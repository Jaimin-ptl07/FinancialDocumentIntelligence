"""allow eval_judge as an llm_usage_event source

The answer-quality eval's optional LLM judge (S1.5, #44) spends Azure OpenAI
tokens. Recorded under a product source it would inflate what /ask appears
to cost; left unrecorded it would be spend nobody can see. Its own source
keeps eval spend in the same ledger and separable from product spend.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-22
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_OLD = "source IN ('semantic_classify','fog_mapping','extract_document','ask_agent')"
_NEW = (
    "source IN ('semantic_classify','fog_mapping','extract_document','ask_agent',"
    "'eval_judge')"
)


def upgrade() -> None:
    op.drop_constraint("ck_llm_usage_event_source", "llm_usage_event", type_="check")
    op.create_check_constraint("ck_llm_usage_event_source", "llm_usage_event", _NEW)


def downgrade() -> None:
    # As in 0009: rows under the new source would fail the old constraint.
    op.execute("DELETE FROM llm_usage_event WHERE source = 'eval_judge'")
    op.drop_constraint("ck_llm_usage_event_source", "llm_usage_event", type_="check")
    op.create_check_constraint("ck_llm_usage_event_source", "llm_usage_event", _OLD)
