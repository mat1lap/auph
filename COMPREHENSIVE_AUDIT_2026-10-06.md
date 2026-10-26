# Implementation audit — 2026-10-06

Baseline: clean Git checkout `63d1de2` in `/home/emvis/Music/auph`.
No repository or ancestor `AGENTS.md` was present. All 28 production Python
modules and their principal callers were included in the review. Requirements,
lint configuration, preservation policy, README, historical audit documents,
test fixtures and CLI entry points were inspected. No replacement modules were
invented, no dependencies or checks were weakened, and no commit was made.

The initial suite passed **659 tests in 120.06 seconds**. Baseline Ruff,
formatting, mypy and dependency consistency checks also passed. New tests use
temporary FLACs, synthetic images and offline provider responses. No live
enrichment was run on the music collection.

## Execution and coverage

Discovery (`discogs_release_find`, `library_files`) → positional/local evidence
(`library_layout`, CUE/log parsers, existing comments) → Discogs/MB/AcoustID
discovery and identity gates → artwork/lyrics acquisition, validation and
selection → track enrichment → staged writer and audio verification →
completeness-dependent loudness → journaled album publication and ledger →
explicit recovery and canonical verification.

| Production modules | Inspected paths and verification |
| --- | --- |
| `audiophile_tagger` | CLI, provider callers, release/recording handoffs, multi-disc enrichment, dates, credits, rip analysis, lyrics/artifacts, DR/ReplayGain, workers, staging and final publication; real album and track-worker fixtures |
| `discogs_release_find` | Path discovery, local signatures, MB/AcoustID bridges, Discogs queries/detail scoring and ambiguity; new glob, private-file, positional-order, nested-evidence and pagination regressions |
| `mb_identity_score`, `mb_track_match`, `release_match` | Release versus recording identities, editions, duration units, versions, artist credits, medium/position conflicts and acceptance margins; existing malformed/ambiguous/partial-release fixtures and caller tests |
| `library_files`, `library_layout`, `album_completeness`, `mb_discid_from_cue` | Private artifacts, recursion, symlinks, numbering, totals, CUE references/encodings, physical versus synthetic TOCs and gain completeness; new medium, malformed-offset, outside-reference, nested-CUE and unreadable-FLAC regressions |
| `lyrics_utils`, `lyrics_pipeline`, `lyrics_quality`, `lyricsfile` | Adapter query/retrieval boundaries, candidate schema, raw/native forms, status/cache handoffs, identity/quality selection, local preservation, conversion and materialization; new API-schema, partial-acquisition, TTML interoperability, short-text and real-worker regressions |
| `audiophile_cover_fetcher` | Every artwork query adapter, release/front evidence, representations, bytes/decode/orientation/color limits, analysis/dedup/ranking, categories, crop/preview and saving; new exact-ID, malformed-row, cache-context, bounded-local-read and nested-embedded regressions plus existing selection/interactive tests |
| `metadata_writer`, `metadata_schema`, `metadata_policy` | Missing/empty values, aliases, scope/cardinality, overwrite/provenance policy, lyrics embedding, front/non-front preservation, PCM validation, no-op behavior and staged publication; real writer/worker and recovery tests |
| `loudness`, `dr14_parse` | Complete/partial/unknown grouping, stale gain cleanup, actual loudgain execution, validated numeric output, native DR aggregation, report resolution/reuse/source and subprocess failure; new cross-disc report and reuse/source regressions plus existing production album tests |
| `provider_cache`, `http_sessions`, `process_lock`, `album_lock`, `album_transaction` | Bounded bodies, classifications, retries, credentials, cache identity/corruption, session isolation, advisory locks, journals, snapshots, ledger coordination and recovery; existing concurrent writers, killed-process and failure-injection tests rerun |
| `pipeline_config`, `verify_all_flacs`, `check_tokens`, `check_all_providers` | Configuration validation/precedence/reload, canonical verification, token diagnostic/redaction boundaries and diagnostic routing; new CLI help/environment tests plus existing verifier/config fixtures |

The test-only `conftest.py` isolates lyrics caches. Production adapter review
covers routing and acquisition/selection contracts; fixtures do not establish
that every response from every live provider is accepted correctly.

## Confirmed findings and fixes

Severity refers to the consequence of the reproduced trigger, not to a claim
that every invocation encounters it. New regressions are in
`test_comprehensive_audit.py`; existing regression assertions remain intact.

