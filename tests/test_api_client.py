import json

import pytest
import requests

from determined_compute.core.api_client import (
    APIError,
    DeterminedAPIClient,
    SubmissionUncertainError,
    _normalize_api_url,
)


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


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("DET_MASTER", "DET_MASTER_ADDR", "DET_MASTER_HOST", "DET_API_TOKEN",
                 "DET_USERNAME", "DET_PASSWORD", "DETERMINED_COMPUTE_SECRETS"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def no_request(*args, **kwargs):
    raise AssertionError("no request may be sent")


def test_master_and_token_can_come_from_secret_file(tmp_path, clean_env):
    secrets = tmp_path / "secrets.env"
    secrets.write_text("DET_MASTER=https://cluster.example.org\nDET_API_TOKEN=secret-token\n")
    resolved = DeterminedAPIClient(secrets_path=secrets)
    assert resolved.api_url == "https://cluster.example.org"
    assert resolved.api_token == "secret-token"


@pytest.mark.parametrize(
    ("environment", "explicit", "source"),
    [
        ({"DET_MASTER": "https://environment.example"}, {}, "DET_MASTER"),
        ({"DET_MASTER_HOST": "environment.example"}, {}, "DET_MASTER"),
        ({}, {"api_url": "https://chosen.example"}, "--api-url"),
        # An explicit token does not make another master acceptable for this file.
        ({}, {"api_url": "https://chosen.example", "api_token": "explicit"}, "--api-url"),
    ],
)
def test_secrets_file_credentials_never_go_to_another_master(
    tmp_path, clean_env, environment, explicit, source
):
    secrets = tmp_path / "secrets.env"
    secrets.write_text("DET_MASTER=https://secret.example\nDET_USERNAME=alice\nDET_PASSWORD=pw-value\n")
    for name, value in environment.items():
        clean_env.setenv(name, value)
    clean_env.setattr(requests, "post", no_request)
    clean_env.setattr(requests, "get", no_request)

    with pytest.raises(ValueError, match="different master than the secrets file") as caught:
        DeterminedAPIClient(secrets_path=secrets, **explicit)

    message = str(caught.value)
    assert message.startswith(source)
    for value in ("secret.example", "environment.example", "chosen.example", "alice", "pw-value", "explicit"):
        assert value not in message


def test_the_same_master_named_twice_is_accepted(tmp_path, clean_env):
    secrets = tmp_path / "secrets.env"
    secrets.write_text("DET_MASTER=secret.example\nDET_API_TOKEN=file-token\n")
    clean_env.setenv("DET_MASTER", "http://secret.example:8080/")
    assert DeterminedAPIClient(secrets_path=secrets).api_url == "http://secret.example:8080"
    # An explicit URL for the file's master is used, and the environment's is not consulted.
    clean_env.setenv("DET_MASTER", "https://environment.example")
    resolved = DeterminedAPIClient("http://secret.example:8080", secrets_path=secrets)
    assert (resolved.api_url, resolved.api_token) == ("http://secret.example:8080", "file-token")


def test_a_file_that_names_its_master_ignores_ambient_credentials(tmp_path, clean_env):
    logins = []

    def login(url, **kwargs):
        logins.append((url, kwargs["json"]))
        return Response({"token": "session-token"})

    clean_env.setattr(requests, "post", login)
    clean_env.setenv("DET_API_TOKEN", "ambient-token")
    clean_env.setenv("DET_USERNAME", "mallory")
    clean_env.setenv("DET_PASSWORD", "ambient-pw")
    named = tmp_path / "named.env"
    named.write_text("DET_MASTER=https://secret.example\nDET_USERNAME=alice\nDET_PASSWORD=pw\n")

    resolved = DeterminedAPIClient(secrets_path=named)

    assert resolved.api_token == "session-token"
    assert logins == [("https://secret.example/api/v1/auth/login", {"username": "alice", "password": "pw"})]
    # A file with a master and no credentials does not borrow the environment's.
    bare = tmp_path / "bare.env"
    bare.write_text("DET_MASTER=https://secret.example\n")
    unauthenticated = DeterminedAPIClient(secrets_path=bare)
    assert unauthenticated.api_token is None and "Authorization" not in unauthenticated.headers
    assert len(logins) == 1
    # An explicit token replaces the file's credentials and goes to the file's master.
    explicit = DeterminedAPIClient(api_token="explicit-token", secrets_path=named)
    assert (explicit.api_url, explicit.api_token) == ("https://secret.example", "explicit-token")
    assert len(logins) == 1


def test_a_file_without_a_master_keeps_the_environment_master_and_token(tmp_path, clean_env):
    unnamed = tmp_path / "unnamed.env"
    unnamed.write_text("DET_API_TOKEN=file-token\n")
    clean_env.setenv("DET_MASTER", "https://environment.example")
    clean_env.setenv("DET_API_TOKEN", "ambient-token")

    resolved = DeterminedAPIClient(secrets_path=unnamed)

    assert (resolved.api_url, resolved.api_token) == ("https://environment.example", "ambient-token")
    explicit = DeterminedAPIClient("https://chosen.example", secrets_path=unnamed)
    assert explicit.api_url == "https://chosen.example"


def test_api_error_fields_and_mutation_uncertainty(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: Response({"message": "no"}, 403))
    with pytest.raises(APIError) as caught:
        client().get_task("command", "c1")
    assert caught.value.code == 403
    assert caught.value.retryable is False

    monkeypatch.setattr(requests, "post", lambda *a, **k: Response({"message": "proxy"}, 502))
    with pytest.raises(SubmissionUncertainError) as caught:
        client().launch_task("command", {"entrypoint": ["true"]})
    assert caught.value.code == "submission_uncertain"
    assert caught.value.retryable is False


def test_transport_read_is_retryable_but_mutation_is_uncertain(monkeypatch):
    def fail(*args, **kwargs):
        raise requests.ConnectionError("disconnected")

    monkeypatch.setattr(requests, "get", fail)
    with pytest.raises(APIError) as caught:
        client().get_task("command", "c1")
    assert caught.value.retryable is True

    monkeypatch.setattr(requests, "post", fail)
    with pytest.raises(SubmissionUncertainError):
        client().launch_task("command", {"entrypoint": ["true"]})


def test_launch_payloads_and_shell_secret_removal(monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs["json"]))
        return Response({"shell": {"id": "s1", "privateKey": "secret", "state": "RUNNING"}})

    monkeypatch.setattr(requests, "post", post)
    result = client().launch_task("shell", {"description": "debug"})
    assert calls == [("http://master:8080/api/v1/shells", {"config": {"description": "debug"}})]
    assert "privateKey" not in result
    assert result["reconnectCommand"] == "det shell show_ssh_command s1"


