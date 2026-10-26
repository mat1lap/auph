# Search feature verification — 2026-10-08–09

Implemented the query → scoped Jackett discovery → authorized topic details →
eligibility/deduplication → CSV → optional evaluation → manual selection and
specific confirmation → trusted resolution → qBittorrent workflow.

Existing unrelated audit changes remain in the working tree. Feature changes
are new search modules/tests/docs, `SearchPolicy` and its typed loader, a small
early `search` dispatch in the existing CLI, and an appended README entry. The
existing enrichment path and global policy file were not rewritten. No real
music files were enriched and no real torrent downloads were submitted.

## Executed checks

| Command | Outcome |
| --- | --- |
| `.venv/bin/pytest -q test_release_search.py` | **82 passed in 2.01s** |
| `.venv/bin/pytest -q` | **787 passed in 126.88s** |
| `.venv/bin/ruff check .` | All checks passed |
| `.venv/bin/ruff format --check .` | 59 files already formatted |
| `.venv/bin/mypy .` | Success; 48 source files checked |
| `.venv/bin/pip check` | No broken requirements found |
| `git diff --check` | Passed, no output |
| `.venv/bin/python audiophile_tagger.py search --help` | Exit 0; search help displayed |
| `.venv/bin/python release_search.py --help` | Exit 0; standalone help displayed |
| `load_config('search.example.toml')` + `validate_policy(...)` via `.venv/bin/python` | Example parsed and validated; no guessed models |

The initial baseline attempt was interrupted during an earlier turn; it is not
reported as a completed pass. An intermediate full run passed 761 tests; a later
checkpoint passed 773. Final results above apply to the final feature code.

## Coverage and defects corrected during implementation

External HTTP boundaries use sanitized deterministic fixtures. Matching,
normalization, ranking, validation, CSV output, confirmation and receipts run
through production code. The main integration regression calls the actual
`audiophile_tagger.main(["search", ...])` with a temporary policy, simulated
terminal, multiple search results, topic HTML, LLM response, download resolution
and mocked qBittorrent HTTP replies, then reads CSV and receipts independently.

Focused regressions first reproduced these defects, then passed after fixes:

| Trigger | Root cause and correction |
| --- | --- |
| Empty topic label immediately followed by another field | DOM joining consumed the next label as artist identity, falsely rejected the work and promoted a missing field to stated evidence. Field boundaries now remain separate; usable title/codec fields and tentative summary fallback survive. |
| Custom credential environment-variable name lacks KEY/TOKEN/COOKIE | Generic name-based scrubbing missed a topic echo of the configured cookie. Acquisition/parsing now receive policy-specific secret values explicitly, and evaluation queries use the same policy-aware redaction. An actual workflow export regression verifies removal from fields and retained evidence. |
| Dispatched search help omitted the `search` token | The standalone parser inherited the outer executable name without its subcommand. The caller now supplies the actual command prefix; a subprocess regression checks production help while standalone help retains its own entry point. |
| Album filter receives a stated studio/live album subtype | Exact raw-string comparison rejected valid albums. Primary-type comparison now recognizes those subtypes while version gates also inspect the stated type, so an unrequested live release remains rejected. |
| CSV output points at audio, a document or an unrelated inventory | Unrestricted destination replacement could destroy unrelated data. Export now requires a `.csv` target and checks existing search-export headers before replacement; regression fixtures confirm original bytes remain intact. The write-failure test now starts from an actual previous search CSV. |
| Nested LLM candidate/message is a scalar/null | `.get()` assumed dictionaries and escaped the fallback handler. Backend adapters now validate nested shapes and convert malformed responses to safe evaluation failure/manual fallback. |
| UTF-16 XML containing a DTD | Byte substring screening missed NUL-interleaved markup. NUL-containing encodings are refused before XML parsing along with DTD/entity declarations. |
| Malformed topic URL or thousands of numeric digits in one search row | URL parsing / integer conversion escaped the per-item acquisition path. Invalid topic URLs are skipped; bounded numeric claims become unknown without discarding other evidence/topics. |
| CSV write fails while replacing an earlier export | Direct destination writes destroyed the previous complete output. Temporary-file publication under the existing cross-process file lock preserves the previous CSV on write failure. |
| Current Jackett proxy returns a magnet redirect | Generic redirect refusal discarded a supported representation. Only the trusted proxy's 302-to-magnet is parsed locally and validated; HTTP redirects are never followed. Tests now use the signed-query URL produced by current Jackett source. |
| Multi-disc topic contains CD1/CD2 numbered lists | A non-numbered medium heading ended parsing after disc one. Recognized disc headings now survive acquisition and CSV, repeated positions remain separate, and counts explicitly describe listed entries, not a verified release total. |
| Rip-log field explicitly says “No log available” | String presence received a rip-evidence preference and allowed an unsupported model reason. Shared evidence eligibility now excludes explicit absence/unknown claims for both fallback ordering and reason validation, retaining original claims in CSV. |
| Processing claim says “Not converted from MP3; no fake upsampling” | Keyword-only rejection treated denials as admissions. Negation is scoped to clauses; a separate positive conversion clause still rejects. This is limited textual evidence processing, not verification of actual audio provenance. |

