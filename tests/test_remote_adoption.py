from __future__ import annotations

import copy
import sqlite3

import pytest

from determined_compute.compute import (
    APIError,
    ComputeProfile,
    ComputeService,
    ConflictError,
    SQLiteTaskStore,
    ValidationError,
)
from determined_compute.core.api_client import DeterminedAPIClient


COMMAND_ID = "12345678-1234-5678-9234-567812345678"
OTHER_COMMAND_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class FakeClient:
    api_url = "https://det.example.test"

    def __init__(self) -> None:
        self.cluster_id = "cluster-1"
        self.user = {"id": "7", "username": "alice"}
        self.calls = []
        self.entities = {}
        self.pages = {}

    def get_cluster_id(self):
        self.calls.append(("GET", "info"))
        return self.cluster_id

    def get_current_user(self):
        self.calls.append(("GET", "api/v1/me"))
        return copy.deepcopy(self.user)

    def list_remote_tasks(self, kind, *, user_id, limit, offset):
        self.calls.append(
            (
                "GET",
                f"api/v1/{kind}s",
                {"user_id": user_id, "limit": limit, "offset": offset},
            )
        )
        return copy.deepcopy(
            self.pages.get(
                kind,
                {
                    "tasks": [],
                    "pagination": {"limit": limit, "offset": offset, "total": 0},
                },
            )
        )

    def get_task(self, kind, remote_id):
        self.calls.append(("GET", f"api/v1/{kind}s/{remote_id}"))
        return copy.deepcopy(self.entities[(kind, remote_id)])

    def task_logs(self, kind, remote_id, tail):
        self.calls.append(("GET", f"api/v1/{kind}s/{remote_id}/logs", {"tail": tail}))
        return [{"message": "remote log"}]

    def cancel_task(self, kind, remote_id):
        self.calls.append(("POST", f"api/v1/{kind}s/{remote_id}/cancel"))
        return {"id": remote_id, "state": "TERMINATING"}

    def task_resources_enabled(self):
        self.calls.append(("GET", "api/v1/task-resources/capability"))
        return True

    def get_task_info(self, task_id):
        self.calls.append(("GET", f"api/v1/tasks/{task_id}"))
        return {
            "task_id": task_id,
            "start_time": "2026-09-20T00:00:00Z",
            "end_time": None,
            "allocations": [],
        }

    def get_task_resources(self, task_id, *, start, end, step, allocation_id=None):
        self.calls.append(("GET", f"api/v1/tasks/{task_id}/resources"))
        return {"enabled": True, "series": [], "warnings": []}

    def list_resource_pools(self):
        self.calls.append(("GET", "api/v1/resource-pools"))
        return [{"name": "gpu", "description": "shared GPUs"}]

    def get_allocation(self, allocation_id):
        self.calls.append(("GET", f"api/v1/allocations/{allocation_id}"))
        return {"allocation_id": allocation_id, "slots": 1, "exit_reason": None,
                "status_code": None}

    def list_gpu_devices(self):
        self.calls.append(("GET", "api/v1/agents"))
        return {}

    def launch_task(self, kind, config):
        self.calls.append(("POST", f"api/v1/{kind}s", copy.deepcopy(config)))
        raise AssertionError("adoption must never launch a task")


@pytest.fixture
def profile():
    return ComputeProfile.from_dict(
        {
            "mounts": [
                {"host_path": "/shared/host", "container_path": "/shared/container"}
            ],
            "defaults": {"image": "image:stable", "pool": "gpu", "slots": 1},
            "cluster_identity": "configured-cluster",
        }
    )


def service_for(tmp_path, profile, client=None, database="tasks.db"):
    client = client or FakeClient()
    store = SQLiteTaskStore(tmp_path / database)
    return ComputeService(client, store, profile), client, store


def command_entity(remote_id=COMMAND_ID, **overrides):
    entity = {
        "id": remote_id,
        "userId": 7,
        "username": "alice",
        "name": "remote command",
        "description": "safe display text",
        "state": "RUNNING",
        "resourcePool": "gpu",
        "startTime": "2026-09-20T00:00:00Z",
    }
    entity.update(overrides)
    return entity


