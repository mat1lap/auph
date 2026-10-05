"""Bounded Lyricsfile 1.0 draft parser; preserve source, never invent intervals."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

import yaml
from yaml.nodes import MappingNode, Node, SequenceNode
from yaml.tokens import AliasToken, AnchorToken

from lyrics_quality import text_similarity, version_signature


class StrictLoader(yaml.SafeLoader):
    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _value in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in seen:
                raise ValueError("Lyricsfile has duplicate or non-string keys")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def timestamp(ms: int) -> str:
    minutes, remainder = divmod(ms, 60_000)
    return f"{minutes:02d}:{remainder // 1000:02d}.{remainder % 1000:03d}"


def checked_ms(value: Any, *, optional: bool = False) -> int | None:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Lyricsfile timestamps must be nonnegative integer milliseconds")
    return value


def load_document(source: str) -> dict[str, Any]:
    if len(source) > 1_000_000:
        raise ValueError("Lyricsfile exceeds text limit")
    try:
        for count, token in enumerate(yaml.scan(source)):
            if count > 50_000 or isinstance(token, (AliasToken, AnchorToken)):
                raise ValueError("Lyricsfile token limit or unsupported aliases/anchors")
        loader = StrictLoader(source)
        try:
            node = loader.get_single_node()
            if node is None:
                raise ValueError("empty Lyricsfile")
            count = 0

            def walk(current: Node, depth: int) -> None:
                nonlocal count
                count += 1
                if depth > 32 or count > 20_000:
                    raise ValueError("Lyricsfile nesting/node limit exceeded")
                if isinstance(current, MappingNode):
                    for key, value in current.value:
                        walk(key, depth + 1)
                        walk(value, depth + 1)
                elif isinstance(current, SequenceNode):
                    for child in current.value:
                        walk(child, depth + 1)

            walk(node, 0)
            data = loader.construct_document(node)
        finally:
            loader.dispose()
    except (yaml.YAMLError, RecursionError) as exc:
        raise ValueError("invalid Lyricsfile YAML") from exc
    if not isinstance(data, dict) or data.get("version") != "1.0" or not isinstance(data.get("metadata"), dict):
        raise ValueError("unsupported Lyricsfile version or structure")

    def validate_values(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                validate_values(child)
        elif isinstance(value, list):
            for child in value:
                validate_values(child)
        elif value is not None and not isinstance(value, (str, int, bool)):
            raise ValueError("unsupported Lyricsfile scalar type")

    validate_values(data)
    return data


def parse_lyricsfile(source: str, *, title: str, artist: str, duration: float | None = None) -> dict[str, Any]:
    data = load_document(source)
    metadata = data["metadata"]
    for key, expected in (("title", title), ("artist", artist)):
        value = metadata.get(key)
        if not isinstance(value, str) or not value.strip() or (expected and text_similarity(value, expected) < 0.85):
            raise ValueError(f"Lyricsfile {key} identity conflict")
    if version_signature(metadata["title"]) != version_signature(title):
        raise ValueError("Lyricsfile version conflict")
    for key in ("language", "album"):
        if key in metadata and not isinstance(metadata[key], str):
            raise TypeError(f"Lyricsfile {key} is not text")
    if "instrumental" in metadata and not isinstance(metadata["instrumental"], bool):
        raise TypeError("Lyricsfile instrumental evidence is not boolean")
    source_duration = checked_ms(metadata.get("duration_ms"), optional=True)
    if source_duration is not None and duration and abs(source_duration / 1000 - duration) > max(8, duration * 0.04):
        raise ValueError("Lyricsfile duration conflict")
    plain = data.get("plain", "")
    if not isinstance(plain, str):
        raise TypeError("Lyricsfile plain text is not a string")
    lines = data.get("lines", [])
    if not isinstance(lines, list) or len(lines) > 10_000:
        raise ValueError("invalid Lyricsfile lines")
    if not all(isinstance(line, dict) and isinstance(line.get("text"), str) for line in lines):
        raise ValueError("invalid Lyricsfile line text")
    result: dict[str, Any] = {
        "plainLyrics": plain or "\n".join(line["text"] for line in lines),
        "language": metadata.get("language", ""),
    }
    warnings: list[str] = []
    if metadata.get("instrumental") is True and result["plainLyrics"]:
        raise ValueError("Lyricsfile instrumental claim conflicts with vocal text")
    # Offset semantics are unresolved in the upstream draft. Archive them;
    # provide usable text without guessing synchronized playback placement.
    if metadata.get("offset_ms") not in (None, 0):
        result["warnings"] = ["Lyricsfile offset semantics unsupported; timing retained only in source"]
        return result
    synced: list[str] = []
    enhanced: list[str] = []
    ttml_lines: list[tuple[dict[str, Any], int, int | None, list[dict[str, Any]]]] = []
    previous = -1
    timing_ok, words_ok, intervals_ok = True, True, bool(lines)
    try:
        for line in lines:
            start = checked_ms(line.get("start_ms"))
            assert start is not None
            end = checked_ms(line.get("end_ms"), optional=True)
            if (
                start < previous
                or (end is not None and end < start)
                or (duration and max(start, end or start) > duration * 1000 + 1000)
            ):
                raise ValueError("Lyricsfile line timing invalid")
            previous = start
            synced.append(f"[{timestamp(start)}]{line['text']}")
            words = line.get("words", [])
            if not isinstance(words, list):
                raise TypeError("Lyricsfile words are not a sequence")
            parts: list[str] = []
            word_previous = -1
            try:
                for word in words:
                    if not isinstance(word, dict) or not isinstance(word.get("text"), str):
                        raise TypeError("invalid Lyricsfile word")
                    word_start = checked_ms(word.get("start_ms"))
                    word_end = checked_ms(word.get("end_ms"), optional=True)
                    assert word_start is not None
                    if (
                        word_start < start
                        or word_start < word_previous
                        or (word_end is not None and word_end < word_start)
                        or (end is not None and max(word_start, word_end or word_start) > end)
                        or (duration and max(word_start, word_end or word_start) > duration * 1000 + 1000)
                    ):
                        raise ValueError("invalid Lyricsfile word timing")
                    word_previous = word_start
                    parts.append(f"<{timestamp(word_start)}>{word['text']}")
                    intervals_ok = intervals_ok and word_end is not None
                if not words or "".join(word["text"] for word in words) != line["text"]:
                    words_ok = False
                else:
                    enhanced.append(f"[{timestamp(start)}]" + "".join(parts))
            except (ValueError, TypeError):
                words_ok = False
                intervals_ok = False
                warnings.append("invalid Lyricsfile word timing; line/plain forms retained")
            intervals_ok = intervals_ok and end is not None
            ttml_lines.append((line, start, end, words))
    except (ValueError, TypeError):
        timing_ok = False
        warnings.append("invalid Lyricsfile line timing; plain form retained")
    if timing_ok and synced:
        result["syncedLyrics"] = "\n".join(synced)
        if words_ok and enhanced:
            result["enhancedLyrics"] = "\n".join(enhanced)
        if intervals_ok and words_ok:
            attributes = {"xmlns": "http://www.w3.org/ns/ttml"}
            if metadata.get("language"):
                attributes["{http://www.w3.org/XML/1998/namespace}lang"] = metadata["language"]
            root = ET.Element("tt", attributes)
            div = ET.SubElement(ET.SubElement(root, "body"), "div")
            for line, start, end, words in ttml_lines:
                paragraph = ET.SubElement(div, "p", {"begin": f"{start}ms", "end": f"{end}ms"})
                for word in words:
                    ET.SubElement(
                        paragraph, "span", {"begin": f"{word['start_ms']}ms", "end": f"{word['end_ms']}ms"}
                    ).text = word["text"]
            result["ttmlLyrics"] = ET.tostring(root, encoding="unicode")
            result["ttml_source_timing"] = True
    result["warnings"] = warnings
    return result
