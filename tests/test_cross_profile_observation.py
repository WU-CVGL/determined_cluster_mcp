from __future__ import annotations

import asyncio
import copy
import json
import sqlite3

import pytest

from determined_compute import compute_cli
from determined_compute.compute import (
    APIError,
    ComputeProfile,
    ComputeService,
    ConflictError,
    SQLiteTaskStore,
)

COMMAND_ID = "12345678-1234-5678-9234-567812345678"
SHELL_ID = "87654321-4321-4765-8321-876543218765"


class FakeClient:
    api_url = "https://det.example.test"

    def __init__(self):
        self.calls = []
        self.entities = {}
        self.entity_errors = {}
        self.user = {"id": "7", "username": "alice"}

    def launch_task(self, kind, config):
        self.calls.append(("POST", f"api/v1/{kind}s"))
        remote_id = {"command": COMMAND_ID, "shell": SHELL_ID}.get(kind, "41")
        marker = next(
            item.partition("=")[2]
            for item in config["environment"]["environment_variables"]
            if item.startswith("COMPUTE_SUBMISSION_MARKER=")
        )
        self.entities[(kind, remote_id)] = {
            "id": int(remote_id) if kind == "experiment" else remote_id,
            "userId": 7,
            "state": "RUNNING",
            "startTime": "2026-09-20T00:00:00Z",
            "submissionMarker": marker,
        }
        return {"id": remote_id}

    def get_current_user(self):
        self.calls.append(("GET", "api/v1/me"))
        return copy.deepcopy(self.user)

    def get_cluster_id(self):
        self.calls.append(("GET", "info"))
        return "cluster-1"

    def get_task(self, kind, remote_id):
        self.calls.append(("GET", f"api/v1/{kind}s/{remote_id}"))
        if (kind, remote_id) in self.entity_errors:
            raise self.entity_errors[(kind, remote_id)]
        return copy.deepcopy(self.entities[(kind, remote_id)])

    def task_logs(self, kind, remote_id, tail):
        self.calls.append(("GET", f"api/v1/{kind}s/{remote_id}/logs"))
        return [{"message": "remote log"}]

    def cancel_task(self, kind, remote_id):
        self.calls.append(("POST", f"api/v1/{kind}s/{remote_id}/kill"))
        return {"id": remote_id, "state": "TERMINATING"}

    def task_resources_enabled(self):
        self.calls.append(("GET", "api/v1/task-resources/capability"))
        return True

    def get_task_info(self, task_id):
        self.calls.append(("GET", f"api/v1/tasks/{task_id}"))
        return {"task_id": task_id, "start_time": "2026-09-20T00:00:00Z", "end_time": None,
                "allocations": []}

    def get_task_resources(self, task_id, *, start, end, step, allocation_id=None):
        self.calls.append(("GET", f"api/v1/tasks/{task_id}/resources"))
        return {"enabled": True, "series": [], "warnings": []}

    def list_resource_pools(self):
        return []

    def get_allocation(self, allocation_id):
        return {"allocation_id": allocation_id, "slots": 1, "exit_reason": None,
                "status_code": None}

    def list_gpu_devices(self):
        return {}


class Admission:
    def require_capacity(self, kind, config):
        return {"admitted": True}


def profile(image="image:stable", label="configured-cluster"):
    return ComputeProfile.from_dict(
        {
            "mounts": [{"host_path": "/shared/host", "container_path": "/shared/container"}],
            "defaults": {"image": image, "pool": "gpu", "slots": 1},
            "cluster_identity": label,
        }
    )


REQUEST = {
    "name": "evaluate",
    "command": "true",
    "workdir": "/shared/container/code",
    "output_dir": "/shared/container/out",
}


def submitted(tmp_path, request=REQUEST):
    client = FakeClient()
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    original = ComputeService(client, store, profile(), inspector=Admission())
    task = original.launch(request, "request-1", "session-a")
    client.calls.clear()
    return client, store, task


def other_profile_service(client, store, **changes):
    return ComputeService(client, store, profile(image="other/image:v2", **changes))


def rows(tmp_path):
    connection = sqlite3.connect(tmp_path / "tasks.db")
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM compute_tasks")]
    finally:
        connection.close()


CROSS_BINDING = {
    "mode": "cross_profile",
    "profile_matches": False,
    "cluster_identity_matches": True,
    "verified": ["remote_owner", "submission_marker"],
    "mutations_allowed": False,
    "message": "cancel, reconcile and launch retries require the task's original compute profile",
}


