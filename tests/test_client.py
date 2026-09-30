"""Client tests against a fake master that answers in the recorded gateway shapes."""

import copy
import json
from urllib.parse import urlsplit

import pytest
import requests
import yaml

from determined_compute.client import APIError, Client, normalize_api_url, redact

JOB = "65a4298a-48b9-4ef1-aae5-8ecbddb1ce24"
OTHER_JOB = "8f85422a-1226-424f-84e8-6d86508ed0f2"
TASK = "0cdc6ee6-70fc-4071-96fe-e001d8e0f390"
TRIAL_TASK = "1.2754dad4-b17d-40f6-8f18-7b65f1b5d6df"
DIGEST = "b" * 64
NEW_DIGEST = "f" * 64
FILES = [
    {"path": "ctx", "type": 53, "content": "", "mtime": "0", "mode": 16877, "uid": 0, "gid": 0},
    {"path": "ctx/a.txt", "type": 48, "content": "aGVsbG8K", "mtime": "0", "mode": 33188,
     "uid": 0, "gid": 0},
]


@pytest.fixture(autouse=True)
def hermetic_credentials(tmp_path, monkeypatch):
    """Keep the caller's master, token and secrets file out of every test."""
    monkeypatch.setenv("DETERMINED_COMPUTE_SECRETS", str(tmp_path / "absent.env"))
    for name in ("DET_MASTER", "DET_MASTER_ADDR", "DET_MASTER_HOST", "DET_API_TOKEN",
                 "DET_USERNAME", "DET_PASSWORD", "DET_VERIFY_SSL"):
        monkeypatch.delenv(name, raising=False)


class Response:
    def __init__(self, payload=None, status=200, lines=None, text=None):
        self.payload = payload
        self.status_code = status
        self.text = text if text is not None else ("" if payload is None else json.dumps(payload))
        self.content = self.text.encode()
        self.lines = lines or []
        self.closed = False

    def json(self):
        if self.payload is None:
            return json.loads(self.text)
        return self.payload

    def iter_lines(self, decode_unicode=True):
        yield from self.lines

    def close(self):
        self.closed = True


def gateway_error(status, code, reason, message):
    return Response({"error": {"code": code, "reason": reason, "error": message}}, status)


class Master:
    """Routes requests by method and path and records each one."""

    def __init__(self, monkeypatch, protocol=1):
        self.calls = []
        self.routes = {("GET", "/api/v1/master"): Response(
            {"version": "0.41.0-dev", "clusterId": "c", "submissionProtocol": protocol}
        )}
        monkeypatch.setattr(requests, "get", self.get)
        monkeypatch.setattr(requests, "post", self.post)

    def route(self, method, path, answer):
        self.routes[(method, path)] = answer

    def _answer(self, method, url, **kwargs):
        path = urlsplit(url).path
        self.calls.append({"method": method, "path": path, **kwargs})
        answer = self.routes.get((method, path))
        if answer is None:
            pytest.fail(f"unexpected {method} {path}")
        if isinstance(answer, BaseException):
            raise answer
        return answer(kwargs) if callable(answer) else answer

    def get(self, url, headers=None, params=None, timeout=None, verify=None, stream=False):
        return self._answer("GET", url, headers=headers or {}, params=params)

    def post(self, url, headers=None, json=None, timeout=None, verify=None):
        return self._answer("POST", url, headers=headers or {}, json=json)

    def paths(self):
        return [(call["method"], call["path"]) for call in self.calls]


def client():
    return Client("master:8080", api_token="token")


def allocation(**overrides):
    value = {
        "taskId": TASK,
        "state": "STATE_QUEUED",
        "allocationId": f"{TASK}.1",
        "slots": 0,
        "exitClass": "EXIT_CLASS_UNSPECIFIED",
        "exitDetail": None,
        "resourcePool": "default",
        "placements": [],
    }
    value.update(overrides)
    return value


def submission(**overrides):
    value = {
        "jobId": JOB,
        "kind": "SUBMISSION_KIND_COMMAND",
        "entityId": TASK,
        "ownerId": 1,
        "owner": "alice",
        "workspaceId": 1,
        "name": "probe",
        "idempotencyKey": "k1",
        "requestDigest": DIGEST,
        "admission": "ADMISSION_QUEUE",
        "submittedAt": "2026-09-30T17:54:08.653Z",
        "endedAt": None,
        "state": "SUBMISSION_STATE_QUEUED",
        "exitClass": "EXIT_CLASS_UNSPECIFIED",
        "exitReason": "",
        "tasks": [{"taskId": TASK, "allocations": [allocation()]}],
    }
    value.update(overrides)
    return value


