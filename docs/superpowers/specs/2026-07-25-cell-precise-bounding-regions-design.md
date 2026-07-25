# Cell-precise bounding regions with current/prior tagging — design

## Problem

A table-sourced field's highlighted region on the PDF page is the region of
the **whole table** the value came from, not the specific cell. Root cause:
the LLM is only ever shown a whole-table evidence card (`t{i}`) since TR-1 P2
removed the finer `t{i}r{j}` row-chunk id (it was flooding the reranker with
same-scored whole-table renders — see
`docs/superpowers/specs/2026-07-22-table-representation-design.md` §6.2).
`assemble_result`'s `source.evidence_ids` can therefore only ever cite `t{i}`
(whole table) or `s{i}` (whole heading paragraph), and
`fdi.core.provenance.resolve_evidence_regions` resolves exactly what it's
given — nothing wrong there, the input is just too coarse.

Meanwhile `fdi.core.table_grid.canonicalize_table` already builds
`cell_regions`: an `n_rows × n_cols` grid of each cell's own Azure DI
bounding region, stored in the `structure` artifact
(`structure["tables"][i]["grid"]["cell_regions"][r][c]`) and never thrown
away. It's just not consulted for provenance today.

Additionally, no part of the pipeline distinguishes which region belongs to
a dual-period field's `current` value vs its `prior` value — one field gets
one undifferentiated region list, regardless of `value` being a scalar or a
`{current, prior}` dict.

## Approach

No LLM, prompt, or schema-file changes. Everything happens post-hoc, in
`fdi.pipeline.extractor._attach_regions`, where `structure` and each
assembled field record are already both in scope simultaneously
(`extract_document`, `pipeline/extractor.py`, after order restoration and
before persistence).

### Row/column matching

New matching function (co-located with `fdi.core.tables.find_concept_rows`,
reusing its token-subsequence algorithm — normalize row-label-column-0 text
and the field's own `keywords`/`aliases` via `normalize_label`, test
containment as a contiguous token subsequence via `_contains_subsequence`)
but keyed on a `FieldSpec`'s own `keywords` rather than an ontology
`CanonicalConcept`. Runs once per field, against every table in the unit's
`structure["tables"]`, on the field's own extraction:

- On a match at row `r`, table `i`:
  - **current** region = `cell_regions[r][1]` (or the column whose header
    Is recognized as the current period, preferring header content over
    position — same convention `table_render._positional_header`/
    `_is_period` already use for column 1 = current, column 2 = prior).
  - **prior** region = `cell_regions[r][2]` (or the recognized prior-period
    column), `[]` if the field isn't dual-period or no second column exists.
- On no match (narrative field, no table row found, ambiguous/multiple
  matches): fall back to today's behavior — whichever whole-table/
  whole-section region `resolve_evidence_regions` already resolves, under
  `current`, with `prior: []`. Never raises, matches this codebase's
  existing guarded-total posture (decision 10) — a failed cell match
  degrades to a coarser box, never no box and never a crash.

Narrative fields never get a `prior` box (approved scope: dual-period
tagging is table-only, since there's no column structure to split
narrative text by).

### Shape change

`BoundingRegionView`/`ExtractSourceView.bounding_regions`:

```
# before
bounding_regions: list[BoundingRegionView]

# after
bounding_regions: BoundingRegionsView  # {current: [...], prior: [...]}
```

This is a breaking shape change. A previously-persisted MinIO extraction
record (flat list) will not validate against the new
`ExtractDocumentView`. Approved handling: bump `EXTRACTION_RECORD_VERSION`
so the content-addressed cache key changes and a stale record is simply
treated as a cache miss (re-extracted fresh) rather than requiring an
in-place migration of old records.

### Frontend

`PdfCanvas.tsx` draws one highlight box per non-empty region set. When
`prior` is non-empty, both boxes render together (approved: show both at
once, not a toggle), each with a small tag label — "New" near the current
box, "Prior" near the prior box — styled with the existing theme-independent
`--pdf-mark` token (the prior box may use a lighter/dashed variant of the
same ink color, staying inside the monochrome system rather than
introducing a second hue).

### Testing

- Backend: unit tests for the new keyword-based row/column matcher (exact
  match, token-subsequence match, no-match, ambiguous-match-picks-first or
  documented tie-break). Integration test: a known dual-period numeric field
  (e.g. "Property plant equipment net") resolves to two distinct regions,
  each smaller than the table's own whole-table region, with the right
  page/column.
- Frontend: render test confirming two tagged boxes appear for a
  `{current: [...], prior: [...]}` field, and exactly one (no "Prior" tag)
  for a `{current: [...], prior: []}` field.

## Out of scope

- Line/word-level geometry for narrative fields (would need Azure DI
  `ocr` artifact line/word boxes joined into provenance — not attempted
  here; narrative fields keep today's whole-heading-paragraph box).
- Any change to retrieval, reranking, or the LLM's evidence-citation
  contract — this is purely a post-hoc geometry lookup.
