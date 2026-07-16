"""JobRepository against a real PostgreSQL 18.4.

These run against the live CHECK constraint from migration 0001, so any status
this class writes that is outside ('queued','running','done','failed') fails
here as an IntegrityError rather than in SP-2.
"""

import uuid
from collections.abc import Awaitable, Callable

import pytest
import sqlalchemy
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.facts import RunFacts
from fdi.platform.db.engine import make_session_factory
from fdi.platform.db.models import Job, JobStatus
from fdi.platform.db.repository import JobRepository, RegistryRepository

pytestmark = pytest.mark.integration

Mutator = Callable[[JobRepository, str], Awaitable[None]]


@pytest.fixture
def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(migrated_engine)


async def test_create_then_get(session_factory: async_sessionmaker[AsyncSession]) -> None:
    job_id = str(uuid.uuid4())
    async with session_factory() as s:
        await JobRepository(s).create(job_id, correlation_id="cid-1")
        await s.commit()

    async with session_factory() as s:
        job = await JobRepository(s).get(job_id)
    assert job is not None
    assert job.status == JobStatus.QUEUED.value
    assert job.correlation_id == "cid-1"
    assert job.artifact_key is None


async def test_get_missing_returns_none(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as s:
        assert await JobRepository(s).get(str(uuid.uuid4())) is None


async def test_mark_done_sets_artifact_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    job_id = str(uuid.uuid4())
    async with session_factory() as s:
        repo = JobRepository(s)
        await repo.create(job_id, correlation_id="cid-2")
        await s.commit()
    async with session_factory() as s:
        await JobRepository(s).mark_done(job_id, artifact_key="k/ping.json")
        await s.commit()
    async with session_factory() as s:
        job = await JobRepository(s).get(job_id)
    assert job is not None
    assert job.status == JobStatus.DONE.value
    assert job.artifact_key == "k/ping.json"


async def test_mark_failed_records_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    job_id = str(uuid.uuid4())
    async with session_factory() as s:
        await JobRepository(s).create(job_id, correlation_id="cid-3")
        await s.commit()
    async with session_factory() as s:
        await JobRepository(s).mark_failed(job_id, error="boom")
        await s.commit()
    async with session_factory() as s:
        job = await JobRepository(s).get(job_id)
    assert job is not None
    assert job.status == JobStatus.FAILED.value
    assert job.error == "boom"


async def test_mark_done_on_missing_job_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        with pytest.raises(KeyError):
            await JobRepository(s).mark_done(str(uuid.uuid4()), artifact_key="x")


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda r, j: r.mark_running(j), id="mark_running"),
        pytest.param(lambda r, j: r.mark_done(j, artifact_key="k/x.json"), id="mark_done"),
        pytest.param(lambda r, j: r.mark_failed(j, error="boom"), id="mark_failed"),
    ],
)
async def test_updated_at_advances_and_created_at_does_not(
    session_factory: async_sessionmaker[AsyncSession], mutate: Mutator
) -> None:
    """`updated_at` must actually move when this class writes. Nothing else watches it.

    The module docstring on `models.py` spends a paragraph establishing that
    `updated_at` is trustworthy only while this repository is the sole writer, and
    that `onupdate` is bypassed by `ON CONFLICT DO UPDATE`, by raw `text()`, and by
    a flush with no net change. That is a documented invariant with, until this
    test, nothing enforcing it: every other test here reads `status`/`artifact_key`
    and would pass just as happily with `onupdate` deleted from the model.

    Verified to bite: deleting `onupdate=func.now()` from `Job.updated_at` turns
    all three params RED with `updated_at` frozen at its INSERT value.

    THE TWO SESSIONS BELOW ARE LOAD-BEARING -- do not collapse them into one.
    Measured against this suite's PostgreSQL 18.4 container: `now()` is
    TRANSACTION-start time. Two SELECTs issued 100ms apart inside a single
    transaction returned a byte-identical `now()` (2026-07-16 02:28:42.925841+00
    both times) while `clock_timestamp()` advanced normally. `onupdate=func.now()`
    renders `now()` into the UPDATE's SET clause, so a create and a mutation
    sharing one transaction stamp `created_at` and `updated_at` from the SAME clock
    reading -- `updated_at > created_at` would then be false for a reason that has
    nothing to do with this repository being correct.

    Equally: no `sleep` is needed and none should be added. Two back-to-back
    transactions were measured ~5.3ms apart with no artificial delay, against
    PostgreSQL's microsecond timestamp resolution.
    """
    job_id = str(uuid.uuid4())

    # Transaction 1 -- INSERT. Both timestamps come from this transaction's now().
    async with session_factory() as s:
        await JobRepository(s).create(job_id, correlation_id="cid-ts")
        await s.commit()

    async with session_factory() as s:
        created = await JobRepository(s).get(job_id)
    assert created is not None
    assert created.created_at == created.updated_at, (
        "one INSERT, one transaction, one now() -- these must start out identical"
    )
    created_at, first_updated_at = created.created_at, created.updated_at

    # Transaction 2 -- UPDATE. A DIFFERENT transaction, hence a later now().
    async with session_factory() as s:
        await mutate(JobRepository(s), job_id)
        await s.commit()

    async with session_factory() as s:
        after = await JobRepository(s).get(job_id)
    assert after is not None
    assert after.updated_at > first_updated_at, (
        "updated_at did not advance -- onupdate did not reach the UPDATE's SET clause"
    )
    assert after.created_at == created_at, "created_at must never move after INSERT"


