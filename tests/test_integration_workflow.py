"""Exercise the real planning, task control and HTTP adapter together, offline."""
from __future__ import annotations

import json

import pytest
import requests

from determined_compute.compute import (
    APIError, ComputeProfile, ComputeService, SubmissionUncertainError,
)
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


COMMAND_ID = '6f1e2d3c-4b5a-4968-8776-655443322110'
ME = {'user': {'id': 7, 'username': 'alice'}}


def routes(monkeypatch, extra):
    capacity = requests.get

    def get(url, **kwargs):
        path = url.split('cluster.example:443/', 1)[1]
        if path in extra:
            return response(extra[path])
        return capacity(url, **kwargs)
    monkeypatch.setattr(requests, 'get', get)


def test_command_round_trip_returns_the_native_id_and_reads_it_back(monkeypatch):
    sent = []
    def post(url, **kwargs):
        sent.append((url, kwargs['json']))
        return response({'command': {'id': COMMAND_ID, 'state': 'STATE_RUNNING'}})
    monkeypatch.setattr(requests, 'post', post)
    client = DeterminedAPIClient(api_url='https://cluster.example:443', api_token='test-token')
    service = ComputeService(client, profile())
    launched = service.launch(request())
    assert launched['kind'] == 'command'
    assert launched['id'] == COMMAND_ID
    assert len(sent) == 1
    assert sent[0][0].endswith('/api/v1/commands')
    payload = sent[0][1]
    assert not {'files', 'context', 'modelDefinition', 'project_root'} & set(payload)
    assert payload['config']['bind_mounts'] == [
        {'host_path': '/workspace/alice', 'container_path': '/work'}]
    assert payload['config']['resources']['slots'] == 1
    assert 'slots_per_trial' not in payload['config']['resources']
    assert '/work/revisions/abc' in str(payload['config']['entrypoint'])
    # A new process needs nothing but the kind and the ID.
    routes(monkeypatch, {
        'api/v1/me': ME,
        f'api/v1/commands/{COMMAND_ID}': {'command': {
            'id': COMMAND_ID, 'userId': 7, 'state': 'STATE_TERMINATED',
            'exitStatus': 'exit code 2'}},
    })
    restarted = ComputeService(
        DeterminedAPIClient(api_url='https://cluster.example:443', api_token='test-token'),
        profile(),
    )
    status = restarted.status('command', launched['id'])
    assert status['remote']['exitStatus'] == 'exit code 2'
    assert status['state'] == 'STATE_TERMINATED'
    assert len(sent) == 1


def test_http_auth_failure_does_not_become_uncertain_submission(monkeypatch):
    calls = []
    def denied(*args, **kwargs):
        calls.append(args)
        return response({'message': 'denied'}, 403)
    monkeypatch.setattr(requests, 'post', denied)
    client = DeterminedAPIClient(api_url='https://cluster.example:443', api_token='test-token')
    service = ComputeService(client, profile())
    with pytest.raises(APIError) as caught:
        service.launch(request())
    assert not isinstance(caught.value, SubmissionUncertainError)
    assert caught.value.code == 403
    assert len(calls) == 1


def test_timeout_is_unconfirmed_never_reposted_and_found_by_marker(monkeypatch):
    calls = []
    def uncertain(url, **kwargs):
        calls.append(kwargs['json'])
        raise requests.ReadTimeout('simulated ambiguous acceptance')
    monkeypatch.setattr(requests, 'post', uncertain)
    client = DeterminedAPIClient(api_url='https://cluster.example:443', api_token='test-token')
    service = ComputeService(client, profile())
    with pytest.raises(SubmissionUncertainError) as caught:
        service.launch(request())
    assert len(calls) == 1
    marker = caught.value.details['submission_marker']
    variables = calls[0]['config']['environment']['environment_variables']
    assert f'COMPUTE_SUBMISSION_MARKER={marker}' in variables

    # The master did accept it; the marker in its stored config identifies it.
    listed = {'id': COMMAND_ID, 'userId': 7, 'username': 'alice',
              'description': 'command: abc', 'state': 'STATE_RUNNING'}
    routes(monkeypatch, {
        'api/v1/me': ME,
        'api/v1/commands': {
            'commands': [listed],
            'pagination': {'limit': 5, 'offset': 0, 'startIndex': 0, 'endIndex': 1,
                           'total': 1},
        },
        f'api/v1/commands/{COMMAND_ID}': {
            'command': listed,
            'config': {'environment': {'environment_variables': {
                'cpu': variables, 'cuda': variables, 'rocm': variables}}},
        },
    })
    found = service.list_tasks('command', limit=5, marker=marker)
    assert [(task['id'], task['submission_marker']) for task in found['tasks']] == [
        (COMMAND_ID, marker)]
    assert len(calls) == 1
