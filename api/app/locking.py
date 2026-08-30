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


def lock_worker_job(db: Session, object_id: uuid.UUID) -> None:
    """Serialize an API recovery mutation with the worker's UUID job lock."""

    key = object_id.int % (2**63 - 1)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def monitoring_source_lock_key(source_key: str) -> int:
    digest = hashlib.sha256(f"monitoring-source:{source_key}".encode("ascii")).digest()
    return int.from_bytes(digest[:8], "big") % (2**63 - 1)


def lock_monitoring_source(db: Session, source_key: str) -> None:
    """Share the worker's per-source serialization boundary in this transaction."""

    db.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": monitoring_source_lock_key(source_key)},
    )
