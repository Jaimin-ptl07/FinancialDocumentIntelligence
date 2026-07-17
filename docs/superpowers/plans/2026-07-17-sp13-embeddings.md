# SP-13 — Embeddings via Azure OpenAI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Embed each SP-8 AST section as one vector via Azure OpenAI embeddings, running as a derived
pipeline stage — proven end to end with a FAKE embedding client (no live Azure), plus an opt-in real
smoke test. SP-13 produces vectors only; the Milvus vector store + retrieval is SP-14.

**Architecture:** An `EmbeddingStage` (SP-2 derived stage, `deps=("ast",)`) reads the AST artifact via the
already-existing `ctx.read("ast")`, builds one text chunk per section (`heading + "\n" + paragraphs`),
calls an injected `EmbeddingClient` (fake in tests, `AzureOpenAIEmbeddingClient` in prod) once for all
chunks, and returns `{schema_version:1, model, dim, chunks:[{chunk_id, heading, vector}]}`.
Content-addressed on `derived_input_hash([ast.output_hash])` + `EmbeddingStage.version` — this caching is
**already generic** in SP-2's `run_stage` (no framework change, same as SP-10); the embedding call is
non-deterministic so "run once, cache" is deliberate.

**Tech Stack:** Python 3.13, `openai` (official SDK, `AzureOpenAI` client — already a dependency since
SP-10), FastAPI, Celery, SQLAlchemy, MinIO, Postgres, pytest + testcontainers, `uv`. Design:
`docs/superpowers/specs/2026-07-17-sp13-embeddings-design.md`.

## Global Constraints

- `uv run --directory backend <cmd>` — NEVER `uv --project`.
- Import contract `api | workers → pipeline → platform → core`, `exhaustive`. `core` stays pure — SP-13
  adds NOTHING to `fdi.core` (no new core module). The `AzureOpenAIEmbeddingClient` (does I/O) lives in
  `platform`. The `EmbeddingClient` protocol + `EmbeddingStage` + `FakeEmbeddingClient` live in `pipeline`.
- **Do not break SP-2/5/8/10/11/12:** `StageContext`/`run_stage` are used AS-IS — no signature changes.
  Existing ping/ocr/normalize/ast/semantic/ontology/structure/quality/graph/provenance tests keep
  passing untouched.
- Determinism exception: embedding artifacts are content-addressed on
  `derived_input_hash([ast.output_hash])` + `EmbeddingStage.version` (Azure OpenAI embeddings are not
  guaranteed byte-stable). A second `embedding` run on the same doc is a store cache hit that does NOT
  call Azure OpenAI — prove it with a call counter on the fake.
- **No live Azure OpenAI in the suite:** all unit/integration tests use `FakeEmbeddingClient`. The
  real-Azure-OpenAI test is `@pytest.mark.azure` (the SAME marker SP-5/SP-10 registered, already
  broadened to cover any live Azure service), skipped unless `FDI_EMBED_ENDPOINT`/`FDI_EMBED_KEY` are set.
- Any new `FDI_` field → Settings AND compose `x-backend-env` AND `.env.example` (inert-config lesson).
  `FDI_EMBED_ENDPOINT`/`FDI_EMBED_KEY` are `str | None = None` so empty→None boots cleanly.
- No new Compose service/profile: `EmbeddingStage` runs under the existing `core` profile; the `vector`
  profile (Milvus/etcd) stays SP-14's responsibility.
- No new dependency: `openai` is already in `backend/pyproject.toml` (SP-10). Do not touch
  `pyproject.toml`/`uv.lock` in this plan.
- Integration tests: reuse `session_factory`/`store`/`minio_client`/`migrated_engine` fixtures; mint
  unique ids/checksums per test (session-scoped DB); use a LOCAL `StageRegistry` (not the global
  `REGISTRY`) so each test's chain is isolated, same as `test_semantic_stage.py`.
- On Windows prefix `lint-imports` with `PYTHONUTF8=1`.

**Canonical commands:** fast `pytest -m "not integration and not e2e and not azure" -q`; integration
`pytest tests/integration -q`; gates `ruff check .` · `mypy --strict src` ·
`PYTHONUTF8=1 lint-imports --config .importlinter`.

