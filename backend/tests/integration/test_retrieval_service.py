"""The Retrieval service (S4) against real Postgres + MinIO, with
`FakeVectorStore`/`FakeEmbeddingClient` and fixture artifacts.

One Company, ACME, with:
  * FY2025-26: doc_key acme-2026, version 1 (superseded) and version 2,
  * FY2024-25: doc_key acme-2025, version 1,
  * FY2023-24: doc_key acme-2024, filed but never processed.
Each processed Report has a consolidated P&L table (p2), a standalone
passage (p3), an unlabelled narrative passage (p1), a note 23 "Other income"
(p5) that the P&L's "Other income" line cites, and a FOG mapping of revenue
to AUTOPL001.

Tests assert what Evidence comes back for a request, never which store was
called in which order."""

import uuid
from collections.abc import Mapping
from datetime import date

import pytest
from minio import Minio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.evidence import Basis, EvidenceKind, RetrievalPath, parse_evidence_id
from fdi.pipeline.embeddings import FakeEmbeddingClient
from fdi.pipeline.retrieval_service import (
    RetrievalRequest,
    RetrievalService,
    Scope,
    StructuralTarget,
    legacy_search,
    package_body,
)
from fdi.pipeline.vectorstore import FakeVectorStore
from fdi.platform.db.engine import make_session_factory
from fdi.platform.db.models import Document
from fdi.platform.db.repository import CompanyRepository, RegistryRepository
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


NOTE_TEXT = "23. Other income\nInterest income 30\nDividend income 20"
NARRATIVE = "Revenue grew on large digital transformation deals"
STANDALONE = "Standalone segment results for the parent company"
PAGES = {1: NARRATIVE, 2: "Statement of Profit and Loss", 3: STANDALONE, 5: NOTE_TEXT}


def _ocr() -> dict[str, object]:
    content, pages = "", []
    for n, text in sorted(PAGES.items()):
        pages.append({"page_number": n, "spans": [{"offset": len(content), "length": len(text)}]})
        content += text
    return {"content": content, "pages": pages}


def _note_window() -> tuple[int, int]:
    start = len(NARRATIVE) + len("Statement of Profit and Loss") + len(STANDALONE)
    return start, start + len(NOTE_TEXT)


def _pl_table(revenue: str) -> dict[str, object]:
    return {
        "caption": {"content": "Statement of Profit and Loss"},
        "bounding_regions": [{"page_number": 2, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}],
        "grid": {
            "n_rows": 3, "n_cols": 3, "header_row_count": 1,
            "column_headers": ["Particulars", "Note", "FY"],
            "grid": [
                ["Particulars", "Note", "FY"],
                ["Revenue from operations", "22", revenue],
                ["Other income", "23", "50"],
            ],
        },
    }


def _note_table() -> dict[str, object]:
    return {
        "caption": {"content": "Other income"},
        "bounding_regions": [{"page_number": 5}],
        "grid": {
            "n_rows": 3, "n_cols": 2, "header_row_count": 1,
            "column_headers": ["Particulars", "FY"],
            "grid": [["Particulars", "FY"], ["Interest income", "30"], ["Dividend income", "20"]],
        },
    }


def _fog(revenue: float) -> dict[str, object]:
    start, end = _note_window()
    st = "st:consolidated:profit_and_loss"
    return {
        "schema_version": 6,
        "bases": {
            "consolidated": {
                "statements": [{"id": st, "statement": "profit_and_loss",
                                "basis": "consolidated", "page": 2, "table_index": 0}],
                "notes": [{"id": "nt:consolidated:23", "ref": "23", "title": "23. Other income",
                           "page": 5, "start_offset": start, "end_offset": end,
                           "table_indices": [1]}],
                "line_items": [
                    {"id": f"li:{st}:r0", "parent": st, "origin": "face",
                     "label": "Revenue from operations", "caption": "Income", "ref": "22",
                     "current": revenue, "prior": None, "page": 2, "table_index": 0,
                     "row_index": 0, "source_row": 1},
                    {"id": f"li:{st}:r1", "parent": st, "origin": "face",
                     "label": "Other income", "caption": "Income", "ref": "23",
                     "current": 50.0, "prior": None, "page": 2, "table_index": 0,
                     "row_index": 1, "source_row": 2},
                ],
                "edges": [{"type": "REFERENCES", "source": f"li:{st}:r1",
                           "target": "nt:consolidated:23"}],
            },
        },
    }


