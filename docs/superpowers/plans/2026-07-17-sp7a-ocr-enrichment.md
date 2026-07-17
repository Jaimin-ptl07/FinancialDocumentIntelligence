# SP-7a — Enrich the OCR Artifact Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Enrich `normalize_ocr` to `schema_version: 2` (preserve paragraph geometry+spans, table cells+regions, the sections tree) and bump `OcrStage.version` to 2 — additive downstream, unblocking SP-7b/c/d.

**Architecture:** A richer `normalize_ocr` maps more of Azure's `AnalyzeResult` into the OCR artifact; `OcrStage.version=2` regenerates it. `normalize`/`ast` read `content` + `paragraphs[role,content]` (unchanged), so they're unaffected; their artifact keys derive from OCR's `output_hash` at runtime and self-adjust.

**Tech Stack:** Python 3.13, the SP-2 framework, pytest + testcontainers, `uv`.

## Global Constraints

- `uv run --directory backend <cmd>` — NEVER `uv --project`. `core` pure (`normalize_ocr` has no SDK import). Import contract `1 kept, 0 broken`.
- The enriched OCR artifact must be ADDITIVE: `paragraphs[]` keep `role` + `content` (gain fields); `content` still required (missing → `ValueError`). No live Azure in the suite (FakeOcrClient).
- Bumping `OcrStage.version` 1→2 changes the ocr artifact KEY (version) and its CONTENT (schema_version 2 + fields) → its `output_hash` changes → derived stages' keys change. Tests that DERIVE keys from `facts["ocr"].output_hash` at runtime self-adjust; only a test that HARDCODES `artifact_key(..., "ocr", 1, ...)` needs updating (confirmed: `test_ocr_stage.py` only).
- On Windows prefix `lint-imports` with `PYTHONUTF8=1`. mypy --strict: use `isinstance`/`cast` for nested `object`-typed extraction (mirror the existing `normalize_ocr`) — no blanket ignores.

**Canonical commands:** fast `pytest -m "not integration and not e2e and not azure" -q`; integration `pytest tests/integration -q`; gates `ruff check .` · `mypy --strict src` · `PYTHONUTF8=1 lint-imports --config .importlinter`.

---

### Task 1: Enrich `normalize_ocr` + bump `OcrStage.version`

**Files:** Modify `backend/src/fdi/core/ocr_schema.py`, `backend/src/fdi/pipeline/stages/ocr.py`; Test `backend/tests/unit/test_ocr_schema.py` (rewrite assertions for schema 2), `backend/tests/integration/test_ocr_stage.py` (version 1→2, schema_version 1→2).

**Interfaces:** `normalize_ocr(raw: Mapping[str, object]) -> dict[str, object]` now returns:
```python
{
  "schema_version": 2,
  "content": <str>,                          # unchanged (missing -> ValueError)
  "pages": [{"page_number","width","height","unit","line_count","word_count"}],   # unchanged
  "paragraphs": [{"role": str|None, "content": str,
                  "bounding_regions": [{"page_number": int, "polygon": list[float]}],
                  "spans": [{"offset": int, "length": int}]}],
  "tables": [{"row_count": int, "column_count": int,
              "bounding_regions": [{"page_number": int, "polygon": list[float]}],
              "cells": [{"row_index": int, "column_index": int, "row_span": int,
                         "column_span": int, "kind": str|None, "content": str}]}],
  "sections": [{"elements": list[str]}],
}
```
Azure camelCase→snake_case: `boundingRegions[].pageNumber/polygon`, `spans[].offset/length`, `cells[].rowIndex/columnIndex/rowSpan/columnSpan/kind/content`, `sections[].elements`. Missing optionals → empty lists / defaults (`row_span`/`column_span` default 1; `kind` default None; cell `content` default ""). `OcrStage.version = 2`.

