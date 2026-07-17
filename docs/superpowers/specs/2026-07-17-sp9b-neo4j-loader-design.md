# SP-9b — Neo4j Graph Loader (SP-9's deferred follow-on)

- **Status:** Draft 2026-07-17 (spec + TDD plan only; not yet implemented). When implemented: branch
  `feature/sp-9b-neo4j-loader`, worktree `D:\fdi-wt\sp9b` (mirrors SP-10's worktree convention).
- **Depends on:** SP-9 (the canonical graph JSON artifact, `fdi.core.graph.build_graph` →
  `{schema_version, nodes:[{id,type,label}], edges:[{source,target,type}]}`, and
  `fdi.pipeline.stages.graph.GraphStage`, `deps=("ast",)`), SP-2 (stage framework — `StageContext.services`,
  content-addressed derived-stage caching), SP-10 (the injected-mockable-client + stage + config +
  worker-wiring + opt-in-real-service pattern this spec mirrors exactly).
- **Roadmap:** `docs/architecture/ingestion-roadmap.md` §3 table — "SP-9 | Canonical Graph → Neo4j | ✅ done
  (JSON graph; Neo4j loading deferred)" and its handoff note: *"A future sub-project must add the `graph`
  Compose profile (Neo4j container) and a loader projecting SP-9's JSON artifact ... into Neo4j, keyed by
  the same `id`s — no schema change anticipated."* SP-9b is that sub-project. SP-9's own §6 (Explicitly
  deferred) says the same thing in its own words: *"Neo4j loading. A `graph` Compose profile (Neo4j
  container) + a loader that syncs the JSON graph artifact into Neo4j (nodes/edges as Cypher `MERGE`s,
  keyed by the same `id`s) is a later, separate sub-project."*
- **Compose already has a `neo4j` service** (added in the project-setup "profiled stack" pass, commit
  `d1fa75b`): `image: neo4j:5.26.28`, `profiles: [graph]`, `environment: {NEO4J_AUTH:
  neo4j/fdipassword}` (hardcoded), a `neo4jdata` volume, ports `7474`/`7687`, **no healthcheck**, and a
  comment reading "Defined now; wired at SP-9" (SP-9 explicitly did not wire it — SP-9b is the actual
  wiring). SP-9b does not invent this service; it wires `FDI_NEO4J_*` config through it (§3.6) and adds
  the healthcheck every other stateful service in the file already has.

## 1. Purpose

Project SP-9's canonical graph JSON artifact — the source of truth — into a real Neo4j graph database, so
a graph query/traversal surface exists over document structure. This is a thin vertical slice: an injected
`GraphStore` protocol, a `Neo4jGraphStore` platform client wrapping the official `neo4j` Python driver, a
derived `GraphLoadStage` (`deps=("graph",)`), config wiring, and an opt-in real-Neo4j smoke test — proven
end to end with a **fake graph store** (no live Neo4j, no container, no cost) in the default suite.

Structurally this is **SP-10 again**, substituting Neo4j-the-database for Azure-OpenAI-the-LLM: same
mockable-client-protocol shape, same fake-for-tests / real-client-for-prod split, same config discipline,
same derived-stage wiring, same opt-in-marker pattern for the live external dependency. A read/query API
over the loaded graph (retrieval) is explicitly deferred (§6) — this slice only writes.

## 2. Scope decisions (made autonomously, mirroring SP-10's grounded pattern)

1. **The database write is an injected `GraphStore` protocol**, not a direct Neo4j import in `pipeline` or
   `core`. The suite runs against a `FakeGraphStore` (in-memory, no network); `Neo4jGraphStore` (real driver)
   is used in production plus an opt-in smoke test. Same SP-3/SP-5/SP-10 lesson: the loading engine is a
   swappable candidate — a different graph DB (or a batched loader, §6) is just another `GraphStore`.
