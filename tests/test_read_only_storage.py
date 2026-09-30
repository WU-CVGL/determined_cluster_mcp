from pathlib import Path

import pytest

from determined_compute.compute import ComputeProfile, ComputeService, SQLiteTaskStore, ValidationError
from determined_compute.policy import Policy
from determined_compute.storage import StorageAccessConfig, StorageError, StorageService


def policy_of(profile):
    mounts = [
        {'host_path': m.host_path, 'container_path': m.container_path, 'read_only': m.read_only}
        for m in profile.mounts
    ]
    return Policy.from_dict({'mounts': mounts, 'defaults': {'image': 'example', 'pool': 'example'}})


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
    storage = StorageService(policy_of(profile), config)
    monkeypatch.setattr(storage, '_run', lambda *a, **kw: pytest.fail('transport called'))
    with pytest.raises(StorageError) as error:
        storage.sync(str(source), '/data/new', dry_run)
    assert error.value.code == 'read_only_storage'
    assert not (data / 'new').exists()


def test_readonly_check_is_policy_aware_and_fetch_allowed(setup, tmp_path):
    profile, _, data = setup
    storage = StorageService(policy_of(profile), StorageAccessConfig())
    result = storage.check('/data')
    assert result['read_only'] is True
    assert result['writable'] is False
    assert result['readable'] is True
    destination = tmp_path / 'download'
    storage.fetch('/data', str(destination), dry_run=False)
    assert (destination / 'reference.txt').read_text() == 'reference'


def test_shared_aliases_do_not_bypass_readonly_policy(setup, tmp_path):
    profile, shared, data = setup
    storage = StorageService(policy_of(profile), StorageAccessConfig())
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
    store = SQLiteTaskStore(':memory:')
    service = ComputeService(None, store, profile)
    checkpoint = {'type': 'shared_fs', 'host_path': str(data if storage_path is None else shared)}
    if storage_path is not None:
        checkpoint['storage_path'] = storage_path
    try:
        with pytest.raises(ValidationError, match='read-only'):
            service.plan({'kind': 'experiment', 'name': 'readonly-check', 'command': ['true'],
                          'workdir': '/work/code', 'output_dir': '/work/output',
                          'experiment_config': {'checkpoint_storage': checkpoint}})
    finally:
        store.close()


def test_explicit_false_retains_legacy_mount_fingerprint(setup):
    profile, shared, _ = setup
    base = {'mounts': [{'host_path': str(shared), 'container_path': '/work'}],
            'defaults': {'image': 'example', 'pool': 'example', 'slots': 0}}
    original = ComputeProfile.from_dict(base)
    base['mounts'][0]['read_only'] = False
    assert original.fingerprint == ComputeProfile.from_dict(base).fingerprint


@pytest.mark.parametrize('storage_path', ['checkpoints', 'ABSOLUTE_CHILD'])
def test_writable_checkpoint_storage_path_is_preserved(setup, storage_path):
    profile, shared, _ = setup
    if storage_path == 'ABSOLUTE_CHILD':
        storage_path = str(shared / 'checkpoints')
    store = SQLiteTaskStore(':memory:')
    try:
        config = {'type': 'shared_fs', 'host_path': str(shared), 'storage_path': storage_path}
        plan = ComputeService(None, store, profile).plan({
            'kind': 'experiment', 'name': 'checkpoint-check', 'command': ['true'],
            'workdir': '/work/code', 'output_dir': '/work/output',
            'experiment_config': {'checkpoint_storage': config},
        })
        assert plan['config']['checkpoint_storage'] == config
    finally:
        store.close()


@pytest.mark.parametrize('checkpoint', ['s3://bucket', {'type': 's3', 'bucket': 'example'}])
def test_explicit_checkpoint_shortcuts_cannot_bypass_shared_storage(setup, checkpoint):
    profile, _, _ = setup
    store = SQLiteTaskStore(':memory:')
    try:
        with pytest.raises(ValidationError, match='shared_fs'):
            ComputeService(None, store, profile).plan({
                'kind': 'experiment', 'command': ['true'], 'workdir': '/work/code',
                'output_dir': '/work/output', 'experiment_config': {'checkpoint_storage': checkpoint},
            })
    finally:
        store.close()


def test_absolute_checkpoint_must_stay_inside_declared_host_root(setup):
    profile, shared, _ = setup
    store = SQLiteTaskStore(':memory:')
    try:
        with pytest.raises(ValidationError, match='inside host_path'):
            ComputeService(None, store, profile).plan({
                'kind': 'experiment', 'command': ['true'], 'workdir': '/work/code',
                'output_dir': '/work/output', 'experiment_config': {'checkpoint_storage': {
                    'type': 'shared_fs', 'host_path': str(shared / 'one'),
                    'storage_path': str(shared / 'other'),
                }},
            })
    finally:
        store.close()


@pytest.mark.parametrize('existing', [True, False])
def test_fetch_cannot_write_through_readonly_subdirectory_mapping(tmp_path, existing):
    mapped = tmp_path / 'mounted-subdir'; mapped.mkdir()
    (mapped / 'reference.txt').write_text('reference')
    target = mapped / 'download'
    if existing:
        target.mkdir()
    policy = Policy.from_dict({
        'mounts': [{'host_path': '/cluster', 'container_path': '/data', 'read_only': True}],
        'defaults': {'image': 'example', 'pool': 'example'},
    })
    access = StorageAccessConfig.from_dict({'local_mounts': [
        {'host_path': '/cluster/shared', 'local_path': str(mapped)},
    ]})
    storage = StorageService(policy, access)
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
    store = SQLiteTaskStore(':memory:')
    try:
        with pytest.raises(ValidationError, match='storage_path instead of legacy'):
            ComputeService(None, store, profile).plan({
                'kind': 'experiment', 'command': ['true'], 'workdir': '/work/code',
                'output_dir': '/work/output', 'experiment_config': {'checkpoint_storage': {
                    'type': 'shared_fs', 'host_path': str(shared), alias: value,
                }},
            })
    finally:
        store.close()
