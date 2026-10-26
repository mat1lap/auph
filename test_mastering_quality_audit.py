"""Regression cases from saved evidence, using synthetic text and real validators."""

from __future__ import annotations

import pytest

from search_evidence_quality import source_relevance
from search_mastering import decision_options, validate_decision, validate_extraction, validate_extraction_items
from search_models import Query, SearchError
from search_opinions import reported_stance, reports_direct_comparison
from search_research import post_excerpt, refresh_source_context
from test_search_mastering import URL, A, B, Q, candidates, decision, evidence, extraction


def test_unsupported_known_edition_citation_does_not_erase_valid_identity() -> None:
    r = evidence()
    unrelated = {
        **r.sources[0],
        "source_id": "forum:other",
        "album_context": f"I prefer {B}.",
        "full_text": f"I prefer {B}.",
    }
    r.sources.append(unrelated)
    payload = extraction(r)
    payload["editions"][0]["source_ids"].append("forum:other")
    graph = validate_extraction_items(payload, r, Q)
    assert graph["editions"][0]["source_ids"] == [r.sources[0]["source_id"]]
    assert graph["opinions"][0]["stance"] == "prefer"
    assert graph["rejected_items"]["UNSUPPORTED_EDITION_SOURCE"] == 1
    payload["editions"][0]["source_ids"].append("invented")
    graph = validate_extraction_items(payload, r, Q)
    assert all(e["edition_id"] != "a" for e in graph["editions"])


@pytest.mark.parametrize("predicate", ["edges out", "beats"])
def test_narrative_winner_does_not_attribute_preference_to_loser(predicate: str) -> None:
    assert reported_stance(f"{A} {predicate} {B}.", A, "prefer")
    assert not reported_stance(f"{A} {predicate} {B}.", B, "prefer")
    assert not reported_stance(f"{A} does not {predicate} {B}.", A, "prefer")
    assert reported_stance(f"{B} is no good.", B, "against")


def test_against_an_alternative_is_not_dissent_about_the_winner() -> None:
    r = evidence(f"I prefer {A} over {B}. {B} is worse.")
    payload = extraction(r)
    payload["opinions"][0].update(
        quote=f"I prefer {A} over {B}.", characteristics=[], comparison_type="not_established"
    )
    payload["opinions"].append(
        {
            **payload["opinions"][0],
            "opinion_id": "against_b",
            "edition_id": "b",
            "compared_edition_id": "",
            "stance": "against",
            "quote": f"{B} is worse.",
        }
    )
    graph = validate_extraction_items(payload, r, Q)
    assert decision_options(graph) == [{"edition_id": "a", "support_opinion_ids": ["o1"], "dissent_opinion_ids": []}]
    assert validate_decision(decision(), graph, r, candidates())["status"] == "preference"


def test_multichannel_opinion_does_not_map_a_stereo_rip_by_catalogue() -> None:
    r = evidence(f"These differences concern the MC mix. I prefer {A} over {B}: smoother EQ. Compared directly.")
    graph = validate_extraction_items(extraction(r), r, Q)
    cs = candidates()
    cs[0].channels = "2.0"
    review = validate_decision(decision(), graph, r, cs)
    assert review["listening_scope"] == "multichannel_or_mixed"
    assert review["candidate_matches"][cs[0].candidate_id]["confidence"] == "unestablished"
    assert "listening_channel_scope" in review["candidate_matches"][cs[0].candidate_id]["missing"]
    assert "limited_sources" in review["limitations"]


