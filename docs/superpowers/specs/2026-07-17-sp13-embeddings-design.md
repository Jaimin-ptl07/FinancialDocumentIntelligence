# SP-13 — Embeddings via Azure OpenAI

- **Status:** Implemented 2026-07-18. Companion plan:
  `docs/superpowers/plans/2026-07-17-sp13-embeddings.md`. **Embedding backend swapped to BGE-small
  (local, fastembed) in SP-13b** (`docs/superpowers/specs/2026-07-18-sp13b-bge-embeddings-design.md`) —
  `AzureOpenAIEmbeddingClient` described below no longer exists in the codebase; the `EmbeddingClient`
  protocol, `EmbeddingStage`, and `FakeEmbeddingClient` this spec designed are unchanged and still exactly
  as described.
- **Depends on:** SP-2 (stage framework — `StageContext.services`, content-addressed derived-stage
  caching), SP-8 (Document AST — provides `{schema_version, title, sections:[{heading, paragraphs}]}`),
  SP-10 (the `openai` SDK dependency is already in `backend/pyproject.toml`; the injected-client /
  `platform`-does-I/O / opt-in-azure-test pattern this spec mirrors).
- **Decision carried from roadmap memory (`external-services-azure`, `sp7-structural-research`):**
  OCR = Azure Document Intelligence (SP-5); LLM = Azure OpenAI (SP-10 semantic, SP-11 ontology). SP-13
  adds a THIRD Azure OpenAI surface — **embeddings**, via `openai.AzureOpenAI(...).embeddings.create(...)`
  — distinct from the chat-completions surface SP-10/SP-11 use. Roadmap ordering
  (`docs/architecture/ingestion-roadmap.md` §5): **SP-13 embeddings → SP-14 indexes**. SP-13 only
  *produces* vectors as a content-addressed artifact; the Milvus vector **store**, upsert, and
  similarity retrieval are SP-14's job. This mirrors how SP-9 shipped a JSON graph artifact and left
  Neo4j *loading* to a later Compose-profile step, and how SP-10 shipped a classifier for SP-3 to later
  measure — SP-13 ships the embedder, SP-14 builds what reads it.

## 1. Purpose

Produce one dense vector per AST section by running **Azure OpenAI embeddings** as a pipeline **stage**
over the SP-8 AST artifact. This is the first *representation* layer (as opposed to SP-10/SP-11's
*semantic-label* layers): everything before it (OCR, normalize, AST, semantic, ontology, structure,
quality, graph, provenance) produces symbolic or structural facts; SP-13 produces the numeric
representation that SP-14's vector search will index and query.

Thin vertical slice: the embedding stage + a mockable Azure OpenAI embeddings client + config wiring,
proven with a **fake embedding client** in the test suite (no live Azure, no cost, no token spend) plus
an **opt-in real Azure OpenAI embeddings smoke test**. The Milvus vector store, chunking finer than
per-section, and re-embedding policy are measurement/design work for later sub-projects, deliberately
deferred (§6).

## 2. Scope decisions (made autonomously, mirroring SP-10's grounded pattern)

1. **One chunk per AST section, not per-paragraph or sliding-window.** The AST already segments a
   document into `{heading, paragraphs}` units; embedding at that granularity needs no new chunking
   logic and gives SP-14 something immediately indexable. Finer chunking (per-paragraph, sliding-window
   token budgets) is a real, SP-3-measurable candidate — a different `chunk()` function is just another
   thing the harness can score once retrieval-quality ground truth exists — but building it now is
   premature: SP-13 ships the coarsest defensible unit and lets SP-3 justify anything finer. Deferred (§6).
2. **The embedding model is an injected `EmbeddingClient` protocol**, so the suite runs against a
   `FakeEmbeddingClient` (deterministic hash-derived vectors, no network) and a real
   `AzureOpenAIEmbeddingClient` (SDK) is used in production plus an opt-in smoke test. Same SP-5/SP-10
   lesson: the embedding model is a swappable candidate (a smaller/cheaper deployment, or eventually a
   non-Azure embedding model) — just another `EmbeddingClient`.
