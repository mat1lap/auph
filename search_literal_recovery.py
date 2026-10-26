"""Recover explicit own preferences from retained prose, without a model or new facts."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from search_edition_identity import CATALOGUE, NAMES, PRINTED
from search_evidence_quality import SourceScope, choice_list_intro, scoped_text
from search_mastering import validate_extraction_items
from search_models import Query
from search_opinions import edition_mentions, opinion_parts, reported_stance, reports_direct_comparison
from search_research import FEATURES, Research, contains

NON_EDITIONS = {
    "CD",
    "LP",
    "DVD",
    "SHM",
    "UK",
    "US",
    "USA",
    "EU",
    "WG",
    "JP",
    "CDP",
    "NR",
    "EQ",
    "DAC",
    "USB",
    "HDMI",
    "DR",
    "IMO",
    "IMHO",
    "TOC",
    "PCM",
    "DSD",
    "MQA",
}


def non_listening_reason(span: str) -> bool:
    """A stated purchase/format advantage alone is not a listening preference."""
    return bool(
        re.search(
            r"(?i)\b(?:because|since|due to)\b[^.!?;\n]{0,100}\b(?:cheaper|price|cost|value|bit[ -]depth|sample rate|higher resolution|DR[ -]?\d{1,2}|\d{2}[ -]?bit|\d{2,3}\s*kHz)\b",
            span,
        )
    )


def candidates(text: str, editions: list[dict[str, Any]]) -> list[str]:
    values = {e["descriptor"] for e in editions if e["descriptor"] in text}
    values.update(m.group() for m in PRINTED.finditer(text))
    for match in re.finditer(
        r"(?i)\b(?:I\s+(?:(?:definitely|personally|still|really|usually)\s+)?prefer|I(?:['’]m| am)\s+going with|voted for)\s+(?:the\s+)?"
        r"(?P<name>[^\n,;:!?]+?)(?=\b(?:over|to|rather than|but)\b|\.(?:\s|$)|[,;:!?\n]|$)",
        text,
    ):
        name = match.group("name").strip()
        if len(name.split()) <= 12 and not re.search(
            r"(?i)\b(?:what|which|I|my|it|this|that|those|them|either|both|none)\b", name
        ):
            values.add(name)
    for pattern in (
        NAMES,
        CATALOGUE,
        r"(?<!\w)[A-Z]{2,8}(?:-[A-Z0-9]{1,8})*(?!\w)",
        r"\b[A-Z]{1,5}-\d{2,6}\b",
        r"\b(?:19|20)\d{2}(?:\s+(?:[Rr]emaster|[Vv]ersion|[Ee]dition))?\b",
    ):
        for match in re.finditer(pattern, text):
            if match.group() not in NON_EDITIONS:
                values.add(match.group())
    return sorted((v for v in values if len(v) <= 160), key=lambda v: (-len(v), v))


def literal_windows(text: str, descriptor: str) -> list[str]:
    parts = opinion_parts(text, descriptor)
    result = []
    for index, (start, end, terminal) in enumerate(parts):
        for last in range(index, min(index + 4, len(parts))):
            if last >= len(parts):
                continue
            _, finish, punctuation = parts[last]
            if punctuation.casefold() == "but":
                continue
            finish += len(punctuation) if punctuation in {".", "!", "?", ";"} else 0
            span = text[start:finish].strip()
            if span and len(span.split()) <= 100 and edition_mentions(span, descriptor):
                result.append(span)
    return result


def recover_preferences(
    research: Research, query: Query, selected_ids: set[str], existing: dict[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    """Emit canonical validated records with explicit rule provenance.

    No outside feature fills, catalogue equivalences, excerpts or rewritten quotes.
    Model errors/omissions are not evidence that an original post lacks a preference.
    """
    scope = SourceScope(research.sources, query)
    known = {e["edition_id"]: e for e in existing["editions"]}
    recorded = {
        (o["source_id"], known[o["edition_id"]]["descriptor"]) for o in existing["opinions"] if o["stance"] == "prefer"
    }
    result = []
    for source in research.sources:
        sid = source["source_id"]
        if sid not in selected_ids or scope.reason(source) != "usable":
            continue
        text = scoped_text(source)
        names = candidates(text, list(known.values()))
        proposals: list[tuple[str, str]] = []
        for descriptor in names:
            if any(
                old_sid == sid and edition_mentions(old_descriptor, descriptor) for old_sid, old_descriptor in recorded
            ):
                continue
            # A bare brand inside a more specific supported name is not an extra vote.
            if any(edition_mentions(p[0], descriptor) for p in proposals):
                continue
            windows = [
                s
                for s in literal_windows(text, descriptor)
                if reported_stance(s, descriptor, "prefer") and not non_listening_reason(s)
            ]
            if not windows and choice_list_intro(source, query) and len(text.split()) <= 45 and descriptor in text:
                windows = [text.strip()]
            if not windows:
                continue
            span = min(
                windows,
                key=lambda s: (
                    not reports_direct_comparison(s),
                    -len(
                        re.findall(
                            r"(?i)\b(?:sound\w*|bass|treble|dynamics|harsh|dull|detailed|relaxed|crankable|natural)\b",
                            s,
                        )
                    ),
                    len(s),
                ),
            )
            proposals.append((descriptor, span))
        for index, (descriptor, span) in enumerate(proposals):
            digest = hashlib.sha256((sid + "\0" + descriptor).encode()).hexdigest()[:16]
            raw_edition: dict[str, Any] = {
                "edition_id": "literal:" + digest,
                "descriptor": descriptor,
                "features": dict.fromkeys(FEATURES, ""),
                "feature_evidence": [],
                "source_ids": [sid],
            }
            # Reuse identifiers only when this own source independently supports
            # every nonempty feature. Another author's full pressing stays separate.
            own = next(
                (
                    e
                    for e in known.values()
                    if e["descriptor"] == descriptor
                    and sid in e["source_ids"]
                    and all(
                        not value
                        or contains(descriptor, value)
                        or any(
                            p["field"] == field and p["value"] == value and p["source_id"] == sid
                            for p in e.get("feature_evidence", [])
                        )
                        for field, value in e["features"].items()
                    )
                ),
                None,
            )
            if own:
                raw_edition["features"] = dict(own["features"])
                raw_edition["feature_evidence"] = [p for p in own.get("feature_evidence", []) if p["source_id"] == sid]
            payload = {
                "editions": [raw_edition],
                "opinions": [
                    {
                        "opinion_id": "literal:" + digest,
                        "source_id": sid,
                        "edition_id": raw_edition["edition_id"],
                        "compared_edition_id": "",
                        "stance": "prefer",
                        "quote": span,
                        "comparison_type": "not_established",
                        "characteristics": [],
                    }
                ],
                "equivalences": [],
            }
            others = [
                v
                for v in names
                if v != descriptor
                and edition_mentions(span, v)
                and not edition_mentions(descriptor, v)
                and not edition_mentions(v, descriptor)
            ]
            others = [v for v in others if not any(w != v and edition_mentions(w, v) for w in others)]
            if reports_direct_comparison(span) and len(others) == 1:
                other = {
                    **raw_edition,
                    "edition_id": raw_edition["edition_id"] + ":other",
                    "descriptor": others[0],
                    "features": dict.fromkeys(FEATURES, ""),
                    "feature_evidence": [],
                }
                payload["editions"].append(other)
                payload["opinions"][0].update(
                    compared_edition_id=other["edition_id"], comparison_type="direct_reported"
                )
            graph = validate_extraction_items(payload, research, query, allowed_source_ids={sid})
            if not graph["opinions"] or graph["opinions"][0]["stance"] != "prefer":
                continue
            graph["opinions"][0]["extraction_method"] = "literal_rule"
            result.append(("literal:" + digest, graph))
    return result
