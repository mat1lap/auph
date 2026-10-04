#!/usr/bin/env python3
"""Structured, conservative lyrics parsing, validation and candidate selection."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from itertools import pairwise
from typing import Literal


@dataclass(frozen=True)
class LyricsLine:
    """One lyric or annotation line. Timestamps are exact integer milliseconds."""

    text: str
    timestamp_ms: int | None = None
    source_precision: int = 0
    kind: Literal["lyric", "section", "metadata", "blank"] = "lyric"


@dataclass(frozen=True)
class LyricsDocument:
    """Parsed document retained alongside its untouched provider representation."""

    lines: tuple[LyricsLine, ...]
    synchronized: bool
    language: str = ""
    source_format: str = "plain"
    offset_ms: int = 0
    warnings: tuple[str, ...] = ()
    raw: str = field(default="", compare=False, repr=False)

    @property
    def timestamp_count(self) -> int:
        return sum(row.timestamp_ms is not None for row in self.lines)

    @property
    def timestamp_coverage(self) -> float:
        lyric_lines = sum(row.kind == "lyric" and bool(row.text.strip()) for row in self.lines)
        timed_lines = sum(
            row.kind == "lyric" and bool(row.text.strip()) and row.timestamp_ms is not None for row in self.lines
        )
        return timed_lines / lyric_lines if lyric_lines else 0.0

    @property
    def first_timestamp_ms(self) -> int | None:
        return next((row.timestamp_ms for row in self.lines if row.timestamp_ms is not None), None)

    @property
    def last_timestamp_ms(self) -> int | None:
        return next((row.timestamp_ms for row in reversed(self.lines) if row.timestamp_ms is not None), None)

    @property
    def line_count(self) -> int:
        return len(self.lines)

    @property
    def character_count(self) -> int:
        return sum(len(row.text) for row in self.lines)


@dataclass(frozen=True)
class LyricsCandidate:
    provider: str
    text: str
    synced: bool = False
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: float | None = None
    language: str = ""
    isrc: str = ""
    recording_id: str = ""
    attribution: str = ""
    generated: bool = False
    translation: bool = False
    romanization: bool = False
    metadata: dict[str, object] = field(default_factory=dict)
    provider_track_id: str = ""
    album_artist: str = ""
    version: str = ""
    raw_lyrics: str = ""
    plain_lyrics: str = ""
    synced_lyrics: str = ""
    parsed: LyricsDocument | None = None
    transformations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class LyricsAssessment:
    candidate: LyricsCandidate
    score: float
    valid: bool
    reasons: tuple[str, ...]
    match_confidence: float = 0.0
    version_confidence: float = 0.0
    text_quality: float = 0.0
    sync_quality: float = 0.0
    source_confidence: float = 0.0
    warnings: tuple[str, ...] = ()


TIMESTAMP_RE = re.compile(r"\[(?:(?P<h>\d{1,3}):)?(?P<m>\d{1,3}):(?P<s>[0-5]\d)(?:[.:](?P<f>\d{1,3}))?\]")
MAX_LYRICS_CHARS = 1_000_000
WORD_TIMESTAMP_RE = re.compile(r"<(?:(?P<h>\d{1,3}):)?(?P<m>\d{1,3}):(?P<s>[0-5]\d)(?:[.:](?P<f>\d{1,3}))?>")
META_RE = re.compile(r"^\[(?:ar|ti|al|au|by|offset|length|re|ve|id):.*\]$", re.IGNORECASE)
_META_VALUE_RE = re.compile(r"^\[(?P<key>ar|ti|al|au|by|offset|length|re|ve|id):(?P<value>.*)\]$", re.IGNORECASE)
_SECTION_RE = re.compile(
    r"^\s*\[(?:intro|verse(?:\s+\d+)?|pre-?chorus|post-?chorus|chorus|hook|bridge|refrain|"
    r"outro|instrumental|guitar solo|solo|spoken|interlude|куплет|припев|бридж|интро|аутро|вступление|кода)\]\s*$",
    re.IGNORECASE,
)
_VERSION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("live", re.compile(r"\blive\b|\bconcert\b|\bunplugged\b", re.IGNORECASE)),
    ("remix", re.compile(r"\bremix\b|\b(?:club|extended|radio|original)\s+mix\b", re.IGNORECASE)),
    ("remastered", re.compile(r"\bremaster(?:ed)?\b", re.IGNORECASE)),
    ("radio_edit", re.compile(r"\bradio\s+(?:edit|version)\b", re.IGNORECASE)),
    ("extended", re.compile(r"\bextended\b|\b12[\"′']?\b", re.IGNORECASE)),
    ("acoustic", re.compile(r"\bacoustic\b|\bunplugged\b", re.IGNORECASE)),
    ("demo", re.compile(r"\bdemo\b|\bdemonstration\b", re.IGNORECASE)),
    ("instrumental", re.compile(r"\binstrumental\b|\bk-?araoke\b|\bbacking track\b", re.IGNORECASE)),
    ("explicit", re.compile(r"\bexplicit\b|\buncensored\b", re.IGNORECASE)),
    ("clean", re.compile(r"\bclean\b|\bradio edit\b", re.IGNORECASE)),
    ("original", re.compile(r"\boriginal(?:\s+(?:mix|version))?\b", re.IGNORECASE)),
    ("alternate_take", re.compile(r"\b(?:alternate|alternative)\s+(?:take|version|mix)\b", re.IGNORECASE)),
    ("re_recorded", re.compile(r"\bre-?recorded\b|\bre-?recording\b", re.IGNORECASE)),
    ("mono", re.compile(r"\bmono(?:phonic)?\b", re.IGNORECASE)),
    ("stereo", re.compile(r"\bstereo(?:phonic)?\b", re.IGNORECASE)),
    ("acapella", re.compile(r"\ba\s*capella\b|\ba\s+cappella\b", re.IGNORECASE)),
)


def _comparison_text(text: str) -> str:
    """Aggressive, comparison-only folding. Never use this to serialize lyrics."""
    folded = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text)).casefold()
    folded = "".join(char for char in folded if not unicodedata.combining(char)).replace("&", " and ")
    return " ".join(re.findall(r"[\w]+", folded, flags=re.UNICODE))


def normalize_lookup(text: str) -> str:
    """Normalize identity text without deleting version-defining words."""
    return _comparison_text(text)


def text_similarity(left: str, right: str) -> float:
    a, b = normalize_lookup(left), normalize_lookup(right)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def conservative_text(raw: str) -> str:
    """Normalize transport details while retaining all printable Unicode."""
    text = str(raw)
    text = text.removeprefix("\ufeff")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # NUL and other C0/C1 controls are not valid lyric content for Vorbis
    # comments. Keep tabs and line breaks, and preserve format characters such
    # as ZWJ/ZWNJ used by real scripts.
    return "".join(char for char in text if char in "\n\t" or unicodedata.category(char) not in {"Cc"})


def _time_parts(match: re.Match[str]) -> tuple[int, int]:
    fraction = match.group("f") or ""
    hours = int(match.group("h") or 0)
    minutes = int(match.group("m"))
    seconds = int(match.group("s"))
    if match.group("h") is not None and minutes > 59:
        raise ValueError("minute field in hh:mm:ss must be below 60")
    millis = int((fraction + "000")[:3]) if fraction else 0
    return ((hours * 3600 + minutes * 60 + seconds) * 1000 + millis, len(fraction))


def timestamp_seconds(match: re.Match[str]) -> float:
    milliseconds, _ = _time_parts(match)
    return milliseconds / 1000.0


def parse_lrc(text: str, *, language: str = "") -> LyricsDocument:
    """Parse LRC into rows without sorting, deleting, or rewriting lyric text."""
    if len(text) > MAX_LYRICS_CHARS:
        raise ValueError(f"lyrics payload exceeds {MAX_LYRICS_CHARS} character safety limit")
    source = conservative_text(text)
    rows: list[LyricsLine] = []
    warnings: list[str] = []
    offset_ms = 0
    for line_no, line in enumerate(source.split("\n"), 1):
        if not line:
            rows.append(LyricsLine("", kind="blank"))
            continue
        header = _META_VALUE_RE.fullmatch(line.strip())
        if header:
            if header.group("key").casefold() == "offset":
                try:
                    offset_ms = int(header.group("value"))
                except ValueError:
                    warnings.append(f"line {line_no}: malformed offset header")
            rows.append(LyricsLine(line, kind="metadata"))
            continue
        pos = 0
        stamps: list[tuple[int, int]] = []
        while pos < len(line):
            prefix_pos = pos
            match_pos = pos
            while match_pos < len(line) and line[match_pos].isspace():
                match_pos += 1
            match = TIMESTAMP_RE.match(line, match_pos)
            if not match:
                # Whitespace between the last timecode and text belongs to
                # the lyric. Keep it in the body rather than trimming it.
                pos = prefix_pos
                break
            try:
                ms, precision = _time_parts(match)
            except ValueError:
                warnings.append(f"line {line_no}: invalid timestamp field")
                break
            stamps.append((ms, precision))
            pos = match.end()
        body = line[pos:]
        if stamps:
            # One source row with multiple timing marks becomes multiple typed
            # rows while preserving the provider's timestamp order.
            for ms, precision in stamps:
                rows.append(LyricsLine(body, ms, precision))
        elif _SECTION_RE.fullmatch(line.strip()):
            rows.append(LyricsLine(line, kind="section"))
        elif TIMESTAMP_RE.search(line) or re.search(r"\[\s*[-+]?\d{1,4}:\d{2}", line):
            warnings.append(f"line {line_no}: malformed or non-prefix timestamp")
            rows.append(LyricsLine(line))
        else:
            rows.append(LyricsLine(line))
    return LyricsDocument(
        tuple(rows),
        any(row.timestamp_ms is not None for row in rows),
        language,
        "lrc",
        offset_ms,
        tuple(warnings),
        source,
    )


def serialize_lrc(document: LyricsDocument, *, include_headers: bool = True) -> str:
    """Serialize LRC rows using each source row's available precision."""
    if include_headers and document.raw:
        source = conservative_text(document.raw)
        reparsed = parse_lrc(source, language=document.language)
        if reparsed.lines == document.lines and reparsed.offset_ms == document.offset_ms:
            return source
    output: list[str] = []
    if include_headers and document.offset_ms:
        output.append(f"[offset:{document.offset_ms}]")
    for row in document.lines:
        if row.kind == "blank":
            output.append("")
            continue
        if row.kind == "metadata":
            if include_headers and not (row.text.casefold().startswith("[offset:") and document.offset_ms):
                output.append(row.text)
            continue
        if row.timestamp_ms is None:
            output.append(row.text)
            continue
        stamp_ms = row.timestamp_ms
        mins, rem = divmod(stamp_ms, 60_000)
        precision = max(0, min(3, row.source_precision))
        if precision == 0:
            stamp = f"{mins:02d}:{rem // 1000:02d}"
        else:
            # Construct explicitly: seconds width 2, then source precision.
            fraction = f"{rem % 1000:03d}"[:precision]
            stamp = f"{mins:02d}:{rem // 1000:02d}.{fraction}"
        output.append(f"[{stamp}]{row.text}")
    return "\n".join(output)


