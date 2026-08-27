import uuid
from datetime import datetime
from enum import Enum

import sqlalchemy as sa
from sqlalchemy import ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.enums import AccessLevel, ErrorSeverity, ProjectRole, ResourceType, RunStatus, UITheme


def value_enum(enum_cls: type[Enum], name: str) -> sa.Enum:
    return sa.Enum(
        enum_cls,
        name=name,
        values_callable=lambda enum_type: [member.value for member in enum_type],
        validate_strings=True,
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(sa.String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    session_version: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="1")
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    is_sysadmin: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    is_approved: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    approved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    ui_theme: Mapped[UITheme] = mapped_column(
        value_enum(UITheme, name="ui_theme"), nullable=False, server_default=UITheme.SYSTEM.value
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


Index("uq_projects_name_ci", sa.func.lower(Project.name), unique=True)


class ProjectMember(Base):
    __tablename__ = "project_members"

    project_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[ProjectRole] = mapped_column(value_enum(ProjectRole, name="project_role"), nullable=False)


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    role: Mapped[ProjectRole] = mapped_column(value_enum(ProjectRole, name="token_role"), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"))
    last_used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    target_scope: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by_token_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, ForeignKey("api_tokens.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    status: Mapped[RunStatus] = mapped_column(
        value_enum(RunStatus, name="run_status"), nullable=False, server_default=RunStatus.PENDING_UPLOAD.value
    )
    artifact_key: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    artifact_sha256: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    artifact_size: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    artifact_content_type: Mapped[str | None] = mapped_column(sa.String(120), nullable=True)
    summary: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=sa.text("jsonb_build_object('endpoints', 0, 'resources', 0, 'items', 0, 'errors', 0)"),
    )
    ingest_progress: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=sa.text("jsonb_build_object('line_offset', 0)"),
    )
    collection_context: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    )


class Endpoint(Base):
    __tablename__ = "endpoints"
    __table_args__ = (
        UniqueConstraint("run_id", "endpoint_key", name="uq_endpoints_run_key"),
        Index("ix_endpoints_run_ip", "run_id", "ip"),
        Index("ix_endpoints_run_id", "run_id", "id"),
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
    provider: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    provider_metadata: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    )


class Resource(Base):
    __tablename__ = "resources"
    __table_args__ = (
        Index(
            "uq_resources_run_endpoint_type_name_legacy",
            "run_id",
            "endpoint_id",
            "resource_type",
            "name",
            unique=True,
            postgresql_where=sa.text("provider_resource_id IS NULL"),
        ),
        Index(
            "uq_resources_run_endpoint_provider_id",
            "run_id",
            "endpoint_id",
            "resource_type",
            "provider_resource_id",
            unique=True,
            postgresql_where=sa.text("provider_resource_id IS NOT NULL"),
        ),
        Index("ix_resources_run_id", "run_id", "id"),
        Index("ix_resources_run_endpoint_id", "run_id", "endpoint_id", "id"),
        Index("ix_resources_run_provider_exposure_id", "run_id", "provider", "exposure", "id"),
        Index("ix_resources_run_identity_id", "run_id", "identity_key", "id"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, ForeignKey("scan_runs.id", ondelete="CASCADE"), nullable=False)
    endpoint_id: Mapped[int] = mapped_column(
        sa.BigInteger, ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False
    )
    resource_type: Mapped[ResourceType] = mapped_column(value_enum(ResourceType, name="resource_type"), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    remark: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    access_level: Mapped[AccessLevel] = mapped_column(value_enum(AccessLevel, name="access_level"), nullable=False)
    access_capabilities: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    provider: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    provider_resource_id: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    web_url: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    provider_metadata: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    )
    exposure: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    exposure_evidence: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    )
    identity_key: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    permission_summary: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    )


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (
        Index(
            "uq_items_run_resource_path_legacy",
            "run_id",
            "resource_id",
            "path",
            unique=True,
            postgresql_where=sa.text("provider_item_id IS NULL"),
        ),
        Index(
            "uq_items_run_resource_provider_id",
            "run_id",
            "resource_id",
            "provider_item_id",
            unique=True,
            postgresql_where=sa.text("provider_item_id IS NOT NULL"),
        ),
        Index("ix_items_run_name", "run_id", "name"),
        Index("ix_items_run_id", "run_id", "id"),
        Index("ix_items_run_resource_id", "run_id", "resource_id", "id"),
        Index("ix_items_run_provider_exposure_id", "run_id", "provider", "exposure", "id"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, ForeignKey("scan_runs.id", ondelete="CASCADE"), nullable=False)
    resource_id: Mapped[int] = mapped_column(
        sa.BigInteger, ForeignKey("resources.id", ondelete="CASCADE"), nullable=False
    )
    path: Mapped[str] = mapped_column(sa.Text, nullable=False)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    is_dir: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    allocation_size_bytes: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    mtime: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    accessed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    changed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    file_attributes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"))
    provider: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    provider_item_id: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    provider_parent_id: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    web_url: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    deleted: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    provider_metadata: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    )
    exposure: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    exposure_evidence: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    )
    identity_key: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    permission_summary: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    )


