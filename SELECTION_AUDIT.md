# Lyrics and artwork selection audit — 2026-10-04

Current follow-up: [IMPLEMENTATION_BACKLOG.md](IMPLEMENTATION_BACKLOG.md) records the
2026-10-05 implementation and verification; older findings/counts below are historical.

Scope: the editable project in `auph`, starting at commit `f25fb71`. This audit
covers acquisition, candidate contracts, validation/selection, materialization,
and their tagger callers. It does not certify every upstream API or image
heuristic. Verification uses offline HTTP boundaries, synthetic images and
temporary FLAC files; the real music collection is not a test output.

## Execution map

Lyrics: `audiophile_tagger.process_single_track` → `inspect_local` →
`lyrics_utils.fetch_betterlyrics` / `fetch_lyrics_cascade` →
`invoke_lyrics_provider` (identity/config/parser-aware cache and typed outcomes)
→ adapter search/direct detail → candidate alternatives →
`lyrics_pipeline.provider_candidate` → `lyrics_quality` identity and text/timing
assessment → `choose_provider_result` → `materialize_provider_results` →
`materialize_selected` → album staging → `metadata_writer` and album commit.

Four concurrent level-one workers collect results; later provider levels also
contribute alternatives. Completion order is not a ranking criterion. Existing
usable synchronized local lyrics stop remote lookup. Curated local plain text
remains primary; remote lookup can complete missing forms only when suitable.
Raw provider source, native timing, translations and romanization have separate
archival outputs. Playback tags contain plain text/validated line LRC under the
existing writer policy; native TTML and enhanced LRC remain archival forms.

Artwork: tagger release evidence → `CoverFetcher` provider queries →
`CoverCandidate` original/preview representations and release/role evidence →
metadata gates → bounded tier-by-tier retrieval → decode, EXIF orientation,
measured visual properties, proposed crop → compatible duplicate grouping →
`_best_in_release_tier` → automatic/manual/category selection →
`prepare_best_cover` / `prepare_cover_choices` → exact displayed derivative →
staged `cover.jpg`, archival source bytes and embedding → album commit.
Standalone cover-fetcher previews are explicit outputs; tagger acquisition
previews are temporary. Multiple final renames are a recoverable transaction,
not an atomic album snapshot. Existing transaction/recovery limits remain as
documented in `AUDIT_LEDGER.md`.

## Confirmed defects and corrections