---

### Task 1: `EmbeddingClient` protocol + `FakeEmbeddingClient` (pipeline)

**Files:** Create `backend/src/fdi/pipeline/embeddings.py`; Test
`backend/tests/unit/test_embedding_client.py`.

**Interfaces:** `class EmbeddingClient(Protocol)` with `model: str` and `def embed(self, texts:
list[str]) -> list[list[float]]`. `class FakeEmbeddingClient` (`__init__(self, dim: int = 8, model: str =
"fake-embedding")`; `embed` records `self.calls += 1`, extends `self.seen` with the texts, returns one
deterministic vector per text derived from `hashlib.sha256(text.encode()).digest()[:dim]`, each byte
scaled to `[0, 1)`).

- [ ] **Step 1: Failing test**
```python
# backend/tests/unit/test_embedding_client.py
from fdi.pipeline.embeddings import EmbeddingClient, FakeEmbeddingClient


def test_fake_is_deterministic_per_text() -> None:
    fake = FakeEmbeddingClient(dim=8)
    client: EmbeddingClient = fake
    v1 = client.embed(["Balance Sheet\nAssets 100"])
    v2 = client.embed(["Balance Sheet\nAssets 100"])
    assert v1 == v2
    assert len(v1[0]) == 8
    assert fake.calls == 2
    assert fake.seen == ["Balance Sheet\nAssets 100", "Balance Sheet\nAssets 100"]


def test_fake_differs_across_texts() -> None:
    fake = FakeEmbeddingClient(dim=8)
    v = fake.embed(["Balance Sheet", "Income Statement"])
    assert v[0] != v[1]


def test_fake_embeds_multiple_texts_in_one_call() -> None:
    fake = FakeEmbeddingClient(dim=4)
    vectors = fake.embed(["a", "b", "c"])
    assert len(vectors) == 3
    assert fake.calls == 1


def test_fake_exposes_model_name() -> None:
    fake = FakeEmbeddingClient(model="fake-embedding")
    assert fake.model == "fake-embedding"
```
- [ ] **Step 2: Run → fails.**
- [ ] **Step 3: Implement** — `Protocol` + `runtime_checkable`; `FakeEmbeddingClient` with
  `model`/`dim`/`calls`/`seen` and the sha256-digest-derived vector.
- [ ] **Step 4: Run → passes.**
- [ ] **Step 5: Commit** — `git add backend/src/fdi/pipeline/embeddings.py backend/tests/unit/test_embedding_client.py && git commit -m "feat(pipeline): EmbeddingClient protocol + FakeEmbeddingClient"`

---

### Task 2: `AzureOpenAIEmbeddingClient` (platform)

**Files:** Create `backend/src/fdi/platform/embeddings.py`; Test
`backend/tests/unit/test_azure_embedding_client.py`.

**Interfaces:** `class AzureOpenAIEmbeddingClient` (implements `EmbeddingClient`): `__init__(self, *,
endpoint, key, deployment, api_version, dim)`; builds `openai.AzureOpenAI(azure_endpoint=endpoint,
api_key=key, api_version=api_version)` at construction; stores `self.model = deployment`. `embed(self,
texts) -> list[list[float]]` calls `self._sdk.embeddings.create(model=self.model, input=texts,
dimensions=self._dim)`, returns `[d.embedding for d in resp.data]`. Plus `from_settings(settings) ->
AzureOpenAIEmbeddingClient | None` (`None` when endpoint/key unset).

- [ ] **Step 1: Failing test** — MONKEYPATCH the SDK (no real Azure OpenAI call): patch
  `fdi.platform.embeddings.AzureOpenAI` with a fake whose `embeddings.create(...)` returns a canned
  response object; assert `AzureOpenAIEmbeddingClient(...).embed([...])` returns the parsed vectors and
  the call carried the right `model=`/`input=`/`dimensions=`; also `from_settings` returns `None` when
  endpoint is `None`.
