#!/usr/bin/env python3
"""Calculate a MusicBrainz Disc ID from an EAC CUE/LOG pair or directory of FLAC files.

EAC/CUE track positions use the same LBA coordinate system: CUE INDEX 01 ==
EAC Start sector. The 150-frame offset is added only when constructing the MB TOC.
The parser is tolerant of Windows EAC encodings and does not require the exact
TOC heading text. When no CUE is present, it can construct TOC layout from audio files.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
from pathlib import Path

from mutagen.flac import FLAC

from library_files import is_pipeline_private, iter_library_flacs
from library_layout import position_evidence

ENCODINGS = ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "cp1252", "latin-1")

# These values are part of the matching contract.  Do not make consumers guess
# authenticity from display strings.
EXACT_TOC_SOURCES = frozenset({"EAC+CUE", "EAC+CUE+CD-EXTRA", "CUE+FLAC-IMAGE"})
SYNTHETIC_TOC_SOURCES = frozenset({"FLAC-DIR", "CUE+FLAC-TRACKS", "CUE-ONLY"})


def read_text_robust(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    if b"\x00" in raw[:4096]:
        for enc in ("utf-16-le", "utf-16-be"):
            try:
                t = raw.decode(enc)
                if "Exact Audio Copy" in t or "TOC" in t:
                    return t
            except UnicodeDecodeError:
                pass
    for enc in ENCODINGS:
        try:
            t = raw.decode(enc)
            if "Exact Audio Copy" in t or "TOC" in t or "TRACK" in t:
                return t
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1", errors="replace")


def parse_time_to_frames(v: str) -> int:
    parts = v.strip().split(":")
    if len(parts) == 3:
        mm, ss, ff = map(int, parts)
        minutes = mm
    elif len(parts) == 4:
        hh, mm, ss, ff = map(int, parts)
        minutes = hh * 60 + mm
    else:
        raise ValueError(f"unsupported CUE time: {v!r}")
    if not (0 <= ss < 60 and 0 <= ff < 75):
        raise ValueError(f"invalid CD frame time: {v!r}")
    return (minutes * 60 + ss) * 75 + ff


def parse_cue(cue: Path) -> list[dict]:
    current_file = None
    tracks = []
    all_tracks = []
    for raw in read_text_robust(cue).splitlines():
        line = raw.strip()
        m = re.match(r'(?i)^FILE\s+(?:"([^"]+)"|(\S+))\s+\S+', line)
        if m:
            current_file = m.group(1) or m.group(2)
            continue
        m = re.match(r"(?i)^TRACK\s+(\d{1,2})\s+(\S+)\s*$", line)
        if m:
            t = {
                "number": int(m.group(1)),
                "mode": m.group(2).upper(),
                "file": current_file,
                "index01": None,
                "title": "",
            }
            all_tracks.append(t)
            if t["mode"] == "AUDIO":
                tracks.append(t)
            continue
        m = re.match(r'(?i)^TITLE\s+(?:"([^"]*)"|([^\r\n]+))\s*$', line)
        if m and all_tracks:
            title_val = (m.group(1) or m.group(2) or "").strip()
            all_tracks[-1]["title"] = title_val
            if all_tracks[-1]["mode"] == "AUDIO":
                tracks[-1]["title"] = title_val
            continue
        m = re.match(r"(?i)^INDEX\s+01\s+(\d+(?::\d{2}){2,3})\s*$", line)
        if m and all_tracks:
            val = parse_time_to_frames(m.group(1))
            all_tracks[-1]["index01"] = val
            if all_tracks[-1]["mode"] == "AUDIO":
                tracks[-1]["index01"] = val
    if any(t["index01"] is None for t in tracks):
        raise ValueError("audio TRACK is missing INDEX 01 in CUE")
    numbers = [int(str(t["number"])) for t in tracks]
    if len(numbers) != len(set(numbers)) or numbers != sorted(numbers):
        raise ValueError("CUE audio track numbers are duplicated or out of order")
    valid_tracks = tracks
    if not valid_tracks:
        raise ValueError("No audio TRACK/INDEX 01 entries found in CUE")
    return valid_tracks


def cddb_sum(n: int) -> int:
    ret = 0
    while n > 0:
        ret += n % 10
        n //= 10
    return ret


def calc_freedb_discid(offsets: list[int], leadout: int) -> str:
    if not offsets or leadout <= offsets[0]:
        return ""
    n = sum(cddb_sum(off // 75) for off in offsets)
    total_sec = (leadout - offsets[0]) // 75
    discid = ((n % 0xFF) << 24) | (total_sec << 8) | len(offsets)
    return f"{discid:08X}"


def parse_cue_freedb_discid(cue: Path) -> str:
    for raw in read_text_robust(cue).splitlines():
        m = re.match(r"(?i)^REM\s+DISCID\s+([0-9A-F]{8})\s*$", raw.strip())
        if m:
            return m.group(1).upper()
    return ""


def parse_eac_log_toc(log: Path) -> list[dict]:
    rows = []
    rx = re.compile(
        r"^\s*(\d{1,2})\s*\|\s*([0-9]+:[0-9]{2}\.[0-9]{2})\s*\|\s*([0-9]+:[0-9]{2}\.[0-9]{2})\s*\|\s*(\d+)\s*\|\s*(\d+)\s*$"
    )
    for raw in read_text_robust(log).splitlines():
        m = rx.match(raw.replace("\r", ""))
        if m:
            rows.append(
                {
                    "number": int(m.group(1)),
                    "start_sector": int(m.group(4)),
                    "end_sector": int(m.group(5)),
                    "start_time": m.group(2),
                    "length_time": m.group(3),
                }
            )
    return rows


def find_eac_logs(cue: Path) -> list[Path]:
    return sorted(
        (p for p in cue.parent.iterdir() if p.is_file() and p.suffix.lower() == ".log"),
        key=lambda p: p.name.lower(),
    )


def _log_matches_cue(cue: Path, tracks: list[dict], rows: list[dict]) -> bool:
    """Require independent local evidence before trusting a log's physical TOC."""
    audio_rows = [row for row in rows if int(row["number"]) in {int(track["number"]) for track in tracks}]
    if [int(row["number"]) for row in audio_rows] != [int(track["number"]) for track in tracks]:
        return False
    is_multi_file = len({track.get("file") for track in tracks}) > 1 or (
        len(tracks) > 1 and all(int(track["index01"]) == 0 for track in tracks[1:])
    )
    if not is_multi_file:
        return all(int(track["index01"]) == int(row["start_sector"]) for track, row in zip(tracks, audio_rows))
    names = [track.get("file") for track in tracks]
    if len(set(names)) != len(tracks) or any(not name for name in names):
        return False
    for track, row in zip(tracks, audio_rows):
        candidate = cue.parent / str(track["file"])
        if not candidate.is_file() or candidate.suffix.casefold() != ".flac":
            return False
        try:
            info = FLAC(candidate).info
            local_frames = round(info.total_samples * 75 / info.sample_rate)
        except (OSError, ValueError, ZeroDivisionError):
            return False
        log_frames = int(row["end_sector"]) - int(row["start_sector"]) + 1
        if abs(local_frames - log_frames) > 150:
            return False
    return True


