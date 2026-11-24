"""Awaiting confirmation, and an Invitee confirming or correcting a Report's
link (S2.3, #50).

A Report whose issuer cannot be identified confidently waits for an Invitee
instead of being filed under a guess -- and a Company is never created from a
name alone (design §6.1). Any Invitee may then confirm or correct the
Report's Company and Fiscal year; each change records who and when, and
survives the Report being resolved again.

Reports go through the real stage chain and `CompanyRegistry`; assertions are
on what the registry answers, never on its tables."""

import asyncio
import random
import string
import uuid

import pytest

from fdi.pipeline.companies import CompanyRegistry
from fdi.platform.db.repository import UserRepository
from fdi.platform.security import hash_password
from tests.integration.conftest import INVITEE_PASSWORD, acme_report, fresh_cin, ingest

pytestmark = pytest.mark.integration


def unique_name() -> str:
    """A registered name no other test uses -- the database is shared."""
    word = "".join(random.choices(string.ascii_lowercase, k=10)).capitalize()
    return f"{word} Power Limited"


async def new_invitee(session_factory) -> str:
    async with session_factory() as s:
        user = await UserRepository(s).create(
            email=f"invitee-{uuid.uuid4().hex}@example.test",
            password_hash=hash_password(INVITEE_PASSWORD),
        )
        await s.commit()
    return user.id


@pytest.fixture
async def invitee_id(session_factory) -> str:
    return await new_invitee(session_factory)


async def resolve(session_factory, store, doc_id: str):
    async with session_factory() as s:
        outcome = await CompanyRegistry(s, store).resolve_report(doc_id)
        await s.commit()
    return outcome


async def report(session_factory, store, doc_id: str):
    async with session_factory() as s:
        return await CompanyRegistry(s, store).report(doc_id)


async def company_cins(session_factory, store) -> set[str]:
    async with session_factory() as s:
        return {c.cin for c in await CompanyRegistry(s, store).list_companies()}


async def companies_named(session_factory, store, name: str) -> list[str]:
    async with session_factory() as s:
        return [c.id for c in await CompanyRegistry(s, store).list_companies() if c.name == name]


# --- awaiting confirmation: never a guess, never a Company from a name ------


async def test_a_report_with_no_cin_awaits_confirmation_with_its_names_and_reason(
    session_factory, store
) -> None:
    name = unique_name()
    doc_id = await ingest(
        session_factory, store, doc_key=f"nocin-{uuid.uuid4()}",
        lines=[f"{name} had a record year.", f"{name} thanks its shareholders."],
    )

    outcome = await resolve(session_factory, store, doc_id)

    assert (outcome.company_id, outcome.link_status) == (None, "awaiting_confirmation")
    assert "no CIN" in outcome.reason
    assert outcome.candidates is not None
    assert outcome.candidates.names == (name,)
    assert await companies_named(session_factory, store, name) == []
    view = await report(session_factory, store, doc_id)
    assert view is not None
    assert (view.company_id, view.link_status, view.link_method) == (
        None, "awaiting_confirmation", None,
    )
    assert view.link_reason is not None and "no CIN" in view.link_reason
    assert view.candidates is not None and view.candidates.names == (name,)


async def test_cins_with_no_clear_winner_await_confirmation_and_create_no_company(
    session_factory, store
) -> None:
    first, second = fresh_cin(), fresh_cin()
    name = unique_name()
    doc_id = await ingest(
        session_factory, store, doc_key=f"ambiguous-{uuid.uuid4()}",
        lines=[f"{name} had a record year.", f"{name} {first}", f"{name} {second}"],
    )

    outcome = await resolve(session_factory, store, doc_id)

    assert (outcome.company_id, outcome.link_status) == (None, "awaiting_confirmation")
    assert first in outcome.reason and second in outcome.reason
    assert outcome.candidates is not None
    assert {c.cin for c in outcome.candidates.cins} == {first, second}
    assert not {first, second} & await company_cins(session_factory, store)


