"""Independent offline regressions; every mutable input lives under tmp_path."""

from __future__ import annotations

import io
import json
import runpy
import sys
from pathlib import Path
from typing import Any

import pytest
from mutagen.flac import FLAC, Picture
from PIL import Image

import album_completeness
import audiophile_cover_fetcher as covers
import audiophile_tagger as tagger
import discogs_release_find as discovery
import lyrics_pipeline as lyrics
import lyrics_utils
import mb_discid_from_cue as toc
import metadata_writer
from test_pipeline_safety import make_flac


def test_partial_provider_acquisition_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def provider(song: str, artist: str) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        lyrics_utils._record_provider_failure(TimeoutError("one independent result failed"))
        return {"found": True, "plainLyrics": "Hello world", "trackName": song, "artistName": artist}

    for _ in range(2):
        result = lyrics_utils.invoke_lyrics_provider("Fixture", provider, "Song", "Artist")
        assert result["found"]
    assert calls == 2


class ImageReply:
    status_code = 200

    def __init__(self) -> None:
        self.headers = {"Content-Type": "image/png"}

    def json(self) -> object:
        return {}

    def iter_content(self, chunk_size: int) -> list[bytes]:
        return [image_bytes()]

    def close(self) -> None:
        pass


def test_artwork_cache_separates_authorization_context(tmp_path: Path) -> None:
    calls = 0

    class Session:
        def get(self, *args: Any, **kwargs: Any) -> ImageReply:
            nonlocal calls
            calls += 1
            return ImageReply()

    fetcher = covers.CoverFetcher("Artist", "Album", cache_dir=tmp_path)
    fetcher.session = Session()
    url = "https://provider.invalid/art"
    assert fetcher.fetch_url(url, {"Authorization": "Bearer fixture-one"})
    assert fetcher.fetch_url(url, {"Authorization": "Bearer fixture-two"})
    assert calls == 2


def test_bad_caa_image_row_does_not_discard_valid_front(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = "af47a444-3553-47c9-9871-c03b0eb97349"
    fetcher = covers.CoverFetcher("Artist", "Album", mb_rel_id=release, cache_dir=tmp_path)
    payload = {
        "release": f"https://musicbrainz.org/release/{release}",
        "images": [
            None,
            {"front": True, "back": False, "image": f"https://coverartarchive.org/release/{release}/1.jpg"},
        ],
    }
    monkeypatch.setattr(fetcher, "fetch_url", lambda *_args, **_kwargs: json.dumps(payload).encode())
    candidates = fetcher.query_cover_art_archive()
    assert len(candidates) == 1 and candidates[0].release_tier == covers.ReleaseTier.EXACT_RELEASE


def image_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (600, 600), "purple").save(output, format="PNG")
    return output.getvalue()


def test_nested_uppercase_embedded_front_is_discovered(tmp_path: Path) -> None:
    disc = tmp_path / "Disc 2"
    disc.mkdir()
    track = make_flac(disc / "01 Song.FLAC")
    audio = FLAC(track)
    front = Picture()
    front.type, front.mime, front.width, front.height = 3, "image/png", 600, 600
    front.data = image_bytes()
    audio.add_picture(front)
    audio.save()
    fetcher = covers.CoverFetcher("Artist", "Album", folder=tmp_path, cache_dir=tmp_path / "cache")
    candidates = fetcher.query_local()
    assert len(candidates) == 1 and candidates[0].raw_data == front.data


def test_absolute_glob_finds_unicode_album(tmp_path: Path) -> None:
    album = tmp_path / "Артист" / "Альбом (2000)"
    album.mkdir(parents=True)
    make_flac(album / "01 Song.flac")
    assert discovery.find_album_folders([str(tmp_path / "Артист" / "*")], base_dir=tmp_path) == [album]


def test_explicit_track_cannot_traverse_symlinked_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    make_flac(outside / "01 Song.flac")
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    assert discovery.find_album_folders([alias / "01 Song.flac"], base_dir=tmp_path) == []


def test_short_local_plain_rejects_wrong_remote_timing() -> None:
    assert not lyrics._sync_text_matches_plain("[00:01.00]Goodbye world", "Hello world")


