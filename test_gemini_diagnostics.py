"""Gemini diagnostics exercise the real HTTP/evaluator boundary without live calls."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import requests

import check_gemini
from pipeline_config import SearchPolicy, load_config
from search_evaluator import EvaluationError, GeminiEvaluator
from search_models import Query, SearchError
from test_release_search import Network, candidate, response_for


@pytest.fixture
def policy(monkeypatch: pytest.MonkeyPatch) -> SearchPolicy:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-not-a-real-secret")
    return SearchPolicy()


@pytest.mark.parametrize(
    "field", ["jackett_key_env", "gemini_key_env", "openai_key_env", "local_key_env", "topic_cookie_env"]
)
@pytest.mark.parametrize("value", ["secret-containing-a-hyphen", "AIzaSecretWithoutHyphens", "secret\nKEY", ""])
def test_environment_names_reject_credentials_without_echo(field: str, value: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError) as exc:
        SearchPolicy(**dict[str, Any]({field: value}))
    assert value not in str(exc.value) if value else "environment variable name" in str(exc.value)
    path = tmp_path / "policy.toml"
    path.write_text("[search]\n" + field + " = " + json.dumps(value) + "\n")
    with pytest.raises(ValueError) as exc:
        load_config(str(path))
    assert value not in str(exc.value) if value else "environment variable name" in str(exc.value)


@pytest.mark.parametrize(
    "status,code,detail,kind",
    [
        (400, "INVALID_ARGUMENT", {"reason": "API_KEY_INVALID"}, "AUTHENTICATION_ERROR"),
        (401, "UNAUTHENTICATED", {}, "AUTHENTICATION_ERROR"),
        (403, "PERMISSION_DENIED", {}, "PERMISSION_DENIED"),
        (404, "NOT_FOUND", {}, "MODEL_UNAVAILABLE"),
        (429, "RESOURCE_EXHAUSTED", {}, "RATE_LIMITED"),
        (429, "RESOURCE_EXHAUSTED", {"@type": "type.googleapis.com/google.rpc.QuotaFailure"}, "QUOTA_EXHAUSTED"),
        (503, "UNAVAILABLE", {}, "SERVER_ERROR"),
        (400, "INVALID_ARGUMENT", {}, "REQUEST_REJECTED"),
    ],
)
def test_gemini_provider_errors_are_safe_and_not_retried(
    monkeypatch: pytest.MonkeyPatch, policy: SearchPolicy, status: int, code: str, detail: dict[str, str], kind: str
) -> None:
    secret = "do-not-log-key-or-provider-message"
    net = Network(
        monkeypatch,
        lambda *args: (
            status,
            {"error": {"status": code, "message": secret, "details": [detail]}},
            {"Retry-After": "17"},
        ),
    )
    c = candidate()
    evaluator = GeminiEvaluator(
        replace(policy, gemini_model="configured-model", remote_metadata_disclosure=True, structured_mode="schema")
    )
    with pytest.raises(EvaluationError) as exc:
        evaluator.evaluate(Query("Queen Jazz"), [c], allow_schema_fallback=False)
    assert exc.value.diagnostics["failure_kind"] == kind
    assert exc.value.diagnostics["http_status"] == status
    assert exc.value.diagnostics["provider_code"] == code
    assert exc.value.diagnostics["retry_after_seconds"] == 17
    assert secret not in str(exc.value) + json.dumps(c.evaluation_diagnostics)
    assert len(net.calls) == 1


@pytest.mark.parametrize(
    "error,kind",
    [(requests.Timeout("SECRET URL"), "TIMEOUT"), (requests.ConnectionError("SECRET URL"), "TRANSPORT_ERROR")],
)
def test_transport_classification(
    monkeypatch: pytest.MonkeyPatch, policy: SearchPolicy, error: Exception, kind: str
) -> None:
    net = Network(monkeypatch, lambda *args: error)
    evaluator = GeminiEvaluator(replace(policy, gemini_model="configured-model", remote_metadata_disclosure=True))
    with pytest.raises(EvaluationError) as exc:
        evaluator.evaluate(Query("Queen Jazz"), [candidate()])
    assert exc.value.diagnostics["failure_kind"] == kind
    assert "SECRET" not in str(exc.value)
    assert len(net.calls) == 1


@pytest.mark.parametrize(
    "body,kind",
    [
        (b"not JSON", "INVALID_RESPONSE"),
        ({"candidates": []}, "INVALID_RESPONSE"),
        ({"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}, "LOCAL_VALIDATION_ERROR"),
    ],
)
def test_response_vs_local_validation(
    monkeypatch: pytest.MonkeyPatch, policy: SearchPolicy, body: object, kind: str
) -> None:
    Network(monkeypatch, lambda *args: (200, body))
    evaluator = GeminiEvaluator(replace(policy, gemini_model="configured-model", remote_metadata_disclosure=True))
    with pytest.raises(EvaluationError) as exc:
        evaluator.evaluate(Query("Queen Jazz"), [candidate()])
    assert exc.value.diagnostics["failure_kind"] == kind


def test_standalone_two_checks_use_only_gemini(
    monkeypatch: pytest.MonkeyPatch, policy: SearchPolicy, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(method: str, url: str, kwargs: dict):
        assert method == "POST" and url.endswith("/models/configured-model:generateContent")
        if "systemInstruction" not in kwargs["json"]:
            return 200, {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}
        assert "Example Artist" in json.dumps(kwargs["json"])
        assert "Queen" not in json.dumps(kwargs["json"])
        return 200, {"candidates": [{"content": {"parts": [{"text": json.dumps(response_for("fixture:1"))}]}}]}

    net = Network(monkeypatch, handler)
    config = tmp_path / "policy.toml"
    config.write_text('[search]\ngemini_model="configured-model"\nremote_metadata_disclosure=true\n')
    assert check_gemini.main(["--config", str(config), "--stage", "both"]) == 0
    assert len(net.calls) == 2
    assert capsys.readouterr().out.count("SUCCESS") == 2
    http = check_gemini.CheckHTTP(15)
    http.calls = 2
    with pytest.raises(SearchError, match="budget"):
        http.request("POST", "https://example.invalid")
    assert len(net.calls) == 2


def test_auth_failure_never_triggers_schema_downgrade(monkeypatch: pytest.MonkeyPatch, policy: SearchPolicy) -> None:
    net = Network(
        monkeypatch,
        lambda *args: (400, {"error": {"status": "INVALID_ARGUMENT", "details": [{"reason": "API_KEY_INVALID"}]}}),
    )
    evaluator = GeminiEvaluator(
        replace(policy, gemini_model="configured-model", remote_metadata_disclosure=True, structured_mode="schema")
    )
    with pytest.raises(EvaluationError, match="AUTHENTICATION_ERROR"):
        evaluator.evaluate(Query("Queen Jazz"), [candidate()])
    assert len(net.calls) == 1


def test_final_failure_diagnostics_survive_csv_and_manual_fallback(
    monkeypatch: pytest.MonkeyPatch, policy: SearchPolicy, tmp_path: Path
) -> None:
    import csv
    import io

    from release_search import workflow
    from test_search_stages import Discovery

    net = Network(
        monkeypatch,
        lambda *args: (503, {"error": {"status": "UNAVAILABLE", "message": "SECRET MESSAGE"}}, {"Retry-After": "9"}),
    )
    adapter = Discovery()
    path = tmp_path / "results.csv"
    output = io.StringIO()
    p = replace(
        policy, backend="gemini", gemini_model="configured-model", remote_metadata_disclosure=True, details=False
    )
    assert (
        workflow(Query("", "Queen", "Jazz", "album"), p, adapter, path, input_stream=io.StringIO(), output=output) == 0
    )
    rows = list(csv.DictReader(io.StringIO(path.read_text())))
    assert len(rows) == 4 and len(net.calls) == 1
    diagnostics = json.loads(
        next(row for row in rows if row["candidate_id"] == "rutracker:1")["evaluation_diagnostics"]
    )[0]
    assert diagnostics["failure_kind"] == "SERVER_ERROR"
    assert diagnostics["provider_code"] == "UNAVAILABLE"
    assert diagnostics["retry_after_seconds"] == 9
    assert "manual selection" in output.getvalue().lower()
    assert "SECRET MESSAGE" not in output.getvalue() + path.read_text()


@pytest.mark.parametrize(
    "error,phase", [(requests.ConnectTimeout("SECRET"), "connect"), (requests.ReadTimeout("SECRET"), "read")]
)
def test_check_timeout_override_reaches_request_without_retries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], error: Exception, phase: str
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-not-a-real-secret")
    net = Network(monkeypatch, lambda *args: error)
    config = tmp_path / "policy.toml"
    original = '[search]\ngemini_model="configured-model"\nremote_metadata_disclosure=true\ntimeout=15.0\n'
    config.write_text(original)
    assert check_gemini.main(["--config", str(config), "--stage", "evaluation", "--timeout", "60"]) == 1
    assert len(net.calls) == 1
    assert net.calls[0][2]["timeout"] == (5, 60)
    diagnostics = json.loads(capsys.readouterr().out)
    assert diagnostics["failure_kind"] == "TIMEOUT"
    assert diagnostics["timeout_phase"] == phase
    assert diagnostics["http_status"] is None and diagnostics["provider_code"] is None
    assert "SECRET" not in json.dumps(diagnostics)
    assert config.read_text() == original
    assert load_config(str(config)).search.timeout == 15


def test_model_and_output_overrides_are_temporary_and_reach_production(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-not-a-real-secret")
    config = tmp_path / "policy.toml"
    original = '[search]\ngemini_model="original-model"\nremote_metadata_disclosure=true\n'
    config.write_text(original)
    net = Network(
        monkeypatch,
        lambda *args: (
            200,
            {"candidates": [{"content": {"parts": [{"text": json.dumps(response_for("fixture:1"))}]}}]},
        ),
    )
    assert (
        check_gemini.main(
            [
                "--config",
                str(config),
                "--stage",
                "evaluation",
                "--model",
                "listed-test-model",
                "--max-output-tokens",
                "1024",
            ]
        )
        == 0
    )
    assert len(net.calls) == 1
    assert net.calls[0][1].endswith("/models/listed-test-model:generateContent")
    assert net.calls[0][2]["json"]["generationConfig"]["maxOutputTokens"] == 1024
    diagnostics = json.loads(capsys.readouterr().out)
    assert diagnostics["model"] == "listed-test-model" and diagnostics["evaluation_attempt"] == 1
    assert config.read_text() == original and load_config(str(config)).search.gemini_model == "original-model"


@pytest.mark.parametrize("limit", [0, 8193, True])
def test_invalid_output_limit_rejected_before_request(
    monkeypatch: pytest.MonkeyPatch, policy: SearchPolicy, limit: int
) -> None:
    net = Network(monkeypatch, lambda *args: (_ for _ in ()).throw(AssertionError("no request allowed")))
    with pytest.raises(SearchError, match="gemini_max_output_tokens"):
        GeminiEvaluator(
            replace(
                policy, gemini_model="configured-model", remote_metadata_disclosure=True, gemini_max_output_tokens=limit
            )
        )
    assert not net.calls


def test_explicit_thinking_level_is_sent_without_silent_fallback(
    monkeypatch: pytest.MonkeyPatch, policy: SearchPolicy
) -> None:
    net = Network(monkeypatch, lambda *args: (400, {"error": {"status": "INVALID_ARGUMENT"}}))
    c = candidate()
    evaluator = GeminiEvaluator(
        replace(policy, gemini_model="configured-model", gemini_thinking_level="low", remote_metadata_disclosure=True)
    )
    with pytest.raises(EvaluationError, match="REQUEST_REJECTED"):
        evaluator.evaluate(Query("Queen Jazz"), [c])
    assert len(net.calls) == 1
    assert net.calls[0][2]["json"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "LOW"}
    assert c.evaluation_diagnostics[0]["thinking_level"] == "low"
    assert c.evaluation_diagnostics[0]["model"] == "configured-model"


def test_default_thinking_is_omitted_and_bad_level_rejected(
    monkeypatch: pytest.MonkeyPatch, policy: SearchPolicy
) -> None:
    p = replace(policy, gemini_model="configured-model", remote_metadata_disclosure=True)
    evaluator = GeminiEvaluator(p)
    assert "thinkingConfig" not in evaluator.request_payload("fixture", "json")[2]["generationConfig"]
    with pytest.raises(SearchError, match="gemini_thinking_level"):
        GeminiEvaluator(replace(p, gemini_thinking_level="unsupported"))


def test_rejected_model_ids_have_safe_rule_code_and_no_retry(
    monkeypatch: pytest.MonkeyPatch, policy: SearchPolicy
) -> None:
    payload = response_for("invented-id")
    net = Network(
        monkeypatch, lambda *args: (200, {"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]})
    )
    c = candidate()
    evaluator = GeminiEvaluator(replace(policy, gemini_model="configured-model", remote_metadata_disclosure=True))
    with pytest.raises(EvaluationError) as exc:
        evaluator.evaluate(Query("Queen Jazz"), [c])
    assert exc.value.diagnostics["validation_code"] == "CANDIDATE_IDS"
    assert exc.value.diagnostics["model"] == "configured-model"
    assert c.evaluation_diagnostics[-1]["validation_code"] == "CANDIDATE_IDS"
    assert len(net.calls) == 1


def test_schema_check_override_does_not_downgrade_or_edit_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-not-a-real-secret")
    path = tmp_path / "policy.toml"
    original = '[search]\ngemini_model="configured-model"\nremote_metadata_disclosure=true\n'
    path.write_text(original)
    net = Network(monkeypatch, lambda *args: (400, {"error": {"status": "INVALID_ARGUMENT"}}))
    assert check_gemini.main(["--config", str(path), "--stage", "evaluation", "--structured-mode", "schema"]) == 1
    assert len(net.calls) == 1
    assert net.calls[0][2]["json"]["generationConfig"]["responseJsonSchema"]["additionalProperties"] is False
    assert path.read_text() == original
    assert json.loads(capsys.readouterr().out)["failure_kind"] == "REQUEST_REJECTED"


def test_minimal_check_applies_explicit_token_and_thinking_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-not-a-real-secret")
    path = tmp_path / "policy.toml"
    path.write_text('[search]\ngemini_model="configured-model"\nremote_metadata_disclosure=true\n')
    net = Network(monkeypatch, lambda *args: (200, {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}))
    assert (
        check_gemini.main(
            ["--config", str(path), "--stage", "minimal", "--thinking-level", "low", "--max-output-tokens", "1024"]
        )
        == 0
    )
    body = net.calls[0][2]["json"]
    assert body["generationConfig"] == {"maxOutputTokens": 1024, "thinkingConfig": {"thinkingLevel": "LOW"}}
    assert "systemInstruction" not in body
    assert len(net.calls) == 1
    assert json.loads(capsys.readouterr().out)["http_status"] == 200


def test_field_errors_reach_evaluator_without_reflected_data(
    monkeypatch: pytest.MonkeyPatch, policy: SearchPolicy
) -> None:
    secret = "fixture-not-a-real-secret"
    net = Network(
        monkeypatch,
        lambda *args: (
            400,
            {
                "error": {
                    "status": "INVALID_ARGUMENT",
                    "message": "Invalid JSON payload received. Unknown name \"responseJsonSchema\" at 'generation_config': Cannot find field.",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.BadRequest",
                            "fieldViolations": [
                                {
                                    "field": "generation_config.responseJsonSchema",
                                    "description": "Cannot find field: " + secret,
                                },
                                {"field": secret, "description": "invalid reflected request"},
                                {
                                    "field": "contents[0].parts[0].text",
                                    "description": "invalid https://private.invalid/?key=" + secret,
                                },
                            ],
                        }
                    ],
                }
            },
        ),
    )
    c = candidate()
    evaluator = GeminiEvaluator(
        replace(policy, gemini_model="configured-model", remote_metadata_disclosure=True, structured_mode="schema")
    )
    with pytest.raises(EvaluationError) as exc:
        evaluator.evaluate(Query("Queen Jazz"), [c], allow_schema_fallback=False)
    violations = exc.value.diagnostics["provider_field_violations"]
    assert {"field": "generation_config.responseJsonSchema", "description": "Unknown request field"} in violations
    assert len(violations) == 2
    assert secret not in json.dumps(c.evaluation_diagnostics)
    assert "private.invalid" not in json.dumps(c.evaluation_diagnostics)
    assert len(net.calls) == 1


def test_message_only_diagnostics_are_bounded_and_redacted(
    monkeypatch: pytest.MonkeyPatch, policy: SearchPolicy
) -> None:
    message = (
        "Invalid response schema: enum values must be non-empty. "
        "Reflected 'Example Artist Example Album' https://private.invalid/?key=fixture-not-a-real-secret "
        "fixture-not-a-real-secret " + "unexpected-payload " * 1000
    )
    net = Network(monkeypatch, lambda *args: (400, {"error": {"status": "INVALID_ARGUMENT", "message": message}}))
    evaluator = GeminiEvaluator(
        replace(policy, gemini_model="configured-model", remote_metadata_disclosure=True, structured_mode="schema")
    )
    with pytest.raises(EvaluationError) as exc:
        evaluator.evaluate(Query("Queen Jazz"), [candidate()], allow_schema_fallback=False)
    projected = exc.value.diagnostics["provider_field_violations"][0]
    assert projected["field"] == "request"
    assert "enum values must be non-empty" in projected["description"]
    assert len(projected["description"]) <= 350
    assert "unexpected-payload" in projected["description"]  # Unknown words are no longer erased.
    for private in ("Example Artist", "private.invalid", "fixture-not-a-real-secret"):
        assert private not in json.dumps(exc.value.diagnostics)
    assert len(net.calls) == 1
