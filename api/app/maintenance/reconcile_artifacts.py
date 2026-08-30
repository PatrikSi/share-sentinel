#!/usr/bin/env python3
"""Report and optionally remove stale unreferenced artifact files.

The database remains authoritative. Destructive mode rechecks every candidate
immediately before deletion and is intentionally bounded so an operator can
review a dry run before applying cleanup in manageable batches.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import stat
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Iterable

DEFAULT_MIN_AGE_HOURS = 24.0
DEFAULT_MAX_DELETE = 1000
INTERNAL_STORAGE_FILES = frozenset({".share-sentinel-capacity.lock"})


class CandidateChangedError(OSError):
    """A scanned candidate no longer names the same regular file."""


@dataclass(frozen=True)
class Candidate:
    category: str
    key: str
    path: Path
    size_bytes: int
    modified_at: datetime
    device: int
    inode: int
    modified_ns: int

    def public_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload.pop("path", None)
        payload.pop("device", None)
        payload.pop("inode", None)
        payload.pop("modified_ns", None)
        payload["modified_at"] = self.modified_at.isoformat()
        return payload


def _database_url(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("DATABASE_URL is required")
    return normalized.replace("postgresql+psycopg://", "postgresql://", 1)


def _safe_key(value: str) -> str:
    path = PurePosixPath(value)
    parts = tuple(part for part in path.parts if part not in {"", "."})
    if path.is_absolute() or not parts or any(part == ".." for part in parts):
        raise ValueError(f"unsafe artifact key in database: {value!r}")
    return PurePosixPath(*parts).as_posix()


def _file_candidate(root: Path, path: Path, category: str) -> Candidate | None:
    try:
        file_stat = path.stat(follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        return None
    if not stat.S_ISREG(file_stat.st_mode):
        return None
    relative = path.relative_to(root).as_posix()
    return Candidate(
        category=category,
        key=relative,
        path=path,
        size_bytes=max(0, file_stat.st_size),
        modified_at=datetime.fromtimestamp(file_stat.st_mtime, tz=UTC),
        device=file_stat.st_dev,
        inode=file_stat.st_ino,
        modified_ns=file_stat.st_mtime_ns,
    )


def _directory_open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_relative_directory(root: Path, parts: tuple[str, ...]) -> int:
    current_fd = os.open(root, _directory_open_flags())
    try:
        if not stat.S_ISDIR(os.fstat(current_fd).st_mode):
            raise OSError("artifact storage root is not a directory")
        for part in parts:
            next_fd = os.open(part, _directory_open_flags(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
            if not stat.S_ISDIR(os.fstat(current_fd).st_mode):
                raise OSError("artifact candidate parent is not a directory")
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _safe_regular_stat(root: Path, key: str):
    parts = PurePosixPath(_safe_key(key)).parts
    try:
        parent_fd = _open_relative_directory(root, parts[:-1])
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return None
        raise
    try:
        try:
            file_stat = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        return file_stat if stat.S_ISREG(file_stat.st_mode) else None
    finally:
        os.close(parent_fd)


def _unlink_candidate(root: Path, candidate: Candidate) -> None:
    parts = PurePosixPath(_safe_key(candidate.key)).parts
    try:
        parent_fd = _open_relative_directory(root, parts[:-1])
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise CandidateChangedError("candidate parent disappeared") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise CandidateChangedError("candidate parent became a symlink") from exc
        raise
    try:
        try:
            current = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise CandidateChangedError("candidate disappeared") from exc
        if not stat.S_ISREG(current.st_mode) or (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
        ) != (
            candidate.device,
            candidate.inode,
            candidate.size_bytes,
            candidate.modified_ns,
        ):
            raise CandidateChangedError("candidate changed after scan")
        os.unlink(parts[-1], dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _remove_empty_relative_parents(root: Path, parts: tuple[str, ...]) -> None:
    for depth in range(len(parts), 0, -1):
        try:
            parent_fd = _open_relative_directory(root, parts[: depth - 1])
        except OSError:
            return
        try:
            os.rmdir(parts[depth - 1], dir_fd=parent_fd)
            os.fsync(parent_fd)
        except OSError:
            return
        finally:
            os.close(parent_fd)


def scan_candidates(
    root: Path,
    referenced_keys: set[str],
    *,
    cutoff: datetime,
) -> tuple[list[Candidate], list[Candidate], list[str], int]:
    if not root.exists() or not root.is_dir():
        raise ValueError(f"artifact storage path is not a directory: {root}")

    normalized_references = {_safe_key(key) for key in referenced_keys}
    missing_references = sorted(key for key in normalized_references if _safe_regular_stat(root, key) is None)
    orphaned: list[Candidate] = []
    stale_multipart: list[Candidate] = []
    recent_unreferenced = 0

    for path in root.rglob("*"):
        relative_parts = path.relative_to(root).parts
        if not relative_parts:
            continue
        if len(relative_parts) == 1 and relative_parts[0] in INTERNAL_STORAGE_FILES:
            continue
        is_multipart = relative_parts[0] == ".multipart"
        candidate = _file_candidate(root, path, "stale_multipart" if is_multipart else "orphan")
        if candidate is None:
            continue
        if is_multipart:
            if candidate.modified_at <= cutoff:
                stale_multipart.append(candidate)
            continue
        if candidate.key in normalized_references:
            continue
        if candidate.modified_at <= cutoff:
            orphaned.append(candidate)
        else:
            recent_unreferenced += 1

    def ordering(item: Candidate) -> tuple[datetime, str]:
        return item.modified_at, item.key

    return (
        sorted(orphaned, key=ordering),
        sorted(stale_multipart, key=ordering),
        missing_references,
        recent_unreferenced,
    )


def _load_referenced_keys(connection) -> set[str]:
    rows = connection.execute("SELECT artifact_key FROM scan_runs WHERE artifact_key IS NOT NULL").fetchall()
    return {_safe_key(str(row[0])) for row in rows}


def _is_referenced(connection, key: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM scan_runs WHERE artifact_key = %s LIMIT 1",
        (key,),
    ).fetchone()
    return row is not None


def _audit_cleanup(
    connection,
    candidate: Candidate,
    action: str,
    *,
    reason: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO audit_events (project_id, action, object_type, object_id, metadata)
        VALUES (NULL, %s, 'artifact', %s, %s::jsonb)
        """,
        (
            action,
            candidate.key,
            json.dumps(
                {
                    "category": candidate.category,
                    "size_bytes": candidate.size_bytes,
                    "modified_at": candidate.modified_at.isoformat(),
                    "actor": "maintenance_cli",
                    **({"reason": reason} if reason else {}),
                },
                separators=(",", ":"),
            ),
        ),
    )


