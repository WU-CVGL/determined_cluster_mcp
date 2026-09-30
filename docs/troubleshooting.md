# Troubleshooting

[English](troubleshooting.md) | [简体中文](troubleshooting.zh.md)

[Home](../README.md) · [Agent workflow](agent-workflow.md) · [Compute reference](compute-service.md) · [Storage access](shared-storage-access.md)

## The MCP server does not start

Check that the client uses the virtual environment's absolute executable path and that every file path in its configuration is absolute. The MCP process requires a readable compute profile, a persistent local database path, and a non-empty owner. The database parent is created automatically, but the process must be able to write there.

```bash
/absolute/path/to/determined_cluster_mcp/.venv/bin/determined-compute-mcp --help
/absolute/path/to/determined_cluster_mcp/.venv/bin/determined-compute --profile /absolute/path/to/profile.yaml plan --request-file /absolute/path/to/request.json
```

The server uses stdout for MCP protocol frames and writes startup errors to stderr. Inspect the MCP client's server log for the exact error. After changing the installation or upgrading the service, restart every MCP process that shares the database so all processes load the same tools and schema. Profile and storage files reject unknown keys, so add a key introduced by an upgrade, such as `snapshots`, only after every process that reads the file runs the new release.

Compute tasks do not need `--storage-config` unless a request uses `create_directories` and its profile host root is not detected as a mount point on this machine; then map that root with `local_mounts`, to itself if the path is the same, or configure SSH. Storage tools automatically use a local shared path when it matches the configured `host_path`. For a custom local mapping or login-node SSH, copy `cfg/storage-access.example.yaml` to `.local/storage.yaml`, edit it, and add `--storage-config /absolute/path/to/.local/storage.yaml`.

## Authentication fails

Confirm that the API URL and credentials refer to the same Determined deployment. The secrets file supports either `DET_API_TOKEN`, or both `DET_USERNAME` and `DET_PASSWORD`:

```dotenv
DET_MASTER=https://determined.example.org
DET_API_TOKEN=replace-with-your-token
```

Do not put credentials in the compute profile, request, owner, task name, or description. Restrict access to the secrets file and inspect only whether required variable names are present, not their values.

The configured `owner` does not select a Determined user. It is only a namespace in the local SQLite database. Remote authorization comes from the API credentials. Therefore changing owner cannot fix an API permission error, and sharing an owner does not share remote permissions.

## TLS certificate verification fails

Use the CA certificate published for the deployment. If it is already installed in the operating system or Python runtime trust store used by the MCP process, no extra CA setting is needed. When the application needs a separate PEM bundle, set Requests' `REQUESTS_CA_BUNDLE` to its absolute path and enable verification:

```bash
export REQUESTS_CA_BUNDLE=/absolute/path/to/organization-ca-bundle.pem
export DET_VERIFY_SSL=true
```

For MCP, keep `--verify-ssl` in the server arguments and pass the CA variable through the stdio client's environment configuration. Adapt the surrounding keys to the client's MCP syntax:

```json
{
  "command": "/absolute/path/to/determined_cluster_mcp/.venv/bin/determined-compute-mcp",
  "args": ["--profile", "/absolute/path/to/profile.yaml", "--db", "/absolute/local/path/to/tasks.sqlite3", "--owner", "your-owner", "--secrets-file", "/absolute/path/to/credentials.env", "--verify-ssl"],
  "env": {
    "REQUESTS_CA_BUNDLE": "/absolute/path/to/organization-ca-bundle.pem"
  }
}
```

A GUI application may not inherit variables exported in a terminal. Configure the variable in the client's MCP environment settings or start the client from an environment that contains it, then restart the MCP server. The CA bundle must be readable by the MCP process.

An unknown issuer is addressed by the correct CA chain. An expired certificate or hostname mismatch must be corrected by the deployment operator; disabling verification does not repair the certificate identity.

## A shared path is rejected or missing

