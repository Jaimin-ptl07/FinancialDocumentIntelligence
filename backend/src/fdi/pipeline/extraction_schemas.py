"""Extraction schemas an Invitee owns (S3, ADR-0004).

`SchemaService` is the only way to create, copy, edit and publish an
Extraction schema; the API and the standard-schema import go through it,
never the tables. Rules it enforces:

* every Invitee may READ, COPY and RUN any schema; only the owner EDITS, and
  nobody edits the FDI standard schema (owner NULL) -- it is imported;
* a schema has at most one mutable draft; every draft write names the
  revision it was based on and is refused when someone wrote first;
* publishing validates the whole draft, refuses a draft identical to the
  latest version, and freezes it as the next version number (chosen under a
  row lock, so two concurrent publishes cannot both claim it);
* a copy keeps each Field's identity -- it is the same Field until it is
  edited -- so the copy reuses every read its source already paid for.

pipeline -> platform + core, like `fdi.pipeline.workspaces`. The caller owns
the transaction: writes flush, the caller commits.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core.extraction_schema import (
    DEFAULT_REVIEW_THRESHOLD,
    CompiledSchema,
    FieldDefinition,
    FieldDiff,
    GroupDefinition,
    SchemaInputError,
    compile_standard_schema,
    default_instructions,
    definition_hash,
    diff_fields,
    field_from_mapping,
    field_to_mapping,
    group_from_mapping,
    group_to_mapping,
    resolve_group,
    schema_hash,
    validate_fields,
)
from fdi.platform.db.extraction_repository import ExtractionRepository
from fdi.platform.db.models import (
    ExtractionField,
    ExtractionSchema,
    ExtractionSchemaDraft,
    ExtractionSchemaVersion,
)

STANDARD_KEY: Final = "fdi-standard"
STANDARD_NAME: Final = "FDI standard"
STANDARD_DESCRIPTION: Final = (
    "FDI's standard financial-statement schema (v4.3 taxonomy): profit and loss, "
    "assets, liabilities, equity and cash flow. Read-only -- copy it to edit."
)


class SchemaNotFound(LookupError):
    pass


class VersionNotFound(LookupError):
    pass


class FieldNotFound(LookupError):
    pass


class SchemaReadOnly(PermissionError):
    """The caller may not edit this schema (not the owner, or FDI's)."""


class StaleRevision(RuntimeError):
    """The draft changed since the revision the caller based its edit on."""

    def __init__(self, current: int) -> None:
        super().__init__(f"the draft is at revision {current}; re-read it and reapply")
        self.current = current


class NothingToPublish(RuntimeError):
    pass


@dataclass(frozen=True)
class SchemaSummary:
    id: str
    name: str
    description: str
    owner_user_id: str | None
    is_standard: bool
    owned_by_me: bool
    latest_version: int | None
    field_count: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class VersionSummary:
    version: int
    definition_hash: str
    field_count: int
    notes: str
    published_by: str | None
    published_at: datetime
    source_version: str | None


@dataclass(frozen=True)
class VersionView:
    schema_id: str
    summary: VersionSummary
    review_threshold: float
    groups: tuple[GroupDefinition, ...]
    fields: tuple[FieldDefinition, ...]
    definition_hashes: Mapping[str, str]


@dataclass(frozen=True)
class DraftView:
    schema_id: str
    revision: int
    base_version: int | None
    review_threshold: float
    groups: tuple[GroupDefinition, ...]
    fields: tuple[FieldDefinition, ...]
    changes: FieldDiff
    unpublished: bool


def is_editable_by(schema: ExtractionSchema, user_id: str) -> bool:
    return schema.owner_user_id is not None and schema.owner_user_id == user_id


def _fields_from_rows(rows: Sequence[ExtractionField]) -> tuple[FieldDefinition, ...]:
    return tuple(field_from_mapping(r.definition, field_id=r.field_id) for r in rows)


def _groups_from(raw: Sequence[Mapping[str, object]]) -> tuple[GroupDefinition, ...]:
    return tuple(group_from_mapping(g) for g in raw)


def _summary(v: ExtractionSchemaVersion) -> VersionSummary:
    return VersionSummary(
        version=v.version, definition_hash=v.definition_hash, field_count=v.field_count,
        notes=v.notes, published_by=v.published_by, published_at=v.published_at,
        source_version=v.source_version,
    )


def _threshold(value: object) -> float:
    if (isinstance(value, int | float) and not isinstance(value, bool)
            and 0.0 <= float(value) <= 1.0):
        return float(value)
    raise SchemaInputError(['"review_threshold" must be a number from 0 to 1'])


class SchemaService:
    """Every method acts as `user_id`."""

    def __init__(self, session: AsyncSession, user_id: str | None) -> None:
        self._s = session
        self._repo = ExtractionRepository(session)
        self._user = user_id

    # -- reading ----------------------------------------------------------

    async def list_all(self) -> list[SchemaSummary]:
        schemas = await self._repo.list_schemas()
        latest = await self._repo.latest_versions([s.id for s in schemas])
        return [self._schema_summary(s, latest.get(s.id)) for s in schemas]

    async def get(self, schema_id: str) -> SchemaSummary:
        schema = await self._schema(schema_id)
        return self._schema_summary(schema, await self._repo.latest_version(schema_id))

    async def versions(self, schema_id: str) -> list[VersionSummary]:
        await self._schema(schema_id)
        return [_summary(v) for v in await self._repo.list_versions(schema_id)]

    async def version(self, schema_id: str, version: int | None) -> VersionView:
        """`version` None means the latest published one."""
        await self._schema(schema_id)
        row = (await self._repo.latest_version(schema_id) if version is None
               else await self._repo.get_version(schema_id, version))
        if row is None:
            raise VersionNotFound(
                f"schema {schema_id} has no published version"
                if version is None else f"schema {schema_id} has no version {version}"
            )
        return await self._version_view(row)

    async def version_by_id(self, version_id: str) -> VersionView:
        row = await self._repo.version_by_id(version_id)
        if row is None:
            raise VersionNotFound(f"no schema version {version_id}")
        return await self._version_view(row)

    async def diff(self, schema_id: str, from_version: int, to_version: int) -> FieldDiff:
        old = await self.version(schema_id, from_version)
        new = await self.version(schema_id, to_version)
        return diff_fields(old.fields, new.fields)

    async def draft(self, schema_id: str) -> DraftView:
        schema = await self._schema(schema_id)
        return await self._draft_view(schema)

    # -- creating ---------------------------------------------------------

    async def create(
        self, *, name: str, description: str, copy_from: str | None = None,
        copy_version: int | None = None,
    ) -> SchemaSummary:
        """A new schema owned by the caller: empty, or a copy of another
        schema's published version (default: its latest) as its draft."""
        if self._user is None:
            raise SchemaReadOnly("only an Invitee can own a schema")
        fields: tuple[FieldDefinition, ...] = ()
        groups: tuple[GroupDefinition, ...] = ()
        threshold = DEFAULT_REVIEW_THRESHOLD
        if copy_from is not None:
            source = await self.version(copy_from, copy_version)
            fields, groups, threshold = source.fields, source.groups, source.review_threshold
        schema = await self._repo.create_schema(
            owner_user_id=self._user, name=name, description=description
        )
        if copy_from is not None:
            # An empty schema needs no row: its draft reads as empty at
            # revision 0 until the first edit. A copy's draft is the copy.
            await self._repo.save_draft(
                schema_id=schema.id, base_version=None,
                fields=[field_to_mapping(f) for f in fields],
                groups=[group_to_mapping(g) for g in groups],
                review_threshold=threshold, updated_by=self._user, expected_revision=None,
            )
        return self._schema_summary(schema, None)

    async def update(
        self, schema_id: str, *, name: str | None, description: str | None
    ) -> SchemaSummary:
        schema = await self._editable(schema_id)
        await self._repo.rename_schema(schema, name=name, description=description)
        return self._schema_summary(schema, await self._repo.latest_version(schema_id))

    # -- the draft ----------------------------------------------------------

    async def replace_draft(
        self,
        schema_id: str,
        *,
        revision: int,
        fields: Sequence[object],
        groups: Sequence[object],
        review_threshold: object = None,
    ) -> DraftView:
        """Replace the whole draft. An entry carrying a `field_id` keeps that
        identity (an edit); one without gets a new identity (a new Field)."""
        schema = await self._editable(schema_id)
        current = await self._draft_view(schema)
        parsed_groups = self._parse_groups(groups)
        parsed = self._parse_fields(fields)
        threshold = (current.review_threshold if review_threshold is None
                     else _threshold(review_threshold))
        return await self._write_draft(schema, current, parsed, parsed_groups, threshold,
                                       revision)

    async def add_field(self, schema_id: str, *, revision: int, field: object) -> DraftView:
        schema = await self._editable(schema_id)
        current = await self._draft_view(schema)
        new = field_from_mapping(field, field_id=str(uuid.uuid4()))
        return await self._write_draft(schema, current, (*current.fields, new),
                                       current.groups, current.review_threshold, revision)

    async def update_field(
        self, schema_id: str, field_id: str, *, revision: int, changes: Mapping[str, object]
    ) -> DraftView:
        """Merge `changes` into the Field's definition (a PATCH); its identity
        and position never change."""
        schema = await self._editable(schema_id)
        current = await self._draft_view(schema)
        out: list[FieldDefinition] = []
        found = False
        for f in current.fields:
            if f.field_id == field_id:
                merged = {**field_to_mapping(f), **{k: v for k, v in changes.items()
                                                     if k != "field_id"}}
                if "instructions" not in changes and any(
                    k in changes for k in ("name", "description", "statement")
                ) and f.instructions == _default_for(f):
                    merged["instructions"] = ""  # keep the default in step
                out.append(field_from_mapping(merged, field_id=f.field_id))
                found = True
            else:
                out.append(f)
        if not found:
            raise FieldNotFound(f"the draft has no Field {field_id}")
        return await self._write_draft(schema, current, out, current.groups,
                                       current.review_threshold, revision)

    async def delete_field(self, schema_id: str, field_id: str, *, revision: int) -> DraftView:
        """Removes the Field from the draft only: versions that had it, and
        its reads, are untouched (history is never rewritten)."""
        schema = await self._editable(schema_id)
        current = await self._draft_view(schema)
        kept = tuple(f for f in current.fields if f.field_id != field_id)
        if len(kept) == len(current.fields):
            raise FieldNotFound(f"the draft has no Field {field_id}")
        return await self._write_draft(schema, current, kept, current.groups,
                                       current.review_threshold, revision)

    async def publish(self, schema_id: str, *, revision: int, notes: str) -> VersionView:
        locked = await self._repo.lock_schema(schema_id)
        if locked is None:
            raise SchemaNotFound(f"schema {schema_id} not found")
        if not is_editable_by(locked, self._user or ""):
            raise SchemaReadOnly(self._read_only_reason(locked))
        current = await self._draft_view(locked)
        if current.revision != revision:
            raise StaleRevision(current.revision)
        if not current.unpublished:
            raise NothingToPublish("the draft is identical to the latest published version")
        errors = validate_fields(current.fields, current.groups)
        if errors:
            raise SchemaInputError(errors)
        latest = await self._repo.latest_version(schema_id)
        row = await self._create_version(
            schema_id=schema_id, version=(latest.version + 1) if latest else 1,
            fields=current.fields, groups=current.groups,
            threshold=current.review_threshold, notes=notes, source_version=None,
        )
        # The draft is never deleted: publishing ADVANCES it (same content,
        # now equal to the new version) with the same compare-and-set every
        # edit uses. So revisions only ever grow -- a stale tab's revision can
        # never match again -- and an edit that slipped in while this publish
        # ran makes the CAS fail, aborting the publish (the transaction rolls
        # back the version) instead of being silently thrown away.
        advanced = await self._repo.save_draft(
            schema_id=schema_id, base_version=row.version,
            fields=[field_to_mapping(f) for f in current.fields],
            groups=[group_to_mapping(g) for g in current.groups],
            review_threshold=current.review_threshold, updated_by=self._user,
            expected_revision=revision if revision > 0 else None,
        )
        if advanced is None:
            fresh = await self._repo.get_draft(schema_id)
            raise StaleRevision(fresh.revision if fresh else 0)
        return await self._version_view(row)

    # -- the standard schema ------------------------------------------------

    async def import_standard(
        self, compiled: CompiledSchema, *, notes: str = ""
    ) -> tuple[SchemaSummary, VersionView | None]:
        """Import `compiled` as the next version of the FDI standard schema,
        creating the schema the first time. Idempotent: an unchanged
        definition publishes nothing (returns None for the version).
        Serialised by a transaction-scoped advisory lock so two booting
        processes cannot both create it."""
        await self._s.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": STANDARD_KEY}
        )
        errors = validate_fields(compiled.fields, compiled.groups)
        if errors:
            raise SchemaInputError(errors)
        schema = await self._repo.standard_schema(STANDARD_KEY)
        if schema is None:
            schema = await self._repo.create_schema(
                owner_user_id=None, name=STANDARD_NAME, description=STANDARD_DESCRIPTION,
                standard_key=STANDARD_KEY,
            )
        await self._repo.lock_schema(schema.id)
        latest = await self._repo.latest_version(schema.id)
        new_hash = schema_hash(compiled.fields, compiled.groups, DEFAULT_REVIEW_THRESHOLD)
        if latest is not None and latest.definition_hash == new_hash:
            return self._schema_summary(schema, latest), None
        row = await self._create_version(
            schema_id=schema.id, version=(latest.version + 1) if latest else 1,
            fields=compiled.fields, groups=compiled.groups,
            threshold=DEFAULT_REVIEW_THRESHOLD,
            notes=notes or f"Imported from the standard document {compiled.source_version}",
            source_version=compiled.source_version or None,
        )
        return self._schema_summary(schema, row), await self._version_view(row)

    async def standard_schema_id(self) -> str | None:
        schema = await self._repo.standard_schema(STANDARD_KEY)
        return None if schema is None else schema.id

    # -- internals ----------------------------------------------------------

    async def _schema(self, schema_id: str) -> ExtractionSchema:
        schema = await self._repo.get_schema(schema_id)
        if schema is None:
            raise SchemaNotFound(f"schema {schema_id} not found")
        return schema

    async def _editable(self, schema_id: str) -> ExtractionSchema:
        schema = await self._schema(schema_id)
        if not is_editable_by(schema, self._user or ""):
            raise SchemaReadOnly(self._read_only_reason(schema))
        return schema

    @staticmethod
    def _read_only_reason(schema: ExtractionSchema) -> str:
        if schema.owner_user_id is None:
            return "the FDI standard schema is read-only; copy it to make your own"
        return "only the schema's owner can edit it; copy it to make your own"

    def _schema_summary(
        self, schema: ExtractionSchema, latest: ExtractionSchemaVersion | None
    ) -> SchemaSummary:
        return SchemaSummary(
            id=schema.id, name=schema.name, description=schema.description,
            owner_user_id=schema.owner_user_id, is_standard=schema.standard_key is not None,
            owned_by_me=is_editable_by(schema, self._user or ""),
            latest_version=latest.version if latest else None,
            field_count=latest.field_count if latest else 0,
            created_at=schema.created_at, updated_at=schema.updated_at,
        )

    async def _version_view(self, row: ExtractionSchemaVersion) -> VersionView:
        field_rows = await self._repo.fields_of(row.id)
        return VersionView(
            schema_id=row.schema_id, summary=_summary(row),
            review_threshold=row.review_threshold, groups=_groups_from(row.groups),
            fields=_fields_from_rows(field_rows),
            definition_hashes={r.field_id: r.definition_hash for r in field_rows},
        )

    async def _draft_view(self, schema: ExtractionSchema) -> DraftView:
        draft: ExtractionSchemaDraft | None = await self._repo.get_draft(schema.id)
        latest = await self._repo.latest_version(schema.id)
        latest_view = await self._version_view(latest) if latest else None
        if draft is None:
            # No draft yet: it reads as the latest version, at revision 0.
            fields = latest_view.fields if latest_view else ()
            groups = latest_view.groups if latest_view else ()
            threshold = (latest_view.review_threshold if latest_view
                         else DEFAULT_REVIEW_THRESHOLD)
            revision, base = 0, latest.version if latest else None
        else:
            fields = tuple(field_from_mapping(f, field_id=str(f["field_id"]))
                           for f in draft.fields)
            groups = _groups_from(draft.groups)
            threshold, revision, base = draft.review_threshold, draft.revision, draft.base_version
        latest_fields = latest_view.fields if latest_view else ()
        unpublished = (
            latest is None and bool(fields)
        ) or (
            latest is not None
            and schema_hash(fields, groups, threshold) != latest.definition_hash
        )
        return DraftView(
            schema_id=schema.id, revision=revision, base_version=base,
            review_threshold=threshold, groups=groups, fields=fields,
            changes=diff_fields(latest_fields, fields), unpublished=unpublished,
        )

    async def _write_draft(
        self,
        schema: ExtractionSchema,
        current: DraftView,
        fields: Sequence[FieldDefinition],
        groups: Sequence[GroupDefinition],
        threshold: float,
        revision: int,
    ) -> DraftView:
        if revision != current.revision:
            raise StaleRevision(current.revision)
        errors = validate_fields(fields, groups)
        if errors:
            raise SchemaInputError(errors)
        saved = await self._repo.save_draft(
            schema_id=schema.id, base_version=current.base_version,
            fields=[field_to_mapping(f) for f in fields],
            groups=[group_to_mapping(g) for g in groups],
            review_threshold=threshold, updated_by=self._user,
            expected_revision=None if revision == 0 else revision,
        )
        if saved is None:
            fresh = await self._repo.get_draft(schema.id)
            raise StaleRevision(fresh.revision if fresh else 0)
        return await self._draft_view(schema)

    @staticmethod
    def _parse_fields(raw: Sequence[object]) -> tuple[FieldDefinition, ...]:
        errors: list[str] = []
        out: list[FieldDefinition] = []
        for i, entry in enumerate(raw):
            field_id = entry.get("field_id") if isinstance(entry, Mapping) else None
            if field_id is not None:
                try:
                    field_id = str(uuid.UUID(str(field_id)))
                except ValueError:
                    errors.append(f'fields[{i}]."field_id" must be a UUID')
                    continue
            try:
                out.append(field_from_mapping(
                    entry, field_id=field_id or str(uuid.uuid4()), where=f"fields[{i}]"
                ))
            except SchemaInputError as exc:
                errors.extend(exc.errors)
        if errors:
            raise SchemaInputError(errors)
        return tuple(out)

    @staticmethod
    def _parse_groups(raw: Sequence[object]) -> tuple[GroupDefinition, ...]:
        errors: list[str] = []
        out: list[GroupDefinition] = []
        for i, entry in enumerate(raw):
            try:
                out.append(group_from_mapping(entry, where=f"groups[{i}]"))
            except SchemaInputError as exc:
                errors.extend(exc.errors)
        if errors:
            raise SchemaInputError(errors)
        return tuple(out)

    async def _create_version(
        self,
        *,
        schema_id: str,
        version: int,
        fields: Sequence[FieldDefinition],
        groups: Sequence[GroupDefinition],
        threshold: float,
        notes: str,
        source_version: str | None,
    ) -> ExtractionSchemaVersion:
        by_key = {g.key: g for g in groups}
        return await self._repo.create_version(
            schema_id=schema_id, version=version,
            definition_hash=schema_hash(fields, groups, threshold),
            review_threshold=threshold, groups=[group_to_mapping(g) for g in groups],
            fields=[(field_to_mapping(f), definition_hash(f, resolve_group(f, by_key)))
                    for f in fields],
            notes=notes, published_by=self._user, source_version=source_version,
        )


def _default_for(f: FieldDefinition) -> str:
    return default_instructions(f.name, f.description, f.statement)


async def import_standard_schema_file(
    session: AsyncSession, path: str, *, only_if_absent: bool
) -> VersionView | None:
    """Read the standard document at `path` and import it (see
    `SchemaService.import_standard`). With `only_if_absent`, a database that
    already holds a standard schema is left alone -- what start-up does, so a
    deploy never silently publishes a new standard version; publishing one is
    the explicit `fdi-admin import-standard-schema`. Returns the version
    published, or None. Raises on an unreadable or invalid document."""
    service = SchemaService(session, None)
    if only_if_absent and await service.standard_schema_id() is not None:
        return None

    def _read() -> object:
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    doc = await asyncio.to_thread(_read)
    if not isinstance(doc, dict):
        raise SchemaInputError([f"{path} does not hold a JSON object"])
    _summary, version = await service.import_standard(compile_standard_schema(doc))
    return version
