"""Mastering-first regressions: real validators/workflow, synthetic source evidence."""

from __future__ import annotations

import csv
import io
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pipeline_config import SearchPolicy
from release_search import workflow
from search_evaluator import GeminiEvaluator, OpenAIResponsesEvaluator
from search_mastering import select_rip, validate_decision, validate_extraction
from search_models import Candidate, Query, SearchError
from search_opinions import reported_stance
from search_research import FEATURES, ForumResearch, Research, parse_forum
from test_release_search import Network
from test_search_stages import Discovery

Q = Query("", "Example Artist", "Example Album", "album")
URL = "https://forums.stevehoffman.tv/threads/example-album.100/"
A = "EMI A-123 CD matrix X1"
B = "EMI B-456 CD matrix X2"


def evidence(text: str = "") -> Research:
    body = text or f"I prefer {A} over {B}: smoother EQ. Compared directly."
    raw = f'<title>Example Artist Example Album mastering</title><article class="message" id="post-1" data-author="A"><div class="message-body">{body}</div></article>'.encode()
    return Research("FOUND", parse_forum(raw, URL, query=Q))


def extraction(r: Research) -> dict[str, Any]:
    sid = r.sources[0]["source_id"]
    return {
        "editions": [
            {
                "edition_id": "a",
                "descriptor": desc,
                "features": {
                    k: {"label": "EMI", "catalog_number": cat, "medium": "CD", "matrix": matrix}.get(k, "")
                    for k in FEATURES
                },
                "source_ids": [sid],
            }
            for desc, cat, matrix in [(A, "A-123", "X1")]
        ]
        + [
            {
                "edition_id": "b",
                "descriptor": B,
                "features": {
                    k: {"label": "EMI", "catalog_number": "B-456", "medium": "CD", "matrix": "X2"}.get(k, "")
                    for k in FEATURES
                },
                "source_ids": [sid],
            }
        ],
        "opinions": [
            {
                "opinion_id": "o1",
                "source_id": sid,
                "edition_id": "a",
                "compared_edition_id": "b",
                "stance": "prefer",
                "quote": r.sources[0]["album_context"],
                "comparison_type": "direct_reported",
                "characteristics": ["smoother EQ"],
            }
        ],
        "equivalences": [],
    }


def decision() -> dict[str, Any]:
    return {
        "status": "preference",
        "preferred_edition_id": "a",
        "support_opinion_ids": ["o1"],
        "dissent_opinion_ids": [],
        "rationale": "Reported smoother EQ in an explicit comparison.",
        "limitations": ["subjective_reviews"],
    }


def candidates() -> list[Candidate]:
    rows = [
        Candidate(
            "rutracker:1",
            "https://rutracker.org/forum/viewtopic.php?t=1",
            "Example Artist - Example Album",
            artist="Example Artist",
            release_title="Example Album",
            catalog_number="A-123",
            medium="CD",
            matrix="X1",
            seeders=1,
        ),
        Candidate(
            "rutracker:2",
            "https://rutracker.org/forum/viewtopic.php?t=2",
            "Example Artist - Example Album",
            artist="Example Artist",
            release_title="Example Album",
            catalog_number="B-456",
            medium="CD",
            matrix="X2",
            seeders=100,
        ),
    ]
    for c in rows:
        for key in ("artist", "release_title", "catalog_number", "medium", "matrix", "seeders"):
            c.claim(key, getattr(c, key), "topic_detail", str(getattr(c, key)))
    return rows


@pytest.mark.parametrize(
    "quote",
    [
        f"I do not prefer {A}.",
        f"I don't prefer {A}.",
        f"I never prefer {A}.",
        f"I prefer {B} over {A}.",
        f"{B} sounds better than {A}.",
        f"Someone prefers {A}.",
        f"I might prefer {A}.",
        f"I prefer {A} less than {B}.",
        f"I cannot recommend {A}.",
        f"I prefer {B} to {A}.",
        f"I compared {A} and {B} and I prefer {B}.",
        f"{B} is better, {A} is worse.",
        f"I wonder whether {A} is better.",
    ],
)
def test_negative_losing_hearsay_and_conditional_preferences(quote: str) -> None:
    assert not reported_stance(quote, A, "prefer")
    r = evidence(quote + " " + B)
    x = extraction(r)
    x["opinions"][0].update(quote=quote, comparison_type="not_established", compared_edition_id="", characteristics=[])
    with pytest.raises(SearchError):
        validate_extraction(x, r, Q)


