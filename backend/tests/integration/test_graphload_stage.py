"""GraphLoadStage auto-chains off GraphStage: `graph_load` is a DERIVED stage
(`deps=("graph",)`) that projects the SP-9 graph artifact into a GraphStore,
against real Postgres + MinIO with a FAKE OCR client and a FAKE graph store."""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.pipeline.graphstore import FakeGraphStore
from fdi.pipeline.intake import intake_document
from fdi.pipeline.keys import artifact_key, derived_input_hash
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.run import run_stage
from fdi.pipeline.stages.ast import AstStage
from fdi.pipeline.stages.graph import GraphStage
from fdi.pipeline.stages.graphload import GraphLoadStage
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


async def test_graph_load_chains_off_graph_and_caches(session_factory, store) -> None:
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="a.pdf", doc_key=str(uuid.uuid4()), content_type="application/pdf",
        session_factory=session_factory, store=store,
    )
    fake_ocr = FakeOcrClient(
        {
            "content": "ACME",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": [
                {"role": "title", "content": "ACME LTD"},
                {"role": "sectionHeading", "content": "Directors' Report"},
                {"content": "The board is pleased."},
            ],
            "tables": [],
        }
    )
    fake_store = FakeGraphStore()

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(GraphStage())
    reg.register(GraphLoadStage())
    resolver = DagResolver(reg.all())

    assert "graph_load" in resolver.successors("graph")  # the chain is automatic

    common = {
        "doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
        "store": store, "registry": reg, "resolver": resolver,
    }
    await run_stage(
        stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake_ocr}, **common
    )
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
    gl_services = {"graph_store": fake_store}
    await run_stage(
        stage_name="graph_load", run_id=str(uuid.uuid4()), services=gl_services, **common
    )
    # Second run: content-addressed cache hit -- must NOT call the fake store twice.
    await run_stage(
        stage_name="graph_load", run_id=str(uuid.uuid4()), services=gl_services, **common
    )
    assert fake_store.calls == 1

    # Ids are namespaced `{doc_id}:{bare_id}`, and every node carries a doc_id property.
    node_ids = {n["id"] for n in fake_store.loaded_nodes}
    assert node_ids == {f"{doc.id}:doc", f"{doc.id}:s0", f"{doc.id}:s0p0"}
    assert all(n["doc_id"] == doc.id for n in fake_store.loaded_nodes)
    edge_pairs = {(e["source"], e["target"]) for e in fake_store.loaded_edges}
    assert edge_pairs == {(f"{doc.id}:doc", f"{doc.id}:s0"), (f"{doc.id}:s0", f"{doc.id}:s0p0")}

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    # version bumps 1 -> 2 for the namespacing fix
    key = artifact_key(doc.id, "graph_load", 2, derived_input_hash([facts["graph"].output_hash]))
    body = await store.get_json(key)
    assert body == {"schema_version": 1, "loaded_nodes": 3, "loaded_edges": 2}


async def test_graph_load_namespaces_ids_no_collision_across_documents(
    session_factory, store
) -> None:
    """The regression test RT-5 exists for: two documents loaded through the
    SAME GraphStore must not collide on id."""
    fake_ocr = FakeOcrClient(
        {
            "content": "ACME",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": [
                {"role": "title", "content": "ACME LTD"},
                {"role": "sectionHeading", "content": "Directors' Report"},
                {"content": "The board is pleased."},
            ],
            "tables": [],
        }
    )
    fake_store = FakeGraphStore()
    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(GraphStage())
    reg.register(GraphLoadStage())
    resolver = DagResolver(reg.all())

    doc_ids: list[str] = []
    for _ in range(2):
        doc = await intake_document(
            pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
            filename="a.pdf", doc_key=str(uuid.uuid4()), content_type="application/pdf",
            session_factory=session_factory, store=store,
        )
        doc_ids.append(doc.id)
        common = {
            "doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
            "store": store, "registry": reg, "resolver": resolver,
        }
        await run_stage(
            stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake_ocr}, **common
        )
        await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
        await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
        await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
        await run_stage(
            stage_name="graph_load", run_id=str(uuid.uuid4()),
            services={"graph_store": fake_store}, **common,
        )

    all_ids = [n["id"] for n in fake_store.loaded_nodes]
    assert len(all_ids) == len(set(all_ids))  # no collision
    for doc_id in doc_ids:
        assert f"{doc_id}:doc" in all_ids