2. **Driver = `neo4j` (the official pure-Python driver), `GraphDatabase.driver(uri, auth=(user, password))`
   + one session per `load()` call.** No dependency on a higher-level OGM (`neomodel`, `py2neo`) — the
   artifact's shape (`{id, type, label}` nodes, `{source, target, type}` edges) is already exactly what a
   handful of parameterized Cypher statements need; an OGM would add a schema-mapping layer this slice has
   no use for.
3. **`load` runs one `MERGE` per node and one `MERGE` per edge, all inside a single write transaction
   (`session.execute_write`).** `MERGE` (not `CREATE`) makes the load **idempotent by construction**: the
   same graph loaded twice produces the same node/relationship set, not duplicates — this is what makes a
   derived stage's cache-hit re-run (§4 below) and an operator's manual re-load both safe. Nodes are keyed
   by their SP-9 `id` (`MERGE (n:Node {id: $id})`); edges are matched by their endpoints' `id`s and merged
   as a typed relationship (`MERGE (a)-[r:REL {type: $type}]->(b)`) — a single generic `:Node` label and a
   single generic `:REL` relationship type, with the SP-9 `type` string (`"document"`/`"section"`/
   `"paragraph"`, `"CONTAINS"`/`"READING_ORDER"`) carried as a **property**, not as the Neo4j label/rel-type
   itself. A dynamic Cypher label per node type (`MERGE (n:\`${type}\` {id: $id})`) would need
   string-interpolated Cypher (parameters cannot bind labels/rel-types in Neo4j) — an injection surface and
   a schema that grows a new label per SP-9 node type change. Fixed `:Node`/`:REL` with a `type` property is
   simpler, safer, and exactly as queryable (`MATCH (n:Node {type: "section"})`).
4. **`GraphLoadStage` is a DERIVED stage** (`name="graph_load"`, `version=1`, `deps=("graph",)`) — same
   shape as SP-9's own `GraphStage` (`deps=("ast",)`) and SP-10's `SemanticStage` (`deps=("ast",)`). It
   reads the graph artifact via `ctx.read("graph")` and calls the injected `GraphStore.load(nodes, edges)`.
   Zero stage-framework changes needed — `StageContext.services` and derived-stage `input_hash`
   (`derived_input_hash([facts["graph"].output_hash])`) already exist (added by SP-5, exercised by every
   derived stage since).
5. **The stored artifact is a small deterministic RECEIPT, not the graph data itself** — the write to
   Neo4j is the actual point (the side effect); the artifact `{schema_version:1, loaded_nodes: int,
   loaded_edges: int}` exists only so the stage fits SP-2's "every stage produces a stored, content-addressed
   body" contract and so a UI/API can confirm a load happened and how large it was, without re-fetching
   Neo4j. This mirrors SP-9's own "the JSON artifact is the source of truth; Neo4j is a projection, not a
   parallel write" call, one level down: here the *receipt* is the artifact, Neo4j is the projection.
6. **Deliberate limitation, stated explicitly (mirrors SP-10's determinism-exception treatment): a cache
   hit on `graph_load` does NOT re-run `GraphStore.load`.** SP-2's generic derived-stage caching means a
   second `graph_load` dispatch against an unchanged `graph` artifact finds the receipt already stored at
   `artifact_key(doc, "graph_load", 1, derived_input_hash([graph.output_hash]))` and skips `stage.run()`
   entirely (§3 of `run_stage`) — so if an operator wipes the Neo4j database and wants to reload, re-running
   `graph_load` on the SAME document is a no-op (the cached receipt satisfies the cache check; Neo4j is
   never touched again). This is the same tradeoff SP-9/SP-10 already accepted for their own derived
   stages (content-addressed caching is a property of the framework, not opt-out per stage) — the
   documented workaround is the same one SP-10 documents for a changed prompt: **bump
   `GraphLoadStage.version`** to force every document's `graph_load` to regenerate (and thus re-`MERGE`
   into whatever Neo4j instance is live). Because `load` is idempotent `MERGE`, re-running against the
   *same* live Neo4j is always safe when the version bump does fire — the risk is purely "stale receipt,
   no re-run", never "duplicate data".
