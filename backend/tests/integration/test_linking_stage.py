"""LinkingStage auto-chains off OcrStage: `linking` is a DERIVED stage (deps=("ocr",))
that surfaces figures (with captions) and footnotes, against real Postgres + MinIO with
a FAKE DI client."""

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
from fdi.pipeline.stages.linking import LinkingStage
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


async def test_linking_surfaces_figures_and_footnotes(  # fixtures from conftest
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

    fake = FakeOcrClient(
        {
            "content": "ACME LTD\nFig 1: Revenue trend\n1. Restated for FY23 comparatives.",
            "pages": [{"pageNumber": 1}],
            "paragraphs": [
                {
                    "role": "title",
                    "content": "ACME LTD",
                    "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                    "spans": [{"offset": 0, "length": 8}],
                },
                {
                    "role": "footnote",
                    "content": "1. Restated for FY23 comparatives.",
                    "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                    "spans": [{"offset": 40, "length": 34}],
                },
            ],
            "tables": [],
            "figures": [
                {
                    "caption": {"content": "Fig 1: Revenue trend"},
                    "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                },
                {
                    "boundingRegions": [{"pageNumber": 1, "polygon": []}],  # no caption -> ""
                },
            ],
            "sections": [],
        }
    )

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(LinkingStage())
    resolver = DagResolver(reg.all())

    assert "linking" in resolver.successors("ocr")  # the chain is automatic

    common = {
        "doc_id": doc.id,
        "correlation_id": "c1",
        "session_factory": session_factory,
        "store": store,
        "registry": reg,
        "resolver": resolver,
    }
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake}, **common)
    await run_stage(stage_name="linking", run_id=str(uuid.uuid4()), **common)

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    linking_key = artifact_key(
        doc.id, "linking", 1, derived_input_hash([facts["ocr"].output_hash])
    )
    body = await store.get_json(linking_key)
    assert body["schema_version"] == 1
    assert body["figures"] == [{"caption": "Fig 1: Revenue trend"}, {"caption": ""}]
    assert body["footnotes"] == [{"text": "1. Restated for FY23 comparatives."}]
