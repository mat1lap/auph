#!/usr/bin/env python3
"""High-performance multi-provider cover art fetcher, aesthetic evaluator, and Kitty display engine.

Discovers, ranks, and retrieves the highest quality and most aesthetically pleasing
album artwork across:
1. Apple Music / iTunes provider-supplied artwork representations.
2. Cover Art Archive (MusicBrainz Release-Group & Release curated front scans).
3. Deezer provider-supplied artwork representations.
4. Discogs API (physical release & master release scans).
5. Existing local cover art (scans directory, artwork directory, local high-res files).

Evaluates candidate images on:
- Pixel resolution (minimum dimension)
- Aspect ratio squareness (1:1 standard with smooth penalty)
- Optical edge sharpness via normalized Laplacian variance with noise pre-filtering
- Dynamic range / contrast standard deviation
- Tonal transition entropy (Shannon entropy)
- Scanner border / letterboxing artifact detection (outer 1% margin)
- Title purity & deluxe/anniversary badge filtering

Separates candidates into two core streams:
1. Best Digital Service (Apple Music, Deezer, etc.)
2. Best Physical Scan (Cover Art Archive, Discogs, local optical scans)
And determines the absolute highest-scoring master cover for embedding.

Provides native Kitty terminal rendering (via kitten icat or direct Kitty Graphics Protocol).
"""

from __future__ import annotations

import argparse
import base64
import binascii
import concurrent.futures
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field, replace
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any

import numpy as np
import requests
from mutagen import MutagenError
from PIL import Image, ImageCms, ImageDraw, ImageFilter, ImageOps, ImageStat, UnidentifiedImageError

from provider_cache import retry_delay

USER_AGENT = "AudiophileArchTagger/6.0 (audiophile@music-archive.org)"
TIMEOUT = 12
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 80_000_000
MAX_UNKNOWN_ROLE_CANDIDATES_PER_PROVIDER = 12
MAX_TITLE_ONLY_CANDIDATES_PER_PROVIDER = 6
VISUAL_EQUIVALENCE_MARGIN = 2.0
MIN_USABLE_COVER_SIDE = 500
UPSCALE_PENALTY_THRESHOLD = 0.60


def _actionable_upscale_confidence(confidence: float) -> float:
    """Ignore weak texture-only upscale evidence; retain strong evidence as a soft penalty."""
    return max(0.0, min(1.0, (confidence - UPSCALE_PENALTY_THRESHOLD) / (1.0 - UPSCALE_PENALTY_THRESHOLD)))


class ReleaseTier(IntEnum):
    CONFLICT = 0
    TITLE_ONLY = 1
    RELEASE_GROUP = 2
    PROBABLE_RELEASE = 3
    EXACT_RELEASE = 4


class RoleTier(IntEnum):
    REJECT = 0
    UNKNOWN = 1
    LIKELY_FRONT = 2
    EXPLICIT_FRONT = 3


class RepresentationKind(StrEnum):
    ORIGINAL = "original"
    PREVIEW = "preview"
    THUMBNAIL = "thumbnail"
    LOCAL = "local"
    UNKNOWN = "unknown"


class ArtworkProvenance(StrEnum):
    DIGITAL_SERVICE = "digital_service"
    PHYSICAL_SCAN = "physical_scan"
    LOCAL = "local"
    UNKNOWN = "unknown"


def artwork_provenance(provider: str) -> ArtworkProvenance:
    """Classify only evidence supplied by the source, never a UI slot."""
    if provider.startswith("Local Optical Scan"):
        return ArtworkProvenance.PHYSICAL_SCAN
    if provider.startswith("Local"):
        return ArtworkProvenance.LOCAL
    if is_digital_provider(provider):
        return ArtworkProvenance.DIGITAL_SERVICE
    # CAA and Discogs accept uploads of several origins. Their provider name
    # establishes a repository, not whether pixels came from a master or scan.
    return ArtworkProvenance.UNKNOWN


def artwork_label(candidate: CoverCandidate) -> str:
    provenance = candidate.provenance
    if provenance == ArtworkProvenance.DIGITAL_SERVICE:
        base = "Digital Service Artwork"
    elif provenance == ArtworkProvenance.PHYSICAL_SCAN:
        base = "Physical Scan"
    elif provenance == ArtworkProvenance.LOCAL:
        base = "Local Artwork"
    elif "cover art archive" in candidate.provider.casefold():
        base = "Cover Art Archive Artwork"
    elif "discogs" in candidate.provider.casefold():
        base = "Discogs Artwork"
    else:
        base = "Artwork — Source Unknown"
    return f"{base} — Extracted Front" if candidate.crop_box and candidate.crop_confidence >= 0.8 else base


def _apply_visual_provenance(candidate: CoverCandidate) -> None:
    """A verified package boundary can identify an otherwise unlabelled scan."""
    if (
        candidate.provenance == ArtworkProvenance.UNKNOWN
        and candidate.source_form == "compound_package_scan"
        and candidate.crop_box
        and candidate.crop_confidence >= 0.85
        and ("cover art archive" in candidate.provider.casefold() or "discogs" in candidate.provider.casefold())
    ):
        candidate.provenance = ArtworkProvenance.PHYSICAL_SCAN
        candidate.provenance_confidence = max(candidate.provenance_confidence, candidate.crop_confidence)


@dataclass(frozen=True)
class ImageRepresentation:
    url: str
    kind: RepresentationKind
    generated: bool = False
    declared_width: int = 0
    declared_height: int = 0


def norm_str(s: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFC", s).casefold(), flags=re.UNICODE)


def clean_album_title(title: str) -> str:
    cleaned = title.split("=")[0].strip()
    cleaned = re.sub(
        r"\s*\(.*?((?:19|20)\d{2}|deluxe|remaster|edition|version|anniversary|disc\s*\d+|cd\s*\d+).*?\)",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\s*\[.*?((?:19|20)\d{2}|deluxe|remaster|edition|version|anniversary|disc\s*\d+|cd\s*\d+).*?\]",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip()


def artist_matches(query_artist: str, cand_artist: str) -> bool:
    q = norm_str(query_artist)
    c = norm_str(cand_artist)
    if not q or not c:
        return False
    if q == c:
        return True
    q_words = [w for w in re.findall(r"\w+", query_artist.lower()) if w]
    c_words = [w for w in re.findall(r"\w+", cand_artist.lower()) if w]
    if len(q_words) == 1:
        first_q = q_words[0]
        if c_words and c_words[0] == first_q:
            if len(c_words) == 1 or c_words[1] in ("and", "feat", "featuring", "with"):
                return True
            rest = cand_artist.lower()[len(first_q) :].strip()
            if rest.startswith(("&", "+", "/", "x", "feat")):
                return True
        return False
    return set(q_words).issubset(set(c_words))


def compute_album_match(query_album: str, cand_title: str) -> float:
    q_clean = clean_album_title(query_album)
    q_norm = norm_str(q_clean)
    c_norm = norm_str(cand_title)
    if not q_norm or not c_norm:
        return 0.0
    if q_norm == c_norm:
        return 1.0

    q_alt = re.sub(r"\bii\b", "2", q_clean.lower())
    q_alt = re.sub(r"\biii\b", "3", q_alt)
    c_alt = re.sub(r"\bii\b", "2", cand_title.lower())
    c_alt = re.sub(r"\biii\b", "3", c_alt)
    if norm_str(q_alt) == norm_str(c_alt):
        return 1.0

    if q_norm in c_norm or norm_str(q_alt) in norm_str(c_alt):
        return 0.95

    q_words = set(re.findall(r"\w+", q_clean.lower()))
    c_words = set(re.findall(r"\w+", cand_title.lower()))
    if q_words and q_words.issubset(c_words):
        return 0.9

    overlap = len(q_words & c_words) / max(len(q_words), 1)
    if overlap >= 0.7:
        return 0.7
    return 0.0


def _title_release_conflicts(query_album: str, candidate_title: str) -> tuple[str, ...]:
    query = f" {query_album.casefold()} "
    candidate = f" {candidate_title.casefold()} "
    markers = (
        "deluxe",
        "anniversary",
        "remaster",
        "remastered",
        "expanded",
        "collector",
        "karaoke",
        "tribute",
        "instrumental",
        "greatest hits",
        "best of",
        "single",
        " ep ",
        "mono",
    )
    return tuple(marker.strip() for marker in markers if marker in candidate and marker not in query)


def is_digital_provider(provider: str) -> bool:
    p = provider.lower()
    return any(x in p for x in ("apple music", "itunes", "deezer", "spotify", "tidal", "qobuz"))


def is_scan_provider(provider: str) -> bool:
    return artwork_provenance(provider) == ArtworkProvenance.PHYSICAL_SCAN


def should_replace_existing_cover(
    selected: CoverCandidate | None,
    existing: CoverCandidate | None,
    *,
    manual: bool = False,
    ignore_existing: bool = False,
) -> bool:
    if selected is None or existing is None or not selected.raw_data or not existing.raw_data:
        return False
    if selected.source_sha256 and selected.source_sha256 == existing.source_sha256:
        return False
    if selected.raw_data == existing.raw_data:
        return False
    if manual:
        return True
    if ignore_existing:
        return selected.release_tier == ReleaseTier.EXACT_RELEASE and selected.role_tier >= RoleTier.LIKELY_FRONT
    return bool(
        selected.release_tier == ReleaseTier.EXACT_RELEASE
        and selected.role_tier >= RoleTier.LIKELY_FRONT
        and selected.visual_quality_score > existing.visual_quality_score + VISUAL_EQUIVALENCE_MARGIN
    )


def _looks_like_json(data: bytes) -> bool:
    return data.lstrip()[:1] in {b"{", b"["}


def _image_header_is_supported(data: bytes) -> bool:
    return data.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")) or (
        len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    )


def _url_mbid(url: str) -> str:
    match = re.search(r"/release/([0-9a-f-]{36})(?:/|$)", url, re.IGNORECASE)
    return match.group(1).lower() if match else ""


def _url_numeric_image_id(url: str) -> str:
    match = re.search(r"/(\d+)(?:-(?:250|500|1200))?\.[a-z0-9]+(?:\?|$)", url, re.IGNORECASE)
    return match.group(1) if match else ""


def _discogs_image_id(url: str) -> str:
    direct = re.search(r"/R-(\d+)-(\d+)-(\d+)\.", url, re.IGNORECASE)
    if direct:
        return "-".join(direct.groups())
    encoded = re.search(
        r"/(?:w|h):\d+/(?![a-z]+:\d+/)(.+)\.(?:jpe?g|png)(?:\?|$)",
        url,
        re.IGNORECASE,
    )
    if encoded:
        try:
            token = encoded.group(1).replace("/", "")
            token += "=" * (-len(token) % 4)
            decoded = base64.urlsafe_b64decode(token).decode("utf-8", errors="ignore")
            asset = re.search(r"/R-(\d+)-(\d+)-(\d+)\.", decoded, re.IGNORECASE)
            if asset:
                return "-".join(asset.groups())
        except (ValueError, TypeError, binascii.Error):
            pass
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _normalized_types(values: object) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    return tuple(str(value).strip().casefold() for value in values if str(value).strip())


def _compatibility_rgb(im: Image.Image) -> tuple[Image.Image, bytes | None]:
    profile = im.info.get("icc_profile")
    if isinstance(profile, bytes):
        try:
            source = ImageCms.ImageCmsProfile(io.BytesIO(profile))
            target = ImageCms.createProfile("sRGB")
            converted = ImageCms.profileToProfile(im, source, target, outputMode="RGB")
            if converted is not None:
                return converted, ImageCms.ImageCmsProfile(target).tobytes()
        except (OSError, ValueError, ImageCms.PyCMSError):
            pass
    if im.mode in {"RGBA", "LA", "P"}:
        rgba = im.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        return Image.alpha_composite(background, rgba).convert("RGB"), None
    return im.convert("RGB"), profile if im.mode == "RGB" and isinstance(profile, bytes) else None


@dataclass
class CoverCandidate:
    provider: str
    url: str
    title: str
    album_match_ratio: float
    raw_data: bytes | None = None
    width: int = 0
    height: int = 0
    aspect_ratio: float = 1.0
    sharpness: float = 0.0
    contrast: float = 0.0
    entropy: float = 0.0
    colorfulness: float = 0.0
    dynamic_range: float = 0.0
    blockiness: float = 1.0
    is_fake_upscale: bool = False
    has_scanner_borders: bool = False
    badge_penalty: float = 0.0
    total_score: float = -9999.0
    file_size: int = 0
    format: str = "JPEG"
    error: str = ""
    local_saved_path: Path | None = None
    image_type: str = "front"
    identity_scope: str = "concept"
    provider_image_id: str = ""
    provider_release_id: str = ""
    catalog_numbers: tuple[str, ...] = ()
    barcode: str = ""
    release_country: str = ""
    release_year: str = ""
    media_format: str = ""
    musicbrainz_release_id: str = ""
    musicbrainz_release_group_id: str = ""
    discogs_release_id: str = ""
    original_url: str = ""
    preview_urls: dict[str, str] = field(default_factory=dict)
    representations: tuple[ImageRepresentation, ...] = ()
    release_key: str = ""
    source_verified_original: bool = False
    representation_kind: RepresentationKind = RepresentationKind.UNKNOWN
    provenance: ArtworkProvenance = ArtworkProvenance.UNKNOWN
    source_form: str = "source_image_unverified"
    generated_representation: bool = False
    provider_order: int = 0
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    approved: bool | None = None
    comment: str = ""
    roles: tuple[str, ...] = ("front",)
    front_flag: bool | None = None
    back_flag: bool | None = None
    role_tier: RoleTier = RoleTier.UNKNOWN
    role_confidence: float = 0.5
    role_evidence: tuple[str, ...] = ()
    release_tier: ReleaseTier = ReleaseTier.TITLE_ONLY
    release_confidence: float = 0.4
    release_evidence: tuple[str, ...] = ()
    release_conflicts: tuple[str, ...] = ()
    declared_width: int = 0
    declared_height: int = 0
    mime_type: str = ""
    oriented_width: int = 0
    oriented_height: int = 0
    effective_width: int = 0
    effective_height: int = 0
    effective_pixels: int = 0
    has_alpha: bool = False
    has_icc_profile: bool = False
    icc_profile_hash: str = ""
    source_sha256: str = ""
    evaluated_url: str = ""
    perceptual_hash: str = ""
    difference_hash: str = ""
    alternate_hashes: list[tuple[str, str]] = field(default_factory=list)
    duplicate_cluster_id: str = ""
    duplicate_of: str = ""
    crop_box: tuple[int, int, int, int] | None = None
    crop_confidence: float = 0.0
    crop_penalty: float = 0.0
    detail_score: float = 0.0
    blur_score: float = 0.0
    compression_score: float = 50.0
    artifact_score: float = 50.0
    tonal_integrity_score: float = 50.0
    color_integrity_score: float = 50.0
    cleanliness_score: float = 50.0
    upscale_confidence: float = 0.0
    watermark_confidence: float = 0.0
    skew_degrees: float = 0.0
    skew_confidence: float = 0.0
    moire_confidence: float = 0.0
    streak_confidence: float = 0.0
    metric_confidence: dict[str, float] = field(default_factory=dict)
    provenance_confidence: float = 0.5
    visual_quality_score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)
    rejection_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    selection_explanation: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.provenance == ArtworkProvenance.UNKNOWN:
            self.provenance = artwork_provenance(self.provider)
        if not self.original_url:
            self.original_url = self.url
        if self.identity_scope == "local" and self.release_tier == ReleaseTier.TITLE_ONLY:
            self.release_tier = ReleaseTier.EXACT_RELEASE
            self.release_confidence = max(self.release_confidence, 0.95)
        if not self.release_key:
            if self.musicbrainz_release_id:
                self.release_key = f"mb:{self.musicbrainz_release_id.casefold()}"
            elif self.discogs_release_id:
                self.release_key = f"discogs:{self.discogs_release_id}"
            elif self.identity_scope == "local":
                self.release_key = "local:target"
        if self.image_type.casefold() != "front" and self.role_tier == RoleTier.UNKNOWN:
            self.role_tier = RoleTier.REJECT
            self.role_confidence = 1.0
        elif self.front_flag is True and self.back_flag is not True:
            self.role_tier = RoleTier.EXPLICIT_FRONT
            self.role_confidence = max(self.role_confidence, 0.98)
        elif self.role_tier == RoleTier.UNKNOWN and self.roles != ("unknown",):
            self.role_tier = RoleTier.LIKELY_FRONT
            self.role_confidence = max(self.role_confidence, 0.7)
        if self.representation_kind == RepresentationKind.UNKNOWN:
            if self.provider.startswith("Local"):
                self.representation_kind = RepresentationKind.LOCAL
                self.source_verified_original = True
        if not self.representations:
            source = ImageRepresentation(
                self.original_url,
                self.representation_kind,
                self.generated_representation,
                self.declared_width,
                self.declared_height,
            )
            previews = tuple(
                ImageRepresentation(
                    url,
                    RepresentationKind.THUMBNAIL,
                    True,
                    int(_name) if _name.isdigit() else {"large": 500, "small": 250}.get(_name, 0),
                    int(_name) if _name.isdigit() else {"large": 500, "small": 250}.get(_name, 0),
                )
                for _name, url in sorted(
                    self.preview_urls.items(),
                    key=lambda item: (
                        int(item[0]) if item[0].isdigit() else {"large": 500, "small": 250}.get(item[0], 0),
                        item[0],
                        item[1],
                    ),
                    reverse=True,
                )
                if url and url != self.original_url
            )
            self.representations = (source, *previews)

    @property
    def stable_id(self) -> str:
        return self.provider_image_id or self.source_sha256 or self.original_url or self.url


def is_valid_manual_cover(cand: CoverCandidate) -> bool:
    """Only decoded, plausible front artwork without identity or quality rejection is selectable."""
    return bool(
        cand.raw_data
        and not cand.error
        and not cand.rejection_reasons
        and not cand.release_conflicts
        and cand.release_tier != ReleaseTier.CONFLICT
        and cand.role_tier >= RoleTier.LIKELY_FRONT
        and cand.album_match_ratio >= 0.7
        and cand.width > 0
        and cand.height > 0
        and cand.visual_quality_score > 0
    )


