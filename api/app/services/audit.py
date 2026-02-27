import uuid

from sqlalchemy.orm import Session

from app.models import AuditEvent


def write_audit_event(
    db: Session,
    action: str,
    object_type: str,
    object_id: str,
    actor_user_id: uuid.UUID | None = None,
    actor_token_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    metadata: dict | None = None,
) -> None:
    event = AuditEvent(
        action=action,
        object_type=object_type,
        object_id=object_id,
        actor_user_id=actor_user_id,
        actor_token_id=actor_token_id,
        project_id=project_id,
        metadata_json=metadata or {},
    )
    db.add(event)
