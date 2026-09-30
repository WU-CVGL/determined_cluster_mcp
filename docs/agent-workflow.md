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

Choose the task kind according to the work:

| Kind | Use it for |
| --- | --- |
| `command` | A finite, non-interactive run such as evaluation, conversion, or a build |
| `shell` | Interactive debugging that needs a reconnectable environment |
| `experiment` | Training, searches, trials, or long-running work that uses Determined experiment features |

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

For a durable run of committed code, prefer `storage_snapshot(repo_dir, revision)` when the deployment configures `snapshots.root`: preview it, review the excluded secret-like files and warnings, then repeat it with `dry_run=false` and put its `request_fields.workdir` and `request_fields.code_revision` into the request. When an include supplies any file, `code_revision` is `<commit>+<snapshot_key>`, which names the snapshot manifest. The manifest also records exclusions, so this value can change while the content stays the same; retry a launch with the recorded request unchanged rather than with the fields of a new snapshot. Identical content is published once and reused by later jobs. The snapshot directory is read-only, so the workload must write under `output_dir`. Use `include` for generated or untracked files the job needs. See [publish a code snapshot](shared-storage-access.md#publish-a-code-snapshot).

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

Call `compute_plan(request)` and inspect the resolved kind, image, pool, mounts, working directory, output directory, resource fields, advisories, and `path_checks`. `path_checks` reports whether each bind mount, the working directory, an experiment's checkpoint directory, and the output directory exist when the MCP server can see them; `unverified` means it cannot see that path, not that the path is missing. A missing required path fails with `path_not_found` and names it in `details.missing_paths`. An experiment checkpoint directory is bind-mounted before the entrypoint runs, so when it does not exist yet, add `"create_directories": ["checkpoint_storage"]` (and `"output_dir"` if wanted); launch then creates it before submitting. Planning does not prove that permissions, credentials, or live capacity are valid.

When the workload needs a particular GPU model, driver, count, or free-memory floor, add `gpu_admission`, for example `{"names": ["APPROVED_GPU_NAME*"], "min_free_mib": 16384}`, with values from the project or administrator. The task then checks the GPUs that `nvidia-smi` reports inside the container before the workload starts. An experiment with more than one slot also needs `experiment_config.resources.is_single_node: true`. See [GPU admission](compute-service.md#gpu-admission).

Generate one stable, caller-controlled `request_id`, then call `compute_launch(request, request_id)`. Preserve the returned local `task_id` and remote ID in the work record. Repeating an identical request with the same request ID is idempotent; reusing it for different content is rejected.

If the launch result is uncertain, do not create a new request ID or submit again. Inspect the local task and remote system. Use `compute_reconcile(task_id, remote_id)` only when repairing that same uncertain local submission and after identifying the matching remote task. See [troubleshooting](troubleshooting.md#submission-outcome-is-uncertain).

## Monitor and accept the result

Call `compute_status(task_id)` until the task reaches a terminal state, and use `compute_logs(task_id, tail)` to inspect progress and the final messages. Cancel a running task with `compute_cancel(task_id)` when the user no longer needs it.

Call `compute_usage(task_id)` when you need to know how much CPU, memory, and GPU a running job uses, for example to spot near-zero GPU utilization or an idle allocation before proposing a resize, cancellation, or relaunch; for an ended task it reports the window before the task ended. It is read-only and requires the master's task-resources integration; `task_resources_disabled` or `task_resources_unsupported` means measurements are unavailable, not that the task is idle. Inspect `warnings` first. A null or missing value means no measurement, never zero, and an empty `series` list means no data for the window. Values are point samples taken every `step` seconds, and GPU metrics cover the whole assigned device, which can include other processes. An experiment reports its latest trial unless `trial_id` is given. `gpus` compares each allocation's GPUs even when `metrics` hides their series: a large `utilization_spread_percent`, a low mean on `least_utilized_gpu_uuid`, or a high `idle_fraction` points to idle or straggling GPUs, and a `gpu_count` below `requested_slots` means fewer GPUs returned a series than the allocation holds, not necessarily that the rest are unused. For an experiment, `trial.batches_per_second_lower_bound` is a lifetime floor, because its wall-clock denominator can also count image pull, startup, initialization, and allocations lost to restarts (not scheduler queue time or gaps between allocations); a `total_batches_processed` of 0 is expected when the workload does not report through Determined's Core API. Report what you observe; changing slots or pools remains an explicit workload decision. See [task usage measurements](compute-service.md#task-usage-measurements).

With `gpu_admission`, the logs contain one `determined-compute gpu_admission: passed|failed ...` line and `output_dir` contains the JSON receipt and its `.jsonl` history. A failed policy exits with code 86 before the workload starts, but a workload can also exit 86 by itself, so treat the exit code only as a hint: confirm a failed admission by the `determined-compute gpu_admission: failed` log line or by the `.jsonl` record whose `determined.allocation_id` matches the task's allocation (`compute_usage` lists each allocation's `allocation_id` and `exit_reason`). Report that record's `failures`, because the `.json` receipt holds only the latest record and may belong to another trial or task that shares `output_dir`. Do not change the policy or pool without an explicit decision. In an experiment, each failed admission consumes a restart.

A successful submission or a terminal state alone is not acceptance. Check the process exit information and the success criteria defined at the start. When storage access is configured, verify expected shared artifacts with `storage_check`; otherwise use workload output or another explicit task-level check. When a local copy is needed, configure storage access, preview `storage_fetch(shared_dir, local_dir, dry_run=true)`, review it, then execute with `dry_run=false` and inspect the fetched result.

Report the local task ID, remote ID, final state, exit result when available, output path, and observed artifact or metric. Never include tokens, passwords, private keys, cookies, or secrets-file contents.

## Discover and adopt existing remote tasks

Use discovery and adoption for a task created independently through the Determined WebUI, native CLI, or another device under the same Determined account:

1. Call `compute_discover(kind, limit=50, offset=0)` with `command`, `shell`, or `experiment`. This is a read-only remote query; it does not create a local record or submit work.
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

A submitted record is also bound to the compute profile and endpoint that submitted it. Status, logs, and usage stay available read-only from another profile on the same endpoint and cluster label: the service first verifies the task's owner and submission marker, and the result's `binding.mode` is `cross_profile` with `mutations_allowed: false`. Cancellation and reconciliation still require the original profile and return `binding_mismatch` otherwise; a launch retry with a changed profile returns `idempotency_conflict` because the profile fingerprint is part of the request hash. Use the original profile to cancel, reconcile, or retry. A cross-profile read returns `cross_profile_unverifiable` when Determined no longer serves the task's entity. For a command or shell more than 24 hours after it ended or after a master restart, read its logs and usage with the original profile; an experiment in this state was deleted, together with its logs, or is not visible to the account. `compute_list_tasks` shows each record's offline `binding`.

Sessions share local records only when they use the same database and owner. Separate databases can adopt the same remote task independently. Keep the database on local durable disk rather than shared NFS. Sharing an owner does not share credentials, and changing credentials does not rename the owner namespace.

On the Determined fork 0.40.1 or later with basic authorization, only a task's Determined owner or an administrator can cancel it. A submitted record binds to the profile and endpoint rather than the account, so after credentials switch to another account, `compute_cancel` can return HTTP 403 for a command or shell and HTTP 404 for an experiment; an adopted record reports `ownership_mismatch` instead. Use the account that owns the task.

## Optional consultation

The client agent can perform this workflow directly. Server-side consultation defaults to `none` and is not needed for any deterministic tool. A deployment may enable the separate read-only Codex backend and configure its model; that model is independent of the MCP client's model. Consultation can return advice but cannot launch, cancel, transfer files, or use the caller's MCP tools. See [optional consultation](consultation.md).
