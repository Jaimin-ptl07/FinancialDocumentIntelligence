"""Proves the migration applies to a real PostgreSQL 18.4 and matches the ORM."""

import pytest
import sqlalchemy
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


async def test_migration_creates_jobs_table(migrated_engine: AsyncEngine) -> None:
    async with migrated_engine.connect() as conn:
        rows = await conn.execute(sqlalchemy.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'jobs'"
        ))
        cols = {r[0] for r in rows}
    assert {"id", "status", "artifact_key", "error",
            "correlation_id", "created_at", "updated_at"} == cols


async def test_status_check_constraint_rejects_unknown_values(migrated_engine: AsyncEngine) -> None:
    """The CHECK is the only thing enforcing the status domain -- prove it bites.

    `Job.status` is String(16), not Enum(JobStatus), so nothing in Python stops
    status="banana": JobStatus IS a str, so mypy accepts the enum and any string
    alike. Without this constraint the four-value domain is a convention, and
    conventions rot silently. A gate never seen to fail is not known to work.
    """
    async with migrated_engine.begin() as conn:
        await conn.execute(sqlalchemy.text(
            "INSERT INTO jobs (id, status) VALUES ('ck-ok', 'queued')"
        ))
    with pytest.raises(sqlalchemy.exc.IntegrityError, match="ck_jobs_status"):
        async with migrated_engine.begin() as conn:
            await conn.execute(sqlalchemy.text(
                "INSERT INTO jobs (id, status) VALUES ('ck-bad', 'banana')"
            ))


async def test_alembic_version_is_stamped(migrated_engine: AsyncEngine) -> None:
    async with migrated_engine.connect() as conn:
        result = await conn.execute(sqlalchemy.text(
            "SELECT version_num FROM alembic_version"
        ))
        assert result.scalar_one() == "0002"


async def test_stage_run_status_check_bites(migrated_engine: AsyncEngine) -> None:
    """The CHECK is the only thing enforcing the status domain -- prove it bites.

    Mirrors test_status_check_constraint_rejects_unknown_values for `jobs`.
    """
    async with migrated_engine.begin() as conn:
        await conn.execute(sqlalchemy.text(
            "INSERT INTO document (id) VALUES ('d-ck')"
        ))
        await conn.execute(sqlalchemy.text(
            "INSERT INTO stage_run (id, doc_id, stage_name, stage_version, input_hash, status) "
            "VALUES ('sr-ok', 'd-ck', 'ping', 1, 'ih', 'queued')"
        ))
    with pytest.raises(sqlalchemy.exc.IntegrityError, match="ck_stage_run_status"):
        async with migrated_engine.begin() as conn:
            await conn.execute(sqlalchemy.text(
                "INSERT INTO document (id) VALUES ('d-ck2')"
            ))
            await conn.execute(sqlalchemy.text(
                "INSERT INTO stage_run (id, doc_id, stage_name, stage_version, input_hash, status) "
                "VALUES ('sr-bad', 'd-ck2', 'ping', 1, 'ih', 'banana')"
            ))
