# Phase 2 — Deterministic provenance + per-cell citations

- **Status:** Design — 2026-07-19. Phase 2 of the ingestion improvement plan
  (`docs/architecture/2026-07-19-ingestion-audit-and-improvement-plan.md`). Depends on **Phase 0**
  (OCR schema 5, committed — `cells[].bounding_regions`, `cells[].elements`) and **Phase 1**
  (`normalize`/`ast` de-pollution, committed — `Role.TABLE_CELL`, non-first `title` blocks kept as
  sections). Does **not** depend on Phase 3 (table/figure graph nodes) — this phase only touches the
  `paragraph`/`section`/`document` graph nodes that already exist, plus the table `grid`.
- **Enables:** Phase 3's table/figure provenance can reuse this phase's direct-index pattern instead of
  inventing a second resolution mechanism; Phase 4/5 read `structure.tables[].grid` unchanged.

## 1. Purpose

The audit measured `build_provenance`'s actual failure mode: it resolves every graph node's page by a
**global first-match on the node's label text** against `ocr["paragraphs"]`, in document order.
**75.5% (7,888/10,450) of paragraph labels are ambiguous document-wide** — every node whose label
happens to match a common phrase resolves to wherever that phrase *first* appears, not where that node's
own text actually is. Measured: every `"Particulars"` node → page 2; "Opinion" (real pages 83, 90, 141)
→ all page 83; 108/538 (20%) headings duplicated this way. Compounding it, `build_graph` truncates every
paragraph label to 80 chars, so even the *match* can be against a truncated string (10.9% of paragraphs
lose ~72% of their text this way), and no per-cell geometry survives `canonicalize_table` at all, so a
number pulled from a table grid cannot be traced to a page or an on-page location.

