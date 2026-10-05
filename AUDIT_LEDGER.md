# Independent repository audit ledger

Current follow-up: [IMPLEMENTATION_BACKLOG.md](IMPLEMENTATION_BACKLOG.md) records the
2026-10-05 implementation and verification; older findings/counts below are historical.

This ledger records observed behavior in this checkout. It is a working record, not a claim that unexamined paths are safe. The final report will distinguish verified behavior from remaining uncertainty.

## Baseline, 2026-09-26

- Working directory: `/home/emvis/Music`. `.git/` exists but is empty; `git status` and `git log` both fail with `not a git repository`. Branch, history, and Git diff are unavailable in this supplied checkout. Existing files were not removed.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider`: **431 passed** in 89.43 s.
- `.venv/bin/ruff check .`: passed.
- `.venv/bin/ruff format --check .`: 33 files formatted.
- `.venv/bin/mypy .`: passed, 28 source files.
- No packaging manifest, CI workflow, or shell script was present in the supplied top-level inventory. Runtime tools and imports still need direct reconciliation.

## Architecture map

| Boundary | Current implementation | Persistent state or side effect |
| --- | --- | --- |
| CLI and discovery | `audiophile_tagger.py:main`, `resolve_target_albums`, `library_files.py`, `library_layout.py` | Reads album trees; selects FLACs and rip artifacts |
| Release evidence | `discogs_release_find.py`, `mb_discid_from_cue.py`, `mb_identity_score.py`, `release_match.py`, `mb_track_match.py` | Provider caches; selected edition and track IDs |
| Artwork | `audiophile_cover_fetcher.py`, cover selection in `process_album` | Staged `cover.jpg`, archival source, embedded FLAC picture |
| Lyrics | `lyrics_utils.py`, `lyrics_quality.py`, `lyrics_pipeline.py` | Provider caches, local/archival sidecars, FLAC lyric tags |
| Local audio evidence | `album_completeness.py`, `dr14_parse.py`, `loudness.py` | DR report/tags; ReplayGain tags on staged FLACs |
| Field policy and serialization | `metadata_schema.py`, `metadata_policy.py`, `metadata_writer.py` | Per-track staged FLACs and provenance rows |
| Album commit and QC | `audiophile_tagger.py:process_album`, `verify_all_flacs.py` | Same-album transaction directory, final FLACs/artwork/sidecars/ledger |
| Provider transport | `provider_cache.py`; additional direct requests in provider modules | `.api_cache`, local provider caches |

Main entry points are `audiophile_tagger.py`, `audiophile_cover_fetcher.py`, `metadata_writer.py`, `discogs_release_find.py`, `mb_discid_from_cue.py`, `dr14_parse.py`, `verify_all_flacs.py`, `check_tokens.py`, and `check_all_providers.py`. The orchestration path is `main` → `process_album` → concurrent `process_single_track` → staged `write_tags` → optional staged ReplayGain → validation → album commit. The nested writer stages and validates a disposable copy of each already staged track.

## Findings

| ID | Severity | Subsystem | Symptom and evidence | Reproduced | Regression test | Root cause | Fix | Verification | Remaining uncertainty |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A-001 | P2 | Release selection | Duplicate discovery rows could hide a close other edition, or falsely make one edition ambiguous. The current main caller deduplicates IDs upstream, so this was a utility contract defect. | Both cases reproduced | `TestReleaseSelection` duplicate-row cases | Margin compared rows, not distinct edition IDs. | Compare the best row per nonempty edition ID. | 5 focused selection tests pass. | Candidate ID provenance still depends on caller. |
| A-002 | P2 | Album transaction | Copy failure during initial album staging left `.metadata-transaction-*` residue. | Injected failure on second copy | `test_album_staging_copy_failure_cleans_residue_and_preserves_sources` | Staging initialization was outside cleanup guard. | Added guarded `stage_album_files` and failure handling in `process_album`. | Original bytes and residue assertions pass. | Sudden process death remains recoverable only manually. |
| A-003 | P0 | Album discovery | A same-named album under a different artist could redirect processing to the curated Queen path. | Reproduced | `test_curated_release_mapping_requires_the_actual_album_path` | Curated match accepted directory basename equality. | Require actual path identity. | Focused test passes. | Curated keyword expansion still needs broader CLI review. |
| A-004 | P0 | Artwork staging | Existing user files named `cover_tmp.jpg` or `cover_source_tmp.png` were deleted or replaced. | Reproduced in full album fixture | Existing `test_process_album_writes_clean_cover_lyrics_and_album_gain` extended with sentinel files | Fixed staging filenames collided with user files. | Use unique hidden staging names. | End-to-end FLAC, artwork, lyrics, loudness test passes with both sentinels intact. | Abrupt termination can still leave hidden staging files. |
| A-005 | P1 | Completeness | A CUE with TRACK rows but no complete FILE mapping could certify album completeness. | Reproduced | `test_cue_without_complete_file_mapping_cannot_prove_album_completeness` | CUE count trusted independently of file references. | Require one named source per track and full local-disc stem coverage. | Single and multi-disc focused tests pass. | Legacy encodings and ambiguous duplicate filenames need further review. |
| A-006 | P1 | Dynamic range | Unrelated DR report rows could attach to local tracks by number or positional order. | Reproduced | `test_unrelated_dr_report_rows_do_not_attach_by_track_number_or_order` | Permissive fallback mapping. | Keep only unique name or title matches. | Focused parser and report tests pass. | Identical generic filenames in an unrelated report remain indistinguishable without stronger evidence. |
| A-007 | P2 | Dynamic range / transaction | Native DR report was written into the album before metadata transaction success; partial measurement could emit an album DR value. | Code path traced; partial failure injected | `test_dr_report_is_staged_and_partial_measurement_has_no_album_value` | Report persisted during acquisition; average used successful subset. | Stage generated report with album transaction; require all measured tracks for album DR. | Focused test passes. | External DR tool behavior remains to verify in live environment. |
| A-008 | P1 | Dynamic range | A partially matched existing report could supply an album DR, and a report from another album with generic matching filenames could be trusted. | Reproduced with generated reports | `test_partial_existing_dr_report_uses_local_measurement`; `test_dr_report_with_matching_filenames_but_wrong_album_is_rejected` | Report count and album header were not validated as a group. | Require complete row mapping and matching `Analyzed:` artist/album when local identity is available; otherwise measure local audio. | Focused tests pass. | Reports without an identity header now fall back to local measurement when artist/album are known. |
| A-009 | P0 | Filesystem/transaction | Replacing a symlinked FLAC path deleted the link while leaving its target untouched. | Code path traced; direct and album tests added | `test_symlinked_flac_is_not_replaced`; `test_album_refuses_symlinked_tracks_before_provider_lookup` | `os.replace` commits to the link path. | Reject symlinked FLAC inputs before provider lookup or writing. | Both focused tests pass. | Symlinked parent directories resolve to their target and need separate policy review. |
| A-010 | P2 | CLI | Invalid numeric options such as zero threads reached late execution failures. | Reproduced by argument parsing | `test_cli_rejects_invalid_numeric_options_before_processing` | Help constraints were not enforced. | Validate threads, MB result count, and image sizes during parsing. | Four focused parameter cases pass. | Additional CLI combinations remain in review queue. |
| A-011 | P2 | Artwork HTTP | A provider `Retry-After: 3600` blocked the album thread for an hour. | Reproduced with a 429 response | `test_cover_fetch_respects_long_retry_after_without_blocking` | Artwork HTTP retry path slept unconditionally. | Respect the provider cooldown by returning without an early retry when it exceeds the short invocation budget. | Focused test passes. | No persisted cooldown scheduler; a later run can retry. |
| A-012 | P2 | Provider cache | A recent unversioned raw cache was accepted as current parser output. | Reproduced | `test_unversioned_raw_cache_is_not_trusted_after_parser_changes` | Legacy cache compatibility bypassed parser version. | Refetch unversioned entries; retain versioned TTL policy. | Focused cache tests pass. | Old raw caches remain on disk but are ignored. |
| A-013 | P2 | Dependency setup | No dependency manifest existed despite mandatory third-party imports. | Repository inventory | Import inventory and tested `.venv` versions | Runtime and test setup were not reproducible from repository files. | Added `requirements.txt` and `requirements-dev.txt`; documented runtime tools. | Installed versions were read from this checkout. | Other Python versions are not validated. |
| A-014 | P3 | Rip artifact discovery | A directory with `accuraterip` in its name was included as a metadata file. | Code inspection | Existing full integration suite | Boolean precedence put the substring arm outside the `is_file()` guard. | Grouped conditions under `is_file()`. | Pending full suite rerun. | No dedicated fault fixture. |
| A-015 | P1 | Album commit | A failed rollback could be followed by deleting its backup tree, and copying originals back could fail under low disk space. | Commit path traced; failure between tracks and rollback failure injected | Extended `test_process_album_writes_clean_cover_lyrics_and_album_gain` with byte readback and recovery-copy assertions | Rollback used copying and unconditionally discarded transaction files. | Restore from same-filesystem backups by rename; retain recovery tree and emit critical diagnostic if rollback itself fails. | Integration fault-injection test passes. | A process crash between renames still requires manual recovery. |
| A-016 | P1 | ReplayGain | Individually valid but inconsistent album gain values were preserved; a loudgain failure was treated like an ordinary skip and album metadata could commit. | Reproduced with two generated FLACs and injected command failure | `test_inconsistent_existing_album_gain_is_recalculated`, `test_inconsistent_album_gain_is_cleared_if_scanner_unavailable`, extended album integration test | Per-file validation omitted cross-track consistency; bool return conflated preserved, unavailable, and failed. | Recalculate inconsistent album gain; clear invalid album values when no scanner; surface outcome status and abort staged album on scan failure. | Focused and album integration tests pass. | If no scanner and no valid track values exist, ReplayGain remains absent with an explicit skip. |
| A-017 | P1 | Metadata writer | Two different incoming scalar values silently used the last one. A picture-only CLI call also re-submitted old provenance IDs as proposals. | Reproduced; full-suite regressions exposed old assumptions | `test_conflicting_incoming_scalar_values_are_rejected_without_write`, revised picture and cardinality tests | Last-value-wins grouping and unnecessary CLI tag replay. | Reject conflicting incoming scalar values; picture/lyrics-only CLI updates use writer's existing-tag preservation. | Focused tests pass; final full suite pending. | Project profile keeps one display `ARTIST`; foreign repeated `ARTIST` is reported by QC. |
| A-018 | P2 | Album discovery | Curated keyword search scheduled nonexistent album paths. | Reproduced with a missing curated folder | `test_curated_keyword_search_does_not_schedule_missing_album` | Curated mapping was expanded without checking path existence. | Require a real directory before scheduling. | Focused test passes. | Other CLI modes remain under review. |
| A-019 | P0 | Artwork filesystem | A symlinked `cover.jpg` could pass through provider lookup and later be replaced. | Reproduced with a symlink fixture | `test_album_refuses_symlinked_cover_before_provider_lookup` | Local cover path was trusted as an ordinary file. | Reject symlinked cover before acquisition. | Focused test passes. | Other symlinked sidecars need review. |
| A-020 | P1 | Track identity | A disc-2 FLAC could take an exact-title track from a release whose only numbered medium was disc 1. | Reproduced | `test_track_match_does_not_borrow_track_from_wrong_numbered_disc` | Content fallback ignored explicit, conflicting medium positions. | Content fallback now considers only media lacking valid positions; return a mismatch otherwise. | Focused test, Ruff, mypy pass. | Ambiguous unnumbered multi-medium results still need review. |
| A-021 | P1 | Album completeness | A single local disc was called complete although the accepted release listed an additional disc. | Reproduced | `test_exact_release_extra_disc_prevents_album_completeness` | External track counts were checked only for locally present discs. | Treat a missing disc from the matched release track list as incomplete. | Focused tests pass. | Exact-release list quality depends on release selection. |
| A-022 | P1 | Layout / track identity | `CD2/01 - Song.flac` with `DISCNUMBER=1` had no conflict, so the directory silently overrode the embedded tag. | Reproduced | `test_position_evidence_reports_directory_and_tag_disc_conflict` | `position_evidence` compared filename and tag, but not directory and tag. | Report the discrepancy to the caller's review guard. | Focused test passes. | Directory naming may itself be wrong; the conflict is left for review. |
| A-023 | P1 | CUE / rip log / release identity | The first EAC log with TOC rows could certify an exact physical Disc ID for unrelated separate FLACs. | Reproduced with two generated tracks and a bogus log | `test_unrelated_eac_log_does_not_certify_exact_disc_id` | Multi-file CUE skipped offset comparison, and log selection stopped at the first parseable log. | Validate track numbers and local audio durations against each log; only a matching log may supply an exact EAC TOC. Otherwise use synthetic local TOC. | Focused test covers rejection and choosing a later valid log. | Pregap-heavy rips beyond two seconds may conservatively lose exact status. |
| A-024 | P1 | TOC discovery | An abandoned `.metadata-transaction-*` pending FLAC could make a valid album appear to span multiple folders and abort TOC discovery. | Reproduced | `test_abandoned_transaction_does_not_enter_toc_discovery` | `build_all` recursively globbed all FLAC and CUE paths without private-path filtering. | Reuse library FLAC traversal and filter private CUE paths. | TOC focused tests pass. | Other direct recursive glob consumers still need inventory. |
| A-025 | P2 | Reprocessing / provenance | A second unchanged album run rewrote every FLAC solely to generate a new `METADATA_PROVENANCE_ID` and ledger row. | Reproduced in full two-track album test | Extended `test_process_album_writes_clean_cover_lyrics_and_album_gain` with byte, tag, picture, cover, sidecar and ledger readback after a second run; existing ID3-cleanup test rerun | Writer minted a provenance UUID before checking whether proposed metadata and pictures changed. | Compare validated staged tags/pictures excluding the new ID; skip replacement and ledger append on a true no-op only when source FLAC container has no ID3 header/trailer. | Focused integration and ID3-cleanup tests pass. | Provider caches may still refresh according to TTL; no-op only covers local persisted album state. |

## Historical review queue (2026-09-26; follow-up results below)

- [ ] CLI, discovery, layout, filesystem, dry run
- [ ] Discogs, MusicBrainz, CUE/log, release and track identity
- [ ] Album completeness, DR, ReplayGain
- [ ] Metadata schema/policy/writer, transaction, provenance, readback
- [ ] Artwork acquisition, scoring, cropping, menu, embed/readback
- [ ] Lyrics providers, parsing, identity, transformations, storage
- [ ] HTTP, cache, concurrency, untrusted input, exception handling
- [ ] Dependency/configuration, test quality, documentation, final verification

## Follow-up audit, 2026-10-04

All named source modules and dependencies are present under intended import
names; there are no `(1)` upload suffixes. No applicable `AGENTS.md`, packaging
manifest or CI workflow was found. Git remains unavailable (empty `.git`).
The source/config/docs baseline was copied to `/tmp/flac-audit-baseline` for
file-level diff review. The real music collection was not processed or modified.
Existing collection artwork was read by historic reproduction tests; all audio
write, failure-injection and end-to-end fixtures are temporary copies/files.

Baseline commands:

- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider`: **459 passed**, 87.61 s.
- `.venv/bin/ruff check .`: passed.
- `.venv/bin/ruff format --check .`: failed on one existing long condition in `metadata_writer.py`; corrected by formatting that condition.
- `.venv/bin/mypy .`: passed, 28 source files.

