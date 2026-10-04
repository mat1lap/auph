from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import unicodedata
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any, ClassVar, Self

import pytest
from mutagen.flac import FLAC, Picture
from PIL import Image

import library_layout
import lyrics_pipeline
import mb_discid_from_cue
import metadata_writer
from library_files import iter_library_flacs
from lyrics_quality import (
    LyricsCandidate,
    assess_candidate,
    conservative_text,
    lrc_to_plain,
    parse_lrc,
    select_best,
    serialize_lrc,
    validate_lrc,
)
from mb_identity_score import evaluate_identity
from mb_track_match import resolve_mb_track
from metadata_policy import Confidence, DecisionAction, decide_field
from provider_cache import PARSER_VERSION, JsonCache, fetch_json, retry_delay
from release_match import ReleaseConfidence, assess_medium_layouts, select_release_candidate
from verify_all_flacs import audit_flac, audit_library


def make_flac(path: Path, *, seconds: float = 0.2) -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            str(seconds),
            str(path),
        ],
        check=True,
    )
    return path


def core_tags(title: str = "Track") -> list[tuple[str, str]]:
    return [
        ("TITLE", title),
        ("ARTIST", "Artist"),
        ("ARTISTS", "Artist"),
        ("ALBUM", "Album"),
        ("ALBUMARTIST", "Artist"),
        ("ALBUMARTISTS", "Artist"),
        ("DATE", "2000"),
        ("ORIGINALDATE", "1990"),
        ("TRACKNUMBER", "1"),
        ("TRACKTOTAL", "1"),
        ("DISCNUMBER", "1"),
        ("DISCTOTAL", "1"),
    ]


def _http_headers(**values: str) -> Message:
    headers = Message()
    for name, value in values.items():
        headers[name.replace("_", "-")] = value
    return headers


class TestFieldPolicy:
    def test_actions_cover_safe_decision_model(self) -> None:
        assert decide_field("TITLE", [], ["New"]).action is DecisionAction.ADD
        assert decide_field("TITLE", ["Same"], ["Same"]).action is DecisionAction.KEEP
        # Genre/style provenance is edition-scoped and must not be blindly
        # unioned without an independently proven same release.
        assert decide_field("GENRE", ["Rock"], ["Pop"]).action is DecisionAction.REVIEW_REQUIRED
        assert (
            decide_field(
                "TITLE",
                ["Old"],
                ["Verified"],
                existing_confidence=Confidence.LOW,
                incoming_confidence=Confidence.VERIFIED,
                release_confidence=Confidence.HIGH,
            ).action
            is DecisionAction.REPLACE
        )
        assert decide_field("TITLE", ["Local"], ["Remote"]).action is DecisionAction.REVIEW_REQUIRED

    def test_stable_identifier_conflict_is_never_forced(self) -> None:
        decision = decide_field(
            "MUSICBRAINZ_ALBUMID",
            ["1bc2fe9f-630e-3a84-8589-7ee6789a5e0f"],
            ["2bc2fe9f-630e-4a84-8589-7ee6789a5e0f"],
            incoming_confidence=Confidence.VERIFIED,
            stable_ids_agree=False,
            force=True,
        )
        assert decision.action is DecisionAction.REVIEW_REQUIRED

    def test_stable_identifier_conflict_blocks_other_entity_fields(self) -> None:
        decision = decide_field(
            "LABEL",
            [],
            ["Wrong Edition Records"],
            incoming_confidence=Confidence.VERIFIED,
            stable_ids_agree=False,
            force=True,
        )
        assert decision.action is DecisionAction.REVIEW_REQUIRED
        assert decision.values == ()

        file_local = decide_field(
            "REPLAYGAIN_TRACK_GAIN",
            [],
            ["-7.00 dB"],
            stable_ids_agree=False,
        )
        assert file_local.action is DecisionAction.ADD

    def test_unicode_is_nfc_without_typography_loss(self) -> None:
        decomposed = "Cafe\N{COMBINING ACUTE ACCENT} — ‘mix’"
        decision = decide_field("TITLE", [], [decomposed])
        assert unicodedata.is_normalized("NFC", decision.values[0])
        assert "— ‘mix’" in decision.values[0]

    def test_release_scoped_empty_field_needs_release_confidence(self) -> None:
        decision = decide_field(
            "CATALOGNUMBER",
            [],
            ["ABC-123"],
            incoming_confidence=Confidence.MEDIUM,
            release_confidence=Confidence.MEDIUM,
        )
        assert decision.action is DecisionAction.REVIEW_REQUIRED

    def test_credits_merge_only_with_same_entity_evidence(self) -> None:
        assert decide_field("COMPOSER", ["One"], ["Two"]).action is DecisionAction.REVIEW_REQUIRED
        merged = decide_field("COMPOSER", ["One"], ["Two"], stable_ids_agree=True)
        assert merged.action is DecisionAction.MERGE
        assert merged.values == ("One", "Two")


class TestTocEvidence:
    def test_synthetic_flac_toc_is_never_exact(self, tmp_path: Path) -> None:
        make_flac(tmp_path / "01 - One.flac")
        toc = mb_discid_from_cue.build_from_flac_dir(tmp_path)
        assert toc["source"] == "FLAC-DIR"
        assert toc["toc_exact"] is False
        assert toc["is_partial"] is False

    def test_exact_source_enum_is_shared(self) -> None:
        assert "EAC+CUE" in mb_discid_from_cue.EXACT_TOC_SOURCES
        assert "FLAC-DIR" not in mb_discid_from_cue.EXACT_TOC_SOURCES

    def test_nested_multi_disc_without_cue_keeps_medium_boundary(self, tmp_path: Path) -> None:
        for disc in (1, 2):
            disc_dir = tmp_path / f"CD{disc}"
            disc_dir.mkdir()
            flac = make_flac(disc_dir / "01 - Track.flac")
            audio = FLAC(str(flac))
            audio["DISCNUMBER"] = str(disc)
            audio.save()
        tocs = mb_discid_from_cue.build_all(tmp_path)
        assert set(tocs) == {1, 2}
        assert all(toc["source"] == "FLAC-DIR" for toc in tocs.values())

    def test_abandoned_transaction_does_not_enter_toc_discovery(self, tmp_path: Path) -> None:
        source = make_flac(tmp_path / "01 - One.flac")
        residue = tmp_path / ".metadata-transaction-old" / "pending"
        residue.mkdir(parents=True)
        shutil.copy2(source, residue / source.name)
        tocs = mb_discid_from_cue.build_all(tmp_path)
        assert set(tocs) == {1}
        assert tocs[1]["track_count"] == 1


class TestAliasMigration:
    def test_agreeing_total_alias_is_migrated_without_data_loss(self, tmp_path: Path) -> None:
        path = make_flac(tmp_path / "alias.flac")
        audio = FLAC(str(path))
        audio["TOTALTRACKS"] = "1"
        audio.save()
        metadata_writer.write_tags(path, core_tags())
        updated = FLAC(str(path))
        assert updated["TRACKTOTAL"] == ["1"]
        assert "TOTALTRACKS" not in updated


class TestReleaseSelection:
    @staticmethod
    def candidate(identifier: str, score: float, confidence: str = "HIGH") -> dict[str, object]:
        return {
            "id": identifier,
            "identity": {"score": score, "confidence": confidence, "rejected": False},
        }

    def test_low_confidence_is_refused(self) -> None:
        selection = select_release_candidate([self.candidate("low", 999, "LOW")])
        assert selection.candidate is None
        assert selection.confidence is ReleaseConfidence.REJECT

    def test_close_editions_require_review(self) -> None:
        selection = select_release_candidate(
            [
                self.candidate("remaster", 720),
                self.candidate("original", 680),
            ]
        )
        assert selection.candidate is None
        assert selection.confidence is ReleaseConfidence.AMBIGUOUS

    def test_clear_exact_winner_is_accepted_deterministically(self) -> None:
        selection = select_release_candidate(
            [
                self.candidate("b", 900, "EXACT"),
                self.candidate("a", 600),
            ]
        )
        assert selection.candidate and selection.candidate["id"] == "b"
        assert selection.margin == 300

    def test_duplicate_discovery_rows_do_not_hide_close_other_edition(self) -> None:
        selection = select_release_candidate(
            [
                self.candidate("edition-a", 900, "EXACT"),
                self.candidate("edition-a", 895, "EXACT"),
                self.candidate("edition-b", 880, "EXACT"),
            ]
        )
        assert selection.candidate is None
        assert selection.confidence is ReleaseConfidence.AMBIGUOUS
        assert selection.margin == 20

    def test_duplicate_discovery_rows_do_not_create_false_ambiguity(self) -> None:
        selection = select_release_candidate([self.candidate("edition-a", 900), self.candidate("edition-a", 895)])
        assert selection.candidate is not None
        assert selection.candidate["id"] == "edition-a"

    def test_track_match_does_not_borrow_track_from_wrong_numbered_disc(self) -> None:
        mb_release = {
            "media": [
                {
                    "position": 1,
                    "tracks": [
                        {"number": "1", "title": "Same Title", "length": 180000, "recording": {"id": "wrong-disc"}}
                    ],
                }
            ]
        }
        match = resolve_mb_track(mb_release, 2, 1, "Same Title", "", "", 180.0)
        assert not match["ok"]
        assert match["reason"] == "disc-position-mismatch"


class TestEditionIdentity:
    @staticmethod
    def release(
        *, barcode: str = "", catalog: str = "", date: str = "1980", artist: str = "Artist"
    ) -> dict[str, object]:
        return {
            "title": "Album",
            "artist-credit": [{"name": artist}],
            "barcode": barcode,
            "date": date,
            "label-info": [{"catalog-number": catalog}] if catalog else [],
            "media": [],
        }

    @staticmethod
    def discogs(
        *, barcode: str = "", catalog: str = "", artist: str = "Artist", classical: bool = False
    ) -> dict[str, object]:
        return {
            "title": "Album",
            "artists": [{"name": artist}],
            "genres": ["Classical"] if classical else [],
            "labels": [{"catno": catalog}] if catalog else [],
            "identifiers": [{"type": "Barcode", "value": barcode}] if barcode else [],
            "tracklist": [],
        }

    def test_explicit_barcode_catalog_and_date_conflicts_are_hard_rejections(self) -> None:
        barcode = evaluate_identity(
            self.discogs(barcode="012345678905"),
            self.release(barcode="999999999999"),
        )
        catalog = evaluate_identity(
            self.discogs(catalog="ABC-100"),
            self.release(catalog="XYZ-900"),
        )
        date = evaluate_identity(
            self.discogs(),
            self.release(date="1990"),
            wyear="1980",
        )
        assert "barcode-conflict" in barcode["hard_conflicts"]
        assert "catalog-conflict" in catalog["hard_conflicts"]
        assert "edition-date-conflict" in date["hard_conflicts"]
        assert barcode["rejected"] and catalog["rejected"] and date["rejected"]

    def test_classical_artist_exception_requires_strong_release_evidence(self) -> None:
        weak = evaluate_identity(
            self.discogs(artist="Soloist", classical=True),
            self.release(artist="Orchestra"),
        )
        strong = evaluate_identity(
            self.discogs(barcode="012345678905", artist="Soloist", classical=True),
            self.release(barcode="012345678905", artist="Orchestra"),
        )
        assert weak["rejected"] is True
        assert strong["rejected"] is False
        assert strong["flags"]["artist_exception"] is True