def test_get_task_preserves_safe_config_for_identity_check(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: Response({
            "command": {"id": "c1"},
            "config": {
                "description": "marker\nhuman text",
                "entrypoint": ["true"],
                "environment_variables": [
                    "PASSWORD=do-not-persist",
                    "COMPUTE_SUBMISSION_MARKER="
                    "determined-compute:11111111-1111-1111-1111-111111111111",
                ],
                "api_token": "do-not-persist",
            },
        }),
    )
    task = client().get_task("command", "c1")
    assert task["config"]["description"].startswith("marker")
    assert task["config"]["entrypoint"] == ["true"]
    assert task["config"]["environment_variables"] == "[redacted]"
    assert "api_token" not in task["config"]
    assert task["submissionMarker"] == (
        "determined-compute:11111111-1111-1111-1111-111111111111"
    )


@pytest.mark.parametrize(
    "environment_variables",
    [
        [
            "SAFE=value",
            "COMPUTE_SUBMISSION_MARKER=determined-compute:22222222-2222-2222-2222-222222222222",
        ],
        {
            "cpu": [
                "COMPUTE_SUBMISSION_MARKER=determined-compute:22222222-2222-2222-2222-222222222222"
            ],
            "cuda": ["SAFE=value"],
        },
        {
            "cpu": {
                "COMPUTE_SUBMISSION_MARKER": (
                    "determined-compute:22222222-2222-2222-2222-222222222222"
                )
            }
        },
    ],
)
def test_get_task_extracts_only_safe_marker_before_environment_redaction(
    monkeypatch, environment_variables
):
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: Response(
            {
                "command": {"id": "c1"},
                "config": {
                    "description": "human task description",
                    "environment": {
                        "environment_variables": environment_variables,
                    },
                },
            }
        ),
    )

    task = client().get_task("command", "c1")

    assert task["submissionMarker"] == (
        "determined-compute:22222222-2222-2222-2222-222222222222"
    )
    assert task["config"]["description"] == "human task description"
    assert task["config"]["environment"]["environment_variables"] == "[redacted]"