### New confirmed defects and implemented fixes

| ID | Severity | Trigger / root cause / consequence | Implemented fix and evidence |
| --- | --- | --- | --- |
| A-026 | P0 | Writer ledger failure followed by failed original restoration; `finally` deleted the only recovery copy. | Retain backup and report its path when rollback fails; byte-exact recovery-copy fault test. |
| A-027 | P0 | ReplayGain second-file replacement and rollback failure; unconditional cleanup deleted backups. Outer album cleanup could then delete retained nested copies. | Attempt all restorations, retain failed recovery copies, propagate recovery requirement so the album tree survives; two-file fault test. |
| A-028 | P1 | Writer trusted stored STREAMINFO MD5; zero-MD5 streams could pass decode/signature checks despite changed samples. ReplayGain checked tags only, allowing a scanner to replace audio. | Compare decoded PCM SHA-256 and stream geometry before publication in both paths; zero-MD5 corruption injection and scanner replacement tests. |
| A-029 | P2 | Embedded-label archive was written beside the real FLAC before validation; failed writes left live sidecars. | Queue archives, publish only with successful validated writes, remove new archives after successful rollback; failed-validation tree test. |
| A-030 | P1 | Completeness ignored filename positions, directory/tag disagreements and conflicting total aliases; complete albums could stay UNKNOWN, or conflicting layouts become COMPLETE. Release counts ignored embedded-only positions. | Share `position_evidence`, read all total aliases and tags in layout counts; four regression cases; contiguous CUE positions required. |
| A-031 | P1 | Duration-layout helper consumed an iterable to build counts, then iterated it again; generators bypassed duration rejection. | Materialize media once; generator with tenfold duration conflict is rejected. |
| A-032 | P1 | Equally plausible unnumbered media selected the first result, creating confident track IDs from ordering. | Refuse insufficient medium score separation; duplicated-medium regression. |
| A-033 | P2 | Library traversal ignored `.FLAC` and included directories ending `.flac`. | Case-insensitive suffix plus file check, retaining private-directory exclusions; discovery regression. |
| A-034 | P2 | Config cache keyed `None` before resolving `AUDIOPHILE_CONFIG`; changing the selected policy retained old settings. | Resolve the selected absolute path before caching; public cache invalidation remains available; two-policy regression. |
| A-035 | P1 | Regex-only ReplayGain checks accepted enormous decimal strings whose numeric values overflowed to infinity. | Require finite gain/peak/reference values; overflow regression. |
| A-036 | P1 | Native DR measurements indexed by basename; `CD1/01.flac` and `CD2/01.flac` shared one measurement. | Album-relative measurement/report keys, path-aware report matching and caller lookup; different disc values and album aggregation regression. Ignore nonzero external DR-tool output. |
| A-037 | P1 | Artwork normalization erased Cyrillic/CJK; artist matching treated empty normalized identity as agreement. | NFC/Unicode comparison and refuse empty artist identity; Cyrillic/CJK regression. Stored names remain untouched. |
| A-038 | P1 | Enhanced LRC checked raw word times without `[offset:]`, accepting negative/out-of-duration playback timing. | Apply source offset to validation only; positive/negative offset regressions. |
| A-039 | P2 | Discogs finder had separate unbounded response/retry loops and could sleep for an hour on a 429. | Reuse bounded JSON transport, typed failures, closure, short retry budget and shared per-module pacing; long Retry-After regression. |
| A-040 | P1 | Concurrent album processes could interleave backups, renames and ledgers. | POSIX advisory album-directory lock before providers/staging, no persistent lock file; concurrent-lock regression. |
| A-041 | P2 | Cache envelopes stored raw URL keys including credentials; provider exceptions could repeat URL secrets. | Store key digest, redact common credential fields/bearer tokens in diagnostics; cache and lyrics-diagnostic regression with synthetic credentials. |
| A-042 | P1 | Direct ledger append followed by write/fsync failure could leave a false or truncated row while the FLAC rolled back. | Publish complete ledger through temporary file/rename under writer lock; fsync injection verifies old ledger and FLAC bytes. Refuse symlinked ledgers. |
| A-043 | P1 | Dangling lyric/artwork sidecar links looked absent and could be replaced or followed by a write. | Reserve symlink paths during materialization, refuse sidecar/art archive links at commit; dangling-link regression. |
| A-044 | P1 | AcoustID ID helper accepted the first response ID regardless of score; NaN also bypassed candidate threshold comparisons. | Shared finite/confident result ranking, bounded transport for ID lookup, no ID on ties; low/NaN/missing-score regressions. |
| A-045 | P2 | Automatic/quiet artwork acquisition wrote fixed preview files before album success. | Disable persistent acquisition previews in album orchestration; category previews stay temporary; real-wiring cover fixture asserts this flag. Standalone explicit preview output remains supported. |
| A-046 | P2 | Discovery and tagger used independent host limiters, permitting a burst across module boundaries. | Share MusicBrainz/Discogs limiter instances within the process; identity regression verifies the actual callers. |

