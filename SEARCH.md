# Album/single search

`audiophile_tagger.py search` searches only Jackett's configured **RuTracker.org**
indexer (`rutracker`). It exports release evidence, optionally requests an LLM
recommendation, and offers explicitly confirmed qBittorrent submission. It does
not modify FLACs or automatically enrich completed downloads.

```bash
# Search, obtain authorized topic details if configured, export; no LLM/client:
.venv/bin/python audiophile_tagger.py search "Queen Jazz" --csv-only --csv /tmp/queen-jazz.csv
# Explicit identity/type filters; backend requires configured model and disclosure:
.venv/bin/python audiophile_tagger.py search --artist Queen --title Jazz --type album --backend gemini --csv /tmp/queen-jazz.csv
# Display selection and request confirmation in a terminal:
.venv/bin/python audiophile_tagger.py search "Queen Jazz" --backend local --submit --csv /tmp/queen-jazz.csv
# Preselect a topic to display; this does NOT approve downloading:
.venv/bin/python audiophile_tagger.py search "Queen Jazz" --select rutracker:123456 --submit
```

Standalone `release_search.py` accepts the same arguments. `--no-details` exports
summaries explicitly marked incomplete. Default output is `search-results.csv`
in the working directory; use `--csv` to select its destination. Output must
use a `.csv` filename. Existing unrelated files/CSV inventories and symlinks
are refused; only an existing search export can be replaced. `--csv-only`
prevents external review search, evaluation and client submission even if the configuration enables
a model. `--csv-only --submit` and `--select` without `--submit` are rejected.

## Configuration and access

Copy/adapt [search.example.toml](search.example.toml), or append its `[search]`
section to an existing metadata policy. Select a policy with `AUDIOPHILE_CONFIG`.
CLI limit/page/backend switches override that policy; unrelated enrichment
settings retain their existing behavior. Existing environment values take
precedence over project `.env` values. Do not commit secrets. Set:

- `JACKETT_URL` (or `search.jackett_url`) and `JACKETT_API_KEY`. Configure and
  authenticate the RuTracker.org indexer through Jackett's own supported UI.
- `RUTRACKER_COOKIE` for the optional separate topic-detail stage: an existing,
  authorized Cookie header for the configured topic host. No automated login
  or authentication bypass is implemented. Without this,
  candidates remain useful search summaries with `NOT_CONFIGURED` details.
- Optional `search.flaresolverr_url` or `FLARESOLVERR_URL` for the **separate**
  topic transport, e.g. `http://127.0.0.1:8191`. Jackett's solver configuration
  is not inherited. Both the service base and its `/v1` endpoint are accepted.
  `search.flaresolverr_timeout` bounds challenge solving (default 30 seconds,
  range 1–60); the client allows 15 additional seconds for startup/cleanup.
  The documented browser API receives only a canonical public topic URL,
  never configured authentication cookies or Jackett credentials. Its clearance
  cookies and matching user agent are used for the authenticated topic request;
  redirects remain refused. No CAPTCHA submission options are enabled.
  Clearance is private to this adapter/run and reused after successful requests.
  Do not enable FlareSolverr DEBUG/HTML logging: upstream can log returned cookies.
- The selected LLM's key environment variable and **explicit model**. OpenAI and
  local endpoints are full **Chat Completions** URLs. Gemini's endpoint is the
  documented API base; its model-specific `generateContent` route is appended.
- `remote_metadata_disclosure=true` to send normalized query/candidate evidence
  to Gemini, OpenAI or a non-loopback local-server host. No HTML, authenticated
  download URLs, cookies or API keys are included in model messages. Relevant
  topic claims are third-party content; enable remote disclosure knowingly.
- `QBITTORRENT_USERNAME`, `QBITTORRENT_PASSWORD`, `search.qbittorrent_url` and
  absolute `search.save_path` on the **client host** for submission. Cookie login
  uses a dedicated Requests session. TLS verification stays enabled. Use HTTPS
  for credentials over nonlocal networks.

`structured_mode="schema"` requests schema-constrained output; a 400/422 rejection
permits one downgrade to JSON-object mode. `json` requests JSON without server
schema support; `text` is for servers lacking those hints. **Every mode still
uses strict local validation.** Unavailable models, refusal, invalid JSON,
invented IDs/evidence or unsupported claims preserve the CSV and manual selection.
There is no default model and no assumption of identical local-server capabilities.

For an existing local search policy, select it explicitly:

```bash
AUDIOPHILE_CONFIG=search.local.toml .venv/bin/python audiophile_tagger.py search \
  --artist Queen --title Jazz --type album --csv-only --limit 2 \
  --page-size 2 --max-pages 1 --csv /tmp/queen-jazz.csv
```

`search.local.toml` is not automatically loaded. The shipped metadata policy has
no Jackett URL; a configured API key/cookie alone does not configure discovery.

## Evidence, eligibility and recommendation

Discovery uses the indexer-specific Torznab `t=search`, audio category `3000`,
query, offset and limit. It never queries `all` or other trackers. Topic IDs and
canonical public URLs define deduplication, keeping separate pressings/topics
separate. Summaries retain titles, sizes, peer counts and publication text. Full
characteristics require a separate topic request. Topic parsing
recognizes explicit English/Russian labels in `.post_body`; it is not a complete
parser for every RuTracker template. Disc headings and repeated positions across discs are retained in recognized
numbered track lists. `track_count` counts parsed listed entries, not a verified
release total. Unrecognized and absent fields stay unknown;
login, blocking, rate-limit and parser failures are distinguished from no results.
Malformed numeric fields are left unknown; malformed rows do not discard other topics.
A later page failure preserves earlier discoveries. Independent detail failures
retain summaries and other candidates.

Leading genre/format groups are removed from tentative title identity, including
adjacent bracket tags and sample-format groups containing `/`. Parsed artist and
album names must match the respective structured filters after Unicode/punctuation
normalization; sharing the words Queen and Jazz is insufficient. Recognized explicit
`[CD]`/`[LP]`/`[SACD]`/`[DVD-A]`, codec and `[24/192]` style annotations are retained
as `search_title` evidence with **title-derived, unverified** status. CD does not
imply 16/44.1, TR24/OF does not imply a codec, and WavPack does not establish its
lossless mode. Unqualified/multiple years retain unknown roles. Explicit Reissue/Remastered/MQA x
UHQCD/country annotations are stated title claims, never sound-quality evidence.
Authenticity and completeness are not inferred.

