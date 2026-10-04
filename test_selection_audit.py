"""Acquisition-to-materialization regressions using offline provider boundaries."""

from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any, Self

import pytest
from PIL import Image

import audiophile_cover_fetcher as covers
import lyrics_pipeline as pipeline
import lyrics_utils as utils
from test_audiophile_pipeline import disable_lyrics_network

IDENTITY: dict[str, Any] = {
    "title": "Fixture Song",
    "artist": "Fixture Artist",
    "album": "Fixture Album",
    "duration": 120.0,
}
TEXT = "A first complete line with meaningful words\nA second complete line with more words\nA third complete line retained forever\nA fourth line extends the complete transcription\nA fifth line preserves all the original verses\nA sixth line ends the song with a final refrain and the remaining original words\nThe seventh verse continues with the exact text of this fixture recording\nThe eighth verse keeps punctuation, Unicode café, and the final repeated words\nThe ninth verse completes the transcription with every remaining sung line"


def result(provider: str = "Fixture", **overrides: Any) -> dict[str, Any]:
    return dict(
        found=True,
        source=provider,
        trackName="Fixture Song",
        artistName="Fixture Artist",
        albumName="Fixture Album",
        duration=120.0,
        plainLyrics=TEXT,
        **overrides,
    )


class Response:
    status = 200

    def __init__(self, payload: Any) -> None:
        self.body = json.dumps(payload).encode()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def read(self, _limit: int = -1) -> bytes:
        return self.body


def image() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (600, 600), "red").save(output, format="PNG")
    return output.getvalue()


def test_actual_unison_cascade_reaches_provider_and_keeps_later_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = utils.fetch_apple_music_ttml
    disable_lyrics_network(monkeypatch)
    monkeypatch.setattr(utils, "fetch_apple_music_ttml", adapter)
    requests: list[str] = []

    def open_fixture(request: Any, **_kwargs: Any) -> Response:
        url = request.full_url
        requests.append(url)
        if "/search?" in url:
            return Response(
                {
                    "data": [
                        {"id": name, "song": "Fixture Song", "artist": "Fixture Artist"}
                        for name in ("broken", "short", "complete")
                    ]
                }
            )
        if url.endswith("/broken"):
            raise urllib.error.URLError("temporary unavailable")
        text = "A short fragment" if url.endswith("/short") else TEXT
        return Response(
            {
                "success": True,
                "data": {"id": url.rsplit("/", 1)[-1], "lyrics": text, "album": "Fixture Album", "duration": 120},
            }
        )

    monkeypatch.setattr(utils, "_provider_urlopen", open_fixture)
    acquired = utils.fetch_lyrics_cascade(
        "Fixture Song",
        "Fixture Artist",
        album="Fixture Album",
        duration=120,
        enable_neural=False,
        isrc="USABC1200001",
        recording_id="recording-1",
    )
    chosen = pipeline.choose_provider_result([acquired], **IDENTITY)
    assert requests and chosen is not None
    assert chosen.candidate.provider_track_id == "complete"
    materialized = pipeline.materialize_selected(tmp_path / "track.flac", chosen)
    assert materialized.paths["plain"].read_text() == TEXT


def test_lrclib_cascade_keeps_complete_plain_beside_partial_timing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disable_lyrics_network(monkeypatch)
    partial = "[00:01.00]A first complete line with meaningful words"
    monkeypatch.setattr(utils, "fetch_lrclib", lambda *_args, **_kwargs: result("LRCLIB", syncedLyrics=partial))
    acquired = utils.fetch_lyrics_cascade(
        "Fixture Song", "Fixture Artist", album="Fixture Album", duration=120, enable_neural=False
    )
    assert acquired is not None and acquired["plainLyrics"] == TEXT
    chosen = pipeline.choose_provider_result([acquired], **IDENTITY)
    assert chosen is not None and not chosen.candidate.synced, acquired
    resolution = pipeline.materialize_selected(tmp_path / "track.flac", chosen)
    assert resolution.paths["plain"].read_text() == TEXT
    assert "synced" not in resolution.paths


