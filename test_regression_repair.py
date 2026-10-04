"""Regression cases from the observed Queen artwork and album loudness failures."""

from __future__ import annotations

import hashlib
import io
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import requests
from mutagen.flac import FLAC
from PIL import Image, ImageDraw

import audiophile_cover_fetcher as covers
import audiophile_tagger as tagger
import metadata_writer

QUEEN = Path(__file__).resolve().parent.parent / "Queen"


def _small_jpeg() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (96, 96), "#667799").save(output, format="JPEG")
    return output.getvalue()


def _queen_candidate(tmp_path: Path, album: str, filename: str) -> covers.CoverCandidate:
    source = QUEEN / album / filename
    fetcher = covers.CoverFetcher("Queen", album, cache_dir=tmp_path / "cache", write_preview_files=False)
    return fetcher.evaluate_candidate(
        covers.CoverCandidate(
            "Local Existing File",
            str(source),
            album,
            1.0,
            raw_data=source.read_bytes(),
            identity_scope="local",
        )
    )


@pytest.mark.parametrize(
    ("provider", "expected_label"),
    [
        ("Local Existing File", "Local Artwork"),
        ("Cover Art Archive (Release)", "Cover Art Archive"),
        ("Mystery Provider", "Source Unknown"),
    ],
)
def test_single_cover_preview_labels_provenance_not_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, expected_label: str
) -> None:
    labels: list[str] = []

    def record_text(self: ImageDraw.ImageDraw, xy: object, text: str, *args: object, **kwargs: object) -> None:
        labels.append(str(text))

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record_text)
    candidate = covers.CoverCandidate(provider, "local://cover", "Album", 1.0, raw_data=_small_jpeg())
    # This is the single-candidate invocation used by the tagger today.
    covers.create_comparison_image(candidate, None, candidate, tmp_path / "comparison.jpg", target_dim=160)
    assert any(expected_label in label for label in labels)
    assert not any("ЦИФРОВОЙ МАСТЕР" in label for label in labels)


def test_made_in_heaven_selected_pixels_use_scored_front_crop(tmp_path: Path) -> None:
    candidate = _queen_candidate(tmp_path, "Made In Heaven (1995-10-24)", "cover.original.png")
    assert candidate.crop_box is not None
    assert candidate.effective_width == candidate.effective_height
    output = tmp_path / "cover.jpg"
    covers.save_optimized_cover(candidate.raw_data or b"", output, crop_box=candidate.crop_box)
    with Image.open(output) as image:
        assert image.size == (candidate.effective_width, candidate.effective_height)


def test_innuendo_real_local_cover_detects_removable_finger_notch(tmp_path: Path) -> None:
    candidate = _queen_candidate(tmp_path, "Innuendo (1991-01-14)", "cover.original.jpg")
    assert candidate.crop_box is not None
    assert candidate.crop_confidence >= 0.80
    assert candidate.effective_width == candidate.effective_height


def _flac(path: Path, frequency: int, track: int, total: int) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:duration=0.5",
            "-c:a",
            "flac",
            str(path),
        ],
        check=True,
    )
    audio = FLAC(path)
    audio["ALBUM"] = ["Test Album"]
    audio["ALBUMARTIST"] = ["Test Artist"]
    audio["DISCNUMBER"] = ["1"]
    audio["TOTALDISCS"] = ["1"]
    audio["TRACKNUMBER"] = [str(track)]
    audio["TOTALTRACKS"] = [str(total)]
    audio.save()


def test_complete_local_album_does_not_require_musicbrainz_release(tmp_path: Path) -> None:
    first = tmp_path / "01 - One.flac"
    second = tmp_path / "02 - Two.flac"
    _flac(first, 440, 1, 2)
    _flac(second, 660, 2, 2)
    decision = tagger.assess_album_completeness([first, second], tmp_path)
    assert decision.state == "COMPLETE"
    assert "contiguous" in " ".join(decision.evidence).lower()


def test_exact_release_extra_disc_prevents_album_completeness(tmp_path: Path) -> None:
    track = tmp_path / "01 - One.flac"
    _flac(track, 440, 1, 1)
    decision = tagger.assess_album_completeness([track], tmp_path, external_disc_counts={1: 1, 2: 1})
    assert decision.state == "INCOMPLETE"
    assert "disc" in " ".join(decision.evidence).lower()


def test_unrelated_eac_log_does_not_certify_exact_disc_id(tmp_path: Path) -> None:
    import mb_discid_from_cue

    first = tmp_path / "01 - One.flac"
    second = tmp_path / "02 - Two.flac"
    _flac(first, 440, 1, 2)
    _flac(second, 660, 2, 2)
    cue = tmp_path / "album.cue"
    cue.write_text(
        'FILE "01 - One.flac" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n'
        'FILE "02 - Two.flac" WAVE\n  TRACK 02 AUDIO\n    INDEX 01 00:00:00\n',
        encoding="utf-8",
    )
    (tmp_path / "unrelated.log").write_text(
        " 1 | 0:00.00 | 10:00.00 | 0 | 44999\n 2 | 10:00.00 | 10:00.00 | 45000 | 89999\n",
        encoding="utf-8",
    )
    toc = mb_discid_from_cue.build(cue)
    assert toc["toc_exact"] is False
    assert toc["source"] == "CUE+FLAC-TRACKS"
    assert toc["eac_log"] is None

    (tmp_path / "z_matching.log").write_text(
        " 1 | 0:00.00 | 0:00.38 | 0 | 37\n 2 | 0:00.38 | 0:00.38 | 38 | 75\n",
        encoding="utf-8",
    )
    verified_toc = mb_discid_from_cue.build(cue)
    assert verified_toc["toc_exact"] is True
    assert verified_toc["source"] == "EAC+CUE"
    assert verified_toc["eac_log"] == str(tmp_path / "z_matching.log")


