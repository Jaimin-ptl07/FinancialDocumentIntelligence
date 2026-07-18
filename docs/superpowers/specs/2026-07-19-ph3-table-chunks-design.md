# Phase 3 — Tables into the retrieval corpus

- **Status:** Design — 2026-07-19. Phase 3 of the ingestion improvement plan
  (`docs/architecture/2026-07-19-ingestion-audit-and-improvement-plan.md`). Depends on **Phase 1**
  (de-polluted AST, committed) for the clean `ast` this spec extends. Does **not** depend on Phase 2
  (deterministic provenance) — this spec deliberately routes table/row citations through `structure`'s
  own `bounding_regions`, not through `provenance`, precisely so it does not have to wait on Phase 2 (see
  §2 decision 6). Sibling of Phase 2 in the plan's DAG (both fan out from Phase 1).
- **Verified on the real doc (from the audit):** tables = 25.1% of the document and **100% of the
  numbers**; after Phase 1, they are *fully* absent from chunks/embedding/graph (the accidental
  number-leak via duplicated table-cell paragraphs is gone) — `get_tables` is the only tool that sees
  them. 261 tables, 33 figures. Azure's `sections[].elements` hierarchy resolves to 608 section-nestings
  + 261 table refs + 33 figure refs + 2,195 paragraph refs (audit §3, row #1).

## 1. Purpose

Close the audit's headline gap: make table content **structured, searchable chunks** — reachable via
`hybrid_search` (vector + keyword), not only via the separate `get_tables` tool — and make tables/figures
first-class **graph nodes** so RT-5's graph tools can browse/cite them. Three deliverables:

1. **Table-derived chunks** in the `chunks` artifact (`ChunksStage`), one per data row + one per whole
   table, additive to the existing per-section chunks.
2. **`EmbeddingStage` reads `chunks` instead of re-deriving section text from `ast`** — the single
   source-of-truth refactor that makes embedding (and therefore Milvus, therefore vector search) cover
   table chunks for free, with zero change to `IndexStage`.
3. **`table`/`figure` graph nodes** with `CONTAINS` edges from their owning section, plus the mandatory
   Neo4j label-allowlist extension without which the real store hard-fails on load.

Plus: exact-duplicate dedup in `build_chunks` (closes the 18.4%-duplicate finding for good, not just for
section chunks).

## 2. Scope decisions (autonomous; documented) — read this section first, it drives everything below

1. **Grain: per-row chunks are primary; one whole-table chunk is added for context (recommend BOTH —
   flagged as a fork, §6/final summary).** Per-cell is rejected outright: a cell in isolation ("2126.88")
   is exactly the structure-free, ambiguous fragment Phase 1 just finished removing from the AST (audit:
   "`inventories=66.88` literally matches 47 chunks") — reintroducing it at the chunk layer would undo
   Phase 1's win. Per-row is the natural "line item" unit: it pairs a label with every period's value in
   one self-contained, embeddable string (`"Property, plant and equipment: 31-03-2024=2126.88,
   31-03-2023=2016.80"`), matches how a financial question is actually phrased ("what was PP&E last
   year"), and keeps chunk COUNT bounded (261 tables × ~8 data rows/table average ≈ 2,000 new chunks —
   comparable order of magnitude to the existing 538 section chunks, not an explosion). Per-table-only
   was rejected because it reintroduces the audit's OTHER finding almost verbatim: 4.6% of section chunks
   are "huge (≥3,000 chars), dilution" — a 24-row balance sheet rendered as one chunk has the same
   embedding-dilution problem prose sections have, just with numbers instead of paragraphs; a query about
   one line item would have to compete against the whole table's embedding. The **whole-table chunk is
   additive, not a replacement** — it exists for statement-level queries ("show me the balance sheet")
   where a single row chunk is too narrow, capped in length (§3.2) to avoid re-introducing dilution.
2. **Row-chunk id: `t{tableIndex}r{rowIndex}`; whole-table id: `t{tableIndex}` (bare) — deliberately the
   SAME string a §2 decision 11 graph `table` node uses for the same real-world table (`t{structureIndex}`
   in both places; different artifacts/namespaces, no actual collision, but a future citation-joining
   feature can treat the two as the same key with zero translation).**
   `tableIndex` is the position in **`structure["tables"]`** (post-stitch, post-sort) — **the same
   `table_index` RT-6's `get_tables`/`concept_lookup` already use** (`fdi/pipeline/tables.py`:
   `{"table_index": i, **t}` for `i, t in enumerate(_tables_of(structure))`). Reusing this exact
   convention means a client can cross-reference a chunk hit `t3r5` against `get_tables(table_index=3)`
   with zero translation. `rowIndex` is the row's **absolute index into `grid["grid"]`** (the same integer
   `core.tables.find_concept_rows` iterates with, `range(header_row_count, n_rows)`) — not a
   data-row-relative counter — so it is directly usable as `grid["grid"][rowIndex]` for debugging/citation
   without an off-by-`header_row_count` correction.
