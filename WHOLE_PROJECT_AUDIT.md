# Whole-project audit — 2026-10-04

Baseline: clean checkout `87e9d4d` in `/home/emvis/Music/auph`.
No repository/ancestor AGENTS instructions were present. Production dependencies
and development checks come from requirements files and `ruff.toml`; no missing
production modules were found. This is a Python repository, not an attachment
bundle; upload suffixes are not module names here. Changes preserve the music
collection: new reproduction, failure injection and PCM checks use temporary
files. Some historical tests read adjacent Queen library assets; those assets
were not edited. No live enrichment command was run on the collection.

## Execution and coverage record

CLI/environment/TOML → recursive album discovery and advisory album lock →
local positions, comments, CUE/log/audio evidence → Discogs discovery/detail →
MusicBrainz edition/medium scoring plus AcoustID recording evidence → release
selection → cover acquisition/validation/preview/preparation and lyrics
acquisition/scoring/materialization → per-track manifest/field decisions and
staged FLAC writer → optional local DR/ReplayGain → staged verification →
individual track replacements → exclusive new artifact publication → ledger
publication. Failed catchable commits attempt restoration; failed restoration
retains recovery copies. This is **not an atomic album snapshot**.

The following records inspected production contracts and exercised paths. It
is not a claim that every line, provider response, language or platform was
exhaustively tested.

| Modules | Inspected paths / verification |
|---|---|
| `audiophile_tagger` | CLI, matching callers, enrichment handoffs, audio helpers, completeness, cover/lyric integration, staging, ReplayGain and final commit; temporary real-FLAC success/no-op/dry-run/failure/interruption tests |
| `library_files`, `library_layout`, `album_lock` | Recursive/private-input filtering, position conflicts and multi-disc grouping, concurrent album refusal; existing traversal/layout/lock tests plus new explicit private/link/uppercase discovery |
| `discogs_release_find` | Local signatures, physical/AcoustID/MB relations, search limits, duration vectors, scoring and margin; new physical tie/sparse-duration/bridge fixtures |
| `mb_identity_score`, `release_match`, `mb_track_match` | Edition versus recording evidence, TOC authenticity, layout/duration/positions, ambiguity, finite scores, artist credits/version markers; new synthetic-TOC, missing-duration/count, ambiguous-track and credit tests |
| `mb_discid_from_cue`, `album_completeness` | CUE/index/log evidence, synthetic geometry/order, per-disc grouping and completeness handoff; old physical-TOC/conflict/missing-disc tests and new malformed CUE/flat multi-disc/position ordering tests |
| `metadata_schema`, `metadata_policy`, `metadata_writer` | Entity-scoped decisions, multivalue/unknown/curated preservation, aliases, optional values, incoming date/numeric validation, pictures, no-op/PCM checks, archives and ledger recovery; real writer/CLI and interrupt/fsync regressions |
| `audiophile_cover_fetcher` | Provider representations, release/front gates, decode, quality/provenance, dedup, tier ranking, crop/preview/save/embed and materialization fallback; existing synthetic-image and real tagger fixtures rerun; new diagnostic redaction |
| `lyrics_utils`, `lyrics_quality`, `lyrics_pipeline` | Adapter/collector contracts, preserved forms/alternatives/statuses, identity/version/quality, sync validation, dedup, curated completion, selection and isolated materialization fallback; provider/selection/restored-provider suites rerun; exception diagnostics corrected |
| `loudness`, `dr14_parse` | Completeness-dependent grouping, gain/peak/range validation, audio preservation, subprocess publication/recovery, native DR/parser/aggregation/report/CLI; new short/mono/silence/8-bit/partial/report-link/exit-code regressions |
| `provider_cache`, `pipeline_config` | Payload bounds, schema/TTL/retries/rate limits, corruption/eviction, API error envelopes, credential/language contexts, configuration precedence/types/ranges/invalidation; new cache and configuration fixtures |
| `verify_all_flacs` | Stream/container/tag/album/library QC and CLI severity exit; calendar validation and bounded stream verification, existing QC suites rerun |
| `check_tokens`, `check_all_providers` | Environment loading, credential health/status reporting and provider diagnostic entrypoint; environment/AcoustID payload regressions; no authenticated diagnostic live run |

README, policy TOML, cover implementation record, lyrics checklist and earlier
selection/audit ledgers were compared to callers and tests. Earlier green runs
are historical evidence only; this audit establishes its own baseline/checks.

## Confirmed defects, triggers and fixes

