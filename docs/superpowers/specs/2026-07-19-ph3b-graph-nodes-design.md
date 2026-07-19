# Phase 3b — Table & figure graph nodes + section membership

- **Status:** Design — 2026-07-19. Phase 3b of the ingestion improvement plan
  (`docs/architecture/2026-07-19-ingestion-audit-and-improvement-plan.md`), closing the graph half of
  audit finding P3-11 ("no table/figure/footnote nodes... no cross-reference edges"). Depends on
  **Phase 2** (merged: `ast` sections carry `heading_source_index`/`paragraph_source_indices`; `structure`
  v3 tables carry per-cell geometry) and **Phase 3a** (merged, `a67e538`: table-derived row/whole-table
  *chunks*, `ChunksStage v2`). Sibling of Phase 2's provenance work, not a dependent of it (mirrors Phase
  3's own §2 decision 6 stance).
- **This is the deferred half of `docs/superpowers/specs/2026-07-19-ph3-table-chunks-design.md`.** That
  spec designed both the chunk grain (§3.1-3.5, shipped in Phase 3a) and the graph grain (§3.6-3.8, §2
  decisions 5, 11-13 — **not** shipped). Its own code now says so explicitly:
  `fdi/pipeline/stages/chunks.py` docstring — *"table<->AST-section linking (`table_refs`) and the
  raw<->structure table-index bridge are deferred to Phase 3b"* — and `fdi/core/retrieval.py`'s
  `_table_context` docstring — *"Phase 3a does not resolve the owning AST section's heading -- that
  requires `ast.table_refs`, deferred to Phase 3b"*. This spec reuses Phase 3's design where it still
  fits the shipped code, and refines it in three places where the shipped Phase 2/3a code makes the
  original design partly stale (§2 decisions 1, 3, 5 below) or partly aspirational (Phase 3's
  `source_indices`/`raw_to_structure_index` were never added to `structure.py` — §2 decision 3).
