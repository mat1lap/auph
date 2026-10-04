"""Interactive cover choice uses category representatives and prepared playback art."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from typing import cast

import pytest
import requests
from mutagen.flac import FLAC, Picture
from PIL import Image

import audiophile_cover_fetcher as covers
import audiophile_tagger as tagger


def _image(color: tuple[int, int, int], size: tuple[int, int] = (120, 120)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def _candidate(provider: str, color: tuple[int, int, int], quality: float) -> covers.CoverCandidate:
    candidate = covers.CoverCandidate(provider, f"fixture://{provider}/{quality}", "Album", 1.0, raw_data=_image(color))
    candidate.width = candidate.height = 120
    candidate.effective_width = candidate.effective_height = 120
    candidate.visual_quality_score = quality
    candidate.total_score = quality * 100
    candidate.role_tier = covers.RoleTier.EXPLICIT_FRONT
    return candidate


def test_cover_fetch_respects_long_retry_after_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = covers.CoverFetcher("Artist", "Album", cache_dir=tmp_path / "cache")

    class RateLimitedResponse:
        status_code = 429

        def __init__(self) -> None:
            self.headers = {"Retry-After": "3600"}

        def close(self) -> None:
            return None

    calls = 0

    def rate_limited(*_args: object, **_kwargs: object) -> RateLimitedResponse:
        nonlocal calls
        calls += 1
        return RateLimitedResponse()

    monkeypatch.setattr(fetcher.session, "get", rate_limited)
    monkeypatch.setattr(covers.time, "sleep", lambda _seconds: pytest.fail("long Retry-After sleep"))
    url = "https://example.invalid/front.jpg"
    assert fetcher.fetch_url(url) is None
    assert fetcher.last_fetch_errors[url] == "rate_limited"
    assert calls == 1


def test_categories_are_independent_and_local_duplicate_survives(tmp_path: Path) -> None:
    fetcher = covers.CoverFetcher("Artist", "Album", cache_dir=tmp_path / "cache")
    digital_low = _candidate("Apple Music (iTunes)", (180, 0, 0), 70)
    digital_high = _candidate("Deezer CDN", (200, 0, 0), 90)
    physical = _candidate("Cover Art Archive (Release)", (0, 180, 0), 88)
    physical.provenance = covers.ArtworkProvenance.PHYSICAL_SCAN
    local = _candidate("Local Existing File", (0, 0, 180), 80)
    local.source_sha256 = digital_high.source_sha256 = "same-image"
    choices = covers.select_cover_categories(fetcher, [digital_low, digital_high, physical, local])
    assert [option.category for option in choices] == ["digital", "physical", "local"]
    assert [option.candidate for option in choices] == [digital_high, physical, local]


def test_default_category_discovery_evaluates_lower_release_tiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = covers.CoverFetcher(
        "Artist", "Album", cache_dir=tmp_path / "cache", exhaustive_categories=True, write_preview_files=False
    )
    digital = _candidate("Apple Music (iTunes)", (190, 0, 0), 90)
    digital.release_tier = covers.ReleaseTier.EXACT_RELEASE
    physical = _candidate("Cover Art Archive (Release)", (0, 190, 0), 88)
    physical.release_tier = covers.ReleaseTier.RELEASE_GROUP
    physical.provenance = covers.ArtworkProvenance.PHYSICAL_SCAN
    for name in (
        "query_itunes",
        "query_cover_art_archive",
        "query_deezer",
        "query_theaudiodb",
        "query_discogs",
        "query_local",
        "query_amazon",
        "query_fanart_tv",
        "query_bandcamp",
        "query_tidal",
        "query_qobuz",
    ):
        monkeypatch.setattr(fetcher, name, list)
    monkeypatch.setattr(fetcher, "query_itunes", lambda: [digital])
    monkeypatch.setattr(fetcher, "query_cover_art_archive", lambda: [physical])
    monkeypatch.setattr(fetcher, "evaluate_candidate", lambda candidate: candidate)
    _winner, _digital, _scan, evaluated = fetcher.find_best_cover()
    assert physical in evaluated
    assert [item.category for item in covers.select_cover_categories(fetcher, evaluated)] == ["digital", "physical"]


@pytest.mark.parametrize(
    "provider,category",
    [
        ("Apple Music (iTunes)", "digital"),
        ("Local Optical Scan", "local"),
        ("Local Existing File", "local"),
        ("Mystery Provider", "unknown"),
    ],
)
def test_single_category_keeps_its_identity(tmp_path: Path, provider: str, category: str) -> None:
    fetcher = covers.CoverFetcher("Artist", "Album", cache_dir=tmp_path / "cache")
    candidate = _candidate(provider, (60, 70, 80), 75)
    assert [option.category for option in covers.select_cover_categories(fetcher, [candidate])] == [category]


def test_report_reconstruction_retains_local_availability_category(tmp_path: Path) -> None:
    reconstructed = covers._parse_cand_from_dict(
        {
            "provider": "Cover Art Archive (Release)",
            "provenance": "physical_scan",
            "identity_scope": "local",
            "title": "Album",
            "url": "file:///archive/front.jpg",
            "resolution": [600, 600],
        },
        tmp_path,
        "Unknown",
        "missing.jpg",
    )
    assert reconstructed is not None
    assert covers.cover_category(reconstructed) == "local"
    assert reconstructed.provenance == covers.ArtworkProvenance.PHYSICAL_SCAN


@pytest.mark.parametrize(
    "present,expected",
    [
        (("digital", "physical"), ("digital", "physical")),
        (("physical", "local"), ("physical", "local")),
        (("digital", "local"), ("digital", "local")),
        ((), ()),
    ],
)
def test_missing_categories_are_omitted(tmp_path: Path, present: tuple[str, ...], expected: tuple[str, ...]) -> None:
    fetcher = covers.CoverFetcher("Artist", "Album", cache_dir=tmp_path / "cache")
    by_category = {
        "digital": _candidate("Apple Music (iTunes)", (200, 0, 0), 80),
        "physical": _candidate("Cover Art Archive (Release)", (0, 200, 0), 80),
        "local": _candidate("Local Existing File", (0, 0, 200), 80),
    }
    by_category["physical"].provenance = covers.ArtworkProvenance.PHYSICAL_SCAN
    choices = covers.select_cover_categories(fetcher, [by_category[name] for name in present])
    assert tuple(choice.category for choice in choices) == expected


def test_prepared_option_and_comparison_show_the_actual_crop(tmp_path: Path) -> None:
    candidate = _candidate("Local Existing File", (220, 0, 0), 80)
    output = io.BytesIO()
    image = Image.new("RGB", (200, 120), (0, 0, 0))
    image.paste((220, 0, 0), (80, 0, 200, 120))
    image.save(output, format="PNG")
    candidate.raw_data = output.getvalue()
    candidate.width, candidate.height = image.size
    candidate.effective_width = candidate.effective_height = 120
    candidate.crop_box = (80, 0, 200, 120)
    candidate.crop_confidence = 0.91
    choice = covers.CoverChoice("local", candidate)
    covers.prepare_cover_choices([choice], tmp_path)
    assert choice.prepared_path is not None
    with Image.open(choice.prepared_path) as prepared:
        assert prepared.size == (120, 120)
        assert cast(tuple[int, int, int], prepared.getpixel((0, 60)))[0] > 150
    comparison = tmp_path / "comparison.jpg"
    covers.create_comparison_image(None, None, candidate, comparison, target_dim=200, options=[choice])
    with Image.open(comparison) as preview:
        assert cast(tuple[int, int, int], preview.getpixel((100, 150)))[0] > 100
    assert candidate.raw_data == output.getvalue()


def test_uncropped_rectangular_preview_preserves_aspect_ratio(tmp_path: Path) -> None:
    candidate = _candidate("Mystery Provider", (210, 40, 40), 75)
    candidate.raw_data = _image((210, 40, 40), (200, 100))
    candidate.width, candidate.height = 200, 100
    candidate.effective_width, candidate.effective_height = 200, 100
    option = covers.CoverChoice("unknown", candidate)
    covers.prepare_cover_choices([option], tmp_path)
    comparison = tmp_path / "comparison.jpg"
    covers.create_comparison_image(None, None, candidate, comparison, target_dim=200, options=[option])
    with Image.open(comparison) as preview:
        assert cast(tuple[int, int, int], preview.getpixel((100, 95)))[0] < 100
        assert cast(tuple[int, int, int], preview.getpixel((100, 180)))[0] > 150
        assert cast(tuple[int, int, int], preview.getpixel((100, 270)))[0] < 100


def test_verified_caa_package_scan_category_survives_analysis_cache(tmp_path: Path) -> None:
    raw = (
        Path(__file__).resolve().parent.parent / "Queen" / "Made In Heaven (1995-10-24)" / "cover.original.png"
    ).read_bytes()
    for _repeat in range(2):
        fetcher = covers.CoverFetcher("Queen", "Made In Heaven", cache_dir=tmp_path / "analysis")
        candidate = fetcher.evaluate_candidate(
            covers.CoverCandidate(
                "Cover Art Archive (Release)",
                "fixture://caa",
                "Made In Heaven",
                1.0,
                raw_data=raw,
                front_flag=True,
                release_tier=covers.ReleaseTier.EXACT_RELEASE,
            )
        )
        assert candidate.crop_box is not None
        assert candidate.provenance == covers.ArtworkProvenance.PHYSICAL_SCAN
        assert [item.category for item in covers.select_cover_categories(fetcher, [candidate])] == ["physical"]


def test_reprocessing_selected_front_does_not_crop_or_reencode_again(tmp_path: Path) -> None:
    raw = (
        Path(__file__).resolve().parent.parent / "Queen" / "Made In Heaven (1995-10-24)" / "cover.original.png"
    ).read_bytes()
    fetcher = covers.CoverFetcher("Queen", "Made In Heaven", cache_dir=tmp_path / "analysis")
    source = fetcher.evaluate_candidate(
        covers.CoverCandidate(
            "Local Existing File",
            "fixture://source",
            "Made In Heaven",
            1.0,
            raw_data=raw,
            identity_scope="local",
        )
    )
    assert source.crop_box is not None
    first = tmp_path / "first.jpg"
    covers.save_optimized_cover(raw, first, crop_box=source.crop_box)
    derivative = fetcher.evaluate_candidate(
        covers.CoverCandidate(
            "Local Existing File",
            str(first),
            "Made In Heaven",
            1.0,
            raw_data=first.read_bytes(),
            identity_scope="local",
        )
    )
    assert derivative.crop_box is None
    second = tmp_path / "second.jpg"
    covers.save_optimized_cover(derivative.raw_data or b"", second, crop_box=derivative.crop_box)
    assert second.read_bytes() == first.read_bytes()


@pytest.mark.parametrize(
    "album,source_name",
    [
        ("Made In Heaven (1995-10-24)", "cover.original.png"),
        ("Innuendo (1991-01-14)", "cover.original.jpg"),
    ],
)
def test_queen_package_and_notch_are_removed_in_prepared_menu_image(
    tmp_path: Path, album: str, source_name: str
) -> None:
    raw = (Path(__file__).resolve().parent.parent / "Queen" / album / source_name).read_bytes()
    fetcher = covers.CoverFetcher("Queen", album, cache_dir=tmp_path / "cache")
    candidate = fetcher.evaluate_candidate(
        covers.CoverCandidate(
            "Local Existing File",
            "fixture://queen",
            album,
            1.0,
            raw_data=raw,
            identity_scope="local",
        )
    )
    assert candidate.crop_box is not None
    option = covers.CoverChoice("local", candidate)
    covers.prepare_cover_choices([option], tmp_path / "prepared")
    assert option.applied_crop and option.prepared_path is not None
    with Image.open(option.prepared_path) as shown:
        assert shown.size == (option.prepared_width, option.prepared_height)
        assert shown.width == shown.height
        assert shown.size != (candidate.width, candidate.height)


def test_single_option_requires_confirmation_and_can_be_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    choice = covers.CoverChoice("local", _candidate("Local Existing File", (50, 60, 70), 80))
    covers.prepare_cover_choices([choice], tmp_path)
    answers = iter(["", "r"])
    monkeypatch.setattr("builtins.input", lambda *_args: next(answers))
    assert covers.prompt_cover_options([choice]) is None
    monkeypatch.setattr("builtins.input", lambda *_args: "1")
    assert covers.prompt_cover_options([choice]) is choice


def test_default_cli_interactive_and_auto_flag_do_not_mix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    albums = [tmp_path / "Album A", tmp_path / "Album B"]
    for album in albums:
        album.mkdir()
    monkeypatch.setenv("DISCOGS_TOKEN", "fixture")
    monkeypatch.setattr(
        tagger, "resolve_target_albums", lambda *_args, **_kwargs: [f"1:::{album}:::CD" for album in albums]
    )
    monkeypatch.setattr(tagger.sys.stdin, "isatty", lambda: True)
    calls: list[bool] = []

    def fake_album(*_args: object, **kwargs: object) -> bool:
        calls.append(bool(kwargs["interactive_covers"]))
        return True

    monkeypatch.setattr(tagger, "process_album", fake_album)
    assert tagger.main([]) == 0
    assert tagger.main(["--auto-covers"]) == 0
    assert calls == [True, True, False, False]


def test_default_cli_without_tty_fails_before_album_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    album = tmp_path / "Album"
    album.mkdir()
    monkeypatch.setenv("DISCOGS_TOKEN", "fixture")
    monkeypatch.setattr(tagger, "resolve_target_albums", lambda *_args, **_kwargs: [f"1:::{album}:::CD"])
    monkeypatch.setattr(tagger.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(tagger, "process_album", lambda *_args, **_kwargs: pytest.fail("album mutated"))
    assert tagger.main([]) != 0
    monkeypatch.setattr(tagger, "process_album", lambda *_args, **_kwargs: True)
    assert tagger.main(["--auto-covers"]) == 0


def test_standalone_cover_entrypoint_uses_category_choice_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digital = _candidate("Apple Music (iTunes)", (210, 0, 0), 80)
    local = _candidate("Local Existing File", (0, 0, 210), 75)
    monkeypatch.setattr(
        covers.CoverFetcher, "find_best_cover", lambda *_args, **_kwargs: (digital, digital, None, [digital, local])
    )
    monkeypatch.setattr(covers, "emit_kitty_image", lambda *_args: False)
    monkeypatch.setattr(covers.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_args: "2")
    monkeypatch.setattr(
        covers.sys,
        "argv",
        ["audiophile_cover_fetcher.py", "--artist", "Artist", "--album", "Album", "--folder", str(tmp_path)],
    )
    assert covers.main() == 0
    with Image.open(tmp_path / "cover.jpg") as chosen:
        assert cast(tuple[int, int, int], chosen.convert("RGB").getpixel((60, 60)))[2] > 170


@pytest.mark.parametrize(
    "answer,expected_color",
    [
        ("1", "digital"),
        ("2", "physical"),
        ("3", "local"),
        ("3", "local_crop"),
        ("1", "single"),
        ("r", "rejected"),
        (None, "none"),
    ],
)
def test_interactive_album_choice_reaches_saved_and_embedded_cover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str | None, expected_color: str
) -> None:
    folder = tmp_path / "Fixture Artist" / "Fixture Album"
    folder.mkdir(parents=True)
    track = folder / "01.flac"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.5",
            "-c:a",
            "flac",
            str(track),
        ],
        check=True,
    )
    audio = FLAC(track)
    for key, value in {
        "ALBUM": "Fixture Album",
        "ALBUMARTIST": "Fixture Artist",
        "ARTIST": "Fixture Artist",
        "TITLE": "First",
        "TRACKNUMBER": "1",
        "TOTALTRACKS": "1",
        "DISCNUMBER": "1",
        "TOTALDISCS": "1",
    }.items():
        audio[key] = [value]
    audio.save()
    local = io.BytesIO()
    Image.new("RGB", (600, 600), (0, 0, 210)).save(local, format="JPEG", quality=95)
    original = Path(__file__).resolve().parent.parent / "Queen" / "Made In Heaven (1995-10-24)" / "cover.original.png"
    remote_scan = original.read_bytes()
    initial_local = remote_scan if expected_color == "local_crop" else local.getvalue()
    (folder / "cover.jpg").write_bytes(initial_local)
    if expected_color not in {"rejected", "none"}:
        incumbent_bytes = io.BytesIO()
        with Image.open(io.BytesIO(remote_scan)) as source_image:
            clean_front = source_image.crop((387, 77, 3191, 2881))
            clean_front.thumbnail((1400, 1400))
            clean_front.convert("RGB").save(incumbent_bytes, format="JPEG", quality=94)
        incumbent = Picture()
        incumbent.type = 3
        incumbent.mime = "image/jpeg"
        incumbent.width = 1400
        incumbent.height = 1400
        incumbent.depth = 24
        incumbent.data = incumbent_bytes.getvalue()
        tagged = FLAC(track)
        tagged.add_picture(incumbent)
        tagged.save()
    discogs = {
        "id": 1,
        "title": "Fixture Album",
        "artists": [{"name": "Fixture Artist", "id": 1}],
        "released": "2020-01-01",
        "country": "US",
        "formats": [{"name": "CD"}],
        "tracklist": [{"position": "1", "title": "First", "duration": "0:01", "type_": "track"}],
    }
    monkeypatch.setattr(tagger, "discogs_get", lambda *_args, **_kwargs: discogs)
    monkeypatch.setattr(tagger, "mb_get", lambda *_args, **_kwargs: (404, {}))
    monkeypatch.setattr(tagger, "query_acoustid_candidates", lambda *_args, **_kwargs: (set(), set()))
    monkeypatch.setattr(tagger.lyrics_utils, "fetch_betterlyrics", lambda **_kwargs: None)
    monkeypatch.setattr(covers, "emit_kitty_image", lambda *_args: False)
    monkeypatch.setattr("builtins.input", lambda *_args: answer)

    def fixture_covers(fetcher: covers.CoverFetcher, **_kwargs: object) -> tuple[object, object, object, object]:
        if expected_color == "none":
            return None, None, None, []
        digital = fetcher.evaluate_candidate(
            covers.CoverCandidate(
                "Apple Music (iTunes)",
                "fixture://digital",
                "Fixture Album",
                1.0,
                raw_data=_image((210, 0, 0), (600, 600)),
                front_flag=True,
                release_tier=covers.ReleaseTier.EXACT_RELEASE,
            )
        )
        physical = fetcher.evaluate_candidate(
            covers.CoverCandidate(
                "Cover Art Archive (Release)",
                "fixture://physical",
                "Fixture Album",
                1.0,
                raw_data=remote_scan,
                front_flag=True,
                release_tier=covers.ReleaseTier.EXACT_RELEASE,
            )
        )
        existing = fetcher.evaluate_candidate(
            covers.CoverCandidate(
                "Local Existing File",
                str(folder / "cover.jpg"),
                "Fixture Album",
                1.0,
                raw_data=initial_local,
                identity_scope="local",
                front_flag=True,
            )
        )
        assert physical.crop_box is not None
        assert physical.provenance == covers.ArtworkProvenance.PHYSICAL_SCAN
        if expected_color == "single":
            return digital, digital, None, [digital]
        return physical, digital, physical, [digital, physical, existing]

    monkeypatch.setattr(covers.CoverFetcher, "find_best_cover", fixture_covers)
    entry = f"1:::{folder}:::2020 CD"
    if expected_color == "physical":
        # Exercise the ordinary entry point with no flags and real cover/FLAC writing.
        monkeypatch.setenv("DISCOGS_TOKEN", "fixture-token")
        monkeypatch.setattr(tagger.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(tagger, "resolve_target_albums", lambda *_args, **_kwargs: [entry])
        assert tagger.main([]) == 0
    else:
        assert tagger.process_album(
            entry,
            "fixture-token",
            "fixture@example.org",
            quiet=True,
            no_loudgain=True,
            no_dr14=True,
            threads=1,
            session=requests.Session(),
            discogs_id_override="1",
            interactive_covers=True,
            embed_max_dim=None,
        )
    with Image.open(folder / "cover.jpg") as saved:
        saved_rgb = saved.convert("RGB")
        if expected_color in {"digital", "single"}:
            assert cast(tuple[int, int, int], saved_rgb.getpixel((saved.width // 2, saved.height // 2)))[0] > 170
        elif expected_color in {"local", "rejected", "none"}:
            assert cast(tuple[int, int, int], saved_rgb.getpixel((saved.width // 2, saved.height // 2)))[2] > 170
            assert (folder / "cover.jpg").read_bytes() == local.getvalue()
        elif expected_color == "local_crop":
            assert saved.width == saved.height < 3356
            assert (folder / "cover.original.png").read_bytes() == remote_scan
        else:
            assert saved.width == saved.height
            assert saved.width < 3356
            assert (folder / "cover.original.png").read_bytes() == remote_scan
    fronts = [picture for picture in FLAC(track).pictures if picture.type == 3]
    if expected_color not in {"rejected", "none"}:
        assert len(fronts) == 1
    front = fronts[0]
    with Image.open(io.BytesIO(front.data)) as embedded:
        expected_size = saved_rgb.size
        if expected_color == "physical":
            assert max(embedded.size) <= 1400  # The ordinary CLI embedding limit.
        else:
            assert embedded.size == expected_size
        center = cast(
            tuple[int, int, int], embedded.convert("RGB").getpixel((embedded.width // 2, embedded.height // 2))
        )
        saved_center = cast(tuple[int, int, int], saved_rgb.getpixel((saved_rgb.width // 2, saved_rgb.height // 2)))
        assert all(abs(a - b) <= 8 for a, b in zip(center, saved_center, strict=True))
