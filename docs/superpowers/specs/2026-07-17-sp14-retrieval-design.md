# SP-14 — Vector Retrieval (Milvus)

- **Status:** Not yet implemented. Spec + TDD plan drafted 2026-07-18 (no code executed). Companion plan:
  `docs/superpowers/plans/2026-07-17-sp14-retrieval.md`.
- **Depends on:** SP-2 (stage framework — `StageContext.services`, content-addressed derived-stage
  caching), SP-13 (the embedding artifact this sub-project consumes — `fdi.pipeline.stages.embedding`,
  `{schema_version:1, model, dim, chunks:[{chunk_id, heading, vector}]}` — and the
  `EmbeddingClient`/`AzureOpenAIEmbeddingClient` seam this spec's `/search` route reuses unchanged), SP-9b
  (the injected-mockable-client + fake + platform SDK client + derived-stage-as-receipt + worker-wiring +
  opt-in-real-service pattern this spec mirrors exactly, one layer down: Milvus instead of Neo4j).
- **Roadmap:** `docs/architecture/ingestion-roadmap.md` §5's build order: **"SP-13 embeddings → SP-14
  indexes"**; §3's table, phase 11 "Retrieval Indexes — metadata · entity · section · clause · table ·
  vector · graph"; SP-13's own §6: *"The Milvus vector STORE + upsert + similarity retrieval → SP-14
  (Retrieval Indexes). SP-13 ships the artifact SP-14 will load into Milvus; SP-13 does not talk to
  Milvus..."* SP-14 is that sub-project. **THIN SLICE, stated up front: this sub-project builds the
  **vector** index and similarity search ONLY.** The other five index types phase 11 names
  (metadata/entity/section/clause/table/graph) are explicitly DEFERRED (§6) — nothing in this repo yet
  produces entity/clause/table-summary artifacts for them to index, and the graph index is Neo4j itself
  (SP-9b, already queryable via Cypher, not a Milvus concern).
- **Compose already has `vector`-profiled `etcd`+`milvus` services** (added in the project-setup "profiled
  stack" pass, commit `d1fa75b` — the same pass that added the `neo4j` service SP-9b later wired):
  `etcd:v3.5.25`, `milvus:v2.6.20` (pinned, `command: ["milvus", "run", "standalone"]`,
  `ETCD_ENDPOINTS`/`MINIO_ADDRESS` env, `milvusdata`/`etcddata` volumes, port `19530`), `profiles: [vector]`
  — never started under `core`. **No `FDI_MILVUS_*` wiring exists yet** (absent from `x-backend-env`,
  `Settings`, `.env.example`) and no healthcheck. The `milvus` service's own comment currently reads
  *"Defined now; wired at SP-13"* — a stale artifact of the roadmap's original phase numbering, since
  SP-13 explicitly deferred the wiring to SP-14 (see its §6, quoted above); Task 7 below corrects that
  comment to *"wired at SP-14"* while it is being touched anyway. **Unlike SP-9b's Neo4j** (which needed
  `FDI_NEO4J_USER`/`FDI_NEO4J_PASSWORD` threaded into the `neo4j` service's own `NEO4J_AUTH` bootstrap
  credential), **Milvus standalone has no bootstrap credential of its own to keep in sync** — SP-14 does
  not invent the `etcd`/`milvus` services and does not need to touch their service blocks at all; wiring
  here means adding `FDI_MILVUS_*` to `x-backend-env` and nothing else in `compose.yml`. README's Compose
  memory table already flags `vector` as the OOM-risk profile (~5 GB estimated, on a ~9.6 GB Docker VM) —
  unchanged by this spec, carried forward as an operational caution, not a design constraint on the code.

## 1. Purpose

Make SP-13's per-section embedding vectors queryable: upsert them into a real Milvus vector index and
answer similarity-search queries over them. This is the first **retrieval** sub-project — everything
before it (OCR through embeddings) *produces* artifacts; SP-14 is the first thing that *answers a
question* using them. Thin vertical slice: an injected `VectorStore` protocol, a `MilvusVectorStore`
platform client wrapping `pymilvus`, a derived `IndexStage` (`deps=("embedding",)`) that upserts one item
per chunk, a `POST /search` endpoint that embeds a query and searches the store, config wiring, and an
opt-in real-Milvus smoke test — proven end to end with a **fake vector store** (no live Milvus, no
container, no cost) in the default suite.

Structurally this is **SP-9b again**, substituting Milvus-the-vector-database for Neo4j-the-graph-database,
plus one new shape SP-9b never needed: a synchronous read endpoint (`/search`) that does not dispatch a
Celery task, because there is no stage to run — the caller wants an answer in the response, not a 202.

## 2. Scope decisions (made autonomously, mirroring SP-9b's grounded pattern)

1. **The vector write/read is an injected `VectorStore` protocol**, not a direct `pymilvus` import in
   `pipeline` or `core`. The suite runs against a `FakeVectorStore` (in-memory, exact cosine similarity, no
   network); `MilvusVectorStore` (real SDK) is used in production plus an opt-in smoke test. Same
   SP-3/SP-5/SP-9b/SP-10/SP-13 lesson: the vector backend is a swappable candidate — a different ANN store
   is just another `VectorStore`.
2. **SDK surface = `pymilvus`'s `MilvusClient`** (the high-level "quick setup" client introduced for
   Milvus 2.4+), not the lower-level `connections` + `Collection` + hand-written `CollectionSchema` API.
   `MilvusClient.create_collection(collection_name, dimension=..., metric_type=...)`'s quick-setup path
   auto-creates a schema (`id` primary key, `vector` field) with **dynamic fields enabled by default** —
   arbitrary extra scalar columns (our `metadata` Mapping) can be inserted and returned without a
   pre-declared schema per index. Same "no OGM, the artifact's shape is already exactly what the client
   needs" reasoning SP-9b gave for choosing the plain `neo4j` driver over `neomodel`/`py2neo`.
