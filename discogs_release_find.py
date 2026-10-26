#!/usr/bin/env python3
"""Autonomous physical release discovery and verifier for Discogs.

Identifies the exact audiophile physical release on Discogs for a local album folder
without requiring a pre-known Discogs ID. Leverages:
1. Physical CD TOC and MusicBrainz DiscID calculated via mb_discid_from_cue.
2. MusicBrainz URL relationships (discid -> MB release -> Discogs release URL).
3. EAC/XLD logs, CUE sheets, and TXT files for catalog numbers, barcodes, and matrix runouts.
4. FLAC audio stream signatures (track count, individual track durations, total duration).
5. Multi-tiered Discogs Database Search with strict audiophile scoring.
"""

from __future__ import annotations

import argparse
import difflib
import glob
import json
import os
import re
import subprocess
import sys
import unicodedata
import urllib.parse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import requests
from mutagen.flac import FLAC

from library_files import is_pipeline_private, iter_library_flacs
from library_layout import DISC_DIR_RE, position_evidence
from mb_discid_from_cue import build, read_text_robust
from mb_identity_score import rank_acoustid_results
from pipeline_config import load_config
from provider_cache import DISCOGS_LIMITER, MUSICBRAINZ_LIMITER, JsonCache, fetch_json

_DISCOGS_LIMITER = DISCOGS_LIMITER
_MB_LIMITER = MUSICBRAINZ_LIMITER


def norm(s: object) -> str:
    return re.sub(r"[^\w]+", "", unicodedata.normalize("NFC", str(s or "")).casefold(), flags=re.UNICODE)


