"""Identity evidence can widen coverage without moving opinions between authors/works."""

from copy import deepcopy

import pytest

from search_edition_identity import edition_references
from search_evidence_quality import SourceScope, source_relevance
from search_mastering import validate_decision, validate_extraction_items
from search_models import Candidate
from search_opinions import reported_stance, reports_direct_comparison
from test_search_mastering import A, B, Q, decision, evidence, extraction


def test_artist_identity_from_an_original_post_in_the_same_album_thread() -> None:
    r = evidence()
    own = r.sources[0]
    own["page_title"] = "Example Album mastering"
    anchor = {
        **deepcopy(own),
        "source_id": "forum:identity",
        "album_context": "Example Artist - Example Album.",
        "full_text": "Example Artist - Example Album.",
    }
    r.sources.append(anchor)
    assert source_relevance(own, Q) == "artist_context_missing"
    scope = SourceScope(r.sources, Q)
    assert scope.reason(own) == "usable"
    assert scope.identity_evidence(own)[0]["quote"] in anchor["album_context"]
    assert validate_extraction_items(extraction(r), r, Q)["opinions"][0]["source_id"] == own["source_id"]


@pytest.mark.parametrize("bad", ["other_thread", "quoted", "snippet", "ai", "other_work"])
def test_unconfirmed_context_cannot_certify_artist_identity(bad: str) -> None:
    r = evidence()
    own = r.sources[0]
    own["page_title"] = "Example Album mastering"
    anchor = {
        **deepcopy(own),
        "source_id": "forum:identity",
        "album_context": "Example Artist - Example Album.",
        "full_text": "Example Artist - Example Album.",
    }
    if bad == "other_thread":
        anchor["url"] = "https://forums.stevehoffman.tv/threads/other.101/"
    if bad == "quoted":
        anchor.update(
            album_context="I agree.", full_text="I agree.", quoted_context=[{"text": "Example Artist - Example Album."}]
        )
    if bad == "snippet":
        anchor["page_status"] = "NOT_FETCHED"
    if bad == "ai":
        anchor["full_text"] = "Got this from a search using AI. Example Artist - Example Album."
    if bad == "other_work":
        anchor.update(album_context="Example Artist - Another Album.", full_text="Example Artist - Another Album.")
    own["thread_artist_evidence"] = [{"source_id": "fake", "quote": "Example Artist - Example Album."}]
    assert SourceScope([own, anchor], Q).reason(own) == "artist_context_missing"


def test_thread_identity_does_not_override_an_explicit_reply_about_another_work() -> None:
    r = evidence('For "Another Album", I prefer ' + A + ".")
    own = r.sources[0]
    own["page_title"] = "Example Album mastering"
    anchor = {
        **deepcopy(own),
        "source_id": "forum:identity",
        "album_context": "Example Artist - Example Album.",
        "full_text": "Example Artist - Example Album.",
    }
    assert SourceScope([own, anchor], Q).reason(own) == "different_work_in_post"


def test_shared_choice_heading_preserves_only_the_requested_album_row() -> None:
    r = evidence()
    source = r.sources[0]
    context = f"Example Album - {A}"
    source.update(
        album_context=context,
        full_text=f"My choice:\n\nOther Album\n- {B}\nSounds bright.\n\nExample Album\n- {A}",
        album_context_complete=False,
    )
    payload = extraction(r)
    payload["editions"] = payload["editions"][:1]
    payload["opinions"][0].update(
        quote=context, compared_edition_id="", comparison_type="not_established", characteristics=["bright"]
    )
    graph = validate_extraction_items(payload, r, Q)
    op = graph["opinions"][0]
    assert op["stance"] == "prefer" and op["preference_basis_quote"] == "My choice:"
    assert op["characteristics"] == []
    assert "personal_list_choice" in op["source_caveats"]