def test_edition_identifiers_cannot_be_borrowed_from_competitor() -> None:
    r = evidence()
    x = extraction(r)
    x["editions"][0]["features"]["matrix"] = "X2"
    with pytest.raises(SearchError):
        validate_extraction(x, r, Q)


def test_full_context_and_quoted_reply_are_separate() -> None:
    text = " ".join(["A complete listening comparison with context."] * 70)
    raw = f'<title>Example Artist Example Album</title><article class="message" id="post-1"><div class="message-body"><blockquote><a href="#post-9">Listener said</a>I prefer {A}.</blockquote>{text}</div></article>'.encode()
    s = parse_forum(raw, URL, query=Q)[0]
    assert len(s["album_context"].split()) > 300
    assert s["album_context_complete"] is True
    assert "I prefer" not in s["album_context"]
    assert s["quoted_context"][0]["url"].endswith("#post-9")
    assert "I prefer" in s["quoted_context"][0]["text"]


def test_quotes_of_other_users_cannot_be_own_preference() -> None:
    r = evidence()
    r.sources[0]["quoted_context"] = [{"text": f"I prefer {B}.", "url": "", "attribution": "Other"}]
    x = extraction(r)
    x["opinions"][0]["quote"] = f"I prefer {B}."
    with pytest.raises(SearchError):
        validate_extraction(x, r, Q)


def test_mastering_winner_survives_seeders_and_absence() -> None:
    r = evidence()
    g = validate_extraction(extraction(r), r, Q)
    rows = candidates()
    result = validate_decision(decision(), g, r, rows)
    assert select_rip(result, rows, SearchPolicy())[0].candidate_id == "rutracker:1"
    absent = validate_decision(decision(), g, r, rows[1:])
    assert select_rip(absent, rows[1:], SearchPolicy()) == []
    assert absent["preferred_edition"] == A and absent["mapping_status"] == "not_found"
    assert absent["recommended_candidate_id"] == ""


def test_dissent_cannot_be_omitted() -> None:
    r = evidence()
    text = f"I prefer {B} over {A}."
    other = evidence(text).sources[0]
    other["url"] = URL + "#post-2"
    other["source_id"] = "second-source"
    r.sources.append(other)
    x = extraction(r)
    x["opinions"].append(
        {
            **x["opinions"][0],
            "opinion_id": "o2",
            "source_id": "second-source",
            "edition_id": "b",
            "compared_edition_id": "a",
            "quote": text,
            "comparison_type": "not_established",
            "characteristics": [],
        }
    )
    g = validate_extraction(x, r, Q)
    with pytest.raises(SearchError):
        validate_decision(decision(), g, r, candidates())
    d = {**decision(), "dissent_opinion_ids": ["o2"]}
    assert "dissent" in validate_decision(d, g, r, candidates())["limitations"]


def test_same_mastering_requires_explicit_affirmative_source() -> None:
    phrase = f"{A} and {B} have the same mastering."
    r = evidence(f"I prefer {A} over {B}: smoother EQ. Compared directly. " + phrase)
    x = extraction(r)
    x["equivalences"] = [{"left": "a", "right": "b", "source_id": r.sources[0]["source_id"], "quote": phrase}]
    graph = validate_extraction(x, r, Q)
    assert graph["editions"][0]["mastering_group_id"] == graph["editions"][1]["mastering_group_id"]
    review = validate_decision(decision(), graph, r, candidates())
    assert select_rip(review, candidates(), SearchPolicy())[0].candidate_id == "rutracker:2"
    x["equivalences"][0]["quote"] = phrase.replace("same", "probably the same")
    with pytest.raises(SearchError):
        validate_extraction(x, r, Q)


