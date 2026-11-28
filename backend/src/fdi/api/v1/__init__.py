"""The versioned API: `/api/v1/`, one router module per resource.

New resources land here rather than in the single all-routes module
(`fdi.api.routes`); moving the existing unversioned routes is Epic #17's.
`v1_router` is included by `fdi.api.app` behind the same access-token gate as
every other protected route."""

from fastapi import APIRouter

from fdi.api.v1.companies import router as companies_router
from fdi.api.v1.extraction_schemas import router as extraction_schemas_router
from fdi.api.v1.extractions import router as extractions_router
from fdi.api.v1.reports import router as reports_router
from fdi.api.v1.workspaces import router as workspaces_router

v1_router = APIRouter(prefix="/api/v1")
v1_router.include_router(companies_router)
v1_router.include_router(reports_router)
v1_router.include_router(workspaces_router)
v1_router.include_router(extraction_schemas_router)
v1_router.include_router(extractions_router)
