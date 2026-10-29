from __future__ import annotations

import itertools
import random

import pytest

from determined_compute.compute.admission import ResourceInspector
from determined_compute.compute.gpu_groups import (
    describe_gpu_groups,
    holds_gpu_groups,
    normalize_gpu_groups,
)
from determined_compute.core.api_client import APIError

RTX = "NVIDIA GeForce RTX 5060 Ti"
CMP = "NVIDIA CMP 170HX"
SIM = {"name": "simulation", "count": 1, "models": [RTX]}
LEARNER = {"name": "learner", "count": 1, "models": [CMP]}


# Validation, as the master's GPUGroupRequests.Check words it.


def test_valid_groups_are_returned_as_plain_dicts():
    assert normalize_gpu_groups([SIM, LEARNER], 2) == [SIM, LEARNER]
    assert normalize_gpu_groups(None, 4) is None


@pytest.mark.parametrize(
    ("value", "slots", "message"),
    [
        ([SIM, LEARNER], 3, "gpu_groups counts must add up to slots (3)"),
        ([], 0, "gpu_groups must not be empty"),
        ("simulation", 1, "gpu_groups must be a list or null"),
        ([{"name": "Sim", "count": 1, "models": ["x"]}], 1,
         'gpu_groups name "Sim" must match ^[a-z][a-z0-9_-]{0,31}$'),
        ([{"count": 1, "models": ["x"]}], 1,
         "gpu_groups name None must match ^[a-z][a-z0-9_-]{0,31}$"),
        ([SIM, SIM], 2, 'gpu_groups name "simulation" is not unique'),
        ([{"name": "a", "count": 0, "models": ["x"]}], 0, "gpu_groups a: count must be at least 1"),
        ([{"name": "a", "count": True, "models": ["x"]}], 1,
         "gpu_groups a: count must be at least 1"),
        ([{"name": "a", "count": 1, "models": []}], 1, "gpu_groups a: models must not be empty"),
        ([{"name": "a", "count": 1, "models": ["x", "x"]}], 1,
         "gpu_groups a: models must be unique"),
        ([{"name": "a", "count": 1, "models": [""]}], 1, "gpu_groups a: a model must not be empty"),
        ([{"name": "a", "count": 1, "models": ["x"], "extra": 1}], 1,
         "gpu_groups[0] has unknown fields: ['extra']"),
        (["a"], 1, "gpu_groups[0] must be an object"),
    ],
)
def test_invalid_groups_are_refused_with_the_masters_words(value, slots, message):
    with pytest.raises(ValueError) as caught:
        normalize_gpu_groups(value, slots)
    assert str(caught.value) == message


def test_the_slots_field_is_named_in_the_count_message():
    with pytest.raises(ValueError) as caught:
        normalize_gpu_groups([SIM], 2, "slots_per_trial", "config.resources.gpu_groups")
    assert str(caught.value) == (
        "config.resources.gpu_groups counts must add up to slots_per_trial (2)"
    )


def test_groups_are_described_as_the_master_describes_them():
    groups = [SIM, {"name": "learner", "count": 2, "models": [CMP, "NVIDIA H100 PCIe"]}]
    assert describe_gpu_groups(groups) == (
        f"simulation: 1 × {RTX}; learner: 2 × {CMP} or NVIDIA H100 PCIe"
    )


# Matching.


def test_overlapping_model_lists_find_an_assignment():
    either = {"name": "either", "count": 1, "models": ["A", "B"]}
    only_a = {"name": "only-a", "count": 1, "models": ["A"]}
    # A greedy choice of A for "either" would leave nothing for "only-a".
    assert holds_gpu_groups([either, only_a], ["A", "B"])
    assert not holds_gpu_groups([either, only_a], ["B", "B"])


def test_more_gpus_than_candidates_are_refused_without_listing_them():
    huge = {"name": "huge", "count": 1 << 62, "models": ["A"]}
    assert not holds_gpu_groups([huge], ["A"] * 8)


def _oracle(groups, models):
    """Brute force: some assignment of distinct GPUs to every slot of every group."""

    slots = [group for group in groups for _ in range(group["count"])]
    for chosen in itertools.permutations(range(len(models)), len(slots)):
        if all(models[device] in group["models"] for device, group in zip(chosen, slots)):
            return True
    return False


