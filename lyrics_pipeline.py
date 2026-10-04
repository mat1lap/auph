#!/usr/bin/env python3
"""Safe orchestration around provider adapters and immutable local lyrics."""

from __future__ import annotations

import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lyrics_quality import (
    MAX_LYRICS_CHARS,
    TIMESTAMP_RE,
    WORD_TIMESTAMP_RE,
    LyricsAssessment,
    LyricsCandidate,
    assess_candidate,
    conservative_text,
    enhanced_to_lrc,
    lrc_to_plain,
    normalize_lookup,
    parse_lrc,
    select_best,
    text_similarity,
    validate_enhanced_lrc,
    validate_lrc,
)
from pipeline_config import load_config
from provider_cache import PARSER_VERSION

_SECTION_LABEL_RE = re.compile(
    r"^\s*\[(?:verse|chorus|bridge|hook|intro|outro|refrain|pre-?chorus|post-?chorus|"
    r"instrumental|guitar\s+solo|solo|spoken|interlude|"
    r"куплет|припев|бридж|интро|аутро|вступление|кода|предприпев|постприпев|инструментал|"
    r"前奏|間奏|後奏|イントロ|アウトロ|サビ|aメロ|bメロ|cメロ|主歌|副歌|间奏|尾奏)(?:\s+\d+)?\]\s*:?\s*$",
    re.IGNORECASE,
)
_SECTION_PREFIX_RE = re.compile(
    r"^\s*\[(?:verse(?:\s+\d+)?|chorus|bridge|hook|intro|outro|refrain|pre-?chorus|post-?chorus|"
    r"instrumental|guitar\s+solo|solo|spoken|interlude|"
    r"куплет(?:\s+\d+)?|припев(?:\s+\d+)?|бридж|интро|аутро|вступление|кода|предприпев|постприпев|"
    r"инструментал|前奏|間奏|後奏|イントロ|アウトロ|サビ|aメロ|bメロ|cメロ|主歌|副歌|间奏|尾奏)\]\s*:?\s*",
    re.IGNORECASE,
)


def display_lyrics(text: str, *, synced: bool = False) -> str:
    """Make a playback derivative without changing preserved provider evidence."""
    output: list[str] = []
    for line in conservative_text(text).splitlines():
        timestamp_prefix = ""
        position = 0
        # Some otherwise valid LRC has a structural label immediately after
        # its timestamp (e.g. ``[00:01.00][Verse 1]line``). Recognize that
        # prefix line by line; whole-document validation can fail because one
        # malformed line exists and must not make labels leak into LYRICS.
        while match := TIMESTAMP_RE.match(line, position):
            position = match.end()
        if synced or position:
            timestamp_prefix = line[:position]
            body = line[position:]
        else:
            body = line
        trimmed = body.strip()
        if _SECTION_LABEL_RE.fullmatch(trimmed):
            continue
        prefix = _SECTION_PREFIX_RE.match(body)
        if prefix:
            body = body[prefix.end() :]
            if not body.strip():
                continue
        output.append(timestamp_prefix + body)
    return "\n".join(output)


@dataclass(frozen=True)
class LyricsResolution:
    source: str
    status: str
    paths: Mapping[str, Path]
    assessment: LyricsAssessment | None = None
    local: bool = False
    errors: tuple[str, ...] = ()
    needs_remote_lookup: bool = False
    has_word_sync: bool = False


def _ttml_has_word_sync(text: str) -> bool:
    """Use the TTML provider parser's timing semantics for local archives."""
    try:
        from lyrics_utils import parse_ttml

        return parse_ttml(text)[2]
    except (ImportError, RecursionError, ValueError, ET.ParseError):
        return False


def _read(path: Path) -> str:
    return conservative_text(path.read_text(encoding="utf-8", errors="strict"))


def _valid_ttml(text: str) -> bool:
    if len(text) > 5_000_000 or re.search(r"<!DOCTYPE|<!ENTITY", text, re.IGNORECASE):
        return False
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return False
    return root.tag.rsplit("}", 1)[-1].casefold() == "tt" and bool("".join(root.itertext()).strip())