def test_discover_is_owner_filtered_bounded_and_metadata_only(tmp_path, profile):
    service, client, _store = service_for(tmp_path, profile)
    secret = "PRIVATE_KEY_MATERIAL_9138"
    entity = command_entity(
        config={"environment": {"TOKEN": secret}},
        privateKey=secret,
        rawConfig=secret,
    )
    client.pages["command"] = {
        "tasks": [entity],
        "pagination": {"limit": 7, "offset": 2, "total": 3},
    }

    result = service.discover("command", "session-a", limit=7, offset=2)

    assert client.calls == [
        ("GET", "info"),
        ("GET", "api/v1/me"),
        (
            "GET",
            "api/v1/commands",
            {"user_id": "7", "limit": 7, "offset": 2},
        ),
    ]
    assert result["account"] == {"id": "7", "username": "alice"}
    assert result["pagination"] == {
        "offset": 2,
        "limit": 7,
        "total": 3,
        "next_offset": None,
    }
    assert result["tasks"] == [
        {
            "kind": "command",
            "remote_id": COMMAND_ID,
            "name": "remote command",
            "description": "safe display text",
            "remote_state": "RUNNING",
            "remote_user_id": "7",
            "remote_username": "alice",
            "resource_pool": "gpu",
            "start_time": "2026-09-20T00:00:00Z",
            "local_task_id": None,
        }
    ]
    assert secret not in repr(result)
    assert service.list_tasks("session-a") == []


@pytest.mark.parametrize(
    ("kind", "remote_id"),
    [
        ("notebook", COMMAND_ID),
        ("command", "not-a-uuid"),
        ("shell", 123),
        ("experiment", "0"),
        ("experiment", "-1"),
        ("experiment", "1.5"),
    ],
)
def test_adopt_rejects_unsupported_kinds_and_invalid_ids_before_network(
    tmp_path, profile, kind, remote_id
):
    service, client, _store = service_for(tmp_path, profile)

    with pytest.raises(ValidationError):
        service.adopt(kind, remote_id, "session-a")

    assert client.calls == []


@pytest.mark.parametrize(
    ("limit", "offset"),
    [(0, 0), (101, 0), (True, 0), (50, -1), (50, True)],
)
def test_discover_rejects_unbounded_pagination_before_network(
    tmp_path, profile, limit, offset
):
    service, client, _store = service_for(tmp_path, profile)

    with pytest.raises(ValidationError):
        service.discover("command", "session-a", limit=limit, offset=offset)

    assert client.calls == []


def test_discover_propagates_identity_authentication_failure(tmp_path, profile):
    service, client, _store = service_for(tmp_path, profile)

    def denied():
        client.calls.append(("GET", "api/v1/me"))
        raise APIError("denied", code=401)

    client.get_current_user = denied

    with pytest.raises(APIError) as caught:
        service.discover("command", "session-a")

    assert caught.value.code == 401
    assert not any(call[1] == "api/v1/commands" for call in client.calls)


@pytest.mark.parametrize(
    "user",
    [
        {"username": "alice", "projectOwnerId": 7},
        {"id": "alice", "username": "alice"},
        {"id": 0, "username": "alice"},
        {"id": 7, "username": ""},
    ],
)
def test_identity_requires_numeric_user_id_and_does_not_use_fallback_fields(
    tmp_path, profile, user
):
    service, client, _store = service_for(tmp_path, profile)
    client.user = user

    with pytest.raises(APIError) as caught:
        service.discover("command", "session-a")

    assert caught.value.code == "invalid_response"
    assert not any(call[1] == "api/v1/commands" for call in client.calls)


def test_discover_rejects_server_results_owned_by_another_user(tmp_path, profile):
    service, client, _store = service_for(tmp_path, profile)
    client.pages["command"] = {
        "tasks": [command_entity(userId=8, username="bob")],
        "pagination": {"limit": 50, "offset": 0, "total": 1},
    }

    with pytest.raises(ConflictError) as caught:
        service.discover("command", "session-a")

    assert caught.value.code == "ownership_mismatch"
    assert service.list_tasks("session-a") == []


