#!/usr/bin/env python3
"""Resolve one local FLAC track to one MusicBrainz release track.

The local title (especially the filename stem / CUE title) is stronger evidence
than a stale TRACKNUMBER tag. Duration is used as a consistency check, never as
the sole identifier. A leading physical Intro/Prelude is handled as a known
1-track offset variant.
"""

from __future__ import annotations

import argparse
import difflib
import json
import math
import re
import unicodedata
from pathlib import Path

from lyrics_quality import version_signature


def norm(s: str) -> str:
    s = unicodedata.normalize("NFC", str(s or "")).casefold()
    s = re.sub(r"\b(feat\.?|featuring|ft\.?)\b", " ", s)
    s = re.sub(r"[^\w]+", "", s, flags=re.UNICODE)
    return s


def sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def parse_duration(v: str | float | None) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def intro_like(title: str) -> bool:
    n = norm(title)
    return any(k in n for k in ("intro", "introduction", "prelude", "overture", "prologue", "opening"))


def version_markers(title: str) -> set[str]:
    markers = set(version_signature(str(title or "")))
    # Preserve legacy mix/edit/reprise qualifiers only inside explicit version
    # segments. Ordinary title words are never version evidence.
    segments = re.findall(r"[([{]([^)]*?)[)\]}]", str(title or ""))
    segments.extend(re.split(r"\s+[-–—/]\s+", str(title or ""))[1:])
    for segment in segments:
        markers.update(set(re.findall(r"[\w]+", segment.casefold())).intersection({"mix", "edit", "reprise"}))
    return markers


def artist_credit(ac: list[dict] | None) -> str:
    out = []
    for c in ac or []:
        out.append(c.get("name") or (c.get("artist") or {}).get("name") or "")
        out.append(c.get("joinphrase") or "")
    return "".join(out).strip()


def build_mediums(mb: dict) -> list[dict]:
    return [m for m in mb.get("media", []) if isinstance(m, dict)]


def choose_medium(mb: dict, disc: int, local_title: str, local_number: int | None) -> tuple[dict | None, str]:
    media = build_mediums(mb)
    if not media:
        return None, "no-media"
    by_position = [m for m in media if str(m.get("position", "")) == str(disc)]
    if len(by_position) > 1:
        return None, "ambiguous-disc-position"
    if by_position:
        return by_position[0], "disc-position"
    unpositioned = [m for m in media if not str(m.get("position") or "").isdigit() or int(m["position"]) < 1]
    if disc > 0 and not unpositioned:
        return None, "disc-position-mismatch"
    scored = []
    for m in unpositioned if disc > 0 else media:
        tracks = m.get("tracks") or []
        score = 0
        if local_number and any(str(t.get("number", "")) == str(local_number) for t in tracks):
            score += 20
        if local_title:
            score += max(
                [
                    int(100 * sim(local_title, str(t.get("title") or (t.get("recording") or {}).get("title") or "")))
                    for t in tracks
                ]
                or [0]
            )
        scored.append((score, m))
    scored.sort(key=lambda x: -x[0])
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 20:
        return None, "ambiguous-medium"
    return scored[0][1], "content-ranked" if scored else "no-media"


