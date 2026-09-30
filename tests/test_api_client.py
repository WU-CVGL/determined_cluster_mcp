import json
import re

import pytest
import requests
import yaml

from determined_compute.core.api_client import (
    APIError,
    DeterminedAPIClient,
    SubmissionUncertainError,
    _normalize_api_url,
)


MARKER = "determined-compute:22222222-2222-2222-2222-222222222222"


class Response:
    def __init__(self, payload=None, status=200, lines=None):
        self.payload = payload
        self.status_code = status
        self.text = "" if payload is None else json.dumps(payload)
        self.content = self.text.encode()
        self.reason = "error"
        self.lines = lines or []
        self.closed = False

    def json(self):
        return self.payload

    def iter_lines(self, decode_unicode=True):
        yield from self.lines

    def close(self):
        self.closed = True


def client():
    return DeterminedAPIClient("master:8080", api_token="token")


def answer_get(monkeypatch, payload, status=200):
    monkeypatch.setattr(requests, "get", lambda *a, **k: Response(payload, status))


def gateway_error(status, grpc_code, reason, message):
    return Response({"error": {"code": grpc_code, "reason": reason, "error": message}}, status)


def assert_invalid_response(caught):
    assert caught.value.code == "invalid_response"
    assert "do-not-echo" not in str(caught.value)
    assert caught.value.details is None


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://cluster.example.org", "https://cluster.example.org"),
        ("host:8443", "http://host:8443"),
        ("[::1]", "http://[::1]:8080"),
        ("::1", "http://[::1]:8080"),
    ],
)
def test_url_normalization(given, expected):
    assert _normalize_api_url(given) == expected


def test_master_and_token_come_from_secret_file_unless_environment_sets_master(
    tmp_path, monkeypatch
):
    for name in ("DET_MASTER", "DET_MASTER_ADDR", "DET_MASTER_HOST", "DET_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    secrets = tmp_path / "secrets.env"
    secrets.write_text("DET_MASTER=https://secret.example\nDET_API_TOKEN=secret-token\n")

    resolved = DeterminedAPIClient(secrets_path=secrets)
    assert (resolved.api_url, resolved.api_token) == ("https://secret.example", "secret-token")

    monkeypatch.setenv("DET_MASTER", "https://environment.example")
    assert DeterminedAPIClient(secrets_path=secrets).api_url == "https://environment.example"


def test_read_errors_keep_retryability_but_failed_launches_are_uncertain(monkeypatch):
    def disconnected(*args, **kwargs):
        raise requests.ConnectionError("disconnected")

    answer_get(monkeypatch, {"message": "no"}, 403)
    with pytest.raises(APIError) as caught:
        client().get_task("command", "c1")
    assert (caught.value.code, caught.value.retryable) == (403, False)

    monkeypatch.setattr(requests, "get", disconnected)
    with pytest.raises(APIError) as caught:
        client().get_task("command", "c1")
    assert (caught.value.code, caught.value.retryable) == ("transport_error", True)

    # The launch may have been accepted, so it must never be resubmitted automatically.
    for post in (lambda *a, **k: Response({"message": "proxy"}, 502), disconnected):
        monkeypatch.setattr(requests, "post", post)
        with pytest.raises(SubmissionUncertainError) as caught:
            client().launch_task("command", {"entrypoint": ["true"]})
        assert (caught.value.code, caught.value.retryable) == ("submission_uncertain", False)


@pytest.mark.parametrize(
    ("status", "retryable"), [(404, False), (501, False), (503, True)]
)
def test_gateway_error_body_message_and_retryability(monkeypatch, status, retryable):
    monkeypatch.setattr(
        requests, "get",
        lambda *a, **k: gateway_error(status, 5, "NotFound", "task 'x' not found"),
    )
    with pytest.raises(APIError) as caught:
        client().get_task("command", "c1")
    assert str(caught.value) == f"{status} task 'x' not found"
    assert caught.value.code == status
    assert caught.value.retryable is retryable


def test_launch_payloads_by_kind_and_shell_private_key_removal(monkeypatch):
    calls = []
    responses = {
        "http://master:8080/api/v1/shells": {
            "shell": {"id": "s1", "privateKey": "secret", "state": "RUNNING"}
        },
        "http://master:8080/api/v1/experiments": {"experiment": {"id": 12}},
    }

    def post(url, **kwargs):
        calls.append((url, kwargs["json"]))
        return Response(responses[url])

    monkeypatch.setattr(requests, "post", post)
    shell = client().launch_task("shell", {"description": "debug"})
    assert calls[0] == ("http://master:8080/api/v1/shells", {"config": {"description": "debug"}})
    assert "privateKey" not in shell
    assert shell["reconnectCommand"] == "det shell show_ssh_command s1"

    config = {
        "name": "example",
        "entrypoint": "python train.py",
        "bind_mounts": [{"host_path": "/shared/host", "container_path": "/shared/container"}],
    }
    assert client().launch_task("experiment", config)["id"] == 12
    url, payload = calls[1]
    assert url == "http://master:8080/api/v1/experiments"
    assert set(payload) == {"config", "activate"}
    assert payload["activate"] is True
    assert yaml.safe_load(payload["config"]) == config


@pytest.mark.parametrize(
    ("kind", "config", "field"),
    [
        ("command", {"modelDefinition": "anything"}, "config.modelDefinition"),
        ("experiment", {"nested": {"model-definition": []}}, "config.nested.model-definition"),
    ],
)
def test_launch_rejects_upload_fields_before_any_request(monkeypatch, kind, config, field):
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("no request expected"))
    with pytest.raises(ValueError, match=re.escape(field)):
        client().launch_task(kind, config)


