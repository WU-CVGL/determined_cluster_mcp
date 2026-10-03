from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import Client, StdioServerParameters

from determined_compute.compute import SubmissionUncertainError
from determined_compute.mcp_server import _runtime, build_parser, create_server


COMMAND_ID = "12345678-1234-5678-9234-567812345678"
MARKER = "determined-compute:0b6f8c1e-2d8a-4c1f-9a51-6f1d2e3c4b5a"
ALL_TOOLS = {
    "compute_plan",
    "compute_launch",
    "compute_status",
    "compute_logs",
    "compute_usage",
    "compute_cancel",
    "compute_pause",
    "compute_resume",
    "compute_list",
    "compute_resources",
    "storage_check",
    "storage_sync",
    "storage_fetch",
}


class FakeService:
    def __init__(self) -> None:
        self.calls = []

    def plan(self, request):
        self.calls.append(("plan", request))
        return {"kind": "command", "config": request}

    def launch(self, request):
        self.calls.append(("launch", request))
        return {"kind": "command", "id": COMMAND_ID, "name": "probe"}

    def status(self, kind, task_id):
        self.calls.append(("status", kind, task_id))
        return {"kind": kind, "id": task_id}

    def logs(self, kind, task_id, tail):
        self.calls.append(("logs", kind, task_id, tail))
        return [{"message": "hello"}]

    def usage(
        self, kind, task_id, window_seconds, allocation_id, trial_id, metrics, include_samples
    ):
        self.calls.append((
            "usage", kind, task_id, window_seconds, allocation_id, trial_id, metrics,
            include_samples,
        ))
        return {"kind": kind, "id": task_id, "series": []}

    def cancel(self, kind, task_id):
        self.calls.append(("cancel", kind, task_id))
        return {"kind": kind, "id": task_id, "cancellation_acknowledged": True}

    def pause(self, kind, task_id):
        self.calls.append(("pause", kind, task_id))
        return {"kind": kind, "id": task_id, "pause_acknowledged": True}

    def resume(self, kind, task_id):
        self.calls.append(("resume", kind, task_id))
        return {"kind": kind, "id": task_id, "resume_acknowledged": True}

    def list_tasks(self, kind, limit=50, offset=0, marker=None):
        self.calls.append(("list", kind, limit, offset, marker))
        return {"kind": kind, "tasks": [{"id": COMMAND_ID}]}


def _structured(result):
    return result.structured_content


def _error(result):
    assert result.is_error is True
    assert result.structured_content is None
    return json.loads(result.content[0].text.split(": ", 1)[1])["error"]


