"""Exercise the real planning, durable state and HTTP adapter together, offline."""
from __future__ import annotations

import json

import pytest
import requests

from determined_compute.compute import ComputeProfile, ComputeService, SQLiteTaskStore
from determined_compute.core.api_client import DeterminedAPIClient


def response(payload, status=200):
    value = requests.Response()
    value.status_code = status
    value._content = json.dumps(payload).encode()
    value.headers['Content-Type'] = 'application/json'
    return value


def profile():
    return ComputeProfile.from_dict({
        'mounts': [{'host_path': '/workspace/alice', 'container_path': '/work'}],
        'defaults': {'image': 'verified-image:tag', 'pool': 'verified-pool', 'slots': 1},
    })


def request():
    return {'command': ['python', 'train.py', '--steps', '1'],
            'workdir': '/work/revisions/abc', 'output_dir': '/work/runs/example',
            'code_revision': 'abc'}


@pytest.fixture(autouse=True)
def mock_cluster_capacity(monkeypatch):
    def get(url, **kwargs):
        if url.endswith('/api/v1/resource-pools'):
            return response({'resourcePools': [{
                'name': 'verified-pool', 'numAgents': 1, 'slotsAvailable': 1,
                'slotsUsed': 0, 'slotType': 'TYPE_CUDA',
                'auxContainerCapacity': 8, 'auxContainersRunning': 0,
            }]})
        if url.endswith('/api/v1/agents'):
            return response({'agents': [{
                'id': 'agent-1', 'resourcePools': ['verified-pool'],
                'enabled': True, 'draining': False,
                'slots': {'0': {'id': '0', 'enabled': True, 'draining': False}},
            }]})
        raise AssertionError(f'unexpected API read: {url}')
    monkeypatch.setattr(requests, 'get', get)


def test_command_round_trip_keeps_shared_paths_and_identity_after_restart(tmp_path, monkeypatch):
    sent = []
    def post(url, **kwargs):
        sent.append((url, kwargs['json']))
        return response({'command': {'id': 'remote-command-1', 'state': 'STATE_RUNNING'}})
    monkeypatch.setattr(requests, 'post', post)
    client = DeterminedAPIClient(api_url='https://cluster.example:443', api_token='test-token')
    db = tmp_path / 'tasks.sqlite3'
    store = SQLiteTaskStore(db)
    service = ComputeService(client, store, profile())
    record = service.launch(request(), 'request-1', 'session-a')
    assert record['remote_id'] == 'remote-command-1'
    assert len(sent) == 1
    assert sent[0][0].endswith('/api/v1/commands')
    payload = sent[0][1]
    assert not {'files', 'context', 'modelDefinition', 'project_root'} & set(payload)
    assert payload['config']['bind_mounts'] == [
        {'host_path': '/workspace/alice', 'container_path': '/work'}]
    assert payload['config']['resources']['slots'] == 1
    assert 'slots_per_trial' not in payload['config']['resources']
    assert '/work/revisions/abc' in str(payload['config']['entrypoint'])
    store.close()
    new_store = SQLiteTaskStore(db)
    resumed = ComputeService(client, new_store, profile())
    assert resumed.launch(request(), 'request-1', 'session-a')['task_id'] == record['task_id']
    assert len(sent) == 1
    assert resumed.list_tasks('other-session') == []
    monkeypatch.setattr(requests, 'get', lambda *a, **kw: response({
        'command': {'id': 'remote-command-1', 'state': 'STATE_TERMINATED', 'exitStatus': 'exit code 2'}}))
    status = resumed.status(record['task_id'], 'session-a')
    assert status['remote']['exitStatus'] == 'exit code 2'
    assert status.get('success') is not True
    new_store.close()


def test_definite_rejection_fails_but_ambiguous_timeout_is_never_reposted(tmp_path, monkeypatch):
    store = SQLiteTaskStore(tmp_path / 'tasks.sqlite3')
    client = DeterminedAPIClient(api_url='https://cluster.example:443', api_token='test-token')
    service = ComputeService(client, store, profile())

    # A definite HTTP rejection is a plain failure, not an uncertain submission.
    monkeypatch.setattr(requests, 'post', lambda *a, **kw: response({'message': 'denied'}, 403))
    with pytest.raises(Exception):
        service.launch(request(), 'auth-test', 'session-a')
    rejected = service.list_tasks('session-a')[0]
    assert rejected['state'] == 'failed'
    assert rejected['error_code'] != 'submission_uncertain'

    # A timeout may have been accepted remotely: persist it as uncertain and never POST again.
    calls = []
    def uncertain(url, **kw):
        calls.append(url)
        raise requests.ReadTimeout('simulated ambiguous acceptance')
    monkeypatch.setattr(requests, 'post', uncertain)
    with pytest.raises(Exception):
        service.launch(request(), 'uncertain-test', 'session-a')
    [record] = [t for t in service.list_tasks('session-a') if t['request_id'] == 'uncertain-test']
    assert record['state'] == 'submission_uncertain'
    repeated = service.launch(request(), 'uncertain-test', 'session-a')
    assert repeated['task_id'] == record['task_id']
    assert len(calls) == 1
    store.close()
