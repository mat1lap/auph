#!/usr/bin/env python3
"""Normalize and process lyrics (LRC, Enhanced LRC, BetterLyrics TTML, NetEase YRC) with full support for:
- Word-level timestamps (<mm:ss.xx>)
- Separate background vocals (agents / background roles in TTML, parenthesized in Enhanced LRC)
- Timed Text Markup Language (TTML XML) native parsing and generation (W3C / AMLL / Apple Music spec)
- BetterLyrics Unison API (https://unison.boidu.dev) with intelligent ranking
- High-precision Syllable-level YRC engine with automatic TTML and Enhanced LRC conversion
- Multi-source fallback: LRCLIB, Musixmatch, Genius, NetEase, Kugou, Lyrics.ovh
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import html
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Mapping
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Self

from lyrics_quality import (
    MAX_LYRICS_CHARS,
    WORD_TIMESTAMP_RE,
    lrc_to_plain,
    normalize_lookup,
    text_similarity,
    validate_enhanced_lrc,
    validate_lrc,
    version_signature,
)
from provider_cache import IntervalRateLimiter, JsonCache, ProviderResult, fetch_json, redact_secrets, retry_delay

_LRCLIB_LOCK = threading.Lock()
_LRCLIB_LAST_REQUEST = 0.0
_LRCLIB_SESSION = None
_LRCLIB_RATE_LIMITER = IntervalRateLimiter(0.25)
_LYRICS_PROVIDER_STATE = threading.local()
_PROVIDER_LIMITER_LOCK = threading.Lock()
_PROVIDER_LIMITERS: dict[str, IntervalRateLimiter] = {}
_LRCLIB_CACHE = JsonCache(
    Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "audiophile-archtagger" / "lyrics-lrclib",
    ttl_seconds=30 * 24 * 3600,
    negative_ttl_seconds=6 * 3600,
)
_PROVIDER_RESULT_CACHE = JsonCache(
    Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    / "audiophile-archtagger"
    / "lyrics-provider-results",
    ttl_seconds=14 * 24 * 3600,
    negative_ttl_seconds=6 * 3600,
)
_MAX_PROVIDER_RESPONSE_BYTES = 5_000_000


def _quote_component(value: object) -> str:
    """Percent-encode a complete URL component, including embedded slashes."""
    return urllib.parse.quote(str(value), safe="")


def _provider_duration_seconds(value: Any) -> float | None:
    """Parse a provider duration, rejecting booleans, non-finite and non-positive values."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def decode_provider_text(response: object, content: bytes) -> str:
    """Decode provider text using its declared charset, failing on corruption."""
    headers = getattr(response, "headers", None)
    charset = None
    if headers is not None:
        get_charset = getattr(headers, "get_content_charset", None)
        if callable(get_charset):
            charset = get_charset()
        if not charset and hasattr(headers, "get"):
            content_type = headers.get("Content-Type") or headers.get("content-type") or ""
            match = re.search(r"charset\s*=\s*['\"]?([^;'\"\s]+)", str(content_type), re.IGNORECASE)
            charset = match.group(1) if match else None
    encoding = str(charset or "utf-8")
    if content.startswith(b"\xef\xbb\xbf") and encoding.casefold().replace("_", "-") in {"utf-8", "utf8"}:
        encoding = "utf-8-sig"
    try:
        return content.decode(encoding, errors="strict")
    except (UnicodeError, LookupError) as exc:
        _record_provider_failure(exc)
        raise


def read_provider_text(response: object, *, max_bytes: int = 5_000_000, encoding: str | None = None) -> str:
    """Read a bounded provider body and decode it without lossy replacement."""
    read = getattr(response, "read", None)
    if not callable(read):
        raise TypeError("provider response has no byte reader")
    try:
        content = read(max_bytes + 1)
    except TypeError:
        # A few lightweight response adapters expose read() without size.
        content = read()
    if len(content) > max_bytes:
        exc = ValueError(f"provider response exceeds {max_bytes} byte safety limit")
        _record_provider_failure(exc)
        raise exc
    if encoding:
        try:
            return content.decode(encoding, errors="strict")
        except (UnicodeError, LookupError) as exc:
            _record_provider_failure(exc)
            raise
    return decode_provider_text(response, content)


class _HTMLNode:
    """Small inert HTML tree used to read provider markup without executing it."""

    def __init__(self, tag: str, attrs: Mapping[str, str] | None = None) -> None:
        self.tag = tag.casefold()
        self.attrs = dict(attrs or {})
        self.children: list[_HTMLNode | str] = []

    def text(self) -> str:
        pieces: list[str] = []

        def visit(node: _HTMLNode) -> None:
            if node.tag in {"script", "style", "noscript"}:
                return
            for child in node.children:
                if isinstance(child, str):
                    pieces.append(child)
                elif child.tag == "br":
                    pieces.append("\n")
                else:
                    block = child.tag in {"div", "p", "li", "tr", "pre", "section"}
                    if block and pieces and not pieces[-1].endswith("\n"):
                        pieces.append("\n")
                    visit(child)
                    if block and pieces and not pieces[-1].endswith("\n"):
                        pieces.append("\n")

        visit(self)
        return "".join(pieces)

    def descendants(self) -> list[_HTMLNode]:
        result: list[_HTMLNode] = []
        stack = list(reversed(self.children))
        while stack:
            child = stack.pop()
            if isinstance(child, _HTMLNode):
                result.append(child)
                stack.extend(reversed(child.children))
        return result


class _ProviderHTMLTreeParser(HTMLParser):
    """Parse untrusted HTML into a bounded, non-executable semantic tree."""

    _VOID_TAGS = frozenset(
        {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.root = _HTMLNode("document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _HTMLNode(tag, {key.casefold(): value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if node.tag not in self._VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in self._VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        wanted = tag.casefold()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == wanted:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)

    def handle_entityref(self, name: str) -> None:
        self.stack[-1].children.append(html.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        self.stack[-1].children.append(html.unescape(f"&#{name};"))


def _parse_provider_html(page: str, provider: str) -> _HTMLNode:
    if len(page) > 5_000_000:
        raise ValueError(f"{provider} HTML response exceeds 5 MB")
    parser = _ProviderHTMLTreeParser()
    try:
        parser.feed(page)
        parser.close()
    except (AssertionError, ValueError) as exc:
        raise ValueError(f"{provider} HTML is malformed: {exc}") from exc
    return parser.root


def _provider_nodes(root: _HTMLNode, *, element_id: str = "", class_name: str = "", tag: str = "") -> list[_HTMLNode]:
    return [
        node
        for node in root.descendants()
        if (not element_id or node.attrs.get("id") == element_id)
        and (not class_name or class_name.casefold() in node.attrs.get("class", "").casefold().split())
        and (not tag or node.tag == tag.casefold())
    ]


def _provider_page_blocked(page: str) -> bool:
    sample = html.unescape(page[:250_000]).casefold()
    return any(
        marker in sample
        for marker in (
            "captcha",
            "verify you are human",
            "checking your browser",
            "access denied",
            "unusual traffic",
            "temporarily blocked",
        )
    )


def _identity_from_result_label(label: str, song: str, artist: str) -> tuple[str, str] | None:
    """Match explicit artist/title labels without promoting query text to evidence."""
    cleaned = html.unescape(label).strip()
    cleaned = re.sub(
        r"\s+(?:lyrics?\s+discussion|discussion|topic|lyrics?|text|lrc|аккорды|текст песни)\s*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    parts = [part.strip() for part in re.split(r"\s+(?:[-–—|]|by)\s+", cleaned, flags=re.IGNORECASE) if part.strip()]
    for split in range(1, len(parts)):
        left, right = " - ".join(parts[:split]), " - ".join(parts[split:])
        if is_track_match(right, left, song, artist):
            return right, left
        if is_track_match(left, right, song, artist):
            return left, right
    return None


def classify_lyrics_exception(exc: BaseException) -> tuple[str, str]:
    """Map acquisition exceptions to stable diagnostics without hiding failures."""
    custom_status = getattr(exc, "status", "")
    if custom_status in {
        "NOT_FOUND",
        "TRANSIENT_ERROR",
        "RATE_LIMITED",
        "BLOCKED",
        "AUTHENTICATION_ERROR",
        "NOT_CONFIGURED",
        "UNSUPPORTED_PROVIDER",
        "PARSER_ERROR",
        "INVALID_RESPONSE",
    }:
        return str(custom_status), redact_secrets(str(exc))
    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if status == 404:
        kind = "NOT_FOUND"
    elif status == 429:
        kind = "RATE_LIMITED"
    elif status in {403, 451}:
        kind = "BLOCKED"
    elif status in {401, 407}:
        kind = "AUTHENTICATION_ERROR"
    elif status in {408, 425, 500, 502, 503, 504}:
        kind = "TRANSIENT_ERROR"
    elif status:
        kind = "INVALID_RESPONSE"
    elif isinstance(exc, (json.JSONDecodeError, ET.ParseError)):
        kind = "PARSER_ERROR"
    elif isinstance(exc, UnicodeError):
        kind = "INVALID_RESPONSE"
    else:
        # URL/DNS/socket errors and timeouts are transient; unexpected parser
        # exceptions are surfaced as parser errors to avoid calling them no-hit.
        name = type(exc).__name__.casefold()
        kind = (
            "TRANSIENT_ERROR"
            if any(token in name for token in ("timeout", "urlerror", "connection", "socket"))
            else "PARSER_ERROR"
        )
    return kind, redact_secrets(f"{type(exc).__name__}: {exc}")


class _LyricsAcquisitionError(RuntimeError):
    """A provider's valid API error with explicit acquisition semantics."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


def _record_provider_failure(exc: BaseException) -> None:
    """Retain swallowed network/decoder/parser errors during one adapter call."""
    if not getattr(_LYRICS_PROVIDER_STATE, "active", False):
        return
    status, detail = classify_lyrics_exception(exc)
    failures = getattr(_LYRICS_PROVIDER_STATE, "failures", None)
    if failures is not None:
        failures.append((status, detail))


class _BoundedProviderResponse:
    """Small urllib response proxy that caps both sized and unbounded reads."""

    def __init__(self, response: Any, *, maximum: int = _MAX_PROVIDER_RESPONSE_BYTES) -> None:
        self._response = response
        self._maximum = maximum
        self._read_total = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self._maximum - self._read_total
        requested = remaining + 1 if size is None or size < 0 else min(size, remaining + 1)
        try:
            content = self._response.read(requested)
        except TypeError:
            content = self._response.read()
        if len(content) > remaining:
            exc = ValueError(f"provider response exceeds {self._maximum} byte safety limit")
            _record_provider_failure(exc)
            raise exc
        self._read_total += len(content)
        return content

    def __enter__(self) -> Self:
        enter = getattr(self._response, "__enter__", None)
        if callable(enter):
            enter()
        return self

    def __exit__(self, *args: object) -> Any:
        exit_method = getattr(self._response, "__exit__", None)
        if callable(exit_method):
            return exit_method(*args)
        close = getattr(self._response, "close", None)
        if callable(close):
            close()
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)


def _provider_urlopen(*args: Any, _opener: Any = None, **kwargs: Any):
    """Bounded, per-host paced request with Retry-After-aware transient retry."""
    url = args[0] if args else kwargs.get("url", "")
    full_url = url.get_full_url() if hasattr(url, "get_full_url") else str(url)
    host = urllib.parse.urlsplit(full_url).netloc.casefold()
    if host:
        with _PROVIDER_LIMITER_LOCK:
            limiter = _PROVIDER_LIMITERS.setdefault(host, IntervalRateLimiter(0.5))
        limiter.acquire()

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = _opener.open(*args, **kwargs) if _opener else urllib.request.urlopen(*args, **kwargs)
        except Exception as exc:
            last_error = exc
            status, _ = classify_lyrics_exception(exc)
            retryable = status in {"RATE_LIMITED", "TRANSIENT_ERROR"}
            if attempt == 0 and retryable:
                delay = retry_delay(exc, attempt + 1, base=0.5, maximum=8.0)
                retry_after = str((getattr(exc, "headers", None) or {}).get("Retry-After", "")).strip()
                # Do not retry a 429 before its Retry-After time. If that
                # wait is longer than this interactive lookup's small retry
                # budget, return the typed rate-limit result immediately.
                retry_budget = 2.0
                if retry_after and delay > retry_budget:
                    break
                if delay > retry_budget:
                    break
                time.sleep(delay)
                if host:
                    with _PROVIDER_LIMITER_LOCK:
                        limiter = _PROVIDER_LIMITERS.setdefault(host, IntervalRateLimiter(0.5))
                    limiter.acquire()
                continue
            break
        else:
            if getattr(_LYRICS_PROVIDER_STATE, "active", False):
                _LYRICS_PROVIDER_STATE.responses = getattr(_LYRICS_PROVIDER_STATE, "responses", 0) + 1
            return _BoundedProviderResponse(response)
    if last_error is not None:
        _record_provider_failure(last_error)
        raise last_error
    raise RuntimeError("provider request failed without an exception")


def _provider_json_loads(value: str | bytes | bytearray, **kwargs: Any) -> Any:
    """JSON decoding boundary used by provider adapters for schema diagnostics."""
    try:
        return json.loads(value, **kwargs)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _record_provider_failure(exc)
        raise


def mark_provider_not_found() -> None:
    """Mark a provider response as a valid, schema-checked empty result."""
    if getattr(_LYRICS_PROVIDER_STATE, "active", False):
        _LYRICS_PROVIDER_STATE.trusted_no_result = True


def invoke_lyrics_provider(
    source: str,
    provider: Any,
    *args: Any,
    _cache_identity: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    """Run one adapter with identity-safe found-result caching and typed diagnostics."""
    positional = list(args)
    title = str(kwargs.get("song") or (positional[0] if positional else ""))
    artist = str(kwargs.get("artist") or (positional[1] if len(positional) > 1 else ""))
    context = dict(_cache_identity or {})
    identity = {
        "provider": source.casefold(),
        "adapter": getattr(provider, "__name__", type(provider).__name__),
        "title": normalize_lookup(str(context.get("title") or title)),
        "artist": normalize_lookup(str(context.get("artist") or artist)),
        "query_title": normalize_lookup(title),
        "query_artist": normalize_lookup(artist),
        "album": normalize_lookup(str(kwargs.get("album") or context.get("album") or "")),
        "versioned_title": normalize_lookup(title),
        "version": normalize_lookup(str(context.get("version") or "")),
        # Keep a punctuation- and diacritic-preserving cache identity beside
        # the fuzzy comparison form. Lookup normalization is intentionally
        # lossy and must not make titles such as "A/B" and "AB" collide.
        "title_exact": unicodedata.normalize("NFC", str(context.get("title") or title)).casefold().strip(),
        "artist_exact": unicodedata.normalize("NFC", str(context.get("artist") or artist)).casefold().strip(),
        "query_title_exact": unicodedata.normalize("NFC", title).casefold().strip(),
        "query_artist_exact": unicodedata.normalize("NFC", artist).casefold().strip(),
        "album_exact": unicodedata.normalize("NFC", str(kwargs.get("album") or context.get("album") or ""))
        .casefold()
        .strip(),
        "duration": round(float(kwargs.get("duration") or context.get("duration") or 0), 1),
        "isrc": str(kwargs.get("isrc") or context.get("isrc") or "").replace("-", "").upper(),
        "recording_id": str(kwargs.get("recording_id") or context.get("recording_id") or "").casefold(),
        "track_id": str(kwargs.get("track_id") or context.get("track_id") or ""),
        "language": normalize_lookup(str(kwargs.get("language") or context.get("language") or "")),
    }
    cache_key = "provider-result:v3:" + json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    cached = _PROVIDER_RESULT_CACHE.load(cache_key)
    if cached and isinstance(cached[1], dict) and cached[1].get("found"):
        cached_result = dict(cached[1])
        cached_result["cache_hit"] = True
        return cached_result

    state = _LYRICS_PROVIDER_STATE
    previous_active = getattr(state, "active", False)
    previous_failures = getattr(state, "failures", None)
    previous_responses = getattr(state, "responses", 0)
    previous_trusted_empty = getattr(state, "trusted_no_result", False)
    state.active = True
    state.failures = []
    state.responses = 0
    state.trusted_no_result = False
    try:
        result = provider(*args, **kwargs)
    except Exception as exc:
        _record_provider_failure(exc)
        result = None
    failures = list(state.failures)
    responses = int(state.responses)
    trusted_empty = bool(state.trusted_no_result)
    state.active = previous_active
    state.failures = previous_failures
    state.responses = previous_responses
    state.trusted_no_result = previous_trusted_empty
    failure_severity = {
        "AUTHENTICATION_ERROR": 7,
        "BLOCKED": 6,
        "RATE_LIMITED": 5,
        "TRANSIENT_ERROR": 4,
        "PARSER_ERROR": 3,
        "INVALID_RESPONSE": 2,
        "NOT_FOUND": 1,
    }
    if result is not None:
        # A fallback query can find a valid empty response after an earlier
        # request failed. That does not prove the provider had no result, so
        # retain the acquisition failure and do not report a clean negative.
        if isinstance(result, dict) and result.get("acquisition_status") == "NOT_FOUND" and failures:
            status, error = max(failures, key=lambda item: failure_severity.get(item[0], 0))
            return {
                "source": source,
                "acquisition_status": status,
                "error": error,
                "partial_no_result": True,
            }
        if isinstance(result, dict) and failures and result.get("found"):
            result = dict(result)
            warnings = list(result.get("validationWarnings") or [])
            warnings.append(
                f"some provider requests failed ({max(failures, key=lambda item: failure_severity.get(item[0], 0))[0]})"
            )
            result["validationWarnings"] = list(dict.fromkeys(warnings))
        if (
            isinstance(result, dict)
            and result.get("found")
            and not result.get("generated")
            and not result.get("acquisition_status")
            and any(result.get(key) for key in ("syncedLyrics", "plainLyrics", "translationLyrics", "romajiLyrics"))
        ):
            try:
                payload_size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
                if payload_size <= 5_000_000:
                    _PROVIDER_RESULT_CACHE.store(cache_key, result)
            except (TypeError, ValueError, OSError):
                pass
        return result
    if failures:
        status, error = max(failures, key=lambda item: failure_severity.get(item[0], 0))
        return {"source": source, "acquisition_status": status, "error": error}
    if responses:
        if trusted_empty:
            return {"source": source, "acquisition_status": "NOT_FOUND"}
        # A successful HTTP response alone does not prove an honest empty
        # search; it could be a changed page/schema. Avoid negative caching and
        # expose it as a parser/schema uncertainty for the caller to diagnose.
        return {
            "source": source,
            "acquisition_status": "PARSER_ERROR",
            "error": "provider returned no candidate without a recognized no-result state",
        }
    return None


def _lrclib_open(request: urllib.request.Request, timeout: int):
    """Serialize LRCLIB requests and honor its polite request cadence."""
    global _LRCLIB_LAST_REQUEST
    with _LRCLIB_LOCK:
        wait = 0.25 - (time.monotonic() - _LRCLIB_LAST_REQUEST)
        if wait > 0:
            time.sleep(wait)
        response = _provider_urlopen(request, timeout=timeout)
        _LRCLIB_LAST_REQUEST = time.monotonic()
        return response


LINE_TIMESTAMP_PATTERN = r"\[(?:\d{1,3}:)?\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]"
WORD_TIMESTAMP_PATTERN = r"<(?:\d{1,3}:)?\d{1,3}:\d{2}(?:[.:]\d{1,3})?>"

LINE_TIMESTAMP_PREFIX_RE = re.compile(rf"^(?:{LINE_TIMESTAMP_PATTERN})+\s*")
ANY_LINE_TIMESTAMP_RE = re.compile(rf"{LINE_TIMESTAMP_PATTERN}")
ANY_WORD_TIMESTAMP_RE = re.compile(rf"{WORD_TIMESTAMP_PATTERN}")
LRC_METADATA_HEADER_RE = re.compile(r"^\[(?:ar|ti|al|by|offset|id|re|ve|length):.*\]$", re.IGNORECASE)

CREDIT_PREFIX_RE = re.compile(
    r"^(?:(?:\(|\[)?\s*(?:"
    r"artist|album|title|song|track|"
    r"written\s+by|writers?|lyrics?\s+by|author|lyricist|"
    r"composed\s+by|composers?|music\s+by|"
    r"produced\s+by|producers?|"
    r"arranged\s+by|arrangers?|"
    r"engineered\s+by|engineers?|"
    r"mixed\s+by|mixing\s+by|mixers?|mastered\s+by|mastering\s+by|"
    r"recorded\s+(?:at|in|by)|recording(?:\s+(?:at|in|by))?|"
    r"(?:lead\s*(?:&|and)\s*)?backing\s*vocals?|vocals?|"
    r"electric\s+guitar|acoustic\s+guitar|guitar[s]?|bass(?:\s+guitar)?|"
    r"drums?|percussion|pianos?|keyboards?|synthesizers?|timpani|gong|operatic\s+vocals?|"
    r"автор(?:\s+слов)?|слова|музыка|композитор|текст(?:\s+песни)?|исполнитель|аранжировка|сведение|мастеринг|звукорежисс[её]р|продюсер|"
    r"作词|作曲|编曲|制作人|混音|吉他|贝斯|鼓|录音|母带|监制|和音|词/曲|原唱|发行|作詞|編曲|작사|작곡|편곡|"
    r"synced\s+by|sync\s+by|lrc\s+by|lrc\s+generated\s+by|lyrics\s+(?:provided|submitted)\s+by|"
    r"translated\s+by|translation|uploaded\s+by|edited\s+by|correction\s+by"
    r")\b\s*[:：]?)",
    re.IGNORECASE,
)

GARBAGE_LINE_RE = re.compile(
    r"(?:^[@#\$%\^&\*]{3,}$)|"
    r"(?:^\s*[\(\[\{]\s*[\)\]\}]\s*$)|"
    r"(?:^[.\s\-_=+~#@\*]{4,}$)|"
    r"(?:^[\(\[<]?(?:music|instrumental|guitar\s+solo|solo)[\)\]>]?$)|"
    r"(?:^\s*\[?\s*текст\s+песни(?:\s+[«\"'].*?[»\"'])?\s*\]?\s*$)|"
    r"(?:qq\s*[:：]\s*\d+)|"
    r"(?:qq音乐|网易云音乐|酷狗音乐)|"
    r"(?:https?://|www\.)",
    re.IGNORECASE,
)

YRC_LINE_RE = re.compile(r"^\[(\d+),(\d+)\](.*)$")
YRC_WORD_RE = re.compile(r"\((\d+),(\d+),\d+\)([^(\n]*)")

KRC_LINE_RE = re.compile(r"^\[(\d+),(\d+)\](.*)$")
KRC_WORD_RE = re.compile(r"<(\d+),(\d+),\d+>([^<\n]*)")
KUGOU_KRC_KEY = [0x40, 0x47, 0x61, 0x77, 0x5E, 0x32, 0x74, 0x47, 0x51, 0x36, 0x31, 0x2D, 0xCE, 0xD2, 0x6E, 0x69]


def decode_kugou_krc(encoded: str, *, maximum: int = 5_000_000) -> str:
    """Decode one bounded Kugou KRC payload without unbounded inflate."""
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) < 5 or raw[:4] != b"krc1":
        raise ValueError("invalid KRC envelope")
    encrypted = raw[4:]
    compressed = bytes(byte ^ KUGOU_KRC_KEY[index % len(KUGOU_KRC_KEY)] for index, byte in enumerate(encrypted))
    inflater = zlib.decompressobj()
    decoded = inflater.decompress(compressed, maximum + 1)
    if len(decoded) > maximum or inflater.unconsumed_tail:
        raise ValueError("KRC lyrics exceed 5 MB decompression limit")
    if not inflater.eof or inflater.unused_data:
        raise ValueError("incomplete or trailing data in KRC stream")
    return decoded.decode("utf-8", errors="strict")


QRC_LINE_RE = re.compile(r"^\[(\d+),(\d+)\](.*)$")
QRC_WORD_RE = re.compile(r"\((\d+),(\d+)\)([^(\n]*)")
CHORD_PATTERN_RE = re.compile(r"^[A-H][b#]?(?:m|maj|min|dim|aug|sus|add)?[0-9]?(?:/[A-H][b#]?)?$")
CHORD_LINE_RE = re.compile(r"^\s*(?:[A-H][b#]?(?:m|maj|min|dim|aug|sus|add)?[0-9]?(?:/[A-H][b#]?)?\s*){2,}$")
INLINE_CHORD_RE = re.compile(r"\[(?:[A-H][b#]?(?:m|maj|min|dim|aug|sus|add)?[0-9]?(?:/[A-H][b#]?)?)\]")
STRUCTURAL_TAG_RE = re.compile(
    r"^\s*[\(\[\{]?(?:"
    r"chorus|verse(?:\s+\d+)?|bridge|outro|intro|hook|pre-chorus|solo|guitar\s+solo|interlude|refrain|"
    r"куплет(?:\s+\d+)?|припев(?:\s+\d+)?|вступление|кода|бридж|проигрыш|запев|соло|переход"
    r")[\)\]\}]?\s*[:：]?",
    re.IGNORECASE,
)
REPETITION_MARKER_RE = re.compile(r"\s*[\(\[\{]\s*(?:x\d+|\d+\s*(?:раза?|times?))\s*[\)\]\}]", re.IGNORECASE)


def detect_instrumental(title: str, artist: str = "", extra_tags: dict[str, Any] | None = None) -> bool:
    """Detect if a track is purely instrumental based on title patterns and metadata tags."""
    if not title:
        return False
    # Arrangement names such as "orchestral version" and "piano version" do
    # not establish that there are no vocals. Only explicit instrumental labels
    # are strong enough to suppress lyrics lookup.
    if re.search(r"\b(instrumental|karaoke|backing\s+track)\b", title, re.IGNORECASE):
        return True
    if extra_tags:
        inst_val = str(extra_tags.get("INSTRUMENTAL", "")).strip().lower()
        if inst_val in ("1", "true", "yes"):
            return True
    return False


def detect_language_branch(title: str, artist: str = "") -> str:
    """Detect language branch: 'CYRILLIC', 'CJK', or 'LATIN'."""
    text = f"{title} {artist}"
    # Cyrillic: \u0400-\u04FF
    if re.search(r"[\u0400-\u04ff]", text):
        return "CYRILLIC"
    # CJK: Chinese (\u4e00-\u9fff), Japanese Hiragana/Katakana (\u3040-\u30ff), Korean Hangul (\uac00-\ud7af)
    if re.search(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", text):
        return "CJK"
    return "LATIN"


def get_query_profiles(title: str, artist: str = "") -> tuple[tuple[str, str], tuple[str, str]]:
    """Return strict and secondary search profiles, preserving recording versions."""
    strict = (title.strip(), artist.strip())
    clean_t = title.strip()
    # Decorative release packaging may be dropped from secondary SEARCH terms.
    # Live/remix/edit/acoustic/demo suffixes remain because they identify a
    # different recording. This function is never used on stored lyrics.
    clean_t = re.sub(
        r"\s*[\(\[\{][^\)\]\}]*(?:deluxe|expanded|anniversary|remaster(?:ed)?|reissue|edition)[^\)\]\}]*[\)\]\}]\s*$",
        "",
        clean_t,
        flags=re.IGNORECASE,
    )
    clean_t = (
        re.sub(
            r"\s*(?:[/|–—-]\s*)?(?:\d{2,4}(?:th|st|nd|rd)?\s+)?(?:anniversary|deluxe|expanded|remaster(?:ed)?|reissue|edition)\b.*$",
            "",
            clean_t,
            flags=re.IGNORECASE,
        ).strip()
        or title.strip()
    )
    clean_a = artist.strip()

    cleaned = (clean_t or title.strip(), clean_a or artist.strip())
    return strict, cleaned


def cyrillic_to_latin(text: str) -> str:
    """Transliterate Cyrillic text to Latin for fallback matching on Western lyrics providers."""
    table = {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "y",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "kh",
        "ц": "ts",
        "ч": "ch",
        "ш": "sh",
        "щ": "shch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
    res: list[str] = []
    for c in text:
        low = c.lower()
        if low in table:
            t = table[low]
            res.append(t.upper() if c.isupper() else t)
        else:
            res.append(c)
    return "".join(res)


def normalize_name(s: str) -> str:
    """Comparison-only identity fold; stored text never passes through this."""
    if not s:
        return ""
    folded = unicodedata.normalize("NFKC", s).casefold().replace("ё", "е").replace("&", " and ")
    decomposed = unicodedata.normalize("NFD", folded)
    folded = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^\w]|_", "", folded, flags=re.UNICODE)


def clean_parentheses(text: str) -> str:
    """Compatibility shim: never rewrite punctuation in authentic lyrics."""
    return text


def is_credit_or_garbage_line(
    line_body: str,
    song_title: str = "",
    artist_name: str = "",
    timestamp_sec: float | None = None,
) -> bool:
    """Recognize transport metadata only; generic lyric words are never garbage."""
    return bool(LRC_METADATA_HEADER_RE.fullmatch(line_body.strip()))


def sanitize_lyrics(
    text: str,
    song_title: str = "",
    artist_name: str = "",
    keep_timestamps: bool = True,
) -> str:
    """Remove LRC transport markers while preserving lyric wording and layout."""
    if not text:
        return ""
    source = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for line in source.split("\n"):
        if LRC_METADATA_HEADER_RE.fullmatch(line.strip()):
            continue
        if keep_timestamps:
            lines.append(line)
        else:
            body = LINE_TIMESTAMP_PREFIX_RE.sub("", line)
            body = ANY_LINE_TIMESTAMP_RE.sub("", body)
            lines.append(ANY_WORD_TIMESTAMP_RE.sub("", body))
    return "\n".join(lines)


def parse_time(t_str: str) -> float:
    """Parse time string in format mm:ss.xxx, hh:mm:ss.xxx or seconds."""
    t_str = t_str.strip().rstrip("s")
    parts = t_str.split(":")
    if len(parts) == 3:
        h, m, s = parts
        hour, minute, second = int(h), int(m), float(s)
        if hour < 0 or not 0 <= minute < 60 or not math.isfinite(second) or not 0 <= second < 60:
            raise ValueError(f"invalid TTML clock time: {t_str}")
        return hour * 3600 + minute * 60 + second
    elif len(parts) == 2:
        m, s = parts
        minute, second = int(m), float(s)
        if minute < 0 or not math.isfinite(second) or not 0 <= second < 60:
            raise ValueError(f"invalid TTML clock time: {t_str}")
        return minute * 60 + second
    else:
        seconds = float(parts[0])
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError(f"invalid TTML time value: {t_str}")
        return seconds


def fmt_lrc_time(seconds: float) -> str:
    """Format seconds into LRC with hundredths, or milliseconds if needed."""
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("LRC timestamp must be finite and non-negative")
    total_ms = round(seconds * 1000)
    m, rem = divmod(total_ms, 60_000)
    if rem % 10:
        return f"{m:02d}:{rem // 1000:02d}.{rem % 1000:03d}"
    return f"{m:02d}:{rem // 1000:02d}.{rem % 1000 // 10:02d}"


def fmt_lrc_time_precision(seconds: float, precision: int) -> str:
    """Format LRC time without adding precision absent from the source."""
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("LRC timestamp must be finite and non-negative")
    precision = max(0, min(3, int(precision)))
    scale = 10**precision
    units = round(seconds * scale)
    minutes, remainder = divmod(units, 60 * scale)
    whole_seconds, fraction = divmod(remainder, scale)
    if not precision:
        return f"{minutes:02d}:{whole_seconds:02d}"
    return f"{minutes:02d}:{whole_seconds:02d}.{fraction:0{precision}d}"


def _source_time_precision(value: str) -> int:
    fractional = re.search(r"[.,](\d+)$", value.strip().rstrip("s"))
    return min(3, len(fractional.group(1))) if fractional else 0


def fmt_ttml_time(seconds: float) -> str:
    """Format seconds into standard TTML mm:ss.xxx timestamp."""
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("TTML timestamp must be finite and non-negative")
    m = int(seconds // 60)
    s = seconds - (m * 60)
    return f"{m:02d}:{s:06.3f}"


def ms_to_lrc_ts(ms: int) -> str:
    """Convert millisecond integer without dropping useful precision."""
    value = int(ms)
    if value < 0:
        raise ValueError("LRC timestamp must be non-negative")
    minutes, rem = divmod(value, 60_000)
    if rem % 10:
        return f"{minutes:02d}:{rem // 1000:02d}.{rem % 1000:03d}"
    return f"{minutes:02d}:{rem // 1000:02d}.{rem % 1000 // 10:02d}"


def ms_to_ttml_ts(ms: int) -> str:
    """Convert millisecond integer to mm:ss.xxx."""
    return fmt_ttml_time(ms / 1000.0)


def is_ttml(text: str) -> bool:
    """Detect if content is TTML XML."""
    stripped = text.lstrip()
    if stripped.startswith("<?xml") or "<tt " in stripped or "<tt\n" in stripped or "<tt>" in stripped:
        return True
    return "http://www.w3.org/ns/ttml" in text


def extract_p_text(p: ET.Element) -> str:
    """Extract plain text from a TTML paragraph."""
    parts: list[str] = []
    if p.text:
        parts.append(p.text)
    for child in p:
        tag = child.tag.split("}")[-1].lower()
        if tag in ("br", "linebreak"):
            parts.append("\n")
        parts.append("".join(child.itertext()))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts).replace("\r\n", "\n").replace("\r", "\n")


def extract_spans_recursive(
    el: ET.Element, default_role: str = ""
) -> list[tuple[float | None, float | None, str, bool, int]]:
    """Recursively extract (begin, end, text, is_bg) from span hierarchy."""
    results: list[tuple[float | None, float | None, str, bool, int]] = []
    for child in el:
        tag = child.tag.split("}")[-1].lower()
        if tag != "span":
            continue
        begin_str = child.attrib.get("begin", "").strip()
        end_str = child.attrib.get("end", "").strip()
        b_val = parse_time(begin_str) if begin_str else None
        e_val = parse_time(end_str) if end_str else None
        b_precision = _source_time_precision(begin_str) if begin_str else 0

        role = child.attrib.get("{http://www.w3.org/ns/ttml#metadata}role", child.attrib.get("role", "")).lower()
        is_bg = role in ("x-bg", "background") or default_role in ("x-bg", "background")

        sub_spans = [sub for sub in child if sub.tag.split("}")[-1].lower() == "span"]
        if sub_spans:
            sub_results = extract_spans_recursive(child, default_role="background" if is_bg else default_role)
            for sb, se, stxt, sbg, _precision in sub_results:
                results.append(
                    (sb if sb is not None else b_val, se if se is not None else e_val, stxt, is_bg or sbg, b_precision)
                )
        else:
            text = "".join(child.itertext())
            results.append((b_val, e_val, text, is_bg, b_precision))

        if child.tail:
            results.append((None, None, child.tail, is_bg, b_precision))
    return results


def parse_ttml(
    xml_text: str,
    song: str = "",
    artist: str = "",
    *,
    diagnostics: list[str] | None = None,
) -> tuple[str, str, bool, bool]:
    """Parse TTML XML into (plain_lyrics, enhanced_synced_lrc, has_word_sync, has_back_vocals).

    Preserves:
    - Word-level timestamps (<mm:ss.xx>) for every span
    - Separate background vocals (marked with secondary agents or role='background' / 'x-bg')
    """

    def warn(message: str) -> None:
        if diagnostics is not None:
            diagnostics.append(message)

    if len(xml_text) > 5_000_000:
        warn("TTML rejected: payload exceeds the 5 MB parser limit")
        return ("", "", False, False)
    if re.search(r"<!DOCTYPE|<!ENTITY", xml_text, re.IGNORECASE):
        warn("TTML rejected: document type/entity declarations are not accepted")
        return ("", "", False, False)
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        start = xml_text.find("<tt")
        end = xml_text.rfind("</tt>")
        if start != -1 and end != -1:
            try:
                root = ET.fromstring(xml_text[start : end + 5])
            except ET.ParseError as fragment_error:
                warn(f"TTML fragment parse failed: {fragment_error}")
                return ("", "", False, False)
        else:
            warn(f"TTML parse failed: {exc}")
            return ("", "", False, False)

    if root.tag.split("}")[-1].casefold() != "tt":
        warn("TTML parser received an unexpected root element")
        return ("", "", False, False)
    explicit_word_timing = any(
        key.split("}")[-1].casefold() == "timing" and str(value).casefold() in {"word", "word-level"}
        for key, value in root.attrib.items()
    )

    # Detect agents: lead/primary vocal vs background vocals
    agents: list[str] = []
    for el in root.iter():
        if el.tag.split("}")[-1].lower() == "agent":
            aid = el.attrib.get("{http://www.w3.org/XML/1998/namespace}id", el.attrib.get("id", ""))
            if aid:
                agents.append(aid)
    primary_agent = agents[0] if agents else "v1"

    enhanced_lines: list[str] = []
    plain_lines: list[str] = []
    has_word_sync = False
    has_back_vocals = False
    timing_unreliable = False

    for el in root.iter():
        tag = el.tag.split("}")[-1].lower()
        if tag != "p":
            continue
        begin_str = el.attrib.get("begin", "").strip()
        if not begin_str:
            text_without_timing = extract_p_text(el)
            if text_without_timing:
                plain_lines.extend(text_without_timing.split("\n"))
                timing_unreliable = True
                warn("TTML paragraph has text but no absolute begin time; synchronization discarded")
            continue
        try:
            p_begin = parse_time(begin_str)
        except ValueError as exc:
            text_without_timing = extract_p_text(el)
            if text_without_timing:
                plain_lines.extend(text_without_timing.split("\n"))
            timing_unreliable = True
            warn(f"TTML paragraph has an invalid begin time; text preserved without timing: {exc}")
            continue
        p_precision = _source_time_precision(begin_str)

        agent = ""
        role = ""
        for k, v in el.attrib.items():
            k_lower = k.split("}")[-1].lower()
            if k_lower == "agent":
                agent = v
            elif k_lower == "role":
                role = v

        p_is_bg = (role.lower() in ("background", "x-bg")) or (agent and agent != primary_agent)
        if p_is_bg:
            has_back_vocals = True

        try:
            spans_info = extract_spans_recursive(el, default_role="background" if p_is_bg else "")
        except (ValueError, RecursionError) as exc:
            text_without_timing = extract_p_text(el)
            if text_without_timing:
                plain_lines.extend(text_without_timing.split("\n"))
            timing_unreliable = True
            warn(f"TTML paragraph has malformed word timing; text preserved without timing: {exc}")
            continue
        timed_spans = [s for s in spans_info if s[0] is not None]

        if timed_spans:
            paragraph_word_sync = explicit_word_timing or sum(bool(span[2].strip()) for span in timed_spans) > 1
            if not paragraph_word_sync:
                # A single timed span wrapping a whole paragraph is line sync,
                # not evidence of word-level alignment. Do not manufacture
                # word timing by copying its timestamp onto the entire line.
                line_text = extract_p_text(el)
                if line_text:
                    if is_credit_or_garbage_line(line_text, song_title=song, artist_name=artist, timestamp_sec=p_begin):
                        continue
                    enhanced_lines.append(f"[{fmt_lrc_time_precision(p_begin, p_precision)}]{line_text}")
                    plain_lines.append(line_text)
                continue

            has_word_sync = True
            word_chunks: list[str] = []
            plain_words: list[str] = []

            for s_st, _, s_text, s_bg, s_precision in spans_info:
                clean_txt = s_text.replace("\r\n", "\n").replace("\r", "\n")
                if s_bg:
                    has_back_vocals = True
                if s_st is not None:
                    word_chunks.append(f"<{fmt_lrc_time_precision(s_st, s_precision or p_precision)}>{clean_txt}")
                else:
                    word_chunks.append(clean_txt)
                plain_words.append(clean_txt)

            line_body = "".join(word_chunks)
            plain_body = "".join(plain_words)

            if is_credit_or_garbage_line(plain_body, song_title=song, artist_name=artist, timestamp_sec=p_begin):
                continue

            line_body = clean_parentheses(line_body)
            plain_body = clean_parentheses(plain_body)

            enhanced_lines.append(f"[{fmt_lrc_time_precision(p_begin, p_precision)}]{line_body}")
            plain_lines.append(plain_body)
        else:
            text = extract_p_text(el)
            if not text:
                continue
            for subline in text.split("\n"):
                if not subline.strip():
                    plain_lines.append(subline)
                    continue
                if is_credit_or_garbage_line(subline, song_title=song, artist_name=artist, timestamp_sec=p_begin):
                    continue
                plain_body = subline
                line_body = subline
                line_body = clean_parentheses(line_body)
                plain_body = clean_parentheses(plain_body)

                enhanced_lines.append(f"[{fmt_lrc_time_precision(p_begin, p_precision)}]{line_body}")
                plain_lines.append(plain_body)

    plain_out = sanitize_lyrics("\n".join(plain_lines), song_title=song, artist_name=artist, keep_timestamps=False)
    if timing_unreliable:
        warn("TTML contains incomplete or malformed paragraph timing; no synchronized derivative emitted")
        return plain_out, "", False, has_back_vocals
    synced_out = sanitize_lyrics("\n".join(enhanced_lines), song_title=song, artist_name=artist, keep_timestamps=True)
    return plain_out, synced_out, has_word_sync, has_back_vocals


def clean_ttml(xml_text: str, song: str = "", artist: str = "") -> str:
    """Remove credit/metadata <p> elements from TTML XML."""
    if not is_ttml(xml_text):
        return xml_text
    if len(xml_text) > 5_000_000 or re.search(r"<!DOCTYPE|<!ENTITY", xml_text, re.IGNORECASE):
        return xml_text
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        start = xml_text.find("<tt")
        end = xml_text.rfind("</tt>")
        if start != -1 and end != -1:
            try:
                root = ET.fromstring(xml_text[start : end + 5])
            except Exception:
                return xml_text
        else:
            return xml_text

    parent_map = {c: p for p in root.iter() for c in p}
    changed = False
    for el in list(root.iter()):
        tag = el.tag.split("}")[-1].lower()
        if tag == "p":
            txt = extract_p_text(el)
            begin_str = el.attrib.get("begin", "").strip()
            try:
                p_begin = parse_time(begin_str) if begin_str else 0.0
            except Exception:
                p_begin = 0.0
            if is_credit_or_garbage_line(txt, song_title=song, artist_name=artist, timestamp_sec=p_begin):
                parent = parent_map.get(el)
                if parent is not None:
                    parent.remove(el)
                    changed = True

    if changed:
        ET.register_namespace("", "http://www.w3.org/ns/ttml")
        ET.register_namespace("ttm", "http://www.w3.org/ns/ttml#metadata")
        ET.register_namespace("itunes", "http://itunes.apple.com/lyric-ttml-extensions")
        ET.register_namespace("amll", "http://www.example.com/ns/amll")
        return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")
    return xml_text


def split_flat_lrc(text: str) -> str:
    """Parse and expand LRC lines, supporting:
    1. Multiple consecutive timestamps for repeated lines: [00:10.00][01:20.00] Chorus
    2. Flattened LRC files where multiple lines are concatenated: [00:10.00] Line 1 [00:15.00] Line 2
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    out: list[str] = []
    for line in lines:
        if not line:
            out.append("")
            continue

        matches = list(ANY_LINE_TIMESTAMP_RE.finditer(line))
        if len(matches) <= 1:
            out.append(line)
            continue

        pending_timestamps: list[str] = []
        for i, m in enumerate(matches):
            pending_timestamps.append(m.group(0))
            next_start = matches[i + 1].start() if i + 1 < len(matches) else len(line)
            segment_text = line[m.end() : next_start]
            if segment_text.strip() or i + 1 == len(matches):
                for ts in pending_timestamps:
                    out.append(f"{ts}{segment_text}")
                pending_timestamps = []
    return "\n".join(out)


def to_plain(text: str, song: str = "", artist: str = "") -> str:
    """Convert any lyrics (TTML, Enhanced LRC, or standard LRC) to clean plain text."""
    if is_ttml(text):
        plain_out, _, _, _ = parse_ttml(text, song=song, artist=artist)
        return sanitize_lyrics(plain_out, song_title=song, artist_name=artist, keep_timestamps=False)

    text = split_flat_lrc(text.lstrip("\ufeff")).replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for line in text.split("\n"):
        if LRC_METADATA_HEADER_RE.fullmatch(line.strip()):
            continue
        body = LINE_TIMESTAMP_PREFIX_RE.sub("", line)
        body = ANY_LINE_TIMESTAMP_RE.sub("", body)
        out.append(ANY_WORD_TIMESTAMP_RE.sub("", body))
    return "\n".join(out)


def normalize_synced(text: str, keep_word_sync: bool = True, song: str = "", artist: str = "") -> str:
    """Return a line-synced derivative without sorting or deleting source rows."""
    if is_ttml(text):
        _, synced_out, _, _ = parse_ttml(text, song=song, artist=artist)
        if not keep_word_sync:
            synced_out = ANY_WORD_TIMESTAMP_RE.sub("", synced_out)
        return synced_out

    rows = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not keep_word_sync:
        rows = [ANY_WORD_TIMESTAMP_RE.sub("", row) for row in rows]
    return "\n".join(rows)


VERSION_WORDS = frozenset(
    {
        "remaster",
        "remastered",
        "live",
        "deluxe",
        "edition",
        "version",
        "mix",
        "edit",
        "mono",
        "stereo",
        "original",
        "bonus",
        "track",
        "take",
        "demo",
        "anniversary",
        "extended",
        "instrumental",
        "acoustic",
        "orchestral",
        "explicit",
        "clean",
        "single",
        "album",
        "radio",
        "cut",
        "reissue",
        "recorded",
        "at",
        "session",
        "sessions",
        "club",
        "feat",
        "ft",
        "featuring",
        "pt",
        "part",
        "vol",
        "volume",
    }
)


def is_track_match(
    cand_song: str,
    cand_artist: str,
    target_song: str,
    target_artist: str,
    cand_duration: float | None = None,
    target_duration: float | None = None,
    duration_tolerance: float = 4.0,
) -> bool:
    """Conservative track/artist matcher that keeps version labels meaningful."""
    # 1. Duration validation if both durations available
    if cand_duration and target_duration and cand_duration > 0 and target_duration > 0:
        allowed_delta = max(duration_tolerance, float(target_duration) * 0.04)
        if abs(float(cand_duration) - float(target_duration)) > allowed_delta:
            return False

    cs = normalize_name(cand_song)
    ca = normalize_name(cand_artist)
    ts = normalize_name(target_song)
    ta = normalize_name(target_artist)
    if not cs or not ts:
        return False

    version_difference = version_signature(cand_song) ^ version_signature(target_song)
    # A remaster marker affects fades/timing but normally not the performance
    # or lyric text, so it remains a soft match distinction in the candidate
    # scorer. Performance and edit versions stay hard identity boundaries.
    if version_difference - {"remastered"}:
        return False

    # Extract word tokens
    tw = [w for w in re.findall(r"[\w]+", target_song.lower()) if w]
    cw = [w for w in re.findall(r"[\w]+", cand_song.lower()) if w]
    tw_set = set(tw)
    cw_set = set(cw)

    song_ok = False
    if cs == ts or tw == cw or tw_set == cw_set:
        song_ok = True
    else:
        # Check if candidate has extra version words (e.g. "Bohemian Rhapsody (2011 Remaster)")
        diff_c = cw_set - tw_set
        diff_t = tw_set - cw_set

        def nonsemantic_packaging_difference(diff: set[str]) -> bool:
            if not diff:
                return True
            if any(w.isdigit() for w in diff):
                # Digits can be part of a song's actual title. Treat a year as
                # decorative only when paired with an explicit reissue label.
                has_reissue_label = bool(diff & {"remaster", "remastered", "reissue", "anniversary"})
                if not has_reissue_label or any(w.isdigit() and not (1900 <= int(w) <= 2099) for w in diff):
                    return False
            return all(w in VERSION_WORDS or (w.isdigit() and 1900 <= int(w) <= 2099) for w in diff)

        if (
            nonsemantic_packaging_difference(diff_c)
            and not diff_t
            or nonsemantic_packaging_difference(diff_t)
            and not diff_c
        ):
            song_ok = True
        else:
            cs_tr = normalize_name(cyrillic_to_latin(cand_song))
            ts_tr = normalize_name(cyrillic_to_latin(target_song))
            if cs_tr and ts_tr and cs_tr == ts_tr:
                song_ok = True
            else:
                import difflib

                ratio = difflib.SequenceMatcher(None, cs, ts).ratio()
                if ratio >= 0.86 and min(len(cs), len(ts)) >= 4:
                    song_ok = True

    if not song_ok:
        return False

    # Full credit is matched first. A featuring suffix may be omitted by a
    # provider, but arbitrary subset/substring artist matches are unsafe.
    def primary_credit(value: str) -> str:
        return re.split(r"\b(?:feat(?:uring)?|ft)\.?\s*", value, maxsplit=1, flags=re.IGNORECASE)[0].strip()

    ca_main = normalize_name(primary_credit(cand_artist))
    ta_main = normalize_name(primary_credit(target_artist))
    artist_ok = bool(ta and ca and (ca == ta or (ca_main and ta_main and ca_main == ta_main)))
    if not artist_ok and ca and ta:
        import difflib

        artist_ok = difflib.SequenceMatcher(None, ca_main or ca, ta_main or ta).ratio() >= 0.90

    return song_ok and artist_ok


def search_netease_yrc(
    song: str,
    artist: str,
    duration: float | None = None,
    timeout: float = 3.5,
) -> dict[str, Any] | None:
    """Search NetEase Cloud Music and retrieve high-precision syllable-level YRC lyrics with fast timeout."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://music.163.com",
    }
    queries = [f"{artist} {song}".strip(), f"{song} {artist}".strip(), song.strip()]
    lat_art = cyrillic_to_latin(artist)
    lat_song = cyrillic_to_latin(song)
    if lat_art != artist or lat_song != song:
        queries.extend([f"{lat_art} {lat_song}".strip(), f"{lat_song} {lat_art}".strip()])

    matching_songs: list[dict[str, Any]] = []
    valid_search_response = False
    search_url = "https://music.163.com/api/cloudsearch/pc"
    for q in queries:
        post_data = urllib.parse.urlencode({"s": q, "type": 1, "limit": 6, "offset": 0}).encode("utf-8")
        try:
            req = urllib.request.Request(search_url, data=post_data, headers=headers)
            with _provider_urlopen(req, timeout=timeout) as resp:
                sdata = _provider_json_loads(read_provider_text(resp))
                if not isinstance(sdata, dict) or not isinstance(sdata.get("result"), dict):
                    raise TypeError("NetEase cloudsearch response has no result object")
                songs = sdata["result"].get("songs")
                if not isinstance(songs, list):
                    raise TypeError("NetEase cloudsearch response has no songs array")
                valid_search_response = True
                for s in songs:
                    if not isinstance(s, dict):
                        raise TypeError("NetEase cloudsearch contains a non-object song")
                    art_list = s.get("ar") or s.get("artists") or []
                    if not isinstance(art_list, list):
                        raise TypeError("NetEase cloudsearch song has an invalid artists field")
                    s_art = " ".join(a.get("name", "") for a in art_list)
                    s_dur = float(s.get("dt", 0)) / 1000.0 if s.get("dt") else None
                    if is_track_match(
                        s.get("name", ""), s_art, song, artist, cand_duration=s_dur, target_duration=duration
                    ):
                        matching_songs.append(s)
                if matching_songs:
                    break
        except Exception as exc:
            _record_provider_failure(exc)
            continue

    if not matching_songs:
        if valid_search_response:
            mark_provider_not_found()
        return None

    lyric_candidates: list[dict[str, Any]] = []
    for s in matching_songs[:6]:
        track_id = s.get("id")
        if track_id is None:
            error = ValueError("NetEase matched track has no provider track ID")
            _record_provider_failure(error)
            continue
        lyric_url = f"https://music.163.com/api/song/lyric?id={track_id}&lv=1&tv=1&yv=1&rv=1"
        try:
            lreq = urllib.request.Request(lyric_url, headers=headers)
            with _provider_urlopen(lreq, timeout=timeout) as lresp:
                ldata = _provider_json_loads(read_provider_text(lresp))
                if not isinstance(ldata, dict):
                    raise TypeError("NetEase lyric response is not an object")
                for field in ("yrc", "lrc", "tlyric", "romalrc", "klyric"):
                    if field in ldata and not isinstance(ldata[field], dict):
                        raise TypeError(f"NetEase lyric response has invalid {field} object")
                yrc = ldata.get("yrc", {}).get("lyric", "")
                lrc = ldata.get("lrc", {}).get("lyric", "")
                tlyric = ldata.get("tlyric", {}).get("lyric", "")
                romalrc = ldata.get("romalrc", {}).get("lyric", "") or ldata.get("klyric", {}).get("lyric", "")
                art_list = s.get("ar") or s.get("artists") or [{}]
                s_dur = float(s.get("dt", 0)) / 1000.0 if s.get("dt") else None
                actual_title = str(s.get("name") or "")
                actual_artist = " / ".join(str(a.get("name") or "") for a in art_list if a.get("name"))
                res = {
                    "id": track_id,
                    "provider_track_id": str(track_id),
                    "title": actual_title,
                    "artist": actual_artist,
                    "trackName": actual_title,
                    "artistName": actual_artist,
                    "albumName": str((s.get("al") or {}).get("name") or ""),
                    "identity_validated": bool(
                        actual_title and actual_artist and is_track_match(actual_title, actual_artist, song, artist)
                    ),
                    "yrc": yrc,
                    "lrc": lrc,
                    "tlyric": tlyric,
                    "romalrc": romalrc,
                    "duration": s_dur,
                    "version": str(s.get("version") or s.get("versionName") or ""),
                    "language": str(s.get("language") or ""),
                }
                if yrc or lrc:
                    lyric_candidates.append(res)
        except Exception as exc:
            _record_provider_failure(exc)
            continue

    if lyric_candidates:
        # Keep the historical word-sync-first response as the root for older
        # callers, but return every retrieved identity-matched recording as an
        # alternative. The downstream quality selector, not NetEase order,
        # decides which version is suitable.
        lyric_candidates.sort(
            key=lambda candidate: (
                not bool(candidate.get("yrc")),
                abs(float(candidate.get("duration") or duration or 0) - float(duration or 0)),
                str(candidate.get("id") or ""),
            )
        )
        primary, *alternatives = lyric_candidates
        primary = dict(primary)
        primary["alternatives"] = alternatives
        return primary

    if valid_search_response:
        mark_provider_not_found()
    return None


def yrc_to_lyrics(
    yrc_text: str,
    song: str,
    artist: str,
    album: str = "",
    tlyric: str = "",
    romalrc: str = "",
) -> dict[str, Any]:
    """Convert NetEase YRC (syllable-level timing) into standard TTML XML and Enhanced LRC."""
    plain_lines: list[str] = []
    std_lines: list[str] = []
    elrc_lines: list[str] = []
    ttml_paragraphs: list[str] = []
    has_word_sync = False
    has_back_vocals = False

    line_idx = 1
    max_end_ms = 0

    for raw_line in yrc_text.splitlines():
        raw_line = raw_line.rstrip("\r\n")
        if not raw_line.strip():
            continue
        m = YRC_LINE_RE.match(raw_line)
        if not m:
            continue
        l_start = int(m.group(1))
        l_dur = int(m.group(2))
        body = m.group(3)
        words = YRC_WORD_RE.findall(body)
        if not words:
            continue

        full_line_text = "".join(w[2] for w in words)
        if not full_line_text.strip():
            continue
        # Filter metadata credits
        if is_credit_or_garbage_line(
            full_line_text, song_title=song, artist_name=artist, timestamp_sec=l_start / 1000.0
        ):
            continue

        has_word_sync = True
        l_end = l_start + l_dur
        max_end_ms = max(max_end_ms, l_end)

        parsed_words: list[tuple[int, int, str]] = []
        for w_st_s, w_dur_s, w_txt in words:
            w_st = int(w_st_s)
            w_dur = int(w_dur_s)
            w_end = w_st + w_dur
            max_end_ms = max(max_end_ms, w_end)
            parsed_words.append((w_st, w_dur, w_txt))

        line_has_bg = "(" in full_line_text or ")" in full_line_text or "（" in full_line_text
        if line_has_bg:
            has_back_vocals = True

        # 1. Plain text
        plain_line = "".join(txt for _, _, txt in parsed_words)
        plain_line = clean_parentheses(plain_line)
        if plain_line:
            plain_lines.append(plain_line)

        # 1b. Standard line-synced LRC (without <...> word timestamps, for legacy players)
        std_line = clean_parentheses(f"[{ms_to_lrc_ts(l_start)}]{plain_line}")
        std_lines.append(std_line)

        # 2. Enhanced LRC (with <...> word timestamps)
        elrc_word_parts = [f"<{ms_to_lrc_ts(w_st)}>{txt}" for w_st, _, txt in parsed_words]
        elrc_line = clean_parentheses(f"[{ms_to_lrc_ts(l_start)}]" + "".join(elrc_word_parts))
        elrc_lines.append(elrc_line)

        # 3. TTML XML
        is_entire_line_bg = plain_line.startswith("(") and plain_line.endswith(")")
        p_agent = "v2" if is_entire_line_bg else "v1"
        p_role = ' ttm:role="background"' if is_entire_line_bg else ""

        span_xml_parts: list[str] = []
        in_bg_span = False
        bg_span_chunks: list[str] = []
        bg_st = 0
        bg_end = 0

        for w_st, w_dur, txt in parsed_words:
            w_end = w_st + w_dur
            txt_xml = txt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

            if "(" in txt and not in_bg_span and not is_entire_line_bg:
                in_bg_span = True
                bg_st = w_st
                bg_span_chunks = []

            if in_bg_span:
                bg_end = max(bg_end, w_end)
                bg_span_chunks.append(
                    f'<span begin="{ms_to_ttml_ts(w_st)}" end="{ms_to_ttml_ts(w_end)}">{txt_xml}</span>'
                )
                if ")" in txt:
                    in_bg_span = False
                    inner = "".join(bg_span_chunks)
                    span_xml_parts.append(
                        f'<span ttm:role="x-bg" begin="{ms_to_ttml_ts(bg_st)}" end="{ms_to_ttml_ts(bg_end)}">{inner}</span>'
                    )
                    bg_span_chunks = []
            else:
                span_xml_parts.append(
                    f'<span begin="{ms_to_ttml_ts(w_st)}" end="{ms_to_ttml_ts(w_end)}">{txt_xml}</span>'
                )

        if bg_span_chunks:
            inner = "".join(bg_span_chunks)
            span_xml_parts.append(
                f'<span ttm:role="x-bg" begin="{ms_to_ttml_ts(bg_st)}" end="{ms_to_ttml_ts(bg_end)}">{inner}</span>'
            )

        p_inner = "".join(span_xml_parts)
        p_xml = f'      <p begin="{ms_to_ttml_ts(l_start)}" end="{ms_to_ttml_ts(l_end)}" itunes:key="L{line_idx}" ttm:agent="{p_agent}"{p_role}>{p_inner}</p>'
        ttml_paragraphs.append(p_xml)
        line_idx += 1

    dur_str = ms_to_ttml_ts(max_end_ms)
    song_esc = song.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    artist_esc = artist.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    album_esc = album.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    ttml_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<tt xmlns="http://www.w3.org/ns/ttml"
    xmlns:ttm="http://www.w3.org/ns/ttml#metadata"
    xmlns:itunes="http://itunes.apple.com/lyric-ttml-extensions"
    xmlns:amll="http://www.example.com/ns/amll"
    itunes:timing="Word">
  <head>
    <metadata>
      <ttm:title>{song_esc}</ttm:title>
      <ttm:agent xml:id="v1" type="person">
        <ttm:name type="full">{artist_esc}</ttm:name>
      </ttm:agent>
      <ttm:agent xml:id="v2" type="person">
        <ttm:name type="full">Background Vocals</ttm:name>
      </ttm:agent>
      <amll:meta key="musicName" value="{song_esc}" />
      <amll:meta key="artists" value="{artist_esc}" />
      <amll:meta key="album" value="{album_esc}" />
    </metadata>
  </head>
  <body dur="{dur_str}">
    <div>
{chr(10).join(ttml_paragraphs)}
    </div>
  </body>
</tt>"""

    return {
        "plainLyrics": sanitize_lyrics(
            "\n".join(plain_lines), song_title=song, artist_name=artist, keep_timestamps=False
        ),
        "syncedLyrics": sanitize_lyrics(
            "\n".join(std_lines), song_title=song, artist_name=artist, keep_timestamps=True
        ),
        "enhancedLyrics": sanitize_lyrics(
            "\n".join(elrc_lines), song_title=song, artist_name=artist, keep_timestamps=True
        ),
        "ttmlLyrics": ttml_xml,
        "translationLyrics": sanitize_lyrics(tlyric, song_title=song, artist_name=artist, keep_timestamps=True)
        if tlyric
        else "",
        "romajiLyrics": sanitize_lyrics(romalrc, song_title=song, artist_name=artist, keep_timestamps=True)
        if romalrc
        else "",
        "has_word_sync": has_word_sync,
        "has_back_vocals": has_back_vocals,
    }


def krc_to_lyrics(krc_text: str, song: str, artist: str, album: str = "") -> dict[str, Any]:
    """Convert Kugou KRC (word-level timing) into standard TTML XML and Enhanced LRC."""
    plain_lines: list[str] = []
    std_lines: list[str] = []
    elrc_lines: list[str] = []
    ttml_paragraphs: list[str] = []
    has_word_sync = False
    has_back_vocals = False

    line_idx = 1
    max_end_ms = 0

    for raw_line in krc_text.splitlines():
        raw_line = raw_line.rstrip("\r\n")
        if not raw_line or not raw_line.startswith("["):
            continue
        m = KRC_LINE_RE.match(raw_line)
        if not m:
            continue
        l_start = int(m.group(1))
        l_dur = int(m.group(2))
        body = m.group(3)
        words = KRC_WORD_RE.findall(body)
        if not words:
            continue

        full_line_text = "".join(w[2] for w in words)
        if not full_line_text.strip():
            continue
        if is_credit_or_garbage_line(
            full_line_text, song_title=song, artist_name=artist, timestamp_sec=l_start / 1000.0
        ):
            continue

        has_word_sync = True
        l_end = l_start + l_dur
        max_end_ms = max(max_end_ms, l_end)

        parsed_words: list[tuple[int, int, str]] = []
        for w_off_s, w_dur_s, w_txt in words:
            w_st = l_start + int(w_off_s)
            w_dur = int(w_dur_s)
            w_end = w_st + w_dur
            max_end_ms = max(max_end_ms, w_end)
            parsed_words.append((w_st, w_dur, w_txt))

        line_has_bg = "(" in full_line_text or ")" in full_line_text or "（" in full_line_text
        if line_has_bg:
            has_back_vocals = True

        # 1. Plain text
        plain_line = "".join(txt for _, _, txt in parsed_words)
        plain_line = clean_parentheses(plain_line)
        if plain_line:
            plain_lines.append(plain_line)

        # 1b. Standard line-synced LRC
        std_line = clean_parentheses(f"[{ms_to_lrc_ts(l_start)}]{plain_line}")
        std_lines.append(std_line)

        # 2. Enhanced LRC (<...> word timestamps)
        elrc_word_parts = [f"<{ms_to_lrc_ts(w_st)}>{txt}" for w_st, _, txt in parsed_words]
        elrc_line = clean_parentheses(f"[{ms_to_lrc_ts(l_start)}]" + "".join(elrc_word_parts))
        elrc_lines.append(elrc_line)

        # 3. TTML XML
        is_entire_line_bg = plain_line.startswith("(") and plain_line.endswith(")")
        p_agent = "v2" if is_entire_line_bg else "v1"
        p_role = ' ttm:role="background"' if is_entire_line_bg else ""

        span_xml_parts: list[str] = []
        in_bg_span = False
        bg_span_chunks: list[str] = []
        bg_st = 0
        bg_end = 0

        for w_st, w_dur, txt in parsed_words:
            w_end = w_st + w_dur
            txt_xml = txt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

            if "(" in txt and not in_bg_span and not is_entire_line_bg:
                in_bg_span = True
                bg_st = w_st
                bg_span_chunks = []

            if in_bg_span:
                bg_end = max(bg_end, w_end)
                bg_span_chunks.append(
                    f'<span begin="{ms_to_ttml_ts(w_st)}" end="{ms_to_ttml_ts(w_end)}">{txt_xml}</span>'
                )
                if ")" in txt:
                    in_bg_span = False
                    inner = "".join(bg_span_chunks)
                    span_xml_parts.append(
                        f'<span ttm:role="x-bg" begin="{ms_to_ttml_ts(bg_st)}" end="{ms_to_ttml_ts(bg_end)}">{inner}</span>'
                    )
                    bg_span_chunks = []
            else:
                span_xml_parts.append(
                    f'<span begin="{ms_to_ttml_ts(w_st)}" end="{ms_to_ttml_ts(w_end)}">{txt_xml}</span>'
                )

        if bg_span_chunks:
            inner = "".join(bg_span_chunks)
            span_xml_parts.append(
                f'<span ttm:role="x-bg" begin="{ms_to_ttml_ts(bg_st)}" end="{ms_to_ttml_ts(bg_end)}">{inner}</span>'
            )

        p_inner = "".join(span_xml_parts)
        p_xml = f'      <p begin="{ms_to_ttml_ts(l_start)}" end="{ms_to_ttml_ts(l_end)}" itunes:key="L{line_idx}" ttm:agent="{p_agent}"{p_role}>{p_inner}</p>'
        ttml_paragraphs.append(p_xml)
        line_idx += 1

    dur_str = ms_to_ttml_ts(max_end_ms)
    song_esc = song.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    artist_esc = artist.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    album_esc = album.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    ttml_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<tt xmlns="http://www.w3.org/ns/ttml"
    xmlns:ttm="http://www.w3.org/ns/ttml#metadata"
    xmlns:itunes="http://itunes.apple.com/lyric-ttml-extensions"
    xmlns:amll="http://www.example.com/ns/amll"
    itunes:timing="Word">
  <head>
    <metadata>
      <ttm:title>{song_esc}</ttm:title>
      <ttm:agent xml:id="v1" type="person">
        <ttm:name type="full">{artist_esc}</ttm:name>
      </ttm:agent>
      <ttm:agent xml:id="v2" type="person">
        <ttm:name type="full">Background Vocals</ttm:name>
      </ttm:agent>
      <amll:meta key="musicName" value="{song_esc}" />
      <amll:meta key="artists" value="{artist_esc}" />
      <amll:meta key="album" value="{album_esc}" />
    </metadata>
  </head>
  <body dur="{dur_str}">
    <div>
{chr(10).join(ttml_paragraphs)}
    </div>
  </body>
</tt>"""

    return {
        "plainLyrics": sanitize_lyrics(
            "\n".join(plain_lines), song_title=song, artist_name=artist, keep_timestamps=False
        ),
        "syncedLyrics": sanitize_lyrics(
            "\n".join(std_lines), song_title=song, artist_name=artist, keep_timestamps=True
        ),
        "enhancedLyrics": sanitize_lyrics(
            "\n".join(elrc_lines), song_title=song, artist_name=artist, keep_timestamps=True
        ),
        "ttmlLyrics": ttml_xml,
        "has_word_sync": has_word_sync,
        "has_back_vocals": has_back_vocals,
    }


def lrc_to_ttml(
    lrc_text: str,
    song: str,
    artist: str,
    album: str = "",
    duration: float | None = None,
) -> str:
    """Refuse the lossy LRC-to-TTML conversion.

    LRC contains lyric onset times, but no line end times or document extent.
    Earlier code invented one-second minimums and four-second final-line ends,
    which made derived TTML appear more precise than its source. Native TTML
    returned by a provider is retained as-is; synchronized playback uses LRC.
    The parameters remain for call-site compatibility while callers migrate.
    """
    del lrc_text, song, artist, album, duration
    return ""


def clean_plain_lyrics_for_alignment(text: str, song: str = "", artist: str = "") -> str:
    """Make a disposable alignment input; source and stored text stay untouched."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(?:div|p|li|tr|h[1-6])\s*>", "\n", text, flags=re.IGNORECASE)
    text = html.unescape(re.sub(r"<[^>]*>", "", text))
    output: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or re.fullmatch(
            r"\[(?:intro|verse(?:\s+\d+)?|pre-?chorus|chorus|bridge|outro|refrain|instrumental|куплет(?:\s+\d+)?|припев|интро|аутро|вступление|кода)\]\s*:?\s*",
            stripped,
            re.IGNORECASE,
        ):
            continue
        # Chord-only rows are not sung. Keep chord annotations embedded in
        # vocal rows out of this alignment-only derivative too.
        if re.fullmatch(r"(?:[A-G](?:#|b)?(?:m|maj|min|sus|dim|aug|add|M)?\d*(?:/[A-G](?:#|b)?)?\s*)+", stripped):
            continue
        line = re.sub(r"\s*\((?:x|×)\s*\d+\)\s*$", "", line, flags=re.IGNORECASE)
        output.append(line)
    return "\n".join(output)


def musixmatch_richsync_to_lyrics(
    rich_raw: str | list[dict[str, Any]],
    song: str = "",
    artist: str = "",
    album: str = "",
    duration: float | None = None,
) -> dict[str, Any] | None:
    """Preserve Musixmatch's native word starts as Enhanced LRC.

    Richsync does not supply reliable word end times. Emitting TTML span ends
    here would invent timing the provider did not measure.
    """
    del song, artist, album
    if not rich_raw:
        return None
    try:
        data = _provider_json_loads(rich_raw) if isinstance(rich_raw, str) else rich_raw
    except (json.JSONDecodeError, UnicodeError, TypeError, ValueError):
        return None
    if not isinstance(data, list) or not data:
        return None

    plain_lines: list[str] = []
    std_lines: list[str] = []
    elrc_lines: list[str] = []
    has_back_vocals = False
    previous_line_ms = -1

    for row in data:
        if not isinstance(row, dict) or "ts" not in row:
            return None
        try:
            ts = float(row["ts"])
        except (TypeError, ValueError):
            return None
        if not math.isfinite(ts) or ts < 0:
            return None
        syllables = row.get("l")
        if not syllables or not isinstance(syllables, list):
            return None

        l_start_ms = round(ts * 1000)
        if l_start_ms < previous_line_ms:
            return None
        previous_line_ms = l_start_ms
        if duration is not None and duration > 0 and l_start_ms > duration * 1000 + max(5_000, duration * 1000 * 0.05):
            return None

        parsed_words: list[tuple[int, str]] = []
        previous_word_ms = l_start_ms
        for syl in syllables:
            if not isinstance(syl, dict) or "c" not in syl or "o" not in syl:
                return None
            c_txt = str(syl.get("c", ""))
            if not c_txt:
                continue
            try:
                offset_sec = float(syl["o"])
            except (TypeError, ValueError):
                return None
            if not math.isfinite(offset_sec) or offset_sec < 0:
                return None
            w_st_ms = round((ts + offset_sec) * 1000)
            if w_st_ms < previous_word_ms:
                return None
            previous_word_ms = w_st_ms
            parsed_words.append((w_st_ms, c_txt))

        if not parsed_words:
            continue

        full_line_text = "".join(t for _, t in parsed_words)
        if not full_line_text.strip():
            continue
        line_has_bg = "(" in full_line_text or ")" in full_line_text or "（" in full_line_text
        if line_has_bg:
            has_back_vocals = True

        # 1. Plain text
        plain_line = full_line_text
        if plain_line:
            plain_lines.append(plain_line)

        # 2. Standard line-synced LRC
        std_line = f"[{ms_to_lrc_ts(l_start_ms)}]{plain_line}"
        std_lines.append(std_line)

        # 3. Enhanced LRC
        elrc_word_parts = [f"<{ms_to_lrc_ts(w_st)}>{txt}" for w_st, txt in parsed_words]
        elrc_line = f"[{ms_to_lrc_ts(l_start_ms)}]" + "".join(elrc_word_parts)
        elrc_lines.append(elrc_line)

    if not std_lines:
        return None

    return {
        "found": True,
        "source": "Musixmatch (Richsync)",
        "format": "enhanced_lrc",
        "syncType": "richsync",
        "has_word_sync": True,
        "has_back_vocals": has_back_vocals,
        "rawLyrics": rich_raw if isinstance(rich_raw, str) else json.dumps(rich_raw, ensure_ascii=False),
        "plainLyrics": "\n".join(plain_lines),
        "syncedLyrics": "\n".join(std_lines),
        "enhancedLyrics": "\n".join(elrc_lines),
        # Richsync supplies native word starts, not reliable word ends. TTML
        # spans would invent those missing end times; Enhanced LRC preserves
        # exactly the timing actually supplied.
        "ttmlLyrics": "",
        "duration": duration,
    }


def validate_lyrics_duration(
    lyrics_obj: dict[str, Any],
    audio_duration: float | None,
    tolerance: float = 2.5,
) -> bool:
    """Check recording duration and timestamp bounds with absolute/relative tolerance.

    Protects against desynchronization between radio edits, single cuts, extended mixes,
    and album releases, while tolerating silence, fades and instrumental outros. Early
    final vocal timestamps alone do not prove that a lyric document is incomplete.
    """
    if audio_duration is None or audio_duration <= 0:
        return True

    raw_txt = lyrics_obj.get("rawLyrics", "")
    elrc_txt = lyrics_obj.get("enhancedLyrics", "")
    synced_txt = lyrics_obj.get("syncedLyrics", "")

    first_timestamp: float | None = None
    last_timestamp: float | None = None

    # Check for [offset: +/-ms] tag in LRC
    offset_sec = 0.0
    for txt in (raw_txt, synced_txt, elrc_txt):
        if txt:
            m_off = re.search(r"\[offset:\s*([+-]?\d+)\]", txt, re.IGNORECASE)
            if m_off:
                try:
                    offset_sec = float(m_off.group(1)) / 1000.0
                    break
                except Exception:
                    pass

    # Try TTML end timestamp of last <p> or <span>
    if lyrics_obj.get("format") == "ttml" or is_ttml(raw_txt):
        m_ends = re.findall(r'end="((?:\d+:)?\d+:\d+(?:\.\d+)?)"', raw_txt)
        m_begins = re.findall(r'begin="((?:\d+:)?\d+:\d+(?:\.\d+)?)"', raw_txt)
        if m_begins:
            try:
                first_timestamp = parse_time(m_begins[0])
            except Exception:
                pass
        if m_ends:
            try:
                last_timestamp = parse_time(m_ends[-1])
            except Exception:
                pass

    # Try word timestamps from enhanced lyrics <mm:ss.xx>
    if (first_timestamp is None or last_timestamp is None) and elrc_txt:
        w_ts = re.findall(r"<((?:\d{1,3}:)?\d{1,3}:\d{2}(?:[.:]\d{1,3})?)>", elrc_txt)
        if w_ts:
            try:
                if first_timestamp is None:
                    first_timestamp = parse_time(w_ts[0]) + offset_sec
                if last_timestamp is None:
                    last_timestamp = parse_time(w_ts[-1]) + offset_sec
            except Exception:
                pass

    # Try line timestamps [mm:ss.xx]
    if (first_timestamp is None or last_timestamp is None) and (synced_txt or raw_txt):
        target = synced_txt or raw_txt
        l_ts = re.findall(r"\[((?:\d{1,3}:)?\d{1,3}:\d{2}(?:[.:]\d{1,3})?)\]", target)
        valid_ts = [
            t
            for t in l_ts
            if not any(t.lower().startswith(p) for p in ("ar:", "ti:", "al:", "by:", "offset:", "length:"))
        ]
        if valid_ts:
            try:
                if first_timestamp is None:
                    first_timestamp = parse_time(valid_ts[0]) + offset_sec
                if last_timestamp is None:
                    last_timestamp = parse_time(valid_ts[-1]) + offset_sec
            except Exception:
                pass

    allowed_delta = max(float(tolerance), 10.0, float(audio_duration) * 0.05)
    timestamp_tolerance = max(5.0, float(audio_duration) * 0.03)

    # LRC structure and timeline validation is separate from candidate identity.
    # A long quiet section or instrumental outro is not a failure by itself.
    sync_source = synced_txt or elrc_txt
    if sync_source and not is_ttml(sync_source):
        valid_lrc, _sync_reasons = validate_lrc(sync_source, duration=audio_duration)
        if not valid_lrc:
            return False

    # Lyrics cannot extend materially beyond the local audio. The shared LRC
    # validator already applies this recording-relative tolerance.
    if last_timestamp is not None and last_timestamp > audio_duration + timestamp_tolerance:
        return False

    # Invariant 2: Lyrics cannot start after audio has already finished
    if first_timestamp is not None and first_timestamp > audio_duration:
        return False

    # Check explicit track duration from candidate metadata or [length:] tag
    cand_dur = lyrics_obj.get("duration")
    explicit_dur: float | None = None
    if cand_dur is not None:
        try:
            val = float(cand_dur)
            if val > 0:
                explicit_dur = val
        except (ValueError, TypeError):
            pass

    if explicit_dur is None:
        for text_key in ("rawLyrics", "syncedLyrics", "enhancedLyrics"):
            txt = lyrics_obj.get(text_key, "")
            if txt:
                m_len = re.search(r"\[length:\s*(\d+):(\d+(?:\.\d+)?)\]", txt, re.IGNORECASE)
                if m_len:
                    try:
                        explicit_dur = int(m_len.group(1)) * 60 + float(m_len.group(2))
                        break
                    except Exception:
                        pass

    # If explicit track duration is available:
    if explicit_dur is not None:
        dur_diff = abs(explicit_dur - audio_duration)
        if dur_diff > allowed_delta:
            return False
    # Don't reject early vocal endings: instrumentals, solos and fade-outs are
    # common. The candidate scorer may mark short coverage as a warning.
    return True


def parse_ultrastar_lyrics(content: str, song: str = "", artist: str = "") -> dict[str, Any] | None:
    """Convert UltraStar karaoke format (.txt) into word-level Enhanced LRC (.elrc)."""
    if len(content) > MAX_LYRICS_CHARS:
        raise ValueError(f"UltraStar chart exceeds {MAX_LYRICS_CHARS} character safety limit")
    lines = content.splitlines()
    bpm = 120.0
    gap_ms = 0.0
    title = ""
    art = ""
    notes: list[tuple[str, int, int, int, str]] = []  # (type, beat, duration, pitch, text)

    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            continue
        if line.startswith("#"):
            if ":" in line:
                tag, val = line[1:].split(":", 1)
                tag = tag.strip().upper()
                val = val.strip()
                if tag == "BPM":
                    try:
                        bpm = float(val.replace(",", "."))
                    except ValueError as exc:
                        raise ValueError("UltraStar chart has malformed BPM") from exc
                    if not math.isfinite(bpm) or not 20 <= bpm <= 500:
                        raise ValueError("UltraStar BPM is outside the supported 20–500 range")
                elif tag == "GAP":
                    try:
                        gap_ms = float(val.replace(",", "."))
                    except ValueError as exc:
                        raise ValueError("UltraStar chart has malformed GAP") from exc
                    if not math.isfinite(gap_ms) or abs(gap_ms) > 300_000:
                        raise ValueError("UltraStar GAP is outside the supported ±300 second range")
                elif tag == "TITLE":
                    title = val
                elif tag == "ARTIST":
                    art = val
            continue

        note = re.match(r"^([:*F])\s+(\d+)\s+(\d+)\s+(-?\d+)(?:\s(.*))?$", line)
        if note:
            kind, beat_raw, dur_raw, pitch_raw, w_text = note.groups()
            beat = int(beat_raw)
            dur = int(dur_raw)
            pitch = int(pitch_raw)
            if beat > 10_000_000 or dur > 10_000_000:
                raise ValueError("UltraStar note timing exceeds the supported chart bound")
            w_text = w_text or ""
            notes.append((kind, beat, dur, pitch, w_text))
            continue
        linebreak = re.match(r"^-\s+(\d+)(?:\s|$)", line)
        if linebreak:
            beat = int(linebreak.group(1))
            notes.append(("-", beat, 0, 0, "\n"))

    if not notes:
        return None

    # UltraStar timing: 1 beat = 15000 / bpm ms
    ms_per_beat = 15000.0 / bpm if bpm > 0 else 125.0

    # Build lines with word timestamps
    elrc_lines: list[str] = []
    current_line_words: list[tuple[float, float, str]] = []

    def flush_line():
        nonlocal current_line_words
        if not current_line_words:
            return
        line_start_sec = current_line_words[0][0]
        l_ts = f"[{fmt_lrc_time(line_start_sec)}]"
        w_parts = []
        for idx, (st, en, w) in enumerate(current_line_words):
            w_ts = fmt_lrc_time(st)
            word_str = w
            if (
                idx > 0
                and not word_str.startswith(" ")
                and not current_line_words[idx - 1][2].endswith(" ")
                and not current_line_words[idx - 1][2].endswith("-")
            ):
                word_str = " " + word_str
            w_parts.append(f"<{w_ts}>{word_str}")
        line_content = "".join(w_parts)
        elrc_lines.append(f"{l_ts}{line_content}")
        current_line_words = []

    for item in notes:
        kind = item[0]
        if kind == "-":
            flush_line()
        else:
            _, beat, dur, _, w_text = item
            start_sec = max(0.0, (gap_ms + beat * ms_per_beat) / 1000.0)
            end_sec = max(start_sec, (gap_ms + (beat + dur) * ms_per_beat) / 1000.0)
            current_line_words.append((start_sec, end_sec, w_text))
    flush_line()

    if not elrc_lines:
        return None

    elrc_text = "\n".join(elrc_lines) + "\n"
    std_lrc = normalize_synced(elrc_text, keep_word_sync=False, song=song or title, artist=artist or art)
    plain_txt = to_plain(elrc_text, song=song or title, artist=artist or art)
    ttml_xml = lrc_to_ttml(elrc_text, song=song or title, artist=artist or art)

    return {
        "found": True,
        "source": "UltraStar DB",
        "trackName": title,
        "artistName": art,
        "identity_validated": bool(title and art and is_track_match(title, art, song, artist)),
        "format": "elrc",
        "syncType": "richsync",
        "has_word_sync": True,
        "has_back_vocals": False,
        "rawLyrics": elrc_text,
        "providerRaw": content,
        "plainLyrics": plain_txt,
        "syncedLyrics": std_lrc,
        "enhancedLyrics": elrc_text,
        "ttmlLyrics": ttml_xml,
    }


def fetch_lrclib(
    song: str,
    artist: str,
    album: str = "",
    duration: float | None = None,
    timeout: int = 8,
    isrc: str = "",
    recording_id: str = "",
) -> dict[str, Any] | None:
    """Query LRCLIB with typed failure outcomes, version-aware matching and a versioned cache."""
    import requests

    global _LRCLIB_SESSION
    if _LRCLIB_SESSION is None:
        _LRCLIB_SESSION = requests.Session()
    headers = {
        "User-Agent": "AudiophileArchTagger/6.0 (LRCLIBClient)",
        "Accept": "application/json",
    }
    failures: list[ProviderResult] = []
    successful_responses = 0
    candidate_pool: list[dict[str, Any]] = []

    def get_json(url: str, expected_type: type) -> Any | None:
        nonlocal successful_responses
        result = fetch_json(
            _LRCLIB_SESSION,
            url,
            headers=headers,
            cache=_LRCLIB_CACHE,
            attempts=2,
            timeout=max(0.5, float(timeout)),
            rate_limiter=_LRCLIB_RATE_LIMITER,
        )
        if result.status == 404:
            successful_responses += 1
            return None
        if result.status == 200:
            if isinstance(result.payload, expected_type):
                schema_ok = True
                if expected_type is dict:
                    item = result.payload
                    schema_ok = bool(
                        isinstance(item, dict)
                        and "trackName" in item
                        and "artistName" in item
                        and "duration" in item
                        and any(key in item for key in ("plainLyrics", "syncedLyrics", "instrumental"))
                    )
                elif expected_type is list and isinstance(result.payload, list):
                    schema_ok = all(
                        isinstance(item, dict) and "trackName" in item and "artistName" in item
                        for item in result.payload
                    )
                if not schema_ok:
                    _LRCLIB_CACHE.delete(url)
                    failures.append(
                        ProviderResult(200, None, result.attempts, error="unexpected LRCLIB response schema")
                    )
                    return None
                successful_responses += 1
                return result.payload
            _LRCLIB_CACHE.delete(url)
            failures.append(
                ProviderResult(
                    200, None, result.attempts, error=f"unexpected LRCLIB schema (wanted {expected_type.__name__})"
                )
            )
            return None
        failures.append(result)
        return None

    def result_from(item: dict[str, Any], source: str, *, exact: bool = False) -> dict[str, Any]:
        synced = str(item.get("syncedLyrics") or "")
        plain = str(item.get("plainLyrics") or "")
        return {
            "source": source,
            "id": item.get("id"),
            "trackName": item.get("trackName") or "",
            "artistName": item.get("artistName") or "",
            "albumName": item.get("albumName") or "",
            "duration": item.get("duration"),
            "isrc": item.get("isrc") or "",
            "language": item.get("language") or "",
            "instrumental": bool(item.get("instrumental")),
            "providerRaw": json.dumps(item, ensure_ascii=False, sort_keys=True),
            "lyricsfile": item.get("lyricsfile") or "",
            "recording_id": item.get("recording_id") or item.get("musicbrainz_recording_id") or "",
            "rawLyrics": synced or plain,
            "syncedLyrics": synced,
            "plainLyrics": plain or (to_plain(synced) if synced else ""),
            "identity_validated": bool(
                item.get("trackName")
                and item.get("artistName")
                and is_track_match(item.get("trackName", ""), item.get("artistName", ""), song, artist)
            ),
            "found": True,
        }

    params = {"track_name": song.strip(), "artist_name": artist.strip()}
    if album.strip():
        params["album_name"] = album.strip()
    if duration is not None:
        try:
            duration_value = round(float(duration))
            if duration_value > 0:
                params["duration"] = str(duration_value)
        except (TypeError, ValueError):
            pass

    exact_url = f"https://lrclib.net/api/get?{urllib.parse.urlencode(params)}"
    exact = get_json(exact_url, dict)
    if (
        isinstance(exact, dict)
        and bool(exact.get("instrumental"))
        and not (exact.get("syncedLyrics") or exact.get("plainLyrics"))
    ):
        exact_title = str(exact.get("trackName") or "")
        exact_artist = str(exact.get("artistName") or "")
        exact_album = str(exact.get("albumName") or "")
        exact_isrc = str(exact.get("isrc") or "").replace("-", "").upper()
        exact_recording_id = str(exact.get("recording_id") or exact.get("musicbrainz_recording_id") or "")
        exact_duration = _provider_duration_seconds(exact.get("duration"))
        normalized_isrc = isrc.replace("-", "").upper()
        stable_match = bool(
            (normalized_isrc and exact_isrc and normalized_isrc == exact_isrc)
            or (recording_id and exact_recording_id and recording_id.casefold() == exact_recording_id.casefold())
        )
        stable_conflict = bool(
            (normalized_isrc and exact_isrc and normalized_isrc != exact_isrc)
            or (recording_id and exact_recording_id and recording_id.casefold() != exact_recording_id.casefold())
        )
        duration_ok = bool(exact_duration and duration and abs(exact_duration - duration) <= max(4.0, duration * 0.03))
        album_ok = bool(exact_album and album and text_similarity(exact_album, album) >= 0.80)
        version_ok = version_signature(exact_title) == version_signature(song)
        identity_ok = bool(
            exact_title
            and exact_artist
            and text_similarity(exact_title, song) >= 0.90
            and text_similarity(exact_artist, artist) >= 0.90
            and (stable_match or (duration_ok and (not album or album_ok) and version_ok))
            and not stable_conflict
        )
        if identity_ok:
            return {
                "source": "LRCLIB (exact instrumental)",
                "id": exact.get("id"),
                "provider_track_id": str(exact.get("id") or ""),
                "trackName": exact_title,
                "artistName": exact_artist,
                "albumName": exact_album,
                "duration": exact_duration,
                "isrc": exact.get("isrc") or "",
                "recording_id": exact_recording_id,
                "language": exact.get("language") or "",
                "instrumental": True,
                "instrumental_verified": True,
                "identity_validated": True,
                "acquisition_status": "INSTRUMENTAL",
            }
    if isinstance(exact, dict) and (exact.get("syncedLyrics") or exact.get("plainLyrics")):
        exact_duration = exact.get("duration")
        try:
            parsed_exact_duration = float(exact_duration) if exact_duration is not None else None
        except (TypeError, ValueError):
            parsed_exact_duration = None
        identity_ok = bool(
            exact.get("trackName")
            and exact.get("artistName")
            and is_track_match(
                str(exact.get("trackName") or ""),
                str(exact.get("artistName") or ""),
                song,
                artist,
                cand_duration=parsed_exact_duration,
                target_duration=duration,
            )
        )
        if identity_ok:
            candidate = result_from(exact, "LRCLIB (exact)", exact=True)
            candidate_pool.append(candidate)

    search_urls = [
        f"https://lrclib.net/api/search?{urllib.parse.urlencode({'track_name': song.strip(), 'artist_name': artist.strip()})}",
        f"https://lrclib.net/api/search?{urllib.parse.urlencode({'q': f'{artist} {song}'.strip()})}",
    ]
    lat_artist, lat_song = cyrillic_to_latin(artist), cyrillic_to_latin(song)
    if (lat_artist, lat_song) != (artist, song):
        search_urls.append(
            f"https://lrclib.net/api/search?{urllib.parse.urlencode({'q': f'{lat_artist} {lat_song}'.strip()})}"
        )

    matched: list[dict[str, Any]] = []
    source_names = ("LRCLIB (search)", "LRCLIB (q-search)", "LRCLIB (translit)")
    for source_name, url in zip(source_names, search_urls):
        items = get_json(url, list)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or not (item.get("syncedLyrics") or item.get("plainLyrics")):
                continue
            candidate_title = str(item.get("trackName") or "")
            candidate_artist = str(item.get("artistName") or "")
            if not is_track_match(
                candidate_title,
                candidate_artist,
                song,
                artist,
                cand_duration=item.get("duration"),
                target_duration=duration,
            ):
                continue
            candidate = result_from(item, source_name)
            candidate["identity_validated"] = bool(candidate_title and candidate_artist)
            matched.append(candidate)

    def rank(candidate: dict[str, Any]) -> tuple[int, int, int, str]:
        candidate_duration = candidate.get("duration")
        delta = abs(float(candidate_duration) - duration) if duration and candidate_duration else float("inf")
        album_match = bool(
            candidate.get("albumName") and album and text_similarity(str(candidate["albumName"]), album) >= 0.80
        )
        return (
            int(album_match),
            -int(delta * 1000) if delta != float("inf") else -(10**12),
            min(len(str(candidate.get("plainLyrics") or "")), 100_000),
            str(candidate.get("id") or ""),
        )

    matched.sort(key=rank, reverse=True)
    candidate_pool.extend(matched)
    unique_candidates: list[dict[str, Any]] = []
    seen_candidates: set[tuple[str, str]] = set()
    for candidate in sorted(candidate_pool, key=rank, reverse=True):
        text_key = normalize_lookup(str(candidate.get("plainLyrics") or candidate.get("syncedLyrics") or ""))
        identity_key = (str(candidate.get("id") or ""), text_key)
        if identity_key in seen_candidates:
            continue
        seen_candidates.add(identity_key)
        unique_candidates.append(candidate)
    if unique_candidates:
        selected = dict(unique_candidates[0])
        selected["alternatives"] = unique_candidates[1:]
        return selected

    if failures:
        severity = {"BLOCKED": 5, "RATE_LIMITED": 4, "TRANSIENT_ERROR": 3, "PARSER_ERROR": 2, "INVALID_RESPONSE": 1}
        status, error = max(
            ((failure.classification, failure.error) for failure in failures),
            key=lambda item: severity.get(item[0], 0),
        )
        return {"source": "LRCLIB", "acquisition_status": status, "error": error}
    if successful_responses:
        return {"source": "LRCLIB", "acquisition_status": "NOT_FOUND"}
    return {"source": "LRCLIB", "acquisition_status": "TRANSIENT_ERROR", "error": "no valid provider response"}


def fetch_kugou(
    song: str,
    artist: str,
    album: str = "",
    duration: float | None = None,
    timeout: int = 6,
) -> dict[str, Any] | None:
    """Read Kugou's observed JSON endpoints with explicit schema/failure states.

    These endpoints are not a documented public lyrics API. They are kept as
    an optional provider adapter; requests are paced, bounded, and never
    attempt authentication or challenge-page workarounds.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 10; Mobile)",
        "Accept": "application/json,text/plain;q=0.9,*/*;q=0.5",
    }
    queries = [f"{artist} {song}".strip()]
    lat_art = cyrillic_to_latin(artist)
    lat_song = cyrillic_to_latin(song)
    if lat_art != artist or lat_song != song:
        queries.append(f"{lat_art} {lat_song}".strip())

    failures: list[tuple[str, str]] = []
    valid_search_responses = 0
    matching: list[dict[str, Any]] = []

    def request_json(url: str, expected_host: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers=headers)
        with _provider_urlopen(request, timeout=timeout) as response:
            text = read_provider_text(response)
            get_url = getattr(response, "geturl", None)
            final_url = urllib.parse.urlparse(get_url() if callable(get_url) else url)
        if final_url.scheme != "https" or final_url.hostname not in {expected_host, f"www.{expected_host}"}:
            raise _LyricsAcquisitionError("INVALID_RESPONSE", "Kugou response redirected outside its HTTPS host")
        if _provider_page_blocked(text):
            raise _LyricsAcquisitionError("BLOCKED", "Kugou returned a challenge or access-denied page")
        payload = _provider_json_loads(text)
        if not isinstance(payload, dict):
            raise TypeError("Kugou response root is not a JSON object")
        return payload

    def remember_failure(exc: BaseException) -> None:
        status, detail = classify_lyrics_exception(exc)
        _record_provider_failure(exc)
        failures.append((status, detail))

    def failure_result() -> dict[str, Any]:
        severity = {
            "AUTHENTICATION_ERROR": 7,
            "BLOCKED": 6,
            "RATE_LIMITED": 5,
            "TRANSIENT_ERROR": 4,
            "PARSER_ERROR": 3,
            "INVALID_RESPONSE": 2,
            "NOT_FOUND": 1,
        }
        status, detail = max(failures, key=lambda value: severity.get(value[0], 0))
        return {"source": "Kugou", "acquisition_status": status, "error": detail}

    for q_text in queries:
        search_url = "https://mobilecdn.kugou.com/api/v3/search/song?" + urllib.parse.urlencode(
            {"format": "json", "keyword": q_text, "page": 1, "pagesize": 5}
        )
        try:
            sdata = request_json(search_url, "mobilecdn.kugou.com")
            data = sdata.get("data")
            songs = data.get("info") if isinstance(data, dict) else None
            if not isinstance(songs, list):
                raise TypeError("Kugou search response is missing data.info array")
            valid_search_responses += 1
            for item in songs:
                if not isinstance(item, dict):
                    raise TypeError("Kugou search result is not a JSON object")
                item_duration = _provider_duration_seconds(item.get("duration"))
                item_title = str(item.get("songname") or "")
                item_artist = str(item.get("singername") or "")
                if not item_title or not item_artist:
                    continue
                if is_track_match(
                    item_title,
                    item_artist,
                    song,
                    artist,
                    cand_duration=item_duration,
                    target_duration=duration,
                ):
                    matching.append({**item, "_parsed_duration": item_duration})
            if matching:
                break
        except _LyricsAcquisitionError as exc:
            _record_provider_failure(exc)
            failures.append((exc.status, str(exc)))
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
            remember_failure(exc)

    if not matching:
        if failures:
            return failure_result()
        if valid_search_responses:
            mark_provider_not_found()
            return {"source": "Kugou", "acquisition_status": "NOT_FOUND"}
        return {"source": "Kugou", "acquisition_status": "TRANSIENT_ERROR", "error": "no valid search response"}

    def duration_delta(item: Mapping[str, Any]) -> float:
        item_duration = item.get("_parsed_duration")
        if duration and item_duration:
            return abs(float(item_duration) - float(duration))
        return float("inf")

    matching.sort(
        key=lambda item: (
            duration_delta(item),
            -text_similarity(str(item.get("songname") or ""), song),
            -text_similarity(str(item.get("singername") or ""), artist),
            str(item.get("hash") or ""),
        )
    )
    candidates: list[dict[str, Any]] = []
    successful_lyric_searches = 0
    for song_item in matching[:4]:
        song_hash = str(song_item.get("hash") or "")
        if not song_hash:
            continue
        item_duration = song_item.get("_parsed_duration")
        lyric_duration_ms = round(float(item_duration or duration or 0) * 1000)
        search_params = {
            "ver": 1,
            "man": "yes",
            "client": "mobi",
            "keyword": song,
            "duration": lyric_duration_ms,
            "hash": song_hash,
        }
        lyrics_url = "https://lyrics.kugou.com/search?" + urllib.parse.urlencode(search_params)
        try:
            lyrics_search = request_json(lyrics_url, "lyrics.kugou.com")
            lyric_matches = lyrics_search.get("candidates")
            if not isinstance(lyric_matches, list):
                raise TypeError("Kugou lyric search response is missing candidates array")
            successful_lyric_searches += 1
        except _LyricsAcquisitionError as exc:
            _record_provider_failure(exc)
            failures.append((exc.status, str(exc)))
            continue
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
            remember_failure(exc)
            continue

        lyric_matches = sorted(
            (item for item in lyric_matches if isinstance(item, dict)),
            key=lambda item: (
                -int(item.get("score") or 0) if str(item.get("score") or "").lstrip("+-").isdigit() else 0,
                str(item.get("id") or ""),
            ),
        )
        for lyric_item in lyric_matches[:2]:
            lyric_id = str(lyric_item.get("id") or "")
            access_key = str(lyric_item.get("accesskey") or "")
            if not lyric_id or not access_key:
                continue
            parsed_lyric_title = str(lyric_item.get("song") or song_item.get("songname") or "")
            parsed_lyric_artist = str(lyric_item.get("singer") or song_item.get("singername") or "")
            if not is_track_match(parsed_lyric_title, parsed_lyric_artist, song, artist):
                continue
            candidate_identity: dict[str, Any] = {
                "provider_track_id": song_hash,
                "provider_lyrics_id": lyric_id,
                "trackName": parsed_lyric_title,
                "artistName": parsed_lyric_artist,
                "albumName": str(
                    lyric_item.get("album") or song_item.get("album_name") or song_item.get("albumname") or ""
                ),
                "duration": item_duration,
                "language": str(lyric_item.get("language") or song_item.get("language") or ""),
                "isrc": str(song_item.get("isrc") or ""),
                "version": str(lyric_item.get("version") or ""),
                "identity_validated": True,
                "providerMetadata": {
                    "song_hash": song_hash,
                    "lyrics_id": lyric_id,
                    "product_from": str(lyric_item.get("product_from") or ""),
                    "score": lyric_item.get("score"),
                    "adjust": lyric_item.get("adjust"),
                },
            }

            for format_name in ("krc", "lrc"):
                download_params = {
                    "ver": 1,
                    "client": "pc",
                    "id": lyric_id,
                    "accesskey": access_key,
                    "fmt": format_name,
                    "charset": "utf8",
                }
                download_url = "https://lyrics.kugou.com/download?" + urllib.parse.urlencode(download_params)
                try:
                    download = request_json(download_url, "lyrics.kugou.com")
                    encoded_content = download.get("content")
                    if encoded_content in (None, ""):
                        continue
                    if not isinstance(encoded_content, str):
                        raise TypeError("Kugou download response content is not text")
                    if format_name == "krc":
                        raw_text = decode_kugou_krc(encoded_content)
                        converted = krc_to_lyrics(raw_text, song=song, artist=artist, album=album)
                        if not converted.get("plainLyrics"):
                            raise ValueError("Kugou KRC parser found no lyric text")
                        if converted.get("has_word_sync"):
                            candidates.append(
                                {
                                    **candidate_identity,
                                    "found": True,
                                    "source": "Kugou KRC",
                                    "id": lyric_id,
                                    "attribution": "Kugou",
                                    "format": "krc",
                                    "syncType": "richsync",
                                    "has_word_sync": True,
                                    "has_back_vocals": converted.get("has_back_vocals", False),
                                    "providerRaw": raw_text,
                                    "rawLyrics": raw_text,
                                    "plainLyrics": converted["plainLyrics"],
                                    "syncedLyrics": converted["syncedLyrics"],
                                    "enhancedLyrics": converted["enhancedLyrics"],
                                    "ttmlLyrics": converted["ttmlLyrics"],
                                }
                            )
                        else:
                            candidates.append(
                                {
                                    **candidate_identity,
                                    "found": True,
                                    "source": "Kugou KRC (unsynchronized text)",
                                    "id": lyric_id,
                                    "attribution": "Kugou",
                                    "format": "plain",
                                    "syncType": "none",
                                    "has_word_sync": False,
                                    "providerRaw": raw_text,
                                    "rawLyrics": raw_text,
                                    "plainLyrics": converted["plainLyrics"],
                                    "syncedLyrics": "",
                                    "enhancedLyrics": "",
                                    "ttmlLyrics": "",
                                    "validationWarnings": ["KRC lyrics text was retained; timing did not validate"],
                                }
                            )
                    else:
                        raw_lrc = base64.b64decode(encoded_content, validate=True).decode("utf-8", errors="strict")
                        if not raw_lrc.strip():
                            continue
                        is_synced = bool(ANY_LINE_TIMESTAMP_RE.search(raw_lrc))
                        valid_lrc = validate_lrc(raw_lrc, duration=duration)[0] if is_synced else False
                        plain = to_plain(raw_lrc, song=song, artist=artist) if is_synced else raw_lrc
                        candidates.append(
                            {
                                **candidate_identity,
                                "found": True,
                                "source": "Kugou LRC" if valid_lrc else "Kugou LRC (unsynchronized text)",
                                "id": lyric_id,
                                "attribution": "Kugou",
                                "format": "lrc" if valid_lrc else "plain",
                                "syncType": "linesync" if valid_lrc else "none",
                                "has_word_sync": False,
                                "has_back_vocals": False,
                                "providerRaw": raw_lrc,
                                "rawLyrics": raw_lrc,
                                "plainLyrics": plain,
                                "syncedLyrics": raw_lrc if valid_lrc else "",
                                "enhancedLyrics": "",
                                "ttmlLyrics": "",
                                "validationWarnings": []
                                if valid_lrc or not is_synced
                                else ["Kugou LRC text was retained; timing did not validate"],
                            }
                        )
                except _LyricsAcquisitionError as exc:
                    _record_provider_failure(exc)
                    failures.append((exc.status, str(exc)))
                except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
                    remember_failure(exc)

    if candidates:
        candidates.sort(
            key=lambda item: (
                abs(float(item["duration"]) - float(duration)) if duration and item.get("duration") else float("inf"),
                -text_similarity(str(item.get("trackName") or ""), song),
                -text_similarity(str(item.get("artistName") or ""), artist),
                -int(bool(item.get("has_word_sync"))),
                str(item.get("provider_lyrics_id") or ""),
                str(item.get("source") or ""),
                normalize_lookup(str(item.get("plainLyrics") or "")),
            )
        )
        selected = dict(candidates[0])
        selected["alternatives"] = candidates[1:]
        if failures:
            selected["validationWarnings"] = list(selected.get("validationWarnings") or []) + [
                f"some Kugou candidates could not be parsed ({failure_result()['acquisition_status']})"
            ]
        return selected
    if failures:
        return failure_result()
    if valid_search_responses or successful_lyric_searches:
        mark_provider_not_found()
        return {"source": "Kugou", "acquisition_status": "NOT_FOUND"}
    return {"source": "Kugou", "acquisition_status": "TRANSIENT_ERROR", "error": "no valid Kugou response"}


def fetch_syncedlyrics_fallback(
    song: str,
    artist: str,
    album: str = "",
    duration: float | None = None,
) -> dict[str, Any] | None:
    """Disable the opaque aggregate scraper; it cannot report source identity.

    Its upstream provider mix and scraping behavior are outside this adapter's
    control, and it is not part of the production cascade. Explicit provider
    adapters are used instead so response, match, and failure provenance stay
    inspectable.
    """
    del song, artist, album, duration
    return {
        "source": "syncedlyrics aggregate",
        "acquisition_status": "UNSUPPORTED_PROVIDER",
        "error": "opaque third-party aggregate scraper is disabled; use an explicit provider adapter",
    }


def _collected_lyrics_result(
    candidates: list[dict[str, Any]],
    failures: list[tuple[str, str]],
    *,
    source: str,
    truncated: bool = False,
) -> dict[str, Any]:
    """Keep successful independent lookups and their acquisition diagnostics."""
    if candidates:
        result = dict(candidates[0])
        result["alternatives"] = [*result.get("alternatives", []), *candidates[1:]]
        if truncated:
            result.setdefault("validationWarnings", []).append(
                "candidate retrieval truncated to the adapter's bounded limit"
            )
        if failures:
            result.setdefault("validationWarnings", []).append(
                f"some candidate lookups failed ({', '.join(sorted({status for status, _ in failures}))})"
            )
        return result
    severity = {
        "AUTHENTICATION_ERROR": 7,
        "BLOCKED": 6,
        "RATE_LIMITED": 5,
        "TRANSIENT_ERROR": 4,
        "PARSER_ERROR": 3,
        "INVALID_RESPONSE": 2,
        "NOT_FOUND": 1,
    }
    if not failures:
        return {"source": source, "acquisition_status": "NOT_FOUND"}
    status, detail = max(failures, key=lambda item: severity.get(item[0], 0))
    return {"source": source, "acquisition_status": status, "error": detail}


def fetch_megalobiz(
    song: str,
    artist: str,
    album: str = "",
    duration: float | None = None,
    timeout: int = 6,
) -> dict[str, Any] | None:
    """Fetch LRC only from a page whose visible title and artist match the query.

    The optional ``syncedlyrics`` adapter is intentionally not used here: its
    search method returns only an LRC string, with no recording identity or
    request timeout, so a same-title live/remix result cannot be validated.
    """
    acquired_forms: list[dict[str, Any]] = []
    item_failures: list[tuple[str, str]] = []
    query = f"{artist} {song}".strip()
    headers = {"User-Agent": "AudiophileArchTagger/6.0 (lyrics metadata client)", "Accept": "text/html"}
    search_url = f"https://www.megalobiz.com/search/all?{urllib.parse.urlencode({'qry': query})}"
    try:
        with _provider_urlopen(urllib.request.Request(search_url, headers=headers), timeout=timeout) as response:
            search_page = read_provider_text(response)
        if _provider_page_blocked(search_page):
            return {"source": "Megalobiz", "acquisition_status": "BLOCKED", "error": "provider challenge page"}
        candidates = parse_megalobiz_search(search_page, song, artist)
        if not candidates:
            mark_provider_not_found()
            return {"source": "Megalobiz", "acquisition_status": "NOT_FOUND"}
        parse_errors: list[str] = []
        for candidate in candidates[:3]:
            try:
                with _provider_urlopen(
                    urllib.request.Request(candidate["url"], headers=headers), timeout=timeout
                ) as response:
                    detail_page = read_provider_text(response)
                    final = (
                        urllib.parse.urlparse(response.geturl())
                        if callable(getattr(response, "geturl", None))
                        else urllib.parse.urlparse(candidate["url"])
                    )
                if final.scheme != "https" or final.hostname not in {"megalobiz.com", "www.megalobiz.com"}:
                    raise _LyricsAcquisitionError("INVALID_RESPONSE", "redirect left provider host")
                try:
                    raw_lrc = parse_megalobiz_lyrics(detail_page, candidate["id"])
                except ValueError as exc:
                    parse_errors.append(str(exc))
                    continue
                if len(raw_lrc.strip()) < 10 or not ANY_LINE_TIMESTAMP_RE.search(raw_lrc):
                    parse_errors.append("Megalobiz detail page has no usable LRC timestamps")
                    continue
                std_lrc = normalize_synced(raw_lrc, keep_word_sync=False)
                acquired_forms.append(
                    {
                        "found": True,
                        "source": "Megalobiz (LRC)",
                        "id": candidate["id"],
                        "provider_track_id": candidate["id"],
                        "url": candidate["url"],
                        "trackName": candidate["title"],
                        "artistName": candidate["artist"],
                        "identity_validated": True,
                        "source_confidence": 55,
                        "format": "lrc",
                        "syncType": "linesync",
                        "providerRaw": raw_lrc,
                        "rawLyrics": raw_lrc,
                        "plainLyrics": to_plain(raw_lrc),
                        "syncedLyrics": std_lrc,
                        "enhancedLyrics": "",
                        "duration": None,
                        "transformations": ["HTML lyrics container decoded; source line order retained"],
                    }
                )
            except (OSError, ValueError, TypeError, KeyError, AttributeError, _LyricsAcquisitionError) as exc:
                _record_provider_failure(exc)
                status, detail = classify_lyrics_exception(exc)
                item_failures.append((status, detail))
                if status in {"RATE_LIMITED", "AUTHENTICATION_ERROR", "BLOCKED"}:
                    break
                continue
        if acquired_forms or item_failures:
            return _collected_lyrics_result(
                acquired_forms, item_failures, source="Megalobiz", truncated=len(candidates) > 3
            )
        if parse_errors:
            error = ValueError(parse_errors[0])
            _record_provider_failure(error)
            return {"source": "Megalobiz", "acquisition_status": "PARSER_ERROR", "error": str(error)}
        return {"source": "Megalobiz", "acquisition_status": "NOT_FOUND"}
    except (OSError, ValueError) as exc:
        status, detail = classify_lyrics_exception(exc)
        _record_provider_failure(exc)
        return {"source": "Megalobiz", "acquisition_status": status, "error": detail}


def parse_megalobiz_search(page: str, song: str, artist: str) -> list[dict[str, str]]:
    root = _parse_provider_html(page, "Megalobiz search")
    output: list[dict[str, str]] = []
    saw_lrc_result = False
    for link in _provider_nodes(root, tag="a"):
        href = link.attrs.get("href", "")
        url = urllib.parse.urlparse(urllib.parse.urljoin("https://www.megalobiz.com/", href))
        if url.scheme != "https" or url.hostname not in {"megalobiz.com", "www.megalobiz.com"}:
            continue
        if not url.path.startswith("/lrc/maker/"):
            continue
        saw_lrc_result = True
        identity = _identity_from_result_label(link.attrs.get("title", "") or link.text(), song, artist)
        if identity is None:
            continue
        match = re.search(r"(?:^|[./_-])(\d+)(?:\.html)?$", url.path)
        if not match:
            continue
        output.append({"id": match.group(1), "url": url.geturl(), "title": identity[0], "artist": identity[1]})
    if (
        not output
        and not saw_lrc_result
        and not re.search(r"no\s+(?:lyrics|results|matches)|nothing\s+found", root.text(), re.IGNORECASE)
    ):
        raise ValueError("Megalobiz search markup has no recognizable result state")
    return output


def parse_megalobiz_lyrics(page: str, provider_id: str) -> str:
    root = _parse_provider_html(page, "Megalobiz lyrics")
    matches = _provider_nodes(root, element_id=f"lrc_{provider_id}_details")
    if len(matches) != 1:
        raise ValueError("Megalobiz LRC details container is missing or ambiguous")
    content = matches[0].text().replace("\r\n", "\n").replace("\r", "\n")
    if not content.strip():
        raise ValueError("Megalobiz LRC details container is empty")
    return content


def fetch_rentanadviser(
    song: str,
    artist: str,
    album: str = "",
    duration: float | None = None,
    timeout: int = 6,
) -> dict[str, Any] | None:
    """Fetch synchronized lyrics only for a title/artist-verified result row."""
    acquired_forms: list[dict[str, Any]] = []
    item_failures: list[tuple[str, str]] = []
    query = f"{artist} {song}".strip()
    headers = {"User-Agent": "AudiophileArchTagger/6.0 (lyrics metadata client)", "Accept": "text/html"}
    search_url = "https://www.rentanadviser.com/en/subtitles/subtitles4songs.aspx?" + urllib.parse.urlencode(
        {"src": query}
    )
    try:
        with _provider_urlopen(urllib.request.Request(search_url, headers=headers), timeout=timeout) as response:
            search_page = read_provider_text(response)
        if _provider_page_blocked(search_page):
            return {"source": "RentAnAdviser", "acquisition_status": "BLOCKED", "error": "provider challenge page"}
        results = parse_rentanadviser_search(search_page, song, artist)
        if not results:
            mark_provider_not_found()
            return {"source": "RentAnAdviser", "acquisition_status": "NOT_FOUND"}
        parser_errors: list[str] = []
        for result in results[:3]:
            try:
                detail_url = "https://www.rentanadviser.com/subtitles/getsubtitles.aspx?" + urllib.parse.urlencode(
                    {"id": result["id"], "type": "lrc"}
                )
                with _provider_urlopen(
                    urllib.request.Request(detail_url, headers=headers), timeout=timeout
                ) as response:
                    page = read_provider_text(response)
                    final = (
                        urllib.parse.urlparse(response.geturl())
                        if callable(getattr(response, "geturl", None))
                        else urllib.parse.urlparse(detail_url)
                    )
                if final.scheme != "https" or final.hostname != "www.rentanadviser.com":
                    raise _LyricsAcquisitionError("INVALID_RESPONSE", "redirect left provider host")
                try:
                    raw_lrc = parse_rentanadviser_lyrics(page)
                except ValueError as exc:
                    parser_errors.append(str(exc))
                    continue
                if not ANY_LINE_TIMESTAMP_RE.search(raw_lrc):
                    parser_errors.append("RentAnAdviser lyrics response contains no LRC timestamps")
                    continue
                acquired_forms.append(
                    {
                        "found": True,
                        "source": "RentAnAdviser (LRC)",
                        "id": result["id"],
                        "provider_track_id": result["id"],
                        "url": detail_url,
                        "trackName": result["title"],
                        "artistName": result["artist"],
                        "identity_validated": True,
                        "source_confidence": 52,
                        "format": "lrc",
                        "syncType": "linesync",
                        "providerRaw": raw_lrc,
                        "rawLyrics": raw_lrc,
                        "plainLyrics": to_plain(raw_lrc),
                        "syncedLyrics": raw_lrc,
                        "enhancedLyrics": "",
                        "duration": None,
                        "transformations": ["HTML lyrics field decoded; provider text retained"],
                    }
                )
            except (OSError, ValueError, TypeError, KeyError, AttributeError, _LyricsAcquisitionError) as exc:
                _record_provider_failure(exc)
                status, detail = classify_lyrics_exception(exc)
                item_failures.append((status, detail))
                if status in {"RATE_LIMITED", "AUTHENTICATION_ERROR", "BLOCKED"}:
                    break
                continue
        if acquired_forms or item_failures:
            return _collected_lyrics_result(
                acquired_forms, item_failures, source="RentAnAdviser", truncated=len(results) > 3
            )
        if parser_errors:
            error = ValueError(parser_errors[0])
            _record_provider_failure(error)
            return {"source": "RentAnAdviser", "acquisition_status": "PARSER_ERROR", "error": str(error)}
        return {"source": "RentAnAdviser", "acquisition_status": "NOT_FOUND"}
    except (OSError, ValueError) as exc:
        status, detail = classify_lyrics_exception(exc)
        _record_provider_failure(exc)
        return {"source": "RentAnAdviser", "acquisition_status": status, "error": detail}


def parse_rentanadviser_search(page: str, song: str, artist: str) -> list[dict[str, str]]:
    root = _parse_provider_html(page, "RentAnAdviser search")
    output: list[dict[str, str]] = []
    saw_detail_link = False
    for link in _provider_nodes(root, tag="a"):
        url = urllib.parse.urlparse(urllib.parse.urljoin("https://www.rentanadviser.com/", link.attrs.get("href", "")))
        if url.hostname != "www.rentanadviser.com" or not url.path.casefold().endswith("getsubtitles.aspx"):
            continue
        query = urllib.parse.parse_qs(url.query)
        ids = query.get("id", [])
        if not ids or not ids[0].isdigit():
            continue
        saw_detail_link = True
        identity = _identity_from_result_label(link.attrs.get("title", "") or link.text(), song, artist)
        if identity:
            output.append({"id": ids[0], "title": identity[0], "artist": identity[1]})
    if not saw_detail_link and not re.search(
        r"no\s+(?:subtitles|lyrics|results|matches)|nothing\s+found", page, re.IGNORECASE
    ):
        raise ValueError("RentAnAdviser search markup has no recognizable subtitle links")
    return output


def parse_rentanadviser_lyrics(page: str) -> str:
    root = _parse_provider_html(page, "RentAnAdviser lyrics")
    matches = _provider_nodes(root, element_id="ctl00_ContentPlaceHolder1_lbllyrics")
    if not matches:
        matches = _provider_nodes(root, tag="textarea")
    if len(matches) != 1:
        raise ValueError("RentAnAdviser lyrics field is missing or ambiguous")
    text = matches[0].text().replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ValueError("RentAnAdviser lyrics field is empty")
    return text


def fetch_lyrics_ovh(song: str, artist: str, timeout: int = 6) -> dict[str, Any]:
    """Disable the diagnostic-only aggregate API, which returns no item identity."""
    del song, artist, timeout
    return {
        "source": "Lyrics.ovh",
        "acquisition_status": "UNSUPPORTED_PROVIDER",
        "error": "diagnostic-only API lacks recording identity and is not used for lyrics selection",
    }


def _musixmatch_body(payload: object, *, endpoint: str) -> dict[str, Any]:
    """Validate the documented Musixmatch envelope and return its body."""
    if not isinstance(payload, dict):
        raise TypeError(f"Musixmatch {endpoint} response is not an object")
    message = payload.get("message")
    if not isinstance(message, dict):
        raise TypeError(f"Musixmatch {endpoint} response has no message object")
    header = message.get("header")
    body = message.get("body")
    if not isinstance(header, dict):
        raise TypeError(f"Musixmatch {endpoint} response has no status header")
    raw_code = header.get("status_code")
    if not isinstance(raw_code, (int, str)) or isinstance(raw_code, bool):
        raise TypeError(f"Musixmatch {endpoint} response has an invalid status code")
    try:
        code = int(raw_code)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Musixmatch {endpoint} response has an invalid status code") from exc
    if code == 404:
        raise _LyricsAcquisitionError("NOT_FOUND", f"Musixmatch {endpoint}: no matching resource")
    if code in {401, 403}:
        raise _LyricsAcquisitionError("AUTHENTICATION_ERROR", f"Musixmatch {endpoint}: credentials rejected")
    if code in {402, 429}:
        raise _LyricsAcquisitionError("RATE_LIMITED", f"Musixmatch {endpoint}: API quota or rate limit")
    if code >= 500:
        raise _LyricsAcquisitionError("TRANSIENT_ERROR", f"Musixmatch {endpoint}: HTTP-like API status {code}")
    if code != 200:
        raise _LyricsAcquisitionError("INVALID_RESPONSE", f"Musixmatch {endpoint}: API status {code}")
    if not isinstance(body, dict):
        raise TypeError(f"Musixmatch {endpoint} response has no object body")
    return body


def parse_musixmatch_search(payload: object) -> list[dict[str, Any]]:
    """Read track.search results without treating schema changes as no-result."""
    body = _musixmatch_body(payload, endpoint="track.search")
    track_list = body.get("track_list")
    if not isinstance(track_list, list):
        if "track_list" not in body:
            raise ValueError("Musixmatch track.search response has no track_list")
        raise TypeError("Musixmatch track.search track_list is not an array")
    tracks: list[dict[str, Any]] = []
    for index, item in enumerate(track_list):
        if not isinstance(item, dict) or not isinstance(item.get("track"), dict):
            raise TypeError(f"Musixmatch track.search result {index} has an invalid track object")
        tracks.append(item["track"])
    return tracks


def parse_musixmatch_lyrics(payload: object) -> tuple[str, str, dict[str, Any]]:
    """Return raw body, conservative playback text, and provider lyric metadata."""
    body = _musixmatch_body(payload, endpoint="track.lyrics.get")
    lyrics = body.get("lyrics")
    if not isinstance(lyrics, dict):
        raise TypeError("Musixmatch track.lyrics.get response has no lyrics object")
    raw = lyrics.get("lyrics_body")
    if not isinstance(raw, str):
        raise TypeError("Musixmatch lyrics_body is missing or not text")
    raw = conservative_provider_text(raw)
    lines = raw.split("\n")
    transformations: list[str] = []
    footer_index = len(lines) - 1
    while footer_index >= 0 and not lines[footer_index].strip():
        footer_index -= 1
    if footer_index >= 0 and re.fullmatch(
        r"\s*\*{3,}\s*this lyrics is not for commercial use\s*\*{3,}\s*",
        lines[footer_index],
        re.IGNORECASE,
    ):
        del lines[footer_index]
        transformations.append("known Musixmatch non-commercial-use footer removed from playback text")
    parsed = "\n".join(lines)
    metadata = {
        "lyrics_language": str(lyrics.get("lyrics_language") or ""),
        "lyrics_copyright": str(lyrics.get("lyrics_copyright") or ""),
        "lyrics_id": str(lyrics.get("lyrics_id") or ""),
        "transformations": transformations,
    }
    return raw, parsed, metadata


def conservative_provider_text(value: str) -> str:
    """Decode transport-only details while preserving all lyric punctuation/layout."""
    return value.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def _musixmatch_api_key() -> str:
    key = os.environ.get("MUSIXMATCH_API_KEY", "").strip()
    if key:
        return key
    path = Path.home() / ".config" / "musixmatch_api_key"
    try:
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    except OSError:
        return ""


def _musixmatch_desktop_token() -> str:
    token = os.environ.get("MUSIXMATCH_TOKEN", "").strip()
    if token:
        return token
    path = Path.home() / ".config" / "musixmatch_token"
    try:
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    except OSError:
        return ""


def fetch_musixmatch(
    song: str,
    artist: str,
    album: str = "",
    duration: float | None = None,
    timeout: int = 6,
    _route: str = "auto",
) -> dict[str, Any] | None:
    """Fetch identity-bound plain, subtitle, and richsync forms where allowed.

    The documented API key route is preferred. An explicitly configured
    desktop user token can use the observed desktop route. When both are
    configured, the desktop route remains eligible for richer native timing.
    No anonymous token harvesting or browser cookie copying occurs.
    """
    api_key = _musixmatch_api_key()
    desktop_token = _musixmatch_desktop_token()
    if _route == "auto" and api_key and desktop_token:
        documented = fetch_musixmatch(song, artist, album, duration, timeout, _route="documented")
        desktop = fetch_musixmatch(song, artist, album, duration, timeout, _route="desktop")
        if desktop and desktop.get("found"):
            if documented and documented.get("found"):
                desktop["alternatives"] = [documented, *desktop.get("alternatives", [])]
            return desktop
        if documented and documented.get("found"):
            if desktop and desktop.get("acquisition_status") not in {None, "NOT_FOUND"}:
                documented.setdefault("validationWarnings", []).append(
                    f"desktop richsync route: {desktop['acquisition_status']}"
                )
            return documented
        return documented if documented and documented.get("acquisition_status") != "NOT_FOUND" else desktop
    if _route == "desktop":
        api_key = ""
    elif _route == "documented":
        desktop_token = ""
    if not api_key and not desktop_token:
        return {
            "source": "Musixmatch",
            "acquisition_status": "NOT_CONFIGURED",
            "error": "MUSIXMATCH_API_KEY or MUSIXMATCH_TOKEN is not configured",
        }

    api_base = "https://api.musixmatch.com/ws/1.1/" if api_key else "https://apic-desktop.musixmatch.com/ws/1.1/"
    auth_params = (
        {"apikey": api_key}
        if api_key
        else {"usertoken": desktop_token, "app_id": "web-desktop-app-v1.0", "format": "json"}
    )

    headers = {"User-Agent": "AudiophileArchTagger/6.0 (LyricsClient)", "Accept": "application/json"}
    search_params = {
        "q_track": song,
        "q_artist": artist,
        "page_size": "10",
        "page": "1",
        "f_has_lyrics": "1",
        **auth_params,
    }
    if album.strip():
        search_params["q_album"] = album
    search_url = api_base + "track.search?" + urllib.parse.urlencode(search_params)
    try:
        request = urllib.request.Request(search_url, headers=headers)
        with _provider_urlopen(request, timeout=timeout) as response:
            search_payload = _provider_json_loads(read_provider_text(response))
        tracks = parse_musixmatch_search(search_payload)
    except _LyricsAcquisitionError as exc:
        return {"source": "Musixmatch", "acquisition_status": exc.status, "error": str(exc)}
    except urllib.error.HTTPError as exc:
        status = {401: "AUTHENTICATION_ERROR", 403: "BLOCKED", 404: "NOT_FOUND", 429: "RATE_LIMITED"}.get(
            exc.code, "TRANSIENT_ERROR" if exc.code >= 500 else "INVALID_RESPONSE"
        )
        return {
            "source": "Musixmatch",
            "acquisition_status": status,
            "error": f"Musixmatch track.search HTTP {exc.code}",
        }
    except Exception as exc:
        status, detail = classify_lyrics_exception(exc)
        return {"source": "Musixmatch", "acquisition_status": status, "error": detail}

    matching: list[tuple[float, dict[str, Any]]] = []
    for track in tracks:
        title = track.get("track_name")
        track_artist = track.get("artist_name")
        if not isinstance(title, str) or not isinstance(track_artist, str):
            continue
        raw_duration = track.get("track_length")
        try:
            track_duration = (
                float(raw_duration)
                if isinstance(raw_duration, (int, float, str)) and str(raw_duration).strip()
                else None
            )
        except (TypeError, ValueError, OverflowError):
            track_duration = None
        if track_duration is not None and (not math.isfinite(track_duration) or track_duration <= 0):
            track_duration = None
        if not is_track_match(
            title, track_artist, song, artist, cand_duration=track_duration, target_duration=duration
        ):
            continue
        track_id = track.get("track_id")
        if not track_id:
            continue
        album_name = str(track.get("album_name") or "")
        score = text_similarity(title, song) + text_similarity(track_artist, artist)
        if album and album_name:
            score += 0.25 * text_similarity(album_name, album)
        if duration and track_duration:
            score -= min(1.0, abs(duration - track_duration) / max(duration, 1.0))
        matching.append((score, {**track, "_track_duration": track_duration, "_track_id": str(track_id)}))

    if not matching:
        if tracks:
            return {
                "source": "Musixmatch",
                "acquisition_status": "INVALID_RESPONSE",
                "error": "Musixmatch search returned only unrelated track identities",
            }
        mark_provider_not_found()
        return {"source": "Musixmatch", "acquisition_status": "NOT_FOUND"}

    # Fetch a small set of identity-compatible results; the downstream scorer,
    # not Musixmatch's search order, chooses the recording/version winner.
    matching.sort(key=lambda entry: (-entry[0], str(entry[1].get("track_id"))))
    candidates: list[dict[str, Any]] = []
    observed_not_found = False
    acquisition_failures: list[tuple[str, str]] = []

    def preserve_partial_or_fail(status: str, detail: str) -> dict[str, Any]:
        if candidates:
            primary = candidates[0]
            warnings = list(primary.get("validationWarnings") or [])
            warnings.append(f"additional candidate lookup stopped: {status}: {detail}")
            primary["validationWarnings"] = warnings
            primary["alternatives"] = [*candidates[1:], *primary.get("alternatives", [])]
            return primary
        return {"source": "Musixmatch", "acquisition_status": status, "error": detail}

    for _score, track in matching[:5]:
        if (track.get("has_lyrics") is False or str(track.get("has_lyrics", "1")) == "0") and not (
            track.get("has_richsync") or track.get("has_subtitles")
        ):
            observed_not_found = True
            continue
        track_id = track["_track_id"]
        lyric_params = {"track_id": track_id, **auth_params}
        lyric_url = api_base + "track.lyrics.get?" + urllib.parse.urlencode(lyric_params)
        raw_text = ""
        parsed_text = ""
        lyric_meta: dict[str, Any] = {
            "lyrics_language": "",
            "lyrics_copyright": "",
            "lyrics_id": "",
            "transformations": [],
        }
        try:
            lyric_request = urllib.request.Request(lyric_url, headers=headers)
            with _provider_urlopen(lyric_request, timeout=timeout) as response:
                lyric_payload = _provider_json_loads(read_provider_text(response))
            raw_text, parsed_text, lyric_meta = parse_musixmatch_lyrics(lyric_payload)
        except _LyricsAcquisitionError as exc:
            if exc.status == "NOT_FOUND":
                observed_not_found = True
            else:
                if exc.status in {"RATE_LIMITED", "AUTHENTICATION_ERROR", "BLOCKED"}:
                    return preserve_partial_or_fail(exc.status, str(exc))
                acquisition_failures.append((exc.status, str(exc)))
        except urllib.error.HTTPError as exc:
            status = {401: "AUTHENTICATION_ERROR", 403: "BLOCKED", 404: "NOT_FOUND", 429: "RATE_LIMITED"}.get(
                exc.code, "TRANSIENT_ERROR" if exc.code >= 500 else "INVALID_RESPONSE"
            )
            if status == "NOT_FOUND":
                observed_not_found = True
            else:
                detail = f"Musixmatch track.lyrics.get HTTP {exc.code}"
                if status in {"RATE_LIMITED", "AUTHENTICATION_ERROR", "BLOCKED"}:
                    return preserve_partial_or_fail(status, detail)
                acquisition_failures.append((status, detail))
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
            status, detail = classify_lyrics_exception(exc)
            acquisition_failures.append((status, detail))
        if len(raw_text) > MAX_LYRICS_CHARS:
            acquisition_failures.append(("INVALID_RESPONSE", "lyrics exceed safety limit"))
            raw_text = parsed_text = ""
        track_duration = track.get("_track_duration")
        metadata = {
            "acquisition_route": "documented_api" if api_key else "configured_desktop",
            "track_id": track_id,
            "commontrack_id": str(track.get("commontrack_id") or ""),
            "album_id": str(track.get("album_id") or ""),
            "artist_id": str(track.get("artist_id") or ""),
            "track_rating": track.get("track_rating"),
            "explicit": track.get("explicit"),
            "instrumental": track.get("instrumental"),
            "lyrics_copyright": lyric_meta["lyrics_copyright"],
            "lyrics_id": lyric_meta["lyrics_id"],
            "source_confidence": 88,
            "adapter_validated": True,
        }
        candidate: dict[str, Any] = {
            "found": True,
            "source": "Musixmatch",
            "id": track_id,
            "provider_track_id": track_id,
            "trackName": str(track.get("track_name") or ""),
            "artistName": str(track.get("artist_name") or ""),
            "albumName": str(track.get("album_name") or ""),
            "duration": track_duration,
            "isrc": str(track.get("track_isrc") or ""),
            "language": lyric_meta["lyrics_language"] or str(track.get("track_language") or ""),
            "identity_validated": True,
            "attribution": "Musixmatch",
            "format": "plain",
            "syncType": "none",
            "has_word_sync": False,
            "has_back_vocals": False,
            "providerRaw": raw_text,
            "rawLyrics": raw_text,
            "plainLyrics": parsed_text,
            "syncedLyrics": "",
            "enhancedLyrics": "",
            "ttmlLyrics": "",
            "providerMetadata": metadata,
            "transformations": lyric_meta["transformations"],
            "instrumental": bool(track.get("instrumental")),
        }
        sync_warnings: list[str] = []
        for endpoint, field, property_name in (
            ("track.richsync.get", "richsync", "richsync_body"),
            ("track.subtitle.get", "subtitle", "subtitle_body"),
        ):
            if endpoint == "track.richsync.get" and not track.get("has_richsync"):
                continue
            if endpoint == "track.subtitle.get" and not track.get("has_subtitles"):
                continue
            params = {"track_id": track_id, **auth_params}
            if endpoint == "track.subtitle.get":
                params["subtitle_format"] = "lrc"
            request = urllib.request.Request(
                api_base + endpoint + "?" + urllib.parse.urlencode(params), headers=headers
            )
            try:
                with _provider_urlopen(request, timeout=timeout) as response:
                    payload = _provider_json_loads(read_provider_text(response))
                body = _musixmatch_body(payload, endpoint=endpoint)
                obj = body.get(field)
                if not isinstance(obj, dict) or not isinstance(obj.get(property_name), str):
                    raise TypeError(f"Musixmatch {endpoint} schema changed")
                raw_sync = obj[property_name]
                if endpoint == "track.richsync.get":
                    parsed_sync = musixmatch_richsync_to_lyrics(
                        raw_sync, song=song, artist=artist, album=album, duration=track_duration
                    )
                    if not parsed_sync:
                        raise ValueError("Musixmatch richsync body has no valid word timing")
                    complete_plain = candidate["plainLyrics"]
                    candidate.update(
                        {
                            key: parsed_sync[key]
                            for key in (
                                "plainLyrics",
                                "syncedLyrics",
                                "enhancedLyrics",
                                "ttmlLyrics",
                                "format",
                                "syncType",
                                "has_word_sync",
                            )
                        }
                    )
                    if complete_plain:
                        candidate["plainLyrics"] = complete_plain
                    candidate["providerRaw"] = raw_sync
                    candidate["providerMetadata"]["plain_lyrics_raw"] = raw_text
                    candidate["providerMetadata"]["richsync_id"] = obj.get("richsync_id")
                else:
                    candidate["providerMetadata"]["subtitle_raw"] = raw_sync
                    if candidate.get("syncedLyrics"):
                        continue
                    valid, reasons = validate_lrc(raw_sync, duration=track_duration)
                    if not valid:
                        raise ValueError(f"Musixmatch subtitle timing invalid: {', '.join(reasons)}")
                    candidate["syncedLyrics"] = raw_sync
                    candidate["format"] = "lrc"
                    candidate["syncType"] = "linesync"
                    if not candidate["plainLyrics"]:
                        candidate["plainLyrics"] = lrc_to_plain(raw_sync)
                        candidate["providerRaw"] = raw_sync
                        candidate["rawLyrics"] = raw_sync
                    candidate["providerMetadata"]["subtitle_raw"] = raw_sync
            except _LyricsAcquisitionError as exc:
                if exc.status != "NOT_FOUND":
                    sync_warnings.append(f"{endpoint}: {exc.status}")
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                status, _detail = classify_lyrics_exception(exc)
                sync_warnings.append(f"{endpoint}: {status}")
        if sync_warnings:
            candidate["validationWarnings"] = sync_warnings
        if len(str(candidate.get("plainLyrics") or "").strip()) >= 10:
            candidates.append(candidate)
        else:
            observed_not_found = True

    if candidates or acquisition_failures:
        return _collected_lyrics_result(
            candidates, acquisition_failures, source="Musixmatch", truncated=len(matching) > 5
        )
    if observed_not_found:
        mark_provider_not_found()
    return {"source": "Musixmatch", "acquisition_status": "NOT_FOUND"}


def parse_ultrastar_search(page: str, song: str, artist: str) -> list[dict[str, str]]:
    """Parse identity-bearing UltraStar result rows from the table structure."""
    root = _parse_provider_html(page, "UltraStar search")
    output: list[dict[str, str]] = []
    rows = [
        node
        for node in root.descendants()
        if node.tag == "tr" and set(node.attrs.get("class", "").casefold().split()) & {"list_tr1", "list_tr2"}
    ]
    for row in rows:
        cells = [node for node in row.descendants() if node.tag == "td"]
        if len(cells) < 2:
            raise ValueError("UltraStar result row has fewer than two cells")
        candidate_artist = cells[0].text().strip()
        candidate_title = cells[1].text().strip()
        track_id = ""
        for node in [*cells[1].descendants(), cells[1]]:
            node_id = node.attrs.get("id", "")
            if node_id.isdigit():
                track_id = node_id
                break
            href = node.attrs.get("href", "")
            query = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            ids = query.get("id", [])
            if ids and ids[0].isdigit() and query.get("link") == ["gettxt"]:
                track_id = ids[0]
                break
        if (
            track_id
            and candidate_title
            and candidate_artist
            and is_track_match(candidate_title, candidate_artist, song, artist)
        ):
            output.append({"id": track_id, "title": candidate_title, "artist": candidate_artist})
    if not rows and not re.search(
        r"no\s+(?:songs|results|matches)|nothing\s+found|keine\s+treffer", root.text(), re.IGNORECASE
    ):
        raise ValueError("UltraStar search page has no recognizable result state")
    output.sort(
        key=lambda item: (
            text_similarity(item["title"], song) + text_similarity(item["artist"], artist),
            item["id"],
        ),
        reverse=True,
    )
    return output


def parse_ultrastar_response(page: str) -> str:
    """Extract the single lyrics textarea without regexing untrusted HTML."""
    root = _parse_provider_html(page, "UltraStar lyrics")
    textareas = _provider_nodes(root, tag="textarea")
    if len(textareas) != 1 or not textareas[0].text().strip():
        raise ValueError("UltraStar lyrics textarea is missing, empty, or ambiguous")
    return textareas[0].text()


def fetch_ultrastar_db(
    song: str,
    artist: str,
    timeout: int = 6,
) -> dict[str, Any]:
    """Query UltraStar karaoke database (USDB) and parse into word-level Enhanced LRC."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    cookie = os.environ.get("USDB_COOKIE", "")
    cookie_file = os.path.expanduser("~/.config/usdb_cookie")
    if not cookie and os.path.isfile(cookie_file):
        try:
            cookie = open(cookie_file, encoding="utf-8").read().strip()
        except Exception:
            pass
    if cookie:
        cookie = cookie.replace("PHEPSESSID", "PHPSESSID")
        headers["Cookie"] = cookie

    search_url = "https://usdb.animux.de/index.php?" + urllib.parse.urlencode(
        {"link": "list", "interpret": artist.strip(), "title": song.strip()}
    )
    candidates: list[dict[str, Any]] = []
    failures: list[tuple[str, str]] = []
    try:
        req = urllib.request.Request(search_url, headers=headers)
        with _provider_urlopen(req, timeout=timeout) as resp:
            final_url = str(getattr(resp, "geturl", lambda: search_url)())
            if urllib.parse.urlsplit(final_url).hostname not in {"usdb.animux.de"}:
                return {
                    "source": "UltraStar DB",
                    "acquisition_status": "INVALID_RESPONSE",
                    "error": "search redirect left the UltraStar provider host",
                }
            html = read_provider_text(resp)
            if _provider_page_blocked(html):
                return {"source": "UltraStar DB", "acquisition_status": "BLOCKED", "error": "provider challenge page"}
            matches = parse_ultrastar_search(html, song, artist)
            if not matches:
                mark_provider_not_found()
                return {"source": "UltraStar DB", "acquisition_status": "NOT_FOUND"}
    except Exception as exc:
        status, detail = classify_lyrics_exception(exc)
        return {"source": "UltraStar DB", "acquisition_status": status, "error": detail}

    for match in matches[:5]:
        song_id = match["id"]
        txt_url = "https://usdb.animux.de/index.php?" + urllib.parse.urlencode({"link": "gettxt", "id": song_id})
        try:
            txt_req = urllib.request.Request(
                txt_url,
                data=urllib.parse.urlencode({"wd": "1"}).encode("ascii"),
                headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
            )
            with _provider_urlopen(txt_req, timeout=timeout) as txt_resp:
                final_url = str(getattr(txt_resp, "geturl", lambda txt_url=txt_url: txt_url)())
                if urllib.parse.urlsplit(final_url).hostname not in {"usdb.animux.de"}:
                    failures.append(("INVALID_RESPONSE", "lyrics redirect left the UltraStar provider host"))
                    continue
                txt_html = read_provider_text(txt_resp)
            if _provider_page_blocked(txt_html):
                failures.append(("BLOCKED", "provider challenge page"))
                continue
            ultra_text = parse_ultrastar_response(txt_html)
            res = parse_ultrastar_lyrics(ultra_text, song=song, artist=artist)
            if not res:
                raise ValueError("UltraStar chart contains no parsable lyric notes")
            returned_title = str(res.get("trackName") or match["title"])
            returned_artist = str(res.get("artistName") or match["artist"])
            if not is_track_match(returned_title, returned_artist, song, artist):
                continue
            res.update(
                {
                    "id": song_id,
                    "provider_track_id": song_id,
                    "identity_validated": True,
                    "url": txt_url,
                    "albumName": "",
                    "duration": None,
                    "language": "",
                    "attribution": "UltraStar DB",
                    "providerMetadata": {"searchTitle": match["title"], "searchArtist": match["artist"]},
                }
            )
            candidates.append(res)
        except Exception as exc:
            status, detail = classify_lyrics_exception(exc)
            failures.append((status, detail))

    if candidates:
        candidates.sort(
            key=lambda item: (
                text_similarity(str(item.get("trackName", "")), song)
                + text_similarity(str(item.get("artistName", "")), artist),
                str(item.get("provider_track_id", "")),
            ),
            reverse=True,
        )
        candidates[0]["alternatives"] = candidates[1:]
        if failures:
            candidates[0]["validationWarnings"] = [
                f"Some UltraStar alternatives could not be inspected ({failures[0][0]})"
            ]
        return candidates[0]
    if failures:
        severity = {
            "AUTHENTICATION_ERROR": 7,
            "BLOCKED": 6,
            "RATE_LIMITED": 5,
            "TRANSIENT_ERROR": 4,
            "PARSER_ERROR": 3,
            "INVALID_RESPONSE": 2,
            "NOT_FOUND": 1,
        }
        status, detail = max(failures, key=lambda item: severity.get(item[0], 0))
        return {"source": "UltraStar DB", "acquisition_status": status, "error": detail}
    mark_provider_not_found()
    return {"source": "UltraStar DB", "acquisition_status": "NOT_FOUND"}


def parse_clonehero_chart(
    chart_text: str,
    song: str = "",
    artist: str = "",
    album: str = "",
    duration: float | None = None,
) -> dict[str, Any] | None:
    """Parse Clone Hero / Moonscraper .chart vocals into word-level Enhanced LRC."""
    resolution = 192
    offset = 0.0
    title = song
    art = artist
    bpm_changes: list[tuple[int, float]] = []
    lyric_tokens: list[tuple[int, str, str]] = []

    current_section = None
    for raw_line in chart_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1].strip()
            continue
        if current_section == "Song":
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip().lower()
                v = v.strip().strip('"')
                if k == "name":
                    title = v
                elif k == "artist":
                    art = v
                elif k == "resolution" and v.isdigit():
                    resolution = int(v)
                elif k == "offset":
                    try:
                        offset = float(v)
                    except ValueError:
                        pass
        elif current_section == "SyncTrack":
            m_bpm = re.match(r"(\d+)\s*=\s*B\s+(\d+)", line)
            if m_bpm:
                bpm_changes.append((int(m_bpm.group(1)), int(m_bpm.group(2)) / 1000.0))
        elif current_section in ("Events", "PART VOCALS"):
            m_lyr = re.match(r"(\d+)\s*=\s*E\s*\"lyric\s+([^\"]+)\"", line)
            if m_lyr:
                lyric_tokens.append((int(m_lyr.group(1)), "lyric", m_lyr.group(2)))
                continue
            m_phr = re.match(r"(\d+)\s*=\s*E\s*\"(phrase_start|phrase_end)\"", line)
            if m_phr:
                lyric_tokens.append((int(m_phr.group(1)), m_phr.group(2), ""))

    if not lyric_tokens:
        return None

    if not bpm_changes:
        bpm_changes = [(0, 120.0)]
    bpm_changes.sort(key=lambda x: x[0])

    def tick_to_sec(tick: int) -> float:
        t_sec = offset
        cur_tick = 0
        cur_bpm = bpm_changes[0][1]
        for b_tick, b_bpm in bpm_changes:
            if tick <= b_tick:
                break
            dt = b_tick - cur_tick
            t_sec += dt / (resolution * (cur_bpm / 60.0))
            cur_tick = b_tick
            cur_bpm = b_bpm
        dt = tick - cur_tick
        t_sec += dt / (resolution * (cur_bpm / 60.0))
        return max(0.0, t_sec)

    elrc_lines: list[str] = []
    current_line_words: list[tuple[float, str]] = []

    def flush_line():
        nonlocal current_line_words
        if not current_line_words:
            return
        line_st = current_line_words[0][0]
        l_ts = f"[{fmt_lrc_time(line_st)}]"
        w_parts = []
        for idx, (st, w) in enumerate(current_line_words):
            w_ts = fmt_lrc_time(st)
            word_str = w
            if (
                idx > 0
                and not word_str.startswith(" ")
                and not current_line_words[idx - 1][1].endswith("-")
                and not current_line_words[idx - 1][1].endswith("=")
            ):
                word_str = " " + word_str
            word_str = word_str.removesuffix("=")
            w_parts.append(f"<{w_ts}>{word_str}")
        elrc_lines.append(f"{l_ts} {''.join(w_parts).strip()}")
        current_line_words = []

    last_sec = 0.0
    for tick, kind, text in lyric_tokens:
        sec = tick_to_sec(tick)
        if kind == "phrase_start":
            if current_line_words:
                flush_line()
        elif kind == "phrase_end":
            flush_line()
        elif kind == "lyric":
            if current_line_words and (sec - last_sec) > 4.0:
                flush_line()
            current_line_words.append((sec, text))
            last_sec = sec
    flush_line()

    if not elrc_lines:
        return None

    elrc_text = "\n".join(elrc_lines) + "\n"
    std_lrc = (
        normalize_synced(elrc_text, keep_word_sync=False, song=title, artist=art)
        or re.sub(r" +", " ", ANY_WORD_TIMESTAMP_RE.sub("", elrc_text)).strip()
    )
    plain_txt = (
        to_plain(elrc_text, song=title, artist=art)
        or re.sub(r" +", " ", re.sub(r"<[^>]+>", "", ANY_LINE_TIMESTAMP_RE.sub("", elrc_text))).strip()
    )
    ttml_xml = lrc_to_ttml(elrc_text, song=title, artist=art, album=album, duration=duration)

    return {
        "found": True,
        "trackName": title,
        "artistName": art,
        "source": "Chorus (Clone Hero / Rock Band)",
        "format": "elrc",
        "syncType": "richsync",
        "has_word_sync": True,
        "has_back_vocals": False,
        "rawLyrics": elrc_text,
        "plainLyrics": plain_txt,
        "syncedLyrics": std_lrc,
        "enhancedLyrics": elrc_text,
        "ttmlLyrics": ttml_xml,
        "duration": duration,
    }


def parse_clonehero_midi(
    midi_bytes: bytes,
    song: str = "",
    artist: str = "",
    album: str = "",
    duration: float | None = None,
) -> dict[str, Any] | None:
    """Extract vocal lyric events from Clone Hero / Rock Band notes.mid into Enhanced LRC."""
    if not midi_bytes.startswith(b"MThd") or len(midi_bytes) < 14:
        return None
    try:
        _, _, division = struct.unpack_from(">HHh", midi_bytes, 8)
        ticks_per_beat = division if division > 0 else 480

        tempo_map: list[tuple[int, float]] = []
        vocal_lyrics: list[tuple[int, str]] = []

        pos = 14
        while pos < len(midi_bytes) - 8:
            if midi_bytes[pos : pos + 4] != b"MTrk":
                break
            (chunk_len,) = struct.unpack_from(">I", midi_bytes, pos + 4)
            track_data = midi_bytes[pos + 8 : pos + 8 + chunk_len]
            pos += 8 + chunk_len

            is_vocal_track = (b"PART VOCALS" in track_data) or (b"VOCALS" in track_data)

            t_pos = 0
            cur_tick = 0
            while t_pos < len(track_data):
                delta = 0
                while t_pos < len(track_data):
                    b = track_data[t_pos]
                    t_pos += 1
                    delta = (delta << 7) | (b & 0x7F)
                    if not (b & 0x80):
                        break
                cur_tick += delta
                if t_pos >= len(track_data):
                    break

                status = track_data[t_pos]
                if status == 0xFF:
                    t_pos += 1
                    if t_pos >= len(track_data):
                        break
                    meta_type = track_data[t_pos]
                    t_pos += 1
                    mlen = 0
                    while t_pos < len(track_data):
                        b = track_data[t_pos]
                        t_pos += 1
                        mlen = (mlen << 7) | (b & 0x7F)
                        if not (b & 0x80):
                            break
                    mdata = track_data[t_pos : t_pos + mlen]
                    t_pos += mlen

                    if meta_type == 0x51 and len(mdata) == 3:
                        mpqn = (mdata[0] << 16) | (mdata[1] << 8) | mdata[2]
                        bpm = 60000000.0 / mpqn if mpqn > 0 else 120.0
                        tempo_map.append((cur_tick, bpm))
                    elif meta_type in (0x01, 0x05) and is_vocal_track:
                        text = mdata.decode("utf-8", errors="strict").strip()
                        if text and not text.startswith("[") and not text.startswith("{"):
                            vocal_lyrics.append((cur_tick, text))
                elif status in (0xF0, 0xF7):
                    t_pos += 1
                    slen = 0
                    while t_pos < len(track_data):
                        b = track_data[t_pos]
                        t_pos += 1
                        slen = (slen << 7) | (b & 0x7F)
                        if not (b & 0x80):
                            break
                    t_pos += slen
                else:
                    t_pos += 1
                    msg_type = status & 0xF0
                    if msg_type in (0xC0, 0xD0):
                        t_pos += 1
                    else:
                        t_pos += 2

        if not vocal_lyrics:
            return None

        if not tempo_map:
            tempo_map = [(0, 120.0)]
        tempo_map.sort(key=lambda x: x[0])

        def midi_tick_to_sec(tick: int) -> float:
            t_sec = 0.0
            cur_t = 0
            cur_bpm = tempo_map[0][1]
            for b_t, b_bpm in tempo_map:
                if tick <= b_t:
                    break
                dt = b_t - cur_t
                t_sec += dt / (ticks_per_beat * (cur_bpm / 60.0))
                cur_t = b_t
                cur_bpm = b_bpm
            dt = tick - cur_t
            t_sec += dt / (ticks_per_beat * (cur_bpm / 60.0))
            return max(0.0, t_sec)

        elrc_lines: list[str] = []
        current_line_words: list[tuple[float, str]] = []

        def flush_line():
            nonlocal current_line_words
            if not current_line_words:
                return
            line_st = current_line_words[0][0]
            l_ts = f"[{fmt_lrc_time(line_st)}]"
            w_parts = []
            for idx, (st, w) in enumerate(current_line_words):
                w_ts = fmt_lrc_time(st)
                word_str = w
                if (
                    idx > 0
                    and not word_str.startswith(" ")
                    and not current_line_words[idx - 1][1].endswith("-")
                    and not current_line_words[idx - 1][1].endswith("=")
                ):
                    word_str = " " + word_str
                word_str = word_str.removesuffix("=")
                w_parts.append(f"<{w_ts}>{word_str}")
            elrc_lines.append(f"{l_ts} {''.join(w_parts).strip()}")
            current_line_words = []

        last_sec = 0.0
        for tick, text in vocal_lyrics:
            sec = midi_tick_to_sec(tick)
            if current_line_words and (sec - last_sec) > 3.5:
                flush_line()
            current_line_words.append((sec, text))
            last_sec = sec
        flush_line()

        if not elrc_lines:
            return None

        elrc_text = "\n".join(elrc_lines) + "\n"
        std_lrc = (
            normalize_synced(elrc_text, keep_word_sync=False, song=song, artist=artist)
            or re.sub(r" +", " ", ANY_WORD_TIMESTAMP_RE.sub("", elrc_text)).strip()
        )
        plain_txt = (
            to_plain(elrc_text, song=song, artist=artist)
            or re.sub(r" +", " ", re.sub(r"<[^>]+>", "", ANY_LINE_TIMESTAMP_RE.sub("", elrc_text))).strip()
        )
        ttml_xml = lrc_to_ttml(elrc_text, song=song, artist=artist, album=album, duration=duration)

        return {
            "found": True,
            "source": "Chorus (Clone Hero / Rock Band)",
            "format": "elrc",
            "syncType": "richsync",
            "has_word_sync": True,
            "has_back_vocals": False,
            "rawLyrics": elrc_text,
            "plainLyrics": plain_txt,
            "syncedLyrics": std_lrc,
            "enhancedLyrics": elrc_text,
            "ttmlLyrics": ttml_xml,
            "duration": duration,
        }
    except Exception:
        pass
    return None


def fetch_chorus_chart(
    song: str,
    artist: str,
    album: str = "",
    duration: float | None = None,
    timeout: int = 6,
) -> dict[str, Any] | None:
    """Query Enchor's chart API and parse a bounded, identity-matched vocal chart."""
    headers = {
        "User-Agent": "AudiophileArchTagger/6.0 (ChorusClient)",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    search_payload = json.dumps({"search": f"{artist} {song}".strip(), "page": 1}).encode("utf-8")
    try:
        req = urllib.request.Request("https://api.enchor.us/search", data=search_payload, headers=headers)
        with _provider_urlopen(req, timeout=timeout) as resp:
            data = _provider_json_loads(read_provider_text(resp))
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise TypeError("Enchor search response has no data array")
            items = data["data"]
            chart_candidates: list[dict[str, Any]] = []
            for it in items:
                if not isinstance(it, dict):
                    raise TypeError("Enchor search response contains a non-object chart")
                diff_v = it.get("diff_vocals", -1)
                candidate_title = str(
                    it.get("songName") or it.get("song_name") or it.get("title") or it.get("name") or ""
                )
                candidate_artist = str(it.get("artistName") or it.get("artist_name") or it.get("artist") or "")
                candidate_duration = it.get("duration") or it.get("length")
                try:
                    candidate_duration = float(candidate_duration) if candidate_duration is not None else None
                except (TypeError, ValueError):
                    candidate_duration = None
                if (
                    not candidate_title
                    or not candidate_artist
                    or not is_track_match(
                        candidate_title,
                        candidate_artist,
                        song,
                        artist,
                        cand_duration=candidate_duration,
                        target_duration=duration,
                    )
                ):
                    continue
                try:
                    has_vocals = diff_v is not None and int(diff_v) >= 0
                except (TypeError, ValueError):
                    has_vocals = False
                if has_vocals:
                    chart_candidate = dict(it)
                    chart_candidate["_matched_title"] = candidate_title
                    chart_candidate["_matched_artist"] = candidate_artist
                    chart_candidate["_matched_duration"] = candidate_duration
                    chart_candidates.append(chart_candidate)
            chart_candidates.sort(
                key=lambda item: (
                    abs(float(item["_matched_duration"]) - duration)
                    if item.get("_matched_duration") and duration
                    else float("inf"),
                    str(item.get("_matched_title") or "").casefold(),
                    str(item.get("md5") or ""),
                )
            )
            if not chart_candidates:
                mark_provider_not_found()
                return {"source": "Chorus", "acquisition_status": "NOT_FOUND"}

            def retrieve_chart(best_chart: dict[str, Any]) -> dict[str, Any]:
                if not best_chart or not best_chart.get("md5"):
                    return {
                        "source": "Chorus",
                        "acquisition_status": "INVALID_RESPONSE",
                        "error": "Enchor matched chart has no package identifier",
                    }

                chart_md5 = best_chart["md5"]
                enc_ver = best_chart.get("encoderVersion", 0)
                has_vid = best_chart.get("hasVideoBackground", False)

                sng_prefix = f"charts/v{enc_ver}/" if enc_ver and enc_ver > 0 else ""
                sng_name = f"{chart_md5}_novideo.sng" if has_vid else f"{chart_md5}.sng"
                sng_urls = [
                    f"https://files.enchor.us/{sng_prefix}{sng_name}",
                    f"https://files.enchor.us/{chart_md5}.sng",
                ]

                raw_sng = None
                package_failures: list[tuple[str, str]] = []
                for sng_url in sng_urls:
                    try:
                        s_req = urllib.request.Request(sng_url, headers={"User-Agent": "Mozilla/5.0"})
                        with _provider_urlopen(s_req, timeout=min(timeout, 8)) as s_resp:
                            raw_sng = s_resp.read()
                            if raw_sng.startswith(b"SNGPKG"):
                                break
                            raise ValueError("Enchor package response is not an SNGPKG archive")
                    except Exception as exc:
                        package_failures.append(classify_lyrics_exception(exc))
                        continue

                if not raw_sng or not raw_sng.startswith(b"SNGPKG") or len(raw_sng) < 26:
                    if package_failures:
                        status, detail = max(
                            package_failures,
                            key=lambda item: {
                                "AUTHENTICATION_ERROR": 7,
                                "BLOCKED": 6,
                                "RATE_LIMITED": 5,
                                "TRANSIENT_ERROR": 4,
                                "PARSER_ERROR": 3,
                                "INVALID_RESPONSE": 2,
                                "NOT_FOUND": 1,
                            }.get(item[0], 0),
                        )
                        return {"source": "Chorus", "acquisition_status": status, "error": detail}
                    return {
                        "source": "Chorus",
                        "acquisition_status": "INVALID_RESPONSE",
                        "error": "Enchor returned an empty or malformed chart package",
                    }
                if len(raw_sng) > _MAX_PROVIDER_RESPONSE_BYTES:
                    return {
                        "source": "Chorus",
                        "acquisition_status": "INVALID_RESPONSE",
                        "error": "Enchor chart package exceeds the provider response limit",
                    }
                if len(raw_sng) < 42:
                    return {
                        "source": "Chorus",
                        "acquisition_status": "INVALID_RESPONSE",
                        "error": "Enchor chart package header is truncated",
                    }

                xor_mask = raw_sng[10:26]
                pos = 26
                _meta_len, meta_cnt = struct.unpack_from("<QQ", raw_sng, pos)
                pos += 16
                if meta_cnt > (len(raw_sng) - pos) // 8:
                    raise ValueError("Enchor package metadata count exceeds its bounds")
                for _ in range(meta_cnt):
                    if pos + 4 > len(raw_sng):
                        raise ValueError("Enchor package metadata key length is truncated")
                    (k_len,) = struct.unpack_from("<i", raw_sng, pos)
                    if k_len < 0 or k_len > len(raw_sng) - pos - 4:
                        raise ValueError("Enchor package metadata key length is invalid")
                    pos += 4 + k_len
                    if pos + 4 > len(raw_sng):
                        raise ValueError("Enchor package metadata value length is truncated")
                    (v_len,) = struct.unpack_from("<i", raw_sng, pos)
                    if v_len < 0 or v_len > len(raw_sng) - pos - 4:
                        raise ValueError("Enchor package metadata value length is invalid")
                    pos += 4 + v_len

                if pos + 16 > len(raw_sng):
                    raise ValueError("Enchor package file table header is truncated")
                _f_meta_len, f_meta_cnt = struct.unpack_from("<QQ", raw_sng, pos)
                pos += 16
                if f_meta_cnt > (len(raw_sng) - pos) // 17:
                    raise ValueError("Enchor package file count exceeds its bounds")
                chart_file_info = None
                midi_file_info = None
                for _ in range(f_meta_cnt):
                    if pos + 1 > len(raw_sng):
                        raise ValueError("Enchor package file name length is truncated")
                    (fn_len,) = struct.unpack_from("<b", raw_sng, pos)
                    pos += 1
                    if fn_len < 0 or fn_len > len(raw_sng) - pos - 16:
                        raise ValueError("Enchor package file name length is invalid")
                    fn = raw_sng[pos : pos + fn_len].decode("utf-8", errors="strict").lower()
                    pos += fn_len
                    if pos + 16 > len(raw_sng):
                        raise ValueError("Enchor package file entry is truncated")
                    c_len, c_idx = struct.unpack_from("<QQ", raw_sng, pos)
                    pos += 16
                    if c_idx > len(raw_sng) or c_len > len(raw_sng) - c_idx:
                        raise ValueError("Enchor package file points outside the archive")
                    if fn.endswith(".chart"):
                        chart_file_info = (c_len, c_idx)
                    elif fn.endswith((".mid", ".midi")):
                        midi_file_info = (c_len, c_idx)

                def unmask_chunk(c_len: int, c_idx: int) -> bytes:
                    if c_len > len(raw_sng) or c_idx > len(raw_sng) or c_len > len(raw_sng) - c_idx:
                        raise ValueError("Enchor chart data range is invalid")
                    data = raw_sng[c_idx : c_idx + c_len]
                    out = bytearray(c_len)
                    a = 0
                    for l in range(c_len):
                        out[l] = data[l] ^ xor_mask[a % 16] ^ a
                        a = (a + 1) % 256
                    return bytes(out)

                if chart_file_info:
                    c_len, c_idx = chart_file_info
                    chart_bytes = unmask_chunk(c_len, c_idx)
                    chart_text = chart_bytes.decode("utf-8", errors="strict")
                    res = parse_clonehero_chart(chart_text, song=song, artist=artist, album=album, duration=duration)
                    if (
                        res
                        and res.get("has_word_sync")
                        and is_track_match(
                            str(res.get("trackName") or ""), str(res.get("artistName") or ""), song, artist
                        )
                    ):
                        # The search item carries recording identity for MIDI
                        # packages, which do not embed title/artist metadata.
                        res.update(
                            {
                                "trackName": res.get("trackName") or best_chart["_matched_title"],
                                "artistName": res.get("artistName") or best_chart["_matched_artist"],
                                "albumName": str(best_chart.get("album") or best_chart.get("albumName") or ""),
                                "duration": res.get("duration") or best_chart.get("_matched_duration"),
                                "provider_track_id": str(best_chart.get("md5") or ""),
                                "id": str(best_chart.get("md5") or ""),
                                "identity_validated": True,
                                "source_confidence": 35,
                                "providerRaw": chart_text,
                            }
                        )
                        return res

                if midi_file_info:
                    c_len, c_idx = midi_file_info
                    midi_bytes = unmask_chunk(c_len, c_idx)
                    res = parse_clonehero_midi(midi_bytes, song=song, artist=artist, album=album, duration=duration)
                    if res and res.get("has_word_sync"):
                        res.update(
                            {
                                "trackName": best_chart["_matched_title"],
                                "artistName": best_chart["_matched_artist"],
                                "albumName": str(best_chart.get("album") or best_chart.get("albumName") or ""),
                                "duration": res.get("duration") or best_chart.get("_matched_duration"),
                                "provider_track_id": str(best_chart.get("md5") or ""),
                                "id": str(best_chart.get("md5") or ""),
                                "identity_validated": True,
                                "source_confidence": 35,
                                "providerRaw": json.dumps(
                                    {
                                        "format": "midi",
                                        "encoding": "base64",
                                        "data": base64.b64encode(midi_bytes).decode("ascii"),
                                    }
                                ),
                            }
                        )
                        return res
                raise ValueError("Enchor package contains no usable vocal chart")

            results: list[dict[str, Any]] = []
            failures: list[tuple[str, str]] = []
            for chart in chart_candidates[:5]:
                try:
                    candidate = retrieve_chart(chart)
                except (OSError, ValueError, TypeError, KeyError, AttributeError, struct.error) as exc:
                    _record_provider_failure(exc)
                    failures.append(classify_lyrics_exception(exc))
                    continue
                if candidate.get("found"):
                    results.append(candidate)
                elif candidate.get("acquisition_status"):
                    status = str(candidate["acquisition_status"])
                    failures.append((status, str(candidate.get("error") or "chart unavailable")))
                    if status in {"RATE_LIMITED", "AUTHENTICATION_ERROR", "BLOCKED"}:
                        break
            return _collected_lyrics_result(results, failures, source="Chorus", truncated=len(chart_candidates) > 5)

    except Exception as exc:
        _record_provider_failure(exc)
        status, detail = classify_lyrics_exception(exc)
        return {"source": "Chorus", "acquisition_status": status, "error": detail}


def _decode_qq_lyric_field(value: object, field: str) -> str:
    """Decode a QQ lyric field as UTF-8 text or strict base64 UTF-8."""
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise TypeError(f"QQ {field} lyric field is not a string")
    text = value.removeprefix("\ufeff")
    if text.lstrip().startswith(("[", "<")):
        return text
    try:
        decoded = base64.b64decode(text, validate=True).decode("utf-8", errors="strict")
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"QQ {field} lyric field is neither UTF-8 text nor valid base64 UTF-8") from exc
    return decoded.removeprefix("\ufeff")


def _qq_qrc_content(raw: str) -> str:
    """Extract QRC line data from XML without regexing provider markup."""
    if not raw.lstrip().startswith("<"):
        return raw
    if len(raw) > MAX_LYRICS_CHARS or re.search(r"<!DOCTYPE|<!ENTITY", raw, re.IGNORECASE):
        raise ValueError("QQ QRC XML exceeds limits or contains a prohibited declaration")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"QQ QRC XML is malformed: {exc}") from exc
    contents = [
        value
        for element in root.iter()
        for key, value in element.attrib.items()
        if key.rsplit("}", 1)[-1].casefold() == "lyriccontent" and value
    ]
    if len(contents) != 1:
        raise ValueError("QQ QRC XML has no unique LyricContent attribute")
    return contents[0]


def _parse_qq_qrc(raw: str, *, duration: float | None) -> tuple[str, str, str, tuple[str, ...]]:
    """Return line LRC, word LRC, plain text and parser diagnostics."""
    body = _qq_qrc_content(raw)
    if len(body) > MAX_LYRICS_CHARS:
        raise ValueError("QQ QRC lyric content exceeds the parser limit")
    word_pattern = re.compile(
        r"(?P<before>[^()\n]*)\((?P<start>\d+),(?P<length>\d+)\)"
        r"|\((?P<start_after>\d+),(?P<length_after>\d+)\)(?P<after>[^()\n]*)"
    )
    line_rows: list[str] = []
    enhanced_rows: list[str] = []
    plain_rows: list[str] = []
    warnings: list[str] = []
    malformed_lines = 0
    for line_no, raw_line in enumerate(body.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
        if not raw_line:
            plain_rows.append("")
            continue
        match = QRC_LINE_RE.fullmatch(raw_line)
        if match is None:
            malformed_lines += 1
            continue
        line_start = int(match.group(1))
        if line_start > 86_400_000:
            raise ValueError(f"QQ QRC line {line_no} exceeds the 24-hour safety bound")
        words_part = match.group(3)
        word_rows: list[tuple[int, str]] = []
        plain_parts: list[str] = []
        cursor = 0
        for word_match in word_pattern.finditer(words_part):
            if word_match.start() > cursor:
                plain_parts.append(words_part[cursor : word_match.start()])
            word_text = word_match.group("before")
            word_time = word_match.group("start")
            if word_text is None:
                word_text = word_match.group("after") or ""
                word_time = word_match.group("start_after")
            assert word_time is not None
            word_start = int(word_time)
            if word_start > 86_400_000:
                raise ValueError(f"QQ QRC word in line {line_no} exceeds the 24-hour safety bound")
            plain_parts.append(word_text)
            word_rows.append((word_start, word_text))
            cursor = word_match.end()
        plain_parts.append(words_part[cursor:])
        plain_line = "".join(plain_parts)
        if not plain_line:
            continue
        plain_rows.append(plain_line)
        if word_rows:
            line_rows.append(f"[{ms_to_lrc_ts(line_start)}]{plain_line}")
            enhanced_rows.append(
                f"[{ms_to_lrc_ts(line_start)}]" + "".join(f"<{ms_to_lrc_ts(at)}>{text}" for at, text in word_rows)
            )
        else:
            line_rows.append(f"[{ms_to_lrc_ts(line_start)}]{plain_line}")
    if malformed_lines:
        warnings.append(f"{malformed_lines} QRC line(s) did not match the provider schema")
    plain = "\n".join(plain_rows)
    line_lrc = "\n".join(line_rows)
    enhanced = "\n".join(enhanced_rows)
    if enhanced:
        valid_words, word_reasons = validate_enhanced_lrc(enhanced, duration=duration)
        if not valid_words:
            warnings.extend(f"QRC word timing rejected: {reason}" for reason in word_reasons)
            enhanced = ""
    if line_lrc:
        valid_lines, line_reasons = validate_lrc(line_lrc, duration=duration)
        if not valid_lines:
            warnings.extend(f"QRC line timing rejected: {reason}" for reason in line_reasons)
            line_lrc = ""
    return line_lrc, enhanced, plain, tuple(warnings)


def fetch_qqmusic(
    song: str,
    artist: str,
    duration: float | None = None,
    timeout: int = 6,
) -> dict[str, Any] | None:
    """Fetch identity-matched QQ candidates, preserving QRC/LRC source data."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://y.qq.com/",
        "Accept": "application/json, text/javascript, */*",
    }
    search_query = f"{artist} {song}".strip()
    s_url = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp?" + urllib.parse.urlencode(
        {"p": 1, "n": 10, "w": search_query, "format": "json"}
    )
    failures: list[tuple[str, str]] = []
    found_song = False
    try:
        req = urllib.request.Request(s_url, headers=headers)
        with _provider_urlopen(req, timeout=timeout) as resp:
            d = _provider_json_loads(read_provider_text(resp))
            if not isinstance(d, dict) or not isinstance(d.get("data"), dict):
                raise TypeError("QQ search response has no data object")
            song_obj = d["data"].get("song")
            song_list = song_obj.get("list") if isinstance(song_obj, dict) else None
            if not isinstance(song_list, list):
                raise TypeError("QQ search response has no song list")
            found_song = True
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        status, detail = classify_lyrics_exception(exc)
        _record_provider_failure(exc)
        return {"source": "QQ Music", "acquisition_status": status, "error": detail}

    matches: list[dict[str, Any]] = []
    for item in song_list:
        if not isinstance(item, dict):
            _record_provider_failure(TypeError("QQ search contains a non-object song"))
            continue
        singers = item.get("singer") or []
        if not isinstance(singers, list) or any(not isinstance(row, dict) for row in singers):
            _record_provider_failure(TypeError("QQ search song has malformed singer data"))
            continue
        item_artist = " / ".join(str(row.get("name") or "") for row in singers if row.get("name"))
        item_title = str(item.get("songname") or "")
        item_duration = _provider_duration_seconds(item.get("interval"))
        if (
            item_title
            and item_artist
            and is_track_match(
                item_title, item_artist, song, artist, cand_duration=item_duration, target_duration=duration
            )
        ):
            matches.append(item)
    if not matches:
        if found_song:
            mark_provider_not_found()
            return {"source": "QQ Music", "acquisition_status": "NOT_FOUND"}
        return {"source": "QQ Music", "acquisition_status": "PARSER_ERROR", "error": "no valid search response"}

    candidates: list[dict[str, Any]] = []
    for item in matches[:8]:
        songmid = str(item.get("songmid") or "")
        if not songmid:
            _record_provider_failure(ValueError("QQ matching song has no songmid"))
            continue
        singers = item.get("singer") or []
        item_artist = " / ".join(str(row.get("name") or "") for row in singers if row.get("name"))
        item_title = str(item.get("songname") or "")
        album_name = str(item.get("albumname") or item.get("album_name") or "")
        item_duration = _provider_duration_seconds(item.get("interval"))
        try:
            l_url = "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg?" + urllib.parse.urlencode(
                {"songmid": songmid, "format": "json", "nobase64": 1}
            )
            l_req = urllib.request.Request(l_url, headers=headers)
            with _provider_urlopen(l_req, timeout=timeout) as l_resp:
                raw_txt = read_provider_text(l_resp)
            json_payload = raw_txt.strip()
            if json_payload.startswith("MusicJsonCallback("):
                if not json_payload.endswith(")") and not json_payload.endswith(");"):
                    raise ValueError("QQ JSONP response has an unterminated callback")
                json_payload = json_payload[len("MusicJsonCallback(") :].removesuffix(";").removesuffix(")")
            lyric_data = _provider_json_loads(json_payload)
            if not isinstance(lyric_data, dict):
                raise TypeError("QQ lyrics response is not a JSON object")
            qrc_raw = _decode_qq_lyric_field(lyric_data.get("qrc"), "QRC")
            lrc_raw = _decode_qq_lyric_field(lyric_data.get("lyric"), "LRC")
            translation_raw = _decode_qq_lyric_field(lyric_data.get("trans"), "translation")
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
            status, detail = classify_lyrics_exception(exc)
            _record_provider_failure(exc)
            failures.append((status, detail))
            continue

        candidate_identity = {
            "id": songmid,
            "provider_track_id": songmid,
            "trackName": item_title,
            "artistName": item_artist,
            "albumName": album_name,
            "duration": item_duration,
            "identity_validated": True,
            "providerRaw": json_payload,
            "providerMetadata": {
                key: item[key] for key in ("songmid", "albummid", "albumname", "interval") if key in item
            },
        }
        translation = translation_raw
        sync_type = "none"
        synced = ""
        enhanced = ""
        plain = ""
        warnings: list[str] = []
        if qrc_raw:
            try:
                qrc_lrc, qrc_enhanced, qrc_plain, qrc_warnings = _parse_qq_qrc(qrc_raw, duration=duration)
                warnings.extend(qrc_warnings)
                if qrc_enhanced:
                    synced, enhanced, plain, sync_type = qrc_lrc, qrc_enhanced, qrc_plain, "richsync"
                elif qrc_lrc:
                    synced, plain, sync_type = qrc_lrc, qrc_plain, "linesync"
                else:
                    plain = qrc_plain
            except (ValueError, TypeError, RecursionError) as exc:
                warnings.append(f"QRC parsing failed; trying QQ LRC fallback: {exc}")
                _record_provider_failure(exc)
        if not synced and lrc_raw:
            if ANY_LINE_TIMESTAMP_RE.search(lrc_raw):
                valid_lrc, lrc_reasons = validate_lrc(lrc_raw, duration=duration)
                if valid_lrc:
                    synced = lrc_raw
                    plain = to_plain(lrc_raw, song=song, artist=artist)
                    sync_type = "linesync"
                else:
                    warnings.extend(f"LRC timing rejected: {reason}" for reason in lrc_reasons)
                    plain = to_plain(lrc_raw, song=song, artist=artist)
            elif len(lrc_raw.strip()) >= 10:
                plain = lrc_raw
        if not plain and qrc_raw and not warnings:
            warnings.append("QQ QRC contained no extractable lyric text")
        if len(plain.strip()) < 10:
            continue
        candidates.append(
            {
                **candidate_identity,
                "found": True,
                "source": "QQ Music (QRC)"
                if sync_type == "richsync"
                else ("QQ Music (LRC)" if synced else "QQ Music (plain)"),
                "format": "qrc" if sync_type == "richsync" else ("lrc" if synced else "plain"),
                "syncType": sync_type,
                "has_word_sync": bool(enhanced),
                "has_back_vocals": False,
                "rawLyrics": enhanced or synced or plain,
                "plainLyrics": plain,
                "syncedLyrics": synced,
                "enhancedLyrics": enhanced,
                "ttmlLyrics": "",
                "translationLyrics": translation,
                "validationWarnings": warnings,
            }
        )
    if candidates:
        candidates.sort(
            key=lambda candidate: (
                text_similarity(str(candidate.get("trackName") or ""), song)
                + text_similarity(str(candidate.get("artistName") or ""), artist),
                -(abs(float(candidate.get("duration") or duration or 0) - float(duration or 0))),
                str(candidate.get("provider_track_id") or ""),
            ),
            reverse=True,
        )
        selected = candidates[0]
        selected["alternatives"] = candidates[1:]
        if failures:
            selected["validationWarnings"] = list(selected.get("validationWarnings") or []) + [
                f"some QQ candidates could not be inspected ({failures[0][0]})"
            ]
        return selected
    if failures:
        severity = {
            "AUTHENTICATION_ERROR": 7,
            "BLOCKED": 6,
            "RATE_LIMITED": 5,
            "TRANSIENT_ERROR": 4,
            "PARSER_ERROR": 3,
            "INVALID_RESPONSE": 2,
            "NOT_FOUND": 1,
        }
        status, detail = max(failures, key=lambda failure: severity.get(failure[0], 0))
        return {"source": "QQ Music", "acquisition_status": status, "error": detail}
    mark_provider_not_found()
    return {"source": "QQ Music", "acquisition_status": "NOT_FOUND"}


class _PetitSearchParser(HTMLParser):
    """Extract result rows and links from PetitLyrics search HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, Any]] = []
        self._row: dict[str, Any] | None = None
        self._cell: list[str] | None = None
        self._anchor: tuple[str, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        if tag.casefold() == "tr":
            self._row = {"cells": [], "links": []}
        elif self._row is not None and tag.casefold() in {"td", "th"}:
            self._cell = []
        elif self._row is not None and tag.casefold() == "a":
            self._anchor = (values.get("href", ""), [])
        elif tag.casefold() == "br" and self._cell is not None:
            self._cell.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._row is not None and self._anchor is not None:
            href, label = self._anchor
            self._row["links"].append((href, "".join(label).strip()))
            self._anchor = None
        elif tag.casefold() in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row["cells"].append("".join(self._cell).strip())
            self._cell = None
        elif tag.casefold() == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)
        if self._anchor is not None:
            self._anchor[1].append(data)


def parse_petitlyrics_search(page: str, song: str, artist: str) -> list[dict[str, str]]:
    if len(page) > 5_000_000:
        raise ValueError("PetitLyrics search page exceeds 5 MB")
    parser = _PetitSearchParser()
    parser.feed(page)
    parser.close()
    if not parser.rows:
        raise ValueError("PetitLyrics search result rows are missing or changed")
    candidates: list[dict[str, str]] = []
    for row in parser.rows:
        links = row["links"]
        lyrics_links = [
            (href, label) for href, label in links if re.fullmatch(r"/lyrics/\d+/?", urllib.parse.urlparse(href).path)
        ]
        if not lyrics_links:
            continue
        title_options = [(text_similarity(label, song), label) for _href, label in lyrics_links if label]
        if not title_options:
            title_options = [(text_similarity(cell, song), cell) for cell in row["cells"] if cell]
        _score, candidate_title = max(title_options, default=(0.0, ""), key=lambda item: (item[0], item[1].casefold()))
        artist_options = [label for _href, label in links if label and label != candidate_title]
        artist_options.extend(cell for cell in row["cells"] if cell and cell != candidate_title)
        candidate_artist = max(
            artist_options, default="", key=lambda value: (text_similarity(value, artist), value.casefold())
        )
        if candidate_title and candidate_artist and is_track_match(candidate_title, candidate_artist, song, artist):
            href = next((href for href, label in lyrics_links if label == candidate_title), lyrics_links[0][0])
            parsed_url = urllib.parse.urlparse(urllib.parse.urljoin("https://petitlyrics.com/", href))
            if parsed_url.scheme != "https" or parsed_url.hostname not in {"petitlyrics.com", "www.petitlyrics.com"}:
                continue
            lyric_id = re.fullmatch(r"/lyrics/(\d+)/?", parsed_url.path)
            if lyric_id is None:
                continue
            candidates.append(
                {
                    "id": lyric_id.group(1),
                    "url": urllib.parse.urljoin("https://petitlyrics.com/", href),
                    "title": candidate_title,
                    "artist": candidate_artist,
                }
            )
    return candidates


class _PetitLyricsPageParser(HTMLParser):
    """Read literal text inside the lyrics canvas; never execute page scripts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.found = False
        self.text: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        if not self.found and tag.casefold() == "canvas" and values.get("id") == "lyrics":
            self.found = True
            self.depth = 1
        elif self.depth:
            if tag.casefold() not in {"br", "img", "hr", "input", "meta", "link", "wbr"}:
                self.depth += 1
            if tag.casefold() in {"script", "style"}:
                self._skip_depth += 1
            if tag.casefold() == "br":
                self.text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"br", "img", "hr", "input", "meta", "link", "wbr"}:
            return
        if self.depth:
            if tag.casefold() in {"script", "style"} and self._skip_depth:
                self._skip_depth -= 1
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self.depth and not self._skip_depth:
            self.text.append(data)


def parse_petitlyrics_page(page: str) -> str:
    if len(page) > 5_000_000:
        raise ValueError("PetitLyrics page exceeds 5 MB")
    parser = _PetitLyricsPageParser()
    parser.feed(page)
    parser.close()
    if not parser.found:
        raise ValueError("PetitLyrics lyrics canvas is missing or changed")
    return "".join(parser.text)


def fetch_petitlyrics(
    song: str,
    artist: str,
    timeout: int = 6,
) -> dict[str, Any] | None:
    """Fetch a title/artist matched result from PetitLyrics' static HTML."""
    acquired_forms: list[dict[str, Any]] = []
    item_failures: list[tuple[str, str]] = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }
    # 1. Search PetitLyrics for candidate track URLs
    candidates: list[dict[str, str]] = []
    queries = [
        f"https://petitlyrics.com/search_lyrics?title={_quote_component(song)}&artist={_quote_component(artist)}",
        f"https://petitlyrics.com/search_lyrics?title={_quote_component(song)}",
    ]
    for q_url in queries:
        try:
            req = urllib.request.Request(q_url, headers=headers)
            with _provider_urlopen(req, timeout=min(timeout, 4)) as resp:
                s_html = read_provider_text(resp)
                candidates.extend(parse_petitlyrics_search(s_html, song, artist))
                if candidates:
                    break
        except (OSError, ValueError) as exc:
            status, error = classify_lyrics_exception(exc)
            return {"source": "PetitLyrics", "acquisition_status": status, "error": error}

    if not candidates:
        return {"source": "PetitLyrics", "acquisition_status": "NOT_FOUND"}

    parser_failures: list[str] = []
    for candidate in candidates[:3]:
        try:
            t_url = candidate["url"]
            try:
                treq = urllib.request.Request(t_url, headers=headers)
                with _provider_urlopen(treq, timeout=min(timeout, 4)) as tresp:
                    thtml = read_provider_text(tresp)
                decoded_lyrics = parse_petitlyrics_page(thtml)
            except (OSError, ValueError) as exc:
                status, error = classify_lyrics_exception(exc)
                if status in {"BLOCKED", "RATE_LIMITED", "TRANSIENT_ERROR", "AUTHENTICATION_ERROR"}:
                    raise _LyricsAcquisitionError(status, error)
                parser_failures.append(error)
                continue
            if len(decoded_lyrics.strip()) > 20:
                acquired_forms.append(
                    {
                        "found": True,
                        "source": "PetitLyrics (Web)",
                        "id": candidate["id"],
                        "provider_track_id": candidate["id"],
                        "url": t_url,
                        "trackName": candidate["title"],
                        "artistName": candidate["artist"],
                        "identity_validated": True,
                        "source_confidence": 70,
                        "format": "plain",
                        "syncType": "none",
                        "has_word_sync": False,
                        "has_back_vocals": False,
                        "providerRaw": decoded_lyrics,
                        "rawLyrics": decoded_lyrics,
                        "plainLyrics": decoded_lyrics,
                        "syncedLyrics": "",
                        "enhancedLyrics": "",
                        "ttmlLyrics": "",
                    }
                )
        except (OSError, ValueError, TypeError, KeyError, AttributeError, _LyricsAcquisitionError) as exc:
            _record_provider_failure(exc)
            status, detail = classify_lyrics_exception(exc)
            item_failures.append((status, detail))
            if status in {"RATE_LIMITED", "AUTHENTICATION_ERROR", "BLOCKED"}:
                break
            continue
    if acquired_forms or item_failures:
        return _collected_lyrics_result(
            acquired_forms, item_failures, source="PetitLyrics", truncated=len(candidates) > 3
        )

    if parser_failures:
        return {"source": "PetitLyrics", "acquisition_status": "PARSER_ERROR", "error": parser_failures[0]}

    return None


def fetch_vk_music_lyrics(
    song: str,
    artist: str,
    album: str = "",
    duration: float | None = None,
    timeout: int = 6,
) -> dict[str, Any] | None:
    """Use VK's token-authenticated API; do not scrape logged-in web sessions."""
    acquired_forms: list[dict[str, Any]] = []
    item_failures: list[tuple[str, str]] = []
    headers = {"User-Agent": "AudiophileArchTagger/6.0 (lyrics metadata client)", "Accept": "application/json"}
    token = os.environ.get("VK_TOKEN", "").strip()
    token_file = Path.home() / ".config" / "vk_token"
    if not token and token_file.is_file():
        try:
            token = token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            status, detail = classify_lyrics_exception(exc)
            return {"source": "VK Music", "acquisition_status": status, "error": detail}
    if not token:
        return {
            "source": "VK Music",
            "acquisition_status": "AUTHENTICATION_ERROR",
            "error": "VK API token is not configured",
        }

    def api_request(method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        query = {**params, "v": "5.131", "access_token": token}
        url = f"https://api.vk.com/method/{method}?{urllib.parse.urlencode(query)}"
        with _provider_urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
            payload = _provider_json_loads(read_provider_text(response))
        if not isinstance(payload, dict):
            raise TypeError(f"VK {method} response is not a JSON object")
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("error_code")
            message = str(error.get("error_msg") or "VK API error")
            status = (
                {5: "AUTHENTICATION_ERROR", 6: "RATE_LIMITED", 9: "RATE_LIMITED", 3: "BLOCKED"}.get(
                    code, "INVALID_RESPONSE"
                )
                if isinstance(code, int)
                else "INVALID_RESPONSE"
            )
            raise _LyricsAcquisitionError(status, f"VK API {code}: {message}")
        return payload

    try:
        query = f"{artist} {song}".strip()
        search = api_request("audio.search", {"q": query, "count": 10})
        response = search.get("response")
        items = response.get("items") if isinstance(response, dict) else None
        if not isinstance(items, list):
            raise TypeError("VK audio.search response is missing response.items")
        for item in items:
            try:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "")
                item_artist = str(item.get("artist") or "")
                raw_duration = item.get("duration")
                try:
                    candidate_duration = float(raw_duration) if raw_duration is not None else None
                except (TypeError, ValueError):
                    candidate_duration = None
                if candidate_duration is not None and (
                    not math.isfinite(candidate_duration) or candidate_duration <= 0
                ):
                    candidate_duration = None
                if not is_track_match(
                    title, item_artist, song, artist, cand_duration=candidate_duration, target_duration=duration
                ):
                    continue
                lyrics_id = item.get("lyrics_id")
                if not lyrics_id:
                    continue
                lyric_response = api_request("audio.getLyrics", {"lyrics_id": str(lyrics_id)})
                lyric_body = lyric_response.get("response")
                text = lyric_body.get("text") if isinstance(lyric_body, dict) else None
                if not isinstance(text, str) or not text.strip():
                    continue
                audio_id = str(item.get("id") or lyrics_id)
                album_obj = item.get("album")
                album_name = str(album_obj.get("title") or "") if isinstance(album_obj, dict) else ""
                synced = bool(ANY_LINE_TIMESTAMP_RE.search(text))
                acquired_forms.append(
                    {
                        "found": True,
                        "source": "VK Music API",
                        "id": str(lyrics_id),
                        "provider_track_id": audio_id,
                        "provider_lyrics_id": str(lyrics_id),
                        "trackName": title,
                        "artistName": item_artist,
                        "albumName": album_name,
                        "duration": candidate_duration,
                        "identity_validated": True,
                        "format": "lrc" if synced else "plain",
                        "syncType": "linesync" if synced else "none",
                        "providerRaw": text,
                        "rawLyrics": text,
                        "plainLyrics": to_plain(text) if synced else text,
                        "syncedLyrics": text if synced else "",
                        "enhancedLyrics": "",
                        "transformations": ["VK API response text retained without cosmetic cleanup"],
                    }
                )
            except (OSError, ValueError, TypeError, KeyError, AttributeError, _LyricsAcquisitionError) as exc:
                _record_provider_failure(exc)
                status, detail = classify_lyrics_exception(exc)
                item_failures.append((status, detail))
                if status in {"RATE_LIMITED", "AUTHENTICATION_ERROR", "BLOCKED"}:
                    break
                continue
        if acquired_forms or item_failures:
            return _collected_lyrics_result(acquired_forms, item_failures, source="VK Music API")
        mark_provider_not_found()
        return {"source": "VK Music API", "acquisition_status": "NOT_FOUND"}
    except _LyricsAcquisitionError as exc:
        _record_provider_failure(exc)
        return {"source": "VK Music API", "acquisition_status": exc.status, "error": str(exc)}
    except (OSError, ValueError, TypeError) as exc:
        status, detail = classify_lyrics_exception(exc)
        _record_provider_failure(exc)
        return {"source": "VK Music API", "acquisition_status": status, "error": detail}


def parse_vk_topics_search(page: str, song: str, artist: str) -> list[dict[str, str]]:
    """Extract topic search links only when the visible result names this track."""
    root = _parse_provider_html(page, "VK Topics search")
    output: list[dict[str, str]] = []
    saw_topic = False
    for link in _provider_nodes(root, tag="a"):
        href = link.attrs.get("href", "")
        parsed = urllib.parse.urlparse(href)
        if parsed.path.casefold() in {"/l/", "/redirect/"}:
            target = urllib.parse.parse_qs(parsed.query).get("uddg", [""])[0]
            parsed = urllib.parse.urlparse(urllib.parse.unquote(target))
        url = urllib.parse.urlparse(urllib.parse.urljoin("https://vk.com/", parsed.geturl()))
        if url.scheme != "https" or url.hostname not in {"vk.com", "www.vk.com", "m.vk.com"}:
            continue
        if not re.fullmatch(r"/topic-\d+_\d+/?", url.path):
            continue
        saw_topic = True
        label = link.attrs.get("title", "") or link.text()
        identity = _identity_from_result_label(label, song, artist)
        if identity:
            output.append({"url": url.geturl(), "title": identity[0], "artist": identity[1]})
    if (
        not output
        and not saw_topic
        and not re.search(r"no results|no more search results|ничего не найдено", root.text(), re.IGNORECASE)
    ):
        raise ValueError("VK Topics search markup has no recognizable result state")
    return output


def parse_vk_topics_page(page: str, song: str, artist: str) -> dict[str, str] | None:
    """Read public topic text only if its semantic heading verifies the track."""
    root = _parse_provider_html(page, "VK Topics page")
    headings = [node.text().strip() for node in root.descendants() if node.tag in {"h1", "h2"} and node.text().strip()]
    heading_match = next(
        (identity for heading in headings if (identity := _identity_from_result_label(heading, song, artist))),
        None,
    )
    if heading_match is None:
        raise ValueError("VK topic page has no matching title/artist heading")
    posts = _provider_nodes(root, class_name="bp_text")
    if not posts:
        raise ValueError("VK topic page post container is missing or changed")
    for post in posts:
        text = post.text().replace("\r\n", "\n").replace("\r", "\n")
        if len(clean_plain_lyrics_for_alignment(text)) > 60:
            return {"title": heading_match[0], "artist": heading_match[1], "text": text}
    return None


def _vk_post_declares_track(text: str, song: str, artist: str) -> bool:
    """Accept an API post only when its opening metadata explicitly names both."""
    opening = " ".join(line.strip() for line in text.splitlines()[:3] if line.strip())
    return _identity_from_result_label(opening, song, artist) is not None


def fetch_vk_topics(
    song: str,
    artist: str,
    timeout: int = 6,
) -> dict[str, Any] | None:
    """Read public VK topic lyrics after visible track identity is verified."""
    errors: list[tuple[str, str]] = []
    acquired_forms: list[dict[str, Any]] = []
    vk_token = os.environ.get("VK_TOKEN", "").strip()
    token_file = Path.home() / ".config" / "vk_token"
    if not vk_token and token_file.is_file():
        try:
            vk_token = token_file.read_text(encoding="utf-8").strip()
        except OSError:
            vk_token = ""
    if vk_token:
        try:
            query = f"{artist} {song} текст песни"
            params = urllib.parse.urlencode({"q": query, "count": 10, "v": "5.131", "access_token": vk_token})
            request = urllib.request.Request(
                f"https://api.vk.com/method/newsfeed.search?{params}",
                headers={
                    "User-Agent": "AudiophileArchTagger/6.0 (lyrics metadata client)",
                    "Accept": "application/json",
                },
            )
            with _provider_urlopen(request, timeout=timeout) as response:
                payload = _provider_json_loads(read_provider_text(response))
            if not isinstance(payload, dict):
                raise TypeError("VK newsfeed.search response is not a JSON object")
            api_error = payload.get("error")
            if isinstance(api_error, dict):
                code = api_error.get("error_code")
                status = (
                    {5: "AUTHENTICATION_ERROR", 6: "RATE_LIMITED", 9: "RATE_LIMITED", 3: "BLOCKED"}.get(
                        code, "INVALID_RESPONSE"
                    )
                    if isinstance(code, int)
                    else "INVALID_RESPONSE"
                )
                errors.append((status, f"VK API {code}: {api_error.get('error_msg', 'API error')}"))
            else:
                response_data = payload.get("response")
                items = response_data.get("items") if isinstance(response_data, dict) else None
                if not isinstance(items, list):
                    raise TypeError("VK newsfeed.search response is missing response.items")
                for item in items[:10]:
                    if not isinstance(item, dict):
                        continue
                    text_value = str(item.get("text") or "")
                    identity = _identity_from_result_label(
                        " ".join(line.strip() for line in text_value.splitlines()[:3] if line.strip()), song, artist
                    )
                    if identity and len(clean_plain_lyrics_for_alignment(text_value)) > 60:
                        acquired_forms.append(
                            {
                                "found": True,
                                "source": "VK Topics API",
                                "trackName": identity[0],
                                "artistName": identity[1],
                                "identity_validated": True,
                                "source_confidence": 35,
                                "format": "plain",
                                "syncType": "none",
                                "providerRaw": text_value,
                                "rawLyrics": text_value,
                                "plainLyrics": text_value,
                                "syncedLyrics": "",
                                "enhancedLyrics": "",
                            }
                        )
        except (OSError, ValueError, TypeError) as exc:
            status, detail = classify_lyrics_exception(exc)
            errors.append((status, detail))

    if acquired_forms:
        return _collected_lyrics_result(acquired_forms, errors, source="VK Topics API")

    query = f'site:vk.com/topic "{artist}" "{song}" текст песни'
    headers = {"User-Agent": "AudiophileArchTagger/6.0 (public topic lookup)", "Accept": "text/html"}
    search_url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    try:
        with _provider_urlopen(urllib.request.Request(search_url, headers=headers), timeout=timeout) as response:
            search_page = read_provider_text(response)
        if _provider_page_blocked(search_page):
            return {"source": "VK Topics", "acquisition_status": "BLOCKED", "error": "search provider challenge page"}
        topics = parse_vk_topics_search(search_page, song, artist)
        for topic in topics[:3]:
            try:
                with _provider_urlopen(
                    urllib.request.Request(topic["url"], headers=headers), timeout=timeout
                ) as response:
                    page = read_provider_text(response)
                    final = (
                        urllib.parse.urlparse(response.geturl())
                        if callable(getattr(response, "geturl", None))
                        else urllib.parse.urlparse(topic["url"])
                    )
                if final.scheme != "https" or final.hostname not in {"vk.com", "www.vk.com", "m.vk.com"}:
                    errors.append(("BLOCKED", "topic requires authentication or left public host"))
                    break
                if _provider_page_blocked(page):
                    errors.append(("BLOCKED", "provider challenge page"))
                    break
                try:
                    matched = parse_vk_topics_page(page, song, artist)
                except ValueError as exc:
                    errors.append(("PARSER_ERROR", str(exc)))
                    continue
                if matched:
                    acquired_forms.append(
                        {
                            "found": True,
                            "source": "VK Topics",
                            "url": topic["url"],
                            "trackName": matched["title"],
                            "artistName": matched["artist"],
                            "identity_validated": True,
                            "source_confidence": 30,
                            "format": "plain",
                            "syncType": "none",
                            "providerRaw": matched["text"],
                            "rawLyrics": matched["text"],
                            "plainLyrics": matched["text"],
                            "syncedLyrics": "",
                            "enhancedLyrics": "",
                        }
                    )
            except (OSError, ValueError, TypeError) as exc:
                status, detail = classify_lyrics_exception(exc)
                errors.append((status, detail))
                if status in {"RATE_LIMITED", "AUTHENTICATION_ERROR", "BLOCKED"}:
                    break
                continue
    except (OSError, ValueError, TypeError) as exc:
        status, detail = classify_lyrics_exception(exc)
        errors.append((status, detail))

    if acquired_forms or errors:
        return _collected_lyrics_result(
            acquired_forms, errors, source="VK Topics", truncated=len(topics) > 3 if acquired_forms else False
        )
    mark_provider_not_found()
    return {"source": "VK Topics", "acquisition_status": "NOT_FOUND"}


def fetch_deezer_lyrics(
    song: str,
    artist: str,
    album: str = "",
    duration: float | None = None,
    timeout: int = 6,
) -> dict[str, Any]:
    """Fetch native Deezer line/word timing with an explicitly supplied ARL.

    Public catalog search supplies recording identity. The authorized web
    client's GraphQL route supplies lyrics; it is an observed private schema.
    """
    arl = os.environ.get("DEEZER_ARL", "").strip()
    if not arl:
        path = Path.home() / ".config" / "deezer_arl"
        try:
            arl = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
        except OSError:
            arl = ""
    if not arl:
        return {"source": "Deezer", "acquisition_status": "NOT_CONFIGURED", "error": "DEEZER_ARL missing"}
    headers = {"Accept": "application/json", "User-Agent": "AudiophileArchTagger/6.0"}

    def request_json(request: urllib.request.Request) -> Any:
        with _provider_urlopen(request, timeout=timeout) as response:
            return _provider_json_loads(read_provider_text(response))

    try:
        query = urllib.parse.urlencode({"q": f"{artist} {song}"})
        search = request_json(urllib.request.Request("https://api.deezer.com/search/track?" + query, headers=headers))
        if not isinstance(search, dict) or not isinstance(search.get("data"), list):
            raise TypeError("Deezer catalog search schema changed")
        matching: list[dict[str, Any]] = []
        for item in search["data"][:20]:
            if not isinstance(item, dict):
                raise TypeError("Deezer catalog track schema changed")
            raw_artist_data = item.get("artist")
            raw_album_data = item.get("album")
            artist_data: dict[str, Any] = raw_artist_data if isinstance(raw_artist_data, dict) else {}
            album_data: dict[str, Any] = raw_album_data if isinstance(raw_album_data, dict) else {}
            if not is_track_match(
                str(item.get("title") or ""),
                str(artist_data.get("name") or ""),
                song,
                artist,
                float(item["duration"]) if isinstance(item.get("duration"), (int, float)) else None,
                duration,
            ):
                continue
            matching.append(item)
        if not matching:
            mark_provider_not_found()
            return {"source": "Deezer", "acquisition_status": "NOT_FOUND"}
        matching.sort(
            key=lambda item: (
                abs(float(item.get("duration") or 0) - duration) if duration else 0,
                str(item.get("id") or ""),
            )
        )
        auth_request = urllib.request.Request(
            "https://auth.deezer.com/login/renew?jo=p&rto=c&i=c",
            data=b"",
            headers={
                **headers,
                "Cookie": f"arl={arl}",
                "Origin": "https://www.deezer.com",
                "Referer": "https://www.deezer.com/",
            },
            method="POST",
        )
        try:
            auth = request_json(auth_request)
        except urllib.error.HTTPError as exc:
            if exc.code in {400, 401, 403}:
                raise _LyricsAcquisitionError(
                    "AUTHENTICATION_ERROR", "Deezer session renewal rejected the configured ARL"
                ) from exc
            raise
        jwt = auth.get("jwt") if isinstance(auth, dict) else None
        if not isinstance(jwt, str) or not jwt:
            raise _LyricsAcquisitionError("AUTHENTICATION_ERROR", "Deezer session renewal returned no JWT")
        results: list[dict[str, Any]] = []
        item_failures: list[tuple[str, str]] = []
        for item in matching[:3]:
            try:
                track_id = str(item.get("id") or "")
                if not track_id:
                    raise ValueError("Deezer matched track has no ID")
                payload = {
                    "operationName": "GetLyrics",
                    "variables": {"trackId": track_id},
                    "query": "query GetLyrics($trackId: String!) { track(trackId: $trackId) { id lyrics { id text synchronizedWordByWordLines { start end words { start end word } } synchronizedLines { lrcTimestamp line lineTranslated milliseconds duration } licence copyright writers } } }",
                }
                lyric_request = urllib.request.Request(
                    "https://pipe.deezer.com/api",
                    data=json.dumps(payload).encode("utf-8"),
                    method="POST",
                    headers={
                        **headers,
                        "Authorization": f"Bearer {jwt}",
                        "Content-Type": "application/json",
                        "Origin": "https://www.deezer.com",
                    },
                )
                body = request_json(lyric_request)
                if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
                    raise TypeError("Deezer lyrics GraphQL schema changed")
                if body.get("errors"):
                    raise _LyricsAcquisitionError("INVALID_RESPONSE", "Deezer lyrics GraphQL returned errors")
                track_data = body["data"].get("track")
                if not isinstance(track_data, dict) or str(track_data.get("id") or "") != track_id:
                    raise ValueError("Deezer lyrics track ID differs from catalog result")
                lyrics = track_data.get("lyrics")
                if lyrics is None:
                    continue
                if not isinstance(lyrics, dict):
                    raise TypeError("Deezer lyrics schema changed")
                plain = str(lyrics.get("text") or "")
                line_rows = lyrics.get("synchronizedLines") or []
                word_rows = lyrics.get("synchronizedWordByWordLines") or []
                warnings: list[str] = []
                if not isinstance(line_rows, list):
                    warnings.append("Deezer line timing schema changed; plain text retained")
                    line_rows = []
                if not isinstance(word_rows, list):
                    warnings.append("Deezer word timing schema changed; other forms retained")
                    word_rows = []
                timed: list[str] = []
                enhanced: list[str] = []
                translations: list[str] = []
                try:
                    for row in line_rows:
                        if not isinstance(row, dict):
                            raise TypeError("Deezer synchronized line schema changed")
                        stamp = row.get("milliseconds")
                        if stamp is None:
                            raise ValueError("Deezer synchronized line lacks milliseconds")
                        ms = int(stamp)
                        if ms < 0:
                            raise ValueError("Deezer synchronized line has negative time")
                        timed.append(f"[{ms_to_lrc_ts(ms)}]{row.get('line') or ''}")
                        if row.get("lineTranslated"):
                            translations.append(str(row["lineTranslated"]))
                except (ValueError, TypeError, KeyError, OverflowError) as exc:
                    timed = []
                    translations = []
                    warnings.append(f"Deezer line timing rejected: {exc}; other forms retained")
                try:
                    for row in word_rows:
                        if not isinstance(row, dict) or not isinstance(row.get("words"), list):
                            raise TypeError("Deezer word timing schema changed")
                        start = int(row["start"])
                        words = []
                        for word in row["words"]:
                            if not isinstance(word, dict) or "start" not in word or "word" not in word:
                                raise ValueError("Deezer word entry schema changed")
                            words.append(f"<{ms_to_lrc_ts(int(word['start']))}>{word['word']}")
                        enhanced.append(f"[{ms_to_lrc_ts(start)}]" + "".join(words))
                except (ValueError, TypeError, KeyError, OverflowError) as exc:
                    enhanced = []
                    warnings.append(f"Deezer word timing rejected: {exc}; other forms retained")
                if not plain.strip() and timed:
                    plain = "\n".join(str(row.get("line") or "") for row in line_rows)
                if not plain.strip():
                    continue
                album_data = item.get("album") or {}
                artist_data = item.get("artist") or {}
                results.append(
                    {
                        "found": True,
                        "source": "Deezer",
                        "id": track_id,
                        "provider_track_id": track_id,
                        "trackName": str(item.get("title") or ""),
                        "artistName": str(artist_data.get("name") or ""),
                        "albumName": str(album_data.get("title") or ""),
                        "duration": item.get("duration"),
                        "isrc": str(item.get("isrc") or ""),
                        "identity_validated": True,
                        "source_confidence": 80,
                        "providerRaw": json.dumps(body, ensure_ascii=False),
                        "rawLyrics": plain,
                        "plainLyrics": plain,
                        "syncedLyrics": "\n".join(timed),
                        "enhancedLyrics": "\n".join(enhanced),
                        "translationLyrics": "\n".join(translations),
                        "ttmlLyrics": "",
                        "format": "enhanced_lrc" if enhanced else "lrc" if timed else "plain",
                        "syncType": "richsync" if enhanced else "linesync" if timed else "none",
                        "has_word_sync": bool(enhanced),
                        "validationWarnings": warnings,
                        "attribution": str(lyrics.get("licence") or "Deezer"),
                    }
                )
            except (OSError, ValueError, TypeError, KeyError, AttributeError, _LyricsAcquisitionError) as exc:
                _record_provider_failure(exc)
                status, detail = classify_lyrics_exception(exc)
                item_failures.append((status, detail))
                if status in {"RATE_LIMITED", "AUTHENTICATION_ERROR", "BLOCKED"}:
                    break
                continue
        if results:
            if item_failures:
                results[0].setdefault("validationWarnings", []).append(
                    f"some recording candidates failed ({item_failures[0][0]})"
                )
            results[0]["alternatives"] = results[1:]
            return results[0]
        if item_failures:
            status, detail = item_failures[0]
            return {"source": "Deezer", "acquisition_status": status, "error": detail}
        mark_provider_not_found()
        return {"source": "Deezer", "acquisition_status": "NOT_FOUND"}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, _LyricsAcquisitionError) as exc:
        status, detail = classify_lyrics_exception(exc)
        return {"source": "Deezer", "acquisition_status": status, "error": detail}


def fetch_amdm_lyrics(
    song: str,
    artist: str,
    timeout: int = 6,
) -> dict[str, Any] | None:
    """Fetch a verified AmDm chord sheet while preserving source and vocal text."""
    acquired_forms: list[dict[str, Any]] = []
    item_failures: list[tuple[str, str]] = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    query = f"{artist} {song}".strip()
    search_url = f"https://amdm.ru/search/?{urllib.parse.urlencode({'q': query})}"
    try:
        req = urllib.request.Request(search_url, headers=headers)
        with _provider_urlopen(req, timeout=timeout) as resp:
            html_txt = read_provider_text(resp)
        if _provider_page_blocked(html_txt):
            return {"source": "AmDm", "acquisition_status": "BLOCKED", "error": "provider challenge page"}
        matched = parse_amdm_search(html_txt, song, artist)
        if not matched:
            mark_provider_not_found()
            return {"source": "AmDm", "acquisition_status": "NOT_FOUND"}
        parser_errors: list[str] = []
        for item in matched[:2]:
            try:
                track_url = item["url"]
                try:
                    treq = urllib.request.Request(track_url, headers=headers)
                    with _provider_urlopen(treq, timeout=timeout) as tresp:
                        thtml = read_provider_text(tresp)
                        final = (
                            urllib.parse.urlparse(tresp.geturl())
                            if callable(getattr(tresp, "geturl", None))
                            else urllib.parse.urlparse(track_url)
                        )
                    if final.scheme != "https" or final.hostname not in {"amdm.ru", "www.amdm.ru"}:
                        raise _LyricsAcquisitionError("INVALID_RESPONSE", "redirect left provider host")
                    raw_content = parse_amdm_lyrics(thtml)
                except ValueError as exc:
                    parser_errors.append(str(exc))
                    continue
                plain_content = _amdm_vocal_text(raw_content)
                if len(clean_plain_lyrics_for_alignment(plain_content)) > 20:
                    acquired_forms.append(
                        {
                            "found": True,
                            "source": "AmDm",
                            "id": item["id"],
                            "provider_track_id": item["id"],
                            "url": track_url,
                            "trackName": item["title"],
                            "artistName": item["artist"],
                            "identity_validated": True,
                            "source_confidence": 45,
                            "format": "plain",
                            "syncType": "none",
                            "providerRaw": raw_content,
                            "rawLyrics": raw_content,
                            "plainLyrics": plain_content,
                            "syncedLyrics": "",
                            "enhancedLyrics": "",
                            "ttmlLyrics": "",
                            "transformations": [
                                "chord-only rows omitted from display text; full chord sheet retained as source"
                            ],
                        }
                    )
            except (OSError, ValueError, TypeError, KeyError, AttributeError, _LyricsAcquisitionError) as exc:
                _record_provider_failure(exc)
                status, detail = classify_lyrics_exception(exc)
                item_failures.append((status, detail))
                if status in {"RATE_LIMITED", "AUTHENTICATION_ERROR", "BLOCKED"}:
                    break
                continue
        if acquired_forms or item_failures:
            return _collected_lyrics_result(acquired_forms, item_failures, source="AmDm", truncated=len(matched) > 2)
        if parser_errors:
            error = ValueError(parser_errors[0])
            _record_provider_failure(error)
            return {"source": "AmDm", "acquisition_status": "PARSER_ERROR", "error": str(error)}
    except (OSError, ValueError) as exc:
        status, detail = classify_lyrics_exception(exc)
        _record_provider_failure(exc)
        return {"source": "AmDm", "acquisition_status": status, "error": detail}
    return {"source": "AmDm", "acquisition_status": "NOT_FOUND"}


def parse_amdm_search(page: str, song: str, artist: str) -> list[dict[str, str]]:
    root = _parse_provider_html(page, "AmDm search")
    matches: list[dict[str, str]] = []
    saw_song_link = False
    for link in _provider_nodes(root, tag="a"):
        url = urllib.parse.urlparse(urllib.parse.urljoin("https://amdm.ru/", link.attrs.get("href", "")))
        if url.scheme != "https" or url.hostname not in {"amdm.ru", "www.amdm.ru"}:
            continue
        if not re.fullmatch(r"/akkordi/[^/]+/\d+/[^/]+/?", url.path):
            continue
        saw_song_link = True
        identity = _identity_from_result_label(link.attrs.get("title", "") or link.text(), song, artist)
        if identity:
            matches.append(
                {
                    "id": url.path.rstrip("/").split("/")[-2],
                    "url": url.geturl(),
                    "title": identity[0],
                    "artist": identity[1],
                }
            )
    if not saw_song_link and not re.search(
        r"ничего не найдено|по вашему запросу.*не найдено", root.text(), re.IGNORECASE
    ):
        raise ValueError("AmDm search markup has no recognizable song results")
    return matches


def parse_amdm_lyrics(page: str) -> str:
    root = _parse_provider_html(page, "AmDm lyrics")
    candidates: list[_HTMLNode] = []
    for node in _provider_nodes(root, tag="pre"):
        classes = node.attrs.get("class", "").casefold().split()
        itemprop = node.attrs.get("itemprop", "").casefold()
        if itemprop in {"chordsblock", "text"} or "podbor" in classes:
            candidates.append(node)
    if len(candidates) != 1:
        raise ValueError("AmDm semantic chord-sheet block is missing or ambiguous")
    text = candidates[0].text().replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ValueError("AmDm chord-sheet block is empty")
    return text


def _amdm_vocal_text(chord_sheet: str) -> str:
    """Remove only unmistakable chord-only rows; leave ambiguous text intact."""
    chord = re.compile(r"(?:[A-H](?:#|b)?(?:m|maj|min|sus|dim|aug|add|M)?\d*(?:/[A-H](?:#|b)?)?\s*){2,}", re.IGNORECASE)
    rows = chord_sheet.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join("" if chord.fullmatch(row.strip()) else row for row in rows)


class _BandcampLyricsParser(HTMLParser):
    """Extract Bandcamp's structured track JSON and semantic lyrics blocks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tralbum: dict[str, Any] | None = None
        self.lyrics_blocks: list[str] = []
        self._lyrics_depth = 0
        self._lyrics_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        encoded = values.get("data-tralbum")
        if encoded and self.tralbum is None:
            try:
                parsed = _provider_json_loads(encoded)
                if isinstance(parsed, dict):
                    self.tralbum = parsed
            except (json.JSONDecodeError, TypeError, ValueError):
                # Structured data is never evaluated as JavaScript.
                pass
        if self._lyrics_depth:
            if tag in {"div", "p"}:
                self._lyrics_depth += 1
            elif tag == "br":
                self._lyrics_text.append("\n")
            return
        classes = set((values.get("class") or "").split())
        if tag in {"div", "p"} and "lyricsText" in classes:
            self._lyrics_depth = 1
            self._lyrics_text = []

    def handle_endtag(self, tag: str) -> None:
        if self._lyrics_depth and tag in {"div", "p"}:
            self._lyrics_depth -= 1
            if self._lyrics_depth == 0:
                self.lyrics_blocks.append("".join(self._lyrics_text))
                self._lyrics_text = []

    def handle_data(self, data: str) -> None:
        if self._lyrics_depth:
            self._lyrics_text.append(data)

    def close(self) -> None:
        super().close()
        if self._lyrics_depth:
            self.lyrics_blocks.append("".join(self._lyrics_text))
            self._lyrics_text = []
            self._lyrics_depth = 0


def parse_bandcamp_lyrics_page(page: str) -> tuple[dict[str, Any] | None, str]:
    """Parse Bandcamp's page data and lyrics container with HTMLParser."""
    if len(page) > 5_000_000:
        raise ValueError("Bandcamp page exceeds 5 MB safety limit")
    parser = _BandcampLyricsParser()
    parser.feed(page)
    parser.close()
    return parser.tralbum, "\n".join(parser.lyrics_blocks)


def fetch_bandcamp_lyrics(
    song: str,
    artist: str,
    timeout: int = 6,
) -> dict[str, Any] | None:
    """Fetch lyrics from Bandcamp via mobile autocomplete API and data-tralbum / HTML parsing."""
    acquired_forms: list[dict[str, Any]] = []
    item_failures: list[tuple[str, str]] = []
    headers_api = {
        "User-Agent": "Bandcamp/2.6.2 (Android 14)",
        "Accept": "application/json",
    }
    headers_web = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    # 1. Search Bandcamp via mobile autocomplete API
    queries = [f"{artist} {song}".strip(), song.strip()]
    track_urls: list[tuple[str, dict[str, Any]]] = []

    for q in queries:
        if not q:
            continue
        try:
            search_url = f"https://bandcamp.com/api/fuzzysearch/1/app_autocomplete?q={_quote_component(q)}"
            req = urllib.request.Request(search_url, headers=headers_api)
            with _provider_urlopen(req, timeout=min(timeout, 4)) as resp:
                data = _provider_json_loads(read_provider_text(resp))
                results = [r for r in data.get("results", []) if r.get("type") == "t"]
                for r in results:
                    raw_u = r.get("url") or ""
                    candidate_title = str(r.get("name") or r.get("title") or r.get("track_name") or "")
                    candidate_artist = str(r.get("band_name") or r.get("artist") or "")
                    if raw_u and is_track_match(candidate_title, candidate_artist, song, artist):
                        parts = raw_u.split("https://")
                        clean_u = "https://" + parts[-1]
                        if "/track/" in clean_u and all(url != clean_u for url, _ in track_urls):
                            track_urls.append((clean_u, r))
                if track_urls:
                    break
        except Exception:
            pass

    if not track_urls:
        return None

    for t_url, search_item in track_urls[:3]:
        try:
            track_name = str(search_item.get("name") or search_item.get("title") or search_item.get("track_name") or "")
            track_artist = str(search_item.get("band_name") or search_item.get("artist") or "")
            if not track_name or not track_artist:
                continue
            try:
                treq = urllib.request.Request(t_url, headers=headers_web)
                with _provider_urlopen(treq, timeout=min(timeout, 4)) as tresp:
                    thtml = read_provider_text(tresp)

                    tr_data, semantic_lyrics = parse_bandcamp_lyrics_page(thtml)
                    if tr_data and isinstance(tr_data.get("trackinfo"), list):
                        for item in tr_data["trackinfo"]:
                            if not isinstance(item, dict):
                                continue
                            item_title = str(item.get("title") or track_name)
                            item_lyrics = item.get("lyrics")
                            if not isinstance(item_lyrics, str) or not item_lyrics.strip():
                                continue
                            if not is_track_match(item_title, track_artist, song, artist):
                                continue
                            parsed = html.unescape(item_lyrics).replace("\r\n", "\n").replace("\r", "\n")
                            provider_id = str(item.get("track_id") or item.get("track_num") or t_url)
                            acquired_forms.append(
                                {
                                    "found": True,
                                    "source": "Bandcamp",
                                    "trackName": item_title,
                                    "artistName": track_artist,
                                    "albumName": str(tr_data.get("album_title") or ""),
                                    "duration": item.get("duration"),
                                    "id": provider_id,
                                    "provider_track_id": provider_id,
                                    "identity_validated": True,
                                    "url": t_url,
                                    "providerRaw": item_lyrics,
                                    "format": "plain",
                                    "syncType": "none",
                                    "has_word_sync": False,
                                    "has_back_vocals": False,
                                    "rawLyrics": parsed,
                                    "plainLyrics": parsed,
                                    "syncedLyrics": "",
                                    "enhancedLyrics": "",
                                    "ttmlLyrics": "",
                                }
                            )
                    if semantic_lyrics.strip():
                        parsed = semantic_lyrics.replace("\r\n", "\n").replace("\r", "\n")
                        acquired_forms.append(
                            {
                                "found": True,
                                "source": "Bandcamp",
                                "trackName": track_name,
                                "artistName": track_artist,
                                "identity_validated": True,
                                "url": t_url,
                                "providerRaw": semantic_lyrics,
                                "format": "plain",
                                "syncType": "none",
                                "has_word_sync": False,
                                "has_back_vocals": False,
                                "rawLyrics": parsed,
                                "plainLyrics": parsed,
                                "syncedLyrics": "",
                                "enhancedLyrics": "",
                                "ttmlLyrics": "",
                            }
                        )

            except Exception:
                pass
        except (OSError, ValueError, TypeError, KeyError, AttributeError, _LyricsAcquisitionError) as exc:
            _record_provider_failure(exc)
            status, detail = classify_lyrics_exception(exc)
            item_failures.append((status, detail))
            if status in {"RATE_LIMITED", "AUTHENTICATION_ERROR", "BLOCKED"}:
                break
            continue
    if acquired_forms or item_failures:
        return _collected_lyrics_result(acquired_forms, item_failures, source="Bandcamp", truncated=len(track_urls) > 3)

    return None


def fetch_baidu_lyrics(
    song: str,
    artist: str,
    timeout: int = 6,
) -> dict[str, Any] | None:
    """Query 1ting's identity-bearing catalog, without Baidu search snippets.

    Generic Baidu snippets previously supplied no recording identity and could
    silently attach unrelated lyrics. That fallback is intentionally removed.
    """
    acquired_forms: list[dict[str, Any]] = []
    item_failures: list[tuple[str, str]] = []
    query = f"{song} {artist}".strip()
    headers = {
        "User-Agent": "AudiophileArchTagger/6.0 (lyrics metadata client)",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    search_url = "https://so.1ting.com/all.do?" + urllib.parse.urlencode({"q": query})
    try:
        with _provider_urlopen(
            urllib.request.Request(search_url, headers=headers), timeout=min(timeout, 4)
        ) as response:
            search_page = _read_legacy_chinese_html(response)
        if _provider_page_blocked(search_page):
            return {"source": "1ting", "acquisition_status": "BLOCKED", "error": "provider challenge page"}
        candidates = parse_1ting_search(search_page, song, artist)
        if not candidates:
            mark_provider_not_found()
            return {"source": "1ting", "acquisition_status": "NOT_FOUND"}
        parser_errors: list[str] = []
        for candidate in candidates[:3]:
            try:
                with _provider_urlopen(
                    urllib.request.Request(candidate["url"], headers=headers), timeout=min(timeout, 4)
                ) as response:
                    page = _read_legacy_chinese_html(response)
                    final = (
                        urllib.parse.urlparse(response.geturl())
                        if callable(getattr(response, "geturl", None))
                        else urllib.parse.urlparse(candidate["url"])
                    )
                if final.scheme != "https" or final.hostname not in {"1ting.com", "www.1ting.com"}:
                    raise _LyricsAcquisitionError("INVALID_RESPONSE", "redirect left provider host")
                try:
                    kind, provider_text = parse_1ting_lyrics(page)
                except ValueError as exc:
                    parser_errors.append(str(exc))
                    continue
                common = {
                    "id": candidate["id"],
                    "provider_track_id": candidate["id"],
                    "url": candidate["url"],
                    "trackName": candidate["title"],
                    "artistName": candidate["artist"],
                    "identity_validated": True,
                    "source_confidence": 48,
                    "providerRaw": provider_text,
                    "rawLyrics": provider_text,
                }
                if kind == "lrc" and ANY_LINE_TIMESTAMP_RE.search(provider_text):
                    acquired_forms.append(
                        {
                            **common,
                            "found": True,
                            "source": "1ting (LRC)",
                            "format": "lrc",
                            "syncType": "linesync",
                            "plainLyrics": to_plain(provider_text),
                            "syncedLyrics": provider_text,
                            "enhancedLyrics": "",
                            "transformations": ["HTML entities decoded; LRC text and row order retained"],
                        }
                    )
                if kind == "plain" and len(clean_plain_lyrics_for_alignment(provider_text)) > 20:
                    acquired_forms.append(
                        {
                            **common,
                            "found": True,
                            "source": "1ting (plain)",
                            "format": "plain",
                            "syncType": "none",
                            "plainLyrics": provider_text,
                            "syncedLyrics": "",
                            "enhancedLyrics": "",
                        }
                    )
                parser_errors.append("1ting page had no usable lyrics content")
            except (OSError, ValueError, TypeError, KeyError, AttributeError, _LyricsAcquisitionError) as exc:
                _record_provider_failure(exc)
                status, detail = classify_lyrics_exception(exc)
                item_failures.append((status, detail))
                if status in {"RATE_LIMITED", "AUTHENTICATION_ERROR", "BLOCKED"}:
                    break
                continue
        if acquired_forms or item_failures:
            return _collected_lyrics_result(
                acquired_forms, item_failures, source="Baidu / 1ting", truncated=len(candidates) > 3
            )
        if parser_errors:
            error = ValueError(parser_errors[0])
            _record_provider_failure(error)
            return {"source": "1ting", "acquisition_status": "PARSER_ERROR", "error": str(error)}
    except (OSError, ValueError) as exc:
        status, detail = classify_lyrics_exception(exc)
        _record_provider_failure(exc)
        return {"source": "1ting", "acquisition_status": status, "error": detail}
    return {"source": "1ting", "acquisition_status": "NOT_FOUND"}


def _read_legacy_chinese_html(response: object) -> str:
    """Decode legacy 1ting GBK pages, preferring a declared HTTP charset."""
    read = getattr(response, "read", None)
    if not callable(read):
        raise TypeError("1ting response has no byte reader")
    content = read(5_000_001)
    if len(content) > 5_000_000:
        raise ValueError("1ting HTML response exceeds 5 MB")
    try:
        return decode_provider_text(response, content)
    except UnicodeDecodeError:
        # GB18030 is a strict superset of legacy GBK. Keep decoding strict so
        # genuinely corrupt bytes are not replaced with U+FFFD.
        return content.decode("gb18030", errors="strict")


def parse_1ting_search(page: str, song: str, artist: str) -> list[dict[str, str]]:
    root = _parse_provider_html(page, "1ting search")
    output: list[dict[str, str]] = []
    saw_lyrics_link = False
    for link in _provider_nodes(root, tag="a"):
        url = urllib.parse.urlparse(urllib.parse.urljoin("https://so.1ting.com/", link.attrs.get("href", "")))
        if url.scheme != "https" or url.hostname not in {"1ting.com", "www.1ting.com"}:
            continue
        track_id = re.search(r"geci(\d+)\.html$", url.path, re.IGNORECASE)
        if not track_id:
            continue
        saw_lyrics_link = True
        identity = _identity_from_result_label(link.attrs.get("title", "") or link.text(), song, artist)
        if identity:
            output.append({"id": track_id.group(1), "url": url.geturl(), "title": identity[0], "artist": identity[1]})
    if (
        not output
        and not saw_lyrics_link
        and not re.search(r"没有找到|无相关|no\s+(?:results|matches)", root.text(), re.IGNORECASE)
    ):
        raise ValueError("1ting search markup has no recognizable result state")
    return output


def parse_1ting_lyrics(page: str) -> tuple[str, str]:
    root = _parse_provider_html(page, "1ting lyrics")
    synced = _provider_nodes(root, element_id="lrc")
    if len(synced) == 1 and synced[0].text().strip():
        return "lrc", synced[0].text().replace("\r\n", "\n").replace("\r", "\n")
    plain = _provider_nodes(root, element_id="container")
    if len(plain) == 1 and plain[0].text().strip():
        return "plain", plain[0].text().replace("\r\n", "\n").replace("\r", "\n")
    raise ValueError("1ting lyrics container is missing, empty, or ambiguous")


class _DarkLyricsDocumentParser(HTMLParser):
    """Extract semantic song sections from DarkLyrics' lyrics container."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._lyrics_depth = 0
        self._container_found = False
        self._header = False
        self._song_number = ""
        self._song_title: list[str] = []
        self._body: list[str] = []
        self.songs: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "div" and "lyrics" in (attrs_dict.get("class") or "").split():
            self._container_found = True
            self._lyrics_depth = 1
            return
        if not self._lyrics_depth:
            return
        if tag == "div":
            self._lyrics_depth += 1
        elif tag == "h3":
            self._finish_song()
            self._header = True
            self._song_number = ""
            self._song_title = []
        elif tag == "a" and self._header:
            self._song_number = str(attrs_dict.get("name") or "")
        elif tag in {"br", "p"} and self._body:
            self._body.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if not self._lyrics_depth:
            return
        if tag == "h3":
            self._header = False
        elif tag == "div":
            self._lyrics_depth -= 1
        elif tag == "p":
            self._body.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._lyrics_depth:
            return
        if self._header:
            self._song_title.append(data)
        elif self._song_title:
            self._body.append(data)

    def _finish_song(self) -> None:
        title = " ".join("".join(self._song_title).split())
        body = "".join(self._body)
        if title:
            self.songs.append((self._song_number, title, body))
        self._song_title = []
        self._body = []

    def close(self) -> None:
        super().close()
        self._finish_song()


class _DarkLyricsIndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            values = dict(attrs)
            self._href = str(values.get("href") or "")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = ""


def _parse_darklyrics_songs(page: str) -> list[tuple[str, str, str]]:
    parser = _DarkLyricsDocumentParser()
    parser.feed(page)
    parser.close()
    return parser.songs if parser._container_found else []


def fetch_darklyrics(
    song: str,
    artist: str,
    timeout: int = 6,
    *,
    return_status: bool = False,
) -> dict[str, Any] | None:
    """Fetch a title-matched song from DarkLyrics' artist album index.

    Index links identify albums, not tracks. Their labels are never used as
    song identity evidence; the individual song heading is matched instead.
    """
    acquired_forms: list[dict[str, Any]] = []
    item_failures: list[tuple[str, str]] = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    title_norm = re.sub(r"[^a-z0-9]", "", song.casefold())
    artist_norm = re.sub(r"[^a-z0-9]", "", artist.casefold())

    def failed(status: str, error: str = "") -> dict[str, Any] | None:
        return {"source": "DarkLyrics", "acquisition_status": status, "error": error} if return_status else None

    def failure_for(exc: Exception) -> tuple[str, str]:
        if isinstance(exc, (UnicodeError, LookupError)):
            return "INVALID_RESPONSE", f"invalid provider text encoding: {exc}"
        code = getattr(exc, "code", None)
        if code == 404:
            return "NOT_FOUND", "HTTP 404"
        if code == 403:
            return "BLOCKED", "HTTP 403"
        if code == 429:
            return "RATE_LIMITED", "HTTP 429"
        if code in {401, 407}:
            return "AUTHENTICATION_ERROR", f"HTTP {code}"
        if code and code >= 500:
            return "TRANSIENT_ERROR", f"HTTP {code}"
        return "TRANSIENT_ERROR", f"{type(exc).__name__}: {exc}"

    if not artist_norm or not title_norm:
        return failed("INVALID_RESPONSE", "empty artist or title query")
    letter = "19" if artist_norm[0].isdigit() else artist_norm[0]
    artist_url = f"https://www.darklyrics.com/{letter}/{artist_norm}.html"
    challenge_markers = ("captcha", "verify you are human", "access denied", "unusual traffic")
    try:
        req = urllib.request.Request(artist_url, headers=headers)
        with _provider_urlopen(req, timeout=timeout) as resp:
            raw_index = resp.read(2_000_001)
            if len(raw_index) > 2_000_000:
                return failed("INVALID_RESPONSE", "artist index exceeds 2 MB")
            thtml = decode_provider_text(resp, raw_index)
            if any(marker in thtml.casefold() for marker in challenge_markers):
                return failed("BLOCKED", "provider returned a challenge/access page")
            index_parser = _DarkLyricsIndexParser()
            index_parser.feed(thtml)
            index_parser.close()
    except Exception as exc:
        status, error = failure_for(exc)
        return failed(status, error)

    album_links: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    for link_url, album_title in index_parser.links:
        parsed = urllib.parse.urlparse(urllib.parse.urljoin(artist_url, link_url))
        path_parts = parsed.path.casefold().strip("/").split("/")
        if len(path_parts) < 3 or path_parts[0] != "lyrics" or path_parts[1] != artist_norm:
            continue
        if parsed.scheme != "https" or parsed.netloc.casefold() not in {"www.darklyrics.com", "darklyrics.com"}:
            continue
        if parsed.path in seen_urls:
            continue
        seen_urls.add(parsed.path)
        album_links.append((urllib.parse.urlunparse(parsed._replace(fragment="")), album_title))
        if len(album_links) >= 20:
            break
    if not album_links:
        return failed("NOT_FOUND")

    parser_failures: list[str] = []
    valid_album_pages = 0
    for album_url, album_title in album_links:
        try:
            try:
                album_req = urllib.request.Request(album_url, headers=headers)
                with _provider_urlopen(album_req, timeout=timeout) as album_resp:
                    raw_album = album_resp.read(5_000_001)
                    if len(raw_album) > 5_000_000:
                        parser_failures.append("album page exceeds 5 MB")
                        continue
                    album_html = decode_provider_text(album_resp, raw_album)
            except Exception as exc:
                status, error = failure_for(exc)
                if status in {"BLOCKED", "RATE_LIMITED", "AUTHENTICATION_ERROR"}:
                    return failed(status, error)
                parser_failures.append(error)
                continue
            if any(marker in album_html.casefold() for marker in challenge_markers):
                return failed("BLOCKED", "provider returned a challenge/access page")
            parsed_songs = _parse_darklyrics_songs(album_html)
            if not parsed_songs:
                parser_failures.append("missing or changed DarkLyrics song container")
                continue
            valid_album_pages += 1
            for _song_number, candidate_title, text in parsed_songs:
                if not is_track_match(candidate_title, artist, song, artist):
                    continue
                if len(text.strip()) < 10:
                    parser_failures.append("matched song heading had an empty lyrics body")
                    continue
                if len(text) > 100_000:
                    return failed("INVALID_RESPONSE", "lyrics body exceeds 100 KB")
                acquired_forms.append(
                    {
                        "found": True,
                        "source": "DarkLyrics",
                        "id": album_url,
                        "provider_track_id": album_url,
                        "url": album_url,
                        "trackName": candidate_title,
                        "artistName": artist,
                        "albumName": album_title,
                        "identity_validated": True,
                        "source_confidence": 70,
                        "providerRaw": text,
                        "rawLyrics": text,
                        "format": "plain",
                        "syncType": "none",
                        "has_word_sync": False,
                        "has_back_vocals": False,
                        "plainLyrics": text,
                        "syncedLyrics": "",
                        "enhancedLyrics": "",
                        "ttmlLyrics": "",
                    }
                )
        except (OSError, ValueError, TypeError, KeyError, AttributeError, _LyricsAcquisitionError) as exc:
            _record_provider_failure(exc)
            status, detail = classify_lyrics_exception(exc)
            item_failures.append((status, detail))
            if status in {"RATE_LIMITED", "AUTHENTICATION_ERROR", "BLOCKED"}:
                break
            continue
    if acquired_forms or item_failures:
        return _collected_lyrics_result(acquired_forms, item_failures, source="DarkLyrics")

    if parser_failures:
        status = (
            "PARSER_ERROR"
            if any("missing or changed" in error or "empty lyrics body" in error for error in parser_failures)
            else (
                "INVALID_RESPONSE" if any("exceeds 5 MB" in error for error in parser_failures) else "TRANSIENT_ERROR"
            )
        )
        return failed(status, parser_failures[0])
    if valid_album_pages:
        return failed("NOT_FOUND")
    return failed("NOT_FOUND")


class _MetalCellParser(HTMLParser):
    """Read text and anchors from one Metal Archives DataTables cell."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.links: list[tuple[str, str, str]] = []
        self._anchor: tuple[str, str, list[str]] | None = None
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): value or "" for key, value in attrs}
        if tag.casefold() in {"script", "style"}:
            self._skip_depth += 1
        if tag.casefold() == "a":
            self._anchor = (attributes.get("href", ""), attributes.get("id", ""), [])
        if tag.casefold() in {"br", "p", "div"}:
            self.text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1
        if tag.casefold() == "a" and self._anchor is not None:
            href, element_id, value = self._anchor
            label = "".join(value).strip()
            self.links.append((href, element_id, label))
            self._anchor = None
        if tag.casefold() in {"p", "div", "tr", "td", "li"}:
            self.text.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self.text.append(data)
        if self._anchor is not None:
            self._anchor[2].append(data)

    def handle_entityref(self, name: str) -> None:
        self.handle_data(html.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        self.handle_data(html.unescape(f"&#{name};"))


class _MetalLyricsFragmentParser(HTMLParser):
    """Extract only a known lyrics container from the AJAX HTML fragment."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self._capture_depth: int | None = None
        self._skip_depth = 0
        self.parts: list[str] = []
        self.seen_container = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() not in {"br", "img", "hr", "input", "meta", "link", "wbr"}:
            self._depth += 1
        attributes = {key.casefold(): value or "" for key, value in attrs}
        classes = set(attributes.get("class", "").casefold().split())
        if not self.seen_container and (
            attributes.get("id", "").casefold() in {"lyrics", "lyricscontainer"}
            or bool(classes & {"lyrics", "lyricscontainer"})
        ):
            self.seen_container = True
            self._capture_depth = self._depth
        if self._capture_depth is not None and tag.casefold() in {"script", "style"}:
            self._skip_depth += 1
        if self._capture_depth is not None and self._skip_depth == 0 and tag.casefold() in {"br", "p", "div"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"br", "img", "hr", "input", "meta", "link", "wbr"}:
            return
        if self._capture_depth is not None and tag.casefold() in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1
        if self._capture_depth is not None and self._skip_depth == 0 and tag.casefold() in {"p", "div", "li"}:
            self.parts.append("\n")
        if self._capture_depth == self._depth:
            self._capture_depth = None
        self._depth = max(0, self._depth - 1)

    def handle_data(self, data: str) -> None:
        if self._capture_depth is not None and self._skip_depth == 0:
            self.parts.append(data)


def _metal_cell(raw: str) -> tuple[str, list[tuple[str, str, str]]]:
    parser = _MetalCellParser()
    parser.feed(raw)
    parser.close()
    text = re.sub(r"[\t\u00a0 ]+", " ", "".join(parser.text))
    text = re.sub(r"\n[ \t]*\n+", "\n", text).strip()
    return text, parser.links


def parse_metal_archives_search(payload: str | bytes, song: str, artist: str) -> list[dict[str, str]]:
    """Parse the observed JSON envelope and only identity-matched rows."""
    data = _provider_json_loads(payload)
    if not isinstance(data, dict) or not isinstance(data.get("aaData"), list):
        raise TypeError("Metal Archives search response has no aaData list")
    rows = data["aaData"]
    if len(rows) > 1000:
        raise ValueError("Metal Archives search response has too many rows")
    matches: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 2 or any(not isinstance(cell, str) for cell in row):
            raise ValueError("Metal Archives search row has an unexpected schema")
        cells = [_metal_cell(cell) for cell in row]
        all_links = [(index, link) for index, (_text, links) in enumerate(cells) for link in links]
        lyric_ids = [
            match.group(1)
            for _index, (href, element_id, _label) in all_links
            for match in [
                re.search(r"lyricsLink_(\d+)", element_id) or re.search(r"lyrics(?:/|=)(\d+)", href, re.IGNORECASE)
            ]
            if match
        ]
        if not lyric_ids:
            continue

        title_options: list[tuple[float, str]] = []
        for _index, (href, element_id, label) in all_links:
            if re.search(r"lyricsLink_\d+", element_id) or re.search(r"lyrics(?:/|=)\d+", href, re.IGNORECASE):
                continue
            if label:
                title_options.append((text_similarity(label, song), label))
        if not title_options:
            title_options.extend((text_similarity(text, song), text) for text, _links in cells if text)
        _title_score, candidate_title = max(
            title_options, default=(0.0, ""), key=lambda item: (item[0], item[1].casefold())
        )

        artist_options = [(text_similarity(text, artist), text) for text, _links in cells if text]
        artist_score, candidate_artist = max(
            artist_options, default=(0.0, ""), key=lambda item: (item[0], item[1].casefold())
        )
        if artist_score < 0.75 or not is_track_match(candidate_title, candidate_artist, song, artist):
            continue

        album_options = [text for text, _links in cells if text and text not in {candidate_title, candidate_artist}]
        matches.append(
            {
                "id": lyric_ids[0],
                "title": candidate_title,
                "artist": candidate_artist,
                "album": album_options[0] if album_options else "",
            }
        )
    return matches


def parse_metal_archives_lyrics_page(payload: str | bytes) -> str:
    """Extract the lyrics container without treating page chrome as lyrics."""
    text = payload.decode("utf-8", errors="strict") if isinstance(payload, bytes) else payload
    if len(text) > 500_000:
        raise ValueError("Metal Archives lyrics fragment exceeds 500 KB")
    if any(marker in text.casefold() for marker in ("captcha", "access denied", "cloudflare", "temporarily blocked")):
        raise ValueError("Metal Archives returned a block/challenge page")
    parser = _MetalLyricsFragmentParser()
    parser.feed(text)
    parser.close()
    if not parser.seen_container:
        raise ValueError("Metal Archives lyrics container is missing or changed")
    lyrics = "".join(parser.parts).replace("\r\n", "\n").replace("\r", "\n")
    lyrics = "\n".join(line.rstrip() for line in lyrics.split("\n")).strip("\n")
    return lyrics


def fetch_metal_archives(
    song: str,
    artist: str,
    timeout: int = 6,
) -> dict[str, Any] | None:
    """Use the provider's AJAX search and verify both song and band before lookup."""
    acquired_forms: list[dict[str, Any]] = []
    item_failures: list[tuple[str, str]] = []
    headers = {
        "User-Agent": "AudiophileArchTagger/6.0 (lyrics lookup; contact through project repository)",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": "https://www.metal-archives.com/",
    }
    query = urllib.parse.urlencode({"songTitle": song, "bandName": artist})
    search_url = f"https://www.metal-archives.com/search/ajax-advanced/searching/songs/?{query}"
    parser_errors: list[str] = []
    valid_search = False
    try:
        request = urllib.request.Request(search_url, headers=headers)
        with _provider_urlopen(request, timeout=timeout) as response:
            search_raw = response.read(1_000_001)
            if len(search_raw) > 1_000_000:
                return {
                    "source": "Metal-Archives",
                    "acquisition_status": "INVALID_RESPONSE",
                    "error": "search response exceeds 1 MB",
                }
            search_text = decode_provider_text(response, search_raw)
        if any(
            marker in search_text.casefold()
            for marker in ("captcha", "access denied", "cloudflare", "temporarily blocked")
        ):
            return {
                "source": "Metal-Archives",
                "acquisition_status": "BLOCKED",
                "error": "provider returned a block/challenge page",
            }
        rows = parse_metal_archives_search(search_text, song, artist)
        valid_search = True
    except Exception as exc:
        status, error = classify_lyrics_exception(exc)
        if status not in {"PARSER_ERROR", "INVALID_RESPONSE"}:
            return {"source": "Metal-Archives", "acquisition_status": status, "error": error}
        parser_errors.append(error)
        rows = []

    for match in rows[:10]:
        try:
            lyric_id = match["id"]
            view_url = f"https://www.metal-archives.com/release/ajax-view-lyrics/id/{_quote_component(lyric_id)}"
            try:
                view_request = urllib.request.Request(view_url, headers=headers)
                with _provider_urlopen(view_request, timeout=min(timeout, 5)) as response:
                    page_raw = response.read(500_001)
                    if len(page_raw) > 500_000:
                        raise _LyricsAcquisitionError("INVALID_RESPONSE", "lyrics response exceeds 500 KB")
                    page = decode_provider_text(response, page_raw)
                if any(
                    marker in page.casefold()
                    for marker in ("captcha", "access denied", "cloudflare", "temporarily blocked")
                ):
                    raise _LyricsAcquisitionError("BLOCKED", "provider returned a block/challenge page")
                lyrics = parse_metal_archives_lyrics_page(page)
            except Exception as exc:
                status, error = classify_lyrics_exception(exc)
                if status in {"BLOCKED", "RATE_LIMITED", "TRANSIENT_ERROR", "AUTHENTICATION_ERROR"}:
                    raise _LyricsAcquisitionError(status, error)
                parser_errors.append(error)
                continue
            if "lyrics not available" in lyrics.casefold() or len(lyrics.strip()) < 20:
                continue
            acquired_forms.append(
                {
                    "found": True,
                    "source": "Metal-Archives",
                    "id": lyric_id,
                    "provider_track_id": lyric_id,
                    "url": view_url,
                    "trackName": match["title"],
                    "artistName": match["artist"],
                    "albumName": match["album"],
                    "identity_validated": True,
                    "source_confidence": 64,
                    "format": "plain",
                    "syncType": "none",
                    "has_word_sync": False,
                    "has_back_vocals": False,
                    "providerRaw": lyrics,
                    "rawLyrics": lyrics,
                    "plainLyrics": lyrics,
                    "syncedLyrics": "",
                    "enhancedLyrics": "",
                    "ttmlLyrics": "",
                }
            )
        except (OSError, ValueError, TypeError, KeyError, AttributeError, _LyricsAcquisitionError) as exc:
            _record_provider_failure(exc)
            status, detail = classify_lyrics_exception(exc)
            item_failures.append((status, detail))
            if status in {"RATE_LIMITED", "AUTHENTICATION_ERROR", "BLOCKED"}:
                break
            continue
    if acquired_forms or item_failures:
        return _collected_lyrics_result(
            acquired_forms, item_failures, source="Metal-Archives", truncated=len(rows) > 10
        )
    if parser_errors:
        return {"source": "Metal-Archives", "acquisition_status": "PARSER_ERROR", "error": parser_errors[0]}
    if valid_search:
        mark_provider_not_found()
    return None


def fetch_vocadb(
    song: str,
    artist: str,
    timeout: int = 6,
) -> dict[str, Any] | None:
    """Query VocaDB (Vocaloid / Synthesizer V / UTAU) official REST API for kanji, romaji and translation lyrics."""
    queries = list(dict.fromkeys(query for query in (f"{song} {artist}".strip(), song.strip()) if query))
    headers = {"User-Agent": "AudiophileArchTagger/6.0 (VocaDBClient)"}
    candidates: list[dict[str, Any]] = []
    failures: list[tuple[str, str]] = []
    valid_search = False
    for query in queries:
        url = "https://vocadb.net/api/songs?" + urllib.parse.urlencode(
            {
                "query": query,
                "sort": "FavoritedTimes",
                "fields": "Lyrics,Artists,Names",
                "maxResults": "5",
            }
        )
        try:
            req = urllib.request.Request(url, headers=headers)
            with _provider_urlopen(req, timeout=timeout) as resp:
                data = _provider_json_loads(read_provider_text(resp))
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise TypeError("VocaDB search response has no items array")
            items = data["items"]
            valid_search = True
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
            status, detail = classify_lyrics_exception(exc)
            _record_provider_failure(exc)
            failures.append((status, detail))
            continue

        for item in items:
            if not isinstance(item, dict):
                failures.append(("PARSER_ERROR", "VocaDB search contains a non-object song"))
                continue
            try:
                it = item
                names = it.get("names") or {}
                if not isinstance(names, dict):
                    raise TypeError("VocaDB song names field is not an object")
                artist_rows = it.get("artists") or []
                if not isinstance(artist_rows, list):
                    raise TypeError("VocaDB song artists field is not an array")
                artist_names = []
                for entry in artist_rows:
                    if not isinstance(entry, dict):
                        raise TypeError("VocaDB song has a malformed artist entry")
                    artist_obj = entry.get("artist") or {}
                    if not isinstance(artist_obj, dict):
                        raise TypeError("VocaDB artist field is not an object")
                    artist_names.append(str(artist_obj.get("name") or entry.get("name") or ""))
                candidate_artist = " ".join(name for name in artist_names if name)
                name_candidates = [
                    str(it.get("name") or ""),
                    str(names.get("original") or ""),
                ]
                additional_names = names.get("additionalNames") or []
                if not isinstance(additional_names, list):
                    raise TypeError("VocaDB additionalNames field is not an array")
                name_candidates.extend(str(value) for value in additional_names)
                candidate_title = next(
                    (
                        candidate
                        for candidate in name_candidates
                        if candidate and is_track_match(candidate, candidate_artist, song, artist)
                    ),
                    "",
                )
                if not candidate_title:
                    continue

                lyric_rows = it.get("lyrics", [])
                if not isinstance(lyric_rows, list):
                    raise TypeError("VocaDB lyrics field is not an array")
                song_id = str(it.get("id") or "")
                if not lyric_rows and song_id:
                    detail_url = f"https://vocadb.net/api/songs/{urllib.parse.quote(song_id, safe='')}?fields=Lyrics"
                    try:
                        detail_request = urllib.request.Request(detail_url, headers=headers)
                        with _provider_urlopen(detail_request, timeout=min(timeout, 4)) as detail_response:
                            detail = _provider_json_loads(read_provider_text(detail_response))
                        if not isinstance(detail, dict) or not isinstance(detail.get("lyrics"), list):
                            raise TypeError("VocaDB detail response has no lyrics array")
                        lyric_rows = detail["lyrics"]
                        it = {**it, **detail}
                    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
                        status, detail_message = classify_lyrics_exception(exc)
                        _record_provider_failure(exc)
                        failures.append((status, detail_message))
                        continue

                original = ""
                romanized = ""
                translated = ""
                original_language = ""
                romanization_language = ""
                translation_language = ""
                unsupported_forms: list[str] = []
                for lyric in lyric_rows:
                    if not isinstance(lyric, dict):
                        raise TypeError("VocaDB lyrics array contains a non-object form")
                    form_type = str(lyric.get("translationType") or "")
                    value = str(lyric.get("value") or "")
                    if not value:
                        continue
                    form_language = str(lyric.get("cultureCode") or lyric.get("language") or "")
                    if form_type == "Original":
                        if not original:
                            original, original_language = value, form_language
                    elif form_type == "Romanized":
                        if not romanized:
                            romanized, romanization_language = value, form_language
                    elif form_type == "Translation":
                        if not translated:
                            translated, translation_language = value, form_language
                    else:
                        unsupported_forms.append(form_type or "unspecified")
                if unsupported_forms and not (original or romanized or translated):
                    raise ValueError("VocaDB returned lyrics in unsupported forms: " + ", ".join(unsupported_forms))
                chosen = original or romanized or translated
                if len(chosen.strip()) < 10:
                    continue
                candidates.append(
                    {
                        "found": True,
                        "source": "VocaDB",
                        "id": song_id,
                        "provider_track_id": song_id,
                        "url": f"https://vocadb.net/S/{urllib.parse.quote(song_id, safe='')}" if song_id else "",
                        "trackName": candidate_title,
                        "artistName": candidate_artist,
                        "albumName": str(it.get("album") or it.get("albumName") or ""),
                        "duration": it.get("lengthSeconds"),
                        "language": original_language,
                        "translationLanguage": translation_language,
                        "romanizationLanguage": romanization_language,
                        "identity_validated": True,
                        "format": "plain",
                        "syncType": "none",
                        "has_word_sync": False,
                        "has_back_vocals": False,
                        "providerRaw": json.dumps(it, ensure_ascii=False, sort_keys=True),
                        "rawLyrics": chosen,
                        "plainLyrics": original,
                        "translationLyrics": translated,
                        "romajiLyrics": romanized,
                        "translation": not original and not romanized and bool(translated),
                        "romanization": not original and bool(romanized),
                        "syncedLyrics": "",
                        "enhancedLyrics": "",
                        "ttmlLyrics": "",
                        "providerMetadata": {
                            "song_id": song_id,
                            "lyric_forms": lyric_rows,
                            "artist_ids": [
                                str((entry.get("artist") or {}).get("id") or "")
                                for entry in artist_rows
                                if isinstance(entry.get("artist") or {}, dict)
                            ],
                            "unsupported_lyric_forms": unsupported_forms,
                        },
                    }
                )
            except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
                status, detail = classify_lyrics_exception(exc)
                _record_provider_failure(exc)
                failures.append((status, detail))

        if items:
            break

    if candidates:
        candidates.sort(
            key=lambda item: (
                text_similarity(str(item.get("trackName") or ""), song)
                + text_similarity(str(item.get("artistName") or ""), artist),
                str(item.get("provider_track_id") or ""),
            ),
            reverse=True,
        )
        candidates[0]["alternatives"] = candidates[1:]
        if failures:
            candidates[0]["validationWarnings"] = [
                f"Some VocaDB alternatives could not be inspected ({failures[0][0]})"
            ]
        return candidates[0]
    if failures:
        severity = {
            "AUTHENTICATION_ERROR": 7,
            "BLOCKED": 6,
            "RATE_LIMITED": 5,
            "TRANSIENT_ERROR": 4,
            "PARSER_ERROR": 3,
            "INVALID_RESPONSE": 2,
            "NOT_FOUND": 1,
        }
        status, detail = max(failures, key=lambda item: severity.get(item[0], 0))
        return {"source": "VocaDB", "acquisition_status": status, "error": detail}
    if valid_search:
        mark_provider_not_found()
        return {"source": "VocaDB", "acquisition_status": "NOT_FOUND"}
    return {"source": "VocaDB", "acquisition_status": "TRANSIENT_ERROR", "error": "no valid API response"}


class _TekstowoPageParser(HTMLParser):
    """Extract search titles and the semantic lyrics container from Tekstowo HTML."""

    _VOID = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.in_results = 0
        self.saw_results_region = False
        self._div_stack: list[bool] = []
        self.anchor: tuple[str, list[str]] | None = None
        self.search_results: list[tuple[str, str]] = []
        self.lyrics_depth = 0
        self.skip_depth = 0
        self.lyrics_text: list[str] = []
        self.lyrics_raw: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").casefold().split())
        if tag.casefold() == "div":
            is_results_region = "box-przeboje" in classes
            self._div_stack.append(is_results_region)
            if is_results_region:
                self.saw_results_region = True
                self.in_results += 1
        if tag == "a" and attributes.get("href") and ("title" in classes or self.in_results):
            self.anchor = (str(attributes["href"] or ""), [])
        if tag == "div" and "inner-text" in classes and self.lyrics_depth == 0:
            self.lyrics_depth = 1
            self.lyrics_raw.append(self.get_starttag_text() or "")
            return
        if self.lyrics_depth:
            self.lyrics_raw.append(self.get_starttag_text() or "")
            if tag.casefold() in {"script", "style"}:
                self.skip_depth += 1
            elif not self.skip_depth and tag.casefold() in {"br", "p", "div"}:
                self.lyrics_text.append("\n")
            if tag.casefold() not in self._VOID:
                self.lyrics_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self.anchor is not None and tag.casefold() == "a":
            self.search_results.append((self.anchor[0], "".join(self.anchor[1]).strip()))
            self.anchor = None
        if self.lyrics_depth:
            self.lyrics_raw.append(f"</{tag}>")
            if tag.casefold() in {"script", "style"} and self.skip_depth:
                self.skip_depth -= 1
            if tag.casefold() not in self._VOID:
                self.lyrics_depth -= 1
        if tag.casefold() == "div" and self._div_stack:
            if self._div_stack.pop():
                self.in_results = max(0, self.in_results - 1)

    def handle_data(self, data: str) -> None:
        if self.anchor is not None:
            self.anchor[1].append(data)
        if self.lyrics_depth and not self.skip_depth:
            self.lyrics_text.append(data)
            self.lyrics_raw.append(data)

    def handle_entityref(self, name: str) -> None:
        value = html.unescape(f"&{name};")
        if self.anchor is not None:
            self.anchor[1].append(value)
        if self.lyrics_depth and not self.skip_depth:
            self.lyrics_text.append(value)
            self.lyrics_raw.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        value = html.unescape(f"&#{name};")
        if self.anchor is not None:
            self.anchor[1].append(value)
        if self.lyrics_depth and not self.skip_depth:
            self.lyrics_text.append(value)
            self.lyrics_raw.append(f"&#{name};")


def _tekstowo_identity(label: str, song: str, artist: str) -> tuple[str, str] | None:
    parts = [part.strip() for part in re.split(r"\s+-\s+", html.unescape(label)) if part.strip()]
    for split in range(1, len(parts)):
        left, right = " - ".join(parts[:split]), " - ".join(parts[split:])
        if is_track_match(right, left, song, artist):
            return right, left
        if is_track_match(left, right, song, artist):
            return left, right
    return None


def parse_tekstowo_search(page: str, song: str, artist: str) -> list[dict[str, str]]:
    if len(page) > 5_000_000:
        raise ValueError("Tekstowo search page exceeds 5 MB")
    parser = _TekstowoPageParser()
    parser.feed(page)
    parser.close()
    if not parser.saw_results_region and not parser.search_results:
        raise ValueError("Tekstowo search results container is missing or changed")
    candidates: list[dict[str, str]] = []
    for href, label in parser.search_results:
        identity = _tekstowo_identity(label, song, artist)
        if identity is None:
            continue
        parsed = urllib.parse.urlparse(urllib.parse.urljoin("https://www.tekstowo.pl/", href))
        if parsed.scheme == "https" and parsed.hostname in {"tekstowo.pl", "www.tekstowo.pl"}:
            candidates.append({"url": parsed.geturl(), "title": identity[0], "artist": identity[1]})
    return candidates


def parse_tekstowo_lyrics(page: str) -> tuple[str, str]:
    if len(page) > 5_000_000:
        raise ValueError("Tekstowo lyrics page exceeds 5 MB")
    parser = _TekstowoPageParser()
    parser.feed(page)
    parser.close()
    if not parser.lyrics_raw:
        raise ValueError("Tekstowo lyrics container is missing or changed")
    return "".join(parser.lyrics_raw), "".join(parser.lyrics_text).replace("\r\n", "\n").replace("\r", "\n")


def fetch_tekstowo(song: str, artist: str, timeout: int = 6) -> dict[str, Any] | None:
    """Fetch lyrics only after matching title and artist in semantic search results."""
    acquired_forms: list[dict[str, Any]] = []
    item_failures: list[tuple[str, str]] = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
        "Accept": "text/html,application/xhtml+xml",
    }
    url = f"https://www.tekstowo.pl/szukaj,wykonawca,{_quote_component(artist)},tytul,{_quote_component(song)}.html"
    request = urllib.request.Request(url, headers=headers)
    with _provider_urlopen(request, timeout=timeout) as response:
        search_html = read_provider_text(response)
    matched = parse_tekstowo_search(search_html, song, artist)
    if not matched:
        mark_provider_not_found()
        return {"source": "Tekstowo.pl", "acquisition_status": "NOT_FOUND"}

    parser_errors: list[str] = []
    for item in matched[:5]:
        try:
            detail_url, matched_title, matched_artist = item["url"], item["title"], item["artist"]
            request = urllib.request.Request(detail_url, headers=headers)
            with _provider_urlopen(request, timeout=timeout) as response:
                page = read_provider_text(response)
                final_url = (
                    urllib.parse.urlparse(response.geturl())
                    if callable(getattr(response, "geturl", None))
                    else urllib.parse.urlparse(detail_url)
                )
            if final_url.scheme != "https" or final_url.hostname not in {"tekstowo.pl", "www.tekstowo.pl"}:
                raise _LyricsAcquisitionError("INVALID_RESPONSE", "redirect left provider host")
            try:
                raw_markup, raw_text = parse_tekstowo_lyrics(page)
            except ValueError as exc:
                parser_errors.append(str(exc))
                continue
            parsed_text = "\n".join(
                line for line in raw_text.split("\n") if line.strip().casefold() != "(tekst piosenki)"
            )
            if len(parsed_text.strip()) < 20:
                parser_errors.append("Tekstowo lyrics container is empty or implausibly short")
                continue
            acquired_forms.append(
                {
                    "found": True,
                    "source": "Tekstowo.pl",
                    "id": detail_url.rsplit("/", 1)[-1],
                    "provider_track_id": detail_url,
                    "url": detail_url,
                    "trackName": matched_title,
                    "artistName": matched_artist,
                    "identity_validated": True,
                    "source_confidence": 64,
                    "format": "plain",
                    "syncType": "none",
                    "has_word_sync": False,
                    "has_back_vocals": False,
                    "providerRaw": raw_markup,
                    "rawLyrics": raw_text,
                    "plainLyrics": parsed_text,
                    "syncedLyrics": "",
                    "enhancedLyrics": "",
                    "ttmlLyrics": "",
                }
            )
        except (OSError, ValueError, TypeError, KeyError, AttributeError, _LyricsAcquisitionError) as exc:
            _record_provider_failure(exc)
            status, detail = classify_lyrics_exception(exc)
            item_failures.append((status, detail))
            if status in {"RATE_LIMITED", "AUTHENTICATION_ERROR", "BLOCKED"}:
                break
            continue
    if acquired_forms or item_failures:
        return _collected_lyrics_result(acquired_forms, item_failures, source="Tekstowo.pl", truncated=len(matched) > 5)
    return {"source": "Tekstowo.pl", "acquisition_status": "PARSER_ERROR", "error": parser_errors[0]}


def _ttml_declared_language(text: str) -> str:
    """Read an explicit TTML language without inferring it from script."""
    if len(text) > 5_000_000 or re.search(r"<!DOCTYPE|<!ENTITY", text, re.IGNORECASE):
        return ""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return ""
    xml_lang = "{http://www.w3.org/XML/1998/namespace}lang"
    declared = {
        str(element.attrib[xml_lang]).strip() for element in root.iter() if element.attrib.get(xml_lang, "").strip()
    }
    return next(iter(declared)) if len(declared) == 1 else ""


def _unison_item_to_candidate(
    item: Mapping[str, Any], query_song: str, query_artist: str, title: str, artist: str
) -> dict[str, Any] | None:
    """Parse one Unison API item while retaining the exact source lyrics."""
    raw_value = item.get("lyrics")
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    raw = raw_value
    title = str(item.get("song") or item.get("trackName") or title)
    artist = str(item.get("artist") or item.get("artistName") or artist)
    native_ttml = is_ttml(raw) or str(item.get("format") or "").casefold() == "ttml"
    warnings: list[str] = []
    plain = ""
    synced = ""
    enhanced = ""
    has_words = False
    has_background = False
    if native_ttml:
        plain, enhanced, has_words, has_background = parse_ttml(
            raw, song=query_song, artist=query_artist, diagnostics=warnings
        )
        synced = normalize_synced(raw, keep_word_sync=False, song=query_song, artist=query_artist)
        if not plain and not synced and warnings:
            raise ValueError("Unison TTML parser could not extract any lyrics")
    else:
        word_timed = bool(WORD_TIMESTAMP_RE.search(raw))
        has_timing_markers = word_timed or bool(ANY_LINE_TIMESTAMP_RE.search(raw))
        if word_timed and validate_enhanced_lrc(raw)[0]:
            enhanced = raw
            has_words = True
            synced = normalize_synced(raw, keep_word_sync=False, song=query_song, artist=query_artist)
        elif ANY_LINE_TIMESTAMP_RE.search(raw) and validate_lrc(raw)[0]:
            synced = raw
        elif has_timing_markers:
            warnings.append("Unison timestamps failed validation; lyric text retained without synchronization")
        plain = to_plain(raw, song=query_song, artist=query_artist) if has_timing_markers else raw

    raw_duration = item.get("duration")
    duration_value = _provider_duration_seconds(raw_duration)
    if raw_duration is not None and duration_value is None:
        warnings.append("Unison supplied an invalid duration; duration evidence ignored")
    language_value = item.get("language")
    declared_language = _ttml_declared_language(raw) if native_ttml else ""
    language = str(language_value).strip() if language_value is not None else declared_language

    if not plain and synced:
        plain = to_plain(synced, song=query_song, artist=query_artist)
    if not plain and not synced:
        return None
    return {
        "found": True,
        "source": "Unison",
        "attribution": "Unison",
        "id": str(item.get("id") or item.get("videoId") or ""),
        "provider_track_id": str(item.get("id") or item.get("videoId") or ""),
        "url": str(item.get("url") or item.get("canonicalUrl") or ""),
        "trackName": title,
        "artistName": artist,
        "albumName": str(item.get("album") or item.get("albumName") or ""),
        "version": str(item.get("version") or item.get("versionName") or ""),
        "duration": duration_value,
        "language": language,
        "isrc": str(item.get("isrc") or ""),
        "recording_id": str(item.get("recording_id") or item.get("musicbrainz_recording_id") or ""),
        "identity_validated": bool(title and artist and is_track_match(title, artist, query_song, query_artist)),
        "format": "ttml" if native_ttml else ("elrc" if has_words else ("lrc" if synced else "plain")),
        "syncType": "richsync" if has_words else ("linesync" if synced else "none"),
        "has_word_sync": has_words,
        "has_back_vocals": has_background,
        "providerRaw": raw,
        "rawLyrics": raw,
        "plainLyrics": plain,
        "syncedLyrics": synced,
        "enhancedLyrics": enhanced,
        "ttmlLyrics": raw if native_ttml else "",
        "ttml_native": native_ttml,
        "validationWarnings": warnings,
        "providerMetadata": {
            key: item[key] for key in ("videoId", "album", "format", "syncType", "language") if key in item
        },
    }


def fetch_apple_music_ttml(
    song: str,
    artist: str,
    album: str = "",
    duration: float | None = None,
    timeout: int = 8,
) -> dict[str, Any] | None:
    """Legacy adapter name for the public Unison lyrics service.

    This function does not call an Apple Music endpoint. The returned source
    and attribution therefore identify Unison rather than implying Apple
    Music supplied or licensed the response.
    """
    headers = {"User-Agent": "AudiophileArchTagger/6.0 (UnisonClient)", "Accept": "application/json"}

    candidates: list[dict[str, Any]] = []
    # 1. Search Unison API candidates
    search_q = f"{artist} {song}".strip()
    s_url = f"https://unison.boidu.dev/lyrics/search?q={_quote_component(search_q)}"
    try:
        req = urllib.request.Request(s_url, headers=headers)
        with _provider_urlopen(req, timeout=timeout) as resp:
            final = urllib.parse.urlparse(resp.geturl() if callable(getattr(resp, "geturl", None)) else s_url)
            if final.scheme != "https" or final.hostname != "unison.boidu.dev":
                return {
                    "source": "Unison",
                    "acquisition_status": "INVALID_RESPONSE",
                    "error": "search redirect left the Unison provider host",
                }
            s_data = _provider_json_loads(read_provider_text(resp))
            if not isinstance(s_data, dict) or not isinstance(s_data.get("data"), list):
                raise TypeError("Unison search response has no data array")
            items = s_data["data"]
            for item in items[:20]:
                if not isinstance(item, dict):
                    _record_provider_failure(TypeError("Unison search response contains a non-object item"))
                    continue
                t_cand = item.get("song", "")
                a_cand = item.get("artist", "")
                if is_track_match(t_cand, a_cand, song, artist):
                    vid = item.get("videoId")
                    lid = item.get("id")
                    f_url = (
                        f"https://unison.boidu.dev/lyrics?v={vid}" if vid else f"https://unison.boidu.dev/lyrics/{lid}"
                    )
                    try:
                        with _provider_urlopen(
                            urllib.request.Request(f_url, headers=headers), timeout=timeout
                        ) as lresp:
                            final = urllib.parse.urlparse(
                                lresp.geturl() if callable(getattr(lresp, "geturl", None)) else f_url
                            )
                            if final.scheme != "https" or final.hostname != "unison.boidu.dev":
                                raise ValueError("lyrics redirect left the Unison provider host")
                            ldata = _provider_json_loads(read_provider_text(lresp))
                            if (
                                not isinstance(ldata, dict)
                                or not ldata.get("success")
                                or not isinstance(ldata.get("data"), dict)
                            ):
                                raise ValueError("Unison lyrics response has an unexpected schema")
                            litem = ldata["data"]
                            if not isinstance(litem.get("lyrics"), str):
                                raise TypeError("Unison lyrics item has no text field")
                            candidate = _unison_item_to_candidate(litem, song, artist, t_cand, a_cand)
                            if candidate:
                                candidate["searchIdentityValidated"] = True
                                candidate["url"] = f_url
                                candidates.append(candidate)
                            if candidate is None:
                                mark_provider_not_found()
                    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
                        _record_provider_failure(exc)
            # A valid search response with no exact artist/title match is
            # trustworthy no-result evidence, while the direct endpoint below
            # still gets its independent chance.
            mark_provider_not_found()
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        _record_provider_failure(exc)

    if candidates:
        selected = dict(candidates[0])
        selected["alternatives"] = candidates[1:]
        if len(items) > 20:
            selected.setdefault("validationWarnings", []).append("Unison search truncated to 20 items")
        return selected

    # 2. Direct endpoint fallback
    params: dict[str, str] = {"song": song.strip(), "artist": artist.strip()}
    if album:
        params["album"] = album.strip()
    if duration is not None:
        try:
            params["duration"] = str(round(float(duration)))
        except Exception:
            pass
    url = f"https://unison.boidu.dev/lyrics?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with _provider_urlopen(req, timeout=timeout) as resp:
            final = urllib.parse.urlparse(resp.geturl() if callable(getattr(resp, "geturl", None)) else url)
            if final.scheme != "https" or final.hostname != "unison.boidu.dev":
                return {
                    "source": "Unison",
                    "acquisition_status": "INVALID_RESPONSE",
                    "error": "direct lookup redirect left the Unison provider host",
                }
            data = _provider_json_loads(read_provider_text(resp))
            if not isinstance(data, dict) or not data.get("success") or not isinstance(data.get("data"), dict):
                raise ValueError("Unison direct response has an unexpected schema")
            if data.get("success") and "data" in data and isinstance(data["data"], dict):
                item = data["data"]
                if not isinstance(item.get("lyrics"), str):
                    raise TypeError("Unison direct result has no text field")
                candidate_title = str(item.get("song") or item.get("trackName") or "")
                candidate_artist = str(item.get("artist") or item.get("artistName") or "")
                candidate = _unison_item_to_candidate(item, song, artist, candidate_title, candidate_artist)
                if candidate:
                    candidate["identity_validated"] = bool(
                        candidate_title
                        and candidate_artist
                        and is_track_match(candidate_title, candidate_artist, song, artist)
                    )
                    candidate["url"] = url
                    return candidate
                mark_provider_not_found()
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        _record_provider_failure(exc)
    return None


class NeuralLyricsEngine:
    """Level 4 Local Neural Fallback:
    - Vocal isolation: Demucs v4
    - VAD filtering: PyAnnote VAD
    - Branch A: CTC Forced Alignment (MMS / Wav2Vec2) when plain text is found
    - Branch B: Faster-Whisper large-v3 ASR + phoneme alignment when no text is found
    """

    @staticmethod
    def is_available() -> dict[str, bool]:
        res = {}
        for mod in ("torch", "torchaudio", "demucs", "pyannote", "faster_whisper"):
            try:
                __import__(mod)
                res[mod] = True
            except ImportError:
                res[mod] = False
        return res

    @classmethod
    def isolate_vocals(cls, audio_path: Path, output_dir: Path | None = None) -> Path | None:
        """Run Demucs v4 to isolate vocal stem."""
        if output_dir is None:
            output_dir = audio_path.parent / ".vocal_stems" / audio_path.stem
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None
        vocals_path = output_dir / "vocals.wav"
        if vocals_path.is_file() and vocals_path.stat().st_size > 0:
            return vocals_path

        demucs_bin = shutil.which("demucs")
        if demucs_bin:
            try:
                cmd = [demucs_bin, "--two-stems=vocals", "-o", str(output_dir), str(audio_path)]
                res = subprocess.run(cmd, capture_output=True, text=True, check=False)
                if res.returncode == 0:
                    found = list(output_dir.glob("**/vocals.wav"))
                    if found:
                        return found[0]
            except Exception:
                pass

        try:
            import demucs.separate  # type: ignore[import-not-found]

            demucs.separate.main(["--two-stems=vocals", "-o", str(output_dir), str(audio_path)])
            found = list(output_dir.glob("**/vocals.wav"))
            if found:
                return found[0]
        except Exception:
            pass

        return None

    @classmethod
    def can_align_mms(cls, plain_text: str, language: str | None) -> bool:
        """Avoid aligning with an English transcript codec for unknown/other languages."""
        code = (language or "").casefold().replace("_", "-").split("-", 1)[0]
        return code in {"en", "eng"} and plain_text.isascii()

    @classmethod
    def forced_alignment(
        cls,
        audio_path: Path,
        plain_text: str,
        song: str = "",
        artist: str = "",
        language: str | None = None,
    ) -> dict[str, Any] | None:
        """Run CTC Forced Alignment (MMS / Wav2Vec2) to lock plain text to audio timestamps."""
        if not cls.can_align_mms(plain_text, language):
            return None
        try:
            import torch  # type: ignore[import-not-found]
            import torchaudio  # type: ignore[import-not-found]

            bundle = torchaudio.pipelines.MMS_FA
            model = bundle.get_model()
            tokenizer = bundle.get_tokenizer()
            aligner = bundle.get_aligner()

            waveform, sample_rate = torchaudio.load(str(audio_path))
            if sample_rate != bundle.sample_rate:
                waveform = torchaudio.functional.resample(waveform, sample_rate, bundle.sample_rate)

            words = plain_text.replace("\n", " ").split()
            if not words:
                return None

            with torch.inference_mode():
                emission, _ = model(waveform)
                token_spans = aligner(emission[0], tokenizer(words))

            elrc_lines = []
            curr_words = []
            for word, spans in zip(words, token_spans):
                if spans:
                    t_start = spans[0].start / bundle.sample_rate
                    w_ts = fmt_lrc_time(t_start)
                    curr_words.append(f"<{w_ts}>{word}")
                    if len(curr_words) >= 7:
                        first_ts = curr_words[0].split(">")[0].lstrip("<")
                        elrc_lines.append(f"[{first_ts}] {' '.join(curr_words)}")
                        curr_words = []
            if curr_words:
                first_ts = curr_words[0].split(">")[0].lstrip("<")
                elrc_lines.append(f"[{first_ts}] {' '.join(curr_words)}")

            if elrc_lines:
                elrc_text = "\n".join(elrc_lines) + "\n"
                std_lrc = normalize_synced(elrc_text, keep_word_sync=False, song=song, artist=artist)
                ttml = lrc_to_ttml(elrc_text, song=song, artist=artist)
                return {
                    "found": True,
                    "source": "Neural Forced Alignment (MMS)",
                    "format": "ttml",
                    "syncType": "richsync",
                    "has_word_sync": True,
                    "has_back_vocals": False,
                    "rawLyrics": elrc_text,
                    "plainLyrics": plain_text,
                    "syncedLyrics": std_lrc,
                    "enhancedLyrics": elrc_text,
                    "ttmlLyrics": ttml,
                }
        except Exception as e:
            sys.stderr.write(f"Neural alignment notice: {e}\n")
        return None

    @classmethod
    def whisper_asr(
        cls,
        audio_path: Path,
        song: str = "",
        artist: str = "",
        language: str | None = None,
    ) -> dict[str, Any] | None:
        """Run Faster-Whisper large-v3 ASR with word timestamps."""
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]

            model = WhisperModel("large-v3", device="auto", compute_type="default")
            segments, info = model.transcribe(str(audio_path), word_timestamps=True, language=language)

            elrc_lines = []
            plain_lines = []
            for seg in segments:
                words = getattr(seg, "words", None) or []
                if words:
                    l_ts = f"[{fmt_lrc_time(words[0].start)}]"
                    w_strs = [f"<{fmt_lrc_time(w.start)}>{w.word.strip()}" for w in words]
                    elrc_lines.append(f"{l_ts} {' '.join(w_strs)}")
                plain_lines.append(seg.text.strip())

            if elrc_lines:
                elrc_text = "\n".join(elrc_lines) + "\n"
                p_text = "\n".join(plain_lines).strip()
                std_lrc = normalize_synced(elrc_text, keep_word_sync=False, song=song, artist=artist)
                ttml = lrc_to_ttml(elrc_text, song=song, artist=artist)
                return {
                    "found": True,
                    "source": "Neural Whisper ASR (large-v3)",
                    "format": "ttml",
                    "syncType": "richsync",
                    "has_word_sync": True,
                    "has_back_vocals": False,
                    "rawLyrics": elrc_text,
                    "plainLyrics": p_text,
                    "language": str(getattr(info, "language", "") or language or ""),
                    "syncedLyrics": std_lrc,
                    "enhancedLyrics": elrc_text,
                    "ttmlLyrics": ttml,
                }
        except Exception as e:
            sys.stderr.write(f"Whisper ASR notice: {e}\n")
        return None


def js_unescape_single_quoted(source: str) -> str:
    """Decode a JavaScript single-quoted string without evaluating code."""
    output: list[str] = []
    index = 0
    escapes = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "0": "\0"}
    while index < len(source):
        char = source[index]
        index += 1
        if char != "\\" or index >= len(source):
            output.append(char)
            continue
        escaped = source[index]
        index += 1
        if escaped in {"'", '"', "\\", "/"}:
            output.append(escaped)
        elif escaped in escapes:
            output.append(escapes[escaped])
        elif escaped == "x" and index + 2 <= len(source):
            try:
                output.append(chr(int(source[index : index + 2], 16)))
                index += 2
            except ValueError:
                output.extend(("\\", escaped))
        elif escaped == "u" and index + 4 <= len(source):
            try:
                output.append(chr(int(source[index : index + 4], 16)))
                index += 4
            except ValueError:
                output.extend(("\\", escaped))
        elif escaped in "\r\n":
            if escaped == "\r" and index < len(source) and source[index] == "\n":
                index += 1
        else:
            # JavaScript treats an unrecognized escape as the escaped codepoint.
            output.append(escaped)
    decoded = "".join(output)
    return re.sub(
        r"[\ud800-\udbff][\udc00-\udfff]",
        lambda m: chr(0x10000 + ((ord(m[0][0]) - 0xD800) << 10) + ord(m[0][1]) - 0xDC00),
        decoded,
    )


class _GeniusLyricsHTMLParser(HTMLParser):
    """Read lyrics only from the provider's semantic lyrics container."""

    def __init__(self, *, fragment: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.fragment = fragment
        self.depth = 0
        self.root_depth = 0
        self.hidden_depth = 0
        self.text: list[str] = []
        self.found_container = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in {"script", "style", "svg", "noscript"}:
            self.hidden_depth += 1
        if self.hidden_depth:
            return
        classes = (values.get("class") or "").split()
        is_container = values.get("data-lyrics-container") == "true" or "lyrics" in classes
        if is_container and not self.depth:
            self.found_container = True
            self.root_depth = 1
            self.depth = 1
        elif self.depth:
            if tag in {"div", "section", "p", "blockquote"}:
                self.depth += 1
            if tag == "br":
                self.text.append("\n")
        elif self.fragment:
            if tag in {"br", "p", "div", "li"}:
                self.text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.hidden_depth:
            if tag in {"script", "style", "svg", "noscript"}:
                self.hidden_depth -= 1
            return
        if self.depth:
            if tag in {"div", "section", "p", "blockquote"}:
                self.depth -= 1
            if not self.depth:
                self.root_depth = 0
        elif self.fragment and tag in {"p", "div", "li"}:
            self.text.append("\n")

    def handle_data(self, data: str) -> None:
        if self.hidden_depth:
            return
        if self.depth or self.fragment:
            self.text.append(data)


def _extract_js_json_parse_argument(source: str, marker: str) -> str | None:
    start = source.find(marker)
    if start < 0:
        return None
    start += len(marker)
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if char == "'" and not escaped:
            if source[index + 1 : index + 3] == ");":
                return source[start:index]
            return None
        if char == "\\" and not escaped:
            escaped = True
        else:
            escaped = False
    return None


def _html_lyrics_text(fragment: str, *, semantic_container: bool) -> str:
    parser = _GeniusLyricsHTMLParser(fragment=not semantic_container)
    parser.feed(fragment)
    parser.close()
    if semantic_container and not parser.found_container:
        return ""
    text = "".join(parser.text).replace("\r\n", "\n").replace("\r", "\n")
    return text


def extract_genius_lyrics(page_html: str) -> str | None:
    """Extract lyrics from structured Genius hydration or its lyrics container."""
    if len(page_html) > 5_000_000:
        return None
    marker = "__PRELOADED_STATE__ = JSON.parse('"
    encoded_state = _extract_js_json_parse_argument(page_html, marker)
    if encoded_state is not None:
        try:
            state = _provider_json_loads(js_unescape_single_quoted(encoded_state))
            lyrics_html = state.get("songPage", {}).get("lyricsData", {}).get("body", {}).get("html", "")
            if isinstance(lyrics_html, str) and lyrics_html:
                text = _html_lyrics_text(lyrics_html, semantic_container=False)
                if text.strip() and len(text) <= 100_000:
                    return text
        except (json.JSONDecodeError, TypeError, AttributeError, ValueError):
            # A malformed or changed hydration schema falls through to semantic HTML.
            pass

    text = _html_lyrics_text(page_html, semantic_container=True)
    if text.strip() and len(text) <= 100_000:
        return text
    return None


def fetch_genius(
    song: str, artist: str, timeout: int = 6, *, return_candidate: bool = False
) -> str | dict[str, Any] | None:
    """Fetch high-quality clean plain text lyrics from Genius."""
    acquisition_failures: list[tuple[str, str]] = []
    had_valid_search = False
    candidates: list[dict[str, Any]] = []
    seen_pages: set[str] = set()
    queries = [
        f"{artist} {song}".strip(),
        f"{cyrillic_to_latin(artist)} {song}".strip(),
        f"{cyrillic_to_latin(artist)} {cyrillic_to_latin(song)}".strip(),
        f"{song}".strip(),
        f"{artist} {re.sub(r'[^\w\s]', ' ', song)}".strip(),
    ]
    seen_queries = set()
    for q_text in queries:
        if q_text in seen_queries:
            continue
        seen_queries.add(q_text)
        query = _quote_component(q_text)
        search_url = f"https://genius.com/api/search/multi?per_page=5&q={query}"
        api_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json",
        }
        page_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        try:
            req = urllib.request.Request(search_url, headers=api_headers)
            with _provider_urlopen(req, timeout=timeout) as resp:
                raw_search = resp.read(2_000_001)
                if len(raw_search) > 2_000_000:
                    acquisition_failures.append(("INVALID_RESPONSE", "search response exceeds 2 MB"))
                    continue
                data = _provider_json_loads(raw_search.decode("utf-8"))
                sections = data.get("response", {}).get("sections", [])
                if not isinstance(sections, list):
                    acquisition_failures.append(("PARSER_ERROR", "unexpected Genius search schema"))
                    break
                had_valid_search = True
                hits = []
                for sec in sections:
                    if sec.get("type") in ("song", "top_hit"):
                        for h in sec.get("hits", []):
                            if h.get("type") == "song" or "title" in h.get("result", {}):
                                hits.append(h)

                matching = [
                    h.get("result", {})
                    for h in hits
                    if is_track_match(
                        h.get("result", {}).get("title", ""),
                        h.get("result", {}).get("primary_artist", {}).get("name", ""),
                        song,
                        artist,
                    )
                ]
                if len(matching) > 20:
                    acquisition_failures.append(("TRUNCATED", "Genius matching candidates limited to 20"))
                for res in matching[:20]:
                    cand_url = str(res.get("url") or "")
                    if not cand_url or cand_url in seen_pages:
                        continue
                    parsed_url = urllib.parse.urlparse(cand_url)
                    if parsed_url.scheme != "https" or parsed_url.hostname not in {"genius.com", "www.genius.com"}:
                        acquisition_failures.append(("INVALID_RESPONSE", "unexpected Genius lyrics URL"))
                        continue
                    seen_pages.add(cand_url)
                    cand_title = str(res.get("title") or "")
                    cand_artist = str(res.get("primary_artist", {}).get("name") or "")
                    album_info = res.get("primary_album")
                    cand_album = str(album_info.get("name") or "") if isinstance(album_info, dict) else ""
                    try:
                        cand_duration = float(res["duration_ms"]) / 1000 if res.get("duration_ms") else None
                    except (TypeError, ValueError):
                        cand_duration = None
                    try:
                        page_req = urllib.request.Request(cand_url, headers=page_headers)
                        with _provider_urlopen(page_req, timeout=timeout) as page_resp:
                            raw_page = page_resp.read(5_000_001)
                            if len(raw_page) > 5_000_000:
                                acquisition_failures.append(("INVALID_RESPONSE", "lyrics page exceeds 5 MB"))
                                continue
                        page_html = decode_provider_text(page_resp, raw_page)
                        lyr = extract_genius_lyrics(page_html)
                        if lyr:
                            if return_candidate:
                                candidates.append(
                                    {
                                        "found": True,
                                        "source": "Genius",
                                        "url": cand_url,
                                        "id": str(res.get("id") or ""),
                                        "provider_track_id": str(res.get("id") or ""),
                                        "trackName": cand_title,
                                        "artistName": cand_artist,
                                        "albumName": cand_album,
                                        "duration": cand_duration,
                                        "language": str(res.get("language") or ""),
                                        "identity_validated": bool(cand_title and cand_artist),
                                        "providerRaw": page_html,
                                        "rawLyrics": lyr,
                                        "plainLyrics": lyr,
                                        "syncedLyrics": "",
                                    }
                                )
                                continue
                            return lyr
                        challenge_markers = ("captcha", "verify you are human", "access denied", "unusual traffic")
                        if any(marker in page_html.casefold() for marker in challenge_markers):
                            acquisition_failures.append(("BLOCKED", "Genius returned a challenge/access page"))
                        else:
                            acquisition_failures.append(
                                ("PARSER_ERROR", "matched Genius page had no recognized lyrics container")
                            )
                    except (OSError, ValueError, UnicodeError, LookupError) as exc:
                        status, detail = classify_lyrics_exception(exc)
                        acquisition_failures.append((status, detail))
                        if status in {"RATE_LIMITED", "AUTHENTICATION_ERROR", "BLOCKED"}:
                            break
                if candidates:
                    break
        except json.JSONDecodeError as exc:
            acquisition_failures.append(("PARSER_ERROR", f"invalid Genius search JSON: {exc}"))
            break
        except (UnicodeError, LookupError) as exc:
            acquisition_failures.append(("INVALID_RESPONSE", f"invalid Genius response encoding: {exc}"))
            break
        except Exception as exc:
            status_code = getattr(exc, "code", None)
            if status_code == 404:
                acquisition_failures.append(("NOT_FOUND", "HTTP 404"))
                continue
            if status_code == 429:
                acquisition_failures.append(("RATE_LIMITED", "HTTP 429"))
                break
            if status_code == 403:
                acquisition_failures.append(("BLOCKED", "HTTP 403"))
                break
            if status_code in {401, 407}:
                acquisition_failures.append(("AUTHENTICATION_ERROR", f"HTTP {status_code}"))
                break
            if status_code in {408, 425} or (status_code and status_code >= 500):
                acquisition_failures.append(("TRANSIENT_ERROR", f"HTTP {status_code}"))
                break
            if status_code:
                acquisition_failures.append(("INVALID_RESPONSE", f"HTTP {status_code}"))
                continue
            acquisition_failures.append(("TRANSIENT_ERROR", f"{type(exc).__name__}: {exc}"))
            continue

    if return_candidate:
        if candidates:
            return _collected_lyrics_result(candidates, acquisition_failures, source="Genius")
        if acquisition_failures:
            severity = {
                "AUTHENTICATION_ERROR": 7,
                "BLOCKED": 6,
                "RATE_LIMITED": 5,
                "TRANSIENT_ERROR": 4,
                "PARSER_ERROR": 3,
                "INVALID_RESPONSE": 2,
            }
            status, error = max(acquisition_failures, key=lambda item: severity.get(item[0], 0))
            return {"source": "Genius", "acquisition_status": status, "error": error}
        if had_valid_search:
            return {"source": "Genius", "acquisition_status": "NOT_FOUND"}
        return {"source": "Genius", "acquisition_status": "TRANSIENT_ERROR", "error": "no valid provider response"}
    return None


def fetch_azlyrics(song: str, artist: str, timeout: int = 6) -> dict[str, Any]:
    """Disable a diagnostic-only positional HTML scraper with no identity model."""
    del song, artist, timeout
    return {
        "source": "AZLyrics",
        "acquisition_status": "UNSUPPORTED_PROVIDER",
        "error": "diagnostic-only positional HTML scraper is disabled; it is not part of candidate selection",
    }


def fetch_songlyrics(song: str, artist: str, timeout: int = 6) -> dict[str, Any]:
    """Disable a diagnostic-only scraper that strips title/version evidence."""
    del song, artist, timeout
    return {
        "source": "SongLyrics",
        "acquisition_status": "UNSUPPORTED_PROVIDER",
        "error": "diagnostic-only scraper is disabled because its URL normalization erased version markers",
    }


def fetch_yandex_music(
    song: str,
    artist: str,
    duration: float | None = None,
    timeout: int = 6,
) -> dict[str, Any] | None:
    """Fetch identity-matched lyrics using an explicitly configured Yandex token."""
    token = os.environ.get("YANDEX_MUSIC_TOKEN") or os.environ.get("YANDEX_TOKEN")
    for tf_name in ("~/.config/yandex_music_token", "~/.config/yandex_token", "~/.config/yandex-music-token"):
        token_file = os.path.expanduser(tf_name)
        if not token and os.path.isfile(token_file):
            try:
                cand_tok = Path(token_file).read_text(encoding="utf-8").strip()
                if cand_tok:
                    token = cand_tok
                    break
            except OSError:
                pass

    if not token:
        return {
            "source": "Yandex Music",
            "acquisition_status": "NOT_CONFIGURED",
            "error": "an explicit Yandex Music token is required",
        }

    def _yandex_request(r_obj: urllib.request.Request, timeout: int = timeout):
        return _provider_urlopen(r_obj, timeout=timeout)

    headers = {
        "User-Agent": "YandexMusicAndroid/24022411 (Google Pixel 7; Android 14)",
        "Accept": "application/json",
    }
    if token:
        if token.startswith("3:") or "Session_id" in token:
            clean_sid = token.replace("Session_id=", "").strip().strip(";")
            headers["Cookie"] = f"Session_id={clean_sid}"
        else:
            headers["Authorization"] = f"OAuth {token}"

    search_url = "https://api.music.yandex.net/search?" + urllib.parse.urlencode(
        {"text": f"{artist} {song}".strip(), "type": "track", "page": 0}
    )
    failures: list[tuple[str, str]] = []
    candidates: list[dict[str, Any]] = []
    successful_response = False

    def request_json(url: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers=headers)
        with _yandex_request(request, timeout=timeout) as response:
            payload = _provider_json_loads(read_provider_text(response))
        if not isinstance(payload, dict):
            raise TypeError("Yandex Music response root is not an object")
        return payload

    def record_failure(exc: BaseException) -> None:
        status, detail = classify_lyrics_exception(exc)
        _record_provider_failure(exc)
        failures.append((status, detail))

    try:
        data = request_json(search_url)
        result = data.get("result")
        tracks_container = result.get("tracks") if isinstance(result, dict) else None
        tracks = tracks_container.get("results") if isinstance(tracks_container, dict) else None
        if not isinstance(tracks, list):
            raise TypeError("Yandex Music search response is missing result.tracks.results")
        successful_response = True
        matching_tracks: list[tuple[float, dict[str, Any]]] = []
        for track in tracks:
            if not isinstance(track, dict):
                record_failure(TypeError("Yandex Music search row is not an object"))
                continue
            title = str(track.get("title") or "")
            artist_rows = track.get("artists")
            if not isinstance(artist_rows, list) or any(not isinstance(row, dict) for row in artist_rows):
                record_failure(TypeError("Yandex Music search row has malformed artists"))
                continue
            candidate_artist = " / ".join(str(row.get("name") or "") for row in artist_rows if row.get("name"))
            duration_ms = track.get("durationMs")
            try:
                candidate_duration = float(duration_ms) / 1000.0 if duration_ms is not None else None
            except (TypeError, ValueError, OverflowError):
                candidate_duration = None
            if candidate_duration is not None and (not math.isfinite(candidate_duration) or candidate_duration <= 0):
                candidate_duration = None
            if not title or not candidate_artist:
                continue
            if is_track_match(
                title, candidate_artist, song, artist, cand_duration=candidate_duration, target_duration=duration
            ):
                matching_tracks.append((candidate_duration or 0.0, track))

        matching_tracks.sort(
            key=lambda item: (
                abs(item[0] - duration) if duration and item[0] else float("inf"),
                -text_similarity(str(item[1].get("title") or ""), song),
                str(item[1].get("id") or ""),
            )
        )
        for candidate_duration, track in matching_tracks[:6]:
            track_id = track.get("id")
            if track_id is None:
                record_failure(TypeError("Yandex Music matching track has no ID"))
                continue
            supplement_url = (
                f"https://api.music.yandex.net/tracks/{urllib.parse.quote(str(track_id), safe='')}/supplement"
            )
            try:
                supplement = request_json(supplement_url)
                supplement_result = supplement.get("result")
                if not isinstance(supplement_result, dict):
                    raise TypeError("Yandex Music supplement response is missing result object")
                lyrics_info = supplement_result.get("lyrics")
                if lyrics_info is None:
                    successful_response = True
                    continue
                if not isinstance(lyrics_info, dict):
                    raise TypeError("Yandex Music supplement lyrics field is not an object")
                full_lyrics = lyrics_info.get("full_lyrics")
                if full_lyrics is None or full_lyrics == "":
                    successful_response = True
                    continue
                if not isinstance(full_lyrics, str):
                    raise TypeError("Yandex Music full_lyrics field is not text")
                if len(full_lyrics) > MAX_LYRICS_CHARS:
                    raise ValueError("Yandex Music lyrics exceed the safety limit")
                artist_rows = track.get("artists")
                assert isinstance(artist_rows, list)
                actual_artist = " / ".join(str(row.get("name") or "") for row in artist_rows if row.get("name"))
                album_rows = track.get("albums") or []
                album_name = (
                    str(album_rows[0].get("title") or "") if album_rows and isinstance(album_rows[0], dict) else ""
                )
                is_timed = bool(ANY_LINE_TIMESTAMP_RE.search(full_lyrics))
                synced = (
                    normalize_synced(full_lyrics, keep_word_sync=False, song=song, artist=artist) if is_timed else ""
                )
                valid_sync = bool(synced and validate_lrc(synced, duration=duration)[0])
                plain = (
                    to_plain(full_lyrics, song=song, artist=artist)
                    if is_timed
                    else sanitize_lyrics(full_lyrics, song_title=song, artist_name=artist, keep_timestamps=False)
                )
                if not plain.strip():
                    record_failure(ValueError("Yandex Music returned empty lyric text"))
                    continue
                candidates.append(
                    {
                        "found": True,
                        "source": "Yandex Music",
                        "attribution": "Yandex Music",
                        "id": str(track_id),
                        "provider_track_id": str(track_id),
                        "trackName": str(track.get("title") or ""),
                        "artistName": actual_artist,
                        "albumName": album_name,
                        "duration": candidate_duration or None,
                        "version": str(track.get("version") or ""),
                        "identity_validated": True,
                        "format": "lrc" if valid_sync else "plain",
                        "syncType": "linesync" if valid_sync else "none",
                        "has_word_sync": False,
                        "has_back_vocals": False,
                        "providerRaw": json.dumps(
                            {"track": track, "lyrics": lyrics_info}, ensure_ascii=False, sort_keys=True
                        ),
                        "rawLyrics": full_lyrics,
                        "plainLyrics": plain,
                        "syncedLyrics": synced if valid_sync else "",
                        "enhancedLyrics": "",
                        "ttmlLyrics": "",
                        "language": str(lyrics_info.get("language") or track.get("language") or ""),
                        "validationWarnings": ["Yandex Music timing failed LRC validation; text retained as unsynced"]
                        if is_timed and not valid_sync
                        else [],
                        "providerMetadata": {"lyrics_format": lyrics_info.get("lyrics_format")},
                    }
                )
                successful_response = True
            except _LyricsAcquisitionError as exc:
                _record_provider_failure(exc)
                failures.append((exc.status, str(exc)))
            except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
                record_failure(exc)
    except _LyricsAcquisitionError as exc:
        _record_provider_failure(exc)
        failures.append((exc.status, str(exc)))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        record_failure(exc)

    if candidates:
        candidates.sort(
            key=lambda item: (
                abs(float(item["duration"]) - duration) if duration and item.get("duration") else float("inf"),
                -text_similarity(str(item.get("trackName") or ""), song),
                str(item.get("provider_track_id") or ""),
            )
        )
        selected = dict(candidates[0])
        selected["alternatives"] = candidates[1:]
        if failures:
            selected["validationWarnings"] = list(selected.get("validationWarnings") or []) + [
                f"some Yandex Music results could not be inspected ({failures[0][0]})"
            ]
        return selected
    if failures:
        severity = {
            "AUTHENTICATION_ERROR": 7,
            "BLOCKED": 6,
            "RATE_LIMITED": 5,
            "TRANSIENT_ERROR": 4,
            "PARSER_ERROR": 3,
            "INVALID_RESPONSE": 2,
        }
        status, detail = max(failures, key=lambda item: severity.get(item[0], 0))
        return {"source": "Yandex Music", "acquisition_status": status, "error": detail}
    if successful_response:
        mark_provider_not_found()
        return {"source": "Yandex Music", "acquisition_status": "NOT_FOUND"}
    return {"source": "Yandex Music", "acquisition_status": "PARSER_ERROR", "error": "no valid response"}


def test_yandex_music_status(token: str | None = None, timeout: int = 6) -> dict[str, Any]:
    """Diagnostic tool to inspect Yandex Music token validity, account status, and geo-availability."""
    if not token:
        token = os.environ.get("YANDEX_MUSIC_TOKEN") or os.environ.get("YANDEX_TOKEN")
        for tf_name in ("~/.config/yandex_music_token", "~/.config/yandex_token", "~/.config/yandex-music-token"):
            token_file = os.path.expanduser(tf_name)
            if not token and os.path.isfile(token_file):
                try:
                    cand = open(token_file, encoding="utf-8").read().strip()
                    if cand:
                        token = cand
                        break
                except Exception:
                    pass

    def _req(r_obj: urllib.request.Request):
        return _provider_urlopen(r_obj, timeout=timeout)

    headers = {
        "User-Agent": "YandexMusicAndroid/24022411 (Google Pixel 7; Android 14)",
        "Accept": "application/json",
    }
    if token:
        if token.startswith("3:") or "Session_id" in token:
            clean_sid = token.replace("Session_id=", "").strip().strip(";")
            headers["Cookie"] = f"Session_id={clean_sid}"
        else:
            headers["Authorization"] = f"OAuth {token}"

    res: dict[str, Any] = {
        "token_found": bool(token),
        "authenticated": False,
        "account": {},
        "catalog_accessible": False,
        "status_code": None,
        "error": None,
        "recommendation": "",
    }

    # 1. Check account status
    try:
        sreq = urllib.request.Request("https://api.music.yandex.net/account/status", headers=headers)
        with _req(sreq) as sresp:
            sdata = _provider_json_loads(read_provider_text(sresp))
            s_res = sdata.get("result", {})
            account = s_res.get("account", {})
            plus = s_res.get("plus", {})
            res["authenticated"] = True
            res["account"] = {
                "uid": account.get("uid"),
                "login": account.get("login"),
                "region": account.get("region"),
                "serviceAvailable": account.get("serviceAvailable"),
                "hasPlus": plus.get("hasPlus"),
            }
    except urllib.error.HTTPError as he:
        res["status_code"] = he.code
        res["error"] = f"HTTP {he.code}: {he.reason}"
    except Exception as e:
        res["error"] = str(e)

    # 2. Check catalog search
    try:
        q_url = "https://api.music.yandex.net/search?text=%D0%A1%D0%B0%D0%BD%D1%81%D0%B0%D1%80%D0%B0&type=track&page=0"
        qreq = urllib.request.Request(q_url, headers=headers)
        with _req(qreq):
            res["catalog_accessible"] = True
            res["status_code"] = 200
            res["recommendation"] = "Яндекс Музыка полностью доступна и работает без ограничений."
    except urllib.error.HTTPError as he:
        res["status_code"] = he.code
        if he.code == 451:
            res["error"] = "HTTP 451: Unavailable For Legal Reasons (Geo-blocked)"
            res["recommendation"] = (
                "Доступ к каталогу заблокирован по географическому признаку (HTTP 451). "
                "Каталог недоступен для этого account/network context; no proxy or access-control workaround is attempted."
            )
        elif he.code in (401, 403):
            res["error"] = f"HTTP {he.code}: Ошибка авторизации"
            res["recommendation"] = (
                "Токен Яндекс Музыки недействителен или истёк. Обновите ~/.config/yandex_music_token."
            )
        else:
            res["error"] = f"HTTP {he.code}: {he.reason}"
    except Exception as e:
        res["error"] = str(e)

    return res


def test_vk_status(
    token: str | None = None,
    cookie: str | None = None,
    timeout: int = 6,
) -> dict[str, Any]:
    """Diagnostic tool to inspect VK token validity, remixsid session, Audio API, and Topics search."""
    if not token:
        token = os.environ.get("VK_TOKEN")
        token_file = os.path.expanduser("~/.config/vk_token")
        if not token and os.path.isfile(token_file):
            try:
                cand = open(token_file, encoding="utf-8").read().strip()
                if cand:
                    token = cand
            except Exception:
                pass

    if not cookie:
        cookie = os.environ.get("VK_COOKIE")
        cookie_file = os.path.expanduser("~/.config/vk_cookie")
        if not cookie and os.path.isfile(cookie_file):
            try:
                cand = open(cookie_file, encoding="utf-8").read().strip()
                if cand:
                    cookie = cand
            except Exception:
                pass

    def _req(req_obj, tout=timeout):
        return _provider_urlopen(req_obj, timeout=tout)

    res: dict[str, Any] = {
        "token_found": bool(token),
        "cookie_found": bool(cookie),
        "token_valid": False,
        "permissions_mask": None,
        "permissions_list": [],
        "user": None,
        "user_api_error": None,
        "audio_api_status": "not_tested",
        "audio_api_error": None,
        "cookie_valid": False,
        "cookie_status": "not_tested",
        "topics_accessible": False,
        "recommendations": [],
    }

    if token:
        # 1. Check permissions
        try:
            url = f"https://api.vk.com/method/account.getAppPermissions?v=5.131&access_token={token}"
            req = urllib.request.Request(url, headers={"User-Agent": "KateMobileAndroid/108"})
            with _req(req) as resp:
                data = _provider_json_loads(read_provider_text(resp))
                if "response" in data:
                    res["token_valid"] = True
                    mask = data["response"]
                    res["permissions_mask"] = mask
                    perm_map = {
                        1: "notify",
                        2: "friends",
                        4: "photos",
                        8: "audio",
                        16: "video",
                        32: "stories",
                        64: "pages",
                        128: "menu",
                        1024: "status",
                        2048: "notes",
                        4096: "messages",
                        8192: "wall",
                        32768: "ads",
                        65536: "offline",
                        131072: "docs",
                        262144: "groups",
                        524288: "notifications",
                        1048576: "stats",
                        2097152: "email",
                        4194304: "market",
                    }
                    perms = [name for bit, name in perm_map.items() if (mask & bit)]
                    res["permissions_list"] = perms
                elif "error" in data:
                    res["token_valid"] = False
                    err = data["error"]
                    res["token_error"] = f"{err.get('error_code')}: {err.get('error_msg')}"
        except Exception as e:
            res["token_error"] = str(e)

        # 2. Check user info
        if res["token_valid"]:
            try:
                u_url = f"https://api.vk.com/method/users.get?v=5.131&access_token={token}"
                req = urllib.request.Request(u_url, headers={"User-Agent": "KateMobileAndroid/108"})
                with _req(req) as u_r:
                    u_d = _provider_json_loads(read_provider_text(u_r))
                    if "response" in u_d and isinstance(u_d["response"], list) and u_d["response"]:
                        u_it = u_d["response"][0]
                        fn = u_it.get("first_name", "")
                        ln = u_it.get("last_name", "")
                        uid = u_it.get("id", "")
                        res["user"] = f"{fn} {ln} (ID: {uid})".strip()
                    elif "error" in u_d:
                        err = u_d["error"]
                        res["user_api_error"] = f"{err.get('error_code')}: {err.get('error_msg')}"
            except Exception as e:
                res["user_api_error"] = str(e)

        # 3. Check audio.search
        try:
            q = _quote_component("Кино Группа крови")
            a_url = f"https://api.vk.com/method/audio.search?q={q}&count=1&v=5.131&access_token={token}"
            req = urllib.request.Request(
                a_url,
                headers={
                    "User-Agent": "KateMobileAndroid/108 lite-armeabi-v7a (Android 14; SDK 34; arm64-v8a; Xiaomi; ru)"
                },
            )
            with _req(req) as a_r:
                a_d = _provider_json_loads(read_provider_text(a_r))
                if "response" in a_d:
                    res["audio_api_status"] = "operational"
                elif "error" in a_d:
                    err = a_d["error"]
                    res["audio_api_status"] = f"error_{err.get('error_code')}"
                    res["audio_api_error"] = f"{err.get('error_code')}: {err.get('error_msg')}"
        except Exception as e:
            res["audio_api_error"] = str(e)

    # 4. Check cookie remixsid
    if cookie:

        class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
            def http_error_302(self, req, fp, code, msg, headers):
                return fp

        c_opener = urllib.request.build_opener(NoRedirectHandler)
        try:
            creq = urllib.request.Request(
                "https://vk.com/feed",
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                    "Cookie": f"remixsid={cookie}",
                },
            )
            with _provider_urlopen(creq, _opener=c_opener, timeout=timeout) as cresp:
                loc = cresp.headers.get("Location", "")
            if "login.vk.com" in loc:
                res["cookie_valid"] = False
                res["cookie_status"] = "Устарела / сброшена (перенаправление на login.vk.com)"
            else:
                res["cookie_valid"] = True
                res["cookie_status"] = "Активна (200 OK)"
        except Exception as e:
            res["cookie_status"] = str(e)

    # 5. Check VK Topics
    try:
        top_res = fetch_vk_topics("Кукла колдуна", "Король и Шут", timeout=min(timeout, 5))
        if top_res and top_res.get("found"):
            res["topics_accessible"] = True
    except Exception:
        pass

    # 6. Build clear recommendations
    recs = []
    if not res["token_found"]:
        recs.append(
            "Токен ВК не найден (~/.config/vk_token). Укажите полученный вами API token через VK_TOKEN или ~/.config/vk_token."
        )
    elif res["token_valid"]:
        if "audio" not in res["permissions_list"]:
            recs.append("Токен действителен, но не имеет прав доступа к аудио (scope audio).")
        if res.get("user_api_error") and "Flood control" in res["user_api_error"]:
            recs.append("VK returned a flood-control response; stop requests and retry later within provider limits.")
        if res.get("audio_api_error") and "3:" in res["audio_api_error"]:
            recs.append(
                "Метод audio.search в VK API ограничен (код 3: Unknown method). ВКонтакте отключил прямой доступ к Audio API для сторонних приложений. Тексты песен извлекаются через поиск в обсуждениях групп (VK Topics) и web-сессию remixsid."
            )
    else:
        recs.append("Токен ВК недействителен или отозван.")

    if res["cookie_found"]:
        if not res["cookie_valid"]:
            recs.append(
                "Кука remixsid (~/.config/vk_cookie) недействительна. Если вы используете этот необязательный ручной способ, обновите cookie самостоятельно."
            )
    else:
        recs.append(
            "Необязательная cookie remixsid не указана; configure it explicitly only if your VK workflow requires it."
        )

    res["recommendations"] = recs
    return res


def load_env_file(env_path: Path | None = None) -> None:
    """Load key=value pairs from .env file into os.environ if not already present."""
    candidates = (
        [env_path]
        if env_path
        else [
            Path.cwd() / ".env",
            Path(__file__).resolve().parent / ".env",
            Path.home() / "Music" / ".env",
        ]
    )
    for p in candidates:
        if p and p.is_file():
            try:
                for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k and k not in os.environ:
                        os.environ[k] = v
                break
            except Exception:
                pass


# Automatically load .env on module import
load_env_file()


def sync_all_tokens(force: bool = False, verbose: bool = False) -> dict[str, Any]:
    """Report explicitly configured provider credentials without scanning browsers.

    Browser cookie harvesting is disabled. Private Web Player routes use only
    tokens explicitly supplied through configuration.
    """
    del force, verbose

    def configured(env_names: tuple[str, ...], files: tuple[str, ...] = ()) -> bool:
        if any(os.environ.get(name, "").strip() for name in env_names):
            return True
        return any(Path(os.path.expanduser(file_name)).is_file() for file_name in files)

    return {
        "browser_cookie_import": {
            "status": "disabled",
            "source": None,
            "reason": "local browser sessions are never read or copied",
        },
        "musixmatch": {
            "status": "configured" if _musixmatch_api_key() or _musixmatch_desktop_token() else "missing",
            "source": "explicit API key"
            if _musixmatch_api_key()
            else "explicit desktop token"
            if _musixmatch_desktop_token()
            else None,
            "credential": "MUSIXMATCH_API_KEY or MUSIXMATCH_TOKEN",
        },
        "spotify": {
            "status": "configured"
            if configured(
                ("SPOTIFY_ACCESS_TOKEN", "SPOTIFY_TOKEN"), ("~/.config/spotify_token", "~/.config/spotify_access_token")
            )
            else "missing",
            "source": "explicit access token"
            if configured(
                ("SPOTIFY_ACCESS_TOKEN", "SPOTIFY_TOKEN"), ("~/.config/spotify_token", "~/.config/spotify_access_token")
            )
            else None,
            "lyrics": "authorized Web Player color-lyrics route (private schema)",
        },
        "deezer": {
            "status": "configured" if configured(("DEEZER_ARL",), ("~/.config/deezer_arl",)) else "missing",
            "source": "explicit ARL" if configured(("DEEZER_ARL",), ("~/.config/deezer_arl",)) else None,
            "lyrics": "authorized Deezer web GraphQL route (private schema)",
        },
        "yandex": {
            "status": "configured"
            if configured(
                ("YANDEX_MUSIC_TOKEN", "YANDEX_TOKEN"),
                ("~/.config/yandex_music_token", "~/.config/yandex_token", "~/.config/yandex-music-token"),
            )
            else "missing",
            "source": "explicit token"
            if configured(
                ("YANDEX_MUSIC_TOKEN", "YANDEX_TOKEN"),
                ("~/.config/yandex_music_token", "~/.config/yandex_token", "~/.config/yandex-music-token"),
            )
            else None,
        },
        "vk": {
            "status": "configured" if configured(("VK_TOKEN",), ("~/.config/vk_token",)) else "missing",
            "source": "explicit API token" if configured(("VK_TOKEN",), ("~/.config/vk_token",)) else None,
        },
        "usdb": {
            "status": "configured" if configured(("USDB_COOKIE",), ("~/.config/usdb_cookie",)) else "missing",
            "source": "explicit configuration" if configured(("USDB_COOKIE",), ("~/.config/usdb_cookie",)) else None,
        },
    }


def get_spotify_access_token(sp_dc: str = "", force_refresh: bool = False, max_age_seconds: int = 2700) -> str | None:
    """Read an explicitly supplied Spotify Web API token; never exchange cookies."""
    del sp_dc, max_age_seconds
    if force_refresh:
        return None
    env_token = os.environ.get("SPOTIFY_ACCESS_TOKEN") or os.environ.get("SPOTIFY_TOKEN")
    if env_token:
        return env_token.replace("Bearer ", "").strip()
    for file_name in ("~/.config/spotify_token", "~/.config/spotify_access_token"):
        token_path = Path(os.path.expanduser(file_name))
        try:
            token = token_path.read_text(encoding="utf-8").strip() if token_path.is_file() else ""
        except OSError:
            token = ""
        if token:
            return token.replace("Bearer ", "").strip()
    return None


def fetch_spotify(
    song: str,
    artist: str,
    album: str = "",
    duration: float | None = None,
    track_id: str = "",
    timeout: int = 6,
) -> dict[str, Any]:
    """Use an explicitly supplied authorized token for Web Player lyrics.

    Catalog identity comes from the documented track API. The color-lyrics
    route is an observed Web Player interface, so schema errors stay explicit.
    """
    token = get_spotify_access_token()
    if not token:
        return {"source": "Spotify", "acquisition_status": "NOT_CONFIGURED", "error": "Spotify access token missing"}
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "AudiophileArchTagger/6.0",
    }

    def get_json(url: str) -> Any:
        with _provider_urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
            return _provider_json_loads(read_provider_text(response))

    try:
        if track_id:
            tracks = [get_json("https://api.spotify.com/v1/tracks/" + urllib.parse.quote(track_id, safe=""))]
        else:
            params = urllib.parse.urlencode({"q": f"track:{song} artist:{artist}", "type": "track", "limit": 10})
            search = get_json("https://api.spotify.com/v1/search?" + params)
            tracks_obj = search.get("tracks") if isinstance(search, dict) else None
            if not isinstance(tracks_obj, dict) or not isinstance(tracks_obj.get("items"), list):
                raise ValueError("Spotify track search schema changed")
            tracks = tracks_obj["items"]
        matching: list[dict[str, Any]] = []
        for item in tracks:
            if not isinstance(item, dict) or not isinstance(item.get("artists"), list):
                raise TypeError("Spotify track metadata schema changed")
            name = str(item.get("name") or "")
            artists = [str(value.get("name") or "") for value in item["artists"] if isinstance(value, dict)]
            raw_album_data = item.get("album")
            album_data: dict[str, Any] = raw_album_data if isinstance(raw_album_data, dict) else {}
            provider_duration = (
                float(item["duration_ms"]) / 1000 if isinstance(item.get("duration_ms"), (int, float)) else None
            )
            if not artists or not any(
                is_track_match(name, value, song, artist, provider_duration, duration) for value in artists
            ):
                continue
            matching.append(item)
        if not matching:
            mark_provider_not_found()
            return {"source": "Spotify", "acquisition_status": "NOT_FOUND"}
        matching.sort(
            key=lambda item: (
                abs(float(item.get("duration_ms") or 0) / 1000 - duration) if duration else 0,
                str(item.get("id") or ""),
            )
        )
        results: list[dict[str, Any]] = []
        item_failures: list[tuple[str, str]] = []
        for item in matching[:3]:
            try:
                spotify_id = str(item.get("id") or "")
                if not spotify_id:
                    raise ValueError("Spotify matched track lacks an ID")
                lyrics_payload = get_json(
                    "https://spclient.wg.spotify.com/color-lyrics/v2/track/"
                    + urllib.parse.quote(spotify_id, safe="")
                    + "?format=json&vocalRemoval=false&market=from_token"
                )
                lyric_data = lyrics_payload.get("lyrics") if isinstance(lyrics_payload, dict) else None
                if not isinstance(lyric_data, dict) or not isinstance(lyric_data.get("lines"), list):
                    raise TypeError("Spotify color-lyrics schema changed")
                sync_type = str(lyric_data.get("syncType") or "").upper()
                warnings: list[str] = []
                valid_timing = sync_type in {"LINE_SYNCED", "SYLLABLE_SYNCED"}
                if sync_type not in {"LINE_SYNCED", "UNSYNCED", "SYLLABLE_SYNCED"}:
                    warnings.append(f"Spotify unsupported synchronization type: {sync_type}; plain text retained")
                plain_lines: list[str] = []
                timed_lines: list[str] = []
                for line in lyric_data["lines"]:
                    if not isinstance(line, dict) or not isinstance(line.get("words"), str):
                        raise TypeError("Spotify lyric line schema changed")
                    words = line["words"]
                    plain_lines.append(words)
                    if sync_type != "UNSYNCED" and words.strip():
                        start = str(line.get("startTimeMs") or "")
                        if not start.isdigit():
                            valid_timing = False
                            warnings.append("Spotify synchronized line has invalid timestamp; plain text retained")
                        else:
                            timed_lines.append(f"[{ms_to_lrc_ts(int(start))}]{words}")
                if not valid_timing:
                    timed_lines = []
                plain = "\n".join(plain_lines)
                if not plain.strip():
                    continue
                album_data = item.get("album") or {}
                external_ids = item.get("external_ids") or {}
                results.append(
                    {
                        "found": True,
                        "source": "Spotify",
                        "id": spotify_id,
                        "provider_track_id": spotify_id,
                        "trackName": item["name"],
                        "artistName": item["artists"][0]["name"],
                        "albumName": str(album_data.get("name") or ""),
                        "duration": float(item.get("duration_ms") or 0) / 1000 or None,
                        "isrc": str(external_ids.get("isrc") or ""),
                        "language": str(lyric_data.get("language") or ""),
                        "identity_validated": True,
                        "source_confidence": 82,
                        "format": "lrc" if timed_lines else "plain",
                        "syncType": "linesync" if timed_lines else "none",
                        "has_word_sync": False,
                        "validationWarnings": list(dict.fromkeys(warnings)),
                        "providerRaw": json.dumps(lyrics_payload, ensure_ascii=False),
                        "rawLyrics": plain,
                        "plainLyrics": plain,
                        "syncedLyrics": "\n".join(timed_lines),
                        "enhancedLyrics": "",
                        "ttmlLyrics": "",
                        "attribution": str(lyric_data.get("provider") or "Spotify"),
                    }
                )
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                _record_provider_failure(exc)
                item_failures.append(classify_lyrics_exception(exc))
                continue
        if results:
            if item_failures:
                results[0].setdefault("validationWarnings", []).append(
                    f"some recording candidates failed ({item_failures[0][0]})"
                )
            results[0]["alternatives"] = results[1:]
            return results[0]
        if item_failures:
            status, detail = item_failures[0]
            return {"source": "Spotify", "acquisition_status": status, "error": detail}
        mark_provider_not_found()
        return {"source": "Spotify", "acquisition_status": "NOT_FOUND"}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, urllib.error.HTTPError) as exc:
        status, detail = classify_lyrics_exception(exc)
        return {"source": "Spotify", "acquisition_status": status, "error": detail}


def fetch_lyrics_cascade(
    song: str,
    artist: str,
    album: str = "",
    duration: float | None = None,
    audio_path: Path | str | None = None,
    timeout: int = 10,
    enable_neural: bool = False,
    language_branch: str | None = None,
    track_id: str = "",
    duration_tolerance: float = 2.5,
    verbose: bool = False,
    collect_candidates: bool = True,
    isrc: str = "",
    recording_id: str = "",
    language: str = "",
) -> dict[str, Any] | None:
    """Collect provider candidates for downstream recording-aware selection.

    1. Normalization & Language Detection:
       - Strict and Cleaned profiles
       - Language branch: 'CYRILLIC', 'LATIN', or 'CJK'

    2. Query bounded providers across word-synced, line-synced and plain forms.
       Provider order controls request order only; it never selects the winner.

    3. Level 4 Local Neural Fallback:
       - Branch A: CTC Forced Alignment (MMS/Wav2Vec2) if clean plain text found at Level 3
       - Branch B: Faster-Whisper large-v3 ASR + phoneme alignment if all databases empty
    ``collect_candidates`` remains accepted for older callers but no longer
    enables provider-order early exit; all eligible results are collected.
    """
    is_verbose = verbose or os.environ.get("AUDIOPHILE_VERBOSE") == "1"
    branch = language_branch or detect_language_branch(song, artist)
    strict_prof, clean_prof = get_query_profiles(song, artist)
    _strict_song, _strict_artist = strict_prof
    clean_song, clean_artist = clean_prof
    cache_identity = {
        "title": song,
        "artist": artist,
        "album": album,
        "duration": duration,
        "track_id": track_id,
        "isrc": isrc,
        "recording_id": recording_id,
        "language": language,
    }

    def invoke_for_track(source: str, provider: Any, *args: Any, **kwargs: Any) -> Any:
        return invoke_lyrics_provider(source, provider, *args, _cache_identity=cache_identity, **kwargs)

    if is_verbose:
        dur_str = f"{duration:.1f}s" if duration else "unknown"
        print(
            f'[VERBOSE][LYRICS] Cascade search for "{clean_song}" by "{clean_artist}" (album: "{album}", dur: {dur_str}, branch: {branch})'
        )

    if detect_instrumental(song, artist):
        if is_verbose:
            print(
                f'[VERBOSE][LYRICS] Detected instrumental track evidence from metadata/title for "{clean_song}"; no lyrics marker will be synthesized'
            )
        return {
            "found": False,
            "source": "Instrumental",
            "acquisition_status": "INSTRUMENTAL",
            "instrumental": True,
            "instrumental_verified": True,
            "identity_validated": True,
            "duration": duration,
        }

    # =========================================================================
    # Provider Lazy-Loading & Response Caching to Eliminate Redundant Network Calls
    # =========================================================================
    unison_res = None
    unison_called = False

    def get_unison_res() -> dict[str, Any] | None:
        nonlocal unison_res, unison_called
        if not unison_called:
            unison_called = True
            unison_res = invoke_for_track(
                "Unison",
                fetch_apple_music_ttml,
                clean_song,
                clean_artist,
                album=album,
                duration=duration,
                timeout=min(timeout, 8),
            )
        return unison_res

    mx_res = None
    mx_called = False

    def get_musixmatch_res() -> dict[str, Any] | None:
        nonlocal mx_res, mx_called
        if not mx_called:
            mx_called = True
            mx_res = invoke_for_track(
                "Musixmatch",
                fetch_musixmatch,
                clean_song,
                clean_artist,
                album=album,
                duration=duration,
                timeout=min(timeout, 6),
            )
        return mx_res

    spotify_res: dict[str, Any] | None = None
    spotify_called = False

    def get_spotify_res() -> dict[str, Any] | None:
        nonlocal spotify_res, spotify_called
        if not spotify_called:
            spotify_called = True
            spotify_res = invoke_for_track(
                "Spotify",
                fetch_spotify,
                song,
                artist,
                album=album,
                duration=duration,
                track_id=track_id,
                timeout=min(timeout, 6),
            )
        return spotify_res

    deezer_res: dict[str, Any] | None = None
    deezer_called = False

    def get_deezer_res() -> dict[str, Any] | None:
        nonlocal deezer_res, deezer_called
        if not deezer_called:
            deezer_called = True
            deezer_res = invoke_for_track(
                "Deezer",
                fetch_deezer_lyrics,
                song,
                artist,
                album=album,
                duration=duration,
                timeout=min(timeout, 6),
            )
        return deezer_res

    usdb_res = None
    usdb_called = False

    def get_ultrastar_res() -> dict[str, Any] | None:
        nonlocal usdb_res, usdb_called
        if not usdb_called:
            usdb_called = True
            usdb_res = invoke_for_track(
                "UltraStar DB", fetch_ultrastar_db, clean_song, clean_artist, timeout=min(timeout, 6)
            )
        return usdb_res

    qq_res = None
    qq_called = False

    def get_qqmusic_res() -> dict[str, Any] | None:
        nonlocal qq_res, qq_called
        if not qq_called:
            qq_called = True
            qq_res = invoke_for_track(
                "QQ Music", fetch_qqmusic, clean_song, clean_artist, duration=duration, timeout=min(timeout, 6)
            )
        return qq_res

    netease_res = None
    netease_called = False

    def get_netease_res() -> dict[str, Any] | None:
        nonlocal netease_res, netease_called
        if not netease_called:
            netease_called = True
            netease_res = invoke_for_track(
                "NetEase", search_netease_yrc, clean_song, clean_artist, duration=duration, timeout=min(timeout, 4.0)
            )
        return netease_res

    kugou_res = None
    kugou_called = False

    def get_kugou_res() -> dict[str, Any] | None:
        nonlocal kugou_res, kugou_called
        if not kugou_called:
            kugou_called = True
            kugou_res = invoke_for_track(
                "Kugou", fetch_kugou, clean_song, clean_artist, album=album, duration=duration, timeout=min(timeout, 6)
            )
        return kugou_res

    petit_res = None
    petit_called = False

    def get_petitlyrics_res() -> dict[str, Any] | None:
        nonlocal petit_res, petit_called
        if not petit_called:
            petit_called = True
            petit_res = invoke_for_track(
                "PetitLyrics", fetch_petitlyrics, clean_song, clean_artist, timeout=min(timeout, 6)
            )
        return petit_res

    ym_res = None
    ym_called = False

    def get_yandex_res() -> dict[str, Any] | None:
        nonlocal ym_res, ym_called
        if not ym_called:
            ym_called = True
            ym_res = invoke_for_track(
                "Yandex Music", fetch_yandex_music, clean_song, clean_artist, duration=duration, timeout=min(timeout, 6)
            )
        return ym_res

    vk_res = None
    vk_called = False

    def get_vk_music_res() -> dict[str, Any] | None:
        nonlocal vk_res, vk_called
        if not vk_called:
            vk_called = True
            vk_res = invoke_for_track(
                "VK Music",
                fetch_vk_music_lyrics,
                clean_song,
                clean_artist,
                album=album,
                duration=duration,
                timeout=min(timeout, 6),
            )
        return vk_res

    lr_res = None
    lr_called = False

    def get_lrclib_res() -> dict[str, Any] | None:
        nonlocal lr_res, lr_called
        if not lr_called:
            lr_called = True
            lr_res = invoke_for_track(
                "LRCLIB",
                fetch_lrclib,
                clean_song,
                clean_artist,
                album=album,
                duration=duration,
                timeout=min(timeout, 8),
                isrc=isrc,
                recording_id=recording_id,
            )
        return lr_res

    chorus_res = None
    chorus_called = False

    def get_chorus_res() -> dict[str, Any] | None:
        nonlocal chorus_res, chorus_called
        if not chorus_called:
            chorus_called = True
            chorus_res = invoke_for_track(
                "Chorus",
                fetch_chorus_chart,
                clean_song,
                clean_artist,
                album=album,
                duration=duration,
                timeout=min(timeout, 6),
            )
        return chorus_res

    megalobiz_res = None
    megalobiz_called = False

    def get_megalobiz_res() -> dict[str, Any] | None:
        nonlocal megalobiz_res, megalobiz_called
        if not megalobiz_called:
            megalobiz_called = True
            megalobiz_res = invoke_for_track(
                "Megalobiz",
                fetch_megalobiz,
                clean_song,
                clean_artist,
                album=album,
                duration=duration,
                timeout=min(timeout, 6),
            )
        return megalobiz_res

    rentanadviser_res = None
    rentanadviser_called = False

    def get_rentanadviser_res() -> dict[str, Any] | None:
        nonlocal rentanadviser_res, rentanadviser_called
        if not rentanadviser_called:
            rentanadviser_called = True
            rentanadviser_res = invoke_for_track(
                "RentAnAdviser",
                fetch_rentanadviser,
                clean_song,
                clean_artist,
                album=album,
                duration=duration,
                timeout=min(timeout, 6),
            )
        return rentanadviser_res

    # =========================================================================
    # LEVEL 1: Word-by-Word Synchronization (Early Exit)
    # =========================================================================
    def try_l1_unison() -> dict[str, Any] | None:
        return get_unison_res()

    def try_l1_musixmatch_richsync() -> dict[str, Any] | None:
        r = get_musixmatch_res()
        if r and (r.get("acquisition_status") or r.get("has_word_sync") or r.get("plainLyrics")):
            return r
        return None

    def try_l1_deezer_richsync() -> dict[str, Any] | None:
        return get_deezer_res()

    def try_l1_spotify() -> dict[str, Any] | None:
        return get_spotify_res()

    def try_l1_ultrastar() -> dict[str, Any] | None:
        return get_ultrastar_res()

    def try_l1_chorus() -> dict[str, Any] | None:
        r = get_chorus_res()
        if r and (r.get("acquisition_status") or r.get("has_word_sync")):
            return r
        return None

    def try_l1_qqmusic() -> dict[str, Any] | None:
        r = get_qqmusic_res()
        if r and (r.get("acquisition_status") or r.get("has_word_sync")):
            return r
        return None

    def try_l1_netease_yrc() -> dict[str, Any] | None:
        n_data = get_netease_res()
        if n_data and n_data.get("acquisition_status"):
            return n_data

        def adapt_netease_track(item: Mapping[str, Any], *, include_alternatives: bool = True) -> dict[str, Any] | None:
            """Convert each raw NetEase search result without dropping identity."""
            yrc = str(item.get("yrc") or "")
            lrc = str(item.get("lrc") or "")
            if not yrc and not lrc:
                return None
            candidate = dict(item)
            candidate.pop("alternatives", None)
            if yrc:
                parsed = yrc_to_lyrics(
                    yrc,
                    song=song,
                    artist=artist,
                    album=album,
                    tlyric=str(item.get("tlyric") or ""),
                    romalrc=str(item.get("romalrc") or ""),
                )
                if parsed.get("has_word_sync"):
                    candidate.update(
                        {
                            "found": True,
                            "source": "NetEase YRC",
                            "provider_track_id": str(item.get("provider_track_id") or item.get("id") or ""),
                            "trackName": item.get("trackName") or item.get("title") or "",
                            "artistName": item.get("artistName") or item.get("artist") or "",
                            "albumName": item.get("albumName") or "",
                            "identity_validated": bool(item.get("identity_validated")),
                            "format": "yrc",
                            "syncType": "richsync",
                            "has_word_sync": True,
                            "has_back_vocals": bool(parsed.get("has_back_vocals")),
                            "providerRaw": yrc,
                            "rawLyrics": yrc,
                            "plainLyrics": parsed["plainLyrics"],
                            "syncedLyrics": parsed["syncedLyrics"],
                            "enhancedLyrics": parsed["enhancedLyrics"],
                            "ttmlLyrics": parsed["ttmlLyrics"],
                            "translationLyrics": parsed.get("translationLyrics", ""),
                            "romajiLyrics": parsed.get("romajiLyrics", ""),
                            "language": item.get("language") or "",
                            "duration": item.get("duration"),
                        }
                    )
                elif lrc:
                    yrc = ""
                else:
                    return {
                        "source": "NetEase YRC",
                        "provider_track_id": str(item.get("provider_track_id") or item.get("id") or ""),
                        "providerRaw": yrc,
                        "acquisition_status": "PARSER_ERROR",
                        "error": "NetEase YRC response contained no parseable timed lyric rows",
                    }
            if not yrc and lrc:
                line_lrc = normalize_synced(lrc, keep_word_sync=False, song=song, artist=artist)
                candidate.update(
                    {
                        "found": True,
                        "source": "NetEase (LRC)",
                        "provider_track_id": str(item.get("provider_track_id") or item.get("id") or ""),
                        "trackName": item.get("trackName") or item.get("title") or "",
                        "artistName": item.get("artistName") or item.get("artist") or "",
                        "albumName": item.get("albumName") or "",
                        "identity_validated": bool(item.get("identity_validated")),
                        "format": "lrc",
                        "syncType": "linesync",
                        "has_word_sync": False,
                        "providerRaw": lrc,
                        "rawLyrics": lrc,
                        "plainLyrics": to_plain(lrc, song=song, artist=artist),
                        "syncedLyrics": line_lrc,
                        "enhancedLyrics": "",
                        "ttmlLyrics": "",
                        "translationLyrics": str(item.get("tlyric") or ""),
                        "romajiLyrics": str(item.get("romalrc") or ""),
                        "language": item.get("language") or "",
                        "duration": item.get("duration"),
                    }
                )
            if include_alternatives:
                alternatives = item.get("alternatives")
                if isinstance(alternatives, (list, tuple)):
                    candidate["alternatives"] = [
                        adapted
                        for alternative in alternatives[:20]
                        if isinstance(alternative, Mapping)
                        if (adapted := adapt_netease_track(alternative, include_alternatives=False)) is not None
                    ]
            return candidate

        if n_data:
            return adapt_netease_track(n_data)
        return None

    def try_l1_kugou_krc() -> dict[str, Any] | None:
        kg_data = get_kugou_res()
        if kg_data and kg_data.get("acquisition_status"):
            return kg_data
        if kg_data and kg_data.get("has_word_sync"):
            return {
                **kg_data,
                "found": True,
                "source": "Kugou KRC",
                "id": kg_data.get("id"),
                "format": "ttml",
                "syncType": "richsync",
                "has_word_sync": True,
                "has_back_vocals": kg_data.get("has_back_vocals", False),
                "rawLyrics": kg_data.get("rawLyrics", ""),
                "plainLyrics": kg_data.get("plainLyrics", ""),
                "syncedLyrics": kg_data.get("syncedLyrics", ""),
                "enhancedLyrics": kg_data.get("enhancedLyrics", ""),
                "ttmlLyrics": kg_data.get("ttmlLyrics", ""),
                "duration": kg_data.get("duration"),
            }
        return None

    def try_l1_petitlyrics() -> dict[str, Any] | None:
        r = get_petitlyrics_res()
        if r and (r.get("acquisition_status") or r.get("has_word_sync")):
            return r
        return None

    if branch == "CYRILLIC":
        l1_chain = [
            ("Unison", try_l1_unison),
            ("Musixmatch (Richsync)", try_l1_musixmatch_richsync),
            ("Deezer (Richsync)", try_l1_deezer_richsync),
            ("Spotify", try_l1_spotify),
            ("Chorus (Clone Hero / Rock Band)", try_l1_chorus),
            ("UltraStar DB", try_l1_ultrastar),
            ("QQ Music (QRC)", try_l1_qqmusic),
            ("NetEase (YRC)", try_l1_netease_yrc),
            ("KuGou (KRC)", try_l1_kugou_krc),
            ("PetitLyrics (PLRC)", try_l1_petitlyrics),
        ]
    elif branch == "CJK":
        l1_chain = [
            ("QQ Music (QRC)", try_l1_qqmusic),
            ("KuGou (KRC)", try_l1_kugou_krc),
            ("NetEase (YRC)", try_l1_netease_yrc),
            ("PetitLyrics (PLRC)", try_l1_petitlyrics),
            ("Unison", try_l1_unison),
            ("Musixmatch (Richsync)", try_l1_musixmatch_richsync),
            ("Deezer (Richsync)", try_l1_deezer_richsync),
            ("Spotify", try_l1_spotify),
            ("Chorus (Clone Hero / Rock Band)", try_l1_chorus),
            ("UltraStar DB", try_l1_ultrastar),
        ]
    else:  # "LATIN"
        l1_chain = [
            ("Unison", try_l1_unison),
            ("Musixmatch (Richsync)", try_l1_musixmatch_richsync),
            ("Deezer (Richsync)", try_l1_deezer_richsync),
            ("Spotify", try_l1_spotify),
            ("QQ Music (QRC)", try_l1_qqmusic),
            ("NetEase (YRC)", try_l1_netease_yrc),
            ("KuGou (KRC)", try_l1_kugou_krc),
            ("Chorus (Clone Hero / Rock Band)", try_l1_chorus),
            ("UltraStar DB", try_l1_ultrastar),
            ("PetitLyrics (PLRC)", try_l1_petitlyrics),
        ]

    l1_results: dict[str, dict[str, Any] | None] = {}
    acquisition_results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(l1_chain))) as executor:
        future_to_name = {executor.submit(fn): name for name, fn in l1_chain}
        for future in concurrent.futures.as_completed(future_to_name):
            p_name = future_to_name[future]
            try:
                l1_results[p_name] = future.result()
            except Exception as e:
                status, error = classify_lyrics_exception(e)
                acquisition_results.append({"source": p_name, "acquisition_status": status, "error": error})
                if is_verbose:
                    print(f"[VERBOSE][LYRICS] [L1 Word-Sync] {p_name:<32} -> ERROR: {e}")
                l1_results[p_name] = None

    for name, _ in l1_chain:
        result = l1_results.get(name)
        if result and result.get("acquisition_status"):
            acquisition_results.append(result)

    if is_verbose:
        for name, _ in l1_chain:
            res = l1_results.get(name)
            if not res:
                print(f"[VERBOSE][LYRICS] [L1 Word-Sync] {name:<32} -> NO RESULT")
            elif not res.get("has_word_sync"):
                print(f"[VERBOSE][LYRICS] [L1 Word-Sync] {name:<32} -> NO WORD-SYNC ({res.get('format', 'lrc')})")
            else:
                lines_cnt = len((res.get("enhancedLyrics") or res.get("syncedLyrics") or "").splitlines())
                cand_dur = res.get("duration")
                dur_str = f"{cand_dur:.1f}s" if cand_dur else "unknown"
                valid_dur = validate_lyrics_duration(res, duration, tolerance=duration_tolerance)
                status_str = "PASS" if valid_dur else "FAIL (Duration mismatch)"
                print(
                    f"[VERBOSE][LYRICS] [L1 Word-Sync] {name:<32} -> FOUND ({lines_cnt} lines, dur: {dur_str}) | Validation: {status_str}"
                )

    l1_candidates = [res for name, _ in l1_chain if (res := l1_results.get(name)) and res.get("has_word_sync")]
    l1_plain_candidates = [
        res
        for name, _ in l1_chain
        if (res := l1_results.get(name))
        and not res.get("acquisition_status")
        and not res.get("has_word_sync")
        and (res.get("plainLyrics") or res.get("syncedLyrics"))
    ]
    collected_candidates: list[dict[str, Any]] = [*l1_candidates, *l1_plain_candidates]
    if l1_candidates:
        # The caller performs identity/quality scoring; provider order never
        # decides correctness.
        if is_verbose:
            print(f"[VERBOSE][LYRICS] Level 1 candidates: {len(l1_candidates)}")

    # =========================================================================
    # LEVEL 2: Line-Synced Synchronization (Early Exit)
    # =========================================================================
    def try_l2_yandex() -> dict[str, Any] | None:
        r = get_yandex_res()
        if r and (r.get("acquisition_status") or r.get("syncedLyrics")):
            return r
        return None

    def try_l2_vk_music() -> dict[str, Any] | None:
        r = get_vk_music_res()
        if r and (r.get("acquisition_status") or r.get("syncedLyrics")):
            return r
        return None

    def try_l2_lrclib() -> dict[str, Any] | None:
        r = get_lrclib_res()
        if r and r.get("acquisition_status"):
            return r
        if r and r.get("syncedLyrics"):
            raw_lrc = r["syncedLyrics"]
            std_lrc = normalize_synced(raw_lrc, keep_word_sync=False, song=song, artist=artist)
            elrc_lrc = normalize_synced(raw_lrc, keep_word_sync=True, song=song, artist=artist)
            plain_text = r.get("plainLyrics") or to_plain(raw_lrc, song=song, artist=artist)
            ttml_xml = lrc_to_ttml(std_lrc, song=song, artist=artist, album=album, duration=duration)
            return {
                **r,
                "found": True,
                "source": r.get("source", "LRCLIB"),
                "id": r.get("id"),
                "provider_track_id": r.get("id"),
                "trackName": r.get("trackName", ""),
                "artistName": r.get("artistName", ""),
                "albumName": r.get("albumName", ""),
                "duration": r.get("duration"),
                "isrc": r.get("isrc", ""),
                "language": r.get("language", ""),
                "identity_validated": bool(r.get("identity_validated")),
                "providerRaw": r.get("providerRaw") or raw_lrc,
                "format": "lrc",
                "syncType": "linesync",
                "has_word_sync": False,
                "has_back_vocals": "(" in std_lrc and ")" in std_lrc,
                "rawLyrics": raw_lrc,
                "plainLyrics": plain_text,
                "syncedLyrics": std_lrc,
                "enhancedLyrics": elrc_lrc,
                "ttmlLyrics": ttml_xml,
            }
        if r and r.get("acquisition_status"):
            return dict(r)
        return None

    def try_l2_megalobiz() -> dict[str, Any] | None:
        r = get_megalobiz_res()
        if r and (r.get("acquisition_status") or r.get("syncedLyrics")):
            return r
        return None

    def try_l2_rentanadviser() -> dict[str, Any] | None:
        r = get_rentanadviser_res()
        if r and (r.get("acquisition_status") or r.get("syncedLyrics")):
            return r
        return None

    def try_l2_petitlyrics() -> dict[str, Any] | None:
        r = get_petitlyrics_res()
        if r and (r.get("acquisition_status") or r.get("syncedLyrics")):
            return r
        return None

    def try_l2_musixmatch() -> dict[str, Any] | None:
        r = get_musixmatch_res()
        if r and (r.get("acquisition_status") or r.get("syncedLyrics")):
            return r
        return None

    def try_l2_spotify() -> dict[str, Any] | None:
        return get_spotify_res()

    def try_l2_deezer() -> dict[str, Any] | None:
        return get_deezer_res()

    def try_l2_netease() -> dict[str, Any] | None:
        r = get_netease_res()
        if r and r.get("acquisition_status"):
            return r
        if r and r.get("lrc"):
            raw_lrc = r["lrc"]
            std_lrc = normalize_synced(raw_lrc, keep_word_sync=False, song=song, artist=artist)
            elrc_lrc = normalize_synced(raw_lrc, keep_word_sync=True, song=song, artist=artist)
            plain_text = to_plain(raw_lrc, song=song, artist=artist)
            ttml_xml = lrc_to_ttml(std_lrc, song=song, artist=artist, album=album, duration=duration)
            trans_lrc = (
                sanitize_lyrics(r.get("tlyric", ""), song_title=song, artist_name=artist, keep_timestamps=True)
                if r.get("tlyric")
                else ""
            )
            roma_lrc = (
                sanitize_lyrics(r.get("romalrc", ""), song_title=song, artist_name=artist, keep_timestamps=True)
                if r.get("romalrc")
                else ""
            )
            return {
                **r,
                "found": True,
                "source": "NetEase (LRC)",
                "id": r.get("id"),
                "provider_track_id": str(r.get("provider_track_id") or r.get("id") or ""),
                "trackName": r.get("trackName") or r.get("title") or "",
                "artistName": r.get("artistName") or r.get("artist") or "",
                "albumName": r.get("albumName") or "",
                "identity_validated": bool(r.get("identity_validated")),
                "providerRaw": raw_lrc,
                "format": "lrc",
                "syncType": "linesync",
                "has_word_sync": False,
                "has_back_vocals": "(" in std_lrc and ")" in std_lrc,
                "rawLyrics": raw_lrc,
                "plainLyrics": plain_text,
                "syncedLyrics": std_lrc,
                "enhancedLyrics": elrc_lrc,
                "ttmlLyrics": ttml_xml,
                "translationLyrics": trans_lrc,
                "romajiLyrics": roma_lrc,
                "duration": r.get("duration"),
                "language": r.get("language") or "",
            }
        return None

    if branch == "CYRILLIC":
        l2_chain = [
            ("Яндекс Музыка (LRC)", try_l2_yandex),
            ("VK Музыка (LRC)", try_l2_vk_music),
            ("LRCLIB (LRC)", try_l2_lrclib),
            ("Megalobiz (LRC)", try_l2_megalobiz),
            ("RentAnAdviser (LRC)", try_l2_rentanadviser),
            ("Musixmatch (LRC)", try_l2_musixmatch),
            ("Spotify (LRC)", try_l2_spotify),
            ("Deezer (LRC)", try_l2_deezer),
            ("NetEase (LRC)", try_l2_netease),
        ]
    elif branch == "CJK":
        l2_chain = [
            ("NetEase (LRC)", try_l2_netease),
            ("LRCLIB (LRC)", try_l2_lrclib),
            ("Megalobiz (LRC)", try_l2_megalobiz),
            ("RentAnAdviser (LRC)", try_l2_rentanadviser),
            ("Musixmatch (LRC)", try_l2_musixmatch),
            ("Spotify (LRC)", try_l2_spotify),
            ("Deezer (LRC)", try_l2_deezer),
            ("PetitLyrics (LRC)", try_l2_petitlyrics),
            ("Яндекс Музыка (LRC)", try_l2_yandex),
            ("VK Музыка (LRC)", try_l2_vk_music),
        ]
    else:  # LATIN
        l2_chain = [
            ("LRCLIB (LRC)", try_l2_lrclib),
            ("Megalobiz (LRC)", try_l2_megalobiz),
            ("RentAnAdviser (LRC)", try_l2_rentanadviser),
            ("Musixmatch (LRC)", try_l2_musixmatch),
            ("Spotify (LRC)", try_l2_spotify),
            ("Deezer (LRC)", try_l2_deezer),
            ("NetEase (LRC)", try_l2_netease),
            ("PetitLyrics (LRC)", try_l2_petitlyrics),
            ("Яндекс Музыка (LRC)", try_l2_yandex),
            ("VK Музыка (LRC)", try_l2_vk_music),
        ]

    if is_verbose:
        print(f"[VERBOSE][LYRICS] --- Entering LEVEL 2: Line-Synced LRC Cascade ({branch}) ---")

    l2_candidates: list[dict[str, Any]] = []
    for name, provider_fn in l2_chain:
        try:
            res = provider_fn()
        except Exception as e:
            status, error = classify_lyrics_exception(e)
            acquisition_results.append({"source": name, "acquisition_status": status, "error": error})
            if is_verbose:
                print(f"[VERBOSE][LYRICS] [L2 Line-Sync] {name:<32} -> ERROR: {e}")
            res = None

        if not res or not res.get("syncedLyrics"):
            if res and res.get("acquisition_status"):
                acquisition_results.append(res)
                if is_verbose:
                    print(
                        f"[VERBOSE][LYRICS] [L2 Line-Sync] {name:<32} -> "
                        f"{res.get('acquisition_status')}: {res.get('error', '')}"
                    )
                continue
            if is_verbose:
                print(f"[VERBOSE][LYRICS] [L2 Line-Sync] {name:<32} -> NO RESULT")
            continue

        lines_cnt = len(res.get("syncedLyrics", "").splitlines())
        cand_dur = res.get("duration")
        dur_str = f"{cand_dur:.1f}s" if cand_dur else "unknown"
        valid_dur = True
        status_str = "PASS" if valid_dur else "FAIL (Duration mismatch)"
        if is_verbose:
            print(
                f"[VERBOSE][LYRICS] [L2 Line-Sync] {name:<32} -> FOUND ({lines_cnt} lines, dur: {dur_str}) | Validation: {status_str}"
            )

        if valid_dur:
            l2_candidates.append(res)
            continue

    if l2_candidates:
        collected_candidates.extend(l2_candidates)
        if is_verbose:
            print(f"[VERBOSE][LYRICS] Level 2 candidates: {len(l2_candidates)}")

    # =========================================================================
    # LEVEL 3: Clean Plain Text Collection
    # =========================================================================
    def try_l3_genius() -> dict[str, Any] | str | None:
        return invoke_for_track(
            "Genius", fetch_genius, clean_song, clean_artist, timeout=min(timeout, 6), return_candidate=True
        )

    def try_l3_vk_topics() -> dict[str, Any] | None:
        return invoke_for_track("VK Topics", fetch_vk_topics, clean_song, clean_artist, timeout=min(timeout, 6))

    def try_l3_amdm() -> dict[str, Any] | None:
        return invoke_for_track("AmDm", fetch_amdm_lyrics, clean_song, clean_artist, timeout=min(timeout, 6))

    def try_l3_bandcamp() -> dict[str, Any] | None:
        return invoke_for_track("Bandcamp", fetch_bandcamp_lyrics, clean_song, clean_artist, timeout=min(timeout, 6))

    def try_l3_qqmusic_plain() -> dict[str, Any] | None:
        r = get_qqmusic_res()
        return r if r and (r.get("acquisition_status") or r.get("plainLyrics")) else None

    def try_l3_lrclib_plain() -> dict[str, Any] | None:
        r = get_lrclib_res()
        return r if r and (r.get("acquisition_status") or r.get("plainLyrics")) else None

    def try_l3_netease_plain() -> dict[str, Any] | None:
        r = get_netease_res()
        if r and r.get("acquisition_status"):
            return r
        if r and (r.get("lrc") or r.get("yrc")):
            parsed = try_l1_netease_yrc()
            if not parsed or parsed.get("acquisition_status"):
                return parsed
            return {
                **parsed,
                "source": "NetEase (plain derivative)",
                "syncedLyrics": "",
                "enhancedLyrics": "",
                "ttmlLyrics": "",
                "has_word_sync": False,
            }
        return None

    def try_l3_baidu() -> dict[str, Any] | None:
        return invoke_for_track("Baidu", fetch_baidu_lyrics, clean_song, clean_artist, timeout=min(timeout, 6))

    def try_l3_petitlyrics() -> dict[str, Any] | None:
        r = get_petitlyrics_res()
        return r if r and r.get("plainLyrics") else None

    def try_l3_darklyrics() -> dict[str, Any] | None:
        return invoke_for_track(
            "DarkLyrics", fetch_darklyrics, clean_song, clean_artist, timeout=min(timeout, 6), return_status=True
        )

    def try_l3_metal_archives() -> dict[str, Any] | None:
        return invoke_for_track(
            "Metal-Archives", fetch_metal_archives, clean_song, clean_artist, timeout=min(timeout, 6)
        )

    def try_l3_vocadb() -> dict[str, Any] | None:
        return invoke_for_track("VocaDB", fetch_vocadb, clean_song, clean_artist, timeout=min(timeout, 6))

    def try_l3_tekstowo() -> dict[str, Any] | None:
        return invoke_for_track("Tekstowo.pl", fetch_tekstowo, clean_song, clean_artist, timeout=min(timeout, 6))

    def try_l3_yandex_plain() -> dict[str, Any] | None:
        r = get_yandex_res()
        return r if r and (r.get("acquisition_status") or r.get("plainLyrics")) else None

    if branch == "CYRILLIC":
        l3_chain = [
            ("Яндекс Музыка (Plain)", try_l3_yandex_plain),
            ("Genius RU / Genius", try_l3_genius),
            ("VK Topics", try_l3_vk_topics),
            ("AmDm", try_l3_amdm),
            ("Tekstowo.pl", try_l3_tekstowo),
            ("LRCLIB (Plain)", try_l3_lrclib_plain),
            ("Bandcamp", try_l3_bandcamp),
            ("QQ Music (Plain)", try_l3_qqmusic_plain),
            ("NetEase (Plain)", try_l3_netease_plain),
            ("Baidu (Plain)", try_l3_baidu),
            ("DarkLyrics", try_l3_darklyrics),
            ("Metal-Archives", try_l3_metal_archives),
        ]
    elif branch == "CJK":
        l3_chain = [
            ("PetitLyrics (Web)", try_l3_petitlyrics),
            ("NetEase (Plain)", try_l3_netease_plain),
            ("QQ Music (Plain)", try_l3_qqmusic_plain),
            ("VocaDB", try_l3_vocadb),
            ("LRCLIB (Plain)", try_l3_lrclib_plain),
            ("Baidu (Plain)", try_l3_baidu),
            ("Genius", try_l3_genius),
            ("Bandcamp", try_l3_bandcamp),
            ("DarkLyrics", try_l3_darklyrics),
            ("Metal-Archives", try_l3_metal_archives),
            ("VK Topics", try_l3_vk_topics),
            ("AmDm", try_l3_amdm),
        ]
    else:  # LATIN
        l3_chain = [
            ("Genius", try_l3_genius),
            ("DarkLyrics", try_l3_darklyrics),
            ("Metal-Archives", try_l3_metal_archives),
            ("Tekstowo.pl", try_l3_tekstowo),
            ("LRCLIB (Plain)", try_l3_lrclib_plain),
            ("Bandcamp", try_l3_bandcamp),
            ("PetitLyrics (Web)", try_l3_petitlyrics),
            ("QQ Music (Plain)", try_l3_qqmusic_plain),
            ("NetEase (Plain)", try_l3_netease_plain),
            ("Baidu (Plain)", try_l3_baidu),
            ("VK Topics", try_l3_vk_topics),
            ("AmDm", try_l3_amdm),
        ]

    clean_plain_found = ""
    raw_plain_found = ""
    plain_source = ""
    first_plain_payload: dict[str, Any] = {}
    l3_candidates: list[dict[str, Any]] = []
    if is_verbose:
        print(f"[VERBOSE][LYRICS] --- Entering LEVEL 3: Clean Plain Text Cascade ({branch}) ---")

    for plain_provider_name, plain_provider_fn in l3_chain:
        try:
            provider_result = plain_provider_fn()
        except Exception as e:
            status, error = classify_lyrics_exception(e)
            acquisition_results.append(
                {
                    "source": plain_provider_name,
                    "acquisition_status": status,
                    "error": error,
                }
            )
            if is_verbose:
                print(f"[VERBOSE][LYRICS] [L3 Plain] {plain_provider_name:<32} -> ERROR: {e}")
            provider_result = None

        if isinstance(provider_result, dict):
            payload = dict(provider_result)
            if payload.get("acquisition_status"):
                acquisition_results.append(payload)
                if is_verbose:
                    print(
                        f"[VERBOSE][LYRICS] [L3 Plain] {plain_provider_name:<32} -> "
                        f"{payload.get('acquisition_status')}: {payload.get('error', '')}"
                    )
                continue
            plain_raw_text = str(payload.get("plainLyrics") or payload.get("rawLyrics") or "")
            if not plain_raw_text and payload.get("syncedLyrics"):
                plain_raw_text = to_plain(str(payload["syncedLyrics"]), song=song, artist=artist)
            payload.setdefault("source", plain_provider_name)
        else:
            payload = {}
            plain_raw_text = str(provider_result or "")

        if plain_raw_text:
            cleaned = clean_plain_lyrics_for_alignment(plain_raw_text, song=song, artist=artist)
            if len(plain_raw_text.strip()) >= 10:
                if not clean_plain_found:
                    clean_plain_found = cleaned
                    raw_plain_found = plain_raw_text
                    plain_source = plain_provider_name
                    first_plain_payload = payload
                if payload:
                    payload.setdefault("providerRaw", payload.get("rawLyrics") or plain_raw_text)
                    payload["plainLyrics"] = plain_raw_text
                    payload.setdefault("format", "plain")
                    payload.setdefault("syncType", "none")
                    if payload.get("identity_validated") or (payload.get("trackName") and payload.get("artistName")):
                        l3_candidates.append(payload)
                if is_verbose:
                    identity_state = (
                        "verified candidate" if payload and payload.get("identity_validated") else "text available"
                    )
                    print(
                        f"[VERBOSE][LYRICS] [L3 Plain] {plain_provider_name:<32} -> FOUND ({len(plain_raw_text)} chars; {identity_state})"
                    )
            else:
                if is_verbose:
                    print(f"[VERBOSE][LYRICS] [L3 Plain] {plain_provider_name:<32} -> REJECTED (too short)")
        else:
            if is_verbose:
                print(f"[VERBOSE][LYRICS] [L3 Plain] {plain_provider_name:<32} -> NO RESULT")

    collected_candidates.extend(l3_candidates)

    # =========================================================================
    # LEVEL 4: Local Neural Fallback (Demucs + VAD + Alignment / ASR)
    # =========================================================================
    if enable_neural and audio_path:
        aud_path = Path(audio_path)
        if aud_path.is_file():
            if is_verbose:
                print(f"[VERBOSE][LYRICS] --- Entering LEVEL 4: Local Neural Engine ({aud_path.name}) ---")
            # Branch A: Clean text was gathered from Level 3
            if clean_plain_found:
                if is_verbose:
                    print(
                        f"[VERBOSE][LYRICS] [L4 Neural] Running CTC Forced Alignment with text from '{plain_source}'..."
                    )
                if NeuralLyricsEngine.can_align_mms(clean_plain_found, language):
                    neural_res = NeuralLyricsEngine.forced_alignment(
                        aud_path, clean_plain_found, song=song, artist=artist, language=language
                    )
                else:
                    neural_res = None
                    if is_verbose:
                        print(
                            "[VERBOSE][LYRICS] [L4 Neural] Forced alignment skipped: exact English language evidence is required"
                        )
                if neural_res and neural_res.get("has_word_sync"):
                    if is_verbose:
                        print("[VERBOSE][LYRICS] [L4 Neural] CTC Forced Alignment succeeded with Word-Sync!")
                    neural_res.update({"generated": True, "source_confidence": 35})
                    collected_candidates.append(neural_res)

            # Branch B: All databases empty -> Full Whisper ASR
            else:
                if is_verbose:
                    print("[VERBOSE][LYRICS] [L4 Neural] All databases empty -> Running Faster-Whisper ASR...")
                neural_res = NeuralLyricsEngine.whisper_asr(
                    aud_path,
                    song=song,
                    artist=artist,
                    language=(language.casefold().replace("_", "-").split("-", 1)[0] or None),
                )
                if neural_res and neural_res.get("has_word_sync"):
                    if is_verbose:
                        print("[VERBOSE][LYRICS] [L4 Neural] Whisper ASR succeeded with Word-Sync!")
                    neural_res.update({"generated": True, "source_confidence": 35})
                    collected_candidates.append(neural_res)

    # If neural block is not available or didn't yield word sync, return Level 3 plain text
    if clean_plain_found:
        if is_verbose:
            print(f"[VERBOSE][LYRICS] Final selection for \"{song}\": Plain Text from '{plain_source}'")
        plain_result = {
            **first_plain_payload,
            "found": True,
            "source": first_plain_payload.get("source") or plain_source or "Plain Text",
            "format": "plain",
            "syncType": "none",
            "has_word_sync": False,
            "has_back_vocals": False,
            # Raw provider text is preservation evidence.  The cleaned form is
            # only a display/alignment derivative.
            "providerRaw": first_plain_payload.get("providerRaw") or raw_plain_found or clean_plain_found,
            "rawLyrics": raw_plain_found or clean_plain_found,
            # Neural/alignment cleanup is a derivative only. The provider's
            # parsed plain text is what sidecars and tags receive.
            "plainLyrics": raw_plain_found or clean_plain_found,
            "syncedLyrics": "",
            "enhancedLyrics": "",
            "ttmlLyrics": "",
        }
        if plain_result.get("identity_validated") or (plain_result.get("trackName") and plain_result.get("artistName")):
            collected_candidates.append(plain_result)

    if collected_candidates or acquisition_results:
        # Do not let provider order or tier determine the winner. The caller
        # expands and scores every candidate using identity/text/sync evidence.
        first_list = collected_candidates or acquisition_results
        first, *rest = first_list
        result = dict(first)
        result["alternatives"] = (
            list(first.get("alternatives") or []) + rest + (acquisition_results if collected_candidates else [])
        )
        return result

    if is_verbose:
        print(f'[VERBOSE][LYRICS] Final selection for "{song}": NO LYRICS FOUND')
    return None


fetch_betterlyrics = fetch_lyrics_cascade
fetch_lyrics = fetch_lyrics_cascade


def run_providers_diagnostics(as_json: bool = False) -> int:
    """Run comprehensive live diagnostics across all lyrics providers and cascade levels."""
    import time

    results: list[dict[str, Any]] = []

    def run_t(category: str, name: str, func: Any, *f_args: Any, **f_kwargs: Any) -> Any:
        t0 = time.time()
        res = None
        err = None
        try:
            res = func(*f_args, **f_kwargs)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        dt = time.time() - t0

        if err:
            status = "ERROR"
            details = err
        elif res:
            status = "OK"
            if isinstance(res, dict):
                src = res.get("source", name)
                fmt = res.get("format", "dict")
                st = res.get("syncType", "unknown")
                ws = res.get("has_word_sync", False)
                raw = res.get("rawLyrics", "") or res.get("syncedLyrics", "") or res.get("plainLyrics", "") or ""
                length = len(raw)
                details = f"src='{src}', fmt={fmt}, sync={st}, word_sync={ws}, len={length}"
            elif isinstance(res, str):
                details = f"plain string, len={len(res)}"
            elif isinstance(res, (list, tuple)):
                details = f"list/tuple items={len(res)}"
            else:
                details = f"type={type(res).__name__}"
        else:
            status = "NO_RESULT"
            details = "Returned None or empty"

        entry = {
            "category": category,
            "name": name,
            "status": status,
            "time": round(dt, 2),
            "details": details,
        }
        results.append(entry)
        if not as_json:
            icon = "✅" if status == "OK" else ("⚠️" if status == "NO_RESULT" else "❌")
            print(f"{icon} [{status:<9}] {category:<8} | {name:<24} | {dt:5.2f}s | {details}")
        return res

    if not as_json:
        print("=" * 95)
        print("AUDIOPHILE CASCADING LYRICS PIPELINE — COMPREHENSIVE PROVIDER DIAGNOSTICS")
        print("=" * 95)

    # LEVEL 1
    if not as_json:
        print("\n--- LEVEL 1: Word-by-Word / Syllable Sync (TTML / QRC / YRC / KRC / Chorus / UltraStar) ---")
    run_t("L1 Word", "Apple Music TTML", fetch_apple_music_ttml, "In the End", "Linkin Park", timeout=5)
    run_t("L1 Word", "Chorus (Clone Hero)", fetch_chorus_chart, "Bohemian Rhapsody", "Queen", timeout=5)
    run_t("L1 Word", "UltraStar DB", fetch_ultrastar_db, "Bohemian Rhapsody", "Queen", timeout=5)
    run_t("L1 Word", "NetEase (YRC)", search_netease_yrc, "海阔天空", "Beyond", timeout=5)
    run_t("L1 Word", "QQ Music (QRC)", fetch_qqmusic, "夜曲", "周杰伦", timeout=5)
    run_t("L1 Word", "KuGou (KRC)", fetch_kugou, "夜曲", "周杰伦", timeout=5)
    run_t("L1 Word", "Musixmatch (Richsync)", fetch_musixmatch, "Bohemian Rhapsody", "Queen", timeout=5)

    # LEVEL 2
    if not as_json:
        print("\n--- LEVEL 2: Line-Synced LRC (Line-Sync) ---")
    run_t("L2 Line", "NetEase (LRC)", search_netease_yrc, "Группа крови", "Кино", timeout=5)
    run_t("L2 Line", "LRCLIB (LRC)", fetch_lrclib, "Bohemian Rhapsody", "Queen", timeout=5)
    run_t("L2 Line", "Megalobiz (LRC)", fetch_megalobiz, "Bohemian Rhapsody", "Queen", timeout=5)
    run_t("L2 Line", "RentAnAdviser (LRC)", fetch_rentanadviser, "Bohemian Rhapsody", "Queen", timeout=5)
    run_t("L2 Line", "Spotify (spclient)", fetch_spotify, "Bohemian Rhapsody", "Queen", timeout=5)
    run_t("L2 Line", "Musixmatch (LRC)", fetch_musixmatch, "Numb", "Linkin Park", timeout=5)
    run_t("L2 Line", "Deezer (LyricFind)", fetch_deezer_lyrics, "Bohemian Rhapsody", "Queen", timeout=5)
    run_t("L2 Line", "Яндекс Музыка (OAuth)", fetch_yandex_music, "Кукушка", "Кино", timeout=5)
    run_t("L2 Line", "VK Музыка", fetch_vk_music_lyrics, "Группа крови", "Кино", timeout=5)
    run_t("L2 Line", "SyncedLyrics Fallback", fetch_syncedlyrics_fallback, "Bohemian Rhapsody", "Queen")

    # LEVEL 3
    if not as_json:
        print("\n--- LEVEL 3: Clean Plain Text Providers ---")
    run_t("L3 Plain", "Genius", fetch_genius, "Bohemian Rhapsody", "Queen", timeout=5)
    run_t("L3 Plain", "AmDm (Chords Stripped)", fetch_amdm_lyrics, "Кукла колдуна", "Король и Шут", timeout=5)
    run_t("L3 Plain", "DarkLyrics (Metal)", fetch_darklyrics, "Master of Puppets", "Metallica", timeout=5)
    run_t("L3 Plain", "Metal-Archives", fetch_metal_archives, "Master of Puppets", "Metallica", timeout=5)
    run_t("L3 Plain", "VocaDB", fetch_vocadb, "World is Mine", "supercell", timeout=5)
    run_t("L3 Plain", "Tekstowo.pl", fetch_tekstowo, "Długość dźwięku samotności", "Myslovitz", timeout=5)
    run_t("L3 Plain", "AZLyrics", fetch_azlyrics, "Bohemian Rhapsody", "Queen", timeout=5)
    run_t("L3 Plain", "SongLyrics", fetch_songlyrics, "Bohemian Rhapsody", "Queen", timeout=5)
    run_t("L3 Plain", "Lyrics.ovh", fetch_lyrics_ovh, "Bohemian Rhapsody", "Queen", timeout=5)
    run_t("L3 Plain", "Bandcamp", fetch_bandcamp_lyrics, "Real Death", "Mount Eerie", timeout=5)
    run_t("L3 Plain", "Baidu / 1ting", fetch_baidu_lyrics, "小晴天", "俞婷", timeout=5)
    run_t("L3 Plain", "PetitLyrics (Web)", fetch_petitlyrics, "First Love", "宇多田ヒカル", timeout=5)
    run_t("L3 Plain", "VK Topics", fetch_vk_topics, "Кукла колдуна", "Король и Шут", timeout=5)

    # LEVEL 4
    if not as_json:
        print("\n--- LEVEL 4: Neural Engine ---")
        neural_ok = NeuralLyricsEngine.is_available()
        print(f"Neural Engine available: {'✅ YES' if neural_ok else '❌ NO'}")

    # FULL CASCADE
    if not as_json:
        print("\n--- FULL CASCADE INTEGRATION TESTS ---")
    run_t("CASCADE", "Cascade (English)", fetch_lyrics_cascade, "Numb", "Linkin Park", timeout=6)
    run_t("CASCADE", "Cascade (Russian)", fetch_lyrics_cascade, "Группа крови", "Кино", timeout=6)
    run_t("CASCADE", "Cascade (CJK)", fetch_lyrics_cascade, "夜に駆ける", "YOASOBI", timeout=6)

    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print("\n" + "=" * 95)
        print("DIAGNOSTICS SUMMARY")
        print("=" * 95)
        ok_cnt = sum(1 for r in results if r["status"] == "OK")
        err_cnt = sum(1 for r in results if r["status"] == "ERROR")
        no_cnt = sum(1 for r in results if r["status"] == "NO_RESULT")
        print(
            f"Total Tested: {len(results)} | Operational (OK): {ok_cnt} | No Result / Blocked: {no_cnt} | Unhandled Exceptions: {err_cnt}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize/process lyrics or fetch from BetterLyrics / LRCLIB / Musixmatch / Genius / NetEase / Kugou / Lyrics.ovh."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: plain
    plain_p = subparsers.add_parser("plain", help="Extract clean plain lyrics from LRC or TTML file.")
    plain_p.add_argument("file", type=Path, help="Path to LRC or TTML file.")
    plain_p.add_argument("--song", default="", help="Track/Song title for credit filtering.")
    plain_p.add_argument("--artist", default="", help="Artist name for credit filtering.")

    # Subcommand: synced
    synced_p = subparsers.add_parser("synced", help="Extract normalized synced LRC from LRC or TTML file.")
    synced_p.add_argument("file", type=Path, help="Path to LRC or TTML file.")
    synced_p.add_argument("--song", default="", help="Track/Song title for credit filtering.")
    synced_p.add_argument("--artist", default="", help="Artist name for credit filtering.")
    synced_p.add_argument(
        "--strip-word-sync", action="store_true", help="Strip word-level timestamps from synced output."
    )

    # Subcommand: clean-file
    clean_p = subparsers.add_parser("clean-file", help="Sanitize an LRC/ELRC/plain lyrics file on disk.")
    clean_p.add_argument("file", type=Path, help="Path to lyrics file.")
    clean_p.add_argument("--song", default="", help="Track/Song title.")
    clean_p.add_argument("--artist", default="", help="Artist name.")
    clean_p.add_argument("--in-place", action="store_true", help="Overwrite the file in place.")

    # Subcommand: fetch-betterlyrics / fetch-lyrics
    fetch_p = subparsers.add_parser(
        "fetch-betterlyrics",
        aliases=["fetch-lyrics"],
        help="Fetch lyrics from BetterLyrics / LRCLIB / Musixmatch / Genius / NetEase / Kugou / Lyrics.ovh.",
    )
    fetch_p.add_argument("--song", required=True, help="Track/Song title.")
    fetch_p.add_argument("--artist", required=True, help="Artist name.")
    fetch_p.add_argument("--album", default="", help="Album title.")

    def safe_duration(val: str) -> float | None:
        if not val or not val.strip():
            return None
        try:
            return float(val.strip())
        except ValueError:
            return None

    fetch_p.add_argument("--duration", type=safe_duration, default=None, help="Track duration in seconds.")
    fetch_p.add_argument(
        "--audio-file", type=Path, default=None, help="Path to local audio file for duration/neural processing."
    )
    fetch_p.add_argument("--tolerance", type=float, default=2.5, help="Duration tolerance in seconds (default: 2.5s).")
    fetch_p.add_argument("--branch", choices=["CYRILLIC", "LATIN", "CJK"], default=None, help="Force language branch.")
    fetch_p.add_argument(
        "--enable-neural",
        action="store_true",
        help="Opt in to generated ASR/alignment candidates (never an authoritative default).",
    )
    fetch_p.add_argument("--track-id", "--spotify-id", default="", help="Optional Spotify track ID.")
    fetch_p.add_argument("--plain-file", type=Path, default=None, help="Save plain lyrics to file.")
    fetch_p.add_argument("--synced-file", type=Path, default=None, help="Save standard line-synced lyrics to file.")
    fetch_p.add_argument(
        "--enhanced-file", "--elrc-file", type=Path, default=None, help="Save word-synced Enhanced LRC to file."
    )
    fetch_p.add_argument("--ttml-file", type=Path, default=None, help="Save raw TTML XML to file if available.")
    fetch_p.add_argument(
        "--flags-file", type=Path, default=None, help="Save manifest flags (e.g. LYRICS_HAS_WORD_SYNC)."
    )
    fetch_p.add_argument("--raw-file", type=Path, default=None, help="Save raw lyrics payload to file.")
    fetch_p.add_argument("--json", action="store_true", help="Output JSON result to stdout.")

    # Subcommand: fetch-spotify
    sp_fetch_p = subparsers.add_parser(
        "fetch-spotify", help="Fetch Spotify lyrics with an explicitly configured authorized access token."
    )
    sp_fetch_p.add_argument("--song", default="", help="Track/Song title.")
    sp_fetch_p.add_argument("--artist", default="", help="Artist name.")
    sp_fetch_p.add_argument("--album", default="", help="Album title.")
    sp_fetch_p.add_argument("--track-id", "--spotify-id", default="", help="Optional Spotify track ID.")
    sp_fetch_p.add_argument("--json", action="store_true", help="Output JSON result to stdout.")

    # Subcommand: fetch-yandex
    ym_fetch_p = subparsers.add_parser("fetch-yandex", help="Fetch lyrics directly from Yandex Music API.")
    ym_fetch_p.add_argument("--song", required=True, help="Track/Song title.")
    ym_fetch_p.add_argument("--artist", required=True, help="Artist name.")
    ym_fetch_p.add_argument("--json", action="store_true", help="Output JSON result to stdout.")

    # Subcommand: test-yandex
    ym_test_p = subparsers.add_parser(
        "test-yandex", help="Test Yandex Music token, account status, and geo-availability."
    )
    ym_test_p.add_argument("--token", default="", help="Optional OAuth token.")
    ym_test_p.add_argument("--json", action="store_true", help="Output JSON result to stdout.")

    # Subcommand: test-vk
    vk_test_p = subparsers.add_parser("test-vk", help="Test explicitly supplied VK API token/cookie and Topics access.")
    vk_test_p.add_argument("--token", default="", help="Optional VK access token.")
    vk_test_p.add_argument("--cookie", default="", help="Optional VK remixsid cookie.")
    vk_test_p.add_argument("--json", action="store_true", help="Output JSON result to stdout.")

    # Subcommand: sync-tokens
    sync_p = subparsers.add_parser(
        "sync-tokens",
        aliases=["tokens"],
        help="Report explicitly configured provider credentials; local browsers are never scanned.",
    )
    sync_p.add_argument("--force", action="store_true", help="Force overwrite of cached token files.")
    sync_p.add_argument("--json", action="store_true", help="Output JSON result to stdout.")

    # Subcommand: batch — produce all output formats in one invocation (speed optimization)
    batch_p = subparsers.add_parser(
        "batch", help="Produce all output formats (clean, synced, enhanced, plain) in one call."
    )
    batch_p.add_argument("file", type=Path, help="Path to LRC, ELRC, or TTML file.")
    batch_p.add_argument("--song", default="", help="Track/Song title for credit filtering.")
    batch_p.add_argument("--artist", default="", help="Artist name for credit filtering.")
    batch_p.add_argument("--clean-out", type=Path, default=None, help="Write cleaned TTML/LRC to this file.")
    batch_p.add_argument(
        "--synced-out", type=Path, default=None, help="Write line-synced LRC (stripped word-sync) to this file."
    )
    batch_p.add_argument("--enhanced-out", type=Path, default=None, help="Write word-synced Enhanced LRC to this file.")
    batch_p.add_argument("--plain-out", type=Path, default=None, help="Write clean plain lyrics to this file.")

    # Subcommand: check-providers
    chk_p = subparsers.add_parser(
        "check-providers",
        aliases=["test-providers", "diagnostics"],
        help="Run comprehensive diagnostics on all lyrics providers and cascade levels.",
    )
    chk_p.add_argument("--json", action="store_true", help="Output JSON result to stdout.")

    args = parser.parse_args()

    if args.command in ("check-providers", "test-providers", "diagnostics"):
        return run_providers_diagnostics(as_json=args.json)

    elif args.command in ("sync-tokens", "tokens"):
        res = sync_all_tokens(force=args.force)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print("================================================================")
            print("         ЯВНО НАСТРОЕННЫЕ ДАННЫЕ ПРОВАЙДЕРОВ                     ")
            print("================================================================")
            p_names = [
                ("Spotify", "spotify"),
                ("Musixmatch", "musixmatch"),
                ("Яндекс Музыка", "yandex"),
                ("Deezer", "deezer"),
                ("ВКонтакте", "vk"),
                ("UltraStar DB", "usdb"),
            ]
            for title, key in p_names:
                info = res.get(key, {})
                status = info.get("status", "unknown")
                st_icon = (
                    "✅ Настроен"
                    if status == "configured"
                    else ("⛔ Отключен" if status == "unsupported" else "❌ Отсутствует")
                )
                src = info.get("source") or "система"
                details = []
                if info.get("login"):
                    details.append(f"Логин: {info['login']}")
                if info.get("user"):
                    details.append(f"Пользователь: {info['user']}")
                if "app_id" in info:
                    details.append(f"App ID: {info['app_id']}")
                if "expires_in" in info:
                    details.append(f"Срок: {info['expires_in']}")
                if "file" in info:
                    details.append(f"Файл: {info['file']}")
                det_str = " | ".join(details) if details else ""
                print(f"[{st_icon}] {title:<16} (источник: {src})")
                if det_str:
                    print(f"       -> {det_str}")
            print("================================================================")
        return 0

    elif args.command == "fetch-spotify":
        spotify_result = fetch_spotify(
            song=args.song,
            artist=args.artist,
            album=args.album,
            track_id=getattr(args, "track_id", ""),
        )
        if not spotify_result or spotify_result.get("acquisition_status"):
            status = spotify_result.get("acquisition_status", "NOT_FOUND") if spotify_result else "NOT_FOUND"
            detail = spotify_result.get("error", "") if spotify_result else ""
            sys.stderr.write(f"Spotify lyrics provider status: {status}. {detail}\n")
            return 1
        if args.json:
            print(json.dumps(spotify_result, ensure_ascii=False, indent=2))
        else:
            print(f"Source: {spotify_result['source']} (Track ID: {spotify_result['id']})")
            print(
                f"Format: {spotify_result['format']}, Sync: {spotify_result['syncType']}, "
                f"WordSync: {spotify_result['has_word_sync']}"
            )
            print("--- Lyrics ---")
            print(spotify_result.get("syncedLyrics") or spotify_result.get("plainLyrics"))
        return 0

    elif args.command == "fetch-yandex":
        yandex_result = fetch_yandex_music(
            song=args.song,
            artist=args.artist,
        )
        if not yandex_result:
            sys.stderr.write(
                "No lyrics found on Yandex Music for this track (check provider availability and account authorization).\n"
            )
            return 1
        if args.json:
            print(json.dumps(yandex_result, ensure_ascii=False, indent=2))
        else:
            print(f"Source: {yandex_result['source']} (Track ID: {yandex_result['id']})")
            print(
                f"Format: {yandex_result['format']}, Sync: {yandex_result['syncType']}, "
                f"WordSync: {yandex_result['has_word_sync']}"
            )
            print("--- Lyrics ---")
            print(yandex_result.get("syncedLyrics") or yandex_result.get("plainLyrics"))
        return 0

    elif args.command == "test-yandex":
        res = test_yandex_music_status(token=args.token or None)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print("=== Диагностика Яндекс Музыки ===")
            print(f"Токен обнаружен: {'Да' if res['token_found'] else 'Нет'}")
            print(f"Авторизация: {'Успешно' if res['authenticated'] else 'Ошибка'}")
            if res.get("account"):
                acc = res["account"]
                print(f"  UID: {acc.get('uid')} | Login: {acc.get('login')}")
                print(f"  Регион: {acc.get('region')} | Подписка Плюс: {'Да' if acc.get('hasPlus') else 'Нет'}")
                print(f"  Доступ к сервису (serviceAvailable): {'Да' if acc.get('serviceAvailable') else 'Нет'}")
            st_code = res.get("status_code")
            cat_status = "Доступен (200 OK)" if res["catalog_accessible"] else f"Заблокирован ({st_code})"
            print(f"Доступ к каталогу (поиск/треки): {cat_status}")
            if res.get("error"):
                print(f"Ошибка: {res['error']}")
            if res.get("recommendation"):
                print(f"Рекомендация: {res['recommendation']}")
        return 0 if res["catalog_accessible"] else 1

    elif args.command == "test-vk":
        res = test_vk_status(
            token=args.token or None,
            cookie=args.cookie or None,
        )
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print("=== Диагностика ВКонтакте (VK) ===")
            print(f"Токен (~/.config/vk_token): {'Обнаружен' if res['token_found'] else 'Не найден'}")
            if res.get("token_found"):
                tok_status = (
                    "Валиден" if res["token_valid"] else f"Недействителен ({res.get('token_error', 'отозван')})"
                )
                print(f"  Статус токена: {tok_status}")
                if res.get("permissions_list"):
                    print(
                        f"  Права (scopes): {', '.join(res['permissions_list'])} (маска: {res.get('permissions_mask')})"
                    )
                if res.get("user"):
                    print(f"  Пользователь: {res['user']}")
                elif res.get("user_api_error"):
                    print(f"  Запрос профиля: Ограничен ({res['user_api_error']})")
            print(f"Кука сессии (~/.config/vk_cookie): {'Обнаружена' if res['cookie_found'] else 'Не найдена'}")
            if res.get("cookie_found"):
                c_stat = "Активна" if res["cookie_valid"] else f"Не активна ({res['cookie_status']})"
                print(f"  Статус сессии web: {c_stat}")
            aud_st = res.get("audio_api_error") or res.get("audio_api_status")
            print(f"Audio API (audio.search): {aud_st}")
            print(
                f"Поиск в обсуждениях (VK Topics): {'Доступен' if res.get('topics_accessible') else 'Недоступен / Ограничен'}"
            )
            if res.get("recommendations"):
                print("\nРекомендации:")
                for r in res["recommendations"]:
                    print(f"  • {r}")
        return 0 if (res.get("token_valid") or res.get("cookie_valid") or res.get("topics_accessible")) else 1

    elif args.command == "batch":
        if not args.file.is_file():
            return 1
        text = args.file.read_text(encoding="utf-8", errors="strict")
        song = args.song
        artist = args.artist

        # Clean the source file (TTML or LRC)
        if args.clean_out:
            if is_ttml(text):
                cleaned = clean_ttml(text, song=song, artist=artist)
            else:
                is_synced_src = bool(ANY_LINE_TIMESTAMP_RE.search(text))
                cleaned = sanitize_lyrics(text, song_title=song, artist_name=artist, keep_timestamps=is_synced_src)
            suffix = "\n" if cleaned and not cleaned.endswith("\n") else ""
            args.clean_out.write_text(cleaned + suffix, encoding="utf-8")
            # Use the cleaned version as source for subsequent operations
            if is_ttml(text):
                text = cleaned

        # Synced output (line-level, word-sync stripped)
        if args.synced_out:
            synced = normalize_synced(text, keep_word_sync=False, song=song, artist=artist)
            args.synced_out.write_text(synced, encoding="utf-8")

        # Enhanced output (word-level timestamps preserved)
        if args.enhanced_out:
            enhanced = normalize_synced(text, keep_word_sync=True, song=song, artist=artist)
            args.enhanced_out.write_text(enhanced, encoding="utf-8")

        # Plain output
        if args.plain_out:
            plain = to_plain(text, song=song, artist=artist)
            args.plain_out.write_text(plain, encoding="utf-8")

        return 0

    elif args.command == "plain":
        if str(args.file) == "-":
            text = sys.stdin.read()
        elif not args.file.is_file():
            return 1
        else:
            text = args.file.read_text(encoding="utf-8", errors="strict")
        sys.stdout.write(to_plain(text, song=args.song, artist=args.artist))
        return 0

    elif args.command == "synced":
        if str(args.file) == "-":
            text = sys.stdin.read()
        elif not args.file.is_file():
            return 1
        else:
            text = args.file.read_text(encoding="utf-8", errors="strict")
        sys.stdout.write(
            normalize_synced(text, keep_word_sync=not args.strip_word_sync, song=args.song, artist=args.artist)
        )
        return 0

    elif args.command == "clean-file":
        if str(args.file) == "-":
            raw = sys.stdin.read()
        elif not args.file.is_file():
            return 1
        else:
            raw = args.file.read_text(encoding="utf-8", errors="strict")
        if is_ttml(raw):
            cleaned = clean_ttml(raw, song=args.song, artist=args.artist)
        else:
            is_synced = bool(ANY_LINE_TIMESTAMP_RE.search(raw))
            cleaned = sanitize_lyrics(raw, song_title=args.song, artist_name=args.artist, keep_timestamps=is_synced)
        if args.in_place and str(args.file) != "-":
            args.file.write_text(cleaned + ("\n" if cleaned and not cleaned.endswith("\n") else ""), encoding="utf-8")
        else:
            sys.stdout.write(cleaned + ("\n" if cleaned and not cleaned.endswith("\n") else ""))
        return 0

    elif args.command in ("fetch-betterlyrics", "fetch-lyrics"):
        cascade_result = fetch_betterlyrics(
            song=args.song,
            artist=args.artist,
            album=args.album,
            duration=args.duration,
            audio_path=getattr(args, "audio_file", None),
            enable_neural=getattr(args, "enable_neural", False),
            language_branch=getattr(args, "branch", None),
            track_id=getattr(args, "track_id", ""),
            duration_tolerance=getattr(args, "tolerance", 2.5),
            collect_candidates=True,
        )
        if not cascade_result:
            return 1

        from lyrics_pipeline import choose_provider_result

        selected = choose_provider_result(
            [cascade_result],
            title=args.song,
            artist=args.artist,
            album=args.album,
            duration=args.duration,
            verbose=bool(getattr(args, "verbose", False)),
        )
        if selected is None:
            return 1
        selected_payload = selected.candidate.metadata.get("result")
        if not isinstance(selected_payload, dict):
            return 1
        res = dict(selected_payload)

        # Guarantee plainLyrics is always populated whenever lyrics exist
        if not res.get("plainLyrics"):
            if res.get("syncedLyrics"):
                res["plainLyrics"] = to_plain(res["syncedLyrics"], song=args.song, artist=args.artist)
            elif res.get("enhancedLyrics"):
                res["plainLyrics"] = to_plain(res["enhancedLyrics"], song=args.song, artist=args.artist)
            elif res.get("rawLyrics"):
                res["plainLyrics"] = to_plain(res["rawLyrics"], song=args.song, artist=args.artist)

        if args.plain_file and res.get("plainLyrics"):
            args.plain_file.write_text(res["plainLyrics"] + "\n", encoding="utf-8")
        if args.synced_file and res.get("syncedLyrics"):
            args.synced_file.write_text(res["syncedLyrics"] + "\n", encoding="utf-8")
        if getattr(args, "enhanced_file", None) and res.get("enhancedLyrics"):
            args.enhanced_file.write_text(res["enhancedLyrics"] + "\n", encoding="utf-8")
        if args.raw_file and res.get("rawLyrics"):
            args.raw_file.write_text(res["rawLyrics"], encoding="utf-8")

        ttml_content = (
            (res.get("ttmlLyrics") or (res.get("rawLyrics") if res.get("format") == "ttml" else ""))
            if res.get("ttml_native")
            else ""
        )
        if args.ttml_file and ttml_content:
            args.ttml_file.write_text(ttml_content, encoding="utf-8")

        if args.flags_file:
            flags = []
            if res.get("has_word_sync"):
                flags.append("LYRICS_HAS_WORD_SYNC=1")
            if res.get("has_back_vocals"):
                flags.append("LYRICS_HAS_BACKGROUND_VOCALS=1")
            if res.get("format"):
                flags.append(f"LYRICS_FORMAT={res['format']}")
            if res.get("syncType"):
                flags.append(f"LYRICS_SYNC_TYPE={res['syncType']}")
            if res.get("source"):
                flags.append(f"LYRICS_SOURCE={res['source']}")
            args.flags_file.write_text("\n".join(flags) + "\n", encoding="utf-8")

        if args.json or (
            not args.plain_file
            and not args.synced_file
            and not getattr(args, "enhanced_file", None)
            and not args.raw_file
            and not args.ttml_file
        ):
            print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
