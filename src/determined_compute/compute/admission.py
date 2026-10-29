"""Read-only resource inspection and conservative launch admission.

The result is a point-in-time advisory, not a reservation.  Admission never
changes the requested resource pool and never treats utilization as capacity.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from determined_compute.compute.gpu_groups import (
    describe_gpu_groups,
    holds_gpu_groups,
    normalize_gpu_groups,
)
from determined_compute.core.api_client import APIError


_STATISTICS_MISSING = "complete per-agent slot capacity statistics are missing"
# The values of prefer_gpu_topology the fork accepts; false and null are off.
_GPU_TOPOLOGY_PREFERENCES = ("soft", "strong")
# The fork's unknown reason of an agent that has not reported its GPU topology since the master
# started (fork 0.42.0). The master lets a "strong" task wait for such an agent.
_TOPOLOGY_PENDING = "not reported since the master started"
_STATIC_POOL = "RESOURCE_POOL_TYPE_STATIC"
_CUDA = "TYPE_CUDA"
# Device types whose slots are GPUs, as the agent detects them (CPU slots carry a CPU brand).
_GPU_TYPES = (_CUDA, "TYPE_ROCM")
# Classes of an agent's GPU topology for a "strong" request.
_KNOWN, _NONE, _PENDING, _HIDDEN, _MALFORMED = "known", "none", "pending", "hidden", "malformed"


def _observed_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _nonnegative_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _slots(value: Any) -> Optional[List[Mapping[str, Any]]]:
    if isinstance(value, dict):
        entries = list(value.values())
    elif isinstance(value, list):
        entries = value
    else:
        return None
    if not all(isinstance(item, Mapping) for item in entries):
        return None
    return entries


def _pool_description(value: Any) -> Optional[str]:
    """The pool's operator-written description, as compute_usage reports it."""

    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:4096] if text else None


def _gpu_models(
    members: List[Tuple[str, Mapping[str, Any], List[Mapping[str, Any]]]]
) -> Optional[List[str]]:
    """Return the sorted, distinct model names of the GPU slots on a pool's agents.

    Every slot of every agent counts, whatever its state. The list is empty only when no slot
    is a GPU, and None when a slot's device type, or a GPU slot's model name, is unreadable.
    The caller passes the agents of a pool that has some; a pool without agents is unknown.
    """

    models = set()
    for _agent_id, _agent, entries in members:
        for slot in entries:
            device = slot.get("device")
            if not isinstance(device, Mapping) or not isinstance(device.get("type"), str):
                return None
            if device["type"] not in _GPU_TYPES:
                continue
            brand = device.get("brand")
            if not isinstance(brand, str) or not brand:
                return None
            models.add(brand[:256])
    return sorted(models)


def _valid_preference(value: Any) -> bool:
    return value is None or value is False or (
        isinstance(value, str) and value in _GPU_TOPOLOGY_PREFERENCES
    )


# Why a "strong" request cannot run in a pool. "use soft" is offered only when an agent has the
# slots but no NUMA node does.
def _no_agent_holds(pool: str, slots: int) -> str:
    return f"no agent in pool {pool} has {slots} slots; request fewer slots or use another pool"


def _no_numa_nodes(pool: str) -> str:
    return f"no agent in pool {pool} reports NUMA nodes; use soft"


def _no_numa_node_holds(pool: str, slots: int) -> str:
    return f"no NUMA node in pool {pool} has {slots} slots; use soft"


def _slot_free(agent: Mapping[str, Any], slot: Mapping[str, Any]) -> bool:
    """Whether a slot takes new work now: enabled, not draining, and holding no container, on an
    enabled agent that is not draining."""

    return (
        agent.get("enabled", True)
        and not agent.get("draining", False)
        and slot.get("enabled", True)
        and not slot.get("draining", False)
        and slot.get("container") is None
    )


