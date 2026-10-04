#!/usr/bin/env python3
"""Typed policy configuration with preservation-safe defaults."""

from __future__ import annotations

import math
import os
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
class PipelineConfig:
    matching: MatchingPolicy = MatchingPolicy()
    lyrics: LyricsPolicy = LyricsPolicy()
    loudness: LoudnessPolicy = LoudnessPolicy()
    preservation: PreservationPolicy = PreservationPolicy()


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
    return PipelineConfig(
        matching=MatchingPolicy(**data.get("matching", {})),
        lyrics=LyricsPolicy(**data.get("lyrics", {})),
        loudness=LoudnessPolicy(**data.get("loudness", {})),
        preservation=PreservationPolicy(**data.get("preservation", {})),
    )


# Retain the public invalidation hook used by callers and fixture tests.
load_config = _ConfigLoader()