def mb_id(first: int, last: int, leadout: int, offsets: list[int]) -> str:
    if not (1 <= first <= 99 and first <= last <= 99):
        raise ValueError(f"invalid track range {first}-{last}")
    if len(offsets) != last - first + 1:
        raise ValueError("track offset count does not match first/last track")
    values = [leadout, *offsets, *([0] * (100 - 1 - len(offsets)))]
    payload = f"{first:02X}{last:02X}" + "".join(f"{x:08X}" for x in values[:100])
    return (
        base64.b64encode(hashlib.sha1(payload.encode("ascii")).digest())
        .decode("ascii")
        .translate(str.maketrans("+/=", "._-"))
    )


def build_from_flac_dir(folder: Path, *, files: list[Path] | None = None) -> dict:
    """Build TOC and durations directly from FLAC files in a folder when no CUE exists."""
    flac_files = (
        files
        if files is not None
        else [
            path
            for path in folder.iterdir()
            if path.is_file()
            and path.suffix.casefold() == ".flac"
            and not path.is_symlink()
            and not is_pipeline_private(path)
        ]
    )
    numbered: list[tuple[int, Path]] = []
    for path in flac_files:
        audio = FLAC(path)
        _disc, number, _title, conflicts = position_evidence(path, folder, audio.tags)
        if conflicts:
            raise ValueError(f"conflicting track position in {path.name}: {'; '.join(conflicts)}")
        numbered.append((number if number is not None else 1000, path))
    flac_files = [path for _number, path in sorted(numbered, key=lambda item: (item[0], item[1].name.casefold()))]
    if not flac_files:
        raise ValueError(f"No CUE or FLAC files found in directory: {folder}")

    track_frames: list[int] = []
    titles: list[str] = []
    track_numbers: list[int] = []

    for f in flac_files:
        audio = FLAC(str(f))
        if not audio.info or not audio.info.total_samples or not audio.info.sample_rate:
            raise ValueError(f"Cannot read audio stream info from: {f}")
        tf = round(audio.info.total_samples * 75.0 / audio.info.sample_rate)
        track_frames.append(tf)
        title = ""
        if audio.tags and audio.tags.get("TITLE"):
            title = str(audio.tags["TITLE"][0])
        if not title:
            title = re.sub(r"^(?:\d{1,2}[-_.:]+)?\d+[\s._-]+", "", f.stem).strip() or f.stem
        titles.append(title)

        t_num: int | None = None
        if audio.tags and audio.tags.get("TRACKNUMBER"):
            raw_t = str(audio.tags["TRACKNUMBER"][0]).split("/")[0].strip()
            if raw_t.isdigit():
                t_num = int(raw_t)
        if t_num is None:
            m = re.search(r"(?:^|[^\d])(?:\d{1,2})[-_.:]+(\d{1,3})(?:[^\d]|$)", f.stem)
            if not m:
                m = re.match(r"^\s*(\d{1,3})(?:[^\d]|$)", f.stem)
            if m:
                t_num = int(m.group(1))
        if t_num is not None:
            track_numbers.append(t_num)

    is_contiguous = bool(len(track_numbers) == len(flac_files) and track_numbers == list(range(1, len(flac_files) + 1)))
    is_partial = not is_contiguous

    offsets = [150]
    for tf in track_frames[:-1]:
        offsets.append(offsets[-1] + tf)
    leadout = offsets[-1] + track_frames[-1]

    durs = [round((offsets[i + 1] - offsets[i]) / 75.0, 6) for i in range(len(offsets) - 1)]
    durs.append(round((leadout - offsets[-1]) / 75.0, 6))

    freedb = calc_freedb_discid(offsets, leadout) if not is_partial else ""
    discid = mb_id(1, len(offsets), leadout, offsets) if not is_partial else ""

    return {
        "discid": discid if discid else None,
        "freedb_discid": freedb if freedb else None,
        "first_track": 1,
        "last_track": track_numbers[-1] if track_numbers else len(offsets),
        "track_count": len(offsets),
        "offsets": offsets if not is_partial else [],
        "leadout": leadout if not is_partial else None,
        "toc": [1, len(offsets), leadout, *offsets] if not is_partial else [],
        "toc_durations": durs,
        "toc_titles": titles,
        "toc_exact": False,
        "is_partial": is_partial,
        "cue_eac_consistent": False,
        "data_tracks_present": False,
        "source": "FLAC-DIR",
        "eac_log": None,
        "cue": str(folder),
    }