def test_ambiguous_catalogue_cannot_select_a_mastering() -> None:
    other = A.replace("X1", "X2")
    r = evidence(f"I prefer {A} over {other}: smoother EQ. Compared directly.")
    x = extraction(evidence())
    for e in x["editions"]:
        e["source_ids"] = [r.sources[0]["source_id"]]
    x["editions"][1].update(descriptor=other, features={**x["editions"][0]["features"], "matrix": "X2"})
    x["opinions"][0].update(source_id=r.sources[0]["source_id"], quote=r.sources[0]["album_context"])
    graph = validate_extraction(x, r, Q)
    c = candidates()[0]
    c.matrix = None
    review = validate_decision(decision(), graph, r, [c])
    assert select_rip(review, [c], SearchPolicy()) == []
    assert review["mapping_status"] == "ambiguous_or_missing_identifiers"


def test_real_new_workflow_calls_only_extraction_and_decision(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-key")
    r = evidence()
    responses = iter([extraction(r), decision()])
    net = Network(
        monkeypatch,
        lambda *args: (
            200,
            {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(next(responses))}]}}]},
        ),
    )
    p = replace(
        SearchPolicy(), backend="gemini", gemini_model="fixture", remote_metadata_disclosure=True, details=False
    )
    adapter = Discovery()
    adapter.rows = candidates()

    class R(ForumResearch):
        def collect(self, *args: Any) -> Research:
            return r

    out = io.StringIO()
    assert (
        workflow(Q, p, adapter, tmp_path / "result.csv", researcher=R(p), input_stream=io.StringIO(), output=out) == 0
    )
    rows = list(csv.DictReader((tmp_path / "result.csv").open()))
    result = json.loads(rows[0]["mastering_decision"])
    assert result["candidate_id"] == "rutracker:1"
    assert result["alternative_practical_candidate_id"] == "rutracker:2"
    assert rows[0]["mastering_rank"] == "1" and rows[1]["practical_rank"] == "1"
    assert len(net.calls) == 2
    assert all("candidates" not in json.loads(call[2]["json"]["contents"][0]["parts"][0]["text"]) for call in net.calls)
    assert "Раздачи рекомендуемого мастеринга" in out.getvalue()


def test_snippets_abstain_without_spending_a_model_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-key")
    r = evidence()
    r.sources[0]["page_status"] = "SNIPPET_ONLY"
    net = Network(monkeypatch, lambda *args: pytest.fail("No model call expected"))
    p = replace(SearchPolicy(), gemini_model="fixture", remote_metadata_disclosure=True)
    result = GeminiEvaluator(p).evaluate_mastering(Q, candidates(), r)
    assert result.mastering_review and result.mastering_review["status"] == "insufficient_evidence"
    assert result.selected_candidate_id == "" and net.calls == []


def test_responses_wire_and_refusal_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    responses = iter([extraction(evidence()), decision()])
    net = Network(
        monkeypatch,
        lambda *args: (
            200,
            {
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": json.dumps(next(responses))}]}
                ],
            },
        ),
    )
    p = replace(
        SearchPolicy(),
        backend="openai",
        openai_api="responses",
        openai_model="gpt-6-astra",
        structured_mode="schema",
        remote_metadata_disclosure=True,
    )
    result = OpenAIResponsesEvaluator(p).evaluate_mastering(Q, candidates(), evidence())
    assert result.selected_candidate_id == "rutracker:1"
    assert all(call[1].endswith("/responses") and call[2]["json"]["store"] is False for call in net.calls)
    assert net.calls[0][2]["json"]["text"]["format"]["strict"] is True
    with pytest.raises(ValueError):
        OpenAIResponsesEvaluator(p).response_text({"status": "incomplete", "output": []})


def test_adaptive_research_discovers_editions_and_follows_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERPER_API_KEY", "fixture-key")
    first = b'<title>Example Artist Example Album mastering</title><article class="message" id="post-1"><div class="message-body">I prefer MFSL UDCD 123 CD.</div></article><a rel="next" href="page-2">Next</a>'
    second = b'<title>Example Artist Example Album mastering</title><article class="message" id="post-2"><div class="message-body">I prefer the original EMI CD.</div></article>'
    net = Network(
        monkeypatch,
        lambda m, u, k: (
            (200, {"organic": [{"link": URL, "title": "Example Artist Example Album mastering", "snippet": ""}]})
            if "serper" in u
            else (200, second if "page-2" in u else first)
        ),
    )
    p = replace(
        SearchPolicy(),
        research_backend="serper",
        research_key_env="SERPER_API_KEY",
        research_disclosure=True,
        research_queries=5,
        research_pages=4,
    )
    result = ForumResearch(p).collect(Q, [])
    searches = [call[2]["json"]["q"] for call in net.calls if "serper" in call[1]]
    assert any('"UDCD 123"' in q for q in searches)
    assert any('"MFSL"' in q for q in searches)
    assert any("disagree" in q for q in searches)
    assert len([call for call in net.calls if "forums" in call[1]]) == 2
    assert len(searches) == 5
    assert any(s["post_id"] == "post-2" for s in result.sources)


