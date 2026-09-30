import asyncio

import pytest

from determined_compute.mcp_server import Tools, create_server
from determined_compute.policy import Policy
from determined_compute.storage import StorageAccessConfig, StorageService


def test_real_mcp_storage_preview_and_copy_round_trip(tmp_path):
    pytest.importorskip("mcp")
    from mcp import Client

    shared = tmp_path / "shared"
    shared.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    (source / "code.py").write_text("print(1)\n")
    (source / ".env.production").write_text("fixture-secret")
    fetched = tmp_path / "fetched"
    policy = Policy.from_dict(
        {
            "mounts": [{"host_path": str(shared), "container_path": "/shared"}],
            "defaults": {"image": "example", "pool": "example"},
        }
    )
    tools = Tools(object(), policy, StorageService(policy, StorageAccessConfig()))

    async def exercise():
        async with Client(create_server(tools)) as client:
            listed = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert listed["storage_check"].annotations.read_only_hint is True
            assert listed["storage_sync"].annotations.destructive_hint is True
            arguments = {"local_dir": str(source), "shared_dir": "/shared/job/code"}
            preview = await client.call_tool("storage_sync", arguments)
            assert not preview.is_error
            assert preview.structured_content["dry_run"] is True
            assert not (shared / "job").exists()
            transferred = await client.call_tool("storage_sync", {**arguments, "dry_run": False})
            assert not transferred.is_error, transferred
            assert (shared / "job/code/code.py").read_text() == "print(1)\n"
            assert not (shared / "job/code/.env.production").exists()
            checked = await client.call_tool("storage_check", {"path": "/shared/job/code"})
            assert checked.structured_content["viewpoint"]["backend"] == "local"
            downloaded = await client.call_tool(
                "storage_fetch",
                {"shared_dir": "/shared/job/code", "local_dir": str(fetched), "dry_run": False},
            )
            assert not downloaded.is_error, downloaded
            assert (fetched / "code.py").read_text() == "print(1)\n"

    asyncio.run(asyncio.wait_for(exercise(), 20))
