"""Real batch validation, wire calls, failure isolation, dissent and source accounting."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from mastering_benchmark import BudgetHTTP
from pipeline_config import SearchPolicy
from release_search import validate_policy
from search_evaluator import GeminiEvaluator
from search_mastering import validate_extraction_items
from search_mastering_batches import merge_graphs
from search_models import SearchError
from search_research import Research
from test_release_search import Network
from test_search_mastering import A, B, Q, candidates, evidence, extraction


def many_sources(count: int) -> Research:
    template = evidence().sources[0]
    return Research(
        "FOUND",
        [
            {
                **deepcopy(template),
                "source_id": f"s{i}",
                "url": template["url"].split("#")[0] + f"#post-{i}",
                "author": f"Listener{i}",
            }
            for i in range(count)
        ],
    )


def batch_payload(sources: list[dict[str, Any]]) -> dict[str, Any]:
    payload = extraction(Research("FOUND", sources[:1]))
    for edition in payload["editions"]:
        edition["source_ids"] = [s["source_id"] for s in sources]
    payload["opinions"] = [
        {
            **payload["opinions"][0],
            "opinion_id": f"o{i}",
            "source_id": s["source_id"],
            "quote": s["album_context"],
            "edition_id": "b" if f"I prefer {B}" in s["album_context"] else "a",
            "compared_edition_id": "a" if f"I prefer {B}" in s["album_context"] else "b",
        }
        for i, s in enumerate(sources)
    ]
    return payload


def wire_reply(value: object) -> tuple[int, dict[str, Any]]:
    return 200, {"candidates": [{"content": {"parts": [{"text": json.dumps(value)}]}}]}


def choose_a(data: dict[str, Any]) -> dict[str, Any]:
    edition = next(e for e in data["graph"]["editions"] if e["descriptor"] == A)
    option = next(o for o in data["decision_options"] if o["edition_id"] == edition["edition_id"])
    return {
        "status": "preference",
        "preferred_edition_id": edition["edition_id"],
        "support_opinion_ids": option["support_opinion_ids"],
        "dissent_opinion_ids": option["dissent_opinion_ids"],
        "rationale": "Личное предпочтение; сохранены все извлечённые разногласия.",
        "limitations": ["limited_sources"],
    }


def request_data(kwargs: dict[str, Any]) -> dict[str, Any]:
    return json.loads(kwargs["json"]["contents"][0]["parts"][0]["text"])


def policy(**kwargs: Any) -> SearchPolicy:
    # These tests isolate model batching/failure handling. Literal recovery has
    # separate enabled integration and adversarial coverage.
    return replace(
        SearchPolicy(),
        gemini_model="fixture",
        remote_metadata_disclosure=True,
        **{"mastering_literal_recovery": False, **kwargs},
    )


def test_small_batches_preserve_colliding_model_ids_and_all_dissent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-key")
    r = many_sources(20)
    r.sources[-1]["album_context"] = f"I prefer {B} over {A}: smoother EQ. Compared directly."
    net = Network(
        monkeypatch,
        lambda method, url, kw: wire_reply(
            batch_payload(request_data(kw)["sources"]) if "sources" in request_data(kw) else choose_a(request_data(kw))
        ),
    )
    result = GeminiEvaluator(policy()).evaluate_mastering(Q, candidates(), r)
    review = result.mastering_review
    assert review and review["status"] == "preference"
    graph = review["evidence_graph"]
    assert len(graph["editions"]) == 2 and len(graph["opinions"]) == 20
    assert len({o["opinion_id"] for o in graph["opinions"]}) == 20
    assert len(review["support_opinion_ids"]) == 19 and len(review["dissent_opinion_ids"]) == 1
    assert review["distinct_accounts"] == 19
    assert [len(request_data(call[2])["sources"]) for call in net.calls[:-1]] == [8, 8, 4]
    assert len(net.calls) == 4 and review["extraction_coverage"]["source_status_counts"] == {"accepted": 20}


@pytest.mark.parametrize("failure", ["json", "contract", "server"])
def test_bad_middle_batch_preserves_other_reviews_without_retry(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-key")

    def respond(method: str, url: str, kw: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        data = request_data(kw)
        if "sources" not in data:
            return wire_reply(choose_a(data))
        if data["sources"][0]["source_id"] == "s1":
            if failure == "server":
                return 503, {"error": {"status": "UNAVAILABLE"}}
            if failure == "json":
                return 200, {"candidates": [{"content": {"parts": [{"text": "invalid JSON"}]}}]}
            return wire_reply({"wrong": "object"})
        return wire_reply(batch_payload(data["sources"]))

    net = Network(monkeypatch, respond)
    result = GeminiEvaluator(policy(mastering_batch_size=1)).evaluate_mastering(Q, candidates(), many_sources(3))
    review = result.mastering_review
    assert review and review["status"] == "preference"
    assert {o["source_id"] for o in review["evidence_graph"]["opinions"]} == {"s0", "s2"}
    assert review["extraction_coverage"]["source_status_counts"] == {"accepted": 2, "request_failed": 1}
    assert not review["extraction_coverage"]["all_usable_sources_represented"]
    assert len(net.calls) == 4 and "limited_sources" in review["limitations"]


def test_quota_error_stops_remaining_calls_but_keeps_the_validated_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-key")
    replies = iter(
        [wire_reply(batch_payload(many_sources(1).sources)), (429, {"error": {"status": "RESOURCE_EXHAUSTED"}})]
    )
    net = Network(monkeypatch, lambda *args: next(replies))
    review = (
        GeminiEvaluator(policy(mastering_batch_size=1))
        .evaluate_mastering(Q, candidates(), many_sources(3))
        .mastering_review
    )
    assert review and review["status"] == "insufficient_evidence"
    assert len(review["evidence_graph"]["opinions"]) == 1 and review["candidate_id"] == ""
    assert review["extraction_coverage"]["source_status_counts"] == {
        "accepted": 1,
        "request_failed": 1,
        "not_attempted": 1,
    }
    assert len(net.calls) == 2


def test_budget_stops_before_network_and_preserves_already_accepted_reviews(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-key")
    net = Network(monkeypatch, lambda *args: wire_reply(batch_payload(many_sources(1).sources)))
    p = policy(mastering_batch_size=1)
    http = BudgetHTTP(p, 1, tmp_path / "counter")
    review = GeminiEvaluator(p, http).evaluate_mastering(Q, candidates(), many_sources(3)).mastering_review
    assert review and len(review["evidence_graph"]["opinions"]) == 1
    assert review["status"] == "insufficient_evidence" and len(net.calls) == http.used == 1
    assert review["extraction_coverage"]["source_status_counts"]["not_attempted"] == 1


def test_three_service_failures_stop_within_one_album(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-key")
    net = Network(monkeypatch, lambda *args: (503, {"error": {"status": "UNAVAILABLE"}}))
    trace: dict[str, Any] = {}
    with pytest.raises(SearchError):
        GeminiEvaluator(policy(mastering_batch_size=1)).evaluate_mastering(
            Q, candidates(), many_sources(6), trace=lambda name, value: trace.update({name: value})
        )
    assert len(net.calls) == 3
    assert trace["coverage"]["source_status_counts"] == {"request_failed": 3, "not_attempted": 3}


def test_source_accounting_distinguishes_missing_from_rejected_opinions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-key")

    def respond(method: str, url: str, kw: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        data = request_data(kw)
        if "sources" not in data:
            return wire_reply(choose_a(data))
        payload = batch_payload(data["sources"])
        payload["opinions"] = payload["opinions"][:2]
        payload["opinions"][1]["quote"] = "A fabricated opinion."
        return wire_reply(payload)

    Network(monkeypatch, respond)
    review = GeminiEvaluator(policy()).evaluate_mastering(Q, candidates(), many_sources(3)).mastering_review
    assert review and review["extraction_coverage"]["source_status_counts"] == {
        "accepted": 1,
        "opinion_rejected": 1,
        "no_opinion_proposed": 1,
    }
    rows = review["extraction_coverage"]["sources"]
    assert rows[1]["rejection_rules"] == ["OPINION_SPAN"] and rows[2]["rejected_opinions"] == 0


def test_batch_cannot_cite_a_source_from_another_batch_even_when_in_the_full_corpus() -> None:
    r = many_sources(2)
    graph = validate_extraction_items(batch_payload(r.sources[1:]), r, Q, allowed_source_ids={"s0"})
    assert graph["editions"] == graph["opinions"] == []


def test_external_thread_identity_anchor_can_certify_scope_but_cannot_supply_a_vote() -> None:
    r = many_sources(2)
    for s in r.sources:
        s["page_title"] = "Example Album mastering"
    r.sources[1].update(album_context="Example Artist - Example Album", full_text="Example Artist - Example Album")
    graph = validate_extraction_items(batch_payload(r.sources[:1]), r, Q, allowed_source_ids={"s0"})
    assert len(graph["opinions"]) == 1 and graph["opinions"][0]["thread_artist_evidence"][0]["source_id"] == "s1"


def test_conflicting_catalogues_with_the_same_descriptor_are_separate_mastering_groups() -> None:
    r = evidence("MFSL CD UDCD 545 is one disc. MFSL CD UDCD 568 is another. I prefer MFSL.")
    payload = extraction(r)
    for edition, cat in zip(payload["editions"], ["UDCD 545", "UDCD 568"], strict=True):
        edition.update(
            descriptor="MFSL",
            features={**dict.fromkeys(edition["features"], ""), "catalog_number": cat},
            feature_evidence=[
                {
                    "field": "catalog_number",
                    "value": cat,
                    "source_id": r.sources[0]["source_id"],
                    "quote": f"MFSL CD {cat}",
                }
            ],
        )
    payload["opinions"] = []
    graph = validate_extraction_items(payload, r, Q)
    assert len({e["mastering_group_id"] for e in graph["editions"]}) == 2
    merged = merge_graphs([("b001", graph), ("b002", graph)], r, Q)
    assert len(merged["editions"]) == 2 and len({e["mastering_group_id"] for e in merged["editions"]}) == 2


@pytest.mark.parametrize("size", [0, True, 101])
def test_invalid_batch_sizes_are_rejected_before_network(size: Any) -> None:
    with pytest.raises(SearchError, match="mastering_batch_size"):
        validate_policy(policy(mastering_batch_size=size))


@pytest.mark.parametrize("plural", [["s0"], ["s0", "other"], ["other"]])
def test_only_an_exactly_redundant_source_list_can_be_removed(plural: list[str]) -> None:
    r = many_sources(1)
    payload = batch_payload(r.sources)
    payload["opinions"][0]["source_ids"] = plural
    graph = validate_extraction_items(payload, r, Q)
    if plural == ["s0"]:
        assert len(graph["opinions"]) == 1
        assert "source_ids" not in graph["opinions"][0]
        assert graph["rejected_items"]["REDUNDANT_OPINION_SOURCE_IDS"] == 1
        payload["opinions"][0]["quote"] = "A fabricated opinion."
        assert validate_extraction_items(payload, r, Q)["opinions"] == []
    else:
        assert graph["opinions"] == []