def _mapping() -> dict[str, object]:
    li = "li:st:consolidated:profit_and_loss:r0"
    return {"bases": {"consolidated": {
        "edges": [{"type": "MAPS_TO", "source": li, "target": "AUTOPL001"}],
        "proof": [{"line_item": li, "method": "lexical", "score": 1.0}],
    }}}


def _chunks(revenue: str) -> dict[str, object]:
    return {"schema_version": 1, "chunks": [
        {"chunk_id": "s0", "heading": "Management discussion", "text": NARRATIVE,
         "section_type": "MDNA", "page": 1},
        {"chunk_id": "s1", "heading": "Standalone results", "text": STANDALONE,
         "section_type": "OTHER", "page": 3},
        {"chunk_id": "t0", "heading": "Statement of Profit and Loss",
         "text": "[Statement of Profit and Loss | PL | basis=consolidated | page 2]\n"
                 f"Columns: Note | FY\nRows: Revenue from operations {revenue}; Other income",
         "section_type": None, "page": 2},
    ]}


BASIS = {"schema_version": 1, "default_basis": "unknown", "regions": [
    {"start_page": 2, "end_page": 2, "basis": "consolidated"},
    {"start_page": 3, "end_page": 3, "basis": "standalone"},
    {"start_page": 5, "end_page": 5, "basis": "consolidated"},
]}


async def _artifact(s: AsyncSession, store: ArtifactStore, doc_id: str, stage: str,
                    body: Mapping[str, object]) -> None:
    repo = RegistryRepository(s)
    run_id = str(uuid.uuid4())
    key = f"test/{doc_id}/{stage}.json"
    await store.put_json(key, body)
    await repo.start_run(run_id, doc_id, stage, 1, uuid.uuid4().hex, None)
    await repo.finish_run(run_id, uuid.uuid4().hex, key)


def _index(vs: FakeVectorStore, emb: FakeEmbeddingClient, doc_id: str,
           chunks: Mapping[str, object]) -> None:
    items = []
    for c in chunks["chunks"]:  # type: ignore[union-attr]
        page = c["page"]
        basis = {2: "consolidated", 3: "standalone", 5: "consolidated"}.get(page, "unknown")
        [vector] = emb.embed([c["text"]])
        items.append({"id": f"{doc_id}:{c['chunk_id']}", "vector": vector, "metadata": {
            "text": c["text"], "doc_id": doc_id, "chunk_id": c["chunk_id"],
            "heading": c["heading"], "section_type": c["section_type"], "page": page,
            "basis": basis, "kind": "table" if c["chunk_id"].startswith("t") else "passage",
        }})
    vs.upsert(items)


class Corpus:
    def __init__(self, company_id: str, docs: dict[str, str]) -> None:
        self.company_id = company_id
        self.docs = docs  # label -> doc_id
        self.v26_doc_key = ""


