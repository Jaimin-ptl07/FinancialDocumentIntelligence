# SP-7b — Cross-page Table Stitching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add table `spans` to the OCR enrichment, a pure `merge_cross_page_tables` (Microsoft's vertical-merge heuristic), and a `StructureStage` that stitches multi-page tables + drops header/footer.

**Architecture:** `StructureStage` (`deps=("ocr",)`) reads the enriched OCR artifact and returns `{schema_version, blocks (header/footer removed), tables (cross-page stitched)}`. The stitch is a pure, exhaustively-unit-tested function.

**Tech Stack:** Python 3.13, the SP-2 framework, pytest + testcontainers, `uv`.

## Global Constraints

- `uv run --directory backend <cmd>` — NEVER `uv --project`. `core` pure (`merge_cross_page_tables`, `normalize_ocr` — no SDK import). Contract `1 kept, 0 broken`.
- Bumping `OcrStage.version` 2→3 (table spans): the ocr key/output_hash changes; normalize/ast/structure keys derive from `output_hash` at runtime and self-adjust; only tests HARDCODING `artifact_key(...,"ocr",2,...)` need the 2→3 edit (confirmed: `test_ocr_stage.py`).
- Merge is CONSERVATIVE: a missed merge is recoverable, a wrong merge corrupts data — refuse on any unmet condition.
- On Windows prefix `lint-imports` with `PYTHONUTF8=1`. mypy --strict: `isinstance`/`cast` for `object`-typed extraction; no blanket ignores.

**Canonical commands:** fast `pytest -m "not integration and not e2e and not azure" -q`; integration `pytest tests/integration -q`; gates `ruff check .` · `mypy --strict src` · `PYTHONUTF8=1 lint-imports --config .importlinter`.

---

### Task 1: Add table `spans` to the OCR enrichment (OcrStage.version → 3)

**Files:** Modify `backend/src/fdi/core/ocr_schema.py`, `backend/src/fdi/pipeline/stages/ocr.py`; Test `backend/tests/unit/test_ocr_schema.py`, `backend/tests/integration/test_ocr_stage.py`.

**Interfaces:** enriched `normalize_ocr` (schema_version **3**) — each table gains `spans: [{offset, length}]` (from Azure `tables[].spans`; missing → `[]`), alongside the SP-7a fields. `OcrStage.version = 3`.

- [ ] **Step 1: Failing test** — in `test_ocr_schema.py`, add `"spans": [{"offset": 40, "length": 25}]` to `_RAW`'s table, bump the asserted `schema_version` to 3, and assert `out["tables"][0]["spans"] == [{"offset": 40, "length": 25}]`.
- [ ] **Step 2: Run → fails.**
- [ ] **Step 3: Implement** — add `spans` extraction to the table mapping in `normalize_ocr`; set `schema_version = 3`; `OcrStage.version = 3`.
- [ ] **Step 4: Run → passes**; update `test_ocr_stage.py` (`"ocr", 2` → `"ocr", 3`; `schema_version == 2` → `== 3`); run `pytest tests/integration/test_ocr_stage.py tests/integration/test_normalize_stage.py tests/integration/test_ast_stage.py -q` (self-adjust) + full non-e2e suite. Gates.
- [ ] **Step 5: Commit** — `git add backend/src/fdi/core/ocr_schema.py backend/src/fdi/pipeline/stages/ocr.py backend/tests/unit/test_ocr_schema.py backend/tests/integration/test_ocr_stage.py && git commit -m "feat(ocr): preserve table spans (schema 3); OcrStage.version 3"`

---

### Task 2: `merge_cross_page_tables` (pure, core) — Microsoft's vertical-merge heuristic

**Files:** Create `backend/src/fdi/core/structure.py`; Test `backend/tests/unit/test_merge_tables.py`.

**Interfaces:** `merge_cross_page_tables(tables: list[Mapping[str, object]], paragraphs: list[Mapping[str, object]]) -> list[dict[str, object]]`. Each table dict has `row_count`, `column_count`, `spans:[{offset,length}]`, `bounding_regions:[{page_number,polygon}]`, `cells:[{row_index,column_index,row_span,column_span,kind,content}]` (the SP-7a/7b-Task-1 shape). Paragraph dict has `role` + `spans:[{offset,length}]`.