def test_invalid_embedded_word_timing_is_not_claimed(tmp_path: Path) -> None:
    value = "[00:01.00]<00:09.00>Hello <00:02.00>world"
    result = lyrics.inspect_local(tmp_path / "song.flac", {"SYNCEDLYRICS": [value]}, 10)
    assert result is not None and not result.has_word_sync


def test_provider_interrupt_restores_thread_acquisition_state() -> None:
    state = lyrics_utils._LYRICS_PROVIDER_STATE
    previous = (getattr(state, "active", False), getattr(state, "failures", None))

    def interrupted(song: str, artist: str) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        lyrics_utils.invoke_lyrics_provider("Interrupt fixture", interrupted, "Song", "Artist")
    assert (state.active, state.failures) == previous


def test_dr_report_does_not_reassign_a_missing_disc_row(tmp_path: Path) -> None:
    import dr14_parse

    path = tmp_path / "Disc 2" / "01 Song.flac"
    rows = [{"name": "Disc 1/01 Song.flac", "dr": 12}]
    assert dr14_parse.resolve_to_files(rows, [path]) == []


def test_native_dr_report_reuses_canonical_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import dr14_parse

    folder = tmp_path / "Layout artist" / "Album (2000)"
    folder.mkdir(parents=True)
    track = make_flac(folder / "01 Song.flac")
    monkeypatch.setattr(dr14_parse, "search_dr_database", lambda *_args, **_kwargs: [])
    reports: list[str] = []
    tagger.measure_album_dr14(folder, [track], artist="Credited Artist", album="Album", report_sink=reports.append)
    (folder / "foo_dr.txt").write_text(reports[0])

    def no_remeasurement(*args: Any, **kwargs: Any) -> dict[str, Any]:
        pytest.fail("valid generated report should be reused")

    monkeypatch.setattr(dr14_parse, "measure_audio_file", no_remeasurement)
    values, album_dr, _ = tagger.measure_album_dr14(
        folder, [track], artist="Credited Artist", album="Album", report_sink=reports.append
    )
    assert values and album_dr


def test_private_pending_file_does_not_discover_album(tmp_path: Path) -> None:
    (tmp_path / ".song.pending.flac").write_bytes(b"disposable input")
    assert discovery.find_album_folders([tmp_path], base_dir=tmp_path.parent) == []


@pytest.mark.parametrize("text", ["Hi!", "Да!", "[Verse]\nOh!"])
def test_short_curated_plain_is_a_preservation_input(tmp_path: Path, text: str) -> None:
    track = tmp_path / "01 Song.flac"
    track.with_suffix(".txt").write_text(text, encoding="utf-8")
    result = lyrics.inspect_local(track, {}, 10)
    assert result is not None and result.local and result.paths["plain"] == track.with_suffix(".txt")


def test_structural_only_plain_is_not_lyrics(tmp_path: Path) -> None:
    track = tmp_path / "01 Song.flac"
    track.with_suffix(".txt").write_text("[Verse]\n[Chorus]\n[Instrumental]\n", encoding="utf-8")
    assert lyrics.inspect_local(track, {}, 10) is None


def test_embedded_enhanced_only_preserves_and_prevents_lookup(tmp_path: Path) -> None:
    value = "[00:01.000]<00:01.000>Hello <00:02.000>world\n[00:03.000]<00:03.000>Again"
    result = lyrics.inspect_local(tmp_path / "song.flac", {"SYNCEDLYRICS_ENHANCED": [value]}, 10)
    assert result is not None and result.local and result.has_word_sync and not result.needs_remote_lookup


def test_cue_disc_assignment_uses_directory_not_enumeration(tmp_path: Path) -> None:
    for disc in (2, 10):
        folder = tmp_path / f"Disc {disc}"
        folder.mkdir()
        make_flac(folder / "01 Song.flac", seconds=1)
        (folder / "album.cue").write_text('FILE "01 Song.flac" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n')
    result = toc.build_all(tmp_path)
    assert set(result) == {2, 10}
    assert Path(result[2]["cue"]).parent.name == "Disc 2"