@pytest.mark.parametrize(
    "pagination",
    [
        {"limit": 49, "offset": 0, "total": 0},
        {"limit": 50, "offset": 1, "total": 0},
        {"limit": 50, "offset": 0, "total": -1},
    ],
)
def test_discover_rejects_inconsistent_server_pagination(
    tmp_path, profile, pagination
):
    service, client, _store = service_for(tmp_path, profile)
    client.pages["command"] = {"tasks": [], "pagination": pagination}

    with pytest.raises(APIError) as caught:
        service.discover("command", "session-a")

    assert caught.value.code == "invalid_response"


def test_adopt_only_registers_whitelisted_metadata_and_is_idempotent(tmp_path, profile):
    service, client, store = service_for(tmp_path, profile)
    secret = "RAW_REMOTE_SECRET_7913"
    client.entities[("command", COMMAND_ID)] = command_entity(
        config={"environment": {"PASSWORD": secret}},
        privateKeys=[secret],
        rawConfig=secret,
    )

    first = service.adopt("command", "{12345678-1234-5678-9234-567812345678}", "session-a")
    second = service.adopt("command", COMMAND_ID.upper(), "session-a")

    assert first["task_id"] == second["task_id"]
    assert first["origin"] == "adopted"
    assert first["state"] == "adopted"
    assert first["remote_id"] == COMMAND_ID
    assert first["workdir"] is None and first["output_dir"] is None
    assert len(store.list_owned("session-a")) == 1
    assert secret.encode() not in (tmp_path / "tasks.db").read_bytes()
    assert not any(call[0] == "POST" for call in client.calls)

    other_service, other_client, other_store = service_for(
        tmp_path, profile, database="other.db"
    )
    other_client.entities[("command", COMMAND_ID)] = command_entity()
    other = other_service.adopt("command", COMMAND_ID, "session-a")
    assert other["task_id"] != first["task_id"]
    assert len(other_store.list_owned("session-a")) == 1


def test_experiment_ids_are_canonical_positive_integers(tmp_path, profile):
    service, client, _store = service_for(tmp_path, profile)
    client.entities[("experiment", "7")] = {
        "id": 7,
        "userId": "7",
        "name": "experiment",
        "state": "ACTIVE",
    }

    adopted = service.adopt("experiment", "0007", "session-a")

    assert adopted["remote_id"] == "7"
    assert ("GET", "api/v1/experiments/7") in client.calls


def test_adopt_rejects_missing_or_different_remote_owner(tmp_path, profile):
    service, client, store = service_for(tmp_path, profile)
    client.entities[("command", COMMAND_ID)] = command_entity(
        userId=None, projectOwnerId=7
    )

    with pytest.raises(APIError) as missing:
        service.adopt("command", COMMAND_ID, "session-a")
    assert missing.value.code == "ownership_unavailable"

    client.entities[("command", COMMAND_ID)] = command_entity(userId=8)
    with pytest.raises(ConflictError) as mismatch:
        service.adopt("command", COMMAND_ID, "session-a")
    assert mismatch.value.code == "ownership_mismatch"
    assert store.list_owned("session-a") == []


def test_adopt_preserves_an_existing_submitted_request_binding(tmp_path, profile):
    service, client, store = service_for(tmp_path, profile)
    original, created = store.claim(
        request_id="original-request",
        owner="session-a",
        payload_hash="original-payload-hash",
        profile_hash=profile.fingerprint,
        kind="command",
        code_revision="git:abc",
        workdir="/shared/container/code",
        output_dir="/shared/container/output",
        cluster_identity=service._cluster_identity(),
        name="original name",
        description="original description",
    )
    assert created is True
    original = store.mark_submitted(original.task_id, COMMAND_ID)
    client.entities[("command", COMMAND_ID)] = command_entity(
        name="replacement name", description="replacement description"
    )

    adopted = service.adopt("command", COMMAND_ID, "session-a")
    preserved = store.get_owned(original.task_id, "session-a")

    assert adopted["task_id"] == original.task_id
    assert adopted["origin"] == "submitted"
    assert preserved.request_id == "original-request"
    assert preserved.payload_hash == "original-payload-hash"
    assert preserved.profile_hash == profile.fingerprint
    assert preserved.workdir == "/shared/container/code"
    assert preserved.output_dir == "/shared/container/output"
    assert preserved.name == "original name"
    assert preserved.description == "original description"
    assert len(store.list_owned("session-a")) == 1