3. **SDK = `openai` (official Python SDK), `openai.AzureOpenAI` sync client**, `.embeddings.create(...)`.
   Already a project dependency (`backend/pyproject.toml`, added at SP-10 for chat completions) — SP-13
   needs no new dependency, just a new call surface on the same SDK client class.
4. **`EmbeddingClient` protocol carries a public `model: str` attribute alongside `embed()`.** The
   literal shape handed down for this stage is `embed(texts: list[str]) -> list[list[float]]`, but the
   artifact schema (§3.4) also needs to record *which* model/deployment produced the vectors — the same
   fact `AzureOpenAIClient` (SP-10 §3.4) stores privately as `self._model` without ever surfacing it,
   because SP-10's artifact schema never needed it. SP-13's does (`"model": <str>`), so `model` is
   promoted from a private, unused field to a public, protocol-visible one:
   `AzureOpenAIEmbeddingClient.model` is the deployment name; `FakeEmbeddingClient.model` is a canned
   string (`"fake-embedding"`) so integration tests can assert the artifact's `model` field without
   touching Azure. This is the one place SP-13's protocol is a strict superset of the literal
   `embed(texts) -> list[list[float]]` brief — resolved explicitly here rather than smuggled in silently.
5. **`EmbeddingStage` is a DERIVED stage** (`name="embedding"`, `version=1`, `deps=("ast",)`) — the same
   shape as SP-10's `SemanticStage`, not a root stage like SP-5's `OcrStage`. It reads the AST artifact
   via `ctx.read("ast")`, builds one text chunk per section, embeds all of them in a single
   `ctx.services["embedder"].embed([...])` call, and returns a new artifact.
6. **Determinism exception (Global Constraint 6), identical shape to SP-10:** embedding output is NOT
   guaranteed byte-stable across API calls (model updates, provider-side non-determinism even at
   effectively-zero temperature for some embedding backends). Because `EmbeddingStage` is a **derived**
   stage, SP-2's existing framework ALREADY content-addresses it on
   `derived_input_hash([ast_run.output_hash])` + `EmbeddingStage.version` — no root-stage special-casing,
   no framework change (§3.1). The framework caches the FIRST successful embedding at that key and does
   not re-embed; a deliberate reprocess or a `version` bump (bump when the chunking unit, embedding
   deployment, or `FDI_EMBED_DIM` changes) regenerates. "Run once, cache" is correct for the same reason
   as SP-10: re-embedding is a paid, non-deterministic external call.
7. **The stored artifact is the vectors plus minimal provenance, not the raw Azure response.** A stable
   JSON `{schema_version:1, model: <str>, dim: <int>, chunks:[{chunk_id, heading, vector}]}` — one chunk
   per AST section, in order, `chunk_id` = `"s{i}"` (0-based index into the AST's `sections` list, stable
   because AST section order is stable). The raw embeddings response (usage, object id, …) is NOT
   persisted — mirrors SP-10's "normalized subset, not the SDK object" call. `dim` is read off the actual
   returned vectors (`len(vectors[0])`, or `0` if there were zero chunks to embed), not merely copied from
   config — config's `FDI_EMBED_DIM` is a *request*, this is the *observed* fact, and the two should
   agree only because the request was honored (see #8).
8. **`FDI_EMBED_DIM` is passed to the SDK call as `dimensions=`, not just recorded as a config comment.**
   OpenAI/Azure OpenAI's `text-embedding-3-small`/`text-embedding-3-large` models (unlike the older
   `text-embedding-ada-002`) accept a `dimensions` parameter on `embeddings.create(...)` to truncate the
   native embedding to a requested, smaller dimensionality — exactly what `FDI_EMBED_DIM` is for. This is
   why the default deployment (`FDI_EMBED_DEPLOYMENT = "text-embedding-3-small"`) is a v3 model: an
   operator who points `FDI_EMBED_DEPLOYMENT` at `ada-002` and leaves `FDI_EMBED_DIM` at its default gets
   an Azure OpenAI 400 for an unsupported parameter — an explicit, documented operator responsibility
   (`.env.example` comment), not defended against in code. Same "operator-driven config, no silent
   correction" discipline as SP-10's `gpt_deployment`/`gpt_model` pairing.
