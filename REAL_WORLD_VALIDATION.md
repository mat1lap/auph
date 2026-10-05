# Real-world validation — 2026-10-05

Started at e32fbc5 (including 663992b), with a clean working tree. Read
IMPLEMENTATION_BACKLOG.md before testing. Full independent album copies,
scripts, sanitized logs, inventories, provider summaries and viewable comparisons
are retained in `/home/emvis/flac-validation-20261005`; reports are in `reports/`.
No original album was enriched. No global policy or credentials were changed.

## Sample and isolation

The located collection, `/home/emvis/Music/Queen`, contains partial single-disc
albums only. The request for another complete/multi-disc album location remains
unanswered. These cases therefore **do not verify real complete-album or
multi-disc operation**. Synthetic multi-disc regressions complement the real
sample without replacing that missing validation.

Selected Innuendo (5/12 tracks), Made in Heaven (3/13 tracks), and the Japanese
News of the World folder (2/11 tracks). All contain existing embedded/front
artwork and local plain/LRC/TTML lyrics. The sample includes noncontiguous
positions, edition conflicts, packaging scans and a Unicode path.

Copied regular files using shutil.copytree, without hard links; the originals
had no symlinks. Production Python/policy files were independently copied to
`runtime/`, excluding .env, caches and personal session transcripts. Existing
credentials were read into subprocess environments without logging their values.
The original virtualenv interpreter executed those modules. Script-local API
caches, XDG lyrics caches and TMPDIR were redirected inside the workspace.
CLI working directories and explicit album arguments resolve to the copies,
including the relative curated Discogs mappings. Reports/lyrics/art/journals
and provenance were generated beside these disposable files. Source hashes
and runtime digests identify the pre-fix and corrected implementations.

Recorded SHA-256 of every original sample file, tag inventories (lyric content
represented by lengths/hashes), embedded picture hashes/roles/MIME/dimensions,
STREAMINFO and decoded native-rate/channel PCM as ffmpeg `pcm_s32le` hashes.
Rechecked every original sample file at completion: unchanged.

## Album results

| Album | Modes | Identification and enrichment | Preservation / repetition |
|---|---|---|---|
| Innuendo | Production automatic dry-run/write/repeat; fresh corrected copies dry-run/write/repeat; default artwork menu in a PTY; controlled HTTP/materialization failure copy | Discogs 2467591; corrected MusicBrainz selection 363808e4-089f-41fa-b1ad-eddbd1cab3c4, CERTAIN, margin 2550. All five positioned AcoustID matches agree. Recording and release-track IDs are distinct and preserved in their proper namespaces. Album remains INCOMPLETE. | Dry-run unchanged; both automatic repeat phases unchanged. Local lyrics/pictures preserved automatically. Interactive choice 2 applies the displayed 2976×2976 crop to a 3387×2976 packaging source; 1400×1400 embedded derivative agrees visually. |
| Made in Heaven | Production and corrected automatic dry-run/write/repeat; all remote image retrievals failed in a controlled separate copy | Discogs 367690; corrected MusicBrainz 97a776fd-9b24-4ada-9f52-81ef96ccf4b9, VERY_HIGH, margin 330; three positioned AcoustID matches. Other editions are retained/evaluated, not accepted solely by provider order. INCOMPLETE. | Dry-run unchanged, repeat unchanged. Existing local cover, embedded artwork, curated lyric tags and sidecars preserved, including when remote images fail. |
| News of the World, Japanese folder | Production/corrected automatic dry-run/write/repeat; separate fresh copy with lyric comments/sidecars intentionally removed; acquisition repeat | Discogs 3872011; MusicBrainz release selection withheld. Existing MBID 0060992e-439c-34b0-9aeb-9df1dcef23f3 actually describes US MFSL UDCD 588 (1993), linked to Discogs 1336781, conflicting with Japanese 32XD-409/local barcode. Original curated identifiers remain untouched; this is a manual review case, not a verified edition. Fresh lyric acquisition selected LRCLIB IDs 3704898/3704904. | Dry-runs unchanged; normal and acquisition repeats unchanged. Original curated copies retained their lyrics; newly acquired copy retains byte-exact selected native YAML, provider JSON, plain and LRC sidecars. All decoded PCM unchanged. |

