#!/usr/bin/env python3
"""Freeze public research once and compare models on identical evidence, without torrents."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from pipeline_config import SearchPolicy, load_config
from release_search import validate_policy
from search_evaluator import EvaluationError, LLMEvaluator, build_evaluator
from search_models import Candidate, Query, SearchError, public_text, secret_values
from search_providers import HTTP
from search_research import ForumResearch, Research

ALBUMS = [
    ("Queen", "Jazz"),
    ("Queen", "Innuendo"),
    ("Queen", "A Night at the Opera"),
    ("Queen", "News of the World"),
    ("Pink Floyd", "The Dark Side of the Moon"),
    ("Pink Floyd", "Wish You Were Here"),
    ("Pink Floyd", "Animals"),
    ("Pink Floyd", "The Wall"),
    ("The Beatles", "Abbey Road"),
    ("The Beatles", "Revolver"),
    ("The Beatles", "Sgt. Pepper's Lonely Hearts Club Band"),
    ("The Beatles", "The Beatles"),
    ("Led Zeppelin", "Led Zeppelin"),
    ("Led Zeppelin", "Led Zeppelin II"),
    ("Led Zeppelin", "Led Zeppelin IV"),
    ("Led Zeppelin", "Houses of the Holy"),
    ("Dire Straits", "Brothers in Arms"),
    ("Dire Straits", "Love over Gold"),
    ("Genesis", "Duke"),
    ("Genesis", "A Trick of the Tail"),
    ("Steely Dan", "Aja"),
    ("Steely Dan", "Gaucho"),
    ("Fleetwood Mac", "Rumours"),
    ("Eagles", "Hotel California"),
]


def write_json(path: Path, data: Any) -> None:
    """Private atomic artifact; never overwrite unrelated files or follow symlinks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.exists():
        raise SearchError("Artifact already exists; use a new output directory")
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".mastering-")
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.write("\n")
        # link publishes without clobbering an intervening user file.
        os.link(name, path)
    finally:
        os.unlink(name)


def fingerprint(snapshot: dict[str, Any]) -> str:
    data = {k: snapshot[k] for k in ("query", "research", "candidates")}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def protocol_fingerprint(policy: SearchPolicy) -> str:
    """Bind resumable results to code and generation settings, never credentials."""
    names = (
        "structured_mode",
        "mastering_max_sources",
        "mastering_batch_size",
        "mastering_literal_recovery",
        "mastering_output_tokens",
        "timeout",
        "gemini_thinking_level",
        "openai_api",
        "openai_reasoning_effort",
    )
    data: dict[str, Any] = {n: getattr(policy, n) for n in names}
    data.update(
        backend=policy.backend, model=getattr(policy, policy.backend + "_model"), python=list(sys.version_info[:2])
    )
    data["endpoint"] = (
        policy.openai_responses_endpoint
        if policy.backend == "openai" and policy.openai_api == "responses"
        else getattr(policy, policy.backend + "_endpoint")
    )
    root = Path(__file__).parent
    data["code"] = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in (
            "mastering_benchmark.py",
            "search_evaluator.py",
            "search_mastering.py",
            "search_mastering_batches.py",
            "search_literal_recovery.py",
            "search_opinions.py",
            "search_evidence_quality.py",
            "search_edition_identity.py",
            "search_models.py",
            "search_research.py",
            "search_providers.py",
            "pipeline_config.py",
        )
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def resume_rows(directory: Path | None) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    if directory is None:
        return result
    if not directory.is_dir():
        raise SearchError("Resume directory does not exist")
    # Also works after interruption before summary.json was published.
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink():
            raise SearchError("Symlinked resume artifact refused")
        row = json.loads(path.read_text())
        if not isinstance(row, dict) or not all(
            isinstance(row.get(k), str) for k in ("case_id", "model", "fingerprint")
        ):
            continue
        key = row["model"], row["case_id"]
        if key in result:
            raise SearchError("Duplicate resume result")
        result[key] = row
    if not result:
        raise SearchError("No evaluated case records in resume directory")
    return result


def reusable_result(row: dict[str, Any]) -> bool:
    review = row.get("review")
    return bool(
        not row.get("error")
        and isinstance(review, dict)
        and review.get("decision_status") in {"succeeded", "not_required"}
        and review.get("status") == row.get("metrics", {}).get("status")
        and not any(b["status"] != "succeeded" for b in review.get("extraction_coverage", {}).get("batches", []))
    )


