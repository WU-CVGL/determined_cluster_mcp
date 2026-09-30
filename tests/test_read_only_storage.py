import pytest

from determined_compute.compute import ComputeProfile, ComputeService, SQLiteTaskStore, ValidationError
from determined_compute.storage import StorageAccessConfig, StorageError, StorageService

DEFAULTS = {'image': 'example', 'pool': 'example', 'slots': 0}
# Checkpoint validation is pure planning, so these host paths are never touched.
CHECKPOINT_PROFILE = {
    'mounts': [
        {'host_path': '/shared', 'container_path': '/work'},
        {'host_path': '/shared/data', 'container_path': '/data', 'read_only': True},
    ],
    'defaults': DEFAULTS,
}


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
        'defaults': DEFAULTS,
    })
    return profile, shared, data


def plan_checkpoint(checkpoint):
    store = SQLiteTaskStore(':memory:')
    try:
        return ComputeService(None, store, ComputeProfile.from_dict(CHECKPOINT_PROFILE)).plan({
            'kind': 'experiment', 'name': 'checkpoint-check', 'command': ['true'],
            'workdir': '/work/code', 'output_dir': '/work/output',
            'experiment_config': {'checkpoint_storage': checkpoint},
        })
    finally:
        store.close()


@pytest.mark.parametrize(('mode', 'dry_run'), [('local', False), ('ssh', True)])
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


def test_fetch_cannot_write_through_readonly_subdirectory_mapping(tmp_path):
    mapped = tmp_path / 'mounted-subdir'; mapped.mkdir()
    (mapped / 'reference.txt').write_text('reference')
    existing = mapped / 'existing'; existing.mkdir()
    missing = mapped / 'missing'
    profile = ComputeProfile.from_dict({
        'mounts': [{'host_path': '/cluster', 'container_path': '/data', 'read_only': True}],
        'defaults': {'image': 'example', 'pool': 'example'},
    })
    access = StorageAccessConfig.from_dict({'local_mounts': [
        {'host_path': '/cluster/shared', 'local_path': str(mapped)},
    ]})
    storage = StorageService(profile, access)
    for target in (existing, missing):
        with pytest.raises(StorageError) as error:
            storage.fetch('/data/shared', str(target), dry_run=False)
        assert error.value.code == 'read_only_storage'
    assert not list(existing.iterdir())
    assert not missing.exists()


def test_explicit_false_retains_legacy_mount_fingerprint():
    base = {'mounts': [{'host_path': '/shared', 'container_path': '/work'}], 'defaults': DEFAULTS}
    original = ComputeProfile.from_dict(base)
    base['mounts'][0]['read_only'] = False
    assert original.fingerprint == ComputeProfile.from_dict(base).fingerprint


@pytest.mark.parametrize(('checkpoint', 'reason'), [
    pytest.param({'type': 'shared_fs', 'host_path': '/shared/data'}, 'read-only', id='read-only-host-path'),
    pytest.param({'type': 'shared_fs', 'host_path': '/shared', 'storage_path': 'data/checkpoints'},
                 'read-only', id='storage-path-into-read-only'),
    pytest.param('s3://bucket', 'shared_fs', id='non-mapping-shortcut'),
    pytest.param({'type': 's3', 'bucket': 'example'}, 'shared_fs', id='non-shared-fs-type'),
    pytest.param({'type': 'shared_fs', 'host_path': '/shared/one', 'storage_path': '/shared/other'},
                 'inside host_path', id='absolute-storage-path-outside-host-path'),
    pytest.param({'type': 'shared_fs', 'host_path': '/shared', 'checkpoint_path': 'data/checkpoints'},
                 'storage_path instead of legacy', id='legacy-checkpoint-path'),
    pytest.param({'type': 'shared_fs', 'host_path': '/shared', 'tensorboard_path': '/outside/checkpoints'},
                 'storage_path instead of legacy', id='legacy-tensorboard-path'),
])
def test_checkpoint_storage_cannot_bypass_mount_policy(checkpoint, reason):
    with pytest.raises(ValidationError, match=reason):
        plan_checkpoint(checkpoint)


@pytest.mark.parametrize('storage_path', ['checkpoints', '/shared/checkpoints'])
def test_writable_checkpoint_storage_path_is_preserved(storage_path):
    config = {'type': 'shared_fs', 'host_path': '/shared', 'storage_path': storage_path}
    assert plan_checkpoint(config)['config']['checkpoint_storage'] == config