def build(cue: Path) -> dict:
    if cue.is_dir():
        cues = sorted(
            (p for p in cue.iterdir() if p.is_file() and p.suffix.lower() == ".cue"),
            key=lambda p: p.name.lower(),
        )
        if cues:
            cue = cues[0]
        else:
            return build_from_flac_dir(cue)

    ct = parse_cue(cue)
    nums = [int(t["number"]) for t in ct]
    freedb = parse_cue_freedb_discid(cue)
    log_used = None
    rows = []
    examined_logs = False
    for log in find_eac_logs(cue):
        r = parse_eac_log_toc(log)
        if r:
            examined_logs = True
        if r and _log_matches_cue(cue, ct, r):
            rows = r
            log_used = str(log)
            break

    data_rows = []
    offsets = []
    leadout = None
    source = "CUE"
    cue_eac_consistent = bool(rows) or not examined_logs

    if rows:
        log_nums = [int(r["number"]) for r in rows]
        audio_rows = rows if log_nums == nums else [r for r in rows if int(r["number"]) in set(nums)]
        if [int(r["number"]) for r in audio_rows] != nums:
            raise ValueError(
                f"CUE/LOG track numbers disagree: CUE={nums!r}, EAC={[int(r['number']) for r in audio_rows]!r}"
            )

        is_multi_file = len({t.get("file") for t in ct}) > 1 or (
            len(ct) > 1 and all(int(t["index01"]) == 0 for t in ct[1:])
        )
        if not is_multi_file:
            mismatches = [
                f"track {t['number']}: CUE={int(t['index01'])}, EAC={int(audio_rows[i]['start_sector'])}"
                for i, t in enumerate(ct)
                if int(t["index01"]) != int(audio_rows[i]["start_sector"])
            ]
            if mismatches:
                cue_eac_consistent = False
                raise ValueError("CUE/LOG INDEX 01 mismatch: " + ", ".join(mismatches))

        offsets = [150 + int(r["start_sector"]) for r in audio_rows]
        data_rows = [r for r in rows if int(r["number"]) not in set(nums)]
        if data_rows:
            later = [r for r in data_rows if int(r["number"]) > nums[-1]]
            if len(data_rows) != 1 or len(later) != 1:
                raise ValueError("unsupported mixed/multi-session TOC; refusing to guess audio lead-out")
            leadout = 150 + int(data_rows[0]["start_sector"]) - 11400
            source = "EAC+CUE+CD-EXTRA"
        else:
            leadout = 150 + int(audio_rows[-1]["end_sector"]) + 1
            source = "EAC+CUE"
    else:
        cue_files = [str(t["file"]) for t in ct if t.get("file")]
        unique_files: list[str] = list(dict.fromkeys(cue_files))
        flac_candidates = sorted(p for p in cue.parent.glob("*.flac") if p.is_file())

        target_image = None
        if len(unique_files) == 1:
            u_file = unique_files[0]
            p = cue.parent / u_file
            if p.exists() and p.suffix.lower() == ".flac":
                target_image = p
            else:
                p_flac = cue.parent / (Path(u_file).stem + ".flac")
                if p_flac.exists():
                    target_image = p_flac
        elif not unique_files and len(flac_candidates) == 1:
            target_image = flac_candidates[0]

        if target_image:
            try:
                audio = FLAC(str(target_image))
                if audio.info and audio.info.total_samples and audio.info.sample_rate:
                    total_frames = round(audio.info.total_samples * 75.0 / audio.info.sample_rate)
                    leadout = 150 + total_frames
                    offsets = [150 + int(t["index01"]) for t in ct]
                    source = "CUE+FLAC-IMAGE"
            except (OSError, ValueError):
                pass
        elif len(flac_candidates) == len(ct):
            try:
                track_frames = []
                for t in ct:
                    cand = None
                    fname = t.get("file")
                    if fname:
                        p = cue.parent / fname
                        if p.exists():
                            cand = p
                        else:
                            p_flac = cue.parent / (Path(fname).stem + ".flac")
                            if p_flac.exists():
                                cand = p_flac
                    if not cand:
                        for f in flac_candidates:
                            if re.search(r"\b0*" + str(t["number"]) + r"\b", f.name):
                                cand = f
                                break
                    if cand:
                        audio = FLAC(str(cand))
                        tf = round(audio.info.total_samples * 75.0 / audio.info.sample_rate)
                        track_frames.append(tf)
                    else:
                        break
                if len(track_frames) == len(ct):
                    offsets = [150]
                    for tf in track_frames[:-1]:
                        offsets.append(offsets[-1] + tf)
                    leadout = offsets[-1] + track_frames[-1]
                    source = "CUE+FLAC-TRACKS"
            except (OSError, ValueError):
                pass

    if not offsets or leadout is None:
        return {
            "discid": "",
            "freedb_discid": freedb,
            "first_track": nums[0],
            "last_track": nums[-1],
            "track_count": len(nums),
            "offsets": [],
            "leadout": 0,
            "toc": [],
            "toc_durations": [],
            "toc_titles": [str(t.get("title") or "") for t in ct],
            "toc_exact": False,
            "cue_eac_consistent": False,
            "data_tracks_present": False,
            "source": "CUE-ONLY",
            "eac_log": log_used,
            "cue": str(cue),
        }

    if leadout <= offsets[-1]:
        raise ValueError(f"invalid lead-out {leadout} <= last track offset {offsets[-1]}")

    if not freedb:
        freedb = calc_freedb_discid(offsets, leadout)

    durs = [round((offsets[i + 1] - offsets[i]) / 75.0, 6) for i in range(len(offsets) - 1)]
    durs.append(round((leadout - offsets[-1]) / 75.0, 6))

    toc_exact = source in EXACT_TOC_SOURCES
    return {
        "discid": mb_id(nums[0], nums[-1], leadout, offsets),
        "freedb_discid": freedb,
        "first_track": nums[0],
        "last_track": nums[-1],
        "track_count": len(offsets),
        "offsets": offsets,
        "leadout": leadout,
        "toc": [nums[0], len(offsets), leadout, *offsets],
        "toc_durations": durs,
        "toc_titles": [str(t.get("title") or "") for t in ct],
        # Separate-track audio cannot reproduce hidden pregaps/HTOA and must
        # never be presented as an authentic physical MusicBrainz Disc ID.
        "toc_exact": toc_exact,
        "cue_eac_consistent": cue_eac_consistent,
        "data_tracks_present": bool(data_rows),
        "source": source,
        "eac_log": log_used,
        "cue": str(cue),
    }


