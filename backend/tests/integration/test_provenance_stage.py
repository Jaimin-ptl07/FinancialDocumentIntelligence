"""ProvenanceStage auto-chains off BOTH GraphStage and OcrStage: `provenance`
is the codebase's FIRST two-dependency DERIVED stage (`deps=("graph", "ocr")`),
joining the SP-9 graph's nodes against the enriched OCR artifact's paragraph
geometry. Against real Postgres + MinIO with a FAKE OCR client."""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.pipeline.intake import intake_document
from fdi.pipeline.keys import artifact_key, derived_input_hash
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.run import run_stage
from fdi.pipeline.stages.ast import AstStage
from fdi.pipeline.stages.graph import GraphStage
from fdi.pipeline.stages.normalize import NormalizeStage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.pipeline.stages.provenance import ProvenanceStage
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


async def test_provenance_chains_off_graph_and_ocr(session_factory, store) -> None:
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="a.pdf",
        doc_key=str(uuid.uuid4()),
        content_type="application/pdf",
        session_factory=session_factory,
        store=store,
    )

    fake = FakeOcrClient(
        {
            "content": "ACME",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": [
                {
                    "role": "title",
                    "content": "ACME LTD",
                    "boundingRegions": [{"pageNumber": 1, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}],
                },
                {
                    "role": "sectionHeading",
                    "content": "Directors' Report",
                    "boundingRegions": [{"pageNumber": 1, "polygon": [0, 1, 1, 1, 1, 2, 0, 2]}],
                },
                {
                    "content": "The board is pleased.",
                    "boundingRegions": [{"pageNumber": 2, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}],
                },
            ],
            "tables": [],
        }
    )

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(GraphStage())
    reg.register(ProvenanceStage())
    resolver = DagResolver(reg.all())

    assert "provenance" in resolver.successors("graph")  # the chain is automatic
    assert "provenance" in resolver.successors("ocr")  # ... off BOTH deps

    common = {
        "doc_id": doc.id,
        "correlation_id": "c1",
        "session_factory": session_factory,
        "store": store,
        "registry": reg,
        "resolver": resolver,
    }
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake}, **common)
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    provenance_key = artifact_key(
        doc.id,
        "provenance",
        1,
        derived_input_hash([facts["graph"].output_hash, facts["ocr"].output_hash]),
    )
    body = await store.get_json(provenance_key)
    assert body["schema_version"] == 1
    assert body["node_provenance"] == [
        {
            "node_id": "doc",
            "page": 1,
            "bounding_regions": [{"page_number": 1, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}],
        },
        {
            "node_id": "s0",
            "page": 1,
            "bounding_regions": [{"page_number": 1, "polygon": [0, 1, 1, 1, 1, 2, 0, 2]}],
        },
        {
            "node_id": "s0p0",
            "page": 2,
            "bounding_regions": [{"page_number": 2, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}],
        },
    ]
