# Real-world validation — 2026-10-05

Started at e32fbc5 (including 663992b), with a clean working tree. Read
IMPLEMENTATION_BACKLOG.md before testing. Full independent album copies,
scripts, sanitized logs, inventories, provider summaries and viewable comparisons
are retained in `/home/emvis/flac-validation-20261005`; reports are in `reports/`.
No original album was enriched. No global policy or credentials were changed.

## Sample and isolation

The located collection, `/home/emvis/Music/Queen`, contains partial single-disc
albums only. These copies do not verify live complete/multi-disc provider identification.
The subsequent generated production workflows below close implementation and
real-scanner coverage without requiring additional user albums; live edition
compatibility for such albums remains unverified.

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

Remaining live compatibility resources: actual complete/multi-disc edition evidence;
renewed/authorized provider credentials or accessible responses for failed/private
routes; the preferred GUI/player configuration for embedded/plain lyrics;
reference DR-meter vectors; and separate platform/power-loss environments from
the implementation ledger. No claim of exhaustive provider/player compatibility,
transcription correctness or filesystem durability follows from this sample.

## Autonomous gap closure — 2026-10-05/06

Baseline HEAD 1521bb9 includes 663992b/e32fbc5. Working tree was clean. New
workspace: `/home/emvis/flac-gap-validation-20261005` (reports, generated fixtures,
readback drivers and headless image). Original sample hashes were rechecked
against the earlier inventory: all three albums and sidecars unchanged.
No enrichment ran on originals or on symlinks to originals.

Generated stereo 44.1kHz FLACs contain six-second controlled sine signals at
540/640/740Hz, with deliberately different amplitudes. Embedded disc/track
positions, opaque shuffled filenames, release/recording IDs, catalog/barcode and
medium counts are controlled independently. Fixtures replace MusicBrainz,
Discogs, AcoustID and cover acquisition boundaries; actual matching, selection,
metadata writing, decoded PCM checks, staging, journal publication and readback
execute. Lyrics lookup is disabled in these completeness fixtures; the separate
conflict fixture observes actual lyric query identity inputs, and restored
adapter/real-world regressions exercise retained lyric forms.

| Generated case | Verified production outcome |
|---|---|
| Complete single disc, 2 tracks | COMPLETE; supported total 2; track gains differ; one album pair; dry-run unchanged and repeat unchanged. |
| Complete two discs, 2+2 tracks | COMPLETE; repeated track numbers resolve to distinct recording/release-track IDs by disc; TRACKTOTAL=2 per medium, DISCTOTAL=2; one album group across both discs. |
| Two discs missing disc-2 track 1 | INCOMPLETE; correct 1/2 medium mapping; totals remain 2/2, not available counts 2/1; no album gain/peak/range. |
| Nonconsecutive tracks 1 and 3, shuffled filenames | INCOMPLETE; recording evidence and tags follow positions independently of enumeration; release total 3. |
| Local total 3 versus matched release total 2 | INCOMPLETE; curated total 3 is preserved with review; no album gain, not forced COMPLETE. |
| Initially missing artist identity | UNKNOWN propagates to provenance and track-only scan. Supported identity is filled by the verified release; the next run can legitimately become COMPLETE and perform an album scan. |
| Equal edition candidates | No candidate MB release/recording IDs written; AMBIGUOUS survives into provenance instead of NONE. |

The five primary generated scenarios retain multivalue/custom curated tags,
unchanged audio and exact repeated-run file hashes (including the ledger).
Independent `metaflac` and `ffprobe` readback covers 13 FLACs in these five cases.
FFmpeg PCM hashes equal independently regenerated pre-enrichment signal
references. These are **synthetic implementation checks**, not evidence of live
complete/multi-disc edition or provider compatibility.

Actual `/usr/bin/loudgain` 0.6.8, libebur128 1.2.6, executes `-a -k -s e -q`
for complete groups and `-r -k -s e -q` for incomplete/unknown groups. Track gains
for the two common signal levels are +17.67/+5.56 dB, peaks
0.017705/0.070786. Complete single/two-disc groups share +8.31 dB/0.070786.
Changing order preserves the audio-group fingerprint; changing membership or
same-sized audio groups forces recalculation. Tests also cover zero STREAMINFO
MD5s, legacy equal gain pairs lacking group evidence, and unavailable scanner
cleanup even when existing track tags are invalid (those track fields remain
explicitly unverified, not fabricated). A nonzero exit after the real scanner writes and a successful process
with a missing track-gain tag both prevent the entire production album commit;
original FLAC bytes and absent ledger are verified.