def test_completeness_accepts_legacy_encoded_cue(tmp_path: Path) -> None:
    first = tmp_path / "01.flac"
    second = tmp_path / "02.flac"
    _flac(first, 440, 1, 2)
    _flac(second, 660, 2, 2)
    (tmp_path / "album.cue").write_bytes(
        b'REM COMMENT "caf\xe9"\r\nFILE "01.flac" WAVE\r\nTRACK 01 AUDIO\r\nFILE "02.flac" WAVE\r\nTRACK 02 AUDIO\r\n'
    )
    assert tagger.assess_album_completeness([first, second], tmp_path).state == "COMPLETE"


def test_unknown_album_runs_track_only_loudness(tmp_path: Path) -> None:
    track = tmp_path / "01 - One.flac"
    _flac(track, 440, 1, 2)
    assert tagger.run_loudgain([track], quiet=True, complete_album=False)
    tags = FLAC(track).tags
    assert tags is not None
    assert tags.get("REPLAYGAIN_TRACK_GAIN")
    assert tags.get("REPLAYGAIN_TRACK_PEAK")
    assert not tags.get("REPLAYGAIN_ALBUM_GAIN")
    assert not tags.get("REPLAYGAIN_ALBUM_PEAK")


def test_complete_local_album_gets_consistent_real_album_gain(tmp_path: Path) -> None:
    tracks = [tmp_path / "01.flac", tmp_path / "02.flac"]
    for number, path in enumerate(tracks, start=1):
        _flac(path, 350 + number * 100, number, 2)
    assert tagger.assess_album_completeness(tracks, tmp_path).state == "COMPLETE"
    assert tagger.run_loudgain(tracks, quiet=True, complete_album=True)
    album_pairs = {
        (tuple(FLAC(path).get("REPLAYGAIN_ALBUM_GAIN", [])), tuple(FLAC(path).get("REPLAYGAIN_ALBUM_PEAK", [])))
        for path in tracks
    }
    assert len(album_pairs) == 1
    assert all(FLAC(path).get("REPLAYGAIN_TRACK_GAIN") for path in tracks)
    assert not tagger.run_loudgain(tracks, quiet=True, complete_album=True)


def test_inconsistent_existing_album_gain_is_recalculated(tmp_path: Path) -> None:
    tracks = [tmp_path / "01.flac", tmp_path / "02.flac"]
    for number, path in enumerate(tracks, 1):
        _flac(path, 450 + number * 100, number, 2)
        audio = FLAC(path)
        audio["REPLAYGAIN_TRACK_GAIN"] = ["-4.00 dB"]
        audio["REPLAYGAIN_TRACK_PEAK"] = ["0.70"]
        audio["REPLAYGAIN_ALBUM_GAIN"] = [f"-{number}.00 dB"]
        audio["REPLAYGAIN_ALBUM_PEAK"] = ["0.80"]
        audio.save()
    assert tagger.run_loudgain(tracks, quiet=True, complete_album=True)
    pairs = {
        (tuple(FLAC(path).get("REPLAYGAIN_ALBUM_GAIN", [])), tuple(FLAC(path).get("REPLAYGAIN_ALBUM_PEAK", [])))
        for path in tracks
    }
    assert len(pairs) == 1


def test_inconsistent_album_gain_is_cleared_if_scanner_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracks = [tmp_path / "01.flac", tmp_path / "02.flac"]
    for number, path in enumerate(tracks, 1):
        _flac(path, 450 + number, number, 2)
        audio = FLAC(path)
        audio["REPLAYGAIN_TRACK_GAIN"] = ["-4.00 dB"]
        audio["REPLAYGAIN_TRACK_PEAK"] = ["0.70"]
        audio["REPLAYGAIN_ALBUM_GAIN"] = [f"-{number}.00 dB"]
        audio["REPLAYGAIN_ALBUM_PEAK"] = ["0.80"]
        audio.save()
    monkeypatch.setattr(tagger.shutil, "which", lambda _binary: None)
    outcome: dict[str, str] = {}
    assert tagger.run_loudgain(tracks, quiet=True, complete_album=True, outcome=outcome)
    assert outcome == {"status": "updated", "tool": "existing-tags", "mode": "track-preserved"}
    for path in tracks:
        audio = FLAC(path)
        assert audio["REPLAYGAIN_TRACK_GAIN"] == ["-4.00 dB"]
        assert "REPLAYGAIN_ALBUM_GAIN" not in audio
        assert "REPLAYGAIN_ALBUM_PEAK" not in audio


