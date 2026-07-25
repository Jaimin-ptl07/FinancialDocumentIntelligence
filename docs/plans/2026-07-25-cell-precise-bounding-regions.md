# Cell-Precise Bounding Regions Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the whole-table/whole-section highlight box a table-sourced
extracted field gets today with a tight box around the specific cell the
value came from, and show a second, tagged box for the "prior" period's cell
when the field is dual-period.

**Architecture:** Post-hoc, in `fdi.pipeline.extractor._attach_regions`
(already running after order restoration, before persistence, with
`structure` in scope): match the field's own `keywords` against each table's
row-label column using the same token-subsequence algorithm
`fdi.core.tables.find_concept_rows` already uses for ontology concepts, but
keyed on the field directly. On a match, resolve `structure["tables"][i]
["grid"]["cell_regions"][r][c]` for the current and prior columns (reusing
`fdi.core.table_render`'s existing column-1-is-current/column-2-is-prior
convention). Restructure `source.bounding_regions` from a flat list into
`{current: [...], prior: [...]}`; frontend draws one tagged box per non-empty
list. No LLM/prompt/schema-file changes. Full design:
`docs/superpowers/specs/2026-07-25-cell-precise-bounding-regions-design.md`.

**Tech Stack:** Python 3.13 / FastAPI / Pydantic (backend), TypeScript /
Next.js / React (frontend), pytest, vitest.

---

### Task 1: `find_field_rows` — keyword-based row matcher

**Files:**
- Modify: `backend/src/fdi/core/tables.py`
- Test: `backend/tests/unit/test_tables.py`

**Step 1: Write the failing test**

Add to `backend/tests/unit/test_tables.py` (reuses the file's existing
`_grid` helper defined at the top):

```python
from fdi.core.tables import find_field_rows


def test_find_field_rows_matches_by_keyword_and_returns_coordinates() -> None:
    grid = _grid(
        ["Particulars", "Column 1 (most recent)", "Column 2 (prior)"],
        [["Property, plant & Equipments", "2018.29", "1838.80"]],
    )
    matches = find_field_rows(grid, ("property", "plant", "equipments"))
    assert matches == [{"row": 1, "row_label": "Property, plant & Equipments"}]


def test_find_field_rows_no_match_returns_empty() -> None:
    grid = _grid(
        ["Particulars", "Column 1 (most recent)", "Column 2 (prior)"],
        [["Intangible assets", "17.71", "21.07"]],
    )
    assert find_field_rows(grid, ("property", "plant", "equipments")) == []


def test_find_field_rows_degrades_on_malformed_grid() -> None:
    assert find_field_rows({}, ("revenue",)) == []
    assert find_field_rows({"n_rows": "bad"}, ("revenue",)) == []
```

**Step 2: Run test to verify it fails**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/unit/test_tables.py -k find_field_rows -v`
Expected: FAIL with `ImportError: cannot import name 'find_field_rows'`

**Step 3: Write minimal implementation**

Add to `backend/src/fdi/core/tables.py`, right after `find_concept_rows`
(after line 156):

```python
def find_field_rows(
    grid: Mapping[str, object], keywords: tuple[str, ...]
) -> list[dict[str, object]]:
    """Match `keywords` (a field's own tokens -- FieldSpec.keywords, already
    aliases + locator_keywords, order-preserving deduplicated) against
    `grid`'s row-label column (column 0), using the SAME normalized
    word-token-subsequence containment `find_concept_rows` uses for ontology
    synonyms -- keyed on a field directly instead of a CanonicalConcept, since
    a FieldSpec has no ontology concept of its own. Header rows
    (`grid["header_row_count"]`) are never treated as data.

    Unlike `find_concept_rows` (which returns every column's value -- it
    never picks "the" period), this returns each match's ROW INDEX, so the
    caller can look up `cell_regions[row][col]` directly for a specific
    column's geometry. `{row, row_label}` per match, in row order.

    Same KNOWN LIMITATION as find_concept_rows: a keyword can appear inside a
    label that means something else. Degrades to [] on a malformed/empty
    `grid`. Never raises."""
    n_rows_raw = grid.get("n_rows")
    n_rows = n_rows_raw if isinstance(n_rows_raw, int) and not isinstance(n_rows_raw, bool) else 0
    header_row_count_raw = grid.get("header_row_count")
    header_row_count = (
        header_row_count_raw
        if isinstance(header_row_count_raw, int) and not isinstance(header_row_count_raw, bool)
        else 0
    )

    rows_raw = grid.get("grid")
    rows = cast("list[object]", rows_raw) if isinstance(rows_raw, list) else []

    needle = [t for t in (normalize_label(k) for k in keywords) if t]
    if not needle:
        return []

    matches: list[dict[str, object]] = []
    for r in range(header_row_count, n_rows):
        row_obj = rows[r] if r < len(rows) else None
        row = cast("list[object]", row_obj) if isinstance(row_obj, list) else []

        label_obj = row[0] if row else ""
        label_raw = label_obj if isinstance(label_obj, str) else ""
        label_tokens = normalize_label(label_raw).split()
        if not label_tokens:
            continue
        if not _contains_subsequence(label_tokens, needle):
            continue

        matches.append({"row": r, "row_label": label_raw})

    return matches