**The fix is not a better heuristic — it's removing the search.** `fdi.core.unified.normalize_document`
already builds `blocks` as a 1:1 walk over `ocr["paragraphs"]` (Phase 1's own docstring: "Blocks stay 1:1
with `ocr["paragraphs"]`"). That means every block, and everything built from it, is *positionally*
traceable back to its exact source OCR paragraph — the pipeline already carries this information
implicitly, and only needs to stop discarding it. Phase 2 threads one new field —
`source_index: int | None`, the position of a block's origin paragraph in `ocr["paragraphs"]` — from
`unified` → `ast` → `graph`, so `build_provenance` becomes an O(1) array index per node instead of an
O(paragraphs) text scan (and, because it's an index and not a string comparison, it cannot be ambiguous).
Per-cell provenance is the same idea applied to `canonicalize_table`: Phase 0 already put
`cells[].bounding_regions` on every OCR table cell; `canonicalize_table` currently reads `cells[].content`
into `grid` and discards `bounding_regions`. Phase 2 threads it into a parallel `cell_regions`/`cell_pages`
matrix, the same shape convention `grid`/`column_headers` already use.

**Closes P1-2 (provenance page bug) and P3-13 (label truncation / cell provenance) from the audit.**

## 2. Scope decisions (autonomous; documented)

1. **`source_index` is threaded explicitly, not reconstructed positionally.** `normalize_document`
   already emits one block per OCR paragraph in order, but only when the raw paragraph entry is a
   `dict` (`if isinstance(para, dict): blocks.append(...)`) — a malformed paragraph entry is silently
   skipped, which means `enumerate(blocks)` position and true OCR-paragraph index are **not
   contractually guaranteed** to coincide, only true in practice on well-formed input. Rather than have
   `build_ast`/`build_graph` re-derive an index by position (fragile, undocumented coupling), Phase 2
   adds `"source_index": i` (the `enumerate(ocr["paragraphs"])` index, from `normalize_document`'s own
   loop) as an explicit field on every unified block. Every downstream layer then *carries* this int
   forward rather than re-deriving it. This is the "minimal artifact addition" the audit invited: one
   `int | None` per block, additive, at the layer closest to the source of truth.
2. **Threaded, not just added at the AST layer.** `unified` → `ast` → `graph` all gain the field, in
   that order, because each layer already has a natural per-item place to carry it (block →
   paragraph-in-section → graph node) and because Phase 3 (table/figure graph nodes) will want the same
   pattern for its own new node types — establishing it once, correctly, here.
3. **`build_ast` carries three shapes of `source_index`**, matching its three places a block's identity
   currently gets flattened into a bare string:
   - `title_source_index: int | None` — the block that became `ast["title"]` (the first `title` block).
   - per section, `heading_source_index: int | None` — the block that started the section (a `heading`
     or non-first `title` block); `None` for the headingless preamble section.
   - per section, `paragraph_source_indices: list[int | None]` — **parallel array** to `paragraphs`,
     same length, same order, one entry per accumulated paragraph string.
   A parallel array (not `paragraphs: list[{text, source_index}]`) was chosen to match the codebase's
   existing convention for this exact tradeoff: `table_grid.canonicalize_table`'s `column_headers` is
   already a parallel array to `grid`, not merged into it, specifically so old consumers reading
   `ast["sections"][i]["paragraphs"]` as `list[str]` (`graph.build_graph` and `retrieval.build_chunks`
   both do — `build_chunks` also joins `heading + "\n" + "\n".join(paragraphs)` into chunk text) keep
   working with zero code change. Converting `paragraphs` to a list of objects would be a breaking shape
   change to both; the parallel array is additive.
4. **`build_graph` carries `source_index` onto each node it already emits** (`doc`, `s{i}`, `s{i}p{j}`)
   — no new node types, no new edges. `doc.source_index = title_source_index`;
   `s{i}.source_index = heading_source_index`; `s{i}p{j}.source_index = paragraph_source_indices[j]`
   (positionally, by the same `j` already used to build the node id — so the value threading and the id
   scheme use the identical index, no separate bookkeeping).
5. **Paragraph label truncation is removed, not enlarged.** `build_graph` currently does `para[:80]`;
   section/document labels were never truncated (`heading`/`title` are stored in full already), so the
   80-char cap was an inconsistency specific to paragraph nodes, not a deliberate size budget. The label
   was *only* ever read by `build_provenance`'s now-deleted text-matching (`content.startswith(label)`)
   — with that gone, there is no remaining code path that benefits from truncation, so Phase 2 stores
   the full paragraph text. (Alternative considered: a larger fixed cap, e.g. 500 chars — rejected
   because there's no longer a reason to cap at all; the cost is graph-artifact size, not correctness,
   and paragraph text is already carried in full in `ast`/`chunks`, so this adds no new peak memory
   class to the pipeline.)
6. **`build_provenance` is rewritten to a direct index lookup; the text-matching helpers are deleted
   entirely** (`_ocr_paragraph_candidates`'s label-comparison role, the `startswith`/`==` matches). For a
   `section`/`paragraph` node: read `node["source_index"]`; if it's an `int` in range of
   `ocr["paragraphs"]`, take that paragraph's `bounding_regions` directly — no scan, no ambiguity, O(1).
   The **conservative omission policy is unchanged**: a node whose `source_index` is `None`
   (headingless preamble section), out of range, or whose paragraph has empty/unusable
   `bounding_regions`, is **omitted** from `node_provenance` — never emitted with a placeholder page.
   This is the same policy `build_provenance`'s current docstring already documents ("never emitted with
   a null/placeholder page"), now enforced by a missing index instead of a failed text match.
7. **The `doc` node keeps its existing fallback, now index-first.** If `source_index` (from
   `title_source_index`) is present and resolves to a paragraph with usable `bounding_regions`, use it
   directly. Otherwise, fall back to the **existing** "first OCR paragraph in document order with
   non-empty `bounding_regions`" scan — that scan was never the buggy part (it's a positional scan, not
   a label match; the audit's ambiguity finding is specifically about `paragraph`/`section` label
   matching), so it's kept as-is rather than rewritten, minimizing blast radius on behavior that already
   works.
8. **Per-cell provenance: parallel matrices, mirroring `grid`'s own shape convention, not a new
   per-cell object type.** `canonicalize_table` adds two new top-level keys to its existing output dict:
   - `cell_regions: list[list[list[dict]]]` — an `n_rows × n_cols` matrix, parallel to `grid`, where
     `cell_regions[r][c]` is that cell's normalized `bounding_regions` (`[{page_number, polygon}]`,
     `[]` if absent), expanded across spanning cells the exact same way `grid`/the internal `kinds`
     matrix already are (the covering cell's regions repeat into every cell it spans).
   - `cell_pages: list[list[int | None]]` — an `n_rows × n_cols` convenience matrix of just
     `cell_regions[r][c][0]["page_number"]` (or `None` if that cell has no regions).
   Two fields, not one, **mirroring `node_provenance`'s own existing convention** (each entry there
   already carries both a convenience `"page": int` and the full `"bounding_regions"` list) — for
   consistency, a per-cell citation consumer gets the same two-field shape whether it's reading
   paragraph provenance or table-cell provenance. `grid`, `column_headers`, `n_rows`, `n_cols`,
   `header_row_count` are **byte-identical** to today; every existing reader (`core/tables.py`'s
   `find_concept_rows`/`filter_tables_by_keyword`, the `/documents/{doc_id}/tables` API route) keeps
   working unchanged, ignoring the two new keys.
9. **No public API schema change in this phase.** `api/schemas.py`'s `TableGridBody` is not extended to
   expose `cell_regions`/`cell_pages` over `GET /documents/{doc_id}/tables` yet — there is no UI
   consumer today, and wiring a public response contract (plus, likely, trimming polygon payload size
   for that specific route) is a separable, later decision once a citation UI actually needs it. The
   artifact-level shape (per-cell parallel matrices, same convention as `node_provenance`) is chosen
   specifically so that a future `TableGridBody.cell_pages: list[list[int | None]] | None = None` is a
   one-line additive Pydantic field when that day comes. Explicitly deferred, §6.
10. **Version bumps.** Per SP-2's content-addressed cache (`artifact_key` includes `stage.version`
    directly — bumping a stage's `version` alone forces recompute on next `run_stage`, independent of
    whether the artifact's own internal `schema_version` field changes shape):
    - `fdi.core.unified` artifact `schema_version 2 → 3` (new `source_index` key per block — a shape
      change) + `NormalizeStage.version 2 → 3`.
    - `fdi.core.ast` artifact `schema_version 1 → 2` (new `title_source_index`/`heading_source_index`/
      `paragraph_source_indices` keys — the first AST shape change since Phase 1 kept it at 1) +
      `AstStage.version 2 → 3`.
    - `fdi.core.graph` artifact `schema_version 1 → 2` (new `source_index` key per node; label
      truncation removed) + `GraphStage.version 1 → 2`.
    - `fdi.core.provenance`: **`schema_version` stays 1** — `node_provenance` entries keep the exact
      shape `{node_id, page, bounding_regions}`; only the *resolution algorithm* changed, not the
      output shape (this mirrors Phase 1's own precedent: `ast`'s `schema_version` stayed at 1 when
      Phase 1 changed `build_ast`'s *behavior* — title/pollution handling — without changing its
      *shape*). `ProvenanceStage.version 1 → 2` **is** bumped regardless, because the cache key must
      change to force every document's `provenance` artifact to recompute — old cached provenance is
      simply wrong (the ambiguous-match bug), and `schema_version` equality is not what triggers
      recompute; `stage.version` is.
    - the pipeline's `structure` artifact (produced by `StructureStage`, which calls
      `fdi.core.table_grid.canonicalize_table` per table and nests its output under
      `structure["tables"][i]["grid"]` — `fdi.core.structure`, the cross-page-stitching module, is
      unrelated and unchanged in this phase): artifact `schema_version 2 → 3` (new `cell_regions`/
      `cell_pages` keys under `grid`, a shape change, even though it's nested rather than top-level —
      chosen to over-signal to any consumer deep-diffing `structure` output) + `StructureStage.version
      2 → 3`.
    - Cascade: bumping `NormalizeStage` re-runs `normalize → ast → graph → provenance/chunks/embedding/
      graphload/...` (everything downstream) via `derived_input_hash` on the changed `output_hash`
      chain — this is the intended, safe SP-2 propagation, not a special case Phase 2 needs to code
      around. `StructureStage` is a separate, parallel branch off `ocr` (unaffected by the
      `normalize`/`ast`/`graph`/`provenance` chain) and is bumped independently for the same reason. No
      stage downstream of these five (`normalize`, `ast`, `graph`, `provenance`, `structure`) needs a
      code change to pick up the new fields — each such stage's own version bump is what forces it to
      recompute even though its *inputs*' content also changed (either signal alone would be enough;
      both are true here); everything further downstream (`chunks`, `embedding`, `graphload`, ...)
      recomputes purely via the `input_hash` cascade, with no code or version change of its own.
