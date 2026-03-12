import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.enums import ProjectRole, RunStatus, UITheme
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


class TokenPairOut(BaseModel):
    access_token: str
    refresh_token: str
    csrf_token: str | None = None
    user: UserOut


class RefreshIn(BaseModel):
    refresh_token: str


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


class MemberAddIn(BaseModel):
    user_id: uuid.UUID
    role: ProjectRole


class MemberAddByEmailIn(BaseModel):
    email: EmailStr
    role: ProjectRole


class RunCreateIn(BaseModel):
    run_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    target_scope: dict[str, Any] = Field(default_factory=dict)


class RunOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    description: str | None
    target_scope: dict[str, Any]
    created_at: datetime
    status: RunStatus
    artifact_size: int | None
    summary: dict[str, Any]


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
