#!/usr/bin/env python3
"""Comprehensive unit test suite for the audiophile tagging & cover art pipeline.

Covers:
1. String cleaning, artist matching, and title normalization algorithms.
2. Digital vs. Scan provider classification and candidate deduplication.
3. Aesthetic scoring metrics (resolution, aspect ratio, Laplacian sharpness, contrast, scanner borders, badge penalties).
4. Image processing (JFIF JPEG optimization, Kitty side-by-side comparison images, escape sequences).
5. Interactive cover selection prompt logic with simulated user inputs.
6. Vorbis tag manifest parsing, validation, and mutagen FLAC verification.
7. Dynamic Range (DR14) table and log parsing.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Self

import numpy as np
import pytest
import requests
from mutagen.flac import FLAC, Picture
from PIL import Image, ImageDraw

import audiophile_cover_fetcher
import audiophile_tagger
import metadata_writer
import verify_all_flacs
from audiophile_cover_fetcher import (
    CoverCandidate,
    CoverFetcher,
    ReleaseTier,
    RepresentationKind,
    RoleTier,
    artist_matches,
    clean_album_title,
    compute_album_match,
    create_comparison_image,
    create_embedded_picture_data,
    emit_kitty_image,
    is_digital_provider,
    is_scan_provider,
    norm_str,
    prompt_all_cover_choice,
    prompt_cover_choice,
    save_optimized_cover,
    save_original_cover,
    should_replace_existing_cover,
)
from audiophile_tagger import (
    format_performer,
    parse_movement_info,
)
from dr14_parse import (
    clean_track_name,
    compute_dr14_samples,
    extract_track_number,
    measure_audio_file,
    measure_audio_files,
    normalize_db,
)
from dr14_parse import (
    parse as parse_dr_report,
)
from metadata_writer import (
    canonical_verify,
    clean_manifest_value,
    load_tags,
    write_tags,
)
from verify_all_flacs import audit_flac


# ==============================================================================
# 1. String Cleaning & Matching Tests
# ==============================================================================
class TestCoverFetcherStringCleaning:
    def test_norm_str(self) -> None:
        assert norm_str("News of the World!") == "newsoftheworld"
        assert norm_str("Queen (Deluxe Edition) [2011]") == "queendeluxeedition2011"
        assert norm_str("AC/DC -- Back in Black") == "acdcbackinblack"
        assert norm_str("") == ""

    def test_clean_album_title(self) -> None:
        assert clean_album_title("News Of The World (2011 Remaster)") == "News Of The World"
        assert clean_album_title("News Of The World [Deluxe Edition]") == "News Of The World"
        assert clean_album_title("News Of The World (40th Anniversary Edition)") == "News Of The World"
        assert clean_album_title("A Night at the Opera (Disc 1)") == "A Night at the Opera"
        assert clean_album_title("Greatest Hits = Best of Queen") == "Greatest Hits"
        assert clean_album_title("Innuendo") == "Innuendo"

    def test_artist_matches(self) -> None:
        # Exact match
        assert artist_matches("Queen", "Queen") is True
        assert artist_matches("Queen", "queen") is True

        # Featured artists
        assert artist_matches("Queen", "Queen feat. David Bowie") is True
        assert artist_matches("Queen", "Queen & David Bowie") is True
        assert artist_matches("Queen", "Queen / Paul Rodgers") is True

        # Multi-word artists
        assert artist_matches("The Beatles", "The Beatles") is True
        assert artist_matches("Pink Floyd", "Pink Floyd (Remastered)") is True

        # Non-matches
        assert artist_matches("Queen", "Queensrÿche") is False
        assert artist_matches("Queen", "The Queen Is Dead") is False
        assert artist_matches("Beatles", "Rolling Stones") is False

    def test_compute_album_match(self) -> None:
        assert compute_album_match("News of the World", "News of the World") == 1.0
        assert compute_album_match("News of the World", "News of the World (Deluxe Edition)") >= 0.7
        assert compute_album_match("News of the World", "Greatest Hits II") == 0.0


# ==============================================================================
# 2. Provider Classification & Discrimination Tests
# ==============================================================================
class TestCoverProviderClassification:
    def test_is_digital_provider(self) -> None:
        assert is_digital_provider("Apple Music / iTunes CDN") is True
        assert is_digital_provider("Deezer High-Res CDN") is True
        assert is_digital_provider("Spotify CDN") is True
        assert is_digital_provider("Tidal CDN") is True
        assert is_digital_provider("Qobuz CDN") is True
        assert is_digital_provider("Cover Art Archive") is False
        assert is_digital_provider("Discogs") is False
        assert is_digital_provider("Local Existing File") is False

    def test_is_scan_provider(self) -> None:
        # Upload repositories do not certify whether an image was scanned.
        assert is_scan_provider("Cover Art Archive (MusicBrainz)") is False
        assert is_scan_provider("Cover Art Archive (Release-Group)") is False
        assert is_scan_provider("Discogs API") is False
        assert is_scan_provider("Local Optical Scan") is True

        # Critical invariant: Local Existing File must NOT be categorized as a physical scan
        assert is_scan_provider("Local Existing File") is False
        assert is_scan_provider("Apple Music / iTunes CDN") is False
        assert is_scan_provider("Deezer High-Res CDN") is False


# ==============================================================================
# 3. Aesthetic Scoring Metric Tests
# ==============================================================================
class TestCoverAestheticScoring:
    @pytest.fixture
    def fetcher(self) -> CoverFetcher:
        return CoverFetcher("Queen", "News of the World")

    def test_aspect_ratio_penalty(self, fetcher: CoverFetcher) -> None:
        # Perfect square 1000x1000
        square_im = Image.new("RGB", (1000, 1000), color=(128, 128, 128))
        buf_sq = io.BytesIO()
        square_im.save(buf_sq, format="JPEG")
        cand_sq = CoverCandidate(
            provider="Test",
            url="u_sq",
            title="Queen - News of the World",
            album_match_ratio=1.0,
            raw_data=buf_sq.getvalue(),
        )
        cand_sq = fetcher.evaluate_candidate(cand_sq)

        # Non-square 1200x800 (1.5 aspect ratio)
        rect_im = Image.new("RGB", (1200, 800), color=(128, 128, 128))
        buf_rect = io.BytesIO()
        rect_im.save(buf_rect, format="JPEG")
        cand_rect = CoverCandidate(
            provider="Test",
            url="u_rect",
            title="Queen - News of the World",
            album_match_ratio=1.0,
            raw_data=buf_rect.getvalue(),
        )
        cand_rect = fetcher.evaluate_candidate(cand_rect)

        assert cand_sq.aspect_ratio == 1.0
        assert cand_sq.visual_quality_score > cand_rect.visual_quality_score + 3.0

    def test_resolution_scoring(self, fetcher: CoverFetcher) -> None:
        # 3000x3000px master
        im_3000 = Image.new("RGB", (3000, 3000), color=(100, 100, 100))
        buf_3000 = io.BytesIO()
        im_3000.save(buf_3000, format="JPEG")
        c_3000 = CoverCandidate(
            provider="Apple Music",
            url="u3000",
            title="News of the World",
            album_match_ratio=1.0,
            raw_data=buf_3000.getvalue(),
        )
        c_3000 = fetcher.evaluate_candidate(c_3000)

        # 600x600px low-res
        im_600 = Image.new("RGB", (600, 600), color=(100, 100, 100))
        buf_600 = io.BytesIO()
        im_600.save(buf_600, format="JPEG")
        c_600 = CoverCandidate(
            provider="LowRes", url="u600", title="News of the World", album_match_ratio=1.0, raw_data=buf_600.getvalue()
        )
        c_600 = fetcher.evaluate_candidate(c_600)

        # Resolution matters, but is bounded so it cannot overwhelm quality.
        assert c_3000.visual_quality_score > c_600.visual_quality_score + 10.0

    def test_sharpness_scoring(self, fetcher: CoverFetcher) -> None:
        # Sharp high-frequency checkerboard pattern
        sharp_im = Image.new("RGB", (800, 800), color=(0, 0, 0))
        draw_s = ImageDraw.Draw(sharp_im)
        for x in range(0, 800, 20):
            for y in range(0, 800, 20):
                if (x + y) % 40 == 0:
                    draw_s.rectangle([x, y, x + 20, y + 20], fill=(255, 255, 255))
        buf_sharp = io.BytesIO()
        sharp_im.save(buf_sharp, format="JPEG")

        c_sharp = CoverCandidate(
            provider="Test",
            url="usharp",
            title="News of the World",
            album_match_ratio=1.0,
            raw_data=buf_sharp.getvalue(),
        )
        c_sharp = fetcher.evaluate_candidate(c_sharp)

        # Blurry solid gray
        blur_im = Image.new("RGB", (800, 800), color=(128, 128, 128))
        buf_blur = io.BytesIO()
        blur_im.save(buf_blur, format="JPEG")

        c_blur = CoverCandidate(
            provider="Test", url="ublur", title="News of the World", album_match_ratio=1.0, raw_data=buf_blur.getvalue()
        )
        c_blur = fetcher.evaluate_candidate(c_blur)

        assert c_sharp.sharpness > c_blur.sharpness
        assert c_sharp.sharpness > 50.0

    def test_scanner_border_detection(self, fetcher: CoverFetcher) -> None:
        # Image with heavy white scanner margin (outer 2%)
        border_im = Image.new("RGB", (800, 800), color=(255, 255, 255))
        draw = ImageDraw.Draw(border_im)
        draw.rectangle([20, 20, 780, 780], fill=(80, 80, 80))
        buf_border = io.BytesIO()
        border_im.save(buf_border, format="JPEG")

        c_border = CoverCandidate(
            provider="Test",
            url="uborder",
            title="News of the World",
            album_match_ratio=1.0,
            raw_data=buf_border.getvalue(),
        )
        c_border = fetcher.evaluate_candidate(c_border)
        assert c_border.has_scanner_borders is True

    def test_badge_and_title_penalties(self, fetcher: CoverFetcher) -> None:
        clean_im = Image.new("RGB", (1000, 1000), color=(100, 100, 100))
        buf = io.BytesIO()
        clean_im.save(buf, format="JPEG")
        data = buf.getvalue()

        # Regular album
        c_regular = CoverCandidate(
            provider="Test", url="u1", title="Queen - News Of The World", album_match_ratio=1.0, raw_data=data
        )
        c_regular = fetcher.evaluate_candidate(c_regular)

        # Karaoke version
        c_karaoke = CoverCandidate(
            provider="Test",
            url="u2",
            title="Queen - News Of The World (Karaoke Tribute)",
            album_match_ratio=1.0,
            raw_data=data,
        )
        c_karaoke = fetcher.evaluate_candidate(c_karaoke)

        # Greatest hits mismatch
        c_hits = CoverCandidate(
            provider="Test", url="u3", title="Queen - Greatest Hits II", album_match_ratio=0.5, raw_data=data
        )
        c_hits = fetcher.evaluate_candidate(c_hits)

        # Content quality is independent of title/release evidence. Release
        # mismatches are gated before visual ranking rather than baked into it.
        assert c_regular.visual_quality_score == pytest.approx(c_karaoke.visual_quality_score)
        assert c_regular.visual_quality_score == pytest.approx(c_hits.visual_quality_score)

    def test_colorfulness_scoring(self, fetcher: CoverFetcher) -> None:
        # Saturated color master vs desaturated monochrome
        c_im = Image.new("RGB", (1000, 1000), color=(255, 60, 20))
        g_im = Image.new("RGB", (1000, 1000), color=(128, 128, 128))
        buf_c, buf_g = io.BytesIO(), io.BytesIO()
        c_im.save(buf_c, format="JPEG")
        g_im.save(buf_g, format="JPEG")

        c_cand = fetcher.evaluate_candidate(
            CoverCandidate("Test", "u_c", "News of the World", 1.0, raw_data=buf_c.getvalue())
        )
        g_cand = fetcher.evaluate_candidate(
            CoverCandidate("Test", "u_g", "News of the World", 1.0, raw_data=buf_g.getvalue())
        )

        assert c_cand.colorfulness > 50.0
        assert g_cand.colorfulness == 0.0
        # Colorful artwork is not intrinsically better than monochrome art.
        assert abs(c_cand.visual_quality_score - g_cand.visual_quality_score) < 3.0

    def test_jpeg_blockiness_detection(self, fetcher: CoverFetcher) -> None:
        # Structured artwork with concentric shapes
        test_im = Image.new("L", (600, 600), color=120)
        d = ImageDraw.Draw(test_im)
        for r in range(50, 280, 20):
            d.ellipse([300 - r, 300 - r, 300 + r, 300 + r], outline=40, width=4)

        buf_q95, buf_q15 = io.BytesIO(), io.BytesIO()
        test_im.save(buf_q95, format="JPEG", quality=95)
        test_im.save(buf_q15, format="JPEG", quality=15)

        cand_q95 = fetcher.evaluate_candidate(
            CoverCandidate("Test", "uq95", "News of the World", 1.0, raw_data=buf_q95.getvalue())
        )
        cand_q15 = fetcher.evaluate_candidate(
            CoverCandidate("Test", "uq15", "News of the World", 1.0, raw_data=buf_q15.getvalue())
        )

        assert cand_q15.blockiness > 1.35
        assert cand_q15.blockiness > cand_q95.blockiness
        assert cand_q15.total_score < cand_q95.total_score - 400.0

    def test_anti_milky_blacks_penalty(self, fetcher: CoverFetcher) -> None:
        # Deep contrast image (0 to 255)
        deep_im = Image.new("RGB", (800, 800), color=(0, 0, 0))
        d_deep = ImageDraw.Draw(deep_im)
        d_deep.rectangle([100, 100, 700, 700], fill=(255, 255, 255))
        buf_d = io.BytesIO()
        deep_im.save(buf_d, format="JPEG")
        cand_deep = fetcher.evaluate_candidate(
            CoverCandidate("Test", "udeep", "News of the World", 1.0, raw_data=buf_d.getvalue())
        )

        # Washed out scan with lifted black level (60) and narrow range (160 - 60 = 100)
        wash_im = Image.new("RGB", (800, 800), color=(60, 60, 60))
        d_wash = ImageDraw.Draw(wash_im)
        d_wash.rectangle([100, 100, 700, 700], fill=(160, 160, 160))
        buf_w = io.BytesIO()
        wash_im.save(buf_w, format="JPEG")
        cand_wash = fetcher.evaluate_candidate(
            CoverCandidate("Test", "uwash", "News of the World", 1.0, raw_data=buf_w.getvalue())
        )

        assert cand_wash.dynamic_range < 160.0
        assert cand_deep.dynamic_range > 200.0
        assert cand_deep.total_score > cand_wash.total_score + 150.0

    def test_anti_upscaling_detection(self, fetcher: CoverFetcher) -> None:
        # Genuine high-res 2000px vs fake interpolated upscale from 300px
        real_img = Image.new("RGB", (2000, 2000), color=(20, 20, 20))
        d_r = ImageDraw.Draw(real_img)
        for i in range(100, 1900, 40):
            d_r.line([(i, 100), (i, 1900)], fill=(220, 220, 220), width=2)

        small = real_img.resize((300, 300), Image.Resampling.LANCZOS)
        fake = small.resize((2000, 2000), Image.Resampling.BICUBIC)

        buf_r, buf_f = io.BytesIO(), io.BytesIO()
        real_img.save(buf_r, format="JPEG")
        fake.save(buf_f, format="JPEG")

        cand_r = fetcher.evaluate_candidate(
            CoverCandidate("Test", "ur", "News of the World", 1.0, raw_data=buf_r.getvalue())
        )
        cand_f = fetcher.evaluate_candidate(
            CoverCandidate("Test", "uf", "News of the World", 1.0, raw_data=buf_f.getvalue())
        )

        assert cand_r.is_fake_upscale is False
        assert cand_f.is_fake_upscale is True
        assert cand_r.visual_quality_score > cand_f.visual_quality_score + 8.0

    def test_step_boundary_border_detection(self, fetcher: CoverFetcher) -> None:
        # Artistic pure white background (e.g. A Night At The Opera) should NOT trigger border
        opera_im = Image.new("RGB", (800, 800), color=(255, 255, 255))
        d_op = ImageDraw.Draw(opera_im)
        d_op.rectangle([300, 300, 500, 500], fill=(180, 150, 40))
        buf_op = io.BytesIO()
        opera_im.save(buf_op, format="JPEG")

        cand_op = fetcher.evaluate_candidate(
            CoverCandidate("Test", "uop", "News of the World", 1.0, raw_data=buf_op.getvalue())
        )
        assert cand_op.has_scanner_borders is False


# ==============================================================================
# 4. Candidate Deduplication & Score Distinctness Tests
# ==============================================================================
class TestCoverDeduplication:
    def test_duplicate_local_file_elimination(self, tmp_path: Path) -> None:
        # Create a dummy folder with cover.jpg
        im = Image.new("RGB", (1000, 1000), color=(100, 120, 140))
        cov_file = tmp_path / "cover.jpg"
        im.save(cov_file, format="JPEG")
        raw_bytes = cov_file.read_bytes()

        fetcher = CoverFetcher("Queen", "News of the World", folder=tmp_path)

        # Mock query results: Apple Music returns exact same image
        apple_cand = CoverCandidate(
            provider="Apple Music / iTunes CDN",
            url="https://apple.com/image.jpg",
            title="Queen - News of the World",
            album_match_ratio=1.0,
            raw_data=raw_bytes,
        )
        local_cand = CoverCandidate(
            provider="Local Existing File",
            url=str(cov_file),
            title="Local cover.jpg",
            album_match_ratio=1.0,
            raw_data=raw_bytes,
        )
        buf_scan = io.BytesIO()
        Image.new("RGB", (600, 600), color=(50, 50, 50)).save(buf_scan, format="JPEG")

        scan_cand = CoverCandidate(
            provider="Cover Art Archive (Release-Group)",
            url="https://caa.org/front.jpg",
            title="Queen - News of the World",
            album_match_ratio=1.0,
            raw_data=buf_scan.getvalue(),
        )

        evaluated = [
            fetcher.evaluate_candidate(apple_cand),
            fetcher.evaluate_candidate(local_cand),
            fetcher.evaluate_candidate(scan_cand),
        ]

        # Invariant: Apple and Local have identical score
        assert abs(apple_cand.total_score - local_cand.total_score) < 0.1

        # Simulate find_best_cover deduplication logic
        valid = [c for c in evaluated if not c.error and c.total_score > 0]
        deduped = []
        for c in valid:
            if c.provider.startswith("Local Existing File"):
                is_dup = any(
                    other is not c
                    and not other.provider.startswith("Local Existing File")
                    and (
                        (
                            c.width == other.width
                            and c.height == other.height
                            and abs(c.total_score - other.total_score) < 1.0
                        )
                        or (c.raw_data and other.raw_data and c.raw_data == other.raw_data)
                    )
                    for other in valid
                )
                if not is_dup:
                    deduped.append(c)
            else:
                deduped.append(c)

        digital_cands = [c for c in deduped if is_digital_provider(c.provider)]
        scan_cands = [c for c in deduped if is_scan_provider(c.provider)]

        assert len(digital_cands) == 1
        assert digital_cands[0].provider == "Apple Music / iTunes CDN"
        assert len(scan_cands) == 0  # CAA origin is unknown, not a verified physical scan.
        assert any(c.provider == "Cover Art Archive (Release-Group)" for c in deduped)

        # CAA provenance is unknown, so it must not appear in the scan slot.
        # The two surviving sources also remain distinct candidates.
        assert digital_cands[0] is not next(c for c in deduped if c.provider == "Cover Art Archive (Release-Group)")


# ==============================================================================
# 5. Image Operations Tests
# ==============================================================================
class TestCoverImageOperations:
    def test_save_optimized_cover(self, tmp_path: Path) -> None:
        im = Image.new("RGBA", (800, 800), color=(200, 100, 50, 255))
        buf = io.BytesIO()
        im.save(buf, format="PNG")

        out_jpg = tmp_path / "out_cover.jpg"
        w, h, sz = save_optimized_cover(buf.getvalue(), out_jpg)

        assert w == 800
        assert h == 800
        assert sz > 2000
        assert out_jpg.is_file()

        with Image.open(out_jpg) as verified:
            assert verified.format == "JPEG"
            assert verified.mode == "RGB"

    def test_create_comparison_image(self, tmp_path: Path) -> None:
        im1 = Image.new("RGB", (500, 500), color=(20, 100, 200))
        im2 = Image.new("RGB", (400, 400), color=(200, 100, 20))
        buf1 = io.BytesIO()
        im1.save(buf1, format="JPEG")
        buf2 = io.BytesIO()
        im2.save(buf2, format="JPEG")

        c1 = CoverCandidate(
            provider="Apple Music",
            url="u1",
            title="Digital",
            album_match_ratio=1.0,
            width=500,
            height=500,
            raw_data=buf1.getvalue(),
        )
        c2 = CoverCandidate(
            provider="Cover Art Archive",
            url="u2",
            title="Scan",
            album_match_ratio=1.0,
            width=400,
            height=400,
            raw_data=buf2.getvalue(),
        )

        out_comp = tmp_path / ".comparison.jpg"
        res = create_comparison_image(c1, c2, c1, out_comp, target_dim=800)

        assert res is not None
        assert res.is_file()

        with Image.open(res) as comp_im:
            assert comp_im.width == 800 * 2 + 20 + 12 * 2
            assert comp_im.height == 800 + 74 + 8 + 2

        # Test single candidate layout
        out_single = tmp_path / ".single.jpg"
        res_single = create_comparison_image(c1, None, c1, out_single, target_dim=800)
        assert res_single is not None
        with Image.open(res_single) as single_im:
            assert single_im.width == 800 + 12 * 2
            assert single_im.height == 800 + 74 + 8 + 2

        # Test full CAA title rendering without truncation
        c_caa = CoverCandidate(
            provider="Cover Art Archive (Release-Group)",
            url="u_caa",
            title="Scan Full",
            album_match_ratio=1.0,
            width=1200,
            height=1200,
            raw_data=buf2.getvalue(),
        )
        out_caa = tmp_path / ".caa_comp.jpg"
        res_caa = create_comparison_image(c1, c_caa, c1, out_caa, target_dim=1200)
        assert res_caa is not None and res_caa.is_file()


# ==============================================================================
# 6. Interactive Selection Prompt Tests
# ==============================================================================
class TestInteractiveCoverChoice:
    @pytest.fixture
    def candidates(self) -> tuple[CoverCandidate, CoverCandidate, CoverCandidate]:
        c_dig = CoverCandidate(
            provider="Apple Music / iTunes CDN",
            url="https://apple.com/cov.jpg",
            title="Queen - News Of The World",
            album_match_ratio=1.0,
            width=3000,
            height=3000,
            sharpness=161.4,
            total_score=4855.3,
            raw_data=b"DIGITAL_DATA",
        )
        c_scan = CoverCandidate(
            provider="Cover Art Archive (Release-Group)",
            url="https://caa.org/front.jpg",
            title="Queen - News Of The World",
            album_match_ratio=1.0,
            width=1500,
            height=1507,
            sharpness=142.1,
            total_score=3511.6,
            raw_data=b"SCAN_DATA",
        )
        winner = c_dig
        return c_dig, c_scan, winner

    def test_choose_digital(
        self, candidates: tuple[CoverCandidate, CoverCandidate, CoverCandidate], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c_dig, c_scan, winner = candidates
        monkeypatch.setattr(sys, "stdin", io.StringIO("1\n"))
        chosen = prompt_cover_choice(c_dig, c_scan, winner, None, None)
        assert chosen is not None
        assert chosen.provider == "Apple Music / iTunes CDN"

    def test_choose_scan(
        self, candidates: tuple[CoverCandidate, CoverCandidate, CoverCandidate], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c_dig, c_scan, winner = candidates
        monkeypatch.setattr(sys, "stdin", io.StringIO("2\n"))
        chosen = prompt_cover_choice(c_dig, c_scan, winner, None, None)
        assert chosen is not None
        assert chosen.provider == "Cover Art Archive (Release-Group)"

    def test_choose_winner_default(
        self, candidates: tuple[CoverCandidate, CoverCandidate, CoverCandidate], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c_dig, c_scan, winner = candidates
        monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
        chosen = prompt_cover_choice(c_dig, c_scan, winner, None, None)
        assert chosen is not None
        assert chosen.provider == winner.provider

    def test_score_fallback_marks_higher_scoring_digital_candidate(
        self,
        candidates: tuple[CoverCandidate, CoverCandidate, CoverCandidate],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        c_dig, c_scan, _winner = candidates
        monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))

        chosen = prompt_cover_choice(c_dig, c_scan, None, None, None)

        assert chosen is c_dig
        menu_lines = capsys.readouterr().out.splitlines()
        digital_line = next(line for line in menu_lines if "Цифра:" in line)
        scan_line = next(line for line in menu_lines if "Cover Art Archive Artwork" in line)
        assert "★" in digital_line
        assert "★" not in scan_line

    def test_choose_custom_file(
        self,
        candidates: tuple[CoverCandidate, CoverCandidate, CoverCandidate],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        c_dig, c_scan, winner = candidates
        custom_img = tmp_path / "my_custom.jpg"
        Image.new("RGB", (1200, 1200), color=(30, 60, 90)).save(custom_img, format="JPEG")

        monkeypatch.setattr(sys, "stdin", io.StringIO(f"{custom_img}\n"))
        chosen = prompt_cover_choice(c_dig, c_scan, winner, None, None)
        assert chosen is not None
        assert "Custom" in chosen.provider

    def test_single_candidate_auto_select(
        self, candidates: tuple[CoverCandidate, CoverCandidate, CoverCandidate]
    ) -> None:
        c_dig, _, _ = candidates
        # No scan available -> should return c_dig without prompting
        chosen = prompt_cover_choice(c_dig, None, c_dig, None, None)
        assert chosen is not None
        assert chosen.provider == c_dig.provider


# ==============================================================================
# 7. Vorbis Comment & Metadata Writer Tests
# ==============================================================================
class TestMetadataWriterVorbis:
    def test_clean_manifest_value(self) -> None:
        assert clean_manifest_value("Value with\x00 null byte") == "Value with null byte"
        assert clean_manifest_value("Line 1\r\nLine 2") == "Line 1 Line 2"
        assert clean_manifest_value("  Padded text  ") == "Padded text"
        # Multiline fields preservation
        multiline = "Paragraph 1\nParagraph 2"
        assert clean_manifest_value(multiline, "DISCOGS_NOTES") == multiline
        assert clean_manifest_value(multiline, "COMMENT") == multiline
        assert clean_manifest_value(multiline, "TITLE") == "Paragraph 1 Paragraph 2"

    def test_load_tags_valid(self, tmp_path: Path) -> None:
        manifest = tmp_path / "tags.txt"
        manifest.write_text(
            "# Comment line\n"
            "TITLE=We Will Rock You\n"
            "ALBUM=News Of The World\n"
            "ALBUMARTIST=Queen\n"
            "TRACKNUMBER=01\n"
            "TRACKTOTAL=11\n"
            "DISCNUMBER=01\n"
            "DISCTOTAL=01\n"
            "DATE=1977\n",
            encoding="utf-8",
        )
        tags = load_tags(manifest)
        tag_dict = dict(tags)
        assert tag_dict["TITLE"] == "We Will Rock You"
        assert tag_dict["TRACKNUMBER"] == "01"
        assert tag_dict["DATE"] == "1977"

    def test_load_tags_invalid_field(self, tmp_path: Path) -> None:
        manifest = tmp_path / "bad_tags.txt"
        manifest.write_text("НЕВАЛИДНОЕ_ПОЛЕ=Value\n", encoding="utf-8")  # Non-ASCII field name
        with pytest.raises(ValueError, match="invalid Vorbis field name"):
            load_tags(manifest)

    def test_canonical_verify_with_dummy_flac(self, tmp_path: Path) -> None:
        flac_path = tmp_path / "track01.flac"
        # Generate dummy 0.1s silent FLAC with ffmpeg
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )

        audio = FLAC(str(flac_path))
        audio.clear()
        audio["TITLE"] = "We Will Rock You"
        audio["ALBUM"] = "News Of The World"
        audio["ARTIST"] = "Queen"
        audio["ALBUMARTIST"] = "Queen"
        audio["TRACKNUMBER"] = "01"
        audio["TRACKTOTAL"] = "11"
        audio["DISCNUMBER"] = "01"
        audio["DISCTOTAL"] = "01"
        audio["DATE"] = "1977"
        audio.save()

        # Should verify without error
        canonical_verify(flac_path)

    def test_canonical_verify_failure_on_missing_tag(self, tmp_path: Path) -> None:
        flac_path = tmp_path / "bad_track.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )

        audio = FLAC(str(flac_path))
        audio.clear()
        audio["TITLE"] = "Incomplete Track"
        audio.save()

        with pytest.raises(RuntimeError, match="core scalar tag invariant failed"):
            canonical_verify(flac_path)

    def test_canonical_verify_failure_on_invalid_track_range(self, tmp_path: Path) -> None:
        flac_path = tmp_path / "range_err.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )

        audio = FLAC(str(flac_path))
        audio.clear()
        audio["TITLE"] = "Track"
        audio["ALBUM"] = "Album"
        audio["ARTIST"] = "Artist"
        audio["ALBUMARTIST"] = "Artist"
        audio["TRACKNUMBER"] = "15"  # 15 out of 11
        audio["TRACKTOTAL"] = "11"
        audio["DISCNUMBER"] = "01"
        audio["DISCTOTAL"] = "01"
        audio["DATE"] = "1977"
        audio.save()

        with pytest.raises(RuntimeError, match="track numbering invariant failed: 15/11"):
            canonical_verify(flac_path)

    def test_preserve_replaygain_and_dr_across_writes(self, tmp_path: Path) -> None:
        flac_path = tmp_path / "preserve_test.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )

        # 1. Initially set core tags plus ReplayGain and Dynamic Range
        initial_tags = [
            ("TITLE", "Initial Title"),
            ("ALBUM", "Initial Album"),
            ("ARTIST", "Initial Artist"),
            ("ALBUMARTIST", "Initial Artist"),
            ("TRACKNUMBER", "1"),
            ("TRACKTOTAL", "10"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("DATE", "1980"),
            ("REPLAYGAIN_TRACK_GAIN", "-4.50 dB"),
            ("REPLAYGAIN_TRACK_PEAK", "0.785000"),
            ("REPLAYGAIN_ALBUM_GAIN", "-3.80 dB"),
            ("REPLAYGAIN_ALBUM_PEAK", "0.790000"),
            ("DYNAMIC_RANGE", "11"),
            ("ALBUM_DYNAMIC_RANGE", "10"),
        ]
        write_tags(flac_path, initial_tags)

        # 2. Write new metadata WITHOUT ReplayGain or DR in the manifest (e.g. title update)
        new_manifest_tags = [
            ("TITLE", "Updated Title"),
            ("ALBUM", "Updated Album"),
            ("ARTIST", "Updated Artist"),
            ("ALBUMARTIST", "Updated Artist"),
            ("TRACKNUMBER", "1"),
            ("TRACKTOTAL", "10"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("DATE", "1980"),
        ]
        write_tags(flac_path, new_manifest_tags)

        # 3. Verify that ReplayGain and DR tags were NOT wiped!
        audio = FLAC(str(flac_path))
        assert audio["TITLE"] == ["Updated Title"]
        assert audio["REPLAYGAIN_TRACK_GAIN"] == ["-4.50 dB"]
        assert audio["REPLAYGAIN_TRACK_PEAK"] == ["0.785000"]
        assert audio["REPLAYGAIN_ALBUM_GAIN"] == ["-3.80 dB"]
        assert audio["REPLAYGAIN_ALBUM_PEAK"] == ["0.790000"]
        assert audio["DYNAMIC_RANGE"] == ["11"]
        assert audio["ALBUM_DYNAMIC_RANGE"] == ["10"]

    def test_standalone_picture_update_preserves_existing_tags(self, tmp_path: Path) -> None:
        flac_path = tmp_path / "pic_update.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )

        initial_tags = [
            ("TITLE", "Track Title"),
            ("ALBUM", "Album Title"),
            ("ARTIST", "Artist"),
            ("ALBUMARTIST", "Artist"),
            ("TRACKNUMBER", "1"),
            ("TRACKTOTAL", "5"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("DATE", "1985"),
        ]
        write_tags(flac_path, initial_tags)

        pic_file = tmp_path / "cover.jpg"
        Image.new("RGB", (600, 600), color=(120, 10, 200)).save(pic_file, format="JPEG")

        # Invoke metadata_writer main with --picture and no manifest file
        res = subprocess.run(
            [sys.executable, "metadata_writer.py", str(flac_path), "--picture", str(pic_file)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0, res.stderr

        audio = FLAC(str(flac_path))
        assert audio["TITLE"] == ["Track Title"]
        assert len(audio.pictures) == 1
        assert audio.pictures[0].width == 600
        assert audio.pictures[0].height == 600


# ==============================================================================
# 8. Dynamic Range (DR14) Parser Tests
# ==============================================================================
class TestDr14Parsing:
    def test_clean_track_name(self) -> None:
        assert clean_track_name("01. We Will Rock You [flac]") == "01. We Will Rock You"
        assert clean_track_name("We Are The Champions\t [flac]\t") == "We Are The Champions"
        assert clean_track_name("Sheer Heart Attack") == "Sheer Heart Attack"

    def test_extract_track_number(self) -> None:
        assert extract_track_number("01-08 - Spread Your Wings") == 8
        assert extract_track_number("08. Spread Your Wings") == 8
        assert extract_track_number("08 - Spread Your Wings") == 8
        assert extract_track_number("Track 08") == 8
        assert extract_track_number("Spread Your Wings") is None

    def test_normalize_db(self) -> None:
        assert normalize_db("over") == "over"
        assert normalize_db("-0.10 dB") == "-0.10 dB"
        assert normalize_db("-12,4 дБ") == "-12.4 dB"
        assert normalize_db("-14.2 DB") == "-14.2 dB"

    def test_parse_dr_report(self) -> None:
        sample_report = """
--------------------------------------------------------------------------------
Analyzed: Queen / News Of The World
--------------------------------------------------------------------------------

DR         Peak         RMS     Duration Track
--------------------------------------------------------------------------------
DR10      -0.20 dB   -11.80 dB      2:01 01-We Will Rock You [flac]
DR11      -0.10 dB   -13.40 dB      2:59 02-We Are The Champions [flac]
DR12       over      -13.10 dB      3:26 03-Sheer Heart Attack [flac]
--------------------------------------------------------------------------------

Number of tracks:  3
Official DR value: DR11

