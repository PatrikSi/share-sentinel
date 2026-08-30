"""preserve durable audit attribution snapshots

Revision ID: 0016_audit_attribution
Revises: 0015_monitoring_indexes
Create Date: 2026-08-30

The nullable columns are metadata-only additions on supported PostgreSQL
versions. Existing rows intentionally are not rewritten in one unbounded
upgrade transaction. The audit-row trigger fills new events immediately;
indexed parent preservation and the bounded legacy backfill follow in 0018.
"""

import sqlalchemy as sa
from alembic import op

revision = "0016_audit_attribution"
down_revision = "0015_monitoring_indexes"
branch_labels = None
depends_on = None


TRIGGER_FUNCTIONS = (
    """
    CREATE FUNCTION audit_events_capture_attribution() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.actor_user_id IS NOT NULL THEN
                NEW.actor_user_ref := NEW.actor_user_id;
                SELECT email INTO NEW.actor_email_snapshot
                FROM users WHERE id = NEW.actor_user_id;
            END IF;
            IF NEW.actor_token_id IS NOT NULL THEN
                NEW.actor_token_ref := NEW.actor_token_id;
                SELECT name INTO NEW.actor_token_name_snapshot
                FROM api_tokens WHERE id = NEW.actor_token_id;
            END IF;
            IF NEW.project_id IS NOT NULL THEN
                NEW.project_ref := NEW.project_id;
                SELECT name INTO NEW.project_name_snapshot
                FROM projects WHERE id = NEW.project_id;
            END IF;
            RETURN NEW;
        END IF;

        NEW.actor_user_ref := COALESCE(
            OLD.actor_user_ref,
            OLD.actor_user_id,
            NEW.actor_user_ref,
            NEW.actor_user_id
        );
        NEW.actor_email_snapshot := COALESCE(
            OLD.actor_email_snapshot,
            NEW.actor_email_snapshot
        );
        NEW.actor_token_ref := COALESCE(
            OLD.actor_token_ref,
            OLD.actor_token_id,
            NEW.actor_token_ref,
            NEW.actor_token_id
        );
        NEW.actor_token_name_snapshot := COALESCE(
            OLD.actor_token_name_snapshot,
            NEW.actor_token_name_snapshot
        );
        NEW.project_ref := COALESCE(
            OLD.project_ref,
            OLD.project_id,
            NEW.project_ref,
            NEW.project_id
        );
        NEW.project_name_snapshot := COALESCE(
            OLD.project_name_snapshot,
            NEW.project_name_snapshot
        );
        RETURN NEW;
    END;
    $$
    """,
)

TRIGGERS = (
    """
    CREATE TRIGGER trg_audit_events_capture_attribution
    BEFORE INSERT OR UPDATE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_capture_attribution()
    """,
)


def upgrade() -> None:
    op.add_column("audit_events", sa.Column("actor_user_ref", sa.Uuid(), nullable=True))
    op.add_column("audit_events", sa.Column("actor_email_snapshot", sa.String(length=320), nullable=True))
    op.add_column("audit_events", sa.Column("actor_token_ref", sa.Uuid(), nullable=True))
    op.add_column(
        "audit_events",
        sa.Column("actor_token_name_snapshot", sa.String(length=120), nullable=True),
    )
    op.add_column("audit_events", sa.Column("project_ref", sa.Uuid(), nullable=True))
    op.add_column("audit_events", sa.Column("project_name_snapshot", sa.String(length=255), nullable=True))

    for statement in TRIGGER_FUNCTIONS:
        op.execute(statement)
    for statement in TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_audit_events_capture_attribution ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS audit_events_capture_attribution()")

    op.drop_column("audit_events", "project_name_snapshot")
    op.drop_column("audit_events", "project_ref")
    op.drop_column("audit_events", "actor_token_name_snapshot")
    op.drop_column("audit_events", "actor_token_ref")
    op.drop_column("audit_events", "actor_email_snapshot")
    op.drop_column("audit_events", "actor_user_ref")
