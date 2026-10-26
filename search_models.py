"""Public, credential-free search evidence; preferences are not audio measurements."""

from __future__ import annotations

import csv
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import asdict, dataclass, field
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pipeline_config import SearchPolicy
from process_lock import file_lock
from provider_cache import redact_secrets


class SearchError(Exception):
    """Safe user-facing diagnostics; never include response bodies/URLs with credentials."""


def secret_values(policy: SearchPolicy) -> tuple[str, ...]:
    names = (
        "jackett_key_env",
        "research_key_env",
        "topic_cookie_env",
        "openai_key_env",
        "gemini_key_env",
        "local_key_env",
        "qbittorrent_password_env",
    )
    values = [value for name in names if (value := os.environ.get(getattr(policy, name), ""))]
    cookies = SimpleCookie()
    try:
        cookies.load(os.environ.get(policy.topic_cookie_env, ""))
    except CookieError:
        pass
    values.extend(morsel.value for morsel in cookies.values() if len(morsel.value) >= 8)
    return tuple(values)


def public_text(value: object, extra_secrets: tuple[str, ...] = ()) -> str:
    text = str(value)
    for secret in extra_secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", text)
    for key, secret in os.environ.items():
        if len(secret) >= 8 and re.search(r"KEY|TOKEN|PASSWORD|COOKIE|PASSKEY|SECRET", key, re.IGNORECASE):
            text = text.replace(secret, "[REDACTED]")
    text = redact_secrets(text)
    # Authenticated URLs and magnets are not public evidence, even in quoted text.
    text = re.sub(r"(?i)(?:https?://|magnet:\?)[^\s<>\"']+", "[URL omitted]", text)
    text = re.sub(r"(?i)\b(?:passkey|jackett_apikey|sp_dc|sid)\s*[=:]\s*[^\s,;]+", "[REDACTED]", text)
    return text if len(text) <= 4000 else text[:4000] + " [TRUNCATED at 4000 characters]"


def norm(text: str) -> str:
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold()))


def public_evidence_url(value: object, domains: tuple[str, ...]) -> str | None:
    if not isinstance(value, str) or len(value) > 1500 or re.search(r"[\s\x00-\x1f\x7f-\x9f]", value):
        return None
    try:
        p = urlsplit(value)
        if p.scheme != "https" or p.hostname not in domains or p.username or p.password or p.port not in {None, 443}:
            return None
    except ValueError:
        return None
    goto = (
        p.hostname == "forums.stevehoffman.tv"
        and p.path == "/goto/post"
        and re.fullmatch(r"id=[0-9]{1,20}", p.query) is not None
        and not p.fragment
    )
    if (
        (p.query and not goto)
        or (p.fragment and not re.fullmatch(r"post-\d+", p.fragment))
        or re.search(r"(?i)token|passkey|logout|login", p.path)
    ):
        return None
    if any(
        len(secret) >= 8 and secret in value
        for name, secret in os.environ.items()
        if re.search(r"KEY|TOKEN|PASSWORD|COOKIE|SECRET", name, re.IGNORECASE)
    ):
        return None
    return urlunsplit((p.scheme, p.netloc, p.path, p.query if goto else "", p.fragment))


def claim_identity(name: str, value: object) -> str:
    """Comparison only; retain every original statement and its provenance."""
    normalized = norm(str(value))
    if name == "country":
        return {
            "holland": "netherlands",
            "the netherlands": "netherlands",
            "usa": "united states",
            "us": "united states",
            "uk": "united kingdom",
        }.get(normalized, normalized)
    if name == "medium":
        # Packaging/role attributes do not change the stated source medium.
        words = set(normalized.split())
        if words & {"lp", "vinyl"} and not words & {"cd", "sacd", "digital"}:
            return "vinyl"
    return normalized


