---
name: intensive-compute-runner
description: Plan, launch, inspect, and stop resource-intensive GPU or CPU work as Determined jobs through this repository's compute MCP. Use for heavy compute or managed cluster tasks; small local checks and hardware inspection alone are outside scope.
---

# Intensive Compute Runner

Use the compute MCP to run heavy work as Determined jobs: write a `TaskSpec`, plan it, review the plan, launch it bound to that plan, then follow the job by its `job_id`. The master records every job, so every client of the same account sees the same jobs; the MCP keeps no local state. Any MCP-capable agent can use the tools with its own model.

## Choose a kind

- Use `command` for a one-off, non-interactive job expected to finish during an ordinary working session.
- Use `shell` for interactive debugging, environment inspection, and iterative work. A shell takes no command, `output_dir`, `workdir`, or `git` code.
- Use `experiment` for overnight or durable work, and whenever experiment features such as search, trial tracking, or checkpoints are needed.

Use the minimum suitable `slots`; CPU work can use zero slots when the pool supports it. Give each spec a short, task-specific `name`; never use a job ID, request ID, or UUID as a name.

## Prepare durable inputs

Put datasets, packages, outputs, checkpoints, and other artifacts on storage covered by the policy's `mounts`, and set `output_dir` to a writable container path there. Choose how code reaches the task:

- `git`: a repository on shared storage whose root the machine running the MCP server mounts; the plan pins a commit that must be on a branch or tag.
- `context`: a local working tree, uploaded at a pinned commit plus explicit `include` paths, up to 99,614,720 bytes.
- `path`: a shared directory run in place and never pinned; reserve it for shells and debugging.

If files must be copied to shared storage, use `storage_check`, preview `storage_sync` or `storage_fetch`, and execute only after review; see [references/compute-workflow.md](references/compute-workflow.md). Storage credentials stay service-side.

## Plan, launch, observe

1. Call `compute_plan(spec)`; nothing is created. Review the resolved `spec`, `commit`, `code` summary (for `context`, the `included` and `excluded` paths), `effective_config`, and every warning. Placement is not evaluated: `compute_resources(pool)` is a snapshot of the pools, not a verdict, and a job waits in the queue until its slots are free. Do not switch pool or slots on your own.
2. Call `compute_launch(spec, request_id, request_digest)` with the three values the plan returned, passing the returned `spec`, not the one you wrote. The launch is bound to the plan: if the code or request changed since, it returns `plan_changed` and creates nothing. Record the `job_id` and `request_id`.
3. Follow the job by `job_id`: `compute_status` for its state, exit class, and explanation; `compute_logs` for output (`trial_id` selects an experiment's trial); `compute_usage` for measured CPU, memory, and GPU use before proposing a resize. `compute_list` finds the account's jobs from every client with their `request_id`. Cancel only the intended job with `compute_cancel(job_id)`.

## Fail reliably

A job may fail, but a failure is never reported as success, and work whose outcome is uncertain is never run again silently.

- After a timeout, a lost answer, `unavailable`, or `invalid_response`, repeat the identical launch with the same `spec`, `request_id`, and `request_digest`, or look the `request_id` up in `compute_list`. The master returns the existing job with `replayed: true` and never creates a second one. After `internal`, repeat it once; the same error again means nothing was created.
- Never plan a new `request_id` while a launch outcome is uncertain: a new key lets the master create a second job.
- `plan_changed`: nothing was created. Plan again and review the new commit before launching. `key_conflict`: the `request_id` is bound to the job in `details.job_id`; read it with `compute_status` before doing anything else.
- `admission_unsupported`, `protocol_unsupported`, and policy or code-check refusals create nothing. Stop and report them; do not work around them with another pool, code source, or tool. `protocol_unsupported` means the master must be upgraded.
- Report a `failed` or `canceled` job as such, with its `exit_class`, `exit_reason`, and the log lines that show the cause; a failed prelude prints a line starting with `compute:`. Never relaunch automatically after a failure or a cancel: a new run is a new plan that the user approves.
- If authentication fails, stop and report the configuration problem; do not fall back to local execution.

Never include credentials in specs, commands, `env`, logs, or reports; a command's text and `env` are stored in the job's config.

## Report

Return the name, kind, `job_id`, `request_id`, state, exit class, pool, slots, commit, output path, and the next status, log, or cancel action. For a failure, say that it failed and why. Omit secrets.

Read [references/compute-workflow.md](references/compute-workflow.md) for spec fields, code sources, plan binding, storage preparation, error codes, usage, and shell lifetime.
