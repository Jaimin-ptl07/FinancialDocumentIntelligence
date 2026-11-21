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


async def test_document_usage_events_returns_raw_per_call_rows_newest_first(
    session_factory,
) -> None:
    doc_id, run_id = await _make_document_and_stage_run(session_factory)
    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.record_ocr_usage(
            str(uuid.uuid4()), doc_id=doc_id, stage_run_id=run_id,
            correlation_id="corr1", page_count=4,
            azure_model="prebuilt-layout", duration_ms=1000,
        )
        await repo.record_llm_usage(
            str(uuid.uuid4()), doc_id=doc_id, stage_run_id=run_id,
            source="semantic_classify", correlation_id="corr1",
            model="gpt-4o", input_tokens=10, output_tokens=5, duration_ms=50,
        )
        await repo.record_llm_usage(
            str(uuid.uuid4()), doc_id=doc_id, stage_run_id=run_id,
            source="semantic_classify", correlation_id="corr1",
            model="gpt-4o", input_tokens=20, output_tokens=8, duration_ms=60,
        )
        await s.commit()

    async with session_factory() as s:
        events = await RegistryRepository(s).document_usage_events(doc_id)

    assert len(events) == 3
    kinds = sorted(e["kind"] for e in events)
    assert kinds == ["llm", "llm", "ocr"]
    # newest first
    assert events == sorted(events, key=lambda e: e["created_at"], reverse=True)

    ocr_event = next(e for e in events if e["kind"] == "ocr")
    assert ocr_event["source"] == "ocr"
    assert ocr_event["model"] == "prebuilt-layout"
    assert ocr_event["page_count"] == 4
    assert ocr_event["input_tokens"] == 0
    assert ocr_event["output_tokens"] == 0
    assert ocr_event["stage_run_id"] == run_id
    assert ocr_event["correlation_id"] == "corr1"

    llm_events = [e for e in events if e["kind"] == "llm"]
    assert {e["input_tokens"] for e in llm_events} == {10, 20}
    for e in llm_events:
        assert e["source"] == "semantic_classify"
        assert e["model"] == "gpt-4o"
        assert e["page_count"] == 0


async def test_corpus_cost_by_source_aggregates_across_documents(session_factory) -> None:
    """The corpus DB is shared and NOT reset between tests (see
    conftest.py's migrated_engine docstring), so this asserts a before/
    after DELTA over a fresh, uniquely-tagged document rather than an
    exact total -- the same pattern test_analytics.py already uses for
    the identical reason."""
    async with session_factory() as s:
        before = {r["source"]: r for r in await RegistryRepository(s).corpus_cost_by_source()}

    doc_id, run_id = await _make_document_and_stage_run(session_factory)
    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.record_ocr_usage(
            str(uuid.uuid4()), doc_id=doc_id, stage_run_id=run_id,
            correlation_id="corr1", page_count=7,
            azure_model="prebuilt-layout", duration_ms=100,
        )
        await repo.record_llm_usage(
            str(uuid.uuid4()), doc_id=doc_id, stage_run_id=run_id,
            source="semantic_classify", correlation_id="corr1",
            model="gpt-4o", input_tokens=30, output_tokens=6, duration_ms=50,
        )
        await s.commit()

    async with session_factory() as s:
        after = {r["source"]: r for r in await RegistryRepository(s).corpus_cost_by_source()}

    before_ocr_pages = before.get("ocr", {}).get("page_count", 0)
    before_llm_in = before.get("semantic_classify", {}).get("input_tokens", 0)
    assert after["ocr"]["page_count"] - before_ocr_pages == 7
    assert after["semantic_classify"]["input_tokens"] - before_llm_in == 30


async def test_corpus_cost_by_document_lists_this_document_with_its_totals(
    session_factory,
) -> None:
    doc_id, run_id = await _make_document_and_stage_run(session_factory)
    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.record_ocr_usage(
            str(uuid.uuid4()), doc_id=doc_id, stage_run_id=run_id,
            correlation_id="corr1", page_count=5,
            azure_model="prebuilt-layout", duration_ms=100,
        )
        await repo.record_llm_usage(
            str(uuid.uuid4()), doc_id=doc_id, stage_run_id=run_id,
            source="semantic_classify", correlation_id="corr1",
            model="gpt-4o", input_tokens=15, output_tokens=3, duration_ms=50,
        )
        await s.commit()

    async with session_factory() as s:
        rows = await RegistryRepository(s).corpus_cost_by_document()
    row = next(r for r in rows if r["doc_id"] == doc_id)
    assert row["ocr_page_count"] == 5
    assert row["llm_input_tokens"] == 15
    assert row["llm_output_tokens"] == 3