def test_matching_equals_a_brute_force_oracle():
    rng = random.Random(20261030)
    palette = ["A", "B", "C"]
    for _ in range(2000):
        models = [rng.choice(palette) for _ in range(rng.randint(0, 6))]
        groups = []
        for index in range(rng.randint(1, 3)):
            groups.append({
                "name": f"g{index}",
                "count": rng.randint(1, 3),
                "models": rng.sample(palette, rng.randint(1, 3)),
            })
        assert holds_gpu_groups(groups, models) == _oracle(groups, models), (groups, models)


# Admission and compute_resources.


def mixed_agent(name="s8030", pool_name="gpu", brands=(CMP, RTX, CMP, RTX, CMP), busy=(),
                nodes=None, reason="", **extra):
    """An agent with one GPU slot per brand; nodes gives each device's NUMA node, if any."""

    slots = {}
    gpus = []
    for device_id, brand in enumerate(brands):
        slot = {
            "id": str(device_id),
            "enabled": True,
            "draining": False,
            "device": {"id": device_id, "type": "TYPE_CUDA", "uuid": f"GPU-{name}-{device_id}",
                       "brand": brand},
        }
        if device_id in busy:
            slot["container"] = {"id": f"container-{device_id}"}
        slots[str(device_id)] = slot
        if nodes is not None:
            gpus.append({"deviceId": device_id, "excluded": False, "numaNode": nodes[device_id],
                         "uuid": f"GPU-{name}-{device_id}"})
    value = {"id": name, "enabled": True, "draining": False, "resourcePools": [pool_name],
             "slots": slots}
    if nodes is not None or reason:
        value["gpuTopology"] = {"unknownReason": reason, "gpus": gpus, "links": []}
    value.update(extra)
    return value


def pool_of(agents, name="gpu", type="RESOURCE_POOL_TYPE_STATIC"):
    counted = [item for item in agents if item["enabled"] and not item["draining"]]
    return {
        "name": name,
        "type": type,
        "numAgents": len(agents),
        "slotsAvailable": sum(len(item["slots"]) for item in counted),
        "slotsUsed": sum(
            1 for item in agents for slot in item["slots"].values() if slot.get("container")
        ),
        "slotType": "CUDA",
        "auxContainerCapacity": 8,
        "auxContainersRunning": 0,
    }


class Client:
    def __init__(self, pools, agents):
        self.pools, self.agents = pools, agents

    def _get(self, endpoint, params=None):
        if endpoint == "api/v1/resource-pools":
            return {"resourcePools": self.pools}
        if endpoint == "api/v1/agents":
            return {"agents": self.agents}
        raise AssertionError(endpoint)


def inspector(agents, **pool_fields):
    return ResourceInspector(Client([pool_of(agents, **pool_fields)], agents))


def config(groups, kind="command", **resources):
    key = "slots_per_trial" if kind == "experiment" else "slots"
    slots = sum(group["count"] for group in groups)
    return {"resources": {key: slots, "resource_pool": "gpu", "gpu_groups": groups, **resources}}


def refused(target, cfg, kind="command"):
    with pytest.raises(APIError) as caught:
        target.require_capacity(kind, cfg)
    return caught.value


NO_AGENT = f"no agent in pool gpu has GPUs for gpu_groups simulation: 1 × {RTX}; learner: 1 × {CMP}"


@pytest.mark.parametrize("kind", ["command", "shell", "generic", "experiment"])
def test_groups_are_admitted_when_one_agent_has_free_gpus_for_every_group(kind):
    admitted = inspector([mixed_agent(busy={0, 1})]).require_capacity(
        kind, config([SIM, LEARNER], kind)
    )

    assert admitted["admitted"] is True
    selected = admitted["selected_pool"]
    assert selected["capacity_kind"] == "gpu_groups_single_agent"
    assert selected["available_capacity"] == 1
    assert admitted["gpu_groups"] == [SIM, LEARNER]


def test_a_two_slot_experiment_with_groups_is_not_treated_as_spanning_agents():
    # Without gpu_groups, a 2-slot experiment without is_single_node may span agents.
    admitted = inspector([mixed_agent()]).require_capacity(
        "experiment", config([SIM, LEARNER], "experiment")
    )
    assert admitted["admitted"] is True