**Algorithm (transcribe exactly — the conservative MS heuristic):**
```python
"""Cross-page table stitching (Microsoft's vertical-merge heuristic). Pure — no I/O."""
from collections.abc import Mapping
from typing import cast

_FURNITURE = {"pageHeader", "pageFooter", "pageNumber"}


def _span_start(t: Mapping[str, object]) -> int:
    spans = cast(list[dict[str, int]], t.get("spans", []))
    return min((s["offset"] for s in spans), default=0)


def _span_end(t: Mapping[str, object]) -> int:
    spans = cast(list[dict[str, int]], t.get("spans", []))
    return max((s["offset"] + s["length"] for s in spans), default=0)


def _pages(t: Mapping[str, object]) -> list[int]:
    regs = cast(list[dict[str, object]], t.get("bounding_regions", []))
    return [int(cast(int, r["page_number"])) for r in regs]


def _last_page(t: Mapping[str, object]) -> int | None:
    p = _pages(t)
    return max(p) if p else None


def _first_page(t: Mapping[str, object]) -> int | None:
    p = _pages(t)
    return min(p) if p else None


def _only_furniture_between(a: Mapping[str, object], b: Mapping[str, object],
                            paragraphs: list[Mapping[str, object]]) -> bool:
    lo, hi = _span_end(a), _span_start(b)
    for p in paragraphs:
        for s in cast(list[dict[str, int]], p.get("spans", [])):
            if lo <= s["offset"] < hi:
                if p.get("role") not in _FURNITURE:
                    return False
    return True


def _can_merge(a: Mapping[str, object], b: Mapping[str, object],
               paragraphs: list[Mapping[str, object]]) -> bool:
    la, fb = _last_page(a), _first_page(b)
    if la is None or fb is None or fb != la + 1:          # adjacent pages only
        return False
    if a.get("column_count") != b.get("column_count"):    # equal columns
        return False
    gap = _span_start(b) - _span_end(a)                   # reading-order gap <= 2
    if not 0 <= gap <= 2:
        return False
    return _only_furniture_between(a, b, paragraphs)


def _merge_two(a: Mapping[str, object], b: Mapping[str, object]) -> dict[str, object]:
    a_rows = int(cast(int, a["row_count"]))
    b_rows = int(cast(int, b["row_count"]))
    a_cells = list(cast(list[dict[str, object]], a["cells"]))
    b_cells = [{**c, "row_index": int(cast(int, c["row_index"])) + a_rows}
               for c in cast(list[dict[str, object]], b["cells"])]
    return {
        "row_count": a_rows + b_rows,
        "column_count": a["column_count"],
        "spans": list(cast(list[object], a.get("spans", []))) + list(cast(list[object], b.get("spans", []))),
        "bounding_regions": list(cast(list[object], a.get("bounding_regions", [])))
                            + list(cast(list[object], b.get("bounding_regions", []))),
        "cells": a_cells + b_cells,
        "stitched": True,
    }


def merge_cross_page_tables(tables: list[Mapping[str, object]],
                            paragraphs: list[Mapping[str, object]]) -> list[dict[str, object]]:
    ordered = sorted(tables, key=_span_start)
    out: list[dict[str, object]] = []
    i = 0
    while i < len(ordered):
        cur: dict[str, object] = {**ordered[i]}
        j = i + 1
        while j < len(ordered) and _can_merge(cur, ordered[j], paragraphs):
            cur = _merge_two(cur, ordered[j])
            j += 1
        out.append(cur)
        i = j
    return out
```
(Note: repeated-header-row dedup on merge is DEFERRED — a measured refinement; the thin slice appends all continuation rows, so a merged row_count is the plain sum.)