FFmpeg's independent `loudnorm=I=-18:TP=-1:LRA=11:print_format=json` analysis
reports target-minus-input values +17.75/+5.55 dB for those same tracks, versus
loudgain's +17.67/+5.56. The third frequency gives +5.45 in both outputs. These
are short synthetic inputs processed by distinct EBU analysis implementations;
FFmpeg loudnorm uses its own upsampling/true-peak processing. Measurements and
settings are retained in `independent-audio-readback.json`. No arbitrary
agreement tolerance, calibrated standard compliance or exact numerical agreement
is claimed. Reference-meter vectors and a controlled longer-signal comparison
would be needed for that stronger conclusion. Neither workflow applies gain to
samples.

Standalone external ledgers are rejected before enrichment writes. Supported
same-parent journals and private staging retain their recovery behavior. A real
second-process ledger appender now waits through recovery, then appends to the
restored ledger without losing its row. Existing interruption/repeated recovery
and snapshot-retention tests rerun. Historical unjournaled external writes need
manual review; there is no new external-root or physical power-loss guarantee.

Read original News identifiers read-only: MBID
0060992e-439c-34b0-9aeb-9df1dcef23f3, catalog 32XD-409, barcode
T4988014316538, positions 2/5 and total 11. A sanitized production regression
reproduces conflicting edition evidence, preserving existing IDs/totals while
withholding their authority from artwork, lyrics recording queries and matched
provenance. Diagnostics request edition barcode/catalog/disc evidence, not a
guessed edition. Original News remains a manual identity-review case.

Bounded current adapter calls using existing access: Spotify AUTHENTICATION_ERROR
(0.19s), Deezer AUTHENTICATION_ERROR (0.73s), Yandex BLOCKED (0.51s). No useful
forms returned, no authentication bypass or user credential request. An initial
Yandex driver wrongly supplied an album argument; that diagnostic is separated
and is not counted as a provider parser failure. Remaining previously blocked
providers retain their earlier observed statuses; they were not all retried.
Offline restored-provider parser/schema/fallback regressions pass.

Headless readback on the existing independent acquisition copies reparses native
Lyricsfile YAML with current code, preserving plain and synchronized forms with
no parser warnings. LRC has 29/37 ordered timestamps, within duration. Exact
native/raw/plain/LRC file hashes are retained without full lyrics in reports.
Headless mpv exits 0 and renders the 1400×1400 embedded News cover to JPEG;
that output was actually viewed and shows the expected front composition.
This is not a new edition-certainty claim or personal GUI/Kitty/embedded-lyrics
compatibility check. Those manual checks remain pending.

Commands executed from `/home/emvis/Music/auph`:

- Baseline `.venv/bin/pytest -q`: 645 passed, 107.96s.
- Final `.venv/bin/pytest -q` (captured with `tee` to `reports/final-pytest.txt`): **659 passed, 136.85s**, after the unavailable-scanner cleanup fix.
- `.venv/bin/pytest -q test_gap_workflows.py --basetemp=/home/emvis/flac-gap-validation-20261005/final-synthetic-tests`: 14 passed, 12.36s.
- `.venv/bin/pytest -q test_gap_workflows.py test_restored_lyrics_providers.py test_real_world_regressions.py test_independent_verification.py`: 50 passed, 18.61s.
- Four before-fix regressions against saved HEAD source: 2+2 expected failures; restored current source afterward. Sanitized outputs in `reports/before-fix-*.txt`.
- Unavailable-scanner/invalid-track regression against its immediately preceding implementation: 2 expected failures, then passes after cleanup fix.
- `.venv/bin/pytest -q test_gap_workflows.py test_regression_repair.py test_entire_audit.py`: 112 passed, 25.97s.
- `.venv/bin/ruff check .`: passed after fixing two local lint findings.
- `.venv/bin/ruff format --check .`: 49 files formatted.
- `.venv/bin/mypy .`: passed, 41 source files.
- `.venv/bin/python -m pip check`: no broken requirements.
- `git diff --check`: passed.
- `.venv/bin/python /home/emvis/flac-gap-validation-20261005/inspect_audio.py`: 5 cases/13 FLACs; independent reference PCM equality; measurement report written.
- `.venv/bin/python /home/emvis/flac-gap-validation-20261005/live_checks.py`: bounded live statuses above (corrected Yandex-only rerun preserves preceding Spotify/Deezer observations).
- `.venv/bin/python /home/emvis/flac-gap-validation-20261005/headless_artifacts.py`: mpv exit 0; image decode and artifact/timestamp readback passed.
- `.venv/bin/python /home/emvis/flac-validation-20261005/validate.py originals`: all three original sample inventories unchanged.

Unresolved implementation work for these selected gaps: none. Explicit rejection
closes the unsafe external-ledger configuration rather than implementing a
multi-root transaction. Remaining compatibility checks: live complete/multi-disc
editions, blocked authenticated services, personal GUI/Kitty/embedded lyric
playback, reference DR/loudness certification, and other platform/power-loss
behavior. No general bug-free or exhaustive provider assertion is made.
