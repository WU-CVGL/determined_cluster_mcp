---
name: intensive-compute-runner
description: Plan, launch, monitor, and stop GPU or CPU jobs through the Determined compute MCP tools. Use for cluster workloads, capacity questions, and explicitly authorized short local GPU runs.
---

# Intensive Compute Runner

Use the project's configured account, image, pool, and mount mappings. Keep credentials service-side. Get the command, inputs, output location, and success criteria from the task.

## Choose execution

Use the cluster for training, long runs, and heavy CPU or memory use.

| Kind | Use |
| --- | --- |
| `command` | Finite, non-interactive work. |
| `shell` | Interactive debugging or environment inspection. |
| `generic` | Child tasks or batch work with `pausable: true`. Resume reruns the command, so it must tolerate interruption and reruns. |
| `experiment` | Trials, automatic restarts, or checkpoints. Continuing from a checkpoint requires workload support. |

Use `kind: auto` when intent is clear; it never selects `generic`. Use the minimum suitable slots; CPU-only work can use `slots: 0` where supported. See the [compute reference](../../docs/compute-service.md) for request fields and kind selection.

For a short single-GPU task already authorized to run locally, follow [local workstation runs](../../docs/agent-workflow.md#local-workstation-runs).

## Run a cluster task

1. Prepare code, dependencies, and outputs on mapped shared storage. Use container paths for `workdir` and `output_dir`, and a stable revision for durable work. If needed, stage files through the [storage workflow](../../docs/shared-storage-access.md), reviewing the transfer preview and secret exclusions. Submit mapped paths without a source archive.
2. Give the request a meaningful `name` (at most 128 characters) and `description` (at most 2,048). Call `compute_plan(request)` and inspect the resolved kind, image, paths, mounts, pool, and slots. Planning is offline.
3. Keep `allow_queue: false` unless queueing is authorized. `compute_launch` checks capacity; use `compute_resources` when a capacity decision needs a live snapshot. Do not change the pool, slots, or execution location automatically.
4. Call `compute_launch(request)` once. Every call is a new submission. Keep the returned `kind`, native Determined `id`, and `submission_marker`; the MCP stores no task records and manages only the configured account's tasks.
5. Follow `compute_status` and `compute_logs`, then check the exit result and expected outputs. Use `compute_usage` when resource measurements are needed; missing values do not mean idle resources. Use `compute_cancel` to stop the intended task.

## Handle failed submissions

If `name` or `description` is rejected as too long with `invalid_request`, shorten it and submit the corrected request; that validation happens before submission.

For `submission_uncertain`, do not launch again automatically. Search with `compute_list(kind, marker=...)` using the returned `submission_marker`. An empty result does not prove failure; report an unresolved or ambiguous result and leave resubmission to the user. See [unconfirmed submissions](../../docs/troubleshooting.md#submission-outcome-is-uncertain).

Report authentication or configuration errors and fix their cause. They do not authorize a local fallback.

## Report

Report the task kind and ID, observed state and exit result, revision, output paths, and whether the expected artifact or metric was verified. Include the next action if work is still running. Omit secrets.

Use the [agent workflow](../../docs/agent-workflow.md) for detailed execution, pause/resume, and ownership guidance.