| ID / severity | Trigger, root cause and consequence | Implemented correction / evidence |
| --- | --- | --- |
| C01 High | Multiple CUEs, including Disc 2/Disc 10: first FLAC or lexical enumeration supplied the medium, attaching TOCs to the wrong disc. | Reconcile explicit CUE directory/name/reference/tag medium evidence; ambiguity raises instead of inventing order. |
| C02 High | A synthetic/missing later-disc TOC borrowed disc 1's MB Disc ID; FreeDB IDs, TOC counts and track-indexed rip logs also used album scalars. | Scope identifiers, TOC fields, CRC/peak/pre-emphasis input and their schema scopes to the medium; retain scalar compatibility only for one disc. Worker manifest and scoped-evidence regressions. |
| C03 High | A single-image CUE with reversed indexes could produce negative track durations and an exact physical Disc ID. | Require increasing indexes within each file and increasing physical offsets. Count-only CUEs remain eligible for completeness without becoming physical TOCs. |
| C04 High | CUE audio references could follow a symlink or traverse outside the CUE directory. | Refuse symlinked/outside references before opening referenced audio. External-audio symlink regression. |
| C05 High | Missing edition date fell back to `orig_date`, silently dating a reissue as the original release. | Omit unsupported edition DATE/YEAR/DATE_SOURCE; retain independently known ORIGINALDATE. Worker manifest regression. |
| C06 High | Short local plain text bypassed plain/sync agreement checking, permitting unrelated remote timing. | Apply agreement checking to short text too; missing comparison text is not corroboration. |
| C07 Medium | Minimum character counts ignored short curated text; enhanced-only embedded lyrics did not consistently prevent lookup or produce playback artifacts. | Recognize non-structural local content; archive enhanced/native forms with collision-safe names and derive only supported existing timing. Short Unicode/plain and enhanced materialization regressions. |
| C08 Medium | Embedded word timestamp syntax alone set `has_word_sync`, even when word order was invalid. | Validate enhanced timing before making that claim. |
| C09 High | VocaDB `Names` is a localized-name array, but the adapter demanded an object; an unrelated first query prevented the independent song-only fallback. | Accept canonical localized names while retaining legacy fixture support; stop only after usable acquisition. Canonical-schema and fallback regression. |
| C10 High | Lyricsfile-generated TTML uses millisecond offsets; the parser treated `1234ms` as an invalid number. Untimed paragraph prefixes were also lost when timed spans existed. | Parse finite ms/s/m/h offsets, preserve millisecond precision and prefix text without adding word timing. Real Lyricsfile→TTML→LRC round trip. |
| C11 Medium | Unison sliced the first 20 search rows before checking relevance, losing an accessible matching later row. | Inspect bounded search payload for relevance before limiting detail retrieval to 20 matching rows; expose matching-detail truncation. |
| C12 Medium | Successful partial lyrics acquisitions entered the durable cache despite independent transient/parser failures. Interruptions also left thread-local acquisition state active. | Avoid success caching when failures are recorded or carried in acquisition diagnostics; restore state in `finally`, including KeyboardInterrupt. |
| C13 High / Medium | Absolute globs were treated as substring searches; private pending FLACs could establish album discovery; an explicit real leaf beneath a symlinked parent escaped the intended target. | Expand explicit glob patterns, reject private/symlink matches including symlinked ancestors and share disc-directory recognition. Unicode/glob/private-file and explicit-parent-traversal regressions. |
| C14 Medium | Nested/uppercase CUEs and nested rip/catalog evidence were missed; Mutagen's FLAC parser error escaped completeness handling. | Traverse case-insensitively with private/symlink filtering; reconcile exact referenced paths; unreadable FLAC gives UNKNOWN. Keep count-only CUE compatibility. |
| C15 Medium | Ordered Discogs title/duration evidence followed filenames (`1`, `10`, `2`) rather than musical positions. | Order by corroborated disc/track positions; withdraw ordered comparisons when positions conflict, are missing or duplicated. |
| C16 High | Supplied Discogs JSON lacking its own release ID inherited the requested exact ID. | Require response ID equality for exact-release artwork. Unknown evidence stays below exact tier. |
| C17 Medium | A malformed CAA/Discogs image row could discard independent usable artwork. Nested uppercase FLAC embedded fronts were missed. | Isolate malformed image rows and use shared recursive FLAC discovery for embedded fronts. |
| C18 Medium | Artwork URL-only caching mixed authentication contexts; HTTP-200 error envelopes were persisted; worker sessions lost configured state. Local/cached artwork was read before byte bounds. | Use actual credential-aware session keys and isolated state-preserving sessions; reject error envelopes; avoid unknown Vary caching; bound local/cache reads and skip local symlinks. Tidal now requires explicitly configured `TIDAL_TOKEN` rather than a built-in application token. |
| C19 Medium | Discogs searches silently sliced 8–25 first-page rows before edition validation. | Retain up to three 50-row pages per query; preserve earlier pages on later failure and warn about truncation. The existing 50-detail scoring ceiling and its warning remain. |
| C20 High / Medium | DR report explicit Disc 1 paths fell back to Disc 2 basenames; generated reports used folder names instead of resolved identity, forcing repeated decoding; rejected reports still supplied DR_SOURCE. | Preserve explicit path identity, generate reusable resolved headers and report the actual measurement source. Bound external-meter runtime. Reuse regression proves the second run performs no measurement. |
| C21 Medium | Invalid preview environment values raised before argparse; `check_all_providers --help` launched network diagnostics; positional helper help was interpreted as input. | Parse preview defaults through argparse, add diagnostic argument parsing and side-effect-free helper help. |

