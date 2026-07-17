# SP-9 — Canonical Structure Graph (JSON, Neo4j loading deferred)

- **Status:** Implemented 2026-07-17 on `feature/sp-9-graph` (worktree `D:\fdi-wt\sp9`). All §7 done criteria verified (unit + testcontainers + static gates; full non-e2e/non-azure suite 229 passed, 5 deselected; ruff / mypy --strict / lint-imports 1 kept 0 broken).
- **Depends on:** SP-8 (the AST artifact, `fdi.core.ast.build_ast` →
  `{schema_version, title, sections:[{heading, paragraphs:[str]}]}`), SP-2 (stage/DAG framework).
- **Roadmap:** `docs/architecture/ingestion-roadmap.md` §3 table — SP-9 "Canonical Graph → Neo4j",
  phase 6 "typed edges: hierarchy · reading · spatial · table · xref", weight medium, currently ⬜.
  Stage-2 build order lists `SP-9 graph → SP-12 provenance` right after structural intelligence.

## 1. Purpose

Phase 6 of the roadmap asks for a **typed node/edge graph** over a document's structure — the shape a
graph database (Neo4j) or a graph-aware retrieval step would walk, instead of the tree SP-8 built. The
full ambition (§2 of the phase list) is five edge families: hierarchy, reading order, spatial, table,
and cross-reference — but today only SP-8's AST (title/sections/paragraphs, no bounding boxes, no
tables, no cross-refs) exists as input. Building all five edge types now would mean inventing spatial
and table edges the codebase has no data for yet (those need SP-7's structure artifact and SP-7d's
linking work), and inventing a schema for edges that will only be exercised by later sub-projects.

SP-9 is therefore the **thin slice**: a typed graph over exactly what the AST already contains —
document → section → paragraph containment, plus reading order between siblings — expressed as a
**pure `build_graph` function** and a **derived pipeline stage**, emitting a versioned **JSON artifact**
(nodes + edges) that is the source of truth. **Neo4j loading is explicitly deferred** — no new
container, no `graph` Compose profile, no loader in this slice. A future sub-project projects this JSON
artifact into Neo4j; SP-9 only has to make that projection possible by giving the artifact a stable,
typed shape.

Thin slice, pure-core-first (the same shape as SP-6/SP-8/SP-10/SP-17): `fdi.core.graph.build_graph` +
exhaustive unit tests, then a `GraphStage` (`deps=("ast",)`) that auto-chains off AST via the existing
DAG machinery — no framework change.

## 2. Scope decisions (autonomous; documented)

1. **Two edge types only: `CONTAINS` and `READING_ORDER`.** `CONTAINS` is the hierarchy edge family
   (document→section, section→paragraph). `READING_ORDER` is the reading edge family (section→next
   section, paragraph→next paragraph within the same section). **Spatial, table, and cross-reference
   edges are explicitly out of scope** — they need bounding boxes (SP-7a/b enriched OCR), canonical
   table grids (SP-7c), and links (SP-7d), none of which the AST carries. Adding them now would mean
   fabricating data the AST doesn't have.
2. **Node ids are positional and deterministic, not UUIDs.** `"doc"` for the single document node,
   `"s{i}"` for the i-th section (0-indexed, source order), `"s{i}p{j}"` for the j-th paragraph
   (0-indexed) within section i. This mirrors the AST's own implicit ordering (a list, not a keyed map)
   and keeps `build_graph` pure and reproducible — no id-generation side channel (RNG, counter object,
   clock) is needed, which matters for roadmap invariant #2 (byte-identical re-run).
3. **Paragraph labels are truncated to keep the graph artifact small and graph-tool-friendly.** A
   paragraph's `label` is its text sliced to the first 80 characters (`text[:80]`) — the full text
   still lives in the AST artifact the graph was built from; the graph is a navigation structure, not a
   second copy of the document body. 80 is an arbitrary but generous "one line in a graph viewer"
   budget; not configurable in this slice (no known need yet).
4. **`GraphStage` is a derived stage** (`deps=("ast",)`), auto-scheduled after `ast` by the DAG
   (`successors("ast")` gains `"graph"` alongside the existing `"quality"`/`"semantic"`), `input_hash`
   from the AST's `output_hash`. Determinism: pure function of the AST, same discipline as
   `AstStage`/`QualityStage`.
