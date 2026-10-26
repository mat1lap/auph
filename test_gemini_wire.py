"""Offline contract and privacy checks for reproducible Gemini diagnostics."""

from __future__ import annotations

import pytest

from search_providers import safe_provider_violations


@pytest.mark.parametrize(
    "message,fragment",
    [
        ("Unsupported parameter: responseJsonSchema for this model.", "responseJsonSchema"),
        ("Invalid enum value: enum must not contain an empty string.", "empty string"),
        ("Schema nesting depth 12 exceeds the maximum supported depth 8.", "depth 12"),
        ("generationConfig.responseSchema and responseJsonSchema are mutually exclusive.", "mutually exclusive"),
    ],
)
def test_error_explanation_survives(message: str, fragment: str) -> None:
    violations = safe_provider_violations({"message": message}, ())
    assert fragment in violations[0]["description"]


def test_unknown_parameter_field_violation_is_retained() -> None:
    violations = safe_provider_violations(
        {
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.BadRequest",
                    "fieldViolations": [
                        {
                            "field": "generationConfig.futureSetting",
                            "description": "Unsupported parameter futureSetting; maximum nesting depth is 8.",
                        }
                    ],
                }
            ]
        },
        (),
    )
    assert violations[0]["field"] == "generationConfig.futureSetting"
    assert "depth is 8" in violations[0]["description"]


def test_private_report_redacts_credentials_and_reflections(tmp_path) -> None:
    import json
    import stat

    from check_gemini_wire import clean_local_error, private_write

    body = {
        "systemInstruction": {"parts": [{"text": "PRIVATE SYSTEM TEXT"}]},
        "contents": [
            {
                "parts": [
                    {
                        "text": json.dumps(
                            {
                                "query": {"artist": "Private Artist"},
                                "candidates": [{"torrent_title": "Private Album Title"}],
                            }
                        )
                    }
                ]
            }
        ],
    }
    error = {
        "message": "Schema nesting depth 12 exceeds 8. Private Artist Private Album Title PRIVATE SYSTEM TEXT real-key\nAuthorization: Bearer abc\nCookie: sid=abc\nhttps://user:pass@private.invalid/?key=secret alice@example.invalid /home/alice/private\n",
        "details": [
            {
                "fieldViolations": [
                    {
                        "field": "generationConfig.futureSetting",
                        "description": "Unsupported futureSetting with depth 12",
                    }
                ]
            }
        ],
    }
    cleaned = clean_local_error(error, body, ("real-key",))
    text = json.dumps(cleaned)
    for sensitive in (
        "Private Artist",
        "Private Album Title",
        "PRIVATE SYSTEM TEXT",
        "real-key",
        "Bearer abc",
        "sid=abc",
        "private.invalid",
        "alice@example.invalid",
        "/home/alice",
    ):
        assert sensitive not in text
    assert "depth 12 exceeds 8" in text and "futureSetting" in text
    report = private_write(tmp_path / "reports", "error.json", cleaned)
    assert stat.S_IMODE(report.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        private_write(tmp_path / "reports", "error.json", cleaned)


def test_capture_exact_production_payload_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    from dataclasses import replace

    import requests

    from check_gemini_wire import capture_payload
    from pipeline_config import SearchPolicy

    monkeypatch.setenv("GEMINI_API_KEY", "fixture-not-secret")

    def refuse(*args, **kwargs):
        pytest.fail("Offline preparation must never call HTTP")

    monkeypatch.setattr(requests.Session, "request", refuse)
    policy = replace(
        SearchPolicy(),
        remote_metadata_disclosure=True,
        gemini_model="configured-model",
        structured_mode="schema",
        gemini_thinking_level="low",
    )
    url, headers, body = capture_payload(policy)
    assert url.endswith("/models/configured-model:generateContent")
    assert headers == {"x-goog-api-key": "fixture-not-secret"}
    assert "fixture-not-secret" not in json.dumps(body)
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "LOW"}
    serialized = requests.Request("POST", url, json=body).prepare().body
    assert isinstance(serialized, bytes)
    assert json.loads(serialized) == body


def test_budget_persists_before_independent_send(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import json

    import requests

    from check_gemini_wire import independent_request, reserve_call
    from pipeline_config import SearchPolicy

    calls = []

    def refuse(self, *args, **kwargs):
        assert json.loads((tmp_path / "budget.json").read_text())["calls"] == 1
        assert kwargs["timeout"] == (5, 60) and kwargs["verify"] is True
        assert kwargs["allow_redirects"] is False
        calls.append(kwargs)
        raise requests.ReadTimeout("secret must not be logged")

    monkeypatch.setattr(requests.Session, "post", refuse)
    result = independent_request("https://example.invalid", {}, {}, tmp_path, SearchPolicy())
    assert result["timeout_phase"] == "read" and len(calls) == 1
    assert "secret must not be logged" not in json.dumps(result)
    for expected in (2, 3, 4):
        assert reserve_call(tmp_path) == expected
    from search_models import SearchError

    with pytest.raises(SearchError, match="budget exhausted"):
        reserve_call(tmp_path)


def test_saved_fixture_does_not_repair_invented_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from check_gemini_wire import project_generated
    from pipeline_config import SearchPolicy
    from search_evaluator import validate_recommendation
    from search_models import Candidate, SearchError
    from test_release_search import response_for

    payload = response_for("invented:999")
    response = {"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]}
    projected = project_generated(response)
    assert isinstance(projected, dict)
    assert projected["selected_candidate_id"] == "_unexpected_string"
    with pytest.raises(SearchError, match="invented/repeated/ineligible IDs"):
        validate_recommendation(
            projected, [Candidate("fixture:1", "https://example.invalid", "Synthetic")], SearchPolicy()
        )


def test_independent_cli_reports_provider_failure_without_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    import json

    from check_gemini_wire import main
    from test_release_search import Network

    monkeypatch.setenv("GEMINI_API_KEY", "fixture-not-secret")
    config = tmp_path / "policy.toml"
    config.write_text('[search]\ngemini_model="configured-model"\nremote_metadata_disclosure=true\n')
    args = ["--config", str(config), "--report-dir", str(tmp_path / "reports")]
    net = Network(
        monkeypatch,
        lambda *args: (
            503,
            {"error": {"status": "UNAVAILABLE", "message": "Model is overloaded; capacity unavailable."}},
        ),
    )
    assert main([*args, "--prepare-only"]) == 0
    assert not net.calls
    capsys.readouterr()
    assert main(args) == 1
    assert len(net.calls) == 1
    terminal = json.loads(capsys.readouterr().out)
    assert terminal["http_status"] == 503
    assert "error" not in terminal
    report = json.loads((tmp_path / "reports" / "call-1.json").read_text())
    assert "capacity unavailable" in report["error"]["message"]