def validate_lrc(text: str, *, duration: float | None = None) -> tuple[bool, tuple[str, ...]]:
    if len(text) > MAX_LYRICS_CHARS:
        return False, (f"lyrics payload exceeds {MAX_LYRICS_CHARS} character safety limit",)
    document = parse_lrc(text)
    reasons = list(document.warnings)
    timed_rows = [row for row in document.lines if row.timestamp_ms is not None and row.text.strip()]
    if not timed_rows:
        reasons.append("no valid line timestamps")
    # A single timed line can be a legitimate short recording, so report weak
    # evidence rather than reject it. Candidate selection combines other signals.
    times = [row.timestamp_ms + document.offset_ms for row in document.lines if row.timestamp_ms is not None]
    if any(value < 0 for value in times):
        reasons.append("negative timestamp after offset")
    if any(value > 86_400_000 for value in times):
        reasons.append("timestamp exceeds 24-hour LRC bound")
    regressions = sum(1 for previous, current in pairwise(times) if current < previous)
    if regressions:
        reasons.append(f"{regressions} timestamp regression(s)")
    # Simultaneous, different vocal lines are legitimate (duets and backing
    # vocals). Only flag highly duplicated timelines, and do not reject a few
    # shared timestamps as malformed.
    distinct_times = len(set(times))
    if len(times) >= 20 and distinct_times / len(times) < 0.5:
        reasons.append("excessive duplicate timestamps")
    if duration is not None and duration > 0 and times:
        tolerance = max(5_000, int(duration * 0.03 * 1000))
        if max(times) > duration * 1000 + tolerance:
            reasons.append("timestamps exceed track duration")
    if len(times) > 20_000:
        reasons.append("implausible timestamp count")
    lyric_source_lines = [
        line
        for line in document.raw.split("\n")
        if line.strip() and not META_RE.fullmatch(line.strip()) and not _SECTION_RE.fullmatch(line.strip())
    ]
    timestamped_source_lines = sum(1 for line in lyric_source_lines if TIMESTAMP_RE.match(line.lstrip()))
    if len(lyric_source_lines) >= 4 and timestamped_source_lines / len(lyric_source_lines) < 0.75:
        reasons.append("synchronization is partial (fewer than 75% of lyric lines are timestamped)")
    return not reasons, tuple(reasons)


