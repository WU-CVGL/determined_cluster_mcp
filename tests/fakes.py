"""A fake master for tool tests: the client surface over an in-memory job ledger."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from typing import Any, Dict, List, Optional

from determined_compute.client import APIError, redact

SUBMITTED_AT = "2026-09-30T17:54:08.653Z"


class FakeMaster:
    """The client surface the tools use, over an in-memory ledger.

    The digest covers the kind, workspace, config and file manifest, but not file times.
    Keys follow the master's order: a used key replays when its stored digest equals
    ``expected_digest`` (or the request's digest), and conflicts otherwise; an unused key
    whose digest differs from ``expected_digest`` is ``plan_changed`` and stays free.
    """

    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.keys: Dict[str, tuple] = {}
        self.bind_mounts: Optional[List[Dict[str, Any]]] = [
            {"host_path": "/cluster/shared", "container_path": "/shared", "read_only": False}
        ]
        self.warnings: List[str] = []
        self.workspaces = {"research": 7}
        self.cancel_after_polls = 1
        self.polls: Dict[str, int] = {}
        self.dry_run_error: Optional[APIError] = None
        # Master and workspace defaults the dry run merges under the request's own config.
        self.defaults: Dict[str, Any] = {}
        self.read_error: Optional[APIError] = None
        self.logs: List[Dict[str, Any]] = [{"log": "hello"}]
        self.pools: List[Dict[str, Any]] = []
        self.agents: List[Dict[str, Any]] = []

    @staticmethod
    def digest(kind, config, files, workspace_id) -> str:
        manifest = [
            {key: item[key] for key in ("path", "type", "mode", "content")} for item in files
        ]
        body = {"kind": kind, "config": config, "files": manifest, "workspace": workspace_id}
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    def submit(
        self,
        kind,
        config,
        *,
        files=(),
        workspace_id=None,
        project_id=None,
        dry_run=False,
        idempotency_key=None,
        expected_digest=None,
        admission="queue",
    ):
        self.calls.append(
            (
                "submit",
                {
                    "kind": kind,
                    "config": copy.deepcopy(config),
                    "files": list(files),
                    "workspace_id": workspace_id,
                    "dry_run": dry_run,
                    "key": idempotency_key,
                    "expected": expected_digest,
                    "admission": admission,
                },
            )
        )
        assert admission == "queue"
        assert dry_run == (idempotency_key is None)
        digest = self.digest(kind, config, files, workspace_id)
        if dry_run:
            if self.dry_run_error is not None:
                raise self.dry_run_error
            effective = {
                **copy.deepcopy(self.defaults),
                **copy.deepcopy(config),
                "bind_mounts": self.bind_mounts,
            }
            effective["environment"]["registry_auth"] = {"password": "hidden"}
            return {
                "job_id": None,
                "replayed": False,
                "request_digest": digest,
                "outcome": None,
                "effective_config": redact(effective),
                "warnings": list(self.warnings),
            }
        if idempotency_key in self.keys:
            job_id, stored = self.keys[idempotency_key]
            if stored != (expected_digest or digest):
                raise APIError(
                    f'idempotency key "{idempotency_key}" is already used by job {job_id} for a '
                    "different request",
                    code="key_conflict",
                    details={"job_id": job_id},
                )
            return self._result(job_id, stored, replayed=True)
        if expected_digest is not None and expected_digest != digest:
            raise APIError(
                f"plan_changed: the request digest {digest} differs from the expected digest "
                f"{expected_digest}",
                code="plan_changed",
                details={"request_digest": digest, "expected_digest": expected_digest},
            )
        job_id = str(uuid.uuid4())
        self.keys[idempotency_key] = (job_id, digest)
        self.jobs[job_id] = submission(
            job_id, kind=kind, key=idempotency_key, digest=digest, name=config.get("name")
        )
        return self._result(job_id, digest, replayed=False)

    def replay(self, kind, idempotency_key, expected_digest):
        """The master's key lookup: nothing is created, and a free key stays free."""

        self.calls.append(("replay", kind, idempotency_key, expected_digest))
        if idempotency_key not in self.keys:
            return None
        job_id, stored = self.keys[idempotency_key]
        if stored != expected_digest or self.jobs[job_id]["kind"] != kind:
            raise APIError(
                f'idempotency key "{idempotency_key}" is already used by job {job_id} for a '
                "different request",
                code="key_conflict",
                details={"job_id": job_id},
            )
        return self._result(job_id, stored, replayed=True)

    @staticmethod
    def _result(job_id, digest, replayed):
        return {
            "job_id": job_id,
            "replayed": replayed,
            "request_digest": digest,
            "outcome": "queued",
            "effective_config": None,
            "warnings": [],
        }

    def created(self) -> List[Dict[str, Any]]:
        return [call[1] for call in self.calls if call[0] == "submit" and not call[1]["dry_run"]]

    def get_submission(self, job_id):
        self.calls.append(("get_submission", job_id))
        if self.read_error is not None:
            raise self.read_error
        if job_id not in self.jobs:
            raise APIError(f"submission '{job_id}' not found", code="not_found")
        if job_id in self.polls:
            self.polls[job_id] += 1
            if self.polls[job_id] >= self.cancel_after_polls:
                self.jobs[job_id].update(
                    state="canceled", exit_class="none", ended_at="2026-09-30T17:55:00Z"
                )
        return copy.deepcopy(self.jobs[job_id])

    def list_submissions(self, *, kind=None, state=None, limit=None, page_token=None):
        self.calls.append(("list_submissions", kind, state, limit, page_token))
        jobs = [job for job in self.jobs.values() if kind in (None, job["kind"])]
        return {"submissions": copy.deepcopy(jobs), "next_page_token": "next"}

    def cancel_submission(self, job_id):
        self.calls.append(("cancel_submission", job_id))
        self.polls.setdefault(job_id, 0)
        return copy.deepcopy(self.jobs[job_id])

    def find_workspace_id(self, name):
        self.calls.append(("find_workspace_id", name))
        if name not in self.workspaces:
            raise APIError(f"workspace {name!r} does not exist", code="not_found")
        return self.workspaces[name]

    def task_logs(self, task_id, tail=200):
        self.calls.append(("task_logs", task_id, tail))
        return self.logs

    def task_resources_enabled(self):
        return False

    def list_resource_pools(self):
        return copy.deepcopy(self.pools)

    def list_agents(self):
        return copy.deepcopy(self.agents)


def allocation(task_id, state="queued", index=1):
    return {
        "allocation_id": f"{task_id}.{index}",
        "state": state,
        "is_ready": None,
        "start_time": None,
        "end_time": None,
        "slots": 0,
        "resource_pool": "gpu",
        "exit_class": None,
        "exit_reason": None,
        "exit_detail": None,
        "status_code": None,
        "placements": [],
    }


def submission(job_id, *, kind="command", key=None, digest=None, name=None, **fields):
    task_id = f"task-{job_id[:8]}"
    value = {
        "job_id": job_id,
        "kind": kind,
        "entity_id": task_id,
        "name": name or "train",
        "owner_id": 1,
        "owner": "alice",
        "workspace_id": 1,
        "project_id": None,
        "idempotency_key": key,
        "request_digest": digest,
        "admission": "queue",
        "submitted_at": SUBMITTED_AT,
        "ended_at": None,
        "state": "queued",
        "exit_class": None,
        "exit_reason": None,
        "tasks": [{"task_id": task_id, "trial_id": None, "allocations": [allocation(task_id)]}],
    }
    value.update(fields)
    return value
