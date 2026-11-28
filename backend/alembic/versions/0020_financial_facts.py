"""Concepts, Observations, Financial facts and their validation results (S5)

ADR-0006. The per-Report financial object graph artifacts stay the record of
how each value was derived; these tables are their cross-Report PROJECTION,
rebuildable at any time (`fdi-facts backfill`), and nothing else writes them.

* `concept` -- the taxonomy Concepts, seeded from the FDI standard schema.
* `financial_observation` -- one value one source states, unique on
  Report version x basis x Concept x Fiscal year x column x source.
* `financial_fact` -- the value the fact rule picks per Company x Concept x
  Fiscal year x basis, re-derived from Observations whenever they change.
* `financial_validation` -- the graph's checks, per Report version x basis.
* `financial_projection` -- what was projected from which artifacts, so a
  re-projection of unchanged inputs is a no-op.

Values are NUMERIC, never float. Deleting a document cascades to its rows
(its Company's facts are re-derived by the caller).

Additive: no existing table loses a column or a row.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-28
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def _ts(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True),
        server_default=None if nullable else sa.func.now(), nullable=nullable,
    )


def _doc_fk(table: str) -> sa.Column:
    return sa.Column(
        "doc_id", sa.String(36),
        sa.ForeignKey("document.id", name=f"fk_{table}_doc_id", ondelete="CASCADE"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "concept",
        sa.Column("tag", sa.String(64), primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("statement", sa.Text, nullable=False),
        sa.Column("period_kind", sa.String(16), nullable=False),
        sa.Column("source_version", sa.Text, nullable=False),
        _ts("created_at"),
        sa.CheckConstraint(
            "period_kind IN ('duration','instant')", name="ck_concept_period_kind"
        ),
    )

    op.create_table(
        "financial_projection",
        sa.Column(
            "doc_id", sa.String(36),
            sa.ForeignKey(
                "document.id", name="fk_financial_projection_doc_id", ondelete="CASCADE"
            ),
            primary_key=True,
        ),
        sa.Column(
            "company_id", sa.String(36),
            sa.ForeignKey("company.id", name="fk_financial_projection_company_id"),
            nullable=True,
        ),
        sa.Column("fiscal_year", sa.String(16), nullable=True),
        sa.Column("projection_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text, nullable=False, server_default=""),
        sa.Column("observation_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("diagnostics", JSONB, nullable=False),
        _ts("projected_at"),
        sa.CheckConstraint(
            "status IN ('projected','skipped')", name="ck_financial_projection_status"
        ),
    )
    op.create_index(
        "ix_financial_projection_company", "financial_projection", ["company_id"]
    )

    op.create_table(
        "financial_observation",
        sa.Column("id", sa.String(36), primary_key=True),
        _doc_fk("financial_observation"),
        sa.Column(
            "company_id", sa.String(36),
            sa.ForeignKey("company.id", name="fk_financial_observation_company_id"),
            nullable=False,
        ),
        sa.Column("basis", sa.String(16), nullable=False),
        sa.Column(
            "concept", sa.String(64),
            sa.ForeignKey("concept.tag", name="fk_financial_observation_concept"),
            nullable=False,
        ),
        sa.Column("fiscal_year", sa.String(16), nullable=False),
        sa.Column("report_column", sa.String(16), nullable=False),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("doc_key", sa.String(256), nullable=True),
        sa.Column("version", sa.Integer, nullable=True),
        sa.Column("report_fiscal_year", sa.String(16), nullable=False),
        sa.Column("value", sa.Numeric, nullable=True),
        sa.Column("printed", sa.Text, nullable=True),
        sa.Column("currency", sa.String(8), nullable=True),
        sa.Column("scale", sa.Numeric, nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("unit_printed", sa.Text, nullable=False, server_default=""),
        sa.Column("normalised", sa.Numeric, nullable=True),
        sa.Column("period_kind", sa.String(16), nullable=False),
        sa.Column("period_end", sa.Date, nullable=True),
        sa.Column("label", sa.Text, nullable=False, server_default=""),
        sa.Column("line_item", sa.String(512), nullable=True),
        sa.Column("page", sa.Integer, nullable=True),
        sa.Column("table_index", sa.Integer, nullable=True),
        sa.Column("evidence_id", sa.String(640), nullable=True),
        sa.Column("validation", sa.String(16), nullable=False),
        sa.Column("confidence", sa.String(16), nullable=True),
        sa.Column("inputs", JSONB, nullable=False),
        sa.Column(
            "extracted_value_id", sa.String(36),
            sa.ForeignKey(
                "field_read.id", name="fk_financial_observation_field_read",
                ondelete="CASCADE",
            ),
            nullable=True,
        ),
        _ts("created_at"),
        sa.UniqueConstraint(
            "doc_id", "basis", "concept", "fiscal_year", "report_column", "source",
            name="uq_financial_observation_identity",
        ),
        sa.CheckConstraint(
            "report_column IN ('current','comparative')",
            name="ck_financial_observation_column",
        ),
        sa.CheckConstraint(
            "source IN ('graph_face','graph_note','graph_composed',"
            "'extraction_accepted','extraction_corrected')",
            name="ck_financial_observation_source",
        ),
        sa.CheckConstraint(
            "validation IN ('passed','failed','unchecked')",
            name="ck_financial_observation_validation",
        ),
    )
    op.create_index(
        "ix_financial_observation_fact", "financial_observation",
        ["company_id", "basis", "concept", "fiscal_year"],
    )

    op.create_table(
        "financial_fact",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "company_id", sa.String(36),
            sa.ForeignKey("company.id", name="fk_financial_fact_company_id"),
            nullable=False,
        ),
        sa.Column(
            "concept", sa.String(64),
            sa.ForeignKey("concept.tag", name="fk_financial_fact_concept"),
            nullable=False,
        ),
        sa.Column("fiscal_year", sa.String(16), nullable=False),
        sa.Column("basis", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "selected_observation_id", sa.String(36),
            sa.ForeignKey(
                "financial_observation.id", name="fk_financial_fact_selected",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column("value", sa.Numeric, nullable=True),
        sa.Column("normalised", sa.Numeric, nullable=True),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("restated", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("restatement_original_id", sa.String(36), nullable=True),
        sa.Column("disagreement_original_id", sa.String(36), nullable=True),
        sa.Column("derived_observation_id", sa.String(36), nullable=True),
        sa.Column("observation_ids", JSONB, nullable=False),
        _ts("updated_at"),
        sa.UniqueConstraint(
            "company_id", "concept", "fiscal_year", "basis", name="uq_financial_fact_key"
        ),
        sa.CheckConstraint(
            "status IN ('reported','composed','restated','corrected','not_reported')",
            name="ck_financial_fact_status",
        ),
    )
    op.create_index(
        "ix_financial_fact_history", "financial_fact",
        ["company_id", "basis", "concept", "fiscal_year"],
    )

    op.create_table(
        "financial_validation",
        sa.Column("id", sa.String(36), primary_key=True),
        _doc_fk("financial_validation"),
        sa.Column("basis", sa.String(16), nullable=False),
        sa.Column("rule_id", sa.String(256), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("passed", sa.Boolean, nullable=False),
        sa.Column("tags", JSONB, nullable=False),
        sa.Column("expected", sa.Text, nullable=False, server_default=""),
        sa.Column("actual", sa.Numeric, nullable=True),
        sa.Column("delta", sa.Numeric, nullable=True),
        sa.Column("tolerance", sa.Numeric, nullable=True),
        sa.UniqueConstraint(
            "doc_id", "basis", "rule_id", name="uq_financial_validation_rule"
        ),
    )
    op.create_index(
        "ix_financial_validation_tags", "financial_validation", ["tags"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_table("financial_validation")
    op.drop_table("financial_fact")
    op.drop_table("financial_observation")
    op.drop_table("financial_projection")
    op.drop_table("concept")
