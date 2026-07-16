"""document and stage_run

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("checksum", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "stage_run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("doc_id", sa.String(36), sa.ForeignKey("document.id"), nullable=False),
        sa.Column("stage_name", sa.String(64), nullable=False),
        sa.Column("stage_version", sa.Integer(), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("output_hash", sa.String(64), nullable=True),
        sa.Column("artifact_key", sa.String(512), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        # The ONLY thing enforcing the status domain -- see the jobs migration note.
        sa.CheckConstraint(
            "status IN ('queued','running','done','failed')",
            name="ck_stage_run_status",
        ),
    )
    op.create_index("ix_stage_run_doc_id", "stage_run", ["doc_id"])
    op.create_index("ix_stage_run_stage_name", "stage_run", ["stage_name"])


def downgrade() -> None:
    op.drop_index("ix_stage_run_stage_name", table_name="stage_run")
    op.drop_index("ix_stage_run_doc_id", table_name="stage_run")
    op.drop_table("stage_run")
    op.drop_table("document")
