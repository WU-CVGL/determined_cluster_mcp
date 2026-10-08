"""Read-only resource inspection and conservative launch admission.

The result is a point-in-time advisory, not a reservation.  Admission never
changes the requested resource pool and never treats utilization as capacity.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from determined_compute.core.api_client import APIError


_STATISTICS_MISSING = "complete per-agent slot capacity statistics are missing"


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


class ResourceInspector:
    """Inspect current capacity through the Determined read-only APIs."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def resources(self, slots: int = 1, pool: Optional[str] = None) -> Dict[str, Any]:
        """Return conservative, per-pool capacity for a request on one agent."""

        self._validate_request(slots, pool)
        return self._resources(slots=slots, pool=pool)

    def _resources(self, *, slots: int, pool: Optional[str]) -> Dict[str, Any]:
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

        assessments = [
            self._assess_pool(item, agents, slots=slots)
            for item in pools
            if isinstance(item, Mapping) and isinstance(item.get("name"), str)
        ]
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
        }

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
    ) -> Dict[str, Any]:
        name = str(raw["name"])
        total = _nonnegative_int(raw.get("slotsAvailable"))
        used = _nonnegative_int(raw.get("slotsUsed"))
        num_agents = _nonnegative_int(raw.get("numAgents"))
        slot_type = raw.get("slotType")
        aux_capacity = _nonnegative_int(raw.get("auxContainerCapacity"))
        aux_running = _nonnegative_int(raw.get("auxContainersRunning"))
        per_agent, holding, agent_problem = self._agent_free(name, agents, num_agents, total)
        agent_data_known = agent_problem is None

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
            problem = agent_problem
            if known and used != holding:
                # The pool's used count is the slots holding containers (numUsedSlots).
                known = False
                problem = (
                    f"used slots ({used}) differ from slots holding containers ({holding}); "
                    "a task may be starting or stopping"
                )
            if known:
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
        }

    @staticmethod
    def _agent_free(
        pool: str,
        agents: List[Any],
        expected_agents: Optional[int],
        expected_slots: Optional[int],
    ) -> Tuple[Dict[str, int], int, Optional[str]]:
        """Return each agent's free slots and the counted slots holding a container.

        The third value says why the agent list does not match the pool, or is None.

        Slots are counted the way the pool counts them (numSlots in the scheduler's agent
        state): a slot counts when it takes new work, or when it is draining and still holds a
        container. A draining agent counts only its counted slots that hold a container, and a
        disabled agent counts none.
        """

        if expected_agents is None or expected_slots is None:
            return {}, 0, _STATISTICS_MISSING
        matching: List[Mapping[str, Any]] = []
        for item in agents:
            if not isinstance(item, Mapping):
                continue
            memberships = item.get("resourcePools")
            if isinstance(memberships, list) and pool in memberships:
                matching.append(item)

        result: Dict[str, int] = {}
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
                return {}, 0, _STATISTICS_MISSING
            counted = 0
            counted_busy = 0
            free = 0
            for slot in entries:
                slot_enabled = slot.get("enabled", True)
                slot_draining = slot.get("draining", False)
                if not isinstance(slot_enabled, bool) or not isinstance(slot_draining, bool):
                    return {}, 0, _STATISTICS_MISSING
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
        if len(matching) != expected_agents or counted_slots != expected_slots:
            return {}, 0, (
                f"pool reports {expected_slots} slot(s) on {expected_agents} agent(s); "
                f"the agent list gives {counted_slots} counted slot(s) on "
                f"{len(matching)} agent(s)"
            )
        return result, holding, None

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
        # A trial is multi-agent unless is_single_node is true (absent or null included). The
        # scheduler tries one agent first; spanning agents is not modelled, so a trial that
        # fits on no single agent has unknown capacity.
        may_span_agents = kind == "experiment" and slots >= 2 and is_single_node is not True
        report = self._resources(slots=slots, pool=pool)
        selected = report["selected_pool"]
        if selected is not None and selected["available"] is True:
            return {**report, "admitted": True}

        available = selected["available_capacity"] if selected is not None else None
        code = "capacity_unknown"
        if selected is None:
            message = f"resource pool {pool!r} is not present or not available to you"
        elif selected["available"] is None:
            message = f"capacity for resource pool {pool!r} is unknown: {selected['explanation']}"
        elif may_span_agents:
            message = (
                f"capacity for resource pool {pool!r} is unknown: this experiment may span "
                "agents, which this service does not check; set is_single_node: true, or "
                "launch with allow_queue=true"
            )
        else:
            code = "capacity_unavailable"
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
            retryable=code == "capacity_unavailable",
        )


__all__ = ["ResourceInspector"]
