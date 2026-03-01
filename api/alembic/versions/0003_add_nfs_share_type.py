"""add nfs resource type

Revision ID: 0003_add_nfs_share_type
Revises: 0002_enterprise_features
Create Date: 2026-03-01
"""

from alembic import op

revision = "0003_add_nfs_share_type"
down_revision = "0002_enterprise_features"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE resource_type ADD VALUE IF NOT EXISTS 'nfs_share'")


def downgrade() -> None:
    # PostgreSQL enums do not support dropping a value safely.
    pass
