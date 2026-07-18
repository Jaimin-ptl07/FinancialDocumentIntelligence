# RT-5 — Graph Retrieval (+ corpus-graph fix)

- **Status:** Implemented — 2026-07-18. Fifth sub-project of the retrieval layer (`docs/architecture/
  retrieval-roadmap.md`), Wave B (parallel with RT-2/RT-3/RT-6; needs only ingestion, not RT-1). All
  10 plan tasks executed via strict TDD; full non-e2e suite green; gates (`ruff`, `mypy --strict`,
  `lint-imports` 1-kept/0-broken) green. See §7 done criteria — all satisfied.
- **Depends on (ingestion, all complete):**
  - **SP-9** (`fdi.core.graph.build_graph(ast) -> {schema_version, nodes:[{id,type,label}],
    edges:[{source,target,type}]}`; node `type` ∈ `{document,section,paragraph}`, edge `type` ∈
    `{CONTAINS,READING_ORDER}`; ids positional and **bare**: `doc`, `s{i}`, `s{i}p{j}`).
  - **SP-9b** (`GraphStore` protocol (`fdi.pipeline.graphstore`) + `Neo4jGraphStore` (`fdi.platform.
    graphstore`) + `FakeGraphStore`; `GraphLoadStage` (`fdi.pipeline.stages.graphload`,
    `deps=("graph",)`) projects the `graph` artifact into the store).
  - **SP-12** (`provenance` artifact `{node_provenance:[{node_id, page, bounding_regions}]}`, keyed
    on the SAME bare ids `build_graph` emits — `node_id == "doc"` / `"s{i}"` / `"s{i}p{j}"`).
  - **SP-2** (stage/DAG framework; `StageContext` exposes `doc_id`, `read(stage)`, `services`).
- **Does NOT depend on RT-1.** The `chunks`/`resolve_chunks` foundation is section-text-shaped; RT-5
  reads the graph and provenance artifacts directly. (RT-1's `s{i}` identity convention is still the
  one RT-5 aligns section-node ids to — see decision 1.)

## 1. Purpose

Ingestion built a correct graph *shape* (`build_graph`) but a broken graph *store*: `Neo4jGraphStore.
_load_tx` does `MERGE (n:Node {id: $id})` and `GraphLoadStage` passes ids straight through
unnamespaced. Every document's graph uses the identical ids `doc`/`s0`/`s0p1`/…, so loading a second
document's graph MERGEs directly onto the first document's nodes — last-writer-wins, cross-document
collision, no usable corpus graph. Every node also carries the generic label `:Node` and every edge the
generic type `:REL` (the real CONTAINS/READING_ORDER semantics sit only in a `type` *property*, never
as a Cypher label/rel-type), so nothing in Neo4j can be queried graph-natively (`MATCH (:Section)`,
`-[:CONTAINS]->`) even for a single document. **No route reads Neo4j at all today.**

RT-5 does two things, in order:

1. **Fixes the corpus graph.** Namespace every node id `{doc_id}:{bare_id}` at load time (so section
   nodes land on the exact same `{doc_id}:s{i}` identity RT-1's `chunks` artifact and Milvus already
   use — see decision 1), give nodes their real Cypher label and edges their real Cypher rel-type
   (decision 2), and keep the load idempotent (`MERGE`, unchanged).
2. **Adds the read seam + tools.** `GraphStore` gains `neighbors`/`document_structure`/
   `reading_order` (mockable — `FakeGraphStore` answers from the nodes/edges it was `load`ed with, so
   the default suite needs no Neo4j), a pipeline orchestrator that bridges a graph hit back to a page/
   bbox citation via the `provenance` artifact, and three routes: `POST /graph/neighbors`,
   `GET /documents/{doc_id}/graph/structure`, `GET /documents/{doc_id}/graph/reading-order`.

Same shape as every ingestion SP and RT-1: pure-core-first (the one genuinely pure addition is a
node-ordering algorithm reused by both the real and fake store), mockable seams, four green gates.

## 2. Scope decisions (autonomous; documented)

