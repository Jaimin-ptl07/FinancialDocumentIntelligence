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
    """Records every `k` argument `search` was called with, to pin the
    over-fetch factor without depending on corpus size."""

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
        ProvenanceStage(), StructureStage(), BasisStage(), ChunksStage(),
        EmbeddingStage(), IndexStage(),
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

    return out, embedder, vector_store


async def test_vector_search_no_filters_returns_both_docs(session_factory, store, two_docs) -> None:
    docs, embedder, vector_store = two_docs
    async with session_factory() as s:
        out = await vector_search(
            session=s, embedder=embedder, vector_store=vector_store,
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
            session=s, embedder=embedder, vector_store=vector_store,
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
            session=s, embedder=embedder, vector_store=vector_store,
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
            session=s, embedder=embedder, vector_store=vector_store,
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
            session=s, embedder=embedder, vector_store=spy,
            query="q", k=3, filters=None,
        )
        await vector_search(
            session=s, embedder=embedder, vector_store=spy,
            query="q", k=3, filters={"section_type": "NOTES"},
        )
    assert spy.search_k_calls == [3, 15]
