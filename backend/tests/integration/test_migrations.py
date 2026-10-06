"""Proves the migration applies to a real PostgreSQL 18.4 and matches the ORM."""

import pytest
import sqlalchemy
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


async def test_alembic_version_is_stamped(migrated_engine: AsyncEngine) -> None:
    """The database is stamped at the migration directory's OWN head.

    Derived, not hardcoded. A literal here has to be edited by every commit
    that adds a migration, and it fails in a way that says nothing about the
    new migration -- it just reports two revision numbers and leaves you to
    work out which is the database and which is the assertion. Reading the head
    from the script directory asserts the thing actually worth asserting:
    `alembic upgrade head` really did reach head."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    assert head is not None
    async with migrated_engine.connect() as conn:
        result = await conn.execute(sqlalchemy.text(
            "SELECT version_num FROM alembic_version"
        ))
        assert result.scalar_one() == head


async def test_usage_event_tables_exist(migrated_engine: AsyncEngine) -> None:
    """Migration 0008 (costing feature) adds ocr_usage_event/llm_usage_event."""
    async with migrated_engine.connect() as conn:
        for table in ("ocr_usage_event", "llm_usage_event"):
            result = await conn.execute(
                sqlalchemy.text("SELECT to_regclass(:t)"), {"t": table}
            )
            assert result.scalar_one() is not None, f"{table} missing"


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


async def test_identity_tables_exist(migrated_engine: AsyncEngine) -> None:
    """Migration 0010 (T3, ADR-0002) adds the gate's two tables: app_user
    (Invitee accounts) and refresh_token (the server-side half of the
    rotating cookie). The unique email bites -- a second row for the same
    address would be a duplicate Invitee, not a second identity."""
    async with migrated_engine.begin() as conn:
        await conn.execute(sqlalchemy.text(
            "INSERT INTO app_user (id, email, password_hash) "
            "VALUES ('u-t3-1', 'first@example.test', '$argon2id$stub')"
        ))
    with pytest.raises(sqlalchemy.exc.IntegrityError, match="uq_app_user_email"):
        async with migrated_engine.begin() as conn:
            await conn.execute(sqlalchemy.text(
                "INSERT INTO app_user (id, email, password_hash) "
                "VALUES ('u-t3-2', 'first@example.test', '$argon2id$stub')"
            ))
