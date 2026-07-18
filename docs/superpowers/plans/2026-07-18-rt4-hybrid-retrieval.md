# RT-4 Hybrid Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fuse RT-2's `vector_search` and RT-3's `keyword_search` with a pure
Reciprocal Rank Fusion function, an orchestrator that degrades gracefully
when Milvus is unconfigured, and one `POST /retrieve` route.

**Architecture:** A pure `fdi.core.retrieval.fuse(vector_hits, keyword_hits,
*, k, rrf_k=60)` sums `1/(rrf_k+rank)` per hit list per `{doc_id}:{chunk_id}`,
sorts descending with a deterministic tie-break, dedups to the richest
citation, truncates to `k`. `fdi.pipeline.retrieval.hybrid_search` calls
`keyword_search` unconditionally and `vector_search` only when both
`embedder`/`vector_store` are configured, then delegates to `fuse` — no I/O,
no fusion logic inline. `POST /retrieve` is a thin route with no 503 branch
(unlike `/retrieve/vector`): keyword-only degradation means the endpoint
always has something to answer with.

**Tech Stack:** Python 3.13, `uv`, SQLAlchemy async, pytest + testcontainers
(real Postgres + MinIO; Milvus stays mocked via `FakeVectorStore`).

## Global Constraints

- Layered contract `api | workers → pipeline → platform → core` (exhaustive),
  CI-enforced; stay "1 kept, 0 broken". `fdi.core.retrieval` is PURE: no
  I/O/config/clock/RNG, no import of `fdi.platform.*`/`fdi.pipeline.*`.
- Determinism: no wall-clock/RNG; `fuse`'s tie-break is a string comparison
  (`{doc_id}:{chunk_id}` ascending), never relies on dict/sort stability
  alone.
- **Upstream signatures RT-4 builds on, unchanged by this plan:**
  - `fdi.pipeline.retrieval.vector_search(*, session, store, embedder,
    vector_store, query, k, filters=None) -> list[dict[str, object]]` — hits
    shaped `{doc_id, doc_key, version, chunk_id, heading, page, section_type,
    text, score}`, best-match-first.
  - `fdi.pipeline.retrieval.keyword_search(*, session, query, k=5,
    section_type=None, doc_key=None, version=None) -> list[dict[str,
    object]]` — the SAME nine-key shape, `ts_rank`-ordered, best-first.
  - Both already exist in `backend/src/fdi/pipeline/retrieval.py`; this plan
    only APPENDS to that module and to `backend/src/fdi/core/retrieval.py`
    (which already has `build_chunks`, `build_vector_results`,
    `chunk_rows_for_index`).
- `fuse` never raises: a hit missing a `str` `doc_id`/`chunk_id` is skipped,
  not erred; empty input lists → `[]`.
- `hybrid_search` never raises and `POST /retrieve` never 503s — an
  unconfigured `embedder`/`vector_store` degrades to keyword-only results,
  it does not fail the request (design spec §2 decision 2, a deliberate
  divergence from `/search`'s and `/retrieve/vector`'s 503-when-unconfigured
  precedent).
- Per-task gates before every commit: `uv run --directory backend ruff check .`
  · `uv run --directory backend mypy --strict src` ·
  `PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter`
  · the task's pytest. Line length ≤ 100.
- TDD: failing test → run (see fail) → minimal impl → run (see pass) →
  commit.