- [ ] **Step 1: Write the failing test** — rewrite `backend/tests/unit/test_ocr_schema.py`'s `_RAW` + assertions to the enriched shape:
```python
_RAW = {
    "content": "ACME LTD\nDirectors' Report\nRevenue 100",
    "pages": [{"pageNumber": 1, "width": 8.5, "height": 11.0, "unit": "inch",
               "lines": [{"content": "ACME LTD"}], "words": [{"content": "ACME"}, {"content": "LTD"}]}],
    "paragraphs": [
        {"role": "title", "content": "ACME LTD",
         "boundingRegions": [{"pageNumber": 1, "polygon": [0.0, 0.0, 1.0, 0.0, 1.0, 0.2, 0.0, 0.2]}],
         "spans": [{"offset": 0, "length": 8}]},
        {"content": "Revenue 100", "boundingRegions": [], "spans": [{"offset": 9, "length": 11}]},
    ],
    "tables": [{"rowCount": 1, "columnCount": 2,
                "boundingRegions": [{"pageNumber": 1, "polygon": [1.0, 1.0, 2.0, 1.0, 2.0, 1.5, 1.0, 1.5]}],
                "cells": [{"rowIndex": 0, "columnIndex": 0, "rowSpan": 1, "columnSpan": 2,
                           "kind": "columnHeader", "content": "Year ended 31 March"}]}],
    "sections": [{"elements": ["/paragraphs/0", "/sections/1"]}],
}


def test_normalize_preserves_structural_payload() -> None:
    out = normalize_ocr(_RAW)
    assert out["schema_version"] == 2
    assert out["content"].startswith("ACME LTD")
    assert out["paragraphs"][0] == {
        "role": "title", "content": "ACME LTD",
        "bounding_regions": [{"page_number": 1, "polygon": [0.0, 0.0, 1.0, 0.0, 1.0, 0.2, 0.0, 0.2]}],
        "spans": [{"offset": 0, "length": 8}],
    }
    assert out["paragraphs"][1]["role"] is None and out["paragraphs"][1]["bounding_regions"] == []
    assert out["tables"][0]["cells"][0] == {
        "row_index": 0, "column_index": 0, "row_span": 1, "column_span": 2,
        "kind": "columnHeader", "content": "Year ended 31 March",
    }
    assert out["tables"][0]["bounding_regions"][0]["page_number"] == 1
    assert out["sections"] == [{"elements": ["/paragraphs/0", "/sections/1"]}]


def test_empty_structure_is_empty_lists() -> None:
    out = normalize_ocr({"content": "x"})
    assert out["schema_version"] == 2 and out["paragraphs"] == [] and out["tables"] == [] and out["sections"] == []


def test_missing_content_raises() -> None:
    import pytest
    with pytest.raises(ValueError, match="content"):
        normalize_ocr({"pages": []})
```

- [ ] **Step 2: Run → fails** (`uv run --directory backend pytest tests/unit/test_ocr_schema.py -q`).
- [ ] **Step 3: Implement** — extend `normalize_ocr` (defensive `.get(...)` + `isinstance`/`cast`); set `schema_version` to 2; add `bounding_regions`/`spans` to each paragraph, `bounding_regions`/`cells` to each table, and the `sections` list. Set `OcrStage.version = 2` (update its comment to note SP-7a's schema enrichment). Keep `pages` as-is.
- [ ] **Step 4: Run → passes**, then update `backend/tests/integration/test_ocr_stage.py`: the artifact key `artifact_key(doc.id, "ocr", 1, ...)` → `... "ocr", 2, ...` and any `body["schema_version"] == 1` → `== 2`. Run `uv run --directory backend pytest tests/integration/test_ocr_stage.py tests/integration/test_normalize_stage.py tests/integration/test_ast_stage.py -q` — all pass (normalize/ast self-adjust via runtime-derived keys). Then the full non-e2e suite `uv run --directory backend pytest -m "not e2e" -q`.
- [ ] **Step 5: Gates + commit** — `ruff check .`, `mypy --strict src`, `PYTHONUTF8=1 lint-imports --config .importlinter` (`1 kept, 0 broken`). Then `git add backend/src/fdi/core/ocr_schema.py backend/src/fdi/pipeline/stages/ocr.py backend/tests/unit/test_ocr_schema.py backend/tests/integration/test_ocr_stage.py && git commit -m "feat(ocr): enrich the OCR artifact with structural payload (schema 2); bump OcrStage.version"`

---

### Task 2: Audit + docs

**Files:** SP-7a spec status; `docs/architecture/ingestion-roadmap.md` (SP-7a note).

- [ ] **Step 1: Static gates** — `ruff`, `mypy --strict src`, `PYTHONUTF8=1 lint-imports` (`1 kept, 0 broken`; `core` pure).
- [ ] **Step 2: Full suite** — `uv run --directory backend pytest -q` (green; e2e + azure skip).
- [ ] **Step 3: Audit spec §7** — map each done criterion (1 enriched normalize_ocr, 2 OcrStage.version==2, 3 chain passes, 4 gates) to its test.
- [ ] **Step 4: Spec status → Implemented 2026-07-17.**
- [ ] **Step 5: Roadmap** — mark SP-7a's status in the SP-7 row (e.g. "🔨 7a done: OCR enriched"); add a note: the OCR artifact now preserves paragraph geometry+spans, table cells+regions, and the sections tree at `schema_version 2`; **SP-7b (cross-page stitching)** is next — a `StructureStage` consuming the enriched artifact that removes header/footer, stitches cross-page reading order + sections + tables (Microsoft's merge heuristic from the research doc), with candidates measured via SP-3's harness.
- [ ] **Step 6: Commit** — `git add docs && git commit -m "docs: SP-7a done — OCR artifact enriched; roadmap"`

## Self-Review

**Spec coverage:** §2 decisions → T1 (paragraph/table geometry preserved, cells kept, sections kept, version bump, additive). §3 → T1. §4 errors → T1 (missing content raises, empty → empty lists). §5 testing → T1 (unit + integration chain). §7 done criteria → T2 audit.

**Type consistency:** `normalize_ocr(raw: Mapping) -> dict` signature unchanged; only its output grows. `OcrStage.version` is read by the framework for the artifact key; downstream input_hash uses OCR's `output_hash` (runtime), not the version, so no cross-task signature drift.

**Placeholder scan:** the "self-adjust via runtime-derived keys" claim is the concrete reason normalize/ast tests need no edit (verified: they call `derived_input_hash([facts["ocr"].output_hash])`). No TODO/TBD.