async def test_a_name_matching_a_known_company_awaits_confirmation_and_suggests_it(
    session_factory, store, invitee_id
) -> None:
    name = unique_name()
    async with session_factory() as s:
        known = await CompanyRegistry(s, store).create_company(
            name=name, cin=fresh_cin(), invitee_id=invitee_id
        )
        await s.commit()
    doc_id = await ingest(
        session_factory, store, doc_key=f"nameonly-{uuid.uuid4()}",
        lines=[f"{name.replace('Limited', 'Ltd.')} had a record year."],
    )

    outcome = await resolve(session_factory, store, doc_id)

    assert (outcome.company_id, outcome.link_status) == (None, "awaiting_confirmation")
    assert outcome.candidates is not None
    assert [c.id for c in outcome.candidates.companies] == [known.id]
    assert await companies_named(session_factory, store, name) == [known.id]
    async with session_factory() as s:
        assert await CompanyRegistry(s, store).company_reports(known.id) == []


async def test_a_report_naming_no_company_at_all_is_unidentified(
    session_factory, store
) -> None:
    doc_id = await ingest(
        session_factory, store, doc_key=f"blank-{uuid.uuid4()}",
        lines=["The year was a good one for everybody."],
    )

    outcome = await resolve(session_factory, store, doc_id)

    assert (outcome.company_id, outcome.link_status) == (None, "unidentified")
    assert outcome.reason


async def test_a_report_no_longer_identified_confidently_leaves_its_old_company(
    session_factory, store, invitee_id
) -> None:
    """Awaiting confirmation means under no Company: a Report filed under one
    by an earlier resolution (an older issuer stage, say) that now resolves
    as awaiting confirmation is taken out of that Company's history."""
    from fdi.platform.db.repository import CompanyRepository

    name = unique_name()
    stale = await hand_company(session_factory, store, invitee_id)
    doc_id = await awaiting_report(session_factory, store, name)
    async with session_factory() as s:  # as an earlier resolution left it
        await CompanyRepository(s).set_report_link(
            doc_id, company_id=stale.id, link_status="linked", link_method="cin"
        )
        await s.commit()

    outcome = await resolve(session_factory, store, doc_id)

    assert (outcome.company_id, outcome.link_status) == (None, "awaiting_confirmation")
    view = await report(session_factory, store, doc_id)
    assert view is not None and (view.company_id, view.link_method) == (None, None)
    async with session_factory() as s:
        assert await CompanyRegistry(s, store).company_reports(stale.id) == []


# --- one CIN, one Company, even under concurrency ---------------------------


async def test_two_reports_of_a_new_issuer_resolving_at_once_make_one_company(
    session_factory, store
) -> None:
    """The second resolution runs while the first's new Company is still
    uncommitted -- the window a read-then-insert would fork the issuer in."""
    cin = fresh_cin()
    first_doc, second_doc = [
        await ingest(
            session_factory, store, doc_key=f"race-{uuid.uuid4()}", lines=acme_report(cin)
        )
        for _ in range(2)
    ]

    async with session_factory() as first, session_factory() as second:
        a = await CompanyRegistry(first, store).resolve_report(first_doc)
        racing = asyncio.create_task(CompanyRegistry(second, store).resolve_report(second_doc))
        await asyncio.sleep(1.0)
        overlapped = not racing.done()  # held up by the first's uncommitted Company
        await first.commit()
        b = await racing
        await second.commit()

    assert overlapped
    assert a.company_id == b.company_id
    assert (a.created_company, b.created_company) == (True, False)
    assert [c for c in await company_cins(session_factory, store) if c == cin] == [cin]


async def test_many_reports_of_a_new_issuer_resolving_together_make_one_company(
    session_factory, store
) -> None:
    from fdi.pipeline.companies import resolve_and_commit

    cin = fresh_cin()
    docs = [
        await ingest(
            session_factory, store, doc_key=f"race-{uuid.uuid4()}", lines=acme_report(cin)
        )
        for _ in range(4)
    ]

    links = await asyncio.gather(*(
        resolve_and_commit(session_factory=session_factory, store=store, doc_id=d) for d in docs
    ))

    assert len({link.company_id for link in links}) == 1
    assert sum(link.created_company for link in links) == 1
    async with session_factory() as s:
        company_id = links[0].company_id
        assert company_id is not None
        reports = await CompanyRegistry(s, store).company_reports(company_id)
    assert sorted(r.doc_id for r in reports) == sorted(docs)