class TestLyricsSafety:
    def test_legacy_provider_failures_remain_distinct_from_valid_no_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import urllib.error
        import urllib.request

        import lyrics_utils

        invoke_lyrics_provider = lyrics_utils.invoke_lyrics_provider

        def no_hit() -> None:
            with lyrics_utils._provider_urlopen("https://provider.test/search", timeout=1):
                return

        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                urllib.error.HTTPError(
                    "https://provider.test/search", 429, "limited", _http_headers(Retry_After="4"), None
                )
            ),
        )
        limited = invoke_lyrics_provider("Fixture", no_hit)
        assert limited["acquisition_status"] == "RATE_LIMITED"

        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                urllib.error.HTTPError("https://provider.test/search", 404, "missing", _http_headers(), None)
            ),
        )
        missing = invoke_lyrics_provider("Fixture", no_hit)
        assert missing["acquisition_status"] == "NOT_FOUND"

        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                urllib.error.HTTPError("https://provider.test/search", 451, "restricted", _http_headers(), None)
            ),
        )
        restricted = invoke_lyrics_provider("Fixture", no_hit)
        assert restricted["acquisition_status"] == "BLOCKED"

        class EmptyResponse:
            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int = -1) -> bytes:
                return b'{"results": []}'

        def valid_empty_search() -> None:
            with lyrics_utils._provider_urlopen("https://provider.test/search", timeout=1) as response:
                lyrics_utils._provider_json_loads(response.read())
                lyrics_utils.mark_provider_not_found()

        monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: EmptyResponse())
        empty = invoke_lyrics_provider("Fixture", valid_empty_search)
        assert empty["acquisition_status"] == "NOT_FOUND"

    def test_unrecognized_successful_page_is_parser_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.request

        import lyrics_utils

        class ChangedPage:
            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int = -1) -> bytes:
                return b"<html><body>layout changed</body></html>"

        monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: ChangedPage())

        def incompatible_parser() -> None:
            with lyrics_utils._provider_urlopen("https://provider.test/page", timeout=1) as response:
                lyrics_utils.read_provider_text(response)

        result = lyrics_utils.invoke_lyrics_provider("Fixture", incompatible_parser)
        assert result["acquisition_status"] == "PARSER_ERROR"

    def test_provider_json_parser_failure_is_not_reported_as_no_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.request

        import lyrics_utils

        class MalformedResponse:
            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int = -1) -> bytes:
                return b"not JSON"

        def parser_failure() -> None:
            with lyrics_utils._provider_urlopen("https://provider.test/api", timeout=1) as response:
                lyrics_utils._provider_json_loads(response.read())

        monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: MalformedResponse())
        result = lyrics_utils.invoke_lyrics_provider("Fixture", parser_failure)
        assert result["acquisition_status"] == "PARSER_ERROR"

    def test_partial_provider_failure_cannot_become_clean_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.error

        import lyrics_utils

        def limited(*_args: object, **_kwargs: object) -> object:
            raise urllib.error.HTTPError(
                "https://provider.test/fallback", 429, "limited", _http_headers(Retry_After="30"), None
            )

        monkeypatch.setattr(lyrics_utils.urllib.request, "urlopen", limited)

        def partial_no_result() -> dict[str, str]:
            try:
                with lyrics_utils._provider_urlopen("https://provider.test/fallback", timeout=1):
                    pass
            except urllib.error.HTTPError:
                pass
            lyrics_utils.mark_provider_not_found()
            return {"source": "Fixture", "acquisition_status": "NOT_FOUND"}

        result = lyrics_utils.invoke_lyrics_provider("Fixture", partial_no_result)
        assert result["acquisition_status"] == "RATE_LIMITED"
        assert result["partial_no_result"] is True

    def test_provider_result_cache_uses_recording_context_and_only_caches_found_results(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import lyrics_utils

        monkeypatch.setattr(lyrics_utils, "_PROVIDER_RESULT_CACHE", JsonCache(tmp_path / "provider-cache"))
        calls = 0

        def found(song: str, artist: str) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {
                "found": True,
                "source": "Fixture",
                "trackName": song,
                "artistName": artist,
                "plainLyrics": "A complete and safely cached provider lyric result.",
            }

        context = {
            "title": "Song (Live)",
            "artist": "Artist",
            "album": "Concert Album",
            "duration": 240.0,
            "isrc": "USABC1234567",
            "recording_id": "mb-recording-live",
        }
        first = lyrics_utils.invoke_lyrics_provider("Fixture", found, "Song (Live)", "Artist", _cache_identity=context)
        again = lyrics_utils.invoke_lyrics_provider("Fixture", found, "Song (Live)", "Artist", _cache_identity=context)
        assert first["plainLyrics"] == again["plainLyrics"]
        assert again["cache_hit"] is True and calls == 1

        different_version = {**context, "title": "Song", "album": "Studio Album", "duration": 180.0}
        lyrics_utils.invoke_lyrics_provider("Fixture", found, "Song", "Artist", _cache_identity=different_version)
        assert calls == 2

        punctuated = {**context, "title": "A/B", "duration": 240.0}
        lyrics_utils.invoke_lyrics_provider("Fixture", found, "A/B", "Artist", _cache_identity=punctuated)
        unpunctuated = {**punctuated, "title": "AB"}
        lyrics_utils.invoke_lyrics_provider("Fixture", found, "AB", "Artist", _cache_identity=unpunctuated)
        assert calls == 4

        empty_calls = 0

        def valid_empty() -> None:
            nonlocal empty_calls
            empty_calls += 1
            lyrics_utils.mark_provider_not_found()

        lyrics_utils.invoke_lyrics_provider("Empty Fixture", valid_empty, _cache_identity=context)
        lyrics_utils.invoke_lyrics_provider("Empty Fixture", valid_empty, _cache_identity=context)
        assert empty_calls == 2

        failures = 0

        def transient_failure() -> None:
            nonlocal failures
            failures += 1
            raise ConnectionError("offline")

        first_failure = lyrics_utils.invoke_lyrics_provider(
            "Transient Fixture", transient_failure, _cache_identity=context
        )
        second_failure = lyrics_utils.invoke_lyrics_provider(
            "Transient Fixture", transient_failure, _cache_identity=context
        )
        assert first_failure["acquisition_status"] == "TRANSIENT_ERROR"
        assert second_failure["acquisition_status"] == "TRANSIENT_ERROR"
        assert failures == 2

    def test_provider_rate_limit_retry_never_violates_retry_after(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.error
        import urllib.request

        import lyrics_utils

        calls = 0

        def limited(*_args: object, **_kwargs: object) -> object:
            nonlocal calls
            calls += 1
            raise urllib.error.HTTPError(
                "https://long-wait-provider.test/search", 429, "limited", _http_headers(Retry_After="4"), None
            )

        monkeypatch.setattr(urllib.request, "urlopen", limited)
        monkeypatch.setattr(lyrics_utils.time, "sleep", lambda _seconds: None)
        with pytest.raises(urllib.error.HTTPError):
            lyrics_utils._provider_urlopen("https://long-wait-provider.test/search", timeout=1)
        assert calls == 1

    def test_lrclib_plain_candidate_is_a_candidate_not_an_acquisition_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import lyrics_pipeline
        import lyrics_utils

        class Response:
            status_code = 200
            headers: ClassVar[dict[str, str]] = {}

            def __init__(self, payload: object) -> None:
                self.payload = payload

            def json(self) -> object:
                return self.payload

        class Session:
            def get(self, url: str, **_kwargs: object) -> Response:
                if "/api/get?" in url:
                    return Response(
                        {
                            "id": 321,
                            "trackName": "Fixture Song",
                            "artistName": "Fixture Artist",
                            "albumName": "Fixture Album",
                            "duration": 180,
                            "isrc": "USABC1234567",
                            "plainLyrics": (
                                "The first original verse keeps its complete text and structure in this result.\n"
                                "A second full stanza proves this is a substantial unsynchronized transcript.\n"
                                "The final line remains part of the correct studio recording."
                            ),
                            "syncedLyrics": None,
                            "instrumental": False,
                        }
                    )
                return Response([])

        class NoWait:
            def acquire(self) -> None:
                return None

        monkeypatch.setattr(lyrics_utils, "_LRCLIB_SESSION", Session())
        monkeypatch.setattr(lyrics_utils, "_LRCLIB_CACHE", JsonCache(tmp_path / "lrclib-cache"))
        monkeypatch.setattr(lyrics_utils, "_LRCLIB_RATE_LIMITER", NoWait())
        result = lyrics_utils.fetch_lrclib("Fixture Song", "Fixture Artist", "Fixture Album", duration=180)
        assert result is not None and result["found"] is True
        assert "acquisition_status" not in result
        selected = lyrics_pipeline.choose_provider_result(
            [result],
            title="Fixture Song",
            artist="Fixture Artist",
            album="Fixture Album",
            duration=180,
            isrc="USABC1234567",
        )
        assert selected is not None and selected.candidate.provider == "LRCLIB (exact)"
        assert selected.candidate.synced is False

    def test_lrclib_instrumental_flag_requires_recording_identity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import lyrics_utils

        class Response:
            status_code = 200
            headers: ClassVar[dict[str, str]] = {}

            def __init__(self, payload: object) -> None:
                self.payload = payload

            def json(self) -> object:
                return self.payload

        class Session:
            def get(self, url: str, **_kwargs: object) -> Response:
                if "/api/get?" in url:
                    return Response(
                        {
                            "id": 981,
                            "trackName": "Instrumental Piece",
                            "artistName": "Fixture Artist",
                            "albumName": "Quiet Album",
                            "duration": 180,
                            "isrc": "USABC1234567",
                            "instrumental": True,
                            "plainLyrics": None,
                            "syncedLyrics": None,
                        }
                    )
                return Response([])

        class NoWait:
            def acquire(self) -> None:
                return None

        monkeypatch.setattr(lyrics_utils, "_LRCLIB_SESSION", Session())
        monkeypatch.setattr(lyrics_utils, "_LRCLIB_CACHE", JsonCache(tmp_path / "lrclib-instrumental"))
        monkeypatch.setattr(lyrics_utils, "_LRCLIB_RATE_LIMITER", NoWait())
        verified = lyrics_utils.fetch_lrclib(
            "Instrumental Piece",
            "Fixture Artist",
            "Quiet Album",
            duration=180,
            isrc="USABC1234567",
        )
        assert verified is not None
        assert verified["acquisition_status"] == "INSTRUMENTAL"
        assert verified["instrumental_verified"] is True
        assert lyrics_pipeline.unsuccessful_lookup_status([verified]) == "INSTRUMENTAL"

        conflict = lyrics_utils.fetch_lrclib(
            "Instrumental Piece",
            "Fixture Artist",
            "Quiet Album",
            duration=180,
            isrc="USZZZ1234567",
        )
        assert conflict is not None
        assert conflict["acquisition_status"] == "NOT_FOUND"
        assert lyrics_pipeline.unsuccessful_lookup_status([conflict]) == "NOT_FOUND"

    def test_instrumental_detection_requires_explicit_evidence(self) -> None:
        from lyrics_utils import detect_instrumental

        assert detect_instrumental("Song (Instrumental)")
        assert not detect_instrumental("Song (Orchestral Version)")
        assert not detect_instrumental("Song (Piano Version)")

    def test_search_url_component_encoding_preserves_unicode_and_encodes_slashes(self) -> None:
        from lyrics_utils import _quote_component

        encoded = _quote_component("AC/DC — Café & 'Song'")
        assert "%2F" in encoded and "%26" in encoded and "%27" in encoded
        assert "%C3%A9" in encoded and "%E2%80%94" in encoded

    def test_same_provider_plain_text_can_invalidate_partial_sync(self, tmp_path: Path) -> None:
        short_sync = (
            "[00:10.00]The first chorus begins with these complete words and lines\n"
            "[00:20.00]A second chorus line is repeated here for the version\n"
            "[00:30.00]Then the chorus ends without any of the verses"
        )
        full_plain = (
            "The opening verse is present in the provider plain version with all its original words.\n"
            "A second verse adds another full stanza before the chorus begins.\n"
            "The first chorus begins with these complete words and lines.\n"
            "A second chorus line is repeated here for the version.\n"
            "Then the chorus ends without any of the verses.\n"
            "A bridge and closing verse complete the full song transcript."
        )
        candidate = lyrics_pipeline.provider_candidate(
            {
                "source": "fixture",
                "trackName": "Song",
                "artistName": "Artist",
                "duration": 240,
                "plainLyrics": full_plain,
                "syncedLyrics": short_sync,
                "providerRaw": "raw provider payload",
            },
            fallback_provider="fixture",
            duration=240,
        )
        assert candidate is not None
        assert not candidate.synced
        assert candidate.text == full_plain
        assert candidate.metadata["sync_text_mismatch"] is True
        assert any("disagrees" in warning for warning in candidate.warnings)

        assessment = assess_candidate(candidate, title="Song", artist="Artist", album="", duration=240)
        output = lyrics_pipeline.materialize_selected(tmp_path / "song.flac", assessment)
        assert "synced" not in output.paths
        assert output.paths["plain"].read_text(encoding="utf-8") == full_plain

    def test_provider_duration_mismatch_demotes_sync_but_keeps_matching_plain_text(self) -> None:
        import lyrics_pipeline

        result = {
            "source": "Fixture",
            "id": "fixture-live-id",
            "trackName": "Song (Live)",
            "artistName": "Artist",
            "duration": 350.0,
            "isrc": "USABC1234567",
            "syncedLyrics": (
                "[00:10.00]The live recording begins with a clearly audible lyric line\n"
                "[00:50.00]Another complete line remains well inside the shorter local track"
            ),
            "plainLyrics": "The live recording begins with a clearly audible lyric line\nAnother complete line remains well inside the shorter local track",
        }
        candidate = lyrics_pipeline.provider_candidate(result, fallback_provider="Fixture", duration=180.0)
        assert candidate is not None
        assert candidate.synced is False
        assert candidate.synced_lyrics == ""
        assert candidate.plain_lyrics == result["plainLyrics"]
        assert candidate.metadata["sync_duration_mismatch"] is True

    def test_translation_and_romanization_remain_separate_forms(self, tmp_path: Path) -> None:
        primary_text = (
            "Original lyrics are preserved here with enough complete wording for a proper candidate.\n"
            "The second line keeps the source language and the provider's exact punctuation."
        )
        translation_text = (
            "La traducción conserva aquí el sentido y la estructura de las líneas originales.\n"
            "Esta segunda línea pertenece a la traducción y no debe sustituir el original."
        )
        romaji_text = (
            "Koko ni wa romaji no uta no ichiban no bun ga arimasu.\n"
            "Nibanme no bun mo moto no nihongo to wakete hozon saremasu."
        )
        common = {
            "trackName": "Song",
            "artistName": "Artist",
            "duration": 180.0,
            "isrc": "USABC1234567",
            "identity_validated": True,
        }
        primary = {
            **common,
            "source": "Original Source",
            "plainLyrics": primary_text,
            "language": "en",
        }
        translation = {
            **common,
            "source": "Translation Source",
            "translationLyrics": translation_text,
            "translation": True,
            "language": "es",
        }
        romanization = {
            **common,
            "source": "Romanization Source",
            "romajiLyrics": romaji_text,
            "romanization": True,
            "language": "ja-Latn",
        }
        selected = lyrics_pipeline.choose_provider_result(
            [primary, translation, romanization],
            title="Song",
            artist="Artist",
            album="Album",
            duration=180.0,
            language="en",
            isrc="USABC1234567",
        )
        assert selected is not None and not selected.candidate.translation
        result = selected.candidate.metadata["result"]
        assert isinstance(result, dict)
        assert result["plainLyrics"] == primary_text
        assert result["translationLyrics"] == translation_text
        assert result["romajiLyrics"] == romaji_text

        outputs = lyrics_pipeline.materialize_selected(tmp_path / "song.flac", selected)
        assert outputs.paths["plain"].read_text(encoding="utf-8") == primary_text
        assert outputs.paths["translation"].read_text(encoding="utf-8") == translation_text
        assert outputs.paths["romanization"].read_text(encoding="utf-8") == romaji_text
        initial_paths = dict(outputs.paths)
        initial_file_count = len(list(tmp_path.iterdir()))
        repeated = lyrics_pipeline.materialize_selected(tmp_path / "song.flac", selected)
        assert repeated.paths == initial_paths
        assert len(list(tmp_path.iterdir())) == initial_file_count

        only_translation = lyrics_pipeline.choose_provider_result(
            [translation],
            title="Song",
            artist="Artist",
            album="Album",
            duration=180.0,
            isrc="USABC1234567",
        )
        assert only_translation is not None and only_translation.candidate.translation
        alternate_only = lyrics_pipeline.materialize_selected(tmp_path / "alternate-only.flac", only_translation)
        assert "plain" not in alternate_only.paths and "synced" not in alternate_only.paths
        assert "translation" in alternate_only.paths

    def test_sync_text_comparison_does_not_rewrite_saved_unicode(self, tmp_path: Path) -> None:
        source = "Cafe\N{COMBINING ACUTE ACCENT} — 日本語の歌詞\nAnother original line with smart ‘quotes’."
        synced = "[00:01.00]Cafe\N{COMBINING ACUTE ACCENT} — 日本語の歌詞\n[00:04.00]Another original line with smart ‘quotes’."
        candidate = lyrics_pipeline.provider_candidate(
            {
                "source": "fixture",
                "trackName": "Song",
                "artistName": "Artist",
                "duration": 60,
                "plainLyrics": source,
                "syncedLyrics": synced,
            },
            fallback_provider="fixture",
            duration=60,
        )
        assert candidate is not None and candidate.synced
        assert candidate.plain_lyrics == source
        assessment = assess_candidate(candidate, title="Song", artist="Artist", album="", duration=60)
        output = lyrics_pipeline.materialize_selected(tmp_path / "song.flac", assessment)
        assert output.paths["plain"].read_text(encoding="utf-8") == source
        assert output.paths["synced"].read_text(encoding="utf-8") == synced

    def test_multiline_lyrics_value_preserves_combining_form_and_outer_blanks(self) -> None:
        from metadata_schema import normalize_value

        value = "\nCafe\N{COMBINING ACUTE ACCENT}  \n\n日本語\n"
        assert normalize_value("UNSYNCEDLYRICS", value) == value
        embedded_bom = "Before\ufeffinside"
        assert normalize_value("UNSYNCEDLYRICS", "\ufeff" + embedded_bom) == embedded_bom

    def test_existing_lyrics_labels_are_archived_then_removed_from_playback_tag(self, tmp_path: Path) -> None:
        path = make_flac(tmp_path / "existing-labels.flac")
        source = "[00:01.00][Verse 1]First verse line\n[00:04.00]Second line\n[Bridge]\n[00:08.00]Final line"
        audio = FLAC(str(path))
        audio["LYRICS"] = [source]
        audio.save()

        metadata_writer.write_tags(path, [])
        updated = FLAC(str(path))
        assert updated["LYRICS"] == ["[00:01.00]First verse line\n[00:04.00]Second line\n[00:08.00]Final line"]
        archives = list(tmp_path.glob("existing-labels.lyrics-tag-archive.*.json"))
        assert len(archives) == 1
        assert json.loads(archives[0].read_text(encoding="utf-8")) == [source]

    def test_lrc_is_the_embedded_sync_form_and_rich_formats_remain_sidecars(self, tmp_path: Path) -> None:
        path = make_flac(tmp_path / "lyrics-storage.flac", seconds=30)
        plain = tmp_path / "lyrics-storage.txt"
        synced = tmp_path / "lyrics-storage.lrc"
        enhanced = tmp_path / "lyrics-storage.elrc"
        ttml = tmp_path / "lyrics-storage.ttml"
        plain.write_text("Original lyrics in the source language.\nSecond complete line.", encoding="utf-8")
        synced_text = "[00:10.00][Verse 1]Original words line one\n[00:20.00]Original words line two"
        enhanced_text = "[00:10.00]<00:10.00>Original <00:10.50>words"
        ttml_text = '<tt xmlns="http://www.w3.org/ns/ttml"><body><p begin="00:10.00">Words</p></body></tt>'
        synced.write_text(synced_text, encoding="utf-8")
        enhanced.write_text(enhanced_text, encoding="utf-8")
        ttml.write_text(ttml_text, encoding="utf-8")

        metadata_writer.write_tags(
            path,
            [],
            lyrics_file=str(plain),
            synced_lyrics_file=str(synced),
            enhanced_lyrics_file=str(enhanced),
            ttml_file=str(ttml),
        )

        tagged = FLAC(str(path))
        assert tagged["LYRICS"] == ["[00:10.00]Original words line one\n[00:20.00]Original words line two"]
        assert tagged["UNSYNCEDLYRICS"] == [plain.read_text(encoding="utf-8")]
        assert tagged["SYNCEDLYRICS"] == [synced_text]
        assert "LYRICS_TTML" not in tagged and "SYNCEDLYRICS_ENHANCED" not in tagged
        assert synced.read_text(encoding="utf-8") == synced_text
        assert enhanced.read_text(encoding="utf-8") == enhanced_text
        assert ttml.read_text(encoding="utf-8") == ttml_text

    def test_version_labels_are_hard_recording_identity_evidence(self) -> None:
        from lyrics_utils import is_track_match

        assert not is_track_match("Song (Live)", "Artist", "Song", "Artist")
        assert not is_track_match("Song (Remix)", "Artist", "Song", "Artist")
        assert not is_track_match("Song (Radio Edit)", "Artist", "Song", "Artist")
        assert not is_track_match("Song Live", "Artist", "Song", "Artist")
        assert not is_track_match("Song Radio Edit", "Artist", "Song", "Artist")
        assert not is_track_match("Song 2", "Artist", "Song 3", "Artist")
        assert is_track_match("Song (2011 Remastered)", "Artist", "Song", "Artist")
        assert is_track_match("Who Wants To Live Forever", "Queen", "Who Wants To Live Forever", "Queen")
        assert is_track_match("The Original Song", "Artist", "The Original Song", "Artist")

    def test_stable_identifier_conflicts_reject_candidate(self) -> None:
        candidate = LyricsCandidate(
            "provider",
            "A sufficiently long lyric line\nAnother full lyric line",
            title="Song",
            artist="Artist",
            duration=180,
            isrc="USAAA0000001",
        )
        selected = select_best([candidate], title="Song", artist="Artist", album="", duration=180, isrc="USBBB0000002")
        assert selected is None

    def test_original_language_conflict_is_rejected_but_translation_is_retained(self) -> None:
        original = LyricsCandidate(
            "Translation mislabeled as original",
            "A complete English translation of the song.",
            title="Song",
            artist="Artist",
            album="Album",
            duration=180,
            language="en",
        )
        selected = select_best([original], title="Song", artist="Artist", album="Album", duration=180, language="ja")
        assert selected is None
        translation = LyricsCandidate(
            "Translation source",
            "A complete English translation of the song.",
            title="Song",
            artist="Artist",
            album="Album",
            duration=180,
            language="en",
            translation=True,
        )
        translated = select_best(
            [translation], title="Song", artist="Artist", album="Album", duration=180, language="ja"
        )
        assert translated and translated.candidate.translation

    def test_forced_alignment_requires_explicit_english_not_latin_script_guess(self) -> None:
        from lyrics_utils import NeuralLyricsEngine

        assert NeuralLyricsEngine.can_align_mms("English lyrics", "en-US")
        assert NeuralLyricsEngine.can_align_mms("English lyrics", "eng")
        assert not NeuralLyricsEngine.can_align_mms("Paroles françaises", "fr")
        assert not NeuralLyricsEngine.can_align_mms("Русский текст", "ru")
        assert not NeuralLyricsEngine.can_align_mms("Lyrics in unknown language", None)

    def test_sync_does_not_outweigh_large_text_quality_difference(self) -> None:
        plain = LyricsCandidate(
            "plain",
            "Verse one has enough meaningful words to establish the complete text.\n"
            "Verse two contains another complete and coherent section of this song.",
            title="Song",
            artist="Artist",
            album="Album",
            duration=180,
        )
        snippet = LyricsCandidate(
            "sync",
            "[00:10.00]Short snippet\n[00:20.00]Two lines",
            synced=True,
            title="Song",
            artist="Artist",
            album="Album",
            duration=180,
        )
        winner = select_best([snippet, plain], title="Song", artist="Artist", album="Album", duration=180)
        assert winner and winner.candidate.provider == "plain"

    def test_good_sync_breaks_tie_with_equivalent_plain_text(self) -> None:
        words = "A lyric line with enough meaningful words\nAnother complete lyric line"
        candidates = [
            LyricsCandidate("plain", words, title="Song", artist="Artist", duration=120),
            LyricsCandidate(
                "sync",
                "[00:10.00]A lyric line with enough meaningful words\n[00:40.00]Another complete lyric line",
                synced=True,
                title="Song",
                artist="Artist",
                duration=120,
            ),
        ]
        winner = select_best(candidates, title="Song", artist="Artist", album="", duration=120)
        assert winner and winner.candidate.provider == "sync"

    def test_incomplete_candidate_is_warned_and_loses_to_full_same_version_text(self) -> None:
        short = LyricsCandidate(
            "short",
            "Only a brief lyric section",
            title="Song",
            artist="Artist",
            album="Album",
            duration=220,
        )
        complete = LyricsCandidate(
            "complete",
            "A complete verse begins with a meaningful lyric line and continues here.\n"
            "A second verse follows with enough distinct words for a strong completeness check.\n"
            "A bridge and final refrain complete the text in the provider result.",
            title="Song",
            artist="Artist",
            album="Album",
            duration=220,
        )
        winner = select_best([short, complete], title="Song", artist="Artist", album="Album", duration=220)
        assert winner and winner.candidate.provider == "complete"
        short_assessment = assess_candidate(short, title="Song", artist="Artist", album="Album", duration=220)
        assert any("unusually short" in warning for warning in short_assessment.warnings)

    def test_very_short_nonempty_lyrics_are_assessed_not_automatically_rejected(self) -> None:
        candidate = LyricsCandidate(
            "exact-provider",
            "Oh, no",
            title="Song",
            artist="Artist",
            album="Album",
            duration=120,
        )
        assessment = assess_candidate(
            candidate,
            title="Song",
            artist="Artist",
            album="Album",
            duration=120,
        )
        assert assessment.valid
        assert 30 <= assessment.text_quality < 35
        assert any("very short non-empty lyrics" in warning for warning in assessment.warnings)
        # It stays eligible for evidence-based comparison, while a short text
        # alone does not reach the pipeline's automatic-selection threshold.
        assert assessment.score < 75

    def test_lrc_to_ttml_refuses_to_invent_line_end_times(self) -> None:
        from lyrics_utils import lrc_to_ttml

        malformed = "[00:20.00]Second line\n[00:10.00]First line"
        assert lrc_to_ttml(malformed, song="Song", artist="Artist") == ""
        source = "[00:01.23]  ((keep))\n[00:02.45] [Verse 1]"
        # LRC has onsets but no lyric end times or document extent. Do not
        # synthesize TTML intervals that imply timing absent from the source.
        assert lrc_to_ttml(source, song="Song", artist="Artist") == ""

    def test_equal_candidates_choose_independently_of_response_order(self) -> None:
        candidates = [
            LyricsCandidate(
                name,
                "A sufficiently complete lyric line\nAnother sufficiently complete line",
                title="Song",
                artist="Artist",
                album="Album",
                duration=180,
                provider_track_id=identifier,
            )
            for name, identifier in (("A provider", "a"), ("B provider", "b"))
        ]
        left = select_best(candidates, title="Song", artist="Artist", album="Album", duration=180)
        right = select_best(list(reversed(candidates)), title="Song", artist="Artist", album="Album", duration=180)
        assert left and right and left.candidate.provider == right.candidate.provider

    def test_generated_candidate_is_below_default_automatic_selection_tier(self) -> None:
        generated = {
            "source": "ASR",
            "generated": True,
            "trackName": "Song",
            "artistName": "Artist",
            "duration": 180,
            "plainLyrics": "A sufficiently complete generated lyric line\nAnother generated line",
        }
        selected = lyrics_pipeline.choose_provider_result(
            [generated], title="Song", artist="Artist", album="", duration=180
        )
        assert selected is None

    def test_unvalidated_provider_word_sync_flag_is_not_trusted(self) -> None:
        result = {
            "found": True,
            "source": "Fixture",
            "trackName": "Song",
            "artistName": "Artist",
            "duration": 180,
            "has_word_sync": True,
            "plainLyrics": "A complete lyric line for the song.\nA second meaningful line.",
            "syncedLyrics": "[00:01.20]A complete lyric line for the song.\n[00:02.40]A second meaningful line.",
            "enhancedLyrics": "[00:01.20]<not-a-time>A complete lyric line",
        }
        candidate = lyrics_pipeline.provider_candidate(result, fallback_provider="fixture", duration=180)
        assert candidate is not None
        assert candidate.synced is True
        raw_result = candidate.metadata["result"]
        assert isinstance(raw_result, dict)
        assert raw_result["has_word_sync"] is False
        assert candidate.metadata["word_sync_valid"] is False

    def test_lrc_roundtrip_keeps_precision_layout_and_unicode(self) -> None:
        raw = "[ar:Artist]\n[00:01.23]  ‘Été’ — 日本語  \n\n[00:02.345]((again))\n"
        parsed = parse_lrc(raw)
        assert parsed.synchronized is True
        assert parsed.timestamp_count == 2
        assert parsed.timestamp_coverage == 1.0
        assert parsed.first_timestamp_ms == 1_230 and parsed.last_timestamp_ms == 2_345
        assert serialize_lrc(parsed) == raw
        assert lrc_to_plain(raw) == "  ‘Été’ — 日本語  \n\n((again))\n"
        assert parse_lrc("A plain lyric line").synchronized is False

    def test_unicode_is_preserved_but_invalid_control_bytes_are_removed(self) -> None:
        raw = "Cafe\N{COMBINING ACUTE ACCENT} — ‘mix’\x00\u0001\n日本語\u200d歌詞"
        result = conservative_text(raw)
        assert result == "Cafe\N{COMBINING ACUTE ACCENT} — ‘mix’\n日本語\u200d歌詞"

    def test_timestamp_formats_precision_and_bad_hour_minutes(self) -> None:
        valid = (
            "[01:02]Line one\n[02:03.4]Line two\n[03:04.56]Line three\n[04:05.678]Line four\n[01:02:03.456]Line five"
        )
        document = parse_lrc(valid)
        assert [line.timestamp_ms for line in document.lines if line.timestamp_ms is not None] == [
            62_000,
            123_400,
            184_560,
            245_678,
            3_723_456,
        ]
        assert serialize_lrc(document) == valid
        parsed_bad = parse_lrc("[01:60:00.000]Invalid time\n[00:02]A valid following line")
        assert any("invalid timestamp" in item for item in parsed_bad.warnings)
        assert not validate_lrc("[01:60:00.000]Invalid time\n[00:02]A valid following line")[0]

    def test_timestamp_conversion_rejects_negative_and_nonfinite_values(self) -> None:
        from lyrics_utils import fmt_lrc_time, fmt_lrc_time_precision, fmt_ttml_time, ms_to_lrc_ts, parse_time

        for value in (-0.001, float("nan")):
            message = "non-negative" if value < 0 else "finite"
            with pytest.raises(ValueError, match=message):
                fmt_lrc_time(value)
            with pytest.raises(ValueError, match=message):
                fmt_lrc_time_precision(value, 2)
            with pytest.raises(ValueError, match=message):
                fmt_ttml_time(value)
        with pytest.raises(ValueError, match="non-negative"):
            ms_to_lrc_ts(-1)
        with pytest.raises(ValueError, match="invalid TTML clock"):
            parse_time("00:01:nan")

    def test_ttml_conversion_preserves_source_precision_and_vocal_text(self) -> None:
        from lyrics_utils import parse_ttml

        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <tt xmlns="http://www.w3.org/ns/ttml" xmlns:ttm="http://www.w3.org/ns/ttml#metadata">
          <body><div>
            <p begin="00:01.20"><span begin="00:01.23">Café</span><span begin="00:01.456">— 東京</span></p>
            <p begin="00:04.5" ttm:role="background"><span begin="00:04.56">(keep these sung brackets)</span></p>
          </div></body>
        </tt>"""
        plain, synced, has_words, has_background = parse_ttml(xml, song="Song", artist="Artist")
        assert has_words and has_background
        assert plain == "Café— 東京\n(keep these sung brackets)"
        assert synced.startswith("[00:01.20]<00:01.23>Café<00:01.456>— 東京")
        # The single span wraps the whole TTML paragraph, so its source
        # timestamp is retained as line timing without being relabelled as a
        # word timestamp.
        assert "[00:04.5](keep these sung brackets)" in synced
        assert " )" not in synced

    def test_ttml_timing_failure_preserves_plain_text_and_reports_diagnostic(self) -> None:
        from lyrics_utils import parse_ttml

        malformed_timing = (
            '<tt xmlns="http://www.w3.org/ns/ttml"><body><p begin="00:01">'
            '<span begin="not-a-clock">Preserved multilingual lyric — 東京</span></p></body></tt>'
        )
        diagnostics: list[str] = []
        plain, synced, has_words, _ = parse_ttml(malformed_timing, diagnostics=diagnostics)
        assert plain == "Preserved multilingual lyric — 東京"
        assert synced == ""
        assert not has_words
        assert any("malformed word timing" in warning for warning in diagnostics)
        assert any("no synchronized derivative" in warning for warning in diagnostics)

        missing_begin = '<tt xmlns="http://www.w3.org/ns/ttml"><body><p>Text with implicit timing</p></body></tt>'
        implicit_diagnostics: list[str] = []
        plain, synced, has_words, _ = parse_ttml(missing_begin, diagnostics=implicit_diagnostics)
        assert plain == "Text with implicit timing"
        assert synced == ""
        assert not has_words
        assert any("no absolute begin time" in warning for warning in implicit_diagnostics)

    def test_line_level_ttml_does_not_claim_word_sync(self) -> None:
        from lyrics_utils import parse_ttml

        line_level = (
            '<tt xmlns="http://www.w3.org/ns/ttml" xmlns:itunes="http://itunes.apple.com/lyric-ttml-extensions" '
            'itunes:timing="Line"><body><p begin="00:01.20"><span begin="00:01.20" end="00:03.00">'
            "A complete timed line</span></p></body></tt>"
        )
        plain, synced, has_words, _ = parse_ttml(line_level)
        assert plain == "A complete timed line"
        assert synced == "[00:01.20]A complete timed line"
        assert not has_words

    def test_malformed_or_entity_ttml_is_rejected_without_exception(self) -> None:
        from lyrics_pipeline import _valid_ttml
        from lyrics_utils import parse_ttml

        assert parse_ttml("<tt><body><p begin='00:01'>unfinished", song="Song") == ("", "", False, False)
        hostile = "<!DOCTYPE tt [<!ENTITY lyric 'secret'>]><tt><body>&lyric;</body></tt>"
        assert parse_ttml(hostile, song="Song") == ("", "", False, False)
        assert not _valid_ttml(hostile)

    def test_section_labels_and_repeats_survive_lrc_to_plain(self) -> None:
        lyrics = "[00:01.00][Verse 1]\n[00:02.00]Same refrain\n[00:03.00]Same refrain"
        assert lrc_to_plain(lyrics) == "[Verse 1]\nSame refrain\nSame refrain"

    def test_fixture_scrapers_extract_only_lyrics_containers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.request

        from lyrics_utils import _parse_darklyrics_songs, extract_genius_lyrics, fetch_darklyrics

        fixture_root = Path(__file__).parent / "tests" / "fixtures" / "lyrics"
        genius = extract_genius_lyrics((fixture_root / "genius" / "valid.html").read_text(encoding="utf-8"))
        assert genius == "[Verse 1]\nLine with “smart quotes”\n\nWord Embed belongs to this song\nこんにちは世界"
        hydration = extract_genius_lyrics((fixture_root / "genius" / "hydration.html").read_text(encoding="utf-8"))
        assert hydration and "日本語の歌詞" in hydration and "quotes" in hydration
        assert extract_genius_lyrics((fixture_root / "genius" / "changed.html").read_text(encoding="utf-8")) is None
        songs = _parse_darklyrics_songs((fixture_root / "darklyrics" / "valid.html").read_text(encoding="utf-8"))
        assert [song[1] for song in songs] == ["Wrong Song", "Requested Song (Remastered)", "Another Track"]
        assert "Repeated chorus" in songs[1][2]
        assert _parse_darklyrics_songs((fixture_root / "darklyrics" / "changed.html").read_text(encoding="utf-8")) == []
        from lyrics_utils import parse_bandcamp_lyrics_page

        bandcamp_root = fixture_root / "bandcamp"
        structured, semantic = parse_bandcamp_lyrics_page((bandcamp_root / "valid.html").read_text(encoding="utf-8"))
        assert structured and structured["album_title"] == "Fixture Album"
        assert structured["trackinfo"][0]["track_id"] == 42
        assert semantic == "Semantic fallback line\nAnother line"
        changed, no_lyrics = parse_bandcamp_lyrics_page((bandcamp_root / "changed.html").read_text(encoding="utf-8"))
        assert changed is None and no_lyrics == ""

        class Response:
            status = 200

            def __init__(self, body: bytes) -> None:
                self.body = body

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int = -1) -> bytes:
                return self.body

        index_page = (fixture_root / "darklyrics" / "index.html").read_bytes()
        album_page = (fixture_root / "darklyrics" / "valid.html").read_bytes()

        def open_fixture(request: urllib.request.Request, timeout: float = 6) -> Response:
            if request.full_url.endswith("exampleartist.html"):
                return Response(index_page)
            return Response(album_page)

        monkeypatch.setattr(urllib.request, "urlopen", open_fixture)
        candidate = fetch_darklyrics("Requested Song", "Example Artist", return_status=True)
        assert candidate and candidate.get("trackName") == "Requested Song (Remastered)"
        assert candidate["identity_validated"] is True
        assert "Compilation Album" == candidate["albumName"]

        def changed_structure(request: urllib.request.Request, timeout: float = 6) -> Response:
            if request.full_url.endswith("exampleartist.html"):
                return Response(index_page)
            return Response((fixture_root / "darklyrics" / "changed.html").read_bytes())

        monkeypatch.setattr(urllib.request, "urlopen", changed_structure)
        parser_result = fetch_darklyrics("Requested Song", "Example Artist", return_status=True)
        assert isinstance(parser_result, dict) and parser_result["acquisition_status"] == "PARSER_ERROR"

    def test_ultrastar_scraper_uses_structural_fixtures_and_typed_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import urllib.error
        import urllib.request

        import lyrics_utils

        root = Path(__file__).parent / "tests" / "fixtures" / "lyrics" / "ultrastar"
        search_html = (root / "search.html").read_text(encoding="utf-8")
        lyrics_html = (root / "lyrics.html").read_text(encoding="utf-8")
        no_results = (root / "no_results.html").read_text(encoding="utf-8")
        changed = (root / "changed.html").read_text(encoding="utf-8")

        rows = lyrics_utils.parse_ultrastar_search(search_html, "Bohemian Rhapsody", "Queen")
        assert rows == [{"id": "31415", "title": "Bohemian Rhapsody", "artist": "Queen"}]
        assert lyrics_utils.parse_ultrastar_search(no_results, "Missing", "Artist") == []
        with pytest.raises(ValueError, match="recognizable result state"):
            lyrics_utils.parse_ultrastar_search(changed, "Song", "Artist")
        assert "#TITLE:Bohemian Rhapsody" in lyrics_utils.parse_ultrastar_response(lyrics_html)
        with pytest.raises(ValueError, match="textarea"):
            lyrics_utils.parse_ultrastar_response(changed)

        class FixtureResponse:
            status = 200

            def __init__(self, body: str) -> None:
                self.body = body.encode("utf-8")

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int = -1) -> bytes:
                return self.body

        requests: list[urllib.request.Request] = []

        def open_fixture(request: urllib.request.Request, timeout: int = 6) -> FixtureResponse:
            del timeout
            requests.append(request)
            return FixtureResponse(search_html if request.get_method() == "GET" else lyrics_html)

        monkeypatch.setattr(lyrics_utils, "_provider_urlopen", open_fixture)
        found = lyrics_utils.fetch_ultrastar_db("Bohemian Rhapsody", "Queen")
        assert found["found"] is True
        assert found["provider_track_id"] == "31415"
        assert found["has_word_sync"] is True
        assert found["identity_validated"] is True
        assert len(requests) == 2
        assert "interpret=Queen" in requests[0].full_url
        assert requests[1].get_method() == "POST"
        assert "id=31415" in requests[1].full_url

        def changed_layout(*_args: object, **_kwargs: object) -> FixtureResponse:
            return FixtureResponse(changed)

        monkeypatch.setattr(lyrics_utils, "_provider_urlopen", changed_layout)
        parser_failure = lyrics_utils.fetch_ultrastar_db("Song", "Artist")
        assert parser_failure["acquisition_status"] == "PARSER_ERROR"

        def rate_limited(*_args: object, **_kwargs: object) -> None:
            raise urllib.error.HTTPError(
                "https://usdb.animux.de/index.php", 429, "limited", _http_headers(Retry_After="4"), None
            )

        monkeypatch.setattr(lyrics_utils, "_provider_urlopen", rate_limited)
        limited = lyrics_utils.fetch_ultrastar_db("Song", "Artist")
        assert limited["acquisition_status"] == "RATE_LIMITED"

    def test_unison_api_fixtures_preserve_ttml_identity_language_and_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import lyrics_utils

        root = Path(__file__).parent / "tests" / "fixtures" / "lyrics" / "unison"
        search = (root / "search.json").read_bytes()
        lyrics = (root / "lyrics.json").read_bytes()
        empty_search = (root / "no_result_search.json").read_bytes()
        empty_lyrics = (root / "no_result_lyrics.json").read_bytes()
        changed = (root / "changed.json").read_bytes()

        class FixtureResponse:
            status = 200

            def __init__(self, body: bytes) -> None:
                self.body = body

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int = -1) -> bytes:
                return self.body

        requests: list[str] = []

        def response(body: bytes) -> FixtureResponse:
            if getattr(lyrics_utils._LYRICS_PROVIDER_STATE, "active", False):
                lyrics_utils._LYRICS_PROVIDER_STATE.responses += 1
            return FixtureResponse(body)

        def open_found(request: urllib.request.Request, timeout: int = 8) -> FixtureResponse:
            del timeout
            url = request.full_url
            requests.append(url)
            return response(search if "/lyrics/search?" in url else lyrics)

        monkeypatch.setattr(lyrics_utils, "_provider_urlopen", open_found)
        result = lyrics_utils.invoke_lyrics_provider(
            "Unison", lyrics_utils.fetch_apple_music_ttml, "Fixture Song", "Fixture Artist", timeout=1
        )
        assert result["source"] == "Unison"
        assert result["trackName"] == "Fixture Song"
        assert result["artistName"] == "Fixture Artist"
        assert result["albumName"] == "Fixture Album"
        assert result["language"] == "ja"
        assert result["provider_track_id"] == "fixture-track-7"
        assert result["has_word_sync"] is True
        assert result["providerRaw"].startswith("<tt")
        assert len(requests) == 2

        def open_empty(request: urllib.request.Request, timeout: int = 8) -> FixtureResponse:
            del timeout
            return response(empty_search if "/lyrics/search?" in request.full_url else empty_lyrics)

        monkeypatch.setattr(lyrics_utils, "_provider_urlopen", open_empty)
        no_result = lyrics_utils.invoke_lyrics_provider(
            "Unison", lyrics_utils.fetch_apple_music_ttml, "Unpublished Song", "Fixture Artist", timeout=1
        )
        assert no_result["acquisition_status"] == "NOT_FOUND"

        monkeypatch.setattr(lyrics_utils, "_provider_urlopen", lambda *_args, **_kwargs: response(changed))
        parser_failure = lyrics_utils.invoke_lyrics_provider(
            "Unison", lyrics_utils.fetch_apple_music_ttml, "Changed Schema Song", "Fixture Artist", timeout=1
        )
        assert parser_failure["acquisition_status"] == "PARSER_ERROR"

    def test_vocadb_api_fixture_keeps_original_translation_and_romanization_separate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import lyrics_utils

        root = Path(__file__).parent / "tests" / "fixtures" / "lyrics" / "vocadb"
        valid = (root / "search.json").read_bytes()
        empty = (root / "empty.json").read_bytes()
        changed = (root / "changed.json").read_bytes()

        class FixtureResponse:
            status = 200

            def __init__(self, body: bytes) -> None:
                self.body = body

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int = -1) -> bytes:
                return self.body

        def open_body(body: bytes):
            def open_fixture(*_args: object, **_kwargs: object) -> FixtureResponse:
                if getattr(lyrics_utils._LYRICS_PROVIDER_STATE, "active", False):
                    lyrics_utils._LYRICS_PROVIDER_STATE.responses += 1
                return FixtureResponse(body)

            return open_fixture

        monkeypatch.setattr(lyrics_utils, "_provider_urlopen", open_body(valid))
        result = lyrics_utils.invoke_lyrics_provider(
            "VocaDB", lyrics_utils.fetch_vocadb, "夜に駆ける", "YOASOBI", timeout=1
        )
        assert result["trackName"] == "夜に駆ける"
        assert result["artistName"] == "YOASOBI"
        assert result["duration"] == 246
        assert result["language"] == "ja"
        assert result["translationLanguage"] == "en"
        assert result["romanizationLanguage"] == "ja-Latn"
        assert "夜に駆ける歌詞" in result["plainLyrics"]
        assert "translated lyric line" in result["translationLyrics"]
        assert "Yoru ni kakeru" in result["romajiLyrics"]
        assert json.loads(result["providerRaw"])["lyrics"][0]["cultureCode"] == "ja"

        selected = lyrics_pipeline.choose_provider_result(
            [result], title="夜に駆ける", artist="YOASOBI", album="", duration=246, language="ja"
        )
        assert selected is not None
        resolution = lyrics_pipeline.materialize_selected(tmp_path / "夜.flac", selected)
        assert resolution.paths["plain"].read_text(encoding="utf-8").startswith("夜に駆ける歌詞")
        assert resolution.paths["translation"].read_text(encoding="utf-8").startswith("The first translated")
        assert resolution.paths["romanization"].read_text(encoding="utf-8").startswith("Yoru ni kakeru")
        assert resolution.paths["raw"].suffix == ".json"

        monkeypatch.setattr(lyrics_utils, "_provider_urlopen", open_body(empty))
        no_result = lyrics_utils.invoke_lyrics_provider(
            "VocaDB", lyrics_utils.fetch_vocadb, "No Such Song", "Unknown Artist", timeout=1
        )
        assert no_result["acquisition_status"] == "NOT_FOUND"

        monkeypatch.setattr(lyrics_utils, "_provider_urlopen", open_body(changed))
        parser_failure = lyrics_utils.invoke_lyrics_provider(
            "VocaDB", lyrics_utils.fetch_vocadb, "Changed", "Unknown", timeout=1
        )
        assert parser_failure["acquisition_status"] == "PARSER_ERROR"

    def test_metal_archives_fixture_parser_matches_song_and_band_and_classifies_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import urllib.error
        import urllib.request

        from lyrics_utils import (
            fetch_metal_archives,
            parse_metal_archives_lyrics_page,
            parse_metal_archives_search,
        )

        root = Path(__file__).parent / "tests" / "fixtures" / "lyrics" / "metal_archives"
        search = (root / "search.json").read_bytes()
        lyrics_html = (root / "valid.html").read_bytes()
        rows = parse_metal_archives_search(search, "Requested Song (Remastered)", "Example Artist")
        assert len(rows) == 1
        assert rows[0]["id"] == "100"
        assert rows[0]["title"] == "Requested Song (Remastered)"
        assert rows[0]["artist"] == "Example Artist"
        assert (
            parse_metal_archives_search((root / "no_results.json").read_bytes(), "Requested Song", "Example Artist")
            == []
        )
        with pytest.raises(TypeError, match="aaData"):
            parse_metal_archives_search((root / "changed.json").read_bytes(), "Requested Song", "Example Artist")
        parsed = parse_metal_archives_lyrics_page(lyrics_html)
        assert "First line with & preserved" in parsed
        assert parsed.count("Repeated chorus") == 2
        assert "Unrelated footer" not in parsed
        with pytest.raises(ValueError, match="container"):
            parse_metal_archives_lyrics_page((root / "changed.html").read_bytes())

        class Response:
            status = 200
            headers: ClassVar[dict[str, str]] = {}

            def __init__(self, body: bytes) -> None:
                self.body = body

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int = -1) -> bytes:
                return self.body

        def open_valid(request: urllib.request.Request, timeout: float = 6) -> Response:
            if "ajax-view-lyrics" in request.full_url:
                return Response(lyrics_html)
            return Response(search)

        monkeypatch.setattr(urllib.request, "urlopen", open_valid)
        result = fetch_metal_archives("Requested Song (Remastered)", "Example Artist")
        assert result and result["found"] is True
        assert result["provider_track_id"] == "100"
        assert result["identity_validated"] is True

        def forbidden(_request: urllib.request.Request, timeout: float = 6) -> Response:
            raise urllib.error.HTTPError(
                "https://www.metal-archives.com/search", 403, "forbidden", _http_headers(), None
            )

        monkeypatch.setattr(urllib.request, "urlopen", forbidden)
        blocked = fetch_metal_archives("Requested Song", "Example Artist")
        assert blocked and blocked["acquisition_status"] == "BLOCKED"

    def test_petitlyrics_fixtures_preserve_text_and_exclude_wrong_versions(self) -> None:
        from lyrics_utils import parse_petitlyrics_page, parse_petitlyrics_search

        root = Path(__file__).parent / "tests" / "fixtures" / "lyrics" / "petitlyrics"
        rows = parse_petitlyrics_search(
            (root / "search.html").read_text(encoding="utf-8"), "First Love", "宇多田ヒカル"
        )
        assert [row["id"] for row in rows] == ["13"]
        parsed = parse_petitlyrics_page((root / "valid.html").read_text(encoding="utf-8"))
        assert "[Verse 1]" in parsed and "日本語の歌詞 & symbols" in parsed
        assert parsed.count("Repeated chorus") == 2
        assert "Unrelated footer" not in parsed and "untrustedScript" not in parsed
        with pytest.raises(ValueError, match="canvas"):
            parse_petitlyrics_page((root / "changed.html").read_text(encoding="utf-8"))

    @pytest.mark.parametrize(
        ("status", "classification"), [(403, "BLOCKED"), (429, "RATE_LIMITED"), (503, "TRANSIENT_ERROR")]
    )
    def test_petitlyrics_http_failures_are_not_no_result(
        self, monkeypatch: pytest.MonkeyPatch, status: int, classification: str
    ) -> None:
        import urllib.error
        import urllib.request

        from lyrics_utils import fetch_petitlyrics

        def fail(request: urllib.request.Request, timeout: float = 6) -> object:
            raise urllib.error.HTTPError(request.full_url, status, "fixture error", _http_headers(), None)

        monkeypatch.setattr(urllib.request, "urlopen", fail)
        result = fetch_petitlyrics("First Love", "宇多田ヒカル")
        assert result and result["acquisition_status"] == classification

    def test_petitlyrics_valid_nonmatch_is_legitimate_no_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.request

        from lyrics_utils import fetch_petitlyrics

        fixture = Path(__file__).parent / "tests" / "fixtures" / "lyrics" / "petitlyrics" / "search.html"
        content = fixture.read_bytes()

        class Response:
            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _size: int = -1) -> bytes:
                return content

        monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
        result = fetch_petitlyrics("No Such Song", "No Such Artist")
        assert result and result["acquisition_status"] == "NOT_FOUND"

    def test_tekstowo_fixture_parsers_match_identity_and_fail_on_changed_structure(self) -> None:
        from lyrics_utils import parse_tekstowo_lyrics, parse_tekstowo_search

        root = Path(__file__).parent / "tests" / "fixtures" / "lyrics" / "tekstowo"
        search = (root / "search.html").read_text(encoding="utf-8")
        candidates = parse_tekstowo_search(search, "Długość dźwięku samotności", "Myslovitz")
        assert len(candidates) == 1
        assert candidates[0]["title"] == "Długość dźwięku samotności"
        assert candidates[0]["artist"] == "Myslovitz"
        unrelated = (root / "no_results.html").read_text(encoding="utf-8")
        assert parse_tekstowo_search(unrelated, "Długość dźwięku samotności", "Myslovitz") == []
        empty_region = (root / "empty_results.html").read_text(encoding="utf-8")
        assert parse_tekstowo_search(empty_region, "Song", "Artist") == []
        with pytest.raises(ValueError, match="search results"):
            parse_tekstowo_search((root / "changed.html").read_text(encoding="utf-8"), "Song", "Artist")
        raw, lyrics = parse_tekstowo_lyrics((root / "valid.html").read_text(encoding="utf-8"))
        assert "<br" in raw and "I nawet kiedy będę sam" in lyrics
        assert "Długość dźwięku samotności" not in lyrics
        with pytest.raises(ValueError, match="lyrics container"):
            parse_tekstowo_lyrics((root / "changed.html").read_text(encoding="utf-8"))

    def test_semantic_provider_scraper_parsers_use_fixtures_and_fail_closed(self) -> None:
        from lyrics_utils import (
            _amdm_vocal_text,
            parse_1ting_lyrics,
            parse_1ting_search,
            parse_amdm_lyrics,
            parse_amdm_search,
            parse_megalobiz_lyrics,
            parse_megalobiz_search,
            parse_rentanadviser_lyrics,
            parse_rentanadviser_search,
        )

        base = Path(__file__).parent / "tests" / "fixtures" / "lyrics"
        song, artist = "Bohemian Rhapsody", "Queen"
        mega = base / "megalobiz"
        mega_results = parse_megalobiz_search((mega / "search.html").read_text(), song, artist)
        assert mega_results[0]["id"] == "987654"
        assert parse_megalobiz_lyrics((mega / "lyrics.html").read_text(), "987654").startswith("[00:00.00]")
        with pytest.raises(ValueError, match="details container"):
            parse_megalobiz_lyrics((mega / "changed.html").read_text(), "987654")

        rent = base / "rentanadviser"
        rent_results = parse_rentanadviser_search((rent / "search.html").read_text(), song, artist)
        assert rent_results == [{"id": "12345", "title": song, "artist": artist}]
        assert "[00:04.20]Is this just fantasy?" in parse_rentanadviser_lyrics((rent / "lyrics.html").read_text())
        with pytest.raises(ValueError, match="lyrics field"):
            parse_rentanadviser_lyrics((rent / "changed.html").read_text())

        amdm = base / "amdm"
        amdm_results = parse_amdm_search((amdm / "search.html").read_text(), song, artist)
        assert amdm_results[0]["id"] == "123"
        chord_sheet = parse_amdm_lyrics((amdm / "lyrics.html").read_text())
        display = _amdm_vocal_text(chord_sheet)
        assert "Is this the real life?" in display
        assert "Am       F" not in display
        assert "G        C" not in display
        assert "Am       F" in chord_sheet
        with pytest.raises(ValueError, match="semantic chord-sheet"):
            parse_amdm_lyrics((amdm / "changed.html").read_text())

        one_ting = base / "1ting"
        one_results = parse_1ting_search((one_ting / "search.html").read_text(), song, artist)
        assert one_results[0]["id"] == "123"
        assert parse_1ting_lyrics((one_ting / "lrc.html").read_text())[0] == "lrc"
        assert parse_1ting_lyrics((one_ting / "plain.html").read_text()) == (
            "plain",
            "First lyric line\nSecond lyric line",
        )
        with pytest.raises(ValueError, match="container"):
            parse_1ting_lyrics((one_ting / "changed.html").read_text())

    def test_vk_topic_scraper_requires_search_and_topic_identity(self) -> None:
        from lyrics_utils import parse_vk_topics_page, parse_vk_topics_search

        root = Path(__file__).parent / "tests" / "fixtures" / "lyrics" / "vk_topics"
        song, artist = "Bohemian Rhapsody", "Queen"
        topics = parse_vk_topics_search((root / "search.html").read_text(), song, artist)
        assert topics == [{"url": "https://vk.com/topic-100_200", "title": song, "artist": artist}]
        matched = parse_vk_topics_page((root / "topic.html").read_text(), song, artist)
        assert matched is not None and "Is this just fantasy?" in matched["text"]
        with pytest.raises(ValueError, match="matching title/artist"):
            parse_vk_topics_page((root / "wrong_topic.html").read_text(), song, artist)
        with pytest.raises(ValueError, match="result state"):
            parse_vk_topics_search((root / "changed.html").read_text(), song, artist)

    def test_vk_lyrics_use_configured_api_token_not_web_cookie_scraping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import json
        import urllib.request

        from lyrics_utils import fetch_vk_music_lyrics

        monkeypatch.setattr(Path, "home", lambda: Path("/tmp/audiophile-test-home"))
        monkeypatch.delenv("VK_TOKEN", raising=False)
        monkeypatch.setenv("VK_COOKIE", "session-cookie-must-not-enable-scraping")
        no_auth = fetch_vk_music_lyrics("Song", "Artist")
        assert no_auth and no_auth["acquisition_status"] == "AUTHENTICATION_ERROR"

        monkeypatch.setenv("VK_TOKEN", "test-token")

        class Response:
            status = 200

            def __init__(self, payload: object, url: str) -> None:
                self.body = json.dumps(payload).encode()
                self.url = url
                self.headers: dict[str, str] = {}

            def read(self, _size: int = -1) -> bytes:
                return self.body

            def geturl(self) -> str:
                return self.url

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

        def fake_urlopen(request: urllib.request.Request, **_kwargs: object) -> Response:
            url = request.full_url
            if "/audio.search?" in url:
                return Response(
                    {
                        "response": {
                            "items": [
                                {
                                    "id": 55,
                                    "lyrics_id": 66,
                                    "title": "Bohemian Rhapsody",
                                    "artist": "Queen",
                                    "duration": 354,
                                    "album": {"title": "A Night at the Opera"},
                                }
                            ]
                        }
                    },
                    url,
                )
            return Response({"response": {"text": "Is this the real life?\nIs this just fantasy?"}}, url)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        result = fetch_vk_music_lyrics("Bohemian Rhapsody", "Queen", duration=354)
        assert result and result["found"]
        assert result["provider_track_id"] == "55"
        assert result["provider_lyrics_id"] == "66"
        assert result["plainLyrics"] == "Is this the real life?\nIs this just fantasy?"

    def test_genius_fixture_fetch_preserves_identity_and_typed_failures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.error
        import urllib.request

        from lyrics_utils import fetch_genius

        fixture_root = Path(__file__).parent / "tests" / "fixtures" / "lyrics" / "genius"
        lyrics_page = (fixture_root / "valid.html").read_bytes()
        changed_page = (fixture_root / "changed.html").read_bytes()
        search = {
            "response": {
                "sections": [
                    {
                        "type": "song",
                        "hits": [
                            {
                                "type": "song",
                                "result": {
                                    "id": 77,
                                    "title": "Song",
                                    "primary_artist": {"name": "Artist"},
                                    "primary_album": {"name": "Album"},
                                    "duration_ms": 120_000,
                                    "url": "https://genius.com/song",
                                },
                            }
                        ],
                    }
                ],
            },
        }

        class Response:
            status = 200

            def __init__(self, body: bytes) -> None:
                self.body = body

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int = -1) -> bytes:
                return self.body

        def open_valid(request: urllib.request.Request, timeout: float = 6) -> Response:
            if "api/search" in request.full_url:
                return Response(json.dumps(search).encode("utf-8"))
            return Response(lyrics_page)

        monkeypatch.setattr(urllib.request, "urlopen", open_valid)
        candidate = fetch_genius("Song", "Artist", return_candidate=True)
        assert isinstance(candidate, dict) and candidate["provider_track_id"] == "77"
        assert candidate["albumName"] == "Album" and candidate["duration"] == 120
        assert candidate["identity_validated"] is True
        assert "Word Embed belongs to this song" in candidate["plainLyrics"]

        def open_changed(request: urllib.request.Request, timeout: float = 6) -> Response:
            if "api/search" in request.full_url:
                return Response(json.dumps(search).encode("utf-8"))
            return Response(changed_page)

        monkeypatch.setattr(urllib.request, "urlopen", open_changed)
        parser_result = fetch_genius("Song", "Artist", return_candidate=True)
        assert isinstance(parser_result, dict) and parser_result["acquisition_status"] == "PARSER_ERROR"

        def rate_limited(_request: urllib.request.Request, timeout: float = 6) -> Response:
            raise urllib.error.HTTPError("https://genius.test", 429, "rate limit", _http_headers(), None)

        monkeypatch.setattr(urllib.request, "urlopen", rate_limited)
        limited = fetch_genius("Song", "Artist", return_candidate=True)
        assert isinstance(limited, dict) and limited["acquisition_status"] == "RATE_LIMITED"

    def test_provider_identity_is_retained_for_scoring(self) -> None:
        result = {
            "source": "LRCLIB",
            "id": 1,
            "trackName": "Song",
            "artistName": "Artist",
            "albumName": "Album",
            "duration": 120,
            "syncedLyrics": "[00:10.00]A lyric line\n[01:50.00]Another lyric line",
        }
        selected = lyrics_pipeline.choose_provider_result(
            [result],
            title="Song",
            artist="Artist",
            album="Album",
            duration=120,
        )
        assert selected and selected.candidate.title == "Song"
        assert selected.candidate.metadata["provider_item_id"] == "1"

    def test_timing_maps_are_not_deduplicated_as_plain_text(self) -> None:
        results = [
            {
                "source": "a",
                "trackName": "Song",
                "artistName": "Artist",
                "duration": 120,
                "syncedLyrics": "[00:10.00]One line\n[01:40.00]Two line",
            },
            {
                "source": "b",
                "trackName": "Song",
                "artistName": "Artist",
                "duration": 120,
                "syncedLyrics": "[00:11.00]One line\n[01:41.00]Two line",
            },
        ]
        selected = lyrics_pipeline.choose_provider_result(
            results, title="Song", artist="Artist", album="", duration=120
        )
        assert selected is not None

    def test_local_plain_sidecar_is_immutable_and_blocks_remote(self, tmp_path: Path) -> None:
        base = tmp_path / "01 - Song.flac"
        sidecar = base.with_suffix(".txt")
        original = "[Verse 1]\nA meaningful line\n\nA second stanza line\n"
        sidecar.write_text(original, encoding="utf-8")
        resolution = lyrics_pipeline.inspect_local(base, {}, 240)
        assert resolution and resolution.local and resolution.status == "PRESERVED"
        assert sidecar.read_text(encoding="utf-8") == original

    def test_correct_plain_beats_wrong_synchronized_candidate(self) -> None:
        correct = LyricsCandidate(
            "plain",
            "A correct lyric line\nAnother correct lyric line",
            title="Song",
            artist="Artist",
            album="Album",
            duration=240,
        )
        wrong_sync = LyricsCandidate(
            "sync",
            "[00:01.00]Wrong version line\n[00:02.00]Still wrong",
            synced=True,
            title="Song (Live)",
            artist="Different Artist",
            duration=100,
        )
        selected = select_best(
            [wrong_sync, correct],
            title="Song",
            artist="Artist",
            album="Album",
            duration=240,
        )
        assert selected and selected.candidate.provider == "plain"

    def test_timestamp_regression_and_overrun_are_rejected(self) -> None:
        valid, reasons = validate_lrc(
            "[00:05.00]Later\n[00:04.00]Earlier\n[04:30.00]Past end",
            duration=120,
        )
        assert not valid
        assert any("regression" in reason for reason in reasons)
        assert any("exceed" in reason for reason in reasons)

    def test_simultaneous_distinct_vocal_lines_are_valid(self) -> None:
        valid, reasons = validate_lrc(
            "[00:01.00]First line\n[00:01.00]Different line\n[00:02.00]Third line",
            duration=120,
        )
        assert valid, reasons

    def test_long_intro_and_instrumental_gap_are_warnings_not_sync_failures(self) -> None:
        candidate = LyricsCandidate(
            "fixture",
            "[01:30.00]A first vocal line after an instrumental intro\n"
            "[04:10.00]A final vocal line after a long instrumental passage",
            synced=True,
            title="Song",
            artist="Artist",
            album="Album",
            duration=300,
        )
        assessment = assess_candidate(candidate, title="Song", artist="Artist", album="Album", duration=300)
        assert assessment.valid
        assert any("instrumental intro" in warning for warning in assessment.warnings)
        assert any("instrumental section" in warning for warning in assessment.warnings)

    def test_remote_materialization_never_overwrites_curated_sidecar(self, tmp_path: Path) -> None:
        base = tmp_path / "song.flac"
        curated = base.with_suffix(".txt")
        curated.write_text("Curated line one\nCurated line two\n", encoding="utf-8")
        candidate = LyricsCandidate(
            "Provider",
            "Remote line one\nRemote line two",
            title="Song",
            artist="Artist",
            album="Album",
            duration=200,
            metadata={"result": {"plainLyrics": "Remote line one\nRemote line two"}},
        )
        assessment = select_best([candidate], title="Song", artist="Artist", album="", duration=200)
        assert assessment
        resolution = lyrics_pipeline.materialize_selected(base, assessment)
        assert curated.read_text(encoding="utf-8") == "Curated line one\nCurated line two\n"
        assert resolution.paths["plain"] != curated

        staging = tmp_path / "pending"
        staging.mkdir()
        staged = lyrics_pipeline.materialize_selected(staging / base.name, assessment, existing_base=base)
        assert staged.paths["plain"] == resolution.paths["plain"]
        assert not list(staging.iterdir())
        resolution.paths["plain"].unlink()
        staged = lyrics_pipeline.materialize_selected(staging / base.name, assessment, existing_base=base)
        assert staged.paths["plain"].parent == staging
        assert staged.paths["plain"].name != curated.name
        assert not (tmp_path / staged.paths["plain"].name).exists()
        assert curated.read_text(encoding="utf-8") == "Curated line one\nCurated line two\n"

    def test_line_sync_materialization_does_not_create_fake_word_or_ttml_sidecars(self, tmp_path: Path) -> None:
        lrc = "[00:01.20]A complete timed line\n[00:03.40]Another complete timed line"
        candidate = LyricsCandidate(
            "fixture",
            lrc,
            synced=True,
            title="Song",
            artist="Artist",
            album="Album",
            duration=30,
            plain_lyrics=lrc_to_plain(lrc),
            synced_lyrics=lrc,
            metadata={
                "result": {
                    "format": "lrc",
                    "has_word_sync": False,
                    "plainLyrics": lrc_to_plain(lrc),
                    "syncedLyrics": lrc,
                    "enhancedLyrics": lrc,
                    "ttmlLyrics": "<generated line-level TTML>",
                    "ttml_native": False,
                }
            },
        )
        assessment = select_best([candidate], title="Song", artist="Artist", album="Album", duration=30)
        assert assessment is not None
        resolution = lyrics_pipeline.materialize_selected(tmp_path / "song.flac", assessment)
        assert "synced" in resolution.paths
        assert "enhanced" not in resolution.paths
        assert "ttml" not in resolution.paths

    def test_nested_provider_alternative_is_scored(self) -> None:
        good = {
            "source": "Exact provider",
            "trackName": "Song",
            "artistName": "Artist",
            "albumName": "Album",
            "duration": 200,
            "plainLyrics": "A meaningful lyric line\nAnother meaningful lyric line\n" * 15,
        }
        wrong = {**good, "artistName": "Unrelated artist", "alternatives": [good]}
        root = {**wrong, "alternatives": [wrong]}
        selected = lyrics_pipeline.choose_provider_result(
            [root], title="Song", artist="Artist", album="Album", duration=200
        )
        assert selected is not None
        assert selected.candidate.artist == "Artist"

    def test_adapter_validation_does_not_reduce_exact_match_confidence(self) -> None:
        candidate = LyricsCandidate(
            "Provider",
            "A meaningful lyric line\nAnother meaningful lyric line",
            title="Song",
            artist="Artist",
            album="Album",
            duration=200,
            metadata={"adapter_validated": True},
        )
        assessment = select_best([candidate], title="Song", artist="Artist", album="Album", duration=200)
        assert assessment is not None
        assert assessment.match_confidence == 99

    def test_provider_raw_sidecar_is_not_trimmed_or_given_a_newline(self, tmp_path: Path) -> None:
        raw = "[Verse 1]\r\nLine with punctuation… and spacing  "
        candidate = LyricsCandidate(
            "fixture provider",
            "A meaningful lyric line\nAnother meaningful lyric line",
            title="Song",
            artist="Artist",
            duration=120,
            raw_lyrics=raw,
            metadata={
                "result": {"providerRaw": raw, "plainLyrics": "A meaningful lyric line\nAnother meaningful lyric line"}
            },
        )
        assessment = select_best([candidate], title="Song", artist="Artist", album="", duration=120)
        assert assessment
        resolution = lyrics_pipeline.materialize_selected(tmp_path / "song.flac", assessment)
        assert resolution.paths["raw"].read_bytes() == raw.encode("utf-8")
        manifest = json.loads(resolution.paths["manifest"].read_text(encoding="utf-8"))
        import hashlib

        assert manifest["artifacts"]["raw"]["sha256"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()
        assert manifest["artifacts"]["raw"]["file"] == resolution.paths["raw"].name
        assert manifest["assessment"]["match"] == assessment.match_confidence
        assert manifest["provider"] == candidate.provider
        assert manifest["schema_version"] == 2
        assert manifest["parser_version"] == PARSER_VERSION

    def test_kugou_krc_decompression_is_bounded_and_strict(self) -> None:
        import base64
        import zlib

        from lyrics_utils import KUGOU_KRC_KEY, decode_kugou_krc

        source = "[00:01.00]<00:01.00>Preserved 日本語"
        compressed = zlib.compress(source.encode("utf-8"))
        encrypted = bytes(byte ^ KUGOU_KRC_KEY[index % len(KUGOU_KRC_KEY)] for index, byte in enumerate(compressed))
        payload = base64.b64encode(b"krc1" + encrypted).decode("ascii")
        assert decode_kugou_krc(payload) == source
        with pytest.raises(ValueError, match="limit"):
            decode_kugou_krc(payload, maximum=4)
        truncated = base64.b64encode(b"krc1" + encrypted[:-2]).decode("ascii")
        with pytest.raises(ValueError, match="incomplete"):
            decode_kugou_krc(truncated)

    def test_opaque_syncedlyrics_aggregate_scraper_is_disabled(self) -> None:
        from lyrics_utils import fetch_syncedlyrics_fallback

        result = fetch_syncedlyrics_fallback("Song", "Artist")
        assert result is not None
        assert result["acquisition_status"] == "UNSUPPORTED_PROVIDER"
        assert "explicit provider adapter" in result["error"]

    def test_diagnostic_only_legacy_lyrics_scrapers_report_unsupported(self) -> None:
        from lyrics_utils import fetch_azlyrics, fetch_lyrics_ovh, fetch_songlyrics

        for result in (
            fetch_azlyrics("Song", "Artist"),
            fetch_songlyrics("Song", "Artist"),
            fetch_lyrics_ovh("Song", "Artist"),
        ):
            assert result["acquisition_status"] == "UNSUPPORTED_PROVIDER"

    def test_kugou_api_fixtures_keep_krc_lrc_identity_and_failure_states(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.error
        import urllib.parse
        import urllib.request

        import lyrics_utils

        root = Path(__file__).parent / "tests" / "fixtures" / "lyrics" / "kugou"
        bodies = {
            "search": (root / "search.json").read_bytes(),
            "lyrics": (root / "lyric_search.json").read_bytes(),
            "krc": (root / "download_krc.json").read_bytes(),
            "lrc": (root / "download_lrc.json").read_bytes(),
            "empty": (root / "no_results.json").read_bytes(),
            "changed": (root / "changed.json").read_bytes(),
        }

        class FixtureResponse:
            status = 200

            def __init__(self, body: bytes, url: str) -> None:
                self.body = body
                self.url = url

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int = -1) -> bytes:
                return self.body

            def geturl(self) -> str:
                return self.url

        requests: list[str] = []

        def open_fixture(request: urllib.request.Request, timeout: int = 6) -> FixtureResponse:
            del timeout
            url = request.full_url
            requests.append(url)
            if "/api/v3/search/song?" in url:
                body = bodies["search"]
            elif "/lyrics.kugou.com/search?" in url or "lyrics.kugou.com/search?" in url:
                body = bodies["lyrics"]
            elif "fmt=krc" in url:
                body = bodies["krc"]
            elif "fmt=lrc" in url:
                body = bodies["lrc"]
            else:
                raise AssertionError(f"unexpected Kugou request: {url}")
            return FixtureResponse(body, url)

        monkeypatch.setattr(lyrics_utils, "_provider_urlopen", open_fixture)
        found = lyrics_utils.invoke_lyrics_provider(
            "Kugou", lyrics_utils.fetch_kugou, "Fixture Song", "Fixture Artist", duration=120, timeout=1
        )
        assert found["found"] is True
        assert found["source"] == "Kugou KRC"
        assert found["provider_track_id"] == "fixture-track-hash"
        assert found["provider_lyrics_id"] == "fixture-lyrics-id"
        assert found["trackName"] == "Fixture Song"
        assert found["artistName"] == "Fixture Artist"
        assert found["albumName"] == "Fixture Album"
        assert found["duration"] == 120
        assert found["has_word_sync"] is True
        assert "First  line" in found["plainLyrics"]
        assert len(found["alternatives"]) == 1
        assert found["alternatives"][0]["source"] == "Kugou LRC"
        candidate = lyrics_pipeline.provider_candidate(found, fallback_provider="Kugou", duration=120)
        assert candidate is not None and candidate.provider_track_id == "fixture-track-hash"
        assert all(urllib.parse.urlparse(url).scheme == "https" for url in requests)
        assert urllib.parse.parse_qs(urllib.parse.urlparse(requests[0]).query)["keyword"] == [
            "Fixture Artist Fixture Song"
        ]

        monkeypatch.setattr(
            lyrics_utils,
            "_provider_urlopen",
            lambda request, timeout=6: FixtureResponse(bodies["empty"], request.full_url),
        )
        empty = lyrics_utils.invoke_lyrics_provider(
            "Kugou", lyrics_utils.fetch_kugou, "No Song", "No Artist", timeout=1
        )
        assert empty["acquisition_status"] == "NOT_FOUND"

        monkeypatch.setattr(
            lyrics_utils,
            "_provider_urlopen",
            lambda request, timeout=6: FixtureResponse(bodies["changed"], request.full_url),
        )
        changed = lyrics_utils.invoke_lyrics_provider(
            "Kugou", lyrics_utils.fetch_kugou, "Changed Song", "No Artist", timeout=1
        )
        assert changed["acquisition_status"] == "PARSER_ERROR"

        def rate_limited(request: urllib.request.Request, timeout: int = 6) -> FixtureResponse:
            del timeout
            raise urllib.error.HTTPError(request.full_url, 429, "limited", _http_headers(Retry_After="4"), None)

        monkeypatch.setattr(lyrics_utils, "_provider_urlopen", rate_limited)
        limited = lyrics_utils.invoke_lyrics_provider(
            "Kugou", lyrics_utils.fetch_kugou, "Limited Song", "No Artist", timeout=1
        )
        assert limited["acquisition_status"] == "RATE_LIMITED"

    def test_yandex_api_fixtures_preserve_candidates_and_typed_failures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.error
        import urllib.parse

        import lyrics_utils

        root = Path(__file__).parent / "tests" / "fixtures" / "lyrics" / "yandex"
        bodies = {
            "search": (root / "search.json").read_bytes(),
            "supplement_501": (root / "supplement_501.json").read_bytes(),
            "supplement_502": (root / "supplement_502.json").read_bytes(),
            "empty": (root / "empty.json").read_bytes(),
            "changed": (root / "changed.json").read_bytes(),
        }

        class FixtureResponse:
            status = 200

            def __init__(self, body: bytes) -> None:
                self.body = body

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int = -1) -> bytes:
                return self.body

        monkeypatch.delenv("YANDEX_MUSIC_TOKEN", raising=False)
        monkeypatch.delenv("YANDEX_TOKEN", raising=False)
        monkeypatch.setattr(lyrics_utils.os.path, "isfile", lambda _path: False)
        unconfigured = lyrics_utils.fetch_yandex_music("Song", "Artist")
        assert unconfigured is not None
        assert unconfigured["acquisition_status"] == "NOT_CONFIGURED"
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "fixture-token")

        def open_fixture(request: urllib.request.Request, timeout: int = 6) -> FixtureResponse:
            del timeout
            url = request.full_url
            parsed = urllib.parse.urlparse(url)
            if parsed.path == "/search":
                query = urllib.parse.parse_qs(parsed.query).get("text", [""])[0]
                if "No Result Song" in query:
                    body = bodies["empty"]
                elif "Changed Song" in query:
                    body = bodies["changed"]
                else:
                    body = bodies["search"]
            elif parsed.path.endswith("/501/supplement"):
                body = bodies["supplement_501"]
            elif parsed.path.endswith("/502/supplement"):
                body = bodies["supplement_502"]
            else:
                raise AssertionError(f"unexpected Yandex request: {url}")
            return FixtureResponse(body)

        monkeypatch.setattr(lyrics_utils, "_provider_urlopen", open_fixture)
        found = lyrics_utils.invoke_lyrics_provider(
            "Yandex Music",
            lyrics_utils.fetch_yandex_music,
            "Fixture Song",
            "Fixture Artist",
            duration=120,
            timeout=1,
        )
        assert found["found"] is True
        assert found["provider_track_id"] == "501"
        assert found["trackName"] == "Fixture Song"
        assert found["artistName"] == "Fixture Artist"
        assert found["albumName"] == "Fixture Album"
        assert found["duration"] == 120
        assert found["language"] == "en"
        assert found["syncedLyrics"].startswith("[00:01.23]")
        assert len(found["alternatives"]) == 1
        assert found["alternatives"][0]["provider_track_id"] == "502"
        raw = json.loads(found["providerRaw"])
        assert raw["lyrics"]["full_lyrics"].startswith("[00:01.23]")

        no_result = lyrics_utils.invoke_lyrics_provider(
            "Yandex Music", lyrics_utils.fetch_yandex_music, "No Result Song", "Fixture Artist", timeout=1
        )
        assert no_result["acquisition_status"] == "NOT_FOUND"
        changed = lyrics_utils.invoke_lyrics_provider(
            "Yandex Music", lyrics_utils.fetch_yandex_music, "Changed Song", "Fixture Artist", timeout=1
        )
        assert changed["acquisition_status"] == "PARSER_ERROR"

        def rate_limited(request: urllib.request.Request, timeout: int = 6) -> FixtureResponse:
            del timeout
            raise urllib.error.HTTPError(request.full_url, 429, "limited", _http_headers(Retry_After="4"), None)

        monkeypatch.setattr(lyrics_utils, "_provider_urlopen", rate_limited)
        limited = lyrics_utils.invoke_lyrics_provider(
            "Yandex Music", lyrics_utils.fetch_yandex_music, "Limited Song", "Fixture Artist", timeout=1
        )
        assert limited["acquisition_status"] == "RATE_LIMITED"

    def test_netease_keeps_and_scores_all_identity_matched_recordings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import urllib.parse
        import urllib.request

        import lyrics_utils

        root = Path(__file__).parent / "tests" / "fixtures" / "lyrics" / "netease"
        search_body = (root / "search.json").read_bytes()
        lyric_bodies = {
            "8101": (root / "lyrics_8101.json").read_bytes(),
            "8102": (root / "lyrics_8102.json").read_bytes(),
        }

        class FixtureResponse:
            status = 200

            def __init__(self, body: bytes, url: str) -> None:
                self.body = body
                self.url = url

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int = -1) -> bytes:
                return self.body

            def geturl(self) -> str:
                return self.url

        def open_fixture(request: urllib.request.Request, timeout: float = 4) -> FixtureResponse:
            del timeout
            url = request.full_url
            if "/api/cloudsearch/pc" in url:
                body = search_body
            elif "/api/song/lyric?" in url:
                track_id = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["id"][0]
                body = lyric_bodies[track_id]
            else:
                raise AssertionError(f"unexpected NetEase request: {url}")
            return FixtureResponse(body, url)

        monkeypatch.setattr(lyrics_utils, "_provider_urlopen", open_fixture)
        monkeypatch.setattr(lyrics_utils, "_PROVIDER_RESULT_CACHE", JsonCache(tmp_path / "netease-cache"))
        for name in (
            "fetch_apple_music_ttml",
            "fetch_musixmatch",
            "fetch_chorus_chart",
            "fetch_ultrastar_db",
            "fetch_qqmusic",
            "fetch_kugou",
            "fetch_petitlyrics",
            "fetch_yandex_music",
            "fetch_vk_music_lyrics",
            "fetch_lrclib",
            "fetch_megalobiz",
            "fetch_rentanadviser",
            "fetch_genius",
            "fetch_vk_topics",
            "fetch_amdm_lyrics",
            "fetch_bandcamp_lyrics",
            "fetch_baidu_lyrics",
            "fetch_darklyrics",
            "fetch_metal_archives",
            "fetch_vocadb",
            "fetch_tekstowo",
        ):
            monkeypatch.setattr(lyrics_utils, name, lambda *_args, **_kwargs: None)

        result = lyrics_utils.fetch_lyrics_cascade(
            "Fixture Song",
            "Fixture Artist",
            album="Fixture Album",
            duration=181,
            language_branch="LATIN",
        )
        assert result is not None
        expanded = lyrics_pipeline._expand_results([result])
        assert {str(item.get("provider_track_id")) for item in expanded if item.get("provider_track_id")} >= {
            "8101",
            "8102",
        }
        for provider_track_id in ("8101", "8102"):
            assert any(
                item.get("provider_track_id") == provider_track_id and item.get("syncedLyrics") for item in expanded
            )

        selected = lyrics_pipeline.choose_provider_result(
            [result],
            title="Fixture Song",
            artist="Fixture Artist",
            album="Fixture Album",
            duration=181,
        )
        assert selected is not None
        assert selected.candidate.provider_track_id == "8102"
        assert selected.candidate.album == "Fixture Album"

    def test_netease_valid_empty_and_changed_schema_are_distinct(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        import lyrics_utils

        root = Path(__file__).parent / "tests" / "fixtures" / "lyrics" / "netease"
        bodies = {
            "empty": (root / "empty.json").read_bytes(),
            "changed": (root / "changed.json").read_bytes(),
        }
        response_kind = "empty"

        class FixtureResponse:
            status = 200

            def __init__(self, body: bytes, url: str) -> None:
                self.body = body
                self.url = url

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int = -1) -> bytes:
                return self.body

            def geturl(self) -> str:
                return self.url

        def open_fixture(request: urllib.request.Request, timeout: float = 4) -> FixtureResponse:
            del timeout
            if getattr(lyrics_utils._LYRICS_PROVIDER_STATE, "active", False):
                lyrics_utils._LYRICS_PROVIDER_STATE.responses += 1
            return FixtureResponse(bodies[response_kind], request.full_url)

        monkeypatch.setattr(lyrics_utils, "_provider_urlopen", open_fixture)
        monkeypatch.setattr(lyrics_utils, "_PROVIDER_RESULT_CACHE", JsonCache(tmp_path / "netease-outcomes"))
        no_result = lyrics_utils.invoke_lyrics_provider(
            "NetEase", lyrics_utils.search_netease_yrc, "Absent Song", "Absent Artist"
        )
        assert no_result["acquisition_status"] == "NOT_FOUND"

        response_kind = "changed"
        malformed = lyrics_utils.invoke_lyrics_provider(
            "NetEase", lyrics_utils.search_netease_yrc, "Changed Song", "Changed Artist"
        )
        assert malformed["acquisition_status"] == "PARSER_ERROR"

    def test_chorus_chart_fixture_and_search_failure_states(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        import lyrics_utils

        root = Path(__file__).parent / "tests" / "fixtures" / "lyrics" / "chorus"
        parsed = lyrics_utils.parse_clonehero_chart(
            (root / "valid.chart").read_text(encoding="utf-8"),
            song="Fixture Song",
            artist="Fixture Artist",
        )
        assert parsed is not None and parsed["has_word_sync"] is True
        assert "Every word remains intact" in parsed["plainLyrics"]

        response_kind = "search_empty"

        class FixtureResponse:
            status = 200

            def __init__(self, body: bytes, url: str) -> None:
                self.body = body
                self.url = url

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int = -1) -> bytes:
                return self.body

            def geturl(self) -> str:
                return self.url

        def open_fixture(request: urllib.request.Request, timeout: float = 6) -> FixtureResponse:
            del timeout
            if getattr(lyrics_utils._LYRICS_PROVIDER_STATE, "active", False):
                lyrics_utils._LYRICS_PROVIDER_STATE.responses += 1
            return FixtureResponse((root / f"{response_kind}.json").read_bytes(), request.full_url)

        monkeypatch.setattr(lyrics_utils, "_provider_urlopen", open_fixture)
        monkeypatch.setattr(lyrics_utils, "_PROVIDER_RESULT_CACHE", JsonCache(tmp_path / "chorus-cache"))
        empty = lyrics_utils.invoke_lyrics_provider(
            "Chorus", lyrics_utils.fetch_chorus_chart, "Missing Song", "Missing Artist"
        )
        assert empty["acquisition_status"] == "NOT_FOUND"

        response_kind = "search_changed"
        changed = lyrics_utils.invoke_lyrics_provider(
            "Chorus", lyrics_utils.fetch_chorus_chart, "Fixture Song", "Fixture Artist"
        )
        assert changed["acquisition_status"] == "PARSER_ERROR"


class TestCacheAndLayout:
    @pytest.mark.parametrize(
        "failure",
        ["TRANSIENT_ERROR", "RATE_LIMITED", "BLOCKED", "AUTHENTICATION_ERROR", "PARSER_ERROR", "INVALID_RESPONSE"],
    )
    def test_final_lookup_status_preserves_failures(self, failure: str) -> None:
        results = [{"acquisition_status": "NOT_FOUND", "alternatives": [{"acquisition_status": failure}]}]
        assert lyrics_pipeline.unsuccessful_lookup_status(results) == failure
        assert lyrics_pipeline.unsuccessful_lookup_status([{"acquisition_status": "NOT_FOUND"}]) == "NOT_FOUND"
        assert (
            lyrics_pipeline.unsuccessful_lookup_status([None, {"acquisition_status": "NOT_FOUND"}]) == "INDETERMINATE"
        )
        assert lyrics_pipeline.unsuccessful_lookup_status([{"plainLyrics": "Wrong song"}]) == "NO_ACCEPTABLE_CANDIDATE"

    def test_embedded_forms_are_archived_without_overwriting_live_sidecars(self, tmp_path: Path) -> None:
        base = tmp_path / "song.flac"
        curated = base.with_suffix(".txt")
        curated.write_text("Curated existing sidecar", encoding="utf-8")
        staged = tmp_path / "pending"
        staged.mkdir()
        original = "[Verse 1]\r\n日本語の歌詞  \r\nПовторяется строка\r\n"
        translation = "Translated line one\nTranslated line two"
        romanization = "[00:01.25]Romanized line\n[00:04.00]Another line"
        tags = {"LYRICS": [original], "LYRICS_TRANSLATION": [translation], "LYRICS_ROMAJI": [romanization]}
        paths = lyrics_pipeline.materialize_embedded(staged / base.name, tags, existing_base=base, duration=120)
        assert paths["plain"].name != curated.name
        assert paths["plain"].read_bytes() == original.encode("utf-8")
        assert paths["translation"].read_text(encoding="utf-8") == translation
        assert paths["romanization"].suffix == ".lrc"
        assert paths["romanization"].read_text(encoding="utf-8") == romanization
        assert curated.read_text(encoding="utf-8") == "Curated existing sidecar"
        assert tags["LYRICS"] == [original]
        assert lyrics_pipeline.materialize_embedded(staged / base.name, tags, existing_base=base, duration=120) == paths

    def test_invalid_embedded_sync_is_archived_but_not_added_as_plain(self, tmp_path: Path) -> None:
        value = "[00:10.00]Later\n[00:01.00]Earlier"
        base = tmp_path / "song.flac"
        paths = lyrics_pipeline.materialize_embedded(base, {"SYNCEDLYRICS": [value]}, existing_base=base, duration=120)
        assert paths == {}
        assert base.with_name("song.lrc.raw").read_text(encoding="utf-8") == value


class TestMusixmatchDocumentedAPI:
    def test_official_api_fixture_preserves_identity_and_raw_lyrics(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.request

        from lyrics_utils import fetch_musixmatch

        root = Path(__file__).parent / "tests" / "fixtures" / "lyrics" / "musixmatch"
        search_body = (root / "search.json").read_bytes()
        lyrics_body = (root / "lyrics.json").read_bytes()
        monkeypatch.setenv("MUSIXMATCH_API_KEY", "fixture-key")
        monkeypatch.setattr("lyrics_utils._PROVIDER_LIMITERS", {})
        calls: list[urllib.request.Request] = []

        class Response:
            status = 200

            def __init__(self, body: bytes) -> None:
                self.body = body
                self.headers = _http_headers(Content_Type="application/json; charset=utf-8")

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _size: int = -1) -> bytes:
                return self.body

        def fixture_open(request: urllib.request.Request, timeout: float = 6) -> Response:
            calls.append(request)
            assert request.full_url.startswith("https://api.musixmatch.com/ws/1.1/")
            if "/track.search?" in request.full_url:
                return Response(search_body)
            assert "/track.lyrics.get?" in request.full_url
            return Response(lyrics_body)

        monkeypatch.setattr(urllib.request, "urlopen", fixture_open)
        result = fetch_musixmatch(
            "Fixture Song", "Fixture Artist", album="Fixture Album", duration=201, _route="documented"
        )
        assert result is not None
        assert result["found"] is True
        assert result["provider_track_id"] == "42001"
        assert result["isrc"] == "USAAA2400001"
        assert result["duration"] == 201
        assert result["language"] == "en"
        assert result["syncedLyrics"] == ""
        assert result["plainLyrics"].count("Repeated line") == 2
        assert "[Verse 1]" in result["plainLyrics"]
        assert "First line stays…" in result["plainLyrics"]
        assert "NOT for Commercial use" not in result["plainLyrics"]
        assert "NOT for Commercial use" in result["providerRaw"]
        assert len(calls) == 2
        assert "apikey=fixture-key" in calls[0].full_url
        assert "q_track=Fixture+Song" in calls[0].full_url

    def test_official_api_parser_distinguishes_empty_from_schema_change(self) -> None:
        from lyrics_utils import parse_musixmatch_search

        valid_empty = {"message": {"header": {"status_code": 200}, "body": {"track_list": []}}}
        assert parse_musixmatch_search(valid_empty) == []
        changed_schema = {"message": {"header": {"status_code": 200}, "body": {"results": []}}}
        with pytest.raises(ValueError, match="track_list"):
            parse_musixmatch_search(changed_schema)

    def test_explicit_desktop_token_activates_musixmatch_without_browser_access(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import io

        import lyrics_utils
        from lyrics_utils import fetch_musixmatch

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("MUSIXMATCH_API_KEY", raising=False)
        monkeypatch.setenv("MUSIXMATCH_TOKEN", "legacy-browser-token")
        requested: list[str] = []

        def empty_search(request: urllib.request.Request, **_kwargs: object) -> io.BytesIO:
            requested.append(request.full_url)
            return io.BytesIO(b'{"message":{"header":{"status_code":200},"body":{"track_list":[]}}}')

        monkeypatch.setattr(lyrics_utils, "_provider_urlopen", empty_search)
        result = fetch_musixmatch("Song", "Artist")
        assert result and result["acquisition_status"] == "NOT_FOUND"
        assert len(requested) == 1 and "apic-desktop.musixmatch.com" in requested[0]

    def test_musixmatch_http_rate_limit_is_not_no_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.error
        import urllib.request

        from lyrics_utils import fetch_musixmatch

        monkeypatch.setenv("MUSIXMATCH_API_KEY", "fixture-key")

        def rate_limited(request: urllib.request.Request, timeout: float = 6) -> object:
            raise urllib.error.HTTPError(request.full_url, 429, "rate limit", _http_headers(Retry_After="30"), None)

        monkeypatch.setattr(urllib.request, "urlopen", rate_limited)
        result = fetch_musixmatch("Song", "Artist")
        assert result and result["acquisition_status"] == "RATE_LIMITED"

    def test_musixmatch_later_rate_limit_keeps_already_retrieved_candidate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import copy
        import json
        import urllib.error
        import urllib.request

        from lyrics_utils import fetch_musixmatch

        root = Path(__file__).parent / "tests" / "fixtures" / "lyrics" / "musixmatch"
        search_payload = json.loads((root / "search.json").read_text(encoding="utf-8"))
        second = copy.deepcopy(search_payload["message"]["body"]["track_list"][0])
        second["track"].update(track_id=42002, album_name="Other Album")
        search_payload["message"]["body"]["track_list"].append(second)
        lyrics_body = (root / "lyrics.json").read_bytes()
        monkeypatch.setenv("MUSIXMATCH_API_KEY", "fixture-key")
        monkeypatch.setattr("lyrics_utils._PROVIDER_LIMITERS", {})
        lyric_calls = 0

        class Response:
            status = 200
            headers: ClassVar[dict[str, str]] = {}

            def __init__(self, body: bytes) -> None:
                self.body = body

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _size: int = -1) -> bytes:
                return self.body

        def fixture_open(request: urllib.request.Request, timeout: float = 6) -> Response:
            nonlocal lyric_calls
            if "/track.search?" in request.full_url:
                return Response(json.dumps(search_payload).encode())
            lyric_calls += 1
            if lyric_calls == 2:
                raise urllib.error.HTTPError(request.full_url, 429, "rate limit", _http_headers(Retry_After="30"), None)
            return Response(lyrics_body)

        monkeypatch.setattr(urllib.request, "urlopen", fixture_open)
        result = fetch_musixmatch(
            "Fixture Song", "Fixture Artist", album="Fixture Album", duration=201, _route="documented"
        )
        assert result and result.get("found") is True
        assert result["provider_track_id"] == "42001"
        assert "additional candidate lookup stopped: RATE_LIMITED" in result["validationWarnings"][0]

    def test_private_lyrics_routes_need_explicit_credentials_and_do_not_request_without_them(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import urllib.request

        import lyrics_utils

        def forbidden(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("disabled private lyrics endpoint must not be requested")

        monkeypatch.setattr(urllib.request, "urlopen", forbidden)
        monkeypatch.setattr(lyrics_utils, "get_spotify_access_token", lambda: None)
        monkeypatch.delenv("DEEZER_ARL", raising=False)
        monkeypatch.setattr(Path, "home", lambda: Path("/tmp/lyrics-no-provider-credentials"))
        spotify = lyrics_utils.fetch_spotify("Song", "Artist")
        deezer = lyrics_utils.fetch_deezer_lyrics("Song", "Artist")
        assert spotify["acquisition_status"] == "NOT_CONFIGURED"
        assert deezer["acquisition_status"] == "NOT_CONFIGURED"

    def test_token_status_does_not_scan_or_copy_browser_sessions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import lyrics_utils

        monkeypatch.setenv("HOME", "/tmp/lyrics-no-browser-token-home")
        monkeypatch.delenv("MUSIXMATCH_API_KEY", raising=False)
        monkeypatch.delenv("MUSIXMATCH_TOKEN", raising=False)
        monkeypatch.delenv("DEEZER_ARL", raising=False)
        monkeypatch.delenv("SPOTIFY_SP_DC", raising=False)
        monkeypatch.delenv("SPOTIFY_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("SPOTIFY_TOKEN", raising=False)
        report = lyrics_utils.sync_all_tokens(force=True)
        assert report["browser_cookie_import"]["status"] == "disabled"
        assert report["musixmatch"]["status"] == "missing"
        assert report["spotify"]["status"] == "missing"

    def test_cache_version_mismatch_is_not_a_legacy_payload(self, tmp_path: Path) -> None:
        cache = JsonCache(tmp_path)
        path = cache.store("recording", {"plainLyrics": "old parsed text"})
        for field in ("cache_schema", "parser_version"):
            cache.store("recording", {"plainLyrics": "old parsed text"})
            envelope = json.loads(path.read_text(encoding="utf-8"))
            envelope[field] = "obsolete"
            path.write_text(json.dumps(envelope), encoding="utf-8")
            assert cache.load("recording") is None

    def test_unversioned_raw_cache_is_not_trusted_after_parser_changes(self, tmp_path: Path) -> None:
        cache = JsonCache(tmp_path)
        cache.path_for("recording").write_text('{"plainLyrics":"old parser result"}', encoding="utf-8")
        assert cache.load("recording") is None

    def test_enhanced_lrc_timestamp_validation(self) -> None:
        from lyrics_quality import validate_enhanced_lrc

        valid, reasons = validate_enhanced_lrc(
            "[00:01.00]<00:01.00>One <00:01.45>two\n[00:02.00]<00:02.00>Three", duration=60
        )
        assert valid, reasons
        valid, reasons = validate_enhanced_lrc("[00:01.00]<00:03.00>Late <00:02.00>Regression", duration=60)
        assert not valid and any("regress" in reason for reason in reasons)
        valid, reasons = validate_enhanced_lrc("[00:01.00]<02:00.00>Past duration", duration=60)
        assert not valid and any("duration" in reason for reason in reasons)
        valid, reasons = validate_enhanced_lrc("[00:01.00]<00:61.00>Malformed", duration=60)
        assert not valid and any("malformed" in reason for reason in reasons)

    def test_valid_enhanced_only_candidate_derives_line_lrc_losslessly(self) -> None:
        enhanced = "[00:01.00]<00:01.00>Hello <00:01.50>world\n[00:03.00]<00:03.00>Second lyric line"
        result = {
            "source": "Word sync source",
            "trackName": "Song",
            "artistName": "Artist",
            "albumName": "Album",
            "duration": 60,
            "enhancedLyrics": enhanced,
        }
        candidate = lyrics_pipeline.provider_candidate(result, fallback_provider="fixture", duration=60)
        assert candidate is not None and candidate.synced
        assert candidate.synced_lyrics == "[00:01.00]Hello world\n[00:03.00]Second lyric line"
        assert "line LRC derived from validated word timestamps" in candidate.transformations
        assessment = select_best([candidate], title="Song", artist="Artist", album="Album", duration=60)
        assert assessment is not None
        paths = lyrics_pipeline.materialize_selected(Path("song.flac"), assessment).paths
        assert paths["synced"].read_text(encoding="utf-8") == candidate.synced_lyrics
        assert paths["enhanced"].read_text(encoding="utf-8") == enhanced

    def test_malformed_enhanced_sync_is_archived_with_raw_suffix(self, tmp_path: Path) -> None:
        invalid = "[00:01.00]<00:03.00>Late <00:02.00>Regression"
        plain = "A sufficiently complete plain transcript line one.\nA second lyric line provides context."
        synced = "[00:01.00]A sufficiently complete plain transcript line one.\n[00:04.00]A second lyric line provides context."
        result = {
            "plainLyrics": plain,
            "syncedLyrics": synced,
            "enhancedLyrics": invalid,
            "source": "Fixture",
            "trackName": "Song",
            "artistName": "Artist",
            "albumName": "Album",
            "duration": 120,
        }
        candidate = lyrics_pipeline.provider_candidate(result, fallback_provider="fixture", duration=120)
        assert candidate is not None
        assessment = select_best([candidate], title="Song", artist="Artist", album="Album", duration=120)
        assert assessment is not None
        paths = lyrics_pipeline.materialize_selected(tmp_path / "song.flac", assessment).paths
        assert paths["enhanced_raw"].read_bytes() == invalid.encode("utf-8")
        assert "enhanced" not in paths

    def test_versioned_cache_and_short_negative_ttl(self, tmp_path: Path) -> None:
        cache = JsonCache(tmp_path, ttl_seconds=100, negative_ttl_seconds=0.01)
        path = cache.store("https://example.test/missing", None, status=404)
        envelope = json.loads(path.read_text(encoding="utf-8"))
        assert envelope["cache_schema"] >= 2
        assert envelope["key_sha256"] == hashlib.sha256(b"https://example.test/missing").hexdigest()
        assert cache.load("https://example.test/missing") == (404, None)
        old = time.time() - 1
        os.utime(path, (old, old))
        assert cache.load("https://example.test/missing") is None

    def test_cache_rejects_invalid_status_without_throwing(self, tmp_path: Path) -> None:
        cache = JsonCache(tmp_path)
        path = cache.store("invalid-status", {"lyrics": "data"})
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["status"] = "not-an-http-status"
        path.write_text(json.dumps(envelope), encoding="utf-8")
        assert cache.load("invalid-status") is None

    def test_retry_after_is_honored(self) -> None:
        response = type("Response", (), {"headers": {"Retry-After": "7"}})()
        assert retry_delay(response, 1) == 7
        for invalid in ("nan", "inf", "-inf"):
            malformed = type("Response", (), {"headers": {"Retry-After": invalid}})()
            assert retry_delay(malformed, 2) == 2

    def test_shared_provider_adapter_retries_and_returns_typed_result(self, tmp_path: Path) -> None:
        class Response:
            def __init__(self, status: int, payload: object, retry_after: str = "") -> None:
                self.status_code = status
                self._payload = payload
                self.headers = {"Retry-After": retry_after} if retry_after else {}

            def json(self) -> object:
                return self._payload

        class Session:
            def __init__(self) -> None:
                self.responses = [Response(429, None, "1"), Response(200, {"id": "ok"})]

            def get(self, *_args: object, **_kwargs: object) -> Response:
                return self.responses.pop(0)

        delays: list[float] = []
        result = fetch_json(
            Session(),
            "https://example.test/release",
            headers={},
            cache=JsonCache(tmp_path),
            attempts=3,
            sleeper=delays.append,
        )
        assert result.ok and result.payload == {"id": "ok"}
        assert result.attempts == 2 and result.cached is False
        assert delays == [1]

    def test_fetch_json_does_not_retry_before_long_retry_after(self, tmp_path: Path) -> None:
        class Response:
            status_code = 429
            headers: ClassVar[dict[str, str]] = {"Retry-After": "30"}

            def json(self) -> object:
                raise AssertionError("429 responses must not be parsed as JSON")

        class Session:
            calls = 0

            def get(self, *_args: object, **_kwargs: object) -> Response:
                self.calls += 1
                return Response()

        session = Session()
        delays: list[float] = []
        result = fetch_json(
            session,
            "https://example.test/rate-limited",
            headers={},
            cache=JsonCache(tmp_path),
            attempts=3,
            sleeper=delays.append,
        )
        assert result.classification == "RATE_LIMITED"
        assert result.attempts == 1 and session.calls == 1
        assert delays == []

    def test_transient_http_failure_is_not_cached_and_parser_errors_are_not_retried(self, tmp_path: Path) -> None:
        class Response:
            def __init__(self, status: int, payload: object = None) -> None:
                self.status_code = status
                self._payload = payload
                self.headers: dict[str, str] = {}

            def json(self) -> object:
                if isinstance(self._payload, Exception):
                    raise self._payload
                return self._payload

        class Session:
            def __init__(self, response: Response) -> None:
                self.response = response
                self.calls = 0

            def get(self, *_args: object, **_kwargs: object) -> Response:
                self.calls += 1
                return self.response

        cache = JsonCache(tmp_path)
        transient = Session(Response(503))
        result = fetch_json(
            transient, "https://example.test/outage", headers={}, cache=cache, attempts=2, sleeper=lambda _: None
        )
        assert result.classification == "TRANSIENT_ERROR"
        assert transient.calls == 2
        assert cache.load("https://example.test/outage") is None

        parser_failure = Session(Response(200, json.JSONDecodeError("broken", "{", 1)))
        parsed = fetch_json(
            parser_failure,
            "https://example.test/changed-schema",
            headers={},
            cache=cache,
            attempts=3,
            sleeper=lambda _: None,
        )
        assert parsed.classification == "PARSER_ERROR"
        assert parser_failure.calls == 1
        assert cache.load("https://example.test/changed-schema") is None

    def test_nested_disc_layout_and_per_disc_counts(self, tmp_path: Path) -> None:
        cd1, cd2 = tmp_path / "CD1", tmp_path / "Disc 2"
        cd1.mkdir()
        cd2.mkdir()
        files = [cd1 / "01 - One.flac", cd1 / "02 - Two.flac", cd2 / "01 - Three.flac"]
        assert library_layout.infer_position(files[2], tmp_path) == (2, 1, "Three")
        assert library_layout.disc_counts(files, tmp_path) == {1: 2, 2: 1}


class TestQQMusicQrcProvider:
    def test_qrc_fixture_preserves_timing_identity_translation_and_alternatives(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import copy
        import json
        import urllib.parse

        import lyrics_utils

        root = Path(__file__).parent / "tests" / "fixtures" / "lyrics" / "qqmusic"
        search_payload = json.loads((root / "search.json").read_text(encoding="utf-8"))
        alternate = copy.deepcopy(search_payload["data"]["song"]["list"][0])
        alternate.update(songmid="qq-track-2", albumname="Alternate Album", albummid="qq-album-2", interval=247)
        search_payload["data"]["song"]["list"].append(alternate)
        lyrics_body = (root / "lyrics.json").read_bytes()
        monkeypatch.setattr(lyrics_utils, "_PROVIDER_RESULT_CACHE", JsonCache(tmp_path / "qq-cache"))

        class FixtureResponse:
            status = 200

            def __init__(self, body: bytes) -> None:
                self.body = body

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int = -1) -> bytes:
                return self.body

        requests: list[str] = []

        def open_fixture(request: Any, timeout: float = 6) -> FixtureResponse:
            del timeout
            requests.append(request.full_url)
            if request.full_url.startswith("https://c.y.qq.com/soso/"):
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
                assert query["w"] == ["Fixture Artist Fixture Song"]
                return FixtureResponse(json.dumps(search_payload, ensure_ascii=False).encode("utf-8"))
            return FixtureResponse(lyrics_body)

        monkeypatch.setattr(lyrics_utils, "_provider_urlopen", open_fixture)
        result = lyrics_utils.invoke_lyrics_provider(
            "QQ Music", lyrics_utils.fetch_qqmusic, "Fixture Song", "Fixture Artist", duration=246, timeout=1
        )
        assert result["found"] is True
        assert result["trackName"] == "Fixture Song"
        assert result["artistName"] == "Fixture Artist"
        assert result["albumName"] == "Fixture Album"
        assert result["duration"] == 246
        assert result["has_word_sync"] is True
        assert "[00:01.00]" in result["syncedLyrics"]
        assert "夜の街を歩く" in result["plainLyrics"]
        assert "translation remains separate" in result["translationLyrics"]
        assert len(result["alternatives"]) == 1
        raw_response = json.loads(result["providerRaw"])
        assert "LyricContent" in raw_response["qrc"]
        selected = lyrics_pipeline.choose_provider_result(
            [result], title="Fixture Song", artist="Fixture Artist", album="Fixture Album", duration=246
        )
        assert selected is not None and selected.candidate.provider_track_id in {"qq-track-1", "qq-track-2"}
        selected_again = lyrics_pipeline.choose_provider_result(
            [result], title="Fixture Song", artist="Fixture Artist", album="Fixture Album", duration=246
        )
        assert selected_again is not None
        assert selected_again.candidate.provider_track_id == selected.candidate.provider_track_id
        resolution = lyrics_pipeline.materialize_selected(tmp_path / "fixture.flac", selected)
        assert "夜の街を歩く" in resolution.paths["plain"].read_text(encoding="utf-8")
        assert "translation remains separate" in resolution.paths["translation"].read_text(encoding="utf-8")
        assert len(requests) == 3

    @pytest.mark.parametrize(
        ("fixture_name", "expected_status"),
        [("empty.json", "NOT_FOUND"), ("changed.json", "PARSER_ERROR")],
    )
    def test_qq_search_empty_and_changed_schemas_are_distinct(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fixture_name: str, expected_status: str
    ) -> None:
        import lyrics_utils

        root = Path(__file__).parent / "tests" / "fixtures" / "lyrics" / "qqmusic"
        body = (root / fixture_name).read_bytes()
        monkeypatch.setattr(lyrics_utils, "_PROVIDER_RESULT_CACHE", JsonCache(tmp_path / fixture_name))

        class FixtureResponse:
            status = 200

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int = -1) -> bytes:
                return body

        monkeypatch.setattr(lyrics_utils, "_provider_urlopen", lambda *_args, **_kwargs: FixtureResponse())
        result = lyrics_utils.invoke_lyrics_provider(
            "QQ Music", lyrics_utils.fetch_qqmusic, "Absent Song", "Absent Artist", timeout=1
        )
        assert result["acquisition_status"] == expected_status


class TestTransactionalWriter:
    def test_conflicting_incoming_scalar_values_are_rejected_without_write(self, tmp_path: Path) -> None:
        flac = make_flac(tmp_path / "track.flac")
        original = flac.read_bytes()
        with pytest.raises(ValueError, match="conflicting incoming single-valued"):
            metadata_writer.write_tags(flac, core_tags() + [("TITLE", "Other Title")])
        assert flac.read_bytes() == original

    def test_symlinked_flac_is_not_replaced(self, tmp_path: Path) -> None:
        target = make_flac(tmp_path / "source.flac")
        linked = tmp_path / "linked.flac"
        linked.symlink_to(target)
        original = target.read_bytes()
        with pytest.raises(ValueError, match="symlinked FLAC"):
            metadata_writer.write_tags(linked, core_tags())
        assert linked.is_symlink()
        assert target.read_bytes() == original

    def test_verified_musicbrainz_mapping_migrates_legacy_fields(self, tmp_path: Path) -> None:
        flac = make_flac(tmp_path / "track.flac")
        recording = "b1a9c0e9-d987-4042-ae91-78d6a3267d69"
        release_track = "b343ca82-baf2-356d-974e-94836915eec0"
        audio = FLAC(flac)
        audio["MUSICBRAINZ_RECORDINGID"] = [recording]
        audio["MUSICBRAINZ_TRACKID"] = [release_track]
        audio.save()
        metadata_writer.write_tags(
            flac,
            core_tags()
            + [
                ("MUSICBRAINZ_TRACKID", recording),
                ("MUSICBRAINZ_RELEASETRACKID", release_track),
            ],
            incoming_confidence=Confidence.VERIFIED,
            release_confidence=Confidence.VERIFIED,
        )
        migrated = FLAC(flac)
        assert "MUSICBRAINZ_RECORDINGID" not in migrated
        assert migrated["MUSICBRAINZ_TRACKID"] == [recording]
        assert migrated["MUSICBRAINZ_RELEASETRACKID"] == [release_track]

    def test_dry_run_returns_decisions_without_mutation_or_ledger(self, tmp_path: Path) -> None:
        flac = make_flac(tmp_path / "track.flac")
        original = flac.read_bytes()
        report = metadata_writer.write_tags(flac, core_tags(), dry_run=True)
        assert any(decision.action is DecisionAction.ADD for decision in report.decisions)
        assert report.provenance_path == ""
        assert flac.read_bytes() == original
        assert not (tmp_path / ".metadata-provenance.jsonl").exists()

    def test_second_identical_write_is_idempotent_except_provenance_link(self, tmp_path: Path) -> None:
        flac = make_flac(tmp_path / "track.flac")
        metadata_writer.write_tags(flac, core_tags())
        report = metadata_writer.write_tags(flac, core_tags(), dry_run=True)
        substantive = [decision for decision in report.decisions if decision.field != "METADATA_PROVENANCE_ID"]
        assert substantive
        assert all(decision.action is DecisionAction.KEEP for decision in substantive)

    def test_unknown_tags_and_nonfront_pictures_survive(self, tmp_path: Path) -> None:
        flac = make_flac(tmp_path / "track.flac")
        audio = FLAC(flac)
        audio["X-ARCHIVE-NOTE"] = ["hand corrected"]
        front = Picture()
        front.type = 3
        front.mime = "image/jpeg"
        front.width = 10
        front.height = 10
        front.data = b"front"
        back = Picture()
        back.type = 4
        back.mime = "image/jpeg"
        back.width = 10
        back.height = 10
        back.data = b"back"
        audio.add_picture(front)
        audio.add_picture(back)
        audio.save()
        cover = tmp_path / "new.jpg"
        Image.new("RGB", (32, 32), "red").save(cover)
        metadata_writer.write_tags(flac, core_tags(), picture_file=str(cover))
        after = FLAC(flac)
        assert after["X-ARCHIVE-NOTE"] == ["hand corrected"]
        assert [picture.data for picture in after.pictures] == [b"front", b"back"]

    def test_explicit_front_replacement_preserves_back(self, tmp_path: Path) -> None:
        flac = make_flac(tmp_path / "track.flac")
        audio = FLAC(flac)
        back = Picture()
        back.type = 4
        back.mime = "image/jpeg"
        back.width = 10
        back.height = 10
        back.data = b"back"
        audio.add_picture(back)
        audio.save()
        cover = tmp_path / "new.jpg"
        Image.new("RGB", (32, 32), "red").save(cover)
        metadata_writer.write_tags(flac, core_tags(), picture_file=str(cover), replace_front_picture=True)
        after = FLAC(flac)
        assert any(p.type == 3 for p in after.pictures)
        assert any(p.type == 4 and p.data == b"back" for p in after.pictures)

    def test_embedded_picture_uses_oriented_dimensions_and_actual_mime(self, tmp_path: Path) -> None:
        flac = make_flac(tmp_path / "track.flac")
        image = Image.new("RGB", (400, 800), "navy")
        exif = image.getexif()
        exif[274] = 6
        cover = tmp_path / "rotated.jpeg"
        image.save(cover, format="JPEG", quality=95, exif=exif)
        metadata_writer.write_tags(flac, core_tags(), picture_file=cover, replace_front_picture=True)
        front = next(picture for picture in FLAC(flac).pictures if picture.type == 3)
        assert front.mime == "image/jpeg"
        assert (front.width, front.height) == (800, 400)
        with Image.open(cover) as original:
            assert original.getexif().get(274) == 6

    def test_untransformed_png_is_embedded_byte_exact(self, tmp_path: Path) -> None:
        flac = make_flac(tmp_path / "track.flac")
        cover = tmp_path / "front.bin"
        Image.new("RGBA", (640, 640), (20, 40, 60, 180)).save(cover, format="PNG")
        original = cover.read_bytes()
        metadata_writer.write_tags(flac, core_tags(), picture_file=cover, replace_front_picture=True)
        front = next(picture for picture in FLAC(flac).pictures if picture.type == 3)
        assert front.mime == "image/png"
        assert front.depth == 32
        assert front.data == original

    def test_validator_failure_leaves_original_byte_exact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        flac = make_flac(tmp_path / "track.flac")
        original = flac.read_bytes()
        monkeypatch.setattr(
            metadata_writer, "canonical_verify", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("injected"))
        )
        with pytest.raises(RuntimeError, match="injected"):
            metadata_writer.write_tags(flac, core_tags())
        assert flac.read_bytes() == original

    def test_provenance_failure_rolls_back_original_byte_exact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        flac = make_flac(tmp_path / "track.flac")
        original = flac.read_bytes()
        monkeypatch.setattr(
            metadata_writer,
            "_append_provenance_record",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("ledger unavailable")),
        )
        with pytest.raises(OSError, match="ledger unavailable"):
            metadata_writer.write_tags(flac, core_tags())
        assert flac.read_bytes() == original

    def test_provenance_records_snapshots_and_rotates_link(self, tmp_path: Path) -> None:
        flac = make_flac(tmp_path / "track.flac")
        first = metadata_writer.write_tags(flac, core_tags("One"))
        second = metadata_writer.write_tags(flac, core_tags("Two"), force=True)
        audio = FLAC(flac)
        assert audio["METADATA_PROVENANCE_ID"] == [second.transaction_id]
        rows = [json.loads(line) for line in Path(second.provenance_path).read_text(encoding="utf-8").splitlines()]
        assert [row["transaction_id"] for row in rows] == [first.transaction_id, second.transaction_id]
        assert rows[-1]["before_tags"] and rows[-1]["after_tags"]
        assert len(rows[-1]["audio_streaminfo"]["md5"]) == 32


class TestQualityControl:
    def test_library_detects_duplicate_release_track_and_mixed_edition(self, tmp_path: Path) -> None:
        files: list[Path] = []
        release_track_id = "b343ca82-baf2-356d-974e-94836915eec0"
        for index in (1, 2):
            flac = make_flac(tmp_path / f"0{index}.flac")
            tags = core_tags(f"Track {index}")
            tags = [(field, str(index) if field == "TRACKNUMBER" else value) for field, value in tags]
            tags = [(field, "2" if field == "TRACKTOTAL" else value) for field, value in tags]
            tags += [
                ("MUSICBRAINZ_RELEASETRACKID", release_track_id),
                ("DISCOGS_RELEASE_ID", str(100 + index)),
            ]
            metadata_writer.write_tags(flac, tags)
            files.append(flac)
        findings = audit_library([audit_flac(path, check_stream=False) for path in files])
        codes = {finding.code for finding in findings}
        assert "library.release_track_duplicate" in codes
        assert "album.inconsistent" in codes


class TestAuditRegressions:
    def test_private_transaction_copies_are_never_discovered(self, tmp_path: Path) -> None:
        real = make_flac(tmp_path / "01 - Real.flac")
        staged_parent = tmp_path / ".metadata-transaction-x" / "pending"
        staged_parent.mkdir(parents=True)
        staged = make_flac(staged_parent / "01 - Real.flac")
        assert iter_library_flacs(tmp_path) == [real]
        assert staged not in iter_library_flacs(tmp_path)

    def test_multivalue_replace_keeps_every_value(self) -> None:
        decision = decide_field(
            "ISRC",
            ["USAAA0000001"],
            ["USAAA0000001", "USAAA0000002"],
            incoming_confidence=Confidence.VERIFIED,
            existing_confidence=Confidence.LOW,
            release_confidence=Confidence.HIGH,
            stable_ids_agree=True,
        )
        assert decision.action is DecisionAction.REPLACE
        assert decision.values == ("USAAA0000001", "USAAA0000002")

    def test_position_evidence_does_not_invent_track_one(self, tmp_path: Path) -> None:
        unknown = tmp_path / "Intro.flac"
        disc, track, _title, conflicts = library_layout.position_evidence(unknown, tmp_path, {})
        assert (disc, track, conflicts) == (None, None, ())

    def test_position_evidence_reports_directory_and_tag_disc_conflict(self, tmp_path: Path) -> None:
        path = tmp_path / "CD2" / "01 - Song.flac"
        disc, track, _title, conflicts = library_layout.position_evidence(
            path, tmp_path, {"DISCNUMBER": ["1"], "TRACKNUMBER": ["1"]}
        )
        assert (disc, track) == (2, 1)
        assert "disc directory conflicts with embedded tag" in conflicts

    def test_repeated_timestamp_lrc_is_valid(self) -> None:
        valid, reasons = validate_lrc("[00:10][00:20]Chorus\n[00:30]Verse\n")
        assert valid, reasons

    def test_all_present_media_must_match_layout(self) -> None:
        result = assess_medium_layouts(
            {1: 10, 2: 8}, [{"position": 1, "track-count": 10}, {"position": 2, "track-count": 7}]
        )
        assert not result["compatible"]
        assert "disc 2 track count 8!=7" in result["conflicts"]

    def test_query_echo_is_not_lyrics_identity_evidence(self) -> None:
        assessment = lyrics_pipeline.choose_provider_result(
            [
                {
                    "source": "opaque provider",
                    "plainLyrics": "actual words\nsecond line",
                    "requested_title": "Song",
                    "requested_artist": "Artist",
                }
            ],
            title="Song",
            artist="Artist",
            album="Album",
            duration=180,
        )
        assert assessment is None

    def test_display_lyrics_hides_section_labels_but_keeps_timed_words(self) -> None:
        source = (
            "[00:01.00][Chorus]\n"
            "[00:02.00][Verse 1]: Actual line\n"
            "[00:03.00][Куплет 2]Следующая строка\n"
            "[00:04.00]Keep [Chorus] as sung text"
        )
        rendered = lyrics_pipeline.display_lyrics(source, synced=True)
        assert rendered == ("[00:02.00]Actual line\n[00:03.00]Следующая строка\n[00:04.00]Keep [Chorus] as sung text")
