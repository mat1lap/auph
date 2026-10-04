"""Advisory album locking without persistent lock files (POSIX)."""

from __future__ import annotations

import fcntl
import os
import sys
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import ParamSpec

P = ParamSpec("P")


def exclusive_album(function: Callable[P, bool]) -> Callable[P, bool]:
    """Refuse a second pipeline run while an album directory is locked.

    The kernel releases the lock on interruption/process exit. This prevents
    cooperating pipeline processes from interleaving backups and commits; it
    cannot lock out unrelated editors or make multiple renames atomic.
    """

    @wraps(function)
    def locked(*args: P.args, **kwargs: P.kwargs) -> bool:
        entry = str(kwargs.get("entry") or (args[0] if args else ""))
        parts = entry.split(":::")
        if len(parts) < 2 or not Path(parts[1]).is_dir():
            return function(*args, **kwargs)
        descriptor = os.open(Path(parts[1]).resolve(), os.O_RDONLY | os.O_DIRECTORY)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(f"ERROR: album is already being processed: {parts[1]}", file=sys.stderr)
                return False
            return function(*args, **kwargs)
        finally:
            os.close(descriptor)

    return locked