Additional coverage: pagination beyond the first result; later-page failures;
repeated pages and visible bounds; separate topic editions; wrong work/type and
partial/processing claims; conflict/missing detail fields; Russian encoding and
Unicode; explicit versus inferred identity; provider-order-independent preferences;
CSV quoting/formula/secret handling; all three backend contracts; schema downgrade;
invented IDs/evidence and prompt-injection strings; missing configuration/model
and remote-disclosure gates; rate limits/size limits; malformed torrents and paths;
valid magnets and unsupported external source parameters; declined/EOF/non-TTY
confirmation; duplicate prevention; interrupted/uncertain outcomes; concurrent
client writers; and a successful add response without client visibility.

A separate **real HTTP transport** test runs against a loopback stub Web API.
It verifies actual Requests cookie persistence, Origin, multipart torrent bytes,
client readback and duplicate prevention. The stub never downloads anything.
This establishes transport wiring, not real qBittorrent interoperability.

## Integration contracts and external limits

| Integration | Route/retained evidence | Offline evidence | Live status |
| --- | --- | --- | --- |
| Jackett/RuTracker indexer | Indexer-specific Torznab; topic IDs/public URLs, original title claims, size, peers, publication text; bounded offset/limit | Search/detail/confirmation integration and signed-query download fixtures | Not configured: no Jackett URL/key available |
| Authorized RuTracker topic | Separate `.post_body` request using configured Cookie; explicit identity, edition, codec, track list, rip/processing/completeness/artwork claims | English/Russian HTML, cp1251, malformed/auth pages and independent topic fallback | No authorized topic cookie available; template breadth unverified |
| OpenAI | Configured Chat Completions endpoint/model; normalized evidence only | Schema/JSON contracts, validation, malformed response and manual fallback | No API key or explicit model configured |
| Gemini | Configured `generateContent` API/model; normalized evidence only | Native request/response contract and structured validation | No API key or explicit model configured |
| Local LLM | Explicit OpenAI-compatible Chat Completions URL/model/capability mode | Compatible request contract and local validation | No local endpoint/model configured |
| qBittorrent | Dedicated cookie login, hash listing, multipart v1 torrent or validated magnet add; receipts | Mocked workflow, failures, duplicates, locks and real transport to loopback stub | No real client URL/credentials configured; no real submission attempted |

Credential/configuration inspection printed **presence only**, never values.
Primary API contracts and Jackett source were checked; their links are in
[SEARCH.md](SEARCH.md). No authenticated service compatibility is inferred from
fixture passes. Topic fields are stated claims, not measured audio quality;
exact mastering superiority cannot be established from this data.

Remaining checks require configured, authorized services: read-only Jackett
search/topic retrieval, an available configured LLM model, and user-confirmed
submission to an actual client. Complex topic templates, unusual release naming,
complex processing-language negation, v2/hybrid torrents, and arbitrary local
LLM protocols are not claimed supported. Interprocess locks are POSIX, and
receipts do not establish distributed atomicity or physical power-loss durability.
No enrichment is automatically attached to downloads.

