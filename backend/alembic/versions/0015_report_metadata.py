"""report metadata: what each Report covers, and how it was filed

Adds to the document registry (no second documents table, design §7.3B):

* `fiscal_year` ("FY2025-26"), `period_start`, `period_end` -- the Fiscal
  year the Report covers, read by the issuer stage. Null when it printed
  none, or the cover and the statements disagree (an Invitee settles it);
* `report_type` -- "annual_report" for now;
* `link_status` -- `linked`, `awaiting_confirmation` (CINs printed, none
  clearly the issuer's) or `unidentified` (no CIN at all); null until the
  issuer stage has run;
* `link_method` -- how a linked Report was linked: `cin` for now.

Statement bases are deliberately NOT stored: they are read from the basis
segmentation and surfaced as "bases present".

Additive only: existing Reports are filled by the backfill (`fdi-companies
backfill`) running the issuer stage + registry resolution, not by a data
migration here.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-22
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("document", sa.Column("fiscal_year", sa.String(16), nullable=True))
    op.add_column("document", sa.Column("period_start", sa.Date, nullable=True))
    op.add_column("document", sa.Column("period_end", sa.Date, nullable=True))
    op.add_column("document", sa.Column("report_type", sa.String(32), nullable=True))
    op.add_column("document", sa.Column("link_status", sa.String(32), nullable=True))
    op.add_column("document", sa.Column("link_method", sa.String(32), nullable=True))
    op.create_check_constraint(
        "ck_document_link_status",
        "document",
        "link_status IN ('linked','awaiting_confirmation','unidentified')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_document_link_status", "document", type_="check")
    for column in (
        "link_method", "link_status", "report_type", "period_end", "period_start", "fiscal_year",
    ):
        op.drop_column("document", column)
