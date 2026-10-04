#!/usr/bin/env python3
"""ReplayGain 2.0 tag validation.

The scanner may use EBU R128 analysis internally, but FLAC output deliberately
uses the widely supported REPLAYGAIN_* comments. Existing R128_* values are
preserved as foreign/Opus-oriented evidence; this pipeline does not fabricate
them for FLAC.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from mutagen.flac import FLAC

GAIN_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?\s*dB$", re.IGNORECASE)
PEAK_RE = re.compile(r"^\d+(?:\.\d+)?$")
REF_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?\s*LUFS$", re.IGNORECASE)
R128_GAIN_RE = re.compile(r"^[+-]?\d{1,5}$")


def validate_replaygain(path: Path, *, require_album: bool = True) -> tuple[bool, tuple[str, ...]]:
    raw_tags = FLAC(str(path)).tags
    tags: dict[str, list[Any]] = {}
    if raw_tags:
        for name in list(raw_tags.keys()):
            tags[str(name).upper()] = list(raw_tags[name])
    errors: list[str] = []
    required = ["REPLAYGAIN_TRACK_GAIN", "REPLAYGAIN_TRACK_PEAK"]
    if require_album:
        required.extend(("REPLAYGAIN_ALBUM_GAIN", "REPLAYGAIN_ALBUM_PEAK"))
    for field in required:
        values = tags.get(field, [])
        if len(values) != 1:
            errors.append(f"{field} must occur exactly once")
            continue
        regex = GAIN_RE if field.endswith("GAIN") else PEAK_RE
        value = str(values[0]).strip()
        numeric = value[:-2].strip() if field.endswith("GAIN") else value
        if not regex.fullmatch(value) or not math.isfinite(float(numeric)):
            errors.append(f"{field} has invalid format")
    ref = tags.get("REPLAYGAIN_REFERENCE_LOUDNESS", [])
    if ref and (
        len(ref) != 1
        or not REF_RE.fullmatch(str(ref[0]).strip())
        or not math.isfinite(float(str(ref[0]).strip()[:-4].strip()))
    ):
        errors.append("REPLAYGAIN_REFERENCE_LOUDNESS has invalid format")
    for field in ("R128_TRACK_GAIN", "R128_ALBUM_GAIN"):
        values = tags.get(field, [])
        if values and (len(values) != 1 or not R128_GAIN_RE.fullmatch(str(values[0]).strip())):
            errors.append(f"{field} has invalid Q7.8 integer format")
    return not errors, tuple(errors)
