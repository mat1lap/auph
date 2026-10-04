# Cover implementation and verification record

## 2026-10-04 audit update

Album orchestration disables persistent `.cover_digital.jpg`/`.cover_scan.jpg`
preview writes during acquisition. Interactive category choices use temporary
prepared images; final display/source art still participates in album commit.
The standalone cover fetcher retains its explicit output/preview behavior.
Unicode comparison now retains non-Latin letters and NFC-normalizes names;
missing artist identity does not become an automatic artist match.

Provider labels are provenance evidence, not proof of pixel origin. CAA and
Discogs uploads with unknown provenance remain unknown unless packaging evidence
supports scan classification. Digital-service provenance identifies the service;
it does not certify that artwork was never scanned. The crop heuristics and
historic reproductions below do not establish accuracy for arbitrary packaging,
fingers or intentional borders. Automatic/manual/category and EXIF conversion
paths have offline coverage; no new live artwork-provider run was performed.

## Selection contract

`CoverFetcher.find_best_cover` discovers candidates, rejects known non-front or
conflicting releases, probes the available full representation, measures quality,
groups duplicates, and selects within the highest release tier that has a valid
front. Unknown Discogs secondary images require corroborating front evidence.
Perceptual similarity alone never permits crossing release tiers.

Visual quality is measured on a 0–100 scale. Contributions are detail 32%,
compression 20%, geometry 14%, tone 10%, color integrity 5%, cleanliness 9%,
and upscale evidence 10%, minus metadata-supported watermark and sub-500-pixel
resolution penalties. A cover below 500 pixels on its shorter effective side is
excluded from automatic and category selection when a usable cover for the same
release is present; it remains available as a last resort and in manual mode.
Differences within two points are treated as inconclusive; front-role confidence
then resolves the choice. Provenance and stable identifiers resolve further ties.
Provider identity contributes no visual-quality points. DPI, file size, PNG,
colorfulness, and entropy receive no direct quality bonus.

## Finding reconciliation

| Finding | Implementation |
|---|---|
| CAA information loss | Original and thumbnail URLs, types, flags, comments, approval, IDs and release evidence retained in `query_cover_art_archive`. |
| CAA unknown dimensions | Actual original decoded before ranking; declared and measured dimensions remain separate. |
| Raw/package front rejection | Originals retained; front ROI proposed only with a strong packaging seam; ambiguous scans stay uncropped. |
| Discogs secondary assumed front | Secondary role unknown; primary only likely front; hashes may corroborate a secondary. |
| Discogs representation ambiguity | CDN transforms explicitly labelled generated previews; URI150 stored separately; available URI probed. No unsupported signed-URL rewriting. |
| Metadata richness bias | API dimensions do not substitute for measurements. |
| Provider/scoring bias | Visual scoring independent of provider; release correctness enforced separately. |
| Amazon 160-pixel variant | Amazon CDN URLs are treated as unverified representations, never labelled high resolution. Their measured dimensions incur a resolution penalty; a 160-pixel variant cannot become the best digital option beside a usable same-release CAA scan. `cover_mode` only breaks near-quality ties. |
| Weak upscale evidence | Native-frequency softness below the fake-upscale threshold no longer reduces effective resolution or authenticity. The soft penalty starts at confidence 0.60; strong synthetic upscales remain penalized. This prevents a 3000-pixel source from being punished merely because a 1200-pixel derivative falls below the detector's size gate. |
| Wrong-edition fallback | Release tiers, title and supplied barcode/catalog/country/media conflicts gate selection; a unanimous local MBID is used for cover discovery only after MusicBrainz title, barcode and Discogs relation verification. |
| Unbounded resolution reward | Bounded detail score with soft upscale discount. |
| Duplicate evidence | SHA-256, perceptual and difference hashes, including square derivatives; grouping and role promotion require compatible release identities. Copies do not vote. |
| Nondeterministic comparison | Stable selection keys and deterministic candidate ordering. |
| EXIF/MIME/parsing | Decode validation, oriented dimensions, actual MIME, JPEG/PNG/WebP support and byte/pixel limits. |
| ICC/CMYK/alpha | Profile-aware analysis and compatibility conversion; originals retained. |
| Repeated quality analysis | Versioned SHA-256 analysis cache independent of provider metadata. |
| Transient failure caching | Failure categories retained; unsuccessful HTTP results not persisted; success clears stale errors. |
| Expensive discovery | Metadata filtering, tier-by-tier original probing, bounded candidate pools and analysis resolution, HTTP stream byte cap and thread-local sessions. Manual mode removes candidate caps. |
| Source generation loss | Byte-exact `cover.original.<format>` source staged with album commit; embedding reads source directly. |
| Existing artwork safety | Existing local cover and each distinct embedded front are measured. A clearly better exact-release front can replace them; old cover bytes are backed up, and nonfront picture roles remain. |
| Picture headers | MIME from decoded format, oriented dimensions, depth/palette fields; oversized sources receive a bounded embedding derivative while archival bytes remain intact. |
| Hidden menu default | Separate automatic choice displayed when winner differs from digital/scan choices; a single remaining category cannot replace that automatic winner. |
| Broken comparison report path | JSON uses generated comparison path. |
| Explainability | Candidate fields, score breakdowns, rejection reasons, verbose output, performance counters and transaction cover provenance. |
| Representation fallback | Actual evaluated URL/kind and original verification state recorded; fallback thumbnails never inherit original measurements. |
| Manual choice | `--manual-covers` renders and lists only decoded, plausible front candidates with no release, role, image or quality rejection; `-vv` is unnecessary. |