@pytest.mark.parametrize("bad", [b"not an image", b"\x89PNG\r\n\x1a\ncorrupt"])
def test_decode_failure_tries_next_representation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: bytes) -> None:
    fetcher = covers.CoverFetcher("Artist", "Album", cache_dir=tmp_path)
    original, thumb = "https://fixture.invalid/original", "https://fixture.invalid/thumbnail"
    candidate = covers.CoverCandidate(
        "Cover Art Archive (Release)",
        original,
        "Album",
        1,
        preview_urls={"500": thumb},
        representation_kind=covers.RepresentationKind.ORIGINAL,
        source_verified_original=True,
    )
    monkeypatch.setattr(fetcher, "fetch_url", lambda url, **_kwargs: bad if url == original else image())
    evaluated = fetcher.evaluate_candidate(candidate)
    assert not evaluated.error
    assert evaluated.raw_data == image()
    assert evaluated.evaluated_url == thumb
    assert evaluated.representation_kind == covers.RepresentationKind.THUMBNAIL
    assert not evaluated.source_verified_original
    assert evaluated.warnings


def test_single_legacy_menu_category_preserves_automatic_winner(tmp_path: Path) -> None:
    digital = covers.CoverCandidate("Apple Music", "digital", "Album", 1, raw_data=image())
    local = covers.CoverCandidate("Local Existing File", "local", "Album", 1, raw_data=image(), identity_scope="local")
    assert covers.prompt_cover_choice(digital, None, local, tmp_path / "cover.jpg", tmp_path) is local


def test_materialization_tries_next_eligible_lyrics_and_discards_failed_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidates = [result("Z winner"), result("A fallback")]
    original = pipeline.materialize_selected

    def fail_selected(base: Path, assessment: Any, **kwargs: Any) -> Any:
        if assessment.candidate.provider == "Z winner":
            base.with_suffix(".txt").write_text("partial failed output")
            raise ValueError("conversion failed")
        return original(base, assessment, **kwargs)

    monkeypatch.setattr(pipeline, "materialize_selected", fail_selected)
    resolution = pipeline.materialize_provider_results(tmp_path / "track.flac", candidates, **IDENTITY)
    assert resolution is not None and resolution.source == "A fallback"
    assert resolution.paths["plain"].read_text() == TEXT
    assert all("partial failed output" not in path.read_text() for path in tmp_path.iterdir() if path.is_file())


@pytest.mark.parametrize("provider", ["Spotify", "Deezer"])
def test_bad_sync_and_one_failed_track_preserve_plain_from_other_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    monkeypatch.setenv("DEEZER_ARL", "fixture-session")
    monkeypatch.setattr(utils, "get_spotify_access_token", lambda: "fixture-token")
    requests: list[str] = []

    def open_fixture(request: Any, **_kwargs: Any) -> Response:
        url = request.full_url
        requests.append(url)
        if "api.spotify.com/v1/search" in url:
            return Response(
                {
                    "tracks": {
                        "items": [
                            {
                                "id": key,
                                "name": "Fixture Song",
                                "artists": [{"name": "Fixture Artist"}],
                                "album": {"name": "Other compilation"},
                                "duration_ms": 120000,
                            }
                            for key in ("a", "b")
                        ]
                    }
                }
            )
        if "api.deezer.com/search" in url:
            return Response(
                {
                    "data": [
                        {
                            "id": key,
                            "title": "Fixture Song",
                            "artist": {"name": "Fixture Artist"},
                            "album": {"title": "Other compilation"},
                            "duration": 120,
                        }
                        for key in ("a", "b")
                    ]
                }
            )
        if "auth.deezer.com" in url:
            return Response({"jwt": "fixture-jwt"})
        if "spclient" in url:
            if "/a?" in url:
                raise urllib.error.URLError("one track unavailable")
            return Response(
                {
                    "lyrics": {
                        "syncType": "LINE_SYNCED",
                        "lines": [{"words": line, "startTimeMs": "invalid"} for line in TEXT.splitlines()],
                    }
                }
            )
        payload = json.loads(request.data)
        key = payload["variables"]["trackId"]
        if key == "a":
            raise urllib.error.URLError("one track unavailable")
        return Response(
            {
                "data": {
                    "track": {
                        "id": key,
                        "lyrics": {"text": TEXT, "synchronizedLines": [{"line": "A line", "milliseconds": "invalid"}]},
                    }
                }
            }
        )

    monkeypatch.setattr(utils, "_provider_urlopen", open_fixture)
    adapter = utils.fetch_spotify if provider == "Spotify" else utils.fetch_deezer_lyrics
    acquired = adapter("Fixture Song", "Fixture Artist", album="Fixture Album", duration=120)
    chosen = pipeline.choose_provider_result([acquired], **IDENTITY)
    assert chosen is not None and not chosen.candidate.synced, acquired
    assert chosen.candidate.plain_lyrics == TEXT
    resolution = pipeline.materialize_selected(tmp_path / "track.flac", chosen)
    assert resolution.paths["plain"].read_text() == TEXT
    assert resolution.paths["raw"].read_text()
    assert requests and chosen.candidate.warnings


