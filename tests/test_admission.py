from __future__ import annotations

import copy

import pytest

from determined_compute.compute.admission import ResourceInspector
from determined_compute.core.api_client import APIError


def pool(name="gpu", *, total=4, used=0, agents=1, aux=8, aux_running=0,
         type="RESOURCE_POOL_TYPE_STATIC"):
    value = {
        "name": name,
        "type": type,
        "numAgents": agents,
        "slotsAvailable": total,
        "slotsUsed": used,
        "slotType": "CUDA",
        "auxContainerCapacity": aux,
        "auxContainersRunning": aux_running,
    }
    return value


def agent(name="agent-1", pool_name="gpu", slots=4, occupied=0, **extra):
    values = {}
    for index in range(slots):
        slot = {"id": str(index), "enabled": True, "draining": False}
        if index < occupied:
            slot["container"] = {"id": f"container-{index}"}
        values[str(index)] = slot
    return {
        "id": name,
        "enabled": True,
        "draining": False,
        "resourcePools": [pool_name],
        "slots": values,
        **extra,
    }


class Client:
    def __init__(self, pools, agents, error=None):
        self.pools = pools
        self.agents = agents
        self.error = error
        self.calls = []

    def _get(self, endpoint, params=None):
        self.calls.append((endpoint, params))
        if self.error:
            raise self.error
        if endpoint == "api/v1/resource-pools":
            return {"resourcePools": self.pools}
        if endpoint == "api/v1/agents":
            return {"agents": self.agents}
        raise AssertionError(endpoint)


def command_config(slots=1, pool_name="gpu", **resources):
    return {
        "resources": {"slots": slots, "resource_pool": pool_name, **resources}
    }


def test_busy_pool_is_unavailable_even_if_slots_are_low_utilization():
    client = Client([pool(total=4, used=4)], [agent(occupied=4)])
    inspector = ResourceInspector(client)

    with pytest.raises(APIError) as caught:
        inspector.require_capacity("command", command_config())

    assert caught.value.code == "capacity_unavailable"
    assert caught.value.details["available"] == 0
    assert client.calls == [
        ("api/v1/agents", {"limit": 0}),
        ("api/v1/resource-pools", {"limit": 0}),
    ]


def test_missing_statistics_are_unknown_not_zero():
    incomplete = pool()
    incomplete.pop("slotsAvailable")
    inspector = ResourceInspector(Client([incomplete], [agent()]))

    report = inspector.resources(pool="gpu")
    assert report["selected_pool"]["available"] is None
    assert report["selected_pool"]["available_capacity"] is None
    with pytest.raises(APIError) as caught:
        inspector.require_capacity("command", command_config())
    assert caught.value.code == "capacity_unknown"

    missing_used = pool()
    missing_used.pop("slotsUsed")
    report = ResourceInspector(Client([missing_used], [agent()])).resources(pool="gpu")
    assert report["selected_pool"]["available"] is None


def test_a_pool_missing_from_the_list_is_not_present_or_not_available():
    # The pool list holds only the pools the account may use.
    inspector = ResourceInspector(Client([pool()], [agent()]))

    report = inspector.resources(pool="restricted")
    assert report["selected_pool"] is None
    assert report["available"] is None
    assert report["explanation"] == (
        "resource pool 'restricted' is not present or not available to you"
    )
    with pytest.raises(APIError) as caught:
        inspector.require_capacity("command", command_config(pool_name="restricted"))
    assert caught.value.code == "capacity_unknown"
    assert str(caught.value) == "resource pool 'restricted' is not present or not available to you"
    assert caught.value.details["resource_pool"] == "restricted"
    assert caught.value.details["candidate_pools"] == ["gpu"]


def test_authentication_error_propagates_unchanged():
    failure = APIError("unauthorized", code=401)
    inspector = ResourceInspector(Client([], [], error=failure))

    with pytest.raises(APIError) as caught:
        inspector.resources()
    assert caught.value is failure


def test_zero_gpu_uses_auxiliary_capacity_not_free_gpu_slots():
    inspector = ResourceInspector(
        Client([pool(total=8, used=8, aux=3, aux_running=2)], [agent(slots=8, occupied=8)])
    )
    admitted = inspector.require_capacity("command", command_config(slots=0))
    selected = admitted["selected_pool"]

    assert selected["capacity_kind"] == "aux_containers"
    assert selected["available_capacity"] == 1
    assert admitted["admitted"] is True

    busy = ResourceInspector(
        Client([pool(total=8, used=0, aux=3, aux_running=3)], [agent(slots=8)])
    )
    with pytest.raises(APIError) as caught:
        busy.require_capacity("shell", command_config(slots=0))
    assert caught.value.code == "capacity_unavailable"
    assert caught.value.details["available"] == 0


SPAN_UNKNOWN = (
    "capacity for resource pool 'gpu' is unknown: this experiment may span agents, which "
    "this service does not check; set is_single_node: true, or launch with allow_queue=true"
)


def experiment_config(slots, **resources):
    return {"resources": {"slots_per_trial": slots, "resource_pool": "gpu", **resources}}


@pytest.mark.parametrize("extra", [{}, {"is_single_node": None}, {"is_single_node": False}])
def test_a_multi_agent_experiment_that_fits_no_single_agent_is_unknown(extra):
    pools = [pool(total=4, used=2, agents=2)]
    agents = [agent("a", slots=2, occupied=1), agent("b", slots=2, occupied=1)]
    inspector = ResourceInspector(Client(pools, agents))

    with pytest.raises(APIError) as caught:
        inspector.require_capacity("experiment", experiment_config(2, **extra))
    assert caught.value.code == "capacity_unknown"
    assert caught.value.retryable is False
    assert str(caught.value) == SPAN_UNKNOWN
    assert caught.value.details["available"] == 1