def validate_enhanced_lrc(text: str, *, duration: float | None = None) -> tuple[bool, tuple[str, ...]]:
    """Validate word-level timing while preserving the source ELRC string."""
    if len(text) > MAX_LYRICS_CHARS:
        return False, (f"lyrics payload exceeds {MAX_LYRICS_CHARS} character safety limit",)
    source = conservative_text(text)
    marks: list[int] = []
    offset_document = parse_lrc(source)
    timed_lines = 0
    lyric_lines = 0
    malformed = 0
    for line in source.split("\n"):
        if not line.strip() or META_RE.fullmatch(line.strip()) or _SECTION_RE.fullmatch(line.strip()):
            continue
        lyric_lines += 1
        if TIMESTAMP_RE.match(line.lstrip()):
            timed_lines += 1
        recognized = 0
        for match in WORD_TIMESTAMP_RE.finditer(line):
            recognized += 1
            try:
                millis, _precision = _time_parts(match)
            except ValueError:
                malformed += 1
                continue
            marks.append(millis + offset_document.offset_ms)
        malformed += len(re.findall(r"<\s*[-+]?\d[^>]*>", line)) - recognized
    reasons: list[str] = []
    reasons.extend(warning for warning in offset_document.warnings if "offset" in warning)
    if any(value < 0 for value in marks):
        reasons.append("negative word timestamp after offset")
    if any(value > 86_400_000 for value in marks):
        reasons.append("word timestamp exceeds 24-hour bound")
    if malformed:
        reasons.append(f"{malformed} malformed word timestamp(s)")
    if not marks:
        reasons.append("no valid word timestamps")
    if any(current < previous for previous, current in pairwise(marks)):
        reasons.append("word timestamps regress")
    if duration is not None and duration > 0 and marks:
        tolerance_ms = max(5_000, int(duration * 0.03 * 1000))
        if max(marks) > duration * 1000 + tolerance_ms:
            reasons.append("word timestamps exceed track duration")
    if lyric_lines >= 4 and timed_lines / lyric_lines < 0.75:
        reasons.append("enhanced synchronization is partial")
    if len(marks) > 100_000:
        reasons.append("implausible word timestamp count")
    return not reasons, tuple(reasons)


