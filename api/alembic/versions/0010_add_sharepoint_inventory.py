"""add first-class SharePoint inventory metadata

Revision ID: 0010_sharepoint_inventory
Revises: 0009_api_scale_indexes
Create Date: 2026-08-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_sharepoint_inventory"
down_revision = "0009_api_scale_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL requires a newly-added enum value to be committed before it
    # can be used. Keep this small DDL operation outside the column transaction.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE resource_type ADD VALUE IF NOT EXISTS 'sharepoint_library'")

    jsonb = postgresql.JSONB(astext_type=sa.Text())
    empty_object = sa.text("'{}'::jsonb")
    op.add_column(
        "scan_runs",
        sa.Column("collection_context", jsonb, server_default=empty_object, nullable=False),
    )
    op.add_column("endpoints", sa.Column("provider", sa.String(length=32), nullable=True))
    op.add_column(
        "endpoints",
        sa.Column("provider_metadata", jsonb, server_default=empty_object, nullable=False),
    )
    op.add_column("resources", sa.Column("provider", sa.String(length=32), nullable=True))
    op.add_column("resources", sa.Column("provider_resource_id", sa.String(length=512), nullable=True))
    op.add_column("resources", sa.Column("web_url", sa.Text(), nullable=True))
    op.add_column(
        "resources",
        sa.Column("provider_metadata", jsonb, server_default=empty_object, nullable=False),
    )
    op.add_column("resources", sa.Column("exposure", sa.String(length=32), nullable=True))
    op.add_column(
        "resources",
        sa.Column("exposure_evidence", jsonb, server_default=empty_object, nullable=False),
    )
    op.add_column("items", sa.Column("provider", sa.String(length=32), nullable=True))
    op.add_column("items", sa.Column("provider_item_id", sa.String(length=512), nullable=True))
    op.add_column("items", sa.Column("provider_parent_id", sa.String(length=512), nullable=True))
    op.add_column("items", sa.Column("web_url", sa.Text(), nullable=True))
    op.add_column("items", sa.Column("mime_type", sa.String(length=255), nullable=True))
    op.add_column("items", sa.Column("deleted", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column(
        "items",
        sa.Column("provider_metadata", jsonb, server_default=empty_object, nullable=False),
    )
    op.add_column("items", sa.Column("exposure", sa.String(length=32), nullable=True))
    op.add_column(
        "items",
        sa.Column("exposure_evidence", jsonb, server_default=empty_object, nullable=False),
    )


def downgrade() -> None:
    # The enum value itself intentionally remains: PostgreSQL cannot safely
    # remove enum members while rows or dependent deployments may reference it.
    for table_name, column_names in (
        (
            "items",
            (
                "exposure_evidence",
                "exposure",
                "provider_metadata",
                "deleted",
                "mime_type",
                "web_url",
                "provider_parent_id",
                "provider_item_id",
                "provider",
            ),
        ),
        (
            "resources",
            (
                "exposure_evidence",
                "exposure",
                "provider_metadata",
                "web_url",
                "provider_resource_id",
                "provider",
            ),
        ),
        ("endpoints", ("provider_metadata", "provider")),
        ("scan_runs", ("collection_context",)),
    ):
        for column_name in column_names:
            op.drop_column(table_name, column_name)