def test_a_single_node_experiment_that_fits_no_agent_is_unavailable():
    pools = [pool(total=4, used=2, agents=2)]
    agents = [agent("a", slots=2, occupied=1), agent("b", slots=2, occupied=1)]
    inspector = ResourceInspector(Client(pools, agents))

    with pytest.raises(APIError) as caught:
        inspector.require_capacity("experiment", experiment_config(2, is_single_node=True))
    assert caught.value.code == "capacity_unavailable"
    assert caught.value.retryable is True
    assert caught.value.details["available"] == 1


@pytest.mark.parametrize("kind", ["command", "shell", "generic", "experiment"])
@pytest.mark.parametrize("value", ["yes", 1, [], {}])
def test_a_non_boolean_is_single_node_is_rejected(kind, value):
    config = experiment_config(2) if kind == "experiment" else command_config(slots=2)
    config["resources"]["is_single_node"] = value
    inspector = ResourceInspector(Client([pool()], [agent()]))

    with pytest.raises(ValueError, match="is_single_node must be a boolean or null"):
        inspector.require_capacity(kind, config)


def test_a_multi_agent_experiment_is_admitted_when_one_agent_has_the_slots():
    pools = [pool(total=16, agents=2)]
    agents = [agent("a", slots=8), agent("b", slots=8)]
    inspector = ResourceInspector(Client(pools, agents))

    admitted = inspector.require_capacity("experiment", experiment_config(8))
    assert admitted["admitted"] is True
    assert admitted["selected_pool"]["available_capacity"] == 8
    assert admitted["selected_pool"]["capacity_kind"] == "slots_single_agent"

    # Free slots are never summed across agents.
    with pytest.raises(APIError) as caught:
        inspector.require_capacity("experiment", experiment_config(16))
    assert caught.value.code == "capacity_unknown"
    assert str(caught.value) == SPAN_UNKNOWN
    assert caught.value.details["available"] == 8

    with pytest.raises(APIError) as caught:
        inspector.require_capacity("experiment", experiment_config(16, is_single_node=True))
    assert caught.value.code == "capacity_unavailable"


@pytest.mark.parametrize("kind", ["command", "shell", "generic"])
def test_single_agent_kinds_never_span_agents(kind):
    pools = [pool(total=16, agents=2)]
    agents = [agent("a", slots=8), agent("b", slots=8)]
    inspector = ResourceInspector(Client(pools, agents))

    with pytest.raises(APIError) as caught:
        inspector.require_capacity(kind, command_config(slots=16, is_single_node=False))
    assert caught.value.code == "capacity_unavailable"
    assert caught.value.details["available"] == 8


def test_a_one_slot_experiment_uses_the_single_agent_rule():
    inspector = ResourceInspector(
        Client([pool(total=2, used=2)], [agent(slots=2, occupied=2)])
    )

    with pytest.raises(APIError) as caught:
        inspector.require_capacity("experiment", experiment_config(1, is_single_node=False))
    assert caught.value.code == "capacity_unavailable"


def test_a_multi_agent_experiment_keeps_an_unknown_pool_cause():
    inspector = ResourceInspector(Client([pool(total=7, agents=2)], [agent("a", slots=8)]))

    with pytest.raises(APIError) as caught:
        inspector.require_capacity("experiment", experiment_config(2))
    assert caught.value.code == "capacity_unknown"
    assert "pool reports 7 slot(s) on 2 agent(s)" in str(caught.value)


def test_resources_report_one_agent_free_slots_only():
    pools = [pool(total=16, used=4, agents=2)]
    agents = [agent("a", slots=8), agent("b", slots=8, occupied=4)]
    report = ResourceInspector(Client(pools, agents)).resources(slots=12, pool="gpu")
    selected = report["selected_pool"]

    assert report["single_node"] is True
    assert report["available"] is False
    assert selected["capacity_kind"] == "slots_single_agent"
    assert selected["available_capacity"] == 8
    assert selected["max_single_agent_free_slots"] == 8
    assert "aggregate_schedulable_free_slots" not in selected


def test_draining_disabled_and_allocated_slots_are_not_free():
    draining = agent("draining", slots=2, draining=True)
    mixed = agent("mixed", slots=3, occupied=1)
    mixed["slots"]["1"]["draining"] = True
    mixed["slots"]["2"]["enabled"] = False
    # The pool counts only the busy slot: the draining agent's slots are idle, and the
    # idle draining slot and the disabled slot leave the pool.
    inspector = ResourceInspector(
        Client([pool(total=1, used=1, agents=2)], [draining, mixed])
    )

    selected = inspector.resources(pool="gpu")["selected_pool"]
    assert selected["per_agent_free_slots"] == {"draining": 0, "mixed": 0}
    assert selected["aggregate_reported_free_slots"] == 0
    assert selected["available"] is False


def set_slot(value, index, **fields):
    value["slots"][str(index)].update(fields)
    return value


def test_a_drained_idle_slot_leaves_the_pool_and_the_rest_stays_known():
    drained = set_slot(agent(slots=8), 0, draining=True)
    report = ResourceInspector(Client([pool(total=7)], [drained])).resources(pool="gpu")

    assert report["selected_pool"]["available"] is True
    assert report["selected_pool"]["available_capacity"] == 7


def test_a_drained_busy_slot_is_counted_but_not_free():
    drained = set_slot(agent(slots=8, occupied=1), 0, draining=True)
    report = ResourceInspector(Client([pool(total=8, used=1)], [drained])).resources(pool="gpu")

    assert report["selected_pool"]["available"] is True
    assert report["selected_pool"]["available_capacity"] == 7


def test_a_disabled_idle_slot_is_not_counted():
    disabled = set_slot(agent(slots=8), 3, enabled=False)
    report = ResourceInspector(Client([pool(total=7)], [disabled])).resources(pool="gpu")

    assert report["selected_pool"]["available_capacity"] == 7