3. **Row-chunk text:** `f"{context} | {row_label}: {col1}={val1}, {col2}={val2}, ..."` — values are the
   **raw cell string** (not `parse_numeric`-parsed; parsing is RT-6's job, this is a search/embedding
   surface, and raw text preserves "(1,234)"/currency markers a numeric-only render would strip), blank
   cells (empty string after `.strip()`) are **omitted** from the pair list entirely (not rendered as
   `col=`) to keep the string dense. `context` is, in priority order: (a) the owning AST section's
   heading, if resolved (§2 decision 5); (b) the table's own `caption.content` if Azure supplied one
   (Phase 0 already retains this); (c) `f"Table {tableIndex}"` as the final fallback — never blank,
   matching this codebase's "degrade, never omit" posture.
4. **Whole-table chunk text:** `context` + column-header line + every data row rendered the same
   `"{row_label}: {col}={val}, ..."` way, one per line, truncated to `max_chars=4000` (reusing
   `core.semantic.build_snippet`'s truncate-don't-raise pattern, sized larger than its 2000-char default
   because a table row is denser information-per-char than prose).
5. **Table→section membership via Azure's `sections[].elements` hierarchy, threaded through `ast`
   (not a new `GraphStage`/`ChunksStage` dep on raw `ocr`) — flagged as a fork, §6.** A new pure function
   `core.ast.attach_table_refs(ast, unified, ocr)` DFS-flattens `ocr["sections"]` (root section's
   `elements`, recursing into `"/sections/N"` refs) into the document-order sequence of
   `("paragraphs"|"tables"|"figures", index)` refs, and replays it in lockstep with the SAME
   heading-boundary rule `build_ast` already uses (a block is a new-section boundary iff
   `unified["blocks"][i]["role"] in {"heading"}` or a non-first `"title"`) to track "which AST section is
   currently open." A `("tables", M)` ref appends `M` to the open section's new `table_refs: list[int]`
   (raw `ocr["tables"]` index); a `("figures", K)` ref appends `{"index": K, "caption": ocr["figures"][K]
   ["caption"]}` to `figure_refs`. This works because the audit's own measured hierarchy counts explain
   the 2,195-of-11,428 paragraph-ref gap: 2,195 ≈ the ~2,612 non-table-cell paragraphs (11,428 total minus
   8,816 table-cell fragments), i.e. Azure's hierarchy already enumerates almost exactly the same
   paragraphs that survive Phase 1's filter into AST sections, PLUS every table/figure ref at its correct
   interleaved position — so replaying it against `build_ast`'s own heading rule stays in lockstep without
   needing to solve the harder "reconcile Azure's nested section tree with our flat heading-derived
   sections" problem Phase 1 explicitly deferred. **Documented assumption, not proven for every
   heading**: every AST section-starting heading paragraph appears as an explicit `"/paragraphs/N"` ref
   somewhere in the flattened Azure sequence. Defensive fallback if a table/figure ref is encountered
   before any heading has opened a section: attach to the preamble section (index 0) if one exists, else
   create one — mirrors `build_ast`'s own preamble handling; never dropped, never raised.
   `AstStage.deps` gains `"ocr"` (→ `("normalize", "ocr")`), the second 2+-dep DERIVED stage after
   `ProvenanceStage`. `table_refs`/`figure_refs` are **additive** keys on each section dict — `heading`/
   `paragraphs` unchanged, so `SemanticStage` (still `ast`-only) is unaffected.
6. **Raw ↔ stitched table-index bridge: `source_indices` on `structure`'s output tables.**
   `attach_table_refs`' `table_refs` are necessarily **raw** `ocr["tables"]` indices (pre-stitch,
   pre-sort — that is all `build_ast`/`attach_table_refs` can see; `structure` doesn't exist yet at
   `ast`-build time in the DAG). But chunk/graph table ids use the **`structure`** index (decision 2, and
   RT-6 precedent). `core.structure.merge_cross_page_tables` therefore additively emits
   `source_indices: list[int]` per output table — the list of original (pre-sort) indices that
   contributed to that output entry (`[i]` for an unmerged table, `[i, j, ...]` for a stitched run). A
   new shared pure helper, `core.table_grid.raw_to_structure_index(tables) -> dict[int, int]`
   (raw index → structure position, last-write-wins if a raw index somehow appears in two entries — should
   not happen, `merge_cross_page_tables` partitions the input), is used by BOTH `build_chunks` (row/table
   context + page) and `build_graph` (CONTAINS-edge attachment) — one implementation, not two. This keeps
   `t{i}` **identical** between the `chunks` artifact, `get_tables`' `table_index`, and the graph's table
   nodes, whenever every raw index maps to exactly one structure entry (true today: stitching threshold
   is unreachable per the audit, §5, so `structure["tables"]` is currently a 1:1, same-order copy of
   `ocr["tables"]` — but the bridge is what makes the id scheme correct *if/when* Phase 5 fixes stitching
   and merges start happening, not just today).
