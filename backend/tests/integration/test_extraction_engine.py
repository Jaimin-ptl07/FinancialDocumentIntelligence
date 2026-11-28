"""The engine's per-Field cache (S3, ADR-0004), against real Postgres/MinIO.

Asserted through what reaches the model (which Fields each call carried)
and what is stored -- never internal call order. Covers the names-only cache
bug the old per-document cache had: adding a Field re-extracted everything,
and rewording one never re-read it."""

import pytest
from sqlalchemy import select

from fdi.core.extractor import FieldSpec
from fdi.pipeline.embeddings import FakeEmbeddingClient
from fdi.pipeline.extractor import (
    EngineField,
    ExtractContext,
    document_engine_field,
    extract_document,
    extract_fields,
)
from fdi.pipeline.vectorstore import FakeVectorStore
from fdi.platform.db.models import Document, FieldRead
from tests.integration.extraction_support import AnsweringClient, indexed_report

pytestmark = pytest.mark.integration


@pytest.fixture
def fakes() -> tuple[FakeEmbeddingClient, FakeVectorStore]:
    return FakeEmbeddingClient(dim=8), FakeVectorStore()


@pytest.fixture
async def report(session_factory, store, fakes) -> str:
    embedder, vector_store = fakes
    return await indexed_report(session_factory, store, embedder=embedder,
                                vector_store=vector_store)


def _spec(path: str, *, prompt: str = "", unit: str = "PL") -> FieldSpec:
    return FieldSpec(
        field_name=path, data_type="decimal", retrieval_query="Revenue from operations",
        keywords=(path,), extraction_prompt=prompt or f"Extract {path}.", enum_values=None,
        module_id=1, module_name="P&L", unit_id=unit, unit_title="Profit and Loss",
        field_path=path, locator_keywords=("revenue",), tag=path,
    )


async def _extract(session_factory, store, fakes, doc_id, specs, client, **kw):
    embedder, vector_store = fakes
    async with session_factory() as s:
        doc = await s.get(Document, doc_id)
        ctx = ExtractContext(session=s, store=store, embedder=embedder,
                             vector_store=vector_store, doc_id=doc_id, doc_key=doc.doc_key,
                             version=doc.version)
        return await extract_document(ctx=ctx, fields=specs, client=client, **kw)


async def test_adding_one_field_extracts_only_that_field(
    session_factory, store, fakes, report
) -> None:
    client = AnsweringClient({"A": 1000.0, "B": 800.0, "C": 200.0})
    first = await _extract(session_factory, store, fakes, report,
                           [_spec("A"), _spec("B")], client)
    assert client.calls == [["A", "B"]]
    assert [f["value"] for f in first["fields"]] == [1000.0, 800.0]

    second = await _extract(session_factory, store, fakes, report,
                            [_spec("A"), _spec("B"), _spec("C")], client)
    assert client.calls[1:] == [["C"]], "only the new Field reaches the model"
    assert [f["value"] for f in second["fields"]] == [1000.0, 800.0, 200.0]

    await _extract(session_factory, store, fakes, report,
                   [_spec("A"), _spec("B"), _spec("C")], client)
    assert len(client.calls) == 2, "nothing changed, so nothing is paid for"


async def test_a_reworded_field_is_reread_and_only_it(
    session_factory, store, fakes, report
) -> None:
    client = AnsweringClient()
    await _extract(session_factory, store, fakes, report, [_spec("A"), _spec("B")], client)
    await _extract(session_factory, store, fakes, report,
                   [_spec("A"), _spec("B", prompt="Extract B, net of rebates.")], client)
    assert client.calls == [["A", "B"], ["B"]]