def test_preparation_failure_uses_next_cover_without_touching_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = covers.CoverFetcher("Artist", "Album", cache_dir=tmp_path / "cache")
    candidates = [
        fetcher.evaluate_candidate(covers.CoverCandidate(name, name, "Album", 1, raw_data=image()))
        for name in ("winner", "fallback")
    ]
    destination = tmp_path / "cover.jpg"
    destination.write_bytes(b"curated content")
    original = covers.save_optimized_cover
    calls = 0

    def fail_once(data: bytes, path: Path, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            path.write_bytes(b"partial")
            raise ValueError("conversion failed")
        return original(data, path, **kwargs)

    monkeypatch.setattr(covers, "save_optimized_cover", fail_once)
    prepared = covers.prepare_best_cover(fetcher, candidates, tmp_path / "staged.jpg", preferred=candidates[0])
    assert prepared is not None and prepared.candidate is candidates[1]
    assert destination.read_bytes() == b"curated content"
    assert not list(tmp_path.glob(".cover-materialize-*"))


def test_very_short_sync_cannot_override_complete_provider_plain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Comparison must also detect a very short timed fragment beside long plain text.
    remote = result(syncedLyrics="[00:01.00]Wrong words")
    chosen = pipeline.choose_provider_result([remote], **IDENTITY)
    assert chosen is not None and not chosen.candidate.synced
    materialized = pipeline.materialize_selected(tmp_path / "track.flac", chosen)
    assert "synced" not in materialized.paths


def test_thumbnail_fallback_uses_largest_accessible_representation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = covers.CoverFetcher("Artist", "Album", cache_dir=tmp_path)
    candidate = covers.CoverCandidate(
        "CAA", "original", "Album", 1, preview_urls={"250": "small", "500": "medium", "1200": "large"}
    )
    fetched: list[str] = []

    def fetch(url: str, **_kwargs: Any) -> bytes | None:
        fetched.append(url)
        return None if url == "original" else image()

    monkeypatch.setattr(fetcher, "fetch_url", fetch)
    fetcher.evaluate_candidate(candidate)
    assert candidate.evaluated_url == "large"
    assert fetched == ["original", "large"]


@pytest.mark.parametrize(
    "method,payload,supplied",
    [
        (
            "query_itunes",
            {
                "results": [
                    {
                        "artistName": "Artist",
                        "collectionName": "Album",
                        "collectionId": 1,
                        "artworkUrl100": "https://cdn.invalid/100x100bb.jpg?signature=keep",
                    }
                ]
            },
            "https://cdn.invalid/100x100bb.jpg?signature=keep",
        ),
        (
            "query_deezer",
            {
                "data": [
                    {
                        "artist": {"name": "Artist"},
                        "title": "Album",
                        "id": 1,
                        "cover_xl": "https://cdn.invalid/1000x1000-000000-80-0-0.jpg",
                    }
                ]
            },
            "https://cdn.invalid/1000x1000-000000-80-0-0.jpg",
        ),
        (
            "query_bandcamp",
            {"results": [{"band_name": "Artist", "name": "Album", "img": "https://cdn.invalid/a123_7.jpg"}]},
            "https://cdn.invalid/a123_7.jpg",
        ),
    ],
)
def test_provider_supplied_urls_are_used_without_inventing_originals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, payload: Any, supplied: str
) -> None:
    fetcher = covers.CoverFetcher("Artist", "Album", cache_dir=tmp_path)
    monkeypatch.setattr(fetcher, "fetch_url", lambda *_args, **_kwargs: json.dumps(payload).encode())
    candidates = getattr(fetcher, method)()
    assert candidates and candidates[0].url == supplied
    assert not candidates[0].source_verified_original


