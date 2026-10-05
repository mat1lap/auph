"""POSIX locks for standalone writer files and provenance across processes.

Locks live in a private per-user temporary directory, never in an album.
Keep lock inodes: deleting them could split waiters between different locks.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import ParamSpec, TypeVar

from library_files import is_pipeline_private
from library_layout import DISC_DIR_RE

P = ParamSpec("P")
T = TypeVar("T")
_HELD_LOCKS = threading.local()


def lock_directory() -> Path:
    directory = Path(tempfile.gettempdir()) / f"auph-locks-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("unsafe writer lock directory")
    return directory


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    directory = lock_directory()
    key = hashlib.sha256(os.fsencode(str(path.resolve()))).hexdigest()
    held = getattr(_HELD_LOCKS, "keys", None)
    if held is None:
        held = set()
        _HELD_LOCKS.keys = held
    if key in held:
        yield
        return
    descriptor = os.open(directory / key, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        held.add(key)
        yield
    finally:
        held.discard(key)
        os.close(descriptor)


def writer_album_root(path: Path) -> Path:
    """Use the same normal disc-directory grouping for locks and recovery gates."""
    return path.parent.parent if DISC_DIR_RE.match(path.parent.name) else path.parent


def serialize_file_writer(function: Callable[P, T]) -> Callable[P, T]:
    """Lock before reading the destination snapshot, not only during rename."""

    @wraps(function)
    def locked(*args: P.args, **kwargs: P.kwargs) -> T:
        path = kwargs.get("flac_path") or (args[0] if args else None)
        if not isinstance(path, (str, Path)):
            raise TypeError("FLAC path is required")
        path = Path(path).absolute()
        if is_pipeline_private(path):
            with file_lock(path):
                return function(*args, **kwargs)
        parent = writer_album_root(path)
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            with file_lock(path):
                return function(*args, **kwargs)
        finally:
            os.close(descriptor)

    return locked
