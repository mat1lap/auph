#!/usr/bin/env python3
"""Typed policy configuration with preservation-safe defaults."""

from __future__ import annotations

import math
import os
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

import tomllib


@dataclass(frozen=True)
class MatchingPolicy:
    release_min_score: float = 500.0
    release_min_margin: float = 120.0
    discogs_min_margin: float = 150.0
    acoustid_min_score: float = 0.80


@dataclass(frozen=True)
class LyricsPolicy:
    online_lookup: bool = True
    generated_candidates: bool = False
    minimum_candidate_score: float = 75.0
    # Deprecated compatibility setting. Playback LYRICS always omits structural
    # labels per the repository's selected storage policy; sidecars retain them.
    remove_section_labels: bool = True
    embed_synced_in_lyrics: bool = True
    write_synced_compat_alias: bool = True
    retain_raw_provider_evidence: bool = True


@dataclass(frozen=True)
class LoudnessPolicy:
    enabled: bool = True
    prevent_clipping: bool = True
    write_r128_flac_tags: bool = False
    retain_enhanced_tags: bool = True
    preserve_existing_measurements: bool = True


@dataclass(frozen=True)
class PreservationPolicy:
    preserve_unknown_comments: bool = True
    preserve_existing_lyrics: bool = True
    preserve_existing_artwork_file: bool = True
    provenance_ledger: bool = True
    existing_metadata_confidence: str = "UNKNOWN"
    mirror_total_aliases: bool = True


