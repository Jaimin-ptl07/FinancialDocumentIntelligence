"""chunk table for RT-3 full-text keyword search

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chunk",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("doc_id", sa.String(36), sa.ForeignKey("document.id"), nullable=False),
        sa.Column("chunk_id", sa.String(64), nullable=False),
        sa.Column("doc_key", sa.String(256), nullable=True),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("heading", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("section_type", sa.String(64), nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_chunk_doc_id", "chunk", ["doc_id"])
    op.create_index("ix_chunk_doc_key_version", "chunk", ["doc_key", "version"])
    op.create_index("ix_chunk_section_type", "chunk", ["section_type"])
    op.create_index("ix_chunk_tsv", "chunk", ["tsv"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_chunk_tsv", table_name="chunk")
    op.drop_index("ix_chunk_section_type", table_name="chunk")
    op.drop_index("ix_chunk_doc_key_version", table_name="chunk")
    op.drop_index("ix_chunk_doc_id", table_name="chunk")
    op.drop_table("chunk")