def _sync_text_matches_plain(synced: str, plain: str) -> bool:
    """Conservatively detect a synced transcript missing a large text section.

    This comparison-only fold never changes either representation. Providers
    sometimes expose complete plain text beside truncated line timing; reject
    the timing in that case while retaining the readable original text.
    """
    sync_text = normalize_lookup(lrc_to_plain(synced))
    plain_text = normalize_lookup(plain)
    if len(sync_text) < 40 or len(plain_text) < 40:
        return True
    length_ratio = max(len(sync_text), len(plain_text)) / max(1, min(len(sync_text), len(plain_text)))
    similarity = text_similarity(sync_text, plain_text)
    return not (length_ratio >= 1.2 and similarity < 0.82)


def inspect_local(base: Path, embedded: Mapping[str, Sequence[str]] | None, duration: float) -> LyricsResolution | None:
    paths = {
        "plain": base.with_suffix(".txt"),
        "synced": base.with_suffix(".lrc"),
        "enhanced": base.with_name(base.stem + ".elrc"),
        "ttml": base.with_suffix(".ttml"),
        "translation": base.with_name(base.stem + ".translation.txt"),
        "romanization_lrc": base.with_name(base.stem + ".romaji.lrc"),
        "romanization_plain": base.with_name(base.stem + ".romaji.txt"),
    }
    valid_paths: dict[str, Path] = {}
    errors: list[str] = []
    local_word_sync = False
    for kind, path in paths.items():
        if not path.is_file() or path.stat().st_size == 0:
            continue
        try:
            text = _read(path)
        except (OSError, UnicodeError) as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        valid = len(text) >= 20
        if kind in {"synced", "enhanced", "romanization_lrc"}:
            valid, why = validate_lrc(text, duration=duration)
            if not valid:
                errors.append(f"{path.name}: {'; '.join(why)}")
            elif kind == "enhanced" and WORD_TIMESTAMP_RE.search(text):
                valid, why = validate_enhanced_lrc(text, duration=duration)
                if not valid:
                    errors.append(f"{path.name}: {'; '.join(why)}")
                else:
                    local_word_sync = True
        elif kind == "ttml":
            valid = _valid_ttml(text)
            if not valid:
                errors.append(f"{path.name}: malformed TTML")
            else:
                local_word_sync = local_word_sync or _ttml_has_word_sync(text)
        if valid:
            output_kind = "romanization" if kind.startswith("romanization_") else kind
            valid_paths[output_kind] = path

    embedded = embedded or {}
    embedded_valid = False
    embedded_synced = False
    for field in ("UNSYNCEDLYRICS", "LYRICS"):
        for value in embedded.get(field, []):
            text = conservative_text(value)
            if len(text.strip()) >= 10:
                embedded_valid = True
            if "[" in text and validate_lrc(text, duration=duration)[0]:
                embedded_synced = True
    for value in embedded.get("SYNCEDLYRICS", []):
        if validate_lrc(value, duration=duration)[0]:
            embedded_valid = True
            embedded_synced = True
    for value in embedded.get("SYNCELYRICS", []):
        if validate_lrc(value, duration=duration)[0]:
            embedded_valid = True
            embedded_synced = True
            local_word_sync = local_word_sync or bool(WORD_TIMESTAMP_RE.search(value))
    for value in embedded.get("SYNCEDLYRICS", []):
        local_word_sync = local_word_sync or bool(WORD_TIMESTAMP_RE.search(value))
    for field in ("SYNCEDLYRICS_ENHANCED", "LYRICS_TTML"):
        for value in embedded.get(field, []):
            if WORD_TIMESTAMP_RE.search(value):
                valid_word_sync, _ = validate_enhanced_lrc(value, duration=duration)
                local_word_sync = local_word_sync or valid_word_sync
            elif field == "LYRICS_TTML" and _valid_ttml(value):
                local_word_sync = local_word_sync or _ttml_has_word_sync(value)
    sidecar_synced = any(kind in valid_paths for kind in ("synced", "enhanced", "ttml"))

    if valid_paths or embedded_valid:
        source = "Local sidecar" if valid_paths else "Embedded Vorbis comment"
        return LyricsResolution(
            source,
            "PRESERVED",
            valid_paths,
            local=True,
            errors=tuple(errors),
            # A plain local transcript is protected while lookup may add a
            # missing synchronized representation. A synced local document is
            # sufficient to skip remote retrieval.
            needs_remote_lookup=not (sidecar_synced or embedded_synced),
            has_word_sync=local_word_sync,
        )
    return None