7. **Page: from `structure`'s own `bounding_regions`, not from `provenance`.** A table/row/whole-table
   chunk's `page` is `structure["tables"][tableIndex]["bounding_regions"][0]["page_number"]` (first
   region — same "first region" convention `core.provenance._page_of` already uses for paragraphs).
   This is deliberately **independent of the `provenance` artifact** (whose `node_provenance` only
   resolves `"paragraph"`/`"section"`/`"doc"` graph-node types today — a table id like `t3r5` was never a
   graph node in the pre-Phase-3 schema and has no provenance entry, and won't gain one until Phase 2
   extends `build_provenance` to handle `"table"`/`"figure"` node types too, which is explicitly NOT this
   phase's job). Section chunks' `page` lookup (via `provenance.node_provenance`) is **unchanged**.
8. **`chunks` artifact shape: additive, not restructured.** Every existing key (`chunk_id`, `heading`,
   `text`, `section_type`, `page`, `para_count`) is unchanged and still present (table chunks set
   `para_count: None` — not applicable). Two **new**, optional keys are added to every chunk record:
   `chunk_type: "section" | "table_row" | "table"` (explicit discriminator — a consumer should not have
   to parse the `chunk_id` string's `s`/`t` prefix to know what it got) and `table_index`/`row_index`
   (`int | None`, `None` for section chunks, `row_index: None` for the whole-table chunk). Section chunks
   get `chunk_type: "section"` for symmetry. `chunk_rows_for_index` (the FTS bridge) reads only the 5
   pre-existing keys it already reads — **no `Chunk` ORM/Postgres migration needed**; the two new keys
   flow into the `chunks` artifact (JSON, schemaless) and are visible to any code that reads that artifact
   directly, but are not (yet) projected into the FTS table. This is a deliberate scope cut (§6).
9. **Dedup: one global exact-text pass over the FULL combined chunk list (sections + table rows + whole
   tables), in `build_chunks`, AFTER assignment of ids.** Keep first occurrence in emission order (sections
   first, then per-table row/whole-table groups in `structure["tables"]` order); drop later chunks whose
   `text` is byte-identical to an earlier one — **never renumber the ids of the ones that survive**. This
   is consistent with the ALREADY-tested "gaps in `s{i}` are fine" contract
   (`test_chunk_id_mirrors_enumerate_skip`), so dedup slots into the existing contract rather than
   inventing a new one. Because `text` is context-prefixed (§2 decision 3), two genuinely different table
   rows that happen to share a bare label+values but sit under different section/table context do **not**
   collapse — only true duplicates do.
10. **Embedding reads `chunks`, not `ast` — `EmbeddingStage.deps: ("ast",) → ("chunks",)` — flagged as a
    fork, §6 (real re-embedding cost).** `EmbeddingStage.run` becomes: read `chunks`, embed
    `[c["text"] for c in chunks["chunks"]]` in list order, emit `{"chunk_id": c["chunk_id"], "heading":
    c["heading"], "vector": v}` per entry — **same output shape as today**, just chunk_id/heading/text
    sourced from the one place that now builds them (`build_chunks`) instead of re-derived independently.
    **Verified, not assumed: `IndexStage` needs ZERO code change.** Its metadata join
    (`meta_by_chunk = {c["chunk_id"]: c for c in chunks_art["chunks"]}`, then `meta_by_chunk.get(str(chunk
    ["chunk_id"]))` per embedded chunk) is already generic over `chunk_id` — it does not special-case
    section ids in any way, so a `t3r5` entry in `embedding.chunks` joins against a `t3r5` entry in
    `chunks_art.chunks` exactly like an `s4` entry does today, picking up the same `section_type`/`page`
    metadata enrichment RT-1 v2 already built. **Verified: RT-1's `resolve_chunks`/`resolve_chunk`
    (`fdi/pipeline/retrieval.py`) need ZERO code change** — they resolve a hit id's `chunk_id` against the
    `chunks` artifact by string match only, format-agnostic; a `t3r5` hit resolves exactly like an `s4`
    hit. `IndexStage.version` stays **2** (unchanged code); it still re-runs automatically because its
    `deps=("embedding","chunks")` input_hash changes when `embedding`'s output_hash changes (the
    established content-addressing mechanism — no manual bump needed for a stage whose own code is
    untouched, same reasoning Phase 0 documented for `normalize`'s downstream). `EmbeddingStage.version:
    1 → 2` (code changed) — its docstring's non-determinism/cache-hit guarantee
    (`derived_input_hash([ast.output_hash])` → same argument, now keyed on `chunks.output_hash`) is
    unchanged in spirit: same `chunks` artifact → same input_hash → cache hit, no re-embed call.
11. **Table/figure graph nodes: `GraphStage.deps: ("ast",) → ("ast", "structure")` — not a new `ocr`
    dep — flagged as a fork alongside decision 5, §6.** `build_graph(ast, structure)` gains a second
    param. For each AST section `s{i}` with non-empty `table_refs`, translate raw indices to structure
    indices via decision 6's shared helper; for each resulting structure index `t`, emit node `{"id":
    f"t{t}", "type": "table", "label": <caption content, else f"Table ({n_rows}x{n_cols})">}` +
    edge `{"source": f"s{i}", "target": f"t{t}", "type": "CONTAINS"}` (first section wins if a stitched
    table's `source_indices` ever straddle a section boundary — documented, should not occur since
    stitching only merges same-statement page-continuations). For `figure_refs` (already `{"index",
    "caption"}` dicts, no translation needed — figures are never reordered/merged), emit `{"id":
    f"f{k}", "type": "figure", "label": <caption, else f"Figure {k}">}` + the same `CONTAINS` edge shape.
    **No `READING_ORDER` edges for table/figure nodes** — out of scope; `order_reading_chain` and RT-5's
    `document_structure`/`reading_order` chain-walking stay section/paragraph-only (§6).
    `GraphStage.version: 1 → 2`.
12. **Neo4j label allowlist MUST be extended — mandatory, not optional.** `fdi/platform/graphstore.py`'s
    `Neo4jGraphStore._load_tx` maps `node["type"]` through a **closed** allowlist
    (`_NODE_LABELS = {"document": "Document", "section": "Section", "paragraph": "Paragraph"}`) and
    **raises `ValueError` on an unknown type** (RT-5 decision 2, by design — a fail-loud guard against
    Cypher label injection, not a fallback). Without extending this dict with `"table": "Table",
    "figure": "Figure"`, the FIRST `graph_load` run against a Phase-3 `graph` artifact containing a table
    node would hard-fail against a real Neo4j instance — a correctness-blocking omission the audit's own
    `#8` row ("figures[].boundingRegions/elements — geometry dropped") implicitly assumes gets fixed here.
    `GraphLoadStage` itself needs **no** code change (its namespacing (`_namespace_node`/`_namespace_edge`)
    is already generic over any `{id, type, label}` dict) and its `.version` stays **2** — it re-runs
    automatically via `graph`'s output_hash changing (decision 11), same mechanism as decision 10's
    `IndexStage`.
13. **`document_structure`/`reading_order` additively surface each section's tables/figures — in scope,
    not deferred (flagged as a smaller fork, §6).** Without this, RT-5's primary "browse this document"
    tool would silently keep omitting tables/figures even after they become graph nodes, which would
    leave the audit's `P3-11` ("no table/figure nodes, no cross-refs") only half-closed. Both
    `FakeGraphStore.document_structure` and `Neo4jGraphStore._document_structure_tx` gain a `"tables"`/
    `"figures"` key per section view (same CONTAINS-edge-filter pattern already used for `"paragraphs"`,
    just filtered by `type in {"table","figure"}` instead of `type=="paragraph"`) — **additive keys**,
    every existing assertion on `"paragraphs"`/`"document"`/section fields is unaffected.
14. **`build_provenance` is untouched.** Table/figure graph nodes get no `node_provenance` entry (its
    node-type check only handles `"paragraph"`/`"section"`; a `"table"`/`"figure"` node hits the existing
    `else: continue` and is silently skipped — exactly its documented "omit rather than emit a bad page"
    contract, unchanged). Consequence: `graphquery`'s `_cite` (RT-5) will show `citation.page: None` for a
    table/figure node returned by `neighbors`/`document_structure` until Phase 2 extends
    `build_provenance` to also resolve those two types (their real geometry — Phase 0's
    `bounding_regions` on tables/`ocr["figures"]` — is sitting right there, unused, for Phase 2 to pick
    up). Zero code change to `fdi/pipeline/graphquery.py` needed for Phase 3; this is a documented,
    accepted interim gap, not a bug.

## 3. Architecture

### 3.1 `fdi.core.ast` — `attach_table_refs` (new, pure)

```python
def attach_table_refs(
    ast: Mapping[str, object], unified: Mapping[str, object], ocr: Mapping[str, object],
) -> dict[str, object]:
    """Additively populate each ast section with `table_refs: list[int]` (raw
    ocr["tables"] indices) and `figure_refs: list[{"index": int, "caption": str}]`
    by DFS-flattening ocr["sections"] (root's `elements`, recursing "/sections/N")
    into document order and replaying it against build_ast's own heading-boundary
    rule (unified["blocks"][i]["role"]). A table/figure ref seen before any
    section has opened attaches to the preamble section (created if absent).
    Never raises; malformed elements entries are skipped."""
```
Returns a new `ast` dict — same `title`, same `sections` list length/order/`heading`/`paragraphs`, plus
the two new per-section keys (`[]` default). `AstStage.run`: `ast = build_ast(unified); return
attach_table_refs(ast, unified, ocr)`.

### 3.2 `fdi.core.structure` — `source_indices` on `merge_cross_page_tables`

```python
ordered = sorted(enumerate(tables), key=lambda pair: _span_start(pair[1]))
# cur tracks its own source_indices; _merge_two concatenates a["source_indices"] + b["source_indices"]
# a fresh (unmerged) table seeds source_indices=[original_index]
```
Additive key on every output table dict; `n_rows`/`n_cols`/`grid`/`caption`/etc. all unaffected.
`StructureStage.version: 2 → 3`.

### 3.3 `fdi.core.table_grid` — shared index bridge (new, pure)

```python
def raw_to_structure_index(tables: Sequence[Mapping[str, object]]) -> dict[int, int]:
    """{raw_ocr_table_index: structure_position} from each table's `source_indices`."""
```

### 3.4 `fdi.core.retrieval.build_chunks` — new `structure` param, row/table chunks, dedup

```python
def build_chunks(
    ast: Mapping[str, object], semantic: Mapping[str, object],
    provenance: Mapping[str, object], structure: Mapping[str, object],
) -> dict[str, object]:
```
1. Build section chunks exactly as today (unchanged loop, unchanged ids/shape plus `chunk_type:
   "section"`, `table_index: None`, `row_index: None`).
2. Build `section_for_raw_table: dict[int, {"heading": str|None, "type": str|None}]` by walking `ast`
   sections' `table_refs` (paired with `sem_sections[k]["type"]` at the same `k`, section-loop-local).
3. Via `raw_to_structure_index(structure["tables"])`, build `context_for_structure_idx: dict[int, ...]`.
4. For each `i, table in enumerate(structure["tables"])`: `grid = table["grid"]`; `context =
   context_for_structure_idx.get(i, {}).get("heading") or table.get("caption",{}).get("content") or
   f"Table {i}"`; `page = table["bounding_regions"][0]["page_number"]` if present else `None`;
   `section_type = context_for_structure_idx.get(i, {}).get("type")`. Emit one row chunk per `r in
   range(grid["header_row_count"], grid["n_rows"])` (§2.3 text format) and one whole-table chunk
   (§2.4 text format, capped).
5. Concatenate section chunks + table chunks (row chunks then whole-table chunk per table, tables in
   `structure` order) → dedup pass (§2 decision 9) → return `{"schema_version": 2, "chunks": [...]}`.
   **`schema_version: 1 → 2`** (new optional keys + new chunk kinds — additive but worth signaling to any
   artifact-version-pinned reader).

`ChunksStage.deps: ("ast", "semantic", "provenance") → ("ast", "semantic", "provenance", "structure")`;
`ChunksStage.version: 1 → 2`.

### 3.5 `fdi.pipeline.stages.embedding` — reads `chunks`

```python
class EmbeddingStage:
    name = "embedding"
    version = 2
    deps: tuple[str, ...] = ("chunks",)

    async def run(self, ctx: StageContext) -> object:
        chunks = cast(Mapping[str, object], await ctx.read("chunks"))
        client = cast(EmbeddingClient, ctx.services["embedder"])
        records = [c for c in chunks.get("chunks", []) if isinstance(c, dict)]
        texts = [cast(str, c.get("text", "")) for c in records]
        vectors = client.embed(texts) if texts else []
        out_chunks = [
            {"chunk_id": r["chunk_id"], "heading": r.get("heading"), "vector": v}
            for r, v in zip(records, vectors, strict=True)
        ]
        return {"schema_version": 1, "model": client.model,
                "dim": len(vectors[0]) if vectors else 0, "chunks": out_chunks}
```
`IndexStage` — **no code change** (§2 decision 10). `IndexStage.version` stays 2.

### 3.6 `fdi.core.graph.build_graph` — `structure` param, table/figure nodes

```python
def build_graph(ast: Mapping[str, object], structure: Mapping[str, object]) -> dict[str, object]:
```
Existing document/section/paragraph/CONTAINS/READING_ORDER logic unchanged. After the existing
per-section loop, using the SAME `sid = f"s{i}"`: translate `section["table_refs"]` via
`raw_to_structure_index(structure["tables"])`, emit `table` nodes/edges (§2 decision 11); emit `figure`
nodes/edges directly from `section["figure_refs"]` (no translation). `GraphStage.deps: ("ast",) →
("ast", "structure")`; `GraphStage.version: 1 → 2`.

### 3.7 `fdi.platform.graphstore.Neo4jGraphStore` — label allowlist

```python
_NODE_LABELS: Mapping[str, str] = {
    "document": "Document", "section": "Section", "paragraph": "Paragraph",
    "table": "Table", "figure": "Figure",
}
```
No other change to `_load_tx` — the same validated-allowlist f-string path already handles any type
added to the dict.

### 3.8 `document_structure` — tables/figures per section (both stores)

`FakeGraphStore.document_structure` / `Neo4jGraphStore._document_structure_tx`: alongside the existing
per-section `paragraphs` (CONTAINS-filtered `type=="paragraph"`), add `tables` (CONTAINS-filtered
`type=="table"`) and `figures` (CONTAINS-filtered `type=="figure"`), each `[{"id","type","label"}]`,
unordered (no `READING_ORDER` chain for these types — §2 decision 11). `reading_order`'s per-section view
(`{k: v for k, v in s.items() if k != "paragraphs"}`) needs its exclusion list widened to also drop
`"tables"`/`"figures"` (it already only returns the outline, not the section's members).

## 4. Error handling

- `attach_table_refs`: malformed `ocr["sections"]` entry, dangling `"/sections/N"` ref, out-of-range
  paragraph/table/figure index → skipped, never raises; empty/absent `ocr["sections"]` → every section
  gets `table_refs: [] , figure_refs: []` (safe on a pre-Phase-0 or hierarchy-less document).
- `raw_to_structure_index`: a raw index absent from every `source_indices` (should not occur; defensive)
  → simply missing from the returned dict; callers `.get(...)` and degrade to the table-own-caption/
  `f"Table {i}"` fallback.
- `build_chunks`: a `structure` with no `tables` key → zero table chunks emitted, section chunks
  unaffected (mirrors today's "malformed input degrades per-field" posture). A table with an empty grid
  (`n_rows == header_row_count`) emits zero row chunks and one (mostly-empty, header-only) whole-table
  chunk — not suppressed, matching this codebase's "never silently omit a real artifact entry" stance,
  though it will very likely be caught by dedup if several such tables share identical near-empty text.
- `build_graph`: a `table_refs`/`figure_refs` entry with no match in `raw_to_structure_index` /
  `ocr["figures"]` → skipped (never raises); an AST section with no `table_refs`/`figure_refs` key at all
  (pre-Phase-3 `ast` artifact, cache staleness) → treated as `[]` (`.get(..., [])`).
- `Neo4jGraphStore._load_tx`: an unknown node/edge type still raises `ValueError` — unchanged, now simply
  never triggered by `table`/`figure` since they're in the allowlist.
- `document_structure`: a document with table/figure nodes but no matching `CONTAINS` edge (should not
  happen, defensive) → empty `tables`/`figures` lists for that section, not an error.

## 5. Testing

- **Unit**, `test_ast.py` (extend): a crafted `ocr["sections"]` hierarchy (root + one nested subsection)
  interleaving paragraph/table/figure refs around two heading paragraphs → asserts `table_refs`/
  `figure_refs` land on the correct sections, in the right order, with figure captions attached; a
  table/figure ref before any heading → lands on the preamble section; malformed/dangling refs → skipped,
  no raise; unmodified Phase-1 assertions (title/pollution-filter tests) still pass byte-for-byte since
  `build_ast` itself is untouched.
- **Unit**, `test_structure.py` (extend): `merge_cross_page_tables` on two mergeable tables →
  `source_indices == [0, 1]` on the merged output; on tables that don't merge → each keeps its own
  singleton `source_indices`; order is stable under the existing `sorted(..., key=_span_start)`.
- **Unit** (new), `test_table_grid_index_bridge.py`: `raw_to_structure_index` over a fixture with
  merged + unmerged tables returns the correct raw→structure mapping.
- **Unit**, `test_retrieval_chunks.py` (extend heavily): a fixture `structure` with one 3-row (1 header +
  2 data) table whose `source_indices=[0]` is referenced by `ast` section 0's `table_refs=[0]` →
  asserts two row chunks (`t0r1`, `t0r2`) with the section-heading context prefix, correct `page` from
  `bounding_regions`, correct `section_type` from the paired `semantic` entry, blank-cell omission in the
  text; one whole-table chunk `t0` with capped/joined text; dedup collapses two byte-identical rows
  (from two distinct NIL/blank tables sharing a label+context) into one surviving chunk id, non-dupes
  untouched; a table with no owning section → context falls back to caption, then `f"Table {i}"`;
  `schema_version == 2`; every EXISTING assertion in this file (section-chunk shape, id-gap behavior,
  `None`-degrade rules) still passes with `structure={}` as the new 4th arg.
- **Integration**, `test_chunks_stage.py` (extend): `deps == ("ast","semantic","provenance","structure")`;
  version bump reflected; chain runs green end-to-end with fixture data including a table.
- **Integration**, `test_embedding_stage.py` (rewrite core assertions): `deps == ("chunks",)`; embeds
  `chunks["chunks"][i]["text"]` in order (not a re-derived-from-ast text); `FakeEmbeddingClient`
  call-counting cache-hit test now keys off a stable `chunks` artifact instead of `ast`; a `chunks`
  artifact containing table-row entries produces `embedding.chunks` entries with `t{i}r{j}` ids.
- **Integration** (new or extended), an index/resolve round-trip test: a full `ocr → ... → chunks →
  embedding → index` chain over a fixture with one table, then `resolve_chunks(ids=["{doc}:t0r1"])`
  returns the row's full text/citation — the concrete proof decision 10's "IndexStage/resolve_chunks need
  no code change" claim holds end-to-end, not just by code inspection.
- **Unit**, `test_build_graph.py` (extend): `build_graph(ast, structure)` signature change reflected in
  every existing call site; a section with `table_refs=[0]` + a `structure` whose table 0 has
  `source_indices=[0]` and a caption → asserts a `table` node `t0` with that label and a `CONTAINS` edge
  from the section; a `figure_refs` entry → asserts an `f{k}` node + edge; no `READING_ORDER` edges
  touch table/figure nodes; existing document/section/paragraph assertions unchanged.
- **Unit**, `test_neo4j_graphstore.py` (extend): loading a `type="table"`/`type="figure"` node no longer
  raises; MERGE query text contains `:Table`/`:Figure`.
- **Unit**, `test_graphstore.py` (extend `FakeGraphStore`): `document_structure` fixture with a section
  CONTAINS-linked to a table and a figure node → section view carries non-empty `tables`/`figures` lists;
  `reading_order`'s per-section view still excludes them (outline-only, unchanged intent).
- **Real-doc regression (scratchpad, documented — not committed, same posture as Phase 0/1's real-doc
  checks):** re-run ingestion on the real STTL doc through Phase 3; confirm (a) `chunks` artifact chunk
  count grows by roughly 261 (tables) × (avg data rows + 1) ≈ 2,000–2,500 new `table_row`/`table`
  entries; (b) dedup ratio measured against Phase 1's post-dedup baseline; (c) for each of the ~30
  headline GT numeric fields (Property/Plant/Equipment, Total Revenue from Operations, Profit for the
  Year, etc.), call `hybrid_search` with the field's plain-English label as the query and assert the
  top-k results include at least one hit whose `chunk_id` matches `t\d+r\d+` (i.e. resolved via a
  structured table-row chunk, not a coincidental prose mention) carrying the correct value in its `text`
  — the quantified proof this phase closes P1-1 in full, not just partially; (d) `document_structure`
  over the same doc's graph shows every major statement section (`Balance Sheet`, `Statement of Profit
  and Loss`, `Cash Flow Statement`) with a non-empty `tables` list.
- **Gates:** grep every `schema_version==1` assertion on the `chunks` artifact and every
  `AstStage.version==2`/`StructureStage.version==2`/`ChunksStage.version==1`/`EmbeddingStage.version==1`/
  `GraphStage.version==1` assertion; update. Full suite green; `ruff`/`mypy --strict`/`lint-imports`
  1-kept/0-broken (every new function stays in `core`, pure, no new cross-layer import — `attach_table_refs`
  takes plain `Mapping`s, same as every other `core.ast`/`core.graph` function).

## 6. Explicitly deferred (and the forks a human should decide)

- **FORK — per-row + per-table (recommended) vs. per-row only.** The whole-table chunk adds ~261 chunks
  and index entries; negligible at this corpus's scale but a real, if small, ongoing embedding-cost and
  index-size line item across a larger corpus. Recommend keeping both; a human with cost/index-size
  authority may prefer per-row only and rely on `get_tables`/`concept_lookup` for whole-table context.
- **FORK — `EmbeddingStage.deps: ast → chunks` (recommended).** This is architecturally the right move
  (single source of chunk truth, table chunks embedded "for free," zero `IndexStage`/`resolve_chunks`
  change — all verified above) but it is **not free at runtime**: every previously-embedded document will
  re-embed in full on next `run_stage` (content-addressing forces a real, non-cached re-run whenever a
  stage's `deps` tuple or `.version` changes), which is a genuine Azure OpenAI/BGE API cost + latency
  hit across the whole corpus, unlike Phase 0/1's "free" AST-only re-derivations. A human with
  cost/budget authority should sign off on this before Phase 3 executes against a real corpus, or the
  re-embed should be scheduled deliberately rather than triggered incidentally.
- **FORK — Azure-hierarchy lockstep replay (recommended, §2 decision 5) vs. a simpler page/offset
  nearest-section heuristic.** The recommended design is exact when its documented assumption holds
  (every section-opening heading appears explicitly in Azure's `sections[].elements`) but is genuinely
  more code and a real, if well-reasoned, assumption. A simpler alternative — attach each table/figure to
  the AST section whose (independently, provenance-style, text-matched) page is the closest preceding
  page — is more robust to a hierarchy that doesn't enumerate every heading, at the cost of being an
  approximation rather than Azure's authoritative structure, and would duplicate `build_provenance`'s
  heading-match logic in a second place. Flagged for explicit sign-off given the complexity/robustness
  trade-off is genuinely close.
- **FORK (smaller) — `document_structure`/`reading_order` surfacing tables/figures (recommended, §2
  decision 13) vs. leaving them reachable only via `neighbors`.** Recommended for completeness (closes
  P3-11 in full), but is itself additional `GraphStore`-protocol surface a human could choose to defer to
  a later RT-9-shaped slice if Phase 3's blast radius should stay smaller.
- **NOT in scope:** projecting the two new `chunks` fields (`chunk_type`, `table_index`/`row_index`) into
  the Postgres `Chunk`/FTS table (§2 decision 8) — no `Chunk` model migration this phase; a keyword search
  cannot yet filter "table rows only," only the vector/graph paths can via the `chunks`/`graph` artifacts
  directly. **NOT in scope:** extending `build_provenance` to resolve `table`/`figure` node types (§2
  decision 14) — left for Phase 2 (or a dedicated follow-up) to pick up using the same
  `bounding_regions` Phase 0 already retained. **NOT in scope:** `READING_ORDER` edges or chain membership
  for table/figure nodes. **NOT in scope:** re-deriving figure *geometry* into the graph node (only
  `id`/`type`/`label` — figure `bounding_regions` stay in `ocr`, unused by the graph layer, same as
  today; Phase 2's `#8` row is the natural owner of figure geometry work).

## 7. Done criteria

1. `attach_table_refs` (pure) additively populates `table_refs`/`figure_refs` on every AST section from
   Azure's `sections[].elements` hierarchy; `AstStage` (`v3`, `deps=("normalize","ocr")`) calls it after
   `build_ast`; `build_ast` itself is unmodified (Phase 1's tests pass unchanged).
2. `merge_cross_page_tables` additively emits `source_indices`; `raw_to_structure_index` (pure, shared)
   bridges raw↔structure table indexing for both `build_chunks` and `build_graph`; `StructureStage v3`.
3. `build_chunks(ast, semantic, provenance, structure)` emits per-row + whole-table chunks (§2 decisions
   1-4, 7-8), globally exact-text-deduplicates the combined list (decision 9) without renumbering
   survivors, `schema_version 2`; `ChunksStage v2`, `deps` gains `"structure"`.
4. `EmbeddingStage` (`v2`, `deps=("chunks",)`) embeds `chunks["chunks"][].text` directly; verified (not
   assumed) that `IndexStage` and `fdi.pipeline.retrieval.resolve_chunk(s)` need zero code change and
   correctly carry table-row chunks through indexing and resolution end-to-end (integration test, §5).
5. `build_graph(ast, structure)` emits `table`/`figure` nodes with `CONTAINS` edges from their owning
   section (id scheme `t{structureIndex}`/`f{rawIndex}`, aligned with `get_tables`' `table_index` for
   tables); `GraphStage v2`, `deps` gains `"structure"`. `Neo4jGraphStore._NODE_LABELS` extended with
   `table`/`figure` (mandatory — real-store load would otherwise raise). `document_structure`/
   `reading_order` (both stores) additively surface each section's `tables`/`figures`.
6. `build_provenance` intentionally untouched (documented gap, owned by Phase 2); `GraphLoadStage`/
   `IndexStage` need no code change and re-run automatically via content-addressed cascade.
7. Unit + integration tests cover every new function/shape change and every degrade path (§5); all stale
   `schema_version`/`.version` assertions updated; all four gates green; `core` stays pure (every new/
   changed function takes plain `Mapping`s, no cross-layer import).
8. Real-doc regression (scratchpad, §5) shows: table/row chunks present in `chunks` at the expected
   order of magnitude, dedup working, and — the audit's actual target — the ~30 headline GT numeric
   fields resolvable via `hybrid_search` through structured `t{i}r{j}` chunks, not only through
   `get_tables`. This is the quantified proof P1-1 is closed **in full** (Phase 1 closed it partially).