@pytest.mark.parametrize(
    ("operation", "extra_calls"),
    [
        ("status", []),
        ("logs", [("GET", f"api/v1/commands/{COMMAND_ID}/logs")]),
        (
            "usage",
            [
                ("GET", "api/v1/task-resources/capability"),
                ("GET", f"api/v1/tasks/{COMMAND_ID}"),
                ("GET", f"api/v1/tasks/{COMMAND_ID}/resources"),
            ],
        ),
    ],
)
def test_changed_profile_can_observe_after_owner_and_marker_checks(
    tmp_path, operation, extra_calls
):
    client, store, task = submitted(tmp_path)
    client.entities[("command", COMMAND_ID)]["state"] = "COMPLETED"
    service = other_profile_service(client, store)
    before = rows(tmp_path)

    if operation == "logs":
        result = service.logs(task["task_id"], "session-a", 20, include_binding=True)
        assert result["logs"] == [{"message": "remote log"}]
        assert result["task_id"] == task["task_id"]
    else:
        result = getattr(service, operation)(task["task_id"], "session-a")

    assert result["binding"] == CROSS_BINDING
    assert client.calls == [
        ("GET", "api/v1/me"),
        ("GET", f"api/v1/commands/{COMMAND_ID}"),
        *extra_calls,
    ]
    after = rows(tmp_path)
    if operation == "status":
        assert result["remote_state"] == "COMPLETED"
        assert result["remote"]["state"] == "COMPLETED"
        # Only the remote-state cache may change.
        for row in before + after:
            row.pop("remote_state")
            row.pop("updated_at")
    assert after == before


def test_default_logs_shape_is_unchanged_under_a_changed_profile(tmp_path):
    client, store, task = submitted(tmp_path)

    logs = other_profile_service(client, store).logs(task["task_id"], "session-a", 5)

    assert logs == [{"message": "remote log"}]


def test_exact_profile_observation_adds_no_remote_calls(tmp_path):
    client, store, task = submitted(tmp_path)
    service = ComputeService(client, store, profile())

    status = service.status(task["task_id"], "session-a")
    logs = service.logs(task["task_id"], "session-a", 5, include_binding=True)

    assert status["binding"] == {
        "mode": "profile",
        "profile_matches": True,
        "cluster_identity_matches": True,
        "verified": [],
        "mutations_allowed": True,
        "message": "bound to the current compute profile and endpoint",
    }
    assert logs["binding"]["mode"] == "profile"
    assert client.calls == [
        ("GET", f"api/v1/commands/{COMMAND_ID}"),
        ("GET", f"api/v1/commands/{COMMAND_ID}/logs"),
    ]


@pytest.mark.parametrize("operation", ["status", "logs", "usage"])
@pytest.mark.parametrize(
    ("change", "code"),
    [
        (
            {"submissionMarker": "determined-compute:00000000-0000-4000-8000-000000000000"},
            "identity_mismatch",
        ),
        ({"submissionMarker": None}, "identity_mismatch"),
        ({"userId": 8}, "ownership_mismatch"),
        ({"id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"}, "identity_mismatch"),
    ],
)
def test_cross_profile_verification_fails_before_logs_or_usage(
    tmp_path, operation, change, code
):
    client, store, task = submitted(tmp_path)
    client.entities[("command", COMMAND_ID)].update(change)
    before = rows(tmp_path)

    with pytest.raises(ConflictError) as caught:
        getattr(other_profile_service(client, store), operation)(task["task_id"], "session-a")

    assert caught.value.code == code
    assert client.calls == [("GET", "api/v1/me"), ("GET", f"api/v1/commands/{COMMAND_ID}")]
    assert rows(tmp_path) == before


SHELL_REQUEST = {
    **{key: value for key, value in REQUEST.items() if key != "command"},
    "kind": "shell",
    "interactive": True,
}
DROPPED = {
    "command": (REQUEST, COMMAND_ID),
    "shell": (SHELL_REQUEST, SHELL_ID),
    "experiment": ({**REQUEST, "kind": "experiment"}, "41"),
}