def enhanced_to_lrc(text: str) -> str:
    """Derive line LRC from validated word timing without changing lyric text."""
    output: list[str] = []
    for line in conservative_text(text).split("\n"):
        indentation = line[: len(line) - len(line.lstrip())]
        body = line[len(indentation) :]
        line_stamps: list[str] = []
        position = 0
        while match := TIMESTAMP_RE.match(body, position):
            line_stamps.append(match.group(0))
            position = match.end()
        word_stamps = list(WORD_TIMESTAMP_RE.finditer(body))
        if not line_stamps and word_stamps:
            line_stamps.append("[" + word_stamps[0].group(0)[1:-1] + "]")
        lyric_text = WORD_TIMESTAMP_RE.sub("", body[position:])
        output.append(indentation + "".join(line_stamps) + lyric_text)
    return "\n".join(output)


def lrc_to_plain(text: str) -> str:
    """Remove timing/header transport only; preserve labels, repeats and layout."""
    document = parse_lrc(text)
    output: list[str] = []
    for row in document.lines:
        if row.kind == "metadata":
            continue
        output.append(WORD_TIMESTAMP_RE.sub("", row.text))
    return "\n".join(output)


_VERSION_DELIMITER_RE = re.compile(r"\s+(?:[-–—]|/)\s+")
_VERSION_SUFFIX_RE = re.compile(
    r"(?P<suffix>(?:live(?:\s+(?:at|in)\s+.+)?|concert(?:\s+version)?|unplugged|"
    r"remaster(?:ed)?(?:\s+\d{4})?|(?:radio|single|album)\s+(?:edit|version)|"
    r"extended(?:\s+(?:mix|version))?|(?:club|radio|original)\s+mix|remix|"
    r"acoustic(?:\s+version)?|demo(?:\s+version)?|instrumental(?:\s+version)?|"
    r"explicit|clean(?:\s+version)?|karaoke|(?:alternate|alternative)\s+(?:take|version|mix)|"
    r"re-?recorded|mono(?:phonic)?|stereo(?:phonic)?|a\s*capella))\s*$",
    re.IGNORECASE,
)


