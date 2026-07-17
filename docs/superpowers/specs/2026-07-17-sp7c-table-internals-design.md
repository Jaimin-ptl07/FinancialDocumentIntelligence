# SP-7c — Table internals: grid canonicalization + nested-header resolution

- **Status:** Implemented 2026-07-17 on `feature/sp-7c-table-internals`. All §7 done criteria verified (unit +
  testcontainers + gates; full suite 193 passed, 4 skipped; ruff / mypy --strict / lint-imports 1 kept 0
  broken).
- **Depends on:** SP-7a (enriched OCR artifact — table `cells` with `kind`/spans/content), SP-7b (`StructureStage`,
  cross-page-stitched tables), SP-3 (harness — for the deferred grid-accuracy candidate).
- **Research:** `docs/architecture/research/2026-07-17-sp7-structural.md` §4 "Nested / multi-row headers" — the
  key directive: table structure is NOT uniquely determined; **canonicalize to ONE convention before use**
  (aligning annotation conventions raised TATR exact-match on FinTabNet 42%→65%).
- **Part of:** SP-7 Structural Intelligence. 7a enriched the OCR payload, 7b stitched tables across pages; 7c
  canonicalizes a (possibly stitched) table's INTERNAL shape — a rectangular grid with resolved headers. 7d
  (linking: captions/footnotes/cross-refs) is next.

## 1. Purpose

Azure's cell model expresses merged cells (`row_span`/`column_span`) and header cells (`kind`) but does not
resolve them into something consumers can use directly — a downstream reader wanting "the value for column
'2023'" still has to walk sparse, spanning cells and reconstruct which header row(s) apply. The research is
explicit that table structure is under-determined and must be canonicalized to ONE convention before use.
SP-7c adds that canonicalization: a pure function that expands every spanning cell into a full rectangular
grid and resolves leading multi-row column headers into one joined-path string per column.

Thin slice, pure-core-first: `fdi.core.table_grid.canonicalize_table`, exhaustively unit-tested, then wired
additively into `StructureStage` so every table in the structure artifact carries its canonical `grid`
alongside the existing sparse-cell representation (nothing is removed). Measuring canonicalization ACCURACY
(cell-F1 / GriTS against ground truth) and deeper nested-header edge cases are explicitly deferred — this
slice establishes the convention; SP-3 measures how well it holds up on real annual-report tables later.

## 2. Scope decisions (autonomous; grounded in research)

1. **One pure function is the whole of "canonicalization."** `fdi.core.table_grid.canonicalize_table(table:
   Mapping[str, object]) -> dict[str, object]` takes a single normalized table (the SP-7a/7b shape: `row_count`,
   `column_count`, `cells: [{row_index, column_index, row_span, column_span, kind, content}]`) and returns
   `{"n_rows", "n_cols", "header_row_count", "column_headers", "grid"}`. No I/O, no stage-level logic — the
   research's "canonicalize to ONE convention" directive is a data-shape decision, not a service.
2. **Spanning cells are EXPANDED, not referenced.** A cell with `column_span=2` is written into BOTH grid
   columns it covers with the SAME content; `row_span=2` likewise fills both grid rows. This trades a small
   amount of duplication for a grid every consumer can index `grid[r][c]` without re-deriving spans — the
   simplest convention that satisfies "canonicalize to ONE convention before use."
3. **`header_row_count` is derived from Azure's `kind`, not position or heuristics.** A leading run of grid
   rows counts as header rows only while EVERY cell covering that row has `kind == "columnHeader"` (checked
   after span-expansion, so a colspan header cell counts across all the columns it covers); the first row
   with any non-`columnHeader` (or uncovered/missing) cell stops the count. Trusting Azure's own `kind`
   classification (rather than inventing a geometric header-detection heuristic) mirrors 7a/7b's "trust Azure
   roles v1; measure later" posture.