def test_nested_cue_can_establish_completeness_without_totals(tmp_path: Path) -> None:
    tracks: list[Path] = []
    for disc in (1, 2):
        folder = tmp_path / f"Disc {disc}"
        folder.mkdir()
        track = make_flac(folder / "01 Song.flac")
        audio = FLAC(track)
        audio["ALBUM"], audio["ALBUMARTIST"] = ["Album"], ["Artist"]
        audio.save()
        tracks.append(track)
        (folder / "album.CUE").write_text('FILE "01 Song.flac" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n')
    assert album_completeness.assess_album_completeness(tracks, tmp_path).state == "COMPLETE"


def test_corrupt_flac_yields_unknown_completeness(tmp_path: Path) -> None:
    track = tmp_path / "01 Bad.flac"
    track.write_bytes(b"corrupt")
    assert album_completeness.assess_album_completeness([track], tmp_path).state == "UNKNOWN"


def test_track_does_not_borrow_another_medium_discid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    disc = tmp_path / "Disc 2"
    disc.mkdir()
    track = make_flac(disc / "01 Song.flac")
    audio = FLAC(track)
    audio["TITLE"], audio["ARTIST"] = ["Song"], ["Artist"]
    audio["SYNCEDLYRICS"] = ["[00:00.00]Hello world"]
    audio.save()
    first_disc_log = tmp_path / "Disc 1.log"
    first_disc_log.write_text("Track 1\n Test CRC DEADBEEF\n Copy CRC DEADBEEF\n")
    captured: list[tuple[str, str]] = []

    def writer(_path: Path, tags: list[tuple[str, str]], **kwargs: Any) -> metadata_writer.WriteReport:
        captured.extend(tags)
        return metadata_writer.WriteReport("fixture", (), "")

    monkeypatch.setattr(metadata_writer, "write_tags", writer)
    monkeypatch.setattr(tagger, "compute_acoustid_fingerprint", lambda *_: (0, ""))
    monkeypatch.setattr(tagger, "compute_bpm_from_pcm", lambda *_: "")
    monkeypatch.setattr(tagger, "get_track_eac_peak", lambda *_args, **_kwargs: "")
    ok, message = tagger.process_single_track(
        track,
        tmp_path,
        "Artist",
        "Album",
        {},
        None,
        [first_disc_log],
        None,
        "",
        {
            "total_discs": "2",
            "orig_date": "1980-03-04",
            "freedb_discid": "first-disc-freedb-id",
            "rip_toc_source": "EAC+CUE",
            "rip_toc_tracks": "99",
            "mb_discid": "first-disc-physical-id",
            "mb_discid_source": "EAC+CUE",
            "mb_tocs": {
                1: {"toc_exact": True, "discid": "first-disc-physical-id"},
                2: {"toc_exact": False, "source": "FLAC-DIR", "freedb_discid": "01234567", "track_count": 1},
            },
            "flac_files": [track],
        },
        tagger.requests.Session(),
        "fixture",
        tmp_path / "cache",
        None,
        dry_run=True,
    )
    assert ok, message
    assert not any(field == "MUSICBRAINZ_DISCID" for field, _ in captured)
    assert ("ORIGINALDATE", "1980-03-04") in captured
    assert not any(field in {"DATE", "YEAR"} for field, _ in captured)
    assert not any(field in {"TEST_CRC", "COPY_CRC"} for field, _ in captured)
    assert ("FREEDB_DISCID", "01234567") in captured
    assert ("RIP_TOC_TRACKS", "1") in captured


def test_vocadb_canonical_names_and_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def response(request: Any, **kwargs: Any) -> io.BytesIO:
        calls.append(request.full_url)
        item = {
            "id": 123,
            "name": "別名",
            "names": [{"language": "English", "value": "Song"}],
            "artists": [{"artist": {"name": "Artist", "id": 7}}],
            "lyrics": [{"translationType": "Original", "value": "A complete original lyric"}],
        }
        if len(calls) == 1:
            item["name"] = "Unrelated"
            item["names"] = []
        return io.BytesIO(json.dumps({"items": [item]}).encode())

    monkeypatch.setattr(lyrics_utils, "_provider_urlopen", response)
    result = lyrics_utils.fetch_vocadb("Song", "Artist")
    assert result and result.get("found") and result["trackName"] == "Song"
    assert result["plainLyrics"] == "A complete original lyric"
    assert len(calls) == 2


