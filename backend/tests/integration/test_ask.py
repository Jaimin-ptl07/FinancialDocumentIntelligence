"""answer_question end-to-end: a scripted FakeAgentClient calling ONE real
tool (search -> hybrid_search) against real Postgres/MinIO with
FakeOcr/FakeSemantic/FakeEmbedding/FakeVectorStore -- NO Azure OpenAI call
anywhere in this file. Reuses test_hybrid_search.py's hybrid_corpus-style
build (fresh uuid4 marker in OCR text to dodge the session-scoped-DB
collision class every other RT integration test already guards against)."""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.agent import AgentResponse, ToolCall
from fdi.core.pricing import LlmCallUsage
from fdi.pipeline.agent import FakeAgentClient
from fdi.pipeline.agent_tools import AgentContext
from fdi.pipeline.ask import answer_question
from fdi.pipeline.embeddings import FakeEmbeddingClient
from fdi.pipeline.intake import intake_document
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
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


@pytest.fixture
async def marked_doc(stub_fdi_env: None, session_factory, store):
    """One fully-processed, doubly-indexed document with a fresh uuid4
    marker embedded in its text (Balance Sheet / Total Assets), mirroring
    test_hybrid_search.py's hybrid_corpus fixture but for ONE document.

    stub_fdi_env: run_stage's OCR-billing path (costing feature) reads
    get_settings().azure_di_model, which needs the six required FDI_ vars
    stubbed -- the autouse _isolate_fdi_env fixture clears them for every
    test otherwise. Same fixture, same reason, as test_ocr_stage.py."""
    marker = uuid.uuid4().hex
    reg = StageRegistry()
    for stage in (OcrStage(), NormalizeStage(), AstStage(), SemanticStage(), GraphStage(),
                  ProvenanceStage(), StructureStage(), BasisStage(), ChunksStage(),
                  EmbeddingStage(), IndexStage()):
        reg.register(stage)
    resolver = DagResolver(reg.all())
    embedder = FakeEmbeddingClient(dim=8)
    vector_store = FakeVectorStore()

    fake_ocr = FakeOcrClient({
        "content": "ACME",
        "pages": [{"pageNumber": 1, "lines": [], "words": []}],
        "paragraphs": [
            {"role": "sectionHeading", "content": "Balance Sheet"},
            {"content": f"Total Assets 100 {marker}"},
        ],
        "tables": [],
    })
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(), filename="bs.pdf",
        doc_key=f"acme-{marker}", content_type="application/pdf",
        session_factory=session_factory, store=store,
    )
    common = {"doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
              "store": store, "registry": reg, "resolver": resolver}
    await run_stage(
        stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake_ocr}, **common
    )
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="semantic", run_id=str(uuid.uuid4()),
        services={"semantic": FakeSemanticClient({"Balance Sheet": "BALANCE_SHEET"})}, **common,
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
    return doc, marker, embedder, vector_store


async def test_answer_question_calls_one_real_tool_then_synthesizes_cited_answer(
    session_factory, store, marked_doc
) -> None:
    doc, marker, embedder, vector_store = marked_doc
    query = f"Total Assets 100 {marker}"
    fake = FakeAgentClient([
        AgentResponse(tool_calls=[ToolCall(id="c1", name="search", arguments={"query": query})]),
        AgentResponse(content=f"Total Assets were 100 (see {doc.id}:s0)."),
    ])
    async with session_factory() as s:
        ctx = AgentContext(session=s, store=store, embedder=embedder, vector_store=vector_store,
                            graph_store=None)
        result = await answer_question(
            question=f"What were total assets, marker {marker}?",
            ctx=ctx, agent_client=fake, max_rounds=5,
        )

    assert result["answer"] == f"Total Assets were 100 (see {doc.id}:s0)."
    assert len(fake.calls) == 2
    assert len(result["tool_trace"]) == 1
    assert result["tool_trace"][0]["tool"] == "search"
    assert result["tool_trace"][0]["error"] is None
    citations = result["citations"]
    assert any(c["doc_id"] == doc.id and c["chunk_id"] == "s0" for c in citations)


async def test_answer_question_exhausts_max_rounds_gracefully(session_factory, store) -> None:
    always_tool_calls = [
        AgentResponse(tool_calls=[ToolCall(id=f"c{i}", name="search",
                                            arguments={"query": "zzzznonexistenttoken9999"})])
        for i in range(3)
    ]
    fake = FakeAgentClient(always_tool_calls)
    async with session_factory() as s:
        ctx = AgentContext(session=s, store=store, embedder=None, vector_store=None,
                            graph_store=None)
        result = await answer_question(
            question="anything", ctx=ctx, agent_client=fake, max_rounds=3,
        )
    assert "3 tool-call round" in result["answer"]
    assert result["citations"] == []
    assert len(result["tool_trace"]) == 3


async def test_ask_records_one_llm_usage_event_per_round(
    session_factory, store, marked_doc
) -> None:
    """Costing feature (Task 14): answer_question records an llm_usage_event
    row per agent-loop round, both on the tool-call round and on the
    final-content round -- verified corpus-wide via corpus_cost_totals
    rather than a stage_run_id-scoped query, since /ask rows carry no
    stage_run_id (source="ask_agent")."""
    doc, marker, embedder, vector_store = marked_doc
    query = f"Total Assets 100 {marker}"
    usage_a = LlmCallUsage(model="gpt-4o", input_tokens=100, output_tokens=20, duration_ms=300)
    usage_b = LlmCallUsage(model="gpt-4o", input_tokens=150, output_tokens=40, duration_ms=250)
    fake = FakeAgentClient([
        AgentResponse(
            tool_calls=[ToolCall(id="c1", name="search", arguments={"query": query})],
            usage=usage_a,
        ),
        AgentResponse(content=f"Total Assets were 100 (see {doc.id}:s0).", usage=usage_b),
    ])
    async with session_factory() as s:
        ctx = AgentContext(session=s, store=store, embedder=embedder, vector_store=vector_store,
                            graph_store=None, correlation_id="test-corr")
        await answer_question(
            question=f"What were total assets, marker {marker}?",
            ctx=ctx, agent_client=fake, max_rounds=5,
        )
        await s.commit()

    async with session_factory() as s:
        totals = await RegistryRepository(s).corpus_cost_totals()
    assert totals["llm_input_tokens"] >= 250  # 100 + 150
    assert totals["llm_output_tokens"] >= 60   # 20 + 40
