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

A useful request is: “Evaluate this revision with one slot, avoid queuing, write `metrics.json` under the shared results directory, and report the task IDs, exit result, and whether that file exists.”

## Read local configuration first

Read `AGENTS.md`, the project's own instructions, the configured compute profile, the relevant request example, and the storage-access configuration when present. Reuse project choices that are current and explicit. Ask for a missing required deployment value instead of guessing.

Do not read or print credential values merely to confirm configuration. The MCP server receives credentials through its secrets file or environment. Treat image and pool values in examples as placeholders unless the project or administrator explicitly selected them.

Choose the task kind according to the work. The four kinds, from the simplest:

- `command` runs your command once in a container and ends when it exits. Use it for finite, non-interactive work such as an evaluation, a conversion, or a build.
- `shell` gives you a container to connect to over SSH instead of a command to run. Use it for interactive debugging and environment inspection.
- `generic` runs your command once like a `command`, with a name, child tasks, and, if launched with `pausable: true`, pause and resume under the same task ID to free its slots. Resuming starts the command again from the beginning in a new container, and nothing restarts it after a failure, so make a task pausable only when it is safe to rerun; see [Pause and resume](#pause-and-resume). It requires a Determined master from the research-cluster fork with WU-CVGL/determined#27, which lists generic tasks with their owners; on an older master the launch fails with `unsupported` before anything is created. `kind: auto` never selects it.
- `experiment` runs your command as one or more trials and adds Determined's experiment features:
  - a searcher, set in `experiment_config.searcher`, that runs a single trial or many trials over a hyperparameter space (grid, random, or adaptive search that stops weak trials early);
  - automatic restarts: a failed trial, including one whose agent was lost, starts again up to `max_restarts` times (Determined's default is 5);
  - checkpoints that the workload saves through Determined's Core API, kept in `checkpoint_storage` under its retention policy (`save_trial_best`, `save_trial_latest`), so a restarted trial can continue from its latest checkpoint instead of from the start;
  - training and validation metrics that the workload reports through the Core API, which the searcher compares and `compute_usage` reports as trial progress and summary metrics;
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

A transfer copies directory contents and does not delete extra destination files. It can replace same-named files, so the preview is part of the safety check. Stage each revision of work that can restart or resume in its own directory and do not sync into it afterwards; a restart or resume runs whatever that directory then holds, while `code_revision` still names the original revision. Without a storage backend, planning still does not verify remote file existence or permissions; make the workload validate required inputs and write an observable result. See [shared storage access](shared-storage-access.md) for SSH authentication, exclusions, and transfer behavior.

## Check capacity and avoid accidental queues

Use `compute_resources(slots, pool)` when choosing resources, answering a capacity question, or investigating a capacity rejection. Positive slots check schedulable agent slots; zero slots check auxiliary-container capacity. The result is a snapshot, not a reservation. With `allow_queue: false`, `compute_launch` performs this admission check before submitting, so a separate capacity query is not required for every launch.

Keep `allow_queue: false` unless the user explicitly wants the task to wait in a queue. If capacity is unavailable or unknown, or the pool is not present or not available to you, report that result; a `permission_denied` error that names a pool means the account may not use it. Do not silently switch pools, change the slot count, or enable queuing.

## Plan, review, and launch once

Create a request with a meaningful `name` (at most 128 characters) and `description` (at most 2,048), the selected `kind`, command, container `workdir`, container `output_dir`, slot count, `allow_queue`, and a revision or content identifier when available. The image and pool may come from the compute profile or explicit approved overrides. An overlong name or description is rejected with `invalid_request` before submission; shorten it and submit the corrected request.

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

Call `compute_plan(request)` and inspect the resolved kind, image, pool, mounts, working directory, output directory, resource fields, and advisories. Planning validates and renders locally; it does not prove that remote files, permissions, credentials, pool access, or live capacity are valid.

Call `compute_launch(request)` once. It returns the task's `kind` and `id`, Determined's own task ID: a UUID for a command, shell, or generic task and an integer for an experiment. Every later tool takes this pair. The MCP keeps no record of the launch, so write the kind, ID, name, and `submission_marker` into your own work record. Every call is a new submission: calling `compute_launch` again with the same request starts a second task.

If the launch returns `submission_uncertain`, the submission is unconfirmed: Determined may or may not have created the task, and it may still appear. Do not launch again automatically. Call `compute_list(kind, marker=...)` with the `submission_marker` from the error details and a small `limit`. One returned task is most likely your submission; continue with its ID. Several returned tasks share a copied config; show them to the user instead of choosing one. An empty result does not prove that the submission failed, because each search covers one page and the master may store the task later. Report the unconfirmed launch and leave the decision to submit again to the user. See [troubleshooting](troubleshooting.md#submission-outcome-is-uncertain).

## Monitor and accept the result

Call `compute_status(kind, id)` until the task reaches a terminal state, and use `compute_logs(kind, id, tail)` to inspect progress and the final messages. Cancel a running task with `compute_cancel(kind, id)` when the user no longer needs it. To check many experiments or generic tasks at once, call `compute_list(kind, states=["STATE_ACTIVE"])`: it lists the account's active ones, queued or running, one page at a time; follow `pagination.next_offset` until it is null. A task that drops off this list may be paused or stopping rather than ended; confirm a terminal state with `compute_status(kind, id)` before accepting the result.

While a task has not ended, `compute_status` also returns `queue`, its job in the pool's queue. `jobs_ahead` is the job's position in that queue, the number of jobs ranked ahead of it, which can include running jobs; it is not a prediction of wait time, and it is `null` when the pool's scheduler does not rank jobs. When reporting a queued task, quote its `jobs_ahead`. A scheduled job's `placement` names each agent and the slot device IDs it holds there; only for NVIDIA GPU slots are these the `nvidia-smi` index. `queue: null` with a `queue_note` means the job was not found in that queue; with `context_unavailable: ["queue"]` it means the lookup failed, which never shows that the task is not queued. In every case `state` remains the authority. See [status](compute-service.md#status-logs-and-cancellation).

Call `compute_usage(kind, id)` when you need to know how much CPU, memory, and GPU a running job uses, for example to spot near-zero GPU utilization or an idle allocation before proposing a resize, cancellation, or relaunch; for an ended task it reports the window before the task ended. It is read-only and requires the master's task-resources integration; `task_resources_disabled` or `task_resources_unsupported` means measurements are unavailable, not that the task is idle. Inspect `warnings` first. A null or missing value means no measurement, never zero, and an empty `series` list means no data for the window. Values are point samples taken every `step` seconds, and GPU metrics cover the whole assigned device, which can include other processes. An experiment reports its latest trial unless `trial_id` is given. `gpus` compares each allocation's GPUs even when `metrics` hides their series: a large `utilization_spread_percent`, a low mean on `least_utilized_gpu_uuid`, or a high `idle_fraction` points to idle or straggling GPUs, and a `gpu_count` below `requested_slots` means fewer GPUs returned a series than the allocation holds, not necessarily that the rest are unused. For an experiment, `trial.batches_per_second_lower_bound` is a lifetime floor, because its wall-clock denominator can also count image pull, startup, initialization, and allocations lost to restarts (not scheduler queue time or gaps between allocations); a `total_batches_processed` of 0 is expected when the workload does not report through Determined's Core API. Report what you observe; changing slots or pools remains an explicit workload decision. See [task usage measurements](compute-service.md#task-usage-measurements).

A successful submission or a terminal state alone is not acceptance. Check the process exit information and the success criteria defined at the start. When storage access is configured, verify expected shared artifacts with `storage_check`; otherwise use workload output or another explicit task-level check. When a local copy is needed, configure storage access, preview `storage_fetch(shared_dir, local_dir, dry_run=true)`, review it, then execute with `dry_run=false` and inspect the fetched result.

Report the task kind and ID, final state, exit result when available, output path, and observed artifact or metric. Never include tokens, passwords, private keys, cookies, or secrets-file contents.

## Pause and resume

Pausing frees a task's slots without ending it: the task keeps its ID and can be resumed later. Experiments and generic tasks launched with `pausable: true` can be paused; commands and shells return `unsupported_kind`, and pausing a generic task that is not pausable fails and leaves it running.

A pause asks the workload to stop through Determined's Core API preemption signal and stops its containers when the task's `preemption_timeout` ends. For an experiment the timeout defaults to one hour, so that each trial can save a checkpoint and exit; for a generic task it defaults to 0, an immediate stop. A plain script that does not use the Core API is stopped when the timeout ends.

Resuming continues differently by kind:

- An experiment continues each trial from its latest checkpoint; a trial without one starts from the beginning.
- A generic task starts a new container under the same task ID and runs the command again from the beginning. Write its command so that it can be stopped at any moment and started again: process work in units, write each unit's output under a temporary name and rename it when complete, skip units whose final output already exists, and remove or redo partial ones on start. Its pausable child tasks are paused with it; children that are not pausable keep running.

To pause and resume:

1. Call `compute_pause(kind, id)`.
2. Poll `compute_status(kind, id)` until `state` is `STATE_PAUSED`. A paused task is not finished. A generic task reports `STATE_STOPPING_PAUSED` while it stops; an experiment reports `STATE_PAUSED` as soon as the pause is accepted, and its trials can take until their timeout to stop.
3. Call `compute_resume(kind, id)` when the work should continue, and check `compute_logs` that it continued from a checkpoint or skipped completed units.

A pause or resume that the master refuses, for example pausing a paused task, returns the master's reason as an error, and nothing changed. A master that predates the generic-task fixes of the research-cluster fork reports refused generic-task requests as server errors instead; these arrive as `submission_uncertain`, so check `compute_status` before trying again.

`compute_cancel` kills a generic task and all its descendants. Exit status 0 ends a generic task as `STATE_COMPLETED`; a non-zero exit or a lost agent ends it as `STATE_ERROR`, and it is not restarted.

Give a generic task a meaningful `name` and `description` as for any launch; Determined stores them, and they appear in the WebUI and in `compute_list`.

## Find existing tasks

`compute_list(kind, limit=50, offset=0)` lists the tasks owned by the authenticated Determined account, newest first, whether they were launched through this MCP, the WebUI, the native CLI, or another device. Each entry has the kind, ID, name, state, resource pool, and start time, and `pagination.next_offset` points to the next page. Use the kind and ID with `compute_status`, `compute_logs`, `compute_usage`, `compute_cancel`, `compute_pause`, and `compute_resume`. Listing is read-only. For experiments and generic tasks, `states` lists only tasks in the given states; see [list tasks](compute-service.md#list-tasks-and-find-a-submission) for the accepted names. Generic tasks need a master with the research-cluster fork's generic task list (WU-CVGL/determined#27); an older master returns `unsupported`.

With `marker`, `compute_list` returns the tasks on the selected page whose config carries that submission marker. It reads each task of the page, so keep `limit` small, such as 5 or 10, when you look for a launch you just made, and follow `pagination.next_offset` to older pages. A marker is a correlation label, not an identity: a config copied outside the MCP carries the same one, so more than one task can match, and an empty page does not show that a task was never created. With `states` too, `states` filters the list, and each returned task shows the state from its own read, which may be newer than the state the filter matched.

## Ownership and records

The Determined account selected by the MCP server's credentials is the only identity. Every tool that acts on a task first checks that this account owns it, and refuses another user's task with `ownership_mismatch`, even when the account is an administrator. A generic task whose owner the master cannot report is refused with `ownership_unavailable`. To act on another account's task, use that account's credentials.

The MCP keeps no task records. Determined keeps the tasks, their logs, and their experiment data; you keep the record of what you launched and why, such as the kind, ID, name, revision, and output path. Determined serves an ended command or shell for only 24 hours after it ends, so read its logs and usage while it is available.

## Local workstation runs

Use the local workstation for a short single-GPU task only when the user has authorized local execution for that work. Keep training, long runs, and heavy CPU or memory use on the cluster. Cluster authentication or capacity errors do not authorize a local fallback.

Use the project's existing Docker command or a direct `docker run` with the workstation's installed GPU runtime. Take the image, environment variables, and `entrypoint` from `compute_plan`'s rendered config, not just the request: only the rendered entrypoint has the service's `mkdir`/`cd` setup step, and only the rendered environment has `COMPUTE_WORKDIR`, `COMPUTE_OUTPUT_DIR`, and, when the request sets `code_revision`, `COMPUTE_CODE_REVISION`. For a direct `docker run`, clear the image entrypoint with `--entrypoint ''`, since Determined replaces it. Pass a list-valued `entrypoint` (commands and generic tasks) as separate arguments in order; pass a string-valued one (experiments) as a single argument to `sh -c`, as Determined does. Map each local source directory to its intended container path, preserving read-only mounts. Check the selected GPU's availability and host memory, then set explicit `--cpus` and `--memory` limits that leave room for other work; set `--memory-swap` equal to `--memory` to disable container swap.

Start the container detached (`docker run -d`) with a unique `--name`, so a shell-tool timeout or an interrupted `docker` client cannot leave a running container you cannot find and stop. Keep the container ID, capture its logs and exit result, and verify the expected outputs in the project's chosen location. If interrupted or abandoning the run, stop that container and confirm it exited. Report the actual GPU, image, revision, and result; a local container has no Determined task ID.