| ID / severity | Root cause and consequence | Implemented correction / evidence |
|---|---|---|
| W01 Critical | Album commit caught `Exception`, bypassing rollback on `KeyboardInterrupt`; recording replacements only after rename also missed post-publication interrupts. | Catch interruption, track attempted publication, restore published tracks only and re-raise interruption after recovery; before/after second-track injection restores exact original bytes. |
| W02 Critical | Writer post-replacement interruption occurred before its publication flag, deleting the only rollback copy. | Record replacement intent before publication; real writer interruption restores original bytes. |
| W03 High | Ledger rename followed by interruption left rows claiming reverted tag writes. | Shared locked ledger publication retains/restores prior ledger state; old/absent ledger writer and album interruption tests, existing fsync injection. Recovery failure retains the prior ledger copy. |
| W04 Critical | Check-then-rename sidecar/report creation could overwrite a file created between check and publication. Content-addressed artwork archive collisions could also lose retained source evidence. | Exclusive same-filesystem hard-link publication with catchable-interruption tracking; collision rejects publication and rolls back audio. Verify archive bytes before reusing a name. Real album late-report collision preserves the concurrent user file. |
| W05 High | Synthetic Disc IDs were promoted to authentic medium matches; Discogs bridge discovery added remote catalog numbers to local evidence and granted confidence bonuses for the discovery route itself. | Require authentic nonpartial TOC for exact evidence, stop remote-to-local mutation, keep bridge bonuses only for retrieval order. Synthetic edition and catalog-evidence regressions. |
| W06 High | Discogs `CERTAIN` score bypassed winner margin, selecting an arbitrary equally supported edition. | Every distinct edition needs the configured margin; deterministic ID ties require review. Real physical scoring fixture. |
| W07 High | Discogs AcoustID bridge inspected only the first response and ignored score; rich later confident release candidates were discarded. | Shared finite/confident AcoustID ranking, bounded HTTP/cache transport, distinct release lookup limit 20 with visible truncation. Later-strong-result fixture; prior bridge fixture updated to realistic confidence/body contract without weakening assertions. |
| W08 High | MB track matching ignored recording length fallback and accepted nonfinite durations; release scores could be infinite or crash parsing. | Use finite recording milliseconds when release-track length is absent; reject malformed candidates/scores. Duration and score regressions. |
| W09 High | Missing track positions borrowed enumeration order, while duration agreement bypassed ambiguity; duplicate medium positions picked the first. | Use explicit position evidence, reject insufficient track margin and duplicate medium positions. Repeated-title/duration fixture. |
| W10 High | Recording artist credit replaced release-track display credit/atomic names; broad title-word version markers missed clean/explicit but treated ordinary “Live” as a version. | Prefer actual release-track credits/join phrases; use shared explicit/delimited version signatures. Credit, clean/explicit and ordinary-title regressions. Comparison NFC is separate from stored credits. |
| W11 High | Missing MB `track-count` discarded fetched full track-list evidence, keeping valid albums unknown/incompatible. | One `release_disc_counts` contract derives absent counts from fetched tracks, refuses unresolved/duplicate media, and is used in both layout validation and actual tagger completeness handoff. Real completeness fixture. |
| W12 High | CUE parser silently dropped tracks lacking INDEX 01; synthetic TOCs used filename order and flat multi-disc TOCs repeated all files on each disc. | Reject incomplete/duplicated/out-of-order CUE numbering; order by corroborated positions; group actual files per medium; synthetic evidence remains nonexact. Malformed CUE and per-disc/order regressions. |
| W13 Medium | Discovery missed uppercase FLACs, misgrouped a direct disc-root traversal, and accepted explicit private/symlink inputs. | Case-insensitive file checks, consistent album-parent grouping, private/symlink exclusion before resolution. Discovery fixtures. |
| W14 High | Track-only gain removal missed album range tags and ran only when existing track tags were valid; scanner absence retained stale album claims. | Clear album gain/peak/range for incomplete/unknown sets, including metadata-only cleanup when scanner/track measurements are absent. Distinct `album-tags-cleared` outcome; PCM preserved; no invented gain. |
| W15 High | Native DR short tracks returned invented peak/RMS, mono RMS divided by two, exact blocks added zeros, and silence overflowed. | Measure real peak and sine-reference whole-track/channel RMS; use actual blocks; finite silence handling. Analytic short/mono/silence tests. Compatibility with proprietary meters is unverified. |
| W16 High | Partial measurement produced album DR; unknown FLAC geometry defaulted to guessed 16-bit stereo; 8-bit peak decoder used 32-bit interpretation. | Withhold partial album DR; require readable stream geometry and supported depth; decode 8-bit correctly. Invalid-file and real 8-bit FLAC regressions; bounded decoder subprocesses. |
| W17 High | DR report identity normalization erased non-Latin names, allowing unrelated headers to compare equal. | Unicode/NFC comparison retains actual identity. Non-Latin regression; report mapping still requires unique names/titles. |
| W18 High | Standalone DR report saving followed dangling links; measurement CLI returned zero even when all measurement failed. | Refuse symlink report, create report exclusively and preserve existing files, return nonzero on failed/partial measurement. Actual CLI/link fixtures. APE/WV direct decoding remains unsupported and now reports failure. |
| W19 Medium | Silence autocorrelation selected the first lag as a false 200 BPM. Malformed optional AcousticBrainz model fields could abort the whole album. | No inferred tempo without an energy signal; bounded shared HTTP acquisition and independent validated model probabilities. Silence and malformed-analysis/valid-BPM fixtures. |
| W20 High | HTTP-200 API error envelopes were durable successful cache entries; URL-only keys mixed explicit authentication/language contexts; cache eviction stat race raised. | Error envelopes are unsuccessful, not cached, and old error entries invalidated; digest explicit credentials/language into keys; one guarded size/mtime snapshot. Error/cross-context/eviction fixtures. |
| W21 High | Invalid/missing/changed policy files silently weakened safety or remained cached, including string booleans and NaN thresholds. | Strict known fields/types/finite ranges, explicit missing-file error, mtime/size invalidation and early CLI validation. Preservation-off options unsupported by implementation fail explicitly. |
| W22 Medium | Missing values became literal `None`; unsupported values/numeric NaN and impossible dates could reach tags; writer CLI rejected uppercase inputs or reported failure after successful picture-only write. | Empty missing values, reject unsupported/nonfinite values and invalid incoming dates; case-insensitive inputs and matching write-validation contract. Writer/PCM/CLI fixtures; QC validates calendar components. |
| W23 Medium | Credential diagnostics used file values instead of exported configuration, assumed AcoustID HTTP success authenticated a key, and provider error logs could expose URL/header secrets. | Environment precedence, parsed diagnostic statuses, bounded diagnostic body reads and redaction including OAuth/Basic; configuration/auth payload and cover error tests. |

