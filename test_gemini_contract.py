"""Contract regressions use the real evaluator; only HTTP is mocked."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from pipeline_config import SearchPolicy
from search_evaluator import EvaluationError, GeminiEvaluator
from search_models import Query
from test_release_search import Network, candidate


@pytest.fixture
def policy(monkeypatch: pytest.MonkeyPatch) -> SearchPolicy:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-not-a-real-secret")
    return SearchPolicy(gemini_model="configured-model", remote_metadata_disclosure=True)


def test_max_tokens_is_not_a_contract_violation(monkeypatch: pytest.MonkeyPatch, policy: SearchPolicy) -> None:
    net = Network(
        monkeypatch,
        lambda *args: (
            200,
            {
                "candidates": [
                    {"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": '{"selected_candidate_id":'}]}}
                ]
            },
        ),
    )
    c = candidate()
    with pytest.raises(EvaluationError) as exc:
        GeminiEvaluator(policy).evaluate(Query("Queen Jazz"), [c])
    assert exc.value.diagnostics["failure_kind"] == "OUTPUT_TRUNCATED"
    assert exc.value.diagnostics["finish_reason"] == "MAX_TOKENS"
    assert "validation_code" not in exc.value.diagnostics
    assert len(net.calls) == 1


def test_model_gets_the_real_reason_evidence_contract(monkeypatch: pytest.MonkeyPatch, policy: SearchPolicy) -> None:
    net = Network(monkeypatch, lambda *args: (503, {"error": {"status": "UNAVAILABLE"}}))
    c = candidate()
    c.claim("artist", "Queen", "fixture", "Queen")
    with pytest.raises(EvaluationError):
        GeminiEvaluator(policy).evaluate(Query("Queen Jazz"), [c])
    data = json.loads(net.calls[0][2]["json"]["contents"][0]["parts"][0]["text"])
    allowed = data["allowed_reasons"][c.candidate_id]
    assert "artist" in allowed["identity"]
    assert "artist" not in allowed.get("uncertain", [])
    assert "availability" not in allowed and "lossless" not in allowed
    schema = data["required_schema"]
    assert schema["properties"]["selected_candidate_id"]["enum"] == ["", c.candidate_id]
    assert schema["properties"]["ranked_candidate_ids"]["maxItems"] == 5
    assert data["abstention_example"]["selected_candidate_id"] == ""


def test_reason_code_reaches_check_and_csv(
    monkeypatch: pytest.MonkeyPatch, policy: SearchPolicy, tmp_path, capsys
) -> None:
    import csv
    import io

    import check_gemini
    from release_search import workflow
    from test_search_stages import Discovery

    fixture = json.loads(Path("tests/fixtures/search/gemini_reason_evidence_mismatch.json").read_text())
    net = Network(
        monkeypatch,
        lambda *args: (
            200,
            {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(fixture)}]}}]},
        ),
    )
    config = tmp_path / "policy.toml"
    config.write_text('[search]\ngemini_model="configured-model"\nremote_metadata_disclosure=true\n')
    assert check_gemini.main(["--config", str(config), "--stage", "evaluation"]) == 1
    diagnostics = json.loads(capsys.readouterr().out)
    assert diagnostics["validation_code"] == "EVIDENCE_REFERENCE"
    assert diagnostics["failure_kind"] == "LOCAL_VALIDATION_ERROR"
    adapter = Discovery()
    fixture["selected_candidate_id"] = "rutracker:1"
    fixture["ranked_candidate_ids"] = ["rutracker:1"]
    fixture["reasons"][0]["candidate_id"] = "rutracker:1"
    path = tmp_path / "results.csv"
    out = io.StringIO()
    assert (
        workflow(
            Query("", "Queen", "Jazz", "album"),
            replace(policy, backend="gemini", details=False),
            adapter,
            path,
            input_stream=io.StringIO(),
            output=out,
        )
        == 0
    )
    rows = list(csv.DictReader(io.StringIO(path.read_text())))
    assert len(rows) == 4
    diagnostics = json.loads(
        next(row for row in rows if row["candidate_id"] == "rutracker:1")["evaluation_diagnostics"]
    )[0]
    assert diagnostics["validation_code"] == "EVIDENCE_REFERENCE"
    assert "manual selection" in out.getvalue().lower()
    assert len(net.calls) == 2


def test_explicit_abstention_and_schema_match_without_global_mutation(
    monkeypatch: pytest.MonkeyPatch, policy: SearchPolicy
) -> None:
    from search_evaluator import ABSTENTION, SCHEMA

    original = json.dumps(SCHEMA, sort_keys=True)
    net = Network(
        monkeypatch,
        lambda *args: (
            200,
            {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(ABSTENTION)}]}}]},
        ),
    )
    result = GeminiEvaluator(replace(policy, structured_mode="schema")).evaluate(Query("Queen Jazz"), [candidate()])
    assert result.selected_candidate_id == "" and not result.ranked_candidate_ids
    schema = net.calls[0][2]["json"]["generationConfig"]["responseJsonSchema"]
    assert "" in schema["properties"]["selected_candidate_id"]["enum"]
    assert "minItems" not in schema["properties"]["ranked_candidate_ids"]
    assert json.dumps(SCHEMA, sort_keys=True) == original


@pytest.mark.parametrize(
    "finish,text,kind",
    [
        ("STOP", '{"incomplete":', "INVALID_RESPONSE"),
        ("SAFETY", "SECRET RESPONSE", "REFUSED_RESPONSE"),
        ("MAX_TOKENS", "{}", "OUTPUT_TRUNCATED"),
    ],
)
def test_termination_precedes_json_and_local_validation(
    monkeypatch: pytest.MonkeyPatch, policy: SearchPolicy, finish: str, text: str, kind: str
) -> None:
    net = Network(
        monkeypatch,
        lambda *args: (200, {"candidates": [{"finishReason": finish, "content": {"parts": [{"text": text}]}}]}),
    )
    with pytest.raises(EvaluationError) as exc:
        GeminiEvaluator(policy).evaluate(Query("Queen Jazz"), [candidate()])
    assert exc.value.diagnostics["failure_kind"] == kind
    assert "validation_code" not in exc.value.diagnostics
    assert "SECRET RESPONSE" not in str(exc.value)
    if finish == "STOP":
        assert exc.value.diagnostics["response_code"] == "INVALID_JSON"
    else:
        assert exc.value.diagnostics["finish_reason"] == finish
    assert len(net.calls) == 1
