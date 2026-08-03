# Ground truth — the corpus every gate is measured against

This directory holds hand-keyed truth read directly off the issuers' PDFs. It is
the denominator of every recall and precision number this project quotes.
Nothing in it is generated from pipeline output; a value that came out of the
pipeline would score the pipeline against itself.

**Status: not yet keyed.** Plan Task 3 (`docs/superpowers/plans/2026-08-03-financial-object-graph-phase-0-2.md`)
is human data entry and is still outstanding. `backend/tests/unit/test_groundtruth_files.py`
is already in place so the corpus cannot arrive malformed, and it skips —
rather than passing — while the directory holds no files.

## File naming

```
{issuer}-{fiscal_year}-{basis}-{statement}.json
```

e.g. `TCS-FY2025-26-consolidated-profit_and_loss.json`.

The stem is asserted against the file's own contents by
`test_filename_matches_its_contents`: a file keyed for the wrong basis scores
the wrong statement set, and that error is invisible in the resulting numbers.

- `basis` ∈ `consolidated` | `standalone`
- `statement` ∈ `profit_and_loss` | `assets` | `liabilities` | `equity`

**RIL has no standalone basis.** It files a consolidated-only integrated report,
so `RIL-*-standalone-*.json` must not exist — generating one would create truth
that does not exist and then measure against it.

## File shape

```json
{
  "issuer": "TCS",
  "fiscal_year": "FY2025-26",
  "basis": "consolidated",
  "statement": "profit_and_loss",
  "values": { "RFOGRS": 255324.0 },
  "not_reported": ["EXCHEXP"]
}
```

`fdi.core.scoring.parse_groundtruth` reads this shape. Generate the skeleton with
`scripts/gt_template.py` so keying is *checking numbers*, not typing 402 tags.

## The two rules that make the numbers mean anything

**1. `values` and `not_reported` must stay disjoint.** A tag in both makes the
absent-vs-missed split undefined, and silently preferring one list would make
the resulting number a lie. `parse_groundtruth` rejects the whole file when they
overlap — that rejection is deliberate and must not be worked around.

`not_reported` is half the record, not an afterthought. The taxonomy is a fixed
list of ~402 tags and ~180 of them cannot apply to a services company at all
(25 extraordinary-item tags cannot apply to any Ind-AS filer). Scoring recall
against the whole list measures the taxonomy's breadth, not the extractor's
reach. Stating what the issuer genuinely does **not** report is the only way to
tell ABSENT from MISSED.

**2. Key the number AS PRINTED. Never rescale.** No unit conversion, no
thousands/lakh/crore normalisation, brackets as negatives. Converting during
keying is how a ×10 unit bug gets baked into the truth set itself, where nothing
downstream can detect it.

Printed units, recorded here rather than converted:

| Issuer | Printed unit |
|---|---|
| TCS | ₹ crore |
| RITES | ₹ lakh |
| RIL | *record it here when keying, read off the statement header* |

Scoring tolerance is absolute and tight (0.01), precisely because truth is keyed
as printed: anything beyond float noise is a real disagreement, not rounding.

## Verifying

```
uv run --directory backend pytest tests/unit/test_groundtruth_files.py -q
```
