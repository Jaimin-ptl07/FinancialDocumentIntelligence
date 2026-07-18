# SP-13b — Embeddings backend swap: Azure OpenAI → BGE-small (local, fastembed) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Replace SP-13's `AzureOpenAIEmbeddingClient` with `BgeEmbeddingClient`, backed by the local,
open-source `BAAI/bge-small-en-v1.5` model via `fastembed` (ONNX, CPU) — no endpoint, no key, no per-call
cost. The `EmbeddingClient` protocol, `EmbeddingStage`, and `FakeEmbeddingClient` (all `fdi.pipeline`) are
NOT touched: SP-13 already made the embedding backend an injected, swappable dependency, and this plan is
that swap.

**Architecture:** `BgeEmbeddingClient` (`fdi.platform.embeddings`) wraps `fastembed.TextEmbedding`,
constructing it LAZILY (on the first `embed()` call, not at `__init__`/`from_settings`) so wiring stays
cheap and no ~130 MB download happens at process boot. `from_settings(settings) -> BgeEmbeddingClient`
returns UNCONDITIONALLY (never `None` — BGE needs no credential to gate on), which ripples into BOTH
composition roots that build an embedder from `Settings` — `fdi/workers/tasks.py`'s `_run_stage_async` AND
`fdi/api/app.py`'s `lifespan` — plus `fdi/api/routes.py`'s `/search` 503 message and one `test_api.py`
assertion that assumed `embedder` could be unconfigured. `FDI_EMBED_*` config collapses from five fields
(Azure-shaped) to two: `embed_model` (default `"BAAI/bge-small-en-v1.5"`), `embed_dim` (default `384`).
Design: `docs/superpowers/specs/2026-07-18-sp13b-bge-embeddings-design.md`.

**Tech Stack:** Python 3.13, `fastembed` (NEW dependency this plan adds — ONNX runtime, CPU, no `torch`),
FastAPI, Celery, SQLAlchemy, MinIO, Postgres, pytest + testcontainers, `uv`.

## Global Constraints

- `uv run --directory backend <cmd>` — NEVER `uv --project`.
- Import contract `api | workers → pipeline → platform → core`, `exhaustive`, `1 kept, 0 broken`. `core`
  stays UNTOUCHED. `fastembed` is imported ONLY in `fdi/platform/embeddings.py` — nowhere else in the
  codebase names it. `pipeline` (`EmbeddingClient` protocol, `EmbeddingStage`, `FakeEmbeddingClient`) is
  UNCHANGED — this plan touches zero files under `fdi/pipeline/`.
- **Do not break SP-2/5/8/9b/10/11/12/13/14:** `StageContext`/`run_stage` are used AS-IS. Existing
  ping/ocr/normalize/ast/semantic/ontology/structure/quality/graph/provenance/embedding/graphload/index
  tests keep passing UNMODIFIED except the two named in Task 4 (`test_api.py`'s
  `test_search_503_when_unconfigured` rename/narrow) — `tests/integration/test_embedding_stage.py` in
  particular must pass with ZERO edits (it is the regression evidence that `EmbeddingStage`'s contract
  survived the swap, design spec §5).
- **`fastembed` ships NO `py.typed` marker (VERIFY against the installed version's own package metadata at
  Task 1 — do not assume; confirm the same way SP-14 confirmed `pymilvus` ships none).** With
  `ignore_missing_imports = true` (already project-wide), its surface resolves to `Any` under `mypy
  --strict`. Discipline: `BgeEmbeddingClient` never returns a raw `fastembed` value — `embed()` rebuilds
  every vector into a plain `list[float]` via `.tolist()` by hand, so `warn_return_any` has nothing to
  catch (same discipline `MilvusVectorStore` uses for `pymilvus`, `fdi/platform/vectorstore.py`). A narrow,
  reason-commented `# type: ignore[...]` is added only if a real gap remains after that — never a
  package-wide ignore.
- **The model is constructed LAZILY, on the first `embed()` call, cached on the instance.**
  `from_settings` is now called UNCONDITIONALLY at every process start (no endpoint/key gate to skip it
  on) — eager construction would make every API/worker process boot pay a ~130 MB download/load. Proven in
  Task 3 by asserting `TextEmbedding` is NOT constructed at `__init__`/`from_settings` time, only on first
  `embed()`, and reused (not rebuilt) on a second `embed()` call.
- **`from_settings` returns unconditionally — never `None`.** This is a NEW shape vs. every other
  `from_settings` in this codebase; the `if x is not None:` guards at both composition-root call sites are
  KEPT (uniform shape with the sibling `oc`/`sc`/`gsc`/`vsc` services) even though now permanently true —
  not special-cased away. See Task 4.