def test_a_slot_disabled_without_drain_is_not_counted_while_its_container_is_killed():
    # The device leaves the pool at once, although the container is still listed.
    killing = set_slot(agent(slots=8, occupied=1), 0, enabled=False)
    report = ResourceInspector(Client([pool(total=7)], [killing])).resources(pool="gpu")

    assert report["selected_pool"]["available"] is True
    assert report["selected_pool"]["available_capacity"] == 7

    # Its container is not among the used slots either (the pool reports 0 used).
    selected = ResourceInspector(Client([pool(total=7, used=0)], [killing])).resources(
        slots=2, pool="gpu"
    )["selected_pool"]
    assert selected["available"] is True
    assert selected["available_capacity"] == 7

    # Under an agent drain it is not counted either.
    drained = agent(slots=8, occupied=2, enabled=False, draining=True)
    set_slot(drained, 0, enabled=False)
    report = ResourceInspector(Client([pool(total=1, used=1)], [drained])).resources(pool="gpu")
    assert report["selected_pool"]["available"] is False
    assert report["selected_pool"]["available_capacity"] == 0

    selected = ResourceInspector(Client([pool(total=1, used=1)], [drained])).resources(
        slots=2, pool="gpu"
    )["selected_pool"]
    assert selected["available"] is False
    assert selected["available_capacity"] == 0


def test_a_busy_enabled_slot_on_a_disabled_agent_counts_as_used():
    # A slot re-enabled on a disabled agent gets its device back with its container: the
    # pool counts it as used although the agent adds no slots.
    off = agent("off", slots=4, occupied=1, enabled=False, draining=False)
    for index in range(1, 4):
        set_slot(off, index, enabled=False)
    on = agent("on", slots=4)
    selected = ResourceInspector(
        Client([pool(total=4, used=1, agents=2)], [off, on])
    ).resources(slots=2, pool="gpu")["selected_pool"]

    assert selected["available"] is True
    assert selected["available_capacity"] == 4
    assert selected["per_agent_free_slots"] == {"off": 0, "on": 4}


def test_a_disabled_agent_counts_no_slots():
    disabled = agent(slots=8, occupied=0, enabled=False, draining=False)
    inspector = ResourceInspector(Client([pool(total=0)], [disabled]))

    selected = inspector.resources(pool="gpu")["selected_pool"]
    assert selected["available"] is False
    assert selected["available_capacity"] == 0
    with pytest.raises(APIError) as caught:
        inspector.require_capacity("command", command_config())
    assert caught.value.code == "capacity_unavailable"


def test_an_agent_under_disable_drain_counts_only_its_busy_slots():
    drained = agent(slots=8, occupied=2, enabled=False, draining=True)
    selected = ResourceInspector(
        Client([pool(total=2, used=2)], [drained])
    ).resources(pool="gpu")["selected_pool"]

    assert selected["available"] is False
    assert selected["available_capacity"] == 0
    assert selected["per_agent_free_slots"] == {"agent-1": 0}


def test_an_agent_with_an_excluded_gpu_matches_its_pool():
    report = ResourceInspector(Client([pool(total=7)], [agent(slots=7)])).resources(pool="gpu")

    assert report["selected_pool"]["available"] is True
    assert report["selected_pool"]["available_capacity"] == 7


def test_a_slot_count_mismatch_is_unknown_and_names_both_counts():
    inspector = ResourceInspector(Client([pool(total=7)], [agent(slots=8)]))

    selected = inspector.resources(pool="gpu")["selected_pool"]
    assert selected["available"] is None
    assert selected["available_capacity"] is None
    expected = (
        "pool reports 7 slot(s) on 1 agent(s); "
        "the agent list gives 8 counted slot(s) on 1 agent(s)"
    )
    assert selected["explanation"] == expected
    with pytest.raises(APIError) as caught:
        inspector.require_capacity("command", command_config())
    assert caught.value.code == "capacity_unknown"
    assert caught.value.retryable is False
    assert str(caught.value) == f"capacity for resource pool 'gpu' is unknown: {expected}"


def test_an_agent_count_mismatch_names_both_counts():
    selected = ResourceInspector(
        Client([pool(total=4, agents=2)], [agent(slots=4)])
    ).resources(pool="gpu")["selected_pool"]

    assert selected["available"] is None
    assert selected["explanation"] == (
        "pool reports 4 slot(s) on 2 agent(s); "
        "the agent list gives 4 counted slot(s) on 1 agent(s)"
    )


def test_inventory_reports_multiple_pools_and_only_suggests_alternatives():
    pools = [
        pool("busy", total=2, used=2),
        pool("idle", total=4, used=0),
    ]
    agents = [
        agent("busy-agent", "busy", slots=2, occupied=2),
        agent("idle-agent", "idle", slots=4),
    ]
    inspector = ResourceInspector(Client(pools, agents))
    report = inspector.resources(slots=2, pool="busy")

    assert [item["resource_pool"] for item in report["pools"]] == ["busy", "idle"]
    assert report["candidate_pools"] == ["idle"]
    assert report["alternatives"] == [
        {
            "resource_pool": "idle",
            "available_capacity": 4,
            "capacity_kind": "slots_single_agent",
        }
    ]
    assert "compatibility was not checked" in report["advisory"]


@pytest.mark.parametrize("slots", [True, -1, 1.5, "1"])
def test_slot_bounds_and_bool_are_rejected(slots):
    inspector = ResourceInspector(Client([], []))
    with pytest.raises(ValueError, match="non-negative integer"):
        inspector.resources(slots=slots)


