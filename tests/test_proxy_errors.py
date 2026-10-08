"""Errors that an HTTP proxy, not Determined, produced."""

from __future__ import annotations

import json

import pytest
import requests

from determined_compute.compute import ComputeProfile, ComputeService
from determined_compute.core.api_client import (
    APIError,
    DeterminedAPIClient,
    SubmissionUncertainError,
    _proxy_status_errors,
)


class RawResponse:
    """A response with an arbitrary body and headers, as a proxy may send it."""

    def __init__(self, status, body="", headers=None):
        self.status_code = status
        self.text = body
        self.content = body.encode()
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})
        self.reason = "Service Unavailable"

    def json(self):
        return json.loads(self.text)

    def iter_lines(self, decode_unicode=True):
        return iter(())

    def close(self):
        pass


# What the HTTP proxy in front of this deployment's clients answers when the master is down.
PROXY_503 = {"Proxy-Connection": "close", "Content-Length": "0", "Connection": "close"}
CONNECT_ERRORS = [
    "dns_error", "dns_timeout", "destination_not_found", "destination_unavailable",
    "connection_refused", "connection_timeout", "destination_ip_prohibited",
    "destination_ip_unroutable",
]


def client():
    return DeterminedAPIClient("master:8080", api_token="token")


def answer(monkeypatch, response):
    calls = []

    def send(url, **kwargs):
        calls.append(url)
        return response

    monkeypatch.setattr(requests, "post", send)
    monkeypatch.setattr(requests, "get", send)
    return calls


@pytest.mark.parametrize(
    ("status", "body", "headers"),
    [
        (503, "", PROXY_503),
        (502, "<html><body>Bad Gateway</body></html>", {"Via": "1.1 squid"}),
        (504, "", {"Proxy-Status": "edge; error=http_response_timeout"}),
        (503, "upstream down", {"proxy-connection": "close"}),
    ],
)
def test_a_proxy_answer_to_a_mutation_is_unconfirmed_and_labelled(monkeypatch, status, body, headers):
    calls = answer(monkeypatch, RawResponse(status, body, headers))

    with pytest.raises(SubmissionUncertainError) as caught:
        client().cancel_task("experiment", "7")

    assert len(calls) == 1
    error = caught.value
    assert error.code == "submission_uncertain" and error.retryable is False
    assert error.details["source"] == "proxy"
    assert error.details["status_code"] == status
    assert f"an HTTP proxy, not Determined, answered with HTTP {status}" in str(error)
    assert "master was probably unreachable" in str(error)


@pytest.mark.parametrize("operation", ["pause_task", "unpause_task", "cancel_task"])
def test_a_proxy_answer_to_a_generic_control_keeps_its_label(monkeypatch, operation):
    answer(monkeypatch, RawResponse(503, "", PROXY_503))
    with pytest.raises(SubmissionUncertainError) as caught:
        getattr(client(), operation)("generic", "3f1c2b8e-1111-4c7a-9d55-0123456789ab")
    assert caught.value.details == {"source": "proxy", "status_code": 503}
    assert str(caught.value).startswith("an HTTP proxy, not Determined, answered")


def test_a_proxy_status_error_type_is_reported(monkeypatch):
    answer(monkeypatch, RawResponse(504, "", {"Proxy-Status": "edge; error=http_response_timeout"}))
    with pytest.raises(SubmissionUncertainError) as caught:
        client().cancel_task("experiment", "7")
    assert caught.value.details["proxy_error"] == "http_response_timeout"


def test_a_proxy_answer_to_a_read_is_a_labelled_retryable_error(monkeypatch):
    answer(monkeypatch, RawResponse(503, "", PROXY_503))
    with pytest.raises(APIError) as caught:
        client().get_task("command", "c1")
    assert not isinstance(caught.value, SubmissionUncertainError)
    assert caught.value.code == 503 and caught.value.retryable is True
    assert caught.value.details == {"source": "proxy", "status_code": 503}
    assert "HTTP proxy, not Determined" in str(caught.value)


def test_a_proxy_refusal_is_no_determined_permission_error(monkeypatch):
    answer(monkeypatch, RawResponse(403, "<html>Forbidden</html>", {"Via": "1.1 squid"}))
    with pytest.raises(APIError) as caught:
        client().get_task("command", "c1")
    assert caught.value.code == 403 and caught.value.retryable is False
    assert str(caught.value) == "403 <html>Forbidden</html>"


def test_a_proxy_page_that_quotes_a_pool_refusal_stays_the_proxy_answer(monkeypatch):
    page = '<html>user "alice" may not use resource pool "a100"</html>'
    answer(monkeypatch, RawResponse(403, page, {"Via": "1.1 squid"}))
    with pytest.raises(APIError) as caught:
        client().launch_task("command", {"entrypoint": ["true"]})
    assert not isinstance(caught.value, SubmissionUncertainError)
    assert caught.value.code == 403 and caught.value.details is None
    assert str(caught.value) == f"403 {page}"


def test_a_proxy_authentication_page_gets_no_secrets_file_advice(monkeypatch):
    answer(monkeypatch, RawResponse(401, "<html>Unauthorized</html>", {"Via": "1.1 squid"}))
    with pytest.raises(APIError) as caught:
        client().get_task("command", "c1")
    assert caught.value.code == 401 and caught.value.retryable is False
    assert str(caught.value) == "401 <html>Unauthorized</html>"


