"""hybrid_search: RRF fusion of vector_search (RT-2) + bm25_search (RT-9)
over the SAME corpus, indexed once into a FakeVectorStore (both channels now
share that one seam -- see fdi.pipeline.stages.index's v3 `text` field).
Against real Postgres + MinIO with FAKE OCR/semantic/embedding/vector-store
clients -- no BGE/Milvus in the default suite. `session`/Postgres are still
required: hybrid_search resolves doc_key/version filters to doc_ids there."""

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
from fdi.pipeline.retrieval import hybrid_search
from fdi.pipeline.run import run_stage
from fdi.pipeline.semantic import FakeSemanticClient
from fdi.pipeline.stages.ast import AstStage
from fdi.pipeline.stages.basis import BasisStage
from fdi.pipeline.stages.chunks import ChunksStage
from fdi.pipeline.stages.embedding import EmbeddingStage
from fdi.pipeline.stages.graph import GraphStage
from fdi.pipeline.stages.index import IndexStage
from fdi.pipeline.stages.normalize import NormalizeStage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.pipeline.stages.provenance import ProvenanceStage
from fdi.pipeline.stages.semantic import SemanticStage
from fdi.pipeline.stages.structure import StructureStage
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

    def search(
        self, vector: list[float], k: int, *, filter: str = ""
    ) -> list[dict[str, object]]:
        self.search_k_calls.append(k)
        return super().search(vector, k, filter=filter)


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
    """Two fully-processed documents, indexed into a fresh, per-fixture
    FakeVectorStore that now carries BOTH the vector and the BM25
    (search_text term-overlap) channels:
    - "bs" (doc_key "alpha-{marker}"): "Balance Sheet\\nTotal Assets 100
      {marker}" (BALANCE_SHEET). `marker` is a fresh uuid4 hex token embedded
      in the OCR text, kept for doc_key uniqueness (Postgres' `document`
      table is session-scoped across the WHOLE integration suite, and
      intake_document allocates the NEXT version per doc_key -- a shared
      literal doc_key here would collide with other fixtures' expected
      version==1 slot). The FakeVectorStore instance itself is fresh per
      fixture, so it carries no cross-test collision risk on its own.
    - "notes" (doc_key "beta-{marker}"): "Notes\\nSee appendix" (NOTES) --
      shares no vocabulary with the marked query, so bm25_search's
      zero-overlap-is-omitted semantics can never return it for that query;
      it exists to prove vector-only / section_type-filtered behavior.
    Returns ({"bs": {...}, "notes": {...}}, embedder, vector_store, marker).
    """
    reg = StageRegistry()
    for stage in (
        OcrStage(), NormalizeStage(), AstStage(), SemanticStage(), GraphStage(),
        ProvenanceStage(), StructureStage(), BasisStage(), ChunksStage(),
        EmbeddingStage(), IndexStage(),
    ):
        reg.register(stage)
    resolver = DagResolver(reg.all())

    embedder = FakeEmbeddingClient(dim=8)
    vector_store = FakeVectorStore()
    marker = uuid.uuid4().hex

    # doc_key is deliberately NOT the literal "alpha"/"beta" test_vector_search.py's
    # own two_docs fixture uses: Postgres' `document` table is session-scoped
    # across the WHOLE integration suite (same collision class the module
    # docstring's marker treatment covers for chunk TEXT), and intake_document
    # allocates the NEXT version per doc_key -- a shared literal doc_key here
    # would silently consume two_docs' expected version==1 slot depending on
    # file collection order. `marker` (already a fresh uuid4 hex) doubles as
    # the doc_key uniqueness source.
    specs = {
        "bs": (
            f"alpha-{marker}",
            _fake_ocr([
                {"role": "sectionHeading", "content": "Balance Sheet"},
                {"content": f"Total Assets 100 {marker}"},
            ]),
            {"Balance Sheet": SectionType.BALANCE_SHEET.value},
        ),
        "notes": (
            f"beta-{marker}",
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
        await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
        await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
        await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)
        await run_stage(stage_name="basis", run_id=str(uuid.uuid4()), services={}, **common)
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

    return out, embedder, vector_store, marker


async def test_hybrid_search_ranks_chunk_found_by_both_tools_first(
    session_factory, store, hybrid_corpus
) -> None:
    docs, embedder, vector_store, marker = hybrid_corpus
    query = f"Balance Sheet\nTotal Assets 100 {marker}"
    async with session_factory() as s:
        out = await hybrid_search(
            session=s, embedder=embedder, vector_store=vector_store,
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
            session=s, embedder=embedder, vector_store=vector_store,
            query=query, k=5, filters={"section_type": "NOTES"},
        )
    assert out
    assert all(r["section_type"] == "NOTES" for r in out)
    assert all(r["doc_id"] != docs["bs"]["doc_id"] for r in out)


async def test_hybrid_search_overfetches_by_the_fuse_factor(session_factory, store) -> None:
    """Pins fetch_k = k * _FUSE_OVERFETCH_FACTOR against a spy, independent
    of hybrid_corpus' size."""
    embedder = FakeEmbeddingClient(dim=8)
    spy = _SpyVectorStore()
    async with session_factory() as s:
        await hybrid_search(
            session=s, embedder=embedder, vector_store=spy,
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
            session=s, embedder=embedder, vector_store=vector_store,
            query="", k=5, filters=None,
        )
    assert out == []