```

Note: `needle` here is built from each keyword's own normalized tokens
flattened, matching how `find_concept_rows` builds `synonym_tokens` per
synonym string — but since a `FieldSpec.keywords` tuple is usually several
SEPARATE short phrases (not one multi-word phrase to subsequence-match as a
whole), test against each keyword individually instead. Fix the
implementation to loop per-keyword like `find_concept_rows` does per-synonym:

```python
    keyword_tokens = [
        tokens for k in keywords if (tokens := normalize_label(k).split())
    ]
    if not keyword_tokens:
        return []

    matches: list[dict[str, object]] = []
    for r in range(header_row_count, n_rows):
        row_obj = rows[r] if r < len(rows) else None
        row = cast("list[object]", row_obj) if isinstance(row_obj, list) else []

        label_obj = row[0] if row else ""
        label_raw = label_obj if isinstance(label_obj, str) else ""
        label_tokens = normalize_label(label_raw).split()
        if not label_tokens:
            continue
        if not any(_contains_subsequence(label_tokens, kw) for kw in keyword_tokens):
            continue

        matches.append({"row": r, "row_label": label_raw})

    return matches
```

Use this second version (mirrors `find_concept_rows`'s `synonym_tokens`
list-of-token-lists pattern exactly, at core/tables.py:128-132).

**Step 4: Run test to verify it passes**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/unit/test_tables.py -k find_field_rows -v`
Expected: PASS (3 tests)

**Step 5: Commit**

```bash
git add backend/src/fdi/core/tables.py backend/tests/unit/test_tables.py
git commit -m "feat(tables): add keyword-based row matcher for field->cell resolution"
```

---

### Task 2: `resolve_field_regions` — current/prior cell geometry lookup

**Files:**
- Modify: `backend/src/fdi/core/provenance.py`
- Test: `backend/tests/unit/test_build_provenance.py` (or a new
  `test_resolve_field_regions.py` sibling if the existing file is
  build_provenance-specific — check the file first; if it only tests
  `build_provenance`, create `backend/tests/unit/test_resolve_field_regions.py`
  instead so this stays a separate, focused test module)

**Step 1: Write the failing test**

Create `backend/tests/unit/test_resolve_field_regions.py`:

