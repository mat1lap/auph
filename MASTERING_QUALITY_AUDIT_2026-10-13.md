# Offline mastering quality audit — 2026-10-13

Reviewed the four previous preferences and all nineteen abstentions using saved
original post text, source metadata and retained model graphs. No provider calls,
HTML reconstruction, downloads, library writes or original-corpus changes occurred.
The reviewed corpus fingerprints are bound to the manual audit observations; another
corpus with the same case IDs cannot inherit these judgements.

## Four recommendations

| Album | Previous preference | Audit result |
| --- | --- | --- |
| Queen — Jazz | 2001/2004 Japan mini vinyl remasters | Withdraw comparative preference. The source reports liking these editions, without establishing superiority over alternatives. Preserve it as an endorsement. |
| Queen — News of the World | 2001/2004 Japan mini vinyl remasters | Same source and same limitation as Jazz. This is not another independent comparative report. |
| Pink Floyd — The Wall | Harvest | Withdraw the current-winner claim. The retained paragraph identifies a former favorite; the same original post continues with praise for a newer European remaster in the Oh by the Way box. The old scoped context omitted that continuation. No replacement winner is invented offline. |
| Steely Dan — Aja | Cisco Music | A tentative personal preference is supported. The author explicitly qualifies it by taste and system. The same post contains criticism of another edition, omitted by the model extraction. This is not a verified global best or consensus. |

Source posts are retained in the private corpus and linked by exact source IDs:
Jazz/News of the World `forum:90f9a6c009dc92b47a24`, The Wall
`forum:9d83d38b2766c84e94c8`, Aja `forum:ae421bb6c099ad8f167f`.
The two Queen results originate from the same account/post. System-specific and
conditional qualifiers on Aja now travel with the opinion graph and require an
explicit evidence limitation.

## Nineteen abstentions

| Album | Observed cause / remaining limit |
| --- | --- |
| Innuendo | Preference refers back to a previously described set. Exact edition binding and pronoun context need further evidence/extraction. |
| A Night at the Opera | Saved text contains an own DCC preference and a first-person comparison. The old graph retained no opinions; comma/negation binding and comparison vocabulary were too formal. The missing model proposal cannot be reconstructed. |
| The Dark Side of the Moon | An important fetched post summarizes polls/other listeners and lacks explicit artist context. An own comparative choice is not established by that summary. |
| Wish You Were Here | Read pages prioritize CD timing and Wizard of Oz synchronization, not mastering preference. These are useful identity context, with lower recommendation-research priority. |
| Animals | Own comparisons are present, but all edition records were rejected in the old extraction. One admitted reply actually concerns The Wall. Missing original extraction records prevent assigning every rejection to a specific sentence. |
| Abbey Road | Beatles/The Beatles spelling rejected relevant sources. An interview republished as plain prose must not count as the forum author's own listening vote. Some other comments remain tentative/positive only. |
| Revolver | Artist-context omissions, narrow comparison/stance checks, and discussion of other mixes/media. Some useful own statements exist. |
| Sgt. Pepper | Short album naming plus a source that explicitly says its list came from an AI search. The latter is contextual material, not listening evidence. |
| The Beatles / White Album | Artist-name matching admitted With the Beatles and other works. None of those unrelated posts establishes a White Album recommendation. |
| Led Zeppelin / debut | Artist-name matching admitted Led Zeppelin III and catalogue-wide comments. Distinct debut identity is required. |
| Led Zeppelin II | An own RL preference exists in saved prose, but no opinion was retained by the old extraction. The omitted model proposal is unavailable. |
| Led Zeppelin IV | The important retained statement reports other people's preferences. Abstention remains appropriate for that hearsay evidence. |
| Brothers in Arms | Own comparisons exist, while the old comparison record failed validation. Stereo, multichannel mixes and media must be kept distinct; a multichannel preference cannot silently establish a stereo winner. |
| Love over Gold | An album-specific MFSL preference exists in prose; the old extraction did not preserve it. Edition references and narrative wording remain important. |
| Duke | Narrative contrasts fatigue from DE with enjoying the 1986 Virgin Japan edition. A keyword-only contract loses that structure. No new model interpretation is fabricated. |
| A Trick of the Tail | An own US ATCO preference uses anaphora after describing the owned disc. Poll results alone do not establish consensus. |
| Gaucho | The preferred-versions introduction lost the UICY-40018 and 1984 identifier rows. This parser defect is fixed. Another statement names Mastering 3 as a personal go-to, requiring exact edition binding. |
| Rumours | A listener who owns both versions prefers the 45. No opinion was retained by the original model graph. The original proposal is unavailable. |
| Hotel California | Some sources omit artist context; many extraction records were rejected. Vinyl, stereo and multichannel comparisons have different scope. |