| ID | Severity | Trigger, root cause and consequence | Correction |
|---|---|---|---|
| S-01 | High | Real Unison cascade passed unsupported keyword arguments; mock adapters hid the TypeError and usable lyrics were never fetched. | Repair wrapper contract; pass stable instrumental evidence to LRCLIB instead. |
| S-02 | High | Unison, Genius, VK Topics and several scraper/API loops returned the first usable result or aborted on one detail failure. Better later matches vanished. | Collect bounded identity-compatible alternatives; isolate detail failures; retain successful items and partial-failure diagnostics. Applies also to Tekstowo, VK Music, PetitLyrics, Megalobiz, RentAnAdviser, Baidu/1ting, AmDm, MetalArchives, DarkLyrics, Bandcamp and Chorus. |
| S-03 | High | LRCLIB/Musixmatch wrappers overwrote full plain text with shortened synchronized text; short sync could pass agreement validation. | Preserve independent plain text, reject materially different/short sync for playback, retain provider source. |
| S-04 | High | Spotify/Deezer rejected same-recording compilation album differences; one failed lookup or bad timestamp discarded other tracks/plain text. | Let recording/version/duration evidence gate identity; isolate track failures and parse plain/line/word forms independently. |
| S-05 | High | Minimum confidence was applied only after selecting a lexicographic winner; an ineligible generated candidate suppressed an eligible alternative. | Retry ranking after removing below-minimum primary candidates. Separate identity-validated auxiliary translations/romanization retain their existing attachment policy. |
| S-06 | High | Lyrics conversion failure aborted enrichment after selecting a winner and could leave partial staged sidecars. | Isolate each materialization attempt, remove failed outputs, try the next eligible result. Publication filesystem failures still propagate to transaction recovery. |
| S-07 | High | Remote timing could be attached beside preserved local plain text with different words. | Compare with curated local text before adding timing; retain source evidence but reject disagreeing playback sync. |
| S-08 | Medium | Metadata-only TTML counted as local lyrics; untimed/malformed local TTML could suppress synchronization lookup. Embedded TTML also lacked local-preservation detection. | Require vocal paragraph text; preserve usable local/embedded text, but only validated complete timing stops remote lookup. |
| S-09 | Medium | Unison search identity hid conflicting detail identity; LRCLIB native fields and Chorus source charts were lost. | Prefer actual detail identity; retain stable IDs, LRCLIB response JSON/lyricsfile, original chart text or base64 MIDI source. |
| S-10 | High | Nonempty corrupt original image stopped representation fallback before decode. | Decode each representation before accepting it; continue to previews, record actual evaluated URL/kind, remove corrupt cache entries when possible. |
| S-11 | Medium | Small thumbnails preceded larger available previews; URL dedup discarded alternate representations. | Prefer supplied larger previews; merge representations only within compatible release identities. Ranking still uses decoded measurements. |
| S-12 | Medium | iTunes/Deezer/Bandcamp/Qobuz adapters invented larger CDN URLs and implied unsupported source resolution. | Use provider-supplied URLs unchanged, including signatures; remove misleading Deezer/Qobuz high-resolution labels. |
| S-13 | High | CAA discovery stopped at the first matching release group and did not adequately check artist identity. | Inspect all bounded matching groups, enforce returned artist evidence, retain actual group IDs. Exact release remains stronger evidence. |
| S-14 | High | Selected artwork conversion failure abandoned eligible alternatives; category preparation/standalone previews could abort before fallback. | Prepare privately, retry eligible candidates, revalidate deferred tiers, preserve existing output until success. Manual tagger failures return to selection rather than silently substituting another image. |
| S-15 | Medium | Larger retained source HTML/JSON could invalidate otherwise usable extracted lyrics under a text-only size limit. | Separate bounded source limit (5 million characters) from lyric-form limit (1 million). |
| S-16 | High | Clone Hero parser initialized title/artist from the query and ignored actual chart headers, allowing a wrong package to inherit search identity. | Retain actual chart identity, reject conflicts before marking it validated, continue to later packages; synthetic SNG integration verifies source and timing retention. |

A single remaining category already preserved a different valid automatic
winner; its regression verifies existing behavior rather than claiming a fix.
Parser cache version is now `2026-10-04-lyrics-v9`, invalidating earlier normalized
provider results. No provider was removed for this audit.

## Eligibility and ranking

Lyrics hard gates include conflicting recording IDs/ISRCs, wrong artist/title,
version conflicts, original-language conflicts, unusable text and malformed
playback timing. Album titles are corroboration, not recording identity. Missing
identity is uncertainty, not confirmation. Plain/sync disagreement removes
playback timing, not otherwise usable plain text or original evidence.
Ranking prioritizes identity/version/text before synchronization and source
confidence; duplicate copies do not vote. The configured minimum still applies
to primary candidates. Short legitimate songs remain subject to the existing
text-quality policy; this audit does not establish a universal completeness
classifier.

Artwork release conflicts and known non-front roles reject candidates.
Uncertain releases/roles retain explicit tiers. Release correctness precedes
visual quality; advertised dimensions, file size and provider identity are not
visual-quality points. Low-resolution images lose against usable same-release
covers under the existing resolution policy. Differences within two quality
points use role/provenance/stable-identity tie breaks. Digital-service origin
identifies a distribution source, not proof that the pixels were never scanned.
Unknown/local provenance is not labelled verified digital. Packaging/crop,
upscale and sharpness heuristics remain uncertain signals, not guarantees.
Only confident proposed crops are applied; prepared category previews and final
saved cover use the same derivative. Original source bytes remain separate.

## Provider acquisition matrix

