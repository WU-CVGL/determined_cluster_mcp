"""Listing and ownership checks of tasks addressed by Determined's own IDs."""

from __future__ import annotations

import copy

import pytest

from determined_compute.compute import (
    APIError,
    ComputeProfile,
    ComputeService,
    ConflictError,
    ValidationError,
)
from determined_compute.core.api_client import DeterminedAPIClient


COMMAND_ID = "12345678-1234-5678-9234-567812345678"
OTHER_COMMAND_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
GENERIC_ID = "3f1c2b8e-1111-4c7a-9d55-0123456789ab"
MARKER = "determined-compute:0b6f8c1e-2d8a-4c1f-9a51-6f1d2e3c4b5a"
OTHER_MARKER = "determined-compute:9e8d7c6b-5a49-4382-b170-6f5e4d3c2b1a"


class FakeClient:
    api_url = "https://det.example.test"

    def __init__(self) -> None:
        self.user = {"id": "7", "username": "alice"}
        self.calls = []
        self.entities = {}
        self.pages = {}

    def get_current_user(self):
        self.calls.append(("GET", "api/v1/me"))
        return copy.deepcopy(self.user)

    def list_remote_tasks(self, kind, *, user_id, limit, offset, states=None):
        query = {"user_id": user_id, "limit": limit, "offset": offset}
        if states is not None:
            query["states"] = states
        self.calls.append(("GET", f"api/v1/{kind}s", query))
        return copy.deepcopy(
            self.pages.get(
                kind,
                {"tasks": [], "pagination": {"limit": limit, "offset": offset, "total": 0}},
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

    def pause_task(self, kind, remote_id):
        self.calls.append(("POST", f"api/v1/{kind}s/{remote_id}/pause"))
        return {"id": remote_id, "acknowledged": True}

    def unpause_task(self, kind, remote_id):
        self.calls.append(("POST", f"api/v1/{kind}s/{remote_id}/unpause"))
        return {"id": remote_id, "acknowledged": True}

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

    def launch_task(self, kind, config, options=None):
        raise AssertionError("listing and control must never launch a task")


@pytest.fixture
def profile():
    return ComputeProfile.from_dict(
        {
            "mounts": [
                {"host_path": "/shared/host", "container_path": "/shared/container"}
            ],
            "defaults": {"image": "image:stable", "pool": "gpu", "slots": 1},
        }
    )


def service_for(profile, client=None):
    client = client or FakeClient()
    return ComputeService(client, profile), client


def command_entity(remote_id=COMMAND_ID, **overrides):
    entity = {
        "id": remote_id,
        "userId": 7,
        "username": "alice",
        "description": "remote command\nsafe display text",
        "state": "RUNNING",
        "resourcePool": "gpu",
        "startTime": "2026-09-20T00:00:00Z",
    }
    entity.update(overrides)
    return entity


def generic_entity(**overrides):
    entity = {
        "id": GENERIC_ID, "userId": 7, "username": "alice", "name": "eval-shards",
        "state": "STATE_ACTIVE", "resourcePool": "gpu", "startTime": "2026-10-01T00:00:00Z",
    }
    entity.update(overrides)
    return entity


def page(tasks, limit=50, offset=0, total=None):
    return {
        "tasks": tasks,
        "pagination": {
            "limit": limit, "offset": offset,
            "total": offset + len(tasks) if total is None else total,
        },
    }


# Listing


def test_list_is_owner_filtered_bounded_and_metadata_only(profile):
    service, client = service_for(profile)
    secret = "PRIVATE_KEY_MATERIAL_9138"
    entity = command_entity(
        config={"environment": {"TOKEN": secret}},
        privateKey=secret,
        rawConfig=secret,
    )
    client.pages["command"] = page([entity], limit=7, offset=2, total=3)

    result = service.list_tasks("command", limit=7, offset=2)

    assert client.calls == [
        ("GET", "api/v1/me"),
        ("GET", "api/v1/commands", {"user_id": "7", "limit": 7, "offset": 2}),
    ]
    assert result["account"] == {"id": "7", "username": "alice"}
    assert result["pagination"] == {"offset": 2, "limit": 7, "total": 3, "next_offset": None}
    assert result["tasks"] == [
        {
            "kind": "command",
            "id": COMMAND_ID,
            "name": "remote command",
            "description": "remote command\nsafe display text",
            "state": "RUNNING",
            "username": "alice",
            "resource_pool": "gpu",
            "start_time": "2026-09-20T00:00:00Z",
            "end_time": None,
        }
    ]
    assert "marker" not in result and "searched" not in result and "filters" not in result
    assert secret not in repr(result)


def test_list_reports_the_next_page(profile):
    service, client = service_for(profile)
    client.pages["command"] = page([command_entity()], limit=1, total=4)

    assert service.list_tasks("command", limit=1)["pagination"]["next_offset"] == 1


def test_list_returns_integer_experiment_ids(profile):
    service, client = service_for(profile)
    client.pages["experiment"] = page(
        [{"id": 12, "userId": 7, "name": "train", "state": "STATE_ACTIVE"}]
    )

    (task,) = service.list_tasks("experiment")["tasks"]

    assert task["id"] == 12
    assert task["name"] == "train"


def test_list_generic_tasks_by_owner(profile):
    service, client = service_for(profile)
    client.pages["generic"] = page([generic_entity()])

    result = service.list_tasks("generic")

    assert [(t["kind"], t["id"], t["name"]) for t in result["tasks"]] == [
        ("generic", GENERIC_ID, "eval-shards"),
    ]


@pytest.mark.parametrize(
    ("kind", "limit", "offset", "marker"),
    [
        ("notebook", 50, 0, None),
        ("command", 0, 0, None),
        ("command", 101, 0, None),
        ("command", True, 0, None),
        ("command", 50, -1, None),
        ("command", 50, True, None),
        ("command", 50, 0, "not-a-marker"),
        ("command", 50, 0, "determined-compute:not-a-uuid"),
    ],
)
def test_list_rejects_invalid_arguments_before_network(profile, kind, limit, offset, marker):
    service, client = service_for(profile)

    with pytest.raises(ValidationError):
        service.list_tasks(kind, limit=limit, offset=offset, marker=marker)

    assert client.calls == []


def test_list_rejects_server_results_owned_by_another_user(profile):
    service, client = service_for(profile)
    client.pages["command"] = page([command_entity(userId=8, username="bob")])

    with pytest.raises(ConflictError) as caught:
        service.list_tasks("command")

    assert caught.value.code == "ownership_mismatch"


@pytest.mark.parametrize(
    "pagination",
    [
        {"limit": 49, "offset": 0, "total": 0},
        {"limit": 50, "offset": 1, "total": 0},
        {"limit": 50, "offset": 0, "total": -1},
    ],
)
def test_list_rejects_inconsistent_server_pagination(profile, pagination):
    service, client = service_for(profile)
    client.pages["command"] = {"tasks": [], "pagination": pagination}

    with pytest.raises(APIError) as caught:
        service.list_tasks("command")

    assert caught.value.code == "invalid_response"


def test_list_propagates_identity_authentication_failure(profile):
    service, client = service_for(profile)

    def denied():
        client.calls.append(("GET", "api/v1/me"))
        raise APIError("denied", code=401)

    client.get_current_user = denied

    with pytest.raises(APIError) as caught:
        service.list_tasks("command")

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
def test_identity_requires_numeric_user_id_and_does_not_use_fallback_fields(profile, user):
    service, client = service_for(profile)
    client.user = user

    with pytest.raises(APIError) as caught:
        service.list_tasks("command")

    assert caught.value.code == "invalid_response"
    assert not any(call[1] == "api/v1/commands" for call in client.calls)


def test_marker_search_returns_every_match_on_the_page(profile):
    # A marker is a correlation label: a config copied outside the service carries it too.
    service, client = service_for(profile)
    first = command_entity()
    other = command_entity(OTHER_COMMAND_ID, startTime="2026-09-21T00:00:00Z")
    copy_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    copied = command_entity(copy_id, startTime="2026-09-22T00:00:00Z")
    client.pages["command"] = page([copied, other, first], limit=10, total=12)
    client.entities[("command", copy_id)] = dict(copied, submissionMarker=MARKER)
    client.entities[("command", OTHER_COMMAND_ID)] = dict(other, submissionMarker=OTHER_MARKER)
    client.entities[("command", COMMAND_ID)] = dict(first, submissionMarker=MARKER)

    result = service.list_tasks("command", limit=10, marker=MARKER)

    assert [task["id"] for task in result["tasks"]] == [copy_id, COMMAND_ID]
    assert {task["submission_marker"] for task in result["tasks"]} == {MARKER}
    assert result["marker"] == MARKER
    assert result["searched"] == 3
    # The search does not end at a match: older pages may hold more.
    assert result["pagination"]["next_offset"] == 3
    assert [call[1] for call in client.calls] == [
        "api/v1/me",
        "api/v1/commands",
        f"api/v1/commands/{copy_id}",
        f"api/v1/commands/{OTHER_COMMAND_ID}",
        f"api/v1/commands/{COMMAND_ID}",
    ]


def test_marker_search_continues_to_the_next_page(profile):
    service, client = service_for(profile)
    newer = [
        command_entity(f"0000000{index}-0000-4000-8000-000000000000") for index in (1, 2)
    ]
    original = command_entity()
    listing = newer + [original]
    client.list_remote_tasks = lambda kind, *, user_id, limit, offset, states=None: copy.deepcopy(
        page(listing[offset:offset + limit], limit=limit, offset=offset, total=len(listing))
    )
    for entity in newer:
        client.entities[("command", entity["id"])] = dict(entity, submissionMarker=OTHER_MARKER)
    client.entities[("command", COMMAND_ID)] = dict(original, submissionMarker=MARKER)

    first = service.list_tasks("command", limit=2, marker=MARKER)

    # An empty page is not a verdict: the result only reports where to continue.
    assert first["tasks"] == []
    assert first["searched"] == 2
    assert first["pagination"]["next_offset"] == 2
    assert set(first) == {"kind", "account", "tasks", "pagination", "marker", "searched"}

    second = service.list_tasks("command", limit=2, offset=2, marker=MARKER)

    assert [task["id"] for task in second["tasks"]] == [COMMAND_ID]
    assert second["pagination"]["next_offset"] is None


def test_marker_search_without_a_match_points_to_older_tasks(profile):
    service, client = service_for(profile)
    client.pages["command"] = page([command_entity()], limit=1, total=5)
    client.entities[("command", COMMAND_ID)] = command_entity(submissionMarker=OTHER_MARKER)

    result = service.list_tasks("command", limit=1, marker=MARKER)

    assert result["tasks"] == []
    assert result["searched"] == 1
    assert result["pagination"]["next_offset"] == 1


def test_marker_search_verifies_the_owner_of_each_read_task(profile):
    service, client = service_for(profile)
    client.pages["command"] = page([command_entity()])
    client.entities[("command", COMMAND_ID)] = command_entity(userId=8, submissionMarker=MARKER)

    with pytest.raises(ConflictError) as caught:
        service.list_tasks("command", marker=MARKER)

    assert caught.value.code == "ownership_mismatch"


# Listing by state


def test_list_filters_experiments_by_state_and_echoes_the_filter(profile):
    service, client = service_for(profile)
    client.pages["experiment"] = page(
        [{"id": 12, "userId": 7, "name": "train", "state": "STATE_RUNNING"}], total=1
    )

    result = service.list_tasks("experiment", states=["STATE_ACTIVE"])

    assert client.calls == [
        ("GET", "api/v1/me"),
        ("GET", "api/v1/experiments",
         {"user_id": "7", "limit": 50, "offset": 0, "states": ["STATE_ACTIVE"]}),
    ]
    assert result["filters"] == {"states": ["STATE_ACTIVE"]}
    assert [(task["id"], task["state"]) for task in result["tasks"]] == [(12, "STATE_RUNNING")]
    assert result["pagination"]["total"] == 1


def test_list_filters_generic_tasks_by_state(profile):
    service, client = service_for(profile)
    client.pages["generic"] = page([generic_entity(state="STATE_PAUSED")])

    result = service.list_tasks("generic", states=["STATE_PAUSED", "STATE_STOPPING_PAUSED"])

    assert client.calls[-1][2]["states"] == ["STATE_PAUSED", "STATE_STOPPING_PAUSED"]
    assert result["filters"] == {"states": ["STATE_PAUSED", "STATE_STOPPING_PAUSED"]}
    assert [task["state"] for task in result["tasks"]] == ["STATE_PAUSED"]


def test_list_with_states_pages_through_the_filtered_total(profile):
    service, client = service_for(profile)
    finished = [
        {"id": index, "userId": 7, "state": "STATE_COMPLETED"} for index in (30, 29, 28)
    ]
    client.list_remote_tasks = lambda kind, *, user_id, limit, offset, states=None: copy.deepcopy(
        page(finished[offset:offset + limit], limit=limit, offset=offset, total=len(finished))
    )

    first = service.list_tasks("experiment", limit=2, states=["STATE_COMPLETED"])
    second = service.list_tasks("experiment", limit=2, offset=2, states=["STATE_COMPLETED"])

    assert [task["id"] for task in first["tasks"]] == [30, 29]
    assert first["pagination"] == {"offset": 0, "limit": 2, "total": 3, "next_offset": 2}
    assert [task["id"] for task in second["tasks"]] == [28]
    assert second["pagination"]["next_offset"] is None


def test_marker_search_covers_the_filtered_page(profile):
    service, client = service_for(profile)
    task = generic_entity()
    client.pages["generic"] = page([task], limit=5)
    client.entities[("generic", GENERIC_ID)] = dict(task, submissionMarker=MARKER)

    result = service.list_tasks("generic", limit=5, marker=MARKER, states=["STATE_ACTIVE"])

    assert client.calls[1] == (
        "GET", "api/v1/generics",
        {"user_id": "7", "limit": 5, "offset": 0, "states": ["STATE_ACTIVE"]},
    )
    assert [t["id"] for t in result["tasks"]] == [GENERIC_ID]
    assert result["marker"] == MARKER and result["searched"] == 1
    assert result["filters"] == {"states": ["STATE_ACTIVE"]}


def test_marker_search_shows_a_state_newer_than_the_filter(profile):
    service, client = service_for(profile)
    client.pages["generic"] = page([generic_entity()], limit=5)
    client.entities[("generic", GENERIC_ID)] = generic_entity(
        state="STATE_COMPLETED", submissionMarker=MARKER
    )

    result = service.list_tasks("generic", limit=5, marker=MARKER, states=["STATE_ACTIVE"])

    assert [(t["id"], t["state"]) for t in result["tasks"]] == [(GENERIC_ID, "STATE_COMPLETED")]
    assert result["filters"] == {"states": ["STATE_ACTIVE"]}


def test_list_with_states_still_checks_every_owner(profile):
    service, client = service_for(profile)
    client.pages["experiment"] = page([{"id": 12, "userId": 8, "state": "STATE_PAUSED"}])

    with pytest.raises(ConflictError) as caught:
        service.list_tasks("experiment", states=["STATE_PAUSED"])

    assert caught.value.code == "ownership_mismatch"


@pytest.mark.parametrize(
    ("kind", "states", "message"),
    [
        ("command", ["STATE_ACTIVE"], "commands cannot be filtered by state"),
        ("shell", ["STATE_COMPLETED"], "shells cannot be filtered by state"),
        ("experiment", ["STATE_RUNNING"], "filter with STATE_ACTIVE"),
        ("experiment", ["STATE_QUEUED"], "filter with STATE_ACTIVE"),
        ("experiment", ["STATE_DELETED"], "must be among"),
        ("generic", ["STATE_DELETING"], "must be among"),
        ("generic", [], "non-empty list"),
        ("experiment", "STATE_ACTIVE", "non-empty list"),
    ],
)
def test_list_rejects_unsupported_states_before_network(profile, kind, states, message):
    service, client = service_for(profile)

    with pytest.raises(ValidationError, match=message) as caught:
        service.list_tasks(kind, states=states)

    assert caught.value.code == "invalid_request"
    assert client.calls == []


def test_list_reports_a_filter_the_master_did_not_apply(profile):
    service, client = service_for(profile)

    def ignored_filter(kind, *, user_id, limit, offset, states=None):
        raise APIError("outside the requested states", code="invalid_response")

    client.list_remote_tasks = ignored_filter

    with pytest.raises(APIError) as caught:
        service.list_tasks("experiment", states=["STATE_COMPLETED"])

    assert caught.value.code == "invalid_response"


# Ownership of every task operation

OPERATIONS = ["status", "logs", "usage", "cancel", "pause", "resume"]


def operate(service, operation, kind, task_id):
    return getattr(service, operation)(kind, task_id)


@pytest.mark.parametrize(
    ("operation", "kind"),
    [
        (operation, kind)
        for operation in OPERATIONS
        for kind in ("command", "generic", "experiment")
        # Commands cannot be paused; that is refused before any request.
        if not (operation in {"pause", "resume"} and kind == "command")
    ],
)
def test_every_operation_refuses_a_task_owned_by_another_user(profile, operation, kind):
    service, client = service_for(profile)
    task_id = {"command": COMMAND_ID, "generic": GENERIC_ID, "experiment": "9"}[kind]
    entity = {"command": command_entity(), "generic": generic_entity(),
              "experiment": {"id": 9, "state": "STATE_ACTIVE"}}[kind]
    # An administrator account can read another user's task through the API.
    client.entities[(kind, task_id)] = dict(entity, userId=8, username="bob")

    with pytest.raises(ConflictError) as caught:
        operate(service, operation, kind, task_id)

    assert caught.value.code == "ownership_mismatch"
    assert client.calls == [("GET", "api/v1/me"), ("GET", f"api/v1/{kind}s/{task_id}")]


@pytest.mark.parametrize("operation", OPERATIONS)
def test_every_operation_refuses_a_generic_task_whose_owner_is_unavailable(
    profile, operation
):
    service, client = service_for(profile)
    entity = generic_entity()
    del entity["userId"]  # a master without the generic task list reports no owner
    client.entities[("generic", GENERIC_ID)] = entity

    with pytest.raises(APIError) as caught:
        operate(service, operation, "generic", GENERIC_ID)

    assert caught.value.code == "ownership_unavailable"
    assert "WU-CVGL/determined#27" in str(caught.value)
    assert not any(call[0] == "POST" for call in client.calls)
    assert client.calls[-1] == ("GET", f"api/v1/generics/{GENERIC_ID}")


@pytest.mark.parametrize("operation", ["status", "logs", "usage", "cancel"])
def test_operations_act_on_an_owned_task(profile, operation):
    service, client = service_for(profile)
    client.entities[("command", COMMAND_ID)] = command_entity()

    result = operate(service, operation, "command", COMMAND_ID)

    assert client.calls[:2] == [("GET", "api/v1/me"), ("GET", f"api/v1/commands/{COMMAND_ID}")]
    if operation != "logs":
        assert result["id"] == COMMAND_ID and result["kind"] == "command"


def test_account_is_read_once_per_process(profile):
    service, client = service_for(profile)
    client.entities[("command", COMMAND_ID)] = command_entity()

    service.status("command", COMMAND_ID)
    service.logs("command", COMMAND_ID, 10)
    service.list_tasks("command")

    assert client.calls.count(("GET", "api/v1/me")) == 1


def test_a_command_without_a_reported_owner_is_refused(profile):
    service, client = service_for(profile)
    entity = command_entity()
    del entity["userId"]
    client.entities[("command", COMMAND_ID)] = entity

    with pytest.raises(APIError) as caught:
        service.cancel("command", COMMAND_ID)

    assert caught.value.code == "ownership_unavailable"
    assert "generic task list" not in str(caught.value)
    assert not any(call[0] == "POST" for call in client.calls)


def test_remote_identity_must_match_the_requested_task(profile):
    service, client = service_for(profile)
    client.entities[("command", COMMAND_ID)] = command_entity(OTHER_COMMAND_ID)

    with pytest.raises(APIError) as caught:
        service.cancel("command", COMMAND_ID)

    assert caught.value.code == "invalid_response"
    assert not any(call[0] == "POST" for call in client.calls)


@pytest.mark.parametrize(
    ("kind", "task_id"),
    [
        ("notebook", COMMAND_ID),
        ("command", "not-a-uuid"),
        ("shell", 123),
        ("generic", "not-a-uuid"),
        ("experiment", "0"),
        ("experiment", "-1"),
        ("experiment", "1.5"),
        ("experiment", True),
    ],
)
@pytest.mark.parametrize("operation", ["status", "logs", "usage", "cancel"])
def test_invalid_kinds_and_ids_are_rejected_before_network(profile, operation, kind, task_id):
    service, client = service_for(profile)

    with pytest.raises(ValidationError):
        operate(service, operation, kind, task_id)

    assert client.calls == []


def test_experiment_ids_are_canonical_positive_integers(profile):
    service, client = service_for(profile)
    client.entities[("experiment", "7")] = {
        "id": 7, "userId": "7", "name": "experiment", "state": "ACTIVE",
    }

    status = service.status("experiment", "0007")

    assert status["id"] == 7
    assert ("GET", "api/v1/experiments/7") in client.calls


def test_usage_reuses_the_verified_entity_for_pool_context(profile):
    service, client = service_for(profile)
    client.entities[("command", COMMAND_ID)] = command_entity()

    usage = service.usage("command", COMMAND_ID)

    assert client.calls.count(("GET", f"api/v1/commands/{COMMAND_ID}")) == 1
    assert client.calls.index(("GET", f"api/v1/commands/{COMMAND_ID}")) < client.calls.index(
        ("GET", "api/v1/task-resources/capability")
    )
    assert usage["resource_pool"]["name"] == "gpu"


def test_experiment_detail_omits_opaque_raw_configs_and_redacts_parsed_config(monkeypatch):
    secret = "ORIGINAL_CONFIG_SECRET_6371"
    client = DeterminedAPIClient(api_url="https://det.example.test", api_token="test-token")
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
