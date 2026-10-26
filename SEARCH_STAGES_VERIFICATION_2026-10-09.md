# Two-stage search verification — 2026-10-09

Scope: current working-tree search modules, configuration, actual tagger dispatch,
`SEARCH.md`, `SEARCH_VERIFICATION.md`, `RUTRACKER_DETAIL_DEBUG_2026-10-09.md` and
`/tmp/queen-jazz.csv`. No applicable AGENTS.md was found in the project/ancestors.
Existing unrelated audit changes and `search.local.toml` were preserved. No commits,
real torrent submissions, enrichment writes, global configuration edits or paid LLM
calls were made.

## Findings and implementation ledger

| Reference / severity | Trigger and root cause | Resolution and evidence |
| --- | --- | --- |
| Unicode identity / high | `6224997` contains U+200E before the artist delimiter. Whitespace-dash regex skipped it and split at the year delimiter instead. | Comparison-only NFKC/format-mark/dash normalization; original title unchanged. Five separator regressions failed before implementation, now pass. Queen Mab/Bee/Cartoonists and Jazz Tour remain rejected. No production album/topic-ID exceptions. |
| Malformed/nested prefix / medium | An unclosed or nested genre group could become negative artist evidence, contrary to the conservative gate contract. | Bounded balanced-group scanner; malformed groups stay ambiguous. Three regressions failed before the fix and pass now. |
| Acquisition ordering / medium | Every discovery fetched a topic before relevance assessment: all 25 pages despite 21 rejected later. | Conservative preliminary gates before requests, ambiguous parses retained, deterministic bounded priority, opt-in LLM reorder, final merged evaluation. The exact 25-title fixture retains all rows, with 20 rejected and five eligible; only five details requested. Budget/time/full-mode tests exercise production workflow. |
| Inaccessible edition evidence / medium | Title edition annotations not retained; real DOM labels/colon/value split into separate nodes. | Explicit title MQA x UHQCD/Reissue/Remastered/country fragments and unknown-role years; DOM field joining, real label aliases, label/catalog pairs, topic-heading provenance and claim history. Five sanitized observed public-post fixtures verify catalog/country/codec/tracks through CSV. |
| Medium / rip representation / medium | `Тип рипа` and `Источник` were treated as medium; recording/distributed sample formats conflated. | Distinct `medium`, `rip_type`, `source_origin`, `recording_format`, `distribution_format`; distributed format defines exposed depth/rate when explicitly stated. No inferred CD format, WavPack mode or year roles. |
| Track list / medium | Vinyl A1/B1 and Side 1/2 entries were not recognized. | Disc/side headings and alphanumeric track positions retained, 13 entries in each observed fixture; counts do not establish complete edition. |
| Unsupported certainty / medium | Conflicting topic codecs could leave earlier title-derived `lossless=True`; cache could restore it. | Codec/lossless unknown on conflicting claims, originals retained, cache applies conflict state. Focused regression reproduced the stale true value before the fix and passed afterward. |
| Extraction diagnostics / medium | FOUND_PUBLIC confused successful transport with comprehensive evidence. | Accessible-description coverage/gaps, unmapped fields and character count; FOUND_PUBLIC is not full extraction or verified edition. Unavailable/skipped details separate from missing recognized fields. |
| Latency / medium | No stage/request timers; costly detail work before rejection; repeated successful parsed acquisition lacked cross-run reuse. | Monotonic stage/request timing, progress/counts, targeted default, opt-in versioned positive parsed-evidence cache. No new concurrency. Live results and repeated cache readback below. |
| Diagnostics regression / medium | Enabling Jackett request diagnostics could classify a legitimate RSS release title “Just a moment” as an HTML challenge. | RSS titles are treated as data; real challenge headers remain evidence. A real Jackett/HTTP regression failed before this correction and passes afterward. |
| Retry/rate handling / medium | Generic retry defaults could multiply detail/model requests. | No automatic transient retries for details or LLM POSTs; one existing schema downgrade / failed-clearance fresh attempt remain bounded. Detail rate limiting stops remaining requests, retaining skipped rows and Retry-After evidence. Failures are not negative-cached. |
| LLM scope / medium | One final-only opinion; no abstention form; title-only evidence could be overlooked. | Optional priority-only preliminary evaluation plus final evaluation, full titles and provenance in both. Validated abstention, edition-specificity evidence reason, mandatory unmeasured/mastering uncertainty. All three backend failure/invalid-ID/JSON/timeout contracts tested; CSV/manual options preserved. |
| Confirmation / preserved contract | Recommendation could otherwise be mistaken for download approval; compact output must not hide conflicts at confirmation. | Existing real CLI-to-confirmation/client integration, rejection/EOF/non-TTY, duplicate and uncertain-submission tests pass unchanged. Important warnings remain visible before confirmation; technical JSON stays in CSV. A decline regression first exposed the omission, then passed after correction. No real client submission. |