```python
# backend/tests/unit/test_azure_embedding_client.py — sketch
import pytest


def test_azure_client_calls_sdk_and_parses_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform import embeddings as emb

    calls: dict[str, object] = {}

    class _Datum:
        def __init__(self, vector: list[float]) -> None:
            self.embedding = vector

    class _Resp:
        data = [_Datum([0.1, 0.2, 0.3]), _Datum([0.4, 0.5, 0.6])]

    class _Embeddings:
        def create(self, **kwargs: object) -> _Resp:
            calls["kwargs"] = kwargs
            return _Resp()

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            calls["init"] = kwargs
        embeddings = _Embeddings()

    monkeypatch.setattr(emb, "AzureOpenAI", _Client)
    c = emb.AzureOpenAIEmbeddingClient(
        endpoint="e", key="k", deployment="text-embedding-3-small",
        api_version="2024-10-21", dim=3,
    )
    vectors = c.embed(["Balance Sheet", "Income Statement"])
    assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    kwargs = calls["kwargs"]
    assert kwargs["model"] == "text-embedding-3-small"
    assert kwargs["input"] == ["Balance Sheet", "Income Statement"]
    assert kwargs["dimensions"] == 3
    assert c.model == "text-embedding-3-small"


def test_from_settings_none_when_unconfigured() -> None:
    from fdi.platform.config import Settings
    from fdi.platform.embeddings import AzureOpenAIEmbeddingClient

    s = Settings(database_url="postgresql+asyncpg://u:p@h:5432/db",
                 broker_url="amqp://guest:guest@h:5672/%2F", result_backend="redis://h:6379/0",
                 s3_endpoint="h:9000", s3_access_key="ak", s3_secret_key="sk")
    assert AzureOpenAIEmbeddingClient.from_settings(s) is None
```
- [ ] **Step 2: Run → fails** (`ModuleNotFoundError`).
- [ ] **Step 3: Implement** — import `AzureOpenAI` from `openai` at module level (so monkeypatch works,
  same discipline as SP-10's `AzureOpenAIClient`). `__init__` builds `self._sdk`, stores `self.model =
  deployment` (public — §2.4 of the design) and `self._dim = dim`. `embed` calls `create(model=self.model,
  input=texts, dimensions=self._dim)`, returns `[d.embedding for d in resp.data]`. `from_settings` returns
  `None` if `endpoint`/`key` is `None` else the client built from the other three settings.
- [ ] **Step 4: Run → passes**; `mypy --strict` may need a scoped `# type: ignore` if `openai`'s stubs
  don't fully satisfy strict mode for the kwargs-heavy `create(...)` call (same caveat SP-10 flagged for
  `chat.completions.create`) — scope it narrowly with a reason comment; confirm `lint-imports` keeps the
  contract (`fdi/platform/embeddings.py` must NOT import from `fdi.pipeline`).
- [ ] **Step 5: Commit** — `git add backend/src/fdi/platform/embeddings.py backend/tests/unit/test_azure_embedding_client.py && git commit -m "feat(platform): AzureOpenAIEmbeddingClient over the openai SDK"`

---

### Task 3: `StageContext.services` — already exists, NO framework change

**Files:** none.

`EmbeddingStage` is a **derived** stage (like SP-10's `SemanticStage` and SP-8's `AstStage`), so it only
needs `ctx.read("ast")` (already general-purpose) and `ctx.services["embedder"]` (the existing bag). This
step exists only to make the "no new framework work" decision explicit and auditable, same as SP-10's
Task 3 — skip straight to Task 4.

- [ ] **Step 1: Read** `stage.py`/`run.py`, confirm `services` + derived-stage `input_hash`
  (`derived_input_hash([facts[d].output_hash for d in stage.deps])`) already do everything SP-13 needs.
  No commit for this task.

---

### Task 4: `FDI_EMBED_*` config (Settings + compose + env)

**Files:** Modify `backend/src/fdi/platform/config.py`, `docker/compose.yml`, `.env.example`; Test
`backend/tests/unit/test_config.py` (ADD).

**Interfaces:** Settings gains `embed_endpoint: str | None = None`, `embed_key: str | None = None`,
`embed_deployment: str = "text-embedding-3-small"`, `embed_api_version: str = "2024-10-21"`,
`embed_dim: int = 1536`.