def test_ttml_millisecond_times_and_untimed_prefix_survive() -> None:
    source = '<tt><body><div><p begin="1234ms">Oh, <span begin="1500ms">hello </span><span begin="2000ms">world!</span></p></div></body></tt>'
    plain, synced, word, _ = lyrics_utils.parse_ttml(source)
    assert plain == "Oh, hello world!"
    assert "[00:01.234]Oh, <00:01.500>hello <00:02.000>world!" in synced
    assert word


def test_nonmonotonic_image_cue_cannot_produce_exact_discid(tmp_path: Path) -> None:
    make_flac(tmp_path / "image.flac", seconds=4)
    cue = tmp_path / "album.cue"
    cue.write_text(
        'FILE "image.flac" WAVE\n TRACK 01 AUDIO\n INDEX 01 00:00:00\n TRACK 02 AUDIO\n INDEX 01 00:03:00\n TRACK 03 AUDIO\n INDEX 01 00:02:00\n'
    )
    with pytest.raises(ValueError, match="(?i)(offset|index|order)"):
        toc.build(cue)


def test_short_embedded_plain_is_materialized(tmp_path: Path) -> None:
    base = tmp_path / "staging" / "Song.flac"
    base.parent.mkdir()
    result = lyrics.materialize_embedded(base, {"LYRICS": ["Hi!"]}, existing_base=tmp_path / base.name, duration=10)
    assert result["plain"].read_text() == "Hi!"


def test_discogs_search_keeps_later_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRIPT_DIR", str(tmp_path))
    finder = discovery.DiscogsFinder(tmp_path, artist="Artist", album="Album")
    calls: list[str] = []

    def response(url: str) -> dict[str, Any]:
        calls.append(url)
        page = 2 if "page=2" in url else 1
        return {"pagination": {"pages": 2}, "results": [{"id": page * 100 + n} for n in range(20)]}

    monkeypatch.setattr(finder, "discogs_api_get", response)
    result = finder.search_candidates({"catnos": ["ABC123"]})
    assert "219" in result and len(result) == 40
    assert len(calls) == 2


def test_discogs_missing_response_id_is_not_exact(tmp_path: Path) -> None:
    fetcher = covers.CoverFetcher(
        "Artist",
        "Album",
        discogs_id="123",
        cache_dir=tmp_path,
        discogs_json_str=json.dumps({"images": [{"uri": "https://provider.invalid/front.jpg", "type": "primary"}]}),
    )
    result = fetcher.query_discogs()
    assert len(result) == 1 and result[0].release_tier != covers.ReleaseTier.EXACT_RELEASE


def test_tidal_requires_configured_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TIDAL_TOKEN", raising=False)
    fetcher = covers.CoverFetcher("Artist", "Album", cache_dir=tmp_path)
    monkeypatch.setattr(fetcher, "fetch_url", lambda *_args, **_kwargs: pytest.fail("unconfigured request"))
    assert fetcher.query_tidal() == []
    assert "not_configured" in fetcher.last_fetch_errors.values()


def test_artwork_error_envelopes_are_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    class Session:
        def get(self, *args: Any, **kwargs: Any) -> ImageReply:
            nonlocal calls
            calls += 1
            reply = ImageReply()
            if calls == 1:
                reply.headers["Content-Type"] = "application/json"
                monkeypatch.setattr(reply, "iter_content", lambda chunk_size: [b'{"error":"temporarily unavailable"}'])
            return reply

    fetcher = covers.CoverFetcher("Artist", "Album", cache_dir=tmp_path)
    fetcher.session = Session()
    url = "https://provider.invalid/art"
    assert fetcher.fetch_url(url) is None
    assert fetcher.last_fetch_errors[url] == "transient_error"
    assert fetcher.fetch_url(url) == image_bytes()
    assert calls == 2


def test_lyricsfile_ttml_round_trip_preserves_source_timing() -> None:
    from lyricsfile import parse_lyricsfile

    source = """version: "1.0"
metadata:
  title: Song
  artist: Artist
lines:
  - text: Hello world
    start_ms: 1234
    end_ms: 3000
    words:
      - {text: "Hello ", start_ms: 1234, end_ms: 2000}
      - {text: world, start_ms: 2000, end_ms: 3000}
"""
    result = parse_lyricsfile(source, title="Song", artist="Artist", duration=10)
    plain, synced, word, _ = lyrics_utils.parse_ttml(result["ttmlLyrics"])
    assert plain == result["plainLyrics"]
    assert synced == result["enhancedLyrics"]
    assert word


