# Audit implementation backlog — 2026-10-05

Baseline: clean `bd5f7d0`, `/home/emvis/Music/auph`; no applicable AGENTS.md.
Sources: WHOLE_PROJECT_AUDIT.md in full; its linked AUDIT_LEDGER.md and
SELECTION_AUDIT.md tables, failure matrices and limitations; COVER_ART_IMPLEMENTATION.md
and LYRICS_IMPLEMENTATION_CHECKLIST.md; original saved report sections B–K.
The original session export is local/ignored; relevant criteria are retained
here without publishing the conversation. Earlier records remain historical. The independent verification of `663992b`
below supersedes the blanket dispositions where narrower evidence or new
regressions were found.

No real-library enrichment is run. New fixtures are temporary. Historical tests
read adjacent Queen assets without writes. No providers or assertions are removed.
Every numbered finding retains its reference; overlapping A/S/W findings are
consolidated operationally by subsystem and tests. Optional proposals and
verification limits are distinguished from confirmed defects below.

Statuses below apply to the inspected contracts and executed offline fixtures.
They do not convert a fixture pass into live compatibility or universal correctness.

## Numbered findings and acceptance criteria

| Reference | Severity / modules | Acceptance criteria | Status | Tests |
|---|---|---|---|---|
| [A-001](AUDIT_LEDGER.md) | P2 / Release selection | Compare the best row per nonempty edition ID. | already resolved and verified | `TestReleaseSelection` duplicate-row cases |
| [A-002](AUDIT_LEDGER.md) | P2 / Album transaction | Added guarded `stage_album_files` and failure handling in `process_album`. | already resolved and verified | `test_album_staging_copy_failure_cleans_residue_and_preserves_sources` |
| [A-003](AUDIT_LEDGER.md) | P0 / Album discovery | Require actual path identity. | already resolved and verified | `test_curated_release_mapping_requires_the_actual_album_path` |
| [A-004](AUDIT_LEDGER.md) | P0 / Artwork staging | Use unique hidden staging names. | already resolved and verified | Existing `test_process_album_writes_clean_cover_lyrics_and_album_gain` extended with sentinel files |
| [A-005](AUDIT_LEDGER.md) | P1 / Completeness | Require one named source per track and full local-disc stem coverage. | already resolved and verified | `test_cue_without_complete_file_mapping_cannot_prove_album_completeness` |
| [A-006](AUDIT_LEDGER.md) | P1 / Dynamic range | Keep only unique name or title matches. | already resolved and verified | `test_unrelated_dr_report_rows_do_not_attach_by_track_number_or_order` |
| [A-007](AUDIT_LEDGER.md) | P2 / Dynamic range / transaction | Stage generated report with album transaction; require all measured tracks for album DR. | already resolved and verified | `test_dr_report_is_staged_and_partial_measurement_has_no_album_value` |
| [A-008](AUDIT_LEDGER.md) | P1 / Dynamic range | Require complete row mapping and matching `Analyzed:` artist/album when local identity is available; otherwise measure local audio. | already resolved and verified | `test_partial_existing_dr_report_uses_local_measurement`; `test_dr_report_with_matching_filenames_but_wrong_album_is_rejected` |
| [A-009](AUDIT_LEDGER.md) | P0 / Filesystem/transaction | Reject symlinked FLAC inputs before provider lookup or writing. | already resolved and verified | `test_symlinked_flac_is_not_replaced`; `test_album_refuses_symlinked_tracks_before_provider_lookup` |
| [A-010](AUDIT_LEDGER.md) | P2 / CLI | Validate threads, MB result count, and image sizes during parsing. | already resolved and verified | `test_cli_rejects_invalid_numeric_options_before_processing` |
| [A-011](AUDIT_LEDGER.md) | P2 / Artwork HTTP | Respect the provider cooldown by returning without an early retry when it exceeds the short invocation budget. | already resolved and verified | `test_cover_fetch_respects_long_retry_after_without_blocking` |
| [A-012](AUDIT_LEDGER.md) | P2 / Provider cache | Refetch unversioned entries; retain versioned TTL policy. | already resolved and verified | `test_unversioned_raw_cache_is_not_trusted_after_parser_changes` |
| [A-013](AUDIT_LEDGER.md) | P2 / Dependency setup | Added `requirements.txt` and `requirements-dev.txt`; documented runtime tools. | already resolved and verified | Import inventory and tested `.venv` versions |
| [A-014](AUDIT_LEDGER.md) | P3 / Rip artifact discovery | Grouped conditions under `is_file()`. | already resolved and verified | Existing full integration suite |
| [A-015](AUDIT_LEDGER.md) | P1 / Album commit | Restore from same-filesystem backups by rename; retain recovery tree and emit critical diagnostic if rollback itself fails. | already resolved and verified | Extended `test_process_album_writes_clean_cover_lyrics_and_album_gain` with byte readback and recovery-copy assertions |
| [A-016](AUDIT_LEDGER.md) | P1 / ReplayGain | Recalculate inconsistent album gain; clear invalid album values when no scanner; surface outcome status and abort staged album on scan failure. | already resolved and verified | `test_inconsistent_existing_album_gain_is_recalculated`, `test_inconsistent_album_gain_is_cleared_if_scanner_unavailable`, extended album integration test |
| [A-017](AUDIT_LEDGER.md) | P1 / Metadata writer | Reject conflicting incoming scalar values; picture/lyrics-only CLI updates use writer's existing-tag preservation. | already resolved and verified | `test_conflicting_incoming_scalar_values_are_rejected_without_write`, revised picture and cardinality tests |
| [A-018](AUDIT_LEDGER.md) | P2 / Album discovery | Require a real directory before scheduling. | already resolved and verified | `test_curated_keyword_search_does_not_schedule_missing_album` |
| [A-019](AUDIT_LEDGER.md) | P0 / Artwork filesystem | Reject symlinked cover before acquisition. | already resolved and verified | `test_album_refuses_symlinked_cover_before_provider_lookup` |
| [A-020](AUDIT_LEDGER.md) | P1 / Track identity | Content fallback now considers only media lacking valid positions; return a mismatch otherwise. | already resolved and verified | `test_track_match_does_not_borrow_track_from_wrong_numbered_disc` |
| [A-021](AUDIT_LEDGER.md) | P1 / Album completeness | Treat a missing disc from the matched release track list as incomplete. | already resolved and verified | `test_exact_release_extra_disc_prevents_album_completeness` |
| [A-022](AUDIT_LEDGER.md) | P1 / Layout / track identity | Report the discrepancy to the caller's review guard. | already resolved and verified | `test_position_evidence_reports_directory_and_tag_disc_conflict` |
| [A-023](AUDIT_LEDGER.md) | P1 / CUE / rip log / release identity | Validate track numbers and local audio durations against each log; only a matching log may supply an exact EAC TOC. Otherwise use synthetic local TOC. | already resolved and verified | `test_unrelated_eac_log_does_not_certify_exact_disc_id` |
| [A-024](AUDIT_LEDGER.md) | P1 / TOC discovery | Reuse library FLAC traversal and filter private CUE paths. | already resolved and verified | `test_abandoned_transaction_does_not_enter_toc_discovery` |
| [A-025](AUDIT_LEDGER.md) | P2 / Reprocessing / provenance | Compare validated staged tags/pictures excluding the new ID; skip replacement and ledger append on a true no-op only when source FLAC container has no ID3 header/trailer. | already resolved and verified | Extended `test_process_album_writes_clean_cover_lyrics_and_album_gain` with byte, tag, picture, cover, sidecar and ledger readback after a second run; existing ID3-cleanup test rerun |
| [A-026](AUDIT_LEDGER.md) | P0 / see referenced affected path | Retain backup and report its path when rollback fails; byte-exact recovery-copy fault test. | already resolved and verified | test_audit_2026.py; test_regression_repair.py; test_pipeline_safety.py; test_audiophile_pipeline.py |
| [A-027](AUDIT_LEDGER.md) | P0 / see referenced affected path | Attempt all restorations, retain failed recovery copies, propagate recovery requirement so the album tree survives; two-file fault test. | already resolved and verified | test_audit_2026.py; test_regression_repair.py; test_pipeline_safety.py; test_audiophile_pipeline.py |
| [A-028](AUDIT_LEDGER.md) | P1 / see referenced affected path | Compare decoded PCM SHA-256 and stream geometry before publication in both paths; zero-MD5 corruption injection and scanner replacement tests. | already resolved and verified | test_audit_2026.py; test_regression_repair.py; test_pipeline_safety.py; test_audiophile_pipeline.py |
| [A-029](AUDIT_LEDGER.md) | P2 / see referenced affected path | Queue archives, publish only with successful validated writes, remove new archives after successful rollback; failed-validation tree test. | already resolved and verified | test_audit_2026.py; test_regression_repair.py; test_pipeline_safety.py; test_audiophile_pipeline.py |
| [A-030](AUDIT_LEDGER.md) | P1 / see referenced affected path | Share `position_evidence`, read all total aliases and tags in layout counts; four regression cases; contiguous CUE positions required. | already resolved and verified | test_audit_2026.py; test_regression_repair.py; test_pipeline_safety.py; test_audiophile_pipeline.py |
| [A-031](AUDIT_LEDGER.md) | P1 / see referenced affected path | Materialize media once; generator with tenfold duration conflict is rejected. | already resolved and verified | test_audit_2026.py; test_regression_repair.py; test_pipeline_safety.py; test_audiophile_pipeline.py |
| [A-032](AUDIT_LEDGER.md) | P1 / see referenced affected path | Refuse insufficient medium score separation; duplicated-medium regression. | already resolved and verified | test_audit_2026.py; test_regression_repair.py; test_pipeline_safety.py; test_audiophile_pipeline.py |
| [A-033](AUDIT_LEDGER.md) | P2 / see referenced affected path | Case-insensitive suffix plus file check, retaining private-directory exclusions; discovery regression. | already resolved and verified | test_audit_2026.py; test_regression_repair.py; test_pipeline_safety.py; test_audiophile_pipeline.py |
| [A-034](AUDIT_LEDGER.md) | P2 / see referenced affected path | Resolve the selected absolute path before caching; public cache invalidation remains available; two-policy regression. | already resolved and verified | test_audit_2026.py; test_regression_repair.py; test_pipeline_safety.py; test_audiophile_pipeline.py |
| [A-035](AUDIT_LEDGER.md) | P1 / see referenced affected path | Require finite gain/peak/reference values; overflow regression. | already resolved and verified | test_audit_2026.py; test_regression_repair.py; test_pipeline_safety.py; test_audiophile_pipeline.py |
| [A-036](AUDIT_LEDGER.md) | P1 / see referenced affected path | Album-relative measurement/report keys, path-aware report matching and caller lookup; different disc values and album aggregation regression. Ignore nonzero external DR-tool output. | already resolved and verified | test_audit_2026.py; test_regression_repair.py; test_pipeline_safety.py; test_audiophile_pipeline.py |
| [A-037](AUDIT_LEDGER.md) | P1 / see referenced affected path | NFC/Unicode comparison and refuse empty artist identity; Cyrillic/CJK regression. Stored names remain untouched. | already resolved and verified | test_audit_2026.py; test_regression_repair.py; test_pipeline_safety.py; test_audiophile_pipeline.py |
| [A-038](AUDIT_LEDGER.md) | P1 / see referenced affected path | Apply source offset to validation only; positive/negative offset regressions. | already resolved and verified | test_audit_2026.py; test_regression_repair.py; test_pipeline_safety.py; test_audiophile_pipeline.py |
| [A-039](AUDIT_LEDGER.md) | P2 / see referenced affected path | Reuse bounded JSON transport, typed failures, closure, short retry budget and shared per-module pacing; long Retry-After regression. | already resolved and verified | test_audit_2026.py; test_regression_repair.py; test_pipeline_safety.py; test_audiophile_pipeline.py |
| [A-040](AUDIT_LEDGER.md) | P1 / see referenced affected path | POSIX advisory album-directory lock before providers/staging, no persistent lock file; concurrent-lock regression. | already resolved and verified | test_audit_2026.py; test_regression_repair.py; test_pipeline_safety.py; test_audiophile_pipeline.py |
| [A-041](AUDIT_LEDGER.md) | P2 / see referenced affected path | Store key digest, redact common credential fields/bearer tokens in diagnostics; cache and lyrics-diagnostic regression with synthetic credentials. | already resolved and verified | test_audit_2026.py; test_regression_repair.py; test_pipeline_safety.py; test_audiophile_pipeline.py |
| [A-042](AUDIT_LEDGER.md) | P1 / see referenced affected path | Publish complete ledger through temporary file/rename under writer lock; fsync injection verifies old ledger and FLAC bytes. Refuse symlinked ledgers. | already resolved and verified | test_audit_2026.py; test_regression_repair.py; test_pipeline_safety.py; test_audiophile_pipeline.py |
| [A-043](AUDIT_LEDGER.md) | P1 / see referenced affected path | Reserve symlink paths during materialization, refuse sidecar/art archive links at commit; dangling-link regression. | already resolved and verified | test_audit_2026.py; test_regression_repair.py; test_pipeline_safety.py; test_audiophile_pipeline.py |
| [A-044](AUDIT_LEDGER.md) | P1 / see referenced affected path | Shared finite/confident result ranking, bounded transport for ID lookup, no ID on ties; low/NaN/missing-score regressions. | already resolved and verified | test_audit_2026.py; test_regression_repair.py; test_pipeline_safety.py; test_audiophile_pipeline.py |
| [A-045](AUDIT_LEDGER.md) | P2 / see referenced affected path | Disable persistent acquisition previews in album orchestration; category previews stay temporary; real-wiring cover fixture asserts this flag. Standalone explicit preview output remains supported. | already resolved and verified | test_audit_2026.py; test_regression_repair.py; test_pipeline_safety.py; test_audiophile_pipeline.py |
| [A-046](AUDIT_LEDGER.md) | P2 / see referenced affected path | Share MusicBrainz/Discogs limiter instances within the process; identity regression verifies the actual callers. | already resolved and verified | test_audit_2026.py; test_regression_repair.py; test_pipeline_safety.py; test_audiophile_pipeline.py |
| [S-01](SELECTION_AUDIT.md) | High / lyrics_utils, lyrics_pipeline, lyrics_quality | Repair wrapper contract; pass stable instrumental evidence to LRCLIB instead. | already resolved and verified | test_selection_audit.py; test_restored_lyrics_providers.py |
| [S-02](SELECTION_AUDIT.md) | High / lyrics_utils, lyrics_pipeline, lyrics_quality | Collect bounded identity-compatible alternatives; isolate detail failures; retain successful items and partial-failure diagnostics. Applies also to Tekstowo, VK Music, PetitLyrics, Megalobiz, RentAnAdviser, Baidu/1ting, AmDm, MetalArchives, DarkLyrics, Bandcamp and Chorus. | already resolved and verified | test_selection_audit.py; test_restored_lyrics_providers.py |
| [S-03](SELECTION_AUDIT.md) | High / lyrics_utils, lyrics_pipeline, lyrics_quality | Preserve independent plain text, reject materially different/short sync for playback, retain provider source. | already resolved and verified | test_selection_audit.py; test_restored_lyrics_providers.py |
| [S-04](SELECTION_AUDIT.md) | High / lyrics_utils, lyrics_pipeline, lyrics_quality | Let recording/version/duration evidence gate identity; isolate track failures and parse plain/line/word forms independently. | already resolved and verified | test_selection_audit.py; test_restored_lyrics_providers.py |
| [S-05](SELECTION_AUDIT.md) | High / lyrics_utils, lyrics_pipeline, lyrics_quality | Retry ranking after removing below-minimum primary candidates. Separate identity-validated auxiliary translations/romanization retain their existing attachment policy. | already resolved and verified | test_selection_audit.py; test_restored_lyrics_providers.py |
| [S-06](SELECTION_AUDIT.md) | High / lyrics_utils, lyrics_pipeline, lyrics_quality | Isolate each materialization attempt, remove failed outputs, try the next eligible result. Publication filesystem failures still propagate to transaction recovery. | already resolved and verified | test_selection_audit.py; test_restored_lyrics_providers.py |
| [S-07](SELECTION_AUDIT.md) | High / lyrics_utils, lyrics_pipeline, lyrics_quality | Compare with curated local text before adding timing; retain source evidence but reject disagreeing playback sync. | already resolved and verified | test_selection_audit.py; test_restored_lyrics_providers.py |
| [S-08](SELECTION_AUDIT.md) | Medium / lyrics_utils, lyrics_pipeline, lyrics_quality | Require vocal paragraph text; preserve usable local/embedded text, but only validated complete timing stops remote lookup. | already resolved and verified | test_selection_audit.py; test_restored_lyrics_providers.py |
| [S-09](SELECTION_AUDIT.md) | Medium / lyrics_utils, lyrics_pipeline, lyrics_quality | Prefer actual detail identity; retain stable IDs, LRCLIB response JSON/lyricsfile, original chart text or base64 MIDI source. | already resolved and verified | test_selection_audit.py; test_restored_lyrics_providers.py |
| [S-10](SELECTION_AUDIT.md) | High / audiophile_cover_fetcher, audiophile_tagger | Decode each representation before accepting it; continue to previews, record actual evaluated URL/kind, remove corrupt cache entries when possible. | already resolved and verified | test_selection_audit.py; test_restored_lyrics_providers.py |
| [S-11](SELECTION_AUDIT.md) | Medium / audiophile_cover_fetcher, audiophile_tagger | Prefer supplied larger previews; merge representations only within compatible release identities. Ranking still uses decoded measurements. | already resolved and verified | test_selection_audit.py; test_restored_lyrics_providers.py |
| [S-12](SELECTION_AUDIT.md) | Medium / audiophile_cover_fetcher, audiophile_tagger | Use provider-supplied URLs unchanged, including signatures; remove misleading Deezer/Qobuz high-resolution labels. | already resolved and verified | test_selection_audit.py; test_restored_lyrics_providers.py |
| [S-13](SELECTION_AUDIT.md) | High / audiophile_cover_fetcher, audiophile_tagger | Inspect all bounded matching groups, enforce returned artist evidence, retain actual group IDs. Exact release remains stronger evidence. | already resolved and verified | test_selection_audit.py; test_restored_lyrics_providers.py |
| [S-14](SELECTION_AUDIT.md) | High / audiophile_cover_fetcher, audiophile_tagger | Prepare privately, retry eligible candidates, revalidate deferred tiers, preserve existing output until success. Manual tagger failures return to selection rather than silently substituting another image. | already resolved and verified | test_selection_audit.py; test_restored_lyrics_providers.py |
| [S-15](SELECTION_AUDIT.md) | Medium / lyrics_utils, lyrics_pipeline | Separate bounded source limit (5 million characters) from lyric-form limit (1 million). | already resolved and verified | test_selection_audit.py; test_restored_lyrics_providers.py |
| [S-16](SELECTION_AUDIT.md) | High / lyrics_utils, lyrics_pipeline | Retain actual chart identity, reject conflicts before marking it validated, continue to later packages; synthetic SNG integration verifies source and timing retention. | already resolved and verified | test_selection_audit.py; test_restored_lyrics_providers.py |
| [W01](WHOLE_PROJECT_AUDIT.md) | Critical / audiophile_tagger | Catch interruption, track attempted publication, restore published tracks only and re-raise interruption after recovery; before/after second-track injection restores exact original bytes. | already resolved and verified | test_entire_audit.py |
| [W02](WHOLE_PROJECT_AUDIT.md) | Critical / metadata_writer | Record replacement intent before publication; real writer interruption restores original bytes. | already resolved and verified | test_entire_audit.py |
| [W03](WHOLE_PROJECT_AUDIT.md) | High / metadata_writer, audiophile_tagger | Shared locked ledger publication retains/restores prior ledger state; old/absent ledger writer and album interruption tests, existing fsync injection. Recovery failure retains the prior ledger copy. | already resolved and verified | test_entire_audit.py |
| [W04](WHOLE_PROJECT_AUDIT.md) | Critical / audiophile_tagger, dr14_parse | Exclusive same-filesystem hard-link publication with catchable-interruption tracking; collision rejects publication and rolls back audio. Verify archive bytes before reusing a name. Real album late-report collision preserves the concurrent user file. | already resolved and verified | test_entire_audit.py |
| [W05](WHOLE_PROJECT_AUDIT.md) | High / mb_identity_score, discogs_release_find | Require authentic nonpartial TOC for exact evidence, stop remote-to-local mutation, keep bridge bonuses only for retrieval order. Synthetic edition and catalog-evidence regressions. | already resolved and verified | test_entire_audit.py |
| [W06](WHOLE_PROJECT_AUDIT.md) | High / discogs_release_find | Every distinct edition needs the configured margin; deterministic ID ties require review. Real physical scoring fixture. | already resolved and verified | test_entire_audit.py |
| [W07](WHOLE_PROJECT_AUDIT.md) | High / discogs_release_find, mb_identity_score | Shared finite/confident AcoustID ranking, bounded HTTP/cache transport, distinct release lookup limit 20 with visible truncation. Later-strong-result fixture; prior bridge fixture updated to realistic confidence/body contract without weakening assertions. | already resolved and verified | test_entire_audit.py |
| [W08](WHOLE_PROJECT_AUDIT.md) | High / mb_track_match, release_match | Use finite recording milliseconds when release-track length is absent; reject malformed candidates/scores. Duration and score regressions. | already resolved and verified | test_entire_audit.py |
| [W09](WHOLE_PROJECT_AUDIT.md) | High / mb_track_match | Use explicit position evidence, reject insufficient track margin and duplicate medium positions. Repeated-title/duration fixture. | already resolved and verified | test_entire_audit.py |
| [W10](WHOLE_PROJECT_AUDIT.md) | High / mb_track_match | Prefer actual release-track credits/join phrases; use shared explicit/delimited version signatures. Credit, clean/explicit and ordinary-title regressions. Comparison NFC is separate from stored credits. | already resolved and verified | test_entire_audit.py |
| [W11](WHOLE_PROJECT_AUDIT.md) | High / release_match, album_completeness, audiophile_tagger | One `release_disc_counts` contract derives absent counts from fetched tracks, refuses unresolved/duplicate media, and is used in both layout validation and actual tagger completeness handoff. Real completeness fixture. | already resolved and verified | test_entire_audit.py |
| [W12](WHOLE_PROJECT_AUDIT.md) | High / mb_discid_from_cue | Reject incomplete/duplicated/out-of-order CUE numbering; order by corroborated positions; group actual files per medium; synthetic evidence remains nonexact. Malformed CUE and per-disc/order regressions. | already resolved and verified | test_entire_audit.py |
| [W13](WHOLE_PROJECT_AUDIT.md) | Medium / discogs_release_find | Case-insensitive file checks, consistent album-parent grouping, private/symlink exclusion before resolution. Discovery fixtures. | already resolved and verified | test_entire_audit.py |
| [W14](WHOLE_PROJECT_AUDIT.md) | High / audiophile_tagger, loudness | Clear album gain/peak/range for incomplete/unknown sets, including metadata-only cleanup when scanner/track measurements are absent. Distinct `album-tags-cleared` outcome; PCM preserved; no invented gain. | already resolved and verified | test_entire_audit.py |
| [W15](WHOLE_PROJECT_AUDIT.md) | High / dr14_parse | Measure real peak and sine-reference whole-track/channel RMS; use actual blocks; finite silence handling. Analytic short/mono/silence tests. Compatibility with proprietary meters is unverified. | already resolved and verified | test_entire_audit.py |
| [W16](WHOLE_PROJECT_AUDIT.md) | High / dr14_parse, audiophile_tagger | Withhold partial album DR; require readable stream geometry and supported depth; decode 8-bit correctly. Invalid-file and real 8-bit FLAC regressions; bounded decoder subprocesses. | already resolved and verified | test_entire_audit.py |
| [W17](WHOLE_PROJECT_AUDIT.md) | High / dr14_parse | Unicode/NFC comparison retains actual identity. Non-Latin regression; report mapping still requires unique names/titles. | already resolved and verified | test_entire_audit.py |
| [W18](WHOLE_PROJECT_AUDIT.md) | High / dr14_parse | Refuse symlink report, create report exclusively and preserve existing files, return nonzero on failed/partial measurement. Actual CLI/link fixtures. APE/WV direct decoding remains unsupported and now reports failure. | already resolved and verified | test_entire_audit.py |
| [W19](WHOLE_PROJECT_AUDIT.md) | Medium / audiophile_tagger | No inferred tempo without an energy signal; bounded shared HTTP acquisition and independent validated model probabilities. Silence and malformed-analysis/valid-BPM fixtures. | already resolved and verified | test_entire_audit.py |
| [W20](WHOLE_PROJECT_AUDIT.md) | High / provider_cache | Error envelopes are unsuccessful, not cached, and old error entries invalidated; digest explicit credentials/language into keys; one guarded size/mtime snapshot. Error/cross-context/eviction fixtures. | already resolved and verified | test_entire_audit.py |
| [W21](WHOLE_PROJECT_AUDIT.md) | High / pipeline_config | Strict known fields/types/finite ranges, explicit missing-file error, mtime/size invalidation and early CLI validation. Preservation-off options unsupported by implementation fail explicitly. | already resolved and verified | test_entire_audit.py |
| [W22](WHOLE_PROJECT_AUDIT.md) | Medium / metadata_schema, metadata_writer, verify_all_flacs | Empty missing values, reject unsupported/nonfinite values and invalid incoming dates; case-insensitive inputs and matching write-validation contract. Writer/PCM/CLI fixtures; QC validates calendar components. | already resolved and verified | test_entire_audit.py |
| [W23](WHOLE_PROJECT_AUDIT.md) | Medium / check_tokens, provider_cache, lyrics_utils, audiophile_cover_fetcher | Environment precedence, parsed diagnostic statuses, bounded diagnostic body reads and redaction including OAuth/Basic; configuration/auth payload and cover error tests. | already resolved and verified | test_entire_audit.py |