def submit_response(kind="command", **submission_fields):
    entity = {"id": "", "state": "STATE_UNSPECIFIED", "jobId": ""}
    result = {
        "jobId": JOB,
        "replayed": False,
        "requestDigest": DIGEST,
        "outcome": "ADMISSION_OUTCOME_QUEUED",
        "effectiveConfig": None,
    }
    result.update(submission_fields)
    return Response({kind: entity, "config": None, "warnings": [], "submission": result})


# URL, credentials and the protocol gate


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://cluster.example.org/", "https://cluster.example.org"),
        ("host:8443", "http://host:8443"),
        ("[::1]", "http://[::1]:8080"),
        ("::1", "http://[::1]:8080"),
        (None, "http://localhost:8080"),
    ],
)
def test_url_normalization(given, expected):
    assert normalize_api_url(given) == expected


def test_url_must_not_carry_credentials():
    with pytest.raises(ValueError, match="credentials"):
        Client("https://user:secret@cluster.example.org", api_token="token")


def test_construction_touches_no_network_and_reads_the_secrets_file(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("network"))
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("network"))
    secrets = tmp_path / "secrets.env"
    secrets.write_text("DET_MASTER=https://secret.example\nDET_USERNAME=alice\nDET_PASSWORD=pw\n")

    assert Client(secrets_path=secrets).api_url == "https://secret.example"
    monkeypatch.setenv("DET_MASTER", "https://environment.example")
    resolved = Client(secrets_path=secrets)
    assert resolved.api_url == "https://environment.example"
    assert "pw" not in repr(resolved) and "alice" not in repr(resolved)


def test_protocol_gate_reads_the_master_without_credentials(monkeypatch):
    master = Master(monkeypatch)

    assert client().check_protocol() == {"submission_protocol": 1}
    assert master.calls[0]["path"] == "/api/v1/master"
    assert master.calls[0]["headers"] == {}


@pytest.mark.parametrize(
    ("protocol", "found"), [(None, "no submission protocol"), (0, "protocol 0")]
)
def test_protocol_gate_refuses_an_old_master_whatever_its_release(monkeypatch, protocol, found):
    master = Master(monkeypatch)
    info = {"version": "0.41.0", "submissionProtocol": protocol}
    if protocol is None:
        del info["submissionProtocol"]
    master.route("GET", "/api/v1/master", Response(info))

    with pytest.raises(APIError) as caught:
        client().check_protocol()
    assert caught.value.code == "protocol_unsupported"
    assert caught.value.retryable is False
    assert found in str(caught.value) and "release 0.41.0" in str(caught.value)
    assert caught.value.details["required"] == 1


def test_no_request_reaches_a_master_that_fails_the_gate(monkeypatch):
    master = Master(monkeypatch, protocol=0)

    with pytest.raises(APIError) as caught:
        client().submit("command", {"entrypoint": ["true"]}, dry_run=True)
    assert caught.value.code == "protocol_unsupported"
    assert master.paths() == [("GET", "/api/v1/master")]


def test_gate_runs_once_then_login_once_and_the_token_rides_every_call(monkeypatch):
    master = Master(monkeypatch)
    monkeypatch.setenv("DET_USERNAME", "alice")
    monkeypatch.setenv("DET_PASSWORD", "pw")
    master.route("POST", "/api/v1/auth/login", Response({"token": "session", "user": {}}))
    master.route("GET", f"/api/v1/submissions/{JOB}", Response({"submission": submission()}))
    api = Client("master:8080")

    api.get_submission(JOB)
    api.get_submission(JOB)

    assert master.paths() == [
        ("GET", "/api/v1/master"),
        ("POST", "/api/v1/auth/login"),
        ("GET", f"/api/v1/submissions/{JOB}"),
        ("GET", f"/api/v1/submissions/{JOB}"),
    ]
    assert master.calls[1]["json"] == {"username": "alice", "password": "pw"}
    assert master.calls[1]["headers"] == {}
    assert master.calls[-1]["headers"] == {"Authorization": "Bearer session"}


def test_rejected_login_is_permission_denied_without_the_password(monkeypatch):
    master = Master(monkeypatch)
    monkeypatch.setenv("DET_USERNAME", "alice")
    monkeypatch.setenv("DET_PASSWORD", "hunter2")
    master.route("POST", "/api/v1/auth/login",
                 gateway_error(401, 16, "Unauthenticated", "invalid credentials"))

    with pytest.raises(APIError) as caught:
        Client("master:8080").list_submissions()
    assert caught.value.code == "permission_denied"
    assert caught.value.details == {"reason": "Unauthenticated"}
    assert "hunter2" not in str(caught.value)