def test_partial_album_preserves_valid_track_gain_and_clears_stale_album_gain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track = tmp_path / "01.flac"
    _flac(track, 440, 1, 2)
    audio = FLAC(track)
    audio["REPLAYGAIN_TRACK_GAIN"] = ["-4.21 dB"]
    audio["REPLAYGAIN_TRACK_PEAK"] = ["0.701234"]
    audio["REPLAYGAIN_ALBUM_GAIN"] = ["-5.10 dB"]
    audio["REPLAYGAIN_ALBUM_PEAK"] = ["0.891234"]
    audio.save()

    real_run = subprocess.run

    def no_rescan(cmd: list[str], **kwargs: Any) -> Any:
        if Path(cmd[0]).name == "loudgain":
            raise AssertionError("valid existing track gain must not be rescanned")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(tagger.subprocess, "run", no_rescan)
    assert tagger.run_loudgain([track], quiet=True, complete_album=False)
    final = FLAC(track)
    assert final["REPLAYGAIN_TRACK_GAIN"] == ["-4.21 dB"]
    assert final["REPLAYGAIN_TRACK_PEAK"] == ["0.701234"]
    assert "REPLAYGAIN_ALBUM_GAIN" not in final
    assert "REPLAYGAIN_ALBUM_PEAK" not in final


def test_failed_album_loudness_leaves_every_original_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tracks = [tmp_path / "01.flac", tmp_path / "02.flac"]
    for number, path in enumerate(tracks, start=1):
        _flac(path, 400 + number * 100, number, 2)
    before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in tracks]

    def failed_scan(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, "loudgain", stderr="fixture failure")

    monkeypatch.setattr(tagger.subprocess, "run", failed_scan)
    assert not tagger.run_loudgain(tracks, quiet=True, complete_album=True)
    assert [hashlib.sha256(path.read_bytes()).hexdigest() for path in tracks] == before
    assert not list(tmp_path.glob(".*.replaygain.flac"))


def test_completeness_distinguishes_missing_tracks_and_mixed_releases(tmp_path: Path) -> None:
    first = tmp_path / "01.flac"
    second = tmp_path / "02.flac"
    _flac(first, 440, 1, 3)
    assert tagger.assess_album_completeness([first], tmp_path).state == "INCOMPLETE"
    _flac(second, 550, 2, 3)
    audio = FLAC(second)
    audio["ALBUM"] = ["Another Album"]
    audio.save()
    assert tagger.assess_album_completeness([first, second], tmp_path).state == "INCOMPLETE"


def test_complete_multidisc_album_and_cue_evidence(tmp_path: Path) -> None:
    tracks = [tmp_path / "d1-1.flac", tmp_path / "d1-2.flac", tmp_path / "d2-1.flac"]
    for path, disc, track, total in zip(tracks, (1, 1, 2), (1, 2, 1), (2, 2, 1), strict=True):
        _flac(path, 440 + disc * 100 + track, track, total)
        audio = FLAC(path)
        audio["DISCNUMBER"] = [str(disc)]
        audio["TOTALDISCS"] = ["2"]
        audio.save()
    assert tagger.assess_album_completeness(tracks, tmp_path).state == "COMPLETE"

    # A matching CUE can supply a missing track total without MusicBrainz.
    for path in tracks[:2]:
        audio = FLAC(path)
        del audio["TOTALTRACKS"]
        audio.save()
    (tmp_path / "Disc 1.cue").write_text(
        'FILE "d1-1.flac" WAVE\n  TRACK 01 AUDIO\nFILE "d1-2.flac" WAVE\n  TRACK 02 AUDIO\n',
        encoding="utf-8",
    )
    assert tagger.assess_album_completeness(tracks, tmp_path).state == "COMPLETE"


def test_completeness_unknown_without_totals_or_cue(tmp_path: Path) -> None:
    path = tmp_path / "01.flac"
    _flac(path, 440, 1, 1)
    audio = FLAC(path)
    del audio["TOTALTRACKS"]
    audio.save()
    assert tagger.assess_album_completeness([path], tmp_path).state == "UNKNOWN"


def test_cue_without_complete_file_mapping_cannot_prove_album_completeness(tmp_path: Path) -> None:
    tracks = [tmp_path / "01.flac", tmp_path / "02.flac"]
    for number, path in enumerate(tracks, 1):
        _flac(path, 440 + number, number, 2)
        audio = FLAC(path)
        del audio["TOTALTRACKS"]
        audio.save()
    cue = tmp_path / "album.cue"
    cue.write_text("TRACK 01 AUDIO\nTRACK 02 AUDIO\n", encoding="utf-8")
    assert tagger.assess_album_completeness(tracks, tmp_path).state == "UNKNOWN"
    cue.write_text('FILE "01.flac" WAVE\nTRACK 01 AUDIO\nTRACK 02 AUDIO\n', encoding="utf-8")
    assert tagger.assess_album_completeness(tracks, tmp_path).state == "UNKNOWN"


