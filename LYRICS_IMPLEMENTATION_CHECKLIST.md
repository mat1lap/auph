# Lyrics implementation verification ledger

## 2026-10-04 audit update

Checked items below are implementation claims with fixture coverage, not proof
that every live provider currently works. This audit did not use account tokens
or perform authenticated provider smoke tests. Existing adapters remain enabled.
The Musixmatch documentation page could not be retrieved through the browser;
its adapter is covered by the existing offline official-API fixtures, while
live access remains unverified.

Enhanced LRC validation now applies `[offset:]` to word timestamps before
checking negative times and duration bounds. Original LRC/ELRC text is retained;
this does not invent word-end times or TTML intervals. Cleanup of embedded section
labels now queues exact original-tag archives inside the writer transaction;
failed validation publishes neither changed lyrics nor archive sidecars.
Dangling sidecar symlinks reserve their filenames and are preserved. Provider
exception diagnostics redact common URL credential fields and bearer tokens;
new cache envelopes store a key digest instead of raw request keys. Historic
cache files have not been migrated or deleted.

This ledger maps the lyrics-specific findings in the saved original audit
(`codex-session-01a0af40-f7b1-7af0-b527-cbd80d25aa9f.md`, section E and the
lyrics tests in section J) plus the user's selected storage policy to the
implementation. The release-metadata findings in that saved report are out of
scope for this lyrics implementation.

## User-selected policy

- [x] Section labels do not appear in playback-facing `LYRICS`; exact provider
  text, source LRC, and existing tag values are retained in archival sidecars.
- [x] Standard LRC is the synchronized playback representation; source/native
  and enhanced forms remain sidecars when available.
- [x] Available original, source, enhanced, native TTML, raw-provider,
  translation, and romanization forms are preserved as sidecars. Original
  lyrics are also embedded; translations and romanizations use separate
  `LYRICS_TRANSLATION` and `LYRICS_ROMAJI` project tags.
- [x] Language metadata is retained separately where supplied, including
  `LYRICS_LANGUAGE`, `LYRICS_TRANSLATION_LANGUAGE`, and
  `LYRICS_ROMAJI_LANGUAGE`. These fields are project conventions, not a promise
  that every player recognizes them.
- [x] Existing local lyrics and sidecars are preserved by default; online
  lookup may add a missing form but does not overwrite the curated value.
- [x] Generated ASR/alignment output is opt-in and cannot automatically
  replace provider or local lyrics.

## P0 — wrong lyrics, corruption, and destructive storage

- [x] No provider-order winner: `lyrics_utils.fetch_lyrics_cascade` collects
  candidates; `lyrics_pipeline.choose_provider_result` and
  `lyrics_quality.select_best` select after identity and quality checks.
- [x] Reject conflicting ISRC/recording IDs and semantic version labels;
  compare title, artist, album, duration, and version evidence.
- [x] Preserve raw provider text separately from parsed, normalized, and
  serialized derivatives; content-addressed sidecars and manifests record
  hashes, provenance, transformations, and parser version.
- [x] Preserve Unicode, punctuation, repeated lines, blank lines, and section
  labels in archival text. Comparison-only folding is separate from storage
  normalization.
- [x] Remove LRC-to-TTML timing fabrication. TTML is archived only when native;
  source formats with real word intervals may produce enhanced LRC/TTML.
- [x] Keep and archive existing lyrics; add remote tags/forms only when absent.
  Do not rewrite a live `.txt`, `.lrc`, `.elrc`, or `.ttml` sidecar.
- [x] Playback label cleanup is limited to standalone/prefix section labels;
  exact source text remains in sidecars/raw evidence.
- [x] Embedded-only forms are materialized as collision-safe sidecars only
  outside dry-run; malformed timed data is archived as raw and never presented
  as valid plain playback text.
- [x] Propagate provider identity and alternative candidates through NetEase,
  LRCLIB, Yandex, Kugou, Musixmatch, Unison, and the aggregate cascade.