Cross-module verification includes a real track-worker→lyrics materialization→
metadata writer test. It checks archived enhanced text, line/plain playback
fields according to the configured profile, Unicode multivalue comments,
non-front picture bytes and decoded PCM SHA-256 before/after writing.

## Validation record

- Baseline: `.venv/bin/pytest -q` — 659 passed, 120.06 s.
- New focused regressions: `.venv/bin/pytest -q test_comprehensive_audit.py` —
  46 passed, 1.48 s (before final documentation edits).
- An intermediate full run reported 693 passed / 1 failed: the new CUE parser
  incorrectly required timing indexes for completeness-only evidence. The
  production distinction was repaired; the existing assertion was retained.
- Final `.venv/bin/pytest -q` — **705 passed in 117.52 s**, including 46 new
  regressions/integration checks. A preceding full run passed 704 tests; final
  diff review then reproduced and fixed explicit symlink-parent traversal, and
  the complete suite was rerun after that correction.
- `.venv/bin/ruff check .` — passed.
- `.venv/bin/ruff format --check .` — 51 files formatted.
- `.venv/bin/mypy .` — no issues in 42 source files.
- `.venv/bin/python -m pip check` — no broken requirements. Pip emitted a
  non-blocking user-cache ownership/writeability warning.
- `git diff --check` — passed.
- CLI smoke: subprocess calls to `.venv/bin/python SCRIPT --help` returned
  zero for all 13 entry points: tagger, cover fetcher, writer, lyrics utilities,
  Discogs finder, MB identity scorer, MB track matcher, CUE Disc ID helper,
  DR parser, canonical verifier, token checker, provider checker and journal
  recovery CLI. The helper and diagnostics help paths have offline regressions.

`ffmpeg`, `flac`, `loudgain`, `fpcalc` and `dr14_tmeter` are installed here.
Existing tests execute real loudgain and decoded-audio checks on temporary
albums, including incomplete/unknown/multi-disc grouping. Network boundaries
are replaced with deterministic fixtures, not replacement matching/writing
implementations. Existing tests also exercise automatic/manual/quiet/non-TTY
cover choice, crop/output consistency, dry-run immutability, ledger/sidecar
collisions, worker/subprocess failures, interruption, killed processes,
failed rollback retention and competing processes.

## Limits and external verification

No missing production source or installed Python dependency blocked this run.
No account credentials were exercised against live services, and the live
provider diagnostic sweep was not run. Private/observed Spotify, Deezer,
Yandex, VK, Kugou, QQ, NetEase, Bandcamp, Unison and HTML scraper schemas can
change; their fixture successes are not a live-service availability claim.
Pre-existing explicitly unsupported aggregate/identity-free adapters remain
unchanged; no provider was deleted to obtain passing tests.

Reviewed artwork routes include iTunes, Deezer, CAA, Discogs, local files/
embedded fronts, Amazon, Fanart.tv, Bandcamp, Tidal, Qobuz and TheAudioDB.
Lyrics routing includes LRCLIB/Lyricsfile, Musixmatch, Unison, Kugou, NetEase,
QQ, UltraStar, Chorus, Megalobiz, RentAnAdviser, Spotify, Deezer, Yandex, VK
Music/topics, PetitLyrics, AmDm, Bandcamp, 1ting, DarkLyrics, Metal-Archives,
VocaDB, Tekstowo and Genius, with their existing status/identity gates. Queries
and detail acquisition have provider-specific bounds; this review does not
certify exhaustive enumeration of an external catalog.

The Discogs developer site returned HTTP 403 during documentation inspection.
Pagination assumptions were checked against its maintained
[official Python client](https://github.com/discogs/discogs_client/blob/master/discogs_client/models.py).
[VocaDB public API documentation](https://wiki.vocadb.net/docs/public-api)
and its [generated song contract](https://github.com/VocaDB/vocadb-openapi-client-java/blob/main/docs/SongForApiContract.md)
support the names correction; [W3C TTML2](https://www.w3.org/TR/ttml2/#timing-value-timeExpression)
supports time-unit parsing; [CAA API](https://musicbrainz.org/doc/Cover_Art_Archive/API)
supports image role/representation review. Undocumented adapters were assessed
against repository fixtures and observed schema assumptions.

Album publication consists of individual replacements with durable snapshots,
advisory locks and an explicit recovery journal. It is **not an atomic album
transaction**: readers can see a mixed set during publication; cooperating
pipeline processes serialize, unrelated editors need not. Hardware/filesystem
power-loss guarantees, every interruption point, all malformed upstream forms,
all platform/tool versions and proprietary-meter equivalence remain uncertain.
Some audio helpers still incur full decoding or sizeable memory use; no broad
performance speedup is claimed. TTML frame/tick timing and arbitrary inherited
timing containers are not fully supported. Ambiguous optional CUE medium
evidence can require review rather than successful enrichment.

These findings and checks are evidence for the inspected behavior, not proof
that every possible defect has been eliminated.
