# RT-2 Semantic (Vector) Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox
> (`- [ ]`) syntax for tracking.

**Goal:** Build `vector_search(query, filters, k)` — embed the query (BGE), search the injected
`VectorStore`, filter by `section_type` (Milvus metadata) and/or `doc_key`/`version` (a batched
Postgres join), attach text + citation via RT-1's `resolve_chunks`, and expose it as `POST
/retrieve/vector` returning `{doc_id, doc_key, version, chunk_id, heading, page, section_type, text,
score}` per result. `POST /search` is left unchanged (design spec §2 decision 5 — a fork flagged for
human confirmation, not silently resolved).

**Architecture:** A pure `fdi.core.retrieval.build_vector_results` (appended to RT-1's existing module)
filters/joins/truncates an already-fetched hit list. `fdi.pipeline.retrieval.vector_search` (appended
to RT-1's existing module) does the I/O: embed, over-fetch `k*5` only when a filter is set, one batched
`DocumentRepository.get_many` call, one `resolve_chunks` call, then delegates assembly to
`build_vector_results`. `DocumentRepository` gains `get_many`. A new `POST /retrieve/vector` route is a
thin wrapper with the same 503-when-unconfigured gate `/search` already uses.

**Tech Stack:** Python 3.13, `uv`, SQLAlchemy async, pytest + testcontainers. Builds directly on RT-1's
`chunks` artifact / `resolve_chunks` and SP-14's `VectorStore`/`EmbeddingClient` seams — no new external
dependency.

## Global Constraints

- Layered contract `api | workers → pipeline → platform → core` (exhaustive), CI-enforced; stay "1
  kept, 0 broken". `fdi.core.retrieval` stays PURE: no I/O/config/clock/RNG, no import of
  `fdi.platform.*`/`fdi.pipeline.*` — `build_vector_results` takes plain `Mapping`s (hits, resolved
  chunk records, and a `{doc_id: {doc_key, version}}` dict), never an ORM `Document`.
- `_OVERFETCH_FACTOR = 5` is a private constant in `fdi.pipeline.retrieval` — no new `FDI_` setting.
- `build_vector_results` never re-sorts: output order is a strict sub-sequence of the input hit order
  (best-match-first, from `VectorStore.search`).
- `vector_search`'s only I/O calls are: one `embedder.embed([query])`, one `vector_store.search(...)`,
  one `DocumentRepository(session).get_many(...)`, one `resolve_chunks(...)` — never per-hit queries.
- `POST /search` and its existing tests (`test_search_503_when_vector_store_unconfigured`,
  `test_search_returns_the_best_match_first`) are NOT modified by this plan. If a task's diff would
  touch `routes.py`'s `/search` handler, stop and re-read design spec §2 decision 5 — that is out of
  scope here.
- Per-task gates before every commit: `uv run --directory backend ruff check .` ·
  `uv run --directory backend mypy --strict src` ·
  `PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter` · the task's pytest.
  Line length ≤ 100.
- TDD: failing test → run (see fail) → minimal impl → run (see pass) → commit.

---

### Task 1: `DocumentRepository.get_many` — batched doc lookup by id

**Files:**
- Modify: `backend/src/fdi/platform/db/repository.py`
- Test: `backend/tests/integration/test_document_repository.py` (extend the existing file)

**Interfaces:**
- Produces: `DocumentRepository.get_many(self, ids: list[str]) -> dict[str, Document]`.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/integration/test_document_repository.py` (keep the existing
`test_create_get_and_version` test and its imports untouched; add this new test below it):

```python
async def test_get_many_returns_known_ids_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    key = f"key-{uuid.uuid4()}"
    async with session_factory() as s:
        repo = DocumentRepository(s)
        d1 = await repo.create(
            id=str(uuid.uuid4()),
            checksum=str(uuid.uuid4()),
            filename="a.pdf",
            byte_size=10,
            content_type="application/pdf",
            doc_key=key,
            version=1,
        )
        d2 = await repo.create(
            id=str(uuid.uuid4()),
            checksum=str(uuid.uuid4()),
            filename="b.pdf",
            byte_size=11,
            content_type="application/pdf",
            doc_key=key,
            version=2,
        )
        await s.commit()

    async with session_factory() as s:
        repo = DocumentRepository(s)
        found = await repo.get_many([d1.id, d2.id, "no-such-doc-id"])
        assert set(found) == {d1.id, d2.id}
        assert found[d1.id].filename == "a.pdf"
        assert found[d2.id].version == 2

        empty = await repo.get_many([])
        assert empty == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_document_repository.py -v`
Expected: FAIL — `AttributeError: 'DocumentRepository' object has no attribute 'get_many'`.

- [ ] **Step 3: Write the implementation**

In `backend/src/fdi/platform/db/repository.py`, add this method to the existing `DocumentRepository`
class (after `list_by_doc_key`, the last method in that class):

```python
    async def get_many(self, ids: list[str]) -> dict[str, Document]:
        """Every `Document` row whose id is in `ids`, keyed by id. Empty input ->
        no query (RT-2's vector_search calls this once per request for its
        doc_key/version filter check AND its doc_key/version citation join --
        an empty hit list must not become a wasted round trip). Unknown ids are
        simply absent from the result, not an error -- same "omit, don't raise"
        posture as resolve_chunks (fdi/pipeline/retrieval.py)."""
        if not ids:
            return {}
        rows = (
            await self._s.execute(select(Document).where(Document.id.in_(ids)))
        ).scalars().all()
        return {d.id: d for d in rows}
```

