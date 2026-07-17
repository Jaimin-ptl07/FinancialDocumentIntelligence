# SP-9b — Neo4j Graph Loader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Project SP-9's canonical graph JSON artifact (`{schema_version, nodes:[{id,type,label}],
edges:[{source,target,type}]}`) into a real Neo4j graph database by running a `GraphLoadStage` (SP-2
derived stage, `deps=("graph",)`) that calls an injected `GraphStore` — proven end to end with a FAKE
graph store (no live Neo4j, no container in the default suite), plus an opt-in real-Neo4j smoke test.

**Architecture:** `GraphLoadStage` reads the graph artifact via the already-existing `ctx.read("graph")`,
calls `ctx.services["graph_store"].load(nodes, edges)` (fake in tests, `Neo4jGraphStore` in prod), and
returns a small deterministic receipt `{schema_version:1, loaded_nodes, loaded_edges}` — the Neo4j write
itself is the side effect and the actual point. Content-addressed on
`derived_input_hash([graph.output_hash])` + `GraphLoadStage.version` (SP-2's generic derived-stage
caching, already in `run_stage` — zero framework change). `GraphStore.load` runs idempotent Cypher `MERGE`
per node/edge inside one write transaction, so a cache-miss re-run against the SAME live Neo4j is always
safe; a cache-HIT re-run does NOT touch Neo4j at all (documented limitation — see design spec §2.6; force
a real reload by bumping `GraphLoadStage.version`).

**Tech Stack:** Python 3.13, `neo4j` (official pure-Python driver, ships `py.typed`), FastAPI, Celery,
SQLAlchemy, MinIO, Postgres, pytest + testcontainers, `uv`. Design:
`docs/superpowers/specs/2026-07-17-sp9b-neo4j-loader-design.md`.

## Global Constraints

- `uv run --directory backend <cmd>` — NEVER `uv --project`.
- Import contract `api | workers → pipeline → platform → core`, `exhaustive`. `core` stays UNTOUCHED —
  SP-9b adds nothing to `fdi.core` (the graph shape is already SP-9's `fdi.core.graph.build_graph`). The
  `Neo4jGraphStore` (does I/O, imports the `neo4j` driver) lives ONLY in `platform`. The `GraphStore`
  protocol + `GraphLoadStage` + `FakeGraphStore` live in `pipeline`.
- **Do not break SP-2/SP-5/SP-9/SP-10:** `StageContext`/`run_stage` are used AS-IS — no signature changes.
  Existing ping/ocr/normalize/ast/graph/semantic/... tests keep passing untouched.
- Determinism/caching note (deliberate limitation, spec §2.6): a second `graph_load` run on an unchanged
  `graph` artifact is a store cache hit that does NOT call `GraphStore.load` again — prove it with a call
  counter on the fake, same pattern as SP-10's `fake_semantic.calls == 1`.
- **No live Neo4j in the default suite:** all unit/integration tests use `FakeGraphStore`. The
  real-Neo4j test is `@pytest.mark.neo4j` — a NEW marker (registered alongside the existing `azure`
  marker), skipped unless `FDI_NEO4J_URI`/`FDI_NEO4J_PASSWORD` are set or a `testcontainers[neo4j]`
  container is used in-test.
- Any new `FDI_` field → Settings AND compose `x-backend-env` AND `.env.example` (inert-config lesson).
  `FDI_NEO4J_URI`/`FDI_NEO4J_PASSWORD` are `str | None = None` in **Settings** so the `core` profile boots
  without Neo4j configured; `.env.example`'s *values* are deliberately NOT blank (spec §2.7) — they mirror
  the working dev credentials this repo already ships for Postgres/MinIO/RabbitMQ, because the same var
  ALSO becomes the `neo4j` Compose service's own `NEO4J_AUTH` bootstrap credential.
- Integration tests: reuse `session_factory`/`store` fixtures; mint unique ids/checksums per test
  (session-scoped DB); use a LOCAL `StageRegistry` (not the global `REGISTRY`) for any test asserting
  exact chain behavior, same as `test_graph_stage.py`/`test_semantic_stage.py`/`test_provenance_stage.py`.
- On Windows prefix `lint-imports` with `PYTHONUTF8=1`.
- The `neo4j` service already exists in `docker/compose.yml` (profile `graph`, image `neo4j:5.26.28`,
  hardcoded `NEO4J_AUTH: neo4j/fdipassword`, no healthcheck) — Task 6 WIRES it to `FDI_NEO4J_*` and adds
  the healthcheck; it does not invent the service or change its pinned image tag.

**Canonical commands:** fast `pytest -m "not integration and not e2e and not azure and not neo4j" -q`;
integration `pytest tests/integration -q`; gates `ruff check .` · `mypy --strict src` ·
`PYTHONUTF8=1 lint-imports --config .importlinter`.

---

### Task 1: `GraphStore` protocol + `FakeGraphStore` (pipeline)

**Files:** Create `backend/src/fdi/pipeline/graphstore.py`; Test `backend/tests/unit/test_graphstore.py`.

**Interfaces:** `class GraphStore(Protocol)` with `def load(self, nodes: list[Mapping[str, object]], edges:
list[Mapping[str, object]]) -> dict[str, object]`. `class FakeGraphStore` (`__init__(self)`; `load` records
`self.calls += 1`, extends `self.loaded_nodes`/`self.loaded_edges`, returns `{"loaded_nodes": len(nodes),
"loaded_edges": len(edges)}`).

- [ ] **Step 1: Failing test**
```python
# backend/tests/unit/test_graphstore.py
from fdi.pipeline.graphstore import FakeGraphStore, GraphStore


