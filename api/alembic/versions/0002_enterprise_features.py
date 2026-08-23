"""enterprise auth and governance features

Revision ID: 0002_enterprise_features
Revises: 0001_initial
Create Date: 2026-03-01
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_enterprise_features"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_approved", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column(
        "users",
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("approved_by_user_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_users_approved_by_user_id_users",
        "users",
        "users",
        ["approved_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute("UPDATE users SET approved_at = created_at WHERE is_approved = true")
    op.create_index("ix_users_approval_status", "users", ["is_approved", "created_at"], unique=False)

    op.add_column(
        "api_tokens",
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column(
        "api_tokens",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE api_tokens
        SET scopes = CASE role
            WHEN 'admin' THEN '[
                "read:projects","read:runs","read:inventory","write:runs",
                "read:audit","read:members","write:members","read:tokens","write:tokens"
            ]'::jsonb
            WHEN 'operator' THEN '["read:projects","read:runs","read:inventory","write:runs"]'::jsonb
            ELSE '["read:projects","read:runs","read:inventory"]'::jsonb
        END
        """
    )


def downgrade() -> None:
    op.drop_column("api_tokens", "expires_at")
    op.drop_column("api_tokens", "scopes")

    op.drop_index("ix_users_approval_status", table_name="users")
    op.drop_constraint("fk_users_approved_by_user_id_users", "users", type_="foreignkey")
    op.drop_column("users", "approved_by_user_id")
    op.drop_column("users", "approved_at")
    op.drop_column("users", "is_approved")