def version_signature(title: str, explicit_version: str = "") -> frozenset[str]:
    """Read version markers only from explicit version fields or delimited suffixes.

    Words inside the recording title (for example ``Live Forever``) do not
    become recording-version metadata by accident. An explicit version phrase
    at the unwrapped end of a title is also recognized for catalogs that omit
    parentheses and separators (``Song Radio Edit``).
    """
    segments = [match.group(1) for match in re.finditer(r"[([{]([^)]*?)[)\]}]", title)]
    segments.extend(_VERSION_DELIMITER_RE.split(title)[1:])
    unwrapped = re.sub(r"[([{].*?[)\]}]", " ", title).strip()
    suffix = _VERSION_SUFFIX_RE.search(unwrapped)
    if suffix and unwrapped[: suffix.start()].strip():
        segments.append(suffix.group("suffix"))
    if explicit_version.strip():
        segments.append(explicit_version)
    return frozenset(
        name for name, pattern in _VERSION_PATTERNS if any(pattern.search(segment) for segment in segments)
    )


def _version_tokens(title: str, explicit_version: str = "") -> frozenset[str]:
    return version_signature(title, explicit_version)


def _text_quality(text: str) -> float:
    plain = lrc_to_plain(text) if TIMESTAMP_RE.search(text) else text
    lines = [line for line in plain.splitlines() if line.strip() and not _SECTION_RE.fullmatch(line.strip())]
    if not lines:
        return 0.0
    chars = sum(len(line.strip()) for line in lines)
    # Repeated chorus lines remain valid; only obvious payload noise is penalized.
    score = min(100.0, 35.0 + min(chars, 500) / 10 + min(len(lines), 30) * 0.7)
    if len(plain) > 100_000:
        score -= 40
    if "lyrics provided by" in plain.casefold() or "you might also like" in plain.casefold():
        score -= 30
    return max(0.0, score)


def _source_quality(candidate: LyricsCandidate) -> float:
    configured = candidate.metadata.get("source_confidence")
    if isinstance(configured, (int, float)):
        return max(0.0, min(100.0, float(configured)))
    if candidate.generated:
        return 35.0
    if candidate.metadata.get("adapter_validated"):
        return 85.0
    if candidate.provider_track_id and candidate.metadata.get("url"):
        return 60.0
    if candidate.provider_track_id:
        return 58.0
    return 55.0