@pytest.mark.parametrize(
    ("body", "headers"),
    [
        # A Determined error body is Determined's answer, whatever headers a proxy added.
        (json.dumps({"error": {"code": 14, "reason": "Unavailable", "error": "db down"}}),
         {"Via": "1.1 nginx", "Proxy-Connection": "close"}),
        (json.dumps({"code": 13, "message": "internal"}), {"Via": "1.1 squid"}),
        # Without a proxy header an empty 5xx is not attributed to a proxy.
        ("", {"Content-Length": "0"}),
        ("<html>oops</html>", {}),
    ],
)
def test_other_server_errors_stay_plainly_unconfirmed(monkeypatch, body, headers):
    answer(monkeypatch, RawResponse(503, body, headers))
    with pytest.raises(SubmissionUncertainError) as caught:
        client().cancel_task("experiment", "7")
    assert "source" not in caught.value.details
    assert str(caught.value) == "Determined mutation outcome is unknown after a server error"


@pytest.mark.parametrize("error_type", CONNECT_ERRORS)
@pytest.mark.parametrize("status", [502, 503, 504])
def test_a_proxy_that_never_connected_upstream_is_a_transport_error(monkeypatch, error_type, status):
    calls = answer(
        monkeypatch,
        RawResponse(status, "", {"Proxy-Status": f'edge.example; error={error_type}; details="x"'}),
    )

    with pytest.raises(APIError) as caught:
        client().cancel_task("command", "c1")

    error = caught.value
    assert not isinstance(error, SubmissionUncertainError)
    assert error.code == "transport_error" and error.retryable is True
    assert error.details == {"source": "proxy", "status_code": status, "proxy_error": error_type}
    assert "did not reach the master" in str(error)
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("value", "errors"),
    [
        ("proxy.example; error=connection_refused", ["connection_refused"]),
        ("CDN; error=dns_error; rcode=NXDOMAIN, inner", ["dns_error"]),
        ('a; error="dns_timeout", b;error=http_protocol_error;details="q \\"x\\" y"',
         ["dns_timeout", "http_protocol_error"]),
        ('("a" "b");x, p; error=destination_unavailable', ["destination_unavailable"]),
        ("p; received-status=503", []),
        ("p; error=connection_refused,", []),  # a trailing comma invalidates the field
        ("p; ERROR=dns_error", []),  # keys are lowercase
        ('p; details="unterminated', []),
        ("", []),
    ],
)
def test_proxy_status_is_parsed_as_a_structured_field_list(value, errors):
    assert _proxy_status_errors(value) == errors


def test_an_invalid_proxy_status_is_not_a_connect_failure(monkeypatch):
    answer(monkeypatch, RawResponse(503, "", {"Proxy-Status": "p; error=connection_refused,"}))
    with pytest.raises(SubmissionUncertainError) as caught:
        client().cancel_task("experiment", "7")
    # The header still shows that a proxy answered.
    assert caught.value.details == {"source": "proxy", "status_code": 503}


def test_a_connect_failure_in_any_proxy_of_the_chain_counts(monkeypatch):
    answer(monkeypatch, RawResponse(
        502, "", {"Proxy-Status": "inner; error=connection_refused, outer; received-status=502"},
    ))
    with pytest.raises(APIError) as caught:
        client().cancel_task("command", "c1")
    assert caught.value.code == "transport_error"


@pytest.fixture
def service():
    profile = ComputeProfile.from_dict({
        "mounts": [{"host_path": "/shared", "container_path": "/shared"}],
        "defaults": {"image": "image", "pool": "pool"},
    })
    return ComputeService(DeterminedAPIClient("master:8080", api_token="token"), profile)


REQUEST = {"name": "probe", "command": "true", "workdir": "/shared/work",
           "output_dir": "/shared/out", "allow_queue": True}


def test_an_unconfirmed_launch_through_a_proxy_keeps_its_marker(monkeypatch, service):
    sent = []

    def post(url, **kwargs):
        sent.append(kwargs["json"])
        return RawResponse(503, "", PROXY_503)

    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(SubmissionUncertainError) as caught:
        service.launch(REQUEST)

    details = caught.value.details
    marker = details["submission_marker"]
    assert details == {"kind": "command", "submission_marker": marker, "source": "proxy",
                       "status_code": 503}
    assert f"COMPUTE_SUBMISSION_MARKER={marker}" in sent[0]["config"]["environment"]["environment_variables"]
    assert "an HTTP proxy, not Determined, answered with HTTP 503" in str(caught.value)
    assert "does not prove that the submission failed" in str(caught.value)
    assert len(sent) == 1


def test_a_launch_the_proxy_never_forwarded_is_a_transport_error(monkeypatch, service):
    sent = []

    def post(url, **kwargs):
        sent.append(url)
        return RawResponse(503, "", {**PROXY_503, "Proxy-Status": "proxy; error=dns_error"})

    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(APIError) as caught:
        service.launch(REQUEST)

    assert not isinstance(caught.value, SubmissionUncertainError)
    assert caught.value.code == "transport_error" and caught.value.retryable is True
    assert caught.value.details["proxy_error"] == "dns_error"
    assert len(sent) == 1


def test_the_mcp_error_reports_the_proxy_label():
    pytest.importorskip("mcp")
    from determined_compute.mcp_server import _tool_error

    error = SubmissionUncertainError("unconfirmed", details={
        "kind": "command", "submission_marker": "determined-compute:x", "source": "proxy",
        "status_code": 503, "error": "raw text",
    })
    assert _tool_error(error)["error"]["details"] == {
        "kind": "command", "submission_marker": "determined-compute:x", "source": "proxy",
        "status_code": 503,
    }
