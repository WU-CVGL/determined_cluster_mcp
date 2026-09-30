"""The MCP tool table, its error envelope, and startup behind the protocol gate."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import Client, StdioServerParameters  # noqa: E402

from determined_compute import __version__  # noqa: E402
from determined_compute.client import APIError  # noqa: E402
from determined_compute.mcp_server import (  # noqa: E402
    INSTRUCTIONS,
    Tools,
    build_parser,
    build_tools,
    create_server,
    main,
)
from determined_compute.policy import Policy  # noqa: E402
from determined_compute.storage import StorageAccessConfig, StorageService  # noqa: E402

from fakes import FakeMaster, submission  # noqa: E402

TOOLS = {
    "compute_plan",
    "compute_launch",
    "compute_status",
    "compute_list",
    "compute_logs",
    "compute_usage",
    "compute_resources",
    "storage_check",
    "compute_cancel",
    "storage_sync",
    "storage_fetch",
}
READ_ONLY = {
    "compute_plan",
    "compute_status",
    "compute_list",
    "compute_logs",
    "compute_usage",
    "compute_resources",
    "storage_check",
}
POLICY = (
    "mounts:\n  - host_path: /cluster/shared\n    container_path: /shared\n"
    "defaults:\n  image: image\n  pool: pool\n  slots: 0\n"
)


@pytest.fixture(autouse=True)
def hermetic_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DETERMINED_COMPUTE_SECRETS", str(tmp_path / "absent.env"))
    for name in ("DET_MASTER", "DET_MASTER_ADDR", "DET_MASTER_HOST", "DET_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def make_tools(master=None) -> Tools:
    policy = Policy.from_dict(
        {
            "mounts": [{"host_path": "/cluster/shared", "container_path": "/shared"}],
            "defaults": {"image": "image", "pool": "pool", "slots": 0},
        }
    )
    storage = StorageService(policy, StorageAccessConfig())
    return Tools(master or FakeMaster(), policy, storage)


def run(exercise, timeout=10):
    asyncio.run(asyncio.wait_for(exercise(), timeout=timeout))


def error_of(result):
    assert result.is_error is True
    return json.loads(result.content[0].text.split(": ", 1)[1])["error"]


SPEC = {"kind": "command", "name": "train", "command": "true", "output_dir": "/shared/out"}


def test_the_table_has_the_eleven_tools_with_their_hints_and_defaults():
    async def exercise():
        async with Client(create_server(make_tools())) as client:
            listed = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert set(listed) == TOOLS
            for name, tool in listed.items():
                assert tool.annotations.read_only_hint is (name in READ_ONLY), name
                assert tool.annotations.open_world_hint is True, name
                assert "owner" not in tool.input_schema.get("properties", {})
            assert listed["compute_cancel"].annotations.destructive_hint is True
            assert listed["compute_launch"].annotations.destructive_hint is False
            assert listed["compute_launch"].annotations.idempotent_hint is True

            def properties(name):
                return listed[name].input_schema["properties"]

            assert set(listed["compute_launch"].input_schema["required"]) == {
                "spec",
                "request_id",
                "request_digest",
            }
            plan_schema = json.dumps(listed["compute_plan"].input_schema)
            assert "output_dir" in plan_schema and "discriminator" in plan_schema
            assert properties("compute_list")["limit"]["default"] == 50
            assert properties("compute_logs")["tail"]["default"] == 200
            assert properties("compute_usage")["window_seconds"]["default"] == 3600
            assert properties("compute_usage")["include_samples"]["default"] is False
            for name in ("storage_sync", "storage_fetch"):
                assert properties(name)["dry_run"]["default"] is True
                assert properties(name)["overwrite"]["default"] is False
            assert set(listed["compute_resources"].input_schema.get("required", [])) == set()

    run(exercise)


def test_the_server_names_its_version_and_instructions():
    server = create_server(make_tools())

    assert server.version == __version__ == "1.0.0"
    assert "compute_plan" in INSTRUCTIONS and "request_digest" in INSTRUCTIONS
    assert "allow_queue" not in INSTRUCTIONS and "task_id" not in INSTRUCTIONS


def test_plan_launch_and_status_through_the_protocol():
    master = FakeMaster()

    async def exercise():
        async with Client(create_server(make_tools(master))) as client:
            planned = await client.call_tool("compute_plan", {"spec": SPEC})
            assert planned.is_error is False, planned
            plan = planned.structured_content
            launched = await client.call_tool(
                "compute_launch",
                {
                    "spec": plan["spec"],
                    "request_id": plan["request_id"],
                    "request_digest": plan["request_digest"],
                },
            )
            job = launched.structured_content
            assert job["replayed"] is False and job["outcome"] == "queued"
            status = await client.call_tool("compute_status", {"job_id": job["job_id"]})
            assert status.structured_content["request_id"] == plan["request_id"]
            logs = await client.call_tool("compute_logs", {"job_id": job["job_id"], "tail": 5})
            assert logs.structured_content["lines"] == [{"log": "hello"}]

    run(exercise)


def test_errors_are_json_with_their_code_and_details():
    master = FakeMaster()
    master.jobs["j"] = submission("j")

    async def exercise():
        async with Client(create_server(make_tools(master))) as client:
            missing = error_of(await client.call_tool("compute_status", {"job_id": "nope"}))
            assert missing == {
                "code": "not_found",
                "message": "submission 'nope' not found",
                "retryable": False,
            }
            immediate = error_of(
                await client.call_tool("compute_plan", {"spec": {**SPEC, "admission": "immediate"}})
            )
            assert immediate["code"] == "admission_unsupported"
            arguments = {"spec": SPEC, "request_id": "mine", "request_digest": "d"}
            key = error_of(await client.call_tool("compute_launch", arguments))
            assert key == {
                "code": "invalid_request",
                "message": "request_id must be the UUID that compute_plan returned",
                "retryable": False,
            }
            invalid = await client.call_tool("compute_plan", {"spec": {**SPEC, "kind": "trial"}})
            assert invalid.is_error is True
            assert [call for call in master.calls if call[0] == "submit"] == []

    run(exercise)


def test_a_key_conflict_keeps_the_job_id():
    class Conflicting(FakeMaster):
        def submit(self, *args, **kwargs):
            if kwargs.get("dry_run"):
                return super().submit(*args, **kwargs)
            raise APIError("used by job j", code="key_conflict", details={"job_id": "j"})

    async def exercise():
        async with Client(create_server(make_tools(Conflicting()))) as client:
            plan = (await client.call_tool("compute_plan", {"spec": SPEC})).structured_content
            error = error_of(
                await client.call_tool(
                    "compute_launch",
                    {
                        "spec": plan["spec"],
                        "request_id": plan["request_id"],
                        "request_digest": plan["request_digest"],
                    },
                )
            )
            assert error["code"] == "key_conflict" and error["details"] == {"job_id": "j"}

    run(exercise)


def test_a_slow_call_does_not_block_other_tools():
    class Slow(FakeMaster):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def cancel_submission(self, job_id):
            self.started.set()
            self.release.wait(timeout=2)
            return super().cancel_submission(job_id)

    master = Slow()
    master.jobs["j"] = submission("j")
    server = create_server(make_tools(master))

    async def exercise():
        cancel = asyncio.create_task(server.call_tool("compute_cancel", {"job_id": "j"}))
        for _ in range(100):
            if master.started.is_set():
                break
            await asyncio.sleep(0.01)
        assert master.started.is_set()
        try:
            listed = await asyncio.wait_for(server.call_tool("compute_list", {}), timeout=0.5)
            assert listed.is_error is False
        finally:
            master.release.set()
        await cancel

    run(exercise)


# Startup


class _Master(BaseHTTPRequestHandler):
    body = b"{}"

    def do_GET(self):  # noqa: N802 - the handler's name is fixed by http.server
        if self.path != "/api/v1/master":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


@pytest.fixture
def local_master():
    """A master on an ephemeral loopback port that answers only GET /api/v1/master."""

    def serve(protocol):
        handler = type("Handler", (_Master,), {})
        info = {"version": "0.41.0"}
        if protocol is not None:
            info["submissionProtocol"] = protocol
        handler.body = json.dumps(info).encode()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    servers = []
    yield serve
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def profile(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_text(POLICY, encoding="utf-8")
    return path


@pytest.mark.parametrize("protocol", [None, 0])
def test_startup_refuses_a_master_below_the_protocol(local_master, profile, capsys, protocol):
    url = local_master(protocol)

    assert main(["--profile", str(profile), "--api-url", url]) == 2

    error = capsys.readouterr().err
    assert "submission protocol" in error and "0.41.0" in error


def test_startup_needs_a_profile(monkeypatch, capsys):
    monkeypatch.delenv("DETERMINED_COMPUTE_PROFILE", raising=False)

    assert main(["--api-url", "http://127.0.0.1:9"]) == 2
    assert "--profile or DETERMINED_COMPUTE_PROFILE is required" in capsys.readouterr().err


def test_the_old_owner_and_database_flags_are_gone(profile):
    for flag in ("--owner", "--db"):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--profile", str(profile), flag, "x"])


def test_startup_passes_the_gate_and_builds_the_tools(local_master, profile):
    url = local_master(1)
    tools = build_tools(build_parser().parse_args(["--profile", str(profile), "--api-url", url]))

    assert tools.policy.pool == "pool"
    assert tools.client.api_url == url


def test_stdio_subprocess_serves_the_tools(local_master, profile, tmp_path):
    url = local_master(1)
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "determined_compute.mcp_server", "--profile", str(profile), "--api-url", url],
        env={
            "PYTHONPATH": source_root,
            "DETERMINED_COMPUTE_SECRETS": str(tmp_path / "absent.env"),
        },
        cwd=str(tmp_path),
    )

    async def exercise():
        async with Client(params) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert tools == TOOLS
            checked = await client.call_tool("storage_check", {"path": "/elsewhere"})
            assert error_of(checked)["code"] == "invalid_storage_path"

    run(exercise, timeout=20)
