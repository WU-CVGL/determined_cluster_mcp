"""Read-only resource inspection and conservative launch admission.

The result is a point-in-time advisory, not a reservation.  Admission never
changes the requested resource pool and never treats utilization as capacity.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

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


def _numa_layout(
    agent: Mapping[str, Any], entries: List[Mapping[str, Any]]
) -> Tuple[str, Dict[int, int], Dict[int, int], str]:
    """Classify an agent's GPU topology and count its CUDA slots by NUMA node.

    Returns the class, the slots per node in any state, the free slots per node, and the
    agent's unknown reason. A slot counts toward a node only when the topology gives it a node
    of 0 or more, as the scheduler's numaNodeOf reads it; a slot is free when it is enabled, not
    draining, and holds no container, on an enabled agent that is not draining.
    """

    cuda: List[Mapping[str, Any]] = []
    for slot in entries:
        device = slot.get("device")
        if not isinstance(device, Mapping) or not isinstance(device.get("type"), str):
            return _MALFORMED, {}, {}, ""
        if device["type"] == _CUDA:
            cuda.append(slot)
    if not cuda:
        return _NONE, {}, {}, ""
    topology = agent.get("gpuTopology")
    ids = [_nonnegative_int(slot["device"].get("id")) for slot in cuda]
    if not isinstance(topology, Mapping) or any(item is None for item in ids):
        # Without the sensitive-agent permission the master drops the topology and sets every
        # device id to -1.
        return _HIDDEN, {}, {}, ""
    if len(set(ids)) != len(ids):
        return _MALFORMED, {}, {}, ""
    reason = topology.get("unknownReason")
    if not isinstance(reason, str):
        return _MALFORMED, {}, {}, ""
    if reason == _TOPOLOGY_PENDING:
        return _PENDING, {}, {}, reason
    if reason:
        return _NONE, {}, {}, reason
    gpus = topology.get("gpus")
    if not isinstance(gpus, list):
        return _MALFORMED, {}, {}, ""
    node_of: Dict[int, int] = {}
    for entry in gpus:
        if not isinstance(entry, Mapping):
            return _MALFORMED, {}, {}, ""
        excluded = entry.get("excluded", False)
        if not isinstance(excluded, bool):
            return _MALFORMED, {}, {}, ""
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
            return _MALFORMED, {}, {}, ""
        node_of[device_id] = node
    if set(node_of) != set(ids):
        return _MALFORMED, {}, {}, ""
    schedulable = agent.get("enabled", True) and not agent.get("draining", False)
    total: Dict[int, int] = {}
    free: Dict[int, int] = {}
    for slot, device_id in zip(cuda, ids):
        node = node_of[device_id]
        if node < 0:
            continue
        total[node] = total.get(node, 0) + 1
        if (
            schedulable
            and slot.get("enabled", True)
            and not slot.get("draining", False)
            and slot.get("container") is None
        ):
            free[node] = free.get(node, 0) + 1
    return _KNOWN, total, free, ""


class ResourceInspector:
    """Inspect current capacity through the Determined read-only APIs."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def resources(
        self,
        slots: int = 1,
        pool: Optional[str] = None,
        prefer_gpu_topology: Any = None,
    ) -> Dict[str, Any]:
        """Return conservative, per-pool capacity for a request on one agent."""

        self._validate_request(slots, pool)
        if not _valid_preference(prefer_gpu_topology):
            raise ValueError('prefer_gpu_topology must be "soft", "strong", false, or null')
        report, _refusals = self._resources(
            slots=slots, pool=pool, preference=prefer_gpu_topology
        )
        return report

    def _resources(
        self, *, slots: int, pool: Optional[str], preference: Any
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
                assessment, refusal = self._assess_pool(item, agents, slots=slots, strong=strong)
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
            "single_node": True,
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
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        """Assess one pool; also return why the master refuses the request, if it does."""

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
            if strong:
                capacity_kind = "slots_one_numa_node"
                available, available_capacity, explanation, refusal, numa = self._assess_strong(
                    name,
                    static=raw.get("type") == _STATIC_POOL,
                    members=members,
                    slots=slots,
                    complete=complete,
                    known=known,
                    problem=problem or _STATISTICS_MISSING,
                )
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

        return {
            "resource_pool": name,
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
        static: bool,
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
        if complete and static and largest < slots:
            # The master refuses a request larger than every agent of a static pool at submit,
            # from slot counts alone (ValidateResources), whatever the topology or the slots in
            # use. A provisioned pool checks its instance size instead, which is not read here.
            refusal = _no_agent_holds(name, slots)

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
        # A trial is multi-agent unless is_single_node is true (absent or null included). The
        # scheduler tries one agent first; spanning agents is not modelled, so a trial that
        # fits on no single agent has unknown capacity. "strong" always uses one agent.
        may_span_agents = (
            kind == "experiment" and slots >= 2 and is_single_node is not True and not strong
        )
        report, refusals = self._resources(slots=slots, pool=pool, preference=preference)
        selected = report["selected_pool"]
        if selected is not None and selected["available"] is True:
            return {**report, "admitted": True}

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
