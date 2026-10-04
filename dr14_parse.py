#!/usr/bin/env python3
"""Robust parser for dr14_tmeter and foo_dr Dynamic Range reports.

Compatible with dr14_tmeter (both basic and default table formats, as well as
stdout), TT Dynamic Range Meter, and Foobar2000 foo_dr logs (including localized versions).
Handles 'over' peaks, optional duration columns, bracketed codecs, localized units (дБ),
and intelligently maps report track entries to local audio files when provided.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

import numpy as np
import requests

AUDIO_EXT = r"(?:flac|wav|ape|wv|ogg|oga|opus|mp3|m4a|mp4|wma|aiff?|aifc)"
UNIT_RE = r"(?:dB|дБ|дб|DB|db)"


def compute_dr14_samples(samples: np.ndarray, fs: int) -> tuple[int, str, str]:
    """Compute block-based DR14 and measured sine-reference peak/RMS levels.

    This follows the dr14_tmeter approach, including its 44.1 kHz block
    adjustment. It is not a certification of agreement with a proprietary meter.
    """
    if fs <= 0 or samples.ndim not in {1, 2} or not samples.size or not np.isfinite(samples).all():
        raise ValueError("DR measurement requires nonempty finite PCM and a positive sample rate")
    if len(samples.shape) == 1:
        samples = samples.reshape(-1, 1)
    ch = samples.shape[1]
    delta_fs = 60 if fs == 44100 else 0
    block_samples = 3 * (fs + delta_fs)
    seg_cnt = int(np.ceil(samples.shape[0] / block_samples))

    rms = np.zeros((seg_cnt, ch))
    peaks = np.zeros((seg_cnt, ch))
    curr_sam = 0
    for i in range(seg_cnt):
        block = samples[curr_sam : curr_sam + block_samples, :]
        rms[i, :] = np.sqrt(2.0 * np.sum(block**2, axis=0) / float(len(block)))
        peaks[i, :] = np.max(np.abs(block), axis=0)
        curr_sam += block_samples

    peaks = np.sort(peaks, axis=0)
    rms = np.sort(rms, axis=0)
    n_blk = max(1, int(np.floor(seg_cnt * 0.2)))
    r = np.arange(seg_cnt - n_blk, seg_cnt)
    rms_sum = np.sum(rms[r, :] ** 2, axis=0)

    sec_peaks = np.maximum(peaks[max(0, seg_cnt - 2), :], 1e-12)
    active = rms_sum > 1e-24
    ch_dr14 = np.zeros(ch)
    ch_dr14[active] = -20.0 * np.log10(np.sqrt(rms_sum[active] / n_blk) / sec_peaks[active])
    dr14 = max(0, round(float(np.mean(ch_dr14))))

    max_peak = float(np.max(peaks))
    peak_db = 20.0 * np.log10(max(max_peak, 1e-12))
    if max_peak >= 0.9999 or peak_db >= -0.005:
        peak_str = "over"
    else:
        peak_str = f"{peak_db:.2f} dB"

    y_rms = float(np.mean(np.sqrt(2.0 * np.mean(samples**2, axis=0))))
    rms_db = 20.0 * np.log10(max(y_rms, 1e-12))
    if abs(rms_db) < 0.005:
        rms_str = "0.00 dB"
    else:
        rms_str = f"{rms_db:.2f} dB"
    return dr14, peak_str, rms_str


def measure_audio_file(file_path: Path) -> dict[str, object]:
    """Measure DR14 directly from audio file (FLAC or WAV)."""
    p = Path(file_path)
    ext = p.suffix.lower()
    if ext == ".flac":
        try:
            from mutagen.flac import FLAC

            audio = FLAC(str(p))
            bps = audio.info.bits_per_sample
            channels = audio.info.channels
            fs = audio.info.sample_rate
            total_secs = audio.info.length
        except (OSError, ValueError) as exc:
            raise ValueError(f"cannot read FLAC stream geometry: {p.name}") from exc

        cmd = ["flac", "-d", "-c", "-s", "--force-raw-format", "--endian=little", "--sign=signed", str(p)]
        proc = subprocess.run(cmd, capture_output=True, check=True, timeout=300)
        raw = proc.stdout
        if bps == 8:
            s = np.frombuffer(raw, dtype=np.int8).astype(np.float64) / 128.0
        elif bps == 16:
            s = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
        elif bps == 24:
            a = np.frombuffer(raw, dtype=np.uint8)
            a0 = a[0::3].astype(np.int32)
            a1 = a[1::3].astype(np.int32) << 8
            a2 = a[2::3].astype(np.int32) << 16
            sign_extend = np.where(a2 & 0x800000, 0xFF000000, 0)
            s = ((a0 | a1 | a2 | sign_extend).astype(np.int32)).astype(np.float64) / 8388608.0
        elif bps == 32:
            s = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
        else:
            raise ValueError(f"unsupported FLAC PCM depth: {bps}")

        if channels > 1:
            s = s.reshape(-1, channels)
        else:
            s = s.reshape(-1, 1)

        dr_val, peak_str, rms_str = compute_dr14_samples(s, fs)
    elif ext in (".wav", ".wave"):
        import wave

        with wave.open(str(p), "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            fs = wf.getframerate()
            nframes = wf.getnframes()
            total_secs = nframes / float(fs) if fs else 0.0
            raw = wf.readframes(nframes)
        if sampwidth == 2:
            s = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
        elif sampwidth == 3:
            a = np.frombuffer(raw, dtype=np.uint8)
            a0 = a[0::3].astype(np.int32)
            a1 = a[1::3].astype(np.int32) << 8
            a2 = a[2::3].astype(np.int32) << 16
            sign_extend = np.where(a2 & 0x800000, 0xFF000000, 0)
            s = ((a0 | a1 | a2 | sign_extend).astype(np.int32)).astype(np.float64) / 8388608.0
        elif sampwidth == 4:
            s = np.frombuffer(raw, dtype=np.int32).astype(np.float64) / 2147483648.0
        else:
            s = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0

        if channels > 1:
            s = s.reshape(-1, channels)
        else:
            s = s.reshape(-1, 1)

        dr_val, peak_str, rms_str = compute_dr14_samples(s, fs)
    else:
        raise ValueError(f"Direct measurement unsupported for extension: {ext}")

    mins = int(total_secs // 60)
    secs = int(total_secs % 60)
    dur_str = f"{mins:02d}:{secs:02d}"

    return {
        "type": "track",
        "dr": dr_val,
        "name": p.name,
        "peak": peak_str,
        "rms": rms_str,
        "duration": dur_str,
    }


def measure_audio_files(files: list[Path]) -> tuple[list[dict[str, object]], int | None]:
    """Measure a list of audio files and compute overall album DR."""
    tracks: list[dict[str, object]] = []
    for f in sorted(files, key=lambda x: x.name.lower()):
        try:
            m = measure_audio_file(f)
            tracks.append(m)
        except Exception as exc:
            print(f"WARNING: failed to measure DR14 for {f.name}: {exc}", file=sys.stderr)

    if not tracks:
        return [], None

    dr_values: list[int] = []
    for track in tracks:
        raw_dr = track.get("dr")
        if not isinstance(raw_dr, (str, int, float)):
            raise TypeError("measured DR value is not numeric")
        dr_values.append(int(raw_dr))
    album_dr = round(sum(dr_values) / len(dr_values)) if len(tracks) == len(files) else None
    return tracks, album_dr


ROW_RE = re.compile(
    r"^\s*DR(?P<dr>\d{1,2})\s+"
    rf"(?P<peak>[-+]?(?:\d+(?:[.,]\d+)?)\s*{UNIT_RE}|over|[-+]?\d+(?:[.,]\d+)?)\s+"
    rf"(?P<rms>[-+]?(?:\d+(?:[.,]\d+)?)\s*{UNIT_RE}?)\s+"
    r"(?:(?P<duration>\d+(?::\d{2}){1,2}(?:[.,]\d+)?)\s+)?"
    r"(?P<name>[^\r\n]+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

NATIVE_RE = re.compile(
    r"^\s*(?P<name>[^\r\n:]+?)\s*:\s*(?:\[\s*)?DR\s*(?P<dr>\d{1,2})(?:\s*\])?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

OFFICIAL_RE = re.compile(
    r"(?:"
    r"(?:Official\s+DR(?:\s+value)?|Album\s+DR|Actual\s+DR(?:\s+value)?|Estimated\s+DR|"
    r"Реальные?\s+значения?\s+DR|DR\s+альбома|Официальн(?:ый|ое)\s+DR(?:\s+значение)?)\s*:\s*(?:DR\s*)?|"
    r"^\s*DR\s*=\s*"
    r")(?P<dr>\d{1,2})",
    re.IGNORECASE | re.MULTILINE,
)


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "cp1252", "latin-1"):
        try:
            return raw.decode(enc).replace("\x00", "")
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").replace("\x00", "")


def normalize_db(val: str | float) -> str:
    cleaned = str(val).strip()
    if cleaned.lower() == "over":
        return "over"
    cleaned = cleaned.replace(",", ".")
    cleaned = re.sub(r"\s*(?:dB|дБ|дб|DB|db)\s*$", " dB", cleaned, flags=re.IGNORECASE)
    if cleaned in ("-0.00 dB", "-0.00", "-0.0 dB"):
        cleaned = "0.00 dB"
    return cleaned


def clean_track_name(raw_name: str) -> str:
    cleaned = raw_name.strip()
    # Strip trailing codec in brackets, e.g. " [flac]" or "\t [flac]\t"
    cleaned = re.sub(r"\s*\[[a-zA-Z0-9]+\]\s*$", "", cleaned).strip()
    return cleaned


def norm(s: str) -> str:
    return re.sub(r"[^\w]+", "", unicodedata.normalize("NFC", s).casefold(), flags=re.UNICODE)


def extract_track_number(s: str) -> int | None:
    """Extract logical track number handling disc-track prefixes (e.g. '01-08' -> 8)."""
    # 1. Disc-track pattern: "01-08 - ...", "1-08 - ...", "CD1-08"
    m = re.search(r"(?:^|[^\d])(?:\d{1,2})[-_.:]+(\d{1,3})(?:[^\d]|$)", s)
    if m:
        return int(m.group(1))
    # 2. Simple track prefix: "08 - ...", "08. ...", "08-Title", "08 Title"
    m = re.match(r"^\s*(\d{1,3})(?:[^\d]|$)", s)
    if m:
        return int(m.group(1))
    # 3. Word track: "track 08"
    m = re.search(r"\btrack\s*(\d{1,3})\b", s, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def parse(text: str) -> tuple[list[dict[str, object]], int | None]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    tracks: list[dict[str, object]] = []
    seen: set[str] = set()

    for m in ROW_RE.finditer(text):
        raw_name = m.group("name")
        clean_name = clean_track_name(raw_name)
        if not clean_name:
            continue
        dr_val = int(m.group("dr"))
        key = clean_name.casefold()
        if key in seen:
            continue
        seen.add(key)
        item: dict[str, object] = {
            "type": "track",
            "dr": dr_val,
            "name": clean_name,
            "raw_name": raw_name.strip(),
        }
        if m.group("peak"):
            item["peak"] = normalize_db(m.group("peak"))
        if m.group("rms"):
            item["rms"] = normalize_db(m.group("rms"))
        if m.group("duration"):
            item["duration"] = m.group("duration").strip()
        tracks.append(item)

    if not tracks:
        for m in NATIVE_RE.finditer(text):
            raw_name = m.group("name")
            clean_name = clean_track_name(raw_name)
            if not clean_name:
                continue
            dr_val = int(m.group("dr"))
            key = clean_name.casefold()
            if key in seen:
                continue
            seen.add(key)
            tracks.append(
                {
                    "type": "track",
                    "dr": dr_val,
                    "name": clean_name,
                    "raw_name": raw_name.strip(),
                }
            )

    official = list(OFFICIAL_RE.finditer(text))
    album_dr = int(official[-1].group("dr")) if official else None
    if album_dr is None:
        track_drs = [
            int(val)
            for t in tracks
            if (val := t.get("dr")) is not None
            and (isinstance(val, (int, float)) or (isinstance(val, str) and val.isdigit()))
        ]
        if track_drs:
            album_dr = round(sum(track_drs) / len(track_drs))
    return tracks, album_dr


def resolve_to_files(tracks: list[dict[str, object]], target_files: list[Path]) -> list[dict[str, object]]:
    """Map parsed report entries to local audio files when possible."""
    if not target_files:
        for t in tracks:
            t["name"] = Path(str(t["name"])).name
            t.pop("raw_name", None)
        return tracks

    available = list(target_files)
    resolved: list[dict[str, object]] = []

    for t in tracks:
        report_path = Path(str(t["name"]))
        c_name = report_path.name
        stem = Path(c_name).stem
        matches = (
            [
                f
                for f in available
                if tuple(part.casefold() for part in f.parts[-len(report_path.parts) :])
                == tuple(part.casefold() for part in report_path.parts)
            ]
            if len(report_path.parts) > 1
            else [f for f in available if f.name.casefold() == c_name.casefold()]
        )
        if not matches:
            matches = [f for f in available if f.stem.casefold() == stem.casefold()]
        if not matches:
            report_title = norm(re.sub(r"^(?:\d{1,2}[-_.:]+)?\d+[\s._-]+", "", stem).strip())
            report_number = extract_track_number(stem)
            if report_title:
                matches = [
                    f
                    for f in available
                    if norm(re.sub(r"^(?:\d{1,2}[-_.:]+)?\d+[\s._-]+", "", f.stem).strip()) == report_title
                    and (report_number is None or extract_track_number(f.stem) in (None, report_number))
                ]
        # A row from an unrelated report can share a track number or its
        # position. Only a unique name/title match can attach measurements.
        if len(matches) != 1:
            continue
        matched = matches[0]
        available.remove(matched)
        res_item = dict(t)
        res_item["name"] = matched.name
        res_item["path"] = str(matched)
        res_item.pop("raw_name", None)
        resolved.append(res_item)

    return resolved


def find_report_in_dir(folder: Path) -> Path | None:
    for cand in ("foo_dr.txt", "foo_dr.log", "dr_report.txt", "dr.txt", "dr14.txt"):
        p = folder / cand
        if p.is_file() and p.stat().st_size > 0:
            return p
    for p in sorted(folder.glob("*.txt")):
        if (
            re.search(r"(?:^|[\W_])(?:dr\d*|dynamic[-_ ]?range)(?:[\W_]|$)", p.stem, re.IGNORECASE)
            and p.is_file()
            and p.stat().st_size > 0
        ):
            return p
    return None


def generate_dr14_report(
    artist: str,
    album: str,
    tracks: list[dict[str, object]],
    album_dr: int | str,
) -> str:
    """Generate standardized TT Dynamic Range Meter report (dr14.txt) for dr.loudness-war.info."""
    artist_hdr = artist.strip() or "Unknown Artist"
    album_hdr = album.strip() or "Unknown Album"
    lines = [
        "-" * 80,
        f"Analyzed: {artist_hdr} / {album_hdr}",
        "-" * 80,
        "",
        f"{'DR':<10}{'Peak':<12}{'RMS':<12}{'Duration':<10}Track",
        "-" * 80,
    ]
    for t in tracks:
        dr_str = f"DR{t.get('dr', '')}"
        peak_str = str(t.get("peak", "")).strip()
        rms_str = str(t.get("rms", "")).strip()
        dur_str = str(t.get("duration", "")).strip()
        name_str = str(t.get("name", "")).strip()
        lines.append(f"{dr_str:<10}{peak_str:<12}{rms_str:<12}{dur_str:<10}{name_str}")

    dr_val_str = str(album_dr) if album_dr is not None else ""
    lines.extend(
        [
            "-" * 80,
            "",
            f"Number of tracks:  {len(tracks)}",
            f"Official DR value: DR{dr_val_str}",
            "=" * 80,
            "",
        ]
    )
    return "\n".join(lines)


def save_dr14_report(folder: Path, report_content: str) -> Path:
    """Save standardized dr14.txt in target folder if not already existing."""
    target = folder / "dr14.txt"
    if target.is_symlink():
        raise ValueError(f"refusing symlinked DR report: {target}")
    try:
        with target.open("x", encoding="utf-8") as handle:
            handle.write(report_content)
    except FileExistsError:
        # Existing reports, including empty user files, are preservation inputs.
        pass
    return target


def parse_dr_search_html(html_text: str) -> list[dict[str, Any]]:
    """Parse search result HTML table from dr.loudness-war.info."""
    results: list[dict[str, Any]] = []
    row_matches = re.finditer(
        r'<tr[^>]*>.*?<a\s+href="[^"]*?/album/view/(\d+)"[^>]*>(.*?)</a>.*?</tr>',
        html_text,
        re.DOTALL | re.IGNORECASE,
    )
    for m in row_matches:
        db_id = m.group(1)
        row_html = m.group(0)
        cols = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.DOTALL | re.IGNORECASE)
        clean_cols = [re.sub(r"<[^>]+>", "", c).strip() for c in cols]
        dr_m = re.search(r"\d+", clean_cols[3]) if len(clean_cols) > 3 else None
        min_m = re.search(r"\d+", clean_cols[4]) if len(clean_cols) > 4 else None
        max_m = re.search(r"\d+", clean_cols[5]) if len(clean_cols) > 5 else None

        item: dict[str, Any] = {
            "id": db_id,
            "url": f"https://dr.loudness-war.info/album/view/{db_id}",
            "artist": clean_cols[0] if len(clean_cols) > 0 else "",
            "album": clean_cols[1] if len(clean_cols) > 1 else "",
            "year": clean_cols[2] if len(clean_cols) > 2 else "",
            "dr": int(dr_m.group()) if dr_m else None,
            "min_dr": int(min_m.group()) if min_m else None,
            "max_dr": int(max_m.group()) if max_m else None,
            "codec": clean_cols[6] if len(clean_cols) > 6 else "",
            "notes": clean_cols[7] if len(clean_cols) > 7 else "",
        }
        results.append(item)
    return results


def search_dr_database(
    artist: str,
    album: str,
    session: requests.Session | None = None,
    timeout: int = 10,
) -> list[dict[str, Any]]:
    """Query dr.loudness-war.info search interface with robust timeout and error handling."""
    import urllib.parse

    params = urllib.parse.urlencode({"artist": artist.strip(), "album": album.strip()})
    url = f"https://dr.loudness-war.info/album/list?{params}"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        req = session if session is not None else requests
        resp = req.get(url, headers=headers, timeout=timeout)
        if resp.status_code == 200:
            return parse_dr_search_html(resp.text)
    except Exception:
        pass
    return []


def match_dr_entry(
    entries: list[dict[str, Any]],
    target_dr: int | None = None,
    target_year: str | None = None,
    catalog_no: str | None = None,
    target_media: str = "CD",
) -> dict[str, Any] | None:
    """Find the best matching DR database entry for a specific release edition."""
    if not entries:
        return None
    scored: list[tuple[float, dict[str, Any]]] = []
    is_digital_target = bool(re.search(r"cd|digital|file|flac|sacd|web|lossless", target_media or "CD", re.IGNORECASE))
    for e in entries:
        score = 0.0
        if target_dr is not None and e.get("dr") is not None:
            if e["dr"] == target_dr:
                score += 50.0
            elif abs(e["dr"] - target_dr) == 1:
                score += 20.0
            else:
                score -= 30.0

        if target_year and e.get("year"):
            y_str = str(e["year"]).strip()
            if target_year == y_str or target_year in y_str:
                score += 25.0

        if catalog_no and e.get("notes"):
            norm_cat = re.sub(r"[^a-zA-Z0-9]+", "", catalog_no).lower()
            norm_notes = re.sub(r"[^a-zA-Z0-9]+", "", str(e["notes"])).lower()
            if norm_cat and norm_cat in norm_notes:
                score += 30.0

        codec = str(e.get("codec", "")).lower()
        notes = str(e.get("notes", "")).lower()

        # Penalize vinyl / LP / cassette when looking for digital/CD release
        if is_digital_target:
            if any(v in codec or v in notes for v in ("vinyl", "lp", "2xlp", "turntable", "cassette", "tape")):
                score -= 100.0
            if "cd" in codec:
                score += 20.0
        else:
            if any(v in codec or v in notes for v in ("vinyl", "lp", "2xlp", "turntable")):
                score += 30.0

        scored.append((score, e))

    scored.sort(key=lambda x: x[0], reverse=True)
    if scored and scored[0][0] > 0:
        return scored[0][1]
    # A search hit is not an edition match.  Returning the first row here used
    # to attach unrelated DR Database IDs whenever every candidate conflicted.
    return None


def main() -> int:
    args = list(sys.argv[1:])
    if "--match-db" in args:
        idx = args.index("--match-db")
        args.pop(idx)
        import argparse

        db_parser = argparse.ArgumentParser(description="Match DR database entry")
        db_parser.add_argument("artist")
        db_parser.add_argument("album")
        db_parser.add_argument("--catno", default="")
        db_parser.add_argument("--dr", type=int, default=None)
        db_parser.add_argument("--year", default="")
        db_parser.add_argument("--media", default="CD")
        db_args, _ = db_parser.parse_known_args(args)
        entries = search_dr_database(db_args.artist, db_args.album)
        matched = match_dr_entry(
            entries,
            target_dr=db_args.dr,
            target_year=db_args.year,
            catalog_no=db_args.catno,
            target_media=db_args.media,
        )
        if matched:
            print(
                json.dumps(
                    {
                        "dr_loudness_war_id": str(matched.get("id", "")),
                        "dr_loudness_war_url": str(matched.get("url", "")),
                        "dr": matched.get("dr"),
                        "min_dr": matched.get("min_dr"),
                        "max_dr": matched.get("max_dr"),
                        "year": matched.get("year"),
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(json.dumps({}))
        return 0

    measure_mode = False
    if "--measure" in args:
        measure_mode = True
        args.remove("--measure")

    extra_files: list[Path] = []
    source_path: Path | None = None
    content: str = ""

    if measure_mode:
        if not args:
            args = ["."]
        for arg in args:
            p = Path(arg)
            if p.is_dir():
                for ext in ("flac", "wav", "ape", "wv"):
                    extra_files.extend(sorted(p.glob(f"*.{ext}")))
            elif p.is_file() and p.suffix.lower().lstrip(".") in ("flac", "wav", "ape", "wv"):
                extra_files.append(p)
        tracks, album_dr = measure_audio_files(extra_files)
        if extra_files and album_dr is not None:
            try:
                first_f = extra_files[0].resolve()
                album_dir = first_f.parent
                cand_reports = [
                    album_dir / name for name in ("dr14.txt", "foo_dr.txt", "foo_dr.log", "dr_report.txt", "dr.txt")
                ]
                if not any(r.is_file() for r in cand_reports):
                    artist_cand = album_dir.parent.name if album_dir.parent != album_dir else ""
                    album_cand = album_dir.name
                    rep = generate_dr14_report(artist_cand, album_cand, tracks, album_dr)
                    save_dr14_report(album_dir, rep)
            except Exception:
                pass
        for row in tracks:
            print(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        print(json.dumps({"type": "album", "dr": album_dr}, ensure_ascii=False, separators=(",", ":")))
        return 0 if extra_files and len(tracks) == len(extra_files) else 1

    if len(args) < 1:
        found = find_report_in_dir(Path.cwd())
        if not found:
            # Fallback to direct measurement if audio files exist in cwd
            audio_files = []
            for ext in ("flac", "wav", "ape", "wv"):
                audio_files.extend(sorted(Path.cwd().glob(f"*.{ext}")))
            if audio_files:
                tracks, album_dr = measure_audio_files(audio_files)
                for row in tracks:
                    print(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
                print(json.dumps({"type": "album", "dr": album_dr}, ensure_ascii=False, separators=(",", ":")))
                return 0 if len(tracks) == len(audio_files) else 1
            print("usage: dr14_parse.py [--measure] [dr14.txt|-|DIR] [AUDIO_FILES...]", file=sys.stderr)
            return 2
        source_path = found
        content = read_text(source_path)
    else:
        source_arg = args[0]
        if source_arg == "-":
            content = sys.stdin.read()
        else:
            p = Path(source_arg)
            if p.is_dir():
                found = find_report_in_dir(p)
                if not found:
                    audio_files = []
                    for ext in ("flac", "wav", "ape", "wv"):
                        audio_files.extend(sorted(p.glob(f"*.{ext}")))
                    if audio_files:
                        tracks, album_dr = measure_audio_files(audio_files)
                        for row in tracks:
                            print(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
                        print(json.dumps({"type": "album", "dr": album_dr}, ensure_ascii=False, separators=(",", ":")))
                        return 0 if len(tracks) == len(audio_files) else 1
                    print(f"ERROR: no DR report or audio files found in directory: {p}", file=sys.stderr)
                    return 1
                source_path = found
                content = read_text(source_path)
                for ext in ("flac", "wav", "ape", "wv", "mp3", "m4a"):
                    extra_files.extend(sorted(p.glob(f"*.{ext}")))
            elif not p.is_file():
                # Report doesn't exist; check if trailing args contain audio files to measure
                cand_audios = [
                    Path(a)
                    for a in args[1:]
                    if Path(a).is_file() and Path(a).suffix.lower().lstrip(".") in ("flac", "wav", "ape", "wv")
                ]
                if cand_audios:
                    tracks, album_dr = measure_audio_files(cand_audios)
                    for row in tracks:
                        print(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
                    print(json.dumps({"type": "album", "dr": album_dr}, ensure_ascii=False, separators=(",", ":")))
                    return 0 if len(tracks) == len(cand_audios) else 1
                print(f"ERROR: report does not exist: {p}", file=sys.stderr)
                return 1
            else:
                source_path = p
                content = read_text(source_path)

    for arg in args[1:]:
        p = Path(arg)
        if p.is_file():
            extra_files.append(p)
        elif Path(p.name).is_file():
            extra_files.append(Path(p.name))
        elif p.is_dir():
            for ext in ("flac", "wav", "ape", "wv", "mp3", "m4a"):
                extra_files.extend(sorted(p.glob(f"*.{ext}")))
        elif p.suffix.lower().lstrip(".") in (
            "flac",
            "wav",
            "ape",
            "wv",
            "ogg",
            "oga",
            "opus",
            "mp3",
            "m4a",
            "aiff",
            "aif",
        ):
            extra_files.append(Path(p.name))

    seen_files: set[str] = set()
    deduped_files: list[Path] = []
    for f in extra_files:
        key = str(f.resolve()) if f.exists() else str(f)
        if key not in seen_files:
            seen_files.add(key)
            deduped_files.append(f)
    extra_files = deduped_files

    tracks, album_dr = parse(content)
    tracks = resolve_to_files(tracks, extra_files)

    for row in tracks:
        print(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
    print(json.dumps({"type": "album", "dr": album_dr}, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
