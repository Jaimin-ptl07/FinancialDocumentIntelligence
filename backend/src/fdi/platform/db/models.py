"""ORM models. Timestamps are server-side: the DB clock is the single source of
truth, and this keeps wall-clock out of artifact bodies (Global Constraint 6)."""

import enum
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class StageStatus(enum.StrEnum):
    """Stage lifecycle states.

    `StrEnum`, not `(str, enum.Enum)` -- ruff UP042 rejects the latter, and it is
    right to. Both give `StageStatus.QUEUED == "queued"`, but on 3.11+ a
    str-mixin enum's `__format__` returns the *repr-ish* name: measured here on
    3.13.7, `f"{StageStatus.QUEUED}"` -> 'StageStatus.QUEUED', and so does
    `str()` and `%s`. Equality, `in`, and `"".join()` all still yield 'queued',
    so the mistake survives every obvious smoke test and surfaces only where a
    status reaches a log line or an f-string'd query -- the two places we would
    read it to debug something else. StrEnum renders 'queued' on every path.
    """

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Document(Base):
    __tablename__ = "document"
    __table_args__ = (UniqueConstraint("checksum", name="uq_document_checksum"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    byte_size: Mapped[int | None] = mapped_column(nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    doc_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    version: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Chunk(Base):
    """RT-3: Postgres full-text keyword index. One row per RT-1 `chunks`
    artifact record, upserted by `fdi.pipeline.retrieval.index_chunks_fts`.

    `id` reuses the unified chunk identity `f"{doc_id}:{chunk_id}"` (the same
    id Milvus already uses for vectors) as PK and upsert conflict target --
    reprocessing a document is a plain idempotent upsert. `doc_key`/`version`
    are denormalized from `Document` at write time (the pipeline layer HAS a
    session, unlike a Stage) so a keyword-filtered query needs no join. `tsv`
    is a Postgres STORED generated column over `text` ALONE (RT-1's `text`
    already begins with `heading + "\n"`, so re-adding heading would just
    double-weight the same lexemes) -- Postgres maintains it on every
    insert/update; nothing in this codebase ever computes or targets it
    directly (a generated column cannot appear in an INSERT/UPDATE column
    list). `ix_chunk_tsv` (GIN) is what makes `@@` queries fast.
    """

    __tablename__ = "chunk"
    __table_args__ = (
        Index("ix_chunk_doc_key_version", "doc_key", "version"),
        Index("ix_chunk_section_type", "section_type"),
        Index("ix_chunk_tsv", "tsv", postgresql_using="gin"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    doc_id: Mapped[str] = mapped_column(ForeignKey("document.id"), index=True, nullable=False)
    chunk_id: Mapped[str] = mapped_column(String(64), nullable=False)
    doc_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    version: Mapped[int | None] = mapped_column(nullable=True)
    heading: Mapped[str | None] = mapped_column(Text, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    section_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    page: Mapped[int | None] = mapped_column(nullable=True)
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', text)", persisted=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
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


class EvalRun(Base):
    __tablename__ = "eval_run"
    __table_args__ = (
        Index("ix_eval_run_dataset_metric", "dataset_name", "metric_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    candidate_name: Mapped[str] = mapped_column(String(128), nullable=False)
    candidate_version: Mapped[int] = mapped_column(nullable=False)
    dataset_name: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset_version: Mapped[int] = mapped_column(nullable=False)
    metric_name: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate: Mapped[float] = mapped_column(Float(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EvalCaseResult(Base):
    __tablename__ = "eval_case_result"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    eval_run_id: Mapped[str] = mapped_column(
        ForeignKey("eval_run.id"), index=True, nullable=False
    )
    case_id: Mapped[str] = mapped_column(String(128), nullable=False)
    score: Mapped[float] = mapped_column(Float(), nullable=False)
