# Gemini verification — 2026-10-12

## Outcome and limits

No validated live recommendation was obtained. The authorized generation budget
was exhausted: **6 generateContent requests**, no retries/schema fallback, and
**1 models.list request** (of the permitted two). No Jackett/RuTracker calls,
download submissions, library writes, billing changes, TLS disabling or global
network changes occurred. Permanent local configuration was left unchanged.

The earlier 503/UNAVAILABLE cause remains unknown. Today's concrete outcomes
are a read timeout on the current model's evaluation, a model/method listing versus
404 inconsistency for an alternative, and HTTP 200 JSON rejected by the strict
local recommendation validator. These are not relabeled as successful evaluation.
The precise failed rule in the last response was not retained by the old generic
LOCAL_VALIDATION_ERROR handler; new safe rule-code diagnostics remedy that gap for
future checks. No further live call was made to recover that missing evidence.

## Current configuration and protocol

Current local model is **gemini-3.7-flash**, already changed in the user's local
configuration before this task. The historical report described gemini-3.8-flash;
this task did not change the permanent model. Endpoint remains the official
`https://generativelanguage.googleapis.com/v1beta`, JSON mode, read timeout 15 s
in the local policy. Diagnostic attempts used command-local read timeout 60 s,
connect timeout 5 s, and 2048 output tokens. This is not a 60-second total deadline.

Presence-only checks: shell key exists and takes precedence over .env; .env has
one matching GEMINI_API_KEY entry; effective key matches it and contains no
whitespace. Configured key_env names GEMINI_API_KEY. All uppercase/lowercase
HTTP/HTTPS/ALL/NO proxy variables are absent; Session.proxies is empty,
trust_env is true, TLS verification remains enabled. No key/header/body was
printed or retained in a fixture/report. No GOOGLE_API_KEY precedence is assumed:
this project explicitly reads its configured key_env, unlike Google's SDK defaults.