@pytest.fixture
async def corpus(session_factory, store) -> tuple[Corpus, FakeVectorStore, FakeEmbeddingClient]:
    vs, emb = FakeVectorStore(), FakeEmbeddingClient()
    tag = uuid.uuid4().hex[:8]
    async with session_factory() as s:
        company, _ = await CompanyRepository(s).get_or_create_by_cin(
            cin=f"L{tag.upper():0<20}"[:21], name=f"ACME {tag}"
        )
        specs = [
            ("2026v1", f"acme-2026-{tag}", 1, "FY2025-26", date(2026, 3, 31), "900"),
            ("2026v2", f"acme-2026-{tag}", 2, "FY2025-26", date(2026, 3, 31), "1,000"),
            ("2025", f"acme-2025-{tag}", 1, "FY2024-25", date(2025, 3, 31), "800"),
            ("2024", f"acme-2024-{tag}", 1, "FY2023-24", date(2024, 3, 31), None),
        ]
        docs: dict[str, str] = {}
        for label, doc_key, version, fy, end, revenue in specs:
            doc_id = str(uuid.uuid4())
            s.add(Document(id=doc_id, doc_key=doc_key, version=version, company_id=company.id,
                           fiscal_year=fy, period_end=end, link_status="linked"))
            await s.flush()
            docs[label] = doc_id
            if revenue is None:
                continue  # filed, never processed
            chunks = _chunks(revenue)
            for stage, body in (
                ("chunks", chunks), ("basis", BASIS), ("ocr", _ocr()),
                ("structure", {"tables": [_pl_table(revenue), _note_table()]}),
                ("fog_objects", _fog(float(revenue.replace(",", "")))),
                ("fog_mapping", _mapping()), ("index", {"upserted": 3}),
            ):
                await _artifact(s, store, doc_id, stage, body)
            _index(vs, emb, doc_id, chunks)
        await s.commit()
    c = Corpus(company.id, docs)
    return c, vs, emb


def _service(s: AsyncSession, store: ArtifactStore, vs: object, emb: object) -> RetrievalService:
    return RetrievalService(session=s, store=store, embedder=emb, vector_store=vs)  # type: ignore[arg-type]


def _doc_ids(package) -> set[str]:  # type: ignore[no-untyped-def]
    return {e.report.doc_id for e in package.evidence}


async def test_company_scope_covers_latest_versions_only(session_factory, store, corpus) -> None:
    c, vs, emb = corpus
    async with session_factory() as s:
        package = await _service(s, store, vs, emb).retrieve(RetrievalRequest(
            query="revenue digital deals", scope=Scope(company_id=c.company_id),
            paths=(RetrievalPath.HYBRID,), k=20,
        ))
    assert package.path("hybrid").status == "ok"
    assert c.docs["2026v1"] not in _doc_ids(package)
    assert _doc_ids(package) <= {c.docs["2026v2"], c.docs["2025"]}
    assert {o.report.doc_id for o in package.reports} == {
        c.docs["2026v2"], c.docs["2025"], c.docs["2024"]}


async def test_explicit_version_reproduces_an_old_report(session_factory, store, corpus) -> None:
    c, vs, emb = corpus
    async with session_factory() as s:
        doc = await s.get(Document, c.docs["2026v1"])
        assert doc is not None
        package = await _service(s, store, vs, emb).retrieve(RetrievalRequest(
            query="revenue", scope=Scope(doc_keys=(doc.doc_key,), version=1),
            paths=(RetrievalPath.HYBRID,), k=20,
        ))
    assert _doc_ids(package) == {c.docs["2026v1"]}


async def test_basis_scope_returns_only_that_basis(session_factory, store, corpus) -> None:
    c, vs, emb = corpus
    async with session_factory() as s:
        package = await _service(s, store, vs, emb).retrieve(RetrievalRequest(
            query="revenue standalone results digital", k=20, paths=(RetrievalPath.HYBRID,),
            scope=Scope(company_id=c.company_id, bases=(Basis.CONSOLIDATED,)),
        ))
    assert package.evidence
    assert {e.basis for e in package.evidence} == {"consolidated"}


async def test_company_and_fiscal_year_scope(session_factory, store, corpus) -> None:
    c, vs, emb = corpus
    async with session_factory() as s:
        package = await _service(s, store, vs, emb).retrieve(RetrievalRequest(
            query="revenue", k=20, paths=(RetrievalPath.HYBRID,),
            scope=Scope(company_id=c.company_id, fiscal_years=("FY2024-25",)),
        ))
    assert _doc_ids(package) == {c.docs["2025"]}
    assert {e.report.fiscal_year for e in package.evidence} == {"FY2024-25"}


