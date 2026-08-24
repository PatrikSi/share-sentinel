"""add access capabilities and item metadata

Revision ID: 0008_access_capabilities
Revises: 0007_ingest_error_fp
Create Date: 2026-08-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_access_capabilities"
down_revision = "0007_ingest_error_fp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE access_level ADD VALUE IF NOT EXISTS 'unknown'")
    op.add_column(
        "resources",
        sa.Column(
            "access_capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column("items", sa.Column("allocation_size_bytes", sa.BigInteger(), nullable=True))
    op.add_column("items", sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("items", sa.Column("accessed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("items", sa.Column("changed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "items",
        sa.Column(
            "file_attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    # Older application versions do not recognize UNKNOWN. Keep the additive
    # PostgreSQL enum label, but make persisted rows readable before the
    # capability columns disappear.
    op.execute("UPDATE resources SET access_level = 'no_access' WHERE access_level = 'unknown'")
    op.drop_column("items", "file_attributes")
    op.drop_column("items", "changed_at")
    op.drop_column("items", "accessed_at")
    op.drop_column("items", "created_at")
    op.drop_column("items", "allocation_size_bytes")
    op.drop_column("resources", "access_capabilities")
    # PostgreSQL enums do not support safely dropping an individual value.