- Any new `FDI_` field → Settings AND compose `x-backend-env` AND `.env.example`, in lockstep (inert-config
  lesson — `Settings._reject_unknown_fdi_env_vars` crashes the app at boot on a stray `FDI_` var). The
  REVERSE direction matters equally here: `embed_endpoint`/`embed_key`/`embed_deployment`/`embed_api_version`
  must be removed from ALL THREE together, or a survivor in compose/`.env.example` alone is a boot crash.
- **No live model download in the default suite:** all unit/integration tests use monkeypatched
  `fastembed.TextEmbedding` or `FakeEmbeddingClient` (unchanged). The real-BGE test is `@pytest.mark.bge`,
  a NEW marker (registered alongside `azure`/`neo4j`/`milvus`), skipped unless `FDI_RUN_BGE_TEST=1` is set
  — NOT a config-presence gate like the other three (BGE has no "unconfigured" state to detect), a
  deliberate, explicit test-selection-only opt-in flag.
- Integration tests: reuse `session_factory`/`store`/`minio_client`/`migrated_engine`/`client`/`make_client`
  fixtures; mint unique ids/checksums per test; use a LOCAL `StageRegistry` (not the global `REGISTRY`)
  where a test asserts exact chain behavior, same as `test_embedding_stage.py`.
- On Windows prefix `lint-imports` with `PYTHONUTF8=1`.

**Canonical commands:** fast `pytest -m "not integration and not e2e and not azure and not neo4j and not
milvus and not bge" -q`; integration `pytest tests/integration -q`; gates `ruff check .` ·
`mypy --strict src` · `PYTHONUTF8=1 lint-imports --config .importlinter`.

---

### Task 1: `fastembed` dependency + `bge` pytest marker

**Files:** Modify `backend/pyproject.toml` (+ `uv.lock`).

**Interfaces:** none (dependency + marker registration only — no source yet).

- [ ] **Step 1: Add the dependency** — `uv add --directory backend fastembed`. Commit `pyproject.toml` +
  `uv.lock` together at the end of this task. `fastembed` pulls in `onnxruntime`, `huggingface-hub`,
  `tokenizers`, `numpy` — no `torch`, matching the project's CPU-only, no-GPU-service deployment posture
  (no `docker/compose.yml` service anywhere requests a GPU). Pin a floor in `dependencies` once `uv add`
  resolves the current release, e.g. `"fastembed>=0.4"` — treat that number as a placeholder to confirm
  against whatever `uv add` actually writes, the same caution SP-14 flagged for `pymilvus>=2.6`.
- [ ] **Step 2: Verify `py.typed` status** — check the installed package for a `fastembed/py.typed` marker
  file or a `package_data`/`py.typed` declaration in its own build metadata (the same check SP-14 ran
  directly against `pymilvus`'s `pyproject.toml`, design spec sec 2.3 there). Record the finding in Task
  3's module docstring (already drafted below assuming it ships none — correct the comment if verification
  finds otherwise; either way `ignore_missing_imports = true` already covers the "no stubs" case with no
  `mypy.ini`/`pyproject.toml` change needed).
- [ ] **Step 3: Register the marker** in `backend/pyproject.toml`:
```toml
markers = [
    "integration: needs Docker containers",
    "e2e: needs the compose stack running",
    "azure: needs live Azure credentials (Document Intelligence and/or OpenAI)",
    "neo4j: needs a live Neo4j instance (FDI_NEO4J_URI/FDI_NEO4J_PASSWORD, or testcontainers[neo4j])",
    "milvus: needs a live Milvus instance (FDI_MILVUS_URI, e.g. docker compose --profile vector up)",
    "bge: loads the real BAAI/bge-small-en-v1.5 model via fastembed (no live service/credentials, but a ~130 MB one-time download and real CPU inference; set FDI_RUN_BGE_TEST=1 to opt in)",
]
```
- [ ] **Step 4: Gates** — `ruff check .`, `mypy --strict src` (no source changed yet, should be a no-op),
  `PYTHONUTF8=1 lint-imports --config .importlinter` (no import edges changed yet).
- [ ] **Step 5: Commit** — `git add backend/pyproject.toml backend/uv.lock && git commit -m "build(deps): add fastembed; register the bge pytest marker"`

---

### Task 2: `FDI_EMBED_*` config restructure (Settings + compose + `.env.example`)

**Files:** Modify `backend/src/fdi/platform/config.py`, `docker/compose.yml`, `.env.example`; Modify
`backend/tests/unit/test_config.py`.