All three groups remain INCOMPLETE due to noncontiguous positions; no full-album
ReplayGain is calculated. Existing valid track measurements were preserved and
stale REPLAYGAIN_ALBUM_RANGE was removed. A separate fresh News copy with
existing ReplayGain tags removed exercised the real `/usr/bin/loudgain -r -k -s e`
track-only scan: −4.12 dB/0.786102 and −2.33 dB/0.761731, no album gain tags,
exit 0 and identical decoded PCM. DR reports/track values were inspected;
News' localized report header fails identity validation and track DR is measured
again (DR10/DR11). The aggregate DR10 describes the two processed files, not an
independently established whole-release measurement. No proprietary meter
agreement is claimed. Original reports, when preserved, are not independently
certified merely by their presence.

## Providers, lyrics and artwork

Credentialed Discogs requests and public MusicBrainz/LRCLIB requests returned
200. Current public LRCLIB responses include native Lyricsfile YAML as well as
plain/LRC. In the acquisition copy the returned collections contained 34 and
32 entries, including failures/duplicate discovery routes; these are **not**
counts of independent confirmations. Useful candidates included NetEase YRC/LRC,
LRCLIB, QQ, Genius, Tekstowo, PetitLyrics and RentAnAdviser. Word timing and
translated/romanized forms appeared on some alternatives; their presence does
not prove their correctness or authorize merging unrelated forms into a winner.
`live-provider-matrix.json` records the observed identities/forms/failures.

Observed failures included Spotify 401, rejected Deezer ARL, Yandex 451, VK API
method blocking and flood control, Metal-Archives 403, Kugou certificate hostname
failure, DarkLyrics connection refusal, and Bandcamp/1ting parser failures.
Musixmatch returned unrelated identities and Chorus hit the payload bound.
These were retained as distinct failure states and did not prevent usable
independent providers from winning. Unsupported/unvisited authenticated routes
remain unverified; no access controls were bypassed.

Selected LRCLIB results retain 29 timestamps (8.06–172.04s, 179.47s track) and
37 timestamps (5.54–209.53s, 272.73s track), ordered and within duration. Native
YAML, plain and LRC outputs match retained provider forms exactly; provider IDs
and output hashes agree with committed provenance. No TTML/word timing was
invented for these selections. Plain text normalized similarity to prior curated
text is 0.984/0.988; differences include line wrapping/punctuation, so similarity
alone is not proof of complete transcription. Original curated copies remain
byte-for-byte preserved. No full copyrighted lyrics are copied into this report.

Visually inspected baseline local/embedded comparisons; the interactive digital,
local-crop and unknown-source comparison; final prepared/saved/embedded images;
and injected materialization fallback images. Innuendo source contains packaging
on the right; the selected crop removes it and preserves the front composition.
Digital Deezer artwork has a different white background, whereas local/CAA scans
have paper texture. CAA is labeled UNKNOWN, not promoted to digital. Made in
Heaven and News show suitable front artwork without visible fingers/packaging.
Measured dimensions and rejected back/CD-art/version candidates appear in logs.
This is visual evidence for these images, not a calibrated heuristic accuracy claim.

Interactive preview equals saved cover bytes; source bytes remain separately
archived; provenance records the actual crop/hash and a local source. Embedded
artwork uses the configured 1400px compatibility derivative, rather than the
archival source. Viewable comparisons are `baseline-artwork-comparison.jpg`,
`interactive-previews/comparison.jpg`, `interactive-final-agreement.jpg` and
`injected-fallback-comparison.jpg`.

## Injected fallback evidence

Injection occurs at Requests HTTP responses and file-open boundaries while
production discovery, evaluation, ranking, preparation and commits execute.
Fresh copies isolate each scenario.

* CAA original 28543392438.jpg receives 404. Its real advertised 1200px thumbnail
  is downloaded/decoded; candidate dimensions become 1200×1054, representation
  `thumbnail`, evaluated_url ends `-1200.jpg`, with a fallback warning.
* First `.cover-materialize-*` write raises OSError. Production rejects that
  materialization and prepares another eligible local source. The resulting crop,
  saved/embedded artwork and archival original were inspected; PCM/lyrics survive.
* Every successful remote image response is replaced by 404. Production retains
  the only usable local cover; existing lyric sidecars and decoded PCM survive.

An initial injected Response lacked a proper consumed-body state and its close()
raised AttributeError. Those invalid-fixture runs are explicitly separated in
`reports/injections-invalid-fixture/` and are not evidence for HTTP fallback.
Corrected injections were rerun; they completed with exit 0.