async def test_every_status_this_class_writes_satisfies_the_check_constraint(
    session_factory: async_sessionmaker[AsyncSession], migrated_engine: AsyncEngine
) -> None:
    """Drive all four statuses past the live CHECK, reading raw SQL back.

    `Job.status` is String(16), so `ck_jobs_status` is the only thing enforcing the
    domain -- and a constraint only guards the write paths something actually
    exercises. The plan's five tests never call `mark_running`, so 'running' had
    never once reached this table from this class.

    Read back via `text()` rather than `job.status` so the assertion is about the
    bytes in the column, not about a value ORM-round-tripped through the same
    JobStatus that produced it. Relevant because JobStatus is a StrEnum: measured,
    both `JobStatus.DONE` and `JobStatus.DONE.value` encode to 'done', so `.value`
    in the implementation is explicitness rather than load-bearing -- and this test
    is what would catch it if that ever stopped being true.
    """
    async def raw_status(job_id: str) -> str:
        async with migrated_engine.connect() as conn:
            result = await conn.execute(
                sqlalchemy.text("SELECT status FROM jobs WHERE id = :i"), {"i": job_id}
            )
            return str(result.scalar_one())

    happy = str(uuid.uuid4())
    async with session_factory() as s:
        await JobRepository(s).create(happy, correlation_id="cid-ck")
        await s.commit()
    assert await raw_status(happy) == "queued"

    async with session_factory() as s:
        await JobRepository(s).mark_running(happy)
        await s.commit()
    assert await raw_status(happy) == "running"

    async with session_factory() as s:
        await JobRepository(s).mark_done(happy, artifact_key="k/ok.json")
        await s.commit()
    assert await raw_status(happy) == "done"

    sad = str(uuid.uuid4())
    async with session_factory() as s:
        await JobRepository(s).create(sad, correlation_id="cid-ck2")
        await s.commit()
    async with session_factory() as s:
        await JobRepository(s).mark_failed(sad, error="boom")
        await s.commit()
    assert await raw_status(sad) == "failed"