- [ ] **Step 1: Failing test** — `backend/tests/unit/test_merge_tables.py`, canned cases:
```python
from fdi.core.structure import merge_cross_page_tables

def _t(cols, rows, page, off, length, cells=None):
    return {"row_count": rows, "column_count": cols,
            "spans": [{"offset": off, "length": length}],
            "bounding_regions": [{"page_number": page, "polygon": []}],
            "cells": cells or []}

def _foot(off): return {"role": "pageFooter", "spans": [{"offset": off, "length": 3}]}
def _body(off): return {"role": None, "spans": [{"offset": off, "length": 5}]}


def test_clean_continuation_merges() -> None:
    a = _t(3, 2, 1, 0, 50); b = _t(3, 4, 2, 52, 40)
    out = merge_cross_page_tables([a, b], [_foot(50)])   # only a footer between, gap 2, adjacent, equal cols
    assert len(out) == 1 and out[0]["row_count"] == 6 and out[0]["stitched"] is True

def test_column_mismatch_does_not_merge() -> None:
    assert len(merge_cross_page_tables([_t(3, 2, 1, 0, 50), _t(4, 4, 2, 52, 40)], [])) == 2

def test_body_paragraph_between_does_not_merge() -> None:
    assert len(merge_cross_page_tables([_t(3, 2, 1, 0, 50), _t(3, 4, 2, 52, 40)], [_body(51)])) == 2

def test_non_adjacent_pages_do_not_merge() -> None:
    assert len(merge_cross_page_tables([_t(3, 2, 1, 0, 50), _t(3, 4, 3, 52, 40)], [])) == 2

def test_three_page_run_merges_to_one() -> None:
    a = _t(2, 2, 1, 0, 10); b = _t(2, 2, 2, 12, 10); c = _t(2, 2, 3, 24, 10)
    out = merge_cross_page_tables([a, b, c], [])
    assert len(out) == 1 and out[0]["row_count"] == 6

def test_unrelated_tables_pass_through() -> None:
    out = merge_cross_page_tables([_t(2, 2, 1, 0, 10), _t(5, 3, 1, 100, 10)], [])
    assert len(out) == 2 and all("stitched" not in t for t in out)
```

- [ ] **Step 2: Run → fails** (`ModuleNotFoundError`).
- [ ] **Step 3: Implement** `structure.py` exactly as above.
- [ ] **Step 4: Run → passes** (6 tests). Gates (ruff/mypy/lint-imports).
- [ ] **Step 5: Commit** — `git add backend/src/fdi/core/structure.py backend/tests/unit/test_merge_tables.py && git commit -m "feat(core): merge_cross_page_tables — Microsoft's vertical-merge heuristic"`

---

### Task 3: `StructureStage` (pipeline) + the ocr→structure chain

**Files:** Create `backend/src/fdi/pipeline/stages/structure.py`; Modify `backend/src/fdi/pipeline/stages/__init__.py`; Test `backend/tests/integration/test_structure_stage.py`.

**Interfaces:** `class StructureStage` (`name="structure"`, `version=1`, `deps=("ocr",)`); `run(ctx)`: reads `ctx.read("ocr")`, builds `blocks` from paragraphs EXCLUDING canonical roles `HEADER`/`FOOTER`/`PAGE_NUMBER`, and `tables = merge_cross_page_tables(ocr["tables"], ocr["paragraphs"])`; returns `{"schema_version": 1, "blocks": [{"role","text"}], "tables": tables}`. Reuses `fdi.core.unified.canonical_role`, `Role`. Self-registers.

