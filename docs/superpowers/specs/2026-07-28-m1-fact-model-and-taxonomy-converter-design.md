# Milestone 1 — Fact Model + Taxonomy→Schema Converter

**Date:** 2026-07-28
**Parent vision:** `2026-07-28-audit-grade-financial-intelligence-vision.md`
**Status:** spec (implementation follows)

## Goal

Make the customer taxonomy the source of truth for extraction, and make every
extracted value a **Fact** carrying its canonical `tag` and an `origin`
discriminator, without losing the existing cell-precise bbox provenance.

This milestone delivers the **contract** every later milestone reads from. It is
deliberately scoped to schema + extraction shape; derivation, validation, and
confidence are Milestones 2–3.

## Deliverables

1. **`fdi.core.taxonomy`** — a pure, deterministic parser: the 4-sheet xlsx →
   a normalized taxonomy document (JSON). No I/O in core; the xlsx is read by a
   thin pipeline/tooling wrapper and the normalized JSON is committed.
2. **`fdi.core.schema_builder`** — pure: normalized taxonomy → the new
   field-extraction schema document (v4.0.0), grouped into retrieval units by
   BlockID, with nested fields, per-leaf `tag`, and `origin`.
3. **Committed artifacts:** `docs/architecture/taxonomy/taxonomy.normalized.json`
   and `docs/architecture/field-extraction-schema.v4.json` (generated, reviewable).
4. **`Fact` shape** threaded through extraction: `FieldSpec` gains `tag` and
   `origin`; `assemble_result` emits `tag` and `origin` on each record; output is
   additionally addressable by tag. Bbox provenance path is unchanged.
5. **A regeneration entrypoint** (script/CLI) so the schema can be rebuilt from
   the xlsx deterministically.

## The taxonomy's real layout (per sheet)

Header row and columns differ per sheet; the parser uses an explicit per-sheet
column map, not positional guessing.

| Sheet | Hdr row | Block | Tag | Sr.No | Particulars | Flags | redir Dataset-2 | redir Dataset-1 |
|---|---|---|---|---|---|---|---|---|
| B-1 P&L | 2 | A | B (`ICICI TAG`) | C | E | G | H | I |
| B-2 Assets | 3 | A | C (`Q3 TAG`) | D | F | G | H | I |
| B-3 Liabilities | 3 | A | C | D | F | G | H | I |
| B-4 Equity | 2 | A | C | D | F | G | H | I |

Blocks (become retrieval units): P&L = PL01/PL02/PL03; Assets = BS01/BS02/BS06;
Liabilities = BS04; Equity = BS03. ~9 units total — retrieval count stays sane.

## Flag semantics (column G)

Normalized to a lowercase key, then mapped:

| Raw flag | Normalized | Effect |
|---|---|---|
| (empty) | — | extracted leaf Fact |
| `new` | `new` | extracted; `is_new: true` metadata only |
| `donot tag`, `do not tag`, `donot tag - old tag` | `derived` | `origin: DERIVED` subtotal; NOT an extracted field |
| `total to tag` | `total_tagged` | a total that IS kept as an extracted/tagged Fact |
| `not considered` (in G) | `excluded` | dropped from schema (kept in normalized doc with `excluded: true`) |

`not considered` appearing in the **redirection** columns (H/I) is different: it
means "this row has no rollup target", not that the row is excluded.

## Normalized taxonomy document shape

```json
{
  "schema_version": 1,
  "source": "Q3VENI-taxonomy-source.xlsx",
  "statements": [
    {
      "sheet": "B-1-PROFIT & LOSS",
      "statement": "profit_and_loss",
      "rows": [
        {
          "block_id": "PL01",
          "tag": "RFSP",
          "sr_no": "1.1",
          "particulars": "Revenue from Sale of Products",
          "flag": null,                      // null | new | derived | total_tagged | excluded
          "origin": "extracted",             // extracted | derived
          "redirect_dataset2": null,         // rollup target tag or null
          "redirect_dataset1": null,
          "is_section_header": false,        // Particulars present, no tag  → structural parent
          "depth": 1                         // from sr_no dot-count, else inferred
        }
      ]
    }
  ]
}
```