- [ ] **Step 1: Add fields** to `config.py` beside the `gpt_*` fields:
```python
# Azure OpenAI embeddings. Endpoint/key default to None so the stack boots
# without it configured; the embedding stage using them is wired separately.
# See docker/compose.yml x-backend-env and .env.example. Unlike azure_di_*/
# gpt_*, there is no pre-existing bare-named source in the sibling .env for
# these -- an operator provisioning a real embeddings deployment sets these
# directly here. embed_dim is passed to the SDK call as `dimensions=`
# (text-embedding-3-* models only); it must match a dimension the deployed
# model actually supports.
embed_endpoint: str | None = None
embed_key: str | None = None
embed_deployment: str = "text-embedding-3-small"
embed_api_version: str = "2024-10-21"
embed_dim: int = 1536
```
  Add all five to `docker/compose.yml`'s `x-backend-env` block:
```yaml
FDI_EMBED_ENDPOINT: ${FDI_EMBED_ENDPOINT}
FDI_EMBED_KEY: ${FDI_EMBED_KEY}
FDI_EMBED_DEPLOYMENT: ${FDI_EMBED_DEPLOYMENT}
FDI_EMBED_API_VERSION: ${FDI_EMBED_API_VERSION}
FDI_EMBED_DIM: ${FDI_EMBED_DIM}
```
  Add to `.env.example` with placeholders (after the `FDI_GPT_*` block):
```
# Azure OpenAI embeddings. Endpoint/key are optional (str | None) -- empty is
# legal and the stack boots without it configured. No sibling-repo rename
# exists for these; set them directly once a real embeddings deployment is
# provisioned. FDI_EMBED_DIM is sent to Azure as `dimensions=` and is only
# honored by text-embedding-3-* deployments.
FDI_EMBED_ENDPOINT=
FDI_EMBED_KEY=
FDI_EMBED_DEPLOYMENT=text-embedding-3-small
FDI_EMBED_API_VERSION=2024-10-21
FDI_EMBED_DIM=1536
```
- [ ] **Step 2: Test** — a unit test that `Settings()` (with `REQUIRED` env set, per the existing
  `test_config.py` fixture pattern) exposes `embed_deployment == "text-embedding-3-small"` and
  `embed_endpoint is None` by default:
```python
def test_embed_settings_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.embed_endpoint is None
    assert s.embed_key is None
    assert s.embed_deployment == "text-embedding-3-small"
    assert s.embed_api_version == "2024-10-21"
    assert s.embed_dim == 1536
```
  Run → fails, implement (Step 1 already did), → passes.
- [ ] **Step 3: Gates + config consistency** — `ruff`, `mypy`, confirm the five fields are in Settings +
  compose + `.env.example`.
- [ ] **Step 4: Commit** — `git add backend/src/fdi/platform/config.py docker/compose.yml .env.example backend/tests/unit/test_config.py && git commit -m "feat(config): FDI_EMBED_* settings"`

---

### Task 5: `EmbeddingStage` (pipeline/stages) — proven with `FakeEmbeddingClient`

**Files:** Create `backend/src/fdi/pipeline/stages/embedding.py`; Modify
`backend/src/fdi/pipeline/stages/__init__.py` (register it, alphabetically between `ast` and `graph`);
Test `backend/tests/integration/test_embedding_stage.py`.

**Interfaces:** `class EmbeddingStage` (`name="embedding"`, `version=1`, `deps=("ast",)`); `run(ctx)`:
reads `ast = await ctx.read("ast")`; `client = cast(EmbeddingClient, ctx.services["embedder"])`; for each
AST section builds `chunk_id = f"s{i}"` and `text = heading + "\n" + "\n".join(paragraphs)`; calls
`client.embed(texts)` once for all chunks (skipped entirely if there are zero sections); returns
`{schema_version:1, model: client.model, dim: len(vectors[0]) if vectors else 0, chunks:[{chunk_id,
heading, vector}]}`. Self-registers into `REGISTRY`.

- [ ] **Step 1: Failing test** (integration; chain `ocr`→`normalize`→`ast`→`embedding` via `run_stage`
  with `FakeOcrClient` + `FakeEmbeddingClient`, same shape as `test_semantic_stage.py`):
