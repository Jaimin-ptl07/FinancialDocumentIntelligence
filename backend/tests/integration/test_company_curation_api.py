"""`/api/v1/companies`: Invitees preparing and enriching the registry (S2.4).

Any Invitee may create a Company, rename it, and add or remove the aliases
and exchange identifiers it is known by; every such change records who made
it and when. Search finds a Company by any of those names. The client and
second-Invitee fixtures are the S2.1 API tests' own, imported (hence F811:
ruff reads each fixture parameter as a redefinition of the import)."""

# ruff: noqa: F811

import uuid

import pytest

from fdi.pipeline.companies import CompanyRegistry
from tests.integration.conftest import acme_report, fresh_cin, ingest
from tests.integration.test_companies_api import another_invitee, make_client  # noqa: F401

pytestmark = pytest.mark.integration


async def create(client, *, name: str, cin: str):
    return await client.post("/api/v1/companies", json={"name": name, "cin": cin})


def ids(response) -> list[str]:
    return [c["id"] for c in response.json()["companies"]]


async def test_an_invitee_creates_a_company_and_a_second_with_the_same_cin_is_rejected(
    make_client, invitee, another_invitee
) -> None:
    cin = fresh_cin()
    async with make_client(invitee) as client:
        created = await create(client, name="Acme Widgets Limited", cin=cin)
    assert created.status_code == 201
    company = created.json()
    assert (company["cin"], company["name"]) == (cin, "Acme Widgets Limited")

    async with make_client(another_invitee) as client:
        seen = await client.get(f"/api/v1/companies/{company['id']}")
        duplicate = await create(client, name="Acme Widgets Ltd.", cin=cin.lower())
    assert seen.json() == company
    assert duplicate.status_code == 409


async def test_a_malformed_cin_is_rejected(make_client, invitee) -> None:
    async with make_client(invitee) as client:
        r = await create(client, name="Acme Widgets Limited", cin="L1234")
    assert r.status_code == 422