def test_generic_task_uses_slots_for_admission():
    inspector = ResourceInspector(Client([pool(total=1, used=0)], [agent(slots=1)]))
    assert inspector.require_capacity("generic", command_config())["admitted"] is True
    busy = ResourceInspector(Client([pool(total=1, used=1)], [agent(slots=1, occupied=1)]))
    with pytest.raises(APIError) as caught:
        busy.require_capacity("generic", command_config())
    assert caught.value.code == "capacity_unavailable"


def test_every_call_reads_agents_then_pools_exactly_once():
    client = Client([pool()], [agent()])
    inspector = ResourceInspector(client)
    expected = [("api/v1/agents", {"limit": 0}), ("api/v1/resource-pools", {"limit": 0})]

    inspector.resources(slots=2, pool="gpu")
    assert client.calls == expected
    client.calls.clear()
    inspector.require_capacity("command", command_config(slots=2))
    assert client.calls == expected
    client.calls.clear()
    inspector.require_capacity("experiment", {
        "resources": {"slots_per_trial": 2, "resource_pool": "gpu"}
    })
    assert client.calls == expected


@pytest.mark.parametrize("used, holding", [(2, 1), (0, 1)])
def test_used_slots_that_differ_from_busy_slots_are_unknown_for_two_or_more_slots(used, holding):
    agents = [agent(slots=4, occupied=holding)]
    inspector = ResourceInspector(Client([pool(total=4, used=used)], agents))

    selected = inspector.resources(slots=2, pool="gpu")["selected_pool"]
    expected = (
        f"used slots ({used}) differ from slots holding containers ({holding}); "
        "a task may be starting or stopping"
    )
    assert selected["available"] is None
    assert selected["available_capacity"] is None
    assert selected["explanation"] == expected
    with pytest.raises(APIError) as caught:
        inspector.require_capacity("command", command_config(slots=2))
    assert caught.value.code == "capacity_unknown"
    assert caught.value.retryable is False
    assert caught.value.details["available"] is None
    assert str(caught.value) == f"capacity for resource pool 'gpu' is unknown: {expected}"


@pytest.mark.parametrize("used", [2, 0])
def test_the_used_slot_check_does_not_apply_to_zero_or_one_slot(used):
    inspector = ResourceInspector(
        Client([pool(total=4, used=used)], [agent(slots=4, occupied=1)])
    )

    admitted = inspector.require_capacity("command", command_config(slots=1))
    assert admitted["selected_pool"]["available_capacity"] == 3
    assert inspector.require_capacity("command", command_config(slots=0))["admitted"] is True


def test_the_used_slot_count_includes_busy_slots_of_a_draining_agent():
    drained = agent("drained", slots=4, occupied=2, draining=True)
    idle = agent("idle", slots=4)
    selected = ResourceInspector(
        Client([pool(total=6, used=2, agents=2)], [drained, idle])
    ).resources(slots=2, pool="gpu")["selected_pool"]

    assert selected["available"] is True
    assert selected["per_agent_free_slots"] == {"drained": 0, "idle": 4}


# prefer_gpu_topology "strong": all slots on one NUMA node of one agent.

PENDING = "not reported since the master started"


def test_the_pending_reason_is_the_forks_constant():
    # Fork 0.42.0 or later (reasonNotReportedSinceMasterStart); re-check on fork upgrades, since
    # a renamed constant would read as an agent without NUMA nodes.
    from determined_compute.compute import admission

    assert admission._TOPOLOGY_PENDING == PENDING


def numa_agent(name="agent-1", pool_name="gpu", nodes=((0, 1, 2, 3), (4, 5, 6, 7)),
               busy=(), reason="", **extra):
    """An agent whose CUDA slot ids sit on the NUMA nodes given in order (node 0, node 1, ...).

    A node given as -1 entries is written as {"ids": [...], "node": -1}.
    """
    placement = []
    for index, ids in enumerate(nodes):
        if isinstance(ids, dict):
            placement.extend((device_id, ids["node"]) for device_id in ids["ids"])
        else:
            placement.extend((device_id, index) for device_id in ids)
    slots = {}
    gpus = []
    for device_id, node in sorted(placement):
        slot = {
            "id": str(device_id),
            "enabled": True,
            "draining": False,
            "device": {"id": device_id, "type": "TYPE_CUDA", "uuid": f"GPU-{name}-{device_id}"},
        }
        if device_id in busy:
            slot["container"] = {"id": f"container-{device_id}"}
        slots[str(device_id)] = slot
        gpus.append({
            "deviceId": device_id,
            "excluded": False,
            "numaNode": node,
            "health": "GPU_HEALTH_OK",
            "nvmlError": "",
            "uuid": f"GPU-{name}-{device_id}",
        })
    value = {
        "id": name,
        "enabled": True,
        "draining": False,
        "resourcePools": [pool_name],
        "slots": slots,
        "gpuTopology": {"unknownReason": reason, "gpus": gpus, "links": []},
    }
    value.update(extra)
    return value


def strong_config(slots, kind="command", **resources):
    key = "slots_per_trial" if kind == "experiment" else "slots"
    return {"resources": {
        key: slots, "resource_pool": "gpu", "prefer_gpu_topology": "strong", **resources,
    }}


def numa_pool(agents, **fields):
    slots = sum(
        len(item["slots"]) for item in agents if item["enabled"] and not item["draining"]
    )
    busy = sum(
        1 for item in agents for slot in item["slots"].values()
        if slot.get("container") is not None
    )
    fields.setdefault("total", slots)
    fields.setdefault("used", busy)
    fields.setdefault("agents", len(agents))
    return pool(**fields)


def strong_inspector(agents, **fields):
    return ResourceInspector(Client([numa_pool(agents, **fields)], agents))


def refused(inspector, config, kind="command"):
    with pytest.raises(APIError) as caught:
        inspector.require_capacity(kind, config)
    return caught.value