Check which namespace the argument requires. Task `workdir` and `output_dir`, `storage_check.path`, and the `shared_dir` side of transfers use container paths from the compute profile. `mounts[].host_path` is a cluster-agent path. `local_dir` is an absolute path on the machine running the MCP server.

Planning validates configured path boundaries. The CLI and MCP server also report `path_checks` for the bind mounts, working directory, experiment checkpoint directory, and output directory; see [launch-path checks](compute-service.md#launch-path-checks). `path_not_found` means a required path is missing or not a directory through the trusted local view (a `local_mounts` entry, or a profile host root that is mounted on the MCP machine); `details.missing_paths` names it. If the path exists on the cluster, check that the local mount or `local_mounts` entry shows the cluster's filesystem. Create it, correct the request, or, for an output or experiment checkpoint directory, add `create_directories`. A missing experiment checkpoint `host_path` otherwise makes the task fail before its container starts, because Determined bind-mounts it at startup.

An `unverified` status never fails and means only that the client could not decide. Rely on `status`; `reason` is open-ended. Common reasons are `not_locally_visible` (the host root is not mounted on the MCP machine; add a `local_mounts` entry if it is mounted elsewhere), `local_mount_unavailable` (the `local_mounts` entry covering the path is missing or unreadable; remount it or fix the entry), `local_view_unconfirmed` (a same-named host root exists locally but is not detected as a mount point on this machine; a same-filesystem bind mount and a directory below a mount are not detected, so if such a root really is the cluster's filesystem, map it to itself with `local_mounts`), `ssh_only_access`, `permission_denied`, `timeout` (the filesystem did not answer within 10 seconds), `storage_config_unavailable` (the storage configuration could not be loaded; run `storage_check` or fix the file), `invalid_storage_path` (the local view resolves through a symlink outside its mapped root), and `os_error:<ERRNO>` such as `os_error:ESTALE`. `create_directories` then needs a trusted local view or SSH access; otherwise launch returns `configuration_required` before claiming a task record. `storage_timeout` from launch means a directory to create, or its creation, did not answer within its deadline: 10 seconds through the local view, the storage `timeout_seconds` for creation over SSH. Nothing was recorded or submitted, so the error is retryable; retry with the same `request_id` once the filesystem responds. When the shared root itself is not writable but a subdirectory is, map that subdirectory with `local_mounts`.

Use `storage_check` when the MCP server has configured local or SSH access. If the workload is already present on shared storage and no client-side inspection or transfer is needed, compute operations can proceed without a storage configuration.

A `read_only: true` mount permits reads and fetches but rejects a working directory, output directory, checkpoint destination, or sync target beneath that mount. See [shared storage access](shared-storage-access.md) for local mappings, SSH host keys, authentication, and rsync requirements.

## SSH storage access fails

Use a login-node `Host` alias that can access the configured cluster-agent host paths. A gateway belongs in `ProxyJump`; it is not the storage endpoint. Complete the first interactive connection and verify the host key before automation.

With `auth: openssh`, the service inherits an existing agent. The stdio MCP process must inherit a usable `SSH_AUTH_SOCK`; a GUI client started earlier may not have it. With password or keyring authentication, follow the credential placement rules in [shared storage access](shared-storage-access.md#authentication-choices).

Always repeat the dry run after correcting SSH, path, or permission errors. Do not turn a failed preview into an executed transfer without reviewing the new resolved endpoints and itemized changes.

## GPU admission failed (exit code 86)

A task with `gpu_admission` exits with code 86 before the workload starts when the GPUs that NVML reports inside the container do not satisfy the policy. A workload can also exit 86 by itself, so exit code 86 alone is only a hint: an admission failure is confirmed by one `determined-compute gpu_admission: failed: ...` log line or by the `.jsonl` record whose `determined.allocation_id` matches the task's allocation (`compute_usage` lists each allocation's `allocation_id` and `exit_reason`). `output_dir` contains the receipt (`gpu-admission.json` by default) with `devices` and `failures`, plus a `.jsonl` line for every attempt. The receipt holds only the latest record from any trial or task that shares `output_dir`, so the matching `.jsonl` record is authoritative. Typical causes are a GPU count that differs from `count`, a GPU name or driver outside the allowed patterns, free memory below `min_free_mib` because another process holds the device, an image without the `nvidia-ml-py` package (`pynvml`), or NVML that cannot initialise because the container has no usable NVIDIA driver library; the `failures` entry names the NVML error. An image without `python3` ends the task with exit code 127 instead, before the workload and without a receipt; the image needs both `python3` and `nvidia-ml-py`. The checks do not apply `CUDA_VISIBLE_DEVICES`, so a count mismatch can also mean that the resource manager restricts GPUs only through that variable. Report the matching record; changing the policy, pool, or slots is an explicit workload decision. In an experiment, each failed admission consumes a restart, so set `max_restarts: 0` to stop after the first failure. See [GPU admission](compute-service.md#gpu-admission).

## Capacity is unavailable or unknown

Run `compute_resources` for the requested pool and slot count. Zero slots check auxiliary-container capacity rather than free GPUs. Capacity is a snapshot, and the service checks it again before a new launch.

With `allow_queue: false`, unavailable or unknown capacity rejects the launch instead of queuing. Do not silently choose a suggested alternative pool, reduce resources, or set `allow_queue: true`; those changes require an explicit workload decision. Authentication or inventory-shape errors are errors, not evidence that capacity exists.

A resource pool that an administrator created dynamically appears in `compute_resources` only once it is Ready. A Pending or Failed pool is absent: the result reports that the pool was not present in the cluster inventory with unknown availability, and a launch without queuing is rejected with `capacity_unknown`. This MCP does not expose the dynamic-pool administration API, so ask an administrator about the pool's status.

## Submission outcome is uncertain

A connection failure after a mutation was sent may mean the remote task was accepted even though the client did not receive its ID. The service records `submission_uncertain` and does not automatically retry.

Keep the original local `task_id`, request, and `request_id`. Do not launch again with a new request ID. Inspect the Determined account for the corresponding remote task, then call `compute_reconcile(task_id, remote_id)` only for the same uncertain local submission. Reconciliation verifies the reserved submission marker before binding the record; a mismatch is rejected.

Use `compute_adopt` for a remote task that was created independently. Adoption is not a workaround for an uncertain local submission. See [discover and adopt](agent-workflow.md#discover-and-adopt-existing-remote-tasks) for the boundary.

## A task is terminal but the result is unclear

Use `compute_status` and `compute_logs` with the local task ID. A successful API submission, remote ID, or terminal state does not by itself prove workload success. Check exit information and the success criteria defined before launch. With storage access configured, verify the expected shared artifact with `storage_check`; if a local copy is required, preview and then execute `storage_fetch`. Without storage access, use task output or another explicit workload-level check. To see whether the task actually used its CPU, memory, or GPUs before it ended, call `compute_usage(task_id)`; for an ended task or a paused trial, the window ends when the task or its last allocation ended.

An experiment may have no trial logs before a trial starts. For a shell, the sanitized reconnect command can be used while the shell remains available. Reports may include task IDs, states, sanitized commands, paths, and errors, but must omit credential values and secret-file contents.

## Usage measurements are unavailable or empty

`compute_usage` depends on the master's task-resources API. `task_resources_disabled` means the master has the API but an administrator has not enabled `integrations.task_resources`. `task_resources_unsupported` means the master lacks the API; it needs a Determined master from the research-cluster fork 0.40.1 or later. Neither is retryable, so ask an administrator. Unavailable measurements are not evidence that a task is idle.

HTTP 503 means the measurement backend is busy or unavailable; each master runs at most four resource queries at once, so wait and retry. HTTP 400 can mean clock skew: the master rejects a window end more than 60 seconds ahead of its own clock, so correct the clock of the machine running the MCP server. HTTP 404 means the Determined task, or a requested trial ID, is missing or inaccessible to the current account.

`task_not_started` means the experiment has no trial yet or its trial has no Determined task; wait until it starts. `trial_not_found` means the requested trial does not belong to this experiment. `allocation_not_found` means the allocation is not listed for the selected task; choose one from the returned `allocations`. `remote_id_unknown` means the local submission is not bound and must be reconciled first.

A non-empty `context_unavailable` is not fatal. It lists context lookups (`resource_pool`, `allocation_details`, or `gpu_models`) that failed with a Determined API error; the related fields are empty or `null`, and the returned measurements remain valid. Retry later if you need that context. After a transport failure the remaining lookups are skipped, so several names can appear at once. A `null` `resource_pool` without a `context_unavailable` entry for an ended command or shell means Determined no longer serves that task's entity, so retrying will not help. A `null` `gpu_model` without a `gpu_models` entry in `context_unavailable` can mean that RBAC hides device UUIDs from the current account, so model names cannot be matched. A trial's `total_batches_processed` of 0 is expected when the workload does not report progress through Determined's Core API, as with a plain bash entrypoint, or has not reported yet; it does not show that the workload made no progress, so judge progress from logs, measured use, and expected artifacts instead.

An empty `series` list means no data for the window, not an idle task: the task may not have run in that window, or monitoring retained no data for it. Compare the window and its `anchor` with `task_start_time` and `allocations`, or use a longer `window_seconds`. If `samples_omitted` is true, narrow the window, select fewer metrics, or choose one allocation. A task can stay `RUNNING` for up to about 150 seconds while its agent is disconnected, because by default the fork waits that long (`agent_reconnect_wait`) for the agent to reconnect; a `RUNNING` state alone therefore does not prove progress. Field meanings and limits are in [task usage measurements](compute-service.md#task-usage-measurements).

## Cancellation is rejected

On the Determined fork 0.40.1 or later with basic authorization, only a task's Determined owner or an administrator can kill or cancel it. For a task owned by another account, `compute_cancel` returns HTTP 403 for a command or shell and HTTP 404 `experiment '<id>' not found` for an experiment. This usually follows a credential change: submitted records bind to the profile and endpoint, not the account, so the service still sends the request. Restore the owning account's credentials or ask an administrator to cancel the task. An adopted record reports `ownership_mismatch` before any cancellation request is sent.

## A task reports binding_mismatch

Submitted records bind to the compute profile and endpoint that launched them. When only the profile changed, `binding_mismatch` from `compute_cancel` or `compute_reconcile` says that read-only status, logs, and usage remain available; use the task's original profile to cancel or reconcile it. When the endpoint or `cluster_identity` label differs, every operation is refused, because the record may describe a task on another master. `compute_list_tasks` shows each record's offline `binding` (`profile`, `cross_profile`, `mismatch`, `unknown`, or `adopted`). A cross-profile read that returns `identity_mismatch` or `ownership_mismatch` found a remote task whose submission marker or owner does not match the record; do not treat it as the same task. A cross-profile read that returns `cross_profile_unverifiable` received HTTP 404 for the task's entity, so the owner and submission marker cannot be verified. It is not retryable. For a command or shell, Determined drops the entity 24 hours after the task ends and on a master restart; this does not mean the task's logs are gone, so read its logs and usage with the task's original profile. For an experiment, the 404 means the experiment was deleted or is not visible to the account; deleting an experiment also deletes its logs, so the original profile can read them only if the experiment still exists. See [cross-profile observation](compute-service.md#cross-profile-observation).

## A transfer is partial or different from the preview

Transfers never add `--delete`, so unrelated destination files remain. Normal rsync behavior can still replace same-named destination files. A failed executed transfer can leave a partial destination; rsync exit code 23 specifically reports that some files or attributes were not transferred.

Inspect the bounded transfer output, correct the filesystem or configuration problem, run a fresh dry run, and review it before executing again. Do not retry automatically with changed permission-preservation flags. The detailed rules are in [shared storage access](shared-storage-access.md#check-preview-and-transfer).