def test_unrelated_dr_report_rows_do_not_attach_by_track_number_or_order(tmp_path: Path) -> None:
    targets = [tmp_path / "01 - First.flac", tmp_path / "02 - Second.flac"]
    report_rows = [
        {"name": "01 - Unrelated", "dr": 6, "peak": "-1.0", "rms": "-10.0"},
        {"name": "02 - Different", "dr": 7, "peak": "-2.0", "rms": "-11.0"},
    ]
    assert tagger.dr14_parse.resolve_to_files(report_rows, targets) == []


def test_dr_report_is_staged_and_partial_measurement_has_no_album_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracks = [tmp_path / "01.flac", tmp_path / "02.flac"]
    for number, path in enumerate(tracks, 1):
        _flac(path, 440 + number, number, 2)

    def measure(path: Path) -> dict[str, object]:
        return {"name": path.name, "type": "track", "dr": 10, "peak": "-1.0", "rms": "-11.0", "duration": "00:01"}

    monkeypatch.setattr(tagger.dr14_parse, "measure_audio_file", measure)
    reports: list[str] = []
    track_map, album_dr, _ = tagger.measure_album_dr14(tmp_path, tracks, report_sink=reports.append)
    assert set(track_map) == {path.name for path in tracks}
    assert album_dr == "10" and len(reports) == 1
    assert not (tmp_path / "dr14.txt").exists()
    transaction_dir, _ = tagger.stage_album_files(tmp_path, tracks, generated_dr_report=reports[0])
    assert (transaction_dir / "pending" / "dr14.txt").is_file()
    assert not (tmp_path / "dr14.txt").exists()

    def partial_measure(path: Path) -> dict[str, object]:
        if path == tracks[1]:
            raise OSError("unreadable second track")
        return measure(path)

    monkeypatch.setattr(tagger.dr14_parse, "measure_audio_file", partial_measure)
    reports.clear()
    _track_map, partial_dr, _ = tagger.measure_album_dr14(tmp_path, tracks, report_sink=reports.append)
    assert partial_dr == "" and reports == []


def test_partial_existing_dr_report_uses_local_measurement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tracks = [tmp_path / "01 - First.flac", tmp_path / "02 - Second.flac"]
    for number, path in enumerate(tracks, 1):
        _flac(path, 440 + number, number, 2)
    report = tagger.dr14_parse.generate_dr14_report(
        "Other Artist",
        "Other Album",
        [
            {"name": tracks[0].name, "dr": 5, "peak": "-1.0", "rms": "-9.0", "duration": "00:01"},
            {"name": "02 - Unrelated.flac", "dr": 6, "peak": "-2.0", "rms": "-10.0", "duration": "00:01"},
        ],
        6,
    )
    (tmp_path / "dr14.txt").write_text(report, encoding="utf-8")
    monkeypatch.setattr(
        tagger.dr14_parse,
        "measure_audio_file",
        lambda path: {
            "name": path.name,
            "type": "track",
            "dr": 11,
            "peak": "-1.0",
            "rms": "-12.0",
            "duration": "00:01",
        },
    )
    track_map, album_dr, _ = tagger.measure_album_dr14(tmp_path, tracks, report_sink=lambda _report: None)
    assert album_dr == "11"
    assert all(values[0] == "11" for values in track_map.values())


def test_dr_report_with_matching_filenames_but_wrong_album_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracks = [tmp_path / "01.flac", tmp_path / "02.flac"]
    for number, path in enumerate(tracks, 1):
        _flac(path, 440 + number, number, 2)
    wrong_report = tagger.dr14_parse.generate_dr14_report(
        "Other Artist",
        "Other Album",
        [{"name": path.name, "dr": 5, "peak": "-1.0", "rms": "-9.0", "duration": "00:01"} for path in tracks],
        5,
    )
    (tmp_path / "dr14.txt").write_text(wrong_report, encoding="utf-8")
    monkeypatch.setattr(
        tagger.dr14_parse,
        "measure_audio_file",
        lambda path: {
            "name": path.name,
            "type": "track",
            "dr": 12,
            "peak": "-1.0",
            "rms": "-12.0",
            "duration": "00:01",
        },
    )
    values, album_dr, _ = tagger.measure_album_dr14(
        tmp_path, tracks, artist="Local Artist", album="Local Album", report_sink=lambda _text: None
    )
    assert album_dr == "12"
    assert all(value[0] == "12" for value in values.values())


def test_track_totals_without_album_identity_cannot_prove_a_group(tmp_path: Path) -> None:
    tracks = [tmp_path / "01.flac", tmp_path / "02.flac"]
    for number, path in enumerate(tracks, start=1):
        _flac(path, 400 + number * 100, number, 2)
        audio = FLAC(path)
        del audio["ALBUM"]
        del audio["ALBUMARTIST"]
        audio.save()
    assert tagger.assess_album_completeness(tracks, tmp_path).state == "UNKNOWN"


