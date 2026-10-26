#!/usr/bin/env python3
"""Strict multi-tier verifier and scorer for a local CD identity against one MusicBrainz release.

Adheres to MusicBrainz Release & Release Group data models, Style Guidelines,
and audiophile identification hierarchies (Steve Hoffman Music Forums, Hydrogenaudio).
"""

from __future__ import annotations

import difflib
import json
import math
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any


def rank_acoustid_results(payload: object, minimum_score: float) -> list[dict[str, Any]]:
    """Keep confident finite fingerprint hits without trusting provider order."""
    if not isinstance(payload, dict) or payload.get("status") != "ok" or not isinstance(payload.get("results"), list):
        return []
    ranked: list[dict[str, Any]] = []
    for row in payload["results"]:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        raw_score = row.get("score")
        if not isinstance(raw_score, (float, int, str)) or isinstance(raw_score, bool):
            continue
        try:
            score = float(raw_score)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(score) and minimum_score <= score <= 1.0:
            ranked.append({**row, "score": score})
    return sorted(ranked, key=lambda row: (-row["score"], str(row["id"])))


def norm(value: object) -> str:
    return re.sub(r"[^\w]+", "", unicodedata.normalize("NFC", str(value or "")).casefold(), flags=re.UNICODE)


def catnorm(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def barnorm_set(value: object) -> set[str]:
    raw = re.sub(r"[^0-9xX]+", "", str(value or "")).upper()
    if not raw:
        return set()
    res = {raw}
    if len(raw) == 11:
        res.add("0" + raw)
        res.add("00" + raw)
    elif len(raw) == 12:
        res.add("0" + raw)
        if raw.startswith("0"):
            res.add(raw[1:])
    elif len(raw) == 13 and raw.startswith("0"):
        res.add(raw[1:])
        if raw.startswith("00"):
            res.add(raw[2:])
    return res


def sim(a: object, b: object) -> float:
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def clean_title_for_compare(title: object) -> str:
    t = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", "", str(title or "")).strip()
    return t if t else str(title or "").strip()


def track_title_sim(a: object, b: object) -> float:
    sa, sb = str(a or ""), str(b or "")
    s1 = sim(sa, sb)
    ca, cb = clean_title_for_compare(sa), clean_title_for_compare(sb)
    s2 = sim(ca, cb)
    best = max(s1, s2)
    na, nb = norm(ca), norm(cb)
    if na and nb:
        if na == nb:
            return 1.0
        if len(na) >= 4 and len(nb) >= 4 and (na in nb or nb in na):
            best = max(best, 0.88)
    return best


def compare_tracklists(local_list: list[str], mb_list: list[str]) -> tuple[bool, bool]:
    if not local_list or not mb_list or len(local_list) != len(mb_list):
        return False, False
    sims = [track_title_sim(l, m) for l, m in zip(local_list, mb_list)]
    exact = all(s >= 0.95 for s in sims)
    avg_sim = sum(sims) / len(sims)
    matched_ratio = sum(1 for s in sims if s >= 0.75) / len(sims)
    compatible = (matched_ratio >= 0.80 and avg_sim >= 0.80) or all(s >= 0.70 for s in sims)
    return exact, compatible


def dur_disc(value: object) -> float | None:
    if value is None:
        return None
    s = str(value).strip()
    m = re.fullmatch(r"(?:(\d+):)?(\d{1,2})(?::(\d{1,2}))?(?:[.,](\d+))?", s)
    if not m:
        return None
    frac = float("0." + (m.group(4) or "0"))
    if m.group(3) is not None:
        hours = int(m.group(1) or 0)
        minutes = int(m.group(2))
        seconds = int(m.group(3))
        return hours * 3600 + minutes * 60 + seconds + frac
    minutes = int(m.group(1) or 0)
    seconds = int(m.group(2))
    return minutes * 60 + seconds + frac


def dur_mb(value: str | float | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return None


def artist_credit(ac: list[dict] | None) -> str:
    return "".join(
        (c.get("name") or (c.get("artist") or {}).get("name") or "") + (c.get("joinphrase") or "") for c in ac or []
    )


def title_of(track: dict) -> str:
    return str(track.get("title") or (track.get("recording") or {}).get("title") or "")


def intro_like(title: str) -> bool:
    n = norm(title)
    return any(k in n for k in ("intro", "introduction", "prelude", "overture", "prologue", "opening"))


COUNTRY_MAP: dict[str, set[str]] = {
    "de": {"de", "xg", "germany", "west germany", "deutschland"},
    "xg": {"de", "xg", "germany", "west germany", "deutschland"},
    "gb": {"gb", "uk", "united kingdom", "great britain", "england"},
    "uk": {"gb", "uk", "united kingdom", "great britain", "england"},
    "us": {"us", "united states", "usa", "america"},
    "jp": {"jp", "japan"},
    "nl": {"nl", "netherlands", "holland"},
    "fr": {"fr", "france"},
    "it": {"it", "italy"},
    "ca": {"ca", "canada"},
    "au": {"au", "australia"},
    "xe": {"xe", "europe", "european union"},
    "xw": {"xw", "worldwide", "world"},
}
EUROPE_COUNTRIES = {
    "de",
    "xg",
    "gb",
    "uk",
    "nl",
    "fr",
    "it",
    "es",
    "se",
    "no",
    "dk",
    "fi",
    "be",
    "ch",
    "at",
    "pl",
    "ie",
    "pt",
    "gr",
    "cz",
    "hu",
}


def evaluate_identity(
    D: dict,
    M: dict,
    wcountry: str = "",
    wcat: str = "",
    wbarcode: str = "",
    wyear: str = "",
    discid_exact: bool = False,
    toc: dict | None = None,
    url_relation: bool = False,
    acoustid_recordings: dict[int, set[str]] | None = None,
    acoustid_position_recordings: dict[tuple[int, int], set[str]] | None = None,
) -> dict:
    """Evaluate and score a candidate MusicBrainz release against Discogs, TOC, and AcoustID data."""
    # --- Discogs side extraction ---
    discogs_id = str(D.get("id") or "")
    dtitle = str(D.get("title") or "")
    dartist = ";".join(str(a.get("name") or "") for a in D.get("artists") or [])

    dcats_raw = {str(x.get("catno") or "") for x in D.get("labels") or [] if x.get("catno")}
    dcats = {catnorm(x) for x in dcats_raw if catnorm(x)}
    dlabels = {norm(x.get("name")) for x in D.get("labels") or [] if x.get("name")}

    dbars = set()
    for x in D.get("identifiers") or []:
        if str(x.get("type", "")).casefold() == "barcode" and x.get("value"):
            dbars |= barnorm_set(x.get("value"))
    wbars = barnorm_set(wbarcode) if wbarcode else set()

    matrix_values = [
        str(x.get("value") or "") for x in D.get("identifiers") or [] if "matrix" in str(x.get("type", "")).casefold()
    ]
    discogs_notes = str(D.get("notes") or "")

    discogs_tracks = [
        (title_of(x), dur_disc(x.get("duration")))
        for x in D.get("tracklist") or []
        if isinstance(x, dict) and x.get("title") and (x.get("type_") or x.get("type") or "track") == "track"
    ]
    dtracks = discogs_tracks

    # --- MusicBrainz side extraction ---
    mtitle = str(M.get("title") or "")
    martist = artist_credit(M.get("artist-credit"))
    mcountry = str(M.get("country") or "")
    myear = str(M.get("date") or "")[:4]

    mcats_raw = {
        str(x.get("catalog-number") or (x.get("catalog") or {}).get("number") or "")
        for x in M.get("label-info") or []
        if x.get("catalog-number") or (x.get("catalog") or {}).get("number")
    }
    mcats = {catnorm(x) for x in mcats_raw if catnorm(x)}

    mlabels = {
        norm((x.get("label") or {}).get("name"))
        for x in M.get("label-info") or []
        if (x.get("label") or {}).get("name")
    }

    mbars = set()
    if M.get("barcode"):
        mbars |= barnorm_set(M.get("barcode"))
    for x in M.get("identifiers") or []:
        if str(x.get("type", "")).casefold() == "barcode" and x.get("value"):
            mbars |= barnorm_set(x.get("value"))

    # Check direct Discogs URL relations inside MB JSON
    mb_discogs_relations: set[str] = set()
    for rel in M.get("relations") or []:
        if isinstance(rel, dict):
            url_obj = rel.get("url") or {}
            res = str(url_obj.get("resource") or rel.get("target") or "")
            m = re.search(r"discogs\.com/release/(\d+)", res)
            if m:
                mb_discogs_relations.add(m.group(1))

    if discogs_id and discogs_id in mb_discogs_relations:
        url_relation = True

    # --- Matching Flags ---
    # Barcode matching (12/13 digits normalized)
    barcode_exact = bool(wbars and (wbars & mbars)) or bool(dbars and mbars and (dbars & mbars))

    # Catalog matching (exact or prefix/core substring)
    wcat_norm = catnorm(wcat)
    target_cats = {wcat_norm} if wcat_norm else dcats
    catno_exact = bool(target_cats and any(c in mcats for c in target_cats))
    catno_partial = False
    if not catno_exact and target_cats and mcats:
        for tc in target_cats:
            if len(tc) >= 5 and any(tc in mc or mc in tc for mc in mcats if len(mc) >= 5):
                catno_partial = True
                break

    # Label matching
    label_exact = bool(
        dlabels and mlabels and any(a == b or (len(a) >= 3 and (a in b or b in a)) for a in dlabels for b in mlabels)
    )

    # Country matching with regional mapping (XE Europe, XW Worldwide, DE/XG Germany)
    wc_key = norm(wcountry)
    mc_key = norm(mcountry)

    country_exact = False
    country_compatible = False

    if wc_key and mc_key:
        if wc_key == mc_key:
            country_exact = True
        else:
            # Check alias groups
            target_group = None
            for k, aliases in COUNTRY_MAP.items():
                if wc_key in aliases or wc_key == k:
                    target_group = k
                    break
            cand_group = None
            for k, aliases in COUNTRY_MAP.items():
                if mc_key in aliases or mc_key == k:
                    cand_group = k
                    break

            if target_group and cand_group and target_group == cand_group:
                country_exact = True
            elif (
                cand_group == "xw"
                or target_group == "xw"
                or (target_group in EUROPE_COUNTRIES and cand_group == "xe")
                or (target_group == "xe" and cand_group in EUROPE_COUNTRIES)
            ):
                country_compatible = True

    # Year matching
    year_exact = bool(wyear and myear == wyear)
    year_close = False
    if not year_exact and wyear and myear and wyear.isdigit() and myear.isdigit():
        year_close = abs(int(wyear) - int(myear)) <= 2

    # Concept matching (Title & Artist)
    title_ratio = sim(mtitle, dtitle)
    artist_ratio = sim(martist, dartist)
    title_exact = title_ratio >= 0.95
    title_similar = title_ratio >= 0.70
    artist_exact = artist_ratio >= 0.90
    artist_similar = artist_ratio >= 0.65
    d_artist_key = norm(dartist)
    m_artist_key = norm(martist)
    various_aliases = {"various", "variousartists", "va"}
    various_exception = d_artist_key in various_aliases and m_artist_key in various_aliases
    is_classical = any(str(value).casefold() == "classical" for value in (D.get("genres") or []))
    classical_exception = bool(
        is_classical
        and title_similar
        and (barcode_exact or url_relation or discid_exact or (catno_exact and label_exact))
    )
    artist_exception = various_exception or classical_exception

    # Disambiguation & Matrix matching
    m_disambig = str(M.get("disambiguation") or "").casefold()
    disambiguation_match = False
    if m_disambig:
        keywords = re.findall(r"[a-z0-9]{4,}", m_disambig)
        haystack = (discogs_notes + " " + " ".join(matrix_values)).casefold()
        common_words = {
            "with",
            "from",
            "disc",
            "album",
            "version",
            "edition",
            "reissue",
            "original",
            "bonus",
            "tracks",
            "release",
            "press",
            "pressing",
        }
        matched_kws = [kw for kw in keywords if kw not in common_words and kw in haystack]
        if matched_kws:
            disambiguation_match = True

    flags: dict[str, bool | int] = {
        "barcode_exact": barcode_exact,
        "catno_exact": catno_exact,
        "catno_partial": catno_partial,
        "label_exact": label_exact,
        "country_exact": country_exact,
        "country_compatible": country_compatible,
        "year_exact": year_exact,
        "year_close": year_close,
        "title_exact": title_exact,
        "title_similar": title_similar,
        "artist_exact": artist_exact,
        "artist_similar": artist_similar,
        "artist_exception": artist_exception,
        "disambiguation_match": disambiguation_match,
        "url_relation": url_relation,
        "discid_query_match": discid_exact,
    }

    # A lookup hit is not physical evidence when the local TOC was synthesized.
    authentic_toc = bool(toc and toc.get("toc_exact") is True and not toc.get("is_partial"))
    discid_exact = bool(discid_exact and authentic_toc)
    flags["discid_query_match"] = discid_exact

    # --- Medium selection and TOC layout analysis ---
    media = [m for m in M.get("media") or [] if isinstance(m, dict)]
    selected: dict | None = None
    medium_discid_exact = False

    def medium_disc_ids(medium: dict) -> list[str]:
        ids: list[str] = []
        for key in ("discs", "discids", "disc-list", "disc_list"):
            val = medium.get(key)
            if isinstance(val, list):
                for it in val:
                    if isinstance(it, str):
                        ids.append(it)
                    elif isinstance(it, dict) and it.get("id"):
                        ids.append(str(it["id"]))
        return ids

    if toc and toc.get("discid"):
        local_discid = str(toc.get("discid"))
        for medium in media:
            if local_discid in medium_disc_ids(medium):
                selected = medium
                medium_discid_exact = authentic_toc
                break
    else:
        local_discid = ""

    local_count = int((toc or {}).get("track_count") or 0)
    local_durs = [float(x) for x in ((toc or {}).get("toc_durations") or [])]
    local_titles = [str(x or "") for x in ((toc or {}).get("toc_titles") or [])]

    if selected is None and media:
        ranked: list[tuple[float, dict]] = []
        for medium in media:
            tracks = [x for x in medium.get("tracks") or [] if isinstance(x, dict)]
            titles = [title_of(t) for t in tracks]
            score_m = 0.0
            if local_count and len(tracks) == local_count:
                score_m += 1000
            elif local_count and len(tracks) + 1 == local_count and local_titles and intro_like(local_titles[0]):
                tail = local_titles[1:]
                if len(tail) == len(titles) and all(sim(a, b) >= 0.82 for a, b in zip(tail, titles)):
                    score_m += 900
            if local_titles and len(tracks) == len(local_titles):
                score_m += 300 * sum(sim(a, b) for a, b in zip(local_titles, titles)) / max(1, len(titles))
            fmt = str(
                (medium.get("format") or {}).get("name")
                if isinstance(medium.get("format"), dict)
                else medium.get("format") or ""
            ).casefold()
            if "cd" in fmt:
                score_m += 50
            ranked.append((score_m, medium))
        ranked.sort(key=lambda item: (-item[0], str(item[1].get("id") or "")))
        if ranked and ranked[0][0] > 0:
            selected = ranked[0][1]

    mtracks: list[tuple[str, float | None]] = []
    mrecordings: list[str] = []
    format_cd = False
    if selected:
        fmt_name = str(
            (selected.get("format") or {}).get("name")
            if isinstance(selected.get("format"), dict)
            else selected.get("format") or ""
        ).casefold()
        format_cd = "cd" in fmt_name or "sacd" in fmt_name
        for track in selected.get("tracks") or []:
            if not isinstance(track, dict):
                continue
            title = title_of(track)
            if not title:
                continue
            length = track.get("length")
            if length is None:
                length = (track.get("recording") or {}).get("length")
            mtracks.append((title, dur_mb(length)))
            rec_obj = track.get("recording") or {}
            mrecordings.append(str(rec_obj.get("id") or "").strip())

    flags["format_cd"] = format_cd
    flags["medium_discid_exact"] = medium_discid_exact
    flags["medium_selected"] = selected is not None
    flags["medium_track_count_exact"] = bool(local_count and local_count == len(mtracks))
    flags["leading_intro_variant"] = False
    flags["logical_track_titles_exact"] = False
    flags["logical_track_titles_compatible"] = False
    flags["toc_duration_compatible"] = False
    flags["toc_duration_strong"] = False
    flags["toc_duration_conflict"] = False
    flags["physical_layout_conflict"] = False
    flags["toc_track_count_conflict"] = False
    flags["acoustid_audio_match"] = False
    flags["acoustid_audio_mismatch"] = False
    flags["acoustid_match_count"] = 0

    if acoustid_position_recordings or (acoustid_recordings and mrecordings):
        matched_rec = 0
        checked_rec = 0
        mismatched_rec = 0
        mapped: list[tuple[str, set[str]]] = []
        if acoustid_position_recordings:
            remote_positions: dict[tuple[int, int], str] = {}
            for medium in media:
                for track in medium.get("tracks") or []:
                    try:
                        position = (int(medium.get("position") or 0), int(track.get("position") or 0))
                    except (TypeError, ValueError):
                        continue
                    if min(position) > 0:
                        remote_positions[position] = str((track.get("recording") or {}).get("id") or "")
            mapped = [
                (remote_positions.get(position, ""), ids) for position, ids in acoustid_position_recordings.items()
            ]
        elif acoustid_recordings:
            # Legacy API accepts indices into the selected medium only.
            mapped = [(rec_id, acoustid_recordings.get(idx, set())) for idx, rec_id in enumerate(mrecordings)]
        for rec_id, acoust_cand in mapped:
            if acoust_cand:
                checked_rec += 1
                if rec_id and rec_id in acoust_cand:
                    matched_rec += 1
                elif rec_id:
                    mismatched_rec += 1
        flags["acoustid_match_count"] = matched_rec
        flags["acoustid_checked_count"] = checked_rec
        flags["acoustid_mismatch_count"] = mismatched_rec
        # One shared recording is not edition evidence.  Require useful
        # complete mapped coverage and reject mixed positive/negative evidence.
        if checked_rec >= 2 and matched_rec == checked_rec and mismatched_rec == 0:
            flags["acoustid_audio_match"] = True
        elif checked_rec > 0 and mismatched_rec > 0:
            flags["acoustid_audio_mismatch"] = True

    toc_is_authentic = bool(toc and (toc.get("toc_exact") is True) and not toc.get("is_partial"))

    map_offset = 0
    if toc_is_authentic:
        if local_count and len(mtracks) == local_count and len(local_titles) >= local_count:
            flags["logical_track_titles_exact"], flags["logical_track_titles_compatible"] = compare_tracklists(
                local_titles[:local_count], [m[0] for m in mtracks]
            )
        elif local_count == len(mtracks) + 1 and local_titles and intro_like(local_titles[0]):
            tail = local_titles[1:]
            if len(tail) == len(mtracks):
                flags["logical_track_titles_exact"], flags["logical_track_titles_compatible"] = compare_tracklists(
                    tail, [m[0] for m in mtracks]
                )
            if flags["logical_track_titles_compatible"]:
                flags["leading_intro_variant"] = True
                map_offset = 1
        else:
            flags["physical_layout_conflict"] = bool(local_count and mtracks and local_count != len(mtracks))

    duration_deltas: list[float] = []
    if toc_is_authentic and mtracks and local_durs:
        pairs = (
            list(zip(local_durs, mtracks))
            if map_offset == 0 and len(local_durs) == len(mtracks)
            else (list(zip(local_durs[1:], mtracks)) if map_offset == 1 and len(local_durs) == len(mtracks) + 1 else [])
        )
        for ld, (_, md) in pairs:
            if md is not None:
                duration_deltas.append(abs(ld - md))
        if duration_deltas and len(duration_deltas) == len(mtracks):
            max_delta = max(duration_deltas)
            total_delta = abs(sum(ld for ld, _ in pairs) - sum(md for _, (_, md) in pairs if md is not None))
            per_track_limit = 6.0 if not map_offset else 8.0
            total_limit = 15.0 if not map_offset else 25.0
            other_deltas = duration_deltas[:-1]
            if other_deltas and max(other_deltas) <= 2.0:
                flags["toc_duration_compatible"] = duration_deltas[-1] <= 10.0 and total_delta <= total_limit
                flags["toc_duration_strong"] = duration_deltas[-1] <= 6.0 and total_delta <= 10.0
            else:
                flags["toc_duration_compatible"] = max_delta <= per_track_limit and total_delta <= total_limit
                flags["toc_duration_strong"] = max_delta <= 2.0 and total_delta <= 5.0
            flags["toc_duration_conflict"] = not flags["toc_duration_compatible"]
        flags["toc_track_count_conflict"] = bool(
            local_count and mtracks and local_count != len(mtracks) and not flags["leading_intro_variant"]
        )
    elif not toc_is_authentic and local_titles and mtracks:
        mb_titles_norm = [norm(m[0]) for m in mtracks]
        matched_count = sum(1 for lt in local_titles if norm(lt) in mb_titles_norm)
        if matched_count == len(local_titles) and matched_count > 0:
            flags["logical_track_titles_compatible"] = True

    reasons: list[str] = []
    if toc_is_authentic and (flags["physical_layout_conflict"] or flags["toc_track_count_conflict"]):
        reasons.append("physical-layout")
    if toc_is_authentic and flags["toc_duration_conflict"] and duration_deltas and not flags["leading_intro_variant"]:
        reasons.append("toc-duration-conflict")
    # Album title and credited artist identify a concept jointly.  Accepting
    # either one lets same-title releases or an artist's different release pass.
    if not title_similar or (not artist_similar and not artist_exception):
        reasons.append("title-artist-mismatch")
    target_barcodes = wbars or dbars
    if target_barcodes and mbars and not barcode_exact:
        reasons.append("barcode-conflict")
    if target_cats and mcats and not (catno_exact or catno_partial):
        reasons.append("catalog-conflict")
    if wc_key and mc_key and not (country_exact or country_compatible):
        reasons.append("country-conflict")
    if wyear and myear and wyear.isdigit() and myear.isdigit() and abs(int(wyear) - int(myear)) > 2:
        reasons.append("edition-date-conflict")
    if flags["acoustid_audio_mismatch"]:
        reasons.append("acoustid-recording-conflict")

    format_parts: list[str] = []
    for fmt in D.get("formats") or []:
        if isinstance(fmt, dict):
            format_parts.append(str(fmt.get("name") or ""))
            format_parts.extend(str(x) for x in fmt.get("descriptions") or [])
    discogs_formats = " ".join(format_parts).casefold()
    mb_format = str(
        ((selected or {}).get("format") or {}).get("name")
        if isinstance((selected or {}).get("format"), dict)
        else ((selected or {}).get("format") or "")
    ).casefold()
    wants_cd = bool(re.search(r"\b(?:cd|sacd)\b", discogs_formats))
    wants_vinyl = bool(re.search(r"\b(?:vinyl|lp)\b", discogs_formats))
    mb_is_cd = "cd" in mb_format or "sacd" in mb_format
    mb_is_vinyl = any(x in mb_format for x in ("vinyl", '12"', '10"', '7"'))
    flags["medium_format_compatible"] = not ((wants_cd and not mb_is_cd) or (wants_vinyl and not mb_is_vinyl))
    if not flags["medium_format_compatible"]:
        reasons.append("medium-format-conflict")

    rejected = bool(reasons)

    # --- Multi-Tier Quantitative Scoring (Feature Hierarchy) ---
    score = 0
    if not rejected:
        # Tier 1: Physical Medium & TOC Layout (Highest Specificity)
        if medium_discid_exact:
            score += 1000
        if flags["toc_duration_strong"] and flags["medium_track_count_exact"]:
            score += 400
        elif flags["toc_duration_compatible"]:
            score += 200
        if flags["logical_track_titles_exact"]:
            score += 150
        elif flags["logical_track_titles_compatible"]:
            score += 80

        # Tier 2: Direct Cross-Database Relations (MusicBrainz Official Relations)
        if flags["url_relation"]:
            score += 500

        # Tier 2.5: Acoustic Fingerprint Confirmation (AcoustID)
        if flags["acoustid_audio_match"]:
            score += 250

        # Tier 3: Industrial Product Identifiers (Barcodes, Catalogs, Labels)
        if flags["barcode_exact"]:
            score += 350
        if flags["catno_exact"]:
            score += 250
        elif flags["catno_partial"]:
            score += 150
        if flags["label_exact"]:
            score += 100

        # Tier 4: Provenance & Release Details (Country, Pressing Year, Packaging)
        if flags["country_exact"]:
            score += 150
        elif flags["country_compatible"]:
            score += 80
        if flags["year_exact"]:
            score += 120
        elif flags["year_close"]:
            score += 60
        if flags["format_cd"]:
            score += 80

        # Tier 5: Matrix, Pressing Plant & Disambiguation Notes
        if flags["disambiguation_match"]:
            score += 100

        # Tier 6: Conceptual Alignment (Title & Artist)
        if flags["title_exact"]:
            score += 100
        elif flags["title_similar"]:
            score += 50
        if flags["artist_exact"]:
            score += 100
        elif flags["artist_similar"]:
            score += 50

    # Classification based on Hierarchy & Score
    if rejected:
        level, confidence, basis = 0, "REJECT", "+".join(reasons)
    elif medium_discid_exact:
        level, confidence, basis = 7, "CERTAIN", "disc-id+exact-medium"
    elif (
        flags["url_relation"]
        and flags["barcode_exact"]
        and flags["catno_exact"]
        and (flags["country_exact"] or flags["country_compatible"])
        and (flags["toc_duration_compatible"] or not local_durs)
    ):
        level, confidence, basis = 7, "CERTAIN", "discogs-mb-relation+edition-ids"
    elif (
        (discid_exact or medium_discid_exact)
        and flags["medium_track_count_exact"]
        and flags["logical_track_titles_compatible"]
        and (flags["toc_duration_strong"] or not local_durs)
    ):
        level, confidence, basis = 6, "EXACT", "disc-id+medium-layout+toc"
    elif (
        (discid_exact or medium_discid_exact)
        and flags["leading_intro_variant"]
        and flags["logical_track_titles_compatible"]
    ):
        level, confidence, basis = 6, "EXACT", "disc-id+leading-intro-variant"
    elif (
        flags["url_relation"]
        and (flags["barcode_exact"] or flags["catno_exact"])
        and (flags["country_exact"] or flags["country_compatible"])
        and (flags["toc_duration_compatible"] or not local_durs)
    ):
        level, confidence, basis = 6, "EXACT", "discogs-mb-relation+identifiers"
    elif (
        flags["barcode_exact"]
        and flags["catno_exact"]
        and (flags["country_exact"] or flags["country_compatible"])
        and (flags["year_exact"] or flags["year_close"])
        and flags["logical_track_titles_compatible"]
        and (flags["toc_duration_compatible"] or not local_durs)
    ):
        level, confidence, basis = 5, "VERY_HIGH", "barcode+catalog+country+year+logical-layout"
    elif flags["url_relation"] and flags["logical_track_titles_compatible"]:
        level, confidence, basis = 5, "VERY_HIGH", "discogs-mb-relation+catalog+country+logical-layout"
    elif (
        (flags["barcode_exact"] or flags["catno_exact"])
        and (flags["country_exact"] or flags["country_compatible"])
        and flags["logical_track_titles_compatible"]
    ):
        level, confidence, basis = 4, "HIGH", "identifiers+country+logical-layout"
    elif (flags["barcode_exact"] or flags["catno_exact"]) and flags["logical_track_titles_compatible"]:
        level, confidence, basis = 3, "MEDIUM", "catalog-or-barcode+logical-layout"
    elif (
        flags["catno_exact"]
        and (flags["country_exact"] or flags["country_compatible"])
        and (flags["year_exact"] or flags["year_close"])
    ):
        level, confidence, basis = 2, "LOW", "catalog+country+year"
    elif flags["title_exact"] and flags["artist_exact"]:
        if score >= 1400:
            level, confidence, basis = 4, "HIGH", "score-alignment"
        elif score >= 1000:
            level, confidence, basis = 3, "MEDIUM", "score-alignment"
        else:
            level, confidence, basis = 1, "LOW", "title+artist"
    else:
        if score >= 1200:
            level, confidence, basis = 4, "HIGH", "score-alignment"
        elif score >= 800:
            level, confidence, basis = 3, "MEDIUM", "score-alignment"
        else:
            level, confidence, basis = 0, "NONE", "none"

    # Acoustic verification upgrade
    if not rejected and flags.get("acoustid_audio_match"):
        if level < 5 and (flags["logical_track_titles_compatible"] or flags["title_exact"] or flags["catno_exact"]):
            level = max(level, 5)
            confidence = "VERY_HIGH"
            basis += "+acoustid-audio-match"
        elif level >= 5:
            basis += "+acoustid-match"

    discogs_duration_deltas: list[float] = []
    if dtracks and mtracks and len(dtracks) == len(mtracks):
        for (_, dd), (_, md) in zip(dtracks, mtracks):
            if dd is not None and md is not None:
                discogs_duration_deltas.append(abs(dd - md))

    toc_max_delta = max(duration_deltas) if duration_deltas else None
    discogs_max_delta = max(discogs_duration_deltas) if discogs_duration_deltas else None

    return {
        "score": score,
        "level": level,
        "confidence": confidence,
        "confidence_basis": basis,
        "rejected": rejected,
        "reason": [k for k, v in flags.items() if v],
        "flags": flags,
        "url_exact": url_relation,
        "discid_exact": discid_exact,
        "local_discid": local_discid,
        "local_toc_track_count": local_count,
        "medium_id": str((selected or {}).get("id") or ""),
        "medium_position": str((selected or {}).get("position") or ""),
        "medium_format": str(
            ((selected or {}).get("format") or {}).get("name")
            if isinstance((selected or {}).get("format"), dict)
            else ((selected or {}).get("format") or "")
        ),
        "toc_duration_max_delta_seconds": round(toc_max_delta, 3) if toc_max_delta is not None else None,
        "discogs_duration_max_delta_seconds": round(discogs_max_delta, 3) if discogs_max_delta is not None else None,
        "toc_segmentation": "leading physical Intro track -> MB logical track 1"
        if flags["leading_intro_variant"]
        else ("1:1" if mtracks else ""),
        "discogs_total_seconds": round(sum(x for _, x in dtracks if x is not None), 3) if dtracks else None,
        "mb_total_seconds": round(sum(x for _, x in mtracks if x is not None), 3) if mtracks else None,
        "mb_country": mcountry,
        "mb_year": myear,
        "mb_title": mtitle,
        "mb_artist": martist,
        "mb_barcode": next(iter(mbars), ""),
        "mb_catnos": sorted(mcats),
        "track_count_mb": len(mtracks),
        "track_count_discogs": len(dtracks),
        "hard_conflicts": reasons,
    }


def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    if argv in (["-h"], ["--help"]):
        print(
            "usage: mb_identity_score.py DISCOGS_JSON MB_JSON COUNTRY CATNO BARCODE YEAR "
            "[DISC_ID_EXACT] [TOC_JSON] [URL_RELATION] [ACOUSTID_JSON]"
        )
        return
    if len(argv) not in (6, 7, 8, 9, 10):
        raise SystemExit(
            "usage: mb_identity_score.py DISCOGS_JSON MB_JSON COUNTRY CATNO BARCODE YEAR "
            "[DISC_ID_EXACT] [TOC_JSON] [URL_RELATION] [ACOUSTID_JSON]"
        )

    D = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    M = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    wcountry, wcat, wbarcode, wyear = argv[2:6]
    discid_exact = len(argv) >= 7 and argv[6] == "1"
    toc = None
    if len(argv) >= 8 and argv[7]:
        try:
            toc = json.loads(Path(argv[7]).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            toc = None
    url_relation = len(argv) >= 9 and argv[8] == "1"
    acoustid_recordings = None
    if len(argv) >= 10 and argv[9]:
        try:
            p = Path(argv[9])
            if p.is_file():
                acoustid_recordings = json.loads(p.read_text(encoding="utf-8"))
            else:
                acoustid_recordings = json.loads(argv[9])
        except (OSError, json.JSONDecodeError):
            acoustid_recordings = None

    result = evaluate_identity(
        D=D,
        M=M,
        wcountry=wcountry,
        wcat=wcat,
        wbarcode=wbarcode,
        wyear=wyear,
        discid_exact=discid_exact,
        toc=toc,
        url_relation=url_relation,
        acoustid_recordings=acoustid_recordings,
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
