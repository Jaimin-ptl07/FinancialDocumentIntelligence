# RT-5 Graph Retrieval (+ corpus-graph fix) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the cross-document-broken Neo4j corpus graph (id namespacing, real Cypher labels/
rel-types), add mockable `GraphStore` read methods (`neighbors`, `document_structure`,
`reading_order`), a provenance-backed citation bridge, and three HTTP routes.

**Architecture:** `GraphLoadStage` v2 namespaces every node id `{doc_id}:{bare_id}` (aligning section
nodes with Milvus/RT-1's `{doc_id}:s{i}` identity) before calling `GraphStore.load`. `Neo4jGraphStore`
v2 MERGEs under real Cypher labels/rel-types via a validated allowlist and reads back via three new
methods; `FakeGraphStore` answers the same methods purely from its accumulated `loaded_nodes`/
`loaded_edges`. Both share one pure ordering algorithm, `fdi.core.graph.order_reading_chain`. A new
orchestrator, `fdi.pipeline.graphquery`, attaches citations (Postgres doc_key/version + the
`provenance` artifact, bare-id keyed) to every returned node. Three routes expose it, gated the same
way `/search` is (`503` when unconfigured).

**Tech Stack:** Python 3.13, `uv`, SQLAlchemy async, FastAPI, `neo4j` driver, pytest + testcontainers
(+ opt-in `neo4j` marker for a live instance).

## Global Constraints

- Layered contract `api | workers → pipeline → platform → core` (exhaustive), CI-enforced; stay "1
  kept, 0 broken". `fdi.core.graph` stays PURE (no I/O/config/clock/RNG); `order_reading_chain` reads
  only plain `Mapping`/`Sequence` arguments.
- `platform` may not import `pipeline` — `Neo4jGraphStore` structurally satisfies `GraphStore` without
  importing the protocol (existing pattern, unchanged).
- Determinism: `order_reading_chain` never depends on wall-clock/RNG/insertion order; only on
  `READING_ORDER` edges + a sorted-heads tie-break.
- Per-task gates before every commit: `uv run --directory backend ruff check .` ·
  `uv run --directory backend mypy --strict src` ·
  `PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter` · the task's pytest.
  Line length ≤ 100.
- TDD: failing test → run (see fail) → minimal impl → run (see pass) → commit.
- **No live Neo4j in the default suite.** Every behavioural assertion in Tasks 1–9 goes through
  `FakeGraphStore` or a monkeypatched fake `neo4j` driver. Only Task 6 (extends the existing opt-in
  `backend/tests/neo4j/test_real_graph_load.py`) touches a real instance, and only when
  `FDI_NEO4J_URI`/`FDI_NEO4J_PASSWORD` are set.

---

### Task 1: `fdi.core.graph.order_reading_chain` (pure)

**Files:**
- Modify: `backend/src/fdi/core/graph.py`
- Test: `backend/tests/unit/test_graph_reading_order.py` (new)

**Interfaces:**
- Produces: `order_reading_chain(nodes: Sequence[Mapping[str, object]], edges: Sequence[Mapping[str,
  object]]) -> list[dict[str, object]]`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_graph_reading_order.py
from fdi.core.graph import order_reading_chain


def _n(i: str) -> dict[str, object]:
    return {"id": i, "type": "section", "label": i}


def _ro(source: str, target: str) -> dict[str, object]:
    return {"source": source, "target": target, "type": "READING_ORDER"}


def test_orders_a_linear_chain() -> None:
    nodes = [_n("s1"), _n("s0"), _n("s2")]  # deliberately NOT in chain order
    edges = [_ro("s0", "s1"), _ro("s1", "s2")]
    assert [n["id"] for n in order_reading_chain(nodes, edges)] == ["s0", "s1", "s2"]


def test_empty_input() -> None:
    assert order_reading_chain([], []) == []


def test_single_node_no_edges() -> None:
    assert [n["id"] for n in order_reading_chain([_n("s0")], [])] == ["s0"]


def test_two_disconnected_chains_both_walked_heads_sorted() -> None:
    nodes = [_n("b0"), _n("b1"), _n("a0"), _n("a1")]
    edges = [_ro("b0", "b1"), _ro("a0", "a1")]
    # heads are "a0" and "b0" -- sorted -> "a" chain first
    assert [n["id"] for n in order_reading_chain(nodes, edges)] == ["a0", "a1", "b0", "b1"]


def test_cycle_does_not_infinite_loop() -> None:
    nodes = [_n("s0"), _n("s1")]
    edges = [_ro("s0", "s1"), _ro("s1", "s0")]  # a 2-cycle: no node lacks an incoming edge
    result = order_reading_chain(nodes, edges)
    assert {n["id"] for n in result} == {"s0", "s1"}
    assert len(result) == 2  # each node appears exactly once, walk terminates


def test_edge_referencing_unknown_id_is_ignored() -> None:
    nodes = [_n("s0")]
    edges = [_ro("s0", "ghost")]
    assert [n["id"] for n in order_reading_chain(nodes, edges)] == ["s0"]


def test_non_reading_order_edge_type_is_ignored() -> None:
    nodes = [_n("s0"), _n("s1")]
    edges = [{"source": "s0", "target": "s1", "type": "CONTAINS"}]
    # no READING_ORDER edge between them -> both are heads, sorted by id
    assert [n["id"] for n in order_reading_chain(nodes, edges)] == ["s0", "s1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_graph_reading_order.py -v`
Expected: FAIL — `ImportError: cannot import name 'order_reading_chain'`.

- [ ] **Step 3: Write the implementation**

Append to `backend/src/fdi/core/graph.py` (keep `build_graph` and its docstring untouched above this):

```python
from collections.abc import Sequence  # add to the existing `from collections.abc import Mapping` line


def order_reading_chain(
    nodes: Sequence[Mapping[str, object]], edges: Sequence[Mapping[str, object]]
) -> list[dict[str, object]]:
    """Order `nodes` by walking their READING_ORDER edges from the chain head
    (a node among `nodes` with no incoming READING_ORDER edge from another
    node in `nodes`) to the tail.

    Pure, defensive: an edge whose type isn't READING_ORDER, or whose source/
    target isn't in `nodes`, is ignored. Multiple disconnected chains are each
    walked and concatenated, heads sorted by id for determinism. A cycle or a
    dangling `next` stops that chain's walk (visited-set guard) rather than
    looping forever; any node never reached this way is appended in id order
    at the end -- never silently dropped. Never raises.
    """
    node_by_id: dict[str, dict[str, object]] = {}
    for n in nodes:
        node_id = n.get("id")
        if isinstance(node_id, str):
            node_by_id[node_id] = dict(n)

    next_of: dict[str, str] = {}
    has_incoming: set[str] = set()
    for e in edges:
        if e.get("type") != "READING_ORDER":
            continue
        source, target = e.get("source"), e.get("target")
        if (
            isinstance(source, str)
            and isinstance(target, str)
            and source in node_by_id
            and target in node_by_id
        ):
            next_of[source] = target
            has_incoming.add(target)

    heads = sorted(nid for nid in node_by_id if nid not in has_incoming)
    ordered: list[dict[str, object]] = []
    visited: set[str] = set()
    for head in heads:
        cur: str | None = head
        while cur is not None and cur not in visited:
            visited.add(cur)
            ordered.append(node_by_id[cur])
            cur = next_of.get(cur)

    for nid in sorted(node_by_id):
        if nid not in visited:
            ordered.append(node_by_id[nid])
            visited.add(nid)

    return ordered
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_graph_reading_order.py -v`
Expected: PASS (all 7).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
git add backend/src/fdi/core/graph.py backend/tests/unit/test_graph_reading_order.py
git commit -m "feat(core): graph.order_reading_chain -- pure READING_ORDER chain walk, shared by real+fake GraphStore"
```

---

### Task 2: `GraphStore` protocol + `FakeGraphStore` read methods

**Files:**
- Modify: `backend/src/fdi/pipeline/graphstore.py`
- Test: `backend/tests/unit/test_graphstore.py` (extend)

**Interfaces:**
- Consumes: `order_reading_chain` (Task 1).
- Produces: `GraphStore.neighbors/document_structure/reading_order`; `FakeGraphStore` implementations.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/unit/test_graphstore.py`:

```python
def _load_two_section_doc(fake: FakeGraphStore, doc_id: str) -> None:
    nodes = [
        {"id": f"{doc_id}:doc", "type": "document", "label": "ACME", "doc_id": doc_id},
        {"id": f"{doc_id}:s0", "type": "section", "label": "First", "doc_id": doc_id},
        {"id": f"{doc_id}:s1", "type": "section", "label": "Second", "doc_id": doc_id},
        {"id": f"{doc_id}:s0p0", "type": "paragraph", "label": "P0", "doc_id": doc_id},
        {"id": f"{doc_id}:s1p0", "type": "paragraph", "label": "P1", "doc_id": doc_id},
    ]
    edges = [
        {"source": f"{doc_id}:doc", "target": f"{doc_id}:s0", "type": "CONTAINS"},
        {"source": f"{doc_id}:doc", "target": f"{doc_id}:s1", "type": "CONTAINS"},
        {"source": f"{doc_id}:s0", "target": f"{doc_id}:s1", "type": "READING_ORDER"},
        {"source": f"{doc_id}:s0", "target": f"{doc_id}:s0p0", "type": "CONTAINS"},
        {"source": f"{doc_id}:s1", "target": f"{doc_id}:s1p0", "type": "CONTAINS"},
    ]
    fake.load(nodes, edges)


def test_fake_neighbors_returns_both_directions() -> None:
    fake = FakeGraphStore()
    _load_two_section_doc(fake, "d1")
    result = fake.neighbors("d1:s0")
    rels = {(r["node"]["id"], r["relationship"], r["direction"]) for r in result}
    assert ("d1:doc", "CONTAINS", "in") in rels
    assert ("d1:s1", "READING_ORDER", "out") in rels
    assert ("d1:s0p0", "CONTAINS", "out") in rels


def test_fake_neighbors_unknown_id_returns_empty() -> None:
    fake = FakeGraphStore()
    _load_two_section_doc(fake, "d1")
    assert fake.neighbors("d1:ghost") == []


def test_fake_document_structure_orders_sections_and_paragraphs() -> None:
    fake = FakeGraphStore()
    _load_two_section_doc(fake, "d1")
    out = fake.document_structure("d1")
    assert out["document"] == {"id": "d1:doc", "type": "document", "label": "ACME", "doc_id": "d1"}
    section_ids = [s["id"] for s in out["sections"]]
    assert section_ids == ["d1:s0", "d1:s1"]
    assert [p["id"] for p in out["sections"][0]["paragraphs"]] == ["d1:s0p0"]
    assert [p["id"] for p in out["sections"][1]["paragraphs"]] == ["d1:s1p0"]


def test_fake_document_structure_unknown_doc_id() -> None:
    fake = FakeGraphStore()
    _load_two_section_doc(fake, "d1")
    assert fake.document_structure("no-such-doc") == {"document": None, "sections": []}


def test_fake_reading_order_matches_document_structure_sections_without_paragraphs() -> None:
    fake = FakeGraphStore()
    _load_two_section_doc(fake, "d1")
    out = fake.reading_order("d1")
    assert [s["id"] for s in out] == ["d1:s0", "d1:s1"]
    assert all("paragraphs" not in s for s in out)


def test_fake_document_structure_scopes_by_doc_id_across_two_documents() -> None:
    fake = FakeGraphStore()
    _load_two_section_doc(fake, "d1")
    _load_two_section_doc(fake, "d2")
    out1 = fake.document_structure("d1")
    assert [s["id"] for s in out1["sections"]] == ["d1:s0", "d1:s1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_graphstore.py -v`
Expected: FAIL — `AttributeError: 'FakeGraphStore' object has no attribute 'neighbors'`.

- [ ] **Step 3: Write the implementation**

Replace `backend/src/fdi/pipeline/graphstore.py` in full:

```python
"""The GraphStore seam: `load(nodes, edges) -> {loaded_nodes, loaded_edges}` plus
three read methods (`neighbors`, `document_structure`, `reading_order`) every
graph retrieval tool (RT-5) calls. `Neo4jGraphStore` (platform, does I/O)
structurally satisfies this without importing it -- `platform` may not import
`pipeline`. `FakeGraphStore` lives here for tests: no live Neo4j call anywhere
in the default suite. Node ids are namespaced `{doc_id}:{bare_id}` by
`GraphLoadStage` before ever reaching `load` -- see `fdi.core.graph.
order_reading_chain` for the shared READING_ORDER-walk both stores use to
answer `document_structure`/`reading_order` correctly (id string order is
NOT reading order -- "s10" < "s2" lexicographically)."""

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from fdi.core.graph import order_reading_chain


@runtime_checkable
class GraphStore(Protocol):
    def load(
        self, nodes: list[Mapping[str, object]], edges: list[Mapping[str, object]]
    ) -> dict[str, object]: ...

    def neighbors(self, node_id: str) -> list[dict[str, object]]: ...

    def document_structure(self, doc_id: str) -> dict[str, object]: ...

    def reading_order(self, doc_id: str) -> list[dict[str, object]]: ...


class FakeGraphStore:
    def __init__(self) -> None:
        self.calls = 0
        self.loaded_nodes: list[Mapping[str, object]] = []
        self.loaded_edges: list[Mapping[str, object]] = []

    def load(
        self, nodes: list[Mapping[str, object]], edges: list[Mapping[str, object]]
    ) -> dict[str, object]:
        self.calls += 1
        self.loaded_nodes.extend(nodes)
        self.loaded_edges.extend(edges)
        return {"loaded_nodes": len(nodes), "loaded_edges": len(edges)}

    def _node(self, node_id: object) -> dict[str, object] | None:
        return next((dict(n) for n in self.loaded_nodes if n.get("id") == node_id), None)

    def neighbors(self, node_id: str) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for e in self.loaded_edges:
            if e.get("source") == node_id:
                m = self._node(e.get("target"))
                if m is not None:
                    out.append({"node": m, "relationship": e.get("type"), "direction": "out"})
            elif e.get("target") == node_id:
                m = self._node(e.get("source"))
                if m is not None:
                    out.append({"node": m, "relationship": e.get("type"), "direction": "in"})
        return out

    def document_structure(self, doc_id: str) -> dict[str, object]:
        scoped = [n for n in self.loaded_nodes if n.get("doc_id") == doc_id]
        ids = {n.get("id") for n in scoped}
        scoped_edges = [
            e for e in self.loaded_edges if e.get("source") in ids and e.get("target") in ids
        ]
        document = next((dict(n) for n in scoped if n.get("type") == "document"), None)
        section_edges = [e for e in scoped_edges if e.get("type") == "READING_ORDER"]
        sections = order_reading_chain(
            [n for n in scoped if n.get("type") == "section"], section_edges
        )
        contains = [e for e in scoped_edges if e.get("type") == "CONTAINS"]
        para_ro = [e for e in scoped_edges if e.get("type") == "READING_ORDER"]
        section_views: list[dict[str, object]] = []
        for s in sections:
            paras = [
                dict(n)
                for n in scoped
                if n.get("type") == "paragraph"
                and any(
                    e.get("source") == s["id"] and e.get("target") == n.get("id")
                    for e in contains
                )
            ]
            section_views.append({**s, "paragraphs": order_reading_chain(paras, para_ro)})
        return {"document": document, "sections": section_views}

    def reading_order(self, doc_id: str) -> list[dict[str, object]]:
        sections = self.document_structure(doc_id)["sections"]
        return [{k: v for k, v in s.items() if k != "paragraphs"} for s in sections]  # type: ignore[union-attr]
```

(If mypy objects to the `sections` typing on the last line, replace with an explicit
`cast(list[dict[str, object]], ...)` from `typing`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_graphstore.py -v`
Expected: PASS (all, old + new).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/pipeline/graphstore.py backend/tests/unit/test_graphstore.py
git commit -m "feat(pipeline): GraphStore.neighbors/document_structure/reading_order + FakeGraphStore impl"
```

---

### Task 3: `GraphLoadStage` v2 — namespace ids, add `doc_id` property

**Files:**
- Modify: `backend/src/fdi/pipeline/stages/graphload.py`
- Test: `backend/tests/integration/test_graphload_stage.py` (extend)

**Interfaces:**
- Produces: `GraphLoadStage` (`version=2`, `deps=("graph",)`), namespacing `_namespace_node`/
  `_namespace_edge`.

- [ ] **Step 1: Write the failing test**

Modify `backend/tests/integration/test_graphload_stage.py`: update the existing test's expectations
and add the cross-document collision regression test.

```python
# Replace the tail of test_graph_load_chains_off_graph_and_caches (from `gl_services = ...` on) with:
    gl_services = {"graph_store": fake_store}
    await run_stage(
        stage_name="graph_load", run_id=str(uuid.uuid4()), services=gl_services, **common
    )
    # Second run: content-addressed cache hit -- must NOT call the fake store twice.
    await run_stage(
        stage_name="graph_load", run_id=str(uuid.uuid4()), services=gl_services, **common
    )
    assert fake_store.calls == 1

    # Ids are namespaced `{doc_id}:{bare_id}`, and every node carries a doc_id property.
    node_ids = {n["id"] for n in fake_store.loaded_nodes}
    assert node_ids == {f"{doc.id}:doc", f"{doc.id}:s0", f"{doc.id}:s0p0"}
    assert all(n["doc_id"] == doc.id for n in fake_store.loaded_nodes)
    edge_pairs = {(e["source"], e["target"]) for e in fake_store.loaded_edges}
    assert edge_pairs == {(f"{doc.id}:doc", f"{doc.id}:s0"), (f"{doc.id}:s0", f"{doc.id}:s0p0")}

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    # version bumps 1 -> 2 for the namespacing fix
    key = artifact_key(doc.id, "graph_load", 2, derived_input_hash([facts["graph"].output_hash]))
    body = await store.get_json(key)
    assert body == {"schema_version": 1, "loaded_nodes": 3, "loaded_edges": 2}


async def test_graph_load_namespaces_ids_no_collision_across_documents(
    session_factory, store
) -> None:
    """The regression test RT-5 exists for: two documents loaded through the
    SAME GraphStore must not collide on id."""
    fake_ocr = FakeOcrClient(
        {
            "content": "ACME",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": [
                {"role": "title", "content": "ACME LTD"},
                {"role": "sectionHeading", "content": "Directors' Report"},
                {"content": "The board is pleased."},
            ],
            "tables": [],
        }
    )
    fake_store = FakeGraphStore()
    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(GraphStage())
    reg.register(GraphLoadStage())
    resolver = DagResolver(reg.all())

    doc_ids: list[str] = []
    for _ in range(2):
        doc = await intake_document(
            pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
            filename="a.pdf", doc_key=str(uuid.uuid4()), content_type="application/pdf",
            session_factory=session_factory, store=store,
        )
        doc_ids.append(doc.id)
        common = {
            "doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
            "store": store, "registry": reg, "resolver": resolver,
        }
        await run_stage(
            stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake_ocr}, **common
        )
        await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
        await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
        await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
        await run_stage(
            stage_name="graph_load", run_id=str(uuid.uuid4()),
            services={"graph_store": fake_store}, **common,
        )

    all_ids = [n["id"] for n in fake_store.loaded_nodes]
    assert len(all_ids) == len(set(all_ids))  # no collision
    for doc_id in doc_ids:
        assert f"{doc_id}:doc" in all_ids
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_graphload_stage.py -v`
Expected: FAIL — node ids still bare (`"doc"`, not `f"{doc.id}:doc"`); `artifact_key(..., 2, ...)`
does not resolve (stage still version 1).

- [ ] **Step 3: Implement v2**

Replace `backend/src/fdi/pipeline/stages/graphload.py` in full:

```python
"""GraphLoadStage: a DERIVED stage that projects the SP-9 graph artifact
(nodes/edges) into a Neo4j-shaped GraphStore via an injected client.
Auto-chains off GraphStage via `deps=("graph",)`. The artifact is a small
deterministic receipt (node/edge counts) -- Neo4j is the side effect and the
actual point.

RT-5 (version 1 -> 2): every node id and every edge's source/target is
namespaced `{doc_id}:{bare_id}` before reaching `GraphStore.load` -- the fix
for the pre-RT-5 bug where every document's graph reused the identical bare
ids ("doc", "s0", "s0p1", ...) and a second document's load collided onto
the first's (MERGE last-writer-wins). Section nodes' namespaced id
(`{doc_id}:s{i}`) is therefore byte-identical to a Milvus hit id (SP-14) and
RT-1's `chunks[...].chunk_id` join key -- the unifying identity across
stores. Each node also gets a `doc_id` property (not just a namespaced id)
so `GraphStore.document_structure`/`reading_order` can scope a read by exact
property match rather than an unindexed id-prefix scan.

A cache hit (unchanged input_hash) does not re-run the load; a version bump
forces a real reload for every previously-loaded document (see spec §2
decision 12) -- `GraphStore.load` is idempotent MERGE either way."""

from collections.abc import Mapping
from typing import cast

from fdi.pipeline.graphstore import GraphStore
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


def _list_of_dicts(value: object) -> list[Mapping[str, object]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _namespace_node(doc_id: str, node: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": f"{doc_id}:{node.get('id')}",
        "type": node.get("type"),
        "label": node.get("label"),
        "doc_id": doc_id,
    }


def _namespace_edge(doc_id: str, edge: Mapping[str, object]) -> dict[str, object]:
    return {
        "source": f"{doc_id}:{edge.get('source')}",
        "target": f"{doc_id}:{edge.get('target')}",
        "type": edge.get("type"),
    }


class GraphLoadStage:
    name = "graph_load"
    version = 2
    deps: tuple[str, ...] = ("graph",)

    async def run(self, ctx: StageContext) -> object:
        graph = cast(Mapping[str, object], await ctx.read("graph"))
        store = cast(GraphStore, ctx.services["graph_store"])

        nodes = [_namespace_node(ctx.doc_id, n) for n in _list_of_dicts(graph.get("nodes"))]
        edges = [_namespace_edge(ctx.doc_id, e) for e in _list_of_dicts(graph.get("edges"))]

        receipt = store.load(nodes, edges)
        return {
            "schema_version": 1,
            "loaded_nodes": receipt["loaded_nodes"],
            "loaded_edges": receipt["loaded_edges"],
        }


REGISTRY.register(GraphLoadStage())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_graphload_stage.py -v`
Expected: PASS.

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/pipeline/stages/graphload.py backend/tests/integration/test_graphload_stage.py
git commit -m "fix(pipeline): GraphLoadStage v2 -- namespace node/edge ids by doc_id, fixing cross-document collision"
```

---

### Task 4: `Neo4jGraphStore._load_tx` v2 — real Cypher labels/rel-types

**Files:**
- Modify: `backend/src/fdi/platform/graphstore.py`
- Test: `backend/tests/unit/test_neo4j_graphstore.py` (extend)

**Interfaces:**
- Produces: `_cypher_label`, `_cypher_rel_type` (private, allowlist-validated); `_load_tx` v2.

- [ ] **Step 1: Write the failing test**

Replace the body of `test_neo4j_store_loads_nodes_and_edges_via_merge` in
`backend/tests/unit/test_neo4j_graphstore.py` and add two new tests:

```python
def test_neo4j_store_loads_nodes_and_edges_via_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform import graphstore as gs

    runs: list[tuple[str, dict[str, object]]] = []

    class _FakeTx:
        def run(self, query: str, **kwargs: object) -> None:
            runs.append((query, kwargs))

    class _FakeSession:
        def __enter__(self) -> "_FakeSession":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def execute_write(self, fn, *args: object) -> object:  # type: ignore[no-untyped-def]
            return fn(_FakeTx(), *args)

    class _FakeDriver:
        def session(self) -> _FakeSession:
            return _FakeSession()

    calls: dict[str, object] = {}

    class _FakeGraphDatabase:
        @staticmethod
        def driver(uri: str, auth: tuple[str, str]) -> _FakeDriver:
            calls["uri"] = uri
            calls["auth"] = auth
            return _FakeDriver()

    monkeypatch.setattr(gs, "GraphDatabase", _FakeGraphDatabase)
    store = gs.Neo4jGraphStore(uri="bolt://h:7687", user="neo4j", password="pw")
    assert calls["uri"] == "bolt://h:7687"
    assert calls["auth"] == ("neo4j", "pw")

    nodes = [{"id": "d1:doc", "type": "document", "label": "ACME", "doc_id": "d1"}]
    edges = [{"source": "d1:doc", "target": "d1:s0", "type": "CONTAINS"}]
    receipt = store.load(nodes, edges)
    assert receipt == {"loaded_nodes": 1, "loaded_edges": 1}

    node_runs = [r for r in runs if "MERGE (n:Document" in r[0]]
    edge_runs = [r for r in runs if "MERGE (a)-[r:CONTAINS]" in r[0]]
    assert len(node_runs) == 1
    assert node_runs[0][1] == {
        "id": "d1:doc", "type": "document", "label": "ACME", "doc_id": "d1",
    }
    assert len(edge_runs) == 1
    assert edge_runs[0][1] == {"source": "d1:doc", "target": "d1:s0", "type": "CONTAINS"}
    # the OLD generic label/rel-type must be gone
    assert not any("MERGE (n:Node" in r[0] for r in runs)
    assert not any(":REL" in r[0] for r in runs)


def test_neo4j_store_uses_section_and_paragraph_labels_and_reading_order_rel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fdi.platform import graphstore as gs

    runs: list[str] = []

    class _FakeTx:
        def run(self, query: str, **kwargs: object) -> None:
            runs.append(query)

    class _FakeSession:
        def __enter__(self) -> "_FakeSession":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def execute_write(self, fn, *args: object) -> object:  # type: ignore[no-untyped-def]
            return fn(_FakeTx(), *args)

    class _FakeDriver:
        def session(self) -> _FakeSession:
            return _FakeSession()

    class _FakeGraphDatabase:
        @staticmethod
        def driver(uri: str, auth: tuple[str, str]) -> _FakeDriver:
            return _FakeDriver()

    monkeypatch.setattr(gs, "GraphDatabase", _FakeGraphDatabase)
    store = gs.Neo4jGraphStore(uri="bolt://h:7687", user="neo4j", password="pw")

    nodes = [
        {"id": "d1:s0", "type": "section", "label": "S", "doc_id": "d1"},
        {"id": "d1:s0p0", "type": "paragraph", "label": "P", "doc_id": "d1"},
    ]
    edges = [{"source": "d1:s0", "target": "d1:s1", "type": "READING_ORDER"}]
    store.load(nodes, edges)

    assert any("MERGE (n:Section" in q for q in runs)
    assert any("MERGE (n:Paragraph" in q for q in runs)
    assert any("MERGE (a)-[r:READING_ORDER]" in q for q in runs)


def test_neo4j_store_unknown_node_type_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform import graphstore as gs

    class _FakeTx:
        def run(self, query: str, **kwargs: object) -> None:
            raise AssertionError("must not run any query for an unknown type")

    class _FakeSession:
        def __enter__(self) -> "_FakeSession":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def execute_write(self, fn, *args: object) -> object:  # type: ignore[no-untyped-def]
            return fn(_FakeTx(), *args)

    class _FakeDriver:
        def session(self) -> _FakeSession:
            return _FakeSession()

    class _FakeGraphDatabase:
        @staticmethod
        def driver(uri: str, auth: tuple[str, str]) -> _FakeDriver:
            return _FakeDriver()

    monkeypatch.setattr(gs, "GraphDatabase", _FakeGraphDatabase)
    store = gs.Neo4jGraphStore(uri="bolt://h:7687", user="neo4j", password="pw")

    with pytest.raises(ValueError, match="unknown graph node type"):
        store.load([{"id": "x", "type": "widget", "label": "?", "doc_id": "d1"}], [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_neo4j_graphstore.py -v`
Expected: FAIL — queries still contain `:Node`/`:REL`; unknown type does not raise.

- [ ] **Step 3: Implement**

In `backend/src/fdi/platform/graphstore.py`, add the allowlist + validators above the class, and
replace `_load_tx`:

```python
_NODE_LABELS: Mapping[str, str] = {
    "document": "Document", "section": "Section", "paragraph": "Paragraph",
}
_EDGE_REL_TYPES: Mapping[str, str] = {"CONTAINS": "CONTAINS", "READING_ORDER": "READING_ORDER"}


def _cypher_label(node_type: object) -> str:
    """The real Cypher label for a graph node `type`, from a closed allowlist.

    Cypher does not allow a parameterized label ($type in a label position is
    a syntax error), so the label MUST be built into the query text -- doing
    that from an unvalidated `type` would let a malformed artifact inject
    arbitrary Cypher via the label position. Only this function's RETURN
    VALUE (one of the dict's fixed literals, never `node_type` itself) may
    ever reach an f-string. An unknown type raises rather than falling back
    to a generic label: it means `build_graph`'s type vocabulary changed
    without this module being updated, not a runtime fact to degrade past.
    """
    if not isinstance(node_type, str) or node_type not in _NODE_LABELS:
        raise ValueError(f"unknown graph node type: {node_type!r}; known: {sorted(_NODE_LABELS)}")
    return _NODE_LABELS[node_type]


def _cypher_rel_type(edge_type: object) -> str:
    """The real Cypher relationship type for a graph edge `type`. Same
    injection-safety and fail-loud rationale as `_cypher_label`."""
    if not isinstance(edge_type, str) or edge_type not in _EDGE_REL_TYPES:
        raise ValueError(
            f"unknown graph edge type: {edge_type!r}; known: {sorted(_EDGE_REL_TYPES)}"
        )
    return _EDGE_REL_TYPES[edge_type]
```

```python
    @staticmethod
    def _load_tx(
        tx: ManagedTransaction,
        nodes: list[Mapping[str, object]],
        edges: list[Mapping[str, object]],
    ) -> tuple[int, int]:
        for node in nodes:
            label = _cypher_label(node.get("type"))
            tx.run(
                f"MERGE (n:{label} {{id: $id}}) "
                "SET n.type = $type, n.label = $label, n.doc_id = $doc_id",
                id=node["id"], type=node["type"], label=node["label"], doc_id=node.get("doc_id"),
            )
        for edge in edges:
            rel = _cypher_rel_type(edge.get("type"))
            tx.run(
                f"MATCH (a {{id: $source}}), (b {{id: $target}}) "
                f"MERGE (a)-[r:{rel}]->(b) SET r.type = $type",
                source=edge["source"], target=edge["target"], type=edge["type"],
            )
        return len(nodes), len(edges)
```

(The node `SET` clause's kwargs to `tx.run` are `{id, type, label, doc_id}` — matches the test's
expected `node_runs[0][1]` exactly. `MATCH (a {id: $source}), (b {id: $target})` stays UNLABELED —
ids are globally unique post-namespacing, so an unlabeled property match is correct; see spec §3.3 for
the deferred index/constraint follow-up.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_neo4j_graphstore.py -v`
Expected: PASS (all, old + new 3).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
git add backend/src/fdi/platform/graphstore.py backend/tests/unit/test_neo4j_graphstore.py
git commit -m "fix(platform): Neo4jGraphStore._load_tx -- real Cypher labels/rel-types via validated allowlist, doc_id property"
```

---

### Task 5: `Neo4jGraphStore` read methods

**Files:**
- Modify: `backend/src/fdi/platform/graphstore.py`
- Test: `backend/tests/unit/test_neo4j_graphstore_reads.py` (new)

**Interfaces:**
- Consumes: `order_reading_chain` (Task 1).
- Produces: `Neo4jGraphStore.neighbors/document_structure/reading_order`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_neo4j_graphstore_reads.py
"""Neo4jGraphStore's three read methods, against a monkeypatched fake driver
that scripts Cypher-shaped row results per query -- no live Neo4j."""

import pytest


class _Row(dict):
    """A dict that also supports `row["key"]` AND `row.get("key")`, matching
    the neo4j Record interface closely enough for these unit tests."""


class _FakeResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def __iter__(self):
        return iter(_Row(r) for r in self._rows)

    def single(self):
        return _Row(self._rows[0]) if self._rows else None


def _make_store(monkeypatch: pytest.MonkeyPatch, script: dict[str, list[dict[str, object]]]):
    """`script` maps a distinguishing substring of the Cypher text to the rows
    that query should return (matched by `in`, first match wins)."""
    from fdi.platform import graphstore as gs

    class _FakeTx:
        def run(self, query: str, **kwargs: object) -> _FakeResult:
            for needle, rows in script.items():
                if needle in query:
                    return _FakeResult(rows)
            raise AssertionError(f"unscripted query: {query}")

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def execute_read(self, fn, *args: object) -> object:  # type: ignore[no-untyped-def]
            return fn(_FakeTx(), *args)

    class _FakeDriver:
        def session(self) -> _FakeSession:
            return _FakeSession()

    class _FakeGraphDatabase:
        @staticmethod
        def driver(uri: str, auth: tuple[str, str]) -> _FakeDriver:
            return _FakeDriver()

    monkeypatch.setattr(gs, "GraphDatabase", _FakeGraphDatabase)
    return gs.Neo4jGraphStore(uri="bolt://h:7687", user="neo4j", password="pw")


def test_neighbors_builds_node_relationship_direction(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _make_store(
        monkeypatch,
        {
            "-[r]->": [
                {"relationship": "CONTAINS", "direction": "out", "id": "d1:s0p0",
                 "type": "paragraph", "label": "P", "doc_id": "d1"},
            ],
        },
    )
    # both directions come from the SAME UNION query in the real implementation;
    # the fake driver only distinguishes by substring, so script both halves
    # under keys that both appear in the one query text.
    result = store.neighbors("d1:s0")
    assert result[0]["node"]["id"] == "d1:s0p0"
    assert result[0]["relationship"] == "CONTAINS"
    assert result[0]["direction"] == "out"


def test_neighbors_unknown_id_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _make_store(monkeypatch, {"-[r]->": []})
    assert store.neighbors("d1:ghost") == []


def test_document_structure_orders_sections_via_reading_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _make_store(
        monkeypatch,
        {
            "MATCH (d:Document": [{"id": "d1:doc", "label": "ACME"}],
            "READING_ORDER]->(b:Section": [{"source": "d1:s0", "target": "d1:s1"}],
            "MATCH (s:Section {doc_id: $doc_id}) RETURN": [
                {"id": "d1:s1", "label": "Second"},  # deliberately out of order
                {"id": "d1:s0", "label": "First"},
            ],
            "CONTAINS]->(p:Paragraph": [{"id": "d1:s0p0", "label": "P0", "section_id": "d1:s0"}],
            "READING_ORDER]->(b:Paragraph": [],
        },
    )
    out = store.document_structure("d1")
    assert out["document"] == {"id": "d1:doc", "type": "document", "label": "ACME", "doc_id": "d1"}
    assert [s["id"] for s in out["sections"]] == ["d1:s0", "d1:s1"]
    assert [p["id"] for p in out["sections"][0]["paragraphs"]] == ["d1:s0p0"]
    assert out["sections"][1]["paragraphs"] == []


def test_document_structure_unknown_doc_returns_none_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _make_store(
        monkeypatch,
        {
            "MATCH (d:Document": [],
            "MATCH (s:Section {doc_id: $doc_id}) RETURN": [],
            "READING_ORDER]->(b:Section": [],
            "CONTAINS]->(p:Paragraph": [],
            "READING_ORDER]->(b:Paragraph": [],
        },
    )
    out = store.document_structure("no-such-doc")
    assert out == {"document": None, "sections": []}


def test_reading_order_returns_sections_without_paragraphs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _make_store(
        monkeypatch,
        {
            "MATCH (s:Section {doc_id: $doc_id}) RETURN": [
                {"id": "d1:s0", "label": "First"},
            ],
            "READING_ORDER]->(b:Section": [],
        },
    )
    out = store.reading_order("d1")
    assert out == [{"id": "d1:s0", "type": "section", "label": "First", "doc_id": "d1"}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_neo4j_graphstore_reads.py -v`
Expected: FAIL — `AttributeError: 'Neo4jGraphStore' object has no attribute 'neighbors'`.

- [ ] **Step 3: Implement**

Append to `backend/src/fdi/platform/graphstore.py` (imports: add
`from fdi.core.graph import order_reading_chain` at the top):

```python
    def neighbors(self, node_id: str) -> list[dict[str, object]]:
        with self._driver.session() as session:
            return cast(
                list[dict[str, object]], session.execute_read(self._neighbors_tx, node_id)
            )

    @staticmethod
    def _neighbors_tx(tx: ManagedTransaction, node_id: str) -> list[dict[str, object]]:
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
            {
                "node": {"id": r["id"], "type": r["type"], "label": r["label"], "doc_id": r["doc_id"]},
                "relationship": r["relationship"],
                "direction": r["direction"],
            }
            for r in rows
        ]

    def document_structure(self, doc_id: str) -> dict[str, object]:
        with self._driver.session() as session:
            return cast(
                dict[str, object], session.execute_read(self._document_structure_tx, doc_id)
            )

    @staticmethod
    def _sections_in_reading_order(tx: ManagedTransaction, doc_id: str) -> list[dict[str, object]]:
        sections = [
            {"id": r["id"], "type": "section", "label": r["label"], "doc_id": doc_id}
            for r in tx.run(
                "MATCH (s:Section {doc_id: $doc_id}) RETURN s.id AS id, s.label AS label",
                doc_id=doc_id,
            )
        ]
        edges = [
            {"source": r["source"], "target": r["target"], "type": "READING_ORDER"}
            for r in tx.run(
                "MATCH (a:Section {doc_id: $doc_id})-[:READING_ORDER]->(b:Section {doc_id: $doc_id}) "
                "RETURN a.id AS source, b.id AS target",
                doc_id=doc_id,
            )
        ]
        return order_reading_chain(sections, edges)

    @staticmethod
    def _document_structure_tx(tx: ManagedTransaction, doc_id: str) -> dict[str, object]:
        doc_row = tx.run(
            "MATCH (d:Document {doc_id: $doc_id}) RETURN d.id AS id, d.label AS label",
            doc_id=doc_id,
        ).single()
        document = (
            {"id": doc_row["id"], "type": "document", "label": doc_row["label"], "doc_id": doc_id}
            if doc_row is not None
            else None
        )
        ordered_sections = Neo4jGraphStore._sections_in_reading_order(tx, doc_id)

        paragraphs = [
            {"id": r["id"], "type": "paragraph", "label": r["label"], "section_id": r["section_id"]}
            for r in tx.run(
                "MATCH (s:Section {doc_id: $doc_id})-[:CONTAINS]->(p:Paragraph {doc_id: $doc_id}) "
                "RETURN p.id AS id, p.label AS label, s.id AS section_id",
                doc_id=doc_id,
            )
        ]
        para_edges = [
            {"source": r["source"], "target": r["target"], "type": "READING_ORDER"}
            for r in tx.run(
                "MATCH (a:Paragraph {doc_id: $doc_id})-[:READING_ORDER]->"
                "(b:Paragraph {doc_id: $doc_id}) RETURN a.id AS source, b.id AS target",
                doc_id=doc_id,
            )
        ]
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

    def reading_order(self, doc_id: str) -> list[dict[str, object]]:
        with self._driver.session() as session:
            return cast(
                list[dict[str, object]],
                session.execute_read(self._sections_in_reading_order, doc_id),
            )
```

(Every dict `_sections_in_reading_order`/`_document_structure_tx` build carries `"doc_id"` — set from
the function's own `doc_id` argument, not fetched via Cypher, since it is already known and identical
for every row one call returns. This matters for parity with `FakeGraphStore`: its equivalent methods
return `dict(n)` copies of the ORIGINAL loaded node, which already has `"doc_id"` set by
`GraphLoadStage` — without this, the real and fake stores would disagree on which keys a
section/paragraph/document node dict carries, breaking the "fake and real must agree" contract Task 2
established. `neighbors`' node dicts already carried `doc_id` via `m.doc_id AS doc_id` from the start;
this closes the same gap for the other two read methods. Task 5's test script fixtures below (`{"id":
"d1:doc", "label": "ACME"}` etc.) are the RAW Cypher row shapes BEFORE this `doc_id` attachment — the
attachment happens in Python after the row is read, so the scripted rows themselves do not need a
`doc_id` column.)

Also add `from typing import cast` to the module's existing import line if not already present.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_neo4j_graphstore_reads.py -v`
Expected: PASS.

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
git add backend/src/fdi/platform/graphstore.py backend/tests/unit/test_neo4j_graphstore_reads.py
git commit -m "feat(platform): Neo4jGraphStore.neighbors/document_structure/reading_order via Cypher + order_reading_chain"
```

---

### Task 6: Opt-in real-Neo4j proof (collision fix + read methods)

**Files:**
- Modify: `backend/tests/neo4j/test_real_graph_load.py`

**Interfaces:**
- No new production code -- this task is proof-only, against a real driver, behind the existing
  `neo4j` marker + `FDI_NEO4J_URI`/`FDI_NEO4J_PASSWORD` skip guard.

- [ ] **Step 1: Write the tests** (these run only when a real Neo4j is configured — there is no
  "run to see it fail" step against production infrastructure the executor may not have; write the
  tests and gate on `pytestmark`, matching the existing file's own posture)

```python
# backend/tests/neo4j/test_real_graph_load.py -- full replacement
"""Opt-in smoke tests against a real Neo4j instance.

Skipped unless FDI_NEO4J_URI and FDI_NEO4J_PASSWORD are set. Never runs in CI
or in an unconfigured environment. The skipif marker is evaluated at
collection time and does not construct Settings or touch the network, so
collection is always safe."""

import os
import uuid

import pytest

from fdi.platform.config import get_settings
from fdi.platform.graphstore import Neo4jGraphStore

pytestmark = [
    pytest.mark.neo4j,
    pytest.mark.skipif(
        not (os.getenv("FDI_NEO4J_URI") and os.getenv("FDI_NEO4J_PASSWORD")),
        reason="Neo4j not configured",
    ),
]


def test_real_load_persists_nodes_and_edges_under_real_labels() -> None:
    store = Neo4jGraphStore.from_settings(get_settings())
    assert store is not None

    doc_id = f"sp9b-smoke-{uuid.uuid4()}"
    nodes = [
        {"id": f"{doc_id}:doc", "type": "document", "label": "ACME", "doc_id": doc_id},
        {"id": f"{doc_id}:s0", "type": "section", "label": "Directors' Report", "doc_id": doc_id},
    ]
    edges = [{"source": f"{doc_id}:doc", "target": f"{doc_id}:s0", "type": "CONTAINS"}]

    receipt = store.load(nodes, edges)
    assert receipt == {"loaded_nodes": 2, "loaded_edges": 1}

    with store._driver.session() as session:  # read-back proof, this test only
        count = session.run(
            "MATCH (n:Document {id: $id}) RETURN count(n) AS c", id=f"{doc_id}:doc"
        ).single()["c"]
    assert count == 1
    store.close()


def test_real_load_two_documents_do_not_collide() -> None:
    """The live-Neo4j proof of the RT-5 fix: two documents' graphs, loaded
    through the same store, must both survive independently."""
    store = Neo4jGraphStore.from_settings(get_settings())
    assert store is not None

    doc_a, doc_b = f"sp9b-a-{uuid.uuid4()}", f"sp9b-b-{uuid.uuid4()}"
    for doc_id, label in ((doc_a, "Company A"), (doc_b, "Company B")):
        store.load(
            [{"id": f"{doc_id}:doc", "type": "document", "label": label, "doc_id": doc_id}], []
        )

    with store._driver.session() as session:
        label_a = session.run(
            "MATCH (n:Document {id: $id}) RETURN n.label AS label", id=f"{doc_a}:doc"
        ).single()["label"]
        label_b = session.run(
            "MATCH (n:Document {id: $id}) RETURN n.label AS label", id=f"{doc_b}:doc"
        ).single()["label"]
    assert label_a == "Company A"
    assert label_b == "Company B"  # pre-fix, the second load would have overwritten the first
    store.close()


def test_real_read_methods_over_a_loaded_document() -> None:
    store = Neo4jGraphStore.from_settings(get_settings())
    assert store is not None

    doc_id = f"sp9b-reads-{uuid.uuid4()}"
    nodes = [
        {"id": f"{doc_id}:doc", "type": "document", "label": "ACME", "doc_id": doc_id},
        {"id": f"{doc_id}:s0", "type": "section", "label": "First", "doc_id": doc_id},
        {"id": f"{doc_id}:s1", "type": "section", "label": "Second", "doc_id": doc_id},
        {"id": f"{doc_id}:s0p0", "type": "paragraph", "label": "P0", "doc_id": doc_id},
    ]
    edges = [
        {"source": f"{doc_id}:doc", "target": f"{doc_id}:s0", "type": "CONTAINS"},
        {"source": f"{doc_id}:doc", "target": f"{doc_id}:s1", "type": "CONTAINS"},
        {"source": f"{doc_id}:s0", "target": f"{doc_id}:s1", "type": "READING_ORDER"},
        {"source": f"{doc_id}:s0", "target": f"{doc_id}:s0p0", "type": "CONTAINS"},
    ]
    store.load(nodes, edges)

    neighbors = store.neighbors(f"{doc_id}:s0")
    rels = {(n["node"]["id"], n["relationship"], n["direction"]) for n in neighbors}
    assert (f"{doc_id}:doc", "CONTAINS", "in") in rels
    assert (f"{doc_id}:s1", "READING_ORDER", "out") in rels

    structure = store.document_structure(doc_id)
    assert [s["id"] for s in structure["sections"]] == [f"{doc_id}:s0", f"{doc_id}:s1"]
    assert [p["id"] for p in structure["sections"][0]["paragraphs"]] == [f"{doc_id}:s0p0"]

    order = store.reading_order(doc_id)
    assert [s["id"] for s in order] == [f"{doc_id}:s0", f"{doc_id}:s1"]
    store.close()
```

- [ ] **Step 2: Run against a real instance, if available**

If a Neo4j instance is reachable (`docker compose --profile graph up`, `FDI_NEO4J_URI`/
`FDI_NEO4J_PASSWORD` set): `uv run --directory backend pytest tests/neo4j -v -m neo4j`.
If not reachable in this environment, these tests SKIP (verify the skip reason is
`"Neo4j not configured"`, not a collection error) — that is an acceptable terminal state for this
task; do not fabricate a pass.

- [ ] **Step 3: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
git add backend/tests/neo4j/test_real_graph_load.py
git commit -m "test(neo4j): opt-in real-Neo4j proof of the label fix, the cross-document collision fix, and the read methods"
```

---

### Task 7: `fdi.pipeline.graphquery` — citation-attaching orchestrator

**Files:**
- Create: `backend/src/fdi/pipeline/graphquery.py`
- Test: `backend/tests/integration/test_graphquery.py`

**Interfaces:**
- Consumes: `GraphStore` (Tasks 2/5), `RegistryRepository`, `ArtifactStore`.
- Produces: `neighbors`, `document_structure`, `reading_order` (all `async`, all take `session, store,
  graph_store` + the relevant id).

**Pre-check (do first, document in the module docstring):** read `backend/src/fdi/pipeline/intake.py`
and confirm `intake_document` generates `doc_id` via `str(uuid.uuid4())` (never containing `:`) — the
fact `_split` relies on to correctly separate a namespaced node id back into `(doc_id, bare_id)`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/integration/test_graphquery.py
"""fdi.pipeline.graphquery: neighbors/document_structure/reading_order, each
citation-enriched via the `provenance` artifact + the Document row. Against
real Postgres + MinIO with FAKE OCR/semantic clients and a FakeGraphStore."""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.semantic import SectionType
from fdi.pipeline.graphquery import document_structure, neighbors, reading_order
from fdi.pipeline.graphstore import FakeGraphStore
from fdi.pipeline.intake import intake_document
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.run import run_stage
from fdi.pipeline.semantic import FakeSemanticClient
from fdi.pipeline.stages.ast import AstStage
from fdi.pipeline.stages.graph import GraphStage
from fdi.pipeline.stages.graphload import GraphLoadStage
from fdi.pipeline.stages.normalize import NormalizeStage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.pipeline.stages.provenance import ProvenanceStage
from fdi.pipeline.stages.semantic import SemanticStage
from fdi.platform.db.engine import make_session_factory
from fdi.platform.storage.artifact_store import ArtifactStore

pytestmark = pytest.mark.integration


@pytest.fixture
def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(migrated_engine)


@pytest.fixture
async def store(minio_client: Minio) -> ArtifactStore:
    s = ArtifactStore(minio_client, bucket="test-artifacts")
    await s.ensure_bucket()
    return s


@pytest.fixture
async def graph_store() -> FakeGraphStore:
    return FakeGraphStore()


@pytest.fixture
async def processed_doc(session_factory, store, graph_store: FakeGraphStore) -> str:
    """Intake one FakeOcr document and run ocr -> ... -> graph_load."""
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="a.pdf", doc_key="ACME-10K", content_type="application/pdf",
        session_factory=session_factory, store=store,
    )
    fake_ocr = FakeOcrClient(
        {
            "content": "ACME",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": [
                {"role": "title", "content": "ACME LTD",
                 "boundingRegions": [{"pageNumber": 1, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}]},
                {"role": "sectionHeading", "content": "Balance Sheet",
                 "boundingRegions": [{"pageNumber": 1, "polygon": [0, 1, 1, 1, 1, 2, 0, 2]}]},
                {"content": "Total Assets 100",
                 "boundingRegions": [{"pageNumber": 1, "polygon": [0, 2, 1, 2, 1, 3, 0, 3]}]},
                {"role": "sectionHeading", "content": "Directors' Report",
                 "boundingRegions": [{"pageNumber": 2, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}]},
                {"content": "The board is pleased.",
                 "boundingRegions": [{"pageNumber": 2, "polygon": [0, 1, 1, 1, 1, 2, 0, 2]}]},
            ],
            "tables": [],
        }
    )
    fake_semantic = FakeSemanticClient(
        {
            "Balance Sheet": SectionType.BALANCE_SHEET.value,
            "Directors' Report": SectionType.DIRECTORS_REPORT.value,
        }
    )

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(SemanticStage())
    reg.register(GraphStage())
    reg.register(ProvenanceStage())
    reg.register(GraphLoadStage())
    resolver = DagResolver(reg.all())

    common = {
        "doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
        "store": store, "registry": reg, "resolver": resolver,
    }
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake_ocr}, **common)
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="semantic", run_id=str(uuid.uuid4()),
        services={"semantic": fake_semantic}, **common,
    )
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="graph_load", run_id=str(uuid.uuid4()),
        services={"graph_store": graph_store}, **common,
    )
    return doc.id


