import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field

from app.enums import ProjectRole, RunStatus, UITheme


class UserOut(BaseModel):
    id: uuid.UUID
    email: EmailStr
    is_active: bool
    is_sysadmin: bool
    ui_theme: UITheme


class LoginIn(BaseModel):
    email: EmailStr
    password: str


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


class ApiTokenOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    role: ProjectRole
    last_used_at: datetime | None
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
