"""eval tables

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "eval_run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("candidate_name", sa.String(128), nullable=False),
        sa.Column("candidate_version", sa.Integer(), nullable=False),
        sa.Column("dataset_name", sa.String(128), nullable=False),
        sa.Column("dataset_version", sa.Integer(), nullable=False),
        sa.Column("metric_name", sa.String(64), nullable=False),
        sa.Column("aggregate", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_eval_run_dataset_metric", "eval_run", ["dataset_name", "metric_name"])
    op.create_table(
        "eval_case_result",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("eval_run_id", sa.String(36), sa.ForeignKey("eval_run.id"), nullable=False),
        sa.Column("case_id", sa.String(128), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
    )
    op.create_index("ix_eval_case_result_run", "eval_case_result", ["eval_run_id"])


def downgrade() -> None:
    op.drop_table("eval_case_result")
    op.drop_table("eval_run")
