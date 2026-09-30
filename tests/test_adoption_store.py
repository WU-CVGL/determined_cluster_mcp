from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from determined_compute.compute import ConflictError, SQLiteTaskStore


LOCAL_CLUSTER = '{"endpoint":"https://det.example","label":"cluster"}'


def _adopt(store, **overrides):
    values = {
        "owner": "session-a",
        "kind": "command",
        "remote_id": "remote-1",
        "remote_state": "RUNNING",
        "cluster_identity": LOCAL_CLUSTER,
        "remote_cluster_id": "cluster-id-1",
        "remote_user_id": "user-id-1",
        "profile_hash": "profile-hash",
        "name": "remote command",
        "description": "Adopted from Determined.",
    }
    values.update(overrides)
    return store.adopt(**values)


def _submitted(store, *, remote_id="remote-1", owner="session-a"):
    record, created = store.claim(
        request_id="original-request",
        owner=owner,
        payload_hash="payload-hash",
        profile_hash="profile-hash",
        kind="command",
        code_revision="revision",
        workdir="/shared/code",
        output_dir="/shared/output",
        cluster_identity=LOCAL_CLUSTER,
        name="submitted command",
        description="Original local submission.",
    )
    assert created
    return store.mark_submitted(record.task_id, remote_id)


def test_legacy_schema_migration_preserves_submitted_record(tmp_path):
    database = tmp_path / "legacy.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE compute_tasks (
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
            workdir TEXT NOT NULL,
            output_dir TEXT NOT NULL,
            cluster_identity TEXT,
            submission_marker TEXT NOT NULL,
            error_code TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(owner, request_id)
        );
        INSERT INTO compute_tasks VALUES (
            'task-1', 'request-1', 'session-a', 'payload', 'profile',
            'command', 'submitted', 'remote-1', 'RUNNING', 'revision',
            '/shared/code', '/shared/output', 'cluster', 'marker', NULL,
            '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z'
        );
        """
    )
    connection.close()

    store = SQLiteTaskStore(database)
    record = store.get_owned("task-1", "session-a")

    assert record.origin == "submitted"
    assert record.name is None
    assert record.description is None
    assert record.remote_user_id is None
    assert record.remote_cluster_id is None
    assert record.workdir == "/shared/code"
    assert record.output_dir == "/shared/output"
    assert record.payload_hash == "payload"
    columns = {
        row[1]
        for row in sqlite3.connect(database).execute("PRAGMA table_info(compute_tasks)")
    }
    assert {
        "name",
        "description",
        "origin",
        "remote_user_id",
        "remote_cluster_id",
    }.issubset(columns)
    store.close()

    # A column added by a newer release does not stop this release from reading rows.
    connection = sqlite3.connect(database)
    connection.execute("ALTER TABLE compute_tasks ADD COLUMN future_field TEXT")
    connection.commit()
    connection.close()
    reopened = SQLiteTaskStore(database)
    assert reopened.get_owned("task-1", "session-a").remote_id == "remote-1"
    assert [item.task_id for item in reopened.list_owned("session-a")] == ["task-1"]


def test_adopt_stores_unknown_paths_as_empty_and_repeats_without_changes(tmp_path):
    database = tmp_path / "tasks.db"
    store = SQLiteTaskStore(database)

    record, created = _adopt(store)

    assert created is True
    assert record.origin == "adopted"
    assert record.state == "adopted"
    assert record.workdir is None
    assert record.output_dir is None
    assert record.code_revision is None
    assert record.payload_hash == ""
    assert record.submission_marker == ""
    assert record.request_id.startswith("adopt:")
    assert record.remote_user_id == "user-id-1"
    assert record.remote_cluster_id == "cluster-id-1"
    public = record.public_dict()
    assert public["workdir"] is None
    assert public["output_dir"] is None
    raw = sqlite3.connect(database).execute(
        "SELECT workdir, output_dir FROM compute_tasks WHERE task_id = ?",
        (record.task_id,),
    ).fetchone()
    assert raw == ("", "")

    again, again_created = _adopt(
        store,
        remote_state="TERMINATED",
        name="replacement name",
        description="replacement description",
    )

    assert again_created is False
    assert again.task_id == record.task_id
    assert again.request_id == record.request_id
    assert again.remote_state == "RUNNING"
    assert again.name == "remote command"
    assert again.description == "Adopted from Determined."


def test_concurrent_adopt_across_connections_creates_one_record(tmp_path):
    database = tmp_path / "tasks.db"
    SQLiteTaskStore(database).close()
    stores = [SQLiteTaskStore(database) for _ in range(8)]

    with ThreadPoolExecutor(max_workers=len(stores)) as executor:
        results = list(executor.map(lambda store: _adopt(store), stores))

    records = [record for record, _created in results]
    assert len({record.task_id for record in records}) == 1
    assert sum(created for _record, created in results) == 1
    assert len(stores[0].list_owned("session-a")) == 1


def test_remote_identity_is_separated_by_owner_kind_and_cluster(tmp_path):
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    first, _ = _adopt(store)
    other_owner, _ = _adopt(store, owner="session-b")
    other_kind, _ = _adopt(store, kind="shell")
    other_cluster, _ = _adopt(store, remote_cluster_id="cluster-id-2")

    assert len({first.task_id, other_owner.task_id, other_kind.task_id, other_cluster.task_id}) == 4
    assert (
        store.lookup_remote(
            owner="session-a",
            kind="command",
            remote_id="remote-1",
            cluster_identity=LOCAL_CLUSTER,
            remote_cluster_id="cluster-id-1",
        ).task_id
        == first.task_id
    )
    assert (
        store.lookup_remote(
            owner="session-b",
            kind="command",
            remote_id="remote-1",
            cluster_identity=LOCAL_CLUSTER,
            remote_cluster_id="cluster-id-1",
        ).task_id
        == other_owner.task_id
    )
    assert (
        store.lookup_remote(
            owner="session-a",
            kind="command",
            remote_id="remote-1",
            cluster_identity=LOCAL_CLUSTER,
            remote_cluster_id="missing-cluster",
        )
        is None
    )


def test_submitted_binding_is_found_by_local_cluster_and_never_replaced_by_adopt(tmp_path):
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    submitted = _submitted(store)

    # A submitted record is matched by its local cluster identity; the remote cluster
    # id is not part of its binding.
    found = store.lookup_remote(
        owner="session-a",
        kind="command",
        remote_id="remote-1",
        cluster_identity=LOCAL_CLUSTER,
        remote_cluster_id="ignored-for-submitted",
    )
    assert found.task_id == submitted.task_id
    assert (
        store.lookup_remote(
            owner="session-a",
            kind="command",
            remote_id="remote-1",
            cluster_identity="different-local-cluster",
            remote_cluster_id="ignored-for-submitted",
        )
        is None
    )

    record, created = _adopt(
        store,
        remote_state="TERMINATED",
        remote_cluster_id="unrelated-remote-cluster-id",
        remote_user_id="different-user",
        name="replacement",
    )

    assert created is False
    assert record.task_id == submitted.task_id
    assert record.origin == "submitted"
    assert record.state == "submitted"
    assert record.remote_state is None
    assert record.remote_user_id is None
    assert record.name == "submitted command"
    assert record.request_id == "original-request"


def test_adopted_remote_user_mismatch_is_rejected(tmp_path):
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    _adopt(store, remote_user_id="known-user")

    with pytest.raises(ConflictError) as caught:
        _adopt(store, remote_user_id="different-user")

    assert caught.value.code == "ownership_mismatch"
    assert len(store.list_owned("session-a")) == 1


def test_lookup_remote_rejects_historical_duplicate_identity(tmp_path):
    database = tmp_path / "tasks.db"
    store = SQLiteTaskStore(database)
    first, _ = _adopt(store)
    connection = sqlite3.connect(database)
    connection.execute(
        """
        INSERT INTO compute_tasks (
            task_id, request_id, owner, payload_hash, profile_hash, kind, state,
            remote_id, remote_state, code_revision, name, description, workdir,
            output_dir, cluster_identity, submission_marker, origin,
            remote_user_id, remote_cluster_id
        ) VALUES (?, ?, ?, '', ?, ?, 'adopted', ?, ?, NULL, NULL, NULL, '', '',
                  ?, '', 'adopted', ?, ?)
        """,
        (
            "duplicate-task",
            "adopt:duplicate",
            "session-a",
            "profile-hash",
            "command",
            "remote-1",
            "RUNNING",
            LOCAL_CLUSTER,
            "user-id-1",
            "cluster-id-1",
        ),
    )
    connection.commit()
    connection.close()

    with pytest.raises(ConflictError) as caught:
        store.lookup_remote(
            owner="session-a",
            kind="command",
            remote_id="remote-1",
            cluster_identity=LOCAL_CLUSTER,
            remote_cluster_id="cluster-id-1",
        )

    assert caught.value.code == "remote_identity_conflict"
    assert first.task_id != "duplicate-task"
