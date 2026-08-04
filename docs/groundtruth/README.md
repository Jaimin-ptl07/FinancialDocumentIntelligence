# Ground truth — the corpus every gate is measured against

This directory holds hand-keyed truth read directly off the issuers' PDFs. It is
the denominator of every recall and precision number this project quotes.
Nothing in it is generated from pipeline output; a value that came out of the
pipeline would score the pipeline against itself.

**Status: partially keyed — every issuer's profit and loss, plus TCS's
consolidated balance sheet. Face level only.**

| File | Tags |
|---|---|
| `TCS-FY2025-26-consolidated-profit_and_loss.json` | 11 |
| `TCS-FY2025-26-standalone-profit_and_loss.json` | 11 |
| `TCS-FY2025-26-consolidated-{assets,liabilities,equity}.json` | 7 / 6 / 1 |
| `RIL-FY2025-26-consolidated-profit_and_loss.json` | 12 |
| `REMC-FY2024-25-unknown-profit_and_loss.json` | 9 |

Still outstanding: TCS standalone balance sheet, all cash flows, RIL's and
REMC's balance sheets, and the **note-level detail for everything**. Until those
land there is **no full baseline** and no accuracy claim should be made.

## Sign conventions are issuer-dependent, and stay that way

REMC prints Current Tax **in brackets** (a deduction from PBT); TCS prints the
same concept **positive** and subtracts the total separately. Keyed as printed,
`CT` therefore carries opposite signs across two issuers.

That is correct for scoring an *extractor* — it must read what is on the page.
Normalising it is the reasoning layer's job, not the truth set's. Do not
"fix" one issuer to match the other.

## A disclosed nil is not the same as an absent line

REMC discloses Other Comprehensive Income as an explicit `-`. Whether that
should score as `0.0` or as absent is an open design question, so `OTCI` is in
neither list for REMC. Prejudging it here would bake the answer into the truth
set, where nothing downstream could detect it.

## How these were read

`pdftotext -table`, which reconstructs rows from the PDF's own text layer. This
is deliberately a *different* extractor from the pipeline's Azure Document
Intelligence, so the truth set is a genuine second opinion rather than a
restatement of what the pipeline already believes.

`pdftotext -layout` was tried and **rejected**: on a wrapped-label statement it
decouples labels from values vertically, so "Revenue from operations" lines up
against the row below it. It produces confident, wrong numbers. Use `-table`.

Every subtotal was re-derived by hand before the file was written.

## Issuer naming — read before keying the third report

The third report is **REMC Limited**, a joint venture of Indian Railways and
RITES Limited. It is **not** RITES. Earlier work in this repo called it "RITES"
and its `doc_key` is still `rites-2024-25`. Ground truth uses the real entity
name, so scoring must map issuer → `doc_key` explicitly:

| Ground-truth issuer | `doc_key` |
|---|---|
| TCS | `tcs-2025-26` |
| RIL | `ril-2025-26` |
| REMC | `rites-2024-25` *(historical, misnamed)* |

## A schema gap that keying exposed

TCS prints "Revenue from operations" as a single face line, but v4.3 makes
`RFOGRS` a **derived** tag composed of `RFSP`/`RFSS`/`RFTG`. The face-level
revenue total therefore has no extractable home and cannot be scored directly —
and the same is true of `TOTFC` (finance costs), `DEPEXP` (depreciation) and
`EEBEXP` (employee benefits), all of which TCS prints as single face lines.

This is not a keying problem. It is the schema assuming every total is built
from components the issuer may not separately disclose. Worth resolving before
note-level truth is keyed, or those face totals read as permanently missing.

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

**0. A tag in NEITHER list is out of scope, and that is a real third state.**
`score_document` ignores it entirely — this file makes no claim about it. Use it
whenever the issuer clearly discloses a line but its mapping to a tag is
ambiguous. Putting such a tag in `not_reported` would score a **correct**
extraction as a false positive. When in doubt, leave it out of both lists.

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
| REMC | ₹ lakh (verify against the statement header before keying) |
| RIL | ₹ crore |

Scoring tolerance is absolute and tight (0.01), precisely because truth is keyed
as printed: anything beyond float noise is a real disagreement, not rounding.

## Verifying

```
uv run --directory backend pytest tests/unit/test_groundtruth_files.py -q
```
