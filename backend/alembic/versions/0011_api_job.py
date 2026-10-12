"""api_job: the polled jobs behind the four expensive routes (T5)

`/ask`, `/extract/document`, `/factsheet/document` and `/eval` used to hold an
HTTP request open across many sequential Azure calls. They now insert one row
here, publish `fdi.run_job`, and answer 202; the worker walks the row through
queued -> running -> succeeded | failed and stores the body the route used to
return inline. Every row has an owning Invitee: a job is visible to nobody
else (cross-user reads are 404, not 403 -- existence is not disclosed).

Not the `jobs` table 0001 created and 0003 dropped: that one tracked pipeline
work, which now lives in `stage_run`. This one tracks API requests, which have
no document to hang off (`/ask` is corpus-wide).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-11
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_job",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("app_user.id", name="fk_api_job_user_id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.String(36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "kind IN ('ask','extract','factsheet','eval')", name="ck_api_job_kind"
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed')", name="ck_api_job_status"
        ),
    )
    # The only read paths: "my jobs, newest first" and by primary key.
    op.create_index("ix_api_job_user_created", "api_job", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_api_job_user_created", table_name="api_job")
    op.drop_table("api_job")