5. **The JSON graph artifact is the source of truth this slice; Neo4j is a later projection, not a
   parallel write.** Writing to both a JSON artifact and a live Neo4j instance in the same stage would
   mean the stage is no longer a pure content-addressed transform (Neo4j writes aren't idempotent or
   content-addressed the way `ArtifactStore.put_json` is) and would pull a new container into every
   later sub-project's Compose stack before anything downstream needs Neo4j directly. Deferring the
   loader keeps this slice small and keeps the "12-factor / profiled Compose" invariant (roadmap §6)
   intact — the `graph` profile is provisioned for later, not turned on now.

## 3. Architecture

- **`fdi.core.graph.build_graph(ast: Mapping[str, object]) -> dict[str, object]`** (pure, no I/O) →
  `{"schema_version": 1, "nodes": [{"id": str, "type": str, "label": str}], "edges": [{"source": str,
  "target": str, "type": str}]}`.
- **`fdi.pipeline.stages.graph.GraphStage`** (`name="graph"`, `version=1`, `deps=("ast",)`) — `run(ctx)`
  reads `ctx.read("ast")` and returns `build_graph(...)`. Self-registered into `REGISTRY`, imported
  alphabetically in `stages/__init__.py` (`ast, graph, normalize, ocr, ping, quality, semantic,
  structure`).

### `build_graph` algorithm

```
title = ast.get("title") as str, else ""
nodes = [{"id": "doc", "type": "document", "label": title}]
edges = []
prev_section_id = None
for i, section in enumerate(ast.get("sections", [])):
    sid = f"s{i}"
    heading = section.get("heading") as str, else ""
    nodes.append({"id": sid, "type": "section", "label": heading})
    edges.append({"source": "doc", "target": sid, "type": "CONTAINS"})
    if prev_section_id is not None:
        edges.append({"source": prev_section_id, "target": sid, "type": "READING_ORDER"})
    prev_section_id = sid

    prev_para_id = None
    for j, para in enumerate(section.get("paragraphs", [])):   # para: str
        pid = f"{sid}p{j}"
        nodes.append({"id": pid, "type": "paragraph", "label": para[:80]})
        edges.append({"source": sid, "target": pid, "type": "CONTAINS"})
        if prev_para_id is not None:
            edges.append({"source": prev_para_id, "target": pid, "type": "READING_ORDER"})
        prev_para_id = pid

return {"schema_version": 1, "nodes": nodes, "edges": edges}
```