def test_unreachable_gate_is_retried_on_the_next_call(monkeypatch):
    master = Master(monkeypatch)
    master.route("GET", "/api/v1/master", requests.ConnectionError("refused"))
    api = client()
    with pytest.raises(APIError) as caught:
        api.list_submissions()
    assert caught.value.code == "unavailable"
    assert caught.value.retryable is True

    master.route("GET", "/api/v1/master", Response({"version": "x", "submissionProtocol": 2}))
    master.route("GET", "/api/v1/submissions", Response({"submissions": [], "nextPageToken": ""}))
    assert api.list_submissions() == {"submissions": [], "next_page_token": None}


# Create routes


def test_command_dry_run_sends_submit_options_and_returns_the_plan(monkeypatch):
    master = Master(monkeypatch)
    effective = {
        "entrypoint": ["/run/determined/command-entrypoint.sh", "sh", "-c", "true"],
        "environment": {"environment_variables": {"cpu": ["TOKEN=secret"]}, "image": "i"},
        "registry_auth": {"password": "secret"},
        "resources": {"resource_pool": "default", "slots": 0},
    }
    master.route("POST", "/api/v1/commands", Response({
        "command": {"id": ""},
        "config": effective,
        "warnings": ["LAUNCH_WARNING_CURRENT_SLOTS_EXCEEDED"],
        "submission": {"jobId": "", "replayed": False, "requestDigest": DIGEST,
                       "outcome": "ADMISSION_OUTCOME_UNSPECIFIED", "effectiveConfig": effective},
    }))
    config = {"entrypoint": ["sh", "-c", "true"], "resources": {"resource_pool": "default"}}

    result = client().submit("command", config, files=FILES, workspace_id=3, dry_run=True)

    body = master.calls[-1]["json"]
    assert body == {
        "config": config,
        "files": FILES,
        "workspace_id": 3,
        "submit": {"admission": "ADMISSION_QUEUE", "dry_run": True},
    }
    assert master.calls[-1]["headers"] == {"Authorization": "Bearer token"}
    assert result == {
        "job_id": None,
        "replayed": False,
        "request_digest": DIGEST,
        "outcome": None,
        "effective_config": {
            "entrypoint": effective["entrypoint"],
            "environment": {"environment_variables": "[redacted]", "image": "i"},
            "resources": {"resource_pool": "default", "slots": 0},
        },
        "warnings": ["current_slots_exceeded"],
    }


def test_keyed_launch_binds_the_plan_digest(monkeypatch):
    master = Master(monkeypatch)
    master.route("POST", "/api/v1/commands", submit_response())

    result = client().submit(
        "command", {"entrypoint": ["true"]}, idempotency_key="k1", expected_digest=DIGEST
    )

    assert master.calls[-1]["json"]["submit"] == {
        "admission": "ADMISSION_QUEUE", "idempotency_key": "k1", "expected_digest": DIGEST,
    }
    assert result == {
        "job_id": JOB, "replayed": False, "request_digest": DIGEST, "outcome": "queued",
        "effective_config": None, "warnings": [],
    }


def test_replay_reports_the_original_job(monkeypatch):
    master = Master(monkeypatch)
    master.route("POST", "/api/v1/commands", submit_response(replayed=True))

    result = client().submit("command", {}, idempotency_key="k1", expected_digest=DIGEST)
    assert (result["job_id"], result["replayed"]) == (JOB, True)


def test_shell_launch_never_returns_the_private_key(monkeypatch):
    master = Master(monkeypatch)
    response = submit_response("shell")
    response.payload["shell"] = {"id": "s1", "privateKey": "-----BEGIN KEY-----",
                                 "publicKey": "ssh-ed25519 AAAA"}
    response.text = json.dumps(response.payload)
    master.route("POST", "/api/v1/shells", response)

    result = client().submit("shell", {"resources": {"slots": 0}}, idempotency_key="s")

    assert "BEGIN KEY" not in json.dumps(result)
    assert set(result) == {"job_id", "replayed", "request_digest", "outcome",
                           "effective_config", "warnings"}


