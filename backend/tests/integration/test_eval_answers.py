"""The answer-quality harness end to end: `run_eval` drives the `/ask`
adapter against the in-process app over HTTP -- login, submit, poll
`/jobs/{id}`, read `/result` -- with a scripted FakeAgentClient whose first
answer comes after one REAL `search` tool call over fixture artifacts
(test_ask.py's pattern). NO Azure call anywhere in this file.

No Celery worker consumes the test broker, so the adapter's poll wait is
where the test plays the worker: it runs every queued `ask` job eagerly,
exactly as test_jobs.py does after a submit."""

import contextlib
import json
import sys
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from fdi.core.agent import AgentResponse, ToolCall
from fdi.core.benchmark import parse_benchmark
from fdi.pipeline.agent import FakeAgentClient
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
from fdi.platform.config import get_settings
from fdi.platform.db.engine import make_session_factory
from fdi.platform.storage.artifact_store import ArtifactStore
from tests.conftest import TEST_SIGNING_SECRET
from tests.integration.conftest import job_services

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from eval_answers import AskJobAdapter, run_eval  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture
def session_factory(migrated_engine):
    return make_session_factory(migrated_engine)


@pytest.fixture
async def store(minio_client):
    """The APP's bucket, so what the eager runner writes the app reads."""
    s = ArtifactStore(minio_client, bucket="artifacts")
    await s.ensure_bucket()
    return s


@pytest.fixture
async def indexed_doc(session_factory, store):
    """One fully-processed, indexed document whose text states Total Assets
    100 next to a fresh uuid4 marker (the session-scoped DB is shared)."""
    marker = uuid.uuid4().hex
    reg = StageRegistry()
    for stage in (OcrStage(), NormalizeStage(), AstStage(), SemanticStage(), GraphStage(),
                  ProvenanceStage(), StructureStage(), BasisStage(), ChunksStage(),
                  EmbeddingStage(), IndexStage()):
        reg.register(stage)
    embedder = FakeEmbeddingClient(dim=8)
    vector_store = FakeVectorStore()
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(), filename="bs.pdf",
        doc_key=f"acme-{marker}", content_type="application/pdf",
        session_factory=session_factory, store=store,
    )
    common = {"doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
              "store": store, "registry": reg, "resolver": DagResolver(reg.all())}
    services = {
        "ocr": {"ocr": FakeOcrClient({
            "content": "ACME",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            # On page 1, so the search hit cites the item's gold evidence page.
            "paragraphs": [
                {"role": "sectionHeading", "content": "Balance Sheet",
                 "boundingRegions": [{"pageNumber": 1, "polygon": []}]},
                {"content": f"Total Assets 100 {marker}",
                 "boundingRegions": [{"pageNumber": 1, "polygon": []}]},
            ],
            "tables": [],
        })},
        "semantic": {"semantic": FakeSemanticClient({"Balance Sheet": "BALANCE_SHEET"})},
        "embedding": {"embedder": embedder},
        "index": {"vector_store": vector_store},
    }
    for name in ("ocr", "normalize", "ast", "semantic", "structure", "graph", "provenance",
                 "basis", "chunks", "embedding", "index"):
        await run_stage(stage_name=name, run_id=str(uuid.uuid4()),
                        services=services.get(name, {}), **common)
    return doc, marker, embedder, vector_store


@pytest.fixture
def app_factory(postgres_url, minio_endpoint, amqp_url, redis_url, monkeypatch):
    """`create_app` wired to the real containers (test_jobs.py's env)."""
    from fdi.api.app import create_app

    def _make():
        for key, value in {
            "FDI_DATABASE_URL": postgres_url, "FDI_BROKER_URL": amqp_url,
            "FDI_RESULT_BACKEND": redis_url, "FDI_S3_ENDPOINT": minio_endpoint,
            "FDI_S3_ACCESS_KEY": "minioadmin", "FDI_S3_SECRET_KEY": "minioadmin",
            "FDI_AUTH_SECRET": TEST_SIGNING_SECRET,
        }.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        return create_app()

    return _make


def _benchmark(doc_key: str):
    def item(n: int) -> dict[str, object]:
        return {
            "id": f"fact_lookup:acme:q{n}", "kind": "fact_lookup",
            "question": f"What were ACME's total assets? (q{n})", "doc_keys": [doc_key],
            "gold": {"answerable": True,
                     "values": [{"value": 100.0, "unit": "INR crore", "tolerance": 0.5}],
                     "evidence": [{"doc_key": doc_key, "page": 1}]},
        }

    return parse_benchmark({"version": "v-e2e", "items": [item(n) for n in range(4)]})