def test_list_heading_does_not_override_negative_or_conditional_prose() -> None:
    r = evidence()
    source = r.sources[0]
    context = f"Example Album - {A}. I would not choose it."
    source.update(album_context=context, full_text=f"My choice:\n\n{context}")
    payload = extraction(r)
    payload["editions"] = payload["editions"][:1]
    payload["opinions"][0].update(
        quote=context, compared_edition_id="", comparison_type="not_established", characteristics=[]
    )
    assert validate_extraction_items(payload, r, Q)["opinions"] == []


def test_short_catalogue_in_opinion_can_bind_to_full_name_in_the_same_post() -> None:
    text = f"I own {A} and {B}. I prefer A-123 over B-456."
    r = evidence(text)
    payload = extraction(r)
    payload["opinions"][0].update(
        quote="I prefer A-123 over B-456.", characteristics=[], comparison_type="not_established"
    )
    graph = validate_extraction_items(payload, r, Q)
    assert graph["opinions"][0]["edition_mention_evidence"] == A
    assert graph["opinions"][0]["stance"] == "prefer"


def test_explicit_parenthetical_definition_can_resolve_bt_in_its_own_post() -> None:
    r = evidence("Black Triangle (BT) is one version. I prefer BT.")
    payload = extraction(r)
    payload["editions"] = payload["editions"][:1]
    payload["editions"][0].update(
        descriptor="Black Triangle", features=dict.fromkeys(payload["editions"][0]["features"], "")
    )
    payload["opinions"][0].update(
        quote="I prefer BT.", compared_edition_id="", characteristics=[], comparison_type="not_established"
    )
    graph = validate_extraction_items(payload, r, Q)
    assert graph["opinions"][0]["edition_mention_evidence"] == "Black Triangle (BT)"


def test_another_authors_full_name_does_not_resolve_a_bare_brand_vote() -> None:
    r = evidence("I prefer MFSL.")
    own = r.sources[0]
    r.sources.append(
        {
            **deepcopy(own),
            "source_id": "forum:reference",
            "album_context": "MFSL gold CD UDCD 545",
            "full_text": "MFSL gold CD UDCD 545",
        }
    )
    payload = extraction(r)
    payload["editions"] = payload["editions"][:1]
    e = payload["editions"][0]
    e.update(
        descriptor="MFSL gold CD UDCD 545", source_ids=["forum:reference"], features=dict.fromkeys(e["features"], "")
    )
    payload["opinions"][0].update(
        quote="I prefer MFSL.", compared_edition_id="", characteristics=[], comparison_type="not_established"
    )
    assert validate_extraction_items(payload, r, Q)["opinions"] == []


def test_two_catalogues_for_the_same_name_are_not_one_edition() -> None:
    r = evidence("MFSL gold CD UDCD 545 and MFSL gold CD UDCD 568. I prefer MFSL.")
    payload = extraction(r)
    for e, desc in zip(payload["editions"], ["MFSL gold CD UDCD 545", "MFSL gold CD UDCD 568"], strict=True):
        e.update(descriptor=desc, features=dict.fromkeys(e["features"], ""))
    payload["opinions"][0].update(
        quote="I prefer MFSL.", compared_edition_id="", characteristics=[], comparison_type="not_established"
    )
    assert validate_extraction_items(payload, r, Q)["opinions"] == []


def test_printed_references_are_literal_and_do_not_borrow_a_comparisons_catalogue() -> None:
    r = evidence("MFSL Remaster (UDCD 568), DCC Compact Classics GZS-1144. MFSL beats the old MCA MCAD-37220.")
    refs = edition_references(r.sources, Q)
    assert {x["catalog_number"] for x in refs if x["name"] == "MFSL"} == {"UDCD 568"}
    assert all(x["quote"] in r.sources[0]["album_context"] for x in refs)
    r.sources[0]["page_status"] = "NOT_FETCHED"
    assert edition_references(r.sources, Q) == []