def test_experiment_sends_yaml_config_model_definition_and_activation(monkeypatch):
    master = Master(monkeypatch)
    master.route("POST", "/api/v1/experiments", submit_response("experiment"))
    config = {
        "name": "exp",
        "entrypoint": "mkdir -p -- /out || exit $?\npython train.py --lr 'yes'",
        "searcher": {"name": "single", "metric": "loss"},
        "hyperparameters": {"flag": "on", "empty": "", "version": "1.10"},
    }

    client().submit("experiment", config, files=FILES[1:], project_id=7,
                    idempotency_key="e", expected_digest=DIGEST)

    body = master.calls[-1]["json"]
    assert isinstance(body["config"], str)
    assert yaml.safe_load(body["config"]) == config
    assert body["model_definition"] == FILES[1:]
    assert body["activate"] is True
    assert body["project_id"] == 7
    assert "files" not in body and "workspace_id" not in body


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"dry_run": True, "idempotency_key": "k"}, "must not carry an idempotency key"),
        ({}, "needs an idempotency key"),
        ({"dry_run": True, "admission": "later"}, "admission"),
        ({"dry_run": True, "project_id": 1}, "workspace_id, not project_id"),
    ],
)
def test_submit_rejects_calls_the_ledger_cannot_make_safe(monkeypatch, kwargs, message):
    master = Master(monkeypatch)
    with pytest.raises(ValueError, match=message):
        client().submit("command", {}, **kwargs)
    with pytest.raises(ValueError, match="kind"):
        client().submit("generic", {}, dry_run=True)
    assert master.calls == []


# Error mapping, from the recorded contract


PLAN_CHANGED = (f"plan_changed: the request digest {NEW_DIGEST} differs from the expected "
                f"digest {DIGEST}")
KEY_USED = f'idempotency key "k1" is already used by job {JOB} for a different request'


@pytest.mark.parametrize(
    ("answer", "kwargs", "code", "retryable", "details"),
    [
        (gateway_error(400, 9, "FailedPrecondition", PLAN_CHANGED),
         {"idempotency_key": "k2", "expected_digest": DIGEST}, "plan_changed", False,
         {"request_digest": NEW_DIGEST, "expected_digest": DIGEST}),
        (gateway_error(409, 6, "AlreadyExists", KEY_USED),
         {"idempotency_key": "k1"}, "key_conflict", False, {"job_id": JOB}),
        (gateway_error(501, 12, "Unimplemented", "immediate admission is not supported yet"),
         {"idempotency_key": "k1", "admission": "immediate"}, "admission_unsupported", False, {}),
        (gateway_error(501, 12, "Unimplemented", "immediate admission is not supported yet"),
         {"dry_run": True, "admission": "immediate"}, "admission_unsupported", False, {}),
        (gateway_error(400, 3, "InvalidArgument", "failed to prepare launch params: validating "
                       "resources: request unfulfillable, please try requesting less slots"),
         {"dry_run": True}, "invalid_request", False, {}),
        (gateway_error(400, 3, "InvalidArgument", "unknown admission 7"),
         {"dry_run": True}, "invalid_request", False, {}),
        (gateway_error(401, 16, "Unauthenticated", "invalid credentials"),
         {"dry_run": True}, "permission_denied", False, {"reason": "Unauthenticated"}),
        (gateway_error(403, 7, "PermissionDenied", "no"),
         {"dry_run": True}, "permission_denied", False, {"reason": "PermissionDenied"}),
        (gateway_error(409, 10, "Aborted", "transaction conflict"),
         {"dry_run": True}, "unavailable", True, {}),
        (Response(text="<html>bad gateway</html>", status=502),
         {"dry_run": True}, "unavailable", True, {}),
        (Response({"error": {"code": 5, "error": "no reason given"}}, 404),
         {"dry_run": True}, "not_found", False, {}),
        (Response({"error": {"code": [5], "reason": None}}, 404),
         {"dry_run": True}, "not_found", False, {}),
        (gateway_error(501, 12, "Unimplemented", "Not Implemented"),
         {"dry_run": True}, "protocol_unsupported", False, {}),
    ],
)
def test_create_errors_map_to_stable_codes(monkeypatch, answer, kwargs, code, retryable, details):
    master = Master(monkeypatch)
    master.route("POST", "/api/v1/commands", answer)

    with pytest.raises(APIError) as caught:
        client().submit("command", {"entrypoint": ["true"]}, **kwargs)

    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert caught.value.details == details
    if answer.payload is not None and "error" in answer.payload["error"]:
        assert str(caught.value).startswith(answer.payload["error"]["error"])