def test_queen_archival_source_preview_and_embedded_crop_agree(tmp_path: Path) -> None:
    candidate = _queen_candidate(tmp_path, "Made In Heaven (1995-10-24)", "cover.original.png")
    source = covers.save_original_cover(candidate.raw_data or b"", tmp_path / "cover.original")
    playback = tmp_path / "cover.jpg"
    covers.save_optimized_cover(candidate.raw_data or b"", playback, crop_box=candidate.crop_box)
    preview = tmp_path / "preview.jpg"
    covers.create_comparison_image(None, None, candidate, preview, target_dim=320)
    track = tmp_path / "01.flac"
    _flac(track, 440, 1, 1)
    metadata_writer.write_tags(track, [], picture_file=playback, max_picture_dim=None)
    front = next(picture for picture in FLAC(track).pictures if picture.type == 3)
    with Image.open(io.BytesIO(front.data)) as embedded, Image.open(playback) as saved:
        assert embedded.size == saved.size == (candidate.effective_width, candidate.effective_height)
        assert hashlib.sha256(front.data).hexdigest() == hashlib.sha256(playback.read_bytes()).hexdigest()
    assert source.read_bytes() == candidate.raw_data
    assert preview.is_file()


def test_innuendo_embedded_front_excludes_notch(tmp_path: Path) -> None:
    candidate = _queen_candidate(tmp_path, "Innuendo (1991-01-14)", "cover.original.jpg")
    playback = tmp_path / "cover.jpg"
    covers.save_optimized_cover(candidate.raw_data or b"", playback, crop_box=candidate.crop_box)
    track = tmp_path / "01.flac"
    _flac(track, 440, 1, 1)
    metadata_writer.write_tags(track, [], picture_file=playback, max_picture_dim=None)
    embedded = next(picture for picture in FLAC(track).pictures if picture.type == 3)
    with Image.open(io.BytesIO(embedded.data)) as image:
        assert image.width == image.height == candidate.height


def test_preview_shows_distinct_winner_and_source_labels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    labels: list[str] = []

    def record(self: ImageDraw.ImageDraw, xy: object, value: str, *args: object, **kwargs: object) -> None:
        labels.append(str(value))

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
    digital = covers.CoverCandidate("Apple Music (iTunes)", "digital", "Album", 1, raw_data=_small_jpeg())
    scan = covers.CoverCandidate("Local Optical Scan", "scan", "Album", 1, raw_data=_small_jpeg())
    winner = covers.CoverCandidate("Local Existing File", "local", "Album", 1, raw_data=_small_jpeg())
    path = tmp_path / "compare.jpg"
    covers.create_comparison_image(digital, scan, winner, path, target_dim=250)
    assert any("Digital Service Artwork" in label for label in labels)
    assert any("Physical Scan" in label for label in labels)
    assert any("Local Artwork" in label for label in labels)
    assert sum("ВЫБОР СИСТЕМЫ" in label for label in labels) == 1
    with Image.open(path) as image:
        assert image.width > 3 * 250


def test_artwork_provenance_and_crop_survive_report_reconstruction(tmp_path: Path) -> None:
    report = {
        "provider": "Cover Art Archive (Release)",
        "url": "https://example.test/image",
        "title": "Album",
        "resolution": [1200, 1100],
        "effective_resolution": [1050, 1050],
        "provenance": "physical_scan",
        "source_form": "compound_package_scan",
        "crop_box": [100, 25, 1150, 1075],
        "crop_confidence": 0.91,
    }
    rebuilt = covers._parse_cand_from_dict(report, tmp_path, "Unknown", "missing.jpg")
    assert rebuilt is not None
    assert rebuilt.provenance == covers.ArtworkProvenance.PHYSICAL_SCAN
    assert rebuilt.source_form == "compound_package_scan"
    assert rebuilt.crop_box == (100, 25, 1150, 1075)
    assert "Physical Scan" in covers.artwork_label(rebuilt)
    another = covers.CoverCandidate("Cover Art Archive (Release)", "other", "Album", 1)
    assert another.provenance == covers.ArtworkProvenance.UNKNOWN


def test_square_package_scan_can_have_removable_frame() -> None:
    rng = np.random.default_rng(5)
    pixels = rng.integers(35, 90, size=(1000, 1000), dtype=np.uint8)
    pixels[70:930, 70:930] = 190
    box, confidence = covers.CoverFetcher._front_crop_box(Image.fromarray(pixels))
    assert box is not None and confidence >= 0.8
    assert abs(box[0] - 70) < 15
    assert abs(box[1] - 70) < 15
    assert box[2] - box[0] == box[3] - box[1]


def test_intentional_dark_border_and_circle_are_not_cut() -> None:
    image = Image.new("L", (1000, 1000), 20)
    draw = ImageDraw.Draw(image)
    draw.rectangle((70, 70, 929, 929), fill=190)
    draw.ellipse((850, 420, 990, 560), fill=10)
    box, _confidence = covers.CoverFetcher._front_crop_box(image)
    assert box is None


def test_rectangular_scan_requires_textured_spine_not_just_one_dark_strip() -> None:
    rng = np.random.default_rng(8)
    pixels = np.full((1000, 1140), 185, dtype=np.uint8)
    pixels[:, :140] = rng.integers(25, 85, size=(1000, 140), dtype=np.uint8)
    box, confidence = covers.CoverFetcher._front_crop_box(Image.fromarray(pixels))
    assert box is not None and confidence >= 0.8
    assert abs(box[0] - 140) < 15
    assert box[2] - box[0] == 1000


