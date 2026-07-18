"""fdi.pipeline.entities.entity_timeline against real Postgres + MinIO, FAKE
OCR/semantic clients. Reuses the ocr->...->knowledge harness
test_resolve_entities.py established (no `embedding` stage -- RT-7's entity
identity is `doc_key`, not an embedding cluster; see design spec decision 1).

Session-scoped DB note: unlike resolve_entities (a corpus-wide scan),
entity_timeline is scoped to exactly one doc_key (DocumentRepository.
list_by_doc_key) -- a fresh uuid4() doc_key per test cannot collide with any
other integration test file's documents, so EXACT member_count/timeline
assertions are safe here (no membership-only hedge needed, unlike
test_resolve_entities.py).
"""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.semantic import SectionType
from fdi.pipeline.entities import entity_timeline
from fdi.pipeline.intake import intake_document
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


def _build_registry() -> tuple[StageRegistry, DagResolver]:
    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(StructureStage())
    reg.register(SemanticStage())
    reg.register(OntologyStage())
    reg.register(QualityStage())
    reg.register(KnowledgeStage())
    return reg, DagResolver(reg.all())


async def _process_document(*, doc_id, session_factory, store, reg, resolver, title) -> None:
    fake_ocr = FakeOcrClient({
        "content": title,
        "pages": [{"pageNumber": 1, "lines": [], "words": []}],
        "paragraphs": [
            {"role": "title", "content": title},
            {"role": "sectionHeading", "content": "Balance Sheet"},
            {"content": "Total Assets"},
        ],
        "tables": [],
    })
    fake_semantic = FakeSemanticClient({"Balance Sheet": SectionType.BALANCE_SHEET.value})
    common = {
        "doc_id": doc_id, "correlation_id": "c1", "session_factory": session_factory,
        "store": store, "registry": reg, "resolver": resolver,
    }
    for stage, services in (
        ("ocr", {"ocr": fake_ocr}), ("normalize", {}), ("ast", {}), ("structure", {}),
        ("semantic", {"semantic": fake_semantic}), ("ontology", {}), ("quality", {}),
        ("knowledge", {}),
    ):
        await run_stage(stage_name=stage, run_id=str(uuid.uuid4()), services=services, **common)


@pytest.fixture
async def two_version_doc_key(session_factory, store) -> tuple[str, str, str]:
    """Two documents, SAME doc_key, DIFFERENT pdf bytes (distinct checksums ->
    version 1, version 2). Returns (doc_key, doc_id_v1, doc_id_v2)."""
    doc_key = str(uuid.uuid4())
    reg, resolver = _build_registry()
    ids: list[str] = []
    for title in ("ACME FY23", "ACME FY24"):
        doc = await intake_document(
            pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(),
            filename=f"{title}.pdf", doc_key=doc_key, content_type="application/pdf",
            session_factory=session_factory, store=store,
        )
        await _process_document(
            doc_id=doc.id, session_factory=session_factory, store=store,
            reg=reg, resolver=resolver, title=title,
        )
        ids.append(doc.id)
    return doc_key, ids[0], ids[1]


async def test_entity_timeline_two_versions_ordered(
    session_factory, store, two_version_doc_key
) -> None:
    doc_key, id_v1, id_v2 = two_version_doc_key
    async with session_factory() as s:
        result = await entity_timeline(session=s, store=store, doc_key=doc_key)

    assert result is not None
    assert result["doc_key"] == doc_key
    assert result["member_count"] == 2
    assert "entity_id" not in result
    timeline = result["timeline"]
    assert [t["doc_id"] for t in timeline] == [id_v1, id_v2]
    assert [t["version"] for t in timeline] == [1, 2]
    assert timeline[0]["section_count"] >= 1
    assert "TOTAL_ASSETS" in timeline[0]["concepts"]
    assert "TOTAL_ASSETS" in result["concepts"]


async def test_entity_timeline_unknown_doc_key_returns_none(session_factory, store) -> None:
    async with session_factory() as s:
        assert await entity_timeline(session=s, store=store, doc_key="no-such-key") is None