def test_get_task_rejects_malformed_marker_metadata(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: Response(
            {
                "command": {
                    "id": "c1",
                    "submissionMarker": (
                        "determined-compute:33333333-3333-3333-3333-333333333333"
                    ),
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
            }
        ),
    )

    task = client().get_task("command", "c1")

    assert "submissionMarker" not in task
    assert task["environmentVariables"] == "[redacted]"
    assert task["config"]["environment"]["environment_variables"] == "[redacted]"


def test_get_task_extracts_marker_from_yaml_experiment_config(monkeypatch):
    import yaml

    config = yaml.safe_dump(
        {
            "name": "human experiment",
            "environment": {
                "environment_variables": {
                    "cuda": [
                        "COMPUTE_SUBMISSION_MARKER="
                        "determined-compute:44444444-4444-4444-4444-444444444444"
                    ]
                }
            },
        }
    )
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: Response({"experiment": {"id": "e1"}, "config": config}),
    )

    task = client().get_task("experiment", "e1")

    assert task["submissionMarker"] == (
        "determined-compute:44444444-4444-4444-4444-444444444444"
    )
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


def test_cancel_unwraps_command_entity(monkeypatch):
    monkeypatch.setattr(
        requests,
        "post",
        lambda *a, **k: Response({"command": {"id": "c1", "state": "STATE_TERMINATED"}}),
    )
    assert client().cancel_task("command", "c1") == {
        "id": "c1",
        "state": "STATE_TERMINATED",
    }


def test_experiment_cancel_preserves_empty_response_acknowledgement(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: Response())
    assert client().cancel_task("experiment", "17") == {
        "id": "17",
        "acknowledged": True,
    }


@pytest.mark.parametrize("field", ["files", "data", "context", "project_root", "modelDefinition"])
def test_launch_rejects_upload_aliases(field):
    with pytest.raises(ValueError, match="uploads are not supported"):
        client().launch_task("command", {field: "anything"})


def test_launch_rejects_nested_normalized_upload_alias():
    with pytest.raises(ValueError, match=r"config\.nested\.model-definition"):
        client().launch_task("experiment", {"nested": {"model-definition": []}})


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
    assert (
        "/api/v1/experiments/9/trials",
        {"sortBy": "SORT_BY_ID", "orderBy": "ORDER_BY_DESC", "limit": 1},
    ) in requested
    assert requested[-1] == (
        "/api/v1/trials/17/logs",
        {"limit": 2, "follow": False, "orderBy": "ORDER_BY_DESC"},
    )
    assert responses["/api/v1/trials/17/logs"].closed is True


def test_experiment_launch_sends_only_yaml_config_and_activation(monkeypatch):
    import yaml
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs['json']))
        return Response({'experiment': {'id': 12}})
    monkeypatch.setattr(requests, 'post', post)
    config = {'name': 'example', 'entrypoint': 'python train.py',
              'bind_mounts': [{'host_path': '/SSD', 'container_path': '/SSD'}]}
    assert client().launch_task('experiment', config)['id'] == 12
    url, payload = calls[0]
    assert url.endswith('/api/v1/experiments')
    assert set(payload) == {'config', 'activate'}
    assert payload['activate'] is True
    assert yaml.safe_load(payload['config']) == config


