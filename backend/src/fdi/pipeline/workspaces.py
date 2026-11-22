"""Workspaces: an Invitee's personal working sets of Companies (S2.5).

A Workspace is a focus list, never an access boundary (#16). It is owned by
one Invitee and every operation here is scoped to that owner: another
Invitee's Workspace is reported exactly like a missing one (`None`), so the
API can answer 404 without revealing that it exists. Nothing in the Company
or Report read path consults this module -- `CompanyRegistry` does not import
it, and must not.

The API calls `WorkspaceService`; nothing touches the workspace tables
directly. pipeline -> platform, as `fdi.pipeline.companies`.
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from fdi.pipeline.companies import CompanyView
from fdi.platform.db.models import Workspace
from fdi.platform.db.workspace_repository import WorkspaceRepository


class UnknownCompany(LookupError):
    """No Company with this id exists, so it cannot join a Workspace."""


@dataclass(frozen=True)
class WorkspaceSummary:
    id: str
    name: str


@dataclass(frozen=True)
class WorkspaceView:
    id: str
    name: str
    companies: list[CompanyView]


class WorkspaceService:
    """Every method acts as `owner_user_id`. The caller owns the transaction:
    writes flush, the caller commits."""

    def __init__(self, session: AsyncSession, owner_user_id: str) -> None:
        self._repo = WorkspaceRepository(session)
        self._owner = owner_user_id

    async def create(self, name: str) -> WorkspaceView:
        workspace = await self._repo.create(owner_user_id=self._owner, name=name)
        return await self._view(workspace)

    async def list_mine(self) -> list[WorkspaceSummary]:
        return [
            WorkspaceSummary(id=w.id, name=w.name)
            for w in await self._repo.list_owned(self._owner)
        ]

    async def get(self, workspace_id: str) -> WorkspaceView | None:
        workspace = await self._repo.get_owned(workspace_id, self._owner)
        return None if workspace is None else await self._view(workspace)

    async def rename(self, workspace_id: str, name: str) -> WorkspaceView | None:
        workspace = await self._repo.get_owned(workspace_id, self._owner)
        if workspace is None:
            return None
        await self._repo.rename(workspace, name)
        return await self._view(workspace)

    async def delete(self, workspace_id: str) -> bool:
        workspace = await self._repo.get_owned(workspace_id, self._owner)
        if workspace is None:
            return False
        await self._repo.delete(workspace)
        return True

    async def add_company(self, workspace_id: str, company_id: str) -> WorkspaceView | None:
        """The Workspace after adding; None if it isn't the owner's.
        Raises `UnknownCompany` if there is no such Company."""
        workspace = await self._repo.get_owned(workspace_id, self._owner)
        if workspace is None:
            return None
        if not await self._repo.add_company(workspace.id, company_id):
            raise UnknownCompany(company_id)
        return await self._view(workspace)

    async def remove_company(self, workspace_id: str, company_id: str) -> WorkspaceView | None:
        """The Workspace after removing; None if it isn't the owner's.
        Removing a Company the Workspace doesn't hold is a no-op."""
        workspace = await self._repo.get_owned(workspace_id, self._owner)
        if workspace is None:
            return None
        await self._repo.remove_company(workspace.id, company_id)
        return await self._view(workspace)

    async def _view(self, workspace: Workspace) -> WorkspaceView:
        companies = await self._repo.companies(workspace.id)
        return WorkspaceView(
            id=workspace.id,
            name=workspace.name,
            companies=[CompanyView(id=c.id, cin=c.cin, name=c.name) for c in companies],
        )