def provider_candidate(
    result: Mapping[str, Any], *, fallback_provider: str, duration: float | None = None
) -> LyricsCandidate | None:
    lyrics_fields = ("syncedLyrics", "plainLyrics", "translationLyrics", "romajiLyrics", "providerRaw", "rawLyrics")
    if any(len(str(result.get(field) or "")) > MAX_LYRICS_CHARS for field in lyrics_fields):
        return None
    synced = conservative_text(str(result.get("syncedLyrics") or ""))
    enhanced = conservative_text(str(result.get("enhancedLyrics") or ""))
    plain = conservative_text(str(result.get("plainLyrics") or ""))
    translation = conservative_text(str(result.get("translationLyrics") or ""))
    romanization = conservative_text(str(result.get("romajiLyrics") or ""))
    try:
        candidate_duration = float(result["duration"]) if result.get("duration") is not None else None
    except (TypeError, ValueError):
        candidate_duration = None
    if candidate_duration is not None and (not math.isfinite(candidate_duration) or candidate_duration <= 0):
        candidate_duration = None
    sync_duration_mismatch = bool(
        duration
        and candidate_duration
        and candidate_duration > 0
        and abs(duration - candidate_duration) > max(8.0, duration * 0.04)
    )
    valid_enhanced, enhanced_reasons = validate_enhanced_lrc(enhanced, duration=duration) if enhanced else (False, ())
    word_timing_source = enhanced or (synced if WORD_TIMESTAMP_RE.search(synced) else "")
    valid_word_sync = bool(
        word_timing_source
        and not sync_duration_mismatch
        and validate_enhanced_lrc(word_timing_source, duration=duration)[0]
    )
    enhanced_derived = False
    if not synced and valid_enhanced and not sync_duration_mismatch:
        synced = enhanced_to_lrc(enhanced)
        enhanced_derived = True
    valid_sync = bool(synced and not sync_duration_mismatch and validate_lrc(synced, duration=duration)[0])
    sync_text_mismatch = bool(valid_sync and plain and not _sync_text_matches_plain(synced, plain))
    if sync_text_mismatch:
        valid_sync = False
    explicit_translation = bool(result.get("translation"))
    explicit_romanization = bool(result.get("romanization"))
    alternate_only = not plain and not valid_sync
    is_romanization = explicit_romanization or (alternate_only and bool(romanization) and not explicit_translation)
    is_translation = explicit_translation or (alternate_only and bool(translation) and not is_romanization)
    if alternate_only and is_romanization:
        # One candidate represents one text form. Other available forms remain
        # attached to the raw result and are serialized to their own fields.
        is_translation = False
    text = (
        synced
        if valid_sync
        else (
            plain
            or (lrc_to_plain(synced) if synced else "")
            or (romanization if is_romanization else "")
            or (translation if is_translation else "")
            or translation
            or romanization
        )
    )
    if not text:
        return None
    provider = str(result.get("source") or fallback_provider)
    # Keep the complete adapter payload so raw source representations and
    # provider IDs survive selection/materialisation.  The normalized display
    # form is deliberately a derivative, not a replacement for evidence.
    payload = dict(result)
    payload["requested_duration"] = duration
    # Treat provider flags as claims. Word timing is exposed only when the
    # actual timestamp syntax validates against the target recording.
    payload["has_word_sync"] = valid_word_sync and not sync_text_mismatch
    payload["lyrics_form"] = "translation" if is_translation else ("romanization" if is_romanization else "original")
    raw_transformations = result.get("transformations", ())
    transformations = (
        tuple(str(item) for item in raw_transformations if item)
        if isinstance(raw_transformations, (list, tuple))
        else ()
    )
    transformations += tuple(
        label
        for label, changed in (
            (
                "leading UTF-8 BOM removed",
                str(result.get("providerRaw") or result.get("rawLyrics") or "").startswith("\ufeff"),
            ),
            ("line endings normalized", "\r" in str(result.get("syncedLyrics") or result.get("plainLyrics") or "")),
            (
                "provider format parsed into LRC",
                bool(
                    result.get("syncedLyrics")
                    and result.get("format")
                    in {"ttml", "qrc", "yrc", "krc", "richsync", "elrc", "ultrastar", "clonehero-chart"}
                ),
            ),
            ("line LRC derived from validated word timestamps", enhanced_derived),
        )
        if changed
    )
    raw_warnings = result.get("validationWarnings", ())
    warnings = tuple(str(item) for item in raw_warnings if item) if isinstance(raw_warnings, (list, tuple)) else ()
    if synced and not valid_sync:
        warnings += ("synchronized representation failed validation; considering its text as unsynchronized",)
    if sync_text_mismatch:
        warnings += ("synchronized text materially disagrees with provider plain lyrics; timing rejected",)
    if sync_duration_mismatch:
        warnings += ("provider duration materially differs from local track; timing rejected",)
    if enhanced and not valid_enhanced:
        warnings += tuple(f"enhanced timing rejected: {reason}" for reason in enhanced_reasons)
    return LyricsCandidate(
        provider=provider,
        text=text,
        synced=valid_sync,
        # A request is not provider evidence.  In particular, an adapter that
        # merely echoes its query must not receive an artificial exact-match
        # score and overwrite a better local transcript.
        title=str(result.get("trackName") or result.get("song") or result.get("title") or ""),
        artist=str(result.get("artistName") or result.get("artist") or ""),
        album=str(result.get("albumName") or result.get("album") or ""),
        duration=candidate_duration,
        language=str(result.get("language") or ""),
        isrc=str(result.get("isrc") or ""),
        recording_id=str(result.get("recording_id") or result.get("musicbrainz_recording_id") or ""),
        attribution=str(result.get("attribution") or provider),
        generated=bool(result.get("generated") or result.get("neural")),
        translation=is_translation,
        romanization=is_romanization,
        metadata={
            "result": payload,
            "adapter_validated": bool(result.get("identity_validated")),
            "provider_item_id": str(result.get("id") or result.get("provider_item_id") or ""),
            "url": str(result.get("url") or result.get("canonical_url") or ""),
            "raw_response_hash": str(result.get("raw_response_hash") or ""),
            "source_confidence": result.get("source_confidence"),
            "sync_text_mismatch": sync_text_mismatch,
            "sync_duration_mismatch": sync_duration_mismatch,
            "enhanced_sync_valid": valid_enhanced,
            "word_sync_valid": valid_word_sync,
        },
        provider_track_id=str(
            result.get("provider_track_id") or result.get("id") or result.get("provider_item_id") or ""
        ),
        album_artist=str(result.get("albumArtist") or result.get("album_artist") or ""),
        version=str(result.get("version") or result.get("versionName") or ""),
        raw_lyrics=str(result.get("providerRaw") or result.get("rawLyrics") or ""),
        plain_lyrics=plain or (lrc_to_plain(synced) if synced else ""),
        synced_lyrics=synced if valid_sync else "",
        parsed=parse_lrc(synced, language=str(result.get("language") or "")) if valid_sync else None,
        transformations=transformations,
        warnings=warnings,
    )