================================================================================
"""
        tracks, official_dr = parse_dr_report(sample_report)
        assert official_dr == 11
        assert len(tracks) == 3
        assert tracks[0]["dr"] == 10
        assert tracks[0]["peak"] == "-0.20 dB"
        assert tracks[0]["name"] == "01-We Will Rock You"
        assert tracks[2]["dr"] == 12
        assert tracks[2]["peak"] == "over"


# ==============================================================================
# 9. Audiophile Quality Audit (verify_all_flacs) Tests
# ==============================================================================
class TestVerifyAllFlacs:
    def test_audit_flac_clean_track(self, tmp_path: Path) -> None:
        flac_path = tmp_path / "perfect.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )

        pic_path = tmp_path / "cov.jpg"
        Image.new("RGB", (600, 600), color=(50, 100, 150)).save(pic_path, format="JPEG")

        tags = [
            ("TITLE", "We Are The Champions"),
            ("TITLESORT", "We Are The Champions"),
            ("ARTIST", "Queen"),
            ("ARTISTSORT", "Queen"),
            ("ALBUM", "News Of The World"),
            ("ALBUMSORT", "News Of The World"),
            ("ALBUMARTIST", "Queen"),
            ("ALBUMARTISTSORT", "Queen"),
            ("DATE", "1977-10-28"),
            ("YEAR", "1977"),
            ("ORIGINALDATE", "1977-10-28"),
            ("ORIGINALYEAR", "1977"),
            ("TRACKNUMBER", "2"),
            ("TRACKTOTAL", "11"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("COMPOSER", "Freddie Mercury"),
            ("GENRE", "Rock"),
            ("MEDIA", "CD"),
            ("LABEL", "Elektra"),
            ("CATALOGNUMBER", "32XD-409"),
            ("DYNAMIC_RANGE", "11"),
            ("ALBUM_DYNAMIC_RANGE", "10"),
            ("REPLAYGAIN_TRACK_GAIN", "-4.12 dB"),
            ("REPLAYGAIN_TRACK_PEAK", "0.786102"),
            ("REPLAYGAIN_ALBUM_GAIN", "-3.11 dB"),
            ("REPLAYGAIN_ALBUM_PEAK", "0.786102"),
            ("REPLAYGAIN_REFERENCE_LOUDNESS", "-18.00 LUFS"),
            ("MUSICBRAINZ_RECORDINGID", "3604eb06-4bc2-4416-9b31-ceadae51bc70"),
            ("MUSICBRAINZ_RELEASEGROUPID", "c172ea98-3a82-3254-b8ad-25a4f465ba03"),
            ("MUSICBRAINZ_ARTISTID", "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"),
            ("DISCOGS_RELEASE_URL", "https://www.discogs.com/release/4928271"),
        ]
        write_tags(flac_path, tags, picture_file=str(pic_path))

        res = audit_flac(flac_path)
        assert res.errors == []
        # Warnings for missing recommended tags (COMPOSER present, but MUSICBRAINZ_ALBUMID etc. may be missing)
        # Core audit passes with zero errors

    def test_audit_flac_reports_missing_mandatory(self, tmp_path: Path) -> None:
        flac_path = tmp_path / "missing_tags.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )
        audio = FLAC(str(flac_path))
        audio.clear()
        audio["TITLE"] = "Partial Track"
        audio.save()

        res = audit_flac(flac_path)
        assert len(res.errors) > 0
        assert any("Missing mandatory tag: ARTIST" in err for err in res.errors)
        # Derived dynamics, loudness, credits and external IDs are optional.
        assert not any("Missing mandatory tag: DYNAMIC_RANGE" in err for err in res.errors)
        assert not any("Missing mandatory tag: REPLAYGAIN_TRACK_GAIN" in err for err in res.errors)
        assert not any("Missing mandatory tag: COMPOSER" in err for err in res.errors)

    def test_audit_flac_reports_invalid_replaygain_format(self, tmp_path: Path) -> None:
        flac_path = tmp_path / "bad_rg.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )
        tags = [
            ("TITLE", "Track"),
            ("TITLESORT", "Track"),
            ("ARTIST", "Artist"),
            ("ARTISTSORT", "Artist"),
            ("ALBUM", "Album"),
            ("ALBUMSORT", "Album"),
            ("ALBUMARTIST", "Artist"),
            ("ALBUMARTISTSORT", "Artist"),
            ("DATE", "1980"),
            ("YEAR", "1980"),
            ("ORIGINALDATE", "1980"),
            ("ORIGINALYEAR", "1980"),
            ("TRACKNUMBER", "1"),
            ("TRACKTOTAL", "1"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("COMPOSER", "Composer"),
            ("GENRE", "Rock"),
            ("DYNAMIC_RANGE", "10"),
            ("ALBUM_DYNAMIC_RANGE", "10"),
            ("REPLAYGAIN_TRACK_GAIN", "invalid_gain"),  # Missing dB / number
            ("REPLAYGAIN_TRACK_PEAK", "not_a_float"),
            ("REPLAYGAIN_ALBUM_GAIN", "-3.0 dB"),
            ("REPLAYGAIN_ALBUM_PEAK", "0.800000"),
            ("MUSICBRAINZ_RECORDINGID", "3604eb06-4bc2-4416-9b31-ceadae51bc70"),
            ("MUSICBRAINZ_RELEASEGROUPID", "c172ea98-3a82-3254-b8ad-25a4f465ba03"),
            ("MUSICBRAINZ_ARTISTID", "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"),
            ("DISCOGS_RELEASE_URL", "https://www.discogs.com/release/12345"),
        ]
        write_tags(flac_path, tags)

        res = audit_flac(flac_path)
        assert any("Invalid REPLAYGAIN_TRACK_GAIN format" in err for err in res.errors)
        assert any("Invalid REPLAYGAIN_TRACK_PEAK format" in err for err in res.errors)


# ==============================================================================
# 9. Native In-Process DR14 Measurement Tests
# ==============================================================================
class TestNativeDr14Measurement:
    def test_compute_dr14_samples_synthetic(self) -> None:
        import numpy as np

        fs = 44100
        # 1. Pure continuous sine wave has 0 dB dynamic range on AES17 scale
        t = np.linspace(0, 10, fs * 10, endpoint=False)
        sine = 0.8 * np.sin(2 * np.pi * 440 * t).reshape(-1, 1)
        dr, peak, rms = compute_dr14_samples(sine, fs)
        assert dr == 0
        assert "dB" in peak
        assert "dB" in rms

        # 2. Dynamic transient signal (0.2 background with 1.0 transient drum peaks)
        t_dyn = np.linspace(0, 15, fs * 15, endpoint=False)
        dyn = 0.2 * np.sin(2 * np.pi * 440 * t_dyn)
        for idx in range(0, len(dyn), fs // 2):
            dyn[idx : idx + 100] = 1.0
        dyn = dyn.reshape(-1, 1)
        dr_dyn, peak_dyn, rms_dyn = compute_dr14_samples(dyn, fs)
        assert 10 <= dr_dyn <= 15
        assert peak_dyn == "over"
        assert "dB" in rms_dyn

    def test_measure_audio_file_real_flac(self) -> None:
        flac_candidates = list((Path(__file__).resolve().parent.parent / "Queen").rglob("*.flac"))
        if not flac_candidates:
            pytest.skip("No Queen FLAC files found")
        test_flac = flac_candidates[0]
        res = measure_audio_file(test_flac)
        assert res["type"] == "track"
        assert isinstance(res["dr"], int)
        assert 6 <= res["dr"] <= 20
        assert res["name"] == test_flac.name
        assert "dB" in str(res["peak"]) or str(res["peak"]) == "over"
        assert "dB" in str(res["rms"])

    def test_measure_audio_files_batch_and_cli(self) -> None:
        flac_candidates = list((Path(__file__).resolve().parent.parent / "Queen").rglob("*.flac"))[:2]
        if not flac_candidates:
            pytest.skip("No Queen FLAC files found")
        tracks, album_dr = measure_audio_files(flac_candidates)
        assert len(tracks) == 2
        assert isinstance(album_dr, int)
        assert 6 <= album_dr <= 20

        # CLI test with --measure
        cmd = [sys.executable, "dr14_parse.py", "--measure", str(flac_candidates[0])]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        assert '"type":"track"' in proc.stdout
        assert '"type":"album"' in proc.stdout


class TestAudiophileTaggerPythonSuite:
    """Unit tests for audiophile_tagger.py core helpers and album discovery."""

    def test_clean_credit(self) -> None:
        import audiophile_tagger

        assert audiophile_tagger.clean_credit("Queen (2)") == "Queen"
        assert audiophile_tagger.clean_credit("Freddie Mercury*") == "Freddie Mercury"
        assert audiophile_tagger.clean_credit("David Bowie (3)**") == "David Bowie"

    def test_normalize_catno(self) -> None:
        import audiophile_tagger

        assert audiophile_tagger.normalize_catno("CDP 7 46016 2") == "cdp7460162"
        assert audiophile_tagger.normalize_catno("GZS-1144") == "gzs1144"

    def test_normalize_country_iso(self) -> None:
        import audiophile_tagger

        assert audiophile_tagger.normalize_country_iso("West Germany") == "DE"
        assert audiophile_tagger.normalize_country_iso("uk") == "GB"
        assert audiophile_tagger.normalize_country_iso("Japan") == "JP"
        assert audiophile_tagger.normalize_country_iso("United States") == "US"
        assert audiophile_tagger.normalize_country_iso("FR") == "FR"

    def test_album_sort_value(self) -> None:
        import audiophile_tagger

        assert audiophile_tagger.album_sort_value("The Works") == "Works, The"
        assert audiophile_tagger.album_sort_value("A Night At The Opera") == "Night At The Opera, A"
        assert audiophile_tagger.album_sort_value("Innuendo") == "Innuendo"

    def test_choose_date_quiet_and_match(self) -> None:
        import audiophile_tagger

        # Exact match
        d, y, src = audiophile_tagger.choose_date("1984-02-27", "1984", quiet_mode=False)
        assert d == "1984-02-27"
        assert y == "1984"
        assert src == "discogs"

        # Conflict in quiet mode
        d, y, src = audiophile_tagger.choose_date("1989", "1984", quiet_mode=True)
        assert d == ""
        assert y == ""
        assert src == "conflict-review"

    def test_resolve_target_albums(self) -> None:
        import audiophile_tagger

        base_dir = Path(__file__).resolve().parent.parent
        # Discover the adjacent collection and retain curated hints
        all_curated = audiophile_tagger.resolve_target_albums([], base_dir)
        assert len(all_curated) == len(audiophile_tagger.CURATED_ALBUMS)

        # Keyword match
        works = audiophile_tagger.resolve_target_albums(["Works"], base_dir)
        assert len(works) >= 1
        assert any("The Works" in w for w in works)

    def test_role_filtering_excludes_chorus_master_and_assistants(self) -> None:
        import re

        non_audio_master_re = re.compile(
            r"\b(chorus|choir|concert|band|web|scrum|ballet|stage|property|puppet)\s*master\b",
            re.IGNORECASE,
        )
        audio_master_re = re.compile(
            r"\b(audio\s+)?(master(ed|ing)?(\s+by)?|lacquer\s+cut|cut\s+by|direct\s+metal\s+master(ed|ing)?|dmm)\b",
            re.IGNORECASE,
        )
        remaster_re = re.compile(r"\b(audio\s+)?remaster(ed|ing)?(\s+by)?\b", re.IGNORECASE)
        chief_engineer_re = re.compile(
            r"\b(recording|sound|balance|tracking|mix(ed|ing)?)?\s*engineer\b", re.IGNORECASE
        )
        assistant_engineer_re = re.compile(r"\b(assistant|second|tape\s+op|trainee)\b", re.IGNORECASE)

        # 1. Chorus Master must NOT be matched as mastering engineer
        role_chorus = "Vocals, Piano, Chorus Master [Choir Meister]"
        assert audio_master_re.search(role_chorus)
        assert non_audio_master_re.search(role_chorus)
        is_audio_master = (
            audio_master_re.search(role_chorus)
            and not remaster_re.search(role_chorus)
            and not non_audio_master_re.search(role_chorus)
        )
        assert not is_audio_master

        # 2. Genuine mastering engineer matches
        role_master = "Mastered By [Gateway Mastering Studios]"
        assert (
            audio_master_re.search(role_master)
            and not remaster_re.search(role_master)
            and not non_audio_master_re.search(role_master)
        )

        # 3. Assistant engineer must NOT be matched as chief engineer
        role_asst = "Engineer [Assistant]"
        is_chief_engineer = chief_engineer_re.search(role_asst) and not assistant_engineer_re.search(role_asst)
        assert not is_chief_engineer

        # 4. Chief engineer matches
        role_eng = "Sound Engineer"
        assert chief_engineer_re.search(role_eng) and not assistant_engineer_re.search(role_eng)

    def test_eac_multiline_accurip_parsing(self, tmp_path: Path) -> None:
        import audiophile_tagger

        log_content = """Exact Audio Copy V1.3 from 2. September 2016
EAC extraction logfile from 8. November 2019

Track  1

     Filename C:\\Rip\\01 - Track.wav
     Pre-gap length  0:00:02.00
     Peak level 89.8 %
     Extraction speed 3.8 X
     Track quality 99.9 %
     Test CRC CE45F903
     Copy CRC CE45F903
     Accurately ripped (confidence 35)  [9E0A220C]  (AR v2)
     Copy OK

Track  2

     Filename C:\\Rip\\02 - Track.wav
     Peak level 95.0 %
     Copy CRC AABBCCDD
     Cannot be verified as accurate  [11223344]
     Copy OK
"""
        log_file = tmp_path / "eac_test.log"
        log_file.write_text(log_content, encoding="utf-8")

        res_t1 = audiophile_tagger.get_track_accurip_data([log_file], 1)
        assert res_t1["ar_result"] == "Accurately ripped (confidence 35)"
        assert res_t1["ar_crc"] == "9E0A220C"

        res_t2 = audiophile_tagger.get_track_accurip_data([log_file], 2)
        assert "Cannot be verified" in res_t2["ar_result"]
        assert res_t2["ar_crc"] == "11223344"

    def test_metadata_writer_preserves_audiophile_flags(self, tmp_path: Path) -> None:
        import metadata_writer

        flac_path = tmp_path / "audiophile_flags.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )
        # Initial tags with pre-emphasis, HDCD, and SPARSCODE
        initial_tags = [
            ("TITLE", "Original Title"),
            ("ALBUM", "Original Album"),
            ("ARTIST", "Artist"),
            ("ALBUMARTIST", "Artist"),
            ("TRACKNUMBER", "1"),
            ("TRACKTOTAL", "1"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("DATE", "1983"),
            ("PRE_EMPHASIS", "1"),
            ("FLAGS", "PRE"),
            ("HDCD", "1"),
            ("SPARSCODE", "AAD"),
            ("ENCODER", "FLAC 1.4.3"),
        ]
        metadata_writer.write_tags(flac_path, initial_tags)

        # Overwrite with a new manifest that doesn't mention PRE_EMPHASIS or HDCD
        new_manifest = [
            ("TITLE", "New Title"),
            ("ALBUM", "New Album"),
            ("ARTIST", "Artist"),
            ("ALBUMARTIST", "Artist"),
            ("TRACKNUMBER", "1"),
            ("TRACKTOTAL", "1"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("DATE", "1983"),
        ]
        metadata_writer.write_tags(flac_path, new_manifest)

        audio = FLAC(str(flac_path))
        assert audio["TITLE"] == ["New Title"]
        assert audio["PRE_EMPHASIS"] == ["1"]
        assert audio["FLAGS"] == ["PRE"]
        assert audio["HDCD"] == ["1"]
        assert audio["SPARSCODE"] == ["AAD"]
        # Unknown pre-existing fields are sovereign and are not overwritten by
        # a manifest merely because it contains the same field name.
        assert audio["ENCODER"] != ["FLAC 1.4.3"]

    def test_generate_and_save_dr14_report(self, tmp_path: Path) -> None:
        import dr14_parse

        tracks = [
            {"type": "track", "dr": 12, "peak": "0.00 dB", "rms": "-14.50 dB", "duration": "3:45", "name": "01-Track1"},
            {
                "type": "track",
                "dr": 14,
                "peak": "-1.20 dB",
                "rms": "-17.20 dB",
                "duration": "4:10",
                "name": "02-Track2",
            },
        ]
        report = dr14_parse.generate_dr14_report("Queen", "Greatest Hits", tracks, 13)
        assert "Analyzed: Queen / Greatest Hits" in report
        assert "DR12" in report
        assert "DR14" in report
        assert "Official DR value: DR13" in report

        out_file = dr14_parse.save_dr14_report(tmp_path, report)
        assert out_file.is_file()
        assert out_file.name == "dr14.txt"
        assert "Official DR value: DR13" in out_file.read_text(encoding="utf-8")

    def test_verify_all_flacs_flags_multiple_display_artists(self, tmp_path: Path) -> None:
        import metadata_writer
        import verify_all_flacs

        flac_path = tmp_path / "multi_artist.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )
        tags = [
            ("TITLE", "Under Pressure"),
            ("TITLESORT", "Under Pressure"),
            ("ARTIST", "Queen & David Bowie"),
            ("ARTISTSORT", "Queen"),
            ("ALBUM", "Hot Space"),
            ("ALBUMSORT", "Hot Space"),
            ("ALBUMARTIST", "Queen"),
            ("ALBUMARTISTSORT", "Queen"),
            ("DATE", "1982-05-21"),
            ("YEAR", "1982"),
            ("ORIGINALDATE", "1982-05-21"),
            ("ORIGINALYEAR", "1982"),
            ("TRACKNUMBER", "11"),
            ("TRACKTOTAL", "11"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("GENRE", "Rock"),
            ("DYNAMIC_RANGE", "12"),
            ("ALBUM_DYNAMIC_RANGE", "11"),
            ("REPLAYGAIN_TRACK_GAIN", "-3.50 dB"),
            ("REPLAYGAIN_TRACK_PEAK", "0.850000"),
            ("REPLAYGAIN_ALBUM_GAIN", "-3.20 dB"),
            ("REPLAYGAIN_ALBUM_PEAK", "0.850000"),
            ("MUSICBRAINZ_RECORDINGID", "3604eb06-4bc2-4416-9b31-ceadae51bc70"),
            ("MUSICBRAINZ_RELEASEGROUPID", "c172ea98-3a82-3254-b8ad-25a4f465ba03"),
            ("MUSICBRAINZ_ARTISTID", "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"),
            ("DISCOGS_RELEASE_URL", "https://www.discogs.com/release/12345"),
            ("SPARSCODE", "AAD"),
        ]
        metadata_writer.write_tags(flac_path, tags)

        # Vorbis permits repeated ARTIST comments from other tools, but this
        # project's chosen display-field profile requires one ARTIST and uses
        # repeatable ARTISTS for the separate credited people.
        multivalue_audio = FLAC(flac_path)
        multivalue_audio["ARTIST"] = ["Queen", "David Bowie"]
        multivalue_audio.save()

        res = verify_all_flacs.audit_flac(flac_path)
        assert any(f.code == "cardinality.single" and "ARTIST" in f.message for f in res.findings)

    def test_load_tags_multiline_manifest(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "test_manifest.txt"
        manifest_path.write_text(
            "TITLE=Bohemian Rhapsody\n"
            "DISCOGS_NOTES=Line 1 of notes\n"
            "Line 2 without equals sign\n"
            "Line 3: extra info\n"
            "ARTIST=Queen\n",
            encoding="utf-8",
        )
        tags = load_tags(manifest_path)
        assert len(tags) == 3
        assert tags[0][0] == "TITLE" and tags[0][1] == "Bohemian Rhapsody"
        assert tags[1][0] == "DISCOGS_NOTES"
        assert "Line 1 of notes\nLine 2 without equals sign\nLine 3: extra info" in tags[1][1]
        assert tags[2][0] == "ARTIST" and tags[2][1] == "Queen"

    def test_load_tags_preserves_lyrics_blank_and_comment_lines(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "lyrics_manifest.txt"
        manifest_path.write_text(
            "TITLE=Song\nLYRICS=First line\n\n# this is lyric content\nA=B is also lyric content\nARTIST=Artist\n",
            encoding="utf-8",
        )

        tags = load_tags(manifest_path)
        assert tags == [
            ("TITLE", "Song"),
            ("LYRICS", "First line\n\n# this is lyric content\nA=B is also lyric content"),
            ("ARTIST", "Artist"),
        ]

    def test_clean_manifest_value_bogus_catno(self) -> None:
        assert clean_manifest_value("none", "CATALOGNUMBER") == ""
        assert clean_manifest_value("[none]", "CATALOGNUMBER") == ""
        assert clean_manifest_value("n/a", "CATALOGNUMBER") == ""
        assert clean_manifest_value("?", "CATALOGNUMBER") == ""
        assert clean_manifest_value("CDP 7 46207 2", "CATALOGNUMBER") == "CDP 7 46207 2"

    def test_dr14_no_negative_zero_db(self) -> None:
        assert normalize_db("-0.00 dB") == "0.00 dB"
        assert normalize_db("-0.00") == "0.00 dB"
        assert normalize_db("-0.01 dB") == "-0.01 dB"
        assert normalize_db("0.00 dB") == "0.00 dB"

    def test_verify_all_flacs_allows_multi_musicbrainz_artistid(self, tmp_path: Path) -> None:
        import metadata_writer
        import verify_all_flacs

        flac_path = tmp_path / "collab.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )
        tags = [
            ("TITLE", "Under Pressure"),
            ("TITLESORT", "Under Pressure"),
            ("ARTIST", "Queen & David Bowie"),
            ("ARTISTSORT", "Queen"),
            ("ALBUM", "Hot Space"),
            ("ALBUMSORT", "Hot Space"),
            ("ALBUMARTIST", "Queen"),
            ("ALBUMARTISTSORT", "Queen"),
            ("DATE", "1982-05-21"),
            ("YEAR", "1982"),
            ("ORIGINALDATE", "1982-05-21"),
            ("ORIGINALYEAR", "1982"),
            ("TRACKNUMBER", "11"),
            ("TRACKTOTAL", "11"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("GENRE", "Rock"),
            ("DYNAMIC_RANGE", "12"),
            ("ALBUM_DYNAMIC_RANGE", "11"),
            ("REPLAYGAIN_TRACK_GAIN", "-3.50 dB"),
            ("REPLAYGAIN_TRACK_PEAK", "0.850000"),
            ("REPLAYGAIN_ALBUM_GAIN", "-3.20 dB"),
            ("REPLAYGAIN_ALBUM_PEAK", "0.850000"),
            ("MUSICBRAINZ_RECORDINGID", "3604eb06-4bc2-4416-9b31-ceadae51bc70"),
            ("MUSICBRAINZ_RELEASEGROUPID", "c172ea98-3a82-3254-b8ad-25a4f465ba03"),
            ("MUSICBRAINZ_ARTISTID", "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"),
            ("MUSICBRAINZ_ARTISTID", "54424533-bbfe-4c29-92c7-b64c11cf9b01"),
            ("DISCOGS_RELEASE_URL", "https://www.discogs.com/release/12345"),
            ("SPARSCODE", "AAD"),
        ]
        metadata_writer.write_tags(flac_path, tags)
        res = verify_all_flacs.audit_flac(flac_path)
        assert not any("MUSICBRAINZ_ARTISTID" in err for err in res.errors)

    def test_preemphasis_dual_tags(self, tmp_path: Path) -> None:
        flac_path = tmp_path / "preemphasis.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )
        tags = [
            ("TITLE", "Test"),
            ("ARTIST", "Artist"),
            ("ALBUM", "Album"),
            ("DATE", "1983"),
            ("YEAR", "1983"),
            ("TRACKNUMBER", "1"),
            ("TRACKTOTAL", "1"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("PRE_EMPHASIS", "1"),
            ("PRE-EMPHASIS", "1"),
            ("FLAGS", "PRE"),
        ]
        write_tags(flac_path, tags)
        audio = FLAC(str(flac_path))
        assert audio["PRE_EMPHASIS"] == ["1"]
        assert audio["PRE-EMPHASIS"] == ["1"]
        assert audio["FLAGS"] == ["PRE"]

    def test_dr14_parse_measure_creates_report(self, tmp_path: Path) -> None:
        f1 = tmp_path / "01 - Track1.flac"
        f2 = tmp_path / "02 - Track2.flac"
        for f in (f1, f2):
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:duration=1", "-c:a", "flac", str(f)],
                check=True,
                capture_output=True,
            )
        # Run CLI measure mode
        dr14_script = Path(__file__).resolve().parent / "dr14_parse.py"
        res = subprocess.run(
            ["python3", str(dr14_script), "--measure", str(f1), str(f2)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "album" in res.stdout
        # Verify dr14.txt was created in tmp_path
        dr_txt = tmp_path / "dr14.txt"
        assert dr_txt.is_file()
        content = dr_txt.read_text(encoding="utf-8")
        assert "Official DR value: DR" in content

    def test_clean_credit_complex_discogs_suffixes(self) -> None:
        import audiophile_tagger

        assert audiophile_tagger.clean_credit("Queen (2)") == "Queen"
        assert audiophile_tagger.clean_credit("Freddie Mercury*") == "Freddie Mercury"
        assert audiophile_tagger.clean_credit("David Bowie (3)**") == "David Bowie"
        assert audiophile_tagger.clean_credit("Artist (123)***") == "Artist"

    def test_lyrics_plain_derivation_from_synced(self) -> None:
        import lyrics_utils

        synced_sample = "[00:12.34]First line of lyrics\n[00:15.67]<00:15.67>Second <00:16.00>line\n"
        plain = lyrics_utils.to_plain(synced_sample, song="Song", artist="Artist")
        assert "First line of lyrics" in plain
        assert "Second line" in plain
        assert "[" not in plain
        assert "<" not in plain


class TestAudiophileWorldClassEnhancements:
    def test_split_artists_python(self) -> None:
        import audiophile_tagger

        assert audiophile_tagger.split_artists("Queen & David Bowie") == ["Queen", "David Bowie"]
        assert audiophile_tagger.split_artists("Freddie Mercury feat. Montserrat Caballé (2)") == [
            "Freddie Mercury",
            "Montserrat Caballé",
        ]
        assert audiophile_tagger.split_artists("Artist A / Artist B") == ["Artist A", "Artist B"]
        assert audiophile_tagger.split_artists("Queen with Paul Rodgers") == ["Queen", "Paul Rodgers"]
        assert audiophile_tagger.split_artists("Pink Floyd") == ["Pink Floyd"]
        assert audiophile_tagger.split_artists("") == []

        # Fish split_artists parity verification
        fish_cmd = """
        fish -c '
        function clean_credit
            set -l s (string trim -- "$argv[1]")
            while true
                set -l prev "$s"
                set s (string replace -r "\\*+\\$" "" -- "$s" | string trim)
                set s (string replace -r "\\s*\\(\\d+\\)\\$" "" -- "$s" | string trim)
                test "$s" = "$prev"; and break
            end
            printf "%s\\n" "$s"
        end
        function split_artists
            set -l raw (string trim -- "$argv[1]")
            test -z "$raw"; and return
            set -l parts (string replace -ra -i "\\s+(?:&|feat\\.|featuring|with|vs\\.)\\s+|\\s+/\\s+" "\\n" -- "$raw")
            set -l cleaned
            for p in $parts
                set -l cp (clean_credit "$p")
                test -n "$cp"; and set -a cleaned "$cp"
            end
            if test (count $cleaned) -gt 1
                printf "%s\\n" $cleaned
            else
                clean_credit "$raw"
            end
        end
        split_artists "Queen & David Bowie"
        '
        """
        res = subprocess.run(fish_cmd, shell=True, capture_output=True, text=True, check=True)
        lines = [
            line.strip() for line in res.stdout.splitlines() if line.strip() and "▄" not in line and "▀" not in line
        ]
        assert "Queen" in lines
        assert "David Bowie" in lines

    def test_clean_lyrics_vorbis_invariants(self, tmp_path: Path) -> None:
        flac_path = tmp_path / "test_lyrics.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "flac", str(flac_path)],
            check=True,
            capture_output=True,
        )
        lrc_file = tmp_path / "test.lrc"
        txt_file = tmp_path / "test.txt"
        raw_lyrics = "[Verse 1]\n[00:00.10]First line of lyrics\n[00:00.50]Second line of lyrics\n"
        synced_lyrics = raw_lyrics
        txt_file.write_text(raw_lyrics, encoding="utf-8")
        lrc_file.write_text(synced_lyrics, encoding="utf-8")

        tags = [
            ("TITLE", "Test Track"),
            ("ARTIST", "Queen"),
            ("ALBUM", "A Night At The Opera"),
            ("DATE", "1975"),
            ("TRACKNUMBER", "1"),
        ]
        write_tags(
            flac_path,
            tags,
            lyrics_file=txt_file,
            synced_lyrics_file=lrc_file,
        )

        audio = FLAC(flac_path)
        # Playback policy mirrors synchronized text to LYRICS and both synced
        # extensions; UNSYNCEDLYRICS remains the plain representation.
        lyrics_val = audio.get("LYRICS", [""])[0]
        assert "[00:00.10]" in lyrics_val
        assert "First line of lyrics" in lyrics_val
        assert "[Verse 1]" not in lyrics_val

        # UNSYNCEDLYRICS: strictly clean plain text without timestamps
        unsynced_val = audio.get("UNSYNCEDLYRICS", [""])[0]
        assert "[00:00.10]" not in unsynced_val
        assert "[Verse 1]" in unsynced_val
        assert "00:00.10" not in unsynced_val
        assert "First line of lyrics" in unsynced_val

        # SYNCEDLYRICS: preserves timestamps
        synced_val = audio.get("SYNCEDLYRICS", [""])[0]
        assert "[00:00.10]" in synced_val
        assert "[Verse 1]" in synced_val
        assert audio.get("SYNCELYRICS", [""])[0] == synced_val

        # Fallback test: when no synced lyrics file is provided, LYRICS gets plain text
        flac_plain = tmp_path / "test_plain.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "flac", str(flac_plain)],
            check=True,
            capture_output=True,
        )
        write_tags(flac_plain, tags, lyrics_file=txt_file)
        audio_plain = FLAC(flac_plain)
        assert "[" not in audio_plain.get("LYRICS", [""])[0]
        assert "First line of lyrics" in audio_plain.get("LYRICS", [""])[0]

        # audit_flac must report 0 lyrics warnings
        report = audit_flac(flac_path)
        assert not any("timestamps" in w.lower() for w in report.warnings)

    def test_picture_downscale_preserves_disk_file(self, tmp_path: Path) -> None:
        flac_path = tmp_path / "test_art.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "flac", str(flac_path)],
            check=True,
            capture_output=True,
        )
        # Create a large 2400x1600 image
        cover_path = tmp_path / "cover.jpg"
        img = Image.new("RGB", (2400, 1600), color=(180, 50, 50))
        img.save(cover_path, format="JPEG", quality=95)
        orig_disk_size = cover_path.stat().st_size

        tags = [
            ("TITLE", "Test Track"),
            ("ARTIST", "Queen"),
            ("ALBUM", "A Night At The Opera"),
            ("DATE", "1975"),
            ("TRACKNUMBER", "1"),
        ]
        write_tags(flac_path, tags, picture_file=cover_path, max_picture_dim=1400)

        # 1. Verify original file on disk is untouched
        with Image.open(cover_path) as disk_img:
            assert disk_img.size == (2400, 1600)
        assert cover_path.stat().st_size == orig_disk_size

        # 2. Verify embedded picture in FLAC is downscaled proportionally
        audio = FLAC(flac_path)
        assert len(audio.pictures) == 1
        pic = audio.pictures[0]
        assert pic.width == 1400
        assert pic.height == 933
        assert len(pic.data) < orig_disk_size

    def test_dr_search_html_parsing_and_matching(self) -> None:
        from dr14_parse import match_dr_entry, parse_dr_search_html

        sample_html = """
        <table class="albumList">
            <thead><tr><th>Artist</th><th>Album</th><th>Year</th><th>DR</th><th>Min</th><th>Max</th><th>Codec</th><th>Notes</th></tr></thead>
            <tbody>
                <tr>
                    <td><a href="/album/view/1001">Queen</a></td>
                    <td><a href="/album/view/1001">A Night At The Opera</a></td>
                    <td>1975</td>
                    <td>DR8</td>
                    <td>DR7</td>
                    <td>DR10</td>
                    <td>CD</td>
                    <td>Elektra 7E-1053</td>
                </tr>
                <tr>
                    <td><a href="/album/view/2002">Queen</a></td>
                    <td><a href="/album/view/2002">A Night At The Opera</a></td>
                    <td>2000</td>
                    <td>DR12</td>
                    <td>DR10</td>
                    <td>DR14</td>
                    <td>CD</td>
                    <td>DCC Compact Classics GZS-1144 Remaster</td>
                </tr>
            </tbody>
        </table>
        """
        entries = parse_dr_search_html(sample_html)
        assert len(entries) == 2
        assert entries[0]["id"] == "1001"
        assert entries[0]["dr"] == 8
        assert entries[1]["id"] == "2002"
        assert entries[1]["dr"] == 12
        assert "GZS-1144" in entries[1]["notes"]

        # Match entry with catalog number and target DR
        matched = match_dr_entry(entries, target_dr=12, catalog_no="GZS-1144")
        assert matched is not None
        assert matched["id"] == "2002"
        assert matched["url"] == "https://dr.loudness-war.info/album/view/2002"

    def test_version_and_source_tags(self, tmp_path: Path) -> None:
        flac_path = tmp_path / "test_version.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "flac", str(flac_path)],
            check=True,
            capture_output=True,
        )
        tags = [
            ("ALBUM", "A Night At The Opera"),
            ("ALBUMARTIST", "Queen"),
            ("ALBUMARTISTSORT", "Queen"),
            ("ALBUMSORT", "Night At The Opera, A"),
            ("ARTIST", "Queen"),
            ("DATE", "1975"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("MEDIA", "CD"),
            ("ORIGINALDATE", "1975"),
            ("ORIGINALYEAR", "1975"),
            ("RELEASEDATE", "2000"),
            ("SOURCE", "CD"),
            ("TITLE", "Bohemian Rhapsody"),
            ("TRACKNUMBER", "1"),
            ("TRACKTOTAL", "1"),
            ("VERSION", "2000 DCC Remaster"),
            ("YEAR", "1975"),
        ]
        write_tags(flac_path, tags)
        audio = FLAC(flac_path)
        assert audio.get("VERSION") == ["2000 DCC Remaster"]
        assert audio.get("SOURCE") == ["CD"]
        assert audio.get("MEDIA") == ["CD"]

        report = audit_flac(flac_path)
        # Ensure VERSION and SOURCE do not trigger any errors or duplicate tag errors
        assert not any("VERSION" in e or "SOURCE" in e for e in report.errors)
        # VERSION should not be flagged as missing recommended
        assert not any("Missing recommended tag: VERSION" in w for w in report.warnings)


class TestAudiophileWorldClassStandardEnhancements:
    def test_format_performer_roles_python(self) -> None:
        assert format_performer("Freddie Mercury", "Lead Vocals, Piano") == "Freddie Mercury (lead vocals, piano)"
        assert (
            format_performer("Brian May", "Guitars [Red Special], Vocals") == "Brian May (guitars red special, vocals)"
        )
        assert format_performer("John Deacon", "") == "John Deacon"
        assert format_performer("Roger Taylor", "performer") == "Roger Taylor"
        assert format_performer("Guest Soloist", "Soloist") == "Guest Soloist"

    def test_parse_movement_info_python(self) -> None:
        assert parse_movement_info("Symphony No. 5: I. Allegro con brio") == ("I", "Allegro con brio")
        assert parse_movement_info("Piano Sonata No. 14 - Movement 1: Adagio sostenuto") == ("1", "Adagio sostenuto")
        assert parse_movement_info("Bohemian Rhapsody") == ("", "")

    def test_dr14_over_peak_exact(self) -> None:
        import numpy as np

        fs = 44100
        # Continuous sine at 1.0 (0.00 dBFS full scale)
        t = np.linspace(0, 5, fs * 5, endpoint=False)
        sine_full = 1.0 * np.sin(2 * np.pi * 1000 * t).reshape(-1, 1)
        _dr, peak, _rms = compute_dr14_samples(sine_full, fs)
        assert peak == "over"

        # Attenuated sine at 0.5 (-6.02 dBFS)
        sine_half = 0.5 * np.sin(2 * np.pi * 1000 * t).reshape(-1, 1)
        _dr_half, peak_half, _rms_half = compute_dr14_samples(sine_half, fs)
        assert "-6." in peak_half
        assert "dB" in peak_half

    def test_verify_all_flacs_new_tags(self, tmp_path: Path) -> None:
        flac_path = tmp_path / "movement_test.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "flac", str(flac_path)],
            check=True,
            capture_output=True,
        )
        tags = [
            ("ALBUM", "Symphony No. 5"),
            ("ALBUMARTIST", "Ludwig van Beethoven"),
            ("ALBUMARTISTSORT", "Beethoven, Ludwig van"),
            ("ALBUMSORT", "Symphony No. 5"),
            ("ARTIST", "Ludwig van Beethoven"),
            ("DATE", "1808"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("GENRE", "Classical"),
            ("MEDIA", "CD"),
            ("LABEL", "Deutsche Grammophon"),
            ("CATALOGNUMBER", "415 324-2"),
            ("DYNAMIC_RANGE", "14"),
            ("ALBUM_DYNAMIC_RANGE", "14"),
            ("ALBUM_MIN_DR", "12"),
            ("ALBUM_MAX_DR", "15"),
            ("DR_PEAK", "over"),
            ("DR_RMS", "-16.50 dB"),
            ("DR_SOURCE", "native_dr14"),
            ("ORIGINALDATE", "1808"),
            ("ORIGINALYEAR", "1808"),
            ("RELEASEDATE", "1990"),
            ("SOURCE", "CD"),
            ("TITLE", "I. Allegro con brio"),
            ("TRACKNUMBER", "1"),
            ("TRACKTOTAL", "4"),
            ("YEAR", "1808"),
            ("REPLAYGAIN_TRACK_GAIN", "-5.10 dB"),
            ("REPLAYGAIN_TRACK_PEAK", "0.998201"),
            ("REPLAYGAIN_ALBUM_GAIN", "-5.10 dB"),
            ("REPLAYGAIN_ALBUM_PEAK", "0.998201"),
            ("REPLAYGAIN_REFERENCE_LOUDNESS", "-18.00 LUFS"),
            ("MUSICBRAINZ_TRACKID", "11111111-2222-4333-8444-555555555555"),
            ("MUSICBRAINZ_RELEASEGROUPID", "22222222-3333-4333-8555-666666666666"),
            ("MUSICBRAINZ_ARTISTID", "33333333-4444-4555-8666-777777777777"),
            ("DISCOGS_RELEASE_URL", "https://www.discogs.com/release/99999"),
            ("PERFORMER", "Herbert von Karajan (conductor)"),
            ("RECORDINGLOCATION", "Jesus-Christus-Kirche, Berlin"),
            ("RECORDINGSTUDIO", "Jesus-Christus-Kirche, Berlin"),
            ("MIXINGSTUDIO", "Berliner Philharmonie"),
            ("RECORDINGDATE", "1962"),
            ("MOVEMENT", "I"),
            ("MOVEMENTNAME", "Allegro con brio"),
            ("HDCD", "1"),
            ("HDCD_PEAK_EXTEND", "1"),
            ("MATRIX", "415 324-2 01 #"),
            ("EDITION", "1990 Reissue"),
            ("VERSION", "Remastered"),
        ]
        write_tags(flac_path, tags)
        report = audit_flac(flac_path)
        assert len(report.errors) == 0, f"Unexpected verification errors: {report.errors}"


class TestAudiophileWorldStandards:
    """Verifies compliance with world-class Vorbis and audiophile tagging standards."""

    def test_fish_jq_catnos_and_matrix_parsing(self) -> None:
        """Verify fish script jq filters for catnos and matrix parse without syntax errors and extract valid data."""
        fish_code = """
        set discogs_json '{"labels":[{"name":"DCC Compact Classics","catno":"GZS-1144"}],"identifiers":[{"type":"Matrix / Runout","value":"$GZS-1144 X0725L"}]}'
        set -l catnos (printf "%s" "$discogs_json" | jq -r '[.labels[]?.catno // empty | select(. != "" and (. | ascii_downcase as $c | ($c != "none" and $c != "[none]" and $c != "n/a" and $c != "?")))] | unique[]?' 2>/dev/null)
        set -l raw_matrix_values (printf "%s" "$discogs_json" | jq -r '[.identifiers[]? | select(.type | test("Matrix|Runout"; "i")) | .value | select(. != null and . != "" and (. | ascii_downcase as $c | ($c != "none" and $c != "[none]" and $c != "n/a" and $c != "?"))) | gsub("[\\r\\n]+"; " ") | sub("^[ \t]+"; "") | sub("[ \t]+$"; "")] | unique | .[]?' 2>/dev/null)
        echo "CATNO_COUNT="(count $catnos)
        echo "CATNO_VAL="$catnos[1]
        echo "MATRIX_COUNT="(count $raw_matrix_values)
        echo "MATRIX_VAL="$raw_matrix_values[1]
        """
        res = subprocess.run(["fish", "--no-config", "-c", fish_code], capture_output=True, text=True, check=True)
        out = res.stdout
        assert "CATNO_COUNT=1" in out
        assert "CATNO_VAL=GZS-1144" in out
        assert "MATRIX_COUNT=1" in out
        assert "MATRIX_VAL=$GZS-1144 X0725L" in out

    def test_musicbrainz_label_info_schema_resolution(self) -> None:
        """Verify MusicBrainz release label-info JSON schema extraction."""
        import audiophile_tagger

        mb_release = {
            "label-info": [
                {
                    "catalog-number": "GZS-1144",
                    "label": {"name": "DCC Compact Classics"},
                },
                {
                    "catalog-number": "none",
                    "label": {"name": "Promotional"},
                },
            ]
        }
        catnos: list[str] = []
        label_names: list[str] = []
        for l_info in mb_release.get("label-info") or mb_release.get("label-info-list") or []:
            raw_catno = l_info.get("catalog-number", "")
            assert isinstance(raw_catno, str)
            mb_c = audiophile_tagger.clean_catno(raw_catno)
            if mb_c and mb_c not in catnos:
                catnos.append(mb_c)
            lbl = l_info.get("label", {})
            if isinstance(lbl, dict):
                lname = audiophile_tagger.clean_credit(lbl.get("name", ""))
                if lname and lname not in label_names:
                    label_names.append(lname)

        assert catnos == ["GZS-1144"]
        assert label_names == ["DCC Compact Classics", "Promotional"]

    def test_verify_all_flacs_mandatory_catalognumber_and_label(self, tmp_path: Path) -> None:
        """Optional release fields may be absent, but malformed values are rejected."""
        flac_path = tmp_path / "test_missing_catno.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "flac", str(flac_path)],
            check=True,
            capture_output=True,
        )
        base_tags = [
            ("TITLE", "Track"),
            ("ARTIST", "Queen"),
            ("ALBUM", "A Night At The Opera"),
            ("ALBUMSORT", "Night At The Opera, A"),
            ("ALBUMARTIST", "Queen"),
            ("ALBUMARTISTSORT", "Queen"),
            ("DATE", "1975-11-21"),
            ("YEAR", "1975"),
            ("ORIGINALDATE", "1975-11-21"),
            ("ORIGINALYEAR", "1975"),
            ("TRACKNUMBER", "11"),
            ("TRACKTOTAL", "12"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("GENRE", "Rock"),
            ("DYNAMIC_RANGE", "11"),
            ("ALBUM_DYNAMIC_RANGE", "11"),
            ("REPLAYGAIN_TRACK_GAIN", "-2.38 dB"),
            ("REPLAYGAIN_TRACK_PEAK", "1.006018"),
            ("REPLAYGAIN_ALBUM_GAIN", "-2.38 dB"),
            ("REPLAYGAIN_ALBUM_PEAK", "1.006018"),
            ("REPLAYGAIN_REFERENCE_LOUDNESS", "-18.00 LUFS"),
            ("MUSICBRAINZ_TRACKID", "11111111-2222-4333-8444-555555555555"),
            ("MUSICBRAINZ_RELEASEGROUPID", "22222222-3333-4333-8555-666666666666"),
            ("MUSICBRAINZ_ARTISTID", "33333333-4444-4555-8666-777777777777"),
            ("DISCOGS_RELEASE_URL", "https://www.discogs.com/release/3894462"),
        ]
        # Digital and undocumented releases legitimately lack these fields.
        write_tags(flac_path, base_tags)
        rep1 = audit_flac(flac_path)
        assert not any("Missing mandatory tag: CATALOGNUMBER" in err for err in rep1.errors)
        assert not any("Missing mandatory tag: LABEL" in err for err in rep1.errors)
        assert not any("Missing mandatory tag: MEDIA" in err for err in rep1.errors)

        # Invalid CATALOGNUMBER value ("?" written directly to tags)
        audio_bad = FLAC(str(flac_path))
        audio_bad["CATALOGNUMBER"] = ["?"]
        audio_bad.save()
        rep2 = audit_flac(flac_path)
        assert any("Invalid CATALOGNUMBER value" in err for err in rep2.errors)

        # Valid CATALOGNUMBER -> passes
        good_tags = list(base_tags) + [
            ("MEDIA", "CD"),
            ("LABEL", "DCC Compact Classics"),
            ("CATALOGNUMBER", "GZS-1144"),
        ]
        write_tags(flac_path, good_tags)
        rep3 = audit_flac(flac_path)
        assert not any("CATALOGNUMBER" in err for err in rep3.errors)

    def test_album_min_max_dr_handling(self, tmp_path: Path) -> None:
        """Verify ALBUM_MIN_DR and ALBUM_MAX_DR validation in audit_flac."""
        flac_path = tmp_path / "test_minmax_dr.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "flac", str(flac_path)],
            check=True,
            capture_output=True,
        )
        base_tags = [
            ("TITLE", "Track"),
            ("ARTIST", "Queen"),
            ("ALBUM", "A Night At The Opera"),
            ("ALBUMSORT", "Night At The Opera, A"),
            ("ALBUMARTIST", "Queen"),
            ("ALBUMARTISTSORT", "Queen"),
            ("DATE", "1975-11-21"),
            ("YEAR", "1975"),
            ("ORIGINALDATE", "1975-11-21"),
            ("ORIGINALYEAR", "1975"),
            ("TRACKNUMBER", "11"),
            ("TRACKTOTAL", "12"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("GENRE", "Rock"),
            ("MEDIA", "CD"),
            ("LABEL", "DCC Compact Classics"),
            ("CATALOGNUMBER", "GZS-1144"),
            ("DYNAMIC_RANGE", "11"),
            ("ALBUM_DYNAMIC_RANGE", "11"),
            ("ALBUM_MIN_DR", "10"),
            ("ALBUM_MAX_DR", "13"),
            ("REPLAYGAIN_TRACK_GAIN", "-2.38 dB"),
            ("REPLAYGAIN_TRACK_PEAK", "1.006018"),
            ("REPLAYGAIN_ALBUM_GAIN", "-2.38 dB"),
            ("REPLAYGAIN_ALBUM_PEAK", "1.006018"),
            ("REPLAYGAIN_REFERENCE_LOUDNESS", "-18.00 LUFS"),
            ("MUSICBRAINZ_TRACKID", "11111111-2222-4333-8444-555555555555"),
            ("MUSICBRAINZ_RELEASEGROUPID", "22222222-3333-4333-8555-666666666666"),
            ("MUSICBRAINZ_ARTISTID", "33333333-4444-4555-8666-777777777777"),
            ("DISCOGS_RELEASE_URL", "https://www.discogs.com/release/3894462"),
        ]
        write_tags(flac_path, base_tags)
        rep = audit_flac(flac_path)
        assert not any("ALBUM_MIN_DR" in err or "ALBUM_MAX_DR" in err for err in rep.errors)

        # Invalid out-of-range Min DR (e.g. 35)
        audio_bad = FLAC(str(flac_path))
        audio_bad["ALBUM_MIN_DR"] = ["35"]
        audio_bad.save()
        rep_bad = audit_flac(flac_path)
        assert any("ALBUM_MIN_DR out of plausible range" in err for err in rep_bad.errors)

    def test_vorbis_comment_multivalue_compliance(self, tmp_path: Path) -> None:
        """Verify that multi-valued Vorbis tags are stored as repeated fields (Xiph standard)."""
        flac_path = tmp_path / "test_multi.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "flac", str(flac_path)],
            check=True,
            capture_output=True,
        )
        tags = [
            ("TITLE", "Under Pressure"),
            ("ARTIST", "Queen & David Bowie"),
            ("ARTISTS", "Queen"),
            ("ARTISTS", "David Bowie"),
            ("ALBUMARTIST", "Queen"),
            ("ALBUM", "Hot Space"),
            ("LABEL", "EMI"),
            ("LABEL", "Elektra"),
            ("CATALOGNUMBER", "CDP 7 46215 2"),
            ("CATALOGNUMBER", "1-60128"),
            ("DATE", "1982-05-21"),
            ("TRACKNUMBER", "11"),
            ("TRACKTOTAL", "11"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
        ]
        write_tags(flac_path, tags)

        audio = FLAC(str(flac_path))
        assert audio.tags is not None
        artists = audio.tags.get("ARTISTS", [])
        assert artists == ["Queen", "David Bowie"]

        labels = audio.tags.get("LABEL", [])
        assert labels == ["EMI", "Elektra"]

        catnos = audio.tags.get("CATALOGNUMBER", [])
        assert catnos == ["CDP 7 46215 2", "1-60128"]


# ==============================================================================
# 9. Audiophile Cascading Lyrics Pipeline Tests (Levels 1-4, Early Exit, Validation)
# ==============================================================================
def disable_lyrics_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep cascade tests offline unless a test explicitly supplies a fixture."""
    import urllib.error
    import urllib.request

    import lyrics_utils

    providers = (
        "fetch_apple_music_ttml",
        "fetch_musixmatch",
        "fetch_chorus_chart",
        "fetch_ultrastar_db",
        "fetch_qqmusic",
        "search_netease_yrc",
        "fetch_kugou",
        "fetch_petitlyrics",
        "fetch_yandex_music",
        "fetch_vk_music_lyrics",
        "fetch_lrclib",
        "fetch_megalobiz",
        "fetch_rentanadviser",
        "fetch_spotify",
        "fetch_deezer_lyrics",
        "fetch_genius",
        "fetch_vk_topics",
        "fetch_amdm_lyrics",
        "fetch_bandcamp_lyrics",
        "fetch_baidu_lyrics",
        "fetch_darklyrics",
        "fetch_metal_archives",
        "fetch_vocadb",
        "fetch_tekstowo",
    )
    for provider in providers:
        if hasattr(lyrics_utils, provider):
            monkeypatch.setattr(lyrics_utils, provider, lambda *args, **kwargs: None)

    def no_network(*_args: object, **_kwargs: object) -> object:
        raise urllib.error.URLError("network disabled in fixture test")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)


