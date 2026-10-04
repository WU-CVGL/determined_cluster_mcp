# Troubleshooting

[English](troubleshooting.md) | [简体中文](troubleshooting.zh.md)

[Home](../README.md) · [Agent workflow](agent-workflow.md) · [Compute reference](compute-service.md) · [Storage access](shared-storage-access.md)

## The MCP server does not start

Check that the client uses the virtual environment's absolute executable path and that every file path in its configuration is absolute. The MCP process requires a readable compute profile. It rejects `--db` and `--owner`, and the profile loader rejects `cluster_identity`; remove them from configurations written for a version with a task database (see [upgrading](compute-service.md#upgrading-from-a-version-with-a-task-database)).

```bash
/absolute/path/to/determined_cluster_mcp/.venv/bin/determined-compute-mcp --help
```

Once the client lists the tools, call `compute_plan` with a request to check the profile and paths; it needs no cluster access. The server uses stdout for MCP protocol frames and writes startup errors to stderr. Inspect the MCP client's server log for the exact error. After changing the installation or upgrading the service, restart every MCP process so that it loads the current tools.

Compute tasks do not need `--storage-config`. Storage tools automatically use a local shared path when it matches the configured `host_path`. For a custom local mapping or login-node SSH, copy `cfg/storage-access.example.yaml` to `.local/storage.yaml`, edit it, and add `--storage-config /absolute/path/to/.local/storage.yaml`.

## Authentication fails

Confirm that the API URL and credentials refer to the same Determined deployment. The secrets file supports either `DET_API_TOKEN`, or both `DET_USERNAME` and `DET_PASSWORD`:

```dotenv
DET_MASTER=http://determined.example.org:8080
DET_API_TOKEN=replace-with-your-token
```

When the secrets file sets `DET_MASTER`, its credentials go only to that master. An error that `--api-url` or `DET_MASTER` names a different master than the secrets file means an override points elsewhere: unset the override, or use a secrets file for that master.

Do not put credentials in the compute profile, request, task name, or description. Restrict access to the secrets file and inspect only whether required variable names are present, not their values.

The credentials select the Determined account, and the MCP acts only on tasks that account owns. Switching credentials therefore changes which tasks `compute_list` shows and which tasks the other tools accept.

## TLS certificate verification fails

Use the CA certificate published for the deployment. If it is already installed in the operating system or Python runtime trust store used by the MCP process, no extra CA setting is needed. When the application needs a separate PEM bundle, set Requests' `REQUESTS_CA_BUNDLE` to its absolute path and enable verification:

```bash
export REQUESTS_CA_BUNDLE=/absolute/path/to/organization-ca-bundle.pem
export DET_VERIFY_SSL=true
```

For MCP, add `--verify-ssl` to the server arguments and pass the CA variable through the stdio client's environment configuration. Adapt the surrounding keys to the client's MCP syntax:

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

Python 3.13 and later verify in OpenSSL's strict X.509 mode. A private CA certificate then needs a critical `basicConstraints` with `CA:TRUE`, `keyUsage` with `keyCertSign`, and a Subject Key Identifier, and the server certificate needs an Authority Key Identifier. Otherwise verification fails, and the error message names the reason, such as `Missing Authority Key Identifier`, `CA cert does not include key usage extension`, or `Basic Constraints of CA cert not marked critical`, although curl and older Python versions accept the chain. Check the chain in strict mode:

```bash
openssl verify -x509_strict -CAfile ca.pem server.pem
```

The fix is to reissue the certificates with these extensions; the MCP keeps strict verification on. A TLS connection failure such as `WRONG_VERSION_NUMBER` usually means that `DET_MASTER` names a plain HTTP port, such as the master's `:8080`; use the HTTPS address the deployment publishes. When an HTTPS master is reached through a proxy, a TLS connection failure without a verification reason, such as `UNEXPECTED_EOF_WHILE_READING`, usually means that the proxy could not reach the master; see [the master is unreachable through a proxy](#the-master-is-unreachable-through-a-proxy).

## The master is unreachable through a proxy

Requests reads `HTTPS_PROXY`, `HTTP_PROXY`, and `NO_PROXY`, or their lowercase forms, which take precedence, from the MCP process environment, which comes from the MCP client; proxy variables in the secrets file are not applied. An error saying that the proxy could not be reached means that the MCP could not connect to the proxy those variables name: fix or remove them, or bypass the proxy for the master as below. When the proxy cannot reach the master, requests fail with an error saying that the proxy refused or could not reach the master or, for an HTTPS master, that the TLS connection to the master failed; other tools may report a TLS connect error. If the master is reachable only through the proxy, check the proxy and its credentials. If the master is reachable without the proxy, add its host, or its domain suffix such as `.example.org`, to both `NO_PROXY` and `no_proxy` in the client's `env` and restart the MCP server; [optional HTTPS](compute-service.md#optional-https) shows an example. An HTTP proxy that answers in place of the master is covered in [submission outcome is uncertain](#submission-outcome-is-uncertain).