```python
from fdi.core.provenance import resolve_field_regions


def _table(cell_regions: list[list[list[dict[str, object]]]]) -> dict[str, object]:
    n_rows = len(cell_regions)
    n_cols = len(cell_regions[0]) if cell_regions else 0
    return {
        "grid": {
            "n_rows": n_rows,
            "n_cols": n_cols,
            "header_row_count": 1,
            "column_headers": ["Particulars", "Column 1 (most recent)", "Column 2 (prior)"],
            "grid": [
                ["Particulars", "Column 1 (most recent)", "Column 2 (prior)"],
                ["Property, plant & Equipments", "2018.29", "1838.80"],
            ],
            "cell_regions": cell_regions,
        }
    }


def _region(x: float) -> dict[str, object]:
    return {"page_number": 79, "polygon": [x, 0, x + 1, 0, x + 1, 1, x, 1]}


def test_resolve_field_regions_matches_row_and_splits_current_prior() -> None:
    cell_regions = [
        [[], [], []],
        [[_region(0)], [_region(1)], [_region(2)]],
    ]
    structure = {"tables": [_table(cell_regions)]}

    result = resolve_field_regions(
        keywords=("property", "plant", "equipments"),
        structure=structure,
        fallback_current=[{"page_number": 79, "polygon": [0, 0, 10, 0, 10, 10, 0, 10]}],
    )

    assert result["current"] == [_region(1)]
    assert result["prior"] == [_region(2)]


def test_resolve_field_regions_falls_back_when_no_row_match() -> None:
    cell_regions = [[[], [], []], [[_region(0)], [_region(1)], [_region(2)]]]
    structure = {"tables": [_table(cell_regions)]}
    fallback = [{"page_number": 79, "polygon": [0, 0, 10, 0, 10, 10, 0, 10]}]

    result = resolve_field_regions(
        keywords=("nonexistent", "concept"),
        structure=structure,
        fallback_current=fallback,
    )

    assert result == {"current": fallback, "prior": []}


def test_resolve_field_regions_falls_back_on_empty_structure() -> None:
    fallback = [{"page_number": 1, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}]
    result = resolve_field_regions(keywords=("x",), structure={}, fallback_current=fallback)
    assert result == {"current": fallback, "prior": []}
```

**Step 2: Run test to verify it fails**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/unit/test_resolve_field_regions.py -v`
Expected: FAIL with `ImportError: cannot import name 'resolve_field_regions'`

**Step 3: Write minimal implementation**

Add to `backend/src/fdi/core/provenance.py`. This module currently has NO
import of `fdi.core.tables` (its own docstring, lines 10-14, explicitly says
it avoids importing `fdi.core.retrieval/graph/table_grid` to stay a pure read
side with no upstream coupling) — importing `fdi.core.tables.find_field_rows`
here is a new, deliberate dependency in the OTHER direction (provenance reads
tables' matching logic, not the reverse), which is fine since `tables.py` has
no imports from `provenance.py` and this doesn't create a cycle. Add the
import at the top of `provenance.py`:

```python
from fdi.core.tables import find_field_rows
```

Then add this function after `resolve_evidence_regions` (end of file, after
line 284):

```python
def _table_grids(structure: Mapping[str, object]) -> list[Mapping[str, object]]:
    tables = structure.get("tables")
    if not isinstance(tables, list):
        return []
    out: list[Mapping[str, object]] = []
    for t in tables:
        if not isinstance(t, Mapping):
            continue
        grid = t.get("grid")
        if isinstance(grid, Mapping):
            out.append(grid)
    return out


def _cell_regions_at(grid: Mapping[str, object], row: int, col: int) -> list[dict[str, object]]:
    cell_regions = grid.get("cell_regions")
    if not isinstance(cell_regions, list) or row < 0 or row >= len(cell_regions):
        return []
    row_cells = cell_regions[row]
    if not isinstance(row_cells, list) or col < 0 or col >= len(row_cells):
        return []
    return _regions_list(row_cells[col])