def _expand_results(results: Iterable[Mapping[str, Any] | None]) -> list[Mapping[str, Any]]:
    expanded: list[Mapping[str, Any]] = []
    pending = [result for result in results if result]
    seen: set[int] = set()
    while pending and len(expanded) < 1000:
        result = pending.pop(0)
        if id(result) in seen:
            continue
        seen.add(id(result))
        expanded.append(result)
        alternatives = result.get("alternatives")
        if isinstance(alternatives, (list, tuple)):
            pending.extend(item for item in alternatives[:200] if isinstance(item, Mapping))
    return expanded


def unsuccessful_lookup_status(results: Iterable[Mapping[str, Any] | None]) -> str:
    """Retain acquisition failure semantics when no candidate passed selection."""
    results = list(results)
    expanded = _expand_results(results)
    statuses = {str(result.get("acquisition_status") or "") for result in expanded}
    for failure in (
        "AUTHENTICATION_ERROR",
        "BLOCKED",
        "RATE_LIMITED",
        "TRANSIENT_ERROR",
        "PARSER_ERROR",
        "INVALID_RESPONSE",
        "NOT_CONFIGURED",
        "UNSUPPORTED_PROVIDER",
    ):
        if failure in statuses:
            return failure
    if any(
        result.get(field)
        for result in expanded
        for field in ("plainLyrics", "syncedLyrics", "translationLyrics", "romajiLyrics")
    ):
        return "NO_ACCEPTABLE_CANDIDATE"
    if verified_instrumental_result(expanded):
        return "INSTRUMENTAL"
    if expanded and all(results) and statuses == {"NOT_FOUND"}:
        return "NOT_FOUND"
    return "INDETERMINATE"