## Additional recommendations and limitations

| Reference | Severity / modules | Acceptance criteria and disposition | Status | Evidence |
|---|---|---|---|---|
| R01; W01–W04; audit H/I-P0; transaction appendices | Critical / album_transaction, tagger, writer | Journal destinations, exact pre/post hashes and recoverable snapshots before publication; sync files/directories; explicit locked recovery preflights every entry; refuse unrelated edits, symlinks and damaged backups. Block new enrichment until recovery. Durable completed journals only clean up. | fixed and verified | test_backlog: process_death, standalone_writer_process_death, recovery_preflights, snapshots_on_recovery_failure, committed_marker, durable_writer_cleanup  Independent follow-up V01/V02/V08/V09 reopened incomplete publication, cleanup, disc recovery and ledger locking; regressions now verified. |
| R02; W20; cache/session limitations | High / provider_cache | Key actual prepared Requests credentials/cookies/language; verify stored request digest/status/payload, bound cache reads even after size races; do not persist responses with unkeyed Vary. Parser v11 invalidates old entries, including unsafe v10 negative Vary entries. | fixed and verified | test_backlog: session_credentials, corrupt_cache, unkeyed_server_vary  Independent V04/V06: negative Vary and contextual schema eviction corrected; v10 entries invalidated. |
| R03; W23; failure matrix | Medium / provider_cache | Explicit invalid-key/rate-limit/service-unavailable error messages produce actionable failure statuses; LRCLIB checks transport success and retains authentication severity rather than relabeling HTTP-200 error envelopes as parser errors; unknown provider codes stay invalid response. None become durable NOT_FOUND. | fixed and verified | test_backlog: api_error_envelopes and lrclib_preserves_transport_error_classification (failed before fixes) |
| R04; A-040/A-042; concurrency appendices | High / process_lock, metadata_writer | Cooperating standalone writers lock before snapshot through commit; directory locks coordinate normal album/CD layouts with tagger; ledger appends serialize across processes. Private lock files remain outside collection. | fixed and verified | test_backlog: concurrent_process_ledger, standalone_writers_serialize  Independent V03/V09: fork ownership and snapshot-through-commit ledger locking corrected. |
| R05; A-046; cache/concurrency appendices | Medium / provider_cache, http_sessions, lyrics_utils | Named MusicBrainz/Discogs pacing shares private runtime state across processes; default track workers isolate cookies/headers while preserving transports; LRCLIB has per-thread sessions. Custom auth/transport thread safety is not certified. | fixed and verified | test_backlog: named_rate_limiter, worker_session, lrclib_workers |
| R06; selection provider matrix / LRCLIB lyricsfile | High / lyricsfile, lyrics_utils, lyrics_pipeline | Bounded draft-1.0 YAML parser preserves source and supplied identity/plain/line/word forms. Reject aliases, duplicate keys, unsafe tags/unknown versions. Bad sync retains plain; unknown offsets or missing ends never invent intervals. Valid real intervals may produce TTML. | fixed and verified | test_backlog: lrclib_lyricsfile_only_survives, lyricsfile_keeps_plain, lyricsfile_rejects; upstream draft checked 2026-10-05; native instrumental follow-up V10/V11  Independent V05/V07/V10/V11: mixed search results, malformed line structure, instrumental acquisition and committed source retention corrected. |
| R07; W15/W16; performance appendix | Medium / dr14_parse, tagger | Avoid full-track NumPy/byte buffers for DR/peak/BPM; validate decoded size and depth, keep equivalent values including over peak semantics, close temp resources. Measure rather than presume improvement. | fixed and verified | test_backlog: block_audio_analysis (8/16/24/32-bit); existing 120 BPM and DR cases; 30s/192kHz/stereo RSS benchmark |
| R08; original H/I-P2.3 / dry-run report | Medium / metadata_writer, tagger | Return and print actual old/proposed values, source/confidence, pictures and unapplied review proposals; no-op must not suppress dry-run conflicts. No collection writes. | fixed and verified | test_backlog: dry_run_reports_exact, dry_run_conflict (failed before correction); existing real album tree dry-run test |
| R09; original G/I-P2.6 / analytical provenance | Medium / tagger, dr14_parse | Version native DR algorithm; retain BPM v1 provenance; identify actual scanner executable by digest, command flags, true peak and album membership. Cleanup/preservation must not claim an RG2 scan or clipping profile. | fixed and verified | DR_ALGORITHM_VERSION, run_loudgain outcome, loudness_scan ledger; existing real-scanner/album fixtures |
| R10; setup/CLI appendices | Medium / README, requirements | Document dependency-supported minimum Python, add YAML parser/type dependencies; keep provider/functionality checks. | fixed and verified | Installed numpy Requires-Python >=3.12; local 3.14 checks; PyYAML 6.0.3 and types pin |
| R11; live/provider matrices, pagination, private routes | Verification limit / all providers | Do not certify live credentialed endpoints or invent undocumented paging/ID transformations. Bounded existing candidates and fallback tests remain. Live checks need accounts/authorized representative responses; no access bypass. | blocked | No authenticated live run or representative new private-schema fixtures; original provider matrix retained |
| R12; platform/filesystem appendix | Verification limit / transactions, locks | POSIX/local filesystem guarantees only. Windows/cross-filesystem/network filesystem recovery and physical power-loss durability require those environments; directory fsync is now implemented, but process-kill tests are not power-loss tests. | blocked | Current environment is POSIX/local; hard links/fcntl intentional supported contract |
| R13; original I-P3.1/.2/.5/.6 | Optional / classical views, OCR, alternate-master reports, player profiles | Reject speculative automatic OCR/mastering assertions and new player-dependent display rewrites. No target-player mapping, annotated OCR dataset, or alternate-master acceptance thresholds supplied. Preserve neutral typed works/credits and existing storage policy; future opt-in tools need separate supported criteria. | rejected with evidence | Existing policy/checklist chooses display ARTIST, LRC tags+sidecars, original artwork and no fabricated provenance; these proposals are optional new features, not confirmed defects |
| R14; original I-P2.5 / Fish wrapper | Low / entrypoints | Keep one Python implementation. Do not recreate a Fish wrapper: historical user explicitly authorized deletion of the legacy script. | already resolved and verified | No audiophile.fish or production references; CLI smoke checks |
| R15; original I-P1.1 / immutable evidence graph | Maintainability / matchers | Retain separate entity/scoped contracts and corroborated layout models; remote discovery cannot mutate local catalog evidence. A second parallel graph rewrite provides no demonstrated correctness benefit after W05/W11 and is rejected; migrate proven contract defects only. | rejected with evidence | LocalSignature, ReleaseMatchDecision, scope_agreement, shared release_disc_counts, existing identity tests |
| R16; original E suggested 100-point sum/consensus cutoffs | Selection policy / lyrics_quality | Keep independently validated identity/version/text/timing dimensions and preservation gates. Reject literal illustrative additive formula (terms exceed 100), uncalibrated score guarantees and majority rejection of a correct unique transcript. | rejected with evidence | Actual correct-plain/wrong-sync, duplicate-source, version/ID/duration tests; current policy minimum 75; no benchmark validates proposed numerical calibration |
| R17; original J / CI and broad property coverage | Verification recommendation / tests | Full local suite and configured checks must pass. CI execution/fresh-host claims need a runner and non-shipped historical Queen assets; do not manufacture green results with skipped failures or publish copyrighted assets. Local suite restructuring is an unimplemented optional recommendation, separately recorded as R22; it is not blocked by credentials. Every arbitrary future Unicode/schema/provider response remains uncertified. | blocked | Local checks feasible; no remote CI run requested/observed; historical tests consume adjacent non-Git collection assets |
| R18; miscellaneous uncertainties in A/S/W appendices | Verification limit / discovery, matching, heuristics | Keep conservative conflict/refusal behavior. Arbitrary mixed releases, legacy encodings, all genres/languages, packaging/crop/upscale cases, custom auth callbacks, hostile noncooperating editors and every config combination need representative evidence, not speculative relaxed gates. | blocked | Existing focused fixtures cover named defects, not universal correctness. Recovery preflight refuses unrelated edits; remaining external edit check/replace race explicit |
| R19; proposed cache ETags/jitter and durable cooldown scheduler | Optional / provider_cache | Retain finite TTL/parser/status identity and bounded Retry-After/backoff. A new conditional-request scheduler lacks provider revision contracts and workload evidence; retries never shorten a provider instruction. Cross-process minimum spacing is implemented; persistent Retry-After scheduling remains a future feature. | rejected with evidence | Existing long-Retry-After and retry tests; no provider-independent ETag contract or desired daemon scheduler supplied |

