# Agent workflow

[English](agent-workflow.md) | [简体中文](agent-workflow.zh.md)

[Home](../README.md) · [Compute reference](compute-service.md) · [Storage access](shared-storage-access.md) · [Troubleshooting](troubleshooting.md)

This workflow is for any agent or client that can call the local stdio MCP tools. The client selects its own model. Normal storage and compute work does not require Codex, a repository skill, or server-side consultation.

## Describe the goal and success criteria

State what should run and what observable result will count as success. Include the project revision, input and output locations, expected artifact or metric, and known resource needs. Refer to a credential file or SSH alias, never credential values.

The following deployment inputs must come from the cluster administrator or the project's existing configuration; do not invent them:

- Determined API URL and account credentials
- approved image and resource pool
- cluster-agent host paths and their container mount paths
- optional local mount or login-node SSH access to shared storage

A useful request is: “Evaluate this revision with one slot, avoid queuing, write `metrics.json` under the shared results directory, and report the task IDs, exit result, and whether that file exists.”

## Read local configuration first

Read `AGENTS.md`, the project's own instructions, the configured compute profile, the relevant request example, and the storage-access configuration when present. Reuse project choices that are current and explicit. Ask for a missing required deployment value instead of guessing.

Do not read or print credential values merely to confirm configuration. The MCP server receives credentials through its secrets file or environment. Treat image and pool values in examples as placeholders unless the project or administrator explicitly selected them.

Choose the task kind according to the work. The four kinds, from the simplest:

- `command` runs your command once in a container and ends when it exits. Use it for finite, non-interactive work such as an evaluation, a conversion, or a build.
- `shell` gives you a container to connect to over SSH instead of a command to run. Use it for interactive debugging and environment inspection.
- `generic` runs your command once like a `command`, and can also be paused to free its slots and resumed later under the same task ID. Resuming starts the command again from the beginning in a new container, and nothing restarts it after a failure, so use it only for long batch work that is safe to rerun; see [Pause and resume](#pause-and-resume). It requires the research-cluster fork 0.40.1 or later of the Determined master, and `kind: auto` never selects it.
- `experiment` runs your command as one or more trials and adds Determined's experiment features:
  - a searcher, set in `experiment_config.searcher`, that runs a single trial or many trials over a hyperparameter space (grid, random, or adaptive search that stops weak trials early);
  - automatic restarts: a failed trial, including one whose agent was lost, starts again up to `max_restarts` times (Determined's default is 5);
  - checkpoints that the workload saves through Determined's Core API, kept in `checkpoint_storage` under its retention policy (`save_trial_best`, `save_trial_latest`), so a restarted trial can continue from its latest checkpoint instead of from the start;
  - training and validation metrics that the workload reports through the Core API, which the searcher compares and `compute_status` reports as trial progress and summary metrics;
  - pause and resume: pausing asks each trial to save a checkpoint and stop, and resuming continues each trial from its latest checkpoint.

  A workload that does not use the Core API still gets the searcher, the restarts, and pause and resume, but a restart or a resume then runs it from the beginning, and it reports no checkpoints or metrics. Use an experiment for training, hyperparameter searches, and long or overnight work that should survive a node failure.

The MCP does not accept `kind: notebook`.

## Understand the three path namespaces

| Namespace | Used by | Example role |
| --- | --- | --- |
| Container path | `workdir`, `output_dir`, `storage_check.path`, and `storage_sync`/`storage_fetch.shared_dir` | Path visible inside a Determined task |
| Cluster-agent host path | `mounts[].host_path` and shared-fs checkpoint configuration | Path mounted by the Determined agent; supplied by deployment configuration |
| MCP-server local path | `storage_sync.local_dir` and `storage_fetch.local_dir` | Absolute path on the machine running the MCP server |

The compute profile maps container paths to cluster-agent host paths. The optional storage configuration maps those host paths to a local mount or reaches them through SSH. The machine showing the chat can differ from the machine running the MCP server, so never infer a `local_dir` from what is visible in the UI.

Keep source, data, packages, checkpoints, and outputs on mapped shared storage. `workdir` and `output_dir` must use writable container paths. Do not send a source archive or project upload through Determined.

## Prepare shared files safely

If the project is already complete on shared storage and the caller supplied its paths, a compute-only workflow can continue to plan and launch using Determined authentication; it does not need a local mount, SSH login, or storage configuration. When storage access is configured, use `storage_check` to verify the relevant container paths. If files need staging or direct client-side verification, configure storage access and then:

1. Call `storage_check(path)` for the destination or its existing parent.
2. Call `storage_sync(local_dir, shared_dir, dry_run=true)`.
3. Review the resolved source, destination, backend, exclusions, and itemized changes.
4. Call the identical operation with `dry_run=false` only when that preview is correct.
5. Call `storage_check` again for the prepared working directory and required inputs.

A transfer copies directory contents and does not delete extra destination files. It can replace same-named files, so the preview is part of the safety check. Without a storage backend, planning still does not verify remote file existence or permissions; make the workload validate required inputs and write an observable result. See [shared storage access](shared-storage-access.md) for SSH authentication, exclusions, and transfer behavior.

## Check capacity and avoid accidental queues

Call `compute_resources(slots, pool)` with the requested pool and slot count. A zero-slot command still needs the auxiliary-capacity check. Capacity is a current snapshot, not a reservation.

Keep `allow_queue: false` unless the user explicitly wants the task to wait in a queue. If capacity is unavailable or unknown, report that result. Do not silently switch pools, change the slot count, or enable queuing.

## Plan, review, and launch once

Create a request with a meaningful `name` and `description`, the selected `kind`, command, container `workdir`, container `output_dir`, slot count, `allow_queue`, and a revision or content identifier when available. The image and pool may come from the compute profile or explicit approved overrides.

```json
{
  "name": "evaluate-checkpoint",
  "description": "Evaluate the selected checkpoint and write metrics to shared storage.",
  "kind": "command",
  "command": ["bash", "-lc", "python scripts/evaluate.py --output \"$COMPUTE_OUTPUT_DIR/metrics.json\""],
  "workdir": "/shared-container/project/repo",
  "output_dir": "/shared-container/project/results",
  "slots": 1,
  "code_revision": "REVISION_OR_CONTENT_ID",
  "allow_queue": false
}
```

Call `compute_plan(request)` and inspect the resolved kind, image, pool, mounts, working directory, output directory, resource fields, and advisories. Planning validates and renders locally; it does not prove that remote files, permissions, credentials, or live capacity are valid.

Generate one stable, caller-controlled `request_id`, then call `compute_launch(request, request_id)`. Preserve the returned local `task_id` and remote ID in the work record. Repeating an identical request with the same request ID is idempotent; reusing it for different content is rejected.

If the launch result is uncertain, do not create a new request ID or submit again. Inspect the local task and remote system. Use `compute_reconcile(task_id, remote_id)` only when repairing that same uncertain local submission and after identifying the matching remote task. See [troubleshooting](troubleshooting.md#submission-outcome-is-uncertain).

## Monitor and accept the result

Call `compute_status(task_id)` until the task reaches a terminal state, and use `compute_logs(task_id, tail)` to inspect progress and the final messages. Cancel a running task with `compute_cancel(task_id)` when the user no longer needs it.

Call `compute_usage(task_id)` when you need to know how much CPU, memory, and GPU a running job uses, for example to spot near-zero GPU utilization or an idle allocation before proposing a resize, cancellation, or relaunch; for an ended task it reports the window before the task ended. It is read-only and requires the master's task-resources integration; `task_resources_disabled` or `task_resources_unsupported` means measurements are unavailable, not that the task is idle. Inspect `warnings` first. A null or missing value means no measurement, never zero, and an empty `series` list means no data for the window. Values are point samples taken every `step` seconds, and GPU metrics cover the whole assigned device, which can include other processes. An experiment reports its latest trial unless `trial_id` is given. `gpus` compares each allocation's GPUs even when `metrics` hides their series: a large `utilization_spread_percent`, a low mean on `least_utilized_gpu_uuid`, or a high `idle_fraction` points to idle or straggling GPUs, and a `gpu_count` below `requested_slots` means fewer GPUs returned a series than the allocation holds, not necessarily that the rest are unused. For an experiment, `trial.batches_per_second_lower_bound` is a lifetime floor, because its wall-clock denominator can also count image pull, startup, initialization, and allocations lost to restarts (not scheduler queue time or gaps between allocations); a `total_batches_processed` of 0 is expected when the workload does not report through Determined's Core API. Report what you observe; changing slots or pools remains an explicit workload decision. See [task usage measurements](compute-service.md#task-usage-measurements).

A successful submission or a terminal state alone is not acceptance. Check the process exit information and the success criteria defined at the start. When storage access is configured, verify expected shared artifacts with `storage_check`; otherwise use workload output or another explicit task-level check. When a local copy is needed, configure storage access, preview `storage_fetch(shared_dir, local_dir, dry_run=true)`, review it, then execute with `dry_run=false` and inspect the fetched result.

Report the local task ID, remote ID, final state, exit result when available, output path, and observed artifact or metric. Never include tokens, passwords, private keys, cookies, or secrets-file contents.

## Pause and resume

Pausing frees a task's slots without ending it: the task keeps its ID and can be resumed later. Experiments and generic tasks can be paused; commands and shells cannot, and return `unsupported_kind`.

A pause asks the workload to stop through Determined's Core API preemption signal and stops its containers when the task's `preemption_timeout` ends. For an experiment the timeout defaults to one hour, so that each trial can save a checkpoint and exit; for a generic task it defaults to 0, an immediate stop. A plain script that does not use the Core API is stopped when the timeout ends.

Resuming continues differently by kind:

- An experiment continues each trial from its latest checkpoint; a trial without one starts from the beginning.
- A generic task starts a new container under the same task ID and runs the command again from the beginning. Write its command so that it can be stopped at any moment and started again: process work in units, write each unit's output under a temporary name and rename it when complete, skip units whose final output already exists, and remove or redo partial ones on start. Its child tasks are paused with it unless they set `no_pause: true`, and a task launched with `no_pause: true` cannot be paused.

To pause and resume:

1. Call `compute_pause(task_id)`.
2. Poll `compute_status(task_id)` until `remote_state` is `STATE_PAUSED`. A paused task is not finished. A generic task reports `STATE_STOPPING_PAUSED` while it stops; an experiment reports `STATE_PAUSED` as soon as the pause is accepted, and its trials can take until their timeout to stop.
3. Call `compute_resume(task_id)` when the work should continue, and check `compute_logs` that it continued from a checkpoint or skipped completed units.

A pause or resume that the master refuses, for example pausing a paused task, returns the master's reason as an error, and nothing changed. A master that predates the generic-task fixes of the research-cluster fork reports refused generic-task requests as server errors instead; these arrive as `submission_uncertain`, so check `compute_status` before trying again.

`compute_cancel` kills a generic task and all its descendants. Exit status 0 ends a generic task as `STATE_COMPLETED`; a non-zero exit or a lost agent ends it as `STATE_ERROR`, and it is not restarted.

Give a generic task a meaningful `name` and `description` as for any launch. A master that predates generic task names rejects them; the service then submits the task without them once, keeps them in the local record, and returns a `generic_task_metadata_unsupported` warning. Treat that warning as informational. The task then appears without a name in the WebUI, so record the local and remote IDs.

## Discover and adopt existing remote tasks

Use discovery and adoption for a task created independently through the Determined WebUI, native CLI, or another device under the same Determined account:

1. Call `compute_discover(kind, limit=50, offset=0)` with `command`, `shell`, or `experiment`. This is a read-only remote query; it does not create a local record or submit work. Generic tasks cannot be discovered or adopted, because Determined does not report which account owns them.
2. Select the intended remote result, then call `compute_adopt(kind, remote_id)`.
3. Keep the returned local `task_id` and use it with `compute_status`, `compute_logs`, `compute_usage`, and `compute_cancel`.

Adoption verifies the actual cluster, current authenticated account, and remote owner. It creates an idempotent local record and never relaunches the remote task. Unknown work paths, output paths, or revisions remain unknown. Adoption does not grant storage access or new cluster permissions.

Reconciliation has a narrower purpose: `compute_reconcile` repairs an existing local submission whose remote acceptance is uncertain by verifying its submission marker. It does not import independently created tasks. If an uncertain local record exists, reconcile it rather than adopting the corresponding remote task.

## Keep identity boundaries separate

Four values participate in task identity and access:

| Value | Meaning |
| --- | --- |
| SQLite database | Local durable task records, idempotency, and reconciliation state |
| `owner` | Namespace within that database; it is not authentication |
| Determined account | API identity and remote authorization selected by credentials |
| Cluster identity | Actual remote cluster used to prevent cross-cluster task confusion |

Sessions share local records only when they use the same database and owner. Separate databases can adopt the same remote task independently. Keep the database on local durable disk rather than shared NFS. Sharing an owner does not share credentials, and changing credentials does not rename the owner namespace.

On the Determined fork 0.40.1 or later with basic authorization, only a task's Determined owner or an administrator can cancel it. A submitted record binds to the profile and endpoint rather than the account, so after credentials switch to another account, `compute_cancel` can return HTTP 403 for a command or shell and HTTP 404 for an experiment; an adopted record reports `ownership_mismatch` instead. Use the account that owns the task.

## Optional consultation

The client agent can perform this workflow directly. Server-side consultation defaults to `none` and is not needed for any deterministic tool. A deployment may enable the separate read-only Codex backend and configure its model; that model is independent of the MCP client's model. Consultation can return advice but cannot launch, cancel, transfer files, or use the caller's MCP tools. See [optional consultation](consultation.md).