def assess_candidate(
    candidate: LyricsCandidate,
    *,
    title: str,
    artist: str,
    album: str = "",
    duration: float | None = None,
    language: str = "",
    isrc: str = "",
    recording_id: str = "",
) -> LyricsAssessment:
    reasons: list[str] = []
    warnings: list[str] = list(candidate.warnings)
    text = conservative_text(candidate.text)
    if not text.strip() or _text_quality(text) < 20:
        return LyricsAssessment(candidate, 0, False, ("lyrics contain no usable lyric text",))
    very_short = len(text.strip()) < 10
    if very_short:
        # A brief refrain, interlude, or vocal sample can be authentic. Keep
        # it assessable, but make its low completeness confidence visible and
        # let identity/quality gates prevent it from winning by text length.
        warnings.append("very short non-empty lyrics; completeness is uncertain")

    if candidate.isrc and isrc and re.sub(r"[-\s]", "", candidate.isrc).upper() != re.sub(r"[-\s]", "", isrc).upper():
        return LyricsAssessment(candidate, 0, False, ("ISRC conflict",))
    if candidate.recording_id and recording_id and candidate.recording_id.casefold() != recording_id.casefold():
        return LyricsAssessment(candidate, 0, False, ("MusicBrainz recording conflict",))

    stable_match = bool((candidate.isrc and isrc) or (candidate.recording_id and recording_id))
    adapter_match = bool(candidate.metadata.get("adapter_validated"))
    title_sim = text_similarity(candidate.title, title) if candidate.title else 0.0
    artist_sim = text_similarity(candidate.artist, artist) if candidate.artist else 0.0
    candidate_versions = _version_tokens(candidate.title, candidate.version)
    requested_versions = _version_tokens(title)
    semantic_versions = {
        "live",
        "remix",
        "radio_edit",
        "extended",
        "acoustic",
        "demo",
        "instrumental",
        "explicit",
        "clean",
        "original",
    }
    version_confidence = 100.0
    differing_versions = (candidate_versions ^ requested_versions) & semantic_versions
    # Performance, edit, and clean/explicit differences change recording
    # identity. Remaster labels are tracked separately below because the words
    # are usually unchanged while fades and sync timing may differ.
    if differing_versions and not stable_match:
        return LyricsAssessment(
            candidate, 0, False, ("version-defining title conflict: " + ", ".join(sorted(differing_versions)),)
        )
    if differing_versions:
        version_confidence = 65.0
        warnings.append("stable recording ID overrides differing version labels")
    if "remastered" in (candidate_versions ^ requested_versions) and not stable_match:
        # Mastering variants are often lyric-equivalent but can shift fades or
        # sync timing. Keep the candidate eligible while making the distinction
        # visible and subordinate to exact version metadata.
        version_confidence = min(version_confidence, 78.0)
        warnings.append("remaster label differs; verify album and synchronized timing")

    duration_delta: float | None = None
    album_similarity = text_similarity(candidate.album, album) if candidate.album and album else 0.0
    if duration and candidate.duration and candidate.duration > 0:
        duration_delta = abs(duration - candidate.duration)

    if stable_match:
        match_confidence = 100.0
    elif (
        title_sim >= 0.92
        and artist_sim >= 0.85
        and (
            album_similarity >= 0.80
            or (duration_delta is not None and duration is not None and duration_delta <= max(8.0, duration * 0.04))
        )
    ):
        match_confidence = 90.0
    elif adapter_match and title_sim >= 0.82 and candidate.artist and artist_sim >= 0.78:
        match_confidence = 82.0
    elif (
        title_sim >= 0.86
        and artist_sim >= 0.80
        and (
            album_similarity >= 0.80
            or (duration_delta is not None and duration is not None and duration_delta <= max(8.0, duration * 0.04))
        )
    ):
        match_confidence = 80.0
    else:
        return LyricsAssessment(
            candidate,
            0,
            False,
            ("insufficient provider-supplied recording identity",),
            match_confidence=max(title_sim, artist_sim) * 100,
            version_confidence=version_confidence,
        )
    if candidate.artist and artist_sim < 0.70 and not stable_match:
        return LyricsAssessment(candidate, 0, False, ("artist mismatch",), match_confidence, version_confidence)

    if candidate.album and album:
        if album_similarity >= 0.90 and not stable_match:
            # Album agreement is useful recording evidence, but only a modest
            # boost: the same recording may legitimately appear on a
            # compilation or reissue.
            match_confidence = min(100.0, match_confidence + 4.0)
        elif album_similarity < 0.45 and not stable_match:
            warnings.append("album differs")
            # Do not reject an otherwise good recording candidate just because
            # lyrics were sourced from a compilation. Keep the conflict visible
            # and let duration/version/text evidence dominate.
            match_confidence = max(78.0, match_confidence - 4.0)
    if duration and duration_delta is not None:
        delta = duration_delta
        if candidate.synced:
            strong = max(3.0, duration * 0.02)
            plausible = max(8.0, duration * 0.04)
        else:
            strong = max(8.0, duration * 0.04)
            plausible = max(15.0, duration * 0.08)
        if delta > plausible and not stable_match:
            return LyricsAssessment(
                candidate,
                0,
                False,
                ("duration mismatch suggests another recording/version",),
                match_confidence,
                version_confidence,
            )
        if delta > strong:
            warnings.append(f"duration differs by {delta:.1f}s")
            match_confidence = min(match_confidence, 80.0)
        else:
            match_confidence = min(100.0, match_confidence + 5.0)

    target_language = language.casefold().replace("_", "-").split("-", 1)[0]
    candidate_language = candidate.language.casefold().replace("_", "-").split("-", 1)[0]
    if (
        target_language
        and candidate_language
        and target_language != candidate_language
        and not (candidate.translation or candidate.romanization)
    ):
        return LyricsAssessment(
            candidate,
            0,
            False,
            ("candidate language conflicts with the requested original language",),
            match_confidence,
            version_confidence,
        )
    requested_language_tag = language.casefold().replace("_", "-")
    candidate_language_tag = candidate.language.casefold().replace("_", "-")
    if (
        target_language
        and candidate_language
        and target_language == candidate_language
        and requested_language_tag != candidate_language_tag
    ):
        warnings.append("language tag matched at language level; regional/script detail retained")
    text_quality = _text_quality(text)
    if very_short:
        text_quality = max(30.0, text_quality - 4.0)
    readable = lrc_to_plain(text) if candidate.synced or TIMESTAMP_RE.search(text) else text
    visible_chars = sum(len(line.strip()) for line in readable.splitlines() if line.strip())
    if (duration and duration >= 300 and visible_chars < 100) or (duration and duration >= 180 and visible_chars < 40):
        # A soft warning only: long instrumentals and short vocal interludes
        # are valid. Other provider candidates can strengthen or contradict it.
        text_quality = max(0.0, text_quality - 8.0)
        warnings.append("text is unusually short for track duration; possible partial lyrics")
    sync_quality = 0.0
    if candidate.synced:
        valid_sync, sync_reasons = validate_lrc(text, duration=duration)
        if not valid_sync:
            reasons.extend(sync_reasons)
        else:
            document = parse_lrc(text)
            timed = [row for row in document.lines if row.timestamp_ms is not None and row.text.strip()]
            total = [row for row in document.lines if row.text.strip() and row.kind == "lyric"]
            ratio = len(timed) / max(1, len(total))
            sync_quality = min(100.0, 60.0 + ratio * 40.0)
            if len(timed) < 2:
                sync_quality = min(sync_quality, 60.0)
                warnings.append("only one timed lyric row")
            if len(timed) >= 2:
                stamps = [row.timestamp_ms for row in timed if row.timestamp_ms is not None]
                if len(stamps) > 1:
                    span = max(stamps) - min(stamps)
                    if duration and span < duration * 1000 * 0.2:
                        warnings.append("lyrics cover a short portion of track duration")
                        sync_quality -= 10
                    if duration and stamps[0] > max(15_000, duration * 1000 * 0.2):
                        warnings.append("first lyric timestamp is late; an instrumental intro may explain it")
                    gap_limit = max(90_000, duration * 1000 * 0.35) if duration else 90_000
                    if any(current - previous > gap_limit for previous, current in pairwise(stamps)):
                        warnings.append("large timestamp gap may represent an instrumental section")
        if not valid_sync:
            # Malformed synchronization does not disqualify valid text. The
            # candidate remains usable as unsynchronized text.
            text_quality = max(text_quality, _text_quality(lrc_to_plain(text)))
            sync_quality = 0.0

    source_confidence = _source_quality(candidate)
    # Match tier dominates. Text quality beats timing quality; source quality
    # is only a weak prior. This explicitly permits accurate plain lyrics to win.
    score = match_confidence * 0.55 + text_quality * 0.32 + sync_quality * 0.08 + source_confidence * 0.05
    if candidate.generated:
        score -= 8
        # Generated transcription/timing is useful as a review artifact, but
        # does not automatically qualify as canonical lyrics.
        score = min(score, 74.0)
        warnings.append("generated lyrics or timing")
    valid = match_confidence >= 78 and version_confidence >= 60 and text_quality >= 30
    if candidate.synced and sync_quality == 0:
        warnings.extend(reasons)
    return LyricsAssessment(
        candidate,
        score,
        valid,
        tuple(dict.fromkeys(reasons)),
        match_confidence,
        version_confidence,
        text_quality,
        max(0.0, sync_quality),
        source_confidence,
        tuple(dict.fromkeys(warnings)),
    )


