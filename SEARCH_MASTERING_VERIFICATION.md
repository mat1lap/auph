# External mastering preference — offline verification, 2026-10-12

## Scope and implementation

Inspected current `SEARCH.md`, `release_search.py`, `search_models.py`,
`search_providers.py`, `search_evaluator.py`, `SearchPolicy`, cache/rate-limiter
contracts and `/tmp/queen-jazz-gemini.csv`. No collection files were enriched and
no real torrents submitted. Existing unrelated work and `search.local.toml` were
preserved. No commits were made.

New `search_research.py` supplies a replaceable bounded public-research adapter,
Brave Web Search transport, XenForo post extraction (quotes removed), source
identity/provenance, edition matching and strict `mastering_review` validation.
Production workflow now joins it after torrent details and before final evaluation.
Gemini, OpenAI and local adapters share the expanded evaluation contract.

General album research precedes queries containing known candidate edition
identifiers. This is not an unbounded survey: currently 3 queries/5 pages by
default, with explicit count limits, 600-character/75-word query bounds, 10 search
results/query, 1 MB/page and 12 post excerpts/page. Search snippets are preserved;
failed/unrecognized/unfetched pages cannot support a confirmed preference.
Failures retain discovery/CSV/manual paths. Rate limiting of search does not
prevent retrieving independent forum pages already discovered.

The main decision is preference by **found subjective reviews**, including
counter-preferences, exact source quotations and reported comparison type.
Availability/rip criteria belong to a separate practical selection. It can differ
from the preferred mastering or abstain independently. No recommendation provides
automatic selection/confirmation/submission authority.

Catalog plus medium is only a possible match. Matrix/mastering-credit agreement
can support stated edition identity; these remain claims, not proof of unique
mastering or audio/rip equivalence. Every candidate retains matched/missing/conflicting
features. Missing identifiers and a known absent edition are different outcomes.
All model IDs, URLs, quotes and identifiers must refer to saved evidence; tokens
from an alternative edition cannot move into the preferred edition descriptor.
Explicit recognized counter-preferences cannot be silently omitted. This narrow
English guard does not understand every kind of disagreement.

## Confirmed defects addressed

- Real CSV `rutracker:6187670` flagged Holland/Netherlands and LP/LP, Album,
  Gatefold as conflicts. Root cause: plain normalized-string equality. Comparison
  now canonicalizes these equivalent country/source-medium claims while retaining
  original title/description provenance and real conflicts.
- Nested CSV string redaction stripped public forum source URLs. Regression used
  real evaluator → workflow → CSV readback and failed until export preserved only
  canonical, retained evidence URLs in the two research structures. Query-bearing,
  credential-bearing, control-character and arbitrary-fragment URLs stay refused.
- The old recommendation contract could represent only practical torrent reasons,
  so external mastering arguments could not survive validation. A separate strict
  evidence-linked review contract now preserves them without weakening existing
  practical reason/ID/measurement checks.
- Initial integration review caught repeated copies of research sources in every
  candidate's model payload. They are exported per CSV row but sent to the model
  once in the research context, avoiding payload multiplication.
- Research cache write failures are optional diagnostics, not causes to discard
  successful acquisition. Corrupt/incompatible entries are refetched. No transient
  errors or inaccessible post pages are cached as successful post evidence.

## Offline evidence

Synthetic forum HTML is intentionally **not** a real Steve Hoffman discussion or
an opinion about Queen Jazz. It has two same-author preferences, a quoted repeat,
a second author's non-comparison, and a conflicting listener preference. Tests
cover exact/possible/unsupported edition matches, multiple matrices with one
catalog, an absent preferred edition, no common preference, snippet-only abstention,
forged source/candidate IDs/URLs/quotes/credits, prompt-injection data, wrong typed
response fields and invented direct-comparison claims.

HTTP-boundary fixtures cover search/page failure, 403/429/503, Retry-After,
sequential bounded requests, cache reuse/write failure, query/page budgets,
unconfigured/undisclosed search and preservation of discovered sources. Adapter
fixtures cover all three existing LLM backends; no paid requests are involved.

The integration regression runs real Jackett parsing, candidate relevance, real
topic detail parsing, public research adapter, Gemini evaluator, CSV export and
submission gating. Preferred candidate 1 and practical candidate 2 remain separate.
Non-TTY refusal occurs before torrent resolution/client login. Another production
workflow case keeps CSV/manual selection after an invented model ID and requires
exact confirmation; declining sends nothing. CSV-only explicitly disables research.

