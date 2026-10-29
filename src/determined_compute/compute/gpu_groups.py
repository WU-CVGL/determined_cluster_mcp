"""resources.gpu_groups of the research-cluster fork (0.42.2 or later).

A task names groups of GPUs by exact model; the master places all of them on one agent at once.
This module validates groups as the master does, describes them as its messages do, and tells
whether a list of GPU models can hold them.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

_NAME_PATTERN = "^[a-z][a-z0-9_-]{0,31}$"
_NAME = re.compile(_NAME_PATTERN)
_GROUP_FIELDS = {"name", "count", "models"}


def normalize_gpu_groups(
    value: Any, slots: int, slots_field: str = "slots", path: str = "gpu_groups"
) -> Optional[List[Dict[str, Any]]]:
    """Return the groups as plain dicts, or None when unset; raise ValueError as the master does.

    Each group needs a unique ``name`` matching the master's pattern, a ``count`` of at least 1
    and a non-empty list of distinct, non-empty ``models``. The counts must add up to the task's
    slots, given with the name of their field.
    """

    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{path} must be a list or null")
    if not value:
        raise ValueError(f"{path} must not be empty")
    groups: List[Dict[str, Any]] = []
    names = set()
    total = 0
    for index, group in enumerate(value):
        if not isinstance(group, Mapping):
            raise ValueError(f"{path}[{index}] must be an object")
        unknown = set(group) - _GROUP_FIELDS
        if unknown:
            raise ValueError(f"{path}[{index}] has unknown fields: {sorted(unknown)}")
        name, count, models = group.get("name"), group.get("count"), group.get("models")
        if not isinstance(name, str) or not _NAME.match(name):
            shown = json.dumps(name) if isinstance(name, str) else repr(name)
            raise ValueError(f"{path} name {shown} must match {_NAME_PATTERN}")
        if name in names:
            raise ValueError(f'{path} name "{name}" is not unique')
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(f"{path} {name}: count must be at least 1")
        if not isinstance(models, (list, tuple)) or not models:
            raise ValueError(f"{path} {name}: models must not be empty")
        seen = set()
        for model in models:
            if not isinstance(model, str) or not model:
                raise ValueError(f"{path} {name}: a model must not be empty")
            if model in seen:
                raise ValueError(f"{path} {name}: models must be unique")
            seen.add(model)
        names.add(name)
        total += count
        groups.append({"name": name, "count": count, "models": list(models)})
    if total != slots:
        raise ValueError(f"{path} counts must add up to {slots_field} ({slots})")
    return groups


def describe_gpu_groups(groups: Sequence[Mapping[str, Any]]) -> str:
    """Describe groups as the master's messages do: ``sim: 1 × A; learner: 2 × B or C``."""

    return "; ".join(
        f"{group['name']}: {group['count']} × {' or '.join(group['models'])}"
        for group in groups
    )


def holds_gpu_groups(groups: Sequence[Mapping[str, Any]], models: Sequence[str]) -> bool:
    """Whether GPUs of these models can serve every group at once, each GPU in one group.

    This is the master's matching (holdsGPUGroups): augmenting paths over the group's slots, with
    model lists that may overlap.
    """

    candidates = [
        model for model in models if any(model in group["models"] for group in groups)
    ]
    needed = sum(group["count"] for group in groups)
    # Refuse before listing the slots: the counts are bounded by the slots, not by the GPUs.
    if needed == 0 or needed > len(candidates):
        return False
    slot_group = [index for index, group in enumerate(groups) for _ in range(group["count"])]
    device_slot = [-1] * len(candidates)

    def augment(slot: int, seen: List[bool]) -> bool:
        group = groups[slot_group[slot]]
        for device, model in enumerate(candidates):
            if seen[device] or model not in group["models"]:
                continue
            seen[device] = True
            if device_slot[device] < 0 or augment(device_slot[device], seen):
                device_slot[device] = slot
                return True
        return False

    return all(augment(slot, [False] * len(candidates)) for slot in range(len(slot_group)))


__all__ = ["describe_gpu_groups", "holds_gpu_groups", "normalize_gpu_groups"]
