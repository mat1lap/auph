"""Remaining audit recommendations: offline integration and recovery contracts."""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
import requests

from provider_cache import JsonCache, fetch_json


class Reply:
    status_code = 200

    def __init__(self, payload: Any) -> None:
        self.headers: dict[str, str] = {}
        self.payload = payload

    def json(self) -> Any:
        return self.payload

    def close(self) -> None:
        pass


class FixtureResponse(requests.Response):
    _content_consumed: bool


class Session(requests.Session):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        self.calls += 1
        response = FixtureResponse()
        response.status_code = 200
        response._content_consumed = True
        response._content = json.dumps(
            {
                "identity": request.headers.get("Authorization", "")
                + request.headers.get("Cookie", "").removeprefix("account=")
            }
        ).encode()
        return response


def test_session_query_parameters_are_part_of_actual_cache_identity(tmp_path: Path) -> None:
    class QuerySession(Session):
        def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
            response = super().send(request, **kwargs)
            response._content = json.dumps({"url": request.url}).encode()
            return response

    session = QuerySession()
    cache = JsonCache(tmp_path)
    for edition in ("one", "two"):
        session.params = {"edition": edition}
        result = fetch_json(session, "https://fixture.example/item", headers={}, cache=cache)
        assert result.payload == {"url": f"https://fixture.example/item?edition={edition}"}
    assert session.calls == 2


def test_disc_writer_refuses_unfinished_album_root_transaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import album_transaction
    import metadata_writer
    from test_entire_audit import album_fixture

    folder, tracks = album_fixture(tmp_path, monkeypatch)
    disc = folder / "CD1"
    disc.mkdir()
    track = tracks[0].rename(disc / tracks[0].name)
    transaction = folder / ".metadata-transaction-interrupted"
    transaction.mkdir()
    source = transaction / "staged.flac"
    before = track.read_bytes()
    source.write_bytes(before)
    album_transaction.prepare_commit(folder, transaction, [(source, track)])
    with pytest.raises(ValueError, match="recovery"):
        metadata_writer.write_tags(track, [("COMMENT", "uncommitted proposal")])
    assert track.read_bytes() == before
    assert (transaction / album_transaction.JOURNAL).exists()


def test_session_credentials_and_cookies_cannot_reuse_another_context(tmp_path: Path) -> None:
    session = Session()
    cache = JsonCache(tmp_path)
    for header, cookie in (("fixture-a", "one"), ("fixture-b", "one"), ("fixture-b", "two")):
        session.headers["Authorization"] = header
        session.cookies.set("account", cookie)
        response = fetch_json(session, "https://fixture.example/item", headers={}, cache=cache)
        assert response.payload == {"identity": header + cookie}
    assert session.calls == 3
    assert all("key" not in json.loads(path.read_text()) for path in cache.directory.glob("*.json"))


@pytest.mark.parametrize(
    "body, expected",
    [
        ({"status": "error", "error": {"code": 4, "message": "invalid API key"}}, "AUTHENTICATION_ERROR"),
        ({"status": "error", "error": {"code": 3, "message": "rate limit exceeded"}}, "RATE_LIMITED"),
        ({"error": {"message": "server temporarily unavailable"}}, "TRANSIENT_ERROR"),
    ],
)
def test_api_error_envelopes_have_actionable_status(tmp_path: Path, body: Any, expected: str) -> None:
    class FailedSession:
        def get(self, *_args: Any, **_kwargs: Any) -> Reply:
            return Reply(body)

    result = fetch_json(FailedSession(), "https://api.acoustid.org/v2/lookup", headers={}, cache=JsonCache(tmp_path))
    assert result.classification == expected
    assert not list(tmp_path.glob("*.json"))


def test_corrupt_cache_cannot_claim_a_different_key_or_negative_payload(tmp_path: Path) -> None:
    cache = JsonCache(tmp_path)
    cache.store("other", {"wrong": True})
    cache.path_for("expected").write_bytes(cache.path_for("other").read_bytes())
    assert cache.load("expected") is None
    path = cache.store("negative", {"wrong": True}, status=404)
    assert cache.load("negative") is None
    assert path.exists()