def test_metadata_only_ttml_does_not_block_remote_completion(tmp_path: Path) -> None:
    base = tmp_path / "track.flac"
    base.with_suffix(".ttml").write_text(
        "<tt><head><metadata>Archive note with no sung text</metadata></head><body/></tt>"
    )
    local = pipeline.inspect_local(base, None, 120)
    assert local is None or local.needs_remote_lookup


def test_tekstowo_later_complete_candidate_survives_first_download_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[str] = []
    search = (
        '<div class="box-przeboje">'
        + "".join(
            f'<a class="title" href="/fixture/{key}.html">Fixture Artist - Fixture Song</a>'
            for key in ("broken", "short", "complete")
        )
        + "</div>"
    )

    class HtmlResponse(Response):
        def __init__(self, value: str) -> None:
            self.body = value.encode()

    def open_fixture(request: Any, **_kwargs: Any) -> HtmlResponse:
        url = request.full_url
        requests.append(url)
        if "szukaj" in url:
            return HtmlResponse(search)
        if "broken" in url:
            raise urllib.error.URLError("temporary failure")
        text = "A short transcription with incomplete text" if "short" in url else TEXT
        return HtmlResponse('<div class="inner-text">' + text.replace("\n", "<br>") + "</div>")

    monkeypatch.setattr(utils, "_provider_urlopen", open_fixture)
    acquired = utils.invoke_lyrics_provider("Tekstowo", utils.fetch_tekstowo, "Fixture Song", "Fixture Artist")
    chosen = pipeline.choose_provider_result([acquired], **IDENTITY)
    assert chosen is not None and "complete.html" in chosen.candidate.provider_track_id
    assert len(requests) == 4
    assert pipeline.materialize_selected(tmp_path / "track.flac", chosen).paths["plain"].read_text() == TEXT


@pytest.mark.parametrize("reverse", [False, True])
def test_below_policy_winner_falls_back_to_eligible_plain(reverse: bool) -> None:
    candidates = [result("Generated", recording_id="recording-1", generated=True), result("Complete plain")]
    if reverse:
        candidates.reverse()
    chosen = pipeline.choose_provider_result(candidates, **IDENTITY, recording_id="recording-1")
    assert chosen is not None and chosen.candidate.provider == "Complete plain"


def test_search_identity_does_not_hide_conflicting_unison_detail_identity() -> None:
    payload = {
        "lyrics": TEXT,
        "song": "Fixture Song (Live)",
        "artist": "Wrong Artist",
        "album": "Fixture Album",
        "duration": 120,
        "isrc": "USABC1200002",
    }
    candidate = utils._unison_item_to_candidate(
        payload, "Fixture Song", "Fixture Artist", "Fixture Song", "Fixture Artist"
    )
    assert candidate is not None
    assert candidate["trackName"] == "Fixture Song (Live)" and candidate["artistName"] == "Wrong Artist"
    assert candidate["isrc"] == "USABC1200002"
    assert pipeline.choose_provider_result([candidate], **IDENTITY, isrc="USABC1200001") is None


