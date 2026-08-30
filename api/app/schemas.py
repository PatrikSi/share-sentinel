import json
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.enums import ErrorSeverity, ProjectRole, RunStatus, UITheme
from app.token_scopes import is_scope_allowed, normalize_token_scopes


class UserOut(BaseModel):
    id: uuid.UUID
    email: EmailStr
    is_active: bool
    is_sysadmin: bool
    is_approved: bool
    approved_at: datetime | None
    approved_by_user_id: uuid.UUID | None
    ui_theme: UITheme


class UserAdminOut(UserOut):
    created_at: datetime


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class RegistrationSettingsOut(BaseModel):
    allow_self_registration: bool
    password_min_length: int
    password_require_lowercase: bool
    password_require_uppercase: bool
    password_require_number: bool
    password_require_special: bool


class SecuritySettingsOut(BaseModel):
    allow_self_registration: bool
    auth_require_csrf: bool
    auth_cookie_secure: bool
    allow_never_expiring_api_tokens: bool
    password_min_length: int
    password_require_lowercase: bool
    password_require_uppercase: bool
    password_require_number: bool
    password_require_special: bool
    auth_login_max_attempts: int
    auth_login_window_seconds: int
    auth_login_lockout_seconds: int
    default_api_token_expiry_days: int
    rbac_enabled: bool
    mfa_enabled: bool
    sso_enabled: bool
    scim_enabled: bool
    password_history_enforced: bool
    session_idle_timeout_minutes: int | None


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class SessionOut(BaseModel):
    user: UserOut


class RefreshIn(BaseModel):
    refresh_token: str | None = Field(default=None, min_length=1, max_length=256)


class RefreshOut(BaseModel):
    ok: bool = True


class ApiTokenCreateIn(BaseModel):
    project_id: uuid.UUID
    name: str = Field(min_length=1, max_length=120)
    role: ProjectRole
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)
    scopes: list[str] = Field(default_factory=list)

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, value: list[str]) -> list[str]:
        normalized = normalize_token_scopes(value)
        invalid = [scope for scope in normalized if not is_scope_allowed(scope)]
        if invalid:
            raise ValueError(f"unsupported scopes: {', '.join(invalid)}")
        return normalized


class ApiTokenOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    role: ProjectRole
    scopes: list[str]
    last_used_at: datetime | None
    expires_at: datetime | None
    created_at: datetime
    revoked_at: datetime | None


class ApiTokenCreateOut(BaseModel):
    token: str
    token_meta: ApiTokenOut


class ProjectCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized


class ProjectOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime


class ProjectUpdateIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized


class ProjectDeleteIn(BaseModel):
    confirm_name: str = Field(min_length=1, max_length=255)


class SettingsProjectCatalogItemOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime
    member_count: int
    admin_count: int
    token_count: int
    active_token_count: int
    run_count: int
    artifact_count: int
    blocking_run_count: int
    has_blocking_runs: bool
    last_run_at: datetime | None


class SettingsProjectBlockingRunOut(BaseModel):
    id: uuid.UUID
    name: str
    status: RunStatus
    created_at: datetime


class SettingsProjectDetailOut(SettingsProjectCatalogItemOut):
    run_status_counts: dict[str, int]
    blocking_runs: list[SettingsProjectBlockingRunOut]


class SettingsProjectArtifactDeleteFailureOut(BaseModel):
    artifact_key: str
    error: str


class SettingsProjectDeleteOut(BaseModel):
    ok: bool = True
    project_id: uuid.UUID
    project_name: str
    deleted_run_count: int
    deleted_artifact_count: int
    artifact_delete_failures: list[SettingsProjectArtifactDeleteFailureOut]


class MemberAddIn(BaseModel):
    user_id: uuid.UUID
    role: ProjectRole


class MemberAddByEmailIn(BaseModel):
    email: EmailStr
    role: ProjectRole


