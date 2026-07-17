# SP-7c — Table Internals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add a pure `canonicalize_table` (spanning-cell grid expansion + joined-path nested-header
resolution — the research's "canonicalize to ONE convention" directive), and wire it additively into
`StructureStage` so every table in the structure artifact carries a `grid`.

**Architecture:** `fdi.core.table_grid.canonicalize_table(table) -> dict` is the whole of the new logic — pure,
exhaustively unit-tested. `StructureStage` (bumped `version` 1→2) calls it per (possibly cross-page-stitched)
table after `merge_cross_page_tables`, additive to the existing sparse-cell fields.

**Tech Stack:** Python 3.13, the SP-2 framework, pytest + testcontainers, `uv`.

## Global Constraints

- `uv run --directory backend <cmd>` — NEVER `uv --project`. `core` pure (`canonicalize_table` — no SDK
  import, no I/O). Contract `1 kept, 0 broken`.
- Bumping `StructureStage.version` 1→2: `structure` has NO downstream consumer yet (per SP-7b §6), so this
  bump is isolated — only tests HARDCODING `artifact_key(...,"structure",1,...)` need the 1→2 edit (confirmed:
  `test_structure_stage.py`).
- Grid expansion is the CONVENTION, not a measured-accurate result: ship it deterministic and correct against
  Azure's own cell model; accuracy against ground truth is a later SP-3 candidate (see spec §6).
- On Windows prefix `lint-imports` with `PYTHONUTF8=1`. mypy --strict: `isinstance`/`cast` for `object`-typed
  extraction; no blanket ignores.

**Canonical commands:** fast `pytest -m "not integration and not e2e and not azure" -q`; integration `pytest
tests/integration -q`; gates `ruff check .` · `mypy --strict src` · `PYTHONUTF8=1 lint-imports --config
.importlinter`.

---

### Task 1: `canonicalize_table` (pure core) — grid expansion + nested-header join

**Files:** Create `backend/src/fdi/core/table_grid.py`; Test `backend/tests/unit/test_table_grid.py`.

**Interfaces:** `canonicalize_table(table: Mapping[str, object]) -> dict[str, object]` returning `{"n_rows":
int, "n_cols": int, "header_row_count": int, "column_headers": list[str], "grid": list[list[str]]}`. Input
`table` has `row_count`, `column_count`, `cells: [{row_index, column_index, row_span, column_span, kind,
content}]` (the SP-7a/7b normalized shape).

**Implementation (transcribe exactly):**
```python
"""Table-grid canonicalization: expand spanning cells into a rectangular grid
and resolve nested/multi-row column headers to ONE joined-path convention.
Pure -- no I/O. (SP-7c; research: "canonicalize to ONE convention before use".)"""

from collections.abc import Mapping
from typing import cast

_HEADER_KIND = "columnHeader"


def _cell_int(cell: Mapping[str, object], key: str, default: int) -> int:
    value = cell.get(key)
    return int(cast(int, value)) if value is not None else default


def canonicalize_table(table: Mapping[str, object]) -> dict[str, object]:
    """Expand a table's spanning cells into a full rectangular grid and
    resolve its leading column-header rows to a single joined-path convention.

    Args:
        table: a normalized table mapping (`row_count`, `column_count`,
            `cells: [{row_index, column_index, row_span, column_span, kind,
            content}]` -- the SP-7a/7b enrichment shape).

    Returns:
        `{"n_rows", "n_cols", "header_row_count", "column_headers", "grid"}`.
        `grid` is the `n_rows` x `n_cols` matrix of cell content, spanning
        cells repeated into every cell they cover. `column_headers[c]` is the
        " / "-joined header path for column `c`, skipping blank cells and
        collapsing an entry equal to the immediately preceding one (a
        row-spanning header cell repeats its content into every header row
        it covers).
    """
    n_rows = _cell_int(table, "row_count", 0)
    n_cols = _cell_int(table, "column_count", 0)
    cells = cast("list[Mapping[str, object]]", table.get("cells") or [])

    grid: list[list[str]] = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    kinds: list[list[object]] = [[None for _ in range(n_cols)] for _ in range(n_rows)]

    for cell in cells:
        row0 = _cell_int(cell, "row_index", 0)
        col0 = _cell_int(cell, "column_index", 0)
        row_span = _cell_int(cell, "row_span", 1)
        col_span = _cell_int(cell, "column_span", 1)
        content = cast("str | None", cell.get("content")) or ""
        kind = cell.get("kind")
        for r in range(row0, min(row0 + row_span, n_rows)):
            for c in range(col0, min(col0 + col_span, n_cols)):
                grid[r][c] = content
                kinds[r][c] = kind

    header_row_count = 0
    for r in range(n_rows):
        if n_cols == 0 or any(kinds[r][c] != _HEADER_KIND for c in range(n_cols)):
            break
        header_row_count += 1

    column_headers: list[str] = []
    for c in range(n_cols):
        parts: list[str] = []
        for r in range(header_row_count):
            value = grid[r][c]
            if value and (not parts or parts[-1] != value):
                parts.append(value)
        column_headers.append(" / ".join(parts))

    return {
        "n_rows": n_rows,
        "n_cols": n_cols,
        "header_row_count": header_row_count,
        "column_headers": column_headers,
        "grid": grid,
    }
```

