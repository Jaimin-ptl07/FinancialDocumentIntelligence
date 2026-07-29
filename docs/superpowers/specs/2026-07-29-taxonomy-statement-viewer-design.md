# Taxonomy Statement Viewer — Design

**Date:** 2026-07-29
**Related specs:** `2026-07-28-audit-grade-financial-intelligence-vision.md` (Pillars 1, 2, 6),
`2026-07-28-m1-fact-model-and-taxonomy-converter-design.md` (the v4.1 schema this
page renders), `2026-07-28-m4-audit-surface-and-xbrl-design.md` (`AuditFactSheet`,
reused here)
**Status:** spec (implementation follows)

## Goal

Add a new root-level page where a bank user picks an already-extracted filing and
sees its figures laid out exactly the way the source taxonomy (`Q3VENI LATEST
TAXANOMY.xlsx`, 4 sheets: P&L, Assets, Liabilities, Equity) is structured — a real,
Screener-style statement table per sheet, with the true parent/child hierarchy
(up to 4 levels deep), current/prior period values, confidence, and one click
through to the source PDF region for any extracted number.

The taxonomy → hierarchical schema conversion already exists
(`docs/architecture/field-extraction-schema.v4_1.json`, from commit `edd0176`) but
has no UI and is not even the schema the live pipeline extracts against yet. This
spec closes both gaps.

## Background

- The xlsx has 4 sheets, each with a `Sr. No.` column encoding hierarchy depth via
  dot-notation (`1` → `1.1` → `1.1.1` → `1.1.1.1`, up to 4 levels), a `BlockID`
  grouping column, and short tag codes per leaf line item — already fully modeled
  in `field-extraction-schema.v4_1.json` (402 fields, 4 modules, nested `fields[]`,
  each node carrying `tag`, `origin` (`extracted`/`derived`), `role`, and — for
  derived subtotals — a `derivation: {method, inputs}` formula).
- **Gap 1 (backend):** the live pipeline (`FDI_EXTRACTION_SCHEMA_PATH`) still
  points at the old flat v3.2.0 schema, not v4.1. Extraction and factsheet output
  today don't reflect the true 4-level hierarchy.
- **Gap 2 (frontend):** no taxonomy UI exists at all. The closest precedent,
  `ModuleTree.tsx` (used on the existing `/schema` page), is a 3-level,
  always-expanded indented list — not deep enough, and not table-shaped.
  `AuditFactSheet.tsx` (ratios/validations/coverage summary) exists and is fully
  built but is not imported into any page anywhere in the app.

## Non-goals

- Editing the taxonomy or schema from this page (that stays on `/schema`).
- Triggering extraction from this page — it is read-only over documents that have
  already been extracted (extraction is triggered from the existing document
  viewer).
- XBRL export, review-queue/correction workflows, immutability/versioning
  (vision doc Pillar 6 items explicitly deferred to a later milestone).

## Architecture

### Backend changes

**1. Wire v4.1 as the live schema.** Point `FDI_EXTRACTION_SCHEMA_PATH` (`.env`,
`.env.example`) at `docs/architecture/field-extraction-schema.v4_1.json` instead of
the current flat file. `parse_field_specs`/`extract_document` then run against the
true 4-level hierarchy. Extraction records are cached by schema hash
(`extraction_key`, `pipeline/keys.py:28`), so this change naturally invalidates
existing cached records — already-extracted filings need a re-extract via the
existing "Read figures" / "Re-extract" controls on the document viewer. No data
migration required.

**2. Add a nested tree to the factsheet response.** `core/factsheet.py` already
has `rows_from_schema`, which walks `modules → retrieval_units → fields` and
flattens to one row per tagged node (used today by `derive_facts`/`coverage`/
`validate`). Add a sibling, `tree_from_schema`, that performs the same walk but
**preserves nesting** and attaches each node's resolved fact
(`value: {current, prior} | null`, `confidence`, `band`, `origin`) from the
`facts` map `document_factsheet` already computes. Only *leaf* nodes in the
existing v4.1 schema carry `field_path`; group/section-header nodes don't, so
`tree_from_schema` additionally synthesizes a `path` string on **every** node
(leaf or group) — e.g. `"profit_and_loss.0.1"`, a dotted index trail from the
statement root — giving the frontend a stable, always-present key for collapse
state without depending on `field_path`'s leaf-only presence. `document_factsheet`'s
returned bundle gains one new key:

```
"statements": [
  {
    "sheet": "B-1-PROFIT & LOSS",
    "statement": "profit_and_loss",
    "tree": [
      {
        "field_name": "Revenue From Operations",
        "path": "profit_and_loss.0",
        "tag": null, "origin": "section", "role": "sub_header",
        "value": null, "confidence": null, "band": null,
        "children": [
          {
            "field_name": "Revenue from Sale of Products", "tag": "RFSP",
            "path": "profit_and_loss.0.0",
            "origin": "extracted", "role": "line",
            "value": {"current": 12345.0, "prior": 11000.0},
            "confidence": 0.94, "band": "high",
            "children": []
          }
        ]
      }
    ]
  }
]
```

Everything else already in the bundle (`ratios`, `validations`, `coverage`,
`summary`) is unchanged. One `POST /factsheet/document` call now serves the whole
new page — summary strip and all four statement trees.

**3. No new endpoint for PDF evidence.** The existing document viewer
(`frontend/components/viewer/ReportViewer.tsx`) gains the ability to read an
initial `tag` from `useSearchParams()` on mount, resolve it against
`extract.data.fields`, and select it exactly as if the user had clicked it in
`FieldList` (same effect: page turns, bbox highlights). This is the only change
needed to support opening `/documents/[docId]?tag=RFSP` in a new tab from the new
page.

### Frontend changes

