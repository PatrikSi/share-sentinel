import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.enums import AccessLevel, ErrorSeverity, ProjectRole, ResourceType, RunStatus, UITheme


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(sa.String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    is_sysadmin: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    ui_theme: Mapped[UITheme] = mapped_column(sa.Enum(UITheme, name="ui_theme"), nullable=False, server_default=UITheme.SYSTEM.value)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


class ProjectMember(Base):
    __tablename__ = "project_members"

    project_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[ProjectRole] = mapped_column(sa.Enum(ProjectRole, name="project_role"), nullable=False)


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    role: Mapped[ProjectRole] = mapped_column(sa.Enum(ProjectRole, name="token_role"), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    target_scope: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by_token_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, ForeignKey("api_tokens.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    status: Mapped[RunStatus] = mapped_column(sa.Enum(RunStatus, name="run_status"), nullable=False, server_default=RunStatus.PENDING_UPLOAD.value)
    artifact_key: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    artifact_sha256: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    artifact_size: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    artifact_content_type: Mapped[str | None] = mapped_column(sa.String(120), nullable=True)
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{\"endpoints\":0,\"resources\":0,\"items\":0,\"errors\":0}'::jsonb"))
    ingest_progress: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{\"line_offset\":0}'::jsonb"))


class Endpoint(Base):
    __tablename__ = "endpoints"
    __table_args__ = (
        UniqueConstraint("run_id", "endpoint_key", name="uq_endpoints_run_key"),
        Index("ix_endpoints_run_ip", "run_id", "ip"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, ForeignKey("scan_runs.id", ondelete="CASCADE"), nullable=False)
    endpoint_key: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    ip: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    hostname: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    domain: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    smb_dialect: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    smb_signing: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    auth_method: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)


class Resource(Base):
    __tablename__ = "resources"
    __table_args__ = (
        UniqueConstraint("run_id", "endpoint_id", "resource_type", "name", name="uq_resources_run_endpoint_type_name"),
        Index("ix_resources_run_endpoint", "run_id", "endpoint_id"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, ForeignKey("scan_runs.id", ondelete="CASCADE"), nullable=False)
    endpoint_id: Mapped[int] = mapped_column(sa.BigInteger, ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False)
    resource_type: Mapped[ResourceType] = mapped_column(sa.Enum(ResourceType, name="resource_type"), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    remark: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    access_level: Mapped[AccessLevel] = mapped_column(sa.Enum(AccessLevel, name="access_level"), nullable=False)


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("run_id", "resource_id", "path", name="uq_items_run_resource_path"),
        Index("ix_items_run_resource_path", "run_id", "resource_id", "path"),
        Index("ix_items_run_name", "run_id", "name"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, ForeignKey("scan_runs.id", ondelete="CASCADE"), nullable=False)
    resource_id: Mapped[int] = mapped_column(sa.BigInteger, ForeignKey("resources.id", ondelete="CASCADE"), nullable=False)
    path: Mapped[str] = mapped_column(sa.Text, nullable=False)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    is_dir: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    mtime: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class IngestError(Base):
    __tablename__ = "ingest_errors"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, ForeignKey("scan_runs.id", ondelete="CASCADE"), nullable=False)
    severity: Mapped[ErrorSeverity] = mapped_column(sa.Enum(ErrorSeverity, name="error_severity"), nullable=False)
    code: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    message: Mapped[str] = mapped_column(sa.Text, nullable=False)
    endpoint_key: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    resource_name: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    path: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_project_ts", "project_id", "ts"),)

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    actor_token_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, ForeignKey("api_tokens.id", ondelete="SET NULL"), nullable=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    object_type: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    object_id: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))


Index("ix_scan_runs_project_created", ScanRun.project_id, ScanRun.created_at)
