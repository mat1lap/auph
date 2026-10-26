# Mastering-first pipeline — 2026-10-13

> Subsequent offline source review withdrew/downgraded three of the four comparative
> preferences. See [the quality audit](MASTERING_QUALITY_AUDIT_2026-10-13.md). Earlier
> figures below describe contract results, not confirmed best-mastering quality.

Implemented the six improvements from the review. Existing user changes were
preserved. No staging, commits, library FLAC writes, downloads, client submissions
or billing changes were performed.

## Resulting behaviour

The default pipeline independently extracts forum evidence, validates each edition
and opinion, chooses a reported mastering family, then ranks matching rips by
practical evidence. A torrent outside that family cannot replace the preferred
mastering because it has more seeders. Other practical options are separately
labelled; an absent or ambiguous match has no automatic recommended torrent.

New modules: `search_mastering.py`, `search_opinions.py`,
`search_openai_research.py`, `mastering_benchmark.py`. Updated research, evaluator,
workflow, model/config contracts, CLI documentation and examples. Regression files:
`test_search_mastering.py`, `test_mastering_benchmark.py`; legacy protocol tests
explicitly retain coverage with `mastering_pipeline=false`.

Research retains cleaned original post text, album-scoped context, quoted-context
attribution, reply links, dates and IDs. Up to 80 posts per page can be retained.
Next pages, solver calls and redirects share the page budget. With >=4 queries,
broad discovery precedes newly found identifiers and a reserved dissent query.
Independent successful pages are reused in the same run. Source capacity is spread
across pages, with comparison and positive/negative opinion context prioritised.
The model input byte budget removes entire sources, not fragments of sentences.

Edition, reported mastering family and rip remain distinct. An affirmative same-
mastering statement explicitly identifying both editions is required to merge
families. Catalogue/engineer equality alone is insufficient. Edition feature values
have literal/provenance requirements. Unsupported feature claims are withheld as
unknown individually; a valid edition descriptor survives. Invalid opinions and
links are excluded, duplicate IDs are all rejected, and partial evidence is visible.
Validators reject negative, hypothetical, hearsay and losing-side preferences and
require complete retention of the validated dissent in the final decision.
These checks are conservative necessary conditions, not a complete NLP proof.

Responses integration supports an explicit Astra model, `store=false`, structured
outputs and optional domain-restricted web discovery with a tool-call cap. Discovery
summaries are not treated as reviews; original forum pages must be fetched. Research
disabling/disclosure controls apply to the extra provider too. An API key is still
required; no ChatGPT credentials were extracted or repurposed.

Local `search.local.toml` now enables the staged/adaptive pipeline with 8 queries,
10 page requests, 80 posts/page, 40 model sources, 15 detail requests, a 600-second
detail-start budget, 60-second provider timeout, medium Gemini thinking and 16384
staged output tokens. The configured Gemini model remains `gemini-3.5-flash-lite`.
Its existing JSON mode was preserved. No credential values were changed.

## Live evidence and its limits

Frozen corpus: **24 real albums, 819 retained source records**. All 24 album research
runs returned FOUND, which means some original post context was acquired, not that
every retained record is a verified review. Each corpus case used two search queries
and a maximum of two forum HTTP requests, without RuTracker access. A synthetic
unidentified rip explicitly prevents pretending the corpus verified real downloads.

Final comparison used identical snapshot fingerprints for every model:

| Model | Generation calls | Accepted preferences | Accepted abstentions | Rejected/failed assessments |
| --- | ---: | ---: | ---: | ---: |
| gemini-3.5-flash-lite | 29 | 4 | 19 | 1 |
| gemini-3.5-flash | 13 | 0 | 0 | 13 |
| gpt-6-astra | 0 | — | — | Not configured: no OPENAI_API_KEY |

Flash failed with 12 HTTP 503/UNAVAILABLE responses followed by a 429/quota response;
that model's comparison stopped. Pro preview had earlier returned 429 in the pilot.
No quota/billing workarounds were attempted. The benchmark now stops a model after
three consecutive server errors as well as access/auth/model/quota errors.

