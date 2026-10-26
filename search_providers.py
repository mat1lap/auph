"""Replaceable Jackett discovery and separately authorized topic-detail acquisition."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field as datafield
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from pipeline_config import SearchPolicy
from provider_cache import IntervalRateLimiter, JsonCache, retry_delay
from search_models import Candidate, Query, SearchError, claim_identity, public_evidence_url, public_text, secret_values


class HTTPError(SearchError):
    def __init__(self, status: int, kind: str, diagnostics: dict[str, Any] | None = None) -> None:
        self.status = status
        self.diagnostics = diagnostics or {}
        # Structured diagnostics are exported separately by the detail caller.
        super().__init__(kind)


def diagnostic_reflections(body: object) -> tuple[str, ...]:
    """Gather request text and individual query/candidate values for redaction."""
    values: set[str] = set()

    def gather(value: object) -> None:
        if isinstance(value, str) and len(value) >= 2:
            values.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                gather(item)
        elif isinstance(value, list):
            for item in value:
                gather(item)

    if isinstance(body, dict):
        for key in ("systemInstruction", "contents", "messages"):
            gather(body.get(key))
        for text in tuple(values):
            try:
                parsed = json.loads(text)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                gather(parsed.get("query"))
                gather(parsed.get("candidates"))
    return tuple(sorted(values, key=len, reverse=True))


def safe_provider_violations(
    error: dict[str, Any], secrets: tuple[str, ...], reflections: tuple[str, ...] = ()
) -> list[dict[str, str]]:
    """Bounded, meaning-preserving diagnostics with targeted redaction, not a word allowlist."""

    def clean(value: object, limit: int) -> str:
        if not isinstance(value, str):
            return ""
        for reflected in reflections:
            value = value.replace(reflected, "[REDACTED input]")
            value = value.replace(json.dumps(reflected)[1:-1], "[REDACTED input]")
        value = public_text(value, secrets)
        value = re.sub(
            r"([\"'])(.*?)\1",
            lambda m: m[0] if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9.\[\]]*", m[2]) else "[REDACTED quoted value]",
            value,
        )
        value = re.sub(
            r"(?im)\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*[:=]\s*[^\r\n]+",
            "[REDACTED credentials]",
            value,
        )
        value = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[REDACTED email]", value)
        value = re.sub(r"(?:/home/|/Users/)[^\s\"']+", "[REDACTED path]", value)
        return value[:limit]

    def path(value: object) -> str:
        field = clean(value, 300)
        return field if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9.\[\]]{0,299}", field) else ""

    result: list[dict[str, str]] = []
    details = error.get("details", [])
    if isinstance(details, list):
        for detail in details[:10]:
            if not isinstance(detail, dict) or detail.get("@type") != "type.googleapis.com/google.rpc.BadRequest":
                continue
            violations = detail.get("fieldViolations", [])
            if isinstance(violations, list):
                for violation in violations[:10]:
                    if isinstance(violation, dict) and (field := path(violation.get("field"))):
                        result.append({"field": field, "description": clean(violation.get("description"), 350)})
    message = error.get("message")
    if isinstance(message, str):
        for match in re.finditer(r'Unknown name "([^"]+)" at \'([^\']+)\': Cannot find field', message[:4000]):
            name, parent = path(match[1]), path(match[2])
            if name and parent:
                result.append({"field": parent + "." + name, "description": "Unknown request field"})
        if not result and re.search(
            r"\b(?:schema|parameter|generationConfig|enum|nesting|field|unsupported|invalid|exceeds|mutually|maximum|minimum)\b",
            message,
            re.IGNORECASE,
        ):
            result.append({"field": "request", "description": clean(message, 350)})
    return list({entry["field"]: entry for entry in result}.values())[:10]


def safe_location(url: str) -> str:
    """Only host/path, never userinfo, query, fragment or authenticated headers."""
    try:
        parsed = urlsplit(url)
        return public_text((parsed.hostname or "") + parsed.path)
    except ValueError:
        return "invalid URL"


def page_evidence(raw: bytes, headers: Any) -> list[str]:
    text = raw[:64_000].lower()
    evidence = []
    if str(headers.get("cf-mitigated", "")).lower() == "challenge":
        evidence.append("cf-mitigated: challenge")
    # Torznab titles are data, not HTML document titles. Adding request timing
    # diagnostics must not make a legitimate "Just a moment" release BLOCKED.
    if re.match(rb"(?:<\?xml[^>]*>\s*)?<rss(?:\s|>)", text.lstrip()):
        return evidence
    for marker in (b"cf-chl-", b"/cdn-cgi/challenge-platform/", b"<title>just a moment", b"ddos-guard"):
        if marker in text and b"post_body" not in text:
            evidence.append(marker.decode("ascii"))
    return evidence


def origin(url: str) -> tuple[str, str, int | None]:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise SearchError("Endpoint must be an explicit HTTP(S) URL without userinfo or fragments")
        port = parsed.port if parsed.port is not None else 443 if parsed.scheme == "https" else 80
        if not 1 <= port <= 65535:
            raise ValueError("port")
        return parsed.scheme, parsed.hostname.lower(), port
    except ValueError:
        raise SearchError("Invalid endpoint host/port") from None


def endpoint(url: str) -> str:
    origin(url)
    if re.search(r"[\x00-\x20\x7f]", url):
        raise SearchError("Endpoint contains unsafe whitespace/control characters")
    if urlsplit(url).query:
        raise SearchError("Configure endpoint credentials in environment variables, not URL queries")
    return url.rstrip("/")


class HTTP:
    def __init__(self, timeout: float = 15, session: requests.Session | None = None) -> None:
        self.timeout = timeout
        self.session = session or requests.Session()

    def request(
        self,
        method: str,
        url: str,
        *,
        retry: bool = True,
        max_bytes: int = 2_000_000,
        magnet_redirect: bool = False,
        diagnostics: dict[str, Any] | None = None,
        diagnostics_secrets: tuple[str, ...] = (),
        accepted_statuses: frozenset[int] = frozenset(),
        public_redirect_domains: tuple[str, ...] = (),
        request_timeout: float | None = None,
        **kwargs: Any,
    ) -> bytes:
        started = time.monotonic()
        try:
            return self._request(
                method,
                url,
                retry=retry,
                max_bytes=max_bytes,
                magnet_redirect=magnet_redirect,
                diagnostics=diagnostics,
                diagnostics_secrets=diagnostics_secrets,
                accepted_statuses=accepted_statuses,
                public_redirect_domains=public_redirect_domains,
                request_timeout=request_timeout,
                **kwargs,
            )
        finally:
            if diagnostics is not None:
                diagnostics["duration_seconds"] = round(time.monotonic() - started, 6)

    def _request(
        self,
        method: str,
        url: str,
        *,
        retry: bool,
        max_bytes: int,
        magnet_redirect: bool,
        diagnostics: dict[str, Any] | None,
        diagnostics_secrets: tuple[str, ...],
        accepted_statuses: frozenset[int],
        public_redirect_domains: tuple[str, ...],
        request_timeout: float | None,
        **kwargs: Any,
    ) -> bytes:
        for attempt in range(1, (3 if retry else 1) + 1):
            try:
                if diagnostics is not None:
                    diagnostics["attempts"] = attempt
                with self.session.request(
                    method,
                    url,
                    timeout=(min(request_timeout or self.timeout, 5), request_timeout or self.timeout),
                    stream=True,
                    allow_redirects=False,
                    **kwargs,
                ) as response:
                    if diagnostics is not None:
                        diagnostics.update(
                            http_status=response.status_code,
                            final_location=public_text(safe_location(response.url or url), diagnostics_secrets),
                            content_type=public_text(
                                response.headers.get("Content-Type", "UNKNOWN"), diagnostics_secrets
                            ),
                        )
                    if public_redirect_domains and 300 <= response.status_code < 400:
                        destination = public_evidence_url(
                            urljoin(url, response.headers.get("Location", "")), public_redirect_domains
                        )
                        if not response.headers.get("Location") or not destination:
                            raise HTTPError(response.status_code, "REDIRECT_REFUSED", diagnostics)
                        if diagnostics is not None:
                            diagnostics["redirect_url"] = destination
                        return b""
                    # Jackett's download proxy can return Redirect(magnet), never
                    # follow it over HTTP. Only its caller may locally validate it.
                    if magnet_redirect and response.status_code == 302:
                        location = response.headers.get("Location", "")
                        if location.startswith("magnet:?") and len(location) <= 16_000:
                            return location.encode("utf-8")
                    if (
                        response.status_code >= 400 or 300 <= response.status_code < 400
                    ) and response.status_code not in accepted_statuses:
                        status = response.status_code
                        if diagnostics is not None and response.headers.get("Retry-After"):
                            diagnostics["retry_after_seconds"] = retry_delay(response, attempt)
                        if retry and status in {408, 429, 500, 502, 503, 504} and attempt < 3:
                            delay = retry_delay(response, attempt)
                            if delay <= 3:
                                time.sleep(delay)
                                continue
                        kind = (
                            "AUTHENTICATION_ERROR"
                            if status in {401, 407}
                            else "BLOCKED"
                            if status in {403, 451}
                            else "RATE_LIMITED"
                            if status == 429
                            else "NOT_FOUND"
                            if status == 404
                            else "TRANSIENT_ERROR"
                            if status >= 500
                            else "REDIRECT_REFUSED"
                            if 300 <= status < 400
                            else "INVALID_RESPONSE"
                        )
                        if diagnostics is not None:
                            # Read only a bounded error prefix. Never publish body text.
                            prefix = bytearray()
                            started = time.monotonic()
                            try:
                                for chunk in response.iter_content(4096):
                                    if time.monotonic() - started > self.timeout:
                                        break
                                    prefix.extend(chunk[: 64_000 - len(prefix)])
                                    if len(prefix) >= 64_000:
                                        break
                            except requests.RequestException:
                                # Known permanent statuses must not become retries
                                # just because their diagnostic body could not be read.
                                diagnostics["body_read"] = "incomplete"
                            # Extract only allowlisted machine codes, never provider messages.
                            try:
                                error = json.loads(prefix).get("error", {})
                                if isinstance(error, dict):
                                    violations = safe_provider_violations(
                                        error, diagnostics_secrets, diagnostic_reflections(kwargs.get("json"))
                                    )
                                    if violations:
                                        diagnostics["provider_field_violations"] = violations
                                    code = error.get("status")
                                    if isinstance(code, str) and code in {
                                        "INVALID_ARGUMENT",
                                        "UNAUTHENTICATED",
                                        "PERMISSION_DENIED",
                                        "NOT_FOUND",
                                        "RESOURCE_EXHAUSTED",
                                        "INTERNAL",
                                        "UNAVAILABLE",
                                        "DEADLINE_EXCEEDED",
                                        "FAILED_PRECONDITION",
                                    }:
                                        diagnostics["provider_code"] = code
                                    details = error.get("details", [])
                                    if isinstance(details, list):
                                        for detail in details:
                                            if not isinstance(detail, dict):
                                                continue
                                            if isinstance(detail.get("reason"), str) and detail.get("reason") in {
                                                "API_KEY_INVALID",
                                                "API_KEY_EXPIRED",
                                                "API_KEY_SERVICE_BLOCKED",
                                                "API_KEY_HTTP_REFERRER_BLOCKED",
                                                "API_KEY_IP_ADDRESS_BLOCKED",
                                                "API_KEY_RESTRICTION_VIOLATION",
                                                "BILLING_DISABLED",
                                                "RATE_LIMIT_EXCEEDED",
                                                "QUOTA_EXCEEDED",
                                            }:
                                                diagnostics["provider_reason"] = detail["reason"]
                                            if detail.get("@type") == "type.googleapis.com/google.rpc.QuotaFailure":
                                                diagnostics["quota_failure"] = True
                            except (ValueError, UnicodeError, AttributeError):
                                pass
                            evidence = page_evidence(bytes(prefix), response.headers)
                            diagnostics["evidence"] = evidence or [f"HTTP {status}"]
                            if evidence:
                                kind = "BLOCKED"
                            if 300 <= status < 400:
                                destination = response.headers.get("Location", "")
                                diagnostics["redirect_location"] = public_text(
                                    safe_location(destination), diagnostics_secrets
                                )
                                try:
                                    is_login = origin(urljoin(url, destination)) == origin(url) and safe_location(
                                        destination
                                    ).endswith("/login.php")
                                except SearchError:
                                    is_login = False
                                if is_login:
                                    kind = "AUTHENTICATION_ERROR"
                        raise HTTPError(status, kind, diagnostics)
                    started = time.monotonic()
                    declared = response.headers.get("Content-Length", "")
                    if declared.isdigit() and int(declared) > max_bytes:
                        raise SearchError("PARSER_ERROR: provider payload exceeds configured safety limit")
                    payload = bytearray()
                    for chunk in response.iter_content(64 * 1024):
                        if time.monotonic() - started > self.timeout:
                            if diagnostics is not None:
                                diagnostics["failure_kind"] = "TIMEOUT"
                            raise SearchError("TRANSIENT_ERROR: response read deadline exceeded")
                        payload.extend(chunk)
                        if len(payload) > max_bytes:
                            raise SearchError("PARSER_ERROR: provider payload exceeds configured safety limit")
                    if diagnostics is not None:
                        evidence = page_evidence(bytes(payload), response.headers)
                        if evidence:
                            diagnostics["evidence"] = evidence
                            raise HTTPError(response.status_code, "BLOCKED", diagnostics)
                    return bytes(payload)
            except requests.RequestException as exc:
                if diagnostics is not None:
                    diagnostics["failure_kind"] = "TIMEOUT" if isinstance(exc, requests.Timeout) else "TRANSPORT_ERROR"
                    if isinstance(exc, requests.Timeout):
                        diagnostics["timeout_phase"] = (
                            "connect"
                            if isinstance(exc, requests.ConnectTimeout)
                            else "read"
                            if isinstance(exc, requests.ReadTimeout)
                            else "unspecified"
                        )
                if retry and attempt < 3:
                    time.sleep(min(attempt, 2))
                    continue
                # Do not include Requests exceptions: they contain authenticated URLs.
                raise SearchError("TRANSIENT_ERROR: connection/timeout; credentials omitted") from None
        raise SearchError("TRANSIENT_ERROR: exhausted retries")

    def json(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            return json.loads(self.request(method, url, **kwargs))
        except (ValueError, UnicodeError) as exc:
            raise SearchError("PARSER_ERROR: invalid provider JSON") from exc


@dataclass(frozen=True)
class Download:
    info_hash: str
    torrent: bytes | None = datafield(default=None, repr=False)
    magnet: str | None = datafield(default=None, repr=False)


class SearchAdapter(Protocol):
    warnings: list[str]

    def search(self, query: Query, limit: int, page_size: int, max_pages: int) -> list[Candidate]: ...
    def details(self, candidate: Candidate) -> None: ...
    def resolve_download(self, candidate: Candidate) -> Download: ...


def numeric_summary(value: str | None, candidate: Candidate) -> int | None:
    if value is None or value == "":
        return None
    if re.fullmatch(r"[0-9]{1,19}", value.strip()) and int(value) <= 2**63 - 1:
        return int(value)
    candidate.warnings.append("Invalid numeric summary value; retained other evidence")
    return None


def parse_search_title(candidate: Candidate) -> None:
    """Separate title annotations from identity; retain only explicit, unverified claims."""
    title = candidate.torrent_title
    # Comparison representation only: preserve the entire provider title unchanged.
    normalized = "".join(ch for ch in unicodedata.normalize("NFKC", title) if unicodedata.category(ch) != "Cf")
    normalized = normalized.translate(str.maketrans("‐‑‒–—−", "------"))
    identity = normalized.strip()
    # RuTracker's leading genre/format groups precede the artist, including
    # adjacent tags such as [TR24][OF]. Balanced groups only; no arbitrary trim.
    malformed_prefix = False
    while identity.startswith(("(", "[")):
        stack: list[str] = []
        end: int | None = None
        for index, character in enumerate(identity):
            if character in "([":
                stack.append(character)
            elif character in ")]":
                if not stack or stack.pop() != {")": "(", "]": "["}[character]:
                    break
                if not stack:
                    end = index + 1
                    break
        if end is None:
            malformed_prefix = True
            break
        identity = identity[end:].lstrip()
    if re.match(r"^[-–—]\s+", identity):
        # A parenthesized artist is identity, not a genre prefix.
        identity = normalized.strip()
    annotations = normalized[: len(normalized) - len(identity)]
    parts = [] if malformed_prefix else re.split(r"\s+[-–—]\s+", identity, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        # A delimiter before a year can indicate a failed identity parse.
        if re.match(r"^(?:19|20)\d{2}\b", parts[1]):
            parts = []
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        candidate.claim("artist", parts[0].strip(), "search_title", title)
        album = re.split(r"\s+[-–—]\s+(?=(?:19|20)\d{2}\b|\()", parts[1], maxsplit=1)[0]
        album = re.split(r"\s*[\[(]", album, maxsplit=1)[0].strip()
        album = re.split(r",\s*(?=(?:FLAC|ALAC|APE|WAV|WavPack|DSD)\b)", album, maxsplit=1, flags=re.IGNORECASE)[
            0
        ].strip()
        if album:
            candidate.claim("release_title", album, "search_title", title)
            candidate.title_parse_status = "parsed"
            annotations += " " + parts[1][len(album) :]
    if candidate.title_parse_status != "parsed":
        candidate.title_parse_status = "ambiguous"
    candidate.years_stated = sorted(set(re.findall(r"\b(?:19|20)\d{2}\b", annotations)))
    if candidate.years_stated:
        candidate.evidence["years_stated"] = {"source": "search_title", "claim": title, "status": "year roles unknown"}
    for name, pattern in (
        ("edition", r"\b(?:MQA\s*[x×]\s*UHQCD|Reissue|Remastered|Embossed)\b"),
        ("mastering", r"\bRemastered\b"),
        ("country", r"\b(?:Japan|Holland|Netherlands|Germany|UK|USA|France|Canada)\b"),
    ):
        fragments = list(dict.fromkeys(m.group() for m in re.finditer(pattern, annotations, re.IGNORECASE)))
        if fragments:
            candidate.claim(name, ", ".join(fragments), "search_title", title)
            if name == "country":
                candidate.evidence[name]["role"] = "unspecified"
                candidate.claims[-1]["role"] = "unspecified"
    # Explicit label/catalog assignments only; ordinary names/numbers are not guessed.
    for name, pattern in (
        ("label", r"(?i)(?:label|лейбл)\s*[:=]\s*([^;,()\[\]]+)"),
        ("catalog_number", r"(?i)(?:catalog(?:ue)?(?: number)?|cat\.?|каталожный номер)\s*[:=#]\s*([^;,()\[\]]+)"),
    ):
        if match := re.search(pattern, annotations):
            candidate.claim(name, match.group(1).strip(), "search_title", title)
    # Do not infer a codec or rate from CD, TR24, OF or the word 'lossless'.
    for field, pattern in (
        ("medium", r"\[(CD|LP|SACD|DVD-A)\]"),
        ("codec", r"\b(FLAC|ALAC|APE|WAV|WavPack|DSD(?:\s+\d+)?)\b"),
    ):
        values = {m.group(1).upper() for m in re.finditer(pattern, annotations, re.IGNORECASE)}
        if len(values) == 1:
            candidate.claim(field, next(iter(values)), "search_title", title)
        elif values:
            candidate.warnings.append(f"Conflicting title-derived {field}; left unknown")
    rates = list(
        re.finditer(r"\[(16|24|32f?)/(44[.,]1|48|88[.,]2|96|176[.,]4|192|352[.,]8|384)\]", annotations, re.IGNORECASE)
    )
    rate_values = {(m.group(1).lower(), m.group(2).replace(",", ".")) for m in rates}
    if len(rate_values) == 1:
        depth, rate = next(iter(rate_values))
        candidate.claim("bit_depth", depth + " bit", "search_title", rates[0].group())
        candidate.claim("sample_rate", rate + " kHz", "search_title", rates[0].group())
    elif rate_values:
        candidate.warnings.append("Conflicting title-derived sample format; left unknown")
    if candidate.codec in {"FLAC", "ALAC", "APE", "WAV"}:
        candidate.claim(
            "lossless", True, "search_title", f"Title codec: {candidate.codec}; source authenticity unverified"
        )
    for evidence in [*candidate.evidence.values(), *candidate.claims]:
        if evidence["source"] == "search_title":
            evidence["status"] = "title-derived, unverified"


# Explicit label values only. Formats in a title are tentative, never measurements.
LABELS = {
    "artist": ("artist", "исполнитель"),
    "release_title": ("album", "название альбома", "название"),
    "release_type": ("release type", "тип релиза"),
    "release_year": ("release year", "год издания", "год выпуска переиздания"),
    "original_album_year": ("original album year", "год оригинального альбома"),
    "label": ("label", "лейбл", "издатель (лейбл)"),
    "catalog_number": ("catalog number", "каталожный номер", "номер по каталогу", "номер издания"),
    "edition": ("edition", "издание"),
    "country": (
        "country",
        "страна",
        "pressing country",
        "release country",
        "country of manufacture",
        "страна-производитель диска",
        "страна-производитель диска (релиза)",
        "страна-производитель",
        "страна издания",
    ),
    "mastering": ("mastering", "мастеринг"),
    "mastering_credit": ("mastered by", "mastering engineer", "mastering credit", "инженер мастеринга"),
    "matrix": ("matrix", "matrix number", "матрица", "матричный номер"),
    "medium": ("source medium", "носитель"),
    "codec": ("codec", "аудиокодек", "аудио кодек", "кодек"),
    "bit_depth": ("bit depth", "разрядность"),
    "sample_rate": ("sample rate", "частота дискретизации"),
    "channels": ("channels", "каналы"),
    "rip_log": ("rip log", "log", "лог", "лог рипа"),
    "verification_evidence": ("verification", "проверка", "checksum", "контрольные суммы"),
    "processing": ("processing", "обработка"),
    "complete": ("complete", "полнота"),
    "disc_count": ("disc count", "количество дисков"),
    "artwork": ("artwork", "сканы", "наличие сканов", "наличие сканов в содержимом раздачи"),
    "bonus_tracks": ("bonus tracks", "бонус-треки"),
    "rip_type": ("rip type", "тип рипа"),
    "recording_format": ("recording format", "формат записи"),
    "distribution_format": ("distribution format", "формат раздачи"),
    "reported_dr": ("official dr value", "reported dr"),
    "source_origin": ("источник", "source origin"),
}


def parse_details(candidate: Candidate, raw: bytes | str, secrets: tuple[str, ...] = ()) -> None:
    def clean(value: object) -> str:
        return public_text(value, secrets)

    soup = BeautifulSoup(
        raw,
        "html.parser",
        from_encoding="windows-1251" if isinstance(raw, bytes) and b"windows-1251" in raw.lower() else None,
    )
    if page_evidence(raw.encode("utf-8") if isinstance(raw, str) else raw, {}):
        raise SearchError("BLOCKED: recognized challenge page; authentication validity unknown")
    post = soup.select_one(".post_body")
    if post is None:
        if soup.select_one('input[name="login_username"], input[name="login_password"]'):
            raise SearchError("AUTHENTICATION_ERROR: topic requires an authorized RUTRACKER_COOKIE")
        raise SearchError("PARSER_ERROR: no recognized topic post; summary retained")
    for node in post.select("script, style, form"):
        node.decompose()
    for br in post.find_all("br"):
        br.replace_with("\n")
    lines = [line.strip() for line in post.get_text("\n").splitlines() if line.strip()]
    # Real topic DOM often splits label, colon and value into three nodes.
    # Reassemble only colon-bound fields, preserving track/title text separately.
    joined: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if index + 1 < len(lines) and lines[index + 1].startswith(":"):
            suffix = lines[index + 1]
            index += 1
            if suffix == ":" and index + 1 < len(lines):
                next_line = lines[index + 1]
                next_is_label = index + 2 < len(lines) and lines[index + 2].startswith(":")
                if not next_is_label and not next_line.endswith(":") and len(next_line) < 1000:
                    # Track-list headers must not consume the first track.
                    if not re.fullmatch(r"(?i)(track\s*list|треклист|список композиций)", line):
                        suffix += " " + next_line
                        index += 1
            line += suffix
        joined.append(line)
        index += 1
    lines = joined
    # Bold label and value can be separate DOM text nodes.
    combined = []
    known_labels = {alias for aliases in LABELS.values() for alias in aliases}
    for index, line in enumerate(lines):
        if line.endswith(":") and index + 1 < len(lines):
            following = lines[index + 1]
            if following.endswith(":") or following.split(":", 1)[0].casefold() in known_labels:
                # Missing value: another field header is not an artist/title.
                combined.append(line)
            else:
                combined.append(line + " " + following)
        else:
            combined.append(line)
    for field, aliases in LABELS.items():
        country_role = "unspecified"
        if field == "country":
            explicit = [
                line
                for line in combined
                if (m := re.match(r"^([^:]{1,70}):\s*(.+)$", line))
                and m.group(1).strip().casefold() in aliases
                and m.group(1).strip().casefold() not in {"country", "страна"}
            ]
            if explicit:
                country_role = "edition"
                field_lines = explicit
                for line in combined:
                    if m := re.match(r"^(?:Country|Страна):\s*(.+)$", line, re.IGNORECASE):
                        candidate.claims.append(
                            {
                                "field": "country_unspecified",
                                "value": clean(m.group(1)),
                                "source": "topic_detail",
                                "claim": clean(line),
                                "status": "country role unknown",
                            }
                        )
            else:
                field_lines = combined
        else:
            field_lines = combined
        values = [
            match.group(2).strip()
            for line in field_lines
            if (match := re.match(r"^([^:]{1,70}):\s*(.+)$", line)) and match.group(1).strip().casefold() in aliases
        ]
        values = sorted(set(values))
        originals = list(values)
        if field in {"country", "medium"}:
            values = list({claim_identity(field, value): value for value in values}.values())
        if field == "codec":
            values = sorted({re.sub(r"(?i)\s*\(\*\.(?:flac|ape|wv|wav|mp3)\)\s*$", "", item) for item in values})
        if len(values) > 1:
            setattr(candidate, field, None)
            candidate.evidence.pop(field, None)
            if field == "codec":
                candidate.lossless = None
                candidate.evidence.pop("lossless", None)
            candidate.warnings.append(f"Conflicting stated {field}; left unknown")
            for original_value in originals:
                candidate.claims.append(
                    {
                        "field": field,
                        "value": original_value,
                        "source": "topic_detail",
                        "claim": clean(original_value),
                        "status": "conflicting stated claim; unresolved",
                    }
                )
        elif values:
            value: Any = clean(values[0])
            if field == "release_type":
                value = {"альбом": "album", "сингл": "single"}.get(value.casefold(), value.casefold())
            if field == "complete":
                value = (
                    True
                    if value.casefold() in {"yes", "да", "complete", "полный"}
                    else False
                    if value.casefold() in {"no", "нет", "partial", "неполный"}
                    else None
                )
            if (
                field in {"release_year", "original_album_year"}
                and len(set(re.findall(r"\b(?:19|20)\d{2}\b", str(value)))) > 1
            ):
                candidate.years_stated = sorted(
                    set(candidate.years_stated + re.findall(r"\b(?:19|20)\d{2}\b", str(value)))
                )
                candidate.claim("years_stated", candidate.years_stated, "topic_detail", clean(originals[0]))
                value = None
                candidate.warnings.append("Multiple years in one label; individual year roles unresolved.")
            candidate.claim(field, value, "topic_detail", clean(originals[0]))
            if field == "country":
                candidate.evidence[field]["role"] = country_role
                candidate.claims[-1]["role"] = country_role
                if country_role == "unspecified":
                    candidate.extraction_gaps["country_role"] = (
                        "A generic country label does not establish pressing or manufacture country."
                    )
    # Unqualified years keep unknown roles, unlike explicit release/original labels.
    for line in combined:
        if re.match(r"(?i)^год выпуска:", line):
            years = re.findall(r"\b(?:19|20)\d{2}\b", line)
            candidate.years_stated = sorted(set(candidate.years_stated + years))
            candidate.claim("years_stated", candidate.years_stated, "topic_detail", clean(line))
            candidate.evidence["years_stated"]["status"] = "year roles unknown"
    if (
        candidate.label
        and candidate.evidence.get("label", {}).get("source") == "topic_detail"
        and not candidate.catalog_number
    ):
        if match := re.fullmatch(
            r"(.+?)\s+[−–—-]\s+([A-Z0-9][A-Z0-9 ./-]*\d[A-Z0-9 ./-]*)",
            "".join(ch for ch in unicodedata.normalize("NFKC", candidate.label) if unicodedata.category(ch) != "Cf"),
        ):
            candidate.claim("catalog_number", match.group(2).strip(), "topic_detail", clean(candidate.label))
            candidate.evidence["catalog_number"]["status"] = "parsed stated label/catalog pair; unverified"
    # Recording and distributed sample formats describe different representations.
    name = "distribution_format" if candidate.distribution_format else "recording_format"
    if candidate.evidence.get(name, {}).get("source") == "topic_detail":
        if match := re.fullmatch(
            r"(16|24|32f?)/(44[.,]1|48|88[.,]2|96|176[.,]4|192|352[.,]8|384)",
            getattr(candidate, name) or "",
            re.IGNORECASE,
        ):
            for field, value in (
                ("bit_depth", match.group(1) + " bit"),
                ("sample_rate", match.group(2).replace(",", ".") + " kHz"),
            ):
                candidate.claim(field, value, "topic_detail", clean(getattr(candidate, name)))
                candidate.evidence[field]["status"] = "stated " + name + "; unmeasured"
    setup_labels = {
        "источник оцифровки",
        "код класса состояния винила",
        "устройство воспроизведения",
        "головка звукоснимателя",
        "предварительный усилитель",
        "фонокорректор",
        "ацп",
        "ацп-оцифровщик",
        "программа-оцифровщик",
        "программа",
    }
    setup = {
        line.split(":", 1)[0].strip(): clean(line.split(":", 1)[1].strip())
        for line in combined
        if ":" in line and line.split(":", 1)[0].strip().casefold() in setup_labels and line.split(":", 1)[1].strip()
    }
    if setup:
        candidate.claim("rip_setup", setup, "topic_detail", clean(json.dumps(setup, ensure_ascii=False)))
    if lines:
        heading = Candidate(candidate.candidate_id, candidate.source_url, clean(lines[0]))
        heading_text = re.sub(r"\s+/\s+", " - ", heading.torrent_title)
        heading.torrent_title = heading_text
        parse_search_title(heading)
        if heading.title_parse_status == "parsed":
            for name in ("artist", "release_title", "edition", "mastering"):
                if getattr(heading, name) and candidate.evidence.get(name, {}).get("source") != "topic_detail":
                    candidate.claim(name, getattr(heading, name), "topic_heading", clean(lines[0]))
                    candidate.evidence[name]["status"] = "stated heading identity/edition; unverified"
    log_markers = [
        clean(line)
        for line in lines
        if re.match(r"(?i)^(?:Exact Audio Copy|EAC extraction logfile|X Lossless Decoder|Лог создания рипа)", line)
    ]
    if log_markers and not candidate.rip_log:
        candidate.claim(
            "rip_log", "Log text present; authenticity unverified", "topic_detail", " | ".join(log_markers[:3])
        )
    verification = [
        clean(line)
        for line in lines
        if re.match(
            r"(?i)^\s*(?:Test CRC|Copy CRC|Copy OK|No errors occurred|None of the tracks are present in the AccurateRip database|All tracks accurately ripped)",
            line,
        )
    ]
    if verification and not candidate.verification_evidence:
        candidate.claim(
            "verification_evidence", " | ".join(verification[:10]), "topic_detail", " | ".join(verification[:10])
        )
        candidate.evidence["verification_evidence"]["status"] = "reported log outcomes; not independently verified"
    tracks: list[str] = []
    in_tracks = False
    track_count = 0
    pending_heading: str | None = None
    for line in lines:
        if re.fullmatch(r"(?i)(track\s*list|треклист|список композиций):?", line):
            in_tracks = True
            continue
        if not in_tracks:
            continue
        if re.fullmatch(r"(?i)(?:(?:CD|disc|disk|диск)\s*#?\s*\d{1,3}\s*:?(?:\s+-\s+.+)?|side\s+[12AB])", line):
            pending_heading = clean(line)
            continue
        if re.match(r"^(?:\d{1,3}[.) -]+|[A-Z]\d{1,3}\s+)\S", line):
            track_count += 1
            if track_count <= 200:
                if pending_heading:
                    tracks.append(pending_heading)
                    pending_heading = None
                tracks.append(clean(line))
        elif track_count:
            break
    candidate.track_list = tracks
    candidate.track_count = track_count if track_count else None
    if tracks:
        candidate.evidence["track_list"] = {
            "source": "topic_detail",
            "claim": clean("\n".join(tracks)),
            "status": "stated list; completeness unverified",
        }
        candidate.evidence["track_count"] = {
            "source": "parsed_topic_list",
            "claim": f"{track_count} listed tracks parsed; not a verified release total",
            "status": "parsed count",
        }
    if track_count > 200:
        candidate.warnings.append("Track list truncated at 200 tracks (disc headings retained)")
    if candidate.codec and candidate.evidence.get("codec", {}).get("source") == "topic_detail":
        codec = candidate.codec.casefold().strip()
        lossless = (
            True
            if codec in {"flac", "alac", "ape", "wavpack lossless", "pcm", "wav"}
            else False
            if codec in {"mp3", "aac", "ogg vorbis", "opus"}
            else None
        )
        candidate.claim(
            "lossless", lossless, "topic_detail", f"Stated codec: {candidate.codec}; source authenticity unverified"
        )
    # Record coverage of accessible evidence, not a completeness guarantee.
    title_fields = {name for name, evidence in candidate.evidence.items() if evidence.get("source") == "search_title"}
    candidate.extraction_gaps = {
        name: (
            "title_claim_only; absent recognized label in accessible description"
            if name in title_fields
            else "absent recognized label in accessible description"
        )
        for name in LABELS
        if candidate.evidence.get(name, {}).get("source") != "topic_detail"
    }
    unparsed = [
        clean(line)
        for line in combined
        if re.match(r"^[^:]{1,70}:\s*\S", line)
        and line.split(":", 1)[0].strip().casefold()
        not in known_labels | {"год выпуска", "треклист", "tracklist", "track list", "список композиций"}
    ]
    if unparsed:
        candidate.unmapped_description_fields = [
            {"source": "topic_detail", "claim": line, "status": "unmapped label; not silently absent"}
            for line in unparsed[:30]
        ]
    candidate.detail_diagnostics["description_characters"] = sum(len(line) for line in lines)
    candidate.detail_diagnostics["unmapped_label_count"] = len(unparsed)
    candidate.evidence_completeness = (
        "accessible_description_partial"
        if candidate.extraction_gaps
        else "recognized_fields_available; release unverified"
    )
    candidate.detail_status = "FOUND"
    candidate.extraction_confidence = (
        "explicit_topic_fields"
        if any(e["source"] == "topic_detail" for e in candidate.evidence.values())
        else "title_only"
    )


class Jackett:
    def __init__(self, policy: SearchPolicy, http: HTTP | None = None) -> None:
        self.policy = policy
        configured_url = policy.jackett_url or os.environ.get("JACKETT_URL", "")
        if not configured_url:
            raise SearchError("NOT_CONFIGURED: set search.jackett_url or JACKETT_URL")
        self.base = endpoint(configured_url)
        # No aggregate/filter indexer is ever accepted. RuTracker.RU is a different tracker.
        if policy.jackett_indexer != "rutracker":
            raise SearchError(
                "Configure Jackett's RuTracker.org indexer ID 'rutracker'; aggregate/other indexers are refused"
            )
        self.key = os.environ.get(policy.jackett_key_env, "")
        if not self.key:
            raise SearchError(
                "NOT_CONFIGURED: set the environment variable named by search.jackett_key_env for Jackett"
            )
        self.topic_base = endpoint(policy.topic_base_url)
        solver_url = policy.flaresolverr_url or os.environ.get("FLARESOLVERR_URL", "")
        self.solver_base = endpoint(solver_url) if solver_url else ""
        self.http = http or HTTP(policy.timeout)
        self.pacing = IntervalRateLimiter(1.0, shared_name="auph-search-rutracker")
        self.warnings: list[str] = []
        self.search_diagnostics: list[dict[str, Any]] = []
        self.download_refs: dict[str, str] = {}
        self._browser_clearance: tuple[list[dict[str, str]], str] | None = None
        self._issued_secrets: set[str] = set()
        self._detail_memory: dict[str, dict[str, Any]] = {}
        self._detail_cache = (
            JsonCache(Path(policy.detail_cache_directory), ttl_seconds=policy.detail_cache_ttl)
            if policy.detail_cache_directory
            else None
        )

    def public_text(self, value: object) -> str:
        return public_text(value, self.detail_secrets())

    def detail_secrets(self) -> tuple[str, ...]:
        return tuple(sorted(set(secret_values(self.policy)) | self._issued_secrets, key=len, reverse=True))

    def topic(self, supplied: str) -> tuple[str, str] | None:
        try:
            if origin(supplied) != origin(self.topic_base):
                return None
        except SearchError:
            return None
        parsed = urlsplit(supplied)
        if parsed.path != urlsplit(self.topic_base).path + "/viewtopic.php":
            return None
        topic_id = parse_qs(parsed.query).get("t", [""])[0]
        if not re.fullmatch(r"[1-9]\d{0,12}", topic_id):
            return None
        return topic_id, self.topic_base + "/viewtopic.php?t=" + topic_id

    def search(self, query: Query, limit: int, page_size: int, max_pages: int) -> list[Candidate]:
        candidates: dict[str, Candidate] = {}
        seen_pages: set[str] = set()
        for page in range(max_pages):
            try:
                self.pacing.acquire()
                page_diagnostics: dict[str, Any] = {"page": page + 1}
                self.search_diagnostics.append(page_diagnostics)
                raw = self.http.request(
                    "GET",
                    self.base + "/api/v2.0/indexers/rutracker/results/torznab/api",
                    diagnostics=page_diagnostics,
                    diagnostics_secrets=self.detail_secrets(),
                    accepted_statuses=(400,),
                    params={
                        "apikey": self.key,
                        "t": "search",
                        "q": query.search_text,
                        "cat": "3000",
                        "limit": page_size,
                        "offset": page * page_size,
                    },
                )
                if b"\x00" in raw or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                    raise SearchError("PARSER_ERROR: XML entities/DTDs and NUL-containing encodings refused")
                tree = ET.fromstring(raw)
                if tree.tag.rsplit("}", 1)[-1] == "error":
                    description = str(tree.attrib.get("description", "")).casefold()
                    code = str(tree.attrib.get("code", ""))
                    page_diagnostics["torznab_error_code"] = code if re.fullmatch(r"\d{1,5}", code) else "unknown"
                    # Classify only known causes. Indexer exception descriptions can
                    # contain authenticated request headers; never publish them.
                    if "flaresolverr" in description and any(
                        v in description for v in ("connection refused", "error connecting", "actively refused")
                    ):
                        raise SearchError(
                            "DEPENDENCY_UNAVAILABLE: Jackett cannot connect to FlareSolverr; start its configured service"
                        )
                    if code == "100" or "invalid api key" in description or "incorrect api key" in description:
                        raise SearchError("AUTHENTICATION_ERROR: Jackett API key rejected")
                    if "cookie did not work" in description or "invalid cookie" in description:
                        raise SearchError(
                            "AUTHENTICATION_ERROR: RuTracker session rejected; update the indexer session in Jackett"
                        )
                    raise SearchError(
                        "INDEXER_ERROR: Jackett could not complete the RuTracker search; check its indexer status"
                    )
                if tree.tag.rsplit("}", 1)[-1] != "rss":
                    raise SearchError("PARSER_ERROR: expected RSS Torznab response")
            except (ET.ParseError, SearchError) as exc:
                if candidates:
                    self.warnings.append(
                        f"Later search page failed ({self.public_text(exc)}); earlier results retained"
                    )
                    break
                if isinstance(exc, SearchError):
                    raise
                raise SearchError("PARSER_ERROR: Jackett did not return Torznab XML") from exc
            items = tree.findall(".//item")
            if not items:
                break
            page_key = hashlib.sha256(raw).hexdigest()
            if page_key in seen_pages:
                self.warnings.append("Repeated page: indexer pagination may be unsupported; stopped")
                break
            seen_pages.add(page_key)
            added = 0
            for item in items:
                topic = next(
                    (p for name in ("comments", "guid", "link") if (p := self.topic(item.findtext(name, "")))), None
                )
                title = item.findtext("title", "")
                if not topic or not title.strip():
                    self.warnings.append("Skipped search row lacking a safe RuTracker topic or title")
                    continue
                topic_id, url = topic
                cid = "rutracker:" + topic_id
                if cid in candidates:
                    if candidates[cid].torrent_title != self.public_text(title):
                        candidates[cid].warnings.append("Duplicate topic has conflicting search titles")
                    continue
                candidate = Candidate(cid, url, self.public_text(title))
                candidate.evidence["torrent_title"] = {
                    "source": "jackett_summary",
                    "claim": self.public_text(title),
                    "status": "search summary",
                }
                parse_search_title(candidate)
                attrs = {
                    node.attrib.get("name", ""): node.attrib.get("value", "")
                    for node in item
                    if node.tag.endswith("attr")
                }
                enclosure = item.find("enclosure")
                size = (
                    attrs.get("size")
                    or item.findtext("size")
                    or (enclosure.get("length") if enclosure is not None else None)
                )

                size_value = numeric_summary(size, candidate)
                peers, seeders = (
                    numeric_summary(attrs.get("peers"), candidate),
                    numeric_summary(attrs.get("seeders"), candidate),
                )
                leechers = None
                if peers is not None and seeders is not None:
                    if peers >= seeders:
                        leechers = peers - seeders
                    else:
                        candidate.warnings.append("Reported peers less than seeders; leechers unknown")
                for field, value in (("size_bytes", size_value), ("seeders", seeders), ("leechers", leechers)):
                    if value is not None:
                        candidate.claim(field, value, "jackett_summary", str(value))
                candidate.published_at = self.public_text(item.findtext("pubDate", "")) or None
                enclosure = item.find("enclosure")
                ref = (enclosure.get("url", "") if enclosure is not None else "") or item.findtext("link", "")
                self.download_refs[cid] = ref
                candidates[cid] = candidate
                added += 1
                if len(candidates) >= limit:
                    self.warnings.append(f"Result limit {limit} reached; additional/better candidates may be omitted")
                    break
            if len(candidates) >= limit:
                break
            if not added:
                self.warnings.append("No new topics on page; pagination stopped")
                break
            if len(items) < page_size:
                break
        else:
            self.warnings.append(f"Search page bound {max_pages} reached; completeness of discovery unknown")
        return sorted(candidates.values(), key=lambda c: c.candidate_id)

    def solver_details(self, candidate: Candidate, cookie: str) -> bytes | str:
        started = time.monotonic()
        try:
            return self._solver_details(candidate, cookie)
        finally:
            candidate.timings["solver_path_seconds"] = round(time.monotonic() - started, 6)
            candidate.timings["flaresolverr_seconds"] = float(
                candidate.detail_diagnostics.get("solver_duration_seconds", 0.0)
            )

    def _solver_details(self, candidate: Candidate, cookie: str) -> bytes | str:
        """Use the configured browser service's documented request.get API.

        The service receives no configured credentials (it logs request bodies).
        Clearance cookies and its user agent are used for a separate authenticated
        topic request. No persistent sessions, automated login or CAPTCHA API.
        """
        if len(cookie) > 16_000 or re.search(r"[^\x20-\x7e]", cookie):
            raise SearchError("INVALID_CONFIGURATION: topic cookie exceeds bounds or contains unsafe characters")
        cookies = SimpleCookie()
        try:
            cookies.load(cookie)
        except CookieError:
            raise SearchError("INVALID_CONFIGURATION: topic cookie is not a valid Cookie string") from None
        if not cookies:
            raise SearchError("INVALID_CONFIGURATION: topic cookie is not a valid Cookie string")
        if self._browser_clearance is not None:
            try:
                return self.authenticated_details(candidate, cookie, *self._browser_clearance)
            except HTTPError as exc:
                if exc.status != 403:
                    raise
                candidate.detail_diagnostics["cached_clearance_http_status"] = exc.status
                # One fresh browser acquisition after expired/rejected clearance.
        timeout = self.policy.flaresolverr_timeout
        api = self.solver_base if urlsplit(self.solver_base).path.endswith("/v1") else self.solver_base + "/v1"
        transport_diagnostics: dict[str, Any] = {}
        try:
            # maxTimeout bounds challenge solving, not browser startup/cleanup.
            # A bounded grace period lets its structured timeout arrive.
            raw_envelope = HTTP(timeout + 15).request(
                "POST",
                api,
                retry=False,
                max_bytes=4_000_000,
                accepted_statuses=frozenset({500}),
                diagnostics=transport_diagnostics,
                json={
                    "cmd": "request.get",
                    "url": candidate.source_url,
                    "maxTimeout": int(timeout * 1000),
                },
            )
        finally:
            candidate.detail_diagnostics.update({"solver_" + k: v for k, v in transport_diagnostics.items()})
        try:
            data = json.loads(raw_envelope)
        except (ValueError, UnicodeError):
            raise SearchError("PARSER_ERROR: invalid FlareSolverr JSON") from None
        if not isinstance(data, dict):
            raise SearchError("INVALID_RESPONSE: malformed FlareSolverr envelope")
        if data.get("status") != "ok":
            # Error messages may echo cookies or URLs. Only report recognized
            # categories, never forward arbitrary solver messages.
            message = str(data.get("message", "")).lower()
            reason = (
                "challenge timeout"
                if "timeout" in message and "challenge" in message
                else "CAPTCHA required"
                if "captcha" in message
                else "browser request denied"
                if "cloudflare has blocked" in message or "access denied" in message
                else "solver failure"
            )
            candidate.detail_diagnostics["evidence"] = [reason]
            kind = "BLOCKED" if reason != "solver failure" else "TRANSIENT_ERROR"
            raise SearchError(kind + ": FlareSolverr " + reason + "; authentication validity unknown")
        solution = data.get("solution")
        if not isinstance(solution, dict):
            raise SearchError("INVALID_RESPONSE: missing FlareSolverr solution")
        url, status, body = solution.get("url"), solution.get("status"), solution.get("response")
        headers = solution.get("headers", {})
        if (
            not isinstance(url, str)
            or type(status) is not int
            or not isinstance(body, str)
            or not isinstance(headers, dict)
        ):
            raise SearchError("INVALID_RESPONSE: malformed FlareSolverr solution")
        if not 100 <= status <= 599:
            raise SearchError("INVALID_RESPONSE: invalid FlareSolverr HTTP status")
        normalized_headers = {str(k).lower(): v for k, v in headers.items()}
        diagnostics = candidate.detail_diagnostics
        diagnostics.update(
            browser_reported_status=status,
            browser_final_location=safe_location(url),
            browser_content_type=public_text(
                normalized_headers.get("content-type", "UNKNOWN"), secret_values(self.policy)
            ),
        )
        raw = body.encode("utf-8")
        if len(raw) > 2_000_000:
            raise SearchError("PARSER_ERROR: topic payload exceeds configured safety limit")
        browser_soup = BeautifulSoup(body, "html.parser")
        diagnostics["browser_topic_post"] = browser_soup.select_one(".post_body") is not None
        diagnostics["browser_login_form"] = (
            browser_soup.select_one('input[name="login_username"], input[name="login_password"]') is not None
        )
        evidence = page_evidence(raw, normalized_headers)
        diagnostics["evidence"] = evidence or [f"HTTP {status}"]
        if evidence or status in {403, 451}:
            raise HTTPError(status, "BLOCKED", diagnostics)
        public_login = origin(url) == origin(self.topic_base) and urlsplit(url).path.endswith("/login.php")
        if not public_login and self.topic(url) != (
            candidate.candidate_id.removeprefix("rutracker:"),
            candidate.source_url,
        ):
            raise SearchError("REDIRECT_REFUSED: solver final URL does not match requested topic")
        if status >= 400 or 300 <= status < 400:
            kind = (
                "AUTHENTICATION_ERROR"
                if status in {401, 407}
                else "RATE_LIMITED"
                if status == 429
                else "NOT_FOUND"
                if status == 404
                else "TRANSIENT_ERROR"
                if status >= 500
                else "INVALID_RESPONSE"
            )
            raise HTTPError(status, kind, diagnostics)
        browser_cookies, user_agent = solution.get("cookies"), solution.get("userAgent")
        if (
            not isinstance(browser_cookies, list)
            or len(browser_cookies) > 100
            or not isinstance(user_agent, str)
            or not user_agent
            or len(user_agent) > 1000
            or re.search(r"[^\x20-\x7e]", user_agent)
        ):
            raise SearchError("INVALID_RESPONSE: missing/invalid browser clearance cookies or user agent")
        clearance: list[dict[str, str]] = []
        for item in browser_cookies:
            if not isinstance(item, dict):
                raise SearchError("INVALID_RESPONSE: malformed browser cookie")
            domain, path, name, value = (item.get("domain"), item.get("path", "/"), item.get("name"), item.get("value"))
            if not all(isinstance(v, str) for v in (domain, path, name, value)):
                raise SearchError("INVALID_RESPONSE: malformed browser cookie fields")
            if (
                str(domain).lstrip(".").lower() != urlsplit(self.topic_base).hostname
                or not urlsplit(candidate.source_url).path.startswith(str(path))
                or not str(path).startswith("/")
            ):
                continue
            if (
                not re.fullmatch(r"[A-Za-z0-9_!#$%&'*+.^`|~-]{1,200}", str(name))
                or len(str(value)) > 16_000
                or re.search(r"[\x00-\x1f\x7f]", str(value))
            ):
                raise SearchError("INVALID_RESPONSE: unsafe browser cookie")
            clearance.append({"name": str(name), "value": str(value)})
        self._browser_clearance = clearance, user_agent
        self._issued_secrets.update(item["value"] for item in clearance if len(item["value"]) >= 8)
        try:
            return self.authenticated_details(candidate, cookie, clearance, user_agent)
        except HTTPError as exc:
            # The independent public response is usable only for this exact topic.
            # Authentication errors are never relaxed into a public fallback.
            if exc.status != 403 or public_login:
                raise
            if not diagnostics["browser_topic_post"]:
                raise
            diagnostics["selected_response"] = "public_browser"
            diagnostics["selected_http_status"] = "UNKNOWN (browser does not expose HTTP status)"
            candidate.warnings.append(
                "Authenticated HTTP remains blocked; using independent public browser topic. Cookie validity unknown."
            )
            return body

    def authenticated_details(
        self, candidate: Candidate, cookie: str, clearance: list[dict[str, str]], user_agent: str
    ) -> bytes:
        combined = SimpleCookie()
        for item in clearance:
            combined[item["name"]] = item["value"]
        # User authentication takes precedence over anonymous browser cookies.
        combined.load(cookie)
        header = "; ".join(f"{name}={morsel.coded_value}" for name, morsel in combined.items())
        if len(header) > 32_000:
            raise SearchError("INVALID_RESPONSE: combined cookie header exceeds safety bound")
        self.pacing.acquire()
        started = time.monotonic()
        try:
            return self.http.request(
                "GET",
                candidate.source_url,
                retry=False,
                headers={"Cookie": header, "User-Agent": user_agent},
                diagnostics=candidate.detail_diagnostics,
                diagnostics_secrets=self.detail_secrets(),
            )
        except HTTPError as exc:
            if exc.status in {403, 451}:
                self._browser_clearance = None
            raise
        finally:
            candidate.timings["direct_http_seconds"] = round(
                candidate.timings.get("direct_http_seconds", 0.0) + time.monotonic() - started, 6
            )

    def details(self, candidate: Candidate) -> None:
        cookie = os.environ.get(self.policy.topic_cookie_env, "")
        if not self.policy.details or not cookie:
            candidate.detail_status = "DISABLED" if not self.policy.details else "NOT_CONFIGURED"
            candidate.warnings.append(
                f"Topic details unavailable; configure authorized {self.policy.topic_cookie_env}. Summary is incomplete."
            )
            return
        key = (
            "search-detail-v7:"
            + hashlib.sha256(
                (
                    candidate.source_url + "\0" + candidate.torrent_title + "\0" + cookie + "\0" + self.solver_base
                ).encode()
            ).hexdigest()
        )
        cached = self._detail_memory.get(key)
        if cached is None and self._detail_cache is not None and self.policy.detail_cache_ttl:
            entry = self._detail_cache.load(key)
            if entry and entry[0] == 200 and isinstance(entry[1], dict):
                cached = entry[1]
        if cached is not None and self.policy.detail_cache_ttl:
            if self._apply_detail_cache(candidate, cached):
                candidate.detail_diagnostics["cache"] = "hit; original acquisition diagnostics retained"
                return
        try:
            if self.topic(candidate.source_url) != (
                candidate.candidate_id.removeprefix("rutracker:"),
                candidate.source_url,
            ):
                raise SearchError("INVALID_RESPONSE: detail URL does not match trusted topic identity")
            self.pacing.acquire()
            candidate.detail_diagnostics = {
                "transport": "flaresolverr" if self.solver_base else "direct",
                "http_status": "UNKNOWN",
                "final_location": safe_location(candidate.source_url),
                "content_type": "UNKNOWN",
            }
            if self.solver_base:
                raw = self.solver_details(candidate, cookie)
            else:
                raw = self.http.request(
                    "GET",
                    candidate.source_url,
                    headers={"Cookie": cookie},
                    retry=False,
                    diagnostics=candidate.detail_diagnostics,
                    diagnostics_secrets=self.detail_secrets(),
                )
            content_type = candidate.detail_diagnostics.get(
                "browser_content_type"
                if candidate.detail_diagnostics.get("selected_response") == "public_browser"
                else "content_type",
                "UNKNOWN",
            )
            if content_type != "UNKNOWN" and not re.match(
                r"(?i)(?:text/html|application/xhtml\+xml)(?:;|$)", content_type
            ):
                raise SearchError("INVALID_RESPONSE: topic response is not HTML")
            if not self.solver_base:
                candidate.timings["direct_http_seconds"] = float(
                    candidate.detail_diagnostics.get("duration_seconds", 0.0)
                )
            started = time.monotonic()
            try:
                parse_details(candidate, raw, self.detail_secrets())
            finally:
                candidate.timings["parsing_seconds"] = round(time.monotonic() - started, 6)
            if candidate.detail_diagnostics.get("selected_response") == "public_browser":
                candidate.detail_status = "FOUND_PUBLIC"
        except SearchError as exc:
            candidate.detail_status = str(exc).split(":", 1)[0]
            candidate.warnings.append(self.public_text(exc))
        if not self.solver_base and candidate.detail_diagnostics:
            candidate.timings["direct_http_seconds"] = float(candidate.detail_diagnostics.get("duration_seconds", 0.0))
        if candidate.detail_diagnostics:
            candidate.detail_diagnostics = {
                key: self.public_text(value) if isinstance(value, str) else value
                for key, value in candidate.detail_diagnostics.items()
            }
            candidate.warnings.append("Topic acquisition: " + json.dumps(candidate.detail_diagnostics, sort_keys=True))
        if candidate.detail_status in {"FOUND", "FOUND_PUBLIC"}:
            state = candidate.public()
            self._detail_memory[key] = deepcopy(state)
            if self._detail_cache is not None:
                try:
                    self._detail_cache.store(key, state)
                except OSError:
                    candidate.warnings.append("Detail cache write failed; acquired evidence retained.")

    @staticmethod
    def _apply_detail_cache(candidate: Candidate, cached: dict[str, Any]) -> bool:
        # Cache is an optimization, never a trust or identity boundary.
        if (
            cached.get("candidate_id") != candidate.candidate_id
            or cached.get("source_url") != candidate.source_url
            or cached.get("torrent_title") != candidate.torrent_title
            or cached.get("detail_status") not in {"FOUND", "FOUND_PUBLIC"}
        ):
            return False
        evidence = cached.get("evidence")
        if not isinstance(evidence, dict) or not all(
            isinstance(v, dict) and all(isinstance(v.get(k), str) for k in ("source", "claim", "status"))
            for v in evidence.values()
        ):
            return False
        claims = cached.get("claims", [])
        if not isinstance(claims, list) or not all(
            isinstance(c, dict)
            and isinstance(c.get("source"), str)
            and isinstance(c.get("field"), str)
            and isinstance(c.get("status"), str)
            for c in claims
        ):
            return False
        if not isinstance(cached.get("warnings", []), list):
            return False
        fields = (*LABELS, "lossless", "track_list", "track_count", "rip_setup", "years_stated")
        for name in fields:
            if evidence.get(name, {}).get("source") not in {"topic_detail", "parsed_topic_list", "topic_heading"}:
                continue
            value = cached.get(name)
            if name in {"track_list", "years_stated"}:
                valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
            elif name == "rip_setup":
                valid = isinstance(value, dict) and all(
                    isinstance(k, str) and isinstance(v, str) for k, v in value.items()
                )
            elif name == "track_count":
                valid = type(value) is int and value >= 0
            elif name in {"complete", "lossless"}:
                valid = value is None or type(value) is bool
            else:
                valid = value is None or isinstance(value, str)
            if not valid:
                return False
        for name in fields:
            if evidence.get(name, {}).get("source") in {"topic_detail", "parsed_topic_list", "topic_heading"}:
                setattr(candidate, name, deepcopy(cached.get(name)))
                candidate.evidence[name] = deepcopy(evidence[name])
        for claim in claims:
            if (
                claim.get("source") == "topic_detail"
                and claim.get("status") == "conflicting stated claim; unresolved"
                and claim.get("field") in LABELS
            ):
                setattr(candidate, claim["field"], None)
                candidate.evidence.pop(claim["field"], None)
                if claim["field"] == "codec":
                    candidate.lossless = None
                    candidate.evidence.pop("lossless", None)
        candidate.claims.extend(deepcopy([c for c in claims if c.get("source") in {"topic_detail", "topic_heading"}]))
        unmapped = cached.get("unmapped_description_fields", [])
        if isinstance(unmapped, list) and all(
            isinstance(c, dict) and all(isinstance(v, str) for v in c.values()) for c in unmapped
        ):
            candidate.unmapped_description_fields = deepcopy(unmapped)
        candidate.warnings.extend(
            w
            for w in cached.get("warnings", [])
            if isinstance(w, str) and w.startswith(("Conflicting", "Multiple years", "Authenticated HTTP"))
        )
        candidate.detail_status = cached["detail_status"]
        candidate.extraction_confidence = (
            "explicit_topic_fields"
            if any(e.get("source") == "topic_detail" for e in evidence.values())
            else "title_only"
        )
        candidate.evidence_completeness = "cached_accessible_description_partial"
        candidate.detail_diagnostics = (
            deepcopy(cached.get("detail_diagnostics", {})) if isinstance(cached.get("detail_diagnostics"), dict) else {}
        )
        candidate.extraction_gaps = (
            deepcopy(cached.get("extraction_gaps", {})) if isinstance(cached.get("extraction_gaps"), dict) else {}
        )
        return True

    def resolve_download(self, candidate: Candidate) -> Download:
        from search_torrent import validate_magnet, validate_torrent

        ref = self.download_refs.get(candidate.candidate_id, "")
        if ref.startswith("magnet:?"):
            return validate_magnet(ref)
        if len(ref) > 16_000:
            raise SearchError("INVALID_DOWNLOAD: download reference exceeds safety bound")
        parsed = urlsplit(ref)
        if origin(ref) == origin(self.base) and parsed.path.rstrip("/") == urlsplit(self.base).path + "/dl/rutracker":
            parameters = parse_qs(parsed.query, max_num_fields=10)
            if parameters.get("jackett_apikey") != [self.key] or not parameters.get("path"):
                raise SearchError("INVALID_DOWNLOAD: incomplete/current Jackett proxy credentials/path")
            data = self.http.request("GET", ref, max_bytes=8_000_000, magnet_redirect=True)
            if data.startswith(b"magnet:?"):
                return validate_magnet(data.decode("utf-8"))
        elif (
            origin(ref) == origin(self.topic_base)
            and parsed.path == urlsplit(self.topic_base).path + "/dl.php"
            and parse_qs(parsed.query).get("t") == [candidate.candidate_id.split(":")[1]]
        ):
            cookie = os.environ.get(self.policy.topic_cookie_env, "")
            if not cookie:
                raise SearchError("NOT_CONFIGURED: authorized topic cookie required for direct download")
            data = self.http.request("GET", ref, headers={"Cookie": cookie}, max_bytes=8_000_000)
        else:
            raise SearchError("Download reference is not a supported trusted Jackett/RuTracker route")
        return validate_torrent(data)
