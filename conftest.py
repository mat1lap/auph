"""Keep provider fixtures independent of persistent user caches and prior runs."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_lyrics_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import lyrics_utils
    from provider_cache import JsonCache

    monkeypatch.setattr(lyrics_utils, "_PROVIDER_RESULT_CACHE", JsonCache(tmp_path / "provider-results"))
    monkeypatch.setattr(lyrics_utils, "_LRCLIB_CACHE", JsonCache(tmp_path / "lrclib"))
