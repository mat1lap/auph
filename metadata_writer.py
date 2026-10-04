#!/usr/bin/env python3
"""Canonical, idempotent FLAC Vorbis-comment writer and strict invariant verifier.

Performance-optimized: uses mutagen for in-process tag I/O instead of
spawning multiple metaflac subprocesses per file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from mutagen.flac import FLAC, Picture

from lyrics_pipeline import display_lyrics
from lyrics_quality import TIMESTAMP_RE, conservative_text, lrc_to_plain, validate_lrc
from metadata_policy import Confidence, DecisionAction, FieldDecision, decide_field
from metadata_schema import (
    FIELD_SPECS,
    MULTILINE_FIELDS,
    SINGLE_VALUED_FIELDS,
    STABLE_ID_FIELDS,
    canonical_field_name,
    normalize_value,
    valid_date,
)
from pipeline_config import load_config

FIELD_RE = re.compile(r"^[\x20-\x3c\x3e-\x7d]+$")
_PROVENANCE_LOCK = threading.Lock()

try:
    from verify_all_flacs import SINGLE_VALUED_TAGS
except ImportError:
    SINGLE_VALUED_TAGS = {
        "TITLE",
        "TITLESORT",
        "ALBUM",
        "ALBUMSORT",
        "ALBUMARTIST",
        "ALBUMARTISTSORT",
        "DISCSUBTITLE",
        "DATE",
        "YEAR",
        "RELEASEDATE",
        "ORIGINALDATE",
        "ORIGINALYEAR",
        "TRACKNUMBER",
        "TRACKTOTAL",
        "TOTALTRACKS",
        "DISCNUMBER",
        "DISCTOTAL",
        "TOTALDISCS",
        "COMPILATION",
        "VERSION",
        "MEDIA",
        "SOURCE",
        "DYNAMIC_RANGE",
        "ALBUM_DYNAMIC_RANGE",
        "DR",
        "ALBUM_DR",
        "ALBUM_MIN_DR",
        "ALBUM_MAX_DR",
        "DR_LOUDNESS_WAR_ID",
        "DR_LOUDNESS_WAR_URL",
        "REPLAYGAIN_TRACK_GAIN",
        "REPLAYGAIN_TRACK_PEAK",
        "REPLAYGAIN_TRACK_RANGE",
        "REPLAYGAIN_ALBUM_GAIN",
        "REPLAYGAIN_ALBUM_PEAK",
        "REPLAYGAIN_ALBUM_RANGE",
        "REPLAYGAIN_REFERENCE_LOUDNESS",
        "MUSICBRAINZ_RECORDINGID",
        "MUSICBRAINZ_RELEASEGROUPID",
        "MUSICBRAINZ_ALBUMID",
        "MUSICBRAINZ_TRACKID",
        "MUSICBRAINZ_ALBUMARTISTID",
        "DISCOGS_RELEASE_URL",
        "DISCOGS_RELEASE_ID",
        "DISCOGS_MASTER_ID",
        "BARCODE",
        "ACCURATERIPRESULT",
        "ACCURATERIPCRC",
        "CTDB_TOCID",
        "CTDB_STATUS",
        "LANGUAGE",
        "COPYRIGHT",
        "SPARSCODE",
        "DR_PEAK",
        "DR_RMS",
        "DR_SOURCE",
        "RECORDINGDATE",
        "HDCD",
        "HDCD_PEAK_EXTEND",
        "HDCD_TRANSIENT_FILTER",
        "ACOUSTID_ID",
        "MUSICBRAINZ_DISCID",
        "MUSICBRAINZ_WORKID",
        "ORIGINALRELEASEDATE",
        "MUSICBRAINZ_ORIGINALRELEASEDATE",
        "PRE_EMPHASIS",
        "DE_EMPHASIS_APPLIED",
        "ACOUSTID_FINGERPRINT",
        "EAC_PEAK",
        "EAC_PEAKS",
        "INSTRUMENTAL",
        "BPM",
        "TEMPO",
        "KEY",
        "INITIALKEY",
        "CAMELOT_KEY",
    }


def die(msg: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    return 1


def clean_manifest_value(value: str, field: str = "") -> str:
    return normalize_value(field, value)


def load_tags(path: Path) -> list[tuple[str, str]]:
    raw_rows: list[list[str]] = []
    # A replacement character can silently corrupt lyrics in a manifest. Fail
    # on invalid UTF-8 so the caller can repair the source instead.
    lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    for n, line in enumerate(lines, 1):
        active_multiline = bool(raw_rows and raw_rows[-1][0] in MULTILINE_FIELDS)
        if not line:
            if active_multiline:
                raw_rows[-1][1] += "\n"
            continue
        if line.startswith("#"):
            if active_multiline:
                raw_rows[-1][1] += "\n" + line
            continue
        if "=" in line:
            f, v = line.split("=", 1)
            f = canonical_field_name(f)
            # A lyrics line can itself contain "=". Within a multiline lyric
            # block, only a known Vorbis field starts the next tag; otherwise
            # keep the line as lyric text. Custom fields remain accepted when
            # they are not ambiguous with an active lyrics payload.
            is_known_field = f in FIELD_SPECS or f.startswith("PERFORMER:")
            if active_multiline and not is_known_field:
                raw_rows[-1][1] += "\n" + line
                continue
            if not f or not FIELD_RE.fullmatch(f):
                raise ValueError(f"invalid Vorbis field name line {n}: {f!r}")
            raw_rows.append([f, v])
        else:
            if raw_rows and raw_rows[-1][0] in MULTILINE_FIELDS:
                raw_rows[-1][1] += "\n" + line
            else:
                raise ValueError(f"invalid manifest line {n}: missing '='")

    rows: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for field, raw_val in raw_rows:
        val = clean_manifest_value(raw_val, field)
        if not val:
            continue
        key = (field.casefold(), val.casefold())
        if key in seen:
            continue
        seen.add(key)
        rows.append((field, val))
    return rows


def canonical_verify(flac_path: Path, *, require_core: bool = True) -> None:
    # Check binary header for prohibited ID3 tags
    try:
        with open(flac_path, "rb") as fp:
            h = fp.read(4)
            if h.startswith(b"ID3"):
                raise RuntimeError("FLAC file contains prohibited ID3v2 header")
            elif h != b"fLaC":
                raise RuntimeError(f"missing 'fLaC' stream marker: {h!r}")
            fp.seek(0, 2)
            if fp.tell() >= 128:
                fp.seek(-128, 2)
                if fp.read(3) == b"TAG":
                    raise RuntimeError("FLAC file contains prohibited ID3v1 trailer")
    except RuntimeError:
        raise
    except Exception:
        pass

    audio = FLAC(str(flac_path))
    comments = audio.tags
    if comments is None:
        raise RuntimeError("missing VORBIS_COMMENT block")

    seen: set[tuple[str, str]] = set()
    for field, values in comments.items():
        for value in values:
            if "\x00" in value:
                raise RuntimeError(f"NUL byte present in {field}")
            key = (field.casefold(), str(value).casefold())
            if key in seen:
                raise RuntimeError(f"duplicate Vorbis comment: {field}={value}")
            seen.add(key)

    by_field = {str(field).casefold(): list(values) for field, values in comments.items()}
    for canonical, alias in (("tracktotal", "totaltracks"), ("disctotal", "totaldiscs")):
        canonical_values = by_field.get(canonical, [])
        alias_values = by_field.get(alias, [])
        if canonical_values and alias_values and canonical_values[0] != alias_values[0]:
            raise RuntimeError(f"conflicting compatibility aliases: {canonical}/{alias}")
    if require_core:
        # Core scalar invariant check
        # DATE is deliberately optional: an unknown edition date is better
        # than a guessed original-release date.
        for field in ("TITLE", "ALBUM", "TRACKNUMBER", "TRACKTOTAL", "DISCNUMBER", "DISCTOTAL"):
            vals = by_field.get(field.casefold(), [])
            if len(vals) != 1 or not str(vals[0]).strip():
                raise RuntimeError(f"core scalar tag invariant failed: {field} (count={len(vals)})")

        # ALBUMARTIST must be present and non-empty (supports collaborations)
        albumartist_vals = by_field.get("albumartist", [])
        if not albumartist_vals or not any(str(v).strip() for v in albumartist_vals):
            raise RuntimeError(
                f"core scalar tag invariant failed: ALBUMARTIST missing or empty (count={len(albumartist_vals)})"
            )

    # Numeric range invariants
    try:
        if "tracknumber" not in by_field or "tracktotal" not in by_field:
            raise KeyError
        t_num = int(by_field["tracknumber"][0])
        t_tot = int(by_field["tracktotal"][0])
        if t_num < 1 or t_tot < 1 or t_num > t_tot:
            raise RuntimeError(f"track numbering invariant failed: {t_num}/{t_tot}")
    except KeyError:
        if require_core:
            raise RuntimeError("missing track numbering")
    except ValueError as exc:
        raise RuntimeError(f"non-integer track numbers: {exc}") from exc

    try:
        if "discnumber" not in by_field or "disctotal" not in by_field:
            raise KeyError
        d_num = int(by_field["discnumber"][0])
        d_tot = int(by_field["disctotal"][0])
        if d_num < 1 or d_tot < 1 or d_num > d_tot:
            raise RuntimeError(f"disc numbering invariant failed: {d_num}/{d_tot}")
    except KeyError:
        if require_core:
            raise RuntimeError("missing disc numbering")
    except ValueError as exc:
        raise RuntimeError(f"non-integer disc numbers: {exc}") from exc

    # ARTIST must be present and non-empty (supports multiple artists in Vorbis comments)
    artist_vals = by_field.get("artist", [])
    if require_core and (not artist_vals or not any(str(v).strip() for v in artist_vals)):
        raise RuntimeError(f"core artist tag invariant failed: ARTIST missing or empty (count={len(artist_vals)})")

    # Picture invariant: if front cover picture is embedded, it must have width and height > 0
    front_pics = [p for p in audio.pictures if p.type == 3]
    if front_pics:
        front = front_pics[0]
        if front.width <= 0 or front.height <= 0:
            raise RuntimeError(f"invalid embedded picture dimensions: {front.width}x{front.height}")


@dataclass(frozen=True)
class WriteReport:
    transaction_id: str
    decisions: tuple[FieldDecision, ...]
    provenance_path: str


def _scope_identity_agreement(existing: dict[str, list[str]], incoming: dict[str, list[str]]) -> dict:
    """Compare stable identifiers only within the entity they identify.

    A recording ID agreement must not make a release date/catalogue editable;
    conversely an old release ID must not suppress a proven recording repair.
    ``None`` means no comparable identifier is available for that scope.
    """
    result: dict = {}
    for field in STABLE_ID_FIELDS:
        spec = FIELD_SPECS.get(field)
        if not spec:
            continue
        if field not in existing or field not in incoming:
            continue
        equal = {v.casefold() for v in existing[field]} == {v.casefold() for v in incoming[field]}
        prior = result.get(spec.scope)
        result[spec.scope] = equal if prior is None else bool(prior and equal)
    return result


# Backwards-compatible private name for integrations that imported it.
def _stable_identity_agreement(existing: dict[str, list[str]], incoming: dict[str, list[str]]) -> bool | None:
    agreements = _scope_identity_agreement(existing, incoming)
    return None if not agreements else all(agreements.values())


def _write_tags_in_place(
    flac_path: Path,
    tags: list[tuple[str, str]],
    lyrics_file: str | Path | None = None,
    synced_lyrics_file: str | Path | None = None,
    enhanced_lyrics_file: str | Path | None = None,
    ttml_file: str | Path | None = None,
    lyrics_translation_file: str | Path | None = None,
    lyrics_romaji_file: str | Path | None = None,
    picture_file: str | Path | None = None,
    max_picture_dim: int | None = None,
    max_picture_bytes: int = 16 * 1024 * 1024,
    *,
    replace_front_picture: bool = False,
    incoming_confidence: Confidence = Confidence.VERIFIED,
    release_confidence: Confidence = Confidence.HIGH,
    existing_confidence: Confidence = Confidence.HIGH,
    force: bool = False,
    archive_base: Path | None = None,
    dry_run: bool = False,
    lyric_archives: dict[Path, str] | None = None,
) -> tuple[FieldDecision, ...]:
    """Apply a policy-controlled update to a disposable FLAC copy."""
    audio = FLAC(str(flac_path))
    if audio.tags is None:
        audio.add_tags()

    existing: dict[str, list[str]] = {}
    raw_keys: dict[str, list[str]] = {}
    assert audio.tags is not None
    for raw_name, values in audio.tags.items():
        name = canonical_field_name(raw_name)
        raw_keys.setdefault(name, []).append(raw_name)
        existing.setdefault(name, []).extend(normalize_value(name, v) for v in values)

    # Set manifest tags (group by field name for multi-value support, with deduplication and single-valued safety)
    tag_dict: dict[str, list[str]] = {}
    for field, value in tags:
        f_up = canonical_field_name(field)

        cleaned_val = clean_manifest_value(value, field)
        if not cleaned_val:
            continue
        if f_up in {"DATE", "ORIGINALDATE", "RECORDINGDATE", "RELEASEDATE"} and not valid_date(cleaned_val):
            raise ValueError(f"invalid incoming {f_up}: {cleaned_val!r}")
        if f_up == "RELEASETYPE":
            cleaned_val = cleaned_val.lower()

        if f_up in SINGLE_VALUED_FIELDS or f_up in SINGLE_VALUED_TAGS:
            previous = tag_dict.get(f_up)
            if previous and previous[0].casefold() != cleaned_val.casefold():
                raise ValueError(f"conflicting incoming single-valued tag {f_up}: {previous[0]!r} vs {cleaned_val!r}")
            tag_dict[f_up] = [cleaned_val]
        else:
            current_vals = tag_dict.setdefault(f_up, [])
            if not any(v.casefold() == cleaned_val.casefold() for v in current_vals):
                current_vals.append(cleaned_val)

    # One-time migration from this project's former MusicBrainz mapping.  It
    # is performed only when the current provider result independently supplies
    # both canonical entities, so an arbitrary UUID is never relabelled by
    # shape alone.
    incoming_recording = (tag_dict.get("MUSICBRAINZ_TRACKID") or [""])[0]
    incoming_release_track = (tag_dict.get("MUSICBRAINZ_RELEASETRACKID") or [""])[0]
    legacy_recording = (existing.get("MUSICBRAINZ_RECORDINGID") or [""])[0]
    old_track = (existing.get("MUSICBRAINZ_TRACKID") or [""])[0]
    identity_authorized = incoming_confidence >= Confidence.HIGH and release_confidence >= Confidence.HIGH
    if (
        identity_authorized
        and legacy_recording
        and incoming_recording
        and legacy_recording.casefold() == incoming_recording.casefold()
    ):
        for raw_name in raw_keys.get("MUSICBRAINZ_RECORDINGID", []):
            if raw_name in audio.tags:
                del audio.tags[raw_name]
        existing.pop("MUSICBRAINZ_RECORDINGID", None)
        if incoming_release_track and old_track and old_track.casefold() == incoming_release_track.casefold():
            for raw_name in raw_keys.get("MUSICBRAINZ_TRACKID", []):
                if raw_name in audio.tags:
                    del audio.tags[raw_name]
            existing.pop("MUSICBRAINZ_TRACKID", None)

    scope_agreements = _scope_identity_agreement(existing, tag_dict)
    decisions: list[FieldDecision] = []
    for field, values in tag_dict.items():
        if field == "METADATA_PROVENANCE_ID":
            decision = FieldDecision(
                field,
                DecisionAction.REPLACE if existing.get(field) else DecisionAction.ADD,
                tuple(values[:1]),
                "transaction linkage",
            )
            decisions.append(decision)
            audio[field] = list(decision.values)
            continue
        decision = decide_field(
            field,
            existing.get(field, []),
            values,
            incoming_confidence=incoming_confidence,
            # Existing tags are preservation inputs.  Unless a future
            # provenance index supplies field evidence, classify them as
            # unknown rather than silently treating legacy data as verified.
            existing_confidence=existing_confidence,
            release_confidence=release_confidence,
            stable_ids_agree=None,
            scope_agreement=scope_agreements,
            force=force,
        )
        decisions.append(decision)
        alias_keys = [raw for raw in raw_keys.get(field, []) if raw.casefold() != field.casefold()]
        # A agreeing historical alias is safe to migrate, but a disagreement
        # remains visible for review rather than being silently discarded.
        if decision.action is DecisionAction.KEEP and alias_keys:
            alias_values = [normalize_value(field, value) for raw in alias_keys for value in audio.tags.get(raw, [])]
            if alias_values and all(
                any(normalize_value(field, value).casefold() == incoming.casefold() for incoming in values)
                for value in alias_values
            ):
                for raw_name in alias_keys:
                    if raw_name in audio.tags:
                        del audio.tags[raw_name]
                audio[field] = list(values)
                decisions[-1] = FieldDecision(field, DecisionAction.REPLACE, tuple(values), "canonical alias migration")
                continue
        if decision.action in {DecisionAction.ADD, DecisionAction.REPLACE, DecisionAction.MERGE}:
            for raw_name in raw_keys.get(field, []):
                if raw_name.casefold() != field.casefold() and raw_name in audio.tags:
                    del audio.tags[raw_name]
            audio[field] = list(decision.values)

    # Lyrics tags (read file content into tag value)
    has_synced = bool(
        synced_lyrics_file and Path(synced_lyrics_file).is_file() and Path(synced_lyrics_file).stat().st_size > 0
    )
    has_plain = bool(lyrics_file and Path(lyrics_file).is_file() and Path(lyrics_file).stat().st_size > 0)

    def _read_file(path: str | None) -> str:
        if not path:
            return ""
        p = Path(path)
        if not p.is_file() or p.stat().st_size == 0:
            return ""
        return conservative_text(p.read_text(encoding="utf-8", errors="strict"))

    # Clean plain lyrics resolution (DAP / hardware player standard: no timestamps)
    plain_text: str = ""
    if has_plain:
        raw_plain = _read_file(lyrics_file)
        plain_text = lrc_to_plain(raw_plain) if raw_plain and TIMESTAMP_RE.search(raw_plain) else raw_plain
    if not plain_text and has_synced:
        raw_synced = _read_file(synced_lyrics_file)
        plain_text = lrc_to_plain(raw_synced) if raw_synced else ""

    # Apply the requested playback-field policy to historic LYRICS values too:
    # section labels remain in the archival sidecars and exact original tag
    # payload is preserved in a content-addressed JSON sidecar before cleanup.
    existing_lyrics = list(audio.tags.get("LYRICS", []))
    if existing_lyrics:
        cleaned_values = [
            display_lyrics(
                value,
                synced=validate_lrc(value, duration=float(audio.info.length or 0))[0],
            )
            for value in existing_lyrics
        ]
        if cleaned_values != existing_lyrics:
            archive_payload = json.dumps(existing_lyrics, ensure_ascii=False, indent=2) + "\n"
            archive_digest = hashlib.sha256(archive_payload.encode("utf-8")).hexdigest()[:16]
            archive_target = archive_base or flac_path
            archive_path = archive_target.with_name(f"{archive_target.stem}.lyrics-tag-archive.{archive_digest}.json")
            if lyric_archives is None:
                raise RuntimeError("lyric cleanup requires transactional archival storage")
            lyric_archives[archive_path] = archive_payload
            nonempty_cleaned = [value for value in cleaned_values if value]
            if nonempty_cleaned:
                audio["LYRICS"] = nonempty_cleaned
            elif "LYRICS" in audio.tags:
                del audio.tags["LYRICS"]
            decisions.append(
                FieldDecision(
                    "LYRICS",
                    DecisionAction.REPLACE,
                    tuple(value for value in cleaned_values if value),
                    f"removed section labels; original values archived in {archive_path.name}",
                )
            )

    synced_text: str = ""
    valid_synced_text: str = ""
    if has_synced:
        synced_text = _read_file(synced_lyrics_file)
        if synced_text:
            valid, _ = validate_lrc(synced_text, duration=float(audio.info.length or 0))
            if valid:
                valid_synced_text = synced_text

    # 1. SYNCEDLYRICS: standard line-synchronized LRC text
    if valid_synced_text:
        if not audio.tags.get("SYNCEDLYRICS"):
            audio["SYNCEDLYRICS"] = [valid_synced_text]
            decisions.append(
                FieldDecision(
                    "SYNCEDLYRICS", DecisionAction.ADD, (valid_synced_text,), "validated synchronized lyrics sidecar"
                )
            )

    # 2. UNSYNCEDLYRICS: strictly clean plain text without timestamps
    if plain_text and not audio.tags.get("UNSYNCEDLYRICS"):
        audio["UNSYNCEDLYRICS"] = [plain_text]
        decisions.append(
            FieldDecision("UNSYNCEDLYRICS", DecisionAction.ADD, (plain_text,), "preserved plain lyrics sidecar")
        )

    # User policy: synchronized lyrics are exposed to Poweramp, Simphonium and
    # MusicBee through LYRICS plus the common synced extensions; plain text has
    # its own UNSYNCEDLYRICS field.
    # LYRICS uses LRC when a validated synchronized source exists. Section
    # labels remain in the archival .lrc/SYNCEDLYRICS representation and are
    # removed only from this playback-facing compatibility value by policy.
    lyrics_display = (
        display_lyrics(valid_synced_text, synced=True)
        if valid_synced_text and load_config().lyrics.embed_synced_in_lyrics
        else display_lyrics(plain_text, synced=False)
    )
    if lyrics_display and not audio.tags.get("LYRICS"):
        audio["LYRICS"] = [lyrics_display]
        decisions.append(
            FieldDecision("LYRICS", DecisionAction.ADD, (lyrics_display,), "configured playback lyrics representation")
        )
    if valid_synced_text and load_config().lyrics.write_synced_compat_alias and not audio.tags.get("SYNCELYRICS"):
        audio["SYNCELYRICS"] = [valid_synced_text]
        decisions.append(
            FieldDecision("SYNCELYRICS", DecisionAction.ADD, (valid_synced_text,), "synced-lyrics compatibility alias")
        )

    # Enhanced LRC/TTML remain in their sidecars. The selected Vorbis profile
    # embeds standard LRC and separate plain/alternate text fields only.

    # 4. Translation Lyrics
    if lyrics_translation_file:
        trans_text = _read_file(lyrics_translation_file)
        if trans_text and not audio.tags.get("LYRICS_TRANSLATION"):
            audio["LYRICS_TRANSLATION"] = [trans_text]
            decisions.append(
                FieldDecision("LYRICS_TRANSLATION", DecisionAction.ADD, (trans_text,), "translation lyrics sidecar")
            )

    # 5. Romaji / Transliteration Lyrics
    if lyrics_romaji_file:
        romaji_text = _read_file(lyrics_romaji_file)
        if romaji_text and not audio.tags.get("LYRICS_ROMAJI"):
            audio["LYRICS_ROMAJI"] = [romaji_text]
            decisions.append(
                FieldDecision("LYRICS_ROMAJI", DecisionAction.ADD, (romaji_text,), "romanization lyrics sidecar")
            )

    # Picture handling: preserve an existing front unless a caller has already
    # proved the candidate is the exact release and explicitly permits change.
    if picture_file:
        pic_path = Path(picture_file)
        if pic_path.is_file() and pic_path.stat().st_size > 0:
            existing_front = [p for p in audio.pictures if p.type == 3]
            if not existing_front or replace_front_picture:
                retained = [p for p in audio.pictures if p.type != 3]
                audio.clear_pictures()
                for old_picture in retained:
                    audio.add_picture(old_picture)
                pic = Picture()
                pic.type, pic.desc = 3, "Front Cover"
                try:
                    import io

                    from PIL import Image, ImageOps, UnidentifiedImageError

                    with Image.open(pic_path) as image:
                        if image.width * image.height > 80_000_000:
                            raise ValueError("cover exceeds 80 megapixel embedding safety limit")
                        image.load()
                        orientation = int(image.getexif().get(274, 1))
                        target = ImageOps.exif_transpose(image)
                        no_resize = not max_picture_dim or max(image.size) <= max_picture_dim
                        original_format = str(image.format or "").casefold()
                        if max_picture_dim and max(target.size) > max_picture_dim:
                            target.thumbnail((max_picture_dim, max_picture_dim), Image.Resampling.LANCZOS)
                        pic.width, pic.height = target.size
                        pic.depth = 32 if target.mode in {"RGBA", "LA"} else (8 if target.mode in {"P", "L"} else 24)
                        pic.colors = len(image.getpalette() or ()) // 3 if target.mode == "P" else 0
                        can_copy_original = (
                            no_resize
                            and orientation in {0, 1}
                            and original_format in {"jpeg", "png"}
                            and image.mode in {"RGB", "RGBA", "L", "P"}
                            and pic_path.stat().st_size <= max_picture_bytes
                        )
                        if can_copy_original:
                            pic.mime = "image/png" if original_format == "png" else "image/jpeg"
                            pic.data = pic_path.read_bytes()
                        else:
                            output = io.BytesIO()
                            icc_profile = image.info.get("icc_profile")
                            save_options = {"icc_profile": icc_profile} if isinstance(icc_profile, bytes) else {}
                            if original_format == "png":
                                pic.mime = "image/png"
                                target.save(output, format="PNG", optimize=True, **save_options)
                            else:
                                from audiophile_cover_fetcher import _compatibility_rgb

                                pic.mime = "image/jpeg"
                                converted, converted_profile = _compatibility_rgb(target)
                                save_options = {"icc_profile": converted_profile} if converted_profile else {}
                                pic.depth, pic.colors = 24, 0
                                converted.save(
                                    output,
                                    format="JPEG",
                                    quality=92,
                                    progressive=False,
                                    optimize=True,
                                    **save_options,
                                )
                            pic.data = output.getvalue()
                        if len(pic.data) > max_picture_bytes:
                            from audiophile_cover_fetcher import create_embedded_picture_data

                            for embed_size in (1600, 1200, 900, 600):
                                pic.data = create_embedded_picture_data(pic_path.read_bytes(), max_dim=embed_size)
                                pic.mime, pic.depth, pic.colors = "image/jpeg", 24, 0
                                with Image.open(io.BytesIO(pic.data)) as reduced:
                                    pic.width, pic.height = reduced.size
                                if len(pic.data) <= max_picture_bytes:
                                    break
                            if len(pic.data) > max_picture_bytes:
                                raise ValueError("cover exceeds embedded-picture size policy")
                except (UnidentifiedImageError, OSError, ValueError) as exc:
                    raise ValueError(f"invalid cover image {pic_path}: {exc}") from exc
                audio.add_picture(pic)
                decisions.append(
                    FieldDecision(
                        "METADATA_BLOCK_PICTURE",
                        DecisionAction.REPLACE if existing_front else DecisionAction.ADD,
                        (hashlib.sha256(pic.data).hexdigest(),),
                        "front artwork transaction",
                    )
                )

    audio.save()
    return tuple(decisions)


def _tag_fingerprint(tags: object) -> str:
    serial = json.dumps(tags, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serial.encode("utf-8")).hexdigest()


@runtime_checkable
class _DynamicTagMapping(Protocol):
    """Structural boundary for Mutagen's list-backed mapping implementation."""

    def items(self) -> Iterable[tuple[object, object]]: ...