def resolve_field_regions(
    *,
    keywords: tuple[str, ...],
    structure: Mapping[str, object],
    fallback_current: list[dict[str, object]],
) -> dict[str, list[dict[str, object]]]:
    """Cell-precise geometry for one extracted field, split by period.

    Matches `keywords` (a FieldSpec's own tokens) against every table's
    row-label column via `fdi.core.tables.find_field_rows` (same token-
    subsequence algorithm `resolve_chunk_regions`'s row-id path was built on,
    now reachable without an LLM-cited row id -- see
    docs/superpowers/specs/2026-07-25-cell-precise-bounding-regions-design.md).

    On the FIRST matching row across all tables: current = column 1's cell
    region, prior = column 2's cell region (fdi.core.table_render's existing
    column-1-is-current/column-2-is-prior convention -- a table with fewer
    than 3 columns has no prior column and `prior` stays []).

    On no match anywhere (narrative field, no table row found, malformed
    structure): `current` is `fallback_current` (whatever
    resolve_evidence_regions already resolved -- whole-table or whole-
    section geometry), `prior` is []. Never raises (decision 10): a failed
    cell match degrades to a coarser box, never no box, never a crash.
    """
    for grid in _table_grids(structure):
        matches = find_field_rows(grid, keywords)
        if not matches:
            continue
        row_obj = matches[0].get("row")
        if not isinstance(row_obj, int):
            continue
        current = _dedup_regions(_cell_regions_at(grid, row_obj, 1))
        prior = _dedup_regions(_cell_regions_at(grid, row_obj, 2))
        if current or prior:
            return {"current": current or fallback_current, "prior": prior}

    return {"current": fallback_current, "prior": []}
```

**Step 4: Run test to verify it passes**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/unit/test_resolve_field_regions.py -v`
Expected: PASS (3 tests)

**Step 5: Commit**

```bash
git add backend/src/fdi/core/provenance.py backend/tests/unit/test_resolve_field_regions.py
git commit -m "feat(provenance): resolve current/prior cell geometry for a field"
```

---

### Task 3: Wire `resolve_field_regions` into `_attach_regions`

**Files:**
- Modify: `backend/src/fdi/pipeline/extractor.py:334-354` (`_attach_regions`), `:454-461` (its call site in `extract_document`)
- Test: `backend/tests/integration/test_extract_document.py`

**Step 1: Write the failing test**

Add to `backend/tests/integration/test_extract_document.py` (check the
existing `processed_doc`/`_schema` fixtures at the top of this file first —
reuse them; the shape below assumes the same fixtures already used by
`test_extract_document_makes_one_llm_call_per_unit`, adjust field/table
setup to match how that fixture actually builds its `structure` artifact if
different from this sketch):

```python
async def test_extract_document_gives_table_field_a_cell_precise_box(
    session_factory, store, processed_doc
) -> None:
    """A numeric field whose keywords match a table row must get a
    bounding_regions shape of {current, prior}, with `current` a strict
    subset of geometry (fewer/smaller regions) than the table's own
    whole-table box -- proving the cell-level lookup actually ran, not the
    old whole-table fallback."""
    doc, marker, embedder, vector_store = processed_doc

    field = FieldSpec(
        field_name="Property plant equipment net", data_type="decimal",
        retrieval_query=f"manufacturing company {marker}",
        keywords=("property", "plant", "equipment"),
        extraction_prompt="Extract property plant and equipment net.",
        enum_values=None, module_id=1, module_name="M", unit_id="U1",
        unit_title="T-U1", field_path="m.ppe", periods=("current", "prior"),
        locator_keywords=("total", "assets"),
    )
    fake = FakeExtractorClient([
        {"m.ppe": {
            "value": {"current": 2018.29, "prior": 1838.80},
            "confidence": 1.0,
            "source": {"evidence_ids": ["t0"]},
        }},
    ])

    async with session_factory() as s:
        ctx = ExtractContext(
            session=s, store=store, embedder=embedder, vector_store=vector_store,
            doc_id=doc.id, doc_key=doc.doc_key, version=doc.version,
        )
        result = await extract_document(ctx=ctx, fields=[field], client=fake)

    bounding_regions = result["fields"][0]["source"]["bounding_regions"]
    assert set(bounding_regions.keys()) == {"current", "prior"}
    # cell-level geometry must be present and distinct from an empty result
    assert bounding_regions["current"] != [] or bounding_regions["prior"] != []
```