9. **Config, not secrets in repo:** `FDI_EMBED_ENDPOINT` / `FDI_EMBED_KEY` are `str | None = None` (the
   stack boots without embeddings configured; `EmbeddingStage` raises a clear error if unconfigured, same
   as SP-5's OCR and SP-10's semantic). No sibling-repo `.env` mapping exists yet for embeddings (unlike
   `FDI_AZURE_DI_*`/`FDI_GPT_*`, which have real upstream names in `D:\FinancialDocIntelligence\.env`) —
   an operator provisioning a real `text-embedding-3-small` deployment sets these directly in this repo's
   gitignored `.env` under the `FDI_EMBED_*` names below; there is no rename-on-the-way-in step because
   there is no pre-existing bare-named source to rename from.
10. **No new Compose profile or service.** `docker/compose.yml` already reserves a `vector` profile
    (`etcd` + `milvus`, both `profiles: [vector]`, not started under `core`) for the future Milvus store —
    per the roadmap's "vector at SP-13" deployment note. That note describes when the *profile* is first
    exercised end-to-end (once SP-14 exists and a document has been embedded and indexed), not a claim
    that SP-13 itself starts Milvus or etcd. `EmbeddingStage` and `AzureOpenAIEmbeddingClient` run under
    the existing `core` profile (`backend-worker`/`backend-api`) exactly like every stage before them;
    `FDI_EMBED_*` is added to `x-backend-env`, which those `core`-profile services already consume. SP-14
    is the sub-project that actually brings up `vector` and writes to it.

## 3. Architecture

### 3.1 No stage-framework change needed (same as SP-10 §3.1)

`EmbeddingStage` needs nothing beyond what SP-5 added (`StageContext.services`) and what SP-2 already
does for a **derived** stage: `ctx.read("ast")` (general-purpose) and the existing derived-stage
`input_hash` computation (`derived_input_hash([facts["ast"].output_hash])` in `run_stage`, unchanged).
Zero changes to `fdi/pipeline/stage.py` or `fdi/pipeline/run.py`.

### 3.2 `EmbeddingClient` (protocol + two implementations, `fdi.pipeline.embeddings`)

```python
# fdi.pipeline.embeddings
"""The EmbeddingClient seam: an `embed(texts) -> list[list[float]]` protocol.

`AzureOpenAIEmbeddingClient` (platform, does I/O) structurally satisfies this
without importing it -- `platform` may not import `pipeline`. `FakeEmbeddingClient`
lives here for tests: no live Azure OpenAI call anywhere in the suite.
"""

import hashlib
from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingClient(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FakeEmbeddingClient:
    """Deterministic, network-free stand-in. Derives each vector from a sha256
    digest of the text so the SAME text always produces the SAME vector within
    a test run (and across runs), without ever calling out to Azure."""

    def __init__(self, dim: int = 8, model: str = "fake-embedding") -> None:
        self.model = model
        self.dim = dim
        self.calls = 0
        self.seen: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.seen.extend(texts)
        return [self._vector(t) for t in texts]

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [b / 255.0 for b in digest[: self.dim]]
```
`calls`/`seen` mirror `FakeSemanticClient`'s call-recording discipline (SP-10 §3.3) so the same
"second run is a cache hit, the fake was NOT called twice" test shape applies unchanged.

### 3.3 `AzureOpenAIEmbeddingClient` (platform, does I/O)

