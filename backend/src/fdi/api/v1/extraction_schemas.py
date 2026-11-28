"""`/api/v1/extraction-schemas`: Extraction schemas, versions and drafts (S3).

Every Invitee may list, read and copy any schema; only its owner edits it,
and nobody edits the FDI standard schema (403 -- a schema's existence is not
a secret, unlike a Workspace's). Edits go to the schema's single draft and
name the draft revision they were based on (409 when someone wrote first);
publishing freezes the draft as the next immutable version (ADR-0004).

Everything goes through `fdi.pipeline.extraction_schemas.SchemaService`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from fdi.api.auth import CurrentUserDep
from fdi.api.deps import SessionDep
from fdi.core.extraction_schema import (
    FieldDefinition,
    FieldDiff,
    GroupDefinition,
    SchemaInputError,
    field_to_mapping,
    group_to_mapping,
)
from fdi.pipeline.extraction_schemas import (
    DraftView,
    FieldNotFound,
    NothingToPublish,
    SchemaNotFound,
    SchemaReadOnly,
    SchemaService,
    SchemaSummary,
    StaleRevision,
    VersionNotFound,
    VersionSummary,
    VersionView,
)

router = APIRouter(prefix="/extraction-schemas", tags=["extraction-schemas"])



# -- bodies ---------------------------------------------------------------


class DerivationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: str = "sum"
    inputs: list[str | dict[str, object]] = []


class _FieldBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str | None = None
    data_type: str | None = None
    unit: str | None = None
    instructions: str | None = None
    statement: str | None = None
    group: str | None = None
    tag: str | None = None
    aliases: list[str] | None = None
    note_aliases: list[str] | None = None
    periods: list[str] | None = None
    allowed_values: list[str] | None = None
    origin: str | None = None
    derivation: DerivationIn | None = None
    required: bool | None = None
    validation: dict[str, object] | None = None
    extra: dict[str, object] | None = None


class FieldIn(_FieldBody):
    """A Field as submitted. Omit `field_id` for a new Field; keep it to
    edit an existing one (its identity, and its cached reads, survive)."""

    field_id: str | None = None
    name: str = Field(min_length=1, max_length=200)
    # Read-only (served on versions): accepted and ignored, so a client can
    # PUT back exactly the Fields it GOT.
    definition_hash: str | None = Field(default=None, exclude=True)


class FieldPatch(_FieldBody):
    name: str | None = Field(default=None, min_length=1, max_length=200)


class GroupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=64)
    title: str | None = None
    retrieval_query: str = Field(min_length=1, max_length=4000)
    locator_keywords: list[str] = []
    basis_preference: str = "standalone"
    statement: str | None = None


class CopyFrom(BaseModel):
    schema_id: str
    version: int | None = Field(default=None, ge=1)


class SchemaCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    copy_from: CopyFrom | None = None


class SchemaPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)


class DraftReplace(BaseModel):
    revision: int = Field(ge=0)
    fields: list[FieldIn]
    groups: list[GroupIn] = []
    review_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class FieldCreate(BaseModel):
    revision: int = Field(ge=0)
    field: FieldIn


class FieldUpdate(BaseModel):
    revision: int = Field(ge=0)
    changes: FieldPatch


class PublishIn(BaseModel):
    revision: int = Field(ge=0)
    notes: str = Field(default="", max_length=4000)


# -- responses --------------------------------------------------------------


class SchemaOut(BaseModel):
    id: str
    name: str
    description: str
    is_standard: bool
    owned_by_me: bool
    owner_user_id: str | None
    latest_version: int | None
    field_count: int
    created_at: datetime
    updated_at: datetime


class SchemaListOut(BaseModel):
    schemas: list[SchemaOut]


class FieldOut(BaseModel):
    field_id: str
    name: str
    description: str
    data_type: str
    unit: str | None
    instructions: str
    statement: str | None
    group: str | None
    tag: str | None
    aliases: list[str]
    note_aliases: list[str]
    periods: list[str]
    allowed_values: list[str]
    origin: str
    derivation: dict[str, object] | None
    required: bool
    validation: dict[str, object]
    extra: dict[str, object]
    definition_hash: str | None = None


class GroupOut(BaseModel):
    key: str
    title: str
    retrieval_query: str
    locator_keywords: list[str]
    basis_preference: str
    statement: str | None


class VersionSummaryOut(BaseModel):
    version: int
    definition_hash: str
    field_count: int
    notes: str
    published_by: str | None
    published_at: datetime
    source_version: str | None


class VersionListOut(BaseModel):
    versions: list[VersionSummaryOut]


class VersionOut(VersionSummaryOut):
    schema_id: str
    review_threshold: float
    groups: list[GroupOut]
    fields: list[FieldOut]


class FieldChangeOut(BaseModel):
    field_id: str
    name: str
    changed: list[str]


class DiffOut(BaseModel):
    added: list[FieldOut]
    removed: list[FieldOut]
    changed: list[FieldChangeOut]


class VersionDiffOut(DiffOut):
    from_version: int
    to_version: int


class DraftOut(BaseModel):
    schema_id: str
    revision: int
    base_version: int | None
    review_threshold: float
    unpublished: bool
    groups: list[GroupOut]
    fields: list[FieldOut]
    changes: DiffOut


# -- mapping ----------------------------------------------------------------


def _schema_out(s: SchemaSummary) -> SchemaOut:
    return SchemaOut(
        id=s.id, name=s.name, description=s.description, is_standard=s.is_standard,
        owned_by_me=s.owned_by_me, owner_user_id=s.owner_user_id,
        latest_version=s.latest_version, field_count=s.field_count,
        created_at=s.created_at, updated_at=s.updated_at,
    )


def _field_out(f: FieldDefinition, hash_: str | None = None) -> FieldOut:
    return FieldOut.model_validate({**field_to_mapping(f), "definition_hash": hash_})


def _group_out(g: GroupDefinition) -> GroupOut:
    return GroupOut.model_validate(group_to_mapping(g))


def _summary_out(v: VersionSummary) -> VersionSummaryOut:
    return VersionSummaryOut(
        version=v.version, definition_hash=v.definition_hash, field_count=v.field_count,
        notes=v.notes, published_by=v.published_by, published_at=v.published_at,
        source_version=v.source_version,
    )


def _version_out(v: VersionView) -> VersionOut:
    return VersionOut(
        **_summary_out(v.summary).model_dump(), schema_id=v.schema_id,
        review_threshold=v.review_threshold, groups=[_group_out(g) for g in v.groups],
        fields=[_field_out(f, v.definition_hashes.get(f.field_id)) for f in v.fields],
    )


def _diff_out(d: FieldDiff) -> DiffOut:
    return DiffOut(
        added=[_field_out(f) for f in d.added], removed=[_field_out(f) for f in d.removed],
        changed=[FieldChangeOut(field_id=c.field_id, name=c.name, changed=list(c.changed))
                 for c in d.changed],
    )


def _draft_out(d: DraftView) -> DraftOut:
    return DraftOut(
        schema_id=d.schema_id, revision=d.revision, base_version=d.base_version,
        review_threshold=d.review_threshold, unpublished=d.unpublished,
        groups=[_group_out(g) for g in d.groups], fields=[_field_out(f) for f in d.fields],
        changes=_diff_out(d.changes),
    )


def _field_body(f: _FieldBody) -> dict[str, object]:
    body = f.model_dump(exclude_unset=True)
    if "derivation" in body and body["derivation"] is None:
        body.pop("derivation")
    return body


async def _call[T](op: Callable[[], Awaitable[T]]) -> T:
    """Run a service call, mapping its refusals onto HTTP answers."""
    try:
        return await op()
    except (SchemaNotFound, VersionNotFound, FieldNotFound) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except SchemaReadOnly as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except StaleRevision as exc:
        raise HTTPException(
            status_code=409, detail={"message": str(exc), "current_revision": exc.current}
        ) from None
    except NothingToPublish as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except SchemaInputError as exc:
        raise HTTPException(
            status_code=422, detail={"message": "the schema is invalid", "errors": exc.errors}
        ) from None


# -- routes -----------------------------------------------------------------


@router.get("", response_model=SchemaListOut)
async def list_schemas(session: SessionDep, user_id: CurrentUserDep) -> SchemaListOut:
    """Every schema: FDI's standard one first, then everyone's by name."""
    schemas = await SchemaService(session, user_id).list_all()
    return SchemaListOut(schemas=[_schema_out(s) for s in schemas])


@router.post("", response_model=SchemaOut, status_code=status.HTTP_201_CREATED)
async def create_schema(
    payload: SchemaCreate, session: SessionDep, user_id: CurrentUserDep
) -> SchemaOut:
    """A new schema owned by the caller: empty, or a copy of another schema's
    version (default its latest) as the new schema's draft. A copy keeps
    every Field's identity, so it reuses the reads its source paid for."""
    service = SchemaService(session, user_id)
    created = await _call(lambda: service.create(
        name=payload.name, description=payload.description,
        copy_from=payload.copy_from.schema_id if payload.copy_from else None,
        copy_version=payload.copy_from.version if payload.copy_from else None,
    ))
    await session.commit()
    return _schema_out(created)