**Interfaces:** Settings' `embed_*` family shrinks from five fields to two: `embed_model: str =
"BAAI/bge-small-en-v1.5"` (replaces `embed_deployment`), `embed_dim: int = 384` (was `1536`).
`embed_endpoint`/`embed_key`/`embed_api_version` are deleted outright.

- [ ] **Step 1: Failing test** — replace `test_config.py`'s existing `test_embed_settings_default` in
  place (it currently asserts the SP-13 Azure shape):
```python
# backend/tests/unit/test_config.py — replaces the existing test_embed_settings_default
def test_embed_settings_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.embed_model == "BAAI/bge-small-en-v1.5"
    assert s.embed_dim == 384
```
- [ ] **Step 2: Run → fails** (`AttributeError: 'Settings' object has no attribute 'embed_model'` against
  the current SP-13 Settings shape).
- [ ] **Step 3: Implement** — replace the `embed_*` block in `backend/src/fdi/platform/config.py`:
```python
# before (SP-13 shape, five fields)
embed_endpoint: str | None = None
embed_key: str | None = None
embed_deployment: str = "text-embedding-3-small"
embed_api_version: str = "2024-10-21"
embed_dim: int = 1536

# after
# BGE-small embeddings (fastembed, local CPU/ONNX). Swapped in at SP-13b,
# replacing SP-13's Azure OpenAI embedding backend -- BAAI/bge-small-en-v1.5
# runs locally, so there is no endpoint/key/api_version to configure, unlike
# azure_di_*/gpt_*. embed_dim is the model's native output size (384) and is
# NOT sent to fastembed as a request parameter (unlike Azure's `dimensions=`
# truncation option, which fastembed has no equivalent of) -- it is
# provenance/documentation only, plus what MilvusVectorStore.from_settings
# (SP-14) reads to size the Milvus collection. Change it only alongside
# embed_model, and only to a value that model actually produces.
embed_model: str = "BAAI/bge-small-en-v1.5"
embed_dim: int = 384
```
  Replace the five `FDI_EMBED_*` lines in `docker/compose.yml`'s `x-backend-env` with two:
```yaml
FDI_EMBED_MODEL: ${FDI_EMBED_MODEL}
FDI_EMBED_DIM: ${FDI_EMBED_DIM}
```
  Replace the `FDI_EMBED_*` block in `.env.example` (currently five lines under the Azure-OpenAI-embeddings
  comment) with:
```
# BGE-small embeddings (fastembed, local CPU/ONNX) -- swapped in at SP-13b,
# replacing SP-13's Azure OpenAI embedding backend. No endpoint/key: the model
# runs locally, downloaded once (~130 MB) to fastembed's local cache on first
# use. FDI_EMBED_DIM (384) is the model's native output size, not a request
# parameter -- change it only alongside FDI_EMBED_MODEL.
FDI_EMBED_MODEL=BAAI/bge-small-en-v1.5
FDI_EMBED_DIM=384
```
- [ ] **Step 4: Run → passes.** Confirm the NEGATIVE half too (INERT-CONFIG discipline, design spec §7.4):
  `embed_endpoint`/`embed_key`/`embed_deployment`/`embed_api_version` appear in NONE of `config.py`,
  `docker/compose.yml`, `.env.example` — a survivor in compose/`.env.example` alone (with the Settings
  field gone) is a boot-time crash via `_reject_unknown_fdi_env_vars`, not a lint warning.
- [ ] **Step 5: Gates** — `ruff check .`, `mypy --strict src`, `PYTHONUTF8=1 lint-imports`.
- [ ] **Step 6: Commit** — `git add backend/src/fdi/platform/config.py docker/compose.yml .env.example backend/tests/unit/test_config.py && git commit -m "feat(config): restructure FDI_EMBED_* for local BGE (drop endpoint/key/api_version, deployment->model, dim 1536->384)"`

---

### Task 3: `BgeEmbeddingClient` (platform) — REPLACES `AzureOpenAIEmbeddingClient`

**Files:** Rewrite `backend/src/fdi/platform/embeddings.py`; Delete
`backend/tests/unit/test_azure_embedding_client.py`; Create
`backend/tests/unit/test_bge_embedding_client.py`.

**Interfaces:** `class BgeEmbeddingClient` (implements `EmbeddingClient`, unimported here — `platform` may
not import `pipeline`): `__init__(self, model: str)` stores `self.model = model` and `self._impl: TextEmbedding
| None = None` — NO `TextEmbedding` construction here. `embed(self, texts: list[str]) -> list[list[float]]`
lazily builds (or reuses) `self._impl` via a private `_get_impl`, then returns `[v.tolist() for v in
impl.embed(texts)]`. `from_settings(settings) -> BgeEmbeddingClient` (no `| None`) returns
`BgeEmbeddingClient(settings.embed_model)` unconditionally.

- [ ] **Step 1: Failing test** — MONKEYPATCH `fastembed.TextEmbedding` (no real model load/download): a
  fake class that records every construction and every `embed()` call; assert lazy construction (none at
  `__init__`), single construction across two `embed()` calls (cached), `.tolist()`-mapped vectors, the
  public `.model` attribute, and that `from_settings` never triggers construction at all.
```python
# backend/tests/unit/test_bge_embedding_client.py
import pytest


