"""The Financial fact service (S5) against real Postgres + MinIO: what facts,
history, gaps and Metrics an Invitee gets once Reports are projected, and that
the projection is idempotent, rebuildable and lossless.

Fixture (`financial_fixtures.acme_specs`): one Company with FY2025-26 (v1
superseded by v2), FY2024-25 -- restated in FY2025-26's comparative column --
and FY2022-23 filed but never processed."""

import asyncio
from decimal import Decimal

import pytest
import sqlalchemy as sa
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.evidence import Basis, RetrievalPath
from fdi.core.financial_facts import FactStatus, GapReason, Source
from fdi.pipeline.financial_facts import FinancialFactService, project_document
from fdi.pipeline.retrieval_service import RetrievalRequest, RetrievalService, Scope
from fdi.platform.db.engine import make_session_factory
from fdi.platform.db.models import (
    AppUser,
    Document,
    ExtractionField,
    ExtractionSchema,
    ExtractionSchemaVersion,
    FieldRead,
    FinancialFactRow,
    FinancialObservationRow,
    FinancialProjection,
    FinancialValidationRow,
    ValueReview,
)
from fdi.platform.db.repository import CompanyRepository, DocumentRepository
from fdi.platform.storage.artifact_store import ArtifactStore
from tests.integration.financial_fixtures import (
    SCHEMA,
    ReportSpec,
    Row,
    Seeded,
    acme_specs,
    seed_company,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(migrated_engine)


@pytest.fixture
async def store(minio_client: Minio) -> ArtifactStore:
    s = ArtifactStore(minio_client, bucket="test-artifacts")
    await s.ensure_bucket()
    return s


def _service(s: AsyncSession, store: ArtifactStore) -> FinancialFactService:
    return FinancialFactService(session=s, store=store, formulas_schema=SCHEMA)


async def _project_all(session_factory, store, seeded: Seeded, *, force: bool = False):  # type: ignore[no-untyped-def]
    out = {}
    for label, doc_id in seeded.docs.items():
        async with session_factory() as s:
            out[label] = await _service(s, store).project(doc_id, force=force)
    return out


@pytest.fixture
async def acme(session_factory, store) -> Seeded:
    async with session_factory() as s:
        await _service(s, store).seed_concepts(SCHEMA)
        await s.commit()
        seeded = await seed_company(s, store, acme_specs())
    await _project_all(session_factory, store, seeded)
    return seeded


async def _snapshot(s: AsyncSession, company_id: str) -> tuple[list[tuple], list[tuple]]:  # type: ignore[type-arg]
    obs = (await s.execute(
        sa.select(FinancialObservationRow).where(FinancialObservationRow.company_id == company_id)
        .order_by(FinancialObservationRow.id))).scalars().all()
    facts = (await s.execute(
        sa.select(FinancialFactRow).where(FinancialFactRow.company_id == company_id)
        .order_by(FinancialFactRow.id))).scalars().all()
    return (
        [(o.id, o.concept, o.fiscal_year, o.source, str(o.value), o.printed, o.unit)
         for o in obs],
        [(f.id, f.status, str(f.value), f.selected_observation_id, f.restated,
          tuple(f.observation_ids)) for f in facts],
    )


async def test_a_three_year_consolidated_history_with_sources_and_validation(
    session_factory, store, acme: Seeded,
) -> None:
    async with session_factory() as s:
        result = await _service(s, store).history(
            company_id=acme.company_id, concepts=["RFOGRS", "PATR"], basis="consolidated")
    by = {(f.concept, f.fiscal_year): f for f in result.facts}
    assert sorted({fy for _, fy in by}) == ["FY2023-24", "FY2024-25", "FY2025-26"]

    rev26 = by[("RFOGRS", "FY2025-26")]
    assert (rev26.status, rev26.value, rev26.selected.printed) == (
        FactStatus.REPORTED, Decimal("267021"), "2,67,021")
    assert rev26.unit.label == "INR crore"
    assert rev26.selected.doc_id == acme.docs["2026"]          # v2, never the superseded v1
    assert rev26.selected.evidence_id == f"{acme.docs['2026']}:fact:consolidated:RFOGRS"

    pat25 = by[("PATR", "FY2024-25")]
    assert pat25.status is FactStatus.REPORTED and not pat25.restated
    assert {o.doc_id for o in pat25.observations} == {acme.docs["2026"], acme.docs["2025"]}
    assert pat25.selected.doc_id == acme.docs["2026"]          # the latest Report states it

    # Validation: FY2024-25's own PATR check failed; the selected (FY2025-26)
    # observation passed its own.
    assert pat25.selected.validation.value == "passed"
    own = next(o for o in pat25.observations if o.doc_id == acme.docs["2025"])
    assert own.validation.value == "failed"
    checks = result.validations[next(iter(result.validations))]
    assert all(isinstance(c, FinancialValidationRow) for c in checks)


async def test_the_fy2024_25_report_and_the_next_reports_comparative_are_reconciled(
    session_factory, store, acme: Seeded,
) -> None:
    async with session_factory() as s:
        fact, gap, _ = await _service(s, store).fact(
            company_id=acme.company_id, concept="RFOGRS", fiscal_year="FY2024-25",
            basis="consolidated")
    assert gap is None and fact is not None
    assert fact.status is FactStatus.RESTATED
    assert fact.value == Decimal("255000")                     # latest wins
    assert fact.restatement is not None
    assert fact.restatement.original.value == Decimal("255324")
    assert fact.restatement.original.doc_id == acme.docs["2025"]
    assert fact.restatement.difference == Decimal("-3240000000")


async def test_gaps_say_why_a_fact_is_absent(session_factory, store, acme: Seeded) -> None:
    async with session_factory() as s:
        result = await _service(s, store).facts(
            company_id=acme.company_id, concepts=["PBTR", "TOTCAR", "EOI"],
            fiscal_years=["FY2025-26", "FY2022-23", "FY2030-31"], basis="consolidated")
    gaps = {(g.concept, g.fiscal_year): g.reason for g in result.gaps}
    assert gaps[("PBTR", "FY2025-26")] is GapReason.MISSED            # asked, unanswered
    assert gaps[("PBTR", "FY2022-23")] is GapReason.NOT_PROCESSED     # filed, not processed
    assert gaps[("TOTCAR", "FY2030-31")] is GapReason.NO_REPORT
    assert gaps[("TOTCAR", "FY2022-23")] is GapReason.NOT_PROCESSED
    eoi = next(f for f in result.facts if f.concept == "EOI")
    assert eoi.status is FactStatus.NOT_REPORTED and eoi.value is None  # printed "-"
    assert eoi.selected.printed == "-"


async def test_reprojecting_is_a_no_op_and_forcing_it_changes_nothing(
    session_factory, store, acme: Seeded,
) -> None:
    async with session_factory() as s:
        before = await _snapshot(s, acme.company_id)
    again = await _project_all(session_factory, store, acme)
    assert {o.status for o in again.values()} == {"unchanged"}
    forced = await _project_all(session_factory, store, acme, force=True)
    assert forced["2026"].status == "projected"
    async with session_factory() as s:
        assert await _snapshot(s, acme.company_id) == before


async def test_dropping_the_projection_and_rebuilding_it_gives_identical_facts(
    session_factory, store, acme: Seeded,
) -> None:
    async with session_factory() as s:
        before = await _snapshot(s, acme.company_id)
        docs = list(acme.docs.values())
        await s.execute(sa.delete(FinancialFactRow).where(
            FinancialFactRow.company_id == acme.company_id))
        for model in (FinancialObservationRow, FinancialValidationRow, FinancialProjection):
            await s.execute(sa.delete(model).where(model.doc_id.in_(docs)))
        await s.commit()
        assert (await _snapshot(s, acme.company_id)) == ([], [])
    await _project_all(session_factory, store, acme)
    async with session_factory() as s:
        assert await _snapshot(s, acme.company_id) == before


async def test_the_projection_is_lossless_against_the_reasoning_artifact(
    session_factory, store, acme: Seeded,
) -> None:
    async with session_factory() as s:
        values = await _service(s, store).report_values(acme.docs["2026"])
    assert {t: v["current"] for t, v in values["consolidated"].items()} == {
        "RFOGRS": "267021", "TOTOTIN": "4402", "PATR": "49454", "TOTCAR": "135705",
        "TOTCLR": "60914"}
    assert values["consolidated"]["RFOGRS"]["prior"] == "255000"


async def test_a_report_filed_under_another_company_moves_its_facts(
    session_factory, store, acme: Seeded,
) -> None:
    async with session_factory() as s:
        other, _ = await CompanyRepository(s).get_or_create_by_cin(
            cin="L99999ZZ2026PLC999999", name="Other Co")
        doc = await s.get(Document, acme.docs["2025"])
        assert doc is not None
        doc.company_id = other.id
        await s.commit()
        other_id = other.id
    async with session_factory() as s:
        outcome = await _service(s, store).project(acme.docs["2025"])
    assert set(outcome.companies) == {acme.company_id, other_id}
    async with session_factory() as s:
        service = _service(s, store)
        moved = await service.facts(company_id=other_id, concepts=["PATR"],
                                    fiscal_years=["FY2024-25"], basis="consolidated")
        stayed = await service.facts(company_id=acme.company_id, concepts=["PATR"],
                                     fiscal_years=["FY2024-25"], basis="consolidated")
    assert moved.facts[0].selected.doc_id == acme.docs["2025"]
    assert {o.doc_id for o in stayed.facts[0].observations} == {acme.docs["2026"]}


async def test_deleting_a_report_falls_back_to_the_next_reports_figure(
    session_factory, store, acme: Seeded,
) -> None:
    async with session_factory() as s:
        await DocumentRepository(s).delete(acme.docs["2026v1"])
        await DocumentRepository(s).delete(acme.docs["2026"])
        await s.commit()
        await _service(s, store).rebuild_company(acme.company_id)
    async with session_factory() as s:
        fact, _, _ = await _service(s, store).fact(
            company_id=acme.company_id, concept="RFOGRS", fiscal_year="FY2024-25",
            basis="consolidated")
    assert fact is not None and fact.status is FactStatus.REPORTED
    assert fact.value == Decimal("255324") and fact.selected.doc_id == acme.docs["2025"]


async def test_an_unfiled_or_unprocessed_report_is_skipped_with_why(
    session_factory, store, acme: Seeded,
) -> None:
    async with session_factory() as s:
        unprocessed = await _service(s, store).project(acme.docs["2023"])
    assert unprocessed.status in ("skipped", "unchanged")
    assert unprocessed.reason.startswith("not processed")
    async with session_factory() as s:
        doc = Document(id="11111111-2222-3333-4444-555555555555", doc_key="orphan")
        s.add(doc)
        await s.commit()
        outcome = await _service(s, store).project(doc.id)
        await DocumentRepository(s).delete(doc.id)
        await s.commit()
    assert (outcome.status, outcome.reason) == ("skipped", "not filed under a Company")


async def test_an_invitees_correction_becomes_the_fact(
    session_factory, store, acme: Seeded,
) -> None:
    doc_id = acme.docs["2026"]
    async with session_factory() as s:
        user = AppUser(id="u-" + doc_id[:30], email=f"{doc_id}@example.test",
                       password_hash="x")
        schema = ExtractionSchema(id="s-" + doc_id[:30], name="Mine", owner_user_id=user.id)
        version = ExtractionSchemaVersion(id="v-" + doc_id[:30], schema_id=schema.id,
                                          version=1, definition_hash="h", review_threshold=0.5,
                                          groups=[], field_count=2)
        s.add_all([user, schema, version])
        await s.flush()
        for fid, tag in (("f-pat", "PATR"), ("f-pbt", "PBTR")):
            s.add(ExtractionField(id=f"{fid}-{doc_id[:20]}", version_id=version.id,
                                  field_id=f"{fid}-{doc_id[:20]}", position=0 if tag == "PATR"
                                  else 1, name=tag, tag=tag, origin="extracted",
                                  definition_hash="h", definition={}))
        reads = {
            "PATR": FieldRead(id="r-pat-" + doc_id[:29], doc_id=doc_id,
                              field_id=f"f-pat-{doc_id[:20]}", cache_key="k1",
                              definition_hash="h", prompt_version="p", retrieval_version="r",
                              model="m", status="extracted", value=49454,
                              basis="consolidated", provenance={}, answered=True),
            "PBTR": FieldRead(id="r-pbt-" + doc_id[:29], doc_id=doc_id,
                              field_id=f"f-pbt-{doc_id[:20]}", cache_key="k2",
                              definition_hash="h", prompt_version="p", retrieval_version="r",
                              model="m", status="extracted", value=65000,
                              basis="consolidated", provenance={}, answered=True),
        }
        s.add_all(reads.values())
        await s.flush()
        s.add_all([
            ValueReview(id="rv1-" + doc_id[:30], read_id=reads["PATR"].id, action="corrected",
                        value=49500, user_id=user.id),
            ValueReview(id="rv2-" + doc_id[:30], read_id=reads["PBTR"].id, action="accepted",
                        value=None, user_id=user.id),
        ])
        await s.commit()
    async with session_factory() as s:
        outcome = await _service(s, store).project(doc_id)
    assert outcome.status == "projected"             # reviewed values changed the key
    async with session_factory() as s:
        result = await _service(s, store).facts(
            company_id=acme.company_id, concepts=["PATR", "PBTR"], fiscal_years=["FY2025-26"],
            basis="consolidated")
    by = {f.concept: f for f in result.facts}
    assert (by["PATR"].status, by["PATR"].value) == (FactStatus.CORRECTED, Decimal("49500"))
    assert by["PBTR"].selected.source is Source.EXTRACTION_ACCEPTED
    assert by["PBTR"].value == Decimal("65000") and by["PBTR"].unit.label == "INR crore"


async def test_metrics_are_computed_from_facts_and_refuse_with_reasons(
    session_factory, store, acme: Seeded,
) -> None:
    async with session_factory() as s:
        service = _service(s, store)
        [ratio] = await service.calculate(metric="current_ratio", company_id=acme.company_id,
                                          fiscal_years=["FY2025-26"], basis="consolidated")
        growth, verdicts = await service.compare_periods(
            metric="revenue_growth", company_id=acme.company_id,
            fiscal_years=["FY2025-26", "FY2024-25"], basis="consolidated")
    assert (ratio.status.value, ratio.value) == ("ok", Decimal("2.2278"))
    assert [g.fiscal_year for g in growth] == ["FY2024-25", "FY2025-26"]
    # FY2025-26 vs the RESTATED FY2024-25 revenue: 267021 / 255000 - 1.
    assert growth[1].value == Decimal("4.7141")
    assert growth[1].warnings == ("restated_input:RFOGRS FY2024-25",)
    assert growth[0].value == Decimal("5.8561")           # 255000 / 240893 - 1
    assert not verdicts[0].comparable
    assert "restated_input:RFOGRS FY2024-25" in verdicts[0].reasons


async def test_two_reports_of_one_company_projected_at_once_stay_consistent(
    session_factory, store,
) -> None:
    async with session_factory() as s:
        await _service(s, store).seed_concepts(SCHEMA)
        await s.commit()
        seeded = await seed_company(s, store, {k: v for k, v in acme_specs().items()
                                               if k in ("2026", "2025")})

    async def one(doc_id: str):  # type: ignore[no-untyped-def]
        async with session_factory() as s:
            return await _service(s, store).project(doc_id)

    outcomes = await asyncio.gather(*(one(d) for d in seeded.docs.values()))
    assert {o.status for o in outcomes} == {"projected"}
    async with session_factory() as s:
        fact, _, _ = await _service(s, store).fact(
            company_id=seeded.company_id, concept="RFOGRS", fiscal_year="FY2024-25",
            basis="consolidated")
    assert fact is not None and fact.status is FactStatus.RESTATED
    assert len(fact.observations) == 2


async def test_project_document_seeds_concepts_from_the_standard_schema_file(
    session_factory, store, tmp_path,
) -> None:
    import json

    path = tmp_path / "standard.json"
    path.write_text(json.dumps(SCHEMA), encoding="utf-8")
    async with session_factory() as s:
        seeded = await seed_company(s, store, {"2025": ReportSpec(
            "solo-2025", "FY2024-25", acme_specs()["2025"].period_end,
            rows=[Row("Profit for the year", "PATR", "1,000", "900")])})
    outcome = await project_document(
        session_factory=session_factory, store=store, doc_id=seeded.docs["2025"],
        formulas_schema=SCHEMA, standard_schema_path=str(path))
    assert (outcome.status, outcome.observations) == ("projected", 2)


async def test_the_structured_retrieval_path_answers_from_financial_facts(
    session_factory, store, acme: Seeded,
) -> None:
    doc = acme.docs["2026"]
    async with session_factory() as s:
        service = RetrievalService(session=s, store=store)
        package = await service.retrieve(RetrievalRequest(
            concept="RFOGRS", scope=Scope(doc_ids=(doc,), bases=(Basis.CONSOLIDATED,)),
            paths=(RetrievalPath.STRUCTURED,)))
        [fact] = package.evidence
        inspected = await service.inspect(fact.evidence_id)
    assert fact.evidence_id == f"{doc}:fact:consolidated:RFOGRS"
    assert "2,67,021 INR crore" in fact.content and fact.value == 267021.0
    assert fact.links == (f"{doc}:li:st:consolidated:profit_and_loss:r0",)
    assert inspected is not None
    assert inspected.context["fact"]["printed"] == "2,67,021"  # type: ignore[index]
    assert inspected.context["fact"]["normalised"] == "2670210000000"  # type: ignore[index]
