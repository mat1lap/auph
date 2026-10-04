"""Offline response fixtures for restored authorized lyrics acquisition routes."""

from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

import lyrics_utils

FIXTURES = Path(__file__).parent / "tests" / "fixtures" / "lyrics"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _mock_responses(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, bytes]) -> list[str]:
    urls: list[str] = []

    def fake_open(request: Any, **_kwargs: Any) -> io.BytesIO:
        url = request.full_url
        urls.append(url)
        for marker, response in mapping.items():
            if marker in url:
                return io.BytesIO(response)
        raise AssertionError(f"unplanned provider request: {url}")

    monkeypatch.setattr(lyrics_utils, "_provider_urlopen", fake_open)
    return urls


def test_spotify_authorized_line_sync_and_recording_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPOTIFY_ACCESS_TOKEN", "test-token")
    urls = _mock_responses(
        monkeypatch,
        {
            "/v1/search?": _fixture("spotify_search.json"),
            "/spotify-studio?": _fixture("spotify_lines.json"),
        },
    )
    result = lyrics_utils.fetch_spotify("Example Song", "Example Artist", "Example Album", 180)
    assert result["provider_track_id"] == "spotify-studio"
    assert result["isrc"] == "USAAA2400001"
    assert result["syncedLyrics"] == "[00:10.00]A first line\n[00:15.675]A second line"
    assert result["language"] == "en"
    assert not any("spotify-live" in url for url in urls)


def test_spotify_rejects_wrong_version_before_lyrics_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPOTIFY_ACCESS_TOKEN", "test-token")
    search = json.loads(_fixture("spotify_search.json"))
    search["tracks"]["items"] = search["tracks"]["items"][1:]
    urls = _mock_responses(monkeypatch, {"/v1/search?": json.dumps(search).encode()})
    result = lyrics_utils.fetch_spotify("Example Song", "Example Artist", "Example Album", 180)
    assert result["acquisition_status"] == "NOT_FOUND"
    assert len(urls) == 1


def test_spotify_changed_schema_is_parser_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPOTIFY_ACCESS_TOKEN", "test-token")
    _mock_responses(monkeypatch, {"/v1/search?": b'{"tracks":{}}'})
    result = lyrics_utils.fetch_spotify("Example Song", "Example Artist")
    assert result["acquisition_status"] == "PARSER_ERROR"


def test_spotify_rate_limit_is_not_a_no_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPOTIFY_ACCESS_TOKEN", "test-token")

    def rate_limited(*_args: Any, **_kwargs: Any) -> Any:
        raise urllib.error.HTTPError("https://api.spotify.com/v1/search", 429, "rate limited", Message(), None)

    monkeypatch.setattr(lyrics_utils, "_provider_urlopen", rate_limited)
    result = lyrics_utils.fetch_spotify("Example Song", "Example Artist")
    assert result["acquisition_status"] == "RATE_LIMITED"


def test_deezer_authorized_word_and_translation_forms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEZER_ARL", "test-arl")
    urls = _mock_responses(
        monkeypatch,
        {
            "/search/track?": _fixture("deezer_search.json"),
            "/login/renew?": b'{"jwt":"test-jwt"}',
            "pipe.deezer.com/api": _fixture("deezer_lyrics.json"),
        },
    )
    result = lyrics_utils.fetch_deezer_lyrics("Example Song", "Example Artist", "Example Album", 180)
    assert result["provider_track_id"] == "12345"
    assert result["has_word_sync"] is True
    assert "<00:10.50>first line" in result["enhancedLyrics"]
    assert result["translationLyrics"].startswith("Une première")
    assert len(urls) == 3


def test_deezer_missing_credentials_are_explicit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DEEZER_ARL", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    result = lyrics_utils.fetch_deezer_lyrics("Example Song", "Example Artist")
    assert result["acquisition_status"] == "NOT_CONFIGURED"


def test_musixmatch_unrelated_cached_search_is_not_negative_cachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MUSIXMATCH_API_KEY", raising=False)
    monkeypatch.setenv("MUSIXMATCH_TOKEN", "test-credential")
    payload = json.loads(_fixture("musixmatch_search.json"))
    track = payload["message"]["body"]["track_list"][0]["track"]
    track["track_name"] = "Unrelated Recording"
    track["artist_name"] = "Another Artist"
    _mock_responses(monkeypatch, {"/track.search?": json.dumps(payload).encode()})
    result = lyrics_utils.fetch_musixmatch("Example Song", "Example Artist")
    assert result is not None
    assert result["acquisition_status"] == "INVALID_RESPONSE"