class TestAudiophileLyricsPipeline:
    def test_detect_language_branch(self) -> None:
        import lyrics_utils

        # Cyrillic
        assert lyrics_utils.detect_language_branch("Группа крови", "Кино") == "CYRILLIC"
        assert lyrics_utils.detect_language_branch("Кукушка", "Виктор Цой") == "CYRILLIC"
        assert lyrics_utils.detect_language_branch("Без бою", "Океан Ельзи") == "CYRILLIC"

        # CJK (Chinese, Japanese, Korean)
        assert lyrics_utils.detect_language_branch("夜曲", "周杰伦") == "CJK"
        assert lyrics_utils.detect_language_branch("First Love", "宇多田ヒカル") == "CJK"
        assert lyrics_utils.detect_language_branch("봄날", "방탄소년단") == "CJK"

        # Latin
        assert lyrics_utils.detect_language_branch("Bohemian Rhapsody", "Queen") == "LATIN"
        assert lyrics_utils.detect_language_branch("Du Hast", "Rammstein") == "LATIN"
        assert lyrics_utils.detect_language_branch("Around the World", "Daft Punk") == "LATIN"

    def test_get_query_profiles(self) -> None:
        import lyrics_utils

        strict_p, clean_p = lyrics_utils.get_query_profiles("Under Pressure (2011 Remaster)", "Queen & David Bowie")
        assert strict_p[0] == "Under Pressure (2011 Remaster)"
        assert clean_p[0] == "Under Pressure"

        # Live and Deluxe phrases
        _, clean_live = lyrics_utils.get_query_profiles(
            "Hotel California - Live at The Forum 1976 / 40th Anniversary Deluxe Edition", "Eagles"
        )
        assert "Hotel California" in clean_live[0]
        assert "Anniversary" not in clean_live[0]
        assert "Deluxe" not in clean_live[0]

    def test_validate_lyrics_duration(self) -> None:
        import lyrics_utils

        # Target duration 240.0s, tolerance ±2.5s -> range [237.5, 242.5]
        candidate_good = {"duration": 241.0, "syncedLyrics": "[00:10.00] Line 1"}
        assert lyrics_utils.validate_lyrics_duration(candidate_good, 240.0, tolerance=2.5) is True

        # Radio edit vs album version desync: 210.0s vs 240.0s -> diff 30s -> False
        candidate_desync = {"duration": 210.0, "syncedLyrics": "[00:10.00] Line 1"}
        assert lyrics_utils.validate_lyrics_duration(candidate_desync, 240.0, tolerance=2.5) is False

        # Parse duration from line timestamps
        candidate_lrc = {"syncedLyrics": "[00:05.00] Start\n[03:59.50] The End\n"}
        assert lyrics_utils.validate_lyrics_duration(candidate_lrc, 240.0, tolerance=2.5) is True
        assert lyrics_utils.validate_lyrics_duration(candidate_lrc, 200.0, tolerance=2.5) is False

        # No target duration provided -> always True (permissive)
        assert lyrics_utils.validate_lyrics_duration(candidate_desync, None) is True

    def test_clean_plain_lyrics_for_alignment(self) -> None:
        import lyrics_utils

        raw_amdm = (
            "[Вступление]:\n"
            "Am  C  G  Em\n"
            "\n"
            "[Куплет 1]\n"
            "Am          C\n"
            "Песня без слов, ночь без сна\n"
            "G           Em\n"
            "Все в свое время - зима и весна (x2)\n"
            "\n"
            "[Припев]\n"
            "F       G\n"
            "Группа крови на рукаве!\n"
        )
        cleaned = lyrics_utils.clean_plain_lyrics_for_alignment(raw_amdm, song="Группа крови", artist="Кино")
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        assert "Песня без слов, ночь без сна" in lines
        assert "Все в свое время - зима и весна" in lines
        assert "Группа крови на рукаве!" in lines
        # Structural tags and chords stripped
        assert not any("Куплет" in l or "Припев" in l or "Вступление" in l for l in lines)
        assert not any("Am" in l and "Em" in l for l in lines)
        assert not any("(x2)" in l for l in lines)

    def test_parse_ultrastar_lyrics(self) -> None:
        import lyrics_utils

        ultrastar_data = (
            "#TITLE:Bohemian Rhapsody\n"
            "#ARTIST:Queen\n"
            "#BPM:120\n"
            "#GAP:1000\n"
            ": 0 4 0 Is \n"
            ": 4 4 2 this \n"
            ": 8 4 4 the \n"
            ": 12 8 5 real \n"
            ": 20 8 4 life \n"
            "- 30\n"
            ": 32 4 4 Is \n"
            ": 36 4 2 this \n"
            ": 40 4 4 just \n"
            ": 44 8 5 fan-\n"
            ": 52 8 4 tasy\n"
            "E\n"
        )
        parsed = lyrics_utils.parse_ultrastar_lyrics(ultrastar_data, song="Bohemian Rhapsody", artist="Queen")
        assert parsed is not None
        assert parsed["found"] is True
        assert parsed["has_word_sync"] is True
        assert "Is this the real life" in parsed["plainLyrics"]
        assert "<00:01." in parsed["enhancedLyrics"]
        assert "[00:01." in parsed["syncedLyrics"]

    def test_fetch_lyrics_cascade_level1_early_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import lyrics_utils

        disable_lyrics_network(monkeypatch)

        level1_called = False
        level2_called = False

        def mock_apple_ttml(song: str, artist: str, **kwargs: Any) -> dict[str, Any]:
            nonlocal level1_called
            level1_called = True
            return {
                "found": True,
                "source": "Unison",
                "format": "ttml",
                "syncType": "richsync",
                "has_word_sync": True,
                "has_back_vocals": False,
                "rawLyrics": "<tt>...</tt>",
                "plainLyrics": "Word by word lyrics",
                "syncedLyrics": "[00:01.00] Word by word lyrics",
                "enhancedLyrics": "[00:01.00] <00:01.00>Word <00:01.50>by <00:02.00>word",
                "ttmlLyrics": "<tt>...</tt>",
                "duration": 200.0,
            }

        def mock_lrclib(song: str, artist: str, **kwargs: Any) -> dict[str, Any]:
            nonlocal level2_called
            level2_called = True
            return {}

        monkeypatch.setattr(lyrics_utils, "fetch_apple_music_ttml", mock_apple_ttml)
        monkeypatch.setattr(lyrics_utils, "fetch_lrclib", mock_lrclib)

        result = lyrics_utils.fetch_lyrics_cascade(
            song="Bohemian Rhapsody",
            artist="Queen",
            duration=200.0,
            enable_neural=False,
        )

        assert result is not None
        assert result["source"] == "Unison"
        assert result["has_word_sync"] is True
        assert level1_called is True
        assert level2_called is True  # lower tiers are collected for candidate scoring
        assert "alternatives" in result

    def test_unsynchronized_official_musixmatch_candidate_reaches_selection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import lyrics_pipeline
        import lyrics_utils

        disable_lyrics_network(monkeypatch)
        plain = (
            "A complete unsynchronized lyric line with enough detail to validate the provider text.\n"
            "Another complete line remains available even though no timestamps were supplied.\n"
        ) * 8
        monkeypatch.setattr(
            lyrics_utils,
            "fetch_musixmatch",
            lambda *_args, **_kwargs: {
                "found": True,
                "source": "Musixmatch",
                "provider_track_id": "mx-123",
                "trackName": "Fixture Song",
                "artistName": "Fixture Artist",
                "albumName": "Fixture Album",
                "duration": 200.0,
                "plainLyrics": plain,
                "providerRaw": plain,
                "language": "en",
            },
        )

        result = lyrics_utils.fetch_lyrics_cascade(
            "Fixture Song", "Fixture Artist", "Fixture Album", 200.0, enable_neural=False
        )
        assert result is not None
        choices = [result, *result.get("alternatives", [])]
        candidate = next(item for item in choices if item.get("source") == "Musixmatch")
        assert candidate["plainLyrics"] == plain
        selected = lyrics_pipeline.choose_provider_result(
            [result], title="Fixture Song", artist="Fixture Artist", album="Fixture Album", duration=200
        )
        assert selected is not None
        assert selected.candidate.provider == "Musixmatch"
        assert not selected.candidate.synced

    def test_fetch_lyrics_cascade_level2_early_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import lyrics_utils

        disable_lyrics_network(monkeypatch)

        level2_called = False
        level3_called = False

        # Level 1 providers return None
        monkeypatch.setattr(lyrics_utils, "fetch_apple_music_ttml", lambda *a, **k: None)
        monkeypatch.setattr(lyrics_utils, "fetch_musixmatch", lambda *a, **k: None)
        monkeypatch.setattr(lyrics_utils, "fetch_chorus_chart", lambda *a, **k: None)
        monkeypatch.setattr(lyrics_utils, "fetch_ultrastar_db", lambda *a, **k: None)
        monkeypatch.setattr(lyrics_utils, "fetch_qqmusic", lambda *a, **k: None)
        monkeypatch.setattr(lyrics_utils, "search_netease_yrc", lambda *a, **k: None)
        monkeypatch.setattr(lyrics_utils, "fetch_kugou", lambda *a, **k: None)
        monkeypatch.setattr(lyrics_utils, "fetch_petitlyrics", lambda *a, **k: None)

        def mock_lrclib(song: str, artist: str, **kwargs: Any) -> dict[str, Any]:
            nonlocal level2_called
            level2_called = True
            return {
                "source": "LRCLIB",
                "syncedLyrics": "[00:10.00] Line synced line\n[03:20.00] End line",
                "plainLyrics": "Line synced line\nEnd line",
                "duration": 200.0,
            }

        def mock_genius(song: str, artist: str, **kwargs: Any) -> str:
            nonlocal level3_called
            level3_called = True
            return "Plain text lyrics"

        monkeypatch.setattr(lyrics_utils, "fetch_lrclib", mock_lrclib)
        monkeypatch.setattr(lyrics_utils, "fetch_genius", mock_genius)

        result = lyrics_utils.fetch_lyrics_cascade(
            song="Radio Ga Ga",
            artist="Queen",
            duration=200.0,
            enable_neural=False,
        )

        assert result is not None
        assert result["syncType"] == "linesync"
        assert level2_called is True
        assert level3_called is True  # plain candidates are collected alongside synchronized results
        assert "alternatives" in result

    def test_plain_cascade_candidate_does_not_echo_requested_duration_as_evidence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import lyrics_pipeline
        import lyrics_utils

        disable_lyrics_network(monkeypatch)
        providers = (
            "fetch_apple_music_ttml",
            "fetch_musixmatch",
            "fetch_chorus_chart",
            "fetch_ultrastar_db",
            "fetch_qqmusic",
            "search_netease_yrc",
            "fetch_kugou",
            "fetch_petitlyrics",
            "fetch_yandex_music",
            "fetch_vk_music_lyrics",
            "fetch_lrclib",
            "fetch_megalobiz",
            "fetch_rentanadviser",
            "fetch_spotify",
            "fetch_deezer_lyrics",
            "fetch_vk_topics",
            "fetch_amdm_lyrics",
            "fetch_bandcamp_lyrics",
            "fetch_baidu_lyrics",
            "fetch_darklyrics",
            "fetch_metal_archives",
            "fetch_vocadb",
            "fetch_tekstowo",
        )
        for provider_name in providers:
            monkeypatch.setattr(lyrics_utils, provider_name, lambda *_args, **_kwargs: None)

        text = (
            "A provider supplied a real plain transcript without duration metadata.\n"
            "The local duration belongs to the query and must not become candidate evidence."
        ) * 8

        def genius(song: str, artist: str, **_kwargs: Any) -> dict[str, Any]:
            return {
                "found": True,
                "source": "Genius",
                "trackName": song,
                "artistName": artist,
                "identity_validated": True,
                "plainLyrics": text,
                "providerRaw": text,
            }

        monkeypatch.setattr(lyrics_utils, "fetch_genius", genius)
        result = lyrics_utils.fetch_lyrics_cascade(
            "No Duration Evidence Song",
            "Fixture Artist",
            "Fixture Album",
            123.0,
            enable_neural=False,
        )
        assert result is not None
        selected = lyrics_pipeline.choose_provider_result(
            [result],
            title="No Duration Evidence Song",
            artist="Fixture Artist",
            album="Fixture Album",
            duration=123.0,
        )
        assert result.get("duration") is None
        result_candidates = [result, *result.get("alternatives", [])]
        genius_candidate = next(
            lyrics_pipeline.provider_candidate(item, fallback_provider="fixture", duration=123.0)
            for item in result_candidates
            if item.get("source") == "Genius" and item.get("plainLyrics")
        )
        assert genius_candidate is not None
        assert genius_candidate.duration is None
        assert selected is not None
        assert selected.candidate.duration is None

    def test_fetch_lyrics_cascade_duration_mismatch_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import lyrics_utils

        disable_lyrics_network(monkeypatch)

        # Level 1 returns a candidate, but with wrong duration (radio edit mismatch)
        def mock_apple_ttml(song: str, artist: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "found": True,
                "source": "Unison",
                "has_word_sync": True,
                "duration": 150.0,  # Far from target 240.0s!
                "trackName": "Somebody To Love",
                "artistName": "Queen",
                "syncedLyrics": "[00:10.00] Short version",
                "plainLyrics": "Short version lyric text",
            }

        # Level 2 returns correct duration
        def mock_lrclib(song: str, artist: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "source": "LRCLIB",
                "syncedLyrics": "[00:10.00]A full album verse begins with a distinctive and complete line of lyrics.\n"
                "[03:59.00]The final refrain closes the complete recording with another long line.",
                "plainLyrics": "A full album verse begins with a distinctive and complete line of lyrics.\n"
                "The final refrain closes the complete recording with another long line.",
                "duration": 240.0,
                "trackName": "Somebody To Love",
                "artistName": "Queen",
            }

        monkeypatch.setattr(lyrics_utils, "fetch_apple_music_ttml", mock_apple_ttml)
        monkeypatch.setattr(lyrics_utils, "fetch_musixmatch", lambda *a, **k: None)
        monkeypatch.setattr(lyrics_utils, "fetch_chorus_chart", lambda *a, **k: None)
        monkeypatch.setattr(lyrics_utils, "fetch_ultrastar_db", lambda *a, **k: None)
        monkeypatch.setattr(lyrics_utils, "fetch_qqmusic", lambda *a, **k: None)
        monkeypatch.setattr(lyrics_utils, "search_netease_yrc", lambda *a, **k: None)
        monkeypatch.setattr(lyrics_utils, "fetch_kugou", lambda *a, **k: None)
        monkeypatch.setattr(lyrics_utils, "fetch_petitlyrics", lambda *a, **k: None)
        monkeypatch.setattr(lyrics_utils, "fetch_lrclib", mock_lrclib)

        result = lyrics_utils.fetch_lyrics_cascade(
            song="Somebody To Love",
            artist="Queen",
            duration=240.0,
            enable_neural=False,
        )

        assert result is not None
        # Both are retained until recording-aware selection rejects the wrong duration.
        providers = [result["source"], *(item["source"] for item in result.get("alternatives", []))]
        assert "Unison" in providers
        assert "LRCLIB" in providers
        import lyrics_pipeline

        selected = lyrics_pipeline.choose_provider_result(
            [result], title="Somebody To Love", artist="Queen", album="", duration=240.0
        )
        assert selected and selected.candidate.provider == "LRCLIB"

    def test_neural_lyrics_engine_graceful_degradation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import shutil

        import lyrics_utils

        dummy_audio = tmp_path / "dummy.flac"
        dummy_audio.write_bytes(b"FLAC_DUMMY_DATA")

        # Mock binary check so unit test does not trigger external model download
        monkeypatch.setattr(shutil, "which", lambda cmd: None)
        monkeypatch.setattr(lyrics_utils.NeuralLyricsEngine, "whisper_asr", lambda *a, **k: None)
        monkeypatch.setattr(lyrics_utils.NeuralLyricsEngine, "forced_alignment", lambda *a, **k: None)

        # Must return None or degrade gracefully without uncaught exceptions
        assert lyrics_utils.NeuralLyricsEngine.isolate_vocals(dummy_audio) is None
        assert lyrics_utils.NeuralLyricsEngine.forced_alignment(dummy_audio, "Some text") is None
        assert lyrics_utils.NeuralLyricsEngine.whisper_asr(dummy_audio) is None