def test_embedded_enhanced_is_archived_and_derived(tmp_path: Path) -> None:
    value = "[00:01.234]<00:01.234>Hello <00:02.000>world!"
    base = tmp_path / "staged" / "Song.flac"
    base.parent.mkdir()
    result = lyrics.materialize_embedded(
        base, {"SYNCEDLYRICS_ENHANCED": [value]}, existing_base=tmp_path / base.name, duration=10
    )
    assert result["enhanced"].read_text() == value
    assert result["synced"].read_text() == "[00:01.234]Hello world!"
    assert result["plain"].read_text() == "Hello world!"


def test_rip_evidence_cannot_cross_discs(tmp_path: Path) -> None:
    track = make_flac(tmp_path / "2-01 Song.flac")
    first = tmp_path / "Disc 1.cue"
    second = tmp_path / "Disc 2.cue"
    first.write_text('FILE "1-01 Song.flac" WAVE\n TRACK 01 AUDIO\n INDEX 01 00:00:00\n')
    second.write_text('FILE "2-01 Song.flac" WAVE\n TRACK 01 AUDIO\n INDEX 01 00:00:00\n')
    unrelated_log = tmp_path / "album.log"
    unrelated_log.write_text("Track 1\nTest CRC DEADBEEF\n")
    result = tagger.rip_evidence_for_disc([first, unrelated_log, second], tmp_path, 2, {}, 2)
    assert result == [second]
    assert FLAC(track).info.total_samples > 0


def test_discogs_local_track_evidence_uses_positions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRIPT_DIR", str(tmp_path))
    for number in (1, 10, 2):
        make_flac(tmp_path / f"{number} Track {number}.flac", seconds=number)
    finder = discovery.DiscogsFinder(tmp_path, artist="Artist", album="Album")
    result = finder.extract_local_signatures()
    assert result["titles"] == ["Track 1", "Track 2", "Track 10"]
    assert result["durations"] == [1.0, 2.0, 10.0]


def test_discogs_local_signatures_find_nested_uppercase_cue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRIPT_DIR", str(tmp_path))
    disc = tmp_path / "Disc 2"
    disc.mkdir()
    (disc / "album.CUE").write_text('REM CATALOGNUMBER "ABC123"\n')
    finder = discovery.DiscogsFinder(tmp_path, artist="Artist", album="Album")
    assert "ABC123" in finder.extract_local_signatures()["catnos"]


def test_cue_does_not_follow_external_audio_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    real = make_flac(outside / "audio.flac", seconds=4)
    album = tmp_path / "album"
    album.mkdir()
    (album / "image.flac").symlink_to(real)
    cue = album / "album.cue"
    cue.write_text('FILE "image.flac" WAVE\n TRACK 01 AUDIO\n INDEX 01 00:00:00\n')
    with pytest.raises(ValueError, match="(?i)(symlink|outside)"):
        toc.build(cue)


def test_oversized_local_cover_is_not_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "cover.png").write_bytes(b"too much input")
    monkeypatch.setattr(covers, "MAX_IMAGE_BYTES", 4)
    monkeypatch.setattr(Path, "read_bytes", lambda *_args: pytest.fail("unbounded oversized cover read"))
    fetcher = covers.CoverFetcher("Artist", "Album", folder=tmp_path, cache_dir=tmp_path / "cache")
    assert fetcher.query_local() == []


def test_dr_source_tracks_actual_remeasurement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import dr14_parse

    track = make_flac(tmp_path / "01 Song.flac")
    (tmp_path / "foo_dr.txt").write_text("Analyzed: Other artist / Other album\n")
    monkeypatch.setattr(dr14_parse, "search_dr_database", lambda *_args, **_kwargs: [])
    values, album_dr, info = tagger.measure_album_dr14(
        tmp_path, [track], artist="Artist", album="Album", report_sink=lambda _report: None
    )
    assert values and album_dr
    assert info["measurement_source"] == dr14_parse.DR_ALGORITHM_VERSION


def test_diagnostics_help_never_calls_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["check_all_providers.py", "--help"])
    monkeypatch.setattr(
        lyrics_utils, "run_providers_diagnostics", lambda **_kwargs: pytest.fail("help caused network diagnostics")
    )
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(str(Path(__file__).with_name("check_all_providers.py")), run_name="__main__")
    assert caught.value.code == 0


