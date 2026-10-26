"""Benchmark provenance, equal inputs, persistent budgets and scoring boundaries."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from mastering_benchmark import ALBUMS, BudgetHTTP, evaluate, fingerprint, metrics, write_json
from pipeline_config import SearchPolicy
from search_models import SearchError
from test_release_search import Network
from test_search_mastering import Q, candidates, decision, evidence, extraction


def corpus(path: Path, expected: Any = None) -> Path:
    data = {
        "case_id": "a",
        "query": {"text": Q.text, "artist": Q.artist, "title": Q.title, "release_type": Q.release_type},
        "research": evidence().public(),
        "candidates": [c.public() for c in candidates()],
        "expected": expected,
    }
    key = fingerprint(data)
    write_json(path / "a.json", data)
    write_json(path / "manifest.json", {"cases": [{"file": "a.json", "fingerprint": key}]})
    return path


def test_unlabelled_cases_cannot_report_expert_accuracy() -> None:
    assert len(ALBUMS) == 24 and len(set(ALBUMS)) == 24
    assert metrics({"status": "preference"}, {})["expert_accuracy"] is None
    assert (
        metrics(
            {"status": "preference", "preferred_edition": "wrong"},
            {"expected": {"status": "preference", "acceptable_editions": ["correct"]}},
        )["expert_accuracy"]
        is False
    )


def test_private_atomic_artifact_does_not_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    write_json(path, {"test": 1})
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(SearchError):
        write_json(path, {"test": 2})
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(SearchError):
        write_json(link, {})
    assert json.loads(path.read_text()) == {"test": 1}


def test_budget_is_persisted_before_request_and_prevents_retries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    net = Network(monkeypatch, lambda *args: (200, {}))
    http = BudgetHTTP(SearchPolicy(), 1, tmp_path / "counter")
    assert http.json("POST", "http://fixture.test") == {}
    with pytest.raises(SearchError, match="BUDGET"):
        http.json("POST", "http://fixture.test")
    assert len(net.calls) == 1 and json.loads((tmp_path / "counter" / "call-001.json").read_text())["number"] == 1


def test_two_models_receive_identical_frozen_evidence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-key")
    replies = iter([extraction(evidence()), decision(), extraction(evidence()), decision()])
    net = Network(
        monkeypatch,
        lambda *args: (200, {"candidates": [{"content": {"parts": [{"text": json.dumps(next(replies))}]}}]}),
    )
    p = replace(SearchPolicy(), remote_metadata_disclosure=True)
    result = evaluate(p, corpus(tmp_path / "corpus"), tmp_path / "results", ["gemini:one", "gemini:two"], 4, 1)
    assert result["calls"] == 4
    assert result["results"][0]["fingerprint"] == result["results"][1]["fingerprint"]
    assert net.calls[0][2]["json"]["contents"] == net.calls[2][2]["json"]["contents"]
    assert all(r["metrics"]["expert_accuracy"] is None for r in result["results"])


def test_changed_corpus_is_rejected_before_provider_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-key")
    path = corpus(tmp_path / "corpus")
    data = json.loads((path / "a.json").read_text())
    data["research"]["sources"][0]["excerpt"] += "changed"
    (path / "a.json").write_text(json.dumps(data))
    net = Network(monkeypatch, lambda *args: pytest.fail("Changed evidence must not be sent"))
    with pytest.raises(SearchError, match="Corpus evidence changed"):
        evaluate(
            replace(SearchPolicy(), remote_metadata_disclosure=True), path, tmp_path / "results", ["gemini:one"], 2, 1
        )
    assert net.calls == []


def test_rejected_decision_retains_private_redacted_stage_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-key")
    rejected = {
        **decision(),
        "preferred_edition_id": "nonexistent",
        "rationale": "fixture-key https://private.invalid/?token=x",
    }
    replies = iter([extraction(evidence()), rejected])
    Network(
        monkeypatch,
        lambda *args: (200, {"candidates": [{"content": {"parts": [{"text": json.dumps(next(replies))}]}}]}),
    )
    out = tmp_path / "results"
    result = evaluate(
        replace(SearchPolicy(), remote_metadata_disclosure=True),
        corpus(tmp_path / "corpus"),
        out,
        ["gemini:one"],
        2,
        1,
    )
    row = result["results"][0]
    assert row["metrics"]["status"] == "insufficient_evidence" and row["metrics"]["decision_failed"]
    assert row["review"]["candidate_id"] == "" and len(row["review"]["evidence_graph"]["opinions"]) == 1
    assert set(row["stage_artifacts"]) == {
        "selected_source_ids",
        "extraction",
        "literal_recovery",
        "coverage",
        "validated_graph",
        "decision",
        "decision_failure",
        "fallback_decision",
    }
    saved = out / row["stage_artifacts"]["decision"]
    assert json.loads(saved.read_text())["preferred_edition_id"] == "nonexistent"
    assert "fixture-key" not in saved.read_text() and "private.invalid" not in saved.read_text()
    assert saved.stat().st_mode & 0o777 == 0o600


def test_repeated_service_errors_stop_model_without_retries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-key")
    path = corpus(tmp_path / "corpus")
    data = json.loads((path / "a.json").read_text())
    cases = []
    for i in range(5):
        item = {**data, "case_id": str(i)}
        write_json(path / f"{i}.json", item)
        cases.append({"file": f"{i}.json", "fingerprint": fingerprint(item)})
    (path / "manifest.json").write_text(json.dumps({"cases": cases}))
    net = Network(monkeypatch, lambda *args: (503, {"error": {"status": "UNAVAILABLE"}}))
    result = evaluate(
        replace(SearchPolicy(), remote_metadata_disclosure=True), path, tmp_path / "results", ["gemini:one"], 10, 5
    )
    assert result["calls"] == 3 and len(net.calls) == 3
    assert result["results"][-1]["status"] == "STOPPED_SERVICE_UNAVAILABLE"


def test_service_error_limit_is_shared_across_albums_and_batches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from test_search_mastering_batches import many_sources

    monkeypatch.setenv("GEMINI_API_KEY", "fixture-key")
    path = corpus(tmp_path / "corpus")
    base = json.loads((path / "a.json").read_text())
    cases = []
    for index, count in enumerate((1, 1, 20)):
        item = {**base, "case_id": str(index), "research": many_sources(count).public()}
        write_json(path / f"{index}.json", item)
        cases.append({"file": f"{index}.json", "fingerprint": fingerprint(item)})
    (path / "manifest.json").write_text(json.dumps({"cases": cases}))
    net = Network(monkeypatch, lambda *args: (503, {"error": {"status": "UNAVAILABLE"}}))
    result = evaluate(
        replace(SearchPolicy(), remote_metadata_disclosure=True, mastering_batch_size=1),
        path,
        tmp_path / "results",
        ["gemini:one"],
        30,
        3,
    )
    assert result["calls"] == len(net.calls) == 3
    assert result["results"][-1]["status"] == "STOPPED_SERVICE_UNAVAILABLE"
    assert result["results"][-2]["metrics"]["failed_batches"] == 1
    assert result["results"][-2]["metrics"]["not_attempted_batches"] == 19


def test_resume_preserves_completed_cases_and_stage_records_without_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-key")
    replies = iter([extraction(evidence()), decision()])
    Network(
        monkeypatch,
        lambda *args: (200, {"candidates": [{"content": {"parts": [{"text": json.dumps(next(replies))}]}}]}),
    )
    p = replace(SearchPolicy(), remote_metadata_disclosure=True)
    cp = corpus(tmp_path / "corpus")
    first = tmp_path / "first"
    evaluate(p, cp, first, ["gemini:one"], 2, 1)
    # A process may finish a case but stop before publishing its summary.
    (first / "summary.json").unlink()
    net = Network(monkeypatch, lambda *args: pytest.fail("Completed case must not make a model call"))
    out = tmp_path / "resumed"
    result = evaluate(p, cp, out, ["gemini:one"], 2, 1, resume_from=first)
    assert result["calls"] == 0 and result["resumed_cases"] == 1 and net.calls == []
    row = result["results"][0]
    assert row["resumed"] and row["original_calls"] == 2
    for stage in row["stage_artifacts"].values():
        assert (out / stage).read_bytes() == (first / stage).read_bytes()
        assert (out / stage).stat().st_mode & 0o777 == 0o600


def test_resume_rejects_different_generation_settings_before_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-key")
    replies = iter([extraction(evidence()), decision()])
    Network(
        monkeypatch,
        lambda *args: (200, {"candidates": [{"content": {"parts": [{"text": json.dumps(next(replies))}]}}]}),
    )
    p = replace(SearchPolicy(), remote_metadata_disclosure=True)
    cp = corpus(tmp_path / "corpus")
    first = tmp_path / "first"
    evaluate(p, cp, first, ["gemini:one"], 2, 1)
    net = Network(monkeypatch, lambda *args: pytest.fail("Different protocol must not be silently mixed"))
    with pytest.raises(SearchError, match="evidence/code/settings differ"):
        evaluate(replace(p, mastering_batch_size=1), cp, tmp_path / "resumed", ["gemini:one"], 2, 1, resume_from=first)
    assert net.calls == []


def test_resume_retries_an_unfinished_decision_instead_of_reusing_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-key")
    replies = iter([extraction(evidence()), {**decision(), "preferred_edition_id": "invalid"}])
    Network(
        monkeypatch,
        lambda *args: (200, {"candidates": [{"content": {"parts": [{"text": json.dumps(next(replies))}]}}]}),
    )
    p = replace(SearchPolicy(), remote_metadata_disclosure=True)
    cp = corpus(tmp_path / "corpus")
    first = tmp_path / "first"
    evaluate(p, cp, first, ["gemini:one"], 2, 1)
    replies = iter([extraction(evidence()), decision()])
    net = Network(
        monkeypatch,
        lambda *args: (200, {"candidates": [{"content": {"parts": [{"text": json.dumps(next(replies))}]}}]}),
    )
    result = evaluate(p, cp, tmp_path / "resumed", ["gemini:one"], 2, 1, resume_from=first)
    assert result["calls"] == len(net.calls) == 2 and result["resumed_cases"] == 0
    assert result["results"][0]["review"]["status"] == "preference"
