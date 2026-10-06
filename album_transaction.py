"""Durable rollback journal for album publication; POSIX/local filesystems.

Several replacements remain visible individually. Recovery refuses unrelated
edits, ambiguous paths and damaged snapshots, and retains the entire journal
until every restore succeeds. It never uses untrusted journal paths unchecked.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from library_layout import DISC_DIR_RE
from process_lock import file_lock, writer_album_root

JOURNAL = "commit-journal.json"


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def digest(path: Path) -> str | None:
    if path.is_symlink():
        raise ValueError(f"refusing symlinked transaction path: {path}")
    if not path.exists():
        return None
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def checked_path(root: Path, relative: str) -> Path:
    part = Path(relative)
    if not relative or part.is_absolute() or ".." in part.parts or part == Path("."):
        raise ValueError(f"unsafe recovery path: {relative!r}")
    path = root / part
    current = root
    for component in part.parts:
        current /= component
        if current.is_symlink():
            raise ValueError(f"symlink in recovery path: {relative}")
    return path


def write_journal(transaction: Path, data: dict[str, Any]) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".journal-", dir=transaction)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, transaction / JOURNAL)
        fsync_directory(transaction)
    finally:
        Path(name).unlink(missing_ok=True)


def prepare_commit(folder: Path, transaction: Path, outputs: list[tuple[Path, Path]]) -> None:
    """Snapshot every destination and sync journal BEFORE first publication.

    Outputs pair staged source and final destination, including the merged
    ledger. Existing transaction backups are reused for FLACs by the caller;
    recovery snapshots are independent of catchable rollback consumption.
    """
    folder = folder.resolve()
    transaction = checked_path(folder, str(transaction.relative_to(folder)))
    recovery = transaction / "recovery"
    recovery.mkdir()
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source, destination in outputs:
        relative = str(destination.relative_to(folder))
        if relative in seen:
            raise ValueError(f"duplicate publication destination: {relative}")
        seen.add(relative)
        checked_path(folder, relative)
        after = digest(source)
        if after is None:
            raise ValueError(f"missing staged output: {source}")
        before = digest(destination)
        backup_relative: str | None = None
        if before is not None:
            backup = recovery / str(len(entries))
            shutil.copy2(destination, backup)
            with backup.open("rb") as handle:
                os.fsync(handle.fileno())
            if digest(backup) != before or digest(destination) != before:
                raise ValueError(f"destination changed while snapshotting: {relative}")
            backup_relative = str(backup.relative_to(transaction))
        with source.open("rb") as handle:
            os.fsync(handle.fileno())
        entries.append({"path": relative, "before": before, "after": after, "backup": backup_relative})
    fsync_directory(recovery)
    write_journal(transaction, {"schema": 1, "state": "prepared", "entries": entries})
    fsync_directory(folder)


def load_journal(transaction: Path) -> dict[str, Any]:
    path = checked_path(transaction, JOURNAL)
    if path.stat().st_size > 10_000_000:
        raise ValueError("recovery journal exceeds safety limit")
    with path.open("rb") as handle:
        raw = handle.read(10_000_001)
    if len(raw) > 10_000_000:
        raise ValueError("recovery journal exceeds safety limit")
    data = json.loads(raw)
    if (
        not isinstance(data, dict)
        or data.get("schema") != 1
        or data.get("state") not in {"prepared", "committed", "rolled_back"}
    ):
        raise ValueError("unsupported recovery journal")
    if not isinstance(data.get("entries"), list) or not data["entries"]:
        raise ValueError("empty or invalid recovery journal")
    return data


def finish_commit(folder: Path, transaction: Path) -> None:
    data = load_journal(transaction)
    parents = {folder}
    for entry in data["entries"]:
        path = checked_path(folder, entry["path"])
        if digest(path) != entry["after"]:
            raise ValueError(f"published output differs from journal: {entry['path']}")
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
        parent = path.parent
        while parent != folder:
            parents.add(parent)
            parent = parent.parent
    for parent in parents:
        fsync_directory(parent)
    data["state"] = "committed"
    write_journal(transaction, data)


def reset_for_rollback(transaction: Path) -> None:
    """Re-arm recovery before undoing a published committed-state marker."""
    data = load_journal(transaction)
    if data["state"] == "committed":
        data["state"] = "prepared"
        write_journal(transaction, data)


def unfinished_transactions(folder: Path) -> list[Path]:
    roots = [folder]
    if folder.is_dir():
        roots.extend(
            path
            for path in folder.iterdir()
            if DISC_DIR_RE.match(path.name) and path.is_dir() and not path.is_symlink()
        )
    return sorted(path for root in roots for path in root.glob(".metadata-transaction-*") if (path / JOURNAL).exists())


def recover_transaction(folder: Path, transaction: Path, *, dry_run: bool = False) -> list[str]:
    """Exclude cooperating file/ledger writers throughout recovery.

    The caller still owns the album-directory lock. In particular, ledger
    appends from private staging do not acquire that directory lock.
    """
    folder = folder.resolve()
    journal = load_journal(transaction)
    paths: list[Path] = []
    for entry in journal["entries"]:
        if not isinstance(entry, dict):
            raise TypeError("invalid journal entry")
        paths.append(checked_path(folder, str(entry.get("path") or "")))
    with ExitStack() as locks:
        for path in sorted(set(paths)):
            locks.enter_context(file_lock(path))
        return _recover_transaction(folder, transaction, dry_run=dry_run)


def _recover_transaction(folder: Path, transaction: Path, *, dry_run: bool = False) -> list[str]:
    """Restore a prepared commit or clean a durably completed commit.

    The caller must hold the album lock. Validate ALL entries/backups before
    changing any file; keep snapshots intact throughout retryable recovery.
    """
    folder = folder.resolve()
    relative_transaction = str(transaction.relative_to(folder))
    if len(Path(relative_transaction).parts) != 1 or not transaction.name.startswith(".metadata-transaction-"):
        raise ValueError("not an album transaction directory")
    transaction = checked_path(folder, relative_transaction)
    data = load_journal(transaction)
    paths: set[str] = set()
    validated: list[tuple[Path, Path | None, str | None, str | None]] = []
    for entry in data["entries"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or entry["path"] in paths:
            raise ValueError("invalid or duplicate recovery entry")
        paths.add(entry["path"])
        before, after = entry.get("before"), entry.get("after")
        if not isinstance(after, str) or not re_digest(after) or (before is not None and not re_digest(before)):
            raise ValueError("invalid recovery content hashes")
        target = checked_path(folder, entry["path"])
        if Path(entry["path"]).parts[0].startswith(".metadata-transaction-"):
            raise ValueError("recovery target is a transaction directory")
        backup = checked_path(transaction, entry["backup"]) if isinstance(entry.get("backup"), str) else None
        if data["state"] == "prepared" and before is not None and (backup is None or digest(backup) != before):
            raise ValueError(f"missing or damaged recovery snapshot: {entry['path']}")
        current = digest(target)
        allowed = (
            {after} if data["state"] == "committed" else {before} if data["state"] == "rolled_back" else {before, after}
        )
        if current not in allowed:
            raise ValueError(f"unrelated edit prevents recovery: {entry['path']}")
        validated.append((target, backup, before, current))
    actions = [f"{data['state']}: {path.relative_to(folder)!s}" for path, *_rest in validated]
    if dry_run:
        return actions
    if data["state"] == "prepared":
        for target, backup, before, current in validated:
            if current == before:
                continue
            # Recheck immediately before restore; external editors do not obey
            # this advisory lock. The remaining check/replace race is documented.
            if digest(target) != current:
                raise ValueError(f"destination changed during recovery: {target.name}")
            if backup is None:
                target.unlink(missing_ok=True)
            else:
                fd, name = tempfile.mkstemp(prefix=".recovery-", dir=target.parent)
                os.close(fd)
                try:
                    shutil.copy2(backup, name)
                    with open(name, "rb") as handle:
                        os.fsync(handle.fileno())
                    os.replace(name, target)
                finally:
                    Path(name).unlink(missing_ok=True)
            fsync_directory(target.parent)
        # A later cleanup interruption must not require snapshots already
        # deleted by cleanup. Sync restored contents and the terminal state
        # before removing any recovery data; repeated recovery only cleans up.
        for target, _backup, before, _current in validated:
            if digest(target) != before:
                raise ValueError(f"restored output differs from journal: {target.name}")
            if before is not None:
                with target.open("rb") as handle:
                    os.fsync(handle.fileno())
            fsync_directory(target.parent)
        data["state"] = "rolled_back"
        write_journal(transaction, data)
    shutil.rmtree(transaction)
    fsync_directory(folder)
    return actions


def re_digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recover journaled interrupted album commits (no provider calls).")
    parser.add_argument("album", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="validate recovery and list actions without writes")
    args = parser.parse_args(argv)
    descriptor: int | None = None
    try:
        folder = args.album.resolve(strict=True)
        lock_root = writer_album_root(folder / ".recovery-target.flac")
        descriptor = os.open(lock_root, os.O_RDONLY | os.O_DIRECTORY)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        transactions = unfinished_transactions(folder)
        for transaction in transactions:
            for action in recover_transaction(transaction.parent, transaction, dry_run=args.dry_run):
                print(f"{transaction.name}: {action}")
        if not transactions:
            print("No journaled transactions found. Legacy recovery trees require manual inspection.")
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"ERROR: recovery refused; snapshots retained: {exc}")
        return 1
    finally:
        if descriptor is not None:
            os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