def _snapshot_vorbis_tags(audio: FLAC) -> dict[str, list[str]]:
    """Return a validated typed snapshot of Mutagen's dynamic tag mapping."""
    raw_tags: object = audio.tags
    if raw_tags is None:
        return {}
    if not isinstance(raw_tags, _DynamicTagMapping):
        raise TypeError("Mutagen returned a non-mapping Vorbis comment block")
    snapshot: dict[str, list[str]] = {}
    for raw_key, raw_values in raw_tags.items():
        if not isinstance(raw_key, str):
            raise TypeError("Vorbis comment key is not text")
        if not isinstance(raw_values, list) or not all(isinstance(value, str) for value in raw_values):
            raise TypeError(f"Vorbis comment {raw_key!r} does not contain text values")
        snapshot[canonical_field_name(raw_key)] = list(raw_values)
    return snapshot


def _stream_signature(path: Path) -> tuple[int, int, int, int, int]:
    audio = FLAC(str(path))
    return (
        int(audio.info.sample_rate),
        int(audio.info.channels),
        int(audio.info.bits_per_sample),
        int(audio.info.total_samples),
        int(audio.info.md5_signature),
    )


def pcm_digest(path: Path) -> str:
    """Hash decoded samples, including streams without a stored MD5.

    Decoder output goes to a disposable file to bound memory use. STREAMINFO
    geometry is checked separately by callers. No audio amplitude is changed.
    """
    with tempfile.TemporaryFile() as decoded:
        result = subprocess.run(
            ["flac", "-d", "-c", "-s", "--force-raw-format", "--endian=little", "--sign=signed", str(path)],
            stdout=decoded,
            stderr=subprocess.PIPE,
            timeout=300,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"FLAC PCM validation failed: {result.stderr.decode('utf-8', errors='replace').strip()}")
        decoded.seek(0)
        digest = hashlib.sha256()
        for block in iter(lambda: decoded.read(1024 * 1024), b""):
            digest.update(block)
        return digest.hexdigest()