async def test_neighbors_returns_cited_hits(
    session_factory, store, graph_store: FakeGraphStore, processed_doc: str
) -> None:
    async with session_factory() as s:
        out = await neighbors(
            session=s, store=store, graph_store=graph_store, node_id=f"{processed_doc}:doc"
        )
    assert out["node_id"] == f"{processed_doc}:doc"
    section = next(n for n in out["neighbors"] if n["node"]["id"] == f"{processed_doc}:s0")
    assert section["relationship"] == "CONTAINS"
    assert section["direction"] == "out"
    citation = section["node"]["citation"]
    assert citation["doc_id"] == processed_doc
    assert citation["doc_key"] == "ACME-10K"
    assert citation["page"] == 1


async def test_neighbors_malformed_node_id_returns_empty(
    session_factory, store, graph_store: FakeGraphStore
) -> None:
    async with session_factory() as s:
        out = await neighbors(session=s, store=store, graph_store=graph_store, node_id="no-colon")
    assert out["neighbors"] == []


async def test_document_structure_returns_ordered_cited_tree(
    session_factory, store, graph_store: FakeGraphStore, processed_doc: str
) -> None:
    async with session_factory() as s:
        out = await document_structure(
            session=s, store=store, graph_store=graph_store, doc_id=processed_doc
        )
    assert out["doc_id"] == processed_doc
    section_labels = [s["label"] for s in out["sections"]]
    assert section_labels == ["Balance Sheet", "Directors' Report"]
    first_section = out["sections"][0]
    assert first_section["citation"]["page"] == 1
    assert [p["label"] for p in first_section["paragraphs"]] == ["Total Assets 100"]
    assert first_section["paragraphs"][0]["citation"]["page"] == 1


