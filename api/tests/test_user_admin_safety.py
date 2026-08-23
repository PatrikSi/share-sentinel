import uuid
from types import SimpleNamespace

import pytest
from app.routers import users as users_router
from fastapi import HTTPException


def _target_user(*, is_active: bool = True, is_approved: bool = True, is_sysadmin: bool = True):
    return SimpleNamespace(
        id=uuid.uuid4(),
        is_active=is_active,
        is_approved=is_approved,
        is_sysadmin=is_sysadmin,
    )


def test_rejects_self_disable() -> None:
    user = _target_user()
    with pytest.raises(HTTPException) as exc:
        users_router._enforce_admin_safety(
            db=SimpleNamespace(),
            actor_user_id=user.id,
            target_user=user,
            next_is_active=False,
            next_is_approved=True,
            next_is_sysadmin=True,
        )
    assert exc.value.status_code == 400
    assert "disable your own account" in str(exc.value.detail)


def test_rejects_self_unapprove() -> None:
    user = _target_user()
    with pytest.raises(HTTPException) as exc:
        users_router._enforce_admin_safety(
            db=SimpleNamespace(),
            actor_user_id=user.id,
            target_user=user,
            next_is_active=True,
            next_is_approved=False,
            next_is_sysadmin=True,
        )
    assert exc.value.status_code == 400
    assert "unapprove your own account" in str(exc.value.detail)


def test_rejects_self_sysadmin_removal() -> None:
    user = _target_user()
    with pytest.raises(HTTPException) as exc:
        users_router._enforce_admin_safety(
            db=SimpleNamespace(),
            actor_user_id=user.id,
            target_user=user,
            next_is_active=True,
            next_is_approved=True,
            next_is_sysadmin=False,
        )
    assert exc.value.status_code == 400
    assert "remove your own sysadmin access" in str(exc.value.detail)


def test_rejects_removing_last_active_sysadmin(monkeypatch) -> None:
    target = _target_user(is_active=True, is_approved=True, is_sysadmin=True)
    monkeypatch.setattr(users_router, "lock_sysadmin_guard", lambda _db: None)
    monkeypatch.setattr(users_router, "_count_active_approved_sysadmins", lambda _db, exclude_user_id=None: 0)
    with pytest.raises(HTTPException) as exc:
        users_router._enforce_admin_safety(
            db=SimpleNamespace(),
            actor_user_id=uuid.uuid4(),
            target_user=target,
            next_is_active=False,
            next_is_approved=True,
            next_is_sysadmin=True,
        )
    assert exc.value.status_code == 400
    assert "at least one active approved sysadmin must remain" in str(exc.value.detail)


def test_allows_admin_update_when_other_sysadmins_exist(monkeypatch) -> None:
    target = _target_user(is_active=True, is_approved=True, is_sysadmin=True)
    monkeypatch.setattr(users_router, "lock_sysadmin_guard", lambda _db: None)
    monkeypatch.setattr(users_router, "_count_active_approved_sysadmins", lambda _db, exclude_user_id=None: 2)
    users_router._enforce_admin_safety(
        db=SimpleNamespace(),
        actor_user_id=uuid.uuid4(),
        target_user=target,
        next_is_active=False,
        next_is_approved=True,
        next_is_sysadmin=True,
    )