## Queen reproduction

Original files were fetched on 2026-09-23 and evaluated without writing the
music library. The CAA parser was used with the fetched release API response.
The earlier library path could leave the MusicBrainz release unresolved while
retaining a Discogs release ID. That omitted the exact CAA release query, so a
Discogs preview could become the best scan by default. The cover-only MBID
recovery now requires unanimous local tags, a matching fetched MusicBrainz
record, its Discogs release relationship, title and barcode agreement before
CAA is queried. The saved `cover.jpg` and embedded picture were also able to
diverge from the ranked winner; replacement now uses measured comparisons.

| Candidate | Dimensions | Front ROI | Detail | Compression | Quality |
|---|---:|---:|---:|---:|---:|
| CAA 28543392438, designated front | 3387×2976 | 3387×2976 | 76.14 | 100.00 | 91.28 |
| CAA 28543394386, type Front | 3890×3272 | 3272×3272 | 79.25 | 100.00 | 92.12 |
| Discogs 2467591-1381076909-7651, primary | 600×523 | 600×523 | 33.18 | 98.18 | 76.31 |
| Discogs 2467591-1381077136-4412, secondary | 600×600 | 600×600 | 37.87 | 98.18 | 77.22 |

All four associate with the exact release. The designated CAA front wins:
the other CAA scan is within the two-point quality tolerance and has weaker
front confidence. Discogs previews have much less measured detail; the
secondary also lacks semantic front confirmation. No album-specific branch exists.

## Apple/Deezer same-art reproduction

The cached Queen *Sheer Heart Attack* images have matching perceptual and
difference hashes and differ by about 2.16 RGB levels per channel after the
3000×3000 Apple image is reduced to Deezer's 1200×1200 size. Before the
threshold correction, Apple scored 81.81 and Deezer 83.00: Apple's moderate
0.478 upscale evidence reduced its authenticity component and effective
resolution, while Deezer was below the detector's 1400-pixel gate. Neither
image provided strong proof of upscaling. With only strong evidence penalized,
Apple scores 88.64 and Deezer 83.00. A synthetic 300-to-2000-pixel fake
upscale still scores substantially below its genuine source.

## Limits of the implemented evidence

Scores are engineering heuristics, not calibrated archival measurements.
Upscaling, skew, moire, borders, and streak signals are uncertain. No trained
watermark/OCR, dust/scratch classifier, perspective recovery, or trustworthy
native-resolution estimator is supplied. A zero watermark value means no
detected metadata evidence, not proof that an image has no watermark.
The ROI is a proposal, not a verified crop, and is not applied to archival bytes.
Content such as deliberately clipped or monochrome artwork can confound blind
quality metrics. Hash grouping is not proof that two editions are identical.

Release year and final physical-pressing identity remain the responsibility of
upstream release identification. The cover layer consumes verified release IDs
and rejects explicit conflicts with supplied barcode, catalog, country and medium.
Candidate cutoffs are bounded heuristics, not a guarantee of exhaustive search.
The Queen comparison covers the identified regression candidates, not a full
live crawl of every provider or a committed album enrichment run.

The cover-specific original prose audit is not stored in this checkout. This
reconciliation checks retained findings and the user's implementation checklist;
it cannot be represented as an independent reread of an unavailable artifact.

## Verification

Run the entire suite and configured checks:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy .
```

The synthetic Queen regression and provider inverse are offline tests; real
images are not required for the test suite. Additional tests exercise original
URLs, declared dimensions, release conflicts, role gates, lossy derivatives,
hash caching, EXIF, image modes, malformed payloads, source preservation,
transient errors, deterministic ties, and unchanged embedded PNG bytes.

## Acquisition/selection follow-up

See [SELECTION_AUDIT.md](SELECTION_AUDIT.md). Provider-supplied iTunes, Deezer,
Bandcamp and Qobuz URLs are now used without invented high-resolution rewrites.
Representation fallback validates decoded content, and conversion failures retry
eligible candidates. The original archival source remains separate from the
prepared preview/playback derivative.