## Executed checks

Baseline before edits:

- `.venv/bin/pytest -q test_release_search.py test_search_details.py`:
  **128 passed in 2.33s**.
- First new Unicode/title/budget regression run: **6 failed**, before fixes.
- Focused stale-lossless regression failed before its fix, then passed.

Final checks:

| Exact command | Actual outcome |
| --- | --- |
| `.venv/bin/pytest -q test_search_stages.py test_release_search.py test_search_details.py` | **168 passed in 16.38s** |
| `.venv/bin/pytest -q` | **873 passed in 154.22s** |
| `.venv/bin/ruff check .` | All checks passed |
| `.venv/bin/ruff format --check .` | 63 files already formatted |
| `.venv/bin/mypy .` | Success, 50 source files |
| `.venv/bin/pip check` | No broken requirements |
| `git diff --check` | Passed, no output |
| `.venv/bin/python audiophile_tagger.py search --help` | Exit 0; new modes, budgets and opt-in LLM documented |
| `fish -n /tmp/auph-title-validation/example.fish` | Exit 0; SEARCH.md fish examples parse |

One pre-existing transport failure test now explicitly chooses `detail_mode="full"`
to retain its original two-provider-topic failure scenario. Its original request-count
and result assertions were retained; new targeted-mode tests independently establish
zero requests for explicit mismatches. Assertions were not broadly weakened. Test
boundaries replace network/pacing, not matching, extraction, selection or CSV publication.

## Observed topic evidence

No prior saved HTML for these five IDs was found. The available CSV cannot establish
what the parser missed in the original response. Bounded requests through the existing
configured public browser service acquired text-only post snapshots; fixtures remove
links, forms, scripts, uploader handles, local paths and long CUE/log bodies while
retaining original field-node boundaries and necessary reported log outcomes.

| Topic | Retained observed claims | Remaining limits |
| --- | --- | --- |
| 6024096 | Universal Music, UICY-40256, Japan, CD/FLAC, MQA x UHQCD title annotation, 13 listed tracks, EAC log text and negative AccurateRip database outcome, reported DR8 | Years 2018/1978 have unresolved roles; no verified AccurateRip success or mastering superiority |
| 6187670 | EMI / 5C 062-61820, Netherlands, LP/DSD128, separate 1/5.64MHz representation text, A/B side list, digitization chain, reported DR13 | Exact DSD semantics preserved as text; real targeted run subsequently blocked; availability varies |
| 6224997 | Virgin EMI Records / 00602547202741, Europe, LP/FLAC, 24/96, Reissue/Remastered/Embossed claims, A1/B1 positions, digitization chain | Europe is stated region, not inferred country; 1978/2015 roles not assigned; original title unchanged |
| 6797739 | Virgin EMI Records / 00602547202741, UK, LP/FLAC, recording 32f/192 versus distribution 24/192, reported DR11 | Recording/distribution distinction is a source claim, not an audio measurement |
| 6892315 | Elektra / P-10601E, Japan, LP/WavPack, 32f/192, 13 listed tracks, stated digitization chain | WavPack lossless mode and edition completeness remain unknown |

Reported DR/CRC/log text is third-party evidence, not independently computed,
authenticated or compared by listening. Full title/description claim history is
retained even when normalized fields stay unknown. No external mastering lookup
was added or invented.

## Live production run and measurements

Executed with existing authorized configuration, no LLM and no submission:

```fish
env AUDIOPHILE_CONFIG=search.local.toml .venv/bin/python audiophile_tagger.py search \
  --artist Queen --title Jazz --type album --csv-only \
  --detail-budget 5 --detail-time-budget 180 --csv /tmp/queen-jazz-two-stage.csv
```

