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
        assert result.scalar_one() == "0007"


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


async def test_document_checksum_is_unique(migrated_engine: AsyncEngine) -> None:
    async with migrated_engine.begin() as conn:
        await conn.execute(sqlalchemy.text(
            "INSERT INTO document (id, checksum, version) VALUES ('d-u1','abc123',1)"))
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        async with migrated_engine.begin() as conn:
            await conn.execute(sqlalchemy.text(
                "INSERT INTO document (id, checksum, version) VALUES ('d-u2','abc123',1)"))


async def test_two_null_checksums_are_allowed(migrated_engine: AsyncEngine) -> None:
    # ping's synthetic checksum-less rows must remain legal under UNIQUE(checksum)
    async with migrated_engine.begin() as conn:
        await conn.execute(sqlalchemy.text("INSERT INTO document (id) VALUES ('d-n1')"))
        await conn.execute(sqlalchemy.text("INSERT INTO document (id) VALUES ('d-n2')"))  # no error


async def test_chunk_table_is_gone(migrated_engine: AsyncEngine) -> None:
    """RT-9: keyword search moved to Milvus-native BM25; migration 0007 drops
    the Postgres `chunk` table (and its tsv generated column + four
    indexes) entirely. `to_regclass` returns NULL for a relation that does
    not exist, rather than raising -- the assertion this replaces
    (`test_chunk_tsv_is_generated_and_searchable`/`_index_is_gin`/
    `_doc_id_fk_enforced`, all of which exercised a table that no longer
    exists) is that the table is well and truly absent."""
    async with migrated_engine.connect() as conn:
        result = await conn.execute(sqlalchemy.text("SELECT to_regclass('chunk')"))
        assert result.scalar_one() is None
