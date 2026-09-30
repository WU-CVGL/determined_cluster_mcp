import pytest

from determined_compute.policy import Policy
from determined_compute.storage import StorageAccessConfig, StorageError, StorageService


@pytest.fixture
def setup(tmp_path):
    shared = tmp_path / 'shared'; shared.mkdir()
    data = shared / 'data'; data.mkdir()
    (data / 'reference.txt').write_text('reference')
    policy = Policy.from_dict({
        'mounts': [
            {'host_path': str(shared), 'container_path': '/work'},
            {'host_path': str(data), 'container_path': '/data', 'read_only': True},
        ],
        'defaults': {'image': 'example', 'pool': 'example', 'slots': 0},
    })
    return policy, shared, data


@pytest.mark.parametrize('mode', ['local', 'ssh'])
@pytest.mark.parametrize('dry_run', [True, False])
def test_readonly_upload_rejected_before_transport(setup, tmp_path, monkeypatch, mode, dry_run):
    policy, _, data = setup
    source = tmp_path / 'source'; source.mkdir()
    config = StorageAccessConfig.from_dict({'mode': mode, 'ssh': {'host': 'example-login'}})
    storage = StorageService(policy, config)
    monkeypatch.setattr(storage, '_run', lambda *a, **kw: pytest.fail('transport called'))
    with pytest.raises(StorageError) as error:
        storage.sync(str(source), '/data/new', dry_run)
    assert error.value.code == 'read_only_storage'
    assert not (data / 'new').exists()


def test_readonly_check_is_policy_aware_and_fetch_allowed(setup, tmp_path):
    policy, _, data = setup
    storage = StorageService(policy, StorageAccessConfig())
    result = storage.check('/data')
    assert result['read_only'] is True
    assert result['writable'] is False
    assert result['readable'] is True
    destination = tmp_path / 'download'
    storage.fetch('/data', str(destination), dry_run=False)
    assert (destination / 'reference.txt').read_text() == 'reference'


def test_shared_aliases_do_not_bypass_readonly_policy(setup, tmp_path):
    policy, shared, data = setup
    storage = StorageService(policy, StorageAccessConfig())
    source = tmp_path / 'source'; source.mkdir()
    assert storage.check('/work/data')['read_only'] is True
    with pytest.raises(StorageError, match='read-only'):
        storage.sync(str(source), '/work/data/output', False)
    with pytest.raises(StorageError, match='read-only'):
        storage.fetch('/data', str(data / 'download'), False)
    assert not (data / 'output').exists()
    assert not (data / 'download').exists()


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