def test_experiment_immediate_admission_and_config_errors(monkeypatch):
    master = Master(monkeypatch)
    refusal = gateway_error(400, 3, "InvalidArgument",
                            "experiments do not support immediate admission")
    master.route("POST", "/api/v1/experiments", refusal)
    with pytest.raises(APIError) as caught:
        client().submit("experiment", {}, dry_run=True, admission="immediate")
    assert caught.value.code == "admission_unsupported"

    # Every experiment config error is Internal; on a dry run it is a plan error.
    invalid = gateway_error(500, 13, "Internal", "invalid experiment configuration: <config>."
                            "searcher: is not an object")
    master.route("POST", "/api/v1/experiments", invalid)
    with pytest.raises(APIError) as caught:
        client().submit("experiment", {}, dry_run=True)
    assert (caught.value.code, caught.value.retryable) == ("invalid_request", False)
    assert "searcher" in str(caught.value)


@pytest.mark.parametrize(
    "answer",
    [
        gateway_error(500, 13, "Internal", "commit failed"),
        gateway_error(503, 14, "Unavailable", "shutting down"),
        requests.ConnectionError("reset"),
        Response(text="not json", status=200),
        Response({"command": {}, "config": None, "warnings": []}),
    ],
)
def test_an_unknown_launch_outcome_is_retried_with_the_same_key(monkeypatch, answer):
    master = Master(monkeypatch)
    master.route("POST", "/api/v1/commands", answer)

    with pytest.raises(APIError) as caught:
        client().submit("command", {}, idempotency_key="k1", expected_digest=DIGEST)

    assert caught.value.retryable is True
    assert caught.value.code in {"internal", "unavailable", "invalid_response"}
    assert "same idempotency key" in str(caught.value)


def test_a_dry_run_transport_failure_is_retryable_without_a_key_hint(monkeypatch):
    master = Master(monkeypatch)
    master.route("POST", "/api/v1/commands", requests.Timeout("slow"))

    with pytest.raises(APIError) as caught:
        client().submit("command", {}, dry_run=True)
    assert (caught.value.code, caught.value.retryable) == ("unavailable", True)
    assert "idempotency" not in str(caught.value)


def test_read_errors_are_not_retryable_unless_the_master_is_unavailable(monkeypatch):
    master = Master(monkeypatch)
    master.route("GET", "/api/v1/submissions/not-a-job",
                 gateway_error(404, 5, "NotFound", "submission 'not-a-job' not found"))
    with pytest.raises(APIError) as caught:
        client().get_submission("not-a-job")
    assert (caught.value.code, caught.value.retryable) == ("not_found", False)
    assert str(caught.value) == "submission 'not-a-job' not found"

    master.route("GET", f"/api/v1/submissions/{JOB}", gateway_error(500, 13, "Internal", "db"))
    with pytest.raises(APIError) as caught:
        client().get_submission(JOB)
    assert (caught.value.code, caught.value.retryable) == ("internal", False)


def test_errors_never_carry_the_token(monkeypatch):
    master = Master(monkeypatch)
    master.route("GET", f"/api/v1/submissions/{JOB}", requests.ConnectionError("Bearer token"))
    with pytest.raises(APIError) as caught:
        client().get_submission(JOB)
    assert "token" not in str(caught.value).lower()
    assert caught.value.__cause__ is None


# Submission reads


def test_get_submission_parses_the_ledger_record(monkeypatch):
    master = Master(monkeypatch)
    ended = allocation(
        state="STATE_TERMINATED",
        exitClass="EXIT_CLASS_WORKLOAD_FAILED",
        exitReason="exit code 1",
        exitDetail={"failure_type": "TaskError", "exit_code": 1},
        statusCode=1,
        isReady=False,
        startTime="2026-09-30T18:00:00Z",
        endTime="2026-09-30T18:10:00.5Z",
        placements=[{"node": "node-a", "acceleratorUuids": ["GPU-1"]}],
    )
    record = submission(
        kind="SUBMISSION_KIND_EXPERIMENT", entityId="1", projectId=4,
        state="SUBMISSION_STATE_FAILED", exitClass="EXIT_CLASS_WORKLOAD_FAILED",
        exitReason="exit code 1", endedAt="2026-09-30T18:10:01Z",
        tasks=[{"taskId": TRIAL_TASK, "trialId": 1, "allocations": [ended]}],
    )
    master.route("GET", f"/api/v1/submissions/{JOB}", Response({"submission": record}))

    assert client().get_submission(JOB) == {
        "job_id": JOB,
        "kind": "experiment",
        "entity_id": "1",
        "name": "probe",
        "owner_id": 1,
        "owner": "alice",
        "workspace_id": 1,
        "project_id": 4,
        "idempotency_key": "k1",
        "request_digest": DIGEST,
        "admission": "queue",
        "submitted_at": "2026-09-30T17:54:08.653Z",
        "ended_at": "2026-09-30T18:10:01Z",
        "state": "failed",
        "exit_class": "workload_failed",
        "exit_reason": "exit code 1",
        "tasks": [{
            "task_id": TRIAL_TASK,
            "trial_id": 1,
            "allocations": [{
                "allocation_id": f"{TASK}.1",
                "state": "terminated",
                "is_ready": False,
                "start_time": "2026-09-30T18:00:00Z",
                "end_time": "2026-09-30T18:10:00.5Z",
                "slots": 0,
                "resource_pool": "default",
                "exit_class": "workload_failed",
                "exit_reason": "exit code 1",
                "exit_detail": {"failure_type": "TaskError", "exit_code": 1},
                "status_code": 1,
                "placements": [{"node": "node-a", "accelerator_uuids": ["GPU-1"]}],
            }],
        }],
    }