- [ ] **Step 1: Failing test** — `backend/tests/unit/test_table_grid.py`:
```python
from fdi.core.table_grid import canonicalize_table


def _cell(
    row: int, col: int, content: str, kind: str | None = None,
    row_span: int = 1, col_span: int = 1,
) -> dict[str, object]:
    return {
        "row_index": row, "column_index": col, "row_span": row_span,
        "column_span": col_span, "kind": kind, "content": content,
    }


def test_simple_bordered_table_builds_grid() -> None:
    table = {
        "row_count": 2, "column_count": 3,
        "cells": [
            _cell(0, 0, "A", kind="columnHeader"),
            _cell(0, 1, "B", kind="columnHeader"),
            _cell(0, 2, "C", kind="columnHeader"),
            _cell(1, 0, "1"),
            _cell(1, 1, "2"),
            _cell(1, 2, "3"),
        ],
    }
    out = canonicalize_table(table)
    assert out["n_rows"] == 2 and out["n_cols"] == 3
    assert out["header_row_count"] == 1
    assert out["column_headers"] == ["A", "B", "C"]
    assert out["grid"] == [["A", "B", "C"], ["1", "2", "3"]]


def test_colspan_header_cell_expands_to_two_columns() -> None:
    table = {
        "row_count": 2, "column_count": 3,
        "cells": [
            _cell(0, 0, "Year ended 31 March", kind="columnHeader", col_span=2),
            _cell(0, 2, "Item", kind="columnHeader"),
            _cell(1, 0, "2023"),
            _cell(1, 1, "2022"),
            _cell(1, 2, "Foo"),
        ],
    }
    out = canonicalize_table(table)
    assert out["grid"][0] == ["Year ended 31 March", "Year ended 31 March", "Item"]
    assert out["n_cols"] == 3


def test_nested_two_row_header_joins_paths() -> None:
    table = {
        "row_count": 2, "column_count": 2,
        "cells": [
            _cell(0, 0, "Year ended 31 March", kind="columnHeader", col_span=2),
            _cell(1, 0, "2023", kind="columnHeader"),
            _cell(1, 1, "2022", kind="columnHeader"),
        ],
    }
    out = canonicalize_table(table)
    assert out["header_row_count"] == 2
    assert out["column_headers"] == [
        "Year ended 31 March / 2023",
        "Year ended 31 March / 2022",
    ]


def test_missing_and_empty_cells_become_blank_in_grid() -> None:
    table = {
        "row_count": 2, "column_count": 2,
        "cells": [
            _cell(0, 0, "H1", kind="columnHeader"),
            _cell(0, 1, "", kind="columnHeader"),  # explicit empty content
            _cell(1, 0, "x"),
            # (1, 1) has no cell at all -- a hole in Azure's cell list
        ],
    }
    out = canonicalize_table(table)
    assert out["grid"] == [["H1", ""], ["x", ""]]


def test_rowspan_cell_fills_down() -> None:
    table = {
        "row_count": 2, "column_count": 2,
        "cells": [
            _cell(0, 0, "Total", row_span=2),
            _cell(0, 1, "X"),
            _cell(1, 1, "Y"),
        ],
    }
    out = canonicalize_table(table)
    assert out["grid"] == [["Total", "X"], ["Total", "Y"]]


def test_pure_and_deterministic() -> None:
    table = {
        "row_count": 1, "column_count": 1,
        "cells": [_cell(0, 0, "X", kind="columnHeader")],
    }
    assert canonicalize_table(table) == canonicalize_table(table)
```