class _FakeVector:
    def __init__(self, values: list[float]) -> None:
        self._values = values

    def tolist(self) -> list[float]:
        return self._values


def test_bge_client_is_lazy_and_caches_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform import embeddings as emb

    construct_calls: list[str] = []
    embed_calls: list[list[str]] = []

    class _FakeTextEmbedding:
        def __init__(self, model_name: str) -> None:
            construct_calls.append(model_name)

        def embed(self, texts: list[str]) -> list[_FakeVector]:
            embed_calls.append(list(texts))
            return [_FakeVector([0.1, 0.2, 0.3]) for _ in texts]

    monkeypatch.setattr(emb, "TextEmbedding", _FakeTextEmbedding)

    client = emb.BgeEmbeddingClient("BAAI/bge-small-en-v1.5")
    assert construct_calls == []  # no model construction at __init__ -- lazy

    vectors = client.embed(["Balance Sheet", "Income Statement"])
    assert vectors == [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]]
    assert construct_calls == ["BAAI/bge-small-en-v1.5"]  # constructed once

    client.embed(["Notes"])
    assert construct_calls == ["BAAI/bge-small-en-v1.5"]  # cached, NOT rebuilt
    assert embed_calls == [["Balance Sheet", "Income Statement"], ["Notes"]]


def test_bge_client_exposes_model_name() -> None:
    from fdi.platform.embeddings import BgeEmbeddingClient

    client = BgeEmbeddingClient("BAAI/bge-small-en-v1.5")
    assert client.model == "BAAI/bge-small-en-v1.5"


def test_from_settings_always_returns_a_client_with_no_model_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fdi.platform import embeddings as emb
    from fdi.platform.config import Settings

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("TextEmbedding must not be constructed by from_settings")

    monkeypatch.setattr(emb, "TextEmbedding", _boom)

    s = Settings(
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        broker_url="amqp://guest:guest@h:5672/%2F", result_backend="redis://h:6379/0",
        s3_endpoint="h:9000", s3_access_key="ak", s3_secret_key="sk",
    )
    client = emb.BgeEmbeddingClient.from_settings(s)
    assert client is not None
    assert client.model == "BAAI/bge-small-en-v1.5"
```
- [ ] **Step 2: Run → fails** (`ModuleNotFoundError`/`AttributeError` — `BgeEmbeddingClient` does not exist
  yet, `fdi.platform.embeddings` still defines `AzureOpenAIEmbeddingClient`).
- [ ] **Step 3: Implement** — rewrite `backend/src/fdi/platform/embeddings.py` entirely (deletes
  `AzureOpenAIEmbeddingClient` and the `from openai import AzureOpenAI` import):
```python
# fdi.platform.embeddings — full file
"""BgeEmbeddingClient -- EmbeddingClient backed by a local BGE-small model via
fastembed (BAAI/bge-small-en-v1.5, 384-dim, ONNX runtime, CPU).

Lives in `platform` (does I/O -- loads/downloads model weights), not
`pipeline`: the `EmbeddingClient` protocol, `EmbeddingStage`, and
`FakeEmbeddingClient` live in `fdi.pipeline`. This module structurally
satisfies that protocol without importing it -- `platform` may not import
`pipeline` per the layered architecture contract.

`fastembed.TextEmbedding` is imported at module level (rather than inside
`embed`) so tests can monkeypatch `fdi.platform.embeddings.TextEmbedding` with
a fake -- no real model load/download anywhere in the default suite.

The MODEL ITSELF is constructed LAZILY: `TextEmbedding(model_name=...)` is
NOT called in `__init__` or `from_settings`, only on the first `embed()` call,
then cached on the instance. Constructing it eagerly would make wiring this
module -- which now happens UNCONDITIONALLY at every process start, since
`from_settings` never returns None -- download/load ~130 MB of ONNX weights at
API lifespan / every worker task, even for a process that never actually
embeds anything. A document with zero AST sections costs zero model loads:
EmbeddingStage already skips calling `embed()` entirely when there are no
chunks (unchanged from SP-13), and laziness means that skip also skips the
model load.

fastembed ships NO py.typed marker (verified against the installed version's
own package metadata, the same check SP-14 ran for pymilvus). With
`ignore_missing_imports = true` its surface resolves to Any under mypy
--strict. Discipline: every value crossing this module's boundary is rebuilt
by hand (`vector.tolist()` into a plain `list[float]`), never returned raw, so
warn_return_any has nothing to catch -- same discipline as MilvusVectorStore
(fdi/platform/vectorstore.py).
"""