def copy_resume_stages(row: dict[str, Any], source: Path, destination: Path, secrets: tuple[str, ...]) -> None:
    for relative in row.get("stage_artifacts", {}).values():
        path = Path(relative)
        if path.is_absolute() or len(path.parts) != 2 or path.parts[0] != "stages" or path.name in {".", ".."}:
            raise SearchError("Unsafe resume stage path")
        original = source / path
        if original.is_symlink() or not original.resolve().is_relative_to(source.resolve()):
            raise SearchError("Unsafe resume stage link")
        write_json(destination / path, private_trace(json.loads(original.read_text()), secrets))


def private_trace(value: object, secrets: tuple[str, ...]) -> object:
    """Preserve parsed stage structure while redacting credentials and arbitrary URLs."""
    if isinstance(value, str):
        return public_text(value, secrets)
    if isinstance(value, list):
        return [private_trace(v, secrets) for v in value]
    if isinstance(value, dict):
        return {public_text(k, secrets): private_trace(v, secrets) for k, v in value.items()}
    return value


def collect(policy: SearchPolicy, directory: Path, count: int, queries: int, pages: int) -> list[dict[str, Any]]:
    policy = replace(
        policy,
        research_queries=queries,
        research_pages=pages,
        research_cache_directory="",
        research_openai_discovery=False,
    )
    validate_policy(policy)
    if policy.research_backend == "none":
        raise SearchError("Configure public research before collecting the corpus")
    entries = []
    for index, (artist, title) in enumerate(ALBUMS[:count], 1):
        q = Query("", artist, title, "album")
        r = ForumResearch(policy).collect(q, [])
        c = Candidate("benchmark:unknown-rip", "https://example.invalid/", artist + " - " + title)
        c.claim("artist", artist, "benchmark", artist)
        c.claim("release_title", title, "benchmark", title)
        snapshot: dict[str, Any] = {
            "version": 1,
            "case_id": f"album-{index:02}",
            "query": asdict(q),
            "research": r.public(),
            "candidates": [c.public()],
            "expected": None,
            "provenance": "Live public forum research. Candidate is a synthetic unidentified rip, not a real torrent. No expert best-mastering label has been asserted.",
        }
        snapshot["fingerprint"] = fingerprint(snapshot)
        filename = snapshot["case_id"] + ".json"
        write_json(directory / filename, snapshot)
        entries.append(
            {
                "case_id": snapshot["case_id"],
                "file": filename,
                "fingerprint": snapshot["fingerprint"],
                "research_status": r.status,
                "source_count": len(r.sources),
            }
        )
        print(
            json.dumps(
                {
                    "case": snapshot["case_id"],
                    "artist": artist,
                    "title": title,
                    "status": r.status,
                    "sources": len(r.sources),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    write_json(directory / "manifest.json", {"version": 1, "cases": entries})
    return entries


class BudgetHTTP(HTTP):
    def __init__(self, policy: SearchPolicy, limit: int, counter_path: Path) -> None:
        super().__init__(policy.timeout)
        self.limit = limit
        self.used = 0
        self.counter_path = counter_path
        self.counter_path.mkdir(parents=True, exist_ok=False)

    def json(self, method: str, url: str, **kwargs: Any) -> Any:
        if self.used >= self.limit:
            raise SearchError("MODEL_CALL_BUDGET_EXHAUSTED")
        self.used += 1
        # Persist consumption BEFORE sending; interruptions cannot hide requests.
        write_json(self.counter_path / f"call-{self.used:03}.json", {"number": self.used})
        return super().json(method, url, **kwargs)


def metrics(
    review: dict[str, Any] | None, snapshot: dict[str, Any], *, coverage: dict[str, Any] | None = None
) -> dict[str, Any]:
    expected = snapshot.get("expected")
    status = review.get("status") if review else "failed"
    graph = review.get("evidence_graph", {}) if review else {}
    opinions = graph.get("opinions", [])
    accounts = {o.get("author") for o in opinions if o.get("author")}
    result: dict[str, Any] = {
        "status": status,
        "opinion_count": len(opinions),
        "distinct_accounts": len(accounts),
        "direct_comparisons": sum(o.get("comparison_type") == "direct_reported" for o in opinions),
        "dissent_count": len(review.get("dissent_opinion_ids", [])) if review else 0,
        "expert_accuracy": None,
        "mapping_accuracy": None,
        "decision_failed": (review or {}).get("decision_status") == "failed",
    }
    coverage = coverage or (review or {}).get("extraction_coverage", {})
    result["source_status_counts"] = coverage.get("source_status_counts", {})
    result["selected_sources"] = coverage.get("selected_sources")
    result["failed_batches"] = sum(b["status"] == "request_failed" for b in coverage.get("batches", []))
    result["not_attempted_batches"] = sum(b["status"] == "not_attempted" for b in coverage.get("batches", []))
    if isinstance(expected, dict) and review:
        acceptable = expected.get("acceptable_editions")
        if isinstance(acceptable, list):
            result["expert_accuracy"] = status == expected.get("status") and (
                status != "preference" or review.get("preferred_edition") in acceptable
            )
        if "candidate_id" in expected:
            result["mapping_accuracy"] = review.get("candidate_id") == expected["candidate_id"]
    return result


def evaluate(
    policy: SearchPolicy,
    corpus: Path,
    directory: Path,
    models: list[str],
    max_calls: int,
    max_cases: int,
    *,
    resume_from: Path | None = None,
) -> dict[str, Any]:
    manifest = json.loads((corpus / "manifest.json").read_text())
    http = BudgetHTTP(policy, max_calls, directory / "request-counter")
    rows = []
    cached_rows = resume_rows(resume_from)
    for spec in models:
        backend, separator, model = spec.partition(":")
        if not separator or backend not in {"gemini", "openai", "local"} or not model:
            raise SearchError("Model format must be backend:model")
        if http.used >= max_calls:
            rows.append({"model": spec, "status": "NOT_EVALUATED_CALL_BUDGET", "calls": 0})
            continue
        overrides: dict[str, Any] = {"backend": backend, backend + "_model": model}
        if backend == "openai":
            overrides["openai_api"] = "responses"
        p = replace(policy, **overrides)
        protocol = protocol_fingerprint(p)
        try:
            evaluator = build_evaluator(p, http)
        except SearchError:
            rows.append({"model": spec, "status": "NOT_CONFIGURED", "calls": 0})
            continue
        if not isinstance(evaluator, LLMEvaluator):
            raise SearchError("Unsupported benchmark evaluator")
        consecutive_service_errors = 0
        for item in manifest["cases"][:max_cases]:
            filename = item["file"]
            if not isinstance(filename, str) or Path(filename).name != filename:
                raise SearchError("Unsafe corpus path")
            snapshot = json.loads((corpus / filename).read_text())
            if fingerprint(snapshot) != item["fingerprint"]:
                raise SearchError("Corpus evidence changed; freeze a new manifest")
            cached = cached_rows.get((spec, snapshot["case_id"]))
            if cached:
                if cached["fingerprint"] != item["fingerprint"] or cached.get("protocol_fingerprint") != protocol:
                    raise SearchError("Resume evidence/code/settings differ; use a fresh evaluation")
                if reusable_result(cached):
                    assert resume_from is not None
                    copy_resume_stages(cached, resume_from, directory, secret_values(p))
                    reused = {
                        **cached,
                        "calls": 0,
                        "seconds": 0.0,
                        "resumed": True,
                        "original_calls": cached.get("original_calls", cached["calls"]),
                    }
                    rows.append(reused)
                    slug = hashlib.sha256(spec.encode()).hexdigest()[:12]
                    write_json(directory / (slug + "-" + snapshot["case_id"] + ".json"), reused)
                    print(
                        json.dumps({k: reused[k] for k in ("model", "case_id", "calls", "resumed", "metrics")}),
                        flush=True,
                    )
                    continue
            q = Query(**snapshot["query"])
            candidates = [Candidate(**c) for c in snapshot["candidates"]]
            r = Research(**snapshot["research"])
            started, before = time.monotonic(), http.used
            review = None
            error = None
            diagnostics: dict[str, Any] = {}
            slug = hashlib.sha256(spec.encode()).hexdigest()[:12]
            stage_artifacts: dict[str, str] = {}
            coverage: dict[str, Any] = {}

            def trace(
                stage: str,
                value: object,
                *,
                prefix: str = f"{slug}-{Path(filename).stem}",
                secrets: tuple[str, ...] = secret_values(p),
                files: dict[str, str] = stage_artifacts,
                source_coverage: dict[str, Any] = coverage,
            ) -> None:
                artifact = f"{prefix}-{stage}.json"
                cleaned = private_trace(value, secrets)
                write_json(directory / "stages" / artifact, cleaned)
                files[stage] = "stages/" + artifact
                if stage == "coverage" and isinstance(cleaned, dict):
                    source_coverage.update(cleaned)

            try:
                review = evaluator.evaluate_mastering(
                    q, candidates, r, trace=trace, service_failure_streak=consecutive_service_errors
                ).mastering_review
            except SearchError as exc:
                error = public_text(exc, secret_values(p))
                if isinstance(exc, EvaluationError):
                    diagnostics = exc.diagnostics
            terminal = [
                b["diagnostics"]
                for b in coverage.get("batches", [])
                if b["diagnostics"] and b["stopped_remaining_calls"]
            ]
            if review and review.get("decision_diagnostics"):
                diagnostics = review["decision_diagnostics"]
            if terminal and not diagnostics:
                diagnostics = terminal[-1]
            row = {
                "model": spec,
                "case_id": snapshot["case_id"],
                "fingerprint": item["fingerprint"],
                "protocol_fingerprint": protocol,
                "calls": http.used - before,
                "seconds": round(time.monotonic() - started, 3),
                "metrics": metrics(review, snapshot, coverage=coverage),
                "review": review,
                "error": error,
                "diagnostics": diagnostics,
                "stage_artifacts": stage_artifacts,
            }
            rows.append(row)
            write_json(directory / (slug + "-" + snapshot["case_id"] + ".json"), row)
            print(json.dumps({k: row[k] for k in ("model", "case_id", "calls", "seconds", "metrics")}), flush=True)
            if coverage.get("batches"):
                for batch in coverage["batches"]:
                    if batch["status"] == "succeeded":
                        consecutive_service_errors = 0
                    elif batch["status"] == "request_failed":
                        consecutive_service_errors = (
                            consecutive_service_errors + 1
                            if batch["diagnostics"].get("http_status") in {500, 502, 503, 504}
                            else 0
                        )
                if error and not any(b["status"] == "request_failed" for b in coverage["batches"]):
                    consecutive_service_errors = 1 if diagnostics.get("http_status") in {500, 502, 503, 504} else 0
                if review and review.get("decision_status") == "failed":
                    consecutive_service_errors = (
                        consecutive_service_errors + 1 if diagnostics.get("http_status") in {500, 502, 503, 504} else 0
                    )
            elif http.used > before:
                consecutive_service_errors = (
                    consecutive_service_errors + 1 if diagnostics.get("http_status") in {500, 502, 503, 504} else 0
                )
            if (
                diagnostics.get("failure_kind") == "MODEL_CALL_BUDGET_EXHAUSTED"
                or error == "MODEL_CALL_BUDGET_EXHAUSTED"
            ):
                rows.append({"model": spec, "status": "STOPPED_CALL_BUDGET", "calls": 0})
                break
            if diagnostics.get("http_status") in {400, 401, 403, 404, 407, 429, 451}:
                rows.append(
                    {
                        "model": spec,
                        "status": "STOPPED_PROVIDER_UNAVAILABLE",
                        "calls": 0,
                        "http_status": diagnostics["http_status"],
                    }
                )
                break
            if consecutive_service_errors >= 3:
                rows.append({"model": spec, "status": "STOPPED_SERVICE_UNAVAILABLE", "calls": 0})
                break
    summary = {
        "version": 1,
        "calls": http.used,
        "budget": max_calls,
        "resumed_cases": sum(bool(r.get("resumed")) for r in rows),
        "results": rows,
        "interpretation": "Unlabelled cases measure evidence/contract behaviour, NOT best-mastering accuracy. Models see identical frozen evidence; synthetic candidate mapping cannot prove real torrent identity.",
    }
    write_json(directory / "summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    from audiophile_tagger import load_env_file

    load_env_file()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("collect", "evaluate"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--corpus", type=Path)
    parser.add_argument(
        "--resume-from",
        type=Path,
        help="Reuse completed cases with identical code/settings/evidence, writing a new output directory",
    )
    parser.add_argument("--cases", type=int, default=24)
    parser.add_argument("--queries", type=int, default=2)
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--models", nargs="+", default=[])
    parser.add_argument("--max-calls", type=int, default=48)
    parser.add_argument("--timeout", type=float, help="Command-local provider timeout (1..60 seconds)")
    parser.add_argument("--thinking-level", choices=("minimal", "low", "medium", "high"))
    parser.add_argument(
        "--batch-size", type=int, help="Command-local extraction size, 1..100; 100 for a single-call baseline"
    )
    args = parser.parse_args(argv)
    if not 1 <= args.cases <= 24 or not 1 <= args.max_calls <= 192:
        parser.error("cases must be 1..24; max-calls must be 1..192")
    policy = load_config(args.config).search
    overrides: dict[str, Any] = {}
    if args.timeout is not None:
        overrides["timeout"] = args.timeout
    if args.thinking_level is not None:
        overrides["gemini_thinking_level"] = args.thinking_level
    if args.batch_size is not None:
        overrides["mastering_batch_size"] = args.batch_size
    policy = replace(policy, **overrides)
    validate_policy(policy)
    try:
        if args.action == "collect":
            collect(policy, args.output, args.cases, args.queries, args.pages)
        else:
            if args.corpus is None or not args.models:
                parser.error("evaluate requires --corpus and --models")
            evaluate(
                policy, args.corpus, args.output, args.models, args.max_calls, args.cases, resume_from=args.resume_from
            )
    except SearchError as exc:
        print("Benchmark failed: " + public_text(exc, secret_values(policy)))
        return 1
    except (OSError, ValueError, TypeError):
        print("Benchmark failed; inspect existing private artifacts and use a new output directory.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