@pytest.mark.parametrize(
    "descriptor,medium,proof,expected",
    [
        ("2001 Japan mini vinyl remasters", "vinyl", False, ""),
        ("2001 Japan mini vinyl remasters", "vinyl", True, ""),
        ("Japan mini-LP edition", "LP", True, ""),
        ("Japan mini-LP CD edition", "CD", False, "CD"),
        ("UK vinyl edition", "vinyl", False, "vinyl"),
    ],
)
def test_packaging_word_does_not_establish_playback_medium(
    descriptor: str, medium: str, proof: bool, expected: str
) -> None:
    r = evidence(f"I like {descriptor}.")
    payload = extraction(r)
    edition = payload["editions"][0]
    edition.update(descriptor=descriptor, features=dict.fromkeys(edition["features"], ""))
    edition["features"]["medium"] = medium
    if proof:
        edition["feature_evidence"] = [
            {"field": "medium", "value": medium, "source_id": r.sources[0]["source_id"], "quote": descriptor}
        ]
    payload.update(editions=[edition], opinions=[])
    graph = validate_extraction_items(payload, r, Q)
    assert graph["editions"][0]["features"]["medium"] == expected
    if not expected:
        assert graph["rejected_items"]["UNSUPPORTED_FEATURE_MEDIUM"] == 1


@pytest.mark.parametrize("text", [f"I like {A}.", f"I love {A}.", f"I recommend {A}."])
def test_endorsement_is_not_comparative_preference(text: str) -> None:
    assert not reported_stance(text, A, "prefer")
    assert reported_stance(text, A, "endorse")
    r = evidence(text + " " + B)
    payload = extraction(r)
    payload["opinions"][0].update(
        quote=text, comparison_type="not_established", compared_edition_id="", characteristics=[]
    )
    graph = validate_extraction_items(payload, r, Q)
    assert graph["opinions"][0]["stance"] == "endorse"
    with pytest.raises(SearchError):
        validate_decision(decision(), graph, r, candidates())


def test_explicit_relative_liking_still_supports_a_preference() -> None:
    assert reported_stance(f"I like {A} better than {B}.", A, "prefer")
    assert not reported_stance(f"I like {A} better than {B}.", B, "prefer")


def test_comma_relative_clause_and_unrelated_negation_do_not_drop_a_review() -> None:
    text = f"It's a shame you didn't have access to {A}, which I think is the best, of the versions I've heard."
    assert reported_stance(text, A, "prefer")
    assert not reported_stance(f"I do not think {A} is better.", A, "prefer")
    assert not reported_stance(f"{B} is better, {A} is worse.", A, "prefer")


@pytest.mark.parametrize(
    "text",
    [
        "I've compared the originals against the remasters.",
        "I have compared the two editions.",
        "I recently compared both editions.",
    ],
)
def test_direct_comparison_can_use_normal_first_person_wording(text: str) -> None:
    assert reports_direct_comparison(text)
    assert not reports_direct_comparison("I have not compared the two editions.")


@pytest.mark.parametrize(
    "text", [f"The {B} version I didn't care for.", f"I don't like {B}.", f"I wasn't impressed with {B}."]
)
def test_negative_own_opinion_is_preserved(text: str) -> None:
    assert reported_stance(text, B, "against")
    assert not reported_stance(text, B, "prefer")


def test_late_qualification_stays_in_explicit_album_scope() -> None:
    text = f"Other Album: excellent.\n\nFor Example Album, I like {A} better than {B}.\n\nUp until recently {A} was my preferred edition.\n\nNow I prefer the newer edition.\n\nRegarding Another Album: unrelated preference."
    context, complete = post_excerpt(text, "Example Album", max_words=1200)
    assert "Now I prefer the newer edition." in context
    assert "Regarding Another Album" not in context and "Other Album" not in context
    assert complete is False


def test_list_introduction_owns_catalogue_rows() -> None:
    text = "Other Album is good.\n\nedit: so for Example Album the 2 preferred versions are\n\n2013 Geffen UICY-40018 SHM-CD\n\nand this 1984 reissue\n\nExample Artist - Example Album\n\nAm I missing something?\n\nAnother Album\n\nOther catalogue."
    context, _ = post_excerpt(text, "Example Album", max_words=1200)
    assert "UICY-40018" in context and "1984 reissue" in context
    assert "Other catalogue" not in context