Rules the parser enforces (each a test case):
- A row with Particulars but **no tag** and no numeric value is a
  `is_section_header` structural parent (e.g. "Revenue From Operations").
- `origin = derived` iff flag ∈ {derived}. Everything else with a tag is
  `extracted` (including `total_tagged`, `new`).
- **Duplicate tags within a sheet** (e.g. `WCL` in Liabilities) are recorded and
  reported; the builder disambiguates by suffixing the field_path, never the tag,
  and emits a `warnings` list. A duplicate is a warning, not a hard failure.
- Whitespace/newlines in Particulars are collapsed; tags are upper-cased+trimmed.

## Hierarchy construction

`Sr. No.` is sparse, so parent/child is built from three signals in priority:
1. `Sr. No.` dot-depth when present (`1` → `1.1` → `1.1.1`).
2. Section-header rows (tagless Particulars) open a new parent scope until the
   next header at the same/înferior indentation.
3. BlockID boundaries always close open scopes.

Each `object` (parent) node in the emitted schema has `children`; each leaf has a
`tag`, `data_type`, and `origin`. `derived` parents additionally carry
`derivation: {method:"sum", inputs:[...]}` **stubs** (populated for real in M2;
here we only record the structural parent→child membership so M2 has the graph).

## Data-type inference

Default `decimal` (INR Lakhs currency). Overrides by Particulars keywords:
`%`/`ratio`/`percentage` → `decimal` tagged `is_ratio`; `days` → `integer`;
`date`/`as at`/`year ended` → `date`; `name`/`address`/`description` → `text`.
Types stay within the existing `_DATA_TYPES` set so `validate_extraction_schema`
keeps passing; new semantic hints ride as metadata, not new types.

## New schema document (v4.0.0)

Same top-level shape the pipeline already parses
(`modules → retrieval_units → fields`) so `parse_field_specs` and
`validate_extraction_schema` keep working, plus additive fields:

- module = statement (profit_and_loss, assets, liabilities, equity)
- retrieval_unit = BlockID; `retrieval_query` synthesized from the block's
  section headers + top Particulars; `locator_keywords` from section headers.
- field gains: `tag` (string), `origin` ("extracted"|"derived"),
  `is_new`/`is_ratio` flags, `redirect` object, `field_path` (unique, dotted),
  and nested `fields` for object parents.

`validate_extraction_schema` is extended (M1) to: accept nesting, require a
non-empty unique `tag` on every extracted leaf, and require `origin` ∈ set.
Extracted-leaf-only: derived nodes need no `extraction_prompt`.

## Fact shape in extraction output

`assemble_result` (fdi.pipeline.extractor) records gain:
```
{ tag, origin: "extracted", value, period, source:{...bounding_regions...}, confidence }
```
Output dict stays keyed by `field_path` (unchanged for existing consumers) and a
parallel `by_tag` index is added: `{ "RFSP": {current:..., prior:...}, ... }`.
Derived tags are absent here (they arrive in M2).

## Retrieval scaling note

Grouping by BlockID yields ~9 units for ~500 tags. Extraction already batches per
unit (`extract_unit`), so this is ~9 retrievals + ~9 LLM calls — comparable to
today's ~17. No retrieval-architecture change needed in M1.

## Testing / acceptance

Pure-core, deterministic → unit tests carry the weight:
1. Parser: golden normalized JSON for a hand-checked slice of each sheet;
   flag-mapping table fully covered; duplicate-tag warning asserted; section
   headers detected; counts match (~224/133/106/49 tags).
2. Builder: emitted schema passes `validate_extraction_schema`; every extracted
   leaf has a unique tag; derived nodes carry child membership; `parse_field_specs`
   yields the expected FieldSpec count with tags populated.
3. Extractor: `assemble_result` emits `tag`+`origin`; `by_tag` index built;
   existing bbox tests still green (regression).
4. Full backend test suite green; `validate_extraction_schema` accepts v4 doc.

## Out of scope (later milestones)

Derivation math, ratio library, validation execution, confidence, UI, XBRL.
M1 only makes the tagged, typed, hierarchical, origin-aware **contract** real.
