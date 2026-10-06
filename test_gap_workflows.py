"""Generated audio exercises production album wiring; providers are offline fixtures."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import requests
from mutagen.flac import FLAC

import audiophile_tagger as tagger
import metadata_writer
from test_entire_audit import album_fixture


def generated_album(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    counts: tuple[int, ...],
    available: list[tuple[int, int]],
    *,
    conflict: bool = False,
) -> tuple[Path, dict[tuple[int, int], Path], dict[str, Any]]:
    folder, old_tracks = album_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("SCRIPT_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    for path in old_tracks:
        path.unlink()
    paths = {}
    for index, (disc, track) in enumerate(reversed(available)):
        parent = folder / f"CD{disc}" if len(counts) > 1 else folder
        parent.mkdir(exist_ok=True)
        path = parent / f"{chr(122 - index)} shuffled.flac"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={440 + track * 100}:sample_rate=44100:duration=6",
                "-af",
                f"volume={0.2 if track == 1 else 0.8}",
                "-ac",
                "2",
                str(path),
            ],
            check=True,
        )
        audio = FLAC(path)
        for key, value in {
            "TITLE": f"Song {disc}-{track}",
            "ARTIST": "Artist",
            "ALBUMARTIST": "Artist",
            "ALBUM": "Album",
            "TRACKNUMBER": str(track),
            "DISCNUMBER": str(disc),
            "DISCTOTAL": str(len(counts)),
            "CURATED": "keep me",
            "CUSTOM_MULTI": ["one", "two"],
        }.items():
            audio[key] = value if isinstance(value, list) else [value]
        if conflict:
            audio["TRACKTOTAL"] = [str(counts[disc - 1] + 1)]
        audio.save()
        paths[disc, track] = path
    discogs = {
        "id": 42,
        "title": "Album",
        "artists": [{"name": "Artist"}],
        "released": "2000",
        "country": "UK",
        "formats": [{"name": "CD", "qty": str(len(counts))}],
        "labels": [{"name": "Label", "catno": "CAT-42"}],
        "identifiers": [{"type": "Barcode", "value": "1234567890123"}],
        "tracklist": [
            {
                "position": f"{d}-{n}" if len(counts) > 1 else str(n),
                "title": f"Song {d}-{n}",
                "type_": "track",
                "duration": "0:06",
            }
            for d, count in enumerate(counts, 1)
            for n in range(1, count + 1)
        ],
    }
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
                "position": d,
                "format": "CD",
                "track-count": count,
                "tracks": [
                    {
                        "position": n,
                        "number": str(n),
                        "title": f"Song {d}-{n}",
                        "length": 6000,
                        "id": f"release-track-{d}-{n}",
                        "recording": {"id": f"recording-{d}-{n}", "title": f"Song {d}-{n}", "length": 6000},
                    }
                    for n in range(1, count + 1)
                ],
            }
            for d, count in enumerate(counts, 1)
        ],
    }
    monkeypatch.setattr(tagger, "discogs_get", lambda *_a, **_k: discogs)

    def mb(url: str, *_a: Any, **_k: Any) -> tuple[int, dict[str, Any]]:
        if "/ws/2/url?" in url:
            return 200, {"relations": [{"release": {"id": release["id"]}}]}
        if f"/release/{release['id']}?" in url:
            return 200, release
        return 404, {}

    monkeypatch.setattr(tagger, "mb_get", mb)
    monkeypatch.setattr(
        tagger,
        "query_acoustid_candidates",
        lambda path, *_a: ([], {f"recording-{FLAC(path)['DISCNUMBER'][0]}-{FLAC(path)['TRACKNUMBER'][0]}"}),
    )
    return folder, paths, release


@pytest.mark.parametrize(
    "counts,available,state,conflict",
    [
        ((2,), [(1, 1), (1, 2)], "COMPLETE", False),
        ((2, 2), [(1, 1), (1, 2), (2, 1), (2, 2)], "COMPLETE", False),
        ((2, 2), [(1, 1), (1, 2), (2, 2)], "INCOMPLETE", False),
        ((3,), [(1, 3), (1, 1)], "INCOMPLETE", False),
        ((2,), [(1, 1), (1, 2)], "INCOMPLETE", True),
    ],
)
def test_generated_production_album(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    counts: tuple[int, ...],
    available: list[tuple[int, int]],
    state: str,
    conflict: bool,
) -> None:
    folder, paths, release = generated_album(tmp_path, monkeypatch, counts, available, conflict=conflict)
    if not shutil.which("loudgain"):
        pytest.skip("real loudgain unavailable; subprocess contracts covered separately")
    before = {path: metadata_writer.pcm_digest(path) for path in paths.values()}
    original = {path: path.read_bytes() for path in folder.rglob("*") if path.is_file()}
    with requests.Session() as session:
        assert tagger.process_album(
            f"42:::{folder}", "fixture", "fixture@example.org", True, False, True, 2, session, dry_run=True
        )
    assert {path: path.read_bytes() for path in folder.rglob("*") if path.is_file()} == original
    with requests.Session() as session:
        assert tagger.process_album(f"42:::{folder}", "fixture", "fixture@example.org", True, False, True, 2, session)
    rows = [json.loads(line) for line in (folder / ".metadata-provenance.jsonl").read_text().splitlines()]
    track_rows = [row for row in rows if "context" in row]
    assert track_rows
    assert {row["context"]["album_completeness"]["state"] for row in track_rows} == {state}
    assert (
        tagger.assess_album_completeness(
            list(paths.values()), folder, external_disc_counts={d: n for d, n in enumerate(counts, 1)}
        ).state
        == state
    )
    album_pairs = set()
    gains = set()
    for (disc, track), path in paths.items():
        audio = FLAC(path)
        assert audio["MUSICBRAINZ_ALBUMID"] == [release["id"]]
        assert audio["MUSICBRAINZ_TRACKID"] == [f"recording-{disc}-{track}"]
        assert audio["MUSICBRAINZ_RELEASETRACKID"] == [f"release-track-{disc}-{track}"]
        assert audio["TRACKTOTAL"] == [str(counts[disc - 1] + int(conflict))]
        assert audio["DISCTOTAL"] == [str(len(counts))]
        assert audio["CUSTOM_MULTI"] == ["one", "two"] and audio["CURATED"] == ["keep me"]
        assert metadata_writer.pcm_digest(path) == before[path]
        gains.add(audio["REPLAYGAIN_TRACK_GAIN"][0])
        if state == "COMPLETE":
            album_pairs.add((audio["REPLAYGAIN_ALBUM_GAIN"][0], audio["REPLAYGAIN_ALBUM_PEAK"][0]))
        else:
            assert not any(
                key in audio for key in ("REPLAYGAIN_ALBUM_GAIN", "REPLAYGAIN_ALBUM_PEAK", "REPLAYGAIN_ALBUM_RANGE")
            )
    assert len(gains) > 1
    if state == "COMPLETE":
        assert len(album_pairs) == 1
    snapshot = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in folder.rglob("*") if path.is_file()}
    with requests.Session() as session:
        assert tagger.process_album(f"42:::{folder}", "fixture", "fixture@example.org", True, False, True, 2, session)
    assert {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in folder.rglob("*") if path.is_file()
    } == snapshot


def test_external_ledger_rejected_before_any_enrichment(tmp_path: Path) -> None:
    from test_pipeline_safety import make_flac

    track = make_flac(tmp_path / "track.flac")
    external = tmp_path / "external"
    external.mkdir()
    ledger = external / "ledger.jsonl"
    ledger.write_bytes(b"existing ledger\n")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    for _ in range(2):
        with pytest.raises(ValueError, match="external-ledger recovery is unsupported"):
            metadata_writer.write_tags(track, [("CUSTOM", "changed")], provenance_file=ledger)
        assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    assert not list(tmp_path.glob(".metadata-transaction-*"))


@pytest.mark.parametrize("failure", ["exit", "partial"])
def test_loudgain_failure_rolls_back_production_album(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    folder, paths, _ = generated_album(tmp_path, monkeypatch, (2,), [(1, 1), (1, 2)])
    before = {path: path.read_bytes() for path in paths.values()}
    run = subprocess.run

    def fail(command: list[str], **kw: Any) -> Any:
        result = run(command, **kw)
        if Path(command[0]).name == "loudgain":
            if failure == "exit":
                raise subprocess.CalledProcessError(7, command, stderr="injected scanner failure after writes")
            audio = FLAC(command[-1])
            del audio["REPLAYGAIN_TRACK_GAIN"]
            audio.save()
        return result

    monkeypatch.setattr(subprocess, "run", fail)
    with requests.Session() as session:
        assert not tagger.process_album(
            f"42:::{folder}", "fixture", "fixture@example.org", True, False, True, 2, session
        )
    assert {path: path.read_bytes() for path in paths.values()} == before
    assert not (folder / ".metadata-provenance.jsonl").exists()
    assert not list(folder.glob(".metadata-transaction-*"))


@pytest.mark.parametrize("zero_checksum", [False, True])
def test_existing_gain_from_another_group_is_not_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, zero_checksum: bool
) -> None:
    _folder, paths, _ = generated_album(tmp_path, monkeypatch, (2,), [(1, 1), (1, 2)])
    files = list(paths.values())
    for path in files:
        audio = FLAC(path)
        audio["REPLAYGAIN_TRACK_GAIN"] = ["0.00 dB"]
        audio["REPLAYGAIN_TRACK_PEAK"] = ["0.5"]
        audio["REPLAYGAIN_ALBUM_GAIN"] = ["0.00 dB"]
        audio["REPLAYGAIN_ALBUM_PEAK"] = ["0.5"]
        if zero_checksum:
            audio.info.md5_signature = 0
        audio.save()
    # Equal valid legacy album values do not identify their audio group.
    assert tagger.run_loudgain(files, quiet=True)
    group = FLAC(files[0])["REPLAYGAIN_ALBUM_GROUP_SHA256"]
    outcome: dict[str, str] = {}
    assert not tagger.run_loudgain(list(reversed(files)), quiet=True, outcome=outcome)
    assert outcome["status"] == "preserved"
    # Both old pairs are still equal and syntactically valid on this subset.
    assert tagger.run_loudgain(files[:1], quiet=True, outcome=outcome)
    assert outcome["status"] == "updated"
    assert FLAC(files[0])["REPLAYGAIN_ALBUM_GROUP_SHA256"] != group
    # The other one-track group has identical format/duration, but different
    # samples. Even absent STREAMINFO checksums must not certify those gains.
    first, second = FLAC(files[0]), FLAC(files[1])
    for field in ("REPLAYGAIN_ALBUM_GAIN", "REPLAYGAIN_ALBUM_PEAK", "REPLAYGAIN_ALBUM_GROUP_SHA256"):
        second[field] = first[field]
    second.save()
    assert tagger.run_loudgain(files[1:], quiet=True, outcome=outcome)
    assert FLAC(files[1])["REPLAYGAIN_ALBUM_GROUP_SHA256"] != first["REPLAYGAIN_ALBUM_GROUP_SHA256"]
    monkeypatch.setattr(tagger.shutil, "which", lambda _name: None)
    broken = FLAC(files[0])
    del broken["REPLAYGAIN_TRACK_GAIN"]
    broken.save()
    assert tagger.run_loudgain(files, quiet=True, outcome=outcome)
    for path in files:
        assert not FLAC(path).get("REPLAYGAIN_ALBUM_GAIN")
        assert not FLAC(path).get("REPLAYGAIN_ALBUM_GROUP_SHA256")


def test_conflicting_existing_identity_is_preserved_without_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    folder, paths, release = generated_album(tmp_path, monkeypatch, (2,), [(1, 1), (1, 2)])
    # Sanitized News scenario: stored MB edition disagrees with explicit
    # Discogs/barcode/catalog evidence. No real album is edited.
    stale_id = "0060992e-439c-34b0-9aeb-9df1dcef23f3"
    for path in paths.values():
        audio = FLAC(path)
        audio["MUSICBRAINZ_ALBUMID"] = [stale_id]
        audio["MUSICBRAINZ_TRACKID"] = ["curated-unverified-recording"]
        audio["TRACKTOTAL"] = ["2"]
        audio.save()
    release["barcode"] = "015775158825"
    release["relations"] = [{"url": {"resource": "https://www.discogs.com/release/1336781"}}]
    original_mb = tagger.mb_get

    def mb(url: str, *args: Any, **kw: Any) -> Any:
        if f"/release/{stale_id}?" in url:
            return 200, {**release, "id": stale_id}
        return original_mb(url, *args, **kw)

    monkeypatch.setattr(tagger, "mb_get", mb)
    policy = Path(os.environ["AUDIOPHILE_CONFIG"])
    policy.write_text("[lyrics]\nonline_lookup=true\n")
    observed = []

    def lyrics(**kw: Any) -> dict[str, Any]:
        observed.append(kw)
        return {"acquisition_status": "NOT_FOUND"}

    monkeypatch.setattr(tagger.lyrics_utils, "fetch_betterlyrics", lyrics)
    covers = []

    def cover_query(fetcher: Any) -> list[Any]:
        covers.append(fetcher.mb_rel_id)
        return []

    monkeypatch.setattr(tagger.audiophile_cover_fetcher.CoverFetcher, "query_discogs", cover_query)
    with requests.Session() as session:
        assert tagger.process_album(f"42:::{folder}", "fixture", "fixture@example.org", True, True, True, 2, session)
    assert observed and all(kw["recording_id"] == "" for kw in observed)
    assert covers and not any(covers)
    rows = [json.loads(line) for line in (folder / ".metadata-provenance.jsonl").read_text().splitlines()]
    for row in rows:
        if "context" in row:
            assert row["context"]["release_match"]["musicbrainz_release_id"] == ""
            assert row["context"]["track_match"]["recording_id"] == ""
    for path in paths.values():
        audio = FLAC(path)
        assert audio["MUSICBRAINZ_ALBUMID"] == [stale_id]
        assert audio["MUSICBRAINZ_TRACKID"] == ["curated-unverified-recording"]
        assert not audio.get("MUSICBRAINZ_RELEASETRACKID")
        assert audio["TRACKTOTAL"] == ["2"]  # Curated local total is preserved.
    output = capsys.readouterr().out
    assert "IDENTITY REVIEW" in output and stale_id in output
    assert "barcode/catalog and disc IDs" in output


def test_unknown_completeness_uses_track_only_until_identity_is_established(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, paths, _ = generated_album(tmp_path, monkeypatch, (2,), [(1, 1), (1, 2)])
    for path in paths.values():
        audio = FLAC(path)
        del audio["ARTIST"]
        del audio["ALBUMARTIST"]
        audio.save()
    assert (
        tagger.assess_album_completeness(list(paths.values()), folder, external_disc_counts={1: 2}).state == "UNKNOWN"
    )
    with requests.Session() as session:
        assert tagger.process_album(f"42:::{folder}", "fixture", "fixture@example.org", True, False, True, 2, session)
    rows = [json.loads(line) for line in (folder / ".metadata-provenance.jsonl").read_text().splitlines()]
    assert {row["context"]["album_completeness"]["state"] for row in rows if "context" in row} == {"UNKNOWN"}
    assert [row["mode"] for row in rows if row.get("kind") == "loudness_scan"] == ["track"]
    for path in paths.values():
        assert not FLAC(path).get("REPLAYGAIN_ALBUM_GAIN")
    # A later run may legitimately use the now-established artist identity.
    with requests.Session() as session:
        assert tagger.process_album(f"42:::{folder}", "fixture", "fixture@example.org", True, False, True, 2, session)
    assert all(FLAC(path).get("REPLAYGAIN_ALBUM_GAIN") for path in paths.values())


def test_ambiguous_release_never_writes_candidate_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder, paths, release = generated_album(tmp_path, monkeypatch, (2,), [(1, 1), (1, 2)])
    other_id = "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee"
    original_mb = tagger.mb_get

    def mb(url: str, *args: Any, **kw: Any) -> Any:
        if "/ws/2/url?" in url:
            return 200, {"relations": [{"release": {"id": release["id"]}}, {"release": {"id": other_id}}]}
        if f"/release/{other_id}?" in url:
            return 200, {**release, "id": other_id}
        return original_mb(url, *args, **kw)

    monkeypatch.setattr(tagger, "mb_get", mb)
    for path in paths.values():
        audio = FLAC(path)
        audio["TRACKTOTAL"] = ["2"]
        audio.save()
    with requests.Session() as session:
        assert tagger.process_album(f"42:::{folder}", "fixture", "fixture@example.org", True, True, True, 2, session)
    rows = [json.loads(line) for line in (folder / ".metadata-provenance.jsonl").read_text().splitlines()]
    assert {row["context"]["release_match"]["confidence"] for row in rows if "context" in row} == {"AMBIGUOUS"}
    for path in paths.values():
        assert not FLAC(path).get("MUSICBRAINZ_ALBUMID")
        assert not FLAC(path).get("MUSICBRAINZ_TRACKID")


def test_recovery_excludes_concurrent_ledger_append(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import multiprocessing
    import os

    import album_transaction as transaction

    ledger = tmp_path / "ledger.jsonl"
    ledger.write_bytes(b"before\n")
    staging = tmp_path / ".metadata-transaction-fixture"
    staging.mkdir()
    incoming = staging / "incoming"
    incoming.write_bytes(b"after\n")
    transaction.prepare_commit(tmp_path, staging, [(incoming, ledger)])
    os.replace(incoming, ledger)
    context = multiprocessing.get_context("fork")
    started, done = context.Event(), context.Event()

    def append() -> None:
        started.set()
        metadata_writer.publish_provenance_bytes(ledger, b"concurrent\n")
        done.set()

    process = context.Process(target=append)
    digest = transaction.digest
    probed = False

    def probe(path: Path) -> str | None:
        nonlocal probed
        value = digest(path)
        if path == ledger and not probed:
            probed = True
            process.start()
            assert started.wait(5)
            assert not done.wait(0.3)
        return value

    monkeypatch.setattr(transaction, "digest", probe)
    try:
        transaction.recover_transaction(tmp_path, staging)
        assert done.wait(5)
    finally:
        if process.pid:
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join()
    assert process.exitcode == 0
    assert ledger.read_bytes() == b"before\nconcurrent\n"
    assert not staging.exists()