@pytest.mark.parametrize("phase", ["track", "ledger"])
def test_process_death_recovery_restores_real_album_pcm_and_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    import multiprocessing
    import os

    import album_transaction as transaction
    import audiophile_tagger as tagger
    import metadata_writer
    from test_entire_audit import album_fixture

    folder, tracks = album_fixture(tmp_path, monkeypatch)
    ledger = folder / ".metadata-provenance.jsonl"
    ledger.write_bytes(b'{"old":true}\n')
    before = {path: path.read_bytes() for path in [*tracks, ledger]}
    pcm = {path: metadata_writer.pcm_digest(path) for path in tracks}
    replace = os.replace

    def child() -> None:
        def terminate(source: Any, destination: Any) -> None:
            replace(source, destination)
            if (phase == "track" and Path(destination) == tracks[0] and Path(source).parent.name == "pending") or (
                phase == "ledger" and Path(destination) == ledger
            ):
                os._exit(77)

        monkeypatch.setattr(tagger.os, "replace", terminate)
        with requests.Session() as session:
            tagger.process_album(f"42:::{folder}", "fixture", "fixture@example.org", True, True, True, 1, session)

    process = multiprocessing.get_context("fork").Process(target=child)
    process.start()
    process.join(20)
    if process.is_alive():
        process.kill()
        process.join()
    assert process.exitcode == 77
    journals = transaction.unfinished_transactions(folder)
    assert len(journals) == 1
    assert any(path.read_bytes() != before[path] for path in tracks)
    # Refuse a new enrichment before any provider call while recovery is pending.
    monkeypatch.setattr(tagger, "discogs_get", lambda *_a, **_k: pytest.fail("providers must not run before recovery"))
    with requests.Session() as session:
        assert not tagger.process_album(
            f"42:::{folder}", "fixture", "fixture@example.org", True, True, True, 1, session
        )
    damaged = {path: path.read_bytes() for path in [*tracks, ledger]}
    assert transaction.main([str(folder), "--dry-run"]) == 0
    assert {path: path.read_bytes() for path in damaged} == damaged
    assert transaction.main([str(folder)]) == 0
    assert {path: path.read_bytes() for path in before} == before
    assert {path: metadata_writer.pcm_digest(path) for path in tracks} == pcm
    assert not transaction.unfinished_transactions(folder)
    assert transaction.main([str(folder)]) == 0


def journal_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    import album_transaction as transaction

    folder = tmp_path / "Album"
    folder.mkdir()
    staging = folder / ".metadata-transaction-fixture"
    staging.mkdir()
    cover = folder / "cover.jpg"
    cover.write_bytes(b"original cover")
    source = staging / "cover.new"
    source.write_bytes(b"new cover")
    lyric = folder / "song.lrc"
    lyrics_source = staging / "new.lrc"
    lyrics_source.write_bytes(b"[00:01.23]real timing")
    transaction.prepare_commit(folder, staging, [(source, cover), (lyrics_source, lyric)])
    cover.write_bytes(source.read_bytes())
    lyric.write_bytes(lyrics_source.read_bytes())
    return folder, staging, cover, lyric


def test_journal_restores_artifacts_and_retains_snapshots_on_recovery_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    import album_transaction as transaction

    folder, staging, cover, lyric = journal_fixture(tmp_path)
    replace = os.replace

    def fail_restore(src: Any, dst: Any) -> None:
        if Path(dst) == cover:
            raise OSError("injected restore failure")
        replace(src, dst)

    monkeypatch.setattr(transaction.os, "replace", fail_restore)
    assert transaction.main([str(folder)]) == 1
    assert staging.is_dir()
    assert (staging / "recovery" / "0").read_bytes() == b"original cover"
    monkeypatch.setattr(transaction.os, "replace", replace)
    assert transaction.main([str(folder)]) == 0
    assert cover.read_bytes() == b"original cover"
    assert not lyric.exists()