async def test_document_structure_unknown_doc_id(session_factory, store, graph_store) -> None:
    async with session_factory() as s:
        out = await document_structure(
            session=s, store=store, graph_store=graph_store, doc_id="no-such-doc"
        )
    assert out == {"schema_version": 1, "doc_id": "no-such-doc", "document": None, "sections": []}


async def test_reading_order_matches_document_structure_section_order(
    session_factory, store, graph_store: FakeGraphStore, processed_doc: str
) -> None:
    async with session_factory() as s:
        out = await reading_order(
            session=s, store=store, graph_store=graph_store, doc_id=processed_doc
        )
    assert [s["label"] for s in out["sections"]] == ["Balance Sheet", "Directors' Report"]
    assert all("paragraphs" not in s for s in out["sections"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_graphquery.py -v`
Expected: FAIL — `ModuleNotFoundError: fdi.pipeline.graphquery`.

- [ ] **Step 3: Implement**

```python
# backend/src/fdi/pipeline/graphquery.py
"""Graph retrieval orchestrator: neighbors/document_structure/reading_order,
each returning GraphStore's raw node/edge shapes enriched with a citation
(doc_id, doc_key, version, heading, page, bounding_regions). A pipeline-layer
service -- imports platform (GraphStore + repository + artifact store) + core
(none directly; the ordering already happened inside GraphStore) -- the same
shape as fdi.pipeline.crossdoc.resolve_entities and fdi.pipeline.retrieval.
resolve_chunk, called by an API route.

Kept separate from fdi.pipeline.retrieval (RT-1's chunk-resolution seam,
which has no reason to import GraphStore) rather than folded into it -- see
design spec §3.5.

Node ids are namespaced `{doc_id}:{bare_id}` (GraphLoadStage, RT-5); the
`provenance` artifact (SP-12) is unchanged and keys on the BARE id --
`_split` recovers `(doc_id, bare_id)` by splitting on the first ':'.

Pre-check (verified against fdi/pipeline/intake.py): `intake_document`
generates `doc_id` via `str(uuid.uuid4())`, which never contains ':' -- so
splitting a namespaced node id on the FIRST ':' always correctly separates
doc_id from bare_id, even though a bare id itself could theoretically
contain further structure (it doesn't today: ids are `doc`/`s{i}`/
`s{i}p{j}`)."""

from collections.abc import Mapping
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from fdi.pipeline.graphstore import GraphStore
from fdi.platform.db.models import Document
from fdi.platform.db.repository import RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore


def _split(node_id: str) -> tuple[str, str] | None:
    if ":" not in node_id:
        return None
    doc_id, bare_id = node_id.split(":", 1)
    return doc_id, bare_id


async def _read_provenance(
    repo: RegistryRepository, store: ArtifactStore, doc_id: str
) -> dict[str, dict[str, object]]:
    """bare node_id -> {"page", "bounding_regions"}, read ONCE per doc_id."""
    run = await repo.latest_run(doc_id, "provenance")
    if run is None or run.artifact_key is None:
        return {}
    body = cast(Mapping[str, object], await store.get_json(run.artifact_key))
    node_prov = body.get("node_provenance")
    out: dict[str, dict[str, object]] = {}
    if isinstance(node_prov, list):
        for rec in node_prov:
            if isinstance(rec, Mapping) and isinstance(rec.get("node_id"), str):
                out[rec["node_id"]] = {
                    "page": rec.get("page"), "bounding_regions": rec.get("bounding_regions"),
                }
    return out


def _cite(
    node: Mapping[str, object], doc: Document | None, prov: Mapping[str, dict[str, object]]
) -> dict[str, object]:
    node_id = node.get("id")
    split = _split(node_id) if isinstance(node_id, str) else None
    entry: dict[str, object] = prov.get(split[1], {}) if split is not None else {}
    return {
        **node,
        "citation": {
            "doc_id": doc.id if doc is not None else None,
            "doc_key": doc.doc_key if doc is not None else None,
            "version": doc.version if doc is not None else None,
            "heading": node.get("label"),
            "page": entry.get("page"),
            "bounding_regions": entry.get("bounding_regions"),
        },
    }


async def neighbors(
    *, session: AsyncSession, store: ArtifactStore, graph_store: GraphStore, node_id: str
) -> dict[str, object]:
    split = _split(node_id)
    if split is None:
        return {"schema_version": 1, "node_id": node_id, "neighbors": []}
    doc_id, _bare_id = split

    repo = RegistryRepository(session)
    doc = await repo.get_document(doc_id)
    prov = await _read_provenance(repo, store, doc_id)

    raw = graph_store.neighbors(node_id)
    return {
        "schema_version": 1,
        "node_id": node_id,
        "neighbors": [
            {**hit, "node": _cite(cast(Mapping[str, object], hit["node"]), doc, prov)}
            for hit in raw
        ],
    }


async def document_structure(
    *, session: AsyncSession, store: ArtifactStore, graph_store: GraphStore, doc_id: str
) -> dict[str, object]:
    repo = RegistryRepository(session)
    doc = await repo.get_document(doc_id)
    prov = await _read_provenance(repo, store, doc_id)

    raw = graph_store.document_structure(doc_id)
    document_node = raw.get("document")
    document = (
        _cite(cast(Mapping[str, object], document_node), doc, prov)
        if document_node is not None
        else None
    )
    sections_raw = raw.get("sections")
    sections = [s for s in sections_raw if isinstance(s, Mapping)] if isinstance(sections_raw, list) else []
    cited_sections = []
    for s in sections:
        paras_raw = s.get("paragraphs")
        paras = [p for p in paras_raw if isinstance(p, Mapping)] if isinstance(paras_raw, list) else []
        cited_sections.append({
            **_cite(s, doc, prov),
            "paragraphs": [_cite(p, doc, prov) for p in paras],
        })

    return {
        "schema_version": 1, "doc_id": doc_id, "document": document, "sections": cited_sections,
    }


async def reading_order(
    *, session: AsyncSession, store: ArtifactStore, graph_store: GraphStore, doc_id: str
) -> dict[str, object]:
    repo = RegistryRepository(session)
    doc = await repo.get_document(doc_id)
    prov = await _read_provenance(repo, store, doc_id)

    raw = graph_store.reading_order(doc_id)
    return {
        "schema_version": 1,
        "doc_id": doc_id,
        "sections": [_cite(s, doc, prov) for s in raw],
    }
```

(`_cite`'s `{**node, "citation": ...}` spread makes the citation-enriched `dict` carry `paragraphs` for
a section if it was already present in `node` — `document_structure` then OVERWRITES `paragraphs` with
the cited paragraph list right after, which is correct and intentional, not a double-spread bug: the
first `_cite(s, ...)` call cites the section's OWN fields (id/type/label/doc_id) and drops its raw
`paragraphs` value forward unchanged, then the surrounding dict literal replaces `paragraphs` with the
cited version.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_graphquery.py -v`
Expected: PASS.

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/pipeline/graphquery.py backend/tests/integration/test_graphquery.py
git commit -m "feat(pipeline): graphquery -- neighbors/document_structure/reading_order with provenance-backed citations"
```

---

### Task 8: API wiring — schemas, `GraphStoreDep`, app.py, routes, 503 gate

**Files:**
- Modify: `backend/src/fdi/api/schemas.py`
- Modify: `backend/src/fdi/api/deps.py`
- Modify: `backend/src/fdi/api/app.py`
- Modify: `backend/src/fdi/api/routes.py`
- Test: `backend/tests/integration/test_api.py` (extend — 503 cases only; happy path is Task 9)

**Interfaces:**
- Produces: `GraphNeighborsRequest`, `GraphCitationView`, `GraphNodeView`, `GraphNeighborView`,
  `GraphNeighborsView`, `GraphParagraphView`, `GraphSectionView`, `DocumentStructureView`,
  `ReadingOrderView` (schemas); `get_graph_store`/`GraphStoreDep` (deps); `app.state.graph_store`
  (app.py); `POST /graph/neighbors`, `GET /documents/{doc_id}/graph/structure`,
  `GET /documents/{doc_id}/graph/reading-order` (routes).

**Note:** this is the FIRST place `app.state.graph_store` / a `GraphStoreDep` is created — the
`graph_store` wired in `fdi.workers.tasks` (for the `graph_load` STAGE) is unrelated and untouched;
`api/app.py`'s lifespan currently wires `embedder`/`vector_store` but nothing graph-related.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/integration/test_api.py`:

```python
# --------------------------------------------------------------------------
# RT-5: graph retrieval routes -- 503 when graph_store is unconfigured.
# --------------------------------------------------------------------------


async def test_graph_neighbors_503_when_graph_store_unconfigured(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.post("/graph/neighbors", json={"node_id": "d1:s0"})
    assert r.status_code == 503
    assert "graph_store" in r.json()["detail"]


async def test_document_structure_503_when_graph_store_unconfigured(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.get("/documents/no-such-doc/graph/structure")
    assert r.status_code == 503
    assert "graph_store" in r.json()["detail"]


async def test_reading_order_503_when_graph_store_unconfigured(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.get("/documents/no-such-doc/graph/reading-order")
    assert r.status_code == 503
    assert "graph_store" in r.json()["detail"]
```

(Executor: confirm the existing `client`/`make_client` fixtures in this file build the app WITHOUT a
configured `FDI_NEO4J_URI` by default, same as they already do for `FDI_MILVUS_URI` in the `/search`
503 test — if not, add `FDI_NEO4J_URI`/`FDI_NEO4J_PASSWORD` to whatever env-clearing the `/search` 503
test relies on, matching that test's setup exactly.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_api.py -k graph_store_unconfigured -v`
Expected: FAIL — `404 Not Found` (routes don't exist yet).

- [ ] **Step 3: Implement**

**`backend/src/fdi/api/schemas.py`** — append:

```python
class GraphCitationView(BaseModel):
    doc_id: str | None
    doc_key: str | None
    version: int | None
    heading: str | None
    page: int | None
    bounding_regions: list[dict[str, object]] | None


class GraphNodeView(BaseModel):
    id: str
    type: str
    label: str
    doc_id: str | None = None
    citation: GraphCitationView


class GraphParagraphView(BaseModel):
    id: str
    type: str
    label: str
    citation: GraphCitationView


class GraphSectionView(BaseModel):
    id: str
    type: str
    label: str
    citation: GraphCitationView
    paragraphs: list[GraphParagraphView]


class GraphNeighborsRequest(BaseModel):
    node_id: str


class GraphNeighborView(BaseModel):
    node: GraphNodeView
    relationship: str
    direction: str


class GraphNeighborsView(BaseModel):
    schema_version: int
    node_id: str
    neighbors: list[GraphNeighborView]


class DocumentStructureView(BaseModel):
    schema_version: int
    doc_id: str
    document: GraphNodeView | None
    sections: list[GraphSectionView]


class ReadingOrderView(BaseModel):
    schema_version: int
    doc_id: str
    sections: list[GraphNodeView]
```

**`backend/src/fdi/api/deps.py`** — add import + dep function + alias:

```python
from fdi.pipeline.graphstore import GraphStore
# ... (alongside the existing fdi.pipeline.* imports, alphabetical)

def get_graph_store(request: Request) -> GraphStore | None:
    graph_store: GraphStore | None = request.app.state.graph_store
    return graph_store

# ... near the other *Dep aliases:
GraphStoreDep = Annotated[GraphStore | None, Depends(get_graph_store)]
```

**`backend/src/fdi/api/app.py`** — add import + one wiring line in `lifespan`, alongside the existing
`embedder`/`vector_store` lines:

```python
from fdi.platform.graphstore import Neo4jGraphStore
# ...
    app.state.embedder = BgeEmbeddingClient.from_settings(settings)
    app.state.vector_store = MilvusVectorStore.from_settings(settings)
    app.state.graph_store = Neo4jGraphStore.from_settings(settings)
    app.state.entity_threshold = settings.entity_sim_threshold
```

**`backend/src/fdi/api/routes.py`** — add imports and three routes at the end of the file:

```python
from fdi.api.deps import (
    # ... existing entries, alphabetically add:
    GraphStoreDep,
)
from fdi.api.schemas import (
    # ... existing entries, alphabetically add:
    DocumentStructureView,
    GraphNeighborsRequest,
    GraphNeighborsView,
    ReadingOrderView,
)
from fdi.pipeline.graphquery import (
    document_structure as document_structure_query,
    neighbors as neighbors_query,
    reading_order as reading_order_query,
)


# --------------------------------------------------------------------------
# RT-5: graph retrieval -- neighbors / document structure / reading order.
# --------------------------------------------------------------------------


def _require_graph_store(graph_store: GraphStore | None) -> GraphStore:
    if graph_store is None:
        raise HTTPException(
            status_code=503,
            detail="graph retrieval is not configured: missing graph_store (set FDI_NEO4J_URI/FDI_NEO4J_PASSWORD)",
        )
    return graph_store


@router.post("/graph/neighbors", response_model=GraphNeighborsView)
async def graph_neighbors(
    body: GraphNeighborsRequest, session: SessionDep, store: StoreDep, graph_store: GraphStoreDep
) -> GraphNeighborsView:
    gs = _require_graph_store(graph_store)
    result = await neighbors_query(session=session, store=store, graph_store=gs, node_id=body.node_id)
    return GraphNeighborsView.model_validate(result)


@router.get("/documents/{doc_id}/graph/structure", response_model=DocumentStructureView)
async def document_structure_route(
    doc_id: str, session: SessionDep, store: StoreDep, graph_store: GraphStoreDep
) -> DocumentStructureView:
    gs = _require_graph_store(graph_store)
    if await RegistryRepository(session).get_document(doc_id) is None:
        raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
    result = await document_structure_query(session=session, store=store, graph_store=gs, doc_id=doc_id)
    return DocumentStructureView.model_validate(result)


@router.get("/documents/{doc_id}/graph/reading-order", response_model=ReadingOrderView)
async def reading_order_route(
    doc_id: str, session: SessionDep, store: StoreDep, graph_store: GraphStoreDep
) -> ReadingOrderView:
    gs = _require_graph_store(graph_store)
    if await RegistryRepository(session).get_document(doc_id) is None:
        raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
    result = await reading_order_query(session=session, store=store, graph_store=gs, doc_id=doc_id)
    return ReadingOrderView.model_validate(result)
```

(`GraphStore` must be imported in `routes.py` for `_require_graph_store`'s annotation —
`from fdi.pipeline.graphstore import GraphStore`. The 503 check runs BEFORE the document-existence
404 check, per spec §4's ordering rationale — an unconfigured backend fails fast without a DB
round-trip that would be moot regardless of its answer.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_api.py -k graph_store_unconfigured -v`
Expected: PASS.

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/api/schemas.py backend/src/fdi/api/deps.py backend/src/fdi/api/app.py backend/src/fdi/api/routes.py backend/tests/integration/test_api.py
git commit -m "feat(api): graph retrieval routes -- POST /graph/neighbors, GET /documents/{id}/graph/structure|reading-order (503-gated)"
```

---

### Task 9: API happy-path tests (`FakeGraphStore` override) + 404

**Files:**
- Test: `backend/tests/integration/test_api.py` (extend)

**Interfaces:**
- No new production code — this task proves Task 8's routes end-to-end via
  `app.dependency_overrides`, mirroring the existing `/search` happy-path test
  (`get_embedder`/`get_vector_store` override pattern).

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/integration/test_api.py`:

```python
# --------------------------------------------------------------------------
# RT-5: graph retrieval routes -- happy path (FakeGraphStore override).
# --------------------------------------------------------------------------


async def test_graph_neighbors_returns_cited_hits(make_client, _alembic_applied) -> None:
    from fdi.api.deps import get_graph_store
    from fdi.pipeline.graphstore import FakeGraphStore

    fake_store = FakeGraphStore()
    fake_store.load(
        [
            {"id": "d1:doc", "type": "document", "label": "ACME", "doc_id": "d1"},
            {"id": "d1:s0", "type": "section", "label": "Balance Sheet", "doc_id": "d1"},
        ],
        [{"source": "d1:doc", "target": "d1:s0", "type": "CONTAINS"}],
    )

    async with make_client(
        dependency_overrides={get_graph_store: lambda: fake_store}
    ) as client:
        r = await client.post("/graph/neighbors", json={"node_id": "d1:doc"})
    assert r.status_code == 200
    body = r.json()
    assert body["node_id"] == "d1:doc"
    hit = body["neighbors"][0]
    assert hit["node"]["id"] == "d1:s0"
    assert hit["relationship"] == "CONTAINS"
    assert hit["node"]["citation"]["heading"] == "Balance Sheet"


async def test_document_structure_route_returns_tree(
    make_client, session_factory, store, _alembic_applied
) -> None:
    """`session_factory`/`store` are THIS FILE's own fixtures (defined above,
    around the `client`/`make_client` fixtures) -- `store` is deliberately
    pinned to bucket "artifacts", the same bucket `make_client`'s app opens
    by default, so seeding through it here is visible to the app's routes.
    Do not reconstruct an engine/ArtifactStore by hand; these already exist.
    """
    from fdi.api.deps import get_graph_store
    from fdi.pipeline.graphstore import FakeGraphStore
    from fdi.pipeline.intake import intake_document

    fake_store = FakeGraphStore()
    doc = await intake_document(
        pdf=b"%PDF-1.7\n%fake\n", filename="a.pdf", doc_key="k",
        content_type="application/pdf", session_factory=session_factory, store=store,
    )
    fake_store.load(
        [
            {"id": f"{doc.id}:doc", "type": "document", "label": "ACME", "doc_id": doc.id},
            {"id": f"{doc.id}:s0", "type": "section", "label": "S0", "doc_id": doc.id},
        ],
        [{"source": f"{doc.id}:doc", "target": f"{doc.id}:s0", "type": "CONTAINS"}],
    )

    async with make_client(
        dependency_overrides={get_graph_store: lambda: fake_store}
    ) as client:
        r = await client.get(f"/documents/{doc.id}/graph/structure")
        assert r.status_code == 200
        body = r.json()
        assert body["doc_id"] == doc.id
        assert body["document"]["id"] == f"{doc.id}:doc"
        assert body["sections"][0]["id"] == f"{doc.id}:s0"

        r404 = await client.get("/documents/no-such-doc/graph/structure")
        assert r404.status_code == 404


async def test_reading_order_route_returns_sections_without_paragraphs(
    make_client, session_factory, store, _alembic_applied
) -> None:
    from fdi.api.deps import get_graph_store
    from fdi.pipeline.graphstore import FakeGraphStore
    from fdi.pipeline.intake import intake_document

    fake_store = FakeGraphStore()
    doc = await intake_document(
        pdf=b"%PDF-1.7\n%fake\n", filename="a.pdf", doc_key="k",
        content_type="application/pdf", session_factory=session_factory, store=store,
    )
    fake_store.load(
        [{"id": f"{doc.id}:doc", "type": "document", "label": "ACME", "doc_id": doc.id},
         {"id": f"{doc.id}:s0", "type": "section", "label": "S0", "doc_id": doc.id}],
        [{"source": f"{doc.id}:doc", "target": f"{doc.id}:s0", "type": "CONTAINS"}],
    )

    async with make_client(
        dependency_overrides={get_graph_store: lambda: fake_store}
    ) as client:
        r = await client.get(f"/documents/{doc.id}/graph/reading-order")
        assert r.status_code == 200
        body = r.json()
        assert body["sections"] == [
            {
                "id": f"{doc.id}:s0", "type": "section", "label": "S0", "doc_id": doc.id,
                "citation": {
                    "doc_id": doc.id, "doc_key": "k", "version": 1, "heading": "S0",
                    "page": None, "bounding_regions": None,
                },
            }
        ]
```

(`session_factory`/`store` are fixtures this file already defines, at lines ~88–112, immediately after
`client`/`make_client` — used unmodified here rather than reconstructed by hand, since `store`'s bucket
choice (`"artifacts"`, matching `make_client`'s app default) is itself load-bearing per that fixture's
own docstring.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_api.py -k "graph_neighbors_returns or document_structure_route or reading_order_route" -v`
Expected: FAIL until Task 8's routes exist — should already PASS immediately if run after Task 8 in
sequence; if run standalone, same `ModuleNotFoundError`/404 shape as Task 8's Step 2.

- [ ] **Step 3: No new implementation** — Task 8 already built the routes; this task is test-only. If
  the tests reveal a citation-shape mismatch against the schemas from Task 8 (e.g. a field name typo),
  fix it in `schemas.py`/`graphquery.py`, re-run.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_api.py -k "graph_neighbors_returns or document_structure_route or reading_order_route" -v`
Expected: PASS.

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/tests/integration/test_api.py
git commit -m "test(api): graph retrieval routes happy path via FakeGraphStore dependency override + 404 case"
```

---

### Task 10: Full gates, roadmap + spec status

**Files:**
- Modify: `docs/superpowers/specs/2026-07-18-rt5-graph-retrieval-design.md` (Status → Implemented)
- Modify: `docs/architecture/retrieval-roadmap.md` (RT-5 row → done + handoff note)

- [ ] **Step 1: Full static gates** — `ruff check .`, `mypy --strict src`,
  `PYTHONUTF8=1 lint-imports` (1 kept/0 broken). Grep-confirm `fdi.core.graph` still imports nothing
  from `platform`/`pipeline` (stays pure).

- [ ] **Step 2: Full suite** — `uv run --directory backend pytest -q`; green (e2e/azure/neo4j/milvus/
  bge skip by default). Then, IF a real Neo4j is reachable in this environment, also run
  `pytest -m neo4j -v` and confirm Task 6's tests pass live (not just skip). Note the passed count.

- [ ] **Step 3: Audit RT-5 done criteria** (§7 of the spec) — map each item to the task that satisfies
  it; confirm the cross-document collision regression test (Task 3) and its real-Neo4j counterpart
  (Task 6) both exist and pass (or skip cleanly, per Task 6's terminal-state note); confirm the two
  flagged forks (raw Cypher, stale-node migration) are documented in the spec, not silently resolved
  in code; confirm the executor pre-check (doc_id never contains `:`) is verified and documented in
  `graphquery.py`'s docstring.

- [ ] **Step 4: Spec status → Implemented 2026-07-18** (only after Steps 1–3 green).

- [ ] **Step 5: Roadmap** — flip the RT-5 row to `✅ done` in `docs/architecture/retrieval-roadmap.md`;
  add a short "What RT-5 shipped that RT-7/RT-8 must know" handoff: the `{doc_id}:s{i}` section-node
  identity now matches Milvus/RT-1 exactly (no cross-store id translation needed for section-level
  joins); the `GraphStore.neighbors/document_structure/reading_order` read seam (mockable via
  `FakeGraphStore`); the citation shape `{doc_id, doc_key, version, heading, page, bounding_regions}`
  every graph tool attaches; the two documented-but-unresolved forks (raw Cypher, stale pre-fix node
  cleanup) for whoever picks them up.

- [ ] **Step 6: Commit**

```bash
git add docs
git commit -m "docs: RT-5 done-criteria audit, spec status, roadmap handoff"
```

---

## Self-Review

**Spec coverage:** §3.1 `order_reading_chain` → Task 1. §3.2 `GraphStore`/`FakeGraphStore` reads →
Task 2. §3.3 `Neo4jGraphStore` v2 (`_load_tx` labels/rel-types, read methods) → Tasks 4–5. §3.4
`GraphLoadStage` v2 (namespacing) → Task 3. §3.5 `graphquery` orchestrator → Task 7. Decisions 1–4
(namespacing, labels/rel-types, `doc_id` property, provenance bridge) → Tasks 3/4/7 with their
dedicated regression tests. Decision 5 (protocol extension, sync methods) → Task 2. Decision 6 (shared
pure ordering) → Task 1, consumed by Tasks 2/5. Decision 7 (route shapes) → Task 8. Decision 8
(citation-once-per-call) → Task 7's `_read_provenance`/`get_document` calls outside any per-node loop.
Decisions 9/10 (raw Cypher, stale nodes — both explicit forks, not built) → documented in spec §2 and
restated in Task 10's audit; NO task builds either. Decisions 11/12 (no new config; version bump) →
Task 3. §4 error handling → unit tests throughout Tasks 1, 2, 4, 5, 7; route-level 503/404 ordering →
Task 8. §5 testing → Tasks 1–9 map one-to-one onto the spec's test list. §6 deferred → confirmed absent
in Task 10's audit. §7 done criteria → Task 10.

**Placeholder scan:** every task (1–9) carries complete, runnable code — no task defers its
implementation to "see an existing file" without also giving the concrete diff/replacement inline.
Task 9 explicitly notes where its test bodies may duplicate fixture setup the executor should instead
reuse from `test_api.py`'s existing helpers (a documented judgment call, not a placeholder). Task 6 is
proof-only by design (opt-in, infra-gated) and says so explicitly rather than pretending a "run to
fail" step against infrastructure that may not exist in the executor's environment.

**Type consistency:** `order_reading_chain(Sequence[Mapping],Sequence[Mapping]) -> list[dict]` (Task 1)
consumed identically by `FakeGraphStore` (Task 2, pipeline layer) and `Neo4jGraphStore` (Task 5,
platform layer) — same function, same import, both layers legal per the layers contract. `GraphStore`
protocol methods (Task 2) are structurally satisfied by `Neo4jGraphStore` (Tasks 4–5) without an
`isinstance`/inheritance relationship, matching the existing `VectorStore`/`MilvusVectorStore`
precedent. `GraphLoadStage`'s namespaced node/edge shape (Task 3: `{id, type, label, doc_id}` /
`{source, target, type}`) is exactly what `GraphStore.load` (Task 2's protocol signature, unchanged)
and `Neo4jGraphStore._load_tx` (Task 4, reads `node["doc_id"]` via `.get`) both expect. `graphquery`'s
`_cite` (Task 7) consumes `GraphStore.neighbors`/`document_structure`/`reading_order`'s exact return
shapes from Tasks 2/5 (`{"node": ..., "relationship", "direction"}` / `{"document", "sections"}` /
`[...]`) unchanged. API schemas (Task 8) mirror `graphquery`'s dict shapes field-for-field
(`GraphCitationView` ↔ `_cite`'s `"citation"` sub-dict, `GraphNeighborsView.neighbors` ↔
`neighbors()`'s `"neighbors"` list).

**Risk flagged:** Task 5's `_document_structure_tx`/`_sections_in_reading_order` issue five separate
Cypher reads per call rather than one JOIN-shaped query — a deliberate correctness-over-round-trips
choice (avoids the row-multiplication/cartesian-product hazard of `OPTIONAL MATCH` chains across three
node types, spec §3.3) that trades a small number of extra round-trips for queries that are each
individually simple to verify correct; revisit only if `document_structure` latency is measured and
found to matter at real-corpus scale, not preemptively. Task 9's happy-path tests were verified against
`test_api.py`'s actual fixtures (read directly, not assumed) — `session_factory` and `store` already
exist in that file (lines ~88–112) with `store` deliberately pinned to bucket `"artifacts"` to match
`make_client`'s app default; Task 9 uses them as-is rather than reconstructing an engine/ArtifactStore
by hand, so there is no open "prefer X if present" judgment call left for the executor on this point.