async def test_force_rereads_and_keeps_the_old_read_superseded(
    session_factory, store, fakes, report
) -> None:
    client = AnsweringClient({"A": 1.0})
    await _extract(session_factory, store, fakes, report, [_spec("A")], client)
    client.values["A"] = 2.0
    again = await _extract(session_factory, store, fakes, report, [_spec("A")], client,
                           force=True)
    assert again["fields"][0]["value"] == 2.0
    field_id = document_engine_field(_spec("A")).field_id
    async with session_factory() as s:
        reads = (await s.execute(
            select(FieldRead).where(FieldRead.doc_id == report, FieldRead.field_id == field_id)
            .order_by(FieldRead.created_at)
        )).scalars().all()
    assert [(r.value, r.superseded_at is None) for r in reads] == [(1.0, False), (2.0, True)]


async def test_a_failed_call_is_stored_as_failed_and_retried_next_time(
    session_factory, store, fakes, report
) -> None:
    broken = AnsweringClient(fail=lambda paths: True)
    out = await _extract(session_factory, store, fakes, report, [_spec("A")], broken)
    assert out["fields"][0]["value"] is None
    field_id = document_engine_field(_spec("A")).field_id
    async with session_factory() as s:
        read = (await s.execute(select(FieldRead).where(
            FieldRead.doc_id == report, FieldRead.field_id == field_id,
            FieldRead.superseded_at.is_(None),
        ))).scalar_one()
    assert read.status == "failed"
    assert "model unavailable" in (read.error or "")

    healed = AnsweringClient({"A": 5.0})
    out = await _extract(session_factory, store, fakes, report, [_spec("A")], healed)
    assert healed.calls == [["A"]], "a failed read is never served as an answer"
    assert out["fields"][0]["value"] == 5.0


async def test_a_field_the_model_skips_is_failed_not_cached_as_absent(
    session_factory, store, fakes, report
) -> None:
    class Skipping(AnsweringClient):
        def extract(self, *, system: str, user: str):
            result = super().extract(system=system, user=user)
            return type(result)(data={k: v for k, v in result.data.items() if k != "B"})

    client = Skipping()
    await _extract(session_factory, store, fakes, report, [_spec("A"), _spec("B")], client)
    await _extract(session_factory, store, fakes, report, [_spec("A"), _spec("B")], client)
    assert client.calls == [["A", "B"], ["B"]]


async def test_no_retrieval_is_a_failure_not_an_answer(session_factory, store, report) -> None:
    """Without an embedder/vector store nothing was searched: the read is
    `failed`, so the value is retried once retrieval works -- never cached
    as "not in this Report"."""
    client = AnsweringClient()
    async with session_factory() as s:
        doc = await s.get(Document, report)
        ctx = ExtractContext(session=s, store=store, embedder=None, vector_store=None,
                             doc_id=report, doc_key=doc.doc_key, version=doc.version)
        [result] = await extract_fields(
            ctx=ctx, fields=[document_engine_field(_spec("A"))], client=client
        )
    assert client.calls == []
    assert result.read.status == "failed"
    assert "not configured" in (result.read.error or "")


async def test_a_different_model_deployment_makes_reads_stale(
    session_factory, store, fakes, report
) -> None:
    first = AnsweringClient()
    await _extract(session_factory, store, fakes, report, [_spec("A")], first)
    other = AnsweringClient()
    other.model_id = "another-deployment"  # type: ignore[misc]
    await _extract(session_factory, store, fakes, report, [_spec("A")], other)
    assert other.calls == [["A"]]


async def test_derived_fields_are_refused_by_the_engine(
    session_factory, store, fakes, report
) -> None:
    from dataclasses import replace

    embedder, vector_store = fakes
    derived = EngineField("d", "h", replace(_spec("T"), origin="derived"))
    async with session_factory() as s:
        doc = await s.get(Document, report)
        ctx = ExtractContext(session=s, store=store, embedder=embedder,
                             vector_store=vector_store, doc_id=report, doc_key=doc.doc_key,
                             version=doc.version)
        with pytest.raises(ValueError, match="derived"):
            await extract_fields(ctx=ctx, fields=[derived], client=AnsweringClient())
