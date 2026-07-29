"""SemanticStage auto-chains off AstStage: `semantic` is a DERIVED stage
(`deps=("ast",)`) that classifies each AST section via an injected
SemanticClient, against real Postgres + MinIO with FAKE OCR + semantic
clients. Proves the cache hit does NOT call the (fake) LLM twice."""

import uuid
from collections.abc import Mapping

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.pricing import LlmCallUsage
from fdi.core.semantic import ClassifyResult, SectionType
from fdi.pipeline.intake import intake_document
from fdi.pipeline.keys import artifact_key, derived_input_hash
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.run import run_stage
from fdi.pipeline.semantic import FakeSemanticClient
from fdi.pipeline.stages.ast import AstStage
from fdi.pipeline.stages.normalize import NormalizeStage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.pipeline.stages.semantic import SemanticStage
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


async def test_semantic_stage_runs_and_caches(  # fixtures from conftest
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
                {"role": "title", "content": "ACME LTD"},
                {"role": "sectionHeading", "content": "Balance Sheet"},
                {"content": "Assets 100"},
            ],
            "tables": [],
        }
    )
    fake_semantic = FakeSemanticClient({"Balance Sheet": SectionType.BALANCE_SHEET.value})

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(SemanticStage())
    resolver = DagResolver(reg.all())

    assert "semantic" in resolver.successors("ast")  # the chain is automatic

    common = {
        "doc_id": doc.id,
        "correlation_id": "c1",
        "session_factory": session_factory,
        "store": store,
        "registry": reg,
        "resolver": resolver,
    }
    ocr_services = {"ocr": fake_ocr}
    semantic_services = {"semantic": fake_semantic}
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()), services=ocr_services, **common)
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="semantic", run_id=str(uuid.uuid4()), services=semantic_services, **common
    )
    # Second run: content-addressed cache hit -- must NOT call the fake LLM twice.
    await run_stage(
        stage_name="semantic", run_id=str(uuid.uuid4()), services=semantic_services, **common
    )
    assert fake_semantic.calls == 1

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    semantic_key = artifact_key(
        doc.id, "semantic", 1, derived_input_hash([facts["ast"].output_hash])
    )
    body = await store.get_json(semantic_key)
    assert body["sections"] == [
        {"heading": "Balance Sheet", "type": "BALANCE_SHEET", "paragraph_count": 1}
    ]


class _UsageCarryingSemanticClient:
    """A classify() double that -- unlike FakeSemanticClient, which always
    returns `usage=None` (see FakeSemanticClient's own docstring: "usage is
    None from every fake/test double") -- returns a real LlmCallUsage on
    every call, to prove SemanticStage.run actually records it."""

    def __init__(self, labels: Mapping[str, str]) -> None:
        self._labels = labels
        self.calls = 0

    def classify(self, heading: str, text: str) -> ClassifyResult:
        self.calls += 1
        return ClassifyResult(
            label=self._labels.get(heading, SectionType.OTHER.value),
            usage=LlmCallUsage(
                model="gpt-4o", input_tokens=10, output_tokens=5, duration_ms=50
            ),
        )


async def test_semantic_stage_records_one_llm_usage_event_per_section(
    session_factory, store
) -> None:
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="c.pdf",
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
                {"role": "title", "content": "ACME LTD"},
                {"role": "sectionHeading", "content": "Balance Sheet"},
                {"content": "Assets 100"},
                {"role": "sectionHeading", "content": "Income Statement"},
                {"content": "Revenue 50"},
            ],
            "tables": [],
        }
    )
    usage_semantic = _UsageCarryingSemanticClient({
        "Balance Sheet": SectionType.BALANCE_SHEET.value,
        "Income Statement": SectionType.INCOME_STATEMENT.value,
    })

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(SemanticStage())
    resolver = DagResolver(reg.all())

    common = {
        "doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
        "store": store, "registry": reg, "resolver": resolver,
    }
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()),
                     services={"ocr": fake_ocr}, **common)
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="semantic", run_id=str(uuid.uuid4()),
                     services={"semantic": usage_semantic}, **common)

    async with session_factory() as s:
        rows = await RegistryRepository(s).document_cost_by_source(doc.id)
    semantic_row = next(r for r in rows if r["source"] == "semantic_classify")
    assert semantic_row["call_count"] == 2  # two sections classified


class _FlakySemanticClient:
    """Raises for any heading in `failing_headings`; classifies every other
    section normally (with real usage). Proves per-section resilience
    (SemanticStage.run guards each classify() call individually, added
    after a real Azure OpenAI content-filter rejection on one section of a
    1000+-section production filing took down the whole stage): a rejected
    section degrades to OTHER and bills nothing for that one call, while
    every other section -- including ones AFTER the failure -- still
    classifies, bills normally, and the stage itself ends up done, not
    failed."""

    def __init__(self, labels: Mapping[str, str], failing_headings: set[str]) -> None:
        self._labels = labels
        self._failing = failing_headings
        self.calls = 0

    def classify(self, heading: str, text: str) -> ClassifyResult:
        self.calls += 1
        if heading in self._failing:
            raise RuntimeError("simulated Azure content-filter rejection")
        return ClassifyResult(
            label=self._labels.get(heading, SectionType.OTHER.value),
            usage=LlmCallUsage(
                model="gpt-4o", input_tokens=10, output_tokens=5, duration_ms=50
            ),
        )


async def test_semantic_stage_degrades_a_failed_section_instead_of_failing(
    session_factory, store
) -> None:
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="e.pdf",
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
                {"role": "title", "content": "ACME LTD"},
                {"role": "sectionHeading", "content": "Balance Sheet"},
                {"content": "Assets 100"},
                {"role": "sectionHeading", "content": "Risk Factors"},
                {"content": "flagged content"},
                {"role": "sectionHeading", "content": "Income Statement"},
                {"content": "Revenue 50"},
            ],
            "tables": [],
        }
    )
    flaky = _FlakySemanticClient(
        labels={
            "Balance Sheet": SectionType.BALANCE_SHEET.value,
            "Income Statement": SectionType.INCOME_STATEMENT.value,
        },
        failing_headings={"Risk Factors"},
    )

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(SemanticStage())
    resolver = DagResolver(reg.all())

    common = {
        "doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
        "store": store, "registry": reg, "resolver": resolver,
    }
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()),
                     services={"ocr": fake_ocr}, **common)
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    # Must NOT raise -- the rejected section degrades instead of failing the stage.
    await run_stage(stage_name="semantic", run_id=str(uuid.uuid4()),
                     services={"semantic": flaky}, **common)

    assert flaky.calls == 3, "every section is still attempted, including the one after the failure"

    async with session_factory() as s:
        rows = await RegistryRepository(s).document_cost_by_source(doc.id)
        facts = await RegistryRepository(s).latest_runs(doc.id)
        run_facts = await RegistryRepository(s).runs_for_document(doc.id)

    assert facts["semantic"].status == "done"
    semantic_row = next(r for r in rows if r["source"] == "semantic_classify")
    assert semantic_row["call_count"] == 2, (
        "only the 2 successful calls bill; the rejected one bills nothing"
    )

    key = artifact_key(
        doc.id, "semantic", 1, derived_input_hash([run_facts["ast"].output_hash])
    )
    body = await store.get_json(key)
    by_heading = {s["heading"]: s["type"] for s in body["sections"]}
    assert by_heading["Balance Sheet"] == "BALANCE_SHEET"
    assert by_heading["Income Statement"] == "INCOME_STATEMENT"
    assert by_heading["Risk Factors"] == "OTHER"  # degraded, not lost
