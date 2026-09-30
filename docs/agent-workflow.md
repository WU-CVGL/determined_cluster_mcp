# Agent workflow

[English](agent-workflow.md) | [简体中文](agent-workflow.zh.md)

[Home](../README.md) · [Compute reference](compute-service.md) · [Storage access](shared-storage-access.md) · [Troubleshooting](troubleshooting.md)

This workflow is for any agent or client that can call the local stdio MCP tools. The client selects its own model. Normal storage and compute work does not require a repository skill.

## Describe the goal and success criteria

State what should run and what observable result will count as success. Include the project revision, input and output locations, expected artifact or metric, and known resource needs. Refer to a credential file or SSH alias, never credential values.

The following deployment inputs must come from the cluster administrator or the project's existing configuration; do not invent them:

- Determined API URL and account credentials
- approved image and resource pool
- cluster-agent host paths and their container mount paths
- optional local mount or login-node SSH access to shared storage

A useful request is: "Evaluate this commit with one slot, write `metrics.json` under the shared results directory, and report the job ID, exit class, and whether that file exists."

## Read local configuration first

Read `AGENTS.md`, the project's own instructions, the configured policy, the relevant request example under `cfg/examples/`, and the storage-access configuration when present. Reuse project choices that are current and explicit. Ask for a missing required deployment value instead of guessing.

Do not read or print credential values merely to confirm configuration. The MCP server receives credentials through its secrets file or environment. Treat image and pool values in examples as placeholders unless the project or administrator explicitly selected them.

Choose the task kind according to the work:

| Kind | Use it for |
| --- | --- |
| `command` | A finite, non-interactive run such as evaluation, conversion, or a build |
| `shell` | Interactive debugging that needs a reconnectable environment |
| `experiment` | Training, searches, trials, or long-running work that uses Determined experiment features |

## Understand the three path namespaces

| Namespace | Used by | Example role |
| --- | --- | --- |
| Container path | `output_dir`, `code.repo` for `git`, `code.dir` for `path`, `storage_check.path`, and the `shared_dir` of transfers | Path visible inside a Determined task |
| Cluster-agent host path | `mounts[].host_path` in the policy | Path the administrator mounts on every agent |
| MCP-server local path | `code.repo` for `context`, and the `local_dir` of transfers | Absolute path on the machine running the MCP server |

The policy maps container paths to cluster-agent host paths. The optional storage configuration maps those host paths to a local mount or reaches them through SSH. The machine showing the chat can differ from the machine running the MCP server, so never infer a local path from what is visible in the UI. `workdir` is relative to the code root.

## Choose how code reaches the task

| Source | Use it when | What the plan pins |
| --- | --- | --- |
| `git` | The repository is on shared storage and its root is mounted on this machine | The commit; the task clones it and nothing is uploaded |
| `context` | The code is in a local working tree and fits in 99,614,720 bytes | The commit and the manifest of the uploaded files |
| `path` | The code must run in place from a shared directory, for example in a shell | Nothing: the content is `unpinned` |

`git` planning reads the repository through a local mount only in this release; over SSH it returns `storage_not_local`. Commit what the run needs: a `git` commit must be on a branch or tag. Keep data, packages, checkpoints, and outputs on mapped shared storage; do not upload them in a `context`.

## Prepare shared files safely

If the project is already complete on shared storage, a compute-only workflow can continue to plan and launch with Determined authentication alone. When files need staging or client-side verification, configure storage access and then:

1. Call `storage_check(path)` for the destination or its existing parent, and read its `viewpoint`: the permissions are those of the local or SSH user, not of the container user.
2. Call `storage_sync(local_dir, shared_dir, dry_run=true)`.
3. Review the resolved source, destination, backend, exclusions, and itemized changes.
4. Call the identical operation with `dry_run=false` only when that preview is correct.
5. Call `storage_check` again for the prepared inputs.

A transfer copies directory contents and does not delete extra destination files. Without `overwrite`, it keeps every existing file and the attributes of existing directories. See [shared storage access](shared-storage-access.md) for SSH authentication, exclusions, and transfer behavior.

## Plan, review, and launch once

Write a `TaskSpec` with a meaningful `name`, the `kind`, the `command`, the `code` source, an `output_dir` on writable shared storage, and the slot count. The image and pool come from the policy unless an approved override is given.

```json
{
  "kind": "command",
  "name": "evaluate-checkpoint",
  "command": "python scripts/evaluate.py --output \"$COMPUTE_OUTPUT_DIR/metrics.json\"",
  "code": {"source": "git", "repo": "/shared-container/project/repo", "revision": "main"},
  "output_dir": "/shared-container/project/results/evaluate-checkpoint",
  "slots": 1
}
```