7. **Config: `FDI_NEO4J_URI` / `FDI_NEO4J_USER` / `FDI_NEO4J_PASSWORD`, with a deliberate split from the
   Azure blank-by-default convention.** `Settings` fields: `neo4j_uri: str | None = None`, `neo4j_user: str
   = "neo4j"`, `neo4j_password: str | None = None` — `from_settings` returns `None` unless URI+password are
   both set, exactly like `AzureOcrClient`/`AzureOpenAIClient.from_settings`, so the `core` Compose profile
   (which never starts `neo4j`) boots with `graph_store` simply absent from `services`. **But
   `.env.example`'s *values* deliberately do NOT mirror the Azure keys' blank defaults** — `FDI_NEO4J_URI`,
   `FDI_NEO4J_USER`, and `FDI_NEO4J_PASSWORD` ship real, working values (`bolt://neo4j:7687` / `neo4j` /
   `fdipassword`) that exactly match what the `neo4j` Compose service's `NEO4J_AUTH` now derives from (§3.7).
   Rationale: Azure DI/OpenAI are genuinely external SaaS the operator may not have credentials for yet —
   blank is the honest "not configured" state. Neo4j here is a **Compose-managed, self-hosted service this
   repo itself boots** (same category as Postgres/MinIO/RabbitMQ, whose `.env.example` credentials are
   likewise real working dev values — `POSTGRES_PASSWORD=fdi`, `MINIO_ROOT_PASSWORD=minioadmin` — never
   blank), and `docker compose --profile graph up` must keep working out of the box the moment this
   sub-project lands, exactly as it silently does today via the hardcoded `NEO4J_AUTH: neo4j/fdipassword`.
   Shipping a blank `FDI_NEO4J_PASSWORD` would be a regression: `NEO4J_AUTH: neo4j/` is not a valid
   credential (Neo4j 5.x rejects a password under 8 characters at bootstrap) and the container would fail
   to start the first time anyone actually turned the `graph` profile on. This exact "self-hosted stateful
   service gets a real default; external SaaS key gets a blank one" split already exists in this file today
   — `FDI_S3_PUBLIC_ENDPOINT=localhost:9000` (Settings-level `str | None = None`, but a real, non-blank
   `.env.example` value) is the precedent.
8. **`neo4j_user` defaults to `"neo4j"` (a plain `str`, not `str | None`)** — Neo4j's own bootstrap always
   names the default admin user `neo4j`; there is no "unset" state worth representing (unlike endpoint/
   password, which gate whether the feature is configured at all). Mirrors `azure_di_model: str =
   "prebuilt-layout"` / `gpt_deployment: str = "gpt-4o"` — a non-`None` default is right whenever the field
   is a tunable-but-always-present setting, not a configured/unconfigured toggle.
9. **A single generic `MERGE` per node/edge, not a batched `UNWIND`.** For the thin-slice, few-hundred-node
   graphs SP-9 currently emits (document → sections → paragraphs, one small annual report at a time), one
   `tx.run(...)` per node/edge inside one transaction is simple and fully correct; `UNWIND $rows AS row
   MERGE (...)`-style batching (fewer round-trips for a 100k-node graph) is deferred (§6) until the corpus
   scale (SP-13/14) makes per-row round-trips measurably slow — the same "naive-then-deepen" call the
   roadmap makes everywhere else (build order §4).

## 3. Architecture

### 3.1 No stage-framework change needed (same as SP-10 §3.1)

`GraphLoadStage` is a **derived** stage reading an upstream artifact via `ctx.read("graph")` — exactly
`GraphStage`'s and `SemanticStage`'s shape. `StageContext.services` and the derived-stage `input_hash`
(`derived_input_hash([facts["graph"].output_hash])`) already exist. Zero changes to
`fdi/pipeline/stage.py` or `fdi/pipeline/run.py`.