## A shared path is rejected or missing

Check which namespace the argument requires. Task `workdir` and `output_dir`, `storage_check.path`, and the `shared_dir` side of transfers use container paths from the compute profile. `mounts[].host_path` is a cluster-agent path. `local_dir` is an absolute path on the machine running the MCP server.

Planning validates configured path boundaries but does not query remote files or permissions. Use `storage_check` when the MCP server has configured local or SSH access. If the workload is already present on shared storage and no client-side inspection or transfer is needed, compute operations can proceed without a storage configuration.

A `read_only: true` mount permits reads and fetches but rejects a working directory, output directory, checkpoint destination, or sync target beneath that mount. See [shared storage access](shared-storage-access.md) for local mappings, SSH host keys, authentication, and rsync requirements.

## SSH storage access fails

Use a login-node `Host` alias that can access the configured cluster-agent host paths. A gateway belongs in `ProxyJump`; it is not the storage endpoint. Complete the first interactive connection and verify the host key before automation.

With `auth: openssh`, the service inherits an existing agent. The stdio MCP process must inherit a usable `SSH_AUTH_SOCK`; a GUI client started earlier may not have it. With password or keyring authentication, follow the credential placement rules in [shared storage access](shared-storage-access.md#authentication-choices).

Always repeat the dry run after correcting SSH, path, or permission errors. Do not turn a failed preview into an executed transfer without reviewing the new resolved endpoints and itemized changes.

## Capacity is unavailable or unknown

Run `compute_resources` for the requested pool and slot count. Zero slots check auxiliary-container capacity rather than free GPUs. Capacity is a snapshot, and the service checks it again before a new launch.

With `allow_queue: false`, unavailable or unknown capacity rejects the launch instead of queuing. Do not silently choose a suggested alternative pool, reduce resources, or set `allow_queue: true`; those changes require an explicit workload decision. Authentication or inventory-shape errors are errors, not evidence that capacity exists.

A resource pool that an administrator created dynamically appears in `compute_resources` only once it is Ready. A Pending or Failed pool is absent: the result reports that the pool was not present in the cluster inventory with unknown availability, and a launch without queuing is rejected with `capacity_unknown`. This MCP does not expose the dynamic-pool administration API, so ask an administrator about the pool's status.

## Submission outcome is uncertain

A connection that drops or times out after a launch request was sent, an HTTP 5xx response, a redirect (HTTP 3xx, which no mutation follows), or a response without a task ID may mean that Determined created the task even though the client did not receive its ID. `compute_launch` then returns `submission_uncertain` with `kind` and `submission_marker` in the error details, and does not retry. A failure before any connection opened, such as a refused connection or a failed name lookup, is a retryable `transport_error` instead: nothing was sent.

When the error details carry `source: "proxy"`, an HTTP proxy between this machine and the master answered, not Determined. The master was probably unreachable: check the API URL, whether the master is up, and whether its address belongs in `NO_PROXY` and `no_proxy`. A launch or other change answered this way is still unconfirmed, because the proxy may have failed after forwarding the request. A proxy that reports in its `Proxy-Status` header that it never connected to the master (for example `error=dns_error` or `error=connection_refused`) gives a retryable `transport_error` with `proxy_error` instead, since nothing reached the master. See [unconfirmed launches](compute-service.md#unconfirmed-launches) for how a proxy's answer is recognized.

Do not launch again automatically. Call `compute_list(kind, marker=submission_marker)` with a small `limit`: it reads the newest tasks of the account and returns every one whose stored config carries that marker. One returned task is most likely the submission; continue with its ID. Several returned tasks share a copied config; let the user decide which one, if any, is the submission. An empty result does not prove that the submission failed: follow `pagination.next_offset` to older pages, search again later, since the master may store the task after the search, or check the WebUI. An ended command or shell that Determined no longer serves does not appear at all. Whether to submit again is the user's decision after checking; a duplicate cancelled afterwards may already have written files or had other effects that cancelling does not undo. See [unconfirmed launches](compute-service.md#unconfirmed-launches).

## A task is terminal but the result is unclear

Use `compute_status` and `compute_logs` with the task's kind and ID. A successful API submission, task ID, or terminal state does not by itself prove workload success. Check exit information and the success criteria defined before launch. With storage access configured, verify the expected shared artifact with `storage_check`; if a local copy is required, preview and then execute `storage_fetch`. Without storage access, use task output or another explicit workload-level check. To see whether the task actually used its CPU, memory, or GPUs before it ended, call `compute_usage(kind, id)`; for an ended task or a paused trial, the window ends when the task or its last allocation ended.

An experiment may have no trial logs before a trial starts. For a shell, the sanitized reconnect command can be used while the shell remains available. Reports may include task IDs, states, sanitized commands, paths, and errors, but must omit credential values and secret-file contents.

## Usage measurements are unavailable or empty

`compute_usage` depends on the master's task-resources API. `task_resources_disabled` means the master has the API but an administrator has not enabled `integrations.task_resources`. `task_resources_unsupported` means the master lacks the API; it needs a Determined master from the research-cluster fork 0.40.1 or later. Neither is retryable, so ask an administrator. Unavailable measurements are not evidence that a task is idle.

HTTP 503 means the measurement backend is busy or unavailable; each master runs at most four resource queries at once, so wait and retry. HTTP 400 can mean clock skew: the master rejects a window end more than 60 seconds ahead of its own clock, so correct the clock of the machine running the MCP server. HTTP 404 means the Determined task, or a requested trial ID, is missing or inaccessible to the current account. Determined serves an ended command or shell for only 24 hours after it ends and not after a master restart; after that its owner cannot be verified, so its usage, status, and logs return HTTP 404 and retrying will not help.

`task_not_started` means the experiment has no trial yet or its trial has no Determined task; wait until it starts. `trial_not_found` means the requested trial does not belong to this experiment. `allocation_not_found` means the allocation is not listed for the selected task; choose one from the returned `allocations`.

A non-empty `context_unavailable` is not fatal. It lists context lookups (`resource_pool`, `allocation_details`, or `gpu_models`) that failed with a Determined API error; the related fields are empty or `null`, and the returned measurements remain valid. Retry later if you need that context. After a transport failure the remaining lookups are skipped, so several names can appear at once. A `null` `gpu_model` without a `gpu_models` entry in `context_unavailable` can mean that RBAC hides device UUIDs from the current account, so model names cannot be matched. A trial's `total_batches_processed` of 0 is expected when the workload does not report progress through Determined's Core API, as with a plain bash entrypoint, or has not reported yet; it does not show that the workload made no progress, so judge progress from logs, measured use, and expected artifacts instead.

An empty `series` list means no data for the window, not an idle task: the task may not have run in that window, or monitoring retained no data for it. Compare the window and its `anchor` with `task_start_time` and `allocations`, or use a longer `window_seconds`. If `samples_omitted` is true, narrow the window, select fewer metrics, or choose one allocation. A task can stay `RUNNING` for up to about 150 seconds while its agent is disconnected, because by default the fork waits that long (`agent_reconnect_wait`) for the agent to reconnect; a `RUNNING` state alone therefore does not prove progress. Field meanings and limits are in [task usage measurements](compute-service.md#task-usage-measurements).

## A task operation is refused

The MCP acts only on tasks owned by the authenticated account. `ownership_mismatch` means the task belongs to another account; the service reads the task, then refuses before any further request, even when the credentials belong to an administrator. Use the owning account's credentials, or ask an administrator to act through Determined directly. `ownership_unavailable` means the master did not report a generic task's owner, because it lacks the research-cluster fork's generic task list (WU-CVGL/determined#27); ask an administrator to upgrade the master. On such a master, launching a generic task or listing generic tasks fails with `unsupported`; a launch checks this before anything is created.

Determined applies its own permissions as well. On the fork 0.40.1 or later with basic authorization, only a task's owner or an administrator can kill, cancel, pause, or resume it; other accounts receive HTTP 403, or HTTP 404 `experiment '<id>' not found` for an experiment.

## A transfer is partial or different from the preview

Transfers never add `--delete`, so unrelated destination files remain. Normal rsync behavior can still replace same-named destination files. A failed executed transfer can leave a partial destination; rsync exit code 23 specifically reports that some files or attributes were not transferred.

Inspect the bounded transfer output, correct the filesystem or configuration problem, run a fresh dry run, and review it before executing again. Do not retry automatically with changed permission-preservation flags. The detailed rules are in [shared storage access](shared-storage-access.md#check-preview-and-transfer).