The separate Houses of the Holy **failed** assessment is not one of the nineteen
abstentions. Its rejected decision body was not retained; the exact failing clause
is unavailable. The audit does not mislabel that failure as insufficient evidence.

These categories overlap. A saved affirmative sentence does not prove that the
original model proposed it correctly, nor that it is sufficient to select a global
best mastering. `original_extraction_available=false` is explicit in each audit
case. Aggregate old rejection counters are preserved separately from the new
record-level diagnostics.

## Confirmed changes

- Separate `endorse` from comparative `prefer`. Independently supported sonic
  descriptions survive a downgrade; an endorsement cannot select a preferred family.
- Preserve late paragraphs following an explicit requested-album scope, and retain
  identifiers following an explicit preferred-versions list. Stop at another scope
  or heading. Re-scope saved original text without changing old source IDs/excerpts.
- Treat explicit historical preferences as context unless a current reaffirmation
  is supported. Preserve system-specific, tentative and conditional source caveats.
- Handle comma-relative clauses, local negation and ordinary first-person comparison
  wording. Recognize negative own expressions such as didn't care for, without
  turning them into positive votes.
- Withhold unsupported comparison/characteristic fields individually rather than
  losing an otherwise valid opinion. Invented IDs and literal source/span errors
  still fail validation. Bounded rejection details contain controlled rules and
  existing source IDs, not arbitrary model strings or raw provider bodies.
- Recognize the vetted Beatles spelling and distinctive work aliases. White Album
  and Zeppelin debut discovery use distinctive terms; other works cannot match just
  the artist name. Explicit other-work replies override a thread-title match.
- Keep declared AI summaries and republished interviews as context rather than an
  author's own listening evidence. Lower the priority of timing-only topics.
- Treat mini vinyl/mini-LP as packaging rather than proof of a vinyl medium. The
  two retained Queen edition records had inherited this false medium from the
  original extraction; it is now withheld without guessing the actual medium.
- Version parsed page caches to v7: old normalized subsets may have omitted newly
  relevant posts and cannot supply them without fetching HTML again. The default
  staged pipeline does not reuse those old subsets. Explicit legacy mode retains
  old-cache compatibility.

The offline readback admits 28 previously missed source records (23 for Abbey Road,
four for Revolver and one for Sgt. Pepper), while withholding 69 previously admitted records: 27 for the wrong Beatles album, 40 for
other Zeppelin works, one other-work reply in Animals and one declared AI summary.
The republished Abbey Road interview was already blocked under the old exact-name
check; after the spelling correction it remains contextual. These are source-scope
counts, not new opinions or recommendations. No new model benchmark was run.

## Artifacts and reproducibility

`audit_mastering_quality.py` reads the original frozen corpus and old results, verifies
fingerprints and produces an immutable private audit directory:

```bash
.venv/bin/python audit_mastering_quality.py \
  --artifacts .mastering-benchmarks/2026-10-13 \
  --output /tmp/auph-mastering-quality-audit
```

Completed artifacts are in
[.mastering-benchmarks/2026-10-13/quality-audit-release](.mastering-benchmarks/2026-10-13/quality-audit-release/).
Per-case JSON distinguishes source review, old rejection counts, new validation,
endorsements, historical context, source admission changes and unknown original
model output. `offline_withdrawal` is an audit-derived withdrawal, not a regenerated
model recommendation. Original snapshots/manifests and the old CSV remain unchanged.

The original implementation report's four preferences were **contract outcomes**
of that earlier version. This audit supersedes their interpretation as comparative
recommendations. One tentative personal choice remains supported; the other three
have been downgraded/withdrawn. It does not claim expert sound-quality accuracy.

Regression checks use synthetic equivalents of saved statements with production
validators and source parser. Tests also prove the audit performs no network calls,
keeps original corpus bytes unchanged and does not apply reviewed annotations to
other fingerprints. Final validation is recorded below.

## Validation

- `.venv/bin/pytest -q -p no:cacheprovider`: **1111 passed in 147.74s (0:02:27)**
  on the final implementation including the packaging/medium regressions.
- `.venv/bin/pytest -q -p no:cacheprovider test_audit_mastering_quality.py
  test_mastering_quality_audit.py test_search_mastering.py test_search_research.py
  test_mastering_benchmark.py`: **172 passed in 7.27s**.
- `.venv/bin/ruff check .`: passed.
- `.venv/bin/ruff format --check .`: 85 files already formatted.
- `.venv/bin/mypy .`: no issues in 67 source files.
- `.venv/bin/pip check`: no broken requirements.
- `git diff --check`: passed.
- Independently rechecked all 24 corpus fingerprints against the original manifest;
  all 19 reviewed abstentions carry fingerprint-bound observations. All JSON files
  in the completed audit directory have mode 0600.