### 3.2 `GraphStore` (protocol + fake), `fdi.pipeline.graphstore`

```python
"""The GraphStore seam: a `load(nodes, edges) -> {loaded_nodes, loaded_edges}`
protocol. `Neo4jGraphStore` (platform, does I/O) structurally satisfies this
without importing it -- `platform` may not import `pipeline`. `FakeGraphStore`
lives here for tests: no live Neo4j call anywhere in the suite."""

from collections.abc import Mapping
from typing import Protocol, runtime_checkable


@runtime_checkable
class GraphStore(Protocol):
    def load(
        self, nodes: list[Mapping[str, object]], edges: list[Mapping[str, object]]
    ) -> dict[str, object]: ...


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
```

### 3.3 `Neo4jGraphStore` (platform, does I/O), `fdi.platform.graphstore`

```python
"""Neo4jGraphStore -- GraphStore backed by a real Neo4j instance via the
official `neo4j` driver.

Lives in `platform` (does I/O), not `pipeline`: the `GraphStore` protocol and
`FakeGraphStore` live in `fdi.pipeline.graphstore`. This module structurally
satisfies that protocol without importing it -- `platform` may not import
`pipeline` per the layered architecture contract.

`GraphDatabase` is imported at module level (rather than inside `__init__`) so
tests can monkeypatch `fdi.platform.graphstore.GraphDatabase` with a fake --
no live Neo4j call in the suite."""

from collections.abc import Mapping

from neo4j import Driver, GraphDatabase, ManagedTransaction

from fdi.platform.config import Settings


class Neo4jGraphStore:
    """GraphStore backed by Neo4j. Lives in platform (does I/O)."""

    def __init__(self, *, uri: str, user: str, password: str) -> None:
        self._driver: Driver = GraphDatabase.driver(uri, auth=(user, password))

    def load(
        self, nodes: list[Mapping[str, object]], edges: list[Mapping[str, object]]
    ) -> dict[str, object]:
        with self._driver.session() as session:
            loaded_nodes, loaded_edges = session.execute_write(
                self._load_tx, nodes, edges
            )
        return {"loaded_nodes": loaded_nodes, "loaded_edges": loaded_edges}

    @staticmethod
    def _load_tx(
        tx: ManagedTransaction,
        nodes: list[Mapping[str, object]],
        edges: list[Mapping[str, object]],
    ) -> tuple[int, int]:
        for node in nodes:
            tx.run(
                "MERGE (n:Node {id: $id}) SET n.type = $type, n.label = $label",
                id=node["id"], type=node["type"], label=node["label"],
            )
        for edge in edges:
            tx.run(
                "MATCH (a:Node {id: $source}), (b:Node {id: $target}) "
                "MERGE (a)-[r:REL {type: $type}]->(b)",
                source=edge["source"], target=edge["target"], type=edge["type"],
            )
        return len(nodes), len(edges)

    def close(self) -> None:
        self._driver.close()

    @staticmethod
    def from_settings(settings: "Settings") -> "Neo4jGraphStore | None":
        if not settings.neo4j_uri or not settings.neo4j_password:
            return None
        return Neo4jGraphStore(
            uri=settings.neo4j_uri,
            user=settings.neo4j_user,
            password=settings.neo4j_password,
        )
```
`close()` is not called anywhere yet (`_run_stage_async` builds `Neo4jGraphStore.from_settings(...)` fresh
per task invocation and never disposes it, same as every other platform SDK client in this codebase —
`AzureOcrClient`/`AzureOpenAIClient` are neither closed nor pooled across tasks today). Documented, not
fixed here: connection-pool lifecycle across Celery task invocations is a house-wide pattern, not a
SP-9b-specific gap.

### 3.4 `GraphLoadStage` (`fdi.pipeline.stages.graphload`)