def test_shell_cancel_unwraps_response_and_removes_private_key(monkeypatch):
    monkeypatch.setattr(requests, 'post', lambda *a, **kw: Response({
        'shell': {'id': 's1', 'state': 'STATE_TERMINATED', 'privateKey': 'fixture-secret'},
    }))
    result = client().cancel_task('shell', 's1')
    assert result['id'] == 's1'
    assert result['state'] == 'STATE_TERMINATED'
    assert 'privateKey' not in result
    assert result['reconnectCommand'] == 'det shell show_ssh_command s1'


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
        {},
        {"user": None},
        {"user": {"id": 0, "username": "alice"}},
        {"user": {"id": True, "username": "alice"}},
        {"user": {"id": " 7", "username": "alice"}},
        {"user": {"id": 7, "username": ""}},
        {"user": {"id": 7, "username": 8}},
    ],
)
def test_get_current_user_rejects_malformed_response_without_echo(monkeypatch, payload):
    payload["raw_secret"] = "do-not-echo"
    monkeypatch.setattr(requests, "get", lambda *a, **k: Response(payload))
    with pytest.raises(APIError) as caught:
        client().get_current_user()
    assert caught.value.code == "invalid_response"
    assert "do-not-echo" not in str(caught.value)
    assert caught.value.details is None


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
    [
        {"master_id": "not-the-cluster-id"},
        {"cluster_id": ""},
        {"cluster_id": 123},
        {"cluster_id": "x" * 257},
    ],
)
def test_get_cluster_id_rejects_malformed_response(monkeypatch, payload):
    monkeypatch.setattr(requests, "get", lambda *a, **k: Response(payload))
    with pytest.raises(APIError) as caught:
        client().get_cluster_id()
    assert caught.value.code == "invalid_response"
    assert caught.value.details is None