async def test_an_invitee_renames_a_company_but_cannot_give_it_another_companys_cin(
    make_client, invitee
) -> None:
    taken = fresh_cin()
    async with make_client(invitee) as client:
        await create(client, name="Taken Limited", cin=taken)
        company = (await create(client, name="Acme Widgts Ltd", cin=fresh_cin())).json()
        renamed = await client.patch(
            f"/api/v1/companies/{company['id']}", json={"name": "Acme Widgets Limited"}
        )
        clash = await client.patch(f"/api/v1/companies/{company['id']}", json={"cin": taken})
        after = await client.get(f"/api/v1/companies/{company['id']}")
        missing = await client.patch(f"/api/v1/companies/{uuid.uuid4()}", json={"name": "X"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Acme Widgets Limited"
    assert clash.status_code == 409
    assert after.json() == {**company, "name": "Acme Widgets Limited"}
    assert missing.status_code == 404


async def test_identifiers_are_unique_per_type_and_value(make_client, invitee) -> None:
    alias = f"Acme{uuid.uuid4().hex[:8]}"
    async with make_client(invitee) as client:
        a = (await create(client, name="Acme Widgets Limited", cin=fresh_cin())).json()
        b = (await create(client, name="Acme Gadgets Limited", cin=fresh_cin())).json()
        added = await client.post(
            f"/api/v1/companies/{a['id']}/identifiers", json={"type": "alias", "value": alias}
        )
        same_spelled_differently = await client.post(
            f"/api/v1/companies/{b['id']}/identifiers",
            json={"type": "alias", "value": f" {alias.upper()}. "},
        )
        same_value_other_type = await client.post(
            f"/api/v1/companies/{b['id']}/identifiers",
            json={"type": "nse_symbol", "value": alias},
        )
        malformed = await client.post(
            f"/api/v1/companies/{a['id']}/identifiers", json={"type": "isin", "value": "INE1"}
        )
        listed = await client.get(f"/api/v1/companies/{a['id']}/identifiers")
    assert added.status_code == 201
    assert added.json()["type"] == "alias"
    assert added.json()["value"] == alias
    assert same_spelled_differently.status_code == 409
    assert same_value_other_type.status_code == 201
    assert malformed.status_code == 422
    assert listed.json()["identifiers"] == [added.json()]


async def test_a_removed_identifier_is_gone_and_can_be_reused(make_client, invitee) -> None:
    symbol = f"ACME{uuid.uuid4().hex[:6].upper()}"
    async with make_client(invitee) as client:
        a = (await create(client, name="Acme Widgets Limited", cin=fresh_cin())).json()
        b = (await create(client, name="Acme Gadgets Limited", cin=fresh_cin())).json()
        added = (
            await client.post(
                f"/api/v1/companies/{a['id']}/identifiers",
                json={"type": "nse_symbol", "value": symbol},
            )
        ).json()
        removed = await client.delete(f"/api/v1/companies/{a['id']}/identifiers/{added['id']}")
        again = await client.delete(f"/api/v1/companies/{a['id']}/identifiers/{added['id']}")
        listed = await client.get(f"/api/v1/companies/{a['id']}/identifiers")
        reused = await client.post(
            f"/api/v1/companies/{b['id']}/identifiers",
            json={"type": "nse_symbol", "value": symbol},
        )
    assert removed.status_code == 204
    assert again.status_code == 404
    assert listed.json()["identifiers"] == []
    assert reused.status_code == 201


async def test_searching_ril_or_reliance_industries_ltd_finds_reliance_industries(
    make_client, invitee
) -> None:
    async with make_client(invitee) as client:
        ril = (
            await create(client, name="Reliance Industries Limited", cin=fresh_cin())
        ).json()
        before = await client.get("/api/v1/companies", params={"q": "RIL"})
        await client.post(
            f"/api/v1/companies/{ril['id']}/identifiers", json={"type": "alias", "value": "RIL"}
        )
        await client.post(
            f"/api/v1/companies/{ril['id']}/identifiers",
            json={"type": "nse_symbol", "value": "RELIANCE"},
        )
        by_alias = await client.get("/api/v1/companies", params={"q": "RIL"})
        by_name = await client.get(
            "/api/v1/companies", params={"q": "Reliance Industries Ltd."}
        )
        by_symbol = await client.get("/api/v1/companies", params={"q": "reliance"})
        by_cin = await client.get("/api/v1/companies", params={"q": ril["cin"].lower()})
        nothing = await client.get("/api/v1/companies", params={"q": "Zebra Unicorn"})

    assert ril["id"] not in ids(before)
    for found in (by_alias, by_name, by_symbol, by_cin):
        assert found.status_code == 200
        assert ids(found).count(ril["id"]) == 1
    assert ids(by_alias) == [ril["id"]]
    assert nothing.json()["companies"] == []


async def test_every_manual_change_records_the_acting_invitee_and_the_time(
    make_client, invitee, another_invitee
) -> None:
    async with make_client(invitee) as client:
        company = (await create(client, name="Acme Widgts Ltd", cin=fresh_cin())).json()
        added = (
            await client.post(
                f"/api/v1/companies/{company['id']}/identifiers",
                json={"type": "alias", "value": f"Acme{uuid.uuid4().hex[:8]}"},
            )
        ).json()
    async with make_client(another_invitee) as client:
        await client.patch(
            f"/api/v1/companies/{company['id']}", json={"name": "Acme Widgets Limited"}
        )
        await client.delete(f"/api/v1/companies/{company['id']}/identifiers/{added['id']}")
        changes = await client.get(f"/api/v1/companies/{company['id']}/changes")

    assert changes.status_code == 200
    log = changes.json()["changes"]
    assert [(c["action"], c["invitee_email"]) for c in log] == [
        ("create", invitee[0]),
        ("add_identifier", invitee[0]),
        ("update", another_invitee[0]),
        ("remove_identifier", another_invitee[0]),
    ]
    assert all(c["at"] for c in log)
    assert log[2]["detail"] == {"name": ["Acme Widgts Ltd", "Acme Widgets Limited"]}
    assert log[3]["detail"] == {"type": "alias", "value": added["value"]}


async def test_a_hand_created_company_is_linked_when_a_report_printing_its_cin_is_ingested(
    make_client, invitee, session_factory, store
) -> None:
    cin = fresh_cin()
    async with make_client(invitee) as client:
        company = (await create(client, name="Acme Widgets Limited", cin=cin)).json()

    doc_key = f"acme-{uuid.uuid4()}"
    doc_id = await ingest(session_factory, store, doc_key=doc_key, lines=acme_report(cin))
    async with session_factory() as s:
        link = await CompanyRegistry(s, store).resolve_report(doc_id)
        await s.commit()

    assert (link.company_id, link.created_company) == (company["id"], False)
    async with make_client(invitee) as client:
        reports = await client.get(f"/api/v1/companies/{company['id']}/reports")
        companies = await client.get("/api/v1/companies", params={"q": cin})
    assert [r["doc_id"] for r in reports.json()["reports"]] == [doc_id]
    assert ids(companies) == [company["id"]]


async def test_changes_to_an_unknown_company_are_404(make_client, invitee) -> None:
    unknown = str(uuid.uuid4())
    async with make_client(invitee) as client:
        add = await client.post(
            f"/api/v1/companies/{unknown}/identifiers", json={"type": "alias", "value": "X"}
        )
        listed = await client.get(f"/api/v1/companies/{unknown}/identifiers")
        changes = await client.get(f"/api/v1/companies/{unknown}/changes")
    assert (add.status_code, listed.status_code, changes.status_code) == (404, 404, 404)