from fastembed import TextEmbedding

from fdi.platform.config import Settings


class BgeEmbeddingClient:
    """EmbeddingClient backed by a local BGE-small model (fastembed, CPU).
    Lives in platform (does I/O: loads/downloads model weights on first use)."""

    def __init__(self, model: str) -> None:
        self.model = model
        self._impl: TextEmbedding | None = None

    def _get_impl(self) -> TextEmbedding:
        if self._impl is None:
            self._impl = TextEmbedding(model_name=self.model)
        return self._impl

    def embed(self, texts: list[str]) -> list[list[float]]:
        impl = self._get_impl()
        return [vector.tolist() for vector in impl.embed(texts)]

    @staticmethod
    def from_settings(settings: "Settings") -> "BgeEmbeddingClient":
        return BgeEmbeddingClient(settings.embed_model)
```
  Delete `backend/tests/unit/test_azure_embedding_client.py` (tests the now-deleted
  `AzureOpenAIEmbeddingClient`).
- [ ] **Step 4: Run → passes.** `mypy --strict` may need a scoped `# type: ignore` if `fastembed`'s (likely
  absent) stubs leave a real gap after the hand-typed `.tolist()` boundary — scope it narrowly with a
  reason comment, same caveat SP-13/SP-14 flagged for `openai`/`pymilvus`; confirm `lint-imports` keeps the
  contract (`fdi/platform/embeddings.py` must NOT import from `fdi.pipeline`; `fastembed` must appear
  nowhere else in the import graph).
- [ ] **Step 5: Commit** — `git add backend/src/fdi/platform/embeddings.py backend/tests/unit/test_bge_embedding_client.py backend/tests/unit/test_azure_embedding_client.py && git commit -m "feat(platform): BgeEmbeddingClient over fastembed, replacing AzureOpenAIEmbeddingClient"`

---

### Task 4: Worker + API composition-root rewire; `/search` 503 message; re-verify `EmbeddingStage` unchanged

**Files:** Modify `backend/src/fdi/workers/tasks.py`, `backend/src/fdi/api/app.py`,
`backend/src/fdi/api/routes.py`; Modify `backend/tests/integration/test_api.py`
(`test_search_503_when_unconfigured` → renamed/narrowed). Read-only re-verification of
`backend/tests/integration/test_embedding_stage.py` (must need NO edits).

**Interfaces:** `_run_stage_async`'s `ec = BgeEmbeddingClient.from_settings(get_settings())` (was
`AzureOpenAIEmbeddingClient.from_settings(...)`); the `if ec is not None: services["embedder"] = ec` guard
is UNCHANGED code, now permanently true underneath. `api/app.py`'s `app.state.embedder =
BgeEmbeddingClient.from_settings(settings)`. `routes.py`'s `/search` 503 detail string drops the stale
`FDI_EMBED_ENDPOINT`/`FDI_EMBED_KEY` mention; its `missing = [...]`/`if missing:` logic is unchanged.

- [ ] **Step 1: Read** `workers/tasks.py`'s `_run_stage_async` (builds `oc`/`sc`/`ec`/`gsc`/`vsc`) AND
  `api/app.py`'s `lifespan` (`app.state.embedder = AzureOpenAIEmbeddingClient.from_settings(settings)`) —
  TWO composition roots build an embedder from `Settings`, not one; both must change together or the API
  process (serves `POST /search`) and the worker process (runs `EmbeddingStage`) disagree about which
  embedding backend is live.
- [ ] **Step 2: Implement** the three source edits:
```python
# backend/src/fdi/workers/tasks.py
# before
from fdi.platform.embeddings import AzureOpenAIEmbeddingClient
...
ec = AzureOpenAIEmbeddingClient.from_settings(get_settings())

# after
from fdi.platform.embeddings import BgeEmbeddingClient
...
ec = BgeEmbeddingClient.from_settings(get_settings())
# `if ec is not None: services["embedder"] = ec` below is UNCHANGED -- `ec` can
# no longer be None (BgeEmbeddingClient.from_settings is unconditional), but
# the guard stays for uniform shape with oc/sc/gsc/vsc, not special-cased away.
```
```python
# backend/src/fdi/api/app.py
# before
from fdi.platform.embeddings import AzureOpenAIEmbeddingClient
...
app.state.embedder = AzureOpenAIEmbeddingClient.from_settings(settings)