def wait_message(slots, most):
    return (
        f"no schedulable agent in pool 'gpu' has {slots} free GPUs on one NUMA node now "
        f"(most on one node: {most}); a \"strong\" task would wait"
    )


NO_NODES = "no agent in pool gpu reports NUMA nodes; use soft"


def no_node_holds(slots):
    return f"no NUMA node in pool gpu has {slots} slots; use soft"


@pytest.mark.parametrize("kind", ["command", "shell", "generic", "experiment"])
def test_strong_is_admitted_when_one_numa_node_has_the_free_gpus(kind):
    # T1: node 0 is free, node 1 is busy.
    inspector = strong_inspector([numa_agent(busy={4, 5, 6, 7})])

    admitted = inspector.require_capacity(kind, strong_config(4, kind))
    selected = admitted["selected_pool"]
    assert admitted["admitted"] is True
    assert admitted["prefer_gpu_topology"] == "strong"
    assert selected["capacity_kind"] == "slots_one_numa_node"
    assert selected["available_capacity"] == 4
    assert selected["max_numa_node_free_slots"] == 4
    assert selected["max_numa_node_slots"] == 4


def test_strong_waits_when_the_free_gpus_span_numa_nodes():
    # T2: 2 + 2 free; without a preference, or with soft, one agent has the 4 free slots.
    agents = [numa_agent(busy={0, 1, 4, 5})]
    inspector = strong_inspector(agents)

    error = refused(inspector, strong_config(4))
    assert error.code == "capacity_unavailable"
    assert error.retryable is True
    assert error.details["available"] == 2
    assert str(error) == wait_message(4, 2)
    for preference in (None, False, "soft"):
        config = command_config(slots=4, prefer_gpu_topology=preference)
        assert inspector.require_capacity("command", config)["admitted"] is True


def test_strong_larger_than_every_numa_node_is_refused_as_the_master_does():
    # T3: 8 free slots, but no node has 5.
    inspector = strong_inspector([numa_agent()])

    error = refused(inspector, strong_config(5))
    assert error.code == "capacity_unavailable"
    assert error.retryable is False
    assert str(error) == no_node_holds(5)
    assert error.details["available"] == 4
    selected = inspector.resources(5, "gpu", "strong")["selected_pool"]
    assert selected["available"] is False
    assert selected["max_numa_node_slots"] == 4
    assert selected["max_numa_node_free_slots"] == 4


def test_strong_equal_to_the_largest_agent_is_not_refused_by_step_b():
    # One 8-slot agent with every slot on node 0: n equal to its slot count fits the agent.
    full = strong_inspector([numa_agent(nodes=(range(8),))])
    admitted = full.require_capacity("command", strong_config(8))
    assert admitted["admitted"] is True
    assert admitted["selected_pool"]["available_capacity"] == 8
    error = refused(strong_inspector([numa_agent(nodes=(range(8),), busy={0})]), strong_config(8))
    assert error.code == "capacity_unavailable"
    assert error.retryable is True
    assert str(error) == wait_message(8, 7)


def test_strong_waits_with_one_free_gpu_per_numa_node():
    # T4
    error = refused(strong_inspector([numa_agent(busy={1, 2, 3, 5, 6, 7})]), strong_config(2))
    assert error.code == "capacity_unavailable"
    assert error.retryable is True
    assert str(error) == wait_message(2, 1)


@pytest.mark.parametrize("slots, retryable, message", [
    (4, True, wait_message(4, 0)),
    (5, True, wait_message(5, 0)),
    (9, False, no_node_holds(9)),
])
def test_an_agent_pending_its_topology_lets_strong_wait(slots, retryable, message):
    # T5: the master waits for a pending agent's report; a request larger than every agent is
    # still refused from slot counts.
    # The fork lists a pending agent's slots with numaNode -1.
    pending = numa_agent("pending", reason=PENDING, nodes=({"ids": range(8), "node": -1},))
    busy = numa_agent("busy", busy=set(range(8)))
    inspector = strong_inspector([pending, busy])

    error = refused(inspector, strong_config(slots))
    assert error.code == "capacity_unavailable"
    assert error.retryable is retryable
    assert str(error) == message
    selected = inspector.resources(slots, "gpu", "strong")["selected_pool"]
    assert selected["max_numa_node_slots"] is None
    assert "agent pending has not reported GPU topology" in selected["explanation"]


def test_agents_without_topology_support_are_refused_with_no_numa_nodes():
    # T6
    reason = "agent 0.40.0 does not report GPU topology"
    agents = [numa_agent("a", reason=reason), numa_agent("b", reason=reason)]
    inspector = strong_inspector(agents)

    error = refused(inspector, strong_config(4))
    assert error.code == "capacity_unavailable"
    assert error.retryable is False
    assert str(error) == NO_NODES
    selected = inspector.resources(4, "gpu", "strong")["selected_pool"]
    assert selected["max_numa_node_slots"] == 0
    assert f"agent a reports no NUMA nodes ({reason!r})" in selected["explanation"]


def _null_topology(value):
    value["gpuTopology"] = None


def _absent_topology(value):
    del value["gpuTopology"]


def _missing_entry(value):
    value["gpuTopology"]["gpus"].pop(2)


def _duplicate_device(value):
    value["gpuTopology"]["gpus"][1]["deviceId"] = 0


def _text_numa_node(value):
    value["gpuTopology"]["gpus"][0]["numaNode"] = "0"


def _bool_numa_node(value):
    value["gpuTopology"]["gpus"][0]["numaNode"] = False


def _no_device(value):
    del value["slots"]["3"]["device"]


def _untyped_device(value):
    del value["slots"]["3"]["device"]["type"]


def _non_text_reason(value):
    value["gpuTopology"]["unknownReason"] = None


def _non_list_gpus(value):
    value["gpuTopology"]["gpus"] = {}