def test_a_job_created_without_submit_options_has_no_key_or_digest(monkeypatch):
    master = Master(monkeypatch)
    record = submission()
    del record["idempotencyKey"], record["requestDigest"]
    master.route("GET", f"/api/v1/submissions/{JOB}", Response({"submission": record}))

    parsed = client().get_submission(JOB)
    assert parsed["idempotency_key"] is None and parsed["request_digest"] is None
    assert parsed["exit_class"] is None and parsed["exit_reason"] is None
    assert parsed["tasks"][0]["allocations"][0]["state"] == "queued"
    assert parsed["tasks"][0]["trial_id"] is None


@pytest.mark.parametrize(
    "broken",
    [
        {"jobId": ""},
        {"kind": "COMMAND"},
        {"ownerId": "1"},
        {"tasks": None},
        {"tasks": [{"taskId": TASK, "allocations": [allocation(slots="0")]}]},
        {"tasks": [{"taskId": TASK, "allocations": [allocation(placements=[{"node": 1}])]}]},
        {"tasks": [{"taskId": TASK, "allocations": [allocation(placements=["node-a"])]}]},
        {"tasks": [{"taskId": TASK, "allocations": ["not an allocation"]}]},
        {"tasks": ["not a task"]},
    ],
)
def test_a_malformed_submission_is_refused(monkeypatch, broken):
    master = Master(monkeypatch)
    record = submission(**broken)
    master.route("GET", f"/api/v1/submissions/{JOB}", Response({"submission": record}))
    with pytest.raises(APIError) as caught:
        client().get_submission(JOB)
    assert caught.value.code == "invalid_response"


def test_list_submissions_filters_and_pages(monkeypatch):
    master = Master(monkeypatch)
    master.route("GET", "/api/v1/submissions", Response({
        "submissions": [submission(), submission(jobId=OTHER_JOB)], "nextPageToken": "sealed",
    }))

    page = client().list_submissions(kind="command", state="queued", limit=2,
                                     submitted_after="2026-09-30T00:00:00Z", page_token="prev")

    assert master.calls[-1]["params"] == {
        "kind": "SUBMISSION_KIND_COMMAND",
        "state": "SUBMISSION_STATE_QUEUED",
        "submittedAfter": "2026-09-30T00:00:00Z",
        "limit": 2,
        "pageToken": "prev",
    }
    assert [item["job_id"] for item in page["submissions"]] == [JOB, OTHER_JOB]
    assert page["next_page_token"] == "sealed"
    client().list_submissions()
    assert master.calls[-1]["params"] == {}


@pytest.mark.parametrize(
    "kwargs",
    [{"kind": "generic"}, {"state": "QUEUED"}, {"state": "done"}, {"limit": 0},
     {"limit": 1001}, {"limit": True}, {"submitted_after": ""}],
)
def test_list_submissions_validates_filters_before_any_request(monkeypatch, kwargs):
    master = Master(monkeypatch)
    with pytest.raises(ValueError):
        client().list_submissions(**kwargs)
    assert master.calls == []


def test_cancel_returns_the_recorded_snapshot(monkeypatch):
    master = Master(monkeypatch)
    snapshot = submission(tasks=[{"taskId": TASK, "allocations": [allocation(
        state="STATE_TERMINATED", exitClass="EXIT_CLASS_NONE",
        exitReason="allocation aborted after exit before start: user requested kill",
    )]}])
    master.route("POST", f"/api/v1/submissions/{JOB}/cancel", Response({"submission": snapshot}))

    result = client().cancel_submission(JOB)

    assert master.calls[-1]["json"] == {}
    # The job reads queued until the kill lands; its allocation already ended.
    assert result["state"] == "queued"
    assert result["tasks"][0]["allocations"][0]["state"] == "terminated"
    assert result["tasks"][0]["allocations"][0]["end_time"] is None