def delete_candidates(
    connection,
    candidates: Iterable[Candidate],
    *,
    root: Path,
    limit: int,
) -> tuple[list[str], list[str]]:
    deleted: list[str] = []
    skipped: list[str] = []
    for candidate in candidates:
        if len(deleted) >= limit:
            break
        if candidate.category == "orphan" and _is_referenced(connection, candidate.key):
            skipped.append(candidate.key)
            continue
        _audit_cleanup(connection, candidate, "ARTIFACT_CLEANUP_REQUESTED")
        connection.commit()
        try:
            _unlink_candidate(root, candidate)
        except CandidateChangedError as exc:
            _audit_cleanup(
                connection,
                candidate,
                "ARTIFACT_CLEANUP_SKIPPED",
                reason=str(exc),
            )
            connection.commit()
            skipped.append(candidate.key)
            continue
        _audit_cleanup(connection, candidate, "ARTIFACT_CLEANUP_DELETED")
        connection.commit()
        deleted.append(candidate.key)
        _remove_empty_relative_parents(root, PurePosixPath(candidate.key).parts[:-1])
    return deleted, skipped


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--storage-path",
        type=Path,
        default=Path(os.getenv("ARTIFACT_STORAGE_PATH", "/artifacts")),
    )
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""))
    parser.add_argument("--min-age-hours", type=float, default=DEFAULT_MIN_AGE_HOURS)
    parser.add_argument("--max-delete", type=int, default=DEFAULT_MAX_DELETE)
    parser.add_argument(
        "--apply", action="store_true", help="Delete bounded stale candidates after rechecking references"
    )
    parser.add_argument("--json", action="store_true", help="Emit a machine-readable report")
    args = parser.parse_args(argv)
    if not 1 <= args.max_delete <= 100_000:
        parser.error("--max-delete must be between 1 and 100000")
    if not 1 <= args.min_age_hours <= 24 * 365:
        parser.error("--min-age-hours must be between 1 and 8760")
    return args


def _report(
    *,
    root: Path,
    cutoff: datetime,
    orphaned: list[Candidate],
    stale_multipart: list[Candidate],
    missing_references: list[str],
    recent_unreferenced: int,
    deleted: list[str],
    skipped: list[str],
    applied: bool,
) -> dict[str, object]:
    return {
        "storage_path": str(root),
        "cutoff": cutoff.isoformat(),
        "applied": applied,
        "counts": {
            "orphaned": len(orphaned),
            "stale_multipart": len(stale_multipart),
            "missing_references": len(missing_references),
            "recent_unreferenced": recent_unreferenced,
            "deleted": len(deleted),
            "skipped": len(skipped),
        },
        "reclaimable_bytes": sum(item.size_bytes for item in orphaned + stale_multipart),
        "orphaned": [item.public_dict() for item in orphaned],
        "stale_multipart": [item.public_dict() for item in stale_multipart],
        "missing_references": missing_references,
        "deleted": deleted,
        "skipped": skipped,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        import psycopg

        database_url = _database_url(args.database_url)
        root = args.storage_path.resolve(strict=True)
        cutoff = datetime.now(tz=UTC) - timedelta(hours=args.min_age_hours)
        with psycopg.connect(database_url, connect_timeout=10) as connection:
            referenced_keys = _load_referenced_keys(connection)
            orphaned, stale_multipart, missing_references, recent_unreferenced = scan_candidates(
                root,
                referenced_keys,
                cutoff=cutoff,
            )
            deleted: list[str] = []
            skipped: list[str] = []
            if args.apply:
                candidates = sorted(orphaned + stale_multipart, key=lambda item: (item.modified_at, item.key))
                deleted, skipped = delete_candidates(
                    connection,
                    candidates,
                    root=root,
                    limit=args.max_delete,
                )
        report = _report(
            root=root,
            cutoff=cutoff,
            orphaned=orphaned,
            stale_multipart=stale_multipart,
            missing_references=missing_references,
            recent_unreferenced=recent_unreferenced,
            deleted=deleted,
            skipped=skipped,
            applied=args.apply,
        )
    except (OSError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: artifact reconciliation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, separators=(",", ":"), sort_keys=True))
    else:
        counts = report["counts"]
        print(
            "Artifact reconciliation "
            f"orphaned={counts['orphaned']} stale_multipart={counts['stale_multipart']} "
            f"missing_references={counts['missing_references']} recent_unreferenced={counts['recent_unreferenced']} "
            f"reclaimable_bytes={report['reclaimable_bytes']} deleted={counts['deleted']} skipped={counts['skipped']}"
        )
        if not args.apply and (counts["orphaned"] or counts["stale_multipart"]):
            print("Dry run only; rerun with --apply after reviewing the JSON report.")
    return 1 if missing_references else 0


if __name__ == "__main__":
    raise SystemExit(main())