`/tmp/queen-jazz-gemini.csv` is evidence of prior acquisition, not fresh external
research. Its warnings confirm the equivalence defect; it does not establish a
preferred mastering. No album-specific rules were added.

## Checks

- `.venv/bin/pytest -q test_search_research.py`: **44 passed**, 2.55 seconds.
- `.venv/bin/ruff check .`: passed.
- `.venv/bin/ruff format --check .`: passed, 73 files already formatted.
- `.venv/bin/mypy .`: passed, 57 source files.
- `.venv/bin/pip check`: no broken requirements.
- `git diff --check`: passed.
- `.venv/bin/python audiophile_tagger.py search --help`: passed.
- `fish --no-execute /tmp/auph-mastering-example.fish`: documented fish example parses, no requests executed.
- Loading `search.example.toml` and `validate_policy(...)`: passed.
- `.venv/bin/pytest -q`: **983 passed**, 156.19 seconds (final code and all 44 new tests).

## External limitations and configuration

**Zero live search or LLM API calls, zero downloads.** The local policy currently
has `research_backend="none"` and `research_disclosure=false`; it was not edited.
To enable real research configure Brave with its own authorized API key and
separate disclosure opt-in, then use the existing model/disclosure configuration.
Setup and exact fish commands are in `SEARCH.md`/`search.example.toml`.

Brave endpoint/parameters/authentication were checked against primary documentation:
https://api-dashboard.search.brave.com/app/documentation/web-search
This is a documented wire contract, not live authenticated compatibility evidence.

Live checks still needed (with a separately authorized budget): Brave response and
access/quota, current public forum DOM/access, expanded production evaluation on
configured Gemini/OpenAI/local models, and human review of linked discussion
context. Unrecognized forum layouts stay snippet-only. Short excerpts/count budgets,
unknown matrices/credits and conservative work-context matching can force abstention
or omit useful reviews. Distinct usernames do not prove independent authors;
independence, consensus and mastering superiority are not established by these tests.

## Serper implementation and authorized live check — 2026-10-12