def _extra_entry(value):
    value["gpuTopology"]["gpus"].append(dict(value["gpuTopology"]["gpus"][0], deviceId=9))


def _redacted(value):
    del value["gpuTopology"]
    for slot in value["slots"].values():
        slot["device"]["id"] = -1
        slot["device"]["uuid"] = "********"


HIDDEN_CASES = [_null_topology, _absent_topology, _redacted]
MALFORMED_CASES = [
    _missing_entry, _duplicate_device, _text_numa_node, _bool_numa_node, _no_device,
    _untyped_device, _non_text_reason, _non_list_gpus, _extra_entry,
]


@pytest.mark.parametrize("change", HIDDEN_CASES + MALFORMED_CASES)
def test_unreadable_topology_is_unknown_never_an_exception(change):
    # T7 and T7b at n=4
    value = numa_agent()
    change(value)
    inspector = strong_inspector([value])

    error = refused(inspector, strong_config(4))
    assert error.code == "capacity_unknown"
    assert error.retryable is False
    assert error.details["available"] is None
    cause = (
        "is not visible to this account" if change in HIDDEN_CASES
        else "is inconsistent with its slots"
    )
    assert str(error) == (
        f"capacity for resource pool 'gpu' is unknown: GPU topology of agent agent-1 {cause}"
    )
    selected = inspector.resources(4, "gpu", "strong")["selected_pool"]
    assert selected["available"] is None
    assert selected["available_capacity"] is None
    assert selected["max_numa_node_free_slots"] is None
    assert selected["max_numa_node_slots"] is None


@pytest.mark.parametrize("change", HIDDEN_CASES + MALFORMED_CASES)
def test_a_request_larger_than_every_agent_is_refused_even_with_unreadable_topology(change):
    # T7b at n=9: the slot count survives redaction.
    value = numa_agent()
    change(value)
    inspector = strong_inspector([value])

    error = refused(inspector, strong_config(9))
    assert error.code == "capacity_unavailable"
    assert error.retryable is False
    assert str(error) == no_node_holds(9)
    assert error.details["available"] is None


def test_a_free_gpu_without_a_known_numa_node_does_not_count():
    # T8: node 0 holds 3 free slots plus one free slot with no known node; node 1 is busy.
    value = numa_agent(nodes=((0, 1, 2), (4, 5, 6, 7), {"ids": [3], "node": -1}),
                       busy={4, 5, 6, 7})
    error = refused(strong_inspector([value]), strong_config(4))
    assert error.code == "capacity_unavailable"
    assert error.retryable is True
    assert str(error) == wait_message(4, 3)


def test_gpus_with_no_numa_node_form_no_node():
    # Every GPU reports numaNode -1 with an empty unknownReason: no node holds a slot.
    inspector = strong_inspector([numa_agent(nodes=({"ids": range(8), "node": -1},))])
    error = refused(inspector, strong_config(4))
    assert error.code == "capacity_unavailable"
    assert error.retryable is False
    assert str(error) == NO_NODES
    selected = inspector.resources(4, "gpu", "strong")["selected_pool"]
    assert selected["max_numa_node_free_slots"] == 0
    assert selected["max_numa_node_slots"] == 0


def test_an_excluded_gpu_is_not_a_slot():
    # T9: a 7-slot agent whose topology also lists the excluded GPU.
    value = numa_agent(nodes=((0, 1, 2, 3), (5, 6, 7)), busy={5, 6, 7})
    value["gpuTopology"]["gpus"].append({
        "deviceId": -1, "excluded": True, "numaNode": 1, "health": "GPU_HEALTH_LINK_BELOW_MAX",
        "nvmlError": "", "uuid": "GPU-excluded",
    })
    inspector = strong_inspector([value])

    admitted = inspector.require_capacity("command", strong_config(4))
    assert admitted["selected_pool"]["available_capacity"] == 4
    assert admitted["selected_pool"]["total_slots"] == 7


@pytest.mark.parametrize("state", [{"enabled": False}, {"draining": True}])
def test_a_disabled_or_draining_agent_has_no_free_numa_slots(state):
    # T10: its slots still count toward the node, so the request waits.
    value = numa_agent(**state)
    inspector = ResourceInspector(Client([pool(total=0, agents=1)], [value]))

    error = refused(inspector, strong_config(4))
    assert error.code == "capacity_unavailable"
    assert error.retryable is True
    assert str(error) == wait_message(4, 0)
    selected = inspector.resources(4, "gpu", "strong")["selected_pool"]
    assert selected["max_numa_node_free_slots"] == 0
    assert selected["max_numa_node_slots"] == 4


def test_a_disabled_slot_counts_toward_its_node_but_is_not_free():
    # T11
    value = numa_agent(busy={4, 5, 6, 7})
    value["slots"]["2"]["enabled"] = False
    inspector = ResourceInspector(Client([pool(total=7, used=4)], [value]))

    error = refused(inspector, strong_config(4))
    assert error.code == "capacity_unavailable"
    assert error.retryable is True
    assert str(error) == wait_message(4, 3)


def test_a_draining_slot_counts_toward_its_node_but_is_not_free():
    # T11 variant: idle slots 0-3 on node 0 are draining; node 1 is busy.
    value = numa_agent(busy={4, 5, 6, 7})
    for index in range(4):
        value["slots"][str(index)]["draining"] = True
    inspector = ResourceInspector(Client([pool(total=4, used=4)], [value]))

    error = refused(inspector, strong_config(4))
    assert error.code == "capacity_unavailable"
    assert error.retryable is True
    assert str(error) == wait_message(4, 0)
    selected = inspector.resources(4, "gpu", "strong")["selected_pool"]
    assert selected["max_numa_node_free_slots"] == 0
    assert selected["max_numa_node_slots"] == 4