```python
"""GraphLoadStage: a DERIVED stage that projects the SP-9 graph artifact
(nodes/edges) into a Neo4j-shaped GraphStore via an injected client.
Auto-chains off GraphStage via `deps=("graph",)`. The artifact is a small
deterministic receipt (node/edge counts) -- Neo4j is the side effect and the
actual point; see spec §2.5-6 for why a cache hit does not re-run the load
(the receipt satisfies SP-2's content-addressed cache, `GraphStore.load`
is idempotent MERGE, and a version bump forces a real reload)."""

from collections.abc import Mapping
from typing import cast

from fdi.pipeline.graphstore import GraphStore
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class GraphLoadStage:
    name = "graph_load"
    version = 1
    deps: tuple[str, ...] = ("graph",)

    async def run(self, ctx: StageContext) -> object:
        graph = cast(Mapping[str, object], await ctx.read("graph"))
        store = cast(GraphStore, ctx.services["graph_store"])

        nodes_raw = graph.get("nodes")
        edges_raw = graph.get("edges")
        nodes = [n for n in nodes_raw if isinstance(n, dict)] if isinstance(nodes_raw, list) else []
        edges = [e for e in edges_raw if isinstance(e, dict)] if isinstance(edges_raw, list) else []

        receipt = store.load(nodes, edges)
        return {
            "schema_version": 1,
            "loaded_nodes": receipt["loaded_nodes"],
            "loaded_edges": receipt["loaded_edges"],
        }


REGISTRY.register(GraphLoadStage())
```
Self-registers into `REGISTRY`; `stages/__init__.py` gains `from fdi.pipeline.stages import graphload as
graphload  # noqa: F401 - import for its registration side effect`, alphabetically ordered (`ast, graph,
graphload, linking, normalize, ocr, ontology, ping, provenance, quality, semantic, structure`). Registering
`GraphLoadStage` adds `"graph_load"` to `successors("graph")` **alongside the pre-existing `"provenance"`**
(SP-12's `ProvenanceStage`, `deps=("graph","ocr")`) — the existing `test_provenance_stage.py` assertion
(`assert "provenance" in resolver.successors("graph")`) is already a membership check, not an exact-list
equality, so no retrofit is expected; scan for exact-list assertions on `successors("graph")` before
landing anyway (same caution SP-9/SP-12 already documented for their own successor lists).

### 3.5 Config (Settings + x-backend-env + .env.example)

New `FDI_` fields, added beside the existing `azure_di_*`/`gpt_*` block in `Settings`:
```python
# Neo4j (graph loader, SP-9b). URI/password default to None so the stack boots
# without Neo4j configured (the `graph` Compose profile is opt-in, never part of
# `core`); the graph_load stage using them is wired separately. See
# docker/compose.yml x-backend-env, the `neo4j` service's NEO4J_AUTH, and
# .env.example. Unlike FDI_AZURE_DI_*/FDI_GPT_* (external SaaS, blank by
# default), .env.example ships REAL working values here because this env var
# ALSO drives the self-hosted `neo4j` Compose service's own bootstrap
# credential (NEO4J_AUTH: ${FDI_NEO4J_USER}/${FDI_NEO4J_PASSWORD}) -- a blank
# password would fail Neo4j's own minimum-password-length bootstrap check.
neo4j_uri: str | None = None
neo4j_user: str = "neo4j"
neo4j_password: str | None = None
```
Added to `docker/compose.yml`'s `x-backend-env`:
```yaml
FDI_NEO4J_URI: ${FDI_NEO4J_URI}
FDI_NEO4J_USER: ${FDI_NEO4J_USER}
FDI_NEO4J_PASSWORD: ${FDI_NEO4J_PASSWORD}
```
The existing `neo4j` service's `environment:` changes from the hardcoded `NEO4J_AUTH: neo4j/fdipassword`
to `NEO4J_AUTH: ${FDI_NEO4J_USER}/${FDI_NEO4J_PASSWORD}` — one source of truth for the container's own
credential and the app's connection settings, instead of two values that happen to agree today by
coincidence. A healthcheck is added (the service currently has none, unlike every other stateful service
in the file):
```yaml
healthcheck:
  test: ["CMD-SHELL", "cypher-shell -u $${FDI_NEO4J_USER} -p $${FDI_NEO4J_PASSWORD} 'RETURN 1'"]
  interval: 15s
  timeout: 10s
  retries: 5
  start_period: 30s
```
(`$$` escapes Compose interpolation so the shell inside the container expands the var, same discipline as
the existing `postgres`/`worker` healthchecks; `cypher-shell` ships in the official `neo4j` image, same
reasoning as `minio`'s healthcheck using `mc` instead of a nonexistent `curl`.)

`.env.example` gains, with real working defaults (§2.7):
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

### 3.6 Dependency

Add `neo4j` (the official pure-Python driver; ships `py.typed`, so `mypy --strict` gets real types, not
`Any`) to `backend/pyproject.toml`'s `dependencies`, pinned with a lower bound matching the Compose server
version's major.minor for protocol compatibility: `"neo4j>=5.26"`. The implementer runs `uv add --directory
backend neo4j`, which updates `uv.lock` — not hand-edited.

### 3.7 Trigger

`POST /documents/{id}/graph_load` → 202: dispatch `run_stage_task(doc_id, "graph_load", correlation_id)`
(reuses `StageDispatcher`, mirrors SP-5's `POST /documents/{id}/ocr` and SP-10's `POST
/documents/{id}/semantic`). 404 if the document is unknown. `GraphLoadTriggerView(BaseModel): doc_id: str;
stage: str` in `api/schemas.py`, mirroring `SemanticTriggerView`. The generic `POST
/docs/{doc_id}/reprocess?from=graph_load` (SP-2/18) already covers ad-hoc re-dispatch; the dedicated route
exists for parity/discoverability with every other stage's trigger.

## 4. Error handling

- Neo4j unconfigured (`neo4j_uri`/`neo4j_password` None) when `graph_load` runs → the stage fails with a
  clear message (`ctx.services["graph_load"]` — actually `ctx.services["graph_store"]` — raises `KeyError`,
  since the worker wiring omits the key entirely when `from_settings` returns `None`, same as SP-5/SP-10);
  `run_stage`'s fail-path records `stage_run(failed, error=...)`. No partial receipt artifact (SP-2's
  existing "no partial artifact on error" behavior — `store.put_json` only runs after `stage.run()`
  returns successfully).
- Neo4j connection/write error (container down, auth failure, network blip) → the driver raises inside
  `Neo4jGraphStore.load`; `run_stage` records the failure; retryable per Celery (same as SP-5/SP-10's
  external-service-error handling).
- A `graph` artifact with zero nodes/edges (a title-only document — see SP-9 §4) → `GraphLoadStage` still
  calls `store.load([], [])` and returns `{"schema_version":1, "loaded_nodes":0, "loaded_edges":0}` — a
  trivially valid, cheap result, not an error (mirrors SP-10's "zero AST sections → zero LLM calls, valid
  empty result" case).
- `graph_load` dispatched before `graph` has run → `run_stage`'s existing derived-stage machinery already
  requires the dep's `stage_run` to exist (`facts["graph"].output_hash` — SP-2 behavior, unchanged).
- **A cache hit never re-runs the Neo4j write** — see §2.6. This is a deliberate, documented limitation,
  not a bug: bump `GraphLoadStage.version` to force every document's `graph_load` to regenerate against
  whatever Neo4j instance is currently live.
- A malformed `graph` artifact (non-`list` `nodes`/`edges`, non-`dict` entries) → `GraphLoadStage` degrades
  to `[]` for the malformed side (same defensive `isinstance` narrowing `build_graph`/`SemanticStage`
  already use for their own upstream input) rather than raising on a shape violation that, by construction,
  should never occur (the only real producer of a `graph` artifact is `build_graph`, which is total).

## 5. Testing

- **Unit** (no Docker, no Neo4j): `FakeGraphStore` records every `(nodes, edges)` `load` call, returns the
  correct counts, `calls` increments once per invocation; `Neo4jGraphStore` (SDK monkeypatched, no live
  call) builds the driver with `GraphDatabase.driver(uri, auth=(user, password))` and its `load` runs the
  expected `MERGE` Cypher (asserted via a fake session/transaction recording every `tx.run(query, **params)`
  call) and returns `{"loaded_nodes": N, "loaded_edges": M}`; `from_settings` returns `None` when
  `uri`/`password` is unset; config defaults (`neo4j_uri is None`, `neo4j_user == "neo4j"`, `neo4j_password
  is None`) present in `Settings`.
- **Integration** (testcontainers, `FakeGraphStore` — NO live Neo4j): run `ast → graph → graph_load` via
  `run_stage` in a **local** `StageRegistry` (register `OcrStage`+`NormalizeStage`+`AstStage`+`GraphStage`+
  `GraphLoadStage`, not the global `REGISTRY`, mirroring `test_graph_stage.py`/`test_semantic_stage.py`);
  assert `"graph_load" in resolver.successors("graph")` (the chain is automatic); assert a `done` stage_run
  + a stored receipt artifact at the content-addressed key (`derived_input_hash([graph.output_hash])`)
  matching the fed graph's actual node/edge counts; a second `graph_load` run is a cache hit (the fake's
  `calls` stays `1` — proving the "no re-run on cache hit" behavior from §2.6/§4, same call-counter pattern
  as SP-10's `fake_semantic.calls == 1` assertion).
- **Opt-in real-Neo4j smoke test** (`@pytest.mark.neo4j`, a NEW marker registered in `pyproject.toml`
  alongside the existing `azure` marker, skipped unless `FDI_NEO4J_URI`/`FDI_NEO4J_PASSWORD` are set OR a
  `testcontainers[neo4j]` `Neo4jContainer` is spun up in-test): constructs the real `Neo4jGraphStore`,
  loads two nodes + one edge, asserts the receipt counts, and (if using a live/queryable instance) runs a
  read-back `MATCH` to confirm the nodes/edge actually landed. NEVER in CI by default (no creds/container
  in the default run), and NEVER in the default suite (`-m "not integration and not e2e and not azure and
  not neo4j"` — the fully-offline command gains one more exclusion term, additive, no existing exclusion
  changes). May use `testcontainers[neo4j]`'s `Neo4jContainer` (spins up its own throwaway instance, no
  operator credentials needed — closer to `azure`'s spirit would be a real always-on instance, but a
  container is the more honest "opt-in, never in the default suite" choice here since Neo4j, unlike Azure
  OpenAI, has no per-call cost to guard against) OR a running instance addressed by `FDI_NEO4J_URI`; either
  way it stays opt-in so the default suite never starts a container implicitly.