`detail_diagnostics` records actual HTTP status, final host/path (without query,
userinfo or fragment), content type, transport and controlled blocking markers.
403/451 denial, challenge pages (including HTTP 200 challenges), login-only pages,
refused redirects, malformed responses, parser errors and solver/connection failures
remain distinct. Login widgets alongside a readable post do not alone prove expired
authentication. Solver API status and browser-reported status are separate: upstream
FlareSolverr currently synthesizes browser status 200 and omits response headers,
so neither is proof that RuTracker returned HTTP 200.

If authenticated HTTP remains 403 but the independent public browser response
contains a readable post at the exact requested topic URL, `FOUND_PUBLIC` retains
that accessible data. Actual authenticated status/challenge evidence stays in the
diagnostics, the selected browser HTTP status is unknown, and cookie validity remains
unknown. A different topic/host, login-only response, missing post, explicit
authentication failure or unresolved browser challenge cannot trigger this fallback.

The CSV preserves its existing columns and appends stage/priority/evidence coverage,
claim history, skipped-detail reasons and timings. It has deterministic columns for identity, edition/year/source/technical
claims, tracks, rip/verification/processing claims, completeness, artwork,
availability, missing information, rejection reasons and evidence provenance.
Original album year is separate from edition year; a recording year is not
silently reinterpreted as either. Unknown scalar values are `UNKNOWN`; lists/maps
are JSON; booleans remain explicit. UTF-8 quoting preserves commas, Unicode and
line breaks. Exports are written to private temporary files, then replaced under
a per-output lock; a failed write preserves the previous complete CSV. Formula-like cells are prefixed with an apostrophe; URL/secret-bearing
text is redacted. Only canonical public topic URLs are exported, never download
links, passkeys or cookie values. **CSV suitability is not proof of audio quality.**

Wrong explicit artist/title evidence, incompatible declared album/single type,
unrequested discographies/live/remix/tribute/compilation markers, stated partial
releases (unless enabled), and stated unwanted lossy-source conversion/upsampling
are excluded. Matching titles alone remain **tentative**. Topic artist/title are
**stated**, never verified recording/edition identities. Conflicting topic labels
are left unknown with warnings. Ambiguity remains visible and can require manual
judgment; the query does not automatically resolve an exact mastering.

The transparent fallback order is stated identity, stated completeness, lossless
preference, stated rip/log/checksum evidence, then seed availability (capped for
ordering), then stable topic ID. These are preferences, **not an objective sound
quality score**. No bonus comes from bit depth, sample rate, file size,
vinyl/SACD, remaster status, branding or bonus-track counts. Lossless is a declared
codec claim (explicit topic label or unverified title annotation), not a measurement. More
seeds improve practical availability, not sound. Logs and verification claims
are not independently authenticated. Explicit absence/unknown claims (such as
“No log available”) remain in the export but receive no rip-evidence preference.

The LLM receives bounded normalized evidence and explicit preferences. Its
structured response selects only eligible IDs, ranks up to five alternatives,
and links each reason to supplied evidence and a supported preference. Explanations
are rendered from those evidence links; arbitrary factual assertions and URLs
are refused. Mandatory uncertainty states that mastering is unverified and
technical claims are unmeasured. Untrusted page instructions have no tools or
submission authority. Prompt isolation and validation cannot guarantee an ideal
opinion; the user sees evidence and must select and confirm.

## Confirmation, validation and uncertain outcomes

Submission requires interactive input **and** output terminals, an eligible
candidate ID, then the exact phrase `DOWNLOAD rutracker:<topic-id>` after displaying
the release/source, known properties and unknowns, recommendation, warnings,
client and destination. There is no default acceptance or LLM-selected consent.
Decline, EOF, wrong phrase, cancellation and non-TTY execution submit nothing.
Download resolution and client authentication happen only after confirmation.

Only the selected adapter's retained trusted Jackett proxy or authorized topic
download route (or supported magnet) is used. HTTP redirects are refused; the
current Jackett proxy's 302-to-magnet response is validated locally without
following a network redirect. Its signed `path` and `jackett_apikey` query
parameters are retained privately, never reconstructed or exported. Torrent data
is bounded and bencode/path/piece-count validated; only v1 metainfo is supported.
V2/hybrid torrents, symlinks, collisions, unsupported external payload sources
and unsafe/ambiguous alternate paths are refused. Supported magnets have one
validated hex/base32 `btih` and optional HTTP(S)/UDP trackers; display names are
dropped and external source parameters (`xs`, `as`, `ws`) are refused.

qBittorrent is checked for the info hash before submission. A private receipt is
written and fsynced **before** the single `torrents/add` request; that request is
never retried. Per-info-hash/client cross-process locks serialize this application's
writers. Existing downloads return without another add request. A successful
add is confirmed with an independent client listing. Timeout, interruption,
ambiguous response or delayed visibility leaves a pending receipt and blocks
subsequent blind attempts. Rejected requests are also retained for review.

Receipt directory defaults to `$XDG_STATE_HOME/auph/search-submissions` (normally
`~/.local/state/auph/search-submissions`), or a configured private owned directory.
If an outcome is uncertain, inspect the client and retained receipt. Stop all
search runs before manually removing a receipt after resolving the outcome.
Confirmed receipts also prevent automatically re-adding a previously submitted
but subsequently removed torrent. This conservative gate can need manual review.
It does not provide a distributed transaction with the client, exclude unrelated
client writers, or establish physical power-loss durability.

## Limits and verification

Result limit is 1–100, page size 1–100, max pages 1–10; timeout is 1–60 seconds.
Defaults are 50/25/4/15. Search and topic requests reuse shared one-second pacing.
HTTP transient retries are at most three attempts; long `Retry-After` values are
reported instead of shortened. Reads have byte and elapsed-read bounds (socket
read timeouts also apply), XML entities/DTDs and NUL-containing XML encodings are refused. Detail/JSON bodies are
limited to 2 MB (solver JSON envelopes to 4 MB), torrents to 8 MB, evaluator input to 200 KB, tracks to 200, and
individual public text to 4,000 characters with visible truncation. Discovery
caps, repeated pages and later-page failures are reported. Indexers may ignore
pagination; absence of more results does not establish exhaustive discovery.
Authenticated responses and download URLs are not persistently cached. Only
requested CSV files and submission receipts/locks are written; enrichment tags,
collection files and global configuration are not modified.

Offline tests use sanitized Torznab/topic fixtures and mocked HTTP boundaries,
including the real CLI-to-confirmation-to-client path. They do not verify live
Jackett/RuTracker authentication, every topic template, paid LLM availability,
or real torrent-client interoperability. No real downloads are submitted during
development verification. See [SEARCH_VERIFICATION.md](SEARCH_VERIFICATION.md)
for executed checks and remaining external limitations.