11. **No new external call, no new config.** Every touched function (`normalize_document`, `build_ast`,
    `build_graph`, `build_provenance`, `canonicalize_table`) stays a pure, synchronous, no-I/O function
    in `fdi.core`. `import-linter`'s layering (`core` importing nothing from `pipeline`/`platform`) is
    unaffected — this phase adds fields, not imports.

## 3. Architecture

### 3.1 `fdi.core.unified.normalize_document` — `source_index` per block

In the existing paragraph loop (`for i, para in enumerate(paragraphs_raw): if isinstance(para, dict):
...`), add one key to each emitted `block`:

```
  block: dict[str, object] = {
      "role": role_value,
      "text": text,
+     "source_index": i,
  }
```

`i` is the loop's own enumerate index over `ocr["paragraphs"]` — the same index `_table_cell_paragraph_indices`
already parses out of `cells[].elements` (`"/paragraphs/{n}"` → `n`) to re-role table-cell blocks, so it is
already known to address `ocr["paragraphs"][i]` correctly today; Phase 2 just stops discarding it after
the loop. `schema_version → 3`. No other field changes; a malformed paragraph entry (not a `dict`) still
produces no block at all (unchanged degrade behavior) — that block position simply never gets a
`source_index` because it never gets a block.

### 3.2 `fdi.core.ast.build_ast` — carry `source_index` through title/heading/paragraph handling