- [ ] **Step 1: Failing test** (integration; fake OCR with a 2-page continued table + a footer + a title)
```python
# backend/tests/integration/test_structure_stage.py — sketch
async def test_structure_stitches_and_filters(session_factory, store) -> None:
    import uuid
    from fdi.pipeline.intake import intake_document
    from fdi.pipeline.keys import artifact_key, root_input_hash
    from fdi.pipeline.ocr import FakeOcrClient
    from fdi.pipeline.registry import StageRegistry
    from fdi.pipeline.resolver import DagResolver
    from fdi.pipeline.run import run_stage
    from fdi.pipeline.stages.ocr import OcrStage
    from fdi.pipeline.stages.structure import StructureStage

    doc = await intake_document(pdf=b"%PDF-1.7 uniq-struct", filename="a.pdf", doc_key=str(uuid.uuid4()),
                                content_type="application/pdf", session_factory=session_factory, store=store)
    fake = FakeOcrClient({
        "content": "ACME LTD\nSchedule 1\nfoot\nSchedule 1 cont",
        "pages": [{"pageNumber": 1}, {"pageNumber": 2}],
        "paragraphs": [
            {"role": "title", "content": "ACME LTD", "boundingRegions": [{"pageNumber": 1, "polygon": []}], "spans": [{"offset": 0, "length": 8}]},
            {"role": "pageFooter", "content": "foot", "boundingRegions": [{"pageNumber": 1, "polygon": []}], "spans": [{"offset": 20, "length": 4}]},
        ],
        "tables": [
            {"rowCount": 2, "columnCount": 3, "spans": [{"offset": 9, "length": 10}], "boundingRegions": [{"pageNumber": 1, "polygon": []}], "cells": []},
            {"rowCount": 3, "columnCount": 3, "spans": [{"offset": 25, "length": 14}], "boundingRegions": [{"pageNumber": 2, "polygon": []}], "cells": []},
        ],
        "sections": [],
    })
    reg = StageRegistry(); reg.register(OcrStage()); reg.register(StructureStage())
    resolver = DagResolver(reg.all())
    assert "structure" in resolver.successors("ocr")
    common = dict(session_factory=session_factory, store=store, registry=reg, resolver=resolver)
    await run_stage(doc_id=doc.id, stage_name="ocr", run_id=str(uuid.uuid4()), correlation_id="c1", services={"ocr": fake}, **common)
    from fdi.platform.db.repository import RegistryRepository
    async with session_factory() as s:
        oh = (await RegistryRepository(s).runs_for_document(doc.id))["ocr"].output_hash
    from fdi.pipeline.keys import derived_input_hash
    await run_stage(doc_id=doc.id, stage_name="structure", run_id=str(uuid.uuid4()), correlation_id="c1", **common)
    body = await store.get_json(artifact_key(doc.id, "structure", 1, derived_input_hash([oh])))
    assert len(body["tables"]) == 1 and body["tables"][0]["row_count"] == 5   # 2-page table stitched
    assert [b["role"] for b in body["blocks"]] == ["title"]                    # footer removed
```

- [ ] **Step 2: Run → fails.**
- [ ] **Step 3: Implement** the stage; register in `stages/__init__.py` (alphabetical: `ast`, `normalize`, `ocr`, `ping`, `structure`).
- [ ] **Step 4: Run → passes**; full non-e2e suite green (registering `structure` adds it to `successors("ocr")` — membership-fix any exact-list assertion, as before). Gates.
- [ ] **Step 5: Commit** — `git add backend/src/fdi/pipeline/stages/structure.py backend/src/fdi/pipeline/stages/__init__.py backend/tests/integration/test_structure_stage.py && git commit -m "feat(pipeline): StructureStage — cross-page table stitching + header/footer removal"`

---

### Task 4: Audit + docs

- [ ] **Step 1: Gates** — ruff, mypy --strict src, PYTHONUTF8=1 lint-imports (1 kept, 0 broken; core pure).
- [ ] **Step 2: Full suite** — `pytest -q` (green; e2e+azure skip).
- [ ] **Step 3: Audit spec §7** — map criteria (1 table spans, 2 merge heuristic cases, 3 StructureStage, 4 gates) to tests.
- [ ] **Step 4: Spec status → Implemented 2026-07-17.**
- [ ] **Step 5: Roadmap** — SP-7b core done (cross-page vertical table stitching + header/footer removal via StructureStage); note deferred: horizontal merge, section/reading-order stitching, reorder + header/footer candidates (measured via SP-3), 7c table-grid/nested-header canonicalization, re-pointing AST at `structure`.
- [ ] **Step 6: Commit** — `git add docs && git commit -m "docs: SP-7b core done — cross-page table stitching; roadmap"`

## Self-Review

**Spec coverage:** §2.1 heuristic → T2 (verbatim conditions + 6 cases). §2.2 table spans → T1. §2.3 header/footer removal + §3.3 stage → T3. §5 testing → unit T1/T2, integration T3. §7 done criteria → T4.

**Type consistency:** `merge_cross_page_tables(tables, paragraphs) -> list[dict]` (T2) consumed by `StructureStage` (T3). Table dict shape (spans/bounding_regions/cells) matches T1's enrichment. `canonical_role`/`Role` (SP-6) reused in T3.

**Placeholder scan:** the header-row-dedup deferral is an explicit, justified simplification (plain-sum row_count, tested as such), not a gap. No TODO/TBD.
