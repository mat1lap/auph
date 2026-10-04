"""Whole-project audit regressions: real local wiring, offline boundaries."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Self

import pytest
import requests
from mutagen.flac import FLAC

import audiophile_cover_fetcher as covers
import audiophile_tagger as tagger
import discogs_release_find as finder_module
import loudness
import mb_identity_score
import mb_track_match
import metadata_schema
import metadata_writer
import pipeline_config
import release_match
from test_pipeline_safety import core_tags, make_flac


def album_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[Path]]:
    folder = tmp_path / "Artist" / "Album"
    folder.mkdir(parents=True)
    tracks = [make_flac(folder / f"{number:02} - Song {number}.flac", seconds=1) for number in (1, 2)]
    for number, path in enumerate(tracks, 1):
        audio = FLAC(path)
        for key, value in core_tags(f"Song {number}"):
            audio[key] = [value]
        audio["TRACKNUMBER"] = [str(number)]
        audio["TRACKTOTAL"] = ["2"]
        audio.save()
    policy = tmp_path / "policy.toml"
    policy.write_text("[lyrics]\nonline_lookup=false\n")
    monkeypatch.setenv("AUDIOPHILE_CONFIG", str(policy))
    monkeypatch.delenv("ACOUSTID_API_KEY", raising=False)
    monkeypatch.delenv("ACOUSTID_CLIENT_KEY", raising=False)
    monkeypatch.setattr(
        tagger,
        "discogs_get",
        lambda *_a, **_k: {
            "id": 42,
            "title": "Album",
            "artists": [{"name": "Artist"}],
            "released": "2000",
            "formats": [{"name": "CD"}],
            "tracklist": [
                {"title": f"Song {number}", "position": str(number), "type_": "track", "duration": "0:01"}
                for number in (1, 2)
            ],
        },
    )
    monkeypatch.setattr(tagger, "mb_get", lambda *_a, **_k: (404, {}))
    for method in (
        "query_itunes",
        "query_deezer",
        "query_cover_art_archive",
        "query_discogs",
        "query_local",
        "query_amazon",
        "query_fanart_tv",
        "query_bandcamp",
        "query_tidal",
        "query_qobuz",
        "query_theaudiodb",
    ):
        monkeypatch.setattr(covers.CoverFetcher, method, lambda _self: [])
    return folder, tracks


@pytest.mark.parametrize("after_replace", [False, True])
def test_album_interrupt_during_commit_rolls_back_all_tracks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_replace: bool
) -> None:
    folder, tracks = album_fixture(tmp_path, monkeypatch)
    before = {path: path.read_bytes() for path in tracks}
    replace = os.replace

    def interrupt(src: Any, dst: Any) -> None:
        if Path(dst) == tracks[1] and Path(src).name == tracks[1].name and "pending" in Path(src).parts:
            if after_replace:
                replace(src, dst)
            raise KeyboardInterrupt("commit interrupted")
        replace(src, dst)

    monkeypatch.setattr(tagger.os, "replace", interrupt)
    with requests.Session() as session, pytest.raises(KeyboardInterrupt):
        tagger.process_album(f"42:::{folder}", "fixture", "fixture@example.org", True, True, True, 1, session)
    assert {path: path.read_bytes() for path in tracks} == before
    assert not list(folder.glob(".metadata-transaction-*"))
    assert not (folder / ".metadata-provenance.jsonl").exists()


def test_writer_interrupt_after_replacement_restores_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    track = make_flac(tmp_path / "track.flac")
    before = track.read_bytes()
    replace = os.replace

    def interrupt(src: Any, dst: Any) -> None:
        replace(src, dst)
        if str(src).endswith(".pending.flac") and Path(dst) == track:
            raise KeyboardInterrupt("immediately after publication")

    monkeypatch.setattr(metadata_writer.os, "replace", interrupt)
    with pytest.raises(KeyboardInterrupt):
        metadata_writer.write_tags(track, core_tags())
    assert track.read_bytes() == before
    assert not list(tmp_path.glob(".*.flac"))


def test_track_only_gain_removes_stale_album_range_without_scanner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track = make_flac(tmp_path / "track.flac")
    audio = FLAC(track)
    audio["REPLAYGAIN_TRACK_GAIN"] = ["-3.00 dB"]
    audio["REPLAYGAIN_TRACK_PEAK"] = ["0.5"]
    audio["REPLAYGAIN_ALBUM_RANGE"] = ["8.00 dB"]
    audio.save()
    before = metadata_writer.pcm_digest(track)
    monkeypatch.setattr(tagger.shutil, "which", lambda _name: None)
    assert tagger.run_loudgain([track], quiet=True, complete_album=False)
    assert not FLAC(track).get("REPLAYGAIN_ALBUM_RANGE")
    assert loudness.validate_replaygain(track, require_album=False)[0]
    assert metadata_writer.pcm_digest(track) == before


@pytest.mark.parametrize(
    "text",
    [
        "[matching]\nrelease_min_margin=-1",
        "[matching]\nrelease_min_score=nan",
        '[lyrics]\nonline_lookup="false"',
        "[matching]\nacoustid_min_score=2",
    ],
)
def test_invalid_safety_policy_is_rejected(tmp_path: Path, text: str) -> None:
    policy = tmp_path / "invalid.toml"
    policy.write_text(text)
    with pytest.raises((ValueError, TypeError)):
        pipeline_config.load_config(str(policy))


def test_synthetic_disc_id_cannot_certify_a_musicbrainz_edition() -> None:
    source = {"id": 42, "title": "Album", "artists": [{"name": "Artist"}], "formats": [{"name": "CD"}]}
    remote = {
        "title": "Album",
        "artist-credit": [{"name": "Artist"}],
        "media": [{"position": 1, "format": "CD", "discs": [{"id": "synthetic"}], "tracks": [{"title": "Song"}]}],
    }
    evidence = {"discid": "synthetic", "toc_exact": False, "track_count": 1, "toc_titles": ["Song"]}
    result = mb_identity_score.evaluate_identity(source, remote, toc=evidence)
    assert not result["flags"]["medium_discid_exact"]
    assert result["confidence"] not in {"CERTAIN", "EXACT"}


@pytest.mark.parametrize("length", [240_000, "nan"])
def test_track_match_uses_finite_recording_duration_fallback(length: Any) -> None:
    release = {
        "media": [
            {
                "position": 1,
                "tracks": [
                    {
                        "id": "track",
                        "number": "1",
                        "title": "Song",
                        "recording": {"id": "recording", "length": length, "artist-credit": [{"name": "Artist"}]},
                    }
                ],
            }
        ]
    }
    match = mb_track_match.resolve_mb_track(release, 1, 1, "Song", "Song", "Artist", 120)
    assert not match["ok"]


@pytest.mark.parametrize("score", [float("inf"), "garbage"])
def test_release_selection_rejects_malformed_scores(score: Any) -> None:
    candidate = {"id": "edition", "identity": {"rejected": False, "confidence": "HIGH", "score": score}}
    assert release_match.select_release_candidate([candidate]).candidate is None


def test_discogs_discovery_does_not_relabel_remote_catalog_as_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCRIPT_DIR", str(tmp_path))
    finder = finder_module.DiscogsFinder(tmp_path, artist="Artist", album="Album")
    monkeypatch.delenv("ACOUSTID_API_KEY", raising=False)
    monkeypatch.delenv("ACOUSTID_CLIENT_KEY", raising=False)
    local = {"discid": "synthetic", "toc_info": {"toc_exact": False}, "catnos": [], "barcodes": []}

    def mb(url: str) -> dict[str, Any]:
        if "/discid/" in url:
            return {"releases": [{"id": "mb-edition"}]}
        return {
            "relations": [{"url": {"resource": "https://www.discogs.com/release/42"}}],
            "label-info": [{"catalog-number": "REMOTE-123"}],
        }

    monkeypatch.setattr(finder, "mb_api_get", mb)
    monkeypatch.setattr(finder, "discogs_api_get", lambda _url: {"results": []})
    candidates = finder.search_candidates(local)
    assert local["catnos"] == []
    assert candidates["42"]["bridge_bonus"] < 1000


def test_missing_value_is_not_serialized_as_the_literal_none() -> None:
    assert metadata_schema.normalize_value("TITLE", None) == ""


@pytest.mark.parametrize("old_ledger", [None, b'{"previous": true}\n'])
def test_writer_interrupt_after_ledger_publication_restores_ledger_and_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old_ledger: bytes | None
) -> None:
    track = make_flac(tmp_path / "track.flac")
    before = track.read_bytes()
    ledger = tmp_path / ".metadata-provenance.jsonl"
    if old_ledger is not None:
        ledger.write_bytes(old_ledger)
    replace = os.replace
    interrupted = False

    def interrupt(src: Any, dst: Any) -> None:
        nonlocal interrupted
        replace(src, dst)
        if Path(dst) == ledger and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("after ledger publication")

    monkeypatch.setattr(metadata_writer.os, "replace", interrupt)
    with pytest.raises(KeyboardInterrupt):
        metadata_writer.write_tags(track, core_tags())
    assert track.read_bytes() == before
    assert (ledger.read_bytes() if ledger.exists() else None) == old_ledger


def test_discovery_uppercase_and_direct_disc_root(tmp_path: Path) -> None:
    album = tmp_path / "Artist" / "Album"
    disc = album / "CD1"
    disc.mkdir(parents=True)
    make_flac(disc / "Song.FLAC")
    assert finder_module.find_album_folders([album], base_dir=tmp_path) == [album]


def test_discovery_ignores_explicit_symlink_and_private_file(tmp_path: Path) -> None:
    private = tmp_path / ".metadata-transaction-residue" / "pending"
    private.mkdir(parents=True)
    track = make_flac(private / "track.flac")
    alias = tmp_path / "outside.flac"
    alias.symlink_to(track)
    assert finder_module.find_album_folders([track, alias], base_dir=tmp_path) == []


def test_equal_certain_discogs_editions_require_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    finder = finder_module.DiscogsFinder(tmp_path, artist="Artist", album="Album")
    local = {"track_count": 1, "durations": [120], "catnos": ["ABC123"], "barcodes": ["123456"], "matrices": []}
    monkeypatch.setattr(finder, "extract_local_signatures", lambda: local)
    monkeypatch.setattr(finder, "search_candidates", lambda _local: {"2": {}, "1": {}})
    monkeypatch.setattr(
        finder,
        "discogs_api_get",
        lambda _url: {
            "title": "Album",
            "artists": [{"name": "Artist"}],
            "formats": [{"name": "CD"}],
            "labels": [{"catno": "ABC123"}],
            "identifiers": [{"type": "Barcode", "value": "123456"}],
            "tracklist": [{"title": "Song", "duration": "2:00", "type_": "track"}],
        },
    )
    result = finder.find()
    assert result["confidence"] == "CERTAIN"
    assert result["review_required"] and not result["ok"]
    assert [row["id"] for row in result["candidates"]] == ["1", "2"]


def test_config_reload_and_explicit_missing_path(tmp_path: Path) -> None:
    config = tmp_path / "policy.toml"
    with pytest.raises(FileNotFoundError):
        pipeline_config.load_config(str(config))
    config.write_text("[lyrics]\nonline_lookup=true\n")
    assert pipeline_config.load_config(str(config)).lyrics.online_lookup
    config.write_text("[lyrics]\nonline_lookup=false\n")
    assert not pipeline_config.load_config(str(config)).lyrics.online_lookup


@pytest.mark.parametrize("seconds", [1, 6])
def test_native_dr_reports_actual_peak_and_rms_for_short_and_mono_audio(seconds: int) -> None:
    import numpy as np

    import dr14_parse

    samples = np.full((seconds * 48000, 1), 0.25)
    dr, peak, rms = dr14_parse.compute_dr14_samples(samples, 48000)
    assert dr == 0
    assert peak == "-12.04 dB"
    assert rms == "-9.03 dB"  # DR RMS uses the documented sine-reference +3 dB convention.


def test_native_dr_handles_silence_without_overflow() -> None:
    import numpy as np

    import dr14_parse

    dr, peak, rms = dr14_parse.compute_dr14_samples(np.zeros((48000 * 6, 2)), 48000)
    assert dr == 0
    assert float(peak.split()[0]) < -100 and float(rms.split()[0]) < -100


def test_partial_dr_measurement_does_not_claim_album_dr(tmp_path: Path) -> None:
    import dr14_parse

    good = make_flac(tmp_path / "good.flac", seconds=4)
    bad = tmp_path / "bad.flac"
    bad.write_bytes(b"invalid audio")
    tracks, album_dr = dr14_parse.measure_audio_files([good, bad])
    assert len(tracks) == 1
    assert album_dr is None


def test_dr_report_normalization_does_not_erase_non_latin_identity() -> None:
    import dr14_parse

    assert dr14_parse.norm("Артист") != dr14_parse.norm("Другой")
    assert dr14_parse.norm("Café") == dr14_parse.norm("Cafe\u0301")


def test_discogs_sparse_durations_keep_positions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    finder = finder_module.DiscogsFinder(tmp_path, artist="Artist", album="Album")
    monkeypatch.setattr(
        finder,
        "discogs_api_get",
        lambda _url: {
            "title": "Album",
            "artists": [{"name": "Artist"}],
            "formats": [{"name": "CD"}],
            "tracklist": [{"title": "One", "duration": ""}, {"title": "Two", "duration": "5:00"}],
        },
    )
    local = {"track_count": 2, "durations": [120, 120]}
    candidate = finder.score_candidate("1", local, 0, [])
    assert not any("duration:avg_delta=0" in reason for reason in candidate["reasons"])
    assert any("duration:mismatch" in reason for reason in candidate["reasons"])


def test_discogs_acoustid_bridge_retains_confident_later_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import urllib.parse

    track = make_flac(tmp_path / "01.flac", seconds=1)
    audio = FLAC(track)
    audio["ACOUSTID_FINGERPRINT"] = ["fixture-fingerprint"]
    audio.save()
    monkeypatch.setenv("ACOUSTID_API_KEY", "fixture-key")
    monkeypatch.setenv("SCRIPT_DIR", str(tmp_path))
    finder = finder_module.DiscogsFinder(tmp_path, artist="Artist", album="Album")

    class Response:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {
                "status": "ok",
                "results": [
                    {"id": "weak", "score": 0.1, "recordings": [{"releases": [{"id": "wrong"}]}]},
                    {"id": "strong", "score": 0.99, "recordings": [{"releases": [{"id": "right"}]}]},
                ],
            }

        def close(self) -> None:
            pass

    def http(url: str, **kwargs: Any) -> Response:
        assert "acoustid.org" in url
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query) or kwargs.get("params", {})
        assert params.get("meta") in (["recordingids releases"], "recordingids releases")
        return Response()

    monkeypatch.setattr(finder.session, "get", http)
    looked_up: list[str] = []

    def mb(url: str) -> dict[str, Any]:
        looked_up.append(url)
        return {"relations": [{"url": {"resource": "https://www.discogs.com/release/42"}}]}

    monkeypatch.setattr(finder, "mb_api_get", mb)
    monkeypatch.setattr(finder, "discogs_api_get", lambda _url: {"results": []})
    result = finder.search_candidates({"catnos": [], "barcodes": []})
    assert "42" in result
    assert any("/release/right?" in url for url in looked_up)
    assert not any("/release/wrong?" in url for url in looked_up)


def test_json_cache_disappearance_is_a_miss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from provider_cache import JsonCache

    cache = JsonCache(tmp_path)
    cache.store("url", {"value": 1})
    real_stat = Path.stat
    calls = 0

    def stat(path: Path, **kwargs: Any) -> os.stat_result:
        nonlocal calls
        if path == cache.path_for("url"):
            calls += 1
            if calls >= 1:
                raise FileNotFoundError("concurrent cache eviction")
        return real_stat(path, **kwargs)

    monkeypatch.setattr(Path, "is_file", lambda _path: True)
    monkeypatch.setattr(Path, "stat", stat)
    assert cache.load("url") is None


def test_token_checks_use_environment_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import check_tokens

    env = tmp_path / ".env"
    env.write_text("DISCOGS_TOKEN=file-value\n")
    monkeypatch.setenv("DISCOGS_TOKEN", "environment-value")
    loaded = check_tokens.load_env_file(env)
    assert loaded["DISCOGS_TOKEN"] == "environment-value"


def test_acoustid_diagnostic_does_not_claim_authentication_on_error_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    import check_tokens

    class Response(io.BytesIO):
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    monkeypatch.setattr(
        check_tokens.urllib.request,
        "urlopen",
        lambda *_a, **_k: Response(b'{"status":"error","error":{"code":4,"message":"invalid API key"}}'),
    )
    assert check_tokens.check_acoustid("fixture-key").status == "FAIL"


@pytest.mark.parametrize("old_ledger", [None, b'{"previous":true}\n'])
def test_album_interrupt_after_final_ledger_restores_original_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old_ledger: bytes | None
) -> None:
    folder, tracks = album_fixture(tmp_path, monkeypatch)
    ledger = folder / ".metadata-provenance.jsonl"
    if old_ledger is not None:
        ledger.write_bytes(old_ledger)
    before = {path: path.read_bytes() for path in tracks}
    replace = os.replace
    interrupted = False

    def interrupt(src: Any, dst: Any) -> None:
        nonlocal interrupted
        replace(src, dst)
        if Path(dst) == ledger and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("after final ledger")

    monkeypatch.setattr(tagger.os, "replace", interrupt)
    with requests.Session() as session, pytest.raises(KeyboardInterrupt):
        tagger.process_album(f"42:::{folder}", "fixture", "fixture@example.org", True, True, True, 1, session)
    assert {path: path.read_bytes() for path in tracks} == before
    assert (ledger.read_bytes() if ledger.exists() else None) == old_ledger
    assert not list(folder.glob(".metadata-transaction-*"))


def test_musicbrainz_release_track_credit_survives_recording_alias() -> None:
    release = {
        "media": [
            {
                "position": 1,
                "tracks": [
                    {
                        "id": "track",
                        "position": 1,
                        "number": "1",
                        "title": "Song",
                        "length": 120000,
                        "artist-credit": [
                            {"name": "Credited Artist", "artist": {"name": "Canonical Artist", "id": "artist"}}
                        ],
                        "recording": {
                            "id": "recording",
                            "artist-credit": [
                                {"name": "Canonical Artist", "artist": {"name": "Canonical Artist", "id": "artist"}}
                            ],
                        },
                    }
                ],
            }
        ]
    }
    match = mb_track_match.resolve_mb_track(release, 1, 1, "Song", "Song", "Credited Artist", 120)
    assert match["ok"]
    assert match["artist_credit"] == "Credited Artist"
    assert match["artist_names"] == ["Credited Artist"]


def test_equal_recordings_without_positions_are_ambiguous() -> None:
    release = {
        "media": [
            {
                "position": 1,
                "tracks": [
                    {"id": name, "title": "Song", "length": 120000, "recording": {"id": name}}
                    for name in ("one", "two")
                ],
            }
        ]
    }
    match = mb_track_match.resolve_mb_track(release, 1, 1, "Song", "Song", "", 120)
    assert not match["ok"]


def test_cue_missing_index_is_not_silently_dropped(tmp_path: Path) -> None:
    from mb_discid_from_cue import parse_cue

    cue = tmp_path / "album.cue"
    cue.write_text(
        'FILE "album.flac" WAVE\nTRACK 01 AUDIO\nINDEX 01 00:00:00\nTRACK 02 AUDIO\nTITLE "Missing position"\n'
    )
    with pytest.raises(ValueError, match="INDEX"):
        parse_cue(cue)


def test_late_report_collision_aborts_album_without_overwriting_user_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dr14_parse

    folder, tracks = album_fixture(tmp_path, monkeypatch)
    before = {path: path.read_bytes() for path in tracks}
    report = folder / "dr14.txt"
    user_content = b"curated report created by another application"
    monkeypatch.setattr(dr14_parse, "search_dr_database", lambda *_a, **_k: [])
    replace, link = os.replace, os.link

    def collide(src: Any, dst: Any) -> None:
        if Path(dst) == report:
            report.write_bytes(user_content)
        replace(src, dst)

    def collide_link(src: Any, dst: Any, **kwargs: Any) -> None:
        if Path(dst) == report:
            report.write_bytes(user_content)
        link(src, dst, **kwargs)

    monkeypatch.setattr(tagger.os, "replace", collide)
    monkeypatch.setattr(tagger.os, "link", collide_link)
    with requests.Session() as session:
        assert not tagger.process_album(
            f"42:::{folder}", "fixture", "fixture@example.org", True, True, False, 1, session
        )
    assert report.read_bytes() == user_content
    assert {path: path.read_bytes() for path in tracks} == before


def test_silent_audio_has_no_inferred_bpm(tmp_path: Path) -> None:
    track = make_flac(tmp_path / "silence.flac", seconds=3)
    assert tagger.compute_bpm_from_pcm(track) == ""


def test_complete_release_tracks_without_declared_counts_reach_completeness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from album_completeness import assess_album_completeness

    folder, tracks = album_fixture(tmp_path, monkeypatch)
    for path in tracks:
        audio = FLAC(path)
        del audio["TRACKTOTAL"]
        audio.save()
    release = {"media": [{"position": 1, "tracks": [{"position": 1}, {"position": 2}]}]}
    counts = release_match.release_disc_counts(release["media"])
    decision = assess_album_completeness(tracks, folder, external_disc_counts=counts)
    assert decision.state == "COMPLETE"
    assert release_match.assess_medium_layouts({1: 2}, release["media"])["complete"]


def test_synthetic_toc_orders_tracks_by_position_not_filename(tmp_path: Path) -> None:
    from mb_discid_from_cue import build_from_flac_dir

    for number, name, seconds in ((1, "Zebra.flac", 2), (2, "Apple.FLAC", 3)):
        path = make_flac(tmp_path / name, seconds=seconds)
        audio = FLAC(path)
        audio["TRACKNUMBER"] = [str(number)]
        audio["TITLE"] = [name.split(".")[0]]
        audio.save()
    toc = build_from_flac_dir(tmp_path)
    assert toc["toc_titles"] == ["Zebra", "Apple"]
    assert toc["toc_durations"] == [2, 3]
    assert not toc["is_partial"] and not toc["toc_exact"]


def test_flat_multidisc_toc_does_not_repeat_all_files_for_each_disc(tmp_path: Path) -> None:
    from mb_discid_from_cue import build_all

    for disc in (1, 2):
        for track in (1, 2):
            path = make_flac(tmp_path / f"{disc}-{track:02} - Song.flac", seconds=1)
            audio = FLAC(path)
            audio["DISCNUMBER"] = [str(disc)]
            audio["TRACKNUMBER"] = [str(track)]
            audio.save()
    toc = build_all(tmp_path)
    assert set(toc) == {1, 2}
    assert all(item["track_count"] == 2 and not item["is_partial"] for item in toc.values())


def test_new_artifact_interruption_records_link_for_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"staged bytes")
    published: list[Path] = []
    link = os.link

    def interrupt(src: Any, dst: Any, **kwargs: Any) -> None:
        link(src, dst, **kwargs)
        raise KeyboardInterrupt("after link")

    monkeypatch.setattr(tagger.os, "link", interrupt)
    with pytest.raises(KeyboardInterrupt):
        tagger.publish_new_artifact(source, destination, published)
    assert published == [destination]
    assert source.read_bytes() == destination.read_bytes()


@pytest.mark.parametrize("field,value", [("DATE", "2020-02-30"), ("ORIGINALDATE", "2020-13"), ("BPM", float("nan"))])
def test_writer_rejects_invalid_incoming_dates_and_nonfinite_numbers(tmp_path: Path, field: str, value: Any) -> None:
    track = make_flac(tmp_path / "track.flac")
    before = track.read_bytes()
    with pytest.raises(ValueError):
        metadata_writer.write_tags(track, [*(row for row in core_tags() if row[0] != field), (field, value)])
    assert track.read_bytes() == before


def test_writer_picture_only_uppercase_cli_uses_writer_validation_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from PIL import Image

    track = make_flac(tmp_path / "Track.FLAC")
    picture = tmp_path / "picture.png"
    Image.new("RGB", (32, 32), "red").save(picture)
    monkeypatch.setattr(sys, "argv", ["metadata_writer.py", str(track), "--picture", str(picture)])
    before = metadata_writer.pcm_digest(track)
    assert metadata_writer.main() == 0
    assert len(FLAC(track).pictures) == 1
    assert metadata_writer.pcm_digest(track) == before


def test_eight_bit_flac_analysis_uses_eight_bit_pcm(tmp_path: Path) -> None:
    import subprocess

    import dr14_parse

    raw = tmp_path / "samples.raw"
    raw.write_bytes(bytes([32]) * 32000)
    track = tmp_path / "eight-bit.flac"
    subprocess.run(
        [
            "flac",
            "-s",
            "--force-raw-format",
            "--endian=little",
            "--sign=signed",
            "--channels=1",
            "--bps=8",
            "--sample-rate=8000",
            "-o",
            str(track),
            str(raw),
        ],
        check=True,
    )
    assert FLAC(track).info.bits_per_sample == 8
    measured = dr14_parse.measure_audio_file(track)
    assert measured["peak"] == "-12.04 dB" and measured["rms"] == "-9.03 dB"
    assert tagger.get_track_eac_peak([], 1, track) == "25.0 %"


def test_artwork_provider_error_redacts_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fetcher = covers.CoverFetcher(artist="Artist", album="Album", folder=tmp_path, write_preview_files=False)
    for method in (
        "query_itunes",
        "query_deezer",
        "query_cover_art_archive",
        "query_discogs",
        "query_local",
        "query_amazon",
        "query_fanart_tv",
        "query_bandcamp",
        "query_tidal",
        "query_qobuz",
        "query_theaudiodb",
    ):
        monkeypatch.setattr(fetcher, method, list)

    def fail() -> list[covers.CoverCandidate]:
        raise requests.RequestException("Authorization: OAuth fixture-secret https://provider/?api_key=fixture-key")

    monkeypatch.setattr(fetcher, "query_itunes", fail)
    fetcher.find_best_cover(verbose=True)
    logged = capsys.readouterr().out + str(fetcher.last_fetch_errors)
    assert "fixture-secret" not in logged and "fixture-key" not in logged


def test_malformed_optional_analysis_does_not_discard_good_bpm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status_code = 200

        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload

        def json(self) -> dict[str, Any]:
            return self.payload

        def close(self) -> None:
            pass

    class Session:
        def get(self, url: str, **_kwargs: Any) -> Response:
            payload = (
                {"rhythm": {"bpm": 130}}
                if "low-level" in url
                else {
                    "highlevel": {"danceability": {"value": "danceable", "probability": "malformed"}, "mood_happy": []},
                }
            )
            return Response(payload)

    session = requests.Session()
    monkeypatch.setattr(session, "get", Session().get)
    result = tagger.get_acousticbrainz_data("fixture-recording", session, tmp_path)
    assert result["bpm"] == "130" and result["danceability"] == "" and result["moods"] == []


def test_http_success_error_envelope_is_not_cached_as_found(tmp_path: Path) -> None:
    from provider_cache import JsonCache, fetch_json

    class Response:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {"status": "error", "error": {"message": "transient service failure"}}

        def close(self) -> None:
            pass

    class Session:
        calls = 0

        def get(self, url: str, **_kwargs: Any) -> Response:
            self.calls += 1
            return Response()

    session = Session()
    cache = JsonCache(tmp_path)
    for _ in range(2):
        result = fetch_json(session, "https://fixture/provider", headers={}, cache=cache)
        assert not result.ok
        assert result.classification == "INVALID_RESPONSE"
    assert session.calls == 2 and not list(tmp_path.glob("*.json"))


def test_json_cache_separates_authentication_and_language(tmp_path: Path) -> None:
    from provider_cache import JsonCache, fetch_json

    class Response:
        status_code = 200

        def __init__(self, value: str) -> None:
            self.value = value

        def json(self) -> dict[str, Any]:
            return {"value": self.value}

        def close(self) -> None:
            pass

    class Session:
        calls = 0

        def get(self, url: str, **kwargs: Any) -> Response:
            self.calls += 1
            return Response(kwargs["headers"]["Authorization"] + kwargs["headers"]["Accept-Language"])

    session = Session()
    cache = JsonCache(tmp_path)
    for token, language in (("fixture-a", "en"), ("fixture-b", "en"), ("fixture-b", "fr")):
        result = fetch_json(
            session,
            "https://fixture/private",
            headers={"Authorization": token, "Accept-Language": language},
            cache=cache,
        )
        assert result.payload == {"value": token + language}
    assert session.calls == 3


def test_dr_cli_measure_failure_has_nonzero_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    import dr14_parse

    bad = tmp_path / "invalid.flac"
    bad.write_bytes(b"not a FLAC")
    monkeypatch.setattr(sys, "argv", ["dr14_parse.py", "--measure", str(bad)])
    assert dr14_parse.main() == 1


def test_dr_report_refuses_dangling_symlink(tmp_path: Path) -> None:
    import dr14_parse

    outside = tmp_path / "outside-report"
    report = tmp_path / "dr14.txt"
    report.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        dr14_parse.save_dr14_report(tmp_path, "measured report")
    assert report.is_symlink() and not outside.exists()


def test_track_only_clears_stale_album_tags_even_without_valid_track_gain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track = make_flac(tmp_path / "track.flac")
    audio = FLAC(track)
    audio["REPLAYGAIN_ALBUM_GAIN"] = ["-4.00 dB"]
    audio["REPLAYGAIN_ALBUM_PEAK"] = ["0.5"]
    audio["REPLAYGAIN_ALBUM_RANGE"] = ["9.00 dB"]
    audio.save()
    before_pcm = metadata_writer.pcm_digest(track)
    monkeypatch.setattr(tagger.shutil, "which", lambda _name: None)
    outcome: dict[str, str] = {}
    assert tagger.run_loudgain([track], quiet=True, complete_album=False, outcome=outcome)
    assert not any(name.upper().startswith("REPLAYGAIN_ALBUM_") for name in FLAC(track))
    assert not FLAC(track).get("REPLAYGAIN_TRACK_GAIN")
    assert outcome["mode"] == "album-tags-cleared"
    assert metadata_writer.pcm_digest(track) == before_pcm


def test_track_versions_gate_clean_explicit_and_preserve_ordinary_title_words() -> None:
    assert mb_track_match.version_markers("Live Forever") == set()
    release = {
        "media": [
            {
                "position": 1,
                "tracks": [
                    {
                        "id": "track",
                        "number": "1",
                        "title": "Song (Explicit)",
                        "length": 120000,
                        "recording": {"id": "recording"},
                    }
                ],
            }
        ]
    }
    match = mb_track_match.resolve_mb_track(release, 1, 1, "Song (Clean)", "Song (Clean)", "", 120)
    assert not match["ok"] and "version-marker-conflict" in match["hard_conflicts"]
