"""drop jobs

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("jobs")


def downgrade() -> None:
    # Reversibility: recreate exactly the SP-1 jobs table from 0001_create_jobs.py.
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
        # The ONLY thing enforcing the status domain -- see the note in 0001.
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed')",
            name="ck_jobs_status",
        ),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_correlation_id", "jobs", ["correlation_id"])
