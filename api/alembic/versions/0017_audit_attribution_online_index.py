"""index durable audit project references online

Revision ID: 0017_audit_attr_index
Revises: 0016_audit_attribution
Create Date: 2026-08-30
"""

import sqlalchemy as sa
from alembic import op

revision = "0017_audit_attr_index"
down_revision = "0016_audit_attribution"
branch_labels = None
depends_on = None

INDEXES = (
    (
        "ix_audit_events_project_ref_ts_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_audit_events_project_ref_ts_id "
        "ON audit_events (project_ref, ts, id)",
    ),
    (
        "ix_audit_events_actor_user_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_audit_events_actor_user_id "
        "ON audit_events (actor_user_id) WHERE actor_user_id IS NOT NULL",
    ),
    (
        "ix_audit_events_actor_token_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_audit_events_actor_token_id "
        "ON audit_events (actor_token_id) WHERE actor_token_id IS NOT NULL",
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


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, statement in INDEXES:
            _ensure_valid_index(name, statement)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _statement in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