def verified_instrumental_result(
    results: Iterable[Mapping[str, Any] | None],
) -> Mapping[str, Any] | None:
    """Return explicit recording-matched instrumental evidence, never a no-hit."""
    return next(
        (
            result
            for result in _expand_results(results)
            if result.get("acquisition_status") == "INSTRUMENTAL"
            and result.get("instrumental") is True
            and result.get("instrumental_verified") is True
            and result.get("identity_validated") is True
        ),
        None,
    )


def choose_provider_result(
    results: Iterable[Mapping[str, Any] | None],
    *,
    title: str,
    artist: str,
    album: str,
    duration: float,
    language: str = "",
    isrc: str = "",
    recording_id: str = "",
    verbose: bool = False,
) -> LyricsAssessment | None:
    candidates: list[LyricsCandidate] = []
    alternate_candidates: dict[str, list[LyricsCandidate]] = {"translation": [], "romanization": []}
    expanded = _expand_results(results)
    for index, result in enumerate(expanded):
        if result.get("acquisition_status") and not (result.get("syncedLyrics") or result.get("plainLyrics")):
            if verbose:
                print(
                    f"[VERBOSE][LYRICS] Provider {result.get('source', 'unknown')}: "
                    f"acquisition={result.get('acquisition_status')} error={result.get('error', '')}"
                )
            continue
        payload = dict(result)
        if any(
            len(str(payload.get(field) or "")) > MAX_LYRICS_CHARS
            for field in (
                "syncedLyrics",
                "plainLyrics",
                "translationLyrics",
                "romajiLyrics",
                "providerRaw",
                "rawLyrics",
            )
        ):
            if verbose:
                print(
                    f"[VERBOSE][LYRICS] Provider {payload.get('source', 'unknown')}: INVALID_RESPONSE lyrics payload exceeds safety limit"
                )
            continue
        candidate = provider_candidate(payload, fallback_provider=f"provider-{index + 1}", duration=duration)
        if not candidate:
            continue
        if candidate.translation:
            alternate_candidates["translation"].append(candidate)
        elif candidate.romanization:
            alternate_candidates["romanization"].append(candidate)
        else:
            # Duplicates are harmless here: selection does not use provider
            # consensus as a vote, and the full candidate evidence remains useful.
            candidates.append(candidate)
    if verbose:
        for candidate in [*candidates, *alternate_candidates["translation"], *alternate_candidates["romanization"]]:
            assessed = assess_candidate(
                candidate,
                title=title,
                artist=artist,
                album=album,
                duration=duration,
                language=language,
                isrc=isrc,
                recording_id=recording_id,
            )
            outcome = "eligible" if assessed.valid else "rejected"
            print(
                f"[VERBOSE][LYRICS] Candidate {candidate.provider}: {outcome}; "
                f"score={assessed.score:.1f} match={assessed.match_confidence:.1f} "
                f"version={assessed.version_confidence:.1f} "
                f"text={assessed.text_quality:.1f} sync={assessed.sync_quality:.1f} "
                f"source={assessed.source_confidence:.1f}; "
                f"warnings={' | '.join(assessed.warnings)}; reasons={' | '.join(assessed.reasons)}"
            )

    def select_form(items: list[LyricsCandidate]) -> LyricsAssessment | None:
        return select_best(
            items,
            title=title,
            artist=artist,
            album=album,
            duration=duration,
            language=language,
            isrc=isrc,
            recording_id=recording_id,
        )

    selected = select_form(candidates)
    # A translation/romanization-only provider may still add a useful dedicated
    # form, but it must never be mislabelled as original lyrics in LYRICS.
    if selected is None:
        selected = select_form(alternate_candidates["translation"] + alternate_candidates["romanization"])

    if selected is not None:
        raw_selected = selected.candidate.metadata.get("result")
        enriched = dict(raw_selected) if isinstance(raw_selected, Mapping) else {}
        form_provenance = dict(enriched.get("alternateProvenance") or {})
        for form_name, field_name, language_field in (
            ("translation", "translationLyrics", "translationLanguage"),
            ("romanization", "romajiLyrics", "romanizationLanguage"),
        ):
            if enriched.get(field_name):
                continue
            form_choice = select_form(alternate_candidates[form_name])
            if form_choice is None:
                continue
            form_candidate = form_choice.candidate
            form_result = form_candidate.metadata.get("result")
            form_value = (
                form_result.get(field_name) if isinstance(form_result, Mapping) else ""
            ) or form_candidate.text
            if not str(form_value).strip():
                continue
            enriched[field_name] = str(form_value)
            # A provider's track language is not necessarily the language of
            # its translated or romanized text. Only retain explicit per-form
            # language metadata supplied for that exact alternate.
            form_language = form_result.get(language_field) if isinstance(form_result, Mapping) else ""
            if form_language:
                enriched[language_field] = str(form_language)
            form_provenance[form_name] = {
                "provider": form_candidate.provider,
                "provider_track_id": form_candidate.provider_track_id,
                "language": form_candidate.language,
                "match_confidence": form_choice.match_confidence,
                "text_quality": form_choice.text_quality,
            }
        if form_provenance:
            enriched["alternateProvenance"] = form_provenance
        # Preserve the selected candidate's original response by replacing
        # only the local result reference used during materialization.
        selected.candidate.metadata["result"] = enriched

    minimum_score = load_config().lyrics.minimum_candidate_score
    if selected and selected.score < minimum_score:
        if verbose:
            print(
                f"[VERBOSE][LYRICS] Candidate {selected.candidate.provider} rejected: "
                f"score {selected.score:.1f} is below configured minimum {minimum_score:.1f}"
            )
        return None
    if verbose and selected:
        print(
            f"[VERBOSE][LYRICS] Selected {selected.candidate.provider}: score={selected.score:.1f}; "
            f"match={selected.match_confidence:.1f}, text={selected.text_quality:.1f}, "
            f"sync={selected.sync_quality:.1f}, source={selected.source_confidence:.1f}"
        )
    return selected