def test_saved_original_text_is_rescoped_without_reconstructing_html() -> None:
    source = {
        "page_title": "Example Artist Discography",
        "full_text": "For Example Album, the original is good.\n\nNow I prefer the remaster.",
        "album_context": "For Example Album, the original is good.",
        "album_context_complete": False,
        "source_id": "fixed-id",
        "excerpt": "original preview",
    }
    result = refresh_source_context(source, Q)
    assert "Now I prefer" in result["album_context"]
    assert result["source_id"] == "fixed-id" and result["excerpt"] == "original preview"
    assert source["album_context"] == "For Example Album, the original is good."
    assert refresh_source_context({"excerpt": "missing original"}, Q) == {"excerpt": "missing original"}


def test_historical_favorite_cannot_become_current_winner() -> None:
    r = evidence(
        f"I like {A} better than {B}. Up until recently {A} was my preferred edition. Now the reissue is better."
    )
    payload = extraction(r)
    payload["opinions"][0].update(
        quote=f"I like {A} better than {B}.", comparison_type="not_established", characteristics=[]
    )
    with pytest.raises(SearchError, match="HISTORICAL_PREFERENCE"):
        validate_extraction(payload, r, Q)
    graph = validate_extraction_items(payload, r, Q)
    assert graph["opinions"][0]["stance"] == "context"
    with pytest.raises(SearchError):
        validate_decision(decision(), graph, r, candidates())


def test_comparison_and_characteristics_are_independent_claims() -> None:
    r = evidence()
    payload = extraction(r)
    payload["opinions"][0].update(
        compared_edition_id="invented",
        comparison_type="direct_reported",
        characteristics=["smoother EQ", "invented sonic detail"],
    )
    graph = validate_extraction_items(payload, r, Q)
    own = graph["opinions"][0]
    assert (
        own["stance"] == "prefer" and own["compared_edition_id"] == "" and own["comparison_type"] == "not_established"
    )
    assert own["characteristics"] == ["smoother EQ"]
    assert graph["rejected_items"]["WITHHELD_COMPARISON"] == 1
    assert graph["rejection_details"][0]["source_ids"] == [r.sources[0]["source_id"]]


def test_declared_ai_summary_cannot_count_as_an_own_review() -> None:
    r = evidence(f"Got this from a search using AI. I prefer {A} over {B}.")
    assert source_relevance(r.sources[0], Q) == "declared_ai_summary"
    with pytest.raises(SearchError):
        validate_extraction(extraction(r), r, Q)


def test_system_and_tentative_qualifications_are_kept_in_graph() -> None:
    r = evidence(f"{A} is my favorite, I guess. ON MY SYSTEM. {B} is another option.")
    payload = extraction(r)
    payload["opinions"][0].update(
        quote=f"{A} is my favorite, I guess.",
        comparison_type="not_established",
        compared_edition_id="",
        characteristics=[],
    )
    graph = validate_extraction(payload, r, Q)
    assert set(graph["opinions"][0]["source_caveats"]) >= {"system_specific_opinion", "tentative_opinion"}
    assert "limited_sources" in validate_decision(decision(), graph, r, candidates())["limitations"]


@pytest.mark.parametrize("artist", ["The Beatles", "Beatles"])
def test_article_alias_preserves_work_identity(artist: str) -> None:
    source = {
        "page_status": "FOUND",
        "scope": "post_excerpt",
        "page_title": "Beatles Abbey Road CD comparison",
        "album_context": "I prefer the original CD.",
        "album_context_complete": True,
    }
    assert source_relevance(source, Query("", artist, "Abbey Road")) == "usable"
    source["page_title"] = "Beatles With the Beatles CD comparison"
    assert source_relevance(source, Query("", artist, "The Beatles")) == "album_context_missing"
    source["page_title"] = "Beatles White Album CD comparison"
    assert source_relevance(source, Query("", artist, "The Beatles")) == "usable"


def test_self_titled_zeppelin_does_not_match_numbered_other_album() -> None:
    source = {
        "page_status": "FOUND",
        "scope": "post_excerpt",
        "page_title": "Led Zeppelin III original vs remaster",
        "album_context": "I prefer the original.",
        "album_context_complete": True,
    }
    assert source_relevance(source, Query("", "Led Zeppelin", "Led Zeppelin")) == "album_context_missing"
    source["page_title"] = "Led Zeppelin I original vs remaster"
    assert source_relevance(source, Query("", "Led Zeppelin", "Led Zeppelin")) == "usable"


