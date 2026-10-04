#!/usr/bin/env python3
"""Infer release track positions without assuming a flat album directory."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from mutagen.flac import FLAC

DISC_DIR_RE = re.compile(r"^(?:cd|disc|disk)\s*[-_. ]*0*(\d{1,2})(?:\D.*)?$", re.IGNORECASE)
DISC_TRACK_RE = re.compile(r"^0*(\d{1,2})[-_.]0*(\d{1,3})(?:\s*-\s*|[ ._-]+)(.*)$")
TRACK_RE = re.compile(r"^0*(\d{1,3})(?:\s*-\s*|[ ._-]+)(.*)$")


def _positive_int(value: object) -> int | None:
    match = re.match(r"^\s*(\d+)", str(value or ""))
    if not match:
        return None
    number = int(match.group(1))
    return number if number > 0 else None


def infer_position(
    path: Path, album_root: Path, tags: Mapping[str, Sequence[str]] | None = None
) -> tuple[int, int, str]:
    stem = path.stem
    match = DISC_TRACK_RE.match(stem)
    if match:
        return int(match.group(1)), int(match.group(2)), match.group(3).strip()

    disc: int | None = None
    try:
        relatives = path.relative_to(album_root).parents
    except ValueError:
        relatives = path.parents
    for parent in relatives:
        if str(parent) == ".":
            continue
        dm = DISC_DIR_RE.match(parent.name)
        if dm:
            disc = int(dm.group(1))
            break

    track_match = TRACK_RE.match(stem)
    track = int(track_match.group(1)) if track_match else None
    title = track_match.group(2).strip() if track_match else ""
    if tags:
        if disc is None:
            disc = _positive_int((tags.get("DISCNUMBER") or [""])[0])
        if track is None:
            track = _positive_int((tags.get("TRACKNUMBER") or [""])[0])
    return disc or 1, track or 1, title


def position_evidence(
    path: Path, album_root: Path, tags: Mapping[str, Sequence[str]] | None = None
) -> tuple[int | None, int | None, str, tuple[str, ...]]:
    """Return non-invented numbering plus conflicts between local evidence.

    ``infer_position`` remains a compatibility helper for read-only display,
    while writers must use this function and refuse an unresolved position.
    """
    stem = path.stem
    filename_disc: int | None = None
    filename_track: int | None = None
    title = ""
    match = DISC_TRACK_RE.match(stem)
    if match:
        filename_disc, filename_track, title = int(match.group(1)), int(match.group(2)), match.group(3).strip()
    else:
        match = TRACK_RE.match(stem)
        if match:
            filename_track, title = int(match.group(1)), match.group(2).strip()
    directory_disc: int | None = None
    try:
        parents = path.relative_to(album_root).parents
    except ValueError:
        parents = path.parents
    for parent in parents:
        found = DISC_DIR_RE.match(parent.name)
        if found:
            directory_disc = int(found.group(1))
            break
    tag_disc = _positive_int((tags or {}).get("DISCNUMBER", [""])[0])
    tag_track = _positive_int((tags or {}).get("TRACKNUMBER", [""])[0])
    conflicts: list[str] = []
    if filename_disc and directory_disc and filename_disc != directory_disc:
        conflicts.append("filename DISCNUMBER conflicts with disc directory")
    if filename_disc and tag_disc and filename_disc != tag_disc:
        conflicts.append("filename DISCNUMBER conflicts with embedded tag")
    if directory_disc and tag_disc and directory_disc != tag_disc:
        conflicts.append("disc directory conflicts with embedded tag")
    if filename_track and tag_track and filename_track != tag_track:
        conflicts.append("filename TRACKNUMBER conflicts with embedded tag")
    disc = filename_disc or directory_disc or tag_disc
    track = filename_track or tag_track
    if disc is None and track is not None and not conflicts:
        # Flat numbered folders conventionally represent the first medium.
        # This is deliberately limited to disc number; unknown tracks remain
        # a hard review condition for writers.
        disc = 1
    return disc, track, title, tuple(conflicts)


def disc_counts(files: Sequence[Path], album_root: Path) -> dict[int, int]:
    counts: dict[int, int] = {}
    for path in files:
        # Do not let an unresolved filename turn into a fictional 1/1 layout
        # during release verification.  Writers will surface it for review.
        tags = {str(key).upper(): list(values) for key, values in FLAC(path).items()} if path.is_file() else {}
        disc, track, _title, conflicts = position_evidence(path, album_root, tags)
        if disc is None or track is None or conflicts:
            continue
        counts[disc] = counts.get(disc, 0) + 1
    return counts