def _picture_inventory(audio: FLAC) -> list[dict[str, object]]:
    return [
        {
            "type": int(picture.type),
            "description": str(picture.desc or ""),
            "mime": str(picture.mime or ""),
            "width": int(picture.width),
            "height": int(picture.height),
            "sha256": hashlib.sha256(picture.data).hexdigest(),
        }
        for picture in audio.pictures
    ]


def publish_provenance_bytes(ledger: Path, rows: bytes) -> None:
    """Publish a ledger and restore its previous state on catchable failure.

    A process kill still requires manual recovery; this is a single-file
    replacement, not an atomic album commit.
    """
    with _PROVENANCE_LOCK:
        if ledger.is_symlink():
            raise ValueError(f"refusing symlinked provenance ledger: {ledger}")
        existed = ledger.exists()
        old = ledger.read_bytes() if existed else b""
        backup: Path | None = None
        if existed:
            fd, backup_name = tempfile.mkstemp(prefix=f".{ledger.name}.", suffix=".rollback", dir=ledger.parent)
            backup = Path(backup_name)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(old)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                backup.unlink(missing_ok=True)
                raise
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=f".{ledger.name}.", suffix=".pending", dir=ledger.parent)
        except BaseException:
            if backup is not None:
                backup.unlink(missing_ok=True)
            raise
        retain_backup = False
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(old)
                handle.write(rows)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, ledger)
        except BaseException as exc:
            if not os.path.exists(temporary):
                try:
                    if backup is not None:
                        os.replace(backup, ledger)
                    else:
                        ledger.unlink(missing_ok=True)
                except OSError as rollback_exc:
                    retain_backup = True
                    raise RuntimeError(
                        f"ledger publication failed ({exc}); recovery failed ({rollback_exc}); "
                        f"recovery copy retained at {backup}"
                    ) from exc
            raise
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
            if backup is not None and not retain_backup:
                backup.unlink(missing_ok=True)


