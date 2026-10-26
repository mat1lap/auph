#!/usr/bin/env python3
"""Canonical Vorbis-comment schema used by the enrichment pipeline.

Vorbis comments are intentionally open ended.  This module does not pretend
that Xiph standardises every music-library field; it records the conventions
chosen by this project and, importantly, their cardinality and identity scope.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from enum import Enum


class Cardinality(str, Enum):
    SINGLE = "single"
    MULTI = "multi"
    MULTILINE = "multiline"


class Scope(str, Enum):
    RECORDING = "recording"
    RELEASE_GROUP = "release-group"
    RELEASE = "release"
    MEDIUM = "medium"
    TRACK = "release-track"
    FILE = "file"


@dataclass(frozen=True)
class FieldSpec:
    cardinality: Cardinality
    scope: Scope
    preserve_existing: bool = False
    stable_identifier: bool = False


def _s(scope: Scope, *, preserve: bool = False, stable: bool = False) -> FieldSpec:
    return FieldSpec(Cardinality.SINGLE, scope, preserve, stable)


def _m(scope: Scope, *, preserve: bool = False, stable: bool = False) -> FieldSpec:
    return FieldSpec(Cardinality.MULTI, scope, preserve, stable)


def _l(scope: Scope, *, preserve: bool = True) -> FieldSpec:
    return FieldSpec(Cardinality.MULTILINE, scope, preserve)


# Canonical names follow common Xiph/Picard practice.  In particular,
# MUSICBRAINZ_TRACKID is the recording MBID and MUSICBRAINZ_RELEASETRACKID is
# the release-track MBID.  ARTIST remains the credited display string while
# ARTISTS carries repeatable credited artist components.
FIELD_SPECS: dict[str, FieldSpec] = {
    "TITLE": _s(Scope.RECORDING),
    "TITLESORT": _s(Scope.RECORDING),
    "ARTIST": _s(Scope.RECORDING),
    "ARTISTS": _m(Scope.RECORDING),
    "ARTISTSORT": _s(Scope.RECORDING),
    "ALBUM": _s(Scope.RELEASE),
    "ALBUMSORT": _s(Scope.RELEASE),
    "ALBUMARTIST": _s(Scope.RELEASE),
    "ALBUMARTISTS": _m(Scope.RELEASE),
    "ALBUMARTISTSORT": _s(Scope.RELEASE),
    "TRACKNUMBER": _s(Scope.TRACK),
    "TRACKTOTAL": _s(Scope.MEDIUM),
    "DISCNUMBER": _s(Scope.TRACK),
    "DISCTOTAL": _s(Scope.RELEASE),
    "DISCSUBTITLE": _s(Scope.MEDIUM),
    "DATE": _s(Scope.RELEASE),
    "ORIGINALDATE": _s(Scope.RELEASE_GROUP),
    "RELEASETYPE": _m(Scope.RELEASE),
    "EDITION": _s(Scope.RELEASE),
    "MEDIA": _s(Scope.MEDIUM),
    "COUNTRY": _s(Scope.RELEASE),
    "LABEL": _m(Scope.RELEASE),
    "CATALOGNUMBER": _m(Scope.RELEASE),
    "BARCODE": _s(Scope.RELEASE, stable=True),
    "ISRC": _m(Scope.RECORDING, stable=True),
    "MUSICBRAINZ_TRACKID": _s(Scope.RECORDING, stable=True),
    "MUSICBRAINZ_RELEASETRACKID": _s(Scope.TRACK, stable=True),
    "MUSICBRAINZ_ALBUMID": _s(Scope.RELEASE, stable=True),
    "MUSICBRAINZ_RELEASEGROUPID": _s(Scope.RELEASE_GROUP, stable=True),
    "MUSICBRAINZ_ARTISTID": _m(Scope.RECORDING, stable=True),
    "MUSICBRAINZ_ALBUMARTISTID": _m(Scope.RELEASE, stable=True),
    "MUSICBRAINZ_WORKID": _m(Scope.RECORDING, stable=True),
    "MUSICBRAINZ_DISCID": _s(Scope.MEDIUM, stable=True),
    "ACOUSTID_ID": _m(Scope.RECORDING, stable=True),
    "COMPOSER": _m(Scope.RECORDING),
    "COMPOSERSORT": _m(Scope.RECORDING),
    "LYRICIST": _m(Scope.RECORDING),
    "WRITER": _m(Scope.RECORDING),
    "CONDUCTOR": _m(Scope.RECORDING),
    "PERFORMER": _m(Scope.RECORDING),
    "REMIXER": _m(Scope.RECORDING),
    "PRODUCER": _m(Scope.RECORDING),
    "ARRANGER": _m(Scope.RECORDING),
    "GENRE": _m(Scope.RELEASE),
    "STYLE": _m(Scope.RELEASE),
    "LANGUAGE": _m(Scope.RECORDING),
    "COPYRIGHT": _m(Scope.RELEASE, preserve=True),
    "COMMENT": _l(Scope.FILE),
    "LYRICS": _l(Scope.RECORDING),
    "UNSYNCEDLYRICS": _l(Scope.RECORDING),
    "SYNCEDLYRICS": _l(Scope.RECORDING),
    "SYNCELYRICS": _l(Scope.RECORDING),
    "SYNCEDLYRICS_ENHANCED": _l(Scope.RECORDING),
    "LYRICS_TTML": _l(Scope.RECORDING),
    "LYRICS_TRANSLATION": _l(Scope.RECORDING),
    "LYRICS_ROMAJI": _l(Scope.RECORDING),
    # These project-specific Vorbis comments describe text forms only when a
    # provider supplied explicit language evidence. The generic LANGUAGE tag
    # remains track/release metadata and is not inferred from translated text.
    "LYRICS_LANGUAGE": _s(Scope.RECORDING, preserve=True),
    "LYRICS_TRANSLATION_LANGUAGE": _s(Scope.RECORDING, preserve=True),
    "LYRICS_ROMAJI_LANGUAGE": _s(Scope.RECORDING, preserve=True),
    "REPLAYGAIN_TRACK_GAIN": _s(Scope.FILE, preserve=True),
    "REPLAYGAIN_TRACK_PEAK": _s(Scope.FILE, preserve=True),
    "REPLAYGAIN_TRACK_RANGE": _s(Scope.FILE, preserve=True),
    "REPLAYGAIN_ALBUM_GAIN": _s(Scope.FILE, preserve=True),
    "REPLAYGAIN_ALBUM_PEAK": _s(Scope.FILE, preserve=True),
    "REPLAYGAIN_ALBUM_RANGE": _s(Scope.FILE, preserve=True),
    "REPLAYGAIN_REFERENCE_LOUDNESS": _s(Scope.FILE, preserve=True),
    "METADATA_PROVENANCE_ID": _s(Scope.FILE),
    # Compatibility and project extension fields.  They remain explicit here
    # so cardinality is deterministic instead of being guessed by the writer.
    "YEAR": _s(Scope.RELEASE),
    "ORIGINALYEAR": _s(Scope.RELEASE_GROUP),
    "RECORDINGDATE": _s(Scope.RECORDING),
    "RELEASESTATUS": _s(Scope.RELEASE),
    "RELEASECOUNTRY": _s(Scope.RELEASE),
    "COMPILATION": _s(Scope.RELEASE),
    "VERSION": _s(Scope.RECORDING),
    "DISCNAME": _s(Scope.MEDIUM),
    "GROUPING": _s(Scope.RECORDING),
    "WORK": _m(Scope.RECORDING),
    "MOVEMENT": _s(Scope.RECORDING),
    "MOVEMENTNAME": _s(Scope.RECORDING),
    "MOVEMENTTOTAL": _s(Scope.RECORDING),
    "OPUS": _s(Scope.RECORDING),
    "OPUSNUMBER": _s(Scope.RECORDING),
    "PART": _s(Scope.RECORDING),
    "COMPOSITION_ID": _s(Scope.RECORDING),
    "COMPOSITION_CATALOG": _s(Scope.RECORDING),
    "ISWC": _m(Scope.RECORDING, stable=True),
    "DISCOGS_RELEASE_ID": _s(Scope.RELEASE, stable=True),
    "DISCOGS_RELEASE_URL": _s(Scope.RELEASE),
    "DISCOGS_MASTER_ID": _s(Scope.RELEASE),
    "DISCOGS_ARTIST_ID": _m(Scope.RECORDING),
    "DISCOGS_NOTES": _l(Scope.RELEASE),
    "FREEDB_DISCID": _s(Scope.MEDIUM),
    "FREEDB_DISCID_SOURCE": _s(Scope.MEDIUM),
    "MUSICBRAINZ_DISCID_SOURCE": _s(Scope.MEDIUM),
    "MUSICBRAINZ_ORIGINALRELEASEDATE": _s(Scope.RECORDING),
    "ACOUSTID_FINGERPRINT": _s(Scope.RECORDING),
    "RIGHTSSOCIETY": _m(Scope.RELEASE),
    "MATRIX": _m(Scope.RELEASE, preserve=True),
    "MATRIX_SOURCE": _s(Scope.RELEASE),
    "MASTERING_SID_CODE": _m(Scope.RELEASE, preserve=True),
    "MOULD_SID_CODE": _m(Scope.RELEASE, preserve=True),
    "PRESSING_PLANT": _m(Scope.RELEASE),
    "PRESSED_BY": _m(Scope.RELEASE),
    "MANUFACTURER": _m(Scope.RELEASE),
    "GLASS_MASTERED_AT": _m(Scope.RELEASE),
    "MASTERED_BY": _m(Scope.RELEASE),
    "MASTERINGENGINEER": _m(Scope.RELEASE),
    "REMASTERED_BY": _m(Scope.RELEASE),
    "REMASTERER": _m(Scope.RELEASE),
    "REMASTERINGENGINEER": _m(Scope.RELEASE),
    "ENGINEER": _m(Scope.RELEASE),
    "MIXER": _m(Scope.RELEASE),
    "MIXINGENGINEER": _m(Scope.RELEASE),
    "ENSEMBLE": _m(Scope.RECORDING),
    "ORCHESTRA": _m(Scope.RECORDING),
    "CHOIR": _m(Scope.RECORDING),
    "CHORUS": _m(Scope.RECORDING),
    "SOLOIST": _m(Scope.RECORDING),
    "LIBRETTIST": _m(Scope.RECORDING),
    "STUDIO": _m(Scope.RECORDING),
    "RECORDINGSTUDIO": _m(Scope.RECORDING),
    "RECORDINGLOCATION": _m(Scope.RECORDING),
    "MIXINGSTUDIO": _m(Scope.RELEASE),
    "MASTERINGSTUDIO": _m(Scope.RELEASE),
    "VENUE": _s(Scope.RECORDING),
    "LOCATION": _s(Scope.RECORDING),
    "LIVE": _s(Scope.RECORDING),
    "SOURCE_FORMAT": _s(Scope.RELEASE),
    "AUDIOPHILE_SERIES": _s(Scope.RELEASE),
    "SPARSCODE": _s(Scope.RELEASE),
    "PRE_EMPHASIS": _s(Scope.FILE, preserve=True),
    "PRE-EMPHASIS": _s(Scope.FILE, preserve=True),
    "DE_EMPHASIS_APPLIED": _s(Scope.FILE, preserve=True),
    "FLAGS": _s(Scope.FILE, preserve=True),
    "HDCD": _s(Scope.FILE, preserve=True),
    "HDCD_DETECTED": _s(Scope.FILE, preserve=True),
    "HDCD_PEAK_EXTEND": _s(Scope.FILE, preserve=True),
    "INSTRUMENTAL": _s(Scope.RECORDING),
    "ACCURATERIPRESULT": _s(Scope.FILE, preserve=True),
    "ACCURATERIPCRC": _s(Scope.FILE, preserve=True),
    "CTDB_TOCID": _s(Scope.FILE, preserve=True),
    "CTDB_STATUS": _s(Scope.FILE, preserve=True),
    "COPY_CRC": _s(Scope.FILE, preserve=True),
    "TEST_CRC": _s(Scope.FILE, preserve=True),
    "EAC_PEAK": _s(Scope.FILE, preserve=True),
    "EAC_PEAKS": _s(Scope.RELEASE, preserve=True),
    "DR": _s(Scope.FILE, preserve=True),
    "DYNAMIC_RANGE": _s(Scope.FILE, preserve=True),
    "ALBUM_DR": _s(Scope.FILE, preserve=True),
    "ALBUM_DYNAMIC_RANGE": _s(Scope.FILE, preserve=True),
    "ALBUM_MIN_DR": _s(Scope.FILE, preserve=True),
    "ALBUM_MAX_DR": _s(Scope.FILE, preserve=True),
    "DR_PEAK": _s(Scope.FILE, preserve=True),
    "DR_RMS": _s(Scope.FILE, preserve=True),
    "DR_SOURCE": _s(Scope.FILE, preserve=True),
    "DR_LOUDNESS_WAR_ID": _s(Scope.RELEASE),
    "DR_LOUDNESS_WAR_URL": _s(Scope.RELEASE),
    "BPM": _s(Scope.RECORDING),
    "TEMPO": _s(Scope.RECORDING),
    "KEY": _s(Scope.RECORDING),
    "INITIALKEY": _s(Scope.RECORDING),
    "CAMELOT_KEY": _s(Scope.RECORDING),
    "MOOD": _m(Scope.RECORDING),
    "ENERGY": _s(Scope.RECORDING),
    "DANCEABILITY": _s(Scope.RECORDING),
    "VALENCE": _s(Scope.RECORDING),
    "BPM_SOURCE": _s(Scope.FILE),
    "KEY_SOURCE": _s(Scope.FILE),
    "ANALYSIS_SOURCE": _m(Scope.FILE),
    "DATE_SOURCE": _s(Scope.RELEASE),
    "METADATA_MATCH": _s(Scope.FILE),
    "METADATA_SOURCE": _m(Scope.FILE),
    "TAGGER": _s(Scope.FILE),
    "LYRICS_SOURCE": _s(Scope.RECORDING),
    "LYRICS_STATUS": _s(Scope.RECORDING),
    "LYRICS_HAS_WORD_SYNC": _s(Scope.RECORDING),
    "LYRICS_HAS_BACKGROUND_VOCALS": _s(Scope.RECORDING),
    "RIP_TOC_SOURCE": _s(Scope.MEDIUM),
    "RIP_TOC_TRACKS": _s(Scope.MEDIUM),
}

ALIASES: dict[str, str] = {
    "TOTALTRACKS": "TRACKTOTAL",
    "TOTALDISCS": "DISCTOTAL",
    "ORIGINALRELEASEDATE": "ORIGINALDATE",
    "RECORDING_DATE": "RECORDINGDATE",
    "DYNAMIC RANGE": "DYNAMIC_RANGE",
    "ALBUM DYNAMIC RANGE": "ALBUM_DYNAMIC_RANGE",
}

MULTILINE_FIELDS = frozenset(name for name, spec in FIELD_SPECS.items() if spec.cardinality is Cardinality.MULTILINE)
SINGLE_VALUED_FIELDS = frozenset(name for name, spec in FIELD_SPECS.items() if spec.cardinality is Cardinality.SINGLE)
MULTI_VALUED_FIELDS = frozenset(name for name, spec in FIELD_SPECS.items() if spec.cardinality is Cardinality.MULTI)
STABLE_ID_FIELDS = frozenset(name for name, spec in FIELD_SPECS.items() if spec.stable_identifier)

DATE_RE = re.compile(r"^\d{4}(?:-\d{2}(?:-\d{2})?)?$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.IGNORECASE)
ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")


def valid_date(value: str) -> bool:
    """Validate calendar components while preserving year/month precision."""
    if not DATE_RE.fullmatch(value):
        return False
    parts = [int(part) for part in value.split("-")]
    try:
        date(parts[0], parts[1] if len(parts) > 1 else 1, parts[2] if len(parts) > 2 else 1)
    except ValueError:
        return False
    return True


def canonical_field_name(name: str) -> str:
    field = unicodedata.normalize("NFC", str(name)).strip().upper()
    if field.startswith("PERFORMER:"):
        return "PERFORMER:" + field.partition(":")[2].replace(" ", "_")
    return ALIASES.get(field, field)


def field_scope(name: str) -> Scope | None:
    """Return the entity scope for a known canonical field.

    Callers use this instead of treating any UUID agreement as permission to
    alter every field in a file.  Unknown extension fields deliberately have
    no inferred scope and therefore remain preservation-only by default.
    """
    spec = FIELD_SPECS.get(canonical_field_name(name))
    return spec.scope if spec else None


def normalize_value(field: str, value: object) -> str:
    """Lossless-enough canonicalisation for storage, never typography rewriting."""
    name = canonical_field_name(field)
    if value is None:
        return ""
    if not isinstance(value, (str, int, float, bool)):
        raise TypeError(f"unsupported value type for {name}: {type(value).__name__}")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"non-finite numeric value for {name}")
    text = str(value)
    text = text.removeprefix("\ufeff")
    text = text.replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lyric_fields = {
        "LYRICS",
        "UNSYNCEDLYRICS",
        "SYNCEDLYRICS",
        "SYNCELYRICS",
        "SYNCEDLYRICS_ENHANCED",
        "LYRICS_TTML",
        "LYRICS_TRANSLATION",
        "LYRICS_ROMAJI",
    }
    if name in lyric_fields:
        # Keep the original Unicode normalization form and framing in lyrics.
        # Comparison code may use NFC/NFKC; the stored text remains a source
        # representation except for invalid NUL and transport BOM/line ends.
        return text
    # NUL is not valid in Vorbis values.  Strip it only when serializing the
    # canonical tag; raw provider evidence belongs in provenance.
    text = unicodedata.normalize("NFC", text)
    if name in MULTILINE_FIELDS:
        # Lyrics are archival text. Leading/trailing blank lines and indentation
        # can be part of the provider document, so preserve them exactly after
        # transport line-ending normalization. Other multiline metadata keeps
        # its historical framing cleanup.
        return text.strip("\n")
    text = " ".join(part.strip() for part in text.splitlines() if part.strip()).strip()
    if name in {"CATALOGNUMBER", "BARCODE"} and re.fullmatch(r"(?i:none|\[none\]|n/?a|\?)", text):
        return ""
    if name == "RELEASETYPE":
        return text.casefold()
    if name == "ISRC":
        return re.sub(r"[-\s]", "", text).upper()
    return text


def values_equal(left: str, right: str) -> bool:
    return unicodedata.normalize("NFC", left).casefold() == unicodedata.normalize("NFC", right).casefold()