class PermissionAssessment(Base):
    __tablename__ = "permission_assessments"
    __table_args__ = (
        UniqueConstraint("run_id", "assessment_key", name="uq_permission_assessments_run_key"),
        UniqueConstraint(
            "run_id",
            "resource_id",
            "subject_key",
            "semantics",
            "permission_surface",
            name="uq_permission_assessments_subject_surface",
        ),
        Index("ix_permission_assessments_run_resource_id", "run_id", "resource_id", "id"),
        Index("ix_permission_assessments_run_item_id", "run_id", "item_id", "id"),
        Index("ix_permission_assessments_run_state_id", "run_id", "assessment_state", "id"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        ForeignKey("scan_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    resource_id: Mapped[int] = mapped_column(
        sa.BigInteger,
        ForeignKey("resources.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_id: Mapped[int | None] = mapped_column(
        sa.BigInteger,
        ForeignKey("items.id", ondelete="CASCADE"),
        nullable=True,
    )
    assessment_key: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    subject_kind: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    subject_key: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    subject_provider_id: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    subject_path: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    provider: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    semantics: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    permission_surface: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    method: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    assessment_state: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    selection_scope: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    selection_coverage: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    retrieval_coverage: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    provider_visibility: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    semantic_coverage: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    principal_resolution: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    effective_access_status: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    negative_conclusion_supported: Mapped[bool] = mapped_column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
    )
    entries_observed: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    entries_emitted: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    entries_omitted: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    unknown_entries: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    evidence_hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    entry_set_hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    limitations: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"))
    error_code: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    errors: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"))
    provider_details: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


class PermissionPrincipal(Base):
    __tablename__ = "permission_principals"
    __table_args__ = (
        UniqueConstraint("run_id", "provider", "principal_key", name="uq_permission_principals_run_key"),
        Index("ix_permission_principals_run_kind_id", "run_id", "kind", "id"),
        Index("ix_permission_principals_run_native_id", "run_id", "identifier_namespace", "native_id"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        ForeignKey("scan_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    principal_key: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    identifier_namespace: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    authority: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    native_id: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    display_name: Mapped[str | None] = mapped_column(sa.String(1024), nullable=True)
    login_name: Mapped[str | None] = mapped_column(sa.String(1024), nullable=True)
    email: Mapped[str | None] = mapped_column(sa.String(1024), nullable=True)
    resolution_state: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    resolution_source: Mapped[str | None] = mapped_column(sa.String(80), nullable=True)
    aliases: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


class PermissionEntry(Base):
    __tablename__ = "permission_entries"
    __table_args__ = (
        UniqueConstraint("assessment_id", "entry_key", name="uq_permission_entries_assessment_key"),
        Index("ix_permission_entries_run_assessment_id", "run_id", "assessment_id", "id"),
        Index("ix_permission_entries_run_principal_id", "run_id", "principal_id", "id"),
        Index("ix_permission_entries_assessment_effect_id", "assessment_id", "entry_effect", "id"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        ForeignKey("scan_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    assessment_id: Mapped[int] = mapped_column(
        sa.BigInteger,
        ForeignKey("permission_assessments.id", ondelete="CASCADE"),
        nullable=False,
    )
    principal_id: Mapped[int | None] = mapped_column(
        sa.BigInteger,
        ForeignKey("permission_principals.id", ondelete="SET NULL"),
        nullable=True,
    )
    entry_key: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    provider_entry_id: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    ordinal: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    entry_kind: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    entry_effect: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    normalized_rights: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"))
    inherited_state: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    expiration_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    evidence_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    provider_details: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


class RunComparison(Base):
    __tablename__ = "run_comparisons"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "baseline_run_id",
            "current_run_id",
            "algorithm_version",
            "options_hash",
            name="uq_run_comparisons_identity",
        ),
        Index("ix_run_comparisons_project_created_id", "project_id", "created_at", "id"),
        Index("ix_run_comparisons_state_heartbeat", "state", "heartbeat_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    baseline_run_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        ForeignKey("scan_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    current_run_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        ForeignKey("scan_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    algorithm_version: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    options_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    state: Mapped[str] = mapped_column(sa.String(24), nullable=False, server_default="queued")
    compatibility: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    progress: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    error_code: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_by_token_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid,
        ForeignKey("api_tokens.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class ComparisonResourceChange(Base):
    __tablename__ = "comparison_resource_changes"
    __table_args__ = (
        UniqueConstraint("comparison_id", "identity_key", name="uq_comparison_resource_changes_identity"),
        Index("ix_comparison_changes_type_impact_id", "comparison_id", "change_type", "impact_rank", "id"),
        Index("ix_comparison_changes_impact_id", "comparison_id", sa.desc("impact_rank"), sa.desc("id")),
        Index(
            "ix_comparison_changes_provider_impact_id",
            "comparison_id",
            "provider",
            sa.desc("impact_rank"),
            sa.desc("id"),
        ),
        Index(
            "ix_comparison_changes_search_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
        Index(
            "ix_comparison_changes_categories_gin",
            "change_categories",
            postgresql_using="gin",
        ),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    comparison_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        ForeignKey("run_comparisons.id", ondelete="CASCADE"),
        nullable=False,
    )
    identity_key: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    change_type: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    provider: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    resource_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    provider_resource_id: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    match_basis: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    match_quality: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    before_resource_id: Mapped[int | None] = mapped_column(
        sa.BigInteger,
        ForeignKey("resources.id", ondelete="SET NULL"),
        nullable=True,
    )
    after_resource_id: Mapped[int | None] = mapped_column(
        sa.BigInteger,
        ForeignKey("resources.id", ondelete="SET NULL"),
        nullable=True,
    )
    endpoint_key_before: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    endpoint_key_after: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    resource_name_before: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    resource_name_after: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    change_categories: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"))
    structural_state: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    access_state: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    content_state: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    access_interpretation: Mapped[str] = mapped_column(sa.Text, nullable=False)
    item_count_before: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    item_count_after: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    before_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    after_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    search_text: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="")
    impact_rank: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")


class IngestError(Base):
    __tablename__ = "ingest_errors"
    __table_args__ = (
        UniqueConstraint("run_id", "fingerprint", name="uq_ingest_errors_run_fingerprint"),
        Index("ix_ingest_errors_run_id", "run_id", "id"),
        Index("ix_ingest_errors_run_severity_id", "run_id", "severity", "id"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, ForeignKey("scan_runs.id", ondelete="CASCADE"), nullable=False)
    severity: Mapped[ErrorSeverity] = mapped_column(value_enum(ErrorSeverity, name="error_severity"), nullable=False)
    code: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    message: Mapped[str] = mapped_column(sa.Text, nullable=False)
    endpoint_key: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    resource_name: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    path: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    fingerprint: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_project_ts_id", "project_id", "ts", "id"),
        Index(
            "ix_audit_events_project_object_ts_id",
            "project_id",
            "object_type",
            "object_id",
            "ts",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_token_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, ForeignKey("api_tokens.id", ondelete="SET NULL"), nullable=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    object_type: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    object_id: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    metadata_json: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )


class SavedInvestigation(Base):
    __tablename__ = "saved_investigations"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    target_tab: Mapped[str] = mapped_column(sa.String(32), nullable=False, server_default="items")
    query_text: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="")
    definition_json: Mapped[dict] = mapped_column(
        "definition", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )


Index("ix_scan_runs_project_created_id", ScanRun.project_id, ScanRun.created_at, ScanRun.id)
Index(
    "ix_scan_runs_project_status_created_id",
    ScanRun.project_id,
    ScanRun.status,
    ScanRun.created_at,
    ScanRun.id,
)
Index("ix_scan_runs_status_created", ScanRun.status, ScanRun.created_at)
Index("ix_refresh_tokens_user_revoked", RefreshToken.user_id, RefreshToken.revoked_at)
Index("ix_users_approval_status", User.is_approved, User.created_at)
Index("ix_users_created_id", User.created_at.desc(), User.id.desc())
Index("ix_api_tokens_created_id", ApiToken.created_at.desc(), ApiToken.id.desc())
Index("ix_audit_events_ts_id", AuditEvent.ts.desc(), AuditEvent.id.desc())
Index("ix_saved_investigations_project_updated", SavedInvestigation.project_id, SavedInvestigation.updated_at)