def _append_provenance_record(ledger: Path, record: Mapping[str, Any]) -> None:
    row = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    publish_provenance_bytes(ledger, row)


def write_tags(
    flac_path: Path,
    tags: list[tuple[str, str]],
    lyrics_file: str | Path | None = None,
    synced_lyrics_file: str | Path | None = None,
    enhanced_lyrics_file: str | Path | None = None,
    ttml_file: str | Path | None = None,
    lyrics_translation_file: str | Path | None = None,
    lyrics_romaji_file: str | Path | None = None,
    picture_file: str | Path | None = None,
    max_picture_dim: int | None = None,
    max_picture_bytes: int = 16 * 1024 * 1024,
    *,
    incoming_confidence: Confidence = Confidence.VERIFIED,
    release_confidence: Confidence = Confidence.HIGH,
    existing_confidence: Confidence = Confidence.HIGH,
    force: bool = False,
    replace_front_picture: bool = False,
    provenance_file: Path | None = None,
    source: str = "enrichment-pipeline",
    dry_run: bool = False,
    provenance_context: Mapping[str, Any] | None = None,
) -> WriteReport:
    """Transactionally update a FLAC and append a machine-readable audit row.

    The source file is never touched until the disposable copy has passed the
    same invariants used by the verifier.  A failed save or verification leaves
    the original byte-for-byte intact.
    """
    flac_path = Path(flac_path)
    if flac_path.is_symlink():
        raise ValueError(f"refusing to replace symlinked FLAC: {flac_path}")
    before = FLAC(str(flac_path))
    before_tags = _snapshot_vorbis_tags(before)
    before_stream = _stream_signature(flac_path)
    before_pcm = pcm_digest(flac_path)
    before_pictures = _picture_inventory(before)
    transaction_id = str(uuid.uuid4())
    fd, tmp_name = tempfile.mkstemp(prefix=f".{flac_path.name}.", suffix=".pending.flac", dir=flac_path.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    backup_path: Path | None = None
    replaced = False
    retain_backup = False
    lyric_archives: dict[Path, str] = {}
    published_archives: list[Path] = []
    try:
        shutil.copy2(flac_path, tmp_path)
        try:
            import mutagen.id3

            mutagen.id3.delete(str(tmp_path))
        except Exception:
            # Most FLACs have no ID3 block.  Verification below remains the
            # authority and will reject a copy where deletion actually failed.
            pass
        decisions = _write_tags_in_place(
            tmp_path,
            [*tags, ("METADATA_PROVENANCE_ID", transaction_id)],
            lyrics_file=lyrics_file,
            synced_lyrics_file=synced_lyrics_file,
            enhanced_lyrics_file=enhanced_lyrics_file,
            ttml_file=ttml_file,
            lyrics_translation_file=lyrics_translation_file,
            lyrics_romaji_file=lyrics_romaji_file,
            picture_file=picture_file,
            max_picture_dim=max_picture_dim,
            max_picture_bytes=max_picture_bytes,
            replace_front_picture=replace_front_picture,
            incoming_confidence=incoming_confidence,
            release_confidence=release_confidence,
            existing_confidence=existing_confidence,
            force=force,
            archive_base=flac_path,
            dry_run=dry_run,
            lyric_archives=lyric_archives,
        )
        canonical_verify(tmp_path, require_core=False)
        stream_check = subprocess.run(
            ["flac", "-t", "-s", str(tmp_path)], capture_output=True, text=True, check=False, timeout=300
        )
        if stream_check.returncode:
            raise RuntimeError(f"temporary FLAC failed decode validation: {stream_check.stderr.strip()}")
        if _stream_signature(tmp_path) != before_stream:
            raise RuntimeError("audio STREAMINFO identity changed during metadata-only transaction")
        if pcm_digest(tmp_path) != before_pcm:
            raise RuntimeError("decoded audio samples changed during metadata-only transaction")
        after_preview = FLAC(str(tmp_path))
        after_tags_preview = _snapshot_vorbis_tags(after_preview)
        comparable_before = {key: values for key, values in before_tags.items() if key != "METADATA_PROVENANCE_ID"}
        comparable_after = {
            key: values for key, values in after_tags_preview.items() if key != "METADATA_PROVENANCE_ID"
        }
        with flac_path.open("rb") as source_handle:
            clean_container = source_handle.read(4) == b"fLaC"
            source_handle.seek(0, os.SEEK_END)
            if source_handle.tell() >= 128:
                source_handle.seek(-128, os.SEEK_END)
                clean_container = clean_container and source_handle.read(3) != b"TAG"
        if (
            clean_container
            and comparable_after == comparable_before
            and _picture_inventory(after_preview) == before_pictures
        ):
            existing_id = (before_tags.get("METADATA_PROVENANCE_ID") or [""])[0]
            return WriteReport(existing_id, decisions, "")
        if dry_run:
            return WriteReport(transaction_id, decisions, "")
        for archive, payload in lyric_archives.items():
            if archive.is_symlink() or (archive.exists() and archive.read_bytes() != payload.encode("utf-8")):
                raise FileExistsError(f"lyric archive collision: {archive}")
        backup_fd, backup_name = tempfile.mkstemp(
            prefix=f".{flac_path.name}.", suffix=".rollback.flac", dir=flac_path.parent
        )
        os.close(backup_fd)
        backup_path = Path(backup_name)
        shutil.copy2(flac_path, backup_path)
        replaced = True
        os.replace(tmp_path, flac_path)
        for archive, payload in lyric_archives.items():
            if not archive.exists():
                with archive.open("x", encoding="utf-8", newline="") as handle:
                    published_archives.append(archive)
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())

        ledger = provenance_file or flac_path.parent / ".metadata-provenance.jsonl"
        after = FLAC(str(flac_path))
        if _stream_signature(flac_path) != before_stream:
            raise RuntimeError("audio STREAMINFO identity changed after atomic replacement")
        context = dict(provenance_context or {})
        record = {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "file": str(context.get("logical_file") or flac_path.name),
            "source": source,
            "context": context,
            "incoming_confidence": incoming_confidence.name,
            "release_confidence": release_confidence.name,
            "before_tags_sha256": _tag_fingerprint(before_tags),
            "after_tags_sha256": _tag_fingerprint(_snapshot_vorbis_tags(after)),
            "before_tags": before_tags,
            "after_tags": _snapshot_vorbis_tags(after),
            "before_pictures": before_pictures,
            "after_pictures": _picture_inventory(after),
            "audio_streaminfo": {
                "sample_rate": before_stream[0],
                "channels": before_stream[1],
                "bits_per_sample": before_stream[2],
                "total_samples": before_stream[3],
                "md5": f"{before_stream[4]:032x}",
            },
            "decisions": [
                {
                    "field": d.field,
                    "action": d.action.value,
                    "values": list(d.values),
                    "reason": d.reason,
                    "entity_scope": FIELD_SPECS[d.field].scope.value if d.field in FIELD_SPECS else "unknown",
                    "incoming_confidence": incoming_confidence.name,
                    "source": source,
                }
                for d in decisions
            ],
        }
        if load_config().preservation.provenance_ledger:
            _append_provenance_record(ledger, record)
        return WriteReport(
            transaction_id, decisions, str(ledger) if load_config().preservation.provenance_ledger else ""
        )
    except BaseException as exc:
        if replaced and backup_path is not None and backup_path.exists():
            try:
                os.replace(backup_path, flac_path)
            except OSError as rollback_exc:
                retain_backup = True
                raise RuntimeError(
                    f"write failed ({exc}); rollback failed ({rollback_exc}); recovery copy retained at {backup_path}"
                ) from exc
        for archive in published_archives:
            archive.unlink(missing_ok=True)
        raise
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
        if not retain_backup and backup_path is not None and backup_path.exists():
            backup_path.unlink()


