from pathlib import Path

import pytest

from determined_compute.compute import ComputeProfile, ComputeService, SQLiteTaskStore, ValidationError


@pytest.fixture
def service():
    profile = ComputeProfile.from_file(Path(__file__).resolve().parents[1] / 'cfg/compute-profile.example.yaml')
    store = SQLiteTaskStore(':memory:')
    yield ComputeService(None, store, profile)
    store.close()


def test_shared_root_accepts_command_paths_without_local_or_cluster_access(service):
    result = service.plan({'command': ['true'], 'workdir': '/SSD/project',
                           'output_dir': '/SSD/runs/task', 'slots': 0})
    assert result['kind'] == 'command'
    assert {'host_path': '/SSD', 'container_path': '/SSD'} in result['config']['bind_mounts']
    assert '/SSD/project' in result['config']['entrypoint'][-1]


def test_cross_root_outputs_and_experiment_checkpoints(service):
    result = service.plan({'kind': 'experiment', 'command': ['python', 'train.py'],
                           'workdir': '/SSD_home/project', 'output_dir': '/UNSAFE_SSD4/results',
                           'experiment_config': {'name': 'example', 'checkpoint_storage': {
                               'type': 'shared_fs', 'host_path': '/SSD3/checkpoints'}}})
    assert result['config']['checkpoint_storage']['host_path'] == '/SSD3/checkpoints'
    assert '/SSD_home/project' in result['config']['entrypoint']
    assert '/UNSAFE_SSD4/results' in result['config']['entrypoint']


@pytest.mark.parametrize('root', ['/SSD_unconfigured', '/UNSAFE_SSD40'])
def test_similar_prefix_does_not_grant_a_mount(service, root):
    with pytest.raises(ValidationError):
        service.plan({'command': ['true'], 'workdir': root + '/project',
                      'output_dir': '/SSD/results'})
