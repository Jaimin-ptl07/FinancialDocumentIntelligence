"""extraction schemas, per-Field reads, reviews and Extraction runs (S3)

ADR-0004. An Extraction schema is a row an Invitee owns (owner NULL = the
FDI standard schema), with immutable published versions whose Fields are rows
of their own, and at most one mutable draft. A `field_read` is one immutable
read of one Field from one Report version, keyed (live) by its cache key; a
`value_review` sits beside it and never overwrites it. An `extraction_run`
records what was asked, its per-Report progress, and per Field x Report what
it did and why (`extraction_run_item`).

Reports are document ids (a Report version). Deleting a document cascades to
its reads, their reviews and run rows, matching `DocumentRepository.delete`.

Also widens `api_job.kind` with 'extraction' (an Extraction run's queued job).

Additive: no existing table loses a column or a row.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-28
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def _ts(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True),
        server_default=None if nullable else sa.func.now(), nullable=nullable,
    )


def upgrade() -> None:
    op.create_table(
        "extraction_schema",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "owner_user_id", sa.String(36),
            sa.ForeignKey("app_user.id", name="fk_extraction_schema_owner_user_id"),
            nullable=True,
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("standard_key", sa.String(64), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("standard_key", name="uq_extraction_schema_standard_key"),
        sa.CheckConstraint(
            "(owner_user_id IS NULL) = (standard_key IS NOT NULL)",
            name="ck_extraction_schema_owner_or_standard",
        ),
    )
    op.create_index(
        "ix_extraction_schema_owner_user_id", "extraction_schema", ["owner_user_id"]
    )

    op.create_table(
        "extraction_schema_version",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "schema_id", sa.String(36),
            sa.ForeignKey(
                "extraction_schema.id", name="fk_extraction_schema_version_schema_id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("definition_hash", sa.String(64), nullable=False),
        sa.Column("review_threshold", sa.Float, nullable=False),
        sa.Column("groups", JSONB, nullable=False),
        sa.Column("field_count", sa.Integer, nullable=False),
        sa.Column("notes", sa.Text, nullable=False, server_default=""),
        sa.Column("source_version", sa.Text, nullable=True),
        sa.Column(
            "published_by", sa.String(36),
            sa.ForeignKey("app_user.id", name="fk_extraction_schema_version_published_by"),
            nullable=True,
        ),
        _ts("published_at"),
        sa.UniqueConstraint("schema_id", "version", name="uq_extraction_schema_version"),
        sa.CheckConstraint("version >= 1", name="ck_extraction_schema_version_positive"),
    )

    op.create_table(
        "extraction_field",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "version_id", sa.String(36),
            sa.ForeignKey(
                "extraction_schema_version.id", name="fk_extraction_field_version_id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column("field_id", sa.String(36), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("tag", sa.String(64), nullable=True),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("definition_hash", sa.String(64), nullable=False),
        sa.Column("definition", JSONB, nullable=False),
        sa.UniqueConstraint("version_id", "field_id", name="uq_extraction_field_identity"),
        sa.UniqueConstraint("version_id", "position", name="uq_extraction_field_position"),
        sa.CheckConstraint(
            "origin IN ('extracted','derived')", name="ck_extraction_field_origin"
        ),
    )
    op.create_index(
        "uq_extraction_field_tag", "extraction_field", ["version_id", "tag"],
        unique=True, postgresql_where=sa.text("tag IS NOT NULL"),
    )

    op.create_table(
        "extraction_schema_draft",
        sa.Column(
            "schema_id", sa.String(36),
            sa.ForeignKey(
                "extraction_schema.id", name="fk_extraction_schema_draft_schema_id",
                ondelete="CASCADE",
            ),
            primary_key=True,
        ),
        sa.Column("base_version", sa.Integer, nullable=True),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("fields", JSONB, nullable=False),
        sa.Column("groups", JSONB, nullable=False),
        sa.Column("review_threshold", sa.Float, nullable=False),
        sa.Column(
            "updated_by", sa.String(36),
            sa.ForeignKey("app_user.id", name="fk_extraction_schema_draft_updated_by"),
            nullable=True,
        ),
        _ts("updated_at"),
    )

    op.create_table(
        "extraction_run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "schema_version_id", sa.String(36),
            sa.ForeignKey(
                "extraction_schema_version.id", name="fk_extraction_run_schema_version_id"
            ),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.String(36),
            sa.ForeignKey("app_user.id", name="fk_extraction_run_user_id"), nullable=False,
        ),
        sa.Column(
            "api_job_id", sa.String(36),
            sa.ForeignKey("api_job.id", name="fk_extraction_run_api_job_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "retry_of", sa.String(36),
            sa.ForeignKey("extraction_run.id", name="fk_extraction_run_retry_of"),
            nullable=True,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("field_ids", JSONB, nullable=True),
        sa.Column("forced_field_ids", JSONB, nullable=False),
        sa.Column("counts", JSONB, nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        _ts("created_at"),
        _ts("started_at", nullable=True),
        _ts("finished_at", nullable=True),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','partial','failed')",
            name="ck_extraction_run_status",
        ),
    )
    op.create_index(
        "ix_extraction_run_user_created", "extraction_run", ["user_id", "created_at"]
    )

    op.create_table(
        "extraction_run_report",
        sa.Column(
            "run_id", sa.String(36),
            sa.ForeignKey(
                "extraction_run.id", name="fk_extraction_run_report_run_id", ondelete="CASCADE"
            ),
            primary_key=True,
        ),
        sa.Column(
            "doc_id", sa.String(36),
            sa.ForeignKey(
                "document.id", name="fk_extraction_run_report_doc_id", ondelete="CASCADE"
            ),
            primary_key=True,
        ),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("counts", JSONB, nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','partial','failed')",
            name="ck_extraction_run_report_status",
        ),
    )

    op.create_table(
        "field_read",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "doc_id", sa.String(36),
            sa.ForeignKey("document.id", name="fk_field_read_doc_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("field_id", sa.String(36), nullable=False),
        sa.Column("cache_key", sa.String(64), nullable=False),
        sa.Column("definition_hash", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("retrieval_version", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("value", JSONB, nullable=True),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column("basis", sa.String(16), nullable=True),
        sa.Column("provenance", JSONB, nullable=False),
        sa.Column("answered", sa.Boolean, nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column(
            "run_id", sa.String(36),
            sa.ForeignKey("extraction_run.id", name="fk_field_read_run_id", ondelete="SET NULL"),
            nullable=True,
        ),
        _ts("created_at"),
        _ts("superseded_at", nullable=True),
        sa.CheckConstraint(
            "status IN ('extracted','not_found','failed')", name="ck_field_read_status"
        ),
    )
    # The live cache: at most one un-superseded read per key. Concurrent runs
    # converge on it (INSERT ... ON CONFLICT DO NOTHING) instead of storing two.
    op.create_index(
        "uq_field_read_live_key", "field_read", ["doc_id", "field_id", "cache_key"],
        unique=True, postgresql_where=sa.text("superseded_at IS NULL"),
    )
    op.create_index(
        "ix_field_read_doc_field_created", "field_read", ["doc_id", "field_id", "created_at"]
    )

    op.create_table(
        "value_review",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "read_id", sa.String(36),
            sa.ForeignKey("field_read.id", name="fk_value_review_read_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("value", JSONB, nullable=True),
        sa.Column("note", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "user_id", sa.String(36),
            sa.ForeignKey("app_user.id", name="fk_value_review_user_id"), nullable=False,
        ),
        _ts("created_at"),
        sa.CheckConstraint(
            "action IN ('accepted','corrected')", name="ck_value_review_action"
        ),
    )
    op.create_index("ix_value_review_read_created", "value_review", ["read_id", "created_at"])

    op.create_table(
        "extraction_run_item",
        sa.Column(
            "run_id", sa.String(36),
            sa.ForeignKey(
                "extraction_run.id", name="fk_extraction_run_item_run_id", ondelete="CASCADE"
            ),
            primary_key=True,
        ),
        sa.Column(
            "doc_id", sa.String(36),
            sa.ForeignKey(
                "document.id", name="fk_extraction_run_item_doc_id", ondelete="CASCADE"
            ),
            primary_key=True,
        ),
        sa.Column("field_id", sa.String(36), primary_key=True),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column(
            "read_id", sa.String(36),
            sa.ForeignKey(
                "field_read.id", name="fk_extraction_run_item_read_id", ondelete="SET NULL"
            ),
            nullable=True,
        ),
        sa.CheckConstraint(
            "action IN ('extract','reuse','derive')", name="ck_extraction_run_item_action"
        ),
        sa.CheckConstraint(
            "outcome IN ('pending','reused','extracted','not_found','failed','derived')",
            name="ck_extraction_run_item_outcome",
        ),
    )

    op.drop_constraint("ck_api_job_kind", "api_job", type_="check")
    op.create_check_constraint(
        "ck_api_job_kind", "api_job",
        "kind IN ('ask','extract','factsheet','eval','extraction')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM api_job WHERE kind = 'extraction'")
    op.drop_constraint("ck_api_job_kind", "api_job", type_="check")
    op.create_check_constraint(
        "ck_api_job_kind", "api_job", "kind IN ('ask','extract','factsheet','eval')"
    )
    op.drop_table("extraction_run_item")
    op.drop_index("ix_value_review_read_created", table_name="value_review")
    op.drop_table("value_review")
    op.drop_index("ix_field_read_doc_field_created", table_name="field_read")
    op.drop_index("uq_field_read_live_key", table_name="field_read")
    op.drop_table("field_read")
    op.drop_table("extraction_run_report")
    op.drop_index("ix_extraction_run_user_created", table_name="extraction_run")
    op.drop_table("extraction_run")
    op.drop_table("extraction_schema_draft")
    op.drop_index("uq_extraction_field_tag", table_name="extraction_field")
    op.drop_table("extraction_field")
    op.drop_table("extraction_schema_version")
    op.drop_index("ix_extraction_schema_owner_user_id", table_name="extraction_schema")
    op.drop_table("extraction_schema")