@pytest.mark.parametrize("module", [tagger, covers])
def test_invalid_preview_environment_is_a_cli_error(module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COVER_PREVIEW_SIZE", "malformed")
    monkeypatch.setattr(sys, "argv", [module.__file__])
    with pytest.raises(SystemExit) as caught:
        module.main()
    assert caught.value.code == 2


@pytest.mark.parametrize("name", ["mb_identity_score", "mb_discid_from_cue", "dr14_parse"])
def test_positional_helper_cli_help(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    module = __import__(name)
    monkeypatch.setattr(sys, "argv", [name + ".py", "--help"])
    assert module.main() in (0, None)


def test_partial_provider_diagnostics_prevent_success_cache() -> None:
    calls = 0

    def provider(song: str, artist: str) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return lyrics_utils._collected_lyrics_result(
            [{"found": True, "plainLyrics": "Hello world", "trackName": song, "artistName": artist}],
            [("TRANSIENT_ERROR", "independent detail request timed out")],
            source="Partial fixture",
        )

    for _ in range(2):
        assert lyrics_utils.invoke_lyrics_provider("Partial fixture", provider, "Song", "Artist")["found"]
    assert calls == 2


def test_unison_filters_relevance_before_detail_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def response(request: Any, **kwargs: Any) -> io.BytesIO:
        calls.append(request.full_url)
        if "/search?" in request.full_url:
            rows = [{"song": "Wrong", "artist": "Other", "id": number} for number in range(20)]
            rows.append({"song": "Song", "artist": "Artist", "id": 21})
            body: dict[str, Any] = {"data": rows}
        elif request.full_url.endswith("/21"):
            body = {
                "success": True,
                "data": {
                    "song": "Song",
                    "artist": "Artist",
                    "lyrics": "Hello world, these are complete original lyrics",
                    "format": "plain",
                },
            }
        else:
            body = {"success": False}
        return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr(lyrics_utils, "_provider_urlopen", response)
    result = lyrics_utils.fetch_apple_music_ttml("Song", "Artist")
    assert result and result.get("found")
    assert len(calls) == 2 and calls[1].endswith("/21")


def test_enhanced_lyrics_worker_preserves_pcm_and_other_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track = make_flac(tmp_path / "01 Song.flac", seconds=10)
    value = "[00:01.234]<00:01.234>Hello <00:02.000>world, here is our original song!\n[00:03.000]<00:03.000>We <00:04.000>sing this line again."
    audio = FLAC(track)
    audio["TITLE"], audio["ARTIST"] = ["Song"], ["Artist"]
    audio["CURATED_MULTI"] = ["one", "два"]
    audio["SYNCEDLYRICS_ENHANCED"] = [value]
    picture = Picture()
    picture.type, picture.mime = 4, "image/png"
    picture.data = image_bytes()
    audio.add_picture(picture)
    audio.save()
    before_pcm = metadata_writer.pcm_digest(track)
    monkeypatch.setattr(tagger, "compute_acoustid_fingerprint", lambda *_: (0, ""))
    monkeypatch.setattr(tagger, "compute_bpm_from_pcm", lambda *_: "")
    monkeypatch.setattr(tagger, "get_track_eac_peak", lambda *_args, **_kwargs: "")
    ok, message = tagger.process_single_track(
        track,
        tmp_path,
        "Artist",
        "Album",
        {},
        None,
        [],
        None,
        "",
        {"flac_files": [track]},
        tagger.requests.Session(),
        "fixture",
        tmp_path / "cache",
        None,
    )
    assert ok, message
    assert metadata_writer.pcm_digest(track) == before_pcm
    audio = FLAC(track)
    assert audio["CURATED_MULTI"] == ["one", "два"]
    assert [(p.type, p.data) for p in audio.pictures] == [(4, picture.data)]
    assert audio["UNSYNCEDLYRICS"] == [lyrics_utils.to_plain(value)]
    assert audio["LYRICS"] == [lyrics.enhanced_to_lrc(value)]
    assert audio["SYNCEDLYRICS"] == [lyrics.enhanced_to_lrc(value)]
    assert track.with_suffix(".elrc").read_text() == value
