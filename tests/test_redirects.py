"""A mutation never follows a redirect: its outcome is unconfirmed instead."""

from __future__ import annotations

import io
import json

import pytest
import requests
from urllib3 import HTTPResponse
from urllib3.exceptions import MaxRetryError, NewConnectionError

from determined_compute.compute import ComputeProfile, ComputeService
from determined_compute.core.api_client import APIError, DeterminedAPIClient, SubmissionUncertainError

MASTER = "http://master:8080"
COMMAND_ID = "12345678-1234-5678-9234-567812345678"
GENERIC_ID = "3f1c2b8e-1111-4c7a-9d55-0123456789ab"
# A redirect target on another host whose query carries a secret no error may repeat.
ELSEWHERE = "https://login.example/landing?token=secret"
REDIRECTS = [301, 302, 303, 307, 308]

# Every mutation the client sends, as (id, path, call).
MUTATIONS = [
    ("launch-command", "/api/v1/commands", lambda c: c.launch_task("command", {"entrypoint": ["true"]})),
    ("launch-shell", "/api/v1/shells", lambda c: c.launch_task("shell", {})),
    ("launch-experiment", "/api/v1/experiments", lambda c: c.launch_task("experiment", {"entrypoint": "true"})),
    ("launch-generic", "/api/v1/generic-tasks", lambda c: c.launch_task("generic", {"entrypoint": ["true"]})),
    ("kill-command", f"/api/v1/commands/{COMMAND_ID}/kill", lambda c: c.cancel_task("command", COMMAND_ID)),
    ("cancel-experiment", "/api/v1/experiments/7/cancel", lambda c: c.cancel_task("experiment", "7")),
    ("kill-generic", f"/api/v1/tasks/{GENERIC_ID}/kill", lambda c: c.cancel_task("generic", GENERIC_ID)),
    ("pause-experiment", "/api/v1/experiments/7/pause", lambda c: c.pause_task("experiment", "7")),
    ("activate-experiment", "/api/v1/experiments/7/activate", lambda c: c.unpause_task("experiment", "7")),
    ("pause-generic", f"/api/v1/tasks/{GENERIC_ID}/pause", lambda c: c.pause_task("generic", GENERIC_ID)),
    ("unpause-generic", f"/api/v1/tasks/{GENERIC_ID}/unpause", lambda c: c.unpause_task("generic", GENERIC_ID)),
]
MUTATION_IDS = [mutation[0] for mutation in MUTATIONS]


class Transport:
    """Canned replies behind requests' own HTTPAdapter; any other request is refused, as by a
    host that is down."""

    def __init__(self) -> None:
        self.replies = {}
        self.sent = []
        self.bodies = []

    def answer(self, method, path, status, payload=None, headers=None):
        body = b"" if payload is None else json.dumps(payload).encode()
        self.replies[(method, MASTER + path)] = (status, body, headers or {})

    def send(self, adapter, request):
        self.sent.append((request.method, request.url))
        self.bodies.append(request.body)
        reply = self.replies.get((request.method, request.url))
        if reply is None:
            reason = NewConnectionError(None, "Failed to establish a new connection: refused")
            raise requests.exceptions.ConnectionError(MaxRetryError(None, request.url, reason), request=request)
        status, body, headers = reply
        raw = HTTPResponse(body=io.BytesIO(body), headers=headers, status=status, preload_content=False)
        return adapter.build_response(request, raw)


@pytest.fixture
def transport(monkeypatch):
    fake = Transport()
    monkeypatch.setattr(
        requests.adapters.HTTPAdapter, "send", lambda adapter, request, **kwargs: fake.send(adapter, request)
    )
    return fake


def client():
    return DeterminedAPIClient("master:8080", api_token="token")


def service():
    profile = ComputeProfile.from_dict({
        "mounts": [{"host_path": "/shared", "container_path": "/shared"}],
        "defaults": {"image": "image", "pool": "pool"},
    })
    return ComputeService(client(), profile)