def test_fake_records_calls_and_returns_counts() -> None:
    fake = FakeGraphStore()
    store: GraphStore = fake
    nodes = [{"id": "doc", "type": "document", "label": "ACME"}]
    edges = [{"source": "doc", "target": "s0", "type": "CONTAINS"}]
    receipt = store.load(nodes, edges)
    assert receipt == {"loaded_nodes": 1, "loaded_edges": 1}
    assert fake.calls == 1
    assert fake.loaded_nodes == nodes
    assert fake.loaded_edges == edges


def test_fake_accumulates_across_calls() -> None:
    fake = FakeGraphStore()
    fake.load([{"id": "a"}], [])
    fake.load([{"id": "b"}], [{"source": "a", "target": "b", "type": "X"}])
    assert fake.calls == 2
    assert len(fake.loaded_nodes) == 2
    assert len(fake.loaded_edges) == 1
```

- [ ] **Step 2: Run → fails.**
- [ ] **Step 3: Implement** — `Protocol` + `runtime_checkable`; `FakeGraphStore` with
  `calls`/`loaded_nodes`/`loaded_edges`.
- [ ] **Step 4: Run → passes.**
- [ ] **Step 5: Commit** — `git add backend/src/fdi/pipeline/graphstore.py backend/tests/unit/test_graphstore.py && git commit -m "feat(pipeline): GraphStore protocol + FakeGraphStore"`

---

### Task 2: `Neo4jGraphStore` (platform) + the `neo4j` dependency

**Files:** Add dep to `backend/pyproject.toml` (+ `uv.lock`); Create `backend/src/fdi/platform/graphstore.py`;
Test `backend/tests/unit/test_neo4j_graphstore.py`.

**Interfaces:** `class Neo4jGraphStore` (implements `GraphStore`): `__init__(self, *, uri, user,
password)` builds `self._driver = GraphDatabase.driver(uri, auth=(user, password))` at construction;
`load(self, nodes, edges) -> dict[str, object]` opens one session, runs `session.execute_write(self._load_tx,
nodes, edges)` (idempotent `MERGE` per node/edge), returns `{"loaded_nodes": N, "loaded_edges": M}`; a
`close()` method; plus `from_settings(settings) -> Neo4jGraphStore | None` (`None` when `uri`/`password`
unset).

- [ ] **Step 1: Add the dependency** — `uv add --directory backend neo4j`. Commit `pyproject.toml` +
  `uv.lock` with this task. Pin the version floor to the Compose server's major.minor for driver/protocol
  compatibility: `"neo4j>=5.26"` in `dependencies`.
- [ ] **Step 2: Failing test** — MONKEYPATCH the driver (no real Neo4j call): patch
  `fdi.platform.graphstore.GraphDatabase` with a fake whose `.driver(uri, auth=...)` returns a fake driver
  exposing `.session()` (context manager) → a fake session exposing `.execute_write(fn, *args)` (calls `fn`
  with a fake transaction that records every `tx.run(query, **kwargs)` call); assert `Neo4jGraphStore(...).load(...)`
  returns the right counts, the right number of `MERGE`-containing `tx.run` calls happened (one per node,
  one per edge), and each call's kwargs carry the right `id`/`type`/`label` or `source`/`target`/`type`;
  also `from_settings` returns `None` when `uri` is `None`.
```python
# backend/tests/unit/test_neo4j_graphstore.py — sketch
import pytest


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

    nodes = [{"id": "doc", "type": "document", "label": "ACME"}]
    edges = [{"source": "doc", "target": "s0", "type": "CONTAINS"}]
    receipt = store.load(nodes, edges)
    assert receipt == {"loaded_nodes": 1, "loaded_edges": 1}

    node_runs = [r for r in runs if "MERGE (n:Node" in r[0]]
    edge_runs = [r for r in runs if "MERGE (a)-[r:REL" in r[0]]
    assert len(node_runs) == 1 and node_runs[0][1] == {"id": "doc", "type": "document", "label": "ACME"}
    assert len(edge_runs) == 1
    assert edge_runs[0][1] == {"source": "doc", "target": "s0", "type": "CONTAINS"}