def test_smaller_clean_front_beats_high_resolution_queen_package(tmp_path: Path) -> None:
    package = _queen_candidate(tmp_path, "Made In Heaven (1995-10-24)", "cover.original.png")
    assert package.crop_box is not None
    # A representative clean-source alternative made from the detected front
    # panel; this fixture is not claimed to be an original digital master.
    with Image.open(io.BytesIO(package.raw_data or b"")) as image:
        front = image.crop(package.crop_box)
        front.thumbnail((1500, 1500))
        output = io.BytesIO()
        front.save(output, format="JPEG", quality=93)
    fetcher = covers.CoverFetcher("Queen", "Made In Heaven", cache_dir=tmp_path / "comparison-cache")
    clean = fetcher.evaluate_candidate(
        covers.CoverCandidate(
            "Apple Music (iTunes)",
            "fixture://clean",
            "Made In Heaven",
            1.0,
            raw_data=output.getvalue(),
            identity_scope="local",
        )
    )
    assert fetcher._best_in_release_tier([package, clean]) is clean
    assert package.score_breakdown["packaging_penalty"] > 10


def test_clean_local_artwork_is_preserved_without_reencoding(tmp_path: Path) -> None:
    raw = _small_jpeg()
    path = tmp_path / "cover.jpg"
    covers.save_optimized_cover(raw, path)
    assert path.read_bytes() == raw