def test_invalid_opinion_does_not_erase_independent_valid_opinion() -> None:
    from search_mastering import validate_extraction_items

    r = evidence()
    x = extraction(r)
    x["opinions"].append({**x["opinions"][0], "opinion_id": "bad", "quote": "Invented preference"})
    graph = validate_extraction_items(x, r, Q)
    assert [o["opinion_id"] for o in graph["opinions"]] == ["o1"]
    assert graph["rejected_items"] == {"OPINION_SPAN": 1}
    assert validate_decision(decision(), graph, r, candidates())["status"] == "preference"


def test_duplicate_ids_are_all_rejected_without_redefinition() -> None:
    from search_mastering import validate_extraction_items

    r = evidence()
    x = extraction(r)
    x["editions"].append(deepcopy(x["editions"][0]))
    graph = validate_extraction_items(x, r, Q)
    assert all(e["edition_id"] != "a" for e in graph["editions"])
    assert not graph["opinions"] and graph["rejected_items"]["DUPLICATE_ID"] == 2


def test_free_text_research_work_requires_agreement() -> None:
    from search_models import resolve_research_query

    q = Query("Example Artist Example Album")
    resolved = resolve_research_query(q, candidates())
    assert resolved.artist == Q.artist and resolved.title == Q.title
    conflict = candidates()
    conflict[0].artist = "Other Artist"
    assert resolve_research_query(Query("Album"), conflict).artist == ""


def test_pagination_never_walks_backwards_or_to_another_thread() -> None:
    from search_research import forum_followups

    raw = b'<div class="PageNav"><a href="page-2">2</a><a href="page-4">4</a></div><a rel="next" href="/threads/other.200/page-9">next</a>'
    assert forum_followups(raw, URL + "page-3", ("forums.stevehoffman.tv",)) == [URL + "page-4"]


def test_identifiers_in_a_single_edition_context_have_explicit_provenance() -> None:
    r = evidence("EMI original edition. Catalogue A-123 CD matrix X1. I prefer this pressing.")
    sid = r.sources[0]["source_id"]
    e = extraction(evidence())["editions"][0]
    e.update(
        descriptor="EMI original edition",
        source_ids=[sid],
        feature_evidence=[
            {"field": k, "value": v, "source_id": sid, "quote": "Catalogue A-123 CD matrix X1."}
            for k, v in e["features"].items()
            if v and k != "label"
        ],
    )
    x = {"editions": [e], "opinions": [], "equivalences": []}
    assert validate_extraction(x, r, Q)["editions"][0]["features"]["catalog_number"] == "A-123"


def test_feature_provenance_cannot_transfer_a_competing_catalogue() -> None:
    from search_mastering import validate_extraction_items

    r = evidence()
    x = extraction(r)
    x["editions"][0]["features"]["catalog_number"] = "B-456"
    x["editions"][0]["feature_evidence"] = [
        {
            "field": "catalog_number",
            "value": "B-456",
            "source_id": r.sources[0]["source_id"],
            "quote": r.sources[0]["album_context"],
        }
    ]
    with pytest.raises(SearchError):
        validate_extraction(x, r, Q)
    graph = validate_extraction_items(x, r, Q)
    retained = next(e for e in graph["editions"] if e["edition_id"] == "a")
    assert retained["features"]["catalog_number"] == ""
    assert graph["rejected_items"]["UNSUPPORTED_FEATURE_CATALOG_NUMBER"] == 1