def test_from_settings_none_when_unconfigured() -> None:
    from fdi.platform.config import Settings
    from fdi.platform.graphstore import Neo4jGraphStore

    s = Settings(
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        broker_url="amqp://guest:guest@h:5672/%2F", result_backend="redis://h:6379/0",
        s3_endpoint="h:9000", s3_access_key="ak", s3_secret_key="sk",
    )
    assert Neo4jGraphStore.from_settings(s) is None
```
- [ ] **Step 3: Run → fails** (ModuleNotFoundError / import).
- [ ] **Step 4: Implement** — import `GraphDatabase`, `Driver`, `ManagedTransaction` from `neo4j` at
  module level (so monkeypatch works, same discipline as SP-10's `AzureOpenAIClient` importing
  `AzureOpenAI` at module level). `__init__` builds `self._driver = GraphDatabase.driver(uri,
  auth=(user, password))`. `load` opens `with self._driver.session() as session:` and calls
  `session.execute_write(self._load_tx, nodes, edges)`; `_load_tx` (a `@staticmethod`, signature
  `(tx: ManagedTransaction, nodes, edges) -> tuple[int, int]`) runs `MERGE (n:Node {id: $id}) SET
  n.type = $type, n.label = $label` per node and `MATCH (a:Node {id: $source}), (b:Node {id: $target})
  MERGE (a)-[r:REL {type: $type}]->(b)` per edge, returns `(len(nodes), len(edges))`. `close()` calls
  `self._driver.close()`. `from_settings` returns `None` if `uri`/`password` is `None`, else the store
  built from the other Settings fields (`user` defaults to `"neo4j"`).
- [ ] **Step 5: Run → passes**; `mypy --strict` — the `neo4j` driver ships `py.typed`, so this should need
  NO `# type: ignore`; if a real stub gap surfaces (e.g. `execute_write`'s generic callback signature),
  scope a narrow ignore with a reason comment, same discipline as SP-10's `AzureOpenAIClient`/SP-5's
  `AzureOcrClient`. Confirm `lint-imports` keeps the contract (`fdi/platform/graphstore.py` must NOT
  import from `fdi.pipeline`).
- [ ] **Step 6: Commit** — `git add backend/pyproject.toml backend/uv.lock backend/src/fdi/platform/graphstore.py backend/tests/unit/test_neo4j_graphstore.py && git commit -m "feat(platform): Neo4jGraphStore over the neo4j driver"`

---

### Task 3: `FDI_NEO4J_*` config (Settings + compose x-backend-env + env)