async def test_same_passage_from_both_channels_is_one_evidence(
    session_factory, store, corpus
) -> None:
    c, vs, emb = corpus
    async with session_factory() as s:
        package = await _service(s, store, vs, emb).retrieve(RetrievalRequest(
            query=NARRATIVE, k=20, paths=(RetrievalPath.HYBRID,),
            scope=Scope(doc_ids=(c.docs["2026v2"],)),
        ))
    ids = [e.evidence_id for e in package.evidence]
    assert len(ids) == len(set(ids))
    assert ids[0] == f"{c.docs['2026v2']}:s0"  # exact text: top of both channels


async def test_consolidated_two_year_search_has_stable_inspectable_ids(
    session_factory, store, corpus
) -> None:
    """Design §17.3 acceptance: a consolidated-only, two-Fiscal-year search
    for one Company returns correctly scoped Evidence with stable ids that
    inspect resolves."""
    c, vs, emb = corpus
    request = RetrievalRequest(
        query="revenue from operations", k=20,
        scope=Scope(company_id=c.company_id, fiscal_years=("FY2025-26", "FY2024-25"),
                    bases=(Basis.CONSOLIDATED,)),
    )
    async with session_factory() as s:
        first = await _service(s, store, vs, emb).retrieve(request)
    async with session_factory() as s:
        second = await _service(s, store, vs, emb).retrieve(request)
        assert [e.evidence_id for e in first.evidence] == [e.evidence_id for e in second.evidence]
        assert first.by_fiscal_year is not None  # auto -> cross-document
        assert [g.fiscal_year for g in first.by_fiscal_year] == ["FY2025-26", "FY2024-25"]
        assert _doc_ids(first) == {c.docs["2026v2"], c.docs["2025"]}
        assert {e.basis for e in first.evidence} == {"consolidated"}
        for e in first.evidence:
            inspected = await _service(s, store, vs, emb).inspect(e.evidence_id)
            assert inspected is not None and inspected.evidence.evidence_id == e.evidence_id


async def test_cross_document_marks_matched_and_not_processed(
    session_factory, store, corpus
) -> None:
    c, vs, emb = corpus
    async with session_factory() as s:
        package = await _service(s, store, vs, emb).retrieve(RetrievalRequest(
            concept="AUTOPL001", scope=Scope(company_id=c.company_id),
            paths=(RetrievalPath.CROSS_DOCUMENT,),
        ))
    status = {o.report.doc_id: o.status.value for o in package.reports}
    assert status == {c.docs["2026v2"]: "matched", c.docs["2025"]: "matched",
                      c.docs["2024"]: "not_processed"}
    values = {e.report.fiscal_year: e.value for e in package.evidence}
    assert values == {"FY2025-26": 1000.0, "FY2024-25": 800.0}
    assert package.by_fiscal_year is not None
    assert [g.fiscal_year for g in package.by_fiscal_year] == [
        "FY2025-26", "FY2024-25", "FY2023-24"]
    body = package_body(package)
    assert body["by_fiscal_year"][2]["reports"][0]["status"] == "not_processed"  # type: ignore[index]


async def test_unmapped_concept_is_no_match_not_not_processed(
    session_factory, store, corpus
) -> None:
    c, vs, emb = corpus
    async with session_factory() as s:
        package = await _service(s, store, vs, emb).retrieve(RetrievalRequest(
            concept="AUTOBS999", scope=Scope(doc_ids=(c.docs["2025"],)),
        ))
    assert package.evidence == ()
    assert [o.status.value for o in package.reports] == ["no_match"]


