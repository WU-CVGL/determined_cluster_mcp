---
name: intensive-compute-runner
description: Plan, launch, monitor, and stop GPU or CPU jobs through the Determined compute MCP tools. Use for Determined cluster workloads and cluster capacity questions, and for a user-authorized short local run of a cluster task; ordinary local runs and hardware inspection are out of scope.
---

# Intensive Compute Runner

Use the project's configured account, image, pool, and mount mappings. Keep credentials service-side. Get the command, inputs, output location, and success criteria from the task. Links to `../../docs/` are relative to this skill directory's real path; if the base directory is a symlink, resolve it first (for example `realpath <base directory>`) before reading them.

## Choose execution

Use the cluster for training, long runs, and heavy CPU or memory use.

| Kind | Use |
| --- | --- |
| `command` | Finite, non-interactive work. |
| `shell` | Interactive debugging or environment inspection. |
| `generic` | Child tasks or batch work with `pausable: true`. Resume reruns the command, so it must tolerate interruption and reruns. |
| `experiment` | Trials, automatic restarts, or checkpoints. Continuing from a checkpoint requires workload support. |

Use `kind: auto` when intent is clear; it never selects `generic`. Use the minimum suitable slots; CPU-only work can use `slots: 0` where supported. See the [compute reference](../../docs/compute-service.md) for request fields and kind selection.

For a short single-GPU task already authorized to run locally, read [local workstation runs](../../docs/agent-workflow.md#local-workstation-runs) first; if it cannot be read, do not run locally.

## Run a cluster task

1. Prepare code, dependencies, and outputs on mapped shared storage. Use container paths for `workdir` and `output_dir`. For durable work, stage each revision in its own directory, record it as `code_revision`, and do not sync into that directory afterwards: a restart or resume runs whatever it then holds. If needed, stage files through the [storage workflow](../../docs/shared-storage-access.md), reviewing the transfer preview and secret exclusions. Submit mapped paths without a source archive.
2. Give the request a meaningful `name` (at most 128 characters) and `description` (at most 2,048). Call `compute_plan(request)` and inspect the resolved kind, image, paths, mounts, pool, and slots. Planning is offline.
3. Keep `allow_queue: false` unless queueing is authorized. `compute_launch` checks capacity; use `compute_resources` when a capacity decision needs a live snapshot. Do not change the pool, slots, or execution location automatically. A launch refused for capacity submitted nothing; do not resubmit it in a loop. For multi-GPU work, follow a GPU topology requirement the user states and otherwise keep the default, no `prefer_gpu_topology` preference (`"strong"` waits for one NUMA node; `"soft"` does not queue extra to wait for a better topology). Pass the same value to `compute_resources` and `compute_launch`. Ask the user only when the topology decides whether the task can run and their intent is unclear.
4. Call `compute_launch(request)` once. Every call is a new submission. Keep the returned `kind`, native Determined `id`, and `submission_marker`; the MCP stores no task records and manages only the configured account's tasks.
5. Follow `compute_status` and `compute_logs` until the task reaches a terminal state; `compute_list(kind, states=["STATE_ACTIVE"])` shows which experiments or generic tasks are still queued or running, one page at a time, but a task that leaves that list may be paused or stopping, so confirm its terminal state with `compute_status` before you check the exit result and expected outputs. While the task has not ended, `compute_status.queue` gives the queue position `jobs_ahead` (not a wait-time prediction) and, once scheduled, the agents and slot device IDs in `placement`; a null `queue` does not mean the task is not queued. Use `compute_usage` when resource measurements are needed; missing values do not mean idle resources, and an allocation's first minutes may have no data. Use `compute_cancel` to stop the intended task.

## Handle failed submissions

If `name` or `description` is rejected as too long with `invalid_request`, shorten it and submit the corrected request; that validation happens before submission.

A `permission_denied` error that names a resource pool means the account may not use that pool; report it and leave choosing another pool, or asking an administrator for access, to the user.

For `submission_uncertain`, do not launch again automatically. Search with `compute_list(kind, marker=...)` using the returned `submission_marker`. An empty result does not prove failure; report an unresolved or ambiguous result and leave resubmission to the user. See [unconfirmed submissions](../../docs/troubleshooting.md#submission-outcome-is-uncertain).

Stop and report authentication or deployment-configuration errors without reading credential values or changing the secrets file or MCP configuration; see [authentication fails](../../docs/troubleshooting.md#authentication-fails). They do not authorize a local fallback.

## Report

Report the task kind and ID, observed state and exit result, revision, output paths, and whether the expected artifact or metric was verified. Include the next action if work is still running. Omit secrets.

Use the [agent workflow](../../docs/agent-workflow.md) for detailed execution, pause/resume, and ownership guidance.
