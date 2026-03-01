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


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)


class RegistrationSettingsOut(BaseModel):
    allow_self_registration: bool


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=12, max_length=256)


class TokenPairOut(BaseModel):
    access_token: str
    refresh_token: str
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
    password: str = Field(min_length=12, max_length=256)
    is_active: bool = True
    is_sysadmin: bool = False
    is_approved: bool = True


class UserUpdateIn(BaseModel):
    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=12, max_length=256)
    is_active: bool | None = None
    is_sysadmin: bool | None = None
    is_approved: bool | None = None


class UserApprovalIn(BaseModel):
    is_approved: bool