@dataclass
class Candidate:
    candidate_id: str
    source_url: str
    torrent_title: str
    artist: str | None = None
    release_title: str | None = None
    release_type: str | None = None
    release_year: str | None = None
    original_album_year: str | None = None
    label: str | None = None
    catalog_number: str | None = None
    edition: str | None = None
    country: str | None = None
    mastering: str | None = None
    medium: str | None = None
    codec: str | None = None
    lossless: bool | None = None
    bit_depth: str | None = None
    sample_rate: str | None = None
    channels: str | None = None
    size_bytes: int | None = None
    seeders: int | None = None
    leechers: int | None = None
    published_at: str | None = None
    rip_log: str | None = None
    verification_evidence: str | None = None
    processing: str | None = None
    complete: bool | None = None
    bonus_tracks: str | None = None
    disc_count: str | None = None
    artwork: str | None = None
    track_list: list[str] = field(default_factory=list)
    track_count: int | None = None
    detail_status: str = "SUMMARY_ONLY"
    detail_diagnostics: dict[str, Any] = field(default_factory=dict)
    extraction_confidence: str = "title_only"
    identity_confidence: str = "unknown"
    evidence: dict[str, dict[str, str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    eligible: bool = True
    rejection_reasons: list[str] = field(default_factory=list)
    recommendation_rank: int | None = None
    recommendation_explanation: str | None = None
    title_parse_status: str = "unparsed"
    selection_stage: str = "discovered"
    preliminary_status: str = "pending"
    preliminary_reasons: list[str] = field(default_factory=list)
    preliminary_evaluation_status: str = "not_requested"
    preliminary_rank: int | None = None
    preliminary_explanation: str | None = None
    final_evaluation_status: str = "not_requested"
    evaluation_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    evidence_completeness: str = "details_not_received"
    years_stated: list[str] = field(default_factory=list)
    claims: list[dict[str, Any]] = field(default_factory=list)
    extraction_gaps: dict[str, str] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    search_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    rip_type: str | None = None
    recording_format: str | None = None
    distribution_format: str | None = None
    reported_dr: str | None = None
    rip_setup: dict[str, str] = field(default_factory=dict)
    source_origin: str | None = None
    unmapped_description_fields: list[dict[str, str]] = field(default_factory=list)

    matrix: str | None = None
    mastering_credit: str | None = None
    research_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    research_status: str = "not_configured"
    external_sources: list[dict[str, Any]] = field(default_factory=list)
    mastering_decision: dict[str, Any] = field(default_factory=dict)
    mastering_rank: int | None = None
    practical_rank: int | None = None
    mastering_group_id: str | None = None

    def claim(self, name: str, value: Any, source: str, original: str) -> None:
        previous = self.evidence.get(name, {})
        if (
            source == "topic_detail"
            and previous.get("source") in {"search_title", "topic_heading"}
            and value is not None
            and claim_identity(name, getattr(self, name)) != claim_identity(name, value)
        ):
            self.warnings.append(f"Title/description claims differ for {name}; compare retained provenance.")
        setattr(self, name, value)
        self.evidence[name] = {"source": source, "claim": public_text(original), "status": "stated, unmeasured"}
        self.claims.append({"field": name, "value": value, **self.evidence[name]})

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Query:
    text: str
    artist: str = ""
    title: str = ""
    release_type: str = ""

    @property
    def search_text(self) -> str:
        return " ".join(part for part in (self.text, self.artist, self.title) if part).strip()


def resolve_research_query(query: Query, candidates: list[Candidate]) -> Query:
    """Fill missing identifiers only when suitable release evidence agrees."""
    pairs = {
        (c.artist, c.release_title)
        for c in candidates
        if c.eligible
        and c.artist
        and c.release_title
        and (not query.artist or norm(c.artist) == norm(query.artist))
        and (not query.title or norm(c.release_title) == norm(query.title))
        and (not query.text or set(norm(query.text).split()) <= set(norm(c.artist + " " + c.release_title).split()))
    }
    if len(pairs) != 1:
        return query
    artist, title = next(iter(pairs))
    return Query(query.text, query.artist or artist or "", query.title or title or "", query.release_type)


def assess_relevance(candidate: Candidate, query: Query, policy: SearchPolicy, *, preliminary: bool = False) -> None:
    candidate.rejection_reasons = []
    combined = norm(" ".join((candidate.artist or "", candidate.release_title or "", candidate.torrent_title)))
    for label, wanted, supplied in (
        ("artist", query.artist, candidate.artist),
        ("title", query.title, candidate.release_title),
    ):
        if wanted:
            haystack = norm(supplied) if supplied else combined
            if preliminary and not supplied:
                continue  # Missing/ambiguous parse is not negative identity evidence.
            if (supplied and norm(wanted) != haystack) or (
                not supplied and not set(norm(wanted).split()) <= set(haystack.split())
            ):
                candidate.rejection_reasons.append(f"requested {label} conflicts with available evidence")
    identity_text = (
        norm(" ".join((candidate.artist or "", candidate.release_title or "")))
        if all(candidate.evidence.get(name, {}).get("source") == "topic_detail" for name in ("artist", "release_title"))
        else combined
    )
    if (
        query.text
        and (not preliminary or candidate.title_parse_status == "parsed")
        and not set(norm(query.text).split()) <= set(identity_text.split())
    ):
        candidate.rejection_reasons.append("free-text work evidence does not match")
    kind = norm(candidate.release_type or "")
    wanted_text = norm(query.search_text)
    for marker in ("discography", "дискография", "tribute", "трибьют", "remix", "live", "compilation", "сборник"):
        if marker in (combined + " " + kind).split() and marker not in wanted_text.split():
            candidate.rejection_reasons.append(f"unrequested {marker} release")
    primary_kind = {
        "studio album": "album",
        "live album": "album",
        "compilation album": "album",
        "compilation": "album",
        "remix album": "album",
        "tribute album": "album",
        "maxi single": "single",
        "digital single": "single",
        "live single": "single",
    }.get(kind, kind)
    if query.release_type and primary_kind and primary_kind != query.release_type:
        candidate.rejection_reasons.append("release type conflicts with album/single filter")
    if query.release_type == "album" and not kind and re.search(r"\b(single|сингл)\b", combined):
        candidate.rejection_reasons.append("single conflicts with requested album")
    if candidate.complete is False and not policy.allow_partial:
        candidate.rejection_reasons.append("stated partial release is excluded by policy")
    if candidate.processing:
        # A keyword in a denial is not positive processing evidence. Scope
        # negation to one clause so "no upsampling; from MP3" still rejects.
        clauses = re.split(r"[;,\n]|\b(?:but|and|но|однако|и)\b", candidate.processing, flags=re.IGNORECASE)
        for clause in clauses:
            match = re.search(
                r"lossy[ -]source|from\s+(?:mp3|aac|ogg|opus)|из\s+(?:mp3|aac|ogg|opus)|fake\s+upsampl|апсемпл",
                clause,
                re.IGNORECASE,
            )
            if match is None:
                continue
            negated = re.search(
                r"\b(?:no|not|without|нет|не|без)\b", clause[: match.start()], re.IGNORECASE
            ) or re.match(r"\s*:\s*(?:no|нет)\b", clause[match.end() :], re.IGNORECASE)
            if not negated:
                candidate.rejection_reasons.append("stated unwanted/lossy-source processing")
                break
    candidate.eligible = not candidate.rejection_reasons
    candidate.identity_confidence = (
        "stated"
        if all(
            candidate.evidence.get(name, {}).get("source") == "topic_detail" and getattr(candidate, name)
            for name in ("artist", "release_title")
        )
        else "tentative"
    )
    candidate.missing_information = [
        name
        for name in ("artist", "release_title", "release_type", "mastering", "medium", "codec", "complete", "rip_log")
        if getattr(candidate, name) is None
    ]
    if candidate.identity_confidence == "tentative":
        candidate.warnings.append("Query/work identity remains ambiguous; title matching alone is not verification.")
    candidate.warnings = sorted(
        set(candidate.warnings + ["Topic technical/mastering claims are not verified audio measurements."])
    )
    if preliminary and candidate.selection_stage != "final":
        candidate.preliminary_status = (
            "explicit_mismatch"
            if not candidate.eligible
            else "suitable"
            if candidate.artist and candidate.release_title
            else "ambiguous"
        )
        candidate.preliminary_reasons = list(candidate.rejection_reasons)
        candidate.selection_stage = "preliminary"


def rip_evidence_fields(candidate: Candidate) -> tuple[str, ...]:
    """Reported absence/unknown is retained, but cannot count as quality evidence."""
    missing = {
        "",
        "no",
        "none",
        "n a",
        "unknown",
        "unavailable",
        "absent",
        "missing",
        "not available",
        "not provided",
        "not supplied",
        "not verified",
        "unverified",
        "нет",
        "отсутствует",
        "неизвестно",
        "не предоставлен",
        "лог отсутствует",
    }
    fields = []
    for name in ("rip_log", "verification_evidence"):
        text = norm(getattr(candidate, name) or "")
        if text in missing or re.fullmatch(
            r"(?:no|without) (?:rip )?(?:logs?|checksums?|verification)(?: (?:available|provided|supplied))?", text
        ):
            continue
        fields.append(name)
    return tuple(fields)


def preference_order(candidates: list[Candidate], policy: SearchPolicy) -> list[Candidate]:
    def key(c: Candidate) -> tuple[Any, ...]:
        return (
            c.identity_confidence == "stated",
            c.complete is True,
            (1 if c.lossless is True else -1 if c.lossless is False else 0) if policy.prefer_lossless else 0,
            bool(rip_evidence_fields(c)) if policy.prefer_documented_rip else False,
            min(c.seeders or 0, 10),
            c.candidate_id,
        )

    # Stable ID resolves equal preferences; never use resolution, size or branding.
    return sorted((c for c in candidates if c.eligible), key=key, reverse=True)


CSV_COLUMNS = tuple(Candidate.__dataclass_fields__)


def csv_cell(value: object, *, evidence_urls: frozenset[str] = frozenset()) -> str:
    if value is None:
        return "UNKNOWN"
    if isinstance(value, (list, dict)):

        def sanitize(item: Any) -> Any:
            if isinstance(item, dict):
                return {public_text(k): sanitize(v) for k, v in item.items()}
            if isinstance(item, list):
                return [sanitize(v) for v in item]
            return (
                item
                if isinstance(item, str) and item in evidence_urls
                else public_text(item)
                if isinstance(item, str)
                else item
            )

        text = json.dumps(sanitize(value), ensure_ascii=False, sort_keys=True)
        return text
    else:
        text = str(value)
    # The only exported URL is the canonical, credential-free topic URL.
    if not re.fullmatch(r"https?://[^?#@]+/viewtopic\.php\?t=\d+", text):
        text = public_text(text)
    if text.lstrip("\ufeff \t\r\n\v\f").startswith(("=", "+", "-", "@")):
        text = "'" + text
    return text


def export_csv(path: Path, candidates: list[Candidate]) -> None:
    # Publish a complete export only. A failed reevaluation export cannot destroy
    # the successful pre-evaluation CSV or mix rows with a concurrent writer.
    if path.suffix.casefold() != ".csv":
        raise SearchError("CSV output must use a .csv filename; audio/documents cannot be export targets")
    with file_lock(path):
        if path.is_symlink():
            raise SearchError("Refusing a symlinked CSV output")
        if path.exists():
            if not path.is_file():
                raise SearchError("CSV output must be a regular file")
            try:
                with path.open(encoding="utf-8-sig", newline="") as previous:
                    header = next(csv.reader([previous.readline(16_384)]), [])
            except (UnicodeError, csv.Error):
                raise SearchError("Existing file is not a readable search CSV export; choose another output") from None
            if not {"candidate_id", "source_url", "torrent_title"} <= set(header):
                raise SearchError("Refusing to overwrite an unrelated CSV; choose another export filename")
        descriptor, name = tempfile.mkstemp(prefix=".auph-search-", suffix=".csv", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
                writer.writeheader()
                for candidate in sorted(candidates, key=lambda c: c.candidate_id):
                    # Only explicitly retained public evidence URLs may survive nested redaction.
                    urls = frozenset(
                        url
                        for source in candidate.external_sources
                        for field_name in ("url", "original_url", "final_url")
                        if isinstance((url := source.get(field_name)), str)
                        and public_evidence_url(url, (urlsplit(url).hostname or "",))
                    )
                    writer.writerow(
                        {
                            key: csv_cell(
                                value,
                                evidence_urls=urls
                                if key in {"external_sources", "mastering_decision"}
                                else frozenset(),
                            )
                            for key, value in candidate.public().items()
                        }
                    )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