@pytest.mark.parametrize("status", REDIRECTS)
@pytest.mark.parametrize(("path", "call"), [mutation[1:] for mutation in MUTATIONS], ids=MUTATION_IDS)
def test_no_mutation_follows_a_redirect(transport, status, path, call):
    transport.answer("POST", path, status, headers={"Location": ELSEWHERE})

    with pytest.raises(SubmissionUncertainError) as caught:
        call(client())

    assert transport.sent == [("POST", MASTER + path)]
    error = caught.value
    assert error.code == "submission_uncertain" and error.retryable is False
    assert error.details["status_code"] == status
    message = str(error)
    assert f"HTTP {status}" in message and "/landing" in message and "not followed" in message
    assert "token" not in message and "login.example" not in message
    assert "server error" not in message


@pytest.mark.parametrize(("path", "call"), [mutation[1:] for mutation in MUTATIONS], ids=MUTATION_IDS)
def test_an_empty_3xx_without_a_location_is_no_acknowledgement(transport, path, call):
    transport.answer("POST", path, 300)

    with pytest.raises(SubmissionUncertainError) as caught:
        call(client())

    assert transport.sent == [("POST", MASTER + path)]
    assert "HTTP 300" in str(caught.value) and caught.value.details["status_code"] == 300


@pytest.mark.parametrize(("path", "call"), [mutation[1:] for mutation in MUTATIONS], ids=MUTATION_IDS)
def test_a_refused_connection_is_still_a_transport_error(transport, path, call):
    with pytest.raises(APIError) as caught:
        call(client())

    assert transport.sent == [("POST", MASTER + path)]
    assert not isinstance(caught.value, SubmissionUncertainError)
    assert caught.value.code == "transport_error" and caught.value.retryable is True


def test_a_2xx_is_unchanged(transport):
    transport.answer("POST", "/api/v1/experiments/7/cancel", 200)
    transport.answer("POST", "/api/v1/commands", 200, {"command": {"id": COMMAND_ID, "state": "STATE_QUEUED"}})

    assert client().cancel_task("experiment", "7") == {"id": "7", "acknowledged": True}
    assert client().launch_task("command", {"entrypoint": ["true"]}) == {"id": COMMAND_ID, "state": "STATE_QUEUED"}


def test_a_redirected_launch_whose_follow_up_fails_keeps_its_marker(transport):
    # The redirect target is unreachable: following it used to report that nothing was sent.
    transport.answer("POST", "/api/v1/commands", 303, headers={"Location": ELSEWHERE})

    with pytest.raises(SubmissionUncertainError) as caught:
        service().launch({"name": "probe", "command": "true", "workdir": "/shared/work",
                          "output_dir": "/shared/out", "allow_queue": True})

    assert transport.sent == [("POST", MASTER + "/api/v1/commands")]
    marker = caught.value.details["submission_marker"]
    assert caught.value.details == {"kind": "command", "submission_marker": marker}
    assert f"COMPUTE_SUBMISSION_MARKER={marker}".encode() in transport.bodies[0]
    message = str(caught.value)
    assert "HTTP 303" in message and "/landing" in message and "not followed" in message
    assert "does not prove that the submission failed" in message and "token" not in message


def test_a_redirected_cancel_has_an_unknown_outcome(transport):
    transport.answer("GET", "/api/v1/me", 200, {"user": {"id": 7, "username": "alice"}})
    transport.answer("GET", "/api/v1/experiments/7", 200,
                     {"experiment": {"id": 7, "userId": 7, "state": "STATE_ACTIVE"}})
    transport.answer("POST", "/api/v1/experiments/7/cancel", 303, headers={"Location": ELSEWHERE})

    with pytest.raises(SubmissionUncertainError) as caught:
        service().cancel("experiment", 7)

    assert transport.sent == [
        ("GET", MASTER + "/api/v1/me"),
        ("GET", MASTER + "/api/v1/experiments/7"),
        ("POST", MASTER + "/api/v1/experiments/7/cancel"),
    ]
    assert caught.value.code == "submission_uncertain"
    assert caught.value.details["status_code"] == 303