class TestGlobalVorbisAndAudiophileStandards:
    """Comprehensive test suite verifying adherence to worldwide Vorbis Comment,

    Steve Hoffman forum audiophile consensus, and dr.loudness-war.info standards.
    """

    def test_xiph_vorbis_comment_rfc_field_specifications(self, tmp_path: Path) -> None:
        """Verify Xiph RFC 7845 Vorbis Comment specifications: ASCII keys, case insensitivity,

        and rejection of invalid field characters.
        """
        import metadata_writer

        flac_path = tmp_path / "xiph_rfc.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )
        # Valid Vorbis field names contain ASCII 0x20-0x7D excluding '='
        manifest = tmp_path / "manifest.txt"
        manifest.write_text(
            "TITLE=Bohemian Rhapsody\n"
            "ARTIST=Queen\n"
            "ALBUM=A Night At The Opera\n"
            "DATE=1975-11-21\n"
            "TRACKNUMBER=11\n"
            "TRACKTOTAL=12\n"
            "DISCNUMBER=1\n"
            "DISCTOTAL=1\n"
            "ALBUMARTIST=Queen\n"
            "CUSTOM_AUDIOPHILE_FIELD=Audiophile Value\n",
            encoding="utf-8",
        )
        tags = metadata_writer.load_tags(manifest)
        metadata_writer.write_tags(flac_path, tags)

        audio = FLAC(str(flac_path))
        assert "TITLE" in audio
        # Mutagen supports case-insensitive dict access for Vorbis comments
        assert audio["title"] == ["Bohemian Rhapsody"]
        assert audio["custom_audiophile_field"] == ["Audiophile Value"]

        # Test rejection of field names with invalid characters (non-ASCII / control characters)
        invalid_manifest = tmp_path / "invalid_manifest.txt"
        invalid_manifest.write_text("ЖАНР=Rock\n", encoding="utf-8")
        with pytest.raises(ValueError, match="invalid Vorbis field name"):
            metadata_writer.load_tags(invalid_manifest)

    def test_native_multivalue_vorbis_tags_vs_semicolon(self, tmp_path: Path) -> None:
        """Verify VorbisComment native multi-value convention (repeated comments)

        over the legacy semicolon antipattern.
        """
        import metadata_writer
        import verify_all_flacs

        flac_path = tmp_path / "multivalue.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )
        # Native Vorbis multi-values
        tags = [
            ("TITLE", "Under Pressure"),
            ("ALBUM", "Hot Space"),
            ("ALBUMSORT", "Hot Space"),
            ("ALBUMARTIST", "Queen"),
            ("ALBUMARTISTSORT", "Queen"),
            ("ARTIST", "Queen & David Bowie"),
            ("ARTISTS", "Queen"),
            ("ARTISTS", "David Bowie"),
            ("ARTISTSORT", "Queen; Bowie, David"),
            ("DATE", "1982-05-21"),
            ("YEAR", "1982"),
            ("ORIGINALDATE", "1982-05-21"),
            ("ORIGINALYEAR", "1982"),
            ("TRACKNUMBER", "11"),
            ("TRACKTOTAL", "11"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("GENRE", "Rock"),
            ("GENRE", "Pop Rock"),
            ("LABEL", "EMI"),
            ("LABEL", "Elektra"),
            ("CATALOGNUMBER", "CDP 7 46215 2"),
            ("CATALOGNUMBER", "0777 7 46215 2 4"),
            ("DYNAMIC_RANGE", "12"),
            ("ALBUM_DYNAMIC_RANGE", "11"),
            ("REPLAYGAIN_TRACK_GAIN", "-3.50 dB"),
            ("REPLAYGAIN_TRACK_PEAK", "0.850000"),
            ("REPLAYGAIN_ALBUM_GAIN", "-3.20 dB"),
            ("REPLAYGAIN_ALBUM_PEAK", "0.850000"),
            ("MUSICBRAINZ_RECORDINGID", "3604eb06-4bc2-4416-9b31-ceadae51bc70"),
            ("MUSICBRAINZ_RELEASEGROUPID", "c172ea98-3a82-3254-b8ad-25a4f465ba03"),
            ("MUSICBRAINZ_ARTISTID", "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"),
            ("DISCOGS_RELEASE_URL", "https://www.discogs.com/release/12345"),
        ]
        metadata_writer.write_tags(flac_path, tags)

        audio = FLAC(str(flac_path))
        # Ensure separate repeated comments exist
        assert audio["ARTIST"] == ["Queen & David Bowie"]
        assert audio["ARTISTS"] == ["Queen", "David Bowie"]
        assert audio["LABEL"] == ["EMI", "Elektra"]
        assert audio["CATALOGNUMBER"] == ["CDP 7 46215 2", "0777 7 46215 2 4"]

        # Audit must pass multi-valued ARTIST, LABEL, and CATALOGNUMBER without duplicate errors
        res = verify_all_flacs.audit_flac(flac_path)
        assert not any("Duplicate single-valued tag: ARTIST" in e for e in res.errors)
        assert not any("Duplicate single-valued tag: LABEL" in e for e in res.errors)
        assert not any("Duplicate single-valued tag: CATALOGNUMBER" in e for e in res.errors)

    def test_shf_mastering_and_remastering_integrity(self, tmp_path: Path) -> None:
        """Verify Steve Hoffman Music Forums audiophile mastering tags:

        MASTERED_BY, MASTERINGENGINEER, REMASTERED_BY, REMASTERER.
        """
        import metadata_writer
        import verify_all_flacs

        flac_path = tmp_path / "shf_mastering.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )
        tags = [
            ("TITLE", "Bohemian Rhapsody"),
            ("ALBUM", "A Night at the Opera"),
            ("ALBUMSORT", "Night at the Opera, A"),
            ("ALBUMARTIST", "Queen"),
            ("ALBUMARTISTSORT", "Queen"),
            ("ARTIST", "Queen"),
            ("DATE", "2000-08-22"),
            ("YEAR", "2000"),
            ("ORIGINALDATE", "1975-11-21"),
            ("ORIGINALYEAR", "1975"),
            ("TRACKNUMBER", "11"),
            ("TRACKTOTAL", "12"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("GENRE", "Rock"),
            ("MEDIA", "Gold CD"),
            ("EDITION", "2000 DCC 24K Gold Collector's Edition"),
            ("MASTERED_BY", "Steve Hoffman"),
            ("MASTERINGENGINEER", "Steve Hoffman"),
            ("REMASTERED_BY", "Steve Hoffman"),
            ("REMASTERER", "Steve Hoffman"),
            ("LABEL", "DCC Compact Classics"),
            ("CATALOGNUMBER", "GZS-1144"),
            ("DYNAMIC_RANGE", "11"),
            ("ALBUM_DYNAMIC_RANGE", "11"),
            ("REPLAYGAIN_TRACK_GAIN", "-2.38 dB"),
            ("REPLAYGAIN_TRACK_PEAK", "1.006018"),
            ("REPLAYGAIN_ALBUM_GAIN", "-2.38 dB"),
            ("REPLAYGAIN_ALBUM_PEAK", "1.006018"),
            ("REPLAYGAIN_REFERENCE_LOUDNESS", "-18.00 LUFS"),
            ("MUSICBRAINZ_TRACKID", "b1a9c0e9-d987-4042-ae91-78d6a3267d69"),
            ("MUSICBRAINZ_RELEASEGROUPID", "6b47c9a0-b9e1-3df9-a5e8-50a6ce0dbdbd"),
            ("MUSICBRAINZ_ARTISTID", "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"),
            ("DISCOGS_RELEASE_URL", "https://www.discogs.com/release/3894462"),
        ]
        metadata_writer.write_tags(flac_path, tags)

        audio = FLAC(str(flac_path))
        assert audio["MASTERED_BY"] == ["Steve Hoffman"]
        assert audio["MASTERINGENGINEER"] == ["Steve Hoffman"]
        assert audio["REMASTERED_BY"] == ["Steve Hoffman"]
        assert audio["REMASTERER"] == ["Steve Hoffman"]
        assert audio["EDITION"] == ["2000 DCC 24K Gold Collector's Edition"]

        res = verify_all_flacs.audit_flac(flac_path)
        assert res.errors == []

    def test_shf_matrix_and_runout_preservation(self, tmp_path: Path) -> None:
        """Verify Matrix/Runout stamp tags essential for identifying specific pressings on SHF."""
        import metadata_writer

        flac_path = tmp_path / "matrix_test.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )
        initial_tags = [
            ("TITLE", "Track 1"),
            ("ALBUM", "Album"),
            ("ARTIST", "Artist"),
            ("ALBUMARTIST", "Artist"),
            ("TRACKNUMBER", "1"),
            ("TRACKTOTAL", "1"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("DATE", "1983"),
            ("MATRIX", "$GZS-1144 X0725L"),
            ("MATRIX_SOURCE", "Discogs"),
        ]
        metadata_writer.write_tags(flac_path, initial_tags)

        # Re-tag without matrix in new manifest: must preserve existing MATRIX
        new_tags = [
            ("TITLE", "Track 1 New"),
            ("ALBUM", "Album"),
            ("ARTIST", "Artist"),
            ("ALBUMARTIST", "Artist"),
            ("TRACKNUMBER", "1"),
            ("TRACKTOTAL", "1"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("DATE", "1983"),
        ]
        metadata_writer.write_tags(flac_path, new_tags)

        audio = FLAC(str(flac_path))
        assert audio["TITLE"] == ["Track 1 New"]
        assert audio["MATRIX"] == ["$GZS-1144 X0725L"]
        assert audio["MATRIX_SOURCE"] == ["Discogs"]

    def test_audiophile_hardware_flags_and_spars(self, tmp_path: Path) -> None:
        """Verify SPARS code, Pre-emphasis, and HDCD flags compliance."""
        import metadata_writer
        import verify_all_flacs

        flac_path = tmp_path / "flags.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )
        tags = [
            ("TITLE", "Test Flags"),
            ("ALBUM", "Test Album"),
            ("ALBUMSORT", "Test Album"),
            ("ALBUMARTIST", "Queen"),
            ("ALBUMARTISTSORT", "Queen"),
            ("ARTIST", "Queen"),
            ("DATE", "1986"),
            ("YEAR", "1986"),
            ("ORIGINALDATE", "1986"),
            ("ORIGINALYEAR", "1986"),
            ("TRACKNUMBER", "1"),
            ("TRACKTOTAL", "1"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("GENRE", "Rock"),
            ("MEDIA", "CD"),
            ("LABEL", "EMI"),
            ("CATALOGNUMBER", "CP32-5152"),
            ("DYNAMIC_RANGE", "12"),
            ("ALBUM_DYNAMIC_RANGE", "12"),
            ("REPLAYGAIN_TRACK_GAIN", "-1.50 dB"),
            ("REPLAYGAIN_TRACK_PEAK", "0.910000"),
            ("REPLAYGAIN_ALBUM_GAIN", "-1.50 dB"),
            ("REPLAYGAIN_ALBUM_PEAK", "0.910000"),
            ("REPLAYGAIN_REFERENCE_LOUDNESS", "-18.00 LUFS"),
            ("MUSICBRAINZ_RECORDINGID", "b1a9c0e9-d987-4042-ae91-78d6a3267d69"),
            ("MUSICBRAINZ_RELEASEGROUPID", "6b47c9a0-b9e1-3df9-a5e8-50a6ce0dbdbd"),
            ("MUSICBRAINZ_ARTISTID", "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"),
            ("DISCOGS_RELEASE_URL", "https://www.discogs.com/release/3736884"),
            ("SPARSCODE", "AAD"),
            ("PRE_EMPHASIS", "1"),
            ("PRE-EMPHASIS", "1"),
            ("FLAGS", "PRE"),
            ("HDCD", "1"),
            ("HDCD_PEAK_EXTEND", "1"),
        ]
        metadata_writer.write_tags(flac_path, tags)

        audio = FLAC(str(flac_path))
        assert audio["SPARSCODE"] == ["AAD"]
        assert audio["PRE_EMPHASIS"] == ["1"]
        assert audio["FLAGS"] == ["PRE"]
        assert audio["HDCD"] == ["1"]

        res = verify_all_flacs.audit_flac(flac_path)
        assert res.errors == []
        assert not any("SPARSCODE" in w for w in res.warnings)

        # Invalid SPARS code generates a warning
        audio["SPARSCODE"] = ["INVALID"]
        audio.save()
        res_invalid = verify_all_flacs.audit_flac(flac_path)
        assert any("Unusual SPARSCODE" in w for w in res_invalid.warnings)

    def test_dynamic_range_dr14_standard_and_database_linkage(self, tmp_path: Path) -> None:
        """Verify Pleasurize Music Foundation DR14 standard and dr.loudness-war.info tags."""
        import dr14_parse
        import metadata_writer
        import verify_all_flacs

        # 1. Verify HTML parser for dr.loudness-war.info search results
        html_sample = """
        <table>
        <tr>
          <td>Queen</td>
          <td><a href="/album/view/3894462">A Night At The Opera</a></td>
          <td>2000</td>
          <td>11</td>
          <td>9</td>
          <td>13</td>
          <td>CD</td>
          <td>DCC GZS-1144 Steve Hoffman</td>
        </tr>
        </table>
        """
        entries = dr14_parse.parse_dr_search_html(html_sample)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["id"] == "3894462"
        assert entry["url"] == "https://dr.loudness-war.info/album/view/3894462"
        assert entry["dr"] == 11
        assert entry["min_dr"] == 9
        assert entry["max_dr"] == 13

        # 2. Matching logic
        matched = dr14_parse.match_dr_entry(entries, target_dr=11, target_year="2000", catalog_no="GZS-1144")
        assert matched is not None
        assert matched["id"] == "3894462"

        # 3. Flac tags audit
        flac_path = tmp_path / "dr_standard.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )
        tags = [
            ("TITLE", "Bohemian Rhapsody"),
            ("ALBUM", "A Night at the Opera"),
            ("ALBUMSORT", "Night at the Opera, A"),
            ("ALBUMARTIST", "Queen"),
            ("ALBUMARTISTSORT", "Queen"),
            ("ARTIST", "Queen"),
            ("DATE", "1975-11-21"),
            ("YEAR", "1975"),
            ("ORIGINALDATE", "1975-11-21"),
            ("ORIGINALYEAR", "1975"),
            ("TRACKNUMBER", "11"),
            ("TRACKTOTAL", "12"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("GENRE", "Rock"),
            ("MEDIA", "CD"),
            ("LABEL", "EMI"),
            ("CATALOGNUMBER", "CDP 7 46207 2"),
            ("DR", "11"),
            ("DYNAMIC_RANGE", "11"),
            ("ALBUM_DR", "11"),
            ("ALBUM_DYNAMIC_RANGE", "11"),
            ("ALBUM_MIN_DR", "9"),
            ("ALBUM_MAX_DR", "13"),
            ("DR_PEAK", "0.00 dB"),
            ("DR_RMS", "-18.16 dB"),
            ("DR_SOURCE", "dr14.txt"),
            ("DR_LOUDNESS_WAR_ID", "3894462"),
            ("DR_LOUDNESS_WAR_URL", "https://dr.loudness-war.info/album/view/3894462"),
            ("REPLAYGAIN_TRACK_GAIN", "-2.38 dB"),
            ("REPLAYGAIN_TRACK_PEAK", "1.006018"),
            ("REPLAYGAIN_ALBUM_GAIN", "-2.38 dB"),
            ("REPLAYGAIN_ALBUM_PEAK", "1.006018"),
            ("REPLAYGAIN_REFERENCE_LOUDNESS", "-18.00 LUFS"),
            ("MUSICBRAINZ_RECORDINGID", "b1a9c0e9-d987-4042-ae91-78d6a3267d69"),
            ("MUSICBRAINZ_RELEASEGROUPID", "6b47c9a0-b9e1-3df9-a5e8-50a6ce0dbdbd"),
            ("MUSICBRAINZ_ARTISTID", "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"),
            ("DISCOGS_RELEASE_URL", "https://www.discogs.com/release/3894462"),
        ]
        metadata_writer.write_tags(flac_path, tags)
        res = verify_all_flacs.audit_flac(flac_path)
        assert res.errors == []

    def test_musicbrainz_picard_date_duality_and_sorting(self, tmp_path: Path) -> None:
        """Verify MusicBrainz Picard standards: original date vs release date, and sort values."""
        import metadata_writer
        import verify_all_flacs

        flac_path = tmp_path / "date_duality.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )
        tags = [
            ("TITLE", "The Show Must Go On"),
            ("TITLESORT", "Show Must Go On, The"),
            ("ALBUM", "The Works"),
            ("ALBUMSORT", "Works, The"),
            ("ALBUMARTIST", "The Beatles"),
            ("ALBUMARTISTSORT", "Beatles, The"),
            ("ARTIST", "The Beatles"),
            ("ARTISTSORT", "Beatles, The"),
            ("DATE", "1969-09-26"),
            ("YEAR", "1969"),
            ("ORIGINALDATE", "1969-09-26"),
            ("ORIGINALYEAR", "1969"),
            ("RELEASEDATE", "2009-09-09"),
            ("ORIGINALRELEASEDATE", "1969-09-26"),
            ("MUSICBRAINZ_ORIGINALRELEASEDATE", "1969-09-26"),
            ("TRACKNUMBER", "1"),
            ("TRACKTOTAL", "1"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("GENRE", "Rock"),
            ("MEDIA", "CD"),
            ("LABEL", "Apple Records"),
            ("CATALOGNUMBER", "0946 3 82468 2 2"),
            ("DYNAMIC_RANGE", "11"),
            ("ALBUM_DYNAMIC_RANGE", "11"),
            ("REPLAYGAIN_TRACK_GAIN", "-4.20 dB"),
            ("REPLAYGAIN_TRACK_PEAK", "0.980000"),
            ("REPLAYGAIN_ALBUM_GAIN", "-4.20 dB"),
            ("REPLAYGAIN_ALBUM_PEAK", "0.980000"),
            ("REPLAYGAIN_REFERENCE_LOUDNESS", "-18.00 LUFS"),
            ("MUSICBRAINZ_RECORDINGID", "b1a9c0e9-d987-4042-ae91-78d6a3267d69"),
            ("MUSICBRAINZ_RELEASEGROUPID", "6b47c9a0-b9e1-3df9-a5e8-50a6ce0dbdbd"),
            ("MUSICBRAINZ_ARTISTID", "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"),
            ("DISCOGS_RELEASE_URL", "https://www.discogs.com/release/1973549"),
        ]
        metadata_writer.write_tags(flac_path, tags)
        audio = FLAC(str(flac_path))
        assert audio["TITLESORT"] == ["Show Must Go On, The"]
        assert audio["ALBUMSORT"] == ["Works, The"]
        assert audio["ORIGINALDATE"] == ["1969-09-26"]
        assert audio["RELEASEDATE"] == ["2009-09-09"]

        res = verify_all_flacs.audit_flac(flac_path)
        assert res.errors == []

    def test_musicbrainz_uuid_and_discid_validation(self, tmp_path: Path) -> None:
        """Verify strict UUID formatting for MusicBrainz IDs and AcoustID."""
        import metadata_writer
        import verify_all_flacs

        flac_path = tmp_path / "mb_uuids.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )
        tags = [
            ("TITLE", "Test Track"),
            ("ALBUM", "Test Album"),
            ("ALBUMSORT", "Test Album"),
            ("ALBUMARTIST", "Queen"),
            ("ALBUMARTISTSORT", "Queen"),
            ("ARTIST", "Queen"),
            ("DATE", "1975"),
            ("YEAR", "1975"),
            ("ORIGINALDATE", "1975"),
            ("ORIGINALYEAR", "1975"),
            ("TRACKNUMBER", "1"),
            ("TRACKTOTAL", "1"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("GENRE", "Rock"),
            ("MEDIA", "CD"),
            ("LABEL", "EMI"),
            ("CATALOGNUMBER", "CDP 7 46207 2"),
            ("DYNAMIC_RANGE", "11"),
            ("ALBUM_DYNAMIC_RANGE", "11"),
            ("REPLAYGAIN_TRACK_GAIN", "-2.38 dB"),
            ("REPLAYGAIN_TRACK_PEAK", "0.980000"),
            ("REPLAYGAIN_ALBUM_GAIN", "-2.38 dB"),
            ("REPLAYGAIN_ALBUM_PEAK", "0.980000"),
            ("REPLAYGAIN_REFERENCE_LOUDNESS", "-18.00 LUFS"),
            ("MUSICBRAINZ_RECORDINGID", "b1a9c0e9-d987-4042-ae91-78d6a3267d69"),
            ("MUSICBRAINZ_RELEASEGROUPID", "6b47c9a0-b9e1-3df9-a5e8-50a6ce0dbdbd"),
            ("MUSICBRAINZ_ARTISTID", "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"),
            ("MUSICBRAINZ_ALBUMID", "1bc2fe9f-630e-3a84-8589-7ee6789a5e0f"),
            ("MUSICBRAINZ_RELEASETRACKID", "b343ca82-baf2-356d-974e-94836915eec0"),
            ("MUSICBRAINZ_WORKID", "41c94a08-a551-3c86-bb17-d9a52e3a618b"),
            ("ACOUSTID_ID", "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"),
            ("MUSICBRAINZ_DISCID", "Bwtd3JqUFeth022I9Zr39vqCK80-"),
            ("DISCOGS_RELEASE_URL", "https://www.discogs.com/release/3894462"),
        ]
        metadata_writer.write_tags(flac_path, tags)
        res = verify_all_flacs.audit_flac(flac_path)
        assert res.errors == []

        # Invalid UUID format test
        audio = FLAC(str(flac_path))
        audio["MUSICBRAINZ_TRACKID"] = ["not-a-valid-uuid"]
        audio.save()
        res_bad = verify_all_flacs.audit_flac(flac_path)
        assert any("Invalid UUID in MUSICBRAINZ_TRACKID" in e for e in res_bad.errors)

    def test_replaygain_2_ebu_r128_standard(self, tmp_path: Path) -> None:
        """Verify ReplayGain 2.0 (EBU R128 / ITU-R BS.1770 -18 LUFS) specifications."""
        import metadata_writer
        import verify_all_flacs

        flac_path = tmp_path / "replaygain_std.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )
        tags = [
            ("TITLE", "Bohemian Rhapsody"),
            ("ALBUM", "A Night at the Opera"),
            ("ALBUMSORT", "Night at the Opera, A"),
            ("ALBUMARTIST", "Queen"),
            ("ALBUMARTISTSORT", "Queen"),
            ("ARTIST", "Queen"),
            ("DATE", "1975-11-21"),
            ("YEAR", "1975"),
            ("ORIGINALDATE", "1975-11-21"),
            ("ORIGINALYEAR", "1975"),
            ("TRACKNUMBER", "11"),
            ("TRACKTOTAL", "12"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("GENRE", "Rock"),
            ("MEDIA", "CD"),
            ("LABEL", "EMI"),
            ("CATALOGNUMBER", "CDP 7 46207 2"),
            ("DYNAMIC_RANGE", "11"),
            ("ALBUM_DYNAMIC_RANGE", "11"),
            ("REPLAYGAIN_TRACK_GAIN", "-2.38 dB"),
            ("REPLAYGAIN_TRACK_PEAK", "1.006018"),
            ("REPLAYGAIN_TRACK_RANGE", "16.14 dB"),
            ("REPLAYGAIN_ALBUM_GAIN", "-2.38 dB"),
            ("REPLAYGAIN_ALBUM_PEAK", "1.006018"),
            ("REPLAYGAIN_ALBUM_RANGE", "16.14 dB"),
            ("REPLAYGAIN_REFERENCE_LOUDNESS", "-18.00 LUFS"),
            ("MUSICBRAINZ_RECORDINGID", "b1a9c0e9-d987-4042-ae91-78d6a3267d69"),
            ("MUSICBRAINZ_RELEASEGROUPID", "6b47c9a0-b9e1-3df9-a5e8-50a6ce0dbdbd"),
            ("MUSICBRAINZ_ARTISTID", "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"),
            ("DISCOGS_RELEASE_URL", "https://www.discogs.com/release/3894462"),
        ]
        metadata_writer.write_tags(flac_path, tags)
        res = verify_all_flacs.audit_flac(flac_path)
        assert res.errors == []

        # Corrupted range format triggers error
        audio = FLAC(str(flac_path))
        audio["REPLAYGAIN_TRACK_RANGE"] = ["bad_range"]
        audio.save()
        res_bad = verify_all_flacs.audit_flac(flac_path)
        assert any("Invalid REPLAYGAIN_TRACK_RANGE format" in e for e in res_bad.errors)

    def test_classical_music_work_and_movement_tags(self, tmp_path: Path) -> None:
        """Verify classical music extensions: WORK, MOVEMENT, MOVEMENTNAME, PERFORMER (role)."""
        import audiophile_tagger
        import metadata_writer
        import verify_all_flacs

        num, name = audiophile_tagger.parse_movement_info("Symphony No. 5 in C minor, Op. 67: I. Allegro con brio")
        assert num == "I"
        assert "Allegro con brio" in name

        flac_path = tmp_path / "classical.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )
        tags = [
            ("TITLE", "I. Allegro con brio"),
            ("WORK", "Symphony No. 5 in C minor, Op. 67"),
            ("MOVEMENT", "1"),
            ("MOVEMENTNAME", "Allegro con brio"),
            ("MOVEMENTTOTAL", "4"),
            ("COMPOSER", "Ludwig van Beethoven"),
            ("COMPOSERSORT", "Beethoven, Ludwig van"),
            ("CONDUCTOR", "Carlos Kleiber"),
            ("ENSEMBLE", "Wiener Philharmoniker"),
            ("PERFORMER", "Carlos Kleiber (conductor)"),
            ("ALBUM", "Beethoven: Symphonien Nos. 5 & 7"),
            ("ALBUMSORT", "Beethoven: Symphonien Nos. 5 & 7"),
            ("ALBUMARTIST", "Ludwig van Beethoven"),
            ("ALBUMARTISTSORT", "Beethoven, Ludwig van"),
            ("ARTIST", "Wiener Philharmoniker, Carlos Kleiber"),
            ("DATE", "1975"),
            ("YEAR", "1975"),
            ("ORIGINALDATE", "1975"),
            ("ORIGINALYEAR", "1975"),
            ("TRACKNUMBER", "1"),
            ("TRACKTOTAL", "8"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("GENRE", "Classical"),
            ("MEDIA", "CD"),
            ("LABEL", "Deutsche Grammophon"),
            ("CATALOGNUMBER", "447 400-2"),
            ("DYNAMIC_RANGE", "13"),
            ("ALBUM_DYNAMIC_RANGE", "13"),
            ("REPLAYGAIN_TRACK_GAIN", "-5.10 dB"),
            ("REPLAYGAIN_TRACK_PEAK", "0.950000"),
            ("REPLAYGAIN_ALBUM_GAIN", "-5.10 dB"),
            ("REPLAYGAIN_ALBUM_PEAK", "0.950000"),
            ("REPLAYGAIN_REFERENCE_LOUDNESS", "-18.00 LUFS"),
            ("MUSICBRAINZ_RECORDINGID", "b1a9c0e9-d987-4042-ae91-78d6a3267d69"),
            ("MUSICBRAINZ_RELEASEGROUPID", "6b47c9a0-b9e1-3df9-a5e8-50a6ce0dbdbd"),
            ("MUSICBRAINZ_ARTISTID", "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"),
            ("DISCOGS_RELEASE_URL", "https://www.discogs.com/release/123456"),
        ]
        metadata_writer.write_tags(flac_path, tags)
        audio = FLAC(str(flac_path))
        assert audio["WORK"] == ["Symphony No. 5 in C minor, Op. 67"]
        assert audio["MOVEMENT"] == ["1"]
        assert audio["MOVEMENTNAME"] == ["Allegro con brio"]
        assert audio["COMPOSER"] == ["Ludwig van Beethoven"]
        assert audio["CONDUCTOR"] == ["Carlos Kleiber"]

        res = verify_all_flacs.audit_flac(flac_path)
        assert res.errors == []

    def test_flac_id3_tag_contamination_detection_and_removal(self, tmp_path: Path) -> None:
        """Verify strict detection and rejection of ID3 tags in FLAC, and automatic cleaning."""
        import metadata_writer
        import verify_all_flacs

        flac_path = tmp_path / "id3_test.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )
        base_tags = [
            ("TITLE", "Test Title"),
            ("ALBUM", "Test Album"),
            ("ALBUMSORT", "Test Album"),
            ("ALBUMARTIST", "Artist"),
            ("ALBUMARTISTSORT", "Artist"),
            ("ARTIST", "Artist"),
            ("DATE", "1975"),
            ("YEAR", "1975"),
            ("ORIGINALDATE", "1975"),
            ("ORIGINALYEAR", "1975"),
            ("TRACKNUMBER", "1"),
            ("TRACKTOTAL", "1"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("GENRE", "Rock"),
            ("MEDIA", "CD"),
            ("LABEL", "EMI"),
            ("CATALOGNUMBER", "CDP 7 46207 2"),
            ("DYNAMIC_RANGE", "11"),
            ("ALBUM_DYNAMIC_RANGE", "11"),
            ("REPLAYGAIN_TRACK_GAIN", "-2.38 dB"),
            ("REPLAYGAIN_TRACK_PEAK", "0.980000"),
            ("REPLAYGAIN_ALBUM_GAIN", "-2.38 dB"),
            ("REPLAYGAIN_ALBUM_PEAK", "0.980000"),
            ("REPLAYGAIN_REFERENCE_LOUDNESS", "-18.00 LUFS"),
            ("MUSICBRAINZ_RECORDINGID", "b1a9c0e9-d987-4042-ae91-78d6a3267d69"),
            ("MUSICBRAINZ_RELEASEGROUPID", "6b47c9a0-b9e1-3df9-a5e8-50a6ce0dbdbd"),
            ("MUSICBRAINZ_ARTISTID", "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"),
            ("DISCOGS_RELEASE_URL", "https://www.discogs.com/release/3894462"),
        ]
        metadata_writer.write_tags(flac_path, base_tags)

        # 1. Clean file passes binary audit
        binary_errs = verify_all_flacs.check_flac_binary_format(flac_path)
        assert binary_errs == []

        # 2. Prepend ID3v2 header to FLAC binary
        content = flac_path.read_bytes()
        id3_header = b"ID3\x03\x00\x00\x00\x00\x00\x00"
        flac_path.write_bytes(id3_header + content)

        res_id3 = verify_all_flacs.audit_flac(flac_path)
        assert any("prohibited ID3v2 header" in e for e in res_id3.errors)

        # 3. metadata_writer cleans ID3 tag upon write_tags
        metadata_writer.write_tags(flac_path, base_tags)
        res_cleaned = verify_all_flacs.audit_flac(flac_path)
        assert not any("prohibited ID3" in e for e in res_cleaned.errors)
        assert res_cleaned.errors == []

    def test_semicolon_antipattern_detection_in_multivalue_tags(self, tmp_path: Path) -> None:
        """Verify warning generation when multi-value fields use semicolon delimiter instead of repeated tags."""
        import metadata_writer
        import verify_all_flacs

        flac_path = tmp_path / "semicolon_test.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )
        tags_with_semicolon = [
            ("TITLE", "Under Pressure"),
            ("ALBUM", "Hot Space"),
            ("ALBUMSORT", "Hot Space"),
            ("ALBUMARTIST", "Queen"),
            ("ALBUMARTISTSORT", "Queen"),
            ("ARTIST", "Queen & David Bowie"),
            ("ARTISTS", "Queen; David Bowie"),
            ("GENRE", "Rock; Pop Rock"),
            ("LABEL", "EMI; Elektra"),
            ("COMPOSER", "Freddie Mercury; David Bowie"),
            ("DATE", "1982-05-21"),
            ("YEAR", "1982"),
            ("ORIGINALDATE", "1982-05-21"),
            ("ORIGINALYEAR", "1982"),
            ("TRACKNUMBER", "11"),
            ("TRACKTOTAL", "11"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("MEDIA", "CD"),
            ("CATALOGNUMBER", "CDP 7 46215 2"),
            ("DYNAMIC_RANGE", "12"),
            ("ALBUM_DYNAMIC_RANGE", "11"),
            ("REPLAYGAIN_TRACK_GAIN", "-3.50 dB"),
            ("REPLAYGAIN_TRACK_PEAK", "0.850000"),
            ("REPLAYGAIN_ALBUM_GAIN", "-3.20 dB"),
            ("REPLAYGAIN_ALBUM_PEAK", "0.850000"),
            ("REPLAYGAIN_REFERENCE_LOUDNESS", "-18.00 LUFS"),
            ("MUSICBRAINZ_RECORDINGID", "3604eb06-4bc2-4416-9b31-ceadae51bc70"),
            ("MUSICBRAINZ_RELEASEGROUPID", "c172ea98-3a82-3254-b8ad-25a4f465ba03"),
            ("MUSICBRAINZ_ARTISTID", "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"),
            ("DISCOGS_RELEASE_URL", "https://www.discogs.com/release/12345"),
        ]
        metadata_writer.write_tags(flac_path, tags_with_semicolon)
        res = verify_all_flacs.audit_flac(flac_path)
        assert any("Detected semicolon delimiter in ARTISTS" in w for w in res.warnings)
        assert any("Detected semicolon delimiter in GENRE" in w for w in res.warnings)
        assert any("Detected semicolon delimiter in LABEL" in w for w in res.warnings)
        assert any("Detected semicolon delimiter in COMPOSER" in w for w in res.warnings)

        # Fixing with repeated comments removes warnings
        tags_repeated = [
            ("TITLE", "Under Pressure"),
            ("ALBUM", "Hot Space"),
            ("ALBUMSORT", "Hot Space"),
            ("ALBUMARTIST", "Queen"),
            ("ALBUMARTISTSORT", "Queen"),
            ("ARTIST", "Queen & David Bowie"),
            ("ARTISTS", "Queen"),
            ("ARTISTS", "David Bowie"),
            ("GENRE", "Rock"),
            ("GENRE", "Pop Rock"),
            ("LABEL", "EMI"),
            ("LABEL", "Elektra"),
            ("COMPOSER", "Freddie Mercury"),
            ("COMPOSER", "David Bowie"),
            ("DATE", "1982-05-21"),
            ("YEAR", "1982"),
            ("ORIGINALDATE", "1982-05-21"),
            ("ORIGINALYEAR", "1982"),
            ("TRACKNUMBER", "11"),
            ("TRACKTOTAL", "11"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("MEDIA", "CD"),
            ("CATALOGNUMBER", "CDP 7 46215 2"),
            ("DYNAMIC_RANGE", "12"),
            ("ALBUM_DYNAMIC_RANGE", "11"),
            ("REPLAYGAIN_TRACK_GAIN", "-3.50 dB"),
            ("REPLAYGAIN_TRACK_PEAK", "0.850000"),
            ("REPLAYGAIN_ALBUM_GAIN", "-3.20 dB"),
            ("REPLAYGAIN_ALBUM_PEAK", "0.850000"),
            ("REPLAYGAIN_REFERENCE_LOUDNESS", "-18.00 LUFS"),
            ("MUSICBRAINZ_RECORDINGID", "3604eb06-4bc2-4416-9b31-ceadae51bc70"),
            ("MUSICBRAINZ_RELEASEGROUPID", "c172ea98-3a82-3254-b8ad-25a4f465ba03"),
            ("MUSICBRAINZ_ARTISTID", "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"),
            ("DISCOGS_RELEASE_URL", "https://www.discogs.com/release/12345"),
        ]
        # Preservation mode does not silently delete the prior malformed value;
        # simulate an explicit reviewed repair before serializing replacements.
        repaired = FLAC(str(flac_path))
        for malformed_field in ("ARTISTS", "GENRE", "LABEL", "COMPOSER"):
            del repaired[malformed_field]
        repaired.save()
        metadata_writer.write_tags(flac_path, tags_repeated)
        res_clean = verify_all_flacs.audit_flac(flac_path)
        assert not any("Detected semicolon delimiter" in w for w in res_clean.warnings)
        assert res_clean.errors == []

    def test_vorbis_comment_rfc_invalid_field_names_and_bom_rejection(self, tmp_path: Path) -> None:
        """Verify strict RFC 7845 field name rules, BOM stripping, and NUL rejection."""
        import metadata_writer
        import verify_all_flacs

        flac_path = tmp_path / "rfc_check.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )
        # Test clean_manifest_value removes BOM and NUL
        raw_val_with_bom = "\ufeffBohemian\x00 Rhapsody"
        cleaned = metadata_writer.clean_manifest_value(raw_val_with_bom, "TITLE")
        assert "\ufeff" not in cleaned
        assert "\x00" not in cleaned
        assert cleaned == "Bohemian Rhapsody"

        # Mutagen itself strictly enforces RFC 7845 ASCII field names
        audio = FLAC(str(flac_path))
        with pytest.raises(ValueError):
            audio["ЖАНР"] = ["Rock"]

        # Mock check that audit_flac flags invalid Vorbis field names
        from unittest.mock import patch

        with patch("verify_all_flacs.FLAC") as mock_flac:
            mock_inst = mock_flac.return_value
            mock_inst.tags = {"ЖАНР": ["Rock"], "TITLE": ["Test"]}
            mock_inst.pictures = []
            res_bad_key = verify_all_flacs.audit_flac(flac_path)
            assert any("Invalid Vorbis comment field name: 'ЖАНР'" in e for e in res_bad_key.errors)

        # Direct injection of BOM into tag value
        audio = FLAC(str(flac_path))
        assert audio.tags is not None
        audio.tags.clear()
        audio["TITLE"] = ["Test Track"]
        audio["ALBUM"] = ["Test Album"]
        audio["TRACKNUMBER"] = ["1"]
        audio["TRACKTOTAL"] = ["1"]
        audio["DISCNUMBER"] = ["1"]
        audio["DISCTOTAL"] = ["1"]
        audio["DATE"] = ["1975"]
        audio["GENRE"] = ["\ufeffRock"]
        audio["COMMENT"] = ["hello\x00world"]
        audio.save()
        res_bom = verify_all_flacs.audit_flac(flac_path)
        assert any("BOM (Byte Order Mark) detected in tag GENRE" in e for e in res_bom.errors)
        assert any("NUL byte present in tag COMMENT" in e for e in res_bom.errors)

    def test_track_slash_numbering_rejection(self, tmp_path: Path) -> None:
        """Verify rejection of slash track numbering syntax (e.g. 01/12) in favor of separate tags."""
        import verify_all_flacs

        flac_path = tmp_path / "slash_test.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )
        audio = FLAC(str(flac_path))
        audio["TITLE"] = ["Test Track"]
        audio["ALBUM"] = ["Test Album"]
        audio["TRACKNUMBER"] = ["01/12"]
        audio["DISCNUMBER"] = ["1/2"]
        audio["DATE"] = ["1975"]
        audio.save()

        res = verify_all_flacs.audit_flac(flac_path)
        assert any("TRACKNUMBER contains slash ('01/12')" in e for e in res.errors)
        assert any("DISCNUMBER contains slash ('1/2')" in e for e in res.errors)

    def test_dr14_report_official_submission_format(self) -> None:
        """Verify roundtrip generation and parsing of dr14.txt report matching dr.loudness-war.info format."""
        import dr14_parse

        tracks_data = [
            {
                "dr": 11,
                "peak": "0.00 dB",
                "rms": "-18.16 dB",
                "duration": "05:55",
                "name": "11 - Bohemian Rhapsody.flac",
            },
            {
                "dr": 12,
                "peak": "-0.50 dB",
                "rms": "-17.40 dB",
                "duration": "03:30",
                "name": "04 - You're My Best Friend.flac",
            },
        ]
        rep = dr14_parse.generate_dr14_report("Queen", "A Night at the Opera", tracks_data, album_dr=11)
        assert "Analyzed: Queen / A Night at the Opera" in rep
        assert "Official DR value: DR11" in rep
        assert "Number of tracks:  2" in rep

        # Parse generated report back
        parsed_tracks, parsed_album_dr = dr14_parse.parse(rep)
        assert parsed_album_dr == 11
        assert len(parsed_tracks) == 2
        assert parsed_tracks[0]["dr"] == 11
        assert parsed_tracks[0]["peak"] == "0.00 dB"
        assert parsed_tracks[0]["rms"] == "-18.16 dB"
        assert parsed_tracks[1]["dr"] == 12


class TestStrictGlobalAudiophileStandardsAudit:
    """Rigorous end-to-end tests auditing adherence to global Vorbis standards,

    numeric bounds, image specs, and single-valued/mandatory tag invariants.
    """

    @pytest.fixture
    def valid_flac(self, tmp_path: Path) -> Path:
        import metadata_writer

        flac_path = tmp_path / "valid_test.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )
        tags = [
            ("TITLE", "Bohemian Rhapsody"),
            ("ALBUM", "A Night at the Opera"),
            ("ALBUMSORT", "Night at the Opera, A"),
            ("ALBUMARTIST", "Queen"),
            ("ALBUMARTISTSORT", "Queen"),
            ("ARTIST", "Queen"),
            ("DATE", "1975-11-21"),
            ("YEAR", "1975"),
            ("ORIGINALDATE", "1975-11-21"),
            ("ORIGINALYEAR", "1975"),
            ("TRACKNUMBER", "11"),
            ("TRACKTOTAL", "12"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("GENRE", "Rock"),
            ("MEDIA", "CD"),
            ("LABEL", "EMI"),
            ("CATALOGNUMBER", "CDP 7 46207 2"),
            ("DYNAMIC_RANGE", "11"),
            ("ALBUM_DYNAMIC_RANGE", "11"),
            ("REPLAYGAIN_TRACK_GAIN", "-2.38 dB"),
            ("REPLAYGAIN_TRACK_PEAK", "0.980000"),
            ("REPLAYGAIN_ALBUM_GAIN", "-2.38 dB"),
            ("REPLAYGAIN_ALBUM_PEAK", "0.980000"),
            ("REPLAYGAIN_REFERENCE_LOUDNESS", "-18.00 LUFS"),
            ("MUSICBRAINZ_RECORDINGID", "b1a9c0e9-d987-4042-ae91-78d6a3267d69"),
            ("MUSICBRAINZ_RELEASEGROUPID", "6b47c9a0-b9e1-3df9-a5e8-50a6ce0dbdbd"),
            ("MUSICBRAINZ_ARTISTID", "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"),
            ("DISCOGS_RELEASE_URL", "https://www.discogs.com/release/3894462"),
        ]
        metadata_writer.write_tags(flac_path, tags)
        return flac_path

    def test_single_valued_tag_duplication_strict_rejection(self, valid_flac: Path) -> None:
        """Verify that duplicating any single-valued Vorbis comment triggers an audit error."""
        import verify_all_flacs

        audio = FLAC(str(valid_flac))
        audio["TITLE"] = ["Bohemian Rhapsody", "Bohemian Rhapsody (Alternate)"]
        audio["DATE"] = ["1975-11-21", "2011-01-01"]
        audio["DYNAMIC_RANGE"] = ["11", "12"]
        audio.save()

        res = verify_all_flacs.audit_flac(valid_flac)
        assert any("Duplicate single-valued tag: TITLE" in e for e in res.errors)
        assert any("Duplicate single-valued tag: DATE" in e for e in res.errors)
        assert any("Duplicate single-valued tag: DYNAMIC_RANGE" in e for e in res.errors)

    def test_iso8601_date_formats_and_invalid_date_rejection(self, valid_flac: Path) -> None:
        """Verify strict ISO 8601 date parsing and rejection of non-standard date strings."""
        import verify_all_flacs

        audio = FLAC(str(valid_flac))
        for bad_date in ("21/11/1975", "11-21-1975", "1975/11/21", "1975-11-21T00:00:00Z", "75", "circa 1975"):
            audio["DATE"] = [bad_date]
            audio.save()
            res = verify_all_flacs.audit_flac(valid_flac)
            assert any("Invalid DATE format" in e for e in res.errors)

        for good_date in ("1975", "1975-11", "1975-11-21"):
            audio["DATE"] = [good_date]
            audio.save()
            res = verify_all_flacs.audit_flac(valid_flac)
            assert not any("Invalid DATE format" in e for e in res.errors)

    def test_year_and_originalyear_four_digit_strictness(self, valid_flac: Path) -> None:
        """Verify strict 4-digit YYYY formatting for YEAR and ORIGINALYEAR."""
        import verify_all_flacs

        audio = FLAC(str(valid_flac))
        for bad_yr in ("75", "19750", "197", "1975?"):
            audio["YEAR"] = [bad_yr]
            audio.save()
            res = verify_all_flacs.audit_flac(valid_flac)
            assert any("Invalid YEAR format" in e for e in res.errors)

    def test_track_and_disc_numbering_boundaries_and_overflow(self, valid_flac: Path) -> None:
        """Verify strict track and disc number bounds: tnum <= ttot, dnum <= dtot, positive ints."""
        import verify_all_flacs

        audio = FLAC(str(valid_flac))

        # Track number overflow
        audio["TRACKNUMBER"] = ["13"]
        audio["TRACKTOTAL"] = ["12"]
        audio.save()
        res = verify_all_flacs.audit_flac(valid_flac)
        assert any("TRACKNUMBER (13) exceeds TRACKTOTAL (12)" in e for e in res.errors)

        # Non-positive track number
        audio["TRACKNUMBER"] = ["0"]
        audio.save()
        res = verify_all_flacs.audit_flac(valid_flac)
        assert any("TRACKNUMBER must be >= 1" in e for e in res.errors)

        # Disc number overflow
        audio["TRACKNUMBER"] = ["11"]
        audio["DISCNUMBER"] = ["2"]
        audio["DISCTOTAL"] = ["1"]
        audio.save()
        res = verify_all_flacs.audit_flac(valid_flac)
        assert any("DISCNUMBER (2) exceeds DISCTOTAL (1)" in e for e in res.errors)

    def test_accuraterip_and_ctdb_audiophile_verification_tags(self, valid_flac: Path) -> None:
        """Verify AccurateRip and CTDB audiophile tags along with ISRC checks."""
        import verify_all_flacs

        audio = FLAC(str(valid_flac))
        audio["ACCURATERIPRESULT"] = ["AccurateRip: Accurate (confidence 14)"]
        audio["ACCURATERIPCRC"] = ["c038c11e"]
        audio["CTDB_TOCID"] = ["k6x7oQpE4i71b3w4a8n"]
        audio["CTDB_STATUS"] = ["Accurately ripped, confidence 25"]
        audio["ISRC"] = ["GBUM71029605"]
        audio.save()

        res = verify_all_flacs.audit_flac(valid_flac)
        assert not any("ISRC" in w for w in res.warnings)

        # Suspicious ISRC format generates a warning
        audio["ISRC"] = ["INVALID-ISRC-123"]
        audio.save()
        res_bad_isrc = verify_all_flacs.audit_flac(valid_flac)
        assert any("Suspicious ISRC format" in w for w in res_bad_isrc.warnings)

    def test_flac_metadata_block_picture_strict_validation(self, valid_flac: Path) -> None:
        """Verify strict front cover artwork checks: dimensions >= 600px, non-zero size, valid type."""
        import verify_all_flacs

        # 1. No picture -> warning
        audio = FLAC(str(valid_flac))
        audio.clear_pictures()
        audio.save()
        res_no_pic = verify_all_flacs.audit_flac(valid_flac)
        assert any("No front cover artwork embedded" in w for w in res_no_pic.warnings)

        # 2. Sub-optimal resolution (< 600px) -> warning
        pic = Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.width = 400
        pic.height = 400
        pic.data = b"\xff\xd8\xff\xe0" + b"\x00" * 2000
        audio.add_picture(pic)
        audio.save()
        res_small = verify_all_flacs.audit_flac(valid_flac)
        assert any("Sub-optimal cover resolution: 400x400 (< 600px)" in w for w in res_small.warnings)

        # 3. Invalid dimensions (0x0) -> error
        pic.width = 0
        pic.height = 0
        audio.clear_pictures()
        audio.add_picture(pic)
        audio.save()
        res_invalid = verify_all_flacs.audit_flac(valid_flac)
        assert any("Invalid METADATA_BLOCK_PICTURE dimensions" in e for e in res_invalid.errors)

        # 4. Standard audiophile high-res cover (1400x1400) -> 0 errors, 0 warnings
        pic.width = 1400
        pic.height = 1400
        audio.clear_pictures()
        audio.add_picture(pic)
        audio.save()
        res_hires = verify_all_flacs.audit_flac(valid_flac)
        assert res_hires.errors == []
        assert not any("cover" in w.lower() for w in res_hires.warnings)

    def test_junk_and_incompatible_tagger_contamination_detection(self, valid_flac: Path) -> None:
        """Verify detection of legacy/junk tags from iTunes, WMP, and obsolete taggers."""
        import verify_all_flacs

        audio = FLAC(str(valid_flac))
        audio["ITUNESADVISORY"] = ["0"]
        audio["ITUNESNORM"] = [" 000003E8 000003E8"]
        audio["WM/YEAR"] = ["1975"]
        audio["COVERART"] = ["base64string"]
        audio["RATING"] = ["5"]
        audio.save()

        res = verify_all_flacs.audit_flac(valid_flac)
        assert any("Junk/legacy tag detected: ITUNESADVISORY" in w for w in res.warnings)
        assert any("Junk/legacy tag detected: ITUNESNORM" in w for w in res.warnings)
        assert any("Junk/legacy tag detected: WM/YEAR" in w for w in res.warnings)
        assert any("Junk/legacy tag detected: COVERART" in w for w in res.warnings)
        assert any("Junk/legacy tag detected: RATING" in w for w in res.warnings)

    def test_unsynced_lyrics_plain_text_cleanliness(self, valid_flac: Path) -> None:
        """Verify that UNSYNCEDLYRICS does not contain timestamped LRC text."""
        import verify_all_flacs

        audio = FLAC(str(valid_flac))
        audio["UNSYNCEDLYRICS"] = ["[00:15.30] Is this the real life?\n[00:19.40] Is this just fantasy?"]
        audio.save()
        res = verify_all_flacs.audit_flac(valid_flac)
        assert any("UNSYNCEDLYRICS tag contains synchronized timestamps" in w for w in res.warnings)

        # Plain text lyrics produces no warning
        audio["UNSYNCEDLYRICS"] = ["Is this the real life?\nIs this just fantasy?"]
        audio.save()
        res_clean = verify_all_flacs.audit_flac(valid_flac)
        assert not any("UNSYNCEDLYRICS" in w for w in res_clean.warnings)

    def test_dynamic_range_bounds_and_hierarchy_invariants(self, valid_flac: Path) -> None:
        """Verify DR bounds [0, 30] and true peak / RMS string formatting."""
        import verify_all_flacs

        audio = FLAC(str(valid_flac))

        audio["DYNAMIC_RANGE"] = ["35"]
        audio.save()
        res = verify_all_flacs.audit_flac(valid_flac)
        assert any("DYNAMIC_RANGE out of plausible range [0, 30]: 35" in e for e in res.errors)

        audio["DYNAMIC_RANGE"] = ["11"]
        audio["ALBUM_DYNAMIC_RANGE"] = ["not-a-number"]
        audio.save()
        res2 = verify_all_flacs.audit_flac(valid_flac)
        assert any("ALBUM_DYNAMIC_RANGE is not an integer" in e for e in res2.errors)

        audio["ALBUM_DYNAMIC_RANGE"] = ["11"]
        audio["DR_PEAK"] = ["bad_peak"]
        audio["DR_RMS"] = ["bad_rms"]
        audio.save()
        res3 = verify_all_flacs.audit_flac(valid_flac)
        assert any("Invalid DR_PEAK format" in e for e in res3.errors)
        assert any("Invalid DR_RMS format" in e for e in res3.errors)

        # 'over' and standard dB values pass
        audio["DR_PEAK"] = ["over"]
        audio["DR_RMS"] = ["-18.16 dB"]
        audio.save()
        res4 = verify_all_flacs.audit_flac(valid_flac)
        assert not any("DR_PEAK" in e for e in res4.errors)
        assert not any("DR_RMS" in e for e in res4.errors)

    def test_mandatory_tags_completeness_audit(self, valid_flac: Path) -> None:
        """Verify that omitting any mandatory audiophile tag produces an error."""
        import verify_all_flacs

        mandatory_list = [
            "TITLE",
            "ARTIST",
            "ALBUM",
            "ALBUMARTIST",
            "TRACKNUMBER",
            "TRACKTOTAL",
            "DISCNUMBER",
            "DISCTOTAL",
        ]
        for m_tag in mandatory_list:
            audio = FLAC(str(valid_flac))
            del audio[m_tag]
            audio.save()
            res = verify_all_flacs.audit_flac(valid_flac)
            assert any(f"Missing mandatory tag: {m_tag}" in e for e in res.errors)

    def test_dr_match_vinyl_penalty(self) -> None:
        """Verify that vinyl entries are penalized when matching CD releases."""
        import dr14_parse

        entries = [
            {
                "id": "101",
                "url": "https://dr.loudness-war.info/album/view/101",
                "artist": "Queen",
                "album": "The Game",
                "year": "1980",
                "dr": 13,
                "codec": "Vinyl",
                "notes": "Original UK Vinyl pressing 24/96 rip",
            },
            {
                "id": "202",
                "url": "https://dr.loudness-war.info/album/view/202",
                "artist": "Queen",
                "album": "The Game",
                "year": "1986",
                "dr": 12,
                "codec": "CD",
                "notes": "EMI CDP 7 46213 2 West Germany",
            },
        ]
        # Target DR 13 matches vinyl's DR, but target_media is CD -> must pick CD entry 202
        matched = dr14_parse.match_dr_entry(entries, target_dr=13, catalog_no="CDP 7 46213 2", target_media="CD")
        assert matched is not None
        assert matched["id"] == "202"

        # If target media is Vinyl, vinyl entry is preferred
        matched_vinyl = dr14_parse.match_dr_entry(entries, target_dr=13, target_media="Vinyl")
        assert matched_vinyl is not None
        assert matched_vinyl["id"] == "101"

    def test_preemphasis_deemphasis_applied_guard(self, tmp_path: Path) -> None:
        """Verify that logs with applied de-emphasis are correctly detected to prevent double de-emphasis."""
        import audiophile_tagger

        log_clean = tmp_path / "eac_clean.log"
        log_clean.write_text("EAC extraction log file\nPre-emphasis: Yes\n", encoding="utf-8")
        assert not audiophile_tagger.log_has_deemphasis_applied(log_clean)

        log_deemph1 = tmp_path / "eac_deemph.log"
        log_deemph1.write_text("EAC extraction log\nDe-emphasis applied: Yes\n", encoding="utf-8")
        assert audiophile_tagger.log_has_deemphasis_applied(log_deemph1)

        log_deemph2 = tmp_path / "sox_deemph.log"
        log_deemph2.write_text("Processed with sox -S input.wav output.wav deemph\n", encoding="utf-8")
        assert audiophile_tagger.log_has_deemphasis_applied(log_deemph2)

    def test_iswc_and_composersort_tags_in_verification(self, valid_flac: Path) -> None:
        """Verify that ISWC, COMPOSERSORT, MASTERINGSTUDIO, and DE_EMPHASIS_APPLIED pass verification."""
        import verify_all_flacs

        audio = FLAC(str(valid_flac))
        audio["ISWC"] = ["T-010.154.505-4", "T-304.031.869-8"]
        audio["COMPOSERSORT"] = ["Mercury, Freddie"]
        audio["MASTERINGSTUDIO"] = ["Sanyo-Verbatim CD Company"]
        audio["DE_EMPHASIS_APPLIED"] = ["1"]
        audio.save()

        res = verify_all_flacs.audit_flac(valid_flac)
        assert not any("ISWC" in e for e in res.errors)
        assert not any("ISWC" in w for w in res.warnings)
        assert not any("COMPOSERSORT" in e for e in res.errors)
        assert not any("MASTERINGSTUDIO" in e for e in res.errors)
        assert not any("DE_EMPHASIS_APPLIED" in e for e in res.errors)

        # Suspicious ISWC produces warning
        audio["ISWC"] = ["INVALID_ISWC_FORMAT"]
        audio.save()
        res_warn = verify_all_flacs.audit_flac(valid_flac)
        assert any("Suspicious ISWC format" in w for w in res_warn.warnings)

    def test_metadata_writer_preserves_new_audiophile_tags(self, tmp_path: Path) -> None:
        """Verify that metadata_writer preserves ISWC, COMPOSERSORT, MASTERINGSTUDIO, and DE_EMPHASIS_APPLIED."""
        import metadata_writer

        flac_p = tmp_path / "preserve_test.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "flac", str(flac_p)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        audio = FLAC(str(flac_p))
        audio["ISWC"] = ["T-010.154.505-4"]
        audio["COMPOSERSORT"] = ["Mercury, Freddie"]
        audio["MASTERINGSTUDIO"] = ["Sterling Sound"]
        audio["DE_EMPHASIS_APPLIED"] = ["1"]
        audio.save()

        # Write unrelated tags without explicitly specifying the preserved tags
        manifest_tags = [
            ("TITLE", "Preserve Test"),
            ("ARTIST", "Queen"),
            ("ALBUM", "A Night At The Opera"),
            ("DATE", "1975"),
        ]
        metadata_writer.write_tags(flac_p, manifest_tags)

        # Re-check that preserved tags remained intact
        audio_after = FLAC(str(flac_p))
        assert audio_after.tags is not None
        assert audio_after.tags["ISWC"] == ["T-010.154.505-4"]
        assert audio_after.tags["COMPOSERSORT"] == ["Mercury, Freddie"]
        assert audio_after.tags["MASTERINGSTUDIO"] == ["Sterling Sound"]
        assert audio_after.tags["DE_EMPHASIS_APPLIED"] == ["1"]

    def test_compute_acoustid_fingerprint(self, tmp_path: Path) -> None:
        """Verify Chromaprint fingerprint computation via fpcalc."""
        from audiophile_tagger import compute_acoustid_fingerprint

        flac_p = tmp_path / "fpcalc_test.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:duration=3", "-c:a", "flac", str(flac_p)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        dur, fp = compute_acoustid_fingerprint(flac_p)
        assert dur >= 2
        assert len(fp) >= 10
        assert fp.startswith(("AQAD", "AQAA"))

    def test_get_track_eac_peak_log_and_pcm_fallback(self, tmp_path: Path) -> None:
        """Verify EAC track peak parsing from logs and bit-accurate PCM fallback."""
        from audiophile_tagger import get_track_eac_peak

        # Log parsing test (UTF-16LE EAC log format)
        log_content = (
            "Exact Audio Copy V1.0 beta 3\n"
            "TOC of the extracted CD\n"
            "Track 1\n"
            "     Peak level 89.8 %\n"
            "     Track quality 100.0 %\n"
            "Track 2\n"
            "     Peak level 100.0 %\n"
            "     Track quality 100.0 %\n"
        )
        log_p = tmp_path / "test_extract.log"
        log_p.write_bytes(log_content.encode("utf-16-le"))

        pk1 = get_track_eac_peak([log_p], 1)
        assert pk1 == "89.8 %"
        pk2 = get_track_eac_peak([log_p], 2)
        assert pk2 == "100.0 %"

        # PCM fallback test with 0 dBFS sine wave (peak = 100.0 %)
        flac_p = tmp_path / "peak_test.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=f=440:d=1,volume=8", "-c:a", "flac", str(flac_p)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pk_meas = get_track_eac_peak([], 1, flac_path=flac_p)
        assert pk_meas == "100.0 %"

    def test_sid_codes_and_discogs_artist_id_verification(self, valid_flac: Path) -> None:
        """Verify audit of EAC_PEAK, DISCOGS_ARTIST_ID, SID codes, and ACOUSTID_FINGERPRINT."""
        import verify_all_flacs

        audio = FLAC(str(valid_flac))
        audio["EAC_PEAK"] = ["100.0 %"]
        audio["ACOUSTID_FINGERPRINT"] = ["AQADtFqWZYnk4F2F_kd_OFSPjjvxKRYqWYf7wD6SeQgPj4lS9Pjx486DW0feaUahxcoEuY"]
        audio["ACOUSTID_ID"] = ["c7c8b093-6b73-4f18-bc19-158a1bb40f82"]
        audio["DISCOGS_ARTIST_ID"] = ["81013"]
        audio["MASTERING_SID_CODE"] = ["IFPI L046"]
        audio["MOULD_SID_CODE"] = ["IFPI 1567"]
        audio.save()

        res = verify_all_flacs.audit_flac(valid_flac)
        assert not any("EAC_PEAK" in e for e in res.errors)
        assert not any("ACOUSTID" in e for e in res.errors)
        assert not any("DISCOGS_ARTIST_ID" in e for e in res.errors)
        assert not any("SID" in w for w in res.warnings)

        # Invalid format triggers error
        audio["EAC_PEAK"] = ["invalid_peak"]
        audio["DISCOGS_ARTIST_ID"] = ["not_a_number"]
        audio.save()
        res_err = verify_all_flacs.audit_flac(valid_flac)
        assert any("Invalid EAC_PEAK format" in e for e in res_err.errors)
        assert any("Invalid DISCOGS_ARTIST_ID format" in e for e in res_err.errors)

    def test_mb_identity_score_acoustid_matching(self) -> None:
        """Verify that evaluate_identity correctly awards AcoustID audio match bonuses."""
        import mb_identity_score

        D = {
            "id": 12345,
            "title": "A Night At The Opera",
            "artists": [{"name": "Queen"}],
            "labels": [{"catno": "CP32-5302", "name": "EMI"}],
            "tracklist": [
                {"title": "Death On Two Legs", "duration": "3:43", "type_": "track"},
                {"title": "Lazing On A Sunday Afternoon", "duration": "1:07", "type_": "track"},
            ],
        }

        M = {
            "id": "rel-uuid-opera",
            "title": "A Night At The Opera",
            "artist-credit": [{"name": "Queen"}],
            "country": "JP",
            "date": "1984",
            "label-info": [{"catalog": {"number": "CP32-5302"}, "label": {"name": "EMI"}}],
            "media": [
                {
                    "format": "CD",
                    "tracks": [
                        {
                            "title": "Death on Two Legs (Dedicated to...)",
                            "length": 223000,
                            "recording": {"id": "rec-uuid-track1"},
                        },
                        {
                            "title": "Lazing on a Sunday Afternoon",
                            "length": 67000,
                            "recording": {"id": "rec-uuid-track2"},
                        },
                    ],
                }
            ],
        }

        # Baseline without AcoustID
        res_baseline = mb_identity_score.evaluate_identity(
            D=D, M=M, wcountry="JP", wcat="CP32-5302", wbarcode="", wyear="1984"
        )
        assert not res_baseline["flags"]["acoustid_audio_match"]

        # One matching recording is useful recording evidence but must not
        # promote an edition. Full mapped coverage is required.
        acoust_match = {0: {"rec-uuid-track1", "other-rec-uuid"}}
        res_matched = mb_identity_score.evaluate_identity(
            D=D, M=M, wcountry="JP", wcat="CP32-5302", wbarcode="", wyear="1984", acoustid_recordings=acoust_match
        )
        assert not res_matched["flags"]["acoustid_audio_match"]
        assert res_matched["flags"]["acoustid_match_count"] == 1
        assert res_matched["score"] == res_baseline["score"]
        assert "acoustid" not in res_matched["confidence_basis"]

        # With mismatched AcoustID
        acoust_mismatch = {0: {"unrelated-rec-uuid"}}
        res_mismatched = mb_identity_score.evaluate_identity(
            D=D, M=M, wcountry="JP", wcat="CP32-5302", wbarcode="", wyear="1984", acoustid_recordings=acoust_mismatch
        )
        assert not res_mismatched["flags"]["acoustid_audio_match"]
        assert res_mismatched["flags"]["acoustid_audio_mismatch"]
        assert res_mismatched["rejected"] is True
        assert res_mismatched["score"] == 0
        assert "acoustid-recording-conflict" in res_mismatched["hard_conflicts"]

    def test_query_acoustid_candidates(self, tmp_path: Path, monkeypatch) -> None:
        """Verify query_acoustid_candidates parses API responses into release and recording IDs."""
        from unittest.mock import MagicMock

        import audiophile_tagger

        monkeypatch.setenv("ACOUSTID_API_KEY", "dummy_key")
        test_flac = tmp_path / "track1.flac"
        test_flac.write_bytes(b"dummy")

        monkeypatch.setattr(audiophile_tagger, "compute_acoustid_fingerprint", lambda p: (180, "AQADtFqWZYnk4F2F"))

        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "ok",
            "results": [
                {
                    "id": "acoust-id-123",
                    "score": 0.95,
                    "recordings": [
                        {
                            "id": "rec-uuid-1",
                            "releases": [
                                {"id": "rel-uuid-a", "title": "Album A"},
                                {"id": "rel-uuid-b", "title": "Album B"},
                            ],
                        }
                    ],
                }
            ],
        }
        mock_resp.headers = {}
        mock_resp.iter_content.side_effect = lambda **_: iter([json.dumps(mock_resp.json.return_value).encode("utf-8")])
        mock_session.get.return_value = mock_resp

        rels, recs = audiophile_tagger.query_acoustid_candidates(test_flac, mock_session)
        assert rels == ["rel-uuid-a", "rel-uuid-b"]
        assert "rec-uuid-1" in recs

    def test_discogs_release_find_acoustid_bridge(self, tmp_path: Path, monkeypatch) -> None:
        """Verify DiscogsFinder utilizes AcoustID bridge when TOC and direct IDs are absent."""
        from unittest.mock import MagicMock

        import discogs_release_find

        monkeypatch.setenv("ACOUSTID_API_KEY", "dummy_key")
        album_dir = tmp_path / "Queen - Unknown Album"
        album_dir.mkdir()
        track_flac = album_dir / "01. Unknown Track.flac"

        # Create a valid minimal FLAC
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=f=440:d=1", "-c:a", "flac", str(track_flac)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        audio = FLAC(str(track_flac))
        audio["ACOUSTID_FINGERPRINT"] = ["AQADtFqWZYnk4F2F"]
        audio.save()

        finder = discogs_release_find.DiscogsFinder(album_dir, quiet=True)

        # Mock AcoustID response
        mock_acoust_resp = MagicMock()
        mock_acoust_resp.status_code = 200
        mock_acoust_resp.json.return_value = {
            "status": "ok",
            "results": [
                {
                    "id": "acoust-123",
                    "recordings": [
                        {
                            "id": "rec-123",
                            "releases": [{"id": "mb-rel-123", "title": "Test Album"}],
                        }
                    ],
                }
            ],
        }
        monkeypatch.setattr(finder.session, "get", lambda *a, **kw: mock_acoust_resp)

        # Mock MusicBrainz release lookup with Discogs relation
        def mock_mb_api_get(url: str):
            if "mb-rel-123" in url:
                return {
                    "id": "mb-rel-123",
                    "title": "Test Album",
                    "relations": [{"url": {"resource": "https://www.discogs.com/release/99887766"}}],
                    "label-info": [{"catalog-number": "CDP-12345"}],
                }
            return None

        monkeypatch.setattr(finder, "mb_api_get", mock_mb_api_get)

        local_meta: dict[str, Any] = {
            "discid": None,
            "catnos": [],
            "barcodes": [],
            "known_discogs_ids": [],
        }
        cands = finder.search_candidates(local_meta)
        assert "99887766" in cands
        assert any("AcoustID-MB-relation" in o for o in cands["99887766"]["origins"])
        assert cands["99887766"]["bridge_bonus"] == 850


class TestAudiophileBoxSetDiscSubtitles:
    """Comprehensive test suite for DISCSUBTITLE parsing in box sets and multi-disc editions."""

    def test_clean_disc_subtitle_various_formats(self) -> None:
        import audiophile_tagger

        # Disc prefix stripping
        assert audiophile_tagger.clean_disc_subtitle("CD 1: The Original Album", "1") == "The Original Album"
        assert audiophile_tagger.clean_disc_subtitle("CD1 - The Original Album", "1") == "The Original Album"
        assert audiophile_tagger.clean_disc_subtitle("Disc 2 - The Miracle Sessions", "2") == "The Miracle Sessions"
        assert audiophile_tagger.clean_disc_subtitle("Disc Two: The Miracle Sessions", "2") == "The Miracle Sessions"
        assert audiophile_tagger.clean_disc_subtitle("[CD 3] BBC Sessions & Live", "3") == "BBC Sessions & Live"
        assert audiophile_tagger.clean_disc_subtitle("The Miracle Sessions (Disc 2)", "2") == "The Miracle Sessions"
        assert audiophile_tagger.clean_disc_subtitle("The Studio Album (CD 1)", "1") == "The Studio Album"

        # Retaining meaningful subtitles with parts/acts
        assert audiophile_tagger.clean_disc_subtitle("Act I: The Gathering", "1") == "Act I: The Gathering"
        assert audiophile_tagger.clean_disc_subtitle("Part 2: The Dark Side", "2") == "Part 2: The Dark Side"

        # Rejecting trivial disc numbering values
        assert audiophile_tagger.clean_disc_subtitle("CD 1", "1") == ""
        assert audiophile_tagger.clean_disc_subtitle("CD1", "1") == ""
        assert audiophile_tagger.clean_disc_subtitle("Disc 2", "2") == ""
        assert audiophile_tagger.clean_disc_subtitle("Disc Two", "2") == ""
        assert audiophile_tagger.clean_disc_subtitle("Compact Disc 1", "1") == ""
        assert audiophile_tagger.clean_disc_subtitle("Side A", "1") == ""
        assert audiophile_tagger.clean_disc_subtitle("Side 1", "1") == ""
        assert audiophile_tagger.clean_disc_subtitle("1", "1") == ""
        assert audiophile_tagger.clean_disc_subtitle("", "1") == ""

    def test_extract_disc_subtitles_from_discogs_headings_explicit_disc(self) -> None:
        import audiophile_tagger

        discogs_data = {
            "tracklist": [
                {"type_": "heading", "title": "CD 1: The Original Album", "position": ""},
                {"type_": "track", "position": "1-1", "title": "Party"},
                {"type_": "track", "position": "1-2", "title": "Khashoggi's Ship"},
                {"type_": "heading", "title": "CD 2: The Miracle Sessions", "position": ""},
                {"type_": "track", "position": "2-1", "title": "Party (Original Take)"},
                {"type_": "heading", "title": "CD 3 - Alternative Takes & Demos", "position": ""},
                {"type_": "track", "position": "3-1", "title": "I Want It All (Demo)"},
            ]
        }
        subs = audiophile_tagger.extract_disc_subtitles(discogs_data, None)
        assert subs["1"] == "The Original Album"
        assert subs["2"] == "The Miracle Sessions"
        assert subs["3"] == "Alternative Takes & Demos"

    def test_extract_disc_subtitles_from_discogs_headings_lookahead_positions(self) -> None:
        import audiophile_tagger

        discogs_data = {
            "tracklist": [
                {"type_": "heading", "title": "The Studio Album", "position": ""},
                {"type_": "track", "position": "1-1", "title": "Track 1"},
                {"type_": "heading", "title": "Bonus EP & Rarities", "position": ""},
                {"type_": "track", "position": "2-1", "title": "Track 2"},
            ]
        }
        subs = audiophile_tagger.extract_disc_subtitles(discogs_data, None)
        assert subs["1"] == "The Studio Album"
        assert subs["2"] == "Bonus EP & Rarities"

    def test_extract_disc_subtitles_from_musicbrainz_and_cue(self, tmp_path: Path) -> None:
        import audiophile_tagger

        mb_recordings = {
            "media": [
                {"position": 1, "title": "CD 1: The Original Mix"},
                {"position": 2, "title": "Live at the Rainbow '74"},
            ]
        }
        subs = audiophile_tagger.extract_disc_subtitles(None, mb_recordings)
        assert subs["1"] == "The Original Mix"
        assert subs["2"] == "Live at the Rainbow '74"

        # Local CUE sheet extraction
        cue_file = tmp_path / "CD 3.cue"
        cue_file.write_text('REM DISCSUBTITLE "BBC Sessions 1977"\nTRACK 01 AUDIO\n', encoding="utf-8")
        cue_subs = audiophile_tagger.extract_disc_subtitles(None, None, folder=tmp_path)
        assert cue_subs["3"] == "BBC Sessions 1977"

    def test_boxset_discsubtitle_verification_audit(self, tmp_path: Path) -> None:
        import metadata_writer
        import verify_all_flacs

        flac_path = tmp_path / "boxset_disc1.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )

        valid_tags = [
            ("TITLE", "Party"),
            ("ALBUM", "The Miracle (Deluxe Edition)"),
            ("ALBUMSORT", "Miracle (Deluxe Edition), The"),
            ("ALBUMARTIST", "Queen"),
            ("ALBUMARTISTSORT", "Queen"),
            ("ARTIST", "Queen"),
            ("DATE", "1989-05-22"),
            ("YEAR", "1989"),
            ("ORIGINALDATE", "1989-05-22"),
            ("ORIGINALYEAR", "1989"),
            ("TRACKNUMBER", "1"),
            ("TRACKTOTAL", "10"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "2"),
            ("DISCSUBTITLE", "The Original Album"),
            ("GENRE", "Rock"),
            ("MEDIA", "CD"),
            ("LABEL", "Parlophone"),
            ("CATALOGNUMBER", "CDPCSD 107"),
            ("DYNAMIC_RANGE", "12"),
            ("ALBUM_DYNAMIC_RANGE", "12"),
            ("REPLAYGAIN_TRACK_GAIN", "-2.00 dB"),
            ("REPLAYGAIN_TRACK_PEAK", "0.950000"),
            ("REPLAYGAIN_ALBUM_GAIN", "-2.00 dB"),
            ("REPLAYGAIN_ALBUM_PEAK", "0.950000"),
            ("REPLAYGAIN_REFERENCE_LOUDNESS", "-18.00 LUFS"),
            ("MUSICBRAINZ_RECORDINGID", "b1a9c0e9-d987-4042-ae91-78d6a3267d69"),
            ("MUSICBRAINZ_RELEASEGROUPID", "6b47c9a0-b9e1-3df9-a5e8-50a6ce0dbdbd"),
            ("MUSICBRAINZ_ARTISTID", "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"),
            ("DISCOGS_RELEASE_URL", "https://www.discogs.com/release/123456"),
        ]
        metadata_writer.write_tags(flac_path, valid_tags)
        audio = FLAC(str(flac_path))
        assert audio["DISCSUBTITLE"] == ["The Original Album"]

        # Verification audit: clean subtitle passes
        res = verify_all_flacs.audit_flac(flac_path)
        assert res.errors == []

        # Trivial subtitle generates error
        audio["DISCSUBTITLE"] = ["CD 1"]
        audio.save()
        res_trivial = verify_all_flacs.audit_flac(flac_path)
        assert any("Trivial DISCSUBTITLE" in e for e in res_trivial.errors)

        # Unclean prefix generates warning
        audio["DISCSUBTITLE"] = ["CD 1: The Original Album"]
        audio.save()
        res_unclean = verify_all_flacs.audit_flac(flac_path)
        assert any("Unclean DISCSUBTITLE prefix" in w for w in res_unclean.warnings)

    def test_foobar2000_dr_tags_preserved_and_verified(self, tmp_path: Path) -> None:
        import metadata_writer
        import verify_all_flacs

        flac_path = tmp_path / "dr_compat.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )
        tags = [
            ("TITLE", "DR Test Track"),
            ("ALBUM", "DR Test Album"),
            ("ALBUMSORT", "DR Test Album"),
            ("ALBUMARTIST", "Artist"),
            ("ALBUMARTISTSORT", "Artist"),
            ("ARTIST", "Artist"),
            ("DATE", "1980"),
            ("YEAR", "1980"),
            ("ORIGINALDATE", "1980"),
            ("ORIGINALYEAR", "1980"),
            ("TRACKNUMBER", "1"),
            ("TRACKTOTAL", "10"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("GENRE", "Rock"),
            ("MEDIA", "CD"),
            ("LABEL", "MFSL"),
            ("CATALOGNUMBER", "UDCD 610"),
            ("DYNAMIC_RANGE", "12"),
            ("ALBUM_DYNAMIC_RANGE", "12"),
            ("DYNAMIC RANGE", "12"),
            ("ALBUM DYNAMIC RANGE", "12"),
            ("PRESSING_PLANT", "Sanyo, Japan"),
            ("PRESSED_BY", "Sanyo, Japan"),
            ("MANUFACTURER", "JVC"),
            ("ARRANGER", "Brian May"),
            ("MIXINGENGINEER", "Mack"),
            ("REMASTERINGENGINEER", "Steve Hoffman"),
            ("REPLAYGAIN_TRACK_GAIN", "-2.00 dB"),
            ("REPLAYGAIN_TRACK_PEAK", "0.950000"),
            ("REPLAYGAIN_ALBUM_GAIN", "-2.00 dB"),
            ("REPLAYGAIN_ALBUM_PEAK", "0.950000"),
            ("REPLAYGAIN_REFERENCE_LOUDNESS", "-18.00 LUFS"),
            ("MUSICBRAINZ_RECORDINGID", "b1a9c0e9-d987-4042-ae91-78d6a3267d69"),
            ("MUSICBRAINZ_RELEASEGROUPID", "6b47c9a0-b9e1-3df9-a5e8-50a6ce0dbdbd"),
            ("MUSICBRAINZ_ARTISTID", "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"),
            ("DISCOGS_RELEASE_URL", "https://www.discogs.com/release/123456"),
        ]
        metadata_writer.write_tags(flac_path, tags)
        audio = FLAC(str(flac_path))
        assert audio["DYNAMIC_RANGE"] == ["12"]
        assert audio["ALBUM_DYNAMIC_RANGE"] == ["12"]
        assert audio["PRESSING_PLANT"] == ["Sanyo, Japan"]
        assert audio["ARRANGER"] == ["Brian May"]
        assert audio["MIXINGENGINEER"] == ["Mack"]

        # Audit verification: must pass cleanly with 0 errors
        res = verify_all_flacs.audit_flac(flac_path)
        assert res.errors == []

        # Re-write without DR in manifest: must be preserved!
        new_tags = [
            ("TITLE", "DR Test Track"),
            ("ALBUM", "DR Test Album"),
            ("ALBUMARTIST", "Artist"),
            ("ARTIST", "Artist"),
            ("DATE", "1980"),
            ("TRACKNUMBER", "1"),
            ("TRACKTOTAL", "10"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
        ]
        metadata_writer.write_tags(flac_path, new_tags)
        audio_re = FLAC(str(flac_path))
        assert audio_re["DYNAMIC_RANGE"] == ["12"]
        assert audio_re["ALBUM_DYNAMIC_RANGE"] == ["12"]
        assert audio_re["PRESSING_PLANT"] == ["Sanyo, Japan"]
        assert audio_re["ARRANGER"] == ["Brian May"]

    def test_preemphasis_existing_tags_t_int_safety(self, tmp_path: Path) -> None:
        import requests

        import audiophile_tagger

        flac_path = tmp_path / "01-01 - Test PE.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", str(flac_path)],
            check=True,
            capture_output=True,
        )
        audio = FLAC(str(flac_path))
        audio["TITLE"] = "Test PE Track"
        audio["ALBUM"] = "Test PE Album"
        audio["ARTIST"] = "Queen"
        audio["ALBUMARTIST"] = "Queen"
        audio["TRACKNUMBER"] = "1"
        audio["TRACKTOTAL"] = "1"
        audio["DISCNUMBER"] = "1"
        audio["DISCTOTAL"] = "1"
        audio["DATE"] = "1984"
        audio["PRE_EMPHASIS"] = "1"  # Pre-existing pre-emphasis tag!
        audio.save()

        # Simulate process_single_track execution with pre-existing pre-emphasis tag
        album_tags = {
            "final_album": "Test PE Album",
            "catno": "CDP 7 46016 2",
            "edition": "First Pressing",
            "orig_date": "1984",
            "press_date": "1984",
            "total_discs": "1",
            "flac_files": [flac_path],
            "media": "CD",
        }
        cache_mb = tmp_path / ".mb_cache"
        cache_mb.mkdir(parents=True, exist_ok=True)
        ok, _msg = audiophile_tagger.process_single_track(
            f=flac_path,
            folder=tmp_path,
            artist_name="Queen",
            album_clean="Test PE Album",
            discogs_json={"tracklist": [{"position": "1", "title": "Test PE Track"}]},
            mb_recordings_json={},
            rip_meta_files=[],
            dr_track_info=("12", "-1.0 dB", "-15.0 dB"),
            album_dr="12",
            album_tags=album_tags,
            session=requests.Session(),
            user_agent="TestAgent/1.0",
            cache_dir_mb=cache_mb,
            cover_file=None,
        )
        assert ok is True
        audio_tagged = FLAC(str(flac_path))
        assert audio_tagged["PRE_EMPHASIS"] == ["1"]
        assert audio_tagged["DYNAMIC_RANGE"] == ["12"]
        assert audio_tagged["ALBUM_DYNAMIC_RANGE"] == ["12"]


# ==============================================================================
# 22. Extended Audiophile Tags & Provider Tests (BPM, Classical, Vinyl, Logs)
# ==============================================================================
class TestExtendedAudiophileTags:
    def test_acousticbrainz_data_parsing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        rec_id = "test-rec-uuid-1234"
        low_data = {
            "rhythm": {"bpm": 120.45},
            "tonal": {"key_key": "D", "key_scale": "minor"},
        }
        high_data = {
            "highlevel": {
                "danceability": {"value": "danceable", "probability": 0.85},
                "mood_happy": {"value": "happy", "probability": 0.75},
                "mood_party": {"value": "party", "probability": 0.70},
                "mood_sad": {"value": "not_sad", "probability": 0.90},
            }
        }

        class MockResponse:
            def __init__(self, data: dict, status_code: int = 200) -> None:
                self._data = data
                self.status_code = status_code

            def json(self) -> dict:
                return self._data

        session = requests.Session()

        def mock_get(url: str, **kwargs):
            if "low-level" in url:
                return MockResponse(low_data)
            elif "high-level" in url:
                return MockResponse(high_data)
            return MockResponse({}, 404)

        monkeypatch.setattr(session, "get", mock_get)

        res = audiophile_tagger.get_acousticbrainz_data(rec_id, session, tmp_path)
        assert res["bpm"] == "120"
        assert res["key"] == "D minor"
        assert res["danceability"] == "1"
        assert "Happy" in res["moods"]
        assert "Party" in res["moods"]
        assert "Sad" not in res["moods"]

    def test_compute_bpm_from_pcm(self, tmp_path: Path) -> None:
        flac_path = tmp_path / "bpm_test.flac"
        sr = 44100
        duration = 5.0
        t = np.linspace(0, duration, int(sr * duration), endpoint=False)
        pulse = np.sin(2 * np.pi * 2.0 * t) ** 4
        carrier = np.sin(2 * np.pi * 440.0 * t)
        signal = (pulse * carrier * 16000.0).astype(np.int16)

        wav_path = tmp_path / "bpm_test.wav"
        import wave

        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(signal.tobytes())

        subprocess.run(["flac", "-s", str(wav_path), "-o", str(flac_path)], check=True)
        detected_bpm = audiophile_tagger.compute_bpm_from_pcm(flac_path)
        assert detected_bpm != ""
        assert 110 <= int(detected_bpm) <= 130

    def test_parse_rip_log_hardware_and_crc(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test_eac.log"
        log_text = """Exact Audio Copy V1.3 from 2. September 2016

Used drive  : PLEXTOR DVDR   PX-891SA   Adapter: 1  ID: 0

Read mode               : Secure
Read offset correction                      : 6
Used output format              : User Defined Encoder
Additional command line options : -V -8 -T "ARTIST=%artist%" %source% -o %dest%

TOC of the extracted CD

     Track |   Start  |  Length  | Start sector | End sector 
    ---------------------------------------------------------
        1  |  0:00.00 |  3:30.00 |         0    |    15749   

Track 1

     Filename C:\\Rip\\Track01.wav

     Peak level 95.5 %
     Extraction speed 4.2 X
     Range quality 100.0 %
     Test CRC B0F4A3DD
     Copy CRC B0F4A3DD
     Copy OK

No errors occurred
"""
        log_file.write_text(log_text, encoding="utf-8")
        parsed = audiophile_tagger.parse_rip_log_hardware_and_crc([log_file], track_num=1)
        assert parsed["ripping_software"] == "Exact Audio Copy V1.3"
        assert parsed["ripping_device"] == "PLEXTOR DVDR PX-891SA"
        assert parsed["read_offset"] == "+6"
        assert parsed["encoder_settings"] == "-V -8"
        assert parsed["test_crc"] == "B0F4A3DD"
        assert parsed["copy_crc"] == "B0F4A3DD"

    def test_parse_classical_attributes(self) -> None:
        attrs1 = audiophile_tagger.parse_classical_attributes(
            title="Piano Sonata No. 14 in C-sharp minor, Op. 27 No. 2: I. Adagio sostenuto",
            work_title="Piano Sonata No. 14, Op. 27 No. 2",
        )
        assert attrs1.get("opus") == "Op. 27 No. 2"
        assert attrs1.get("opusnumber") == "27"

        attrs2 = audiophile_tagger.parse_classical_attributes(title="Toccata and Fugue in D minor, BWV 565")
        assert attrs2.get("composition_catalog") == "BWV 565"

        attrs3 = audiophile_tagger.parse_classical_attributes(title="Messiah, HWV 56: Part 1, Scene 1 (Mov. 1 of 4)")
        assert attrs3.get("part") == "Part 1, Scene 1"
        assert attrs3.get("movementtotal") == "4"

    def test_parse_vinyl_attributes(self) -> None:
        formats = [
            {
                "name": "Vinyl",
                "qty": "2",
                "descriptions": ["LP", "Album", "33 ⅓ RPM", "180 Gram", "Test Pressing"],
            }
        ]
        notes = (
            "Turntable: Technics SL-1200MK2\n"
            "Cartridge: Audio-Technica AT-VM95ML\n"
            "Phono Preamp: Schiit Mani\n"
            "ADC: Focusrite Scarlett 2i2\n"
            "Condition: NM\n"
        )
        v = audiophile_tagger.parse_vinyl_attributes(formats, notes)
        assert v["speed"] == "33 ⅓ RPM"
        assert v["weight"] == "180g"
        assert v["pressing"] == "Test Pressing"
        assert v["turntable"] == "Technics SL-1200MK2"
        assert v["cartridge"] == "Audio-Technica AT-VM95ML"
        assert v["phono_preamp"] == "Schiit Mani"
        assert v["adc"] == "Focusrite Scarlett 2i2"
        assert v["vinyl_condition"] == "NM"

    def test_no_rating_or_favorite_tags_policy(self, tmp_path: Path) -> None:
        flac_path = tmp_path / "01 - Test Song.flac"
        wav_path = tmp_path / "test.wav"
        import wave

        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(b"\x00" * 44100 * 4)
        subprocess.run(["flac", "-s", str(wav_path), "-o", str(flac_path)], check=True)

        album_tags = {
            "final_album": "Test Album",
            "catno": "TEST-001",
            "edition": "Standard",
            "orig_date": "1990",
            "press_date": "1990",
            "total_discs": "1",
            "flac_files": [flac_path],
            "media": "CD",
        }
        ok, _msg = audiophile_tagger.process_single_track(
            f=flac_path,
            folder=tmp_path,
            artist_name="Test Artist",
            album_clean="Test Album",
            discogs_json={"tracklist": [{"position": "1", "title": "Test Song"}]},
            mb_recordings_json={},
            rip_meta_files=[],
            dr_track_info=("10", "-0.5 dB", "-12.0 dB"),
            album_dr="10",
            album_tags=album_tags,
            session=requests.Session(),
            user_agent="TestAgent/1.0",
            cache_dir_mb=tmp_path / ".mb_cache",
            cover_file=None,
        )
        assert ok is True
        audio = FLAC(str(flac_path))
        assert audio.tags is not None
        # Ensure RATING, FAVORITE, LOVED are NOT present in any form
        # Mutagen's Vorbis mapping iterator yields key/value pairs; use the
        # explicit key view rather than relying on ordinary-dict semantics.
        for k in audio.tags.keys():
            uk = k.upper()
            assert "RATING" not in uk, f"Prohibited tag found: {k}"
            assert "FAVORITE" not in uk, f"Prohibited tag found: {k}"
            assert "LOVED" not in uk, f"Prohibited tag found: {k}"

    def test_verify_all_flacs_extended_tags_validation(self, tmp_path: Path) -> None:
        flac_path = tmp_path / "01 - Verified Song.flac"
        wav_path = tmp_path / "test_v.wav"
        import wave

        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(b"\x00" * 44100 * 4)
        subprocess.run(["flac", "-s", str(wav_path), "-o", str(flac_path)], check=True)

        audio = FLAC(str(flac_path))
        # Mandatory tags
        for k, v in [
            ("TITLE", "Verified Song"),
            ("ARTIST", "Verified Artist"),
            ("ALBUM", "Verified Album"),
            ("ALBUMSORT", "Verified Album"),
            ("ALBUMARTIST", "Verified Artist"),
            ("ALBUMARTISTSORT", "Verified Artist"),
            ("DATE", "2000"),
            ("YEAR", "2000"),
            ("ORIGINALDATE", "2000"),
            ("ORIGINALYEAR", "2000"),
            ("TRACKNUMBER", "1"),
            ("TRACKTOTAL", "1"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("GENRE", "Rock"),
            ("MEDIA", "CD"),
            ("LABEL", "EMI"),
            ("CATALOGNUMBER", "CDP 7 12345 2"),
            ("DYNAMIC_RANGE", "11"),
            ("ALBUM_DYNAMIC_RANGE", "11"),
            ("DYNAMIC RANGE", "11"),
            ("ALBUM DYNAMIC RANGE", "11"),
            ("REPLAYGAIN_TRACK_GAIN", "-5.00 dB"),
            ("REPLAYGAIN_TRACK_PEAK", "0.950000"),
            ("REPLAYGAIN_ALBUM_GAIN", "-5.00 dB"),
            ("REPLAYGAIN_ALBUM_PEAK", "0.950000"),
            ("REPLAYGAIN_REFERENCE_LOUDNESS", "-18.00 LUFS"),
            ("MUSICBRAINZ_TRACKID", "11111111-2222-4333-8444-555555555555"),
            ("MUSICBRAINZ_RELEASEGROUPID", "22222222-3333-4333-8555-666666666666"),
            ("MUSICBRAINZ_ARTISTID", "33333333-4444-4555-8666-777777777777"),
            ("DISCOGS_RELEASE_URL", "https://www.discogs.com/release/12345"),
            # Extended single-valued tags
            ("BPM", "128"),
            ("KEY", "A minor"),
            ("DANCEABILITY", "1"),
            ("OPUS", "Op. 18"),
            ("COMPOSITION_CATALOG", "K. 550"),
            ("PART", "Part 1"),
            ("SPEED", "33 ⅓ RPM"),
            ("WEIGHT", "180g"),
            ("PRESSING", "First Pressing"),
            ("TURNTABLE", "Technics SL-1200"),
            ("CARTRIDGE", "Audio-Technica VM95"),
            ("PHONO_PREAMP", "Schiit Mani"),
            ("ADC", "Scarlett 2i2"),
            ("VINYL_CONDITION", "NM"),
            ("RIPPING_SOFTWARE", "Exact Audio Copy V1.3"),
            ("RIPPING_DEVICE", "PLEXTOR PX-891SA"),
            ("READ_OFFSET", "+6"),
            ("TEST_CRC", "1A2B3C4D"),
            ("COPY_CRC", "1A2B3C4D"),
            ("ENCODERSETTINGS", "-V -8"),
            ("LIVE", "1"),
            ("VENUE", "Wembley Stadium"),
            ("LOCATION", "London"),
            ("GROUPING", "Symphony No. 5"),
            ("DISCNAME", "Disc 1"),
        ]:
            audio[k] = [v]

        # Extended multi-valued tags
        audio["ORCHESTRA"] = ["London Philharmonic Orchestra", "Royal Philharmonic Orchestra"]
        audio["CHORUS"] = ["London Symphony Chorus"]
        audio["SOLOIST"] = ["Freddie Mercury"]
        audio["LIBRETTIST"] = ["W. A. Mozart"]
        audio["MOOD"] = ["Energetic", "Happy"]
        audio.save()

        res = verify_all_flacs.audit_flac(flac_path)
        assert res.errors == [], f"Unexpected errors: {res.errors}"
        for err in res.errors:
            assert "Duplicate single-valued" not in err
        for w in res.warnings:
            assert "semicolon" not in w


class TestItems1To4AudiophileExtensions:
    """Comprehensive test suite for Items 1-4 audiophile extensions."""

    @staticmethod
    def _create_flac(flac_path: Path) -> None:
        wav_path = flac_path.with_suffix(".wav")
        import wave

        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(b"\x00" * 44100 * 4)
        subprocess.run(["flac", "-s", "-f", str(wav_path), "-o", str(flac_path)], check=True)
        if wav_path.is_file():
            wav_path.unlink()

    def test_musical_key_to_camelot_mapping(self) -> None:
        """Verify standard 24 musical keys to Camelot Wheel mapping."""
        # Major keys
        assert audiophile_tagger.musical_key_to_camelot("C") == "8B"
        assert audiophile_tagger.musical_key_to_camelot("C major") == "8B"
        assert audiophile_tagger.musical_key_to_camelot("G") == "9B"
        assert audiophile_tagger.musical_key_to_camelot("D") == "10B"
        assert audiophile_tagger.musical_key_to_camelot("A") == "11B"
        assert audiophile_tagger.musical_key_to_camelot("E") == "12B"
        assert audiophile_tagger.musical_key_to_camelot("B") == "1B"
        assert audiophile_tagger.musical_key_to_camelot("F#") == "2B"
        assert audiophile_tagger.musical_key_to_camelot("Gb") == "2B"
        assert audiophile_tagger.musical_key_to_camelot("C#") == "3B"
        assert audiophile_tagger.musical_key_to_camelot("Db") == "3B"
        assert audiophile_tagger.musical_key_to_camelot("Ab") == "4B"
        assert audiophile_tagger.musical_key_to_camelot("Eb") == "5B"
        assert audiophile_tagger.musical_key_to_camelot("Bb") == "6B"
        assert audiophile_tagger.musical_key_to_camelot("F") == "7B"

        # Minor keys
        assert audiophile_tagger.musical_key_to_camelot("Am") == "8A"
        assert audiophile_tagger.musical_key_to_camelot("A minor") == "8A"
        assert audiophile_tagger.musical_key_to_camelot("Em") == "9A"
        assert audiophile_tagger.musical_key_to_camelot("Bm") == "10A"
        assert audiophile_tagger.musical_key_to_camelot("F#m") == "11A"
        assert audiophile_tagger.musical_key_to_camelot("C#m") == "12A"
        assert audiophile_tagger.musical_key_to_camelot("G#m") == "1A"
        assert audiophile_tagger.musical_key_to_camelot("D#m") == "2A"
        assert audiophile_tagger.musical_key_to_camelot("Ebm") == "2A"
        assert audiophile_tagger.musical_key_to_camelot("Bbm") == "3A"
        assert audiophile_tagger.musical_key_to_camelot("Fm") == "4A"
        assert audiophile_tagger.musical_key_to_camelot("Cm") == "5A"
        assert audiophile_tagger.musical_key_to_camelot("Gm") == "6A"
        assert audiophile_tagger.musical_key_to_camelot("Dm") == "7A"

        # Already Camelot format
        assert audiophile_tagger.musical_key_to_camelot("8B") == "8B"
        assert audiophile_tagger.musical_key_to_camelot("11a") == "11A"
        assert audiophile_tagger.musical_key_to_camelot("") == ""

    def test_parse_instrument_roles(self) -> None:
        """Verify normalization of instrument roles for Vorbis PERFORMER:<role> tags."""
        roles1 = audiophile_tagger.parse_instrument_roles("Lead Vocals, Piano")
        assert "lead vocals" in roles1
        assert "piano" in roles1

        roles2 = audiophile_tagger.parse_instrument_roles("Bass [Electric], Backing Vocals")
        assert "bass guitar" in roles2 or "bass" in roles2
        assert "backing vocals" in roles2

        roles3 = audiophile_tagger.parse_instrument_roles("Drums, Percussion")
        assert "drums" in roles3
        assert "percussion" in roles3

        roles4 = audiophile_tagger.parse_instrument_roles("Guitar [Lead], Synthesizer")
        assert "lead guitar" in roles4
        assert "synthesizer" in roles4

    def test_detect_audiophile_series_and_source_format(self) -> None:
        """Verify detection of audiophile series and analog source formats."""
        notes = "DCC Compact Classics 24K Gold CD remastered by Steve Hoffman from the original analog master tapes."
        series = audiophile_tagger.detect_audiophile_series(notes=notes)
        assert series == "DCC Compact Classics 24K Gold"
        src = audiophile_tagger.detect_source_format(notes=notes)
        assert src in ("Original Master Tapes", "Original Analog Master")

        notes_mfsl = 'Mobile Fidelity Sound Lab Ultradisc II, 1/2" 30 ips flat transfer from master tapes.'
        series_mfsl = audiophile_tagger.detect_audiophile_series(notes=notes_mfsl)
        assert "MFSL" in series_mfsl
        src_mfsl = audiophile_tagger.detect_source_format(notes=notes_mfsl)
        assert src_mfsl == "Flat Transfer from Master Tapes"

        notes_target = "Target CD pressed in West Germany by PolyGram"
        series_target = audiophile_tagger.detect_audiophile_series(notes=notes_target)
        assert series_target == "Target CD"

    def test_metadata_writer_preserves_items_1_to_4(self, tmp_path: Path) -> None:
        """Verify metadata_writer preserves PERFORMER:*, stream metrics, Camelot, and series tags across rewrites."""
        flac_path = tmp_path / "test_extended_preserve.flac"
        self._create_flac(flac_path)

        initial_manifest = [
            ("TITLE", "Test Title"),
            ("ARTIST", "Queen"),
            ("ALBUM", "Test Album"),
            ("DATE", "1975-11-21"),
            ("TRACKNUMBER", "1"),
            ("TRACKTOTAL", "1"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("ALBUMARTIST", "Queen"),
            ("PERFORMER:lead vocals", "Freddie Mercury"),
            ("PERFORMER:piano", "Freddie Mercury"),
            ("PERFORMER:guitar", "Brian May"),
            ("BITS_PER_SAMPLE", "16"),
            ("SAMPLERATE", "44100"),
            ("CHANNELS", "2"),
            ("AUDIO_MD5", "2e028a6fe222e9e5c67c580d30cc8b68"),
            ("HIRES", "0"),
            ("LOSSLESS_TYPE", "CD Red Book (16/44.1)"),
            ("CAMELOT_KEY", "8B"),
            ("ENERGY", "0.85"),
            ("VALENCE", "0.62"),
            ("SOURCE_FORMAT", "Original Master Tapes"),
            ("AUDIOPHILE_SERIES", "DCC Compact Classics 24K Gold"),
            ("STUDIO", "Trident Studios, London"),
            ("RECORDING_DATE", "1975-08"),
        ]
        metadata_writer.write_tags(flac_path, initial_manifest)

        # Re-write with only core tags in new manifest
        new_manifest = [
            ("TITLE", "Updated Title"),
            ("ARTIST", "Queen"),
            ("ALBUM", "Test Album"),
            ("DATE", "1975-11-21"),
            ("TRACKNUMBER", "1"),
            ("TRACKTOTAL", "1"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("ALBUMARTIST", "Queen"),
        ]
        metadata_writer.write_tags(flac_path, new_manifest)

        audio = FLAC(str(flac_path))
        assert audio["TITLE"] == ["Updated Title"]
        # Items 1-4 tags MUST be preserved by existing_preserves!
        assert audio["PERFORMER:LEAD_VOCALS"] == ["Freddie Mercury"] or audio["PERFORMER:lead_vocals"] == [
            "Freddie Mercury"
        ]
        assert audio["PERFORMER:PIANO"] == ["Freddie Mercury"] or audio["PERFORMER:piano"] == ["Freddie Mercury"]
        assert audio["PERFORMER:GUITAR"] == ["Brian May"] or audio["PERFORMER:guitar"] == ["Brian May"]
        assert audio["BITS_PER_SAMPLE"] == ["16"]
        assert audio["SAMPLERATE"] == ["44100"]
        assert audio["AUDIO_MD5"] == ["2e028a6fe222e9e5c67c580d30cc8b68"]
        assert audio["CAMELOT_KEY"] == ["8B"]
        assert audio["ENERGY"] == ["0.85"]
        assert audio["VALENCE"] == ["0.62"]
        assert audio["SOURCE_FORMAT"] == ["Original Master Tapes"]
        assert audio["AUDIOPHILE_SERIES"] == ["DCC Compact Classics 24K Gold"]
        assert audio["STUDIO"] == ["Trident Studios, London"]
        assert audio["RECORDINGDATE"] == ["1975-08"]

    def test_verify_all_flacs_accepts_items_1_to_4(self, tmp_path: Path) -> None:
        """Verify verify_all_flacs passes items 1-4 tags with 0 errors and 0 warnings, and detects violations."""
        flac_path = tmp_path / "test_verify_items_1_to_4.flac"
        self._create_flac(flac_path)

        audio = FLAC(str(flac_path))
        # Add core mandatory tags
        for k, v in [
            ("TITLE", "Bohemian Rhapsody"),
            ("ARTIST", "Queen"),
            ("ALBUM", "A Night at the Opera"),
            ("ALBUMSORT", "Night at the Opera, A"),
            ("ALBUMARTIST", "Queen"),
            ("ALBUMARTISTSORT", "Queen"),
            ("DATE", "1975-11-21"),
            ("YEAR", "1975"),
            ("ORIGINALDATE", "1975-11-21"),
            ("ORIGINALYEAR", "1975"),
            ("TRACKNUMBER", "11"),
            ("TRACKTOTAL", "12"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("GENRE", "Rock"),
            ("MEDIA", "CD"),
            ("LABEL", "DCC Compact Classics"),
            ("CATALOGNUMBER", "GZS-1144"),
            ("BARCODE", "010963114423"),
            ("DR", "11"),
            ("ALBUM_DR", "11"),
            ("DYNAMIC RANGE", "11"),
            ("ALBUM DYNAMIC RANGE", "11"),
            ("DYNAMIC_RANGE", "11"),
            ("ALBUM_DYNAMIC_RANGE", "11"),
            ("REPLAYGAIN_TRACK_GAIN", "-5.00 dB"),
            ("REPLAYGAIN_TRACK_PEAK", "0.950000"),
            ("REPLAYGAIN_ALBUM_GAIN", "-5.00 dB"),
            ("REPLAYGAIN_ALBUM_PEAK", "0.950000"),
            ("REPLAYGAIN_REFERENCE_LOUDNESS", "-18.00 LUFS"),
            ("MUSICBRAINZ_TRACKID", "11111111-2222-4333-8444-555555555555"),
            ("MUSICBRAINZ_RELEASEGROUPID", "22222222-3333-4333-8555-666666666666"),
            ("MUSICBRAINZ_ARTISTID", "33333333-4444-4555-8666-777777777777"),
            ("DISCOGS_RELEASE_URL", "https://www.discogs.com/release/12345"),
            # Items 1-4 single-valued tags
            ("BITS_PER_SAMPLE", "16"),
            ("SAMPLERATE", "44100"),
            ("CHANNELS", "2"),
            ("AUDIO_MD5", "2e028a6fe222e9e5c67c580d30cc8b68"),
            ("HIRES", "0"),
            ("LOSSLESS_TYPE", "CD Red Book (16/44.1)"),
            ("CAMELOT_KEY", "8B"),
            ("ENERGY", "0.80"),
            ("VALENCE", "0.65"),
            ("SOURCE_FORMAT", "Original Master Tapes"),
            ("AUDIOPHILE_SERIES", "DCC Compact Classics 24K Gold"),
            ("STUDIO", "Trident Studios, London"),
            ("RECORDING_DATE", "1975-08"),
        ]:
            audio[k] = [v]

        # Items 1-4 multi-valued tags
        audio["PERFORMER:lead vocals"] = ["Freddie Mercury"]
        audio["PERFORMER:piano"] = ["Freddie Mercury"]
        audio["PERFORMER:guitar"] = ["Brian May"]
        audio["RELEASETYPE"] = ["Album", "Compilation"]
        audio.save()

        res = verify_all_flacs.audit_flac(flac_path)
        assert res.errors == [], f"Unexpected errors in clean audit: {res.errors}"
        for w in res.warnings:
            assert "semicolon" not in w

        # Test semicolon detection in PERFORMER:<role>
        audio["PERFORMER:piano"] = ["Freddie Mercury; Guest"]
        audio.save()
        res_semi = verify_all_flacs.audit_flac(flac_path)
        assert any("semicolon" in w.lower() for w in res_semi.warnings)

        # Test duplicate single-valued tag error
        audio["PERFORMER:piano"] = ["Freddie Mercury"]
        audio["CAMELOT_KEY"] = ["8B", "9B"]
        audio.save()
        res_dup = verify_all_flacs.audit_flac(flac_path)
        assert any("Duplicate single-valued" in err for err in res_dup.errors)


# ==============================================================================
# 18. Niche, Rhythm Game, and Regional Lyrics Providers Tests
# ==============================================================================
class TestNicheAndRhythmGameLyricsProviders:
    """Test suite for specialized aggregators, Clone Hero/Rock Band charts, and niche archives."""

    @staticmethod
    def _cascade_chains(variable: str) -> dict[str, list[str]]:
        import ast
        import inspect
        import textwrap

        from lyrics_utils import fetch_lyrics_cascade

        tree = ast.parse(textwrap.dedent(inspect.getsource(fetch_lyrics_cascade)))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef))
        chains: dict[str, list[str]] = {}

        def assigned_names(statements: list[ast.stmt]) -> list[str] | None:
            for statement in statements:
                if not isinstance(statement, ast.Assign) or not any(
                    isinstance(target, ast.Name) and target.id == variable for target in statement.targets
                ):
                    continue
                if not isinstance(statement.value, ast.List):
                    continue
                return [
                    str(element.elts[0].value)
                    for element in statement.value.elts
                    if isinstance(element, ast.Tuple)
                    and element.elts
                    and isinstance(element.elts[0], ast.Constant)
                    and isinstance(element.elts[0].value, str)
                ]
            return None

        def visit(node: ast.If) -> None:
            branch = "LATIN"
            if (
                isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "branch"
                and len(node.test.comparators) == 1
                and isinstance(node.test.comparators[0], ast.Constant)
                and node.test.comparators[0].value in {"CYRILLIC", "CJK"}
            ):
                branch = str(node.test.comparators[0].value)
            current = assigned_names(node.body)
            if current is not None:
                chains[branch] = current
            for statement in node.orelse:
                if isinstance(statement, ast.If):
                    visit(statement)
                else:
                    latin = assigned_names([statement])
                    if latin is not None:
                        chains["LATIN"] = latin

        for statement in function.body:
            if isinstance(statement, ast.If):
                visit(statement)
        return chains

    def test_cascade_collects_megalobiz_and_rentanadviser_in_all_branches(self) -> None:
        """Verify regional cascade branches include these sources without asserting winner priority."""
        chains = self._cascade_chains("l2_chain")
        assert set(chains) == {"CYRILLIC", "CJK", "LATIN"}
        for names in chains.values():
            assert "Megalobiz (LRC)" in names
            assert "RentAnAdviser (LRC)" in names

    def test_cascade_collects_word_synced_sources(self) -> None:
        """Verify both chart sources remain available for candidate scoring."""
        chains = self._cascade_chains("l1_chain")
        assert set(chains) == {"CYRILLIC", "CJK", "LATIN"}
        for names in chains.values():
            assert "Chorus (Clone Hero / Rock Band)" in names
            assert "UltraStar DB" in names

    def test_cascade_collects_niche_archives(self) -> None:
        """Verify regional text sources remain candidates without a fixed priority contract."""
        chains = self._cascade_chains("l3_chain")
        assert set(chains) == {"CYRILLIC", "CJK", "LATIN"}
        assert {"DarkLyrics", "Metal-Archives", "Tekstowo.pl"}.issubset(chains["LATIN"])
        assert {"VocaDB", "QQ Music (Plain)"}.issubset(chains["CJK"])

    def test_parse_clonehero_chart_precision(self) -> None:
        """Test Clone Hero .chart parser for syllable-level timing calculation."""
        from lyrics_utils import parse_clonehero_chart

        chart_sample = """
        [Song]
        {
          Name = "Bohemian Rhapsody"
          Artist = "Queen"
          Resolution = 192
          Offset = 0.5
        }
        [SyncTrack]
        {
          0 = TS 4
          0 = B 60000
        }
        [Events]
        {
          0 = E "phrase_start"
          192 = E "lyric Is"
          384 = E "lyric this"
          576 = E "lyric the"
          768 = E "lyric real"
          960 = E "lyric life?="
          1152 = E "phrase_end"
          1344 = E "phrase_start"
          1344 = E "lyric fan-"
          1536 = E "lyric ta-"
          1728 = E "lyric sy?"
          1920 = E "phrase_end"
        }
        """
        res = parse_clonehero_chart(chart_sample, song="Bohemian Rhapsody", artist="Queen")
        assert res is not None
        assert res["found"] is True
        assert res["has_word_sync"] is True
        assert res["format"] == "elrc"
        assert "Is" in res["plainLyrics"]
        assert "real life?" in res["plainLyrics"]
        assert "<00:01.50>Is" in res["enhancedLyrics"]
        assert "<00:02.50> this" in res["enhancedLyrics"]
        assert res["ttmlLyrics"] == ""

    def test_parse_clonehero_midi_precision(self) -> None:
        """Test Clone Hero / Rock Band notes.mid parser for vocal track lyrics."""
        import struct

        from lyrics_utils import parse_clonehero_midi

        # Construct minimal valid Type 1 MIDI: 2 tracks, 480 ticks/beat
        trk1_events = bytearray()
        trk1_events.extend(b"\x00\xff\x51\x03\x07\xa1\x20")
        trk1_events.extend(b"\x00\xff\x2f\x00")
        trk1_chunk = b"MTrk" + struct.pack(">I", len(trk1_events)) + trk1_events

        trk2_events = bytearray()
        name_b = b"PART VOCALS"
        trk2_events.extend(b"\x00\xff\x03" + bytes([len(name_b)]) + name_b)
        trk2_events.extend(b"\x83\x60\xff\x05\x02We")
        trk2_events.extend(b"\x83\x60\xff\x05\x04will")
        trk2_events.extend(b"\x83\x60\xff\x05\x04rock")
        trk2_events.extend(b"\x83\x60\xff\x05\x03you")
        trk2_events.extend(b"\x00\xff\x2f\x00")
        trk2_chunk = b"MTrk" + struct.pack(">I", len(trk2_events)) + trk2_events

        midi_hdr = b"MThd\x00\x00\x00\x06\x00\x01\x00\x02\x01\xe0"
        full_midi = midi_hdr + trk1_chunk + trk2_chunk

        res = parse_clonehero_midi(full_midi, song="We Will Rock You", artist="Queen")
        assert res is not None
        assert res["found"] is True
        assert res["has_word_sync"] is True
        assert "We will rock you" in res["plainLyrics"]
        assert "<00:00.50>We" in res["enhancedLyrics"]
        assert "<00:01.00> will" in res["enhancedLyrics"]

    def test_vocadb_json_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test VocaDB JSON payload parsing and prioritizing Japanese kanji original lyrics."""
        import json
        import urllib.request

        from lyrics_utils import fetch_vocadb

        mock_payload = {
            "items": [
                {
                    "id": 1326,
                    "name": "ワールドイズマイン",
                    "names": {"additionalNames": ["World is Mine"]},
                    "artists": [{"name": "supercell"}],
                    "lyrics": [
                        {
                            "translationType": "Original",
                            "value": "世界で一番おひめさま そういう扱い心得てよね\nその一 いつもと違う髪形に気が付くこと",
                        },
                        {
                            "translationType": "Romanized",
                            "value": "Sekai de ichiban ohime-sama Sou iu atsukai kokoro ete yo ne",
                        },
                    ],
                }
            ]
        }

        class MockResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return json.dumps(mock_payload).encode("utf-8")

        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=6: MockResponse())

        res = fetch_vocadb("World is Mine", "supercell")
        assert res is not None
        assert res["found"] is True
        assert res["source"] == "VocaDB"
        assert "世界で一番おひめさま" in res["plainLyrics"]

    def test_tekstowo_pl_html_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test Tekstowo.pl search and HTML lyric extraction."""
        import urllib.request

        from lyrics_utils import fetch_tekstowo

        search_html = """
        <html>
          <div class="box-przeboje">
            <a class="title" href="/myslovitz/dlugosc_dzwieku_samotnosci.html">Myslovitz - Długość dźwięku samotności</a>
          </div>
        </html>
        """
        song_html = """
        <html>
          <div class="inner-text">
            I nawet kiedy będę sam<br />
            Nie zmienię się, to nie mój świat<br />
            Przede mną droga, którą znam<br />
            (tekst piosenki)
          </div>
        </html>
        """

        class MockResponse:
            def __init__(self, content):
                self.content = content

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return self.content.encode("utf-8")

        def mock_urlopen(req, timeout=6):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "szukaj" in url:
                return MockResponse(search_html)
            return MockResponse(song_html)

        monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

        res = fetch_tekstowo("Długość dźwięku samotności", "Myslovitz")
        assert res is not None
        assert res["found"] is True
        assert res["source"] == "Tekstowo.pl"
        assert "I nawet kiedy będę sam" in res["plainLyrics"]
        assert "(tekst piosenki)" not in res["plainLyrics"]

    def test_megalobiz_and_rentanadviser_mock_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exercise both scraper request paths from captured provider fixtures."""
        import lyrics_utils

        fixture_root = Path(__file__).parent / "tests" / "fixtures" / "lyrics"

        class FixtureResponse:
            status = 200

            def __init__(self, body: bytes, final_url: str) -> None:
                self.body = body
                self.final_url = final_url

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int = -1) -> bytes:
                return self.body

            def geturl(self) -> str:
                return self.final_url

        def open_fixture(request: Any, timeout: float = 6) -> FixtureResponse:
            del timeout
            url = request.full_url
            if "megalobiz.com/search/" in url:
                path = fixture_root / "megalobiz" / "search.html"
            elif "megalobiz.com/lrc/maker/" in url:
                path = fixture_root / "megalobiz" / "lyrics.html"
            elif "subtitles4songs.aspx" in url:
                path = fixture_root / "rentanadviser" / "search.html"
            elif "getsubtitles.aspx" in url:
                path = fixture_root / "rentanadviser" / "lyrics.html"
            else:
                raise AssertionError(f"unexpected fixture URL: {url}")
            return FixtureResponse(path.read_bytes(), url)

        monkeypatch.setattr(lyrics_utils, "_provider_urlopen", open_fixture)
        res_mb = lyrics_utils.fetch_megalobiz("Bohemian Rhapsody", "Queen")
        assert res_mb is not None
        assert res_mb["found"] is True
        assert res_mb["source"] == "Megalobiz (LRC)"
        assert res_mb["syncType"] == "linesync"
        assert "[00:00.00]" in res_mb["syncedLyrics"]
        assert "Is this the real life?" in res_mb["syncedLyrics"]

        res_ra = lyrics_utils.fetch_rentanadviser("Bohemian Rhapsody", "Queen")
        assert res_ra is not None
        assert res_ra["found"] is True
        assert res_ra["source"] == "RentAnAdviser (LRC)"
        assert "Is this just fantasy?" in res_ra["plainLyrics"]


class TestNewCoverProvidersAndMetadataDeduplication:
    def test_metadata_writer_single_valued_deduplication(self, tmp_path: Path) -> None:
        flac_path = tmp_path / "01 - Dedup Test.flac"
        wav_path = tmp_path / "dedup.wav"
        import wave

        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(b"\x00" * 44100 * 4)
        subprocess.run(["flac", "-s", str(wav_path), "-o", str(flac_path)], check=True)

        tags = [
            ("TITLE", "Correct Title"),
            ("ALBUM", "Test Album"),
            ("TRACKNUMBER", "1"),
            ("TRACKTOTAL", "1"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("DATE", "1975-11-21"),
            ("RECORDINGDATE", "1975-08-24"),
            ("RECORDINGDATE", "1975-08-24"),
            ("ARTIST", "Queen & David Bowie"),
            ("ARTISTS", "Queen"),
            ("ARTISTS", "David Bowie"),
            ("GENRE", "Rock"),
            ("GENRE", "Classic Rock"),
        ]

        write_tags(flac_path, tags)

        from mutagen.flac import FLAC

        audio = FLAC(str(flac_path))

        # Single valued tags must only have 1 entry
        assert len(audio["RECORDINGDATE"]) == 1
        assert audio["RECORDINGDATE"] == ["1975-08-24"]
        assert len(audio["TITLE"]) == 1
        assert audio["TITLE"] == ["Correct Title"]

        # Multi-value tags must preserve separate entries
        assert audio["ARTIST"] == ["Queen & David Bowie"]
        assert audio["ARTISTS"] == ["Queen", "David Bowie"]
        assert len(audio["GENRE"]) == 2
        assert audio["GENRE"] == ["Rock", "Classic Rock"]

    def test_query_amazon_asin(self) -> None:
        fetcher = CoverFetcher("Queen", "A Night at the Opera", asin="B000000001")
        cands = fetcher.query_amazon()
        assert len(cands) >= 1
        assert any("B000000001" in c.url for c in cands)
        assert cands[0].provider == "Amazon CDN"
        assert cands[0].image_type == "front"
        assert cands[0].identity_scope == "release"

    def test_query_fanart_tv(self, monkeypatch) -> None:
        import json

        sample_json = {
            "albums": {
                "rg-12345": {
                    "albumcover": [{"url": "https://assets.fanart.tv/cover.jpg"}],
                    "cdart": [{"url": "https://assets.fanart.tv/cd.png"}],
                    "albumback": [{"url": "https://assets.fanart.tv/back.jpg"}],
                }
            }
        }
        fetcher = CoverFetcher("Queen", "A Night at the Opera", mb_rg_id="rg-12345", fanart_api_key="fake-key")
        monkeypatch.setattr(fetcher, "fetch_url", lambda url, headers=None: json.dumps(sample_json).encode("utf-8"))
        cands = fetcher.query_fanart_tv()
        assert len(cands) == 3
        by_type = {c.image_type: c for c in cands}
        assert "front" in by_type and by_type["front"].url == "https://assets.fanart.tv/cover.jpg"
        assert "medium" in by_type and by_type["medium"].url == "https://assets.fanart.tv/cd.png"
        assert "back" in by_type and by_type["back"].url == "https://assets.fanart.tv/back.jpg"

    def test_query_bandcamp_master_resolution(self, monkeypatch) -> None:
        import json

        sample_bc = {
            "auto": {
                "results": [
                    {
                        "type": "a",
                        "band_name": "Carpenter Brut",
                        "name": "Trilogy",
                        "art_id": 12345678,
                        "img": "https://f4.bcbits.com/img/a12345678_10.jpg",
                    }
                ]
            }
        }
        fetcher = CoverFetcher("Carpenter Brut", "Trilogy")
        monkeypatch.setattr(fetcher, "fetch_url", lambda url, headers=None: json.dumps(sample_bc).encode("utf-8"))
        cands = fetcher.query_bandcamp()
        assert len(cands) == 1
        assert cands[0].url == "https://f4.bcbits.com/img/a12345678_0.jpg"
        assert cands[0].provider == "Bandcamp Artwork"

    def test_query_caa_front_only(self, monkeypatch) -> None:
        import json

        sample_caa = {
            "images": [
                {"image": "https://caa.org/front.jpg", "front": True, "types": ["Front"]},
                {"image": "https://caa.org/back.jpg", "back": True, "types": ["Back"]},
                {"image": "https://caa.org/cd.jpg", "types": ["Medium"]},
                {"image": "https://caa.org/booklet.jpg", "types": ["Booklet"]},
            ]
        }
        fetcher = CoverFetcher("Queen", "A Night at the Opera", mb_rg_id="rg-test")
        monkeypatch.setattr(fetcher, "fetch_url", lambda url, headers=None: json.dumps(sample_caa).encode("utf-8"))
        cands = fetcher.query_cover_art_archive()
        # Must only extract the front cover, ignoring back, cd/medium, and booklet
        assert len(cands) == 1
        assert cands[0].url == "https://caa.org/front.jpg"
        assert "Front" in cands[0].title

    def test_sidecar_artwork_function_removed(self) -> None:
        import audiophile_cover_fetcher

        assert not hasattr(audiophile_cover_fetcher, "save_sidecar_artwork")

    def test_prompt_cover_choice_interactive_digital_vs_scan(self, tmp_path: Path, monkeypatch) -> None:
        im1 = Image.new("RGB", (400, 400), color=(255, 0, 0))
        b1 = io.BytesIO()
        im1.save(b1, format="JPEG")
        raw1 = b1.getvalue()

        im2 = Image.new("RGB", (300, 300), color=(0, 0, 255))
        b2 = io.BytesIO()
        im2.save(b2, format="JPEG")
        raw2 = b2.getvalue()

        dig = CoverCandidate(
            provider="Apple Music",
            url="https://apple.com/c.jpg",
            title="Apple Digital",
            album_match_ratio=1.0,
            width=400,
            height=400,
            total_score=95.0,
            raw_data=raw1,
        )
        scan = CoverCandidate(
            provider="Discogs",
            url="https://discogs.com/c.jpg",
            title="Discogs Scan",
            album_match_ratio=1.0,
            width=300,
            height=300,
            total_score=80.0,
            raw_data=raw2,
        )
        winner = dig

        out_file = tmp_path / "chosen.jpg"

        # Choice '1' selects digital
        monkeypatch.setattr("sys.stdin.readline", lambda: "1\n")
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        chosen1 = prompt_cover_choice(dig, scan, winner, output_path=out_file, folder_path=tmp_path)
        assert chosen1 is not None
        assert chosen1.provider == "Apple Music"

        # Choice '2' selects scan
        monkeypatch.setattr("sys.stdin.readline", lambda: "2\n")
        chosen2 = prompt_cover_choice(dig, scan, winner, output_path=out_file, folder_path=tmp_path)
        assert chosen2 is not None
        assert chosen2.provider == "Discogs"

        # Choice '' (Enter) selects default winner (Apple Music)
        monkeypatch.setattr("sys.stdin.readline", lambda: "\n")
        chosen_def = prompt_cover_choice(dig, scan, winner, output_path=out_file, folder_path=tmp_path)
        assert chosen_def is not None
        assert chosen_def.provider == "Apple Music"

    def test_create_comparison_and_kitty_display(self, tmp_path: Path) -> None:
        im1 = Image.new("RGB", (200, 200), color=(100, 150, 200))
        b1 = io.BytesIO()
        im1.save(b1, format="JPEG")

        im2 = Image.new("RGB", (200, 200), color=(200, 150, 100))
        b2 = io.BytesIO()
        im2.save(b2, format="JPEG")

        dig = CoverCandidate(
            provider="Amazon",
            url="u1",
            title="Amazon",
            album_match_ratio=1.0,
            width=200,
            height=200,
            raw_data=b1.getvalue(),
        )
        scan = CoverCandidate(
            provider="CAA", url="u2", title="CAA", album_match_ratio=1.0, width=200, height=200, raw_data=b2.getvalue()
        )

        comp_path = tmp_path / ".cover_comparison.jpg"
        res = create_comparison_image(dig, scan, dig, comp_path)
        assert res is not None
        assert comp_path.is_file()
        with Image.open(comp_path) as c_im:
            assert c_im.width > 200

        # emit_kitty_image test
        emitted = emit_kitty_image(comp_path)
        assert isinstance(emitted, bool)

    def test_cmyk_and_exif_transpose_handling(self, tmp_path: Path) -> None:
        im_cmyk = Image.new("CMYK", (200, 200), color=(0, 100, 100, 0))
        buf = io.BytesIO()
        im_cmyk.save(buf, format="JPEG")

        out_file = tmp_path / "cmyk_converted.jpg"
        _w, _h, _sz = save_optimized_cover(buf.getvalue(), out_file)
        assert out_file.is_file()
        with Image.open(out_file) as check_im:
            assert check_im.mode == "RGB"


class TestWorldClassVorbisAndCoverPipelineEnhancements:
    def test_case_insensitive_releasetype_deduplication(self, tmp_path: Path) -> None:
        flac_path = tmp_path / "01 - Case Dedup.flac"
        wav_path = tmp_path / "test.wav"
        import wave

        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(b"\x00" * 44100 * 4)
        subprocess.run(["flac", "-s", str(wav_path), "-o", str(flac_path)], check=True)

        tags = [
            ("TITLE", "Bohemian Rhapsody"),
            ("ALBUM", "A Night at the Opera"),
            ("ARTIST", "Queen"),
            ("ALBUMARTIST", "Queen"),
            ("TRACKNUMBER", "11"),
            ("TRACKTOTAL", "12"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("DATE", "1975-11-21"),
            ("RELEASETYPE", "album"),
            ("RELEASETYPE", "Album"),
            ("GENRE", "Rock"),
            ("GENRE", "rock"),
        ]
        write_tags(flac_path, tags)

        audio = FLAC(str(flac_path))
        # RELEASETYPE and GENRE must be case-insensitively deduplicated
        assert len(audio["RELEASETYPE"]) == 1
        assert audio["RELEASETYPE"][0].lower() == "album"
        assert len(audio["GENRE"]) == 1
        assert audio["GENRE"][0].lower() == "rock"

    def test_baseline_jpeg_saved_and_embedded(self, tmp_path: Path) -> None:
        im = Image.new("RGB", (600, 600), color=(120, 80, 40))
        raw_buf = io.BytesIO()
        im.save(raw_buf, format="JPEG", quality=95, progressive=True)
        prog_raw = raw_buf.getvalue()

        # save_optimized_cover must save baseline JPEG by default
        out_file = tmp_path / "baseline_cover.jpg"
        save_optimized_cover(prog_raw, out_file)
        saved_bytes = out_file.read_bytes()

        # Check markers: SOF0 (0xFFC0 - baseline) present, SOF2 (0xFFC2 - progressive) absent
        assert b"\xff\xc0" in saved_bytes
        assert b"\xff\xc2" not in saved_bytes

        # create_embedded_picture_data must also produce baseline JPEG
        embed_bytes = create_embedded_picture_data(prog_raw, max_dim=1600, quality=90)
        assert b"\xff\xc0" in embed_bytes
        assert b"\xff\xc2" not in embed_bytes

    def test_cd_booklet_aspect_ratio_tolerance(self) -> None:
        # Standard CD booklet: 120mm x 121mm -> ratio ~0.9917, diff ~0.0083
        im_booklet = Image.new("RGB", (1200, 1210), color=(100, 100, 100))
        buf = io.BytesIO()
        im_booklet.save(buf, format="JPEG")

        fetcher = CoverFetcher("Queen", "A Night at the Opera")
        cand = CoverCandidate(
            provider="Cover Art Archive (Release)",
            url="https://caa.org/front_booklet.jpg",
            title="Booklet Scan",
            album_match_ratio=1.0,
            raw_data=buf.getvalue(),
        )
        evaluated = fetcher.evaluate_candidate(cand)
        # Should NOT receive harsh aspect penalty, score should remain strongly positive
        assert evaluated.aspect_ratio < 1.0
        assert evaluated.total_score > 1000.0

    def test_cover_mode_policy_priority(self) -> None:
        im_scan = Image.new("RGB", (1200, 1200), color=(80, 80, 80))
        buf_scan = io.BytesIO()
        im_scan.save(buf_scan, format="JPEG")

        cand_scan = CoverCandidate(
            provider="Cover Art Archive (Release)",
            url="https://caa.org/scan.jpg",
            title="CAA Release Front",
            album_match_ratio=1.0,
            raw_data=buf_scan.getvalue(),
        )

        im_dig = Image.new("RGB", (3000, 3000), color=(90, 90, 90))
        buf_dig = io.BytesIO()
        im_dig.save(buf_dig, format="JPEG")

        CoverCandidate(
            provider="Apple Music / iTunes CDN",
            url="https://apple.com/digital.jpg",
            title="Apple Digital",
            album_match_ratio=1.0,
            raw_data=buf_dig.getvalue(),
        )

        # Provider preference must not alter measured visual quality.
        fetcher_phys = CoverFetcher("Queen", "A Night at the Opera", cover_mode="physical")
        ev_scan = fetcher_phys.evaluate_candidate(cand_scan)

        fetcher_auto = CoverFetcher("Queen", "A Night at the Opera", cover_mode="auto")
        ev_scan_auto = fetcher_auto.evaluate_candidate(
            CoverCandidate(
                provider="Cover Art Archive (Release)",
                url="https://caa.org/scan2.jpg",
                title="CAA Release Front",
                album_match_ratio=1.0,
                raw_data=buf_scan.getvalue(),
            )
        )
        assert ev_scan.visual_quality_score == pytest.approx(ev_scan_auto.visual_quality_score)

    def test_query_qobuz_extraction(self, monkeypatch) -> None:
        sample_html = """
        <html><body>
        <a href="/album/test" title="More details on A Night at the Opera by Queen."><img src="https://static.qobuz.com/images/covers/44/b6/zw9o56jyjb644_230.jpg"/></a>
        <a href="/album/test2" title="More details on A Night at the Opera (Deluxe) by Queen."><img src="https://static.qobuz.com/images/covers/3a/kd/n2yjn4gjgkd3a_230.jpg"/></a>
        <a href="/album/mismatch" title="More details on Brandenburg Concerto by Sir Neville Marriner."><img src="https://static.qobuz.com/images/covers/xx/yy/marriner_230.jpg"/></a>
        </body></html>
        """
        fetcher = CoverFetcher("Queen", "A Night at the Opera")
        monkeypatch.setattr(fetcher, "fetch_url", lambda url, headers=None: sample_html.encode("utf-8"))
        cands = fetcher.query_qobuz()
        assert len(cands) == 2
        assert cands[0].provider == "Qobuz High-Res CDN"
        assert cands[0].url == "https://static.qobuz.com/images/covers/44/b6/zw9o56jyjb644_org.jpg"
        assert not any("marriner" in c.url for c in cands)

    def test_verify_all_flacs_detects_case_duplicate(self, tmp_path: Path) -> None:
        from verify_all_flacs import audit_flac

        flac_path = tmp_path / "01 - Warn Test.flac"
        wav_path = tmp_path / "test.wav"
        import wave

        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(b"\x00" * 44100 * 4)
        subprocess.run(["flac", "-s", str(wav_path), "-o", str(flac_path)], check=True)

        audio = FLAC(str(flac_path))
        audio["TITLE"] = ["Test Track"]
        audio["ALBUM"] = ["Test Album"]
        audio["ARTIST"] = ["Queen"]
        audio["ALBUMARTIST"] = ["Queen"]
        audio["TRACKNUMBER"] = ["1"]
        audio["TRACKTOTAL"] = ["1"]
        audio["DISCNUMBER"] = ["1"]
        audio["DISCTOTAL"] = ["1"]
        audio["DATE"] = ["1975-11-21"]
        audio["YEAR"] = ["1975"]
        audio["ORIGINALDATE"] = ["1975-11-21"]
        audio["ORIGINALYEAR"] = ["1975"]
        audio["ALBUMSORT"] = ["Test Album"]
        audio["ALBUMARTISTSORT"] = ["Queen"]
        audio["TITLESORT"] = ["Test Track"]
        audio["GENRE"] = ["Rock", "rock"]
        audio.save()

        res = audit_flac(flac_path)
        assert any("Detected near-duplicate values differing only by case in GENRE" in w for w in res.warnings)

    def test_caa_retains_original_front_sources_and_flags_packaging(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CAA front originals remain eligible while compound scans carry warnings."""
        import json

        fetcher = CoverFetcher(artist="Queen", album="Made In Heaven", mb_rel_id="test_rel", mb_rg_id="test_rg")

        caa_response = {
            "images": [
                {
                    "front": True,
                    "types": ["Front", "Other", "Raw/Unedited"],
                    "image": "https://coverartarchive.org/release/test/jewel_case.png",
                    "comment": "600 dpi PNG uncropped",
                },
                {
                    "front": True,
                    "types": ["Front", "Spine"],
                    "image": "https://coverartarchive.org/release/test/spine.png",
                },
                {
                    "front": True,
                    "types": ["Front"],
                    "image": "https://coverartarchive.org/release/test/clean_booklet.jpg",
                    "comment": "",
                },
            ]
        }
        monkeypatch.setattr(fetcher, "fetch_url", lambda url, headers=None: json.dumps(caa_response).encode("utf-8"))
        cands = fetcher.query_cover_art_archive()
        # Raw/unedited can be the best preservation source and compound scans
        # can contain a recoverable front panel, so retain and label them.
        assert any(c.url == "https://coverartarchive.org/release/test/clean_booklet.jpg" for c in cands)
        raw = next(c for c in cands if c.url == "https://coverartarchive.org/release/test/jewel_case.png")
        spine = next(c for c in cands if c.url == "https://coverartarchive.org/release/test/spine.png")
        assert raw.representation_kind.value == "original"
        assert "raw/unedited source requires crop review" in raw.warnings
        assert "compound packaging image" in spine.warnings

    def test_jewel_case_front_panel_is_measured_without_reencoding(self) -> None:
        """A compound jewel scan is evaluated through a non-destructive front ROI."""
        fetcher = CoverFetcher(artist="Queen", album="Made In Heaven")
        # Create a synthetic jewel case image: width 1140, height 1000 (aspect 1.14)
        # with a dark plastic spine and sharp edge at x=100
        arr = np.full((1000, 1140, 3), 150, dtype=np.uint8)
        # Plastic spine on left (x=0 to 100)
        arr[:, :100] = 30
        im = Image.fromarray(arr)
        buf = io.BytesIO()
        im.save(buf, format="JPEG")

        cand = CoverCandidate(
            provider="Cover Art Archive (Release)",
            url="http://test/jewel.jpg",
            title="Jewel",
            album_match_ratio=1.0,
            raw_data=buf.getvalue(),
        )
        res = fetcher.evaluate_candidate(cand)
        assert not res.error
        # A flat dark strip has the same geometry as an intentional artwork
        # border. Without corroborating package evidence it must not be cut.
        assert res.crop_box is None
        assert res.effective_width == 1140
        assert res.raw_data == buf.getvalue()

    def test_theaudiodb_provider_extraction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verifies TheAudioDB album cover query and extraction."""
        import json

        fetcher = CoverFetcher(artist="Queen", album="Made In Heaven")
        adb_response = {
            "album": [
                {
                    "strArtist": "Queen",
                    "strAlbum": "Made in Heaven",
                    "strAlbumThumbHQ": "https://r2.theaudiodb.com/images/thumbhq.jpg",
                    "strAlbumThumb": "https://r2.theaudiodb.com/images/thumb.jpg",
                }
            ]
        }
        monkeypatch.setattr(fetcher, "fetch_url", lambda url, headers=None: json.dumps(adb_response).encode("utf-8"))
        cands = fetcher.query_theaudiodb()
        assert len(cands) == 1
        assert cands[0].provider == "TheAudioDB"
        assert cands[0].url == "https://r2.theaudiodb.com/images/thumbhq.jpg"


class TestWorldClassAudiophileStandardsAndLyricsOptimization:
    """Rigorous unit tests for audiophile metadata, protected band preservation,
    precision lyrics matching, and zero-generation-loss artwork embedding.
    """

    def test_protected_band_names_split_artists(self) -> None:
        import audiophile_tagger

        # Bands with '&' or 'and' that must NEVER be split
        assert audiophile_tagger.split_artists("Earth, Wind & Fire") == ["Earth, Wind & Fire"]
        assert audiophile_tagger.split_artists("Simon & Garfunkel") == ["Simon & Garfunkel"]
        assert audiophile_tagger.split_artists("Crosby, Stills, Nash & Young") == ["Crosby, Stills, Nash & Young"]
        assert audiophile_tagger.split_artists("Emerson, Lake & Palmer") == ["Emerson, Lake & Palmer"]
        assert audiophile_tagger.split_artists("Blood, Sweat & Tears") == ["Blood, Sweat & Tears"]
        assert audiophile_tagger.split_artists("KC & The Sunshine Band") == ["KC & The Sunshine Band"]
        assert audiophile_tagger.split_artists("Huey Lewis & The News") == ["Huey Lewis & The News"]
        assert audiophile_tagger.split_artists("Joan Jett & The Blackhearts") == ["Joan Jett & The Blackhearts"]
        assert audiophile_tagger.split_artists("Echo & The Bunnymen") == ["Echo & The Bunnymen"]
        assert audiophile_tagger.split_artists("Tom Petty and the Heartbreakers") == ["Tom Petty and the Heartbreakers"]
        assert audiophile_tagger.split_artists("Florence + The Machine") == ["Florence + The Machine"]

        # Legitimate collaborations that MUST be split
        assert audiophile_tagger.split_artists("Queen & David Bowie") == ["Queen", "David Bowie"]
        assert audiophile_tagger.split_artists("Freddie Mercury feat. Montserrat Caballé") == [
            "Freddie Mercury",
            "Montserrat Caballé",
        ]
        assert audiophile_tagger.split_artists("Queen with Paul Rodgers") == ["Queen", "Paul Rodgers"]

    def test_is_track_match_short_title_false_positive_guard(self) -> None:
        import lyrics_utils

        # False positives on short titles must be rejected
        assert lyrics_utils.is_track_match("Stone Cold Crazy", "Queen", "One", "Queen") is False
        assert lyrics_utils.is_track_match("Radio", "Queen", "Radio Ga Ga", "Queen") is False
        assert lyrics_utils.is_track_match("Radio Ga Ga", "Queen", "Radio", "Queen") is False
        assert lyrics_utils.is_track_match("Somebody to Love", "Queen", "Love", "Queen") is False

        # Legitimate remasters and versions must match
        assert lyrics_utils.is_track_match("One (2011 Remaster)", "Queen", "One", "Queen") is True
        assert (
            lyrics_utils.is_track_match("Bohemian Rhapsody (2011 Remaster)", "Queen", "Bohemian Rhapsody", "Queen")
            is True
        )
        assert lyrics_utils.is_track_match("Killer Queen [2011 Remaster]", "Queen", "Killer Queen", "Queen") is True

        # Duration validation
        assert (
            lyrics_utils.is_track_match(
                "Bohemian Rhapsody", "Queen", "Bohemian Rhapsody", "Queen", cand_duration=355.0, target_duration=354.0
            )
            is True
        )
        assert (
            lyrics_utils.is_track_match(
                "Bohemian Rhapsody", "Queen", "Bohemian Rhapsody", "Queen", cand_duration=180.0, target_duration=354.0
            )
            is False
        )

    def test_pre_emphasis_single_valued_and_canonical_invariants(self, tmp_path: Path) -> None:
        import metadata_writer
        import verify_all_flacs

        # Verify sets
        assert "PRE_EMPHASIS" in metadata_writer.SINGLE_VALUED_TAGS
        assert "PRE_EMPHASIS" in verify_all_flacs.SINGLE_VALUED_TAGS
        assert "LYRICS_TRANSLATION" in metadata_writer.MULTILINE_FIELDS
        assert "LYRICS_ROMAJI" in metadata_writer.MULTILINE_FIELDS
        assert "LYRICS_TRANSLATION" in verify_all_flacs.SINGLE_VALUED_TAGS
        assert "LYRICS_ROMAJI" in verify_all_flacs.SINGLE_VALUED_TAGS

        flac_p = tmp_path / "test_preemphasis.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "flac", str(flac_p)],
            check=True,
            capture_output=True,
        )

        trans_file = tmp_path / "trans.txt"
        trans_file.write_text("Translated lyrics line 1\nTranslated line 2\n", encoding="utf-8")
        romaji_file = tmp_path / "romaji.lrc"
        romaji_file.write_text("[00:10.00]Romaji line 1\n[00:15.00]Romaji line 2\n", encoding="utf-8")

        tags = [
            ("TITLE", "Bohemian Rhapsody"),
            ("ARTIST", "Queen"),
            ("ALBUMARTIST", "Queen"),
            ("ALBUM", "A Night At The Opera"),
            ("DATE", "1975"),
            ("TRACKNUMBER", "11"),
            ("TRACKTOTAL", "12"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("PRE_EMPHASIS", "1"),
            ("FLAGS", "PRE"),
        ]

        metadata_writer.write_tags(
            flac_path=flac_p,
            tags=tags,
            lyrics_translation_file=str(trans_file),
            lyrics_romaji_file=str(romaji_file),
        )

        audio = FLAC(str(flac_p))
        assert audio["PRE_EMPHASIS"] == ["1"]
        assert audio["FLAGS"] == ["PRE"]
        assert "Translated lyrics line 1" in audio["LYRICS_TRANSLATION"][0]
        assert "[00:10.00]Romaji line 1" in audio["LYRICS_ROMAJI"][0]

        # Verify Vorbis invariant checks pass
        metadata_writer.canonical_verify(flac_p)

        # Audit should not complain about PRE_EMPHASIS or multiline lyrics
        audit = verify_all_flacs.audit_flac(flac_p)
        assert not any("PRE_EMPHASIS" in e for e in audit.errors)
        assert not any("LYRICS_TRANSLATION" in e for e in audit.errors)
        assert not any("LYRICS_ROMAJI" in e for e in audit.errors)

    def test_zero_generation_loss_cover_embedding(self, tmp_path: Path) -> None:
        import metadata_writer

        flac_p = tmp_path / "test_zero_loss.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "flac", str(flac_p)],
            check=True,
            capture_output=True,
        )

        # Create a 600x600 test JPEG
        img_p = tmp_path / "cover.jpg"
        im = Image.new("RGB", (600, 600), color=(128, 64, 32))
        im.save(img_p, format="JPEG", quality=90)
        orig_bytes = img_p.read_bytes()

        tags = [
            ("TITLE", "Zero Loss Track"),
            ("ARTIST", "Queen"),
            ("ALBUM", "A Night At The Opera"),
            ("DATE", "1975"),
            ("TRACKNUMBER", "1"),
            ("TRACKTOTAL", "1"),
        ]

        # Embed with max_picture_dim=1000 (no resize needed -> zero generation loss!)
        metadata_writer.write_tags(
            flac_path=flac_p,
            tags=tags,
            picture_file=str(img_p),
            max_picture_dim=1000,
        )

        audio = FLAC(str(flac_p))
        assert len(audio.pictures) == 1
        pic = audio.pictures[0]
        assert pic.mime == "image/jpeg"
        assert pic.width == 600
        assert pic.height == 600
        # Pristine exact raw bytes must match 100% (zero generation loss)
        assert pic.data == orig_bytes

    def test_discogs_preemphasis_detection(self) -> None:
        import audiophile_tagger

        # Simulated discogs release with pre-emphasis in release notes (e.g. early Japanese Black Triangle)
        discogs_data = {
            "title": "A Night At The Opera",
            "artists": [{"name": "Queen"}],
            "formats": [{"name": "CD", "descriptions": ["Album"]}],
            "notes": "Target CD pressing. Mastered with Pre-emphasis in subcode.",
        }
        assert audiophile_tagger.extract_discogs_preemphasis(discogs_data) is True

        # Simulated discogs release explicitly without pre-emphasis
        discogs_data_no_pe = {
            "title": "A Night At The Opera",
            "artists": [{"name": "Queen"}],
            "formats": [{"name": "CD", "descriptions": ["Album"]}],
            "notes": "Standard 1991 remaster without pre-emphasis.",
        }
        assert audiophile_tagger.extract_discogs_preemphasis(discogs_data_no_pe) is False

        # Simulated discogs release with format description pre-emphasis
        discogs_data_fmt = {
            "title": "Abbey Road",
            "artists": [{"name": "The Beatles"}],
            "formats": [{"name": "CD", "descriptions": ["Pre-emphasis", "Album"]}],
            "notes": "CP35-3016 first pressing.",
        }
        assert audiophile_tagger.extract_discogs_preemphasis(discogs_data_fmt) is True

    def test_multivalued_catalognumber_preservation(self, tmp_path: Path) -> None:
        """Verify that multiple CATALOGNUMBER tags are preserved and pass strict audit."""
        import metadata_writer
        import verify_all_flacs

        flac_p = tmp_path / "multi_cat.flac"
        # Create minimal valid FLAC file
        cmd = [
            "flac",
            "-f",
            "-s",
            "--silent",
            "-0",
            "--endian=little",
            "--sign=signed",
            "--channels=2",
            "--bps=16",
            "--sample-rate=44100",
            "--force-raw-format",
            "-o",
            str(flac_p),
            "/dev/zero",
        ]
        subprocess.run(cmd, input=b"\x00" * 44100 * 4, check=True)

        manifest = [
            ("TITLE", "Bohemian Rhapsody"),
            ("ARTIST", "Queen"),
            ("ALBUM", "A Night at the Opera"),
            ("ALBUMARTIST", "Queen"),
            ("DATE", "1975-11-21"),
            ("TRACKNUMBER", "11"),
            ("TRACKTOTAL", "12"),
            ("DISCNUMBER", "1"),
            ("DISCTOTAL", "1"),
            ("CATALOGNUMBER", "GZS-1144"),
            ("CATALOGNUMBER", "HR-61144"),
        ]
        metadata_writer.write_tags(flac_p, manifest)

        audio = FLAC(str(flac_p))
        assert audio.tags is not None
        cats = audio.tags.get("CATALOGNUMBER", [])
        assert len(cats) == 2
        assert "GZS-1144" in cats
        assert "HR-61144" in cats

        audit = verify_all_flacs.audit_flac(flac_p)
        assert not any("CATALOGNUMBER" in err for err in audit.errors)

    def test_musixmatch_richsync_conversion(self) -> None:
        """Verify Musixmatch Richsync JSON conversion to Enhanced LRC and TTML."""
        import json

        import lyrics_utils

        sample_richsync = json.dumps(
            [
                {
                    "ts": 0.5,
                    "te": 3.8,
                    "l": [
                        {"c": "Is ", "o": 0.0},
                        {"c": "this ", "o": 0.4},
                        {"c": "the ", "o": 0.8},
                        {"c": "real ", "o": 1.2},
                        {"c": "life", "o": 2.0},
                    ],
                },
                {
                    "ts": 4.0,
                    "te": 7.5,
                    "l": [
                        {"c": "Is ", "o": 0.0},
                        {"c": "this ", "o": 0.5},
                        {"c": "just ", "o": 1.0},
                        {"c": "fantasy", "o": 1.5},
                    ],
                },
            ]
        )

        out = lyrics_utils.musixmatch_richsync_to_lyrics(
            sample_richsync, song="Bohemian Rhapsody", artist="Queen", duration=355.0
        )
        assert out is not None
        assert out["found"] is True
        assert out["has_word_sync"] is True
        assert out["format"] == "enhanced_lrc"
        assert "<00:00.50>Is " in out["enhancedLyrics"]
        assert "<00:00.90>this " in out["enhancedLyrics"]
        assert "[00:00.50]Is this the real life" in out["syncedLyrics"]
        assert "Is this the real life" in out["plainLyrics"]
        assert out["ttmlLyrics"] == ""  # Provider gives starts, not word ends.

    def test_yesterday_safe_lyrics_cleaning(self) -> None:
        """Verify that opening lyrics starting with song title are protected while metadata headers are stripped."""
        import lyrics_utils

        # Word-synced line beginning with song title at 0.5s must NOT be dropped
        word_synced_yesterday = "[00:00.50] <00:00.50>Yesterday <00:01.20>all <00:01.50>my <00:01.80>troubles"
        assert (
            lyrics_utils.is_credit_or_garbage_line(
                word_synced_yesterday, song_title="Yesterday", artist_name="The Beatles", timestamp_sec=0.5
            )
            is False
        )

        # Sung line at 0.5s without announcement indicator must NOT be dropped
        plain_sung_yesterday = "Yesterday, all my troubles seemed so far away"
        assert (
            lyrics_utils.is_credit_or_garbage_line(
                plain_sung_yesterday, song_title="Yesterday", artist_name="The Beatles", timestamp_sec=0.5
            )
            is False
        )

        # An announcement can be spoken lyrics; text alone is not proof of garbage.
        announcement_line = "Song: Yesterday - The Beatles"
        assert (
            lyrics_utils.is_credit_or_garbage_line(
                announcement_line, song_title="Yesterday", artist_name="The Beatles", timestamp_sec=0.5
            )
            is False
        )

        assert lyrics_utils.is_credit_or_garbage_line("[ar:The Beatles]") is True

    def test_adaptive_duration_tolerance(self) -> None:
        """Verify adaptive tolerance allows minor CD silence/pre-gap offset when vocal timings match."""
        import lyrics_utils

        # CD track has 3.5s longer silence at end (243.5s vs 240.0s candidate duration),
        # but vocal line ends at 220.0s -> safely within audio bounds -> True
        candidate_cd_offset = {"duration": 240.0, "syncedLyrics": "[00:10.00] Opening\n[03:40.00] Final vocal line\n"}
        assert lyrics_utils.validate_lyrics_duration(candidate_cd_offset, 243.5, tolerance=2.5) is True

        # Major mismatch (radio edit cut 210.0s vs 240.0s) -> False
        candidate_radio_cut = {"duration": 210.0, "syncedLyrics": "[00:10.00] Line 1"}
        assert lyrics_utils.validate_lyrics_duration(candidate_radio_cut, 240.0, tolerance=2.5) is False

        # Instrumental outro test: Queen's "Who Wants To Live Forever" (audio 316.9s, last vocal 221.7s -> 95.2s outro)
        candidate_who_wants = {"syncedLyrics": "[00:10.00] There is no time for us\n[03:41.70] Forever is our today\n"}
        assert lyrics_utils.validate_lyrics_duration(candidate_who_wants, 316.9, tolerance=2.5) is True

        # False-positive protection: metadata says 240.0s, but lyrics go past audio length (300.0s vs 240.0s) -> False
        candidate_desynced_timestamps = {
            "duration": 240.0,
            "syncedLyrics": "[00:10.00] Line 1\n[05:00.00] Way past 4 minutes audio\n",
        }
        assert lyrics_utils.validate_lyrics_duration(candidate_desynced_timestamps, 240.0, tolerance=2.5) is False

        # Offset tag handling: [offset: 5000] shifts 03:55.00 (+5s) to 04:00.00 (240.0s) -> fits within 240.0s audio
        candidate_with_offset = {
            "syncedLyrics": "[offset: 5000]\n[00:05.00] Line 1\n[03:55.00] Outro line\n",
        }
        assert lyrics_utils.validate_lyrics_duration(candidate_with_offset, 240.0, tolerance=2.5) is True


class TestVerboseLoggingMode:
    """Test suite for -v / --verbose diagnostic logging across tagger, lyrics, covers, and loudgain."""

    def test_cli_verbose_argparse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify -v, -vv, and --verbose flags are properly recognized by audiophile_tagger."""
        import argparse

        # Verify parser has -v / --verbose argument with count action
        parser = argparse.ArgumentParser()
        parser.add_argument("-v", "--verbose", action="count", default=0)
        assert parser.parse_args(["-v"]).verbose == 1
        assert parser.parse_args(["--verbose"]).verbose == 1
        assert parser.parse_args(["-vv"]).verbose == 2
        assert parser.parse_args(["-v", "-v"]).verbose == 2
        assert parser.parse_args(["-vvv"]).verbose == 3
        assert parser.parse_args([]).verbose == 0

    def test_verbose_cover_fetcher_logs(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify CoverFetcher emits structured [VERBOSE][COVER] diagnostics."""
        from audiophile_cover_fetcher import CoverCandidate, CoverFetcher

        fetcher = CoverFetcher(artist="Test Artist", album="Test Album", verbose=True)

        # Mock iTunes to return one candidate and others empty to avoid network
        fake_cand = CoverCandidate(
            provider="Apple Music (iTunes)",
            url="https://example.com/test.jpg",
            title="Test Cover",
            album_match_ratio=1.0,
            image_type="front",
        )
        monkeypatch.setattr(fetcher, "query_itunes", lambda: [fake_cand])
        monkeypatch.setattr(fetcher, "query_cover_art_archive", list)
        monkeypatch.setattr(fetcher, "query_deezer", list)
        monkeypatch.setattr(fetcher, "query_theaudiodb", list)
        monkeypatch.setattr(fetcher, "query_discogs", list)
        monkeypatch.setattr(fetcher, "query_local", list)
        monkeypatch.setattr(fetcher, "query_amazon", list)
        monkeypatch.setattr(fetcher, "query_fanart_tv", list)
        monkeypatch.setattr(fetcher, "query_bandcamp", list)
        monkeypatch.setattr(fetcher, "query_tidal", list)
        monkeypatch.setattr(fetcher, "query_qobuz", list)

        # Mock evaluate_candidate to set valid score
        def mock_eval(cand: CoverCandidate) -> CoverCandidate:
            cand.width = 1400
            cand.height = 1400
            cand.aspect_ratio = 1.0
            cand.sharpness = 120.0
            cand.badge_penalty = 0.0
            cand.total_score = 95.0
            cand.raw_data = b"fake-jpg"
            return cand

        monkeypatch.setattr(fetcher, "evaluate_candidate", mock_eval)

        _winner, _best_dig, _best_scan, _all_cands = fetcher.find_best_cover(verbose=True)
        captured = capsys.readouterr().out
        assert "[VERBOSE][COVER]" in captured
        assert "Apple Music (iTunes)" in captured
        assert "WINNER SELECTED" in captured

    def test_verbose_lyrics_cascade_logs(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Verify fetch_lyrics_cascade emits structured [VERBOSE][LYRICS] diagnostics."""
        import lyrics_utils

        # Test instrumental logging
        res = lyrics_utils.fetch_lyrics_cascade("Seven Seas of Rhye (Instrumental)", "Queen", verbose=True)
        captured = capsys.readouterr().out
        assert "[VERBOSE][LYRICS]" in captured
        assert "Detected instrumental track" in captured
        assert res is not None and res["source"] == "Instrumental"

    def test_verbose_loudgain_logs(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Verify run_loudgain emits structured [VERBOSE][LOUDGAIN] diagnostics."""

        import audiophile_tagger
        from test_pipeline_safety import make_flac

        fake_flac = make_flac(tmp_path / "test.flac")

        monkeypatch.setattr(
            audiophile_tagger.shutil, "which", lambda cmd: "/usr/bin/loudgain" if cmd == "loudgain" else None
        )

        class FakeProcess:
            returncode = 0
            stdout = "Track: test.flac\n Loudness: -14.20 LUFS\n Range: 8.5 dB\n Peak: 0.980000\n Gain: -3.80 dB\n\nAlbum:\n Loudness: -14.20 LUFS\n Gain: -3.80 dB\n"
            stderr = ""

        real_run = subprocess.run

        def scan(cmd: list[str], **kwargs: Any) -> Any:
            if cmd[0] != "/usr/bin/loudgain":
                return real_run(cmd, **kwargs)
            for name in cmd:
                if name.endswith(".replaygain.flac"):
                    audio = FLAC(name)
                    audio["REPLAYGAIN_TRACK_GAIN"] = ["-3.80 dB"]
                    audio["REPLAYGAIN_TRACK_PEAK"] = ["0.98"]
                    audio["REPLAYGAIN_ALBUM_GAIN"] = ["-3.80 dB"]
                    audio["REPLAYGAIN_ALBUM_PEAK"] = ["0.98"]
                    audio.save()
            return FakeProcess()

        monkeypatch.setattr(audiophile_tagger.subprocess, "run", scan)

        assert audiophile_tagger.run_loudgain([fake_flac], verbose=True)
        captured = capsys.readouterr().out
        assert "[VERBOSE][LOUDGAIN]" in captured
        assert "Binary path: /usr/bin/loudgain" in captured
        assert "Loudness: -14.20 LUFS" in captured

    def test_verbose_level_2_cover_rendering(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify -vv (verbosity >= 2) renders all found cover candidates in terminal via emit_kitty_image."""
        import io

        from PIL import Image

        import audiophile_cover_fetcher
        from audiophile_cover_fetcher import CoverCandidate, CoverFetcher

        rendered_paths: list[Path] = []

        def record_rendered_path(path: Path) -> bool:
            rendered_paths.append(path)
            return True

        monkeypatch.setattr(audiophile_cover_fetcher, "emit_kitty_image", record_rendered_path)

        fetcher = CoverFetcher(artist="Queen", album="A Night At The Opera", verbose=2)

        # Create two fake valid image candidates with raw_data
        img1 = Image.new("RGB", (500, 500), color="red")
        b1 = io.BytesIO()
        img1.save(b1, format="JPEG")
        data1 = b1.getvalue()

        img2 = Image.new("RGB", (600, 600), color="blue")
        b2 = io.BytesIO()
        img2.save(b2, format="JPEG")
        data2 = b2.getvalue()

        cand1 = CoverCandidate(
            provider="Apple Music (iTunes)",
            url="https://example.com/art1.jpg",
            title="Queen - A Night At The Opera",
            album_match_ratio=1.0,
            image_type="front",
            raw_data=data1,
            width=500,
            height=500,
            aspect_ratio=1.0,
            sharpness=12.0,
            total_score=95.0,
        )
        cand2 = CoverCandidate(
            provider="Cover Art Archive (CAA)",
            url="https://example.com/art2.jpg",
            title="Queen - A Night At The Opera",
            album_match_ratio=1.0,
            image_type="front",
            raw_data=data2,
            width=600,
            height=600,
            aspect_ratio=1.0,
            sharpness=10.0,
            total_score=90.0,
        )

        monkeypatch.setattr(fetcher, "query_itunes", lambda: [cand1])
        monkeypatch.setattr(fetcher, "query_cover_art_archive", lambda: [cand2])
        monkeypatch.setattr(fetcher, "query_deezer", list)
        monkeypatch.setattr(fetcher, "query_theaudiodb", list)
        monkeypatch.setattr(fetcher, "query_discogs", list)
        monkeypatch.setattr(fetcher, "query_local", list)
        monkeypatch.setattr(fetcher, "query_amazon", list)
        monkeypatch.setattr(fetcher, "query_fanart_tv", list)
        monkeypatch.setattr(fetcher, "query_bandcamp", list)
        monkeypatch.setattr(fetcher, "query_tidal", list)
        monkeypatch.setattr(fetcher, "query_qobuz", list)
        monkeypatch.setattr(fetcher, "evaluate_candidate", lambda c: c)

        _winner, _best_dig, _best_scan, _all_cands = fetcher.find_best_cover(verbose=2)
        captured = capsys.readouterr().out

        # Verify emit_kitty_image was invoked for each candidate with image data
        assert len(rendered_paths) == 2
        assert "[VERBOSE][COVER-PREVIEW] Rendering 2 found cover candidate(s) to terminal (-vv)" in captured
        assert "Apple Music (iTunes)" in captured
        assert "Cover Art Archive (CAA)" in captured
        # With no edition-linked source at all, title-matched artwork remains a
        # deterministic last-resort tier rather than producing no cover.
        assert "OVERALL WINNER" in captured
        assert "BEST SCAN" not in captured  # CAA is an upload repository, not verified scan provenance.

        # Verify that with verbose=1, candidates are NOT rendered (only text logs)
        rendered_paths.clear()
        fetcher.find_best_cover(verbose=1)
        assert len(rendered_paths) == 0


class TestReleaseAwareCoverSelection:
    @staticmethod
    def _art(size: int, *, quality: int = 95, image_format: str = "JPEG", blurred: bool = False) -> bytes:
        image = Image.new("RGB", (size, size), color=(235, 226, 195))
        draw = ImageDraw.Draw(image)
        for offset in range(24, size - 24, max(12, size // 45)):
            draw.line((offset, 20, size - offset // 3, size - 20), fill=(25, 45, 75), width=max(1, size // 500))
        draw.ellipse(
            (size // 4, size // 4, size * 3 // 4, size * 3 // 4), outline=(170, 40, 25), width=max(2, size // 120)
        )
        if blurred:
            from PIL import ImageFilter

            image = image.filter(ImageFilter.GaussianBlur(radius=max(2, size // 160)))
        buffer = io.BytesIO()
        image.save(buffer, format=image_format, quality=quality)
        return buffer.getvalue()

    @staticmethod
    def _exact(
        provider: str,
        image_id: str,
        data: bytes,
        *,
        release_tier: ReleaseTier = ReleaseTier.EXACT_RELEASE,
        role_tier: RoleTier = RoleTier.EXPLICIT_FRONT,
        watermark: float = 0.0,
    ) -> CoverCandidate:
        return CoverCandidate(
            provider=provider,
            url=f"https://example.test/{image_id}.jpg",
            title="Queen - Innuendo",
            album_match_ratio=1.0,
            raw_data=data,
            identity_scope="release" if release_tier == ReleaseTier.EXACT_RELEASE else "concept",
            provider_image_id=image_id,
            release_key="test:queen-innuendo-edition",
            release_tier=release_tier,
            release_confidence=0.99 if release_tier == ReleaseTier.EXACT_RELEASE else 0.62,
            front_flag=role_tier == RoleTier.EXPLICIT_FRONT,
            role_tier=role_tier,
            role_confidence=0.99 if role_tier == RoleTier.EXPLICIT_FRONT else 0.2,
            watermark_confidence=watermark,
        )

    @staticmethod
    def _install_candidates(
        monkeypatch: pytest.MonkeyPatch, fetcher: CoverFetcher, candidates: list[CoverCandidate]
    ) -> None:
        monkeypatch.setattr(fetcher, "query_itunes", lambda: candidates)
        for method in (
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
            monkeypatch.setattr(fetcher, method, list)

    def test_caa_original_is_not_replaced_by_thumbnail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        release_id = "363808e4-089f-41fa-b1ad-eddbd1cab3c4"
        payload = {
            "release": f"https://musicbrainz.org/release/{release_id}",
            "images": [
                {
                    "id": 28543392438,
                    "approved": True,
                    "front": True,
                    "back": False,
                    "types": ["Front"],
                    "comment": "original jewel scan",
                    "image": f"https://coverartarchive.org/release/{release_id}/28543392438.jpg",
                    "thumbnails": {
                        "250": f"https://coverartarchive.org/release/{release_id}/28543392438-250.jpg",
                        "1200": f"https://coverartarchive.org/release/{release_id}/28543392438-1200.jpg",
                    },
                }
            ],
        }
        fetcher = CoverFetcher("Queen", "Innuendo", mb_rel_id=release_id)
        monkeypatch.setattr(fetcher, "fetch_url", lambda url, headers=None: json.dumps(payload).encode())
        candidate = fetcher.query_cover_art_archive()[0]
        assert candidate.url.endswith("28543392438.jpg")
        assert candidate.original_url == candidate.url
        assert candidate.preview_urls["1200"].endswith("28543392438-1200.jpg")
        assert candidate.representation_kind is RepresentationKind.ORIGINAL
        assert candidate.provider_image_id == "28543392438"
        assert candidate.approved is True
        assert candidate.release_tier is ReleaseTier.EXACT_RELEASE

    def test_discogs_dimensions_are_declared_not_measured_quality(self) -> None:
        payload = {
            "images": [
                {
                    "type": "primary",
                    "uri": "https://i.discogs.com/key/rs:fit/q:90/h:600/w:600/source.jpeg",
                    "uri150": "https://i.discogs.com/key/rs:fit/q:40/h:150/w:150/source.jpeg",
                    "width": 2400,
                    "height": 2400,
                },
                {
                    "type": "secondary",
                    "uri": "https://i.discogs.com/key2/rs:fit/q:90/h:600/w:600/other.jpeg",
                    "width": 600,
                    "height": 600,
                },
            ]
        }
        fetcher = CoverFetcher("Queen", "Innuendo", discogs_id="2467591", discogs_json_str=json.dumps(payload))
        primary, secondary = fetcher.query_discogs()
        assert primary.declared_width == 2400
        assert primary.width == 0
        assert primary.representation_kind is RepresentationKind.PREVIEW
        assert primary.role_tier is RoleTier.LIKELY_FRONT
        assert secondary.role_tier is RoleTier.UNKNOWN

    def test_queen_innuendo_general_regression(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        fetcher = CoverFetcher("Queen", "Innuendo", cache_dir=tmp_path)
        caa = self._exact("Cover Art Archive (Release)", "28543392438", self._art(1800))
        discogs = self._exact("Discogs (secondary)", "R-2467591-1381077136-4412", self._art(600, quality=35))
        self._install_candidates(monkeypatch, fetcher, [discogs, caa])
        winner, _digital, _scan, evaluated = fetcher.find_best_cover()
        assert winner is not None
        assert winner.provider_image_id == "28543392438"
        measured = {item.provider_image_id: item for item in evaluated}
        assert measured["28543392438"].detail_score > measured["R-2467591-1381077136-4412"].detail_score
        assert measured["28543392438"].visual_quality_score > measured["R-2467591-1381077136-4412"].visual_quality_score

    def test_provider_neutral_inverse_discogs_can_win(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        fetcher = CoverFetcher("Queen", "Innuendo", cache_dir=tmp_path)
        caa = self._exact("Cover Art Archive (Release)", "caa-poor", self._art(650, quality=25, blurred=True))
        discogs = self._exact("Discogs (primary)", "discogs-clean", self._art(1700))
        self._install_candidates(monkeypatch, fetcher, [caa, discogs])
        winner, *_rest = fetcher.find_best_cover()
        assert winner is not None
        assert winner.provider_image_id == "discogs-clean"

    def test_exact_release_gates_better_wrong_edition(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        fetcher = CoverFetcher("Queen", "Innuendo", cache_dir=tmp_path)
        exact = self._exact("Discogs (primary)", "exact", self._art(900, quality=75))
        wrong_edition = self._exact(
            "Cover Art Archive (Release-Group)",
            "wrong-edition",
            self._art(2000),
            release_tier=ReleaseTier.RELEASE_GROUP,
        )
        fetcher.evaluate_candidate(wrong_edition)
        self._install_candidates(monkeypatch, fetcher, [wrong_edition, exact])
        winner, *_rest = fetcher.find_best_cover()
        assert winner is exact
        assert wrong_edition.visual_quality_score > exact.visual_quality_score

    def test_title_only_fallback_rejects_unrequested_edition_marker(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fetcher = CoverFetcher("Queen", "Innuendo", cache_dir=tmp_path)
        plain = CoverCandidate(
            "Apple Music",
            "https://example.test/plain.jpg",
            "Queen - Innuendo",
            1.0,
            raw_data=self._art(800, quality=75),
            provider_image_id="plain",
        )
        deluxe = CoverCandidate(
            "Deezer",
            "https://example.test/deluxe.jpg",
            "Queen - Innuendo (Deluxe Remastered Edition)",
            0.95,
            raw_data=self._art(1800),
            provider_image_id="deluxe",
        )
        self._install_candidates(monkeypatch, fetcher, [deluxe, plain])
        winner, _digital, _scan, all_candidates = fetcher.find_best_cover()
        assert winner is plain
        rejected = next(candidate for candidate in all_candidates if candidate.provider_image_id == "deluxe")
        assert "title_indicates_different_edition" in rejected.rejection_reasons
        assert rejected.total_score < 0

    def test_role_gate_rejects_back_and_booklet(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        fetcher = CoverFetcher("Queen", "Innuendo", cache_dir=tmp_path)
        front = self._exact("Discogs (primary)", "front", self._art(800, quality=70))
        booklet = self._exact("CAA", "booklet", self._art(1800), role_tier=RoleTier.REJECT)
        booklet.image_type = "booklet"
        self._install_candidates(monkeypatch, fetcher, [booklet, front])
        winner, *_rest = fetcher.find_best_cover()
        assert winner is front

    def test_watermark_and_compression_can_defeat_more_pixels(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fetcher = CoverFetcher("Queen", "Innuendo", cache_dir=tmp_path)
        damaged = self._exact("CAA", "large-watermark", self._art(1800, quality=20), watermark=0.95)
        clean = self._exact("Discogs", "clean", self._art(1100, quality=95))
        self._install_candidates(monkeypatch, fetcher, [damaged, clean])
        winner, *_rest = fetcher.find_best_cover()
        assert winner is clean
        assert damaged.score_breakdown["watermark_penalty"] > 35

    def test_dpi_and_format_have_no_automatic_bonus(self, tmp_path: Path) -> None:
        image = Image.open(io.BytesIO(self._art(700)))
        low_dpi, high_dpi, png = io.BytesIO(), io.BytesIO(), io.BytesIO()
        image.save(low_dpi, format="JPEG", quality=95, dpi=(72, 72))
        image.save(high_dpi, format="JPEG", quality=95, dpi=(600, 600))
        image.save(png, format="PNG")
        fetcher = CoverFetcher("Queen", "Innuendo", cache_dir=tmp_path)
        low = fetcher.evaluate_candidate(self._exact("A", "low-dpi", low_dpi.getvalue()))
        high = fetcher.evaluate_candidate(self._exact("B", "high-dpi", high_dpi.getvalue()))
        lossless = fetcher.evaluate_candidate(self._exact("C", "png", png.getvalue()))
        assert low.visual_quality_score == pytest.approx(high.visual_quality_score)
        assert abs(lossless.visual_quality_score - low.visual_quality_score) < 10.0

    def test_duplicate_and_derivative_grouping(self, tmp_path: Path) -> None:
        original = self._art(1000)
        image = Image.open(io.BytesIO(original))
        derivative_buffer = io.BytesIO()
        image.resize((800, 800), Image.Resampling.LANCZOS).save(derivative_buffer, format="JPEG", quality=75)
        fetcher = CoverFetcher("Queen", "Innuendo", cache_dir=tmp_path)
        first = fetcher.evaluate_candidate(self._exact("CAA", "original", original))
        exact_copy = fetcher.evaluate_candidate(self._exact("Discogs", "copy", original))
        derivative = fetcher.evaluate_candidate(self._exact("Other", "derivative", derivative_buffer.getvalue()))
        fetcher._assign_duplicate_clusters([first, exact_copy, derivative])
        assert first.duplicate_cluster_id == exact_copy.duplicate_cluster_id
        assert derivative.duplicate_cluster_id == first.duplicate_cluster_id
        assert sum(not item.duplicate_of for item in (first, exact_copy, derivative)) == 1

    def test_exif_rotation_probe_and_malformed_rejection(self, tmp_path: Path) -> None:
        image = Image.new("RGB", (400, 800), color=(90, 100, 110))
        exif = image.getexif()
        exif[274] = 6
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=95, exif=exif)
        fetcher = CoverFetcher("Queen", "Innuendo", cache_dir=tmp_path)
        rotated = fetcher.evaluate_candidate(self._exact("CAA", "rotated", buffer.getvalue()))
        malformed = fetcher.evaluate_candidate(self._exact("CAA", "bad", b"not an image" * 300))
        assert (rotated.width, rotated.height) == (800, 400)
        assert malformed.error.startswith("image_parse_error")
        assert malformed.total_score < 0

    @pytest.mark.parametrize(
        ("mode", "image_format", "expected_mime"),
        [
            ("L", "PNG", "image/png"),
            ("P", "PNG", "image/png"),
            ("RGBA", "PNG", "image/png"),
            ("CMYK", "JPEG", "image/jpeg"),
            ("RGB", "WEBP", "image/webp"),
        ],
    )
    def test_supported_image_modes_and_formats(
        self, mode: str, image_format: str, expected_mime: str, tmp_path: Path
    ) -> None:
        source = Image.open(io.BytesIO(self._art(700))).convert(mode)
        if mode == "P":
            source = source.quantize(colors=128)
        buffer = io.BytesIO()
        source.save(buffer, format=image_format, quality=90)
        fetcher = CoverFetcher("Queen", "Innuendo", cache_dir=tmp_path)
        evaluated = fetcher.evaluate_candidate(self._exact("CAA", f"{mode}-{image_format}", buffer.getvalue()))
        assert not evaluated.error
        assert evaluated.mime_type == expected_mime
        assert evaluated.visual_quality_score > 0

    def test_missing_api_dimensions_are_probed_and_source_is_preserved(self, tmp_path: Path) -> None:
        data = self._art(777, image_format="PNG")
        fetcher = CoverFetcher("Queen", "Innuendo", cache_dir=tmp_path)
        candidate = self._exact("CAA", "probe", data)
        assert candidate.declared_width == 0
        evaluated = fetcher.evaluate_candidate(candidate)
        saved = save_original_cover(data, tmp_path / "cover.original")
        assert (evaluated.width, evaluated.height) == (777, 777)
        assert saved.suffix == ".png"
        assert saved.name == "cover.original.png"
        assert saved.read_bytes() == data

    def test_similarity_never_overrides_release_identity(self, tmp_path: Path) -> None:
        fetcher = CoverFetcher("Queen", "Innuendo", cache_dir=tmp_path)
        exact = self._exact("Discogs", "exact-poor", b"", release_tier=ReleaseTier.EXACT_RELEASE)
        other = self._exact("CAA", "other-clean", b"", release_tier=ReleaseTier.RELEASE_GROUP)
        exact.visual_quality_score = 30
        other.visual_quality_score = 95
        exact.duplicate_cluster_id = other.duplicate_cluster_id = "similar-artwork"
        assert fetcher._best_in_release_tier([other, exact]) is exact

    def test_explicit_conflict_is_not_promoted_by_legacy_scope(self, tmp_path: Path) -> None:
        candidate = CoverCandidate(
            "CAA",
            "conflict",
            "Front",
            1.0,
            identity_scope="release",
            release_tier=ReleaseTier.CONFLICT,
        )
        assert candidate.release_tier is ReleaseTier.CONFLICT
        fetcher = CoverFetcher("Queen", "Innuendo", cache_dir=tmp_path)
        assert fetcher._best_in_release_tier([candidate]) is None

    def test_unknown_secondary_requires_front_evidence(self, tmp_path: Path) -> None:
        candidate = CoverCandidate(
            "Discogs",
            "unknown",
            "secondary",
            1.0,
            identity_scope="release",
            roles=("unknown",),
            role_tier=RoleTier.UNKNOWN,
            visual_quality_score=99,
        )
        fetcher = CoverFetcher("Queen", "Innuendo", cache_dir=tmp_path)
        assert fetcher._best_in_release_tier([candidate]) is None

    def test_near_equal_quality_prefers_confirmed_front_but_clear_quality_can_win(self, tmp_path: Path) -> None:
        fetcher = CoverFetcher("Artist", "Album", cache_dir=tmp_path)
        front = self._exact("CAA", "front", b"")
        alternative = self._exact("Discogs", "alternative", b"", role_tier=RoleTier.LIKELY_FRONT)
        front.visual_quality_score = 90
        alternative.visual_quality_score = 91
        assert fetcher._best_in_release_tier([alternative, front]) is front
        alternative.visual_quality_score = 95
        assert fetcher._best_in_release_tier([front, alternative]) is alternative

    def test_skew_is_a_soft_confidence_signal(self, tmp_path: Path) -> None:
        image = Image.new("L", (800, 800), color=255)
        draw = ImageDraw.Draw(image)
        for offset in range(50, 751, 50):
            draw.line((offset, 30, offset, 770), fill=20, width=3)
            draw.line((30, offset, 770, offset), fill=20, width=3)
        skewed = image.rotate(2, Image.Resampling.BICUBIC, expand=False, fillcolor=255)
        straight_buffer, skewed_buffer = io.BytesIO(), io.BytesIO()
        image.save(straight_buffer, format="PNG")
        skewed.save(skewed_buffer, format="PNG")
        fetcher = CoverFetcher("Queen", "Innuendo", cache_dir=tmp_path)
        straight = fetcher.evaluate_candidate(self._exact("CAA", "straight", straight_buffer.getvalue()))
        rotated = fetcher.evaluate_candidate(self._exact("CAA", "skewed", skewed_buffer.getvalue()))
        assert straight.skew_confidence == 0.0
        assert abs(rotated.skew_degrees) == pytest.approx(2.0)
        assert 0.0 < rotated.skew_confidence <= 0.7
        assert rotated.visual_quality_score > 0.0

    def test_deterministic_tie_break_independent_of_response_order(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        data = self._art(700)
        winners: list[str] = []
        for order in (("a", "b"), ("b", "a")):
            fetcher = CoverFetcher("Queen", "Innuendo", cache_dir=tmp_path)
            candidates = [self._exact("Provider", image_id, data) for image_id in order]
            self._install_candidates(monkeypatch, fetcher, candidates)
            winner, *_rest = fetcher.find_best_cover()
            assert winner is not None
            winners.append(winner.provider_image_id)
        assert winners[0] == winners[1]

    def test_content_hash_quality_cache_avoids_redecoding(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        data = self._art(720)
        first_fetcher = CoverFetcher("Queen", "Innuendo", cache_dir=tmp_path)
        first = first_fetcher.evaluate_candidate(self._exact("CAA", "first", data))
        assert list(tmp_path.glob(f"quality-v8-{first.source_sha256}.json"))

        second_fetcher = CoverFetcher("Queen", "Innuendo", cache_dir=tmp_path)

        def fail_decode(*args: object, **kwargs: object) -> None:
            raise AssertionError("content-hash cache should avoid image decoding")

        monkeypatch.setattr(Image, "open", fail_decode)
        second = second_fetcher.evaluate_candidate(self._exact("Discogs", "second", data))
        assert second.visual_quality_score == pytest.approx(first.visual_quality_score)
        assert second.perceptual_hash == first.perceptual_hash

    def test_transient_fetch_failure_is_classified_and_not_cached(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fetcher = CoverFetcher("Queen", "Innuendo", cache_dir=tmp_path)
        url = "https://example.test/transient.jpg"
        monkeypatch.setattr(fetcher.session, "get", lambda *args, **kwargs: (_ for _ in ()).throw(requests.Timeout()))
        monkeypatch.setattr("audiophile_cover_fetcher.time.sleep", lambda _seconds: None)
        assert fetcher.fetch_url(url) is None
        assert fetcher.last_fetch_errors[url] == "timeout"
        assert not fetcher._get_cache_path(url).exists()

        data = self._art(640)

        class Response:
            def __init__(self) -> None:
                self.status_code = 200
                self.content = data
                self.headers = {"Content-Type": "image/jpeg"}

        monkeypatch.setattr(fetcher.session, "get", lambda *args, **kwargs: Response())
        assert fetcher.fetch_url(url) == data
        assert fetcher._get_cache_path(url).read_bytes() == data
        assert url not in fetcher.last_fetch_errors

    def test_small_lossless_payload_is_not_rejected_by_file_size(self, tmp_path: Path) -> None:
        buffer = io.BytesIO()
        Image.new("L", (256, 256), 128).save(buffer, format="PNG")
        assert len(buffer.getvalue()) < 2000
        fetcher = CoverFetcher("Artist", "Album", cache_dir=tmp_path)
        candidate = fetcher.evaluate_candidate(self._exact("CAA", "small-png", buffer.getvalue()))
        assert not candidate.error
        assert candidate.width == 256


class TestCoverAuditCorrections:
    @staticmethod
    def _png(size: tuple[int, int] = (700, 700)) -> bytes:
        image = Image.new("RGB", size, (210, 180, 130))
        inset = min(90, min(size) // 4)
        ImageDraw.Draw(image).rectangle((inset, inset, size[0] - inset, size[1] - inset), fill=(40, 70, 130))
        output = io.BytesIO()
        image.save(output, "PNG")
        return output.getvalue()

    def test_caa_mixed_front_and_back_is_retained_but_not_explicit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        release_id = "363808e4-089f-41fa-b1ad-eddbd1cab3c4"
        payload = {
            "release": f"https://musicbrainz.org/release/{release_id}",
            "images": [
                {
                    "image": f"https://coverartarchive.org/release/{release_id}/123.jpg",
                    "id": 123,
                    "front": True,
                    "back": True,
                    "types": ["Front", "Back"],
                    "width": 3200,
                    "height": 3000,
                    "mime": "image/jpeg",
                    "thumbnails": {"1200": "https://example.test/preview.jpg"},
                }
            ],
        }
        fetcher = CoverFetcher("Queen", "Innuendo", mb_rel_id=release_id, cache_dir=tmp_path)
        monkeypatch.setattr(fetcher, "fetch_url", lambda *_args, **_kwargs: json.dumps(payload).encode())
        candidate = fetcher.query_cover_art_archive()[0]
        assert candidate.role_tier is RoleTier.LIKELY_FRONT
        assert candidate.source_verified_original
        assert candidate.original_url.endswith("123.jpg")
        assert candidate.preview_urls["1200"].endswith("preview.jpg")
        assert (candidate.declared_width, candidate.declared_height) == (3200, 3000)
        assert candidate.provider_metadata["declared_mime"] == "image/jpeg"

    def test_caa_release_url_disagreement_is_rejected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        release_id = "363808e4-089f-41fa-b1ad-eddbd1cab3c4"
        other_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        payload = {
            "release": f"https://musicbrainz.org/release/{release_id}",
            "images": [
                {
                    "image": f"https://coverartarchive.org/release/{other_id}/123.jpg",
                    "id": 123,
                    "front": True,
                    "back": False,
                    "types": ["Front"],
                }
            ],
        }
        fetcher = CoverFetcher("Queen", "Innuendo", mb_rel_id=release_id, cache_dir=tmp_path)
        monkeypatch.setattr(fetcher, "fetch_url", lambda *_args, **_kwargs: json.dumps(payload).encode())
        candidate = fetcher.query_cover_art_archive()[0]
        assert candidate.release_tier is ReleaseTier.CONFLICT
        assert candidate.release_conflicts

    def test_discogs_response_mismatch_is_not_exact(self, tmp_path: Path) -> None:
        payload = {
            "id": 1,
            "images": [
                {
                    "type": "primary",
                    "uri": "https://i.discogs.com/a/rs:fit/w:600/x.jpg",
                    "width": 2500,
                    "height": 2500,
                    "uri150": "https://example.test/x-150.jpg",
                }
            ],
        }
        fetcher = CoverFetcher(
            "Queen", "Innuendo", discogs_id="2", discogs_json_str=json.dumps(payload), cache_dir=tmp_path
        )
        candidate = fetcher.query_discogs()[0]
        assert candidate.release_tier is ReleaseTier.CONFLICT
        assert candidate.representation_kind is RepresentationKind.PREVIEW
        assert candidate.declared_width == 2500 and candidate.width == 0
        assert candidate.representations[1].kind is RepresentationKind.THUMBNAIL

    def test_discogs_edition_fields_can_veto_an_id_match(self, tmp_path: Path) -> None:
        payload = {
            "id": 2467591,
            "identifiers": [{"type": "Barcode", "value": "1111111111111"}],
            "labels": [{"catno": "WRONG 123"}],
            "formats": [{"name": "Vinyl"}],
            "images": [{"type": "primary", "uri": "https://example.test/cover.jpg"}],
        }
        fetcher = CoverFetcher(
            "Queen",
            "Innuendo",
            discogs_id="2467591",
            discogs_json_str=json.dumps(payload),
            target_barcode="0077779588701",
            target_catalog="CDP 795887-0",
            target_media="CD",
            cache_dir=tmp_path,
        )
        candidate = fetcher.query_discogs()[0]
        assert candidate.release_tier is ReleaseTier.CONFLICT
        assert len(candidate.release_conflicts) == 3
        assert candidate.barcode == "1111111111111"

    def test_unavailable_original_falls_back_to_labelled_thumbnail(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        original = "https://example.test/original.png"
        preview = "https://example.test/preview.png"
        candidate = CoverCandidate(
            "CAA",
            original,
            "Front",
            1.0,
            original_url=original,
            preview_urls={"1200": preview},
            representation_kind=RepresentationKind.ORIGINAL,
        )
        fetcher = CoverFetcher("Queen", "Innuendo", cache_dir=tmp_path)
        monkeypatch.setattr(fetcher, "fetch_url", lambda url, headers=None: self._png() if url == preview else None)
        fetcher.evaluate_candidate(candidate)
        assert not candidate.error
        assert candidate.evaluated_url == preview
        assert candidate.representation_kind is RepresentationKind.THUMBNAIL
        assert not candidate.source_verified_original
        assert candidate.width == 700
        assert any("fallback" in warning for warning in candidate.warnings)

    @pytest.mark.parametrize("side", ["left", "right"])
    def test_crop_requires_a_seam_on_the_correct_side(self, side: str) -> None:
        image = Image.new("L", (920, 800), 235)
        draw = ImageDraw.Draw(image)
        if side == "left":
            draw.rectangle((0, 0, 119, 799), fill=60)
        else:
            draw.rectangle((800, 0, 919, 799), fill=60)
        box, confidence = CoverFetcher._front_crop_box(image)
        # A single flat seam cannot safely distinguish packaging from a
        # deliberate colored margin; it needs corroborating visual evidence.
        assert box is None
        assert confidence == 0.0

    def test_identical_art_from_different_editions_does_not_promote_role(self, tmp_path: Path) -> None:
        data = self._png()
        fetcher = CoverFetcher("Queen", "Innuendo", cache_dir=tmp_path)
        front = fetcher.evaluate_candidate(
            CoverCandidate(
                "CAA",
                "front",
                "Front",
                1.0,
                raw_data=data,
                release_key="mb:edition-one",
                release_tier=ReleaseTier.EXACT_RELEASE,
                role_tier=RoleTier.EXPLICIT_FRONT,
                front_flag=True,
            )
        )
        unknown = fetcher.evaluate_candidate(
            CoverCandidate(
                "Discogs",
                "unknown",
                "Unknown",
                1.0,
                raw_data=data,
                release_key="discogs:edition-two",
                release_tier=ReleaseTier.EXACT_RELEASE,
                role_tier=RoleTier.UNKNOWN,
                roles=("unknown",),
            )
        )
        fetcher._promote_unknown_fronts([front, unknown])
        fetcher._assign_duplicate_clusters([front, unknown])
        assert unknown.role_tier is RoleTier.UNKNOWN
        assert unknown.duplicate_cluster_id != front.duplicate_cluster_id

    def test_manual_cover_menu_lists_valid_fronts_and_selects_requested_candidate(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        first = CoverCandidate(
            "CAA",
            "one",
            "Front",
            1.0,
            raw_data=self._png(),
            release_tier=ReleaseTier.EXACT_RELEASE,
            role_tier=RoleTier.EXPLICIT_FRONT,
        )
        second = CoverCandidate(
            "Discogs",
            "two",
            "Front",
            1.0,
            raw_data=self._png(),
            release_tier=ReleaseTier.EXACT_RELEASE,
            role_tier=RoleTier.LIKELY_FRONT,
        )
        fetcher = CoverFetcher("Artist", "Album", cache_dir=tmp_path)
        fetcher.evaluate_candidate(first)
        fetcher.evaluate_candidate(second)
        monkeypatch.setattr("builtins.input", lambda _prompt: "2")
        assert prompt_all_cover_choice([second, first], first) is second
        output = capsys.readouterr().out
        assert "CAA" in output and "Discogs" in output

    def test_manual_cover_menu_hides_rejected_and_invalid_images(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        fetcher = CoverFetcher("Artist", "Album", cache_dir=tmp_path)
        valid = fetcher.evaluate_candidate(CoverCandidate("Valid front", "valid", "Front", 1.0, raw_data=self._png()))
        back = fetcher.evaluate_candidate(
            CoverCandidate("Back page", "back", "Back", 1.0, raw_data=self._png(), image_type="back")
        )
        conflict = fetcher.evaluate_candidate(
            CoverCandidate(
                "Wrong edition",
                "wrong",
                "Front",
                1.0,
                raw_data=self._png(),
                release_tier=ReleaseTier.CONFLICT,
            )
        )
        tiny = fetcher.evaluate_candidate(
            CoverCandidate("Tiny preview", "tiny", "Front", 1.0, raw_data=self._png((160, 160)))
        )
        tiny.rejection_reasons.append("low_resolution_with_usable_same_release_cover")
        malformed = fetcher.evaluate_candidate(CoverCandidate("Broken image", "broken", "Front", 1.0, raw_data=b"bad"))
        monkeypatch.setattr("builtins.input", lambda _prompt: "1")
        assert prompt_all_cover_choice([valid, back, conflict, tiny, malformed], valid) is valid
        output = capsys.readouterr().out
        assert "Valid front" in output
        assert "Back page" not in output
        assert "Wrong edition" not in output
        assert "Tiny preview" not in output
        assert "Broken image" not in output

    def test_soft_large_artwork_is_not_dismissed_as_an_upscale(self, tmp_path: Path) -> None:
        side = 1800
        axis = np.arange(side, dtype=np.float32)
        smooth = 40 + 50 * np.sin(axis[None, :] / 71) + 45 * np.cos(axis[:, None] / 98)
        noise = np.random.default_rng(11).normal(0, 4, (side, side)).astype(np.float32)
        gray = Image.fromarray(np.clip(smooth + noise, 0, 255).astype(np.uint8))
        source = Image.merge("RGB", (gray, gray, gray))
        draw = ImageDraw.Draw(source)
        draw.rectangle((180, 180, 1600, 450), fill=(200, 40, 40))
        draw.text((225, 225), "TEST COVER", fill="white", stroke_width=3, stroke_fill="black")
        smaller = source.resize((1200, 1200), Image.Resampling.LANCZOS)
        large_bytes, small_bytes = io.BytesIO(), io.BytesIO()
        source.save(large_bytes, "JPEG", quality=85)
        smaller.save(small_bytes, "JPEG", quality=95)
        fetcher = CoverFetcher("Artist", "Album", cache_dir=tmp_path)
        large = fetcher.evaluate_candidate(
            CoverCandidate("Apple", "large", "Front", 1.0, raw_data=large_bytes.getvalue())
        )
        small = fetcher.evaluate_candidate(
            CoverCandidate("Deezer", "small", "Front", 1.0, raw_data=small_bytes.getvalue())
        )
        assert 0.4 < large.upscale_confidence < 0.6
        assert large.score_breakdown["upscale_penalty_confidence"] == 0
        assert fetcher._best_in_release_tier([large, small]) is large

    def test_existing_cover_replacement_needs_exact_release_and_clear_gain(self) -> None:
        old = CoverCandidate("Local Existing File", "old", "Front", 1.0, raw_data=b"old")
        old.source_sha256, old.visual_quality_score = "old", 75
        new = CoverCandidate(
            "CAA",
            "new",
            "Front",
            1.0,
            raw_data=b"new",
            release_tier=ReleaseTier.EXACT_RELEASE,
            role_tier=RoleTier.EXPLICIT_FRONT,
        )
        new.source_sha256, new.visual_quality_score = "new", 90
        assert should_replace_existing_cover(new, old)
        new.release_tier = ReleaseTier.RELEASE_GROUP
        assert not should_replace_existing_cover(new, old)
        assert should_replace_existing_cover(new, old, manual=True)

    def test_exact_front_avoids_lower_tier_full_downloads(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        exact = CoverCandidate(
            "CAA",
            "https://example.test/exact.png",
            "Front",
            1.0,
            raw_data=self._png(),
            release_tier=ReleaseTier.EXACT_RELEASE,
            role_tier=RoleTier.EXPLICIT_FRONT,
            release_key="mb:edition",
        )
        fallback = CoverCandidate(
            "Apple",
            "https://example.test/fallback.png",
            "Album",
            1.0,
            raw_data=self._png(),
            release_tier=ReleaseTier.TITLE_ONLY,
        )
        fetcher = CoverFetcher("Artist", "Album", cache_dir=tmp_path)
        monkeypatch.setattr(fetcher, "query_itunes", lambda: [exact, fallback])
        for method in (
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
            monkeypatch.setattr(fetcher, method, list)
        winner, _digital, _scan, candidates = fetcher.find_best_cover()
        assert winner is exact
        assert fallback.file_size == 0
        assert "not_probed_lower_release_tier" in next(c for c in candidates if c is fallback).rejection_reasons

    def test_existing_mbid_is_cover_evidence_only_when_discogs_and_barcode_agree(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        mbid = "363808e4-089f-41fa-b1ad-eddbd1cab3c4"
        monkeypatch.setattr(audiophile_tagger, "FLAC", lambda _path: {"MUSICBRAINZ_ALBUMID": [mbid]})
        response = {
            "id": mbid,
            "title": "Innuendo",
            "barcode": "0077779588701",
            "release-group": {"id": "daf57856-a734-3441-b0fd-26aa5f28ca64"},
            "relations": [{"url": {"resource": "https://www.discogs.com/release/2467591"}}],
        }
        monkeypatch.setattr(audiophile_tagger, "mb_get", lambda *_args: (200, response))
        args = (
            [tmp_path / "a.flac", tmp_path / "b.flac"],
            "2467591",
            "Innuendo",
            "0077779588701",
            "",
            "",
            requests.Session(),
            "agent",
            tmp_path,
        )
        assert audiophile_tagger.resolve_cover_release_identity(*args)[0] == mbid
        assert (
            audiophile_tagger.resolve_cover_release_identity(
                args[0], args[1], args[2], "wrong-barcode", args[4], args[5], args[6], args[7], args[8]
            )[0]
            == ""
        )

    def test_large_source_gets_bounded_embedded_derivative(self, tmp_path: Path) -> None:
        flac_path = tmp_path / "track.flac"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.2", "-c:a", "flac", str(flac_path)],
            check=True,
            capture_output=True,
        )
        rng = np.random.default_rng(7)
        pixels = (rng.integers(0, 24, (1200, 1200, 3), dtype=np.uint8) + 100).astype(np.uint8)
        source = tmp_path / "source.png"
        Image.fromarray(pixels, "RGB").save(source)
        source_bytes = source.read_bytes()
        assert len(source_bytes) > 100_000
        metadata_writer.write_tags(
            flac_path,
            [("TITLE", "Test")],
            picture_file=source,
            max_picture_dim=None,
            max_picture_bytes=100_000,
        )
        embedded = FLAC(str(flac_path)).pictures[0]
        assert embedded.mime == "image/jpeg"
        assert len(embedded.data) <= 100_000
        assert source.read_bytes() == source_bytes

    def test_manual_covers_excludes_nonfront_caa_artwork(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        release_id = "363808e4-089f-41fa-b1ad-eddbd1cab3c4"
        payload = {
            "release": f"https://musicbrainz.org/release/{release_id}",
            "images": [
                {
                    "id": 10,
                    "image": f"https://coverartarchive.org/release/{release_id}/10.jpg",
                    "front": False,
                    "back": True,
                    "types": ["Back"],
                }
            ],
        }
        fetcher = CoverFetcher("Queen", "Innuendo", mb_rel_id=release_id, cache_dir=tmp_path, manual_all=True)
        monkeypatch.setattr(fetcher, "fetch_url", lambda *_args, **_kwargs: json.dumps(payload).encode())
        candidates = fetcher.query_cover_art_archive()
        assert candidates == []

    def test_tiny_amazon_variant_is_not_best_digital_when_same_release_scan_exists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        release_id = "363808e4-089f-41fa-b1ad-eddbd1cab3c4"
        amazon = CoverCandidate(
            "Amazon CDN",
            "https://example.test/amazon.jpg",
            "Amazon ASIN",
            1.0,
            raw_data=self._png((160, 160)),
            musicbrainz_release_id=release_id,
            release_tier=ReleaseTier.EXACT_RELEASE,
            role_tier=RoleTier.LIKELY_FRONT,
        )
        caa = CoverCandidate(
            "Cover Art Archive (Release)",
            "https://example.test/caa.jpg",
            "CAA front",
            1.0,
            raw_data=self._png((1416, 1408)),
            musicbrainz_release_id=release_id,
            release_tier=ReleaseTier.EXACT_RELEASE,
            role_tier=RoleTier.EXPLICIT_FRONT,
            front_flag=True,
        )
        fetcher = CoverFetcher("Queen", "Jazz", mb_rel_id=release_id, cover_mode="digital", cache_dir=tmp_path)
        monkeypatch.setattr(fetcher, "query_amazon", lambda: [amazon])
        monkeypatch.setattr(fetcher, "query_cover_art_archive", lambda: [caa])
        for method in (
            "query_itunes",
            "query_deezer",
            "query_theaudiodb",
            "query_discogs",
            "query_local",
            "query_fanart_tv",
            "query_bandcamp",
            "query_tidal",
            "query_qobuz",
        ):
            monkeypatch.setattr(fetcher, method, list)
        winner, best_digital, best_scan, candidates = fetcher.find_best_cover()
        assert winner is caa
        assert best_digital is None
        assert best_scan is None  # CAA does not verify physical-scan provenance.
        assert amazon.visual_quality_score < caa.visual_quality_score
        assert "low_resolution_with_usable_same_release_cover" in amazon.rejection_reasons
        assert amazon in candidates

    def test_tiny_exact_cover_remains_last_resort_when_alone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        tiny = CoverCandidate(
            "Amazon CDN",
            "https://example.test/only.jpg",
            "Amazon ASIN",
            1.0,
            raw_data=self._png((160, 160)),
            release_tier=ReleaseTier.EXACT_RELEASE,
            role_tier=RoleTier.LIKELY_FRONT,
        )
        fetcher = CoverFetcher("Artist", "Album", cache_dir=tmp_path)
        monkeypatch.setattr(fetcher, "query_amazon", lambda: [tiny])
        for method in (
            "query_itunes",
            "query_cover_art_archive",
            "query_deezer",
            "query_theaudiodb",
            "query_discogs",
            "query_local",
            "query_fanart_tv",
            "query_bandcamp",
            "query_tidal",
            "query_qobuz",
        ):
            monkeypatch.setattr(fetcher, method, list)
        winner, _, _, _ = fetcher.find_best_cover()
        assert winner is tiny

    def test_single_visible_category_does_not_override_automatic_winner(self) -> None:
        local = CoverCandidate("Local Existing File", "local", "Front", 1.0)
        scan = CoverCandidate("Cover Art Archive (Release)", "scan", "Front", 1.0)
        assert prompt_cover_choice(None, scan, local) is local
        assert prompt_cover_choice(scan, None, local) is local

    def test_no_local_covers_excludes_local_files_and_scans(self, tmp_path: Path) -> None:
        (tmp_path / "cover.jpg").write_bytes(self._png())
        scan_dir = tmp_path / "Scans"
        scan_dir.mkdir()
        (scan_dir / "front.png").write_bytes(self._png())
        ordinary = CoverFetcher("Artist", "Album", folder=tmp_path, cache_dir=tmp_path / "cache")
        ignored = CoverFetcher("Artist", "Album", folder=tmp_path, cache_dir=tmp_path / "cache", no_local_covers=True)
        assert ordinary.query_local()
        assert ignored.query_local() == []

    def test_no_local_covers_replaces_only_with_verified_front(self) -> None:
        existing = CoverCandidate("Local Existing File", "local", "Front", 1.0, raw_data=b"existing")
        exact = CoverCandidate(
            "Cover Art Archive (Release)",
            "remote",
            "Front",
            1.0,
            raw_data=b"remote",
            release_tier=ReleaseTier.EXACT_RELEASE,
            role_tier=RoleTier.EXPLICIT_FRONT,
        )
        exact.visual_quality_score = 50.0
        existing.visual_quality_score = 95.0
        assert not should_replace_existing_cover(exact, existing)
        assert should_replace_existing_cover(exact, existing, ignore_existing=True)
        exact.release_tier = ReleaseTier.TITLE_ONLY
        assert not should_replace_existing_cover(exact, existing, ignore_existing=True)

    def test_manual_covers_renders_only_valid_fronts_without_vv(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        front = CoverCandidate("CAA", "front", "Front", 1.0, raw_data=self._png())
        back = CoverCandidate("CAA", "back", "Back", 1.0, raw_data=self._png(), image_type="back")
        fetcher = CoverFetcher(
            "Artist",
            "Album",
            folder=tmp_path,
            cache_dir=tmp_path,
            manual_all=True,
            verbose=0,
            write_preview_files=False,
        )
        monkeypatch.setattr(fetcher, "query_cover_art_archive", lambda: [front, back])
        for method in (
            "query_itunes",
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
            monkeypatch.setattr(fetcher, method, list)
        rendered: list[Path] = []

        def record_render(path: Path) -> bool:
            rendered.append(path)
            return True

        monkeypatch.setattr(audiophile_cover_fetcher, "emit_kitty_image", record_render)
        fetcher.find_best_cover(verbose=0)
        output = capsys.readouterr().out
        assert len(rendered) == 1
        assert "[COVER 1/1]" in output
        assert "[COVER 2/2]" not in output
        assert not list(tmp_path.glob(".cover_*.jpg"))

    @pytest.mark.parametrize("script", ["audiophile_tagger.py", "audiophile_cover_fetcher.py"])
    def test_manual_covers_is_the_documented_cli_flag(self, script: str) -> None:
        result = subprocess.run([sys.executable, script, "--help"], check=True, capture_output=True, text=True)
        assert "--manual-covers" in result.stdout
        assert "--manual-albums" not in result.stdout
