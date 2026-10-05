"""Independent failure-boundary regressions for implementation commit 663992b."""

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
from typing import Any

import pytest
from mutagen.flac import FLAC

import album_transaction as transaction
import lyrics_pipeline as pipeline
import lyrics_utils as utils
import metadata_writer
from process_lock import file_lock
from provider_cache import JsonCache, fetch_json
from test_backlog import Reply, journal_fixture, lyricsfile_source
from test_pipeline_safety import make_flac
from test_selection_audit import IDENTITY, TEXT


@pytest.mark.parametrize("completed", [False, True])
def test_recovery_retries_after_partial_snapshot_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, completed: bool
) -> None:
    folder, staging, cover, lyric = journal_fixture(tmp_path)
    if completed:
        transaction.finish_commit(folder, staging)
    remove_tree = transaction.shutil.rmtree
    interrupted = False

    def partial_cleanup(path: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal interrupted
        if not interrupted and (Path(path) == staging or Path(path) == staging / "recovery"):
            interrupted = True
            (staging / "recovery" / "0").unlink()
            raise OSError("cleanup interrupted after removing a snapshot")
        remove_tree(path, *args, **kwargs)

    monkeypatch.setattr(transaction.shutil, "rmtree", partial_cleanup)
    assert transaction.main([str(folder)]) == 1
    assert interrupted
    assert cover.read_bytes() == (b"new cover" if completed else b"original cover")
    assert lyric.exists() is completed
    assert transaction.main([str(folder)]) == 0
    assert not staging.exists()
    assert transaction.main([str(folder)]) == 0


def test_recovery_retries_after_interruption_following_artifact_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, staging, cover, lyric = journal_fixture(tmp_path)
    unlink = Path.unlink

    def interrupt(path: Path, *args: Any, **kwargs: Any) -> None:
        unlink(path, *args, **kwargs)
        if path == lyric:
            raise KeyboardInterrupt("interrupted after removing a new artifact")

    monkeypatch.setattr(Path, "unlink", interrupt)
    with pytest.raises(KeyboardInterrupt):
        transaction.main([str(folder)])
    assert cover.read_bytes() == b"original cover" and not lyric.exists()
    assert (staging / "recovery" / "0").read_bytes() == b"original cover"
    monkeypatch.setattr(Path, "unlink", unlink)
    assert transaction.main([str(folder)]) == 0


def test_writer_archive_publication_survives_process_death(tmp_path: Path) -> None:
    track = make_flac(tmp_path / "song.flac")
    audio = FLAC(track)
    original = "[Verse]\n" + TEXT
    audio["LYRICS"] = [original]
    audio.save()
    ledger = tmp_path / ".metadata-provenance.jsonl"
    ledger.write_bytes(b'{"old":true}\n')
    before = {path: path.read_bytes() for path in (track, ledger)}
    pcm = metadata_writer.pcm_digest(track)

    def worker() -> None:
        path_open, link = Path.open, os.link

        def kill_open(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            handle = path_open(path, mode, *args, **kwargs)
            if mode == "x" and ".lyrics-tag-archive." in path.name:
                os._exit(88)
            return handle

        def kill_link(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
            link(src, dst, *args, **kwargs)
            if ".lyrics-tag-archive." in Path(dst).name:
                os._exit(88)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(Path, "open", kill_open)
            patch.setattr(os, "link", kill_link)
            metadata_writer.write_tags(track, [("CUSTOM", "new")])

    process = multiprocessing.get_context("fork").Process(target=worker)
    process.start()
    process.join(15)
    if process.is_alive():
        process.kill()
        process.join()
    assert process.exitcode == 88
    archive = next(tmp_path.glob("*.lyrics-tag-archive.*.json"))
    assert json.loads(archive.read_text()) == [original]
    assert transaction.main([str(tmp_path)]) == 0
    assert {path: path.read_bytes() for path in before} == before
    assert metadata_writer.pcm_digest(track) == pcm
    assert not archive.exists()


def test_forked_child_does_not_inherit_reentrant_lock_ownership(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    acquired = context.Event()
    path = tmp_path / "ledger"

    def worker() -> None:
        with file_lock(path):
            acquired.set()

    with file_lock(path):
        process = context.Process(target=worker)
        process.start()
        acquired_while_parent_locked = acquired.wait(0.2)
    try:
        assert not acquired_while_parent_locked
        assert acquired.wait(5)
    finally:
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join()
    assert process.exitcode == 0


def test_negative_cache_respects_unknown_vary(tmp_path: Path) -> None:
    class Transport:
        calls = 0

        def get(self, *_args: Any, **kwargs: Any) -> Reply:
            self.calls += 1
            response = Reply({"plainLyrics": TEXT})
            response.status_code = 404 if kwargs["headers"]["Accept"] == "application/absent" else 200
            response.headers["Vary"] = "Accept"
            return response

    transport = Transport()
    cache = JsonCache(tmp_path)
    assert (
        fetch_json(transport, "https://fixture/item", headers={"Accept": "application/absent"}, cache=cache).status
        == 404
    )
    found = fetch_json(transport, "https://fixture/item", headers={"Accept": "application/json"}, cache=cache)
    assert found.ok and found.payload == {"plainLyrics": TEXT}
    assert transport.calls == 2 and not list(tmp_path.glob("*.json"))


def test_lrclib_mixed_search_results_retain_usable_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = {
        "id": 101,
        "trackName": "Fixture Song",
        "artistName": "Fixture Artist",
        "duration": 120,
        "plainLyrics": TEXT,
    }

    class Transport:
        def get(self, url: str, **_kwargs: Any) -> Reply:
            response = Reply([None, item])
            if "/get?" in url:
                response.status_code = 404
            return response

    monkeypatch.setattr(utils, "_LRCLIB_SESSION", Transport())
    monkeypatch.setattr(utils, "_LRCLIB_CACHE", JsonCache(tmp_path))
    result = utils.fetch_lrclib("Fixture Song", "Fixture Artist", duration=120)
    assert result is not None
    chosen = pipeline.choose_provider_result([result], **IDENTITY)
    assert chosen is not None and chosen.candidate.plain_lyrics == TEXT
    assert chosen.warnings


def test_lrclib_invalid_schema_is_evicted_using_actual_cache_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Transport:
        def get(self, *_args: Any, **_kwargs: Any) -> Reply:
            return Reply({"unexpected": "malformed schema"})

    from test_backlog import Session

    class AuthTransport(Session):
        def send(self, request: Any, **kwargs: Any) -> Any:
            response = super().send(request, **kwargs)
            response._content = b'{"unexpected":"malformed schema"}'
            return response

    transport = AuthTransport()
    transport.headers["Authorization"] = "fixture-context"
    cache = JsonCache(tmp_path)
    monkeypatch.setattr(utils, "_LRCLIB_SESSION", transport)
    monkeypatch.setattr(utils, "_LRCLIB_CACHE", cache)
    result = utils.fetch_lrclib("Fixture Song", "Fixture Artist", duration=120)
    assert result and result["acquisition_status"] == "PARSER_ERROR"
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.parametrize("bad_lines", [[42], "not a sequence"])
def test_lyricsfile_invalid_line_structure_retains_plain_through_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_lines: Any
) -> None:
    import yaml

    document = yaml.safe_load(lyricsfile_source())
    document["lines"] = bad_lines
    source = yaml.safe_dump(document)
    item = {
        "id": 101,
        "trackName": "Fixture Song",
        "artistName": "Fixture Artist",
        "duration": 120,
        "lyricsfile": source,
    }

    class Transport:
        def get(self, url: str, **_kwargs: Any) -> Reply:
            return Reply(item if "/get?" in url else [item])

    monkeypatch.setattr(utils, "_LRCLIB_SESSION", Transport())
    monkeypatch.setattr(utils, "_LRCLIB_CACHE", JsonCache(tmp_path / "cache"))
    result = utils.fetch_lrclib("Fixture Song", "Fixture Artist", duration=120)
    assert result is not None
    chosen = pipeline.choose_provider_result([result], **IDENTITY)
    assert chosen is not None
    resolution = pipeline.materialize_selected(tmp_path / "song.flac", chosen)
    assert resolution.paths["plain"].read_text() == TEXT
    assert resolution.paths["lyricsfile"].read_text() == source
    assert "synced" not in resolution.paths and chosen.warnings


def test_album_rejects_and_recovers_interrupted_disc_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import requests

    import audiophile_tagger as tagger
    from test_entire_audit import album_fixture

    folder, tracks = album_fixture(tmp_path, monkeypatch)
    disc = folder / "CD1"
    disc.mkdir()
    track = tracks[0].rename(disc / tracks[0].name)
    tracks[1].rename(disc / tracks[1].name)
    before = track.read_bytes()
    pcm = metadata_writer.pcm_digest(track)

    def worker() -> None:
        replace = os.replace

        def kill(src: Any, dst: Any) -> None:
            replace(src, dst)
            if Path(dst) == track and Path(src).name.endswith(".pending.flac"):
                os._exit(89)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "replace", kill)
            metadata_writer.write_tags(track, [("CUSTOM", "new value")])

    process = multiprocessing.get_context("fork").Process(target=worker)
    process.start()
    process.join(15)
    if process.is_alive():
        process.kill()
        process.join()
    assert process.exitcode == 89
    queried = []
    discogs_get = tagger.discogs_get

    def observed(*args: Any, **kwargs: Any) -> Any:
        queried.append(True)
        return discogs_get(*args, **kwargs)

    monkeypatch.setattr(tagger, "discogs_get", observed)
    with requests.Session() as session:
        assert not tagger.process_album(
            f"42:::{folder}", "fixture", "fixture@example.org", True, True, True, 1, session
        )
    assert not queried, "unfinished child journal must block acquisition and another commit"
    assert transaction.main([str(folder), "--dry-run"]) == 0
    assert track.read_bytes() != before
    assert transaction.main([str(folder)]) == 0
    assert track.read_bytes() == before and metadata_writer.pcm_digest(track) == pcm
    assert not transaction.unfinished_transactions(folder)


def test_album_holds_shared_ledger_lock_from_snapshot_through_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import requests

    import audiophile_tagger as tagger
    from test_entire_audit import album_fixture

    folder, _tracks = album_fixture(tmp_path, monkeypatch)
    other_folder = tmp_path / "other album"
    other_folder.mkdir()
    other_track = make_flac(other_folder / "other.flac")
    ledger = folder / ".metadata-provenance.jsonl"
    context = multiprocessing.get_context("fork")
    started, done = context.Event(), context.Event()
    prepare = transaction.prepare_commit
    processes: list[Any] = []
    finished_during_commit: list[bool] = []

    def independent_append() -> None:
        started.set()
        metadata_writer.write_tags(other_track, [("CUSTOM", "independent")], provenance_file=ledger)
        done.set()

    def prepare_and_append(root: Path, staging: Path, outputs: list[tuple[Path, Path]]) -> None:
        prepare(root, staging, outputs)
        if root == folder:
            process = context.Process(target=independent_append)
            processes.append(process)
            process.start()
            assert started.wait(5)
            finished_during_commit.append(done.wait(0.4))

    monkeypatch.setattr(transaction, "prepare_commit", prepare_and_append)
    try:
        with requests.Session() as session:
            success = tagger.process_album(
                f"42:::{folder}", "fixture", "fixture@example.org", True, True, True, 1, session
            )
        assert success
        assert finished_during_commit == [False]
        assert done.wait(10)
    finally:
        for process in processes:
            process.join(10)
            if process.is_alive():
                process.kill()
                process.join()
    assert all(process.exitcode == 0 for process in processes)
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert sum(row.get("file") == other_track.name for row in rows) == 1
    for row in rows:
        if row.get("file") == other_track.name:
            assert row["transaction_id"] == FLAC(other_track)["METADATA_PROVENANCE_ID"][0]
    assert not transaction.unfinished_transactions(folder)


@pytest.mark.parametrize("outer_instrumental", [None, True])
def test_lrclib_instrumental_evidence_checks_native_lyricsfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outer_instrumental: bool | None
) -> None:
    import yaml

    metadata = {"title": "Fixture Song", "artist": "Fixture Artist", "instrumental": outer_instrumental is None}
    document = {"version": "1.0", "metadata": metadata}
    if outer_instrumental is True:
        document["plain"] = TEXT
    item: dict[str, Any] = {
        "trackName": "Fixture Song",
        "artistName": "Fixture Artist",
        "duration": 120,
        "lyricsfile": yaml.safe_dump(document),
    }
    if outer_instrumental is not None:
        item["instrumental"] = outer_instrumental

    class Transport:
        def get(self, url: str, **_kwargs: Any) -> Reply:
            return Reply(item if "/get?" in url else [])

    monkeypatch.setattr(utils, "_LRCLIB_SESSION", Transport())
    monkeypatch.setattr(utils, "_LRCLIB_CACHE", JsonCache(tmp_path))
    result = utils.fetch_lrclib("Fixture Song", "Fixture Artist", duration=120)
    assert result is not None
    if outer_instrumental is None:
        assert result["acquisition_status"] == "INSTRUMENTAL"
        assert result["lyricsfile"] == item["lyricsfile"]
        assert json.loads(result["providerRaw"])["lyricsfile"] == item["lyricsfile"]
    else:
        assert result.get("acquisition_status") != "INSTRUMENTAL"
        assert result.get("plainLyrics") == TEXT
        assert result["validationWarnings"]


def test_native_instrumental_source_survives_album_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.parse

    import requests
    import yaml

    import audiophile_tagger as tagger
    from test_entire_audit import album_fixture

    folder, tracks = album_fixture(tmp_path, monkeypatch)
    policy = tmp_path / "policy.toml"
    policy.write_text("[lyrics]\nonline_lookup=true\n")
    pcm = {path: metadata_writer.pcm_digest(path) for path in tracks}

    class Transport:
        def get(self, url: str, **_kwargs: Any) -> Reply:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            title = query.get("track_name", [""])[0]
            artist = query.get("artist_name", [""])[0]
            source = yaml.safe_dump(
                {"version": "1.0", "metadata": {"title": title, "artist": artist, "instrumental": True}}
            )
            return Reply(
                {
                    "id": 100 + int(title.rsplit(" ", 1)[-1]),
                    "trackName": title,
                    "artistName": artist,
                    "albumName": "Album",
                    "duration": 1,
                    "lyricsfile": source,
                }
            )

    def single_provider(**kwargs: Any) -> Any:
        return utils.fetch_lrclib(kwargs["song"], kwargs["artist"], album=kwargs["album"], duration=kwargs["duration"])

    monkeypatch.setattr(utils, "_LRCLIB_SESSION", Transport())
    monkeypatch.setattr(utils, "_LRCLIB_CACHE", JsonCache(tmp_path / "cache"))
    monkeypatch.setattr(utils, "fetch_betterlyrics", single_provider)
    with requests.Session() as session:
        assert tagger.process_album(f"42:::{folder}", "fixture", "fixture@example.org", True, True, True, 1, session)
    rows = [json.loads(line) for line in (folder / ".metadata-provenance.jsonl").read_text().splitlines()]
    for number, path in enumerate(tracks, 1):
        assert metadata_writer.pcm_digest(path) == pcm[path]
        assert FLAC(path)["INSTRUMENTAL"] == ["1"]
        source = next(folder.glob(path.stem + "*.lyricsfile.yaml"))
        assert yaml.safe_load(source.read_text())["metadata"]["instrumental"] is True
        row = next(row for row in rows if row.get("file") == path.name)
        assert row["context"]["lyrics"]["status"] == "INSTRUMENTAL"
        assert row["context"]["lyrics"]["provider_track_id"] == str(100 + number)
        assert "lyricsfile" in row["context"]["lyrics"]["outputs"]
        assert "LYRICS" not in FLAC(path)


def test_unsafe_v10_negative_cache_entry_is_refetched(tmp_path: Path) -> None:
    cache = JsonCache(tmp_path)
    url = "https://fixture/item"
    path = cache.store(url, None, status=404)
    envelope = json.loads(path.read_text())
    envelope["parser_version"] = "2026-10-05-lyrics-v10"
    path.write_text(json.dumps(envelope))

    class Transport:
        calls = 0

        def get(self, *_args: Any, **_kwargs: Any) -> Reply:
            self.calls += 1
            return Reply({"plainLyrics": TEXT})

    transport = Transport()
    result = fetch_json(transport, url, headers={}, cache=cache)
    assert result.ok and result.payload == {"plainLyrics": TEXT}
    assert transport.calls == 1 and not result.cached


@pytest.mark.parametrize("flag", ["false", 1])
def test_lrclib_malformed_instrumental_flag_is_not_no_vocals_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: Any
) -> None:
    class Transport:
        def get(self, url: str, **_kwargs: Any) -> Reply:
            item = {"trackName": "Fixture Song", "artistName": "Fixture Artist", "duration": 120, "instrumental": flag}
            return Reply(item if "/get?" in url else [])

    monkeypatch.setattr(utils, "_LRCLIB_SESSION", Transport())
    monkeypatch.setattr(utils, "_LRCLIB_CACHE", JsonCache(tmp_path))
    result = utils.fetch_lrclib("Fixture Song", "Fixture Artist", duration=120)
    assert result is not None and result["acquisition_status"] == "PARSER_ERROR"