## Independent readback and player limits

Used metaflac tag export, ffprobe stream properties, Pillow actual image decode,
ffmpeg PCM comparison and direct artifact/provenance hash checks. Final inspected
scenarios have no abandoned transaction directories. All ten selected original
tracks and every original sample sidecar/cover remain unchanged. Unrelated and
multivalue tags are preserved except independently justified enrichment and
stale album-range cleanup in the copies.

Installed mpv renders the embedded News cover to a viewable JPEG (exit 0,
--no-config, --ao=null, --vo=image). A separate paused IPC check at 10 seconds
loads the external LRC subtitle track and returns text matching the expected
cue. The controlled IPC quit exits 4, so this is evidence of parsed cues, not a
claim of successful normal player completion. The image output driver did not
render subtitle overlays. Actual Kitty terminal rendering (rather than the captured PTY/prepared images),
personal GUI configuration, plain lyric display and
reading SYNCEDLYRICS from embedded tags remain unverified. Manual check: open a
workspace copy in the preferred player, confirm cover/colors and track/disc
labels, then verify plain lyrics and synchronized highlighting throughout the
song, including repeat lines and the final cue. Do not test against originals.

## Confirmed implementation fixes

| ID | Root cause and consequence | Fix and regression |
|---|---|---|
| RW01 — high | AcoustID keyed by sorted file-list index; partial albums/multiple discs compare recordings to the wrong release positions, creating false conflicts. | Production carries explicit (disc, track) evidence; identity scorer checks all media by those positions. Real Innuendo changes from 1/5 to 5/5 matches. Integration verifies final release/recording/release-track tags; multi-disc negative check retains wrong-recording rejection. |
| RW02 — high | Medium layout required present file count to equal release total, rejecting otherwise supported partial editions. | Only an explicitly positioned, duration-compatible subset is eligible; unknown positions, extra tracks and duration conflicts remain rejection gates. Complete is true only for exact per-disc counts. Real accepted albums remain INCOMPLETE. |
| RW03 — medium | Worker overwrote resolved release totals with number of local files. Dry-run proposed 5/3/2 instead of 12/13/11. | Prefer validated MusicBrainz medium counts or contiguous explicit Discogs release positions, then single-disc physical count evidence; do not override known totals with a partial folder count. Production dry-run regression reproduced wrong proposals before correction. |
| RW04 — low | Dry-run logged “Writing” and said DR14 skipped by an option the user never supplied. | Log “Planning” and the actual skip reason; regression runs the production dry-run path. |

Two corrected integration reproductions failed before fixes (the initial test
ledger assertion used the wrong container shape and was corrected before counting
that reproduction). The dry-run diagnostic case also failed before its fix.
Six focused integration/negative-gate cases now pass.

## Commands and results

Run from `/home/emvis/Music/auph` unless indicated:

* `.venv/bin/pytest -q test_real_world_regressions.py`: 6 passed in 1.18s.
* Related suites before the diagnostic addition: 87 passed in 5.08s.
* `.venv/bin/pytest -q`: final 645 passed in 110.31s (earlier intermediate 644 and 645).
* `.venv/bin/ruff check .`: passed.
* `.venv/bin/ruff format --check .`: 48 files already formatted.
* `.venv/bin/mypy .`: passed, 40 source files.
* `.venv/bin/python -m pip check`: passed.
* `git diff --check`: passed.

Live drivers (all use `/home/emvis/Music/auph/.venv/bin/python`):
`validate.py prepare/dry/auto/repeat/originals`, `interactive.py`, `corrected.py`,
`injections.py`, `final_checks.py`, and `player_check.py` in the validation root.
Underlying CLI: `runtime/audiophile_tagger.py --auto-covers -v -j 2 ALBUM_COPY`,
adding `--dry-run` for dry runs; the PTY run omits --auto-covers. Observer drivers
invoke the same production main and delegate actual behavior unchanged, recording
only safe summaries. Exact per-mode logs, outcomes and source digests are retained.
The final diagnostic dry-run and acquisition repeat both exited 0 without changes.

Remaining resources needed: a real complete album and multi-disc album location;
renewed/authorized provider credentials or accessible responses for failed/private
routes; the preferred GUI/player configuration for embedded/plain lyrics;
reference DR-meter vectors; and separate platform/power-loss environments from
the implementation ledger. No claim of exhaustive provider/player compatibility,
transcription correctness or filesystem durability follows from this sample.