- Postgres' `chunk` table is **session-scoped across the whole integration
  suite** (already true before this plan — see
  `backend/tests/integration/test_keyword_search.py`'s own comment on this).
  Several existing fixtures (`_seed_processed_doc` in `test_api.py`,
  `two_docs` in `test_vector_search.py`) already seed the literal, unmarked
  text `"Balance Sheet"` / `"Total Assets 100"`. Every new integration test
  in this plan that needs a keyword-side exact match embeds a fresh
  `uuid.uuid4().hex` marker token into its own fixture's OCR text and builds
  its query around that marker, so `plainto_tsquery`'s AND-of-lexemes
  semantics guarantee a match against ONLY that test's own row — this is not
  optional decoration, a test that skips it is flaky by construction once
  enough other suite fixtures share the unmarked phrase.

---

### Task 1: `fdi.core.retrieval.fuse` (pure Reciprocal Rank Fusion)

**Files:**
- Modify: `backend/src/fdi/core/retrieval.py` (append `_RRF_K`,
  `_citation_richness`, `fuse`)
- Test: `backend/tests/unit/test_retrieval_fuse.py`

**Interfaces:**
- Consumes: nothing new — pure function of its own two list arguments.
- Produces: `fuse(vector_hits: list[Mapping[str, object]], keyword_hits:
  list[Mapping[str, object]], *, k: int, rrf_k: int = 60) ->
  list[dict[str, object]]`. This is what Task 2's `hybrid_search` calls.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_retrieval_fuse.py
import pytest

from fdi.core.retrieval import fuse


def _hit(
    doc_id: str,
    chunk_id: str,
    *,
    doc_key: str | None = "K",
    version: int | None = 1,
    heading: str | None = "H",
    page: int | None = 1,
    section_type: str | None = "NOTES",
    text: str = "text",
) -> dict[str, object]:
    return {
        "doc_id": doc_id,
        "doc_key": doc_key,
        "version": version,
        "chunk_id": chunk_id,
        "heading": heading,
        "page": page,
        "section_type": section_type,
        "text": text,
        "score": 0.5,  # the input hit's own tool-specific score; fuse ignores it
    }


def test_fuse_empty_lists_returns_empty() -> None:
    assert fuse([], [], k=5) == []


def test_fuse_chunk_in_both_lists_outranks_single_list_chunk() -> None:
    a = _hit("d1", "s0")
    b = _hit("d1", "s1")
    out = fuse([a, b], [a], k=5)  # a: vector rank1 + keyword rank1; b: vector rank2 only
    ids = [f"{h['doc_id']}:{h['chunk_id']}" for h in out]
    assert ids == ["d1:s0", "d1:s1"]
    assert out[0]["score"] == pytest.approx(2 / 61)
    assert out[1]["score"] == pytest.approx(1 / 62)


def test_fuse_disjoint_lists_merge_by_score() -> None:
    a = _hit("d1", "s0")  # vector rank1
    b = _hit("d2", "s0")  # keyword rank1
    out = fuse([a], [b], k=5)
    ids = {f"{h['doc_id']}:{h['chunk_id']}" for h in out}
    assert ids == {"d1:s0", "d2:s0"}
    assert out[0]["score"] == pytest.approx(out[1]["score"])


def test_fuse_ties_broken_by_chunk_id_ascending() -> None:
    a = _hit("zzz", "s0")  # vector rank1, alone
    b = _hit("aaa", "s0")  # keyword rank1, alone -- same score as a
    out = fuse([a], [b], k=5)
    assert [f"{h['doc_id']}:{h['chunk_id']}" for h in out] == ["aaa:s0", "zzz:s0"]


def test_fuse_truncates_to_k_keeping_best_first() -> None:
    hits = [_hit(f"d{i}", "s0") for i in range(5)]
    out = fuse(hits, [], k=2)
    assert [f"{h['doc_id']}:{h['chunk_id']}" for h in out] == ["d0:s0", "d1:s0"]


def test_fuse_richest_citation_wins_on_dedup() -> None:
    thin = _hit("d1", "s0", doc_key=None, version=None, page=None, section_type=None)
    rich = _hit("d1", "s0", doc_key="K2", version=2, page=7, section_type="BALANCE_SHEET")
    out = fuse([thin], [rich], k=5)
    assert out[0]["doc_key"] == "K2"
    assert out[0]["version"] == 2
    assert out[0]["page"] == 7
    assert out[0]["section_type"] == "BALANCE_SHEET"


def test_fuse_richest_citation_tie_prefers_vector_hit_seen_first() -> None:
    v = _hit("d1", "s0", heading="from-vector")
    kw = _hit("d1", "s0", heading="from-keyword")
    out = fuse([v], [kw], k=5)  # equal richness (heading differs, both non-None) -> tie
    assert out[0]["heading"] == "from-vector"


def test_fuse_skips_hits_missing_doc_id_or_chunk_id() -> None:
    missing_doc_id: dict[str, object] = {"chunk_id": "s0"}
    missing_chunk_id: dict[str, object] = {"doc_id": "d1"}
    good = _hit("d1", "s1")
    out = fuse([missing_doc_id, missing_chunk_id, good], [], k=5)
    assert len(out) == 1
    assert out[0]["doc_id"] == "d1" and out[0]["chunk_id"] == "s1"


def test_fuse_custom_rrf_k_changes_score_value() -> None:
    a = _hit("d1", "s0")
    default_out = fuse([a], [], k=5)
    custom_out = fuse([a], [], k=5, rrf_k=10)
    assert default_out[0]["score"] == pytest.approx(1 / 61)
    assert custom_out[0]["score"] == pytest.approx(1 / 11)


def test_fuse_preserves_text_and_score_is_float() -> None:
    a = _hit("d1", "s0", text="the actual passage")
    out = fuse([a], [], k=5)
    assert out[0]["text"] == "the actual passage"
    assert isinstance(out[0]["score"], float)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_retrieval_fuse.py -v`
Expected: FAIL — `ImportError: cannot import name 'fuse' from 'fdi.core.retrieval'`.

- [ ] **Step 3: Write the implementation**

Append to `backend/src/fdi/core/retrieval.py` (existing file — do not remove
`build_chunks`/`build_vector_results`/`chunk_rows_for_index`; extend the
module docstring's function list to mention `fuse`):

```python
_RRF_K = 60


def _citation_richness(hit: Mapping[str, object]) -> int:
    return sum(
        1
        for key in ("doc_key", "version", "heading", "page", "section_type")
        if hit.get(key) is not None
    )


def fuse(
    vector_hits: list[Mapping[str, object]],
    keyword_hits: list[Mapping[str, object]],
    *,
    k: int,
    rrf_k: int = _RRF_K,
) -> list[dict[str, object]]:
    """Reciprocal Rank Fusion (RRF) over two best-first, citation-shaped hit
    lists (RT-2's vector_search / RT-3's keyword_search output shape --
    {doc_id, doc_key, version, chunk_id, heading, page, section_type, text,
    score}). Each hit is identified by "{doc_id}:{chunk_id}" (roadmap
    cross-cutting decision 1; computed here, not read off a field). A hit's
    fused score is the sum, over every list it appears in, of
    1/(rrf_k + rank), rank 1-based -- a hit found by both lists therefore
    routinely outranks one found by only one. Output is sorted by fused
    score descending, ties broken by the "{doc_id}:{chunk_id}" string
    ascending (deterministic; never relies on input order or sort stability
    once scores tie), truncated to k. When the same chunk appears in both
    lists with citations of different completeness, the RICHEST citation
    (most non-None among doc_key/version/heading/page/section_type) is kept
    on the fused record; a tie keeps whichever hit was seen first --
    vector_hits before keyword_hits, this function's own argument order. A
    hit missing a str doc_id/chunk_id is skipped, not erred. Pure, total,
    never raises. No I/O."""
    scores: dict[str, float] = {}
    richest: dict[str, Mapping[str, object]] = {}
    for hits in (vector_hits, keyword_hits):
        for rank, hit in enumerate(hits, start=1):
            doc_id = hit.get("doc_id")
            chunk_id = hit.get("chunk_id")
            if not isinstance(doc_id, str) or not isinstance(chunk_id, str):
                continue
            full_id = f"{doc_id}:{chunk_id}"
            scores[full_id] = scores.get(full_id, 0.0) + 1.0 / (rrf_k + rank)
            current = richest.get(full_id)
            if current is None or _citation_richness(hit) > _citation_richness(current):
                richest[full_id] = hit

    ordered_ids = sorted(scores, key=lambda fid: (-scores[fid], fid))
    results: list[dict[str, object]] = []
    for full_id in ordered_ids[:k]:
        rec = dict(richest[full_id])
        rec["score"] = scores[full_id]
        results.append(rec)
    return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_retrieval_fuse.py -v`
Expected: PASS (all 10).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
git add backend/src/fdi/core/retrieval.py backend/tests/unit/test_retrieval_fuse.py
git commit -m "feat(core): retrieval.fuse -- Reciprocal Rank Fusion over vector+keyword hits"
```

---

### Task 2: `fdi.pipeline.retrieval.hybrid_search` (orchestrator)

**Files:**
- Modify: `backend/src/fdi/pipeline/retrieval.py` (append
  `_FUSE_OVERFETCH_FACTOR`, `hybrid_search`; extend the `fdi.core.retrieval`
  import to include `fuse`)
- Test: `backend/tests/integration/test_hybrid_search.py`

**Interfaces:**
- Consumes: `vector_search`, `keyword_search` (existing, same module);
  `fuse` (Task 1).
- Produces: `hybrid_search(*, session: AsyncSession, store: ArtifactStore,
  embedder: EmbeddingClient | None, vector_store: VectorStore | None, query:
  str, k: int, filters: Mapping[str, object] | None = None) ->
  list[dict[str, object]]` — Task 3's route calls this.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/integration/test_hybrid_search.py
"""hybrid_search: RRF fusion of vector_search (RT-2) + keyword_search (RT-3)
over the SAME corpus, indexed into BOTH FakeVectorStore and the `chunk`
table. Against real Postgres + MinIO with FAKE OCR/semantic/embedding/
vector-store clients -- no BGE/Milvus in the default suite."""

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
from fdi.pipeline.retrieval import hybrid_search, index_chunks_fts
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
    """Records every `k` argument `search` was called with, to pin
    hybrid_search's fetch_k = k * _FUSE_OVERFETCH_FACTOR wiring."""

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
async def hybrid_corpus(
    session_factory, store
) -> tuple[dict[str, dict[str, str]], FakeEmbeddingClient, FakeVectorStore, str]:
    """Two fully-processed, DOUBLY-indexed documents (Milvus fake AND the
    Postgres `chunk` table):
    - "bs" (doc_key "alpha"): "Balance Sheet\\nTotal Assets 100 {marker}"
      (BALANCE_SHEET). `marker` is a fresh uuid4 hex token embedded in the
      OCR text so a query built around it matches ONLY this fixture's chunk
      under plainto_tsquery's AND-of-lexemes semantics -- the `chunk` table
      is session-scoped across the WHOLE integration suite, and several
      other fixtures already use the literal, unmarked "Balance Sheet" /
      "Total Assets 100" text.
    - "notes" (doc_key "beta"): "Notes\\nSee appendix" (NOTES) -- shares no
      vocabulary with the marked query, so it can never satisfy the marked
      keyword query's AND clause; it exists to prove vector-only /
      section_type-filtered behavior.
    Returns ({"bs": {...}, "notes": {...}}, embedder, vector_store, marker).
    The FakeVectorStore is a fresh, per-fixture instance -- unlike Postgres,
    it carries NO cross-test collision risk, so only the keyword side needs
    the marker treatment.
    """
    reg = StageRegistry()
    for stage in (
        OcrStage(), NormalizeStage(), AstStage(), SemanticStage(), GraphStage(),
        ProvenanceStage(), ChunksStage(), EmbeddingStage(), IndexStage(),
    ):
        reg.register(stage)
    resolver = DagResolver(reg.all())

    embedder = FakeEmbeddingClient(dim=8)
    vector_store = FakeVectorStore()
    marker = uuid.uuid4().hex

    specs = {
        "bs": (
            "alpha",
            _fake_ocr([
                {"role": "sectionHeading", "content": "Balance Sheet"},
                {"content": f"Total Assets 100 {marker}"},
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
            filename=f"{key}.pdf", doc_key=doc_key, content_type="application/pdf",
            session_factory=session_factory, store=store,
        )
        common = {
            "doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
            "store": store, "registry": reg, "resolver": resolver,
        }
        await run_stage(
            stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake_ocr}, **common
        )
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
        async with session_factory() as s:
            await index_chunks_fts(session=s, store=store, doc_id=doc.id)
            await s.commit()
        out[key] = {"doc_id": doc.id, "doc_key": doc_key}

    return out, embedder, vector_store, marker


async def test_hybrid_search_ranks_chunk_found_by_both_tools_first(
    session_factory, store, hybrid_corpus
) -> None:
    docs, embedder, vector_store, marker = hybrid_corpus
    query = f"Balance Sheet\nTotal Assets 100 {marker}"
    async with session_factory() as s:
        out = await hybrid_search(
            session=s, store=store, embedder=embedder, vector_store=vector_store,
            query=query, k=2, filters=None,
        )
    assert out
    assert (out[0]["doc_id"], out[0]["chunk_id"]) == (docs["bs"]["doc_id"], "s0")
    assert len(out) <= 2


async def test_hybrid_search_section_type_filter_applies_to_both_tools(
    session_factory, store, hybrid_corpus
) -> None:
    docs, embedder, vector_store, marker = hybrid_corpus
    query = f"Balance Sheet\nTotal Assets 100 {marker}"
    async with session_factory() as s:
        out = await hybrid_search(
            session=s, store=store, embedder=embedder, vector_store=vector_store,
            query=query, k=5, filters={"section_type": "NOTES"},
        )
    assert out
    assert all(r["section_type"] == "NOTES" for r in out)
    assert all(r["doc_id"] != docs["bs"]["doc_id"] for r in out)


async def test_hybrid_search_degrades_to_keyword_only_when_vector_unconfigured(
    session_factory, store, hybrid_corpus
) -> None:
    docs, _embedder, _vector_store, marker = hybrid_corpus
    query = f"Total Assets 100 {marker}"
    async with session_factory() as s:
        out = await hybrid_search(
            session=s, store=store, embedder=None, vector_store=None,
            query=query, k=5, filters=None,
        )
    assert out
    assert out[0]["doc_id"] == docs["bs"]["doc_id"] and out[0]["chunk_id"] == "s0"
    assert out[0]["score"] == pytest.approx(1 / 61)


async def test_hybrid_search_overfetches_by_the_fuse_factor(session_factory, store) -> None:
    """Pins fetch_k = k * _FUSE_OVERFETCH_FACTOR against a spy, independent
    of hybrid_corpus' size."""
    embedder = FakeEmbeddingClient(dim=8)
    spy = _SpyVectorStore()
    async with session_factory() as s:
        await hybrid_search(
            session=s, store=store, embedder=embedder, vector_store=spy,
            query="q", k=4, filters=None,
        )
    assert spy.search_k_calls == [12]


async def test_hybrid_search_empty_query_over_empty_corpus_returns_empty(
    session_factory, store
) -> None:
    embedder = FakeEmbeddingClient(dim=8)
    vector_store = FakeVectorStore()
    async with session_factory() as s:
        out = await hybrid_search(
            session=s, store=store, embedder=embedder, vector_store=vector_store,
            query="", k=5, filters=None,
        )
    assert out == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_hybrid_search.py -v`
Expected: FAIL — `ImportError: cannot import name 'hybrid_search' from 'fdi.pipeline.retrieval'`.

- [ ] **Step 3: Implement**

In `backend/src/fdi/pipeline/retrieval.py`, change the existing import line

```python
from fdi.core.retrieval import build_vector_results, chunk_rows_for_index
```

to

```python
from fdi.core.retrieval import build_vector_results, chunk_rows_for_index, fuse
```

Append at the end of the file:

```python
_FUSE_OVERFETCH_FACTOR = 3


async def hybrid_search(
    *,
    session: AsyncSession,
    store: ArtifactStore,
    embedder: EmbeddingClient | None,
    vector_store: VectorStore | None,
    query: str,
    k: int,
    filters: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    """RT-4's hybrid retrieval orchestrator: fetch k*_FUSE_OVERFETCH_FACTOR
    from RT-3's keyword_search (always -- Postgres is mandatory config) and,
    if BOTH embedder and vector_store are configured, from RT-2's
    vector_search over the SAME query/filters; fuse via
    fdi.core.retrieval.fuse and truncate to k. Degrades to keyword-only
    (never raises, never needs a 503) when embedder/vector_store is None --
    design spec sec 2 decision 2. Pipeline layer: imports platform + core,
    no I/O left inline -- fusion itself lives in fdi.core.retrieval, pure
    and independently unit-tested."""
    fetch_k = k * _FUSE_OVERFETCH_FACTOR
    section_type = filters.get("section_type") if filters else None
    doc_key = filters.get("doc_key") if filters else None
    version = filters.get("version") if filters else None

    keyword_hits = await keyword_search(
        session=session,
        query=query,
        k=fetch_k,
        section_type=cast(str | None, section_type),
        doc_key=cast(str | None, doc_key),
        version=cast(int | None, version),
    )

    vector_hits: list[dict[str, object]] = []
    if embedder is not None and vector_store is not None:
        vector_hits = await vector_search(
            session=session,
            store=store,
            embedder=embedder,
            vector_store=vector_store,
            query=query,
            k=fetch_k,
            filters=filters,
        )

    return fuse(vector_hits, keyword_hits, k=k)
```

(`EmbeddingClient`, `VectorStore`, `cast`, `Mapping`, `AsyncSession`,
`ArtifactStore` are all already imported in this module by RT-2/RT-3 — no
new imports besides the `fuse` addition above.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_hybrid_search.py -v`
Expected: PASS (all 5).

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/pipeline/retrieval.py backend/tests/integration/test_hybrid_search.py
git commit -m "feat(pipeline): hybrid_search -- RT-2+RT-3 fused via RRF, degrades to keyword-only"
```

---

### Task 3: `POST /retrieve` (hybrid route + schemas)

**Files:**
- Modify: `backend/src/fdi/api/schemas.py` (add `HybridSearchFilters`,
  `HybridSearchRequest`, `HybridSearchResultView`, `HybridSearchResultsView`)
- Modify: `backend/src/fdi/api/routes.py` (import `hybrid_search` +
  the new schemas; add the `POST /retrieve` route)
- Test: `backend/tests/integration/test_api.py` (append a new RT-4 section)

**Interfaces:**
- Consumes: `hybrid_search` (Task 2); `EmbedderDep`/`VectorStoreDep`/
  `SessionDep`/`StoreDep` (existing `fdi.api.deps`).
- Produces: `POST /retrieve` — `200` always (never `503`), body
  `{results: [{doc_id, doc_key, version, chunk_id, heading, page,
  section_type, text, score}]}`.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/integration/test_api.py`, after the existing
`# RT-2: POST /retrieve/vector ...` section's tests (i.e. immediately before
`# Task 6 (SP-16): POST /entities/resolve`):

```python
# --------------------------------------------------------------------------
# RT-4: POST /retrieve -- hybrid (RRF) retrieval. Unlike /retrieve/vector,
# NEVER 503s: degrades to keyword-only when embedder/vector_store are
# unconfigured (design spec sec 2 decision 2).
# --------------------------------------------------------------------------


async def _seed_hybrid_doc(session_factory, store, *, marker: str) -> dict[str, object]:
    """One fully-processed, FTS-indexed document whose body text embeds a
    fresh, globally-unique `marker` token -- Postgres' `chunk` table is
    session-scoped across the WHOLE integration suite, and several other
    fixtures in this file already use the literal, unmarked "Balance
    Sheet"/"Total Assets 100" text; without a marker a keyword query for
    that phrase would non-deterministically compete with unrelated rows for
    a place inside hybrid_search's over-fetch window."""
    from fdi.core.semantic import SectionType
    from fdi.pipeline.intake import intake_document
    from fdi.pipeline.ocr import FakeOcrClient
    from fdi.pipeline.registry import StageRegistry
    from fdi.pipeline.resolver import DagResolver
    from fdi.pipeline.retrieval import index_chunks_fts
    from fdi.pipeline.run import run_stage
    from fdi.pipeline.semantic import FakeSemanticClient
    from fdi.pipeline.stages.ast import AstStage
    from fdi.pipeline.stages.chunks import ChunksStage
    from fdi.pipeline.stages.graph import GraphStage
    from fdi.pipeline.stages.normalize import NormalizeStage
    from fdi.pipeline.stages.ocr import OcrStage
    from fdi.pipeline.stages.provenance import ProvenanceStage
    from fdi.pipeline.stages.semantic import SemanticStage

    doc_key = f"hybrid-{uuid.uuid4()}"
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
            {"content": f"Total Assets 100 {marker}"},
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
    await run_stage(
        stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake_ocr}, **common
    )
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="semantic", run_id=str(uuid.uuid4()),
        services={"semantic": fake_semantic}, **common,
    )
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)

    async with session_factory() as s:
        await index_chunks_fts(session=s, store=store, doc_id=doc.id)
        await s.commit()
    return {"doc_id": doc.id, "doc_key": doc_key}


async def test_hybrid_route_never_503s_without_vector_configured(
    client: AsyncClient, _alembic_applied, session_factory, store
) -> None:
    marker = uuid.uuid4().hex
    seeded = await _seed_hybrid_doc(session_factory, store, marker=marker)
    r = await client.post(
        "/retrieve", json={"query": f"Total Assets 100 {marker}", "k": 5}
    )
    assert r.status_code == 200, r.text
    ours = [h for h in r.json()["results"] if h["doc_id"] == seeded["doc_id"]]
    assert ours and ours[0]["chunk_id"] == "s0"
    assert ours[0]["score"] == pytest.approx(1 / 61)  # keyword-only: sole rank-1 match


async def test_hybrid_route_returns_empty_results_for_no_match(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.post("/retrieve", json={"query": "zzzznonexistenttoken9999", "k": 5})
    assert r.status_code == 200
    assert r.json()["results"] == []


async def test_hybrid_route_fuses_vector_and_keyword_results(
    make_client, _alembic_applied, session_factory, store
) -> None:
    from fdi.api.deps import get_embedder, get_vector_store
    from fdi.pipeline.embeddings import FakeEmbeddingClient
    from fdi.pipeline.vectorstore import FakeVectorStore

    marker = uuid.uuid4().hex
    seeded = await _seed_hybrid_doc(session_factory, store, marker=marker)
    query = f"Balance Sheet\nTotal Assets 100 {marker}"
    fake_embedder = FakeEmbeddingClient(dim=8)
    fake_store = FakeVectorStore()
    fake_store.upsert([{
        "id": f"{seeded['doc_id']}:s0",
        "vector": fake_embedder.embed([query])[0],
        "metadata": {"heading": "Balance Sheet", "chunk_id": "s0",
                      "section_type": "BALANCE_SHEET", "page": 1},
    }])

    async with make_client(
        dependency_overrides={
            get_embedder: lambda: fake_embedder,
            get_vector_store: lambda: fake_store,
        }
    ) as client:
        r = await client.post("/retrieve", json={"query": query, "k": 5})

    assert r.status_code == 200, r.text
    ours = [h for h in r.json()["results"] if h["doc_id"] == seeded["doc_id"]]
    assert ours
    assert ours[0]["chunk_id"] == "s0"
    assert ours[0]["doc_key"] == seeded["doc_key"]
    # found by BOTH tools -> fused score is the sum of two RRF terms, > keyword-only alone
    assert ours[0]["score"] > 1 / 61


async def test_hybrid_route_section_type_filter_reaches_both_tools(
    make_client, _alembic_applied, session_factory, store
) -> None:
    from fdi.api.deps import get_embedder, get_vector_store
    from fdi.pipeline.embeddings import FakeEmbeddingClient
    from fdi.pipeline.vectorstore import FakeVectorStore

    marker = uuid.uuid4().hex
    seeded = await _seed_hybrid_doc(session_factory, store, marker=marker)
    query = f"Balance Sheet\nTotal Assets 100 {marker}"
    fake_embedder = FakeEmbeddingClient(dim=8)
    fake_store = FakeVectorStore()
    fake_store.upsert([{
        "id": f"{seeded['doc_id']}:s0",
        "vector": fake_embedder.embed([query])[0],
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
            "/retrieve",
            json={"query": query, "k": 5, "filters": {"section_type": "NOTES"}},
        )

    assert r.status_code == 200
    # Only ONE vector is indexed in this test (the BALANCE_SHEET chunk), and
    # the marker guarantees no other row anywhere in the shared Postgres
    # session can match the keyword query -- a NOTES filter must therefore
    # empty BOTH lists, not merely exclude our doc_id from a nonempty result.
    assert r.json()["results"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/integration/test_api.py -k hybrid_route -v`
Expected: FAIL — `404 Not Found` (`/retrieve` doesn't exist yet) /
`ImportError` once schemas are referenced.

- [ ] **Step 3: Implement**

In `backend/src/fdi/api/schemas.py`, add after `KeywordSearchResultsView`:

```python
class HybridSearchFilters(BaseModel):
    section_type: str | None = None
    doc_key: str | None = None
    version: int | None = None


class HybridSearchRequest(BaseModel):
    query: str
    k: int = 5
    filters: HybridSearchFilters | None = None


class HybridSearchResultView(BaseModel):
    doc_id: str
    doc_key: str | None
    version: int | None
    chunk_id: str
    heading: str | None
    page: int | None
    section_type: str | None
    text: str
    score: float


class HybridSearchResultsView(BaseModel):
    results: list[HybridSearchResultView]
```

In `backend/src/fdi/api/routes.py`:

1. Extend the existing multi-line `from fdi.api.schemas import (...)` block
   in `routes.py`, alphabetically, with `HybridSearchRequest`,
   `HybridSearchResultView`, `HybridSearchResultsView` — the three names
   `routes.py` references directly. `HybridSearchFilters` is NOT imported
   here: it is only used as `HybridSearchRequest.filters`' field type inside
   `schemas.py` itself, never named directly in `routes.py` (same pattern
   `VectorSearchFilters`/`KeywordSearchRequest`'s filter fields already
   follow — grep `VectorSearchFilters` in `routes.py` to confirm it isn't
   imported there either before assuming this is an oversight).
2. Extend the existing retrieval import line

```python
from fdi.pipeline.retrieval import index_chunks_fts, keyword_search, vector_search
```

   to

```python
from fdi.pipeline.retrieval import (
    hybrid_search,
    index_chunks_fts,
    keyword_search,
    vector_search,
)
```

3. Add the route, placed immediately after the RT-3 section (after
   `keyword_search_route`, before the `# RT-5: graph retrieval` comment
   block):

```python
# --------------------------------------------------------------------------
# RT-4: POST /retrieve -- hybrid (RRF) retrieval over RT-2's vector_search +
# RT-3's keyword_search. Unlike /retrieve/vector, this route has NO
# 503-when-unconfigured branch: keyword_search has no external dependency
# (Postgres is mandatory config, RT-3 sec 2 decision 9), so hybrid_search
# degrades to keyword-only when embedder/vector_store are unconfigured
# rather than failing the whole request -- design spec sec 2 decision 2.
# --------------------------------------------------------------------------


@router.post("/retrieve", response_model=HybridSearchResultsView)
async def hybrid_search_route(
    body: HybridSearchRequest,
    session: SessionDep,
    store: StoreDep,
    embedder: EmbedderDep,
    vector_store: VectorStoreDep,
) -> HybridSearchResultsView:
    filters = {
        "section_type": body.filters.section_type if body.filters else None,
        "doc_key": body.filters.doc_key if body.filters else None,
        "version": body.filters.version if body.filters else None,
    }
    results = await hybrid_search(
        session=session,
        store=store,
        embedder=embedder,
        vector_store=vector_store,
        query=body.query,
        k=body.k,
        filters=filters,
    )
    return HybridSearchResultsView(
        results=[HybridSearchResultView.model_validate(r) for r in results]
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/integration/test_api.py -k hybrid_route -v`
Expected: PASS (all 4). Then run the whole file to confirm nothing else
broke: `uv run --directory backend pytest tests/integration/test_api.py -v`.

- [ ] **Step 5: Gates + commit**

```bash
uv run --directory backend ruff check .
uv run --directory backend mypy --strict src
PYTHONUTF8=1 uv run --directory backend lint-imports --config .importlinter
git add backend/src/fdi/api/schemas.py backend/src/fdi/api/routes.py backend/tests/integration/test_api.py
git commit -m "feat(api): POST /retrieve -- hybrid RRF search, never 503s (keyword-only fallback)"
```

---

### Task 4: Full gates, roadmap + spec status

**Files:**
- Modify: `docs/superpowers/specs/2026-07-18-rt4-hybrid-retrieval-design.md`
  (Status → Implemented)
- Modify: `docs/architecture/retrieval-roadmap.md` (RT-4 row → done + "what
  RT-4 shipped" handoff)

- [ ] **Step 1: Full static gates** — `ruff check .`, `mypy --strict src`,
  `PYTHONUTF8=1 lint-imports` (1 kept/0 broken). Grep-confirm
  `fdi.core.retrieval` still imports neither `fdi.platform.*` nor
  `fdi.pipeline.*` after Task 1's addition.

- [ ] **Step 2: Full suite** — `uv run --directory backend pytest -q`;
  green (e2e/azure/neo4j/milvus/bge skip). Note the passed count against the
  prior total (RT-3's handoff recorded 371 passed, 8 skipped after RT-2; RT-5
  and RT-6 landed since — read the roadmap's current numbers before
  computing the delta, don't assume RT-2's count is still the baseline).

- [ ] **Step 3: Audit RT-4 done criteria** (§7 of the design spec) — map
  each to the tasks; confirm: the exact-formula RRF score assertions
  (Task 1), the tie-break assertion (Task 1), the richest-citation-dedup
  assertion (Task 1), the keyword-always/vector-if-configured degradation
  (Task 2's `test_hybrid_search_degrades_to_keyword_only_when_vector_unconfigured`),
  the over-fetch-factor pin (Task 2's spy test), and the never-503 HTTP
  assertion against the exact unconfigured condition `/search`/
  `/retrieve/vector` DO 503 on (Task 3's
  `test_hybrid_route_never_503s_without_vector_configured`) all pass.

- [ ] **Step 4: Spec status → Implemented 2026-07-18** (only after Steps
  1–3 green).

- [ ] **Step 5: Roadmap** — flip the RT-4 row to `✅ done`; add a "What RT-4
  shipped that RT-7/RT-8 must know" handoff covering: the `fuse` RRF formula
  and its two constants (`_RRF_K = 60`, `_FUSE_OVERFETCH_FACTOR = 3`, both
  private, both flagged as revisit-later-with-real-corpus-data, not tunable
  settings); the keyword-always/vector-if-configured degradation policy and
  why `POST /retrieve` has no 503 branch (a deliberate divergence from
  `/search`/`/retrieve/vector`'s precedent); the richest-citation dedup rule
  (count of non-None optional fields, argument-order tie-break); and the
  flagged-but-not-built `degraded`/`sources_used` response field fork for a
  human or RT-8's agent design to pick up if callers need to distinguish a
  keyword-only answer from a fully-fused one without inspecting score
  magnitudes.

- [ ] **Step 6: Commit**

```bash
git add docs
git commit -m "docs: RT-4 done-criteria audit, spec status, roadmap handoff"
```

---

## Self-Review

**Spec coverage:** §3.1 `fuse` → Task 1 (RRF sum, tie-break, richest-citation
dedup, skip-malformed all unit-tested). §3.2 `hybrid_search` → Task 2
(over-fetch, degrade-when-unconfigured, filter pass-through all
integration-tested). §3.3–3.4 schemas + route → Task 3. §2 decisions:
identity-computed-not-read (d1) → Task 1's skip-malformed tests; degradation
policy (d2) → Task 2's degrade test + Task 3's never-503 test; richest-
citation dedup (d3) → Task 1's dedup + tie tests; `rrf_k=60` constant (d4) →
Task 1's default-vs-custom test; over-fetch factor `3` (d5) → Task 2's spy
test; filters pass-through (d6) → Task 2's section_type-filter test + Task
3's HTTP filter test; fused score is a new unit (d7) → documented in the
design spec, exercised implicitly (Task 3's fuses-both test asserts `score >
1/61`, i.e. NOT equal to either raw tool score); no new route for `fuse`
alone (d8), no new config (d9) → nothing to test, confirmed by absence.
§4 error handling → Task 1's empty/malformed cases, Task 2's Postgres-
failure-propagates note (not separately tested — same posture RT-3 already
established and did not test either). §5 testing → Tasks 1–3. §6 deferred →
not built (Task 4 audit + roadmap handoff surfaces the flagged fork). §7
done criteria → Task 4.

**Placeholder scan:** every step in Tasks 1–3 carries complete, runnable
code — no "TBD", no "similar to Task N" without the code repeated in full
(Task 3's `_seed_hybrid_doc` is written out completely, not referenced by
name from `_seed_processed_doc`, precisely because it needs the marker
treatment `_seed_processed_doc` doesn't have). Task 2/3's fixtures are
adapted, not merely pointed at, from `test_vector_search.py`'s `two_docs`
and `test_api.py`'s `_seed_processed_doc` — the adaptations (marker
embedding, `index_chunks_fts` call) are spelled out in full, not left as an
exercise.

**Type consistency:** `fuse(vector_hits: list[Mapping[str, object]],
keyword_hits: list[Mapping[str, object]], *, k: int, rrf_k: int = 60) ->
list[dict[str, object]]` (Task 1) is consumed unchanged by `hybrid_search`
(Task 2), which itself has the exact signature `hybrid_search(*, session:
AsyncSession, store: ArtifactStore, embedder: EmbeddingClient | None,
vector_store: VectorStore | None, query: str, k: int, filters:
Mapping[str, object] | None = None) -> list[dict[str, object]]` that Task
3's route calls with `embedder`/`vector_store` taken straight from
`EmbedderDep`/`VectorStoreDep` (already `T | None` in `fdi.api.deps`, no
new Optional-handling invented at the route layer). The nine-key hit shape
(`doc_id, doc_key, version, chunk_id, heading, page, section_type, text,
score`) is identical across `vector_search`'s output, `keyword_search`'s
output, `fuse`'s output, and `HybridSearchResultView`'s fields — verified
by inspection against the actual `backend/src/fdi/pipeline/retrieval.py`
and `backend/src/fdi/api/schemas.py` read before writing this plan, not
assumed from the design spec alone.

**Risk flagged:** the Postgres session-scoping collision risk (Global
Constraints, called out explicitly) is real and already documented
in-repo (`test_keyword_search.py`'s own comment) — every new integration
test that does an exact-match keyword assertion in this plan uses a fresh
`uuid.uuid4().hex` marker for exactly this reason; a reviewer should treat
a future RT-4 test WITHOUT a marker, that asserts rank-1 position on an
unmarked literal phrase, as a latent flake, not a style nitpick.