3. **`pymilvus` does NOT ship a `py.typed` marker** — checked directly (its own `pyproject.toml` declares
   no `package-data`/`py.typed` entry as of the pinned `2.6.x` line), not assumed. With
   `ignore_missing_imports = true`, every `pymilvus` call therefore resolves to `Any`. The mitigation:
   `MilvusVectorStore` never returns a raw SDK value — every hit is rebuilt into an explicitly-typed
   `dict[str, object]` by hand at the platform boundary (`{"id": hit["id"], "score": hit["distance"],
   "metadata": entity}`), so `mypy --strict`'s `warn_return_any`/`no-any-return` has nothing to flag and no
   blanket module-level ignore is needed. If a real, narrow gap surfaces during implementation (e.g. a
   specific kwarg mypy cannot resolve even through a hand-built dict), scope a single-line
   `# type: ignore[code]` with a reason comment there — same discipline SP-10/SP-13 used for `openai`'s own
   stub gaps — never a package-wide ignore.
4. **One collection, name from config** (`FDI_MILVUS_COLLECTION`, default `"fdi_sections"`) — **not** a
   collection per document or per doc_key. Milvus's own primary key (`id`) already disambiguates rows
   across documents (`{doc_id}:{chunk_id}`, decision 8 below); a single collection is the simplest thing
   that supports cross-document search, which is the entire point of a shared vector index.