@pytest.mark.parametrize("damage", ["unrelated-edit", "snapshot", "traversal", "symlink"])
def test_recovery_preflights_every_entry_before_any_mutation(tmp_path: Path, damage: str) -> None:
    import album_transaction as transaction

    folder, staging, cover, lyric = journal_fixture(tmp_path)
    if damage == "unrelated-edit":
        lyric.write_bytes(b"curated user edit after crash")
    elif damage == "snapshot":
        (staging / "recovery" / "0").write_bytes(b"damaged backup")
    elif damage == "symlink":
        lyric.unlink()
        lyric.symlink_to(tmp_path / "outside.lrc")
    else:
        journal = staging / transaction.JOURNAL
        data = json.loads(journal.read_text())
        data["entries"][1]["path"] = "../outside.lrc"
        journal.write_text(json.dumps(data))
    current_cover = cover.read_bytes()
    assert transaction.main([str(folder)]) == 1
    assert cover.read_bytes() == current_cover
    assert staging.exists()


def test_committed_journal_cleanup_preserves_published_outputs(tmp_path: Path) -> None:
    import album_transaction as transaction

    folder, staging, cover, lyric = journal_fixture(tmp_path)
    transaction.finish_commit(folder, staging)
    assert transaction.main([str(folder)]) == 0
    assert cover.read_bytes() == b"new cover"
    assert lyric.read_bytes() == b"[00:01.23]real timing"
    assert not staging.exists()


def test_concurrent_process_ledger_appends_retain_every_committed_row(tmp_path: Path) -> None:
    import multiprocessing

    import metadata_writer

    ledger = tmp_path / "provenance.jsonl"
    context = multiprocessing.get_context("fork")
    start = context.Event()

    def worker(number: int) -> None:
        start.wait(10)
        for row in range(8):
            metadata_writer._append_provenance_record(ledger, {"worker": number, "row": row})

    processes = [context.Process(target=worker, args=(number,)) for number in range(4)]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(15)
        if process.is_alive():
            process.kill()
            process.join()
        assert process.exitcode == 0
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert {(row["worker"], row["row"]) for row in rows} == {(worker, row) for worker in range(4) for row in range(8)}
    assert len(rows) == 32


def test_standalone_writers_serialize_snapshot_through_commit(tmp_path: Path) -> None:
    import multiprocessing
    import os
    import time

    from mutagen.flac import FLAC

    import metadata_writer
    from test_pipeline_safety import make_flac

    track = make_flac(tmp_path / "song.flac")
    before_pcm = metadata_writer.pcm_digest(track)
    context = multiprocessing.get_context("fork")
    start = context.Event()

    def worker(number: int) -> None:
        replace = os.replace

        def delay(src: Any, dst: Any) -> None:
            if Path(dst) == track:
                time.sleep(0.1)
            replace(src, dst)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(metadata_writer.os, "replace", delay)
            start.wait(10)
            metadata_writer.write_tags(track, [(f"CUSTOM_{number}", f"value {number}")])

    processes = [context.Process(target=worker, args=(number,)) for number in range(3)]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(15)
        if process.is_alive():
            process.kill()
            process.join()
        assert process.exitcode == 0
    audio = FLAC(track)
    assert all(audio[f"CUSTOM_{number}"] == [f"value {number}"] for number in range(3))
    assert metadata_writer.pcm_digest(track) == before_pcm


def test_lrclib_workers_do_not_share_mutable_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import concurrent.futures
    import threading

    import lyrics_utils as utils

    seen: dict[int, set[int]] = {}
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    monkeypatch.setattr(utils, "_LRCLIB_SESSION", None)

    def fake_fetch(session: Any, *_args: Any, **_kwargs: Any) -> Any:
        from provider_cache import ProviderResult

        with lock:
            seen.setdefault(threading.get_ident(), set()).add(id(session))
        if len(seen[threading.get_ident()]) == 1 and _args and "/get?" in _args[0]:
            barrier.wait(5)
        return ProviderResult(404, None, 1)

    monkeypatch.setattr(utils, "fetch_json", fake_fetch)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(utils.fetch_lrclib, f"Song {number}", "Artist") for number in range(2)]
        for future in futures:
            future.result()
    assert len(seen) == 2
    assert all(len(ids) == 1 for ids in seen.values())
    assert len(set.union(*seen.values())) == 2