def resolve_mb_track(
    mb: dict,
    disc: int,
    tracknumber: int,
    filename_title: str,
    current_title: str,
    current_artist: str,
    duration: float,
    cue_title: str = "",
    offset: int = 0,
) -> dict:
    medium, medium_reason = choose_medium(mb, disc, filename_title, tracknumber)
    if medium is None:
        return {"ok": False, "reason": medium_reason}

    tracks = [t for t in medium.get("tracks", []) if isinstance(t, dict)]
    intro_variant = bool(offset == 1 and tracknumber == 1 and intro_like(filename_title or cue_title))
    # A physical intro/HTOA-like file has no corresponding logical release
    # track.  Refuse to borrow MB track 1 rather than shifting every identity.
    if intro_variant:
        return {
            "ok": False,
            "confidence": "AMBIGUOUS",
            "reason": ["leading-intro-unmatched"],
            "medium_id": str(medium.get("id") or ""),
            "medium_position": str(medium.get("position") or ""),
            "medium_reason": medium_reason,
            "intro_variant": True,
        }

    # Evidence hierarchy is deliberate:
    #   1) filename / CUE title
    #   2) duration
    #   3) embedded TITLE (legacy data)
    #   4) track number as a consistency signal only
    filename_title = filename_title or ""
    cue_title = cue_title or ""
    current_title = current_title or ""
    fallback_titles = [current_title] if current_title else []

    candidates: list[tuple[float, dict, list[str]]] = []

    for t in tracks:
        title = str(t.get("title") or (t.get("recording") or {}).get("title") or "")
        number = str(t.get("number") or "")
        length_ms = t.get("length")
        if length_ms is None:
            length_ms = (t.get("recording") or {}).get("length")
        try:
            mb_dur = float(length_ms) / 1000.0 if length_ms is not None else None
        except (ValueError, TypeError, OverflowError):
            continue
        if mb_dur is not None and (not math.isfinite(mb_dur) or mb_dur <= 0):
            continue
        duration_delta = abs(duration - mb_dur) if mb_dur is not None and duration > 0 else None
        candidate_artist = artist_credit(t.get("artist-credit") or (t.get("recording") or {}).get("artist-credit"))
        artist_sim = sim(current_artist, candidate_artist) if current_artist and candidate_artist else None

        filename_sim = sim(filename_title, title) if filename_title else 0.0
        cue_sim = sim(cue_title, title) if cue_title else 0.0
        fallback_best = max([sim(x, title) for x in fallback_titles] or [0.0])
        filename_exact = bool(filename_title and norm(filename_title) == norm(title))
        cue_exact = bool(cue_title and norm(cue_title) == norm(title))
        fallback_exact = bool(current_title and norm(current_title) == norm(title))

        score = 0.0
        reasons: list[str] = []

        if filename_exact:
            score += 1900
            reasons.append("filename-title-exact")
        elif filename_title:
            score += filename_sim * 520
            if filename_sim >= 0.97:
                reasons.append("filename-title-near-exact")
            elif filename_sim >= 0.85:
                reasons.append("filename-title-fuzzy")
            elif filename_sim >= 0.70:
                reasons.append("filename-title-weak")

        if cue_title:
            if not filename_title:
                if cue_exact:
                    score += 900
                    reasons.append("cue-title-exact")
                else:
                    score += cue_sim * 300
                    if cue_sim >= 0.85:
                        reasons.append("cue-title-fuzzy")
            elif cue_exact and filename_sim >= 0.90:
                score += 450
                reasons.append("cue-title-corrob")
            elif cue_sim >= 0.90 and filename_sim >= 0.90:
                score += 180
                reasons.append("cue-title-corrob")
            elif cue_sim < 0.50 and filename_sim >= 0.90:
                reasons.append("cue-title-conflict-ignored")

        if current_title:
            if not filename_title and not cue_title:
                if fallback_exact:
                    score += 700
                    reasons.append("legacy-title-exact")
                elif fallback_best >= 0.85:
                    score += fallback_best * 240
                    reasons.append("legacy-title-fuzzy")
            elif filename_sim >= 0.85 and fallback_best >= 0.85:
                score += 80
                reasons.append("legacy-title-corrob")

        if artist_sim is not None:
            if artist_sim >= 0.90:
                score += 180
                reasons.append("artist-match")
            elif artist_sim >= 0.70:
                score += 70
                reasons.append("artist-compatible")
            elif artist_sim < 0.45:
                score -= 500
                reasons.append("artist-conflict")

        local_markers = version_markers(filename_title or cue_title or current_title)
        remote_markers = version_markers(title)
        if local_markers != remote_markers and (local_markers or remote_markers):
            score -= 900
            reasons.append("version-marker-conflict")

        best_local_title = max(filename_sim, cue_sim)
        if duration_delta is not None:
            if duration_delta <= 1.0:
                score += 320
                reasons.append("dur-match-1s")
            elif duration_delta <= 2.5:
                score += 220
                reasons.append("dur-match-2.5s")
            elif duration_delta <= 5.0:
                score += 110
                reasons.append("dur-match-5s")
            elif duration_delta <= 8.0:
                score += 30
                reasons.append("dur-match-8s")
            elif duration_delta >= 20.0:
                if best_local_title >= 0.90:
                    score -= 120
                    reasons.append("dur-penalized-title-override")
                else:
                    score -= 420
                    reasons.append("dur-mismatch")

        expected_pos = max(1, tracknumber - offset)
        mb_pos = t.get("position")
        raw_num = None
        m_num = re.search(r"\d+", number)
        if m_num:
            raw_num = int(m_num.group(0))

        if raw_num == expected_pos or mb_pos == expected_pos:
            score += 90
            reasons.append("track-pos-exact")

        candidates.append((score, t, reasons))

    candidates.sort(key=lambda x: -x[0])
    if not candidates:
        return {"ok": False, "reason": "no-candidates"}

    best_score, best, reasons = candidates[0]
    second_score = candidates[1][0] if len(candidates) > 1 else -9999.0
    margin = best_score - second_score

    title = str(best.get("title") or (best.get("recording") or {}).get("title") or "")
    length_ms = best.get("length")
    if length_ms is None:
        length_ms = (best.get("recording") or {}).get("length")
    try:
        mb_dur = float(length_ms) / 1000.0 if length_ms is not None else None
    except (ValueError, TypeError):
        mb_dur = None
    delta = abs(duration - mb_dur) if mb_dur is not None else None

    filename_sim = sim(filename_title, title) if filename_title else 0.0
    cue_sim = sim(cue_title, title) if cue_title else 0.0
    fallback_best = max([sim(x, title) for x in fallback_titles] or [0.0])
    best_local_title = max(filename_sim, cue_sim)

    hard_conflicts: list[str] = []
    duration_limit = max(12.0, duration * 0.06) if duration > 0 else 12.0
    if delta is not None and delta > duration_limit:
        hard_conflicts.append("duration-version-conflict")
    if version_markers(filename_title or cue_title or current_title) != version_markers(title):
        if version_markers(filename_title or cue_title or current_title) or version_markers(title):
            hard_conflicts.append("version-marker-conflict")
    candidate_artist = artist_credit(best.get("artist-credit") or (best.get("recording") or {}).get("artist-credit"))
    selected_artist_sim = sim(current_artist, candidate_artist) if current_artist and candidate_artist else None
    if selected_artist_sim is not None and selected_artist_sim < 0.45:
        hard_conflicts.append("artist-conflict")

    ok = False
    if (
        best_score >= 1200
        and margin >= 80
        or best_local_title >= 0.88
        and (margin >= 80 or (delta is not None and delta <= 3.0))
        or best_local_title >= 0.75
        and (delta is not None and delta <= 2.0)
        and margin >= 50
        or not filename_title
        and not cue_title
        and fallback_best >= 0.90
        and (delta is not None and delta <= 3.0)
    ):
        ok = True

    if not ok and filename_sim >= 0.90 and delta is not None and delta <= 2.0:
        tied_primary = [
            c
            for c in candidates
            if sim(filename_title, str(c[1].get("title") or (c[1].get("recording") or {}).get("title") or ""))
            >= filename_sim - 0.01
        ]
        ok = len(tied_primary) == 1
    if margin < 50:
        hard_conflicts.append("ambiguous-release-track")
    if hard_conflicts:
        ok = False

    recording = best.get("recording") or {}
    recording_ac = best.get("artist-credit") or recording.get("artist-credit") or []
    artist_ids = []
    artist_names = []
    artist_sorts = []
    for credit in recording_ac:
        artist = credit.get("artist") or {}
        aid = artist.get("id")
        name = credit.get("name") or artist.get("name") or ""
        sort_name = artist.get("sort-name") or ""
        if aid and aid not in artist_ids:
            artist_ids.append(aid)
        if name and name not in artist_names:
            artist_names.append(name)
        if sort_name and sort_name not in artist_sorts:
            artist_sorts.append(sort_name)
    isrcs = [
        str(x.get("id") or "") if isinstance(x, dict) else str(x)
        for x in (recording.get("isrcs") or [])
        if x and (isinstance(x, str) or x.get("id"))
    ]
    best_title_sim = best_local_title if (filename_title or cue_title) else fallback_best
    return {
        "ok": ok,
        "confidence": "EXACT" if ok and best_title_sim >= 0.97 else ("HIGH" if ok else "AMBIGUOUS"),
        "reason": reasons,
        "medium_id": str(medium.get("id") or ""),
        "medium_position": str(medium.get("position") or ""),
        "medium_reason": medium_reason,
        "track_id": str(best.get("id") or ""),
        "track_number": str(best.get("number") or ""),
        "track_position": str(best.get("position") or ""),
        "title": title,
        "artist_credit": artist_credit(best.get("artist-credit") or recording.get("artist-credit")),
        "artist_ids": artist_ids,
        "artist_names": artist_names,
        "artist_sorts": artist_sorts,
        "isrcs": isrcs,
        "recording_id": str(recording.get("id") or ""),
        "duration_seconds": mb_dur,
        "duration_delta_seconds": delta,
        "title_similarity": round(best_title_sim, 4),
        "score": round(best_score, 2),
        "score_margin": round(best_score - second_score, 2),
        "hard_conflicts": hard_conflicts,
        "artist_similarity": round(selected_artist_sim, 4) if selected_artist_sim is not None else None,
        "intro_variant": intro_variant,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mb_json")
    ap.add_argument("disc", type=int)
    ap.add_argument("tracknumber", type=int)
    ap.add_argument("filename_title")
    ap.add_argument("current_title")
    ap.add_argument("current_artist")
    ap.add_argument("duration", type=float)
    ap.add_argument("--cue-title", default="")
    ap.add_argument("--offset", type=int, default=0, help="physical-to-logical track-number offset")
    args = ap.parse_args()

    mb = json.loads(Path(args.mb_json).read_text(encoding="utf-8"))
    result = resolve_mb_track(
        mb=mb,
        disc=args.disc,
        tracknumber=args.tracknumber,
        filename_title=args.filename_title,
        current_title=args.current_title,
        current_artist=args.current_artist,
        duration=args.duration,
        cue_title=args.cue_title,
        offset=args.offset,
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