- [ ] **Step 2: Run → fails** (`ModuleNotFoundError: fdi.core.table_grid`).
```
uv run --directory backend pytest tests/unit/test_table_grid.py -q
```
- [ ] **Step 3: Implement** `backend/src/fdi/core/table_grid.py` exactly as above.
- [ ] **Step 4: Run → passes** (6 tests). Gates:
```
uv run --directory backend pytest tests/unit/test_table_grid.py -q
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
```
- [ ] **Step 5: Commit** — `git add backend/src/fdi/core/table_grid.py backend/tests/unit/test_table_grid.py && git commit -m "feat(core): canonicalize_table -- grid expansion + nested-header join"`

---

### Task 2: Wire `canonicalize_table` into `StructureStage` (version 1→2)

**Files:** Modify `backend/src/fdi/pipeline/stages/structure.py`; Test `backend/tests/integration/test_structure_stage.py`.

**Interfaces:** `StructureStage.version = 2`; `run(ctx)` returns `{"schema_version": 2, "blocks": [...], "tables":
[... each with an added "grid": canonicalize_table(table) key ...]}`. Everything else in the stage (blocks
filtering, `merge_cross_page_tables` call) is UNCHANGED.

- [ ] **Step 1: Failing test** — edit `backend/tests/integration/test_structure_stage.py`:
  1. Bump the hardcoded version literal in the `artifact_key(...)` call from `1` to `2`:
  ```python
  structure_key = artifact_key(
      doc.id, "structure", 2, derived_input_hash([facts["ocr"].output_hash])
  )
  ```
  2. Give the FIRST fake table (the one that survives stitching as table `A`, page 1) two real cells so the
     grid assertion is non-trivial — change its `"cells": []` to:
  ```python
              {
                  "rowCount": 2,
                  "columnCount": 3,
                  "spans": [{"offset": 9, "length": 10}],
                  "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                  "cells": [
                      {"rowIndex": 0, "columnIndex": 0, "kind": "columnHeader", "content": "Year"},
                      {"rowIndex": 0, "columnIndex": 1, "kind": "columnHeader", "content": "Q1"},
                      {"rowIndex": 0, "columnIndex": 2, "kind": "columnHeader", "content": "Q2"},
                  ],
              },
  ```
  3. Add, after the existing `assert [b["role"] ...]` line:
  ```python
      grid = body["tables"][0]["grid"]
      assert grid["n_rows"] == 5 and grid["n_cols"] == 3
      assert grid["header_row_count"] == 1
      assert grid["column_headers"] == ["Year", "Q1", "Q2"]
  ```
- [ ] **Step 2: Run → fails** (`artifact_key` version mismatch — no `v2` artifact exists yet — then, once the
  version literal alone is fixed, `KeyError: "grid"`).
