"""attribute llm_usage_event rows to the api_job that spent them

`GET /jobs/{job_id}/usage` (S1.3) reports one job's LLM spend to its owner.
The obvious key, `correlation_id`, cannot carry that: the id is caller-
supplied (`X-Request-ID`), so an Invitee can stamp their own job with someone
else's and the two jobs' usage rows become indistinguishable. Ownership has to
be part of the lookup, and only the job row knows its owner -- so each usage
row now names its job.

Nullable: pipeline stages and synchronous routes spend money outside any API
job, and rows recorded before this revision have no job to name.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-22
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_usage_event",
        sa.Column(
            "api_job_id",
            sa.String(36),
            sa.ForeignKey("api_job.id", name="fk_llm_usage_event_api_job_id"),
            nullable=True,
        ),
    )
    op.create_index("ix_llm_usage_event_api_job_id", "llm_usage_event", ["api_job_id"])


def downgrade() -> None:
    op.drop_index("ix_llm_usage_event_api_job_id", table_name="llm_usage_event")
    op.drop_column("llm_usage_event", "api_job_id")