All rows below were inspected in code. **No new live authenticated provider
compatibility check was performed.** “Fixtures” means offline contract/parser
coverage, not observed production responses. Search pages and detail limits are
bounded; this is not exhaustive pagination or crawling. Some existing providers
still expose only the supported forms documented below.

| Lyrics provider | Discovery / detail identity | Available and retained forms/evidence | Configuration; fallback; evidence |
|---|---|---|---|
| Unison | title/artist search; item ID detail; bounded 20 | plain, LRC/enhanced, native TTML when supplied; raw; actual detail title/artist/album/duration/stable IDs | public observed schema; per-item typed errors and alternatives; new cascade fixture |
| LRCLIB | title/artist/album/duration `/get`, search and item IDs | independent plain/sync, explicit instrumental, metadata; full item JSON including `lyricsfile` | public; failures distinct from empty; fixtures; bounded Lyricsfile 1.0 YAML consumption added in R06 of IMPLEMENTATION_BACKLOG.md |
| Musixmatch | identity-compatible track.search; track ID lyrics/subtitle/richsync, up to 5 | plain, line LRC, real richsync, original bodies/subtitle metadata, language/IDs/ISRC | explicit API key or desktop token; both configured routes retained; partial lookup fallback; old/new fixtures |
| Spotify | catalog search; track ID color-lyrics, up to 3 matches | plain/line timing, raw JSON, language/catalog identity/ISRC | explicit access token; private lyrics schema; fixtures only; independent track failures |
| Deezer | catalog search; ARL renewal; track-ID GraphQL, up to 3 | plain, line/word sync, translations, raw JSON, catalog identity/ISRC | explicit ARL; private schema; fixtures only; bad timing retains plain |
| Yandex Music | track search page 0; up to 6 ID supplements | plain/LRC, source JSON, language/version/catalog metadata | explicit token; private supplement schema; existing fixtures; newer upstream timing routes not certified |
| QQ Music | catalog search; song ID lyric lookup | plain/line/word timing, translation/romanization when supplied, raw QRC/LRC and identity | public observed routes; existing parsers/fixtures; no live claim |
| NetEase | cascade search; song ID lyric lookup | LRC/YRC, translations, romanization and original timing | public observed schema; existing fixtures; no live claim |
| Kugou | catalog/hash lyric discovery; candidate-ID/access key download | KRC real timing, line/plain derivatives, translation/romanization | observed routes; bounded candidates; existing fixtures; no live claim |
| Chorus / Enchor | chart search; MD5/version SNG package, up to 5 | chart/MIDI timing, original chart or base64 MIDI, package/search identity | public observed routes; package failures permit later candidates; synthetic SNG search/download/parse/selection/materialization fixture; no live verification |
| UltraStar | identity-bearing search rows; chart IDs, up to 5 | native karaoke notes and derived actual word/line timing | explicit cookie when needed; failed charts retain others; existing parser/adapter fixtures |
| PetitLyrics | XML catalog matches; native payload, up to 3 | plain/decoded source and catalog evidence | observed API; alternatives retained; existing fixtures |
| VK Music | audio.search; audio.getLyrics IDs | plain/source and audio identity | explicit VK token; per-item failures; existing fixtures, no live claim |
| VK Topics | authorized newsfeed up to 10 posts; public search up to 3 topics | plain/raw text and visible heading identity | API token optional; public route fallback; new later-topic failure fixture; first usable post per topic remains parser scope |
| VocaDB | catalog matches; stable song details | separate original/translation/romanization texts, language and song metadata | public API; existing fixtures; no live claim |
| Megalobiz | matching search/page links, up to 3 | source LRC/plain and visible identity | public HTML; alternatives/failure isolation; existing fixtures |
| RentAnAdviser | matching search/page links, up to 3 | synchronized/native markup and derived text | public HTML; alternatives/failure isolation; existing fixtures |
| Genius | multi-search; matched song IDs/pages, up to 20 | full page source, extracted plain, language/album/duration/ID | public observed route; enforce provider URLs, page failure fallback; old/new fixtures |
| Tekstowo | matching search links, up to 5 | plain/source and returned page identity | public HTML; later better result reaches materialization; new fixture |
| AmDm | matching result links, up to 2 | original chord sheet and parsed vocal text | public HTML; independent lookups retained; existing fixtures |
| Bandcamp | catalog/page links, up to 3 | native lyric/source text and semantic identity | public HTML; alternatives/failure isolation; existing fixtures |
| Baidu / 1ting | public indexed matches, up to 3 | plain/source and visible page identity | observed HTML routes; alternatives/failure isolation; existing fixtures |
| DarkLyrics | artist/album routes and track sections | plain/source and section identity | public HTML; retains matching routes, independent failures; existing fixtures |
| Metal Archives | catalog matches; stable song lyric IDs, up to 10 | plain/source with song/artist metadata | public observed AJAX; alternatives/failure isolation; existing fixtures |