def test_caa_search_keeps_later_artist_matched_group_when_first_has_no_art(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = covers.CoverFetcher("Fixture Artist", "Fixture Album", cache_dir=tmp_path)
    good = "11111111-1111-1111-1111-111111111111"
    later = "22222222-2222-2222-2222-222222222222"
    foreign = "33333333-3333-3333-3333-333333333333"
    queried: list[str] = []

    def fetch(url: str, **_kwargs: Any) -> bytes | None:
        queried.append(url)
        if "musicbrainz.org/ws/2" in url:
            return json.dumps(
                {
                    "release-groups": [
                        {
                            "id": value,
                            "title": "Fixture Album",
                            "primary-type": "Album",
                            "artist-credit": [{"name": artist}],
                        }
                        for value, artist in (
                            (foreign, "Foreign Artist"),
                            (good, "Fixture Artist"),
                            (later, "Fixture Artist"),
                        )
                    ]
                }
            ).encode()
        if good in url:
            return None
        return json.dumps({"images": [{"front": True, "image": "https://fixture.invalid/front.png"}]}).encode()

    monkeypatch.setattr(fetcher, "fetch_url", fetch)
    candidates = fetcher.query_cover_art_archive()
    assert len(candidates) == 1 and candidates[0].musicbrainz_release_group_id == later
    assert not any(foreign in url for url in queried)
    assert candidates[0].release_tier == covers.ReleaseTier.RELEASE_GROUP


@pytest.mark.parametrize("local", [False, True])
def test_tagger_materialization_fallback_reaches_real_writer_and_preserves_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local: bool
) -> None:
    import requests
    from mutagen.flac import FLAC

    import audiophile_tagger as tagger
    import metadata_writer
    from test_pipeline_safety import make_flac

    track = make_flac(tmp_path / "01 - Fixture Song.flac", seconds=1)
    audio = FLAC(track)
    audio["TITLE"] = ["Fixture Song"]
    audio["ARTIST"] = ["Fixture Artist"]
    audio["ALBUM"] = ["Fixture Album"]
    audio["TRACKNUMBER"] = ["1"]
    audio["DISCNUMBER"] = ["1"]
    audio.save()
    if local:
        track.with_suffix(".txt").write_text(TEXT)
    before = metadata_writer.pcm_digest(track)
    candidates = result("Z failed", alternatives=[result("A fallback")])
    candidates["duration"] = 1.0
    candidates["alternatives"][0]["duration"] = 1.0
    monkeypatch.setattr(utils, "fetch_betterlyrics", lambda **_kwargs: candidates)
    monkeypatch.setattr(tagger, "compute_acoustid_fingerprint", lambda _path: (0, ""))
    original = pipeline.materialize_selected

    def fail_first(base: Path, assessment: Any, **kwargs: Any) -> Any:
        if assessment.candidate.provider == "Z failed":
            base.with_suffix(".txt").write_text("failed partial transcription")
            raise ValueError("conversion failed")
        return original(base, assessment, **kwargs)

    monkeypatch.setattr(pipeline, "materialize_selected", fail_first)
    with requests.Session() as session:
        ok, message = tagger.process_single_track(
            track,
            tmp_path,
            "Fixture Artist",
            "Fixture Album",
            {},
            None,
            [],
            None,
            "",
            {"tracktotal": "1", "total_discs": "1"},
            session,
            "fixture",
            tmp_path / "mb",
            None,
        )
    assert ok, message
    tagged = FLAC(track)
    assert tagged["LYRICS"] == [TEXT]
    assert "failed partial transcription" not in str(dict(tagged))
    assert metadata_writer.pcm_digest(track) == before
    assert not list(tmp_path.glob(".lyrics-attempt-*"))


def test_curated_plain_cannot_be_completed_with_disagreeing_remote_timing(tmp_path: Path) -> None:
    remote = result(
        syncedLyrics="\n".join(f"[00:{index:02d}.00]{line}" for index, line in enumerate(TEXT.splitlines(), start=1))
    )
    curated = "Completely different preserved curated text with enough recording words\nAnother whole verse from the real local transcription"
    chosen = pipeline.choose_provider_result([remote], **IDENTITY, local_plain=curated)
    assert chosen is not None and not chosen.candidate.synced
    base = tmp_path / "track.flac"
    base.with_suffix(".txt").write_text(curated)
    resolution = pipeline.materialize_provider_results(base, [remote], **IDENTITY, local_plain=curated)
    assert resolution is not None and "synced" not in resolution.paths
    assert base.with_suffix(".txt").read_text() == curated
    assert (
        any("preserved local" in warning for warning in resolution.assessment.warnings)
        if resolution.assessment
        else False
    )


