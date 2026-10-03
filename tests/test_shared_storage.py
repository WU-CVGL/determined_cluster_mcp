from pathlib import Path

import pytest

from determined_compute.compute import ComputeProfile, ComputeService, ValidationError

ROOTS = ['/SSD', '/SSD_home', '/SSD_datasets', '/SSD3', '/SSD3_home', '/SSD3_datasets', '/UNSAFE_SSD4']

@pytest.fixture
def service():
    profile = ComputeProfile.from_file(Path(__file__).resolve().parents[1] / 'cfg/compute-profile.example.yaml')
    return ComputeService(None, profile)

@pytest.mark.parametrize('root', ROOTS)
def test_shared_root_accepts_command_paths_without_local_or_cluster_access(service, root):
    result = service.plan({'command': ['true'], 'workdir': root + '/project',
                           'output_dir': root + '/runs/task', 'slots': 0})
    assert result['kind'] == 'command'
    assert {'host_path': root, 'container_path': root} in result['config']['bind_mounts']
    assert root + '/project' in result['config']['entrypoint'][-1]


def test_cross_root_outputs_and_experiment_checkpoints(service):
    result = service.plan({'kind': 'experiment', 'command': ['python', 'train.py'],
                           'workdir': '/SSD_home/project', 'output_dir': '/UNSAFE_SSD4/results',
                           'experiment_config': {'name': 'example', 'checkpoint_storage': {
                               'type': 'shared_fs', 'host_path': '/SSD3/checkpoints'}}})
    assert result['config']['checkpoint_storage']['host_path'] == '/SSD3/checkpoints'
    assert '/SSD_home/project' in result['config']['entrypoint']
    assert '/UNSAFE_SSD4/results' in result['config']['entrypoint']

@pytest.mark.parametrize('root', ['/SSD_unconfigured', '/SSD3_unconfigured', '/UNSAFE_SSD40'])
def test_similar_prefix_does_not_grant_a_mount(service, root):
    with pytest.raises(ValidationError):
        service.plan({'command': ['true'], 'workdir': root + '/project',
                      'output_dir': '/SSD/results'})
