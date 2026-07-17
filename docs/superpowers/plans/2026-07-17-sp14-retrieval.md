# SP-14 — Vector Retrieval (Milvus) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Upsert SP-13's per-section embedding vectors into Milvus and answer similarity-search queries
over them — an `IndexStage` (SP-2 derived stage, `deps=("embedding",)`) that calls an injected
`VectorStore`, plus a new synchronous `POST /search` route that embeds a query and searches the store.
Proven end to end with FAKE services (no live Milvus, no live Azure OpenAI) in the default suite, plus an
opt-in real-Milvus smoke test.

**Architecture:** `IndexStage` reads the embedding artifact via the already-existing `ctx.read("embedding")`,
builds one item per chunk (`id=f"{doc_id}:{chunk_id}"`, `vector=chunk["vector"]`,
`metadata={"heading":..., "chunk_id":...}`), calls `ctx.services["vector_store"].upsert(items)` (fake in
tests, `MilvusVectorStore` in prod), and returns a small deterministic receipt
`{schema_version:1, upserted:N}` — content-addressed on `derived_input_hash([embedding.output_hash])` +
`IndexStage.version` (SP-2's generic derived-stage caching, already in `run_stage` — zero framework
change). `POST /search` is a NEW kind of route in this codebase: it does not dispatch a Celery task (there
is no stage to run), it embeds the query via an API-layer `EmbeddingClient` dependency and searches via an
API-layer `VectorStore` dependency, both built once in `lifespan` from `Settings` and `None` when
unconfigured.

**Tech Stack:** Python 3.13, `pymilvus` (official SDK, `MilvusClient` — NEW dependency this plan adds),
FastAPI, Celery, SQLAlchemy, MinIO, Postgres, pytest + testcontainers, `uv`. Design:
`docs/superpowers/specs/2026-07-17-sp14-retrieval-design.md`.

## Global Constraints

- `uv run --directory backend <cmd>` — NEVER `uv --project`.
- Import contract `api | workers → pipeline → platform → core`, `exhaustive`, `1 kept, 0 broken`. `core`
  stays UNTOUCHED — SP-14 adds nothing to `fdi.core`. The `MilvusVectorStore` (does I/O, imports
  `pymilvus`) lives ONLY in `platform`. The `VectorStore` protocol + `IndexStage` + `FakeVectorStore` live
  in `pipeline`. `api` may import both `pipeline` and `platform` directly (both are layers below it) —
  already exercised by `ArtifactStore`'s existing module-level import in `api/app.py`.
- **`pymilvus` ships NO `py.typed` marker** (checked directly against its own `pyproject.toml` — no
  `package-data`/`py.typed` entry). With `ignore_missing_imports = true`, its whole surface resolves to
  `Any`. Discipline: `MilvusVectorStore` never returns a raw SDK value — every value crossing the module
  boundary is rebuilt into an explicitly-typed `dict`/`list` by hand, so `mypy --strict`'s
  `warn_return_any`/`no-any-return` has nothing to catch. If a genuine, narrow gap surfaces, scope a
  single-line `# type: ignore[code]` with a reason comment — never a package-wide ignore.
- **Do not break SP-2/5/8/9b/10/11/12/13:** `StageContext`/`run_stage` are used AS-IS — no signature
  changes. Existing ping/ocr/normalize/ast/semantic/ontology/structure/quality/graph/provenance/embedding/
  graphload tests keep passing untouched.