The first 13 new regressions were run before fixes: **13 failed**. Five further
DR/artwork/LRC/Discogs cases failed before their fixes. Three AcoustID threshold
cases also failed before correction. New tests live in `test_audit_2026.py`;
existing album integration was extended with full-tree dry-run and PCM readback.
Existing verbose/no-rescan fixtures now mock only the scanner boundary and let
real FLAC validation run. Cache-envelope assertions check the new key digest.
Assertions, provider functionality and type-check configuration were not weakened.

### Reviewed execution and limitations

- Discovery → Discogs physical evidence → CUE/log/TOC, MusicBrainz edition scoring and AcoustID recording evidence → release/medium/track selection → cover and lyrics acquisition → per-track staging/writer → optional DR report and ReplayGain → validation → individual album renames and ledger publication. Existing `Architecture map` identifies modules. Current tests exercise real wiring through successful/no-op/failed/rollback album updates.
- Edition vs recording vs release-track fields remain separate; synthetic TOC cannot establish an exact Disc ID. Mixed/incomplete disc evidence does not force COMPLETE. Existing multi-disc, repeated-title, version-conflict, rip-log and identity fixtures remain part of final suite.
- Artwork unknown provenance remains unknown; CAA/Discogs are not automatically digital. Provider-neutral ranking and EXIF/crop preview/save/embed checks have offline coverage. Heuristic front/crop correctness on arbitrary packaging is unproven. No album-specific production exceptions were introduced.
- Local/provider lyric archives, plain/synced scoring, provider statuses, language/translation/romanization and native TTML remain covered by existing fixtures. No provider was removed or disabled in this audit. Live account-authenticated providers and scraping layouts remain unverified.
- The actual `loudgain --help` was checked: `-r` is track-only, `-a` album plus track, `-k` clipping prevention, `-s e` enhanced gain metadata. Real scanner runs on generated FLACs exercise these arguments. PCM comparisons verify sample preservation.
- Dry-run permits provider-cache refresh and disposable staging/decoding, but the end-to-end temporary album keeps the same file inventory/bytes and runs no loudgain, metaflac or DR tag-writing subprocess. Album acquisition no longer leaves preview files before commit.
- Album locks coordinate this album entry point on POSIX/local filesystems; standalone writer invocations and unrelated applications are not globally excluded. Shared HTTP sessions, cross-process rate pacing, all credential formats, hostile cache races, network-filesystem semantics, and large-library memory/performance have not been exhaustively verified.
- Album publication is **not atomic as a group**. Readers can see mixed files during commit. No kill/power-loss durability simulation was performed; interrupted commits require manual recovery. Directory fsync, an automatic recovery command and a durable commit journal are not implemented. Failed rollback copies are retained rather than discarded.
- No missing production modules or unavailable required runtime tools block offline integration. `dr14_tmeter` availability is not needed for the native path; live external DR-tool behavior is unverified. Only the supplied Python 3.14 environment is validated; other advertised Python versions and fresh dependency installation are untested.
- Primary documentation was checked: [MusicBrainz API](https://musicbrainz.org/doc/MusicBrainz_API), [AcoustID lookup](https://acoustid.org/webservice), [LRCLIB API](https://lrclib.net/docs). MusicBrainz requires application-wide one-request-per-second pacing; discovery/enrichment now share the limiter, while cross-process coordination remains unverified. Musixmatch documentation retrieval failed; offline fixtures do not prove live availability.

### Final verification, after all code changes

| Exact command | Outcome |
| --- | --- |
| `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider` | **486 passed**, 89.13 s; no failures or skips. 27 new cases beyond the 459-test baseline, plus strengthened existing integration fixtures. |
| `.venv/bin/ruff check .` | **Passed**. |
| `.venv/bin/ruff format --check .` | **Passed**, 36 files already formatted. |
| `.venv/bin/mypy .` | **Passed**, 30 source files. No new exclusions/ignores or weakened configuration. |
| `.venv/bin/python -m pip check` | **Passed**, no broken requirements. Pip reported its disabled cache in the restricted environment; dependency consistency still completed. |
| `.venv/bin/python audiophile_tagger.py --help >/tmp/flac-audit-tagger-help.txt` | **Exit 0**. |
| `.venv/bin/python audiophile_cover_fetcher.py --help >/tmp/flac-audit-cover-help.txt` | **Exit 0**. |
| `.venv/bin/python metadata_writer.py --help >/tmp/flac-audit-writer-help.txt` | **Exit 0**. |

Focused runs also passed: the initial 13 new cases; 104 cases covering new
regressions, artwork interaction, transactional writer, real album integration
and logging; and 81 preservation/album cases after subsequent changes. The final
486-case suite supersedes those intermediate checks. Interim full-suite failures
from overbroad subprocess mocks and the old cache-key assertion were corrected
by tightening the external boundaries and checking the actual new contract.

The final Python diff against `/tmp/flac-audit-baseline` was reviewed and saved
to `/tmp/flac-audit-final.diff`. Implementations remain editable in this workspace;
no commit was created because this supplied directory is not a Git repository.
No assertion that all possible bugs or external integration failures are gone
is made. The untested paths above remain limits of this verification.


## Project relocation and initial Git import (2026-10-04)

The project and its local runtime state were moved into
`/home/emvis/Music/auph`, using the already initialized repository there.
The adjacent Queen collection remains in `/home/emvis/Music/Queen`; no music
was moved or modified. Virtual-environment launchers and test input paths
were adjusted for the new project directory. `.gitignore` excludes `.env`,
virtual environments, caches, private agent/session files, and generated
verification artifacts. The prior no-repository statements above describe the
audit baseline, before this relocation.

Repeated verification exposed persistent lyrics caches bypassing fixture
providers in three tests. A shared autouse fixture now gives each test real
`JsonCache` instances in its temporary directory. Cache functionality and
provider assertions remain enabled; no user cache is deleted.

Relocated checks: `.venv/bin/ruff check .` passed;
`.venv/bin/ruff format --check .` passed (35 files);
`.venv/bin/mypy .` passed (31 source files);
`.venv/bin/python -m pip check` passed. Tagger, cover fetcher, and writer
`--help` commands exited 0. The three cache-contaminated provider cases passed
with isolated caches. `git diff --cached --check` flags intentional trailing
spaces in the EAC-log and raw UltraStar HTML fixtures; those source forms were
preserved instead of rewriting fixture content for a whitespace check.

Final relocated suite: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider` — **486 passed in 99.71 seconds**, with no skips.

## Lyrics/artwork selection follow-up — 2026-10-04

[SELECTION_AUDIT.md](SELECTION_AUDIT.md) records S-01 through S-16, the provider
acquisition and failure matrices, and actual verification outcomes. New regressions
are in `test_selection_audit.py`; all added writes target temporary fixtures.