```python
# backend/tests/integration/test_embedding_stage.py
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


async def test_embedding_stage_runs_and_caches(  # fixtures from conftest
    session_factory, store
) -> None:
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="a.pdf",
        doc_key=str(uuid.uuid4()),
        content_type="application/pdf",
        session_factory=session_factory,
        store=store,
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

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(EmbeddingStage())
    resolver = DagResolver(reg.all())

    assert "embedding" in resolver.successors("ast")  # the chain is automatic

    common = {
        "doc_id": doc.id,
        "correlation_id": "c1",
        "session_factory": session_factory,
        "store": store,
        "registry": reg,
        "resolver": resolver,
    }
    ocr_services = {"ocr": fake_ocr}
    embed_services = {"embedder": fake_embedder}
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()), services=ocr_services, **common)
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="embedding", run_id=str(uuid.uuid4()), services=embed_services, **common
    )
    # Second run: content-addressed cache hit -- must NOT call the fake embedder twice.
    await run_stage(
        stage_name="embedding", run_id=str(uuid.uuid4()), services=embed_services, **common
    )
    assert fake_embedder.calls == 1

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    embedding_key = artifact_key(
        doc.id, "embedding", 1, derived_input_hash([facts["ast"].output_hash])
    )
    body = await store.get_json(embedding_key)
    assert body["model"] == "fake-embedding"
    assert body["dim"] == 8
    assert [c["chunk_id"] for c in body["chunks"]] == ["s0"]
    assert body["chunks"][0]["heading"] == "Balance Sheet"
    assert len(body["chunks"][0]["vector"]) == 8
```
- [ ] **Step 2: Run → fails.**
- [ ] **Step 3: Implement** the stage (+ register in `stages/__init__.py`:
  `from fdi.pipeline.stages import embedding as embedding  # noqa: F401 - import for its registration side effect`,
  placed alphabetically between the `ast` and `graph` imports).
- [ ] **Step 4: Run → passes** (cache hit → `fake_embedder.calls == 1`); full non-e2e suite green.
- [ ] **Step 5: Commit** — `git add backend/src/fdi/pipeline/stages backend/tests/integration/test_embedding_stage.py && git commit -m "feat(pipeline): EmbeddingStage over the AST artifact"`

---

### Task 6: Wire the embedding client into the worker + API trigger

**Files:** Modify `backend/src/fdi/workers/tasks.py` (`_run_stage_async` builds
`services["embedder"] = AzureOpenAIEmbeddingClient.from_settings(...)` alongside the existing
`services["ocr"]`/`services["semantic"]`); `backend/src/fdi/api/routes.py` (`POST
/documents/{id}/embedding`); `backend/src/fdi/api/schemas.py` (`EmbeddingTriggerView`); Test
`backend/tests/integration/test_api.py` (ADD).

**Interfaces:** `_run_stage_async` extends its `services` dict with an optional `"embedder"` entry (from
Settings; omitted if unconfigured — same pattern as `"ocr"`/`"semantic"`). `POST
/documents/{doc_id}/embedding` → 202: `dispatcher.dispatch(doc_id, "embedding", correlation_id)`, 404 if
the document is unknown.

- [ ] **Step 1: Read** `workers/tasks.py` `_run_stage_async` (currently builds `"ocr"` and `"semantic"`
  services) and `routes.py`'s `trigger_semantic` (the route to mirror exactly).
- [ ] **Step 2: Failing test** — `POST /documents/{id}/embedding` on an intake'd doc → 202; unknown id →
  404 (mirrors the existing semantic trigger test in `test_api.py`).