@pytest.mark.parametrize("state", ["pending", "submitting", "submission_uncertain"])
def test_adopt_marker_match_requires_reconcile_without_creating_duplicate(
    tmp_path, profile, state
):
    service, client, store = service_for(tmp_path, profile)
    pending, _created = store.claim(
        request_id=f"request-{state}",
        owner="session-a",
        payload_hash="payload",
        profile_hash=profile.fingerprint,
        kind="command",
        code_revision="git:abc",
        workdir="/shared/container/code",
        output_dir="/shared/container/output",
        cluster_identity=service._cluster_identity(),
    )
    if state == "submitting":
        pending = store.mark_submitting(pending.task_id)
    elif state == "submission_uncertain":
        pending = store.mark_uncertain(pending.task_id)
    client.entities[("command", COMMAND_ID)] = command_entity(
        submissionMarker=pending.submission_marker
    )

    with pytest.raises(ConflictError) as caught:
        service.adopt("command", COMMAND_ID, "session-a")

    assert caught.value.code == "reconcile_required"
    assert caught.value.details == {"task_id": pending.task_id, "action": "reconcile"}
    assert caught.value.task_id == pending.task_id
    records = store.list_owned("session-a")
    assert len(records) == 1
    assert records[0].task_id == pending.task_id
    assert records[0].remote_id is None


def test_marker_match_in_another_database_can_be_adopted(tmp_path, profile):
    source_service, _source_client, source_store = service_for(
        tmp_path, profile, database="source.db"
    )
    pending, _created = source_store.claim(
        request_id="uncertain-request",
        owner="session-a",
        payload_hash="payload",
        profile_hash=profile.fingerprint,
        kind="command",
        code_revision=None,
        workdir="/shared/container/code",
        output_dir="/shared/container/output",
        cluster_identity=source_service._cluster_identity(),
    )
    source_store.mark_uncertain(pending.task_id)

    service, client, store = service_for(tmp_path, profile, database="independent.db")
    client.entities[("command", COMMAND_ID)] = command_entity(
        submissionMarker=pending.submission_marker
    )

    adopted = service.adopt("command", COMMAND_ID, "session-a")

    assert adopted["origin"] == "adopted"
    assert len(store.list_owned("session-a")) == 1


def test_legacy_first_line_marker_requires_reconcile_for_metadata_less_record(
    tmp_path, profile
):
    service, client, store = service_for(tmp_path, profile)
    pending, _created = store.claim(
        request_id="legacy-uncertain",
        owner="session-a",
        payload_hash="legacy-payload",
        profile_hash=profile.fingerprint,
        kind="command",
        code_revision=None,
        workdir="/shared/container/code",
        output_dir="/shared/container/output",
        cluster_identity=service._cluster_identity(),
    )
    pending = store.mark_uncertain(pending.task_id)
    assert pending.name is None and pending.description is None
    client.entities[("command", COMMAND_ID)] = command_entity(
        description=pending.submission_marker + "\nlegacy display text"
    )

    with pytest.raises(ConflictError) as caught:
        service.adopt("command", COMMAND_ID, "session-a")

    assert caught.value.code == "reconcile_required"
    assert caught.value.details["task_id"] == pending.task_id
    assert len(store.list_owned("session-a")) == 1


@pytest.mark.parametrize(
    ("pending_kind", "name", "description"),
    [
        ("shell", None, None),
        ("command", "modern task", None),
        ("command", None, "modern description"),
    ],
)
def test_legacy_description_marker_does_not_match_other_kind_or_modern_metadata(
    tmp_path, profile, pending_kind, name, description
):
    service, client, store = service_for(tmp_path, profile)
    pending, _created = store.claim(
        request_id="not-a-legacy-match",
        owner="session-a",
        payload_hash="payload",
        profile_hash=profile.fingerprint,
        kind=pending_kind,
        code_revision=None,
        workdir="/shared/container/code",
        output_dir="/shared/container/output",
        cluster_identity=service._cluster_identity(),
        name=name,
        description=description,
    )
    store.mark_uncertain(pending.task_id)
    client.entities[("command", COMMAND_ID)] = command_entity(
        description=pending.submission_marker + "\nremote display text"
    )

    adopted = service.adopt("command", COMMAND_ID, "session-a")

    assert adopted["origin"] == "adopted"
    assert adopted["task_id"] != pending.task_id
    assert len(store.list_owned("session-a")) == 2


