# AudiophileArchTagger

The project lives in `/home/emvis/Music/auph`; the music collection stays in
`/home/emvis/Music`. From the collection directory, invoke it with
`auph/.venv/bin/python auph/audiophile_tagger.py --dry-run -v "Queen/Album"`.
Run development checks from `auph`. Credentials in `auph/.env`, the virtual
environment, provider caches and private session logs are excluded from Git.
Historic artwork, discovery, and DR14 tests read the adjacent `../Queen`
collection; they do not modify it. Those local inputs are required for the
complete local suite and are not included in this repository. Lyrics provider
tests use temporary caches, independent of user caches and earlier test runs.

AudiophileArchTagger enriches FLAC metadata while treating preservation and
release identity as hard constraints. It uses Discogs for physical-release
evidence, MusicBrainz and AcoustID for release/recording identity, local CUE and
rip logs for layout evidence, optional lyrics services, local DR14 measurement,
and `loudgain` for ReplayGain 2.0.

The project does not assume that any catalogue is automatically authoritative.
Remote data is accepted only when identifiers, release facts, track layout and
winner margins are compatible. Ambiguous candidates are reported and
release-specific fields are withheld.

## Safety model

The active implementation is Python-only. The obsolete Fish implementation has
been removed.

- Existing unknown Vorbis comments are preserved by default.
- Conflicting incoming values for a single-valued field are rejected before
  writing; the manifest order does not decide which value is true.
- Every managed field receives a `KEEP`, `ADD`, `REPLACE`, `MERGE`, or
  `REVIEW_REQUIRED` decision.
- Stable-ID conflicts are never overwritten automatically.
- An album is processed in a staging tree. Originals are replaced only after
  every staged FLAC validates; commit failures restore rollback copies.
- Album commits use several individual renames. Readers can observe a mixture
  during commit; this is not an atomic album snapshot. A process crash or power
  loss can leave a partial commit. Journaled updates support explicit recovery
  through `album_transaction.py`; legacy trees need manual review. Do not delete
  retained `.metadata-transaction-*` trees before reviewing them.
- On POSIX systems, an advisory directory lock refuses concurrent album runs.
  It does not exclude unrelated applications editing the same files.
- FLAC symlinks are refused because replacing a symlink would silently remove
  the link rather than update its target.
- The writer verifies FLAC decodability, STREAMINFO geometry and decoded PCM
  SHA-256 before replacement, including files without a stored audio MD5.
  ReplayGain also checks decoded samples before publishing scanner output.
  These checks use temporary decoded files and add decoding time/disk usage.
- Normal terminal use asks for an explicit cover choice per album. The menu
  compares the best eligible digital, physical-scan, and local front covers;
  any validated crop is already applied in the preview. Other picture types
  are always preserved.
- Local lyrics, embedded lyrics, artwork and sidecars are preservation inputs;
  remote candidates do not overwrite them silently.
- Each committed write links to an append-only `.metadata-provenance.jsonl`
  record containing snapshots, decisions, picture hashes, source and audio
  identity.
- Provenance publication uses a complete temporary ledger and rename, so a
  failed write/fsync cannot leave a partial appended row. Lyric tag archives
  are published with successful writes; failed verification leaves none.
- `--dry-run` performs matching and field planning without committing FLAC,
  artwork, lyric-sidecar, DR, ReplayGain, or provenance changes.
- Native DR reports are staged with the album and committed only after the
  staged FLACs validate. Partial DR measurement does not yield an album DR.

No metadata tool can guarantee that community catalogue data is error-free.
Review `REVIEW_REQUIRED` decisions and provider evidence for rare editions.

## Canonical metadata choices

- `DATE` is the date of the specific tagged edition.
- `ORIGINALDATE` is the earliest reliably evidenced release date.
- `ARTIST` and `ALBUMARTIST` are credited display strings.
- Repeated `ARTISTS` and `ALBUMARTISTS` contain atomic structured credits when
  a provider supplies them. Display strings are never split heuristically.
- `MUSICBRAINZ_TRACKID` is the recording MBID.
- `MUSICBRAINZ_RELEASETRACKID` is the release-track MBID.
- Audio-derived layouts are never stored as `MUSICBRAINZ_DISCID`; only an exact,
  non-partial physical TOC may populate that field.
- Repeatable fields are stored as repeated Vorbis comments, not flattened with
  semicolons.
- Metadata comparison/storage uses NFC without typography/capitalization rewriting.
  Lyric source forms retain their original Unicode normalization and framing.
- Missing optional facts are omitted; placeholders such as `Unknown` are not
  fabricated.
- Edition descriptions use `EDITION`. `VERSION` is reserved for a recording or
  track version.

The complete schema and cardinality rules live in `metadata_schema.py`; policy
thresholds live in `metadata_policy.toml`.