@router.get("/{schema_id}", response_model=SchemaOut)
async def get_schema(schema_id: str, session: SessionDep, user_id: CurrentUserDep) -> SchemaOut:
    service = SchemaService(session, user_id)
    return _schema_out(await _call(lambda: service.get(schema_id)))


@router.patch("/{schema_id}", response_model=SchemaOut)
async def rename_schema(
    schema_id: str, payload: SchemaPatch, session: SessionDep, user_id: CurrentUserDep
) -> SchemaOut:
    service = SchemaService(session, user_id)
    updated = await _call(lambda: service.update(
        schema_id, name=payload.name, description=payload.description
    ))
    await session.commit()
    return _schema_out(updated)


@router.get("/{schema_id}/versions", response_model=VersionListOut)
async def list_versions(
    schema_id: str, session: SessionDep, user_id: CurrentUserDep
) -> VersionListOut:
    service = SchemaService(session, user_id)
    versions = await _call(lambda: service.versions(schema_id))
    return VersionListOut(versions=[_summary_out(v) for v in versions])


@router.get("/{schema_id}/versions/latest", response_model=VersionOut)
async def get_latest_version(
    schema_id: str, session: SessionDep, user_id: CurrentUserDep
) -> VersionOut:
    service = SchemaService(session, user_id)
    return _version_out(await _call(lambda: service.version(schema_id, None)))


