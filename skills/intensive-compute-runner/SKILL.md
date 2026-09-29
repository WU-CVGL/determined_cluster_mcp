---
name: intensive-compute-runner
description: Plan, launch, inspect, and stop resource-intensive GPU or CPU work through this repository's Determined compute service. Use for heavy compute or managed cluster tasks; small local checks and hardware inspection alone are outside scope.
---

# Intensive Compute Runner

Use this repository's `ComputeService` for heavy-compute planning, idempotent launch, task state, logs, measured usage, and cancellation. Any MCP-capable agent can use the tools with its own model. An optional consultation worker can also load this repository skill; do not install it globally.

## Choose a mode

Let `kind: auto` select from intent when the request is clear:

- Use `command` for a one-off, non-interactive job expected to finish during an ordinary working session.
- Use `shell` for interactive debugging, environment inspection, and iterative work. A deployment may advertise an inactivity window such as about two hours; treat it as an advisory, configurable site policy rather than a Determined guarantee.
- Use `experiment` for overnight or durable work, and whenever experiment features such as search, trial tracking, or checkpoint lifecycle are actually needed. There is no rigid midnight cutoff.

Set `interactive` or `overnight` explicitly when intent would otherwise be ambiguous. Use the minimum suitable `slots`; heavy CPU work can use zero GPU slots only if the service and target pool support it.

Give each request a short, task-specific `name` and a `description` that states its purpose or config. Do not use task IDs, request IDs, or UUIDs as display names.

## Prepare durable inputs

Put code, configs, datasets, packages, outputs, checkpoints, and other artifacts on storage covered by the compute profile's `mounts`. Use the mapped container path for `workdir` and `output_dir`. Never send source through an experiment `modelDefinition`, project archive, or other upload field.

For durable jobs, publish the exact revision with `storage_snapshot` when the deployment configures a snapshot root: preview it, review the excluded secret-like files, publish with `dry_run=false`, and use its `request_fields` for `workdir` and `code_revision`. Identical content is stored once, and the snapshot is read-only, so the job writes under `output_dir`. Otherwise use a stable revision in its own shared directory and record `code_revision`. Reserve mutable workspaces for shell debugging.

If files must be copied into shared storage, read [references/compute-workflow.md](references/compute-workflow.md). Preserve its secret exclusions and safe sync rules.

If the client lacks cluster mounts, read [the shared-storage access guide](../../docs/shared-storage-access.md). Use `storage_check`, preview `storage_sync` or `storage_fetch`, and execute only after review. Storage credentials stay service-side; the consultation worker cannot test them.

## Plan, then execute

1. Call `compute_plan`; inspect the resolved kind, config, paths, revision, advisories, and `path_checks`. `unverified` is not missing; `path_not_found` names a missing required path. Add `create_directories: ["checkpoint_storage"]` when an experiment checkpoint directory does not exist yet. Add `gpu_admission` when the job needs a specific GPU model, driver, count, or free memory; exit code 86 means admission failed before the workload started.
2. Call `compute_resources` for the requested slots and pool. Capacity is a snapshot, not a reservation; do not switch pool or location automatically.
3. Keep `allow_queue: false` unless queueing is approved for this call. Resolve unsafe or unknown capacity before launch.
4. Call `compute_launch` with a stable `request_id`. Keep its local `task_id`, which differs from the remote ID.
5. Observe with `compute_status`, `compute_logs`, and `compute_list_tasks`; use `compute_usage` to check measured CPU, memory, and GPU use before proposing a resize. These reads also work read-only for a task submitted with another profile on the same endpoint (`binding.mode: cross_profile`); cancel with the task's original profile. Cancel only the intended task.

Never include credentials in requests, configs, logs, or reports. A launch with `allow_queue: false` performs admission checking and rejects busy or unknown capacity without submitting; `true` explicitly permits scheduler queueing.

If launch outcome is unknown after a timeout or connection loss, do not submit again blindly. Use `compute_reconcile` only with a verified remote ID for the known task; the service checks its submission marker before binding. If authentication fails, stop and report the configuration problem; do not fall back to local execution.

If the server exposes `compute_consult`, it can request a read-only plan or diagnosis from the deployment's configured consultation backend and model. Consultation is optional; the caller can plan directly with its own model. Consultation state persists, but the worker cannot launch or cancel work; deterministic service tools perform mutations.

## Report

Return the name, mode, IDs, state, pool, slots, mapped paths, revision, and next status/log/cancel action. Omit secrets.

Read [references/compute-workflow.md](references/compute-workflow.md) for request fields, storage preparation, failure handling, and deployment-specific shell policy.