def test_adopted_management_uses_live_cluster_and_account_not_profile_hash(
    tmp_path, profile
):
    service, client, _store = service_for(tmp_path, profile)
    client.entities[("command", COMMAND_ID)] = command_entity()
    adopted = service.adopt("command", COMMAND_ID, "session-a")

    changed_profile = ComputeProfile.from_dict(
        {
            "mounts": [{"host_path": "/other", "container_path": "/other"}],
            "defaults": {"image": "other:image", "pool": "cpu", "slots": 0},
            "cluster_identity": "renamed-profile",
        }
    )
    restarted = ComputeService(client, service.store, changed_profile)
    client.entities[("command", COMMAND_ID)]["state"] = "COMPLETED"

    status = restarted.status(adopted["task_id"], "session-a")
    logs = restarted.logs(adopted["task_id"], "session-a", tail=12)
    usage = restarted.usage(adopted["task_id"], "session-a")
    cancelled = restarted.cancel(adopted["task_id"], "session-a")

    assert status["remote_state"] == "COMPLETED"
    assert logs == [{"message": "remote log"}]
    assert usage["determined_task_id"] == COMMAND_ID
    assert ("GET", f"api/v1/tasks/{COMMAND_ID}/resources") in client.calls
    assert usage["resource_pool"] == {"name": "gpu", "description": "shared GPUs"}
    assert usage["context_unavailable"] == []
    assert cancelled["remote_state"] == "TERMINATING"
    assert any(call[0] == "POST" for call in client.calls)


@pytest.mark.parametrize(
    ("operation", "identity_change", "error_code"),
    [
        ("status", "cluster", "binding_mismatch"),
        ("logs", "cluster", "binding_mismatch"),
        ("cancel", "cluster", "binding_mismatch"),
        ("usage", "cluster", "binding_mismatch"),
        ("status", "account", "ownership_mismatch"),
        ("logs", "account", "ownership_mismatch"),
        ("cancel", "account", "ownership_mismatch"),
        ("usage", "account", "ownership_mismatch"),
    ],
)
def test_management_identity_changes_fail_before_remote_task_access(
    tmp_path, profile, operation, identity_change, error_code
):
    service, client, _store = service_for(tmp_path, profile)
    client.entities[("command", COMMAND_ID)] = command_entity()
    adopted = service.adopt("command", COMMAND_ID, "session-a")
    client.calls.clear()
    if identity_change == "cluster":
        client.cluster_id = "cluster-2"
    else:
        client.user = {"id": "8", "username": "bob"}

    with pytest.raises(ConflictError) as caught:
        getattr(service, operation)(adopted["task_id"], "session-a")

    assert caught.value.code == error_code
    assert client.calls == [("GET", "info"), ("GET", "api/v1/me")]


@pytest.mark.parametrize("operation", ["status", "logs", "cancel", "usage"])
def test_management_remote_owner_change_fails_before_logs_or_cancel(
    tmp_path, profile, operation
):
    service, client, _store = service_for(tmp_path, profile)
    client.entities[("command", COMMAND_ID)] = command_entity()
    adopted = service.adopt("command", COMMAND_ID, "session-a")
    client.entities[("command", COMMAND_ID)]["userId"] = 8
    client.calls.clear()

    with pytest.raises(ConflictError) as caught:
        getattr(service, operation)(adopted["task_id"], "session-a")

    assert caught.value.code == "ownership_mismatch"
    assert client.calls == [
        ("GET", "info"),
        ("GET", "api/v1/me"),
        ("GET", f"api/v1/commands/{COMMAND_ID}"),
    ]
    assert not any(
        call[0] == "POST" or call[1].endswith(("/logs", "/resources", "/capability"))
        for call in client.calls
    )