async def test_run_eval_scores_ask_answers_end_to_end(
    session_factory, store, indexed_doc, invitee, app_factory, tmp_path,
) -> None:
    from fdi.api.deps import get_agent_client, get_embedder, get_vector_store
    from fdi.pipeline.jobs import execute_job

    doc, marker, embedder, vector_store = indexed_doc
    agent = FakeAgentClient([
        # q0: one real search, then the right figure in Indian style.
        AgentResponse(tool_calls=[ToolCall(
            id="c1", name="search", arguments={"query": f"Total Assets 100 {marker}"},
        )]),
        AgentResponse(content="ACME's total assets were ₹100 crore (Balance Sheet, p.1)."),
        # q1: a wrong figure.
        AgentResponse(content="ACME's total assets were ₹90 crore."),
        # q2: right, restated in another scale (1,000 million = 100 crore).
        AgentResponse(content="Total assets: INR 1,000 million."),
        # q3: the script is exhausted -> the job fails -> a failed item.
    ])

    app = app_factory()
    app.dependency_overrides.update({
        get_agent_client: lambda: agent,
        get_embedder: lambda: embedder,
        get_vector_store: lambda: vector_store,
    })

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):

        async def play_worker(_interval: float) -> None:
            queued = (await http.get("/jobs", params={"kind": "ask"})).json()["jobs"]
            for job in queued:
                if job["status"] != "queued":
                    continue
                services = job_services(session_factory, store, agent_client=agent,
                                        embedder=embedder, vector_store=vector_store)
                # A failure is recorded on the job; a worker would only log it.
                with contextlib.suppress(AssertionError):
                    await execute_job(job["job_id"], services)

        adapter = AskJobAdapter(http, poll_interval=0.0, sleep=play_worker)
        await adapter.login(*invitee)
        stamp = {"benchmark_version": "v-e2e", "git_commit": "abc123",
                 "endpoint": "http://test", "mode": "ask"}
        report = await run_eval(
            _benchmark(doc.doc_key), adapter, limit=None, stamp=stamp, out_dir=tmp_path,
        )

    # The real search tool ran over the fixture: its hit went back to the model.
    tool_message = agent.calls[1][0][-1]
    assert tool_message["role"] == "tool"
    assert marker in str(tool_message["content"])

    assert [i.status for i in report.items] == ["correct", "wrong", "correct", "failed"]
    assert "FakeAgentClient exhausted" in report.items[3].reason
    assert (report.overall.run, report.overall.correct) == (4, 2)
    # q0's search hit cites the gold page; q2 is right but cites nothing.
    assert [i.completed for i in report.items] == [True, False, False, False]
    assert report.items[0].valid_citations >= 1
    assert report.items[2].citations == 0
    assert report.overall.completion_rate == 0.25

    body = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert body["stamp"] == stamp
    assert body["benchmark_size"] == 4
    assert body["by_kind"]["fact_lookup"]["accuracy"] == 0.5
    assert body["items"][0]["answer"].startswith("ACME's total assets were ₹100 crore")
    assert body["items"][1]["reason"] == "100 INR crore not stated"
    assert body["items"][0]["completed"] is True
    assert body["items"][2]["completed"] is False
    assert body["by_kind"]["fact_lookup"]["completion_rate"] == 0.25
    # Usage was read back through the real GET /jobs/{id}/usage: the
    # fake model reports none, so each item is priced, at zero.
    assert body["items"][0]["job_id"]
    assert (body["items"][0]["input_tokens"], body["items"][0]["cost_inr"]) == (0, 0.0)
    assert body["cost"]["items_without_usage"] == 0
    assert body["cost"]["completed"] == 1

    summary = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "`abc123`" in summary
    assert "| fact_lookup | 4 | 2 | 1 | 1 | 50.0% |" in summary
    assert "| fact_lookup | 4 | 1 | 25.0% | 1 | 25.0% |" in summary
    assert "`fact_lookup:acme:q2`" in summary.split("## Correct but not completed")[1]


async def test_a_job_that_never_finishes_times_out_and_the_run_continues(
    invitee, app_factory, tmp_path,
) -> None:
    """Nobody plays the worker here: the first job stays queued past its
    timeout, and -- one job in flight per Invitee -- the second submission
    is refused. Both are recorded as failed items with their reasons."""
    from fdi.api.deps import get_agent_client

    app = app_factory()
    app.dependency_overrides[get_agent_client] = lambda: FakeAgentClient([])

    async def no_wait(_interval: float) -> None:
        return None

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        adapter = AskJobAdapter(http, poll_interval=0.0, timeout=0.0, sleep=no_wait)
        await adapter.login(*invitee)
        report = await run_eval(
            _benchmark("acme"), adapter, limit=2, stamp={}, out_dir=tmp_path,
        )

    assert [i.status for i in report.items] == ["failed", "failed"]
    assert "timed out after 0s" in report.items[0].reason
    assert "still queued" in report.items[0].reason
    assert report.items[1].reason.startswith("POST /ask returned 429")