- **Verified against the current tree (not assumed):** `fdi/core/graph.py::build_graph(ast)` takes one
  arg today (document/section/paragraph nodes, CONTAINS/READING_ORDER edges only, `schema_version: 2`).
  `fdi/core/ast.py::build_ast` emits no `table_refs`/`figure_refs` key anywhere. `fdi/core/retrieval.py`'s
  shipped `_table_chunks` already proves the `t{i}` id scheme (`i` = position in `structure["tables"]`,
  **not** a bridged raw index) is live in the `chunks` artifact today — this spec's table node id must
  match it exactly, or a future citation-joining feature would see two different numbers for "the same"
  table. `fdi/core/structure.py::merge_cross_page_tables` has **no** `source_indices` key on its output
  today (Phase 3's §2 decision 6/§3.2 was never implemented) and this phase's scope excludes
  `structure.py`, so §2 decision 3 designs around that constraint rather than assuming it away.

## 1. Purpose

Close P3-11 in full: make tables and figures first-class, browsable **graph** nodes (not just retrieval
chunks), reachable from their owning section via `CONTAINS`, and safe to `load` into a real Neo4j
instance. Three deliverables, all additive to existing shapes:

1. **`table_refs`/`figure_refs` on AST sections** (`fdi.core.ast.attach_table_refs`, new pure function) —
   membership of each table/figure under its owning AST section, derived from Azure's authoritative
   `sections[].elements` hierarchy (already retained by Phase 0's `normalize_ocr`, currently unused).
2. **`table`/`figure` graph nodes** (`fdi.core.graph.build_graph(ast, structure)`, gains a second param) —
   one node + one `CONTAINS` edge from the owning section, per table/figure with membership. Zero change
   to existing document/section/paragraph nodes or CONTAINS/READING_ORDER edges.
3. **Neo4j label allowlist extension** (`fdi.platform.graphstore.Neo4jGraphStore._NODE_LABELS`) — mandatory,
   not optional: today's closed allowlist raises `ValueError` on `type="table"`/`"figure"`, so the very
   first `graph_load` run against a Phase-3b `graph` artifact would hard-fail against a real store without
   this. Plus `document_structure`/`reading_order` (both `FakeGraphStore` and `Neo4jGraphStore`)
   additively surfacing each section's tables/figures, so RT-5's primary browse tool doesn't silently keep
   omitting them even after they exist as nodes.

**Explicitly out of scope** (per the task's scope boundary, and consistent with Phase 3's own deferrals):
`structure.py`, `ontology.py`, `quality.py`, `retrieval.py`, `chunks.py`/`embedding.py` stages. This
spec does not change table/figure *chunk* text or ids (Phase 3a's, already shipped) and does not add
`table_refs`-aware context to `_table_context` — that would be a `retrieval.py` change, explicitly out of
scope here, and is noted as a small follow-up in §6.

## 2. Scope decisions (autonomous; documented) — read this section first

1. **Membership mechanism: Azure-hierarchy lockstep (recommended), evaluated against a span/page
   heuristic — same fork Phase 3 flagged in its §6, resolved here with a concrete refinement Phase 3
   couldn't make.** Two options were evaluated:
   - **(a) Hierarchy lockstep** — DFS-flatten `ocr["sections"]` (Azure's authoritative nested tree,
     retained since Phase 0) into document order, and use each `("paragraphs", N)` ref that matches a
     known AST section's `heading_source_index` as a "now inside section K" marker; every `("tables", M)`/
     `("figures", K)` ref between two such markers belongs to the currently-open section.
   - **(b) Span/page heuristic** — independently locate each table/figure's page (`bounding_regions[0]
     .page_number`, already used by `_table_page` in `retrieval.py`) and attach it to whichever AST
     section's own resolved page is the closest preceding one — the kind of provenance-style text/page
     matching `build_provenance` already does for paragraphs.

   **(a) is recommended and adopted**, for one reason more decisive than in Phase 3's original writeup:
   Azure's `sections[].elements` is not just "structure we could use" — it is the **one Azure artifact
   that states table/figure/paragraph ordering as ground truth**, whereas (b) would *approximate* that same
   ordering via page numbers, which are coarse (many tables share a page with several sections) and would
   require **duplicating** `build_provenance`'s heading-resolution logic in a second place — exactly the
   kind of "global first-match on ambiguous labels" bug class Phase 2 (`P1-2`) was written to eliminate.
   Using Azure's own hierarchy is the same "consume structure Azure already emits" principle that drove
   Phases 0-3a; a page-nearest heuristic is a step backward into the audit's `P1-2` failure mode, just
   applied to tables instead of paragraphs. **Documented assumption** (unchanged from Phase 3, still not
   proven for every possible document): every AST section-opening heading paragraph appears as an explicit
   `"/paragraphs/N"` ref somewhere in Azure's flattened `sections[].elements` sequence. On the audited STTL
   doc this holds directionally (audit §3 row 1: Azure's own hierarchy resolves to 608 section-nestings;
   the de-polluted, post-Phase-1 AST has 607 sections — the two counts were measured at different points
   in the plan and are not a proven 1:1 correspondence, which is exactly why this stays a documented
   assumption rather than a verified invariant). If it is ever violated for a document, the defensive fallback (decision
   5 below) means the effect is "a table/figure attaches to the wrong section it's still in, or to the
   preamble," never a crash or a dropped table/figure — this codebase's standing "degrade a field, never
   drop an artifact" posture.
2. **Node id scheme: `t{structureIndex}` for tables (same convention as the ALREADY-SHIPPED `chunks`
   artifact's `t{i}`/`t{i}r{j}` ids — Phase 3a's `_table_chunks` uses `i = enumerate(structure["tables"])`
   directly), `f{rawIndex}` for figures (figures are never reordered/merged anywhere in this pipeline —
   `structure.py` only stitches tables).** After `GraphLoadStage`'s existing, unmodified namespacing
   (`_namespace_node`), a table node's real id is `{doc_id}:t{i}` — **byte-identical** to a Milvus/chunks
   hit id for the same table (`{doc_id}:t{i}`, from `chunk_rows_for_index`'s `id: f"{doc_id}:{chunk_id}"`)
   and to `get_tables`' `table_index` (`fdi/pipeline/tables.py`). Zero-translation cross-referencing between
   a graph node, a chunk hit, and a structured-table lookup, exactly as Phase 3's §2 decision 2 intended —
   this decision makes that intent real by using the SAME index `_table_chunks` already uses, rather than
   Phase 3's original (never-shipped) `source_indices`-bridged index (see decision 3).
3. **Raw ↔ structure table-index bridge lives in `core/graph.py` as an OPTIONAL-key reader, not as a new
   `structure.py` field — a genuine refinement of Phase 3's §2 decision 6/§3.2, forced by two facts
   verified in the current tree, not assumed.** Fact one: `merge_cross_page_tables` emits **no**
   `source_indices` key today — Phase 3's design for it was written but never implemented, and this
   phase's scope explicitly excludes `structure.py`. Fact two: Phase 3a's shipped `_table_chunks` already
   committed to "`t{i}` = position in `structure["tables"]`" **without** any raw-index bridge at all — so
   for internal consistency with the artifact that's already in production, the graph's `t{i}` must use
   the exact same position, not a hypothetical bridged one.
   `attach_table_refs`' `table_refs` are necessarily **raw** `ocr["tables"]` indices (that's the only
   index space Azure's hierarchy refs and `ocr["tables"]` share; `structure` doesn't exist yet at
   `ast`-build time in the stage DAG). `build_graph` therefore needs a raw→structure map. Design: a
   private helper in `core/graph.py`,
   ```python
   def _raw_to_structure_index(tables: Sequence[Mapping[str, object]]) -> dict[int, int]:
       """{raw_ocr_table_index: structure_position}. Reads an OPTIONAL `source_indices`
       list per table (future-proofing for if/when a later phase adds real stitching +
       emits it); a table with no such key defaults to `[i]` (identity) -- true for
       100% of tables today (audited 0/261 merges). Malformed entries skipped; a raw
       index appearing in two tables' `source_indices` is last-write-wins (defensive;
       `merge_cross_page_tables` partitions its input so this should not occur)."""
   ```
   **Today this is provably an identity map** (0/261 merges, audited) **combined with one caveat this
   spec is explicit about and Phase 3's original writeup was not**: `merge_cross_page_tables` calls
   `sorted(tables, key=_span_start)` — it re-orders by span position regardless of `ocr["tables"]`'s
   original order, so identity holds only if Azure *also* already emits `tables[]` in span-ascending
   order (the typical/observed case, not independently re-verified beyond the audit's 0-merge finding).
   Reading an **optional** key rather than hard-coding identity means: (a) zero risk from this
   never-fully-proven ordering assumption today (the fallback IS the assumption, made explicit rather than
   implicit), and (b) if a future Phase 5 fixes cross-page stitching and starts adding a real
   `source_indices` field to `structure.py` (still out of scope here), `build_graph` picks it up
   automatically with **zero further code change to `core/graph.py`** — the exact forward-compatibility
   Phase 3's original bridge was going for, achieved without touching a file outside this phase's scope.
4. **`attach_table_refs(ast, ocr) -> dict` — a two-argument signature, simpler than Phase 3's original
   three-argument (`ast, unified, ocr`) design.** Phase 3's design replayed `build_ast`'s own
   role-based heading rule against `unified["blocks"]` to reconstruct "which section is currently open."
   That was necessary in Phase 3's original context but is now redundant: Phase 2 (merged since) already
   threads `heading_source_index` onto every AST section — the **exact** `ocr["paragraphs"]` index that
   opened it, already computed, already reliable (Phase 2 measured "0 wrong-page collapses across 354
   ambiguous groups" on this exact mechanism). Section-boundary tracking becomes a direct **index-equality
   match** against `heading_source_index` (built once as a `dict[int, int]`, `raw_paragraph_index ->
   ast_section_index`) instead of re-deriving section boundaries from role text a second time — fewer
   moving parts, no dependency on `unified["blocks"]`'s role vocabulary staying in sync with `build_ast`'s
   rule, and `AstStage.deps` only needs to add `"ocr"` (not keep `"normalize"` for a second purpose it no
   longer needs beyond building the `ast` in the first place).
5. **Defensive fallback: identical to Phase 3's original, plus one addition (cycle protection) Phase 3's
   docstring didn't mention but this codebase's "never raise" posture requires.** A table/figure ref
   encountered before any section has opened (i.e., before the first `heading_source_index` match) attaches
   to the preamble section (`ast["sections"][0]` if its `heading is None`) or, if no preamble section
   exists at all (document opens directly on a heading), a synthetic empty preamble section is **created**
   and inserted at index 0 — mirroring `build_ast`'s own preamble-creation behavior exactly, with every
   later `ast_section_index` in the (rebuilt) `heading_source_index` map shifted +1 accordingly. **New**:
   the DFS walk over `ocr["sections"]` (following `"/sections/N"` refs) keeps a `visited: set[int]` of
   section indices and refuses to re-enter one already on the current path — a malformed/cyclic Azure
   artifact (should not occur; defensive) degrades to "that branch stops early," never an infinite
   recursion. Malformed elements entries (non-string, wrong prefix, out-of-range paragraph/table/figure/
   section index) are skipped; empty/absent `ocr["sections"]` → every AST section gets `table_refs: []`,
   `figure_refs: []` (safe on a pre-Phase-0 or hierarchy-less document — same degrade Phase 3 specified).
6. **`figure_refs` shape: `list[{"index": int, "caption": str}]`, caption captured at attach-time — not a
   bare int list like `table_refs`.** Reusing Phase 3's original shape unchanged: it lets `build_graph`
   read a figure's label straight from `ast` without adding `"ocr"` to `GraphStage.deps` (which stays
   `("ast", "structure")` — `structure` never touches figures, only `ast` carries the caption already).
   `ocr["figures"][k]["caption"]` is a **plain string** in the current schema (verified in
   `fdi/core/ocr_schema.py`: `figures_data.append({"caption": caption_content, ...})`) — unlike a table's
   caption, which is a nested `{content, bounding_regions}` dict. `attach_table_refs` reads it directly,
   no unwrapping needed; an out-of-range figure index degrades to skipping that ref (never a raise).
7. **`table_refs` land raw indices only (`list[int]`) — no bridging inside `attach_table_refs` itself.**
   The raw→structure translation is `build_graph`'s job (decision 3), because `structure` doesn't exist
   at `ast`-build time. This keeps `attach_table_refs` a function of exactly `(ast, ocr)`, with no implicit
   dependency on `structure`'s stitching behavior leaking into the AST layer.
8. **`build_graph(ast, structure)` — the second table/figure pass is a SEPARATE loop appended after the
   existing document/section/paragraph loop, not interleaved into it.** The existing loop body (lines
   41-96 of today's `core/graph.py`) is left **byte-for-byte unchanged**; a new loop over `sections` runs
   afterward, reusing the same `sid = f"s{i}"` convention. This is a stronger, more literal reading of
   "existing nodes/edges unchanged" than editing the existing loop in place would be — a diff reviewer
   can see the old logic untouched and the new logic additive, and it matches this codebase's established
   pattern of keeping pure `core` functions reviewable as a sequence of independent passes (e.g.
   `_dedup_by_text` as its own pass in `build_chunks`).
9. **Node label: table caption else `f"Table {i}"`; figure caption else `f"Figure {k}"` — deliberately the
   SAME two-tier fallback `retrieval.py`'s already-shipped `_table_context` uses for table chunks, not a
   three-tier fallback.** The task prompt's "caption/first-row or 'Table i'" phrasing was evaluated
   against introducing a first-row/`f"Table ({n_rows}x{n_cols})"` third tier (Phase 3's original decision
   11 had proposed the latter); both are **rejected** for one concrete reason: the SAME real-world table
   already has an established, shipped label convention in `chunks` (`_table_context`), and a human
   browsing `graph`'s `document_structure` for table `t3` should see the same label they'd see citing
   chunk `t3` — introducing a second, different fallback convention for "no caption" tables buys nothing
   and costs consistency. A bare header row ("Particulars") is also not more informative than "Table 3"
   and re-introduces a smaller version of the ambiguous-bare-fragment problem Phase 1 removed from the
   AST. This is a **resolved** decision, not a fork, but is called out because the task prompt explicitly
   suggested the alternative.
10. **Table/figure nodes carry `source_index: None`, not a repurposed index.** Every existing node type's
    `source_index` means "position in `ocr["paragraphs"]`" (Phase 2's contract). Tables/figures are never
    in `unified["blocks"]` and have no such position — reusing the field for a *different* index space
    (e.g. the raw table index) would silently break that contract for any consumer that assumes
    `source_index` means paragraph position. `None` is the correct, honest degrade, consistent with how
    `_as_source_index` already treats "not a real int" — and note `GraphLoadStage`'s `_namespace_node`
    already drops `source_index` before it ever reaches a store, so this is purely an in-artifact
    consistency choice, not a citation-affecting one.
11. **First-section-wins dedup for a table/figure referenced (in principle) from more than one section.**
    Should not occur under current stitching semantics (a `source_indices`-merged table's raw refs would
    all resolve to the same `structure` position, and today there's no merging at all — decision 3), but
    is defended against: `build_graph` tracks `emitted_tables: set[int]`/`emitted_figures: set[int]` (by
    structure/raw index) and skips a repeat encounter — no duplicate node in `graph["nodes"]`, no duplicate
    `CONTAINS` edge. This matters for **test/artifact determinism** (exact-list assertions,
    `FakeGraphStore.loaded_nodes` counts), even though Neo4j's `MERGE` would silently reconcile duplicates
    at the store layer regardless.
12. **Neo4j label allowlist MUST be extended — mandatory, not optional (same framing as Phase 3's original
    §2 decision 12, still true).** `Neo4jGraphStore._load_tx` maps `node["type"]` through the closed
    `_NODE_LABELS` dict and **raises `ValueError`** on an unknown type (`_cypher_label`, by design — a
    fail-loud Cypher-injection guard, not a fallback). Extend it with `"table": "Table", "figure":
    "Figure"`. `_EDGE_REL_TYPES` needs **no** change (only `CONTAINS` is used for table/figure edges — no
    new edge type). `FakeGraphStore.load`/`.neighbors` need **zero** code change (already fully generic
    over any `{id, type, label}` dict, verified by reading the current file — no allowlist there at all).
13. **`document_structure`/`reading_order` additively surface each section's tables/figures — implemented
    now (in scope: `graphstore.py`, explicitly a spec-read file), not deferred as a fork.** Phase 3's
    original write-up flagged this as a "smaller fork" a human might defer; this spec resolves it: without
    it, RT-5's primary "browse this document" tool keeps silently omitting tables/figures even after they
    exist as nodes, leaving P3-11 only half-closed exactly as the audit found it — and it is genuinely
    small (one more CONTAINS-filtered query per store, same shape as the existing `paragraphs` one).
    **`FakeGraphStore.document_structure`**: alongside the existing per-section `paragraphs` (filtered
    `type=="paragraph"`), add `tables` (filtered `type=="table"`) and `figures` (filtered `type=="figure"`)
    — each a **numeric-suffix-sorted** `[{"id","type","label","doc_id"}]` list (sort key: the integer
    suffix of the bare id after stripping `{doc_id}:` and its one-letter type prefix — e.g. `"d1:t10"` →
    `10` — **not** a lexicographic string sort, which would put `"d1:t10"` before `"d1:t2"`, the exact bug
    class `order_reading_chain`'s own docstring already warns about for section ids). There is no
    `READING_ORDER` chain for tables/figures (decision 14), so this sort is the only ordering guarantee —
    stated explicitly since "unordered" would make the artifact non-deterministic and untestable.
    **`Neo4jGraphStore._document_structure_tx`**: two more Cypher queries, same shape as the existing
    `paragraphs` query (`MATCH (s:Section {doc_id: $doc_id})-[:CONTAINS]->(t:Table {doc_id: $doc_id})
    RETURN t.id, t.label, s.id AS section_id`, and the `Figure` equivalent), grouped by section the same
    way, same Python-side numeric-suffix sort as the Fake store (so both stores are behaviorally
    identical — the standing contract `FakeGraphStore` exists to test against). **`reading_order`**:
    `Neo4jGraphStore.reading_order`/`_sections_in_reading_order` need **no** change — it was already built
    from its own lean, outline-only query (`RETURN s.id, s.label`) that never included paragraphs, so it
    never needed a "tables"/"figures" exclusion in the first place. `FakeGraphStore.reading_order` DOES
    need its per-section key-exclusion list widened: `{k: v for k, v in s.items() if k not in
    {"paragraphs", "tables", "figures"}}` (was `if k != "paragraphs"`) — since it derives from
    `document_structure`'s now-larger section dicts, without the widen it would leak `tables`/`figures`
    into what's meant to stay an outline-only view. This is the one genuine asymmetry between the two
    store implementations this decision introduces, and it's called out precisely so it isn't missed in
    review.
14. **`fdi/pipeline/graphquery.py` (RT-5's citation wrapper over `GraphStore`) needs no code change —
    verified, not assumed, and explicitly staying untouched (not in this phase's file scope).** Read in
    full: `document_structure`'s per-section dict is built as `{**_cite(s, doc, prov), "paragraphs": [...]}`
    — the `**_cite(s, ...)` spread already carries through **any** extra keys `s` has, including the new
    `tables`/`figures` lists, with zero code change. The one real, accepted gap: `_cite` is only called on
    the section/paragraph/document nodes, not on the entries **inside** a section's `tables`/`figures`
    lists — so those individual table/figure node dicts pass through **without** a `citation` field (no
    page/doc_key/version). This mirrors the exact posture Phase 3's original decision 14 already
    accepted for `build_provenance` not resolving table/figure node types (`"citation.page: None"` until a
    future phase extends `build_provenance`) — same shape of documented, accepted interim gap, not a new
    one this phase introduces. Noted here (not silently discovered later) because it's directly relevant
    to whether decision 13 "closes P3-11 in full" — it closes the **graph structure** half; per-table/
    figure **citation** richness stays Phase 2's (or a dedicated follow-up's) job, as originally scoped.
15. **`build_provenance` stays untouched — same as Phase 3's original decision 14, restated because it's
    still true and still relevant.** No new work here; table/figure geometry (`bounding_regions`) already
    sits in `structure`/`ocr`, unused by `provenance`, for Phase 2 (or a follow-up) to pick up.
16. **`GraphLoadStage` stays version 2, no code change — re-runs automatically via the content-addressed
    cascade, same established pattern Phase 3's decision 12 used for `IndexStage`.** Its namespacing
    (`_namespace_node`/`_namespace_edge`) is already generic over any `{id, type, label}` dict — verified
    by reading the current file, it does not special-case node/edge `type` at all. `GraphLoadStage.deps =
    ("graph",)` is unchanged, so it re-runs (real Neo4j `MERGE`, not a cache hit) automatically the moment
    `graph`'s output_hash changes from this phase's work — no manual version bump needed for a stage whose
    own code is untouched.
17. **A real, if small, side effect worth flagging: bumping `AstStage` to v4 forces one harmless
    `ChunksStage` recompute per previously-ingested document, even though `ChunksStage`'s own code and
    version (`v2`) are untouched by this phase.** `ChunksStage.deps` includes `"ast"`; the moment
    `AstStage`'s output_hash changes (because every `ast` artifact now carries the new `table_refs`/
    `figure_refs` keys), `ChunksStage`'s `input_hash` changes too, so it re-runs rather than cache-hitting
    — and, transitively, so does `EmbeddingStage` (real re-embed cost, the same class of side effect
    Phase 3's decision 10 fork already flagged for its own `ast → chunks` dep change). The recompute is
    functionally a no-op: `build_chunks` does not read `table_refs`/`figure_refs` today (§6 follow-up), so
    `chunks`'s content stays byte-identical — but it is a REAL re-run, not a cache hit, and for
    `EmbeddingStage` specifically a real (if one-time) embedding-API cost across the whole corpus. Called
    out here so it isn't a surprise mid-rollout, mirroring how Phase 0 documented its own downstream
    "free" AST-only re-derivations.

## 3. Architecture

### 3.1 `fdi.core.ast` — `attach_table_refs` (new, pure)

```python
def attach_table_refs(ast: Mapping[str, object], ocr: Mapping[str, object]) -> dict[str, object]:
    """Additively populate every ast section with `table_refs: list[int]` (raw
    ocr["tables"] indices) and `figure_refs: list[{"index": int, "caption": str}]`,
    derived from Azure's authoritative `ocr["sections"]` elements hierarchy (root
    section's `elements`, recursing "/sections/N" refs, cycle-guarded).

    Section-boundary tracking is index equality against each existing AST
    section's `heading_source_index` (Phase 2) -- NOT a re-derivation of
    build_ast's role rule. A table/figure ref seen before any section has
    opened attaches to the preamble section (created at index 0 if absent,
    mirroring build_ast's own preamble handling). Never raises; malformed
    elements entries, dangling/cyclic "/sections/N" refs, and out-of-range
    paragraph/table/figure indices are skipped.
    """
```
Sketch:
```python
def _heading_index_map(sections: list[Mapping[str, object]]) -> dict[int, int]:
    out: dict[int, int] = {}
    for i, s in enumerate(sections):
        hsi = s.get("heading_source_index")
        if isinstance(hsi, int) and not isinstance(hsi, bool):
            out[hsi] = i
    return out

def _flatten(ocr_sections: list[Mapping[str, object]]) -> list[tuple[str, int]]:
    """DFS pre-order over ocr_sections[0]'s elements, following "/sections/N"
    refs recursively; visited-set cycle guard. Returns [(kind, index), ...]
    for "paragraphs"/"tables"/"figures" refs only ("/sections/N" is walked,
    not itself yielded)."""
```
`attach_table_refs` walks `_flatten(...)`, updates `current` on a `("paragraphs", n)` hit against
`_heading_index_map`, and appends to `sections[current]["table_refs"]`/`["figure_refs"]` on a
`("tables", m)`/`("figures", k)` hit (creating the preamble section per decision 5 if `current is None`).
Every section gets `table_refs: []`, `figure_refs: []` as a default first. Returns a new `ast` dict — same
`title`/`sections` length/order/existing keys, plus the two new per-section keys.

`AstStage.run`: `ast = build_ast(unified); return attach_table_refs(ast, ocr)`.
`AstStage.version: 3 → 4`; `AstStage.deps: ("normalize",) → ("normalize", "ocr")`.

### 3.2 `fdi.core.graph` — `build_graph(ast, structure)`, table/figure nodes

```python
def build_graph(ast: Mapping[str, object], structure: Mapping[str, object]) -> dict[str, object]:
```
Existing document/section/paragraph/CONTAINS/READING_ORDER logic (today's lines 41-96) **unchanged,
byte-for-byte**. A new pass appended after it:
```python
def _raw_to_structure_index(tables: Sequence[Mapping[str, object]]) -> dict[int, int]:
    """{raw_ocr_table_index: structure_position}; reads an optional per-table
    `source_indices` list, defaulting to `[i]` (identity) when absent -- true
    for every table today (0/261 stitched merges, audited)."""

def _table_label(table: Mapping[str, object], i: int) -> str:
    """table["caption"]["content"] if non-blank, else f"Table {i}" -- SAME
    fallback retrieval.py's _table_context already uses for table chunks."""

tables = [t for t in structure.get("tables", []) if isinstance(t, Mapping)]
bridge = _raw_to_structure_index(tables)
emitted_tables: set[int] = set()
emitted_figures: set[int] = set()

for i, section in enumerate(sections):          # sections: same list built above, unchanged
    sid = f"s{i}"
    for raw_idx in _int_list(section.get("table_refs")):
        t_idx = bridge.get(raw_idx)
        if t_idx is None or t_idx >= len(tables) or t_idx in emitted_tables:
            continue
        emitted_tables.add(t_idx)
        tid = f"t{t_idx}"
        nodes.append({"id": tid, "type": "table",
                       "label": _table_label(tables[t_idx], t_idx), "source_index": None})
        edges.append({"source": sid, "target": tid, "type": "CONTAINS"})
    for fig in _figure_ref_list(section.get("figure_refs")):
        k = fig.get("index")
        if not isinstance(k, int) or isinstance(k, bool) or k in emitted_figures:
            continue
        emitted_figures.add(k)
        caption = fig.get("caption")
        label = caption if isinstance(caption, str) and caption.strip() else f"Figure {k}"
        fid = f"f{k}"
        nodes.append({"id": fid, "type": "figure", "label": label, "source_index": None})
        edges.append({"source": sid, "target": fid, "type": "CONTAINS"})

return {"schema_version": 3, "nodes": nodes, "edges": edges}
```
`graph.schema_version: 2 → 3` (new node/edge kinds — additive but worth signaling to a version-pinned
reader, same rationale Phase 3 used for `chunks.schema_version`). **No `READING_ORDER` edges** for
table/figure nodes (decision 8's separate-pass design makes this trivially true — the new pass never
touches `prev_section_id`/`prev_para_id`). `order_reading_chain` is untouched (it already ignores any
edge whose `type != "READING_ORDER"` and any node not passed to it — ready for table/figure nodes with
zero change, verified by reading its current defensive filtering).

`GraphStage.run`: `ast = await ctx.read("ast"); structure = await ctx.read("structure"); return
build_graph(ast, structure)`. `GraphStage.version: 2 → 3`; `GraphStage.deps: ("ast",) → ("ast",
"structure")`. Per `fdi/pipeline/run.py`'s `input_hash = derived_input_hash([facts[d].output_hash for d
in stage.deps])`, the new `graph` artifact's input hash is `derived_input_hash([ast.output_hash,
structure.output_hash])`, in that declared order.

### 3.3 `fdi.platform.graphstore.Neo4jGraphStore` — label allowlist + document_structure

```python
_NODE_LABELS: Mapping[str, str] = {
    "document": "Document", "section": "Section", "paragraph": "Paragraph",
    "table": "Table", "figure": "Figure",
}
```
No other change to `_load_tx`/`_cypher_label` — the existing validated-allowlist f-string path already
handles any type added to the dict; the injection-safety contract (`_cypher_label` never lets an
unvalidated `node_type` reach an f-string) is unaffected.

`_document_structure_tx`: two additive queries alongside the existing `paragraphs` one —
```cypher
MATCH (s:Section {doc_id: $doc_id})-[:CONTAINS]->(t:Table {doc_id: $doc_id})
RETURN t.id AS id, t.label AS label, s.id AS section_id
```
(and the `Figure` equivalent), grouped `by_section` the same way as `paragraphs` today, each list
numeric-suffix-sorted (decision 13) rather than passed through `order_reading_chain` (no READING_ORDER
edges for these types). Sections gain `"tables"`/`"figures"` keys alongside the existing `"paragraphs"`.
`reading_order`/`_sections_in_reading_order` — **unchanged** (decision 13: never included paragraphs
either).

### 3.4 `fdi.pipeline.graphstore.FakeGraphStore` — document_structure + reading_order

```python
def document_structure(self, doc_id: str) -> dict[str, object]:
    ...  # existing document/sections/paragraphs logic UNCHANGED
    for s in sections:
        paras = [...]                                    # unchanged
        tables = _members(scoped, contains, s["id"], "table")     # new
        figures = _members(scoped, contains, s["id"], "figure")   # new
        section_views.append({
            **s, "paragraphs": order_reading_chain(paras, para_ro),
            "tables": _numeric_sort(tables), "figures": _numeric_sort(figures),
        })
    ...

def reading_order(self, doc_id: str) -> list[dict[str, object]]:
    sections = self.document_structure(doc_id)["sections"]
    return [{k: v for k, v in s.items() if k not in {"paragraphs", "tables", "figures"}}
            for s in sections]
```
`_members`/`_numeric_sort` are small private helpers: `_members` = same CONTAINS-filter pattern already
used for `paragraphs`, parameterized by `type`; `_numeric_sort` sorts by the integer suffix of each bare
id after stripping `{doc_id}:` and its one-letter prefix (decision 13).

## 4. Error handling

- `attach_table_refs`: malformed `ocr["sections"]` entry, dangling/cyclic `"/sections/N"` ref (cycle
  guard), out-of-range paragraph/table/figure index → skipped, never raises. Empty/absent
  `ocr["sections"]` → every section gets `table_refs: []`, `figure_refs: []` (safe on a pre-Phase-0 or
  hierarchy-less document). A document whose AST has zero sections and a table/figure ref arrives →
  a preamble section is created holding it (never dropped).
- `_raw_to_structure_index`: a raw index absent from every table's (optional) `source_indices`/identity
  default → simply missing from the returned dict; `build_graph` `.get(...)`s and skips that ref rather
  than raising or emitting a malformed node.
- `build_graph`: a `table_refs`/`figure_refs` entry with no match in the bridge, or a structure index
  `>= len(tables)` → skipped. An AST section missing the `table_refs`/`figure_refs` key entirely
  (pre-Phase-3b `ast` artifact, cache staleness) → treated as `[]` via `.get(..., [])`. A repeat structure/
  raw index across sections → first-section-wins, no duplicate node/edge (decision 11).
- `Neo4jGraphStore._load_tx`: an unknown node/edge type still raises `ValueError` — unchanged, and this
  phase's own test (`test_neo4j_store_unknown_node_type_raises`, unmodified) keeps proving it still fires
  for a genuinely unknown type (e.g. `"widget"`) even after the allowlist grows.
- `document_structure` (both stores): a table/figure node with no matching `CONTAINS` edge (should not
  happen; defensive) → empty `tables`/`figures` list for that section, not an error — same posture as the
  existing `paragraphs` handling.

## 5. Testing

- **Unit, new `test_attach_table_refs.py`**: a crafted `ocr["sections"]` hierarchy (root + one nested
  subsection reached via `"/sections/N"`) interleaving `"/paragraphs/N"`/`"/tables/M"`/`"/figures/K"` refs
  around two heading paragraphs whose `source_index` matches the `ast` fixture's `heading_source_index`
  values → asserts `table_refs`/`figure_refs` land on the correct sections, in ref-encounter order, with
  figure captions attached; a table/figure ref before any heading → lands on the preamble section (case A:
  one already exists; case B: none exists → one is created at index 0, later section indices shift);
  a cyclic `"/sections/N"` chain → terminates (no hang), refs past the cycle point on that branch are
  simply not walked; malformed/dangling refs → skipped, no raise; empty/absent `ocr["sections"]` → every
  section gets `[]`/`[]`; `build_ast`'s own tests (`test_build_ast.py`) untouched, `build_ast` itself
  unmodified.
- **Integration, `test_ast_stage.py` (extend)**: `AstStage.deps == ("normalize", "ocr")`, `.version == 4`;
  a `FakeOcrClient` fixture with a minimal `sections`/`tables`/`figures` hierarchy → the persisted `ast`
  artifact has correct `table_refs`/`figure_refs`; `artifact_key(..., "ast", 4, ...)` used for the lookup.
- **Unit, `test_build_graph.py` (extend heavily)**: every existing call site updated to
  `build_graph(ast, {})` (an empty `structure` degrades to zero table/figure nodes — proves the existing
  document/section/paragraph assertions stay byte-identical, i.e. genuinely additive). New cases: a
  section with `table_refs=[0]` + a `structure["tables"][0]` with a caption and **no** `source_indices`
  key → table node `t0` with that label + `CONTAINS` edge from the section (identity-default bridge); the
  same but with an explicit `source_indices=[0, 1]` on `structure["tables"][2]` and `table_refs=[1]` →
  resolves to `t2` (proves the optional-key bridge, not just the identity default); an unmapped raw index
  → skipped, no raise, no node; a `figure_refs=[{"index": 3, "caption": "Fig 3: Org chart"}]` → `f3` node
  with that label + edge; a figure ref with a blank/absent caption → falls back to `"Figure {k}"`; the
  same raw table index appearing in two sections' `table_refs` (simulated) → only the first section's
  node/edge is emitted (decision 11); no `READING_ORDER` edge ever touches a `t*`/`f*` node id;
  `schema_version == 3`.
- **Unit, `test_graphstore.py` (extend `FakeGraphStore`)**: `document_structure` fixture with a section
  CONTAINS-linked to a `type="table"` node (id `d1:t3`) and a `type="figure"` node (id `d1:f1`) →
  section view's `tables`/`figures` lists are populated and correctly shaped; a fixture with `d1:t10` and
  `d1:t2` under the same section → asserts `t2` sorts before `t10` (numeric, not lexicographic — the
  regression test for decision 13's explicit sort-key choice); `reading_order`'s per-section view excludes
  `paragraphs`, `tables`, **and** `figures` (widened exclusion, was `paragraphs`-only); every existing
  `FakeGraphStore` test in this file (calls-recording, neighbors, doc-scoping) untouched/still passing.
- **Unit, `test_neo4j_graphstore.py` (extend)**: loading a `type="table"`/`type="figure"` node no longer
  raises; the recorded Cypher query text contains `MERGE (n:Table` / `MERGE (n:Figure`;
  `test_neo4j_store_unknown_node_type_raises` (unmodified) still raises for `"widget"`; a new test for
  `_document_structure_tx` (via the existing `_FakeTx`-recording pattern) asserting a `Table`-typed
  `CONTAINS` query is issued and its rows are grouped/numeric-sorted into the returned section's `tables`
  key, mirroring the equivalent `paragraphs` assertion style already in this file.
- **Integration, `test_graph_stage.py` (extend)**: register + run `StructureStage` in the chain before
  `graph` (today's fixture has none); `assert "graph" in resolver.successors("structure")` alongside the
  existing `resolver.successors("ast")` assertion (both now hold — `graph` depends on both); the
  `artifact_key(..., "graph", 3, derived_input_hash([facts["ast"].output_hash,
  facts["structure"].output_hash]))` lookup (order matches `GraphStage.deps`); `body["schema_version"] ==
  3`; existing document/section/paragraph node/edge assertions unchanged; extend the `FakeOcrClient`
  fixture with one table so at least one `t*` node/edge round-trips end-to-end through real
  Postgres+MinIO, not just the pure unit test.
- **Real-doc regression (scratchpad, documented — not committed, same posture as prior phases):** re-run
  ingestion on the real STTL doc through Phase 3b; confirm (a) `ast`'s `table_refs`/`figure_refs` sum to
  the audit's measured 261 tables / 33 figures with membership distributed across sections (not all
  dumped into the preamble — the concrete proof decision 1's hierarchy-lockstep assumption holds on the
  real doc, not just a fixture); (b) `graph["nodes"]` gains ~261 `table` + ~33 `figure` nodes, `CONTAINS`
  edge count grows by the same amount, zero change to the pre-existing document/section/paragraph node
  count; (c) `document_structure` over the real doc's graph shows every major statement section (`Balance
  Sheet`, `Statement of Profit and Loss`, `Cash Flow Statement`) with a non-empty `tables` list — the
  quantified proof this phase closes P3-11's graph half in full.
- **Gates:** grep every `schema_version == 2` assertion on the `graph` artifact and every
  `AstStage.version == 3`/`GraphStage.version == 2`/`build_graph(ast)`-single-arg call site; update. Full
  suite green; `ruff`/`mypy --strict`/`lint-imports` 1-kept/0-broken (every new/changed function stays in
  `core`, pure, plain `Mapping`/`Sequence` params, no new cross-layer import — `attach_table_refs` and the
  `build_graph` additions follow the exact same purity contract every other `core.ast`/`core.graph`
  function already does).

## 6. Explicitly deferred (and the forks a human should decide)

- **FORK — hierarchy lockstep (recommended, §2 decision 1) vs. span/page heuristic.** Resolved in this
  spec (adopted (a)) for the reasons in decision 1, but restated as a fork because the assumption
  ("every section-opening heading appears explicitly in Azure's hierarchy") is still not proven for every
  possible document shape — only verified true on the audited STTL doc. A human could prefer (b) for
  robustness against a hierarchy that skips some headings, at the cost of duplicating provenance-style
  page-matching logic in a second place.
- **FORK (smaller) — `document_structure`/`reading_order` surfacing tables/figures (recommended AND
  implemented now, §2 decision 13) vs. leaving them reachable only via `neighbors`.** The task framing for
  this item was explicitly "recommend; can defer the protocol surface," matching Phase 3's original
  posture of flagging it as a smaller, deferrable fork. This spec resolves it by implementing it (the
  query is small, symmetric with the existing `paragraphs` one, and leaving it out would keep P3-11 only
  half-closed) — but a human optimizing for the smallest possible diff in this phase could still cut
  §2 decision 13 (both `document_structure` changes and the `FakeGraphStore.reading_order` exclusion
  widen) and ship only §2 decisions 1-12/15-17 (membership + nodes + allowlist, no browse-surface change);
  tables/figures would then still be fully valid, loadable graph nodes, just not listed per-section by
  `document_structure` until a later pass adds it.
- **`retrieval.py`'s `_table_context` does not consume the new `table_refs` to prefer the owning section's
  heading over the table's own caption — deliberately NOT done here (out of file scope for this phase).**
  Once `table_refs`/the raw→structure bridge exist, `_table_context` COULD upgrade its two-tier fallback
  (caption else `"Table {i}"`) to a three-tier one (section heading, THEN caption, THEN `"Table {i}"` —
  Phase 3's original §2 decision 3 priority order), giving table chunks a more contextual embed string.
  This is a real, small, likely-valuable follow-up, explicitly left to whichever track next touches
  `retrieval.py`/`ChunksStage` (Phase 4/5, or a dedicated small PR) — not this phase's job per the given
  scope boundary.
- **`graphquery.py`'s per-table/figure citation gap (§2 decision 14) — deferred, same posture as Phase 3's
  original decision 14 for `build_provenance`.** `document_structure`'s pass-through already surfaces
  `tables`/`figures` per section (zero code change verified), but individual entries inside those lists
  don't get a `citation` (page/doc_key/version) the way sections/paragraphs do, since `_cite` isn't called
  on them. Cheap to add later (`[_cite(t, doc, prov) for t in s.get("tables", [])]`) once `build_provenance`
  actually resolves `"table"`/`"figure"` node types (Phase 2's job, per its own decision 14) — adding the
  citation call now would just produce `page: None` for every entry, no real improvement yet.
- **NOT in scope:** `structure.py`'s real `source_indices` field (§2 decision 3) — `core/graph.py`'s
  optional-key reader is forward-compatible with it but does not require or add it. **NOT in scope:**
  `READING_ORDER` edges or reading-chain membership for table/figure nodes. **NOT in scope:** footnote
  graph nodes (audit's P3-11 also names footnotes; Azure tagged 0/11,428 paragraphs as footnote on the
  real doc per the audit's linking findings, so there is currently no real footnote data to attach — left
  for whichever phase addresses footnote detection/fallback).

## 7. Done criteria

1. `attach_table_refs` (pure, `core.ast`) additively populates `table_refs`/`figure_refs` on every AST
   section from Azure's `sections[].elements` hierarchy, using `heading_source_index` equality (not a
   re-derived role rule) for section-boundary tracking; cycle-guarded, never raises. `AstStage` (`v4`,
   `deps=("normalize","ocr")`) calls it after `build_ast`; `build_ast` itself is unmodified (all its
   existing tests pass unchanged).
2. `build_graph(ast, structure)` emits `table`/`figure` nodes with `CONTAINS` edges from their owning
   section (id scheme `t{structureIndex}`/`f{rawIndex}`, matching the already-shipped `chunks` artifact's
   `t{i}` convention exactly — zero-translation cross-referencing), via a private, optional-key
   raw→structure bridge that defaults to identity today and requires no `structure.py` change.
   `GraphStage` (`v3`, `deps=("ast","structure")`); the pre-existing document/section/paragraph
   nodes/edges and `order_reading_chain` are byte-identical to before this phase.
3. `Neo4jGraphStore._NODE_LABELS` extended with `table`/`figure` (mandatory — a real-store `graph_load`
   would otherwise raise `ValueError` on the first Phase-3b artifact); `FakeGraphStore` needs no
   allowlist change (already generic). `document_structure`/`reading_order` (both stores) additively
   surface each section's numeric-sorted `tables`/`figures`; `FakeGraphStore.reading_order`'s exclusion
   list widened accordingly; `Neo4jGraphStore.reading_order` needs no change (was already outline-only).
4. `graphquery.py` needs zero code change (verified, not assumed) to pass through the new `tables`/
   `figures` keys; the per-entry citation gap this leaves is documented, not silently discovered later.
   `GraphLoadStage` needs zero code change and re-runs automatically via the content-addressed cascade
   (stays `v2`); the one-time, functionally-no-op `ChunksStage`/`EmbeddingStage` recompute this phase's
   `AstStage` output-hash change triggers is documented (§2 decision 17), not a surprise.
5. Unit + integration tests cover every new function/shape change and every degrade path (§5); all stale
   `schema_version`/`.version`/single-arg-`build_graph` call sites updated; all four gates green; `core`
   stays pure (every new/changed function takes plain `Mapping`s/`Sequence`s, no new cross-layer import).
6. Real-doc regression (scratchpad, §5) shows table/figure membership distributed across real sections
   (not dumped into the preamble), the corresponding node/edge counts in `graph`, and every major
   financial-statement section's `document_structure` view carrying a non-empty `tables` list — the
   quantified proof P3-11 is closed **in full** on the graph side (Phase 3a already closed it on the
   retrieval/chunk side).