@pytest.mark.parametrize("operation", ["status", "logs", "usage"])
@pytest.mark.parametrize("kind", ["command", "shell", "experiment"])
def test_cross_profile_read_of_a_dropped_entity_is_unverifiable(tmp_path, kind, operation):
    request, remote_id = DROPPED[kind]
    client, store, task = submitted(tmp_path, request)
    # Determined drops an ended command or shell 24 hours after it ends or on a restart;
    # an experiment returns 404 once it is deleted or when it is not visible.
    client.entity_errors[(kind, remote_id)] = APIError("404 not found", code=404)
    before = rows(tmp_path)

    with pytest.raises(ConflictError) as caught:
        getattr(other_profile_service(client, store), operation)(task["task_id"], "session-a")

    assert caught.value.code == "cross_profile_unverifiable"
    assert caught.value.retryable is False
    assert "original compute profile" in str(caught.value)
    assert isinstance(caught.value.__cause__, APIError)
    assert caught.value.__cause__.code == 404
    # No task logs, task info or resource series are read without verification.
    assert client.calls == [("GET", "api/v1/me"), ("GET", f"api/v1/{kind}s/{remote_id}")]
    assert rows(tmp_path) == before


@pytest.mark.parametrize("kind", ["command", "shell", "experiment"])
def test_unverifiable_message_matches_the_task_kind(tmp_path, kind):
    request, remote_id = DROPPED[kind]
    client, store, task = submitted(tmp_path, request)
    client.entity_errors[(kind, remote_id)] = APIError("404 not found", code=404)

    with pytest.raises(ConflictError) as caught:
        other_profile_service(client, store).logs(task["task_id"], "session-a")

    message = str(caught.value)
    if kind == "experiment":
        # Experiments are never dropped on a timer, and deleting one deletes its logs.
        assert "deleted or is not visible to this account" in message
        assert "deleting an experiment also deletes its logs" in message
        assert "if the experiment still exists" in message
        assert "24 hours" not in message
        assert "remain readable" not in message
    else:
        assert message == (
            "Determined no longer returns this task's entity (an ended command or shell "
            "is dropped 24 hours after it ends and on a master restart), so its owner "
            "and submission marker cannot be verified from another compute profile; "
            "logs and usage remain readable with the task's original compute profile"
        )


@pytest.mark.parametrize("operation", ["status", "logs", "usage"])
@pytest.mark.parametrize(
    "error",
    [
        APIError("503 unavailable", code=503, retryable=True),
        APIError("403 forbidden", code=403),
        APIError("timed out", code="transport_error", retryable=True),
    ],
)
def test_cross_profile_entity_errors_other_than_404_propagate_unchanged(
    tmp_path, operation, error
):
    client, store, task = submitted(tmp_path)
    client.entity_errors[("command", COMMAND_ID)] = error

    with pytest.raises(APIError) as caught:
        getattr(other_profile_service(client, store), operation)(task["task_id"], "session-a")

    assert caught.value is error
    assert client.calls == [("GET", "api/v1/me"), ("GET", f"api/v1/commands/{COMMAND_ID}")]


def test_exact_profile_reads_of_a_dropped_entity_are_unchanged(tmp_path):
    client, store, task = submitted(tmp_path)
    client.entity_errors[("command", COMMAND_ID)] = APIError("404 not found", code=404)
    service = ComputeService(client, store, profile())

    with pytest.raises(APIError) as status:
        service.status(task["task_id"], "session-a")
    logs = service.logs(task["task_id"], "session-a", 5, include_binding=True)
    usage = service.usage(task["task_id"], "session-a")

    assert type(status.value) is APIError
    assert status.value.code == 404
    assert logs["logs"] == [{"message": "remote log"}]
    assert logs["binding"]["mode"] == "profile"
    assert usage["binding"]["mode"] == "profile"
    assert usage["resource_pool"] is None


def test_legacy_description_marker_is_accepted_only_for_legacy_records(tmp_path):
    client, store, _task = submitted(tmp_path)
    legacy, _created = store.claim(
        request_id="legacy-1", owner="session-a", payload_hash="h", profile_hash="old",
        kind="command", code_revision=None, workdir="/shared/container/code",
        output_dir="/shared/container/out",
        cluster_identity=ComputeService(client, store, profile())._cluster_identity(),
    )
    other_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    store.mark_submitted(legacy.task_id, other_id)
    client.entities[("command", other_id)] = {
        "id": other_id,
        "userId": 7,
        "state": "RUNNING",
        "description": f"{legacy.submission_marker}\nold description",
    }
    service = other_profile_service(client, store)

    assert service.status(legacy.task_id, "session-a")["binding"]["verified"] == [
        "remote_owner", "submission_marker",
    ]

    named, _created = store.claim(
        request_id="named-1", owner="session-a", payload_hash="h", profile_hash="old",
        kind="command", code_revision=None, workdir="/shared/container/code",
        output_dir="/shared/container/out", name="named",
        cluster_identity=legacy.cluster_identity,
    )
    store.mark_submitted(named.task_id, other_id)
    client.entities[("command", other_id)]["description"] = f"{named.submission_marker}\n"
    with pytest.raises(ConflictError) as caught:
        service.status(named.task_id, "session-a")
    assert caught.value.code == "identity_mismatch"


