"""Safe library traversal and transaction-residue detection.

The enrichment pipeline must never regard its own disposable copies as music.
Keeping this rule in one module prevents a future caller from reintroducing a
recursive ``rglob('*.flac')`` footgun.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

_EXCLUDED_PREFIXES = (
    ".metadata-transaction-",
    ".provider-cache",
    ".api_cache",
)
_EXCLUDED_SUFFIXES = (".pending.flac", ".rollback.flac", ".replaygain.flac", ".backup.flac")


def is_pipeline_private(path: Path, *, root: Path | None = None) -> bool:
    """Return whether *path* is a pipeline work product rather than library data."""
    try:
        parts: Iterable[str] = path.relative_to(root).parts if root else path.parts
    except ValueError:
        parts = path.parts
    if any(part.startswith(_EXCLUDED_PREFIXES) for part in parts):
        return True
    name = path.name.casefold()
    return name.startswith(".") and name.endswith(_EXCLUDED_SUFFIXES)


def iter_library_flacs(root: Path) -> list[Path]:
    """Return deterministic real-library FLAC paths, excluding pipeline residue."""
    root = Path(root).resolve()
    return sorted(
        (
            path
            for path in root.rglob("*")
            if path.suffix.casefold() == ".flac" and path.is_file() and not is_pipeline_private(path, root=root)
        ),
        key=lambda path: (path.parent.as_posix().casefold(), path.name.casefold()),
    )


def abandoned_transactions(root: Path) -> list[Path]:
    """Return transaction directories for review or explicit journal recovery."""
    root = Path(root).resolve()
    return sorted(path for path in root.rglob(".metadata-transaction-*") if path.is_dir())