def test_adopted_task_cannot_be_reconciled_or_used_as_launch_retry(tmp_path, profile):
    service, client, _store = service_for(tmp_path, profile)
    client.entities[("command", COMMAND_ID)] = command_entity()
    adopted = service.adopt("command", COMMAND_ID, "session-a")

    with pytest.raises(ConflictError) as reconcile:
        service.reconcile(adopted["task_id"], "session-a", OTHER_COMMAND_ID)
    assert reconcile.value.code == "invalid_operation"

    request = {
        "kind": "command",
        "command": ["true"],
        "workdir": "/shared/container/code",
        "output_dir": "/shared/container/output",
        "allow_queue": True,
    }
    with pytest.raises(ConflictError) as retry:
        service.launch(request, adopted["request_id"], "session-a")
    assert retry.value.code == "idempotency_conflict"
    assert not any(call[0] == "POST" for call in client.calls)


def test_discover_does_not_link_an_adoption_from_a_different_remote_account(
    tmp_path, profile
):
    service, client, store = service_for(tmp_path, profile)
    client.entities[("command", COMMAND_ID)] = command_entity()
    original = service.adopt("command", COMMAND_ID, "session-a")
    assert store.get_owned(original["task_id"], "session-a").remote_user_id == "7"

    client.user = {"id": "8", "username": "bob"}
    client.pages["command"] = {
        "tasks": [command_entity(userId=8, username="bob")],
        "pagination": {"limit": 50, "offset": 0, "total": 1},
    }

    discovered = service.discover("command", "session-a")

    assert discovered["tasks"][0]["local_task_id"] is None


def test_adopted_database_has_no_raw_remote_config_columns_or_values(tmp_path, profile):
    service, client, _store = service_for(tmp_path, profile)
    secret = "PEM_PRIVATE_KEY_9812"
    client.entities[("command", COMMAND_ID)] = command_entity(
        config={"environment": {"SECRET": secret}}, rawConfig=secret, privateKeys=secret
    )

    service.adopt("command", COMMAND_ID, "session-a")

    connection = sqlite3.connect(tmp_path / "tasks.db")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(compute_tasks)")}
    connection.close()
    assert {"config", "raw_config", "environment", "private_keys"}.isdisjoint(columns)
    assert secret.encode() not in (tmp_path / "tasks.db").read_bytes()


def test_experiment_detail_omits_opaque_raw_configs_and_redacts_parsed_config(
    monkeypatch,
):
    secret = "ORIGINAL_CONFIG_SECRET_6371"
    client = DeterminedAPIClient(
        api_url="https://det.example.test", api_token="test-token"
    )
    monkeypatch.setattr(
        client,
        "_get",
        lambda _endpoint: {
            "experiment": {
                "id": 9,
                "userId": 7,
                "state": "ACTIVE",
                "originalConfig": f"environment:\n  TOKEN: {secret}\n",
                "config": f"password: {secret}\n",
            },
            "config": (
                "name: safe-name\n"
                "environment:\n"
                "  environment_variables:\n"
                f"    - TOKEN={secret}\n"
            ),
        },
    )

    task = client.get_task("experiment", "9")

    assert task["id"] == 9
    assert task["config"] == {
        "name": "safe-name",
        "environment": {"environment_variables": "[redacted]"},
    }
    assert "originalConfig" not in task
    assert secret not in repr(task)


def test_adopted_usage_reuses_verified_entity_for_pool_context(tmp_path, profile):
    service, client, _store = service_for(tmp_path, profile)
    client.entities[("command", COMMAND_ID)] = command_entity()
    adopted = service.adopt("command", COMMAND_ID, "session-a")
    client.calls.clear()

    usage = service.usage(adopted["task_id"], "session-a")

    assert client.calls.count(("GET", f"api/v1/commands/{COMMAND_ID}")) == 1
    assert client.calls.index(("GET", f"api/v1/commands/{COMMAND_ID}")) < client.calls.index(
        ("GET", "api/v1/task-resources/capability")
    )
    assert usage["resource_pool"]["name"] == "gpu"