Subprocess timeouts also bound fingerprint, PCM/BPM/peak/DR, gain and verifier
commands. This bounds waiting; it does not certify runtime/resource performance
on every recording or external tool. No performance speedup is claimed.

## Acquisition and failure matrices

The detailed lyrics/artwork matrix remains in
[SELECTION_AUDIT.md](SELECTION_AUDIT.md#provider-acquisition-matrix). Its
24 lyrics provider rows and 11 artwork provider rows document search/direct-ID
routes, retained forms, credentials, bounded limits and fixture/live status.
Those suites are included in this audit's full rerun. No provider was removed
or disabled to make tests pass. Pre-existing unsupported diagnostic adapters
remain explicitly unsupported. Raw/native TTML, enhanced LRC, translations,
romanization and candidate alternatives retain the existing archival contract.

| Additional provider/evidence route | Acquisition and retained evidence | Failure/fallback; verification |
|---|---|---|
| Discogs | Physical/catalog/barcode search, known IDs and MB URL relations; detail tracklist/duration/edition/credits/art representations; 50 scored candidates, visible truncation | Missing/failing detail excluded, ties review; credential + shared paced bounded JSON transport; new offline physical/bridge fixtures, no authenticated live lookup |
| MusicBrainz | Release/recording/direct Disc ID and bounded search; release versus group IDs, media/track/recording IDs and credited display names | Synthetic TOC stays weak; recording conflict is not edition certainty; malformed layout/count/score conservative rejection; offline identity fixtures, no live collection write |
| AcoustID | Fingerprint lookup with documented `recordingids releases`, returned confidence, recording and release IDs | Reject nonfinite/low scores, retain confident alternatives; finder bridge limited to 20 release IDs; fingerprint/API failures leave other discovery routes; offline fixtures |
| AcousticBrainz | Recording-ID low/high legacy dataset, BPM/key/model probabilities | Independent model validation and bounded JSON/cache; valid BPM survives malformed optional models; fixture compatibility, service availability unverified |
| DR database | Artist/album search and bounded HTTP timeout, diagnostic edition comparison | Does not supply authoritative local DR/edition tags; external HTML/live schema unverified; local measurements and report mapping independently tested |

| Failure phase | Actual outcome / tested limit |
|---|---|
| Provider auth/block/rate-limit/transient/parser/empty | Shared typed statuses and cache policy; error envelope is INVALID_RESPONSE, not FOUND. API-specific error bodies are not universally mapped to authentication subtypes. Other independently acquired candidates remain usable. |
| Bad sync versus valid plain / missing richer form | Existing acquisition and selection regressions retain plain/native forms and select eligible text; malformed timing does not become fabricated TTML. |
| Winning image/lyric materialization fails | Existing real-wiring regressions retry other eligible candidates privately; publication filesystem errors abort transaction, rather than relax identity. |
| Original image fails / thumbnail works | Existing decode-fallback fixtures retain actual URL, representation kind and decoded dimensions. |
| Every remote fails / local only / user cancellation | Existing local preservation, interactive/category/quiet/non-TTY/dry-run fixtures; one remaining category does not replace a different eligible automatic winner. |
| Mid-publication exception/interruption/collision | New track/writer/ledger/late-report injection plus existing failed-rollback tests; restore originals where possible, preserve recovery data on recovery failure. |
| Process kill/power loss | Not catchable; no automatic recovery journal. Readers may see mixed files. Manual review/recovery from retained transaction trees is required. |

## Validation record

- Baseline `.venv/bin/pytest -q`: **521 passed in 109.04 s**.
- Initial new regressions: 15 failures before fixes, then 15 passed. Later
  defect-specific batches also reproduced failures before correction where
  practical. Some added tests verify adjacent existing contracts rather than
  claim new defects.
- Relevant intermediate `.venv/bin/pytest -q test_entire_audit.py
  test_pipeline_safety.py test_regression_repair.py`: **222 passed in 29.80 s**.
- Full intermediate run: **558 passed, 2 failed in 106.07 s**; failures were a
  new ledger-backup cleanup regression and an outdated mocked HTTP body/score,
  both corrected. Next full run: **572 passed in 83.71 s**; further final
  CLI/cleanup/version cases and fixes followed.
- Final `.venv/bin/pytest -q`: **576 passed in 89.29 s**.
- Final focused `.venv/bin/pytest -q test_entire_audit.py`: **55 passed in
  1.60 s**. These cases include temporary real FLACs and PCM preservation,
  cross-module wiring, controlled provider responses and failure injection.
- `.venv/bin/ruff check .`: **passed**.
- `.venv/bin/ruff format --check .`: **39 files already formatted**.
- `.venv/bin/mypy .`: **no issues found in 33 source files**.
- `.venv/bin/python -m pip check`: **no broken requirements found**.
- `git diff --check`: **passed**.
- CLI smoke commands `.venv/bin/python <script> --help` for
  `audiophile_tagger.py`, `audiophile_cover_fetcher.py`, `metadata_writer.py`,
  `verify_all_flacs.py` and `discogs_release_find.py`: **exit 0**.

## Remaining uncertainty and external limits

- No missing production source blocked this audit. Historical Queen-dependent
  tests require adjacent local assets not shipped by Git; they were present here.
- No live authenticated lyric/art/Discogs lookup was certified. Scraping/private
  schemas, newer language/timing routes, all provider pagination, genuine content
  completeness, and arbitrary image packaging/crop/upscale heuristics remain
  unverified. Existing LRCLIB `lyricsfile`-only YAML consumption remains absent;
  rich item JSON is retained, but this is not full acquisition support.
- Exclusive new-artifact publication assumes same-filesystem hard links. The
  staging tree is on the album filesystem; unsupported filesystems abort safely.
  Cross-filesystem/Windows behavior was not tested. Advisory POSIX album locks
  do not exclude other programs or coordinate standalone writer processes.
- Abrupt kill/power-loss durability, directory fsync, automatic journal recovery,
  a second interruption during rollback and unrelated concurrent edits of
  existing FLACs/ledgers remain untested. Do not describe these commits as atomic.
- Explicit HTTP credential/language headers are part of shared JSON cache keys.
  Session-cookie/server `Vary` interactions and cross-process cache/rate-limit
  coordination are not comprehensively verified. Requests sessions still cross
  worker boundaries; no universal thread-safety guarantee is claimed.
- Native DR/BPM/peak helpers still decode full audio into memory. High-resolution
  long recordings and nested executors were reviewed but not benchmarked; memory
  pressure/performance and proprietary-meter equivalence remain uncertain.
- Alternate legacy encodings, arbitrary malformed upstream dictionaries,
  mixed-release folder layouts, every optional config combination, tools/platforms
  and live identification on rare editions remain outside fixture certification.

Primary references checked: [MusicBrainz API](https://musicbrainz.org/doc/MusicBrainz_API),
[AcoustID lookup](https://acoustid.org/webservice), and
[upstream DR14 implementation](https://github.com/simon-r/dr14_t.meter/blob/master/dr14tmeter/compute_dr14.py).
These support contract/algorithm review, not live provider availability or a
claim that all possible defects have been eliminated.