4. **`column_headers[c]` is the " / "-joined header path for column `c`,** built by walking header rows
   top-to-bottom, taking `grid[r][c]`, skipping blank (`""`) cells, and — because a rowspan header cell's
   content is duplicated into every header row it covers under decision 2 — collapsing an entry that equals
   the immediately preceding one. This is the one subtlety needed for decisions 2+3 to compose correctly: a
   header cell spanning down 2 header rows must contribute its text ONCE to the path, not twice. Example
   (from the research's "Year ended 31 March | 2023 | 2022"): a row-0 cell `content="Year ended 31 March"`
   with `column_span=2`, followed by row-1 cells `"2023"`/`"2022"` in the two columns, yields
   `column_headers == ["Year ended 31 March / 2023", "Year ended 31 March / 2022"]`.
5. **`grid` covers ALL rows (header + body), not just the body.** Downstream consumers that want "just the
   data" can slice `grid[header_row_count:]`; consumers that want the header text as literal cells (e.g. a
   verbatim re-render) still have it. No second field duplicates this.
6. **Additive wiring into `StructureStage` (bump `StructureStage.version` 1→2, and the artifact's own
   `schema_version` field 1→2 in lockstep — the same pattern 7a/7b used for `OcrStage.version`/`normalize_ocr
   schema_version`).** Each table in `tables` gains a `"grid": canonicalize_table(table)` key; the existing
   sparse `cells`/`row_count`/`column_count`/etc. are UNCHANGED. `structure` has no downstream consumer yet
   (per 7b §6), so the version bump is isolated — no other artifact/test outside `structure` itself needs a
   version-literal edit.
7. **Accuracy measurement is explicitly OUT of scope for this slice.** The research names cell-F1/GriTS
   against annotated ground truth as the way to know whether a canonicalization convention is actually
   right for Indian financial-statement tables (nested units-of-measure rows, multi-level year headers,
   footnote markers inside cells). That is a SP-3 candidate for later — this slice ships the convention
   deterministically and correctly per Azure's own cell model, which is a precondition for measuring it at
   all, not a substitute for measuring it.

## 3. Architecture

### 3.1 `canonicalize_table` (pure, `fdi.core.table_grid`)

```python
def canonicalize_table(table: Mapping[str, object]) -> dict[str, object]:
    ...
```

Algorithm:
1. Read `n_rows = row_count`, `n_cols = column_count` (defensive `int(...)`, default 0).
2. Allocate an `n_rows × n_cols` grid of `""` and a parallel `kind` grid of `None`.
3. For each cell, for every `(r, c)` it covers (`row_index..row_index+row_span-1` × `column_index..column_index
   +column_span-1`, clipped to the grid bounds), write `content` into `grid[r][c]` and `kind` into the kind
   grid — spanning cells thus appear once per covered slot (decision 2).
4. Walk rows top-to-bottom; `header_row_count` is the length of the leading run where every column's kind
   equals `"columnHeader"` (decision 3); stop at the first row that isn't fully header (or if `n_cols == 0`).
5. For each column, join non-blank `grid[r][c]` for `r` in `range(header_row_count)` with `" / "`, skipping a
   value equal to the immediately-preceding accepted value (decision 4).
6. Return `{"n_rows", "n_cols", "header_row_count", "column_headers", "grid"}`.