The existing block walk gains, at each of its three write sites:

```
  if role == "title" and title is None:
      title = text
+     title_source_index = block.get("source_index")  # int | None
  elif role in {"header", "footer", "page_number", "table_cell"}:
      pass
  elif role == "heading" or role == "title":
      current = {"heading": text, "paragraphs": []}
+     current["heading_source_index"] = block.get("source_index")
+     current["paragraph_source_indices"] = []
      sections.append(current)
  else:
      ...
      paragraphs.append(text)
+     current["paragraph_source_indices"].append(block.get("source_index"))
```

The headingless preamble section (created lazily on the first non-heading block before any heading) gets
`"heading_source_index": None` at creation. `block.get("source_index")` degrades to `None` when the field
is absent (e.g., a `unified` artifact from before this phase's version bump, or a hand-built test fixture)
— `build_ast` never raises on a missing `source_index`; it only ever propagates whatever it finds.

Output shape:
```
{
  "schema_version": 2,
  "title": str | None,
  "title_source_index": int | None,
  "sections": [
    {
      "heading": str | None,
      "heading_source_index": int | None,
      "paragraphs": [str, ...],
      "paragraph_source_indices": [int | None, ...],  # same length as "paragraphs"
    },
    ...
  ],
}
```

### 3.3 `fdi.core.graph.build_graph` — `source_index` on every node; drop label truncation

```
  nodes: list[dict[str, object]] = [
-     {"id": "doc", "type": "document", "label": title}
+     {"id": "doc", "type": "document", "label": title, "source_index": title_source_index}
  ]
  ...
  nodes.append({
      "id": sid, "type": "section", "label": heading,
+     "source_index": heading_source_index,
  })
  ...
  nodes.append({
      "id": pid, "type": "paragraph",
-     "label": para[:80],
+     "label": para,
+     "source_index": paragraph_source_indices[j] if j < len(paragraph_source_indices) else None,
  })
```

`title_source_index`/`heading_source_index`/`paragraph_source_indices` are read from `ast` with the same
defensive `isinstance` degradation the function already applies to `title`/`sections`/`paragraphs`
(non-`int` → `None`; missing list → `[]`, so any positional read degrades to `None` rather than raising).
Edge ordering, `CONTAINS`/`READING_ORDER` emission, and node id scheme (`doc`, `s{i}`, `s{i}p{j}`) are
**unchanged** — this is purely additive per-node metadata.

### 3.4 `fdi.core.provenance.build_provenance` — direct index lookup, no text matching

The entire label-matching apparatus (`_ocr_paragraph_candidates`'s use for `startswith`/`==` comparison,
and the `next(c for c in candidates if ...)` scans) is deleted. Replacement shape:

```
def _paragraph_regions(ocr_paragraphs: list[Mapping[str, object]], index: object) -> list[dict] | None:
    """ocr_paragraphs[index]["bounding_regions"], or None if index isn't a
    valid in-range int, the paragraph isn't a Mapping, or regions are empty."""
    if not isinstance(index, int) or isinstance(index, bool):
        return None
    if index < 0 or index >= len(ocr_paragraphs):
        return None
    para = ocr_paragraphs[index]
    if not isinstance(para, Mapping):
        return None
    regions = para.get("bounding_regions")
    if not isinstance(regions, list) or not regions:
        return None
    return [dict(r) for r in regions if isinstance(r, Mapping)] or None
```

For each graph node (`section`/`paragraph` type): `regions = _paragraph_regions(ocr_paragraphs,
node.get("source_index"))`; if `regions` is `None`, **omit** the node (unchanged conservative policy);
else emit `{"node_id": ..., "page": _page_of(regions), "bounding_regions": regions}`, omitting if
`_page_of` still returns `None` (a paragraph can have geometry with no usable `page_number`, e.g.
malformed OCR — same defensive posture as today). The `doc` node: try `_paragraph_regions(ocr_paragraphs,
node.get("source_index"))` first; if `None`, fall back to the existing "first paragraph in document order
with non-empty `bounding_regions`" scan (this scan's implementation is unchanged — it was never the
ambiguity bug). `_page_of` is unchanged. `schema_version` stays `1` (§2.10).