async def test_corpus_cost_by_document_excludes_ask_agents_untethered_rows(
    session_factory,
) -> None:
    """/ask rows (doc_id=None) belong to no single document -- they must
    never appear in the per-document listing."""
    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.record_llm_usage(
            str(uuid.uuid4()), doc_id=None, stage_run_id=None,
            source="ask_agent", correlation_id="corr3",
            model="gpt-4o", input_tokens=99, output_tokens=11, duration_ms=200,
        )
        await s.commit()

    async with session_factory() as s:
        rows = await RegistryRepository(s).corpus_cost_by_document()
    assert all(r["doc_id"] is not None for r in rows)


async def test_fog_mapping_is_an_accepted_llm_usage_source(session_factory) -> None:
    """`fog_mapping` is the only FOG stage that spends money, and
    ck_llm_usage_event_source rejected it until migration 0009. The
    rejection was not a lost billing row: record_llm_usage runs AFTER the
    Azure call returns, so the CheckViolationError propagated out of the
    stage and killed a run whose LLM calls had already been paid for."""
    doc_id, run_id = await _make_document_and_stage_run(session_factory)
    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.record_llm_usage(
            str(uuid.uuid4()), doc_id=doc_id, stage_run_id=run_id,
            source="fog_mapping", correlation_id="corr-fog",
            model="gpt-4o", input_tokens=41, output_tokens=9, duration_ms=70,
        )
        await s.commit()

    async with session_factory() as s:
        rows = await RegistryRepository(s).document_usage_events(doc_id)
    row = next(r for r in rows if r["source"] == "fog_mapping")
    assert row["input_tokens"] == 41
    assert row["output_tokens"] == 9


async def test_stage_cost_totals_attributes_spend_to_the_stage_that_ran(
    session_factory,
) -> None:
    """Attribution comes from the JOINED stage_run, not from the `source`
    string. The previous implementation filtered on source ==
    'semantic_classify' and hardcoded the key "semantic", so a mapping call
    billed under a real stage_run reported as zero cost -- a stage that
    spends money reading as free is the one error a cost screen must not
    make."""
    doc_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.create_document(doc_id)
        await repo.start_run(run_id, doc_id, "fog_mapping", 7, "hash-fog", "corr-fog")
        await s.commit()

    async with session_factory() as s:
        before = (await RegistryRepository(s).stage_cost_totals()).get("fog_mapping")
    before_in = before["llm_input_tokens"] if before else 0

    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.record_llm_usage(
            str(uuid.uuid4()), doc_id=doc_id, stage_run_id=run_id,
            source="fog_mapping", correlation_id="corr-fog",
            model="gpt-4o", input_tokens=120, output_tokens=25, duration_ms=90,
        )
        await s.commit()

    async with session_factory() as s:
        after = await RegistryRepository(s).stage_cost_totals()
    assert after["fog_mapping"]["llm_input_tokens"] - before_in == 120


async def test_stage_cost_totals_ignores_calls_with_no_stage_run(
    session_factory,
) -> None:
    """/ask and /extract/document run outside run_stage. They have no
    stage to attribute to, so the inner join must drop them rather than
    invent one."""
    async with session_factory() as s:
        before = await RegistryRepository(s).stage_cost_totals()

    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.record_llm_usage(
            str(uuid.uuid4()), doc_id=None, stage_run_id=None,
            source="ask_agent", correlation_id="corr-ask",
            model="gpt-4o", input_tokens=500, output_tokens=50, duration_ms=200,
        )
        await s.commit()

    async with session_factory() as s:
        after = await RegistryRepository(s).stage_cost_totals()
    assert {k: v["llm_input_tokens"] for k, v in after.items()} == {
        k: v["llm_input_tokens"] for k, v in before.items()
    }
