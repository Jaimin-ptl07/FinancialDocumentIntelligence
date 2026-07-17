# SP-7b — Cross-page table stitching (StructureStage)

- **Status:** Implemented 2026-07-17 on `feature/sp-1-project-setup` (7b core). All §7 done criteria verified (unit + testcontainers + gates; full suite 187 passed, 4 skipped; ruff / mypy --strict / lint-imports 1 kept 0 broken).
- **Depends on:** SP-7a (enriched OCR artifact), SP-2 (stage framework), SP-3 (harness — for later candidates).
- **Research:** `docs/architecture/research/2026-07-17-sp7-structural.md` §"multi-page tables" (Microsoft's official merge heuristic, verbatim thresholds).
- **Part of:** SP-7 Structural Intelligence. This is 7b's core (vertical table stitching); horizontal-split merge, cross-page section/reading-order stitching, and reorder candidates are 7b-continuation / 7c.

## 1. Purpose

Multi-page schedules (notes to accounts, financial statements) are pervasive in annual reports, and Azure
returns each page's slice of a continued table as a SEPARATE table object — the research confirms stitching
is a required post-OCR step with no native support. SP-7b adds a `StructureStage` over the enriched OCR
artifact that **merges vertically-continued tables across page boundaries** using Microsoft's official
heuristic, and drops header/footer/page-number paragraphs, producing a **structure artifact** the AST +
graph layers consume.

Thin slice: table `spans` added to the enrichment, a pure `merge_cross_page_tables`, and a `StructureStage`.
Horizontal (left/right split) merge, section-tree stitching, and reading-order reorder candidates are
deferred (7b-cont / 7c), each to be MEASURED via SP-3 rather than asserted.

## 2. Scope decisions (autonomous; grounded in research)

1. **Implement Microsoft's VERTICAL merge heuristic** (verbatim from the Azure-Samples RAG notebook): merge
   table B into table A iff — A and B are on ADJACENT pages (A's last bounding-region page, B's first =
   A_page or A_page+1); the paragraphs whose span offset lies BETWEEN A's end and B's start are ALL
   `pageHeader`/`pageFooter`/`pageNumber` role (or none); A and B have equal `column_count`; and the
   span-offset gap between A's end and B's start ≤ 2 chars. Merge = concatenate B's non-header rows under A.
2. **This needs table `spans`.** SP-7a kept paragraph spans but not table spans. Add table-level `spans`
   (offset+length into `content`) to the OCR enrichment and bump `OcrStage.version` 2→3 (regenerates OCR +
   downstream via the version stamp — the SP-2 mechanism).
3. **Header/footer/page-number removal by Azure role** (v1 — the research's default; a repeated-text-across-
   pages candidate is a later, MEASURED addition). Removed paragraphs do not appear in the structure blocks.
4. **`StructureStage` is a derived stage** (`deps=("ocr",)`, `name="structure"`, `version=1`) producing a
   structure artifact `{schema_version, blocks:[{role,text}], tables:[stitched]}`. It sits beside `normalize`
   (both `deps=("ocr",)`); the AST (SP-8) currently reads `normalize` — a LATER part re-points the AST at
   `structure`. For now `structure` is an additional artifact; nothing downstream breaks.
5. **The merge is a pure function** (`fdi.core.structure.merge_cross_page_tables`) — the complex logic lives
   where it is exhaustively unit-testable with canned cases (continuation, non-adjacent, column mismatch,
   body-paragraph-between → no merge), not behind a stage.

## 3. Architecture

### 3.1 Table-spans enrichment (SP-7a follow-on)
`normalize_ocr` (schema_version 3): each table gains `spans: [{offset, length}]` (from Azure
`tables[].spans`). `OcrStage.version = 3`. Additive; normalize/ast self-adjust via runtime-derived keys.

### 3.2 `merge_cross_page_tables` (pure, `fdi.core.structure`)
```python
def merge_cross_page_tables(
    tables: list[Mapping], paragraphs: list[Mapping]
) -> list[dict]:
```
Iterate tables in reading order (by first span offset). For each adjacent pair (A, B) satisfying the §2.1
conditions, merge B into A (A gains B's rows: `row_count += B.row_count - (B header rows if repeated)`; cells
appended with shifted `row_index`; `bounding_regions` extended with B's; span extended to cover B).
Non-mergeable tables pass through unchanged. Returns the merged table list. Pure; deterministic.

Helpers (pure): `_adjacent(a, b)` (page numbers from `bounding_regions`), `_only_furniture_between(a, b,
paragraphs)` (paragraphs with span offset in `(a_end, b_start)` are all header/footer/page_number),
`_span_end(t)` / `_span_start(t)` (min/max over `t["spans"]`), `_gap(a, b) <= 2`.

### 3.3 `StructureStage` (`fdi.pipeline.stages.structure`)
```python
class StructureStage:
    name = "structure"; version = 1; deps = ("ocr",)
    async def run(self, ctx):
        ocr = await ctx.read("ocr")
        blocks = [{"role": canonical_role(p.get("role")).value, "text": p["content"]}
                  for p in ocr["paragraphs"]
                  if canonical_role(p.get("role")) not in {HEADER, FOOTER, PAGE_NUMBER}]
        tables = merge_cross_page_tables(ocr["tables"], ocr["paragraphs"])
        return {"schema_version": 1, "blocks": blocks, "tables": tables}
```
Reuses SP-6's `canonical_role`. Self-registers.

## 4. Error handling

- A table without `spans`/`bounding_regions` (older artifact) → treated as non-mergeable (defensive `.get`),
  passes through. No crash.
- Ambiguous multi-table-same-boundary (research open question) → the heuristic merges only strictly-adjacent
  equal-column pairs with clean furniture-between; ambiguous cases DON'T merge (conservative — a missed merge
  is recoverable downstream; a wrong merge corrupts data). Recorded as a known limitation for a measured
  candidate later.

## 5. Testing

- **Unit** (no Docker, the bulk): `merge_cross_page_tables` over canned cases — (a) a clean 2-page
  continuation (adjacent, equal columns, only pageFooter between, gap≤2) MERGES; (b) column mismatch → NO
  merge; (c) a body paragraph between → NO merge; (d) non-adjacent pages → NO merge; (e) a 3-page run merges
  into one; (f) two unrelated tables pass through. Plus the enriched `normalize_ocr` preserves table `spans`.
- **Integration** (testcontainers, fake OCR): a FakeOcrClient canned `AnalyzeResult` with a 2-page continued
  table → run `ocr → structure`; assert the structure artifact has ONE stitched table and header/footer
  blocks removed.

## 6. Explicitly deferred (YAGNI → 7b-cont / 7c / 7d)

- Horizontal (left/right split) merge (edge-coverage 99%/1%); cross-page reading-order + section-tree
  stitching; repeated-text header/footer detection; reorder candidates (XY-Cut++/LayoutReader) — each a
  MEASURED candidate via SP-3. Table-grid + nested-header canonicalization → 7c. Re-pointing the AST at
  `structure` → when `structure` supersedes `normalize`.

## 7. Done criteria

1. `normalize_ocr` (schema_version 3) preserves table `spans`; `OcrStage.version == 3`.
2. `merge_cross_page_tables` merges a clean multi-page continuation and correctly REFUSES on column mismatch
   / body-paragraph-between / non-adjacent (proven by unit cases); a 3-page run merges to one.
3. `StructureStage` (`deps=("ocr",)`) produces `{schema_version, blocks (header/footer removed), tables
   (stitched)}`; a rerun is a cache hit; `successors("ocr")` includes `"structure"`.
4. All four static gates green; `core` stays pure (merge is pure); full suite passes with no live Azure call.
