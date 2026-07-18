# Phase 1 — De-pollute the document model (filter table-cell paragraphs; fix dropped titles)

- **Status:** Design — 2026-07-19. Phase 1 of the ingestion improvement plan
  (`docs/architecture/2026-07-19-ingestion-audit-and-improvement-plan.md`). Depends on **Phase 0**
  (OCR schema 5, committed `fd11ee3` — provides `cells[].elements`).
- **Verified on the real doc:** Phase 0's enriched OCR yields **8,816 cells with `elements`** (the
  cell→paragraph refs), across 261 tables. Those referenced paragraph indices are exactly the
  duplicated table-cell fragments that pollute the AST.

## 1. Purpose

The audit found the single worst structural problem: Azure DI **double-emits every table cell as a
free-standing `paragraphs[]` entry**, and nothing filters them, so **42.8% of AST paragraphs are bare
numeric tokens** (e.g. `'2126.88'`) with no row/column linkage — 77% of raw paragraphs (8,816/11,428)
are these fragments. They pollute `unified` → `ast` → `chunks`/`embedding`/`graph`: they dilute
embeddings, create 18.4% duplicate chunks, and add ~8,800 meaningless graph nodes.

Phase 0 now retains `cells[].elements`, so we can identify these paragraphs **by exact Azure
reference** (not heuristics). Phase 1:

1. **Tag table-cell paragraphs** at the `normalize` layer with a new `Role.TABLE_CELL`, computed from
   `ocr["tables"][].cells[].elements`.
2. **`build_ast` excludes `table_cell` blocks** from section paragraphs — the AST becomes clean prose,
   while the table content still lives (unchanged) in the `structure` artifact.
3. **Fix the `title`-drop bug**: keep non-first `title`-role blocks as section headings (currently only
   the first becomes the doc title; 69 others — incl. the Cash Flow Statement heading — are silently
   discarded).

**Deliberately NOT in Phase 1** (deferred to keep blast radius bounded): rebuilding section
segmentation from Azure's `sections[].elements` tree, and `table_refs`/`figure_refs` membership. Those
land in **Phase 3** (table-derived chunks), which is where membership is actually consumed. Phase 1 is
the clean, low-risk, high-value de-pollution + bug fix.

## 2. Scope decisions (autonomous; documented)

1. **The table-cell paragraph set is computed from `cells[].elements`.** For every table cell, parse
   each `elements` entry matching `"/paragraphs/{n}"` → the integer `n`; the union is the set of OCR
   paragraph indices that ARE table cells. Measured: ~8,816 references resolving to a large set of the
   11,428 paragraphs. A cell may reference multiple paragraphs; a paragraph referenced by any cell is
   in the set.
2. **`normalize_document` re-roles, it does not drop.** A paragraph in the table-cell set gets
   `role = "table_cell"` (new `Role.TABLE_CELL`) **instead of** its Azure-derived role. The `blocks`
   list stays **1:1 with `ocr["paragraphs"]` in the same order and count** — purely a role change, so
   `unified` loses no data and any consumer that walks all blocks still sees everything. This is more
   conservative than deleting the paragraphs (the content is still present, just labeled), and keeps
   block↔paragraph index alignment intact.
3. **`Role.TABLE_CELL` is a new, additive enum member.** `canonical_role` is unchanged (Azure has no
   "table cell" role — the tag is assigned by index membership, not by Azure role mapping). Existing
   `Role` members and `canonical_role`'s output for real Azure roles are byte-identical.
4. **`build_ast` skips `table_cell` blocks** wherever it currently collects `body` paragraphs — the
   only place table-cell fragments used to leak in (they were `role=None → body`). No other AST logic
   changes for this filter.
5. **Title fix:** `build_ast` currently takes the first `title`-role block as `ast["title"]` and
   discards the rest. Change: the first `title` block remains `ast["title"]`; every subsequent `title`
   block is treated **as a section heading** (identical to how `heading`-role blocks start a new
   section). This recovers the 69 dropped headings (Cash Flow Statement, "Notes forming part of…",
   etc.) as real sections. AST output shape is unchanged (`{title, sections:[{heading, paragraphs}]}`).
6. **Version bumps (content-addressed re-run):** `normalize` artifact `schema_version 1 → 2` +
   `NormalizeStage.version 1 → 2` (blocks now carry `table_cell` roles); `AstStage.version` bumped
   (its output changes — cleaner paragraphs, more sections). Both cascade downstream via input-hash
   change; no other stage needs code changes (they read the same shapes, just cleaner content).
7. **No new external call, no new config.** Both `normalize_document` and `build_ast` stay pure core
   functions; the change is pure logic + version bumps.

## 3. Architecture

### 3.1 `fdi.core.unified` — `Role.TABLE_CELL` + `normalize_document`