Setup, examples, preferences, credentials, bounds, confirmation and receipt
recovery are documented in [SEARCH.md](SEARCH.md) and [search.example.toml](search.example.toml).

## Live topic-detail remediation — 2026-10-09

This section supersedes the earlier Jackett/RuTracker availability statements:
API credentials and the topic cookie are configured, and the existing local
search policy and FlareSolverr service are available. The policy must be selected
explicitly; this tool shell did not inherit `AUDIOPHILE_CONFIG`.

Detailed triggers, fixes, coverage, live evidence and limitations are recorded in
[RUTRACKER_DETAIL_DEBUG_2026-10-09.md](RUTRACKER_DETAIL_DEBUG_2026-10-09.md).

Final checks against the completed code:

| Command | Actual result |
| --- | --- |
| `.venv/bin/python -m pytest -q test_release_search.py test_search_details.py` | **128 passed in 2.23s** |
| `.venv/bin/python -m pytest -q` | **833 passed in 140.30s** |
| `.venv/bin/ruff check .` | All checks passed |
| `.venv/bin/ruff format --check .` | 61 files already formatted |
| `.venv/bin/mypy .` | Success; 49 source files checked |
| `.venv/bin/pip check` | No broken requirements; harmless unwritable pip-cache warning |
| `git diff --check` | Passed, no output |
| `.venv/bin/python audiophile_tagger.py search --help` | Exit 0 |
| `load_config(...)` + `validate_policy(...)` | Both example and existing local search policy validated |

Full/focused suites were run outside the sandbox to allow the existing loopback
HTTP integration fixture. No test failures were skipped or assertions weakened.
An original-source temporary copy reproduced 12 failures in the new title/challenge
regressions; the new module now contains 46 passing regressions.

The original configured live search exported 25 topics, all blocked by Cloudflare.
The final bounded live CLI search exported one `FOUND_PUBLIC` topic to
`/tmp/queen-jazz-final-diagnostics.csv`. Its independent public browser response
contained the correct topic post; the authenticated HTTP request still returned
403 with `cf-mitigated: challenge`. Browser-reported status 200 is synthetic in
upstream FlareSolverr and is not reported as measured topic HTTP success. Cookie
validity and unsupported topic fields remain unknown. Earlier browser probes also
hit 30-second challenge timeouts; consistent future availability is unproven.

The final integration sends no configured cookies or API keys to FlareSolverr.
Initial cookie-bearing probes were retained by its existing INFO-level request
logger, confirmed by a presence-only check. Existing container logs were left
untouched. No cookie values, tokens or authenticated headers were printed or put
in fixtures/CSV/documentation. No service restart, real submission, music-file
write or new-cookie request was performed.

## Two-stage refinement — 2026-10-09

Current default is targeted title filtering and at most five detail candidates,
with retained ambiguous/budget-skipped rows and a 180-second request-start deadline.
Full-detail mode is explicit. The newer implementation, fixtures, live timings,
cache repeat-run evidence and final **873-test** verification are recorded in
[SEARCH_STAGES_VERIFICATION_2026-10-09.md](SEARCH_STAGES_VERIFICATION_2026-10-09.md).
Earlier tables above describe their historical checkpoints, not the complete current
behavior. No paid LLM calls or real torrent submissions were made in this refinement.

## Isolated Gemini diagnostics — 2026-10-09

See [GEMINI_VERIFICATION_2026-10-09.md](GEMINI_VERIFICATION_2026-10-09.md)
for the two-call live allowance, observed timeouts, corrected diagnostic contracts
and current checks. Earlier "no paid LLM calls" statements apply only to their
historical refinement; this isolated check made two authorized generateContent
attempts. Their timeout outcomes do not verify model/authentication compatibility.

Final Gemini-diagnostic revision checks: `.venv/bin/pytest -q` — **909 passed**;
related search/Gemini suite — **204 passed**. Ruff lint/format, mypy, pip check and
git diff whitespace checks passed. Full command/evidence record is in the linked
Gemini report; no successful live model response is claimed.