@pytest.mark.parametrize("fields", [
    {"health": "GPU_HEALTH_LINK_BELOW_MAX"}, {"nvmlError": "GPU is lost"},
])
def test_gpu_health_does_not_change_the_fit(fields):
    # T12
    value = numa_agent(busy={4, 5, 6, 7})
    for entry in value["gpuTopology"]["gpus"]:
        entry.update(fields)

    assert strong_inspector([value]).require_capacity("command", strong_config(4))["admitted"]


def test_strong_uses_the_agent_whose_node_holds_the_request():
    # T13
    agents = [numa_agent("a", busy={0, 1, 4, 5}), numa_agent("b", busy={0, 1, 2, 3})]
    admitted = strong_inspector(agents).require_capacity("command", strong_config(4))
    assert admitted["admitted"] is True
    assert admitted["selected_pool"]["max_numa_node_free_slots"] == 4


@pytest.mark.parametrize("extra", [{}, {"is_single_node": False}, {"is_single_node": None}])
def test_a_strong_experiment_always_uses_one_agent(extra):
    # T14: two idle 4-slot agents (2 + 2 each) never hold 8 on one node.
    agents = [numa_agent("a", nodes=((0, 1), (2, 3))), numa_agent("b", nodes=((0, 1), (2, 3)))]
    inspector = strong_inspector(agents)

    error = refused(inspector, strong_config(8, "experiment", **extra), "experiment")
    assert error.code == "capacity_unavailable"
    assert error.retryable is False
    assert str(error) == no_node_holds(8)
    # Without strong the experiment may span agents, which is not checked.
    plain = refused(inspector, experiment_config(8, **extra), "experiment")
    assert plain.code == "capacity_unknown"
    assert str(plain) == SPAN_UNKNOWN

    # A strong experiment that fits on no single node now waits, as a single-agent request
    # does, and never gets the may-span-agents answer.
    agents = [numa_agent("a", busy={0, 1, 4, 5}), numa_agent("b", busy={2, 3, 6, 7})]
    inspector = strong_inspector(agents)
    error = refused(inspector, strong_config(4, "experiment", **extra), "experiment")
    assert error.code == "capacity_unavailable"
    assert error.retryable is True
    assert str(error) == wait_message(4, 2)
    assert inspector.require_capacity("experiment", experiment_config(4, **extra))["admitted"]


def cpu_agent(name, slots=4):
    value = agent(name, slots=slots)
    for index, slot in value["slots"].items():
        slot["device"] = {"id": int(index), "type": "TYPE_CPU", "uuid": ""}
    value["gpuTopology"] = None
    return value


def test_cpu_agents_have_no_numa_nodes():
    # T15
    inspector = strong_inspector([cpu_agent("a"), cpu_agent("b")])

    error = refused(inspector, strong_config(2))
    assert error.code == "capacity_unavailable"
    assert error.retryable is False
    assert str(error) == NO_NODES
    explanation = inspector.resources(2, "gpu", "strong")["selected_pool"]["explanation"]
    assert "agent a has no CUDA slots" in explanation


@pytest.mark.parametrize("slots, retryable, message", [
    (5, False, no_node_holds(5)),
    (4, True, wait_message(4, 0)),
])
def test_cpu_agents_next_to_a_known_cuda_agent(slots, retryable, message):
    # T15b: everything is busy.
    cpu = cpu_agent("cpu", slots=8)
    for slot in cpu["slots"].values():
        slot["container"] = {"id": "c"}
    agents = [cpu, numa_agent("gpu-agent", busy=set(range(8)))]

    error = refused(strong_inspector(agents), strong_config(slots))
    assert error.code == "capacity_unavailable"
    assert error.retryable is retryable
    assert str(error) == message


def test_a_static_pool_without_agents_refuses_strong():
    # T16
    inspector = ResourceInspector(Client([pool(total=0, agents=0)], []))

    error = refused(inspector, strong_config(2))
    assert error.code == "capacity_unavailable"
    assert error.retryable is False
    assert str(error) == NO_NODES
    assert error.details["available"] == 0


@pytest.mark.parametrize("pool_type", ["RESOURCE_POOL_TYPE_AWS", None])
def test_a_pool_that_is_not_static_never_refuses_strong(pool_type):
    # T16: provisioned pools check the instance size themselves.
    empty = pool(total=0, agents=0, type=pool_type)
    if pool_type is None:
        del empty["type"]
    error = refused(ResourceInspector(Client([empty], [])), strong_config(2))
    assert error.code == "capacity_unavailable"
    assert error.retryable is True
    assert str(error) == wait_message(2, 0)

    agents = [numa_agent()]
    error = refused(strong_inspector(agents, type=pool_type), strong_config(5))
    assert error.retryable is True
    assert str(error) == wait_message(5, 4)


@pytest.mark.parametrize("change", [_redacted, _duplicate_device])
def test_an_unreadable_agent_makes_strong_unknown_even_when_another_fits(change):
    # T17
    other = numa_agent("other")
    change(other)
    agents = [numa_agent("fits"), other]
    inspector = strong_inspector(agents)

    error = refused(inspector, strong_config(4))
    assert error.code == "capacity_unknown"
    assert "GPU topology of agent other" in str(error)
    selected = inspector.resources(4, "gpu", "strong")["selected_pool"]
    assert selected["max_numa_node_free_slots"] is None
    assert selected["max_numa_node_slots"] is None
    report = inspector.resources(4, None, "strong")
    assert report["candidate_pools"] == []
    assert report["alternatives"] == []


