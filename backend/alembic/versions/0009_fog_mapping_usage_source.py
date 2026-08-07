"""allow fog_mapping as an llm_usage_event source

`ck_llm_usage_event_source` was written when three call sites existed. The
FOG pipeline added a fourth -- `fog_mapping`, the ONLY stage in that pipeline
that spends money -- and the constraint rejected it. Because the insert
happens after the Azure call returns, the rejection did not merely lose a
billing row: it raised inside the stage and killed the run, discarding the
mapping work that had already been paid for.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-08
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_OLD = "source IN ('semantic_classify','extract_document','ask_agent')"
_NEW = "source IN ('semantic_classify','fog_mapping','extract_document','ask_agent')"


def upgrade() -> None:
    op.drop_constraint("ck_llm_usage_event_source", "llm_usage_event", type_="check")
    op.create_check_constraint("ck_llm_usage_event_source", "llm_usage_event", _NEW)


def downgrade() -> None:
    # Rows recorded under the new source would violate the old constraint, so
    # they are removed rather than left to fail the ADD CONSTRAINT validation.
    op.execute("DELETE FROM llm_usage_event WHERE source = 'fog_mapping'")
    op.drop_constraint("ck_llm_usage_event_source", "llm_usage_event", type_="check")
    op.create_check_constraint("ck_llm_usage_event_source", "llm_usage_event", _OLD)
