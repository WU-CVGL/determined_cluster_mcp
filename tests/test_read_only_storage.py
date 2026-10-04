from pathlib import Path

import pytest

from determined_compute.compute import ComputeProfile, ComputeService, ValidationError
from determined_compute.storage import StorageAccessConfig, StorageError, StorageService


@pytest.fixture
def setup(tmp_path):
    shared = tmp_path / 'shared'; shared.mkdir()
    data = shared / 'data'; data.mkdir()
    (data / 'reference.txt').write_text('reference')
    profile = ComputeProfile.from_dict({
        'mounts': [
            {'host_path': str(shared), 'container_path': '/work'},
            {'host_path': str(data), 'container_path': '/data', 'read_only': True},
        ],
        'defaults': {'image': 'example', 'pool': 'example', 'slots': 0},
    })
    return profile, shared, data


@pytest.mark.parametrize('mode', ['local', 'ssh'])
@pytest.mark.parametrize('dry_run', [True, False])
def test_readonly_upload_rejected_before_transport(setup, tmp_path, monkeypatch, mode, dry_run):
    profile, _, data = setup
    source = tmp_path / 'source'; source.mkdir()
    config = StorageAccessConfig.from_dict({'mode': mode, 'ssh': {'host': 'example-login'}})
    storage = StorageService(profile, config)
    monkeypatch.setattr(storage, '_run', lambda *a, **kw: pytest.fail('transport called'))
    with pytest.raises(StorageError) as error:
        storage.sync(str(source), '/data/new', dry_run)
    assert error.value.code == 'read_only_storage'
    assert not (data / 'new').exists()


def test_readonly_check_is_policy_aware_and_fetch_allowed(setup, tmp_path):
    profile, _, data = setup
    storage = StorageService(profile, StorageAccessConfig())
    result = storage.check('/data')
    assert result['read_only'] is True
    assert result['writable'] is False
    assert result['readable'] is True
    destination = tmp_path / 'download'
    storage.fetch('/data', str(destination), dry_run=False)
    assert (destination / 'reference.txt').read_text() == 'reference'


def test_shared_aliases_do_not_bypass_readonly_policy(setup, tmp_path):
    profile, shared, data = setup
    storage = StorageService(profile, StorageAccessConfig())
    source = tmp_path / 'source'; source.mkdir()
    assert storage.check('/work/data')['read_only'] is True
    with pytest.raises(StorageError, match='read-only'):
        storage.sync(str(source), '/work/data/output', False)
    with pytest.raises(StorageError, match='read-only'):
        storage.fetch('/data', str(data / 'download'), False)
    assert not (data / 'output').exists()
    assert not (data / 'download').exists()


@pytest.mark.parametrize('storage_path', [None, 'data/checkpoints'])
def test_checkpoint_cannot_write_readonly_host_path(setup, storage_path):
    profile, shared, data = setup
    service = ComputeService(None, profile)
    checkpoint = {'type': 'shared_fs', 'host_path': str(data if storage_path is None else shared)}
    if storage_path is not None:
        checkpoint['storage_path'] = storage_path
    with pytest.raises(ValidationError, match='read-only'):
        service.plan({'kind': 'experiment', 'name': 'readonly-check', 'command': ['true'],
                      'workdir': '/work/code', 'output_dir': '/work/output',
                      'experiment_config': {'checkpoint_storage': checkpoint}})


def test_explicit_false_read_only_is_the_default_mount(setup):
    profile, shared, _ = setup
    base = {'mounts': [{'host_path': str(shared), 'container_path': '/work'}],
            'defaults': {'image': 'example', 'pool': 'example', 'slots': 0}}
    original = ComputeProfile.from_dict(base)
    base['mounts'][0]['read_only'] = False
    explicit = ComputeProfile.from_dict(base)
    assert original == explicit
    assert explicit.mounts[0].as_config() == {'host_path': str(shared), 'container_path': '/work'}


@pytest.mark.parametrize('storage_path', ['checkpoints', 'ABSOLUTE_CHILD'])
def test_writable_checkpoint_storage_path_is_preserved(setup, storage_path):
    profile, shared, _ = setup
    if storage_path == 'ABSOLUTE_CHILD':
        storage_path = str(shared / 'checkpoints')
    config = {'type': 'shared_fs', 'host_path': str(shared), 'storage_path': storage_path}
    plan = ComputeService(None, profile).plan({
        'kind': 'experiment', 'name': 'checkpoint-check', 'command': ['true'],
        'workdir': '/work/code', 'output_dir': '/work/output',
        'experiment_config': {'checkpoint_storage': config},
    })
    assert plan['config']['checkpoint_storage'] == config


@pytest.mark.parametrize('checkpoint', ['s3://bucket', {'type': 's3', 'bucket': 'example'}])
def test_explicit_checkpoint_shortcuts_cannot_bypass_shared_storage(setup, checkpoint):
    profile, _, _ = setup
    with pytest.raises(ValidationError, match='shared_fs'):
        ComputeService(None, profile).plan({
            'kind': 'experiment', 'command': ['true'], 'workdir': '/work/code',
            'output_dir': '/work/output', 'experiment_config': {'checkpoint_storage': checkpoint},
        })


def test_absolute_checkpoint_must_stay_inside_declared_host_root(setup):
    profile, shared, _ = setup
    with pytest.raises(ValidationError, match='inside host_path'):
        ComputeService(None, profile).plan({
            'kind': 'experiment', 'command': ['true'], 'workdir': '/work/code',
            'output_dir': '/work/output', 'experiment_config': {'checkpoint_storage': {
                'type': 'shared_fs', 'host_path': str(shared / 'one'),
                'storage_path': str(shared / 'other'),
            }},
        })


@pytest.mark.parametrize('existing', [True, False])
def test_fetch_cannot_write_through_readonly_subdirectory_mapping(tmp_path, existing):
    mapped = tmp_path / 'mounted-subdir'; mapped.mkdir()
    (mapped / 'reference.txt').write_text('reference')
    target = mapped / 'download'
    if existing:
        target.mkdir()
    profile = ComputeProfile.from_dict({
        'mounts': [{'host_path': '/cluster', 'container_path': '/data', 'read_only': True}],
        'defaults': {'image': 'example', 'pool': 'example'},
    })
    access = StorageAccessConfig.from_dict({'local_mounts': [
        {'host_path': '/cluster/shared', 'local_path': str(mapped)},
    ]})
    storage = StorageService(profile, access)
    with pytest.raises(StorageError) as error:
        storage.fetch('/data/shared', str(target), dry_run=False)
    assert error.value.code == 'read_only_storage'
    assert target.exists() is existing
    if existing:
        assert not list(target.iterdir())


@pytest.mark.parametrize('alias', ['checkpoint_path', 'tensorboard_path'])
@pytest.mark.parametrize('value', ['data/checkpoints', '/outside/checkpoints'])
def test_legacy_checkpoint_path_aliases_cannot_bypass_policy(setup, alias, value):
    profile, shared, _ = setup
    with pytest.raises(ValidationError, match='storage_path instead of legacy'):
        ComputeService(None, profile).plan({
            'kind': 'experiment', 'command': ['true'], 'workdir': '/work/code',
            'output_dir': '/work/output', 'experiment_config': {'checkpoint_storage': {
                'type': 'shared_fs', 'host_path': str(shared), alias: value,
            }},
        })
