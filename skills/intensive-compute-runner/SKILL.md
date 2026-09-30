---
name: intensive-compute-runner
description: Plan, launch, inspect, and stop resource-intensive GPU or CPU work through this repository's Determined compute service. Use for heavy compute or managed cluster tasks; small local checks and hardware inspection alone are outside scope.
---

# Intensive Compute Runner

Use this repository's compute MCP to plan and launch heavy work on Determined, then read its state, logs, measured usage, and cancel it. Any MCP-capable agent can use the tools with its own model.

## Choose a kind

- Use `command` for a one-off, non-interactive job expected to finish during an ordinary working session.
- Use `shell` for interactive debugging, environment inspection, and iterative work. A shell takes no command, `output_dir`, `workdir`, or `git` code.
- Use `experiment` for overnight or durable work, and whenever experiment features such as search, trial tracking, or checkpoints are needed.

Use the minimum suitable `slots`; CPU work can use zero slots when the pool supports it. Give each spec a short, task-specific `name`; never use a job ID, request ID, or UUID as a name.

## Prepare durable inputs

Put datasets, packages, outputs, checkpoints, and other artifacts on storage covered by the policy's `mounts`, and set `output_dir` to a writable container path there. Choose how code reaches the task:

- `git`: a repository on shared storage whose root this machine mounts; the plan pins a commit that must be on a branch or tag.
- `context`: a local working tree, uploaded at a pinned commit plus explicit `include` paths, up to 99,614,720 bytes.
- `path`: a shared directory run in place and never pinned; reserve it for shells and debugging.

If files must be copied to shared storage, read [references/compute-workflow.md](references/compute-workflow.md) and [the shared-storage access guide](../../docs/shared-storage-access.md). Use `storage_check`, preview `storage_sync` or `storage_fetch`, and execute only after review. Storage credentials stay service-side.

## Plan, then execute

1. Call `compute_plan(spec)`. Review the resolved spec, `commit`, `code` summary, `effective_config`, and `warnings`. Placement is not evaluated; `compute_resources` shows the pools as a snapshot, and a job waits in the queue until its slots are free. Do not switch pool or slots automatically.
2. Call `compute_launch(spec, request_id, request_digest)` with the values the plan returned. Keep the `job_id` and `request_id`.
3. Observe with `compute_status(job_id)` and `compute_logs(job_id)`; use `compute_usage(job_id)` to check measured CPU, memory, and GPU use before proposing a resize. Cancel only the intended job with `compute_cancel(job_id)`.

If a launch answer is lost or `unavailable`, repeat the same launch: it returns the same job with `replayed: true`, even if the tree changed. After `internal`, repeat it once; the same error again means nothing was created. `plan_changed` means nothing was created and the content moved: plan again. Never plan a new `request_id` while an outcome is uncertain; `compute_list` shows every job of the account with its `request_id`.

Never include credentials in specs, commands, `env`, logs, or reports; a command's text and `env` are stored in the job's config. If authentication fails, stop and report the configuration problem; do not fall back to local execution.

## Report

Return the name, kind, `job_id`, `request_id`, state, exit class, pool, slots, commit, output path, and the next status, log, or cancel action. Omit secrets.

Read [references/compute-workflow.md](references/compute-workflow.md) for spec fields, storage preparation, failure handling, and shell lifetime.