Node order: `doc`, then per section (section node, then its paragraph nodes) in AST order — a
depth-first walk, matching the AST's own nesting. Edge order: for each section, its `CONTAINS` edge
from `doc` and (if any) its inter-section `READING_ORDER` edge from the previous section are emitted
before its paragraphs' `CONTAINS`/`READING_ORDER` edges — i.e. edges are emitted in the same walk order
as the nodes that produce them, not grouped by type. This ordering is part of the pure function's
observable, tested output (determinism, roadmap invariant #2) — not an implementation detail free to
vary.

## 4. Error handling

- Missing/malformed `title` (absent, `None`, non-`str`) → `""` — a document with no captured title
  still gets a valid, empty-labeled `doc` node, not an error.
- Missing `sections` (absent, `None`, non-`list`) → treated as `[]` — the graph is just the lone `doc`
  node, no edges. A document with no walkable structure is valid (mirrors SP-8's empty-AST case), not
  an error.
- Non-`dict`/non-`Mapping` entries inside `sections`, or a missing/non-`str` `heading` → the entry is
  skipped entirely (not just given an empty heading) if it isn't a mapping at all; a mapping with a bad
  `heading` degrades to `""` the same as a missing title. This mirrors `build_ast`'s own defensive
  `isinstance` narrowing (`fdi/core/ast.py`) rather than trusting the input's shape, because although
  `build_graph`'s only real caller today is `build_ast`'s own output, `Mapping[str, object]` at the type
  level gives mypy no such guarantee — narrowing has to happen at runtime either way.
- Missing/malformed `paragraphs` (absent, `None`, non-`list`) → `[]`; non-`str` entries inside
  `paragraphs` are dropped (not coerced) — a paragraph is text, by construction of `build_ast`; a
  non-`str` paragraph would be an upstream bug, and silently stringifying it risks masking that bug
  behind a plausible-looking label.
- No raise anywhere in `build_graph` — like `build_ast`, an unusual/empty input produces a smaller
  (never absent) graph, never an exception. This is deliberate: a scanned cover page or a
  title-only document is a valid, if graph-thin, document.

## 5. Testing

- **Unit** (no Docker), `backend/tests/unit/test_build_graph.py`:
  - A 1-section, 2-paragraph AST → the exact nodes list (`doc`, `s0`, `s0p0`, `s0p1`) and edges list
    (`CONTAINS doc→s0`, `CONTAINS s0→s0p0`, `CONTAINS s0→s0p1`, `READING_ORDER s0p0→s0p1`) — no
    inter-section `READING_ORDER` (only one section).
  - An empty AST (`{}`) → just the `doc` node (`label: ""`), `edges: []`.
  - A 2-section AST (1 paragraph each) → both sections' `CONTAINS` edges plus the inter-section
    `READING_ORDER s0→s1` edge, confirming siblings link even when each has only one paragraph (so the
    inter-section edge isn't accidentally produced by the paragraph-loop logic).
  - A paragraph longer than 80 characters → its node `label` is exactly the first 80 characters (proves
    the truncation, not just "some" truncation).
  - Missing `title` / missing `sections` / non-`str` `heading` → degrade to `""` / `[]` per §4, never
    raise.
- **Integration** (testcontainers, fake OCR), `backend/tests/integration/test_graph_stage.py`: run
  `ocr → normalize → ast → graph` via `run_stage` in a **local** `StageRegistry` (register
  `OcrStage`+`NormalizeStage`+`AstStage`+`GraphStage`, not the global `REGISTRY`); assert the `graph`
  artifact's nodes/edges match the AST it was built from, and that `DagResolver(reg.all()).successors("ast")`
  contains `"graph"`. Note: registering `GraphStage` into the process-wide `REGISTRY` (via
  `stages/__init__.py`) means the **global** `successors("ast")` also gains `"graph"` alongside
  `"quality"`/`"semantic"` — any assertion on the exact contents of `successors("ast")` must be a
  membership check (`"graph" in ...`), not an exact-list equality; the existing SP-10/SP-17 tests
  already use membership for this reason, so no retrofit is expected, but scan for exact-list
  assertions before landing.

## 6. Explicitly deferred (YAGNI)

- **Neo4j loading.** A `graph` Compose profile (Neo4j container) + a loader that syncs the JSON graph
  artifact into Neo4j (nodes/edges as Cypher `MERGE`s, keyed by the same `id`s) is a later, separate
  sub-project. The JSON artifact's stable `{id, type, label}` / `{source, target, type}` shape is
  designed to make that projection mechanical when it happens — no schema change anticipated, only a
  new consumer.
- **Spatial edges** (bounding-box adjacency/overlap) — need SP-7a/b's enriched OCR geometry (words/
  cells/bboxes), which the AST does not carry.
- **Table edges** (row/column/cell membership, header association) — need SP-7c's canonical table
  grid; the AST has no table nodes at all yet.
- **Cross-reference edges** (captions, footnotes, "see Note 12") — need SP-7d's linking work, not yet
  built.
- **Provenance edges** (node → page → polygon → PDF span) — SP-12 (Provenance Graph), phase 9; SP-9
  only reaches as far as the AST's own abstraction, which has no page/polygon information.
- **Semantic-typed section nodes** (`section.type` from SP-10, e.g. `BALANCE_SHEET`) — not folded into
  the graph's node `type`/`label` in this slice; the graph's `section` node type stays structural,
  distinct from SP-10's semantic classification. A future version could join the two artifacts (same
  `heading`-order alignment SP-10's handoff note already documents) — deliberately not this slice's job.
- A configurable paragraph-label truncation length — no known caller needs it yet; `text[:80]` is a
  literal, not a parameter, until one does.

## 7. Done criteria

1. `build_graph(ast)` returns `{schema_version, nodes[{id,type,label}], edges[{source,target,type}]}`
   per the algorithm in §3 (one `doc` node; one `section` node + one `paragraph` node per AST
   paragraph; `CONTAINS` document→section and section→paragraph; `READING_ORDER` between consecutive
   sections and between consecutive paragraphs within a section; label truncation at 80 chars; all §4
   error-handling cases correct).
2. `GraphStage` (`deps=("ast",)`) reads the AST and produces the graph; a rerun is a cache hit; its
   `input_hash` derives from the AST's `output_hash`.
3. `DagResolver(REGISTRY.all()).successors("ast")` includes `"graph"` (alongside the pre-existing
   `"quality"`/`"semantic"`).
4. All four static gates green (`ruff`, `mypy --strict`, `lint-imports` 1 kept/0 broken, full suite);
   `core` stays pure (`fdi/core/graph.py` has no I/O); Neo4j loading and richer edge types remain
   explicitly out of scope (§6), noted as follow-ons in the roadmap handoff.