@pytest.mark.parametrize("vary", ["*", "Accept", "X-Edition"])
def test_unkeyed_server_vary_is_usable_but_not_persisted(tmp_path: Path, vary: str) -> None:
    class VarySession:
        def get(self, *_args: Any, **_kwargs: Any) -> Reply:
            response = Reply({"value": "usable"})
            response.headers["Vary"] = vary
            return response

    response = fetch_json(VarySession(), "https://fixture/item", headers={}, cache=JsonCache(tmp_path))
    assert response.ok and response.payload == {"value": "usable"}
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.parametrize("channels, width, rate", [(1, 1, 8000), (2, 2, 44100), (2, 3, 48000), (2, 4, 96000)])
def test_block_audio_analysis_matches_full_pcm_and_retains_source(
    tmp_path: Path, channels: int, width: int, rate: int
) -> None:
    import subprocess
    import wave

    import numpy as np

    import audiophile_tagger as tagger
    import dr14_parse as dr
    import metadata_writer

    source = tmp_path / "analysis.wav"
    frames = rate * 7 + 123
    values = (np.sin(np.arange(frames) * 2 * np.pi * 440 / rate) * 0.7 * (2 ** (8 * width - 1))).astype(np.int32)
    values = np.repeat(values[:, None], channels, axis=1).reshape(-1)
    if width == 1:
        raw = (values + 128).astype(np.uint8).tobytes()
    elif width == 3:
        raw = np.column_stack([values & 255, (values >> 8) & 255, (values >> 16) & 255]).astype(np.uint8).tobytes()
    else:
        raw = values.astype(f"<i{width}").tobytes()
    with wave.open(str(source), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(width)
        output.setframerate(rate)
        output.writeframes(raw)
    track = tmp_path / "analysis.flac"
    subprocess.run(["flac", "-s", "-f", "-o", str(track), str(source)], check=True, capture_output=True)
    pcm_before = metadata_writer.pcm_digest(track)
    expected = dr.compute_dr14_samples(dr.decode_pcm(raw, width, channels, unsigned_eight_bit=True), rate)
    for file in (source, track):
        measured = dr.measure_audio_file(file)
        assert (measured["dr"], measured["peak"], measured["rms"]) == expected
    blocks = list(dr.iter_flac_pcm(track, block_frames=rate))
    assert len(blocks) == 8 and max(len(block) for block in blocks) <= rate
    assert tagger.get_track_eac_peak([], 1, track) == ("69.5 %" if width == 1 else "70.0 %")
    assert metadata_writer.pcm_digest(track) == pcm_before


def lyricsfile_source() -> str:
    import yaml

    from test_selection_audit import TEXT

    return yaml.safe_dump(
        {
            "version": "1.0",
            "metadata": {"title": "Fixture Song", "artist": "Fixture Artist", "duration_ms": 120000, "language": "en"},
            "plain": TEXT,
            "lines": [
                {
                    "text": text,
                    "start_ms": number * 1000 + 123,
                    "end_ms": number * 1000 + 923,
                    "words": [{"text": text, "start_ms": number * 1000 + 123, "end_ms": number * 1000 + 923}],
                }
                for number, text in enumerate(TEXT.splitlines())
            ],
        },
        allow_unicode=True,
        sort_keys=False,
    )


def test_lrclib_lyricsfile_only_survives_selection_materialization_and_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mutagen.flac import FLAC

    import lyrics_pipeline as pipeline
    import lyrics_utils as utils
    import metadata_writer
    from test_pipeline_safety import make_flac
    from test_selection_audit import IDENTITY, TEXT

    source = lyricsfile_source()
    item = {
        "id": 101,
        "trackName": "Fixture Song",
        "artistName": "Fixture Artist",
        "duration": 120.0,
        "lyricsfile": source,
    }

    class LyricsSession:
        def get(self, url: str, **_kwargs: Any) -> Reply:
            return Reply(item if "/get?" in url else [item])

    monkeypatch.setattr(utils, "_LRCLIB_SESSION", LyricsSession())
    monkeypatch.setattr(utils, "_LRCLIB_CACHE", JsonCache(tmp_path / "cache"))
    acquired = utils.fetch_lrclib("Fixture Song", "Fixture Artist", duration=120)
    assert acquired is not None and acquired.get("found")
    assert acquired["plainLyrics"] == TEXT and "[00:00.123]" in acquired["syncedLyrics"]
    assessment = pipeline.choose_provider_result([acquired], **IDENTITY)
    assert assessment is not None and assessment.candidate.synced
    track = make_flac(tmp_path / "song.flac", seconds=120)
    before_pcm = metadata_writer.pcm_digest(track)
    resolution = pipeline.materialize_selected(track, assessment)
    assert resolution.paths["lyricsfile"].read_bytes() == source.encode()
    assert resolution.paths["plain"].read_text() == TEXT
    assert "enhanced" in resolution.paths and "ttml" in resolution.paths
    metadata_writer.write_tags(
        track,
        [],
        lyrics_file=resolution.paths["plain"],
        synced_lyrics_file=resolution.paths["synced"],
        provenance_context={
            "lyrics": {"provider": acquired["source"], "source_file": resolution.paths["lyricsfile"].name}
        },
    )
    audio = FLAC(track)
    assert audio["UNSYNCEDLYRICS"] == [TEXT]
    assert "[00:00.123]" in audio["LYRICS"][0]
    assert metadata_writer.pcm_digest(track) == before_pcm
    ledger = json.loads((tmp_path / ".metadata-provenance.jsonl").read_text())
    assert resolution.paths["lyricsfile"].name in json.dumps(ledger)


@pytest.mark.parametrize("change", ["invalid-line", "invalid-word", "offset", "missing-end"])
def test_lyricsfile_keeps_plain_without_inventing_timing(change: str) -> None:
    import yaml

    from lyricsfile import parse_lyricsfile
    from test_selection_audit import TEXT

    data = yaml.safe_load(lyricsfile_source())
    if change == "invalid-line":
        data["lines"][1]["start_ms"] = -1
    elif change == "invalid-word":
        data["lines"][1]["words"][0]["start_ms"] = -1
    elif change == "offset":
        data["metadata"]["offset_ms"] = 1000
    else:
        for line in data["lines"]:
            del line["end_ms"]
            del line["words"][0]["end_ms"]
    parsed = parse_lyricsfile(yaml.safe_dump(data), title="Fixture Song", artist="Fixture Artist", duration=120)
    assert parsed["plainLyrics"] == TEXT
    assert not parsed.get("ttmlLyrics")
    if change in {"invalid-line", "offset"}:
        assert not parsed.get("syncedLyrics")
    else:
        assert parsed.get("syncedLyrics")


@pytest.mark.parametrize(
    "source",
    [
        "version: '1.0'\nversion: '1.0'\nmetadata: {}",
        "version: '1.0'\nmetadata: &m {title: Song, artist: Artist}\ncopy: *m",
        "!!python/object/apply:os.system ['false']",
        "version: '2.0'\nmetadata: {title: Song, artist: Artist}",
        "version: '1.0'\nmetadata: {title: 'Fixture Song (Live)', artist: 'Fixture Artist'}\nplain: wrong version",
    ],
)
def test_lyricsfile_rejects_unsafe_structure_and_conflicting_identity(source: str) -> None:
    from lyricsfile import parse_lyricsfile

    with pytest.raises(ValueError):
        parse_lyricsfile(source, title="Fixture Song", artist="Fixture Artist", duration=120)


def test_worker_session_preserves_transport_and_isolates_headers_cookies() -> None:
    import concurrent.futures
    import threading

    from http_sessions import isolated_worker_session

    root = requests.Session()
    root.headers["Authorization"] = "fixture-token"
    root.cookies.set("account", "original")
    barrier = threading.Barrier(2)

    @isolated_worker_session
    def worker(number: int, session: requests.Session) -> tuple[str, str]:
        assert session.get_adapter("https://example.org") is root.get_adapter("https://example.org")
        assert session.headers["Authorization"] == "fixture-token"
        session.cookies.set("account", str(number))
        session.headers["X-Track"] = str(number)
        barrier.wait(5)
        return str(session.cookies.get("account")), str(session.headers["X-Track"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker, number, root) for number in range(2)]
        assert [future.result() for future in futures] == [("0", "0"), ("1", "1")]
    assert root.cookies.get("account") == "original" and "X-Track" not in root.headers
    root.close()


@pytest.mark.parametrize("phase", ["track", "ledger"])
def test_standalone_writer_process_death_is_recoverable(tmp_path: Path, phase: str) -> None:
    import multiprocessing
    import os

    import album_transaction as transaction
    import metadata_writer
    from test_pipeline_safety import make_flac

    track = make_flac(tmp_path / "standalone.flac")
    ledger = tmp_path / ".metadata-provenance.jsonl"
    ledger.write_bytes(b'{"previous":true}\n')
    before = {path: path.read_bytes() for path in (track, ledger)}
    pcm = metadata_writer.pcm_digest(track)

    def worker() -> None:
        replace = os.replace

        def terminate(src: Any, dst: Any) -> None:
            replace(src, dst)
            if (phase == "track" and Path(dst) == track and Path(src).name.endswith(".pending.flac")) or (
                phase == "ledger" and Path(dst) == ledger
            ):
                os._exit(77)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(metadata_writer.os, "replace", terminate)
            metadata_writer.write_tags(track, [("CUSTOM", "new value")])

    process = multiprocessing.get_context("fork").Process(target=worker)
    process.start()
    process.join(15)
    if process.is_alive():
        process.kill()
        process.join()
    assert process.exitcode == 77
    with pytest.raises(ValueError, match="requires recovery"):
        metadata_writer.write_tags(track, [("CUSTOM", "other value")])
    assert transaction.main([str(tmp_path)]) == 0
    assert {path: path.read_bytes() for path in before} == before
    assert metadata_writer.pcm_digest(track) == pcm


def test_committed_marker_interruption_rearms_before_album_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    import album_transaction as transaction
    import audiophile_tagger as tagger
    from test_entire_audit import album_fixture

    folder, tracks = album_fixture(tmp_path, monkeypatch)
    before = {track: track.read_bytes() for track in tracks}
    replace = os.replace
    interrupted = False

    def interrupt(src: Any, dst: Any) -> None:
        nonlocal interrupted
        replace(src, dst)
        if Path(dst).name == transaction.JOURNAL and not interrupted:
            data = json.loads(Path(dst).read_text())
            if data["state"] == "committed":
                interrupted = True
                raise KeyboardInterrupt("marker published before directory sync")

    monkeypatch.setattr(tagger.os, "replace", interrupt)
    with requests.Session() as session, pytest.raises(KeyboardInterrupt):
        tagger.process_album(f"42:::{folder}", "fixture", "fixture@example.org", True, True, True, 1, session)
    assert interrupted and {track: track.read_bytes() for track in tracks} == before
    assert not transaction.unfinished_transactions(folder)


def test_named_rate_limiter_paces_independent_processes(tmp_path: Path) -> None:
    import multiprocessing
    import time

    from provider_cache import IntervalRateLimiter

    context = multiprocessing.get_context("fork")
    start = context.Event()
    times = context.Queue()

    def worker() -> None:
        limiter = IntervalRateLimiter(0.05, shared_name=str(tmp_path))
        start.wait(5)
        limiter.acquire()
        times.put(time.time())

    processes = [context.Process(target=worker) for _ in range(3)]
    for process in processes:
        process.start()
    start.set()
    measured = sorted(times.get(timeout=10) for _ in processes)
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    assert all(second - first >= 0.045 for first, second in pairwise(measured))


def test_dry_run_reports_exact_proposals_and_leaves_all_album_contents_unchanged(tmp_path: Path) -> None:
    import metadata_writer
    from test_pipeline_safety import make_flac

    track = make_flac(tmp_path / "song.flac")
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    report = metadata_writer.write_tags(track, [("CUSTOM", "café 日本語")], dry_run=True, source="fixture")
    assert report.changes["CUSTOM"] == {
        "before": [],
        "proposed": ["café 日本語"],
        "source": "fixture",
        "confidence": "VERIFIED",
    }
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()} == before
    assert not list(tmp_path.glob(".metadata-transaction-*"))