Pre-existing diagnostic stubs (`syncedlyrics` aggregate, lyrics.ovh, AZLyrics,
SongLyrics) report `UNSUPPORTED_PROVIDER`; they were not removed or disabled in
this audit. Automatic generated candidates remain disabled by default.

| Artwork provider | Discovery / stable lookup | Retained representations/evidence | Configuration; verification |
|---|---|---|---|
| CAA | exact MusicBrainz release/RG; bounded MB search (5) | original, 250/500/1200 thumbnails, front/types/approval/comments, IDs and actual RG | public; new later-group/decode-fallback fixtures |
| Discogs | supplied release payload/ID | URI/URI150, primary/secondary role uncertainty, release metadata | token when required; existing fixtures; exact edition preferred |
| iTunes | bounded album searches (25 per query), collection ID | supplied artwork URL and collection identity; dimensions measured | public; supplied signed URL regression; artworkUrl100 is a thumbnail |
| Deezer | bounded album search (15), album ID | supplied cover_xl/big plus catalog identity | public; supplied URL regression; no invented 1800 URL |
| Local / embedded | filesystem discovery, FLAC front-picture role | original bytes/path, actual measurements, conservative provenance | no credential; existing interactive/EXIF/preservation tests |
| Amazon | ASIN from release relations/configuration | legacy generated ASIN representations, measured pixels; not verified originals | legacy URL availability unverified; existing low-resolution regressions |
| Fanart.tv | release-group MBID | supplied album art URLs and role evidence | explicit API key; existing fixtures; live unverified |
| Bandcamp | album autocomplete | provided image URL and catalog identity | public observed route; no invented image-ID/full-size URL; new fixture |
| Tidal | album discovery, UUID image route | legacy service URL and catalog identity, measured pixels | legacy token/API assumptions live unverified |
| Qobuz | search HTML | supplied image src and visible identity | public observed markup; no invented `_org` URL; fixture |
| TheAudioDB | album search | supplied HQ/thumb URLs and returned album/artist identity | current public/demo-key assumptions live unverified |