@router.get("/{schema_id}/versions/{version}", response_model=VersionOut)
async def get_version(
    schema_id: str, version: int, session: SessionDep, user_id: CurrentUserDep
) -> VersionOut:
    service = SchemaService(session, user_id)
    return _version_out(await _call(lambda: service.version(schema_id, version)))


@router.get("/{schema_id}/versions/{version}/diff", response_model=VersionDiffOut)
async def diff_versions(
    schema_id: str,
    version: int,
    session: SessionDep,
    user_id: CurrentUserDep,
    against: int = Query(ge=1, description="The earlier version to compare with."),
) -> VersionDiffOut:
    """What changed from version `against` to `version`, Field by Field."""
    service = SchemaService(session, user_id)
    diff = await _call(lambda: service.diff(schema_id, against, version))
    return VersionDiffOut(**_diff_out(diff).model_dump(), from_version=against,
                          to_version=version)


@router.get("/{schema_id}/draft", response_model=DraftOut)
async def get_draft(schema_id: str, session: SessionDep, user_id: CurrentUserDep) -> DraftOut:
    """The draft and how it differs from the latest version. With no draft
    yet it reads as the latest version at revision 0."""
    service = SchemaService(session, user_id)
    return _draft_out(await _call(lambda: service.draft(schema_id)))


@router.put("/{schema_id}/draft", response_model=DraftOut)
async def replace_draft(
    schema_id: str, payload: DraftReplace, session: SessionDep, user_id: CurrentUserDep
) -> DraftOut:
    service = SchemaService(session, user_id)
    draft = await _call(lambda: service.replace_draft(
        schema_id, revision=payload.revision,
        fields=[_field_body(f) | ({"name": f.name} if f.name else {}) for f in payload.fields],
        groups=[g.model_dump() for g in payload.groups],
        review_threshold=payload.review_threshold,
    ))
    await session.commit()
    return _draft_out(draft)


@router.post("/{schema_id}/draft/fields", response_model=DraftOut,
             status_code=status.HTTP_201_CREATED)
async def add_field(
    schema_id: str, payload: FieldCreate, session: SessionDep, user_id: CurrentUserDep
) -> DraftOut:
    service = SchemaService(session, user_id)
    body = _field_body(payload.field) | {"name": payload.field.name}
    body.pop("field_id", None)
    draft = await _call(lambda: service.add_field(schema_id, revision=payload.revision,
                                                  field=body))
    await session.commit()
    return _draft_out(draft)


@router.patch("/{schema_id}/draft/fields/{field_id}", response_model=DraftOut)
async def update_field(
    schema_id: str, field_id: str, payload: FieldUpdate, session: SessionDep,
    user_id: CurrentUserDep,
) -> DraftOut:
    service = SchemaService(session, user_id)
    draft = await _call(lambda: service.update_field(
        schema_id, field_id, revision=payload.revision, changes=_field_body(payload.changes)
    ))
    await session.commit()
    return _draft_out(draft)


@router.delete("/{schema_id}/draft/fields/{field_id}", response_model=DraftOut)
async def delete_field(
    schema_id: str, field_id: str, session: SessionDep, user_id: CurrentUserDep,
    revision: int = Query(ge=0),
) -> DraftOut:
    """Removes the Field from the draft. Versions that had it keep it, and
    so do its past Extracted values."""
    service = SchemaService(session, user_id)
    draft = await _call(lambda: service.delete_field(schema_id, field_id, revision=revision))
    await session.commit()
    return _draft_out(draft)


@router.post("/{schema_id}/draft/publish", response_model=VersionOut,
             status_code=status.HTTP_201_CREATED)
async def publish_draft(
    schema_id: str, payload: PublishIn, session: SessionDep, user_id: CurrentUserDep
) -> VersionOut:
    """Freeze the draft as the next version. 409 when the draft is stale or
    identical to the latest version; 422 when it is invalid."""
    service = SchemaService(session, user_id)
    version = await _call(lambda: service.publish(schema_id, revision=payload.revision,
                                                  notes=payload.notes))
    await session.commit()
    return _version_out(version)