def test_unbound_cross_profile_record_is_returned_without_remote_calls_or_writes(tmp_path):
    client = FakeClient()
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    identity = ComputeService(client, store, profile())._cluster_identity()
    record, _created = store.claim(
        request_id="request-1", owner="session-a", payload_hash="h", profile_hash="old",
        kind="command", code_revision=None, workdir="/shared/container/code",
        output_dir="/shared/container/out", cluster_identity=identity, name="pending",
    )
    service = ComputeService(client, store, profile(), submission_stale_seconds=0)
    before = rows(tmp_path)

    status = service.status(record.task_id, "session-a")
    logs = service.logs(record.task_id, "session-a", 5, include_binding=True)
    with pytest.raises(ConflictError) as usage:
        service.usage(record.task_id, "session-a")

    assert status["state"] == "pending"
    assert status["binding"]["mode"] == "cross_profile"
    assert status["binding"]["verified"] == []
    assert logs == {"task_id": record.task_id, "binding": status["binding"], "logs": []}
    assert usage.value.code == "remote_id_unknown"
    assert client.calls == []
    assert rows(tmp_path) == before


@pytest.mark.parametrize("operation", ["status", "logs", "usage", "cancel"])
@pytest.mark.parametrize(
    "service_factory",
    [
        lambda client, store: ComputeService(
            client, store, profile(image="other/image:v2", label="other-cluster")
        ),
        lambda client, store: ComputeService(_EndpointClient(client), store, profile()),
    ],
)
def test_changed_label_or_endpoint_still_fails_closed(tmp_path, operation, service_factory):
    client, store, task = submitted(tmp_path)

    with pytest.raises(ConflictError) as caught:
        getattr(service_factory(client, store), operation)(task["task_id"], "session-a")

    assert caught.value.code == "binding_mismatch"
    assert client.calls == []


class _EndpointClient:
    api_url = "https://other-det.example.test"

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_mutations_still_require_the_original_profile(tmp_path):
    client, store, task = submitted(tmp_path)
    service = other_profile_service(client, store)

    with pytest.raises(ConflictError) as cancel:
        service.cancel(task["task_id"], "session-a")
    with pytest.raises(ConflictError) as reconcile:
        service.reconcile(task["task_id"], "session-a", COMMAND_ID)
    with pytest.raises(ConflictError) as retry:
        ComputeService(
            client, store, profile(image="other/image:v2"), inspector=Admission()
        ).launch(REQUEST, "request-1", "session-a")

    assert cancel.value.code == "binding_mismatch"
    assert "Read-only status, logs and usage remain available" in str(cancel.value)
    assert reconcile.value.code == "binding_mismatch"
    assert retry.value.code == "idempotency_conflict"
    assert client.calls == []


def test_list_reports_offline_bindings_without_remote_calls(tmp_path):
    client, store, task = submitted(tmp_path)
    adopted_entity = {"id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "userId": 7,
                      "state": "RUNNING", "name": "remote"}
    client.entities[("command", adopted_entity["id"])] = adopted_entity
    ComputeService(client, store, profile()).adopt("command", adopted_entity["id"], "session-a")
    client.calls.clear()

    def bindings(service):
        return {item["task_id"]: item["binding"] for item in service.list_tasks("session-a")}

    same = bindings(ComputeService(client, store, profile()))
    changed = bindings(other_profile_service(client, store))
    elsewhere = bindings(ComputeService(client, store, profile(label="other-cluster")))

    assert same[task["task_id"]] == {
        "mode": "profile", "profile_matches": True, "cluster_identity_matches": True,
        "mutations_allowed": True,
    }
    assert changed[task["task_id"]] == {
        "mode": "cross_profile", "profile_matches": False, "cluster_identity_matches": True,
        "mutations_allowed": False,
    }
    assert elsewhere[task["task_id"]]["mode"] == "mismatch"
    assert elsewhere[task["task_id"]]["mutations_allowed"] is False
    adopted = [value for key, value in same.items() if key != task["task_id"]]
    assert adopted == [{
        "mode": "adopted", "profile_matches": None, "cluster_identity_matches": None,
        "mutations_allowed": None,
    }]
    assert client.calls == []