Official sources checked:
- [Models REST API](https://ai.google.dev/api/models): list route, pageSize (1000), supportedGenerationMethods, nextPageToken.
- [generateContent](https://ai.google.dev/api/generate-content): v1beta route, contents, systemInstruction, generationConfig, maxOutputTokens, responseMimeType and responseJsonSchema.
- [API-key authentication](https://ai.google.dev/gemini-api/docs/api-key): x-goog-api-key header.
- [Structured output](https://ai.google.dev/gemini-api/docs/structured-output): supported JSON schema properties; local semantic validation remains required.
- [Thinking](https://ai.google.dev/gemini-api/docs/thinking): model-dependent reasoning controls; generic generateContent ThinkingConfig uses thinkingLevel. The current model's exact supported-level table is not published on that page; HTTP 200 on the LOW attempt does not prove the setting was honored.

Authorized list: HTTP 200 in **0.280512 s**, 62 models, no next page. Both
models/gemini-3.7-flash and models/gemini-2.5-flash-lite were returned with
generateContent support. Listing is not evidence of quota, tariff or successful
generation. Three successful Requests generation responses demonstrate that this
client can communicate with this endpoint/key/model; they do not explain prior
503s or establish reliable evaluation availability.

## Generation attempts

All inputs were small synthetic data; no real album metadata/full HTML/lyrics.
Input remains bounded by the evaluator's 200 KB guard, output by 2048 tokens.
Provider bodies and generated recommendation content were not saved.

| Call | Model | Stage/factor | HTTP | Safe outcome | Seconds |
|---|---|---|---|---|---|
| 1 | gemini-3.7-flash | Small JSON-request text, no systemInstruction/JSON MIME | 200 | Text received (11 characters) | 16.184573 |
| 2 | gemini-3.7-flash | Same text, add project systemInstruction | 200 | Text received (182 characters) | 35.654611 |
| 3 | gemini-3.7-flash | Same text/system, add JSON MIME | 200 | Expected small JSON object parsed | 31.135280 |
| 4 | gemini-3.7-flash | Real production evaluator, synthetic candidate | none | TIMEOUT, read phase | 60.126223 |
| 5 | gemini-2.5-flash-lite | Temporary override to a list-returned text model, minimal input | 404 | NOT_FOUND / MODEL_UNAVAILABLE | 0.395683 |
| 6 | gemini-3.7-flash | Same production evaluation, explicit LOW thinking | 200 | LOCAL_VALIDATION_ERROR | 22.763038 |

Procedure deviation: calls 2 and 3 briefly overlapped because a yielded wait was
mistakenly treated as completion before starting the next stage. This did not
create retries or extra calls, but their timings must not be treated as a serial
performance comparison. Subsequent calls were sequential; total remained six.
The temporary budget file lost call 2's completed row through overlapping updates;
the row above is taken from the tool's observed result, not reconstructed content.

No independent client was needed to establish successful Requests communication;
none was used. No additional models.get call was made. The existing model was
never silently switched; alternative call 5 was temporary and explicitly labeled.

## Implemented changes

- Gemini output was not token-bounded: configurable
  `search.gemini_max_output_tokens` now defaults to 2048 (allowed 1..8192),
  applied to real adapter and isolated checks. Limits are checked before network
  access. This can truncate a thinking-heavy response; truncation/invalid content
  is rejected and CSV/manual fallback is retained, never silently accepted.
- Add opt-in `search.gemini_thinking_level`: empty means provider default;
  minimal/low/medium/high are passed explicitly using ThinkingConfig. No automatic
  level downgrade or model fallback is introduced; unsupported settings fail safely.
- Preserve actual model and evaluation attempt ordinal in safe CSV diagnostics;
  configured thinking level is recorded separately from a claim it was honored.
- Local validation formerly discarded every rule: add fixed `validation_code`
  values (e.g. CANDIDATE_IDS, EVIDENCE_REFERENCE, MISSING_UNCERTAINTY). Generated
  text/exception values are never emitted. All rejection gates remain intact.
- `check_gemini.py` gains temporary --model, --max-output-tokens,
  --thinking-level and --structured-mode overrides. No config file mutation;
  schema check never retries with JSON after rejection. --timeout remains local.

No global timeout, permanent local model, proxy, credential or billing changes.
New policy defaults are implementation changes, not edits to search.local.toml.
The old 15-second default remains, so an ordinary evaluation can still time out;
there is no evidence-backed automatic switch/retry that fixes provider instability.

## Next concrete check / unresolved work

A live schema-mode evaluation of the current model with LOW reasoning is still
unverified. It could constrain structural errors but cannot guarantee semantic
identity/evidence rules. A new separately authorized generation allowance is needed;
no such call is made in this task. Example fish command (one paid-capable request):

```fish
cd /home/emvis/Music/auph
.venv/bin/python check_gemini.py --config search.local.toml --stage evaluation --timeout 60 --model gemini-3.7-flash --max-output-tokens 2048 --thinking-level low --structured-mode schema
```

This is an unverified next diagnostic, not a claimed working fix. The exact
already-executed final attempt used the same command without --model and
--structured-mode (current model, configured JSON mode). If returned results
remain invalid, safe validation_code now identifies the rejecting rule. For the
list/404 inconsistency, the provider must expose a usable generateContent route
for that model or the alternative must remain unsupported; no guessed aliases,
quota changes or authentication bypass were attempted.

## Final offline verification

- Baseline before changes: `.venv/bin/pytest -q test_gemini_diagnostics.py test_release_search.py test_search_details.py test_search_stages.py` — **206 passed in 16.81 s**.
- Final profile: same command — **215 passed in 16.76 s**.
- `.venv/bin/pytest -q` — **920 passed in 162.82 s**.
- `.venv/bin/ruff check .` — all checks passed.
- `.venv/bin/ruff format --check .` — 67 files already formatted.
- `.venv/bin/mypy .` — no issues in 52 source files.
- `.venv/bin/pip check` — no broken requirements.
- `git diff --check` — exit 0.
- `.venv/bin/python check_gemini.py --help` — exit 0; no network call.

Nine added regressions exercise temporary overrides through real adapter/HTTP
wiring, bounded output and pre-request validation, explicit thinking configuration,
model/rule diagnostics, no schema retry, and unchanged configuration files. Existing
CSV/manual-fallback and exact-candidate confirmation cases pass unchanged. No
assertion was weakened or provider disabled to obtain these results. These offline
checks do not turn the last rejected live recommendation into a success.

## Contract follow-up: two additional authorized calls

The user separately authorized at most two more generateContent requests to
investigate LOCAL_VALIDATION_ERROR. Before any call, the evaluator, checker,
loader, prior report and temporary budget artifacts were inspected. The earlier
provider recommendation was **not retained**: only metrics/model-list metadata
survived. Its precise failing rule cannot be reconstructed or attributed to the
new offline counterexample. Permanent local configuration remains unchanged.

Current model for both requests: gemini-3.7-flash. Same configured key, diagnostic
read timeout 60 s / connect timeout 5 s, output bound 2048, explicit LOW reasoning.
Each call used the real production evaluator and checker fixture, with an HTTP
boundary guard allowing exactly one request. The guard retained a response only
in process memory and would persist only an allowlisted, sanitized failing
recommendation; no recommendation was received in either attempt, so none was
persisted. No retries, compatibility downgrade, model-list, tracker or client calls.

| Additional call | Mode | Safe observed result | Duration |
|---|---|---|---|
| 1 (before contract fixes) | configured JSON | HTTP 503 / UNAVAILABLE, SERVER_ERROR | 1.225922 s |
| 2 (after fixes) | temporary schema override | HTTP 400 / INVALID_ARGUMENT, REQUEST_REJECTED | 1.160586 s |

**Outcome:** no new HTTP-200 recommendation, no reproduced live validation_code,
no validated live evaluation. The second request was rejected before local
recommendation validation. The provider diagnostics did not identify the offending
parameter; model schema incompatibility, key validity or a particular unsupported
schema keyword is not established by the generic INVALID_ARGUMENT result. Exactly
**two additional generations** were made (eight across both October 12 tasks),
zero new metadata requests; the current allowance is exhausted. No further call
was made to isolate the rejected parameter.

### Confirmed local defects and implemented fixes

1. **Missing reason/evidence contract in model input.** The validator permits only
   particular evidence fields for each preference, and disables unsupported
   lossless/completeness/rip/availability claims. Previously neither the input nor
   schema communicated the per-candidate allowed combinations. An artist evidence
   ID could satisfy the response schema yet fail the `uncertain` preference's
   semantic gate. Input now includes `allowed_reasons` computed from real evidence
   and policy flags; the prompt explicitly requires those pairs.
2. **Schema/validator shape mismatch.** Schema had arbitrary strings for candidate
   IDs, omitted local array bounds, and did not constrain the required empty-string
   abstention branch. Per-evaluation schemas now enumerate eligible IDs (including
   empty selected ID for abstention), available evidence keys and documented array
   bounds. The prompt includes the exact abstention example and mandatory uncertainty
   codes. Schema is copied per call; no IDs leak through a mutated global schema.
   Per-candidate evidence checks, ranked-ID checks, unsupported-claim checks and
   mandatory uncertainty validation remain local and unchanged. Provider acceptance
   of this richer schema remains unverified because the live request returned 400.
3. **Truncation hidden as generic invalid output.** Gemini MAX_TOKENS now produces
   `OUTPUT_TRUNCATED` plus safe finish_reason before JSON/contract validation, even
   when the partial response happens to parse as JSON. Other controlled refusal
   finish reasons produce REFUSED_RESPONSE. Malformed JSON without a truncation
   signal remains INVALID_RESPONSE / INVALID_JSON; it is not falsely declared a
   proven truncation. Neither case is LOCAL_VALIDATION_ERROR.
4. **Diagnostic integration verification.** Real checker and CSV workflow tests
   now demonstrate propagation of EVIDENCE_REFERENCE and retention of manual fallback.

Two focused regressions were run before changes and failed: MAX_TOKENS returned
INVALID_RESPONSE; model input lacked allowed_reasons. They passed after fixes.
No semantic check was weakened. The local validator correctly rejects the minimal
counterexample in `tests/fixtures/search/gemini_reason_evidence_mismatch.json`:
`uncertain` with `artist` evidence. This is a **synthetic contract counterexample**,
not a sanitized copy of the missing historical provider response. It contains only
fixture:1, code values and an evidence field name, no lyrics/personal metadata.

The implementation was compared with primary documentation:
[generateContent FinishReason](https://ai.google.dev/api/generate-content) defines
MAX_TOKENS; [JSON schema support](https://ai.google.dev/gemini-api/docs/structured-output)
documents enum/minItems/maxItems and still requires semantic validation. These
contracts do not guarantee acceptance for this key/model/endpoint combination.
An independent jsonschema library was unavailable in the existing environment;
no dependency was installed and independent library validation is not claimed.

Exact fish command equivalent to the second production check (one API call):

```fish
cd /home/emvis/Music/auph
.venv/bin/python check_gemini.py --config search.local.toml --stage evaluation --timeout 60 --max-output-tokens 2048 --thinking-level low --structured-mode schema
```

This command returned 400 in this task; it is not advertised as a working fix.
Further live isolation would require a fresh explicitly authorized budget and safe
field-level provider diagnostics. Until then the live schema rejection and the
historical LOCAL_VALIDATION_ERROR remain unresolved; CSV/manual selection continue
and no rejected response approves a download. Global policy, permanent model/key,
network settings and real music collection were untouched.

### Final verification of contract follow-up

All checks used temporary fixtures or mocked network boundaries; none submitted
downloads or enriched music files.

| Command | Actual result |
|---|---|
| `.venv/bin/pytest -q test_gemini_diagnostics.py test_release_search.py test_search_details.py test_search_stages.py` (baseline) | 215 passed in 17.34 s |
| `.venv/bin/pytest -q test_gemini_contract.py` (before production fixes, initial two regressions) | 2 failed, reproducing missing allowed-reasons input and misclassified truncation |
| `.venv/bin/pytest -q test_gemini_contract.py test_gemini_diagnostics.py test_release_search.py test_search_details.py test_search_stages.py` (final) | 222 passed in 17.54 s |
| `.venv/bin/pytest -q` | 927 passed in 157.82 s |
| `.venv/bin/ruff check .` | All checks passed |
| `.venv/bin/ruff format --check .` | 68 files already formatted |
| `.venv/bin/mypy .` | No issues in 53 source files |
| `.venv/bin/pip check` | No broken requirements |
| `git diff --check` | Exit 0, no whitespace errors |

These offline results verify strict validation, diagnostic propagation and manual
fallback. They do not establish live structured-output compatibility or recover
the missing historical response. No permanent configuration changes or commits
were made in this follow-up; unrelated existing changes were preserved.

## Schema-error follow-up (three additional authorized calls)

### Offline comparison and safety

Before network calls, current evaluator/schema/checker and this report were read.
No applicable AGENTS.md was present in the repository or its ancestors. Current
permanent configuration still selects **gemini-3.7-flash**; it was not changed.

The actual schema-mode generationConfig contains `maxOutputTokens: 2048`,
`thinkingConfig.thinkingLevel: LOW`, `responseMimeType: application/json`, and
`responseJsonSchema` (not responseSchema). The adapter uses the configured v1beta
models/{model}:generateContent route and x-goog-api-key authentication. Command-local
read timeout was 60 s, connect timeout 5 s; TLS/default proxy behavior was unchanged.

The official [GenerationConfig reference](https://ai.google.dev/api/generate-content)
exposes responseJsonSchema and distinguishes it from the OpenAPI-style responseSchema.
Its current documentation also marks older schema fields deprecated; that does not
prove they are rejected for this request. The [structured-output subset](https://ai.google.dev/gemini-api/docs/structured-output)
lists the project's keywords: type, properties, required, additionalProperties,
string enum, items, minItems and maxItems. Production schema has no references or
recursive types. Eligible IDs are enumerated; selected ID allows empty-string
abstention; ranking/reasons permit empty arrays; uncertainty requires at least two
entries. This offline comparison establishes no specific invalid parameter. It
also does not prove provider acceptance of the empty enum member or this schema's
complexity. No keyword, identity gate or semantic validation was relaxed on a guess.

Prepared comparison schemas: minimal object with one required boolean `ok`, and
the real per-candidate production schema. Both use the same responseJsonSchema field,
model, LOW setting, output bound and temporary timeout.

### Confirmed diagnostic defect and fix

Previously bounded HTTP error parsing retained status/code/auth/quota indicators
but discarded all BadRequest.fieldViolations and error.message. It could not expose
an offending parameter even if the provider supplied one. The fix projects up to ten
known request/schema field paths (bounded to 300 characters) and fixed descriptions.
Unknown-name errors available only in message are parsed without retaining raw text.
If no recognized field violation is available, a message-only projection under
`field=request` retains only a closed diagnostic vocabulary and known field names:
quoted values, URLs, known configured secrets, unknown words/numbers are redacted;
the resulting description is capped at 350 characters. This lossy projection may
still fail to identify a cause; it is never represented as a raw provider message.

`provider_field_violations` survives EvaluationError filtering and is appended to
candidate evaluation diagnostics, which the existing CSV serializer exports. Known
configured secrets are passed explicitly into production evaluator HTTP diagnostics.
Strict local ID/evidence/unsupported-claim/uncertainty checks and CSV/manual fallback
remain unchanged. No new search, client, provider or configuration feature was added.

### Actual sequential calls and unresolved outcome

A temporary HTTP-boundary guard counted each wire request before sending it, denied
more than one per invocation, disabled retries, and maintained a three-call task
budget. Production evaluator used allow_schema_fallback=False. No tracker/client,
models-list, permanent configuration, music or billing operations occurred.

| Call | Request | HTTP / outcome | Seconds |
|---|---|---|---|
| 1 | Minimal one-boolean schema, no systemInstruction | 503 / UNAVAILABLE / SERVER_ERROR | 1.063835 |
| 2 | Real production evaluation, field-details diagnostics | 400 / INVALID_ARGUMENT / REQUEST_REJECTED | 16.724418 |
| 3 | Same production schema; confirm message-only diagnostic fix | 503 / UNAVAILABLE / SERVER_ERROR | 2.770476 |

Exactly **three additional generateContent calls**, sequential, no retries/schema
fallback. In this task the total is three, not the earlier report's eight calls.
The minimal schema was not accepted or rejected on schema grounds: it received 503.
Call 2 provided no recognized BadRequest field violation or unknown-field message;
message-only projection was added after that observation and tested by call 3.
Call 3 supplied only a redacted request-level server message, with no schema field.
Both 503 results are separate server failures and do not explain the 400.

**Concrete invalid parameter remains unidentified; production evaluation did not
succeed or reach local recommendation validation.** No schema fix is claimed because
none was justified by the observed responses. The third call confirms only safe
message propagation for a server error, not resolution of INVALID_ARGUMENT. Remaining
blocker: a repeatable 400 response with an actionable safe field/constraint diagnostic
(or a provider-side request trace identifying it). Any further live isolation needs
new explicit request authorization; the three-call allowance is exhausted.

Equivalent one-call production check in fish (not a claimed successful command):

```fish
cd /home/emvis/Music/auph
.venv/bin/python check_gemini.py --config search.local.toml --stage evaluation --timeout 60 --max-output-tokens 2048 --thinking-level low --structured-mode schema
```

No raw request/response body, authorization headers, key/cookie or sensitive URL was
retained. The added offline fixtures are synthetic error objects inside tests, not
copies of live error bodies. Model IDs, secrets and permanent settings were not changed.

### Offline verification for schema-error follow-up

- Baseline: `.venv/bin/pytest -q test_gemini_contract.py test_gemini_diagnostics.py` — **54 passed in 1.05 s**.
- Final profile: `.venv/bin/pytest -q test_gemini_contract.py test_gemini_diagnostics.py test_release_search.py test_search_details.py test_search_stages.py` — **224 passed in 17.24 s**.
- `.venv/bin/pytest -q` — **929 passed in 162.94 s**.
- `.venv/bin/ruff check .` — all checks passed.
- `.venv/bin/ruff format --check .` — 68 files already formatted.
- `.venv/bin/mypy .` — no issues in 53 source files.
- `.venv/bin/pip check` — no broken requirements.
- `git diff --check` — exit 0.

Two new deterministic regressions exercise the production HTTP/evaluator boundary:
BadRequest/unknown-field extraction with secret/reflected-data exclusion, and bounded
message-only projection without URLs, quoted inputs or configured keys. Each checks
one wire call and no schema fallback. Existing strict validation, CSV/manual fallback
and download-confirmation tests passed. No test asserts a live schema rejection cause
that the three observed responses did not establish. No unrelated changes were committed.

## Independent reproducible-request investigation

This follow-up replaces the repeated generic-error approach with a frozen synthetic
payload and a separate network path. The historical 400 and HTTP-200 rejected
recommendation bodies were not saved; they were not reconstructed.

### Offline reproduction and documentation

No AGENTS.md was present in the project/ancestors. Current actual configuration
still selects **gemini-3.7-flash** and the official HTTPS
`generativelanguage.googleapis.com/v1beta` endpoint. Permanent configuration and
credentials were not modified. The configured key is loaded using the existing
shell/.env policy and sent only in x-goog-api-key, never URL/process arguments.

The [Google generateContent reference](https://ai.google.dev/api/generate-content)
and [structured-output documentation](https://ai.google.dev/gemini-api/docs/structured-output)
were checked again: responseJsonSchema is distinct from OpenAPI responseSchema;
responseMimeType accompanies schema; the used type/properties/required/enum/items/
minItems/maxItems/additionalProperties keywords are documented. LOW is a documented
ThinkingConfig level, with model-dependent support. Documentation also warns that
large/deep schemas may be rejected. This is not proof of acceptance for the current
model/request, and no new model listing or metadata request was made.

`check_gemini_wire.py --prepare-only` captures the real evaluator's request **before
HTTP and recommendation validation**, using the existing synthetic checker fixture.
The independent network path calls Requests Session.post directly, without production
HTTP parsing/error sanitizer/recommendation validator. It uses the same endpoint,
model, effective key and JSON object. Requests prepared bytes decode exactly to the
captured object. Comparison recorded **0 semantic payload differences**, **8526 bytes**,
SHA-256 `bc5a2693848ebb0455acee0664a5bb393c919e062b9d3a48bb08e9566c4307af`.
Both paths use Requests; this is an independent application client, not an independent
TLS implementation. Its explicit zero-retry adapter/TLS verification match production
defaults; it bypasses production error processing. No network call was spent on
serialization comparison.

### Confirmed defects fixed

Before changes, five new offline diagnostic tests produced **3 failures / 2 passes**:
closed-vocabulary redaction erased schema nesting depth/limits, mutually-exclusive
field explanations and unknown fieldViolations paths. The word allowlist has been
removed. Bounded short diagnostics now preserve constraint vocabulary/numbers and
unknown parameter paths after targeted redaction of secrets, reflected request/query/
candidate values, quoted non-field values, credential headers, URLs, email and home
paths. Arbitrary message-only non-constraint text stays out of ordinary diagnostics.
An existing test's artificial `non - empty` spacing expectation was updated to the
actual preserved `non-empty` wording; privacy assertions for secrets/URLs/quoted
metadata remain, and unknown diagnostic words are explicitly required to survive.

The new **opt-in, local** report is separate from short checker/CSV diagnostics.
Its independent redactor preserves bounded error.message/error.details rather than
filtering words. It removes configured/environment secrets, cookie/Authorization/
proxy credentials, synthetic reflected request/system/query/candidate text, URL
credentials/parameters, email, home paths and addresses. Reports never retain raw
response bodies, request headers or key-bearing URLs. Fully synthetic inputs limit
privacy exposure; this is not a promise of arbitrary PII detection in real album
payloads. The diagnostic client accepts no arbitrary album payload.

Report files are created with O_EXCL/O_NOFOLLOW and **0600**, outside the repository
and music collection. The report directory was created with 0700. A locked persistent
budget is incremented/fsynced before each wire request; the tool permits at most four
calls per directory and one production call per invocation. Requests are sequential,
TLS-verified, connect timeout 5 s/read timeout 60 s, output 2048, LOW, no redirects,
retries/schema fallback/model switching. The optional production path uses the real
validator and saves a minimal projected invalid recommendation with validation_code
if received; projected unknown IDs/evidence remain invalid, never repaired.

### Live results: stop early on confirmed current external rejection

Private artifacts: `/tmp/auph-gemini-wire-2026-10-12-r1/`:
`synthetic-payload.json`, `comparison.json`, `call-1.json`, `call-2.json`, `budget.json`.
Actual file modes were independently read back as 0600.

| Call | Hypothesis / single changed factor | HTTP / provider code | Seconds |
|---|---|---|---|
| 1 | Same production schema through independent client; bypass adapter error processing | 503 / UNAVAILABLE | 6.448712 |
| 2 | Remove **only** generationConfig.responseJsonSchema; same prompt, embedded required_schema, MIME, model, LOW and tokens | 503 / UNAVAILABLE | 6.329965 |

Both private cleaned error reports preserve the same substantive provider message:

> This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.

**Exactly two new generateContent calls**, of a four-call allowance. No remaining
calls were spent on identical repeats or a production confirmation while both paths
were unavailable. The present 503 refusal is attributed by the provider to high demand;
no independent load measurement or internal cause is claimed. Removing responseJsonSchema
did not resolve that refusal. Neither response establishes schema acceptance, explains
historical 400/INVALID_ARGUMENT, proves key/model/quota validity, or reaches recommendation
validation. **No validated live evaluation and no proven schema/request fix** resulted.
The diagnostic/reproduction fixes are not represented as fixing live availability.

Remaining obstacle: the provider rejected both controlled requests before producing
content. Resolving the historical schema failure still needs an actionable 400
explanation or a successful controlled schema response. The now-preserved request and
meaningful private diagnostics allow that comparison without real album data. There
is no justified client-side correction for the provider's high-demand refusal; no
billing/quota/model/global-network changes were made. No Jackett/RuTracker/client
calls, downloads or music writes occurred.

### Exact fish commands

Offline preparation (zero API calls; choose a fresh private directory):

```fish
cd /home/emvis/Music/auph
set report_dir (mktemp -d /tmp/auph-gemini-wire.XXXXXX)
.venv/bin/python check_gemini_wire.py --config search.local.toml --report-dir "$report_dir" --prepare-only
```

One independent request, with brief terminal output and private cleaned report:

```fish
.venv/bin/python check_gemini_wire.py --config search.local.toml --report-dir "$report_dir" --transport independent
```

The executed requests used `/tmp/auph-gemini-wire-2026-10-12-r1`; call 2 additionally
passed `--omit-response-schema`. A **future explicitly authorized** real-evaluator
check (not run in this follow-up) is:

```fish
.venv/bin/python check_gemini_wire.py --config search.local.toml --report-dir "$report_dir" --transport production
```

No response from this task passed HTTP 200; no invalid-recommendation fixture could be
created from live content. The new fixtures/tests are synthetic and labeled as such.

### Final verification of independent reproducer

- Pre-fix diagnostic regressions: `.venv/bin/pytest -q test_gemini_wire.py` — **3 failed / 2 passed** (depth/limits, mutual exclusion, unknown field path).
- Final profile: `.venv/bin/pytest -q test_gemini_wire.py test_gemini_contract.py test_gemini_diagnostics.py test_release_search.py test_search_details.py test_search_stages.py` — **234 passed in 17.13 s**.
- `.venv/bin/pytest -q` — **939 passed in 154.32 s** on the final code. An earlier development full run was 938 passed before adding the diagnostic-client exit-status regression; it is not the final result.
- `.venv/bin/ruff check .` — all checks passed.
- `.venv/bin/ruff format --check .` — 70 files already formatted.
- `.venv/bin/mypy .` — no issues in 55 source files.
- `.venv/bin/pip check` — no broken requirements.
- `git diff --check` — exit 0.

New regressions cover the five requested diagnostic shapes, targeted privacy
redaction and 0600/exclusive reports, offline exact payload capture, pre-send durable
budget reservation/no retry/read-timeout classification, a projected invented ID
remaining invalid, and the CLI reporting HTTP failure with nonzero exit status.
Independent HTTP 200 is explicitly marked locally_validated=false; only real
production validator success is evaluation success. The CLI now exits 1 on provider
failure (the two live calls predated that small exit-status correction; the correction
was verified offline without more API calls). Semantic validation was unchanged.

`reproduction-index.json` was also saved privately with the frozen request/control
hashes and the exact one-field omission; it was derived offline from the captured
synthetic request and the actually executed control command, not from a guessed
historical response. All API activity stopped at two calls. No commits or unrelated
working-tree modifications were made in this follow-up.