- Determinism/caching note (deliberate limitation, spec §2.9, same shape as SP-9b's `graph_load`): a second
  `index` run on an unchanged `embedding` artifact is a store cache hit that does NOT call
  `VectorStore.upsert` again — prove it with a call counter on the fake, same pattern as SP-9b's
  `fake_store.calls == 1`.
- **Zero chunks skips the store call entirely** (spec §2.10, a deliberate DEPARTURE from `GraphLoadStage`'s
  own "always call, even with empty lists" choice) — `IndexStage` returns `{"schema_version":1,
  "upserted":0}` without touching `VectorStore.upsert` when there are no chunks to index.
- **No live Milvus in the default suite:** all unit/integration tests use `FakeVectorStore`. The
  real-Milvus test is `@pytest.mark.milvus` — a NEW marker (registered alongside the existing
  `azure`/`neo4j` markers), skipped unless `FDI_MILVUS_URI` is set.
- Any new `FDI_` field → Settings AND compose `x-backend-env` AND `.env.example` (inert-config lesson).
  `FDI_MILVUS_URI` is `str | None = None` in **Settings** so the `core` profile boots without Milvus
  configured; `.env.example`'s value is deliberately NOT blank (spec §3.5) — a real working default,
  mirroring SP-9b's Neo4j precedent for a self-hosted Compose service, though (unlike Neo4j) there is no
  username/password pair to keep in sync since Milvus standalone has no bootstrap credential.
- **The `etcd`/`milvus` Compose services already exist** (`profiles: [vector]`, pinned `v3.5.25`/`v2.6.20`,
  added in the project-setup "profiled stack" pass) — this plan does NOT invent them and does NOT add a
  healthcheck or `environment:` change to either service block (Milvus standalone has no bootstrap
  credential to thread through, unlike SP-9b's `neo4j` service); wiring is limited to adding `FDI_MILVUS_*`
  to `x-backend-env` (Task 3) and fixing one stale comment (Task 7). The `vector` profile is the
  memory-heavy one per README's Compose-profile table (~5 GB estimated, OOM risk on a ~9.6 GB Docker VM) —
  an operational caution to repeat at the manual-smoke-check step, not a code change.
- Integration tests: reuse `session_factory`/`store`/`minio_client`/`migrated_engine` fixtures; mint unique
  ids/checksums per test (session-scoped DB); use a LOCAL `StageRegistry` (not the global `REGISTRY`) for
  any test asserting exact chain behavior, same as `test_graphload_stage.py`/`test_embedding_stage.py`.
- **`POST /search` tests use FastAPI's `app.dependency_overrides`** (spec §2.11) — the first use of this
  mechanism in this codebase, because `/search` is the first route that performs I/O against an injected
  service directly in the API process rather than dispatching a Celery task. `test_api.py`'s `make_client`
  fixture gains ONE new keyword-only parameter (`dependency_overrides`) to support this; nothing else about
  the fixture changes.
- On Windows prefix `lint-imports` with `PYTHONUTF8=1`.

**Canonical commands:** fast `pytest -m "not integration and not e2e and not azure and not neo4j and not
milvus" -q`; integration `pytest tests/integration -q`; gates `ruff check .` · `mypy --strict src` ·
`PYTHONUTF8=1 lint-imports --config .importlinter`.

---

### Task 1: `VectorStore` protocol + `FakeVectorStore` (pipeline)

**Files:** Create `backend/src/fdi/pipeline/vectorstore.py`; Test `backend/tests/unit/test_vectorstore.py`.

**Interfaces:** `class VectorStore(Protocol)` with `def upsert(self, items: list[Mapping[str, object]]) ->
dict[str, object]` and `def search(self, vector: list[float], k: int) -> list[dict[str, object]]`. `class
FakeVectorStore` (`__init__(self)`; in-memory `dict[str, Mapping[str, object]]` keyed by `id`; `upsert`
records `self.calls += 1`, overwrites by `id` (not append — re-upserting the same id updates it), returns
`{"upserted": len(items)}`; `search` computes exact cosine similarity against every stored vector, sorts
best-first, returns the top `k`).

- [ ] **Step 1: Failing test**
```python
# backend/tests/unit/test_vectorstore.py
import math

from fdi.pipeline.vectorstore import FakeVectorStore, VectorStore


def test_fake_upsert_records_calls_and_returns_count() -> None:
    fake = FakeVectorStore()
    store: VectorStore = fake
    items = [{"id": "doc1:s0", "vector": [1.0, 0.0], "metadata": {"heading": "Balance Sheet"}}]
    receipt = store.upsert(items)
    assert receipt == {"upserted": 1}
    assert fake.calls == 1


def test_fake_search_ranks_by_cosine_similarity_best_first() -> None:
    fake = FakeVectorStore()
    fake.upsert(
        [
            {"id": "a", "vector": [1.0, 0.0], "metadata": {}},
            {"id": "b", "vector": [0.0, 1.0], "metadata": {}},
            {"id": "c", "vector": [0.9, 0.1], "metadata": {}},
        ]
    )
    results = fake.search([1.0, 0.0], k=2)
    assert [r["id"] for r in results] == ["a", "c"]
    assert results[0]["score"] == 1.0
    assert math.isclose(results[1]["score"], 0.9 / math.sqrt(0.9**2 + 0.1**2))


def test_fake_upsert_overwrites_by_id_not_duplicate() -> None:
    fake = FakeVectorStore()
    fake.upsert([{"id": "a", "vector": [1.0, 0.0], "metadata": {"heading": "old"}}])
    fake.upsert([{"id": "a", "vector": [0.0, 1.0], "metadata": {"heading": "new"}}])
    results = fake.search([0.0, 1.0], k=5)
    assert len(results) == 1
    assert results[0]["metadata"]["heading"] == "new"


def test_fake_search_empty_store_returns_empty_list() -> None:
    fake = FakeVectorStore()
    assert fake.search([1.0, 0.0], k=5) == []
```
- [ ] **Step 2: Run → fails.**
- [ ] **Step 3: Implement**
```python
# backend/src/fdi/pipeline/vectorstore.py
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
- [ ] **Step 4: Run → passes.**
- [ ] **Step 5: Commit** — `git add backend/src/fdi/pipeline/vectorstore.py backend/tests/unit/test_vectorstore.py && git commit -m "feat(pipeline): VectorStore protocol + FakeVectorStore"`

---

### Task 2: `MilvusVectorStore` (platform) + the `pymilvus` dependency

**Files:** Add dep to `backend/pyproject.toml` (+ `uv.lock`); Create `backend/src/fdi/platform/vectorstore.py`;
Test `backend/tests/unit/test_milvus_vectorstore.py`.

**Interfaces:** `class MilvusVectorStore` (implements `VectorStore`): `__init__(self, *, uri, collection,
dim)` builds `self._client = MilvusClient(uri=uri)` at construction; `upsert` returns `{"upserted": 0}`
without any client call when `items` is empty, else ensures the collection exists
(`has_collection`/`create_collection` with `dimension=dim`, `metric_type="COSINE"`) and calls
`self._client.upsert(...)`; `search` returns `[]` without any search call when `has_collection()` is
`False`, else calls `self._client.search(...)` and maps hits to `{id, score, metadata}`; plus
`from_settings(settings) -> MilvusVectorStore | None` (`None` when `milvus_uri` unset).

- [ ] **Step 1: Add the dependency** — `uv add --directory backend pymilvus`. Commit `pyproject.toml` +
  `uv.lock` with this task. Pin the version floor to the Compose server's major.minor for protocol
  compatibility (same reasoning as SP-9b's `neo4j>=5.26`): `"pymilvus>=2.6"` in `dependencies`.
  `pymilvus` ships NO `py.typed` marker (verified against its own `pyproject.toml` — no `package-data`/
  `py.typed` declaration); with `ignore_missing_imports = true` its surface resolves to `Any` under
  `mypy --strict`. No stub package is installed for it, matching the existing `neo4j`/`openai` precedent of
  relying on `ignore_missing_imports` rather than a stub dependency.
- [ ] **Step 2: Failing test** — MONKEYPATCH the client (no real Milvus call): patch
  `fdi.platform.vectorstore.MilvusClient` with a fake class recording every `has_collection`/
  `create_collection`/`upsert`/`search` call and returning canned data; assert `MilvusVectorStore(...)`
  creates the collection only when absent, upserts the right rows (`id`/`vector`/flattened `metadata`),
  maps `search` hits to `{id, score, metadata}`, skips ALL client calls for an empty `upsert([])`, and
  returns `[]` from `search` without calling `.search` when the collection does not exist; also
  `from_settings` returns `None` when `milvus_uri` is `None`.
```python
# backend/tests/unit/test_milvus_vectorstore.py
import pytest


def test_milvus_store_creates_collection_then_upserts(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform import vectorstore as vs

    calls: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, uri: str) -> None:
            calls["uri"] = uri
            self._exists = False

        def has_collection(self, name: str) -> bool:
            calls["has_collection_name"] = name
            return self._exists

        def create_collection(self, **kwargs: object) -> None:
            calls["create_collection"] = kwargs
            self._exists = True

        def upsert(self, **kwargs: object) -> None:
            calls["upsert"] = kwargs

        def search(self, **kwargs: object) -> list[list[dict[str, object]]]:
            calls["search"] = kwargs
            return [[{"id": "doc1:s0", "distance": 0.99, "entity": {"heading": "Balance Sheet"}}]]

    monkeypatch.setattr(vs, "MilvusClient", _FakeClient)

    store = vs.MilvusVectorStore(uri="http://h:19530", collection="fdi_sections", dim=4)
    assert calls["uri"] == "http://h:19530"

    receipt = store.upsert(
        [{"id": "doc1:s0", "vector": [0.1, 0.2, 0.3, 0.4], "metadata": {"heading": "Balance Sheet"}}]
    )
    assert receipt == {"upserted": 1}
    assert calls["create_collection"]["collection_name"] == "fdi_sections"
    assert calls["create_collection"]["dimension"] == 4
    assert calls["create_collection"]["metric_type"] == "COSINE"
    assert calls["upsert"]["data"] == [
        {"id": "doc1:s0", "vector": [0.1, 0.2, 0.3, 0.4], "heading": "Balance Sheet"}
    ]

    hits = store.search([0.1, 0.2, 0.3, 0.4], k=1)
    assert hits == [{"id": "doc1:s0", "score": 0.99, "metadata": {"heading": "Balance Sheet"}}]
    assert calls["search"]["limit"] == 1


def test_milvus_store_skips_empty_upsert(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform import vectorstore as vs

    touched: list[str] = []

    class _FakeClient:
        def __init__(self, uri: str) -> None:
            pass

        def has_collection(self, name: str) -> bool:
            touched.append("has_collection")
            return False

        def create_collection(self, **kwargs: object) -> None:
            touched.append("create_collection")

        def upsert(self, **kwargs: object) -> None:
            touched.append("upsert")

    monkeypatch.setattr(vs, "MilvusClient", _FakeClient)
    store = vs.MilvusVectorStore(uri="http://h:19530", collection="fdi_sections", dim=4)
    assert store.upsert([]) == {"upserted": 0}
    assert touched == []


def test_milvus_store_search_returns_empty_when_collection_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fdi.platform import vectorstore as vs

    class _FakeClient:
        def __init__(self, uri: str) -> None:
            pass

        def has_collection(self, name: str) -> bool:
            return False

    monkeypatch.setattr(vs, "MilvusClient", _FakeClient)
    store = vs.MilvusVectorStore(uri="http://h:19530", collection="fdi_sections", dim=4)
    assert store.search([0.1, 0.2, 0.3, 0.4], k=5) == []


def test_from_settings_none_when_unconfigured() -> None:
    from fdi.platform.config import Settings
    from fdi.platform.vectorstore import MilvusVectorStore

    s = Settings(
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        broker_url="amqp://guest:guest@h:5672/%2F", result_backend="redis://h:6379/0",
        s3_endpoint="h:9000", s3_access_key="ak", s3_secret_key="sk",
    )
    assert MilvusVectorStore.from_settings(s) is None
```
- [ ] **Step 3: Run → fails** (`ModuleNotFoundError` / import).
- [ ] **Step 4: Implement**
```python
# backend/src/fdi/platform/vectorstore.py
"""MilvusVectorStore -- VectorStore backed by a real Milvus instance via
pymilvus's MilvusClient.

Lives in `platform` (does I/O), not `pipeline`: the `VectorStore` protocol and
`FakeVectorStore` live in `fdi.pipeline.vectorstore`. This module structurally
satisfies that protocol without importing it -- `platform` may not import
`pipeline` per the layered architecture contract.

`MilvusClient` is imported at module level (rather than inside `__init__`) so
tests can monkeypatch `fdi.platform.vectorstore.MilvusClient` with a fake --
no live Milvus call in the suite.

pymilvus ships NO py.typed marker (design spec sec 2.3) -- every value that
crosses this module's boundary is rebuilt into an explicitly-typed dict by
hand, never returned raw, so mypy --strict's warn_return_any has nothing to
catch here; no blanket ignore is needed."""

from collections.abc import Mapping
from typing import cast

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
            # No Milvus call at all for an empty batch -- creating a
            # collection for a document with nothing to store is an unforced
            # side effect. Mirrors IndexStage's own zero-chunk skip one layer
            # up (belt-and-suspenders: correct even if ever called directly).
            return {"upserted": 0}
        self._ensure_collection()
        rows = [
            {
                "id": item["id"],
                "vector": item["vector"],
                **dict(cast(Mapping[str, object], item.get("metadata") or {})),
            }
            for item in items
        ]
        self._client.upsert(collection_name=self._collection, data=rows)
        return {"upserted": len(items)}

    def search(self, vector: list[float], k: int) -> list[dict[str, object]]:
        if not self._client.has_collection(self._collection):
            # Nothing has been indexed yet -- the same state FakeVectorStore
            # represents as an empty `_items` dict, not an error. Design
            # spec sec 2.8: fake and real must agree here explicitly.
            return []
        hits = self._client.search(
            collection_name=self._collection,
            data=[vector],
            limit=k,
            output_fields=["*"],
        )
        results: list[dict[str, object]] = []
        for hit in hits[0]:
            entity = dict(cast(Mapping[str, object], hit.get("entity", {})))
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
- [ ] **Step 5: Run → passes**; `mypy --strict` — every returned value is a hand-built `dict`/`list`, so
  this should need NO `# type: ignore`; if a real stub gap surfaces, scope a narrow ignore with a reason
  comment, same discipline as SP-9b/SP-13. Confirm `lint-imports` keeps the contract
  (`fdi/platform/vectorstore.py` must NOT import from `fdi.pipeline`).
- [ ] **Step 6: Commit** — `git add backend/pyproject.toml backend/uv.lock backend/src/fdi/platform/vectorstore.py backend/tests/unit/test_milvus_vectorstore.py && git commit -m "feat(platform): MilvusVectorStore over pymilvus's MilvusClient"`

---

### Task 3: `FDI_MILVUS_*` config (Settings + compose x-backend-env + env)

**Files:** Modify `backend/src/fdi/platform/config.py`, `docker/compose.yml` (`x-backend-env` only —
the `etcd`/`milvus` services' own blocks are untouched, see Global Constraints), `.env.example`; Test
`backend/tests/unit/test_config.py` (ADD).

**Interfaces:** Settings gains `milvus_uri: str | None = None`, `milvus_collection: str = "fdi_sections"`.

- [ ] **Step 1: Add fields** to `config.py` beside the `neo4j_*` fields:
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
  Add both to `docker/compose.yml`'s `x-backend-env` block:
```yaml
FDI_MILVUS_URI: ${FDI_MILVUS_URI}
FDI_MILVUS_COLLECTION: ${FDI_MILVUS_COLLECTION}
```
  Add to `.env.example` with a real working default (NOT blank — see rationale above and design spec §3.5):
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
- [ ] **Step 2: Test** — a unit test that `Settings()` (with `REQUIRED` env set, per the existing
  `test_config.py` fixture pattern) exposes the Milvus defaults:
```python
def test_milvus_settings_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.milvus_uri is None
    assert s.milvus_collection == "fdi_sections"
```
  Run → fails, implement (Step 1 already did), → passes.
- [ ] **Step 3: Gates + config consistency** — `ruff`, `mypy`, confirm the two fields are in Settings +
  compose `x-backend-env` + `.env.example`.
- [ ] **Step 4: Commit** — `git add backend/src/fdi/platform/config.py docker/compose.yml .env.example backend/tests/unit/test_config.py && git commit -m "feat(config): FDI_MILVUS_* settings"`

---

### Task 4: `IndexStage` (pipeline/stages) — proven with `FakeVectorStore`

**Files:** Create `backend/src/fdi/pipeline/stages/index.py`; Modify
`backend/src/fdi/pipeline/stages/__init__.py` (register it, alphabetically between `graphload` and
`linking`); Test `backend/tests/integration/test_index_stage.py`.

**Interfaces:** `class IndexStage` (`name="index"`, `version=1`, `deps=("embedding",)`); `run(ctx)`: reads
`embedding = await ctx.read("embedding")`; `store = cast(VectorStore, ctx.services["vector_store"])`;
narrows `chunks` to a list of dicts (defensive, mirrors `GraphLoadStage`/`EmbeddingStage`'s own
`isinstance` narrowing); builds one item per chunk (`id=f"{ctx.doc_id}:{chunk['chunk_id']}"`,
`vector=chunk["vector"]`, `metadata={"heading":..., "chunk_id":...}`); calls `store.upsert(items)` ONLY
when `items` is non-empty (design spec §2.10); returns `{"schema_version":1, "upserted": N}`.
Self-registers into `REGISTRY`.

- [ ] **Step 1: Failing test** (integration; run `ocr → normalize → ast → embedding → index` via
  `run_stage` in a LOCAL `StageRegistry`, `FakeOcrClient` + `FakeEmbeddingClient` + `FakeVectorStore`,
  mirroring `test_embedding_stage.py`'s shape exactly):
```python
# backend/tests/integration/test_index_stage.py
import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.pipeline.embeddings import FakeEmbeddingClient
from fdi.pipeline.intake import intake_document
from fdi.pipeline.keys import artifact_key, derived_input_hash
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.run import run_stage
from fdi.pipeline.stages.ast import AstStage
from fdi.pipeline.stages.embedding import EmbeddingStage
from fdi.pipeline.stages.index import IndexStage
from fdi.pipeline.stages.normalize import NormalizeStage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.pipeline.vectorstore import FakeVectorStore
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


async def test_index_stage_chains_off_embedding_and_caches(session_factory, store) -> None:
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
                {"role": "sectionHeading", "content": "Balance Sheet"},
                {"content": "Assets 100"},
            ],
            "tables": [],
        }
    )
    fake_embedder = FakeEmbeddingClient(dim=8)
    fake_store = FakeVectorStore()

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(EmbeddingStage())
    reg.register(IndexStage())
    resolver = DagResolver(reg.all())

    assert "index" in resolver.successors("embedding")  # the chain is automatic

    common = {
        "doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
        "store": store, "registry": reg, "resolver": resolver,
    }
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake_ocr}, **common)
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="embedding", run_id=str(uuid.uuid4()), services={"embedder": fake_embedder}, **common
    )
    index_services = {"vector_store": fake_store}
    await run_stage(stage_name="index", run_id=str(uuid.uuid4()), services=index_services, **common)
    # Second run: content-addressed cache hit -- must NOT call the fake store twice.
    await run_stage(stage_name="index", run_id=str(uuid.uuid4()), services=index_services, **common)
    assert fake_store.calls == 1

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    key = artifact_key(doc.id, "index", 1, derived_input_hash([facts["embedding"].output_hash]))
    body = await store.get_json(key)
    assert body == {"schema_version": 1, "upserted": 1}

    query_vector = fake_embedder.embed(["Balance Sheet\nAssets 100"])[0]
    hits = fake_store.search(query_vector, k=1)
    assert hits[0]["id"] == f"{doc.id}:s0"
    assert hits[0]["metadata"]["heading"] == "Balance Sheet"
```
- [ ] **Step 2: Run → fails.**
- [ ] **Step 3: Implement**
```python
# backend/src/fdi/pipeline/stages/index.py
"""IndexStage: a DERIVED stage that upserts SP-13's embedding artifact
(one chunk = one vector) into a VectorStore via an injected client. Auto-chains
off EmbeddingStage via `deps=("embedding",)`. The artifact is a small
deterministic receipt (upserted count) -- the vector-store write is the side
effect and the actual point; see design spec sec 2.9-10 for why a cache hit
does not re-run the upsert, and why zero chunks skips the call entirely
(unlike SP-9b's GraphLoadStage)."""

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

        upserted = cast(int, store.upsert(items)["upserted"]) if items else 0
        return {"schema_version": 1, "upserted": upserted}


REGISTRY.register(IndexStage())
```
  Register in `stages/__init__.py`, alphabetically between `graphload` and `linking`:
```python
from fdi.pipeline.stages import (
    index as index,  # noqa: F401 - import for its registration side effect
)
```
- [ ] **Step 4: Run → passes** (cache hit → `fake_store.calls == 1`); full non-e2e suite green; scan for
  any exact-list assertion on `successors("embedding")` before landing (none exists today — checked).
- [ ] **Step 5: Commit** — `git add backend/src/fdi/pipeline/stages backend/tests/integration/test_index_stage.py && git commit -m "feat(pipeline): IndexStage over the SP-13 embedding artifact"`

---

### Task 5: Wire `MilvusVectorStore` into the worker

**Files:** Modify `backend/src/fdi/workers/tasks.py` (`_run_stage_async` builds
`services["vector_store"] = MilvusVectorStore.from_settings(...)` alongside the existing `"ocr"`/
`"semantic"`/`"embedder"`/`"graph_store"` entries).

**Interfaces:** `_run_stage_async` extends its `services` dict with an optional `"vector_store"` entry
(from Settings; omitted if unconfigured — same pattern as every prior service).

- [ ] **Step 1: Read** `workers/tasks.py`'s `_run_stage_async` (currently builds `"ocr"`, `"semantic"`,
  `"embedder"`, `"graph_store"`).
- [ ] **Step 2: Implement**:
```python
# workers/tasks.py, inside _run_stage_async, beside the existing oc/sc/ec/gsc construction
from fdi.platform.vectorstore import MilvusVectorStore
oc = AzureOcrClient.from_settings(get_settings())
sc = AzureOpenAIClient.from_settings(get_settings())
ec = AzureOpenAIEmbeddingClient.from_settings(get_settings())
gsc = Neo4jGraphStore.from_settings(get_settings())
vsc = MilvusVectorStore.from_settings(get_settings())
services: dict[str, object] = {}
if oc is not None:
    services["ocr"] = oc
if sc is not None:
    services["semantic"] = sc
if ec is not None:
    services["embedder"] = ec
if gsc is not None:
    services["graph_store"] = gsc
if vsc is not None:
    services["vector_store"] = vsc
```
- [ ] **Step 3: Verify** — no new dedicated test for this task by itself (SP-14 does not add a
  `/documents/{id}/index` trigger route — design spec §3.6 — so unlike SP-9b/SP-13's worker-wiring tasks,
  which paired the wiring with a new trigger-route test, this task's only observable effect is
  `services["vector_store"]` becoming available inside a REAL Celery task path; that path is exercised for
  real only by Task 8's opt-in Milvus test, or indirectly by Task 4's integration test, which passes
  `vector_store` explicitly through a local `StageRegistry` rather than through this wiring). Run the full
  non-e2e suite green; `mypy --strict` (the new import resolves cleanly, `workers -> platform` is a legal
  edge already exercised by the `oc`/`sc`/`ec`/`gsc` lines above it); `lint-imports` unchanged (`1 kept, 0
  broken`).
- [ ] **Step 4: Commit** — `git add backend/src/fdi/workers/tasks.py && git commit -m "feat: wire MilvusVectorStore into the worker"`

---

### Task 6: `POST /search` — the new synchronous retrieval route

**Files:** Modify `backend/src/fdi/api/schemas.py` (`SearchRequest`/`SearchResultView`/`SearchResultsView`);
Modify `backend/src/fdi/api/deps.py` (`get_embedder`/`get_vector_store` + `EmbedderDep`/`VectorStoreDep`);
Modify `backend/src/fdi/api/app.py` (`lifespan` builds `app.state.embedder`/`app.state.vector_store`);
Modify `backend/src/fdi/api/routes.py` (`POST /search`); Test `backend/tests/integration/test_api.py`
(ADD + extend the `make_client` fixture with one new keyword-only parameter).

**Interfaces:** `POST /search` body `{"query": str, "k": int = 5}` → `{"results":[{"id","score",
"metadata"}]}`. `503` (naming which service is missing in `detail`) when `embedder` and/or `vector_store`
is unconfigured (`None`).

- [ ] **Step 1: Read** `api/deps.py` (the `get_store`/`StoreDep` pattern to mirror exactly — a plain
  `request.app.state.X` accessor plus an `Annotated[..., Depends(...)]` alias), `api/app.py`'s `lifespan`
  (where `app.state.artifact_store`/`app.state.resolver` are built — the two new services are built the
  SAME way, from `Settings`, once per app), and `test_api.py`'s `make_client` fixture (the `**overrides:
  str` env-var pattern this task's `dependency_overrides` parameter sits ALONGSIDE, not replaces).
- [ ] **Step 2: Failing tests** — extend `make_client`, then add the `/search` tests:
```python
# backend/tests/integration/test_api.py -- make_client gains ONE new parameter
@pytest.fixture
def make_client(postgres_url, minio_endpoint, amqp_url, redis_url, monkeypatch):
    @asynccontextmanager
    async def _make(
        *,
        raise_app_exceptions: bool = True,
        dependency_overrides: dict[object, object] | None = None,
        **overrides: str,
    ):
        env = {
            "FDI_DATABASE_URL": postgres_url,
            "FDI_BROKER_URL": amqp_url,
            "FDI_RESULT_BACKEND": redis_url,
            "FDI_S3_ENDPOINT": minio_endpoint,
            "FDI_S3_ACCESS_KEY": "minioadmin",
            "FDI_S3_SECRET_KEY": "minioadmin",
        }
        env.update(overrides)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

        from fdi.api.app import create_app

        app = create_app()
        if dependency_overrides:
            app.dependency_overrides.update(dependency_overrides)
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                yield client

    return _make
```
```python
# backend/tests/integration/test_api.py -- additions
# --------------------------------------------------------------------------
# Task 6 (SP-14): POST /search
# --------------------------------------------------------------------------


async def test_search_503_when_unconfigured(client: AsyncClient, _alembic_applied) -> None:
    r = await client.post("/search", json={"query": "Balance Sheet"})
    assert r.status_code == 503
    assert "embedder" in r.json()["detail"]
    assert "vector_store" in r.json()["detail"]


async def test_search_returns_the_best_match_first(make_client, _alembic_applied) -> None:
    from fdi.api.deps import get_embedder, get_vector_store
    from fdi.pipeline.embeddings import FakeEmbeddingClient
    from fdi.pipeline.vectorstore import FakeVectorStore

    fake_embedder = FakeEmbeddingClient(dim=8)
    fake_store = FakeVectorStore()
    fake_store.upsert(
        [
            {
                "id": "doc1:s0",
                "vector": fake_embedder.embed(["Balance Sheet\nAssets 100"])[0],
                "metadata": {"heading": "Balance Sheet", "chunk_id": "s0"},
            },
            {
                "id": "doc1:s1",
                "vector": fake_embedder.embed(["Notes\nSee appendix"])[0],
                "metadata": {"heading": "Notes", "chunk_id": "s1"},
            },
        ]
    )

    async with make_client(
        dependency_overrides={get_embedder: lambda: fake_embedder, get_vector_store: lambda: fake_store}
    ) as client:
        r = await client.post("/search", json={"query": "Balance Sheet\nAssets 100", "k": 1})

    assert r.status_code == 200
    body = r.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["id"] == "doc1:s0"
    assert body["results"][0]["metadata"]["heading"] == "Balance Sheet"
    assert body["results"][0]["score"] == 1.0
```
- [ ] **Step 3: Run → fails** (no `/search` route yet).
- [ ] **Step 4: Implement**
```python
# api/schemas.py -- additions
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
```python
# api/deps.py -- additions
from fdi.pipeline.embeddings import EmbeddingClient
from fdi.pipeline.vectorstore import VectorStore


def get_embedder(request: Request) -> EmbeddingClient | None:
    embedder: EmbeddingClient | None = request.app.state.embedder
    return embedder


def get_vector_store(request: Request) -> VectorStore | None:
    vector_store: VectorStore | None = request.app.state.vector_store
    return vector_store


EmbedderDep = Annotated[EmbeddingClient | None, Depends(get_embedder)]
VectorStoreDep = Annotated[VectorStore | None, Depends(get_vector_store)]
```
```python
# api/app.py -- lifespan additions, beside the existing app.state.artifact_store wiring
from fdi.platform.embeddings import AzureOpenAIEmbeddingClient
from fdi.platform.vectorstore import MilvusVectorStore
...
app.state.embedder = AzureOpenAIEmbeddingClient.from_settings(settings)
app.state.vector_store = MilvusVectorStore.from_settings(settings)
```
```python
# api/routes.py -- additions
from fdi.api.deps import EmbedderDep, VectorStoreDep
from fdi.api.schemas import SearchRequest, SearchResultsView, SearchResultView


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
- [ ] **Step 5: Run → passes**; full non-e2e suite green; gates green (`ruff`, `mypy --strict`,
  `lint-imports` — `api/deps.py` importing `fdi.pipeline.embeddings`/`fdi.pipeline.vectorstore` and
  `api/app.py` importing `fdi.platform.embeddings`/`fdi.platform.vectorstore` are both ordinary legal
  edges, no new `ignore_imports` entry needed).
- [ ] **Step 6: Commit** — `git add backend/src/fdi/api backend/tests/integration/test_api.py && git commit -m "feat: POST /search over the injected embedder + vector store"`

---

### Task 7: Compose `vector` profile — stale-comment fix + `@pytest.mark.milvus` registration

**Files:** Modify `docker/compose.yml` (the `milvus` service's comment ONLY — no `environment:`/
healthcheck change, see Global Constraints); Modify `backend/pyproject.toml` (register the `milvus` pytest
marker). `FDI_MILVUS_*` in `x-backend-env` was already added at Task 3; `pymilvus` was already added at
Task 2 — this task touches neither again.

**Interfaces:** the `milvus` service's comment changes from *"Defined now; wired at SP-13"* to accurately
say SP-14. `pyproject.toml`'s `[tool.pytest.ini_options] markers` gains `"milvus: needs a live Milvus
instance (FDI_MILVUS_URI, e.g. docker compose --profile vector up)"`.

- [ ] **Step 1: Fix the stale comment** on the `milvus` service in `docker/compose.yml`:
```yaml
  milvus:
    # NEVER :latest -- it is months stale. And do NOT copy the master-branch
    # compose: it points at v3.0-beta. Defined at project setup; wired at
    # SP-14 (FDI_MILVUS_* added to x-backend-env at SP-14 Task 3) -- standalone
    # Milvus has no bootstrap credential of its own to thread through this
    # service block, unlike neo4j's NEO4J_AUTH, so no environment:/healthcheck
    # change was needed here, only the config wiring above app-side.
    image: milvusdb/milvus:v2.6.20
    profiles: [vector]
    command: ["milvus", "run", "standalone"]
    environment:
      ETCD_ENDPOINTS: etcd:2379
      MINIO_ADDRESS: minio:9000
    volumes:
      - milvusdata:/var/lib/milvus
    ports: ["19530:19530"]
    depends_on: [etcd, minio]
```
  (The `command:`/`environment:`/`volumes:`/`ports:`/`depends_on:` lines are unchanged — shown only so the
  diff context is unambiguous; only the leading comment block changes.)
- [ ] **Step 2: Register the marker** in `backend/pyproject.toml`:
```toml
markers = [
    "integration: needs Docker containers",
    "e2e: needs the compose stack running",
    "azure: needs live Azure credentials (Document Intelligence and/or OpenAI)",
    "neo4j: needs a live Neo4j instance (FDI_NEO4J_URI/FDI_NEO4J_PASSWORD, or testcontainers[neo4j])",
    "milvus: needs a live Milvus instance (FDI_MILVUS_URI, e.g. docker compose --profile vector up)",
]
```
- [ ] **Step 3: Smoke-check** (manual, not a pytest test — heed README's OOM caution before running this
  on a constrained box): `docker compose -f docker/compose.yml --env-file .env --profile vector up -d etcd
  milvus` with `.env` copied fresh from `.env.example` (Task 3's real default `FDI_MILVUS_URI=http://milvus:19530`)
  → both containers reach a running state. `docker compose --profile vector down -v` after.
- [ ] **Step 4: Gates** — `ruff`, `mypy --strict src` (no source changed, should be a no-op check),
  `PYTHONUTF8=1 lint-imports`.
- [ ] **Step 5: Commit** — `git add docker/compose.yml backend/pyproject.toml && git commit -m "build(compose): correct milvus service comment; register the milvus pytest marker"`

---

### Task 8: Opt-in real-Milvus smoke test

**Files:** Create `backend/tests/milvus/__init__.py`, `backend/tests/milvus/test_real_vectorstore.py`.

**Interfaces:** `@pytest.mark.milvus`, skipped unless `FDI_MILVUS_URI` env is set (mirrors
`tests/neo4j/test_real_graph_load.py`'s skip pattern exactly). Builds
`MilvusVectorStore.from_settings(get_settings())`, upserts two orthogonal vectors, searches for the one
matching the first item, asserts the top hit is that item's id.

- [ ] **Step 1: Create the empty test package** — `backend/tests/milvus/__init__.py` (empty, mirrors
  `tests/neo4j/__init__.py`).
- [ ] **Step 2: Write the test** with a module-level skip:
```python
# backend/tests/milvus/test_real_vectorstore.py
"""Opt-in smoke test against a real Milvus instance.

Skipped unless FDI_MILVUS_URI is set. Never runs in CI or in an unconfigured
environment. The skipif marker is evaluated at collection time and does not
construct Settings or touch the network, so collection is always safe."""

import os
import uuid

import pytest

from fdi.platform.config import get_settings
from fdi.platform.vectorstore import MilvusVectorStore

pytestmark = [
    pytest.mark.milvus,
    pytest.mark.skipif(
        not os.getenv("FDI_MILVUS_URI"),
        reason="Milvus not configured",
    ),
]


def test_real_upsert_and_search_finds_the_match() -> None:
    store = MilvusVectorStore.from_settings(get_settings())
    assert store is not None

    doc_id = f"sp14-smoke-{uuid.uuid4()}"
    dim = get_settings().embed_dim
    match_vector = [1.0] + [0.0] * (dim - 1)
    other_vector = [0.0] * (dim - 1) + [1.0]
    items = [
        {"id": f"{doc_id}:s0", "vector": match_vector, "metadata": {"heading": "Balance Sheet"}},
        {"id": f"{doc_id}:s1", "vector": other_vector, "metadata": {"heading": "Notes"}},
    ]
    receipt = store.upsert(items)
    assert receipt == {"upserted": 2}

    hits = store.search(match_vector, k=1)
    assert hits[0]["id"] == f"{doc_id}:s0"
```
  (`pymilvus` has no first-class `testcontainers` module in this project's pinned `testcontainers==4.14.2`,
  unlike SP-9b's optional `testcontainers[neo4j]` path — this test requires an operator-started
  `docker compose --profile vector up -d etcd milvus`, or a real reachable instance, addressed by
  `FDI_MILVUS_URI`; it stays opt-in via the env-var skip either way.)
- [ ] **Step 3: Run** `uv run --directory backend pytest tests/milvus -rs` → skipped (Milvus unconfigured
  in this env, honest skip alongside the existing OCR/OpenAI/Neo4j smoke tests). If the operator has
  `FDI_MILVUS_URI` set and `docker compose --profile vector up` running, it runs for real.
- [ ] **Step 4: Commit** — `git add backend/tests/milvus && git commit -m "test(milvus): opt-in real Milvus vector-store smoke test"`

---

### Task 9: Audit + docs

**Files:** SP-14 spec status; `docs/architecture/ingestion-roadmap.md` (SP-14 row → done + handoff).

- [ ] **Step 1: Static gates** — `ruff`, `mypy --strict src`, `PYTHONUTF8=1 lint-imports` (`1 kept, 0
  broken`; `core` untouched; `pymilvus` import ONLY in `fdi/platform/vectorstore.py`). Config consistency:
  `FDI_MILVUS_*` in Settings + compose `x-backend-env` + `.env.example`.
- [ ] **Step 2: Full suite** — `uv run --directory backend pytest -q` (green; e2e + azure + neo4j + milvus
  skip; NO live Milvus call).
- [ ] **Step 3: Audit spec §7** — map each done criterion (1 VectorStore/FakeVectorStore, 2
  MilvusVectorStore, 3 IndexStage+zero-chunk-skip, 4 content-address+cache-no-2nd-upsert, 5 config, 6
  POST /search+503+dependency_overrides test, 7 opt-in smoke skips, 8 gates+core untouched) to its test.
- [ ] **Step 4: Spec status → Implemented `<date>`.**
- [ ] **Step 5: Roadmap update** — SP-14's row (`docs/architecture/ingestion-roadmap.md` §3 table): the
  `vector` Compose profile is now fully wired (`FDI_MILVUS_*` config, `IndexStage` projecting SP-13's
  embedding artifact into Milvus, `POST /search` answering similarity queries); phase 11's other five index
  types (metadata/entity/section/clause/table) remain explicitly deferred until their source artifacts
  exist; a cache hit does not re-run the upsert (bump `IndexStage.version` to force a reload); a superseded
  document version's stale vectors are not garbage-collected (SP-16 territory, same shape as SP-9b's own
  cross-document id-collision flag). Note this for whichever sub-project builds the remaining index types
  or hybrid/re-ranked search (both deferred here).
- [ ] **Step 6: Commit** — `git add docs && git commit -m "docs: SP-14 done-criteria audit, spec status, roadmap"`

## Self-Review

**Spec coverage:** §2 decisions → T1(VectorStore/FakeVectorStore) T2(MilvusVectorStore+pymilvus dep,
incl. #3's py.typed finding and #8's empty-collection search) T3(config, incl. #5's shared FDI_EMBED_DIM)
T4(IndexStage+cache+zero-chunk-skip, incl. #9/#10) T5(wire worker) T6(wire API+/search, incl. #11/#12)
T7(compose comment+marker) T8(opt-in real). §3 architecture → all (3.1 no-change implicit in T4 reusing
existing framework; 3.6 no dedicated trigger route, a deliberate omission explained in T5's Step 3). §4
errors → T4 (unconfigured service, cache hit never re-runs, malformed embedding degrades, zero-chunk
skip), T6 (503 on missing service(s), empty-collection search returns `200`/`[]` not an error). §5 testing
→ unit T1/T2/T3, integration T4/T6, milvus T8, compose T7. §7 done criteria → T9 audit.

**Type consistency:** `VectorStore.upsert(items: list[Mapping[str, object]]) -> dict[str, object]` /
`search(vector: list[float], k: int) -> list[dict[str, object]]` (T1) matches `FakeVectorStore` (T1),
`MilvusVectorStore` (T2), and `IndexStage` usage (T4). `StageContext.read`/`.doc_id`/`.services`
(pre-existing, confirmed via SP-9b/SP-13 precedent) match `IndexStage` (T4). SP-13's embedding artifact
shape (pre-existing, `chunks:[{chunk_id, heading, vector}]`) is what T4 reads and reshapes into
`VectorStore.upsert`'s item shape. `run_stage(..., services=)` (pre-existing) matches T4/T5 calls.
`MilvusVectorStore.from_settings` (T2) matches T5/T6. `EmbeddingClient.embed` (pre-existing, SP-13) matches
T6's `/search` route usage.

**Placeholder scan:** T5's "no new dedicated test" is a deliberate, explained omission (§3.6's decision not
to add a `/documents/{id}/index` trigger route), not a silent gap — mirrors SP-13's own Task 3
"no-framework-change, documented" pattern. T7's comment-only compose change is precise about why nothing
else in the service block needs to move (no bootstrap credential to thread through, unlike neo4j). T8's
"no first-class testcontainers module" is a stated fact about the pinned `testcontainers==4.14.2` version,
not an assumption — re-check if that dependency is ever upgraded. No TODO/TBD.