Primary references checked: [LRCLIB documentation](https://lrclib.net/docs),
[CAA API](https://musicbrainz.org/doc/Cover_Art_Archive/API),
[Apple search results](https://developer.apple.com/library/archive/documentation/AudioVideo/Conceptual/iTuneSearchAPI/UnderstandingSearchResults.html)
and [Apple search parameters](https://developer.apple.com/library/archive/documentation/AudioVideo/Conceptual/iTuneSearchAPI/Searching.html).
CAA documents distinct originals/thumbnails and representative RG art; Apple
documents artworkUrl100/60 as thumbnails, not arbitrary larger URL transforms.
Private/undocumented lyric routes remain fixture evidence, not official promises.

## Failure matrix and preservation

| Failure | Result / next step | Evidence |
|---|---|---|
| Missing auth/config | typed NOT_CONFIGURED/auth outcome; other providers remain eligible | existing token/adapter tests |
| Empty or unrelated search | NOT_FOUND only for a valid empty state; unrelated/ambiguous identity never promoted | existing identity/cache tests |
| Timeout/server error/parser/auth/block/rate limit | typed failure; not durable negative cache; independent providers/items survive | existing network/cache tests plus new batch failures |
| Corrupt/stale cache | envelope/parser validation rejects old data; corrupt image decode continues to another representation | existing cache tests, new image regressions; v9 invalidation |
| Invalid sync with valid plain | plain remains eligible, original malformed source archived | new LRCLIB/Spotify/Deezer/Musixmatch regressions |
| Original download or decode fails | try supplied previews largest first; evaluated URL/kind reflects actual bytes | new image fallback fixtures |
| All remote candidates fail | existing local content retained; no unsuitable replacement synthesized | new real FLAC tagger tests with/without local content and existing preservation tests |
| Selected lyrics conversion fails | isolated attempt discarded; next eligible candidate selected | new helper + real tagger/writer/decoded-PCM tests |
| Selected artwork conversion fails | private attempt removed; next eligible automatic/category candidate tried; manual tagger asks again | new preparation test and existing interaction tests |
| Final publication filesystem fails | propagate to transaction abort/recovery, not a fake provider fallback | existing transaction failure-injection tests |
| User cancel / quiet / non-TTY / dry-run | existing explicit interaction policy; automatic mode retains winner; dry-run avoids collection/tag writes | existing interactive/dry-run tests |

No blind identity relaxation follows failure. Source cache/temp writes are
separate from collection writes. Local-only content, nonfront embedded art and
curated lyrics retain existing preservation rules. Native TTML intervals and
word timestamps are not fabricated. Library-wide acoustic identity, all
provider languages, parser completeness on changing live HTML, crop accuracy
on arbitrary packaging, and every upstream pagination route remain unverified.

## Verification record

Baseline: `.venv/bin/pytest -q` — **486 passed in 100.92 s**.
The initial six new cases produced five failures before correction; the sixth
verified the existing single-category interaction contract. Later regressions
were added as confirmed adapter/materialization defects emerged.

Final focused command: `.venv/bin/pytest -q test_selection_audit.py` —
**35 passed in 1.11 s**. This includes controlled provider failures, reversed
candidate order, source-form retention, decoded-image fallback, isolated
materialization retries, and real tagger/writer FLAC integration with decoded
PCM equality. `.venv/bin/pytest -q test_selection_audit.py
 test_restored_lyrics_providers.py` ran at an earlier stage: **40 passed**;
subsequent new cases are included in the final focused/full runs.

`.venv/bin/pytest -q test_selection_audit.py test_audiophile_pipeline.py
 -k 'chorus or clonehero'` — **4 passed, 260 deselected**.
The full run before the final chart/TTML changes was **517 passed in 97.36 s**.
Final `.venv/bin/pytest -q` — **521 passed in 110.26 s**.

Final configured checks, all exit status 0:

- `.venv/bin/ruff check .` — all checks passed.
- `.venv/bin/ruff format --check .` — 37 files already formatted.
- `.venv/bin/mypy .` — no issues in 32 source files.
- `.venv/bin/python -m pip check` — no broken requirements.
- `git diff --check` — no whitespace errors.
- `.venv/bin/python audiophile_tagger.py --help`
- `.venv/bin/python audiophile_cover_fetcher.py --help`
- `.venv/bin/python lyrics_utils.py --help`

CLI help checks completed without provider acquisition. There were no missing
project modules blocking offline integration. Authenticated/private provider
compatibility, changing live HTML,
and legacy artwork URL availability remain unverified as described above.

Existing test expectations changed only for the intentional URL/label behavior:
provider-supplied Bandcamp/Qobuz URLs replace invented larger URLs. The Genius
fixture now uses the provider host (HTTP remains mocked), and the Musixmatch
single-credential test isolates `Path.home()` from real configured credentials.
The auxiliary-form regression found by full testing was fixed without weakening
its assertions.

The historical LRCLIB lyricsfile-only gap is resolved by R06 in
IMPLEMENTATION_BACKLOG.md; live LRCLIB YAML availability remains unverified.