def select_best(
    candidates: list[LyricsCandidate],
    *,
    title: str,
    artist: str,
    album: str,
    duration: float | None,
    language: str = "",
    isrc: str = "",
    recording_id: str = "",
) -> LyricsAssessment | None:
    assessed = [
        assess_candidate(
            c,
            title=title,
            artist=artist,
            album=album,
            duration=duration,
            language=language,
            isrc=isrc,
            recording_id=recording_id,
        )
        for c in candidates
    ]
    eligible = [a for a in assessed if a.valid]
    if not eligible:
        return None
    # Conservative cross-provider completeness signal. Agreement is not
    # counted as independent votes; one much fuller, same-version candidate
    # can only apply a small warning/penalty to a substantially shorter one.
    references = sorted(
        eligible,
        key=lambda a: (a.match_confidence, a.text_quality, a.candidate.provider.casefold()),
        reverse=True,
    )
    adjusted: list[LyricsAssessment] = []
    for assessment in eligible:
        current_candidate = assessment.candidate
        candidate_text = (
            lrc_to_plain(assessment.candidate.text) if assessment.candidate.synced else assessment.candidate.text
        )
        candidate_words = normalize_lookup(candidate_text)
        candidate_version = _version_tokens(assessment.candidate.title, assessment.candidate.version)
        warnings = list(assessment.warnings)
        score = assessment.score
        text_quality = assessment.text_quality
        for reference in references:
            if reference.candidate is assessment.candidate:
                continue
            if reference.match_confidence < assessment.match_confidence or reference.version_confidence < 80:
                continue
            ref_version = _version_tokens(reference.candidate.title, reference.candidate.version)
            if candidate_version != ref_version:
                continue
            if (
                current_candidate.duration
                and reference.candidate.duration
                and abs(current_candidate.duration - reference.candidate.duration)
                > max(8.0, current_candidate.duration * 0.04)
            ):
                continue
            reference_text = (
                lrc_to_plain(reference.candidate.text) if reference.candidate.synced else reference.candidate.text
            )
            reference_words = normalize_lookup(reference_text)
            if (
                len(reference_words) >= 80
                and len(candidate_words) < len(reference_words) * 0.60
                and text_similarity(candidate_text, reference_text) < 0.30
            ):
                warnings.append("substantially shorter and different text than a strong same-version candidate")
                text_quality = max(0.0, text_quality - 8.0)
                score -= 2.5
                break
        adjusted.append(
            LyricsAssessment(
                assessment.candidate,
                score,
                assessment.valid,
                assessment.reasons,
                assessment.match_confidence,
                assessment.version_confidence,
                text_quality,
                assessment.sync_quality,
                assessment.source_confidence,
                tuple(dict.fromkeys(warnings)),
            )
        )
    eligible = adjusted
    # Collapse exact/near duplicate text for selection only. Keep all originals
    # in their provider result and sidecars; duplicates never act as consensus
    # votes and never cause stored text to be rewritten.
    deduplicated: list[LyricsAssessment] = []
    for assessment in sorted(
        eligible,
        key=lambda a: (
            a.match_confidence,
            a.version_confidence,
            a.text_quality,
            a.sync_quality,
            a.source_confidence,
            a.candidate.provider.casefold(),
            a.candidate.provider_track_id.casefold(),
        ),
        reverse=True,
    ):
        text = lrc_to_plain(assessment.candidate.text) if assessment.candidate.synced else assessment.candidate.text
        comparison = normalize_lookup(text)
        duplicate_index: int | None = None
        if len(comparison) >= 40:
            for index, kept in enumerate(deduplicated):
                kept_text = lrc_to_plain(kept.candidate.text) if kept.candidate.synced else kept.candidate.text
                kept_comparison = normalize_lookup(kept_text)
                if len(kept_comparison) < 40:
                    continue
                same_version = _version_tokens(
                    assessment.candidate.title, assessment.candidate.version
                ) == _version_tokens(kept.candidate.title, kept.candidate.version)
                if not same_version:
                    continue
                if (
                    assessment.candidate.duration
                    and kept.candidate.duration
                    and abs(assessment.candidate.duration - kept.candidate.duration)
                    > max(8.0, assessment.candidate.duration * 0.04)
                ):
                    continue
                if comparison == kept_comparison or text_similarity(text, kept_text) >= 0.97:
                    duplicate_index = index
                    break
        if duplicate_index is None:
            deduplicated.append(assessment)
        else:
            kept = deduplicated[duplicate_index]
            providers = list(kept.warnings)
            providers.append(f"duplicate or near-duplicate text also returned by {assessment.candidate.provider}")
            deduplicated[duplicate_index] = LyricsAssessment(
                kept.candidate,
                kept.score,
                kept.valid,
                kept.reasons,
                kept.match_confidence,
                kept.version_confidence,
                kept.text_quality,
                kept.sync_quality,
                kept.source_confidence,
                tuple(dict.fromkeys(providers)),
            )
    eligible = deduplicated
    # Stable ordering: identity and version tier, then text, then sync, then
    # weak source prior; provider name and canonical text hash settle true ties.
    eligible.sort(
        key=lambda a: (
            a.match_confidence,
            a.version_confidence,
            a.text_quality,
            a.sync_quality,
            a.source_confidence,
            a.candidate.provider.casefold(),
            _comparison_text(a.candidate.text),
            a.candidate.provider_track_id.casefold(),
            _comparison_text(a.candidate.title),
            _comparison_text(a.candidate.artist),
            _comparison_text(a.candidate.album),
            a.candidate.language.casefold(),
            a.candidate.duration or 0.0,
            a.candidate.raw_lyrics,
            a.candidate.text,
        ),
        reverse=True,
    )
    return eligible[0]
