# SP-12 — Provenance (node → page → geometry)

- **Status:** Implemented 2026-07-17. `build_provenance` (`fdi.core.provenance`, 8 unit tests) +
  `ProvenanceStage` (`deps=("graph", "ocr")`, integration test proving the `graph`+`ocr` → `provenance`
  chain and `"provenance" in successors("graph")`/`successors("ocr")` membership) both land as
  specified; all four gates green (`ruff`, `mypy --strict`, `lint-imports` 1 kept/0 broken, full
  non-e2e suite 271 passed).
- **Depends on:** SP-9 (`fdi.core.graph.build_graph` → `{schema_version, nodes:[{id,type,label}],
  edges:[{source,target,type}]}`; node ids `"doc"`, `"s{i}"`, `"s{i}p{j}"`; paragraph-node `label` =
  paragraph text truncated to 80 chars; section-node `label` = heading; document-node `label` =
  title), SP-7a (the enriched OCR artifact, `fdi.core.ocr_schema.normalize_ocr` →
  `paragraphs:[{role, content, bounding_regions:[{page_number, polygon}], spans}]`, produced by
  `OcrStage` at the `ocr` artifact key), SP-2 (stage/DAG framework — **confirmed multi-dep support,
  established fact, not re-investigated for this spec**: `run.py`'s `input_hash =
  derived_input_hash([facts[d].output_hash for d in stage.deps])` hashes deps' `output_hash`es IN
  ORDER; `resolver.py`'s `successors(name)` returns every stage with `name in deps` and `_depth(name)
  = 1 + max(_depth(d) for d in deps)` — both already iterate `stage.deps`/`s.deps` generically, so no
  framework change is needed to give a stage two deps).
- **Roadmap:** `docs/architecture/ingestion-roadmap.md` §3 table — SP-12 "Provenance Graph", phase 9
  "Fact → Cell → Table → Page → Polygon → Span → PDF", weight medium, currently ⬜. Build order §4
  lists "SP-9 graph → SP-12 provenance" right after structural/graph intelligence. **Note:** SP-17b's
  spec (`docs/superpowers/specs/2026-07-17-sp17b-structure-quality-design.md`, also Draft/not yet
  implemented) independently designs `QualityStage` to grow to `deps=("ast","structure")` and, as
  written, also calls itself "the pipeline's first two-dependency stage." As of this writing NEITHER
  is implemented — every stage actually registered in `stages/__init__.py` has exactly one dep — so
  the claim is accurate for both specs today; whichever of SP-12/SP-17b lands first in code earns
  "first" in practice, and the other's spec text should be corrected post-hoc rather than treated as a
  contradiction to resolve now.

## 1. Purpose

SP-9 built a typed `CONTAINS`/`READING_ORDER` graph over the AST, but that graph is purely
structural — a `paragraph` node's only payload is 80 characters of text; there is no way to answer
"which page, and where on that page, did this node come from?" Phase 9 of the roadmap
(`Fact → Cell → Table → Page → Polygon → Span → PDF`) asks for exactly that: a **provenance
mapping** from every graph node back to its source geometry, so that later consumers (a UI that
highlights the source region for a fact, a retrieval step that needs to cite a page) can answer "show
me where this came from" without re-deriving it.

The full ambition — provenance for table cells, figures, footnotes, and cross-reference edges — needs
graph node types SP-9 does not emit yet (no table/figure/footnote nodes in the graph) and linking data
SP-7d produces but the graph doesn't consume. SP-12 is therefore the **thin slice**: provenance for
the node types the graph *does* have today — `document`, `section`, `paragraph` — via a **label-based
join** against the enriched OCR artifact's paragraphs (the same artifact `normalize_ocr` already
carries `bounding_regions`/`page_number` on). No new OCR call, no new geometry — SP-12 only has to
*join* two artifacts that already exist.

Thin slice, pure-core-first (the same shape as SP-9/SP-11): `fdi.core.provenance.build_provenance` +
exhaustive unit tests, then a `ProvenanceStage` (`deps=("graph", "ocr")`) that auto-chains off BOTH
via the existing DAG machinery — no framework change. This is the codebase's **first stage to
actually declare two dependencies** (see the roadmap note above re: SP-17b's parallel draft).

## 2. Scope decisions (autonomous; documented)

1. **The join key is the node's `label` against the OCR paragraph's `content` — not a span/offset
   join.** SP-9's `build_graph` truncates a paragraph's text to 80 characters for its `label`; the
   full, untruncated text still lives only in the AST, which in turn only carries `str` paragraph
   text (no span/offset back-reference to the OCR artifact — `build_ast` drops that information).
   Reconstructing a true span-based join would mean threading span offsets through `build_ast` and
   `build_graph` first, which neither does today and is out of scope for this slice (a "de-lossify the
   AST" project of its own). A **label-based join against the OCR paragraphs directly** — the OCR
   artifact IS still available as `ProvenanceStage`'s second dependency — needs no upstream change and
   is the join this slice ships.
2. **Match rule differs by node type, using the same asymmetry SP-9's `build_graph` already has
   between truncated and untruncated labels:**
   - **`paragraph`** node: label is a **prefix** (the first 80 chars) of the true paragraph text, so
     the match is `ocr_paragraph.content.startswith(node.label)` — `startswith`, not `==`, is the
     *correct* test here, not a looser approximation of one.
   - **`section`** node: label IS the full, untruncated heading (`build_graph` never truncates
     `heading`), so the match is exact equality, `ocr_paragraph.content == node.label`.
   - **`document`** node (`id == "doc"`): its label is the document title, which is not itself an OCR
     *paragraph* (Azure emits it with `role: "title"`, which `normalize_ocr` still stores as a regular
     paragraph entry with that content) — but rather than special-case title-role matching, the doc
     node's provenance is defined as **the page of the first OCR paragraph (any role) that has
     geometry at all** (a non-empty `bounding_regions` list). This gives every document a
     provenance-mapped `doc` node whenever the OCR artifact has ANY located paragraph, without
     depending on whether the title paragraph in particular carries geometry.
3. **First match wins, in document order — ambiguity is a documented, deferred limitation, not
   silently "improved" away.** If two OCR paragraphs share an 80-char prefix, or if the same heading
   text appears twice (e.g. a repeated "Notes" section), `build_provenance` resolves to the first
   OCR paragraph (in document order) satisfying the match — deterministic and simple, but capable of
   attributing a node to the wrong occurrence when duplicates exist. A precise fix needs the span-
   offset join deferred in decision 1; not attempted here (§6).
4. **A node with no match, or whose matched paragraph has empty `bounding_regions`, is OMITTED from
   `node_provenance` — never emitted with a null/placeholder `page`.** `node_provenance` is a sparse
   list (a subset of the graph's node ids), not a total map with nullable fields — a consumer that
   looks up a node id and doesn't find it knows unambiguously "no located source", rather than having
   to additionally check for a null `page`.
5. **`ProvenanceStage.deps = ("graph", "ocr")`, in that fixed order — the order is part of the
   artifact's cache identity, not just an implementation detail.** Per SP-2's confirmed multi-dep
   behavior (see "Depends on" above), `run.py` computes `input_hash =
   derived_input_hash([facts["graph"].output_hash, facts["ocr"].output_hash])` — reordering the tuple
   later would silently change every document's `provenance` artifact key. `graph` is listed first
   because it is the artifact whose nodes are being resolved (the "primary" input); `ocr` is the
   geometry source consulted per node. This mirrors SP-17b's parallel decision (`deps=("ast",
   "structure")`, primary-artifact-first) for the same reason.
6. **`ProvenanceStage` is a DERIVED stage** (`name="provenance"`, `version=1`, `deps=("graph",
   "ocr")`) — a pure function of two upstream artifacts, no injected service, no external call.
   Because it depends on both `graph` and `ocr`, the SP-2 resolver makes it a successor of **both**:
   `resolver.successors("graph")` and `resolver.successors("ocr")` both gain `"provenance"`.
   `run_stage("ocr", ...)` already auto-enqueues `normalize`/`ast`/`graph`; `provenance` now
   additionally waits on `graph` finishing (which itself waits on `ast`) before it can run — handled
   entirely by the existing generic `stale`/`plan`/`successors` machinery, no new code path.
7. **Only `document`/`section`/`paragraph` graph nodes get provenance in this slice — because those
   are the only node types SP-9's graph emits.** There is nothing to defer here so much as nothing yet
   to build: table-cell, figure, and footnote nodes don't exist in the graph artifact yet (SP-9 §6),
   so `build_provenance` has no node of those types to ever see. A node of an unrecognized `type`
   (forward-compatible: some future graph version emits one SP-12 doesn't know about) is silently
   skipped, exactly like a no-match — not an error.

## 3. Architecture

### 3.1 Layering

- **`fdi.core.provenance`** — `build_provenance(graph: Mapping[str, object], ocr: Mapping[str,
  object]) -> dict[str, object]` (pure; no I/O).
- **`fdi.pipeline.stages.provenance`** — `ProvenanceStage`, self-registered into `REGISTRY`.

### 3.2 `build_provenance` algorithm

```
candidates = []                          # [(content: str, bounding_regions: list[dict]), ...], document order
for para in ocr.get("paragraphs", []):    # skip non-Mapping entries
    content = para.get("content")
    if not isinstance(content, str):
        continue                          # can't match a non-str content against a str label
    regions = [dict(r) for r in para.get("bounding_regions", []) if isinstance(r, Mapping)]
    candidates.append((content, regions))

def page_of(regions):
    if not regions:
        return None
    p = regions[0].get("page_number")
    return p if isinstance(p, int) else None

doc_entry = None
for content, regions in candidates:       # first candidate with ANY geometry
    page = page_of(regions)
    if page is not None:
        doc_entry = {"node_id": "doc", "page": page, "bounding_regions": regions}
        break

node_provenance = []
for node in graph.get("nodes", []):       # skip non-Mapping entries
    node_id = node.get("id")
    if node_id == "doc":
        if doc_entry is not None:
            node_provenance.append(doc_entry)
        continue

    node_type = node.get("type")
    label = node.get("label")
    label = label if isinstance(label, str) else ""

    if node_type == "paragraph":
        match = first candidate where content.startswith(label)
    elif node_type == "section":
        match = first candidate where content == label
    else:
        continue                          # unrecognized node type: nothing to resolve, skip

    if match is None or not isinstance(node_id, str):
        continue
    _, regions = match
    page = page_of(regions)
    if page is None:                      # empty/malformed bounding_regions
        continue
    node_provenance.append({"node_id": node_id, "page": page, "bounding_regions": regions})

return {"schema_version": 1, "node_provenance": node_provenance}
```

`node_provenance` preserves the graph's own node order (`doc`, then each section followed by its
paragraphs, per SP-9's depth-first walk) — a stable, tested ordering, not incidental dict/list
iteration.

### 3.3 `ProvenanceStage`

```python
class ProvenanceStage:
    name = "provenance"
    version = 1
    deps: tuple[str, ...] = ("graph", "ocr")

    async def run(self, ctx: StageContext) -> object:
        graph = cast(Mapping[str, object], await ctx.read("graph"))
        ocr = cast(Mapping[str, object], await ctx.read("ocr"))
        return build_provenance(graph, ocr)
```

`deps=("graph", "ocr")` makes the SP-2 resolver schedule `provenance` as a successor of both
`graph` and `ocr` and hashes its `input_hash` from `derived_input_hash([graph.output_hash,
ocr.output_hash])` (dep-tuple order — decision 5). Running `ocr` still auto-enqueues the whole chain
(`normalize → ast → {graph, ontology, semantic quality...}`); `provenance` additionally waits on
`graph` before it can run, which the existing `run_stage`/`stale`/`plan` machinery already handles
generically.

## 4. Error handling

- Missing/non-list `ocr["paragraphs"]` → `candidates = []` → every node either has no match (skipped)
  or, for `doc`, `doc_entry` stays `None` (skipped) — a provenance-less document (e.g. OCR produced no
  paragraphs at all) yields `{"schema_version": 1, "node_provenance": []}`, not an error.
- A paragraph entry that isn't a `Mapping`, or whose `content` isn't a `str` (absent, `None`,
  non-`str`) → dropped from `candidates` entirely — it cannot be compared against a node's `str`
  label, so it is not a valid match target (distinct from "matched but empty regions", which still
  counts as a candidate).
- Missing/non-list/malformed `bounding_regions` (absent, `None`, non-list, entries that aren't
  `Mapping`) → degrades to `[]` for that paragraph — the paragraph is still a valid content-match
  candidate, but any node matched to it is omitted (empty-regions rule, decision 4) because there is
  no page to report.
- A `bounding_regions[0]` present but whose `page_number` is missing/non-`int` → treated the same as
  "no page": the matched node is omitted. This extends decision 4's "empty regions → omit" rule to
  "regions present but unusable → omit" — never fabricate or coerce a page number.
- Missing/non-list `graph["nodes"]` → no nodes to walk → `node_provenance = []` (mirrors `build_graph`
  own degrade-not-raise posture for a malformed AST).
- A node entry that isn't a `Mapping`, or whose `id` isn't a `str` (for anything other than the literal
  `"doc"` sentinel check, which is a plain equality and tolerates any type) → skipped; a `label` that
  is missing/non-`str` degrades to `""` (an empty-label paragraph node matches the FIRST candidate,
  since `content.startswith("")` is always `True` — a documented, tested degenerate case, not a bug).
- No raise anywhere in `build_provenance` — like `build_graph`/`map_document`, any malformed/partial
  input produces a smaller (possibly empty) `node_provenance`, never an exception.
- Running `provenance` before `graph` and/or `ocr` has a recorded artifact for the document →
  `ctx.read(...)` (via `latest_run`) asserts the upstream run exists; absent, it fails clearly — the
  same DAG-ordering guarantee/failure mode every other derived stage in this codebase already has.

## 5. Testing

- **Unit** (no Docker), `backend/tests/unit/test_build_provenance.py`:
  - A paragraph node whose 80-char `label` is a prefix of an OCR paragraph's `content` → the correct
    `page` + `bounding_regions` in `node_provenance`.
  - A section node whose `label` equals an OCR paragraph's `content` exactly → correct `page` +
    `bounding_regions`.
  - The `doc` node → the page of the FIRST OCR paragraph with any non-empty `bounding_regions`,
    regardless of that paragraph's role/content.
  - A node whose `label` matches no OCR paragraph → omitted from `node_provenance` (not present, not a
    null-page entry).
  - A node whose matched OCR paragraph has an empty `bounding_regions` list → omitted.
  - An empty graph (`{}` or `{"nodes": []}`) → `{"schema_version": 1, "node_provenance": []}`.
  - Two OCR paragraphs sharing the same 80-char prefix → the FIRST one (document order) is the
    resolved match — proves decision 3's determinism, not just that "a" match happens.
  - Malformed OCR paragraphs (non-`Mapping` entries, non-`str` `content`, missing/non-list
    `bounding_regions`, a `bounding_regions[0]` with a non-`int` `page_number`) → each degrades per §4,
    never raises.
- **Integration** (testcontainers, fake OCR), `backend/tests/integration/test_provenance_stage.py`:
  run `ocr → normalize → ast → graph → provenance` via `run_stage` in a **local** `StageRegistry`
  (register `OcrStage`+`NormalizeStage`+`AstStage`+`GraphStage`+`ProvenanceStage`, not the global
  `REGISTRY`) with a `FakeOcrClient` whose canned `AnalyzeResult` paragraphs carry `boundingRegions`;
  assert the `provenance` artifact maps the `doc`/section/paragraph nodes to the expected pages and
  regions; assert **both** `"provenance" in resolver.successors("graph")` **and** `"provenance" in
  resolver.successors("ocr")` (membership, per the SP-6/SP-8/SP-9/SP-11 "search-and-fix" precedent —
  registering `ProvenanceStage` globally via `stages/__init__.py` also adds `"provenance"` to the
  GLOBAL `successors("graph")`/`successors("ocr")`; any test asserting an exact list against the
  global registry must become a membership check — grep and fix before landing, none expected since
  every prior successor-adding stage already left the suite membership-only). Read the `provenance`
  key via `artifact_key(doc.id, "provenance", 1, derived_input_hash([facts["graph"].output_hash,
  facts["ocr"].output_hash]))` — dep-tuple order, decision 5.

## 6. Explicitly deferred (YAGNI)

- **Span-offset-based exact matching**, replacing the label/prefix/equality join (decisions 1-2) once
  `build_ast`/`build_graph` are extended to carry the OCR paragraph's `spans` (offset/length) through
  to the graph node. This removes the ambiguous-prefix/duplicate-heading failure mode (decision 3)
  entirely, at the cost of touching SP-8's and SP-9's artifact shapes — a real but separate follow-on.
- **Provenance for table-cell, figure, and footnote graph nodes** — blocked on the graph artifact
  itself gaining those node types (SP-9 §6's own deferral); `build_provenance` has nothing to resolve
  for a node type the graph doesn't emit.
- **Cross-reference edge provenance** ("see Note 12" pointing at the actual Note 12 paragraph) — needs
  SP-7d's linking/resolver output wired into the graph as edges first; SP-12 only maps NODES to
  geometry, not edges.
- **Polygon-level (sub-paragraph) provenance** — today's `bounding_regions` is the paragraph's whole
  region(s); highlighting a specific word/phrase within a paragraph needs word-level geometry
  (`pages[].words[].polygon`, already present in the raw Azure result but not carried into
  `normalize_ocr`'s paragraph shape) and is out of scope here.
- **Disambiguating repeated headings/duplicate prefixes** by anything smarter than "first in document
  order" (e.g. scoping the search to paragraphs within the correct section's span range) — needs the
  span-offset join (first bullet) to do properly; not attempted as a standalone heuristic.

## 7. Done criteria

1. `build_provenance(graph, ocr)` returns `{schema_version, node_provenance:[{node_id, page,
   bounding_regions}]}` per the algorithm in §3.2: paragraph nodes matched by label-prefix, section
   nodes matched by label-equality, the `doc` node matched to the first paragraph with any geometry;
   unmatched nodes and empty/unusable-geometry matches omitted; all §4 error-handling cases correct;
   never raises.
2. `ProvenanceStage` (`deps=("graph", "ocr")`) reads both artifacts and produces the provenance
   artifact; a rerun is a cache hit; its `input_hash` derives from `derived_input_hash([graph.
   output_hash, ocr.output_hash])` in that order.
3. `DagResolver(REGISTRY.all()).successors("graph")` **and** `.successors("ocr")` both include
   `"provenance"` — the pipeline's first stage (among those actually implemented) to declare two
   dependencies, exercising SP-2's already-generic multi-dep machinery with no framework change.
4. All four static gates green (`ruff`, `mypy --strict`, `lint-imports` 1 kept/0 broken, full suite);
   `core` stays pure (`fdi/core/provenance.py` has no I/O); no test asserts an exact-list
   `successors("graph")`/`successors("ocr")` result that this addition would break (confirmed by
   audit, not assumed); span-offset matching and richer node-type coverage remain explicitly out of
   scope (§6), noted as follow-ons in the roadmap handoff.