`select` and `Document` are already imported at the top of `repository.py` (used by
`DocumentRepository.get_by_checksum`/`next_version` and the module's `Document` import respectively) —
no new imports needed.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_document_repository.py -v`
Expected: PASS (both the existing test and the new one).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/platform/db/repository.py backend/tests/integration/test_document_repository.py
git commit -m "feat(platform): DocumentRepository.get_many -- batched doc lookup by id"
```

---

### Task 2: `fdi.core.retrieval.build_vector_results` (pure filter + join + truncate)

**Files:**
- Modify: `backend/src/fdi/core/retrieval.py`
- Test: `backend/tests/unit/test_retrieval_vector_results.py`

**Interfaces:**
- Produces: `build_vector_results(hits: list[Mapping[str, object]], resolved: Mapping[str,
  Mapping[str, object]], docs: Mapping[str, Mapping[str, object]], *, section_type: str | None,
  doc_key: str | None, version: int | None, k: int) -> list[dict[str, object]]`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_retrieval_vector_results.py
from fdi.core.retrieval import build_vector_results


def _hit(id_: str, score: float, metadata: dict[str, object] | None = None) -> dict[str, object]:
    return {"id": id_, "score": score, "metadata": metadata or {}}


def _rec(doc_id: str, chunk_id: str, **extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "doc_id": doc_id, "chunk_id": chunk_id, "heading": "H", "text": "T",
        "section_type": None, "page": None,
    }
    base.update(extra)
    return base


def test_no_filters_returns_all_resolvable_hits_in_order() -> None:
    hits = [_hit("d1:s0", 0.9), _hit("d1:s1", 0.5)]
    resolved = {"d1:s0": _rec("d1", "s0"), "d1:s1": _rec("d1", "s1")}
    docs: dict[str, dict[str, object]] = {}
    out = build_vector_results(
        hits, resolved, docs, section_type=None, doc_key=None, version=None, k=5
    )
    assert [r["chunk_id"] for r in out] == ["s0", "s1"]
    assert out[0]["doc_key"] is None and out[0]["version"] is None
    assert out[0]["score"] == 0.9
    assert out[0]["text"] == "T"


def test_section_type_filter_drops_non_matching() -> None:
    hits = [
        _hit("d1:s0", 0.9, {"section_type": "BALANCE_SHEET"}),
        _hit("d1:s1", 0.8, {"section_type": "NOTES"}),
        _hit("d1:s2", 0.7, {}),  # no metadata -> never matches a set filter
    ]
    resolved = {k: _rec("d1", k.split(":")[1]) for k in ("d1:s0", "d1:s1", "d1:s2")}
    out = build_vector_results(
        hits, resolved, {}, section_type="BALANCE_SHEET", doc_key=None, version=None, k=5
    )
    assert [r["chunk_id"] for r in out] == ["s0"]


def test_doc_key_and_version_filters_drop_mismatches_and_missing_docs() -> None:
    hits = [_hit("d1:s0", 0.9), _hit("d2:s0", 0.8), _hit("d3:s0", 0.7)]
    resolved = {k: _rec(k.split(":")[0], "s0") for k in ("d1:s0", "d2:s0", "d3:s0")}
    docs = {
        "d1": {"doc_key": "annual", "version": 1},
        "d2": {"doc_key": "annual", "version": 2},
        # d3 has no Document row at all
    }
    out = build_vector_results(
        hits, resolved, docs, section_type=None, doc_key="annual", version=1, k=5
    )
    assert [r["doc_id"] for r in out] == ["d1"]
    assert out[0]["doc_key"] == "annual" and out[0]["version"] == 1


def test_no_doc_key_or_version_filter_missing_doc_degrades_to_none_not_dropped() -> None:
    hits = [_hit("d1:s0", 0.9)]
    resolved = {"d1:s0": _rec("d1", "s0")}
    out = build_vector_results(
        hits, resolved, {}, section_type=None, doc_key=None, version=None, k=5
    )
    assert len(out) == 1
    assert out[0]["doc_key"] is None and out[0]["version"] is None


def test_unresolved_hit_is_dropped() -> None:
    hits = [_hit("d1:s0", 0.9), _hit("d1:s1", 0.8)]
    resolved = {"d1:s0": _rec("d1", "s0")}  # s1 never resolved
    out = build_vector_results(
        hits, resolved, {}, section_type=None, doc_key=None, version=None, k=5
    )
    assert [r["chunk_id"] for r in out] == ["s0"]


def test_truncates_to_k_without_resorting() -> None:
    # Deliberately NOT score-sorted going in, to prove no re-sort happens.
    hits = [_hit("d1:s0", 0.1), _hit("d1:s1", 0.9), _hit("d1:s2", 0.5)]
    resolved = {k: _rec("d1", k.split(":")[1]) for k in ("d1:s0", "d1:s1", "d1:s2")}
    out = build_vector_results(
        hits, resolved, {}, section_type=None, doc_key=None, version=None, k=2
    )
    assert [r["chunk_id"] for r in out] == ["s0", "s1"]  # input order, not score order


def test_empty_inputs() -> None:
    assert build_vector_results([], {}, {}, section_type=None, doc_key=None, version=None, k=5) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_retrieval_vector_results.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_vector_results' from 'fdi.core.retrieval'`.

- [ ] **Step 3: Write the implementation**

Append to `backend/src/fdi/core/retrieval.py` (below the existing `build_chunks`; also extend the
module docstring's first paragraph to mention the new function):

```python
def build_vector_results(
    hits: list[Mapping[str, object]],
    resolved: Mapping[str, Mapping[str, object]],
    docs: Mapping[str, Mapping[str, object]],
    *,
    section_type: str | None,
    doc_key: str | None,
    version: int | None,
    k: int,
) -> list[dict[str, object]]:
    """Pure post-processing of an ALREADY-FETCHED vector-store hit list (RT-2).
    `hits` is VectorStore.search's shape ({id, score, metadata}, best-match-
    first); `resolved` is a resolve_chunks() result; `docs` is a plain
    {doc_id: {"doc_key": ..., "version": ...}} dict the caller already read
    from Postgres -- no ORM type crosses into core. Filters, joins citations,
    truncates to k WITHOUT re-sorting (output order is a sub-sequence of the
    input order). A hit is dropped, not erred, when: section_type is set and
    doesn't match; doc_key/version is set and the hit's Document row is
    missing or doesn't match; or its chunk id is absent from `resolved`. No I/O.
    """
    results: list[dict[str, object]] = []
    for hit in hits:
        full_id = str(hit.get("id"))

        if section_type is not None:
            meta = hit.get("metadata")
            hit_type = meta.get("section_type") if isinstance(meta, Mapping) else None
            if hit_type != section_type:
                continue

        doc_id = full_id.split(":", 1)[0] if ":" in full_id else full_id
        doc = docs.get(doc_id)
        if doc_key is not None and (doc is None or doc.get("doc_key") != doc_key):
            continue
        if version is not None and (doc is None or doc.get("version") != version):
            continue

        rec = resolved.get(full_id)
        if rec is None:
            continue

        score = hit.get("score")
        results.append({
            "doc_id": rec.get("doc_id"),
            "doc_key": doc.get("doc_key") if doc is not None else None,
            "version": doc.get("version") if doc is not None else None,
            "chunk_id": rec.get("chunk_id"),
            "heading": rec.get("heading"),
            "page": rec.get("page"),
            "section_type": rec.get("section_type"),
            "text": rec.get("text"),
            "score": float(score) if isinstance(score, int | float) else 0.0,
        })
        if len(results) >= k:
            break

    return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_retrieval_vector_results.py -v`
Expected: PASS (all 7).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
git add backend/src/fdi/core/retrieval.py backend/tests/unit/test_retrieval_vector_results.py
git commit -m "feat(core): retrieval.build_vector_results -- filter+join+truncate vector hits, pure"
```

---

### Task 3: `fdi.pipeline.retrieval.vector_search` (orchestrator)

**Files:**
- Modify: `backend/src/fdi/pipeline/retrieval.py`
- Test: `backend/tests/integration/test_vector_search.py`

**Interfaces:**
- Consumes: `build_vector_results` (Task 2); `resolve_chunks` (RT-1, same module);
  `DocumentRepository.get_many` (Task 1); `EmbeddingClient`/`VectorStore` protocols (SP-13/SP-14).
- Produces: `vector_search(*, session: AsyncSession, store: ArtifactStore, embedder: EmbeddingClient,
  vector_store: VectorStore, query: str, k: int, filters: Mapping[str, object] | None = None) ->
  list[dict[str, object]]`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/integration/test_vector_search.py
"""vector_search: embed + search + filter (section_type in-memory, doc_key/
version via a batched Postgres join) + attach text/citation via resolve_chunks.
Against real Postgres + MinIO with FAKE OCR/semantic/embedding/vector-store
clients -- no BGE/Milvus in the default suite."""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.semantic import SectionType
from fdi.pipeline.embeddings import FakeEmbeddingClient
from fdi.pipeline.intake import intake_document
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.retrieval import vector_search
from fdi.pipeline.run import run_stage
from fdi.pipeline.semantic import FakeSemanticClient
from fdi.pipeline.stages.ast import AstStage
from fdi.pipeline.stages.chunks import ChunksStage
from fdi.pipeline.stages.embedding import EmbeddingStage
from fdi.pipeline.stages.graph import GraphStage
from fdi.pipeline.stages.index import IndexStage
from fdi.pipeline.stages.normalize import NormalizeStage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.pipeline.stages.provenance import ProvenanceStage
from fdi.pipeline.stages.semantic import SemanticStage
from fdi.pipeline.vectorstore import FakeVectorStore
from fdi.platform.db.engine import make_session_factory
from fdi.platform.storage.artifact_store import ArtifactStore

pytestmark = pytest.mark.integration


class _SpyVectorStore(FakeVectorStore):
    """Records every `k` argument `search` was called with, to pin the
    over-fetch factor without depending on corpus size."""

    def __init__(self) -> None:
        super().__init__()
        self.search_k_calls: list[int] = []

    def search(self, vector: list[float], k: int) -> list[dict[str, object]]:
        self.search_k_calls.append(k)
        return super().search(vector, k)


@pytest.fixture
def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(migrated_engine)


@pytest.fixture
async def store(minio_client: Minio) -> ArtifactStore:
    s = ArtifactStore(minio_client, bucket="test-artifacts")
    await s.ensure_bucket()
    return s


def _fake_ocr(title_paras: list[dict[str, object]]) -> FakeOcrClient:
    return FakeOcrClient(
        {
            "content": "ACME",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": title_paras,
            "tables": [],
        }
    )


@pytest.fixture
async def two_docs(
    session_factory, store
) -> tuple[dict[str, dict[str, str]], FakeEmbeddingClient, FakeVectorStore]:
    """Two fully-processed documents, distinct doc_key AND section_type:
    - "bs" (doc_key "alpha", version 1): one BALANCE_SHEET section.
    - "notes" (doc_key "beta", version 1): one NOTES section.
    Returns ({"bs": {"doc_id": ..., "doc_key": "alpha"}, "notes": {...}}, embedder,
    vector_store) -- a tuple, not a mixed-type dict, so the doc metadata and the
    two fake clients each keep their own real type (no `# type: ignore` needed to
    stuff a client instance into a `dict[str, str]`-shaped fixture)."""
    reg = StageRegistry()
    for stage in (
        OcrStage(), NormalizeStage(), AstStage(), SemanticStage(), GraphStage(),
        ProvenanceStage(), ChunksStage(), EmbeddingStage(), IndexStage(),
    ):
        reg.register(stage)
    resolver = DagResolver(reg.all())

    embedder = FakeEmbeddingClient(dim=8)
    vector_store = FakeVectorStore()

    specs = {
        "bs": (
            "alpha",
            _fake_ocr([
                {"role": "sectionHeading", "content": "Balance Sheet"},
                {"content": "Total Assets 100"},
            ]),
            {"Balance Sheet": SectionType.BALANCE_SHEET.value},
        ),
        "notes": (
            "beta",
            _fake_ocr([
                {"role": "sectionHeading", "content": "Notes"},
                {"content": "See appendix"},
            ]),
            {"Notes": SectionType.NOTES.value},
        ),
    }

    out: dict[str, dict[str, str]] = {}
    for key, (doc_key, fake_ocr, sem_map) in specs.items():
        doc = await intake_document(
            pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(),
            filename=f"{key}.pdf",
            doc_key=doc_key,
            content_type="application/pdf",
            session_factory=session_factory,
            store=store,
        )
        common = {
            "doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
            "store": store, "registry": reg, "resolver": resolver,
        }
        await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake_ocr}, **common)
        await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
        await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
        await run_stage(
            stage_name="semantic", run_id=str(uuid.uuid4()),
            services={"semantic": FakeSemanticClient(sem_map)}, **common,
        )
        await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
        await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)
        await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)
        await run_stage(
            stage_name="embedding", run_id=str(uuid.uuid4()),
            services={"embedder": embedder}, **common,
        )
        await run_stage(
            stage_name="index", run_id=str(uuid.uuid4()),
            services={"vector_store": vector_store}, **common,
        )
        out[key] = {"doc_id": doc.id, "doc_key": doc_key}

    return out, embedder, vector_store


async def test_vector_search_no_filters_returns_both_docs(session_factory, store, two_docs) -> None:
    docs, embedder, vector_store = two_docs
    async with session_factory() as s:
        out = await vector_search(
            session=s, store=store, embedder=embedder, vector_store=vector_store,
            query="Balance Sheet\nTotal Assets 100", k=5, filters=None,
        )
    doc_ids = {r["doc_id"] for r in out}
    assert docs["bs"]["doc_id"] in doc_ids
    assert docs["notes"]["doc_id"] in doc_ids
    bs_result = next(r for r in out if r["doc_id"] == docs["bs"]["doc_id"])
    assert bs_result["doc_key"] == "alpha" and bs_result["version"] == 1
    assert bs_result["section_type"] == "BALANCE_SHEET"
    assert "Total Assets 100" in bs_result["text"]


async def test_vector_search_section_type_filter(session_factory, store, two_docs) -> None:
    docs, embedder, vector_store = two_docs
    async with session_factory() as s:
        out = await vector_search(
            session=s, store=store, embedder=embedder, vector_store=vector_store,
            query="assets and notes", k=5, filters={"section_type": "NOTES"},
        )
    assert out
    assert all(r["section_type"] == "NOTES" for r in out)
    assert docs["notes"]["doc_id"] in {r["doc_id"] for r in out}
    assert docs["bs"]["doc_id"] not in {r["doc_id"] for r in out}


async def test_vector_search_doc_key_filter(session_factory, store, two_docs) -> None:
    _docs, embedder, vector_store = two_docs
    async with session_factory() as s:
        out = await vector_search(
            session=s, store=store, embedder=embedder, vector_store=vector_store,
            query="assets and notes", k=5, filters={"doc_key": "alpha"},
        )
    assert out
    assert all(r["doc_key"] == "alpha" for r in out)


async def test_vector_search_filter_matching_nothing_returns_empty(
    session_factory, store, two_docs
) -> None:
    _docs, embedder, vector_store = two_docs
    async with session_factory() as s:
        out = await vector_search(
            session=s, store=store, embedder=embedder, vector_store=vector_store,
            query="assets and notes", k=5, filters={"doc_key": "no-such-doc-key"},
        )
    assert out == []


async def test_vector_search_overfetches_only_when_filtered(session_factory, store) -> None:
    """Pins the k*5 over-fetch factor directly against a spy, independent of
    two_docs' fixture corpus size."""
    embedder = FakeEmbeddingClient(dim=8)
    spy = _SpyVectorStore()
    async with session_factory() as s:
        await vector_search(
            session=s, store=store, embedder=embedder, vector_store=spy,
            query="q", k=3, filters=None,
        )
        await vector_search(
            session=s, store=store, embedder=embedder, vector_store=spy,
            query="q", k=3, filters={"section_type": "NOTES"},
        )
    assert spy.search_k_calls == [3, 15]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_vector_search.py -v`
Expected: FAIL — `ImportError: cannot import name 'vector_search' from 'fdi.pipeline.retrieval'`.

- [ ] **Step 3: Write the implementation**

Replace `backend/src/fdi/pipeline/retrieval.py`'s top import block and append `vector_search` below
`resolve_chunks`:

```python
"""Retrieval orchestrator helpers: turn a hit id `{doc_id}:{chunk_id}` (Milvus,
or later a graph node) into its full RT-1 chunk record (text + citation) by
reading the `chunks` artifact. Pipeline layer (does I/O) -- imports platform
(repo + store) + core, the same shape as resolve_entities. RT-2..RT-8 call
resolve_chunks to attach text to a whole result set in one read per document.

RT-2's vector_search (below) is the first consumer: embed the query, search the
injected VectorStore, filter by section_type (Milvus metadata, in-memory) and/or
doc_key/version (one batched Postgres join via DocumentRepository.get_many), then
delegate the filter/join/truncate assembly to fdi.core.retrieval.build_vector_results
(pure, independently unit-tested)."""

from collections.abc import Mapping
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core.retrieval import build_vector_results
from fdi.pipeline.embeddings import EmbeddingClient
from fdi.pipeline.vectorstore import VectorStore
from fdi.platform.db.repository import DocumentRepository, RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore


async def _read_chunks(
    repo: RegistryRepository, store: ArtifactStore, doc_id: str
) -> list[Mapping[str, object]]:
    run = await repo.latest_run(doc_id, "chunks")
    if run is None or run.artifact_key is None:
        return []
    body = cast(Mapping[str, object], await store.get_json(run.artifact_key))
    chunks = body.get("chunks")
    return [c for c in chunks if isinstance(c, Mapping)] if isinstance(chunks, list) else []


async def resolve_chunk(
    *, session: AsyncSession, store: ArtifactStore, doc_id: str, chunk_id: str
) -> dict[str, object] | None:
    repo = RegistryRepository(session)
    for c in await _read_chunks(repo, store, doc_id):
        if c.get("chunk_id") == chunk_id:
            return {"doc_id": doc_id, **c}
    return None


async def resolve_chunks(
    *, session: AsyncSession, store: ArtifactStore, ids: list[str]
) -> dict[str, dict[str, object]]:
    by_doc: dict[str, list[str]] = {}
    for full_id in ids:
        if ":" not in full_id:
            continue
        doc_id, chunk_id = full_id.split(":", 1)
        by_doc.setdefault(doc_id, []).append(chunk_id)

    repo = RegistryRepository(session)
    out: dict[str, dict[str, object]] = {}
    for doc_id, chunk_ids in by_doc.items():
        records = {c.get("chunk_id"): c for c in await _read_chunks(repo, store, doc_id)}
        for chunk_id in chunk_ids:
            rec = records.get(chunk_id)
            if rec is not None:
                out[f"{doc_id}:{chunk_id}"] = {"doc_id": doc_id, **rec}
    return out


_OVERFETCH_FACTOR = 5


async def vector_search(
    *,
    session: AsyncSession,
    store: ArtifactStore,
    embedder: EmbeddingClient,
    vector_store: VectorStore,
    query: str,
    k: int,
    filters: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    """RT-2's semantic retrieval orchestrator: embed `query`, search
    `vector_store`, filter/join/cite via build_vector_results, attaching text
    through resolve_chunks. Over-fetches k*_OVERFETCH_FACTOR from the vector
    store when any filter is set (VectorStore.search has no filter parameter --
    design spec sec 2.3-4), single-shot: no adaptive re-query on under-fill.
    Exactly one embed call, one vector_store.search call, one
    DocumentRepository.get_many call, one resolve_chunks call -- no N+1."""
    section_type = filters.get("section_type") if filters else None
    doc_key = filters.get("doc_key") if filters else None
    version = filters.get("version") if filters else None
    has_filter = section_type is not None or doc_key is not None or version is not None

    [vector] = embedder.embed([query])
    fetch_k = k * _OVERFETCH_FACTOR if has_filter else k
    hits = vector_store.search(vector, fetch_k)

    doc_ids = {str(h["id"]).split(":", 1)[0] for h in hits if ":" in str(h["id"])}
    doc_rows = await DocumentRepository(session).get_many(list(doc_ids))
    docs: dict[str, Mapping[str, object]] = {
        doc_id: {"doc_key": d.doc_key, "version": d.version} for doc_id, d in doc_rows.items()
    }

    resolved = await resolve_chunks(session=session, store=store, ids=[str(h["id"]) for h in hits])

    return build_vector_results(
        hits,
        resolved,
        docs,
        section_type=cast(str | None, section_type),
        doc_key=cast(str | None, doc_key),
        version=cast(int | None, version),
        k=k,
    )
```

(`resolve_chunk`/`resolve_chunks`/`_read_chunks` are reproduced unchanged from RT-1 — this is a full-file
replace for clarity of the new import block and append point, not a behavior change to those three.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_vector_search.py tests/integration/test_resolve_chunk.py -v`
Expected: PASS (the new file's 5 tests; RT-1's existing `resolve_chunk`/`resolve_chunks` tests
unaffected by the reproduced-unchanged code above them).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/pipeline/retrieval.py backend/tests/integration/test_vector_search.py
git commit -m "feat(pipeline): retrieval.vector_search -- embed+search+filter+cite orchestrator"
```

---

### Task 4: `POST /retrieve/vector`

**Files:**
- Modify: `backend/src/fdi/api/schemas.py`
- Modify: `backend/src/fdi/api/routes.py`
- Test: `backend/tests/integration/test_api.py` (extend the existing file)

**Interfaces:**
- Consumes: `vector_search` (Task 3); `SessionDep`, `StoreDep`, `EmbedderDep`, `VectorStoreDep`
  (existing, `fdi.api.deps`).
- Produces: `VectorSearchFilters`, `VectorSearchRequest`, `VectorSearchResultView`,
  `VectorSearchResultsView` (`fdi.api.schemas`); `POST /retrieve/vector` (`fdi.api.routes`).

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/integration/test_api.py`, directly below the existing `# Task 6 (SP-14): POST
/search` section (after `test_search_returns_the_best_match_first`, before the `POST /entities/resolve`
section). Do **not** modify either existing `/search` test.

```python
# --------------------------------------------------------------------------
# RT-2: POST /retrieve/vector -- text + citations, section_type/doc_key/version filters.
# --------------------------------------------------------------------------


async def test_retrieve_vector_503_when_unconfigured(client: AsyncClient, _alembic_applied) -> None:
    r = await client.post("/retrieve/vector", json={"query": "Balance Sheet"})
    assert r.status_code == 503
    assert "vector_store" in r.json()["detail"]


async def _seed_processed_doc(session_factory, store) -> dict[str, object]:
    """One fully-processed document (real chunks artifact + a real Document
    row), for exercising the route's Postgres join / resolve_chunks path --
    NOT the raw synthetic-vector shape /search's own test uses."""
    from fdi.core.semantic import SectionType
    from fdi.pipeline.intake import intake_document
    from fdi.pipeline.ocr import FakeOcrClient
    from fdi.pipeline.registry import StageRegistry
    from fdi.pipeline.resolver import DagResolver
    from fdi.pipeline.run import run_stage
    from fdi.pipeline.semantic import FakeSemanticClient
    from fdi.pipeline.stages.ast import AstStage
    from fdi.pipeline.stages.chunks import ChunksStage
    from fdi.pipeline.stages.graph import GraphStage
    from fdi.pipeline.stages.normalize import NormalizeStage
    from fdi.pipeline.stages.ocr import OcrStage
    from fdi.pipeline.stages.provenance import ProvenanceStage
    from fdi.pipeline.stages.semantic import SemanticStage

    doc_key = f"annual-{uuid.uuid4()}"
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(),
        filename="a.pdf", doc_key=doc_key, content_type="application/pdf",
        session_factory=session_factory, store=store,
    )
    fake_ocr = FakeOcrClient({
        "content": "ACME",
        "pages": [{"pageNumber": 1, "lines": [], "words": []}],
        "paragraphs": [
            {"role": "sectionHeading", "content": "Balance Sheet"},
            {"content": "Total Assets 100"},
        ],
        "tables": [],
    })
    fake_semantic = FakeSemanticClient({"Balance Sheet": SectionType.BALANCE_SHEET.value})

    reg = StageRegistry()
    for stage in (
        OcrStage(), NormalizeStage(), AstStage(), SemanticStage(), GraphStage(),
        ProvenanceStage(), ChunksStage(),
    ):
        reg.register(stage)
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
    await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)
    return {"doc_id": doc.id, "doc_key": doc_key}


async def test_retrieve_vector_returns_text_and_citation(
    make_client, _alembic_applied, session_factory, store
) -> None:
    from fdi.api.deps import get_embedder, get_vector_store
    from fdi.pipeline.embeddings import FakeEmbeddingClient
    from fdi.pipeline.vectorstore import FakeVectorStore

    seeded = await _seed_processed_doc(session_factory, store)
    fake_embedder = FakeEmbeddingClient(dim=8)
    fake_store = FakeVectorStore()
    fake_store.upsert([{
        "id": f"{seeded['doc_id']}:s0",
        "vector": fake_embedder.embed(["Balance Sheet\nTotal Assets 100"])[0],
        "metadata": {"heading": "Balance Sheet", "chunk_id": "s0",
                      "section_type": "BALANCE_SHEET", "page": 1},
    }])

    async with make_client(
        dependency_overrides={
            get_embedder: lambda: fake_embedder,
            get_vector_store: lambda: fake_store,
        }
    ) as client:
        r = await client.post(
            "/retrieve/vector",
            json={"query": "Balance Sheet\nTotal Assets 100", "k": 1},
        )

    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    assert result["doc_id"] == seeded["doc_id"]
    assert result["doc_key"] == seeded["doc_key"]
    assert result["version"] == 1
    assert result["chunk_id"] == "s0"
    assert result["section_type"] == "BALANCE_SHEET"
    assert "Total Assets 100" in result["text"]
    assert result["score"] == pytest.approx(1.0)


async def test_retrieve_vector_section_type_filter(
    make_client, _alembic_applied, session_factory, store
) -> None:
    from fdi.api.deps import get_embedder, get_vector_store
    from fdi.pipeline.embeddings import FakeEmbeddingClient
    from fdi.pipeline.vectorstore import FakeVectorStore

    seeded = await _seed_processed_doc(session_factory, store)
    fake_embedder = FakeEmbeddingClient(dim=8)
    fake_store = FakeVectorStore()
    fake_store.upsert([{
        "id": f"{seeded['doc_id']}:s0",
        "vector": fake_embedder.embed(["Balance Sheet\nTotal Assets 100"])[0],
        "metadata": {"heading": "Balance Sheet", "chunk_id": "s0",
                      "section_type": "BALANCE_SHEET", "page": 1},
    }])

    async with make_client(
        dependency_overrides={
            get_embedder: lambda: fake_embedder,
            get_vector_store: lambda: fake_store,
        }
    ) as client:
        miss = await client.post(
            "/retrieve/vector",
            json={
                "query": "Balance Sheet\nTotal Assets 100", "k": 5,
                "filters": {"section_type": "NOTES"},
            },
        )
        hit = await client.post(
            "/retrieve/vector",
            json={
                "query": "Balance Sheet\nTotal Assets 100", "k": 5,
                "filters": {"section_type": "BALANCE_SHEET"},
            },
        )

    assert miss.json()["results"] == []
    assert len(hit.json()["results"]) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_api.py -k retrieve_vector -v`
Expected: FAIL — 404 (no `/retrieve/vector` route yet).

- [ ] **Step 3: Implement schemas + route**

In `backend/src/fdi/api/schemas.py`, append (after `SearchResultsView`):

```python
class VectorSearchFilters(BaseModel):
    section_type: str | None = None
    doc_key: str | None = None
    version: int | None = None


class VectorSearchRequest(BaseModel):
    query: str
    k: int = 5
    filters: VectorSearchFilters | None = None


class VectorSearchResultView(BaseModel):
    doc_id: str
    doc_key: str | None
    version: int | None
    chunk_id: str
    heading: str | None
    page: int | None
    section_type: str | None
    text: str
    score: float


class VectorSearchResultsView(BaseModel):
    results: list[VectorSearchResultView]
```

In `backend/src/fdi/api/routes.py`, change the `from fdi.api.schemas import (...)` block from:

```python
from fdi.api.schemas import (
    DocumentVersionsView,
    DocumentView,
    EmbeddingTriggerView,
    EntitiesView,
    EntityResolveRequest,
    EvalAccepted,
    EvalRequest,
    GraphLoadTriggerView,
    JobAccepted,
    JobView,
    LeaderboardEntry,
    LeaderboardView,
    OcrTriggerView,
    ReadyView,
    ReprocessView,
    SearchRequest,
    SearchResultsView,
    SearchResultView,
    SemanticTriggerView,
    StageStatusView,
    StagesView,
    StaleView,
)
```

to (four new names inserted alphabetically):

```python
from fdi.api.schemas import (
    DocumentVersionsView,
    DocumentView,
    EmbeddingTriggerView,
    EntitiesView,
    EntityResolveRequest,
    EvalAccepted,
    EvalRequest,
    GraphLoadTriggerView,
    JobAccepted,
    JobView,
    LeaderboardEntry,
    LeaderboardView,
    OcrTriggerView,
    ReadyView,
    ReprocessView,
    SearchRequest,
    SearchResultsView,
    SearchResultView,
    SemanticTriggerView,
    StageStatusView,
    StagesView,
    StaleView,
    VectorSearchRequest,
    VectorSearchResultsView,
    VectorSearchResultView,
)
```

**Deliberately NOT importing `VectorSearchFilters` here**: the route body accesses it only via
`body.filters.section_type` etc. (attribute access on an already-typed `VectorSearchRequest` instance)
and never names the `VectorSearchFilters` class itself — importing it anyway would be an unused import
(`ruff` F401) and fail the gate. `VectorSearchRequest`'s own field annotation is what makes
`body.filters` a validated `VectorSearchFilters | None` at runtime; the route module needs no import of
the nested model's name to use it.

and change:

```python
from fdi.pipeline.crossdoc import resolve_entities
from fdi.pipeline.eval_service import run_evaluation
from fdi.pipeline.intake import intake_document
```

to:

```python
from fdi.pipeline.crossdoc import resolve_entities
from fdi.pipeline.eval_service import run_evaluation
from fdi.pipeline.intake import intake_document
from fdi.pipeline.retrieval import vector_search
```

Then append the new route directly below the existing `/search` block (after its closing `)` and
before the `# Task 6 (SP-16): POST /entities/resolve` comment block), with its own section comment:

```python
# --------------------------------------------------------------------------
# RT-2: POST /retrieve/vector -- semantic search WITH text + citations,
# filterable by section_type (Milvus metadata) and/or doc_key/version (a
# Postgres join). POST /search above is UNCHANGED -- design spec
# docs/superpowers/specs/2026-07-18-rt2-vector-retrieval-design.md sec 2
# decision 5 -- /search stays the raw, never-dropping probe it always was;
# this route is the additive, citation-first tool the retrieval roadmap's
# vector_search(query, filters, k) describes.
# --------------------------------------------------------------------------


@router.post("/retrieve/vector", response_model=VectorSearchResultsView)
async def retrieve_vector(
    body: VectorSearchRequest,
    session: SessionDep,
    store: StoreDep,
    embedder: EmbedderDep,
    vector_store: VectorStoreDep,
) -> VectorSearchResultsView:
    missing = [
        name
        for name, svc in (("embedder", embedder), ("vector_store", vector_store))
        if svc is None
    ]
    if missing:
        raise HTTPException(
            status_code=503,
            detail=(
                f"retrieve/vector is not configured: missing {', '.join(missing)} "
                "(set FDI_MILVUS_URI -- embeddings run locally via BGE and need no endpoint/key)"
            ),
        )
    assert embedder is not None
    assert vector_store is not None
    filters = {
        "section_type": body.filters.section_type if body.filters else None,
        "doc_key": body.filters.doc_key if body.filters else None,
        "version": body.filters.version if body.filters else None,
    }
    results = await vector_search(
        session=session,
        store=store,
        embedder=embedder,
        vector_store=vector_store,
        query=body.query,
        k=body.k,
        filters=filters,
    )
    return VectorSearchResultsView(
        results=[VectorSearchResultView.model_validate(r) for r in results]
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_api.py -v`
Expected: PASS — every existing test in the file (including both `/search` tests, unchanged) plus the
three new `/retrieve/vector` tests.

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/api/schemas.py backend/src/fdi/api/routes.py backend/tests/integration/test_api.py
git commit -m "feat(api): POST /retrieve/vector -- semantic search with text + citations + filters"
```

---

### Task 5: Full gates, roadmap + spec status

**Files:**
- Modify: `docs/superpowers/specs/2026-07-18-rt2-vector-retrieval-design.md` (Status → Implemented)
- Modify: `docs/architecture/retrieval-roadmap.md` (RT-2 row → done + "what RT-2 shipped" handoff)

- [ ] **Step 1: Full static gates** — `ruff check .`, `mypy --strict src`, `PYTHONUTF8=1 lint-imports`
  (1 kept/0 broken). Grep-confirm `fdi.core.retrieval` still imports neither `fdi.platform.*` nor
  `fdi.pipeline.*`.

- [ ] **Step 2: Full suite** — `uv run --directory backend pytest -q`; green (e2e/azure/neo4j/milvus/
  bge skip). Note the passed count against RT-1's baseline + this plan's 16 new tests (Task 1: 1, Task
  2: 7, Task 3: 5, Task 4: 3, plus the untouched pre-existing `/search` tests).

- [ ] **Step 3: Audit RT-2 done criteria** (design spec §7) — map each to Tasks 1–4; confirm the
  over-fetch-factor assertion (`test_vector_search_overfetches_only_when_filtered`), the doc_key/
  version-drops-mismatches assertion, and the truncation-without-resort assertion all pass; confirm
  `POST /search`'s two pre-existing tests are byte-for-byte unmodified in the diff (`git diff main --
  backend/tests/integration/test_api.py` shows only additions, no changes inside the existing `/search`
  test bodies).

- [ ] **Step 4: Spec status → Implemented 2026-07-18** (only after Steps 1–3 green).

- [ ] **Step 5: Roadmap** — flip the RT-2 row to `✅ done`; add a short "What RT-2 shipped that
  RT-3…RT-8 must know" handoff covering: `vector_search`/`build_vector_results`'s pure/impure split (RT-5
  graph retrieval and RT-6 table retrieval may want the same shape); `DocumentRepository.get_many` as a
  reusable batched-doc-lookup primitive; the `_OVERFETCH_FACTOR = 5` constant and its single-shot,
  no-Milvus-filter-parameter design; and, prominently, that **`POST /search` was deliberately left
  unchanged** (not silently dropped from the roadmap's original wording) — RT-8's `/ask` agent should
  register `vector_search`/`/retrieve/vector` as its citation-bearing tool, not `/search`.

- [ ] **Step 6: Commit**

```bash
git add docs
git commit -m "docs: RT-2 done-criteria audit, spec status, roadmap handoff"
```

---

## Self-Review

**Spec coverage:** §3.1 `build_vector_results` → Task 2. §3.2 `vector_search` → Task 3. §3.3
`DocumentRepository.get_many` → Task 1. §3.4/3.5 schemas + route → Task 4. §2 decisions: orchestrator
signature/location (d1) → Task 3; pure/impure split (d2) → Tasks 2+3; over-fetch factor/single-shot
(d3) → Task 3 + its spy test; no protocol change (d4) → Task 3 (no `VectorStore.search` signature edit
anywhere in this plan); `/search` left unchanged (d5) → Task 4 (Global Constraint explicitly forbids
touching it; Task 5 Step 3 verifies the diff); citation shape / None-vs-dropped rules (d6) → Task 2's
tests `test_no_doc_key_or_version_filter_missing_doc_degrades_to_none_not_dropped` and
`test_doc_key_and_version_filters_drop_mismatches_and_missing_docs`; order preservation (d7) → Task 2's
`test_truncates_to_k_without_resorting`; route shape (d8) → Task 4; no new config (d9) → Global
Constraints + Task 3 (private constant, no `config.py`/`compose.yml`/`.env.example` touched anywhere in
this plan). §4 error handling → Task 2's empty/missing-doc/unresolved tests, Task 3's filter-matching-
nothing test, Task 4's 503 test. §5 testing → Tasks 1–4 map 1:1 to the spec's five test-file bullets.
§6 deferred → not built (Task 5 audit; no adaptive re-query, no Milvus filter param, no new config var,
no `/search` change anywhere in the diff). §7 done criteria → Task 5.

**Placeholder scan:** Tasks 1–4 carry complete, runnable code for every new function, method, schema,
and route — no `...` bodies, no "reuse X's fixture" hand-waves left unresolved (Task 3/4's fixtures are
written out in full, adapted from the NAMED existing harnesses `test_index_stage.py`/
`test_resolve_chunk.py`/`test_api.py`'s own `test_search_returns_the_best_match_first`, not merely
referenced). Task 3's `resolve_chunk`/`resolve_chunks`/`_read_chunks` reproduction is intentionally
verbatim (RT-1's unchanged code), called out as such so the executor doesn't second-guess a diff that
touches lines it didn't need to change.

**Type consistency:** `build_vector_results(hits: list[Mapping], resolved: Mapping[str, Mapping], docs:
Mapping[str, Mapping], ...) -> list[dict]` (Task 2) is consumed unchanged by `vector_search` (Task 3),
which builds `docs` as `dict[str, Mapping[str, object]]` from `DocumentRepository.get_many`'s
`dict[str, Document]` (Task 1) via a plain dict comprehension (`{"doc_key": d.doc_key, "version":
d.version}`) — the ORM type never crosses into `core`. `vector_search(...) -> list[dict[str, object]]`
(Task 3) is consumed unchanged by the route (Task 4) via `VectorSearchResultView.model_validate(r)` —
every key `build_vector_results` emits (`doc_id, doc_key, version, chunk_id, heading, page,
section_type, text, score`) matches `VectorSearchResultView`'s field names exactly, so
`model_validate` needs no field renaming/aliasing.

**Risk flagged:** Task 3's `two_docs` fixture and Task 4's `_seed_processed_doc` both duplicate a
similar "process one doc through ocr→chunks" sequence already seen in `test_resolve_chunk.py`/
`test_index_stage.py` rather than importing a shared fixture — matches this codebase's existing
per-file-fixture convention (every reviewed integration test file defines its own `session_factory`/
`store`/processing fixtures rather than a shared `conftest.py` helper), so this is consistent with
house style, not a deviation, but the executor should resist the urge to "clean up" the duplication
into a shared conftest fixture as an unplanned scope addition to this task.