# after
from fdi.platform.embeddings import BgeEmbeddingClient
...
app.state.embedder = BgeEmbeddingClient.from_settings(settings)
```
```python
# backend/src/fdi/api/routes.py -- message only, missing=/if missing: unchanged
# before
"(set FDI_EMBED_ENDPOINT/FDI_EMBED_KEY and/or FDI_MILVUS_URI)"
# after
"(set FDI_MILVUS_URI -- embeddings run locally via BGE and need no endpoint/key)"
```
- [ ] **Step 3: Run `test_api.py` → `test_search_503_when_unconfigured` now FAILS.** This is an EXPECTED
  regression signal, not a bug: that test asserted `"embedder" in r.json()["detail"]`, true only because
  Azure OpenAI embeddings were unconfigured in the test environment; `embedder` can never be `None` now, so
  the assertion is false. If this test still passed unmodified, the composition-root rewire did not
  actually reach the code path `/search` reads from — treat a still-green run here as a sign Step 2 was
  incomplete, not as good news.
- [ ] **Step 4: Fix the test** — replace it with the narrowed version:
```python
# backend/tests/integration/test_api.py — replaces test_search_503_when_unconfigured
async def test_search_503_when_vector_store_unconfigured(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.post("/search", json={"query": "Balance Sheet"})
    assert r.status_code == 503
    assert "vector_store" in r.json()["detail"]
    assert "embedder" not in r.json()["detail"]
```
  `test_search_returns_the_best_match_first` (unchanged file, same test) needs NO edit — it drives
  `/search` entirely through `app.dependency_overrides` for `get_embedder`/`get_vector_store` and never
  reads `app.state.embedder`.
- [ ] **Step 5: Run → full non-e2e suite green.** Specifically re-run
  `backend/tests/integration/test_embedding_stage.py` and confirm it passes with the file UNTOUCHED — the
  regression evidence that `EmbeddingStage`'s artifact shape (`{schema_version, model, dim, chunks}`) and
  content-addressed caching (`fake_embedder.calls == 1` after a second run) survived the backend swap,
  because the test that would catch a regression there was never edited to match one. Also re-run
  `test_trigger_embedding_on_a_document`/`_unknown_document_is_404` (unchanged) and confirm they still
  pass.
- [ ] **Step 6: Gates** — `ruff check .`, `mypy --strict src`, `PYTHONUTF8=1 lint-imports`.
- [ ] **Step 7: Commit** — `git add backend/src/fdi/workers/tasks.py backend/src/fdi/api/app.py backend/src/fdi/api/routes.py backend/tests/integration/test_api.py && git commit -m "feat: wire BgeEmbeddingClient into both the worker and API composition roots; fix /search's stale 503 message"`

---

### Task 5: Opt-in real-BGE smoke test; delete the opt-in real-Azure-embeddings test

**Files:** Create `backend/tests/bge/__init__.py`, `backend/tests/bge/test_real_bge_embedding.py`; Delete
`backend/tests/azure/test_real_embedding.py` (NOT the `backend/tests/azure/` directory —
`test_real_ocr.py`/`test_real_semantic.py` still live there, untouched by this SP).

**Interfaces:** `@pytest.mark.bge`, skipped unless `FDI_RUN_BGE_TEST=1` is set (a test-selection-only flag,
NOT a Settings field, checked via `os.getenv` directly — unlike `azure`/`neo4j`/`milvus`, which skip on
ABSENT config, BGE has no "unconfigured" state to detect: `BgeEmbeddingClient.from_settings` always
succeeds). Builds a real `BgeEmbeddingClient.from_settings(get_settings())`, embeds one canned string
against the real model, asserts the returned vector's length equals `settings.embed_dim`.

- [ ] **Step 1: Delete** `backend/tests/azure/test_real_embedding.py` (tests the deleted
  `AzureOpenAIEmbeddingClient`; `backend/tests/unit/test_azure_embedding_client.py` was already deleted at
  Task 3).
- [ ] **Step 2: Create the empty test package** — `backend/tests/bge/__init__.py` (empty, mirrors
  `tests/neo4j/__init__.py`/`tests/milvus/__init__.py`).
- [ ] **Step 3: Write the test** with a module-level skip:
```python
# backend/tests/bge/test_real_bge_embedding.py
"""Opt-in smoke test that actually loads the real BAAI/bge-small-en-v1.5
model via fastembed and embeds text.

Skipped unless FDI_RUN_BGE_TEST=1 is set. Unlike the azure/neo4j/milvus
opt-in tests, BGE needs no endpoint/key/URI -- BgeEmbeddingClient.from_settings
never returns None, so there is no "unconfigured" signal to gate on.
FDI_RUN_BGE_TEST is a test-selection-only flag (not a Settings field, never
reaches the app) that exists purely so this test's real cost -- a ~130 MB
one-time model download on first run plus real CPU inference -- stays opt-in
and never runs unattended in CI. Also excluded from the default suite via
`-m "not ... and not bge"`; this skipif is defense-in-depth in case that flag
is ever forgotten (same shape as the azure/neo4j/milvus opt-in tests' skipif).
"""

import os

import pytest

from fdi.platform.config import get_settings
from fdi.platform.embeddings import BgeEmbeddingClient

pytestmark = [
    pytest.mark.bge,
    pytest.mark.skipif(
        os.getenv("FDI_RUN_BGE_TEST") != "1",
        reason="real BGE model load/download not requested (set FDI_RUN_BGE_TEST=1)",
    ),
]


def test_real_bge_embedding_matches_configured_dim() -> None:
    settings = get_settings()
    client = BgeEmbeddingClient.from_settings(settings)
    vectors = client.embed(["Balance Sheet\nTotal Assets 1,00,000\nTotal Liabilities 1,00,000"])
    assert len(vectors) == 1
    assert len(vectors[0]) == settings.embed_dim
```
- [ ] **Step 4: Run** `uv run --directory backend pytest tests/bge -rs` → skipped (`FDI_RUN_BGE_TEST` unset
  in this env, honest skip alongside the existing OCR/semantic/graph/vector-store opt-in tests). With
  `FDI_RUN_BGE_TEST=1` set, it runs for real: downloads the model on first run (subsequent runs load from
  `fastembed`'s local cache) and asserts a 384-length vector.
- [ ] **Step 5: Commit** — `git add backend/tests/bge backend/tests/azure/test_real_embedding.py && git commit -m "test(bge): opt-in real BGE embedding smoke test; remove the opt-in real-Azure-embeddings test"`

---

### Task 6: Audit + docs

**Files:** `docs/superpowers/specs/2026-07-17-sp13-embeddings-design.md` (status note only — SP-13's spec
stays historically accurate, gains a forward pointer); `docs/architecture/ingestion-roadmap.md` (new SP-13b
row + handoff note).

- [ ] **Step 1: Static gates** — `ruff check .`, `mypy --strict src` (`fastembed` imported ONLY in
  `fdi/platform/embeddings.py`), `PYTHONUTF8=1 lint-imports --config .importlinter` (`1 kept, 0 broken`;
  `core`/`pipeline` untouched). Config consistency: `FDI_EMBED_MODEL`/`FDI_EMBED_DIM` in Settings + compose
  + `.env.example`; `FDI_EMBED_ENDPOINT`/`FDI_EMBED_KEY`/`FDI_EMBED_DEPLOYMENT`/`FDI_EMBED_API_VERSION` in
  NONE of the three.
- [ ] **Step 2: Full suite** — `uv run --directory backend pytest -q` (green; e2e/azure/neo4j/milvus/bge
  all skip; NO real model download, NO live Azure OpenAI call anywhere in the embedding path).
- [ ] **Step 3: Audit spec §7** — map each SP-13b done criterion (1 dependency+marker, 2
  BgeEmbeddingClient+lazy+unconditional-from_settings, 3 pipeline-layer unchanged
  [test_embedding_stage.py green+unedited], 4 config restructure lockstep, 5 both composition roots
  rewired, 6 /search message+test fixed, 7 old tests deleted/new tests added, 8 artifact shape/caching
  unchanged, 9 gates+no-download) to its test/step in this plan.
- [ ] **Step 4: SP-13 spec status note** — add one line near the top of
  `docs/superpowers/specs/2026-07-17-sp13-embeddings-design.md`'s Status block (do not rewrite the rest of
  that historical document):
```markdown
- **Status:** Implemented 2026-07-18. Companion plan:
  `docs/superpowers/plans/2026-07-17-sp13-embeddings.md`. **Embedding backend swapped to BGE-small
  (local, fastembed) in SP-13b** (`docs/superpowers/specs/2026-07-18-sp13b-bge-embeddings-design.md`) —
  `AzureOpenAIEmbeddingClient` described below no longer exists in the codebase; the `EmbeddingClient`
  protocol, `EmbeddingStage`, and `FakeEmbeddingClient` this spec designed are unchanged and still exactly
  as described.
```
- [ ] **Step 5: SP-13b spec status → Implemented** (or the actual implementation date) in
  `docs/superpowers/specs/2026-07-18-sp13b-bge-embeddings-design.md`'s own Status line.
- [ ] **Step 6: Roadmap** — insert a new row immediately after SP-13 in the phase table
  (`docs/architecture/ingestion-roadmap.md`, same phase number as SP-13 since this is a same-phase backend
  swap, not a new capability — mirrors how SP-7a-d share phase 4):
```markdown
| **SP-13** | Embeddings → Milvus | 10 | **heavy** | ✅ done (embedding artifact only; Milvus store is SP-14) |
| **SP-13b** | Embeddings backend swap: Azure OpenAI → BGE-small (local, fastembed) | 10 | light | ✅ done |
| **SP-14** | Retrieval Indexes | 11 | **heavy** | ✅ done (vector index + search only; metadata/entity/section/clause/table indexes deferred) |
```
  Handoff note: the embedding artifact's SHAPE is unchanged (`{schema_version:1, model, dim, chunks:[...]}`)
  — only its `model`/`dim` VALUES change (`"BAAI/bge-small-en-v1.5"`/`384` instead of
  `"text-embedding-3-small"`/`1536`); SP-14's `MilvusVectorStore` needs no code change, only awareness that
  a fresh `fdi_sections` Milvus collection created after this ships will be 384-dim, and that an operator
  with a live 1536-dim collection from a prior real Azure-backed run must drop and recreate it manually
  (`MilvusVectorStore._ensure_collection` only creates when absent, never migrates).
- [ ] **Step 7: Commit** — `git add docs && git commit -m "docs: SP-13b done-criteria audit, SP-13 spec status note, roadmap"`

## Self-Review

**Spec coverage:** §2 decisions → T1 (§2.3/§2.8 dependency+marker) T3 (§2.2/§2.4/§2.5's `BgeEmbeddingClient`
shape, lazy construction, unconditional `from_settings`) T2 (§2.6/§2.7 config restructure) T4 (§2.5's full
ripple: both composition roots, `/search` message, `test_api.py`) T5 (§2.8's opt-in-flag-not-config-gate
reasoning). §2.9 (Milvus dim downstream, no SP-14 code change) → covered by Task 6's handoff note, no task
touches `fdi/platform/vectorstore.py`. §3 architecture → T3(3.2) T2(3.3) T4(3.4/3.5) T1(3.6); 3.1/3.7
(no pipeline/route change) verified by T4 Step 5's unedited-file re-run and by no task touching
`fdi/pipeline/` or the trigger route. §4 errors → T3 (real load failure propagates, lazy-so-zero-sections-
costs-nothing), T4 (KeyError path retired as a production case, /search short-circuit before any real
embed call). §5 testing → unit T2/T3, integration T4 (re-verified unedited + the one narrowed test),
opt-in T5. §7 done criteria → T6 audit.

**Type consistency:** `BgeEmbeddingClient.embed(texts: list[str]) -> list[list[float]]` + `model: str` (T3)
structurally satisfies the UNCHANGED `EmbeddingClient` protocol (`fdi.pipeline.embeddings`, not touched by
any task) exactly as `AzureOpenAIEmbeddingClient` did — confirmed by T4 Step 5's unedited
`test_embedding_stage.py` passing, which exercises `EmbeddingStage`'s `cast(EmbeddingClient,
ctx.services["embedder"])` against whatever concrete object is in `services["embedder"]`.
`BgeEmbeddingClient.from_settings(settings) -> BgeEmbeddingClient` (T3, no `| None`) matches its two call
sites (T4: `workers/tasks.py`, `api/app.py`) and the deliberately-kept-but-now-inert `if ec is not None`
guard shape. `Settings.embed_model`/`embed_dim` (T2) match `BgeEmbeddingClient.from_settings`'s reads (T3)
and `MilvusVectorStore.from_settings`'s existing `dim=settings.embed_dim` read (SP-14, unmodified).

**Placeholder scan:** No TODO/TBD. T1's `fastembed>=0.4` version floor and py.typed finding are explicitly
flagged as "verify, don't assume" rather than silently guessed — the SP-14 precedent for `pymilvus>=2.6`
and its py.typed check. T3's possible `# type: ignore` is scoped to whatever gap fastembed's actual stub
coverage leaves, with a reason comment, same caveat SP-13/SP-14 resolved for `openai`/`pymilvus`. T4's
"expected test failure" in Step 3 is a deliberate TDD checkpoint (confirms the rewire reached the code
path), not a placeholder step.
