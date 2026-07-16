"""ORM models. Timestamps are server-side: the DB clock is the single source of
truth, and this keeps wall-clock out of artifact bodies (Global Constraint 6)."""

import enum
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class JobStatus(enum.StrEnum):
    """Job lifecycle states.

    `StrEnum`, not `(str, enum.Enum)` -- ruff UP042 rejects the latter, and it is
    right to. Both give `JobStatus.QUEUED == "queued"`, but on 3.11+ a str-mixin
    enum's `__format__` returns the *repr-ish* name: measured here on 3.13.7,
    `f"{JobStatus.QUEUED}"` -> 'JobStatus.QUEUED', and so does `str()` and `%s`.
    Equality, `in`, and `"".join()` all still yield 'queued', so the mistake
    survives every obvious smoke test and surfaces only where a status reaches a
    log line or an f-string'd query -- the two places we would read it to debug
    something else. StrEnum renders 'queued' on every path.
    """

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # String, not Enum(JobStatus): a native PG enum needs an ALTER TYPE to add a
    # state, and `Enum(JobStatus)` also persists the NAMES ('QUEUED'), not the
    # values this enum is defined by. The cost is that the column itself admits
    # any 16 chars -- JobStatus is the domain's *definition*, not its guard.
    # `Enum(..., native_enum=False)` is not the fix either: create_constraint
    # defaults to False in SQLAlchemy 2.x, so it would emit a bare VARCHAR and
    # merely look enforced. The guard is a CHECK constraint in the migration.
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    artifact_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # `onupdate` is SQLAlchemy-level, not a DB trigger: it adds `updated_at=now()`
    # to the SET clause of UPDATEs that SQLAlchemy itself builds. Measured: it
    # fires for both ORM flushes and `update(Job)` statements, and is bypassed by
    # raw `text()` SQL, by `ON CONFLICT DO UPDATE`, and by any writer that is not
    # this process. It is also skipped when a flush finds no net attribute change
    # -- no UPDATE is emitted at all. So this column means "last write *through
    # the ORM*", which is only the same thing as "last write" for as long as the
    # repository stays the sole writer.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class StageStatus(enum.StrEnum):
    """Stage lifecycle states. See `JobStatus` for the `StrEnum` rationale."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Document(Base):
    __tablename__ = "document"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StageRun(Base):
    __tablename__ = "stage_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','done','failed')", name="ck_stage_run_status"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    doc_id: Mapped[str] = mapped_column(ForeignKey("document.id"), index=True, nullable=False)
    stage_name: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    stage_version: Mapped[int] = mapped_column(nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
