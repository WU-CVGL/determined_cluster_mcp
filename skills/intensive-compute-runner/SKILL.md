---
name: intensive-compute-runner
description: Plans, launches, inspects and stops resource-intensive GPU or CPU work on a Determined cluster through the determined-compute MCP tools (compute_* and storage_*), and runs a user-authorized short single-GPU job on the local workstation in the cluster's container image. Use for heavy compute, managed cluster tasks, request planning and capacity checks, and when the user asks to run a cluster job locally; hardware inspection alone is outside scope.
---

# Intensive Compute Runner

Use this repository's `ComputeService` for heavy-compute planning, launch, task state, logs, measured usage, and cancellation. It keeps no task records: tools address tasks by Determined's own IDs and act only on tasks owned by the configured account, so keep your own record of what you launch. Any MCP-capable agent can use the tools with its own model. Install the skill by linking this directory into the agent's skills directory, as the repository README describes; relative links such as `../../docs/` then resolve through that link to the repository checkout.

## Choose a mode

Let `kind: auto` select from intent when the request is clear:

- Use `command` for a one-off, non-interactive job expected to finish during an ordinary working session.
- Use `shell` for interactive debugging, environment inspection, and iterative work. A deployment may advertise an inactivity window such as about two hours; treat it as an advisory, configurable site policy rather than a Determined guarantee.
- Use `generic`, explicitly, for batch work that needs a name or child tasks, and with `pausable: true` when it must be pausable with `compute_pause` and `compute_resume` without becoming an experiment. Resuming reruns the command from the start and nothing restarts automatically, so a pausable command must skip completed outputs and resume or clean partial ones.
- Use `experiment` for overnight or durable work, and whenever its features are needed: a searcher over one or many trials, automatic restarts of a failed trial up to `max_restarts`, checkpoints and metrics that the workload reports through Determined's Core API, and pause and resume, so a restarted or resumed trial continues from its latest checkpoint. There is no rigid midnight cutoff.

Set `interactive` or `overnight` explicitly when intent would otherwise be ambiguous. Use the minimum suitable `slots`; heavy CPU work can use zero GPU slots only if the service and target pool support it.

Give each request a short, task-specific `name` and a `description` that states its purpose or config. Do not use task IDs or UUIDs as display names.

The service validates `name` to at most 128 characters and `description` to at most 2,048 characters and refuses a longer value before anything is submitted. A validation refusal (`invalid_request`, not retryable) means no task was created: shorten the field, confirm with `compute_list(kind, limit=5)` that nothing matching the name exists, and launch once more.

## Prepare durable inputs

Put code, configs, datasets, packages, outputs, checkpoints, and other artifacts on storage covered by the compute profile's `mounts`. Use the mapped container path for `workdir` and `output_dir`. Never send source through an experiment `modelDefinition`, project archive, or other upload field.

For durable jobs, use a stable revision in its own shared directory and record `code_revision`. Reserve mutable workspaces for shell debugging.

If files must be copied into shared storage, read [references/compute-workflow.md](references/compute-workflow.md). Preserve its secret exclusions and safe sync rules.

If the client lacks cluster mounts, read [the shared-storage access guide](../../docs/shared-storage-access.md). Use `storage_check`, preview `storage_sync` or `storage_fetch`, and execute only after review. Storage credentials stay service-side.

## Plan, then execute

1. Call `compute_plan`; inspect the resolved kind, config, paths, revision, and advisories.
2. Call `compute_resources` for the requested slots and pool. Capacity is a snapshot, not a reservation; do not switch pool or location automatically.
3. Keep `allow_queue: false` unless queueing is approved for this call. Resolve unsafe or unknown capacity before launch.
4. Call `compute_launch(request)` once. Record the returned `kind`, `id` (Determined's task ID), name, and `submission_marker`; every call is a new submission.
5. Observe with `compute_status(kind, id)` and `compute_logs(kind, id)`, and find the account's tasks with `compute_list(kind)`; use `compute_usage(kind, id)` to check measured CPU, memory, and GPU use before proposing a resize. Cancel only the intended task.

Never include credentials in requests, configs, logs, or reports. A launch with `allow_queue: false` performs admission checking and rejects busy or unknown capacity without submitting; `true` explicitly permits scheduler queueing.

If `compute_launch` returns `submission_uncertain`, do not submit again automatically. Look for the task with `compute_list(kind, marker=...)`, using the `submission_marker` from the error details and a small `limit`. One match is most likely the submission; several matches share a copied config, so ask the user. An empty result does not prove that the submission failed; report the unconfirmed launch and let the user decide whether to submit again. If authentication fails, stop and report the configuration problem; do not fall back to local execution.

## Local workstation runs

Only when the user has authorized it for the work at hand, and only for a short single-GPU job whose cluster slot would sit mostly idle: run the request's own command in the cluster's container image on a local GPU with `scripts/run_local.sh <request.json> <gpu-uuid>` (`--dry` prints the docker command). Training, RAM-heavy work and anything that wants many parallel CPU processes stay on the cluster. Read [references/local-runs.md](references/local-runs.md) before the first local run: it gives the pre-launch checks, the memory limit and watch, the launch record that replaces a Determined ID, and the rule that a local card is never compared bitwise with the cluster's.

## Report

Return the name, kind and ID, state, pool, slots, mapped paths, revision, and next status/log/cancel action. Omit secrets.

Read [references/compute-workflow.md](references/compute-workflow.md) for request fields, storage preparation, failure handling, and deployment-specific shell policy.