## Matching behavior

Release candidates are rejected on incompatible artist/title, medium, physical
layout, duration vectors, identifiers, fingerprints, or edition qualifiers.
Automatic selection requires both an absolute score and a clear winner margin.
Release-group search may discover candidates, but a release group is never used
as a fallback edition.

Track matching uses medium position, credited artist, title/version qualifiers,
duration, track position and available fingerprints. Major duration or version
conflicts are hard failures. Nested `CD1`, `Disc 2`, and similar layouts use
per-disc positions and totals.

Artwork is identity-gated before aesthetic scoring. Selection prefers the
highest eligible release tier. If exact/local artwork is unavailable, automatic
mode can use eligible release-group or title-matched service artwork; this is
weaker evidence, not a verified edition match. Existing covers retain the
separate conservative replacement policy. Manual mode exposes eligible
alternatives and their evidence.

Cover discovery keeps original and preview URLs separate. Candidates are
compared by release tier, cover role, then measured image quality; provider
names, nominal DPI, bytes and format do not grant quality points. The original
winning bytes are retained as `cover.original.<format>`. A compatible display
`cover.jpg` and, if needed, a bounded embedded derivative can be generated
without replacing that archival source. Verbose mode (`-vv`) reports individual
quality signals, rejection reasons and fetch/analysis counts.

## Lyrics behavior

Local `.txt`, `.lrc`, `.elrc`, `.ttml`, translation and romanization sidecars,
plus valid embedded lyrics, take priority and are not rewritten. When no curated
local lyrics exist, provider results are collected and scored by title, artist,
album, duration, language, ISRC/recording ID, timestamp validity, duplication
and generated status. Correct plain lyrics can beat incorrect synced lyrics.
Generated candidates are disabled by default and never masquerade as curated
text.

Plain conversion removes timing transport only. It preserves stanza breaks,
repetitions, punctuation and Unicode typography; configured display derivatives
omit standalone section labels such as `[Chorus]`, while provider-native raw
evidence retains them. Synchronized
timestamps must be monotonic and within the track duration. The pipeline never
fabricates timestamps.

## Loudness and dynamics

For FLAC, `loudgain -a -k -s e` is used by default to write interoperable
ReplayGain 2.0 fields. Clipping prevention (`-k`) is configurable. Generic
`R128_TRACK_GAIN`/`R128_ALBUM_GAIN` tags are not written to FLAC. DR14 is an
optional local diagnostic and is not a substitute for ReplayGain or integrated
loudness. Dynamic Range Database results are comparison-only and are not
imported as local measurements or edition identity.

## Configuration

Python 3.12 or later is required by the pinned dependencies; the audited environment is Python 3.14. Install the tested Python dependencies with
`python3 -m pip install -r requirements.txt`; for development checks use
`python3 -m pip install -r requirements-dev.txt`. FLAC stream verification
requires the `flac` executable. Generated audio fixtures use `ffmpeg`.
ReplayGain requires `loudgain`; when it is unavailable, new gain calculation is
skipped. For incomplete/unknown albums, stale album gain/peak/range tags are
cleared even when track measurements are missing; this cleanup does not claim
to have scanned or validated the retained track tags. If existing album ReplayGain values disagree
between tracks, the command recalculates them when possible; without the
scanner it retains valid track values and removes the inconsistent album
values. A failed scan aborts the staged album update. The native DR path does
not require `dr14_tmeter`.

Completeness reconciles filename, disc-directory and embedded positions;
numbered files with reliable totals can establish completeness without an
embedded track number. Conflicting total aliases or disc evidence prevent album
gain. DR measurements use album-relative paths, so identically named files on
different discs retain independent measurements. Gain validation rejects
non-finite numeric values. Incomplete rollback retains recovery copies for
manual inspection rather than deleting them.

Edit `metadata_policy.toml` or set `AUDIOPHILE_CONFIG` to another policy file.
Explicit missing files, unknown settings, wrong types, non-finite/negative
thresholds and unsupported destructive preservation settings are rejected.
Changes to a policy file invalidate the in-process configuration cache.
The shipped defaults are preservation-safe:

```toml
[matching]
release_min_score = 500.0
release_min_margin = 120.0
discogs_min_margin = 150.0
acoustid_min_score = 0.80

[lyrics]
online_lookup = true
generated_candidates = false
minimum_candidate_score = 75.0

[loudness]
enabled = true
prevent_clipping = true
write_r128_flac_tags = false

[preservation]
preserve_unknown_comments = true
preserve_existing_lyrics = true
preserve_existing_artwork_file = true
provenance_ledger = true
```

Credentials may be supplied in `.env`, the environment, or command-line
options:

```ini
DISCOGS_TOKEN=your_discogs_token
MB_CONTACT=you@example.org
ACOUSTID_API_KEY=your_acoustid_key
```