def test_cancel_transport_failure_is_retryable(monkeypatch):
    master = Master(monkeypatch)
    master.route("POST", f"/api/v1/submissions/{JOB}/cancel", requests.ConnectionError())
    with pytest.raises(APIError) as caught:
        client().cancel_submission(JOB)
    assert (caught.value.code, caught.value.retryable) == ("unavailable", True)


def test_job_ids_are_quoted_into_the_path(monkeypatch):
    master = Master(monkeypatch)
    master.route("GET", "/api/v1/submissions/a%2Fb", Response({"submission": submission()}))
    client().get_submission("a/b")
    with pytest.raises(ValueError):
        client().get_submission("")


# Logs and trials


def log_line(index, **fields):
    value = {"id": str(index), "timestamp": f"2026-09-30T17:54:0{index}Z",
             "message": f"[ts] || INFO: line {index}\n", "level": "LOG_LEVEL_INFO",
             "taskId": TASK, "log": f"line {index}\n", "source": "master", "stdtype": "stdout"}
    value.update(fields)
    return json.dumps({"result": value})


def test_task_logs_are_the_tail_oldest_first(monkeypatch):
    master = Master(monkeypatch)
    response = Response(lines=[log_line(3), "", log_line(2, allocationId=f"{TASK}.1", rankId=0)])
    master.route("GET", f"/api/v1/tasks/{TASK}/logs", response)

    entries = client().task_logs(TASK, tail=2)

    assert master.calls[-1]["params"] == {"limit": 2, "follow": False, "orderBy": "ORDER_BY_DESC"}
    assert entries == [
        {"timestamp": "2026-09-30T17:54:02Z", "level": "info", "source": "master",
         "stdtype": "stdout", "allocation_id": f"{TASK}.1", "rank_id": 0, "log": "line 2\n"},
        {"timestamp": "2026-09-30T17:54:03Z", "level": "info", "source": "master",
         "stdtype": "stdout", "allocation_id": None, "rank_id": None, "log": "line 3\n"},
    ]
    assert response.closed
    assert client().task_logs(TASK, tail=0) == []
    with pytest.raises(ValueError):
        client().task_logs(TASK, tail=-1)


def test_task_log_stream_errors_are_classified(monkeypatch):
    master = Master(monkeypatch)
    error = json.dumps({"error": {"grpcCode": 5, "httpCode": 404, "message": "task not found"}})
    master.route("GET", f"/api/v1/tasks/{TASK}/logs", Response(lines=[error]))
    with pytest.raises(APIError) as caught:
        client().task_logs(TASK)
    assert (caught.value.code, str(caught.value)) == ("not_found", "task not found")

    master.route("GET", f"/api/v1/tasks/{TASK}/logs", Response(lines=["{not json"]))
    with pytest.raises(APIError) as caught:
        client().task_logs(TASK)
    assert caught.value.code == "invalid_response"


def test_get_trial(monkeypatch):
    master = Master(monkeypatch)
    master.route("GET", "/api/v1/trials/7", Response({"trial": {"id": 7, "experimentId": 3}}))
    assert client().get_trial(7) == {"id": 7, "experimentId": 3}
    master.route("GET", "/api/v1/trials/8", Response({"trial": {"id": 9}}))
    with pytest.raises(APIError):
        client().get_trial(8)
    with pytest.raises(ValueError):
        client().get_trial(0)


# Task resources and the cluster


@pytest.mark.parametrize("enabled", [True, False])
def test_task_resources_capability(monkeypatch, enabled):
    master = Master(monkeypatch)
    master.route("GET", "/api/v1/task-resources/capability", Response({"enabled": enabled}))
    assert client().task_resources_enabled() is enabled