| R20; R02 / prepared query identity | High / provider_cache | Session-level query parameters change the actual requested edition/version/language. Include the prepared URL in the cache key so the previous query cannot supply another query’s response. | fixed and verified | test_session_query_parameters_are_part_of_actual_cache_identity: failed before fix; passes after |
| R21; R01/R04 / multi-disc recovery gate | High / metadata_writer, process_lock | A standalone CD/Disc-subfolder writer must refuse an unfinished journal at the album root before changing metadata. Share the same root contract with its directory lock; check both root and immediate parent journals. | fixed and verified | test_disc_writer_refuses_unfinished_album_root_transaction: failed before fix; passes after  Independent V08: reciprocal album discovery of child journals, root recovery and canonical root locking corrected. |

## Cover reconciliation (C references)

| Reference | Acceptance criteria (cover_fetcher/tagger/writer) | Status | Evidence |
|---|---|---|---|
| C01: CAA information loss | Original and thumbnail URLs, types, flags, comments, approval, IDs and release evidence retained in `query_cover_art_archive`. | already resolved and verified | cover/interactive/selection/regression suites |
| C02: CAA unknown dimensions | Actual original decoded before ranking; declared and measured dimensions remain separate. | already resolved and verified | cover/interactive/selection/regression suites |
| C03: Raw/package front rejection | Originals retained; front ROI proposed only with a strong packaging seam; ambiguous scans stay uncropped. | already resolved and verified | cover/interactive/selection/regression suites |
| C04: Discogs secondary assumed front | Secondary role unknown; primary only likely front; hashes may corroborate a secondary. | already resolved and verified | cover/interactive/selection/regression suites |
| C05: Discogs representation ambiguity | CDN transforms explicitly labelled generated previews; URI150 stored separately; available URI probed. No unsupported signed-URL rewriting. | already resolved and verified | cover/interactive/selection/regression suites |
| C06: Metadata richness bias | API dimensions do not substitute for measurements. | already resolved and verified | cover/interactive/selection/regression suites |
| C07: Provider/scoring bias | Visual scoring independent of provider; release correctness enforced separately. | already resolved and verified | cover/interactive/selection/regression suites |
| C08: Amazon 160-pixel variant | Amazon CDN URLs are treated as unverified representations, never labelled high resolution. Their measured dimensions incur a resolution penalty; a 160-pixel variant cannot become the best digital option beside a usable same-release CAA scan. `cover_mode` only breaks near-quality ties. | already resolved and verified | cover/interactive/selection/regression suites |
| C09: Weak upscale evidence | Native-frequency softness below the fake-upscale threshold no longer reduces effective resolution or authenticity. The soft penalty starts at confidence 0.60; strong synthetic upscales remain penalized. This prevents a 3000-pixel source from being punished merely because a 1200-pixel derivative falls below the detector's size gate. | already resolved and verified | cover/interactive/selection/regression suites |
| C10: Wrong-edition fallback | Release tiers, title and supplied barcode/catalog/country/media conflicts gate selection; a unanimous local MBID is used for cover discovery only after MusicBrainz title, barcode and Discogs relation verification. | already resolved and verified | cover/interactive/selection/regression suites |
| C11: Unbounded resolution reward | Bounded detail score with soft upscale discount. | already resolved and verified | cover/interactive/selection/regression suites |
| C12: Duplicate evidence | SHA-256, perceptual and difference hashes, including square derivatives; grouping and role promotion require compatible release identities. Copies do not vote. | already resolved and verified | cover/interactive/selection/regression suites |
| C13: Nondeterministic comparison | Stable selection keys and deterministic candidate ordering. | already resolved and verified | cover/interactive/selection/regression suites |
| C14: EXIF/MIME/parsing | Decode validation, oriented dimensions, actual MIME, JPEG/PNG/WebP support and byte/pixel limits. | already resolved and verified | cover/interactive/selection/regression suites |
| C15: ICC/CMYK/alpha | Profile-aware analysis and compatibility conversion; originals retained. | already resolved and verified | cover/interactive/selection/regression suites |
| C16: Repeated quality analysis | Versioned SHA-256 analysis cache independent of provider metadata. | already resolved and verified | cover/interactive/selection/regression suites |
| C17: Transient failure caching | Failure categories retained; unsuccessful HTTP results not persisted; success clears stale errors. | already resolved and verified | cover/interactive/selection/regression suites |
| C18: Expensive discovery | Metadata filtering, tier-by-tier original probing, bounded candidate pools and analysis resolution, HTTP stream byte cap and thread-local sessions. Manual mode removes candidate caps. | already resolved and verified | cover/interactive/selection/regression suites |
| C19: Source generation loss | Byte-exact `cover.original.<format>` source staged with album commit; embedding reads source directly. | already resolved and verified | cover/interactive/selection/regression suites |
| C20: Existing artwork safety | Existing local cover and each distinct embedded front are measured. A clearly better exact-release front can replace them; old cover bytes are backed up, and nonfront picture roles remain. | already resolved and verified | cover/interactive/selection/regression suites |
| C21: Picture headers | MIME from decoded format, oriented dimensions, depth/palette fields; oversized sources receive a bounded embedding derivative while archival bytes remain intact. | already resolved and verified | cover/interactive/selection/regression suites |
| C22: Hidden menu default | Separate automatic choice displayed when winner differs from digital/scan choices; a single remaining category cannot replace that automatic winner. | already resolved and verified | cover/interactive/selection/regression suites |
| C23: Broken comparison report path | JSON uses generated comparison path. | already resolved and verified | cover/interactive/selection/regression suites |
| C24: Explainability | Candidate fields, score breakdowns, rejection reasons, verbose output, performance counters and transaction cover provenance. | already resolved and verified | cover/interactive/selection/regression suites |
| C25: Representation fallback | Actual evaluated URL/kind and original verification state recorded; fallback thumbnails never inherit original measurements. | already resolved and verified | cover/interactive/selection/regression suites |
| C26: Manual choice | `--manual-covers` renders and lists only decoded, plausible front candidates with no release, role, image or quality rejection; `-vv` is unnecessary. | already resolved and verified | cover/interactive/selection/regression suites |

