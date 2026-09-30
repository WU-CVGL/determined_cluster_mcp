# Troubleshooting

[English](troubleshooting.md) | [简体中文](troubleshooting.zh.md)

[Home](../README.md) · [Agent workflow](agent-workflow.md) · [Compute reference](compute-service.md) · [Storage access](shared-storage-access.md)

## The MCP server does not start

Check that the client uses the virtual environment's absolute executable path and that every file path in its configuration is absolute. The server needs a readable policy passed with `--profile`; it keeps no database and takes no owner.

```bash
/absolute/path/to/determined_cluster_mcp/.venv/bin/determined-compute-mcp --help
```

The server writes startup errors to stderr, because stdout carries MCP frames; inspect the MCP client's server log for the exact message. It exits with status 2 when:

- it gets an argument it does not take, such as `--db`, `--owner`, `--repo-root`, or a `--consultation-*` option from an earlier release; remove it (see [Upgrade from an earlier release](../README.md#upgrade-from-an-earlier-release));
- the policy, the storage access file, or the credential source is invalid. A policy from an earlier release fails with `invalid_policy` on `cluster_identity`, `shell_inactivity_seconds`, or `shared_mounts`;
- the environment's `DET_MASTER` differs from the one in the secrets file (see [Authentication fails](#authentication-fails));
- the master answers but fails the protocol gate (see [The master lacks the submission protocol](#the-master-lacks-the-submission-protocol)).

An unreachable master does not stop startup. The server logs the connection error, the storage tools keep working, and the protocol check runs again before the first call to the master; until the master answers, compute tools return `unavailable`.

After an upgrade, restart every MCP process: a process started before it keeps the old code, its old tools, and its old database.

Compute tasks do not need `--storage-config`. Storage tools automatically use a local shared path when it matches the configured `host_path`. For a custom local mapping or login-node SSH, copy `cfg/storage-access.example.yaml` to `.local/storage.yaml`, edit it, and add `--storage-config /absolute/path/to/.local/storage.yaml`.

## The master lacks the submission protocol

`protocol_unsupported` means the master does not speak submission protocol 1, the job ledger this MCP needs: it is an upstream release or an older build. At startup the server then exits with status 2, and the message names the master's release. When the master was unreachable at startup, the server starts anyway, and the first compute tool call returns `protocol_unsupported` instead. A master that answers `Unimplemented` for a submission route returns it too.

The release string does not decide, because a local build reports the previous tag and a release candidate the next one. Read `submissionProtocol` from `GET /api/v1/master`, which needs no login:

```bash
curl -s https://determined.example.org/api/v1/master | python3 -c 'import json, sys; print(json.load(sys.stdin).get("submissionProtocol"))'
```

Upgrade the master to a build of the Determined fork with the job ledger. There is no fallback to older APIs; the storage tools keep working meanwhile.

## Authentication fails

Confirm that the API URL and credentials refer to the same Determined deployment. The secrets file supports either `DET_API_TOKEN`, or both `DET_USERNAME` and `DET_PASSWORD`:

```dotenv
DET_MASTER=https://determined.example.org
DET_API_TOKEN=replace-with-your-token
```

When the secrets file names `DET_MASTER`, its credentials go to that master alone: the environment's `DET_API_TOKEN`, `DET_USERNAME`, and `DET_PASSWORD` are ignored, and an environment `DET_MASTER` that names another master stops startup. Unset one of them, or pass `--api-url` to choose deliberately. When the secrets file has no `DET_MASTER`, the environment supplies the master, and its token wins over the file's.

Do not put credentials in the policy, a TaskSpec, a task name, or a command. Restrict access to the secrets file and inspect only whether the required variable names are present, not their values. The owner of every job is the authenticated account.

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
  "args": ["--profile", "/absolute/path/to/profile.yaml", "--secrets-file", "/absolute/path/to/credentials.env", "--verify-ssl"],
  "env": {
    "REQUESTS_CA_BUNDLE": "/absolute/path/to/organization-ca-bundle.pem"
  }
}
```

A GUI application may not inherit variables exported in a terminal. Configure the variable in the client's MCP environment settings or start the client from an environment that contains it, then restart the MCP server. The CA bundle must be readable by the MCP process.

An unknown issuer is addressed by the correct CA chain. An expired certificate or hostname mismatch must be corrected by the deployment operator; disabling verification does not repair the certificate identity.

## A spec or argument is refused

A spec or argument that fails validation returns `invalid_request` with `details.errors`, each naming a location such as `spec.experiment` and the reason. Common causes: a shell with a command, `output_dir`, `workdir`, or `git` code; a command or experiment without `output_dir`; an experiment that sets a field the MCP renders (`entrypoint`, `resources.slots_per_trial`, `environment.image`, and so on), `bind_mounts`, or a `checkpoint_storage` `host_path`; a `storage_path` without `type: shared_fs`; a search without `searcher.max_concurrent_trials`; or a legacy `module:Class` command. `admission: immediate` returns `admission_unsupported`: this release queues every job and does not evaluate placement before launch, so use `queue`, the default. None of these create anything.

The master validates the rest of an experiment config in the plan's dry run and reports a problem as `invalid_request` with its own message.

## A shared path is rejected or missing

Check which namespace the argument requires. `output_dir`, `code.repo` for `git`, `code.dir` for `path`, `storage_check.path`, and the `shared_dir` of transfers are container paths from the policy. `mounts[].host_path` is a cluster-agent path. `code.repo` for `context` and `local_dir` are absolute paths on the machine running the MCP server.

`path_not_mounted` means no policy mount holds the path; `read_only_storage` means the path is under a `read_only: true` mount, which permits reads and fetches but no `output_dir` or sync target. A plan warning `path_not_bind_mounted` means the master's effective bind mounts do not hold the path, so the task cannot reach it; ask the administrator. See [shared storage access](shared-storage-access.md) for local mappings, SSH host keys, authentication, and rsync requirements.

## git code cannot be planned

`storage_not_local` means the repository's root is not readable through a local mount on the machine running the MCP server. This release plans `git` code only through a local mount, not over SSH: map the root in the storage access file's `local_mounts` with `mode: auto` or `local`, mount it at its host path, or send the code as `context`.

`commit_not_on_ref` means the pinned commit is on no branch or tag; push or tag it, because the task's clone borrows objects from the repository and `git gc` prunes unreachable ones. `git_too_old` names the git version found and the one required (2.32). Partial clones, linked worktrees, and repositories with alternates are refused, because the task could not resolve their objects. `lfs_object_missing` means an LFS object is not in the repository; fetch it first.

## SSH storage access fails

Use a login-node `Host` alias that can access the configured cluster-agent host paths. A gateway belongs in `ProxyJump`; it is not the storage endpoint. Complete the first interactive connection and verify the host key before automation.

With `auth: openssh`, the service inherits an existing agent. The stdio MCP process must inherit a usable `SSH_AUTH_SOCK`; a GUI client started earlier may not have it. With password or keyring authentication, follow the credential placement rules in [shared storage access](shared-storage-access.md#authentication-choices).

Always repeat the dry run after correcting SSH, path, or permission errors. Do not turn a failed preview into an executed transfer without reviewing the new resolved endpoints and itemized changes.

## A job stays queued

Placement is not evaluated before launch, and every job is admitted to the queue. A job waits while its pool lacks free slots; `current_slots_exceeded` in a plan warns that the request needs more slots than the cluster has now. Call `compute_resources(pool)` to see the pool's slots and device models, and `compute_status(job_id)` for the explanation. An active experiment whose trials wait reads `running`, and its explanation says it waits for the scheduler.

Changing the pool or the slot count is a workload decision: cancel the queued job and plan a new one only when the user agrees. A pool that `compute_resources` does not list is unknown to the master, or not visible to the account.

## A launch outcome is uncertain

The master keeps every job under its `request_id`, so a launch is always safe to repeat with the same `spec`, `request_id`, and `request_digest`:

- A lost response, a timeout, `unavailable`, or `invalid_response`: repeat the launch. It returns the job with `replayed: true` if one was created, even when the working tree, the policy, or a workspace changed since the plan.
- `internal`: repeat it once. The same error again means nothing was created; fix the request and plan again.
- A render error that names `request_id` in its details and says the outcome is unknown: the master could not be asked. Check `compute_list` for that `request_id` before planning again.

Never plan again for a new `request_id` while an outcome is uncertain. `compute_list` shows every job of the account, from every client, with its `request_id`.

`plan_changed` and `key_conflict` are not uncertain: nothing was created, or the error names the job; see the next section.

## A launch returns plan_changed or key_conflict

`plan_changed` means the request the launch rendered differs from the one the plan dry-ran, so the master created nothing and the `request_id` stays unused. Common causes:

- The launch passed the original spec instead of the resolved one, and its branch moved since the plan. The `spec` that `compute_plan` returns pins the commit and the policy defaults; always launch with it.
- For `context`, an `include` path changed in the working tree, or the tree became dirty or clean, which `.code-provenance.json` records.
- For a command or shell, the workspace named in the spec now resolves to another workspace.

The error's `details` give the `commit` and `content_digest` that the launch rendered. Review them, call `compute_plan` again, and launch with the new plan's `spec`, `request_id`, and `request_digest`. A change to master or pool defaults never causes `plan_changed`: those apply as they stand at launch.

`key_conflict` means the `request_id` is already bound to a job whose request differs, given in `details.job_id`; this happens when a `request_id` is reused with another plan's spec or digest. Read that job with `compute_status`: it may be the job intended, launched earlier. Otherwise plan again, which mints a new `request_id`; never edit one by hand.

## A job ended but the result is unclear

Use `compute_status(job_id)` and `compute_logs(job_id)`. The job's `exit_class` says why it ended: `workload_failed` for a workload error, including a failed prelude, which prints a line starting with `compute:` in the logs (for example `compute: git code delivery failed: ...` or `compute: the workdir resolves to ..., outside the code root`); `workload_initialization_failed` when the container failed before the workload started, such as an image pull; and `infrastructure_failed` when an agent or its connection was lost. A job with no exit class was submitted before the ledger, or is a cancelled experiment whose trial never started.

An ended state alone does not prove success. Check the success criteria defined before launch; with storage access configured, verify the expected shared artifact with `storage_check`, and preview then execute `storage_fetch` for a local copy. To see whether the job used its CPU, memory, or GPUs, call `compute_usage(job_id)`; for an ended job the window ends when the job or its last allocation ended.

## Usage measurements are unavailable or empty

`measurement: "unmeasured"` means the master has no task-resources integration; an administrator configures `integrations.task_resources`. It is not evidence that a job is idle. `unavailable` means the measurement backend is busy or unreachable; wait and retry. `invalid_request` for a window can mean clock skew: the master rejects a window end more than 60 seconds ahead of its own clock.

`task_not_started` means the job, or the selected trial, has no task yet. `not_found` means the trial or allocation belongs to another job; choose one from `allocations` in `compute_status`. `invalid_request` for an allocation means it belongs to another trial than `trial_id`; omit `trial_id` to select the allocation's own trial.

A non-empty `context_unavailable` is not fatal: it lists best-effort lookups (`trial`, `resource_pool`, or `gpu_models`) that failed, and the measurements remain valid. An empty `series` means no data for the window: compare `window` and its `anchor` with the allocations, or use a longer `window_seconds`. If `samples_omitted` is true, narrow the window, select fewer metrics, or choose one allocation. A `total_batches_processed` of 0 is expected when the workload does not report through Determined's Core API. Field meanings are in [task usage measurements](compute-service.md#task-usage-measurements).

## Cancellation is rejected

Under basic authorization, only a job's owner or an administrator can cancel it, and `compute_cancel` returns `permission_denied` or `not_found` for another account's job. Use the account that launched the job, or ask an administrator. `cancel: "recorded"` is not a failure: the master recorded the cancel and ends the job shortly; `compute_status` shows when.

## A transfer is partial or different from the preview

Transfers never add `--delete`, so unrelated destination files remain. Without `overwrite`, existing files and the attributes of existing directories are kept; with `overwrite`, which the policy must allow, same-named files are replaced. A failed executed transfer can leave a partial destination; rsync exit code 23 reports that some files or attributes were not transferred.

`overwrite_not_allowed` means `overwrite=true` was passed while the policy's `allow_overwrite` is `false`; nothing was transferred. Earlier releases replaced same-named files by default. Now an existing destination file is kept unless `overwrite` is set and allowed, so write to a new run directory, or ask the administrator to allow overwrite.

Inspect the bounded transfer output, correct the filesystem or configuration problem, run a fresh dry run, and review it before executing again. Do not retry automatically with changed permission-preservation flags. The detailed rules are in [shared storage access](shared-storage-access.md#check-preview-and-transfer).