class RunCreateIn(BaseModel):
    run_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    target_scope: dict[str, Any] = Field(default_factory=dict)

    @field_validator("target_scope")
    @classmethod
    def validate_target_scope(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("target_scope must contain JSON values") from exc
        if len(encoded) > 65_536:
            raise ValueError("target_scope must not exceed 64 KiB")
        nodes = 0

        def validate_node(node: Any, depth: int) -> None:
            nonlocal nodes
            nodes += 1
            if nodes > 4096:
                raise ValueError("target_scope contains too many values")
            if depth > 8:
                raise ValueError("target_scope nesting must not exceed 8 levels")
            if isinstance(node, dict):
                if len(node) > 256:
                    raise ValueError("target_scope objects must not exceed 256 fields")
                for key, item in node.items():
                    fingerprint = "".join(character for character in str(key).casefold() if character.isalnum())
                    if any(
                        stem in fingerprint
                        for stem in (
                            "password",
                            "passphrase",
                            "secret",
                            "token",
                            "credential",
                            "privatekey",
                            "clientsecret",
                            "accesskey",
                            "refreshtoken",
                            "saskey",
                        )
                    ):
                        raise ValueError("target_scope must not contain credentials or secret-labeled fields")
                    if len(str(key)) > 256:
                        raise ValueError("target_scope field names must not exceed 256 characters")
                    validate_node(item, depth + 1)
            elif isinstance(node, list):
                if len(node) > 512:
                    raise ValueError("target_scope lists must not exceed 512 values")
                for item in node:
                    validate_node(item, depth + 1)

        validate_node(value, 0)
        return value


class RunOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    source_id: uuid.UUID | None = None
    name: str
    description: str | None
    target_scope: dict[str, Any]
    created_at: datetime
    status: RunStatus
    artifact_size: int | None
    artifact_sha256: str | None
    artifact_content_type: str | None
    ingest_progress: dict[str, Any]
    summary: dict[str, Any]
    collection_context: dict[str, Any] = Field(default_factory=dict)


class IngestErrorOut(BaseModel):
    id: int
    severity: ErrorSeverity
    code: str
    message: str
    endpoint_key: str | None
    resource_name: str | None
    path: str | None
    created_at: datetime


class RunActivityEventOut(BaseModel):
    id: int
    ts: datetime
    action: str
    object_type: str
    object_id: str
    metadata: dict[str, Any]


class ComparisonCreateIn(BaseModel):
    baseline_run_id: uuid.UUID
    current_run_id: uuid.UUID

    @field_validator("current_run_id")
    @classmethod
    def validate_distinct_runs(cls, value: uuid.UUID, info) -> uuid.UUID:
        baseline = info.data.get("baseline_run_id")
        if baseline is not None and value == baseline:
            raise ValueError("baseline_run_id and current_run_id must be different")
        return value


class ComparisonOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    source_id: uuid.UUID | None = None
    baseline_run_id: uuid.UUID
    current_run_id: uuid.UUID
    baseline_run: dict[str, Any] | None = None
    current_run: dict[str, Any] | None = None
    algorithm_version: str
    algorithm_current: bool
    algorithm_warning: str | None = None
    trigger: str = "manual"
    state: str
    compatibility: dict[str, Any] = Field(default_factory=dict)
    progress: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, str] | None = None
    attempt_count: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    heartbeat_at: datetime | None
    next_retry_at: datetime | None


class ComparisonResourceChangeOut(BaseModel):
    id: int
    identity_key: str
    change_type: str
    provider: str
    resource_type: str
    provider_resource_id: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    change_categories: list[str]
    structural_state: str
    access_state: str
    content_state: str
    access_interpretation: str
    match: dict[str, str]
    item_changes: dict[str, Any]
    impact_rank: int


class ComparisonItemChangeOut(BaseModel):
    id: int
    resource_change_id: int
    identity_key: str
    change_type: str
    provider: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    change_categories: list[str]
    evidence_state: str
    limitations: list[str]
    match: dict[str, str]
    impact_rank: int


class CollectionSourceUpdateIn(BaseModel):
    expected_display_name: str = Field(min_length=1, max_length=255)
    expected_enabled: bool
    expected_current_interval_seconds: int | None = Field(ge=300, le=31_536_000)
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    enabled: bool | None = None
    expected_interval_seconds: int | None = Field(default=None, ge=300, le=31_536_000)

    @field_validator("display_name", "expected_display_name")
    @classmethod
    def normalize_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("display_name must not be blank")
        return normalized


class FindingUpdateIn(BaseModel):
    status: str | None = Field(default=None, max_length=24)
    assignee_user_id: uuid.UUID | None = None
    accepted_risk_expires_at: datetime | None = None
    note: str | None = Field(default=None, max_length=4000)
    revision: int = Field(ge=1)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if normalized not in {"open", "acknowledged", "accepted_risk", "resolved"}:
            raise ValueError("unsupported finding status")
        return normalized


