#!/usr/bin/env python3
"""Typed policy configuration with preservation-safe defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
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
class PipelineConfig:
    matching: MatchingPolicy = MatchingPolicy()
    lyrics: LyricsPolicy = LyricsPolicy()
    loudness: LoudnessPolicy = LoudnessPolicy()
    preservation: PreservationPolicy = PreservationPolicy()


class _ConfigLoader:
    def __call__(self, path: str | None = None) -> PipelineConfig:
        default_path = str(Path(__file__).with_name("metadata_policy.toml"))
        selected = Path(path or os.environ.get("AUDIOPHILE_CONFIG") or default_path)
        return _load_selected_config(str(selected.resolve()))

    def cache_clear(self) -> None:
        _load_selected_config.cache_clear()


@lru_cache(maxsize=4)
def _load_selected_config(path: str) -> PipelineConfig:
    selected = Path(path)
    if not selected.is_file():
        return PipelineConfig()
    with selected.open("rb") as handle:
        data = tomllib.load(handle)
    return PipelineConfig(
        matching=MatchingPolicy(**data.get("matching", {})),
        lyrics=LyricsPolicy(**data.get("lyrics", {})),
        loudness=LoudnessPolicy(**data.get("loudness", {})),
        preservation=PreservationPolicy(**data.get("preservation", {})),
    )


# Retain the public invalidation hook used by callers and fixture tests.
load_config = _ConfigLoader()
