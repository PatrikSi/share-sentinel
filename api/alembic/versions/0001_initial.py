"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-02-27
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    ui_theme = postgresql.ENUM("light", "dark", "system", name="ui_theme", create_type=False)
    project_role = postgresql.ENUM("admin", "operator", "viewer", name="project_role", create_type=False)
    token_role = postgresql.ENUM("admin", "operator", "viewer", name="token_role", create_type=False)
    run_status = postgresql.ENUM(
        "PENDING_UPLOAD",
        "UPLOADED",
        "INGESTING",
        "COMPLETE",
        "FAILED",
        name="run_status",
        create_type=False,
    )
    resource_type = postgresql.ENUM("smb_share", name="resource_type", create_type=False)
    access_level = postgresql.ENUM("no_access", "list_only", "readable", name="access_level", create_type=False)
    error_severity = postgresql.ENUM("warn", "error", name="error_severity", create_type=False)

    bind = op.get_bind()
    ui_theme.create(bind, checkfirst=True)
    project_role.create(bind, checkfirst=True)
    token_role.create(bind, checkfirst=True)
    run_status.create(bind, checkfirst=True)
    resource_type.create(bind, checkfirst=True)
    access_level.create(bind, checkfirst=True)
    error_severity.create(bind, checkfirst=True)
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_sysadmin", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("ui_theme", ui_theme, server_default="system", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )

    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "project_members",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", project_role, nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id", "user_id"),
    )

    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("role", token_role, nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_api_tokens_token_hash", "api_tokens", ["token_hash"], unique=False)

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_refresh_tokens_user_revoked", "refresh_tokens", ["user_id", "revoked_at"], unique=False)

    op.create_table(
        "scan_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("target_scope", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_token_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("status", run_status, server_default="PENDING_UPLOAD", nullable=False),
        sa.Column("artifact_key", sa.String(length=512), nullable=True),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=True),
        sa.Column("artifact_size", sa.BigInteger(), nullable=True),
        sa.Column("artifact_content_type", sa.String(length=120), nullable=True),
        sa.Column(
            "summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("jsonb_build_object('endpoints', 0, 'resources', 0, 'items', 0, 'errors', 0)"),
        ),
        sa.Column(
            "ingest_progress",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("jsonb_build_object('line_offset', 0)"),
        ),
        sa.ForeignKeyConstraint(["created_by_token_id"], ["api_tokens.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scan_runs_project_created", "scan_runs", ["project_id", "created_at"], unique=False)
    op.create_index("ix_scan_runs_status_created", "scan_runs", ["status", "created_at"], unique=False)

    op.create_table(
        "endpoints",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint_key", sa.String(length=255), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("hostname", sa.String(length=255), nullable=True),
        sa.Column("domain", sa.String(length=255), nullable=True),
        sa.Column("smb_dialect", sa.String(length=64), nullable=True),
        sa.Column("smb_signing", sa.String(length=64), nullable=True),
        sa.Column("auth_method", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["scan_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "endpoint_key", name="uq_endpoints_run_key"),
    )
    op.create_index("ix_endpoints_run_ip", "endpoints", ["run_id", "ip"], unique=False)

    op.create_table(
        "resources",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint_id", sa.BigInteger(), nullable=False),
        sa.Column("resource_type", resource_type, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column("access_level", access_level, nullable=False),
        sa.ForeignKeyConstraint(["endpoint_id"], ["endpoints.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["scan_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "endpoint_id", "resource_type", "name", name="uq_resources_run_endpoint_type_name"),
    )
    op.create_index("ix_resources_run_endpoint", "resources", ["run_id", "endpoint_id"], unique=False)

    op.create_table(
        "items",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("resource_id", sa.BigInteger(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("is_dir", sa.Boolean(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("mtime", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["resource_id"], ["resources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["scan_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "resource_id", "path", name="uq_items_run_resource_path"),
    )
    op.create_index("ix_items_run_resource_path", "items", ["run_id", "resource_id", "path"], unique=False)
    op.create_index("ix_items_run_name", "items", ["run_id", "name"], unique=False)
    op.execute("CREATE INDEX IF NOT EXISTS ix_items_name_trgm ON items USING GIN (name gin_trgm_ops)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_items_path_trgm ON items USING GIN (path gin_trgm_ops)")

    op.create_table(
        "ingest_errors",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("severity", error_severity, nullable=False),
        sa.Column("code", sa.String(length=128), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("endpoint_key", sa.String(length=255), nullable=True),
        sa.Column("resource_name", sa.String(length=255), nullable=True),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["scan_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("actor_token_id", sa.Uuid(), nullable=True),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=120), nullable=False),
        sa.Column("object_type", sa.String(length=80), nullable=False),
        sa.Column("object_id", sa.String(length=255), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.ForeignKeyConstraint(["actor_token_id"], ["api_tokens.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_project_ts", "audit_events", ["project_id", "ts"], unique=False)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_items_path_trgm")
    op.execute("DROP INDEX IF EXISTS ix_items_name_trgm")
    op.drop_index("ix_audit_events_project_ts", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("ingest_errors")
    op.drop_index("ix_items_run_name", table_name="items")
    op.drop_index("ix_items_run_resource_path", table_name="items")
    op.drop_table("items")
    op.drop_index("ix_resources_run_endpoint", table_name="resources")
    op.drop_table("resources")
    op.drop_index("ix_endpoints_run_ip", table_name="endpoints")
    op.drop_table("endpoints")
    op.drop_index("ix_scan_runs_status_created", table_name="scan_runs")
    op.drop_index("ix_scan_runs_project_created", table_name="scan_runs")
    op.drop_table("scan_runs")
    op.drop_index("ix_refresh_tokens_user_revoked", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_index("ix_api_tokens_token_hash", table_name="api_tokens")
    op.drop_table("api_tokens")
    op.drop_table("project_members")
    op.drop_table("projects")
    op.drop_table("users")

    sa.Enum(name="error_severity").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="access_level").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="resource_type").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="run_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="token_role").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="project_role").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="ui_theme").drop(op.get_bind(), checkfirst=True)