- **API** (integration): `POST /documents/{id}/graph_load` → 202; unknown doc id → 404.
- Config consistency: `FDI_NEO4J_*` present in `Settings` + compose `x-backend-env` + `.env.example`; the
  `neo4j` service's `NEO4J_AUTH` derives from the same two vars (no drift between container credential and
  app connection settings).

## 6. Explicitly deferred (YAGNI)

- **Loading provenance onto graph nodes** (SP-12's `provenance` artifact — page/`bounding_regions` per
  node) as Neo4j node properties — SP-12 already joins graph nodes to OCR geometry in its own JSON
  artifact; folding that onto the Neo4j nodes this loader creates is a follow-on property-enrichment pass,
  not part of the initial node/edge projection.
- **Semantic/ontology properties on graph nodes** (SP-10's `SectionType`, SP-11's `CanonicalConcept`) — the
  graph's `section`/`paragraph` nodes stay structural-only in this slice, same deferral SP-9 §6 already
  made for its own JSON artifact ("a future version could join the two artifacts... deliberately not this
  slice's job"); SP-9b projects exactly what SP-9 emits today, no new joins.
- **Cross-reference edges** (SP-7d's `linking` artifact — figures/footnotes as leaf nodes, captions/
  cross-refs as edges) — SP-9 §6 already deferred these at the JSON-artifact level; nothing to project
  until they exist upstream.
- **A read/query API over Neo4j** (retrieval — Cypher-backed search, graph traversal endpoints) — this
  slice is write-only (`load`); a query surface is a distinct, later sub-project (roadmap phase 11,
  Retrieval Indexes, SP-14) once there is a retrieval use case to serve.
- **Batching for very large graphs** (`UNWIND`-based bulk `MERGE` instead of one `tx.run` per node/edge) —
  deferred until corpus-scale ingestion (SP-13/14) makes per-row round-trips measurably slow; see §2.9.
- **Multi-document graph merging / cross-document entity resolution** (would need node ids that are unique
  and stable ACROSS documents, not just within one — SP-9's positional ids (`s0`, `s0p0`) collide across
  documents by design) — SP-16 (cross-document layer) territory, not this loader's job; today's `MERGE`
  keys purely on the SP-9 `id`, which is only unique per-document, so loading two different documents into
  the same Neo4j instance will silently alias their `s0` nodes onto each other. Flagged here explicitly as
  a real, known limitation of shipping this loader before SP-16 exists — not silently swept under "later."
- **Retry/backoff policy tuned to Neo4j-specific transient errors** (leader election during a Neo4j Causal
  Cluster failover, etc.) — irrelevant for a single-instance Community Edition container; generic Celery
  retry covers the thin slice.

## 7. Done criteria

1. `GraphStore` protocol + `FakeGraphStore` (`fdi.pipeline.graphstore`) exist; the fake records every
   `(nodes, edges)` `load` call and returns the correct counts.
2. `Neo4jGraphStore` (`fdi.platform.graphstore`) wraps the `neo4j` driver; `load` runs idempotent `MERGE`
   Cypher for every node and edge inside one write transaction and returns `{"loaded_nodes",
   "loaded_edges"}`; `from_settings` returns `None` when `uri`/`password` is unset; no live Neo4j call in
   its unit test (SDK monkeypatched).
3. `GraphLoadStage` (`name="graph_load"`, `version=1`, `deps=("graph",)`) is registered; `run(ctx)` reads
   the graph artifact, calls the injected `GraphStore`, and returns `{schema_version:1, loaded_nodes,
   loaded_edges}`.
4. Running `graph_load` over a document with a `graph` artifact stores the receipt content-addressed on
   `derived_input_hash([graph.output_hash])` + `GraphLoadStage.version` and records a `done` stage_run; a
   second run is a store cache hit that does NOT call `GraphStore.load` again (documented limitation, §2.6).
5. `FDI_NEO4J_*` config is wired (Settings + compose `x-backend-env` + the `neo4j` service's `NEO4J_AUTH` +
   `.env.example`); the `core` profile boots with Neo4j unconfigured; `graph_load` then fails with a clear,
   recorded error; the `neo4j` service gains a healthcheck.
6. `POST /documents/{id}/graph_load` dispatches the graph-load stage (202); unknown id → 404.
7. The opt-in real-Neo4j smoke test (`@pytest.mark.neo4j`, newly registered) exists and skips cleanly when
   unconfigured/no container.
8. All four static gates green; `core` stays untouched (SP-9b adds nothing to `fdi.core` — the graph shape
   is already SP-9's); driver imports (`neo4j`) live ONLY in `platform`; full suite passes with NO live
   Neo4j call (fake store only).