1. **Id namespacing contract: `{doc_id}:{bare_id}`.** `GraphLoadStage` — not `build_graph`, which
   stays untouched and still emits bare `doc`/`s{i}`/`s{i}p{j}` (its existing unit tests in
   `test_build_graph.py` keep passing unmodified) — rewrites every node id to `f"{doc_id}:{id}"` and
   every edge's `source`/`target` to the same, immediately before calling `GraphStore.load`. This is
   the **unifying win**: a section node's id becomes `{doc_id}:s{i}`, byte-identical to a Milvus hit id
   (SP-14) and RT-1's `chunks[…].chunk_id` join key (`{doc_id}:{chunk_id}`, `chunk_id="s{i}"`). A
   future cross-store join (vector hit → graph neighbors → chunk text) needs no id translation for
   section nodes. `doc_id` itself is a UUID4 string (`intake_document` — see
   `fdi.pipeline.intake`) and is guaranteed not to contain `:`, so splitting a namespaced id on the
   *first* `:` deterministically recovers `(doc_id, bare_id)` — used by the provenance bridge
   (decision 4) and nowhere else (bare ids never leave `GraphLoadStage`'s input into the store).
2. **Real Cypher labels/rel-types, via a validated allowlist — not string interpolation of the raw
   `type` field.** Cypher does not allow parameterized labels or relationship types (`MERGE (n:$label
   ...)` is a syntax error, not a bind), so the label/rel-type MUST be built into the query text. Doing
   that from an *unvalidated* `node["type"]` would let a malformed or attacker-influenced artifact
   inject arbitrary Cypher via the label position (e.g. `type = "X {a:1}) DETACH DELETE (n"`-shaped
   strings are exactly what parameterized `$id`/`$label` protect against elsewhere in this query, and
   labels are the one place that protection doesn't reach). `Neo4jGraphStore` therefore maps through a
   closed, private allowlist:
   ```python
   _NODE_LABELS = {"document": "Document", "section": "Section", "paragraph": "Paragraph"}
   _EDGE_REL_TYPES = {"CONTAINS": "CONTAINS", "READING_ORDER": "READING_ORDER"}
   ```
   Only the two dicts' *values* (fixed literals, never `type`'s value directly) ever reach an f-string;
   an unknown `type` raises `ValueError` rather than falling back to a generic label — a `type` outside
   this closed set means `build_graph` changed shape and the loader needs updating, not a silent
   `:Node`. The `type`/`label` *properties* stay on the node too (decision 3) so the store's own read
   methods never need `labels(n)` gymnastics to recover them.
3. **`doc_id` becomes a node property, in addition to the namespaced id.** Every namespaced node also
   gets `doc_id: <bare doc_id>` set via Cypher `SET`. Without it, `document_structure(doc_id)`/
   `reading_order(doc_id)` would have to `WHERE n.id STARTS WITH $doc_id + ':'` — a full label-scan
   string-prefix filter with no index, and one that breaks the moment any bare id itself contains a
   colon. `{doc_id: $doc_id}` as part of a `MATCH` pattern is an exact-equality property filter,
   cheap to index later (`CREATE CONSTRAINT ... FOR (n:Section) REQUIRE (n.doc_id) IS NOT NULL` is a
   deferred perf follow-up, not required for correctness at this corpus's scale — §6). `edges` do NOT
   get a `doc_id` property — an edge's two endpoints already carry it, and RT-5's graph has no
   cross-document edges to disambiguate.
4. **Provenance keys on BARE ids; the graph→provenance bridge strips the `{doc_id}:` prefix.** `SP-12`
   was built before namespacing existed and is untouched by RT-5 — `node_provenance[…].node_id` is
   still `"doc"`/`"s{i}"`/`"s{i}p{j}"`. `fdi.pipeline.graphquery` (§3.4) is the one place that knows
   both id shapes: it splits a namespaced node id on the first `:` to get `(doc_id, bare_id)`, reads
   that document's `provenance` artifact ONCE (batched, same "read once per doc" posture as RT-1's
   `resolve_chunks`), and looks up `bare_id` in it. A node with no provenance match degrades its
   citation to `page: None, bounding_regions: None` — never omits the node, never raises (same
   posture as `build_provenance`'s own degrade rules).
5. **Read-method seam: extend `GraphStore` (protocol + real + fake), not a new module.** `neighbors`,
   `document_structure`, `reading_order` join `load` on the existing protocol
   (`fdi.pipeline.graphstore.GraphStore`) — one seam for "the thing that knows how to talk to a graph
   store," matching `VectorStore`'s `upsert`+`search` shape (SP-14). All three are **sync**, matching
   `load`'s existing sync signature (the `neo4j` package's driver is blocking; `GraphLoadStage.run`
   already calls `store.load(...)` unawaited inside an `async def` — the established pattern in this
   codebase, not a new one RT-5 introduces). `FakeGraphStore` answers every read method purely from the
   `loaded_nodes`/`loaded_edges` lists it already accumulates across `load()` calls — no new state,
   no live Neo4j anywhere in the default suite.
6. **A pure `order_reading_chain` helper lives in `fdi.core.graph`, reused by BOTH stores.**
   `document_structure`/`reading_order` need sections (and, nested, paragraphs) in reading order, but
   node insertion order is not reliable (Neo4j returns rows in whatever order the query planner picks;
   `FakeGraphStore`'s accumulated lists reflect `load()` call order, not graph order) and **id order is
   actively wrong** — ids are `s{i}`/`s{i}p{j}` strings, and `"s10" < "s2"` lexicographically. The
   correct order is the `READING_ORDER` edge chain `build_graph` already encodes. Walking that chain is
   a pure graph algorithm with no I/O — it belongs in `core`, and both `platform.graphstore.
  Neo4jGraphStore` (order Python-side after three simple, unordered Cypher fetches — see §3.2) and
   `pipeline.graphstore.FakeGraphStore` (order Python-side over its in-memory lists) call the same
   function, so the ordering logic is written and tested exactly once. Layering allows this: `core` has
   no dependents to violate (everything may import it), and both `platform` and `pipeline` may import
   `core`.
7. **Route shapes: `POST /graph/neighbors` (body), `GET /documents/{doc_id}/graph/structure` and
   `GET /documents/{doc_id}/graph/reading-order` (path param, no body).** `document_structure`/
   `reading_order` are inherently document-scoped reads with no other parameters — matching this
   codebase's existing convention for that shape (`GET /documents/{doc_id}/stages`,
   `GET /documents/{doc_id}/artifacts/{stage}`), so they get a path param and a plain `GET`, not a
   POST body carrying a single `doc_id` field. `neighbors` takes an arbitrary (already fully-namespaced)
   node id with no natural place in a REST path under `/documents/{doc_id}/...` (the node itself, not
   the document, is the addressed resource, and a node id containing `:` is awkward as a path segment
   without a second layer of encoding) — it follows the existing "corpus-level tool" POST convention
   already established by `POST /search` and `POST /entities/resolve`. All three are exposed for RT-8
   to later register as LLM function tools (roadmap §"What RT-1 shipped" framing) — POST+JSON-body is
   also the natural shape a function-tool call arrives in, which `neighbors`' POST already matches and
   the two GETs will need a thin wrapper for at that point (deferred, not RT-5's problem — §6).
8. **Citations are attached by the orchestrator, not the route.** Every node RT-5 returns (a
   `neighbors` hit, a `document_structure` section/paragraph, a `reading_order` section) carries a
   `citation: {doc_id, doc_key, version, heading, page, bounding_regions}` — `doc_key`/`version` via one
   `RegistryRepository.get_document(doc_id)` Postgres lookup, `page`/`bounding_regions` via the
   provenance bridge (decision 4), `heading` is the node's own `label`. Because graph edges never cross
   documents (CONTAINS/READING_ORDER are built per-document by `build_graph`; nothing in this slice
   creates a cross-document edge), every node returned by one `neighbors`/`document_structure`/
   `reading_order` call belongs to the SAME `doc_id`, so the orchestrator does the Postgres lookup and
   the provenance-artifact read **once per call**, not once per node — same "read once per document"
   discipline as RT-1's `resolve_chunks`.
9. **No raw-Cypher escape hatch.** RT-5 does not add a `POST /graph/cypher`-shaped route. The three
   read methods are the entire sanctioned graph surface; exposing raw Cypher would both bypass the
   label/rel-type allowlist's injection protection (decision 2) and let a client run arbitrary
   mutations against production Neo4j through what is supposed to be a read-only retrieval tool.
   **Flagged as a fork a human should decide explicitly** if a real analyst need for ad-hoc graph
   queries shows up later (an admin-only, separately-authorized route is the shape that would take,
   not a widening of this one) — not resolved here, just kept out of scope.
10. **Stale pre-fix nodes are not migrated.** `MERGE` only adds/updates, never deletes — a Neo4j
    instance that already ran `graph_load` against the OLD unnamespaced loader has bare-id nodes
    (`id="doc"`, `id="s0"`, …, label `:Node`) sitting in it, reflecting whichever document loaded LAST
    under the old scheme. Re-running `graph_load` for existing documents (forced by the version bump,
    decision 12) MERGEs the NEW namespaced nodes/edges (`{doc_id}:doc`, etc.) alongside those stale
    ones — it does not touch or remove them, because their ids no longer match anything the new loader
    ever constructs. **Flagged as a fork for whoever owns the Neo4j deployment**: either start from a
    fresh Neo4j volume (simplest — this is a walking-skeleton project with no production corpus yet),
    or run a one-time manual cleanup (`MATCH (n:Node) DETACH DELETE n`) before/after re-running
    `graph_load` across the corpus. RT-5 does not build an automated migration for this — no route, no
    stage, no script — because there is no evidence yet that a real corpus was ever loaded under the
    broken scheme; building migration tooling against a hypothetical is exactly the kind of
    over-engineering this project's discipline avoids (§6 makes this explicit as deferred).
11. **`FDI_` config: none new.** RT-5 reuses `FDI_NEO4J_URI`/`FDI_NEO4J_USER`/`FDI_NEO4J_PASSWORD`
    (already in `Settings`, SP-9b) unchanged. No new environment variable, no `Settings` field.
12. **`GraphLoadStage.version` bumps `1 → 2`.** The namespacing change alters `GraphLoadStage`'s
    output for every document even though its `deps` (`("graph",)`) and the `graph` artifact itself are
    unchanged — a version bump is the only way SP-2's content-addressed cache re-runs the load (new
    `input_hash` from `derived_input_hash([...,  stage_version])`, same mechanism RT-1's `IndexStage`
    v1→v2 bump used). Existing documents' `graph_load` re-runs automatically on next trigger —
    intended, not a regression (decision 10 covers what re-running does and does not clean up).

## 3. Architecture

### 3.1 `fdi.core.graph` — add `order_reading_chain` (pure)

```python
def order_reading_chain(
    nodes: Sequence[Mapping[str, object]], edges: Sequence[Mapping[str, object]]
) -> list[dict[str, object]]:
    """Order `nodes` by walking their READING_ORDER edges from the chain head
    (the node among `nodes` with no incoming READING_ORDER edge from another
    node in `nodes`) to the tail.

    Pure, defensive: an edge whose type isn't READING_ORDER, or whose source/
    target isn't in `nodes`, is ignored. Multiple disconnected chains (should
    not occur for a single document's sections/paragraphs, but not assumed)
    are each walked and concatenated, heads sorted by id for determinism. A
    cycle or a dangling `next` stops that chain's walk (visited-set guard)
    rather than looping forever; any node never reached this way is appended
    in id order at the end -- never silently dropped. Never raises.
    """
```
Used by `Neo4jGraphStore.document_structure`/`reading_order` (over rows fetched by simple, unordered
Cypher) and `FakeGraphStore.document_structure`/`reading_order` (over its in-memory `loaded_nodes`/
`loaded_edges`) — see §3.2/§3.3. `build_graph` itself is unchanged.

### 3.2 `fdi.pipeline.graphstore` — `GraphStore` protocol + `FakeGraphStore` v2

```python
@runtime_checkable
class GraphStore(Protocol):
    def load(self, nodes, edges) -> dict[str, object]: ...
    def neighbors(self, node_id: str) -> list[dict[str, object]]: ...
    def document_structure(self, doc_id: str) -> dict[str, object]: ...
    def reading_order(self, doc_id: str) -> list[dict[str, object]]: ...
```
- `neighbors(node_id) -> [{"node": {"id","type","label","doc_id"}, "relationship": "CONTAINS"|
  "READING_ORDER", "direction": "out"|"in"}, ...]` — every edge touching `node_id`, both directions,
  unordered (no chain to walk for an arbitrary node's neighbor set).
- `document_structure(doc_id) -> {"document": {"id","type","label"} | None, "sections": [{"id","type",
  "label","paragraphs": [{"id","type","label"}, ...]}, ...]}` — sections and each section's paragraphs
  in `READING_ORDER` order (via `order_reading_chain`).
- `reading_order(doc_id) -> [{"id","type","label"}, ...]` — just the ordered sections (the same list
  `document_structure` would produce per-section, without the paragraph nesting) — a standalone tool
  for "what's the outline" without materializing every paragraph.

`FakeGraphStore.load` is UNCHANGED (still just accumulates `nodes`/`edges` into two lists and counts
calls — RT-5 does not touch its cache-hit-detection role in `test_graphload_stage.py`). The three new
methods are pure Python over those same two lists:
```python
def neighbors(self, node_id: str) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for e in self.loaded_edges:
        if e.get("source") == node_id and (m := self._node(e.get("target"))) is not None:
            out.append({"node": m, "relationship": e.get("type"), "direction": "out"})
        elif e.get("target") == node_id and (m := self._node(e.get("source"))) is not None:
            out.append({"node": m, "relationship": e.get("type"), "direction": "in"})
    return out

def _node(self, node_id: object) -> dict[str, object] | None:
    return next((dict(n) for n in self.loaded_nodes if n.get("id") == node_id), None)

def document_structure(self, doc_id: str) -> dict[str, object]:
    scoped = [n for n in self.loaded_nodes if n.get("doc_id") == doc_id]
    ids = {n["id"] for n in scoped}
    scoped_edges = [e for e in self.loaded_edges if e.get("source") in ids and e.get("target") in ids]
    document = next((dict(n) for n in scoped if n.get("type") == "document"), None)
    sections = order_reading_chain(
        [n for n in scoped if n.get("type") == "section"],
        [e for e in scoped_edges if e.get("type") == "READING_ORDER"],
    )
    contains = [e for e in scoped_edges if e.get("type") == "CONTAINS"]
    para_ro = [e for e in scoped_edges if e.get("type") == "READING_ORDER"]
    section_views = []
    for s in sections:
        paras = [
            dict(n) for n in scoped
            if n.get("type") == "paragraph"
            and any(e.get("source") == s["id"] and e.get("target") == n.get("id") for e in contains)
        ]
        section_views.append({**s, "paragraphs": order_reading_chain(paras, para_ro)})
    return {"document": document, "sections": section_views}

def reading_order(self, doc_id: str) -> list[dict[str, object]]:
    return [{k: v for k, v in s.items() if k != "paragraphs"} for s in self.document_structure(doc_id)["sections"]]
```
(Executor note: the `document_structure` paragraph-membership scan above is `O(sections × paragraphs ×
contains)` — fine at fixture scale (a handful of nodes per test document); not a scale claim for a real
corpus, matching this fake's existing "correct, not approximate, small-scale" posture, same as
`FakeVectorStore.search`'s exact-cosine scan.)

### 3.3 `fdi.platform.graphstore` — `Neo4jGraphStore` v2

`_load_tx` (MERGE, unchanged idempotency) gains the label/rel-type allowlist (decision 2) and the
`doc_id` property (decision 3):
```python
_NODE_LABELS: Mapping[str, str] = {"document": "Document", "section": "Section", "paragraph": "Paragraph"}
_EDGE_REL_TYPES: Mapping[str, str] = {"CONTAINS": "CONTAINS", "READING_ORDER": "READING_ORDER"}

def _cypher_label(node_type: object) -> str:
    if not isinstance(node_type, str) or node_type not in _NODE_LABELS:
        raise ValueError(f"unknown graph node type: {node_type!r}; known: {sorted(_NODE_LABELS)}")
    return _NODE_LABELS[node_type]

def _cypher_rel_type(edge_type: object) -> str:
    if not isinstance(edge_type, str) or edge_type not in _EDGE_REL_TYPES:
        raise ValueError(f"unknown graph edge type: {edge_type!r}; known: {sorted(_EDGE_REL_TYPES)}")
    return _EDGE_REL_TYPES[edge_type]

@staticmethod
def _load_tx(tx, nodes, edges) -> tuple[int, int]:
    for node in nodes:
        label = _cypher_label(node.get("type"))
        tx.run(
            f"MERGE (n:{label} {{id: $id}}) SET n.type = $type, n.label = $label, n.doc_id = $doc_id",
            id=node["id"], type=node["type"], label=node["label"], doc_id=node.get("doc_id"),
        )
    for edge in edges:
        rel = _cypher_rel_type(edge.get("type"))
        tx.run(
            f"MATCH (a {{id: $source}}), (b {{id: $target}}) MERGE (a)-[r:{rel}]->(b) SET r.type = $type",
            source=edge["source"], target=edge["target"], type=edge["type"],
        )
    return len(nodes), len(edges)
```
(`label`/`rel` are always one of the two dicts' fixed values by the time they reach the f-string —
never `node.get("type")`/`edge.get("type")` directly — the allowlist call is what makes the
interpolation safe, not the interpolation itself. `MATCH (a {id: $source})` deliberately carries no
label — ids are globally unique post-namespacing, so an unlabeled property match is correct; a
label-scan index (`CREATE CONSTRAINT ... FOR (n:Section) REQUIRE n.id IS UNIQUE`, one per label) is a
deferred perf follow-up, §6.)

Read methods (each `with self._driver.session() as session: return session.execute_read(self._X_tx,
...)`):
```python
@staticmethod
def _neighbors_tx(tx, node_id: str) -> list[dict[str, object]]:
    rows = tx.run(
        "MATCH (n {id: $id})-[r]->(m) "
        "RETURN type(r) AS relationship, 'out' AS direction, "
        "m.id AS id, m.type AS type, m.label AS label, m.doc_id AS doc_id "
        "UNION "
        "MATCH (n {id: $id})<-[r]-(m) "
        "RETURN type(r) AS relationship, 'in' AS direction, "
        "m.id AS id, m.type AS type, m.label AS label, m.doc_id AS doc_id",
        id=node_id,
    )
    return [
        {"node": {"id": r["id"], "type": r["type"], "label": r["label"], "doc_id": r["doc_id"]},
         "relationship": r["relationship"], "direction": r["direction"]}
        for r in rows
    ]
```
`type(r)` reads the REAL Cypher rel-type directly (`CONTAINS`/`READING_ORDER`) — the value RT-5 exists
to make meaningful; the `r.type` property is redundant with it post-fix but is kept set (unchanged from
v1) for consistency with the node-side `type` property and to keep the MERGE match stable if a future
edge ever needs a second discriminating property.

`document_structure`/`reading_order` share a private `_sections_and_reading_order(tx, doc_id)` that runs
three unordered, unparameterized-label-scan Cypher reads and orders the result Python-side via
`order_reading_chain` (decision 6):
```python
@staticmethod
def _sections_and_reading_order(tx, doc_id: str) -> list[dict[str, object]]:
    sections = [{"id": r["id"], "type": "section", "label": r["label"], "doc_id": doc_id}
                for r in tx.run("MATCH (s:Section {doc_id: $doc_id}) RETURN s.id AS id, s.label AS label", doc_id=doc_id)]
    edges = [{"source": r["source"], "target": r["target"], "type": "READING_ORDER"}
             for r in tx.run(
                 "MATCH (a:Section {doc_id: $doc_id})-[:READING_ORDER]->(b:Section {doc_id: $doc_id}) "
                 "RETURN a.id AS source, b.id AS target", doc_id=doc_id)]
    return order_reading_chain(sections, edges)

@staticmethod
def _document_structure_tx(tx, doc_id: str) -> dict[str, object]:
    doc_row = tx.run("MATCH (d:Document {doc_id: $doc_id}) RETURN d.id AS id, d.label AS label", doc_id=doc_id).single()
    document = (
        {"id": doc_row["id"], "type": "document", "label": doc_row["label"], "doc_id": doc_id}
        if doc_row else None
    )
    ordered_sections = Neo4jGraphStore._sections_and_reading_order(tx, doc_id)
    paragraphs = [{"id": r["id"], "type": "paragraph", "label": r["label"], "section_id": r["section_id"]}
                  for r in tx.run(
                      "MATCH (s:Section {doc_id: $doc_id})-[:CONTAINS]->(p:Paragraph {doc_id: $doc_id}) "
                      "RETURN p.id AS id, p.label AS label, s.id AS section_id", doc_id=doc_id)]
    para_edges = [{"source": r["source"], "target": r["target"], "type": "READING_ORDER"}
                  for r in tx.run(
                      "MATCH (a:Paragraph {doc_id: $doc_id})-[:READING_ORDER]->(b:Paragraph {doc_id: $doc_id}) "
                      "RETURN a.id AS source, b.id AS target", doc_id=doc_id)]
    by_section: dict[str, list[dict[str, object]]] = {}
    for p in paragraphs:
        by_section.setdefault(str(p["section_id"]), []).append(
            {"id": p["id"], "type": p["type"], "label": p["label"], "doc_id": doc_id}
        )
    sections = [
        {**s, "paragraphs": order_reading_chain(by_section.get(str(s["id"]), []), para_edges)}
        for s in ordered_sections
    ]
    return {"document": document, "sections": sections}

@staticmethod
def _reading_order_tx(tx, doc_id: str) -> list[dict[str, object]]:
    return Neo4jGraphStore._sections_and_reading_order(tx, doc_id)
```
(Every node dict `document_structure`/`reading_order` build carries `"doc_id"` — set from the
function's own `doc_id` argument, not fetched via Cypher, since it is already known and identical for
every row a single call returns. This is deliberate: `FakeGraphStore`'s equivalent methods return
`dict(n)` copies of the ORIGINAL loaded node — which already has `"doc_id"` set by `GraphLoadStage` —
so without this, the real and fake stores would disagree on which keys a section/paragraph/document
node dict carries. `neighbors`' node dicts already carried `doc_id` via `m.doc_id AS doc_id` from the
start — this closes the same gap for the other two read methods.)

### 3.4 `fdi.pipeline.graphload` — `GraphLoadStage` v2

```python
class GraphLoadStage:
    name = "graph_load"
    version = 2  # namespacing fix -- forces every existing document to reload
    deps: tuple[str, ...] = ("graph",)

    async def run(self, ctx: StageContext) -> object:
        graph = cast(Mapping[str, object], await ctx.read("graph"))
        store = cast(GraphStore, ctx.services["graph_store"])
        nodes = [_namespace_node(ctx.doc_id, n) for n in _list_of_dicts(graph.get("nodes"))]
        edges = [_namespace_edge(ctx.doc_id, e) for e in _list_of_dicts(graph.get("edges"))]
        receipt = store.load(nodes, edges)
        return {"schema_version": 1, "loaded_nodes": receipt["loaded_nodes"], "loaded_edges": receipt["loaded_edges"]}


def _namespace_node(doc_id: str, node: Mapping[str, object]) -> dict[str, object]:
    return {"id": f"{doc_id}:{node.get('id')}", "type": node.get("type"), "label": node.get("label"), "doc_id": doc_id}


def _namespace_edge(doc_id: str, edge: Mapping[str, object]) -> dict[str, object]:
    return {"source": f"{doc_id}:{edge.get('source')}", "target": f"{doc_id}:{edge.get('target')}", "type": edge.get("type")}
```
(`_list_of_dicts` is the existing `isinstance(x, list)` / `isinstance(item, dict)` filter already
inlined in v1 — factored into a tiny helper since it is now used twice. This mirrors `IndexStage`'s own
`f"{ctx.doc_id}:{chunk['chunk_id']}"` id-building, which likewise lives directly in the stage file, not
in `core` — string formatting tightly coupled to one stage's specific job is a pipeline-layer concern,
not a shared pure algorithm.) The `graph_load` artifact's own shape (`{schema_version, loaded_nodes,
loaded_edges}`) is UNCHANGED — namespacing changes node/edge CONTENT, not the receipt's shape or the
counts within it.

### 3.5 `fdi.pipeline.graphquery` (NEW — orchestrator)

A new module, not folded into `fdi.pipeline.retrieval`: `retrieval.py` is the RT-1 chunk-resolution
seam (`resolve_chunk`/`resolve_chunks`, consumed by RT-2…RT-4/RT-8's text-returning tools) and imports
no `GraphStore` — it has no reason to grow a graph dependency, and its existing consumers should not
have to pull in graph-store types transitively. `graphquery.py` is the pipeline-layer home for
everything that needs BOTH a `GraphStore` (graph shape) and the `provenance`/document-catalog reads
(citations) — same "imports platform (repo + store) + core, called by a route" shape as
`fdi.pipeline.crossdoc.resolve_entities` and `fdi.pipeline.retrieval.resolve_chunk`.

```python
async def neighbors(*, session: AsyncSession, store: ArtifactStore, graph_store: GraphStore, node_id: str) -> dict[str, object]:
    """{"schema_version": 1, "node_id": node_id, "neighbors": [{"node": <cited node view>, "relationship", "direction"}, ...]}"""

async def document_structure(*, session: AsyncSession, store: ArtifactStore, graph_store: GraphStore, doc_id: str) -> dict[str, object]:
    """{"schema_version": 1, "doc_id": doc_id, "document": <cited node view> | None, "sections": [<cited node view w/ paragraphs>, ...]}"""

async def reading_order(*, session: AsyncSession, store: ArtifactStore, graph_store: GraphStore, doc_id: str) -> dict[str, object]:
    """{"schema_version": 1, "doc_id": doc_id, "sections": [<cited node view>, ...]}"""
```
Shared internals:
```python
async def _read_provenance(repo: RegistryRepository, store: ArtifactStore, doc_id: str) -> dict[str, dict[str, object]]:
    """bare node_id -> {"page": int|None, "bounding_regions": list|None}, read ONCE per doc_id."""
    run = await repo.latest_run(doc_id, "provenance")
    if run is None or run.artifact_key is None:
        return {}
    body = cast(Mapping[str, object], await store.get_json(run.artifact_key))
    node_prov = body.get("node_provenance")
    out: dict[str, dict[str, object]] = {}
    if isinstance(node_prov, list):
        for rec in node_prov:
            if isinstance(rec, Mapping) and isinstance(rec.get("node_id"), str):
                out[rec["node_id"]] = {"page": rec.get("page"), "bounding_regions": rec.get("bounding_regions")}
    return out


def _split(node_id: str) -> tuple[str, str] | None:
    """namespaced id -> (doc_id, bare_id); None if malformed (no ':')."""
    if ":" not in node_id:
        return None
    doc_id, bare_id = node_id.split(":", 1)
    return doc_id, bare_id


def _cite(node: Mapping[str, object], doc, prov: Mapping[str, dict[str, object]]) -> dict[str, object]:
    """Attach a citation to one node view. `doc` is the Document row (or None);
    `prov` is THIS node's own doc_id's provenance map -- a neighbor from a
    different doc_id (never happens today; graphs have no cross-doc edges,
    but defensively checked) degrades page/bounding_regions to None rather
    than borrowing another document's provenance."""
    split = _split(str(node.get("id", "")))
    bare_id = split[1] if split else None
    entry = prov.get(bare_id, {}) if bare_id is not None else {}
    return {
        **node,
        "citation": {
            "doc_id": doc.id if doc else None,
            "doc_key": doc.doc_key if doc else None,
            "version": doc.version if doc else None,
            "heading": node.get("label"),
            "page": entry.get("page"),
            "bounding_regions": entry.get("bounding_regions"),
        },
    }
```
`neighbors` extracts `doc_id` from the QUERIED node's id (`_split(node_id)`), reads that one document's
`Document` row + provenance map once, and cites every returned neighbor against them (decision 8's
"every node in one call shares a `doc_id`" invariant — a malformed `node_id` with no `:` returns an
empty neighbor list rather than raising, matching `GraphStore.neighbors`'s own "unknown id -> []"
degrade). `document_structure`/`reading_order` take `doc_id` directly (from the route's path param) and
do the same one-time reads before citing the document/section/paragraph nodes `GraphStore` returned.

## 4. Error handling

- `GraphLoadStage`: a non-`dict` node/edge in the `graph` artifact is already filtered before
  namespacing (unchanged v1 behaviour, `_list_of_dicts`); namespacing itself never raises (string
  formatting over `.get()` results, `None` becomes the literal substring `"None"` in the vanishingly
  unlikely case of a malformed `id` — matching this codebase's existing "degrade, don't special-case"
  posture rather than adding a new validation path for input `build_graph` never actually produces).
- `Neo4jGraphStore._load_tx`: an unknown node `type`/edge `type` (outside the two-entry allowlists)
  raises `ValueError` — a real bug upstream (`build_graph`'s type vocabulary changed without this
  module being updated), not a runtime environment fact to degrade past. Matches `Settings._reject_
  unknown_fdi_env_vars`'s "fail loud on a closed-set violation" precedent.
- `Neo4jGraphStore.neighbors`/`document_structure`/`reading_order`: an unknown `node_id`/`doc_id` (no
  matching rows) returns `[]`/`{"document": None, "sections": []}`/`[]` respectively — a graph query
  over a corpus that never had that id is "no results," not an error, matching `MilvusVectorStore.
  search`'s "nothing indexed yet -> []" precedent (SP-14 design §2.8).
- `FakeGraphStore`'s three read methods mirror the same "unknown -> empty" contract exactly (design
  spec sec 2.8's "fake and real must agree" precedent applies here too).
- `fdi.pipeline.graphquery`: a `node_id` with no `:` (malformed) → `neighbors` returns `{"schema_
  version": 1, "node_id": node_id, "neighbors": []}` rather than raising. A `doc_id` with no `provenance`
  artifact, or a node with no matching provenance record → that node's citation `page`/
  `bounding_regions` are `None`, never omitted keys. A `doc_id` with no `Document` row (should not
  happen for a graph hit reached via a real document, but not assumed) → citation `doc_key`/`version`
  are `None` rather than raising.
- `order_reading_chain`: never raises (§3.1); a cycle or a dangling edge stops that chain's walk, a
  never-reached node is appended in id order rather than dropped.
- Routes: `graph_store is None` → `503` with a `FDI_NEO4J_URI`-naming detail message, same shape as
  `/search`'s `503` (checked FIRST, before any Postgres/document-existence lookup — an unconfigured
  backend should fail fast without spending a DB round-trip on a check that is moot regardless of its
  answer, same ordering `/search` uses). For the two document-scoped GETs, `document_structure`/
  `reading_order` returning `{"document": None, "sections": []}`/`[]` for an unknown `doc_id` is NOT
  turned into a 404 by the route — `RegistryRepository.get_document(doc_id) is None` is checked
  separately (mirroring `/documents/{doc_id}/stages`) to distinguish "no such document" (404) from "a
  real document with an empty/not-yet-loaded graph" (200 with empty `sections`).

## 5. Testing

- **Unit** (pure, no Docker), `backend/tests/unit/test_graph_reading_order.py`: single linear chain
  orders correctly; empty `nodes`/`edges`; a node with no `READING_ORDER` edges at all (single-element
  chain); two disconnected chains (both walked, heads sorted); a cycle (visited-set guard, no infinite
  loop, finite output); an edge referencing an id not in `nodes` (ignored); an edge of a non-
  `READING_ORDER` type mixed in (ignored). `order_reading_chain([], [])  == []`.
- **Unit**, `backend/tests/unit/test_graphstore.py` (extend): `FakeGraphStore.neighbors` returns both
  out- and in-edges with correct `direction`; unknown `node_id` → `[]`. `FakeGraphStore.document_
  structure` over a 2-section/2-paragraph-each fixture returns sections AND their nested paragraphs in
  `READING_ORDER` order (constructed out of insertion order on purpose, to prove ordering is real, not
  accidental); a `doc_id` never `load()`ed → `{"document": None, "sections": []}`. `FakeGraphStore.
  reading_order` returns the same sections, without a `paragraphs` key. Existing `load()`/`calls`
  accumulation tests untouched.
- **Unit**, `backend/tests/unit/test_neo4j_graphstore.py` (extend `_load_tx` tests): the MERGE query
  text now contains the real label (`MERGE (n:Section {id: $id})` for a `type="section"` node, not
  `MERGE (n:Node ...)`); the edge MERGE now contains the real rel-type (`MERGE (a)-[r:CONTAINS]->(b)`,
  not `MERGE (a)-[r:REL ...]->(b)`); `SET` params now include `doc_id`. An unknown node `type`
  (`"widget"`) raises `ValueError` before any `tx.run` for that node. An unknown edge `type` likewise.
- **Unit**, `backend/tests/unit/test_neo4j_graphstore_reads.py` (new, same fake-driver-monkeypatch
  technique as the existing file — a `_FakeTx.run` that returns a scripted result set per query
  keyword, not a live driver): `neighbors` builds `{"node", "relationship", "direction"}` records from
  scripted UNION rows; `document_structure`/`reading_order` order sections via a scripted
  `READING_ORDER` edge list and assert the SAME `order_reading_chain` semantics as the core unit tests
  (out-of-insertion-order fixture, in-order result).
- **Integration** (testcontainers + fakes), `backend/tests/integration/test_graphload_stage.py`
  (extend the existing file): the existing `test_graph_load_chains_off_graph_and_caches` test's
  assertions on `fake_store.loaded_nodes`/`loaded_edges` update to expect namespaced ids
  (`f"{doc.id}:doc"`, etc.) and a `doc_id` key on each node; loaded/cached counts (3 nodes, 2 edges)
  are UNCHANGED (namespacing changes id values, not counts) — the `key = artifact_key(doc.id,
  "graph_load", 2, ...)` read (note **version bumps to 2** in this test) still returns `{"schema_
  version": 1, "loaded_nodes": 3, "loaded_edges": 2}`. **New test**: run `graph_load` for TWO different
  documents through the SAME `FakeGraphStore`; assert `fake_store.loaded_nodes` has 2× the per-document
  node count with NO id collisions (`len({n["id"] for n in fake_store.loaded_nodes}) == len(fake_store.
  loaded_nodes)`) — the regression test that proves decision 1 actually fixes the bug this sub-project
  exists for.
- **Opt-in real-Neo4j**, `backend/tests/neo4j/test_real_graph_load.py` (extend, same `pytestmark`
  skip-unless-configured gate): the existing `test_real_load_persists_nodes_and_edges` already
  namespaces its OWN fixture ids by hand (`f"{doc_id}-doc"`) — update its Cypher read-back assertion
  from `MATCH (n:Node {id: $id})` to `MATCH (n:Document {id: $id})` (proving the real label landed).
  **New test**: load two real documents' worth of nodes/edges through `Neo4jGraphStore.load` using
  RT-5's OWN namespacing shape (`f"{doc_id}:doc"`, `doc_id` property set) and assert BOTH documents'
  `doc` nodes exist independently (`MATCH (n:Document {id: $id1})`/`{id: $id2}` both return exactly one
  row) — the live-Neo4j proof of the collision fix, alongside the `FakeGraphStore` regression test
  above. **New test**: `neighbors`/`document_structure`/`reading_order` against the real driver over a
  small loaded fixture, asserting the same shapes the unit tests assert against the fake driver.
- **Integration**, `backend/tests/integration/test_graphquery.py` (new): reuse `test_resolve_chunk.py`'s
  `processed_doc`-style fixture (intake + `run_stage` through `ocr → normalize → ast → semantic → graph
  → provenance`), ADD `graph_load` to the chain with a `FakeGraphStore` (so `graphquery`'s orchestrator
  functions have something loaded to read). Assert: `neighbors(node_id=f"{doc_id}:doc")` returns the
  namespaced `s0` section with `relationship="CONTAINS"`, `direction="out"`, and a citation carrying the
  real `page` from the fixture's provenance; `document_structure(doc_id)` returns sections in the
  fixture's authored order with nested, cited paragraphs; `reading_order(doc_id)` returns the same
  section order without paragraphs; an unknown `doc_id` → empty structure/order, no raise; a malformed
  `node_id` (no `:`) → empty neighbors, no raise.
- **Integration**, `backend/tests/integration/test_api.py` (extend): `POST /graph/neighbors` /
  `GET /documents/{doc_id}/graph/structure` / `GET /documents/{doc_id}/graph/reading-order` each `503`
  when `graph_store` is unconfigured (mirror `test_search_503_when_vector_store_unconfigured`, checking
  `"graph_store"` appears in the `detail`). With `app.dependency_overrides[get_graph_store] = lambda:
  fake_graph_store` (mirroring the `get_vector_store`/`get_embedder` override pattern) and a document
  processed through `graph_load`: each route returns 200 with the expected cited shape.
  `GET /documents/{doc_id}/graph/structure` for a NONEXISTENT `doc_id` → 404 (checked after the 503
  guard, per §4).
- **Executor pre-check (verify, don't assume):** confirm `intake_document`'s generated `doc_id` (`str(
  uuid.uuid4())`, `fdi.pipeline.intake`) never contains `:` — the assumption decision 1's `_split`
  relies on — by reading `fdi/pipeline/intake.py`; document the finding in `graphquery.py`'s docstring.

## 6. Explicitly deferred (YAGNI)

- **A raw-Cypher route** — decision 9; kept out of scope, flagged as a fork for a human, not a TODO in
  this codebase.
- **Automated migration/cleanup of stale pre-fix `:Node`-labeled nodes** — decision 10; an operational
  note (fresh volume or a one-time manual Cypher), not a stage/route/script, for the reason given there.
- **Neo4j indexes/constraints on `id`/`doc_id`** (`CREATE CONSTRAINT ... IS UNIQUE`, `CREATE INDEX ...
  FOR (n:Section) ON (n.doc_id)`) — correctness does not depend on them at this corpus's scale; a
  perf follow-up once query latency is actually measured against a real corpus, not before.
- **Cross-document graph edges** (e.g. an entity or concept node linking two documents' graphs) — SP-9's
  `build_graph` produces a strictly per-document tree; RT-5 fixes that tree's storage, it does not
  introduce new edge types or a corpus-wide graph shape. (RT-7's cross-document/entity retrieval is the
  roadmap's home for anything cross-document.)
- **`neighbors` multi-hop traversal** (e.g. "neighbors of neighbors," a depth parameter) — one-hop only,
  matching the roadmap's stated tool name; a depth-N variant is a straightforward but unrequested
  extension of the same Cypher pattern, left for when a concrete need for it appears.
- **A dedicated provenance/citation HTTP route** (`GET /graph/nodes/{id}/provenance`) — the provenance
  bridge (`fdi.pipeline.graphquery`'s `_cite`) is exercised entirely through the three read tools, which
  already attach a citation to every node they return; a bare id→citation endpoint has no route-level
  purpose RT-5's three tools don't already cover.
- **GET-shaped/query-string variants of `POST /graph/neighbors`** — RT-8's function-tool registration
  (decision 7) is the reason a POST+body shape was chosen; revisit only if RT-8 actually needs
  something different, not preemptively.

## 7. Done criteria

1. `GraphLoadStage` (`version=2`, `deps=("graph",)`) namespaces every node id `{doc_id}:{bare_id}` and
   every edge's `source`/`target` the same way, sets a `doc_id` node property, before calling
   `GraphStore.load`; loading two different documents through the same store produces zero id
   collisions (integration test, §5); `build_graph` itself is unmodified.
2. `Neo4jGraphStore._load_tx` MERGEs nodes under their real Cypher label (`:Document`/`:Section`/
   `:Paragraph`, from a closed, validated allowlist — never raw string interpolation of `type`) and
   edges under their real Cypher rel-type (`:CONTAINS`/`:READING_ORDER`), sets `doc_id`; an unknown
   `type` raises `ValueError` rather than silently falling back to a generic label/rel-type. Load stays
   idempotent (`MERGE`, unchanged).
3. `GraphStore` (protocol, `Neo4jGraphStore`, `FakeGraphStore`) exposes `neighbors(node_id)`,
   `document_structure(doc_id)`, `reading_order(doc_id)` with the shapes in §3.2/§3.3; all three agree
   on the "unknown id/doc → empty result, never raise" contract between the real and fake store;
   `order_reading_chain` (`fdi.core.graph`, pure) is the single ordering algorithm both use.
4. `fdi.pipeline.graphquery.neighbors`/`document_structure`/`reading_order` attach a citation
   (`doc_id, doc_key, version, heading, page, bounding_regions`) to every returned node, reading the
   `provenance` artifact and the `Document` row ONCE per call (not per node); the bare-id lookup
   correctly strips the `{doc_id}:` prefix (decision 1's `_split`).
5. `POST /graph/neighbors`, `GET /documents/{doc_id}/graph/structure`, `GET /documents/{doc_id}/graph/
   reading-order` are registered, return 503 when `graph_store` is unconfigured (checked before any
   Postgres lookup), 404 for a nonexistent `doc_id` on the two document-scoped GETs, and 200 with the
   cited shape otherwise; `GraphStoreDep`/`get_graph_store` wired in `api/deps.py` + `api/app.py`
   (`app.state.graph_store = Neo4jGraphStore.from_settings(settings)`, mirroring `embedder`/
   `vector_store`) — this wiring DID NOT EXIST before RT-5 (only `fdi.workers.tasks` wired a
   `graph_store` service, for the `graph_load` STAGE, not for a route) and is added, not merely
   "noted," as part of this sub-project.
6. All four gates green (`ruff`, `mypy --strict`, `lint-imports` 1-kept/0-broken, full suite); the
   default suite (no `neo4j` marker) exercises every behavioural change via `FakeGraphStore`; the
   opt-in `neo4j`-marked tests (skipped without `FDI_NEO4J_URI`/`FDI_NEO4J_PASSWORD`) prove the same
   contracts against a real driver, including the cross-document collision fix (§5); the executor
   pre-check (§5, `doc_id` never contains `:`) is verified and documented; deferred items (§6 of this
   spec) remain out of scope; both flagged forks (raw Cypher, stale-node migration — decisions 9/10)
   are documented, not silently resolved; the retrieval roadmap's RT-5 row flips to done with a "what
   RT-5 shipped" handoff for RT-7/RT-8 (the `{doc_id}:s{i}` section-node identity now matching Milvus/
   RT-1, the `GraphStore` read seam, the citation shape).
