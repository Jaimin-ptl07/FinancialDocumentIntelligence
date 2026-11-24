"""report link confirmation: an Invitee confirming or correcting a Report's
Company and Fiscal year (S2.3)

* `document.fiscal_year_method` -- how the Fiscal year was set: null while
  it is the issuer stage's reading, `confirmed_by_invitee` once an Invitee
  corrected it. Re-resolving a Report leaves an Invitee's Fiscal year alone,
  exactly as `link_method = 'confirmed_by_invitee'` leaves its Company alone.
* `report_link_change` -- every manual confirm or correction of a Report's
  Company or Fiscal year, with the acting Invitee and the time. Append only;
  it goes with its Report if the Report is deleted (CASCADE), as the Report's
  stage history does.

Additive only.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("document", sa.Column("fiscal_year_method", sa.String(32), nullable=True))
    op.create_table(
        "report_link_change",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "doc_id",
            sa.String(36),
            sa.ForeignKey(
                "document.id", name="fk_report_link_change_doc_id", ondelete="CASCADE"
            ),
            nullable=False,
        ),
        sa.Column(
            "invitee_id",
            sa.String(36),
            sa.ForeignKey("app_user.id", name="fk_report_link_change_invitee_id"),
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
    op.create_index("ix_report_link_change_doc_id", "report_link_change", ["doc_id"])


def downgrade() -> None:
    op.drop_index("ix_report_link_change_doc_id", table_name="report_link_change")
    op.drop_table("report_link_change")
    op.drop_column("document", "fiscal_year_method")