def test_another_explicit_work_overrides_thread_title() -> None:
    source = {
        "page_status": "FOUND",
        "scope": "post_excerpt",
        "page_title": "Pink Floyd Animals comparison",
        "album_context": 'I was referring to "The Wall" especially. I prefer Harvest.',
        "album_context_complete": True,
    }
    assert source_relevance(source, Query("", "Pink Floyd", "Animals")) == "different_work_in_post"


def test_self_titled_discovery_uses_a_distinctive_work_identifier() -> None:
    from search_research import query_plan

    domains = ("forums.stevehoffman.tv",)
    white = query_plan(Query("", "The Beatles", "The Beatles"), [], domains, 1)[0]["query"]
    debut = query_plan(Query("", "Led Zeppelin", "Led Zeppelin"), [], domains, 1)[0]["query"]
    assert '"White Album"' in white and '"Led Zeppelin I"' in debut


def test_timing_discussion_is_context_priority_not_sound_comparison() -> None:
    from search_research import page_priority

    technical = {"title": "Pink Floyd Wish You Were Here - CD timing differences", "description": "", "url": URL}
    sound = {
        "title": "Pink Floyd Wish You Were Here best sounding masterings comparison",
        "description": "",
        "url": URL,
    }
    q = Query("", "Pink Floyd", "Wish You Were Here")
    assert page_priority(sound, q) < page_priority(technical, q)


def test_bad_diagnostics_do_not_retain_arbitrary_model_strings() -> None:
    r = evidence()
    p = extraction(r)
    p["opinions"][0]["source_id"] = "private-arbitrary-token"
    graph = validate_extraction_items(p, r, Q)
    assert "private-arbitrary-token" not in str(graph)
    assert graph["rejection_details"][0]["source_ids"] == []


def test_plain_text_interview_is_not_the_forum_authors_own_vote() -> None:
    r = evidence("Here's an excerpt from a very long online interview. Guest: I prefer " + A + ".")
    assert source_relevance(r.sources[0], Q) == "republished_commentary"


def test_cached_old_page_cannot_hide_newly_relevant_posts(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import json
    from dataclasses import replace

    from pipeline_config import SearchPolicy
    from search_research import ForumResearch
    from test_release_search import Network

    monkeypatch.setenv("SERPER_API_KEY", "fixture-key")
    policy = replace(
        SearchPolicy(),
        research_backend="serper",
        research_key_env="SERPER_API_KEY",
        research_disclosure=True,
        research_queries=1,
        research_pages=1,
        research_cache_directory=str(tmp_path),
    )
    researcher = ForumResearch(policy)
    old = [{**s, "original_url": URL, "final_url": URL} for s in evidence().sources]
    researcher.store("mastering-page-v6:80:" + URL + ":" + json.dumps([Q.artist, Q.title, Q.text]), old, {})
    new = b'<title>Example Artist Example Album</title><article class="message" id="post-99"><div class="message-body">I prefer the newly reviewed edition.</div></article>'
    net = Network(
        monkeypatch,
        lambda m, u, k: (
            (200, {"organic": [{"link": URL, "title": "Example Artist Example Album", "snippet": ""}]})
            if "serper" in u
            else (200, new)
        ),
    )
    result = researcher.collect(Q, [])
    assert len(net.calls) == 2 and result.sources[0]["post_id"] == "post-99"


def test_endorsement_keeps_independently_supported_sound_characteristics() -> None:
    r = evidence(f"I like {A}: smoother EQ. {B} is another option.")
    p = extraction(r)
    p["opinions"][0].update(
        quote=f"I like {A}: smoother EQ.", compared_edition_id="", comparison_type="not_established"
    )
    graph = validate_extraction_items(p, r, Q)
    assert graph["opinions"][0]["stance"] == "endorse"
    assert graph["opinions"][0]["characteristics"] == ["smoother EQ"]