@pytest.mark.parametrize("kind", ["command", "shell", "experiment"])
def test_list_remote_tasks_filters_pages_and_redacts(kind, monkeypatch):
    calls = []
    collection_key = f"{kind}s"

    def get(url, **kwargs):
        calls.append((url, kwargs.get("params")))
        return Response(
            {
                collection_key: [
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
    result = client().list_remote_tasks(kind, user_id="007", limit=25, offset=5)

    assert calls == [
        (
            f"http://master:8080/api/v1/{kind}s",
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
    ("kwargs", "message"),
    [
        ({"kind": "commands", "user_id": "7"}, "kind must be"),
        ({"kind": "command", "user_id": "0"}, "positive numeric"),
        ({"kind": "command", "user_id": "7", "limit": 0}, "between 1 and 100"),
        ({"kind": "command", "user_id": "7", "limit": True}, "between 1 and 100"),
        ({"kind": "command", "user_id": "7", "offset": -1}, "non-negative"),
        ({"kind": "command", "user_id": "7", "offset": False}, "non-negative"),
    ],
)
def test_list_remote_tasks_validates_inputs(kwargs, message):
    kind = kwargs.pop("kind")
    with pytest.raises(ValueError, match=message):
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
        {
            "commands": [],
            "pagination": {
                "limit": True,
                "offset": 0,
                "startIndex": 0,
                "endIndex": 0,
                "total": 0,
            },
        },
    ],
)
def test_list_remote_tasks_rejects_malformed_pages_without_echo(monkeypatch, payload):
    payload["raw_secret"] = "do-not-echo"
    monkeypatch.setattr(requests, "get", lambda *a, **k: Response(payload))
    with pytest.raises(APIError) as caught:
        client().list_remote_tasks("command", user_id="7")
    assert caught.value.code == "invalid_response"
    assert "do-not-echo" not in str(caught.value)
    assert caught.value.details is None


def test_remote_discovery_does_not_swallow_authentication_error(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: Response({"message": "denied"}, status=401),
    )
    with pytest.raises(APIError) as caught:
        client().list_remote_tasks("command", user_id="7")
    assert caught.value.code == 401
    assert caught.value.retryable is False


def gateway_error(status, grpc_code, reason, message):
    return Response({"error": {"code": grpc_code, "reason": reason, "error": message}}, status)


@pytest.mark.parametrize(
    ("status", "retryable"), [(404, False), (500, True), (501, False), (503, True)]
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


@pytest.mark.parametrize("enabled", [True, False])
def test_task_resources_capability(monkeypatch, enabled):
    requested = []

    def get(url, **kwargs):
        requested.append(url)
        return Response({"enabled": enabled})

    monkeypatch.setattr(requests, "get", get)
    assert client().task_resources_enabled() is enabled
    assert requested == ["http://master:8080/api/v1/task-resources/capability"]


@pytest.mark.parametrize("status", [404, 501])
def test_task_resources_capability_missing_route_is_unsupported(monkeypatch, status):
    monkeypatch.setattr(
        requests, "get",
        lambda *a, **k: gateway_error(status, 12, "Unimplemented", "Not Implemented"),
    )
    with pytest.raises(APIError) as caught:
        client().task_resources_enabled()
    assert caught.value.code == "task_resources_unsupported"
    assert caught.value.retryable is False


def test_task_resources_capability_keeps_authentication_errors(monkeypatch):
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: gateway_error(401, 16, "Unauthenticated", "no")
    )
    with pytest.raises(APIError) as caught:
        client().task_resources_enabled()
    assert caught.value.code == 401

    monkeypatch.setattr(requests, "get", lambda *a, **k: Response({"enabled": "yes"}))
    with pytest.raises(APIError) as caught:
        client().task_resources_enabled()
    assert caught.value.code == "invalid_response"


def test_task_resources_request_and_parsing(monkeypatch):
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


@pytest.mark.parametrize(
    "payload",
    [
        {"enabled": True, "series": {}, "warnings": [], "raw": "do-not-echo"},
        {"enabled": True, "series": [{"metric": "cpu_cores", "labels": {}}], "warnings": []},
        {
            "enabled": True,
            "series": [{
                "metric": "cpu_cores", "labels": {},
                "samples": [{"timestampSeconds": 1, "value": "NaN"}],
            }],
            "warnings": [],
        },
        {"enabled": True, "series": [], "warnings": [{"code": "x"}]},
        {
            "enabled": True,
            "series": [{
                "metric": "cpu_cores", "labels": {},
                "samples": [{"timestampSeconds": 1.7e12, "value": 1.0}],
            }],
            "warnings": [],
        },
        {
            "enabled": True,
            "series": [{
                "metric": "cpu_cores", "labels": {},
                "samples": [{"timestampSeconds": -1, "value": 1.0}],
            }],
            "warnings": [],
        },
        {"series": [], "warnings": []},
    ],
)
def test_task_resources_rejects_malformed_payload(monkeypatch, payload):
    monkeypatch.setattr(requests, "get", lambda *a, **k: Response(payload))
    with pytest.raises(APIError) as caught:
        client().get_task_resources("c1", start=0, end=900, step=15)
    assert caught.value.code == "invalid_response"
    assert "do-not-echo" not in str(caught.value)
    assert caught.value.details is None


def test_task_resources_validates_range_before_request(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("no request expected"))
    with pytest.raises(ValueError):
        client().get_task_resources("c1", start=True, end=900, step=15)
    with pytest.raises(ValueError):
        client().get_task_resources("c1", start=0, end=900, step=15, allocation_id="")


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

    monkeypatch.setattr(requests, "get", lambda *a, **k: Response({"resourcePools": [{}]}))
    with pytest.raises(APIError) as caught:
        client().list_resource_pools()
    assert caught.value.code == "invalid_response"


def test_gpu_device_models_skip_hidden_and_non_accelerator_devices(monkeypatch):
    agents = {"agents": [
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
    ]}
    monkeypatch.setattr(requests, "get", lambda *a, **k: Response(agents))
    assert client().list_gpu_devices() == {"GPU-1": "Model X", "GPU-2": "Model Y"}

    monkeypatch.setattr(requests, "get", lambda *a, **k: Response({"agents": ["bad"]}))
    with pytest.raises(APIError) as caught:
        client().list_gpu_devices()
    assert caught.value.code == "invalid_response"


def test_cpu_only_allocation_has_zero_slots(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: Response({"allocation": {
        "allocationId": "c.1", "slots": 0,
    }}))
    assert client().get_allocation("c.1")["slots"] == 0
