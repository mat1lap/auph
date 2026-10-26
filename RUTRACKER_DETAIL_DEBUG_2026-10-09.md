# RuTracker topic-detail debug — 2026-10-09

Scope: CLI/environment/policy selection → scoped Jackett discovery → separate
RuTracker detail HTTP transport → FlareSolverr API → HTML/title parsing → identity
gates → CSV. Reviewed `release_search`, `search_providers`, `search_models`,
`pipeline_config`, CLI environment loading, evaluator/client entry gates and
existing search tests. Earlier unrelated audit and feature changes were preserved.
No download resolution/submission was exercised live; no collection files were changed.

## Reproduction and precise cause

The supplied command initially exited 2 with `NOT_CONFIGURED`: this tool shell
had the API key and topic cookie, but neither `JACKETT_URL` nor
`AUDIOPHILE_CONFIG`. The existing `search.local.toml` contained the working URL.
Selecting it explicitly reproduced the reported failure:

```bash
AUDIOPHILE_CONFIG=search.local.toml .venv/bin/python audiophile_tagger.py search \
  --artist Queen --title Jazz --type album --csv-only --csv /tmp/queen-jazz.csv
```

Result: 25 topics, all `BLOCKED`; the Cartoonists release was incorrectly eligible.
A separate one-topic response inspection confirmed:

- HTTP 403 at `rutracker.org/forum/viewtopic.php`, `text/html; charset=UTF-8`.
- `Server: cloudflare`, `cf-mitigated: challenge`, “Just a moment” challenge markup.
- No redirect was followed. This was a blocking/challenge response, not a topic
  parser failure, malformed provider payload or evidence of an expired cookie.
- Detail retrieval used `HTTP.request(GET, topic, Cookie=...)` directly. It never
  used Jackett's existing solver integration; the project's policy had no solver option.

FlareSolverr was running on localhost port 8191 and its health endpoint returned
200. Some initial browser probes reached a topic; others produced challenge
timeouts after 30 seconds (confirmed with sanitized category/duration log output).
Authentication-cookie validity cannot be established from a Cloudflare denial.

## Confirmed defects and fixes

| Consequence / severity | Root cause | Correction |
| --- | --- | --- |
| All details blocked despite Jackett's solver; high acquisition impact | Detail requests bypassed the separate service | Explicit `flaresolverr_url`/`FLARESOLVERR_URL` integration, timeout validation, configured local endpoint; bounded documented API calls and matching clearance/user-agent handshake |
| Wrong work eligible; high relevance impact | Artist/title compared as sets of words | Parsed identities compare independently after Unicode/punctuation normalization; Cartoonists, Queen Bee and a different album containing Jazz are excluded |
| Genre/format strings become artist or prevent parsing; medium | Title regex began at the prefix and refused `/` in sample-format annotations | Balanced leading groups separated from identity; Unicode dash separators and year/format suffixes parsed conservatively |
| Explicit technical title claims lost; medium | Summary parser deliberately extracted no technical fields | Medium, codec and explicit sample-format claims retained as `search_title`, `title-derived, unverified`; no inferred CD rates, TR24 codec, mastering, years or completeness |
| Title codec falsely promoted to detail provenance; medium | Detail post-processing reused any existing codec | Derive topic lossless claims only from a topic-derived codec; preserve title provenance when detail lacks that field |
| Challenge/auth/parser failures indistinguishable; medium | HTTP error discarded status/response context | Structured sanitized HTTP status, host/path, content type, transport and controlled marker evidence; challenge/login/redirect/content/schema/parser/connection categories remain distinct |
| Accessible public post discarded; medium | Failed authenticated HTTP discarded independent browser result; inline login widget mistaken for login-only page | Exact-topic public post can be retained as `FOUND_PUBLIC` after HTTP 403; actual denial remains visible; explicit authentication errors, wrong topics and login-only responses cannot use fallback |
| Cookies retained in service logs; high privacy impact | Upstream FlareSolverr INFO logs incoming request bodies, including the documented cookie parameter | Final API calls never include configured credentials. Authentication goes only to the trusted topic host. Cookie components/issued clearance values are scrubbed from parsing and diagnostics |
| Misleading HTTP success; medium diagnostics impact | Upstream synthesizes browser status 200 and empty headers | Browser-reported status is separate from measured HTTP status; selected public-browser HTTP status remains unknown |
| Permanent failure retried after error-body read failure; medium | Diagnostic body exception entered generic network retries | Retain known permanent HTTP status; bounded error prefix read failure cannot turn 403 into transient retries |
| Stale clearance loses a candidate; medium | Cached clearance returned immediately on failure | A rejected cache gets one fresh bounded browser acquisition; successful clearance is reused without reusing another topic's body |

