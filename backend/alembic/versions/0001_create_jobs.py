"""create jobs table

Revision ID: 0001
Revises:
Create Date: 2026-07-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("artifact_key", sa.String(512), nullable=True),
        sa.Column("error", sa.String(2048), nullable=True),
        sa.Column("correlation_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        # The ONLY thing enforcing the status domain -- see the note below.
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed')",
            name="ck_jobs_status",
        ),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_correlation_id", "jobs", ["correlation_id"])


def downgrade() -> None:
    op.drop_index("ix_jobs_correlation_id", table_name="jobs")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_table("jobs")