This section supersedes the previous **local configuration** statement above; the
historical offline checks remain historical. Added `research_backend="serper"` via
[official Serper Search API playground](https://serper.dev/playground), POST
`https://google.serper.dev/search`, `X-API-KEY`, `{q,num:10}`, `organic` results.
The publicly served official playground client confirms these contract fields;
this is not a guessed Google endpoint. Brave remains a separate GET adapter.
Backend/endpoint-sensitive normalized cache keys prevent cross-backend reuse.
No retries on search/pages; 401/403/429 search responses stop subsequent searches.

Search provenance now distinguishes `album`, `exact_catalog` (quoted identifier),
and `expanded_edition` (explicitly omits catalog). Expanded discovery cannot inherit
exact-catalog evidence. Query-budget omissions are warned. Summary/post-excerpt
mention checks are signals, never verified identities. All previous strict
candidate/source/URL/quote/edition rules and submission gates remain unchanged.
Search-result dates are retained where supplied; snippets remain preliminary.

### Live observations (not fixtures)

Used all 25 saved rows from `/tmp/queen-jazz-gemini.csv`, retaining their supplied
fields/evidence and five eligible releases. No Jackett/RuTracker requests or new
edition assertions. A temporary harness reconstructs dataclass values only for
this check; no production CSV importer was added. Query and page counters were
incremented and persisted **before** each request. Independent budget guard
allowed only Serper, configured public forum host, and one Gemini generation;
all requests `retry=False`. Caches were isolated under the report directory.

Actual calls: **4 Serper searches, 5 public forum page requests, 1 Gemini generation**.
Search HTTP 200 durations: 2.56s album-wide, 0.80s `"UICY-40256"`, 0.73s
`"5C 062-61820"`, 1.20s `"00602547202741"`. Album-wide search retained 10 public
results; all three exact catalog searches returned **zero organic results**.
`P-10601E` and expanded searches were not reached within the four-call budget.
Empty exact searches do not prove the editions are absent from forum discussions.

Real discovered pages include:

- https://forums.stevehoffman.tv/threads/rolands-favorite-cd-versions-of-queen-albums.141208/
- https://forums.stevehoffman.tv/threads/queen-catalogue-comparison-results.708601/
- https://forums.stevehoffman.tv/threads/vinyl-queen-jazz-recommendations-please.969897/
- https://forums.stevehoffman.tv/threads/queen-on-vinyl-list-your-favorite-pressings.120642/
- https://forums.stevehoffman.tv/threads/best-sounding-queen-cds.748515/page-2

All five attempted public pages returned **HTTP 403, Cloudflare challenge** in
0.14–0.24s. No bypass attempted. Other pages were marked `SKIPPED_BUDGET`.
Research is therefore `SNIPPETS_ONLY`; no post context, author independence,
contradictory-opinion completeness, preferred edition or mastering was verified.
Search snippets mention editions such as EMI `CDP 7 46210 2` and US Elektra
`6E-166`; these are unverified leads, not validated preferences or exact matches
to the candidates. Saved candidates include Japanese `UICY-40256` CD,
Netherlands `5C 062-61820` LP, two claims of `00602547202741` LP (Europe/UK), and
Japanese `P-10601E` LP. The two shared catalog claims are not proven identical
masterings/rips. No supported preferred-edition mapping can currently be made;
“preferred edition absent” would also be unjustified.

The sole final production `GeminiEvaluator.evaluate(..., research=...,
allow_schema_fallback=False)` used current **gemini-3.5-flash-lite**, temporary
60-second read timeout (5-second connect), existing configured output/mode, no
model/config changes. HTTP **200 in 3.419356s**, then strict local rejection:
`LOCAL_VALIDATION_ERROR`, `validation_code=EVIDENCE_REFERENCE` (a reason references
absent/unrelated candidate evidence). It is **not a successful recommendation**.
No corrective ID/evidence substitution or further API request was made. The
provider response was not retained; its exact offending pair cannot be reconstructed
from the safe code alone. CSV/manual fallback remains available. No sound-quality
claim is made from this failed evaluation.

Safe diagnostics, source snippets, CSV and counters are retained under
`/tmp/auph-serper-validation-2026-10-12/` (report JSONs mode 0600), without keys,
cookies, headers, raw HTML or raw LLM bodies. `queen-jazz-serper.csv` preserves all
25 rows and evaluation failure diagnostics. These files are temporary, outside Git.
The harness refuses further calls with its exhausted counters; don't rerun it to
attempt new API access without a new budget.

### Configuration and verification

Only three settings changed in existing `search.local.toml`: `research_backend`
`serper`, `research_key_env` `SERPER_API_KEY`, `research_disclosure` true.
All other parsed configuration values were compared and preserved, including
`gemini-3.5-flash-lite`. The `.env` key was loaded without printing it.

New offline tests exercise Serper POST/auth/organic normalization, exact versus
expanded provenance, cache hits/backend separation, malformed/empty responses,
wrong-identity nonempty results, and 429 without retries retaining Retry-After.
Existing tests cover strict recommendations, injection, absent editions, dissent,
CSV/manual fallback and explicit confirmation. These fixtures do not establish
live forum access or accepted live LLM evaluation.

Final targeted verification: `.venv/bin/pytest -q test_search_research.py` —
**53 passed, 4.15s**. The real-boundary integration test is parameterized for both
Brave and Serper (including CSV and non-TTY submission refusal). A malformed cached
title now triggers bounded refetch instead of breaking content checks.
Independent CSV readback: 25 rows, five eligible, all retained sources and
`EVIDENCE_REFERENCE` present. No extra network calls were needed for readback.

Static checks: `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`
(73 files), `.venv/bin/mypy .` (57 source files), `.venv/bin/pip check`,
`git diff --check`, production CLI `search --help`, and `fish --no-execute -c`
on the documented command passed. Full-suite results are recorded below after
final verification. No files were staged or committed; unrelated user changes
were preserved.

Final `.venv/bin/pytest -q` on the completed implementation: **992 passed,
146.59 seconds**. Earlier intermediate full runs passed 988 and 991 tests;
the final count includes all nine additions relative to the original 983-test
baseline. `git diff --check` was repeated after this report update and passed.
Remaining live checks: accessible public forum post context, verified edition
identifiers and an accepted strict LLM recommendation. Search snippets and the
HTTP-200-but-rejected evaluation do not close those gaps. No more calls were
made after the authorized budget was exhausted.

## Public-context/evidence follow-up — 2026-10-12

### Original rejection and contract review

The previous directory contains diagnostics, search rows and snippets, **not** the
Gemini response. The original specific invalid reference cannot be recovered.
`EVIDENCE_REFERENCE` means a practical reason used a field absent from that
candidate's evidence or incompatible with its preference. It does not by itself
identify a forum quote problem. No past response was reconstructed.

Confirmed contract/coverage gaps closed:

- Generic schema enumerated a union of evidence keys, while validity is per
  candidate/preference. The existing allowed-reasons table and validator were
  consistent, but the response contract did not express all pair restrictions.
  Input now adds explicit flattened permissible practical reason options and
  disjoint forum-source/practical-field namespaces; semantic validation stays local.
- Practical warning codes and forum limitation codes are explicitly separated.
  Known invalid pairs/cross-namespace codes receive safe `validation_details` in
  checker/CSV diagnostics; arbitrary model prose/IDs are not echoed.
- Snippet/unknown scope cannot masquerade as a read post merely through FOUND.
  Neither preference nor `no_common_preference` can be established from unread
  snippets. Adjacent context cannot independently support a preference/opposition.
- `/goto/post?id=2427498` previously failed blanket query rejection. A narrowly
  constrained numeric-id route is now admitted; other query URLs remain refused.
  Every direct redirect target is validated before request; loops/hop counts and
  shared fetch budget are enforced. Original/final links survive nested CSV export.
- XenForo 1 original `blockquote.messageText` bodies were being stripped as
  quotes. Original bodies now survive while actual quotes are removed.
- Post cap no longer precedes album relevance. Requested album mentions and
  labeled neighbors are selected in general artist threads; literal artist/album
  context no longer incorrectly requires contiguous words in the page title.
- Comparison/edition and album-specific pages precede song polls. Cached usable
  context remains available after network budget exhaustion. Work-specific page
  cache identity prevents context reuse for a different requested album.

Public browser reading is opt-in `research_page_transport="flaresolverr"`, with
optional forum-only `research_flaresolverr_url`; otherwise the existing configured
solver is reused. Official API contract:
https://github.com/FlareSolverr/FlareSolverr#usage
Only cmd/url/maxTimeout are sent; no cookies, authentication, browser sessions,
search/model keys or CAPTCHA configuration. Challenge/login-only/non-post HTML is
not accepted as useful evidence. The API exposes no redirect chain: changed final
URLs are refused; unchanged final URLs cannot attest absence of intermediate browser
navigation. `/goto/post` resolves directly. Browser navigation chain completeness
remains an explicit limitation, not a verified guarantee.

### Actual bounded observations

Reused the 25 candidates and four normalized Serper cache records. **Zero new
Serper requests**, zero Jackett/RuTracker requests. The original raw Serper response
was not retained. The new Serper fixture is explicitly labeled as reconstructed
from real normalized rows plus the user's supplied goto URL, with no invented
heading/snippet for that link; it is not presented as a new live organic result.

Exactly **five** sequential FlareSolverr request.get attempts and **two** Gemini
calls; counters persisted before submission, retries/schema fallback disabled.
No further calls after exhaustion. No downloads or music writes.

Public URLs attempted (service HTTP 200 durations):

1. https://forums.stevehoffman.tv/posts/17184709/ — 13.80s
2. https://forums.stevehoffman.tv/threads/best-sounding-queen-cds.748515/page-2 — 12.49s
3. https://forums.stevehoffman.tv/threads/queen-catalogue-comparison-results.708601/ — 12.24s
4. https://forums.stevehoffman.tv/threads/queen-half-speed-vinyl-masters-from-wal-mart.1171291/ — 12.20s
5. https://forums.stevehoffman.tv/threads/queen-on-vinyl-list-your-favorite-pressings.120642/ — 12.26s

Returned HTML had recognized message/body counts of 25, 25, 25, 10 and 25
respectively (see retained structure reports). **Validation-harness defect:** the
script extracted HTML for structure inspection, then accidentally returned that
HTML instead of the solver JSON envelope. Production JSON parsing consequently
reported INVALID_RESPONSE. These are NOT five verified Cloudflare failures, nor
five successfully validated post contexts. The harness has been fixed to return
its original bytes. The original envelopes/HTML were not retained, so final URL
validation and actual album-focused post excerpts cannot be retrospectively
verified. No extra page request was made to conceal or repair this evidence gap.
The exact selected-page ordering was subsequently improved offline; that changed
priority order was not live-replayed under the exhausted page budget.

Gemini call 1: current gemini-3.5-flash-lite, HTTP 200, **3.782712s**, local
`WARNING_CODES`. Its specific offending code was not retained; it cannot be
reconstructed. Namespace separation was then made explicit in input/prompt; no
new accepted codes, ID repair, dropped reasons or validator weakening.

Gemini call 2: same model, HTTP 200, **2.879802s**, **SUCCESS after strict local
validation**. Final response:

- Mastering: `insufficient_evidence`, empty preferred edition/source IDs/candidate,
  `match_confidence="unestablished"`, `mapping_status="abstained"`.
- No claim that the preferred edition is absent among torrents; identity is unknown.
- Separate conditional practical choice: **rutracker:6797739**, with existing
  `lossless` and `seeders` evidence only. This is not a preferred-mastering or
  measured-audio-quality claim. All five eligible IDs were ranked with valid reasons.
- Root uncertainty: mastering_not_verified, technical_claims_unmeasured;
  forum limitations include limited_sources and snippet_only.

The accepted response was independently revalidated against current validators
without additional API calls. Final CSV has 25 rows, current ranks/explanations,
practical candidate ID separate from mastering abstention, and a note identifying
the harness failures. No unsafe forum preference/source claims were accepted.
Artifacts: `/tmp/auph-forum-context-validation-2026-10-12/`, including
`research.json`, `evaluation.json`, `queen-jazz-final.csv`, structure JSONs and
pre-send `counts.json`. Safe JSON reports are mode 0600. The harness now captures
minimal code-only rejected warning fixtures and anonymized practical-pair fixtures
for future failures. Existing synthetic regressions reproduce the strict
EVIDENCE_REFERENCE case; they are not reconstructions of the missing old reply.

Permanent `search.local.toml`, keys/model/billing were not changed. A mode-0600
`search.forum.toml` copy in that temporary directory changes only
research_page_transport to flaresolverr. Exact fish command and opt-in configuration
are in SEARCH.md. Future page/context verification needs a new authorized budget;
there is no remaining budget in this run. Live mastering comparison remains
unverified. The completed outcome is a validated abstention plus separate practical
advice, not a mastering preference.

### Follow-up verification

- `.venv/bin/pytest -q test_search_research.py test_gemini_contract.py test_gemini_diagnostics.py test_release_search.py test_search_details.py test_search_stages.py test_gemini_wire.py`: **312 passed, 25.55s** (before the final anonymized accepted-shape regression).
- `.venv/bin/pytest -q test_search_research.py`: **79 passed, 5.74s**, including that final fixture regression.
- `.venv/bin/ruff check .`: passed.
- `.venv/bin/ruff format --check .`: passed, 73 files.
- `.venv/bin/mypy .`: passed, 57 source files.
- `.venv/bin/pip check`: no broken requirements.
- `git diff --check`: passed.
- Production `search --help` and `fish --no-execute -c` for the documented command: passed without provider access.
- Offline independent current-validator/readback check: accepted abstention and practical ranking; 25 CSV rows.

Regression coverage includes narrow goto URL validation, permitted/refused redirects
and shared hop budget, original/final CSV links, credential-free solver envelopes,
challenge/login/captcha/wrong-origin rejection, XenForo 1 bodies, relevant posts
beyond initial cap, adjacent context, page priority, work-specific positive caches
remaining usable after fetch exhaustion, disjoint evidence and warning namespaces,
snippet-only abstention, and source scope not being promoted by FOUND. The full
Jackett → details → research → Gemini → CSV → non-TTY confirmation-refusal test is
parameterized for Brave, Serper/direct and Serper/solver; all network boundaries are
mocked. Existing confirmation/download gates remain strict.

`accepted_abstention.json` is an explicitly anonymized structural copy of the
accepted live output with synthetic candidate facts, not an original provider body
or proof of mastering quality. It allows local strict-contract verification without
further generation. The original EVIDENCE_REFERENCE reply and first new WARNING_CODES
reply remain unavailable; no specific offending token was invented for them.

Final full `.venv/bin/pytest -q`: **1018 passed, 147.13s** on the completed
implementation and all fixtures. Earlier intermediate full run: 1017 passed,
148.38s, before the final accepted-output fixture test. `git diff --check` repeated
after report completion and passed. There were no additional live calls, permanent
configuration changes, staged files or commits.

## 2026-10-12 — saved production WARNING_CODES follow-up

Input: `/tmp/queen-jazz-forum.csv`, 25 discovered candidates, five eligible;
30 retained sources: 16 post excerpts, seven adjacent-context excerpts and seven
snippets. All 23 page excerpts have `context_complete=false`. No search or forum
requests were made in this follow-up. Neither the original rejected Gemini reply
nor full forum HTML was available in the saved validation directories. The two
old `[UNRECOGNIZED]` tokens therefore cannot be identified or reconstructed.

The existing schema and validator already agreed on permitted warning enums.
The response cannot establish exactly which other vocabulary the model copied.
Confirmed defects: code diagnostics suppressed every unfamiliar token, preventing
reproduction; post parsing applied word windows twice and flattened album-list
boundaries. The supplied excerpt containing `Jazz - EMI CDP 7 46210 2` also contains
`Sound better then the MFSL` before that album item; it cannot establish that the
comparison concerns Jazz.

Changes: exact path-specific response enums, explicit separation of input warnings
from output enums, bounded redacted retention of unknown code tokens, complete
sentence/list-item extraction, omission flags and v4 page cache. Local validation
requires the album in prefer/against quotes from incomplete excerpts, and
`limited_sources` when incomplete excerpts are cited. Practical `rip_type` is
explicitly distinguished from evidence of rip quality. Mastering abstention must
not assert that an unidentified edition is absent. Validators do not repair or
accept invented codes, IDs, evidence or URLs.

One real generateContent call, current `gemini-3.5-flash-lite`, existing configured
JSON mode/output/thinking settings, command-local read timeout 60, no retries or
schema fallback. HTTP **200**, **4.010209 seconds**. Root warnings were valid:
`mastering_not_verified`, `technical_claims_unmeasured`. The entire production
assessment was rejected with **EVIDENCE_REFERENCE**: its first reason for
`rutracker:6797739` used `preference= rip_evidence`, `evidence_ids=[rip_type]`;
that candidate has no allowed rip-evidence fields. This is not an accepted
recommendation. No download or preferred mastering was selected by the validated
pipeline. The reply's mastering object said `insufficient_evidence`; that is a
model proposal within a rejected response, not separately verified success.

A minimum anonymized reproduction retaining the exact rejected preference/field
pair is `tests/fixtures/search/mastering/rejected_rip_type.json`. It is tested
against the real validator. Private mode-0600 evidence, request counter and the
25-row re-export are in `/tmp/auph-production-warning-validation-2026-10-12/`.
The larger locally redacted reply there has URLs omitted and is not a complete
replay fixture; the minimal pair fixture reproduces the actual first rejection.
`/tmp/auph_warning_validate.py` is a one-call guarded reproduction harness; its
persisted counter is exhausted. It refuses further requests. The original CSV was
preserved. No permanent configuration, keys, model, music, indexer/client access,
staging or commits were changed.

Remaining limitation: no budget remains for testing the additional rip-type prompt
clarification live. Local tests verify strict contracts; they do not prove that
Gemini will obey them. Complete context cannot be recovered without saved HTML
or separately authorized page acquisition. Correct operational outcome is
withholding a mastering preference and preserving manual selection, not claiming
that the preferred edition is absent or choosing an objectively best mastering.

Verification on the completed follow-up:

- `.venv/bin/pytest -q test_search_research.py`: **89 passed, 5.79s** (final focused regressions).
- `.venv/bin/pytest -q test_search_research.py test_gemini_contract.py test_gemini_diagnostics.py test_release_search.py test_search_stages.py test_search_details.py test_gemini_wire.py`: **322 passed, 24.59s** before the final context-sentence test.
- `.venv/bin/ruff check .`: passed.
- `.venv/bin/ruff format --check .`: passed, 73 files.
- `.venv/bin/mypy .`: passed, 57 source files.
- `.venv/bin/pip check`: no broken requirements.
- `git diff --check`: passed.

Regression fixtures exercise the actual Gemini request serializer with precise
response-field enums, unknown-code retention with credential redaction, complete
sentences and list boundaries without terminal punctuation, retained follow-on
sentence context, incomplete-source restrictions, and the actual rejected
`rip_evidence/rip_type` pair. Existing mocked workflow tests preserve CSV/manual
fallback and explicit download confirmation. Intermediate full run: 1025 passed,
149.00s; a final full run includes the later list/context regressions.

Final readback/security refinement also covers configured key variable names
without KEY/TOKEN/SECRET in their names: unknown diagnostic code tokens are
redacted using the effective policy's secret values. Final focused run:
`.venv/bin/pytest -q test_search_research.py` — **90 passed, 5.85s**.
Final full `.venv/bin/pytest -q` — **1029 passed, 149.09s**. Ruff lint/format,
mypy (57 sources), pip check and diff check passed after that refinement. Earlier
1028-pass full run predates only this last redaction regression and fix.

A future live evaluation is needed to establish compliance with the added
rip-type clarification. The authorized one-call budget is exhausted; there are
no hidden retries or accepted partial recommendations.
