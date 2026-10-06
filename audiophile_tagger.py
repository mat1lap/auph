#!/usr/bin/env python3
"""AudiophileArchTagger 6.0 (High-Performance Multithreaded Engine).

Design goals:
  * Discogs release is the physical truth; MusicBrainz provides linked acoustic IDs.
  * Pre-emphasis checked via CUE and affirmative log parsing without false positives.
  * Native parallel DR14 measurement and concurrent multi-provider lyrics resolution.
  * In-process Mutagen Vorbis tag writing with strict Xiph/Vorbis compliance.
  * Multi-provider cover fetcher (Apple Music 3000px, CAA, Deezer, Discogs).
  * ReplayGain 2.0 (EBU R128 via loudgain) and post-tagging canonical verification.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass


import numpy as np
import requests
from mutagen import MutagenError
from mutagen.flac import FLAC

import album_transaction

# In-process pipeline imports
import audiophile_cover_fetcher
import discogs_release_find
import dr14_parse
import library_layout
import loudness
import lyrics_pipeline
import lyrics_utils
import mb_discid_from_cue
import mb_identity_score
import mb_track_match
import metadata_writer
import release_match
from album_completeness import assess_album_completeness
from album_lock import exclusive_album
from http_sessions import isolated_worker_session
from library_files import iter_library_flacs
from pipeline_config import load_config
from provider_cache import DISCOGS_LIMITER, MUSICBRAINZ_LIMITER, JsonCache, fetch_json

CURATED_ALBUMS = [
    "2608459:::Queen/A Day At The Races (1976-12-10):::EMI CDP 7 46208 2, W.Germany",
    "3736884:::Queen/A Kind Of Magic (1986-06-02):::EMI-Toshiba CP32-5152, Japan",
    "3894462:::Queen/A Night At The Opera (1975-11-21):::2000, DCC Compact Classics, GZS-1144",
    "2467591:::Queen/Innuendo (1991-01-14):::Parlophone CDP 79 5887 2 Holland",
    "2284136:::Queen/Jazz (1978-11-10):::1986, West Germany, CDP 7 46210 2",
    "367690:::Queen/Made In Heaven (1995-10-24):::CDPCSD 167",
    "3872011:::Queen/News Of The World = 世界に捧ぐ (1977-10-28):::1986, Japan 1st Press, 32XD-409",
    "14332265:::Queen/Queen II (1974-04-09):::1992, UK, EMI UK-CD-FA 3099",
    "1777269:::Queen/Sheer Heart Attack (1974-11-01):::EMI CDP 7 46206 2, Holland",
    "2370094:::Queen/The Game (1980-06-24):::MFSL UDCD-610",
    "5104254:::Queen/The Works (1984-02-27):::1989, West Germany, CDP 7 46016 2",
]


class RateLimiter:
    """Thread-safe rate limiter enforcing a minimum delay between consecutive calls."""

    def __init__(self, min_interval_seconds: float) -> None:
        self.interval = min_interval_seconds
        self.last_call = 0.0
        self.lock = threading.Lock()

    def acquire(self) -> None:
        with self.lock:
            now = time.time()
            elapsed = now - self.last_call
            if elapsed < self.interval:
                time.sleep(self.interval - elapsed)
            self.last_call = time.time()


mb_rate_limiter = MUSICBRAINZ_LIMITER
discogs_rate_limiter = DISCOGS_LIMITER


def clean_credit(val: str) -> str:
    s = str(val).strip()
    changed = True
    while changed:
        prev = s
        s = re.sub(r"\*+$", "", s).strip()
        s = re.sub(r"\s*\(\d+\)$", "", s).strip()
        changed = s != prev
    return s


PROTECTED_BAND_NAMES = frozenset(
    {
        "earth, wind & fire",
        "earth, wind and fire",
        "simon & garfunkel",
        "simon and garfunkel",
        "emerson, lake & palmer",
        "emerson, lake and palmer",
        "crosby, stills, nash & young",
        "crosby, stills, nash and young",
        "crosby, stills & nash",
        "crosby, stills and nash",
        "huey lewis & the news",
        "huey lewis and the news",
        "bob marley & the wailers",
        "bob marley and the wailers",
        "tom petty and the heartbreakers",
        "tom petty & the heartbreakers",
        "kc & the sunshine band",
        "kc and the sunshine band",
        "echo & the bunnymen",
        "echo and the bunnymen",
        "sly & the family stone",
        "sly and the family stone",
        "joan jett & the blackhearts",
        "joan jett and the blackhearts",
        "siouxsie & the banshees",
        "siouxsie and the banshees",
        "nick cave & the bad seeds",
        "nick cave and the bad seeds",
        "captain & tennille",
        "captain and tennille",
        "blood, sweat & tears",
        "blood, sweat and tears",
        "brooks & dunn",
        "brooks and dunn",
        "daryl hall & john oates",
        "hall & oates",
        "hall and oates",
        "katrina and the waves",
        "katrina & the waves",
        "florence + the machine",
        "florence and the machine",
        "florence & the machine",
        "derek & the dominos",
        "derek and the dominos",
        "me first and the gimme gimmes",
        "arms and sleepers",
        "godspeed you! black emperor",
        "king gizzard & the lizard wizard",
        "king gizzard and the lizard wizard",
        "of monsters and men",
        "iron & wine",
        "iron and wine",
        "marina and the diamonds",
        "marina & the diamonds",
        "big brother & the holding company",
        "big brother and the holding company",
        "the mamas & the papas",
        "the mamas and the papas",
        "gladys knight & the pips",
        "gladys knight and the pips",
        "frankie lymon & the teenagers",
        "frankie lymon and the teenagers",
        "toots & the maytals",
        "toots and the maytals",
        "kool & the gang",
        "kool and the gang",
        "sam & dave",
        "sam and dave",
        "chas & dave",
        "chas and dave",
        "peaches & herb",
        "peaches and herb",
        "ashford & simpson",
        "ashford and simpson",
        "dr. hook & the medicine show",
        "dr. hook and the medicine show",
        "prince and the revolution",
        "prince & the revolution",
        "prince and the new power generation",
        "prince & the new power generation",
        "hootie & the blowfish",
        "hootie and the blowfish",
        "maroon 5",
        "blink-182",
        "ac/dc",
    }
)


def split_artists(artist_str: str) -> list[str]:
    """Split collaborative artists (e.g. 'Queen & David Bowie' -> ['Queen', 'David Bowie']).

    Preserves protected band names containing '&', 'and', etc. (e.g. 'Simon & Garfunkel').
    """
    raw = str(artist_str).strip()
    if not raw:
        return []
    if raw.casefold() in PROTECTED_BAND_NAMES:
        return [clean_credit(raw)]
    parts = re.split(r"\s+(?:&|feat\.|featuring|with|vs\.)\s+|\s+/\s+", raw, flags=re.IGNORECASE)
    cleaned = [clean_credit(p) for p in parts if clean_credit(p)]
    return cleaned if len(cleaned) > 1 else [clean_credit(raw)]


def extract_discogs_preemphasis(discogs_json: dict[str, Any]) -> bool:
    """Detect pre-emphasis flag from Discogs formats and release notes."""
    if not isinstance(discogs_json, dict):
        return False
    formats = discogs_json.get("formats", [])
    if formats:
        for fmt in formats:
            for desc in fmt.get("descriptions", []):
                if re.search(r"\bpre-?emphasis\b", str(desc), re.IGNORECASE):
                    return True
            if re.search(r"\bpre-?emphasis\b", str(fmt.get("name", "")), re.IGNORECASE):
                return True
    discogs_notes = str(discogs_json.get("notes") or "").strip()
    if discogs_notes:
        if re.search(r"\b(?:pre-?emphasis|with\s+emphasis)\b", discogs_notes, re.IGNORECASE) and not re.search(
            r"\b(?:no\s+pre-?emphasis|without\s+pre-?emphasis)\b", discogs_notes, re.IGNORECASE
        ):
            return True
    return False


def format_performer(name: str, role: str) -> str:
    """Format performer tag with clean role description (e.g. 'Freddie Mercury (lead vocals, piano)')."""
    who = clean_credit(name)
    if not who:
        return ""
    clean_role = re.sub(r"[\[\]]", "", str(role or "")).strip()
    clean_role = re.sub(r"\s+", " ", clean_role).strip().lower()
    if not clean_role or clean_role in ("performer", "soloist", "performed by", "guest", "artist"):
        return who
    return f"{who} ({clean_role})"


def parse_instrument_roles(role_str: str) -> list[str]:
    """Extract normalized instrument role strings for Vorbis PERFORMER:<role> tags."""
    if not role_str:
        return []
    cleaned = re.sub(r"[\[\]\(\)]", " ", str(role_str)).strip().lower()
    parts = re.split(r"[,/&]|\band\b", cleaned)
    results = []

    role_map = [
        (r"\blead\s+vocals?\b", "lead vocals"),
        (r"\bbacking\s+vocals?\b", "backing vocals"),
        (r"\bvocals?\b", "vocals"),
        (r"\b(?:electric\s+bass(?:\s+guitar)?|bass\s+electric|bass\s+guitar)\b", "bass guitar"),
        (r"\bbass\b", "bass"),
        (r"\b(?:electric\s+guitar|guitar\s+electric)\b", "electric guitar"),
        (r"\b(?:acoustic\s+guitar|guitar\s+acoustic)\b", "acoustic guitar"),
        (r"\b(?:lead\s+guitar|guitar\s+lead)\b", "lead guitar"),
        (r"\b(?:rhythm\s+guitar|guitar\s+rhythm)\b", "rhythm guitar"),
        (r"\bguitars?\b", "guitar"),
        (r"\bdrums?\b", "drums"),
        (r"\bpercussion\b", "percussion"),
        (r"\bpiano\b", "piano"),
        (r"\bkeyboards?\b", "keyboards"),
        (r"\bsynthesizers?\b|\bsynth\b", "synthesizer"),
        (r"\borgans?\b", "organ"),
        (r"\bsaxophones?\b|\bsax\b", "saxophone"),
        (r"\btrumpets?\b", "trumpet"),
        (r"\btrombones?\b", "trombone"),
        (r"\bflutes?\b", "flute"),
        (r"\bharmonica\b", "harmonica"),
        (r"\bviolins?\b", "violin"),
        (r"\bcellos?\b", "cello"),
        (r"\bviolas?\b", "viola"),
    ]
    for p in parts:
        p_str = p.strip()
        matched = False
        for pattern, canon in role_map:
            if re.search(pattern, p_str):
                if canon not in results:
                    results.append(canon)
                matched = True
        if not matched and p_str and len(p_str) <= 30 and re.match(r"^[a-z\s-]+$", p_str):
            if p_str not in results and p_str not in ("performer", "guest", "artist", "soloist"):
                results.append(p_str)
    return results


def musical_key_to_camelot(key_str: str) -> str:
    """Convert musical key representation to standard Camelot Wheel notation (e.g. 'C' -> '8B', 'Am' -> '8A')."""
    if not key_str:
        return ""
    k = key_str.strip()
    if re.match(r"^(?:1[0-2]|[1-9])[ABab]$", k):
        return k.upper()

    is_minor = bool(re.search(r"\b(?:minor|min)\b", k, re.IGNORECASE) or re.search(r"^[A-G][#b♭♯]?m$", k))

    m_root = re.match(r"^([A-Ga-g][#b♭♯]?)", k)
    if not m_root:
        return ""
    root = m_root.group(1).capitalize()
    root = root.replace("♭", "b").replace("♯", "#")

    major_map = {
        "C": "8B",
        "G": "9B",
        "D": "10B",
        "A": "11B",
        "E": "12B",
        "B": "1B",
        "F#": "2B",
        "Gb": "2B",
        "C#": "3B",
        "Db": "3B",
        "G#": "4B",
        "Ab": "4B",
        "D#": "5B",
        "Eb": "5B",
        "A#": "6B",
        "Bb": "6B",
        "F": "7B",
    }
    minor_map = {
        "A": "8A",
        "E": "9A",
        "B": "10A",
        "F#": "11A",
        "Gb": "11A",
        "C#": "12A",
        "Db": "12A",
        "G#": "1A",
        "Ab": "1A",
        "D#": "2A",
        "Eb": "2A",
        "A#": "3A",
        "Bb": "3A",
        "F": "4A",
        "C": "5A",
        "G": "6A",
        "D": "7A",
    }
    if is_minor:
        return minor_map.get(root, "")
    else:
        return major_map.get(root, "")


def detect_audiophile_series(notes: str = "", series_list: list | None = None, labels: list | None = None) -> str:
    """Detect prestigious audiophile mastering series from release metadata."""
    combined = (notes or "") + " "
    if series_list:
        for s in series_list:
            if isinstance(s, dict):
                combined += s.get("name", "") + " "
            elif isinstance(s, str):
                combined += s + " "
    if labels:
        for l in labels:
            if isinstance(l, dict):
                combined += l.get("name", "") + " "
            elif isinstance(l, str):
                combined += l + " "

    series_patterns = [
        (
            r"\b(?:Mobile\s+Fidelity(?:\s+Sound\s+Lab)?|MFSL|Original\s+Master\s+Recording|Ultradisc(?:\s+II)?)\b",
            "Mobile Fidelity Sound Lab (MFSL)",
        ),
        (
            r"\b(?:DCC(?:\s+Compact\s+Classics)?(?:\s+24K)?|24\s*Karat\s+Gold\s+CD\s+from\s+DCC)\b",
            "DCC Compact Classics 24K Gold",
        ),
        (r"\b(?:Analogue\s+Productions)\b", "Analogue Productions"),
        (r"\b(?:Target(?:\s+CD)?)\b", "Target CD"),
        (r"\b(?:Black\s+Triangle)\b", "Black Triangle"),
        (r"\b(?:CBS/Sony\s+Master\s*Sound|Master\s*Sound(?:\s+Collector'?s\s+Edition)?)\b", "CBS/Sony Master Sound"),
        (r"\b(?:Audio\s+Fidelity(?:\s+24K)?)\b", "Audio Fidelity"),
        (r"\b(?:SHM-SACD)\b", "SHM-SACD"),
        (r"\b(?:SHM-CD)\b", "SHM-CD"),
        (r"\b(?:XRCD(?:2|24)?)\b", "XRCD"),
        (r"\b(?:K2HD)\b", "K2HD"),
        (r"\b(?:Blu-spec\s+CD(?:2)?|BSCD2)\b", "Blu-spec CD"),
        (r"\b(?:Reference\s+Recordings)\b", "Reference Recordings"),
        (r"\b(?:Chesky\s+Records)\b", "Chesky Records"),
    ]
    for pattern, name in series_patterns:
        if re.search(pattern, combined, re.IGNORECASE):
            return name
    return ""


def detect_source_format(notes: str = "", formats_list: list | None = None) -> str:
    """Detect audio source format / master tape provenance from notes and format descriptors."""
    combined = (notes or "") + " "
    if formats_list:
        for f in formats_list:
            if isinstance(f, dict):
                combined += f.get("name", "") + " "
                for d in f.get("descriptions", []):
                    combined += str(d) + " "
            elif isinstance(f, str):
                combined += f + " "

    source_patterns = [
        (
            r"\b(?:flat\s+transfer(?:\s+from\s+(?:the\s+)?(?:analog|master)\s+tapes?)?)\b",
            "Flat Transfer from Master Tapes",
        ),
        (
            r"\b(?:original\s+(?:analog\s+)?master\s+tapes?|from\s+the\s+original\s+master\s+tapes?)\b",
            "Original Master Tapes",
        ),
        (r"\b(?:original\s+analog\s+master|analog\s+master)\b", "Original Analog Master"),
        (r"\b(?:1/4\"|1/2\")\s*(?:15|30)\s*ips\b", '1/4" Analog Master'),
        (r"\b(?:Direct\s+Metal\s+Mastering|DMM)\b", "Direct Metal Mastering (DMM)"),
        (r"\b(?:Half-Speed\s+Master(?:ed|ing)?)\b", "Half-Speed Mastered"),
        (r"\b(?:Digital\s+Master(?:\s*\(?PCM\)?)?)\b", "Digital Master (PCM)"),
        (r"\b(?:DSD\s+Master|Direct\s+Stream\s+Digital)\b", "DSD Master"),
    ]
    for pattern, name in source_patterns:
        if re.search(pattern, combined, re.IGNORECASE):
            return name
    return ""


def parse_movement_info(title: str) -> tuple[str, str]:
    """Extract (movement_number, movement_name) from track title if present."""
    m = re.search(r"\b(?:Movement|Movt\.?|Part|Pt\.?)\s+([0-9IVXLCDM]+)[:.-]?\s*(.*)", title, re.IGNORECASE)
    if not m:
        m = re.search(
            r"\b([IVXLCDM]+)\.\s+(Allegro|Adagio|Andante|Presto|Largo|Moderato|Vivace|Scherzo|Rondo|Menuetto|Grave|Lento|Larghetto)\b(.*)",
            title,
            re.IGNORECASE,
        )
    if m:
        num = m.group(1).strip()
        name = (m.group(2).strip() + " " + m.group(3).strip()).strip() if len(m.groups()) >= 3 else m.group(2).strip()
        return num, name
    return "", ""


def clean_disc_subtitle(raw_title: str, disc_num: str | int = "") -> str:
    """Normalize and clean a multi-disc or box set subtitle.

    Strips redundant disc numbering prefixes (e.g. 'CD 1: The Original Album' -> 'The Original Album'),
    bracketed tags, and rejects trivial disc numbers (e.g. 'CD 1', 'Disc 2').
    """
    s = str(raw_title or "").strip()
    if not s:
        return ""

    # Strip enclosing quotes or backticks
    s = re.sub(r'^["\'`]+|["\'`]+$', "", s).strip()

    # Check if subtitle is purely a disc / media / side identifier without actual subtitle
    trivial_pattern = (
        r"^(?:(?:compact\s+disc|cd|disc|lp|record|vinyl|dvd|bd|bluray)\s*(?:[0-9IVXLCDM]+|[a-z]+)?"
        r"|side\s*[a-z0-9]+"
        r"|[0-9IVXLCDM]+)$"
    )
    if re.match(trivial_pattern, s, re.IGNORECASE):
        return ""

    # Strip disc prefixes like 'CD 1: ', 'CD1 - ', 'Disc 1: ', 'Disc One - ', '[CD 1] '
    prefix_pattern = (
        r"^(?:\[?(?:compact\s+disc|cd|disc|lp|record|vinyl|dvd|bd|bluray)\s*(?:[0-9IVXLCDM]+|[a-z]+)?\]?"
        r"|\[?[0-9IVXLCDM]+\]?)"
        r"\s*[:.\-—–/]\s*"
    )
    cleaned = re.sub(prefix_pattern, "", s, flags=re.IGNORECASE).strip()

    # Also strip leading bracketed disc prefix like '[CD 1] The Studio Album'
    cleaned = re.sub(r"^\[(?:cd|disc)\s*[0-9IVXLCDM]+\]\s*", "", cleaned, flags=re.IGNORECASE).strip()

    # Strip trailing disc suffixes like '(Disc 1)', '(CD 1)'
    cleaned = re.sub(
        r"\s*[\(\[](?:compact\s+disc|cd|disc|lp)\s*[0-9IVXLCDM]+[\)\]]$", "", cleaned, flags=re.IGNORECASE
    ).strip()

    # If stripping left nothing or trivial text, return empty
    if not cleaned or re.match(trivial_pattern, cleaned, re.IGNORECASE):
        return ""

    return cleaned


def extract_disc_subtitles(
    discogs_json: dict[str, Any] | None,
    mb_recordings_json: dict[str, Any] | None,
    folder: Path | None = None,
) -> dict[str, str]:
    """Extract and normalize disc subtitles for multi-disc releases and box sets.

    Searches:
    1. Discogs tracklist headings and index tracks
    2. Discogs format descriptions / texts
    3. MusicBrainz media titles
    4. Local folder structure (e.g. 'CD1 - The Original Album') and CUE sheets

    Returns a mapping of disc_number (as string e.g. '1', '2') to normalized subtitle string.
    """
    subtitles: dict[str, str] = {}

    # 1. MusicBrainz media titles
    if mb_recordings_json:
        for med in mb_recordings_json.get("media", []):
            pos = str(med.get("position", "")).strip()
            title = str(med.get("title", "")).strip()
            if pos and title:
                cleaned = clean_disc_subtitle(title, pos)
                if cleaned:
                    subtitles[pos] = cleaned

    # 2. Discogs tracklist headings and index tracks
    if discogs_json:
        tracklist = discogs_json.get("tracklist", [])
        for idx, item in enumerate(tracklist):
            itype = str(item.get("type_", item.get("type", "track"))).lower()
            ititle = str(item.get("title", "")).strip()
            ipos = str(item.get("position", "")).strip()

            if itype in ("heading", "index") and ititle:
                # Check if heading title or position explicitly mentions disc number
                disc_m = re.search(
                    r"\b(?:cd|disc|compact\s+disc)\s*([0-9IVXLCDM]+)\b", f"{ipos} {ititle}", re.IGNORECASE
                )
                if disc_m:
                    d_raw = disc_m.group(1).upper()
                    roman_map = {
                        "I": "1",
                        "II": "2",
                        "III": "3",
                        "IV": "4",
                        "V": "5",
                        "VI": "6",
                        "VII": "7",
                        "VIII": "8",
                        "IX": "9",
                        "X": "10",
                    }
                    d_num = roman_map.get(d_raw, d_raw)
                    if d_num.isdigit():
                        cleaned = clean_disc_subtitle(ititle, d_num)
                        if cleaned and d_num not in subtitles:
                            subtitles[d_num] = cleaned
                else:
                    # Look ahead at subsequent track positions to determine which disc this heading introduces
                    for next_item in tracklist[idx + 1 :]:
                        next_type = str(next_item.get("type_", next_item.get("type", "track"))).lower()
                        if next_type in ("heading", "index"):
                            break
                        next_pos = str(next_item.get("position", "")).strip()
                        pm = re.match(r"^(?:cd\s*)?([0-9]+)[-_.][0-9]+", next_pos, re.IGNORECASE)
                        if pm:
                            d_num = str(int(pm.group(1)))
                            cleaned = clean_disc_subtitle(ititle, d_num)
                            if cleaned and d_num not in subtitles:
                                subtitles[d_num] = cleaned
                            break
            elif itype == "index" and item.get("sub_tracks"):
                sub_tracks = item.get("sub_tracks", [])
                if sub_tracks and ititle:
                    first_sub_pos = str(sub_tracks[0].get("position", "")).strip()
                    pm = re.match(r"^(?:cd\s*)?([0-9]+)[-_.][0-9]+", first_sub_pos, re.IGNORECASE)
                    if pm:
                        d_num = str(int(pm.group(1)))
                        cleaned = clean_disc_subtitle(ititle, d_num)
                        if cleaned and d_num not in subtitles:
                            subtitles[d_num] = cleaned

        # Formats text descriptions
        formats = discogs_json.get("formats", [])
        if len(formats) > 1 and not subtitles:
            for d_idx, fmt in enumerate(formats, 1):
                f_text = str(fmt.get("text", "")).strip()
                if f_text:
                    cleaned = clean_disc_subtitle(f_text, d_idx)
                    if cleaned and str(d_idx) not in subtitles:
                        subtitles[str(d_idx)] = cleaned

    # 3. Local folder / CUE / subfolder clues
    if folder and folder.is_dir():
        try:
            for sub in folder.iterdir():
                if sub.is_dir():
                    m = re.match(r"^(?:cd|disc)\s*([0-9]+)\s*[:.\-—–]\s*(.+)$", sub.name, re.IGNORECASE)
                    if m:
                        d_num = str(int(m.group(1)))
                        cleaned = clean_disc_subtitle(m.group(2), d_num)
                        if cleaned and d_num not in subtitles:
                            subtitles[d_num] = cleaned
        except Exception:
            pass

        try:
            for cue in folder.glob("*.cue"):
                content = cue.read_text(encoding="utf-8", errors="replace")
                for line in content.splitlines():
                    sub_m = re.match(r"^\s*REM\s+DISCSUBTITLE\s+[\"']?(.+?)[\"']?\s*$", line, re.IGNORECASE)
                    if sub_m:
                        c_val = clean_disc_subtitle(sub_m.group(1))
                        d_m = re.search(r"\b(?:cd|disc)\s*([0-9]+)\b", cue.stem, re.IGNORECASE)
                        d_num = str(int(d_m.group(1))) if d_m else "1"
                        if c_val and d_num not in subtitles:
                            subtitles[d_num] = c_val
                    comm_m = re.match(
                        r"^\s*REM\s+COMMENT\s+[\"']?(?:cd|disc)\s*([0-9]+)\s*[:.\-—–]\s*(.+?)[\"']?\s*$",
                        line,
                        re.IGNORECASE,
                    )
                    if comm_m:
                        d_num = str(int(comm_m.group(1)))
                        c_val = clean_disc_subtitle(comm_m.group(2), d_num)
                        if c_val and d_num not in subtitles:
                            subtitles[d_num] = c_val
        except Exception:
            pass

    return subtitles


def normalize_catno(val: str) -> str:
    s = str(val).strip()
    if re.match(r"^(none|\[none\]|n/a|\?)$", s, re.IGNORECASE):
        return ""
    return re.sub(r"[^a-zA-Z0-9]+", "", s).lower()


def clean_catno(val: str) -> str:
    s = str(val).strip()
    if re.match(r"^(none|\[none\]|n/a|\?)$", s, re.IGNORECASE):
        return ""
    return s


def normalize_country_iso(val: str) -> str:
    c = str(val).strip().lower()
    mapping = {
        "usa": "US",
        "us": "US",
        "united states": "US",
        "uk": "GB",
        "u.k.": "GB",
        "united kingdom": "GB",
        "germany": "DE",
        "west germany": "DE",
        "w. germany": "DE",
        "west-germany": "DE",
        "east germany": "DE",
        "de": "DE",
        "japan": "JP",
        "jp": "JP",
        "france": "FR",
        "fr": "FR",
        "italy": "IT",
        "it": "IT",
        "netherlands": "NL",
        "holland": "NL",
        "nl": "NL",
        "canada": "CA",
        "ca": "CA",
        "australia": "AU",
        "au": "AU",
        "worldwide": "XW",
        "ww": "XW",
        "europe": "XE",
    }
    if c in mapping:
        return mapping[c]
    if re.match(r"^[a-z]{2}$", c):
        return c.upper()
    return ""


def album_sort_value(title: str) -> str:
    title = title.strip()
    m = re.match(r"^(The|A|An)\s+(.+)$", title, re.IGNORECASE)
    if m:
        article, rest = m.groups()
        return f"{rest}, {article}"
    return title


def choose_date(discogs_date: str, edition_year: str, quiet_mode: bool = False) -> tuple[str, str, str]:
    m_disc = re.match(r"^(\d{4})", discogs_date)
    discogs_year = m_disc.group(1) if m_disc else ""

    if not discogs_date:
        if edition_year:
            return edition_year, edition_year, "edition"
        return "", "", ""

    if not edition_year or edition_year == discogs_year:
        return discogs_date, discogs_year, "discogs"

    if quiet_mode:
        # A conflict is not resolved by silence. Preserve existing DATE and
        # surface the ambiguity through provenance/review instead.
        return "", "", "conflict-review"

    print("\n  DATE CONFLICT")
    print(f"  Discogs release date : {discogs_date}")
    print(f"  EDITION year         : {edition_year}")
    print("  [D] Discogs date  [E] Edition year  [S] skip/leave DATE empty")
    while True:
        try:
            choice = input("  Выбор [D/e/s]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = "s"
        if choice in ("d", ""):
            return discogs_date, discogs_year, "discogs"
        elif choice == "e":
            return edition_year, edition_year, "edition"
        elif choice == "s":
            return "", "", "skipped"


def choose_mb_candidate(candidate_rows: list[dict[str, Any]], quiet_mode: bool = False) -> tuple[str, str]:
    if quiet_mode:
        print("  -> MB MATCH: AMBIGUOUS — пропуск (--quiet)")
        return "", "AMBIGUOUS"
    if not candidate_rows:
        return "", "NONE"

    selectable = [r for r in candidate_rows if not r.get("identity", {}).get("rejected", False)]

    print("\n  MB MATCH: MANUAL REVIEW")
    print("  MusicBrainz candidates after physical verification:\n")
    header = f"  {'#':<2} {'Confidence':<10} {'Date':<12} {'Cn':<3} {'Trk':<5} {'Title':<16} {'MaxΔ':<6} {'TotΔ':<7} {'TOCΔ':<7} {'Score':<7} {'MBID':<36}"
    print(header)
    print("  " + "─" * 118)

    for i, row in enumerate(candidate_rows, start=1):
        ident = row.get("identity", {})
        score = ident.get("score") or row.get("score", 0)
        conf = ident.get("confidence", "NONE")
        date = (row.get("date", "") or "")[:12]
        cntry = (row.get("country", "") or "")[:3]
        trk = str(ident.get("track_count_mb", ""))[:5]
        title = (row.get("title", "") or "")[:16]
        max_d = (
            f"{round(ident['duration_max_delta_seconds'])}s"
            if ident.get("duration_max_delta_seconds") is not None
            else "—"
        )
        tot_d = "—"
        if ident.get("discogs_total_seconds") is not None and ident.get("mb_total_seconds") is not None:
            tot_d = f"{round(abs(ident['discogs_total_seconds'] - ident['mb_total_seconds']))}s"
        toc_d = (
            f"{round(ident['toc_duration_max_delta_seconds'])}s"
            if ident.get("toc_duration_max_delta_seconds") is not None
            else "—"
        )
        mbid = str(row.get("id", ""))[:36]
        rejected = ident.get("rejected", False)
        if rejected:
            conf = "REJECT"
        print(
            f"  {i:<2} {conf:<10} {date:<12} {cntry:<3} {trk:<5} {title:<16} {max_d:<6} {tot_d:<7} {toc_d:<7} {score:<7} {mbid:<36}"
        )
        if rejected:
            print(f"     ↳ REJECT: {ident.get('confidence_basis', 'physical conflict')}")
        else:
            print(f"     ↳ {ident.get('confidence_basis', 'none')}")

    print()
    if not selectable:
        print("  [S] skip — все кандидаты отклонены verifier")
        return "", "NONE"

    while True:
        try:
            val = input(f"  Выбор [1-{len(candidate_rows)}/s]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "", "NONE"
        if val == "s":
            return "", "NONE"
        if val.isdigit():
            idx = int(val)
            if 1 <= idx <= len(candidate_rows):
                picked = candidate_rows[idx - 1]
                if picked.get("identity", {}).get("rejected", False):
                    print("  -> Нельзя выбрать REJECT-кандидата.")
                    continue
                mb_id = str(picked.get("id", ""))
                if mb_id:
                    print(f"  -> Выбран MB release: {mb_id}")
                    return mb_id, "MANUAL"


def discogs_get(
    url: str,
    session: requests.Session,
    token: str,
    user_agent: str,
    cache_dir: Path,
) -> dict[str, Any] | None:
    headers = {
        "Authorization": f"Discogs token={token}",
        "User-Agent": user_agent,
    }
    result = fetch_json(
        session,
        url,
        headers=headers,
        cache=JsonCache(cache_dir),
        attempts=3,
        rate_limiter=discogs_rate_limiter,
    )
    if result.ok and isinstance(result.payload, dict):
        return result.payload
    if result.status != 404:
        print(f"WARNING: Discogs request incomplete for {url}: {result.error}", file=sys.stderr)
    return None


def mb_get(
    url: str,
    session: requests.Session,
    user_agent: str,
    cache_dir: Path,
) -> tuple[int, Any]:
    headers = {
        "Accept": "application/json",
        "User-Agent": user_agent,
    }
    result = fetch_json(
        session,
        url,
        headers=headers,
        cache=JsonCache(cache_dir),
        attempts=4,
        rate_limiter=mb_rate_limiter,
    )
    if not result.ok and result.status != 404:
        print(f"WARNING: MusicBrainz request incomplete for {url}: {result.error}", file=sys.stderr)
    return result.status, result.payload


def extract_cue_track_title(cue_path: Path, track_num: int) -> str:
    if not cue_path.is_file():
        return ""
    try:
        text = cue_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    in_track = False
    for line in text.splitlines():
        m_tr = re.match(r"^\s*TRACK\s+(\d+)", line, re.IGNORECASE)
        if m_tr:
            in_track = int(m_tr.group(1)) == track_num
            continue
        if in_track:
            m_ti = re.match(r'^\s*TITLE\s+"?([^"\r\n]+)"?', line, re.IGNORECASE)
            if m_ti:
                return m_ti.group(1).strip()
    return ""


def cue_track_has_preemphasis(cue_path: Path, track_num: int) -> bool:
    if not cue_path.is_file():
        return False
    try:
        text = cue_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    in_track = False
    for line in text.splitlines():
        m_tr = re.match(r"^\s*TRACK\s+(\d+)", line, re.IGNORECASE)
        if m_tr:
            in_track = int(m_tr.group(1)) == track_num
            continue
        if in_track and re.search(r"(?:^|\s)FLAGS\s+(?:[^\s]+\s+)*PRE(?:\s|$)", line, re.IGNORECASE):
            return True
    return False


def log_track_has_preemphasis(log_path: Path, track_num: int) -> bool:
    if not log_path.is_file():
        return False
    raw = log_path.read_bytes()
    text = ""
    for enc in ("utf-8", "utf-16", "utf-16-le", "cp1252", "cp1251", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except Exception:
            continue
    if not text:
        return False
    in_track = False
    for line in text.splitlines():
        m_tr = re.match(r"^\s*(?:FILE\s+)?[Tt][Rr][Aa][Cc][Kk]\s+(\d+)", line)
        if m_tr:
            in_track = int(m_tr.group(1)) == track_num
            continue
        if in_track and re.search(r"(?:Pre-?emphasis|Преакцент)", line, re.IGNORECASE):
            if re.search(r"(?:Yes|Ja|Да|True|1)", line, re.IGNORECASE) and not re.search(
                r"(?:No|Nein|Нет|False|0)", line, re.IGNORECASE
            ):
                return True
    return False


def log_has_deemphasis_applied(log_path: Path) -> bool:
    """Check if the rip log indicates de-emphasis was already applied during ripping/decoding."""
    if not log_path.is_file():
        return False
    raw = log_path.read_bytes()
    text = ""
    for enc in ("utf-8", "utf-16", "utf-16-le", "cp1252", "cp1251", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except Exception:
            continue
    if not text:
        return False
    applied_patterns = [
        r"de-emphasis\s+applied\s*:\s*(?:yes|true|1|да)",
        r"applied\s+de-emphasis",
        r"apply\s+de-emphasis\s*:\s*(?:yes|true|1|да)",
        r"sox\s+.*?\bdeemph\b",
        r"de-emphasized\s*:\s*(?:yes|true|1|да)",
        r"деакцент\s*:\s*(?:да|true|1)",
    ]
    for pat in applied_patterns:
        if re.search(pat, text, re.IGNORECASE):
            return True
    return False


def get_track_accurip_data(meta_files: list[Path], track_num: int) -> dict[str, str]:
    ar_result = ""
    ar_crc = ""
    ctdb_tocid = ""
    ctdb_status = ""

    for mf in meta_files:
        if not mf.is_file():
            continue
        raw = mf.read_bytes()
        text = ""
        for enc in ("utf-8", "utf-16", "utf-16-le", "cp1252", "cp1251", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except Exception:
                continue
        if not text:
            continue
        text = text.replace("\r\n", "\n").replace("\r", "\n")

        # 1. CTDB TOCID
        if not ctdb_tocid:
            m_ctdb = re.search(r"\[CTDB TOCID:\s*([^\]]+)\]", text)
            if m_ctdb:
                ctdb_tocid = m_ctdb.group(1).strip()

        # 2. AccurateRip track line:
        if not ar_result or not ar_crc:
            # Multi-line block matching (EAC 1.0+, XLD, CUETools)
            m_block = re.search(
                rf"^\s*Track\s+0*{track_num}\b(.*?)(?=^\s*Track\s+\d+\b|\Z)",
                text,
                flags=re.MULTILINE | re.DOTALL,
            )
            if m_block:
                blk = m_block.group(1)
                m_ar = re.search(
                    r"^\s*(Accurately ripped[^\r\n\[\]]*|Cannot be verified[^\r\n\[\]]*)\s*\[([0-9A-Fa-f]{8})\]",
                    blk,
                    flags=re.MULTILINE | re.IGNORECASE,
                )
                if m_ar:
                    ar_result = m_ar.group(1).strip()
                    ar_crc = m_ar.group(2).upper().strip()
                elif not ar_result:
                    m_not_present = re.search(
                        r"^\s*(Track not present in AccurateRip database[^\r\n]*)",
                        blk,
                        flags=re.MULTILINE | re.IGNORECASE,
                    )
                    if m_not_present:
                        ar_result = m_not_present.group(1).strip()

            # Single-line fallback (AccurateRip summary / cue logs)
            if not ar_result or not ar_crc:
                m_ar_single = re.search(
                    rf"Track\s+0*{track_num}\s*[:\s]\s*([^\r\n\[\]]+?)\s*\[([0-9A-Fa-f]{{8}})\]",
                    text,
                    re.IGNORECASE,
                )
                if m_ar_single:
                    if not ar_result:
                        ar_result = m_ar_single.group(1).strip()
                    if not ar_crc:
                        ar_crc = m_ar_single.group(2).upper().strip()

        # 3. CTDB status line:
        if not ctdb_status and "CTDB Status" in text:
            ctdb_section = text.split("CTDB Status", 1)[1]
            m_ctdb_stat = re.search(rf"^\s*0*{track_num}\s*\|\s*([^\n]+)$", ctdb_section, re.MULTILINE)
            if m_ctdb_stat:
                ctdb_status = m_ctdb_stat.group(1).strip()

    return {
        "ar_result": ar_result,
        "ar_crc": ar_crc,
        "ctdb_tocid": ctdb_tocid,
        "ctdb_status": ctdb_status,
    }


def get_track_eac_peak(rip_meta_files: list[Path], track_num: int, flac_path: Path | None = None) -> str:
    """Extract track peak level in percentage (e.g. '100.0 %') from EAC/XLD logs or measure from PCM."""
    # 1. Search logs for track-specific peak
    for mf in rip_meta_files:
        if mf.suffix.lower() != ".log":
            continue
        raw = mf.read_bytes()
        text = ""
        enc_order: tuple[str, ...]
        if raw.startswith(b"\xff\xfe"):
            enc_order = ("utf-16-le", "utf-16")
        elif raw.startswith(b"\xfe\xff"):
            enc_order = ("utf-16-be", "utf-16")
        elif b"\x00" in raw[:200]:
            enc_order = ("utf-16-le", "utf-16", "utf-8", "cp1252")
        else:
            enc_order = ("utf-8-sig", "utf-8", "cp1252", "cp1251", "latin-1")

        for enc in enc_order:
            try:
                cand = raw.decode(enc)
                if "\x00" not in cand:
                    text = cand
                    break
            except Exception:
                continue
        if not text:
            continue
        text = text.replace("\r\n", "\n").replace("\r", "\n")

        # Multi-line track block matching (e.g. "Track 1" followed by "Peak level 89.8 %")
        m_block = re.search(
            rf"^\s*Track\s+0*{track_num}\b(.*?)(?=^\s*Track\s+\d+\b|\Z)",
            text,
            flags=re.MULTILINE | re.DOTALL | re.IGNORECASE,
        )
        if m_block:
            blk = m_block.group(1)
            m_peak = re.search(r"Peak\s+level\s+([\d.,]+)\s*%", blk, flags=re.IGNORECASE)
            if m_peak:
                p_val = m_peak.group(1).replace(",", ".")
                return f"{float(p_val):.1f} %"

        # Single-line format fallback: "Track X ... Peak level YY.Y %"
        m_single = re.search(
            rf"Track\s+0*{track_num}\b[^\n\r]*?Peak\s+level\s+([\d.,]+)\s*%",
            text,
            flags=re.IGNORECASE,
        )
        if m_single:
            p_val = m_single.group(1).replace(",", ".")
            return f"{float(p_val):.1f} %"

    # 2. Fallback: Bit-accurate PCM sample peak measurement (standard EAC formula)
    if flac_path and flac_path.is_file():
        try:
            max_val = max(float(np.max(np.abs(block))) for block in dr14_parse.iter_flac_pcm(flac_path))
            return f"{max_val * 100.0:.1f} %"
        except Exception:
            pass
    return ""


def compute_acoustid_fingerprint(flac_path: Path) -> tuple[int, str]:
    """Compute duration (seconds) and Chromaprint fingerprint using fpcalc."""
    if not shutil.which("fpcalc") or not flac_path.is_file():
        return 0, ""
    try:
        res = subprocess.run(
            ["fpcalc", "-json", str(flac_path)],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        data = json.loads(res.stdout)
        dur = round(float(data.get("duration", 0)))
        fp = str(data.get("fingerprint", "")).strip()
        return dur, fp
    except Exception:
        return 0, ""


def _rank_acoustid_results(payload: object) -> list[dict[str, Any]]:
    return mb_identity_score.rank_acoustid_results(payload, load_config().matching.acoustid_min_score)


def resolve_acoustid_id(duration: int, fingerprint: str, session: requests.Session) -> str:
    """Lookup AcoustID UUID using AcoustID API if API key is provided."""
    client_key = os.environ.get("ACOUSTID_API_KEY") or os.environ.get("ACOUSTID_CLIENT_KEY")
    if not client_key or not fingerprint or duration <= 0:
        return ""
    try:
        url = "https://api.acoustid.org/v2/lookup"
        acoustid_params: dict[str, str | int] = {
            "client": client_key,
            "meta": "recordingids",
            "duration": duration,
            "fingerprint": fingerprint,
        }
        response = fetch_json(
            session,
            url + "?" + urllib.parse.urlencode(acoustid_params),
            headers={"User-Agent": "AudiophileArchTagger/6.0"},
            cache=JsonCache(Path(__file__).parent / ".api_cache" / "acoustid"),
            timeout=8,
        )
        if response.ok:
            results = _rank_acoustid_results(response.payload)
            if results and (len(results) == 1 or results[0]["score"] > results[1]["score"]):
                return str(results[0]["id"])
    except Exception:
        pass
    return ""


def get_acousticbrainz_data(
    recording_id: str,
    session: requests.Session,
    cache_dir: Path | None = None,
) -> dict[str, Any]:
    """Fetch musical analysis (BPM, Key, Mood, Danceability) from AcousticBrainz."""
    res: dict[str, Any] = {
        "bpm": "",
        "key": "",
        "danceability": "",
        "moods": [],
        "energy": "",
        "valence": "",
    }
    if not recording_id:
        return res

    ab_cache = JsonCache(
        (cache_dir or Path(__file__).parent / ".api_cache") / "acousticbrainz", ttl_seconds=90 * 24 * 3600
    )
    headers = {"User-Agent": "AudiophileArchTagger/6.0"}
    low_url = f"https://acousticbrainz.org/api/v1/{recording_id}/low-level"
    low_result = fetch_json(session, low_url, headers=headers, cache=ab_cache, timeout=5)
    low_data = low_result.payload if low_result.ok else None
    if isinstance(low_data, dict):
        rhythm = low_data.get("rhythm")
        bpm_val = rhythm.get("bpm") if isinstance(rhythm, dict) else None
        if bpm_val is not None:
            try:
                parsed_bpm = float(bpm_val)
                if math.isfinite(parsed_bpm) and parsed_bpm > 0:
                    res["bpm"] = str(round(parsed_bpm))
            except Exception:
                pass
        raw_tonal = low_data.get("tonal")
        tonal = raw_tonal if isinstance(raw_tonal, dict) else {}
        key_k = tonal.get("key_key", "")
        key_s = tonal.get("key_scale", "")
        if key_k:
            res["key"] = f"{key_k} {key_s}".strip() if key_s else key_k

    # 2. High-level data (Mood, Danceability)
    high_url = f"https://acousticbrainz.org/api/v1/{recording_id}/high-level"
    high_result = fetch_json(session, high_url, headers=headers, cache=ab_cache, timeout=5)
    high_data = high_result.payload if high_result.ok else None
    if isinstance(high_data, dict):
        raw_highlevel = high_data.get("highlevel")
        raw_models = raw_highlevel if isinstance(raw_highlevel, dict) else {}
        hl: dict[str, dict[str, Any]] = {}
        for name, model in raw_models.items():
            if not isinstance(model, dict):
                continue
            try:
                probability = float(model.get("probability", 0))
            except (TypeError, ValueError, OverflowError):
                continue
            if not math.isfinite(probability) or not 0 <= probability <= 1:
                continue
            value = model.get("value", "")
            if not isinstance(value, str):
                continue
            hl[name] = {"value": value, "probability": probability}
        dance = hl.get("danceability", {})
        if dance.get("value") == "danceable" and dance.get("probability", 0) >= 0.5:
            res["danceability"] = "1"
        elif dance.get("value") == "not_danceable" and dance.get("probability", 0) >= 0.5:
            res["danceability"] = "0"

        # Moods: happy, sad, relaxed, aggressive, party, acoustic, electronic
        mood_keys = [
            ("mood_happy", "Happy"),
            ("mood_sad", "Sad"),
            ("mood_relaxed", "Relaxed"),
            ("mood_aggressive", "Aggressive"),
            ("mood_party", "Party"),
            ("mood_acoustic", "Acoustic"),
            ("mood_electronic", "Electronic"),
        ]
        mood_list = []
        for mk, mname in mood_keys:
            m_obj = hl.get(mk, {})
            val = m_obj.get("value", "")
            prob = m_obj.get("probability", 0.0)
            if val and not val.startswith("not_") and prob >= 0.6:
                mood_list.append(mname)
        res["moods"] = mood_list

        # Energy metric: aggressive, party, danceability
        agg_prob = (
            hl.get("mood_aggressive", {}).get("probability", 0.0)
            if hl.get("mood_aggressive", {}).get("value") == "aggressive"
            else 0.0
        )
        party_prob = (
            hl.get("mood_party", {}).get("probability", 0.0)
            if hl.get("mood_party", {}).get("value") == "party"
            else 0.0
        )
        dance_prob = (
            hl.get("danceability", {}).get("probability", 0.0)
            if hl.get("danceability", {}).get("value") == "danceable"
            else 0.0
        )
        energy_score = round(max(agg_prob, party_prob, dance_prob * 0.8), 2)
        if energy_score > 0:
            res["energy"] = f"{energy_score:.2f}"

        # Valence metric: happy / relaxed vs sad
        happy_prob = (
            hl.get("mood_happy", {}).get("probability", 0.0)
            if hl.get("mood_happy", {}).get("value") == "happy"
            else 0.0
        )
        sad_prob = (
            hl.get("mood_sad", {}).get("probability", 0.0) if hl.get("mood_sad", {}).get("value") == "sad" else 0.0
        )
        relaxed_prob = (
            hl.get("mood_relaxed", {}).get("probability", 0.0)
            if hl.get("mood_relaxed", {}).get("value") == "relaxed"
            else 0.0
        )
        if happy_prob or sad_prob or relaxed_prob:
            valence_score = round(max(happy_prob, relaxed_prob * 0.7, 1.0 - sad_prob if sad_prob else 0.5), 2)
            res["valence"] = f"{valence_score:.2f}"

    return res


def compute_bpm_from_pcm(flac_path: Path) -> str:
    """Compute BPM from FLAC PCM audio using pure-NumPy energy envelope autocorrelation."""
    try:
        audio = FLAC(str(flac_path))
        sr = audio.info.sample_rate
        channels = audio.info.channels
        bps = audio.info.bits_per_sample
        if sr <= 0 or channels <= 0:
            return ""

        if bps not in {8, 16, 24, 32}:
            return ""
        hop = max(1, sr // 1000)
        envelopes = []
        for block in dr14_parse.iter_flac_pcm(flac_path, block_frames=hop * 1000):
            mono = block.astype(np.float32).mean(axis=1)
            frames = len(mono) // hop
            if frames:
                envelopes.append(np.abs(mono[: frames * hop]).reshape(frames, hop).mean(axis=1))
        if not envelopes:
            return ""
        env = np.concatenate(envelopes)
        if len(env) < 2000:
            return ""
        env -= np.mean(env)

        fps = sr / hop
        min_lag = int(fps * 60.0 / 200.0)
        max_lag = int(fps * 60.0 / 60.0)
        if max_lag >= len(env):
            max_lag = len(env) - 1

        mid = len(env) // 2
        seg_len = int(fps * 30.0)
        start = max(0, mid - seg_len // 2)
        end = min(len(env), start + seg_len)
        seg = env[start:end]

        if np.max(np.abs(seg)) <= 1e-12:
            return ""
        corr = np.correlate(seg, seg, mode="full")
        corr = corr[len(seg) - 1 :]
        lags = np.arange(min_lag, max_lag)
        if len(lags) == 0:
            return ""
        best_lag = lags[np.argmax(corr[min_lag:max_lag])]
        bpm = round((fps * 60.0) / best_lag)
        if 50 <= bpm <= 220:
            return str(bpm)
    except Exception:
        pass
    return ""


def parse_rip_log_hardware_and_crc(rip_meta_files: list[Path], track_num: int) -> dict[str, str]:
    """Extract ripping software, device, read offset, encoder settings, and test/copy CRCs from EAC/XLD logs."""
    res = {
        "ripping_software": "",
        "ripping_device": "",
        "read_offset": "",
        "encoder_settings": "",
        "test_crc": "",
        "copy_crc": "",
    }
    for mf in rip_meta_files:
        if mf.suffix.lower() != ".log":
            continue
        raw = mf.read_bytes()
        text = ""
        enc_order: tuple[str, ...]
        if raw.startswith(b"\xff\xfe"):
            enc_order = ("utf-16-le", "utf-16")
        elif raw.startswith(b"\xfe\xff"):
            enc_order = ("utf-16-be", "utf-16")
        elif b"\x00" in raw[:200]:
            enc_order = ("utf-16-le", "utf-16", "utf-8", "cp1252")
        else:
            enc_order = ("utf-8-sig", "utf-8", "cp1252", "cp1251", "latin-1")

        for enc in enc_order:
            try:
                cand = raw.decode(enc)
                if "\x00" not in cand:
                    text = cand
                    break
            except Exception:
                continue
        if not text:
            continue
        text = text.replace("\r\n", "\n").replace("\r", "\n")

        # 1. Ripping software
        if not res["ripping_software"]:
            m_sw = re.search(
                r"(Exact Audio Copy\s+(?:V|beta\s+)?[0-9.]+|EAC\s+extraction\s+logfile|X Lossless Decoder\s+version\s+[0-9]+|whipper\s+[0-9.]+|dBpoweramp\s+Release\s+[0-9.]+)",
                text,
                re.IGNORECASE,
            )
            if m_sw:
                sw_cand = m_sw.group(1).strip()
                if "EAC extraction logfile" in sw_cand:
                    sw_cand = "Exact Audio Copy"
                res["ripping_software"] = sw_cand

        # 2. Ripping device (drive)
        if not res["ripping_device"]:
            m_dr = re.search(
                r"Used drive\s*:\s*([^,\n\r]+?)(?:\s+Adapter:|\s+ID:|\s*$)",
                text,
                re.MULTILINE | re.IGNORECASE,
            )
            if not m_dr:
                m_dr = re.search(r"Drive\s*:\s*([^,\n\r]+)", text, re.MULTILINE | re.IGNORECASE)
            if m_dr:
                res["ripping_device"] = re.sub(r"\s+", " ", m_dr.group(1)).strip()

        # 3. Read offset correction
        if not res["read_offset"]:
            m_off = re.search(r"Read offset correction\s*:\s*([+-]?\d+)", text, re.IGNORECASE)
            if m_off:
                val = int(m_off.group(1))
                res["read_offset"] = f"+{val}" if val > 0 else str(val)

        # 4. Encoder settings
        if not res["encoder_settings"]:
            m_enc = re.search(r"Additional command line options\s*:\s*([^\r\n]+)", text, re.IGNORECASE)
            if m_enc:
                raw_opts = m_enc.group(1).strip()
                m_flag = re.match(r"^([-\w\s]+?)(?:\s+-[tT]\b|\s+-[oO]\b|\s+%|\s*$)", raw_opts)
                res["encoder_settings"] = m_flag.group(1).strip() if m_flag else raw_opts[:40]

        # 5. Track-level CRC
        m_block = re.search(
            rf"^\s*Track\s+0*{track_num}\b(.*?)(?=^\s*Track\s+\d+\b|\Z)",
            text,
            flags=re.MULTILINE | re.DOTALL | re.IGNORECASE,
        )
        if m_block:
            blk = m_block.group(1)
            m_tc = re.search(r"Test CRC\s+([0-9A-Fa-f]{8})", blk, re.IGNORECASE)
            m_cc = re.search(r"Copy CRC\s+([0-9A-Fa-f]{8})", blk, re.IGNORECASE)
            if m_tc:
                res["test_crc"] = m_tc.group(1).upper()
            if m_cc:
                res["copy_crc"] = m_cc.group(1).upper()

        # Single image rip fallback (Range status)
        if not res["test_crc"] or not res["copy_crc"]:
            m_tc_range = re.search(r"Test CRC\s+([0-9A-Fa-f]{8})", text, re.IGNORECASE)
            m_cc_range = re.search(r"Copy CRC\s+([0-9A-Fa-f]{8})", text, re.IGNORECASE)
            if not res["test_crc"] and m_tc_range:
                res["test_crc"] = m_tc_range.group(1).upper()
            if not res["copy_crc"] and m_cc_range:
                res["copy_crc"] = m_cc_range.group(1).upper()

    return res


def parse_classical_attributes(title: str, work_title: str = "") -> dict[str, str]:
    """Parse classical metadata: Opus, Catalog number, Part, Movement total."""
    res: dict[str, str] = {}
    combined = f"{work_title} - {title}" if work_title else title

    # Catalog numbers: BWV, K., KV, D., Hob., RV, WoO
    m_cat = re.search(
        r"\b(BWV\s*\d+[a-z]?|K(?:V)?\.?\s*\d+[a-z]?|D\.?\s*\d+|Hob\.\s*[I|V|X]+[:.]?\d+|RV\s*\d+|WoO\s*\d+)\b",
        combined,
        re.IGNORECASE,
    )
    if m_cat:
        res["composition_catalog"] = re.sub(r"\s+", " ", m_cat.group(1)).strip()

    # Opus number
    m_op = re.search(r"\b(?:Opus|Op\.)\s*(\d+)(?:\s*(?:No\.|Nr\.)\s*(\d+))?", combined, re.IGNORECASE)
    if m_op:
        op_num = m_op.group(1)
        sub_no = m_op.group(2)
        if sub_no:
            res["opus"] = f"Op. {op_num} No. {sub_no}"
            res["opusnumber"] = op_num
        else:
            res["opus"] = f"Op. {op_num}"
            res["opusnumber"] = op_num

    # Part / Act / Scene / No.
    m_part = re.search(
        r"\b((?:Part|Act)\s+[0-9IVXLCDM]+(?:\s*,\s*(?:Scene|No\.|Nr\.)\s*\d+)?)\b",
        combined,
        re.IGNORECASE,
    )
    if m_part:
        res["part"] = m_part.group(1).strip()

    # Movement total
    m_mtot = re.search(
        r"\b(?:movement|mov(?:ement)?\.?|mvmt\.?)\s*[0-9IVX]+\s*(?:of|/)\s*([0-9IVX]+)\b|\b([0-9IVX]+)\s*/\s*([0-9IVX]+)\b",
        combined,
        re.IGNORECASE,
    )
    if m_mtot:
        tot = m_mtot.group(1) or m_mtot.group(3)
        if tot:
            res["movementtotal"] = tot

    return res


def parse_vinyl_attributes(formats: list[dict[str, Any]], notes: str = "") -> dict[str, str]:
    """Extract vinyl parameters and audiophile needle-drop playback equipment."""
    res: dict[str, str] = {
        "speed": "",
        "weight": "",
        "pressing": "",
        "turntable": "",
        "cartridge": "",
        "phono_preamp": "",
        "adc": "",
        "vinyl_condition": "",
    }
    all_descs: list[str] = []
    for fmt in formats:
        if isinstance(fmt, dict):
            for d in fmt.get("descriptions", []):
                all_descs.append(str(d).lower())

    # Speed
    for d in all_descs:
        if "33" in d:
            res["speed"] = "33 ⅓ RPM"
            break
        elif "45" in d:
            res["speed"] = "45 RPM"
            break
        elif "78" in d:
            res["speed"] = "78 RPM"
            break

    # Weight
    for d in all_descs:
        if "180" in d:
            res["weight"] = "180g"
            break
        elif "200" in d:
            res["weight"] = "200g"
            break

    # Pressing
    for d in all_descs:
        if "test pressing" in d:
            res["pressing"] = "Test Pressing"
            break
        elif "white label" in d:
            res["pressing"] = "White Label"
            break
        elif "first pressing" in d or "1st pressing" in d:
            res["pressing"] = "First Pressing"
            break
        elif "club edition" in d:
            res["pressing"] = "Club Edition"
            break

    # Audio equipment from rip notes / Discogs release notes
    if notes:
        m_tt = re.search(r"(?:turntable|deck|table)\s*[:=]\s*([^\r\n,;]+)", notes, re.IGNORECASE)
        if m_tt:
            res["turntable"] = m_tt.group(1).strip()
        m_cart = re.search(r"(?:cartridge|cart|stylus)\s*[:=]\s*([^\r\n,;]+)", notes, re.IGNORECASE)
        if m_cart:
            res["cartridge"] = m_cart.group(1).strip()
        m_pre = re.search(r"(?:phono(?:\s+preamp(?:lifier)?)?|preamp)\s*[:=]\s*([^\r\n,;]+)", notes, re.IGNORECASE)
        if m_pre:
            res["phono_preamp"] = m_pre.group(1).strip()
        m_adc = re.search(r"(?:adc|audio\s+interface|capture\s+device)\s*[:=]\s*([^\r\n,;]+)", notes, re.IGNORECASE)
        if m_adc:
            res["adc"] = m_adc.group(1).strip()
        m_cond = re.search(
            r"(?:(?:vinyl|record)?\s*condition|grading)\s*[:=]\s*(M|NM|VG\+|VG|G\+|G)\b", notes, re.IGNORECASE
        )
        if m_cond:
            res["vinyl_condition"] = m_cond.group(1).upper().strip()

    return res


def query_acoustid_candidates(
    flac_path: Path, session: requests.Session, cache_dir: Path | None = None
) -> tuple[list[str], set[str]]:
    """Lookup candidate MusicBrainz release IDs and recording IDs from AcoustID."""
    client_key = os.environ.get("ACOUSTID_API_KEY") or os.environ.get("ACOUSTID_CLIENT_KEY")
    if not client_key or not flac_path.is_file():
        return [], set()

    dur = 0
    fp = ""
    try:
        audio = FLAC(str(flac_path))
        if audio.tags:
            fp_tag = audio.tags.get("ACOUSTID_FINGERPRINT") or audio.tags.get("acoustid_fingerprint")
            if fp_tag and fp_tag[0]:
                fp = str(fp_tag[0]).strip()
        if audio.info and audio.info.length:
            dur = round(audio.info.length)
    except Exception:
        pass

    if not fp or dur <= 0:
        dur, fp = compute_acoustid_fingerprint(flac_path)

    if not fp or dur <= 0:
        return [], set()

    rel_ids: list[str] = []
    rec_ids: set[str] = set()

    try:
        params = urllib.parse.urlencode(
            {"client": client_key, "meta": "recordingids releases", "duration": dur, "fingerprint": fp}
        )
        url = f"https://api.acoustid.org/v2/lookup?{params}"
        cache = JsonCache((cache_dir or flac_path.parent / ".provider-cache") / "acoustid")
        response = fetch_json(
            session, url, headers={"User-Agent": "AudiophileArchTagger/6.0"}, cache=cache, attempts=3, timeout=8
        )
        if response.ok and isinstance(response.payload, dict):
            data = response.payload
            ranked_results = _rank_acoustid_results(data)
            for res in ranked_results:
                if not isinstance(res.get("recordings"), list):
                    continue
                for rec in res.get("recordings", []):
                    if not isinstance(rec, dict):
                        continue
                    rid = rec.get("id")
                    if rid:
                        rec_ids.add(str(rid))
                    for rel in rec.get("releases") or []:
                        if not isinstance(rel, dict):
                            continue
                        relid = rel.get("id")
                        if relid and relid not in rel_ids:
                            rel_ids.append(str(relid))
    except (OSError, ValueError, requests.RequestException) as exc:
        print(f"WARNING: AcoustID lookup failed for {flac_path.name}: {type(exc).__name__}", file=sys.stderr)

    return rel_ids, rec_ids


def resolve_target_albums(patterns: list[str], base_dir: Path) -> list[str]:
    if not patterns:
        # The pipeline is a library tool.  Personal curated mappings remain
        # optional hints, never an implicit restriction to one artist.
        patterns = [str(base_dir)]

    target_entries: list[str] = []
    discovered_folders = discogs_release_find.find_album_folders(patterns, base_dir=base_dir)

    for df in discovered_folders:
        if not df.is_dir():
            continue
        clean_df = str(df.relative_to(base_dir) if df.is_relative_to(base_dir) else df)
        matched_entry = ""
        for entry in CURATED_ALBUMS:
            eparts = entry.split(":::")
            if len(eparts) >= 2:
                efolder = eparts[1]
                eabs = (base_dir / efolder).resolve()
                dfabs = df.resolve()
                if efolder == clean_df or eabs == dfabs:
                    matched_entry = entry
                    break
        if matched_entry:
            target_entries.append(matched_entry)
        else:
            target_entries.append(f"auto:::{clean_df}:::auto")

    # Match curated album list by text/ID/keyword
    for pattern in patterns:
        clean_pat = pattern.strip().strip("./")
        if not clean_pat:
            continue
        for entry in CURATED_ALBUMS:
            entry_parts = entry.split(":::")
            curated_path = base_dir / entry_parts[1]
            if clean_pat.lower() in entry.lower() and curated_path.is_dir() and entry not in target_entries:
                target_entries.append(entry)

    return sorted(dict.fromkeys(target_entries))


def measure_album_dr14(
    folder: Path,
    flac_files: list[Path],
    threads: int = 4,
    artist: str = "",
    album: str = "",
    catalog_no: str = "",
    session: requests.Session | None = None,
    target_media: str = "CD",
    verbose: bool = False,
    report_sink: Callable[[str], None] | None = None,
) -> tuple[dict[str, tuple[str, str, str]], str, dict[str, str]]:
    """Measure or resolve DR14 for tracks and album, and cross-reference dr.loudness-war.info."""
    is_verbose = verbose or os.environ.get("AUDIOPHILE_VERBOSE") == "1"
    if is_verbose:
        print(f"[VERBOSE][DR14] Measuring / resolving Dynamic Range for {len(flac_files)} tracks...")
    dr_db_info: dict[str, str] = {}
    track_map: dict[str, tuple[str, str, str]] = {}
    album_dr = ""

    # 1. Existing report
    report_file = dr14_parse.find_report_in_dir(folder)
    if report_file and report_file.is_file():
        try:
            report_text = dr14_parse.read_text(report_file)
            analyzed = re.search(r"(?im)^\s*Analyzed:\s*(.*?)\s+/\s+(.*?)\s*$", report_text)
            if artist and album:
                if not analyzed or (
                    dr14_parse.norm(analyzed.group(1)) != dr14_parse.norm(artist)
                    or dr14_parse.norm(analyzed.group(2)) != dr14_parse.norm(album)
                ):
                    raise ValueError("report artist/album header does not match local album")
            entries, a_dr = dr14_parse.parse(report_text)
            resolved = dr14_parse.resolve_to_files(entries, flac_files)
            if len(entries) == len(flac_files) == len(resolved):
                track_map = {
                    str(Path(str(item["path"])).relative_to(folder))
                    if item.get("path")
                    else str(item.get("name", "")): (
                        str(item.get("dr", "")),
                        str(item.get("peak", "")),
                        str(item.get("rms", "")),
                    )
                    for item in resolved
                    if str(item.get("name", ""))
                }
                album_dr = str(a_dr) if a_dr is not None else ""
            else:
                print(
                    "WARNING: existing DR14 report does not map to every local FLAC; measuring audio instead.",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(f"WARNING: existing DR14 report could not be parsed: {exc}", file=sys.stderr)

    # 2. dr14_tmeter CLI if present and no existing report
    if not track_map and report_sink is None and shutil.which("dr14_tmeter"):
        try:
            res = subprocess.run(
                ["dr14_tmeter", "-1", "-b", "."],
                cwd=folder,
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode:
                raise RuntimeError(f"dr14_tmeter failed with exit code {res.returncode}")
            entries, a_dr = dr14_parse.parse(res.stdout)
            resolved = dr14_parse.resolve_to_files(entries, flac_files)
            if len(entries) == len(flac_files) == len(resolved):
                track_map = {
                    str(Path(str(item["path"])).relative_to(folder))
                    if item.get("path")
                    else str(item.get("name", "")): (
                        str(item.get("dr", "")),
                        str(item.get("peak", "")),
                        str(item.get("rms", "")),
                    )
                    for item in resolved
                    if str(item.get("name", ""))
                }
                album_dr = str(a_dr) if a_dr is not None else ""
        except Exception:
            pass

    # 3. Native parallel DR14 measurement via dr14_parse if still not resolved
    if not track_map:
        measured_tracks: dict[str, dict[str, Any]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
            futures = {ex.submit(dr14_parse.measure_audio_file, f): f for f in flac_files}
            for fut in concurrent.futures.as_completed(futures):
                f = futures[fut]
                try:
                    m = fut.result()
                    key = str(f.relative_to(folder))
                    measured_tracks[key] = {**m, "name": key}
                except Exception as e:
                    print(f"WARNING: failed to measure DR14 for {f.name}: {e}", file=sys.stderr)

        dr_values = []
        for f in flac_files:
            key = str(f.relative_to(folder))
            if key in measured_tracks:
                m = measured_tracks[key]
                dr_val = str(m.get("dr", ""))
                track_map[key] = (dr_val, str(m.get("peak", "")), str(m.get("rms", "")))
                if dr_val.isdigit():
                    dr_values.append(int(dr_val))

        album_dr = str(round(sum(dr_values) / len(dr_values))) if len(dr_values) == len(flac_files) else ""
        if len(measured_tracks) == len(flac_files) and album_dr:
            try:
                report_tracks = [measured_tracks[str(f.relative_to(folder))] for f in flac_files]
                rep_text = dr14_parse.generate_dr14_report(
                    artist=folder.parent.name if folder.parent != folder else "",
                    album=folder.name,
                    tracks=report_tracks,
                    album_dr=album_dr,
                )
                if report_sink is None:
                    dr14_parse.save_dr14_report(folder, rep_text)
                else:
                    report_sink(rep_text)
            except Exception:
                pass

    # 4. Cross-reference dr.loudness-war.info online database if artist and album provided
    if artist and album:
        try:
            cand_dr = int(album_dr) if album_dr and album_dr.isdigit() else None
            db_entries = dr14_parse.search_dr_database(artist, album, session=session)
            best_db = dr14_parse.match_dr_entry(
                db_entries,
                target_dr=cand_dr,
                catalog_no=catalog_no,
                target_media=target_media,
            )
            if best_db and best_db.get("id"):
                # The public DR database is useful for manual comparison, not
                # release identity or local measurement.  Never import its ID,
                # min/max or DR values into authoritative file metadata.
                if is_verbose:
                    print(
                        f"[VERBOSE][DR14] dr.loudness-war.info comparison candidate: Release #{best_db['id']} ({best_db.get('url')}); not tagged"
                    )
            elif is_verbose:
                print(
                    f"[VERBOSE][DR14] dr.loudness-war.info: No matching entry found for '{artist}' - '{album}' (evaluated {len(db_entries)} candidates)."
                )
        except Exception as exc:
            if is_verbose:
                print(f"[VERBOSE][DR14] dr.loudness-war.info lookup error: {exc}")

    if is_verbose and track_map:
        for fname, (t_dr, t_pk, t_rms) in sorted(track_map.items()):
            print(f"[VERBOSE][DR14] Track {fname:<35} -> DR{t_dr:<2} | Peak: {t_pk} | RMS: {t_rms}")
        if album_dr:
            print(f"[VERBOSE][DR14] Overall Album Dynamic Range: DR{album_dr}")

    return track_map, album_dr, dr_db_info


@isolated_worker_session
def process_single_track(
    f: Path,
    folder: Path,
    artist_name: str,
    album_clean: str,
    discogs_json: dict[str, Any],
    mb_recordings_json: dict[str, Any] | None,
    rip_meta_files: list[Path],
    dr_track_info: tuple[str, str, str] | None,
    album_dr: str,
    album_tags: dict[str, Any],
    session: requests.Session,
    user_agent: str,
    cache_dir_mb: Path,
    cover_file: Path | None,
    write_target: Path | None = None,
    provenance_file: Path | None = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> tuple[bool, str]:
    """Process a single FLAC track: metadata resolution, lyrics, DR, and tag writing."""
    is_verbose = verbose or os.environ.get("AUDIOPHILE_VERBOSE") == "1"
    stem = f.stem

    try:
        audio = FLAC(str(f))
        samples = audio.info.total_samples
        rate = audio.info.sample_rate
        track_duration = round(samples / rate, 2) if rate > 0 else 0.0
        current_title = audio.tags.get("TITLE", [""])[0] if audio.tags else ""
        current_artist = audio.tags.get("ARTIST", [""])[0] if audio.tags else ""
    except Exception as e:
        return False, f"Failed to read FLAC audio {f.name}: {e}"

    inferred_disc, inferred_track, filename_title, position_conflicts = library_layout.position_evidence(
        f, folder, audio.tags
    )
    if position_conflicts or inferred_disc is None or inferred_track is None:
        explanation = "; ".join(position_conflicts) or "no reliable disc/track position"
        return False, f"Review required for {f.name}: {explanation}"
    disc_val = str(inferred_disc)
    track_num = str(inferred_track)
    # Embedded title is user/library evidence; filenames are only a fallback
    # and matching signal, never an automatic typography rewrite.
    final_title = current_title or filename_title or stem
    final_artist = artist_name
    if is_verbose:
        print(
            f"[VERBOSE][TRACK] Processing '{f.name}' (Title: '{final_title}', Artist: '{final_artist}', Duration: {track_duration}s)"
        )
    resolved_tracknumber = track_num
    resolved_track_id = ""
    resolved_recording_id = ""
    resolved_artist_ids: list[str] = []
    resolved_artist_names: list[str] = []
    resolved_artist_sorts: list[str] = []
    resolved_isrcs: list[str] = []
    resolved_work_id = ""
    resolved_work_ids: list[str] = []
    resolved_work_title = ""
    resolved_language = ""
    resolved_recording_date = ""
    track_match_conf = "LOCAL"

    cue_track_title = ""
    for pf in rip_meta_files:
        if pf.suffix.lower() == ".cue":
            cue_track_title = extract_cue_track_title(pf, int(track_num) if track_num.isdigit() else 1)
            if cue_track_title:
                break

    offset = 1 if album_tags.get("leading_intro_variant") else 0
    if mb_recordings_json and track_duration > 0:
        try:
            tm = mb_track_match.resolve_mb_track(
                mb_recordings_json,
                disc=int(disc_val) if disc_val.isdigit() else 1,
                tracknumber=int(track_num) if track_num.isdigit() else 1,
                filename_title=filename_title,
                current_title=current_title,
                current_artist=current_artist,
                duration=track_duration,
                cue_title=cue_track_title,
                offset=offset,
            )
            if tm.get("ok"):
                resolved_track_id = tm.get("track_id", "")
                resolved_recording_id = tm.get("recording_id", "")
                resolved_tracknumber = str(tm.get("track_position", track_num))
                final_title = tm.get("title") or final_title
                final_artist = tm.get("artist_credit") or final_artist
                resolved_artist_ids = [aid for aid in tm.get("artist_ids", []) if aid]
                resolved_artist_names = [name for name in tm.get("artist_names", []) if name]
                resolved_artist_sorts = [asort for asort in tm.get("artist_sorts", []) if asort]
                for isrc_entry in tm.get("isrcs", []):
                    if isrc_entry not in resolved_isrcs:
                        resolved_isrcs.append(isrc_entry)
                track_match_conf = tm.get("confidence", "EXACT")
        except Exception as exc:
            print(f"WARNING: MusicBrainz track matching failed for {f.name}: {exc}", file=sys.stderr)

    # Composers, lyricists, conductors, arrangers, and performers
    track_composers: list[str] = []
    track_composer_sorts: list[str] = []
    track_lyricists: list[str] = []
    track_writers: list[str] = []
    track_arrangers: list[str] = []
    track_conductors: list[str] = []
    track_ensembles: list[str] = []
    track_performers: list[str] = []
    track_orchestras: list[str] = []
    track_choruses: list[str] = []
    track_soloists: list[str] = []
    track_librettists: list[str] = []
    track_performer_roles: list[tuple[str, str]] = []
    resolved_iswcs: list[str] = []

    # 1. MusicBrainz recording work-level relations (highest authority, curated ISWC works)
    if resolved_recording_id:
        rec_url = f"https://musicbrainz.org/ws/2/recording/{resolved_recording_id}?inc=work-rels+work-level-rels+artist-rels&fmt=json"
        code, rec_data = mb_get(rec_url, session, user_agent, cache_dir_mb)
        if code == 200 and isinstance(rec_data, dict):
            # Extract language from recording attributes
            if not resolved_language:
                rec_lang = rec_data.get("text-representation", {}).get("language", "")
                if rec_lang:
                    resolved_language = rec_lang
            for rel in rec_data.get("relations", []):
                rel_type = rel.get("type", "")
                art_name = clean_credit(rel.get("artist", {}).get("name", ""))
                if art_name:
                    if rel_type == "conductor" and art_name not in track_conductors:
                        track_conductors.append(art_name)
                    elif rel_type in ("arranger", "orchestrator") and art_name not in track_arrangers:
                        track_arrangers.append(art_name)
                    elif rel_type in ("orchestra", "orchestral") and art_name not in track_orchestras:
                        track_orchestras.append(art_name)
                    elif rel_type in ("chorus", "choir", "choral") and art_name not in track_choruses:
                        track_choruses.append(art_name)
                    elif rel_type == "soloist" and art_name not in track_soloists:
                        track_soloists.append(art_name)
                    elif rel_type in ("performer", "instrument", "vocal"):
                        attrs = rel.get("attributes", [])
                        role_desc = ", ".join(attrs) if attrs else rel_type
                        p_entry = format_performer(art_name, role_desc)
                        if p_entry and p_entry not in track_performers:
                            track_performers.append(p_entry)
                        for clean_role in parse_instrument_roles(role_desc):
                            r_tag = f"PERFORMER:{clean_role.replace(' ', '_')}"
                            if (r_tag, art_name) not in track_performer_roles:
                                track_performer_roles.append((r_tag, art_name))
                # A relationship's arbitrary begin date can mean membership,
                # touring, or an artist role.  Only explicit recording-event
                # relations are evidence for a recording date.
                if rel.get("type") in ("recording", "recorded at") and rel.get("begin") and not resolved_recording_date:
                    resolved_recording_date = rel.get("begin") or ""
                if rel.get("target_type") == "work" or rel.get("type") == "performance":
                    work = rel.get("work", {})
                    # Extract MUSICBRAINZ_WORKID and WORK title
                    if work.get("id"):
                        if not resolved_work_id:
                            resolved_work_id = work["id"]
                        if work["id"] not in resolved_work_ids:
                            resolved_work_ids.append(work["id"])
                    if not resolved_work_title and work.get("title"):
                        resolved_work_title = work["title"]
                    # Extract ISWC from work
                    work_iswcs = work.get("iswcs", [])
                    if isinstance(work_iswcs, list):
                        for iswc_cand in work_iswcs:
                            if iswc_cand and iswc_cand not in resolved_iswcs:
                                resolved_iswcs.append(iswc_cand)
                    elif isinstance(work.get("iswc"), str):
                        iswc_single = work["iswc"].strip()
                        if iswc_single and iswc_single not in resolved_iswcs:
                            resolved_iswcs.append(iswc_single)
                    # Extract language from work if not yet resolved
                    if not resolved_language:
                        work_lang = work.get("language", "")
                        if work_lang:
                            resolved_language = work_lang
                    for wrel in work.get("relations", []):
                        wtype = wrel.get("type", "")
                        art_info = wrel.get("artist", {})
                        who = clean_credit(art_info.get("name", ""))
                        who_sort = clean_credit(art_info.get("sort-name", ""))
                        if not who:
                            continue
                        if wtype == "composer" and who not in track_composers:
                            track_composers.append(who)
                            if who_sort and who_sort not in track_composer_sorts:
                                track_composer_sorts.append(who_sort)
                        if wtype == "lyricist" and who not in track_lyricists:
                            track_lyricists.append(who)
                        if wtype == "writer" and who not in track_writers:
                            track_writers.append(who)
                        if wtype in ("arranger", "orchestrator") and who not in track_arrangers:
                            track_arrangers.append(who)
                        if wtype in ("libretto", "librettist") and who not in track_librettists:
                            track_librettists.append(who)

    # A release text language is not evidence of a song/lyrics language.
    # Unknown and instrumental tracks deliberately remain untagged.

    # 2. Discogs track-level extraartists (if not already populated by MB)
    discogs_track_titles = [
        mb_track_match.norm(str(item.get("title") or ""))
        for item in discogs_json.get("tracklist", [])
        if item.get("type_", item.get("type", "track")) == "track"
    ]
    for trk in discogs_json.get("tracklist", []):
        t_pos = trk.get("position", "").strip()
        t_title = trk.get("title", "").strip()
        pos_match = (
            t_pos == track_num
            or t_pos == f"{disc_val}-{track_num}"
            or t_pos == f"{disc_val}.{track_num}"
            or (t_pos.isdigit() and int(t_pos) == int(track_num))
        )
        title_match = t_title.lower() == final_title.lower() or t_title.lower() == current_title.lower()
        # Position and title corroborate each other when both are present.
        # An OR match misassigns credits on repeated titles and multi-disc or
        # vinyl-style layouts.
        duration_compatible = True
        duration_text = str(trk.get("duration") or "")
        duration_match = re.fullmatch(r"(\d+):(\d{2})", duration_text)
        if duration_match and track_duration:
            discogs_duration = int(duration_match.group(1)) * 60 + int(duration_match.group(2))
            duration_compatible = abs(track_duration - discogs_duration) <= max(8.0, track_duration * 0.04)
        title_key = mb_track_match.norm(t_title)
        unique_title = bool(title_key and discogs_track_titles.count(title_key) == 1)
        unsupported_position = bool(t_pos and not re.fullmatch(r"\d+(?:[-.]\d+)?", t_pos))
        identity_match = (
            (title_match and duration_compatible and (pos_match or (unsupported_position and unique_title)))
            if t_pos and t_title
            else (pos_match or (title_match and duration_compatible and unique_title))
        )
        if identity_match:
            if not resolved_recording_id and trk.get("artists"):
                credit_parts: list[str] = []
                structured_names: list[str] = []
                for credit in trk.get("artists") or []:
                    credit_name = clean_credit(credit.get("anv") or credit.get("name") or "")
                    if not credit_name:
                        continue
                    structured_names.append(credit_name)
                    credit_parts.append(credit_name + str(credit.get("join") or ""))
                if credit_parts:
                    final_artist = "".join(credit_parts).strip()
                    resolved_artist_names = structured_names
            for ea in trk.get("extraartists", []):
                role_l = ea.get("role", "").lower()
                who = clean_credit(ea.get("name", ""))
                if not who:
                    continue
                # Discogs role prose is not a license to conflate text and
                # composition.  Preserve generic writing as WRITER.
                if re.search(r"\b(composed|composer|music by)\b", role_l):
                    if who not in track_composers:
                        track_composers.append(who)
                if re.search(r"\b(lyrics|words|libretto)\b", role_l) and who not in track_lyricists:
                    track_lyricists.append(who)
                if re.search(r"\b(written|writer)\b", role_l) and who not in track_writers:
                    track_writers.append(who)
                if re.search(r"\b(arrang(ed|er|ement)?|orchestrat(ed|or|ion)?)\b", role_l):
                    if who not in track_arrangers:
                        track_arrangers.append(who)
                if re.search(r"conductor|dirigent|choir\s+director", role_l):
                    if who not in track_conductors:
                        track_conductors.append(who)
                if re.search(r"\b(orchestra|philharmonic|symphony)\b", role_l):
                    if who not in track_orchestras:
                        track_orchestras.append(who)
                if re.search(r"\b(choir|chorus|choral)\b", role_l) and who not in track_choruses:
                    track_choruses.append(who)
                if re.search(r"\bsoloist\b", role_l) and who not in track_soloists:
                    track_soloists.append(who)
                if re.search(r"\b(libretto|librettist)\b", role_l) and who not in track_librettists:
                    track_librettists.append(who)
                if re.search(r"orchestra|ensemble|quartet|trio|band|choir", role_l):
                    if who not in track_ensembles:
                        track_ensembles.append(who)
                if re.search(
                    r"guitar|bass|drums|vocals?|keyboard|piano|strings|brass|flute|sax|violin|cello|performer|soloist",
                    role_l,
                ):
                    p_entry = format_performer(who, ea.get("role", ""))
                    if p_entry and p_entry not in track_performers:
                        track_performers.append(p_entry)
                    for clean_role in parse_instrument_roles(ea.get("role", "")):
                        r_tag = f"PERFORMER:{clean_role.replace(' ', '_')}"
                        if (r_tag, who) not in track_performer_roles:
                            track_performer_roles.append((r_tag, who))
            break

    # 3. Preserve existing FLAC composers if still missing
    if not track_composers and audio.tags:
        for ec in audio.tags.get("COMPOSER", []):
            c_clean = clean_credit(ec)
            if c_clean and c_clean not in track_composers:
                track_composers.append(c_clean)

    # Release-level credits are album facts.  They are not copied onto every
    # recording when a track credit is missing.

    if audio.tags:
        for k, vals in audio.tags.items():
            if k.upper().startswith("PERFORMER:"):
                role_sub = k.upper()[len("PERFORMER:") :]
                k_norm = f"PERFORMER:{role_sub.replace(' ', '_')}"
                for v in vals:
                    if (k_norm, v) not in track_performer_roles:
                        track_performer_roles.append((k_norm, v))

    # Always initialize track number integer representation early to prevent UnboundLocalError
    t_int = int(track_num) if str(track_num).isdigit() else 1

    # Pre-emphasis check (check existing FLAC tags first, then CUE/LOG)
    track_preemphasis = False
    deemphasis_applied = False
    for pf in rip_meta_files:
        if pf.suffix.lower() == ".log" and log_has_deemphasis_applied(pf):
            deemphasis_applied = True
            break

    if audio.tags:
        pe_tags = (
            audio.tags.get("PRE_EMPHASIS", []) or audio.tags.get("PRE-EMPHASIS", []) or audio.tags.get("FLAGS", [])
        )
        if any(v in ("1", "yes", "PRE") for v in pe_tags):
            track_preemphasis = True
        de_tags = audio.tags.get("DE_EMPHASIS_APPLIED", [])
        if any(v in ("1", "yes", "true") for v in de_tags):
            deemphasis_applied = True

    if not track_preemphasis and not deemphasis_applied:
        for pf in rip_meta_files:
            if pf.suffix.lower() == ".cue":
                if cue_track_has_preemphasis(pf, t_int):
                    track_preemphasis = True
                    break
            elif pf.suffix.lower() == ".log" and log_track_has_preemphasis(pf, t_int):
                track_preemphasis = True
                break
        if not track_preemphasis and album_tags.get("is_preemphasis"):
            track_preemphasis = True

    # If de-emphasis was already applied, do NOT mark track_preemphasis to prevent double de-emphasis
    if deemphasis_applied:
        track_preemphasis = False

    # HDCD check (existing FLAC tags or CUE/LOG indication)
    track_hdcd = False
    track_hdcd_peak_extend = False
    if audio.tags:
        hdcd_tags = audio.tags.get("HDCD", []) or audio.tags.get("HDCD_DETECTED", [])
        if any(v in ("1", "yes", "true") for v in hdcd_tags):
            track_hdcd = True
        pe_tags = audio.tags.get("HDCD_PEAK_EXTEND", [])
        if any(v in ("1", "yes", "true") for v in pe_tags):
            track_hdcd_peak_extend = True
    if not track_hdcd:
        for pf in rip_meta_files:
            if pf.suffix.lower() in (".log", ".cue"):
                try:
                    txt = pf.read_text(encoding="utf-8", errors="replace")
                    if re.search(r"\bHDCD\b", txt, re.IGNORECASE) and not re.search(
                        r"HDCD\s*:\s*No", txt, re.IGNORECASE
                    ):
                        track_hdcd = True
                        if re.search(
                            r"peak\s+extend\s*:\s*(?:yes|true|1)|peak\s+extend\s+detected", txt, re.IGNORECASE
                        ):
                            track_hdcd_peak_extend = True
                        break
                except Exception:
                    pass
    if not track_hdcd and album_tags.get("is_hdcd"):
        track_hdcd = True
        if album_tags.get("is_hdcd_peak_extend"):
            track_hdcd_peak_extend = True

    # AccurateRip & CTDB metadata
    ar_data = get_track_accurip_data(rip_meta_files, t_int)

    # EAC Track Peak extraction
    track_eac_peak = get_track_eac_peak(rip_meta_files, t_int, flac_path=f)

    # Chromaprint / AcoustID fingerprinting
    acoust_dur, acoust_fp = compute_acoustid_fingerprint(f)
    resolved_acoustid_id = resolve_acoustid_id(acoust_dur, acoust_fp, session)
    if not resolved_acoustid_id and audio.tags:
        resolved_acoustid_id = audio.tags.get("ACOUSTID_ID", [""])[0]

    # DR14 resolution
    track_dr = ""
    track_dr_peak = ""
    track_dr_rms = ""
    if dr_track_info:
        track_dr, track_dr_peak, track_dr_rms = dr_track_info

    # Preserve local lyrics. Plain text permits lookup to add missing timing;
    # valid local synchronized lyrics stop remote lookup.
    lyrics_resolution = lyrics_pipeline.inspect_local(f, audio.tags, track_duration)
    lyrics_source = lyrics_resolution.source if lyrics_resolution else ""
    lyrics_status = lyrics_resolution.status if lyrics_resolution else ""
    lyrics_paths: dict[str, Path] = dict(lyrics_resolution.paths) if lyrics_resolution else {}
    if not dry_run and audio.tags:
        embedded_paths = lyrics_pipeline.materialize_embedded(
            write_target or f, audio.tags, existing_base=f, duration=track_duration
        )
        lyrics_paths = {**embedded_paths, **lyrics_paths}
        if lyrics_resolution:
            lyrics_resolution = lyrics_pipeline.LyricsResolution(
                lyrics_resolution.source,
                lyrics_resolution.status,
                lyrics_paths,
                assessment=lyrics_resolution.assessment,
                local=True,
                errors=lyrics_resolution.errors,
                needs_remote_lookup=lyrics_resolution.needs_remote_lookup,
                has_word_sync=lyrics_resolution.has_word_sync,
            )
    lyrics_has_word_sync = lyrics_resolution.has_word_sync if lyrics_resolution else False
    lyrics_has_bg_vocals = False
    selected_payload: dict[str, Any] = {}

    # Instrumental status is recorded separately; no fabricated lyric text is
    # inserted and existing lyrics are never removed.
    is_instrumental = bool(
        re.search(r"\b(instrumental|karaoke|backing\s+track)\b", final_title, re.IGNORECASE)
        or (audio.tags and any(v.strip() in ("1", "true", "yes") for v in audio.tags.get("INSTRUMENTAL", [])))
    )
    if is_instrumental:
        lyrics_source = "Instrumental"
        lyrics_status = "INSTRUMENTAL"
        lyrics_has_word_sync = False
        lyrics_has_bg_vocals = False

    if (
        (lyrics_resolution is None or lyrics_resolution.needs_remote_lookup)
        and not is_instrumental
        and load_config().lyrics.online_lookup
    ):
        provider_calls = (
            lambda: lyrics_utils.fetch_betterlyrics(
                song=final_title,
                artist=final_artist,
                album=album_clean,
                duration=track_duration,
                audio_path=f,
                enable_neural=load_config().lyrics.generated_candidates,
                verbose=is_verbose,
                collect_candidates=True,
                isrc=resolved_isrcs[0] if resolved_isrcs else "",
                recording_id=resolved_recording_id,
                language=resolved_language,
            ),
        )
        provider_results: list[dict[str, Any] | None] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(provider_calls)) as lyrics_executor:
            futures = [lyrics_executor.submit(call) for call in provider_calls]
            for future in futures:
                try:
                    provider_result = future.result()
                    provider_results.append(provider_result)
                except Exception as exc:
                    status, detail = lyrics_utils.classify_lyrics_exception(exc)
                    provider_results.append({"acquisition_status": status, "error": detail})
                    if is_verbose:
                        print(f"[VERBOSE][LYRICS] Provider error for '{final_title}': {exc}")
        local_plain = ""
        if lyrics_resolution and lyrics_resolution.local:
            plain_path = lyrics_paths.get("plain")
            if plain_path:
                local_plain = plain_path.read_text(encoding="utf-8")
            elif audio.tags:
                local_plain = next(iter(audio.tags.get("UNSYNCEDLYRICS", []) or audio.tags.get("LYRICS", [])), "")
        selected_lyrics = lyrics_pipeline.choose_provider_result(
            provider_results,
            title=final_title,
            artist=final_artist,
            album=album_clean,
            duration=track_duration,
            language=resolved_language,
            isrc=resolved_isrcs[0] if resolved_isrcs else "",
            recording_id=resolved_recording_id,
            verbose=is_verbose,
            local_plain=local_plain,
        )
        remote_resolution = None
        materialization_failed = False
        if selected_lyrics and not dry_run:
            remote_resolution = lyrics_pipeline.materialize_provider_results(
                write_target or f,
                provider_results,
                existing_base=f,
                title=final_title,
                artist=final_artist,
                album=album_clean,
                duration=track_duration,
                language=resolved_language,
                isrc=resolved_isrcs[0] if resolved_isrcs else "",
                recording_id=resolved_recording_id,
                verbose=is_verbose,
                local_plain=local_plain,
            )
            materialization_failed = remote_resolution is None
            selected_lyrics = remote_resolution.assessment if remote_resolution else None
        if selected_lyrics:
            if is_verbose:
                cand = selected_lyrics.candidate
                print(
                    "[VERBOSE][LYRICS] Candidate decision: "
                    f"provider={cand.provider!r} match={selected_lyrics.match_confidence:.1f} "
                    f"version={selected_lyrics.version_confidence:.1f} "
                    f"text={selected_lyrics.text_quality:.1f} sync={selected_lyrics.sync_quality:.1f} "
                    f"source={selected_lyrics.source_confidence:.1f} "
                    f"warnings={'; '.join(selected_lyrics.warnings)} "
                    f"rejections={'; '.join(selected_lyrics.reasons)}"
                )
            if not dry_run:
                # Remote sidecars are generated beside the staged FLAC. They
                # become visible only with the album transaction commit.
                old_paths = dict(lyrics_resolution.paths) if lyrics_resolution and lyrics_resolution.local else {}
                assert remote_resolution is not None
                merged_paths = {**remote_resolution.paths, **old_paths}
                lyrics_resolution = lyrics_pipeline.LyricsResolution(
                    remote_resolution.source,
                    remote_resolution.status,
                    merged_paths,
                    assessment=remote_resolution.assessment,
                    errors=remote_resolution.errors,
                    has_word_sync=remote_resolution.has_word_sync
                    or bool(lyrics_resolution and lyrics_resolution.has_word_sync),
                )
                lyrics_source = lyrics_resolution.source
                lyrics_status = lyrics_resolution.status
                lyrics_paths = dict(lyrics_resolution.paths)
            else:
                lyrics_source = selected_lyrics.candidate.provider
                lyrics_status = "DRY_RUN_CANDIDATE"
            raw_selected_payload = selected_lyrics.candidate.metadata.get("result")
            selected_payload = dict(raw_selected_payload) if isinstance(raw_selected_payload, Mapping) else {}
            lyrics_has_word_sync = bool(selected_payload.get("has_word_sync"))
            lyrics_has_bg_vocals = bool(selected_payload.get("has_back_vocals"))
            if not resolved_language and selected_lyrics.candidate.language:
                resolved_language = selected_lyrics.candidate.language
        else:
            lookup_status = (
                "MATERIALIZATION_ERROR"
                if materialization_failed
                else lyrics_pipeline.unsuccessful_lookup_status(provider_results)
            )
            if lookup_status == "INSTRUMENTAL" and lyrics_resolution is None:
                evidence = lyrics_pipeline.verified_instrumental_result(provider_results)
                is_instrumental = True
                lyrics_source = str(evidence.get("source") or "Provider metadata") if evidence else "Provider metadata"
                lyrics_status = "INSTRUMENTAL"
                lyrics_has_word_sync = False
                lyrics_has_bg_vocals = False
                if evidence:
                    selected_payload = dict(evidence)
                    if not dry_run:
                        lyrics_paths = lyrics_pipeline.archive_instrumental_evidence(
                            write_target or f,
                            evidence,
                            existing_base=f,
                        )
            else:
                lyrics_status = f"PRESERVED_{lookup_status}" if lyrics_resolution else lookup_status

    # Assemble Vorbis Comments
    manifest_tags: list[tuple[str, str]] = []

    final_album = album_tags.get("final_album", album_clean)
    manifest_tags.append(("ALBUM", final_album))
    album_artist = album_tags.get("album_artist") or artist_name
    manifest_tags.append(("ALBUMARTIST", album_artist))
    # Never parse credited display strings on punctuation. Atomic artists only
    # come from structured credits; without those, retain the display credit as
    # one value.
    for album_artist_part in album_tags.get("album_artist_parts", [album_artist]):
        manifest_tags.append(("ALBUMARTISTS", album_artist_part))
    if album_tags.get("mb_albumartist_sort"):
        manifest_tags.append(("ALBUMARTISTSORT", album_tags["mb_albumartist_sort"]))
    # Sort names are identifiers for display/indexing, not an English-language
    # formatting exercise.  Omit them unless the provider supplied one.
    if album_tags.get("mb_album_sort"):
        manifest_tags.append(("ALBUMSORT", album_tags["mb_album_sort"]))
    split_art_list = resolved_artist_names or [final_artist]
    manifest_tags.append(("ARTIST", final_artist))
    for a in split_art_list:
        manifest_tags.append(("ARTISTS", a))

    if len(resolved_artist_sorts) == 1:
        manifest_tags.append(("ARTISTSORT", resolved_artist_sorts[0]))

    if album_tags.get("barcode"):
        manifest_tags.append(("BARCODE", album_tags["barcode"]))
    for cn in album_tags.get("catnos", []):
        manifest_tags.append(("CATALOGNUMBER", cn))
    if not album_tags.get("catnos") and album_tags.get("catno"):
        manifest_tags.append(("CATALOGNUMBER", album_tags["catno"]))

    press_dt = album_tags.get("press_date", "")
    orig_dt = album_tags.get("orig_date", "")
    edition_yr = album_tags.get("edition_year", "")
    discogs_dt = album_tags.get("discogs_release_date", "")

    physical_date = press_dt or edition_yr or discogs_dt or orig_dt
    # DATE is the date of this exact edition. ORIGINALDATE is the earliest
    # reliably known release date. Keeping those meanings stable prevents a
    # remaster from masquerading as the original pressing.
    tag_date = physical_date
    tag_year = album_tags.get("press_year") or (physical_date[:4] if len(physical_date) >= 4 else "")

    if tag_date:
        manifest_tags.append(("DATE", tag_date))
    tag_year = tag_year or (tag_date[:4] if len(tag_date) >= 4 else "")
    if tag_year:
        manifest_tags.append(("YEAR", tag_year[:4]))

    if album_tags.get("press_date_source"):
        manifest_tags.append(("DATE_SOURCE", album_tags["press_date_source"]))

    manifest_tags.append(("DISCNUMBER", disc_val))
    if album_tags.get("master_id"):
        manifest_tags.append(("DISCOGS_MASTER_ID", str(album_tags["master_id"])))
    if album_tags.get("discogs_notes"):
        manifest_tags.append(("DISCOGS_NOTES", album_tags["discogs_notes"]))
    if album_tags.get("discogs_id"):
        manifest_tags.append(("DISCOGS_RELEASE_ID", str(album_tags["discogs_id"])))
        manifest_tags.append(("DISCOGS_RELEASE_URL", f"https://www.discogs.com/release/{album_tags['discogs_id']}"))

    total_discs = album_tags.get("total_discs", "1")
    manifest_tags.append(("DISCTOTAL", str(total_discs)))
    if load_config().preservation.mirror_total_aliases:
        manifest_tags.append(("TOTALDISCS", str(total_discs)))

    if album_tags.get("edition"):
        manifest_tags.append(("EDITION", album_tags["edition"]))
    if album_tags.get("freedb_discid"):
        manifest_tags.append(("FREEDB_DISCID", album_tags["freedb_discid"]))
    if album_tags.get("freedb_discid_source"):
        manifest_tags.append(("FREEDB_DISCID_SOURCE", album_tags["freedb_discid_source"]))

    for g in album_tags.get("genres", []):
        manifest_tags.append(("GENRE", g))

    for ln in album_tags.get("label_names", []):
        manifest_tags.append(("LABEL", ln))
    if not album_tags.get("label_names") and album_tags.get("label_name"):
        manifest_tags.append(("LABEL", album_tags["label_name"]))

    # COPYRIGHT from Discogs labels / companies (Xiph recommended field)
    copyright_val = album_tags.get("copyright", "")
    if copyright_val:
        manifest_tags.append(("COPYRIGHT", copyright_val))
    for rs in album_tags.get("rights_societies", []):
        manifest_tags.append(("RIGHTSSOCIETY", rs))

    if lyrics_source:
        manifest_tags.append(("LYRICS_SOURCE", lyrics_source))
    if lyrics_status:
        manifest_tags.append(("LYRICS_STATUS", lyrics_status))
    if is_instrumental:
        manifest_tags.append(("INSTRUMENTAL", "1"))
    if lyrics_has_word_sync:
        manifest_tags.append(("LYRICS_HAS_WORD_SYNC", "1"))
    if lyrics_has_bg_vocals:
        manifest_tags.append(("LYRICS_HAS_BACKGROUND_VOCALS", "1"))

    for mv in album_tags.get("matrix_values", []):
        manifest_tags.append(("MATRIX", mv))
    if album_tags.get("matrix_values"):
        manifest_tags.append(("MATRIX_SOURCE", "Discogs"))
    for msid in album_tags.get("mastering_sid_codes", []):
        manifest_tags.append(("MASTERING_SID_CODE", msid))
    for mould in album_tags.get("mould_sid_codes", []):
        manifest_tags.append(("MOULD_SID_CODE", mould))
    for daid in album_tags.get("discogs_artist_ids", []):
        manifest_tags.append(("DISCOGS_ARTIST_ID", daid))
    for pp in album_tags.get("pressing_plants", []):
        manifest_tags.append(("PRESSING_PLANT", pp))
        manifest_tags.append(("PRESSED_BY", pp))
    for mf in album_tags.get("manufacturers", []):
        manifest_tags.append(("MANUFACTURER", mf))
    for gm in album_tags.get("glass_mastered_by", []):
        manifest_tags.append(("GLASS_MASTERED_AT", gm))

    if album_tags.get("media"):
        manifest_tags.append(("MEDIA", album_tags["media"]))

    mb_match_kind = album_tags.get("mb_match_kind", "NONE")
    manifest_tags.append(("METADATA_MATCH", f"{mb_match_kind}/{track_match_conf}"))
    metadata_sources = ["Discogs"]
    if album_tags.get("mb_release_id") and resolved_recording_id:
        metadata_sources.append("MusicBrainz")
    for metadata_source in metadata_sources:
        manifest_tags.append(("METADATA_SOURCE", metadata_source))

    for album_artist_id in album_tags.get("mb_albumartist_ids", []) or (
        [album_tags["mb_albumartist_id"]] if album_tags.get("mb_albumartist_id") else []
    ):
        manifest_tags.append(("MUSICBRAINZ_ALBUMARTISTID", album_artist_id))
    if resolved_artist_ids:
        for aid in resolved_artist_ids:
            manifest_tags.append(("MUSICBRAINZ_ARTISTID", aid))

    if album_tags.get("mb_release_id"):
        manifest_tags.append(("MUSICBRAINZ_ALBUMID", album_tags["mb_release_id"]))
    # Disc IDs identify a physical medium, never the whole multi-disc box.
    disc_key = int(disc_val) if str(disc_val).isdigit() else 1
    medium_toc = (album_tags.get("mb_tocs") or {}).get(disc_key, {})
    per_disc_id = medium_toc.get("discid") if medium_toc.get("toc_exact") else album_tags.get("mb_discid")
    per_disc_source = medium_toc.get("source") or album_tags.get("mb_discid_source")
    if per_disc_id:
        manifest_tags.append(("MUSICBRAINZ_DISCID", per_disc_id))
    if per_disc_source:
        manifest_tags.append(("MUSICBRAINZ_DISCID_SOURCE", per_disc_source))
    if resolved_recording_id:
        manifest_tags.append(("MUSICBRAINZ_TRACKID", resolved_recording_id))
    if album_tags.get("mb_release_group_id"):
        manifest_tags.append(("MUSICBRAINZ_RELEASEGROUPID", album_tags["mb_release_group_id"]))
    if resolved_track_id:
        manifest_tags.append(("MUSICBRAINZ_RELEASETRACKID", resolved_track_id))
    for work_id in resolved_work_ids or ([resolved_work_id] if resolved_work_id else []):
        manifest_tags.append(("MUSICBRAINZ_WORKID", work_id))
    if resolved_work_title:
        manifest_tags.append(("WORK", resolved_work_title))
    for iswc_entry in resolved_iswcs:
        manifest_tags.append(("ISWC", iswc_entry))

    for isrc_entry in resolved_isrcs:
        manifest_tags.append(("ISRC", isrc_entry))

    # COMPOSER: only write if actually resolved from Discogs/MB/existing tags
    for comp in track_composers:
        manifest_tags.append(("COMPOSER", comp))
    for csort in track_composer_sorts:
        manifest_tags.append(("COMPOSERSORT", csort))
    for lyr in track_lyricists:
        manifest_tags.append(("LYRICIST", lyr))
    for writer in track_writers:
        manifest_tags.append(("WRITER", writer))

    # ORIGINALDATE and ORIGINALYEAR are written only from independently
    # resolved earliest-release evidence.  An edition that lacks a visible
    # reissue marker is not proof that it was the first issue.
    orig_dt = album_tags.get("orig_date", "")
    orig_yr = album_tags.get("orig_year", "")

    if orig_dt:
        manifest_tags.append(("ORIGINALDATE", orig_dt))
    if orig_yr:
        manifest_tags.append(("ORIGINALYEAR", orig_yr[:4]))

    mb_orig_dt = album_tags.get("mb_orig_date", "")
    if mb_orig_dt:
        manifest_tags.append(("MUSICBRAINZ_ORIGINALRELEASEDATE", mb_orig_dt))

    rel_country = album_tags.get("rel_country", "")
    if rel_country:
        manifest_tags.append(("RELEASECOUNTRY", rel_country))
        manifest_tags.append(("COUNTRY", rel_country))

    if album_tags.get("mb_release_status"):
        manifest_tags.append(("RELEASESTATUS", album_tags["mb_release_status"]))
    if album_tags.get("mb_release_type"):
        mb_rtype = str(album_tags["mb_release_type"]).strip().lower()
        manifest_tags.append(("RELEASETYPE", mb_rtype))
    is_compilation = bool(
        album_tags.get("is_compilation", False)
        or "compilation" in {str(item).casefold() for item in album_tags.get("release_types", [])}
        or album_tags.get("mb_release_type") == "compilation"
        or artist_name.lower() in ("various artists", "various", "va")
        or final_artist.lower() in ("various artists", "various", "va")
    )
    if is_compilation:
        manifest_tags.append(("COMPILATION", "1"))

    if album_tags.get("rip_toc_source"):
        manifest_tags.append(("RIP_TOC_SOURCE", album_tags["rip_toc_source"]))
    if album_tags.get("rip_toc_tracks"):
        manifest_tags.append(("RIP_TOC_TRACKS", str(album_tags["rip_toc_tracks"])))

    for s in album_tags.get("styles", []):
        manifest_tags.append(("STYLE", s))
    for p in album_tags.get("producers", []):
        manifest_tags.append(("PRODUCER", p))
    for a in track_arrangers or album_tags.get("arrangers", []):
        manifest_tags.append(("ARRANGER", a))
    for m in album_tags.get("mastered_by", []):
        manifest_tags.append(("MASTERED_BY", m))
        manifest_tags.append(("MASTERINGENGINEER", m))
    for r in album_tags.get("remastered_by", []):
        manifest_tags.append(("REMASTERED_BY", r))
        manifest_tags.append(("REMASTERER", r))
        manifest_tags.append(("REMASTERINGENGINEER", r))
    for engineer in album_tags.get("engineers", []):
        manifest_tags.append(("ENGINEER", engineer))
    for x in album_tags.get("mixers", []):
        manifest_tags.append(("MIXER", x))
        manifest_tags.append(("MIXINGENGINEER", x))
    for c in track_conductors or album_tags.get("conductors", []):
        manifest_tags.append(("CONDUCTOR", c))
    for en in track_ensembles or album_tags.get("ensembles", []):
        manifest_tags.append(("ENSEMBLE", en))
    for orchestra in track_orchestras or album_tags.get("orchestras", []):
        manifest_tags.append(("ORCHESTRA", orchestra))
    for chorus in track_choruses or album_tags.get("choruses", []):
        manifest_tags.append(("CHOIR", chorus))
    for soloist in track_soloists or album_tags.get("soloists", []):
        manifest_tags.append(("SOLOIST", soloist))
    for librettist in track_librettists or album_tags.get("librettists", []):
        manifest_tags.append(("LIBRETTIST", librettist))
    for performer_name in track_performers or album_tags.get("performers", []):
        manifest_tags.append(("PERFORMER", performer_name))
    for ms in album_tags.get("mastering_studios", []):
        manifest_tags.append(("MASTERINGSTUDIO", ms))
    version_val = album_tags.get("track_version") or ""
    if version_val:
        manifest_tags.append(("VERSION", version_val))

    for rs in album_tags.get("recording_studios", []):
        manifest_tags.append(("RECORDINGLOCATION", rs))
        manifest_tags.append(("RECORDINGSTUDIO", rs))
    for ms in album_tags.get("mixing_studios", []):
        manifest_tags.append(("MIXINGSTUDIO", ms))
    rec_date_final = resolved_recording_date or album_tags.get("recording_date", "")
    if rec_date_final:
        manifest_tags.append(("RECORDINGDATE", rec_date_final))

    mov_num, mov_name = parse_movement_info(final_title)
    if mov_num:
        manifest_tags.append(("MOVEMENT", mov_num))
        if mov_name:
            manifest_tags.append(("MOVEMENTNAME", mov_name))

    manifest_tags.append(("TAGGER", "AudiophileArchTagger"))
    manifest_tags.append(("TITLE", final_title))
    # TITLESORT: only write if different from TITLE (avoid redundant clutter)
    tsort = album_sort_value(final_title)
    if tsort != final_title:
        manifest_tags.append(("TITLESORT", tsort))

    # LANGUAGE: ISO 639 code from MB recording/work relations
    if resolved_language:
        manifest_tags.append(("LANGUAGE", resolved_language))
    lyrics_form = str(selected_payload.get("lyrics_form") or "")
    if lyrics_form == "original" and selected_payload.get("language"):
        manifest_tags.append(("LYRICS_LANGUAGE", str(selected_payload["language"])))
    if selected_payload.get("translationLanguage"):
        manifest_tags.append(("LYRICS_TRANSLATION_LANGUAGE", str(selected_payload["translationLanguage"])))
    if selected_payload.get("romanizationLanguage"):
        manifest_tags.append(("LYRICS_ROMAJI_LANGUAGE", str(selected_payload["romanizationLanguage"])))
    manifest_tags.append(("TRACKNUMBER", resolved_tracknumber))

    disc_tracktotal = str(
        album_tags.get("disc_track_counts", {}).get(
            int(disc_val), album_tags.get("resolved_tracktotal", len(album_tags.get("flac_files", [])))
        )
    )
    if mb_recordings_json:
        for med in mb_recordings_json.get("media", []):
            if str(med.get("position", "")) == disc_val:
                if med.get("track-count"):
                    disc_tracktotal = str(med["track-count"])
                break
    else:
        # A partial folder count is not the medium's track total. Prefer the
        # fetched release list, with physical CUE/TOC evidence as a single-disc
        # fallback, rather than counting the files currently present.
        release_counts = release_match.discogs_disc_counts((discogs_json or {}).get("tracklist") or [])
        if int(disc_val) in release_counts:
            disc_tracktotal = str(release_counts[int(disc_val)])
        elif int(album_tags.get("total_discs") or 1) == 1 and album_tags.get("resolved_tracktotal"):
            disc_tracktotal = str(album_tags["resolved_tracktotal"])

    # DISCSUBTITLE for multi-disc releases and box sets
    disc_sub = album_tags.get("disc_subtitles", {}).get(disc_val, "")
    if not disc_sub and mb_recordings_json:
        for med in mb_recordings_json.get("media", []):
            if str(med.get("position", "")) == disc_val and med.get("title"):
                disc_sub = clean_disc_subtitle(med["title"], disc_val)
                break
    if disc_sub:
        manifest_tags.append(("DISCSUBTITLE", disc_sub))

    manifest_tags.append(("TRACKTOTAL", disc_tracktotal))
    if load_config().preservation.mirror_total_aliases:
        manifest_tags.append(("TOTALTRACKS", disc_tracktotal))

    if ar_data["ar_result"]:
        manifest_tags.append(("ACCURATERIPRESULT", ar_data["ar_result"]))
    if ar_data["ar_crc"]:
        manifest_tags.append(("ACCURATERIPCRC", ar_data["ar_crc"]))
    if ar_data["ctdb_tocid"]:
        manifest_tags.append(("CTDB_TOCID", ar_data["ctdb_tocid"]))
    if ar_data["ctdb_status"]:
        manifest_tags.append(("CTDB_STATUS", ar_data["ctdb_status"]))

    if track_eac_peak:
        manifest_tags.append(("EAC_PEAK", track_eac_peak))
    if album_tags.get("album_eac_peaks"):
        manifest_tags.append(("EAC_PEAKS", album_tags["album_eac_peaks"]))
    if acoust_fp:
        manifest_tags.append(("ACOUSTID_FINGERPRINT", acoust_fp))
    if resolved_acoustid_id:
        manifest_tags.append(("ACOUSTID_ID", resolved_acoustid_id))

    if track_dr:
        manifest_tags.append(("DR", track_dr))
        manifest_tags.append(("DYNAMIC_RANGE", track_dr))
        if track_dr_peak:
            manifest_tags.append(("DR_PEAK", track_dr_peak))
        if track_dr_rms:
            manifest_tags.append(("DR_RMS", track_dr_rms))
        if album_dr:
            manifest_tags.append(("ALBUM_DR", album_dr))
            manifest_tags.append(("ALBUM_DYNAMIC_RANGE", album_dr))
        if album_tags.get("min_dr"):
            manifest_tags.append(("ALBUM_MIN_DR", str(album_tags["min_dr"])))
        if album_tags.get("max_dr"):
            manifest_tags.append(("ALBUM_MAX_DR", str(album_tags["max_dr"])))
        manifest_tags.append(("DR_SOURCE", album_tags.get("dr_source", dr14_parse.DR_ALGORITHM_VERSION)))

    if album_tags.get("dr_loudness_war_id"):
        manifest_tags.append(("DR_LOUDNESS_WAR_ID", str(album_tags["dr_loudness_war_id"])))
        manifest_tags.append(("DR_LOUDNESS_WAR_URL", str(album_tags["dr_loudness_war_url"])))

    if track_preemphasis:
        manifest_tags.append(("PRE_EMPHASIS", "1"))
        manifest_tags.append(("FLAGS", "PRE"))
    elif deemphasis_applied:
        manifest_tags.append(("DE_EMPHASIS_APPLIED", "1"))
        manifest_tags.append(("FLAGS", "DEEMPH_APPLIED"))
    if track_hdcd:
        manifest_tags.append(("HDCD", "1"))
        manifest_tags.append(("HDCD_DETECTED", "1"))
        if track_hdcd_peak_extend:
            manifest_tags.append(("HDCD_PEAK_EXTEND", "1"))
    if album_tags.get("spars_code"):
        manifest_tags.append(("SPARSCODE", album_tags["spars_code"]))

    # 1. Musical Analysis / DJ Tags (AcousticBrainz + pure-NumPy PCM fallback)
    ab_cache_parent = cache_dir_mb.parent if cache_dir_mb else None
    ab_data = get_acousticbrainz_data(resolved_recording_id, session, ab_cache_parent)
    bpm_val = ab_data.get("bpm") or ""
    bpm_source = "AcousticBrainz legacy dataset" if bpm_val else ""
    if not bpm_val:
        bpm_val = compute_bpm_from_pcm(f)
        if bpm_val:
            bpm_source = "local numpy autocorrelation v1"
    if not bpm_val and audio.tags:
        bpm_val = audio.tags.get("BPM", [""])[0] or audio.tags.get("TEMPO", [""])[0]
    if bpm_val:
        manifest_tags.append(("BPM", bpm_val))
        manifest_tags.append(("TEMPO", bpm_val))
        manifest_tags.append(("BPM_SOURCE", bpm_source or "existing value"))

    key_val = ab_data.get("key") or ""
    if not key_val and audio.tags:
        key_val = audio.tags.get("KEY", [""])[0] or audio.tags.get("INITIALKEY", [""])[0]
    if key_val:
        manifest_tags.append(("KEY", key_val))
        manifest_tags.append(
            ("KEY_SOURCE", "AcousticBrainz legacy dataset" if ab_data.get("key") else "existing value")
        )
        camelot = musical_key_to_camelot(key_val)
        if camelot:
            manifest_tags.append(("INITIALKEY", camelot))
            manifest_tags.append(("CAMELOT_KEY", camelot))
        else:
            manifest_tags.append(("INITIALKEY", key_val))

    dance_val = ab_data.get("danceability") or ""
    if not dance_val and audio.tags:
        dance_val = audio.tags.get("DANCEABILITY", [""])[0]
    if dance_val:
        manifest_tags.append(("DANCEABILITY", dance_val))

    moods = ab_data.get("moods") or []
    if not moods and audio.tags:
        moods = [m for m in audio.tags.get("MOOD", []) if m]
    for md in moods:
        manifest_tags.append(("MOOD", md))

    energy_val = ab_data.get("energy") or ""
    if not energy_val and audio.tags:
        energy_val = audio.tags.get("ENERGY", [""])[0]
    if energy_val:
        manifest_tags.append(("ENERGY", energy_val))

    valence_val = ab_data.get("valence") or ""
    if not valence_val and audio.tags:
        valence_val = audio.tags.get("VALENCE", [""])[0]
    if valence_val:
        manifest_tags.append(("VALENCE", valence_val))
    if any(ab_data.get(name) for name in ("bpm", "key", "danceability", "moods", "energy", "valence")):
        manifest_tags.append(("ANALYSIS_SOURCE", "AcousticBrainz legacy dataset; recording MBID matched"))

    # 2. Classical Attributes & Roles
    classical_attrs = parse_classical_attributes(final_title, resolved_work_title) if resolved_work_id else {}
    if classical_attrs.get("opus"):
        manifest_tags.append(("OPUS", classical_attrs["opus"]))
    if classical_attrs.get("opusnumber"):
        manifest_tags.append(("OPUSNUMBER", classical_attrs["opusnumber"]))
    if classical_attrs.get("composition_catalog"):
        manifest_tags.append(("COMPOSITION_CATALOG", classical_attrs["composition_catalog"]))
        manifest_tags.append(("COMPOSITION_ID", classical_attrs["composition_catalog"]))
    if classical_attrs.get("part"):
        manifest_tags.append(("PART", classical_attrs["part"]))
    if classical_attrs.get("movementtotal"):
        manifest_tags.append(("MOVEMENTTOTAL", classical_attrs["movementtotal"]))

    for orch in track_orchestras or album_tags.get("orchestras", []):
        manifest_tags.append(("ORCHESTRA", orch))
    for cho in track_choruses or album_tags.get("choruses", []):
        manifest_tags.append(("CHORUS", cho))
        manifest_tags.append(("CHOIR", cho))
    for sol in track_soloists or album_tags.get("soloists", []):
        manifest_tags.append(("SOLOIST", sol))
    for lib in track_librettists or album_tags.get("librettists", []):
        manifest_tags.append(("LIBRETTIST", lib))

    # 3. Rip Log Hardware & CRC
    rip_hw = parse_rip_log_hardware_and_crc(rip_meta_files, t_int)
    if rip_hw["ripping_software"]:
        manifest_tags.append(("RIPPING_SOFTWARE", rip_hw["ripping_software"]))
        manifest_tags.append(("RIPPER", rip_hw["ripping_software"]))
    if rip_hw["ripping_device"]:
        manifest_tags.append(("RIPPING_DEVICE", rip_hw["ripping_device"]))
        manifest_tags.append(("OPTICAL_DRIVE", rip_hw["ripping_device"]))
    if rip_hw["read_offset"]:
        manifest_tags.append(("READ_OFFSET", rip_hw["read_offset"]))
    if rip_hw["encoder_settings"]:
        manifest_tags.append(("ENCODERSETTINGS", rip_hw["encoder_settings"]))
    if rip_hw["test_crc"]:
        manifest_tags.append(("TEST_CRC", rip_hw["test_crc"]))
    if rip_hw["copy_crc"]:
        manifest_tags.append(("COPY_CRC", rip_hw["copy_crc"]))

    # 4. Vinyl parameters & Needle-drop Hardware
    if album_tags.get("speed"):
        manifest_tags.append(("SPEED", album_tags["speed"]))
    if album_tags.get("weight"):
        manifest_tags.append(("WEIGHT", album_tags["weight"]))
    if album_tags.get("pressing"):
        manifest_tags.append(("PRESSING", album_tags["pressing"]))
    if album_tags.get("turntable"):
        manifest_tags.append(("TURNTABLE", album_tags["turntable"]))
    if album_tags.get("cartridge"):
        manifest_tags.append(("CARTRIDGE", album_tags["cartridge"]))
    if album_tags.get("phono_preamp"):
        manifest_tags.append(("PHONO_PREAMP", album_tags["phono_preamp"]))
    if album_tags.get("adc"):
        manifest_tags.append(("ADC", album_tags["adc"]))
    if album_tags.get("vinyl_condition"):
        manifest_tags.append(("VINYL_CONDITION", album_tags["vinyl_condition"]))

    # 5. Live & Venue Context
    is_live_track = False
    venue_val = album_tags.get("venue", "")
    loc_val = album_tags.get("location", "")

    if re.search(r"\b(live|concert|tour)\b", final_title, re.IGNORECASE):
        is_live_track = True
        m_live = re.search(r"\blive\s+at\s+([^,()]+)(?:,\s*([^()]+))?", final_title, re.IGNORECASE)
        if m_live:
            venue_cand = m_live.group(1).strip()
            loc_cand = m_live.group(2).strip() if m_live.group(2) else ""
            if venue_cand and not venue_val:
                venue_val = venue_cand
            if loc_cand and not loc_val:
                loc_val = loc_cand
    elif album_tags.get("is_live"):
        is_live_track = True

    if is_live_track:
        manifest_tags.append(("LIVE", "1"))
    if venue_val:
        manifest_tags.append(("VENUE", venue_val))
    if loc_val:
        manifest_tags.append(("LOCATION", loc_val))

    # 6. Grouping.  DISCSUBTITLE is the interoperable medium-title field;
    # emitting DISCNAME as a duplicate creates two independently editable
    # sources of truth.
    grouping_val = resolved_work_title or album_tags.get("grouping", "")
    if grouping_val:
        manifest_tags.append(("GROUPING", grouping_val))

    # Stream properties (sample rate, channels, bit depth and FLAC MD5) already
    # live in STREAMINFO. Duplicating them as comments creates stale values and
    # previously mislabeled 16/48 files as Red Book CDs.

    # 8. Source Format & Audiophile Series
    source_fmt = album_tags.get("source_format")
    if source_fmt:
        manifest_tags.append(("SOURCE_FORMAT", source_fmt))
    audiophile_ser = album_tags.get("audiophile_series")
    if audiophile_ser:
        manifest_tags.append(("AUDIOPHILE_SERIES", audiophile_ser))
    for rtype in album_tags.get("release_types", []):
        r_clean = str(rtype).strip().lower()
        if r_clean:
            manifest_tags.append(("RELEASETYPE", r_clean))

    # 9. Studio & Recording Date
    studio_val = album_tags.get("studio") or (
        album_tags.get("recording_studios", [""])[0] if album_tags.get("recording_studios") else ""
    )
    if studio_val:
        manifest_tags.append(("STUDIO", studio_val))
        manifest_tags.append(("RECORDINGSTUDIO", studio_val))

    # 10. Performer Instrument Roles
    for p_tag, p_val in track_performer_roles:
        manifest_tags.append((p_tag, p_val))

    # Write tags to FLAC file
    lyrics_file = str(lyrics_paths["plain"]) if "plain" in lyrics_paths else None
    synced_lyrics_file = str(lyrics_paths["synced"]) if "synced" in lyrics_paths else None
    enhanced_lyrics_file = str(lyrics_paths["enhanced"]) if "enhanced" in lyrics_paths else None
    ttml_file = str(lyrics_paths["ttml"]) if "ttml" in lyrics_paths else None
    translation_file = str(lyrics_paths["translation"]) if "translation" in lyrics_paths else None
    romaji_file = str(lyrics_paths["romanization"]) if "romanization" in lyrics_paths else None
    picture_file = str(cover_file) if cover_file and cover_file.is_file() and cover_file.stat().st_size > 0 else None

    embed_dim = album_tags.get("embed_max_dim", 1400)
    if is_verbose:
        print(
            f"[VERBOSE][METADATA] {'Planning' if dry_run else 'Writing'} {len(manifest_tags)} Vorbis comments to '{f.name}' (Artwork: {'Yes' if picture_file else 'No'})..."
        )
    release_confidence = (
        metadata_writer.Confidence.VERIFIED
        if mb_match_kind in {"CERTAIN", "EXACT"}
        else metadata_writer.Confidence.HIGH
        if mb_match_kind in {"VERY_HIGH", "HIGH"}
        else metadata_writer.Confidence.MEDIUM
    )
    configured_existing_confidence = getattr(
        metadata_writer.Confidence,
        str(load_config().preservation.existing_metadata_confidence).upper(),
        metadata_writer.Confidence.UNKNOWN,
    )
    try:
        raw_lyrics = str(selected_payload.get("rawLyrics") or "")
        provenance_context = {
            "logical_file": str(f.relative_to(folder)),
            "release_match": {
                "confidence": mb_match_kind,
                "score": album_tags.get("mb_match_score", 0),
                "margin": album_tags.get("mb_match_margin", 0),
                "musicbrainz_release_id": album_tags.get("mb_release_id", ""),
                "discogs_release_id": album_tags.get("discogs_id", ""),
            },
            "track_match": {"confidence": track_match_conf, "recording_id": resolved_recording_id},
            "album_completeness": {
                "state": album_tags.get("album_completeness_state", "UNKNOWN"),
                "evidence": album_tags.get("album_completeness_evidence", []),
            },
            "release_label_catalog_pairs": album_tags.get("label_catalog_pairs", []),
            "lyrics": {
                "source": lyrics_source,
                "status": lyrics_status,
                "provider_track_id": str(selected_payload.get("provider_track_id") or selected_payload.get("id") or ""),
                "raw_sha256": hashlib.sha256(raw_lyrics.encode("utf-8")).hexdigest() if raw_lyrics else "",
                "outputs": {
                    name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for name, path in lyrics_paths.items()
                    if path.is_file()
                },
            },
            "cover_selection": album_tags.get("cover_selection", {}),
        }
        present_front_hashes = [
            hashlib.sha256(picture.data).hexdigest() for picture in audio.pictures if picture.type == 3 and picture.data
        ]
        eligible_front_hashes = set(album_tags.get("cover_selection", {}).get("replace_front_hashes", []))
        replace_this_front = bool(present_front_hashes) and all(
            digest in eligible_front_hashes for digest in present_front_hashes
        )
        provenance_context["cover_selection"] = {
            **album_tags.get("cover_selection", {}),
            "track_front_replaced": replace_this_front,
            "previous_front_hashes": present_front_hashes,
        }
        write_report = metadata_writer.write_tags(
            write_target or f,
            manifest_tags,
            lyrics_file=lyrics_file,
            synced_lyrics_file=synced_lyrics_file,
            enhanced_lyrics_file=enhanced_lyrics_file,
            ttml_file=ttml_file,
            lyrics_translation_file=translation_file,
            lyrics_romaji_file=romaji_file,
            picture_file=picture_file,
            max_picture_dim=embed_dim,
            replace_front_picture=replace_this_front,
            incoming_confidence=release_confidence,
            release_confidence=release_confidence,
            existing_confidence=configured_existing_confidence,
            provenance_file=provenance_file,
            source="Discogs+MusicBrainz" if album_tags.get("mb_release_id") else "Discogs",
            dry_run=dry_run,
            provenance_context=provenance_context,
        )
        review_fields = [
            decision.field
            for decision in write_report.decisions
            if decision.action is metadata_writer.DecisionAction.REVIEW_REQUIRED
        ]
        if dry_run:
            counts: dict[str, int] = {}
            for decision in write_report.decisions:
                counts[decision.action.value] = counts.get(decision.action.value, 0) + 1
            summary = ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))
            return True, f"DRY RUN {f.name}: {summary or 'no changes'}\n" + json.dumps(
                write_report.changes, ensure_ascii=False, sort_keys=True
            )
        if is_verbose:
            print(f"[VERBOSE][METADATA] Tags successfully committed to '{f.name}'.")
            if review_fields:
                print(f"[VERBOSE][METADATA] Preserved conflicting fields for review: {', '.join(review_fields)}")
    except Exception as e:
        return False, f"Failed to write metadata for {f.name}: {e}"

    return True, f"Track {f.name}: MB {resolved_tracknumber} | {final_title} | {track_match_conf}"


def run_loudgain(
    flac_files: list[Path],
    quiet: bool = False,
    verbose: bool = False,
    *,
    complete_album: bool = True,
    outcome: dict[str, str] | None = None,
) -> bool:
    if outcome is not None:
        outcome.clear()
    loudness_policy = load_config().loudness
    is_verbose = verbose or os.environ.get("AUDIOPHILE_VERBOSE") == "1"
    if loudness_policy.write_r128_flac_tags:
        # R128_* has Opus-specific integer semantics.  Do not manufacture
        # FLAC values merely because a compatibility flag was set.
        print("WARNING: write_r128_flac_tags is unsupported for FLAC; using ReplayGain only.", file=sys.stderr)
    lg_bin = shutil.which("loudgain")
    scan_mode = "album" if complete_album else "track"
    if not complete_album and not quiet:
        print("  -> REPLAYGAIN: album completeness unverified; scanning individual tracks only.")
    preserve_track_only = False
    cleanup_only = False
    strip_inconsistent_album = False
    group_field = "REPLAYGAIN_ALBUM_GROUP_SHA256"
    # Bind album measurements to decoded audio, independent of names/order.
    # STREAMINFO MD5 can be zero, so it cannot identify the group on its own.
    group_audio: dict[Path, tuple[tuple[int, int, int, int, int], str]] = {}
    try:
        if complete_album:
            group_audio = {
                path: (metadata_writer._stream_signature(path), metadata_writer.pcm_digest(path)) for path in flac_files
            }
        group_signature = (
            hashlib.sha256(json.dumps(sorted(group_audio.values())).encode("utf-8")).hexdigest()
            if complete_album
            else ""
        )
    except (OSError, ValueError, MutagenError, RuntimeError, subprocess.SubprocessError) as exc:
        if outcome is not None:
            outcome["status"] = "failed"
        print(f"WARNING: cannot identify ReplayGain audio group: {exc}", file=sys.stderr)
        return False
    if loudness_policy.preserve_existing_measurements:
        # A malformed/unreadable input is left to the normal staged loudgain
        # path, which reports its own diagnostic.  Preservation probing must
        # never turn a scan attempt into an uncaught parser failure.
        existing: list[tuple[bool, tuple[str, ...]]] = []
        for path in flac_files:
            try:
                existing.append(loudness.validate_replaygain(path, require_album=complete_album))
            except Exception:
                existing.append((False, ("unreadable existing loudness tags",)))
        if complete_album and existing and all(valid for valid, _errors in existing):
            album_pairs = {
                (
                    tuple(FLAC(path).get("REPLAYGAIN_ALBUM_GAIN", [])),
                    tuple(FLAC(path).get("REPLAYGAIN_ALBUM_PEAK", [])),
                )
                for path in flac_files
            }
            group_verified = all(FLAC(path).get(group_field) == [group_signature] for path in flac_files)
            if len(album_pairs) == 1 and group_verified:
                if outcome is not None:
                    outcome["status"] = "preserved"
                if not quiet:
                    print("  -> REPLAYGAIN: preserved complete existing track/album measurements.")
                return False
            if not quiet:
                print(
                    "  -> REPLAYGAIN: existing album values disagree or lack current audio-group evidence; recalculating."
                )
            if not lg_bin:
                preserve_track_only = True
                strip_inconsistent_album = True
        if not complete_album and existing and all(valid for valid, _errors in existing):
            has_stale_album_gain = any(
                any(
                    field in FLAC(path)
                    for field in (
                        "REPLAYGAIN_ALBUM_GAIN",
                        "REPLAYGAIN_ALBUM_PEAK",
                        "REPLAYGAIN_ALBUM_RANGE",
                        group_field,
                    )
                )
                for path in flac_files
            )
            if not has_stale_album_gain:
                if outcome is not None:
                    outcome["status"] = "preserved"
                if not quiet:
                    print("  -> REPLAYGAIN: preserved valid existing track measurements.")
                return False
            preserve_track_only = True
    if not lg_bin and not preserve_track_only:
        # Uncertified album measurements must not survive merely because
        # track tags are invalid and no scanner is available.
        try:
            cleanup_only = any(
                any(
                    field in FLAC(path)
                    for field in (
                        "REPLAYGAIN_ALBUM_GAIN",
                        "REPLAYGAIN_ALBUM_PEAK",
                        "REPLAYGAIN_ALBUM_RANGE",
                        group_field,
                    )
                )
                for path in flac_files
            )
        except (OSError, ValueError):
            cleanup_only = False
        preserve_track_only = cleanup_only
        strip_inconsistent_album = cleanup_only and complete_album
    if not lg_bin and not preserve_track_only:
        if outcome is not None:
            outcome["status"] = "unavailable"
        if not quiet:
            print("  -> REPLAYGAIN: loudgain binary not found in PATH, skipping.")
        return False

    # Stage the complete album before touching originals. loudgain uses EBU R128
    # analysis but writes interoperable ReplayGain 2.0 comments for FLAC.
    transaction_id = hashlib.sha256(
        "\n".join(str(p.resolve()) for p in flac_files).encode("utf-8") + os.urandom(16)
    ).hexdigest()[:16]
    staged: list[tuple[Path, Path]] = []
    backups: list[tuple[Path, Path]] = []
    retain_backups = False
    try:
        if any(path.is_symlink() for path in flac_files):
            raise ValueError("refusing ReplayGain replacement of a symlinked FLAC")
        before_audio = group_audio or {
            path: (metadata_writer._stream_signature(path), metadata_writer.pcm_digest(path)) for path in flac_files
        }
        for index, original in enumerate(flac_files):
            pending = original.with_name(f".{original.name}.{transaction_id}.{index}.replaygain.flac")
            staged.append((original, pending))
            shutil.copy2(original, pending)

        if not preserve_track_only:
            assert lg_bin is not None
            cmd = [lg_bin, "-a" if complete_album else "-r"]
            if load_config().loudness.prevent_clipping:
                cmd.append("-k")
            # Enhanced loudgain fields are useful provenance but optional; the
            # standard ReplayGain comments remain the interoperability contract.
            cmd.extend(("-s", "e" if loudness_policy.retain_enhanced_tags else "i"))
            if quiet and not is_verbose:
                cmd.append("-q")
            if outcome is not None:
                outcome["command_profile"] = json.dumps(cmd[1:])
                binary = Path(lg_bin)
                if binary.is_file():
                    with binary.open("rb") as handle:
                        outcome["scanner_executable_sha256"] = hashlib.file_digest(handle, "sha256").hexdigest()
            cmd.extend(str(pending) for _original, pending in staged)

            if is_verbose:
                print(f"[VERBOSE][LOUDGAIN] Binary path: {lg_bin}")
                print(f"[VERBOSE][LOUDGAIN] Executing: {' '.join(cmd[:5])} [{len(flac_files)} FLAC tracks]")

            proc = subprocess.run(
                cmd, check=True, capture_output=True, text=True, timeout=max(300, 300 * len(flac_files))
            )
            if is_verbose:
                for line in [line.strip() for line in proc.stdout.splitlines() if line.strip()]:
                    print(f"[VERBOSE][LOUDGAIN] {line}")
                for line in [line.strip() for line in proc.stderr.splitlines() if line.strip()]:
                    print(f"[VERBOSE][LOUDGAIN][STDERR] {line}")
        validation_errors: list[str] = []
        for original, pending in staged:
            if (metadata_writer._stream_signature(pending), metadata_writer.pcm_digest(pending)) != before_audio[
                original
            ]:
                raise RuntimeError(f"ReplayGain scanner changed audio samples: {original.name}")
            valid, errors = loudness.validate_replaygain(
                pending, require_album=complete_album and not strip_inconsistent_album
            )
            if not valid and not cleanup_only:
                validation_errors.extend(f"{original.name}: {error}" for error in errors)
            if not complete_album or strip_inconsistent_album:
                audio = FLAC(pending)
                for field in ("REPLAYGAIN_ALBUM_GAIN", "REPLAYGAIN_ALBUM_PEAK", "REPLAYGAIN_ALBUM_RANGE", group_field):
                    if field in audio:
                        del audio[field]
                audio.save()
            elif not validation_errors:
                audio = FLAC(pending)
                audio[group_field] = [group_signature]
                audio.save()
        if complete_album and not strip_inconsistent_album and not validation_errors:
            gain_pairs = {
                (
                    tuple(FLAC(pending).get("REPLAYGAIN_ALBUM_GAIN", [])),
                    tuple(FLAC(pending).get("REPLAYGAIN_ALBUM_PEAK", [])),
                )
                for _original, pending in staged
            }
            if len(gain_pairs) != 1:
                validation_errors.append("album gain/peak differ between staged tracks")
        if validation_errors:
            raise RuntimeError("; ".join(validation_errors))
        for index, (original, _pending) in enumerate(staged):
            backup = original.with_name(f".{original.name}.{transaction_id}.{index}.backup.flac")
            backups.append((original, backup))
            shutil.copy2(original, backup)
        try:
            for original, pending in staged:
                os.replace(pending, original)
        except BaseException as exc:
            rollback_errors: list[str] = []
            for original, backup in backups:
                try:
                    os.replace(backup, original)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{original}: {rollback_exc}")
            if rollback_errors:
                retain_backups = True
                if outcome is not None:
                    outcome["recovery_required"] = "true"
                raise RuntimeError(
                    f"ReplayGain commit failed ({exc}); incomplete rollback; recovery backups retained: "
                    + "; ".join(rollback_errors)
                ) from exc
            raise
        if cleanup_only and not quiet:
            print(
                "  -> REPLAYGAIN: scanner unavailable; cleared unverified album values, leaving existing track tags unverified."
            )
        elif preserve_track_only and not quiet:
            print("  -> REPLAYGAIN: preserved track measurements and cleared unverified album values.")
        elif is_verbose:
            print(
                f"  -> REPLAYGAIN: {scan_mode} scan completed successfully ({len(flac_files)} files tagged with ReplayGain 2.0 / EBU R128)."
            )
        elif not quiet:
            print(f"  -> REPLAYGAIN: {scan_mode} scan completed successfully.")
        if outcome is not None:
            outcome.update(
                status="updated",
                tool="existing-tags" if preserve_track_only else "loudgain",
                mode="album-tags-cleared" if cleanup_only else "track-preserved" if preserve_track_only else scan_mode,
            )
        return True
    except subprocess.CalledProcessError as exc:
        if outcome is not None:
            outcome["status"] = "failed"
        print(
            f"WARNING: loudgain calculation failed with exit code {exc.returncode}: {exc.stderr or exc}",
            file=sys.stderr,
        )
    except Exception as exc:
        if outcome is not None:
            outcome["status"] = "failed"
        print(f"WARNING: loudgain calculation failed: {exc}", file=sys.stderr)
    finally:
        for _original, pending in staged:
            if pending.exists():
                pending.unlink()
        for _original, backup in backups:
            if not retain_backups and backup.exists():
                backup.unlink()
    return False


def commit_provenance_ledger(final_ledger: Path, pending_ledger: Path) -> None:
    """Publish the merged ledger with rollback on a catchable interruption."""
    metadata_writer.publish_provenance_bytes(final_ledger, pending_ledger.read_bytes())


def publish_new_artifact(source: Path, destination: Path, published: list[Path]) -> None:
    """Publish on the staging filesystem without clobbering a late user file.

    Retain the source link until transaction cleanup. A catchable interruption
    after link creation still records the destination for rollback.
    """
    try:
        os.link(source, destination, follow_symlinks=False)
    except BaseException:
        if destination.exists() and not destination.is_symlink() and os.path.samefile(source, destination):
            published.append(destination)
        raise
    published.append(destination)


def stage_album_files(
    folder: Path, flac_files: list[Path], *, generated_dr_report: str = ""
) -> tuple[Path, dict[Path, Path]]:
    """Copy every album track into one disposable transaction tree.

    A failed copy must not leave an incomplete tree that a later run could
    mistake for a recoverable transaction.
    """
    transaction_dir = Path(tempfile.mkdtemp(prefix=".metadata-transaction-", dir=folder))
    pending_root = transaction_dir / "pending"
    pending_files: dict[Path, Path] = {}
    try:
        for flac in flac_files:
            pending = pending_root / flac.relative_to(folder)
            pending.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(flac, pending)
            pending_files[flac] = pending
        if generated_dr_report and not (folder / "dr14.txt").exists():
            (pending_root / "dr14.txt").write_text(generated_dr_report, encoding="utf-8")
    except BaseException:
        shutil.rmtree(transaction_dir)
        raise
    return transaction_dir, pending_files


def resolve_cover_release_identity(
    flac_files: list[Path],
    discogs_id: str,
    album: str,
    barcode: str,
    mb_release_id: str,
    mb_release_group_id: str,
    session: requests.Session,
    user_agent: str,
    cache_dir_mb: Path,
) -> tuple[str, str]:
    """Recover a cover-only MBID only when local tags and Discogs agree."""
    if mb_release_id:
        return mb_release_id, mb_release_group_id
    local_mbids: set[str] = set()
    for track_file in flac_files:
        try:
            local_mbids.update(str(value).strip() for value in FLAC(track_file).get("MUSICBRAINZ_ALBUMID", []) if value)
        except (OSError, ValueError, MutagenError):
            return "", mb_release_group_id
    if len(local_mbids) > 1:
        print(
            "  -> IDENTITY REVIEW: conflicting existing MusicBrainz release IDs; verify edition barcode/catalog and disc IDs. Existing IDs preserved, not used for cover identity."
        )
    if len(local_mbids) != 1 or not discogs_id:
        return "", mb_release_group_id
    local_mbid = next(iter(local_mbids))
    release_url = (
        f"https://musicbrainz.org/ws/2/release/{local_mbid}"
        "?inc=artist-credits+labels+recordings+release-groups+media+discids+isrcs+url-rels&fmt=json"
    )
    code, record = mb_get(release_url, session, user_agent, cache_dir_mb)
    if code != 200 or not isinstance(record, dict):
        return "", mb_release_group_id
    related_discogs_ids = {
        match.group(1)
        for relation in record.get("relations", [])
        if (
            match := re.search(
                r"discogs\.com/release/(\d+)",
                str((relation.get("url") or {}).get("resource") or ""),
                re.IGNORECASE,
            )
        )
    }
    local_barcode = re.sub(r"\D", "", barcode)
    remote_barcode = re.sub(r"\D", "", str(record.get("barcode") or ""))
    if (
        str(record.get("id") or "").casefold() == local_mbid.casefold()
        and discogs_id in related_discogs_ids
        and audiophile_cover_fetcher.compute_album_match(album, str(record.get("title") or "")) >= 0.9
        and (not barcode or (bool(local_barcode) and bool(remote_barcode) and local_barcode == remote_barcode))
    ):
        return local_mbid, str((record.get("release-group") or {}).get("id") or "")
    print(
        f"  -> IDENTITY REVIEW: existing MusicBrainz release {local_mbid} does not corroborate "
        f"Discogs {discogs_id} and local barcode/title. Verify edition barcode/catalog and disc IDs. "
        "Existing IDs preserved, not used for cover identity."
    )
    return "", mb_release_group_id


@exclusive_album
def process_album(
    entry: str,
    discogs_token: str,
    mb_contact: str,
    quiet: bool,
    no_loudgain: bool,
    no_dr14: bool,
    threads: int,
    session: requests.Session,
    discogs_id_override: str = "",
    mb_results_limit: int = 10,
    embed_max_dim: int | None = 1400,
    dry_run: bool = False,
    verbose: int | bool = 0,
    manual_covers: bool = False,
    no_local_covers: bool = False,
    interactive_covers: bool = False,
) -> bool:
    """Main album orchestration pipeline."""
    v_env = os.environ.get("AUDIOPHILE_VERBOSE", "0")
    try:
        v_env_int = int(v_env)
    except ValueError:
        v_env_int = 1 if v_env in ("true", "yes", "1") else 0
    if isinstance(verbose, bool):
        verbosity = 1 if verbose else v_env_int
    elif isinstance(verbose, int):
        verbosity = max(verbose, v_env_int)
    else:
        verbosity = v_env_int
    is_verbose = verbosity > 0
    parts = entry.split(":::")
    if len(parts) < 2:
        return False

    id_val = parts[0]
    folder_str = parts[1]
    edition = parts[2] if len(parts) >= 3 else ""

    if discogs_id_override:
        id_val = discogs_id_override

    folder = Path(folder_str).resolve()
    if not folder.is_dir():
        return False

    if album_transaction.unfinished_transactions(folder):
        print(
            "ERROR: journaled album commit requires recovery; run album_transaction.py on this album first.",
            file=sys.stderr,
        )
        return False

    flac_files = iter_library_flacs(folder)
    local_track_count = len(flac_files)
    if local_track_count == 0:
        return False
    linked_tracks = [path for path in flac_files if path.is_symlink()]
    if linked_tracks:
        print(
            f"ERROR: album contains symlinked FLAC files; refusing to replace links: {', '.join(path.name for path in linked_tracks)}",
            file=sys.stderr,
        )
        return False
    if (folder / "cover.jpg").is_symlink():
        print(
            "ERROR: album cover.jpg is a symlink; refusing an artwork transaction that could replace it.",
            file=sys.stderr,
        )
        return False

    album_raw = folder.name
    parent_dir = folder.parent
    artist_name = parent_dir.name if parent_dir != folder and parent_dir.name and parent_dir.name != "." else ""
    if not artist_name:
        try:
            a = FLAC(str(flac_files[0]))
            if a.tags:
                artist_name = a.tags.get("ALBUMARTIST", [""])[0] or a.tags.get("ARTIST", [""])[0]
        except Exception:
            pass

    # Folder layout is weak discovery evidence.  Remove only a recognizable
    # layout date, never a legitimate parenthetical title or localized title.
    album_clean = re.sub(r"\s*[\(\[]\d{4}(?:-\d{2}(?:-\d{2})?)?[\)\]]\s*$", "", album_raw).strip()

    script_dir = Path(__file__).resolve().parent
    cache_dir_discogs = script_dir / ".api_cache" / "discogs"
    cache_dir_mb = script_dir / ".api_cache" / "musicbrainz"
    user_agent = f"AudiophileArchTagger/6.0 ({mb_contact})"

    # Autonomously discover Discogs ID if empty or auto
    if not id_val or id_val in ("auto", "none", "0"):
        print("==========================================================")
        print(f">>> ПОИСК ИЗДАНИЯ DISCOGS: {folder}")
        print("==========================================================")
        finder = discogs_release_find.DiscogsFinder(
            folder=folder,
            artist=artist_name,
            album=album_clean,
            discogs_token=discogs_token,
            mb_contact=mb_contact,
            quiet=quiet,
        )
        res = finder.find()
        disc_id = res.get("discogs_id", "")
        if res.get("ok") is True and disc_id and disc_id != "NONE":
            id_val = str(disc_id)
            if not edition or edition == "auto":
                edition = res.get("edition_summary", "")
            print(f"  -> Автоматически определен Discogs Release ID: {id_val} ({edition})")

    if not id_val or id_val in ("auto", "none", "0"):
        print(f"ERROR: Не удалось определить Discogs Release ID для '{folder}'.")
        return False

    print("==========================================================")
    print(f">>> ОБРАБОТКА: {folder}")
    print(f">>> ИЗДАНИЕ : {edition} (Discogs ID: {id_val})")
    print("==========================================================")
    print(">>> PRE-FLIGHT: сначала решаем все вопросы, требующие подтверждения")

    folder_orig_date = ""
    folder_press_date = ""
    # 1. Prefix date: "1975 - Album..." or "1975. Album..."
    m_prefix = re.match(r"^([0-9]{4}(?:-[0-9]{2}-[0-9]{2})?)\s*[-._\s]", folder.name)
    if m_prefix:
        folder_orig_date = m_prefix.group(1)

    # 2. Parenthesized date: "(1975) Album" or "Album (1975)"
    date_matches = list(re.finditer(r"[\(\[]([0-9]{4}(?:-[0-9]{2}-[0-9]{2})?)[\)\]]", folder.name))
    for dm in date_matches:
        d_val = dm.group(1)
        # Check if this parenthesized date is adjacent to remaster/reissue keywords
        context = folder.name[max(0, dm.start() - 10) : min(len(folder.name), dm.end() + 25)]
        if re.search(r"remaster|reissue|deluxe|edition|anniversary|dcc|mfsl|sacd", context, re.IGNORECASE):
            if not folder_press_date:
                folder_press_date = d_val
        else:
            if not folder_orig_date:
                folder_orig_date = d_val

    orig_date = ""
    orig_year = ""

    rip_meta_files = [
        p
        for p in folder.iterdir()
        if p.is_file() and (p.suffix.lower() in (".cue", ".log", ".accurip") or "accuraterip" in p.name.lower())
    ]
    if rip_meta_files:
        print("  -> Rip metadata: CUE/LOG/AccurateRip files found; track-level flags evaluated.")

    discogs_json = discogs_get(
        f"https://api.discogs.com/releases/{id_val}", session, discogs_token, user_agent, cache_dir_discogs
    )
    if not discogs_json:
        print(f"ERROR: Не удалось получить данные Discogs Release {id_val}")
        return False

    discogs_album_artist = "".join(
        clean_credit(credit.get("anv") or credit.get("name") or "") + str(credit.get("join") or "")
        for credit in discogs_json.get("artists") or []
        if isinstance(credit, dict)
    ).strip()
    if discogs_album_artist and artist_name and mb_identity_score.sim(discogs_album_artist, artist_name) < 0.65:
        print("ERROR: Discogs release artist conflicts with the local album identity; refusing to tag.")
        return False
    if discogs_album_artist:
        artist_name = discogs_album_artist

    discogs_title = str(discogs_json.get("title") or "")
    if discogs_title and album_clean and mb_identity_score.sim(discogs_title, album_clean) < 0.70:
        print("ERROR: Discogs release title conflicts with the local album identity; refusing to tag.")
        return False

    discogs_release_date = str(discogs_json.get("released") or discogs_json.get("year") or "").strip()
    discogs_release_date = re.sub(r"(-00)+$", "", discogs_release_date)
    if discogs_release_date == "0":
        discogs_release_date = ""

    edition_year_match = re.search(r"\b(19[0-9]{2}|20[0-9]{2})\b", edition)
    edition_year = edition_year_match.group(1) if edition_year_match else ""

    press_date, press_year, press_date_source = choose_date(discogs_release_date, edition_year, quiet)

    labels = discogs_json.get("labels") or []
    label_names: list[str] = []
    catnos: list[str] = []
    label_catalog_pairs: list[dict[str, str]] = []
    for lbl in labels:
        lname = clean_credit(lbl.get("name", ""))
        if lname and lname not in label_names:
            label_names.append(lname)
        c_raw = clean_catno(lbl.get("catno", ""))
        if c_raw and c_raw not in catnos:
            catnos.append(c_raw)
        if lname or c_raw:
            label_catalog_pairs.append({"label": lname, "catalog_number": c_raw})

    label_name = label_names[0] if label_names else ""
    catno = catnos[0] if catnos else ""
    barcode = ""
    for ident in discogs_json.get("identifiers", []):
        if str(ident.get("type", "")).casefold() == "barcode" and ident.get("value"):
            barcode = re.sub(r"[\s-]+", "", str(ident["value"]))
            break

    master_id = discogs_json.get("master_id")
    country = discogs_json.get("country", "")
    formats = discogs_json.get("formats") or []
    media = formats[0].get("name", "") if formats else ""
    genres = discogs_json.get("genres") or []
    styles = discogs_json.get("styles") or []

    # Check compilation status from Discogs formats or artist
    is_compilation = False
    for fmt in formats:
        for desc in fmt.get("descriptions", []):
            if re.search(r"\bcompilation\b", str(desc), re.IGNORECASE):
                is_compilation = True
                break
    for ea in discogs_json.get("artists", []):
        if str(ea.get("name", "")).strip().lower() in ("various", "various artists", "va"):
            is_compilation = True
            break

    discogs_album_artist_names = [
        clean_credit(credit.get("anv") or credit.get("name") or "")
        for credit in discogs_json.get("artists", [])
        if clean_credit(credit.get("anv") or credit.get("name") or "")
    ]
    if is_compilation:
        resolved_album_artist = "Various Artists"
        discogs_album_artist_names = ["Various Artists"]
    else:
        resolved_album_artist = (
            "".join(
                clean_credit(credit.get("anv") or credit.get("name") or "") + str(credit.get("join") or "")
                for credit in discogs_json.get("artists", [])
                if clean_credit(credit.get("anv") or credit.get("name") or "")
            ).strip()
            or artist_name
        )

    # Discogs Artist ID extraction
    discogs_artist_ids: list[str] = []
    for ea in discogs_json.get("artists", []):
        aid = str(ea.get("id", "")).strip()
        if aid and aid.isdigit() and aid not in discogs_artist_ids:
            discogs_artist_ids.append(aid)

    # IFPI SID codes extraction (Mastering SID Code / Mould SID Code)
    mastering_sid_codes: list[str] = []
    mould_sid_codes: list[str] = []
    for ident in discogs_json.get("identifiers", []):
        itype = str(ident.get("type", "")).strip()
        ival = str(ident.get("value", "")).strip()
        if not ival or re.match(r"^(none|\[none\]|n/a|\?)$", ival, re.IGNORECASE):
            continue
        clean_sid = re.sub(r"^ifpi\s*", "IFPI ", ival, flags=re.IGNORECASE)
        if not clean_sid.upper().startswith("IFPI "):
            clean_sid = f"IFPI {clean_sid}"
        if re.search(r"mastering\s+sid", itype, re.IGNORECASE):
            if clean_sid not in mastering_sid_codes:
                mastering_sid_codes.append(clean_sid)
        elif re.search(r"mould\s+sid", itype, re.IGNORECASE) and clean_sid not in mould_sid_codes:
            mould_sid_codes.append(clean_sid)

    # Matrix runout extraction (deduped, up to 6)
    matrix_values: list[str] = []
    for ident in discogs_json.get("identifiers", []):
        itype = str(ident.get("type", ""))
        ival = str(ident.get("value", ""))
        if re.search(r"matrix|runout", itype, re.IGNORECASE) and ival:
            clean_mv = re.sub(r"[\r\n]+", " ", ival).strip()
            if clean_mv and not re.match(r"^(none|\[none\]|n/a|\?)$", clean_mv, re.IGNORECASE):
                if clean_mv not in matrix_values:
                    matrix_values.append(clean_mv)
                    if len(matrix_values) >= 6:
                        break

    discogs_notes = str(discogs_json.get("notes") or "").replace("\r\n", "\n").replace("\r", "\n").strip()

    # SPARS Code extraction (AAD, ADD, DDD, DAD)
    spars_code = ""
    for ident in discogs_json.get("identifiers", []):
        itype = str(ident.get("type", "")).casefold()
        ival = str(ident.get("value", "")).upper().strip()
        if "spars" in itype and ival in ("AAD", "ADD", "DDD", "DAD"):
            spars_code = ival
            break
    if not spars_code and formats:
        for fmt in formats:
            for desc in fmt.get("descriptions", []):
                if str(desc).upper().strip() in ("AAD", "ADD", "DDD", "DAD"):
                    spars_code = str(desc).upper().strip()
                    break
            if spars_code:
                break
    if not spars_code and discogs_notes:
        m_spars = re.search(r"\bSPARS(?:\s+Code)?\s*[:：]?\s*([A-D]{3})\b", discogs_notes, re.IGNORECASE)
        if m_spars:
            cand_spars = m_spars.group(1).upper()
            if cand_spars in ("AAD", "ADD", "DDD", "DAD"):
                spars_code = cand_spars
    if not spars_code:
        for flac_p in flac_files[:3]:
            try:
                a_check = FLAC(flac_p)
                if a_check and a_check.tags:
                    sp_tags = a_check.tags.get("SPARSCODE", []) or a_check.tags.get("SPARS_CODE", [])
                    for sp_val in sp_tags:
                        sp_clean = sp_val.upper().strip()
                        if sp_clean in ("AAD", "ADD", "DDD", "DAD"):
                            spars_code = sp_clean
                            break
                if spars_code:
                    break
            except Exception:
                pass

    # Pre-emphasis detection from Discogs metadata (formats and release notes)
    is_preemphasis = extract_discogs_preemphasis(discogs_json)

    # Extract COPYRIGHT from Discogs companies (Copyright (c), Phonographic Copyright (p))
    copyright_info = ""
    rights_societies: list[str] = []
    for ident in discogs_json.get("identifiers", []):
        if str(ident.get("type", "")).casefold() in ("rights society",) and ident.get("value"):
            rs_val = str(ident["value"]).strip()
            if rs_val and rs_val not in rights_societies:
                rights_societies.append(rs_val)

    # Discogs company roles identify rights holders but are not themselves a
    # verbatim copyright notice.  Preserve an existing COPYRIGHT tag in the
    # writer; do not manufacture one from a label, company and guessed year.

    format_descriptions = ""
    if formats and "descriptions" in formats[0]:
        descs = [
            d
            for d in formats[0]["descriptions"]
            if re.search(r"reissue|remaster|deluxe|repress|anniversary", d, re.IGNORECASE)
        ]
        format_descriptions = " | ".join(descs)
    if format_descriptions and format_descriptions.lower() not in edition.lower():
        edition = f"{edition} | {format_descriptions}" if edition else format_descriptions
    # Master release original date
    if master_id:
        master_json = discogs_get(
            f"https://api.discogs.com/masters/{master_id}", session, discogs_token, user_agent, cache_dir_discogs
        )
        if master_json:
            m_year = str(master_json.get("year", "")).strip()
            if m_year:
                orig_date = m_year
                orig_year = m_year

    # Release-level credits
    producers: list[str] = []
    arrangers: list[str] = []
    engineers: list[str] = []
    mixers: list[str] = []
    mastered_by: list[str] = []
    remastered_by: list[str] = []
    conductors: list[str] = []
    ensembles: list[str] = []
    performers: list[str] = []
    orchestras: list[str] = []
    choruses: list[str] = []
    soloists: list[str] = []
    librettists: list[str] = []
    recording_studios: list[str] = []
    mixing_studios: list[str] = []
    mastering_studios: list[str] = []
    pressing_plants: list[str] = []
    manufacturers: list[str] = []
    glass_mastered_by: list[str] = []

    # Non-audio roles containing 'master' that must NOT match mastering engineers
    non_audio_master_re = re.compile(
        r"\b(chorus|choir|concert|band|web|scrum|ballet|stage|property|puppet)\s*master\b",
        re.IGNORECASE,
    )
    audio_master_re = re.compile(
        r"\b(audio\s+)?(master(ed|ing)?(\s+by)?|lacquer\s+cut|cut\s+by|direct\s+metal\s+master(ed|ing)?|dmm)\b",
        re.IGNORECASE,
    )
    remaster_re = re.compile(r"\b(audio\s+)?remaster(ed|ing)?(\s+by)?\b", re.IGNORECASE)
    chief_engineer_re = re.compile(r"\b(recording|sound|balance|tracking|mix(ed|ing)?)?\s*engineer\b", re.IGNORECASE)
    assistant_engineer_re = re.compile(r"\b(assistant|second|tape\s+op|trainee)\b", re.IGNORECASE)

    for ea in discogs_json.get("extraartists", []):
        role_l = ea.get("role", "").lower()
        who = clean_credit(ea.get("name", ""))
        if not who:
            continue
        if re.search(r"producer|produced\b", role_l) and who not in producers:
            producers.append(who)
        if re.search(r"\b(arrang(ed|er|ement)?|orchestrat(ed|or|ion)?)\b", role_l) and who not in arrangers:
            arrangers.append(who)
        if remaster_re.search(role_l) and who not in remastered_by:
            remastered_by.append(who)
        if (
            audio_master_re.search(role_l)
            and not remaster_re.search(role_l)
            and not non_audio_master_re.search(role_l)
            and who not in mastered_by
        ):
            mastered_by.append(who)
        if re.search(r"mix(ed|ing)?\b", role_l) and who not in mixers:
            mixers.append(who)
        if chief_engineer_re.search(role_l) and not assistant_engineer_re.search(role_l) and who not in engineers:
            engineers.append(who)
        if re.search(r"\bconductor\b", role_l) and who not in conductors:
            conductors.append(who)
        if re.search(r"\b(orchestra|philharmonic|symphony)\b", role_l) and who not in orchestras:
            orchestras.append(who)
        if re.search(r"\b(choir|chorus|choral)\b", role_l) and who not in choruses:
            choruses.append(who)
        if re.search(r"\bsoloist\b", role_l) and who not in soloists:
            soloists.append(who)
        if re.search(r"\b(libretto|librettist)\b", role_l) and who not in librettists:
            librettists.append(who)
        if (
            re.search(r"\b(orchestra|ensemble|choir|chorus|quartet|trio|quintet|band)\b", role_l)
            and who not in ensembles
        ):
            ensembles.append(who)
        if re.search(
            r"\b(performed by|performer|soloist|vocals|lead vocals|guitar|bass|drums|keyboards|piano)\b", role_l
        ):
            p_entry = format_performer(who, ea.get("role", ""))
            if p_entry and p_entry not in performers:
                performers.append(p_entry)
        if (
            re.search(r"\b((re)?mastered\s+at|lacquer\s+cut\s+at|glass\s+mastered\s+at)\b", role_l)
            and who not in mastering_studios
        ):
            mastering_studios.append(who)

    for comp in discogs_json.get("companies", []):
        etn = comp.get("entity_type_name", "").lower()
        cname = clean_credit(comp.get("name", ""))
        if not cname:
            continue
        if re.search(r"\b(recorded\s+(at|by)|recording\s+studio)\b", etn):
            if cname not in recording_studios:
                recording_studios.append(cname)
        elif re.search(r"\b(mixed\s+(at|by)|mixing\s+studio)\b", etn):
            if cname not in mixing_studios:
                mixing_studios.append(cname)
        elif re.search(r"\b((re)?mastered\s+at|lacquer\s+cut\s+at)\b", etn):
            if cname not in mastering_studios:
                mastering_studios.append(cname)
        elif re.search(r"\b(pressed\s+(at|by)|pressing\s+plant)\b", etn):
            if cname not in pressing_plants:
                pressing_plants.append(cname)
        elif re.search(r"\b(manufactured\s+(at|by)|manufacturer)\b", etn):
            if cname not in manufacturers:
                manufacturers.append(cname)
        elif re.search(r"\b(glass\s+mastered\s+(at|by))\b", etn) and cname not in glass_mastered_by:
            glass_mastered_by.append(cname)

    is_hdcd = bool(
        re.search(r"\bhdcd\b", edition, re.IGNORECASE)
        or re.search(r"\bhdcd\b", folder.name, re.IGNORECASE)
        or any("hdcd" in str(d).lower() for d in discogs_json.get("formats", [{}])[0].get("descriptions", []))
    )

    recording_date = ""
    notes_str = discogs_json.get("notes", "") or ""
    rec_m = re.search(
        r"\brecorded\s+(?:in\s+|at\s+[^\n.,]+,\s*)?([A-Za-z0-9\s,'/-]+?\b(?:19\d{2}|20\d{2})\b)",
        notes_str,
        re.IGNORECASE,
    )
    if rec_m:
        cand_date = rec_m.group(1).strip()
        yr_m = re.search(r"\b(19\d{2}|20\d{2})\b", cand_date)
        if yr_m:
            recording_date = yr_m.group(1)

    # Vinyl & Needle-drop Hardware attributes
    vinyl_attrs = parse_vinyl_attributes(formats, notes_str)

    # Source format & Audiophile series detection
    source_format = detect_source_format(notes_str, formats)
    audiophile_series = detect_audiophile_series(
        notes_str, discogs_json.get("series", []), discogs_json.get("labels", [])
    )
    mb_release_types: list[str] = []

    # Live & Venue detection
    is_live = bool(
        re.search(r"\b(live|concert)\b", folder.name, re.IGNORECASE)
        or re.search(r"\b(live|concert)\b", album_clean, re.IGNORECASE)
        or any("live" in str(d).lower() for fmt in formats for d in fmt.get("descriptions", []))
    )
    venue = ""
    location = ""
    m_live = re.search(r"\blive\s+at\s+([^,()]+)(?:,\s*([^()]+))?", album_clean, re.IGNORECASE)
    if m_live:
        venue = m_live.group(1).strip()
        if m_live.group(2):
            location = m_live.group(2).strip()

    # MusicBrainz verification with Multi-Tier Candidate Search & Scoring
    mb_release_id = ""
    mb_match_kind = "NONE"
    mb_recordings_json: dict[str, Any] | None = None
    mb_release_group_id = ""
    mb_orig_date = ""
    mb_discid = ""
    mb_title = ""
    mb_release_status = ""
    mb_release_type = ""
    mb_albumartist_id = ""
    mb_albumartist_ids: list[str] = []
    mb_albumartist_sort = ""
    mb_total_tracks = ""
    freedb_discid = ""
    freedb_discid_source = ""
    mb_discid_source = ""
    rip_toc_source = ""
    rip_toc_tracks = ""
    mb_leading_intro = False

    # Keep physical evidence by medium.  The release resolver still begins
    # with disc 1 where it needs a single Disc ID query, but writes use the
    # correct per-disc record below.
    mb_tocs = mb_discid_from_cue.build_all(folder)
    mb_toc = mb_tocs.get(1) or next(iter(mb_tocs.values()), {})
    if mb_toc:
        raw_discid = mb_toc.get("discid")
        is_partial = mb_toc.get("is_partial", False)
        freedb_discid = mb_toc.get("freedb_discid", "")
        rip_toc_source = mb_toc.get("source", "")
        rip_toc_tracks = str(mb_toc.get("track_count", ""))

        if "eac" in rip_toc_source.lower():
            freedb_discid_source = "EAC"
            mb_discid_source = "CUE+EAC_TOC"
        elif "cue" in rip_toc_source.lower():
            freedb_discid_source = "CUE"
            mb_discid_source = "CUE"
        else:
            freedb_discid_source = "AUDIO"
            mb_discid_source = "AUDIO"

        if raw_discid and not is_partial and mb_toc.get("toc_exact") is True:
            mb_discid = raw_discid
            print(f"  -> Disc ID (CUE + EAC TOC): {mb_discid} | TOC tracks={rip_toc_tracks}")
        elif freedb_discid:
            print(f"  -> FreeDB Disc ID: {freedb_discid} ({freedb_discid_source}) | TOC tracks={rip_toc_tracks}")
        else:
            print("  -> Disc ID: partial or audio-derived TOC (evaluating via barcode & release metadata)")
    else:
        print("  -> Disc ID: no physical CUE sheet present (evaluating via barcode & release metadata)")

    # Candidate entries: tuple of (cand_id, is_discid_exact, is_url_exact)
    candidate_entries: list[tuple[str, bool, bool]] = []
    candidate_limit = max(1, min(int(mb_results_limit), 99))

    # Tier 1: Query by MusicBrainz Disc ID
    if mb_discid:
        # The Disc ID lookup endpoint does not accept release-detail `inc`
        # parameters. It is discovery-only; every returned release is fetched
        # through the release endpoint and evaluated below.
        disc_url = f"https://musicbrainz.org/ws/2/discid/{mb_discid}?fmt=json"
        code, body = mb_get(disc_url, session, user_agent, cache_dir_mb)
        if code == 200 and isinstance(body, dict):
            for rel in body.get("releases", []):
                cid = rel.get("id")
                if cid:
                    candidate_entries.append((cid, True, False))

    # Tier 2: Query by Discogs Release URL relation
    if len(candidate_entries) < candidate_limit:
        url_q = f"https://musicbrainz.org/ws/2/url?resource=https://www.discogs.com/release/{id_val}&inc=release-rels&fmt=json"
        code, body = mb_get(url_q, session, user_agent, cache_dir_mb)
        if code == 200 and isinstance(body, dict):
            for rel in body.get("relations", []):
                cid = rel.get("release", {}).get("id")
                if cid:
                    candidate_entries.append((cid, False, True))

    # Tier 3: Query by Barcode
    if len(candidate_entries) < candidate_limit and barcode:
        bar_q = f"https://musicbrainz.org/ws/2/release/?query=barcode:{urllib.parse.quote(barcode)}+AND+status:official&limit={candidate_limit}&fmt=json"
        code, body = mb_get(bar_q, session, user_agent, cache_dir_mb)
        if code == 200 and isinstance(body, dict):
            for rel in body.get("releases", [])[:candidate_limit]:
                cid = rel.get("id")
                if cid:
                    candidate_entries.append((cid, False, False))

    # Tier 4: Query by Catalog Number + Artist
    if catno:
        cat_q = f"https://musicbrainz.org/ws/2/release/?query=catno:%22{urllib.parse.quote(catno)}%22+AND+artist:%22{urllib.parse.quote(artist_name)}%22+AND+status:official&limit={candidate_limit}&fmt=json"
        code, body = mb_get(cat_q, session, user_agent, cache_dir_mb)
        if code == 200 and isinstance(body, dict):
            for rel in body.get("releases", [])[:candidate_limit]:
                cid = rel.get("id")
                if cid:
                    candidate_entries.append((cid, False, False))

    # Tier 5: Query by Release Title + Artist + Format CD
    if album_clean:
        alb_q = f"https://musicbrainz.org/ws/2/release/?query=release:%22{urllib.parse.quote(album_clean)}%22+AND+artist:%22{urllib.parse.quote(artist_name)}%22+AND+format:CD&limit={candidate_limit}&fmt=json"
        code, body = mb_get(alb_q, session, user_agent, cache_dir_mb)
        if code == 200 and isinstance(body, dict):
            for rel in body.get("releases", [])[:candidate_limit]:
                cid = rel.get("id")
                if cid:
                    candidate_entries.append((cid, False, False))

    # Tier 6: Query by Release Group
    if album_clean:
        rg_q = f"https://musicbrainz.org/ws/2/release-group/?query=releasegroup:%22{urllib.parse.quote(album_clean)}%22+AND+artist:%22{urllib.parse.quote(artist_name)}%22&fmt=json"
        code, body = mb_get(rg_q, session, user_agent, cache_dir_mb)
        if code == 200 and isinstance(body, dict):
            rgs = body.get("release-groups", [])
            if rgs:
                # Search results are not an identity decision. Evaluate all
                # bounded credible groups rather than trusting result #1.
                for found_rg in rgs[: min(candidate_limit, 10)]:
                    group_id = found_rg.get("id", "")
                    if not group_id:
                        continue
                    rels_q = f"https://musicbrainz.org/ws/2/release?release-group={group_id}&inc=artist-credits+recordings+media+isrcs&fmt=json"
                    rcode, rbody = mb_get(rels_q, session, user_agent, cache_dir_mb)
                    if rcode == 200 and isinstance(rbody, dict):
                        for rel in [r for r in rbody.get("releases", []) if r.get("status") == "Official"][
                            :candidate_limit
                        ]:
                            cid = rel.get("id")
                            if cid:
                                candidate_entries.append((cid, False, False))

    # Tier 6.5: AcoustID Audio Fingerprint Match & Candidate Discovery
    acoustid_track_recordings: dict[tuple[int, int], set[str]] = {}
    if flac_files:
        acoust_release_votes: dict[str, int] = {}
        # Fingerprints identify recordings, not editions.  Evaluate every track
        # so one erroneous first-track result cannot steer release selection.
        for flac_path in flac_files:
            acoust_rel_ids, acoust_rec_ids = query_acoustid_candidates(flac_path, session, cache_dir_mb)
            audio_tags = {str(key).upper(): list(values) for key, values in FLAC(flac_path).items()}
            disc, track, _title, conflicts = library_layout.position_evidence(flac_path, folder, audio_tags)
            if acoust_rec_ids and disc is not None and track is not None and not conflicts:
                acoustid_track_recordings.setdefault((disc, track), set()).update(acoust_rec_ids)
            for acoust_release_id in acoust_rel_ids:
                acoust_release_votes[acoust_release_id] = acoust_release_votes.get(acoust_release_id, 0) + 1
        if len(candidate_entries) < candidate_limit:
            for acoust_release_id, _votes in sorted(acoust_release_votes.items(), key=lambda item: (-item[1], item[0]))[
                :candidate_limit
            ]:
                candidate_entries.append((acoust_release_id, False, False))

    # Evaluate and score candidates
    evaluated_candidates: list[dict[str, Any]] = []
    local_disc_counts = library_layout.disc_counts(flac_files, folder)
    local_disc_durations: dict[int, list[float]] = {}
    local_track_durations: dict[tuple[int, int], float] = {}
    for local_file in flac_files:
        try:
            local_audio = FLAC(local_file)
            local_tags = {str(key).upper(): list(values) for key, values in local_audio.items()}
            disc_number, track_number, _title, conflicts = library_layout.position_evidence(
                local_file, folder, local_tags
            )
            if disc_number is not None and track_number is not None and not conflicts:
                local_disc_durations.setdefault(disc_number, []).append(float(local_audio.info.length))
                local_track_durations[(disc_number, track_number)] = float(local_audio.info.length)
        except Exception:
            pass

    seen_cids: set[str] = set()
    rel_country = normalize_country_iso(country)

    for cid, c_discid, c_url in candidate_entries:
        if not cid or cid in seen_cids:
            continue
        seen_cids.add(cid)

        lookup_url = f"https://musicbrainz.org/ws/2/release/{cid}?inc=artist-credits+labels+recordings+release-groups+media+discids+isrcs+url-rels&fmt=json"
        code, cand_json = mb_get(lookup_url, session, user_agent, cache_dir_mb)
        if code == 200 and isinstance(cand_json, dict):
            verify = mb_identity_score.evaluate_identity(
                D=discogs_json,
                M=cand_json,
                wcountry=rel_country,
                wcat=catno,
                wbarcode=barcode,
                wyear=press_year,
                discid_exact=c_discid,
                toc=mb_toc,
                url_relation=c_url,
                acoustid_position_recordings=acoustid_track_recordings,
            )
            medium_layout = release_match.assess_medium_layouts(
                local_disc_counts,
                cand_json.get("media") or [],
                {disc: tuple(values) for disc, values in local_disc_durations.items()},
                local_track_durations,
            )
            verify["medium_layout"] = medium_layout
            if not medium_layout["compatible"]:
                verify["rejected"] = True
                verify["confidence"] = "REJECT"
                verify.setdefault("hard_conflicts", []).extend(medium_layout["conflicts"])
            cand_item = dict(cand_json)
            cand_item["identity"] = verify
            evaluated_candidates.append(cand_item)

    selection = release_match.select_release_candidate(evaluated_candidates)
    if selection.candidate is not None:
        chosen_identity = selection.candidate.get("identity") or {}
        mb_release_id = str(selection.candidate.get("id") or "")
        mb_match_kind = selection.confidence.value
        mb_recordings_json = {k: v for k, v in selection.candidate.items() if k != "identity"}
        mb_leading_intro = bool(chosen_identity.get("flags", {}).get("leading_intro_variant", False))
        print(
            f"  -> MB MATCH: {mb_match_kind} "
            f"[{chosen_identity.get('confidence_basis', 'none')} | score: "
            f"{chosen_identity.get('score', 0)} | margin: {selection.margin:.0f}]"
        )

        mb_title = mb_recordings_json.get("title", "")
        mb_release_status = str(mb_recordings_json.get("status", "")).lower()
        rg_info = mb_recordings_json.get("release-group") or {}
        mb_release_type = str(rg_info.get("primary-type", "")).lower()
        if rg_info.get("primary-type"):
            mb_release_types.append(rg_info["primary-type"].capitalize())
        for st in rg_info.get("secondary-types", []):
            if st and st.capitalize() not in mb_release_types:
                mb_release_types.append(st.capitalize())
        art_credits = mb_recordings_json.get("artist-credit") or []
        if art_credits:
            for credit in art_credits:
                art_obj = credit.get("artist") or {}
                artist_id = art_obj.get("id", "")
                if artist_id and artist_id not in mb_albumartist_ids:
                    mb_albumartist_ids.append(artist_id)
                if not mb_albumartist_id:
                    mb_albumartist_id = artist_id
                    mb_albumartist_sort = art_obj.get("sort-name", "")

        media_list = mb_recordings_json.get("media") or []
        if media_list:
            mb_total_tracks = str(sum(int(medium.get("track-count") or 0) for medium in media_list))

        mb_release_group_id = rg_info.get("id", "")
        mb_first_rel = rg_info.get("first-release-date", "")
        if mb_first_rel:
            mb_orig_date = mb_first_rel
            orig_date = mb_first_rel
            orig_year = mb_first_rel[:4]

        mb_rel_date = mb_recordings_json.get("date", "")
        if mb_rel_date and press_date_source != "edition":
            press_date = mb_rel_date
            press_year = mb_rel_date[:4]
            press_date_source = "MusicBrainz"

        if not barcode:
            barcode = mb_recordings_json.get("barcode", "")
        for l_info in mb_recordings_json.get("label-info") or mb_recordings_json.get("label-info-list") or []:
            mb_c = clean_catno(l_info.get("catalog-number", ""))
            if mb_c and mb_c not in catnos:
                catnos.append(mb_c)
            mb_lbl = l_info.get("label", {})
            if isinstance(mb_lbl, dict):
                mb_lname = clean_credit(mb_lbl.get("name", ""))
                if mb_lname and mb_lname not in label_names:
                    label_names.append(mb_lname)
        if not catno and catnos:
            catno = catnos[0]
        if not label_name and label_names:
            label_name = label_names[0]
        if not country:
            country = mb_recordings_json.get("country", "")
            if country:
                rel_country = normalize_country_iso(country)

    # A release group identifies an album concept, never a pressing.  Do not
    # write a text-search release-group ID or its artist/date when edition
    # candidate selection failed.
    if not mb_release_id:
        mb_release_group_id = ""
        mb_albumartist_id = ""
        mb_albumartist_ids = []
        mb_albumartist_sort = ""
        if selection.confidence is release_match.ReleaseConfidence.AMBIGUOUS:
            mb_match_kind = selection.confidence.value
            print(f"  -> MB REVIEW REQUIRED: {selection.reason}; release-specific MB tags withheld.")

    # Folder dates are organizational hints, not evidence of an original
    # release.  Only an explicitly labelled edition date may fill DATE, and
    # ORIGINALDATE must come from Discogs/MusicBrainz/package evidence.
    if not press_date and folder_press_date:
        press_date = folder_press_date
        press_year = folder_press_date[:4]
        press_date_source = "folder"

    resolved_tracktotal = str(local_disc_counts.get(1, local_track_count))
    if rip_toc_tracks and rip_meta_files:
        resolved_tracktotal = rip_toc_tracks
    else:
        disc_track_count = len(
            [t for t in discogs_json.get("tracklist", []) if t.get("type_", t.get("type", "track")) == "track"]
        )
        if mb_total_tracks and mb_total_tracks.isdigit() and int(mb_total_tracks) > 0:
            resolved_tracktotal = mb_total_tracks
        elif disc_track_count > 0:
            resolved_tracktotal = str(disc_track_count)

    total_discs = max(local_disc_counts, default=1)

    if mb_recordings_json:
        m_len = len(mb_recordings_json.get("media", []))
        total_discs = max(total_discs, m_len)

    disc_qty = str(formats[0].get("qty", "")) if formats else ""
    if disc_qty.isdigit() and int(disc_qty) > total_discs:
        total_discs = int(disc_qty)

    exact_release_disc_counts = (
        release_match.release_disc_counts((mb_recordings_json or {}).get("media") or [])
        if mb_match_kind in release_match.ACCEPTABLE
        else None
    )
    completeness = assess_album_completeness(flac_files, folder, external_disc_counts=exact_release_disc_counts)
    print(f"  -> PRE-FLIGHT: все подтверждения завершены. [Discs: {total_discs} | Tracks: {resolved_tracktotal}]")

    # Multi-Provider Cover Art Engine
    cover_mb_release_id, cover_mb_release_group_id = resolve_cover_release_identity(
        flac_files,
        id_val,
        album_clean,
        str(barcode or ""),
        mb_release_id,
        mb_release_group_id,
        session,
        user_agent,
        cache_dir_mb,
    )
    cover_file = folder / "cover.jpg"
    cover_stage_id = uuid.uuid4().hex
    cover_tmp = folder / f".cover-stage-{cover_stage_id}.jpg"
    cover_source_tmp: Path | None = None
    selected_cover: audiophile_cover_fetcher.CoverCandidate | None = None
    interactive_selected = False
    replace_existing_front = False
    replace_cover_file = False
    replace_front_hashes: set[str] = set()
    fetcher: audiophile_cover_fetcher.CoverFetcher | None = None

    best_cand: audiophile_cover_fetcher.CoverCandidate | None
    all_cands: list[audiophile_cover_fetcher.CoverCandidate]
    if dry_run and not manual_covers:
        best_cand, all_cands = None, []
    else:
        fetcher = audiophile_cover_fetcher.CoverFetcher(
            artist=artist_name,
            album=album_clean,
            folder=folder,
            mb_rg_id=cover_mb_release_group_id,
            mb_rel_id=cover_mb_release_id,
            discogs_id=id_val,
            discogs_token=discogs_token,
            discogs_json_str=json.dumps(discogs_json),
            verbose=verbosity,
            manual_all=manual_covers,
            exhaustive_categories=interactive_covers,
            no_local_covers=no_local_covers,
            write_preview_files=False,
            target_barcode=str(barcode or ""),
            target_catalog=str(catno or ""),
            target_country=str(rel_country or ""),
            target_media=str(media or ""),
        )
        best_cand, _best_digital, _best_scan, all_cands = fetcher.find_best_cover(verbose=verbosity)
        if dry_run:
            audiophile_cover_fetcher.prompt_all_cover_choice(all_cands, best_cand)
            best_cand, all_cands = None, []
            fetcher = None

    if best_cand and best_cand.raw_data and not interactive_covers and not manual_covers and fetcher is not None:
        prepared_cover = audiophile_cover_fetcher.prepare_best_cover(fetcher, all_cands, cover_tmp, preferred=best_cand)
        selected_cover = prepared_cover.candidate if prepared_cover else None

    if manual_covers and not dry_run:
        while fetcher is not None:
            chosen = audiophile_cover_fetcher.prompt_all_cover_choice(all_cands, best_cand)
            if not chosen or not chosen.raw_data:
                break
            prepared_cover = audiophile_cover_fetcher.prepare_best_cover(
                fetcher, all_cands, cover_tmp, preferred=chosen, allow_fallback=False
            )
            if prepared_cover:
                selected_cover = chosen
                break
            best_cand = fetcher._best_in_release_tier(all_cands)
            if best_cand is None:
                break
    elif interactive_covers and fetcher is not None:
        choices = audiophile_cover_fetcher.select_cover_categories(fetcher, all_cands)
        if choices:
            with tempfile.TemporaryDirectory(prefix=".cover-options-", dir=folder) as option_directory:
                option_root = Path(option_directory)
                audiophile_cover_fetcher.prepare_cover_choices(
                    choices, option_root, fetcher=fetcher, candidates=all_cands
                )
                comparison = option_root / "comparison.jpg"
                audiophile_cover_fetcher.create_comparison_image(None, None, best_cand, comparison, options=choices)
                displayed = audiophile_cover_fetcher.emit_kitty_image(comparison)
                if displayed and len(choices) > 1 and shutil.get_terminal_size((80, 24)).columns < 160:
                    print("  -> Showing prepared front covers individually for a readable comparison.")
                    for number, choice in enumerate(choices, start=1):
                        if choice.prepared_path:
                            print(f"  {number}. {choice.category.upper()} — {choice.candidate.provider}")
                            audiophile_cover_fetcher.emit_kitty_image(choice.prepared_path)
                if not displayed:
                    print("  -> Terminal image display unavailable; inspect the prepared image paths below.")
                chosen_option = audiophile_cover_fetcher.prompt_cover_options(
                    choices, album=f"{artist_name} — {album_clean}"
                )
                if chosen_option and chosen_option.prepared_path:
                    selected_cover = chosen_option.candidate
                    shutil.copy2(chosen_option.prepared_path, cover_tmp)
                    interactive_selected = True
        else:
            print("  -> No eligible front-cover artwork was found for this album.")

    if selected_cover and selected_cover.raw_data and not selected_cover.source_sha256 and fetcher is not None:
        fetcher.evaluate_candidate(selected_cover)
    existing_cover = next(
        (c for c in all_cands if c.provider == "Local Existing File" and c.url == str(cover_file)), None
    )
    if selected_cover and selected_cover.raw_data and cover_file.is_file():
        if existing_cover is None and fetcher is not None:
            existing_cover = audiophile_cover_fetcher.CoverCandidate(
                "Local Existing File",
                str(cover_file),
                "Existing cover.jpg",
                1.0,
                raw_data=cover_file.read_bytes(),
                identity_scope="local",
            )
            if not no_local_covers:
                existing_cover = fetcher.evaluate_candidate(existing_cover)
        replace_cover_file = audiophile_cover_fetcher.should_replace_existing_cover(
            selected_cover,
            existing_cover,
            manual=manual_covers or interactive_selected,
            ignore_existing=no_local_covers,
        )
        if (
            selected_cover.crop_box
            and selected_cover.crop_confidence >= 0.8
            and selected_cover.provider.startswith("Local Existing File")
            and selected_cover.raw_data == cover_file.read_bytes()
        ):
            replace_cover_file = True
        if replace_cover_file and not cover_tmp.is_file():
            audiophile_cover_fetcher.save_optimized_cover(
                selected_cover.raw_data,
                cover_tmp,
                crop_box=selected_cover.crop_box if selected_cover.crop_confidence >= 0.8 else None,
            )

    if selected_cover and selected_cover.raw_data and fetcher is not None:
        seen_fronts: set[str] = set()
        for track_file in flac_files:
            try:
                for picture in FLAC(track_file).pictures:
                    if picture.type != 3 or not picture.data:
                        continue
                    digest = hashlib.sha256(picture.data).hexdigest()
                    if digest in seen_fronts:
                        continue
                    seen_fronts.add(digest)
                    if digest == selected_cover.source_sha256:
                        if selected_cover.crop_box and selected_cover.crop_confidence >= 0.8:
                            replace_front_hashes.add(digest)
                        continue
                    if (
                        manual_covers
                        or interactive_selected
                        or (
                            no_local_covers
                            and selected_cover.release_tier == audiophile_cover_fetcher.ReleaseTier.EXACT_RELEASE
                        )
                    ):
                        replace_front_hashes.add(digest)
                    elif selected_cover.release_tier == audiophile_cover_fetcher.ReleaseTier.EXACT_RELEASE:
                        incumbent = audiophile_cover_fetcher.CoverCandidate(
                            "Local Embedded Front",
                            f"embedded://{digest}",
                            "Existing front",
                            1.0,
                            raw_data=bytes(picture.data),
                            identity_scope="local",
                        )
                        fetcher.evaluate_candidate(incumbent)
                        if incumbent.error or selected_cover.visual_quality_score > (
                            incumbent.visual_quality_score + audiophile_cover_fetcher.VISUAL_EQUIVALENCE_MARGIN
                        ):
                            replace_front_hashes.add(digest)
            except (OSError, ValueError, MutagenError):
                continue
    replace_existing_front = bool(replace_front_hashes)
    if cover_file.is_file() and not replace_cover_file and not (manual_covers or interactive_selected):
        replace_front_hashes.clear()
        replace_existing_front = False

    if cover_tmp.is_file() and cover_tmp.stat().st_size > 0 and (not cover_file.is_file() or replace_cover_file):
        # Keep fetched art as a transaction-local candidate until every FLAC
        # and sidecar has validated. It is embedded from this path below.
        print("  -> Обложка: staged for transaction commit.")
    elif cover_file.is_file() and cover_file.stat().st_size > 0:
        # An existing source is kept unless a better exact-release front won.
        if cover_tmp.is_file() and not replace_cover_file and not dry_run:
            cover_tmp.unlink()
        print("  -> Обложка: сохранена существующая локальная обложка.")
    elif cover_tmp.is_file() and not dry_run:
        cover_tmp.unlink()
    else:
        print("  -> Обложка: не найдена.")

    if (
        selected_cover
        and selected_cover.raw_data
        and (not cover_file.exists() or replace_cover_file or replace_front_hashes)
    ):
        cover_source_tmp = audiophile_cover_fetcher.save_original_cover(
            selected_cover.raw_data,
            folder / f".cover-source-stage-{cover_stage_id}",
        )
    # Replacement evidence was measured separately for every distinct
    # embedded front, so an exceptional track-specific image is retained.

    # Dynamic Range / DR14 Album measurement
    dr_track_map: dict[str, tuple[str, str, str]] = {}
    album_dr = ""
    dr_src = dr14_parse.DR_ALGORITHM_VERSION
    generated_dr_reports: list[str] = []
    if no_dr14 or dry_run:
        if not quiet:
            print(f"  -> DR14: skipped ({'--dry-run' if dry_run else '--no-dr14'}).")
    else:
        existing_rep = dr14_parse.find_report_in_dir(folder)
        if existing_rep:
            dr_src = existing_rep.name
        dr_track_map, album_dr, _dr_db_info = measure_album_dr14(
            folder,
            flac_files,
            threads=threads,
            artist=artist_name,
            album=album_clean,
            catalog_no=catno,
            session=session,
            target_media=media,
            verbose=is_verbose,
            report_sink=generated_dr_reports.append,
        )
        if album_dr:
            print(f"  -> Dynamic Range: Album DR{album_dr} ({dr_src})")

    release_language = ""
    if mb_recordings_json:
        release_language = mb_recordings_json.get("text-representation", {}).get("language", "")

    disc_subtitles = extract_disc_subtitles(discogs_json, mb_recordings_json, folder)
    if disc_subtitles and not quiet:
        print(f"  -> Box Set / Multi-Disc Subtitles: {disc_subtitles}")

    if not mb_release_types:
        for fmt in formats:
            for d in fmt.get("descriptions", []):
                if d.lower() in ("album", "single", "ep", "compilation", "live", "soundtrack", "box set"):
                    c_d = "Boxset" if d.lower() == "box set" else d.capitalize()
                    if c_d not in mb_release_types:
                        mb_release_types.append(c_d)

    # Album-level tags dictionary passed to track workers
    album_tags: dict[str, Any] = {
        "final_album": mb_title or album_clean,
        "album_artist": resolved_album_artist,
        "album_artist_parts": discogs_album_artist_names or [resolved_album_artist],
        "mb_albumartist_sort": mb_albumartist_sort,
        "disc_subtitles": disc_subtitles,
        "source_format": source_format,
        "audiophile_series": audiophile_series,
        "release_types": mb_release_types,
        "studio": recording_studios[0] if recording_studios else "",
        "barcode": barcode,
        "catno": catno,
        "catnos": catnos,
        "label_name": label_name,
        "label_names": label_names,
        "label_catalog_pairs": label_catalog_pairs,
        "press_date": press_date,
        "press_year": press_year,
        "press_date_source": press_date_source,
        "orig_date": orig_date,
        "orig_year": orig_year,
        "embed_max_dim": embed_max_dim,
        "dr_loudness_war_id": "",
        "dr_loudness_war_url": "",
        "min_dr": "",
        "max_dr": "",
        "mb_orig_date": mb_orig_date,
        "discogs_release_date": discogs_release_date,
        "master_id": master_id,
        "discogs_notes": discogs_notes,
        "is_preemphasis": is_preemphasis,
        "discogs_id": id_val,
        "total_discs": str(total_discs),
        "edition": edition,
        "track_version": "",
        "is_compilation": is_compilation,
        "freedb_discid": freedb_discid,
        "freedb_discid_source": freedb_discid_source,
        "copyright": copyright_info,
        "rights_societies": rights_societies,
        "release_language": release_language,
        "genres": genres,
        "matrix_values": matrix_values,
        "mastering_sid_codes": mastering_sid_codes,
        "mould_sid_codes": mould_sid_codes,
        "discogs_artist_ids": discogs_artist_ids,
        "media": media,
        "mb_match_kind": mb_match_kind,
        "mb_match_score": float((selection.candidate or {}).get("identity", {}).get("score", 0)),
        "mb_match_margin": selection.margin,
        "mb_albumartist_id": mb_albumartist_id,
        "mb_albumartist_ids": mb_albumartist_ids,
        "mb_release_id": mb_release_id,
        "mb_discid": mb_discid,
        "mb_discid_source": mb_discid_source,
        "mb_tocs": mb_tocs,
        "mb_release_group_id": mb_release_group_id,
        "rel_country": rel_country,
        "mb_release_status": mb_release_status,
        "mb_release_type": mb_release_type,
        "rip_toc_source": rip_toc_source,
        "rip_toc_tracks": rip_toc_tracks,
        "styles": styles,
        "producers": producers,
        "arrangers": arrangers,
        "mastered_by": mastered_by,
        "remastered_by": remastered_by,
        "engineers": engineers,
        "mixers": mixers,
        "conductors": conductors,
        "ensembles": ensembles,
        "performers": performers,
        "recording_studios": recording_studios,
        "mixing_studios": mixing_studios,
        "mastering_studios": mastering_studios,
        "pressing_plants": pressing_plants,
        "manufacturers": manufacturers,
        "glass_mastered_by": glass_mastered_by,
        "orchestras": orchestras,
        "choruses": choruses,
        "soloists": soloists,
        "librettists": librettists,
        "speed": vinyl_attrs["speed"],
        "weight": vinyl_attrs["weight"],
        "pressing": vinyl_attrs["pressing"],
        "turntable": vinyl_attrs["turntable"],
        "cartridge": vinyl_attrs["cartridge"],
        "phono_preamp": vinyl_attrs["phono_preamp"],
        "adc": vinyl_attrs["adc"],
        "vinyl_condition": vinyl_attrs["vinyl_condition"],
        "is_live": is_live,
        "venue": venue,
        "location": location,
        "recording_date": recording_date,
        "is_hdcd": is_hdcd,
        "resolved_tracktotal": resolved_tracktotal,
        "disc_track_counts": local_disc_counts,
        "flac_files": flac_files,
        "dr_source": dr_src,
        "leading_intro_variant": mb_leading_intro,
        "album_complete": completeness.state == "COMPLETE",
        "album_completeness_state": completeness.state,
        "album_completeness_evidence": completeness.evidence,
        "spars_code": spars_code,
    }
    # The ranked candidate is only a proposal until the curated local-cover
    # preservation gate has run. Report the artwork that will actually be
    # staged, and retain the proposal separately for diagnostics.
    playback_cover = (
        existing_cover
        if cover_file.is_file() and not replace_cover_file and existing_cover is not None
        else selected_cover
    )
    if playback_cover:
        playback_crop = (
            playback_cover.crop_box
            if playback_cover is selected_cover and (replace_cover_file or not cover_file.is_file())
            else None
        )
        album_tags["cover_selection"] = {
            "target_musicbrainz_release_id": cover_mb_release_id,
            "target_discogs_release_id": id_val,
            "provider": playback_cover.provider,
            "provider_image_id": playback_cover.provider_image_id,
            "provider_release_id": playback_cover.provider_release_id,
            "source_sha256": playback_cover.source_sha256,
            "provenance": playback_cover.provenance.value,
            "representation": playback_cover.representation_kind.value,
            "source_form": playback_cover.source_form,
            "playback_form": "extracted_front_cover" if playback_crop else "source_front_cover",
            "crop_box": list(playback_crop) if playback_crop else None,
            "crop_confidence": playback_cover.crop_confidence if playback_crop else 0.0,
            "proposed_winner_provider": selected_cover.provider if selected_cover else "",
            "proposed_winner_sha256": selected_cover.source_sha256 if selected_cover else "",
            "playback_sha256": hashlib.sha256(
                cover_tmp.read_bytes()
                if cover_tmp.is_file() and (replace_cover_file or not cover_file.is_file())
                else cover_file.read_bytes()
                if cover_file.is_file()
                else b""
            ).hexdigest(),
            "release_tier": playback_cover.release_tier.name,
            "release_confidence": playback_cover.release_confidence,
            "role_tier": playback_cover.role_tier.name,
            "role_confidence": playback_cover.role_confidence,
            "width": playback_cover.width,
            "height": playback_cover.height,
            "effective_width": playback_cover.width if not playback_crop else playback_cover.effective_width,
            "effective_height": playback_cover.height if not playback_crop else playback_cover.effective_height,
            "visual_quality_score": playback_cover.visual_quality_score,
            "score_breakdown": playback_cover.score_breakdown,
            "explanation": playback_cover.selection_explanation,
            "replace_existing_front": replace_existing_front,
            "replace_front_hashes": sorted(replace_front_hashes),
            "replace_cover_file": replace_cover_file,
            "displayed_source_sha256": (
                playback_cover.source_sha256
                if replace_cover_file or not cover_file.exists()
                else hashlib.sha256(cover_file.read_bytes()).hexdigest()
            ),
        }

    # Pre-calculate EAC Peak Table across the album for Steve Hoffman Forums mastering index
    album_peaks: list[str] = []
    for t_idx in range(1, len(flac_files) + 1):
        pk = get_track_eac_peak(rip_meta_files, t_idx)
        if pk:
            album_peaks.append(pk.replace(" %", ""))
    if album_peaks and len(album_peaks) == len(flac_files):
        album_tags["album_eac_peaks"] = " / ".join(album_peaks)

    # Parallel Track Processing (Multithreaded!)
    print(f">>> ПАРАЛЛЕЛЬНАЯ ОБРАБОТКА ТРЕКОВ ({threads} потоков)...")
    track_results: list[tuple[bool, str]] = []
    try:
        transaction_dir, pending_files = stage_album_files(
            folder, flac_files, generated_dr_report=generated_dr_reports[-1] if generated_dr_reports else ""
        )
    except OSError as exc:
        if cover_tmp.is_file():
            cover_tmp.unlink()
        if cover_source_tmp and cover_source_tmp.is_file():
            cover_source_tmp.unlink()
        print(f"ERROR: Album staging failed before any source FLAC was changed: {exc}", file=sys.stderr)
        return False
    pending_root = transaction_dir / "pending"
    backup_root = transaction_dir / "originals"
    pending_ledger = transaction_dir / "provenance.jsonl"

    def discard_transaction() -> None:
        if transaction_dir.is_dir() and transaction_dir.name.startswith(".metadata-transaction-"):
            shutil.rmtree(transaction_dir)

    def discard_cover_staging() -> None:
        if cover_tmp.is_file():
            cover_tmp.unlink()
        if cover_source_tmp and cover_source_tmp.is_file():
            cover_source_tmp.unlink()

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
        future_to_track = {
            executor.submit(
                process_single_track,
                f=flac,
                folder=folder,
                artist_name=artist_name,
                album_clean=album_clean,
                discogs_json=discogs_json,
                mb_recordings_json=mb_recordings_json,
                rip_meta_files=rip_meta_files,
                dr_track_info=dr_track_map.get(str(flac.relative_to(folder))),
                album_dr=album_dr,
                album_tags=album_tags,
                session=session,
                user_agent=user_agent,
                cache_dir_mb=cache_dir_mb,
                cover_file=(
                    cover_tmp
                    if cover_tmp.is_file() and (replace_cover_file or not cover_file.is_file())
                    else cover_file
                )
                if (cover_file.is_file() or cover_tmp.is_file())
                else None,
                write_target=pending_files[flac],
                provenance_file=pending_ledger,
                dry_run=dry_run,
                verbose=is_verbose,
            ): flac
            for flac in flac_files
        }

        for future in concurrent.futures.as_completed(future_to_track):
            flac = future_to_track[future]
            try:
                ok, msg = future.result()
                track_results.append((ok, msg))
                print(f"  -> {msg}", flush=True)
            except Exception as exc:
                track_results.append((False, f"Exception processing {flac.name}: {exc}"))
                print(f"ERROR: Track {flac.name} failed: {exc}", file=sys.stderr, flush=True)

    if not track_results or not all(ok for ok, _message in track_results):
        discard_transaction()
        discard_cover_staging()
        print("ERROR: Album transaction rolled back because one or more tracks failed.", file=sys.stderr)
        return False

    if dry_run:
        discard_transaction()
        discard_cover_staging()
        print(
            "  -> DRY RUN: no FLAC, artwork, lyric sidecar, DR report, ReplayGain tag, or provenance ledger was committed."
        )
        return True

    # ReplayGain 2.0 (loudgain)
    if no_loudgain or not load_config().loudness.enabled:
        if not quiet:
            reason = "--no-loudgain" if no_loudgain else "policy loudness.enabled=false"
            print(f"  -> REPLAYGAIN: skipped ({reason}).")
    else:
        loudness_outcome: dict[str, str] = {}
        loudness_written = run_loudgain(
            [pending_files[path] for path in flac_files],
            quiet=quiet,
            verbose=is_verbose,
            complete_album=bool(album_tags.get("album_complete")),
            outcome=loudness_outcome,
        )
        if loudness_outcome.get("status") == "failed":
            if loudness_outcome.get("recovery_required") == "true":
                print(f"CRITICAL: ReplayGain recovery files retained in {transaction_dir}", file=sys.stderr)
                return False
            discard_transaction()
            discard_cover_staging()
            print("ERROR: Album transaction rolled back because ReplayGain failed.", file=sys.stderr)
            return False
        if loudness_written:
            record = {
                "schema_version": 1,
                "kind": "loudness_scan",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "tool": loudness_outcome["tool"],
                "mode": loudness_outcome["mode"],
                "reference_loudness": "-18 LUFS" if loudness_outcome["tool"] == "loudgain" else None,
                "prevent_clipping": bool(load_config().loudness.prevent_clipping)
                if loudness_outcome["tool"] == "loudgain"
                else None,
                "peak_kind": "true-peak"
                if loudness_outcome["tool"] == "loudgain"
                else "existing measurement; unverified origin",
                "command_profile": json.loads(loudness_outcome["command_profile"])
                if loudness_outcome.get("command_profile")
                else None,
                "scanner_executable_sha256": loudness_outcome.get("scanner_executable_sha256"),
                "files": [str(path.relative_to(folder)) for path in flac_files],
            }
            with pending_ledger.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    # Post-verification of invariants
    print(">>> ВЕРИФИКАЦИЯ МЕТАДАННЫХ АЛЬБОМА...")
    all_ok = True
    for flac in flac_files:
        try:
            metadata_writer.canonical_verify(pending_files[flac])
            if is_verbose:
                print(f"[VERBOSE][POST-VERIFY] {flac.name}: Core tags, DR14, and invariants passed.")
        except Exception as exc:
            all_ok = False
            print(f"ERROR: Post-verification failed for {flac.name}: {exc}", file=sys.stderr)

    if all_ok:
        committed: list[Path] = []
        committed_sidecars: list[Path] = []
        cover_was_replaced = False
        cover_backup = backup_root / "cover.jpg"
        commit_locks = ExitStack()
        try:
            # The merged ledger used by the journal must be the exact ledger
            # eventually published. Other cooperating albums may share it.
            commit_locks.enter_context(metadata_writer.file_lock(folder / ".metadata-provenance.jsonl"))
            outputs = [(pending_files[original], original) for original in flac_files]
            for sidecar in sorted(pending_root.rglob("*")):
                if sidecar.is_file() and sidecar.suffix.casefold() != ".flac":
                    destination = folder / sidecar.relative_to(pending_root)
                    if not destination.exists():
                        outputs.append((sidecar, destination))
            if cover_tmp.is_file() and (not cover_file.exists() or replace_cover_file):
                if cover_file.exists():
                    previous_digest = hashlib.sha256(cover_file.read_bytes()).hexdigest()[:16]
                    previous_cover = folder / f"cover.previous.{previous_digest}{cover_file.suffix.lower()}"
                    if not previous_cover.exists():
                        outputs.append((cover_file, previous_cover))
                outputs.append((cover_tmp, cover_file))
            if cover_source_tmp and cover_source_tmp.is_file():
                source_destination = folder / f"cover.original{cover_source_tmp.suffix.lower()}"
                if source_destination.exists() and source_destination.read_bytes() != cover_source_tmp.read_bytes():
                    source_destination = folder / (
                        f"cover.original.{selected_cover.source_sha256[:16] if selected_cover else 'unknown'}"
                        f"{cover_source_tmp.suffix.lower()}"
                    )
                if not source_destination.exists():
                    outputs.append((cover_source_tmp, source_destination))
            if pending_ledger.is_file():
                final_ledger = folder / ".metadata-provenance.jsonl"
                if final_ledger.is_symlink():
                    raise ValueError("refusing symlinked provenance ledger")
                merged_ledger = transaction_dir / "merged-ledger.jsonl"
                merged_ledger.write_bytes(
                    (final_ledger.read_bytes() if final_ledger.exists() else b"") + pending_ledger.read_bytes()
                )
                outputs.append((merged_ledger, final_ledger))
            album_transaction.prepare_commit(folder, transaction_dir, outputs)
            for original in flac_files:
                relative = original.relative_to(folder)
                backup = backup_root / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, backup)
            for original in flac_files:
                committed.append(original)
                os.replace(pending_files[original], original)
            # Lyrics are staged beside their disposable FLAC and are copied
            # only after every audio replacement has succeeded. `_safe_path`
            # ensures these are new candidate files, never overwrites local
            # curated sidecars.
            for staged_sidecar in sorted(pending_root.rglob("*")):
                if not staged_sidecar.is_file() or staged_sidecar.suffix.lower() == ".flac":
                    continue
                destination = folder / staged_sidecar.relative_to(pending_root)
                if destination.is_symlink():
                    raise RuntimeError(f"refusing symlinked sidecar during commit: {destination.name}")
                if destination.exists():
                    if destination.read_bytes() == staged_sidecar.read_bytes():
                        continue
                    raise RuntimeError(f"refusing to overwrite existing sidecar during commit: {destination.name}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                publish_new_artifact(staged_sidecar, destination, committed_sidecars)
            if cover_tmp.is_file() and (not cover_file.exists() or replace_cover_file):
                if cover_file.exists():
                    cover_backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(cover_file, cover_backup)
                    previous_digest = hashlib.sha256(cover_file.read_bytes()).hexdigest()[:16]
                    previous_cover = folder / f"cover.previous.{previous_digest}{cover_file.suffix.lower()}"
                    if previous_cover.is_symlink():
                        raise RuntimeError(f"refusing symlinked artwork backup: {previous_cover}")
                    if previous_cover.exists():
                        if previous_cover.read_bytes() != cover_file.read_bytes():
                            raise FileExistsError(f"artwork backup collision: {previous_cover}")
                    else:
                        publish_new_artifact(cover_backup, previous_cover, committed_sidecars)
                    cover_was_replaced = True
                if cover_was_replaced:
                    os.replace(cover_tmp, cover_file)
                else:
                    publish_new_artifact(cover_tmp, cover_file, committed_sidecars)
            if cover_source_tmp and cover_source_tmp.is_file():
                source_destination = folder / f"cover.original{cover_source_tmp.suffix.lower()}"
                if source_destination.is_symlink():
                    raise RuntimeError(f"refusing symlinked artwork archive: {source_destination}")
                if source_destination.exists() and source_destination.read_bytes() != cover_source_tmp.read_bytes():
                    source_destination = folder / (
                        f"cover.original.{selected_cover.source_sha256[:16] if selected_cover else 'unknown'}"
                        f"{cover_source_tmp.suffix.lower()}"
                    )
                if source_destination.is_symlink():
                    raise RuntimeError(f"refusing symlinked artwork archive: {source_destination}")
                if source_destination.exists():
                    if source_destination.read_bytes() != cover_source_tmp.read_bytes():
                        raise FileExistsError(f"artwork archive collision: {source_destination}")
                else:
                    publish_new_artifact(cover_source_tmp, source_destination, committed_sidecars)
            if pending_ledger.is_file():
                final_ledger = folder / ".metadata-provenance.jsonl"
                commit_provenance_ledger(final_ledger, pending_ledger)
            album_transaction.finish_commit(folder, transaction_dir)
        except BaseException as exc:
            if (transaction_dir / album_transaction.JOURNAL).is_file():
                try:
                    album_transaction.reset_for_rollback(transaction_dir)
                except BaseException as journal_exc:
                    print(
                        f"CRITICAL: cannot re-arm recovery journal ({journal_exc}); outputs and snapshots retained in {transaction_dir}",
                        file=sys.stderr,
                    )
                    if not isinstance(journal_exc, Exception):
                        raise
                    if not isinstance(exc, Exception):
                        raise exc
                    return False
            rollback_errors: list[str] = []
            for original in committed:
                # A replacement that raised before publication still has its
                # staged source. Do not retry that failed destination in rollback.
                if pending_files[original].exists():
                    continue
                backup = backup_root / original.relative_to(folder)
                try:
                    if not backup.is_file():
                        raise FileNotFoundError(f"missing rollback copy: {backup}")
                    os.replace(backup, original)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{original.name}: {rollback_exc}")
            for sidecar in committed_sidecars:
                try:
                    if sidecar.is_file():
                        sidecar.unlink()
                except OSError as rollback_exc:
                    rollback_errors.append(f"{sidecar.name}: {rollback_exc}")
            if cover_was_replaced:
                try:
                    if not cover_backup.is_file():
                        raise FileNotFoundError(f"missing cover rollback copy: {cover_backup}")
                    os.replace(cover_backup, cover_file)
                except OSError as rollback_exc:
                    rollback_errors.append(f"cover.jpg: {rollback_exc}")
            if not rollback_errors and (transaction_dir / album_transaction.JOURNAL).is_file():
                try:
                    album_transaction.recover_transaction(folder, transaction_dir)
                except (OSError, ValueError, TypeError, KeyError) as recovery_exc:
                    rollback_errors.append(f"journal recovery: {recovery_exc}")
            if rollback_errors:
                print(
                    f"CRITICAL: Album commit failed ({exc}); rollback is incomplete. "
                    f"Preserved recovery files in {transaction_dir}: {'; '.join(rollback_errors)}",
                    file=sys.stderr,
                )
                if not isinstance(exc, Exception):
                    raise
                return False
            discard_cover_staging()
            discard_transaction()
            print(f"ERROR: Album commit failed and was rolled back: {exc}", file=sys.stderr)
            if not isinstance(exc, Exception):
                raise
            return False
        finally:
            commit_locks.close()
        print("  -> POST-VERIFY: core tags, DR14, and duplicate guards passed.")
        discard_transaction()
        discard_cover_staging()
    else:
        discard_transaction()
        discard_cover_staging()
        print("ERROR: Album transaction rolled back after verification failure.", file=sys.stderr)
    return all_ok


def load_env_file(env_path: Path | None = None) -> None:
    """Load key=value pairs from .env file into os.environ if not already present."""
    candidates = [env_path] if env_path else [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]
    for p in candidates:
        if p and p.is_file():
            try:
                for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k and k not in os.environ:
                        os.environ[k] = v
                break
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    parser = argparse.ArgumentParser(
        description="AudiophileArchTagger 6.0: High-performance multithreaded metadata tagging engine."
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="Quiet non-interactive mode")
    parser.add_argument(
        "--manual-covers", action="store_true", help="Choose from all valid discovered front covers for each album"
    )
    parser.add_argument(
        "--auto-covers", action="store_true", help="Use automatic artwork ranking without an interactive selection"
    )
    parser.add_argument(
        "--no-local-covers",
        action="store_true",
        help="Ignore local covers during selection and replace them with an exact-release remote front",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Enable verbose diagnostics (-v for detailed logs, -vv to render all found cover candidates in terminal)",
    )
    parser.add_argument("--no-loudgain", "--skip-loudgain", action="store_true", help="Skip ReplayGain calculation")
    parser.add_argument("--no-dr14", "--skip-dr14", action="store_true", help="Skip DR14 dynamic range measurement")
    parser.add_argument(
        "--dry-run", action="store_true", help="Resolve and report metadata decisions without modifying library files"
    )
    parser.add_argument("--mb-results", type=int, default=10, help="MusicBrainz candidate results limit (1..99)")
    parser.add_argument("--discogs-id", default="", help="Override Discogs release ID")
    parser.add_argument("--discogs-token", default="", help="Discogs user token (or set DISCOGS_TOKEN in .env/env)")
    parser.add_argument("--mb-contact", default="", help="MusicBrainz contact email (or set MB_CONTACT in .env/env)")
    parser.add_argument("-j", "--threads", type=int, default=min(8, os.cpu_count() or 4), help="Worker threads count")
    parser.add_argument(
        "--embed-max-dim",
        type=int,
        default=1400,
        help="Max pixel dimension for embedded artwork (default: 1400)",
    )
    parser.add_argument(
        "--embed-full-res",
        action="store_true",
        help="Embed full-resolution artwork without resizing",
    )
    parser.add_argument(
        "--preview-size",
        type=int,
        default=int(os.environ.get("COVER_PREVIEW_SIZE", "1200")),
        help="Target pixel dimension for terminal cover preview (default: 1200)",
    )
    parser.add_argument("--helper", choices=["eac-peak", "acoustid", "acoustid-candidates"], help=argparse.SUPPRESS)
    parser.add_argument("--flac", help=argparse.SUPPRESS)
    parser.add_argument("--track-num", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--meta-files", nargs="*", default=[], help=argparse.SUPPRESS)
    parser.add_argument("patterns", nargs="*", help="Album folders, glob patterns, or keywords")

    args = parser.parse_args(argv)
    try:
        load_config()
    except (OSError, ValueError, TypeError) as exc:
        parser.error(f"invalid policy configuration: {exc}")
    if args.manual_covers and args.auto_covers:
        parser.error("--manual-covers and --auto-covers cannot be combined")
    if args.threads < 1:
        parser.error("--threads must be at least 1")
    if not 1 <= args.mb_results <= 99:
        parser.error("--mb-results must be in 1..99")
    if args.embed_max_dim < 1:
        parser.error("--embed-max-dim must be at least 1")
    if args.preview_size < 1:
        parser.error("--preview-size must be at least 1")
    if args.preview_size:
        os.environ["COVER_PREVIEW_SIZE"] = str(args.preview_size)

    if args.helper:
        if args.helper == "eac-peak":
            flac_p = Path(args.flac) if args.flac else None
            meta_paths = [Path(p) for p in args.meta_files]
            pk = get_track_eac_peak(meta_paths, args.track_num, flac_path=flac_p)
            print(pk)
            return 0
        elif args.helper == "acoustid":
            flac_p = Path(args.flac) if args.flac else None
            if not flac_p or not flac_p.is_file():
                print(json.dumps({}))
                return 0
            dur, fp = compute_acoustid_fingerprint(flac_p)
            session = requests.Session()
            aid = resolve_acoustid_id(dur, fp, session)
            print(json.dumps({"fingerprint": fp, "duration": dur, "acoustid_id": aid}))
            return 0
        elif args.helper == "acoustid-candidates":
            flac_p = Path(args.flac) if args.flac else None
            if not flac_p or not flac_p.is_file():
                print(json.dumps({"release_ids": [], "recording_ids": []}))
                return 0
            session = requests.Session()
            rels, recs = query_acoustid_candidates(flac_p, session)
            print(json.dumps({"release_ids": rels, "recording_ids": sorted(recs)}))
            return 0

    discogs_token = args.discogs_token.strip() or os.environ.get("DISCOGS_TOKEN", "").strip()
    if not discogs_token:
        print("ERROR: DISCOGS_TOKEN is not set in environment, .env, or CLI (--discogs-token).", file=sys.stderr)
        print("  -> Get a free token at https://www.discogs.com/settings/developers", file=sys.stderr)
        print("  -> Set it via: export DISCOGS_TOKEN='your_token' or create a .env file", file=sys.stderr)
        return 1

    mb_contact = (
        args.mb_contact.strip() or os.environ.get("MB_CONTACT", "").strip() or "AudiophileArchTagger@example.com"
    )

    base_dir = Path.cwd().resolve()
    target_entries = resolve_target_albums(args.patterns, base_dir=base_dir)

    if not target_entries:
        print("No matching album directories found.", file=sys.stderr)
        return 0

    interactive_covers = not (args.auto_covers or args.quiet or args.dry_run or args.manual_covers)
    if (interactive_covers or args.manual_covers) and not sys.stdin.isatty():
        print(
            "ERROR: interactive artwork selection requires a terminal. Use --auto-covers or --quiet for automation.",
            file=sys.stderr,
        )
        return 2

    embed_dim = None if args.embed_full_res else args.embed_max_dim
    if args.verbose > 0:
        os.environ["AUDIOPHILE_VERBOSE"] = str(args.verbose)
    session = requests.Session()
    success = True
    for entry in target_entries:
        try:
            ok = process_album(
                entry=entry,
                discogs_token=discogs_token,
                mb_contact=mb_contact,
                quiet=args.quiet,
                no_loudgain=args.no_loudgain,
                no_dr14=args.no_dr14,
                threads=args.threads,
                session=session,
                discogs_id_override=args.discogs_id,
                mb_results_limit=args.mb_results,
                embed_max_dim=embed_dim,
                dry_run=args.dry_run,
                verbose=args.verbose,
                manual_covers=args.manual_covers,
                no_local_covers=args.no_local_covers,
                interactive_covers=interactive_covers,
            )
            if not ok:
                success = False
        except KeyboardInterrupt:
            print("\nAborted by user.", file=sys.stderr)
            return 130
        except Exception as exc:
            print(f"Unhandled error processing {entry}: {exc}", file=sys.stderr)
            success = False

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