class CoverFetcher:
    def __init__(
        self,
        artist: str,
        album: str,
        folder: Path | None = None,
        mb_rg_id: str = "",
        mb_rel_id: str = "",
        discogs_id: str = "",
        discogs_token: str = "",
        discogs_json_str: str = "",
        cache_dir: Path | None = None,
        asin: str = "",
        fanart_api_key: str = "",
        cover_mode: str = "auto",
        verbose: bool | int = False,
        manual_all: bool = False,
        exhaustive_categories: bool = False,
        no_local_covers: bool = False,
        write_preview_files: bool = True,
        target_barcode: str = "",
        target_catalog: str = "",
        target_country: str = "",
        target_media: str = "",
    ) -> None:
        self.artist = artist.strip()
        self.album = album.strip()
        self.clean_album = clean_album_title(self.album)
        self.folder = folder.resolve() if folder else None
        self.mb_rg_id = mb_rg_id.strip()
        self.mb_rel_id = mb_rel_id.strip()
        self.discogs_id = discogs_id.strip()
        self.discogs_token = discogs_token.strip() or os.environ.get("DISCOGS_TOKEN", "").strip()
        self.discogs_json_str = discogs_json_str
        self.asin = asin.strip() or os.environ.get("AMAZON_ASIN", "").strip()
        self.fanart_api_key = fanart_api_key.strip() or os.environ.get("FANART_API_KEY", "").strip()
        self.cover_mode = (cover_mode or os.environ.get("COVER_MODE", "auto")).strip().lower()
        self.manual_all = manual_all
        self.exhaustive_categories = exhaustive_categories
        self.no_local_covers = no_local_covers
        self.write_preview_files = write_preview_files
        self.target_barcode = re.sub(r"\D", "", target_barcode)
        self.target_catalog = norm_str(target_catalog)
        self.target_country = target_country.strip().casefold()
        self.target_media = target_media.strip().casefold()

        v_env = os.environ.get("AUDIOPHILE_VERBOSE", "0")
        try:
            v_env_int = int(v_env)
        except ValueError:
            v_env_int = 1 if v_env in ("true", "yes", "1") else 0
        if isinstance(verbose, bool):
            self.verbosity = 1 if verbose else v_env_int
        elif isinstance(verbose, int):
            self.verbosity = max(verbose, v_env_int)
        else:
            self.verbosity = v_env_int
        self.verbose = self.verbosity > 0

        if cache_dir:
            self.cache_dir = cache_dir
        else:
            base_cache = Path(os.environ.get("SCRIPT_DIR", str(Path(__file__).parent))) / ".api_cache" / "covers"
            self.cache_dir = base_cache
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_ttl_seconds = float(os.environ.get("COVER_CACHE_TTL_SECONDS", str(7 * 24 * 3600)))

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self._thread_local = threading.local()
        self.last_fetch_errors: dict[str, str] = {}
        self._analysis_memory_cache: dict[str, dict[str, Any]] = {}
        self.performance: dict[str, int] = {
            "http_requests": 0,
            "http_bytes": 0,
            "url_cache_hits": 0,
            "image_decodes": 0,
            "analysis_cache_hits": 0,
        }
        self._performance_lock = threading.Lock()

    def _count(self, key: str, amount: int = 1) -> None:
        with self._performance_lock:
            self.performance[key] += amount

    def _get_cache_path(self, url: str) -> Path:
        h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        return self.cache_dir / f"{h}.cache"

    def fetch_url(self, url: str, headers: dict[str, str] | None = None) -> bytes | None:
        cache_file = self._get_cache_path(url)
        if (
            cache_file.is_file()
            and cache_file.stat().st_size > 0
            and time.time() - cache_file.stat().st_mtime <= self.cache_ttl_seconds
        ):
            try:
                cached = cache_file.read_bytes()
                if len(cached) <= MAX_IMAGE_BYTES and (_looks_like_json(cached) or _image_header_is_supported(cached)):
                    self._count("url_cache_hits")
                    self.last_fetch_errors.pop(url, None)
                    return cached
            except OSError:
                pass

        hdrs = {"User-Agent": USER_AGENT}
        if headers:
            hdrs.update(headers)

        resp: requests.Response | None = None
        for attempt in range(1, 4):
            resp = None
            payload: bytes | None = None
            try:
                session = self.session
                if threading.current_thread() is not threading.main_thread():
                    thread_session: requests.Session | None = getattr(self._thread_local, "session", None)
                    if thread_session is None:
                        thread_session = requests.Session()
                        thread_session.headers.update({"User-Agent": USER_AGENT})
                        self._thread_local.session = thread_session
                    session = thread_session
                self._count("http_requests")
                resp = session.get(url, headers=hdrs, timeout=TIMEOUT, stream=True)
                if resp.status_code == 200:
                    if hasattr(resp, "iter_content"):
                        chunks: list[bytes] = []
                        received = 0
                        for chunk in resp.iter_content(chunk_size=128 * 1024):
                            received += len(chunk)
                            if received > MAX_IMAGE_BYTES:
                                self.last_fetch_errors[url] = "payload_too_large"
                                return None
                            chunks.append(chunk)
                        payload = b"".join(chunks)
                    else:
                        payload = resp.content
                    if not payload:
                        self.last_fetch_errors[url] = "empty_response"
                        return None
                    if len(payload) > MAX_IMAGE_BYTES:
                        self.last_fetch_errors[url] = "payload_too_large"
                        return None
                    self._count("http_bytes", len(payload))
                    content_type = str(resp.headers.get("Content-Type", "")).casefold()
                    if "json" in content_type and not _looks_like_json(payload):
                        self.last_fetch_errors[url] = "invalid_json_payload"
                        return None
                    if content_type.startswith("image/") and not _image_header_is_supported(payload):
                        self.last_fetch_errors[url] = "invalid_image_payload"
                        return None
                    fd, tmp_name = tempfile.mkstemp(prefix=f".{cache_file.name}.", suffix=".tmp", dir=self.cache_dir)
                    os.close(fd)
                    tmp_path = Path(tmp_name)
                    try:
                        tmp_path.write_bytes(payload)
                        os.replace(tmp_path, cache_file)
                    finally:
                        if tmp_path.exists():
                            tmp_path.unlink()
                    self.last_fetch_errors.pop(url, None)
                    return payload
                if resp.status_code in {429, 500, 502, 503, 504}:
                    self.last_fetch_errors[url] = (
                        "rate_limited" if resp.status_code == 429 else f"temporary_http_{resp.status_code}"
                    )
                    if attempt == 3:
                        return None
                    delay = retry_delay(resp, attempt)
                    # Retry-After is an instruction not to request early. A
                    # provider's long cooldown must not freeze album work;
                    # the next invocation can try after that time instead.
                    if delay > 2.0:
                        return None
                    time.sleep(delay)
                    continue
                self.last_fetch_errors[url] = "not_found" if resp.status_code == 404 else f"http_{resp.status_code}"
                return None
            except requests.Timeout:
                self.last_fetch_errors[url] = "timeout"
                if attempt < 3:
                    time.sleep(retry_delay(None, attempt))
                    continue
                return None
            except requests.RequestException:
                self.last_fetch_errors[url] = "network_error"
                if attempt < 3:
                    time.sleep(retry_delay(None, attempt))
                    continue
                return None
            except OSError:
                # A cache failure must not discard a valid provider response.
                return payload if resp is not None and resp.status_code == 200 else None
            finally:
                if resp is not None and hasattr(resp, "close"):
                    resp.close()
        return None

    def query_itunes(self) -> list[CoverCandidate]:
        candidates: list[CoverCandidate] = []
        queries = [
            f"{self.artist} {self.album}",
            f"{self.artist} {self.clean_album}",
            f"{self.clean_album}",
        ]
        seen_artworks: set[str] = set()

        for q in queries:
            url = f"https://itunes.apple.com/search?term={urllib.parse.quote(q)}&entity=album&limit=25"
            raw = self.fetch_url(url)
            if not raw:
                continue
            try:
                data = json.loads(raw.decode("utf-8", errors="replace"))
            except (ValueError, UnicodeDecodeError):
                continue

            for item in data.get("results", []):
                raw_art = item.get("artworkUrl100", "")
                if not raw_art:
                    continue

                col_name = str(item.get("collectionName", "")).strip()
                art_name = str(item.get("artistName", "")).strip()

                if self.artist and not artist_matches(self.artist, art_name):
                    continue

                high_art = raw_art
                if high_art in seen_artworks:
                    continue
                seen_artworks.add(high_art)

                match_ratio = compute_album_match(self.clean_album, col_name)
                candidates.append(
                    CoverCandidate(
                        provider="Apple Music / iTunes CDN",
                        url=high_art,
                        title=f"{art_name} - {col_name}",
                        album_match_ratio=match_ratio,
                        image_type="front",
                        original_url=high_art,
                        preview_urls={"100": raw_art},
                        representation_kind=RepresentationKind.PREVIEW,
                        generated_representation=True,
                        provider_image_id=str(item.get("collectionId") or ""),
                        provenance_confidence=0.72,
                    )
                )

        return candidates

    def query_deezer(self) -> list[CoverCandidate]:
        candidates: list[CoverCandidate] = []
        q = f"{self.artist} {self.clean_album}"
        url = f"https://api.deezer.com/search/album?q={urllib.parse.quote(q)}&limit=15"
        raw = self.fetch_url(url)
        if not raw:
            return candidates

        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeDecodeError):
            return candidates

        seen: set[str] = set()
        for item in data.get("data", []):
            cover_url = item.get("cover_xl") or item.get("cover_big")
            if not cover_url:
                continue

            art_name = item.get("artist", {}).get("name", "")
            alb_name = item.get("title", "")

            if self.artist and not artist_matches(self.artist, art_name):
                continue

            high_art = str(cover_url)
            if high_art in seen:
                continue
            seen.add(high_art)

            match_ratio = compute_album_match(self.clean_album, alb_name)
            candidates.append(
                CoverCandidate(
                    provider="Deezer CDN",
                    url=high_art,
                    title=f"{art_name} - {alb_name}",
                    album_match_ratio=match_ratio,
                    original_url=high_art,
                    preview_urls={"api": str(cover_url)},
                    representation_kind=RepresentationKind.PREVIEW,
                    generated_representation=True,
                    provider_image_id=str(item.get("id") or ""),
                    provenance_confidence=0.7,
                )
            )
        return candidates

    def query_cover_art_archive(self) -> list[CoverCandidate]:
        candidates: list[CoverCandidate] = []
        caa_targets: list[tuple[str, str]] = []

        if self.mb_rg_id:
            caa_targets.append((f"https://coverartarchive.org/release-group/{self.mb_rg_id}", "Release-Group"))
        elif self.artist and self.clean_album:
            q_mb = f'artist:"{self.artist}" AND releasegroup:"{self.clean_album}"'
            mb_search_url = (
                f"https://musicbrainz.org/ws/2/release-group/?query={urllib.parse.quote(q_mb)}&fmt=json&limit=5"
            )
            raw_mb = self.fetch_url(mb_search_url)
            if raw_mb:
                try:
                    mb_data = json.loads(raw_mb.decode("utf-8", errors="replace"))
                    for rg in mb_data.get("release-groups", []):
                        rg_id = rg.get("id")
                        rg_title = rg.get("title", "")
                        if (
                            rg_id
                            and str(rg.get("primary-type") or "").casefold() == "album"
                            and compute_album_match(self.clean_album, rg_title) >= 0.7
                            and any(
                                artist_matches(
                                    self.artist, str(credit.get("name") or credit.get("artist", {}).get("name") or "")
                                )
                                for credit in rg.get("artist-credit", [])
                                if isinstance(credit, dict)
                            )
                        ):
                            caa_targets.append((f"https://coverartarchive.org/release-group/{rg_id}", "Release-Group"))
                except (ValueError, UnicodeDecodeError):
                    pass

        if self.mb_rel_id:
            caa_targets.append((f"https://coverartarchive.org/release/{self.mb_rel_id}", "Release"))

        for caa_url, kind in caa_targets:
            raw = self.fetch_url(caa_url)
            if not raw:
                continue
            try:
                data = json.loads(raw.decode("utf-8", errors="replace"))
            except (ValueError, UnicodeDecodeError):
                continue

            release_url = str(data.get("release") or "")
            for provider_order, img in enumerate(data.get("images", [])):
                orig_img = str(img.get("image") or "")
                if not orig_img:
                    continue
                orig_img = re.sub(r"^http://", "https://", orig_img)
                types = _normalized_types(img.get("types", []))
                front_flag = bool(img.get("front"))
                back_flag = bool(img.get("back"))
                has_front_role = front_flag or "front" in types
                if not has_front_role:
                    continue
                if any(bad_t in types for bad_t in ("medium", "obi", "matrix/runout", "track")):
                    continue
                comment = str(img.get("comment", "")).strip()
                try:
                    declared_width = max(0, int(img.get("width") or 0))
                    declared_height = max(0, int(img.get("height") or 0))
                except (TypeError, ValueError):
                    declared_width = declared_height = 0
                thumbnails = {
                    str(name): re.sub(r"^http://", "https://", str(value))
                    for name, value in (img.get("thumbnails") or {}).items()
                    if value
                }
                url_release_id = _url_mbid(orig_img)
                response_release_id = _url_mbid(release_url)
                image_release_id = url_release_id or response_release_id
                inconsistent_release = bool(
                    url_release_id and response_release_id and url_release_id != response_release_id
                )
                if kind == "Release" and not image_release_id:
                    image_release_id = self.mb_rel_id
                is_exact_release = bool(
                    kind == "Release"
                    and self.mb_rel_id
                    and not inconsistent_release
                    and image_release_id.casefold() == self.mb_rel_id.casefold()
                )
                role_tier = (
                    RoleTier.EXPLICIT_FRONT
                    if front_flag and not back_flag
                    else RoleTier.LIKELY_FRONT
                    if has_front_role
                    else RoleTier.REJECT
                )
                role_confidence = 0.98 if role_tier == RoleTier.EXPLICIT_FRONT else 0.78 if has_front_role else 0.0
                warnings: list[str] = []
                if "raw/unedited" in types:
                    warnings.append("raw/unedited source requires crop review")
                if any(value in types for value in ("spine", "tray")):
                    warnings.append("compound packaging image")
                if img.get("approved") is False:
                    warnings.append("CAA image is not approved")
                if inconsistent_release:
                    warnings.append("CAA response release conflicts with image URL")

                candidates.append(
                    CoverCandidate(
                        provider=f"Cover Art Archive ({kind})",
                        url=orig_img,
                        title=f"CAA Front ({kind})" + (f": {comment}" if comment else ""),
                        album_match_ratio=1.0,
                        identity_scope=(
                            "release"
                            if is_exact_release
                            else "release-group"
                            if kind == "Release-Group"
                            else "conflict"
                        ),
                        provider_image_id=str(img.get("id") or _url_numeric_image_id(orig_img)),
                        provider_release_id=image_release_id,
                        musicbrainz_release_id=image_release_id if kind == "Release" else "",
                        musicbrainz_release_group_id=(
                            caa_url.rsplit("/", 1)[-1] if kind == "Release-Group" else self.mb_rg_id
                        ),
                        original_url=orig_img,
                        preview_urls=thumbnails,
                        source_verified_original=True,
                        representation_kind=RepresentationKind.ORIGINAL,
                        provider_order=provider_order,
                        provider_metadata={
                            "approved": img.get("approved"),
                            "edit": img.get("edit"),
                            "types": list(img.get("types") or []),
                            "response_release_id": response_release_id,
                            "image_release_id": url_release_id,
                            "declared_mime": str(img.get("mime") or img.get("mime_type") or ""),
                            "declared_format": str(img.get("format") or ""),
                        },
                        approved=img.get("approved") if isinstance(img.get("approved"), bool) else None,
                        comment=comment,
                        roles=types or ("front",),
                        front_flag=front_flag,
                        back_flag=back_flag,
                        role_tier=role_tier,
                        role_confidence=role_confidence,
                        role_evidence=(("CAA front=true",) if front_flag else ("CAA type=Front",)),
                        release_tier=(
                            ReleaseTier.EXACT_RELEASE
                            if is_exact_release
                            else ReleaseTier.RELEASE_GROUP
                            if kind == "Release-Group"
                            else ReleaseTier.CONFLICT
                        ),
                        release_confidence=0.99 if is_exact_release else 0.62 if kind == "Release-Group" else 0.0,
                        release_evidence=(
                            (f"exact MusicBrainz release {image_release_id}",)
                            if is_exact_release
                            else (f"release-group representative release {image_release_id}",)
                            if kind == "Release-Group"
                            else (f"MusicBrainz release conflict {image_release_id}",)
                        ),
                        release_conflicts=(
                            ("CAA response/image release mismatch",)
                            if inconsistent_release
                            else ()
                            if kind != "Release" or is_exact_release
                            else (f"expected {self.mb_rel_id}",)
                        ),
                        provenance_confidence=0.92 if img.get("approved") is not False else 0.55,
                        declared_width=declared_width,
                        declared_height=declared_height,
                        watermark_confidence=1.0 if "watermark" in types else 0.0,
                        warnings=warnings,
                    )
                )

        return candidates

    def query_discogs(self) -> list[CoverCandidate]:
        candidates: list[CoverCandidate] = []
        images: list[dict[str, Any]] = []
        response_release_id = ""
        release_data: dict[str, Any] = {}

        if self.discogs_json_str:
            try:
                dj = json.loads(self.discogs_json_str)
                release_data = dj
                images.extend(dj.get("images", []))
                response_release_id = str(dj.get("id") or "")
                m_val = dj.get("master_id")
                if m_val:
                    str(m_val)
            except ValueError:
                pass

        if not images and self.discogs_id:
            durl = f"https://api.discogs.com/releases/{self.discogs_id}"
            hdrs = {}
            if self.discogs_token:
                hdrs["Authorization"] = f"Discogs token={self.discogs_token}"
            raw = self.fetch_url(durl, headers=hdrs)
            if raw:
                try:
                    dj = json.loads(raw.decode("utf-8", errors="replace"))
                    release_data = dj
                    images.extend(dj.get("images", []))
                    response_release_id = str(dj.get("id") or "")
                    m_val = dj.get("master_id")
                    if m_val:
                        str(m_val)
                except ValueError:
                    pass

        # A Discogs master image represents the release concept and can differ
        # from a specific pressing. It is intentionally not mixed into the
        # exact-release pool.

        seen_uris: set[str] = set()
        release_conflict = bool(response_release_id and self.discogs_id and response_release_id != self.discogs_id)
        exact_release = bool(self.discogs_id and not release_conflict)
        barcode_values = tuple(
            re.sub(r"\D", "", str(item.get("value") or ""))
            for item in release_data.get("identifiers", [])
            if str(item.get("type") or "").casefold() == "barcode"
        )
        catalog_numbers = tuple(
            str(item.get("catno") or "") for item in release_data.get("labels", []) if item.get("catno")
        )
        media_formats = tuple(
            str(item.get("name") or "") for item in release_data.get("formats", []) if item.get("name")
        )
        release_country = str(release_data.get("country") or "")
        edition_conflicts: list[str] = []
        if self.target_barcode and barcode_values and self.target_barcode not in barcode_values:
            edition_conflicts.append("barcode differs from local edition")
        if (
            self.target_catalog
            and catalog_numbers
            and self.target_catalog not in {norm_str(value) for value in catalog_numbers}
        ):
            edition_conflicts.append("catalog number differs from local edition")
        if (
            self.target_media
            and media_formats
            and not any(self.target_media == value.casefold() for value in media_formats)
        ):
            edition_conflicts.append("media format differs from local edition")
        if (
            len(self.target_country) == 2
            and len(release_country) == 2
            and self.target_country != release_country.casefold()
        ):
            edition_conflicts.append("country differs from local edition")
        for provider_order, img in enumerate(images):
            uri = img.get("uri")
            if not uri or uri in seen_uris:
                continue
            seen_uris.add(uri)
            itype = str(img.get("type", "secondary")).casefold()
            try:
                declared_width = max(0, int(img.get("width") or 0))
                declared_height = max(0, int(img.get("height") or 0))
            except (TypeError, ValueError):
                declared_width = declared_height = 0
            is_generated = bool("/rs:" in uri or "/q:" in uri or re.search(r"/(?:w|h):\d+", uri))
            image_id = _discogs_image_id(uri)
            role_tier = RoleTier.LIKELY_FRONT if itype == "primary" else RoleTier.UNKNOWN
            role_confidence = 0.72 if itype == "primary" else 0.25
            candidates.append(
                CoverCandidate(
                    provider=f"Discogs ({itype})",
                    url=uri,
                    title=f"Discogs {itype} image",
                    album_match_ratio=1.0,
                    identity_scope="release",
                    provider_image_id=image_id,
                    provider_release_id=self.discogs_id,
                    catalog_numbers=catalog_numbers,
                    barcode=barcode_values[0] if barcode_values else "",
                    release_country=release_country,
                    release_year=str(release_data.get("year") or ""),
                    media_format=", ".join(media_formats),
                    discogs_release_id=self.discogs_id if exact_release else "",
                    original_url=uri,
                    preview_urls={"150": str(img.get("uri150") or "")},
                    representations=(
                        ImageRepresentation(
                            uri,
                            RepresentationKind.PREVIEW if is_generated else RepresentationKind.UNKNOWN,
                            is_generated,
                            declared_width,
                            declared_height,
                        ),
                        *(
                            (ImageRepresentation(str(img["uri150"]), RepresentationKind.THUMBNAIL, True),)
                            if img.get("uri150")
                            else ()
                        ),
                    ),
                    representation_kind=RepresentationKind.PREVIEW if is_generated else RepresentationKind.UNKNOWN,
                    generated_representation=is_generated,
                    provider_order=provider_order,
                    provider_metadata={
                        "discogs_type": itype,
                        "resource_url": str(img.get("resource_url") or ""),
                        "uri150": str(img.get("uri150") or ""),
                        "response_release_id": response_release_id,
                    },
                    roles=("front-candidate",) if itype == "primary" else ("unknown",),
                    front_flag=None,
                    back_flag=None,
                    role_tier=role_tier,
                    role_confidence=role_confidence,
                    role_evidence=(f"Discogs type={itype}; not a semantic artwork role",),
                    release_tier=(
                        ReleaseTier.CONFLICT
                        if release_conflict or edition_conflicts
                        else ReleaseTier.EXACT_RELEASE
                        if exact_release
                        else ReleaseTier.TITLE_ONLY
                    ),
                    release_confidence=0.0 if release_conflict or edition_conflicts else 0.98 if exact_release else 0.4,
                    release_evidence=(f"Discogs release {self.discogs_id}",),
                    release_conflicts=(
                        (f"Discogs response ID {response_release_id} differs from {self.discogs_id}",)
                        if release_conflict
                        else ()
                    )
                    + tuple(edition_conflicts),
                    declared_width=declared_width,
                    declared_height=declared_height,
                    provenance_confidence=0.78 if itype == "primary" else 0.68,
                )
            )

        return candidates

    def query_local(self) -> list[CoverCandidate]:
        candidates: list[CoverCandidate] = []
        if self.no_local_covers or not self.folder or not self.folder.is_dir():
            return candidates

        for fname in (
            *sorted(
                p.name
                for p in self.folder.glob("cover.original.*")
                if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
            ),
            "cover.jpg",
            "cover.png",
            "folder.jpg",
            "folder.png",
            "front.jpg",
            "front.png",
            "front_cover.jpg",
            "front_cover.png",
        ):
            lp = self.folder / fname
            if lp.is_file() and lp.stat().st_size > 0:
                try:
                    data = lp.read_bytes()
                    cand = CoverCandidate(
                        provider="Local Existing File",
                        url=str(lp),
                        title=f"Local {fname}",
                        album_match_ratio=1.0,
                        raw_data=data,
                        identity_scope="local",
                        representation_kind=RepresentationKind.LOCAL,
                        front_flag=True,
                        role_tier=RoleTier.EXPLICIT_FRONT,
                        role_confidence=0.98,
                        role_evidence=(f"front artwork filename {fname}",),
                        release_evidence=("stored in exact release folder",),
                        provenance_confidence=0.98,
                    )
                    candidates.append(cand)
                except OSError:
                    pass

        for subdir_name in ("Scans", "scans", "Artwork", "artwork", "Covers", "covers", "Scan", "scan"):
            subdir = self.folder / subdir_name
            if subdir.is_dir():
                for scan_file in subdir.glob("*.*"):
                    if scan_file.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") and scan_file.stat().st_size > 0:
                        sname_lower = scan_file.name.lower()
                        excluded_role = next(
                            (
                                role
                                for role in ("back", "disc", "cd", "booklet", "tray", "spine", "obi", "matrix")
                                if role in sname_lower
                            ),
                            "",
                        )
                        if not excluded_role and any(k in sname_lower for k in ("front", "cover", "folder", "00")):
                            try:
                                data = scan_file.read_bytes()
                                cand = CoverCandidate(
                                    provider="Local Optical Scan",
                                    url=str(scan_file),
                                    title=f"Scan {scan_file.name}",
                                    album_match_ratio=1.0,
                                    raw_data=data,
                                    identity_scope="local",
                                    representation_kind=RepresentationKind.LOCAL,
                                    role_tier=RoleTier.LIKELY_FRONT,
                                    role_confidence=0.85 if "front" in sname_lower else 0.68,
                                    role_evidence=(f"local scan filename {scan_file.name}",),
                                    release_evidence=("stored in exact release scan directory",),
                                    provenance_confidence=0.95,
                                )
                                candidates.append(cand)
                            except OSError:
                                pass

        # Existing embedded fronts are preservation material and must compete
        # as exact-release candidates instead of being silently discarded.
        seen_embedded: set[str] = set()
        for first_flac in sorted(self.folder.glob("*.flac")):
            try:
                from mutagen.flac import FLAC

                audio = FLAC(first_flac)
                for front in (picture for picture in audio.pictures if picture.type == 3 and picture.data):
                    digest = hashlib.sha256(front.data).hexdigest()
                    if digest in seen_embedded:
                        continue
                    seen_embedded.add(digest)
                    candidates.append(
                        CoverCandidate(
                            provider="Local Embedded Front",
                            url=f"embedded://{first_flac.name}/front/{digest}",
                            title=f"Embedded front from {first_flac.name}",
                            album_match_ratio=1.0,
                            raw_data=bytes(front.data),
                            identity_scope="local",
                            representation_kind=RepresentationKind.LOCAL,
                            front_flag=True,
                            role_tier=RoleTier.EXPLICIT_FRONT,
                            role_confidence=1.0,
                            role_evidence=("FLAC picture type 3",),
                            release_evidence=("embedded in exact release audio",),
                            declared_width=int(front.width or 0),
                            declared_height=int(front.height or 0),
                            provider_image_id=digest,
                            mime_type=str(front.mime or ""),
                            provenance_confidence=1.0,
                        )
                    )
            except (OSError, ValueError, MutagenError):
                pass
        return candidates

    def query_amazon(self) -> list[CoverCandidate]:
        candidates: list[CoverCandidate] = []
        target_asin = self.asin
        verified_asin = False
        if not target_asin and self.mb_rel_id:
            mb_rel_url = f"https://musicbrainz.org/ws/2/release/{self.mb_rel_id}?inc=url-rels&fmt=json"
            raw_mb = self.fetch_url(mb_rel_url)
            if raw_mb:
                try:
                    rel_data = json.loads(raw_mb.decode("utf-8", errors="replace"))
                    if rel_data.get("asin"):
                        target_asin = str(rel_data["asin"]).strip()
                        verified_asin = True
                    else:
                        for r in rel_data.get("relations", []):
                            u = r.get("url", {}).get("resource", "")
                            m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", u)
                            if m:
                                target_asin = m.group(1)
                                verified_asin = True
                                break
                except (ValueError, TypeError, AttributeError, UnicodeDecodeError) as exc:
                    self.last_fetch_errors[mb_rel_url] = f"parse_error: {exc}"

        if target_asin:
            for suffix in ("_SL2000_.jpg", "_SCRM_.jpg"):
                url = f"https://images-na.ssl-images-amazon.com/images/P/{target_asin}.01.MAIN.{suffix}"
                candidates.append(
                    CoverCandidate(
                        provider="Amazon CDN",
                        url=url,
                        title=f"Amazon ASIN {target_asin}",
                        album_match_ratio=1.0,
                        image_type="front",
                        identity_scope="release",
                        release_tier=ReleaseTier.EXACT_RELEASE if verified_asin else ReleaseTier.TITLE_ONLY,
                        release_confidence=0.94 if verified_asin else 0.4,
                        release_evidence=(f"MusicBrainz release {self.mb_rel_id} ASIN",) if verified_asin else (),
                        provider_release_id=target_asin,
                        musicbrainz_release_id=self.mb_rel_id if verified_asin else "",
                        representation_kind=RepresentationKind.UNKNOWN,
                        generated_representation=True,
                    )
                )
        return candidates

    def query_fanart_tv(self) -> list[CoverCandidate]:
        candidates: list[CoverCandidate] = []
        if not self.mb_rg_id:
            return candidates

        api_key = self.fanart_api_key or os.environ.get("FANART_API_KEY", "").strip()
        if not api_key:
            return candidates

        url = f"https://webservice.fanart.tv/v3/music/albums/{self.mb_rg_id}?api_key={api_key}"
        raw = self.fetch_url(url)
        if not raw:
            return candidates

        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
            alb_data = data.get("albums", {}).get(self.mb_rg_id, {})
            for item in alb_data.get("albumcover", []):
                u = item.get("url")
                if u:
                    candidates.append(
                        CoverCandidate(
                            provider="Fanart.tv (Front)",
                            url=u,
                            title="Fanart.tv Curated Cover",
                            album_match_ratio=1.0,
                            image_type="front",
                            musicbrainz_release_group_id=self.mb_rg_id,
                            release_tier=ReleaseTier.RELEASE_GROUP,
                            release_confidence=0.62,
                        )
                    )
            for item in alb_data.get("cdart", []):
                u = item.get("url")
                if u:
                    candidates.append(
                        CoverCandidate(
                            provider="Fanart.tv (CD Art)",
                            url=u,
                            title="Fanart.tv CD Disc Art",
                            album_match_ratio=1.0,
                            image_type="medium",
                        )
                    )
            for item in alb_data.get("albumback", []):
                u = item.get("url")
                if u:
                    candidates.append(
                        CoverCandidate(
                            provider="Fanart.tv (Back)",
                            url=u,
                            title="Fanart.tv Back Cover",
                            album_match_ratio=1.0,
                            image_type="back",
                        )
                    )
        except (ValueError, TypeError, AttributeError, UnicodeDecodeError) as exc:
            self.last_fetch_errors[url] = f"parse_error: {exc}"
        return candidates

    def query_bandcamp(self) -> list[CoverCandidate]:
        candidates: list[CoverCandidate] = []
        q = f"{self.artist} {self.clean_album}"
        url = f"https://bandcamp.com/api/fuzzysearch/1/app_autocomplete?q={urllib.parse.quote(q)}"
        raw = self.fetch_url(url)
        if not raw:
            return candidates

        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
            results = data.get("results") or data.get("auto", {}).get("results", [])
            for item in results:
                raw_img = item.get("img") or ""
                item_name = item.get("name") or ""
                item_band = item.get("band_name") or ""
                if self.artist and not artist_matches(self.artist, item_band):
                    continue
                match_ratio = compute_album_match(self.clean_album, item_name)
                if match_ratio < 0.7:
                    continue

                if not raw_img:
                    continue
                master_url = str(raw_img)

                candidates.append(
                    CoverCandidate(
                        provider="Bandcamp Artwork",
                        url=master_url,
                        title=f"Bandcamp {item_band} - {item_name}",
                        album_match_ratio=match_ratio,
                        image_type="front",
                        original_url=master_url,
                        preview_urls={"api": str(raw_img)} if raw_img else {},
                        representation_kind=RepresentationKind.UNKNOWN,
                        generated_representation=True,
                    )
                )
        except (ValueError, TypeError, AttributeError, UnicodeDecodeError) as exc:
            self.last_fetch_errors[url] = f"parse_error: {exc}"
        return candidates

    def query_tidal(self) -> list[CoverCandidate]:
        candidates: list[CoverCandidate] = []
        q = f"{self.artist} {self.clean_album}"
        url = f"https://api.tidal.com/v1/search/albums?query={urllib.parse.quote(q)}&limit=5&countryCode=US"
        hdrs = {"x-tidal-token": "CzET4vdadNUFQ5JU"}
        raw = self.fetch_url(url, headers=hdrs)
        if not raw:
            return candidates

        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
            for alb in data.get("items", []):
                art_name = alb.get("artist", {}).get("name", "")
                title = alb.get("title", "")
                if self.artist and not artist_matches(self.artist, art_name):
                    continue
                match_ratio = compute_album_match(self.clean_album, title)
                if match_ratio < 0.7:
                    continue
                cover_uuid = alb.get("cover")
                if cover_uuid:
                    path_part = cover_uuid.replace("-", "/")
                    cand_url = f"https://resources.tidal.com/images/{path_part}/1280x1280.jpg"
                    candidates.append(
                        CoverCandidate(
                            provider="Tidal High-Res CDN",
                            url=cand_url,
                            title=f"{art_name} - {title}",
                            album_match_ratio=match_ratio,
                            image_type="front",
                            original_url=cand_url,
                            representation_kind=RepresentationKind.PREVIEW,
                            generated_representation=True,
                            provider_image_id=str(alb.get("id") or cover_uuid),
                            provenance_confidence=0.72,
                        )
                    )
        except (ValueError, TypeError, AttributeError, UnicodeDecodeError) as exc:
            self.last_fetch_errors[url] = f"parse_error: {exc}"
        return candidates

    def query_qobuz(self) -> list[CoverCandidate]:
        candidates: list[CoverCandidate] = []
        q = f"{self.artist} {self.clean_album}"
        url = f"https://www.qobuz.com/us-en/search?q={urllib.parse.quote(q)}"
        hdrs = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
        raw = self.fetch_url(url, headers=hdrs)
        if not raw:
            return candidates
        try:
            from bs4 import BeautifulSoup

            html = raw.decode("utf-8", errors="replace")
            soup = BeautifulSoup(html, "html.parser")
            seen: set[str] = set()

            for a in soup.find_all("a"):
                title_attr = str(a.get("title") or a.get("aria-label") or "")
                m = re.match(r"More details on (.*?) by (.*?)\.?$", title_attr)
                if not m:
                    continue
                alb_name = m.group(1).strip()
                art_name = m.group(2).strip()

                if self.artist and not artist_matches(self.artist, art_name):
                    continue

                match_ratio = compute_album_match(self.clean_album, alb_name)
                if match_ratio < 0.7:
                    continue

                img = a.find("img")
                if not img or not img.get("src"):
                    continue
                src = str(img["src"])
                if "static.qobuz.com/images/covers/" not in src:
                    continue

                org_u = src
                if org_u in seen:
                    continue
                seen.add(org_u)

                candidates.append(
                    CoverCandidate(
                        provider="Qobuz CDN",
                        url=org_u,
                        title=f"{art_name} - {alb_name}",
                        album_match_ratio=match_ratio,
                        image_type="front",
                        original_url=org_u,
                        preview_urls={"api": src},
                        representation_kind=RepresentationKind.UNKNOWN,
                        generated_representation=True,
                    )
                )
        except (ImportError, ValueError, TypeError, AttributeError, UnicodeDecodeError) as exc:
            self.last_fetch_errors[url] = f"parse_error: {exc}"
        return candidates

    def query_theaudiodb(self) -> list[CoverCandidate]:
        candidates: list[CoverCandidate] = []
        if not self.artist or not self.clean_album:
            return candidates
        url = f"https://www.theaudiodb.com/api/v1/json/2/searchalbum.php?s={urllib.parse.quote(self.artist)}&a={urllib.parse.quote(self.clean_album)}"
        raw = self.fetch_url(url)
        if not raw:
            return candidates
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeDecodeError):
            return candidates

        for alb in data.get("album") or []:
            art = alb.get("strArtist", "")
            if self.artist and not artist_matches(self.artist, art):
                continue
            alb_name = alb.get("strAlbum", "")
            match_ratio = compute_album_match(self.clean_album, alb_name)
            if match_ratio < 0.75:
                continue

            for key in ("strAlbumThumbHQ", "strAlbumThumb"):
                img_url = alb.get(key)
                if img_url and img_url.startswith("http"):
                    candidates.append(
                        CoverCandidate(
                            provider="TheAudioDB",
                            url=img_url,
                            title=f"TheAudioDB: {art} - {alb_name}",
                            album_match_ratio=match_ratio,
                            image_type="front",
                            original_url=str(img_url),
                            preview_urls={
                                "standard": str(alb.get("strAlbumThumb") or ""),
                            },
                            representation_kind=RepresentationKind.UNKNOWN,
                            provider_image_id=str(alb.get("idAlbum") or ""),
                            provenance_confidence=0.68,
                        )
                    )
                    break
        return candidates

    @staticmethod
    def _front_crop_box(gray: Image.Image) -> tuple[tuple[int, int, int, int] | None, float]:
        """Locate a front insert only when package seams or a case notch support it."""
        w, h = gray.size
        if min(w, h) < 500 or max(w, h) / min(w, h) > 1.4:
            return None, 0.0
        scale = min(1.0, 650 / max(w, h))
        sample = gray.resize((round(w * scale), round(h * scale)), Image.Resampling.LANCZOS)
        arr = np.asarray(sample, dtype=np.float32)
        sh, sw = arr.shape
        columns = np.mean(np.abs(np.diff(arr, axis=1)), axis=0)
        rows = np.mean(np.abs(np.diff(arr, axis=0)), axis=1)

        def seam(values: np.ndarray, start: float, stop: float) -> tuple[int, float]:
            lo, hi = max(1, round(len(values) * start)), min(len(values) - 1, round(len(values) * stop))
            if lo >= hi:
                return 0, 0.0
            index = lo + int(np.argmax(values[lo:hi]))
            return index + 1, float(values[index])

        left, left_strength = seam(columns, 0.025, 0.22)
        right, right_strength = seam(columns, 0.78, 0.985)
        top, top_strength = seam(rows, 0.012, 0.14)
        bottom, bottom_strength = seam(rows, 0.86, 0.992)
        core_width, core_height = right - left, bottom - top
        if (
            min(left_strength, right_strength, top_strength, bottom_strength) >= 12
            and abs(core_width - core_height) <= 0.045 * max(core_width, core_height)
            and min(left, sw - right, top, sh - bottom) >= 0
            and (left / sw > 0.035 or (sw - right) / sw > 0.035)
            and max(
                float(np.mean(np.abs(np.diff(arr[:, : max(2, left - 2)], axis=1)))) if left > 3 else 0.0,
                float(np.mean(np.abs(np.diff(arr[:, min(sw - 2, right + 2) :], axis=1)))) if sw - right > 3 else 0.0,
            )
            >= 4.0
        ):
            side = min(core_width, core_height)
            x0 = left + (core_width - side) // 2
            y0 = top + (core_height - side) // 2
            return (
                round(x0 / scale),
                round(y0 / scale),
                round((x0 + side) / scale),
                round((y0 + side) / scale),
            ), 0.91

        if 1.05 <= w / h <= 1.35:
            extra = sw - sh
            if (
                abs(left - extra) <= 0.035 * sw
                and left_strength >= 16
                and left > 5
                and float(np.mean(np.abs(np.diff(arr[:, : left - 2], axis=1)))) >= 4
            ):
                return (round(left / scale), 0, round(left / scale) + h, h), 0.84
            if (
                abs(right - sh) <= 0.035 * sw
                and right_strength >= 16
                and sw - right > 5
                and float(np.mean(np.abs(np.diff(arr[:, right + 2 :], axis=1)))) >= 4
            ):
                return (0, 0, h, h), 0.84

        # A CD case finger notch produces a localized burst of edges in the
        # otherwise quiet outer strip. A circle inside the cover is insufficient.
        if 1.05 <= w / h <= 1.3 and sw - sh >= round(0.05 * sw):
            band_width = max(5, round(sw * 0.025))
            side_energy = np.mean(np.abs(np.diff(arr[:, -band_width:], axis=1)), axis=1)
            middle = side_energy[round(sh * 0.38) : round(sh * 0.62)]
            outer = np.concatenate((side_energy[: round(sh * 0.3)], side_energy[round(sh * 0.7) :]))
            terminal, terminal_strength = seam(columns, 0.975, 0.999)
            if (
                terminal >= round(sw * 0.98)
                and terminal_strength >= 18
                and float(np.max(middle)) >= 2.4 * max(float(np.median(outer)), 1.0)
                and float(np.max(middle)) >= 10
            ):
                return (0, 0, h, h), 0.82
        return None, 0.0

    @staticmethod
    def _perceptual_hash(gray: Image.Image) -> str:
        arr = np.asarray(gray.resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float64)
        n = 32
        positions = np.arange(n, dtype=np.float64)
        transform = np.cos((2 * positions[:, None] + 1) * positions[None, :] * math.pi / (2 * n))
        coeff = transform.T @ arr @ transform
        low = coeff[:8, :8].copy()
        threshold = float(np.median(low[1:, :]))
        bits = low.flatten() > threshold
        return f"{int(''.join('1' if bit else '0' for bit in bits), 2):016x}"

    @staticmethod
    def _difference_hash(gray: Image.Image) -> str:
        arr = np.asarray(gray.resize((9, 8), Image.Resampling.LANCZOS), dtype=np.int16)
        bits = (arr[:, 1:] > arr[:, :-1]).flatten()
        return f"{int(''.join('1' if bit else '0' for bit in bits), 2):016x}"

    @staticmethod
    def _hamming_hash(left: str, right: str) -> int:
        if not left or not right or len(left) != len(right):
            return 256
        return (int(left, 16) ^ int(right, 16)).bit_count()

    def _near_same_scan(self, left: CoverCandidate, right: CoverCandidate) -> bool:
        left_pairs = left.alternate_hashes or [(left.perceptual_hash, left.difference_hash)]
        right_pairs = right.alternate_hashes or [(right.perceptual_hash, right.difference_hash)]
        return any(
            self._hamming_hash(lp, rp) <= 8 and self._hamming_hash(ld, rd) <= 10
            for lp, ld in left_pairs
            for rp, rd in right_pairs
        )

    @staticmethod
    def _profile_aware_rgb(im: Image.Image) -> Image.Image:
        converted, _profile = _compatibility_rgb(im)
        return converted

    @staticmethod
    def _blockiness(arr: np.ndarray) -> tuple[float, float]:
        height, width = arr.shape
        if height < 64 or width < 64:
            return 1.0, 0.0
        boundary_cols = np.arange(7, width - 8, 8)
        inner_cols = np.arange(8, width - 8, 8)
        boundary_rows = np.arange(7, height - 8, 8)
        inner_rows = np.arange(8, height - 8, 8)
        boundary_h = float(np.mean(np.abs(arr[:, boundary_cols] - arr[:, boundary_cols + 1])))
        inner_h = float(np.mean(np.abs(arr[:, inner_cols] - arr[:, inner_cols + 1])))
        boundary_v = float(np.mean(np.abs(arr[boundary_rows, :] - arr[boundary_rows + 1, :])))
        inner_v = float(np.mean(np.abs(arr[inner_rows, :] - arr[inner_rows + 1, :])))
        ratio = (boundary_h / (inner_h + 1e-4) + boundary_v / (inner_v + 1e-4)) / 2.0
        step = (max(0.0, boundary_h - inner_h) + max(0.0, boundary_v - inner_v)) / 2.0
        severity = max(0.0, step - 0.45) * 8.0 + max(0.0, ratio - 1.15) * 18.0
        return float(ratio), min(100.0, severity)

    @staticmethod
    def _has_uniform_border(arr: np.ndarray) -> bool:
        size = arr.shape[0]
        margin = max(3, int(size * 0.015))
        edge_pairs = (
            (arr[:margin, :], arr[margin : 2 * margin, :]),
            (arr[-margin:, :], arr[-2 * margin : -margin, :]),
            (arr[:, :margin], arr[:, margin : 2 * margin]),
            (arr[:, -margin:], arr[:, -2 * margin : -margin]),
        )
        for edge, adjacent in edge_pairs:
            extreme = max(float(np.mean(edge >= 248)), float(np.mean(edge <= 6)))
            if extreme > 0.85 and float(np.std(edge)) < 8.0 and abs(float(np.mean(edge) - np.mean(adjacent))) > 18.0:
                return True
        return False

    @staticmethod
    def _scan_defect_signals(gray: Image.Image) -> tuple[float, float, float, float]:
        sample = gray.resize((256, 256), Image.Resampling.LANCZOS)
        small_arr = np.asarray(sample, dtype=np.float32)
        edges = sample.filter(ImageFilter.FIND_EDGES)

        def alignment_score(image: Image.Image) -> float:
            values = np.asarray(image, dtype=np.float32)[12:-12, 12:-12]
            return float(np.var(np.mean(values, axis=0)) + np.var(np.mean(values, axis=1)))

        base = alignment_score(edges) + 1e-6
        best_angle = 0.0
        best_score = base
        fill = int(np.median(np.asarray(sample)))
        for angle in (-3.0, -2.0, -1.0, 1.0, 2.0, 3.0):
            rotated = edges.rotate(angle, resample=Image.Resampling.BILINEAR, expand=False, fillcolor=fill)
            score = alignment_score(rotated)
            if score > best_score:
                best_score = score
                best_angle = -angle
        improvement = best_score / base - 1.0
        skew_confidence = min(0.7, max(0.0, (improvement - 0.10) * 1.6)) if best_angle else 0.0

        centered = small_arr - float(np.mean(small_arr))
        spectrum = np.abs(np.fft.rfft2(centered))
        spectrum[:8, :8] = 0
        spectral_median = float(np.median(spectrum)) + 1e-6
        spectral_peak = float(np.percentile(spectrum, 99.95)) / spectral_median
        moire_confidence = min(0.45, max(0.0, (spectral_peak - 120.0) / 500.0))

        column_means = np.mean(small_arr, axis=0)
        row_means = np.mean(small_arr, axis=1)
        column_jump = float(np.max(np.abs(np.diff(column_means)))) if len(column_means) > 1 else 0.0
        row_jump = float(np.max(np.abs(np.diff(row_means)))) if len(row_means) > 1 else 0.0
        streak_confidence = min(0.45, max(0.0, (max(column_jump, row_jump) - 18.0) / 80.0))
        return best_angle, skew_confidence, moire_confidence, streak_confidence

    @staticmethod
    def _analysis_fields() -> tuple[str, ...]:
        return (
            "width",
            "height",
            "aspect_ratio",
            "file_size",
            "format",
            "mime_type",
            "oriented_width",
            "oriented_height",
            "effective_width",
            "effective_height",
            "effective_pixels",
            "has_alpha",
            "has_icc_profile",
            "icc_profile_hash",
            "source_sha256",
            "perceptual_hash",
            "difference_hash",
            "alternate_hashes",
            "crop_box",
            "crop_confidence",
            "source_form",
            "crop_penalty",
            "sharpness",
            "contrast",
            "entropy",
            "colorfulness",
            "dynamic_range",
            "blockiness",
            "is_fake_upscale",
            "has_scanner_borders",
            "detail_score",
            "blur_score",
            "compression_score",
            "tonal_integrity_score",
            "color_integrity_score",
            "cleanliness_score",
            "upscale_confidence",
            "skew_degrees",
            "skew_confidence",
            "moire_confidence",
            "streak_confidence",
            "metric_confidence",
        )

    def _apply_analysis_cache(self, cand: CoverCandidate) -> bool:
        cached = self._analysis_memory_cache.get(cand.source_sha256)
        cache_path = self.cache_dir / f"quality-v8-{cand.source_sha256}.json"
        if cached is None and cache_path.is_file():
            try:
                parsed = json.loads(cache_path.read_text(encoding="utf-8"))
                if parsed.get("schema") == 6 and parsed.get("source_sha256") == cand.source_sha256:
                    cached = parsed
            except (OSError, ValueError):
                cached = None
        if cached is None:
            return False
        for name in self._analysis_fields():
            if name not in cached:
                return False
            value = cached[name]
            if name == "crop_box" and isinstance(value, list):
                value = tuple(int(item) for item in value)
            setattr(cand, name, value)
        if (
            cand.declared_width
            and cand.declared_height
            and (cand.declared_width != cand.width or cand.declared_height != cand.height)
        ):
            cand.warnings.append(
                "provider dimensions differ from downloaded representation: "
                f"declared {cand.declared_width}x{cand.declared_height}, measured {cand.width}x{cand.height}"
            )
        self._finalize_visual_score(cand)
        return True

    def _store_analysis_cache(self, cand: CoverCandidate) -> None:
        payload = {name: getattr(cand, name) for name in self._analysis_fields()}
        payload["schema"] = 6
        self._analysis_memory_cache[cand.source_sha256] = payload
        cache_path = self.cache_dir / f"quality-v8-{cand.source_sha256}.json"
        fd, temporary_name = tempfile.mkstemp(prefix=".quality-", suffix=".tmp", dir=self.cache_dir)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, cache_path)
        except OSError:
            pass
        finally:
            if temporary.exists():
                temporary.unlink()

    def _finalize_visual_score(self, cand: CoverCandidate) -> None:
        oversharpen_penalty = min(25.0, max(0.0, cand.sharpness - 1700.0) / 100.0)
        geometry_score = 96.0 - cand.crop_penalty - (14.0 if cand.has_scanner_borders else 0.0)
        if cand.crop_box is None and not (0.97 <= cand.aspect_ratio <= 1.03):
            geometry_score -= min(35.0, abs(math.log(cand.aspect_ratio)) * 105.0)
        actionable_upscale = _actionable_upscale_confidence(cand.upscale_confidence)
        authenticity_score = 100.0 - 72.0 * actionable_upscale
        metadata_text = " ".join((cand.title, *cand.roles, *cand.role_evidence)).casefold()
        if any(word in metadata_text for word in ("watermark", "site logo", "digital overlay")):
            cand.watermark_confidence = max(cand.watermark_confidence, 0.95)
        cand.metric_confidence["watermark"] = 0.9 if cand.watermark_confidence else 0.0
        watermark_penalty = 42.0 * cand.watermark_confidence
        useful_side = min(cand.effective_width, cand.effective_height)
        resolution_penalty = 30.0 * max(0.0, 1.0 - useful_side / MIN_USABLE_COVER_SIDE)
        removed_fraction = (
            1.0 - (cand.effective_width * cand.effective_height) / max(1, cand.oriented_width * cand.oriented_height)
            if cand.crop_box
            else 0.0
        )
        # A recovered insert remains a scan of a compound package. Even an
        # accurate boundary cannot prove that plastic reflections are absent.
        packaging_penalty = min(16.0, 4.0 + 48.0 * removed_fraction) if cand.crop_box else 0.0
        cand.artifact_score = max(
            0.0,
            0.55 * cand.compression_score + 0.45 * authenticity_score - oversharpen_penalty,
        )
        cand.visual_quality_score = max(
            0.0,
            min(
                100.0,
                0.32 * cand.detail_score
                + 0.20 * cand.compression_score
                + 0.14 * geometry_score
                + 0.10 * cand.tonal_integrity_score
                + 0.05 * cand.color_integrity_score
                + 0.09 * cand.cleanliness_score
                + 0.10 * authenticity_score
                - watermark_penalty
                - resolution_penalty
                - packaging_penalty,
            ),
        )
        cand.score_breakdown = {
            "detail": cand.detail_score,
            "compression": cand.compression_score,
            "geometry": geometry_score,
            "tone": cand.tonal_integrity_score,
            "color_integrity": cand.color_integrity_score,
            "cleanliness": cand.cleanliness_score,
            "authenticity": authenticity_score,
            "upscale_evidence": cand.upscale_confidence,
            "upscale_penalty_confidence": actionable_upscale,
            "watermark_penalty": watermark_penalty,
            "resolution_penalty": resolution_penalty,
            "packaging_penalty": packaging_penalty,
            "skew_penalty": abs(cand.skew_degrees) * cand.skew_confidence,
            "moire_penalty": 4.0 * cand.moire_confidence,
            "streak_penalty": 4.0 * cand.streak_confidence,
            "visual_quality": cand.visual_quality_score,
        }
        cand.badge_penalty = watermark_penalty
        cand.total_score = cand.visual_quality_score * 100.0
        cand.selection_explanation = [
            f"release={cand.release_tier.name.lower()} ({cand.release_confidence:.2f})",
            f"role={cand.role_tier.name.lower()} ({cand.role_confidence:.2f})",
            f"original={cand.width}x{cand.height} {cand.format}",
            f"effective={cand.effective_width}x{cand.effective_height}",
            f"detail={cand.detail_score:.1f}",
            f"compression={cand.compression_score:.1f}",
            f"upscale={cand.upscale_confidence:.2f}",
            f"skew={cand.skew_degrees:.1f}deg ({cand.skew_confidence:.2f})",
            f"visual={cand.visual_quality_score:.1f}",
        ]

    def evaluate_candidate(self, cand: CoverCandidate) -> CoverCandidate:
        """Probe the original representation and calculate content-based quality evidence."""
        source_url = cand.original_url or cand.url
        if cand.raw_data is None:
            failures: list[str] = []
            seen_urls: set[str] = set()
            for representation in cand.representations:
                if not representation.url or representation.url in seen_urls:
                    continue
                seen_urls.add(representation.url)
                headers = {}
                if "discogs.com" in representation.url and self.discogs_token:
                    headers["Authorization"] = f"Discogs token={self.discogs_token}"
                data = self.fetch_url(representation.url, headers=headers)
                if not data:
                    failures.append(self.last_fetch_errors.get(representation.url, "empty_response"))
                    continue
                probe = replace(
                    cand,
                    raw_data=data,
                    evaluated_url=representation.url,
                    representation_kind=representation.kind,
                    generated_representation=representation.generated,
                    source_verified_original=(
                        representation.kind in {RepresentationKind.ORIGINAL, RepresentationKind.LOCAL}
                        and not representation.generated
                    ),
                    warnings=list(cand.warnings),
                    rejection_reasons=list(cand.rejection_reasons),
                )
                self.evaluate_candidate(probe)
                if probe.error:
                    failures.append(probe.error)
                    # Header-valid but corrupt cached images must not poison
                    # subsequent retrievals of the preferred representation.
                    try:
                        self._get_cache_path(representation.url).unlink(missing_ok=True)
                    except OSError as exc:
                        failures.append(f"corrupt cache removal failed: {type(exc).__name__}")
                    continue
                if failures or representation.url != source_url:
                    probe.warnings.append("preferred representation unavailable; evaluated fallback")
                cand.__dict__.update(probe.__dict__)
                return cand
            cand.error = failures[-1] if failures else "empty_or_too_small_payload"
            cand.rejection_reasons.append(cand.error)
            cand.total_score = -9999.0
            return cand
        elif not cand.evaluated_url:
            cand.evaluated_url = source_url

        if not cand.raw_data:
            cand.error = self.last_fetch_errors.get(source_url, "empty_or_too_small_payload")
            cand.rejection_reasons.append(cand.error)
            cand.total_score = -9999.0
            return cand
        if len(cand.raw_data) > MAX_IMAGE_BYTES:
            cand.error = "image_exceeds_byte_limit"
            cand.rejection_reasons.append(cand.error)
            cand.total_score = -9999.0
            return cand

        cand.file_size = len(cand.raw_data)
        cand.source_sha256 = hashlib.sha256(cand.raw_data).hexdigest()
        if self._apply_analysis_cache(cand):
            self._count("analysis_cache_hits")
            _apply_visual_provenance(cand)
            return cand

        try:
            with Image.open(io.BytesIO(cand.raw_data)) as source:
                self._count("image_decodes")
                source_width, source_height = source.size
                if source_width <= 0 or source_height <= 0:
                    raise ValueError("zero dimensions")
                if source_width * source_height > MAX_IMAGE_PIXELS:
                    raise ValueError(f"image exceeds {MAX_IMAGE_PIXELS} pixel safety limit")
                source.load()
                cand.format = (source.format or "UNKNOWN").upper()
                cand.mime_type = Image.MIME.get(cand.format, "application/octet-stream")
                cand.has_alpha = "A" in source.getbands() or "transparency" in source.info
                profile = source.info.get("icc_profile")
                cand.has_icc_profile = bool(profile)
                if isinstance(profile, bytes):
                    cand.icc_profile_hash = hashlib.sha256(profile).hexdigest()

                oriented = ImageOps.exif_transpose(source)
                cand.width, cand.height = oriented.size
                cand.oriented_width, cand.oriented_height = oriented.size
                cand.aspect_ratio = cand.width / cand.height
                if (
                    cand.declared_width
                    and cand.declared_height
                    and (cand.declared_width != source_width or cand.declared_height != source_height)
                ):
                    cand.warnings.append(
                        "provider dimensions differ from downloaded representation: "
                        f"declared {cand.declared_width}x{cand.declared_height}, measured {source_width}x{source_height}"
                    )
                rgb = self._profile_aware_rgb(oriented)
                gray = rgb.convert("L")

                crop_box, crop_confidence = self._front_crop_box(gray)
                cand.crop_box = crop_box
                cand.crop_confidence = crop_confidence
                cand.source_form = "compound_package_scan" if crop_box else "source_image_unverified"
                analysis_gray = gray.crop(crop_box) if crop_box else gray
                analysis_rgb = rgb.crop(crop_box) if crop_box else rgb
                cand.effective_width, cand.effective_height = analysis_gray.size
                cand.crop_penalty = 0.0 if crop_box is None else (1.0 - crop_confidence) * 8.0

                max_analysis = 1000
                scale = min(1.0, max_analysis / max(analysis_gray.size))
                analysis_size = (
                    max(32, round(analysis_gray.width * scale)),
                    max(32, round(analysis_gray.height * scale)),
                )
                norm_gray = analysis_gray.resize(analysis_size, Image.Resampling.LANCZOS)
                norm_rgb = analysis_rgb.resize(analysis_size, Image.Resampling.LANCZOS)
                arr_l = np.asarray(norm_gray, dtype=np.float32)
                arr_rgb = np.asarray(norm_rgb, dtype=np.float32)

                cand.perceptual_hash = self._perceptual_hash(analysis_gray)
                cand.difference_hash = self._difference_hash(analysis_gray)
                cand.alternate_hashes = [(cand.perceptual_hash, cand.difference_hash)]
                if 0.74 <= gray.width / gray.height <= 1.35 and gray.width != gray.height:
                    side = min(gray.size)
                    for box in ((0, 0, side, side), (gray.width - side, gray.height - side, gray.width, gray.height)):
                        crop = gray.crop(box)
                        pair = (self._perceptual_hash(crop), self._difference_hash(crop))
                        if pair not in cand.alternate_hashes:
                            cand.alternate_hashes.append(pair)

                denoised = norm_gray.filter(ImageFilter.GaussianBlur(radius=0.7))
                edges = denoised.filter(ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], 1, 0))
                cand.sharpness = float(ImageStat.Stat(edges).var[0])
                cand.blur_score = max(0.0, min(100.0, 24.0 * math.log10(max(cand.sharpness, 1.0))))
                oversharpen_penalty = min(25.0, max(0.0, cand.sharpness - 1700.0) / 100.0)

                cand.entropy = float(norm_gray.entropy())
                cand.contrast = float(ImageStat.Stat(norm_gray).stddev[0])
                p1, p99 = (float(value) for value in np.percentile(arr_l, (1, 99)))
                cand.dynamic_range = p99 - p1
                # Large white or black artwork is often intentional. Only
                # count isolated clipping in otherwise varied regions.
                clipped = float(np.mean((arr_l <= 1) | (arr_l >= 254)))
                range_penalty = max(0.0, 75.0 - cand.dynamic_range) * 0.35
                cand.tonal_integrity_score = max(
                    0.0, 100.0 - min(18.0, max(0.0, clipped - 0.15) * 45.0) - range_penalty
                )

                red, green, blue = arr_rgb[:, :, 0], arr_rgb[:, :, 1], arr_rgb[:, :, 2]
                rg = red - green
                yb = 0.5 * (red + green) - blue
                cand.colorfulness = float(
                    math.hypot(float(np.std(rg)), float(np.std(yb)))
                    + 0.3 * math.hypot(float(np.mean(rg)), float(np.mean(yb)))
                )
                channel_means = np.asarray((np.mean(red), np.mean(green), np.mean(blue)))
                cast = float(np.ptp(channel_means))
                cast_penalty = max(0.0, cast - 105.0) * 0.25
                cand.color_integrity_score = max(55.0, 100.0 - cast_penalty)

                # JPEG blocks are on the source's 8-pixel grid. A 1000-pixel
                # analysis resize destroys that alignment and hides artifacts.
                native_patch = np.asarray(
                    gray.crop((0, 0, min(1024, gray.width), min(1024, gray.height))), dtype=np.float32
                )
                cand.blockiness, block_severity = self._blockiness(native_patch)
                quantization_penalty = 0.0
                quantization = getattr(source, "quantization", None)
                if cand.format == "JPEG" and isinstance(quantization, dict) and quantization:
                    q_values = [value for table in quantization.values() for value in table]
                    q_mean = float(np.mean(q_values)) if q_values else 0.0
                    quantization_penalty = min(45.0, max(0.0, q_mean - 12.0) * 0.75)
                cand.compression_score = max(0.0, 100.0 - block_severity - quantization_penalty)

                cand.has_scanner_borders = self._has_uniform_border(arr_l)
                (
                    cand.skew_degrees,
                    cand.skew_confidence,
                    cand.moire_confidence,
                    cand.streak_confidence,
                ) = self._scan_defect_signals(norm_gray)
                cand.cleanliness_score = (
                    (82.0 if cand.has_scanner_borders else 96.0)
                    - abs(cand.skew_degrees) * cand.skew_confidence
                    - 4.0 * cand.moire_confidence
                    - 4.0 * cand.streak_confidence
                )

                native_center = analysis_gray.crop(
                    (
                        max(0, (analysis_gray.width - min(768, analysis_gray.width)) // 2),
                        max(0, (analysis_gray.height - min(768, analysis_gray.height)) // 2),
                        min(analysis_gray.width, (analysis_gray.width + min(768, analysis_gray.width)) // 2),
                        min(analysis_gray.height, (analysis_gray.height + min(768, analysis_gray.height)) // 2),
                    )
                )
                native_arr = np.asarray(native_center, dtype=np.float32)
                down = native_center.resize(
                    (max(16, native_center.width // 2), max(16, native_center.height // 2)), Image.Resampling.BILINEAR
                )
                restored = down.resize(native_center.size, Image.Resampling.BILINEAR)
                high_frequency = np.abs(native_arr - np.asarray(restored, dtype=np.float32))
                hf_mean = float(np.mean(high_frequency))
                hf_p95 = float(np.percentile(high_frequency, 95))
                if min(cand.effective_width, cand.effective_height) >= 1400 and float(np.std(arr_l)) > 3.0:
                    low_frequency_confidence = max(0.0, min(1.0, (4.0 - hf_mean) / 3.5))
                    weak_edge_confidence = max(0.0, min(1.0, (15.0 - hf_p95) / 13.0))
                    blur_confidence = max(0.0, min(1.0, (45.0 - cand.sharpness) / 40.0))
                    cand.upscale_confidence = (
                        0.35 * low_frequency_confidence + 0.25 * weak_edge_confidence + 0.40 * blur_confidence
                    )
                else:
                    cand.upscale_confidence = 0.0
                cand.is_fake_upscale = cand.upscale_confidence >= 0.60

                native_scale = max(0.35, 1.0 - 0.7 * _actionable_upscale_confidence(cand.upscale_confidence))
                effective_min_dim = min(cand.effective_width, cand.effective_height) * native_scale
                cand.effective_pixels = round(cand.effective_width * cand.effective_height * native_scale**2)
                dimension_detail = max(0.0, min(100.0, 20.0 + 25.0 * math.log2(max(1.0, effective_min_dim / 500.0))))
                cand.detail_score = max(
                    0.0,
                    min(100.0, 0.72 * dimension_detail + 0.28 * cand.blur_score - oversharpen_penalty),
                )

                if cand.format not in {"JPEG", "PNG", "WEBP"}:
                    raise ValueError(f"unsupported cover format: {cand.format}")

                cand.metric_confidence = {
                    "dimensions": 1.0,
                    "detail": 0.82,
                    "compression": 0.85 if cand.format == "JPEG" else 0.65,
                    "upscale": 0.68,
                    "border": 0.75,
                    "skew": cand.skew_confidence,
                    "moire": 0.35,
                    "streak": 0.35,
                    "watermark": 0.9 if cand.watermark_confidence else 0.0,
                }

                self._finalize_visual_score(cand)
                _apply_visual_provenance(cand)
                self._store_analysis_cache(cand)
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
            cand.error = f"image_parse_error: {exc}"
            cand.rejection_reasons.append(cand.error)
            cand.total_score = -9999.0

        return cand

    @staticmethod
    def _deterministic_key(cand: CoverCandidate) -> tuple[float, float, float, int, str, str, str, int]:
        """A provider-neutral ordering key; the final fields only break true ties."""
        return (
            round(cand.visual_quality_score, 6),
            round(cand.role_confidence, 6),
            round(cand.provenance_confidence, 6),
            cand.effective_pixels,
            cand.stable_id,
            cand.provider,
            cand.original_url,
            -cand.provider_order,
        )

    def _assign_duplicate_clusters(self, candidates: list[CoverCandidate]) -> None:
        clusters: list[list[CoverCandidate]] = []
        for cand in sorted(
            candidates, key=lambda item: (item.release_key, item.stable_id, item.provider, item.original_url)
        ):
            match: list[CoverCandidate] | None = None
            for cluster in clusters:
                representative = cluster[0]
                exact = bool(
                    cand.source_sha256
                    and cand.source_sha256 == representative.source_sha256
                    and cand.release_tier == representative.release_tier
                    and self._same_release_identity(cand, representative)
                )
                near = (
                    cand.release_tier == representative.release_tier
                    and self._same_release_identity(cand, representative)
                    and self._near_same_scan(cand, representative)
                )
                if exact or near:
                    match = cluster
                    break
            if match is None:
                clusters.append([cand])
            else:
                match.append(cand)

        for cluster in clusters:
            cluster_source = min(item.source_sha256 or item.stable_id for item in cluster)
            cluster_identity = min(
                item.release_key or f"tier:{item.release_tier.name}:{item.stable_id}" for item in cluster
            )
            cluster_id = hashlib.sha256(f"{cluster_identity}|{cluster_source}".encode()).hexdigest()[:16]
            best = max(cluster, key=self._deterministic_key)
            for cand in cluster:
                cand.duplicate_cluster_id = cluster_id
                if cand is not best:
                    cand.duplicate_of = best.stable_id

    def _same_release_identity(self, left: CoverCandidate, right: CoverCandidate) -> bool:
        if left.release_key and left.release_key == right.release_key:
            return True
        # The two upstream IDs are independently checked against the local
        # edition before they are supplied to this fetcher.
        return bool(
            self.mb_rel_id
            and self.discogs_id
            and {left.release_key, right.release_key}
            == {f"mb:{self.mb_rel_id.casefold()}", f"discogs:{self.discogs_id}"}
        )

    def _promote_unknown_fronts(self, candidates: list[CoverCandidate]) -> None:
        known_fronts = [item for item in candidates if item.role_tier >= RoleTier.LIKELY_FRONT]
        for cand in candidates:
            if cand.role_tier != RoleTier.UNKNOWN:
                continue
            for known in known_fronts:
                if cand.release_tier != known.release_tier or not self._same_release_identity(cand, known):
                    continue
                if self._near_same_scan(cand, known):
                    cand.role_tier = RoleTier.LIKELY_FRONT
                    cand.role_confidence = max(cand.role_confidence, 0.76)
                    cand.role_evidence += (f"near-duplicate of front {known.stable_id}",)
                    break

    def _best_in_release_tier(self, candidates: list[CoverCandidate]) -> CoverCandidate | None:
        if not candidates:
            return None
        acceptable = [
            item
            for item in candidates
            if item.role_tier >= RoleTier.LIKELY_FRONT
            and item.release_tier != ReleaseTier.CONFLICT
            and not item.release_conflicts
            and not item.error
            and not item.rejection_reasons
        ]
        if not acceptable:
            return None

        best_tier = max(item.release_tier for item in acceptable)
        tier_pool = [item for item in acceptable if item.release_tier == best_tier]
        best_quality = max(item.visual_quality_score for item in tier_pool)
        comparable = [
            item for item in tier_pool if item.visual_quality_score >= best_quality - VISUAL_EQUIVALENCE_MARGIN
        ]
        winner = max(
            comparable,
            key=lambda item: (item.role_confidence, *self._deterministic_key(item), item.original_url, item.provider),
        )
        if len(comparable) > 1:
            winner.selection_explanation.append(
                f"front-role confidence breaks quality differences within {VISUAL_EQUIVALENCE_MARGIN:g} points"
            )

        # Perceptual similarity does not prove edition identity. Crossing an
        # established release tier requires an explicit user choice.
        return winner

    def find_best_cover(
        self,
        verbose: bool | int | None = None,
    ) -> tuple[CoverCandidate | None, CoverCandidate | None, CoverCandidate | None, list[CoverCandidate]]:
        if verbose is not None:
            if isinstance(verbose, bool):
                verbosity = 1 if verbose else 0
            elif isinstance(verbose, int):
                verbosity = verbose
            else:
                verbosity = 0
        else:
            verbosity = getattr(self, "verbosity", 1 if self.verbose else 0)
        is_verbose = verbosity > 0
        render_all = verbosity >= 2 or self.manual_all
        if is_verbose:
            print(f"[VERBOSE][COVER] Starting multi-provider search for '{self.artist}' - '{self.album}'...")

        provider_queries = [
            ("Apple Music (iTunes)", self.query_itunes),
            ("Cover Art Archive (CAA)", self.query_cover_art_archive),
            ("Deezer CDN", self.query_deezer),
            ("TheAudioDB", self.query_theaudiodb),
            ("Discogs API", self.query_discogs),
            ("Local Files / Scans", self.query_local),
            ("Amazon", self.query_amazon),
            ("Fanart.tv", self.query_fanart_tv),
            ("Bandcamp", self.query_bandcamp),
            ("Tidal", self.query_tidal),
            ("Qobuz", self.query_qobuz),
        ]

        raw_candidates: list[CoverCandidate] = []
        for p_name, query_fn in provider_queries:
            try:
                cands = query_fn()
                raw_candidates.extend(cands)
                if is_verbose:
                    print(f"[VERBOSE][COVER] {p_name:<24} -> {len(cands)} candidate(s) found")
            except Exception as e:
                self.last_fetch_errors[p_name] = f"provider_error: {e}"
                if is_verbose:
                    print(f"[VERBOSE][COVER] {p_name:<24} -> ERROR: {e}")

        if not raw_candidates:
            if is_verbose:
                print("[VERBOSE][COVER] No candidates found across any providers.")
            return None, None, None, []

        unique_cands: dict[tuple[str, str, str], CoverCandidate] = {}
        for cand in raw_candidates:
            source_url = cand.original_url or cand.url
            identity_key = (source_url, cand.release_key, cand.provider_release_id)
            previous = unique_cands.get(identity_key)
            if previous is None:
                unique_cands[identity_key] = cand
                continue
            best = max(
                (previous, cand),
                key=lambda item: (
                    item.release_tier,
                    item.role_tier,
                    item.role_confidence,
                    item.source_verified_original,
                    item.provider,
                    item.stable_id,
                ),
            )
            representations: dict[str, ImageRepresentation] = {}
            for representation in (*previous.representations, *cand.representations):
                known = representations.get(representation.url)

                def evidence_key(item: ImageRepresentation) -> tuple[bool, bool, int, int, str]:
                    return (
                        item.kind in {RepresentationKind.ORIGINAL, RepresentationKind.LOCAL},
                        not item.generated,
                        item.declared_width,
                        item.declared_height,
                        item.kind.value,
                    )

                if known is None or evidence_key(representation) > evidence_key(known):
                    representations[representation.url] = representation
            best.representations = tuple(
                sorted(
                    representations.values(),
                    key=lambda representation: (
                        representation.url == source_url,
                        representation.declared_width * representation.declared_height,
                        representation.url,
                    ),
                    reverse=True,
                )
            )
            best.preview_urls = {**previous.preview_urls, **cand.preview_urls}
            best.warnings = sorted(set(previous.warnings + cand.warnings))
            unique_cands[identity_key] = best

        metadata_candidates: list[CoverCandidate] = []
        prefilter_rejected: list[CoverCandidate] = []
        for cand in unique_cands.values():
            if cand.role_tier == RoleTier.REJECT:
                cand.rejection_reasons.append("not_front_cover")
            if cand.release_tier == ReleaseTier.CONFLICT or cand.release_conflicts:
                cand.rejection_reasons.append("release_conflict")
            if cand.album_match_ratio < 0.7:
                cand.rejection_reasons.append("album_title_mismatch")
            if cand.release_tier == ReleaseTier.TITLE_ONLY:
                conflicts = _title_release_conflicts(self.album, cand.title)
                if conflicts:
                    cand.release_conflicts += tuple(f"unexpected edition marker: {item}" for item in conflicts)
                    cand.release_tier = ReleaseTier.CONFLICT
                    cand.release_confidence = 0.0
                    cand.rejection_reasons.append("title_indicates_different_edition")
            if cand.rejection_reasons:
                prefilter_rejected.append(cand)
            else:
                metadata_candidates.append(cand)
        metadata_candidates.sort(
            key=lambda cand: (
                -int(cand.release_tier),
                -int(cand.role_tier),
                -cand.album_match_ratio,
                cand.provider,
                cand.provider_order,
                cand.stable_id,
            )
        )
        candidate_list: list[CoverCandidate] = []
        unknown_counts: dict[str, int] = {}
        title_only_counts: dict[str, int] = {}
        for cand in metadata_candidates:
            if cand.release_tier == ReleaseTier.TITLE_ONLY:
                title_count = title_only_counts.get(cand.provider, 0)
                if (
                    not (self.manual_all or self.exhaustive_categories)
                    and title_count >= MAX_TITLE_ONLY_CANDIDATES_PER_PROVIDER
                ):
                    cand.rejection_reasons.append("metadata_prefilter_title_only_limit")
                    prefilter_rejected.append(cand)
                    continue
                title_only_counts[cand.provider] = title_count + 1
            if cand.role_tier == RoleTier.UNKNOWN:
                count = unknown_counts.get(cand.provider, 0)
                if (
                    not (self.manual_all or self.exhaustive_categories)
                    and count >= MAX_UNKNOWN_ROLE_CANDIDATES_PER_PROVIDER
                ):
                    cand.rejection_reasons.append("metadata_prefilter_unknown_role_limit")
                    prefilter_rejected.append(cand)
                    continue
                unknown_counts[cand.provider] = count + 1
            candidate_list.append(cand)
        if is_verbose:
            print(f"[VERBOSE][COVER] Probing {len(candidate_list)} of {len(raw_candidates)} discovered candidates...")

        evaluated: list[CoverCandidate] = list(prefilter_rejected)
        tiers = sorted({c.release_tier for c in candidate_list}, reverse=True)
        for tier in tiers:
            tier_candidates = [c for c in candidate_list if c.release_tier == tier]
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                future_to_cand = {executor.submit(self.evaluate_candidate, c): c for c in tier_candidates}
                for future in concurrent.futures.as_completed(future_to_cand):
                    try:
                        evaluated.append(future.result())
                    except Exception as e:
                        c = future_to_cand[future]
                        c.error = str(e)
                        evaluated.append(c)
            if not (self.manual_all or self.exhaustive_categories) and any(
                c.role_tier >= RoleTier.LIKELY_FRONT
                and not c.error
                and not c.rejection_reasons
                and c.visual_quality_score > 0
                for c in tier_candidates
            ):
                for lower in candidate_list:
                    if lower.release_tier < tier:
                        lower.rejection_reasons.append("not_probed_lower_release_tier")
                        evaluated.append(lower)
                break

        evaluated.sort(key=lambda cand: (cand.provider, cand.provider_order, cand.stable_id))
        valid = [
            cand for cand in evaluated if not cand.error and (cand.visual_quality_score > 0.0 or cand.total_score > 0.0)
        ]
        self._promote_unknown_fronts(valid)
        self._assign_duplicate_clusters(valid)
        for cand in valid:
            if min(cand.effective_width, cand.effective_height) >= MIN_USABLE_COVER_SIDE:
                continue
            if any(
                other is not cand
                and other.role_tier >= RoleTier.LIKELY_FRONT
                and not other.error
                and not other.rejection_reasons
                and min(other.effective_width, other.effective_height) >= MIN_USABLE_COVER_SIDE
                and other.release_tier == cand.release_tier
                and self._same_release_identity(cand, other)
                for other in valid
            ):
                cand.rejection_reasons.append("low_resolution_with_usable_same_release_cover")
        ranked = sorted(valid, key=self._deterministic_key, reverse=True)

        if is_verbose:
            for c in evaluated:
                if c.error or c.rejection_reasons or c.total_score <= 0:
                    reason = c.error or ", ".join(c.rejection_reasons) or "quality <= 0"
                    print(f"[VERBOSE][COVER] Candidate {c.provider:<28} -> REJECTED: {reason}")
                else:
                    border_str = "Yes" if c.has_scanner_borders else "No"
                    print(
                        f"[VERBOSE][COVER] Candidate {c.provider:<28} | {c.width}x{c.height} | aspect: {c.aspect_ratio:.3f} | "
                        f"release: {c.release_tier.name} | role: {c.role_tier.name} | "
                        f"detail: {c.detail_score:.1f} | compression: {c.compression_score:.1f} | "
                        f"upscale: {c.upscale_confidence:.2f} | border: {border_str} | QUALITY: {c.visual_quality_score:.1f}"
                    )
                    if verbosity >= 2:
                        print(
                            f"[VERBOSE][COVER]   id={c.stable_id} release_evidence={c.release_evidence} "
                            f"role_evidence={c.role_evidence} representation={c.representation_kind.value} "
                            f"evaluated_url={c.evaluated_url} bytes={c.file_size} "
                            f"effective={c.effective_width}x{c.effective_height} "
                            f"crop={c.crop_box} crop_confidence={c.crop_confidence:.2f} "
                            f"watermark={c.watermark_confidence:.2f} quality_components={c.score_breakdown}"
                        )

        pool = [cand for cand in ranked if cand.role_tier >= RoleTier.LIKELY_FRONT and not cand.rejection_reasons]
        if not pool:
            pool = [cand for cand in ranked if cand.role_tier == RoleTier.UNKNOWN and not cand.rejection_reasons]

        digital_cands = [cand for cand in pool if cand.provenance == ArtworkProvenance.DIGITAL_SERVICE]
        scan_cands = [cand for cand in pool if cand.provenance == ArtworkProvenance.PHYSICAL_SCAN]

        best_digital = self._best_in_release_tier(digital_cands)
        best_scan = self._best_in_release_tier(scan_cands)
        winner = self._best_in_release_tier(pool)
        best_release_tier = max((cand.release_tier for cand in pool), default=ReleaseTier.CONFLICT)
        preferred = (
            scan_cands if self.cover_mode == "physical" else digital_cands if self.cover_mode == "digital" else []
        )
        preferred = [cand for cand in preferred if cand.release_tier == best_release_tier]
        preferred_winner = self._best_in_release_tier(preferred)
        if (
            winner
            and preferred_winner
            and preferred_winner.visual_quality_score >= winner.visual_quality_score - VISUAL_EQUIVALENCE_MARGIN
        ):
            winner = preferred_winner

        if is_verbose:
            if winner:
                print(
                    f"[VERBOSE][COVER] WINNER SELECTED : {winner.provider} ({winner.width}x{winner.height}, quality: {winner.visual_quality_score:.1f})"
                )
                for reason in winner.selection_explanation:
                    print(f"[VERBOSE][COVER]   {reason}")
            else:
                print("[VERBOSE][COVER] WINNER SELECTED : None (no valid images)")
            if best_digital:
                print(
                    f"[VERBOSE][COVER] Best Digital    : {best_digital.provider} ({best_digital.width}x{best_digital.height}, quality: {best_digital.visual_quality_score:.1f})"
                )
            if best_scan:
                print(
                    f"[VERBOSE][COVER] Best Scan       : {best_scan.provider} ({best_scan.width}x{best_scan.height}, quality: {best_scan.visual_quality_score:.1f})"
                )
            if verbosity >= 2:
                print(f"[VERBOSE][COVER] Performance: {self.performance}")

        if render_all:
            cands_with_images = [
                c for c in evaluated if (is_valid_manual_cover(c) if self.manual_all else bool(c.raw_data))
            ]
            if cands_with_images:
                print(f"\n{'=' * 74}")
                print(
                    f"[VERBOSE][COVER-PREVIEW] Rendering {len(cands_with_images)} found cover candidate(s) to terminal (-vv)"
                )
                print(f"{'=' * 74}")
                for idx, c in enumerate(cands_with_images, start=1):
                    role_flags = []
                    if c is winner:
                        role_flags.append("OVERALL WINNER")
                    if c is best_digital:
                        role_flags.append("BEST DIGITAL")
                    if c is best_scan:
                        role_flags.append("BEST SCAN")
                    role_str = f" [★ {', '.join(role_flags)}]" if role_flags else ""

                    status_str = (
                        f"REJECTED ({c.error})"
                        if c.error
                        else f"REJECTED ({', '.join(c.rejection_reasons)})"
                        if c.rejection_reasons
                        else (
                            f"REJECTED (quality {c.visual_quality_score:.1f} <= 0)" if c.total_score <= 0 else "VALID"
                        )
                    )
                    border_str = "Yes" if c.has_scanner_borders else "No"
                    print(f"\n--- [COVER {idx}/{len(cands_with_images)}] {c.provider}{role_str} ---")
                    print(
                        f"    Dimensions: {c.width}x{c.height} | Aspect: {c.aspect_ratio:.3f} | "
                        f"Sharpness: {c.sharpness:.1f} | Border: {border_str} | Score: {c.total_score:.1f} | Status: {status_str}"
                    )
                    if c.url:
                        print(f"    Source: {c.url}")

                    img_to_render: Path | None = None
                    temp_file_to_clean: Path | None = None
                    if c.local_saved_path and Path(c.local_saved_path).is_file():
                        img_to_render = Path(c.local_saved_path)
                    else:
                        if c.raw_data is None:
                            continue
                        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp_f:
                            img_to_render = Path(tmp_f.name)
                            temp_file_to_clean = img_to_render
                        save_optimized_cover(
                            c.raw_data,
                            img_to_render,
                            crop_box=c.crop_box if c.crop_confidence >= 0.8 else None,
                        )

                    try:
                        rendered = emit_kitty_image(img_to_render)
                        if not rendered:
                            print("    [Image data present, Kitty graphics protocol not active]")
                    finally:
                        if temp_file_to_clean and temp_file_to_clean.is_file():
                            try:
                                temp_file_to_clean.unlink()
                            except OSError:
                                pass
                print(f"{'=' * 74}\n")

        if self.folder and self.write_preview_files:
            for candidate, name in ((best_digital, ".cover_digital.jpg"), (best_scan, ".cover_scan.jpg")):
                if candidate and candidate.raw_data:
                    prepared = prepare_best_cover(
                        self, evaluated, self.folder / name, preferred=candidate, allow_fallback=False
                    )
                    if prepared:
                        candidate.local_saved_path = prepared.prepared_path
                    else:
                        candidate.warnings.append(
                            "preview materialization failed; selection will retry eligible covers"
                        )

        return winner, best_digital, best_scan, evaluated


@dataclass
class CoverChoice:
    """One category representative and the exact playback derivative shown in the menu."""

    category: str
    candidate: CoverCandidate
    prepared_path: Path | None = None
    prepared_width: int = 0
    prepared_height: int = 0
    applied_crop: bool = False


def cover_category(candidate: CoverCandidate) -> str:
    """Local availability takes precedence over an image's earlier origin."""
    if candidate.identity_scope == "local" or candidate.provider.startswith("Local"):
        return "local"
    if candidate.provenance == ArtworkProvenance.DIGITAL_SERVICE:
        return "digital"
    if candidate.provenance == ArtworkProvenance.PHYSICAL_SCAN:
        return "physical"
    return "unknown"


def select_cover_categories(fetcher: CoverFetcher, candidates: list[CoverCandidate]) -> list[CoverChoice]:
    """Rank each source category independently without discarding local duplicates."""
    categories: dict[str, list[CoverCandidate]] = {name: [] for name in ("digital", "physical", "local", "unknown")}
    for candidate in candidates:
        if not is_valid_manual_cover(candidate):
            continue
        categories[cover_category(candidate)].append(candidate)
    return [
        CoverChoice(category, best)
        for category in categories
        if (best := fetcher._best_in_release_tier(categories[category])) is not None
    ]


def prepare_best_cover(
    fetcher: CoverFetcher,
    candidates: list[CoverCandidate],
    destination: Path,
    *,
    preferred: CoverCandidate | None = None,
    category: str | None = None,
    allow_fallback: bool = True,
) -> CoverChoice | None:
    """Materialize eligible artwork before publishing, retrying validated alternatives."""
    pool = [c for c in candidates if is_valid_manual_cover(c) and (category is None or cover_category(c) == category)]
    deferred = sorted(
        (
            c
            for c in candidates
            if c.rejection_reasons == ["not_probed_lower_release_tier"]
            and (category is None or cover_category(c) == category)
        ),
        key=lambda c: (int(c.release_tier), c.provider, c.stable_id),
        reverse=True,
    )
    candidate = (
        preferred if preferred is not None and is_valid_manual_cover(preferred) else fetcher._best_in_release_tier(pool)
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    while True:
        if candidate is None:
            if not allow_fallback or not deferred:
                return None
            next_candidate = deferred.pop(0)
            next_candidate.rejection_reasons.remove("not_probed_lower_release_tier")
            fetcher.evaluate_candidate(next_candidate)
            if is_valid_manual_cover(next_candidate):
                pool.append(next_candidate)
            candidate = fetcher._best_in_release_tier(pool)
            continue
        crop = candidate.crop_box if candidate.crop_confidence >= 0.8 else None
        fd, name = tempfile.mkstemp(prefix=".cover-materialize-", suffix=".jpg", dir=destination.parent)
        os.close(fd)
        attempt = Path(name)
        try:
            width, height, _size = save_optimized_cover(candidate.raw_data or b"", attempt, crop_box=crop)
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            candidate.rejection_reasons.append("materialization_failed")
            candidate.warnings.append(f"cover materialization failed: {exc}")
            print(f"  -> Artwork from {candidate.provider} could not be prepared: {exc}")
        else:
            # Publication errors abort the enclosing album transaction rather
            # than pretending that a different source can repair the filesystem.
            os.replace(attempt, destination)
            return CoverChoice(cover_category(candidate), candidate, destination, width, height, crop is not None)
        finally:
            attempt.unlink(missing_ok=True)
        if not allow_fallback:
            return None
        pool = [c for c in pool if not c.rejection_reasons]
        candidate = fetcher._best_in_release_tier(pool)


def prepare_cover_choices(
    choices: list[CoverChoice],
    directory: Path,
    *,
    fetcher: CoverFetcher | None = None,
    candidates: list[CoverCandidate] | None = None,
) -> None:
    """Materialize each selected front once; menu and final staging use these bytes."""
    directory.mkdir(parents=True, exist_ok=True)
    prepared: list[CoverChoice] = []
    for index, choice in enumerate(choices, start=1):
        if fetcher is not None:
            option = prepare_best_cover(
                fetcher,
                candidates or [],
                directory / f"{index}-{choice.category}.jpg",
                preferred=choice.candidate,
                category=choice.category,
            )
            if option is not None:
                prepared.append(option)
            continue
        source = choice.candidate.raw_data
        if not source:
            raise ValueError(f"{choice.category} cover has no image data")
        crop = choice.candidate.crop_box if choice.candidate.crop_confidence >= 0.8 else None
        destination = directory / f"{index}-{choice.category}.jpg"
        width, height, _size = save_optimized_cover(source, destination, crop_box=crop)
        choice.prepared_path = destination
        choice.prepared_width = width
        choice.prepared_height = height
        choice.applied_crop = crop is not None
        prepared.append(choice)
    choices[:] = prepared


def save_optimized_cover(
    data: bytes,
    output_path: Path,
    max_dim: int | None = None,
    progressive: bool = False,
    quality: int = 95,
    crop_box: tuple[int, int, int, int] | None = None,
) -> tuple[int, int, int]:
    """Create a compatibility JPEG derivative; this is not the archival source copy."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(data)) as im:
        can_preserve_jpeg = (
            crop_box is None
            and str(im.format or "").upper() == "JPEG"
            and im.mode in {"RGB", "L"}
            and int(im.getexif().get(274, 1)) in {0, 1}
            and not im.info.get("progressive")
            and (not max_dim or max(im.size) <= max_dim)
        )
        if can_preserve_jpeg:
            im.load()
            output_path.write_bytes(data)
            return im.width, im.height, len(data)
        im_oriented = ImageOps.exif_transpose(im)
        if crop_box is not None:
            if not (
                0 <= crop_box[0] < crop_box[2] <= im_oriented.width
                and 0 <= crop_box[1] < crop_box[3] <= im_oriented.height
            ):
                raise ValueError("cover crop is outside the oriented source image")
            im_oriented = im_oriented.crop(crop_box)
        rgb_im, output_profile = _compatibility_rgb(im_oriented)
        if max_dim and max_dim > 0 and (rgb_im.width > max_dim or rgb_im.height > max_dim):
            rgb_im.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        w, h = rgb_im.size
        options = {"icc_profile": output_profile} if output_profile else {}
        rgb_im.save(
            output_path,
            format="JPEG",
            quality=quality,
            progressive=progressive,
            optimize=True,
            **options,
        )
        return w, h, output_path.stat().st_size


def save_original_cover(data: bytes, output_stem: Path) -> Path:
    """Validate and save the winning source byte-for-byte with its real extension."""
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("cover exceeds source byte limit")
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > MAX_IMAGE_PIXELS:
            raise ValueError("cover exceeds source pixel limit")
        image.verify()
        image_format = str(image.format or "").upper()
    extension = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(image_format)
    if extension is None:
        raise ValueError(f"unsupported archival cover format: {image_format or 'unknown'}")
    output_path = output_stem.parent / (output_stem.name + extension)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, output_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return output_path


def create_embedded_picture_data(
    data: bytes,
    max_dim: int = 1600,
    quality: int = 90,
    crop_box: tuple[int, int, int, int] | None = None,
) -> bytes:
    """Creates hardware-DAP-compliant baseline JPEG data for FLAC METADATA_BLOCK_PICTURE."""
    with Image.open(io.BytesIO(data)) as im:
        im_oriented = ImageOps.exif_transpose(im)
        if crop_box is not None:
            if not (
                0 <= crop_box[0] < crop_box[2] <= im_oriented.width
                and 0 <= crop_box[1] < crop_box[3] <= im_oriented.height
            ):
                raise ValueError("cover crop is outside the oriented source image")
            im_oriented = im_oriented.crop(crop_box)
        rgb_im, output_profile = _compatibility_rgb(im_oriented)
        if max_dim > 0 and (rgb_im.width > max_dim or rgb_im.height > max_dim):
            rgb_im.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        options = {"icc_profile": output_profile} if output_profile else {}
        rgb_im.save(buf, format="JPEG", quality=quality, progressive=False, optimize=True, **options)
        return buf.getvalue()


def _get_terminal_geometry() -> tuple[int, int, int, int]:
    """Returns (cols, lines, xpix, ypix) of the terminal."""
    import fcntl
    import struct
    import termios

    for fd in (1, 0):
        try:
            data = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
            lines, cols, xpix, ypix = struct.unpack("HHHH", data)
            if lines > 0 and cols > 0:
                return cols, lines, xpix, ypix
        except Exception:
            pass
    try:
        with open("/dev/tty", "rb") as f:
            data = fcntl.ioctl(f.fileno(), termios.TIOCGWINSZ, b"\x00" * 8)
            lines, cols, xpix, ypix = struct.unpack("HHHH", data)
            if lines > 0 and cols > 0:
                return cols, lines, xpix, ypix
    except Exception:
        pass
    ts = shutil.get_terminal_size((80, 24))
    return ts.columns, ts.lines, 0, 0


def create_comparison_image(
    cand1: CoverCandidate | None,
    cand2: CoverCandidate | None,
    winner: CoverCandidate | None,
    output_path: Path,
    target_dim: int | None = None,
    options: list[CoverChoice] | None = None,
) -> Path | None:
    """Generates a high-aesthetic, high-resolution comparison image for Kitty terminal."""
    if not cand1 and not cand2 and not winner and not options:
        return None

    if target_dim is None:
        env_val = os.environ.get("COVER_PREVIEW_SIZE")
        if env_val and env_val.isdigit():
            target_dim = int(env_val)
        else:
            target_dim = 1200

    banner_h = 74
    gap = 20
    pad_side = 12
    pad_top = 8
    pad_bottom = 2

    if options is not None:
        panels = [(item.candidate, item.prepared_path, item.category.upper()) for item in options]
    else:
        panels = [(candidate, None, "") for candidate in (cand1, cand2) if candidate is not None]
        if winner is not None and all(winner is not candidate for candidate, _path, _label in panels):
            panels.append((winner, None, ""))
    total_w = target_dim * len(panels) + gap * (len(panels) - 1) + pad_side * 2
    total_h = target_dim + banner_h + pad_top + pad_bottom

    comp = Image.new("RGB", (total_w, total_h), color=(18, 20, 26))
    draw = ImageDraw.Draw(comp)

    # Load system TrueType fonts if available for crisp high-resolution typography
    font_bold = None
    font_regular = None
    for fpath in (
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ):
        if os.path.isfile(fpath):
            try:
                from PIL import ImageFont

                font_bold = ImageFont.truetype(fpath, 19)
                font_regular = ImageFont.truetype(fpath, 14)
                break
            except Exception:
                pass

    def draw_panel(
        cand: CoverCandidate | None,
        x_offset: int,
        default_title: str,
        is_win: bool,
        accent_col: tuple[int, int, int],
        prepared_path: Path | None,
        category_label: str,
    ) -> None:
        if not cand:
            return
        p_rect = [(x_offset, pad_top), (x_offset + target_dim, pad_top + banner_h)]
        hdr_bg = (28, 32, 42) if not is_win else (40, 36, 20)
        border_col = accent_col if not is_win else (255, 204, 50)
        draw.rectangle(p_rect, fill=hdr_bg, outline=border_col, width=3 if is_win else 1)

        title_text = f"{default_title}. {category_label or artwork_label(cand)}"
        disp_title = title_text
        max_title_w = target_dim - 24
        title_font = font_bold
        if title_font and title_font.getlength(disp_title) > max_title_w:
            from PIL import ImageFont

            for size in range(18, 8, -1):
                candidate_font = ImageFont.truetype(
                    getattr(font_bold, "path", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"), size
                )
                title_font = candidate_font
                if candidate_font.getlength(disp_title) <= max_title_w:
                    break
        draw.text((x_offset + 12, pad_top + 8), disp_title, fill=(255, 255, 255), font=title_font)

        chosen_option = next((item for item in options or [] if item.candidate is cand), None)
        shown_width = chosen_option.prepared_width if chosen_option else cand.width
        shown_height = chosen_option.prepared_height if chosen_option else cand.height
        stats_text = f"{shown_width}×{shown_height} px | Quality: {cand.visual_quality_score:.1f}"
        draw.text((x_offset + 12, pad_top + 30), stats_text, fill=(180, 190, 210), font=font_regular)

        badge_text = "★ ВЫБОР СИСТЕМЫ" if is_win else "ВАРИАНТ ДЛЯ ВЫБОРА"
        badge_col = (255, 215, 0) if is_win else (140, 150, 170)
        draw.text((x_offset + 12, pad_top + 50), badge_text, fill=badge_col, font=font_bold)

        if cand.raw_data or (prepared_path and prepared_path.is_file()):
            try:
                source = (
                    prepared_path if prepared_path and prepared_path.is_file() else io.BytesIO(cand.raw_data or b"")
                )
                with Image.open(source) as im:
                    oriented = im if prepared_path else ImageOps.exif_transpose(im)
                    if not prepared_path and cand.crop_box and cand.crop_confidence >= 0.8:
                        oriented = oriented.crop(cand.crop_box)
                    im_rgb = oriented.convert("RGB")
                    im_rgb.thumbnail((target_dim, target_dim), Image.Resampling.LANCZOS)
                    comp.paste(
                        im_rgb,
                        (
                            x_offset + (target_dim - im_rgb.width) // 2,
                            pad_top + banner_h + (target_dim - im_rgb.height) // 2,
                        ),
                    )
            except Exception:
                pass

    for index, (candidate, prepared_path, category_label) in enumerate(panels, start=1):
        draw_panel(
            candidate,
            pad_side + (index - 1) * (target_dim + gap),
            str(index),
            winner is candidate,
            (80, 120, 200),
            prepared_path,
            category_label,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    comp.save(output_path, format="JPEG", quality=95)
    return output_path


def emit_kitty_image(img_path: Path) -> bool:
    """Renders image in terminal using kitten icat with Kitty Graphics Protocol fallback."""
    if not img_path.is_file() or img_path.stat().st_size == 0:
        return False

    kitten_bin = shutil.which("kitten")
    if kitten_bin:
        try:
            fit_mode = os.environ.get("COVER_PREVIEW_FIT", "both")
            cols, lines, xpix, ypix = _get_terminal_geometry()
            base_cmd = [kitten_bin, "icat", "--align", "center", "--fit", fit_mode, "--scale-up"]
            # Dynamically reserve rows for prompt choices so the image does not scroll
            # the top banner out of the terminal viewport.
            if lines > 10:
                reserved_rows = int(os.environ.get("COVER_PREVIEW_RESERVED_ROWS", "5"))
                target_lines = max(6, lines - reserved_rows)
                target_xpix = xpix if xpix > 0 else cols * 10
                target_ypix = int(ypix * (target_lines / lines)) if ypix > 0 else target_lines * 20
                size_spec = f"{cols},{target_lines},{target_xpix},{target_ypix}"
                cmd = base_cmd + ["--use-window-size", size_spec, str(img_path)]
                res = subprocess.run(cmd, check=False, stderr=subprocess.DEVNULL, timeout=4)
                if res.returncode == 0:
                    return True

            res = subprocess.run(base_cmd + [str(img_path)], check=False, stderr=subprocess.DEVNULL, timeout=4)
            if res.returncode == 0:
                return True
        except Exception:
            pass

    try:
        with Image.open(img_path) as im:
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            png_bytes = buf.getvalue()

        raw_b64 = base64.b64encode(png_bytes).decode("ascii")
        chunks = [raw_b64[i : i + 4096] for i in range(0, len(raw_b64), 4096)]
        out_stream = sys.stdout
        tty_f = None
        try:
            tty_f = open("/dev/tty", "w")
            out_stream = tty_f
        except OSError:
            pass

        try:
            for i, chunk in enumerate(chunks):
                is_last = i == len(chunks) - 1
                m = 0 if is_last else 1
                if i == 0:
                    out_stream.write(f"\033_Ga=T,f=100,m={m};{chunk}\033\\")
                else:
                    out_stream.write(f"\033_Gm={m};{chunk}\033\\")
            out_stream.write("\n")
            out_stream.flush()
            return True
        finally:
            if tty_f:
                try:
                    tty_f.close()
                except OSError:
                    pass
    except Exception:
        return False


_last_rendered_comp_image: str | None = None


def display_in_kitty(
    best_digital: CoverCandidate | None,
    best_scan: CoverCandidate | None,
    winner: CoverCandidate | None,
    comp_image: Path | None,
) -> None:
    """Outputs artwork preview in Kitty terminal without extra text clutter."""
    global _last_rendered_comp_image
    if comp_image and comp_image.is_file():
        img_key = str(comp_image.resolve())
        if img_key != _last_rendered_comp_image:
            emit_kitty_image(comp_image)
            _last_rendered_comp_image = img_key


def _parse_cand_from_dict(
    d: dict[str, Any] | None,
    folder: Path | None,
    default_prov: str,
    local_name: str,
) -> CoverCandidate | None:
    if not d:
        return None
    res = d.get("resolution", [0, 0])
    w = res[0] if isinstance(res, (list, tuple)) and len(res) > 0 else 0
    h = res[1] if isinstance(res, (list, tuple)) and len(res) > 1 else 0
    local_path = (folder / local_name) if folder else None
    raw_data: bytes | None = None
    if local_path and local_path.is_file():
        try:
            raw_data = local_path.read_bytes()
        except OSError:
            pass
    return CoverCandidate(
        provider=d.get("provider", default_prov),
        url=d.get("url", ""),
        title=d.get("title", ""),
        album_match_ratio=1.0,
        width=w,
        height=h,
        sharpness=float(d.get("sharpness", 0.0)),
        contrast=float(d.get("contrast", 0.0)),
        entropy=float(d.get("entropy", 0.0)),
        colorfulness=float(d.get("colorfulness", 0.0)),
        dynamic_range=float(d.get("dynamic_range", 0.0)),
        blockiness=float(d.get("blockiness", 1.0)),
        total_score=float(d.get("score", 0.0)),
        visual_quality_score=float(d.get("visual_quality_score", 0.0)),
        effective_width=int((d.get("effective_resolution") or [0, 0])[0]),
        effective_height=int((d.get("effective_resolution") or [0, 0])[1]),
        provider_image_id=str(d.get("provider_image_id", "")),
        provider_release_id=str(d.get("provider_release_id", "")),
        identity_scope=str(d.get("identity_scope", "")),
        provenance=ArtworkProvenance(str(d.get("provenance", "unknown")))
        if str(d.get("provenance", "unknown")) in {item.value for item in ArtworkProvenance}
        else ArtworkProvenance.UNKNOWN,
        crop_box=tuple(d["crop_box"]) if isinstance(d.get("crop_box"), list) and len(d["crop_box"]) == 4 else None,
        crop_confidence=float(d.get("crop_confidence", 0.0)),
        source_form=str(d.get("source_form") or "source_image_unverified"),
        raw_data=raw_data,
        local_saved_path=local_path,
    )


def prompt_cover_choice(
    best_digital: CoverCandidate | None,
    best_scan: CoverCandidate | None,
    winner: CoverCandidate | None,
    output_path: Path | None = None,
    folder_path: Path | None = None,
    comp_image: Path | None = None,
) -> CoverCandidate | None:
    """Interactively choose among category representatives and the winner."""
    global _last_rendered_comp_image

    if not best_digital and not best_scan:
        return winner

    is_same = False
    if (
        best_digital
        and best_scan
        and (
            best_digital.url
            and best_digital.url == best_scan.url
            or best_digital.raw_data
            and best_scan.raw_data
            and best_digital.raw_data == best_scan.raw_data
        )
    ):
        is_same = True

    if comp_image and comp_image.is_file():
        img_key = str(comp_image.resolve())
        if img_key != _last_rendered_comp_image:
            emit_kitty_image(comp_image)
            _last_rendered_comp_image = img_key

    if is_same or (best_digital and not best_scan):
        automatic_choice = winner or best_digital
        if automatic_choice:
            if (
                automatic_choice.raw_data is None
                and automatic_choice.local_saved_path
                and automatic_choice.local_saved_path.is_file()
            ):
                try:
                    automatic_choice.raw_data = automatic_choice.local_saved_path.read_bytes()
                except OSError:
                    pass
            if automatic_choice.raw_data and output_path:
                save_optimized_cover(
                    automatic_choice.raw_data,
                    output_path,
                    crop_box=automatic_choice.crop_box if automatic_choice.crop_confidence >= 0.8 else None,
                )
                if folder_path and output_path != (folder_path / "cover.jpg"):
                    save_optimized_cover(
                        automatic_choice.raw_data,
                        folder_path / "cover.jpg",
                        crop_box=automatic_choice.crop_box if automatic_choice.crop_confidence >= 0.8 else None,
                    )
            print(
                f"  \033[1;32m✔\033[0m Обложка: {automatic_choice.provider} ({automatic_choice.width}×{automatic_choice.height}, quality: {automatic_choice.visual_quality_score:.1f})"
            )
        return automatic_choice

    if not best_digital and best_scan:
        automatic_choice = winner or best_scan
        if automatic_choice:
            if (
                automatic_choice.raw_data is None
                and automatic_choice.local_saved_path
                and automatic_choice.local_saved_path.is_file()
            ):
                try:
                    automatic_choice.raw_data = automatic_choice.local_saved_path.read_bytes()
                except OSError:
                    pass
            if automatic_choice.raw_data and output_path:
                save_optimized_cover(
                    automatic_choice.raw_data,
                    output_path,
                    crop_box=automatic_choice.crop_box if automatic_choice.crop_confidence >= 0.8 else None,
                )
                if folder_path and output_path != (folder_path / "cover.jpg"):
                    save_optimized_cover(
                        automatic_choice.raw_data,
                        folder_path / "cover.jpg",
                        crop_box=automatic_choice.crop_box if automatic_choice.crop_confidence >= 0.8 else None,
                    )
            print(
                f"  \033[1;32m✔\033[0m Обложка: {automatic_choice.provider} ({automatic_choice.width}×{automatic_choice.height}, quality: {automatic_choice.visual_quality_score:.1f})"
            )
        return automatic_choice

    visible_candidates = [candidate for candidate in (best_digital, best_scan) if candidate is not None]
    recommended = winner or max(visible_candidates, key=lambda candidate: candidate.total_score)

    def is_recommended(candidate: CoverCandidate | None) -> bool:
        if candidate is None:
            return False
        if recommended is candidate:
            return True
        return bool(recommended.url and candidate.url and recommended.url == candidate.url)

    w_is_dig = is_recommended(best_digital)
    w_is_scan = is_recommended(best_scan)
    star_dig = " \033[1;33m★\033[0m" if w_is_dig else ""
    star_scan = " \033[1;33m★\033[0m" if w_is_scan else ""

    winner_visible = is_recommended(best_digital) or is_recommended(best_scan)
    if winner and not winner_visible:
        print(
            f"  \033[1;35m[A]\033[0m Автовыбор: {artwork_label(winner)} ({winner.provider}) "
            f"({winner.width}×{winner.height}, quality: {winner.visual_quality_score:.1f}) \033[1;33m★\033[0m"
        )

    if best_digital:
        print(
            f"  \033[1;34m[1]\033[0m Цифра: {artwork_label(best_digital)} ({best_digital.provider}, {best_digital.width}×{best_digital.height}, quality: {best_digital.visual_quality_score:.1f}){star_dig}"
        )
    if best_scan:
        print(
            f"  \033[1;31m[2]\033[0m {artwork_label(best_scan)}: {best_scan.provider} ({best_scan.width}×{best_scan.height}, quality: {best_scan.visual_quality_score:.1f}){star_scan}"
        )

    chosen: CoverCandidate | None = None
    choice = ""
    try:
        if sys.stdin.isatty():
            choice = input("  \033[1;36mВыбор [A/1/2/Enter]: \033[0m").strip().lower()
        else:
            line = sys.stdin.readline()
            choice = line.strip().lower() if line else ""
    except (EOFError, KeyboardInterrupt):
        choice = ""

    if choice in ("a", "auto", "automatic"):
        chosen = recommended
    elif choice in ("1", "d", "digital"):
        chosen = best_digital or winner
    elif choice in ("2", "s", "scan"):
        chosen = best_scan or winner
    elif choice == "":
        chosen = recommended
    else:
        custom_path = Path(choice).expanduser().resolve()
        if custom_path.is_file() and custom_path.stat().st_size > 1000:
            try:
                cdata = custom_path.read_bytes()
                with Image.open(io.BytesIO(cdata)) as cimg:
                    cw, ch = cimg.size
                chosen = CoverCandidate(
                    provider=f"Custom ({custom_path.name})",
                    url=str(custom_path),
                    title="Custom User Cover",
                    album_match_ratio=1.0,
                    width=cw,
                    height=ch,
                    total_score=9999.0,
                    raw_data=cdata,
                )
            except Exception:
                chosen = winner or best_digital
        else:
            chosen = winner or best_digital

    if chosen:
        if chosen.raw_data is None and chosen.local_saved_path and chosen.local_saved_path.is_file():
            try:
                chosen.raw_data = chosen.local_saved_path.read_bytes()
            except OSError:
                pass

        if chosen.raw_data and output_path:
            save_optimized_cover(
                chosen.raw_data, output_path, crop_box=chosen.crop_box if chosen.crop_confidence >= 0.8 else None
            )
            if folder_path and output_path != (folder_path / "cover.jpg"):
                save_optimized_cover(
                    chosen.raw_data,
                    folder_path / "cover.jpg",
                    crop_box=chosen.crop_box if chosen.crop_confidence >= 0.8 else None,
                )

        print(
            f"  \033[1;32m✔\033[0m Выбрана обложка: {chosen.provider} ({chosen.width}×{chosen.height}, quality: {chosen.visual_quality_score:.1f})\n"
        )

    return chosen


def prompt_cover_options(choices: list[CoverChoice], *, album: str = "") -> CoverChoice | None:
    """Require an explicit numbered choice or rejection from the prepared category menu."""
    if not choices:
        print("  -> No eligible front-cover artwork was found.")
        return None
    if any(not choice.prepared_path or not choice.prepared_path.is_file() for choice in choices):
        raise ValueError("all displayed artwork options must be prepared before selection")
    if album:
        print(f"\n{album}")
    labels = {
        "digital": "DIGITAL ARTWORK",
        "physical": "PHYSICAL SCAN",
        "local": "LOCAL ARTWORK",
        "unknown": "ARTWORK — SOURCE UNKNOWN",
    }
    for index, choice in enumerate(choices, start=1):
        candidate = choice.candidate
        dimension = f"{choice.prepared_width} × {choice.prepared_height}"
        print(f"  {index}. {labels[choice.category]} — {candidate.provider}")
        print(f"     Front cover: {dimension} px | Quality: {candidate.visual_quality_score:.1f}")
        if choice.applied_crop:
            print(f"     Crop: applied from {candidate.width} × {candidate.height} px source")
        else:
            print("     Crop: not applied")
            if candidate.crop_box or candidate.has_scanner_borders or abs(candidate.aspect_ratio - 1) > 0.05:
                print("     Warning: uncropped borders or geometry warrant visual inspection")
        for warning in candidate.warnings[:2]:
            print(f"     Warning: {warning}")
        if choice.prepared_path:
            print(f"     Inspect: {choice.prepared_path}")
    print("  R. Reject artwork for this album")
    while True:
        try:
            response = input(f"  Select artwork [1-{len(choices)}/R]: ").strip().casefold()
        except (EOFError, KeyboardInterrupt):
            return None
        if response in {"r", "reject", "0"}:
            return None
        if response.isdigit() and 1 <= int(response) <= len(choices):
            choice = choices[int(response) - 1]
            candidate = choice.candidate
            print(
                f"  -> Selected {labels[choice.category]} from {candidate.provider}: "
                f"{choice.prepared_width} × {choice.prepared_height} px, "
                f"crop {'applied' if choice.applied_crop else 'not applied'}."
            )
            return choice
        print("  Enter an option number or R to reject; Enter alone does not select artwork.")


def prompt_all_cover_choice(
    candidates: list[CoverCandidate],
    winner: CoverCandidate | None,
) -> CoverCandidate | None:
    """Let a person inspect and choose any validated front cover."""
    choices = sorted(
        (c for c in candidates if is_valid_manual_cover(c)),
        key=lambda c: (
            -int(c.release_tier),
            -int(c.role_tier),
            -c.visual_quality_score,
            c.provider,
            c.stable_id,
            c.original_url,
        ),
    )
    if not choices:
        print("No valid front covers were found for manual selection.")
        return winner if winner and is_valid_manual_cover(winner) else None
    print("\nAvailable album covers (v NUMBER previews an image, Enter accepts the automatic choice):")
    for number, cand in enumerate(choices, 1):
        mark = " *automatic*" if cand is winner else ""
        print(
            f"{number:3}. {cand.provider} [{cand.stable_id}] {cand.width}x{cand.height} "
            f"{cand.release_tier.name}/{cand.role_tier.name} quality={cand.visual_quality_score:.1f} "
            f"eligible{mark}"
        )
    while True:
        try:
            response = input("Cover number, v NUMBER to preview, or Enter for automatic: ").strip().lower()
        except EOFError:
            raise RuntimeError("--manual-covers requires an interactive cover choice") from None
        if not response:
            return winner
        preview = response.startswith("v ")
        token = response[2:].strip() if preview else response
        if not token.isdigit() or not 1 <= int(token) <= len(choices):
            print("Enter a listed number.")
            continue
        chosen = choices[int(token) - 1]
        if preview:
            fd, temp_name = tempfile.mkstemp(prefix="cover-preview-", suffix=".jpg")
            os.close(fd)
            temp_path = Path(temp_name)
            try:
                save_optimized_cover(
                    chosen.raw_data or b"",
                    temp_path,
                    crop_box=chosen.crop_box if chosen.crop_confidence >= 0.8 else None,
                )
                emit_kitty_image(temp_path)
                print(" / ".join(chosen.selection_explanation))
            finally:
                temp_path.unlink(missing_ok=True)
            continue
        print(f"Selected {chosen.provider} [{chosen.stable_id}]")
        return chosen


def load_env_file(env_path: Path | None = None) -> None:
    """Load key=value pairs from .env file into os.environ if not already present."""
    candidates = [env_path] if env_path else [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]
    for c in candidates:
        if c and c.is_file():
            try:
                for line in c.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
                break
            except Exception:
                pass


def main() -> int:
    load_env_file()
    parser = argparse.ArgumentParser(description="Audiophile multi-provider cover art fetcher")
    parser.add_argument("--artist", help="Artist name")
    parser.add_argument("--album", help="Album title")
    parser.add_argument("--folder", help="Target album directory path")
    parser.add_argument("--output", help="Destination file path for winner cover")
    parser.add_argument("--mb-release-group-id", default="", help="MusicBrainz Release Group ID")
    parser.add_argument("--mb-release-id", default="", help="MusicBrainz Release ID")
    parser.add_argument("--discogs-id", default="", help="Discogs Release ID")
    parser.add_argument("--discogs-token", default="", help="Discogs API token")
    parser.add_argument("--discogs-json", default="", help="Discogs release JSON string")
    parser.add_argument("--json", action="store_true", help="Output full JSON evaluation report")
    parser.add_argument("--display-kitty", action="store_true", help="Render covers in Kitty terminal")
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress terminal preview")
    parser.add_argument("--render-audition", help="Path to comparison image to render in Kitty")
    parser.add_argument("--interactive-select", action="store_true", help="Interactively audition and choose cover")
    parser.add_argument("--manual-covers", action="store_true", help="Choose from all valid discovered front covers")
    parser.add_argument("--auto-covers", action="store_true", help="Use automatic ranking without a selection prompt")
    parser.add_argument(
        "--no-local-covers", action="store_true", help="Ignore local cover files, scans and embedded fronts"
    )
    parser.add_argument(
        "--preview-size",
        type=int,
        default=int(os.environ.get("COVER_PREVIEW_SIZE", "1200")),
        help="Target pixel dimension for each cover in terminal preview (default: 1200)",
    )
    parser.add_argument("--report-json", help="JSON report content or file path for audition rendering")
    parser.add_argument(
        "--cover-mode",
        default=os.environ.get("COVER_MODE", "auto"),
        choices=["auto", "physical", "digital"],
        help="Cover selection priority policy (auto, physical, digital)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Enable verbose diagnostics (-v for detailed logs, -vv to render all found cover candidates in terminal)",
    )
    args = parser.parse_args()
    if args.manual_covers and args.json:
        parser.error("--manual-covers cannot be combined with --json")
    if args.manual_covers and args.auto_covers:
        parser.error("--manual-covers and --auto-covers cannot be combined")

    if args.interactive_select or args.render_audition:
        folder_path = Path(args.folder).resolve() if args.folder else None
        out_file = Path(args.output).resolve() if args.output else (folder_path / "cover.jpg" if folder_path else None)
        comp_path: Path | None = None
        if args.render_audition:
            comp_path = Path(args.render_audition).resolve()
        elif folder_path:
            cand_comp = folder_path / ".cover_comparison.jpg"
            if cand_comp.is_file():
                comp_path = cand_comp

        d_cand: CoverCandidate | None = None
        s_cand: CoverCandidate | None = None
        w_cand: CoverCandidate | None = None

        if args.report_json:
            try:
                rep_text = args.report_json
                if Path(rep_text).is_file():
                    rep_text = Path(rep_text).read_text(encoding="utf-8")
                rep_data = json.loads(rep_text)
                d_cand = _parse_cand_from_dict(
                    rep_data.get("best_digital"), folder_path, "Digital", ".cover_digital.jpg"
                )
                s_cand = _parse_cand_from_dict(rep_data.get("best_scan"), folder_path, "Scan", ".cover_scan.jpg")
                w_cand = _parse_cand_from_dict(rep_data.get("winner"), folder_path, "Winner", "cover.jpg")
                if not comp_path and rep_data.get("comparison_image"):
                    cand_c = Path(rep_data["comparison_image"]).resolve()
                    if cand_c.is_file():
                        comp_path = cand_c
            except Exception:
                pass

        if not args.quiet:
            display_in_kitty(d_cand, s_cand, w_cand, comp_path)

        if args.interactive_select:
            chosen = prompt_cover_choice(d_cand, s_cand, w_cand, out_file, folder_path)
            return 0 if chosen else 1

        return 0

    if not args.artist or not args.album:
        parser.error("--artist and --album are required when not using --render-audition or --interactive-select")

    interactive_default = not (args.auto_covers or args.quiet or args.json or args.manual_covers)
    if (interactive_default or args.manual_covers) and not sys.stdin.isatty():
        print("Interactive cover selection requires a terminal; use --auto-covers or --quiet.", file=sys.stderr)
        return 2

    folder_path = Path(args.folder).resolve() if args.folder else None
    fetcher = CoverFetcher(
        artist=args.artist,
        album=args.album,
        folder=folder_path,
        mb_rg_id=args.mb_release_group_id,
        mb_rel_id=args.mb_release_id,
        discogs_id=args.discogs_id,
        discogs_token=args.discogs_token,
        discogs_json_str=args.discogs_json,
        cover_mode=args.cover_mode,
        verbose=0 if args.json else args.verbose,
        manual_all=args.manual_covers,
        exhaustive_categories=interactive_default,
        no_local_covers=args.no_local_covers,
    )

    effective_verbose = 0 if args.json else args.verbose
    winner, best_digital, best_scan, all_cands = fetcher.find_best_cover(verbose=effective_verbose)
    best_local = next(
        (choice.candidate for choice in select_cover_categories(fetcher, all_cands) if choice.category == "local"),
        None,
    )
    if args.manual_covers:
        winner = prompt_all_cover_choice(all_cands, winner)

    out_file = Path(args.output).resolve() if args.output else (folder_path / "cover.jpg" if folder_path else None)

    saved_w, saved_h, saved_sz = 0, 0, 0
    generated_comp_path: Path | None = None
    if interactive_default:
        choices = select_cover_categories(fetcher, all_cands)
        if choices:
            preview_parent = folder_path if folder_path and folder_path.is_dir() else None
            with tempfile.TemporaryDirectory(prefix="cover-options-", dir=preview_parent) as option_directory:
                prepare_cover_choices(choices, Path(option_directory), fetcher=fetcher, candidates=all_cands)
                comparison = Path(option_directory) / "comparison.jpg"
                create_comparison_image(None, None, winner, comparison, target_dim=args.preview_size, options=choices)
                if not emit_kitty_image(comparison):
                    print("Terminal image display unavailable; inspect the prepared image paths below.")
                chosen_option = prompt_cover_options(choices, album=f"{args.artist} — {args.album}")
                winner = chosen_option.candidate if chosen_option else None
                if chosen_option and chosen_option.prepared_path and out_file:
                    out_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(chosen_option.prepared_path, out_file)
                    saved_w, saved_h = chosen_option.prepared_width, chosen_option.prepared_height
                    saved_sz = out_file.stat().st_size
                    if folder_path and out_file != folder_path / "cover.jpg":
                        shutil.copy2(chosen_option.prepared_path, folder_path / "cover.jpg")
        else:
            print("No eligible front-cover artwork was found.")
            winner = None
    elif winner and winner.raw_data and out_file:
        prepared = prepare_best_cover(
            fetcher, all_cands, out_file, preferred=winner, allow_fallback=not args.manual_covers
        )
        winner = prepared.candidate if prepared else None
        if prepared:
            saved_w, saved_h, saved_sz = prepared.prepared_width, prepared.prepared_height, out_file.stat().st_size
        if folder_path and out_file != (folder_path / "cover.jpg"):
            if prepared:
                shutil.copy2(out_file, folder_path / "cover.jpg")

    if folder_path and not interactive_default:
        comp_file = folder_path / ".cover_comparison.jpg"
        generated_comp_path = create_comparison_image(
            best_digital, best_scan, winner, comp_file, target_dim=args.preview_size
        )

    if (args.display_kitty or not args.quiet) and not args.json and not interactive_default:
        display_in_kitty(best_digital, best_scan, winner, generated_comp_path)
        if sys.stdin.isatty() and not (args.manual_covers or args.auto_covers):
            chosen = prompt_cover_choice(best_digital, best_scan, winner, out_file, folder_path)
            if chosen:
                winner = chosen

    report: dict[str, Any] = {
        "ok": winner is not None,
        "artist": args.artist,
        "album": args.album,
        "winner": {
            "provider": winner.provider,
            "title": winner.title,
            "resolution": [saved_w or (winner.width if winner else 0), saved_h or (winner.height if winner else 0)],
            "aspect_ratio": round(winner.aspect_ratio, 4) if winner else 0,
            "sharpness": round(winner.sharpness, 1) if winner else 0,
            "contrast": round(winner.contrast, 1) if winner else 0,
            "entropy": round(winner.entropy, 2) if winner else 0,
            "colorfulness": round(winner.colorfulness, 1) if winner else 0,
            "dynamic_range": round(winner.dynamic_range, 1) if winner else 0,
            "blockiness": round(winner.blockiness, 3) if winner else 1.0,
            "score": round(winner.total_score, 1) if winner else 0,
            "visual_quality_score": round(winner.visual_quality_score, 3) if winner else 0,
            "effective_resolution": [winner.effective_width, winner.effective_height] if winner else [0, 0],
            "release_tier": winner.release_tier.name if winner else "",
            "release_confidence": winner.release_confidence if winner else 0,
            "release_evidence": list(winner.release_evidence) if winner else [],
            "role_tier": winner.role_tier.name if winner else "",
            "role_confidence": winner.role_confidence if winner else 0,
            "role_evidence": list(winner.role_evidence) if winner else [],
            "provider_image_id": winner.provider_image_id if winner else "",
            "provider_release_id": winner.provider_release_id if winner else "",
            "representation": winner.representation_kind.value if winner else "",
            "provenance": winner.provenance.value if winner else "",
            "identity_scope": winner.identity_scope if winner else "",
            "category": cover_category(winner) if winner else "",
            "crop_box": list(winner.crop_box) if winner and winner.crop_box else None,
            "crop_confidence": winner.crop_confidence if winner else 0.0,
            "source_form": winner.source_form if winner else "",
            "mime_type": winner.mime_type if winner else "",
            "score_breakdown": winner.score_breakdown if winner else {},
            "explanation": winner.selection_explanation if winner else [],
            "url": winner.url if winner else "",
            "file_size": saved_sz or (winner.file_size if winner else 0),
        }
        if winner
        else None,
        "best_digital": {
            "provider": best_digital.provider,
            "title": best_digital.title,
            "resolution": [best_digital.width, best_digital.height],
            "score": round(best_digital.total_score, 1),
            "sharpness": round(best_digital.sharpness, 1),
            "contrast": round(best_digital.contrast, 1),
            "entropy": round(best_digital.entropy, 2),
            "colorfulness": round(best_digital.colorfulness, 1),
            "dynamic_range": round(best_digital.dynamic_range, 1),
            "blockiness": round(best_digital.blockiness, 3),
            "url": best_digital.url,
            "provenance": best_digital.provenance.value,
            "identity_scope": best_digital.identity_scope,
        }
        if best_digital
        else None,
        "best_scan": {
            "provider": best_scan.provider,
            "title": best_scan.title,
            "resolution": [best_scan.width, best_scan.height],
            "score": round(best_scan.total_score, 1),
            "sharpness": round(best_scan.sharpness, 1),
            "contrast": round(best_scan.contrast, 1),
            "entropy": round(best_scan.entropy, 2),
            "colorfulness": round(best_scan.colorfulness, 1),
            "dynamic_range": round(best_scan.dynamic_range, 1),
            "blockiness": round(best_scan.blockiness, 3),
            "url": best_scan.url,
            "provenance": best_scan.provenance.value,
            "identity_scope": best_scan.identity_scope,
        }
        if best_scan
        else None,
        "best_local": {
            "provider": best_local.provider,
            "title": best_local.title,
            "resolution": [best_local.width, best_local.height],
            "effective_resolution": [best_local.effective_width, best_local.effective_height],
            "score": round(best_local.total_score, 1),
            "visual_quality_score": round(best_local.visual_quality_score, 3),
            "provenance": best_local.provenance.value,
            "identity_scope": best_local.identity_scope,
            "crop_box": list(best_local.crop_box) if best_local.crop_box else None,
            "crop_confidence": best_local.crop_confidence,
            "url": best_local.url,
        }
        if best_local
        else None,
        "comparison_image": str(generated_comp_path) if generated_comp_path else None,
        "total_evaluated": len(all_cands),
        "candidates": [
            {
                "provider": c.provider,
                "title": c.title,
                "resolution": [c.width, c.height],
                "score": round(c.total_score, 1),
                "visual_quality_score": round(c.visual_quality_score, 3),
                "effective_resolution": [c.effective_width, c.effective_height],
                "release_tier": c.release_tier.name,
                "release_confidence": c.release_confidence,
                "release_evidence": list(c.release_evidence),
                "release_conflicts": list(c.release_conflicts),
                "role_tier": c.role_tier.name,
                "role_confidence": c.role_confidence,
                "role_evidence": list(c.role_evidence),
                "provider_image_id": c.provider_image_id,
                "provider_release_id": c.provider_release_id,
                "representation": c.representation_kind.value,
                "provenance": c.provenance.value,
                "identity_scope": c.identity_scope,
                "category": cover_category(c),
                "mime_type": c.mime_type,
                "detail_score": round(c.detail_score, 3),
                "compression_score": round(c.compression_score, 3),
                "upscale_confidence": round(c.upscale_confidence, 4),
                "watermark_confidence": round(c.watermark_confidence, 4),
                "skew_degrees": round(c.skew_degrees, 3),
                "skew_confidence": round(c.skew_confidence, 4),
                "moire_confidence": round(c.moire_confidence, 4),
                "streak_confidence": round(c.streak_confidence, 4),
                "metric_confidence": c.metric_confidence,
                "crop_box": list(c.crop_box) if c.crop_box else None,
                "crop_confidence": c.crop_confidence,
                "source_form": c.source_form,
                "score_breakdown": c.score_breakdown,
                "duplicate_cluster_id": c.duplicate_cluster_id,
                "duplicate_of": c.duplicate_of,
                "rejection_reasons": c.rejection_reasons,
                "warnings": c.warnings,
                "explanation": c.selection_explanation,
                "sharpness": round(c.sharpness, 1),
                "contrast": round(c.contrast, 1),
                "colorfulness": round(c.colorfulness, 1),
                "dynamic_range": round(c.dynamic_range, 1),
                "blockiness": round(c.blockiness, 3),
                "aspect_ratio": round(c.aspect_ratio, 3),
                "error": c.error,
            }
            for c in all_cands
        ],
    }

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        if winner:
            print(
                f"WINNER: {winner.provider} | {saved_w}x{saved_h} | Score: {round(winner.total_score, 1)} | Sharp: {round(winner.sharpness, 1)} | Color: {round(winner.colorfulness, 1)} | Title: {winner.title}"
            )
        else:
            print("NO_COVER_FOUND", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
