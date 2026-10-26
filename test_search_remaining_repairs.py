"""Known source losses, false edition matches and durable partial mastering results."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pipeline_config import SearchPolicy
from search_evaluator import GeminiEvaluator
from search_literal_recovery import recover_preferences
from search_mastering import validate_extraction_items
from search_models import Candidate, Query
from search_opinions import reported_stance
from search_providers import Jackett, parse_details
from search_research import Research, edition_match
from test_release_search import Network
from test_search_mastering import Q, candidates, evidence, extraction
from test_search_mastering_batches import choose_a, request_data, wire_reply

EMPTY: dict[str, Any] = {"editions": [], "opinions": [], "equivalences": []}
SOURCE_CASES = json.loads((Path(__file__).parent / "tests/fixtures/search/mastering/source_recovery.json").read_text())[
    "cases"
]


@pytest.mark.parametrize("case", SOURCE_CASES, ids=lambda r: r["source_id"])
def test_original_source_preferences_survive_an_empty_model_graph(case: dict[str, Any]) -> None:
    r = Research("FOUND", case["sources"])
    parts = recover_preferences(r, Query(**case["query"]), {case["source_id"]}, EMPTY)
    opinions = [o for _, g in parts for o in g["opinions"]]
    assert bool(opinions) == case["expected_preference"]
    for _, graph in parts:
        assert graph["equivalences"] == []
        for opinion in graph["opinions"]:
            assert opinion["quote"] in case["sources"][0]["album_context"]
            assert opinion["source_id"] == case["source_id"]
            assert opinion["extraction_method"] == "literal_rule"


@pytest.mark.parametrize(
    "text,loser,winner",
    [
        ("I prefer SHM-SACD over SACD.", "SACD", "SHM-SACD"),
        ("I prefer SACD over SHM-SACD.", "SHM-SACD", "SACD"),
        ("I prefer SHM SACD over SACD.", "SACD", "SHM SACD"),
        ("The SHM-SACD is a unique mastering to that, and even better.", "SACD", "SHM-SACD"),
        ("The SHM-SACD is unique, and MFSL is better.", "SHM-SACD", "MFSL"),
        ("I prefer Japan for U.S. over MFSL.", "MFSL", "Japan for U.S."),
    ],
)
def test_preference_belongs_to_the_literal_winning_edition(text: str, loser: str, winner: str) -> None:
    assert reported_stance(text, winner, "prefer")
    assert not reported_stance(text, loser, "prefer")


@pytest.mark.parametrize(
    "text",
    [
        "Which is better, MFSL or DCC?",
        "Alice said MFSL is best.",
        "They prefer MFSL.",
        "If I had to choose, I would prefer MFSL.",
        "I do not prefer MFSL.",
        "DCC sounds better than MFSL.",
        "I prefer DCC over MFSL.",
        "MFSL is the best value.",
        "MFSL is preferred by other people.",
        "MFSL is preferred by other listeners.",
        "MFSL is my least favorite.",
        "MFSL is the best according to Alice.",
        "There is a preference for MFSL in this poll.",
        "I prefer MFSL because it is cheaper.",
        "I prefer MFSL because it has a DR14 score.",
        "I prefer MFSL because it is 24-bit.",
    ],
)
def test_native_recovery_cannot_turn_an_unconfirmed_mfsl_choice_into_a_vote(text: str) -> None:
    parts = recover_preferences(evidence(text), Q, {evidence(text).sources[0]["source_id"]}, EMPTY)
    assert all(
        e["descriptor"] != "MFSL"
        for _, g in parts
        for e in g["editions"]
        if any(o["edition_id"] == e["edition_id"] for o in g["opinions"])
    )


def test_pronoun_comparison_requires_one_explicit_preceding_subject() -> None:
    text = "The BT is more relaxed than the 2009. It remains sonically superior at louder levels."
    assert reported_stance(text, "BT", "prefer")
    assert not reported_stance(text, "2009", "prefer")
    assert not reported_stance("I own BT and 2009. It is better.", "BT", "prefer")
    assert not reported_stance("The 2009 sounds warmer than BT. It is better.", "BT", "prefer")


def test_literal_recovery_respects_selection_and_never_reuses_another_authors_catalogue() -> None:
    r = evidence("I prefer MFSL.")
    original = r.sources[0]
    reference = {
        **original,
        "source_id": "reference",
        "album_context": "MFSL UDCD 545 CD",
        "full_text": "MFSL UDCD 545 CD",
    }
    r.sources.append(reference)
    payload = extraction(r)
    payload.update(opinions=[])
    payload["editions"] = [
        {
            "edition_id": "full",
            "descriptor": "MFSL UDCD 545 CD",
            "features": {
                **dict.fromkeys(payload["editions"][0]["features"], ""),
                "catalog_number": "UDCD 545",
                "medium": "CD",
            },
            "source_ids": ["reference"],
        }
    ]
    existing = validate_extraction_items(payload, r, Q)
    assert recover_preferences(r, Q, set(), existing) == []
    recovered = recover_preferences(r, Q, {original["source_id"]}, existing)
    assert recovered and recovered[0][1]["editions"][0]["features"]["catalog_number"] == ""
    assert recovered[0][1]["editions"][0]["descriptor"] == "MFSL"


def test_a_valid_model_preference_is_not_duplicated_by_rules() -> None:
    r = evidence()
    graph = validate_extraction_items(extraction(r), r, Q)
    assert recover_preferences(r, Q, {r.sources[0]["source_id"]}, graph) == []


def test_country_of_the_artist_cannot_conflict_with_pressing_identity() -> None:
    c = Candidate("rutracker:1", "https://rutracker.org/forum/viewtopic.php?t=1", "Queen - Jazz")
    parse_details(c, b'<div class="post_body">Country: UK<br>Catalog number: UDCD545<br>Source medium: CD</div>')
    match = edition_match({"catalog_number": "UDCD-545", "medium": "CD", "country": "Japan"}, c)
    assert match["confidence"] == "possible" and "country" in match["missing"] and "country" not in match["conflicting"]


def test_explicit_pressing_country_has_priority_over_unspecified_country() -> None:
    c = Candidate("rutracker:1", "https://rutracker.org/forum/viewtopic.php?t=1", "Queen - Jazz")
    parse_details(
        c,
        b'<div class="post_body">Country: UK<br>Pressing country: Japan<br>Catalog number: UDCD545<br>Source medium: CD</div>',
    )
    assert c.country == "Japan" and c.evidence["country"]["role"] == "edition"
    assert any(x["field"] == "country_unspecified" and x["value"] == "UK" for x in c.claims)
    assert "country" in edition_match({"country": "UK"}, c)["conflicting"]


@pytest.mark.parametrize("catalogue", ["UDCD 0545", "UDCD 568", "545"])
def test_catalogue_typography_matching_preserves_numbers_and_prefixes(catalogue: str) -> None:
    c = Candidate("x", "", "", catalog_number=catalogue, medium="CD")
    assert "catalog_number" in edition_match({"catalog_number": "UDCD 545", "medium": "CD"}, c)["conflicting"]


@pytest.mark.parametrize(
    "code,description,expected",
    [
        (
            "900",
            "Error connecting to FlareSolverr server: Connection refused (localhost:8191). Cookie: fixture-key",
            "DEPENDENCY_UNAVAILABLE",
        ),
        ("100", "Invalid API key: fixture-key", "AUTHENTICATION_ERROR"),
        ("900", "Your cookie did not work. bb_session=fixture-key", "AUTHENTICATION_ERROR"),
    ],
)
def test_indexer_xml_errors_report_the_cause_without_reflecting_credentials(
    monkeypatch: pytest.MonkeyPatch, code: str, description: str, expected: str
) -> None:
    from search_models import SearchError

    monkeypatch.setenv("JACKETT_API_KEY", "fixture-key")
    raw = f'<error code="{code}" description="{description}"/>'.encode()
    Network(monkeypatch, lambda *args: (400, raw, {"Content-Type": "application/xml"}))
    indexer = Jackett(replace(SearchPolicy(), jackett_url="http://jackett.test"))
    with pytest.raises(SearchError, match=expected) as error:
        indexer.search(Q, 10, 10, 1)
    assert "fixture-key" not in str(error.value) and "bb_session" not in str(error.value)


def test_empty_model_extraction_does_not_erase_an_explicit_own_preference(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-key")
    Network(
        monkeypatch,
        lambda method, url, kw: wire_reply(EMPTY if "sources" in request_data(kw) else choose_a(request_data(kw))),
    )
    p = replace(SearchPolicy(), gemini_model="fixture", remote_metadata_disclosure=True)
    review = GeminiEvaluator(p).evaluate_mastering(Q, candidates(), evidence()).mastering_review
    assert review and review["status"] == "preference"
    assert review["extraction_coverage"]["literal_opinion_count"] >= 1
    assert all(o["quote"] in evidence().sources[0]["album_context"] for o in review["evidence_graph"]["opinions"])


@pytest.mark.parametrize("failure", ["quota", "invalid_json", "invalid_decision"])
def test_decision_failure_retains_source_evidence_and_abstains_without_retry(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-key")

    def respond(method: str, url: str, kw: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        data = request_data(kw)
        if "sources" in data:
            return wire_reply(extraction(evidence()))
        if failure == "quota":
            return 429, {"error": {"status": "RESOURCE_EXHAUSTED"}}
        if failure == "invalid_json":
            return 200, {"candidates": [{"content": {"parts": [{"text": "bad json"}]}}]}
        return wire_reply({**choose_a(data), "preferred_edition_id": "invented"})

    net = Network(monkeypatch, respond)
    p = replace(SearchPolicy(), gemini_model="fixture", remote_metadata_disclosure=True)
    trace: dict[str, Any] = {}
    review = (
        GeminiEvaluator(p)
        .evaluate_mastering(Q, candidates(), evidence(), trace=lambda name, value: trace.update({name: value}))
        .mastering_review
    )
    assert review and review["decision_status"] == "failed"
    assert review["status"] == "insufficient_evidence" and review["candidate_id"] == ""
    assert len(review["evidence_graph"]["opinions"]) == 1
    assert "decision_failure" in trace and "fallback_decision" in trace
    assert len(net.calls) == 2
