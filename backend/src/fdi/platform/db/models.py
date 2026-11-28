"""ORM models. Timestamps are server-side: the DB clock is the single source of
truth, and this keeps wall-clock out of artifact bodies (Global Constraint 6)."""

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
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
    __table_args__ = (
        UniqueConstraint("checksum", name="uq_document_checksum"),
        CheckConstraint(
            "link_status IN ('linked','awaiting_confirmation','unidentified')",
            name="ck_document_link_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    byte_size: Mapped[int | None] = mapped_column(nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    doc_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    version: Mapped[int | None] = mapped_column(nullable=True)
    # The Company this Report is filed under (ADR-0003); None until the
    # issuer stage + company registry have identified it.
    company_id: Mapped[str | None] = mapped_column(
        ForeignKey("company.id"), nullable=True, index=True
    )
    # Report metadata (S2.2, migration 0015), written by the company registry
    # from the issuer artifact. Statement bases are NOT here: they are read
    # from the basis segmentation ("bases present").
    fiscal_year: Mapped[str | None] = mapped_column(String(16), nullable=True)
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    report_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    link_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    link_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # None while `fiscal_year` is the issuer stage's reading;
    # "confirmed_by_invitee" once an Invitee corrected it (S2.3, migration
    # 0018) -- re-resolving the Report then leaves it alone.
    fiscal_year_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Company(Base):
    """A Company, identified by its CIN (ADR-0003). One CIN, one Company."""

    __tablename__ = "company"
    __table_args__ = (UniqueConstraint("cin", name="uq_company_cin"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    cin: Mapped[str] = mapped_column(String(21), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ReportLinkChange(Base):
    """One Invitee's confirm or correction of a Report's Company or Fiscal
    year -- who and when (S2.3). Append only; goes with its Report."""

    __tablename__ = "report_link_change"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    doc_id: Mapped[str] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), nullable=False, index=True
    )
    invitee_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    # clock_timestamp(): two changes in one transaction keep their order.
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )


class CompanyIdentifier(Base):
    """A hand-entered name or code a Company is known by (S2.4): an NSE
    symbol, BSE code, ISIN or alias. `key` is what makes two identifiers of a
    kind the same (`fdi.core.company_names.identifier_key`), so "RIL" and
    "r.i.l" cannot both be aliases; it is unique per kind across all
    Companies -- one ticker, one Company."""

    __tablename__ = "company_identifier"
    __table_args__ = (
        UniqueConstraint("kind", "key", name="uq_company_identifier_kind_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )


class CompanyChange(Base):
    """One manual change to a Company -- who made it and when (S2.4). Append
    only: a removed identifier's row is gone, its removal stays here -- and a
    Company with a history cannot be deleted out from under it (RESTRICT)."""

    __tablename__ = "company_change"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("company.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    invitee_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    # clock_timestamp(), not now(): two changes in one transaction still
    # get distinct, ordered times.
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
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
    misses it -- it takes the work down with it.

    `eval_judge` (migration 0013) is the answer-quality eval's optional LLM
    judge: eval spend, recorded under its own source so it never reads as
    product spend."""

    __tablename__ = "llm_usage_event"
    __table_args__ = (
        CheckConstraint(
            "source IN ('semantic_classify','fog_mapping','extract_document',"
            "'ask_agent','eval_judge')",
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
    # The API job that spent it (migration 0012), NULL outside one. Not
    # correlation_id: that is caller-supplied, so it cannot prove whose
    # job a row belongs to.
    api_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("api_job.id"), index=True, nullable=True
    )
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    input_tokens: Mapped[int] = mapped_column(nullable=False)
    output_tokens: Mapped[int] = mapped_column(nullable=False)
    duration_ms: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AppUser(Base):
    """An Invitee's account (ADR-0002). `app_user`, not `user`: USER is a
    reserved word in PostgreSQL, and quoting it forever to keep a prettier
    name buys nothing. There is no self-signup and no role column yet --
    accounts exist only because an admin CLI created them, and roles are the
    product path ADR-0002 deliberately defers.

    `password_hash` is an argon2id string (fdi.platform.security); it is
    never the password, and no view ever serialises this row."""

    __tablename__ = "app_user"
    __table_args__ = (UniqueConstraint("email", name="uq_app_user_email"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # RFC 5321's forward-mailbox ceiling (64 local + @ + 255 domain), not
    # the folklore 254, so no legal address is rejected by the column.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(nullable=False, server_default=true())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ApiJob(Base):
    """One polled request to an expensive route (T5): `/ask`, document
    extraction, factsheet generation or an eval run.

    `params` is the validated request the route accepted; `result` is the
    body the route used to return inline -- JSON, or, for an XBRL factsheet,
    the XML document as a JSON string. Status is its own vocabulary, not
    `StageStatus`: a job SUCCEEDS (the caller's request was answered), a
    stage is DONE (an artifact exists); conflating them would make the jobs
    endpoint say `done` for ping runs and `succeeded` for these."""

    __tablename__ = "api_job"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('ask','extract','factsheet','eval','extraction')", name="ck_api_job_kind"
        ),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed')", name="ck_api_job_status"
        ),
        Index("ix_api_job_user_created", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    params: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    result: Mapped[object | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class RefreshToken(Base):
    """One live refresh-token session row (ADR-0002).

    The cookie carries the raw token; this row stores only its SHA-256.
    Rotation is `revoked_at IS NULL -> revoked_at = now()` plus a fresh row,
    so replaying a rotated-or-logged-out token finds `revoked_at` set and
    fails. Rows are per-LOGIN, not per-user: the same Invitee may hold
    several concurrent sessions (laptop + phone), each dying independently."""

    __tablename__ = "refresh_token"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("app_user.id"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Workspace(Base):
    """An Invitee's working set of Companies (S2.5): a focus list, never an
    access boundary (#16). Owned by one Invitee and visible only to them;
    nothing reads it to decide who may see a Company or Report."""

    __tablename__ = "workspace"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkspaceCompany(Base):
    """One Company in one Workspace. Both foreign keys cascade: deleting the
    Workspace or the Company removes the membership, never the other side."""

    __tablename__ = "workspace_company"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspace.id", ondelete="CASCADE"), primary_key=True
    )
    company_id: Mapped[str] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# --- Extraction schemas, reads and runs (S3, ADR-0004, migration 0019) -------


class ExtractionSchema(Base):
    """A named list of Fields. `owner_user_id` NULL means FDI owns it (the
    standard schema, identified by `standard_key`); only the owner edits,
    every Invitee reads, copies and runs (#16)."""

    __tablename__ = "extraction_schema"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("app_user.id"), index=True, nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    standard_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ExtractionSchemaVersion(Base):
    """A published, immutable version. Its Fields are `ExtractionField` rows."""

    __tablename__ = "extraction_schema_version"
    __table_args__ = (
        UniqueConstraint("schema_id", "version", name="uq_extraction_schema_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schema_id: Mapped[str] = mapped_column(
        ForeignKey("extraction_schema.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    review_threshold: Mapped[float] = mapped_column(Float, nullable=False)
    groups: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    field_count: Mapped[int] = mapped_column(Integer, nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    source_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_by: Mapped[str | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ExtractionField(Base):
    """One Field of one published version. `field_id` is the identity the
    Field keeps across versions; `definition` is
    `fdi.core.extraction_schema.field_to_mapping`."""

    __tablename__ = "extraction_field"
    __table_args__ = (
        UniqueConstraint("version_id", "field_id", name="uq_extraction_field_identity"),
        UniqueConstraint("version_id", "position", name="uq_extraction_field_position"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version_id: Mapped[str] = mapped_column(
        ForeignKey("extraction_schema_version.id", ondelete="CASCADE"), nullable=False
    )
    field_id: Mapped[str] = mapped_column(String(36), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    tag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    definition: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)


class ExtractionSchemaDraft(Base):
    """A schema's one mutable draft. `revision` increments on every write; a
    write naming a stale revision is refused, so two tabs never clobber."""

    __tablename__ = "extraction_schema_draft"

    schema_id: Mapped[str] = mapped_column(
        ForeignKey("extraction_schema.id", ondelete="CASCADE"), primary_key=True
    )
    base_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    fields: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    groups: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    review_threshold: Mapped[float] = mapped_column(Float, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ExtractionRun(Base):
    __tablename__ = "extraction_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','succeeded','partial','failed')",
            name="ck_extraction_run_status",
        ),
        Index("ix_extraction_run_user_created", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schema_version_id: Mapped[str] = mapped_column(
        ForeignKey("extraction_schema_version.id"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    api_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("api_job.id", ondelete="SET NULL"), nullable=True
    )
    retry_of: Mapped[str | None] = mapped_column(ForeignKey("extraction_run.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    field_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    forced_field_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    counts: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExtractionRunReport(Base):
    __tablename__ = "extraction_run_report"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("extraction_run.id", ondelete="CASCADE"), primary_key=True
    )
    doc_id: Mapped[str] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    counts: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class FieldRead(Base):
    """One immutable read of one Field from one Report version (ADR-0004).
    Never updated, except to be marked superseded by a forced re-read."""

    __tablename__ = "field_read"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    doc_id: Mapped[str] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), nullable=False
    )
    field_id: Mapped[str] = mapped_column(String(36), nullable=False)
    cache_key: Mapped[str] = mapped_column(String(64), nullable=False)
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    retrieval_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[object | None] = mapped_column(JSONB, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    basis: Mapped[str | None] = mapped_column(String(16), nullable=True)
    provenance: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    answered: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("extraction_run.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ValueReview(Base):
    """An Invitee's accept or correct of one read: beside it, never over it."""

    __tablename__ = "value_review"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    read_id: Mapped[str] = mapped_column(
        ForeignKey("field_read.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[object | None] = mapped_column(JSONB, nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ExtractionRunItem(Base):
    """What one run did for one Field x Report, and why (the plan's reason)."""

    __tablename__ = "extraction_run_item"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("extraction_run.id", ondelete="CASCADE"), primary_key=True
    )
    doc_id: Mapped[str] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), primary_key=True
    )
    field_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    read_id: Mapped[str | None] = mapped_column(
        ForeignKey("field_read.id", ondelete="SET NULL"), nullable=True
    )


# -- Financial facts (S5, ADR-0006): a rebuildable projection -----------------


class ConceptRow(Base):
    """A taxonomy Concept, seeded from the FDI standard schema. Never created
    by a model."""

    __tablename__ = "concept"

    tag: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    period_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FinancialProjection(Base):
    """What one Report version's projection was made from, and what it saw."""

    __tablename__ = "financial_projection"

    doc_id: Mapped[str] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), primary_key=True
    )
    company_id: Mapped[str | None] = mapped_column(ForeignKey("company.id"), nullable=True)
    fiscal_year: Mapped[str | None] = mapped_column(String(16), nullable=True)
    projection_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    diagnostics: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    projected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FinancialObservationRow(Base):
    """One value one source states for Concept x Fiscal year x basis."""

    __tablename__ = "financial_observation"
    __table_args__ = (
        UniqueConstraint(
            "doc_id", "basis", "concept", "fiscal_year", "report_column", "source",
            name="uq_financial_observation_identity",
        ),
        Index("ix_financial_observation_fact", "company_id", "basis", "concept", "fiscal_year"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    doc_id: Mapped[str] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[str] = mapped_column(ForeignKey("company.id"), nullable=False)
    basis: Mapped[str] = mapped_column(String(16), nullable=False)
    concept: Mapped[str] = mapped_column(ForeignKey("concept.tag"), nullable=False)
    fiscal_year: Mapped[str] = mapped_column(String(16), nullable=False)
    report_column: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    doc_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    report_fiscal_year: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    printed: Mapped[str | None] = mapped_column(Text, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    scale: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    unit_printed: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    normalised: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    period_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    label: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    line_item: Mapped[str | None] = mapped_column(String(512), nullable=True)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    table_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_id: Mapped[str | None] = mapped_column(String(640), nullable=True)
    validation: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    inputs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    extracted_value_id: Mapped[str | None] = mapped_column(
        ForeignKey("field_read.id", ondelete="CASCADE"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FinancialFactRow(Base):
    """The value the fact rule picks for Company x Concept x Fiscal year x
    basis. Written only by re-deriving it from Observations."""

    __tablename__ = "financial_fact"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "concept", "fiscal_year", "basis", name="uq_financial_fact_key"
        ),
        Index("ix_financial_fact_history", "company_id", "basis", "concept", "fiscal_year"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("company.id"), nullable=False)
    concept: Mapped[str] = mapped_column(ForeignKey("concept.tag"), nullable=False)
    fiscal_year: Mapped[str] = mapped_column(String(16), nullable=False)
    basis: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    selected_observation_id: Mapped[str] = mapped_column(
        ForeignKey("financial_observation.id", ondelete="CASCADE"), nullable=False
    )
    value: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    normalised: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    restated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    restatement_original_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    disagreement_original_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    derived_observation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    observation_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FinancialValidationRow(Base):
    """One of a Report version's graph checks, on one basis."""

    __tablename__ = "financial_validation"
    __table_args__ = (
        UniqueConstraint("doc_id", "basis", "rule_id", name="uq_financial_validation_rule"),
        Index("ix_financial_validation_tags", "tags", postgresql_using="gin"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    doc_id: Mapped[str] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), nullable=False
    )
    basis: Mapped[str] = mapped_column(String(16), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    expected: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    actual: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    delta: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    tolerance: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