def test_real_sdk_client_lists_tools_and_passes_native_ids():
    async def exercise():
        service = FakeService()
        server = create_server(service)
        async with Client(server) as client:
            listed = await client.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            assert set(tools) == ALL_TOOLS - {
                "compute_resources", "storage_check", "storage_sync", "storage_fetch",
            }
            for tool in tools.values():
                properties = tool.input_schema.get("properties", {})
                assert not {"owner", "request_id", "task_id"} & set(properties)
            assert tools["compute_launch"].input_schema["required"] == ["request"]
            assert tools["compute_launch"].annotations.idempotent_hint is False
            assert tools["compute_launch"].annotations.read_only_hint is False
            for name in (
                "compute_status", "compute_logs", "compute_usage", "compute_cancel",
                "compute_pause", "compute_resume",
            ):
                assert set(tools[name].input_schema["required"]) == {"kind", "id"}
            list_schema = tools["compute_list"].input_schema
            assert list_schema["required"] == ["kind"]
            assert list_schema["properties"]["limit"]["default"] == 50
            assert list_schema["properties"]["offset"]["default"] == 0
            assert list_schema["properties"]["marker"]["default"] is None
            assert tools["compute_list"].annotations.read_only_hint is True
            assert tools["compute_list"].annotations.open_world_hint is True
            usage_schema = tools["compute_usage"].input_schema
            assert usage_schema["properties"]["window_seconds"]["default"] == 3600
            assert usage_schema["properties"]["include_samples"]["default"] is False
            assert tools["compute_usage"].annotations.read_only_hint is True
            assert "compute_resources" in tools["compute_usage"].description
            for name in ("compute_pause", "compute_resume"):
                assert tools[name].annotations.read_only_hint is False
                assert tools[name].annotations.open_world_hint is True
            assert tools["compute_pause"].annotations.destructive_hint is True
            assert tools["compute_resume"].annotations.destructive_hint is False
            assert tools["compute_cancel"].annotations.destructive_hint is True
            assert "generic" in tools["compute_pause"].description

            launched = await client.call_tool("compute_launch", {"request": {"command": "true"}})
            assert _structured(launched)["id"] == COMMAND_ID

            # An experiment ID is an integer; other kinds use UUID strings.
            paused = await client.call_tool("compute_pause", {"kind": "experiment", "id": 12})
            assert _structured(paused)["pause_acknowledged"] is True
            resumed = await client.call_tool("compute_resume", {"kind": "experiment", "id": "12"})
            assert _structured(resumed)["resume_acknowledged"] is True
            status = await client.call_tool("compute_status", {"kind": "command", "id": COMMAND_ID})
            assert _structured(status) == {"kind": "command", "id": COMMAND_ID}

            logs = await client.call_tool(
                "compute_logs", {"kind": "command", "id": COMMAND_ID, "tail": 5}
            )
            assert _structured(logs) == {"result": [{"message": "hello"}]}

            await client.call_tool(
                "compute_usage",
                {
                    "kind": "experiment",
                    "id": 12,
                    "window_seconds": 900,
                    "allocation_id": "a.1",
                    "trial_id": 4,
                    "metrics": ["cpu_cores", "gpu_power_watts"],
                    "include_samples": True,
                },
            )
            await client.call_tool("compute_cancel", {"kind": "command", "id": COMMAND_ID})
            listed_tasks = await client.call_tool(
                "compute_list", {"kind": "command", "limit": 7, "offset": 2, "marker": MARKER}
            )
            assert _structured(listed_tasks)["tasks"] == [{"id": COMMAND_ID}]
        assert ("launch", {"command": "true"}) in service.calls
        assert ("pause", "experiment", 12) in service.calls
        assert ("resume", "experiment", "12") in service.calls
        assert ("status", "command", COMMAND_ID) in service.calls
        assert ("logs", "command", COMMAND_ID, 5) in service.calls
        assert (
            "usage", "experiment", 12, 900, "a.1", 4, ["cpu_cores", "gpu_power_watts"], True
        ) in service.calls
        assert ("cancel", "command", COMMAND_ID) in service.calls
        assert ("list", "command", 7, 2, MARKER) in service.calls

    asyncio.run(asyncio.wait_for(exercise(), timeout=10))


def test_tool_errors_are_structured():
    async def exercise():
        server = create_server(FakeService())
        async with Client(server) as client:
            result = await client.call_tool(
                "compute_logs", {"kind": "command", "id": COMMAND_ID, "tail": 0}
            )
            assert _error(result) == {
                "code": "internal_error", "message": "tail must be at least 1",
            }

    asyncio.run(asyncio.wait_for(exercise(), timeout=10))


def test_unconfirmed_launch_error_carries_the_marker():
    class Uncertain(FakeService):
        def launch(self, request):
            error = SubmissionUncertainError(
                f"The command submission is unconfirmed; check compute_list(marker={MARKER!r})"
            )
            error.details = {
                "kind": "command", "submission_marker": MARKER, "status_code": 502,
                "error": "echoed request data",
            }
            raise error

    async def exercise():
        async with Client(create_server(Uncertain())) as client:
            result = await client.call_tool("compute_launch", {"request": {"command": "true"}})
            error = _error(result)
            assert error["code"] == "submission_uncertain"
            assert error["retryable"] is False
            assert error["details"] == {"kind": "command", "submission_marker": MARKER}
            assert MARKER in error["message"]

    asyncio.run(asyncio.wait_for(exercise(), timeout=10))


def test_slow_service_call_does_not_block_other_tools():
    class SlowService(FakeService):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def launch(self, request):
            self.started.set()
            self.release.wait(timeout=2)
            return super().launch(request)

    async def exercise():
        service = SlowService()
        server = create_server(service)
        launch = asyncio.create_task(
            server.call_tool("compute_launch", {"request": {"command": "true"}})
        )
        for _ in range(100):
            if service.started.is_set():
                break
            await asyncio.sleep(0.01)
        assert service.started.is_set()
        try:
            listed = await asyncio.wait_for(
                server.call_tool("compute_list", {"kind": "command"}), timeout=0.5
            )
            assert listed.is_error is False
        finally:
            service.release.set()
        await launch

    asyncio.run(asyncio.wait_for(exercise(), timeout=10))


def _profile(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "mounts:\n  - host_path: /shared\n    container_path: /shared\n"
        "defaults:\n  image: image\n  pool: pool\n",
        encoding="utf-8",
    )
    return profile


