"""Resolving a Company name or alias to a Company and its Reports (S2.4).

`CompanyRegistry.resolve_company` is the call agents (S4-S8) scope a question
with, so it must never depend on a `doc_key`. Reports go through the real
stage chain and `resolve_report`; aliases are added the way an Invitee adds
them. Assertions are on what the registry answers."""

import random
import string
import uuid

import pytest

from fdi.pipeline.companies import CompanyRegistry
from fdi.platform.db.repository import UserRepository
from fdi.platform.security import hash_password
from tests.integration.conftest import fresh_cin, ingest

pytestmark = pytest.mark.integration


def unique_name() -> str:
    """A registered name no other test uses -- the database is shared across
    the session, and every Acme fixture is "Acme Widgets Limited"."""
    word = "".join(random.choices(string.ascii_lowercase, k=10)).capitalize()
    return f"{word} Industries Limited"


def annual_report(name: str, cin: str, year_end: int) -> list[str]:
    return [
        f"{name} Annual Report for the year ended 31st March {year_end}.",
        f"{name} CIN: {cin} Registered Office: Mumbai",
    ]


@pytest.fixture
async def invitee_id(session_factory) -> str:
    async with session_factory() as s:
        user = await UserRepository(s).create(
            email=f"invitee-{uuid.uuid4().hex}@example.test",
            password_hash=hash_password("correct-horse-battery-staple"),
        )
        await s.commit()
    return user.id


async def file_report(session_factory, store, *, lines: list[str]) -> tuple[str, str]:
    doc_id = await ingest(session_factory, store, doc_key=f"r-{uuid.uuid4()}", lines=lines)
    async with session_factory() as s:
        link = await CompanyRegistry(s, store).resolve_report(doc_id)
        await s.commit()
    assert link.company_id is not None, link.reason
    return doc_id, link.company_id


async def test_an_alias_resolves_to_the_company_and_its_reports_newest_fiscal_year_first(
    session_factory, store, invitee_id
) -> None:
    name, cin = unique_name(), fresh_cin()
    alias = f"{name.split()[0][:4].upper()}{uuid.uuid4().hex[:4].upper()}"
    # The newer Fiscal year is uploaded first, so upload order alone can't pass.
    fy2526, company_id = await file_report(
        session_factory, store, lines=annual_report(name, cin, 2026)
    )
    fy2425, _ = await file_report(session_factory, store, lines=annual_report(name, cin, 2025))
    async with session_factory() as s:
        await CompanyRegistry(s, store).add_identifier(
            company_id, kind="alias", value=alias, invitee_id=invitee_id
        )
        await s.commit()

    async with session_factory() as s:
        registry = CompanyRegistry(s, store)
        by_alias = await registry.resolve_company(alias.lower())
        by_name = await registry.resolve_company(name.replace("Limited", "Ltd."))
        listed = await registry.company_reports(company_id)

    for resolution in (by_alias, by_name):
        assert resolution.company is not None
        assert (resolution.company.id, resolution.company.cin) == (company_id, cin)
        assert [(r.doc_id, r.fiscal_year) for r in resolution.reports] == [
            (fy2526, "FY2025-26"),
            (fy2425, "FY2024-25"),
        ]
        assert resolution.reports == listed


async def test_a_name_two_companies_share_resolves_to_neither_and_lists_both(
    session_factory, store, invitee_id
) -> None:
    name = unique_name()
    async with session_factory() as s:
        registry = CompanyRegistry(s, store)
        a = await registry.create_company(name=name, cin=fresh_cin(), invitee_id=invitee_id)
        b = await registry.create_company(name=name, cin=fresh_cin(), invitee_id=invitee_id)
        await s.commit()

    async with session_factory() as s:
        resolution = await CompanyRegistry(s, store).resolve_company(name)

    assert resolution.company is None
    assert resolution.reports == []
    assert {c.id for c in resolution.candidates} == {a.id, b.id}


async def test_a_partial_name_is_a_search_not_a_resolution(
    session_factory, store, invitee_id
) -> None:
    name = unique_name()
    async with session_factory() as s:
        registry = CompanyRegistry(s, store)
        company = await registry.create_company(name=name, cin=fresh_cin(), invitee_id=invitee_id)
        await s.commit()

    async with session_factory() as s:
        registry = CompanyRegistry(s, store)
        partial = await registry.resolve_company(name.split()[0])
        unknown = await registry.resolve_company("Zebra Unicorn Holdings")

    assert partial.company is None
    assert [c.id for c in partial.candidates] == [company.id]
    assert (unknown.company, unknown.candidates) == (None, [])