def test_cancel_unwraps_entities_and_acknowledges_empty_experiment_response(monkeypatch):
    responses = {
        "/api/v1/commands/c1/kill": Response(
            {"command": {"id": "c1", "state": "STATE_TERMINATED"}}
        ),
        "/api/v1/shells/s1/kill": Response(
            {"shell": {"id": "s1", "state": "STATE_TERMINATED", "privateKey": "fixture-secret"}}
        ),
        "/api/v1/experiments/17/cancel": Response(),
    }
    monkeypatch.setattr(
        requests, "post", lambda url, **k: responses[url.removeprefix("http://master:8080")]
    )

    assert client().cancel_task("command", "c1") == {"id": "c1", "state": "STATE_TERMINATED"}
    assert client().cancel_task("shell", "s1") == {
        "id": "s1",
        "state": "STATE_TERMINATED",
        "reconnectCommand": "det shell show_ssh_command s1",
    }
    assert client().cancel_task("experiment", "17") == {"id": "17", "acknowledged": True}


def test_get_task_preserves_safe_config_for_identity_check(monkeypatch):
    answer_get(monkeypatch, {
        "command": {"id": "c1"},
        "config": {
            "description": "marker\nhuman text",
            "entrypoint": ["true"],
            "environment_variables": [
                "PASSWORD=do-not-persist",
                f"COMPUTE_SUBMISSION_MARKER={MARKER}",
            ],
            "api_token": "do-not-persist",
        },
    })
    task = client().get_task("command", "c1")
    assert task["config"]["description"].startswith("marker")
    assert task["config"]["entrypoint"] == ["true"]
    assert task["config"]["environment_variables"] == "[redacted]"
    assert "api_token" not in task["config"]
    assert task["submissionMarker"] == MARKER


@pytest.mark.parametrize(
    "environment_variables",
    [
        ["SAFE=value", f"COMPUTE_SUBMISSION_MARKER={MARKER}"],
        {"cpu": {"COMPUTE_SUBMISSION_MARKER": MARKER}},
    ],
    ids=["list", "platform-mapping"],
)
def test_get_task_extracts_only_safe_marker_before_environment_redaction(
    monkeypatch, environment_variables
):
    answer_get(monkeypatch, {
        "command": {"id": "c1"},
        "config": {
            "description": "human task description",
            "environment": {"environment_variables": environment_variables},
        },
    })

    task = client().get_task("command", "c1")

    assert task["submissionMarker"] == MARKER
    assert task["config"]["description"] == "human task description"
    assert task["config"]["environment"]["environment_variables"] == "[redacted]"


def test_get_task_rejects_malformed_marker_metadata(monkeypatch):
    answer_get(monkeypatch, {
        "command": {
            "id": "c1",
            "submissionMarker": "determined-compute:33333333-3333-3333-3333-333333333333",
            "environmentVariables": ["TOKEN=raw-entity-secret"],
        },
        "config": {
            "environment": {
                "environment_variables": [
                    "COMPUTE_SUBMISSION_MARKER=not-a-safe-marker",
                    "TOKEN=secret",
                ]
            }
        },
    })

    task = client().get_task("command", "c1")

    assert "submissionMarker" not in task
    assert task["environmentVariables"] == "[redacted]"
    assert task["config"]["environment"]["environment_variables"] == "[redacted]"