async def test_structured_fact_links_to_its_line_item(session_factory, store, corpus) -> None:
    c, vs, emb = corpus
    doc = c.docs["2026v2"]
    async with session_factory() as s:
        service = _service(s, store, vs, emb)
        package = await service.retrieve(RetrievalRequest(
            concept="AUTOPL001", scope=Scope(doc_ids=(doc,), bases=(Basis.CONSOLIDATED,)),
        ))
        [fact] = package.evidence
        assert fact.evidence_id == f"{doc}:fact:consolidated:AUTOPL001"
        assert fact.kind is EvidenceKind.FACT and fact.value == 1000.0
        assert fact.links == (f"{doc}:li:st:consolidated:profit_and_loss:r0",)
        inspected = await service.inspect(fact.evidence_id)
    assert inspected is not None
    assert inspected.context["fact"]["value"] == 1000.0  # type: ignore[index]
    assert "Revenue from operations" in str(inspected.context["table"])


async def test_inspect_cell_returns_the_full_grid(session_factory, store, corpus) -> None:
    c, vs, emb = corpus
    doc = c.docs["2026v2"]
    async with session_factory() as s:
        inspected = await _service(s, store, vs, emb).inspect(f"{doc}:t0:r1:c2")
    assert inspected is not None
    assert inspected.evidence.kind is EvidenceKind.TABLE_CELL
    assert inspected.context["cell"] == {
        "row": 1, "col": 2, "value": "1,000",
        "row_label": "Revenue from operations", "column_header": "FY",
    }
    grid = inspected.context["grid"]
    assert grid["rows"][2] == ["Other income", "23", "50"]  # type: ignore[index]
    assert inspected.evidence.page == 2 and inspected.evidence.basis == "consolidated"


async def test_inspect_resolves_page_passage_and_note(session_factory, store, corpus) -> None:
    c, vs, emb = corpus
    doc = c.docs["2026v2"]
    async with session_factory() as s:
        service = _service(s, store, vs, emb)
        page = await service.inspect(f"{doc}:p3")
        passage = await service.inspect(f"{doc}:s0")
        note = await service.inspect(f"{doc}:note:consolidated:23")
        missing = [await service.inspect(i) for i in (
            f"{doc}:p99", f"{doc}:t9", f"{doc}:t0:r9:c0", f"{uuid.uuid4()}:s0", "junk",
        )]
    assert page is not None and page.context["page_text"] == STANDALONE
    assert page.evidence.basis == "standalone"
    assert passage is not None and passage.context["page_text"] == NARRATIVE
    assert note is not None and note.context["text"] == NOTE_TEXT
    assert note.evidence.links == (f"{doc}:t1",)
    assert note.context["tables"][0]["evidence_id"] == f"{doc}:t1"  # type: ignore[index]
    assert missing == [None] * 5


async def test_statement_line_follows_to_its_note(session_factory, store, corpus) -> None:
    c, vs, emb = corpus
    doc = c.docs["2026v2"]
    line = f"{doc}:li:st:consolidated:profit_and_loss:r1"
    async with session_factory() as s:
        service = _service(s, store, vs, emb)
        package = await service.retrieve(RetrievalRequest(
            targets=(StructuralTarget(target="references", evidence_id=line),),
        ))
        inspected = await service.inspect(line)
    [note] = package.evidence
    assert note.evidence_id == f"{doc}:note:consolidated:23"
    assert note.content == NOTE_TEXT and line in note.links
    assert inspected is not None
    assert inspected.context["references"] == [f"{doc}:note:consolidated:23"]


async def test_statement_target_returns_the_whole_table(session_factory, store, corpus) -> None:
    c, vs, emb = corpus
    async with session_factory() as s:
        package = await _service(s, store, vs, emb).retrieve(RetrievalRequest(
            targets=(StructuralTarget(target="statement", statement="profit_and_loss"),),
            scope=Scope(company_id=c.company_id, fiscal_years=("FY2025-26",)),
        ))
    [table] = package.evidence
    assert table.evidence_id == f"{c.docs['2026v2']}:t0"
    assert "Other income" in table.content and "1,000" in table.content
    assert table.basis == "consolidated" and table.page == 2
    assert table.links == ()  # the P&L sits in no note window; note 23 holds t1