- Add `TABLE_CELL = "table_cell"` to `Role`.
- `normalize_document(ocr)`: before the paragraph loop, build `table_cell_indices: set[int]` from
  `ocr["tables"][].cells[].elements` (parse `"/paragraphs/{n}"`). In the loop, use the paragraph's
  positional index `i`; if `i in table_cell_indices`, set `block["role"] = Role.TABLE_CELL.value`;
  else the current `canonical_role(...)`. `schema_version → 2`. Pure; a malformed elements entry is
  skipped (never raises).

```python
def _table_cell_paragraph_indices(ocr: Mapping[str, object]) -> set[int]:
    out: set[int] = set()
    tables = ocr.get("tables")
    if isinstance(tables, list):
        for t in tables:
            cells = t.get("cells") if isinstance(t, Mapping) else None
            for c in cells if isinstance(cells, list) else []:
                els = c.get("elements") if isinstance(c, Mapping) else None
                for e in els if isinstance(els, list) else []:
                    if isinstance(e, str) and e.startswith("/paragraphs/"):
                        try:
                            out.add(int(e.rsplit("/", 1)[1]))
                        except ValueError:
                            pass
    return out
```

### 3.2 `fdi.core.ast` — `build_ast`

- Skip blocks whose `role == "table_cell"` when accumulating a section's `paragraphs`.
- Title handling: first `title` block → `ast["title"]`; each later `title` block → starts a new
  section (heading = its text), exactly like a `heading` block.
- Output shape unchanged.

### 3.3 Stage versions

- `NormalizeStage.version 1 → 2`, `AstStage.version` +1 — comments note "Phase 1: table-cell
  paragraphs re-roled/excluded; non-first titles kept as sections."

## 4. Error handling

- No tables / no `cells[].elements` → empty `table_cell_indices` → `normalize_document` behaves exactly
  as before (nothing re-roled) — safe on pre-Phase-0 or table-less documents.
- Malformed `elements` entry (non-`/paragraphs/N`) → skipped.
- `build_ast` with no `title` blocks → `ast["title"]` stays `None` (unchanged); with all-`table_cell`
  section → an empty-paragraph section (already handled).
- Both functions never raise except the existing missing-`content` `ValueError`s.

## 5. Testing

- **Unit** `test_unified.py`: an ocr with a table whose cell `elements=["/paragraphs/1"]` and 3
  paragraphs → block[1] has `role=="table_cell"`, blocks 0 and 2 keep their normal roles; block count
  == paragraph count (re-role not drop); empty/malformed elements → no re-roling, no raise;
  `schema_version==2`.
- **Unit** `test_ast.py`: unified blocks with two `title` blocks + one `heading` + `table_cell` +
  `body` → `ast["title"]` = first title, a section for the second title, `table_cell` block excluded
  from paragraphs, `body` included. Confirms the 69-titles fix and the pollution filter in one.
- **Integration** `test_ast_stage.py` / `test_normalize_stage.py`: version bumps reflected; the
  chain still runs green with `FakeOcrClient`.
- **Real-doc regression (documented, run via the scratchpad audit, not committed):** after Phase 1,
  re-derive AST on STTL and confirm **table-cell paragraphs excluded** (AST paragraph count drops
  from ~10,450 toward the ~2,600 real-prose paragraphs; bare-numeric-token share collapses from 42.8%),
  and the Cash Flow Statement heading now appears as a section. Chunk dedup improves.
- **Gates:** grep for `schema_version == 1` on the normalize/unified artifact and `NormalizeStage`/
  `AstStage` version assertions; update. Full suite green.

## 6. Explicitly deferred

- **Section segmentation from Azure `sections[].elements`** (true nesting) and **`table_refs`/
  `figure_refs` membership** → Phase 3 (consumed by table-derived chunks).
- **Filtering at the chunk layer** instead of normalize — chosen normalize so *all* AST consumers
  (semantic, embedding, graph) benefit at once.

## 7. Done criteria

1. `Role.TABLE_CELL` added; `normalize_document` (schema 2) re-roles table-cell paragraphs by
   `cells[].elements` index membership, blocks stay 1:1 with paragraphs, pure, never raises.
2. `build_ast` excludes `table_cell` blocks from section paragraphs and keeps non-first `title` blocks
   as sections; output shape unchanged.
3. `NormalizeStage`/`AstStage` versions bumped; downstream re-runs via content-addressing, no other
   stage code change.
4. Unit + integration tests cover the re-role, the title fix, the exclusion, and malformed degradation;
   stale `schema_version`/`version` assertions updated. All four gates green; `core` pure.
5. Real-doc regression (scratchpad audit) shows the AST de-polluted (bare-number share collapses) and
   the dropped headings recovered — the quantified Phase-1 proof.