def test_list_survives_an_unresolvable_client(tmp_path):
    _client, store, task = submitted(tmp_path)

    class Broken:
        @property
        def api_url(self):
            raise RuntimeError("credentials unavailable")

    listed = ComputeService(Broken(), store, profile()).list_tasks("session-a")

    assert listed[0]["task_id"] == task["task_id"]
    assert listed[0]["binding"]["mode"] == "unknown"
    assert listed[0]["binding"]["cluster_identity_matches"] is None


def test_lazy_client_resolves_the_endpoint_without_constructing_a_client():
    constructed = []
    endpoints = iter(["https://first.example.test", "https://changed.example.test"])

    class Client:
        def __init__(self, api_url):
            self.api_url = api_url

    lazy = compute_cli._LazyClient(
        lambda api_url: constructed.append(Client(api_url)) or constructed[-1],
        lambda: next(endpoints),
    )

    assert lazy.api_url == "https://first.example.test"
    # The endpoint is read once; the constructed client uses the binding already checked.
    assert lazy.api_url == "https://first.example.test"
    assert constructed == []
    lazy._resolve_client()
    assert [client.api_url for client in constructed] == ["https://first.example.test"]
    assert lazy.api_url == "https://first.example.test"


def test_lazy_client_rereads_the_endpoint_after_a_failed_construction():
    endpoints = iter(["https://old.example.test", "https://fixed.example.test"])
    received = []

    class Client:
        def __init__(self, api_url):
            self.api_url = api_url

    def factory(api_url):
        received.append(api_url)
        if len(received) == 1:
            raise APIError("login failed", code="transport_error", retryable=True)
        return Client(api_url)

    lazy = compute_cli._LazyClient(factory, lambda: next(endpoints))
    assert lazy.api_url == "https://old.example.test"
    with pytest.raises(APIError):
        lazy._resolve_client()

    assert lazy.api_url == "https://fixed.example.test"
    assert lazy._resolve_client().api_url == "https://fixed.example.test"
    assert received == ["https://old.example.test", "https://fixed.example.test"]


def test_cli_logs_with_binding_forwards_the_option(monkeypatch, capsys):
    calls = []

    class Service:
        def logs(self, task_id, owner, tail, include_binding=False):
            calls.append((task_id, owner, tail, include_binding))
            return {"task_id": task_id, "binding": {}, "logs": []} if include_binding else []

    monkeypatch.setattr(compute_cli, "_resolve_runtime", lambda args: (Service(), "alice"))

    assert compute_cli.main(["logs", "task-1", "--tail", "7"]) == 0
    assert json.loads(capsys.readouterr().out)["result"] == []
    assert compute_cli.main(["logs", "task-1", "--with-binding"]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["task_id"] == "task-1"
    assert calls == [("task-1", "alice", 7, False), ("task-1", "alice", 200, True)]


def test_mcp_logs_include_binding_defaults_to_the_list_shape():
    pytest.importorskip("mcp")
    from mcp import Client

    from determined_compute.mcp_server import create_server

    calls = []

    class Service:
        def logs(self, task_id, owner, tail, include_binding=False):
            calls.append((task_id, owner, tail, include_binding))
            if include_binding:
                return {"task_id": task_id, "binding": {"mode": "profile"}, "logs": []}
            return [{"message": "m"}]

    async def exercise():
        async with Client(create_server(Service(), "alice")) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            schema = tools["compute_logs"].input_schema
            assert schema["properties"]["include_binding"]["default"] is False
            assert schema["properties"]["tail"]["default"] == 200
            assert set(schema["required"]) == {"task_id"}
            assert "owner" not in schema["properties"]
            plain = await client.call_tool("compute_logs", {"task_id": "t"})
            assert plain.structured_content == {"result": [{"message": "m"}]}
            wrapped = await client.call_tool(
                "compute_logs", {"task_id": "t", "include_binding": True}
            )
            assert wrapped.structured_content["result"]["binding"] == {"mode": "profile"}

    asyncio.run(asyncio.wait_for(exercise(), timeout=10))
    assert calls == [("t", "alice", 200, False), ("t", "alice", 200, True)]
