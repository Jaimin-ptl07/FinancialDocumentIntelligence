"""`/api/v1/retrieval` and `/api/v1/evidence`: the Retrieval service (S4)
over HTTP.

`POST /retrieval` takes an explicit-scope retrieval request and returns an
Evidence package: the Evidence (one shape for every path, each with a stable
location-based id), the scoped Reports with matched / no_match /
not_processed, the paths that ran with ok / degraded / skipped, and -- for a
cross-document request -- the Evidence grouped by Fiscal year. A store outage
degrades a path in the package; it is not an HTTP error.

`GET /evidence/{evidence_id}` returns one Evidence with its full context (the
whole section, the whole table grid, the page, the note) -- what a
verification step checks a claim against.

Every Invitee may read every Report (public filings); the access-token gate
on `v1_router` is the only check."""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from fdi.api.deps import EmbedderDep, SessionDep, StoreDep, VectorStoreDep
from fdi.core.evidence import parse_evidence_id
from fdi.pipeline.retrieval_service import (
    RetrievalRequest,
    RetrievalService,
    inspected_body,
    package_body,
)

router = APIRouter(tags=["retrieval"])


class EvidenceOut(BaseModel):
    evidence_id: str
    kind: str
    doc_id: str
    doc_key: str | None
    version: int | None
    company_id: str | None
    fiscal_year: str | None
    page: int | None
    section: str | None
    section_type: str | None
    basis: str | None
    content: str
    table_index: int | None
    cell_refs: list[str]
    links: list[str]
    bounding_regions: list[dict[str, object]]
    value: float | None
    retrieval_path: str | None
    retrieval_paths: list[str]
    score: float | None


class ReportOutcomeOut(BaseModel):
    doc_id: str
    doc_key: str | None
    version: int | None
    company_id: str | None
    fiscal_year: str | None
    status: Literal["matched", "no_match", "not_processed"]
    evidence_count: int


class PathOutcomeOut(BaseModel):
    path: str
    status: Literal["ok", "degraded", "skipped"]
    reason: str | None
    evidence_count: int


class FiscalYearGroupOut(BaseModel):
    fiscal_year: str | None
    reports: list[ReportOutcomeOut]
    evidence_ids: list[str]


class EvidencePackageOut(BaseModel):
    evidence: list[EvidenceOut]
    reports: list[ReportOutcomeOut]
    paths: list[PathOutcomeOut]
    by_fiscal_year: list[FiscalYearGroupOut] | None


class InspectedEvidenceOut(BaseModel):
    evidence: EvidenceOut
    context: dict[str, object]


@router.post("/retrieval", response_model=EvidencePackageOut)
async def retrieve(
    body: RetrievalRequest,
    session: SessionDep,
    store: StoreDep,
    embedder: EmbedderDep,
    vector_store: VectorStoreDep,
) -> EvidencePackageOut:
    service = RetrievalService(
        session=session, store=store, embedder=embedder, vector_store=vector_store
    )
    package = await service.retrieve(body)
    return EvidencePackageOut.model_validate(package_body(package))


@router.get("/evidence/{evidence_id}", response_model=InspectedEvidenceOut)
async def inspect_evidence(
    evidence_id: str, session: SessionDep, store: StoreDep
) -> InspectedEvidenceOut:
    if parse_evidence_id(evidence_id) is None:
        raise HTTPException(status_code=422, detail=f"not an Evidence id: {evidence_id!r}")
    inspected = await RetrievalService(session=session, store=store).inspect(evidence_id)
    if inspected is None:
        raise HTTPException(status_code=404, detail=f"Evidence {evidence_id} not found")
    return InspectedEvidenceOut.model_validate(inspected_body(inspected))