```python
# fdi.platform.embeddings
"""AzureOpenAIEmbeddingClient -- EmbeddingClient backed by Azure OpenAI embeddings.

Lives in `platform` (does I/O), not `pipeline`: the `EmbeddingClient` protocol,
`EmbeddingStage`, and `FakeEmbeddingClient` live in `fdi.pipeline`. This module
structurally satisfies that protocol without importing it -- `platform` may not
import `pipeline` per the layered architecture contract.

`AzureOpenAI` is imported at module level (rather than inside `embed`) so tests
can monkeypatch `fdi.platform.embeddings.AzureOpenAI` with a fake -- no live
Azure OpenAI call in the suite.
"""

from openai import AzureOpenAI

from fdi.platform.config import Settings


class AzureOpenAIEmbeddingClient:
    """EmbeddingClient backed by Azure OpenAI. Lives in platform (does I/O)."""

    def __init__(
        self, *, endpoint: str, key: str, deployment: str, api_version: str, dim: int
    ) -> None:
        self._sdk = AzureOpenAI(azure_endpoint=endpoint, api_key=key, api_version=api_version)
        self.model = deployment
        self._dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = self._sdk.embeddings.create(model=self.model, input=texts, dimensions=self._dim)
        return [d.embedding for d in resp.data]

    @staticmethod
    def from_settings(settings: "Settings") -> "AzureOpenAIEmbeddingClient | None":
        if not settings.embed_endpoint or not settings.embed_key:
            return None
        return AzureOpenAIEmbeddingClient(
            endpoint=settings.embed_endpoint,
            key=settings.embed_key,
            deployment=settings.embed_deployment,
            api_version=settings.embed_api_version,
            dim=settings.embed_dim,
        )
```
`dimensions=self._dim` is honored by `text-embedding-3-*` deployments (§2.8); it is passed
unconditionally, matching the "operator-driven config" discipline rather than branching on deployment
name in code.

### 3.4 `EmbeddingStage` (`fdi.pipeline.stages.embedding`)

```python
# fdi.pipeline.stages.embedding
"""EmbeddingStage: a DERIVED stage that embeds one chunk per AST section via an
injected EmbeddingClient. Auto-chains off AstStage via `deps=("ast",)`.

Content-addressed on `derived_input_hash([ast.output_hash])` +
`EmbeddingStage.version` -- deliberate, since the embedding call is
non-deterministic: a second run against the same `ast` artifact is a store
cache hit that does NOT call the client again (proven in
tests/integration/test_embedding_stage.py with a call-counting fake)."""

from collections.abc import Mapping
from typing import cast

from fdi.pipeline.embeddings import EmbeddingClient
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class EmbeddingStage:
    name = "embedding"
    version = 1
    deps: tuple[str, ...] = ("ast",)

    async def run(self, ctx: StageContext) -> object:
        ast = cast(Mapping[str, object], await ctx.read("ast"))
        client = cast(EmbeddingClient, ctx.services["embedder"])

        chunk_ids: list[str] = []
        headings: list[object] = []
        texts: list[str] = []
        sections_raw = ast.get("sections")
        if isinstance(sections_raw, list):
            for i, section in enumerate(sections_raw):
                if not isinstance(section, dict):
                    continue
                heading_raw = section.get("heading")
                heading = heading_raw if isinstance(heading_raw, str) else ""
                paragraphs_raw = section.get("paragraphs")
                paragraphs = (
                    [p for p in paragraphs_raw if isinstance(p, str)]
                    if isinstance(paragraphs_raw, list)
                    else []
                )
                chunk_ids.append(f"s{i}")
                headings.append(heading_raw)
                texts.append(heading + "\n" + "\n".join(paragraphs))

        vectors = client.embed(texts) if texts else []
        chunks = [
            {"chunk_id": cid, "heading": heading, "vector": vector}
            for cid, heading, vector in zip(chunk_ids, headings, vectors, strict=True)
        ]
        return {
            "schema_version": 1,
            "model": client.model,
            "dim": len(vectors[0]) if vectors else 0,
            "chunks": chunks,
        }


REGISTRY.register(EmbeddingStage())
```
Self-registers into `REGISTRY` (`fdi.pipeline.stages.__init__` gains `from fdi.pipeline.stages import
embedding as embedding  # noqa: F401`, alphabetically between `ast` and `graph`). `client.embed(texts)`
is called only when there is at least one chunk — an AST with zero sections costs zero embedding calls,
same "no I/O for a trivially empty case" rule as SP-10 §4. `zip(..., strict=True)` hard-fails (not
silently truncates) if a real `EmbeddingClient` ever returns a different number of vectors than texts
sent — a contract violation that must be loud, not swallowed.

### 3.5 Config (Settings + x-backend-env + .env.example)

