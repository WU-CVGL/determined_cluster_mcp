---
name: intensive-compute-runner
description: Plan, launch, inspect, and stop resource-intensive GPU or CPU work on a Determined cluster through the determined-compute MCP tools (compute_* and storage_*). Use for heavy compute or managed cluster tasks; small local checks and hardware inspection alone are outside scope.
---

# Intensive Compute Runner

Use this repository's `ComputeService` for heavy-compute planning, idempotent launch, task state, logs, measured usage, and cancellation. Any MCP-capable agent can use the tools with its own model. Install the skill by linking this directory into the agent's skills directory, as the repository README describes; relative links such as `../../docs/` then resolve through that link to the repository checkout.

## Choose a mode

Let `kind: auto` select from intent when the request is clear:

- Use `command` for a one-off, non-interactive job expected to finish during an ordinary working session.
- Use `shell` for interactive debugging, environment inspection, and iterative work. A deployment may advertise an inactivity window such as about two hours; treat it as an advisory, configurable site policy rather than a Determined guarantee.
- Use `generic`, explicitly, for batch work that needs a name or child tasks, and with `pausable: true` when it must be pausable with `compute_pause` and `compute_resume` without becoming an experiment. Resuming reruns the command from the start and nothing restarts automatically, so a pausable command must skip completed outputs and resume or clean partial ones.
- Use `experiment` for overnight or durable work, and whenever its features are needed: a searcher over one or many trials, automatic restarts of a failed trial up to `max_restarts`, checkpoints and metrics that the workload reports through Determined's Core API, and pause and resume, so a restarted or resumed trial continues from its latest checkpoint. There is no rigid midnight cutoff.

Set `interactive` or `overnight` explicitly when intent would otherwise be ambiguous. Use the minimum suitable `slots`; heavy CPU work can use zero GPU slots only if the service and target pool support it.

Give each request a short, task-specific `name` and a `description` that states its purpose or config. Do not use task IDs, request IDs, or UUIDs as display names.

## Prepare durable inputs

Put code, configs, datasets, packages, outputs, checkpoints, and other artifacts on storage covered by the compute profile's `mounts`. Use the mapped container path for `workdir` and `output_dir`. Never send source through an experiment `modelDefinition`, project archive, or other upload field.

For durable jobs, use a stable revision in its own shared directory and record `code_revision`. Reserve mutable workspaces for shell debugging.

If files must be copied into shared storage, read [references/compute-workflow.md](references/compute-workflow.md). Preserve its secret exclusions and safe sync rules.

If the client lacks cluster mounts, read [the shared-storage access guide](../../docs/shared-storage-access.md). Use `storage_check`, preview `storage_sync` or `storage_fetch`, and execute only after review. Storage credentials stay service-side.

## Plan, then execute

1. Call `compute_plan`; inspect the resolved kind, config, paths, revision, and advisories.
2. Call `compute_resources` for the requested slots and pool. Capacity is a snapshot, not a reservation; do not switch pool or location automatically.
3. Keep `allow_queue: false` unless queueing is approved for this call. Resolve unsafe or unknown capacity before launch.
4. Call `compute_launch` with a stable `request_id`. Keep its local `task_id`, which differs from the remote ID.
5. Observe with `compute_status`, `compute_logs`, and `compute_list_tasks`; use `compute_usage` to check measured CPU, memory, and GPU use before proposing a resize. Cancel only the intended task.

Never include credentials in requests, configs, logs, or reports. A launch with `allow_queue: false` performs admission checking and rejects busy or unknown capacity without submitting; `true` explicitly permits scheduler queueing.

If launch outcome is unknown after a timeout or connection loss, do not submit again blindly. Use `compute_reconcile` only with a verified remote ID for the known task; the service checks its submission marker before binding. If authentication fails, stop and report the configuration problem; do not fall back to local execution.

## Report

Return the name, mode, IDs, state, pool, slots, mapped paths, revision, and next status/log/cancel action. Omit secrets.

Read [references/compute-workflow.md](references/compute-workflow.md) for request fields, storage preparation, failure handling, and deployment-specific shell policy.
