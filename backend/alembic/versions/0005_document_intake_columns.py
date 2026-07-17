"""document intake columns

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("document", sa.Column("filename", sa.Text(), nullable=True))
    op.add_column("document", sa.Column("byte_size", sa.Integer(), nullable=True))
    op.add_column("document", sa.Column("content_type", sa.String(128), nullable=True))
    op.add_column("document", sa.Column("doc_key", sa.String(256), nullable=True))
    op.add_column("document", sa.Column("version", sa.Integer(), nullable=True))
    op.create_unique_constraint("uq_document_checksum", "document", ["checksum"])


def downgrade() -> None:
    op.drop_constraint("uq_document_checksum", "document", type_="unique")
    for col in ("version", "doc_key", "content_type", "byte_size", "filename"):
        op.drop_column("document", col)