def test_task_resources_request_and_parsing(monkeypatch):
    master = Master(monkeypatch)
    master.route("GET", f"/api/v1/tasks/{TASK}/resources", Response({
        "enabled": True,
        "series": [{
            "metric": "gpu_utilization_percent",
            "labels": {"allocationId": f"{TASK}.1", "node": "", "gpuUuid": "GPU-1"},
            "samples": [{"timestampSeconds": 100, "value": 0},
                        {"timestampSeconds": 115.5, "value": None},
                        {"timestampSeconds": 130}],
        }],
        "warnings": [{"code": "gpu_full_device", "message": "whole device"}],
    }))

    result = client().get_task_resources(TASK, start=100, end=200, step=15,
                                         allocation_id=f"{TASK}.1")

    assert master.calls[-1]["params"] == {"start": 100, "end": 200, "step": 15,
                                          "allocationId": f"{TASK}.1"}
    assert result == {
        "enabled": True,
        "series": [{"metric": "gpu_utilization_percent",
                    "labels": {"allocation_id": f"{TASK}.1", "node": None, "gpu_uuid": "GPU-1"},
                    "samples": [[100, 0], [115.5, None], [130, None]]}],
        "warnings": [{"code": "gpu_full_device", "message": "whole device"}],
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"enabled": True, "series": {}, "warnings": []},
        {"enabled": True, "series": [{"metric": "", "samples": []}], "warnings": []},
        {"enabled": True, "series": [{"metric": "m", "samples": [{"timestampSeconds": -1}]}],
         "warnings": []},
        {"enabled": True, "series": [{"metric": "m", "samples": [
            {"timestampSeconds": 1, "value": float("nan")}]}], "warnings": []},
        {"enabled": True, "series": [], "warnings": [{"code": 1}]},
    ],
)
def test_task_resources_rejects_malformed_payload(monkeypatch, payload):
    master = Master(monkeypatch)
    master.route("GET", f"/api/v1/tasks/{TASK}/resources", Response(payload))
    with pytest.raises(APIError) as caught:
        client().get_task_resources(TASK, start=0, end=1, step=15)
    assert caught.value.code == "invalid_response"


def test_task_resources_validate_range_before_request(monkeypatch):
    master = Master(monkeypatch)
    with pytest.raises(ValueError):
        client().get_task_resources(TASK, start=-1, end=1, step=15)
    assert master.calls == []


def test_resource_pools_are_projected(monkeypatch):
    master = Master(monkeypatch)
    master.route("GET", "/api/v1/resource-pools", Response({"resourcePools": [{
        "name": "default", "description": "", "type": "RESOURCE_POOL_TYPE_STATIC",
        "numAgents": 0, "slotsAvailable": 0, "slotsUsed": 0, "slotType": "TYPE_UNSPECIFIED",
        "auxContainerCapacity": 0, "auxContainersRunning": 0, "slotsPerAgent": -1,
        "startupScript": "secret-ish", "details": {"priorityScheduler": {}},
    }], "pagination": {}}))

    assert client().list_resource_pools() == [{
        "name": "default", "description": None, "type": "static", "num_agents": 0,
        "slots_available": 0, "slots_used": 0, "slot_type": None, "slots_per_agent": None,
        "aux_container_capacity": 0, "aux_containers_running": 0,
    }]
    assert master.calls[-1]["params"] == {"limit": 0}


def test_agents_list_devices_and_hide_masked_uuids(monkeypatch):
    master = Master(monkeypatch)
    master.route("GET", "/api/v1/agents", Response({"agents": [{
        "id": "agent-a", "enabled": True, "draining": False, "resourcePools": ["gpu"],
        "slots": {
            "0": {"device": {"brand": "NVIDIA A100", "uuid": "GPU-1", "type": "TYPE_CUDA"}},
            "1": {"device": {"brand": "NVIDIA A100", "uuid": "****", "type": "TYPE_CUDA"}},
            "2": {"device": None},
        },
    }]}))

    assert client().list_agents() == [{
        "id": "agent-a", "resource_pools": ["gpu"], "enabled": True, "draining": False,
        "devices": [{"type": "cuda", "brand": "NVIDIA A100", "uuid": "GPU-1"},
                    {"type": "cuda", "brand": "NVIDIA A100", "uuid": None}],
    }]


# Redaction


def test_redaction_covers_secret_aliases_without_masking_innocent_tokens():
    assert redact({
        "wandb_api_key": "secret",
        "Authorization": "Bearer secret",
        "service-credential": "secret",
        "privateKey": "secret",
        "session_key": "secret",
        "cookies": "secret",
        "passwd": "secret",
        "originalConfig": "name: x\npassword: y\n",
        "config": "environment: {}",
        "tokenizer": "bert",
        "context_tokens": 4096,
        "nested": [{"api_token": "secret", "keep": 1}],
        "registry_auth": {"username": "u", "server": "registry.example"},
        "environment": {"environment_variables": ["A=1"], "proxy_environment_variables": {}},
    }) == {
        "tokenizer": "bert",
        "context_tokens": 4096,
        "nested": [{"keep": 1}],
        "environment": {"environment_variables": "[redacted]",
                        "proxy_environment_variables": "[redacted]"},
    }


def test_redaction_leaves_its_input_alone():
    value = {"a": {"password": "x"}}
    before = copy.deepcopy(value)
    redact(value)
    assert value == before
