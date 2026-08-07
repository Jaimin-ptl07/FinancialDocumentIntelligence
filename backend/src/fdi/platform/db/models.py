"""ORM models. Timestamps are server-side: the DB clock is the single source of
truth, and this keeps wall-clock out of artifact bodies (Global Constraint 6)."""

import enum
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
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


class OcrUsageEvent(Base):
    """One real (cache-miss) Azure Document Intelligence `analyze()` call.
    Always doc- and stage_run-scoped: OCR only ever runs inside `run_stage`
    for one document. Costing feature (docs/superpowers/specs/2026-07-28-
    costing-feature-design.md); INR cost is computed at read time from
    `page_count`, never stored here."""

    __tablename__ = "ocr_usage_event"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    doc_id: Mapped[str] = mapped_column(ForeignKey("document.id"), index=True, nullable=False)
    stage_run_id: Mapped[str] = mapped_column(
        ForeignKey("stage_run.id"), index=True, nullable=False
    )
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    page_count: Mapped[int] = mapped_column(nullable=False)
    azure_model: Mapped[str] = mapped_column(String(64), nullable=False)
    duration_ms: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LlmUsageEvent(Base):
    """One real Azure OpenAI chat.completions.create() call, across all
    four call sites (semantic classification, FOG concept mapping, schema-
    driven extraction, the /ask agent loop). `doc_id`/`stage_run_id` are
    nullable: `/ask` is corpus-wide (doc_id NULL), and both `/ask` and
    `/extract/document` run OUTSIDE run_stage entirely (stage_run_id NULL)
    -- only the `semantic` and `fog_mapping` stages' calls carry a real
    stage_run_id. Costing feature (docs/superpowers/specs/2026-07-28-
    costing-feature-design.md); INR cost is computed at read time from
    input_tokens/output_tokens, never stored here.

    `fog_mapping` was added to the source set in migration 0009. Its
    absence was not a cosmetic gap: the constraint is enforced on INSERT,
    so every mapping call raised CheckViolationError while recording the
    usage it had ALREADY spent money on, and the stage died mid-run. A
    billing ledger that rejects a real charge is worse than one that
    misses it -- it takes the work down with it."""

    __tablename__ = "llm_usage_event"
    __table_args__ = (
        CheckConstraint(
            "source IN ('semantic_classify','fog_mapping','extract_document',"
            "'ask_agent')",
            name="ck_llm_usage_event_source",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    doc_id: Mapped[str | None] = mapped_column(
        ForeignKey("document.id"), index=True, nullable=True
    )
    stage_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("stage_run.id"), index=True, nullable=True
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    input_tokens: Mapped[int] = mapped_column(nullable=False)
    output_tokens: Mapped[int] = mapped_column(nullable=False)
    duration_ms: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