## Lyrics checklist (L references)

Each checked item is revalidated as a contract, not as proof of live availability.

| Reference | Acceptance criteria (lyrics_utils/quality/pipeline/writer/cache) | Status | Evidence |
|---|---|---|---|
| L01 | Section labels do not appear in playback-facing `LYRICS`; exact provider   text, source LRC, and existing tag values are retained in archival sidecars. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L02 | Standard LRC is the synchronized playback representation; source/native   and enhanced forms remain sidecars when available. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L03 | Available original, source, enhanced, native TTML, raw-provider,   translation, and romanization forms are preserved as sidecars. Original   lyrics are also embedded; translations and romanizations use separate   `LYRICS_TRANSLATION` and `LYRICS_ROMAJI` project tags. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L04 | Language metadata is retained separately where supplied, including   `LYRICS_LANGUAGE`, `LYRICS_TRANSLATION_LANGUAGE`, and   `LYRICS_ROMAJI_LANGUAGE`. These fields are project conventions, not a promise   that every player recognizes them. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L05 | Existing local lyrics and sidecars are preserved by default; online   lookup may add a missing form but does not overwrite the curated value. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L06 | Generated ASR/alignment output is opt-in and cannot automatically   replace provider or local lyrics. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L07 | No provider-order winner: `lyrics_utils.fetch_lyrics_cascade` collects   candidates; `lyrics_pipeline.choose_provider_result` and   `lyrics_quality.select_best` select after identity and quality checks. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L08 | Reject conflicting ISRC/recording IDs and semantic version labels;   compare title, artist, album, duration, and version evidence. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L09 | Preserve raw provider text separately from parsed, normalized, and   serialized derivatives; content-addressed sidecars and manifests record   hashes, provenance, transformations, and parser version. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L10 | Preserve Unicode, punctuation, repeated lines, blank lines, and section   labels in archival text. Comparison-only folding is separate from storage   normalization. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L11 | Remove LRC-to-TTML timing fabrication. TTML is archived only when native;   source formats with real word intervals may produce enhanced LRC/TTML. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L12 | Keep and archive existing lyrics; add remote tags/forms only when absent.   Do not rewrite a live `.txt`, `.lrc`, `.elrc`, or `.ttml` sidecar. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L13 | Playback label cleanup is limited to standalone/prefix section labels;   exact source text remains in sidecars/raw evidence. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L14 | Embedded-only forms are materialized as collision-safe sidecars only   outside dry-run; malformed timed data is archived as raw and never presented   as valid plain playback text. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L15 | Propagate provider identity and alternative candidates through NetEase,   LRCLIB, Yandex, Kugou, Musixmatch, Unison, and the aggregate cascade. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L16 | Keep identity-less or brittle diagnostic-only routes explicitly   unsupported: `syncedlyrics`, Lyrics.ovh, AZLyrics, and SongLyrics return   `UNSUPPORTED_PROVIDER` rather than silently competing with validated   results. Spotify and Deezer are restored as configured provider adapters;   they participate in candidate matching and selection (see provider notes   below). | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L17 | Structured `LyricsCandidate`, `LyricsDocument`, `LyricsLine`, and   `LyricsAssessment` represent identity, forms, exact milliseconds, source   precision, warnings, transformations, and independent quality dimensions. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L18 | Candidate match, version, text, sync, completeness, and source confidence   remain distinct; source reputation is a weak prior only. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L19 | Album agreement strengthens identity; album conflict is visible and a   modest penalty, not a hard rejection that would exclude compilation copies. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L20 | Version signatures read explicit fields and delimited/suffix markers;   ordinary title words are not stripped or rewritten. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L21 | Validate line and enhanced LRC timestamps for syntax, sign, ordering,   offset, duplicate density, coverage, duration, and payload bounds. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L22 | Long intros/gaps remain warnings, not automatic failures. A malformed   sync representation can still contribute validated plain text. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L23 | Compare synced/plain forms using a comparison-only normalization view;   detect material disagreement and suspicious incompleteness conservatively. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L24 | Very short non-empty vocal text remains assessable with a completeness   warning and conservative score; empty or structural-only results are rejected. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L25 | Collapse duplicate/near-duplicate text for selection without counting   copies as independent votes or changing stored text. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L26 | Correct, high-match plain lyrics can beat weak or invalid synchronized   lyrics; good sync is a secondary quality dimension. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L27 | Provider-supplied instrumental status is distinct from `NOT_FOUND` and is   accepted only for explicit, recording-matched LRCLIB evidence. Existing local   lyrics prevent a remote instrumental claim from overriding local state. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L28 | Typed outcomes distinguish `NOT_FOUND`, `TRANSIENT_ERROR`,   `RATE_LIMITED`, `BLOCKED`, `AUTHENTICATION_ERROR`, `PARSER_ERROR`,   `INVALID_RESPONSE`, `NOT_CONFIGURED`, `UNSUPPORTED_PROVIDER`, and   `INSTRUMENTAL`. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L29 | Bounded response reads, strict charset decoding, inert HTML parsing,   JSON/schema validation, and explicit parser diagnostics protect against   malformed and oversized provider input. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L30 | Retry/backoff is bounded; `Retry-After` is honored; host pacing is shared;   permanent parser/schema errors are not retried as network failures. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L31 | Only valid positive provider results and trustworthy empty API responses   are cacheable. Transient/parser/rate-limit failures are not negative cached. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L32 | Cache identity includes provider, adapter, title/artist/album/version,   duration, stable IDs, and language; parser/schema version invalidates stale   serialized responses. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L33 | `provider_cache.PARSER_VERSION` is `2026-10-05-lyrics-v11` (supersedes the historical v9/v10 checklists). | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L34 | Content-addressed sidecars prevent collisions and preserve curated files;   conversion manifests now include schema and parser versions, evidence,   transformations, and artifact hashes. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L35 | HTML provider fixtures cover Genius, Bandcamp, DarkLyrics, Metal   Archives, PetitLyrics, Megalobiz, RentAnAdviser, Tekstowo, AmDm, 1ting,   VK Topics, and UltraStar. API/parser fixtures cover Musixmatch, QQ, Kugou,   Yandex, NetEase, VocaDB, Unison, Chorus chart parsing, and the restored   Spotify and Deezer adapters (`test_restored_lyrics_providers.py`). | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L36 | Challenge/block results are classified and surfaced. No CAPTCHA,   authentication, or access-control bypass was added. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L37 | Fragile adapters without identity evidence or usable diagnostics are   explicitly unsupported rather than returning unattributed lyric strings. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L38 | Verbose diagnostics show identity/version, duration, text, sync, source,   warnings, rejection reasons, and selection without logging full lyrics. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L39 | Transformation history records transport normalization, parser   conversions, label display cleanup, and provider-specific cleanup. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L40 | Section E pipeline/transform/provider/scoring recommendations and the   lyrics-specific section J regression list were re-read and checked against   final code. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L41 | Regression tests cover wrong artist/version/ID/duration, provider bias,   plain-vs-sync selection, malformed timestamps, Unicode/layout preservation,   negative caching, scraper/API fixtures, restored-provider authentication and   rich forms, and sidecar/tag preservation. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |
| L42 | Final architecture review found no remaining LRC-to-TTML fabricated   precision and no generic line cleanup applied to stored lyrics. | already resolved and verified | lyrics/provider/selection/safety suites; R06 extends YAML support; R02 updates parser to v11 |

