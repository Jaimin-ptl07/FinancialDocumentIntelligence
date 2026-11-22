"""Workspace membership follows its Company and its Workspace out.

No route deletes a Company today; this pins the schema so that when one does,
the Company leaves every Workspace with it instead of the delete failing on
a dangling membership row."""

import uuid

import pytest

from fdi.platform.db.models import AppUser
from fdi.platform.db.repository import CompanyRepository
from fdi.platform.db.workspace_repository import WorkspaceRepository
from tests.integration.conftest import fresh_cin

pytestmark = pytest.mark.integration


async def _owner(session) -> str:
    user = AppUser(
        id=str(uuid.uuid4()),
        email=f"ws-{uuid.uuid4().hex}@example.test",
        password_hash="x",
    )
    session.add(user)
    await session.flush()
    return user.id


async def test_deleting_a_company_removes_it_from_workspaces(session_factory) -> None:
    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        workspace = await repo.create(owner_user_id=await _owner(s), name="Watchlist")
        company, _ = await CompanyRepository(s).get_or_create_by_cin(
            cin=fresh_cin(), name="Doomed Limited"
        )
        assert await repo.add_company(workspace.id, company.id)
        await s.commit()

        await s.delete(company)
        await s.commit()

        assert await repo.companies(workspace.id) == []
        assert await repo.get_owned(workspace.id, workspace.owner_user_id) is not None


async def test_deleting_a_workspace_leaves_its_companies(session_factory) -> None:
    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        owner = await _owner(s)
        workspace = await repo.create(owner_user_id=owner, name="Watchlist")
        company, _ = await CompanyRepository(s).get_or_create_by_cin(
            cin=fresh_cin(), name="Survivor Limited"
        )
        await repo.add_company(workspace.id, company.id)
        await s.commit()

        await repo.delete(workspace)
        await s.commit()

        assert await repo.get_owned(workspace.id, owner) is None
        assert await CompanyRepository(s).get(company.id) is not None
