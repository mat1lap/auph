#!/usr/bin/env python3
"""Isolated Gemini checks: no indexer, topic requests, downloads or automatic retries."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import replace
from typing import Any

from audiophile_tagger import load_env_file
from pipeline_config import load_config
from search_evaluator import EvaluationError, GeminiEvaluator, ResponseFailure, evaluation_failure
from search_models import Candidate, Query, SearchError, public_text, secret_values
from search_providers import HTTP


class CheckHTTP(HTTP):
    def __init__(self, timeout: float) -> None:
        super().__init__(timeout)
        self.calls = 0

    def request(self, method: str, url: str, **kwargs: Any) -> bytes:
        if self.calls >= 2:
            raise SearchError("Check request budget exhausted")
        self.calls += 1
        kwargs["retry"] = False
        return super().request(method, url, **kwargs)


def check(evaluator: GeminiEvaluator, stage: str) -> dict[str, Any]:
    if stage == "minimal":
        diagnostics: dict[str, Any] = {"model": public_text(evaluator.model, secret_values(evaluator.policy))}
        url, headers, template = evaluator.request_payload("", "text")
        body = {
            "contents": [{"role": "user", "parts": [{"text": "Reply with OK."}]}],
            "generationConfig": template["generationConfig"],
        }
        try:
            response = evaluator.http.json(
                "POST", url, retry=False, headers=headers, json=body, diagnostics=diagnostics
            )
        except SearchError as exc:
            raise evaluation_failure(exc, diagnostics, "gemini") from None
        try:
            if not evaluator.response_text(response).strip():
                raise ValueError("empty response")
        except ResponseFailure as exc:
            diagnostics["finish_reason"] = exc.finish_reason
            raise EvaluationError(exc.kind, diagnostics) from None
        except (KeyError, IndexError, TypeError, ValueError):
            raise EvaluationError("INVALID_RESPONSE", diagnostics) from None
        return EvaluationError("SUCCESS", diagnostics).diagnostics
    candidate = Candidate("fixture:1", "https://example.invalid/release/1", "Example Artist - Example Album [CD FLAC]")
    candidate.claim("artist", "Example Artist", "fixture", "Example Artist")
    candidate.claim("release_title", "Example Album", "fixture", "Example Album")
    candidate.claim("torrent_title", candidate.torrent_title, "fixture", candidate.torrent_title)
    evaluator.evaluate(
        Query("Example Artist Example Album", "Example Artist", "Example Album", "album"),
        [candidate],
        allow_schema_fallback=False,
    )
    return candidate.evaluation_diagnostics[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Isolated Gemini check; one generateContent per stage, at most two; no retries. Uses configured model and disclosure permission; may incur API charges."
    )
    parser.add_argument("--config", help="Policy TOML (otherwise AUDIOPHILE_CONFIG/default)")
    parser.add_argument("--stage", choices=("minimal", "evaluation", "both"), default="minimal")
    parser.add_argument(
        "--timeout",
        type=float,
        help="Read timeout in seconds for this check only (1..60); connect timeout remains capped at 5 seconds",
    )
    parser.add_argument("--model", help="Explicit temporary Gemini model override; no automatic model switching")
    parser.add_argument(
        "--max-output-tokens", type=int, help="Temporary output token bound (1..8192, default from config)"
    )
    parser.add_argument(
        "--thinking-level",
        choices=("minimal", "low", "medium", "high"),
        help="Explicit temporary thinking level; model support varies, no automatic retry on rejection",
    )
    parser.add_argument(
        "--structured-mode",
        choices=("schema", "json", "text"),
        help="Temporary evaluation response mode; schema failure is not silently retried",
    )
    args = parser.parse_args(argv)
    if args.max_output_tokens is not None and not 1 <= args.max_output_tokens <= 8192:
        parser.error("--max-output-tokens must be between 1 and 8192")
    if args.timeout is not None and (not math.isfinite(args.timeout) or not 1 <= args.timeout <= 60):
        parser.error("--timeout must be finite and between 1 and 60 seconds")
    load_env_file()
    try:
        policy = replace(load_config(args.config).search, backend="gemini")
        if args.timeout is not None:
            policy = replace(policy, timeout=args.timeout)
        if args.model is not None:
            policy = replace(policy, gemini_model=args.model)
        if args.max_output_tokens is not None:
            policy = replace(policy, gemini_max_output_tokens=args.max_output_tokens)
        if args.thinking_level is not None:
            policy = replace(policy, gemini_thinking_level=args.thinking_level)
        if args.structured_mode is not None:
            policy = replace(policy, structured_mode=args.structured_mode)
        evaluator = GeminiEvaluator(policy, CheckHTTP(policy.timeout))
    except (SearchError, ValueError, TypeError, OSError):
        # Configuration values, paths and exception bodies may themselves contain secrets.
        print(
            "NOT_CONFIGURED: check policy path, gemini_endpoint/model/key_env, key presence and remote_metadata_disclosure"
        )
        return 2
    failed = False
    for stage in ("minimal", "evaluation") if args.stage == "both" else (args.stage,):
        try:
            result = check(evaluator, stage)
        except EvaluationError as exc:
            result = exc.diagnostics
            failed = True
        print(
            json.dumps(
                {
                    "stage": stage,
                    "http_status": None,
                    "provider_code": None,
                    "timeout_phase": None,
                    "connect_timeout_seconds": min(policy.timeout, 5),
                    "read_timeout_seconds": policy.timeout,
                    **result,
                },
                sort_keys=True,
            )
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