def test_stdio_subprocess_initializes_and_calls_offline_plan(tmp_path):
    profile = _profile(tmp_path)
    repo_root = Path(__file__).resolve().parents[1]
    source_root = str(repo_root / "src")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "determined_compute.mcp_server", "--profile", str(profile)],
        env={"PYTHONPATH": source_root},
        cwd=str(tmp_path),
    )

    async def exercise():
        async with Client(params) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert tools == ALL_TOOLS
            result = await client.call_tool(
                "compute_plan",
                {
                    "request": {
                        "command": ["echo", "hello"],
                        "workdir": "/shared/work",
                        "output_dir": "/shared/output",
                    }
                },
            )
            assert result.is_error is False
            assert result.structured_content["kind"] == "command"

    asyncio.run(asyncio.wait_for(exercise(), timeout=10))
    # The server keeps no task records, so it writes no files.
    assert sorted(path.name for path in tmp_path.iterdir()) == ["profile.yaml"]


def test_default_runtime_registers_all_tools(tmp_path):
    args = build_parser().parse_args(["--profile", str(_profile(tmp_path))])
    server = _runtime(args)

    async def exercise():
        async with Client(server) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert tools == ALL_TOOLS
            result = await client.call_tool(
                "compute_plan",
                {
                    "request": {
                        "command": "true",
                        "workdir": "/shared/work",
                        "output_dir": "/shared/output",
                    }
                },
            )
            assert result.is_error is False

    asyncio.run(asyncio.wait_for(exercise(), timeout=10))


@pytest.mark.parametrize("option", ["--db", "--owner"])
def test_removed_task_store_options_are_rejected(option, capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--profile", "profile.yaml", option, "value"])
    assert "unrecognized arguments" in capsys.readouterr().err


class _Master:
    """The state of a Determined master, shared by the clients of two MCP processes."""

    def __init__(self):
        self.entities = {}
        self.cancelled = []


class _MasterClient:
    api_url = "https://det.example.test"

    def __init__(self, master):
        self.master = master

    def get_current_user(self):
        return {"id": "7", "username": "alice"}

    def launch_task(self, kind, config):
        entity = {"id": COMMAND_ID, "userId": 7, "state": "QUEUED",
                  "description": config["description"]}
        self.master.entities[(kind, COMMAND_ID)] = entity
        return dict(entity)

    def get_task(self, kind, task_id):
        return dict(self.master.entities[(kind, task_id)])

    def task_logs(self, kind, task_id, tail):
        return [{"message": f"{kind} {task_id} log"}]

    def cancel_task(self, kind, task_id):
        self.master.cancelled.append((kind, task_id))
        return {"id": task_id, "state": "TERMINATING"}


def test_a_new_server_process_continues_by_native_id(tmp_path, monkeypatch):
    from determined_compute.compute import ComputeProfile, ComputeService

    monkeypatch.chdir(tmp_path)
    profile = ComputeProfile.from_dict({
        "mounts": [{"host_path": "/shared", "container_path": "/shared"}],
        "defaults": {"image": "image", "pool": "pool"},
    })
    master = _Master()
    request = {"name": "probe", "command": "true", "workdir": "/shared/work",
               "output_dir": "/shared/out", "allow_queue": True}

    async def launch():
        server = create_server(ComputeService(_MasterClient(master), profile))
        async with Client(server) as client:
            return _structured(await client.call_tool("compute_launch", {"request": request}))

    async def continue_in_a_new_process(task):
        server = create_server(ComputeService(_MasterClient(master), profile))
        async with Client(server) as client:
            arguments = {"kind": task["kind"], "id": task["id"]}
            status = _structured(await client.call_tool("compute_status", arguments))
            logs = _structured(await client.call_tool("compute_logs", {**arguments, "tail": 2}))
            cancel = _structured(await client.call_tool("compute_cancel", arguments))
            return status, logs, cancel

    task = asyncio.run(asyncio.wait_for(launch(), timeout=10))
    status, logs, cancel = asyncio.run(asyncio.wait_for(continue_in_a_new_process(task), 10))

    assert (task["kind"], task["id"]) == ("command", COMMAND_ID)
    assert status["name"] == "probe" and status["state"] == "QUEUED"
    assert logs == {"result": [{"message": f"command {COMMAND_ID} log"}]}
    assert cancel["cancellation_acknowledged"] is True
    assert master.cancelled == [("command", COMMAND_ID)]
    assert list(tmp_path.iterdir()) == []
