"""Sanitized integration regressions discovered using isolated real album copies."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import requests
from mutagen.flac import FLAC

import audiophile_tagger as tagger
from test_entire_audit import album_fixture


def partial_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[Path], dict[str, Any]]:
    folder, tracks = album_fixture(tmp_path, monkeypatch)
    renamed = []
    for path, number in zip(tracks, (2, 5), strict=True):
        target = folder / f"01-{number:02} - Song {number}.flac"
        path.rename(target)
        audio = FLAC(target)
        audio["TITLE"] = [f"Song {number}"]
        audio["TRACKNUMBER"] = [str(number)]
        audio["TRACKTOTAL"] = ["5"]
        audio["TOTALTRACKS"] = ["5"]
        audio.save()
        renamed.append(target)
    discogs = {
        "id": 42,
        "title": "Album",
        "artists": [{"name": "Artist"}],
        "released": "2000",
        "country": "UK",
        "formats": [{"name": "CD", "qty": "1"}],
        "labels": [{"name": "Label", "catno": "CAT-42"}],
        "identifiers": [{"type": "Barcode", "value": "1234567890123"}],
        "tracklist": [
            {"position": str(n), "title": f"Song {n}", "type_": "track", "duration": "0:01"} for n in range(1, 6)
        ],
    }
    monkeypatch.setattr(tagger, "discogs_get", lambda *_a, **_k: discogs)
    return folder, renamed, discogs


def test_partial_release_acoustid_uses_disc_track_positions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder, tracks, _discogs = partial_fixture(tmp_path, monkeypatch)
    release = {
        "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "title": "Album",
        "country": "GB",
        "date": "2000",
        "barcode": "1234567890123",
        "status": "Official",
        "artist-credit": [{"name": "Artist", "artist": {"name": "Artist"}}],
        "label-info": [{"catalog-number": "CAT-42", "label": {"name": "Label"}}],
        "release-group": {"id": "group", "primary-type": "Album", "first-release-date": "2000"},
        "media": [
            {
                "position": 1,
                "format": "CD",
                "track-count": 5,
                "tracks": [
                    {
                        "position": n,
                        "number": str(n),
                        "title": f"Song {n}",
                        "length": 1000,
                        "id": f"release-track-{n}",
                        "recording": {"id": f"recording-{n}", "title": f"Song {n}", "length": 1000},
                    }
                    for n in range(1, 6)
                ],
            }
        ],
    }

    def mb(url: str, *_args: Any, **_kw: Any) -> tuple[int, dict[str, Any]]:
        if "/ws/2/url?" in url:
            return 200, {"relations": [{"release": {"id": release["id"]}}]}
        if f"/release/{release['id']}?" in url:
            return 200, release
        return 404, {}

    monkeypatch.setattr(tagger, "mb_get", mb)
    monkeypatch.setattr(
        tagger, "query_acoustid_candidates", lambda path, *_a: ([], {f"recording-{FLAC(path)['TRACKNUMBER'][0]}"})
    )
    observed = []
    evaluate = tagger.mb_identity_score.evaluate_identity

    def inspect(**kw: Any) -> dict[str, Any]:
        result = evaluate(**kw)
        observed.append(result)
        return result

    monkeypatch.setattr(tagger.mb_identity_score, "evaluate_identity", inspect)
    with requests.Session() as session:
        assert tagger.process_album(f"42:::{folder}", "fixture", "fixture@example.org", True, True, True, 1, session)
    assert observed[0]["flags"]["acoustid_match_count"] == 2
    assert not observed[0]["flags"]["acoustid_audio_mismatch"]
    for path, number in zip(tracks, (2, 5), strict=True):
        audio = FLAC(path)
        assert audio["MUSICBRAINZ_ALBUMID"] == [release["id"]]
        assert audio["MUSICBRAINZ_TRACKID"] == [f"recording-{number}"]
        assert audio["MUSICBRAINZ_RELEASETRACKID"] == [f"release-track-{number}"]
        assert audio["TRACKTOTAL"] == ["5"]
        assert "REPLAYGAIN_ALBUM_GAIN" not in audio
    assert tagger.assess_album_completeness(tracks, folder, external_disc_counts={1: 5}).state == "INCOMPLETE"


def test_partial_discogs_total_is_release_total_not_files_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    folder, tracks, _discogs = partial_fixture(tmp_path, monkeypatch)
    with requests.Session() as session:
        assert tagger.process_album(
            f"42:::{folder}", "fixture", "fixture@example.org", True, True, True, 1, session, dry_run=True
        )
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert len(reports) == len(tracks)
    for report in reports:
        # Equal values may be a KEEP absent from changes; existing totals must
        # never be proposed for replacement with the count of present files.
        assert "TRACKTOTAL" not in report or set(report["TRACKTOTAL"]["proposed"]) == {"5"}


@pytest.mark.parametrize("disc", [1, 2])
def test_partial_position_checks_keep_duration_and_range_gates(disc: int) -> None:
    import release_match

    media = [{"position": disc, "track-count": 5, "tracks": [{"position": n, "length": 100000} for n in range(1, 6)]}]
    valid = release_match.assess_medium_layouts(
        {disc: 2}, media, local_track_durations={(disc, 2): 100.0, (disc, 5): 100.0}
    )
    assert valid["compatible"]
    assert not valid["complete"]
    for evidence in ({(disc, 2): 100.0, (disc, 5): 200.0}, {(disc, 2): 100.0, (disc, 6): 100.0}):
        invalid = release_match.assess_medium_layouts({disc: 2}, media, local_track_durations=evidence)
        assert not invalid["compatible"]
    assert not release_match.assess_medium_layouts({disc: 2}, media)["compatible"]


def test_positioned_acoustid_checks_every_disc_and_rejects_wrong_recordings() -> None:
    from mb_identity_score import evaluate_identity

    discogs = {"title": "Album", "artists": [{"name": "Artist"}], "formats": [{"name": "CD"}]}
    release = {
        "title": "Album",
        "artist-credit": [{"artist": {"name": "Artist"}}],
        "media": [
            {
                "position": disc,
                "format": "CD",
                "track-count": 2,
                "tracks": [
                    {"position": n, "title": f"Song {n}", "recording": {"id": f"recording-{disc}-{n}"}} for n in (1, 2)
                ],
            }
            for disc in (1, 2)
        ],
    }
    evidence = {(1, 2): {"recording-1-2"}, (2, 2): {"recording-2-2"}}
    valid = evaluate_identity(D=discogs, M=release, acoustid_position_recordings=evidence)
    assert valid["flags"]["acoustid_match_count"] == 2
    assert not valid["flags"]["acoustid_audio_mismatch"]
    evidence[(2, 2)] = {"recording-1-2"}
    invalid = evaluate_identity(D=discogs, M=release, acoustid_position_recordings=evidence)
    assert invalid["rejected"]
    assert "acoustid-recording-conflict" in invalid["hard_conflicts"]


def test_dry_run_diagnostics_describe_planning_not_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    folder, _tracks = album_fixture(tmp_path, monkeypatch)
    with requests.Session() as session:
        assert tagger.process_album(
            f"42:::{folder}",
            "fixture",
            "fixture@example.org",
            False,
            False,
            False,
            1,
            session,
            dry_run=True,
            verbose=True,
        )
    output = capsys.readouterr().out
    assert "[VERBOSE][METADATA] Writing" not in output
    assert "DR14: skipped (--dry-run)" in output
    assert not (folder / ".metadata-provenance.jsonl").exists()