def build_all(folder: Path) -> dict[int, dict]:
    """Build one TOC record per medium, never silently choosing the first CUE.

    CUE files are associated with their DISCNUMBER when available, otherwise
    their deterministic filename order is used.  A caller can therefore keep
    Disc IDs and physical evidence attached to the correct files.
    """
    cues = sorted(
        (
            p
            for p in folder.rglob("*")
            if p.suffix.casefold() == ".cue"
            and p.is_file()
            and not p.is_symlink()
            and not is_pipeline_private(p, root=folder)
        ),
        key=lambda p: str(p).casefold(),
    )
    if not cues:
        by_disc: dict[int, list[Path]] = {}
        for flac_path in iter_library_flacs(folder):
            if flac_path.is_symlink():
                raise ValueError(f"refusing symlinked TOC input: {flac_path}")
            audio = FLAC(flac_path)
            disc, _track, _title, conflicts = position_evidence(flac_path, folder, audio.tags)
            if conflicts:
                raise ValueError(f"conflicting disc position in {flac_path.name}: {'; '.join(conflicts)}")
            group = by_disc.setdefault(disc or 1, [])
            if group and group[0].parent != flac_path.parent:
                raise ValueError(f"disc {disc} spans folders without CUE evidence; refusing synthetic cross-folder TOC")
            group.append(flac_path)
        if not by_disc:
            raise ValueError(f"No CUE or FLAC files found in directory: {folder}")
        return {disc: build_from_flac_dir(group[0].parent, files=group) for disc, group in sorted(by_disc.items())}
    result: dict[int, dict] = {}
    next_disc = 1
    for cue_path in cues:
        info = build(cue_path)
        disc = next_disc
        for flac_path in cue_path.parent.glob("*.flac"):
            try:
                audio = FLAC(str(flac_path))
                tags = audio.tags
                raw = (tags.get("DISCNUMBER", [""]) if tags is not None else [""])[0]
                if str(raw).split("/", 1)[0].strip().isdigit():
                    disc = int(str(raw).split("/", 1)[0].strip())
                    break
            except (OSError, ValueError):
                continue
        if disc in result:
            raise ValueError(f"ambiguous CUE files for disc {disc}: {cue_path}")
        result[disc] = info
        next_disc = max(next_disc + 1, disc + 1)
    return result


def main() -> int:
    if len(sys.argv) < 2:
        return 2
    cue = Path(sys.argv[1]).resolve()
    if not cue.exists():
        print(f"ERROR: CUE or directory does not exist: {cue}", file=sys.stderr)
        return 2
    try:
        info = build(cue)
    except (OSError, ValueError, RuntimeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(
        json.dumps(info, ensure_ascii=False, separators=(",", ":"))
        if len(sys.argv) >= 3 and sys.argv[2] == "--json"
        else info["discid"]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