# --- an Invitee confirms or corrects: who and when, always ------------------


async def awaiting_report(session_factory, store, name: str, year_end: int = 2026) -> str:
    """A Report naming `name` but printing no CIN, resolved: awaiting
    confirmation."""
    doc_id = await ingest(
        session_factory, store, doc_key=f"await-{uuid.uuid4()}",
        lines=[f"{name} had a record year.", f"for the year ended 31st March {year_end}"],
    )
    outcome = await resolve(session_factory, store, doc_id)
    assert outcome.link_status == "awaiting_confirmation"
    return doc_id


async def hand_company(session_factory, store, invitee_id: str, name: str | None = None):
    async with session_factory() as s:
        company = await CompanyRegistry(s, store).create_company(
            name=name or unique_name(), cin=fresh_cin(), invitee_id=invitee_id
        )
        await s.commit()
    return company


async def test_confirming_files_the_report_under_the_company_and_records_who_and_when(
    session_factory, store, invitee_id
) -> None:
    name = unique_name()
    company = await hand_company(session_factory, store, invitee_id, name)
    doc_id = await awaiting_report(session_factory, store, name)
    confirmer = await new_invitee(session_factory)

    async with session_factory() as s:
        view = await CompanyRegistry(s, store).link_report(
            doc_id, company_id=company.id, invitee_id=confirmer
        )
        await s.commit()

    assert view is not None
    assert (view.company_id, view.link_status, view.link_method) == (
        company.id, "linked", "confirmed_by_invitee",
    )
    assert view.candidates is None
    assert view.last_change is not None
    assert (view.last_change.action, view.last_change.invitee_id) == ("confirm", confirmer)
    assert view.last_change.at is not None
    async with session_factory() as s:
        reports = await CompanyRegistry(s, store).company_reports(company.id)
    assert [r.doc_id for r in reports] == [doc_id]


async def test_correcting_moves_a_cin_linked_report_to_another_company(
    session_factory, store, invitee_id
) -> None:
    doc_id = await ingest(
        session_factory, store, doc_key=f"acme-{uuid.uuid4()}", lines=acme_report(fresh_cin())
    )
    wrong = (await resolve(session_factory, store, doc_id)).company_id
    right = await hand_company(session_factory, store, invitee_id)

    async with session_factory() as s:
        registry = CompanyRegistry(s, store)
        view = await registry.link_report(doc_id, company_id=right.id, invitee_id=invitee_id)
        await s.commit()

    assert view is not None
    assert (view.company_id, view.link_method) == (right.id, "confirmed_by_invitee")
    assert view.last_change is not None
    assert view.last_change.action == "correct_company"
    assert view.last_change.detail == {"company_id": [wrong, right.id]}
    async with session_factory() as s:
        registry = CompanyRegistry(s, store)
        assert [r.doc_id for r in await registry.company_reports(right.id)] == [doc_id]
        assert wrong is not None
        assert await registry.company_reports(wrong) == []


async def test_correcting_the_fiscal_year_moves_the_report_in_its_companys_history(
    session_factory, store, invitee_id
) -> None:
    name = unique_name()
    company = await hand_company(session_factory, store, invitee_id, name)
    older = await awaiting_report(session_factory, store, name, year_end=2025)
    newer = await awaiting_report(session_factory, store, name, year_end=2026)
    async with session_factory() as s:
        registry = CompanyRegistry(s, store)
        for doc_id in (older, newer):
            await registry.link_report(doc_id, company_id=company.id, invitee_id=invitee_id)
        await s.commit()
    async with session_factory() as s:
        before = [r.doc_id for r in await CompanyRegistry(s, store).company_reports(company.id)]
    assert before == [newer, older]

    corrector = await new_invitee(session_factory)
    async with session_factory() as s:
        view = await CompanyRegistry(s, store).correct_fiscal_year(
            older, fiscal_year="FY2026-27", invitee_id=corrector
        )
        await s.commit()

    assert view is not None
    assert (view.fiscal_year, str(view.period_start), str(view.period_end)) == (
        "FY2026-27", "2026-04-01", "2027-03-31",
    )
    assert view.fiscal_year_method == "confirmed_by_invitee"
    assert view.last_change is not None
    assert (view.last_change.action, view.last_change.invitee_id) == (
        "correct_fiscal_year", corrector,
    )
    assert view.last_change.detail == {"fiscal_year": ["FY2024-25", "FY2026-27"]}
    async with session_factory() as s:
        registry = CompanyRegistry(s, store)
        after = [r.doc_id for r in await registry.company_reports(company.id)]
        history = await registry.report_changes(older)
    assert after == [older, newer]
    assert [c.action for c in history] == ["confirm", "correct_fiscal_year"]
    assert history[0].at < history[1].at


