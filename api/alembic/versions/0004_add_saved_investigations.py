"""add saved investigations

Revision ID: 0004_add_saved_investigations
Revises: 0003_add_nfs_share_type
Create Date: 2026-03-10
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_add_saved_investigations"
down_revision = "0003_add_nfs_share_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "saved_investigations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("target_tab", sa.String(length=32), server_default="items", nullable=False),
        sa.Column("query_text", sa.Text(), server_default="", nullable=False),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_saved_investigations_project_updated", "saved_investigations", ["project_id", "updated_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_saved_investigations_project_updated", table_name="saved_investigations")
    op.drop_table("saved_investigations")
