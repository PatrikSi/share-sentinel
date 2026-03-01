import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.deps import AuthContext
from app.enums import ProjectRole
from app.models import ProjectMember
from app.routers import users as users_router


class _ExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, projects, memberships):
        self.projects = projects
        self.memberships = memberships
        self.added = []

    def execute(self, _stmt):
        return _ExecuteResult([SimpleNamespace(id=project_id) for project_id in self.projects])

    def get(self, model, key):
        if model is ProjectMember and isinstance(key, dict):
            return self.memberships.get((key["project_id"], key["user_id"]))
        return None

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, ProjectMember):
            self.memberships[(obj.project_id, obj.user_id)] = obj


def test_assign_user_to_all_projects_creates_missing_memberships() -> None:
    user_id = uuid.uuid4()
    projects = [uuid.uuid4(), uuid.uuid4()]
    fake_db = _FakeDb(projects=projects, memberships={})

    assigned = users_router._assign_user_to_all_projects(fake_db, user_id, ProjectRole.VIEWER, overwrite_existing=False)

    assert assigned == 2
    assert len(fake_db.added) == 2
    assert all(isinstance(row, ProjectMember) for row in fake_db.added)


def test_assign_user_to_all_projects_overwrite_updates_existing() -> None:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    existing = ProjectMember(project_id=project_id, user_id=user_id, role=ProjectRole.VIEWER)
    fake_db = _FakeDb(projects=[project_id], memberships={(project_id, user_id): existing})

    assigned = users_router._assign_user_to_all_projects(fake_db, user_id, ProjectRole.ADMIN, overwrite_existing=True)

    assert assigned == 1
    assert existing.role == ProjectRole.ADMIN


def test_assign_user_to_all_projects_without_overwrite_keeps_existing() -> None:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    existing = ProjectMember(project_id=project_id, user_id=user_id, role=ProjectRole.OPERATOR)
    fake_db = _FakeDb(projects=[project_id], memberships={(project_id, user_id): existing})

    assigned = users_router._assign_user_to_all_projects(fake_db, user_id, ProjectRole.ADMIN, overwrite_existing=False)

    assert assigned == 0
    assert existing.role == ProjectRole.OPERATOR


def test_assign_user_to_all_projects_overwrite_keeps_last_project_admin(monkeypatch) -> None:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    existing = ProjectMember(project_id=project_id, user_id=user_id, role=ProjectRole.ADMIN)
    fake_db = _FakeDb(projects=[project_id], memberships={(project_id, user_id): existing})

    monkeypatch.setattr(users_router, "_count_project_admins", lambda *_args, **_kwargs: 0)
    assigned = users_router._assign_user_to_all_projects(fake_db, user_id, ProjectRole.VIEWER, overwrite_existing=True)

    assert assigned == 0
    assert existing.role == ProjectRole.ADMIN


def test_assign_user_to_all_projects_overwrite_demotes_admin_when_other_admin_exists(monkeypatch) -> None:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    existing = ProjectMember(project_id=project_id, user_id=user_id, role=ProjectRole.ADMIN)
    fake_db = _FakeDb(projects=[project_id], memberships={(project_id, user_id): existing})

    monkeypatch.setattr(users_router, "_count_project_admins", lambda *_args, **_kwargs: 1)
    assigned = users_router._assign_user_to_all_projects(fake_db, user_id, ProjectRole.VIEWER, overwrite_existing=True)

    assert assigned == 1
    assert existing.role == ProjectRole.VIEWER


def test_require_member_write_scope_if_token_rejects_missing_scope() -> None:
    with pytest.raises(HTTPException) as exc:
        users_router._require_member_write_scope_if_token(
            AuthContext(
                user_id=uuid.uuid4(),
                token_id=uuid.uuid4(),
                token_project_id=uuid.uuid4(),
                token_role=ProjectRole.ADMIN,
                token_scopes=["write:users"],
            )
        )
    assert exc.value.status_code == 403