def test_musixmatch_richsync_keeps_brackets_and_does_not_invent_word_ends() -> None:
    rows = [
        {"ts": 10.0, "l": [{"c": "(Hello) ", "o": 0.0}, {"c": "world", "o": 0.625}]},
        {"ts": 15.0, "l": [{"c": "[Chorus] Again", "o": 0.0}]},
    ]
    result = lyrics_utils.musixmatch_richsync_to_lyrics(rows, duration=180)
    assert result is not None
    assert result["plainLyrics"] == "(Hello) world\n[Chorus] Again"
    assert "<00:10.625>world" in result["enhancedLyrics"]
    assert result["ttmlLyrics"] == ""
    assert result["duration"] == 180
    assert lyrics_utils.musixmatch_richsync_to_lyrics(list(reversed(rows)), duration=180) is None


def test_musixmatch_subtitle_survives_plain_lyrics_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSIXMATCH_API_KEY", "test-key")
    search = json.loads(_fixture("musixmatch_search.json"))
    track = search["message"]["body"]["track_list"][0]["track"]
    track["has_lyrics"] = 0
    track["has_richsync"] = 0
    _mock_responses(
        monkeypatch,
        {
            "/track.search?": json.dumps(search).encode(),
            "/track.lyrics.get?": b'{"message":{"header":{"status_code":404},"body":{}}}',
            "/track.subtitle.get?": _fixture("musixmatch_subtitle.json"),
        },
    )
    result = lyrics_utils.fetch_musixmatch("Example Song", "Example Artist", "Example Album", 180)
    assert result is not None
    assert result["plainLyrics"] == "A first line\nA second line"
    assert result["syncedLyrics"].startswith("[00:10.00]")


@pytest.mark.parametrize("credential", ["MUSIXMATCH_API_KEY", "MUSIXMATCH_TOKEN"])
def test_musixmatch_documented_and_desktop_routes_retain_richsync(
    monkeypatch: pytest.MonkeyPatch,
    credential: str,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("MUSIXMATCH_API_KEY", raising=False)
    monkeypatch.delenv("MUSIXMATCH_TOKEN", raising=False)
    monkeypatch.setenv(credential, "test-credential")
    urls = _mock_responses(
        monkeypatch,
        {
            "/track.search?": _fixture("musixmatch_search.json"),
            "/track.lyrics.get?": _fixture("musixmatch_plain.json"),
            "/track.richsync.get?": _fixture("musixmatch_rich.json"),
            "/track.subtitle.get?": _fixture("musixmatch_subtitle.json"),
        },
    )
    result = lyrics_utils.fetch_musixmatch("Example Song", "Example Artist", "Example Album", 180)
    assert result is not None
    assert result["provider_track_id"] == "42"
    assert result["has_word_sync"] is True
    assert result["plainLyrics"] == "A first line\nA second line"
    assert result["enhancedLyrics"].startswith("[00:10.00]<00:10.00>A ")
    assert any("track.richsync.get" in url for url in urls)
    assert all(("apic-desktop" in url) == (credential == "MUSIXMATCH_TOKEN") for url in urls)


def test_musixmatch_dual_route_keeps_private_richsync_when_documented_is_plain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MUSIXMATCH_API_KEY", "test-api-key")
    monkeypatch.setenv("MUSIXMATCH_TOKEN", "test-desktop-token")
    documented_search = json.loads(_fixture("musixmatch_search.json"))
    documented_search["message"]["body"]["track_list"][0]["track"]["has_richsync"] = 0
    urls: list[str] = []

    def fake_open(request: Any, **_kwargs: Any) -> io.BytesIO:
        url = request.full_url
        urls.append(url)
        desktop = "apic-desktop" in url
        if "track.search?" in url:
            return io.BytesIO(_fixture("musixmatch_search.json") if desktop else json.dumps(documented_search).encode())
        if "track.lyrics.get?" in url:
            return io.BytesIO(_fixture("musixmatch_plain.json"))
        if "track.richsync.get?" in url and desktop:
            return io.BytesIO(_fixture("musixmatch_rich.json"))
        if "track.subtitle.get?" in url:
            return io.BytesIO(_fixture("musixmatch_subtitle.json"))
        raise AssertionError(f"unexpected request: {url}")

    monkeypatch.setattr(lyrics_utils, "_provider_urlopen", fake_open)
    result = lyrics_utils.fetch_musixmatch("Example Song", "Example Artist", "Example Album", 180)
    assert result is not None
    assert result["has_word_sync"] is True
    assert result["providerMetadata"]["acquisition_route"] == "configured_desktop"
    assert result["alternatives"][0]["providerMetadata"]["acquisition_route"] == "documented_api"
    assert any("api.musixmatch.com" in url for url in urls)
    assert any("apic-desktop.musixmatch.com" in url for url in urls)
