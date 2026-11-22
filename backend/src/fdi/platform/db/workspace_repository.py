"""Workspace persistence (S2.5). The caller owns the transaction.

Every read and write is keyed by the owner as well as the Workspace id, so
another Invitee's Workspace is indistinguishable from a missing one at this
level already -- there is no "fetch by id, then check the owner" path for a
caller to get wrong. `fdi.pipeline.workspaces.WorkspaceService` is the only
caller."""

import uuid

import sqlalchemy
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fdi.platform.db.models import Company, Workspace, WorkspaceCompany


class WorkspaceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(self, *, owner_user_id: str, name: str) -> Workspace:
        workspace = Workspace(id=str(uuid.uuid4()), owner_user_id=owner_user_id, name=name)
        self._s.add(workspace)
        await self._s.flush()
        return workspace

    async def get_owned(self, workspace_id: str, owner_user_id: str) -> Workspace | None:
        return (
            await self._s.execute(
                select(Workspace).where(
                    Workspace.id == workspace_id, Workspace.owner_user_id == owner_user_id
                )
            )
        ).scalar_one_or_none()

    async def list_owned(self, owner_user_id: str) -> list[Workspace]:
        rows = (
            await self._s.execute(
                select(Workspace)
                .where(Workspace.owner_user_id == owner_user_id)
                .order_by(Workspace.created_at, Workspace.id)
            )
        ).scalars().all()
        return list(rows)

    async def rename(self, workspace: Workspace, name: str) -> None:
        workspace.name = name
        await self._s.flush()

    async def delete(self, workspace: Workspace) -> None:
        """Membership rows go with it (`ON DELETE CASCADE`)."""
        await self._s.delete(workspace)
        await self._s.flush()

    async def add_company(self, workspace_id: str, company_id: str) -> bool:
        """False when no such Company exists. Idempotent: adding a Company
        already in the Workspace is a no-op.

        The foreign key decides "no such Company", inside a savepoint so the
        caller's transaction survives the violation -- one statement, no
        read-then-insert window for a concurrent Company delete to slip into."""
        try:
            async with self._s.begin_nested():
                await self._s.execute(
                    pg_insert(WorkspaceCompany)
                    .values(workspace_id=workspace_id, company_id=company_id)
                    .on_conflict_do_nothing()
                )
        except IntegrityError:
            return False
        return True

    async def remove_company(self, workspace_id: str, company_id: str) -> None:
        """Idempotent: removing a Company not in the Workspace is a no-op."""
        await self._s.execute(
            sqlalchemy.delete(WorkspaceCompany).where(
                WorkspaceCompany.workspace_id == workspace_id,
                WorkspaceCompany.company_id == company_id,
            )
        )
        await self._s.flush()

    async def companies(self, workspace_id: str) -> list[Company]:
        rows = (
            await self._s.execute(
                select(Company)
                .join(WorkspaceCompany, WorkspaceCompany.company_id == Company.id)
                .where(WorkspaceCompany.workspace_id == workspace_id)
                .order_by(Company.name, Company.id)
            )
        ).scalars().all()
        return list(rows)