def test_cross_post_catalogue_reference_can_propose_a_check_but_not_recommend_a_rip() -> None:
    r = evidence("I prefer MFSL over the remaster.")
    own = r.sources[0]
    r.sources.append(
        {
            **deepcopy(own),
            "source_id": "forum:reference",
            "album_context": "MFSL gold CD UDCD 545",
            "full_text": "MFSL gold CD UDCD 545",
        }
    )
    payload = extraction(r)
    payload["editions"] = payload["editions"][:1]
    e = payload["editions"][0]
    e.update(descriptor="MFSL", features=dict.fromkeys(e["features"], ""))
    payload["opinions"][0].update(
        quote="I prefer MFSL over the remaster.",
        compared_edition_id="",
        characteristics=[],
        comparison_type="not_established",
    )
    graph = validate_extraction_items(payload, r, Q)
    assert graph["editions"][0]["features"]["catalog_number"] == ""
    c = Candidate(
        "rutracker:1",
        "https://rutracker.org/forum/viewtopic.php?t=1",
        "Example Artist - Example Album",
        catalog_number="UDCD545",
        medium="CD",
    )
    review = validate_decision(decision(), graph, r, [c])
    assert review["candidate_id"] == "" and review["match_confidence"] == "unestablished"
    assert review["identity_reference_candidates"][0]["catalog_number"] == "UDCD 545"
    assert review["identity_reference_candidates"][0]["identity_binding"] == "unconfirmed"


def test_price_advantage_does_not_establish_sound_preference() -> None:
    assert not reported_stance("The Piros cut is the best bang for your buck.", "Piros cut", "prefer")
    assert not reported_stance("MFSL is the best value.", "MFSL", "prefer")
    assert reported_stance("MFSL is the best sounding to me.", "MFSL", "prefer")


def test_explicit_listening_choice_is_not_lost_without_the_word_prefer() -> None:
    quote = (
        "After doing several listening sessions of comparing the 2009 remaster, "
        "to the Black Triangle, I'm going with the 2009 remaster."
    )
    assert reported_stance(quote, "2009 remaster", "prefer")
    assert not reported_stance(quote, "Black Triangle", "prefer")
    assert reports_direct_comparison(quote)
    assert not reported_stance("Are you going with the 2009 remaster?", "2009 remaster", "prefer")
    assert not reported_stance("I'm not going with the 2009 remaster.", "2009 remaster", "prefer")
    assert not reports_direct_comparison("I did not do listening sessions of comparing these.")


def test_catalogue_check_links_the_post_that_printed_the_identifier() -> None:
    r = evidence("I prefer MFSL.")
    sid = r.sources[0]["source_id"]
    r.sources.append(
        {
            **deepcopy(r.sources[0]),
            "source_id": "forum:catalogue",
            "album_context": "MFSL UDCD 545",
            "full_text": "MFSL UDCD 545",
        }
    )
    payload = extraction(r)
    payload["editions"] = payload["editions"][:1]
    e = payload["editions"][0]
    e.update(
        descriptor="MFSL",
        features={**dict.fromkeys(e["features"], ""), "catalog_number": "UDCD 545"},
        source_ids=[sid],
        feature_evidence=[
            {
                "field": "catalog_number",
                "value": "UDCD 545",
                "source_id": "forum:catalogue",
                "quote": "MFSL UDCD 545",
            }
        ],
    )
    payload["opinions"][0].update(
        quote="I prefer MFSL.", compared_edition_id="", comparison_type="not_established", characteristics=[]
    )
    graph = validate_extraction_items(payload, r, Q)
    c = Candidate("rutracker:1", "https://rutracker.org/forum/viewtopic.php?t=1", "Example", catalog_number="UDCD 545")
    review = validate_decision(decision(), graph, r, [c])
    ref = next(r for r in review["identity_reference_candidates"] if r["identity_binding"] == "catalogue_only")
    assert ref["source_id"] == "forum:catalogue" and ref["quote"] == "MFSL UDCD 545"