**Files:** Modify `backend/src/fdi/platform/config.py`, `docker/compose.yml` (`x-backend-env` only — the
`neo4j` service's own `environment:`/healthcheck is Task 6), `.env.example`; Test
`backend/tests/unit/test_config.py` (ADD).

**Interfaces:** Settings gains `neo4j_uri: str | None = None`, `neo4j_user: str = "neo4j"`,
`neo4j_password: str | None = None`.

- [ ] **Step 1: Add fields** to `config.py` beside the `gpt_*` fields:
```python
# Neo4j (graph loader, SP-9b). URI/password default to None so the stack boots
# without Neo4j configured (the `graph` Compose profile is opt-in, never part
# of `core`); the graph_load stage using them is wired separately. See
# docker/compose.yml x-backend-env, the `neo4j` service's NEO4J_AUTH, and
# .env.example. Unlike FDI_AZURE_DI_*/FDI_GPT_* (external SaaS, blank by
# default), .env.example ships REAL working values here: this var also drives
# the self-hosted `neo4j` Compose service's own bootstrap credential.
neo4j_uri: str | None = None
neo4j_user: str = "neo4j"
neo4j_password: str | None = None
```
  Add all three to `docker/compose.yml`'s `x-backend-env` block:
```yaml
FDI_NEO4J_URI: ${FDI_NEO4J_URI}
FDI_NEO4J_USER: ${FDI_NEO4J_USER}
FDI_NEO4J_PASSWORD: ${FDI_NEO4J_PASSWORD}
```
  Add to `.env.example` with REAL working defaults (NOT blank — see rationale above and spec §2.7):
```
# Neo4j (graph loader, SP-9b). Self-hosted via the `graph` Compose profile
# (docker compose --profile graph up) -- unlike Azure DI/OpenAI above, these
# are NOT blank: FDI_NEO4J_USER/FDI_NEO4J_PASSWORD also become the neo4j
# service's own NEO4J_AUTH bootstrap credential, so the profile must have a
# working password the moment it is started.
FDI_NEO4J_URI=bolt://neo4j:7687
FDI_NEO4J_USER=neo4j
FDI_NEO4J_PASSWORD=fdipassword
```
- [ ] **Step 2: Test** — a unit test that `Settings()` (with `REQUIRED` env set, per the existing
  `test_config.py` fixture pattern) exposes the Neo4j defaults:
```python
def test_neo4j_settings_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.neo4j_uri is None
    assert s.neo4j_user == "neo4j"
    assert s.neo4j_password is None
```
  Run → fails, implement (Step 1 already did), → passes.
- [ ] **Step 3: Gates + config consistency** — `ruff`, `mypy`, confirm the three fields are in Settings +
  compose `x-backend-env` + `.env.example`.
- [ ] **Step 4: Commit** — `git add backend/src/fdi/platform/config.py docker/compose.yml .env.example backend/tests/unit/test_config.py && git commit -m "feat(config): FDI_NEO4J_* settings"`

---

### Task 4: `GraphLoadStage` (pipeline/stages) — proven with `FakeGraphStore`

**Files:** Create `backend/src/fdi/pipeline/stages/graphload.py`; Modify
`backend/src/fdi/pipeline/stages/__init__.py` (register it); Test
`backend/tests/integration/test_graphload_stage.py`.

**Interfaces:** `class GraphLoadStage` (`name="graph_load"`, `version=1`, `deps=("graph",)`); `run(ctx)`:
reads `graph = await ctx.read("graph")`; `store = cast(GraphStore, ctx.services["graph_store"])`; narrows
`nodes`/`edges` to lists of dicts (defensive, mirrors `SemanticStage`/`build_graph`'s own `isinstance`
narrowing); calls `store.load(nodes, edges)`; returns `{"schema_version":1, "loaded_nodes", "loaded_edges"}`.
Self-registers into `REGISTRY`.

- [ ] **Step 1: Failing test** (integration; run `ocr → normalize → ast → graph → graph_load` via
  `run_stage` in a LOCAL `StageRegistry`, `FakeOcrClient` + `FakeGraphStore`, mirroring
  `test_graph_stage.py`/`test_semantic_stage.py`'s shape exactly):
```python
# backend/tests/integration/test_graphload_stage.py — sketch
import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.pipeline.graphstore import FakeGraphStore
from fdi.pipeline.intake import intake_document
from fdi.pipeline.keys import artifact_key, derived_input_hash
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.run import run_stage
from fdi.pipeline.stages.ast import AstStage
from fdi.pipeline.stages.graph import GraphStage
from fdi.pipeline.stages.graphload import GraphLoadStage
from fdi.pipeline.stages.normalize import NormalizeStage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.platform.db.engine import make_session_factory
from fdi.platform.db.repository import RegistryRepository
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


async def test_graph_load_chains_off_graph_and_caches(session_factory, store) -> None:
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="a.pdf", doc_key=str(uuid.uuid4()), content_type="application/pdf",
        session_factory=session_factory, store=store,
    )
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

    assert "graph_load" in resolver.successors("graph")  # the chain is automatic

    common = {
        "doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
        "store": store, "registry": reg, "resolver": resolver,
    }
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake_ocr}, **common)
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
    gl_services = {"graph_store": fake_store}
    await run_stage(stage_name="graph_load", run_id=str(uuid.uuid4()), services=gl_services, **common)
    # Second run: content-addressed cache hit -- must NOT call the fake store twice.
    await run_stage(stage_name="graph_load", run_id=str(uuid.uuid4()), services=gl_services, **common)
    assert fake_store.calls == 1

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    key = artifact_key(doc.id, "graph_load", 1, derived_input_hash([facts["graph"].output_hash]))
    body = await store.get_json(key)
    assert body == {"schema_version": 1, "loaded_nodes": 3, "loaded_edges": 2}
```
  (Node/edge counts: `doc`, `s0`, `s0p0` = 3 nodes; `doc→s0` CONTAINS + `s0→s0p0` CONTAINS = 2 edges — the
  same graph SP-9's own `test_graph_stage.py` produces from this exact fixture; reuse that file's expected
  shape as the source of truth rather than recomputing by hand.)
- [ ] **Step 2: Run → fails.**
- [ ] **Step 3: Implement** the stage (+ register in `stages/__init__.py`, alphabetically after `graph`:
  `from fdi.pipeline.stages import graphload as graphload  # noqa: F401 - import for its registration side effect`).
- [ ] **Step 4: Run → passes** (cache hit → `fake_store.calls == 1`); full non-e2e suite green; scan
  `test_provenance_stage.py`'s `"provenance" in resolver.successors("graph")` assertion — it is already a
  membership check, so registering `GraphLoadStage` (which also adds `"graph_load"` to
  `successors("graph")`) needs no retrofit there.
- [ ] **Step 5: Commit** — `git add backend/src/fdi/pipeline/stages backend/tests/integration/test_graphload_stage.py && git commit -m "feat(pipeline): GraphLoadStage over the SP-9 graph artifact"`

---

### Task 5: Wire the graph store into the worker + API trigger

**Files:** Modify `backend/src/fdi/workers/tasks.py` (`_run_stage_async` builds
`services["graph_store"] = Neo4jGraphStore.from_settings(...)` alongside the existing `"ocr"`/`"semantic"`
entries); `backend/src/fdi/api/routes.py` (`POST /documents/{id}/graph_load`); `backend/src/fdi/api/schemas.py`
(`GraphLoadTriggerView`); Test `backend/tests/integration/test_api.py` (ADD).

**Interfaces:** `_run_stage_async` extends its `services` dict with an optional `"graph_store"` entry
(from Settings; omitted if unconfigured — same pattern as `"ocr"`/`"semantic"`). `POST
/documents/{doc_id}/graph_load` → 202: `dispatcher.dispatch(doc_id, "graph_load", correlation_id)`, 404 if
the document is unknown.

- [ ] **Step 1: Read** `workers/tasks.py`'s `_run_stage_async` (currently builds `"ocr"` and `"semantic"`)
  and `routes.py`'s `trigger_semantic` (the route to mirror exactly).
- [ ] **Step 2: Failing test** — `POST /documents/{id}/graph_load` on an intake'd doc → 202; unknown id →
  404 (mirrors the existing semantic trigger test in `test_api.py`).
```python
# backend/tests/integration/test_api.py — additions
async def test_trigger_graph_load_on_a_document(client: AsyncClient, _alembic_applied) -> None:
    pdf = f"PDF-GRAPHLOAD-{uuid.uuid4()}".encode()
    up = await client.post("/documents", files={"file": ("a.pdf", pdf, "application/pdf")})
    doc_id = up.json()["id"]
    r = await client.post(f"/documents/{doc_id}/graph_load")
    assert r.status_code == 202
    assert r.json() == {"doc_id": doc_id, "stage": "graph_load"}


async def test_trigger_graph_load_unknown_document_is_404(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.post("/documents/00000000-0000-0000-0000-000000000000/graph_load")
    assert r.status_code == 404
```
- [ ] **Step 3: Implement**:
```python
# workers/tasks.py, inside _run_stage_async, beside the existing oc/sc entries
from fdi.platform.graphstore import Neo4jGraphStore
oc = AzureOcrClient.from_settings(get_settings())
sc = AzureOpenAIClient.from_settings(get_settings())
gsc = Neo4jGraphStore.from_settings(get_settings())
services: dict[str, object] = {}
if oc is not None:
    services["ocr"] = oc
if sc is not None:
    services["semantic"] = sc
if gsc is not None:
    services["graph_store"] = gsc
```
```python
# api/schemas.py
class GraphLoadTriggerView(BaseModel):
    doc_id: str
    stage: str


# api/routes.py, mirroring trigger_semantic
@router.post(
    "/documents/{doc_id}/graph_load",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=GraphLoadTriggerView,
)
async def trigger_graph_load(
    doc_id: str, session: SessionDep, correlation_id: CorrelationIdDep, dispatcher: DispatcherDep
) -> GraphLoadTriggerView:
    if await RegistryRepository(session).get_document(doc_id) is None:
        raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
    dispatcher.dispatch(doc_id, "graph_load", correlation_id)
    return GraphLoadTriggerView(doc_id=doc_id, stage="graph_load")
```
- [ ] **Step 4: Run → passes**; full non-e2e suite green; gates green.
- [ ] **Step 5: Commit** — `git add backend/src/fdi/workers/tasks.py backend/src/fdi/api/routes.py backend/src/fdi/api/schemas.py backend/tests/integration/test_api.py && git commit -m "feat: wire Neo4jGraphStore into the worker; POST /documents/{id}/graph_load"`

---

### Task 6: Compose `neo4j` service wiring + `@pytest.mark.neo4j` registration

**Files:** Modify `docker/compose.yml` (the existing `neo4j` service's `environment:` + a new
`healthcheck:`); Modify `backend/pyproject.toml` (register the `neo4j` pytest marker). The `neo4j` driver
dependency itself was already added in Task 2 — this task only touches the Compose service definition and
the pytest marker list.

**Interfaces:** the `neo4j` service (profile `graph`) moves from a hardcoded `NEO4J_AUTH: neo4j/fdipassword`
to `NEO4J_AUTH: ${FDI_NEO4J_USER}/${FDI_NEO4J_PASSWORD}` and gains a `cypher-shell`-based healthcheck.
`pyproject.toml`'s `[tool.pytest.ini_options] markers` gains `"neo4j: needs a live Neo4j instance
(FDI_NEO4J_URI/FDI_NEO4J_PASSWORD, or testcontainers[neo4j])"`.

- [ ] **Step 1: Update the `neo4j` service** in `docker/compose.yml`:
```yaml
  neo4j:
    # Pinned exactly: neo4j:latest is the CalVer line, a DIFFERENT lineage from
    # 5.26 LTS. Wired at SP-9b: NEO4J_AUTH derives from the same FDI_NEO4J_USER/
    # FDI_NEO4J_PASSWORD the app connects with -- one source of truth, not a
    # hardcoded credential that happens to agree with Settings by coincidence.
    image: neo4j:5.26.28
    profiles: [graph]
    environment:
      NEO4J_AUTH: ${FDI_NEO4J_USER}/${FDI_NEO4J_PASSWORD}
    volumes:
      - neo4jdata:/data
    ports: ["7474:7474", "7687:7687"]
    healthcheck:
      # $$ escapes compose interpolation so the shell expands it in-container,
      # same discipline as the postgres/worker healthchecks above. cypher-shell
      # ships in the official neo4j image (no curl needed, same reasoning as
      # minio's `mc`-based healthcheck).
      test: ["CMD-SHELL", "cypher-shell -u $${FDI_NEO4J_USER} -p $${FDI_NEO4J_PASSWORD} 'RETURN 1'"]
      interval: 15s
      timeout: 10s
      retries: 5
      start_period: 30s
```
  Since `neo4j` is in `x-backend-env` (Task 3) but the `neo4j` service itself does NOT use the
  `*backend-env` anchor (it is not a `backend` image), `FDI_NEO4J_USER`/`FDI_NEO4J_PASSWORD` must resolve
  from the top-level `.env` the same way `POSTGRES_USER`/`POSTGRES_PASSWORD` already do for the `postgres`
  service — no anchor needed, Compose reads `.env` for every service's `environment:` block directly.
- [ ] **Step 2: Register the marker** in `backend/pyproject.toml`:
```toml
markers = [
    "integration: needs Docker containers",
    "e2e: needs the compose stack running",
    "azure: needs live Azure credentials (Document Intelligence and/or OpenAI)",
    "neo4j: needs a live Neo4j instance (FDI_NEO4J_URI/FDI_NEO4J_PASSWORD, or testcontainers[neo4j])",
]
```
- [ ] **Step 3: Smoke-check** (manual, not a pytest test): `docker compose -f docker/compose.yml
  --env-file .env --profile graph up -d neo4j` with `.env` copied fresh from `.env.example` (Task 3's real
  defaults) → the container reaches `healthy` using the SAME `FDI_NEO4J_USER`/`FDI_NEO4J_PASSWORD` the app
  would connect with. `docker compose --profile graph down -v` after.
- [ ] **Step 4: Gates** — `ruff`, `mypy --strict src` (no source changed, should be a no-op check),
  `PYTHONUTF8=1 lint-imports`.
- [ ] **Step 5: Commit** — `git add docker/compose.yml backend/pyproject.toml && git commit -m "build(compose): wire FDI_NEO4J_* into the neo4j service; register the neo4j pytest marker"`

---

### Task 7: Opt-in real-Neo4j smoke test

**Files:** Create `backend/tests/neo4j/__init__.py`, `backend/tests/neo4j/test_real_graph_load.py`.

**Interfaces:** `@pytest.mark.neo4j`, skipped unless `FDI_NEO4J_URI` and `FDI_NEO4J_PASSWORD` env are set
(mirrors `tests/azure/test_real_semantic.py`'s skip pattern exactly). Builds
`Neo4jGraphStore.from_settings(get_settings())`, loads a tiny two-node/one-edge graph against the real
instance, asserts the receipt counts and (since a real instance is reachable) reads the nodes back via a
`MATCH` to confirm they actually landed.

- [ ] **Step 1: Create the empty test package** — `backend/tests/neo4j/__init__.py` (empty, mirrors
  `tests/azure/__init__.py`).
- [ ] **Step 2: Write the test** with a module-level skip:
```python
# backend/tests/neo4j/test_real_graph_load.py
"""Opt-in smoke test against a real Neo4j instance.

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


def test_real_load_persists_nodes_and_edges() -> None:
    store = Neo4jGraphStore.from_settings(get_settings())
    assert store is not None

    doc_id = f"sp9b-smoke-{uuid.uuid4()}"
    nodes = [
        {"id": f"{doc_id}-doc", "type": "document", "label": "ACME"},
        {"id": f"{doc_id}-s0", "type": "section", "label": "Directors' Report"},
    ]
    edges = [{"source": f"{doc_id}-doc", "target": f"{doc_id}-s0", "type": "CONTAINS"}]

    receipt = store.load(nodes, edges)
    assert receipt == {"loaded_nodes": 2, "loaded_edges": 1}

    with store._driver.session() as session:  # read-back proof, this test only
        count = session.run(
            "MATCH (n:Node {id: $id}) RETURN count(n) AS c", id=f"{doc_id}-doc"
        ).single()["c"]
    assert count == 1
    store.close()
```
  (Reading `store._driver` directly is acceptable ONLY in this opt-in smoke test — it exists purely to
  prove the write landed against a real instance; no production code reaches into `Neo4jGraphStore`'s
  private driver.)
- [ ] **Step 3 (alternative, if no operator instance is configured): `testcontainers[neo4j]`** — add
  `testcontainers[neo4j]` to the `dev` dependency group (`uv add --directory backend --group dev
  "testcontainers[neo4j]"`), and use `Neo4jContainer("neo4j:5.26.28")` in a session-scoped fixture that
  yields a real bolt URI + credentials for the duration of this one opt-in test module — still gated by
  `@pytest.mark.neo4j` so it never starts implicitly in the default suite. Either approach (real instance
  via env, or an ad-hoc container) satisfies the design's "opt-in, never in the default suite" requirement;
  pick whichever the implementer's environment makes easier — the design does not mandate one.
- [ ] **Step 4: Run** `uv run --directory backend pytest tests/neo4j -rs` → skipped (Neo4j unconfigured in
  this env, honest skip alongside the existing OCR/OpenAI smoke tests). If the operator sets `FDI_NEO4J_*`
  or a container fixture is used, it runs for real.
- [ ] **Step 5: Commit** — `git add backend/tests/neo4j backend/pyproject.toml backend/uv.lock && git commit -m "test(neo4j): opt-in real Neo4j graph-load smoke test"`

---

### Task 8: Audit + docs

**Files:** SP-9b spec status; `docs/architecture/ingestion-roadmap.md` (SP-9's roadmap row/handoff note →
add the SP-9b follow-through; the "Canonical Graph → Neo4j" phase-6 row can now read "loading" as done too).

- [ ] **Step 1: Static gates** — `ruff`, `mypy --strict src`, `PYTHONUTF8=1 lint-imports` (`1 kept, 0
  broken`; `core` untouched; `neo4j` driver import ONLY in `fdi/platform/graphstore.py`). Config
  consistency: `FDI_NEO4J_*` in Settings + compose `x-backend-env` + `.env.example`; the `neo4j` service's
  `NEO4J_AUTH` derives from the same two vars.
- [ ] **Step 2: Full suite** — `uv run --directory backend pytest -q` (green; e2e + azure + neo4j skip; NO
  live Neo4j call).
- [ ] **Step 3: Audit spec §7** — map each done criterion (1 GraphStore/FakeGraphStore, 2 Neo4jGraphStore,
  3 GraphLoadStage, 4 content-address+cache-no-2nd-load, 5 config+healthcheck, 6 POST trigger, 7 opt-in
  smoke skips, 8 gates+core untouched) to its test.
- [ ] **Step 4: Spec status → Implemented `<date>`.**
- [ ] **Step 5: Roadmap update** — SP-9's row/handoff note: the `graph` Compose profile is now wired (not
  just provisioned), `GraphLoadStage` (`deps=("graph",)`) projects SP-9's JSON graph into Neo4j via an
  injected `GraphStore`, a cache hit does not re-run the load (bump `GraphLoadStage.version` to force a
  reload), and node ids are only unique per-document — cross-document graph merging is explicitly SP-16's
  job, not this loader's (§6). Note this for whichever sub-project builds a read/query API over the loaded
  graph (deferred here).
- [ ] **Step 6: Commit** — `git add docs && git commit -m "docs: SP-9b done-criteria audit, spec status, roadmap"`

## Self-Review

**Spec coverage:** §2 decisions → T1(GraphStore/FakeGraphStore) T2(Neo4jGraphStore+driver dep)
T3(config) T4(GraphLoadStage+cache) T5(wire+trigger) T6(compose+marker) T7(opt-in real). §3 architecture →
all (3.1 no-change implicit in T4 reusing existing framework). §4 errors → T4 (unconfigured service, cache
hit never re-runs, malformed graph degrades, zero-node/edge valid), T5 (`from_settings` None omitted from
services). §5 testing → unit T1/T2/T3, integration T4/T5, neo4j T7, compose T6. §7 done criteria → T8 audit.

**Type consistency:** `GraphStore.load(nodes: list[Mapping[str, object]], edges: list[Mapping[str,
object]]) -> dict[str, object]` (T1) matches `FakeGraphStore` (T1), `Neo4jGraphStore` (T2), and
`GraphLoadStage` usage (T4). `StageContext.read`/`services` (pre-existing, confirmed via SP-9/SP-10
precedent) match `GraphLoadStage` (T4). `build_graph`'s node/edge shape (pre-existing, SP-9) is what T4
narrows and feeds to `GraphStore.load`. `run_stage(..., services=)` (pre-existing) matches T4/T5 calls.
`Neo4jGraphStore.from_settings` (T2) matches T5.

**Placeholder scan:** T6's "no anchor needed, Compose reads `.env` directly" is a precise statement about
how the `neo4j` service (unlike `api`/`worker`) is NOT built from `*backend-env` — not a gap. T7's
container-vs-real-instance choice is deliberately left to the implementer (both satisfy the same opt-in
requirement) — not an unresolved design question, an intentionally flexible implementation detail. No
TODO/TBD.
