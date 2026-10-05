#!/usr/bin/env python3
"""Schema-aware, severity-classified FLAC metadata quality control."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any

from mutagen.flac import FLAC

from library_files import abandoned_transactions, iter_library_flacs
from lyrics_quality import lrc_to_plain, validate_lrc
from metadata_schema import (
    ALIASES,
    ISRC_RE,
    MULTI_VALUED_FIELDS,
    MULTILINE_FIELDS,
    SINGLE_VALUED_FIELDS,
    STABLE_ID_FIELDS,
    UUID_RE,
    valid_date,
)

SINGLE_VALUED_TAGS = set(SINGLE_VALUED_FIELDS) | set(MULTILINE_FIELDS)
VORBIS_FIELD_RE = re.compile(r"^[\x20-\x3c\x3e-\x7d]+$")
GAIN_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?\s*dB$", re.IGNORECASE)
PEAK_RE = re.compile(r"^\d+(?:\.\d+)?$")
REF_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?\s*LUFS$", re.IGNORECASE)
YEAR_RE = re.compile(r"^\d{4}$")
ISWC_RE = re.compile(r"^T-\d{3}\.\d{3}\.\d{3}-\d$", re.IGNORECASE)
DB_RE = re.compile(r"^(?:over|[+-]?\d+(?:\.\d+)?\s*dB)$", re.IGNORECASE)
RANGE_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?\s*dB$", re.IGNORECASE)
MUSICBRAINZ_DISCID_RE = re.compile(r"^[A-Za-z0-9._-]{28}$")


class Severity(IntEnum):
    INFORMATIONAL = 10
    LOW = 20
    MEDIUM = 30
    HIGH = 40
    CRITICAL = 50


@dataclass(frozen=True)
class Finding:
    severity: Severity
    code: str
    message: str
    path: Path | None = None
    repairable: bool = False


class TrackAuditResult:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.findings: list[Finding] = []
        self.info: dict[str, Any] = {}

    def add(self, severity: Severity, code: str, message: str, *, repairable: bool = False) -> None:
        self.findings.append(Finding(severity, code, message, self.path, repairable))

    @property
    def errors(self) -> list[str]:
        return [f.message for f in self.findings if f.severity >= Severity.HIGH]

    @property
    def warnings(self) -> list[str]:
        return [f.message for f in self.findings if Severity.LOW <= f.severity < Severity.HIGH]

    @property
    def is_clean(self) -> bool:
        return not any(f.severity >= Severity.LOW for f in self.findings)


def verify_audio_stream(path: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["flac", "-t", "-s", str(path)], capture_output=True, text=True, check=False, timeout=300
        )
    except FileNotFoundError:
        return ["flac binary not found in PATH"]
    except subprocess.TimeoutExpired:
        return ["FLAC stream verification timed out after 300 seconds"]
    if result.returncode:
        return [f"FLAC stream corrupted (flac -t returned {result.returncode}): {result.stderr.strip()}"]
    return []


def check_flac_binary_format(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        with path.open("rb") as handle:
            header = handle.read(4)
            if header.startswith(b"ID3"):
                errors.append("FLAC contains a prohibited ID3v2 header")
            elif header != b"fLaC":
                errors.append(f"FLAC stream marker is invalid: {header!r}")
            handle.seek(0, 2)
            if handle.tell() >= 128:
                handle.seek(-128, 2)
                if handle.read(3) == b"TAG":
                    errors.append("FLAC contains a prohibited ID3v1 trailer")
    except OSError as exc:
        errors.append(f"cannot inspect FLAC container: {exc}")
    return errors


def _tag_map(audio: FLAC, result: TrackAuditResult) -> dict[str, list[str]]:
    tags: dict[str, list[str]] = {}
    assert audio.tags is not None
    for raw_name, raw_values in audio.tags.items():
        if not VORBIS_FIELD_RE.fullmatch(raw_name):
            result.add(Severity.HIGH, "vorbis.invalid_field", f"Invalid Vorbis comment field name: {raw_name!r}")
        name = raw_name.upper()
        values = [str(value) for value in raw_values]
        tags.setdefault(name, []).extend(values)
        if name in ALIASES and name not in {"TOTALTRACKS", "TOTALDISCS"}:
            result.add(
                Severity.LOW, "schema.alias", f"Non-canonical alias {name}; use {ALIASES[name]}", repairable=True
            )
        for value in values:
            if not value.strip():
                result.add(Severity.MEDIUM, "value.empty", f"Empty value in {name}", repairable=True)
            if "\ufeff" in value:
                result.add(
                    Severity.HIGH, "unicode.bom", f"BOM (Byte Order Mark) detected in tag {name}", repairable=True
                )
            if "\x00" in value:
                result.add(Severity.HIGH, "unicode.invalid", f"NUL byte present in tag {name}")
            elif any(0xD800 <= ord(char) <= 0xDFFF for char in value):
                result.add(Severity.HIGH, "unicode.invalid", f"Invalid Unicode content in {name}")
            elif unicodedata.normalize("NFC", value) != value:
                result.add(Severity.LOW, "unicode.non_nfc", f"Non-NFC Unicode in {name}", repairable=True)
    return tags


def audit_flac(flac_path: Path, *, check_stream: bool = True) -> TrackAuditResult:
    result = TrackAuditResult(flac_path)
    if check_stream:
        for message in verify_audio_stream(flac_path):
            result.add(Severity.CRITICAL, "audio.corrupt", message)
    for message in check_flac_binary_format(flac_path):
        result.add(Severity.CRITICAL, "container.invalid", message)
    try:
        audio = FLAC(str(flac_path))
    except Exception as exc:
        result.add(Severity.CRITICAL, "metadata.unreadable", f"Failed to parse FLAC metadata: {exc}")
        return result
    if audio.tags is None:
        result.add(Severity.HIGH, "metadata.missing", "Missing VORBIS_COMMENT block", repairable=True)
        return result
    tags = _tag_map(audio, result)

    def values(name: str) -> list[str]:
        return tags.get(name, [])

    def first(name: str) -> str:
        return values(name)[0].strip() if values(name) else ""

    for name in SINGLE_VALUED_FIELDS:
        if len(values(name)) > 1:
            result.add(
                Severity.HIGH,
                "cardinality.single",
                f"Duplicate single-valued tag: {name} has {len(values(name))} entries",
            )
    for name, vals in tags.items():
        normalized = [unicodedata.normalize("NFC", value).casefold() for value in vals]
        if len(normalized) != len(set(normalized)):
            result.add(Severity.MEDIUM, "cardinality.duplicate", f"Duplicate values in {name}", repairable=True)
        if len(vals) > 1 and len(set(normalized)) < len(vals):
            result.add(
                Severity.MEDIUM,
                "cardinality.case_duplicate",
                f"Detected near-duplicate values differing only by case in {name}",
                repairable=True,
            )
        if name in MULTI_VALUED_FIELDS or name.startswith("PERFORMER:"):
            if any(";" in value for value in vals):
                result.add(
                    Severity.MEDIUM,
                    "cardinality.delimiter",
                    f"Detected semicolon delimiter in {name}; use repeated Vorbis comments",
                )

    # DATE is deliberately not required: precision/edition provenance matters
    # more than filling a guessed year.
    required = ("TITLE", "ARTIST", "ALBUM", "ALBUMARTIST", "TRACKNUMBER", "TRACKTOTAL", "DISCNUMBER", "DISCTOTAL")
    for name in required:
        if not first(name):
            result.add(Severity.HIGH, "core.missing", f"Missing mandatory tag: {name}")
    for canonical, alias in (("TRACKTOTAL", "TOTALTRACKS"), ("DISCTOTAL", "TOTALDISCS")):
        if first(canonical) and first(alias) and first(canonical) != first(alias):
            result.add(Severity.HIGH, "schema.alias_conflict", f"Conflicting {canonical} and {alias}")
        elif first(canonical) and first(alias):
            result.add(Severity.INFORMATIONAL, "schema.alias_compatible", f"Compatible {canonical}/{alias} aliases")

    for name in ("DATE", "ORIGINALDATE", "RECORDINGDATE"):
        if first(name) and not valid_date(first(name)):
            result.add(Severity.HIGH, "date.invalid", f"Invalid {name} format: {first(name)!r}")
    if first("DATE") and first("ORIGINALDATE") and first("ORIGINALDATE")[:4] > first("DATE")[:4]:
        result.add(Severity.HIGH, "date.order", "ORIGINALDATE is later than edition DATE")
    for name in ("YEAR", "ORIGINALYEAR"):
        if first(name) and not YEAR_RE.fullmatch(first(name)):
            result.add(Severity.HIGH, "date.year", f"Invalid {name} format: {first(name)!r}")

    parsed: dict[str, int] = {}
    for name in ("TRACKNUMBER", "TRACKTOTAL", "DISCNUMBER", "DISCTOTAL"):
        alias = {"TRACKTOTAL": "TOTALTRACKS", "DISCTOTAL": "TOTALDISCS"}.get(name, "")
        raw = first(name) or (first(alias) if alias else "")
        if "/" in raw:
            result.add(
                Severity.HIGH, "number.combined", f"{name} contains slash ({raw!r}); totals belong in a separate field"
            )
        if raw:
            try:
                parsed[name] = int(raw)
                if parsed[name] < 1:
                    result.add(Severity.HIGH, "number.nonpositive", f"{name} must be >= 1")
            except ValueError:
                result.add(Severity.HIGH, "number.invalid", f"{name} is not a positive integer: {raw!r}")
    if parsed.get("TRACKNUMBER", 0) > parsed.get("TRACKTOTAL", 10**9):
        result.add(
            Severity.HIGH,
            "number.track_range",
            f"TRACKNUMBER ({parsed['TRACKNUMBER']}) exceeds TRACKTOTAL ({parsed['TRACKTOTAL']})",
        )
    if parsed.get("DISCNUMBER", 0) > parsed.get("DISCTOTAL", 10**9):
        result.add(
            Severity.HIGH,
            "number.disc_range",
            f"DISCNUMBER ({parsed['DISCNUMBER']}) exceeds DISCTOTAL ({parsed['DISCTOTAL']})",
        )

    uuid_fields = {
        name for name in STABLE_ID_FIELDS if name.startswith("MUSICBRAINZ_") and name != "MUSICBRAINZ_DISCID"
    } | {"ACOUSTID_ID"}
    for name in uuid_fields:
        for value in values(name):
            if not UUID_RE.fullmatch(value):
                result.add(Severity.HIGH, "identifier.uuid", f"Invalid UUID in {name}: {value!r}")
    for isrc in values("ISRC"):
        if not ISRC_RE.fullmatch(re.sub(r"[-\s]", "", isrc).upper()):
            result.add(Severity.MEDIUM, "identifier.isrc", f"Suspicious ISRC format: {isrc!r}")
    if first("MUSICBRAINZ_DISCID") and not MUSICBRAINZ_DISCID_RE.fullmatch(first("MUSICBRAINZ_DISCID")):
        result.add(Severity.HIGH, "identifier.discid", f"Invalid MusicBrainz Disc ID: {first('MUSICBRAINZ_DISCID')!r}")
    for iswc in values("ISWC"):
        if not ISWC_RE.fullmatch(iswc):
            result.add(Severity.MEDIUM, "identifier.iswc", f"Suspicious ISWC format: {iswc!r}")
    if first("MUSICBRAINZ_RECORDINGID"):
        result.add(
            Severity.LOW,
            "schema.mb_recording_legacy",
            "MUSICBRAINZ_RECORDINGID is non-canonical; use MUSICBRAINZ_TRACKID",
            repairable=True,
        )
        if first("MUSICBRAINZ_TRACKID") and first("MUSICBRAINZ_RECORDINGID") != first("MUSICBRAINZ_TRACKID"):
            # Previous versions used TRACKID for a release-track UUID.  This
            # is a migration review, not proof that two recording identities
            # conflict.
            result.add(
                Severity.MEDIUM,
                "identifier.recording_legacy_ambiguous",
                "Legacy MusicBrainz TRACKID semantics require migration review",
                repairable=True,
            )

    for name in ("REPLAYGAIN_TRACK_GAIN", "REPLAYGAIN_ALBUM_GAIN"):
        if first(name) and not GAIN_RE.fullmatch(first(name)):
            result.add(Severity.HIGH, "loudness.gain", f"Invalid {name} format: {first(name)!r}")
    for name in ("REPLAYGAIN_TRACK_PEAK", "REPLAYGAIN_ALBUM_PEAK"):
        if first(name) and not PEAK_RE.fullmatch(first(name)):
            result.add(Severity.HIGH, "loudness.peak", f"Invalid {name} format: {first(name)!r}")
    for name in ("REPLAYGAIN_TRACK_RANGE", "REPLAYGAIN_ALBUM_RANGE"):
        if first(name) and not RANGE_RE.fullmatch(first(name)):
            result.add(Severity.HIGH, "loudness.range", f"Invalid {name} format: {first(name)!r}")
    if first("REPLAYGAIN_REFERENCE_LOUDNESS") and not REF_RE.fullmatch(first("REPLAYGAIN_REFERENCE_LOUDNESS")):
        result.add(Severity.HIGH, "loudness.reference", "Invalid REPLAYGAIN_REFERENCE_LOUDNESS")
    for name in tags:
        if name.startswith("R128_"):
            result.add(
                Severity.INFORMATIONAL,
                "loudness.r128_preserved",
                f"{name} preserved; pipeline does not manufacture R128 FLAC tags",
            )

    synced, plain = first("SYNCEDLYRICS") or first("SYNCELYRICS"), first("UNSYNCEDLYRICS")
    if synced:
        valid, reasons = validate_lrc(synced, duration=float(audio.info.length or 0))
        if not valid:
            result.add(Severity.HIGH, "lyrics.sync_invalid", "Malformed synchronized lyrics: " + "; ".join(reasons))
    if plain and re.search(r"^\[\d{1,3}:[0-5]\d", plain, re.MULTILINE):
        result.add(Severity.MEDIUM, "lyrics.plain_timestamped", "UNSYNCEDLYRICS tag contains synchronized timestamps")
    if (
        plain
        and synced
        and re.sub(r"\s+", " ", plain).strip().casefold()
        != re.sub(r"\s+", " ", lrc_to_plain(synced)).strip().casefold()
    ):
        result.add(Severity.MEDIUM, "lyrics.conflict", "Synced and unsynced lyrics contain materially different text")
    if first("LYRICS") and plain and first("LYRICS").strip() == plain.strip():
        result.add(
            Severity.INFORMATIONAL, "lyrics.compat_duplicate", "LYRICS duplicates UNSYNCEDLYRICS for compatibility"
        )
    if first("SYNCEDLYRICS") and first("SYNCELYRICS") and first("SYNCEDLYRICS") != first("SYNCELYRICS"):
        result.add(Severity.HIGH, "lyrics.sync_alias_conflict", "SYNCEDLYRICS and SYNCELYRICS differ")

    front = [picture for picture in audio.pictures if picture.type == 3]
    if not front:
        result.add(Severity.LOW, "artwork.front_missing", "No front cover artwork embedded")
    for picture in front:
        if not picture.data or picture.width <= 0 or picture.height <= 0:
            result.add(
                Severity.HIGH,
                "artwork.invalid",
                f"Invalid METADATA_BLOCK_PICTURE dimensions: {picture.width}x{picture.height}",
            )
        elif min(picture.width, picture.height) < 600:
            result.add(
                Severity.LOW,
                "artwork.small",
                f"Sub-optimal cover resolution: {picture.width}x{picture.height} (< 600px)",
            )

    if first("SPARSCODE") and first("SPARSCODE") not in {"AAD", "ADD", "DDD", "DAD"}:
        result.add(Severity.LOW, "engineering.spars", f"Unusual SPARSCODE: {first('SPARSCODE')!r}")
    if first("CATALOGNUMBER") and re.fullmatch(r"(?i:none|\[none\]|n/?a|\?)", first("CATALOGNUMBER")):
        result.add(Severity.HIGH, "release.catalog", f"Invalid CATALOGNUMBER value: {first('CATALOGNUMBER')!r}")
    for name in ("DYNAMIC_RANGE", "ALBUM_DYNAMIC_RANGE", "ALBUM_MIN_DR", "ALBUM_MAX_DR"):
        if not first(name):
            continue
        try:
            dr_value = int(first(name))
        except ValueError:
            result.add(Severity.HIGH, "engineering.dr", f"{name} is not an integer: {first(name)!r}")
            continue
        if not 0 <= dr_value <= 30:
            result.add(Severity.HIGH, "engineering.dr", f"{name} out of plausible range [0, 30]: {dr_value}")
    for name in ("DR_PEAK", "DR_RMS"):
        if first(name) and not DB_RE.fullmatch(first(name)):
            result.add(Severity.HIGH, "engineering.dr_format", f"Invalid {name} format: {first(name)!r}")
    if first("EAC_PEAK") and not re.fullmatch(r"\d+(?:\.\d+)?\s*%", first("EAC_PEAK")):
        result.add(Severity.HIGH, "rip.eac_peak", f"Invalid EAC_PEAK format: {first('EAC_PEAK')!r}")
    for value in values("DISCOGS_ARTIST_ID"):
        if not value.isdigit():
            result.add(Severity.HIGH, "identifier.discogs_artist", f"Invalid DISCOGS_ARTIST_ID format: {value!r}")
    if first("DISCSUBTITLE"):
        subtitle = first("DISCSUBTITLE")
        if re.fullmatch(r"(?i)(?:cd|disc|disk)\s*\d+", subtitle):
            result.add(Severity.HIGH, "release.discsubtitle", f"Trivial DISCSUBTITLE: {subtitle!r}")
        elif re.match(r"(?i)^(?:cd|disc|disk)\s*\d+\s*[:.-]", subtitle):
            result.add(
                Severity.LOW,
                "release.discsubtitle_prefix",
                f"Unclean DISCSUBTITLE prefix: {subtitle!r}",
                repairable=True,
            )
    for junk in ("ITUNESADVISORY", "ITUNESNORM", "WM/YEAR", "COVERART", "RATING"):
        if values(junk):
            result.add(Severity.LOW, "schema.legacy", f"Junk/legacy tag detected: {junk}")

    result.info = {
        "title": first("TITLE"),
        "album": first("ALBUM"),
        "albumartist": first("ALBUMARTIST"),
        "track": f"{parsed.get('TRACKNUMBER', -1)}/{parsed.get('TRACKTOTAL', -1)}",
        "disc": f"{parsed.get('DISCNUMBER', -1)}/{parsed.get('DISCTOTAL', -1)}",
        "mb_recording": first("MUSICBRAINZ_TRACKID"),
        "mb_release": first("MUSICBRAINZ_ALBUMID"),
        "isrcs": tuple(values("ISRC")),
        "duration": float(audio.info.length or 0),
        "tags": tags,
    }
    return result


def audit_library(results_or_paths: Iterable[TrackAuditResult | Path]) -> list[Finding]:
    results = [item if isinstance(item, TrackAuditResult) else audit_flac(item) for item in results_or_paths]
    findings: list[Finding] = []
    groups: dict[tuple[str, str, str], list[TrackAuditResult]] = {}
    for result in results:
        parent = str(result.path.parent.resolve()) if result.path else ""
        release_id = (result.info.get("tags", {}).get("MUSICBRAINZ_ALBUMID") or [""])[0]
        # A stable release ID is the only safe cross-directory grouping key for
        # multi-disc sets. Without it retain directory isolation.
        group_key = release_id or parent
        groups.setdefault((group_key, result.info.get("albumartist", ""), result.info.get("album", "")), []).append(
            result
        )

    for tracks in groups.values():
        positions: dict[tuple[str, str], Path] = {}
        for result in tracks:
            tags = result.info.get("tags", {})
            position = ((tags.get("DISCNUMBER") or [""])[0], (tags.get("TRACKNUMBER") or [""])[0])
            if position in positions:
                findings.append(
                    Finding(
                        Severity.CRITICAL,
                        "album.duplicate_position",
                        f"Duplicate disc/track position {position[0]}-{position[1]}; also used by {positions[position].name}",
                        result.path,
                    )
                )
            positions[position] = result.path
        for field in (
            "ALBUM",
            "ALBUMARTIST",
            "DATE",
            "DISCTOTAL",
            "MUSICBRAINZ_ALBUMID",
            "DISCOGS_RELEASE_ID",
            "BARCODE",
            "CATALOGNUMBER",
            "LABEL",
            "COUNTRY",
        ):
            observed = {
                tuple(result.info.get("tags", {}).get(field) or [])
                for result in tracks
                if result.info.get("tags", {}).get(field)
            }
            if len(observed) > 1:
                findings.append(
                    Finding(
                        Severity.HIGH,
                        "album.inconsistent",
                        f"{field} differs within one release: {sorted(observed)!r}",
                        tracks[0].path,
                    )
                )
        by_disc: dict[str, list[TrackAuditResult]] = {}
        for result in tracks:
            tags = result.info.get("tags", {})
            by_disc.setdefault((tags.get("DISCNUMBER") or [""])[0], []).append(result)
        for disc, disc_tracks in by_disc.items():
            totals = {(r.info.get("tags", {}).get("TRACKTOTAL") or [""])[0] for r in disc_tracks}
            if len(totals) > 1:
                findings.append(
                    Finding(
                        Severity.HIGH,
                        "album.tracktotal",
                        f"Disc {disc} has conflicting TRACKTOTAL values",
                        disc_tracks[0].path,
                        True,
                    )
                )
            elif totals and next(iter(totals)).isdigit() and int(next(iter(totals))) != len(disc_tracks):
                findings.append(
                    Finding(
                        Severity.INFORMATIONAL,
                        "collection.partial_medium",
                        f"Disc {disc} has {len(disc_tracks)} of declared {next(iter(totals))} tracks; completeness unknown",
                        disc_tracks[0].path,
                    )
                )
            track_positions = sorted(
                int((r.info.get("tags", {}).get("TRACKNUMBER") or ["0"])[0])
                for r in disc_tracks
                if (r.info.get("tags", {}).get("TRACKNUMBER") or ["0"])[0].isdigit()
            )
            if track_positions and track_positions != list(range(1, max(track_positions) + 1)):
                findings.append(
                    Finding(
                        Severity.INFORMATIONAL,
                        "collection.partial_positions",
                        f"Disc {disc} has non-contiguous positions: {track_positions}; may be a curated partial collection",
                        disc_tracks[0].path,
                    )
                )
            genre_sets = {tuple(r.info.get("tags", {}).get("GENRE") or []) for r in disc_tracks}
            if len(genre_sets) > 1:
                findings.append(
                    Finding(Severity.MEDIUM, "album.genre", f"GENRE differs across disc {disc}", disc_tracks[0].path)
                )

    isrc_index: dict[str, TrackAuditResult] = {}
    recording_index: dict[str, TrackAuditResult] = {}
    release_track_index: dict[str, TrackAuditResult] = {}
    for result in results:
        for isrc in result.info.get("isrcs", ()):
            isrc = re.sub(r"[-\s]", "", isrc).upper()
            previous = isrc_index.get(isrc)
            if (
                previous
                and previous.info.get("title", "").casefold() != result.info.get("title", "").casefold()
                and abs(previous.info.get("duration", 0) - result.info.get("duration", 0)) > 5
            ):
                findings.append(
                    Finding(
                        Severity.HIGH,
                        "library.isrc_conflict",
                        f"ISRC {isrc} is assigned to incompatible recordings",
                        result.path,
                    )
                )
            isrc_index[isrc] = result
        recording = result.info.get("mb_recording")
        previous = recording_index.get(recording) if recording else None
        if previous and abs(previous.info.get("duration", 0) - result.info.get("duration", 0)) > 15:
            findings.append(
                Finding(
                    Severity.HIGH,
                    "library.recording_conflict",
                    f"MusicBrainz recording {recording} has incompatible durations",
                    result.path,
                )
            )
        if recording:
            recording_index[recording] = result
        release_track = (result.info.get("tags", {}).get("MUSICBRAINZ_RELEASETRACKID") or [""])[0]
        if release_track:
            previous_track = release_track_index.get(release_track)
            if previous_track and previous_track.path != result.path:
                findings.append(
                    Finding(
                        Severity.CRITICAL,
                        "library.release_track_duplicate",
                        f"MusicBrainz release-track {release_track} is assigned to multiple files",
                        result.path,
                    )
                )
            release_track_index[release_track] = result
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only FLAC stream and metadata quality audit")
    parser.add_argument("root", nargs="?", default=".", help="FLAC file or library directory")
    parser.add_argument(
        "--no-stream-check",
        action="store_true",
        help="Skip full flac -t decoding and audit container metadata only",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    files = iter_library_flacs(root) if root.is_dir() else [root]
    if not files:
        print(f"ERROR: no FLAC files found in {root}", file=sys.stderr)
        return 2
    results = [audit_flac(path, check_stream=not args.no_stream_check) for path in files]
    findings = [finding for result in results for finding in result.findings] + audit_library(results)
    if root.is_dir():
        findings.extend(
            Finding(
                Severity.CRITICAL,
                "transaction.abandoned",
                "Abandoned metadata transaction; use album_transaction.py for a journaled tree, or inspect legacy recovery files manually",
                path,
            )
            for path in abandoned_transactions(root)
        )
    for finding in sorted(findings, key=lambda item: (-int(item.severity), str(item.path or ""), item.code)):
        repair = "auto-repairable" if finding.repairable else "review/manual"
        print(f"[{finding.severity.name}] {finding.path or root}: {finding.code}: {finding.message} ({repair})")
    counts = {severity.name: sum(1 for finding in findings if finding.severity is severity) for severity in Severity}
    print("SUMMARY " + " ".join(f"{name}={count}" for name, count in counts.items()))
    return 1 if any(finding.severity >= Severity.HIGH for finding in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