**Two new routes**, following the existing `documents/[docId]` file-based-routing
convention:

- `frontend/app/taxonomy/page.tsx` — landing/picker page. Reuses
  `listDocuments()` (same query the dashboard uses), filtered to documents with a
  completed extraction, rendered as a searchable list (styled like the dashboard's
  `.sheet` rows). Selecting a row navigates to `/taxonomy/[docId]`.
- `frontend/app/taxonomy/[docId]/page.tsx` — the statement viewer. Fetches
  `getDocument(docId)` then the new `factsheetDocument(docKey)` wrapper
  (`POST /factsheet/document`), via `useQuery`, matching the existing
  `ReportViewer` data-fetching pattern. Renders:
  1. `PageHeader` (title = filing label).
  2. A summary strip reusing `AuditFactSheet`'s `Stat` row idiom (completeness %,
     extracted/derived/missing counts, checks failed) — giving that
     already-built, already-tested component its first real home in the app.
  3. Four tabs — P&L / Assets / Liabilities / Equity — each rendering one
     `StatementTable` for its statement's `tree`.

**Nav registration:** one entry added to `DESTINATIONS` in
`frontend/components/shell/Spine.tsx`: `{ href: "/taxonomy", label: "Taxonomy" }`.

**Types/API:** `frontend/lib/types.ts` gains `TaxonomyTreeNode` (the shape above)
and an extended factsheet bundle type carrying `statements`.
`frontend/lib/api.ts` gains `factsheetDocument(docKey)`.

**New component — `frontend/components/taxonomy/StatementTable.tsx`,** one per
active tab:

- A real `<table>` inside `.sheet-flat`. Columns: **Particulars** (indented by
  depth, disclosure triangle on rows with children) · **Tag** (`.data`, muted,
  blank on section headers) · **Current** · **Prior** · **Δ%** · **Confidence** ·
  a small provenance glyph. Numeric columns right-aligned, `.data` class
  (IBM Plex Mono, tabular-nums — matching every other numeral in the app).
- **Hierarchy:** up to 4 levels via indentation steps (extends `ModuleTree`'s
  indentation approach into table cells). Default state: **expanded to depth 2**
  (statement → section → top line items visible; deeper nesting starts
  collapsed). Each row's triangle toggles its own subtree; an "Expand all /
  Collapse all" control sits above the table. Collapse state is local component
  state keyed by each node's `path`.
- **Origin/derivation, in the app's existing weight/glyph idiom** (color is
  reserved for provenance per the design system's own rule — see `globals.css`
  header comment): derived rows render in a lighter weight with a small `Σ`
  glyph; hovering/clicking shows the derivation formula (e.g.
  `= INVTRMI + INVTRMD`) as a tooltip, built from the `derivation.inputs` the
  backend already carries.
- **Confidence:** rendered as `AuditFactSheet`'s ✓/! band glyph, not a raw number.
- **Provenance:** each *extracted* leaf with a source gets a link-out glyph.
  Clicking it does `window.open(\`/documents/${docId}?tag=${tag}\`, "_blank")` —
  opens the existing PDF viewer in a **new browser tab**, pre-selected to that
  field per the backend section's `ReportViewer` extension. Derived/section rows
  get no provenance glyph (no single bbox source).
- **Search:** a text input above the table filters rows by particulars/tag,
  reusing `ModuleTree`'s prune-empty-branches filtering approach, scoped to the
  active statement.

## Data flow

`/taxonomy` → `listDocuments()` filtered client-side to completed extractions →
user picks a filing → `/taxonomy/[docId]` → `getDocument(docId)` +
`factsheetDocument(docKey)` (react-query) → response's `statements[]` feeds the
active tab's `StatementTable`; `summary`/`ratios`/`validations`/`coverage` feed the
header strip. Clicking a provenance glyph opens
`/documents/[docId]?tag=TAG` in a new tab, which resolves the tag against its own
already-fetched extraction data and highlights the source region. No extraction is
triggered from `/taxonomy` — it is a read-only view.

## Error handling

- **No completed extractions:** picker shows an empty state linking to `/`
  (dashboard) to find/extract a filing.
- **Factsheet fetch fails:** inline error message in place of the tabs, same
  pattern as `ReportViewer`'s existing `extract.error` handling — no partial or
  broken table rendering.
- **A tag was never extracted:** the leaf row still renders (from the schema
  tree) with an em-dash value and a muted "not found" state — this is coverage
  information, not an error to hide.
- **A derived value can't resolve** (missing inputs): em-dash + `Σ` glyph, tooltip
  explains which inputs are missing — mirrors `derive_facts`'s existing
  partial-fixpoint behavior server-side.
- **Provenance link on a tag with no bbox source** (defensive only — the glyph is
  conditionally rendered so this shouldn't normally occur): opened tab falls back
  to page 1, no highlight, instead of erroring.

## Testing

- **Backend:** unit tests for `tree_from_schema` (nesting preserved, facts
  attached correctly by tag, missing/unresolved values handled) alongside the
  existing `test_reasoned_v41.py`/`test_document_factsheet.py` style. Integration
  test extending `test_schema_endpoints.py` coverage to confirm
  `POST /factsheet/document` now returns `statements`.
- **Frontend:** `page.test.tsx` for both new routes (picker empty/populated
  states; detail page loading/error/populated), colocated per existing
  convention. Component test for `StatementTable` (expand/collapse, search
  filtering, derivation tooltip, provenance link href) using the existing `msw`
  handler pattern from `test/handlers.ts`.
- **Manual verification:** run the dev server, pick a filing, confirm all 4
  statement tabs render with hierarchy depth matching the source xlsx, click a
  provenance link and confirm the PDF opens in a new tab with the correct region
  highlighted.