Net effect: `build_provenance` goes from O(nodes × paragraphs) worst case (a `next()` scan per node over
up to ~11,428 candidates) to O(nodes) with O(1) work per node — a complexity class removed, not just a
correctness fix, on top of the ambiguity fix.

### 3.5 `fdi.core.table_grid.canonicalize_table` — per-cell provenance matrices

Alongside the existing `grid`/`kinds` accumulation loop (`for cell in cells: ... for r in range(...): for
c in range(...): grid[r][c] = content; kinds[r][c] = kind`), add a third parallel accumulator:

```
  cell_regions: list[list[list[dict[str, object]]]] = [[[] for _ in range(n_cols)] for _ in range(n_rows)]
  ...
  for cell in cells:
      ...
+     regions_raw = cell.get("bounding_regions")
+     regions = [dict(r) for r in regions_raw if isinstance(r, Mapping)] if isinstance(regions_raw, list) else []
      for r in range(row0, min(row0 + row_span, n_rows)):
          for c in range(col0, min(col0 + col_span, n_cols)):
              grid[r][c] = content
              kinds[r][c] = kind
+             cell_regions[r][c] = regions
  ...
+ cell_pages: list[list[int | None]] = [
+     [regions[0]["page_number"] if regions and isinstance(regions[0].get("page_number"), int) else None
+      for regions in row]
+     for row in cell_regions
+ ]

  return {
      "n_rows": n_rows,
      "n_cols": n_cols,
      "header_row_count": header_row_count,
      "column_headers": column_headers,
      "grid": grid,
+     "cell_regions": cell_regions,
+     "cell_pages": cell_pages,
  }
```

