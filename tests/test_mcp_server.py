from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import Client, StdioServerParameters

from determined_compute.mcp_server import _runtime, build_parser, create_server


class FakeService:
    def __init__(self) -> None:
        self.calls = []

    def plan(self, request):
        self.calls.append(("plan", request))
        return {"kind": "command", "config": request}

    def launch(self, request, request_id, owner):
        self.calls.append(("launch", request, request_id, owner))
        return {"task_id": "task-1", "owner": owner}

    def status(self, task_id, owner):
        self.calls.append(("status", task_id, owner))
        return {"task_id": task_id, "owner": owner}

    def logs(self, task_id, owner, tail):
        self.calls.append(("logs", task_id, owner, tail))
        return [{"message": "hello"}]

    def usage(
        self, task_id, owner, window_seconds, allocation_id, trial_id, metrics, include_samples
    ):
        self.calls.append((
            "usage", task_id, owner, window_seconds, allocation_id, trial_id, metrics,
            include_samples,
        ))
        return {"task_id": task_id, "owner": owner, "series": []}

    def cancel(self, task_id, owner):
        self.calls.append(("cancel", task_id, owner))
        return {"task_id": task_id, "state": "cancelling"}

    def reconcile(self, task_id, owner, remote_id):
        self.calls.append(("reconcile", task_id, owner, remote_id))
        return {"task_id": task_id, "remote_id": remote_id, "owner": owner}

    def list_tasks(self, owner):
        self.calls.append(("list", owner))
        return [{"task_id": "task-1", "owner": owner}]

    def discover(self, kind, owner, limit=50, offset=0):
        self.calls.append(("discover", kind, owner, limit, offset))
        return {
            "kind": kind,
            "owner": owner,
            "limit": limit,
            "offset": offset,
            "tasks": [{"remote_id": "remote-1"}],
        }

    def adopt(self, kind, remote_id, owner):
        self.calls.append(("adopt", kind, remote_id, owner))
        return {
            "task_id": "adopted-1",
            "kind": kind,
            "remote_id": remote_id,
            "owner": owner,
        }


def _structured(result):
    return result.structured_content


def test_real_sdk_client_lists_tools_and_invokes_bound_owner():
    async def exercise():
        service = FakeService()
        server = create_server(service, "alice")
        async with Client(server) as client:
            listed = await client.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            assert set(tools) == {
                "compute_plan",
                "compute_launch",
                "compute_status",
                "compute_logs",
                "compute_usage",
                "compute_cancel",
                "compute_reconcile",
                "compute_list_tasks",
                "compute_discover",
                "compute_adopt",
            }
            for tool in tools.values():
                assert "owner" not in tool.input_schema.get("properties", {})
            assert set(tools["compute_discover"].input_schema["required"]) == {"kind"}
            assert tools["compute_discover"].input_schema["properties"]["limit"]["default"] == 50
            assert tools["compute_discover"].input_schema["properties"]["offset"]["default"] == 0
            assert set(tools["compute_adopt"].input_schema["required"]) == {
                "kind",
                "remote_id",
            }
            assert tools["compute_discover"].annotations.read_only_hint is True
            assert tools["compute_discover"].annotations.open_world_hint is True
            assert tools["compute_adopt"].annotations.read_only_hint is False
            assert tools["compute_adopt"].annotations.destructive_hint is False
            assert tools["compute_adopt"].annotations.idempotent_hint is True
            assert tools["compute_adopt"].annotations.open_world_hint is True
            usage_schema = tools["compute_usage"].input_schema
            assert set(usage_schema["required"]) == {"task_id"}
            assert usage_schema["properties"]["window_seconds"]["default"] == 3600
            assert usage_schema["properties"]["include_samples"]["default"] is False
            assert tools["compute_usage"].annotations.read_only_hint is True
            assert tools["compute_usage"].annotations.destructive_hint is False
            assert tools["compute_usage"].annotations.open_world_hint is True
            assert "compute_resources" in tools["compute_usage"].description

            launched = await client.call_tool(
                "compute_launch",
                {"request": {"command": "true"}, "request_id": "req-1"},
            )
            assert _structured(launched)["owner"] == "alice"

            logs = await client.call_tool("compute_logs", {"task_id": "task-1", "tail": 5})
            assert _structured(logs) == {"result": [{"message": "hello"}]}

            usage = await client.call_tool(
                "compute_usage",
                {
                    "task_id": "task-1",
                    "window_seconds": 900,
                    "allocation_id": "a.1",
                    "trial_id": 4,
                    "metrics": ["cpu_cores", "gpu_power_watts"],
                    "include_samples": True,
                },
            )
            assert _structured(usage)["owner"] == "alice"

            discovered = await client.call_tool(
                "compute_discover",
                {"kind": "command", "limit": 7, "offset": 2},
            )
            assert _structured(discovered)["owner"] == "alice"

            adopted = await client.call_tool(
                "compute_adopt", {"kind": "command", "remote_id": "remote-1"}
            )
            assert _structured(adopted)["task_id"] == "adopted-1"
        assert ("launch", {"command": "true"}, "req-1", "alice") in service.calls
        assert ("discover", "command", "alice", 7, 2) in service.calls
        assert (
            "usage", "task-1", "alice", 900, "a.1", 4, ["cpu_cores", "gpu_power_watts"], True
        ) in service.calls
        assert ("adopt", "command", "remote-1", "alice") in service.calls

    asyncio.run(asyncio.wait_for(exercise(), timeout=10))


