#!/usr/bin/env python3
"""Synthetic reproducer: independent HTTP, private opt-in reports, no retries."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import requests

from audiophile_tagger import load_env_file
from check_gemini import check
from pipeline_config import SearchPolicy, load_config
from process_lock import file_lock
from search_evaluator import PREFERENCES, SCHEMA, WARNINGS, EvaluationError, GeminiEvaluator
from search_models import SearchError, secret_values
from search_providers import HTTP


class Captured(Exception):
    """Stop payload capture before HTTP or recommendation validation."""


class CaptureHTTP(HTTP):
    def json(self, method: str, url: str, **kwargs: Any) -> Any:
        self.url = url
        self.body = kwargs["json"]
        self.headers = kwargs["headers"]
        raise Captured


def capture_payload(policy: SearchPolicy) -> tuple[str, dict[str, str], dict[str, Any]]:
    capture = CaptureHTTP(policy.timeout)
    try:
        check(GeminiEvaluator(policy, capture), "evaluation")
    except Captured:
        return capture.url, capture.headers, capture.body
    raise SearchError("Payload capture did not stop before network")


def report_secrets(policy: SearchPolicy) -> tuple[str, ...]:
    values = list(secret_values(policy))
    for name, value in os.environ.items():
        if value and re.search(r"KEY|TOKEN|PASSWORD|COOKIE|SECRET|AUTHORIZATION", name, re.IGNORECASE):
            values.append(value)
        if value and re.search(r"(?:https?|all)_proxy", name, re.IGNORECASE):
            values.append(value)
            parsed = urlsplit(value)
            values.extend(unquote(item) for item in (parsed.username, parsed.password) if item)
    return tuple(values)


def clean_local_error(error: object, body: dict[str, Any], secrets: tuple[str, ...]) -> Any:
    """Independent targeted redaction; retains provider vocabulary/numbers and bounded details."""
    reflected: set[str] = set()

    def gather(value: object) -> None:
        if isinstance(value, str) and len(value) >= 2:
            reflected.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                gather(item)
        elif isinstance(value, list):
            for item in value:
                gather(item)

    for part in body.get("contents", []):
        for item in part.get("parts", []):
            text = item.get("text", "")
            gather(text)
            try:
                parsed = json.loads(text)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                gather(parsed.get("query"))
                gather(parsed.get("candidates"))
    gather(body.get("systemInstruction"))
    sensitive = sorted(set(secrets) | reflected, key=len, reverse=True)

    def clean(value: object, depth: int = 0) -> Any:
        if depth > 6:
            return "[depth bounded]"
        if isinstance(value, dict):
            return {str(clean(key, depth + 1)): clean(item, depth + 1) for key, item in list(value.items())[:20]}
        if isinstance(value, list):
            return [clean(item, depth + 1) for item in value[:20]]
        if isinstance(value, str):
            for secret in sensitive:
                if secret:
                    value = value.replace(secret, "[REDACTED]")
                    value = value.replace(json.dumps(secret)[1:-1], "[REDACTED]")
            value = re.sub(
                r"(?im)\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*[:=]\s*[^\r\n]+",
                "[REDACTED credentials]",
                value,
            )
            value = re.sub(r"(?i)(?:https?://|magnet:\?)[^\s<>\"']+", "[URL omitted]", value)
            value = re.sub(
                r"(?i)\b(?:api[_-]?key|passkey|token|password)\s*[=:]\s*[^\s,;]+", "[REDACTED credential]", value
            )
            value = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[REDACTED email]", value)
            value = re.sub(r"(?:/home/|/Users/)[^\s\"']+", "[REDACTED path]", value)
            value = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[REDACTED address]", value)
            value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", value)
            return value[:4000]
        if value is None or isinstance(value, (int, float, bool)):
            return value
        return "[unsupported value]"

    return clean(error)


def project_generated(response: object) -> object:
    """Minimal synthetic regression representation; unknown generated strings are removed."""
    if not isinstance(response, dict):
        return None
    candidates = response.get("candidates", [])
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        return None
    content = candidates[0].get("content", {})
    if not isinstance(content, dict):
        return None
    parts = content.get("parts", [])
    if not isinstance(parts, list):
        return None
    text = "".join(
        part.get("text", "") for part in parts if isinstance(part, dict) and isinstance(part.get("text", ""), str)
    )
    try:
        payload = json.loads(text)
    except ValueError:
        return {"response_code": "INVALID_JSON", "text_length": len(text)}
    keys = set(SCHEMA["required"]) | {"candidate_id", "preference", "evidence_ids"}
    strings = (
        {"fixture:1", ""}
        | set(PREFERENCES)
        | set(WARNINGS)
        | {field for fields in PREFERENCES.values() for field in fields}
    )

    def project(value: object, depth: int = 0) -> object:
        if depth > 5:
            return "_bounded"
        if isinstance(value, dict):
            return {
                key if key in keys else "_unexpected_key": project(item, depth + 1)
                for key, item in list(value.items())[:30]
            }
        if isinstance(value, list):
            return [project(item, depth + 1) for item in value[:30]]
        if isinstance(value, str):
            return value if value in strings else "_unexpected_string"
        return value if value is None or isinstance(value, (int, float, bool)) else "_unexpected_type"

    return project(payload)


def private_write(directory: Path, name: str, data: object) -> Path:
    directory = directory.resolve()
    repo = Path(__file__).resolve().parent
    if directory == repo or repo in directory.parents or Path("/home/emvis/Music") in directory.parents:
        raise SearchError("Diagnostic reports must be outside the repository and music collection")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / name
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return target


def reserve_call(directory: Path) -> int:
    budget = directory / "budget.json"
    with file_lock(budget):
        count = json.loads(budget.read_text())["calls"] if budget.exists() else 0
        if type(count) is not int or not 0 <= count < 4:
            raise SearchError("Four-call diagnostic budget exhausted")
        pending = private_write(directory, f"budget-{count + 1}.pending.json", {"calls": count + 1})
        os.replace(pending, budget)
        return count + 1


def independent_request(
    url: str, headers: dict[str, str], body: dict[str, Any], directory: Path, policy: SearchPolicy
) -> dict[str, Any]:
    call = reserve_call(directory)
    result: dict[str, Any] = {
        "call": call,
        "transport": "independent",
        "model": policy.gemini_model,
        "connect_timeout_seconds": 5,
        "read_timeout_seconds": 60,
    }
    start = time.monotonic()
    with requests.Session() as session:
        session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        try:
            with session.post(
                url, headers=headers, json=body, timeout=(5, 60), verify=True, allow_redirects=False, stream=True
            ) as response:
                result["http_status"] = response.status_code
                result["retry_after"] = response.headers.get("Retry-After")
                raw = bytearray()
                for chunk in response.iter_content(4096):
                    if len(raw) + len(chunk) > 2_000_000 or time.monotonic() - start > 65:
                        raise SearchError("Bounded diagnostic response exceeded limit")
                    raw.extend(chunk)
                try:
                    payload = json.loads(raw)
                except ValueError:
                    result["response_code"] = "NON_JSON_RESPONSE"
                else:
                    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                        error = payload["error"]
                        code = error.get("status")
                        result["provider_code"] = (
                            code
                            if isinstance(code, str)
                            and code
                            in {
                                "INVALID_ARGUMENT",
                                "UNAUTHENTICATED",
                                "PERMISSION_DENIED",
                                "NOT_FOUND",
                                "RESOURCE_EXHAUSTED",
                                "INTERNAL",
                                "UNAVAILABLE",
                                "DEADLINE_EXCEEDED",
                                "FAILED_PRECONDITION",
                            }
                            else "UNKNOWN"
                        )
                        result["error"] = clean_local_error(
                            {k: error[k] for k in ("code", "status", "message", "details") if k in error},
                            body,
                            report_secrets(policy),
                        )
                    elif isinstance(payload, dict):
                        candidates = payload.get("candidates", [])
                        if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
                            result["finish_reason"] = candidates[0].get("finishReason")
                        result["response_received"] = True
                        result["locally_validated"] = False
                        result["synthetic_recommendation"] = project_generated(payload)
        except requests.ConnectTimeout:
            result["failure_kind"], result["timeout_phase"] = "TIMEOUT", "connect"
        except requests.ReadTimeout:
            result["failure_kind"], result["timeout_phase"] = "TIMEOUT", "read"
        except requests.RequestException:
            result["failure_kind"] = "TRANSPORT_ERROR"
    result["duration_seconds"] = round(time.monotonic() - start, 6)
    private_write(directory, f"call-{call}.json", result)
    return result


class BudgetHTTP(HTTP):
    def __init__(self, directory: Path) -> None:
        super().__init__(60)
        self.directory = directory
        self.calls = 0
        self.ordinal = 0
        self.response: object = None

    def json(self, method: str, url: str, **kwargs: Any) -> Any:
        result = super().json(method, url, **kwargs)
        self.response = result
        return result

    def request(self, method: str, url: str, **kwargs: Any) -> bytes:
        if self.calls:
            raise SearchError("One production request per diagnostic invocation")
        self.ordinal = reserve_call(self.directory)
        self.calls += 1
        kwargs["retry"] = False
        return super().request(method, url, **kwargs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Synthetic Gemini wire reproduction. Private opt-in reports outside Git. At most four sequential calls per report directory; no retries, TLS verified. Remote API charges may apply."
    )
    parser.add_argument("--config", default="search.local.toml")
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument(
        "--prepare-only", action="store_true", help="Compare/save synthetic payload, without any API call"
    )
    parser.add_argument("--transport", choices=("independent", "production"), default="independent")
    parser.add_argument(
        "--omit-response-schema",
        action="store_true",
        help="Explicit one-factor diagnostic control; leaves prompt and other generationConfig fields unchanged",
    )
    args = parser.parse_args(argv)
    load_env_file()
    policy = replace(
        load_config(args.config).search,
        backend="gemini",
        structured_mode="schema",
        timeout=60,
        gemini_max_output_tokens=2048,
        gemini_thinking_level="low",
    )
    url, headers, body = capture_payload(policy)
    wire = requests.Request("POST", url, headers=headers, json=body).prepare().body
    assert isinstance(wire, bytes)
    assert json.loads(wire) == body
    if args.prepare_only:
        private_write(args.report_dir, "synthetic-payload.json", body)
        private_write(
            args.report_dir,
            "comparison.json",
            {
                "payload_sha256": hashlib.sha256(wire).hexdigest(),
                "payload_bytes": len(wire),
                "semantic_differences": [],
                "same_endpoint_model_key": True,
                "transport_differences": [
                    "Independent Session explicitly sets max_retries=0 and verify=True; production defaults are equivalent",
                    "Independent errors bypass production HTTP parser and sanitizer",
                ],
            },
        )
        print("Synthetic payload prepared; semantic differences: 0; API calls: 0")
        return 0
    frozen = json.loads((args.report_dir / "synthetic-payload.json").read_text())
    if frozen != body:
        raise SearchError("Production payload changed; prepare a fresh comparison before calling")
    if args.omit_response_schema:
        if args.transport != "independent":
            raise SearchError("Control is independent only; production verification must use actual adapter")
        body["generationConfig"].pop("responseJsonSchema")
    if args.transport == "independent":
        result = independent_request(url, headers, body, args.report_dir, policy)
    else:
        http = BudgetHTTP(args.report_dir)
        try:
            result = check(GeminiEvaluator(policy, http), "evaluation")
        except EvaluationError as exc:
            result = exc.diagnostics
        if http.response is not None:
            if isinstance(http.response, dict):
                candidates = http.response.get("candidates", [])
                if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
                    result["finish_reason"] = candidates[0].get("finishReason")
            if result.get("failure_kind") == "LOCAL_VALIDATION_ERROR":
                private_write(
                    args.report_dir,
                    f"call-{http.ordinal}-invalid-recommendation.json",
                    project_generated(http.response),
                )
        result.update(call=http.ordinal, transport="production")
        private_write(args.report_dir, f"call-{http.ordinal}.json", result)
    summary = {
        key: result[key]
        for key in (
            "call",
            "transport",
            "model",
            "http_status",
            "provider_code",
            "duration_seconds",
            "finish_reason",
            "validation_code",
            "failure_kind",
            "locally_validated",
        )
        if key in result
    }
    print(json.dumps(summary, sort_keys=True))
    if args.transport == "production":
        return 0 if result.get("failure_kind") == "SUCCESS" else 1
    status = result.get("http_status")
    return 0 if isinstance(status, int) and 200 <= status < 300 and result.get("response_received") else 1


if __name__ == "__main__":
    raise SystemExit(main())