`cell.get("bounding_regions")` is exactly the field Phase 0 added to
`ocr["tables"][].cells[].bounding_regions` (verified present in `core/ocr_schema.py`'s `normalize_ocr`,
already flowing unchanged through `merge_cross_page_tables`'s per-cell dict copies into
`StructureStage.run`'s `canonicalize_table(t)` call — no upstream change needed beyond what Phase 0
already shipped). A cell with no `bounding_regions` (or a hole in the grid, per the existing
`test_missing_and_empty_cells_become_blank_in_grid` case) gets `cell_regions[r][c] == []` and
`cell_pages[r][c] is None` — the same "degrade, never fabricate" posture the rest of `core` uses.

## 4. Error handling

- `unified`: a malformed/non-`dict` `ocr["paragraphs"]` entry still yields no block (unchanged); a
  well-formed entry always gets an `int` `source_index` (its own enumerate position) — never `None` at
  this layer (a block always knows its own index).
- `ast`: a block missing `source_index` (e.g. an older cached `unified` artifact, or a hand-built test
  fixture predating this phase) degrades every derived index to `None`; `build_ast` never raises on this.
- `graph`: a non-`int` `title_source_index`/`heading_source_index`/`paragraph_source_indices` entry (or a
  missing list) degrades to `None` per node, matching the existing `isinstance` guards on `title`/
  `heading`/`paragraphs`. Never raises.
- `provenance`: `node.get("source_index")` that is `None`, non-`int`, `bool` (excluded explicitly —
  `isinstance(x, bool)` is `isinstance(x, int)` in Python), negative, or out of range for
  `ocr["paragraphs"]` → the node is **omitted**, not erred, not given a placeholder page. A paragraph at
  a valid index with empty/malformed `bounding_regions` → omitted, same as today. `doc` node's fallback
  scan degrades to no entry (already true today) if no OCR paragraph anywhere has usable geometry.
  `build_provenance({}, ocr)` / `build_provenance({"nodes": []}, ocr)` → `{"schema_version": 1,
  "node_provenance": []}`, unchanged.
- `table_grid`: a cell with a missing/non-list/malformed `bounding_regions` → `[]` for that cell's
  `cell_regions` entry, `None` for `cell_pages` (never raises, mirrors the existing `content`/`kind`
  defensive `.get()` posture already in the function). A row-count/column-count mismatch that already
  produces a "hole" in `grid`/`kinds` today (§ existing `test_missing_and_empty_cells_become_blank_in_grid`)
  produces the matching `[]`/`None` hole in `cell_regions`/`cell_pages` — no new failure mode introduced.

## 5. Testing

- **Unit `test_unified.py`** (extend): a 3-paragraph OCR input → every emitted block carries
  `"source_index"` equal to its own position; a non-`dict` paragraph entry at index 1 → blocks for index
  0 and 2 only, no `source_index` gap artifact (unchanged block-count-shrinks-on-malformed-input
  behavior, now just also checked against `source_index` values); `schema_version == 3`.
- **Unit `test_build_ast.py`** (extend): unified blocks `[title(idx0), heading(idx1), body(idx2), body(idx3)]`
  → `ast["title_source_index"] == 0`; the section's `heading_source_index == 1`;
  `paragraph_source_indices == [2, 3]` (same length/order as `paragraphs`); a preamble section (body
  before any heading) → `heading_source_index is None`; a block with `source_index` missing/non-int →
  the corresponding AST index degrades to `None`, not a raise; `schema_version == 2`.