Exit 0. **25 rows, 20 SKIPPED_MISMATCH, five eligible, four FOUND_PUBLIC, one BLOCKED**.
`6224997` is eligible and its catalog/format data survived to CSV. Authenticated
HTTP still received 403; public descriptions do not establish cookie freshness.

| Stage | Measured seconds |
| --- | ---: |
| Jackett discovery | 1.015498 |
| Preliminary filtering | 0.001145 |
| Five detail attempts, summed | 90.222998 |
| Preliminary/final LLM | Not called |

| Topic | Detail total | FlareSolverr | Direct HTTP | Parsing |
| --- | ---: | ---: | ---: | ---: |
| 6797739 | 12.704965 | 12.060539 | 0.192525 | 0.042163 |
| 6024096 | 12.312980 | 12.045523 | 0.137831 | 0.050241 |
| 6224997 | 12.704495 | 12.481311 | 0.142692 | 0.036588 |
| 6187670 | 36.306281 | 36.303784 | Not reached | Not reached |
| 6892315 | 16.194277 | 16.005818 | 0.136659 | 0.032698 |

The exact previous wall time is unknown: original CSV has no timings. The observed
improvement is **20 expensive requests avoided**, not a claimed exact speedup ratio
against the user's approximate 5–10 minutes. Serial browser work dominates this
sample; concurrent load was not increased. Earlier bounded fixture acquisitions
also had two failures around 40.5 seconds; a single fresh 15s challenge-budget
attempt for each obtained the remaining snapshots in about 12.35 seconds.
This demonstrates variable service behavior, not reliable authenticated compatibility.

### Actual repeated CLI / positive cache

Two separate production CLI processes used an isolated policy copy and positive
cache under `/tmp/auph-title-validation`, with `--csv-only --limit 1 --page-size 1
--max-pages 1`, structured Queen/Jazz filters and different CSV outputs. They did
not alter the existing local policy. Both returned `6892315`, FOUND_PUBLIC.

| Measurement | First process | Second process |
| --- | ---: | ---: |
| Jackett | 0.029917s | 0.028218s |
| Detail processing | 13.389645s | 0.001059s (cache hit) |
| Search workflow total | 13.681608s | 0.291834s |

Independent CSV readback confirmed **19 identity/detail/eligibility fields unchanged**.
The second process reused parsed evidence and did not repeat detail transport; its
saved acquisition diagnostics are explicitly historical cache-hit metadata. Jackett is measured separately: its provider/cache behavior cannot be attributed
to this project's detail cache. An earlier repeat before the final prefix refinement
also reused evidence (detail 12.821905s → 0.000864s), but had substantially different
Jackett times (7.680360s → 0.020691s); no project-wide speedup ratio is inferred. Offline tests additionally cover changed
authorization identity, corrupted entries, conflicts and non-caching of failures.

## Retained outputs and residual checks

- Live CSV/output: `/tmp/queen-jazz-two-stage.csv`,
  `/tmp/queen-jazz-two-stage-output.txt`.
- Isolated comparison/cache outputs: `/tmp/auph-title-validation`.
- Portable sanitized topic fixtures and title list: `tests/fixtures/search`.
- Test logs: `/tmp/auph-search-stages-tests.txt`,
  `/tmp/auph-search-stages-final-full-tests.txt`.

OpenAI/Gemini/local evaluator wiring was tested with mocks, not paid/live inference.
Whole-discovery detail mode, deadlines/rate limiting, ambiguous rows, budget
retention, prompt injection, invented IDs, malformed responses, provider failures,
secret handling and confirmation are offline integration evidence. A real all-topic
run was intentionally not used to duplicate costly irrelevant requests.

Remaining uncertainties: consistent authenticated access/cookie validity,
public/protected description completeness, unobserved topic templates and language
variants, true audio properties/rip authenticity/mastering quality, model judgment
and actual qBittorrent compatibility. Full mode remains bounded by discovery limits
and the chosen start deadline; it does not guarantee every tracker topic has been
found or checked. Persistent detail caching is opt-in and can retain source claims
until its TTL; use --refresh-details when fresh evidence matters.