def test_get_task_extracts_marker_from_yaml_experiment_config(monkeypatch):
    config = yaml.safe_dump({
        "name": "human experiment",
        "environment": {
            "environment_variables": {"cuda": [f"COMPUTE_SUBMISSION_MARKER={MARKER}"]}
        },
    })
    answer_get(monkeypatch, {"experiment": {"id": "e1"}, "config": config})

    task = client().get_task("experiment", "e1")

    assert task["submissionMarker"] == MARKER
    assert task["config"]["name"] == "human experiment"
    assert task["config"]["environment"]["environment_variables"] == "[redacted]"


def test_redaction_covers_secret_aliases_without_masking_innocent_tokens():
    redacted = client()._redact_secrets({
        "wandb_api_key": "secret",
        "Authorization": "Bearer secret",
        "service-credential": "secret",
        "private-key": "secret",
        "session_key": "secret",
        "cookies": "secret",
        "passwd": "secret",
        "tokenizer": "bert",
        "context_tokens": 4096,
    })
    assert redacted == {"tokenizer": "bert", "context_tokens": 4096}


def test_get_experiment_unwrap_and_true_tail(monkeypatch):
    requested = []
    responses = {
        "/api/v1/experiments/9": Response({"experiment": {"id": 9, "state": "STATE_RUNNING"}}),
        "/api/v1/experiments/9/trials": Response({"trials": [{"id": 2}, {"id": 17}]}),
        "/api/v1/trials/17/logs": Response(
            lines=[
                json.dumps({"result": {"message": "new"}}),
                json.dumps({"result": {"message": "old"}}),
            ]
        ),
    }

    def get(url, **kwargs):
        key = url.removeprefix("http://master:8080")
        requested.append((key, kwargs.get("params")))
        return responses[key]

    monkeypatch.setattr(requests, "get", get)
    assert client().get_task("experiment", "9")["id"] == 9
    messages = [
        item["message"] for item in client().task_logs("experiment", "9", tail=2)
    ]
    assert messages == ["old", "new"]
    assert requested[-1] == (
        "/api/v1/trials/17/logs",
        {"limit": 2, "follow": False, "orderBy": "ORDER_BY_DESC"},
    )
    assert responses["/api/v1/trials/17/logs"].closed is True


def test_get_current_user_normalizes_positive_id_and_returns_only_identity(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs.get("params")))
        return Response(
            {
                "user": {
                    "id": "0007",
                    "username": " alice ",
                    "password": "must-not-leak",
                    "admin": True,
                }
            }
        )

    monkeypatch.setattr(requests, "get", get)
    assert client().get_current_user() == {"id": "7", "username": "alice"}
    assert calls == [("http://master:8080/api/v1/me", None)]


@pytest.mark.parametrize(
    "payload",
    [
        {"user": None},
        {"user": {"id": True, "username": "alice"}},
        {"user": {"id": " 7", "username": "alice"}},
        {"user": {"id": 7, "username": ""}},
        {"user": {"id": 7, "username": 8}},
    ],
)
def test_get_current_user_rejects_malformed_response_without_echo(monkeypatch, payload):
    answer_get(monkeypatch, {**payload, "raw_secret": "do-not-echo"})
    with pytest.raises(APIError) as caught:
        client().get_current_user()
    assert_invalid_response(caught)