def test_durable_writer_cleanup_failure_does_not_revert_successful_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mutagen.flac import FLAC

    import album_transaction as transaction
    import metadata_writer
    from test_pipeline_safety import make_flac

    track = make_flac(tmp_path / "song.flac")
    pcm = metadata_writer.pcm_digest(track)
    rmtree = metadata_writer.shutil.rmtree

    def fail_cleanup(path: Any, **_kwargs: Any) -> None:
        if Path(path).name.startswith(".metadata-transaction-writer-"):
            raise OSError("injected cleanup failure")
        rmtree(path)

    monkeypatch.setattr(metadata_writer.shutil, "rmtree", fail_cleanup)
    with pytest.raises(RuntimeError, match="write committed"):
        metadata_writer.write_tags(track, [("CUSTOM", "committed")])
    assert FLAC(track)["CUSTOM"] == ["committed"]
    assert metadata_writer.pcm_digest(track) == pcm
    ledger = json.loads((tmp_path / ".metadata-provenance.jsonl").read_text())
    assert ledger["after_tags"]["CUSTOM"] == ["committed"]
    monkeypatch.setattr(metadata_writer.shutil, "rmtree", rmtree)
    assert transaction.main([str(tmp_path)]) == 0
    assert FLAC(track)["CUSTOM"] == ["committed"]