Pure, deterministic (no dict-ordering dependency — grid indices are explicit ints, not iteration order over
Azure's `cells` list), no I/O, no SDK import. Reuses no existing helper (a new small module — `fdi.core.
structure`'s helpers are span/page arithmetic for cross-page merge, unrelated to grid expansion).

### 3.2 `StructureStage` extension (`fdi.pipeline.stages.structure`)

```python
class StructureStage:
    name = "structure"; version = 2; deps = ("ocr",)
    async def run(self, ctx):
        ...  # unchanged: blocks filtering, tables = merge_cross_page_tables(...)
        for t in tables:
            t["grid"] = canonicalize_table(t)
        return {"schema_version": 2, "blocks": blocks, "tables": tables}
```

Runs AFTER `merge_cross_page_tables` — a stitched table's `cells` already carry shifted `row_index` (7b
`_merge_two`), so `canonicalize_table` sees one coherent cell list per (possibly multi-page) table and
produces one coherent grid, which is the point: grid canonicalization composes with cross-page stitching
for free because both operate on the same normalized cell shape.

## 4. Error handling

- A table with `cells: []` (older/degenerate artifact, or a table Azure returned with no cell detail) →
  `canonicalize_table` still returns a well-formed `{n_rows, n_cols, header_row_count: 0, column_headers: [""
  ] * n_cols, grid: <all-blank n_rows × n_cols>}`. No crash, no exception.
- `row_count`/`column_count` missing or non-numeric → default to `0`; a `0 × 0` table returns an empty grid
  and `column_headers: []`. `n_cols == 0` short-circuits header detection (an empty `all()`/`any()` over zero
  columns would otherwise vacuously classify a headerless table as all-header).
- A cell whose span would run past the table's declared `row_count`/`column_count` (malformed input) is
  clipped to the grid bounds rather than raising or growing the grid — defensive, matches 7a/7b's `.get(...)`-
  everywhere posture.
- A cell with `content: None` (should not happen post-7a normalization, which defaults it to `""`, but
  defended anyway) → treated as `""`.

## 5. Testing

- **Unit** (no Docker, the whole of this slice's substance): `canonicalize_table` over canned cases —
  (a) a simple 2×3 bordered table (one header row, no spans) → the plain grid, `header_row_count == 1`,
  `column_headers == ["A","B","C"]`; (b) a `column_span=2` header cell → its content fills BOTH grid columns
  in that row; (c) a 2-row nested header (row 0: one `column_span=2` header cell; row 1: two 1-col header
  cells) → `header_row_count == 2` and `column_headers` are the " / "-joined paths from decision 4's worked
  example; (d) missing cells (a slot with no cell in Azure's list at all) and an explicit empty-content cell
  both read back as `""` in the grid, no crash; (e) a `row_span=2` body cell fills the same content into both
  grid rows it covers. Plus a determinism check (same input → identical output twice), per the project's
  determinism invariant (roadmap §5.2).
- **Integration** (testcontainers, fake OCR): extend `test_structure_stage.py`'s existing 2-page-continuation
  fixture — after `ocr → structure`, assert the (now-stitched) table's artifact entry has a `"grid"` key with
  `n_rows`/`n_cols` matching the stitched `row_count`/`column_count`; assert the hardcoded `artifact_key(...,
  "structure", 1, ...)` literal is updated to `2` (the version bump — isolated, per decision 6, since
  `structure` has no downstream consumer to self-adjust).

## 6. Explicitly deferred (YAGNI → measured candidates / 7d)

- **Grid-accuracy measurement** (table cell-F1 / GriTS against annotated ground truth) — a SP-3 candidate;
  this slice ships the convention, not its validated accuracy on Indian financial-statement tables.
- **Deeper nested-header edge cases beyond the joined-path convention** — row headers (`kind ==
  "rowHeader"`/`"stubHead"`) canonicalized symmetrically to column headers; "projected row headers" (in-body
  group rows, per the research explicitly NOT the nested-column-header mechanism); footnote markers or units
  embedded inside header text; header rows that are not a clean leading run (e.g. a header row appearing
  after a subtotal in a malformed table). Each is a MEASURED refinement once ground truth exists, not an
  assumption baked in now.
- **Column-header-path normalization** (e.g. stripping repeated substrings, unit extraction from header
  text) — downstream semantic/ontology work (SP-10/11), not a structural concern.
- Everything already deferred by 7a/7b (horizontal table merge, reading-order repair, caption/footnote
  linking) remains deferred; unaffected by this slice.

## 7. Done criteria

1. `canonicalize_table` expands `row_span`/`column_span` cells into every grid cell they cover; a 2-row
   nested header (colspan header row + single-col header row beneath) produces correctly `" / "`-joined
   `column_headers`; missing/empty cells read back as `""`; proven by the 5 canned cases + a determinism
   check (unit, no Docker).
2. `StructureStage.version == 2`, `schema_version == 2` in its returned body; every table in the artifact's
   `tables` list carries a `"grid"` field additive to its existing sparse-cell fields; a rerun is a cache hit.
3. The `ocr → structure` integration chain (fake OCR, 2-page stitched table) passes with the updated
   version-literal and asserts the stitched table's `grid` shape matches its stitched `row_count`/
   `column_count`.
4. All four static gates green (`ruff`, `mypy --strict`, `PYTHONUTF8=1 lint-imports` 1 kept 0 broken, full
   suite); `core` stays pure (`canonicalize_table` has no I/O/SDK import).