async def test_resolving_again_keeps_an_invitees_company_and_fiscal_year(
    session_factory, store, invitee_id
) -> None:
    """Re-processing or backfilling a Report re-runs `resolve_report`; what an
    Invitee confirmed or corrected must survive it."""
    name = unique_name()
    company = await hand_company(session_factory, store, invitee_id, name)
    doc_id = await awaiting_report(session_factory, store, name, year_end=2026)
    async with session_factory() as s:
        registry = CompanyRegistry(s, store)
        await registry.link_report(doc_id, company_id=company.id, invitee_id=invitee_id)
        await registry.correct_fiscal_year(doc_id, fiscal_year="FY2023-24", invitee_id=invitee_id)
        await s.commit()

    outcome = await resolve(session_factory, store, doc_id)

    assert (outcome.company_id, outcome.link_status, outcome.fiscal_year) == (
        company.id, "linked", "FY2023-24",
    )
    view = await report(session_factory, store, doc_id)
    assert view is not None
    assert (view.company_id, view.link_status, view.link_method) == (
        company.id, "linked", "confirmed_by_invitee",
    )
    assert (view.fiscal_year, str(view.period_end), view.fiscal_year_method) == (
        "FY2023-24", "2024-03-31", "confirmed_by_invitee",
    )


async def test_resolving_again_keeps_a_corrected_report_off_its_printed_cins_company(
    session_factory, store, invitee_id
) -> None:
    doc_id = await ingest(
        session_factory, store, doc_key=f"acme-{uuid.uuid4()}",
        lines=[*acme_report(fresh_cin()), "for the year ended 31st March 2026"],
    )
    printed = (await resolve(session_factory, store, doc_id)).company_id
    right = await hand_company(session_factory, store, invitee_id)
    async with session_factory() as s:
        await CompanyRegistry(s, store).link_report(
            doc_id, company_id=right.id, invitee_id=invitee_id
        )
        await s.commit()

    outcome = await resolve(session_factory, store, doc_id)

    assert outcome.company_id == right.id
    # The Fiscal year was never corrected, so it is still the Report's own.
    assert outcome.fiscal_year == "FY2025-26"
    async with session_factory() as s:
        registry = CompanyRegistry(s, store)
        assert [r.doc_id for r in await registry.company_reports(right.id)] == [doc_id]
        assert printed is not None and await registry.company_reports(printed) == []


async def test_a_fiscal_year_that_is_not_one_is_refused(session_factory, store, invitee_id) -> None:
    from fdi.pipeline.companies import InvalidCompanyInput

    doc_id = await awaiting_report(session_factory, store, unique_name())

    async with session_factory() as s:
        with pytest.raises(InvalidCompanyInput):
            await CompanyRegistry(s, store).correct_fiscal_year(
                doc_id, fiscal_year="sometime", invitee_id=invitee_id
            )


async def test_linking_to_an_unknown_company_or_report_is_refused(
    session_factory, store, invitee_id
) -> None:
    from fdi.pipeline.companies import UnknownCompany

    doc_id = await awaiting_report(session_factory, store, unique_name())
    company = await hand_company(session_factory, store, invitee_id)

    async with session_factory() as s:
        registry = CompanyRegistry(s, store)
        with pytest.raises(UnknownCompany):
            await registry.link_report(doc_id, company_id=str(uuid.uuid4()), invitee_id=invitee_id)
        assert await registry.link_report(
            str(uuid.uuid4()), company_id=company.id, invitee_id=invitee_id
        ) is None