```python
# backend/tests/integration/test_api.py — additions, mirroring test_trigger_semantic_*
async def test_trigger_embedding_on_a_document(client: AsyncClient, _alembic_applied) -> None:
    pdf = f"PDF-EMBEDDING-{uuid.uuid4()}".encode()
    up = await client.post(
        "/documents", files={"file": ("a.pdf", pdf, "application/pdf")}
    )
    doc_id = up.json()["id"]
    r = await client.post(f"/documents/{doc_id}/embedding")
    assert r.status_code == 202
    assert r.json() == {"doc_id": doc_id, "stage": "embedding"}


async def test_trigger_embedding_unknown_document_is_404(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.post("/documents/00000000-0000-0000-0000-000000000000/embedding")
    assert r.status_code == 404
```
- [ ] **Step 3: Implement**:
```python
# workers/tasks.py, inside _run_stage_async, beside the existing oc/sc construction
from fdi.platform.embeddings import AzureOpenAIEmbeddingClient
oc = AzureOcrClient.from_settings(get_settings())
sc = AzureOpenAIClient.from_settings(get_settings())
ec = AzureOpenAIEmbeddingClient.from_settings(get_settings())
services: dict[str, object] = {}
if oc is not None:
    services["ocr"] = oc
if sc is not None:
    services["semantic"] = sc
if ec is not None:
    services["embedder"] = ec
```
```python
# api/schemas.py
class EmbeddingTriggerView(BaseModel):
    doc_id: str
    stage: str


# api/routes.py, mirroring trigger_semantic
@router.post(
    "/documents/{doc_id}/embedding",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=EmbeddingTriggerView,
)
async def trigger_embedding(
    doc_id: str, session: SessionDep, correlation_id: CorrelationIdDep, dispatcher: DispatcherDep
) -> EmbeddingTriggerView:
    if await RegistryRepository(session).get_document(doc_id) is None:
        raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
    dispatcher.dispatch(doc_id, "embedding", correlation_id)
    return EmbeddingTriggerView(doc_id=doc_id, stage="embedding")
```
- [ ] **Step 4: Run → passes**; full non-e2e suite green; gates green.
- [ ] **Step 5: Commit** — `git add backend/src/fdi/workers/tasks.py backend/src/fdi/api/routes.py backend/src/fdi/api/schemas.py backend/tests/integration/test_api.py && git commit -m "feat: wire Azure OpenAI embedding client into the worker; POST /documents/{id}/embedding"`

---

### Task 7: Opt-in real-Azure-OpenAI-embeddings smoke test

**Files:** Create `backend/tests/azure/test_real_embedding.py`. No marker-description change needed —
SP-10 already broadened the `azure` marker to "needs live Azure credentials (Document Intelligence
and/or OpenAI)", which already covers the embeddings endpoint of the same OpenAI surface.

**Interfaces:** `@pytest.mark.azure`, skipped unless `FDI_EMBED_ENDPOINT` and `FDI_EMBED_KEY` env are
set. Builds `AzureOpenAIEmbeddingClient.from_settings(get_settings())`, embeds one canned text against the
real deployment, asserts the returned vector's length matches `settings.embed_dim`.

- [ ] **Step 1: Write the test** with a module-level skip:
```python
# backend/tests/azure/test_real_embedding.py
"""Opt-in smoke test against real Azure OpenAI embeddings.

Skipped unless FDI_EMBED_ENDPOINT and FDI_EMBED_KEY are set. Never runs in CI
or in an unconfigured environment. The skipif marker is evaluated at
collection time and does not construct Settings or touch the network, so
collection is always safe.
"""

import os

import pytest

from fdi.platform.config import get_settings
from fdi.platform.embeddings import AzureOpenAIEmbeddingClient

pytestmark = [
    pytest.mark.azure,
    pytest.mark.skipif(
        not (os.getenv("FDI_EMBED_ENDPOINT") and os.getenv("FDI_EMBED_KEY")),
        reason="Azure OpenAI embeddings not configured",
    ),
]


def test_real_embedding_matches_configured_dim() -> None:
    settings = get_settings()
    client = AzureOpenAIEmbeddingClient.from_settings(settings)
    assert client is not None
    vectors = client.embed(["Balance Sheet\nTotal Assets 1,00,000\nTotal Liabilities 1,00,000"])
    assert len(vectors) == 1
    assert len(vectors[0]) == settings.embed_dim
```
- [ ] **Step 2: Run** `uv run --directory backend pytest tests/azure -rs` → skipped (Azure OpenAI
  embeddings unconfigured in this env, honest skip alongside the existing OCR/semantic smoke tests). If
  the operator has `FDI_EMBED_*` creds set, it runs against the real deployment.
