import hashlib
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session


def advisory_xact_lock(db: Session, namespace: str, resource: str) -> None:
    digest = hashlib.blake2b(f"{namespace}:{resource}".encode("utf-8"), digest_size=8).digest()
    key = int.from_bytes(digest, "big", signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def lock_sysadmin_guard(db: Session) -> None:
    advisory_xact_lock(db, "share-sentinel", "sysadmin-guard")


def lock_project_admin_guard(db: Session, project_id: uuid.UUID) -> None:
    advisory_xact_lock(db, "share-sentinel", f"project-admin-guard:{project_id}")
