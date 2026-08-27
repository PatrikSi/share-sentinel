"""add online comparison and identity indexes

Revision ID: 0013_comparison_indexes
Revises: 0012_permission_evidence
Create Date: 2026-08-27

This revision contains only idempotent concurrent index operations. Keeping it
separate prevents Alembic's required autocommit boundary from partially
committing the transactional schema revision that creates the evidence model.
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_comparison_indexes"
down_revision = "0012_permission_evidence"
branch_labels = None
depends_on = None

INDEXES = (
    (
        "ix_resources_run_identity_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_resources_run_identity_id ON resources (run_id, identity_key, id)",
    ),
    (
        "ix_comparison_changes_impact_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_comparison_changes_impact_id "
        "ON comparison_resource_changes (comparison_id, impact_rank DESC, id DESC)",
    ),
    (
        "ix_comparison_changes_provider_impact_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_comparison_changes_provider_impact_id "
        "ON comparison_resource_changes (comparison_id, provider, impact_rank DESC, id DESC)",
    ),
    (
        "ix_comparison_changes_search_trgm",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_comparison_changes_search_trgm "
        "ON comparison_resource_changes USING GIN (search_text gin_trgm_ops)",
    ),
    (
        "ix_comparison_changes_categories_gin",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_comparison_changes_categories_gin "
        "ON comparison_resource_changes USING GIN (change_categories)",
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