Primary integration contracts checked:
[Jackett Torznab](https://github.com/Jackett/Jackett/blob/master/README.md),
[FlareSolverr API](https://github.com/FlareSolverr/FlareSolverr#usage),
[FlareSolverr status and logging implementation](https://github.com/FlareSolverr/FlareSolverr/blob/master/src/flaresolverr_service.py),
[Jackett RuTracker implementation](https://github.com/Jackett/Jackett/blob/master/src/Jackett.Common/Indexers/Definitions/RuTracker.cs),
[Jackett download proxy](https://github.com/Jackett/Jackett/blob/master/src/Jackett.Server/Controllers/DownloadController.cs),
[Torznab specification](https://torznab.github.io/spec-1.3-draft/torznab/Specification-v1.3.html),
[qBittorrent Web API](https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-%28qBittorrent-5.0%29),
[OpenAI structured output](https://developers.openai.com/api/docs/guides/structured-outputs),
[Gemini generateContent](https://ai.google.dev/api/generate-content),
[Gemini structured output](https://ai.google.dev/gemini-api/docs/structured-output).


## Two-stage search (2026-10-09)

**Default behavior changed:** discovery is followed by conservative title filtering,
then targeted details for at most **five** potentially relevant topics. The detail
stage stops starting new requests after **180 seconds**. An in-flight bounded
request may finish later; this is a scheduling deadline, not hard cancellation.
All discovered rows, including mismatches, ambiguous parses and budget-skipped
topics, are exported before retrieval and remain in the final CSV.

Comparison parsing strips Unicode formatting marks and normalizes dash/space
variants without changing the retained `torrent_title`. Full original titles are
included in both LLM payloads. Structured artist/title filters compare identities
independently: Queen Bee/Queen Mab/The Queen's Cartoonists and Jazz Tour are not
Queen/Jazz. Missing or ambiguous parsing is not sufficient for early rejection.
Free-text-only queries can remain ambiguous; structured filters are recommended.

Priority defaults to the existing deterministic identity/completeness,
stated lossless/rip-evidence and availability preferences, with stable-ID ties.
`--preliminary-llm` optionally adds a priority-only evaluation using the configured
backend; it can reorder eligible/ambiguous topics but never delete unranked ones.
It is **off by default** and can add cost. `--csv-only` disables both LLM stages.
After acquisition the final evaluator receives merged title/description evidence
and unexamined candidates with their skip statuses. Without external sources its output is conditional
practical advice; with research enabled, a separate mastering-review decision is the primary result. It can abstain (`selected_candidate_id=""`, empty ranks/reasons)
while retaining mandatory uncertainty codes. Failed/refused/invalid/invented-ID
responses preserve deterministic ordering, CSV and manual selection.

The normalized fields are not independent verification. `claims` retains successive
source statements, including disagreements, with `search_title`, `topic_heading`
and `topic_detail` provenance. Optional external mastering research is a separate stage,
with public sources and an independent preference decision (see below).
Explicit label/catalog pairs, edition markers and year fragments survive parsing.
Multiple years in one label are retained in `years_stated` without assigning roles.
`recording_format` and `distribution_format` describe separate representations;
bit depth/rate use the stated distribution format when available. `rip_type`
(image+.cue/tracks) is separate from `medium` (LP/CD). Stated DR/log/CRC/AccurateRip
text remains **unverified**, including negative database outcomes. No playback,
audio decoding or independent rip verification takes place in search.

`FOUND_PUBLIC` means an accessible post was parsed, **not complete evidence**.
`evidence_completeness`, `extraction_gaps`, `unmapped_description_fields` and
`detail_diagnostics.description_characters` distinguish unavailable details,
partial recognized evidence and unmapped labeled text. Fields absent from recognized
accessible text remain unknown; this does not prove absence from the full topic.
Public responses can omit protected sections.

`--detail-budget N` sets the targeted count. `--detail-time-budget SECONDS` sets
its start deadline (1–3600 seconds). `--full-details` attempts every discovered
row, including preliminary mismatches, without a count cap or LLM reduction;
the explicit time deadline and discovery limits still apply. This also allows
checking a potentially misleading summary against its actual topic.

Example commands for **fish** (no downloads or paid model requests):

```fish
cd /home/emvis/Music/auph
# Fast, deterministic CSV: up to five potential topics, 180s start deadline.
env AUDIOPHILE_CONFIG=$PWD/search.local.toml .venv/bin/python audiophile_tagger.py search \
  --artist Queen --title Jazz --type album --csv-only \
  --detail-budget 5 --detail-time-budget 180 --csv /tmp/queen-jazz-fast.csv
# Complete detail acquisition within declared search/time bounds.
env AUDIOPHILE_CONFIG=$PWD/search.local.toml .venv/bin/python audiophile_tagger.py search \
  --artist Queen --title Jazz --type album --csv-only --full-details \
  --limit 100 --page-size 25 --max-pages 4 --detail-time-budget 3600 \
  --csv /tmp/queen-jazz-full.csv
# Optional final local-model recommendation (requires configured local server/model).
env AUDIOPHILE_CONFIG=$PWD/search.local.toml .venv/bin/python audiophile_tagger.py search \
  --artist Queen --title Jazz --type album --backend local \
  --detail-budget 5 --csv /tmp/queen-jazz-recommended.csv
```

For an additional LLM priority step add `--preliminary-llm`. Remote backends still
require explicit model/key and `remote_metadata_disclosure=true`. No selection
or recommendation bypasses the existing exact terminal download confirmation.

### Timing, load and cache

Requests remain serial and paced; no new executor/browser sessions are created.
`timings` records Jackett, preliminary filtering/evaluation, direct HTTP,
FlareSolverr, parsing, final evaluation and total search duration. Request
`duration_seconds`/`attempts` are separately retained in `search_diagnostics` and
`detail_diagnostics`. Shared stage timings on each CSV row are **not additive**.
Cached diagnostics describe original acquisition, explicitly marked as a cache hit.
Normal output is compact; technical evidence and diagnostics stay in the CSV.

Detail HTTP/browser calls are not automatically retried on transient failures;
existing failed cached clearance has at most one fresh browser attempt. LLM POSTs
are not automatically retried; unsupported schema mode permits the existing one
JSON compatibility downgrade. Jackett discovery retains its bounded transient
retry contract. A detail rate limit stops further topic requests in that run;
reported Retry-After is recorded without sleeping briefly and then hammering the
provider. All remaining topics stay visible as `SKIPPED_RATE_LIMIT`.
Configured RuTracker cookies **never** go to FlareSolverr.

Successful parsed details can be reused within a run. Set
`search.detail_cache_directory` to a separate cache directory to reuse them across
CLI runs; persistent caching is opt-in. `detail_cache_ttl` defaults to one hour.
The key includes parser version, exact topic/title, solver origin and a digest of
the configured authorization identity. Cache files contain sanitized parsed evidence,
not raw HTML, cookies or download URLs. Corrupt/stale entries cause a bounded fresh
request. Blocking/auth/parser/transient failures are not cached as NOT_FOUND.
`--refresh-details` ignores cache reads. This cache uses existing versioned JSON
publication helpers; it does not provide distributed exactly-once retrieval.

The original 25-result CSV and five public-topic text fixtures are covered in
`test_search_stages.py`. Measurement and verification evidence are recorded in
[SEARCH_STAGES_VERIFICATION_2026-10-09.md](SEARCH_STAGES_VERIFICATION_2026-10-09.md).

## Isolated Gemini diagnostics

Use the existing model, key and disclosure permission without Jackett, RuTracker,
FlareSolverr or a torrent client:

```fish
cd /home/emvis/Music/auph
.venv/bin/python check_gemini.py --config search.local.toml --stage minimal
# Optional separate check of the real evaluation/strict validation contract:
.venv/bin/python check_gemini.py --config search.local.toml --stage evaluation
# Both stages in one process, at most two generateContent requests:
.venv/bin/python check_gemini.py --config search.local.toml --stage both
```

Each stage makes one request with no retries or schema downgrade. These are live
API calls and may incur charges. `minimal` sends only "Reply with OK";
`evaluation` sends synthetic Example Artist/Album evidence, never library data.
The configured model is never changed. `.env` loading follows the tagger policy:
existing shell variables take precedence. `--config` overrides AUDIOPHILE_CONFIG.
Remote disclosure permission remains required.

All search `*_env` settings (including `gemini_key_env`, not the key itself) must
contain environment-variable names. Known key prefixes and invalid name syntax
are rejected without echoing values; missing-key errors identify the setting,
not its potentially sensitive contents. Custom valid variable names still work.

Evaluation failures now report a safe category, HTTP status when received,
allowlisted provider status/reason codes and parsed Retry-After seconds when
present. CSV adds `evaluation_diagnostics` with request duration/attempt count;
no provider messages, bodies, request headers or URLs are included. Categories
separate TIMEOUT, TRANSPORT_ERROR, AUTHENTICATION_ERROR, PERMISSION_DENIED/BLOCKED,
MODEL_UNAVAILABLE (404: verify both model and configured route), RATE_LIMITED,
QUOTA_EXHAUSTED (only explicit quota evidence), SERVER_ERROR, REQUEST_REJECTED,
INVALID_RESPONSE and LOCAL_VALIDATION_ERROR. No response/status is claimed after
a transport timeout. Rate limiting without explicit quota evidence does not
establish exhausted billing quota. Local validation failure means generated JSON
violated the candidate/evidence contract; it never approves a download.

Normal search retains its single schema-to-JSON compatibility downgrade for
400/422 request rejection; known authentication failures do not trigger it.
Transport, timeout, quota and server failures are not automatically retried by
LLM evaluation. CSV/manual fallback and exact-candidate confirmation remain in
place. A diagnostic timeout does not establish why an earlier run returned 5xx.

For a longer isolated check, pass `--timeout 60` to `check_gemini.py`:

```fish
.venv/bin/python check_gemini.py --config search.local.toml --stage evaluation --timeout 60
```

This overrides only the in-memory policy and HTTP client for that command;
configuration files and normal enrichment/search timeouts remain unchanged.
Requests receives separate connect/read limits: connect is capped at 5 seconds,
read uses the explicit value (1–60 seconds). This is not a total wall-clock deadline.
Output includes `timeout_phase` (`connect`, `read`, or `unspecified` when the
exception does not identify the phase); absent HTTP/provider codes are JSON null.

## Bounded Gemini generation and temporary controls (2026-10-12)

Gemini now sends `search.gemini_max_output_tokens` (default 2048, 1..8192).
Thinking can consume this budget; a truncated/invalid recommendation is rejected,
with CSV/manual selection preserved. Increase the explicit bound when justified,
without treating it as a latency or quality guarantee. `search.gemini_thinking_level`
defaults to empty (provider default); opt-in minimal/low/medium/high are
model-dependent and never silently downgraded. These settings do not establish
free quota or superior recommendations.

`check_gemini.py` accepts temporary `--model`, `--max-output-tokens`,
`--thinking-level`, and `--structured-mode` overrides; existing files are not
rewritten. Model override must be explicit. Each stage is one request with no
retries/schema fallback. `schema` sends responseJsonSchema; `json` requests JSON
MIME plus prompt instructions. Both still undergo the unchanged local semantic
validation. New CSV evaluation diagnostics retain actual model, evaluation attempt,
configured thinking level and safe local `validation_code`, not generated text.

The six-call October 12 comparison did **not** obtain a valid live recommendation;
see [GEMINI_VERIFICATION_2026-10-12.md](GEMINI_VERIFICATION_2026-10-12.md).
No permanent local model/timeout change or automatic model fallback was made.

Gemini contract refinement: per-evaluation schemas enumerate eligible candidate IDs
and bound arrays; model input supplies per-candidate allowed preference/evidence
pairs and an exact abstention example. Local semantic checks remain authoritative.
MAX_TOKENS is reported as OUTPUT_TRUNCATED with a safe finish_reason, separately
from malformed JSON and LOCAL_VALIDATION_ERROR. No truncated recommendation is
accepted and no output text is included in diagnostics. The later bounded live
schema check was rejected with HTTP 400 / INVALID_ARGUMENT; schema compatibility
for the configured model remains unverified. See the contract follow-up in
[GEMINI_VERIFICATION_2026-10-12.md](GEMINI_VERIFICATION_2026-10-12.md).

Schema-request failures can now retain bounded `provider_field_violations` in
checker/CSV diagnostics: known field paths and fixed or vocabulary-filtered
constraint descriptions, with reflected values and secrets redacted. A
request-level description does not identify an offending schema field. The latest
bounded live check still returned 400/503; structured-output compatibility is
unverified. See `GEMINI_VERIFICATION_2026-10-12.md` for exact observations.

For an isolated, reproducible Gemini investigation, `check_gemini_wire.py` captures
only the built-in synthetic evaluation request offline (`--prepare-only`) and compares
its JSON with the production adapter. Its independent client bypasses production HTTP
error parsing and saves meaning-preserving, targeted-redaction error reports only to
an explicitly specified directory outside Git/music, with files mode 0600. Terminal
output stays brief. `--transport production` exercises the actual strict evaluator;
`--omit-response-schema` is an explicit independent one-factor diagnostic control.
There are no retries, redirects or model switches, and at most four calls per report
directory. Remote charges may apply. No real album input is accepted by this tool.
See `GEMINI_VERIFICATION_2026-10-12.md` for fish commands, the preserved request and
actual two-call high-demand failures; this is not a validated live-integration claim.

## Preference by found mastering reviews

The extended path is Jackett → conservative relevance checks → bounded edition details →
external public forum search → edition/pressing matching → comparative LLM evaluation →
existing manual selection and explicit confirmation. **The primary result is preference by
found reviews, not objectively proven best sound.** The practical download choice remains
separate; seeds, codec and documented rip claims cannot change the preferred mastering.
No automatic download or enrichment is introduced.

The replaceable `ForumResearch` adapter supports Brave and Serper. Brave uses the documented
[Brave Web Search API](https://api-dashboard.search.brave.com/app/documentation/web-search)
(`GET /res/v1/web/search`, `X-Subscription-Token`, `q`, `count=10`). It does not use Gemini
memory or pretend Gemini has web tools. A separate Brave account/key/plan is required;
this does not establish free access. No Brave or LLM calls were made during offline
implementation verification.

Add these settings **inside the existing `[search]` section** of your selected policy:

```toml
research_backend = "brave"
research_disclosure = true
research_key_env = "BRAVE_SEARCH_API_KEY"
research_domains = "forums.stevehoffman.tv"
research_queries = 3
research_pages = 5
research_cache_directory = "/home/YOUR_USER/.cache/auph/mastering"
research_cache_ttl = 86400.0
```

Keep the configured LLM backend/model and `remote_metadata_disclosure=true` (or use your
configured local backend). Search disclosure is a **separate opt-in**: artist, title and
known edition identifiers are sent to the selected search provider. Forum URLs/excerpts/public usernames and dates
are sent to the evaluator under the existing model-disclosure opt-in. Keys belong only in
`.env` or environment variables; `*_key_env` contains a variable name. Research is disabled in the default/example policy until explicitly configured.
The current local policy now uses Serper; see the alternative setup below.

Fish, after configuring the selected policy and environment:

```fish
cd /home/emvis/Music/auph
# Optional secret entry without including the key in shell history:
read -s -P 'Brave Search API key: ' brave_key
set -gx BRAVE_SEARCH_API_KEY $brave_key
set -e brave_key
# Default: bounded candidate details, then external research and final recommendation:
env AUDIOPHILE_CONFIG="$PWD/search.local.toml" .venv/bin/python audiophile_tagger.py search --artist Queen --title Jazz --type album --backend gemini --csv /tmp/queen-jazz-mastering.csv
# All torrent details, retaining the time deadline; research has its independent bounds:
env AUDIOPHILE_CONFIG="$PWD/search.local.toml" .venv/bin/python audiophile_tagger.py search --artist Queen --title Jazz --type album --full-details --detail-time-budget 1800 --backend gemini --csv /tmp/queen-jazz-mastering-full.csv
# Existing metadata-only behavior, without external research:
env AUDIOPHILE_CONFIG="$PWD/search.local.toml" .venv/bin/python audiophile_tagger.py search --artist Queen --title Jazz --type album --no-research --csv-only --csv /tmp/queen-jazz-summary.csv
```

`--submit` still requires a terminal, an eligible manually selected candidate and the exact
`DOWNLOAD rutracker:<id>` confirmation. A mastering recommendation supplies neither selection
nor consent. EOF, decline and non-TTY refuse submission before download resolution/client login.

Research sends one general album/mastering-comparison query first, then known candidate
edition identifiers, in deterministic ID order. Bounds: 1–8 queries, 1–10 public page attempts,
10 search results/query, queries bounded to 600 characters/75 words, 1 MB/page,
12 recognized posts/page. Research requests are sequential,
spaced at least one second using the shared rate limiter, with existing connect/read limits,
TLS verification and **no retries**. 429 stops further calls to that service and retains
Retry-After in `research_diagnostics`; search rate limiting does not erase already discovered
forum pages. Redirects, query-bearing/authenticated URLs and hosts outside the exact allowlist
are refused. No RuTracker cookies, model keys or Brave keys are sent to forum pages. There is
no FlareSolverr/CAPTCHA/authentication bypass in this adapter.

Optional versioned cache stores only normalized public results/post excerpts, never full HTML
or credentials. Corrupt/incompatible cache records are refetched; failures and blocked/empty
post-context pages are not stored as successful posts. Successful empty search results can be
cached for the configured TTL. Cached evidence is not a fresh live observation.

XenForo public-post layouts are recognized (including quoted-text removal). Additional exact
forum domains can be configured; unrecognized markup yields `NO_POST_CONTEXT`, not fabricated
reviews. Public retrieval can be blocked. Snippets are retained with their page status, but
**snippet-only evidence cannot establish a preferred mastering**. No backend/key/consent gives
“Внешние отзывы не проверены”, preserving CSV and the existing practical/manual fallback.

Only short post excerpts (at most 25 words each), title context, post URL, public author and
available date are retained. Excerpts may omit nuance, replies and disagreement; `FOUND`
means recognized public post context, never complete research. Same-author replies and quoted
text do not count as independent support. Distinct accounts do not prove independence or
consensus. Recording-quality opinions alone, marketing terms and file technical properties
are not mastering comparisons. Current conservative English comparison signals can miss
other wording/languages; inspect the original links when evidence is insufficient.

The model returns the existing practical fields plus `mastering_review`: preference,
no-common-preference or insufficient-evidence; an exact source edition descriptor; literal
identifiers; preferred-edition sources; mapped candidate or empty ID; confidence; cited
comparative quotes, alternatives, reported direct-comparison status and limitations. Local
validation checks eligible IDs, saved source IDs, exact saved URLs and literal quotes/edition
identifiers; identifiers from a compared edition cannot move into the preferred descriptor.
The source context must contain the requested work; conservative matching can reject valid
unusual wording, in which case manual review remains available. Unsupported output is rejected,
not silently repaired. Instructions inside forum text have no tools or download authority.
Prompt isolation is not a proof that a subjective interpretation is correct.

Matching distinguishes album, pressing, mastering and rip:

- `unestablished`: missing catalog/medium match or any conflicting stated identifier.
- `possible`: catalog and medium match without conflict; catalog alone is never sufficient.
- `supported`: additionally matching matrix or mastering credit. This supports the **stated
  edition** only; it still does not prove unique mastering or audio/rip identity.

All candidates' matched/missing/conflicting features are saved. Different matrices sharing one
catalog are not merged. Missing identifiers are distinguished from an absent preferred edition;
`not_found` requires known conflicts across all eligible torrents. Edition years use explicitly
identified release years, not arbitrary original-album years from titles. If the preferred
edition cannot be mapped, the model may offer a separate practical download, or abstain.
Rip evidence/processing/availability remain per torrent, not inherited from a forum review.

New CSV fields preserve all previous columns: `matrix`, `mastering_credit`, `research_status`,
`research_diagnostics`, `external_sources`, `mastering_decision`. The latter retains status,
preferred candidate, practical candidate, comparisons, source references, matching confidence
and per-candidate gaps. Only canonical saved public evidence URLs survive nested URL redaction;
other URLs/secrets remain removed. Holland/Netherlands and LP/LP, Album, Gatefold compare as
equivalent without replacing original provenance. Real country/medium conflicts remain visible.

Live Brave compatibility, current forum markup/access and the expanded response contract on
real LLM models remain unverified; offline responses do not establish provider availability or
any mastering preference for Queen Jazz. See `SEARCH_MASTERING_VERIFICATION.md`.

### Serper alternative (2026-10-12)

Brave remains supported. Serper uses the official [Search API playground](https://serper.dev/playground):
`POST https://google.serper.dev/search`, `X-API-KEY` authentication and JSON `{q, num: 10}`;
public results come from `organic[].link/title/snippet/date`. This integration does not
use Gemini grounding or invent a search capability in the evaluator.

In the existing `[search]` section:

```toml
research_backend = "serper"
research_key_env = "SERPER_API_KEY"
research_disclosure = true
# Optional; independent of Brave's research_endpoint:
research_serper_endpoint = "https://google.serper.dev/search"
research_queries = 4
research_pages = 5
```

Set `SERPER_API_KEY` in `.env` or the environment, never in `research_key_env`. API access
and credits must already be available; this project does not enable billing. Disclosure
sends artist/title and edition identifiers to the configured search provider. The local
policy has been switched to Serper; its LLM model and all unrelated settings are unchanged.

Queries are ordered: album-wide discussions, exact **quoted catalog** lookups, then
explicit `expanded_edition` queries using known label/year/country/medium without the
catalog restriction. These last queries are broadened discovery, not successful exact
matches. The total query limit applies across stages; some catalog/expanded searches may
not run. Increase `research_queries` deliberately when more coverage is required. No
hidden retries occur on either backend. `discovered_by` records every discovery stage;
`summary_checks` and `content_checks` record artist/title/catalog mentions in the retained
search summary or post title/excerpt. Mentions are not edition/mastering verification.
Missing mentions remain false/unknown; nonempty search results never upgrade identity.
Strict source, quote, candidate and edition validation still governs the recommendation.

The normalized search cache includes backend and endpoint; changing between Brave and
Serper cannot reuse the other's response. Public forum page caches are shared. A page
failure retains snippets as preliminary evidence and cannot support a mastering preference.

```fish
cd /home/emvis/Music/auph
env AUDIOPHILE_CONFIG="$PWD/search.local.toml" .venv/bin/python audiophile_tagger.py search --artist Queen --title Jazz --type album --backend gemini --csv /tmp/queen-jazz-serper.csv
```

This command performs a new search and uses configured budgets; it is not the saved-CSV
validation harness. It never submits a download without the existing explicit confirmation.
`--csv-only` and `--no-research` preserve their prior behavior.

### Public forum context and evidence namespaces (2026-10-12 follow-up)

Forum transport is a **separate opt-in**, independent of RuTracker detail transport:

```toml
# Inside [search]; direct is the default:
research_page_transport = "flaresolverr"
# Empty default reuses existing flaresolverr_url or FLARESOLVERR_URL:
research_flaresolverr_url = ""
# Existing solver settings remain in effect:
flaresolverr_url = "http://127.0.0.1:8191"
flaresolverr_timeout = 30.0
research_pages = 5
```

The [FlareSolverr request.get API](https://github.com/FlareSolverr/FlareSolverr#usage)
receives only `cmd`, the approved public URL and `maxTimeout`. No credentials,
cookies, session IDs, search keys or CAPTCHA-solver configuration are sent. There
is no automatic direct-to-browser fallback. The service request has a bounded
read timeout of solver timeout + 15 seconds (connect at most 5 seconds), independent
of the global timeout. Login-only/challenge/CAPTCHA pages remain unavailable;
transport success does not establish readable posts. This route is for authorized
public reading only, not login or explicit access-denial circumvention.

`research_pages` now bounds **fetch attempts**, including each direct redirect hop;
requests are sequential and never retried automatically. Valid cached post contexts
remain usable after this network budget is exhausted. Sources not fetched stay
snippets, marked with their failure/budget status. Pages are prioritized using album
mentions and edition/mastering-comparison signals; song polls are demoted.

The only admitted query-bearing forum URL is exactly
`https://forums.stevehoffman.tv/goto/post?id=<digits>`: one numeric `id`, no extra
parameters/fragment, allowed host. Direct HTTP follows at most three redirects,
checking every target before sending, detecting loops and recording the chain.
Original and final public source links survive CSV export. A `/goto/post` URL uses
direct resolution even when the browser route is configured. FlareSolverr exposes
only its final URL, not the internal redirect chain: changed destinations are
refused, rather than claimed as validated redirects. Intermediate browser
navigation cannot be attested by this API; use direct resolution for redirecting
links. A canonical-looking unchanged browser destination alone is not proof that
no intermediate browser navigation occurred.

XenForo 1 `blockquote.messageText` is an original body and is retained; actual quote
blocks are removed. Album-specific posts are selected **before** the 12-excerpt cap.
General artist threads include labeled adjacent context; adjacent fragments cannot
independently support preference/opposition. Thread/post work checks require both
artist and album terms, without requiring them to be adjacent in a page title.
Extracts remain short and incomplete; authors are not assumed independent and quote
repetition is not new evidence. Cache identity includes the requested work.

LLM input explicitly separates:

- `practical_reason_options`: exact candidate/preference/literal-field-ID combinations;
- `forum_evidence_usage`: read-post IDs versus context-only/snippet IDs;
- `response_code_namespaces`: practical warnings/uncertainty versus forum limitations.

A forum source ID or URL cannot be used as a practical reason's evidence field.
Snippet-only input requires mastering abstention, even when a conditional practical
ranking is possible. `no_common_preference` also requires read post context; unknown
reviews are not evidence of disagreement. A misleading FOUND label on a snippet
cannot promote it into a read post. Local ID/quote/claim/uncertainty validation stays
strict; truncation remains `OUTPUT_TRUNCATED`. Safe `validation_details` report a
rejected reason index, known candidate/preference/field IDs and allowed fields, or
mis-scoped known warning codes. Arbitrary model strings become `[UNRECOGNIZED]`;
no claim text or raw response body is logged.

The follow-up check produced a **locally validated mastering abstention**, not a
mastering preference. Its forum inspection was limited by a validation-harness
error, recorded in `SEARCH_MASTERING_VERIFICATION.md`. The permanent local policy
still uses direct transport. A mode-0600 copy with an explicit forum transport
exists for a future separately budgeted run:

```fish
cd /home/emvis/Music/auph
env AUDIOPHILE_CONFIG=/tmp/auph-forum-context-validation-2026-10-12/search.forum.toml .venv/bin/python audiophile_tagger.py search --artist Queen --title Jazz --type album --backend gemini --csv /tmp/queen-jazz-forum.csv
```

This is a **new** production search, not an offline replay. It consumes configured
provider budgets. No download is submitted without `--submit` and the existing
explicit candidate confirmation. Do not rerun the exhausted diagnostic harness
as a way to obtain additional API requests.

### Warning codes and incomplete forum excerpts

Evaluation payloads include `response_field_enums` keyed by exact response paths:
`$.warnings[*]`, `$.uncertainty[*]`, and, when research is supplied,
`$.mastering_review.limitations[*]`. Input research/candidate warnings are data,
not permitted output codes. Unknown codes still reject the entire evaluation;
safe bounded code tokens are retained in validation diagnostics after secret
redaction, rather than replaced merely because they are outside a known vocabulary.

Forum parsing retains complete sentences/list items within a 75-word post budget,
instead of cutting a 25-word window mid-sentence. Multi-album list items are kept
separate; explicitly album-scoped units are retained when relevant. Overlong units
are omitted rather than presented as complete. `context_complete=true` means the
retained unquoted post units fit in this excerpt, **not** that the whole discussion,
quoted history, or mastering identity has been verified. Omissions are marked
`context_limitations=["post_units_omitted"]`. Page cache version is now v4; old
word-window excerpts cannot be reconstructed from cached excerpts alone.

For incomplete excerpts a prefer/against quotation must itself mention the
requested album; a thread title alone does not establish which album the opinion
concerns. Other excerpts may supply labeled context. Citing incomplete excerpts
requires `limited_sources`. An unidentified preferred edition must not be declared
absent from torrent candidates. A malformed practical choice invalidates evaluation
even when the model labels its mastering result `insufficient_evidence`; CSV and
manual selection remain available. Download confirmation remains mandatory.

## Mastering-first research

The default `mastering_pipeline=true` separates three decisions:

1. Extract editions, reported sound characteristics, direct comparisons and dissent
   from album-scoped original post text. Preserve full cleaned post context up to
   the existing 4000-character sanitization limit. Quotations, reply links, post IDs
   and dates are retained separately; another user's quotation is never a new vote.
2. Choose an edition/mastering family from validated opinions, without torrent
   availability or technical branding in the model input. Negative, hypothetical,
   hearsay and losing-side statements cannot support a positive preference.
3. Match that edition/family to eligible topics and choose a practical rip within
   that group. Supported identity precedes possible identity; ambiguous candidates
   matching competing families are withheld. Seed availability, completeness and
   documented rip preferences only break ties within the chosen family.

`mastering_rank` and `mastering_group_id` describe this recommendation.
`practical_rank` records the independent practical order. The CSV's
`mastering_decision` contains the opinion graph, source IDs, comparison spans,
feature provenance, rejected-record counts, rationale, limitations and both
recommended/alternative candidate IDs. An unavailable preferred edition stays the
preferred edition; another practical torrent cannot silently replace it. Selection
and download confirmation remain manual.

Edition identifiers can be inside the literal descriptor or supported by an exact
feature-evidence span in a single-edition context. A comparison's country, year,
catalogue or matrix cannot be transferred to its other edition. A shared mastering
family requires an explicit affirmative source statement linking both editions;
matching catalogue or engineer alone never merges them. This is a reported
relationship, not independently measured audio identity.

Extraction records are validated independently. An invented statement/identifier
is excluded, never repaired; unrelated valid evidence survives. Duplicate IDs are
all rejected to prevent redefinition. Partial evidence adds `limited_sources`.
The separate decision must cite all retained supporting and dissenting opinions;
account counts are not treated as consensus. No usable opinions means abstention.
The lexical checks are conservative necessary conditions, not complete natural
language understanding or a guarantee of listening quality.

With `research_adaptive=true` and a query budget of at least four, research first
searches broadly, then searches identifiers learned from read posts, reserves a
counter-evidence query, and uses remaining capacity for known candidates. This can
find editions unavailable among discovered torrents. Next thread pages share the
HTTP budget, including solver calls and redirects; prior pages/other threads are
not mistaken for pagination. Positive caches and per-run page reuse avoid duplicate
fetches. Raw HTML is not cached. Legacy parsed caches remain visibly limited to
whatever context they actually contain.

For a deeper run with the existing configured provider:

```bash
AUDIOPHILE_CONFIG=search.local.toml .venv/bin/python audiophile_tagger.py search \
  --artist Queen --title Jazz --backend gemini \
  --research-queries 8 --research-pages 10 --detail-budget 15 \
  --detail-time-budget 600 --csv /tmp/queen-jazz-mastering.csv
```

Free-text queries fill missing artist/title for research only when eligible release
claims agree on a single work. Explicit user identifiers are preserved; ambiguity
is not resolved by choosing the most popular topic.

The staged model requests respect `structured_mode` and make no hidden retries or
schema downgrades. Use `schema` when supported by the configured API, or `json`
with strict local value validation. `mastering_output_tokens` is separate from the
legacy practical evaluation's output budget. `mastering_pipeline=false` retains
legacy combined-response compatibility and its independent practical/mastering
fields; the default uses the new pipeline.

## Astra integration

Use the existing `[search]` section and an actual API key in the environment:

```toml
backend = "openai"
openai_model = "gpt-6-astra"
openai_api = "responses"
openai_reasoning_effort = "high"
structured_mode = "schema"
remote_metadata_disclosure = true
research_disclosure = true
# Optional autonomous discovery in addition to the configured Serper/Brave search:
research_openai_discovery = true
research_openai_model = "gpt-6-astra"
research_openai_tool_calls = 4
```

Responses requests use `store=false`. Optional web discovery restricts domains and
has a tool-call limit. Only real tool source URLs/citation annotations are accepted
as discovery links; model summaries never become forum reviews. Canonical forum
pages must still be acquired independently before supporting a preference. A
missing key/provider failure does not fabricate sources or discard Serper/Brave
results. Disabling research also disables this extra provider stage.

Official interfaces: [Responses web search](https://developers.openai.com/api/docs/guides/tools-web-search),
[Structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses),
[Gemini generateContent structured output](https://ai.google.dev/gemini-api/docs/generate-content/structured-output).

## Reproducible model comparison

`mastering_benchmark.py` contains 24 real album targets. Collection contacts only
public research providers/forum pages; it does not search RuTracker, download audio
or change the library. Each private snapshot freezes sanitized evidence, a synthetic
unidentified rip and its SHA-256 fingerprint. The same snapshot is supplied to each
model. Use different new output directories for independent runs:

```bash
.venv/bin/python mastering_benchmark.py collect --config search.local.toml \
  --output /tmp/mastering-corpus --cases 24 --queries 2 --pages 2
.venv/bin/python mastering_benchmark.py evaluate --config search.local.toml \
  --corpus /tmp/mastering-corpus --output /tmp/mastering-comparison \
  --models gemini:gemini-3.5-flash-lite openai:gpt-6-astra \
  --max-calls 96 --cases 24 --timeout 60
```

A request counter is persisted **before** every model call. No retries occur.
Auth/access/model/quota errors stop that model rather than spending calls on all
remaining albums. Missing keys are reported as `NOT_CONFIGURED`.

Reports distinguish accepted preferences, abstentions, direct comparisons, retained
opinions/dissent, provider failures and elapsed time. `expert_accuracy` stays null
until a snapshot has an independently reviewed `expected` annotation, for example
`{"status":"preference","acceptable_editions":["literal edition descriptor"],
"candidate_id":"..."}`. Synthetic rip mapping never proves a real torrent identity.
A successful contract or a confident answer is not an expert best-mastering score.
For deployment comparisons, freeze real candidate evidence as well and annotate
acceptable alternatives, contested cases and required abstentions separately.

## Offline quality review

The staged graph separates positive `endorse` statements from comparative `prefer`
statements. Endorsements are retained and displayed, but do not choose a mastering.
Historical preferences are context; source-level caveats remain visible. Unsupported
comparison or characteristic metadata is withheld independently, with bounded
record-level diagnostics. AI-generated summaries and explicitly republished
interviews cannot count as the forum author's own listening vote.

Album scope retains continuation paragraphs and preferred-version list identifiers.
Cache v7 avoids reusing old page subsets that may have omitted newly relevant posts.
Legacy cache compatibility is available only in explicit legacy mode. Read-only
`audit_mastering_quality.py` reviews frozen source data without API calls and binds
manual observations to the exact reviewed fingerprints. See
[MASTERING_QUALITY_AUDIT_2026-10-13.md](MASTERING_QUALITY_AUDIT_2026-10-13.md) for the
four recommendation verdicts and the nineteen abstention cases.

The benchmark also saves private, redacted parsed stage records in `stages/`,
including decisions rejected by validation. These are diagnostic model proposals,
not accepted source evidence. Original public source URLs remain in the corpus;
arbitrary URLs and credential values are removed from the diagnostic copies.
Source IDs selected for each model input are saved before generation.

Multichannel/mixed source context is explicitly qualified and cannot automatically
select a stereo rip from catalogue identity. See
[MASTERING_FOLLOWUP_2026-10-14.md](MASTERING_FOLLOWUP_2026-10-14.md) for the live
24-case comparison, five independent source reviews and real RuTracker checks.
The local search now uses pages/results of up to 100, with the same 15-description
budget: the former 25-item page lost an early Jazz CD among unrelated genre matches.
Reaching a bound still does not establish search completeness.

Artist identity missing from an album-specific thread can be established by a
retained original post naming both artist and album in that same thread. The
literal identity span is recorded separately from the review; quotes, snippets,
AI summaries and other threads cannot certify it. Short edition names require
their full descriptor/explicit definition in the same own post, with ambiguity
checks. A personal list heading is retained as a separate preference basis,
without neighboring albums' sound assessments.

Printed brand/catalogue references may produce `identity_reference_candidates`
for manual checking. They never fill a preferred edition's unknown features or
establish mastering equivalence. Catalogue-only checks do not automatically
recommend a rip. See
[MASTERING_IDENTITY_RESOLUTION_2026-10-14.md](MASTERING_IDENTITY_RESOLUTION_2026-10-14.md)
for live results, missed-source analysis and the remaining Flash-Lite limitations.

## Batched extraction and source accounting

`mastering_batch_size=8` is the default (1..100). Each selected source belongs to
one extraction batch. Claims can cite only that batch's source IDs; thread artist
identity can still use retained original anchors as identity evidence, never votes.
The common source and input budgets remain in force. With the default 40-source
cap, extraction uses at most five calls and decision at most one. Set the batch
size to 100 for a single-call comparison, including via benchmark `--batch-size`.

Validated graphs retain colliding model IDs with batch namespaces. Only identical
literal descriptors with identical features are coalesced; conflicting catalogue
identities and bare/full names are not merged. Mastering group IDs include edition
features. Reported equivalence links remain explicitly attributed.

An isolated malformed response or server failure preserves other batches. Auth,
quota, model access, call budget and three consecutive service errors stop remaining
calls. If all batches fail, evaluation fails with retained coverage diagnostics.
If a terminal failure interrupts a partial extraction, the graph is preserved with
an abstention. Graph capacity limits cause abstention rather than dropping dissent.

`extraction_coverage` records per-source outcomes, proposal/acceptance counts,
rejection rules and batch diagnostics. `no_opinion_proposed` means no retained model
record, not a proven neutral post. Benchmark persists coverage even when evaluation
fails and distinguishes unattempted batches from failed ones. See
[MASTERING_BATCHED_EXTRACTION_2026-10-14.md](MASTERING_BATCHED_EXTRACTION_2026-10-14.md)
for measured source retention, the quota-interrupted comparison and final checks.