```
uv run --directory backend pytest tests/integration/test_structure_stage.py -q
```
- [ ] **Step 3: Implement** — in `backend/src/fdi/pipeline/stages/structure.py`:
  1. Add `from fdi.core.table_grid import canonicalize_table` to the imports.
  2. Change `version = 1` to `version = 2`.
  3. After computing `tables = merge_cross_page_tables(tables, paragraphs)` (rename the intermediate result if
     needed, or reassign in place), add:
  ```python
      stitched = merge_cross_page_tables(tables, paragraphs)
      for t in stitched:
          t["grid"] = canonicalize_table(t)
  ```
  4. Return `{"schema_version": 2, "blocks": blocks, "tables": stitched}`.

  Full resulting `run` body:
  ```python
      async def run(self, ctx: StageContext) -> object:
          ocr = cast(Mapping[str, object], await ctx.read("ocr"))
          paragraphs = cast(list[Mapping[str, object]], ocr.get("paragraphs", []))
          tables = cast(list[Mapping[str, object]], ocr.get("tables", []))
          blocks = [
              {
                  "role": canonical_role(cast("str | None", p.get("role"))).value,
                  "text": cast(str, p.get("content", "")),
              }
              for p in paragraphs
              if canonical_role(cast("str | None", p.get("role"))) not in _DROP
          ]
          stitched = merge_cross_page_tables(tables, paragraphs)
          for t in stitched:
              t["grid"] = canonicalize_table(t)
          return {
              "schema_version": 2,
              "blocks": blocks,
              "tables": stitched,
          }
  ```
- [ ] **Step 4: Run → passes**; run the full non-e2e suite + integration to confirm no other test hardcodes
  `"structure", 1`:
```
uv run --directory backend pytest -m "not integration and not e2e and not azure" -q
uv run --directory backend pytest tests/integration -q
```
  Gates (ruff/mypy/lint-imports) as in Task 1 Step 4.
- [ ] **Step 5: Commit** — `git add backend/src/fdi/pipeline/stages/structure.py backend/tests/integration/test_structure_stage.py && git commit -m "feat(pipeline): StructureStage emits canonical table grids (version 2)"`

---

### Task 3: Audit + docs

- [ ] **Step 1: Gates** — `ruff check .`, `mypy --strict src`, `PYTHONUTF8=1 lint-imports --config .importlinter`
  (1 kept, 0 broken; `core` stays pure).
- [ ] **Step 2: Full suite** — `uv run --directory backend pytest -q` (green; e2e + azure skip).
- [ ] **Step 3: Audit spec §7** — map done criteria (1 `canonicalize_table` unit cases, 2 `StructureStage`
  version/schema bump + additive `grid`, 3 integration chain, 4 gates) to the tests written in Tasks 1-2.
- [ ] **Step 4: Spec status** — update `docs/superpowers/specs/2026-07-17-sp7c-table-internals-design.md`'s
  `Status` line to `Implemented <date> on feature/sp-1-project-setup. All §7 done criteria verified (unit +
  testcontainers + gates; full suite <N> passed, <M> skipped; ruff / mypy --strict / lint-imports 1 kept 0
  broken).`
- [ ] **Step 5: Roadmap** — update `docs/architecture/ingestion-roadmap.md`'s SP-7c row (⬜ → done) and the
  Stage-1-line summary; note deferred: grid-accuracy measurement (cell-F1/GriTS, a SP-3 candidate), row-header
  canonicalization, projected-row-header handling, header-path text normalization (SP-10/11 concern) — then
  point at SP-7d (linking: captions/footnotes/cross-references) as next.
- [ ] **Step 6: Commit** — `git add docs && git commit -m "docs: SP-7c done -- table-grid canonicalization; roadmap"`

## Self-Review

**Spec coverage:** §2 decisions 1-5 (pure function, span expansion, `kind`-driven header detection, joined-path
+ dedup convention, full-grid-not-just-body) → Task 1's implementation + 5 canned cases + determinism check.
§2 decision 6 (additive `StructureStage` wiring, version bump) → Task 2. §5 testing → unit (Task 1), integration
(Task 2). §7 done criteria → Task 3's audit.

**Type consistency:** `canonicalize_table(table: Mapping[str, object]) -> dict[str, object]` (Task 1) consumed
per-table inside `StructureStage.run` (Task 2) on the SAME dict shape `merge_cross_page_tables` already returns
(`row_count`, `column_count`, `cells`) — no shape translation needed between 7b's stitch output and 7c's grid
input, which is the point of both operating on one normalized cell representation.

**Placeholder scan:** grid-accuracy measurement and row-header/projected-row-header handling are explicit,
justified deferrals (spec §6), not gaps — no TODO/TBD in the transcribed code.