def main() -> int:
    ap = argparse.ArgumentParser(description="Canonical FLAC Vorbis-comment writer")
    ap.add_argument("positionals", nargs="+", help="FLAC file(s) and optional tags manifest")
    ap.add_argument("--lyrics-file")
    ap.add_argument("--synced-lyrics-file")
    ap.add_argument("--enhanced-lyrics-file")
    ap.add_argument("--ttml-file")
    ap.add_argument("--lyrics-translation-file")
    ap.add_argument("--lyrics-romaji-file")
    ap.add_argument("--picture")
    ap.add_argument("--max-picture-dim", type=int, default=None, help="Max width/height for embedded picture")
    ap.add_argument("--dry-run", action="store_true", help="Plan and validate changes without replacing the FLAC")
    args = ap.parse_args()

    # Split positional args: .flac files are FLAC inputs, non-.flac is the manifest
    flac_files: list[str] = []
    tags_file: str | None = None
    for pos in args.positionals:
        if Path(pos).suffix.casefold() == ".flac":
            flac_files.append(pos)
        else:
            if tags_file is not None:
                return die("only one tags manifest is allowed")
            tags_file = pos
    if not flac_files:
        return die("at least one FLAC input is required")

    has_write_flags = any(
        [
            args.lyrics_file,
            args.synced_lyrics_file,
            args.enhanced_lyrics_file,
            args.ttml_file,
            args.lyrics_translation_file,
            args.lyrics_romaji_file,
            args.picture,
        ]
    )

    if tags_file is None and not has_write_flags:
        # Verify-only mode (batch)
        errors = 0
        for flac_str in flac_files:
            flac = Path(flac_str)
            if not flac.is_file():
                errors += die(f"FLAC does not exist: {flac}")
                continue
            try:
                canonical_verify(flac)
            except (OSError, RuntimeError, ValueError) as exc:
                errors += die(f"{flac}: {exc}")
        return errors

    # Write mode: single file
    if len(flac_files) != 1:
        return die("Write mode requires exactly one FLAC file")

    flac = Path(flac_files[0])
    if not flac.is_file():
        return die(f"FLAC does not exist: {flac}")

    try:
        if tags_file is not None:
            manifest = Path(tags_file)
            tags = load_tags(manifest)
        else:
            # The writer reads and preserves existing comments itself. Passing
            # them back as new input would duplicate the old provenance ID and
            # turn foreign multi-value comments into contradictory proposals.
            tags = []

        write_tags(
            flac,
            tags,
            lyrics_file=args.lyrics_file,
            synced_lyrics_file=args.synced_lyrics_file,
            enhanced_lyrics_file=args.enhanced_lyrics_file,
            ttml_file=args.ttml_file,
            lyrics_translation_file=args.lyrics_translation_file,
            lyrics_romaji_file=args.lyrics_romaji_file,
            picture_file=args.picture,
            max_picture_dim=args.max_picture_dim,
            dry_run=args.dry_run,
        )

        if not args.dry_run:
            canonical_verify(flac, require_core=False)
    except (OSError, RuntimeError, ValueError) as exc:
        return die(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
