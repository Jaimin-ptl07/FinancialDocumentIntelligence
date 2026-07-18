"""KnowledgeStage auto-chains off SemanticStage, OntologyStage, AND
QualityStage: `knowledge` is the codebase's FIRST three-dependency DERIVED
stage (`deps=("semantic", "ontology", "quality")`), folding SP-10's section
classification, SP-11's concept mappings, and SP-17b's quality report into
ONE per-document knowledge record. Against real Postgres + MinIO with FAKE
OCR + semantic clients."""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.semantic import SectionType
from fdi.pipeline.intake import intake_document
from fdi.pipeline.keys import artifact_key, derived_input_hash
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.run import run_stage
from fdi.pipeline.semantic import FakeSemanticClient
from fdi.pipeline.stages.ast import AstStage
from fdi.pipeline.stages.knowledge import KnowledgeStage
from fdi.pipeline.stages.normalize import NormalizeStage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.pipeline.stages.ontology import OntologyStage
from fdi.pipeline.stages.quality import QualityStage
from fdi.pipeline.stages.semantic import SemanticStage
from fdi.pipeline.stages.structure import StructureStage
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


async def test_knowledge_chains_off_semantic_ontology_and_quality(
    session_factory, store
) -> None:
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="a.pdf",
        doc_key=str(uuid.uuid4()),
        content_type="application/pdf",
        session_factory=session_factory,
        store=store,
    )

    fake_ocr = FakeOcrClient(
        {
            "content": "ACME",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": [
                {"role": "title", "content": "ACME LTD Annual Report"},
                {"role": "sectionHeading", "content": "Balance Sheet"},
                {"content": "Total Assets"},
                {"role": "sectionHeading", "content": "Directors' Report"},
                {"content": "The board is pleased with results."},
            ],
            "tables": [],
        }
    )
    fake_semantic = FakeSemanticClient(
        {
            "Balance Sheet": SectionType.BALANCE_SHEET.value,
            "Directors' Report": SectionType.DIRECTORS_REPORT.value,
        }
    )

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(StructureStage())
    reg.register(SemanticStage())
    reg.register(OntologyStage())
    reg.register(QualityStage())
    reg.register(KnowledgeStage())
    resolver = DagResolver(reg.all())

    assert "knowledge" in resolver.successors("semantic")  # the chain is automatic
    assert "knowledge" in resolver.successors("ontology")  # ... off ALL THREE deps
    assert "knowledge" in resolver.successors("quality")

    common = {
        "doc_id": doc.id,
        "correlation_id": "c1",
        "session_factory": session_factory,
        "store": store,
        "registry": reg,
        "resolver": resolver,
    }
    await run_stage(
        stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake_ocr}, **common
    )
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="semantic",
        run_id=str(uuid.uuid4()),
        services={"semantic": fake_semantic},
        **common,
    )
    await run_stage(stage_name="ontology", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="quality", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="knowledge", run_id=str(uuid.uuid4()), services={}, **common)

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    knowledge_key = artifact_key(
        doc.id,
        "knowledge",
        1,
        derived_input_hash(
            [
                facts["semantic"].output_hash,
                facts["ontology"].output_hash,
                facts["quality"].output_hash,
            ]
        ),
    )
    body = await store.get_json(knowledge_key)
    assert body == {
        "schema_version": 1,
        "section_count": 2,
        "section_types": {"BALANCE_SHEET": 1, "DIRECTORS_REPORT": 1},
        "concepts": ["TOTAL_ASSETS"],
        "concept_count": 1,
        "quality": {"ok": True, "error_count": 0, "warning_count": 0},
    }
