#!/usr/bin/env python3
"""Read frozen evidence and audit old results offline; never invokes a provider."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from mastering_benchmark import fingerprint, write_json
from search_evidence_quality import literal_term, source_relevance
from search_mastering import decision_schema, extraction_schema, validate_decision, validate_extraction_items
from search_models import Candidate, Query, SearchError
from search_research import Research, refresh_source_context

# Independent audit observations, not missing model replies or objective listening labels.
CASE_OBSERVATIONS: dict[str, tuple[list[str], str]] = {
    "album-02": (
        ["edition_reference_needs_context", "lexical_or_extraction_loss"],
        "Reviewer names Innuendo as a preferred version of a previously described set; exact edition binding remains incomplete.",
    ),
    "album-03": (
        ["explicit_preference_not_retained", "lexical_or_extraction_loss"],
        "An own DCC preference and first-person comparison exist in retained original text; zero opinions survived the old extraction.",
    ),
    "album-05": (
        ["artist_context_missing", "secondhand_summary"],
        "Retrieved post summarizes differing poll/other-user preferences; it does not establish its own comparative choice.",
    ),
    "album-06": (
        ["retrieval_topic_mismatch"],
        "Read pages focus on CD timing and Wizard of Oz synchronization, rather than mastering preference.",
    ),
    "album-07": (
        ["extraction_records_rejected", "different_work_in_post"],
        "Own Animals comparisons exist, but some replies explicitly discuss The Wall; old output lost all editions.",
    ),
    "album-09": (
        ["artist_article_alias", "ambiguous_or_positive_only"],
        "Beatles/The Beatles spelling filtered relevant posts. Some retained statements are generic interviews or tentative liking.",
    ),
    "album-10": (
        ["artist_context_missing", "lexical_or_extraction_loss", "mix_and_medium_scope"],
        "Own stereo/CD preferences exist; some sources lack artist context or discuss other mixes/media.",
    ),
    "album-11": (
        ["declared_ai_summary", "album_alias"],
        "A seemingly relevant album-by-album recommendation explicitly says it came from an AI search. It cannot count as listening evidence.",
    ),
    "album-12": (
        ["self_titled_work_ambiguity"],
        "The Beatles as an album title admitted With the Beatles and other Beatles works; distinctive White Album identification is required.",
    ),
    "album-13": (
        ["self_titled_work_ambiguity", "catalogue_wide_not_album_specific"],
        "Led Zeppelin as an album title admitted Led Zeppelin III and catalogue-wide comments; a specific debut identifier is needed.",
    ),
    "album-14": (
        ["explicit_preference_not_retained"],
        "A reviewer reports the RL cut as best in their own experience; the old graph contains no opinions. The original model proposal is unavailable.",
    ),
    "album-15": (
        ["hearsay_only"],
        "The retained recommendation statement explicitly reports what other people say, not the author's own comparison.",
    ),
    "album-17": (
        ["explicit_preference_not_retained", "comparison_record_rejected", "mix_and_medium_scope"],
        "Own West German/SACD and DVD-A opinions exist; multichannel mix listening must not silently establish a stereo mastering winner.",
    ),
    "album-18": (
        ["explicit_preference_not_retained", "edition_reference_needs_context"],
        "One post explicitly favors MFSL for Love over Gold; the original extraction did not preserve a supporting opinion.",
    ),
    "album-19": (
        ["lexical_or_extraction_loss"],
        "The author reports fatigue with the DE and recovery after obtaining 1986 Virgin Japan; a narrative preference is not captured by a simple keyword contract.",
    ),
    "album-20": (
        ["explicit_preference_not_retained", "pronoun_reference", "poll_not_consensus"],
        "Own US ATCO preference exists; it uses anaphora after describing the owned disc. Aggregate poll results do not prove consensus.",
    ),
    "album-22": (
        ["list_identifiers_omitted", "edition_reference_needs_context"],
        "The preferred-versions introduction lost UICY-40018 and the 1984 reissue lines. Other posts name a personal Mastering 3 go-to.",
    ),
    "album-23": (
        ["explicit_preference_not_retained", "pronoun_reference"],
        "A listener who bought both versions explicitly prefers the 45; no opinion was present in the old extraction.",
    ),
    "album-24": (
        ["artist_context_missing", "extraction_records_rejected", "mix_and_medium_scope"],
        "Own DCC and DVD-A opinions exist but artist context is sometimes absent; vinyl, stereo and multichannel scope differ.",
    ),
}


# Manual observations apply only to the exact reviewed evidence, never another corpus.
REVIEWED_FINGERPRINTS: dict[str, str] = {
    "album-01": "7a5a0d4ed05685912a4020f31509018c7283a7366769e2c24fa0b9062ec07b62",
    "album-02": "ca6b869795a93dd478cf5a364a6964f97704eaf9c82a7b45b4fb316ed1aa8d5c",
    "album-03": "bcd98802be23a04993cd08356fd047e369ab58a97bf8d457773d89f0fa0f0a59",
    "album-04": "38c440731705fb5e93e99368f0d718c290855b77fa00d82af1ac108a5cf923de",
    "album-05": "080f634faefb4baec9e030437a7b0df5d3d201cb123b3060f11ec0696d075f48",
    "album-06": "2d420f91c8329441da32e632825233953482cc6fe6b4abfafbf31e2ed42018fc",
    "album-07": "793ad7800b7427a94cc6855d5b4fc9e76b210bb817367fd829b37ad832cb6ec2",
    "album-08": "88b1d2983065c1eb7dd055bae3bf2e9e242e6996af7fabd67a256a74302af321",
    "album-09": "cd10be17ceba63577019a7b336cf55af4affb6b061d97dac7bbedfd18c2642df",
    "album-10": "102aa1d1718ee3bc9b9a341ffcaf5c069f102e1af86502331760a78fc63ec324",
    "album-11": "cde26540bc3f230d5b4662a781c598f34e19c9c26854d129490b988b5fcbc4b8",
    "album-12": "9e2821655fabaa53bb7b694762bb7d636e53ad1649232f54942745747f4e4a2a",
    "album-13": "88473f70f152687a52e626fa07f42f92ff3662254dc292f002c07a26356be9dd",
    "album-14": "3e70c56198335651e93360adb29188fb53eeab865935db93ebdb571331f4aea1",
    "album-15": "b8cf07132d2eec085ee9ad909b16adaadfac3271de7e973756e5be4c137ea8f3",
    "album-16": "621bc8332acc65f0baf5fd142b09606f6911239e322c9af2d3f04c33d0248b79",
    "album-17": "bffdbf77186e9519984b77b6f121ed29f9eb65b0ab51a8efbc5add20039b0043",
    "album-18": "7207f2df1e87098baf5aae591fce3d71f0c8caf2fddfb36147d6838061e7fbd4",
    "album-19": "38067b1e8cba55103515cf5d59bbcfb65dbc704c98076f66fccc50d567cc889f",
    "album-20": "7a1a976f9d4ac197d65919e7a0e040697eb54d1cf3f1f5f54e8a0cc373c81e92",
    "album-21": "d4c407874c8c477d659c0ff4bcc8509e7ff97414c63d967d2a09ae71568d89b4",
    "album-22": "7558be79dd991c7d30c142d55a87ce2bae134d7839758aa2945099905b6fc817",
    "album-23": "e5ff7a57447a168dfd4a0ab25af9d80bc2ef9048b772daf43fb33eaa6f50fc6d",
    "album-24": "668dc080b9d16dc9895fd25f53f5b1f6fb2b243eb4070d1a8dd4d1c2ccb58d8e",
}


def payload_from_graph(graph: dict[str, Any]) -> dict[str, Any]:
    schema = extraction_schema()["properties"]
    return {
        field: [{k: row[k] for k in schema[field]["items"]["properties"] if k in row} for row in graph.get(field, [])]
        for field in ("editions", "opinions", "equivalences")
    }


def original_source_admission(source: dict[str, Any], query: Query) -> bool:
    """Frozen pre-audit admission contract, used only for offline before/after counts."""
    if source.get("page_status") != "FOUND" or source.get("scope") != "post_excerpt":
        return False
    text = str(source.get("album_context") or source.get("excerpt", ""))
    terms = [query.artist, query.title] if query.artist or query.title else [query.text]
    if not all(literal_term(str(source.get("page_title", "")) + " " + text, term) for term in terms if term):
        return False
    return bool(
        source.get("album_context_complete", source.get("context_complete", False))
        or (query.title and literal_term(text, query.title))
    )


def audit(base: Path, output: Path) -> dict[str, Any]:
    manifest = json.loads((base / "corpus/manifest.json").read_text())
    checks = {r["case_id"]: r["fingerprint"] for r in manifest["cases"]}
    summary = json.loads((base / "final-comparison/summary.json").read_text())
    rows = []
    for row in summary["results"]:
        if row["model"] != "gemini:gemini-3.5-flash-lite" or "case_id" not in row:
            continue
        cid = row["case_id"]
        if cid not in checks:
            raise SearchError("Unknown corpus case")
        snapshot = json.loads((base / "corpus" / (cid + ".json")).read_text())
        if fingerprint(snapshot) != checks[cid]:
            raise SearchError("Corpus changed")
        q = Query(**snapshot["query"])
        old_sources = snapshot["research"]["sources"]
        sources = [refresh_source_context(s, q) for s in old_sources]
        admitted_before = {s["source_id"] for s in old_sources if original_source_admission(s, q)}
        admitted_after = {s["source_id"] for s in sources if source_relevance(s, q) == "usable"}
        research = Research(**{**snapshot["research"], "sources": sources})
        review = row.get("review") or {}
        graph = validate_extraction_items(payload_from_graph(review.get("evidence_graph", {})), research, q)
        result: dict[str, Any] = {
            "case_id": cid,
            "artist": q.artist,
            "album": q.title,
            "fingerprint": checks[cid],
            "old_status": row["metrics"]["status"],
            "source_relevance": dict(Counter(source_relevance(s, q) for s in sources)),
            "old_retained_sources": review.get("retained_sources"),
            "old_usable_sources": len(admitted_before),
            "current_usable_sources": len(admitted_after),
            "newly_admitted_source_ids": sorted(admitted_after - admitted_before),
            "now_context_only_source_ids": sorted(admitted_before - admitted_after),
            "context_expanded_source_ids": [
                new["source_id"]
                for old, new in zip(old_sources, sources, strict=True)
                if len(new.get("album_context", "")) > len(old.get("album_context", ""))
            ],
            "old_rejections": review.get("evidence_graph", {}).get("rejected_items", {}),
            "current_graph": graph,
            "original_extraction_available": False,
            "interpretation": "Readback of retained model records only; no missing opinions/replies were reconstructed or newly generated.",
        }
        if result["old_status"] == "preference":
            decision = {k: review[k] for k in decision_schema(graph)["properties"]}
            try:
                current = validate_decision(decision, graph, research, [Candidate(**c) for c in snapshot["candidates"]])
            except SearchError:
                stance_counts = Counter(o["stance"] for o in graph["opinions"])
                result["verdict"] = (
                    "endorsement_only"
                    if stance_counts["endorse"] and not stance_counts["prefer"]
                    else "historical_or_insufficient_preference"
                )
                result["previous_preferred_edition"] = review["preferred_edition"]
                result["offline_withdrawal"] = validate_decision(
                    {
                        "status": "insufficient_evidence",
                        "preferred_edition_id": "",
                        "support_opinion_ids": [],
                        "dissent_opinion_ids": [],
                        "rationale": "Прежнее сравнительное предпочтение не подтверждено после проверки полного сохранённого контекста.",
                        "limitations": ["limited_sources"],
                    },
                    graph,
                    research,
                    [],
                )
            else:
                result["verdict"] = "conditional_preference_supported"
                result["checked_preference"] = current
                result["qualification"] = (
                    "One author's tentative/system-specific choice; other original-post criticism was omitted by the old extraction, so not a verified global best."
                )
        elif result["old_status"] == "insufficient_evidence":
            if REVIEWED_FINGERPRINTS.get(cid) == checks[cid] and cid in CASE_OBSERVATIONS:
                causes, note = CASE_OBSERVATIONS[cid]
                result.update(observed_causes=causes, audit_note=note, manual_observations_applied=True)
            else:
                result.update(
                    observed_causes=[],
                    manual_observations_applied=False,
                    audit_note="No reviewed annotation applies to this fingerprint; diagnostics only.",
                )
        else:
            result["verdict"] = "separate_decision_contract_failure"
            result["audit_note"] = (
                "Not one of the 19 abstentions. Rejected decision body was not retained; its precise failing rule cannot be reconstructed."
            )
        rows.append(result)
        write_json(output / (cid + ".json"), result)
    result = {
        "version": 1,
        "cases": rows,
        "recommendation_verdicts": dict(Counter(r["verdict"] for r in rows if r["old_status"] == "preference")),
        "abstentions_audited": sum(r["old_status"] == "insufficient_evidence" for r in rows),
        "new_provider_calls": 0,
        "original_corpus_modified": False,
        "caveat": "Source-review and current-validator readback; not a new model benchmark or objective audio listening score.",
    }
    write_json(output / "summary.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--artifacts", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args(argv)
    result = audit(args.artifacts, args.output)
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "recommendation_verdicts",
                    "abstentions_audited",
                    "new_provider_calls",
                    "original_corpus_modified",
                )
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
