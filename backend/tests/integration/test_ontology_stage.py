"""OntologyStage auto-chains off AstStage: `ontology` is a DERIVED stage
(`deps=("ast",)`) that maps every AST section heading and paragraph to a
CanonicalConcept via the pure dictionary-based map_document, against real
Postgres + MinIO with a FAKE OCR client."""

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
from fdi.pipeline.stages.normalize import NormalizeStage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.pipeline.stages.ontology import OntologyStage
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


async def test_ontology_chains_off_ast(session_factory, store) -> None:  # fixtures from conftest
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
                {"role": "title", "content": "ACME LTD"},
                {"role": "sectionHeading", "content": "Revenue"},
                {"content": "Sales"},
                {"role": "sectionHeading", "content": "Balance Sheet"},
                {"content": "Total Assets"},
            ],
            "tables": [],
        }
    )

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(OntologyStage())
    resolver = DagResolver(reg.all())

    assert "ontology" in resolver.successors("ast")  # the chain is automatic

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
    await run_stage(stage_name="ontology", run_id=str(uuid.uuid4()), services={}, **common)
    # Second run: content-addressed cache hit against the same ast output_hash.
    await run_stage(stage_name="ontology", run_id=str(uuid.uuid4()), services={}, **common)

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    ontology_key = artifact_key(
        doc.id, "ontology", 1, derived_input_hash([facts["ast"].output_hash])
    )
    body = await store.get_json(ontology_key)
    assert body["schema_version"] == 1
    assert body["mappings"] == [
        {"label": "Revenue", "concept": "REVENUE"},
        {"label": "Sales", "concept": "REVENUE"},
        {"label": "Total Assets", "concept": "TOTAL_ASSETS"},
    ]