def _safe_path(path: Path, provider: str, content: str, *, existing_path: Path | None = None) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", provider.casefold()).strip("-")[:32] or "remote"

    def same_content(candidate: Path) -> bool:
        try:
            return candidate.read_bytes().decode("utf-8", errors="strict") == content
        except (OSError, UnicodeError):
            return False

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    for name in (
        path.name,
        f"{path.stem}.{slug}.{digest[:16]}{path.suffix}",
        f"{path.stem}.{slug}.{digest}{path.suffix}",
    ):
        candidate = path.with_name(name)
        live = existing_path.with_name(name) if existing_path is not None else candidate
        if live.exists() or live.is_symlink():
            if not live.is_symlink() and same_content(live):
                return live
            continue
        if not candidate.is_symlink() and (not candidate.exists() or same_content(candidate)):
            return candidate
    raise FileExistsError(f"content-addressed lyrics sidecar collision at {path}")


def materialize_embedded(
    base: Path,
    embedded: Mapping[str, Sequence[str]],
    *,
    existing_base: Path,
    duration: float,
) -> dict[str, Path]:
    """Archive every local form and expose validated forms without changing tags.

    The caller supplies the transaction staging path and never invokes this
    during a dry run. Existing sidecars are reused or given collision-safe
    alternatives, just as remote provider artifacts are.
    """
    outputs: dict[str, Path] = {}
    fields = {
        "LYRICS": "original",
        "UNSYNCEDLYRICS": "original",
        "SYNCEDLYRICS": "original",
        "SYNCELYRICS": "original",
        "LYRICS_TRANSLATION": "translation",
        "LYRICS_ROMAJI": "romanization",
    }
    for field, form in fields.items():
        for value in embedded.get(field, ()):
            if not value:
                continue
            synchronized = len(value) <= MAX_LYRICS_CHARS and validate_lrc(value, duration=duration)[0]
            looks_timed = bool(re.search(r"\[\s*[-+]?\d+:\d", value))
            extension = ".lrc" if synchronized else (".lrc.raw" if looks_timed else ".txt")
            suffix = {"original": "", "translation": ".translation", "romanization": ".romaji"}[form]
            wanted = base.with_name(base.stem + suffix + extension)
            destination = _safe_path(wanted, "embedded", value, existing_path=existing_base.parent / wanted.name)
            if not destination.exists():
                destination.write_text(value, encoding="utf-8", newline="")
            key = ("synced" if synchronized else "plain") if form == "original" else form
            # Malformed timing remains archived, but is never reintroduced as
            # plain playback text through a failed synchronization conversion.
            if synchronized or (not looks_timed and len(value.strip()) >= 10):
                outputs.setdefault(key, destination)
    return outputs


