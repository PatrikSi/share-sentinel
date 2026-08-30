"""backfill durable audit attribution in bounded transactions

Revision ID: 0018_audit_attr_backfill
Revises: 0017_audit_attr_index
Create Date: 2026-08-30

The parent lookup indexes are already valid when this migration starts.
Parent triggers protect label changes and deletes while the restart-safe
backfill updates legacy rows in small autocommitted batches. Rows orphaned
before 0016 cannot be reconstructed and intentionally remain null.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "0018_audit_attr_backfill"
down_revision = "0017_audit_attr_index"
branch_labels = None
depends_on = None

BATCH_SIZE = 5_000

PARENT_TRIGGER_FUNCTIONS = (
    """
    CREATE OR REPLACE FUNCTION projects_preserve_audit_attribution() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        UPDATE audit_events
        SET project_ref = COALESCE(project_ref, OLD.id),
            project_name_snapshot = COALESCE(project_name_snapshot, OLD.name)
        WHERE project_id = OLD.id
          AND (project_ref IS NULL OR project_name_snapshot IS NULL);
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION users_preserve_audit_attribution() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        UPDATE audit_events
        SET actor_user_ref = COALESCE(actor_user_ref, OLD.id),
            actor_email_snapshot = COALESCE(actor_email_snapshot, OLD.email)
        WHERE actor_user_id = OLD.id
          AND (actor_user_ref IS NULL OR actor_email_snapshot IS NULL);
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION api_tokens_preserve_audit_attribution() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        UPDATE audit_events
        SET actor_token_ref = COALESCE(actor_token_ref, OLD.id),
            actor_token_name_snapshot = COALESCE(actor_token_name_snapshot, OLD.name)
        WHERE actor_token_id = OLD.id
          AND (actor_token_ref IS NULL OR actor_token_name_snapshot IS NULL);
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END;
    $$
    """,
)

PARENT_TRIGGER_DROPS = (
    "DROP TRIGGER IF EXISTS trg_projects_preserve_audit_name_update ON projects",
    "DROP TRIGGER IF EXISTS trg_projects_preserve_audit_name_delete ON projects",
    "DROP TRIGGER IF EXISTS trg_users_preserve_audit_email_update ON users",
    "DROP TRIGGER IF EXISTS trg_users_preserve_audit_email_delete ON users",
    "DROP TRIGGER IF EXISTS trg_api_tokens_preserve_audit_name_update ON api_tokens",
    "DROP TRIGGER IF EXISTS trg_api_tokens_preserve_audit_name_delete ON api_tokens",
)

PARENT_TRIGGERS = (
    """
    CREATE TRIGGER trg_projects_preserve_audit_name_update
    BEFORE UPDATE OF name ON projects
    FOR EACH ROW EXECUTE FUNCTION projects_preserve_audit_attribution()
    """,
    """
    CREATE TRIGGER trg_projects_preserve_audit_name_delete
    BEFORE DELETE ON projects
    FOR EACH ROW EXECUTE FUNCTION projects_preserve_audit_attribution()
    """,
    """
    CREATE TRIGGER trg_users_preserve_audit_email_update
    BEFORE UPDATE OF email ON users
    FOR EACH ROW EXECUTE FUNCTION users_preserve_audit_attribution()
    """,
    """
    CREATE TRIGGER trg_users_preserve_audit_email_delete
    BEFORE DELETE ON users
    FOR EACH ROW EXECUTE FUNCTION users_preserve_audit_attribution()
    """,
    """
    CREATE TRIGGER trg_api_tokens_preserve_audit_name_update
    BEFORE UPDATE OF name ON api_tokens
    FOR EACH ROW EXECUTE FUNCTION api_tokens_preserve_audit_attribution()
    """,
    """
    CREATE TRIGGER trg_api_tokens_preserve_audit_name_delete
    BEFORE DELETE ON api_tokens
    FOR EACH ROW EXECUTE FUNCTION api_tokens_preserve_audit_attribution()
    """,
)

BACKFILL_STATEMENTS = (
    """
    WITH batch AS MATERIALIZED (
        SELECT audit.id, projects.name AS snapshot_label
        FROM audit_events AS audit
        JOIN projects ON projects.id = audit.project_id
        WHERE audit.id > :last_id
          AND (audit.project_ref IS NULL OR audit.project_name_snapshot IS NULL)
        ORDER BY audit.id
        LIMIT :batch_size
    ), updated AS (
        UPDATE audit_events AS audit
        SET project_ref = COALESCE(audit.project_ref, audit.project_id),
            project_name_snapshot = COALESCE(audit.project_name_snapshot, batch.snapshot_label)
        FROM batch
        WHERE audit.id = batch.id
        RETURNING audit.id
    )
    SELECT count(*) AS row_count, max(id) AS max_id FROM updated
    """,
    """
    WITH batch AS MATERIALIZED (
        SELECT audit.id, users.email AS snapshot_label
        FROM audit_events AS audit
        JOIN users ON users.id = audit.actor_user_id
        WHERE audit.id > :last_id
          AND (audit.actor_user_ref IS NULL OR audit.actor_email_snapshot IS NULL)
        ORDER BY audit.id
        LIMIT :batch_size
    ), updated AS (
        UPDATE audit_events AS audit
        SET actor_user_ref = COALESCE(audit.actor_user_ref, audit.actor_user_id),
            actor_email_snapshot = COALESCE(audit.actor_email_snapshot, batch.snapshot_label)
        FROM batch
        WHERE audit.id = batch.id
        RETURNING audit.id
    )
    SELECT count(*) AS row_count, max(id) AS max_id FROM updated
    """,
    """
    WITH batch AS MATERIALIZED (
        SELECT audit.id, api_tokens.name AS snapshot_label
        FROM audit_events AS audit
        JOIN api_tokens ON api_tokens.id = audit.actor_token_id
        WHERE audit.id > :last_id
          AND (audit.actor_token_ref IS NULL OR audit.actor_token_name_snapshot IS NULL)
        ORDER BY audit.id
        LIMIT :batch_size
    ), updated AS (
        UPDATE audit_events AS audit
        SET actor_token_ref = COALESCE(audit.actor_token_ref, audit.actor_token_id),
            actor_token_name_snapshot = COALESCE(
                audit.actor_token_name_snapshot,
                batch.snapshot_label
            )
        FROM batch
        WHERE audit.id = batch.id
        RETURNING audit.id
    )
    SELECT count(*) AS row_count, max(id) AS max_id FROM updated
    """,
)


def _backfill_attribution(
    connection: Connection,
    statement: str,
    *,
    batch_size: int = BATCH_SIZE,
) -> None:
    last_id = 0
    while True:
        row_count, max_id = connection.execute(
            sa.text(statement),
            {"last_id": last_id, "batch_size": batch_size},
        ).one()
        if not row_count:
            return
        last_id = int(max_id)


def upgrade() -> None:
    for statement in PARENT_TRIGGER_DROPS:
        op.execute(statement)
    for statement in PARENT_TRIGGER_FUNCTIONS:
        op.execute(statement)
    for statement in PARENT_TRIGGERS:
        op.execute(statement)

    with op.get_context().autocommit_block():
        connection = op.get_bind()
        for statement in BACKFILL_STATEMENTS:
            _backfill_attribution(connection, statement)


def downgrade() -> None:
    for statement in reversed(PARENT_TRIGGER_DROPS):
        op.execute(statement)
    op.execute("DROP FUNCTION IF EXISTS api_tokens_preserve_audit_attribution()")
    op.execute("DROP FUNCTION IF EXISTS users_preserve_audit_attribution()")
    op.execute("DROP FUNCTION IF EXISTS projects_preserve_audit_attribution()")
