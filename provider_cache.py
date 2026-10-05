#!/usr/bin/env python3
"""Versioned, expiring JSON cache and HTTP retry helpers."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol

import requests

from process_lock import file_lock, lock_directory


class JsonResponse(Protocol):
    status_code: int

    def json(self) -> object: ...


class JsonSession(Protocol):
    def get(self, url: str, *, headers: dict[str, str], timeout: float, stream: bool = False) -> JsonResponse: ...


SCHEMA_VERSION = 2
PARSER_VERSION = "2026-10-05-lyrics-v10"


def redact_secrets(message: str) -> str:
    """Strip common credential fields from URL/header exception diagnostics."""
    message = re.sub(r"(?i)(\b(?:Bearer|OAuth|Basic)\s+)\S+", r"\1[REDACTED]", message)
    return re.sub(
        r"(?i)(\b(?:access_token|api[_-]?key|apikey|token|client|password|secret|authorization|cookie)\s*[=:]\s*)[^\s&,'\"<>]+",
        r"\1[REDACTED]",
        message,
    )


@dataclass(frozen=True)
class ProviderResult:
    """Typed result for a cached/retried JSON provider request."""

    status: int
    payload: Any
    attempts: int
    cached: bool = False
    error: str = ""
    failure_classification: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and 200 <= self.status < 300 and isinstance(self.payload, (dict, list))

    @property
    def classification(self) -> str:
        """Acquisition semantics suitable for logs and negative-cache policy."""
        if self.failure_classification:
            return self.failure_classification
        if self.status == 404:
            return "NOT_FOUND"
        if self.status == 429:
            return "RATE_LIMITED"
        if self.status == 403:
            return "BLOCKED"
        if self.status == 451:
            return "BLOCKED"
        if self.status in {401, 407}:
            return "AUTHENTICATION_ERROR"
        if self.status in {408, 425, 500, 502, 503, 504} or self.status == 0:
            return "TRANSIENT_ERROR"
        if self.status == 200 and self.error:
            return "INVALID_RESPONSE" if self.error == "provider error response" else "PARSER_ERROR"
        if self.ok:
            return "FOUND"
        return "INVALID_RESPONSE"


class JsonCache:
    def __init__(
        self,
        directory: Path,
        *,
        ttl_seconds: float = 30 * 24 * 3600,
        negative_ttl_seconds: float = 6 * 3600,
    ) -> None:
        self.directory = Path(directory)
        self.ttl_seconds = ttl_seconds
        self.negative_ttl_seconds = negative_ttl_seconds

    def path_for(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.directory / f"{digest}.json"

    def load(self, key: str) -> tuple[int, Any] | None:
        path = self.path_for(key)
        try:
            stat = path.stat()
            if stat.st_size == 0 or stat.st_size > 10_100_000:
                return None
            with path.open("rb") as handle:
                raw = handle.read(10_100_001)
            if len(raw) > 10_100_000:
                return None
            obj = json.loads(raw)
        except (OSError, json.JSONDecodeError, UnicodeError):
            return None
        if isinstance(obj, dict) and ("cache_schema" in obj or "parser_version" in obj):
            if (
                obj.get("cache_schema") != SCHEMA_VERSION
                or obj.get("parser_version") != PARSER_VERSION
                or "payload" not in obj
                or obj.get("key_sha256") != hashlib.sha256(key.encode("utf-8")).hexdigest()
            ):
                return None
        if (
            isinstance(obj, dict)
            and obj.get("cache_schema") == SCHEMA_VERSION
            and obj.get("parser_version") == PARSER_VERSION
            and "payload" in obj
        ):
            try:
                status = int(obj.get("status", 200))
            except (TypeError, ValueError, OverflowError):
                return None
            if status not in {200, 204, 404}:
                return None
            if status == 404 and obj["payload"] is not None:
                return None
            if status == 204 and obj["payload"] != []:
                return None
            if status == 200 and not isinstance(obj["payload"], (dict, list)):
                return None
            ttl = self.negative_ttl_seconds if status in {204, 404} else self.ttl_seconds
            if time.time() - stat.st_mtime > ttl:
                return None
            return status, obj["payload"]
        # Raw legacy payloads have no parser version. Their apparent freshness
        # cannot establish that they were parsed under the current contract.
        return None

    def store(self, key: str, payload: Any, *, status: int = 200) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(key)
        envelope = {
            "cache_schema": SCHEMA_VERSION,
            "parser_version": PARSER_VERSION,
            "stored_at": datetime.now(timezone.utc).isoformat(),
            "key_sha256": hashlib.sha256(key.encode("utf-8")).hexdigest(),
            "status": int(status),
            "payload": payload,
        }
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(envelope, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return path

    def delete(self, key: str) -> None:
        """Invalidate a syntactically valid response that failed schema parsing."""
        try:
            self.path_for(key).unlink(missing_ok=True)
        except OSError:
            pass


def retry_delay(response: object | None, attempt: int, *, base: float = 1.0, maximum: float = 60.0) -> float:
    headers = getattr(response, "headers", {}) or {}
    retry_after = str(headers.get("Retry-After") or "").strip()
    if retry_after:
        try:
            # Retry-After is a provider instruction. Never cap it downward and
            # send a request early; callers with a shorter retry budget should
            # return the typed 429/5xx result without retrying yet.
            delay = float(retry_after)
            if not math.isfinite(delay):
                raise ValueError("non-finite Retry-After")
            return max(0.0, delay)
        except ValueError:
            try:
                when = parsedate_to_datetime(retry_after)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    # Deterministic exponential backoff; rate limiting already serializes calls.
    return min(maximum, base * (2 ** max(0, attempt - 1)))


def request_cache_key(url: str, headers: dict[str, str]) -> str:
    """Separate explicit credential/language contexts without retaining secrets."""
    context = {
        name.casefold(): value
        for name, value in headers.items()
        if name.casefold() in {"authorization", "cookie", "accept-language"}
        or name.casefold().endswith(("-token", "-key", "-api-version"))
    }
    if not context:
        return url
    digest = hashlib.sha256(json.dumps(context, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"{url}\nrequest-context:{digest}"


def session_cache_key(session: JsonSession, url: str, headers: dict[str, str]) -> str:
    """Use the headers/cookies/auth Requests will actually send to this URL."""
    if isinstance(session, requests.Session):
        prepared = session.prepare_request(requests.Request("GET", url, headers=headers))
        return request_cache_key(prepared.url or url, dict(prepared.headers))
    return request_cache_key(url, headers)


def error_envelope_classification(payload: dict[str, Any]) -> str:
    """Classify explicit error messages without guessing provider-specific codes."""
    error = payload.get("error")
    message = str(error.get("message") if isinstance(error, dict) else error or "").casefold()
    if any(
        phrase in message for phrase in ("invalid api key", "invalid token", "unauthorized", "authentication required")
    ):
        return "AUTHENTICATION_ERROR"
    if any(phrase in message for phrase in ("rate limit", "too many requests")):
        return "RATE_LIMITED"
    if any(phrase in message for phrase in ("temporarily unavailable", "service unavailable")):
        return "TRANSIENT_ERROR"
    return "INVALID_RESPONSE"


def fetch_json(
    session: JsonSession,
    url: str,
    *,
    headers: dict[str, str],
    cache: JsonCache,
    attempts: int = 3,
    timeout: float = 30,
    rate_limiter: object | None = None,
    sleeper: Any = time.sleep,
) -> ProviderResult:
    """Fetch JSON with shared cache, retry, negative-cache and error semantics."""
    cache_key = session_cache_key(session, url, headers)
    cached = cache.load(cache_key)
    if (
        cached is not None
        and isinstance(cached[1], dict)
        and (cached[1].get("status") == "error" or cached[1].get("error"))
    ):
        cache.delete(cache_key)
        cached = None
    if cached is not None:
        # 204 is an internal marker for a trustworthy empty search response;
        # callers still observe the provider's original HTTP 200 semantics.
        status = 200 if cached[0] == 204 else cached[0]
        return ProviderResult(status, cached[1], 0, cached=True)

    retryable = {408, 425, 429, 500, 502, 503, 504}
    last_status = 0
    last_error = "request exhausted"
    maximum_response_bytes = 10_000_000
    for attempt in range(1, max(1, attempts) + 1):
        if rate_limiter is not None:
            acquire = getattr(rate_limiter, "acquire", None)
            if callable(acquire):
                acquire()
        response: JsonResponse | None = None
        try:
            get = session.get
            try:
                parameters = inspect.signature(get).parameters.values()
                supports_stream = any(
                    parameter.name == "stream" or parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters
                )
            except (TypeError, ValueError):
                supports_stream = False
            request_kwargs: dict[str, Any] = {"headers": headers, "timeout": timeout}
            if supports_stream:
                request_kwargs["stream"] = True
            response = get(url, **request_kwargs)
            last_status = int(response.status_code)
            if last_status == 200:
                response_headers = getattr(response, "headers", {}) or {}
                content_length = response_headers.get("Content-Length")
                try:
                    if content_length is not None and int(content_length) > maximum_response_bytes:
                        return ProviderResult(200, None, attempt, error="response exceeds 10 MB safety limit")
                except (TypeError, ValueError):
                    pass
                try:
                    iter_content = getattr(response, "iter_content", None)
                    if callable(iter_content):
                        body = bytearray()
                        for chunk in iter_content(chunk_size=64 * 1024):
                            if not chunk:
                                continue
                            body.extend(chunk)
                            if len(body) > maximum_response_bytes:
                                return ProviderResult(200, None, attempt, error="response exceeds 10 MB safety limit")
                        payload = json.loads(body)
                    else:
                        content = getattr(response, "content", None)
                        if isinstance(content, (bytes, bytearray)) and len(content) > maximum_response_bytes:
                            return ProviderResult(200, None, attempt, error="response exceeds 10 MB safety limit")
                        payload = response.json()
                except (ValueError, UnicodeDecodeError) as exc:
                    return ProviderResult(200, None, attempt, error=f"invalid JSON: {exc}")
                else:
                    if isinstance(payload, dict) and (payload.get("status") == "error" or payload.get("error")):
                        return ProviderResult(
                            200,
                            payload,
                            attempt,
                            error="provider error response",
                            failure_classification=error_envelope_classification(payload),
                        )
                    if isinstance(payload, (dict, list)):
                        vary = {
                            name.strip().casefold()
                            for name in str(response_headers.get("Vary", "")).split(",")
                            if name.strip()
                        }
                        # Unknown representation dimensions cannot safely share
                        # the current key. Return the data but do not persist it.
                        if vary <= {"authorization", "cookie", "accept-language", "accept-encoding"}:
                            try:
                                cache.store(cache_key, payload, status=204 if payload == [] else 200)
                            except OSError:
                                pass
                        return ProviderResult(200, payload, attempt)
                    return ProviderResult(
                        200,
                        None,
                        attempt,
                        error=f"unexpected JSON type {type(payload).__name__}",
                    )
            elif last_status == 404:
                try:
                    cache.store(cache_key, None, status=404)
                except OSError:
                    pass
                return ProviderResult(404, None, attempt, error="not found")
            elif last_status in retryable:
                last_error = f"HTTP {last_status}"
            else:
                return ProviderResult(last_status, None, attempt, error=f"HTTP {last_status}")
        except requests.RequestException as exc:
            last_error = redact_secrets(f"{type(exc).__name__}: {exc}")
            last_status = 0
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        if attempt < attempts:
            delay = retry_delay(response, attempt)
            retry_after = str((getattr(response, "headers", {}) or {}).get("Retry-After") or "").strip()
            # Keep provider requests bounded and never retry before the
            # specified Retry-After. A later user invocation can try again.
            retry_budget_seconds = 2.0
            if retry_after and delay > retry_budget_seconds:
                return ProviderResult(
                    last_status, None, attempt, error=f"{last_error}; Retry-After exceeds retry budget"
                )
            if delay > retry_budget_seconds:
                return ProviderResult(
                    last_status, None, attempt, error=f"{last_error}; retry delay exceeds retry budget"
                )
            sleeper(delay)
    return ProviderResult(last_status, None, max(1, attempts), error=last_error)


class IntervalRateLimiter:
    """Thread-safe minimum spacing between calls to one documented API."""

    def __init__(self, interval_seconds: float, *, shared_name: str = "") -> None:
        self.shared_name = shared_name
        self.interval_seconds = max(0.0, interval_seconds)
        self._lock = threading.Lock()
        self._last = 0.0

    def acquire(self) -> None:
        with self._lock:
            if self.shared_name:
                key = hashlib.sha256(self.shared_name.encode()).hexdigest()
                state = lock_directory() / f"rate-{key}"
                with file_lock(state):
                    descriptor = os.open(state, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                    try:
                        raw = os.read(descriptor, 128)
                        try:
                            last = float(raw) if raw else 0.0
                        except ValueError:
                            last = 0.0
                        # Wall-clock jumps/reboots cannot impose an unbounded wait.
                        elapsed = time.time() - last if math.isfinite(last) else self.interval_seconds
                        wait = min(self.interval_seconds, max(0.0, self.interval_seconds - elapsed))
                        if wait:
                            time.sleep(wait)
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        os.ftruncate(descriptor, 0)
                        os.write(descriptor, repr(time.time()).encode("ascii"))
                    finally:
                        os.close(descriptor)
                return
            wait = self.interval_seconds - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()


# Discovery and enrichment must share host pacing within one application.
MUSICBRAINZ_LIMITER = IntervalRateLimiter(1.1, shared_name="musicbrainz.org")
DISCOGS_LIMITER = IntervalRateLimiter(1.0, shared_name="api.discogs.com")
