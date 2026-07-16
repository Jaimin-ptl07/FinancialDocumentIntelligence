"""Proves the migration applies to a real PostgreSQL 18.4 and matches the ORM."""

import pytest
import sqlalchemy
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


async def test_alembic_version_is_stamped(migrated_engine: AsyncEngine) -> None:
    async with migrated_engine.connect() as conn:
        result = await conn.execute(sqlalchemy.text(
            "SELECT version_num FROM alembic_version"
        ))
        assert result.scalar_one() == "0004"


async def test_stage_run_status_check_bites(migrated_engine: AsyncEngine) -> None:
    """The CHECK is the only thing enforcing the status domain -- prove it bites."""
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


async def test_eval_run_and_case_result_exist(migrated_engine: AsyncEngine) -> None:
    async with migrated_engine.begin() as conn:
        await conn.execute(sqlalchemy.text(
            "INSERT INTO eval_run (id, candidate_name, candidate_version, dataset_name, "
            "dataset_version, metric_name, aggregate) VALUES "
            "('er1','upper',1,'toy',1,'exact_match',0.5)"))
        await conn.execute(sqlalchemy.text(
            "INSERT INTO eval_case_result (id, eval_run_id, case_id, score) "
            "VALUES ('cr1','er1','c1',1.0)"))
    # a case result whose run does not exist must violate the FK
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        async with migrated_engine.begin() as conn:
            await conn.execute(sqlalchemy.text(
                "INSERT INTO eval_case_result (id, eval_run_id, case_id, score) "
                "VALUES ('cr2','NO_SUCH_RUN','c1',1.0)"))
