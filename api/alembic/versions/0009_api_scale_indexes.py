"""add API query scale indexes

Revision ID: 0009_api_scale_indexes
Revises: 0008_access_capabilities
Create Date: 2026-08-24
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_api_scale_indexes"
down_revision = "0008_access_capabilities"
branch_labels = None
depends_on = None


INDEXES = (
    (
        "ix_scan_runs_project_created_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_scan_runs_project_created_id "
        "ON scan_runs (project_id, created_at, id)",
    ),
    (
        "ix_scan_runs_project_status_created_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_scan_runs_project_status_created_id "
        "ON scan_runs (project_id, status, created_at, id)",
    ),
    (
        "ix_endpoints_run_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_endpoints_run_id ON endpoints (run_id, id)",
    ),
    (
        "ix_resources_run_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_resources_run_id ON resources (run_id, id)",
    ),
    (
        "ix_resources_run_endpoint_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_resources_run_endpoint_id "
        "ON resources (run_id, endpoint_id, id)",
    ),
    (
        "ix_items_run_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_items_run_id ON items (run_id, id)",
    ),
    (
        "ix_items_run_resource_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_items_run_resource_id "
        "ON items (run_id, resource_id, id)",
    ),
    (
        "ix_ingest_errors_run_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_ingest_errors_run_id ON ingest_errors (run_id, id)",
    ),
    (
        "ix_ingest_errors_run_severity_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_ingest_errors_run_severity_id "
        "ON ingest_errors (run_id, severity, id)",
    ),
    (
        "ix_audit_events_project_ts_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_audit_events_project_ts_id "
        "ON audit_events (project_id, ts, id)",
    ),
    (
        "ix_audit_events_project_object_ts_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_audit_events_project_object_ts_id "
        "ON audit_events (project_id, object_type, object_id, ts, id)",
    ),
    (
        "ix_audit_events_ts_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_audit_events_ts_id "
        "ON audit_events (ts DESC, id DESC)",
    ),
    (
        "ix_users_created_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_users_created_id "
        "ON users (created_at DESC, id DESC)",
    ),
    (
        "ix_api_tokens_created_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_api_tokens_created_id "
        "ON api_tokens (created_at DESC, id DESC)",
    ),
)

REPLACED_INDEXES = (
    (
        "ix_scan_runs_project_created",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_scan_runs_project_created "
        "ON scan_runs (project_id, created_at)",
    ),
    (
        "ix_resources_run_endpoint",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_resources_run_endpoint "
        "ON resources (run_id, endpoint_id)",
    ),
    (
        "ix_items_run_resource_path",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_items_run_resource_path "
        "ON items (run_id, resource_id, path)",
    ),
    (
        "ix_audit_events_project_ts",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_audit_events_project_ts "
        "ON audit_events (project_id, ts)",
    ),
)


def _index_validity(name: str) -> bool | None:
    return op.get_bind().execute(
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
    ).scalar_one_or_none()


def _ensure_valid_index(name: str, statement: str) -> None:
    validity = _index_validity(name)
    if validity is True:
        return
    if validity is False:
        # Interrupted concurrent builds leave an INVALID catalog entry. An
        # `IF NOT EXISTS` create would silently keep it and leave the query
        # path unindexed, so remove that entry before retrying.
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.execute(statement)


def upgrade() -> None:
    # These tables can be large on existing installations. Autocommit is
    # required by PostgreSQL for concurrent index creation and avoids blocking
    # ingestion writes for the duration of each build.
    with op.get_context().autocommit_block():
        for name, statement in INDEXES:
            _ensure_valid_index(name, statement)
        for name, _statement in REPLACED_INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, statement in REPLACED_INDEXES:
            _ensure_valid_index(name, statement)
        for name, _statement in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
