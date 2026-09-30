import pytest

from determined_compute.policy import Mount, MountMap, Policy, PolicyError, Resources


def config(**overrides):
    value = {
        "mounts": [
            {"host_path": "/shared/projects", "container_path": "/workspace"},
            {"host_path": "/shared/projects/reference", "container_path": "/reference",
             "read_only": True},
        ],
        "defaults": {"image": "registry/image:1", "pool": "gpu", "slots": 1},
    }
    value.update(overrides)
    return value


def refused(code, operation, *args, **kwargs):
    with pytest.raises(PolicyError) as caught:
        operation(*args, **kwargs)
    assert caught.value.code == code
    assert caught.value.retryable is False
    return caught.value


def test_defaults_fill_what_the_request_omits():
    policy = Policy.from_dict(config())

    assert policy.resources() == Resources(image="registry/image:1", pool="gpu", slots=1)
    assert policy.resources(image="other:2", slots=0) == Resources("other:2", "gpu", 0)
    assert policy.allow_overwrite is False
    assert policy.max_slots is None


def test_without_an_allow_list_only_the_default_pool_is_allowed():
    policy = Policy.from_dict(config())

    assert policy.pools == frozenset({"gpu"})
    error = refused("pool_not_allowed", policy.resources, pool="cpu")
    assert error.details == {"pool": "cpu", "allowed": ["gpu"]}


def test_allow_list_admits_named_pools_and_must_hold_the_default():
    policy = Policy.from_dict(config(pools=["gpu", "cpu"]))

    assert policy.resources(pool="cpu").pool == "cpu"
    refused("pool_not_allowed", policy.check_pool, "a100")
    refused("invalid_policy", Policy.from_dict, config(pools=["cpu"]))


def test_max_slots_bounds_slots_times_concurrent_trials():
    policy = Policy.from_dict(config(max_slots=8))

    assert policy.resources(slots=8).slots == 8
    assert policy.resources(slots=2, trials=4).slots == 2
    error = refused("slots_exceed_limit", policy.resources, slots=3, trials=3)
    assert error.details == {"slots": 3, "trials": 3, "max_slots": 8}
    assert "9 slots" in str(error)
    refused("slots_exceed_limit", policy.check_slots, 9)
    refused("invalid_request", policy.check_slots, 1, 0)
    refused("invalid_request", policy.check_slots, True)
    refused("invalid_request", policy.check_slots, -1)
    refused("invalid_policy", Policy.from_dict, config(max_slots=0))


def test_overwrite_needs_the_policy():
    refused("overwrite_not_allowed", Policy.from_dict(config()).check_overwrite, True)
    Policy.from_dict(config()).check_overwrite(False)
    Policy.from_dict(config(allow_overwrite=True)).check_overwrite(True)
    refused("invalid_policy", Policy.from_dict, config(allow_overwrite="yes"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"cluster_identity": "cluster"},
        {"shell_inactivity_seconds": 7200},
        {"shared_mounts": []},
        {"mounts": []},
        {"mounts": [{"host_path": "relative", "container_path": "/x"}]},
        {"mounts": [{"host_path": "/a/../b", "container_path": "/x"}]},
        {"mounts": [{"host_path": "/a", "container_path": "/x", "bind": True}]},
        {"mounts": [{"host_path": "/a", "container_path": "/x", "read_only": "no"}]},
        {"mounts": [{"host_path": "/a", "container_path": "/x"},
                    {"host_path": "/b", "container_path": "/x/y"}]},
        {"defaults": {"image": "", "pool": "gpu"}},
        {"defaults": {"image": "i", "pool": "gpu", "slots": -1}},
        {"defaults": {"image": "i", "pool": "gpu", "slots": True}},
        {"defaults": {"image": "i", "pool": "gpu", "memory": 1}},
        {"pools": "gpu"},
        {"max_slots": -1},
    ],
)
def test_policy_file_is_strict(overrides):
    refused("invalid_policy", Policy.from_dict, config(**overrides))


def test_policy_file_formats(tmp_path):
    yaml_file = tmp_path / "policy.yaml"
    yaml_file.write_text(
        "mounts:\n  - {host_path: /h, container_path: /c}\n"
        "defaults: {image: i, pool: p}\nmax_slots: 4\n",
        encoding="utf-8",
    )
    policy = Policy.from_file(yaml_file)
    assert policy.mounts == MountMap((Mount("/h", "/c"),))
    assert policy.max_slots == 4
    assert policy.slots == 1

    json_file = tmp_path / "policy.json"
    json_file.write_text('{"mounts": [{"host_path": "/h", "container_path": "/c"}], '
                         '"defaults": {"image": "i", "pool": "p"}}', encoding="utf-8")
    assert Policy.from_file(json_file).pool == "p"

    refused("invalid_policy", Policy.from_file, tmp_path / "missing.yaml")
    (tmp_path / "bad.yaml").write_text("mounts: [", encoding="utf-8")
    refused("invalid_policy", Policy.from_file, tmp_path / "bad.yaml")


def test_mount_map_translates_container_paths_to_host_paths():
    mounts = Policy.from_dict(config()).mounts

    assert mounts.container_roots == ("/workspace", "/reference")
    assert mounts.to_host("/workspace") == (mounts.mounts[0], "/shared/projects")
    assert mounts.to_host("//workspace/./runs/one/")[1] == "/shared/projects/runs/one"
    assert mounts.to_host("/reference/data")[1] == "/shared/projects/reference/data"
    error = refused("path_not_mounted", mounts.to_host, "/workspace2/x")
    assert error.details == {"path": "/workspace2/x", "roots": ["/workspace", "/reference"]}
    refused("invalid_path", mounts.to_host, "workspace/x")
    refused("invalid_path", mounts.to_host, "/workspace/../etc")
    refused("invalid_path", mounts.to_host, None)


def test_read_only_follows_the_most_specific_host_root():
    mounts = Policy.from_dict(config()).mounts

    assert mounts.read_only("/workspace/runs") is False
    assert mounts.read_only("/reference/data") is True
    # The same host directory reached through the writable alias stays read-only.
    assert mounts.read_only("/workspace/reference/data") is True
    assert mounts.host_read_only("/shared/projects/runs") is False
    assert mounts.host_read_only("/elsewhere") is True


def test_read_only_wins_a_tie_between_aliases_of_one_host_root():
    mounts = MountMap((Mount("/h", "/a"), Mount("/h", "/b", read_only=True)))

    assert mounts.read_only("/a/x") is True
    assert mounts.host_read_only("/h/x") is True


def test_root_host_mount_translates_without_a_double_slash():
    mounts = MountMap((Mount("/", "/host"),))

    assert mounts.to_host("/host/etc")[1] == "/etc"
    assert mounts.to_host("/host")[1] == "/"