- **Unit `test_build_graph.py`** (extend): an AST with known `title_source_index`/
  `heading_source_index`/`paragraph_source_indices` → each emitted node (`doc`, `s0`, `s0p0`, `s0p1`)
  carries the matching `source_index`; **update** `test_paragraph_label_truncated_to_80_chars` — this
  test's premise is reversed by this phase, so replace it with
  `test_paragraph_label_is_not_truncated` asserting a 200-char paragraph's node `label` is the full 200
  chars; `schema_version == 2`.
- **Unit `test_build_provenance.py`** (rewrite fixtures to the new node shape — `source_index` instead of
  relying on `label` text matching the paragraph `content`):
  - a `paragraph`/`section` node with a correct `source_index` → its exact OCR paragraph's page +
    `bounding_regions`, **regardless of label text** — the key regression case: two nodes with
    *identical* labels (e.g. two `"Particulars"` section nodes) but different `source_index` values
    resolve to **different, correct pages** — this is the direct replacement for
    `test_ambiguous_shared_prefix_resolves_to_first_in_document_order` (that test's premise — "ambiguity
    resolves to first-in-order" — no longer applies; replace it with a test proving there is no longer
    any ambiguity to resolve).
  - `source_index` missing / `None` / non-int / negative / out-of-range / pointing at a non-Mapping
    paragraph / pointing at a paragraph with empty `bounding_regions` → node omitted, `node_provenance`
    excludes it (five or six small cases, one assertion each, mirroring the existing
    `test_matched_paragraph_with_empty_bounding_regions_is_omitted` /
    `test_unmatched_node_is_omitted` style).
  - `doc` node: `title_source_index` present and valid → direct hit; `title_source_index is None` (no
    title in the doc) → falls back to the existing first-paragraph-with-regions scan (keep
    `test_document_node_uses_first_paragraph_with_regions`, adjusted to a `doc` node with no
    `source_index`).
  - `test_empty_graph_yields_empty_node_provenance`, malformed-`ocr`-paragraphs degrade tests: kept,
    adjusted to the new field name.
- **Unit `test_table_grid.py`** (extend): a cell with `bounding_regions: [{"page_number": 5, "polygon":
  [...]}]` → `cell_regions[r][c] == [{"page_number": 5, "polygon": [...]}]` and `cell_pages[r][c] == 5`;
  a row-spanning cell with regions → the regions repeat into every row it covers (mirrors
  `test_rowspan_cell_fills_down`); a cell with no `bounding_regions` / the "hole" case (no cell at all
  for a grid position) → `cell_regions[r][c] == []`, `cell_pages[r][c] is None`; existing `grid`/
  `column_headers` assertions in every existing test in this file are **unchanged and still pass**
  (per-key assertions, not full-dict equality — verified: no test in this file asserts the whole return
  dict, only individual keys).
- **Integration** `test_normalize_stage.py` / `test_ast_stage.py` / `test_graph_stage.py` /
  `test_provenance_stage.py` / `test_structure_stage.py`: version-number assertions updated (`2→3`,
  `1→2`/`2→3`, `1→2`, `1→2`, `2→3` respectively); chains still run green end-to-end through
  `FakeOcrClient`.
- **Gates:** grep for every stale `schema_version ==`/`.version ==` literal this phase's five version
  bumps touch (`unified`/`NormalizeStage` 2, `ast`/`AstStage` 1 or 2, `graph`/`GraphStage` 1,
  `ProvenanceStage` 1, `structure`/`StructureStage` 2) and update to the new numbers. Full suite green;
  `ruff`, `mypy --strict`, `lint-imports` (1-kept/0-broken, `core` still importing nothing from
  `pipeline`/`platform`).

## 6. Explicitly deferred

- **Public API exposure** of `cell_regions`/`cell_pages` via `TableGridBody`/`GET
  /documents/{doc_id}/tables` (§2.9) — no UI consumer yet; the artifact shape is deliberately chosen to
  make this a one-line additive Pydantic field later.
