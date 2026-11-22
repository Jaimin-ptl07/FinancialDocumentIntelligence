"""company identifiers: hand-entered aliases and exchange codes, and the log
of manual changes to Companies (S2.4)

A Company is identified by its CIN (ADR-0003) but found by the names people
use. `company_identifier` holds an Invitee's NSE symbols, BSE codes, ISINs and
aliases, unique per (kind, key) across all Companies -- `key` is the
spelling-independent form (`fdi.core.company_names.identifier_key`), so one
ticker cannot point at two Companies and "RIL" cannot be entered twice.

`company_change` records every manual change -- create, update, add or remove
an identifier -- with the acting Invitee and the time. Append only.

Additive only.

Revision ID: 0016
Revises: 0014
Create Date: 2026-11-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0016"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_identifier",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "company_id",
            sa.String(36),
            sa.ForeignKey(
                "company.id", name="fk_company_identifier_company_id", ondelete="CASCADE"
            ),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("key", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.UniqueConstraint("kind", "key", name="uq_company_identifier_kind_key"),
    )
    op.create_index(
        "ix_company_identifier_company_id", "company_identifier", ["company_id"]
    )
    op.create_table(
        "company_change",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "company_id",
            sa.String(36),
            sa.ForeignKey(
                "company.id", name="fk_company_change_company_id", ondelete="CASCADE"
            ),
            nullable=False,
        ),
        sa.Column(
            "invitee_id",
            sa.String(36),
            sa.ForeignKey("app_user.id", name="fk_company_change_invitee_id"),
            nullable=False,
        ),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("detail", JSONB, nullable=False),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
    )
    op.create_index("ix_company_change_company_id", "company_change", ["company_id"])


def downgrade() -> None:
    op.drop_index("ix_company_change_company_id", table_name="company_change")
    op.drop_table("company_change")
    op.drop_index("ix_company_identifier_company_id", table_name="company_identifier")
    op.drop_table("company_identifier")