class FindingBulkUpdateIn(BaseModel):
    finding_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    expected_revisions: dict[uuid.UUID, int] = Field(min_length=1, max_length=100)
    status: str | None = Field(default=None, max_length=24)
    assignee_user_id: uuid.UUID | None = None
    accepted_risk_expires_at: datetime | None = None
    note: str | None = Field(default=None, max_length=4000)

    @field_validator("finding_ids")
    @classmethod
    def unique_finding_ids(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(set(value)) != len(value):
            raise ValueError("finding_ids must not contain duplicates")
        return value

    @model_validator(mode="after")
    def revisions_cover_findings(self):
        if set(self.expected_revisions) != set(self.finding_ids):
            raise ValueError("expected_revisions must contain exactly one revision for every finding_id")
        if any(revision < 1 for revision in self.expected_revisions.values()):
            raise ValueError("expected revisions must be positive integers")
        return self

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if normalized not in {"open", "acknowledged", "accepted_risk", "resolved"}:
            raise ValueError("unsupported finding status")
        return normalized


class AccessEvidenceOut(BaseModel):
    resource: dict[str, Any]
    overall: dict[str, Any]
    assessments: list[dict[str, Any]]
    provenance: dict[str, Any]


class ThemeUpdateIn(BaseModel):
    ui_theme: UITheme


class UserCreateIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    is_active: bool = True
    is_sysadmin: bool = False
    is_approved: bool = True
    add_to_all_projects: bool = False
    all_projects_role: ProjectRole = ProjectRole.VIEWER


class UserUpdateIn(BaseModel):
    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=1, max_length=256)
    is_active: bool | None = None
    is_sysadmin: bool | None = None
    is_approved: bool | None = None


class UserApprovalIn(BaseModel):
    is_approved: bool


class UserAssignAllProjectsIn(BaseModel):
    role: ProjectRole = ProjectRole.VIEWER
    overwrite_existing: bool = False


class ApiTokenAdminOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    user_email: str
    project_id: uuid.UUID
    project_name: str
    name: str
    role: ProjectRole
    scopes: list[str]
    last_used_at: datetime | None
    expires_at: datetime | None
    created_at: datetime
    revoked_at: datetime | None


class ApiTokenAdminCreateIn(BaseModel):
    user_id: uuid.UUID
    project_id: uuid.UUID
    name: str = Field(min_length=1, max_length=120)
    role: ProjectRole
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)
    scopes: list[str] = Field(default_factory=list)

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, value: list[str]) -> list[str]:
        normalized = normalize_token_scopes(value)
        invalid = [scope for scope in normalized if not is_scope_allowed(scope)]
        if invalid:
            raise ValueError(f"unsupported scopes: {', '.join(invalid)}")
        return normalized


class ApiTokenAdminUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    role: ProjectRole | None = None
    scopes: list[str] | None = None
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)
    never_expires: bool = False

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        normalized = normalize_token_scopes(value)
        invalid = [scope for scope in normalized if not is_scope_allowed(scope)]
        if invalid:
            raise ValueError(f"unsupported scopes: {', '.join(invalid)}")
        return normalized


class ApiTokenAdminCreateOut(BaseModel):
    token: str
    token_meta: ApiTokenAdminOut


class AuditEventOut(BaseModel):
    id: int
    ts: datetime
    actor_user_id: uuid.UUID | None
    actor_email: str | None
    actor_token_id: uuid.UUID | None
    actor_token_name: str | None
    project_id: uuid.UUID | None
    project_name: str | None
    action: str
    object_type: str
    object_id: str
    metadata: dict[str, Any]


class ProjectMembershipOut(BaseModel):
    project_id: uuid.UUID
    project_name: str
    user_id: uuid.UUID
    user_email: str
    role: ProjectRole


class ProjectMembershipUpsertIn(BaseModel):
    project_id: uuid.UUID
    user_id: uuid.UUID
    role: ProjectRole


class SavedInvestigationIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    target_tab: str = Field(default="items", min_length=1, max_length=32)
    query_text: str = Field(default="", max_length=4000)
    definition: dict[str, Any] = Field(default_factory=dict)

    @field_validator("target_tab")
    @classmethod
    def validate_target_tab(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"items", "resources", "endpoints"}:
            raise ValueError("target_tab must be one of: items, resources, endpoints")
        return normalized


class SavedInvestigationUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    target_tab: str | None = Field(default=None, min_length=1, max_length=32)
    query_text: str | None = Field(default=None, max_length=4000)
    definition: dict[str, Any] | None = None

    @field_validator("target_tab")
    @classmethod
    def validate_target_tab(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if normalized not in {"items", "resources", "endpoints"}:
            raise ValueError("target_tab must be one of: items, resources, endpoints")
        return normalized


class SavedInvestigationOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    created_by_user_id: uuid.UUID | None
    name: str
    description: str | None
    target_tab: str
    query_text: str
    definition: dict[str, Any]
    created_at: datetime
    updated_at: datetime
