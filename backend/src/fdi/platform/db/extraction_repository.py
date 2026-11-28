"""Persistence for Extraction schemas, per-Field reads, reviews and runs (S3).

The caller owns the transaction: writes flush, the caller commits.
`fdi.pipeline.extraction` is the only caller -- routes and the worker go
through its services, never these tables (same rule as the workspace and
company repositories).

Two invariants live here because the database is what enforces them:

* a version number is chosen under a row lock on its schema
  (`lock_schema`), so two concurrent publishes cannot both claim "next";
* a read is inserted with ON CONFLICT DO NOTHING against the live-key unique
  index, so two runs reading the same Field of the same Report converge on
  one live read (`insert_read` returns whichever won).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from fdi.platform.db.models import (
    ExtractionField,
    ExtractionRun,
    ExtractionRunItem,
    ExtractionRunReport,
    ExtractionSchema,
    ExtractionSchemaDraft,
    ExtractionSchemaVersion,
    FieldRead,
    ValueReview,
)


def _new_id() -> str:
    return str(uuid.uuid4())


class ExtractionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # -- schemas ----------------------------------------------------------

    async def create_schema(
        self, *, owner_user_id: str | None, name: str, description: str,
        standard_key: str | None = None,
    ) -> ExtractionSchema:
        schema = ExtractionSchema(
            id=_new_id(), owner_user_id=owner_user_id, name=name,
            description=description, standard_key=standard_key,
        )
        self._s.add(schema)
        await self._s.flush()
        await self._s.refresh(schema)  # server defaults (created_at/updated_at)
        return schema

    async def get_schema(self, schema_id: str) -> ExtractionSchema | None:
        return await self._s.get(ExtractionSchema, schema_id)

    async def lock_schema(self, schema_id: str) -> ExtractionSchema | None:
        """The schema row, locked FOR UPDATE until the transaction ends."""
        return (
            await self._s.execute(
                select(ExtractionSchema)
                .where(ExtractionSchema.id == schema_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def standard_schema(self, standard_key: str) -> ExtractionSchema | None:
        return (
            await self._s.execute(
                select(ExtractionSchema).where(ExtractionSchema.standard_key == standard_key)
            )
        ).scalar_one_or_none()

    async def list_schemas(self) -> list[ExtractionSchema]:
        """FDI's schemas first, then by name."""
        rows = (
            await self._s.execute(
                select(ExtractionSchema).order_by(
                    ExtractionSchema.owner_user_id.is_not(None),
                    func.lower(ExtractionSchema.name),
                    ExtractionSchema.created_at,
                )
            )
        ).scalars().all()
        return list(rows)

    async def rename_schema(
        self, schema: ExtractionSchema, *, name: str | None, description: str | None
    ) -> None:
        if name is not None:
            schema.name = name
        if description is not None:
            schema.description = description
        schema.updated_at = func.now()  # type: ignore[assignment,unused-ignore]
        await self._s.flush()
        await self._s.refresh(schema)

    # -- versions ---------------------------------------------------------

    async def latest_version(self, schema_id: str) -> ExtractionSchemaVersion | None:
        return (
            await self._s.execute(
                select(ExtractionSchemaVersion)
                .where(ExtractionSchemaVersion.schema_id == schema_id)
                .order_by(ExtractionSchemaVersion.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def latest_versions(
        self, schema_ids: Sequence[str]
    ) -> dict[str, ExtractionSchemaVersion]:
        if not schema_ids:
            return {}
        sub = (
            select(
                ExtractionSchemaVersion.schema_id,
                func.max(ExtractionSchemaVersion.version).label("v"),
            )
            .where(ExtractionSchemaVersion.schema_id.in_(schema_ids))
            .group_by(ExtractionSchemaVersion.schema_id)
            .subquery()
        )
        rows = (
            await self._s.execute(
                select(ExtractionSchemaVersion).join(
                    sub,
                    (ExtractionSchemaVersion.schema_id == sub.c.schema_id)
                    & (ExtractionSchemaVersion.version == sub.c.v),
                )
            )
        ).scalars().all()
        return {v.schema_id: v for v in rows}

    async def get_version(self, schema_id: str, version: int) -> ExtractionSchemaVersion | None:
        return (
            await self._s.execute(
                select(ExtractionSchemaVersion).where(
                    ExtractionSchemaVersion.schema_id == schema_id,
                    ExtractionSchemaVersion.version == version,
                )
            )
        ).scalar_one_or_none()

    async def version_by_id(self, version_id: str) -> ExtractionSchemaVersion | None:
        return await self._s.get(ExtractionSchemaVersion, version_id)

    async def list_versions(self, schema_id: str) -> list[ExtractionSchemaVersion]:
        rows = (
            await self._s.execute(
                select(ExtractionSchemaVersion)
                .where(ExtractionSchemaVersion.schema_id == schema_id)
                .order_by(ExtractionSchemaVersion.version.desc())
            )
        ).scalars().all()
        return list(rows)

    async def fields_of(self, version_id: str) -> list[ExtractionField]:
        rows = (
            await self._s.execute(
                select(ExtractionField)
                .where(ExtractionField.version_id == version_id)
                .order_by(ExtractionField.position)
            )
        ).scalars().all()
        return list(rows)

    async def create_version(
        self,
        *,
        schema_id: str,
        version: int,
        definition_hash: str,
        review_threshold: float,
        groups: list[dict[str, object]],
        fields: Sequence[tuple[Mapping[str, object], str]],
        notes: str,
        published_by: str | None,
        source_version: str | None = None,
    ) -> ExtractionSchemaVersion:
        """`fields` is (definition mapping, definition hash) in order. Call
        with the schema locked (`lock_schema`)."""
        row = ExtractionSchemaVersion(
            id=_new_id(), schema_id=schema_id, version=version,
            definition_hash=definition_hash, review_threshold=review_threshold,
            groups=groups, field_count=len(fields), notes=notes,
            published_by=published_by, source_version=source_version,
        )
        self._s.add(row)
        await self._s.flush()
        if fields:
            await self._s.execute(
                pg_insert(ExtractionField),
                [
                    {
                        "id": _new_id(), "version_id": row.id,
                        "field_id": str(definition["field_id"]), "position": i,
                        "name": str(definition["name"]), "tag": definition.get("tag"),
                        "origin": str(definition["origin"]), "definition_hash": hash_,
                        "definition": dict(definition),
                    }
                    for i, (definition, hash_) in enumerate(fields)
                ],
            )
        await self._s.execute(
            update(ExtractionSchema)
            .where(ExtractionSchema.id == schema_id)
            .values(updated_at=func.now())
            .execution_options(synchronize_session=False)
        )
        await self._s.flush()
        await self._s.refresh(row)  # published_at
        schema = await self._s.get(ExtractionSchema, schema_id)
        if schema is not None:
            await self._s.refresh(schema)  # the updated_at just written
        return row

    # -- drafts -----------------------------------------------------------

    async def get_draft(self, schema_id: str) -> ExtractionSchemaDraft | None:
        return await self._s.get(ExtractionSchemaDraft, schema_id)

    async def save_draft(
        self,
        *,
        schema_id: str,
        base_version: int | None,
        fields: list[dict[str, object]],
        groups: list[dict[str, object]],
        review_threshold: float,
        updated_by: str | None,
        expected_revision: int | None,
    ) -> ExtractionSchemaDraft | None:
        """Create the draft (`expected_revision` None) or replace it when its
        revision is still `expected_revision`; None when someone else wrote
        first. Compare-and-set in one statement, so it holds without a lock."""
        if expected_revision is None:
            stmt = (
                pg_insert(ExtractionSchemaDraft)
                .values(
                    schema_id=schema_id, base_version=base_version, revision=1,
                    fields=fields, groups=groups, review_threshold=review_threshold,
                    updated_by=updated_by,
                )
                .on_conflict_do_nothing(index_elements=["schema_id"])
                .returning(ExtractionSchemaDraft.schema_id)
            )
            if (await self._s.execute(stmt)).scalar_one_or_none() is None:
                return None
        else:
            stmt2 = (
                update(ExtractionSchemaDraft)
                .where(
                    ExtractionSchemaDraft.schema_id == schema_id,
                    ExtractionSchemaDraft.revision == expected_revision,
                )
                .values(
                    base_version=base_version, revision=expected_revision + 1,
                    fields=fields, groups=groups, review_threshold=review_threshold,
                    updated_by=updated_by, updated_at=func.now(),
                )
                .returning(ExtractionSchemaDraft.schema_id)
                .execution_options(synchronize_session=False)
            )
            if (await self._s.execute(stmt2)).scalar_one_or_none() is None:
                return None
        return (
            await self._s.execute(
                select(ExtractionSchemaDraft)
                .where(ExtractionSchemaDraft.schema_id == schema_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()

    # -- reads ------------------------------------------------------------

    async def live_reads(
        self, doc_ids: Sequence[str], field_ids: Sequence[str]
    ) -> list[FieldRead]:
        """Every un-superseded read of these Fields in these Reports, newest
        first (per pair the first one is the latest)."""
        if not doc_ids or not field_ids:
            return []
        rows = (
            await self._s.execute(
                select(FieldRead)
                .where(
                    FieldRead.doc_id.in_(doc_ids),
                    FieldRead.field_id.in_(field_ids),
                    FieldRead.superseded_at.is_(None),
                )
                .order_by(FieldRead.created_at.desc(), FieldRead.id)
            )
        ).scalars().all()
        return list(rows)

    async def supersede(self, *, doc_id: str, field_id: str, cache_key: str) -> None:
        """Retire the live read for this key (a forced or failed re-read is
        about to take its place). The row is kept: history is never lost."""
        await self._s.execute(
            update(FieldRead)
            .where(
                FieldRead.doc_id == doc_id, FieldRead.field_id == field_id,
                FieldRead.cache_key == cache_key, FieldRead.superseded_at.is_(None),
            )
            .values(superseded_at=func.now())
            .execution_options(synchronize_session=False)
        )

    async def insert_read(self, values: Mapping[str, object]) -> str:
        """Insert one read; return the id of the LIVE read for its key --
        this one, or the one a concurrent run stored first."""
        row = {"id": _new_id(), **values}
        stmt = (
            pg_insert(FieldRead)
            .values(**row)
            .on_conflict_do_nothing(
                index_elements=["doc_id", "field_id", "cache_key"],
                index_where=FieldRead.superseded_at.is_(None),
            )
            .returning(FieldRead.id)
        )
        inserted = (await self._s.execute(stmt)).scalar_one_or_none()
        if inserted is not None:
            return str(inserted)
        existing = (
            await self._s.execute(
                select(FieldRead.id).where(
                    FieldRead.doc_id == values["doc_id"],
                    FieldRead.field_id == values["field_id"],
                    FieldRead.cache_key == values["cache_key"],
                    FieldRead.superseded_at.is_(None),
                )
            )
        ).scalar_one()
        return str(existing)

    async def get_read(self, read_id: str) -> FieldRead | None:
        return await self._s.get(FieldRead, read_id)

    async def reads_by_ids(self, read_ids: Iterable[str]) -> dict[str, FieldRead]:
        ids = list(set(read_ids))
        if not ids:
            return {}
        rows = (await self._s.execute(select(FieldRead).where(FieldRead.id.in_(ids)))).scalars()
        return {r.id: r for r in rows}

    # -- reviews ----------------------------------------------------------

    async def add_review(
        self, *, read_id: str, action: str, value: object, note: str, user_id: str
    ) -> ValueReview:
        review = ValueReview(
            id=_new_id(), read_id=read_id, action=action, value=value, note=note,
            user_id=user_id,
        )
        self._s.add(review)
        await self._s.flush()
        await self._s.refresh(review)
        return review

    async def latest_reviews(self, read_ids: Iterable[str]) -> dict[str, ValueReview]:
        ids = list(set(read_ids))
        if not ids:
            return {}
        rows = (
            await self._s.execute(
                select(ValueReview)
                .where(ValueReview.read_id.in_(ids))
                .order_by(ValueReview.read_id, ValueReview.created_at.desc(), ValueReview.id)
                .distinct(ValueReview.read_id)
            )
        ).scalars().all()
        return {r.read_id: r for r in rows}

    async def reviews_of(self, read_id: str) -> list[ValueReview]:
        rows = (
            await self._s.execute(
                select(ValueReview)
                .where(ValueReview.read_id == read_id)
                .order_by(ValueReview.created_at, ValueReview.id)
            )
        ).scalars().all()
        return list(rows)

    # -- runs -------------------------------------------------------------

    async def create_run(
        self,
        *,
        run_id: str,
        schema_version_id: str,
        user_id: str,
        doc_ids: Sequence[str],
        field_ids: Sequence[str] | None,
        forced_field_ids: Sequence[str],
        retry_of: str | None = None,
    ) -> ExtractionRun:
        run = ExtractionRun(
            id=run_id, schema_version_id=schema_version_id, user_id=user_id,
            status="queued", field_ids=None if field_ids is None else list(field_ids),
            forced_field_ids=list(forced_field_ids), counts={}, retry_of=retry_of,
        )
        self._s.add(run)
        await self._s.flush()
        await self._s.refresh(run)
        self._s.add_all(
            ExtractionRunReport(run_id=run_id, doc_id=d, position=i, status="queued", counts={})
            for i, d in enumerate(doc_ids)
        )
        await self._s.flush()
        return run

    async def attach_job(self, run_id: str, api_job_id: str) -> None:
        await self._s.execute(
            update(ExtractionRun)
            .where(ExtractionRun.id == run_id)
            .values(api_job_id=api_job_id)
            .execution_options(synchronize_session=False)
        )

    async def get_run(self, run_id: str) -> ExtractionRun | None:
        return (
            await self._s.execute(
                select(ExtractionRun)
                .where(ExtractionRun.id == run_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def list_runs(self, user_id: str, *, limit: int, offset: int) -> list[ExtractionRun]:
        rows = (
            await self._s.execute(
                select(ExtractionRun)
                .where(ExtractionRun.user_id == user_id)
                .order_by(ExtractionRun.created_at.desc(), ExtractionRun.id)
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()
        return list(rows)

    async def run_reports(self, run_id: str) -> list[ExtractionRunReport]:
        rows = (
            await self._s.execute(
                select(ExtractionRunReport)
                .where(ExtractionRunReport.run_id == run_id)
                .order_by(ExtractionRunReport.position)
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
        return list(rows)

    async def set_run_status(
        self, run_id: str, *, status: str, counts: Mapping[str, int] | None = None,
        error: str | None = None, started: bool = False, finished: bool = False,
    ) -> None:
        values: dict[str, object] = {"status": status}
        if counts is not None:
            values["counts"] = dict(counts)
        if error is not None:
            values["error"] = error
        if started:
            values["started_at"] = func.now()
        if finished:
            values["finished_at"] = func.now()
        await self._s.execute(
            update(ExtractionRun)
            .where(ExtractionRun.id == run_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )

    async def set_report_status(
        self, run_id: str, doc_id: str, *, status: str, counts: Mapping[str, int],
        error: str | None = None,
    ) -> None:
        await self._s.execute(
            update(ExtractionRunReport)
            .where(ExtractionRunReport.run_id == run_id, ExtractionRunReport.doc_id == doc_id)
            .values(status=status, counts=dict(counts), error=error)
            .execution_options(synchronize_session=False)
        )

    async def add_items(self, items: Sequence[Mapping[str, object]]) -> None:
        """Record planned items. Idempotent: a redelivered run keeps the
        items (and outcomes) it already has."""
        if not items:
            return
        await self._s.execute(
            pg_insert(ExtractionRunItem)
            .values([dict(i) for i in items])
            .on_conflict_do_nothing(index_elements=["run_id", "doc_id", "field_id"])
        )

    async def run_items(
        self, run_id: str, *, doc_id: str | None = None
    ) -> list[ExtractionRunItem]:
        stmt = select(ExtractionRunItem).where(ExtractionRunItem.run_id == run_id)
        if doc_id is not None:
            stmt = stmt.where(ExtractionRunItem.doc_id == doc_id)
        rows = (
            await self._s.execute(stmt.execution_options(populate_existing=True))
        ).scalars().all()
        return list(rows)

    async def set_item_outcomes(
        self, run_id: str, doc_id: str, outcomes: Mapping[str, tuple[str, str | None]]
    ) -> None:
        """`outcomes` maps Field id -> (outcome, read id). One ORM bulk
        UPDATE by primary key, not a statement per Field."""
        if not outcomes:
            return
        await self._s.execute(
            update(ExtractionRunItem),
            [
                {"run_id": run_id, "doc_id": doc_id, "field_id": field_id,
                 "outcome": outcome, "read_id": read_id}
                for field_id, (outcome, read_id) in outcomes.items()
            ],
        )
