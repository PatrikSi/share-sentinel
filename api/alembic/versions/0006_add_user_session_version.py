"""add user session version

Revision ID: 0006_add_user_session_version
Revises: 0005_unique_project_names
Create Date: 2026-04-19
"""

from alembic import op
import sqlalchemy as sa

revision = "0006_add_user_session_version"
down_revision = "0005_unique_project_names"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("session_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )


def downgrade() -> None:
    op.drop_column("users", "session_version")