- [ ] **Step 3: Commit** — `git add backend/tests/azure/test_real_embedding.py && git commit -m "test(azure): opt-in real Azure OpenAI embeddings smoke test"`

---

### Task 8: Audit + docs

**Files:** SP-13 spec status; `docs/architecture/ingestion-roadmap.md` (SP-13 → done + handoff).

- [ ] **Step 1: Static gates** — `ruff`, `mypy --strict src`, `PYTHONUTF8=1 lint-imports` (`1 kept, 0
  broken`; `core` unchanged/pure; `AzureOpenAIEmbeddingClient` is in `platform`). Config consistency:
  `FDI_EMBED_*` in Settings + compose + `.env.example`.
- [ ] **Step 2: Full suite** — `uv run --directory backend pytest -q` (green; e2e + azure skip; NO live
  Azure OpenAI call).
- [ ] **Step 3: Audit spec §7** — map each done criterion (1 EmbeddingClient/FakeEmbeddingClient, 2
  AzureOpenAIEmbeddingClient, 3 EmbeddingStage+chunking, 4 content-address+cache-no-2nd-call, 5 config
  boots without Azure OpenAI, 6 POST trigger, 7 opt-in smoke skips, 8 gates) to its test.
- [ ] **Step 4: Spec status → Implemented 2026-07-17** (or the actual implementation date).
- [ ] **Step 5: Roadmap SP-13 → done** + handoff: the embedding artifact = `{schema_version:1, model,
  dim, chunks:[{chunk_id, heading, vector}]}` at the content-addressed key; **SP-14 (Retrieval Indexes)
  consumes it** to upsert vectors into Milvus (bringing up the `vector` Compose profile for the first
  time) and build metadata/entity/section/clause/table/graph indexes alongside it; the `EmbeddingClient`
  protocol means SP-3's harness can later measure chunking-granularity or embedding-model variants as
  candidates; `EmbeddingStage.version` must be bumped on chunking-unit/deployment/`FDI_EMBED_DIM` change.
- [ ] **Step 6: Commit** — `git add docs && git commit -m "docs: SP-13 done-criteria audit, spec status, roadmap"`

## Self-Review

**Spec coverage:** §2 decisions → T1(EmbeddingClient+FakeEmbeddingClient, incl. #4's `model` attribute)
T2(AzureOpenAIEmbeddingClient, incl. #8's `dimensions=`) T3(no framework change, documented)
T4(config, incl. #9's no-rename note) T5(EmbeddingStage+per-section chunking+cache, incl. #1/#7) T6(wire+
trigger) T7(opt-in real). §2.10 (no new Compose profile) → covered by Global Constraints, no task touches
`compose.yml` services/profiles beyond `x-backend-env`. §3 architecture → all (3.1 no-change explicitly
covered by T3). §4 errors → T5 (unconfigured service via missing services key, zero-sections no-op,
`zip(strict=True)` mismatch), T6 (`from_settings` None omitted from services). §5 testing → unit
T1/T2/T4, integration T5/T6, azure T7. §7 done criteria → T8 audit.

**Type consistency:** `EmbeddingClient.embed(texts: list[str]) -> list[list[float]]` + `model: str` (T1)
matches `FakeEmbeddingClient` (T1), `AzureOpenAIEmbeddingClient` (T2), and `EmbeddingStage` usage (T5).
`StageContext.read`/`services` (pre-existing, confirmed in T3) match `EmbeddingStage` (T5). `run_stage(...,
services=)` (pre-existing) matches T5/T6 calls. `AzureOpenAIEmbeddingClient.from_settings` (T2) matches
T6. `derived_input_hash`/`artifact_key` (pre-existing, SP-2) match T5's cache-key assertion.

**Placeholder scan:** T3 is a deliberate no-op task (documents why no framework change is needed rather
than leaving a silent gap), same as SP-10's T3. No TODO/TBD. T2's possible `# type: ignore` is scoped to
`openai`'s stub coverage with a reason, same caveat SP-10 flagged and resolved.
