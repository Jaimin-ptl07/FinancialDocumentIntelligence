"""`/api/v1/reports`: a Report awaiting confirmation, and any Invitee
confirming or correcting its Company and Fiscal year (S2.3, #50).

No roles: whoever is logged in may do it, and the response says who changed
the Report and when."""

import random
import string
import uuid

import pytest

from fdi.pipeline.companies import CompanyRegistry
from tests.integration.conftest import acme_report, fresh_cin, ingest

pytestmark = pytest.mark.integration


def unique_name() -> str:
    word = "".join(random.choices(string.ascii_lowercase, k=10)).capitalize()
    return f"{word} Power Limited"


async def awaiting_report(session_factory, store, name: str) -> str:
    doc_id = await ingest(
        session_factory, store, doc_key=f"await-{uuid.uuid4()}",
        lines=[f"{name} had a record year.", "for the year ended 31st March 2026"],
    )
    async with session_factory() as s:
        await CompanyRegistry(s, store).resolve_report(doc_id)
        await s.commit()
    return doc_id


async def test_a_report_awaiting_confirmation_shows_its_candidates_and_reason(
    make_client, invitee, session_factory, store
) -> None:
    name = unique_name()
    async with make_client(invitee) as client:
        known = (
            await client.post("/api/v1/companies", json={"name": name, "cin": fresh_cin()})
        ).json()
    doc_id = await awaiting_report(session_factory, store, name)

    async with make_client(invitee) as client:
        one = await client.get(f"/api/v1/reports/{doc_id}")
        waiting = await client.get("/api/v1/reports/awaiting-confirmation")

    assert one.status_code == 200
    body = one.json()
    assert (body["link_status"], body["company_id"], body["link_method"]) == (
        "awaiting_confirmation", None, None,
    )
    assert "no CIN" in body["link_reason"]
    assert body["candidates"]["names"] == [name]
    assert body["candidates"]["cins"] == []
    assert [c["id"] for c in body["candidates"]["companies"]] == [known["id"]]
    assert body["last_change"] is None
    assert doc_id in [r["doc_id"] for r in waiting.json()["reports"]]


async def test_any_invitee_confirms_and_another_corrects_and_each_is_shown_who_and_when(
    make_client, invitee, another_invitee, session_factory, store
) -> None:
    name = unique_name()
    doc_id = await awaiting_report(session_factory, store, name)
    async with make_client(invitee) as client:
        first = (
            await client.post("/api/v1/companies", json={"name": name, "cin": fresh_cin()})
        ).json()
        second = (
            await client.post("/api/v1/companies", json={"name": unique_name(), "cin": fresh_cin()})
        ).json()
        confirmed = await client.put(
            f"/api/v1/reports/{doc_id}/company", json={"company_id": first["id"]}
        )

    assert confirmed.status_code == 200
    body = confirmed.json()
    assert (body["company_id"], body["link_status"], body["link_method"]) == (
        first["id"], "linked", "confirmed_by_invitee",
    )
    assert body["candidates"] is None
    assert (body["last_change"]["action"], body["last_change"]["invitee_email"]) == (
        "confirm", invitee[0],
    )
    assert body["last_change"]["at"]

    async with make_client(another_invitee) as client:
        corrected = await client.put(
            f"/api/v1/reports/{doc_id}/company", json={"company_id": second["id"]}
        )
        year = await client.put(
            f"/api/v1/reports/{doc_id}/fiscal-year", json={"fiscal_year": "FY2024-25"}
        )
        changes = await client.get(f"/api/v1/reports/{doc_id}/changes")
        under_second = await client.get(f"/api/v1/companies/{second['id']}/reports")
        under_first = await client.get(f"/api/v1/companies/{first['id']}/reports")

    assert corrected.status_code == 200
    assert corrected.json()["company_id"] == second["id"]
    assert (corrected.json()["last_change"]["action"], corrected.json()["last_change"][
        "invitee_email"
    ]) == ("correct_company", another_invitee[0])
    assert year.status_code == 200
    assert (year.json()["fiscal_year"], year.json()["period_end"]) == ("FY2024-25", "2025-03-31")
    assert year.json()["fiscal_year_method"] == "confirmed_by_invitee"
    log = changes.json()["changes"]
    assert [(c["action"], c["invitee_email"]) for c in log] == [
        ("confirm", invitee[0]),
        ("correct_company", another_invitee[0]),
        ("correct_fiscal_year", another_invitee[0]),
    ]
    assert all(c["at"] for c in log)
    assert [r["doc_id"] for r in under_second.json()["reports"]] == [doc_id]
    assert under_first.json()["reports"] == []


async def test_confirming_a_cin_linked_report_keeps_its_company_and_records_the_confirm(
    make_client, invitee, session_factory, store
) -> None:
    doc_id = await ingest(
        session_factory, store, doc_key=f"acme-{uuid.uuid4()}", lines=acme_report(fresh_cin())
    )
    async with session_factory() as s:
        company_id = (await CompanyRegistry(s, store).resolve_report(doc_id)).company_id
        await s.commit()

    async with make_client(invitee) as client:
        before = (await client.get(f"/api/v1/reports/{doc_id}")).json()
        confirmed = await client.put(
            f"/api/v1/reports/{doc_id}/company", json={"company_id": company_id}
        )

    assert (before["link_method"], before["link_reason"] is not None) == ("cin", True)
    assert (confirmed.json()["company_id"], confirmed.json()["link_method"]) == (
        company_id, "confirmed_by_invitee",
    )
    assert confirmed.json()["last_change"]["action"] == "confirm"


async def test_bad_confirmations_are_refused(
    make_client, invitee, session_factory, store
) -> None:
    doc_id = await awaiting_report(session_factory, store, unique_name())
    async with make_client(invitee) as client:
        company = (
            await client.post("/api/v1/companies", json={"name": unique_name(), "cin": fresh_cin()})
        ).json()
        unknown_company = await client.put(
            f"/api/v1/reports/{doc_id}/company", json={"company_id": str(uuid.uuid4())}
        )
        unknown_report = await client.put(
            f"/api/v1/reports/{uuid.uuid4()}/company", json={"company_id": company["id"]}
        )
        bad_year = await client.put(
            f"/api/v1/reports/{doc_id}/fiscal-year", json={"fiscal_year": "someday"}
        )
        missing = await client.get(f"/api/v1/reports/{uuid.uuid4()}")
        unknown_changes = await client.get(f"/api/v1/reports/{uuid.uuid4()}/changes")
        still = (await client.get(f"/api/v1/reports/{doc_id}")).json()

    assert unknown_company.status_code == 422
    assert unknown_report.status_code == 404
    assert bad_year.status_code == 422
    assert missing.status_code == 404
    assert unknown_changes.status_code == 404
    assert (still["link_status"], still["last_change"]) == ("awaiting_confirmation", None)


async def test_reports_need_a_login(make_client) -> None:
    async with make_client(None) as client:
        r = await client.put(
            f"/api/v1/reports/{uuid.uuid4()}/company", json={"company_id": str(uuid.uuid4())}
        )
    assert r.status_code == 401
