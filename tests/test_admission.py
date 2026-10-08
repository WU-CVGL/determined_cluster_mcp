from __future__ import annotations

import pytest

from determined_compute.compute.admission import ResourceInspector
from determined_compute.core.api_client import APIError


def pool(name="gpu", *, total=4, used=0, agents=1, aux=8, aux_running=0):
    value = {
        "name": name,
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
        ("api/v1/resource-pools", {"limit": 0}),
        ("api/v1/agents", {"limit": 0}),
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


def test_fragmented_multigpu_is_rejected_by_default_but_explicit_multinode_fits():
    pools = [pool(total=4, used=2, agents=2)]
    agents = [
        agent("a", slots=2, occupied=1),
        agent("b", slots=2, occupied=1),
    ]
    inspector = ResourceInspector(Client(pools, agents))
    experiment = {
        "resources": {"slots_per_trial": 2, "resource_pool": "gpu"}
    }

    with pytest.raises(APIError) as caught:
        inspector.require_capacity("experiment", experiment)
    assert caught.value.code == "capacity_unavailable"
    assert caught.value.details["available"] == 1

    experiment["resources"]["is_single_node"] = False
    admitted = inspector.require_capacity("experiment", experiment)
    assert admitted["selected_pool"]["available_capacity"] == 2


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

    # Under an agent drain it is not counted either.
    drained = agent(slots=8, occupied=2, enabled=False, draining=True)
    set_slot(drained, 0, enabled=False)
    report = ResourceInspector(Client([pool(total=1, used=1)], [drained])).resources(pool="gpu")
    assert report["selected_pool"]["available"] is False
    assert report["selected_pool"]["available_capacity"] == 0


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