def materialize_selected(
    base: Path, assessment: LyricsAssessment, *, existing_base: Path | None = None
) -> LyricsResolution:
    raw_result = assessment.candidate.metadata.get("result")
    result: dict[str, Any] = dict(raw_result) if isinstance(raw_result, Mapping) else {}
    provider = assessment.candidate.provider
    outputs: dict[str, Path] = {}
    requested_duration = result.get("requested_duration")
    try:
        check_duration = float(requested_duration) if requested_duration is not None else None
    except (TypeError, ValueError):
        check_duration = None

    is_alternate_text = assessment.candidate.translation or assessment.candidate.romanization
    preserve_sync = bool(assessment.candidate.synced_lyrics) and not assessment.candidate.metadata.get(
        "sync_text_mismatch"
    )
    representations = {
        "plain": (
            base.with_suffix(".txt"),
            None if is_alternate_text else (assessment.candidate.plain_lyrics or result.get("plainLyrics")),
        ),
        "synced": (
            base.with_suffix(".lrc"),
            None if is_alternate_text else (assessment.candidate.synced_lyrics if preserve_sync else ""),
        ),
        "enhanced": (
            base.with_name(base.stem + ".elrc"),
            None
            if is_alternate_text or not preserve_sync or not result.get("has_word_sync")
            else result.get("enhancedLyrics"),
        ),
        "translation": (base.with_name(base.stem + ".translation.txt"), result.get("translationLyrics")),
        "romanization": (
            base.with_name(
                base.stem
                + (
                    ".romaji.lrc"
                    if validate_lrc(str(result.get("romajiLyrics") or ""), duration=check_duration)[0]
                    else ".romaji.txt"
                )
            ),
            result.get("romajiLyrics"),
        ),
    }
    raw_enhanced = str(result.get("enhancedLyrics") or "")
    if raw_enhanced and not validate_enhanced_lrc(raw_enhanced, duration=check_duration)[0]:
        representations["enhanced_raw"] = (base.with_name(base.stem + ".elrc.raw"), raw_enhanced)
        representations["enhanced"] = (base.with_name(base.stem + ".elrc"), None)
    raw_ttml = "" if is_alternate_text else str(result.get("ttmlLyrics") or "")
    if raw_ttml and _valid_ttml(raw_ttml) and result.get("ttml_native"):
        representations["ttml"] = (base.with_suffix(".ttml"), raw_ttml)

    # Preserve the provider representation separately from normalized display
    # derivatives so no cleanup step destroys evidence such as section labels,
    # attribution, whitespace, or native markup.
    provider_raw = str(result.get("providerRaw") or result.get("rawLyrics") or assessment.candidate.raw_lyrics or "")
    if provider_raw:
        if _valid_ttml(provider_raw):
            raw_suffix = ".provider.raw.ttml"
        else:
            try:
                json.loads(provider_raw)
            except json.JSONDecodeError:
                raw_suffix = ".provider.raw.txt"
            else:
                raw_suffix = ".provider.raw.json"
        representations["raw"] = (base.with_name(base.stem + raw_suffix), provider_raw)

    if not representations["plain"][1] and assessment.candidate.synced:
        representations["plain"] = (base.with_suffix(".txt"), lrc_to_plain(assessment.candidate.text))

    for kind, (wanted, raw) in representations.items():
        # Provider and archival sidecars keep labels, spacing, punctuation and
        # Unicode. Playback-field policy is applied later by metadata_writer.
        text = str(raw or "")
        if not text:
            continue
        # The archival provider sidecar is the exact extracted source string;
        # other representations cross the UTF-8/line-ending storage boundary.
        if kind not in {"raw", "enhanced_raw"}:
            text = conservative_text(text)
        if kind == "enhanced" and not validate_enhanced_lrc(text, duration=check_duration)[0]:
            continue
        if kind == "synced" and not validate_lrc(text, duration=check_duration)[0]:
            continue
        if kind == "romanization" and wanted.suffix == ".lrc" and not validate_lrc(text, duration=check_duration)[0]:
            continue
        existing_path = existing_base.parent / wanted.name if existing_base is not None else None
        destination = _safe_path(wanted, provider, text, existing_path=existing_path)
        if not destination.exists():
            destination.write_text(text, encoding="utf-8", newline="")
        outputs[kind] = destination
    if outputs:
        candidate = assessment.candidate
        manifest = {
            "schema_version": 2,
            "parser_version": PARSER_VERSION,
            "provider": provider,
            "provider_track_id": candidate.provider_track_id,
            "language": candidate.language,
            "attribution": candidate.attribution,
            "identity": {
                "artist": candidate.artist,
                "title": candidate.title,
                "album": candidate.album,
                "duration": candidate.duration,
                "isrc": candidate.isrc,
                "recording_id": candidate.recording_id,
            },
            "assessment": {
                "match": assessment.match_confidence,
                "version": assessment.version_confidence,
                "text": assessment.text_quality,
                "sync": assessment.sync_quality,
                "source": assessment.source_confidence,
            },
            "warnings": list(assessment.warnings),
            "transformations": list(candidate.transformations),
            "storage_policy": "Original LRC; playback section labels excluded; archival text preserved",
            "artifacts": {
                kind: {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                for kind, path in sorted(outputs.items())
            },
        }
        content = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        wanted = base.with_name(base.stem + ".lyrics.json")
        existing_path = existing_base.parent / wanted.name if existing_base is not None else None
        destination = _safe_path(wanted, provider, content, existing_path=existing_path)
        if not destination.exists():
            destination.write_text(content, encoding="utf-8", newline="")
        outputs["manifest"] = destination
    return LyricsResolution(
        provider,
        "FOUND",
        outputs,
        assessment=assessment,
        local=False,
        has_word_sync=bool(result.get("has_word_sync")),
    )
