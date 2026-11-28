"""`/api/v1/extraction-schemas` (S3): schemas as data an Invitee owns.

Draft -> publish -> immutable versions, copies that keep Field identity,
ownership (only the owner edits; anyone reads, copies, runs), the read-only
FDI standard schema, optimistic draft revisions, and version diffs."""

import pytest

pytestmark = pytest.mark.integration

BASE = "/api/v1/extraction-schemas"
CAPEX = {"name": "Capital expenditure", "description": "Purchase of property, plant "
         "and equipment", "data_type": "decimal", "statement": "cash_flow", "tag": "CAPEX",
         "validation": {"non_negative": True}}


async def _schema(client, name: str = "My schema") -> dict:
    r = await client.post(BASE, json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


async def _standard(client) -> dict:
    schemas = (await client.get(BASE)).json()["schemas"]
    return next(s for s in schemas if s["is_standard"])


async def test_the_standard_schema_is_there_and_read_only(make_client, invitee) -> None:
    async with make_client(invitee) as client:
        standard = await _standard(client)
        version = (await client.get(f"{BASE}/{standard['id']}/versions/latest")).json()
        edit = await client.post(f"{BASE}/{standard['id']}/draft/fields",
                                 json={"revision": 0, "field": CAPEX})
    assert standard["name"] == "FDI standard"
    assert standard["owned_by_me"] is False
    assert standard["latest_version"] >= 1
    assert standard["field_count"] == version["field_count"] > 500
    tags = {f["tag"] for f in version["fields"]}
    assert {"PATR", "TOTEXPR", "PBTR"} <= tags
    assert [g["key"] for g in version["groups"]] == ["PL", "AS", "LI", "EQ", "CF"]
    assert edit.status_code == 403


async def test_draft_publish_and_immutable_versions(make_client, invitee) -> None:
    async with make_client(invitee) as client:
        schema = await _schema(client)
        sid = schema["id"]
        empty = (await client.get(f"{BASE}/{sid}/draft")).json()
        added = await client.post(f"{BASE}/{sid}/draft/fields",
                                  json={"revision": empty["revision"], "field": CAPEX})
        draft = added.json()
        v1 = await client.post(f"{BASE}/{sid}/draft/publish",
                               json={"revision": draft["revision"], "notes": "first"})
        again = await client.post(f"{BASE}/{sid}/draft/publish", json={"revision": 0})
        after = (await client.get(f"{BASE}/{sid}/draft")).json()
        field_id = draft["fields"][0]["field_id"]
        edited = await client.patch(
            f"{BASE}/{sid}/draft/fields/{field_id}",
            json={"revision": after["revision"],
                  "changes": {"description": "Capex incl. intangibles"}},
        )
        v2 = await client.post(f"{BASE}/{sid}/draft/publish",
                               json={"revision": edited.json()["revision"]})
        v1_again = (await client.get(f"{BASE}/{sid}/versions/1")).json()
        versions = (await client.get(f"{BASE}/{sid}/versions")).json()["versions"]
        diff = (await client.get(f"{BASE}/{sid}/versions/2/diff?against=1")).json()

    assert empty == {**empty, "revision": 0, "fields": [], "unpublished": False}
    assert added.status_code == 201
    assert draft["unpublished"] is True
    assert [f["name"] for f in draft["changes"]["added"]] == ["Capital expenditure"]
    assert v1.status_code == 201 and v1.json()["version"] == 1
    assert again.status_code == 409, "an unchanged draft has nothing to publish"
    # Publishing advances the draft: revisions only ever grow.
    assert after["revision"] == draft["revision"] + 1 and after["base_version"] == 1
    assert after["unpublished"] is False
    assert v2.json()["version"] == 2
    # Version 1 never changes; the Field keeps its identity across versions.
    assert v1_again["fields"][0]["description"] == CAPEX["description"]
    assert v2.json()["fields"][0]["field_id"] == field_id
    assert v1_again["fields"][0]["definition_hash"] != v2.json()["fields"][0]["definition_hash"]
    assert [v["version"] for v in versions] == [2, 1]
    assert diff["added"] == [] and diff["removed"] == []
    assert diff["changed"][0]["field_id"] == field_id
    assert "description" in diff["changed"][0]["changed"]


async def test_a_stale_revision_is_refused(make_client, invitee) -> None:
    async with make_client(invitee) as client:
        sid = (await _schema(client))["id"]
        first = await client.post(f"{BASE}/{sid}/draft/fields",
                                  json={"revision": 0, "field": CAPEX})
        stale = await client.post(f"{BASE}/{sid}/draft/fields",
                                  json={"revision": 0, "field": {"name": "Dividends paid"}})
    assert first.status_code == 201
    assert stale.status_code == 409
    assert stale.json()["current_revision"] == first.json()["revision"]


async def test_a_tab_stale_from_before_a_publish_stays_stale(make_client, invitee) -> None:
    """Revisions never restart: after tab B publishes and edits again, tab
    A's old revision must not match the new draft."""
    async with make_client(invitee) as client:
        sid = (await _schema(client))["id"]
        d1 = (await client.post(f"{BASE}/{sid}/draft/fields",
                                json={"revision": 0, "field": CAPEX})).json()
        tab_a_revision = d1["revision"]
        published = await client.post(f"{BASE}/{sid}/draft/publish",
                                      json={"revision": tab_a_revision})
        after = (await client.get(f"{BASE}/{sid}/draft")).json()
        await client.post(f"{BASE}/{sid}/draft/fields",
                          json={"revision": after["revision"],
                                "field": {"name": "Dividends paid"}})
        stale_edit = await client.post(f"{BASE}/{sid}/draft/fields",
                                       json={"revision": tab_a_revision,
                                             "field": {"name": "From tab A"}})
        stale_publish = await client.post(f"{BASE}/{sid}/draft/publish",
                                          json={"revision": tab_a_revision})
    assert published.status_code == 201
    assert stale_edit.status_code == 409
    assert stale_publish.status_code == 409


async def test_an_invalid_field_is_refused_with_every_problem(make_client, invitee) -> None:
    async with make_client(invitee) as client:
        sid = (await _schema(client))["id"]
        r = await client.post(f"{BASE}/{sid}/draft/fields", json={
            "revision": 0,
            "field": {"name": "X", "data_type": "money", "validation": {"min": 5, "max": 1}},
        })
        tags = await client.put(f"{BASE}/{sid}/draft", json={"revision": 0, "fields": [
            {"name": "A", "tag": "T"}, {"name": "B", "tag": "T"},
        ]})
    assert r.status_code == 422
    errors = " | ".join(r.json()["errors"])
    assert "data_type" in errors and "min greater than max" in errors
    assert tags.status_code == 422
    assert "tag 'T' appears twice" in " | ".join(tags.json()["errors"])


async def test_removing_a_field_keeps_it_in_published_versions(make_client, invitee) -> None:
    async with make_client(invitee) as client:
        sid = (await _schema(client))["id"]
        draft = (await client.post(f"{BASE}/{sid}/draft/fields",
                                   json={"revision": 0, "field": CAPEX})).json()
        await client.post(f"{BASE}/{sid}/draft/publish", json={"revision": draft["revision"]})
        fid = draft["fields"][0]["field_id"]
        current = (await client.get(f"{BASE}/{sid}/draft")).json()["revision"]
        removed = await client.delete(f"{BASE}/{sid}/draft/fields/{fid}?revision={current}")
        v2 = await client.post(f"{BASE}/{sid}/draft/publish",
                               json={"revision": removed.json()["revision"]})
        v1 = (await client.get(f"{BASE}/{sid}/versions/1")).json()
    assert removed.json()["changes"]["removed"][0]["field_id"] == fid
    assert v2.json()["field_count"] == 0
    assert [f["field_id"] for f in v1["fields"]] == [fid]


async def test_others_read_and_copy_but_never_edit(
    make_client, invitee, another_invitee
) -> None:
    async with make_client(invitee) as owner:
        sid = (await _schema(owner, "Owner's"))["id"]
        draft = (await owner.post(f"{BASE}/{sid}/draft/fields",
                                  json={"revision": 0, "field": CAPEX})).json()
        await owner.post(f"{BASE}/{sid}/draft/publish", json={"revision": draft["revision"]})
    async with make_client(another_invitee) as other:
        listed = (await other.get(BASE)).json()["schemas"]
        read = await other.get(f"{BASE}/{sid}/versions/1")
        edit = await other.post(f"{BASE}/{sid}/draft/fields",
                                json={"revision": 0, "field": {"name": "Mine now"}})
        rename = await other.patch(f"{BASE}/{sid}", json={"name": "Hijacked"})
        copy = await other.post(BASE, json={"name": "My copy", "copy_from": {"schema_id": sid}})
        copy_draft = (await other.get(f"{BASE}/{copy.json()['id']}/draft")).json()

    mine = next(s for s in listed if s["id"] == sid)
    assert mine["owned_by_me"] is False
    assert read.status_code == 200
    assert edit.status_code == 403
    assert rename.status_code == 403
    assert copy.status_code == 201 and copy.json()["owned_by_me"] is True
    # The copy's Fields ARE the source's Fields (same identity) until edited.
    assert [f["field_id"] for f in copy_draft["fields"]] == [draft["fields"][0]["field_id"]]
    assert copy_draft["unpublished"] is True


async def test_unknown_schema_and_version_are_404(make_client, invitee) -> None:
    async with make_client(invitee) as client:
        sid = (await _schema(client))["id"]
        missing = await client.get(f"{BASE}/00000000-0000-0000-0000-000000000000")
        no_version = await client.get(f"{BASE}/{sid}/versions/latest")
    assert missing.status_code == 404
    assert no_version.status_code == 404


async def test_importing_the_standard_document_again_publishes_nothing(
    session_factory,
) -> None:
    from fdi.pipeline.extraction_schemas import import_standard_schema_file
    from fdi.platform.config import _default_standard_schema_path

    async with session_factory() as s:
        first = await import_standard_schema_file(
            s, str(_default_standard_schema_path()), only_if_absent=False
        )
        await s.commit()
    async with session_factory() as s:
        second = await import_standard_schema_file(
            s, str(_default_standard_schema_path()), only_if_absent=False
        )
        await s.commit()
    assert second is None
    # (`first` is None too when start-up already imported this exact document.)
    assert first is None or first.summary.version >= 1