def test_get_cluster_id_uses_root_info_cluster_id(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return Response({"cluster_id": " cluster-123 ", "master_id": "wrong-value"})

    monkeypatch.setattr(requests, "get", get)
    assert client().get_cluster_id() == "cluster-123"
    assert calls == ["http://master:8080/info"]


@pytest.mark.parametrize(
    "payload",
    [{"master_id": "not-the-cluster-id"}, {"cluster_id": " "}, {"cluster_id": "x" * 257}],
)
def test_get_cluster_id_rejects_malformed_response(monkeypatch, payload):
    answer_get(monkeypatch, payload)
    with pytest.raises(APIError) as caught:
        client().get_cluster_id()
    assert_invalid_response(caught)


def test_list_remote_tasks_filters_pages_and_redacts(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs.get("params")))
        return Response(
            {
                "experiments": [
                    {
                        "id": 19,
                        "userId": "7",
                        "username": "alice",
                        "name": "native-name",
                        "displayName": "display",
                        "description": "safe description",
                        "state": "STATE_RUNNING",
                        "resourcePool": "gpu",
                        "startTime": "2026-09-20T00:00:00Z",
                        "endTime": None,
                        "config": {"environment": {"TOKEN": "secret"}},
                        "originalConfig": "secret config",
                        "privateKey": "secret key",
                        "environment": {"PASSWORD": "secret"},
                        "hyperparameters": {"secret": "value"},
                    },
                    {"id": "native-string-id", "state": "STATE_COMPLETED"},
                ],
                "pagination": {
                    "limit": 25,
                    "offset": 5,
                    "startIndex": 5,
                    "endIndex": 7,
                    "total": 42,
                    "ignored": "value",
                },
            }
        )

    monkeypatch.setattr(requests, "get", get)
    result = client().list_remote_tasks("experiment", user_id="007", limit=25, offset=5)

    assert calls == [
        (
            "http://master:8080/api/v1/experiments",
            {
                "userIds": [7],
                "limit": 25,
                "offset": 5,
                "orderBy": "ORDER_BY_DESC",
                "sortBy": "SORT_BY_START_TIME",
            },
        )
    ]
    assert result["pagination"] == {
        "limit": 25,
        "offset": 5,
        "startIndex": 5,
        "endIndex": 7,
        "total": 42,
    }
    assert result["tasks"][0] == {
        "id": 19,
        "userId": "7",
        "username": "alice",
        "name": "native-name",
        "displayName": "display",
        "description": "safe description",
        "state": "STATE_RUNNING",
        "resourcePool": "gpu",
        "startTime": "2026-09-20T00:00:00Z",
        "endTime": None,
    }
    assert result["tasks"][1] == {
        "id": "native-string-id",
        "state": "STATE_COMPLETED",
    }
    encoded = json.dumps(result)
    for forbidden in ("config", "privateKey", "environment", "hyperparameters", "secret"):
        assert forbidden not in encoded


@pytest.mark.parametrize(
    ("kind", "kwargs", "invalid"),
    [
        ("commands", {"user_id": "7"}, "kind"),
        ("command", {"user_id": "0"}, "user_id"),
        ("command", {"user_id": "7", "limit": True}, "limit"),
        ("command", {"user_id": "7", "offset": -1}, "offset"),
    ],
)
def test_list_remote_tasks_validates_inputs_before_any_request(
    monkeypatch, kind, kwargs, invalid
):
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("no request expected"))
    with pytest.raises(ValueError, match=f"^{invalid} "):
        client().list_remote_tasks(kind, **kwargs)


@pytest.mark.parametrize(
    "payload",
    [
        {"commands": {}, "pagination": {}},
        {"commands": ["not-an-object"], "pagination": {}},
        {
            "commands": [{"id": "task", "description": {"privateKey": "secret"}}],
            "pagination": {
                "limit": 50,
                "offset": 0,
                "startIndex": 0,
                "endIndex": 1,
                "total": 1,
            },
        },
        {"commands": [], "pagination": None},
        {
            "commands": [],
            "pagination": {
                "limit": 50,
                "offset": 0,
                "startIndex": 0,
                "endIndex": 0,
            },
        },
    ],
)
def test_list_remote_tasks_rejects_malformed_pages_without_echo(monkeypatch, payload):
    answer_get(monkeypatch, {**payload, "raw_secret": "do-not-echo"})
    with pytest.raises(APIError) as caught:
        client().list_remote_tasks("command", user_id="7")
    assert_invalid_response(caught)


def test_task_resources_capability_reports_the_master_flag(monkeypatch):
    requested = []
    flags = iter([True, False])

    def get(url, **kwargs):
        requested.append(url)
        return Response({"enabled": next(flags)})

    monkeypatch.setattr(requests, "get", get)
    assert client().task_resources_enabled() is True
    assert client().task_resources_enabled() is False
    assert requested == ["http://master:8080/api/v1/task-resources/capability"] * 2


@pytest.mark.parametrize(
    ("response", "code"),
    [
        # A master without the route answers 501 through the gateway, or 404 behind a proxy.
        (gateway_error(404, 12, "Unimplemented", "Not Implemented"), "task_resources_unsupported"),
        (gateway_error(501, 12, "Unimplemented", "Not Implemented"), "task_resources_unsupported"),
        (gateway_error(401, 16, "Unauthenticated", "no"), 401),
        (Response({"enabled": "yes"}), "invalid_response"),
    ],
    ids=["404", "501", "401", "malformed"],
)
def test_task_resources_capability_errors(monkeypatch, response, code):
    monkeypatch.setattr(requests, "get", lambda *a, **k: response)
    with pytest.raises(APIError) as caught:
        client().task_resources_enabled()
    assert caught.value.code == code
    assert caught.value.retryable is False


