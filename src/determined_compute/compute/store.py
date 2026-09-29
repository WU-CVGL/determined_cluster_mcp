"""SQLite persistence for compute task identity and idempotency."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from .models import ConflictError, NotFoundError, TaskRecord


_SCHEMA = """
CREATE TABLE IF NOT EXISTS compute_tasks (
    task_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    profile_hash TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    remote_id TEXT,
    remote_state TEXT,
    code_revision TEXT,
    name TEXT,
    description TEXT,
    workdir TEXT NOT NULL,
    output_dir TEXT NOT NULL,
    cluster_identity TEXT,
    submission_marker TEXT NOT NULL,
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    origin TEXT NOT NULL DEFAULT 'submitted',
    remote_user_id TEXT,
    remote_cluster_id TEXT,
    UNIQUE(owner, request_id)
);
CREATE INDEX IF NOT EXISTS compute_tasks_owner_created
    ON compute_tasks(owner, created_at DESC, task_id DESC);
"""
_RECORD_FIELDS = frozenset(field.name for field in fields(TaskRecord))


class SQLiteTaskStore:
    """A small transactional store safe for threads and multiple processes."""

    def __init__(self, path: object) -> None:
        self.path = str(Path(path)) if str(path) != ":memory:" else ":memory:"
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path, timeout=30.0, isolation_level=None, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA busy_timeout = 30000")
            if self.path != ":memory:":
                self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.executescript(_SCHEMA)
            self._migrate_schema()

    def _migrate_schema(self) -> None:
        """Add metadata columns without rewriting existing task rows."""

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            columns = {
                str(row["name"])
                for row in self._connection.execute("PRAGMA table_info(compute_tasks)")
            }
            if "name" not in columns:
                self._connection.execute("ALTER TABLE compute_tasks ADD COLUMN name TEXT")
            if "description" not in columns:
                self._connection.execute(
                    "ALTER TABLE compute_tasks ADD COLUMN description TEXT"
                )
            if "origin" not in columns:
                self._connection.execute(
                    """ALTER TABLE compute_tasks ADD COLUMN origin TEXT
                    NOT NULL DEFAULT 'submitted'"""
                )
            if "remote_user_id" not in columns:
                self._connection.execute(
                    "ALTER TABLE compute_tasks ADD COLUMN remote_user_id TEXT"
                )
            if "remote_cluster_id" not in columns:
                self._connection.execute(
                    "ALTER TABLE compute_tasks ADD COLUMN remote_cluster_id TEXT"
                )
            self._connection.execute("COMMIT")
        except Exception:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _record(row: sqlite3.Row) -> TaskRecord:
        # Ignore columns added by a newer release that shares this database.
        value = {key: row[key] for key in row.keys() if key in _RECORD_FIELDS}
        if value.get("origin") == "adopted":
            if value.get("workdir") == "":
                value["workdir"] = None
            if value.get("output_dir") == "":
                value["output_dir"] = None
        return TaskRecord(**value)

    def claim(
        self,
        *,
        request_id: str,
        owner: str,
        payload_hash: str,
        profile_hash: str,
        kind: str,
        code_revision: Optional[str],
        workdir: str,
        output_dir: str,
        cluster_identity: Optional[str],
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Tuple[TaskRecord, bool]:
        """Create a pending record, or return the prior identical request atomically."""

        with self._lock:
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM compute_tasks WHERE owner = ? AND request_id = ?",
                    (owner, request_id),
                ).fetchone()
                if row is not None:
                    record = self._record(row)
                    if record.payload_hash != payload_hash:
                        raise ConflictError(
                            "request_id was already used with a different request",
                            code="idempotency_conflict",
                        )
                    connection.execute("COMMIT")
                    return record, False

                task_id = str(uuid.uuid4())
                marker = "determined-compute:" + str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO compute_tasks (
                        task_id, request_id, owner, payload_hash, profile_hash, kind,
                        state, code_revision, name, description, workdir, output_dir,
                        cluster_identity, submission_marker
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        request_id,
                        owner,
                        payload_hash,
                        profile_hash,
                        kind,
                        code_revision,
                        name,
                        description,
                        workdir,
                        output_dir,
                        cluster_identity,
                        marker,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM compute_tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                connection.execute("COMMIT")
                assert row is not None
                return self._record(row), True
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def lookup_request(self, request_id: str, owner: str) -> Optional[TaskRecord]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM compute_tasks WHERE owner = ? AND request_id = ?",
                (owner, request_id),
            ).fetchone()
        return self._record(row) if row is not None else None

    @staticmethod
    def _matching_remote_records(
        connection: sqlite3.Connection,
        *,
        owner: str,
        kind: str,
        remote_id: str,
        cluster_identity: Optional[str],
        remote_cluster_id: Optional[str],
    ) -> List[TaskRecord]:
        rows = connection.execute(
            """SELECT * FROM compute_tasks
            WHERE owner = ? AND kind = ? AND remote_id = ?""",
            (owner, kind, remote_id),
        ).fetchall()
        records = [SQLiteTaskStore._record(row) for row in rows]
        return [
            record
            for record in records
            if (
                record.origin == "adopted"
                and record.remote_cluster_id == remote_cluster_id
            )
            or (
                record.origin == "submitted"
                and record.cluster_identity == cluster_identity
            )
        ]

    @staticmethod
    def _one_remote_record(records: List[TaskRecord]) -> Optional[TaskRecord]:
        if len(records) > 1:
            raise ConflictError(
                "multiple local tasks match the same remote task identity",
                code="remote_identity_conflict",
            )
        return records[0] if records else None

    def lookup_remote(
        self,
        *,
        owner: str,
        kind: str,
        remote_id: str,
        cluster_identity: Optional[str],
        remote_cluster_id: Optional[str],
    ) -> Optional[TaskRecord]:
        with self._lock:
            records = self._matching_remote_records(
                self._connection,
                owner=owner,
                kind=kind,
                remote_id=remote_id,
                cluster_identity=cluster_identity,
                remote_cluster_id=remote_cluster_id,
            )
        return self._one_remote_record(records)

    def adopt(
        self,
        *,
        owner: str,
        kind: str,
        remote_id: str,
        remote_state: Optional[str],
        cluster_identity: Optional[str],
        remote_cluster_id: Optional[str],
        remote_user_id: Optional[str],
        profile_hash: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        submission_marker: Optional[str] = None,
        legacy_submission_markers: Tuple[str, ...] = (),
    ) -> Tuple[TaskRecord, bool]:
        """Create or return a local binding for an existing remote task."""

        with self._lock:
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                if submission_marker is not None or legacy_submission_markers:
                    rows = connection.execute(
                        """SELECT * FROM compute_tasks
                        WHERE owner = ? AND kind = ? AND cluster_identity IS ?
                          AND origin = 'submitted' AND remote_id IS NULL
                          AND state IN ('pending', 'submitting', 'submission_uncertain')""",
                        (owner, kind, cluster_identity),
                    ).fetchall()
                    pending = next(
                        (
                            record
                            for record in (self._record(row) for row in rows)
                            if record.submission_marker == submission_marker
                            or (
                                record.name is None
                                and record.description is None
                                and record.submission_marker
                                in legacy_submission_markers
                            )
                        ),
                        None,
                    )
                    if pending is not None:
                        error = ConflictError(
                            "remote task matches an unresolved local submission; "
                            "use reconcile instead of adopt",
                            code="reconcile_required",
                        )
                        error.details = {
                            "task_id": pending.task_id,
                            "action": "reconcile",
                        }
                        error.task_id = pending.task_id
                        raise error
                records = self._matching_remote_records(
                    connection,
                    owner=owner,
                    kind=kind,
                    remote_id=remote_id,
                    cluster_identity=cluster_identity,
                    remote_cluster_id=remote_cluster_id,
                )
                existing = self._one_remote_record(records)
                if existing is not None:
                    if (
                        existing.origin == "adopted"
                        and existing.remote_user_id != remote_user_id
                    ):
                        raise ConflictError(
                            "remote task belongs to a different remote user",
                            code="ownership_mismatch",
                        )
                    connection.execute("COMMIT")
                    return existing, False

                task_id = str(uuid.uuid4())
                request_id = "adopt:" + str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO compute_tasks (
                        task_id, request_id, owner, payload_hash, profile_hash, kind,
                        state, remote_id, remote_state, code_revision, name,
                        description, workdir, output_dir, cluster_identity,
                        submission_marker, origin, remote_user_id, remote_cluster_id
                    ) VALUES (
                        ?, ?, ?, '', ?, ?, 'adopted', ?, ?, NULL, ?, ?, '', '', ?,
                        '', 'adopted', ?, ?
                    )
                    """,
                    (
                        task_id,
                        request_id,
                        owner,
                        profile_hash,
                        kind,
                        remote_id,
                        remote_state,
                        name,
                        description,
                        cluster_identity,
                        remote_user_id,
                        remote_cluster_id,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM compute_tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                connection.execute("COMMIT")
                assert row is not None
                return self._record(row), True
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def get_owned(self, task_id: str, owner: str) -> TaskRecord:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM compute_tasks WHERE task_id = ? AND owner = ?", (task_id, owner)
            ).fetchone()
        if row is None:
            # Deliberately do not reveal whether another owner has this task id.
            raise NotFoundError("task not found")
        return self._record(row)

    def list_owned(self, owner: str) -> List[TaskRecord]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM compute_tasks WHERE owner = ?
                ORDER BY created_at DESC, task_id DESC""",
                (owner,),
            ).fetchall()
        return [self._record(row) for row in rows]

    def mark_submitting(self, task_id: str) -> TaskRecord:
        return self._update(task_id, state="submitting", error_code=None)

    def mark_submitted(self, task_id: str, remote_id: str) -> TaskRecord:
        return self._update(
            task_id, state="submitted", remote_id=remote_id, remote_state=None, error_code=None
        )

    def mark_failed(self, task_id: str, error_code: str) -> TaskRecord:
        return self._update(task_id, state="failed", error_code=error_code)

    def mark_uncertain(self, task_id: str) -> TaskRecord:
        return self._update(
            task_id, state="submission_uncertain", error_code="submission_uncertain"
        )

    def update_remote_state(self, task_id: str, remote_state: Optional[str]) -> TaskRecord:
        return self._update(task_id, remote_state=remote_state)

    def mark_stale_submission_uncertain(
        self, task_id: str, owner: str, stale_after_seconds: int
    ) -> TaskRecord:
        """Conservatively recover a launch record whose submitter stopped updating it."""

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
        cutoff_text = cutoff.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        with self._lock:
            self._connection.execute(
                """
                UPDATE compute_tasks
                SET state = 'submission_uncertain', error_code = 'submission_uncertain',
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE task_id = ? AND owner = ?
                  AND state IN ('pending', 'submitting') AND updated_at <= ?
                """,
                (task_id, owner, cutoff_text),
            )
            row = self._connection.execute(
                "SELECT * FROM compute_tasks WHERE task_id = ? AND owner = ?",
                (task_id, owner),
            ).fetchone()
        if row is None:
            raise NotFoundError("task not found")
        return self._record(row)

    def bind_reconciled(
        self, task_id: str, remote_id: str, remote_state: Optional[str]
    ) -> TaskRecord:
        return self._update(
            task_id,
            state="submitted",
            remote_id=remote_id,
            remote_state=remote_state,
            error_code=None,
        )

    def _update(self, task_id: str, **values: Optional[str]) -> TaskRecord:
        allowed = {"state", "remote_id", "remote_state", "error_code"}
        if not values or not set(values).issubset(allowed):
            raise ValueError("invalid task update")
        assignments = ", ".join(f"{key} = ?" for key in values)
        parameters = list(values.values()) + [task_id]
        with self._lock:
            cursor = self._connection.execute(
                f"""UPDATE compute_tasks SET {assignments},
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE task_id = ?""",
                parameters,
            )
            if cursor.rowcount != 1:
                raise NotFoundError("task not found")
            row = self._connection.execute(
                "SELECT * FROM compute_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        assert row is not None
        return self._record(row)


TaskStore = SQLiteTaskStore

__all__ = ["SQLiteTaskStore", "TaskStore"]
