"""add SharePoint provider identity indexes

Revision ID: 0011_sharepoint_indexes
Revises: 0010_sharepoint_inventory
Create Date: 2026-08-24
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_sharepoint_indexes"
down_revision = "0010_sharepoint_inventory"
branch_labels = None
depends_on = None


INDEXES = (
    (
        "uq_resources_run_endpoint_type_name_legacy",
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
        "uq_resources_run_endpoint_type_name_legacy "
        "ON resources (run_id, endpoint_id, resource_type, name) "
        "WHERE provider_resource_id IS NULL",
    ),
    (
        "uq_resources_run_endpoint_provider_id",
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
        "uq_resources_run_endpoint_provider_id "
        "ON resources (run_id, endpoint_id, resource_type, provider_resource_id) "
        "WHERE provider_resource_id IS NOT NULL",
    ),
    (
        "uq_items_run_resource_path_legacy",
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
        "uq_items_run_resource_path_legacy "
        "ON items (run_id, resource_id, path) WHERE provider_item_id IS NULL",
    ),
    (
        "uq_items_run_resource_provider_id",
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
        "uq_items_run_resource_provider_id "
        "ON items (run_id, resource_id, provider_item_id) "
        "WHERE provider_item_id IS NOT NULL",
    ),
    (
        "ix_resources_run_provider_exposure_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_resources_run_provider_exposure_id "
        "ON resources (run_id, provider, exposure, id)",
    ),
    (
        "ix_items_run_provider_exposure_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_items_run_provider_exposure_id "
        "ON items (run_id, provider, exposure, id)",
    ),
)

LEGACY_CONSTRAINTS = (
    ("resources", "uq_resources_run_endpoint_type_name"),
    ("items", "uq_items_run_resource_path"),
)

LEGACY_DUPLICATE_PREFLIGHTS = (
    (
        "resources",
        """
        SELECT 1
        FROM resources
        GROUP BY run_id, endpoint_id, resource_type, name
        HAVING COUNT(*) > 1
        LIMIT 1
        """,
    ),
    (
        "items",
        """
        SELECT 1
        FROM items
        GROUP BY run_id, resource_id, path
        HAVING COUNT(*) > 1
        LIMIT 1
        """,
    ),
)


def _index_validity(name: str) -> bool | None:
    return (
        op.get_bind()
        .execute(
            sa.text(
                """
            SELECT index_state.indisvalid
            FROM pg_catalog.pg_index AS index_state
            JOIN pg_catalog.pg_class AS index_class
              ON index_class.oid = index_state.indexrelid
            WHERE index_class.relname = :name
              AND pg_catalog.pg_table_is_visible(index_class.oid)
            """
            ),
            {"name": name},
        )
        .scalar_one_or_none()
    )


def _ensure_valid_index(name: str, statement: str) -> None:
    validity = _index_validity(name)
    if validity is True:
        return
    if validity is False:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.execute(statement)


def _preflight_legacy_constraints() -> None:
    """Fail before schema mutation when legacy uniqueness cannot be restored."""

    connection = op.get_bind()
    duplicate_tables = [
        table_name
        for table_name, statement in LEGACY_DUPLICATE_PREFLIGHTS
        if connection.execute(sa.text(statement)).first() is not None
    ]
    if duplicate_tables:
        joined = ", ".join(duplicate_tables)
        raise RuntimeError(
            "cannot downgrade SharePoint identity indexes: provider-identified rows "
            f"contain duplicate legacy keys in {joined}"
        )


def upgrade() -> None:
    # These tables can contain millions of rows. The schema columns are already
    # durable in 0010, so interrupted concurrent builds are safe to inspect and
    # retry without replaying ADD COLUMN statements.
    with op.get_context().autocommit_block():
        for name, statement in INDEXES:
            _ensure_valid_index(name, statement)

    for table_name, constraint_name in LEGACY_CONSTRAINTS:
        op.execute(f"ALTER TABLE {table_name} DROP CONSTRAINT IF EXISTS {constraint_name}")


def downgrade() -> None:
    # The concurrent index drops cannot be rolled back. Prove that both legacy
    # constraints can be restored before making any schema change, so an unsafe
    # downgrade fails with all SharePoint indexes still protecting the data.
    _preflight_legacy_constraints()
    with op.get_context().autocommit_block():
        for name, _statement in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")

    op.create_unique_constraint(
        "uq_resources_run_endpoint_type_name",
        "resources",
        ["run_id", "endpoint_id", "resource_type", "name"],
    )
    op.create_unique_constraint(
        "uq_items_run_resource_path",
        "items",
        ["run_id", "resource_id", "path"],
    )