async def test_auto_expands_top_table_hits_to_full_grids(session_factory, store, corpus) -> None:
    c, vs, emb = corpus
    doc = c.docs["2026v2"]
    async with session_factory() as s:
        package = await _service(s, store, vs, emb).retrieve(RetrievalRequest(
            query="Statement of Profit and Loss revenue other income", k=5,
            scope=Scope(doc_ids=(doc,)),
        ))
    table = next(e for e in package.evidence if e.evidence_id == f"{doc}:t0")
    assert "| Other income |" in table.content  # the grid, not the location card
    assert table.retrieval_paths == ("hybrid", "structural")
    assert package.path("structural").status == "ok"


class _DownVectorStore(FakeVectorStore):
    def search(self, vector, k, *, filter=""):  # type: ignore[no-untyped-def]
        raise ConnectionError("milvus is down")

    def search_text(self, query, k, *, filter=""):  # type: ignore[no-untyped-def]
        raise ConnectionError("milvus is down")


async def test_vector_outage_degrades_hybrid_only(session_factory, store, corpus) -> None:
    c, _vs, emb = corpus
    async with session_factory() as s:
        package = await _service(s, store, _DownVectorStore(), emb).retrieve(RetrievalRequest(
            query="revenue", concept="AUTOPL001",
            targets=(StructuralTarget(target="note", note_ref="23"),),
            scope=Scope(doc_ids=(c.docs["2026v2"],)),
        ))
    assert package.path("hybrid").status == "degraded"
    assert "milvus is down" in (package.path("hybrid").reason or "")
    assert package.path("structured").status == "ok"
    assert package.path("structural").status == "ok"
    kinds = {e.kind for e in package.evidence}
    assert kinds == {EvidenceKind.FACT, EvidenceKind.NOTE}


async def test_unconfigured_vector_store_degrades_not_raises(
    session_factory, store, corpus
) -> None:
    c, _vs, _emb = corpus
    async with session_factory() as s:
        package = await RetrievalService(session=s, store=store).retrieve(
            RetrievalRequest(query="revenue", scope=Scope(doc_ids=(c.docs["2025"],)))
        )
    assert package.path("hybrid").status == "degraded"


@pytest.mark.parametrize("scope", [
    {"doc_ids": ['x" or doc_id != "']},
    {"company_id": "1; drop table document"},
    {"fiscal_years": ["2025 or 1=1"]},
    {"bases": ["consolidated\" or \"1"]},
    {"kinds": ["chunk"]},
    {"section_types": ["NOT_A_TYPE"]},
    {"version": 2},
    {"evil": "x"},
])
def test_malicious_scope_is_rejected_before_any_io(scope: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RetrievalRequest.model_validate({"query": "x", "scope": scope})


def test_request_needs_something_to_ask() -> None:
    with pytest.raises(ValidationError):
        RetrievalRequest()
    with pytest.raises(ValidationError):
        RetrievalRequest(concept="AUTOPL001", paths=(RetrievalPath.HYBRID,))
    with pytest.raises(ValidationError):
        StructuralTarget(target="references", evidence_id="not-an-id")


async def test_legacy_search_keeps_pre_s4_semantics(session_factory, store, corpus) -> None:
    c, vs, emb = corpus
    async with session_factory() as s:
        doc = await s.get(Document, c.docs["2026v1"])
        assert doc is not None
        service = _service(s, store, vs, emb)
        hits = await legacy_search(service, session=s, query="revenue", k=20,
                                   doc_key=doc.doc_key)
        none = await legacy_search(service, session=s, query="revenue", k=5,
                                   section_type="NOT_A_TYPE")
    # every version of the doc_key, as before S4
    assert {h["doc_id"] for h in hits} == {c.docs["2026v1"], c.docs["2026v2"]}
    for h in hits:
        assert parse_evidence_id(h["evidence_id"]) is not None
        assert {"doc_key", "version", "chunk_id", "heading", "text", "score"} <= set(h)
    assert none == []