def _numa_devices(
    agent: Mapping[str, Any], entries: List[Mapping[str, Any]]
) -> Tuple[str, List[Tuple[Mapping[str, Any], int]], str]:
    """Classify an agent's GPU topology and give each CUDA slot its NUMA node.

    Returns the class, the CUDA slots with their nodes (for the known class only), and the
    agent's unknown reason. A node below 0 means the topology gives the slot no node, as the
    scheduler's numaNodeOf reads it.
    """

    cuda: List[Mapping[str, Any]] = []
    for slot in entries:
        device = slot.get("device")
        if not isinstance(device, Mapping) or not isinstance(device.get("type"), str):
            return _MALFORMED, [], ""
        if device["type"] == _CUDA:
            cuda.append(slot)
    if not cuda:
        return _NONE, [], ""
    topology = agent.get("gpuTopology")
    ids = [_nonnegative_int(slot["device"].get("id")) for slot in cuda]
    if not isinstance(topology, Mapping) or any(item is None for item in ids):
        # Without the sensitive-agent permission the master drops the topology and sets every
        # device id to -1.
        return _HIDDEN, [], ""
    if len(set(ids)) != len(ids):
        return _MALFORMED, [], ""
    reason = topology.get("unknownReason")
    if not isinstance(reason, str):
        return _MALFORMED, [], ""
    if reason == _TOPOLOGY_PENDING:
        return _PENDING, [], reason
    if reason:
        return _NONE, [], reason
    gpus = topology.get("gpus")
    if not isinstance(gpus, list):
        return _MALFORMED, [], ""
    node_of: Dict[int, int] = {}
    for entry in gpus:
        if not isinstance(entry, Mapping):
            return _MALFORMED, [], ""
        excluded = entry.get("excluded", False)
        if not isinstance(excluded, bool):
            return _MALFORMED, [], ""
        if excluded:
            # An excluded GPU is never a slot.
            continue
        device_id = _nonnegative_int(entry.get("deviceId"))
        node = entry.get("numaNode")
        if (
            device_id is None
            or device_id in node_of
            or isinstance(node, bool)
            or not isinstance(node, int)
        ):
            return _MALFORMED, [], ""
        node_of[device_id] = node
    if set(node_of) != set(ids):
        return _MALFORMED, [], ""
    return _KNOWN, [(slot, node_of[device_id]) for slot, device_id in zip(cuda, ids)], ""


def _numa_layout(
    agent: Mapping[str, Any], entries: List[Mapping[str, Any]]
) -> Tuple[str, Dict[int, int], Dict[int, int], str]:
    """Classify an agent's GPU topology and count its CUDA slots by NUMA node.

    Returns the class, the slots per node in any state, the free slots per node, and the
    agent's unknown reason. A slot counts toward a node only when the topology gives it a node
    of 0 or more; a slot is free as _slot_free says.
    """

    cls, devices, reason = _numa_devices(agent, entries)
    total: Dict[int, int] = {}
    free: Dict[int, int] = {}
    for slot, node in devices:
        if node < 0:
            continue
        total[node] = total.get(node, 0) + 1
        if _slot_free(agent, slot):
            free[node] = free.get(node, 0) + 1
    return cls, total, free, reason


def _gpu_slots(entries: List[Mapping[str, Any]]) -> Optional[List[Tuple[Mapping[str, Any], str]]]:
    """Return the agent's GPU slots (CUDA or ROCm) with their model names, or None when a slot's
    device type or a GPU's model name is unreadable."""

    out = []
    for slot in entries:
        device = slot.get("device")
        if not isinstance(device, Mapping) or not isinstance(device.get("type"), str):
            return None
        if device["type"] not in _GPU_TYPES:
            continue
        brand = device.get("brand")
        if not isinstance(brand, str) or not brand:
            return None
        out.append((slot, brand))
    return out


def _free_gpus_by_model(
    members: List[Tuple[str, Mapping[str, Any], List[Mapping[str, Any]]]]
) -> Optional[Dict[str, Dict[str, int]]]:
    """Each agent's free GPUs by model, or None when a GPU's model is unreadable."""

    result: Dict[str, Dict[str, int]] = {}
    for agent_id, agent, entries in members:
        gpus = _gpu_slots(entries)
        if gpus is None:
            return None
        counts: Dict[str, int] = {}
        for slot, brand in gpus:
            if _slot_free(agent, slot):
                counts[brand[:256]] = counts.get(brand[:256], 0) + 1
        result[agent_id] = counts
    return result