New `FDI_` fields: `FDI_EMBED_ENDPOINT: str | None = None`, `FDI_EMBED_KEY: str | None = None`,
`FDI_EMBED_DEPLOYMENT: str = "text-embedding-3-small"`, `FDI_EMBED_API_VERSION: str = "2024-10-21"`
(same current stable AOAI API version SP-10 already pins for chat completions; it also covers the
embeddings endpoint), `FDI_EMBED_DIM: int = 1536` (the native output size of `text-embedding-3-small`;
also the value passed as `dimensions=` per §2.8 — an operator wanting a smaller index shrinks this, not
the model). All five added to Settings AND compose `x-backend-env` AND `.env.example` (the inert-config
lesson, carried from SP-5/SP-10). Endpoint/key `str | None = None` so empty→None boots cleanly.

### 3.6 Trigger

`POST /documents/{id}/embedding` → 202: dispatch `run_stage_task(doc_id, "embedding", correlation_id)`
(reuses `StageDispatcher`, mirrors SP-10's `POST /documents/{id}/semantic`). 404 if the document is
unknown. The generic `POST /docs/{doc_id}/reprocess?from=embedding` (SP-2/18) already covers ad-hoc
re-dispatch; the dedicated route exists for parity/discoverability, same rationale as SP-5/SP-10.

### 3.7 Dependency

None. `openai` is already in `backend/pyproject.toml` (added at SP-10); `AzureOpenAIEmbeddingClient` uses
the same `AzureOpenAI` class SP-10's `AzureOpenAIClient` uses, on a different attribute (`.embeddings`
instead of `.chat.completions`).

## 4. Error handling

- Azure OpenAI (embeddings) unconfigured (`endpoint`/`key` None) when `embedding` runs →
  `ctx.services["embedder"]` raises `KeyError` (the wiring omits the key entirely when
  `from_settings` returns `None` — see Task 6); `run_stage`'s fail-path records
  `stage_run(failed, error=...)`. No partial artifact.
- Azure OpenAI API error / timeout / a `dimensions` value the deployment rejects → the SDK raises;
  `run_stage` records the failure; retryable per Celery (same as SP-5/SP-10).
- An AST artifact with zero sections → `EmbeddingStage` returns
  `{schema_version:1, model: client.model, dim: 0, chunks: []}` with **no embedding calls at all** — a
  trivially valid, cheap result, not an error (mirrors SP-10 §4's zero-sections case; `client.model` is
  still readable without I/O).
- `embedding` dispatched before `ast` has run → `run_stage`'s existing derived-stage machinery already
  requires the dep's `stage_run` to exist (`facts["ast"].output_hash` — SP-2 behavior, unchanged).
- A real `EmbeddingClient` returning a different vector count than texts sent → `zip(..., strict=True)`
  raises `ValueError` — a hard, recorded failure (§3.4), not a silently-misaligned `chunks` list.

## 5. Testing

- **Unit** (no Docker, no Azure): `FakeEmbeddingClient` returns the SAME vector for the SAME text
  (determinism) and DIFFERENT vectors for different text; records `calls`/`seen`; `AzureOpenAIEmbeddingClient`
  (SDK monkeypatched, no live call) builds the `embeddings.create` call with the right `model=`/`input=`/
  `dimensions=` and parses `resp.data[*].embedding`; `from_settings` returns `None` when endpoint/key
  unset; config defaults (`embed_endpoint is None`, `embed_deployment == "text-embedding-3-small"`,
  `embed_dim == 1536`, …) present in Settings.
- **Integration** (testcontainers, `FakeEmbeddingClient` — NO live Azure OpenAI): run `ocr`→`normalize`→
  `ast` then `embedding` via `run_stage` with `services={"embedder": FakeEmbeddingClient(dim=8)}`; assert
  a `done` stage_run + a stored artifact at the content-addressed key + `chunks[].vector` has length 8 +
  `chunks[].chunk_id` == `"s0"`, `"s1"`, … in AST section order; a second `embedding` run is a cache hit
  (embedder NOT called twice — proven by a call counter on the fake, same pattern as SP-10's semantic
  cache-hit test).
