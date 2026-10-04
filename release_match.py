#!/usr/bin/env python3
"""Deterministic release-candidate acceptance and ambiguity handling."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


def assess_medium_layouts(
    local_disc_counts: dict[int, int],
    media: Iterable[dict[str, Any]],
    local_disc_durations: dict[int, tuple[float, ...]] | None = None,
) -> dict[str, Any]:
    """Validate every present local medium against a candidate release.

    Audio fingerprints identify recordings, not editions; the per-medium layout
    is the minimum release-level evidence for multi-disc safety.
    """
    media = list(media)
    candidate = {int(item.get("position") or 0): int(item.get("track-count") or 0) for item in media}
    present = sorted(local_disc_counts)
    conflicts: list[str] = []
    for disc, count in sorted(local_disc_counts.items()):
        expected = candidate.get(disc)
        if expected is None:
            conflicts.append(f"candidate lacks local disc {disc}")
        elif expected != count:
            conflicts.append(f"disc {disc} track count {count}!={expected}")
    if local_disc_durations:
        for medium in media:
            position = int(medium.get("position") or 0)
            local = local_disc_durations.get(position)
            tracks = medium.get("tracks") or []
            if not local or len(local) != len(tracks):
                continue
            remote = [
                float(track.get("length") or (track.get("recording") or {}).get("length") or 0) / 1000
                for track in tracks
            ]
            if all(remote) and any(abs(left - right) > max(8.0, left * 0.06) for left, right in zip(local, remote)):
                conflicts.append(f"disc {position} duration layout conflict")
    complete = bool(candidate) and set(present) == set(candidate) and not conflicts
    return {
        "compatible": not conflicts,
        "complete": complete,
        "conflicts": tuple(conflicts),
        "candidate_disc_count": len(candidate),
    }


from pipeline_config import load_config


class ReleaseConfidence(StrEnum):
    REJECT = "REJECT"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    VERY_HIGH = "VERY_HIGH"
    EXACT = "EXACT"
    CERTAIN = "CERTAIN"
    AMBIGUOUS = "AMBIGUOUS"


ACCEPTABLE = {
    ReleaseConfidence.HIGH.value,
    ReleaseConfidence.VERY_HIGH.value,
    ReleaseConfidence.EXACT.value,
    ReleaseConfidence.CERTAIN.value,
}


@dataclass(frozen=True)
class ReleaseSelection:
    candidate: dict[str, Any] | None
    confidence: ReleaseConfidence
    margin: float
    reason: str
    ranked: tuple[dict[str, Any], ...]


def select_release_candidate(
    candidates: Iterable[dict[str, Any]],
    *,
    minimum_score: float | None = None,
    minimum_margin: float | None = None,
) -> ReleaseSelection:
    config = load_config().matching
    minimum_score = config.release_min_score if minimum_score is None else minimum_score
    minimum_margin = config.release_min_margin if minimum_margin is None else minimum_margin
    eligible = [
        item
        for item in candidates
        if not (item.get("identity") or {}).get("rejected", True)
        and str((item.get("identity") or {}).get("confidence", "NONE")) in ACCEPTABLE
        and float((item.get("identity") or {}).get("score", 0)) >= minimum_score
    ]
    eligible.sort(
        key=lambda item: (
            -float((item.get("identity") or {}).get("score", 0)),
            str(item.get("id") or ""),
        )
    )
    # Discovery can return the same edition through several routes. Compare
    # the winner against the next *different edition*, otherwise duplicate
    # rows can either manufacture ambiguity or conceal a close competitor.
    distinct: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in eligible:
        edition_id = str(item.get("id") or "").strip().casefold()
        if edition_id and edition_id in seen_ids:
            continue
        if edition_id:
            seen_ids.add(edition_id)
        distinct.append(item)
    ranked = tuple(distinct)
    if not distinct:
        return ReleaseSelection(None, ReleaseConfidence.REJECT, 0.0, "no high-confidence edition candidate", ranked)
    best = distinct[0]
    best_ident = best.get("identity") or {}
    best_score = float(best_ident.get("score", 0))
    second_score = float((distinct[1].get("identity") or {}).get("score", 0)) if len(distinct) > 1 else 0.0
    margin = best_score - second_score if len(distinct) > 1 else best_score
    confidence = ReleaseConfidence(str(best_ident.get("confidence")))
    strongest = confidence in {ReleaseConfidence.CERTAIN, ReleaseConfidence.EXACT}

    if len(distinct) > 1 and margin < minimum_margin:
        return ReleaseSelection(
            None, ReleaseConfidence.AMBIGUOUS, margin, "top edition candidates are too close", ranked
        )
    if not strongest and margin < minimum_margin:
        return ReleaseSelection(
            None, ReleaseConfidence.AMBIGUOUS, margin, "candidate lacks a safe score margin", ranked
        )
    return ReleaseSelection(best, confidence, margin, "accepted high-confidence edition", ranked)