def catnorm(s: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").casefold())


def barnorm(s: object) -> str:
    return re.sub(r"[^0-9]+", "", str(s or ""))


def dur_to_seconds(val: object) -> float | None:
    if val is None:
        return None
    s = str(val).strip()
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


class DiscogsFinder:
    def __init__(
        self,
        folder: Path,
        artist: str = "",
        album: str = "",
        discogs_token: str = "",
        mb_contact: str = "",
        quiet: bool = False,
    ) -> None:
        self.folder = folder.resolve()
        self.quiet = quiet
        self.token = discogs_token or os.environ.get("DISCOGS_TOKEN", "").strip()
        self.mb_contact = mb_contact or os.environ.get("MB_CONTACT", "AudiophileArchTagger@example.com").strip()
        self.user_agent = f"AudiophileArchTagger/6.0 ({self.mb_contact})"

        self.cache_dir_discogs = (
            Path(os.environ.get("SCRIPT_DIR", str(Path(__file__).parent))) / ".api_cache" / "discogs"
        )
        self.cache_dir_discogs.mkdir(parents=True, exist_ok=True)
        self.cache_dir_mb = (
            Path(os.environ.get("SCRIPT_DIR", str(Path(__file__).parent))) / ".api_cache" / "musicbrainz"
        )
        self.cache_dir_mb.mkdir(parents=True, exist_ok=True)

        self.session = requests.Session()
        self.flac_files = iter_library_flacs(self.folder)
        self.artist = artist
        self.album = album
        self._init_artist_and_album()

    def _init_artist_and_album(self) -> None:
        if not self.album:
            raw = self.folder.name
            cleaned = re.sub(r"\s*[\(\[]\d{4}(?:-\d{2}(?:-\d{2})?)?[\)\]]\s*$", "", raw).strip()
            self.album = cleaned

        if not self.artist:
            parent = self.folder.parent
            if parent != self.folder and parent.name and parent.name != ".":
                self.artist = parent.name.strip()

        # Fallback to CUE sheet or FLAC tags
        if not self.artist or not self.album:
            cues = list(self.folder.glob("*.cue"))
            if cues:
                txt = read_text_robust(cues[0])
                for line in txt.splitlines():
                    m = re.match(r'(?i)^\s*PERFORMER\s+"([^"]+)"', line)
                    if m and not self.artist:
                        self.artist = m.group(1).strip()
                    m = re.match(r'(?i)^\s*TITLE\s+"([^"]+)"', line)
                    if m and not self.album:
                        self.album = re.sub(r"\s*\[[^\]]+\]", "", m.group(1)).strip()

        if self.flac_files and (not self.artist or not self.album):
            try:
                audio = FLAC(str(self.flac_files[0]))
                if audio.tags:
                    if not self.artist:
                        art = audio.tags.get("ALBUMARTIST") or audio.tags.get("ARTIST")
                        if art:
                            self.artist = str(art[0]).strip()
                    if not self.album:
                        alb = audio.tags.get("ALBUM")
                        if alb:
                            self.album = str(alb[0]).strip()
            except Exception:
                pass

    def discogs_api_get(self, url: str) -> dict[str, Any] | None:
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Discogs token={self.token}"
        result = fetch_json(
            self.session,
            url,
            headers=headers,
            cache=JsonCache(self.cache_dir_discogs),
            attempts=4,
            timeout=15,
            rate_limiter=_DISCOGS_LIMITER,
        )
        if result.ok and isinstance(result.payload, dict):
            return result.payload
        if result.status != 404:
            print(f"WARNING: Discogs discovery request failed: {result.classification}", file=sys.stderr)
        return None

    def mb_api_get(self, url: str) -> dict[str, Any] | None:
        result = fetch_json(
            self.session,
            url,
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            cache=JsonCache(self.cache_dir_mb),
            attempts=4,
            timeout=15,
            rate_limiter=_MB_LIMITER,
        )
        if result.ok and isinstance(result.payload, dict):
            return result.payload
        if result.status != 404:
            print(f"WARNING: MusicBrainz discovery request failed: {result.classification}", file=sys.stderr)
        return None

    def extract_local_signatures(self) -> dict[str, Any]:
        """Collect all physical and acoustic clues from files in the folder."""
        catnos: set[str] = set()
        barcodes: set[str] = set()
        matrices: set[str] = set()
        keywords: set[str] = set()
        country_hints: set[str] = set()
        edition_years: set[str] = set()
        known_discogs_ids: set[str] = set()

        # 1. Existing FLAC tags
        local_durations: list[float] = []
        local_titles: list[str] = []
        local_track_numbers: list[int] = []
        positioned: list[tuple[int, int, str, float]] = []

        for f in self.flac_files:
            try:
                audio = FLAC(str(f))
                if audio.info and audio.info.total_samples and audio.info.sample_rate:
                    local_durations.append(round(audio.info.total_samples / audio.info.sample_rate, 3))
                tags = audio.tags
                if tags is None:
                    continue
                for k in ("CATALOGNUMBER", "CATALOG_NUMBER", "CATALOG"):
                    for v in tags.get(k, []):
                        if str(v).strip():
                            catnos.add(str(v).strip())
                for v in tags.get("BARCODE", []):
                    b = barnorm(v)
                    if len(b) in (11, 12, 13):
                        barcodes.add(b)
                for v in tags.get("DISCOGS_RELEASE_ID", []):
                    if str(v).strip().isdigit():
                        known_discogs_ids.add(str(v).strip())
                for v in tags.get("EDITION", []):
                    if str(v).strip():
                        keywords.add(str(v).strip())
                for v in tags.get("RELEASECOUNTRY", []):
                    if str(v).strip():
                        country_hints.add(str(v).strip())
                title_val = str(tags.get("TITLE", [""])[0]).strip()
                if not title_val:
                    title_val = re.sub(r"^(?:\d{1,2}[-_.:]+)?\d+[\s._-]+", "", f.stem).strip()
                local_titles.append(title_val)

                tnum_val = None
                raw_t = tags.get("TRACKNUMBER", [""])[0]
                if "/" in raw_t:
                    raw_t = raw_t.split("/")[0]
                if raw_t.strip().isdigit():
                    tnum_val = int(raw_t.strip())
                if tnum_val is None:
                    m = re.search(r"(?:^|[^\d])(?:\d{1,2})[-_.:]+(\d{1,3})(?:[^\d]|$)", f.stem)
                    if m:
                        tnum_val = int(m.group(1))
                if tnum_val is not None:
                    local_track_numbers.append(tnum_val)
                disc, track, _title, conflicts = position_evidence(f, self.folder, tags)
                if disc is not None and track is not None and not conflicts and audio.info:
                    positioned.append((disc, track, title_val, round(audio.info.length, 3)))
            except Exception:
                pass

        # Filename enumeration is deterministic but is not musical order.
        # Missing/conflicting/duplicate positions cannot support an ordered
        # duration/title comparison against a provider tracklist.
        if len(positioned) == len(self.flac_files) and len({row[:2] for row in positioned}) == len(positioned):
            positioned.sort(key=lambda row: row[:2])
            local_titles = [row[2] for row in positioned]
            local_durations = [row[3] for row in positioned]
        else:
            local_titles, local_durations = [], []

        # 2. Extract from CUE, LOG, TXT, ACCURIP
        cue_and_log_files = sorted(
            path
            for path in self.folder.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and not is_pipeline_private(path, root=self.folder)
            and path.suffix.casefold() in {".cue", ".log", ".txt", ".accurip"}
        )

        for meta_file in cue_and_log_files:
            # Check brackets in file name
            for m in re.finditer(r"\[([A-Za-z0-9 -]{3,30})\]", meta_file.name):
                cand = m.group(1).strip()
                if not re.match(r"^[0-9A-Fa-f]{8}$", cand):
                    catnos.add(cand)
            for m in re.finditer(r"\(([A-Za-z0-9 -]{3,30})\)", meta_file.name):
                cand = m.group(1).strip()
                if re.match(r"^\d{4}$", cand):
                    edition_years.add(cand)
                elif not re.match(r"^[0-9A-Fa-f]{8}$", cand):
                    catnos.add(cand)

            txt = read_text_robust(meta_file)
            for line in txt.splitlines():
                # Check Filename path in EAC log
                if re.match(r"(?i)^\s*Filename\s+", line):
                    for bracketed in re.findall(r"[\(\[]([^\)\]]+)[\)\]]", line):
                        bracketed = bracketed.strip()
                        if any(
                            k in bracketed.lower()
                            for k in (
                                "dcc",
                                "mfsl",
                                "mofi",
                                "cp32",
                                "cdp",
                                "32xd",
                                "emi",
                                "parlophone",
                                "elektra",
                                "japan",
                                "germany",
                                "holland",
                                "uk",
                                "udcd",
                                "audio fidelity",
                                "target",
                                "reissue",
                            )
                        ):
                            keywords.add(bracketed)
                            # Catalog regex: e.g. "EMI-Toshiba CP32-5152, Japan" -> "CP32-5152"
                            m_cat = re.search(r"\b([A-Z0-9]{2,8}[ -][A-Z0-9 -]{3,15})\b", bracketed)
                            if m_cat:
                                catnos.add(m_cat.group(1).strip())
                            if "dcc" in bracketed.lower():
                                keywords.add("DCC")
                            if "japan" in bracketed.lower():
                                country_hints.add("Japan")
                            if "germany" in bracketed.lower() or "w.germany" in bracketed.lower():
                                country_hints.add("Germany")
                            if "holland" in bracketed.lower() or "netherlands" in bracketed.lower():
                                country_hints.add("Netherlands")
                            if "uk" in bracketed.lower():
                                country_hints.add("UK")

                # CUE TITLE [catno]
                m_title = re.match(r'(?i)^\s*TITLE\s+"[^"]*\[([A-Za-z0-9 -]{3,30})\]', line)
                if m_title and not re.match(r"^[0-9A-Fa-f]{8}$", m_title.group(1)):
                    catnos.add(m_title.group(1).strip())

                # CUE REM CATALOGNUMBER
                m_rem_cat = re.match(r'(?i)^\s*REM\s+CATALOG(?:NUMBER)?\s+"?([^"]+)"?', line)
                if m_rem_cat:
                    catnos.add(m_rem_cat.group(1).strip())

                # CUE REM DATE
                m_rem_date = re.match(r"(?i)^\s*REM\s+DATE\s+(\d{4})", line)
                if m_rem_date:
                    edition_years.add(m_rem_date.group(1))

                # CUE CATALOG (barcode)
                m_cat_bar = re.match(r"(?i)^\s*CATALOG\s+(\d{10,14})", line)
                if m_cat_bar and m_cat_bar.group(1) != "0" * len(m_cat_bar.group(1)):
                    barcodes.add(m_cat_bar.group(1).strip())

                # LOG explicit fields
                m_log_cat = re.search(
                    r"(?i)\b(?:cat(?:alog)?\.?\s*(?:no|num|number)?|order\s*no)\s*[:=]\s*([A-Za-z0-9 -]{4,25})",
                    line,
                )
                if m_log_cat and not re.match(r"^[0-9A-Fa-f]{8}$", m_log_cat.group(1)):
                    catnos.add(m_log_cat.group(1).strip())

                m_log_bar = re.search(r"(?i)\bbarcode\s*[:=]\s*([0-9 -]{10,20})", line)
                if m_log_bar:
                    clean_b = barnorm(m_log_bar.group(1))
                    if len(clean_b) in (11, 12, 13) and clean_b != "0" * len(clean_b):
                        barcodes.add(clean_b)

                m_log_mat = re.search(r"(?i)\bmatrix\s*(?:/\s*runout)?\s*[:=]\s*([^\r\n]+)", line)
                if m_log_mat:
                    mat_val = m_log_mat.group(1).strip()
                    if len(mat_val) >= 4:
                        matrices.add(mat_val)

        # 3. CD TOC & MusicBrainz DiscID
        toc_info: dict[str, Any] = {}
        try:
            toc_info = build(self.folder)
        except Exception:
            pass

        return {
            "catnos": sorted(catnos),
            "barcodes": sorted(barcodes),
            "matrices": sorted(matrices),
            "keywords": sorted(keywords),
            "country_hints": sorted(country_hints),
            "edition_years": sorted(edition_years),
            "known_discogs_ids": sorted(known_discogs_ids),
            "durations": local_durations,
            "titles": local_titles,
            "track_count": len(self.flac_files),
            "toc_info": toc_info,
            "discid": toc_info.get("discid"),
            "freedb_discid": toc_info.get("freedb_discid"),
        }

    def search_candidates(self, local: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Query Discogs & MusicBrainz in multiple tiers and return candidate releases map."""
        candidates: dict[str, dict[str, Any]] = {}

        def search(url: str) -> dict[str, Any]:
            """Keep bounded pages before edition validation, including partial success."""
            rows: list[dict[str, Any]] = []
            total_pages = 1
            for page in range(1, 4):
                page_url = url if page == 1 else url + f"&page={page}"
                response = self.discogs_api_get(page_url)
                if not response or not isinstance(response.get("results"), list):
                    if page > 1:
                        print("WARNING: Discogs search pagination failed; retained earlier results.", file=sys.stderr)
                    break
                rows.extend(row for row in response["results"][:50] if isinstance(row, dict))
                pagination = response.get("pagination")
                if isinstance(pagination, dict):
                    try:
                        total_pages = max(1, int(pagination.get("pages", 1)))
                    except (TypeError, ValueError, OverflowError):
                        print("WARNING: invalid Discogs search pagination.", file=sys.stderr)
                        break
                if page >= total_pages:
                    break
            if total_pages > 3:
                print("WARNING: Discogs search truncated to 3 pages (at most 150 rows).", file=sys.stderr)
            return {"results": rows}

        def record_candidate(
            release_id: str | int,
            origin: str,
            bonus: int = 0,
            extra: dict[str, Any] | None = None,
        ) -> None:
            rid = str(release_id).strip()
            if not rid or not rid.isdigit():
                return
            if rid not in candidates:
                candidates[rid] = {
                    "id": rid,
                    "origins": [origin],
                    "bridge_bonus": bonus,
                    "extra": extra or {},
                }
            else:
                if origin not in candidates[rid]["origins"]:
                    candidates[rid]["origins"].append(origin)
                candidates[rid]["bridge_bonus"] = max(candidates[rid]["bridge_bonus"], bonus)

        # 0. Pre-existing Discogs ID is a hypothesis, not authority. It can
        # help discovery but must never outweigh physical contradictions.
        for kid in local.get("known_discogs_ids", []):
            record_candidate(kid, "FLAC-tag", bonus=80)

        # Tier 1: MusicBrainz DiscID Bridge (Identical physical CD TOC)
        discid = local.get("discid")
        has_direct_ids = bool(local.get("catnos") or local.get("barcodes") or local.get("known_discogs_ids"))
        if discid and not has_direct_ids:
            mb_disc_url = f"https://musicbrainz.org/ws/2/discid/{discid}?fmt=json"
            mb_resp = self.mb_api_get(mb_disc_url)
            if mb_resp and "releases" in mb_resp:
                for rel in mb_resp["releases"][:3]:
                    rel_id = rel.get("id")
                    if not rel_id:
                        continue
                    mb_rel_url = f"https://musicbrainz.org/ws/2/release/{rel_id}?inc=url-rels+labels&fmt=json"
                    rel_detail = self.mb_api_get(mb_rel_url)
                    if rel_detail:
                        for r_rel in rel_detail.get("relations", []):
                            res = (r_rel.get("url") or {}).get("resource", "")
                            m = re.search(r"discogs\.com/release/(\d+)", res)
                            if m:
                                record_candidate(
                                    m.group(1),
                                    f"MB-DiscID-relation({discid[:8]})",
                                    bonus=1000
                                    if local.get("toc_info", {}).get("toc_exact") is True
                                    and not local.get("toc_info", {}).get("is_partial")
                                    else 100,
                                )

        # Tier 1.5: AcoustID MusicBrainz Bridge (Acoustic audio fingerprint -> MB release -> Discogs URL)
        acoustid_api_key = os.environ.get("ACOUSTID_API_KEY") or os.environ.get("ACOUSTID_CLIENT_KEY")
        if acoustid_api_key and self.flac_files and not candidates:
            try:
                first_flac = self.flac_files[0]
                dur = 0
                fp = ""
                audio = FLAC(str(first_flac))
                if audio.tags:
                    fp_tag = audio.tags.get("ACOUSTID_FINGERPRINT") or audio.tags.get("acoustid_fingerprint")
                    if fp_tag and fp_tag[0]:
                        fp = str(fp_tag[0]).strip()
                if audio.info and audio.info.length:
                    dur = round(audio.info.length)
                if not fp or dur <= 0:
                    proc = subprocess.run(
                        ["fpcalc", "-json", str(first_flac)], capture_output=True, text=True, timeout=15
                    )
                    if proc.returncode == 0:
                        calc_data = json.loads(proc.stdout)
                        dur = round(float(calc_data.get("duration", 0)))
                        fp = calc_data.get("fingerprint", "")
                if fp and dur > 0:
                    acoust_url = "https://api.acoustid.org/v2/lookup"
                    acoustid_params: dict[str, str | int] = {
                        "client": acoustid_api_key,
                        "meta": "recordingids releases",
                        "duration": dur,
                        "fingerprint": fp,
                    }
                    response = fetch_json(
                        self.session,
                        acoust_url + "?" + urllib.parse.urlencode(acoustid_params),
                        headers={"User-Agent": "AudiophileArchTagger/6.0"},
                        cache=JsonCache(self.cache_dir_mb / "acoustid"),
                        timeout=8,
                    )
                    if response.ok:
                        ranked = rank_acoustid_results(response.payload, load_config().matching.acoustid_min_score)
                        release_ids = sorted(
                            {
                                str(release["id"])
                                for result in ranked
                                for recording in result.get("recordings", [])
                                if isinstance(recording, dict)
                                for release in recording.get("releases", [])
                                if isinstance(release, dict) and release.get("id")
                            }
                        )
                        if len(release_ids) > 20:
                            print(
                                "WARNING: AcoustID release bridge truncated to 20 distinct confident candidates.",
                                file=sys.stderr,
                            )
                        for rel_id in release_ids[:20]:
                            mb_rel_url = f"https://musicbrainz.org/ws/2/release/{rel_id}?inc=url-rels+labels&fmt=json"
                            rel_detail = self.mb_api_get(mb_rel_url)
                            if rel_detail:
                                for r_rel in rel_detail.get("relations", []):
                                    resource = (r_rel.get("url") or {}).get("resource", "")
                                    match = re.search(r"discogs\.com/release/(\d+)", resource)
                                    if match:
                                        record_candidate(match.group(1), f"AcoustID-MB-relation({rel_id})", bonus=850)
                    elif not self.quiet:
                        print(f"WARNING: AcoustID discovery: {response.classification}", file=sys.stderr)
            except (OSError, ValueError, TypeError, requests.RequestException, subprocess.SubprocessError) as exc:
                print(f"WARNING: AcoustID discovery failed: {type(exc).__name__}", file=sys.stderr)

        # Tier 2: Catalog Number & Barcode direct search in Discogs
        seen_cat_searches: set[str] = set()
        for cat in local.get("catnos", []):
            clean_c = cat.strip()
            # Only search if contains digits and is >= 3 chars
            if not clean_c or not any(c.isdigit() for c in clean_c) or len(clean_c) < 3:
                continue
            if catnorm(clean_c) in seen_cat_searches:
                continue
            seen_cat_searches.add(catnorm(clean_c))

            # Query with artist and catno
            params = {"catno": clean_c, "type": "release", "per_page": "50"}
            if self.artist:
                params["artist"] = self.artist
            url = f"https://api.discogs.com/database/search?{urllib.parse.urlencode(params)}"
            res = search(url)
            if res and "results" in res:
                for row in res["results"]:
                    record_candidate(row.get("id"), f"Discogs-CatNo({clean_c})", bonus=500, extra=row)

            # Fallback without artist if 0 results
            if not res or not res.get("results"):
                params_no_art = {"catno": clean_c, "type": "release", "per_page": "50"}
                url_no_art = f"https://api.discogs.com/database/search?{urllib.parse.urlencode(params_no_art)}"
                res_no_art = search(url_no_art)
                if res_no_art and "results" in res_no_art:
                    for row in res_no_art["results"]:
                        record_candidate(row.get("id"), f"Discogs-CatNo-global({clean_c})", bonus=400, extra=row)

        for bar in local.get("barcodes", []):
            clean_b = barnorm(bar)
            if len(clean_b) not in (11, 12, 13):
                continue
            url = f"https://api.discogs.com/database/search?{urllib.parse.urlencode({'barcode': clean_b, 'type': 'release', 'per_page': '50'})}"
            res = search(url)
            if res and "results" in res:
                for row in res["results"]:
                    record_candidate(row.get("id"), f"Discogs-Barcode({clean_b})", bonus=600, extra=row)

        # Tier 3: precise edition-keyword searches.  Every request/result is
        # locally scoped; do not reuse a stale barcode response or variable.
        for kw in local.get("keywords", []):
            clean_kw = re.sub(r"[\(\)\[\]]", " ", kw).strip()
            if any(k in clean_kw.lower() for k in ("dcc", "mfsl", "mofi", "cp32", "32xd", "target", "udcd")):
                query_str = f"{self.artist} {self.album} {clean_kw}".strip()
                url = f"https://api.discogs.com/database/search?{urllib.parse.urlencode({'q': query_str, 'type': 'release', 'per_page': '50'})}"
                res = search(url)
                if res and "results" in res:
                    for row in res["results"]:
                        record_candidate(row.get("id"), f"Discogs-Keyword({clean_kw})", bonus=350, extra=row)

        # Tier 4: Artist + Title + CD Format search (Broad fallback)
        if len(candidates) < 3 and self.artist and self.album:
            params = {
                "artist": self.artist,
                "release_title": self.album,
                "format": "CD",
                "type": "release",
                "per_page": "50",
            }
            url = f"https://api.discogs.com/database/search?{urllib.parse.urlencode(params)}"
            res = search(url)
            if res and "results" in res:
                for row in res["results"]:
                    record_candidate(row.get("id"), "Discogs-Artist-Title-CD", bonus=100, extra=row)

        # Tier 5: MusicBrainz Artist + Release query for Discogs relationships
        if len(candidates) < 2 and self.artist and self.album:
            alb_q = urllib.parse.quote(self.album)
            art_q = urllib.parse.quote(self.artist)
            mb_search_url = f"https://musicbrainz.org/ws/2/release/?query=release:%22{alb_q}%22+AND+artist:%22{art_q}%22+AND+format:CD&fmt=json"
            mb_res = self.mb_api_get(mb_search_url)
            if mb_res and "releases" in mb_res:
                for rel in mb_res["releases"][:5]:
                    rel_id = rel.get("id")
                    if not rel_id:
                        continue
                    mb_rel_url = f"https://musicbrainz.org/ws/2/release/{rel_id}?inc=url-rels&fmt=json"
                    rel_det = self.mb_api_get(mb_rel_url)
                    if rel_det:
                        for r_rel in rel_det.get("relations", []):
                            res = (r_rel.get("url") or {}).get("resource", "")
                            m = re.search(r"discogs\.com/release/(\d+)", res)
                            if m:
                                record_candidate(m.group(1), "MB-Release-Search-relation", bonus=300)

        return candidates

    def score_candidate(
        self, release_id: str, local: dict[str, Any], bridge_bonus: int, origins: list[str]
    ) -> dict[str, Any]:
        """Fetch full Discogs release JSON and score against local audiophile truth."""
        url = f"https://api.discogs.com/releases/{release_id}"
        data = self.discogs_api_get(url)
        if not data or not isinstance(data, dict):
            return {"id": release_id, "score": -9999, "rejected": True, "reason": "fetch_failed"}

        # Discovery route affects retrieval order, never edition confidence.
        score = 0
        reasons: list[str] = list(origins)
        rejected = False
        reject_reason = ""

        # Basic properties
        title = str(data.get("title") or "")
        country = str(data.get("country") or "")
        year = str(data.get("year") or "")
        labels = data.get("labels") or []
        catnos = [str(l.get("catno") or "").strip() for l in labels if l.get("catno")]
        label_names = [str(l.get("name") or "").strip() for l in labels if l.get("name")]

        formats = data.get("formats") or []
        format_names = [str(fmt.get("name") or "").upper() for fmt in formats]
        format_descs = []
        for fmt in formats:
            for d in fmt.get("descriptions") or []:
                format_descs.append(str(d))

        identifiers = data.get("identifiers") or []
        barcodes = [
            barnorm(ident.get("value"))
            for ident in identifiers
            if str(ident.get("type")).casefold() == "barcode" and ident.get("value")
        ]
        matrix_values = [
            str(ident.get("value") or "").strip()
            for ident in identifiers
            if "matrix" in str(ident.get("type")).lower() or "runout" in str(ident.get("type")).lower()
        ]

        # 1. Format check
        if "CD" in format_names or "SACD" in format_names:
            score += 150
            reasons.append("format:CD(+150)")
        elif any(v in format_names for v in ("VINYL", "CASSETTE", "SHELLAC")):
            score -= 600
            rejected = True
            reject_reason = "physical_format_conflict"
            reasons.append("format:non-CD(-600)")

        # 2. Track count check
        tracklist = [t for t in data.get("tracklist", []) if t.get("type_") in (None, "track")]
        candidate_track_count = len(tracklist)
        local_track_count = local.get("track_count", 0)

        if local_track_count > 0:
            if candidate_track_count == local_track_count:
                score += 500
                reasons.append(f"tracks:exact-{candidate_track_count}(+500)")
            else:
                score -= 1000
                rejected = True
                reject_reason = f"track_count_conflict({candidate_track_count}!={local_track_count})"
                reasons.append("tracks:edition-conflict(-1000)")

        # 3. Track durations check
        candidate_durations = [dur_to_seconds(t.get("duration")) for t in tracklist]
        cand_valid_durs = [d for d in candidate_durations if d is not None and d > 0]
        local_durs = local.get("durations", [])

        if cand_valid_durs and local_durs:
            compare_len = min(len(candidate_durations), len(local_durs))
            deltas = []
            for i in range(compare_len):
                if candidate_durations[i] is not None:
                    deltas.append(abs(candidate_durations[i] - local_durs[i]))
            if deltas:
                avg_delta = sum(deltas) / len(deltas)
                if avg_delta <= 2.5:
                    score += 350
                    reasons.append(f"duration:avg_delta={avg_delta:.2f}s(+350)")
                elif avg_delta <= 6.0:
                    score += 150
                    reasons.append(f"duration:avg_delta={avg_delta:.2f}s(+150)")
                else:
                    score -= 200
                    reasons.append(f"duration:mismatch-avg={avg_delta:.2f}s(-200)")
                    if len(deltas) >= max(2, int(local_track_count * 0.8)) and avg_delta > 8.0:
                        rejected = True
                        reject_reason = "track_duration_layout_conflict"

        # 4. Catalog Number match
        local_cat_norms = {catnorm(c) for c in local.get("catnos", []) if catnorm(c)}
        cand_cat_norms = {catnorm(c) for c in catnos if catnorm(c)}
        matched_cats = local_cat_norms & cand_cat_norms
        if matched_cats:
            score += 650
            reasons.append(f"catno:match({next(iter(matched_cats))})(+650)")
        else:
            # Substring match for prefixes/suffixes
            for lcn in local_cat_norms:
                for ccn in cand_cat_norms:
                    if len(lcn) >= 4 and len(ccn) >= 4 and (lcn in ccn or ccn in lcn):
                        score += 300
                        reasons.append(f"catno:submatch({lcn}~{ccn})(+300)")
                        break
        if local_cat_norms and cand_cat_norms and not matched_cats:
            rejected = True
            reject_reason = "catalog_number_conflict"

        # 5. Barcode match
        local_barnorms = {barnorm(b) for b in local.get("barcodes", []) if barnorm(b)}
        cand_barnorms = {barnorm(b) for b in barcodes if barnorm(b)}
        matched_bars = local_barnorms & cand_barnorms
        if matched_bars:
            score += 600
            reasons.append(f"barcode:match({next(iter(matched_bars))})(+600)")
        elif local_barnorms and cand_barnorms:
            rejected = True
            reject_reason = "barcode_conflict"

        # 6. Matrix / Runout match
        local_matrices = local.get("matrices", [])
        if local_matrices and matrix_values:
            matched_mat = False
            for lm in local_matrices:
                nlm = catnorm(lm)
                for cm in matrix_values:
                    ncm = catnorm(cm)
                    if len(nlm) >= 6 and (nlm in ncm or ncm in nlm):
                        matched_mat = True
                        break
                if matched_mat:
                    break
            if matched_mat:
                score += 800
                reasons.append("matrix:match(+800)")

        # 7. Country match
        local_countries = {c.lower() for c in local.get("country_hints", [])}
        cand_country = country.lower()
        if cand_country and local_countries and any(lc in cand_country or cand_country in lc for lc in local_countries):
            score += 150
            reasons.append(f"country:match({country})(+150)")

        # 8. Year match
        local_years = local.get("edition_years", [])
        if year and year in local_years:
            score += 120
            reasons.append(f"year:match({year})(+120)")

        # 9. Title similarity
        if self.album:
            title_similarity = difflib.SequenceMatcher(None, norm(self.album), norm(title)).ratio()
            if title_similarity >= 0.75:
                score += int(title_similarity * 200)
                reasons.append(f"title:sim={title_similarity:.2f}(+{int(title_similarity * 200)})")
            else:
                rejected = True
                reject_reason = "album_title_conflict"
                reasons.append(f"title:conflict={title_similarity:.2f}")

        candidate_artist = " ".join(str(a.get("name") or "") for a in data.get("artists") or [] if isinstance(a, dict))
        if self.artist and candidate_artist:
            artist_similarity = difflib.SequenceMatcher(None, norm(self.artist), norm(candidate_artist)).ratio()
            if artist_similarity >= 0.72:
                score += int(artist_similarity * 200)
                reasons.append(f"artist:sim={artist_similarity:.2f}(+{int(artist_similarity * 200)})")
            else:
                rejected = True
                reject_reason = "album_artist_conflict"
                reasons.append(f"artist:conflict={artist_similarity:.2f}")

        # 10. Audiophile keywords in format / notes (DCC, MFSL, Remaster, 1st press)
        all_text = (" ".join(format_descs) + " " + str(data.get("notes") or "") + " " + " ".join(label_names)).lower()
        for kw in local.get("keywords", []):
            clean_kw = re.sub(r"[^a-zA-Z0-9]+", " ", kw).lower().strip()
            words = [w for w in clean_kw.split() if len(w) >= 3 and w not in ("queen", "album", "audio")]
            if words and all(w in all_text for w in words):
                score += 250
                reasons.append(f"keyword:match({clean_kw[:20]})(+250)")
                break

        # Curated summary construction
        label_disp = label_names[0] if label_names else ""
        cat_disp = catnos[0] if catnos else ""
        edition_summary_parts = []
        if year:
            edition_summary_parts.append(year)
        if country:
            edition_summary_parts.append(country)
        if label_disp and cat_disp:
            edition_summary_parts.append(f"{label_disp} {cat_disp}")
        elif label_disp:
            edition_summary_parts.append(label_disp)
        elif cat_disp:
            edition_summary_parts.append(cat_disp)

        audiophile_desc = [
            d for d in format_descs if d.lower() in ("remastered", "reissue", "deluxe edition", "club edition")
        ]
        if audiophile_desc:
            edition_summary_parts.append(" | ".join(audiophile_desc))

        edition_summary = ", ".join(edition_summary_parts) if edition_summary_parts else f"Discogs {release_id}"

        # Confidence categorization
        if score >= 1500 and not rejected:
            confidence = "CERTAIN"
        elif score >= 1000 and not rejected:
            confidence = "EXACT"
        elif score >= 600 and not rejected:
            confidence = "HIGH"
        elif score >= 300 and not rejected:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        return {
            "id": release_id,
            "title": title,
            "country": country,
            "year": year,
            "label": label_disp,
            "catno": cat_disp,
            "edition_summary": edition_summary,
            "score": score,
            "confidence": confidence,
            "rejected": rejected,
            "reject_reason": reject_reason,
            "reasons": reasons,
            "track_count": candidate_track_count,
            "data": data,
        }

    def find(self) -> dict[str, Any]:
        """Execute discovery and return ranked candidate result."""
        local = self.extract_local_signatures()
        raw_candidates = self.search_candidates(local)
        # Score every bounded discovery candidate. Bridge hints prioritize
        # network order only; they are never a reason to hide a stronger
        # physical match that appears later in the raw search result.
        sorted_raw = sorted(raw_candidates.items(), key=lambda item: (-item[1].get("bridge_bonus", 0), str(item[0])))

        scored_candidates: list[dict[str, Any]] = []
        if len(sorted_raw) > 50:
            print(
                f"WARNING: Discogs edition scoring truncated to 50 of {len(sorted_raw)} discovered candidates.",
                file=sys.stderr,
            )
        for rid, meta in sorted_raw[:50]:
            sc = self.score_candidate(rid, local, meta.get("bridge_bonus", 0), meta.get("origins", []))
            if not sc.get("rejected"):
                scored_candidates.append(sc)

        scored_candidates.sort(key=lambda c: (-c["score"], str(c["id"])))

        best = scored_candidates[0] if scored_candidates else None
        second = scored_candidates[1] if len(scored_candidates) > 1 else None
        margin = (best["score"] - second["score"]) if best and second else (best["score"] if best else 0)
        safe = bool(
            best
            and best.get("confidence") in {"HIGH", "EXACT", "CERTAIN"}
            and margin >= load_config().matching.discogs_min_margin
        )
        return {
            "ok": safe,
            "folder": str(self.folder),
            "artist": self.artist,
            "album": self.album,
            "discogs_id": best["id"] if safe and best else "",
            "edition_summary": best["edition_summary"] if safe and best else "",
            "score": best["score"] if best else 0,
            "confidence": best["confidence"] if best else "NONE",
            "margin": margin,
            "review_required": bool(best and not safe),
            "candidates": [
                {
                    "id": c["id"],
                    "year": c.get("year", ""),
                    "country": c.get("country", ""),
                    "label": c.get("label", ""),
                    "catno": c.get("catno", ""),
                    "edition_summary": c.get("edition_summary", ""),
                    "score": c["score"],
                    "confidence": c["confidence"],
                    "reasons": c.get("reasons", [])[:4],
                }
                for c in scored_candidates[:10]
            ],
        }


def find_album_folders(paths: Sequence[str | Path], base_dir: Path | None = None) -> list[Path]:
    """Discover all album directories under given paths (library root, artist dir, album dir, or glob)."""
    album_dirs: set[Path] = set()
    disc_pattern = DISC_DIR_RE
    base = (base_dir or Path.cwd()).resolve()

    def process_dir(root: Path) -> None:
        if root.is_symlink() or not root.is_dir():
            return
        if is_pipeline_private(root, root=base):
            return
        # If directory contains .flac files directly
        if any(
            p.is_file()
            and not p.is_symlink()
            and p.suffix.casefold() == ".flac"
            and not is_pipeline_private(p, root=base)
            for p in root.iterdir()
        ):
            if disc_pattern.match(root.name) and root.parent.exists() and root.parent != base:
                album_dirs.add(root.parent.resolve())
            else:
                album_dirs.add(root.resolve())
            return

        # Check subdirectories containing flacs (depth-first recursive)
        for flac in iter_library_flacs(root):
            if flac.is_symlink():
                continue
            parent = flac.parent.resolve()
            if disc_pattern.match(parent.name) and parent.parent.exists() and parent.parent != base:
                album_dirs.add(parent.parent.resolve())
            else:
                album_dirs.add(parent)

    for p_val in paths:
        raw_str = str(p_val).strip().strip("\"'")
        if not raw_str:
            continue
        p = Path(raw_str)
        if not p.is_absolute():
            p = base / p
        if p.is_symlink() or any(parent.is_symlink() for parent in p.parents) or is_pipeline_private(p, root=base):
            continue
        if p.exists():
            if p.is_file():
                if p.suffix.lower() == ".flac":
                    album_dirs.add(p.parent.resolve())
            elif p.is_dir():
                process_dir(p.resolve())
        else:
            if glob.has_magic(raw_str):
                for name in glob.iglob(str(p), recursive=True):
                    match = Path(name)
                    if match.is_symlink() or is_pipeline_private(match, root=base):
                        continue
                    # glob can traverse symlinked parents. Such matches are
                    # outside the explicit path contract even if the leaf is real.
                    if any(parent.is_symlink() for parent in match.parents):
                        continue
                    if match.is_dir():
                        process_dir(match)
                    elif match.is_file() and match.suffix.casefold() == ".flac":
                        process_dir(match.parent)
                continue
            # Pattern / substring matching against directories
            clean_term = raw_str.strip("\"'")
            matched = False
            for d in base.rglob(f"*{clean_term}*"):
                if d.is_dir() and not d.is_symlink():
                    process_dir(d.resolve())
                    matched = True
            if not matched:
                for d in base.glob(f"*{clean_term}*"):
                    if d.is_dir() and not d.is_symlink():
                        process_dir(d.resolve())

    return sorted(album_dirs, key=lambda p: str(p).lower())


def main() -> int:
    ap = argparse.ArgumentParser(description="Autonomous Discogs release finder")
    ap.add_argument("folder", nargs="*", help="Album/Artist/Library directory to analyze or scan")
    ap.add_argument("--find-albums", action="store_true", help="Discover all album folders under the given paths")
    ap.add_argument("--artist", default="", help="Artist override")
    ap.add_argument("--album", default="", help="Album override")
    ap.add_argument("--discogs-token", default="", help="Discogs API token")
    ap.add_argument("--mb-contact", default="", help="MusicBrainz contact email")
    ap.add_argument("-q", "--quiet", action="store_true", help="Quiet mode (no interactive prompt)")
    ap.add_argument("--json", action="store_true", help="Output full JSON report")
    args = ap.parse_args()

    if args.find_albums:
        targets = args.folder or ["."]
        albums = find_album_folders(targets)
        for alb in albums:
            print(str(alb))
        return 0

    if not args.folder:
        print("ERROR: folder argument required", file=sys.stderr)
        return 1

    folder = Path(args.folder[0])
    if not folder.is_dir():
        print(f"ERROR: folder does not exist: {folder}", file=sys.stderr)
        return 1

    finder = DiscogsFinder(
        folder,
        artist=args.artist,
        album=args.album,
        discogs_token=args.discogs_token,
        mb_contact=args.mb_contact,
        quiet=args.quiet,
    )
    res = finder.find()

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 2

    if not res.get("ok"):
        print("NONE", file=sys.stderr)
        return 2

    # Output format expected by caller: "DISCOGS_ID:::EDITION_SUMMARY"
    print(f"{res['discogs_id']}:::{res['edition_summary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