## Original field matrix (D references)

| Original reference | Acceptance / current policy disposition | Status | Evidence |
|---|---|---|---|
| D: `TITLE` | Single; exact credited track title; Exact MB release track or physical tracklist / existing title; NFC; preserve punctuation, case, script and version text; REPLACE only after high-confidence track match; otherwise KEEP | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `ARTIST` | Single credited display string; Exact release-track artist credit / existing; NFC; preserve join phrases such as `feat.` and `&`; KEEP unless exact release track is verified | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `ARTISTS` | Repeated atomic artist names; Structured MB artist credit / structured Discogs credit; NFC; no heuristic punctuation splitting; MERGE only when identities agree; Picard 3 convention, uneven player support | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `ALBUM` | Single exact edition title; Physical cover/exact MB release / existing; Preserve subtitles and edition wording when printed as part of title; REPLACE only on verified release | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `ALBUMARTIST` | Single credited display string; Exact release artist credit / existing; Use `Various Artists` only for genuine VA releases; not merely one featured guest; KEEP or verified REPLACE | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `ALBUMARTISTS` | Repeated atomic release artists; Structured MB release credit; Same rules as `ARTISTS`; ADD/MERGE; compatibility field | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `TRACKNUMBER` | Single positive integer, per medium; Exact medium track position / CUE / filename; No `n/total` packing; REPLACE only after layout verification | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `TRACKTOTAL` | Single integer, tracks on this disc; Exact medium; —; REPLACE when medium is verified | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `TOTALTRACKS` | Same value as `TRACKTOTAL`; Generated alias; —; Optional compatibility alias; always synchronized | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `DISCNUMBER` | Single positive integer; Exact release medium; —; REPLACE only after layout verification | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `DISCTOTAL` | Single integer; Exact release; —; Verified release only | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `TOTALDISCS` | Same value as `DISCTOTAL`; Generated alias; —; Optional compatibility alias | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `DISCSUBTITLE` | Single, only if the medium has a real title; Exact release medium; Do not write “Disc 1”; KEEP or verified REPLACE | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `DATE` | Single partial ISO date: `YYYY`, `YYYY-MM`, or `YYYY-MM-DD`; Specific tagged edition; Preserve known precision; never invent month/day; REPLACE only with exact-release evidence | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `ORIGINALDATE` | Single partial ISO date; Earliest applicable release-group date; Same precision rule; ADD; REVIEW if existing date represents recording rather than first release | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `YEAR`, `ORIGINALYEAR` | Optional derived aliases; First four digits of respective date; Generated; Optional; avoid if applications handle full dates | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `RELEASETYPE` | Repeated lower-case MB primary/secondary types; Exact MB release group; Controlled values; MERGE verified values; not a user genre | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `RELEASESTATUS` | Single controlled value; Exact MB release; `official`, `promotion`, `bootleg`, `pseudo-release`, etc.; ADD only on verified release | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `RELEASECOUNTRY` | Single ISO 3166-1 alpha-2 when meaningful; Exact edition; Uppercase; ADD/REPLACE only on verified release | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `MEDIA` | Single or repeated per application profile; Exact edition medium; Controlled values such as `CD`, `SACD`, `Digital Media`; KEEP on conflict; REVIEW | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `EDITION` | Optional single custom display summary; Physical evidence/exact release; Human-readable, e.g. `DCC 24K Gold remaster`; KEEP/MERGE manually; custom field | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `VERSION` | Optional track-version text only; Exact credited track version; Examples: `radio edit`, `live`, `2024 remix`; Do not put label/year/catalog summary here | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `LABEL` | Repeated; Exact edition label-info/printed release; NFC; retain multiple labels; MERGE only for same release | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `CATALOGNUMBER` | Repeated, ideally paired in provenance with label; Exact edition/printed packaging; Trim only; preserve meaningful punctuation and leading zeros; KEEP on conflict; REVIEW | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `BARCODE` | Normally single digits; repeated only if the exact release documents alternatives; Exact physical edition; Remove scanner spaces/hyphens, preserve leading zeros; REPLACE only with exact edition | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `ISRC` | Repeated recording identifier; MB recording or physical subcode/official label source; Uppercase, 12 alphanumerics without punctuation; MERGE if all identify the same recording; conflicts HIGH | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `COMPILATION` | Single `1` or absent; VA release determination; Do not use merely because tracks contain guests; Verified ADD/REMOVE | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `MUSICBRAINZ_TRACKID` | Recording MBID under Picard’s Vorbis convention. Single in ordinary cases; REPLACE only after verified recording match. | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `MUSICBRAINZ_RELEASETRACKID` | Track entity within a particular release. Requires high-confidence release and medium match. | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `MUSICBRAINZ_ALBUMID` | Specific release MBID. Never derive from the first release in a release group. | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `MUSICBRAINZ_RELEASEGROUPID` | Release-group MBID. Useful even when edition is unresolved, but must not imply a specific release. | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `MUSICBRAINZ_ARTISTID` | Repeated track-artist MBIDs aligned with structured artist credit. | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `MUSICBRAINZ_ALBUMARTISTID` | Repeated release-artist MBIDs. | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `MUSICBRAINZ_WORKID` | Repeated where a recording performs multiple works; preserve hierarchy externally. | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `MUSICBRAINZ_DISCID` | Only from an authentic physical CD TOC. A Disc ID may still be linked to multiple releases, so it is strong medium-layout evidence but not sufficient edition proof. [MusicBrainz Disc ID documentation](https://musicbrainz.org/doc/Disc_ID). | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `ACOUSTID_ID` | Recording-level evidence, not edition identity. Preserve multiple verified associations only with review. | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `DISCOGS_RELEASE_ID` | Specific Discogs release. Write only after edition verification; preserve URL externally or as `DISCOGS_RELEASE_URL`. | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `COMPOSER` | Repeated; typed work relationships, liner notes; MERGE verified names; do not infer from generic “writer” when role is ambiguous | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `LYRICIST` | Repeated; typed work relation or liner notes; Same | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `CONDUCTOR` | Repeated; Track/recording relation first; album credit only if explicitly applies to all tracks | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `PERFORMER` | Repeated values formatted `Name (instrument/role)`; Picard-compatible; avoid simultaneously duplicating everything into `PERFORMER:INSTRUMENT` unless a target player requires it | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `REMIXER`, `PRODUCER`, `ARRANGER`, `ENGINEER` | Repeated; Preserve role scope in provenance; never promote release-level credits to every track without evidence | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `GENRE` | Repeated, small controlled library vocabulary; Existing curated library value first; optionally map provider terms. Omit when unknown | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `STYLE` | Repeated, finer-grained vocabulary; Discogs/user taxonomy; subjective and never allowed to replace curated `GENRE` automatically | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `COMMENT` | Single or repeated human notes; KEEP by default; never use as a dumping ground for machine provenance | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `DESCRIPTION` | Optional human description; KEEP | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `COPYRIGHT` | Exact printed/legal statement; Preserve symbols and wording. Do not synthesize from label plus year | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `LICENSE` | Exact known license; KEEP; never delete merely because the pipeline does not manage it | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `WORK`, `MOVEMENTNAME`, `MOVEMENTNUMBER`, `MOVEMENTTOTAL` | Classical/custom profile; Structured work hierarchy preferred; preserve printed track title independently | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `GROUPING` | Preserve curated GROUPING; scoped verified WORK may supply missing grouping under established project profile. Do not introduce unspecified player profiles. | rejected with evidence | metadata policy preservation; classical fixtures |
| D: `LYRICS` | Original plain-LYRICS suggestion is superseded by explicit user-selected synchronized LYRICS / plain UNSYNCEDLYRICS compatibility policy. Keep both selected forms and archives. | rejected with evidence | lyrics checklist User-selected policy; writer integration tests |
| D: `UNSYNCEDLYRICS` | Original plain-LYRICS suggestion is superseded by explicit user-selected synchronized LYRICS / plain UNSYNCEDLYRICS compatibility policy. Keep both selected forms and archives. | rejected with evidence | lyrics checklist User-selected policy; writer integration tests |
| D: `SYNCEDLYRICS` | Nonstandard but pragmatically useful for LRC-capable players. Use only after compatibility testing and document the convention. | blocked | Storage/tag contract verified offline; actual player interoperability was not tested. Requires selected player versions and temporary playback fixtures, not more parser mocks. |
| D: `LYRICS_TTML`, `SYNCEDLYRICS_ENHANCED` | Custom, poorly interoperable and potentially very large. Prefer versioned sidecars as canonical storage. | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: Translation/romanization | Separate sidecars or explicitly named custom fields with `LANGUAGE`, translation language and relationship in provenance. Never merge into original text silently. | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `LYRICS_SOURCE` | Optional summary only; detailed provider item ID, URL, retrieval time, license and score belong in the provenance ledger. | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `ARTISTSORT`, `ALBUMARTISTSORT`, `COMPOSERSORT`, `TITLESORT` | Write only from authoritative sort names or user rules. Do not mechanically move initial articles in arbitrary languages or song titles. Preserve existing values. | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `REPLAYGAIN_TRACK_GAIN`, `REPLAYGAIN_TRACK_PEAK`, `REPLAYGAIN_ALBUM_GAIN`, `REPLAYGAIN_ALBUM_PEAK` | Canonical ReplayGain fields for FLAC. Compute together with one documented scanner/version. | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `REPLAYGAIN_REFERENCE_LOUDNESS` | Optional; for this RG2 profile, `-18.00 LUFS`. | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `REPLAYGAIN_*_RANGE` | Optional loudgain extensions; harmless but not broadly consumed. | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: `R128_TRACK_GAIN`, `R128_ALBUM_GAIN` | Do not write to FLAC under this profile; these are RFC-defined Opus tags with integer Q7.8 values referenced to −23 LUFS. | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: DR14 fields | Optional custom diagnostics only, derived from the local audio and tagged with tool/version. Never treat them as ReplayGain or R128. | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |
| D: Artwork | Native FLAC PICTURE blocks. Preserve all existing types; replace front art only after exact-edition validation. Keep original scans losslessly outside the listening derivative. | already resolved and verified | schema/policy/writer/identity/QC/lyrics suites; R13 excludes unspecified full browsing profiles |

## Original implementation-plan crosswalk

| Original reference | Recommendation | Status | Disposition / current acceptance |
|---|---|---|---|
| I-P0.1 | Rewrite `write_tags()` in [metadata_writer.py](metadata_writer.py): | already resolved and verified | writer preservation/transaction suites; W01–W04/R01 |
| I-P0.2 | Fix automatic Discogs discovery: | already resolved and verified | real Discogs find() contract, A/S/W discovery tests |
| I-P0.3 | Remove automatic release-group edition fallback and LOW-confidence acceptance in [audiophile_tagger.py](audiophile_tagger.py). | already resolved and verified | field confidence / no edition fallback tests |
| I-P0.4 | Fix `title-artist-mismatch` logic in [mb_identity_score.py](mb_identity_score.py). | already resolved and verified | identity title+artist gates |
| I-P0.5 | Migrate MusicBrainz IDs: | already resolved and verified | entity-backed alias migration and distinct IDs |
| I-P0.6 | Make `DATE` the specific edition date and `ORIGINALDATE` the earliest release date. Deprecate `date_mode=original` as a storage policy. | already resolved and verified | DATE/ORIGINALDATE schema/tests |
| I-P0.7 | Never store audio-derived pseudo-TOCs as `MUSICBRAINZ_DISCID`. Mark `CUE+FLAC-TRACKS` as estimated, not exact. | already resolved and verified | authentic TOC only; A-023/W05/W12 |
| I-P0.8 | Stop overwriting sidecars and curated lyrics. Disable neural transcription by default and require explicit generated-lyrics mode. | already resolved and verified | curated local preservation; S-03/S-07/S-08 |
| I-P0.9 | Remove the DR database’s `entries[0]` fallback and stop importing remote min/max/IDs without exact edition review. | already resolved and verified | local DR/report identity; W15–W18 |
| I-P1.1 | Introduce explicit release/medium/track/recording/work models shared by Discogs and MusicBrainz matchers. | rejected with evidence | R15 minimal coherent entity contracts |
| I-P1.2 | Require hard conflict checks and winner margins; incorporate full-album fingerprints and artist/version matching. | already resolved and verified | every-track AcoustID lookup; hard conflicts/margins tests |
| I-P1.3 | Implement credited `ARTIST` plus repeated `ARTISTS`, and the corresponding album fields. Remove heuristic splitting as a source of truth. | already resolved and verified | structured credited/atomic names; W10 |
| I-P1.4 | Fix multidisc traversal, per-disc totals, nested folders and multiple CUE/log files. | already resolved and verified | shared multi-disc traversal/evidence; A/W cases |
| I-P1.5 | Remove default English language and fabricated `GENRE=Unknown`. | already resolved and verified | unknown language/genre omission tests |
| I-P1.6 | Separate `VERSION` from `EDITION`; stop synthesizing copyright and CD provenance. | already resolved and verified | VERSION/EDITION and exact copyright/source tests |
| I-P1.7 | Replace lyrics provider priority with candidate collection and scoring. Validate timestamp monotonicity, coverage, duplicates, language and text consensus. | already resolved and verified | S-01–S-09/S-15–S-16 plus R06/R16 |
| I-P1.8 | Gate artwork on exact release identity before visual-quality scoring. | already resolved and verified | C/S cover tiers; lower-evidence fallback explicit, no edition promotion |
| I-P1.9 | Rebuild [verify_all_flacs.py](verify_all_flacs.py) around the severity model and cross-library checks. | already resolved and verified | severity QC, release/position/identifier/library tests |
| I-P2.1 | Add structured provider adapters, typed responses and a shared retry/rate-limit layer. | already resolved and verified | typed acquisition outcomes and shared transport; R02–R05 |
| I-P2.2 | Add TTL/versioned caches with negative-cache expiration and `Retry-After` handling. | already resolved and verified | versioned/expiring caches and bounded Retry-After; R19 |
| I-P2.3 | Add an append-only provenance ledger and dry-run report. | already resolved and verified | R01/R08 provenance and dry-run reports |
| I-P2.4 | Add NFC storage/comparison policy while preserving original display strings. | already resolved and verified | comparison shadow + retained source; schema/Unicode tests |
| I-P2.5 | Consolidate the Fish pipeline into a thin wrapper around the Python implementation. | already resolved and verified | R14 deleted legacy wrapper, not recreated |
| I-P2.6 | Version locally derived BPM/key/mood algorithms; treat discontinued AcousticBrainz values as optional legacy evidence. | already resolved and verified | R07/R09 versioned diagnostics and optional legacy evidence |
| I-P2.7 | Reduce redundant technical tags already represented by STREAMINFO, or explicitly mark them as generated snapshots. | already resolved and verified | STREAMINFO duplicates removed from manifests; snapshots remain in ledger |
| I-P2.8 | Replace broad swallowed exceptions with typed errors and explicit incomplete-state reporting. | already resolved and verified | typed provider/worker failure propagation and review status |
| I-P3.1 | Full classical work hierarchy and configurable classical display profiles. | rejected with evidence | R13 optional unspecified classical profiles rejected |
| I-P3.2 | OCR-assisted barcode/catalog/matrix review from scans. | rejected with evidence | R13 uncalibrated OCR assertions rejected |
| I-P3.3 | Perceptual artwork deduplication while preserving original files. | already resolved and verified | cover duplicate grouping retains originals; C/S cases |
| I-P3.4 | Versioned TTML/ELRC sidecar archive with conversion manifests. | already resolved and verified | versioned lyric manifests, native/source intervals; R06 |
| I-P3.5 | Optional cross-library acoustic duplicate and alternate-master reports. | rejected with evidence | R13 alternate-master report thresholds unspecified |
| I-P3.6 | Configurable player profiles for Navidrome, foobar2000, MusicBee, Roon and hardware DAPs. | rejected with evidence | R13 player mappings unspecified, preserve chosen policy |


## Appendices and cross-module acceptance

- Original B/C/E/F/G/H/J duplicate observations map to the numbered findings,
  C/L/D criteria and I crosswalk. Required preservation, metadata/entity/date,
  workflow, gain-versus-amplitude and fixture checks are exercised by the full
  suite; optional experiments are R13/R15–R19. Original K choices use existing
  README/TOML/lyrics policy: no new incompatible display profile is inferred.
- Failure matrices: auth/config/empty/parser/rate limit are R02/R03/R11;
  retrieval/decode/invalid sync/materialization alternatives are S/C/L suites;
  local-only/cancel/category/quiet/manual/non-TTY/dry-run/crop consistency are
  interactive and real tagger fixtures; interrupted publication is R01/R04.
- Provider acquisition rows retain the matrix in SELECTION_AUDIT.md. R06 updates
  LRCLIB acquisition; other observed bounded search/detail routes retain their
  supported contracts. No unlimited crawling or guessed CDN transformations.
- Journaled recovery is explicit, not an atomic album snapshot. A terminated
  preparation may leave unjournaled private staging trees with untouched live
  files. Legacy trees, cross-directory standalone ledgers, noncooperating editors
  and physical power-loss behavior need manual/environment-specific review.
  Existing outside-journal pending/rollback files may remain after a killed
  standalone writer; recovery restores tracked user content and leaves unknown
  residue for inspection rather than deleting by filename alone.
- Recovery and publication are cooperating POSIX/local operations. Custom
  transport/auth callbacks and other applications are not universally locked.
- Disk-backed decode bounds RAM but requires temporary disk space and still
  decodes the full track. No general runtime speedup or unlimited-size guarantee.

## Verification record

Baseline `.venv/bin/pytest -q`: **576 passed in 119.57s**.
The first five cache/status regressions failed before correction. The dry-run
review/no-op regression also failed before correction. New architectural
recovery/concurrency paths have process/failure-injection tests; there is no
claim that every new test was run against old code.

Intermediate full run: **592 passed in 114.86s**. After bounded PCM refactoring:
**611 passed, 2 failed in 122.03s**; both exposed lost `over` peak formatting.
The original peak policy was restored; both original regressions pass unchanged.
The 8-bit fixture initially expected an unquantized 70% peak; its exact signed
8-bit 89/128 source is 69.5%, and the test now asserts that value explicitly.

Stable pre-final-review full run: **619 passed in 115.24s**. The previous
**614 passed / 3 failed** run changed Python source while source-inspection tests
were running; their loaded function line numbers no longer matched disk source.
The unchanged tests pass in the stable run. Final review then reproduced R20/R21:
`.venv/bin/pytest -q test_backlog.py -k 'session_query_parameters or disc_writer_refuses'`
— **2 failed, 43 deselected** before fixes.

Related integration run after R20/R21:
`.venv/bin/pytest -q test_backlog.py test_entire_audit.py test_selection_audit.py test_restored_lyrics_providers.py`
— **147 passed in 9.63s**. This includes real writer/album wiring, decoded PCM,
process termination and rollback, exact archived YAML/provider forms, temporary
FLACs, and controlled provider boundaries.

Final checks on stable Python source, all exit status 0:

| Exact command | Actual outcome |
|---|---|
| `.venv/bin/pytest -q` | **621 passed in 115.36s**; 45 new parametrized backlog cases beyond the 576-test baseline |
| `.venv/bin/ruff check .` | All checks passed |
| `.venv/bin/ruff format --check .` | 45 files already formatted |
| `.venv/bin/mypy .` | No issues in 38 source files |
| `.venv/bin/python -m pip check` | No broken requirements |
| `git diff --check` | No whitespace errors |
| `.venv/bin/python audiophile_tagger.py --help` | Exit 0 |
| `.venv/bin/python audiophile_cover_fetcher.py --help` | Exit 0 |
| `.venv/bin/python lyrics_utils.py --help` | Exit 0 |
| `.venv/bin/python metadata_writer.py --help` | Exit 0 |
| `.venv/bin/python album_transaction.py --help` | Exit 0 |

The original numbered findings are verified against their existing regressions;
new R01–R10/R20–R21 behavior uses the tests above. Crosswalk rows consolidate
repeated recommendations without discarding their references. That implementation run reported no actionable confirmed defect pending;
the independent follow-up below subsequently reopened incomplete fixes. R11/R12/R17/R18 remain blocked
verification limits; R13/R15/R16/R19 and superseded field suggestions retain
explicit evidence-backed rejection rather than pretending they were implemented.
There is no claim of fresh-host CI, authenticated live compatibility, complete
heuristic accuracy, physical power-loss safety, or elimination of every bug.

Memory measurement: a temporary 30-second 192 kHz stereo 32-bit FLAC generated
with `ffmpeg -hide_banner -loglevel error -f lavfi -i
'sine=frequency=440:duration=30:sample_rate=192000' -ac 2 -sample_fmt s32 sample.flac`
was measured by baseline `bd5f7d0:dr14_parse.py` and current `measure_audio_file`.
Separate `.venv/bin/python` children measured with `os.wait4` / `ru_maxrss` used
**498588 KiB / 1.177s** baseline and **113268 KiB / 1.016s** current. Both returned
DR 0, peak/RMS −21.07 dB and 00:30. This is one local fixture, not a general
runtime speedup; decoded temporary-disk usage remains. `/usr/bin/time` was
unavailable, so the resource measurement used Python's child resource accounting.

## Independent verification of 663992b — 2026-10-05

Started from a clean checkout of `663992b`. Independently inspected the full
commit's changed contracts and affected callers, WHOLE_PROJECT_AUDIT.md,
SELECTION_AUDIT.md, AUDIT_LEDGER.md, both implementation checklists and the
original saved report A–K (including field tables, architecture, test proposals
and optional enhancements). All A/S/W references are represented in this ledger.
The original stand-alone cover prose is unavailable, as its checklist already
states; reconciliation covers the retained C criteria and selection audit.
No real collection enrichment or authenticated live provider check was run.

| Reference / reopened entry | Severity, trigger and root cause | Correction / acceptance evidence | Disposition |
|---|---|---|---|
| V01 / R01, W04 | Critical: standalone lyric archive opened at its live path before writing; a process kill produced an empty archive whose hash recovery refused. Prior kill tests only covered FLAC/ledger renames. | Stage complete archive bytes and publish by exclusive hard link; real writer process-death test checks exact original lyric JSON, restored FLAC/ledger bytes, decoded PCM and removal of the new artifact. | fixed and verified |
| V02 / R01 | High: cleanup could delete snapshots while the journal still required them on the next invocation. A completed journal also unnecessarily required old snapshots. | Durably record `rolled_back` after validating/syncing restored content, before cleanup; completed states validate their expected live content and need no obsolete snapshots. Test interrupted cleanup in both prepared and committed states, then repeat recovery. | fixed and verified |
| V03 / R04 | High: fork copied thread-local reentrancy, so a child treated the parent's lock as its own and entered before release. Inherited descriptors could prolong ownership. | Close registered inherited writer/file-lock descriptors and reset reentrancy in the child; verify exclusion while parent holds the lock and acquisition after release. This does not certify arbitrary forks of an active multithreaded application or third-party locks. | fixed and verified |
| V04 / R02 | High: unknown `Vary` disabled only successful-response caching; 404 still poisoned a second request context. Old unsafe negatives survived the fix. | Apply the Vary gate to 404 too; parser v11 forces refetch of v10 entries. Test an actual 404-to-valid-response fallback with different Accept values and a preexisting v10 negative. | fixed and verified |
| V05 / R06, S-02, L15 | High: one malformed LRCLIB search item discarded every valid item in the batch. | Filter malformed entries independently, retain eligible candidates and carry partial parser diagnostics through real candidate assessment. | fixed and verified |
| V06 / R02, W20, L31 | Medium: LRCLIB deleted schema failures by URL while Requests credentials/language used a different actual cache key. | Evict the prepared request key; real Requests transport fixture with an account header verifies no malformed schemas remain cached. | fixed and verified |
| V07 / R06, S-03, L22 | High: malformed YAML line structure raised before extracting valid independent plain lyrics. Existing tests covered negative timestamps, not invalid line objects/sequences. | Retain independent plain text with warnings, reject unusable timing and keep exact YAML. Actual adapter → selection → materialization regression covers both malformed structures. | fixed and verified |
| V08 / R21, R01 | Critical: an interrupted standalone CD1 writer left a child journal; root album processing ignored it and committed again. Recovery on CD1 also locked CD1 rather than the root used by writers. | Find journals in normal direct disc folders, block acquisition before another commit, recover each journal relative to its own parent, and lock the canonical album root. Real killed-writer → album refusal → dry-run/recovery → exact-byte/PCM checks pass. Arbitrary non-disc nesting is outside this grouping contract. | fixed and verified |
| V09 / R04, R01, W03 | High: album journal snapshot and ledger publication used different lock scopes. A cooperating writer targeting the same ledger changed the expected hash, causing incomplete rollback. | Hold the ledger lock from snapshot through publication/finish/rollback. A real second process writes another temporary FLAC using a shared ledger; both transactions and all rows survive. | fixed and verified |
| V10 / R06, L27 | High: native YAML instrumental evidence became NOT_FOUND; an outer instrumental flag could override actual native vocal text. | Validate native evidence before the instrumental decision; retain strict recording/version/duration gates; vocal text remains eligible with a conflict warning. Two native-form acquisition regressions reproduced the wrong statuses. A final malformed-response check also reproduced string `false` and integer `1` being truthily accepted as no-vocals evidence; certification now requires a JSON boolean, and malformed instrumental-only responses are parser failures. | fixed and verified |
| V11 / R06, L09 | Medium: instrumental decisions had no LyricsAssessment and therefore discarded retained native/raw forms before final commit. | Archive exact instrumental YAML/provider JSON without making up lyrics; carry provider ID, output hashes and INSTRUMENTAL status into actual tagger provenance. Album integration verifies committed sources/tags and decoded PCM preservation. | fixed and verified |
| R22 / original J, R17 | Optional: splitting the mixed historical test files into new unit/integration/live groups has not been implemented; this is not an external-service blocker. | Reclassified explicitly. Current tests are offline and full runs remain required; reorganizing mixed unittest/pytest files provides no demonstrated correctness fix in this review. Actual live tests would need a separate credential/fixture contract. | rejected with evidence |

The unsupported adapters remain explicitly identified in L16; no supported
provider was deleted. R13 optional new display/OCR/mastering/player products,
R15 alternate evidence-graph rewrite, R16 illustrative scoring constants and
R19 conditional-cache/durable-cooldown scheduler remain rejected proposals,
not implemented features. The code's finite TTLs, matching gates and bounded
retries were inspected instead of accepting their labels as proof.

R01 is scoped to album transactions and default/same-parent standalone ledgers.
Automatic journal recovery for a custom provenance file outside the FLAC parent
is **not implemented**; it is not externally untestable and is not represented
as a fixed feature. The existing explicitly selected outside-directory ledger
API retains its catchable rollback/manual-recovery behavior. Extending recovery
requires an authorized external-root contract and two-root publication/kill/
recovery tests; accepting arbitrary absolute paths from a journal would weaken
its path boundary. Legacy unjournaled trees similarly require manual review.

Player compatibility for D: SYNCEDLYRICS is reopened as blocked verification:
writing the compatibility tag is tested, playback in the user's selected player
versions is not. R11 live/private APIs, R12 platform/filesystem/power-loss,
R17 fresh-host CI and R18 broad heuristic datasets remain verification limits.
Fresh-host CI needs replacement/licensed fixtures for historical Queen-dependent
tests and an observed runner. The local split-group proposal is separated above.

Independent baseline `.venv/bin/pytest -q`: **621 passed in 121.28s**.
The first reproduction batch contained test-call mistakes in three new cases;
those were corrected before counting them as reproductions. The corrected
LRCLIB subset then showed **4 failed / 6 deselected** before provider fixes.
Partial-cleanup (2), archive publication, fork ownership and negative Vary
regressions failed before fixes. The native-instrumental pair showed
**2 failed / 12 deselected**, the album-source-retention case showed
**1 failed / 14 deselected**, and the old v10 negative showed
**1 failed / 15 deselected** before their fixes. Disc and shared-ledger integration
regressions separately failed against the unsafe production paths.

Related post-fix run:
`.venv/bin/pytest -q test_independent_verification.py test_backlog.py test_entire_audit.py test_selection_audit.py test_restored_lyrics_providers.py`
— **162 passed in 14.74s** before the final v10 invalidation case.
Intermediate full run: **636 passed in 109.89s** before that invalidation case.
Parser-v11 full run before the last boolean-type regressions:
**637 passed in 106.68s**. Those last two regressions failed before correction
(`.venv/bin/pytest -q test_independent_verification.py -k malformed_instrumental`
— **2 failed / 16 deselected**), then the independent suite passed
**18 tests in 7.18s**. The native-source integration fixture was tightened to
use distinct provider IDs for the two distinct tracks.

An additional temporary-data production check held the canonical album-root
flock and called `album_transaction.main([CD1])`: it refused recovery with
exit 1 and unchanged contents. After lock release it returned 0 and restored
the original cover bytes. The check used a temporary journal fixture and
`fcntl.flock`, without mocking recovery or locks. This verifies the direct-CD
entry point in addition to the killed-writer root-recovery regression.

Final stable checks, all exit status 0:

| Exact command | Actual outcome |
|---|---|
| `.venv/bin/pytest -q` | **639 passed in 108.85s**; includes 18 new independent regression/verification cases |
| `.venv/bin/pytest -q test_independent_verification.py` | **18 passed in 7.18s** before tightening the two-track provider-ID fixture; tightened fixture also passes in the final full run |
| `.venv/bin/ruff check .` | All checks passed |
| `.venv/bin/ruff format --check .` | 46 files already formatted |
| `.venv/bin/mypy .` | No issues in 39 source files |
| `.venv/bin/python -m pip check` | No broken requirements |
| `git diff --check` | No whitespace errors |
| `.venv/bin/python album_transaction.py --help` | Exit 0 |
| `.venv/bin/python audiophile_tagger.py --help` | Exit 0 |
| `.venv/bin/python lyrics_utils.py --help` | Exit 0 |

No original test assertions were weakened or providers removed. Baseline and
final full runs were executed independently of the preceding implementation
report. Passing fixtures establish the named paths, not complete correctness.
The reopened fixes are covered by actual writer/tagger/provider/cache/recovery
wiring; remaining scope and optional non-implementations are identified above.

The upstream [Lyricsfile draft specification](https://github.com/tranxuanthang/lyricsfile/blob/main/SPECIFICATION.md)
was read during this verification. Independent plain/timed forms, boolean
instrumental evidence, real millisecond intervals and unresolved offsets were
checked against it; this does not verify the live LRCLIB endpoint supplies every
form or any authenticated provider's current response schema.

Review of the audio-analysis tests distinguishes two kinds of evidence:
`test_backlog` compares streamed/file decoding to the shared block algorithm;
it is not an independent mathematical DR reference. Existing short/mono/silence
analytic vectors assert specific measured peak/RMS behavior independently.
Agreement with a proprietary meter still requires separately supplied reference
vectors/tools, as already stated in W15/R07. No new performance benchmark was
claimed during this verification.

## Real-world copy validation — 2026-10-05

See REAL_WORLD_VALIDATION.md and `/home/emvis/flac-validation-20261005/reports/`.
The located real collection contains only partial single-disc albums. Tested
Innuendo (5 tracks), Made in Heaven (3), and Japanese News of the World (2),
using full independent copies outside the collection. All original sample files
and decoded audio remain unchanged. Dry-run, automatic, repeated, PTY default
artwork and external-boundary fallback paths were exercised. Accepted corrected
editions remain INCOMPLETE; grouping is not forced to COMPLETE.

| Reference | Trigger / root cause | Implementation and evidence | Status |
|---|---|---|---|
| RW01 / release/recording position evidence | Sorted file index was used as AcoustID release-track position; missing songs and disc resets created false hard conflicts. | Map explicit disc/track evidence into all candidate media; legacy selected-medium index API remains supported. Real Innuendo now matches all 5 recordings, rather than 1; production regression verifies final recording/release-track namespaces and wrong-disc recording rejection. | fixed and verified |
| RW02 / R18 partial albums | Full count equality rejected a supported partial subset as a different medium. | Permit only explicit, range-valid, duration-compatible subset positions; reject incomplete evidence/conflicting durations and retain complete=False. Real Innuendo/Made edition selections recovered; both remain INCOMPLETE. | fixed and verified |
| RW03 / numbering | Resolved release count was overwritten by present FLAC count; dry-run proposed 5/3/2 totals instead of 12/13/11. | Use fetched per-medium/explicit release positions before single-disc resolved evidence. Actual dry-run integration reproduces old proposal and verifies correction. | fixed and verified |
| RW04 / R10 diagnostics | Dry-run announced writing and incorrectly attributed DR14 skip to --no-dr14. | Report planning and actual --dry-run reason. Real corrected CLI dry-run and focused production regression pass without album changes. | fixed and verified |
| R11 live services | Previously no authenticated live evidence. | Credentialed Discogs and public MusicBrainz/LRCLIB checked live; NetEase/QQ and several text providers produced candidates. Spotify/Deezer auth, Yandex/VK/Metal-Archives blocking, TLS/parser/rate-limit failures remain distinctly reported; matrix in validation report. | blocked for remaining routes; partially live verified |
| D: SYNCEDLYRICS | Personal player compatibility is still not established. | Installed mpv renders embedded artwork and IPC parses an external LRC cue at its real time; no GUI/plain/embedded-lyrics assertion. IPC controlled quit exits 4, not a successful normal playback claim. | blocked for remaining interoperability checks |
| R18 complete/multi-disc live coverage | No such actual albums were located; query for another path unanswered. | Synthetic positioned multi-disc/conflict regressions pass, but these do not replace real complete/multi-disc runs. | blocked by missing representative input |

Added `test_real_world_regressions.py`: 6 cases. Corrected initial test-call/schema
mistakes before recording reproductions; two integration cases and the diagnostic
case failed before their fixes. Final full suite: **645 passed in 110.31s**;
Ruff lint/format (48 files), mypy (40 source files), pip check and diff check pass.
Independent inspection uses metaflac, ffprobe, Pillow and ffmpeg decoded PCM.
Controlled image HTTP failure and materialization/file-open injection delegate
real selection/commit behavior; invalid initial HTTP doubles were separated and
corrected before claiming fallback evidence. Original credentials/configuration
are not modified, and reports contain lyric counts/hashes rather than full text.
No whole-project or physical power-loss guarantee is added.

A separate fresh News copy without existing ReplayGain tags exercised real
`/usr/bin/loudgain -r -k -s e`: exit 0, track gains −4.12/−2.33 dB and finite
peaks, no album gain fields, decoded PCM unchanged. Existing valid measurements
were preserved in the other scenarios; successful cleanup is not substituted
for a scanner run. Personal GUI/embedded lyric display and real complete/multi-disc
inputs remain missing; actual terminal Kitty rendering was not observed.