def test_genius_retains_later_matches_after_a_failed_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def open_fixture(request: Any, **_kwargs: Any) -> Response:
        if "/api/search/" in request.full_url:
            return Response(
                {
                    "response": {
                        "sections": [
                            {
                                "type": "song",
                                "hits": [
                                    {
                                        "type": "song",
                                        "result": {
                                            "id": name,
                                            "title": IDENTITY["title"],
                                            "primary_artist": {"name": IDENTITY["artist"]},
                                            "url": f"https://genius.com/{name}-lyrics",
                                        },
                                    }
                                    for name in ("failed", "fragment", "complete")
                                ],
                            }
                        ]
                    }
                }
            )
        if "failed" in request.full_url:
            raise urllib.error.HTTPError(request.full_url, 404, "missing", Message(), None)
        response = Response({})
        text = "A short fragment" if "fragment" in request.full_url else TEXT
        response.body = f'<div data-lyrics-container="true">{text.replace(chr(10), "<br/>")}</div>'.encode()
        return response

    monkeypatch.setattr(utils, "_provider_urlopen", open_fixture)
    acquired = utils.fetch_genius("Fixture Song", "Fixture Artist", return_candidate=True)
    assert isinstance(acquired, dict)
    assert len(acquired["alternatives"]) == 1
    assessment = pipeline.choose_provider_result([acquired], **IDENTITY)
    assert assessment is not None
    assert assessment.candidate.provider_track_id == "complete"
    assert "<div data-lyrics-container=" in assessment.candidate.raw_lyrics
    materialized = pipeline.materialize_selected(tmp_path / "track.flac", assessment)
    assert "plain" in materialized.paths
    assert "ninth verse" in materialized.paths["plain"].read_text()


def test_collected_empty_search_is_not_found() -> None:
    assert utils._collected_lyrics_result([], [], source="Fixture")["acquisition_status"] == "NOT_FOUND"


def test_musixmatch_failed_first_track_and_partial_richsync_keep_complete_plain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("MUSIXMATCH_API_KEY", "fixture-key")
    monkeypatch.delenv("MUSIXMATCH_TOKEN", raising=False)

    def payload(body: dict[str, Any]) -> Response:
        return Response({"message": {"header": {"status_code": 200}, "body": body}})

    def open_fixture(request: Any, **_kwargs: Any) -> Response:
        url = request.full_url
        if "track.search?" in url:
            return payload(
                {
                    "track_list": [
                        {
                            "track": {
                                "track_id": number,
                                "track_name": "Fixture Song",
                                "artist_name": "Fixture Artist",
                                "album_name": "Fixture Album",
                                "track_length": 120,
                                "has_lyrics": 1,
                                "has_subtitles": 1,
                                "has_richsync": 1,
                            }
                        }
                        for number in (1, 2)
                    ]
                }
            )
        if "track_id=1" in url:
            raise urllib.error.URLError("temporary failure of this recording")
        if "track.lyrics.get?" in url:
            return payload({"lyrics": {"lyrics_body": TEXT}})
        if "track.richsync.get?" in url:
            return payload(
                {
                    "richsync": {
                        "richsync_body": json.dumps([{"ts": 10, "te": 12, "l": [{"c": "Only a fragment", "o": 0}]}])
                    }
                }
            )
        return payload({"subtitle": {"subtitle_body": "[00:10.00]Only a fragment"}})

    monkeypatch.setattr(utils, "_provider_urlopen", open_fixture)
    acquired = utils.fetch_musixmatch("Fixture Song", "Fixture Artist", "Fixture Album", 120)
    assert acquired is not None and acquired["provider_track_id"] == "2"
    assert acquired["plainLyrics"] == TEXT
    assert acquired["providerMetadata"]["subtitle_raw"] == "[00:10.00]Only a fragment"
    chosen = pipeline.choose_provider_result([acquired], **IDENTITY)
    assert chosen is not None and not chosen.candidate.synced
    materialized = pipeline.materialize_selected(tmp_path / "track.flac", chosen)
    assert materialized.paths["plain"].read_text() == TEXT
    assert "Only a fragment" in materialized.paths["raw"].read_text()