def test_groups_wait_when_only_one_group_has_a_free_gpu():
    # Both RTX are busy: enough free slots, but none for "simulation".
    error = refused(inspector([mixed_agent(busy={1, 3})]), config([SIM, LEARNER]))

    assert error.code == "capacity_unavailable" and error.retryable is True
    assert str(error) == (
        f"no schedulable agent in pool 'gpu' has free GPUs for gpu_groups "
        f"simulation: 1 × {RTX}; learner: 1 × {CMP} now; the task would wait"
    )


def test_groups_no_agent_can_hold_are_refused_as_the_master_does():
    only_cmp = mixed_agent(brands=(CMP, CMP, CMP))
    error = refused(inspector([only_cmp]), config([SIM, LEARNER]))

    assert error.code == "capacity_unavailable" and error.retryable is False
    assert str(error) == NO_AGENT


def test_the_refusal_counts_busy_disabled_and_draining_gpus():
    # The only RTX is disabled: it takes no work now, but the master only waits for it.
    agent = mixed_agent(brands=(CMP, RTX))
    agent["slots"]["1"]["enabled"] = False
    target = ResourceInspector(Client([{**pool_of([agent]), "slotsAvailable": 1}], [agent]))
    error = refused(target, config([SIM, LEARNER]))

    assert error.retryable is True


def test_a_static_pool_without_agents_is_refused_and_a_dynamic_one_waits():
    static = refused(ResourceInspector(Client([pool_of([])], [])), config([SIM, LEARNER]))
    assert static.retryable is False and str(static) == NO_AGENT

    dynamic = refused(
        ResourceInspector(Client([pool_of([], type="RESOURCE_POOL_TYPE_AWS")], [])),
        config([SIM, LEARNER]),
    )
    assert dynamic.retryable is True


def test_an_unreadable_gpu_model_makes_the_capacity_unknown():
    agent = mixed_agent()
    del agent["slots"]["1"]["device"]["brand"]
    error = refused(inspector([agent]), config([SIM, LEARNER]))

    assert error.code == "capacity_unknown"
    assert "GPU models of agent s8030 are unreadable" in str(error)


def test_strong_groups_need_one_numa_node_with_free_gpus_for_every_group():
    split = mixed_agent(brands=(RTX, CMP, RTX, CMP), nodes=(0, 1, 1, 0), busy={2, 3})
    error = refused(inspector([split]), config([SIM, LEARNER], prefer_gpu_topology="strong"))
    assert error.retryable is True and "on one NUMA node now" in str(error)

    together = mixed_agent(brands=(RTX, CMP, RTX, CMP), nodes=(0, 1, 1, 0), busy={1})
    admitted = inspector([together]).require_capacity(
        "command", config([SIM, LEARNER], prefer_gpu_topology="strong")
    )
    assert admitted["selected_pool"]["capacity_kind"] == "gpu_groups_one_numa_node"


def test_strong_groups_no_numa_node_can_hold_are_refused():
    apart = mixed_agent(brands=(RTX, CMP), nodes=(0, 1))
    error = refused(inspector([apart]), config([SIM, LEARNER], prefer_gpu_topology="strong"))

    assert error.retryable is False and str(error) == NO_AGENT


def test_strong_groups_wait_for_an_agent_without_reported_topology():
    pending = mixed_agent(brands=(RTX, CMP), reason="not reported since the master started")
    error = refused(inspector([pending]), config([SIM, LEARNER], prefer_gpu_topology="strong"))

    assert error.retryable is True


def test_resources_reports_free_gpus_by_model_and_checks_groups():
    target = inspector([mixed_agent(busy={0})])

    plain = target.resources(1, "gpu")["selected_pool"]
    assert plain["per_agent_free_gpus_by_model"] == {"s8030": {CMP: 2, RTX: 2}}
    assert plain["capacity_kind"] == "slots_single_agent"

    report = target.resources(2, "gpu", None, [SIM, LEARNER])
    assert report["gpu_groups"] == [SIM, LEARNER]
    assert report["selected_pool"]["capacity_kind"] == "gpu_groups_single_agent"
    assert report["available"] is True


def test_resources_refuses_groups_whose_counts_differ_from_slots():
    with pytest.raises(ValueError) as caught:
        inspector([mixed_agent()]).resources(3, "gpu", None, [SIM, LEARNER])
    assert str(caught.value) == "gpu_groups counts must add up to slots (3)"