@pytest.mark.parametrize("used", [3, 5])
def test_a_used_slot_mismatch_is_unknown_for_strong_after_the_slot_count_check(used):
    # T18: n=4 is unknown; n=9 is refused from slot counts first.
    agents = [numa_agent(busy={4, 5, 6, 7})]
    inspector = strong_inspector(agents, used=used)

    error = refused(inspector, strong_config(4))
    assert error.code == "capacity_unknown"
    assert str(error) == (
        f"capacity for resource pool 'gpu' is unknown: used slots ({used}) differ from slots "
        "holding containers (4); a task may be starting or stopping"
    )
    error = refused(inspector, strong_config(9))
    assert error.code == "capacity_unavailable"
    assert error.retryable is False
    assert str(error) == no_node_holds(9)
    selected = inspector.resources(9, "gpu", "strong")["selected_pool"]
    assert selected["available"] is False
    assert selected["max_numa_node_free_slots"] is None
    assert selected["max_numa_node_slots"] is None


def test_incomplete_agent_data_is_unknown_for_strong_without_a_refusal():
    agents = [numa_agent()]
    inspector = ResourceInspector(Client([pool(total=7)], agents))

    error = refused(inspector, strong_config(9))
    assert error.code == "capacity_unknown"
    assert "pool reports 7 slot(s) on 1 agent(s)" in str(error)


NUMA_FIELDS = {"max_numa_node_free_slots", "max_numa_node_slots"}


@pytest.mark.parametrize("slots, preference", [
    (0, "strong"), (1, "strong"), (2, None), (2, False), (2, "soft"), (4, "soft"),
])
def test_resources_without_strong_from_two_slots_match_no_preference(slots, preference):
    # T19
    agents = [numa_agent(busy={0, 1, 4, 5})]
    inspector = strong_inspector(agents)

    report = inspector.resources(slots, "gpu", preference)
    plain = inspector.resources(slots, "gpu")
    assert report["prefer_gpu_topology"] == preference
    for item in (report, plain):
        item.pop("observed_at")
        item.pop("prefer_gpu_topology")
    assert report == plain
    assert not NUMA_FIELDS & set(report["selected_pool"])
    assert report["selected_pool"]["capacity_kind"] != "slots_one_numa_node"


def test_resources_with_strong_report_both_numa_maxima():
    # T20: agent a has 2 + 1 free, agent b has 3 + 0 free on 4 + 4 slots.
    agents = [numa_agent("a", busy={0, 1, 5, 6, 7}), numa_agent("b", busy={3, 4, 5, 6, 7})]
    inspector = strong_inspector(agents, name="gpu")

    report = inspector.resources(2, "gpu", "strong")
    selected = report["selected_pool"]
    assert report["prefer_gpu_topology"] == "strong"
    assert report["available"] is True
    assert selected["capacity_kind"] == "slots_one_numa_node"
    assert selected["max_numa_node_free_slots"] == 3
    assert selected["max_numa_node_slots"] == 4
    assert selected["available_capacity"] == 3
    assert selected["explanation"] == (
        "3 GPU(s) are currently free on one NUMA node of one schedulable agent; 2 requested"
    )
    assert inspector.resources(4, "gpu", "strong")["available"] is False


@pytest.mark.parametrize("value", [True, "Soft", "off", 1, 0, {}])
def test_resources_reject_other_preferences(value):
    inspector = strong_inspector([numa_agent()])
    with pytest.raises(ValueError, match="prefer_gpu_topology must be"):
        inspector.resources(2, "gpu", value)
    with pytest.raises(ValueError, match="prefer_gpu_topology must be"):
        inspector.require_capacity("command", command_config(slots=2, prefer_gpu_topology=value))


SOFT_CASES = [
    # (pools, agents, kind, config) with the verdict compared against no preference
    ([pool(total=4, used=4)], [agent(occupied=4)], "command", command_config(slots=2)),
    ([pool(total=16, agents=2)], [agent("a", slots=8), agent("b", slots=8)],
     "experiment", experiment_config(8)),
    ([pool(total=16, agents=2)], [agent("a", slots=8), agent("b", slots=8)],
     "experiment", experiment_config(16)),
    ([pool(total=16, agents=2)], [agent("a", slots=8), agent("b", slots=8)],
     "experiment", experiment_config(16, is_single_node=True)),
    ([pool(total=4, used=2)], [agent(slots=4, occupied=1)], "command", command_config(slots=2)),
    ([pool(total=7, agents=2)], [agent("a", slots=8)], "command", command_config(slots=2)),
    ([pool(total=8)], [numa_agent(busy={0, 1, 4, 5})], "command", command_config(slots=4)),
    ([pool(total=8)], [numa_agent(busy={0, 1, 4, 5})], "shell", command_config(slots=5)),
]


def _verdict(inspector, kind, config):
    try:
        result = inspector.require_capacity(kind, config)
    except APIError as exc:
        return exc.code, exc.retryable, str(exc), exc.details
    result.pop("observed_at")
    result.pop("prefer_gpu_topology")
    return result


@pytest.mark.parametrize("pools, agents, kind, config", SOFT_CASES)
def test_soft_is_admitted_exactly_like_no_preference(pools, agents, kind, config):
    # T21
    soft = copy.deepcopy(config)
    soft["resources"]["prefer_gpu_topology"] = "soft"
    inspector = ResourceInspector(Client(pools, agents))

    assert _verdict(inspector, kind, soft) == _verdict(inspector, kind, config)


def test_every_strong_call_reads_agents_then_pools_exactly_once():
    # T22
    client = Client([numa_pool([numa_agent()])], [numa_agent()])
    inspector = ResourceInspector(client)
    expected = [("api/v1/agents", {"limit": 0}), ("api/v1/resource-pools", {"limit": 0})]

    inspector.resources(2, "gpu", "strong")
    assert client.calls == expected
    for slots in (2, 5, 9):
        client.calls.clear()
        try:
            inspector.require_capacity("command", strong_config(slots))
        except APIError:
            pass
        assert client.calls == expected