- **Table/figure graph nodes and their own provenance** (Phase 3) — this phase only threads
  `source_index` through the `document`/`section`/`paragraph` nodes that already exist; Phase 3's new
  node types should reuse the same `source_index`-on-node, direct-lookup pattern rather than reinventing
  text matching, but wiring them is Phase 3's scope.
- **Figure geometry** (`figures[].bounding_regions`, already retained losslessly by Phase 0) is not
  threaded into any node in this phase — no figure nodes exist yet to attach it to (Phase 3).
- **Low-confidence-word / cell flagging** using Phase 0's `low_confidence_words` — Phase 5.
- **Reconciling `cell_pages` against a table's own `bounding_regions`** (e.g. flagging a stitched
  cross-page table where a cell's page falls outside the table's overall page range) — no known
  consumer need yet; `cell_pages` is exact per-cell truth already (it comes straight from Azure's own
  per-cell `boundingRegions`), so no cross-check is required for correctness, only left as a possible
  future sanity validator.

## 7. Done criteria

1. `normalize_document` (schema 3) adds `source_index: int` to every emitted block, the OCR-paragraph
   position it came from; additive, never raises beyond existing behavior.
2. `build_ast` (schema 2) carries `title_source_index`, per-section `heading_source_index`, and
   per-section `paragraph_source_indices` (parallel to `paragraphs`); degrades to `None` on missing
   input, never raises; existing `title`/`sections`/`paragraphs` shape unchanged.
3. `build_graph` (schema 2) puts `source_index` on every node (`doc`, `s{i}`, `s{i}p{j}`) and stores
   paragraph node labels **in full** (80-char truncation removed); node id scheme and edge emission
   unchanged.
4. `build_provenance` resolves every `section`/`paragraph` node via **direct index lookup** into
   `ocr["paragraphs"][source_index]["bounding_regions"]` — no text/label matching anywhere in the
   function; the conservative "omit, never placeholder" policy for unresolvable nodes is preserved; the
   `doc` node keeps its existing (already-deterministic) fallback scan when it has no `source_index`.
5. `canonicalize_table` additively returns `cell_regions` (n_rows × n_cols matrix of each cell's
   `bounding_regions`, expanded across spans exactly like `grid`) and `cell_pages` (parallel page-number
   convenience matrix); `grid`/`column_headers`/`n_rows`/`n_cols`/`header_row_count` byte-identical to
   today.
6. `NormalizeStage`(3)/`AstStage`(3)/`GraphStage`(2)/`ProvenanceStage`(2)/`StructureStage`(3) versions
   bumped per §2.10; downstream re-runs via content-addressing with no other stage code change.
7. Unit + integration tests cover every new field, every degrade-not-raise path, and the core regression
   case (two nodes with identical label text, different `source_index`, resolving to different correct
   pages); all stale `schema_version`/`.version` literals updated. All four gates green (`ruff`,
   `mypy --strict`, `lint-imports` 1-kept/0-broken, full suite); `core` stays pure — no new I/O, no new
   external call, no new config in any touched function.
8. **Real-doc regression (scratchpad, not committed)**, on the STTL FY2023-24 doc: re-run
   `ocr → normalize → ast → graph → provenance` end-to-end; confirm (a) no two `node_provenance` entries
   with distinct `source_index` but identical label collapse to the same page unless their source
   paragraphs are genuinely on the same page (the direct fix for the "`Particulars`→page 2 always" /
   "`Opinion`→page 83 always" findings); (b) for a hand-picked ground-truth sample of statement-section
   headings (Balance Sheet, Statement of Profit & Loss, Cash Flow Statement, Notes headings — pages
   known from the source PDF), `node_provenance` page-match recall against that ground truth approaches
   the plan's ~95% target (up from the audit's measured 60.9%), with the residual gap attributable only
   to the known, documented omission set (headingless preamble sections, paragraphs with genuinely empty
   OCR geometry) rather than to any remaining mismatch; (c) on a sampled stitched table with cells on
   two different physical pages, `cell_pages` reflects the correct page per cell, not the table's first
   page uniformly.
