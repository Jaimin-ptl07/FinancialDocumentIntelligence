"""company: the Company registry, and each Report's link to its Company

A Company is identified by its CIN (ADR-0003): one CIN, one Company, enforced
here by a unique constraint so two concurrent uploads of a new issuer cannot
fork it. A Report keeps its free-text `doc_key` unchanged -- it identifies the
Report (one Report, many versions), not the Company -- and gains a nullable
link to the Company it was filed under. Null means "not filed yet": the
issuer stage has not run, or found no CIN it could trust.

Additive only: existing rows are linked by the backfill (`fdi-companies
backfill`) running the same issuer stage + registry resolution as new
uploads, not by a data migration here.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-22
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cin", sa.String(21), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("cin", name="uq_company_cin"),
    )
    op.add_column(
        "document",
        sa.Column(
            "company_id",
            sa.String(36),
            sa.ForeignKey("company.id", name="fk_document_company_id"),
            nullable=True,
        ),
    )
    op.create_index("ix_document_company_id", "document", ["company_id"])


def downgrade() -> None:
    op.drop_index("ix_document_company_id", table_name="document")
    op.drop_column("document", "company_id")
    op.drop_table("company")