If the `processed_doc` fixture's fake table doesn't actually contain a row
matching `("property", "plant", "equipment")` with real `cell_regions`, this
test will only exercise the fallback path (still valid, but doesn't prove
cell-precision). Check the fixture's `structure`/table setup
(`test_extract_document.py`'s top-level fixtures) and adjust the field's
`keywords` and the fixture's fake table row-label to align on a real match —
or add a small standalone table fixture inline in this test, following
`test_extract_document_basis_filter_drops_consolidated_table_for_standalone_unit`'s
pattern of NOT using `processed_doc` when a bespoke table shape is needed.

**Step 2: Run test to verify it fails**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/integration/test_extract_document.py -k cell_precise -v`
Expected: FAIL — `bounding_regions` is still a flat list (`TypeError` or
`AttributeError` on `.keys()`), or `AssertionError` on the shape check.

**Step 3: Write minimal implementation**

In `backend/src/fdi/pipeline/extractor.py`, change `_attach_regions`
(lines 334-354):

```python
def _attach_regions(
    record: dict[str, object],
    field: FieldSpec,
    *,
    doc_id: str,
    provenance: Mapping[str, object],
    structure: Mapping[str, object],
) -> dict[str, object]:
    """Add `source.bounding_regions` to one assemble_result record, as
    `{current: [...], prior: [...]}` -- cell-precise when `field`'s keywords
    match a table row (fdi.core.provenance.resolve_field_regions), falling
    back to the field's whole-table/whole-section geometry
    (resolve_evidence_regions) under `current` with `prior: []` otherwise.
    Never raises."""
    source = record.get("source")
    if not isinstance(source, dict):
        return record
    evidence_ids = source.get("evidence_ids")
    ids: Sequence[object] = evidence_ids if isinstance(evidence_ids, list) else []
    fallback = resolve_evidence_regions(doc_id, ids, provenance, structure)
    source["bounding_regions"] = resolve_field_regions(
        keywords=field.keywords, structure=structure, fallback_current=fallback,
    )
    return record
```

Add the import at the top of `extractor.py` (near the existing
`from fdi.core.provenance import resolve_evidence_regions` line):

```python
from fdi.core.provenance import resolve_evidence_regions, resolve_field_regions
```

Update the call site (lines 454-461) to pass each result's matching
`FieldSpec` — `results` and `fields` are already positionally aligned at
this point in `extract_document` (the line right before builds `results`
from `fields` in order):

```python
    results = [
        _attach_regions(
            r, f, doc_id=ctx.doc_id, provenance=provenance, structure=structure
        )
        for r, f in zip(results, fields, strict=True)
    ]
```

**Step 4: Run test to verify it passes**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/integration/test_extract_document.py -k cell_precise -v`
Expected: PASS

**Step 5: Run the FULL extract_document test file to check nothing broke**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/integration/test_extract_document.py -v`
Expected: all tests PASS. (Existing tests that assert on `bounding_regions`
being a flat list, if any, will need their assertions updated to the new
`{current, prior}` shape in this step — fix them here, not in a later task.)

**Step 6: Commit**

```bash
git add backend/src/fdi/pipeline/extractor.py backend/tests/integration/test_extract_document.py
git commit -m "feat(extractor): attach cell-precise current/prior bounding regions"
```

---

### Task 4: Bump cache version, update API schema shape

**Files:**
- Modify: `backend/src/fdi/core/extractor.py` (`EXTRACTION_RECORD_VERSION`, line 518)
- Modify: `backend/src/fdi/api/schemas.py:461-477` (`BoundingRegionView`, `ExtractSourceView`)
- Test: `backend/tests/integration/test_extract_document.py` (cache tests already exist per this session's earlier work — verify they still pass, don't need new ones)

**Step 1: Bump the cache version**

In `backend/src/fdi/core/extractor.py`, change:

```python
EXTRACTION_RECORD_VERSION = 1
```

to:

```python
EXTRACTION_RECORD_VERSION = 2
```

Add a one-line comment above it: `# v2: bounding_regions is {current, prior}, not a flat list (see 2026-07-25 design)`.

This changes the content-addressed cache key
(`extraction_key(doc_id, EXTRACTION_RECORD_VERSION, schema_hash)`), so any
extraction record persisted under v1 becomes an automatic cache miss under
v2 — the approved handling per the design doc, no migration code needed.

**Step 2: Update the API schema shape**

In `backend/src/fdi/api/schemas.py`, replace lines 461-477:

```python
class BoundingRegionView(BaseModel):
    """One page-scoped bounding region, in the shape `ocr`/`provenance`
    already store: `page_number` plus Azure's flat coordinate list."""

    page_number: int | None = None
    polygon: list[object] = []


class BoundingRegionsView(BaseModel):
    """A field's bounding geometry, split by period. `current` is always a
    list (possibly the whole-table/whole-section fallback box); `prior` is
    [] for a non-dual-period field or when no prior-column cell was found."""

    current: list[BoundingRegionView] = []
    prior: list[BoundingRegionView] = []


class ExtractSourceView(BaseModel):
    page: int | None
    snippet: str | None
    evidence_ids: list[str]
    # {current, prior}, not a flat list -- see 2026-07-25 design. A record
    # persisted before this change (schema_version 1) will not validate here;
    # EXTRACTION_RECORD_VERSION was bumped alongside this so a v1 record is a
    # cache miss (re-extracted fresh), never fed to this validator.
    bounding_regions: BoundingRegionsView = BoundingRegionsView()
```

**Step 3: Run the schema-adjacent tests**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/integration/test_extract_document.py tests/integration/test_schema_endpoints.py -v`
Expected: all PASS.

**Step 4: Run the full backend suite**

Run: `cd backend && .venv/Scripts/python.exe -m pytest -q`
Expected: all PASS. Fix any test that constructs an `ExtractSourceView` or
raw dict with the old flat-list `bounding_regions` shape (search: `grep -rn
"bounding_regions" backend/tests`).

**Step 5: Commit**

```bash
git add backend/src/fdi/core/extractor.py backend/src/fdi/api/schemas.py
git commit -m "feat(api): bounding_regions is {current, prior}; bump cache version"
```

---

### Task 5: Frontend types + dual-box rendering

**Files:**
- Modify: `frontend/lib/types.ts:52-59`
- Modify: `frontend/components/viewer/PdfCanvas.tsx`
- Modify: `frontend/components/viewer/ReportViewer.tsx:167` (prop passed to `PdfCanvas`)
- Test: none exist yet for `PdfCanvas` (per this session's earlier investigation)
  — write a new `frontend/components/viewer/PdfCanvas.test.tsx`

**Step 1: Update the type**

In `frontend/lib/types.ts`, replace lines 52-59:

```typescript
export interface BoundingRegionsView {
  current: BoundingRegion[];
  prior: BoundingRegion[];
}

export interface ExtractSourceView {
  page: number | null;
  snippet: string | null;
  evidence_ids: string[];
  /** Split by period — `current` is always a list (possibly a whole-table/
   *  whole-section fallback box); `prior` is `[]` for a non-dual-period
   *  field or when no prior-column cell was found. */
  bounding_regions: BoundingRegionsView;
}
```

**Step 2: Write the failing test**

pdfjs-dist cannot run under jsdom (per the existing mock convention in
`ReportViewer.test.tsx:10-12`), so mock it here too. Create
`frontend/components/viewer/PdfCanvas.test.tsx`:

```typescript
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { PdfCanvas } from "./PdfCanvas";

vi.mock("pdfjs-dist", () => ({
  GlobalWorkerOptions: {},
  getDocument: () => ({
    promise: Promise.resolve({
      numPages: 1,
      getPage: () =>
        Promise.resolve({
          getViewport: () => ({ width: 100, height: 100 }),
          render: () => ({ promise: Promise.resolve(), cancel: () => {} }),
        }),
    }),
  }),
}));

const region = (x: number) => ({ page_number: 1, polygon: [x, 0, x + 1, 0, x + 1, 1, x, 1] });

describe("PdfCanvas", () => {
  it("tags a prior-period box distinctly from the current one", async () => {
    render(
      <PdfCanvas
        url="http://example.com/doc.pdf"
        page={1}
        regions={{ current: [region(0)], prior: [region(5)] }}
      />,
    );
    expect(await screen.findByText("New")).toBeInTheDocument();
    expect(screen.getByText("Prior")).toBeInTheDocument();
  });

  it("shows only a current box, no tag, when there is no prior region", async () => {
    render(
      <PdfCanvas
        url="http://example.com/doc.pdf"
        page={1}
        regions={{ current: [region(0)], prior: [] }}
      />,
    );
    await screen.findByRole("presentation", { hidden: true }).catch(() => {});
    expect(screen.queryByText("New")).not.toBeInTheDocument();
    expect(screen.queryByText("Prior")).not.toBeInTheDocument();
  });
});
```

(The second test's `findByRole` line is a wait-for-render nicety, not a hard
assertion — if it's awkward under jsdom timing, replace with
`await new Promise((r) => setTimeout(r, 0))` before the `queryByText`
assertions, matching whatever wait pattern the codebase's other async
component tests already use.)

**Step 3: Run test to verify it fails**

Run: `cd frontend && npx vitest run components/viewer/PdfCanvas.test.tsx`
Expected: FAIL — `regions` prop type mismatch (still `BoundingRegion[] | null`) and no "New"/"Prior" text rendered yet.

**Step 4: Write minimal implementation**

In `frontend/components/viewer/PdfCanvas.tsx`, change the props type and the
render logic. Replace:

```typescript
import type { BoundingRegion } from "@/lib/types";
```

with:

```typescript
import type { BoundingRegion, BoundingRegionsView } from "@/lib/types";
```

Replace the `regions` prop type (line 22):

```typescript
  regions: BoundingRegionsView | null;
```

Replace the region-filtering + rendering block (lines 111-166). Currently:

```typescript
  const onPage = (regions ?? []).filter((r) => r.page_number === page);
```

becomes two lists, current and prior, each filtered to the visible page:

```typescript
  const currentOnPage = (regions?.current ?? []).filter((r) => r.page_number === page);
  const priorOnPage = (regions?.prior ?? []).filter((r) => r.page_number === page);
```

Factor the existing single-box JSX into a small local helper so both lists
render through the same markup with a different tag/style, rather than
duplicating the whole block. Replace the `{onPage.map(...)}` block with:

```typescript
      {renderBoxes(currentOnPage, "New", scale)}
      {renderBoxes(priorOnPage, "Prior", scale)}
```

And add this function above `boxOf` at the bottom of the file:

```typescript
function renderBoxes(regions: BoundingRegion[], tag: "New" | "Prior", scale: number) {
  return regions.map((region, index) => {
    const box = boxOf(region.polygon);
    return (
      <div
        key={`${tag}-${index}`}
        aria-hidden="true"
        className="absolute pointer-events-none tether-highlight"
        style={{
          left: box.x * scale * POINTS_PER_INCH - 3,
          top: box.y * scale * POINTS_PER_INCH - 3,
          width: box.width * scale * POINTS_PER_INCH + 6,
          height: box.height * scale * POINTS_PER_INCH + 6,
          padding: 3,
          border: tag === "Prior" ? "1px dashed var(--pdf-mark)" : "1px solid var(--pdf-mark)",
          borderRadius: "var(--radius-sm)",
          boxShadow: "0 1px 4px color-mix(in srgb, var(--pdf-mark) 18%, transparent)",
        }}
      >
        <div
          style={{
            width: "100%",
            height: "100%",
            border: tag === "Prior" ? "2px dashed var(--pdf-mark)" : "2px solid var(--pdf-mark)",
            background: "var(--pdf-mark-wash)",
            borderRadius: "var(--radius-sm)",
          }}
        />
        <span
          className="data"
          style={{
            position: "absolute",
            top: -18,
            left: 0,
            fontSize: "var(--text-2xs)",
            fontWeight: 600,
            padding: "1px 4px",
            background: "var(--pdf-mark)",
            color: "var(--leaf)",
            borderRadius: "var(--radius-sm)",
            whiteSpace: "nowrap",
          }}
        >
          {tag}
        </span>
      </div>
    );
  });
}
```

Note: the removed `highlightRef`/scroll-into-view effect (lines 26,
107-109) referenced `onPage[0]` implicitly via the ref on `index === 0`.
Rewire it onto the FIRST current-period box instead (scrolling should always
follow the current value, not whichever renders first):

```typescript
  const highlightRef = useRef<HTMLDivElement>(null);
```

stays as-is; thread the ref through `renderBoxes` only for the current tag's
first box — change the call site to:

```typescript
      {renderBoxes(currentOnPage, "New", scale, highlightRef)}
      {renderBoxes(priorOnPage, "Prior", scale)}
```

and give `renderBoxes` an optional 4th param:

```typescript
function renderBoxes(
  regions: BoundingRegion[],
  tag: "New" | "Prior",
  scale: number,
  firstRef?: React.RefObject<HTMLDivElement>,
) {
  return regions.map((region, index) => {
    const box = boxOf(region.polygon);
    return (
      <div
        key={`${tag}-${index}`}
        ref={index === 0 ? firstRef : undefined}
        ...
```

**Step 5: Run test to verify it passes**

Run: `cd frontend && npx vitest run components/viewer/PdfCanvas.test.tsx`
Expected: PASS (2 tests)

**Step 6: Update the call site**

In `frontend/components/viewer/ReportViewer.tsx`, line 167 already passes
`active?.source.bounding_regions ?? null` — this now type-checks against
`BoundingRegionsView | null` automatically since the type changed in Step 1.
No code change needed here; just re-run the type-checker to confirm:

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors.

**Step 7: Run the full frontend suite**

Run: `cd frontend && npx vitest run`
Expected: all PASS. If `Value.test.tsx` or any other test constructs a raw
`ExtractFieldView`/`ExtractSourceView` fixture with the old flat-list
`bounding_regions: []`, update those fixtures to
`bounding_regions: { current: [], prior: [] }` in this step.

**Step 8: Commit**

```bash
git add frontend/lib/types.ts frontend/components/viewer/PdfCanvas.tsx frontend/components/viewer/PdfCanvas.test.tsx
git commit -m "feat(viewer): render tagged current/prior bounding boxes"
```

---

### Task 6: Manual verification against the real document

**Files:** none (verification only, no commit)

**Step 1: Restart the backend**

The cache version bump (Task 4) means the next `/extract/document` call for
`Final-Annual-Report-2021-2022-01_09.pdf` will be a fresh, full re-extraction
(the v1-keyed cached record is now unreachable). Rebuild and restart:

```bash
cd docker && docker compose build api worker && docker compose up -d --force-recreate api worker
```

**Step 2: Re-extract and inspect**

In the browser, open the report viewer for
`Final-Annual-Report-2021-2022-01_09.pdf`, click "Re-extract" (or wait for
the auto-triggered fresh read since the cache missed), and once figures
finish reading, select "Property plant equipment net" (or any other numeric
table field). Confirm:

- The highlighted box is now sized to roughly one table cell, not the whole
  table.
- A second, dashed box with a "Prior" tag appears for the prior-period
  column, alongside the "New"-tagged current box.
- A narrative (non-numeric, non-table) field still shows its original
  whole-section box with no "Prior" tag (dual-box is table-only, per the
  approved design scope).

If cell matching is missing for some fields (row-label token match fails —
expected per the design's documented limitation), confirm those fields
gracefully fall back to the old whole-table/whole-section box rather than
showing nothing.

**Step 3: Report findings**

No commit for this task — it's a manual check. If gaps are found (e.g. a
specific row-label phrasing the matcher misses), file them as follow-up
items rather than expanding this plan's scope.

---

### Task 7: Update the design doc's status

**Files:**
- Modify: `docs/superpowers/specs/2026-07-25-cell-precise-bounding-regions-design.md`

**Step 1: Add an implementation note**

Append a short section at the end of the design doc:

```markdown
## Implementation status

Implemented per `docs/plans/2026-07-25-cell-precise-bounding-regions.md`.
`find_field_rows` (core/tables.py) and `resolve_field_regions`
(core/provenance.py) do the row/column matching; `_attach_regions`
(pipeline/extractor.py) wires it in. `EXTRACTION_RECORD_VERSION` bumped to 2
for the shape change. Frontend renders both boxes via `PdfCanvas.tsx`'s
`renderBoxes` helper.
```

**Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-07-25-cell-precise-bounding-regions-design.md
git commit -m "docs: mark cell-precise bounding regions design as implemented"
```