def test_task_resources_request_validation_and_parsing(monkeypatch):
    requested = []
    payload = {
        "enabled": True,
        "series": [
            {
                "metric": "gpu_utilization_percent",
                "labels": {"allocationId": "1.abc.1", "node": "", "gpuUuid": "GPU-1"},
                "samples": [
                    {"timestampSeconds": 100, "value": 0},
                    {"timestampSeconds": 115.5, "value": None},
                    {"timestampSeconds": 130},
                ],
            }
        ],
        "warnings": [{"code": "gpu_full_device", "message": "whole device"}],
    }

    def get(url, **kwargs):
        requested.append((url, kwargs.get("params")))
        return Response(payload)

    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(ValueError):
        client().get_task_resources("c1", start=True, end=900, step=15)
    with pytest.raises(ValueError):
        client().get_task_resources("c1", start=0, end=900, step=15, allocation_id="")
    assert requested == []

    result = client().get_task_resources(
        "1.abc/def", start=100, end=130, step=15, allocation_id="1.abc.1"
    )

    assert requested == [(
        "http://master:8080/api/v1/tasks/1.abc%2Fdef/resources",
        {"start": 100, "end": 130, "step": 15, "allocationId": "1.abc.1"},
    )]
    assert result == {
        "enabled": True,
        "series": [
            {
                "metric": "gpu_utilization_percent",
                "labels": {"allocation_id": "1.abc.1", "node": None, "gpu_uuid": "GPU-1"},
                "samples": [[100, 0], [115.5, None], [130, None]],
            }
        ],
        "warnings": [{"code": "gpu_full_device", "message": "whole device"}],
    }

    requested.clear()
    client().get_task_resources("c1", start=0, end=900, step=15)
    assert requested[0][1] == {"start": 0, "end": 900, "step": 15}


def one_series(**sample):
    return {
        "enabled": True,
        "series": [{"metric": "cpu_cores", "labels": {}, "samples": [sample]}],
        "warnings": [],
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"enabled": True, "series": {}, "warnings": []},
        {"enabled": True, "series": [{"metric": "cpu_cores", "labels": {}}], "warnings": []},
        one_series(timestampSeconds=1, value="NaN"),
        # Milliseconds instead of seconds.
        one_series(timestampSeconds=1.7e12, value=1.0),
        {"enabled": True, "series": [], "warnings": [{"code": "x"}]},
        {"series": [], "warnings": []},
    ],
    ids=[
        "series-not-list", "no-samples", "non-numeric-value", "timestamp-ms", "warning",
        "no-enabled-flag",
    ],
)
def test_task_resources_rejects_malformed_payload(monkeypatch, payload):
    answer_get(monkeypatch, {**payload, "raw": "do-not-echo"})
    with pytest.raises(APIError) as caught:
        client().get_task_resources("c1", start=0, end=900, step=15)
    assert_invalid_response(caught)


def test_task_info_and_trials(monkeypatch):
    requested = []
    responses = {
        "/api/v1/tasks/c1": Response({
            "task": {
                "taskId": "c1",
                "startTime": "2026-09-20T00:00:00.123456789Z",
                "allocations": [{
                    "allocationId": "c1.1",
                    "state": "STATE_RUNNING",
                    "isReady": True,
                    "startTime": "2026-09-20T00:00:05.5",
                }],
            }
        }),
        "/api/v1/experiments/9/trials": Response({
            "trials": [{"id": 17, "experimentId": 9, "taskIds": ["9.a", "9.a-1"]}],
            "pagination": {"total": 30},
        }),
        "/api/v1/trials/4": Response({"trial": {"id": 4, "experimentId": 9}}),
    }

    def get(url, **kwargs):
        key = url.removeprefix("http://master:8080")
        requested.append((key, kwargs.get("params")))
        return responses[key]

    monkeypatch.setattr(requests, "get", get)
    assert client().get_task_info("c1") == {
        "task_id": "c1",
        "start_time": "2026-09-20T00:00:00.123456789Z",
        "end_time": None,
        "allocations": [{
            "allocation_id": "c1.1",
            "state": "STATE_RUNNING",
            "is_ready": True,
            "start_time": "2026-09-20T00:00:05.5",
            "end_time": None,
        }],
    }
    latest = client().get_latest_trial("9")
    assert latest == {"trial": {"id": 17, "experimentId": 9, "taskIds": ["9.a", "9.a-1"]}, "total": 30}
    assert client().get_trial("4")["experimentId"] == 9
    assert requested[1] == (
        "/api/v1/experiments/9/trials",
        {"sortBy": "SORT_BY_ID", "orderBy": "ORDER_BY_DESC", "limit": 1},
    )

    responses["/api/v1/tasks/c1"] = Response({"task": {"taskId": "other", "allocations": []}})
    with pytest.raises(APIError) as caught:
        client().get_task_info("c1")
    assert caught.value.code == "invalid_response"