Flash-Lite's one rejected final assessment failed the mastering decision contract.
All **23 accepted final benchmark assessments** were replayed against the current
validators without new model calls and passed. The earlier comparison predates the
individual feature-withholding refinement and is retained as an intermediate
artifact, not reported as the final implementation's result.

These are evidence/contract outcomes, **not expert sound-quality accuracy**. All
real corpus `expected` labels remain null: no objective best-mastering labels were
invented from the same model being evaluated. There were no retained explicit direct
comparisons in the accepted final preferences. Four preferences versus nineteen
abstentions demonstrates that evidence coverage, extraction capability and API
availability still limit the result. Equality with an expert or Astra is unproven.
The benchmark supports independently reviewed accepted alternatives and mapping
labels for a future quality score on the same frozen evidence.

A real ordinary CLI search for **Queen — Jazz** completed with exit 0:

- 25 discovered topics; five eligible candidates, all five descriptions acquired.
- 61 retained source records; 12 usable sources included in the staged assessment.
- Conditional preference: `2001/2004 Japan mini vinyl remasters`, supported by one
  named account's own positive statement about News Of The World and Jazz. No direct
  comparison or consensus was established; no unsupported sonic characteristics
  were attached to that statement.
- Mapping remained `ambiguous_or_missing_identifiers` / `unestablished`; no torrent
  was automatically selected or submitted. Other practical releases stayed visible.
- Current-validator readback of the acquired opinion graph and decision passed.
- Jackett 20.46s; detail acquisition 62.95s; final model stages 9.64s. These stage
  times do not include the separate forum research duration.

The broader ordinary search and the smaller frozen Jazz corpus gave different
conditional preferences. Their source sets differed. This is an explicit remaining
coverage limitation, not evidence that either is objectively the best mastering.

All live generation attempts in this turn: **87**, including failed/intermediate
pilots, intermediate/final comparisons and two ordinary-search stages; no hidden
retries. One models.list request was also made. API keys, headers and original
provider response bodies were not retained. Request counters were persisted before
benchmark calls. Corpus snapshots, public source links, redacted results, private
counters, CSV and the ordinary-search log are preserved in
[.mastering-benchmarks/2026-10-13](.mastering-benchmarks/2026-10-13/), ignored by Git.
Files retain mode 0600. Initial copies remain under `/tmp/auph-mastering-2026-10-13/`.

## Verification

Current targeted verification and the final full-suite result are recorded below.
Lint, format, type checks, dependency compatibility, diff whitespace and production
CLI help were exercised. No ignores/exclusions or weakened type settings were added.
Regressions use production validators, serializers and workflow with only network
boundaries mocked; they cover mastering/practical separation, absent editions,
ambiguous families, explicit equivalence evidence, dissent, negation, pronouns,
quoted votes, feature provenance/withholding, pagination, source byte budgets,
provider rejection, Responses discovery, frozen evidence, atomic private artifacts
and persisted call budgets.

Final checks on the completed implementation:

- `.venv/bin/pytest -q -p no:cacheprovider`: **1077 passed, 146.75s**.
- `.venv/bin/pytest -q -p no:cacheprovider test_search_mastering.py test_mastering_benchmark.py`: **41 passed**.
- `.venv/bin/ruff check .`: passed.
- `.venv/bin/ruff format --check .`: passed, 80 files.
- `.venv/bin/mypy .`: passed, 63 source files.
- `.venv/bin/pip check`: no broken requirements.
- `git diff --check`: passed.
- Production `audiophile_tagger.py search --help`, benchmark CLI help and the
  updated local policy validation: passed.

The earlier 1068/1074-pass runs preceded later regression cases; the 1077-pass,
146.75-second run is the final full-suite result. The 23 benchmark readbacks and
ordinary-search readback passed against the current validators, without generation.
