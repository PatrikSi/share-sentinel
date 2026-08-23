import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import (
    AuthContext,
    get_auth_context,
    request_meta,
    require_project_role,
    require_sysadmin,
    require_token_scopes,
)
from app.enums import ProjectRole
from app.locking import lock_project_admin_guard
from app.models import Project, ProjectMember, User
from app.schemas import MemberAddByEmailIn, MemberAddIn, ProjectCreateIn, ProjectOut
from app.services.audit import write_audit_event
from app.token_scopes import SCOPE_READ_MEMBERS, SCOPE_READ_PROJECTS, SCOPE_WRITE_MEMBERS, SCOPE_WRITE_PROJECTS

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=ProjectOut)
def create_project(
    payload: ProjectCreateIn,
    request: Request,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_PROJECTS)),
    admin: User = Depends(require_sysadmin),
):
    project = Project(name=payload.name)
    db.add(project)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="project name already exists") from exc

    membership = ProjectMember(project_id=project.id, user_id=admin.id, role=ProjectRole.ADMIN)
    db.add(membership)

    write_audit_event(
        db,
        action="PROJECT_CREATED",
        object_type="project",
        object_id=str(project.id),
        actor_user_id=admin.id,
        project_id=project.id,
        metadata=request_meta(request),
    )

    db.commit()
    db.refresh(project)
    return ProjectOut(id=project.id, name=project.name, created_at=project.created_at)


@router.get("", response_model=list[ProjectOut])
def list_projects(
    request: Request,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_PROJECTS)),
    auth: AuthContext = Depends(get_auth_context),
):
    if auth.token_id:
        project = db.get(Project, auth.token_project_id)
        if project:
            write_audit_event(
                db,
                action="PROJECTS_LISTED",
                object_type="user",
                object_id=str(auth.user_id or auth.token_id),
                actor_user_id=auth.user_id,
                actor_token_id=auth.token_id,
                project_id=project.id,
                metadata=request_meta(request),
            )
            db.commit()
        return [ProjectOut(id=project.id, name=project.name, created_at=project.created_at)] if project else []

    if auth.user_id is None:
        return []

    stmt = (
        select(Project)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(ProjectMember.user_id == auth.user_id)
        .order_by(Project.created_at.desc())
    )
    projects = db.execute(stmt).scalars().all()
    write_audit_event(
        db,
        action="PROJECTS_LISTED",
        object_type="user",
        object_id=str(auth.user_id),
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        metadata={**request_meta(request), "result_count": len(projects)},
    )
    db.commit()
    return [ProjectOut(id=p.id, name=p.name, created_at=p.created_at) for p in projects]


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_PROJECTS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    write_audit_event(
        db,
        action="PROJECT_VIEWED",
        object_type="project",
        object_id=str(project_id),
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata=request_meta(request),
    )
    db.commit()
    return ProjectOut(id=project.id, name=project.name, created_at=project.created_at)


@router.get("/{project_id}/my-role")
def get_my_role(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_PROJECTS)),
    auth: AuthContext = Depends(get_auth_context),
):
    role = require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    return {"role": role.value}


@router.post("/{project_id}/members")
def add_member(
    project_id: uuid.UUID,
    payload: MemberAddIn,
    request: Request,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_MEMBERS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.ADMIN, auth, db)

    user = db.get(User, payload.user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    existing = db.get(ProjectMember, {"project_id": project_id, "user_id": payload.user_id})
    if existing:
        if existing.role == ProjectRole.ADMIN and payload.role != ProjectRole.ADMIN:
            if _count_project_admins(db, project_id, exclude_user_id=payload.user_id) < 1:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="at least one project admin must remain",
                )
        existing.role = payload.role
        db.add(existing)
    else:
        db.add(ProjectMember(project_id=project_id, user_id=payload.user_id, role=payload.role))

    write_audit_event(
        db,
        action="PROJECT_MEMBER_UPSERT",
        object_type="project_member",
        object_id=f"{project_id}:{payload.user_id}",
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata={**request_meta(request), "role": payload.role.value},
    )
    db.commit()
    return {"ok": True}


@router.post("/{project_id}/members/by-email")
def add_member_by_email(
    project_id: uuid.UUID,
    payload: MemberAddByEmailIn,
    request: Request,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_MEMBERS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.ADMIN, auth, db)

    stmt = select(User).where(func.lower(User.email) == payload.email.lower())
    user = db.execute(stmt).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    existing = db.get(ProjectMember, {"project_id": project_id, "user_id": user.id})
    if existing:
        if existing.role == ProjectRole.ADMIN and payload.role != ProjectRole.ADMIN:
            if _count_project_admins(db, project_id, exclude_user_id=user.id) < 1:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="at least one project admin must remain",
                )
        existing.role = payload.role
        db.add(existing)
    else:
        db.add(ProjectMember(project_id=project_id, user_id=user.id, role=payload.role))

    write_audit_event(
        db,
        action="PROJECT_MEMBER_UPSERT",
        object_type="project_member",
        object_id=f"{project_id}:{user.id}",
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata={**request_meta(request), "role": payload.role.value, "email": payload.email.lower()},
    )
    db.commit()
    return {"ok": True, "user_id": str(user.id), "email": user.email}


@router.get("/{project_id}/members")
def list_members(
    project_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_MEMBERS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.ADMIN, auth, db)

    stmt = (
        select(ProjectMember.user_id, User.email, ProjectMember.role)
        .join(User, User.id == ProjectMember.user_id)
        .where(ProjectMember.project_id == project_id)
        .order_by(User.email.asc())
    )
    rows = db.execute(stmt).all()
    write_audit_event(
        db,
        action="PROJECT_MEMBERS_VIEWED",
        object_type="project",
        object_id=str(project_id),
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata={**request_meta(request), "result_count": len(rows)},
    )
    db.commit()
    return {
        "items": [
            {
                "user_id": str(row.user_id),
                "email": row.email,
                "role": row.role.value if hasattr(row.role, "value") else row.role,
            }
            for row in rows
        ]
    }


@router.delete("/{project_id}/members/{user_id}")
def remove_member(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_MEMBERS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.ADMIN, auth, db)

    membership = db.get(ProjectMember, {"project_id": project_id, "user_id": user_id})
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    if membership.role == ProjectRole.ADMIN:
        if _count_project_admins(db, project_id, exclude_user_id=user_id) < 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="at least one project admin must remain")

    db.delete(membership)
    write_audit_event(
        db,
        action="PROJECT_MEMBER_REMOVED",
        object_type="project_member",
        object_id=f"{project_id}:{user_id}",
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata=request_meta(request),
    )
    db.commit()
    return {"ok": True}


def _count_project_admins(db: Session, project_id: uuid.UUID, exclude_user_id: uuid.UUID | None = None) -> int:
    lock_project_admin_guard(db, project_id)
    stmt = select(func.count(ProjectMember.user_id)).where(
        ProjectMember.project_id == project_id,
        ProjectMember.role == ProjectRole.ADMIN,
    )
    if exclude_user_id is not None:
        stmt = stmt.where(ProjectMember.user_id != exclude_user_id)
    return int(db.execute(stmt).scalar() or 0)