- [x] Keep identity-less or brittle diagnostic-only routes explicitly
  unsupported: `syncedlyrics`, Lyrics.ovh, AZLyrics, and SongLyrics return
  `UNSUPPORTED_PROVIDER` rather than silently competing with validated
  results. Spotify and Deezer are restored as configured provider adapters;
  they participate in candidate matching and selection (see provider notes
  below).

## P1 — matching and validation

- [x] Structured `LyricsCandidate`, `LyricsDocument`, `LyricsLine`, and
  `LyricsAssessment` represent identity, forms, exact milliseconds, source
  precision, warnings, transformations, and independent quality dimensions.
- [x] Candidate match, version, text, sync, completeness, and source confidence
  remain distinct; source reputation is a weak prior only.
- [x] Album agreement strengthens identity; album conflict is visible and a
  modest penalty, not a hard rejection that would exclude compilation copies.
- [x] Version signatures read explicit fields and delimited/suffix markers;
  ordinary title words are not stripped or rewritten.
- [x] Validate line and enhanced LRC timestamps for syntax, sign, ordering,
  offset, duplicate density, coverage, duration, and payload bounds.
- [x] Long intros/gaps remain warnings, not automatic failures. A malformed
  sync representation can still contribute validated plain text.
- [x] Compare synced/plain forms using a comparison-only normalization view;
  detect material disagreement and suspicious incompleteness conservatively.
- [x] Very short non-empty vocal text remains assessable with a completeness
  warning and conservative score; empty or structural-only results are rejected.
- [x] Collapse duplicate/near-duplicate text for selection without counting
  copies as independent votes or changing stored text.
- [x] Correct, high-match plain lyrics can beat weak or invalid synchronized
  lyrics; good sync is a secondary quality dimension.
- [x] Provider-supplied instrumental status is distinct from `NOT_FOUND` and is
  accepted only for explicit, recording-matched LRCLIB evidence. Existing local
  lyrics prevent a remote instrumental claim from overriding local state.

## P2 — acquisition, scraping, caching, and maintainability

- [x] Typed outcomes distinguish `NOT_FOUND`, `TRANSIENT_ERROR`,
  `RATE_LIMITED`, `BLOCKED`, `AUTHENTICATION_ERROR`, `PARSER_ERROR`,
  `INVALID_RESPONSE`, `NOT_CONFIGURED`, `UNSUPPORTED_PROVIDER`, and
  `INSTRUMENTAL`.
- [x] Bounded response reads, strict charset decoding, inert HTML parsing,
  JSON/schema validation, and explicit parser diagnostics protect against
  malformed and oversized provider input.
- [x] Retry/backoff is bounded; `Retry-After` is honored; host pacing is shared;
  permanent parser/schema errors are not retried as network failures.
- [x] Only valid positive provider results and trustworthy empty API responses
  are cacheable. Transient/parser/rate-limit failures are not negative cached.
- [x] Cache identity includes provider, adapter, title/artist/album/version,
  duration, stable IDs, and language; parser/schema version invalidates stale
  serialized responses.
- [x] `provider_cache.PARSER_VERSION` is `2026-09-25-lyrics-v7`.
- [x] Content-addressed sidecars prevent collisions and preserve curated files;
  conversion manifests now include schema and parser versions, evidence,
  transformations, and artifact hashes.
- [x] HTML provider fixtures cover Genius, Bandcamp, DarkLyrics, Metal
  Archives, PetitLyrics, Megalobiz, RentAnAdviser, Tekstowo, AmDm, 1ting,
  VK Topics, and UltraStar. API/parser fixtures cover Musixmatch, QQ, Kugou,
  Yandex, NetEase, VocaDB, Unison, Chorus chart parsing, and the restored
  Spotify and Deezer adapters (`test_restored_lyrics_providers.py`).
- [x] Challenge/block results are classified and surfaced. No CAPTCHA,
  authentication, or access-control bypass was added.
- [x] Fragile adapters without identity evidence or usable diagnostics are
  explicitly unsupported rather than returning unattributed lyric strings.

## P3 — explainability and verification

- [x] Verbose diagnostics show identity/version, duration, text, sync, source,
  warnings, rejection reasons, and selection without logging full lyrics.
