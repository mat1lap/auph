# Gemini diagnostics verification — 2026-10-09

Scope: configured Gemini alone; no indexer/topic requests, torrent submissions,
music writes, model changes or billing changes. Unrelated worktree changes were
preserved. Official contracts inspected: [generateContent](https://ai.google.dev/api/generate-content)
and [troubleshooting](https://ai.google.dev/gemini-api/docs/troubleshooting).

Presence-only inspection confirmed the configured environment name is valid,
its key is present after existing `.env` loading, and conventional GEMINI_API_KEY
is also present. Model: `gemini-3.8-flash`; endpoint: official Google `/v1beta`;
timeout: 15 seconds; structured mode: JSON; remote disclosure: enabled.
Request uses x-goog-api-key, contents, systemInstruction and generationConfig;
no key is placed in a URL. No model-list lookup or model substitution occurred.

Exactly **two generateContent calls** were issued, one attempt each:

| Stage | Command | Observed outcome |
|---|---|---|
| Minimal | `.venv/bin/python check_gemini.py --config search.local.toml --stage both` | TIMEOUT, 15.182205 s, no HTTP status received |
| Production evaluator, synthetic candidate | `.venv/bin/python check_gemini.py --config search.local.toml --stage evaluation` | TIMEOUT, 15.188158 s, no HTTP status received |

The first command's evaluation stage encountered a local missing fixture argument
before making any request. That development defect was corrected and covered by
the standalone two-stage regression; the separate evaluation command used the
remaining one-call allowance. No additional live call was made.

**Established result:** Gemini currently times out within the configured read
budget. The cause of the earlier `TRANSIENT_ERROR` after 3.02 seconds could not be
established: its HTTP status/provider code was discarded by the old adapter.
Neither key validity, model availability, quota, successful structured output nor
live recommendation compatibility was verified by these timeouts. A later bounded
standalone call, when authorized, can distinguish returned provider errors using
the new diagnostics; no speculative diagnosis is presented as a fact.

Confirmed implementation defects fixed:

- Evaluator discarded HTTP/provider diagnostics: bounded error parsing now retains
  allowlisted codes, status and Retry-After without body/message disclosure.
- Requests exceptions conflated transport and timeout: evaluator now exposes
  distinct safe categories. Server, auth, model/route, quota/rate, malformed output
  and local contract rejection are separately classified.
- Configuration error echoed an env setting containing a pasted credential:
  all search env names are validated at construction/config loading; diagnostics
  never repeat their values. Known API-key prefixes are rejected even if their
  characters happen to form a valid variable name.
- Schema downgrade could retry a 400 invalid-key error: authentication failures
  are now excluded. Standalone checks disable every compatibility retry.
- Search CSV had no structured evaluation failure evidence: eligible rows retain
  bounded request diagnostics through the real workflow and export.

Offline verification uses the real HTTP adapter/evaluator with mocked network
responses. Covers provider categories, Retry-After, no retries, malformed output,
local validation, env-name redaction, standalone request bounds, and failure
propagation to CSV/manual fallback. Existing search tests cover mandatory exact
candidate confirmation and cancellation; those behaviors were retained.

Verification commands already completed:

- Baseline: `.venv/bin/pytest -q test_release_search.py test_search_details.py test_search_stages.py` — 168 passed in 16.08 s.
- `.venv/bin/pytest -q test_gemini_diagnostics.py` — 36 passed in 0.69 s.
- `.venv/bin/pytest -q test_gemini_diagnostics.py test_release_search.py test_search_details.py test_search_stages.py` — 204 passed in 16.73 s.
- `.venv/bin/ruff check .` — all checks passed.
- `.venv/bin/ruff format --check .` — 66 files already formatted.
- `.venv/bin/mypy .` — no issues in 52 source files.
- `.venv/bin/pip check` — no broken requirements.
- `git diff --check` — exit 0.
- `.venv/bin/python check_gemini.py --help` — exit 0, no API call.

An intermediate related run exposed three existing assertions expecting the
manual-selection reminder; the diagnostic text was corrected to retain that
contract without weakening assertions. Development lint/type errors were fixed;
the final commands above passed. The earlier full run passed 907 tests before the
last two regressions were added; final full-suite results are recorded below.

Final full suite after all implementation and test additions:
`.venv/bin/pytest -q` — **909 passed in 137.88 s**. No tests skipped or
assertions weakened to obtain this result. Network responses in new regressions
were mocked; the two separately recorded live attempts both timed out.

## Separately authorized 60-second evaluation check

A later user request authorized exactly one additional evaluation call. Added
`check_gemini.py --timeout`, applied only to its in-memory policy/HTTP client;
connect timeout remains capped at 5 seconds and read timeout becomes 60 seconds.
No global configuration or normal pipeline timeout was changed. Requests timeout
exceptions now retain a safe `timeout_phase`: connect/read/unspecified.

Command:
`.venv/bin/python check_gemini.py --config search.local.toml --stage evaluation --timeout 60`

Observed safe diagnostics:

```json
{"attempts":1,"connect_timeout_seconds":5,"duration_seconds":48.800613,"failure_kind":"SERVER_ERROR","http_status":503,"provider_code":"UNAVAILABLE","read_timeout_seconds":60.0,"stage":"evaluation","timeout_phase":null}
```

Exactly one request, no retries or schema downgrade, same configured key/model,
no topic/indexer requests. The response establishes HTTP 503 / UNAVAILABLE for
this attempt; it was not a connect/read timeout. It does not establish why an
earlier attempt failed, key/model compatibility, quota status or the provider's
underlying reason for unavailability. No raw response body/headers were printed.

Verification after the timeout change:
- `.venv/bin/pytest -q test_gemini_diagnostics.py` — 38 passed in 0.72 s.
- `.venv/bin/pytest -q test_gemini_diagnostics.py test_release_search.py test_search_details.py test_search_stages.py` — 206 passed in 16.62 s.
- `.venv/bin/ruff check .` — passed.
- `.venv/bin/ruff format --check .` — 66 files formatted.
- `.venv/bin/mypy .` — no issues in 52 source files.
- `git diff --check` — passed.

The 909-test full-suite checkpoint above predates these two additional timeout
regressions; it is not presented as a full-suite run of this follow-up revision.

## Follow-up on 2026-10-12

See [GEMINI_VERIFICATION_2026-10-12.md](GEMINI_VERIFICATION_2026-10-12.md)
for the current gemini-3.7-flash configuration, authorized models.list, six bounded
generation comparisons, strict-validation failure and remaining limitations.
The model in this historical report must not be assumed to be current.
