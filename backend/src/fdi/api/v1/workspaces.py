"""`/api/v1/workspaces`: an Invitee's personal working sets of Companies.

A Workspace is a focus list, never an access boundary (#16). Each one is
visible only to the Invitee who owns it, and someone else's Workspace is the
same 404 as a missing one -- never 403, which would confirm it exists. All
of it goes through `fdi.pipeline.workspaces.WorkspaceService`, never the
tables; the Company routes never consult Workspaces."""


from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from fdi.api.auth import CurrentUserDep
from fdi.api.deps import SessionDep
from fdi.pipeline.workspaces import (
    UnknownCompany,
    WorkspaceService,
    WorkspaceSummary,
    WorkspaceView,
)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


class WorkspaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class WorkspaceCompanyOut(BaseModel):
    id: str
    cin: str
    name: str


class WorkspaceOut(BaseModel):
    id: str
    name: str
    companies: list[WorkspaceCompanyOut]


class WorkspaceSummaryOut(BaseModel):
    id: str
    name: str


class WorkspaceListOut(BaseModel):
    workspaces: list[WorkspaceSummaryOut]


def _out(w: WorkspaceView) -> WorkspaceOut:
    return WorkspaceOut(
        id=w.id,
        name=w.name,
        companies=[WorkspaceCompanyOut(id=c.id, cin=c.cin, name=c.name) for c in w.companies],
    )


def _summary_out(w: WorkspaceSummary) -> WorkspaceSummaryOut:
    return WorkspaceSummaryOut(id=w.id, name=w.name)


def _not_found(workspace_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"workspace {workspace_id} not found")


@router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    payload: WorkspaceIn, session: SessionDep, user_id: CurrentUserDep
) -> WorkspaceOut:
    workspace = await WorkspaceService(session, user_id).create(payload.name)
    await session.commit()
    return _out(workspace)


@router.get("", response_model=WorkspaceListOut)
async def list_workspaces(session: SessionDep, user_id: CurrentUserDep) -> WorkspaceListOut:
    workspaces = await WorkspaceService(session, user_id).list_mine()
    return WorkspaceListOut(workspaces=[_summary_out(w) for w in workspaces])


@router.get("/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace(
    workspace_id: str, session: SessionDep, user_id: CurrentUserDep
) -> WorkspaceOut:
    workspace = await WorkspaceService(session, user_id).get(workspace_id)
    if workspace is None:
        raise _not_found(workspace_id)
    return _out(workspace)


@router.patch("/{workspace_id}", response_model=WorkspaceOut)
async def rename_workspace(
    workspace_id: str, payload: WorkspaceIn, session: SessionDep, user_id: CurrentUserDep
) -> WorkspaceOut:
    workspace = await WorkspaceService(session, user_id).rename(workspace_id, payload.name)
    if workspace is None:
        raise _not_found(workspace_id)
    await session.commit()
    return _out(workspace)


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    workspace_id: str, session: SessionDep, user_id: CurrentUserDep
) -> None:
    if not await WorkspaceService(session, user_id).delete(workspace_id):
        raise _not_found(workspace_id)
    await session.commit()


@router.put("/{workspace_id}/companies/{company_id}", response_model=WorkspaceOut)
async def add_company(
    workspace_id: str, company_id: str, session: SessionDep, user_id: CurrentUserDep
) -> WorkspaceOut:
    """Idempotent: putting a Company that is already there changes nothing."""
    try:
        workspace = await WorkspaceService(session, user_id).add_company(
            workspace_id, company_id
        )
    except UnknownCompany:
        raise HTTPException(
            status_code=404, detail=f"company {company_id} not found"
        ) from None
    if workspace is None:
        raise _not_found(workspace_id)
    await session.commit()
    return _out(workspace)


@router.delete("/{workspace_id}/companies/{company_id}", response_model=WorkspaceOut)
async def remove_company(
    workspace_id: str, company_id: str, session: SessionDep, user_id: CurrentUserDep
) -> WorkspaceOut:
    """Idempotent: removing a Company that isn't there changes nothing."""
    workspace = await WorkspaceService(session, user_id).remove_company(
        workspace_id, company_id
    )
    if workspace is None:
        raise _not_found(workspace_id)
    await session.commit()
    return _out(workspace)
