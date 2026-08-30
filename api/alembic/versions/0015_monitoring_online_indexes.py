"""build monitoring indexes online and validate additive foreign keys

Revision ID: 0015_monitoring_indexes
Revises: 0014_monitoring_findings
Create Date: 2026-08-30

The indexed tables may already contain enterprise-scale inventories. Index
creation therefore runs outside Alembic's transaction with PostgreSQL's
CONCURRENTLY mode. Revision 0014 installs nullable foreign keys as NOT VALID;
new writes are enforced immediately, while validation is performed here after
the transactional schema revision has committed.
"""

import sqlalchemy as sa
from alembic import op

revision = "0015_monitoring_indexes"
down_revision = "0014_monitoring_findings"
branch_labels = None
depends_on = None

INDEXES = (
    (
        "ix_scan_runs_source_created_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_scan_runs_source_created_id "
        "ON scan_runs (source_id, created_at DESC, id DESC)",
    ),
    (
        "ix_run_comparisons_source_created_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_run_comparisons_source_created_id "
        "ON run_comparisons (source_id, created_at DESC, id DESC)",
    ),
    (
        "ix_items_run_resource_identity_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_items_run_resource_identity_id "
        "ON items (run_id, resource_id, identity_key, id)",
    ),
    (
        "ix_items_active_resource_identity_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_items_active_resource_identity_id "
        "ON items (resource_id, identity_key, id) WHERE deleted IS FALSE",
    ),
    (
        "ix_resources_run_unkeyed_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_resources_run_unkeyed_id "
        "ON resources (run_id, id) WHERE identity_key IS NULL",
    ),
    (
        "ix_items_run_unkeyed_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_items_run_unkeyed_id "
        "ON items (run_id, id) WHERE identity_key IS NULL",
    ),
)

FOREIGN_KEYS = (
    ("scan_runs", "fk_scan_runs_source_id"),
    ("run_comparisons", "fk_run_comparisons_source_id"),
)
CHECK_CONSTRAINTS = (("run_comparisons", "ck_run_comparisons_trigger"),)


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


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, statement in INDEXES:
            _ensure_valid_index(name, statement)
        for table_name, constraint_name in (*FOREIGN_KEYS, *CHECK_CONSTRAINTS):
            op.execute(f"ALTER TABLE {table_name} VALIDATE CONSTRAINT {constraint_name}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _statement in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
