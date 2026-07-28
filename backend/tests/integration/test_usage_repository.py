"""RegistryRepository's OCR/LLM usage-event read/write methods, against
real Postgres. Costing feature."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.platform.db.engine import make_session_factory
from fdi.platform.db.repository import DocumentRepository, RegistryRepository

pytestmark = pytest.mark.integration


@pytest.fixture
def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(migrated_engine)


async def _make_document_and_stage_run(session_factory) -> tuple[str, str]:
    doc_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.create_document(doc_id)
        await repo.start_run(run_id, doc_id, "ocr", 5, "hash1", "corr1")
        await s.commit()
    return doc_id, run_id


async def test_record_ocr_usage_persists_a_row(session_factory) -> None:
    doc_id, run_id = await _make_document_and_stage_run(session_factory)
    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.record_ocr_usage(
            str(uuid.uuid4()), doc_id=doc_id, stage_run_id=run_id,
            correlation_id="corr1", page_count=12,
            azure_model="prebuilt-layout", duration_ms=2500,
        )
        await s.commit()
    async with session_factory() as s:
        rows = await RegistryRepository(s).document_cost_by_source(doc_id)
    ocr_row = next(r for r in rows if r["source"] == "ocr")
    assert ocr_row["page_count"] == 12
    assert ocr_row["call_count"] == 1
    assert ocr_row["duration_ms"] == 2500


async def test_record_llm_usage_persists_a_row(session_factory) -> None:
    doc_id, run_id = await _make_document_and_stage_run(session_factory)
    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.record_llm_usage(
            str(uuid.uuid4()), doc_id=doc_id, stage_run_id=run_id,
            source="semantic_classify", correlation_id="corr1",
            model="gpt-4o", input_tokens=100, output_tokens=20, duration_ms=800,
        )
        await s.commit()
    async with session_factory() as s:
        rows = await RegistryRepository(s).document_cost_by_source(doc_id)
    llm_row = next(r for r in rows if r["source"] == "semantic_classify")
    assert llm_row["input_tokens"] == 100
    assert llm_row["output_tokens"] == 20
    assert llm_row["call_count"] == 1


async def test_llm_usage_with_no_doc_or_stage_run_is_allowed(session_factory) -> None:
    """/ask's untethered rows: doc_id=None, stage_run_id=None."""
    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.record_llm_usage(
            str(uuid.uuid4()), doc_id=None, stage_run_id=None,
            source="ask_agent", correlation_id="corr2",
            model="gpt-4o", input_tokens=50, output_tokens=10, duration_ms=400,
        )
        await s.commit()
    async with session_factory() as s:
        totals = await RegistryRepository(s).corpus_cost_totals()
    assert totals["llm_input_tokens"] >= 50
    assert totals["llm_output_tokens"] >= 10


async def test_document_delete_removes_usage_events_too(session_factory) -> None:
    """DocumentRepository.delete must not violate ocr_usage_event/
    llm_usage_event's FKs to stage_run/document -- both tables' rows are
    children of stage_run (which is itself a child of document), so they
    must be deleted before stage_run, which must be deleted before
    document. Regression test: this previously raised
    ForeignKeyViolationError on a document with any real usage history."""
    doc_id, run_id = await _make_document_and_stage_run(session_factory)
    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.record_ocr_usage(
            str(uuid.uuid4()), doc_id=doc_id, stage_run_id=run_id,
            correlation_id="corr1", page_count=3,
            azure_model="prebuilt-layout", duration_ms=100,
        )
        await repo.record_llm_usage(
            str(uuid.uuid4()), doc_id=doc_id, stage_run_id=run_id,
            source="semantic_classify", correlation_id="corr1",
            model="gpt-4o", input_tokens=10, output_tokens=5, duration_ms=50,
        )
        await s.commit()

    async with session_factory() as s:
        await DocumentRepository(s).delete(doc_id)
        await s.commit()

    async with session_factory() as s:
        assert await DocumentRepository(s).get(doc_id) is None
        rows = await RegistryRepository(s).document_cost_by_source(doc_id)
    assert rows == []