## Usage

Preview decisions without changing the library:

```bash
python3 audiophile_tagger.py --dry-run -v "Queen/A Night at the Opera"
```

Process one or more album folders:

```bash
python3 audiophile_tagger.py
python3 audiophile_tagger.py --threads 4 "Queen/A Night at the Opera"
python3 audiophile_tagger.py -vv "Various Artists/*"
python3 audiophile_tagger.py --auto-covers "Queen/Innuendo"
python3 audiophile_tagger.py --manual-covers "Queen/Innuendo"
python3 audiophile_tagger.py --no-local-covers "Queen/Jazz"
```

With no flags, each album shows its available category representatives and
waits for a number or `R` to reject artwork for that album. One candidate
still requires confirmation. The menu shows the prepared front-cover image;
the original scan remains archived. If terminal graphics are unavailable, the
menu prints paths to the prepared images for inspection while the prompt is
open. A terminal is required for this default mode. `--auto-covers` bypasses
the prompt for automation; `--quiet` also remains non-interactive. The
standalone `audiophile_cover_fetcher.py --artist ... --album ...` uses the same
category menu by default.

`--manual-covers` renders every valid front-cover candidate in the terminal,
then lists them for selection. Back covers, booklets, wrong-release artwork,
malformed files and candidates rejected by quality checks are omitted. Enter
a number to choose one, `v N` to preview it again,
or press Enter for the automatic choice. The standalone
`audiophile_cover_fetcher.py` accepts the same option in interactive mode.

`--no-local-covers` removes local cover files, scan folders, and embedded fronts
from discovery and ranking. If an exact-release remote front wins, it replaces
the existing display cover and embedded fronts transactionally while preserving
the previous cover as a backup. When no verified remote front is available,
existing artwork is left in place rather than deleted. The standalone cover
fetcher accepts the same flag.

Validate a library with severity-classified findings:

```bash
python3 verify_all_flacs.py /path/to/library
```

Run offline verification:

```bash
python3 -m pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy .
```

The suite contains unit, regression, fixture-style integration, serialization,
matching, lyrics, transaction, provenance, cache/retry and library-QC coverage.
Live provider access is not required for the default test run.

## Main modules

| Module | Responsibility |
| --- | --- |
| `audiophile_tagger.py` | Album orchestration, evidence collection and staged commit |
| `metadata_schema.py` | Canonical fields, cardinality and identity scope |
| `metadata_policy.py` | Field-level overwrite decisions |
| `metadata_writer.py` | Transactional FLAC writer and provenance ledger |
| `release_match.py` | Release acceptance and ambiguity handling |
| `mb_identity_score.py` | Discogs/MusicBrainz identity evidence scoring |
| `mb_track_match.py` | Medium and recording-track matching |
| `lyrics_pipeline.py` / `lyrics_quality.py` | Immutable-local lyrics and candidate scoring |
| `audiophile_cover_fetcher.py` | Identity-gated artwork collection and ranking |
| `provider_cache.py` | Versioned TTL caches and Retry-After handling |
| `verify_all_flacs.py` | Track, album and cross-library quality control |

## Compatibility notes

Vorbis comments are intentionally extensible; several fields above are common
MusicBrainz/Picard conventions rather than Xiph-standardized names. Some players
do not expose repeated fields or synchronized lyrics consistently. Keep sidecars
when they are part of the archive and test playback behavior before changing
the compatibility policy for a whole library.

## Library safety defaults

- A normal invocation discovers the entire library; the historical curated list
  is only a hint when explicitly selected.
- Pipeline-private directories such as `.metadata-transaction-*` are never
  treated as music.  The validator reports abandoned transactions for manual
  recovery rather than deleting them.
- Symlinked source FLACs and `cover.jpg` require manual review before an album
  transaction. An exact Disc ID from a separate-track CUE requires a matching
  EAC log and local audio durations; an unrelated log is ignored.
- A matched release with additional discs makes a local subset incomplete for
  album ReplayGain, even when local `TOTALTRACKS` tags are internally consistent.
- Reprocessing an unchanged album preserves FLAC bytes, artwork, sidecar names,
  and provenance IDs. A new provenance row is written only for a real tag or
  picture change.
- Existing tags without a provenance record are treated as **unknown
  provenance** by the enrichment pipeline: conflicting remote data becomes a
  review item, not an automatic replacement.
- `TRACKTOTAL`/`TOTALTRACKS` and `DISCTOTAL`/`TOTALDISCS` are mirrored for
  Picard/player compatibility when their values agree.
- Playback lyrics follow the configured Poweramp/Simphonium/MusicBee profile:
  synced text is stored in `LYRICS`, `SYNCEDLYRICS`, and `SYNCELYRICS`; plain
  text is stored in `UNSYNCEDLYRICS`. Raw provider evidence remains sidecar
  material and section labels are removed only from display derivatives.