5. **Dimension is NOT a new Milvus-specific setting.** The collection is created with `FDI_EMBED_DIM` —
   the SAME field SP-13 already added and already passes to Azure OpenAI as `dimensions=`. A second,
   independently-set "Milvus dim" could silently disagree with the dimensionality the embedder actually
   produces (SP-13 §2.8's exact concern, one layer downstream); reusing the one existing field makes that
   class of drift structurally impossible rather than merely documented against.
6. **`upsert`, not `insert`.** Milvus's own `upsert` is idempotent by primary key — the same "safe to
   re-run against the same live backend" property SP-9b's Cypher `MERGE` gave the graph loader. This is
   what makes `IndexStage`'s cache-hit-does-not-rerun tradeoff (decision 9 below) low-risk in exactly the
   same way SP-9b argued for Neo4j: a cache-MISS re-run (a real reprocess after a version bump) is always
   safe against the same live Milvus, because upserting the same id twice just overwrites, never
   duplicates.
7. **Metric = `COSINE`**, matching the literal spec brief's `FakeVectorStore` ("does exact cosine
   similarity") and the standard way embedding closeness is compared (direction, not raw magnitude) —
   `text-embedding-3-*`'s vectors are not naturally unit-length, so cosine (not dot-product/L2) is the
   metric whose ranking does not silently reward longer vectors.
8. **`MilvusVectorStore.search` returns `[]` for a collection that does not exist yet**, rather than
   letting `pymilvus` raise on a missing collection. This is the one place the fake and the real
   implementation are made to agree EXPLICITLY rather than forking silently: `FakeVectorStore.search`
   against an empty `_items` dict is naturally `[]` (nothing has been `upsert`ed yet); `MilvusVectorStore`
   checks `has_collection()` first and returns `[]` for the same "nothing indexed yet" state instead of
   surfacing a Milvus-specific "collection not found" error a caller of the *fake* would never see. Decided
   explicitly here rather than left to whichever exception `pymilvus` happens to raise.
9. **`IndexStage` is a DERIVED stage** (`name="index"`, `version=1`, `deps=("embedding",)`) — the same
   shape as SP-9b's `GraphLoadStage` and SP-13's `EmbeddingStage`. The stored artifact is a small
   deterministic RECEIPT (`{schema_version:1, upserted: int}`), not the vectors themselves — SP-13 already
   stores those; the Milvus write is the side effect and the actual point (SP-9b's "receipt, not the
   underlying data" call, one layer downstream). **A cache hit does NOT re-run the upsert** — the same
   deliberate, documented limitation SP-9b accepted for Neo4j: a second `index` run against an unchanged
   `embedding` artifact finds the receipt already stored and skips `stage.run()` entirely. The documented
   workaround is the same one SP-9b/SP-10 document: **bump `IndexStage.version`** to force every document's
   `index` to regenerate (and thus re-`upsert` into whatever Milvus instance is live). Because `upsert` is
   idempotent, a version-bump re-run against the SAME live Milvus is always safe.
10. **Zero chunks (a title-only / zero-section document's embedding artifact) → `IndexStage` returns
    `{"schema_version":1, "upserted":0}` WITHOUT calling `VectorStore.upsert` at all.** This is a
    deliberate DEPARTURE from `GraphLoadStage`'s own choice to always call `store.load([], [])` even with
    empty lists (Neo4j's empty `MERGE` loop is a cheap, side-effect-free no-op). Milvus's
    `create_collection` + `upsert([])` on an empty batch is a real, unforced side effect — creating a
    collection for a document that produced no vectors to store, which a later document's real upsert
    would then have to discover was already (emptily) there. Skipping entirely for zero chunks mirrors
    SP-13's own "zero AST sections → zero embedding calls" rule instead — resolved explicitly here because
    the two upstream precedents (SP-9b, SP-13) disagree on this exact point and a silent pick would smuggle
    the disagreement past review.
11. **`/search` is the first route in this codebase that performs I/O against an injected service directly
    in the API process, rather than dispatching a Celery task.** Every earlier "trigger" route (`/ocr`,
    `/semantic`, `/embedding`, `/graph_load`) just enqueues a stage and returns 202; the worker builds
    `services` from `Settings` per-task (`workers/tasks.py`'s `_run_stage_async`). There is no stage to
    dispatch for `/search` — the caller wants an answer in THIS response. This needs two new *API-layer*
    dependencies, `get_embedder`/`get_vector_store` in `api/deps.py`, built ONCE in `lifespan` from
    `Settings` (mirroring how `app.state.artifact_store`/`app.state.resolver` are already built there), each
    returning `None` when unconfigured — not a reuse of `StageDispatcher`, which has nothing to dispatch
    here. Tests substitute fakes via FastAPI's `app.dependency_overrides` (the standard FastAPI test seam),
    its first use in this codebase: no earlier integration test needed to swap a fake INTO a running,
    `app.state`-built service, because every earlier service-touching test exercised the WORKER path
    (`run_stage` against a local `StageRegistry`) rather than the API path.
12. **`/search` requires BOTH `embedder` and `vector_store` configured, and returns `503`** (not 400/404)
    naming which is missing, when either is `None`. Mirrors `/ready`'s existing use of 503 for "this
    instance cannot serve [a capability] right now" — a fact about server-side configuration/availability,
    not about the caller's input, so the 5xx family is the honest one. A partially-configured search
    (embedder present, store absent, or vice versa) is refused up front rather than silently returning an
    embedded-but-unsearched or all-zero-vector result.

## 3. Architecture

### 3.1 No stage-framework change needed (same as SP-9b/SP-13 §3.1)

`IndexStage` is a **derived** stage reading an upstream artifact via `ctx.read("embedding")` — exactly
`GraphLoadStage`'s and `EmbeddingStage`'s shape. `StageContext.services`/`.doc_id` and the derived-stage
`input_hash` (`derived_input_hash([facts["embedding"].output_hash])`) already exist. Zero changes to
`fdi/pipeline/stage.py` or `fdi/pipeline/run.py`. `/search` similarly needs no stage-framework change — it
never touches `run_stage`/`StageDispatcher` at all; it is a plain FastAPI route with two new
`Depends`-injected services.

### 3.2 `VectorStore` (protocol + fake), `fdi.pipeline.vectorstore`

```python
"""The VectorStore seam: `upsert(items) -> {"upserted": int}` and
`search(vector, k) -> [{"id", "score", "metadata"}, ...]` (best match first).

`MilvusVectorStore` (platform, does I/O) structurally satisfies this without
importing it -- `platform` may not import `pipeline`. `FakeVectorStore` lives
here for tests: no live Milvus call anywhere in the suite."""

import math
from collections.abc import Mapping
from typing import Protocol, cast, runtime_checkable


@runtime_checkable
class VectorStore(Protocol):
    def upsert(self, items: list[Mapping[str, object]]) -> dict[str, object]: ...

    def search(self, vector: list[float], k: int) -> list[dict[str, object]]: ...


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class FakeVectorStore:
    """In-memory VectorStore. `search` does EXACT cosine similarity over every
    stored vector -- correct, not approximate, for the small fixture-scale
    corpora this suite ever indexes; no ANN structure, no network."""

    def __init__(self) -> None:
        self.calls = 0
        self._items: dict[str, Mapping[str, object]] = {}

    def upsert(self, items: list[Mapping[str, object]]) -> dict[str, object]:
        self.calls += 1
        for item in items:
            self._items[str(item["id"])] = item
        return {"upserted": len(items)}

    def search(self, vector: list[float], k: int) -> list[dict[str, object]]:
        scored = [
            {
                "id": item_id,
                "score": _cosine(vector, cast(list[float], item["vector"])),
                "metadata": dict(cast(Mapping[str, object], item.get("metadata") or {})),
            }
            for item_id, item in self._items.items()
        ]
        scored.sort(key=lambda r: cast(float, r["score"]), reverse=True)
        return scored[:k]
```
`calls` mirrors `FakeGraphStore`'s call-recording discipline (SP-9b §3.2) so the same "second run is a
cache hit, the fake was NOT called twice" test shape applies unchanged.

### 3.3 `MilvusVectorStore` (platform, does I/O), `fdi.platform.vectorstore`

```python
"""MilvusVectorStore -- VectorStore backed by a real Milvus instance via
pymilvus's MilvusClient.

Lives in `platform` (does I/O), not `pipeline`: the `VectorStore` protocol and
`FakeVectorStore` live in `fdi.pipeline.vectorstore`. This module structurally
satisfies that protocol without importing it -- `platform` may not import
`pipeline` per the layered architecture contract.

`MilvusClient` is imported at module level (rather than inside `__init__`) so
tests can monkeypatch `fdi.platform.vectorstore.MilvusClient` with a fake --
no live Milvus call in the suite.

pymilvus ships NO py.typed marker (spec sec 2.3) -- every value that crosses
this module's boundary is rebuilt into an explicitly-typed dict by hand, never
returned raw, so mypy --strict's warn_return_any has nothing to catch here."""

from collections.abc import Mapping

from pymilvus import MilvusClient

from fdi.platform.config import Settings


class MilvusVectorStore:
    """VectorStore backed by Milvus. Lives in platform (does I/O)."""

    def __init__(self, *, uri: str, collection: str, dim: int) -> None:
        self._client = MilvusClient(uri=uri)
        self._collection = collection
        self._dim = dim

    def _ensure_collection(self) -> None:
        if not self._client.has_collection(self._collection):
            self._client.create_collection(
                collection_name=self._collection,
                dimension=self._dim,
                metric_type="COSINE",
                auto_id=False,
                primary_field_name="id",
                id_type="string",
                vector_field_name="vector",
            )

    def upsert(self, items: list[Mapping[str, object]]) -> dict[str, object]:
        if not items:
            return {"upserted": 0}
        self._ensure_collection()
        rows = [
            {"id": item["id"], "vector": item["vector"], **dict(cast(Mapping[str, object], item.get("metadata") or {}))}
            for item in items
        ]
        self._client.upsert(collection_name=self._collection, data=rows)
        return {"upserted": len(items)}

    def search(self, vector: list[float], k: int) -> list[dict[str, object]]:
        if not self._client.has_collection(self._collection):
            return []
        hits = self._client.search(
            collection_name=self._collection,
            data=[vector],
            limit=k,
            output_fields=["*"],
        )
        results: list[dict[str, object]] = []
        for hit in hits[0]:
            entity = dict(hit.get("entity", {}))
            entity.pop("vector", None)
            results.append({"id": hit["id"], "score": hit["distance"], "metadata": entity})
        return results

    @staticmethod
    def from_settings(settings: "Settings") -> "MilvusVectorStore | None":
        if not settings.milvus_uri:
            return None
        return MilvusVectorStore(
            uri=settings.milvus_uri,
            collection=settings.milvus_collection,
            dim=settings.embed_dim,
        )
```
(`cast`/`Mapping` imports elided above for brevity in this excerpt; the plan's Task 2 code carries the full
import list.) Note `upsert`'s early return for `items == []` — decision 10: no collection is created and no
Milvus call happens for a document with nothing to store, mirroring `IndexStage`'s own zero-chunk skip one
layer up (belt-and-suspenders: `IndexStage` already never calls `upsert([])` in the normal path, but the
store itself stays correct if ever called that way directly, e.g. from the opt-in smoke test or a future
caller).

### 3.4 `IndexStage` (`fdi.pipeline.stages.index`)

```python
"""IndexStage: a DERIVED stage that upserts SP-13's embedding artifact
(one chunk = one vector) into a VectorStore via an injected client. Auto-chains
off EmbeddingStage via `deps=("embedding",)`. The artifact is a small
deterministic receipt (upserted count) -- the vector-store write is the side
effect and the actual point; see spec sec 2.9-10 for why a cache hit does not
re-run the upsert, and why zero chunks skips the call entirely (unlike
SP-9b's GraphLoadStage)."""

from collections.abc import Mapping
from typing import cast

from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext
from fdi.pipeline.vectorstore import VectorStore


class IndexStage:
    name = "index"
    version = 1
    deps: tuple[str, ...] = ("embedding",)

    async def run(self, ctx: StageContext) -> object:
        embedding = cast(Mapping[str, object], await ctx.read("embedding"))
        store = cast(VectorStore, ctx.services["vector_store"])

        chunks_raw = embedding.get("chunks")
        chunks = (
            [c for c in chunks_raw if isinstance(c, dict)] if isinstance(chunks_raw, list) else []
        )

        items: list[Mapping[str, object]] = [
            {
                "id": f"{ctx.doc_id}:{chunk['chunk_id']}",
                "vector": chunk["vector"],
                "metadata": {"heading": chunk.get("heading"), "chunk_id": chunk["chunk_id"]},
            }
            for chunk in chunks
        ]

        upserted = store.upsert(items)["upserted"] if items else 0
        return {"schema_version": 1, "upserted": upserted}


REGISTRY.register(IndexStage())
```
Self-registers into `REGISTRY`; `stages/__init__.py` gains `from fdi.pipeline.stages import index as index
# noqa: F401 - import for its registration side effect`, alphabetically ordered between `graphload` and
`linking` (`ast, embedding, graph, graphload, index, linking, normalize, ocr, ontology, ping, provenance,
quality, semantic, structure`). Registering `IndexStage` adds `"index"` to `successors("embedding")`; scan
for any exact-list assertion on `successors("embedding")` before landing (none exists today — checked; the
same caution SP-9b/SP-13 already documented for their own successor lists).

### 3.5 Config (Settings + x-backend-env + .env.example)

New `FDI_` fields, added beside the existing `neo4j_*` block in `Settings`:
```python
# Milvus (vector index, SP-14). URI defaults to None so the stack boots
# without Milvus configured (the `vector` Compose profile is opt-in, never
# part of `core`); the index stage and the /search route using it are wired
# separately. See docker/compose.yml x-backend-env and .env.example. Like
# FDI_NEO4J_* (self-hosted via Compose), .env.example ships a REAL working
# value here -- but unlike Neo4j, Milvus standalone has no bootstrap
# credential of its own to keep in sync, so there is no auth pairing to get
# wrong. Dimension is deliberately NOT a separate Milvus-specific field: the
# collection is created with FDI_EMBED_DIM (SP-13) -- the same
# dimensionality the embedder actually produces -- so a second,
# independently-set "Milvus dim" can never silently disagree with it.
milvus_uri: str | None = None
milvus_collection: str = "fdi_sections"
```
Added to `docker/compose.yml`'s `x-backend-env`:
```yaml
FDI_MILVUS_URI: ${FDI_MILVUS_URI}
FDI_MILVUS_COLLECTION: ${FDI_MILVUS_COLLECTION}
```
No other `compose.yml` change: the `etcd`/`milvus` service blocks (already defined, pinned, `profiles:
[vector]`) need no `environment:`/healthcheck change — Milvus standalone has no bootstrap credential to
thread through, unlike SP-9b's `neo4j` service.

`.env.example` gains, with a real working default (mirrors SP-9b's Neo4j precedent — a self-hosted Compose
service gets a real value, not a blank one):
```
# Milvus (vector index, SP-14). Self-hosted via the `vector` Compose profile
# (docker compose --profile vector up) -- like Neo4j above (and unlike Azure
# DI/OpenAI/embeddings), this is NOT blank: a working default URI, though
# unlike Neo4j there is no username/password pair to keep in sync, since
# Milvus standalone has no bootstrap credential of its own.
# WARNING: `vector` (etcd+milvus) is the memory-heavy Compose profile -- see
# README's profile-memory table (~5 GB estimated) before bringing it up on a
# constrained Docker VM.
FDI_MILVUS_URI=http://milvus:19530
FDI_MILVUS_COLLECTION=fdi_sections
```

### 3.6 `IndexStage` trigger

No new dedicated trigger route for this spec's thin slice — `POST /docs/{doc_id}/reprocess?from=index`
(SP-2/18's existing generic re-dispatch route) already covers ad-hoc dispatch of `index`, exactly as it
does for every other stage. Every prior sub-project (SP-9b, SP-13) added a DEDICATED trigger route
(`/documents/{id}/graph_load`, `/documents/{id}/embedding`) purely for parity/discoverability with the
already-existing `/ocr`/`/semantic` routes; `index` is deliberately NOT given one here, because this
spec's one genuinely new, load-bearing route is `/search` (§3.7) — adding a ninth near-identical
`/documents/{id}/index` trigger route would be parity theater, not a functional gap. (Nothing prevents
adding it later, at zero cost, the same one-line shape as the other five, if discoverability turns out to
matter in practice.)

### 3.7 `POST /search` (the new, load-bearing route)

**Schemas** (`api/schemas.py`):
```python
class SearchRequest(BaseModel):
    query: str
    k: int = 5


class SearchResultView(BaseModel):
    id: str
    score: float
    metadata: dict[str, object]


class SearchResultsView(BaseModel):
    results: list[SearchResultView]
```

**Dependencies** (`api/deps.py`), new:
```python
def get_embedder(request: Request) -> EmbeddingClient | None:
    embedder: EmbeddingClient | None = request.app.state.embedder
    return embedder


def get_vector_store(request: Request) -> VectorStore | None:
    vector_store: VectorStore | None = request.app.state.vector_store
    return vector_store


EmbedderDep = Annotated[EmbeddingClient | None, Depends(get_embedder)]
VectorStoreDep = Annotated[VectorStore | None, Depends(get_vector_store)]
```
(`EmbeddingClient` from `fdi.pipeline.embeddings`, `VectorStore` from `fdi.pipeline.vectorstore` — both
legal imports for `api` per the layers contract, `api` sitting above both.)

**Lifespan** (`api/app.py`), added beside the existing `app.state.artifact_store`/`app.state.resolver`
wiring:
```python
from fdi.platform.embeddings import AzureOpenAIEmbeddingClient
from fdi.platform.vectorstore import MilvusVectorStore
...
app.state.embedder = AzureOpenAIEmbeddingClient.from_settings(settings)
app.state.vector_store = MilvusVectorStore.from_settings(settings)
```
Both module-level imports (not function-local): unlike `run_stage_task`/`StageDispatcher` (deferred to
avoid importing the Celery app at collection time — Global Constraint 3's concern), constructing these two
platform clients has no import-time side effect and no cross-layer exemption is needed (`api -> platform`
is an ordinary legal edge, already exercised by `ArtifactStore`'s own module-level import in this file).

**Route** (`api/routes.py`):
```python
@router.post("/search", response_model=SearchResultsView)
async def search(
    body: SearchRequest, embedder: EmbedderDep, vector_store: VectorStoreDep
) -> SearchResultsView:
    missing = [
        name
        for name, svc in (("embedder", embedder), ("vector_store", vector_store))
        if svc is None
    ]
    if missing:
        raise HTTPException(
            status_code=503,
            detail=(
                f"search is not configured: missing {', '.join(missing)} "
                "(set FDI_EMBED_ENDPOINT/FDI_EMBED_KEY and/or FDI_MILVUS_URI)"
            ),
        )
    [vector] = embedder.embed([body.query])
    hits = vector_store.search(vector, body.k)
    return SearchResultsView(
        results=[
            SearchResultView(id=str(h["id"]), score=float(h["score"]), metadata=dict(h["metadata"]))  # type: ignore[arg-type]
            for h in hits
        ]
    )
```
`[vector] = embedder.embed([body.query])` destructures the single-text call's single result — a hard
failure (`ValueError`) if a real `EmbeddingClient` ever returned zero or >1 vectors for one input text,
which would be a contract violation worth seeing loudly, same "loud, not swallowed" discipline SP-13's
`zip(..., strict=True)` uses.

### 3.8 Dependency

Add `pymilvus` to `backend/pyproject.toml`'s `dependencies`, pinned with a lower bound matching the
Compose server's major.minor for protocol compatibility (same reasoning as SP-9b's `neo4j>=5.26`):
`"pymilvus>=2.6"`. The implementer runs `uv add --directory backend pymilvus`, which updates `uv.lock` —
not hand-edited.

## 4. Error handling

- Milvus unconfigured (`milvus_uri` `None`) when `index` runs → `ctx.services["vector_store"]` raises
  `KeyError` (the worker wiring omits the key entirely when `from_settings` returns `None`, same as every
  prior stage's service); `run_stage`'s fail-path records `stage_run(failed, error=...)`. No partial
  receipt artifact.
- Milvus connection/write error (container down, network blip, a dimension mismatch against an
  already-existing collection) → the SDK raises inside `MilvusVectorStore.upsert`/`.search`; `run_stage`
  records the failure; retryable per Celery (same as every prior external-service stage).
- An `embedding` artifact with zero chunks (a title-only document) → `IndexStage` returns
  `{"schema_version":1, "upserted":0}` with **no Milvus call at all** — decision 10; a trivially valid,
  cheap result, not an error.
- `index` dispatched before `embedding` has run → `run_stage`'s existing derived-stage machinery already
  requires the dep's `stage_run` to exist (`facts["embedding"].output_hash` — SP-2 behavior, unchanged).
- **A cache hit never re-runs the Milvus upsert** — decision 9. Documented limitation, not a bug: bump
  `IndexStage.version` to force every document's `index` to regenerate against whatever Milvus instance is
  currently live.
- A malformed `embedding` artifact (non-`list` `chunks`, non-`dict` entries) → `IndexStage` degrades to
  `[]` for the malformed side (same defensive `isinstance` narrowing `GraphLoadStage`/`EmbeddingStage`
  already use), rather than raising on a shape violation that, by construction, should never occur (the
  only real producer of an `embedding` artifact is `EmbeddingStage`, which is total).
- `POST /search` with `embedder` and/or `vector_store` unconfigured (`None`) → `503`, body names which is
  missing (decision 12). Never a 500 — this is an expected, checkable configuration state, not an
  unanticipated failure.
- `POST /search` against a `vector_store` whose collection does not exist yet (nothing has been `index`ed
  anywhere) → `MilvusVectorStore.search` returns `[]` (decision 8), so the route responds `200` with
  `{"results": []}`, not an error — searching an empty index is a valid, if unhelpful, query.
- A real `EmbeddingClient.embed([query])` returning a count other than 1 for a single input text → the
  `[vector] = ...` destructuring raises `ValueError`, surfacing through the unhandled-exception path (§
  `api/errors.py`'s `_unhandled`) as a `500` — a genuine SDK contract violation, not a state this route
  should special-case.

## 5. Testing

- **Unit** (no Docker, no Milvus): `FakeVectorStore.search` returns results sorted best-first by exact
  cosine similarity, `upsert` accumulates and overwrites by `id` (re-upserting the same id updates it, not
  duplicates), `calls` increments once per `upsert` invocation; `MilvusVectorStore` (SDK monkeypatched, no
  live call) builds `MilvusClient(uri=...)`, `upsert` creates the collection only when
  `has_collection()` is `False` and calls `.upsert(...)` with the right rows (`id`/`vector`/flattened
  `metadata`), `search` calls `.search(...)` with `metric`/`limit` and maps hits to `{id, score,
  metadata}`, `search` returns `[]` without calling `.search` at all when `has_collection()` is `False`;
  `from_settings` returns `None` when `milvus_uri` is unset; config defaults (`milvus_uri is None`,
  `milvus_collection == "fdi_sections"`) present in `Settings`.
- **Integration** (testcontainers, `FakeVectorStore` — NO live Milvus): run `ocr → normalize → ast →
  embedding → index` via `run_stage` in a **local** `StageRegistry` (register
  `OcrStage`+`NormalizeStage`+`AstStage`+`EmbeddingStage`+`IndexStage`, not the global `REGISTRY`, mirroring
  `test_graphload_stage.py`/`test_embedding_stage.py`); assert `"index" in resolver.successors("embedding")`
  (the chain is automatic); assert a `done` stage_run + a stored receipt artifact at the content-addressed
  key (`derived_input_hash([embedding.output_hash])`) matching the fed embedding's actual chunk count; a
  second `index` run is a cache hit (the fake's `calls` stays `1` — proving "no re-run on cache hit",
  decision 9, same call-counter pattern as SP-9b's `fake_store.calls == 1`).
- **API** (integration): `POST /search` embeds the query and searches, returning `{"results": [...]}`, using
  `FakeEmbeddingClient` + `FakeVectorStore` substituted via `app.dependency_overrides[get_embedder]` /
  `app.dependency_overrides[get_vector_store]` (decision 11 — the standard FastAPI test seam); the default,
  fully-unconfigured `client` fixture (no overrides, real containers but no Azure/Milvus creds) gets `503`
  from the same route with no overrides at all.
- **Opt-in real-Milvus smoke test** (`@pytest.mark.milvus`, a NEW marker registered in `pyproject.toml`
  alongside the existing `azure`/`neo4j` markers, skipped unless `FDI_MILVUS_URI` is set): constructs the
  real `MilvusVectorStore`, upserts two items with hand-built orthogonal vectors, searches for the vector
  matching the first item, and asserts the top hit is that item's id. NEVER in CI by default (no
  URI/container in the default run), and NEVER in the default suite (`-m "not integration and not e2e and
  not azure and not neo4j and not milvus"` — the fully-offline command gains one more exclusion term,
  additive, no existing exclusion changes). Requires an operator-started `docker compose --profile vector
  up -d etcd milvus` (or an equivalent reachable instance) — `pymilvus` has no first-class
  `testcontainers` module in this project's pinned `testcontainers==4.14.2`, unlike SP-9b's optional
  `testcontainers[neo4j]` path, so this smoke test does not attempt to spin up its own container; it stays
  opt-in via the env-var skip exactly like `tests/azure/`'s smoke tests.
- Config consistency: `FDI_MILVUS_*` present in `Settings` + compose `x-backend-env` + `.env.example`.

## 6. Explicitly deferred (YAGNI)

- **The other five index types** phase 11 names (metadata, entity, section, clause, table) — nothing
  upstream in this repo yet produces entity/clause/table-summary artifacts for them to index; the graph
  index already exists as Neo4j itself (SP-9b), queried via Cypher, not a Milvus/vector-search concern.
  Building any of these is a later sub-project once the artifacts they'd index exist.
- **Hybrid (vector + keyword) search and re-ranking** — a real, SP-3-measurable candidate once retrieval
  ground truth exists to score it against (same deferral shape SP-13 gave chunking granularity); this thin
  slice ships pure vector similarity only.
- **Per-section-type filtering** (e.g. "search only BALANCE_SHEET sections", joining SP-10's `SectionType`
  label into the search) — the `metadata` Mapping already carries `heading`/`chunk_id` and Milvus's dynamic
  fields would support a filter expression cheaply, but no filtering UI/API surface is built here; a caller
  wanting this today can filter the returned `metadata` client-side.
- **Chunking finer than per-section** — SP-13 §6's deferral, unchanged; `IndexStage` indexes exactly what
  `EmbeddingStage` currently produces, one vector per AST section.
- **Pagination on `/search`** — `k` (default 5) bounds result count; no `offset`/cursor. A caller wanting
  more calls again with a larger `k`; not a real gap at this corpus scale (SP-13/14's own "5000-doc run"
  deployment posture is still ahead, per the roadmap).
- **Deleting/re-indexing a superseded document version** — `id = f"{doc_id}:{chunk_id}"` includes the
  version-specific `doc_id`, so a new upload's `intake_document` version creates FRESH ids rather than
  overwriting the old version's vectors; stale vectors from a superseded version linger in the index
  indefinitely. Flagged here explicitly as a real, known limitation (mirrors SP-9b's equally explicit
  cross-document-id-collision flag) — not silently swept under "later." Garbage-collecting superseded
  vectors is SP-16 (cross-document layer) territory, same as SP-9b's own cross-document id-collision
  deferral.
- **Collection schema/index-type tuning** (HNSW vs. the quick-setup client's default ANN index, `nlist`/
  `nprobe` and similar Milvus-specific search-quality knobs) — the thin slice accepts `MilvusClient`'s
  quick-setup defaults; real corpus-scale tuning is a later, measurable step once retrieval-quality ground
  truth exists to tune against.
- **Batch/async upsert across many documents at once** (parallelizing `index` across a large corpus,
  request batching beyond one `upsert()` call per document) — deferred to when the 5000-doc run is real,
  same deferral SP-9b/SP-13 each carry for their own corpus-scale batching.
- **A dedicated `POST /documents/{id}/index` trigger route** — deliberately not added (§3.6); the generic
  `reprocess?from=index` route already covers it.

## 7. Done criteria

1. `VectorStore` protocol + `FakeVectorStore` (`fdi.pipeline.vectorstore`) exist; the fake does exact
   cosine similarity, best-first, in-memory, no network, and records every `upsert` call.
2. `MilvusVectorStore` (`fdi.platform.vectorstore`) wraps `pymilvus`'s `MilvusClient`; `upsert` ensures the
   collection exists (dimension from `FDI_EMBED_DIM`) and upserts; `search` runs a `COSINE` search and maps
   hits to `{id, score, metadata}`, returning `[]` for a not-yet-created collection; `from_settings` returns
   `None` when `milvus_uri` is unset; no live Milvus call in its unit test (SDK monkeypatched).
3. `IndexStage` (`name="index"`, `version=1`, `deps=("embedding",)`) is registered; `run(ctx)` reads the
   embedding artifact, builds one item per chunk (`id=f"{doc_id}:{chunk_id}"`), calls the injected
   `VectorStore.upsert` (skipped entirely for zero chunks), and returns `{schema_version:1, upserted:N}`.
4. Running `index` over a document with an `embedding` artifact stores the receipt content-addressed on
   `derived_input_hash([embedding.output_hash])` + `IndexStage.version` and records a `done` stage_run; a
   second run is a store cache hit that does NOT call `VectorStore.upsert` again (documented limitation,
   §2.9).
5. `FDI_MILVUS_*` config is wired (Settings + compose `x-backend-env` + `.env.example`); the `core` profile
   boots with Milvus unconfigured; `index` then fails with a clear, recorded error.
6. `POST /search` embeds the query via the injected `EmbeddingClient` and searches via the injected
   `VectorStore`, returning `{"results":[{"id","score","metadata"}]}`; `503` with a clear `detail` naming
   which service is missing when either is unconfigured; integration-tested with `FakeEmbeddingClient` +
   `FakeVectorStore` via `app.dependency_overrides`.
7. The opt-in real-Milvus smoke test (`@pytest.mark.milvus`, newly registered) exists and skips cleanly
   when unconfigured.
8. All four static gates green; `core` stays untouched (SP-14 adds nothing to `fdi.core`); `pymilvus`
   imports live ONLY in `platform`; full suite passes with NO live Milvus call (fake store only).
