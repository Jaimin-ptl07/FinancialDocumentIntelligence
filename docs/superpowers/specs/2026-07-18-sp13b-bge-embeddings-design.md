# SP-13b — Embeddings backend swap: Azure OpenAI → BGE-small (local, fastembed)

- **Status:** Implemented 2026-07-18. Companion plan:
  `docs/superpowers/plans/2026-07-18-sp13b-bge-embeddings.md`.
- **Depends on:** SP-13 (ships the `EmbeddingClient` protocol, `EmbeddingStage`, `FakeEmbeddingClient` —
  ALL THREE stay byte-for-byte unchanged here; SP-13b replaces only `AzureOpenAIEmbeddingClient`, the one
  `platform` implementation SP-13 shipped). SP-14 (`MilvusVectorStore.from_settings` reads
  `settings.embed_dim` to size the Milvus collection, `fdi/platform/vectorstore.py` line 91 — this
  automatically becomes 384 once SP-13b ships; SP-14's code needs no change, see §2.9).
- **Decision carried from an explicit user directive (not roadmap memory):** the user wants embeddings
  produced by the open-source **BGE-small** model (`BAAI/bge-small-en-v1.5`, 384-dim, CPU-only) via
  **fastembed**, not a third Azure OpenAI surface. OCR (Azure Document Intelligence, SP-5) and the
  semantic/ontology GPT stage (Azure OpenAI chat completions, SP-10/SP-11) are UNCHANGED — SP-13b replaces
  ONLY the embedding backend SP-13 shipped. `AzureOpenAIEmbeddingClient` is deleted, not deprecated in
  place; if a remote/Azure embedding option is ever needed again it re-enters through the same
  `EmbeddingClient` seam as a new `platform` implementation (§6).

## 1. Purpose

Swap the concrete implementation behind the `EmbeddingClient` seam SP-13 already built: replace
`fdi.platform.embeddings.AzureOpenAIEmbeddingClient` (Azure OpenAI, `.embeddings.create(...)`, paid,
remote) with `fdi.platform.embeddings.BgeEmbeddingClient` (BGE-small via `fastembed`, free, local, CPU).
Nothing above the `platform` layer changes: the `EmbeddingClient` protocol, `EmbeddingStage`, and
`FakeEmbeddingClient` (all `fdi.pipeline.embeddings` / `fdi.pipeline.stages.embedding`) are the exact
proof SP-13's design was right to make the embedding model an injected, swappable dependency — this SP is
that swap actually happening, sooner than SP-13's own §6 ("a smaller/cheaper deployment, or eventually a
non-Azure embedding model") anticipated.

Thin vertical slice: one new `platform` class, a two-field config restructure (five Azure-shaped fields
collapse to two: `embed_model`, `embed_dim`), the composition-root rewiring in BOTH processes that build an
embedder from `Settings` (the worker AND the API — see §2.5, a fact this SP had to discover, not just
carry over), and test/doc cleanup. No pipeline-framework change, no new stage, no new route.

## 2. Scope decisions (made autonomously, mirroring SP-13's grounded pattern)

1. **Chunking unit is unchanged — one chunk per AST section.** Inherited verbatim from SP-13 §2.1; not
   revisited here. `EmbeddingStage` (`fdi/pipeline/stages/embedding.py`) is not touched by this SP at all.
2. **`EmbeddingClient` protocol is unchanged.** SP-13 §2.2/§2.4 already anticipated a swappable embedding
   backend and gave the protocol exactly the shape a local model needs: `embed(texts: list[str]) ->
   list[list[float]]` plus a public `model: str` (SP-13 promoted `model` from private to protocol-visible
   specifically so the artifact could record which model produced a vector — `BgeEmbeddingClient.model`
   fills that same slot with `"BAAI/bge-small-en-v1.5"` instead of a deployment name). BGE is "just another
   `EmbeddingClient`" structurally satisfying the same protocol; zero protocol change.
3. **Library = `fastembed` (Qdrant's ONNX-runtime-backed embedding library), model =
   `BAAI/bge-small-en-v1.5`.** `fastembed` wraps `onnxruntime` for CPU inference with no `torch`
   dependency — the model ships pre-exported to ONNX, ready to run on commodity CPU, matching this
   project's deployment posture (`docker/compose.yml` has no GPU-enabled service anywhere; every existing
   `core`-profile service is CPU-only). This is a NEW dependency (`backend/pyproject.toml` gains
   `fastembed`) — unlike SP-13, which added nothing because `openai` was already present from SP-10.
4. **The model is constructed LAZILY — on the first `embed()` call, not in `__init__` or `from_settings`.**
   `fastembed.TextEmbedding(model_name=...)` downloads (first use) or loads-from-cache (subsequent uses)
   roughly 130 MB of ONNX weights and builds an inference session — real, non-trivial work. Because
   `BgeEmbeddingClient.from_settings` is now called UNCONDITIONALLY at every process start (§2.5 — there is
   no `endpoint`/`key` gate to skip it on), eager construction would mean importing/wiring this module in
   the API process's `lifespan` AND every Celery worker's `_run_stage_async` pays that download/load cost
   at boot, every time, even for a process that never actually embeds anything (e.g. a worker that only
   ever runs the `ocr` stage). Lazy construction means the cost is paid exactly once per process, exactly
   when the first real `embed()` call happens, and a document with zero AST sections triggers ZERO model
   loads at all (`EmbeddingStage` already skips calling `client.embed(...)` entirely when there are no
   chunks — SP-13 §3.4, unchanged — so the laziness composes with that existing skip for free).
5. **`from_settings(settings) -> BgeEmbeddingClient` returns UNCONDITIONALLY — never `None`.** This is a
   genuinely new shape in this codebase: every other `from_settings` (`AzureOcrClient`, the deleted
   `AzureOpenAIEmbeddingClient`, `AzureOpenAIClient`, `Neo4jGraphStore`, `MilvusVectorStore`) returns
   `X | None`, gated on an unset endpoint/URI/key, because each of those backends is an external service
   that may not be configured. BGE has nothing to gate on: it runs locally, needs no credential, no
   endpoint, no URI — "unconfigured" is not a state that exists for it. Ripple, found by reading (not
   assumed) every call site the design brief pointed at plus the ones it didn't:
   - `fdi/workers/tasks.py`'s `_run_stage_async` builds `ec = BgeEmbeddingClient.from_settings(...)` and
     keeps the existing `if ec is not None: services["embedder"] = ec` guard — now permanently true, kept
     anyway for uniform shape with the four sibling services (`oc`/`sc`/`gsc`/`vsc`), not special-cased
     away for one `if`'s worth of savings.
   - **`fdi/api/app.py`'s `lifespan` ALSO builds an embedder** — `app.state.embedder =
     AzureOpenAIEmbeddingClient.from_settings(settings)` — a SECOND composition root the design brief's
     "grep `embedder`/`AzureOpenAIEmbeddingClient` in `workers/tasks.py`" pointer did not name but that
     exists and must be swapped in lockstep, or the API process (which serves `POST /search`) and the
     worker process (which runs `EmbeddingStage`) would silently disagree about which embedding backend is
     live.
   - **`fdi/api/routes.py`'s `POST /search`** builds its "is search configured" check as `missing = [name
     for name, svc in (("embedder", embedder), ("vector_store", vector_store)) if svc is None]` and its 503
     detail string says `"(set FDI_EMBED_ENDPOINT/FDI_EMBED_KEY and/or FDI_MILVUS_URI)"`. Once `embedder`
     can never be `None` in a normally-running process, that message is stale (those two env vars no
     longer exist as of §2.7) and the `embedder` branch of `missing` is permanently empty — the check and
     branch are KEPT (uniform two-service shape, `vector_store` genuinely can still be unconfigured), only
     the message text changes.
   - **`backend/tests/integration/test_api.py`'s `test_search_503_when_unconfigured`** asserts `"embedder"
     in r.json()["detail"]` — true today only because Azure OpenAI embeddings are unconfigured in the test
     environment. After this swap `embedder` is never `None`, so that assertion becomes false; the test is
     renamed `test_search_503_when_vector_store_unconfigured` and narrowed to assert `"vector_store" in
     detail` and `"embedder" NOT in detail`. `test_search_returns_the_best_match_first` is unaffected — it
     uses `app.dependency_overrides` for `get_embedder`/`get_vector_store` directly and never touches
     `app.state.embedder`.
6. **No `dimensions=` truncation parameter — fastembed has no equivalent.** Azure OpenAI's
   `text-embedding-3-*` models accepted a `dimensions=` kwarg on `embeddings.create(...)` to truncate the
   native embedding to a requested smaller size (SP-13 §2.8); `fastembed.TextEmbedding.embed(...)` accepts
   no such parameter — `BAAI/bge-small-en-v1.5`'s output dimensionality is fixed at 384, full stop.
   `FDI_EMBED_DIM` therefore changes role: it is no longer a value SENT to the embedding call (there is
   nothing to send it to); it is now purely a DOCUMENTED/OBSERVED fact plus the one thing
   `MilvusVectorStore.from_settings` (SP-14, unchanged) reads to size the Milvus collection
   (`dim=settings.embed_dim`). `EmbeddingStage.run`'s own `"dim": len(vectors[0]) if vectors else 0` (SP-13
   §3.4, unchanged) still computes the OBSERVED dimension from the actual returned vectors, not from
   config — it will read `384` automatically once `BgeEmbeddingClient` is wired, for the same
   request-vs-observed-fact reason SP-13 §2.7 established, just with the "request" half now inert for the
   embed call specifically (still live for Milvus's collection-sizing use, §2.9).
7. **Config restructure, all three files in lockstep (INERT-CONFIG discipline — `Settings._reject_unknown_fdi_env_vars`
   crashes the app at boot on ANY `FDI_` var present in the container's environment that isn't a declared
   Settings field, so a var left behind in `docker/compose.yml`'s `x-backend-env` or `.env.example` after
   being removed from `Settings` is a boot-time crash, not a lint warning).** REMOVE `embed_endpoint`,
   `embed_key`, `embed_api_version` (Azure-only; nothing local needs them). REPLACE `embed_deployment` with
   **`embed_model: str = "BAAI/bge-small-en-v1.5"`**. CHANGE **`embed_dim: int` default `1536` → `384`**
   (BGE-small's native output size). Net Settings fields: `embed_model` + `embed_dim` — two, where SP-13
   had five. Mirrored exactly in `docker/compose.yml`'s `x-backend-env` (`FDI_EMBED_MODEL`,
   `FDI_EMBED_DIM` — the other three keys deleted) and `.env.example` (`FDI_EMBED_MODEL=BAAI/bge-small-en-v1.5`,
   `FDI_EMBED_DIM=384`, comment rewritten to say local/no-key instead of "optional endpoint/key").
8. **`fastembed` is a real new dependency; a `bge` pytest marker is registered alongside
   `azure`/`neo4j`/`milvus`.** Added via `uv add --directory backend fastembed` (`pyproject.toml` +
   `uv.lock`). Unlike `azure`/`neo4j`/`milvus` (whose opt-in tests skip on ABSENT credentials/URI — a
   config-presence gate), BGE has no such gate (§2.5: nothing to be "unconfigured"). The opt-in real-BGE
   test therefore skips on an explicit test-selection-only flag (`FDI_RUN_BGE_TEST=1`, not a Settings
   field, never reaches the app) instead of a config-absence check — see §5.
9. **Downstream, unchanged code, re-verified not re-written:** SP-14's `MilvusVectorStore.from_settings`
   (`fdi/platform/vectorstore.py` line 91: `dim=settings.embed_dim`) automatically creates a 384-dim
   collection once this ships — no SP-14 code change. The one OPERATIONAL (not code) hazard: an operator
   who already ran SP-13/SP-14 against a real Azure OpenAI deployment and has a live 1536-dim
   `fdi_sections` Milvus collection from that run will hit a dimension mismatch on the next `upsert`,
   because `MilvusVectorStore._ensure_collection` only creates a collection when ABSENT and never migrates
   an existing one's dimension (§6).

## 3. Architecture

### 3.1 No pipeline-framework or `pipeline`-layer change (byte-identical to SP-13)

`fdi/pipeline/embeddings.py` (`EmbeddingClient` protocol + `FakeEmbeddingClient`) and
`fdi/pipeline/stages/embedding.py` (`EmbeddingStage`) are NOT modified by this SP — diff: none. SP-13's
own reasoning (§3.1/§3.4) already covers everything this SP needs: `ctx.services["embedder"]` is a
protocol-typed slot the stage neither knows nor cares is now backed by a local ONNX model instead of a
remote HTTP call. This is re-verified (not re-argued) by leaving `tests/integration/test_embedding_stage.py`
untouched and green (§5).

### 3.2 `BgeEmbeddingClient` (platform, does I/O) — REPLACES `AzureOpenAIEmbeddingClient`

```python
# fdi.platform.embeddings — full file, replaces the SP-13 Azure version
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
`from_settings` never returns None (design spec sec 2.5) -- download/load
~130 MB of ONNX weights at API lifespan / every worker task, even for a
process that never actually embeds anything. A document with zero AST
sections costs zero model loads: EmbeddingStage already skips calling
`embed()` entirely when there are no chunks (unchanged from SP-13), and
laziness means that skip also skips the model load.

fastembed ships NO py.typed marker as of the version pinned in
pyproject.toml (verify directly against the installed version's own package
metadata, the same way SP-14 verified pymilvus ships none -- design spec
sec 2.3 there). With `ignore_missing_imports = true` its surface resolves to
Any under mypy --strict. Discipline: every value crossing this module's
boundary is rebuilt by hand (`vector.tolist()` into a plain `list[float]`),
never returned raw, so warn_return_any has nothing to catch -- same
discipline as MilvusVectorStore (fdi/platform/vectorstore.py).
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
`from_settings` takes no branch and cannot return `None` — see §2.5 for why that is deliberate and what it
ripples into. `_get_impl` is the entire "lazy, cache-on-instance" contract: called from `embed`, never from
`__init__`/`from_settings`/`from_settings`'s caller, so constructing a `BgeEmbeddingClient` — including via
`from_settings` at process boot — never touches the filesystem or network.

### 3.3 Config (Settings + x-backend-env + .env.example) — restructured, not appended

`config.py`'s `embed_*` block (currently `embed_endpoint`, `embed_key`, `embed_deployment`,
`embed_api_version`, `embed_dim`) is replaced with:
```python
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
`x-backend-env` (`docker/compose.yml`) and `.env.example` are mirrored exactly: two `FDI_EMBED_*` keys
where SP-13 had five (§2.7).

### 3.4 Composition-root wiring — TWO processes, not one

`fdi/workers/tasks.py`'s `_run_stage_async` and `fdi/api/app.py`'s `lifespan` both build an embedder from
`Settings` (§2.5). Both change identically: `from fdi.platform.embeddings import BgeEmbeddingClient`,
`BgeEmbeddingClient.from_settings(...)` in place of the deleted `AzureOpenAIEmbeddingClient.from_settings(...)`.
Neither call site's surrounding control flow changes — the worker's `if ec is not None:` guard and the
API's unconditional `app.state.embedder = ...` assignment are both left exactly as shaped, now with an
always-non-`None` producer underneath.

### 3.5 `POST /search`'s 503 message (routes.py) — text only, no logic change

```python
# before
"(set FDI_EMBED_ENDPOINT/FDI_EMBED_KEY and/or FDI_MILVUS_URI)"
# after
"(set FDI_MILVUS_URI -- embeddings run locally via BGE and need no endpoint/key)"
```
The `missing = [...]`/`if missing:` structure is unchanged (§2.5) — `embedder` can still theoretically
appear in `missing` if something other than normal `lifespan` startup leaves `app.state.embedder` unset
(e.g. a test that constructs the app without running lifespan), so the check stays generically correct;
only the message's stale env-var names are wrong today and are fixed.

### 3.6 Dependency + marker

`backend/pyproject.toml` gains `fastembed` in `dependencies` (§2.8) and a `bge` marker in
`[tool.pytest.ini_options] markers`, registered alongside `azure`/`neo4j`/`milvus`.

### 3.7 `POST /documents/{id}/embedding` trigger route — unchanged

The route added at SP-13 §3.6 dispatches the `"embedding"` stage by name; it has no knowledge of which
`EmbeddingClient` implementation the dispatched worker will wire in. Zero change.

## 4. Error handling

- A real BGE model load/download failure on first `embed()` (no network on a cold cache, corrupted cache,
  unsupported platform/ONNX runtime issue) → `fastembed`/`onnxruntime`/`huggingface_hub` raises; propagates
  out of `embed()` uncaught; `run_stage`'s existing fail-path records `stage_run(failed, error=...)` — same
  "SDK raises, run_stage records" shape SP-13 §4 established for Azure OpenAI errors, no new handling code.
- **The "embedder unconfigured" failure mode SP-13 §4 described (`ctx.services["embedder"]` raising
  `KeyError` because `from_settings` returned `None` and the wiring omitted the key) is RETIRED as a
  production possibility** — `BgeEmbeddingClient.from_settings` never returns `None`, so
  `services["embedder"]` is always populated by both composition roots (§3.4). The `KeyError` path remains
  reachable only in a test that deliberately builds `services={}` without an embedder (a valid, still-used
  pattern for isolating stage behavior in isolation, not a config gap).
- An AST artifact with zero sections → unchanged from SP-13 §4: `EmbeddingStage` returns
  `{schema_version:1, model: client.model, dim: 0, chunks: []}` with **no embedding calls and, per §2.4, no
  model load either** — reading `client.model` never touches `_get_impl()`.
- `embedding` dispatched before `ast` has run → unchanged, SP-2's existing derived-stage machinery.
- A real `EmbeddingClient` returning a different vector count than texts sent → unchanged,
  `zip(..., strict=True)` raises `ValueError`.
- `POST /search` when `vector_store` is unconfigured (the normal state of a `core`-only deployment, Milvus
  is opt-in via the `vector` Compose profile) but `embedder` is (now always) present → `missing =
  ["vector_store"]`, 503 with the corrected message (§3.5); `embedder.embed(...)` is never reached because
  the `if missing:` check short-circuits first — confirmed by reading `routes.py`'s `search` handler, not
  assumed: the default test suite therefore never triggers a real BGE model load via this route either,
  because `FDI_MILVUS_URI` is unset by default in every test environment (Settings default `None`).

## 5. Testing

- **Unit** (no Docker, no download): `BgeEmbeddingClient` (fastembed's `TextEmbedding` monkeypatched, no
  real load) — construction does NOT call `TextEmbedding(...)` (lazy, asserted via a call-recording fake);
  the FIRST `embed()` call constructs it exactly once; a SECOND `embed()` call reuses the cached instance
  (constructed only once total — the "lazy AND cached" contract in one assertion, same call-counting
  discipline `FakeEmbeddingClient`/`FakeSemanticClient` established); `embed()` maps each fake vector's
  `.tolist()` into the returned list; `.model` exposes the configured model name; `from_settings` returns a
  non-`None` instance whose `.model == settings.embed_model` WITHOUT constructing `TextEmbedding` at all
  (asserted by monkeypatching `TextEmbedding` to raise if called, then calling `from_settings` and
  confirming no raise). Config defaults: `embed_model == "BAAI/bge-small-en-v1.5"`, `embed_dim == 384`.
- **Integration, UNCHANGED and re-run as evidence, not re-written**:
  `tests/integration/test_embedding_stage.py` (SP-13) is not touched by this SP and must still pass exactly
  as written — it drives `EmbeddingStage` with `FakeEmbeddingClient(dim=8)` and asserts
  `body["model"] == "fake-embedding"`, `body["dim"] == 8`, content-addressed caching (`fake_embedder.calls
  == 1` after a second run). A green, unmodified run of this file is the proof that `EmbeddingStage`'s
  artifact schema and caching behavior survived the backend swap untouched — the strongest kind of
  regression evidence available, because the test that would catch a regression was never edited to match
  one.
- **Integration, `test_api.py`**: `test_trigger_embedding_on_a_document`/`_unknown_document_is_404` (SP-13)
  unchanged. `test_search_503_when_unconfigured` renamed `test_search_503_when_vector_store_unconfigured`
  and narrowed to assert only `"vector_store"` (not `"embedder"`) appears in the 503 detail (§2.5).
  `test_search_returns_the_best_match_first` unchanged (uses `dependency_overrides`, never touches
  `app.state.embedder`).
- **Opt-in real-BGE smoke test** (`@pytest.mark.bge`, `backend/tests/bge/test_real_bge_embedding.py`):
  unlike `azure`/`neo4j`/`milvus` (skip on ABSENT config — a `None` endpoint/URI/key), BGE has no such
  "unconfigured" state to detect (§2.5/§2.8), so this test skips on an explicit, test-only opt-in flag
  (`FDI_RUN_BGE_TEST=1`, checked via `os.getenv`, NOT a Settings field, never reaches the app) instead of a
  config-presence check. Builds a real `BgeEmbeddingClient.from_settings(get_settings())`, embeds one canned
  string against the real model, asserts the returned vector's length equals `settings.embed_dim` (384).
  Also excluded from the default suite via `-m "not ... and not bge"`, same belt-and-suspenders shape as
  the other three opt-in markers.
- **Deleted**: `backend/tests/unit/test_azure_embedding_client.py` and
  `backend/tests/azure/test_real_embedding.py` (both test the deleted `AzureOpenAIEmbeddingClient`).
  `backend/tests/azure/` itself is NOT deleted — `test_real_ocr.py`/`test_real_semantic.py` still live there
  for Document Intelligence/GPT, both unaffected by this SP.
- Config consistency: `FDI_EMBED_MODEL`/`FDI_EMBED_DIM` present in Settings + compose + `.env.example`, and
  — the negative half, equally load-bearing per the INERT-CONFIG discipline — `FDI_EMBED_ENDPOINT`,
  `FDI_EMBED_KEY`, `FDI_EMBED_DEPLOYMENT`, `FDI_EMBED_API_VERSION` present in NONE of the three (a stray
  survivor in compose/`.env.example` alone, with the Settings field removed, is a boot-time crash via
  `_reject_unknown_fdi_env_vars`, not a lint warning — the exact failure mode that validator exists to
  catch, now self-inflicted if this task is done out of lockstep).

## 6. Explicitly deferred (YAGNI)

- **GPU/ONNX-provider tuning** (CUDA/OpenVINO execution providers for `fastembed`) — CPU-only for the thin
  slice, matching the project's no-GPU Compose deployment posture (§2.3); a real candidate only once
  corpus-scale throughput (the 5000-doc run) makes CPU embedding latency a measured bottleneck.
- **Alternate BGE sizes** (`bge-base-en-v1.5`, `bge-large-en-v1.5` — higher-dim, slower, arguably
  higher-quality) — a genuine, `EmbeddingClient`-swap-free (just change `FDI_EMBED_MODEL` +
  `FDI_EMBED_DIM`) SP-3-measurable candidate once retrieval-quality ground truth exists, same deferral
  shape SP-13 §6 already carried for embedding-model variants generally.
- **Sharing/caching the loaded model ACROSS processes.** `fastembed`'s own on-disk cache means the ~130 MB
  DOWNLOAD happens once per machine (subsequent processes load from disk), but the in-memory ONNX inference
  session is still rebuilt once per process on that process's first `embed()` call (each Celery prefork
  worker child, the API process). A real perf question once many worker processes are running concurrently
  at corpus scale, not the thin slice.
- **Batch/async embedding at corpus scale** — same deferral SP-13 §6 already carried, unchanged by this SP.
- **Re-introducing a remote/Azure (or any other externally-hosted) embedding option.** The seam
  (`EmbeddingClient` protocol) still supports it — a new `platform` class implementing `embed`/`model` — but
  `AzureOpenAIEmbeddingClient` is DELETED, not kept dormant; reintroducing it means writing it again against
  whatever real need motivates it, not restoring dead code.
- **Migrating an existing 1536-dim Milvus `fdi_sections` collection** left over from a prior real SP-13/SP-14
  Azure-backed run. `MilvusVectorStore._ensure_collection` only creates when the collection is ABSENT and
  never migrates an existing one's dimension (SP-14, unchanged) — an operator with such a collection must
  drop and recreate it manually (a runbook step, documented at Task 6 of the companion plan, not code this
  SP writes).

## 7. Done criteria

1. `fastembed` is a `backend/pyproject.toml` dependency; a `bge` pytest marker is registered alongside
   `azure`/`neo4j`/`milvus`.
2. `fdi.platform.embeddings.BgeEmbeddingClient` exists and REPLACES `AzureOpenAIEmbeddingClient` (deleted,
   not kept): lazy (`TextEmbedding` constructed on first `embed()` only, cached on the instance
   thereafter), public `model: str`, `embed(texts) -> list[list[float]]` via `.tolist()`; `from_settings`
   returns a `BgeEmbeddingClient` unconditionally (never `None`).
3. `EmbeddingClient` protocol / `EmbeddingStage` / `FakeEmbeddingClient` are byte-identical to SP-13 —
   proven by `tests/integration/test_embedding_stage.py` passing UNMODIFIED.
4. Settings has exactly `embed_model` (default `"BAAI/bge-small-en-v1.5"`) and `embed_dim` (default `384`)
   in the `embed_*` family; `embed_endpoint`/`embed_key`/`embed_deployment`/`embed_api_version` are absent
   from Settings AND `docker/compose.yml`'s `x-backend-env` AND `.env.example`, all in lockstep; the stack
   boots with these two fields and no others.
5. Both composition roots (`fdi/workers/tasks.py`'s `_run_stage_async`, `fdi/api/app.py`'s `lifespan`) wire
   `BgeEmbeddingClient.from_settings(...)` in place of the deleted Azure class; `POST
   /documents/{id}/embedding` continues to dispatch the `embedding` stage unchanged.
6. `POST /search`'s 503 "not configured" message no longer references `FDI_EMBED_ENDPOINT`/`FDI_EMBED_KEY`;
   `test_search_503_when_vector_store_unconfigured` (renamed/narrowed from
   `test_search_503_when_unconfigured`) asserts `embedder` is never listed as missing.
7. `backend/tests/unit/test_azure_embedding_client.py` and `backend/tests/azure/test_real_embedding.py` are
   deleted; `backend/tests/unit/test_bge_embedding_client.py` and the opt-in
   `backend/tests/bge/test_real_bge_embedding.py` exist.
8. Running `embedding` over a document with an `ast` artifact (via `FakeEmbeddingClient`, as in SP-13's
   unmodified integration test) still produces `{schema_version:1, model, dim, chunks:[...]}` at the same
   content-addressed key, with the same "second run is a cache hit, embedder not called twice" behavior.
9. All four static gates green (`ruff`, `mypy --strict`, `lint-imports` — `core`/`pipeline` untouched, only
   `platform`, config, the two composition roots, `routes.py`'s message string, tests, and docs change);
   full default suite passes with NO real model download/load (fake only) and NO live Azure OpenAI call
   anywhere in the embedding path (the Azure embedding surface no longer exists in this codebase).