def test_openai_discovery_accepts_only_tool_citations_and_discloses_no_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from search_openai_research import discover_urls

    monkeypatch.setenv("OPENAI_API_KEY", "fixture-secret-openai")
    monkeypatch.setenv("RUTRACKER_COOKIE", "sid=fixture-secret-cookie")
    response = {
        "status": "completed",
        "output": [
            {
                "type": "web_search_call",
                "action": {"sources": [{"url": URL}, {"url": "https://rutracker.org/forum/viewtopic.php?t=1"}]},
            },
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "https://forums.stevehoffman.tv/threads/invented.9/",
                        "annotations": [
                            {"type": "url_citation", "url": URL + "#post-1"},
                            {"type": "url_citation", "url": "https://evil.test/"},
                        ],
                    }
                ],
            },
        ],
    }
    net = Network(monkeypatch, lambda *args: (200, response))
    p = replace(
        SearchPolicy(), research_openai_model="gpt-6-astra", research_disclosure=True, remote_metadata_disclosure=True
    )
    urls, diag = discover_urls(p, Q)
    assert urls == [URL, URL + "#post-1"] and diag["url_count"] == 2
    body = net.calls[0][2]["json"]
    assert body["store"] is False and body["max_tool_calls"] == 4
    assert body["tools"][0]["filters"]["allowed_domains"] == ["forums.stevehoffman.tv"]
    assert "fixture-secret" not in json.dumps(body) and "rutracker" not in json.dumps(body).casefold()


def test_disabling_research_also_disables_optional_openai_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    net = Network(monkeypatch, lambda *args: pytest.fail("Research is disabled"))
    p = replace(
        SearchPolicy(),
        research_openai_discovery=True,
        research_openai_model="gpt-6-astra",
        research_disclosure=True,
        remote_metadata_disclosure=True,
    )
    assert ForumResearch(p).collect(Q, candidates()).status == "NOT_CONFIGURED"
    assert not net.calls


def test_json_stage_mode_keeps_strict_local_validation_without_schema_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-key")
    r = evidence("I own a CD, without a listening preference.")
    net = Network(
        monkeypatch,
        lambda *args: (
            200,
            {
                "candidates": [
                    {"content": {"parts": [{"text": json.dumps({"editions": [], "opinions": [], "equivalences": []})}]}}
                ]
            },
        ),
    )
    p = replace(SearchPolicy(), gemini_model="fixture", remote_metadata_disclosure=True, structured_mode="json")
    result = GeminiEvaluator(p).evaluate_mastering(Q, candidates(), r)
    assert result.mastering_review and result.mastering_review["status"] == "insufficient_evidence"
    config = net.calls[0][2]["json"]["generationConfig"]
    assert "responseJsonSchema" not in config and config["responseMimeType"] == "application/json"
    assert len(net.calls) == 1


def test_large_reply_context_drops_whole_sources_with_visible_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-key")
    r = evidence()
    template = r.sources[0]
    r.sources = [
        {
            **template,
            "source_id": f"source-{i}",
            "url": URL + f"#post-{i}",
            "quoted_context": [{"text": "q" * 4000, "url": "", "attribution": ""} for _ in range(10)],
        }
        for i in range(20)
    ]
    net = Network(
        monkeypatch,
        lambda *args: (
            200,
            {
                "candidates": [
                    {"content": {"parts": [{"text": json.dumps({"editions": [], "opinions": [], "equivalences": []})}]}}
                ]
            },
        ),
    )
    p = replace(SearchPolicy(), gemini_model="fixture", remote_metadata_disclosure=True)
    result = GeminiEvaluator(p).evaluate_mastering(Q, candidates(), r)
    sent = net.calls[0][2]["json"]["contents"][0]["parts"][0]["text"]
    assert len(sent.encode()) < 200000
    data = json.loads(sent)
    assert 0 < len(data["sources"]) < 20
    assert all(s["album_context"] == template["album_context"] for s in data["sources"])
    assert result.mastering_review and "limited_sources" in result.mastering_review["limitations"]


def test_malformed_openai_discovery_is_a_controlled_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from search_openai_research import discover_urls

    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    Network(monkeypatch, lambda *args: (200, {"status": "completed", "output": "bad"}))
    p = replace(
        SearchPolicy(), research_openai_model="gpt-6-astra", research_disclosure=True, remote_metadata_disclosure=True
    )
    with pytest.raises(SearchError, match="INVALID_RESPONSE"):
        discover_urls(p, Q)
