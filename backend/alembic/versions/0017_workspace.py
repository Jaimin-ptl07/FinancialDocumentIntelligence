"""workspace: an Invitee's working sets of Companies (S2.5)

A Workspace is owned by one Invitee and holds a set of Companies. It is a
focus list, never an access boundary (#16): nothing reads it to decide who
may see a Company or Report. Membership cascades from both sides, so
deleting a Workspace or a Company removes the membership row and nothing
else; removing an Invitee's account removes their Workspaces.

Additive only: two new tables, no existing table is altered.

Revision ID: 0017
Revises: 0014
Create Date: 2026-11-22
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.String(36),
            sa.ForeignKey(
                "app_user.id", name="fk_workspace_owner_user_id", ondelete="CASCADE"
            ),
            nullable=False,
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_workspace_owner_user_id", "workspace", ["owner_user_id"])
    op.create_table(
        "workspace_company",
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey(
                "workspace.id", name="fk_workspace_company_workspace_id", ondelete="CASCADE"
            ),
            primary_key=True,
        ),
        sa.Column(
            "company_id",
            sa.String(36),
            sa.ForeignKey(
                "company.id", name="fk_workspace_company_company_id", ondelete="CASCADE"
            ),
            primary_key=True,
        ),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_workspace_company_company_id", "workspace_company", ["company_id"])


def downgrade() -> None:
    op.drop_index("ix_workspace_company_company_id", table_name="workspace_company")
    op.drop_table("workspace_company")
    op.drop_index("ix_workspace_owner_user_id", table_name="workspace")
    op.drop_table("workspace")