def test_vk_topic_page_failure_keeps_later_candidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("VK_TOKEN", raising=False)

    def open_fixture(request: Any, **_kwargs: Any) -> Response:
        response = Response({})
        if "duckduckgo.com" in request.full_url:
            response.body = "".join(
                f'<a href="https://vk.com/topic-1_{number}">Fixture Artist - Fixture Song</a>' for number in range(1, 4)
            ).encode()
        elif request.full_url.endswith("_1"):
            raise urllib.error.URLError("one public topic unavailable")
        else:
            text = TEXT[:100] if request.full_url.endswith("_2") else TEXT
            response.body = f'<h1>Fixture Artist - Fixture Song</h1><div class="bp_text">{text}</div>'.encode()
        return response

    monkeypatch.setattr(utils, "_provider_urlopen", open_fixture)
    acquired = utils.fetch_vk_topics("Fixture Song", "Fixture Artist")
    assert acquired is not None
    assert len(acquired["alternatives"]) == 1
    chosen = pipeline.choose_provider_result([acquired], **IDENTITY)
    # Public-topic evidence is deliberately weak: verify completeness before
    # selection applies the configured minimum confidence threshold.
    candidates = [acquired, *acquired["alternatives"]]
    assert candidates[1]["plainLyrics"] == TEXT
    assert chosen is not None and chosen.candidate.plain_lyrics == TEXT


def test_source_html_size_does_not_use_the_extracted_lyrics_limit(tmp_path: Path) -> None:
    raw = "<html>" + ("x" * 1_000_010) + "</html>"
    chosen = pipeline.choose_provider_result([result(providerRaw=raw)], **IDENTITY)
    assert chosen is not None
    materialized = pipeline.materialize_selected(tmp_path / "track.flac", chosen)
    assert materialized.paths["raw"].read_text() == raw
    assert materialized.paths["plain"].read_text() == TEXT
    assert pipeline.choose_provider_result([result(providerRaw="x" * 5_000_001)], **IDENTITY) is None


def install_cover_boundaries(
    fetcher: covers.CoverFetcher, candidates: list[covers.CoverCandidate], monkeypatch: pytest.MonkeyPatch
) -> None:
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
    monkeypatch.setattr(fetcher, "query_itunes", lambda: candidates)


@pytest.mark.parametrize("reverse", [False, True])
def test_cover_url_dedup_retains_alternative_representations_deterministically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reverse: bool
) -> None:
    original = "https://fixture.invalid/original"
    large = "https://fixture.invalid/large"
    small = "https://fixture.invalid/small"
    candidates = [
        covers.CoverCandidate(
            "Cover Art Archive (Release)",
            original,
            "Album",
            1,
            preview_urls={size: url},
            release_tier=covers.ReleaseTier.EXACT_RELEASE,
            role_tier=covers.RoleTier.EXPLICIT_FRONT,
            release_key="mb:fixture",
        )
        for size, url in (("500", small), ("1200", large))
    ]
    if reverse:
        candidates.reverse()
    fetcher = covers.CoverFetcher("Artist", "Album", cache_dir=tmp_path)
    install_cover_boundaries(fetcher, candidates, monkeypatch)
    calls: list[str] = []

    def fetch(url: str, **_kwargs: Any) -> bytes:
        calls.append(url)
        return b"corrupt" if url == original else image()

    monkeypatch.setattr(fetcher, "fetch_url", fetch)
    winner, _, _, evaluated = fetcher.find_best_cover()
    assert winner is not None and winner.evaluated_url == large
    assert calls == [original, large]
    assert len(evaluated) == 1
    assert {rep.url for rep in winner.representations} == {original, large, small}