The initial cookie-bearing browser probes used the documented `cookies` field.
A presence-only check confirmed the existing INFO-level container logs retained
configured cookie values. No values were printed, copied to fixtures or exported.
The final transport avoids this field. Existing container logs were not altered
or purged; DEBUG/HTML service logging can also expose returned browser cookies.

## Regression evidence

An isolated `/tmp` source copy containing the original modules reproduced 12
failures in the new title/challenge regressions (`-k title`; ten title cases plus
two parametrized challenge cases containing a title). They passed with the fixes.
The new regression module exercises real production discovery, parsing, matching,
CSV and fallback wiring with only HTTP boundaries replaced. It covers malformed
solver responses, cookie domain/header safety, original cp1251 text, independent
candidate failures, public fallback gates, credential privacy, cache recovery,
non-retried forbidden bodies and inline login widgets.

The pre-change search suite passed 82 tests outside the sandbox. Inside it,
81 passed and its real-loopback HTTP fixture failed to bind a socket with
`PermissionError`; this was environmental and rerunning outside resolved it.
Final verification commands/results are appended to `SEARCH_VERIFICATION.md`.

## Bounded live final verification and limits

```bash
AUDIOPHILE_CONFIG=search.local.toml .venv/bin/python audiophile_tagger.py search \
  --artist Queen --title Jazz --type album --csv-only --limit 1 \
  --page-size 1 --max-pages 1 --csv /tmp/queen-jazz-final-diagnostics.csv
```

Exit 0; one topic (`rutracker:6892315`) exported as `FOUND_PUBLIC`. The selected
public post was at the exact requested URL and included an inline login widget.
Tentative title identity was Queen / Jazz; retained explicit claims were LP,
WavPack, 32f bit and 192 kHz. WavPack lossless mode and unsupported topic labels
remained unknown. The authenticated HTTP request still returned Cloudflare 403
with challenge evidence, even using clearance and the matching browser user agent.
Its response content type was HTML; public browser content type/actual HTTP
status remained unknown. Solver API HTTP 200 was recorded separately.

This verifies accessible public acquisition, not successful authenticated access,
cookie freshness, every topic template or consistent future challenge resolution.
No authentication/CAPTCHA workaround or new-cookie request was used. The service
was neither restarted nor reconfigured. No real music files/downloads were touched.

Primary contracts:
[FlareSolverr API](https://github.com/FlareSolverr/FlareSolverr#usage),
[upstream logger and synthetic browser status](https://github.com/FlareSolverr/FlareSolverr/blob/master/src/flaresolverr_service.py).

## Subsequent two-stage correction

The separate U+200E delimiter defect in topic 6224997, real-post field extraction
gaps, targeted acquisition, timings and cache verification are now covered in
[SEARCH_STAGES_VERIFICATION_2026-10-09.md](SEARCH_STAGES_VERIFICATION_2026-10-09.md).
The live 25-result refinement retains five eligible topics (including 6224997),
requests details for those five, and retains twenty explicit mismatches without
page requests. FOUND_PUBLIC continues to mean accessible data, not full extraction
or verified authenticated access.
