from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from .auth import GraphTokenContext

STATE_SCHEMA_VERSION = 3
MAX_DELTA_LINK_BYTES = 256 * 1024
MAX_ITEM_PAYLOAD_BYTES = 256 * 1024
MAX_PROVIDER_ITEM_ID_CHARACTERS = 512
MAX_ITEM_NAME_CHARACTERS = 255
MAX_ITEM_PATH_CHARACTERS = 400
MAX_ITEM_PATH_BYTES = 2000
STALE_STAGE_SECONDS = 24 * 60 * 60


class StateStoreError(RuntimeError):
    pass


class StateConflictError(StateStoreError):
    pass


@dataclass(frozen=True)
class DriveState:
    version: int = 0
    delta_link: str | None = field(default=None, repr=False)
    status: str = "new"
    last_successful_sync: str | None = None
    last_full_sync: str | None = None


def state_scope_key(context: GraphTokenContext) -> str:
    """Partition delta/snapshot state by the effective assessment context."""

    payload = {
        "auth_type": context.auth_type,
        "tenant_id": context.tenant_id,
        "client_id": context.client_id,
        "user_id": context.user_id if context.auth_type == "delegated" else None,
        "user_principal_name": (
            context.user_principal_name.casefold()
            if context.auth_type == "delegated" and context.user_principal_name
            else None
        ),
        "scopes": list(context.scopes),
        "roles": list(context.roles),
        # Imported opaque tokens cannot expose scopes/roles safely. A token-
        # derived discriminator prevents a rotated permission context from
        # reusing a prior materialized snapshot or delta checkpoint. Only the
        # outer scope hash is persisted; this inner digest is never emitted.
        "opaque_token_discriminator": (
            hashlib.sha256(context.access_token.encode("utf-8")).hexdigest()
            if context.jwt_inspection == "opaque_token_context_supplied_by_operator"
            else None
        ),
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def default_state_path() -> Path:
    configured = os.environ.get("SHARE_SENTINEL_GRAPH_STATE_PATH")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "ShareSentinel" / "sharepoint-state.sqlite3"
    xdg_state = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
    return base / "share-sentinel" / "sharepoint-state.sqlite3"


class SharePointStateStore:
    """SQLite-backed materialized metadata snapshots and staged delta checkpoints."""

    def __init__(self, path: str | Path, *, busy_timeout_seconds: float = 15.0) -> None:
        self.path = Path(path).expanduser()
        self.busy_timeout_seconds = max(0.1, float(busy_timeout_seconds))
        self._initialize_lock = threading.Lock()
        self._initialized = False

    def initialize(self) -> None:
        with self._initialize_lock:
            if self._initialized:
                return
            try:
                parent_existed = self.path.parent.exists()
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if os.name != "nt" and not parent_existed:
                    os.chmod(self.path.parent, 0o700)
                if not self.path.exists():
                    descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    os.close(descriptor)
                with self._connection() as conn:
                    conn.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS state_metadata (
                            key TEXT PRIMARY KEY,
                            value TEXT NOT NULL
                        );

                        CREATE TABLE IF NOT EXISTS drive_state (
                            scope_key TEXT NOT NULL,
                            tenant_id TEXT NOT NULL,
                            site_id TEXT NOT NULL,
                            drive_id TEXT NOT NULL,
                            version INTEGER NOT NULL DEFAULT 0,
                            delta_link TEXT,
                            status TEXT NOT NULL,
                            last_successful_sync TEXT,
                            last_full_sync TEXT,
                            updated_at REAL NOT NULL,
                            PRIMARY KEY (scope_key, tenant_id, site_id, drive_id)
                        );

                        CREATE TABLE IF NOT EXISTS items (
                            scope_key TEXT NOT NULL,
                            tenant_id TEXT NOT NULL,
                            site_id TEXT NOT NULL,
                            drive_id TEXT NOT NULL,
                            item_id TEXT NOT NULL,
                            parent_id TEXT,
                            item_name TEXT NOT NULL,
                            sort_path TEXT NOT NULL,
                            payload TEXT NOT NULL,
                            updated_at REAL NOT NULL,
                            PRIMARY KEY (scope_key, tenant_id, site_id, drive_id, item_id)
                        );

                        CREATE TABLE IF NOT EXISTS pending_syncs (
                            session_id TEXT NOT NULL,
                            scope_key TEXT NOT NULL,
                            tenant_id TEXT NOT NULL,
                            site_id TEXT NOT NULL,
                            drive_id TEXT NOT NULL,
                            base_version INTEGER NOT NULL,
                            sync_mode TEXT NOT NULL,
                            delta_link TEXT,
                            complete INTEGER NOT NULL DEFAULT 0,
                            created_at REAL NOT NULL,
                            PRIMARY KEY (session_id, scope_key, tenant_id, site_id, drive_id)
                        );

                        CREATE TABLE IF NOT EXISTS staged_items (
                            session_id TEXT NOT NULL,
                            scope_key TEXT NOT NULL,
                            tenant_id TEXT NOT NULL,
                            site_id TEXT NOT NULL,
                            drive_id TEXT NOT NULL,
                            item_id TEXT NOT NULL,
                            parent_id TEXT,
                            item_name TEXT,
                            sort_path TEXT NOT NULL,
                            payload TEXT,
                            deleted INTEGER NOT NULL,
                            PRIMARY KEY (
                                session_id, scope_key, tenant_id, site_id, drive_id, item_id
                            )
                        );

                        CREATE INDEX IF NOT EXISTS idx_items_materialize
                            ON items (scope_key, tenant_id, site_id, drive_id, sort_path, item_id);
                        CREATE INDEX IF NOT EXISTS idx_staged_materialize
                            ON staged_items (
                                session_id, scope_key, tenant_id, site_id, drive_id,
                                sort_path, item_id
                            );
                        CREATE INDEX IF NOT EXISTS idx_pending_created_at
                            ON pending_syncs (created_at);
                        """
                    )
                    row = conn.execute("SELECT value FROM state_metadata WHERE key = 'schema_version'").fetchone()
                    existing_version = int(row[0]) if row is not None else None
                    if existing_version not in {None, 1, 2, STATE_SCHEMA_VERSION}:
                        raise StateStoreError(f"unsupported SharePoint state schema version: {row[0]}")
                    conn.execute("BEGIN IMMEDIATE")
                    hierarchy_columns = {
                        "items": {"parent_id": "TEXT", "item_name": "TEXT"},
                        "staged_items": {"parent_id": "TEXT", "item_name": "TEXT"},
                    }
                    missing_columns = False
                    for table_name, expected_columns in hierarchy_columns.items():
                        present = {str(column[1]) for column in conn.execute(f"PRAGMA table_info({table_name})")}
                        for column_name, column_type in expected_columns.items():
                            if column_name in present:
                                continue
                            if existing_version == STATE_SCHEMA_VERSION:
                                raise StateStoreError("SharePoint state schema is missing hierarchy metadata")
                            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
                            missing_columns = True
                    if existing_version in {1, 2} or missing_columns:
                        # Version 1 did not retain stable parent relationships.
                        # Version 2 predates file archive-state metadata, which
                        # delta cannot backfill for unchanged items. Invalidate
                        # either snapshot transactionally and let the next run
                        # perform one complete, checkpoint-safe metadata sync.
                        conn.execute("DELETE FROM staged_items")
                        conn.execute("DELETE FROM pending_syncs")
                        conn.execute("DELETE FROM items")
                        conn.execute("DELETE FROM drive_state")
                    conn.execute(
                        """
                        INSERT INTO state_metadata (key, value) VALUES ('schema_version', ?)
                        ON CONFLICT (key) DO UPDATE SET value = excluded.value
                        """,
                        (str(STATE_SCHEMA_VERSION),),
                    )
                    conn.commit()
                self._protect_state_files()
            except (OSError, sqlite3.Error, ValueError) as exc:
                if isinstance(exc, StateStoreError):
                    raise
                raise StateStoreError("unable to initialize SharePoint state database") from exc
            self._initialized = True

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_seconds,
            isolation_level=None,
        )
        try:
            conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_seconds * 1000)}")
            if not self._initialized:
                conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("PRAGMA foreign_keys = ON")
            self._protect_state_files()
            yield conn
        finally:
            conn.close()
            self._protect_state_files()

    def _protect_state_files(self) -> None:
        if os.name == "nt":
            return
        for candidate in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            try:
                os.chmod(candidate, 0o600)
            except FileNotFoundError:
                continue

    def cleanup_stale_sessions(self, *, now: float | None = None) -> int:
        self.initialize()
        cutoff = (time.time() if now is None else now) - STALE_STAGE_SECONDS
        try:
            with self._connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    "SELECT DISTINCT session_id FROM pending_syncs WHERE created_at < ?",
                    (cutoff,),
                ).fetchall()
                session_ids = [str(row[0]) for row in rows]
                for session_id in session_ids:
                    conn.execute("DELETE FROM staged_items WHERE session_id = ?", (session_id,))
                    conn.execute("DELETE FROM pending_syncs WHERE session_id = ?", (session_id,))
                conn.commit()
                return len(session_ids)
        except sqlite3.Error as exc:
            raise StateStoreError("unable to clean stale SharePoint staging state") from exc

    def get_drive_state(
        self,
        scope_key: str,
        tenant_id: str,
        site_id: str,
        drive_id: str,
    ) -> DriveState:
        self.initialize()
        try:
            with self._connection() as conn:
                row = conn.execute(
                    """
                    SELECT version, delta_link, status, last_successful_sync, last_full_sync
                    FROM drive_state
                    WHERE scope_key = ? AND tenant_id = ? AND site_id = ? AND drive_id = ?
                    """,
                    (scope_key, tenant_id, site_id, drive_id),
                ).fetchone()
        except sqlite3.Error as exc:
            raise StateStoreError("unable to read SharePoint drive state") from exc
        if row is None:
            return DriveState()
        return DriveState(
            version=int(row[0]),
            delta_link=str(row[1]) if row[1] else None,
            status=str(row[2]),
            last_successful_sync=str(row[3]) if row[3] else None,
            last_full_sync=str(row[4]) if row[4] else None,
        )

    def begin_drive_stage(
        self,
        *,
        session_id: str,
        scope_key: str,
        tenant_id: str,
        site_id: str,
        drive_id: str,
        base_version: int,
        sync_mode: str,
    ) -> None:
        self.initialize()
        if sync_mode not in {"full", "delta"}:
            raise StateStoreError("invalid SharePoint sync mode")
        key = (session_id, scope_key, tenant_id, site_id, drive_id)
        try:
            with self._connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    DELETE FROM staged_items
                    WHERE session_id = ? AND scope_key = ? AND tenant_id = ?
                      AND site_id = ? AND drive_id = ?
                    """,
                    key,
                )
                conn.execute(
                    """
                    DELETE FROM pending_syncs
                    WHERE session_id = ? AND scope_key = ? AND tenant_id = ?
                      AND site_id = ? AND drive_id = ?
                    """,
                    key,
                )
                conn.execute(
                    """
                    INSERT INTO pending_syncs (
                        session_id, scope_key, tenant_id, site_id, drive_id,
                        base_version, sync_mode, delta_link, complete, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, ?)
                    """,
                    (*key, int(base_version), sync_mode, time.time()),
                )
                conn.commit()
        except sqlite3.Error as exc:
            raise StateStoreError("unable to begin SharePoint drive staging") from exc

    def stage_items(
        self,
        *,
        session_id: str,
        scope_key: str,
        tenant_id: str,
        site_id: str,
        drive_id: str,
        items: list[dict[str, object]],
    ) -> None:
        self.initialize()
        key = (session_id, scope_key, tenant_id, site_id, drive_id)
        rows: list[tuple[object, ...]] = []
        for item in items:
            item_id = item.get("provider_item_id")
            if (
                not isinstance(item_id, str)
                or not item_id
                or not item_id.strip()
                or "\x00" in item_id
                or len(item_id) > MAX_PROVIDER_ITEM_ID_CHARACTERS
            ):
                raise StateStoreError("SharePoint item is missing a bounded provider item ID")
            try:
                item_id.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise StateStoreError("SharePoint item is missing a bounded provider item ID") from exc
            raw_deleted = item.get("deleted", False)
            if not isinstance(raw_deleted, bool):
                raise StateStoreError("SharePoint item deletion marker must be a boolean")
            deleted = raw_deleted
            if deleted:
                parent_id = None
                item_name = None
                payload = None
                sort_path = "/"
            else:
                item_name = item.get("name")
                if (
                    not isinstance(item_name, str)
                    or not item_name
                    or not item_name.strip()
                    or "\x00" in item_name
                    or len(item_name) > MAX_ITEM_NAME_CHARACTERS
                ):
                    raise StateStoreError("SharePoint item is missing a bounded name")
                raw_parent_id = item.get("provider_parent_id")
                if raw_parent_id is None:
                    parent_id = None
                elif (
                    not isinstance(raw_parent_id, str)
                    or not raw_parent_id
                    or not raw_parent_id.strip()
                    or "\x00" in raw_parent_id
                    or len(raw_parent_id) > MAX_PROVIDER_ITEM_ID_CHARACTERS
                ):
                    raise StateStoreError("SharePoint item has an invalid parent identity")
                else:
                    parent_id = raw_parent_id
                sort_path = item.get("path")
                if (
                    not isinstance(sort_path, str)
                    or not sort_path
                    or "\x00" in sort_path
                    or len(sort_path) > MAX_ITEM_PATH_CHARACTERS
                ):
                    raise StateStoreError("SharePoint item path exceeds the supported metadata bounds")
                try:
                    item_name.encode("utf-8")
                    if parent_id is not None:
                        parent_id.encode("utf-8")
                    path_size = len(sort_path.encode("utf-8"))
                    payload = json.dumps(item, ensure_ascii=True, separators=(",", ":"))
                    payload_size = len(payload.encode("utf-8"))
                except (TypeError, ValueError, UnicodeEncodeError) as exc:
                    raise StateStoreError("SharePoint item metadata is not safely serializable") from exc
                if path_size > MAX_ITEM_PATH_BYTES:
                    raise StateStoreError("SharePoint item path exceeds the supported metadata bounds")
                if payload_size > MAX_ITEM_PAYLOAD_BYTES:
                    raise StateStoreError("SharePoint item metadata exceeds the per-item safety limit")
            rows.append((*key, item_id, parent_id, item_name, sort_path, payload, int(deleted)))
        try:
            with self._connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                pending = conn.execute(
                    """
                    SELECT 1 FROM pending_syncs
                    WHERE session_id = ? AND scope_key = ? AND tenant_id = ?
                      AND site_id = ? AND drive_id = ?
                    """,
                    key,
                ).fetchone()
                if pending is None:
                    raise StateStoreError("SharePoint drive staging session is missing")
                if rows:
                    conn.executemany(
                        """
                        INSERT INTO staged_items (
                            session_id, scope_key, tenant_id, site_id, drive_id,
                            item_id, parent_id, item_name, sort_path, payload, deleted
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (
                            session_id, scope_key, tenant_id, site_id, drive_id, item_id
                        ) DO UPDATE SET
                            parent_id = excluded.parent_id,
                            item_name = excluded.item_name,
                            sort_path = excluded.sort_path,
                            payload = excluded.payload,
                            deleted = excluded.deleted
                        """,
                        rows,
                    )
                conn.execute(
                    """
                    UPDATE pending_syncs SET created_at = ?
                    WHERE session_id = ? AND scope_key = ? AND tenant_id = ?
                      AND site_id = ? AND drive_id = ?
                    """,
                    (time.time(), *key),
                )
                conn.commit()
        except StateStoreError:
            raise
        except sqlite3.Error as exc:
            raise StateStoreError("unable to stage SharePoint item metadata") from exc

    def complete_drive_stage(
        self,
        *,
        session_id: str,
        scope_key: str,
        tenant_id: str,
        site_id: str,
        drive_id: str,
        delta_link: str,
    ) -> None:
        if not isinstance(delta_link, str) or not delta_link or "\x00" in delta_link:
            raise StateStoreError("Graph delta response is missing a bounded delta link")
        try:
            delta_link_size = len(delta_link.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise StateStoreError("Graph delta response is missing a bounded delta link") from exc
        if delta_link_size > MAX_DELTA_LINK_BYTES:
            raise StateStoreError("Graph delta response is missing a bounded delta link")
        normalized = delta_link
        key = (session_id, scope_key, tenant_id, site_id, drive_id)
        try:
            with self._connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                pending = conn.execute(
                    """
                    SELECT sync_mode FROM pending_syncs
                    WHERE session_id = ? AND scope_key = ? AND tenant_id = ?
                      AND site_id = ? AND drive_id = ?
                    """,
                    key,
                ).fetchone()
                if pending is None:
                    raise StateStoreError("SharePoint drive staging session is missing")
                self._validate_staged_parent_deletions(
                    conn,
                    key=key,
                    sync_mode=str(pending[0]),
                )
                cursor = conn.execute(
                    """
                    UPDATE pending_syncs SET delta_link = ?, complete = 1, created_at = ?
                    WHERE session_id = ? AND scope_key = ? AND tenant_id = ?
                      AND site_id = ? AND drive_id = ?
                    """,
                    (normalized, time.time(), *key),
                )
                if cursor.rowcount != 1:
                    raise StateStoreError("SharePoint drive staging session is missing")
                conn.commit()
        except StateStoreError:
            raise
        except sqlite3.Error as exc:
            raise StateStoreError("unable to complete SharePoint drive staging") from exc

    @staticmethod
    def _validate_staged_parent_deletions(
        conn: sqlite3.Connection,
        *,
        key: tuple[str, str, str, str, str],
        sync_mode: str,
    ) -> None:
        session_id, scope_key, tenant_id, site_id, drive_id = key
        if sync_mode == "full":
            candidate_query = """
                SELECT item_id, parent_id
                FROM staged_items
                WHERE session_id = ? AND scope_key = ? AND tenant_id = ?
                  AND site_id = ? AND drive_id = ? AND deleted = 0
            """
            candidate_parameters: tuple[str, ...] = key
        elif sync_mode == "delta":
            candidate_query = """
                SELECT i.item_id, i.parent_id
                FROM items i
                WHERE i.scope_key = ? AND i.tenant_id = ?
                  AND i.site_id = ? AND i.drive_id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM staged_items override
                      WHERE override.session_id = ? AND override.scope_key = i.scope_key
                        AND override.tenant_id = i.tenant_id
                        AND override.site_id = i.site_id
                        AND override.drive_id = i.drive_id
                        AND override.item_id = i.item_id
                  )
                UNION ALL
                SELECT staged.item_id, staged.parent_id
                FROM staged_items staged
                WHERE staged.session_id = ? AND staged.scope_key = ?
                  AND staged.tenant_id = ? AND staged.site_id = ?
                  AND staged.drive_id = ? AND staged.deleted = 0
            """
            candidate_parameters = (
                scope_key,
                tenant_id,
                site_id,
                drive_id,
                session_id,
                *key,
            )
        else:
            raise StateStoreError("invalid SharePoint sync mode")

        surviving_child = conn.execute(
            f"""
            WITH
            candidate(item_id, parent_id) AS (
                {candidate_query}
            ),
            deleted_parent(item_id) AS (
                SELECT item_id FROM staged_items
                WHERE session_id = ? AND scope_key = ? AND tenant_id = ?
                  AND site_id = ? AND drive_id = ? AND deleted = 1
            )
            SELECT 1
            FROM candidate child
            JOIN deleted_parent parent ON parent.item_id = child.parent_id
            LIMIT 1
            """,
            (*candidate_parameters, *key),
        ).fetchone()
        if surviving_child is not None:
            raise StateStoreError("SharePoint folder deletion still has live children after applying the delta")

    def count_current_items(
        self,
        scope_key: str,
        tenant_id: str,
        site_id: str,
        drive_id: str,
    ) -> int:
        self.initialize()
        try:
            with self._connection() as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*) FROM items
                    WHERE scope_key = ? AND tenant_id = ? AND site_id = ? AND drive_id = ?
                    """,
                    (scope_key, tenant_id, site_id, drive_id),
                ).fetchone()
                return int(row[0])
        except sqlite3.Error as exc:
            raise StateStoreError("unable to count current SharePoint snapshot items") from exc

    def count_staged_items(
        self,
        *,
        session_id: str,
        scope_key: str,
        tenant_id: str,
        site_id: str,
        drive_id: str,
        include_deleted: bool = False,
    ) -> int:
        self.initialize()
        key = (session_id, scope_key, tenant_id, site_id, drive_id)
        deleted_clause = "" if include_deleted else " AND deleted = 0"
        try:
            with self._connection() as conn:
                row = conn.execute(
                    f"""
                    SELECT COUNT(*) FROM staged_items
                    WHERE session_id = ? AND scope_key = ? AND tenant_id = ?
                      AND site_id = ? AND drive_id = ?{deleted_clause}
                    """,
                    key,
                ).fetchone()
                return int(row[0])
        except sqlite3.Error as exc:
            raise StateStoreError("unable to count staged SharePoint items") from exc

    def count_materialized_items(
        self,
        *,
        session_id: str,
        scope_key: str,
        tenant_id: str,
        site_id: str,
        drive_id: str,
    ) -> int:
        self.initialize()
        key = (session_id, scope_key, tenant_id, site_id, drive_id)
        try:
            with self._connection() as conn:
                pending = self._pending_row(conn, key)
                if pending[1] == "full":
                    row = conn.execute(
                        """
                        SELECT COUNT(*) FROM staged_items
                        WHERE session_id = ? AND scope_key = ? AND tenant_id = ?
                          AND site_id = ? AND drive_id = ? AND deleted = 0
                        """,
                        key,
                    ).fetchone()
                else:
                    row = conn.execute(
                        """
                        SELECT
                            (SELECT COUNT(*) FROM items i
                             WHERE i.scope_key = ? AND i.tenant_id = ?
                               AND i.site_id = ? AND i.drive_id = ?
                               AND NOT EXISTS (
                                   SELECT 1 FROM staged_items s
                                   WHERE s.session_id = ? AND s.scope_key = i.scope_key
                                     AND s.tenant_id = i.tenant_id AND s.site_id = i.site_id
                                     AND s.drive_id = i.drive_id AND s.item_id = i.item_id
                               ))
                          + (SELECT COUNT(*) FROM staged_items
                             WHERE session_id = ? AND scope_key = ? AND tenant_id = ?
                               AND site_id = ? AND drive_id = ? AND deleted = 0)
                        """,
                        (
                            scope_key,
                            tenant_id,
                            site_id,
                            drive_id,
                            session_id,
                            session_id,
                            scope_key,
                            tenant_id,
                            site_id,
                            drive_id,
                        ),
                    ).fetchone()
                return int(row[0])
        except StateStoreError:
            raise
        except sqlite3.Error as exc:
            raise StateStoreError("unable to count SharePoint snapshot items") from exc

    def iter_materialized_items(
        self,
        *,
        session_id: str,
        scope_key: str,
        tenant_id: str,
        site_id: str,
        drive_id: str,
        fetch_size: int = 1000,
    ) -> Iterator[dict[str, object]]:
        self.initialize()
        key = (session_id, scope_key, tenant_id, site_id, drive_id)
        expected_count = self.count_materialized_items(
            session_id=session_id,
            scope_key=scope_key,
            tenant_id=tenant_id,
            site_id=site_id,
            drive_id=drive_id,
        )
        try:
            with self._connection() as conn:
                pending = self._pending_row(conn, key)
                if pending[1] == "full":
                    candidate_query = """
                        SELECT item_id, parent_id, item_name, sort_path, payload
                        FROM staged_items
                        WHERE session_id = ? AND scope_key = ? AND tenant_id = ?
                          AND site_id = ? AND drive_id = ? AND deleted = 0
                    """
                    candidate_parameters = key
                else:
                    candidate_query = """
                            SELECT i.item_id, i.parent_id, i.item_name, i.sort_path, i.payload
                            FROM items i
                            WHERE i.scope_key = ? AND i.tenant_id = ?
                              AND i.site_id = ? AND i.drive_id = ?
                              AND NOT EXISTS (
                                  SELECT 1 FROM staged_items s
                                  WHERE s.session_id = ? AND s.scope_key = i.scope_key
                                    AND s.tenant_id = i.tenant_id AND s.site_id = i.site_id
                                    AND s.drive_id = i.drive_id AND s.item_id = i.item_id
                              )
                            UNION ALL
                            SELECT s.item_id, s.parent_id, s.item_name, s.sort_path, s.payload
                            FROM staged_items s
                            WHERE s.session_id = ? AND s.scope_key = ? AND s.tenant_id = ?
                              AND s.site_id = ? AND s.drive_id = ? AND s.deleted = 0
                    """
                    candidate_parameters = (
                        scope_key,
                        tenant_id,
                        site_id,
                        drive_id,
                        session_id,
                        session_id,
                        scope_key,
                        tenant_id,
                        site_id,
                        drive_id,
                    )

                cursor = conn.execute(
                    f"""
                    WITH RECURSIVE
                    candidate(item_id, parent_id, item_name, seed_path, payload) AS (
                        {candidate_query}
                    ),
                    hierarchy(item_id, parent_id, item_name, resolved_path, payload, depth) AS (
                        SELECT
                            child.item_id,
                            child.parent_id,
                            child.item_name,
                            child.seed_path,
                            child.payload,
                            0
                        FROM candidate child
                        WHERE child.parent_id IS NULL
                           OR NOT EXISTS (
                               SELECT 1 FROM candidate parent
                               WHERE parent.item_id = child.parent_id
                           )
                        UNION ALL
                        SELECT
                            child.item_id,
                            child.parent_id,
                            child.item_name,
                            CASE
                                WHEN parent.resolved_path = '/'
                                    THEN '/' || child.item_name
                                ELSE parent.resolved_path || '/' || child.item_name
                            END,
                            child.payload,
                            parent.depth + 1
                        FROM candidate child
                        JOIN hierarchy parent ON child.parent_id = parent.item_id
                        WHERE parent.depth < {MAX_ITEM_PATH_CHARACTERS}
                    )
                    SELECT item_id, resolved_path, payload
                    FROM hierarchy
                    ORDER BY resolved_path, item_id
                    """,
                    candidate_parameters,
                )
                emitted = 0
                while True:
                    rows = cursor.fetchmany(max(1, int(fetch_size)))
                    if not rows:
                        break
                    for row in rows:
                        resolved_path = str(row[1])
                        try:
                            resolved_size = len(resolved_path.encode("utf-8"))
                        except UnicodeEncodeError as exc:
                            raise StateStoreError("SharePoint item hierarchy contains invalid Unicode") from exc
                        if len(resolved_path) > MAX_ITEM_PATH_CHARACTERS or resolved_size > MAX_ITEM_PATH_BYTES:
                            raise StateStoreError("SharePoint materialized item path exceeds supported metadata bounds")
                        payload = json.loads(str(row[2]))
                        if not isinstance(payload, dict):
                            raise StateStoreError("SharePoint materialized item metadata is invalid")
                        payload["path"] = resolved_path
                        emitted += 1
                        yield payload
                if emitted != expected_count:
                    raise StateStoreError(
                        "SharePoint item hierarchy contains a cycle or unresolved parent relationship"
                    )
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            raise StateStoreError("unable to stream SharePoint snapshot items") from exc

    @staticmethod
    def _pending_row(conn: sqlite3.Connection, key: tuple[str, ...]) -> tuple[int, str, str]:
        row = conn.execute(
            """
            SELECT base_version, sync_mode, delta_link FROM pending_syncs
            WHERE session_id = ? AND scope_key = ? AND tenant_id = ?
              AND site_id = ? AND drive_id = ? AND complete = 1
            """,
            key,
        ).fetchone()
        if row is None:
            raise StateStoreError("SharePoint drive staging is incomplete")
        return int(row[0]), str(row[1]), str(row[2])

    def commit_drive(
        self,
        *,
        session_id: str,
        scope_key: str,
        tenant_id: str,
        site_id: str,
        drive_id: str,
        completed_at: str | None = None,
    ) -> None:
        self.initialize()
        key = (session_id, scope_key, tenant_id, site_id, drive_id)
        timestamp = completed_at or datetime.now(tz=UTC).isoformat()
        try:
            with self._connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                base_version, sync_mode, delta_link = self._pending_row(conn, key)
                state_row = conn.execute(
                    """
                    SELECT version, last_full_sync FROM drive_state
                    WHERE scope_key = ? AND tenant_id = ? AND site_id = ? AND drive_id = ?
                    """,
                    (scope_key, tenant_id, site_id, drive_id),
                ).fetchone()
                current_version = int(state_row[0]) if state_row else 0
                previous_full_sync = str(state_row[1]) if state_row and state_row[1] else None
                if current_version != base_version:
                    raise StateConflictError("SharePoint state changed concurrently; checkpoint was not advanced")

                if sync_mode == "full":
                    conn.execute(
                        """
                        DELETE FROM items
                        WHERE scope_key = ? AND tenant_id = ? AND site_id = ? AND drive_id = ?
                        """,
                        (scope_key, tenant_id, site_id, drive_id),
                    )
                else:
                    conn.execute(
                        """
                        DELETE FROM items
                        WHERE scope_key = ? AND tenant_id = ? AND site_id = ? AND drive_id = ?
                          AND item_id IN (
                              SELECT item_id FROM staged_items
                              WHERE session_id = ? AND scope_key = ? AND tenant_id = ?
                                AND site_id = ? AND drive_id = ? AND deleted = 1
                          )
                        """,
                        (
                            scope_key,
                            tenant_id,
                            site_id,
                            drive_id,
                            session_id,
                            scope_key,
                            tenant_id,
                            site_id,
                            drive_id,
                        ),
                    )

                conn.execute(
                    """
                    INSERT INTO items (
                        scope_key, tenant_id, site_id, drive_id,
                        item_id, parent_id, item_name, sort_path, payload, updated_at
                    )
                    SELECT scope_key, tenant_id, site_id, drive_id,
                           item_id, parent_id, item_name, sort_path, payload, ?
                    FROM staged_items
                    WHERE session_id = ? AND scope_key = ? AND tenant_id = ?
                      AND site_id = ? AND drive_id = ? AND deleted = 0
                    ON CONFLICT (scope_key, tenant_id, site_id, drive_id, item_id)
                    DO UPDATE SET
                        parent_id = excluded.parent_id,
                        item_name = excluded.item_name,
                        sort_path = excluded.sort_path,
                        payload = excluded.payload,
                        updated_at = excluded.updated_at
                    """,
                    (time.time(), *key),
                )
                full_sync = timestamp if sync_mode == "full" else previous_full_sync
                conn.execute(
                    """
                    INSERT INTO drive_state (
                        scope_key, tenant_id, site_id, drive_id, version,
                        delta_link, status, last_successful_sync, last_full_sync, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, 'ok', ?, ?, ?)
                    ON CONFLICT (scope_key, tenant_id, site_id, drive_id)
                    DO UPDATE SET
                        version = drive_state.version + 1,
                        delta_link = excluded.delta_link,
                        status = 'ok',
                        last_successful_sync = excluded.last_successful_sync,
                        last_full_sync = excluded.last_full_sync,
                        updated_at = excluded.updated_at
                    """,
                    (
                        scope_key,
                        tenant_id,
                        site_id,
                        drive_id,
                        delta_link,
                        timestamp,
                        full_sync,
                        time.time(),
                    ),
                )
                conn.execute(
                    """
                    DELETE FROM staged_items
                    WHERE session_id = ? AND scope_key = ? AND tenant_id = ?
                      AND site_id = ? AND drive_id = ?
                    """,
                    key,
                )
                conn.execute(
                    """
                    DELETE FROM pending_syncs
                    WHERE session_id = ? AND scope_key = ? AND tenant_id = ?
                      AND site_id = ? AND drive_id = ?
                    """,
                    key,
                )
                conn.commit()
        except StateConflictError:
            raise
        except (sqlite3.Error, StateStoreError) as exc:
            if isinstance(exc, StateStoreError):
                raise
            raise StateStoreError("unable to commit SharePoint drive state") from exc

    def discard_drive(
        self,
        *,
        session_id: str,
        scope_key: str,
        tenant_id: str,
        site_id: str,
        drive_id: str,
    ) -> None:
        self.initialize()
        key = (session_id, scope_key, tenant_id, site_id, drive_id)
        try:
            with self._connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """DELETE FROM staged_items WHERE session_id = ? AND scope_key = ?
                       AND tenant_id = ? AND site_id = ? AND drive_id = ?""",
                    key,
                )
                conn.execute(
                    """DELETE FROM pending_syncs WHERE session_id = ? AND scope_key = ?
                       AND tenant_id = ? AND site_id = ? AND drive_id = ?""",
                    key,
                )
                conn.commit()
        except sqlite3.Error as exc:
            raise StateStoreError("unable to discard SharePoint drive staging") from exc

    def discard_session(self, session_id: str) -> None:
        self.initialize()
        try:
            with self._connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM staged_items WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM pending_syncs WHERE session_id = ?", (session_id,))
                conn.commit()
        except sqlite3.Error as exc:
            raise StateStoreError("unable to discard SharePoint staging session") from exc