def test_tool_errors_are_structured():
    async def exercise():
        server = create_server(FakeService(), "alice")
        async with Client(server) as client:
            result = await client.call_tool(
                "compute_logs", {"task_id": "task-1", "tail": 0}
            )
            assert result.is_error is True
            assert result.structured_content is None
            encoded = result.content[0].text.split(": ", 1)[1]
            assert json.loads(encoded) == {
                "error": {"code": "internal_error", "message": "tail must be at least 1"}
            }

    asyncio.run(asyncio.wait_for(exercise(), timeout=10))


def test_slow_service_call_does_not_block_other_tools():
    class SlowService(FakeService):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def launch(self, request, request_id, owner):
            self.started.set()
            self.release.wait(timeout=2)
            return super().launch(request, request_id, owner)

    async def exercise():
        service = SlowService()
        server = create_server(service, "alice")
        launch = asyncio.create_task(
            server.call_tool(
                "compute_launch",
                {"request": {"command": "true"}, "request_id": "req-1"},
            )
        )
        for _ in range(100):
            if service.started.is_set():
                break
            await asyncio.sleep(0.01)
        assert service.started.is_set()
        try:
            listed = await asyncio.wait_for(
                server.call_tool("compute_list_tasks", {}), timeout=0.5
            )
            assert listed.is_error is False
        finally:
            service.release.set()
        await launch

    asyncio.run(asyncio.wait_for(exercise(), timeout=10))


def test_stdio_subprocess_initializes_and_calls_offline_plan(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "mounts:\n  - host_path: /shared\n    container_path: /shared\n"
        "defaults:\n  image: image\n  pool: pool\n",
        encoding="utf-8",
    )
    repo_root = Path(__file__).resolve().parents[1]
    source_root = str(repo_root / "src")
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "determined_compute.mcp_server",
            "--profile",
            str(profile),
            "--db",
            str(tmp_path / "tasks.db"),
            "--owner",
            "alice",
        ],
        env={"PYTHONPATH": source_root},
        cwd=str(tmp_path),
    )

    async def exercise():
        async with Client(params) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert len(tools) == 14
            assert "compute_plan" in tools
            assert "compute_discover" in tools
            assert "compute_adopt" in tools
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


def test_default_runtime_registers_all_tools(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "mounts:\n  - host_path: /shared\n    container_path: /shared\n"
        "defaults:\n  image: image\n  pool: pool\n",
        encoding="utf-8",
    )
    args = build_parser().parse_args(
        [
            "--profile",
            str(profile),
            "--db",
            str(tmp_path / "tasks.db"),
            "--owner",
            "alice",
        ]
    )
    server, owner = _runtime(args)

    async def exercise():
        async with Client(server) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert len(tools) == 14
            assert "compute_plan" in tools
            assert "compute_discover" in tools
            assert "compute_adopt" in tools
            assert "storage_check" in tools
            assert "compute_resources" in tools
            assert "compute_usage" in tools
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

    assert owner == "alice"
    asyncio.run(asyncio.wait_for(exercise(), timeout=10))
