"""app_user + refresh_token: the identity tables (ADR-0002, T3)

The Gated demo's login wall. `app_user` holds Invitee accounts created only by
the admin CLI (no signup endpoint exists); `refresh_token` holds one row per
login, keyed by the SHA-256 of the opaque cookie value, so rotation and logout
are row updates and a database leak leaks no usable cookies.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-07
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_user",
        sa.Column("id", sa.String(36), primary_key=True),
        # RFC 5321's forward-mailbox ceiling, not the folklore 254.
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "active", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("email", name="uq_app_user_email"),
    )
    op.create_table(
        "refresh_token",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("app_user.id", name="fk_refresh_token_user_id"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_refresh_token_user_id", "refresh_token", ["user_id"])
    # The rotation/revocation lookup is always by the presented cookie's hash.
    op.create_index("uq_refresh_token_hash", "refresh_token", ["token_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_refresh_token_hash", table_name="refresh_token")
    op.drop_index("ix_refresh_token_user_id", table_name="refresh_token")
    op.drop_table("refresh_token")
    op.drop_table("app_user")