@dataclass(frozen=True)
class SearchPolicy:
    jackett_url: str = ""
    jackett_indexer: str = "rutracker"
    jackett_key_env: str = "JACKETT_API_KEY"
    topic_base_url: str = "https://rutracker.org/forum"
    topic_cookie_env: str = "RUTRACKER_COOKIE"
    flaresolverr_url: str = ""
    flaresolverr_timeout: float = 30.0
    details: bool = True
    detail_mode: str = "targeted"
    detail_budget: int = 5
    detail_time_budget: float = 180.0
    preliminary_llm: bool = False
    detail_cache_directory: str = ""
    detail_cache_ttl: float = 3600.0
    result_limit: int = 50
    page_size: int = 25
    max_pages: int = 4
    timeout: float = 15.0
    prefer_lossless: bool = True
    prefer_documented_rip: bool = True
    allow_partial: bool = False
    backend: str = "none"
    remote_metadata_disclosure: bool = False
    openai_endpoint: str = "https://api.openai.com/v1/chat/completions"
    openai_model: str = ""
    openai_key_env: str = "OPENAI_API_KEY"
    gemini_endpoint: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_model: str = ""
    gemini_max_output_tokens: int = 2048
    gemini_thinking_level: str = ""
    gemini_key_env: str = "GEMINI_API_KEY"
    local_endpoint: str = ""
    local_model: str = ""
    local_key_env: str = "LOCAL_LLM_API_KEY"
    research_backend: str = "none"
    research_endpoint: str = "https://api.search.brave.com/res/v1/web/search"
    research_serper_endpoint: str = "https://google.serper.dev/search"
    research_key_env: str = "BRAVE_SEARCH_API_KEY"
    research_disclosure: bool = False
    research_domains: str = "forums.stevehoffman.tv"
    research_queries: int = 3
    research_page_transport: str = "direct"
    research_flaresolverr_url: str = ""
    research_pages: int = 5
    research_cache_directory: str = ""
    research_cache_ttl: float = 86400.0
    structured_mode: str = "json"
    mastering_pipeline: bool = True
    research_adaptive: bool = True
    research_posts_per_page: int = 80
    research_follow_pages: bool = True
    mastering_max_sources: int = 40
    mastering_batch_size: int = 8
    mastering_literal_recovery: bool = True
    mastering_output_tokens: int = 8192
    openai_api: str = "chat"  # chat | responses
    openai_responses_endpoint: str = "https://api.openai.com/v1/responses"
    openai_reasoning_effort: str = "high"
    research_openai_discovery: bool = False
    research_openai_model: str = ""
    research_openai_tool_calls: int = 4
    qbittorrent_url: str = ""
    qbittorrent_user_env: str = "QBITTORRENT_USERNAME"
    qbittorrent_password_env: str = "QBITTORRENT_PASSWORD"
    save_path: str = ""
    state_directory: str = ""

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if name.endswith("_env") and (
                not isinstance(value, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", value)
                or value.startswith(("AIza", "sk-", "sk_"))
            ):
                # Never echo this value: it may be an accidentally pasted credential.
                raise ValueError(f"search.{name} must contain an environment variable name, not a credential")


@dataclass(frozen=True)
class PipelineConfig:
    matching: MatchingPolicy = MatchingPolicy()
    lyrics: LyricsPolicy = LyricsPolicy()
    loudness: LoudnessPolicy = LoudnessPolicy()
    preservation: PreservationPolicy = PreservationPolicy()
    search: SearchPolicy = SearchPolicy()


class _ConfigLoader:
    def __call__(self, path: str | None = None) -> PipelineConfig:
        default_path = str(Path(__file__).with_name("metadata_policy.toml"))
        selected = Path(path or os.environ.get("AUDIOPHILE_CONFIG") or default_path)
        if (path or os.environ.get("AUDIOPHILE_CONFIG")) and not selected.is_file():
            raise FileNotFoundError(f"configured policy file does not exist: {selected}")
        stat = selected.stat() if selected.is_file() else None
        return _load_selected_config(
            str(selected.resolve()), stat.st_mtime_ns if stat else 0, stat.st_size if stat else 0
        )

    def cache_clear(self) -> None:
        _load_selected_config.cache_clear()


@lru_cache(maxsize=4)
def _load_selected_config(path: str, _mtime_ns: int, _size: int) -> PipelineConfig:
    selected = Path(path)
    if not selected.is_file():
        return PipelineConfig()
    with selected.open("rb") as handle:
        data = tomllib.load(handle)
    defaults = asdict(PipelineConfig())
    for section, values in data.items():
        if section not in defaults or not isinstance(values, dict):
            raise ValueError(f"unknown or invalid policy section: {section}")
        for name, value in values.items():
            default = defaults[section].get(name)
            key = f"{section}.{name}"
            if default is None:
                raise ValueError(f"unknown policy setting: {key}")
            if isinstance(default, bool):
                if not isinstance(value, bool):
                    raise TypeError(f"{key} must be a boolean")
                if section == "preservation" and name.startswith("preserve_") and not value:
                    raise ValueError(f"{key}=false is unsupported; existing content is preserved")
            elif isinstance(default, float):
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 0
                ):
                    raise ValueError(f"{key} must be a finite nonnegative number")
                maximum = 1 if name == "acoustid_min_score" else 100 if name == "minimum_candidate_score" else None
                if maximum is not None and value > maximum:
                    raise ValueError(f"{key} must be at most {maximum}")
            elif name == "existing_metadata_confidence":
                if not isinstance(value, str) or value.upper() not in {"UNKNOWN", "LOW", "MEDIUM", "HIGH", "VERIFIED"}:
                    raise ValueError(f"{key} must name a supported confidence level")
                values[name] = value.upper()
            elif section == "search":
                if isinstance(default, int):
                    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                        raise ValueError(f"{key} must be a positive integer")
                elif not isinstance(value, str):
                    raise TypeError(f"{key} must be text")
    return PipelineConfig(
        matching=MatchingPolicy(**data.get("matching", {})),
        lyrics=LyricsPolicy(**data.get("lyrics", {})),
        loudness=LoudnessPolicy(**data.get("loudness", {})),
        preservation=PreservationPolicy(**data.get("preservation", {})),
        search=SearchPolicy(**data.get("search", {})),
    )


# Retain the public invalidation hook used by callers and fixture tests.
load_config = _ConfigLoader()