## Whole-project verification

See [WHOLE_PROJECT_AUDIT.md](WHOLE_PROJECT_AUDIT.md) for the October 4 audit,
confirmed defects, module coverage, temporary-fixture validation and remaining
limits. [SELECTION_AUDIT.md](SELECTION_AUDIT.md) contains the detailed lyrics/art
provider and failure matrices; fixture coverage does not certify live services.

Album publication remains several individual operations. Catchable commit
interruptions attempt rollback. Durable journals now support explicit recovery
after process termination; physical power-loss guarantees remain unverified. New sidecars/reports/archives are published exclusively so a
late destination collision aborts rather than overwriting another application's
file. This requires hard-link support on the staging filesystem; unsupported
filesystems fail safely and trigger rollback.

Native DR14 now reports measured short-track/mono peak and sine-reference RMS,
handles silence, and excludes phantom empty blocks. Values may differ from old
reports; preserved existing reports are not automatically rewritten. Partial
measurement withholds album DR, and the measurement CLI returns nonzero on
failure. Exact agreement with proprietary DR meters has not been certified.


## Audit backlog implementation

[IMPLEMENTATION_BACKLOG.md](IMPLEMENTATION_BACKLOG.md) maps the completed audits,
checklists, field recommendations and remaining limits to acceptance criteria
and current verification. Prior audit counts describe their dated checkouts.

Before publishing an album or a default standalone writer update, the pipeline
syncs a journal containing the exact destinations, prior/new hashes and recovery
copies. A new run refuses a journaled unfinished update, including standalone
writes in normal CD/Disc subfolders of that album. Inspect the intended
recovery without writes, then restore an interrupted publication explicitly:

```sh
.venv/bin/python album_transaction.py --dry-run "/path/to/album"
.venv/bin/python album_transaction.py "/path/to/album"
```

Recovery preflights all destinations and backups. It refuses symlinks, path
traversal, damaged snapshots and unrelated later edits, retaining recovery data
for inspection. A fully committed journal cleans up without undoing the new
metadata. Recovery records a durable rolled-back state before cleanup, so an
interrupted cleanup can be retried even after snapshots have been removed.
Root recovery includes normal direct CD/Disc subfolder journals and holds the
same album-root lock used by standalone writers. Legacy trees and standalone ledgers outside the track directory
remain manual-recovery cases. Unknown outside-journal temporary files are left
for inspection. Multiple replacements are still observable individually.

Standalone writer and provenance locks coordinate cooperating POSIX processes.
Track-worker headers/cookies are independent; adapter pools remain owned by the
parent session. Custom authentication/transport callbacks retain their behavior
but their thread safety is not certified. MusicBrainz/Discogs request spacing
also coordinates across processes on this host. Private lock/rate state lives
in the per-user temporary directory, outside the music collection.

JSON parser/cache version is `2026-10-05-lyrics-v11`. Actual prepared session
URL query parameters and credentials/cookies/language affect cache identity. Unknown server `Vary`
dimensions disable persistence for positive and negative responses. Parser v11
invalidates v10 entries that could contain unsafe negatives. Explicit API error messages
remain typed failures; parser/auth/rate failures do not become NOT_FOUND caches.

LRCLIB Lyricsfile 1.0 draft YAML now contributes plain, line and supplied word
timing. Exact YAML and provider JSON are retained separately. Duplicate keys,
aliases, unsafe tags and unsupported versions are rejected; invalid timing
keeps usable plain text. Offset semantics that the draft has not settled are
archived without synchronized playback guesses. TTML derivatives require real
source intervals; absent ends are never invented. These are fixture-verified
contracts based on the [upstream draft](https://github.com/tranxuanthang/lyricsfile/blob/main/SPECIFICATION.md),
not a certification of live content completeness.

DR/peak/BPM processing uses disk-backed decoded PCM and bounded NumPy blocks.
It still needs temporary storage for a full decoded FLAC and a bounded decoder
runtime; this is not an unlimited-size guarantee. Native DR provenance uses
`native_dr14_v2`. Scanner records include command flags and executable digest;
metadata-only cleanup does not claim a newly measured RG2 reference or peak.

Dry-run emits old/proposed values and source/confidence, including review
proposals that were not applied. It also reports picture inventories by hash.
Collection contents remain unchanged; private cache/lock/temp activity is
separate from collection writes.

Independent verification of `663992b` and its follow-up fixes are recorded in
[IMPLEMENTATION_BACKLOG.md](IMPLEMENTATION_BACKLOG.md#independent-verification-of-663992b--2026-10-05).
Standalone lyric tag archives now publish complete bytes exclusively. Album
ledger locks cover the journal snapshot through commit, and instrumental native
YAML/raw sources are archived with their provider identity instead of discarded.
