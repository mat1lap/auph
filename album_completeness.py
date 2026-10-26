"""Evidence based completeness decision for an album loudness scan."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mutagen import MutagenError
from mutagen.flac import FLAC

from library_files import is_pipeline_private
from library_layout import position_evidence
from mb_discid_from_cue import cue_disc_number, parse_cue


@dataclass(frozen=True)
class CompletenessDecision:
    state: str
    evidence: tuple[str, ...]


def _single(tags: FLAC, *names: str) -> str:
    for name in names:
        values = tags.get(name, [])
        if values:
            return str(values[0]).strip()
    return ""


def _number(value: str) -> int | None:
    head = value.split("/", 1)[0].strip()
    return int(head) if head.isdigit() and int(head) > 0 else None


def assess_album_completeness(
    flac_files: list[Path],
    folder: Path,
    *,
    external_disc_counts: dict[int, int] | None = None,
) -> CompletenessDecision:
    """Require contiguous local disc positions plus a trustworthy track list.

    Tag totals, a matching CUE track list, or an exact external release track
    list can establish completeness. Contradictory identity/positions never do.
    """
    if not flac_files:
        return CompletenessDecision("UNKNOWN", ("no local audio tracks",))
    positions: dict[int, set[int]] = {}
    disc_files: dict[int, set[Path]] = {}
    totals: dict[int, set[int]] = {}
    disc_totals: set[int] = set()
    identities: set[tuple[str, str]] = set()
    release_ids: set[str] = set()
    evidence: list[str] = []
    try:
        for path in flac_files:
            audio = FLAC(path)
            identities.add((_single(audio, "ALBUM").casefold(), _single(audio, "ALBUMARTIST", "ARTIST").casefold()))
            release_id = _single(audio, "MUSICBRAINZ_ALBUMID")
            if release_id:
                release_ids.add(release_id)
            tags = {str(key).upper(): list(values) for key, values in audio.items()}
            disc, track, _title, conflicts = position_evidence(path, folder, tags)
            if conflicts:
                return CompletenessDecision("INCOMPLETE", (f"{path.name}: {'; '.join(conflicts)}",))
            if track is None or disc is None:
                return CompletenessDecision("UNKNOWN", (f"missing track number: {path.name}",))
            if track in positions.setdefault(disc, set()):
                return CompletenessDecision("INCOMPLETE", (f"duplicate disc {disc} track {track}",))
            positions[disc].add(track)
            disc_files.setdefault(disc, set()).add(path.resolve())
            for field in ("TOTALTRACKS", "TRACKTOTAL"):
                for value in audio.get(field, []):
                    track_total = _number(value)
                    if track_total:
                        totals.setdefault(disc, set()).add(track_total)
            for field in ("TOTALDISCS", "DISCTOTAL"):
                for value in audio.get(field, []):
                    disc_total = _number(value)
                    if disc_total:
                        disc_totals.add(disc_total)
    except (OSError, ValueError, MutagenError) as exc:
        return CompletenessDecision("UNKNOWN", (f"unreadable local tags: {exc}",))

    if any(not album or not artist for album, artist in identities):
        return CompletenessDecision("UNKNOWN", ("local album or album-artist identity is missing",))
    if len(identities) != 1 or len(release_ids) > 1:
        return CompletenessDecision("INCOMPLETE", ("mixed album or release identities",))
    if len(disc_totals) > 1:
        return CompletenessDecision("INCOMPLETE", ("conflicting disc totals",))
    missing_release_discs = set(external_disc_counts or {}) - set(positions)
    if missing_release_discs:
        return CompletenessDecision(
            "INCOMPLETE", (f"matched release has missing local discs: {sorted(missing_release_discs)}",)
        )
    expected_discs = next(iter(disc_totals), max(positions))
    if set(positions) != set(range(1, expected_discs + 1)):
        return CompletenessDecision("INCOMPLETE", ("missing or inconsistent disc numbering",))

    cue_counts: dict[int, int] = {}
    cues = sorted(
        path
        for path in folder.rglob("*")
        if path.suffix.casefold() == ".cue"
        and path.is_file()
        and not path.is_symlink()
        and not is_pipeline_private(path, root=folder)
    )
    for cue in cues:
        try:
            rows = parse_cue(cue, require_indexes=False)
            disc = cue_disc_number(cue, folder, single_cue=len(cues) == 1)
        except (OSError, ValueError, MutagenError):
            continue
        tracks = [row["number"] for row in rows]
        named_files = [row["file"] for row in rows if row.get("file")]
        references = {cue.parent / str(name).replace("\\", "/") for name in named_files}
        if any(path.is_symlink() for path in references):
            continue
        cue_files = {
            (path if path.suffix.casefold() == ".flac" else path.with_suffix(".flac")).resolve() for path in references
        }
        # Only a per-track CUE that names every local FLAC on this disc can
        # corroborate a total. A free-floating TRACK list, or a CUE for one
        # image file, cannot establish completeness of separate local files.
        if (
            disc
            and len(named_files) == len(tracks) == len(cue_files) == len(disc_files.get(disc, set()))
            and tracks == list(range(1, len(tracks) + 1))
            and cue_files == disc_files[disc]
        ):
            cue_counts[disc] = len(tracks)
    for disc, numbers in sorted(positions.items()):
        if numbers != set(range(1, max(numbers) + 1)):
            return CompletenessDecision("INCOMPLETE", (f"disc {disc} has noncontiguous tracks",))
        declarations = totals.get(disc, set())
        if len(declarations) > 1:
            return CompletenessDecision("INCOMPLETE", (f"disc {disc} has conflicting track totals",))
        expected = next(iter(declarations), None)
        cue_expected = cue_counts.get(disc)
        external_expected = (external_disc_counts or {}).get(disc)
        if any(value is not None and value != len(numbers) for value in (expected, cue_expected, external_expected)):
            return CompletenessDecision("INCOMPLETE", (f"disc {disc} audio count conflicts with track list",))
        if expected is None and cue_expected is None and external_expected is None:
            return CompletenessDecision("UNKNOWN", (f"disc {disc} lacks a reliable track total, CUE, or release list",))
        evidence.append(
            f"disc {disc}: contiguous {len(numbers)} tracks verified by "
            + ("local totals" if expected is not None else "CUE" if cue_expected is not None else "release list")
        )
    return CompletenessDecision("COMPLETE", tuple(evidence))