- **Opt-in real-Azure-OpenAI-embeddings smoke test** (`@pytest.mark.azure`, skipped unless
  `FDI_EMBED_ENDPOINT`/`FDI_EMBED_KEY` set): constructs the real `AzureOpenAIEmbeddingClient`, embeds one
  canned text against a real deployment, asserts the returned vector's length matches `FDI_EMBED_DIM`.
  NEVER in CI (no creds, no cost). Reuses the existing `azure` pytest marker (already broadened at SP-10
  to cover "Document Intelligence and/or OpenAI" — the embeddings endpoint is the same OpenAI surface, no
  further broadening needed) so the fully-offline command (`-m "not integration and not e2e and not
  azure"`) still excludes it without a new flag.
- **API** (integration): `POST /documents/{id}/embedding` → 202; unknown doc id → 404.
- Config consistency: `FDI_EMBED_*` present in Settings + compose + `.env.example`.

## 6. Explicitly deferred (YAGNI)

- **The Milvus vector STORE + upsert + similarity retrieval** → SP-14 (Retrieval Indexes). SP-13 ships
  the artifact SP-14 will load into Milvus; SP-13 does not talk to Milvus, does not bring up the `vector`
  Compose profile, and does not implement any nearest-neighbor search.
- **Chunking finer than per-section** (per-paragraph, sliding-window with a token budget) — a real,
  SP-3-measurable candidate once retrieval-quality ground truth exists to score chunk granularity against
  (same deferral shape as SP-10's prompt-variant measurement, §2.1 above).
- **Embedding tables/figures.** The AST (SP-8) carries only `title`/`sections`/`paragraphs` — no table or
  figure content flows into `EmbeddingStage` today. Table/figure embedding waits on those artifacts
  gaining a text-extractable representation upstream.
- **Re-embedding policy** (when to bump `EmbeddingStage.version` automatically, e.g. on a deployment
  upgrade) — an operator/runbook concern for when a real deployment is swapped, not code today. Same
  deferral shape as SP-10's "bump when the prompt/deployment/label-set changes" manual-version-bump note.
- **Batch/async embedding for corpus-scale** (parallelizing across many documents' worth of chunks,
  request batching beyond one `embed()` call per document) — deferred to when the 5000-doc run is real
  (SP-13/14 per the roadmap's deployment posture), same deferral SP-10 carries for classification.
- **Confidence/quality scoring of embeddings** (e.g. detecting a degenerate all-zero or NaN vector) — add
  if real documents show the need; not built for the thin slice.

## 7. Done criteria

1. `EmbeddingClient` protocol (`embed(texts: list[str]) -> list[list[float]]` + public `model: str`) +
   `FakeEmbeddingClient` (`fdi.pipeline.embeddings`) exist; the fake is deterministic (same text → same
   vector) and records every call.
2. `AzureOpenAIEmbeddingClient` (`fdi.platform.embeddings`) wraps `openai.AzureOpenAI(...).embeddings.create(...)`,
   passes `dimensions=` from config, and parses `resp.data[*].embedding`; `from_settings` returns `None`
   unless endpoint+key are set.
3. `EmbeddingStage` (`name="embedding"`, `version=1`, `deps=("ast",)`) is registered; `run(ctx)` reads the
   AST artifact, builds one chunk per section (`chunk_id="s{i}"`), embeds all chunks in one call via the
   injected `EmbeddingClient`, and returns `{schema_version:1, model, dim, chunks:[{chunk_id, heading, vector}]}`.
4. Running `embedding` over a document with an `ast` artifact stores the result content-addressed on
   `derived_input_hash([ast.output_hash])` + `EmbeddingStage.version` and records a `done` stage_run; a
   second run is a store cache hit that does NOT call the embedder again.
5. `FDI_EMBED_*` config is wired (Settings + compose + `.env.example`); the stack boots with Azure OpenAI
   embeddings unconfigured (endpoint/key None), and `embedding` then fails with a clear, recorded error.
6. `POST /documents/{id}/embedding` dispatches the embedding stage (202); unknown id → 404.
7. The opt-in real-Azure-OpenAI-embeddings smoke test exists and skips cleanly when unconfigured.
8. All four static gates green; `core` stays pure (SP-13 adds nothing to `fdi.core`); full suite passes
   with NO live Azure OpenAI call (fake client only).