def test_cover_materialization_revalidates_deferred_tier_after_winner_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exact = covers.CoverCandidate(
        "Cover Art Archive (Release)",
        "https://fixture.invalid/exact",
        "Album",
        1,
        raw_data=image(),
        release_tier=covers.ReleaseTier.EXACT_RELEASE,
        role_tier=covers.RoleTier.EXPLICIT_FRONT,
        release_key="mb:fixture",
    )
    alternative = covers.CoverCandidate("Apple Music", "https://fixture.invalid/fallback", "Album", 1)
    fetcher = covers.CoverFetcher("Artist", "Album", cache_dir=tmp_path)
    install_cover_boundaries(fetcher, [exact, alternative], monkeypatch)
    downloads: list[str] = []

    def fetch(url: str, **_kwargs: Any) -> bytes:
        downloads.append(url)
        return image()

    monkeypatch.setattr(fetcher, "fetch_url", fetch)
    winner, _, _, candidates = fetcher.find_best_cover()
    assert winner is exact and not downloads
    assert alternative.rejection_reasons == ["not_probed_lower_release_tier"]
    original_save = covers.save_optimized_cover
    attempts = 0

    def fail_once(data: bytes, path: Path, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("selected derivative failed")
        return original_save(data, path, **kwargs)

    monkeypatch.setattr(covers, "save_optimized_cover", fail_once)
    prepared = covers.prepare_best_cover(fetcher, candidates, tmp_path / "cover.jpg", preferred=winner)
    assert prepared is not None and prepared.candidate is alternative
    assert downloads == [alternative.url]
    assert not alternative.rejection_reasons


def test_chorus_package_fallback_preserves_source_and_checks_embedded_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import struct

    events: list[str] = []
    for number, line in enumerate(TEXT.splitlines()):
        tick = 384 * (number + 1)
        events.extend((f'{tick} = E "phrase_start"', f'{tick} = E "lyric {line}"', f'{tick + 192} = E "phrase_end"'))
    chart = '[Song]\nName = "Fixture Song"\nArtist = "Fixture Artist"\nResolution = 192\n[Events]\n' + "\n".join(events)

    def package(text: str) -> bytes:
        raw = text.encode()
        name = b"notes.chart"
        offset = 26 + 16 + 16 + 1 + len(name) + 16
        return (
            b"SNGPKG"
            + bytes(20)
            + struct.pack("<QQQQ", 0, 0, 0, 1)
            + bytes([len(name)])
            + name
            + struct.pack("<QQ", len(raw), offset)
            + bytes(value ^ (position % 256) for position, value in enumerate(raw))
        )

    def open_fixture(request: Any, **_kwargs: Any) -> Response:
        if "/search" in request.full_url:
            return Response(
                {
                    "data": [
                        {
                            "md5": key,
                            "songName": "Fixture Song",
                            "artistName": "Fixture Artist",
                            "duration": 120,
                            "diff_vocals": 0,
                        }
                        for key in ("a", "b", "c")
                    ]
                }
            )
        if request.full_url.endswith("/a.sng"):
            raise urllib.error.URLError("one chart unavailable")
        response = Response({})
        source = (
            chart.replace('Name = "Fixture Song"', 'Name = "Wrong Recording"')
            if request.full_url.endswith("/b.sng")
            else chart
        )
        response.body = package(source)
        return response

    monkeypatch.setattr(utils, "_provider_urlopen", open_fixture)
    acquired = utils.fetch_chorus_chart("Fixture Song", "Fixture Artist", "Fixture Album", 120)
    assert acquired is not None and acquired["provider_track_id"] == "c"
    assert acquired["providerRaw"] == chart
    chosen = pipeline.choose_provider_result([acquired], **IDENTITY)
    assert chosen is not None and chosen.candidate.synced
    resolution = pipeline.materialize_selected(tmp_path / "track.flac", chosen)
    assert resolution.paths["raw"].read_text() == chart
    assert "ninth verse" in resolution.paths["plain"].read_text()
    assert "enhanced" in resolution.paths and "synced" in resolution.paths
    assert "ttml" not in resolution.paths


@pytest.mark.parametrize("begin,lookup", [("", True), (' begin="invalid"', True), (' begin="00:10.00"', False)])
def test_local_ttml_only_valid_timing_can_stop_lookup(tmp_path: Path, begin: str, lookup: bool) -> None:
    base = tmp_path / "track.flac"
    ttml = f'<tt xmlns="http://www.w3.org/ns/ttml"><body><p{begin}>{TEXT}</p></body></tt>'
    base.with_suffix(".ttml").write_text(ttml)
    local = pipeline.inspect_local(base, {}, 120)
    assert local is not None and local.local
    assert local.needs_remote_lookup is lookup
    assert local.paths["ttml"].read_text() == ttml
    base.with_suffix(".ttml").unlink()
    embedded = pipeline.inspect_local(base, {"LYRICS_TTML": [ttml]}, 120)
    assert embedded is not None and embedded.local
    assert embedded.needs_remote_lookup is lookup