Call `compute_plan(spec)`. It pins the revision, applies the policy, and dry-runs the exact request on the master; nothing is created. Review the resolved `spec`, the `commit`, the `code` summary (for `context`, the `included` and `excluded` paths), the `effective_config`, and every warning. `path_not_bind_mounted` means the task cannot reach a path; `secret_like_included` means an include uploads a file that looks like a secret. Master and pool defaults apply as they stand at launch.

Placement is not evaluated before launch. `compute_resources` shows the pools and their device models as a snapshot; a job whose slots exceed what the pool has now waits in the queue. Do not switch pools or slot counts silently; that is a workload decision.

Call `compute_launch(spec, request_id, request_digest)` with the values the plan returned; pass the returned `spec`, not the original. Keep the `job_id` and `request_id` in the work record.

- After a timeout, a lost answer, or `unavailable`, repeat the identical launch with the same `spec`, `request_id`, and `request_digest`. It returns the same job with `replayed: true`, even if the working tree changed since, and never creates a second one. Or find the job in `compute_list` by its `request_id`; the list covers every client of the account.
- If it returns `internal`, repeat it once; a second identical error means nothing was created.
- If it returns `plan_changed`, nothing was created: the code or request moved since the plan. Plan again and review the new commit before launching.
- Never resubmit with a new `request_id` after a timeout or any other uncertain outcome. A new plan mints a new key, so the master would create a second job beside the one that may already exist.

## Monitor and accept the result

Call `compute_status(job_id)` until the job ends, and use `compute_logs(job_id, tail=...)` to inspect progress and final messages; for an experiment, `trial_id` selects a trial. The `explanation` interprets the state: an active experiment whose trials wait for resources reads `running`, and the explanation says it waits for the scheduler. Cancel a job with `compute_cancel(job_id)` when it is no longer needed.

Call `compute_usage(job_id)` to see how much CPU, memory, and GPU a job uses, for example to spot idle GPUs before proposing a resize or cancellation. It is read-only; `measurement: "unmeasured"` means the master has no task-resources integration, not that the job is idle. Inspect `warnings` first. A null or missing value means no measurement, never zero, and an empty `series` means no data for the window. GPU metrics cover the whole assigned device. An experiment reports its latest trial unless `trial_id` or `allocation_id` selects another. `gpus` compares each allocation's GPUs: a large `utilization_spread_percent`, a low mean on `least_utilized_gpu_uuid`, or a high `idle_fraction` points to idle or straggling GPUs. `trial.batches_per_second_lower_bound` is a lifetime floor, and a `total_batches_processed` of 0 is expected when the workload does not report through Determined's Core API. See [task usage measurements](compute-service.md#task-usage-measurements).

A launched job or an ended state alone is not acceptance. Check the job's `exit_class`, the logs, and the success criteria defined at the start. A failed prelude (code delivery, `output_dir`, or `workdir`) prints a line starting with `compute:` and classifies as `workload_failed`. When storage access is configured, verify expected shared artifacts with `storage_check`; when a local copy is needed, preview `storage_fetch(shared_dir, local_dir, dry_run=true)`, review it, then execute with `dry_run=false`.

Report the job ID, request ID, final state, exit class, output path, and observed artifact or metric. Never include tokens, passwords, private keys, cookies, or secrets-file contents.

## Report failures as failures

A job may fail, but a failure is never reported as success, and work whose outcome is uncertain is never run again silently.

- Report a `failed` or `canceled` job as such, with its `exit_class`, `exit_reason`, and the log lines that show the cause, such as a `compute:` line from the prelude. An ended job is not a finished task, and an output that was not checked is not a result.
- Never relaunch automatically, whether after a failure, a cancel, `plan_changed`, or an uncertain launch. Repeating the identical launch is not a relaunch: it returns the existing job. A new run is a new plan with a new `request_id`, reviewed, and the user decides on it.
- Nothing retries a failed job for you, except the restarts an experiment's own `max_restarts` allows.
- When a request is refused, for example `admission_unsupported`, `protocol_unsupported`, or a policy or code check, stop and report the refusal. Do not work around it with another pool, code source, or tool.

## Keep identity boundaries separate

The authenticated Determined account owns every job it launches, and the master keeps the record. There is no local database or owner namespace: any client of the same account sees the same jobs, and a `request_id` belongs to that account's jobs. Under basic authorization, only a job's owner or an administrator can cancel it, so use the account that launched the job. Secrets written into a command or `env` are stored in the job's config and visible to its readers; keep them in files on shared storage instead.