def test_allocation_details_parse_and_validate(monkeypatch):
    requested = []
    payload = {"allocation": {
        "taskId": "1.a", "allocationId": "1.a/b.1", "state": "STATE_TERMINATED", "slots": 4,
        "exitReason": "x" * 2000, "statusCode": 137, "startTime": "2027-01-15 06:00:00 +0000 UTC",
    }}

    def get(url, **kwargs):
        requested.append(url)
        return Response(payload)

    monkeypatch.setattr(requests, "get", get)
    detail = client().get_allocation("1.a/b.1")
    assert requested == ["http://master:8080/api/v1/allocations/1.a%2Fb.1"]
    assert detail == {
        "allocation_id": "1.a/b.1", "slots": 4, "exit_reason": "x" * 1024, "status_code": 137,
    }

    payload["allocation"] = {"allocationId": "1.a/b.1", "slots": 1}
    assert client().get_allocation("1.a/b.1")["exit_reason"] is None

    # A CPU-only allocation has zero slots, which is valid.
    payload["allocation"] = {"allocationId": "1.a/b.1", "slots": 0}
    assert client().get_allocation("1.a/b.1")["slots"] == 0

    for broken in (
        {"allocationId": "other", "slots": 1},
        {"allocationId": "1.a/b.1", "slots": "1"},
        {"allocationId": "1.a/b.1", "slots": 1, "statusCode": True},
    ):
        payload["allocation"] = broken
        with pytest.raises(APIError) as caught:
            client().get_allocation("1.a/b.1")
        assert caught.value.code == "invalid_response"


def test_resource_pool_descriptions(monkeypatch):
    requested = []

    def get(url, **kwargs):
        requested.append((url, kwargs.get("params")))
        return Response({"resourcePools": [
            {"name": "gpu", "description": "Operator text", "slotsAvailable": 8},
            {"name": "cpu", "description": ""},
        ]})

    monkeypatch.setattr(requests, "get", get)
    assert client().list_resource_pools() == [
        {"name": "gpu", "description": "Operator text"},
        {"name": "cpu", "description": None},
    ]
    assert requested == [("http://master:8080/api/v1/resource-pools", {"limit": 0})]

    answer_get(monkeypatch, {"resourcePools": [{}]})
    with pytest.raises(APIError) as caught:
        client().list_resource_pools()
    assert caught.value.code == "invalid_response"


def test_gpu_device_models_skip_hidden_and_non_accelerator_devices(monkeypatch):
    answer_get(monkeypatch, {"agents": [
        {"id": "a", "slots": {
            "0": {"device": {"brand": "Model X", "uuid": "GPU-1", "type": "TYPE_CUDA"}},
            "1": {"device": {"brand": "Model X", "uuid": "********", "type": "TYPE_CUDA"}},
            "2": {"device": {"brand": "Epyc", "uuid": "cpu-0", "type": "TYPE_CPU"}},
        }},
        {"id": "b", "slots": [
            {"device": {"brand": "Model Y", "uuid": "GPU-2", "type": "TYPE_ROCM"}},
            {"device": {"brand": "", "uuid": "GPU-3", "type": "TYPE_CUDA"}},
            {"device": None},
        ]},
        {"id": "c"},
    ]})
    assert client().list_gpu_devices() == {"GPU-1": "Model X", "GPU-2": "Model Y"}

    answer_get(monkeypatch, {"agents": ["bad"]})
    with pytest.raises(APIError) as caught:
        client().list_gpu_devices()
    assert caught.value.code == "invalid_response"
