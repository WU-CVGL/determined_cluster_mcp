import asyncio

import pytest

from determined_compute.compute import ComputeProfile, ComputeService, SQLiteTaskStore
from determined_compute.storage import StorageAccessConfig, StorageService


def make_profile(tmp_path):
    shared = tmp_path / 'shared'
    shared.mkdir()
    return ComputeProfile.from_dict({
        'mounts': [{'host_path': str(shared), 'container_path': '/shared'}],
        'defaults': {'image': 'example', 'pool': 'example'},
    }), shared


def test_real_mcp_storage_preview_and_copy_round_trip(tmp_path):
    pytest.importorskip('mcp')
    from mcp import Client
    from determined_compute.mcp_server import create_server
    profile, shared = make_profile(tmp_path)
    source = tmp_path / 'source'; source.mkdir()
    (source / 'code.py').write_text('print(1)\n')
    (source / '.env.production').write_text('fixture-secret')
    fetched = tmp_path / 'fetched'
    store = SQLiteTaskStore(':memory:')
    service = ComputeService(None, store, profile)
    storage = StorageService(profile, StorageAccessConfig())
    async def exercise():
        async with Client(create_server(service, 'fixture', storage_service=storage)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert tools['storage_check'].annotations.read_only_hint is True
            assert tools['storage_sync'].annotations.destructive_hint is True
            preview = await client.call_tool('storage_sync', {'local_dir': str(source), 'shared_dir': '/shared/job/code'})
            assert not preview.is_error
            assert preview.structured_content['dry_run'] is True
            assert not (shared / 'job').exists()
            transferred = await client.call_tool('storage_sync', {'local_dir': str(source), 'shared_dir': '/shared/job/code', 'dry_run': False})
            assert not transferred.is_error, transferred
            assert (shared / 'job/code/code.py').read_text() == 'print(1)\n'
            assert not (shared / 'job/code/.env.production').exists()
            downloaded = await client.call_tool('storage_fetch', {'shared_dir': '/shared/job/code', 'local_dir': str(fetched), 'dry_run': False})
            assert not downloaded.is_error, downloaded
            assert (fetched / 'code.py').read_text() == 'print(1)\n'
    try:
        asyncio.run(asyncio.wait_for(exercise(), 20))
    finally:
        store.close()


def test_real_mcp_lazy_client_blocks_busy_pool_before_submission(tmp_path):
    pytest.importorskip('mcp')
    from mcp import Client
    from determined_compute.mcp_server import _LazyClient
    from determined_compute.compute.admission import ResourceInspector
    from determined_compute.mcp_server import create_server
    profile, _ = make_profile(tmp_path)
    class API:
        api_url = 'https://cluster.example'
        def _get(self, endpoint, params=None):
            if endpoint == 'api/v1/resource-pools':
                return {'resourcePools': [{'name': 'example', 'numAgents': 0,
                    'slotsAvailable': 0, 'slotsUsed': 0, 'slotType': 'TYPE_CUDA',
                    'auxContainerCapacity': 0, 'auxContainersRunning': 0}]}
            assert endpoint == 'api/v1/agents'
            return {'agents': []}
        def launch_task(self, *args):
            pytest.fail('busy pool must not receive a launch request')
    store = SQLiteTaskStore(':memory:')
    lazy = _LazyClient(API)
    service = ComputeService(lazy, store, profile)
    async def exercise():
        async with Client(create_server(service, 'fixture', resource_inspector=ResourceInspector(lazy))) as c:
            capacity = await c.call_tool('compute_resources', {'pool': 'example', 'slots': 1})
            assert not capacity.is_error
            assert capacity.structured_content['available'] is False
            denied = await c.call_tool('compute_launch', {'request_id': 'busy', 'request': {
                'name': 'busy-check', 'command': ['true'], 'workdir': '/shared/project',
                'output_dir': '/shared/output', 'slots': 1,
            }})
            assert denied.is_error
            assert 'capacity_unavailable' in denied.content[0].text
            assert service.list_tasks('fixture') == []
    try:
        asyncio.run(asyncio.wait_for(exercise(), 10))
    finally:
        store.close()
