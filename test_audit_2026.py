"""Offline failure injection for preservation and evidence contracts."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import requests
from mutagen.flac import FLAC

import album_completeness
import audiophile_cover_fetcher as covers
import audiophile_tagger as tagger
import discogs_release_find
import library_files
import library_layout
import loudness
import mb_track_match
import metadata_writer
import pipeline_config
import provider_cache
import release_match
from lyrics_quality import validate_enhanced_lrc
from test_pipeline_safety import core_tags, make_flac


def test_writer_retains_recovery_copy_when_rollback_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = make_flac(tmp_path / "track.flac")
    before = source.read_bytes()
    real_replace = os.replace

    def replace(src: str | Path, dst: str | Path) -> None:
        if str(src).endswith(".rollback.flac"):
            raise OSError("rollback unavailable")
        real_replace(src, dst)

    def ledger_failure(*_args: object) -> None:
        raise OSError("ledger unavailable")

    monkeypatch.setattr(metadata_writer.os, "replace", replace)
    monkeypatch.setattr(metadata_writer, "_append_provenance_record", ledger_failure)
    with pytest.raises(RuntimeError, match="recovery"):
        metadata_writer.write_tags(source, core_tags())
    backups = list(tmp_path.glob(".*.rollback.flac"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == before


def test_failed_writer_does_not_publish_lyric_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = make_flac(tmp_path / "track.flac")
    audio = FLAC(source)
    audio["LYRICS"] = ["[Verse 1]\nA curated first line\nAnother curated line"]
    audio.save()
    before = source.read_bytes()

    def invalid(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("verification rejected")

    monkeypatch.setattr(metadata_writer, "canonical_verify", invalid)
    with pytest.raises(RuntimeError, match="verification rejected"):
        metadata_writer.write_tags(source, core_tags())
    assert source.read_bytes() == before
    assert set(tmp_path.iterdir()) == {source}


def _scanner(monkeypatch: pytest.MonkeyPatch, *, replacement: Path | None = None) -> None:
    real_run = subprocess.run
    monkeypatch.setattr(tagger.shutil, "which", lambda _name: "/fixture/loudgain")

    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if cmd[0] != "/fixture/loudgain":
            return real_run(cmd, **kwargs)
        for name in cmd:
            if name.endswith(".replaygain.flac"):
                if replacement:
                    shutil.copy2(replacement, name)
                audio = FLAC(name)
                for field, value in {
                    "REPLAYGAIN_TRACK_GAIN": "-2.0 dB",
                    "REPLAYGAIN_TRACK_PEAK": "0.5",
                    "REPLAYGAIN_ALBUM_GAIN": "-2.0 dB",
                    "REPLAYGAIN_ALBUM_PEAK": "0.5",
                }.items():
                    audio[field] = [value]
                audio.save()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(tagger.subprocess, "run", run)


def test_loudgain_retains_backups_when_rollback_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tracks = [make_flac(tmp_path / f"{number}.flac") for number in (1, 2)]
    before = [path.read_bytes() for path in tracks]
    _scanner(monkeypatch)
    real_replace = os.replace

    def replace(src: str | Path, dst: str | Path) -> None:
        if str(src).endswith(".backup.flac") or (str(src).endswith(".replaygain.flac") and Path(dst) == tracks[1]):
            raise OSError("injected replacement failure")
        real_replace(src, dst)

    monkeypatch.setattr(tagger.os, "replace", replace)
    outcome: dict[str, str] = {}
    assert not tagger.run_loudgain(tracks, quiet=True, outcome=outcome)
    assert outcome["status"] == "failed"
    backups = sorted(tmp_path.glob(".*.backup.flac"))
    assert len(backups) == 2
    assert [path.read_bytes() for path in backups] == before


def test_loudgain_rejects_audio_changes_even_with_valid_gain_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_flac(tmp_path / "source.flac")
    other = make_flac(tmp_path / "other.flac", seconds=0.4)
    before = source.read_bytes()
    _scanner(monkeypatch, replacement=other)
    outcome: dict[str, str] = {}
    assert not tagger.run_loudgain([source], quiet=True, outcome=outcome)
    assert outcome["status"] == "failed"
    assert source.read_bytes() == before


def _identity(path: Path, **fields: str) -> None:
    audio = FLAC(path)
    for field, value in {"ALBUM": "Album", "ALBUMARTIST": "Artist", **fields}.items():
        audio[field] = [value]
    audio.save()


def test_completeness_uses_known_filename_positions(tmp_path: Path) -> None:
    tracks = [make_flac(tmp_path / f"{number:02} - Title.flac") for number in (1, 2)]
    for path in tracks:
        _identity(path, TOTALTRACKS="2")
    assert library_layout.disc_counts(tracks, tmp_path) == {1: 2}
    assert album_completeness.assess_album_completeness(tracks, tmp_path).state == "COMPLETE"


def test_completeness_rejects_disc_directory_tag_conflict(tmp_path: Path) -> None:
    folder = tmp_path / "CD2"
    folder.mkdir()
    track = make_flac(folder / "01 - Title.flac")
    _identity(track, DISCNUMBER="1", TRACKNUMBER="1", TOTALTRACKS="1")
    assert album_completeness.assess_album_completeness([track], tmp_path).state != "COMPLETE"


def test_tag_only_positions_reach_release_layout(tmp_path: Path) -> None:
    track = make_flac(tmp_path / "Title.flac")
    _identity(track, DISCNUMBER="2", TRACKNUMBER="1", TOTALTRACKS="1")
    assert library_layout.disc_counts([track], tmp_path) == {2: 1}


def test_completeness_rejects_conflicting_total_aliases(tmp_path: Path) -> None:
    track = make_flac(tmp_path / "01 - Title.flac")
    _identity(track, TRACKNUMBER="1", TOTALTRACKS="1", TRACKTOTAL="2")
    assert album_completeness.assess_album_completeness([track], tmp_path).state == "INCOMPLETE"


def test_duration_layout_checks_iterables_once() -> None:
    media = iter([{"position": 1, "track-count": 1, "tracks": [{"length": 200000}]}])
    result = release_match.assess_medium_layouts({1: 1}, media, {1: (20.0,)})
    assert not result["compatible"]


def test_ambiguous_unnumbered_media_do_not_choose_first() -> None:
    medium = {"tracks": [{"title": "Same Song", "number": "1"}]}
    selected, reason = mb_track_match.choose_medium({"media": [dict(medium), dict(medium)]}, 2, "Same Song", 1)
    assert selected is None
    assert "ambiguous" in reason


def test_discovery_includes_uppercase_flac_and_excludes_directories(tmp_path: Path) -> None:
    source = make_flac(tmp_path / "Music.FLAC")
    (tmp_path / "directory.flac").mkdir()
    private = tmp_path / ".metadata-transaction-old"
    private.mkdir()
    shutil.copy2(source, private / "staged.FLAC")
    assert library_files.iter_library_flacs(tmp_path) == [source]


def test_config_environment_selection_is_not_cached_as_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = tmp_path / "first.toml", tmp_path / "second.toml"
    first.write_text("[lyrics]\nonline_lookup = true\n")
    second.write_text("[lyrics]\nonline_lookup = false\n")
    pipeline_config.load_config.cache_clear()
    monkeypatch.setenv("AUDIOPHILE_CONFIG", str(first))
    assert pipeline_config.load_config().lyrics.online_lookup
    monkeypatch.setenv("AUDIOPHILE_CONFIG", str(second))
    assert not pipeline_config.load_config().lyrics.online_lookup
    pipeline_config.load_config.cache_clear()


def test_loudness_rejects_numeric_overflow(tmp_path: Path) -> None:
    track = make_flac(tmp_path / "01.flac")
    _identity(track, REPLAYGAIN_TRACK_GAIN="9" * 400 + " dB", REPLAYGAIN_TRACK_PEAK="9" * 400)
    assert not loudness.validate_replaygain(track, require_album=False)[0]


def test_multidisc_dr_measurements_do_not_collide_by_basename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tracks = []
    for disc in (1, 2):
        folder = tmp_path / f"CD{disc}"
        folder.mkdir()
        tracks.append(make_flac(folder / "01 - Same.flac"))

    def measure(path: Path) -> dict[str, str]:
        return {"name": path.name, "dr": "8" if path.parent.name == "CD1" else "12", "peak": "-1", "rms": "-10"}

    monkeypatch.setattr(tagger.dr14_parse, "measure_audio_file", measure)
    reports: list[str] = []
    result, album_dr, _ = tagger.measure_album_dr14(tmp_path, tracks, report_sink=reports.append)
    assert result == {"CD1/01 - Same.flac": ("8", "-1", "-10"), "CD2/01 - Same.flac": ("12", "-1", "-10")}
    assert album_dr == "10"
    assert len(reports) == 1


def test_unicode_artwork_identity_is_not_empty_or_compatible_with_any_artist() -> None:
    assert covers.compute_album_match("心", "心") == 1.0
    assert not covers.artist_matches("Кино", "Queen")
    assert not covers.artist_matches("", "Queen")


@pytest.mark.parametrize("offset", [100000, -2000])
def test_enhanced_lrc_applies_offset_to_word_validation(offset: int) -> None:
    value = f"[offset:{offset}]\n[00:01.00]<00:01.00>One <00:02.00>two"
    assert not validate_enhanced_lrc(value, duration=10.0)[0]


def test_discogs_long_retry_after_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRIPT_DIR", str(tmp_path))
    finder = discogs_release_find.DiscogsFinder(tmp_path, "Artist", "Album")
    calls: list[str] = []

    class Response:
        status_code = 429

        def __init__(self) -> None:
            self.headers = {"Retry-After": "3600"}

        def close(self) -> None:
            pass

    def get(url: str, **_kwargs: object) -> Response:
        calls.append(url)
        return Response()

    monkeypatch.setattr(finder.session, "get", get)
    monkeypatch.setattr(discogs_release_find._DISCOGS_LIMITER, "acquire", lambda: None)
    import provider_cache

    monkeypatch.setattr(provider_cache.time, "sleep", lambda delay: pytest.fail(f"unbounded sleep: {delay}"))
    assert finder.discogs_api_get("https://fixture.invalid/release/1") is None
    assert len(calls) == 1


def test_album_lock_refuses_concurrent_run_before_providers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_flac(tmp_path / "01.flac")
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(tagger, "discogs_get", lambda *_a, **_k: pytest.fail("provider called while locked"))
        assert not tagger.process_album(
            f"1:::{tmp_path}",
            "fixture",
            "fixture@example.org",
            quiet=True,
            no_loudgain=True,
            no_dr14=True,
            threads=1,
            session=requests.Session(),
        )
    finally:
        os.close(descriptor)
    assert not list(tmp_path.glob(".*"))


def test_cache_and_provider_diagnostics_do_not_persist_credential_keys(tmp_path: Path) -> None:
    cache = provider_cache.JsonCache(tmp_path)
    url = "https://fixture.invalid/api?apikey=fixture-private-value&track=1"
    destination = cache.store(url, {"found": True})
    assert "fixture-private-value" not in destination.read_text()
    assert cache.load(url) == (200, {"found": True})
    assert "fixture-private-value" not in provider_cache.redact_secrets(f"request failed: {url}")
    from lyrics_utils import classify_lyrics_exception

    _, message = classify_lyrics_exception(RuntimeError(f"Bearer fixture-private-value url={url}"))
    assert "fixture-private-value" not in message


def test_partial_ledger_write_keeps_old_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = make_flac(tmp_path / "track.flac")
    ledger = tmp_path / ".metadata-provenance.jsonl"
    ledger.write_text(json.dumps({"prior": True}) + "\n")
    old_ledger, old_flac = ledger.read_bytes(), source.read_bytes()
    real_fsync = os.fsync

    def fsync(descriptor: int) -> None:
        # The ledger helper is reached only after the FLAC save/validation.
        if ".metadata-provenance.jsonl." in os.readlink(f"/proc/self/fd/{descriptor}"):
            raise OSError("ledger fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(metadata_writer.os, "fsync", fsync)
    with pytest.raises(OSError, match="ledger fsync failure"):
        metadata_writer.write_tags(source, core_tags())
    assert source.read_bytes() == old_flac
    assert ledger.read_bytes() == old_ledger
    assert set(tmp_path.iterdir()) == {source, ledger}


def test_writer_checks_pcm_when_streaminfo_md5_is_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = make_flac(tmp_path / "source.flac")
    replacement = tmp_path / "different.flac"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=700:duration=0.2", "-ac", "2", str(replacement)],
        check=True,
    )
    for path in (source, replacement):
        audio = FLAC(path)
        audio.info.md5_signature = 0
        audio.save()
    assert metadata_writer._stream_signature(source) == metadata_writer._stream_signature(replacement)
    before = source.read_bytes()
    actual_write = metadata_writer._write_tags_in_place

    def corrupt(path: Path, *args: Any, **kwargs: Any) -> Any:
        decisions = actual_write(path, *args, **kwargs)
        shutil.copy2(replacement, path)
        return decisions

    monkeypatch.setattr(metadata_writer, "_write_tags_in_place", corrupt)
    with pytest.raises(RuntimeError, match="decoded audio samples"):
        metadata_writer.write_tags(source, core_tags())
    assert source.read_bytes() == before


def test_lyric_materialization_preserves_dangling_symlink(tmp_path: Path) -> None:
    from lyrics_pipeline import materialize_embedded

    source = tmp_path / "track.flac"
    live = tmp_path / "track.txt"
    outside = tmp_path / "missing-target.txt"
    live.symlink_to(outside)
    stage = tmp_path / "stage"
    stage.mkdir()
    result = materialize_embedded(
        stage / source.name, {"LYRICS": ["A curated line\nAnother curated line"]}, existing_base=source, duration=10
    )
    assert result["plain"].parent == stage
    assert result["plain"].name != live.name
    assert live.is_symlink()
    assert not outside.exists()


@pytest.mark.parametrize("score", [0.1, "NaN", None])
def test_acoustid_id_requires_a_finite_confident_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, score: object
) -> None:
    monkeypatch.setenv("ACOUSTID_API_KEY", "fixture-client-key")
    payload = {"status": "ok", "results": [{"id": "wrong-id", "score": score}]}
    session = requests.Session()

    class Response:
        status_code = 200

        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

        def json(self) -> dict[str, Any]:
            return payload

        def close(self) -> None:
            pass

    monkeypatch.setattr(session, "get", lambda *_a, **_k: Response())
    monkeypatch.setattr(tagger, "JsonCache", lambda _path: provider_cache.JsonCache(tmp_path))
    assert tagger.resolve_acoustid_id(180, "fixture-fingerprint", session) == ""


def test_discovery_and_enrichment_share_host_pacing() -> None:
    assert tagger.mb_rate_limiter is discogs_release_find._MB_LIMITER
    assert tagger.discogs_rate_limiter is discogs_release_find._DISCOGS_LIMITER