def test_process_album_writes_clean_cover_lyrics_and_album_gain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "Example Artist" / "Example Album"
    folder.mkdir(parents=True)
    tracks = [folder / "01 - First.flac", folder / "02 - Second.flac"]
    for number, path in enumerate(tracks, start=1):
        _flac(path, 440 + number * 100, number, 2)
        audio = FLAC(path)
        audio["TITLE"] = ["First" if number == 1 else "Second"]
        audio["ALBUM"] = ["Example Album"]
        audio["ALBUMARTIST"] = ["Example Artist"]
        audio["ARTIST"] = ["Example Artist"]
        audio.save()
    image = Image.new("RGB", (660, 600), "#1a1a1a")
    draw = ImageDraw.Draw(image)
    draw.rectangle((60, 0, 659, 599), fill="#d08050")
    draw.ellipse((180, 120, 520, 460), fill="#4050a0")
    original_buffer = io.BytesIO()
    image.save(original_buffer, format="PNG")
    original = original_buffer.getvalue()
    (folder / "cover.jpg").write_bytes(original)
    # User files with names resembling old staging paths must survive.
    (folder / "cover_tmp.jpg").write_bytes(b"user preview")
    (folder / "cover_source_tmp.png").write_bytes(b"user archive")
    discogs = {
        "id": 1,
        "title": "Example Album",
        "artists": [{"name": "Example Artist", "id": 1}],
        "released": "2020-01-01",
        "country": "US",
        "formats": [{"name": "CD"}],
        "tracklist": [
            {"position": "1", "title": "First", "duration": "0:01", "type_": "track"},
            {"position": "2", "title": "Second", "duration": "0:01", "type_": "track"},
        ],
    }
    monkeypatch.setattr(tagger, "discogs_get", lambda *_args, **_kwargs: discogs)
    monkeypatch.setattr(tagger, "mb_get", lambda *_args, **_kwargs: (404, {}))
    monkeypatch.setattr(tagger, "query_acoustid_candidates", lambda *_args, **_kwargs: (set(), set()))

    def local_cover(fetcher: covers.CoverFetcher, **_kwargs: object) -> tuple[object, object, object, object]:
        assert not fetcher.write_preview_files
        candidate = fetcher.evaluate_candidate(
            covers.CoverCandidate(
                "Local Existing File",
                str(folder / "cover.jpg"),
                "Example Album",
                1.0,
                raw_data=(folder / "cover.jpg").read_bytes(),
                identity_scope="local",
            )
        )
        return candidate, None, None, [candidate]

    monkeypatch.setattr(covers.CoverFetcher, "find_best_cover", local_cover)
    monkeypatch.setattr(
        tagger.lyrics_utils,
        "fetch_betterlyrics",
        lambda **kwargs: {
            "found": True,
            "source": "Fixture Lyrics",
            "provider_track_id": kwargs["song"],
            "trackName": kwargs["song"],
            "artistName": kwargs["artist"],
            "albumName": "Example Album",
            "duration": kwargs["duration"],
            "identity_validated": True,
            "plainLyrics": "A long phrase of original poetry appears here\nThe next verse carries another thoughtful phrase\nA third line remembers a distant summer night\nThe refrain repeats a familiar melody again",
            "syncedLyrics": "",
            "rawLyrics": "A long phrase of original poetry appears here\nThe next verse carries another thoughtful phrase\nA third line remembers a distant summer night\nThe refrain repeats a familiar melody again",
            "format": "plain",
            "syncType": "none",
        },
    )
    original_pcm = {path: metadata_writer.pcm_digest(path) for path in tracks}
    original_tree = {path.relative_to(folder): path.read_bytes() for path in folder.rglob("*") if path.is_file()}
    real_run = subprocess.run

    def dry_run_tools(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        assert Path(command[0]).name not in {"loudgain", "metaflac", "dr14_tmeter"}
        return real_run(command, **kwargs)

    monkeypatch.setattr(tagger.subprocess, "run", dry_run_tools)
    assert tagger.process_album(
        f"1:::{folder}:::2020 CD",
        "fixture-discogs-token",
        "fixture@example.org",
        quiet=True,
        dry_run=True,
        no_loudgain=False,
        no_dr14=False,
        threads=2,
        session=requests.Session(),
        discogs_id_override="1",
    )
    assert {
        path.relative_to(folder): path.read_bytes() for path in folder.rglob("*") if path.is_file()
    } == original_tree
    assert not list(folder.glob(".metadata-transaction-*"))
    monkeypatch.setattr(tagger.subprocess, "run", real_run)
    succeeded = tagger.process_album(
        f"1:::{folder}:::2020 CD",
        "fixture-discogs-token",
        "fixture@example.org",
        quiet=True,
        no_loudgain=False,
        no_dr14=True,
        threads=1,
        session=requests.Session(),
        discogs_id_override="1",
    )
    assert succeeded
    assert {path: metadata_writer.pcm_digest(path) for path in tracks} == original_pcm
    assert (folder / "cover_tmp.jpg").read_bytes() == b"user preview"
    assert (folder / "cover_source_tmp.png").read_bytes() == b"user archive"
    assert (folder / "cover.jpg").read_bytes() == original
    with Image.open(folder / "cover.jpg") as saved:
        assert saved.size == (660, 600)
    for path in tracks:
        audio = FLAC(path)
        assert audio.get("REPLAYGAIN_ALBUM_GAIN") and audio.get("REPLAYGAIN_TRACK_GAIN")
        assert audio.get("LYRICS")
        front = next(picture for picture in audio.pictures if picture.type == 3)
        with Image.open(io.BytesIO(front.data)) as embedded:
            assert embedded.size == (660, 600)

    stable_cover = (folder / "cover.jpg").read_bytes()
    stable_sidecars = {path.name for path in folder.glob("*.txt")}
    stable_tracks = {path: path.read_bytes() for path in tracks}
    stable_ledger = (folder / ".metadata-provenance.jsonl").read_bytes()
    stable_tag_maps = {path: dict(FLAC(path)) for path in tracks}
    stable_picture_hashes = {
        path: [hashlib.sha256(picture.data).hexdigest() for picture in FLAC(path).pictures] for path in tracks
    }
    assert tagger.process_album(
        f"1:::{folder}:::2020 CD",
        "fixture-discogs-token",
        "fixture@example.org",
        quiet=True,
        no_loudgain=False,
        no_dr14=True,
        threads=1,
        session=requests.Session(),
        discogs_id_override="1",
    )
    assert (folder / "cover.jpg").read_bytes() == stable_cover
    assert {path.name for path in folder.glob("*.txt")} == stable_sidecars
    current_tag_maps = {path: dict(FLAC(path)) for path in tracks}
    changed_tags = {
        path.name: {
            key: (stable_tag_maps[path].get(key), current_tag_maps[path].get(key))
            for key in stable_tag_maps[path].keys() | current_tag_maps[path].keys()
            if stable_tag_maps[path].get(key) != current_tag_maps[path].get(key)
        }
        for path in tracks
    }
    assert current_tag_maps == stable_tag_maps, changed_tags
    assert {
        path: [hashlib.sha256(picture.data).hexdigest() for picture in FLAC(path).pictures] for path in tracks
    } == stable_picture_hashes
    assert {path: path.read_bytes() for path in tracks} == stable_tracks
    assert (folder / ".metadata-provenance.jsonl").read_bytes() == stable_ledger

    # Force a loudgain scan and fail it after the track workers have staged
    # their metadata. No FLAC, cover, sidecar, or ledger change may commit.
    first_audio = FLAC(tracks[0])
    del first_audio["REPLAYGAIN_ALBUM_GAIN"]
    first_audio.save()
    before_failed_loudness = {path: path.read_bytes() for path in tracks}
    ledger = folder / ".metadata-provenance.jsonl"
    ledger_before_failed_loudness = ledger.read_bytes()
    real_subprocess_run = tagger.subprocess.run

    def fail_loudgain(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if command and "loudgain" in str(command[0]):
            raise subprocess.CalledProcessError(1, command, stderr="injected loudgain failure")
        return real_subprocess_run(command, **kwargs)

    monkeypatch.setattr(tagger.subprocess, "run", fail_loudgain)
    assert not tagger.process_album(
        f"1:::{folder}:::2020 CD",
        "fixture-discogs-token",
        "fixture@example.org",
        quiet=True,
        no_loudgain=False,
        no_dr14=True,
        threads=1,
        session=requests.Session(),
        discogs_id_override="1",
    )
    monkeypatch.setattr(tagger.subprocess, "run", real_subprocess_run)
    assert {path: path.read_bytes() for path in tracks} == before_failed_loudness
    assert ledger.read_bytes() == ledger_before_failed_loudness
    assert not list(folder.glob(".metadata-transaction-*"))

    before = {path: path.read_bytes() for path in tracks}
    ledger_before = ledger.read_bytes()
    cover_before = (folder / "cover.jpg").read_bytes()
    real_replace = tagger.os.replace

    def fail_second_album_commit(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == tracks[1] and ".metadata-transaction-" in str(source):
            raise OSError("injected second-track commit failure")
        real_replace(source, destination)

    monkeypatch.setattr(tagger.os, "replace", fail_second_album_commit)
    assert not tagger.process_album(
        f"1:::{folder}:::2020 CD",
        "fixture-discogs-token",
        "fixture@example.org",
        quiet=True,
        no_loudgain=False,
        no_dr14=True,
        threads=1,
        session=requests.Session(),
        discogs_id_override="1",
    )
    assert {path: path.read_bytes() for path in tracks} == before
    assert (folder / "cover.jpg").read_bytes() == cover_before
    assert ledger.read_bytes() == ledger_before
    assert not list(folder.glob(".metadata-transaction-*"))

    def fail_commit_and_rollback(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == tracks[1] and ".metadata-transaction-" in str(source):
            raise OSError("injected second-track commit failure")
        if Path(destination) == tracks[0] and "/originals/" in str(source):
            raise OSError("injected rollback failure")
        real_replace(source, destination)

    monkeypatch.setattr(tagger.os, "replace", fail_commit_and_rollback)
    assert not tagger.process_album(
        f"1:::{folder}:::2020 CD",
        "fixture-discogs-token",
        "fixture@example.org",
        quiet=True,
        no_loudgain=False,
        no_dr14=True,
        threads=1,
        session=requests.Session(),
        discogs_id_override="1",
    )
    recovery = list(folder.glob(".metadata-transaction-*/originals/01 - First.flac"))
    assert len(recovery) == 1
    assert recovery[0].read_bytes() == before[tracks[0]]
    assert ledger.read_bytes() == ledger_before


def test_album_staging_copy_failure_cleans_residue_and_preserves_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracks = [tmp_path / "01.flac", tmp_path / "02.flac"]
    for number, track in enumerate(tracks, 1):
        _flac(track, 440 + number, number, 2)
    original_bytes = [track.read_bytes() for track in tracks]
    real_copy = tagger.shutil.copy2
    calls = 0

    def fail_second_copy(source: Path, destination: Path) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected copy failure")
        return Path(real_copy(source, destination))

    monkeypatch.setattr(tagger.shutil, "copy2", fail_second_copy)
    with pytest.raises(OSError, match="injected copy failure"):
        tagger.stage_album_files(tmp_path, tracks)
    assert [track.read_bytes() for track in tracks] == original_bytes
    assert not list(tmp_path.glob(".metadata-transaction-*"))


def test_curated_release_mapping_requires_the_actual_album_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    same_name = tmp_path / "Another Artist" / "Queen II (1974-04-09)"
    same_name.mkdir(parents=True)
    monkeypatch.setattr(tagger.discogs_release_find, "find_album_folders", lambda *_args, **_kwargs: [same_name])
    entries = tagger.resolve_target_albums([str(same_name)], tmp_path)
    assert entries == ["auto:::Another Artist/Queen II (1974-04-09):::auto"]


def test_curated_keyword_does_not_schedule_nonexistent_album_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tagger.discogs_release_find, "find_album_folders", lambda *_args, **_kwargs: [])
    assert tagger.resolve_target_albums(["Queen"], tmp_path) == []


def test_album_refuses_symlinked_tracks_before_provider_lookup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.flac"
    _flac(source, 440, 1, 1)
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    linked = album / "01.flac"
    linked.symlink_to(source)
    monkeypatch.setattr(tagger, "discogs_get", lambda *_args, **_kwargs: pytest.fail("unexpected provider lookup"))
    assert not tagger.process_album(
        f"1:::{album}:::edition",
        "token",
        "contact@example.org",
        quiet=True,
        no_loudgain=True,
        no_dr14=True,
        threads=1,
        session=requests.Session(),
    )
    assert linked.is_symlink()


def test_album_refuses_symlinked_cover_before_provider_lookup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    _flac(album / "01.flac", 440, 1, 1)
    target = tmp_path / "outside-cover.jpg"
    target.write_bytes(_small_jpeg())
    linked_cover = album / "cover.jpg"
    linked_cover.symlink_to(target)
    monkeypatch.setattr(tagger, "discogs_get", lambda *_args, **_kwargs: pytest.fail("unexpected provider lookup"))
    assert not tagger.process_album(
        f"1:::{album}:::edition",
        "token",
        "contact@example.org",
        quiet=True,
        no_loudgain=True,
        no_dr14=True,
        threads=1,
        session=requests.Session(),
    )
    assert linked_cover.is_symlink()
    assert target.read_bytes() == _small_jpeg()


@pytest.mark.parametrize(
    "arguments", [["--threads", "0"], ["--mb-results", "100"], ["--embed-max-dim", "-1"], ["--preview-size", "0"]]
)
def test_cli_rejects_invalid_numeric_options_before_processing(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        tagger.main(arguments)
    assert exc.value.code == 2
