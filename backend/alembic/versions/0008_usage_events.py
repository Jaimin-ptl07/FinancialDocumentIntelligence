"""usage events

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ocr_usage_event",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("doc_id", sa.String(36), sa.ForeignKey("document.id"), nullable=False),
        sa.Column(
            "stage_run_id", sa.String(36), sa.ForeignKey("stage_run.id"), nullable=False
        ),
        sa.Column("correlation_id", sa.String(36), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("azure_model", sa.String(64), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ocr_usage_event_doc_id", "ocr_usage_event", ["doc_id"])
    op.create_index(
        "ix_ocr_usage_event_stage_run_id", "ocr_usage_event", ["stage_run_id"]
    )

    op.create_table(
        "llm_usage_event",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("doc_id", sa.String(36), sa.ForeignKey("document.id"), nullable=True),
        sa.Column(
            "stage_run_id", sa.String(36), sa.ForeignKey("stage_run.id"), nullable=True
        ),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("correlation_id", sa.String(36), nullable=True),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "source IN ('semantic_classify','extract_document','ask_agent')",
            name="ck_llm_usage_event_source",
        ),
    )
    op.create_index("ix_llm_usage_event_doc_id", "llm_usage_event", ["doc_id"])
    op.create_index(
        "ix_llm_usage_event_stage_run_id", "llm_usage_event", ["stage_run_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_llm_usage_event_stage_run_id", table_name="llm_usage_event")
    op.drop_index("ix_llm_usage_event_doc_id", table_name="llm_usage_event")
    op.drop_table("llm_usage_event")
    op.drop_index("ix_ocr_usage_event_stage_run_id", table_name="ocr_usage_event")
    op.drop_index("ix_ocr_usage_event_doc_id", table_name="ocr_usage_event")
    op.drop_table("ocr_usage_event")