def _fulfillable(
    raw: Mapping[str, Any],
    members: List[Tuple[str, Mapping[str, Any], List[Mapping[str, Any]]]],
    *,
    complete: bool,
    slots: int,
) -> Optional[bool]:
    """Whether one agent of the pool can ever have the slots, as the master decides at submit
    (ValidateResources): from a positive slots per instance (slotsPerAgent) alone, even without
    agents; otherwise, for a static pool whose agent list matches it, from its largest agent,
    every slot counted. None when this cannot be told."""

    per_instance = _nonnegative_int(raw.get("slotsPerAgent"))
    if per_instance:
        return per_instance >= slots
    if complete and raw.get("type") == _STATIC_POOL:
        return max((len(entries) for _id, _agent, entries in members), default=0) >= slots
    return None


def _no_agent_has_gpus_for(pool: str, groups: List[Mapping[str, Any]]) -> str:
    """The master's refusal of gpu_groups that no agent of the pool can hold
    (errGPUGroupsNoAgent)."""

    return f"no agent in pool {pool} has GPUs for gpu_groups {describe_gpu_groups(groups)}"


class ResourceInspector:
    """Inspect current capacity through the Determined read-only APIs."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def resources(
        self,
        slots: int = 1,
        pool: Optional[str] = None,
        prefer_gpu_topology: Any = None,
        gpu_groups: Any = None,
    ) -> Dict[str, Any]:
        """Return conservative, per-pool capacity for a request on one agent."""

        self._validate_request(slots, pool)
        if not _valid_preference(prefer_gpu_topology):
            raise ValueError('prefer_gpu_topology must be "soft", "strong", false, or null')
        groups = normalize_gpu_groups(gpu_groups, slots)
        report, _refusals = self._resources(
            slots=slots, pool=pool, preference=prefer_gpu_topology, groups=groups
        )
        return report

    def _resources(
        self,
        *,
        slots: int,
        pool: Optional[str],
        preference: Any,
        groups: Optional[List[Mapping[str, Any]]] = None,
        single_node: bool = True,
    ) -> Tuple[Dict[str, Any], Dict[str, str]]:
        """Return the report and, by pool, why the master refuses the request."""

        # "strong" takes effect from 2 slots, as in the scheduler's strongTopology.
        strong = preference == "strong" and slots >= 2
        # Agents first: a slot taken before or between the two reads then shows as a
        # used-slot mismatch, not as a free slot.
        agents_value = self.client._get("api/v1/agents", params={"limit": 0})
        pools_value = self.client._get("api/v1/resource-pools", params={"limit": 0})
        pools = pools_value.get("resourcePools") if isinstance(pools_value, Mapping) else None
        agents = agents_value.get("agents") if isinstance(agents_value, Mapping) else None
        if not isinstance(pools, list) or not isinstance(agents, list):
            raise APIError(
                "cluster capacity statistics are unavailable",
                code="capacity_unknown",
                details={
                    "resource_pool": pool,
                    "requested_slots": slots,
                    "available": None,
                    "candidate_pools": [],
                },
            )

        assessments = []
        refusals: Dict[str, str] = {}
        for item in pools:
            if isinstance(item, Mapping) and isinstance(item.get("name"), str):
                assessment, refusal = self._assess_pool(
                    item, agents, slots=slots, strong=strong, groups=groups,
                    single_node=single_node,
                )
                assessments.append(assessment)
                if refusal is not None:
                    refusals[assessment["resource_pool"]] = refusal
        candidate_names = sorted(
            item["resource_pool"] for item in assessments if item["available"] is True
        )
        selected = next(
            (item for item in assessments if item["resource_pool"] == pool), None
        )
        if pool is None:
            available: Optional[bool]
            if candidate_names:
                available = True
            elif any(item["available"] is None for item in assessments):
                available = None
            else:
                available = False
        else:
            available = selected["available"] if selected is not None else None

        alternatives = [
            {
                "resource_pool": item["resource_pool"],
                "available_capacity": item["available_capacity"],
                "capacity_kind": item["capacity_kind"],
            }
            for item in assessments
            if item["available"] is True and item["resource_pool"] != pool
        ]
        if pool is not None and selected is None:
            explanation = f"resource pool {pool!r} is not present or not available to you"
        elif pool is None:
            explanation = "capacity is reported for inventory only; no resource pool was selected"
        else:
            explanation = selected["explanation"]
        return {
            "observed_at": _observed_at(),
            "requested_slots": slots,
            "requested_pool": pool,
            "prefer_gpu_topology": preference,
            "gpu_groups": groups,
            "single_node": single_node,
            "available": available,
            "selected_pool": selected,
            "pools": assessments,
            "candidate_pools": candidate_names,
            "alternatives": alternatives,
            "explanation": explanation,
            "advisory": (
                "Point-in-time scheduler capacity only; this is not a reservation. "
                "Alternative pools are suggestions and image/workload compatibility "
                "was not checked."
            ),
        }, refusals

    @staticmethod
    def _validate_request(slots: Any, pool: Any) -> None:
        if isinstance(slots, bool) or not isinstance(slots, int) or slots < 0:
            raise ValueError("slots must be a non-negative integer")
        if pool is not None and (not isinstance(pool, str) or not pool):
            raise ValueError("pool must be a non-empty string or null")

    def _assess_pool(
        self,
        raw: Mapping[str, Any],
        agents: List[Any],
        *,
        slots: int,
        strong: bool = False,
        groups: Optional[List[Mapping[str, Any]]] = None,
        single_node: bool = True,
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        """Assess one pool; also return why the master refuses the request, if it does.

        ``single_node`` says the request uses one agent, as commands and shells always do and
        experiments and generic tasks do with is_single_node: true; the master then refuses it
        at submit when no agent of the pool can ever have the slots. "strong" and gpu_groups
        always use one agent.
        """

        name = str(raw["name"])
        total = _nonnegative_int(raw.get("slotsAvailable"))
        used = _nonnegative_int(raw.get("slotsUsed"))
        num_agents = _nonnegative_int(raw.get("numAgents"))
        slot_type = raw.get("slotType")
        aux_capacity = _nonnegative_int(raw.get("auxContainerCapacity"))
        aux_running = _nonnegative_int(raw.get("auxContainersRunning"))
        per_agent, holding, members, agent_problem = self._agent_free(
            name, agents, num_agents, total
        )
        agent_data_known = agent_problem is None
        refusal: Optional[str] = None
        numa: Dict[str, Any] = {}

        if slots == 0:
            capacity_kind = "aux_containers"
            known = (
                aux_capacity is not None
                and aux_running is not None
                and aux_running <= aux_capacity
            )
            available_capacity = (
                max(0, aux_capacity - aux_running) if known else None
            )
            available = available_capacity > 0 if available_capacity is not None else None
            explanation = (
                f"{available_capacity} auxiliary-container position(s) are free"
                if available_capacity is not None
                else "auxiliary-container capacity statistics are missing"
            )
        else:
            capacity_kind = "slots_single_agent"
            known = (
                agent_data_known
                and used is not None
                and total is not None
                and used <= total
                and isinstance(slot_type, str)
                and bool(slot_type)
            )
            complete = known
            problem = agent_problem
            if known and used != holding:
                # The pool's used count is the slots holding containers (numUsedSlots).
                known = False
                problem = (
                    f"used slots ({used}) differ from slots holding containers ({holding}); "
                    "a task may be starting or stopping"
                )
            fulfillable = _fulfillable(raw, members, complete=complete, slots=slots)
            if groups is not None:
                capacity_kind = "gpu_groups_one_numa_node" if strong else "gpu_groups_single_agent"
                available, available_capacity, explanation, refusal = self._assess_groups(
                    name,
                    fulfillable=fulfillable,
                    members=members,
                    groups=groups,
                    slots=slots,
                    strong=strong,
                    complete=complete,
                    known=known,
                    problem=problem or _STATISTICS_MISSING,
                )
            elif strong:
                capacity_kind = "slots_one_numa_node"
                available, available_capacity, explanation, refusal, numa = self._assess_strong(
                    name,
                    fulfillable=fulfillable,
                    per_instance=bool(_nonnegative_int(raw.get("slotsPerAgent"))),
                    members=members,
                    slots=slots,
                    complete=complete,
                    known=known,
                    problem=problem or _STATISTICS_MISSING,
                )
            elif single_node and fulfillable is False:
                refusal = _no_agent_holds(name, slots)
                available = False
                available_capacity = max(per_agent.values(), default=0) if complete else None
                explanation = refusal
            elif known:
                available_capacity = max(per_agent.values(), default=0)
                available = available_capacity >= slots
                explanation = (
                    f"{available_capacity} slot(s) are currently free across one "
                    f"schedulable agent; {slots} requested"
                )
            else:
                available_capacity = None
                available = None
                explanation = problem or _STATISTICS_MISSING

        # Since fork 0.42.1 the pool list also holds pools the account may not use, with usable
        # false; older masters leave them out. Such a pool is never capacity for this account.
        usable = raw.get("usable") is not False
        if not usable:
            available = False
            explanation = f"this account may not use resource pool {name}: the pool is restricted"
        return {
            "resource_pool": name,
            "usable": usable,
            "description": _pool_description(raw.get("description")),
            # Read from the agents only when they match the pool, so a partial list never
            # passes for the pool's models, and unknown for a pool without agents, whose
            # GPUs nothing reports.
            "gpu_models": _gpu_models(members) if agent_data_known and members else None,
            "slot_type": slot_type,
            "num_agents": num_agents,
            "total_slots": total,
            "used_slots": used,
            "aggregate_reported_free_slots": (
                max(0, total - used) if total is not None and used is not None else None
            ),
            "per_agent_free_slots": per_agent if agent_data_known else None,
            # Free GPUs by model on each agent, for jobs that need certain models (gpu_groups).
            "per_agent_free_gpus_by_model": (
                _free_gpus_by_model(members) if agent_data_known else None
            ),
            "max_single_agent_free_slots": (
                max(per_agent.values(), default=0) if agent_data_known else None
            ),
            "aux_container_capacity": aux_capacity,
            "aux_containers_running": aux_running,
            "aux_containers_free": (
                max(0, aux_capacity - aux_running)
                if aux_capacity is not None and aux_running is not None
                else None
            ),
            "capacity_kind": capacity_kind,
            "available_capacity": available_capacity,
            "available": available,
            "explanation": explanation,
            **numa,
        }, refusal

    @staticmethod
    def _assess_strong(
        name: str,
        *,
        fulfillable: Optional[bool],
        per_instance: bool = False,
        members: List[Tuple[str, Mapping[str, Any], List[Mapping[str, Any]]]],
        slots: int,
        complete: bool,
        known: bool,
        problem: str,
    ) -> Tuple[Optional[bool], Optional[int], str, Optional[str], Dict[str, Any]]:
        """Assess a "strong" request: all slots on one NUMA node of one agent.

        ``complete`` says the agent list matches the pool, and ``known`` that the used slots
        match as well. Returns the verdict, the largest "strong" task that fits now, the
        explanation, why the master refuses the request (or None), and the pool's NUMA fields.
        """

        refusal: Optional[str] = None
        largest = max((len(entries) for _id, _agent, entries in members), default=0)
        if fulfillable is False:
            # The master refuses a request that no agent of the pool can ever hold at submit,
            # from slot counts alone (ValidateResources, see _fulfillable), whatever the
            # topology or the slots in use. A pool whose instances are too small gets the
            # master's "no NUMA node holds" reason, and a static pool without agents its
            # "no NUMA nodes" reason.
            if per_instance:
                refusal = _no_numa_node_holds(name, slots)
            else:
                refusal = _no_agent_holds(name, slots) if members else _no_numa_nodes(name)

        layouts = [
            (agent_id, *_numa_layout(agent, entries)) for agent_id, agent, entries in members
        ] if complete else []
        unreadable = next(
            (item for item in layouts if item[1] in (_HIDDEN, _MALFORMED)), None
        )
        pending = any(item[1] == _PENDING for item in layouts)
        readable = known and unreadable is None
        known_layouts = [item for item in layouts if item[1] == _KNOWN]
        max_free = max(
            (max(free.values(), default=0) for _id, _cls, _total, free, _r in known_layouts),
            default=0,
        )
        max_total = max(
            (max(total.values(), default=0) for _id, _cls, total, _free, _r in known_layouts),
            default=0,
        )
        numa = {
            "max_numa_node_free_slots": max_free if readable else None,
            "max_numa_node_slots": max_total if readable and not pending else None,
        }
        notes = []
        for agent_id, cls, _total, _free, reason in layouts:
            if cls == _PENDING:
                notes.append(f"agent {agent_id} has not reported GPU topology ({reason!r})")
            elif cls == _NONE and reason:
                notes.append(f"agent {agent_id} reports no NUMA nodes ({reason!r})")
            elif cls == _NONE:
                notes.append(f"agent {agent_id} has no CUDA slots")
        capacity = numa["max_numa_node_free_slots"]

        def explained(text: str) -> str:
            return "; ".join([text, *notes])

        if refusal is not None:
            return False, capacity, explained(refusal), refusal, numa
        if not known:
            return None, None, problem, None, numa
        if unreadable is not None:
            agent_id, cls = unreadable[0], unreadable[1]
            cause = (
                f"GPU topology of agent {agent_id} is not visible to this account"
                if cls == _HIDDEN
                else f"GPU topology of agent {agent_id} is inconsistent with its slots"
            )
            return None, None, cause, None, numa
        summary = (
            f"{max_free} GPU(s) are currently free on one NUMA node of one schedulable "
            f"agent; {slots} requested"
        )
        if max_free >= slots:
            return True, max_free, explained(summary), None, numa
        if layouts and not pending and max_total < slots:
            # The master refuses the request once every current agent of the pool, static or
            # provisioned, has reported its topology (strongCannotFit); disabled and draining
            # slots count toward a node. A pool without agents waits.
            nodes = any(total for _id, _cls, total, _free, _r in known_layouts)
            if largest < slots:
                refusal = _no_agent_holds(name, slots)
            elif nodes:
                refusal = _no_numa_node_holds(name, slots)
            else:
                refusal = _no_numa_nodes(name)
            return False, max_free, explained(refusal), refusal, numa
        return False, max_free, explained(summary), None, numa

    @staticmethod
    def _assess_groups(
        name: str,
        *,
        fulfillable: Optional[bool],
        members: List[Tuple[str, Mapping[str, Any], List[Mapping[str, Any]]]],
        groups: List[Mapping[str, Any]],
        slots: int,
        strong: bool,
        complete: bool,
        known: bool,
        problem: str,
    ) -> Tuple[Optional[bool], Optional[int], str, Optional[str]]:
        """Assess a request with gpu_groups: one agent whose free GPUs hold every group at once,
        on one known NUMA node under "strong".

        Returns the verdict, the number of agents whose free GPUs hold the groups now, the
        explanation, and why the master refuses the request (or None). The master refuses a
        pool that no agent can ever give the task's slots (_fulfillable), and a pool none of whose
        agents could hold the groups with every slot free, disabled and draining slots included,
        on one NUMA node under "strong" (gpuGroupsCannotFit). A pool without agents waits, and so
        does an agent that has not reported its GPU topology.
        """

        described = describe_gpu_groups(groups)
        on_node = " on one NUMA node" if strong else ""
        refusal: Optional[str] = None
        if fulfillable is False:
            refusal = _no_agent_has_gpus_for(name, groups)
        if refusal is not None:
            return False, None, refusal, refusal
        if not complete:
            return None, None, problem, None

        fitting = 0
        could_fit = False
        for agent_id, agent, entries in members:
            gpus = _gpu_slots(entries)
            if gpus is None:
                return None, None, f"GPU models of agent {agent_id} are unreadable", None
            if not holds_gpu_groups(groups, [brand for _slot, brand in gpus]):
                continue
            if not strong:
                could_fit = True
                free = [brand for slot, brand in gpus if _slot_free(agent, slot)]
                fitting += holds_gpu_groups(groups, free)
                continue
            cls, devices, _reason = _numa_devices(agent, entries)
            if cls == _HIDDEN:
                cause = f"GPU topology of agent {agent_id} is not visible to this account"
                return None, None, cause, None
            if cls == _MALFORMED:
                cause = f"GPU topology of agent {agent_id} is inconsistent with its slots"
                return None, None, cause, None
            if cls == _PENDING:
                # The master lets the task wait for the agent's topology.
                could_fit = True
                continue
            brand_of = {id(slot): brand for slot, brand in gpus}
            all_by_node: Dict[int, List[str]] = {}
            free_by_node: Dict[int, List[str]] = {}
            for slot, node in devices:
                if node < 0:
                    continue
                all_by_node.setdefault(node, []).append(brand_of[id(slot)])
                if _slot_free(agent, slot):
                    free_by_node.setdefault(node, []).append(brand_of[id(slot)])
            could_fit = could_fit or any(
                holds_gpu_groups(groups, models) for models in all_by_node.values()
            )
            fitting += any(holds_gpu_groups(groups, models) for models in free_by_node.values())
        if members and not could_fit:
            refusal = _no_agent_has_gpus_for(name, groups)
            return False, fitting, refusal, refusal
        if not known:
            return None, None, problem, None
        if fitting:
            return True, fitting, (
                f"{fitting} schedulable agent(s) have free GPUs for gpu_groups {described}{on_node}"
            ), None
        return False, 0, (
            f"no schedulable agent in pool {name} has free GPUs for gpu_groups {described}"
            f"{on_node} now"
        ), None

    @staticmethod
    def _agent_free(
        pool: str,
        agents: List[Any],
        expected_agents: Optional[int],
        expected_slots: Optional[int],
    ) -> Tuple[
        Dict[str, int],
        int,
        List[Tuple[str, Mapping[str, Any], List[Mapping[str, Any]]]],
        Optional[str],
    ]:
        """Return each agent's free slots, the counted slots holding a container, and the
        pool's agents with their slot entries.

        The last value says why the agent list does not match the pool, or is None.

        Slots are counted the way the pool counts them (numSlots in the scheduler's agent
        state): a slot counts when it takes new work, or when it is draining and still holds a
        container. A draining agent counts only its counted slots that hold a container, and a
        disabled agent counts none.
        """

        if expected_agents is None or expected_slots is None:
            return {}, 0, [], _STATISTICS_MISSING
        matching: List[Mapping[str, Any]] = []
        for item in agents:
            if not isinstance(item, Mapping):
                continue
            memberships = item.get("resourcePools")
            if isinstance(memberships, list) and pool in memberships:
                matching.append(item)

        result: Dict[str, int] = {}
        members: List[Tuple[str, Mapping[str, Any], List[Mapping[str, Any]]]] = []
        counted_slots = 0
        holding = 0
        for agent in matching:
            agent_id = agent.get("id")
            entries = _slots(agent.get("slots"))
            enabled = agent.get("enabled", True)
            draining = agent.get("draining", False)
            if (
                not isinstance(agent_id, str)
                or not agent_id
                or agent_id in result
                or entries is None
                or not isinstance(enabled, bool)
                or not isinstance(draining, bool)
            ):
                return {}, 0, [], _STATISTICS_MISSING
            counted = 0
            counted_busy = 0
            free = 0
            for slot in entries:
                slot_enabled = slot.get("enabled", True)
                slot_draining = slot.get("draining", False)
                if not isinstance(slot_enabled, bool) or not isinstance(slot_draining, bool):
                    return {}, 0, [], _STATISTICS_MISSING
                busy = slot.get("container") is not None
                if (slot_enabled and not slot_draining) or (slot_draining and busy):
                    counted += 1
                    counted_busy += busy
                if slot_enabled and not slot_draining and not busy:
                    free += 1
            if draining:
                counted_slots += counted_busy
            elif enabled:
                counted_slots += counted
            holding += counted_busy
            result[agent_id] = free if enabled and not draining else 0
            members.append((agent_id, agent, entries))
        if len(matching) != expected_agents or counted_slots != expected_slots:
            return {}, 0, [], (
                f"pool reports {expected_slots} slot(s) on {expected_agents} agent(s); "
                f"the agent list gives {counted_slots} counted slot(s) on "
                f"{len(matching)} agent(s)"
            )
        return result, holding, members, None

    def require_capacity(self, kind: str, config: Mapping[str, Any]) -> Dict[str, Any]:
        """Require current capacity for the config's exact selected pool."""

        if kind not in {"command", "shell", "experiment", "generic"}:
            raise ValueError("kind must be command, shell, generic, or experiment")
        if not isinstance(config, Mapping):
            raise ValueError("config must be an object")
        resources = config.get("resources")
        if not isinstance(resources, Mapping):
            raise ValueError("config.resources must be an object")
        slot_key = "slots_per_trial" if kind == "experiment" else "slots"
        slots = resources.get(slot_key)
        pool = resources.get("resource_pool")
        self._validate_request(slots, pool)
        if pool is None:
            raise ValueError("config.resources.resource_pool must be a non-empty string")
        is_single_node = resources.get("is_single_node")
        if is_single_node is not None and not isinstance(is_single_node, bool):
            raise ValueError("config.resources.is_single_node must be a boolean or null")
        preference = resources.get("prefer_gpu_topology")
        if not _valid_preference(preference):
            raise ValueError(
                'config.resources.prefer_gpu_topology must be "soft", "strong", false, or null'
            )
        strong = preference == "strong" and slots >= 2
        groups = normalize_gpu_groups(
            resources.get("gpu_groups"), slots, slot_key, "config.resources.gpu_groups"
        )
        # A trial is multi-agent unless is_single_node is true (absent or null included). The
        # scheduler tries one agent first; spanning agents is not modelled, so a trial that
        # fits on no single agent has unknown capacity. "strong" and gpu_groups always use one
        # agent.
        may_span_agents = (
            kind == "experiment"
            and slots >= 2
            and is_single_node is not True
            and not strong
            and groups is None
        )
        # Commands and shells always use one agent (the master validates them as single-node);
        # experiments and generic tasks only with is_single_node: true.
        single_node = kind in {"command", "shell"} or is_single_node is True
        report, refusals = self._resources(
            slots=slots, pool=pool, preference=preference, groups=groups,
            single_node=single_node or strong or groups is not None,
        )
        selected = report["selected_pool"]
        if selected is not None and selected["available"] is True:
            return {**report, "admitted": True}
        if selected is not None and selected["usable"] is False:
            raise APIError(
                f"this account may not use resource pool {pool!r}: the pool is restricted",
                code="permission_denied",
                details={"resource_pool": pool, "requested_slots": slots},
                retryable=False,
            )

        available = selected["available_capacity"] if selected is not None else None
        code = "capacity_unknown"
        retryable = False
        if selected is None:
            message = f"resource pool {pool!r} is not present or not available to you"
        elif pool in refusals:
            # The master refuses this request with the pool's current agents.
            code = "capacity_unavailable"
            message = refusals[pool]
        elif selected["available"] is None:
            message = f"capacity for resource pool {pool!r} is unknown: {selected['explanation']}"
        elif groups is not None:
            code = "capacity_unavailable"
            retryable = True
            on_node = " on one NUMA node" if strong else ""
            message = (
                f"no schedulable agent in pool {pool!r} has free GPUs for gpu_groups "
                f"{describe_gpu_groups(groups)}{on_node} now; the task would wait"
            )
        elif strong:
            code = "capacity_unavailable"
            retryable = True
            message = (
                f"no schedulable agent in pool {pool!r} has {slots} free GPUs on one NUMA "
                f"node now (most on one node: {available}); a \"strong\" task would wait"
            )
        elif may_span_agents:
            message = (
                f"capacity for resource pool {pool!r} is unknown: this experiment may span "
                "agents, which this service does not check; set is_single_node: true, or "
                "launch with allow_queue=true"
            )
        else:
            code = "capacity_unavailable"
            retryable = True
            message = f"resource pool {pool!r} cannot currently fit the request without queueing"
        raise APIError(
            message,
            code=code,
            details={
                "resource_pool": pool,
                "requested_slots": slots,
                "available": available,
                "candidate_pools": report["candidate_pools"],
            },
            retryable=retryable,
        )


__all__ = ["ResourceInspector"]
