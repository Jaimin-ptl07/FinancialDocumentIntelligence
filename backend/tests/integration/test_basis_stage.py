"""BasisStage auto-chains off BOTH AstStage and ProvenanceStage
(deps=("ast", "provenance")): it segments the document into standalone/
consolidated page regions. Against real Postgres + MinIO with a FAKE OCR
client.

A focused, non-containerised smoke test below drives `BasisStage.run`
directly against a hand-built `StageContext`, mirroring how
`test_index_enrichment.py` exercises `IndexStage` without Docker."""

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
from fdi.pipeline.stage import StageContext
from fdi.pipeline.stages.ast import AstStage
from fdi.pipeline.stages.basis import BasisStage
from fdi.pipeline.stages.graph import GraphStage
from fdi.pipeline.stages.normalize import NormalizeStage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.pipeline.stages.provenance import ProvenanceStage
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


async def test_basis_chains_off_ast_and_provenance(session_factory, store) -> None:
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
                    "content": (
                        "Independent Auditor's Report on the Standalone "
                        "Financial Statements"
                    ),
                    "boundingRegions": [{"pageNumber": 1, "polygon": [0, 1, 1, 1, 1, 2, 0, 2]}],
                },
                {
                    "content": "The financial statements give a true and fair view.",
                    "boundingRegions": [{"pageNumber": 1, "polygon": [0, 2, 1, 2, 1, 3, 0, 3]}],
                },
            ],
            "tables": [],
        }
    )

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(StructureStage())
    reg.register(GraphStage())
    reg.register(ProvenanceStage())
    reg.register(BasisStage())
    resolver = DagResolver(reg.all())

    assert "basis" in resolver.successors("ast")  # the chain is automatic
    assert "basis" in resolver.successors("provenance")  # ... off BOTH deps

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
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="basis", run_id=str(uuid.uuid4()), services={}, **common)

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    basis_key = artifact_key(
        doc.id,
        "basis",
        1,
        derived_input_hash([facts["ast"].output_hash, facts["provenance"].output_hash]),
    )
    body = await store.get_json(basis_key)
    assert body["schema_version"] == 1
    assert isinstance(body["regions"], list)
    assert body["regions"] == [{"start_page": 1, "end_page": 1, "basis": "standalone"}]
    assert body["default_basis"] == "unknown"


async def test_basis_stage_smoke_no_docker() -> None:
    """`BasisStage.run` over a hand-built `StageContext` -- no Postgres/MinIO
    needed -- exercising the same wiring `IndexStage`'s no-Docker test does."""

    async def read_artifact(doc_id: str, stage_name: str) -> object:
        if stage_name == "ast":
            return {
                "schema_version": 2,
                "title": "ACME LTD",
                "sections": [
                    {
                        "heading": (
                            "Independent Auditor's Report on the Standalone Financial "
                            "Statements"
                        )
                    },
                    {
                        "heading": (
                            "Independent Auditor's Report on the Consolidated Financial "
                            "Statements"
                        )
                    },
                ],
            }
        if stage_name == "provenance":
            return {
                "schema_version": 1,
                "node_provenance": [
                    {"node_id": "s0", "page": 3},
                    {"node_id": "s1", "page": 90},
                ],
            }
        raise AssertionError(f"unexpected read_artifact stage: {stage_name!r}")

    ctx = StageContext(doc_id="doc-smoke", read_artifact=read_artifact)

    result = await BasisStage().run(ctx)

    assert result == {
        "schema_version": 1,
        "regions": [
            {"start_page": 3, "end_page": 89, "basis": "standalone"},
            {"start_page": 90, "end_page": 90, "basis": "consolidated"},
        ],
        "default_basis": "unknown",
    }