def test_dry_run_conflict_distinguishes_proposed_value_from_retained_value(tmp_path: Path) -> None:
    from mutagen.flac import FLAC

    import metadata_writer
    from test_pipeline_safety import make_flac

    track = make_flac(tmp_path / "song.flac")
    audio = FLAC(track)
    audio["TITLE"] = ["Curated title"]
    audio.save()
    report = metadata_writer.write_tags(
        track, [("TITLE", "Remote title")], dry_run=True, existing_confidence=metadata_writer.Confidence.UNKNOWN
    )
    assert report.changes["TITLE"]["before"] == ["Curated title"]
    assert report.changes["TITLE"]["proposed"] == ["Remote title"]
    assert report.changes["TITLE"]["applied"] is False
    assert FLAC(track)["TITLE"] == ["Curated title"]


@pytest.mark.parametrize(
    "message, classification", [("invalid API key", "AUTHENTICATION_ERROR"), ("rate limit exceeded", "RATE_LIMITED")]
)
def test_lrclib_preserves_transport_error_classification_through_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str, classification: str
) -> None:
    import lyrics_utils as utils

    class FailedSession:
        def get(self, *_args: Any, **_kwargs: Any) -> Reply:
            return Reply({"error": {"message": message}})

    monkeypatch.setattr(utils, "_LRCLIB_SESSION", FailedSession())
    monkeypatch.setattr(utils, "_LRCLIB_CACHE", JsonCache(tmp_path))
    result = utils.fetch_lrclib("Song", "Artist", duration=120)
    assert result is not None and result["acquisition_status"] == classification
    assert not list(tmp_path.glob("*.json"))