- [x] Transformation history records transport normalization, parser
  conversions, label display cleanup, and provider-specific cleanup.
- [x] Section E pipeline/transform/provider/scoring recommendations and the
  lyrics-specific section J regression list were re-read and checked against
  final code.
- [x] Regression tests cover wrong artist/version/ID/duration, provider bias,
  plain-vs-sync selection, malformed timestamps, Unicode/layout preservation,
  negative caching, scraper/API fixtures, restored-provider authentication and
  rich forms, and sidecar/tag preservation.
- [x] Final architecture review found no remaining LRC-to-TTML fabricated
  precision and no generic line cleanup applied to stored lyrics.

## Original audit transformation findings — final disposition

| Audit finding | Final disposition |
|---|---|
| BOM and line-ending cleanup | Transport-only; archival provider evidence is retained; serialized sidecars use UTF-8/LF. |
| LRC metadata headers | Parsed as metadata and omitted only from plain-text derivatives; raw/source LRC remains. |
| Timestamp sorting | Removed; original row order is preserved and regressions fail sync validation. |
| Generic credits/URLs/watermarks/noise deletion | Removed from generic lyric cleanup; only known provider-structure cleanup is applied. |
| HTML entities/tags | Decoded/extracted in provider-specific HTML parsers; raw response/evidence remains separate. |
| Section labels | Retained in sidecars and raw evidence; excluded only from playback `LYRICS` by selected policy. |
| Chords/repetition markers/choruses | Preserved in stored lyric text; optional alignment-only derivative may omit non-vocal notation. |
| Timestamp removal | Used only to derive separate plain text; synchronized source remains available. |
| LRC to TTML | Refused because LRC lacks line ends/document extent; no fake durations. |
| TTML roles/language | Native TTML is preserved; parsed text keeps vocal wording; no hard-coded English language tag. |
| Blank-line removal | Removed for lyrics serialization; stanza boundaries survive. |
| Existing `LYRICS` label cleanup | Original values are content-addressed to an archive before the policy derivative is stored. |
| Existing sidecar overwrite | Prevented by live-path checks, hashes, and collision-safe provider-specific paths. |

## Inapplicable or intentionally unsupported

- LRC cannot be converted to semantically faithful TTML because it has no
  source line-end timing. The converter returns an empty result by design.
- Spotify and Deezer are enabled with distinct acquisition stages and explicit
  user credentials. Spotify uses its documented Web API for track identity
  and an observed, private Web Player color-lyrics route for lyric payloads;
  its access token must be explicitly configured. Deezer uses its catalog API
  for identity and an observed, private web GraphQL schema for lyrics; its ARL
  must be explicitly configured. The private response schemas are not stable
  public API contracts, so schema/authentication failures are reported rather
  than converted to `NOT_FOUND`. Offline fixtures verify parsing and failure
  handling, not current live-service availability. No cookie harvesting,
  authentication bypass, or access-control circumvention is implemented.
- The identity-less Lyrics.ovh endpoint, fragile positional AZLyrics/SongLyrics
  scrapers, and opaque `syncedlyrics` aggregate remain explicitly unsupported.
  Other supported providers are not implicitly treated as independent sources
  when their lyrics are duplicates.
- Vorbis comments have UTF-8 values and allow nonstandard field names, but
  synchronized lyrics and the requested translation/romanization names are not
  universally mapped by players. The project keeps LRC sidecars and embeds the
  selected compatibility tags explicitly per user policy.

## Final verification

Latest source verification (2026-09-26):

- Full suite: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider` — 431 passed.
- Ruff lint: `.venv/bin/ruff check .` — passed.
- Ruff format check: `.venv/bin/ruff format --check .` — passed, 33 files already formatted.
- Mypy: `.venv/bin/mypy .` — passed, 28 source files.
- The full suite includes `test_restored_lyrics_providers.py`, `test_pipeline_safety.py`,
  and FLAC tag/sidecar integration tests. Provider network access is mocked in
  deterministic tests; these results do not claim live endpoint availability.