@pytest.mark.parametrize(
    ("filler", "octets_per_char"),
    [
        pytest.param("x", 1, id="ascii"),
        pytest.param("\U0001f600", 4, id="4-byte-emoji"),
    ],
)
async def test_overlong_error_is_truncated_to_column_width_not_rejected(
    session_factory: async_sessionmaker[AsyncSession],
    migrated_engine: AsyncEngine,
    filler: str,
    octets_per_char: int,
) -> None:
    """`error[:2048]` must slice in the units PostgreSQL counts, and must not raise.

    Measured against this container: `varchar(2048)` counts CHARACTERS, not bytes
    -- 2048 emoji (8192 octets) INSERT fine and store as char_length=2048,
    octet_length=8192; 2049 raise StringDataRightTruncationError. Python slicing is
    codepoints too, so `error[:2048]` and the column agree exactly. The emoji param
    is what pins that: a "fix" to byte-wise truncation (`error.encode()[:2048]`)
    would still pass the ascii param while silently cutting multi-byte diagnostics
    to a quarter length.

    That this test reaches its assertions at all is the other half -- without the
    slice PostgreSQL RAISES (StringDataRightTruncationError, surfaced as
    sqlalchemy DBAPIError) and aborts the transaction, so an over-long error would
    mean the job never gets marked failed at all. The slice buys a recorded failure
    at the cost of a silently shortened diagnostic.

    The width is read off the model rather than hardcoded, because the `2048` in
    `repository.mark_failed` and the `String(2048)` in `models.Job` are two copies
    of one number with nothing linking them. Hardcoding 2048 here would let someone
    widen the column, leave the slice at 2048, and still see green -- the widening
    would silently do nothing.
    """
    column_width = Job.__table__.c.error.type.length
    assert column_width is not None, "Job.error must be a bounded String for this to mean anything"

    job_id = str(uuid.uuid4())
    async with session_factory() as s:
        await JobRepository(s).create(job_id, correlation_id="cid-long")
        await s.commit()

    async with session_factory() as s:
        await JobRepository(s).mark_failed(job_id, error=filler * (column_width + 100))
        await s.commit()

    async with migrated_engine.connect() as conn:
        result = await conn.execute(
            sqlalchemy.text(
                "SELECT char_length(error), octet_length(error) FROM jobs WHERE id = :i"
            ),
            {"i": job_id},
        )
        char_length, octet_length = result.one()

    assert char_length == column_width, (
        "slice width and column width have drifted apart -- mark_failed truncates "
        f"to a different length than Job.error holds ({column_width})"
    )
    assert octet_length == column_width * octets_per_char, (
        "truncation counted the wrong units -- PostgreSQL's varchar(n) counts "
        "characters, so the slice must too"
    )


async def test_run_lifecycle_and_facts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Unique doc + run ids: the migrated DB is session-scoped, so hardcoded literals
    # collide with the same ids in sibling tests on the PKs.
    async with session_factory() as s:
        repo = RegistryRepository(s)
        doc, r1 = str(uuid.uuid4()), str(uuid.uuid4())
        await repo.create_document(doc)
        await repo.start_run(r1, doc, "ping", 1, "ih", "corr-1")
        await repo.finish_run(r1, output_hash="oh", artifact_key=f"artifacts/{doc}/ping/v1/ih.json")
        await s.commit()

        facts = await repo.runs_for_document(doc)
        assert facts == {"ping": RunFacts(version=1, input_hash="ih", output_hash="oh")}

        run = await repo.latest_run(doc, "ping")
        assert run is not None and run.status == "done" and run.artifact_key.endswith("ih.json")


async def test_failed_run_is_recorded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        repo = RegistryRepository(s)
        doc, r2 = str(uuid.uuid4()), str(uuid.uuid4())
        await repo.create_document(doc)
        await repo.start_run(r2, doc, "ping", 1, "ih", None)
        await repo.fail_run(r2, "boom")
        await s.commit()

        run = await repo.latest_run(doc, "ping")
        assert run is not None
        assert run.status == "failed" and run.error == "boom"
        assert await repo.runs_for_document(doc) == {}  # only DONE runs are facts
