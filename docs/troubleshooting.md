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

The MCP obtains its token once per process, from `--api-token`, `DET_API_TOKEN`, or a login with `DET_USERNAME` and `DET_PASSWORD`; once it has a token, it does not log in again. A 401 from the login itself means the master rejected the username and password: correct it where the MCP reads it; a secrets-file change takes effect on the next call, and an environment change needs a restart. A login token expires after 7 days, and a password change revokes the account's sessions and tokens, so after either event every call fails with HTTP 401; the 401 message says how to recover. If only the login token expired, restarting the MCP logs in again. If the password changed, or an API token expired or was revoked, update it where the MCP reads it (`--api-token` in the MCP arguments, the secrets file, or the environment), then restart the MCP. Retrying does not help: report it, so that the user takes these steps.

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

Drained or disabled slots and disabled agents are not capacity. When the agent list still disagrees with the pool, the `capacity_unknown` message gives the pool's slot and agent counts and the counts from the agent list. For 1 or more slots, a message that the used slots differ from the slots holding containers means a task is starting or stopping in the pool; report it, and the user may check again; do not loop. An experiment that may span agents (2 or more `slots_per_trial` without `is_single_node: true`) and fits on no single agent is `capacity_unknown`, because placement across agents is not checked: set `is_single_node: true` when the work fits on one agent, or queue with `allow_queue: true` only with the user's consent.

For a request with `prefer_gpu_topology: "strong"` and 2 or more slots:

- `capacity_unavailable` with `retryable: false`: the master refuses this `"strong"` request with the pool's current agents, static or provisioned. When no agent has N slots, the request needs fewer slots or another pool; when an agent has N slots but no NUMA node does, `"soft"` may also fit. The choice is the user's.
- `capacity_unknown` naming GPU topology: the account cannot see agent GPU topology, or it does not match the slots. Requests without `"strong"` do not need it. Queue only with the user's consent.
- A queued `"strong"` task logs `GPU topology preference strong: waiting until one NUMA node of an agent in pool P has N free GPUs`. It can still fail later with `no NUMA node in pool P has N slots; use soft`, for example after a master restart or a GPU exclusion; a trial that fails this way is not restarted.

A resource pool that an administrator created dynamically appears in `compute_resources` only once it is Ready. A Pending or Failed pool is absent: the result reports that the pool is not present or not available to you, with unknown availability, and a launch without queuing is rejected with `capacity_unknown`. This MCP does not expose the dynamic-pool administration API, so ask an administrator about the pool's status.

On the research-cluster fork 0.42.0 or later, an administrator can restrict a pool to some accounts, and the pool list omits a pool that the account may not use, so `compute_resources` and a launch without queuing report it the same way. A launch with `allow_queue: true`, or resuming an experiment or generic task in such a pool, fails with `permission_denied`; its message and `details.resource_pool` name the pool. Choose another pool with the user or ask an administrator for access.

## Submission outcome is uncertain

A connection that drops or times out after a launch request was sent, an HTTP 5xx response, a redirect (HTTP 3xx, which no mutation follows), or a response without a task ID may mean that Determined created the task even though the client did not receive its ID. `compute_launch` then returns `submission_uncertain` with `kind` and `submission_marker` in the error details, and does not retry. A failure before any connection opened, such as a refused connection or a failed name lookup, is a retryable `transport_error` instead: nothing was sent. When the research-cluster fork 0.42.0 or later cannot check whether the account may use the requested pool, it answers with HTTP 503 `could not check access to resource pool "<pool>": ...; try again`, which the service also reports as `submission_uncertain`.

After an outage or a 5xx, check from your own turn with `compute_resources` whether the master answers; do not start a shell watcher on the master.

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

An empty `series` list means no data for the window, not an idle task: the task may not have run in that window, or monitoring retained no data for it. If the cluster's task-mapping delay is nonzero (`observability.task_mapping_delay`, 5 minutes by default in the fork; the MCP cannot read it), measurements from the first minutes of each allocation, counted from allocation start including image pull, are not attributed to the task and are never backfilled, so an allocation that ended sooner has none. Compare the window and its `anchor` with `task_start_time` and `allocations`; when an earlier allocation ended before the window starts, as after a pause and resume, pass that `allocation_id` or a `window_seconds` that reaches back to it. If `samples_omitted` is true, narrow the window, select fewer metrics, or choose one allocation. A task can stay `RUNNING` for up to about 150 seconds while its agent is disconnected, because by default the fork waits that long (`agent_reconnect_wait`) for the agent to reconnect; a `RUNNING` state alone therefore does not prove progress. Field meanings and limits are in [task usage measurements](compute-service.md#task-usage-measurements).

## A task operation is refused

The MCP acts only on tasks owned by the authenticated account. `ownership_mismatch` means the task belongs to another account; the service reads the task, then refuses before any further request, even when the credentials belong to an administrator. Use the owning account's credentials, or ask an administrator to act through Determined directly. `ownership_unavailable` means the master did not report a generic task's owner, because it lacks the research-cluster fork's generic task list (WU-CVGL/determined#27); ask an administrator to upgrade the master. On such a master, launching a generic task or listing generic tasks fails with `unsupported`; a launch checks this before anything is created.

Determined applies its own permissions as well. On the fork 0.40.1 or later with basic authorization, only a task's owner or an administrator can kill, cancel, pause, or resume it; other accounts receive `permission_denied` (HTTP 403), or HTTP 404 `experiment '<id>' not found` for an experiment.

## Shell access fails

- `unsupported` with a message about `websocket-client`: reinstall the server with its MCP extra (`python -m pip install -e '.[mcp]'` in the checkout) and restart the MCP process.
- `unsupported` with a message about a proxy: the environment selects a proxy other than `http://`, such as `socks5://`, for the master. Shell access reaches the master only directly or through an `http://` proxy; list the master in `NO_PROXY` and `no_proxy`, or set an `http://` proxy for it.
- `compute_shell_connect` returns `state: null` with `context_unavailable: ["state"]`: during `wait_seconds` the shell could not be checked, for example because the master was briefly unreachable. The tunnel is open; check `compute_status` before relying on it.
- `shell_not_running`: only a `STATE_RUNNING` shell can be connected. A queued shell is waiting for capacity: pass `wait_seconds`, up to 600, or connect again later. An ended one cannot be reopened, so launch a new shell. A shell that ends while `wait_seconds` waits for its sshd also gives `shell_not_running`; a tunnel that another connect has returned stays open until `compute_shell_disconnect`.
- `ready: false`, or `probe.ok: false` right after the shell started: sshd is still starting. `compute_logs` shows `Server listening on` once it is up; call `compute_shell_connect` again, which keeps the open tunnel and probes again.
- A probe error `the WebSocket handshake was refused with HTTP <status>` comes from the master or its shell proxy: 404 or 502 usually means the shell ended or its proxy is not registered yet.
- A probe error `WebSocketProxyException: failed CONNECT via proxy status: <status>` comes from the HTTP proxy chosen for the master: 407 means it wants other credentials, set in the proxy URL, and 403 or 405 means it does not allow `CONNECT` to the master's port. TLS errors and other proxy failures have the same causes as for the API; see [TLS certificate verification fails](#tls-certificate-verification-fails) and [the master is unreachable through a proxy](#the-master-is-unreachable-through-a-proxy).
- `cancelled`: the request that started `compute_shell_connect` was cancelled, for example by a client timeout, while it waited. It opened no tunnel, or closed the one it waited on, unless another `compute_shell_connect` has returned that tunnel or still waits on it; connect again.
- `shell_access_stopping`: the MCP server is stopping, or its last client session ended; it opens no tunnel. Restart the MCP server, or reconnect it in the client.
- `shell_access_closed`: the tunnel was disconnected, for example by `compute_shell_disconnect` or `compute_cancel`, while `compute_shell_connect` waited for its sshd. Connect again to open a new one.
- `compute_shell_disconnect` returns an error such as `No space left on device` or one that names the key file: the tunnel has stopped, but the configs could not be rewritten or the key could not be deleted. Free the space or fix the file, and remove the key by hand if it is still there; the next connect rewrites the configs.
- `compute_shell_disconnect` returns `files: kept`: the shell-access directory or its lock file was deleted while tunnels were open, so the server left the files alone in case another server owns them now. The next connect after every tunnel is closed takes the directory again and removes them.
- A connect fails with `No locks available` (`ENOLCK`): the shell-access directory is on a file system without working locks, such as NFS without a lock daemon. Point `--shell-access-dir` at a local directory.
- `port_unavailable`: another program uses the requested `local_port`. Omit it to get a free port.
- `shell_access_conflict` saying that another determined-compute-mcp process uses the shell access directory: another MCP server, such as one from another client session, uses it. Give each server its own `--shell-access-dir` or `DETERMINED_COMPUTE_SHELL_ACCESS`.
- `shell_access_conflict` saying that the shell access directory was removed or taken over while this server had open tunnels: the directory was deleted, or its lock file was deleted and another server locked a new one. Call `compute_shell_disconnect` for this server's open tunnels, which leaves the directory's files alone, or restart the server; then connect again.
- `shell_access_conflict` saying that the shell already has a tunnel: it is open on another port. Use it, or call `compute_shell_disconnect` before connecting with another `local_port`.
- `shell_access_conflict` saying that a directory exists and is not a shell access directory: a directory with the shell's ID as its name holds something other than shell-access files. Remove it, or use a dedicated shell-access directory.
- ssh-mcp fails to start with its config file not found, or does not know the profile: the file exists only while a tunnel is open, and ssh-mcp reads it only at startup, so start or reconnect ssh-mcp after `compute_shell_connect`. Check that it was registered as `--config=<ssh_mcp.config_path>`, with `=`: ssh-mcp ignores a value separated by a space.
- ssh-mcp reports a host-key mismatch: its config is older than the tunnel on that port. Restart it so that it reads the current `trustedHostKey`.
- stderr shows `mux_client_request_session: session request failed: Session open refused by peer`, then `ControlSocket ... already exists, disabling multiplexing`: more commands ran at once through one multiplexing master than sshd's `MaxSessions` (10 by default) allows. The command was not refused: `ssh` ran it over a new connection of its own. Judge it by its exit status, not by this message, and do not run it again. Run fewer commands at once to keep them on one connection.
- A command through the alias hangs or fails while others work, or a large transfer should not share the connection: run it with `-S none` added to `ssh_command`, which gives it a connection of its own; `ssh ... -O exit` ends the shared one, and the next command opens a new one.
- `control_path_dir` is `null`: no socket directory was short and private enough, or the MCP server runs on Windows, where OpenSSH does not multiplex. Commands still work, each over its own connection. To multiplex them, use a shell-access directory of at most 39 bytes, set `XDG_RUNTIME_DIR` to a short directory that you own with mode 0700, or create `~/.ssh` when `~/.ssh/det-cm` fits; then restart the MCP server.
- `ssh_command` for an alias fails with `connect to host 127.0.0.1 port 1: Connection refused`: no open tunnel has that alias, because it was disconnected, the MCP server restarted, or the alias belongs to another server. Call `compute_shell_connect` and use the alias it returns.
- `compute_shell_connect` fails with `invalid_response` about the login user: the master reported a shell user that is not a POSIX login name, which the generated `ssh_config` cannot hold safely. Ask an administrator to check the account's agent user.
- An SSH session drops when the determined-compute MCP restarts: tunnels live in that process. Call `compute_shell_connect` again, and restart ssh-mcp, since the port changes.

## A transfer is partial or different from the preview

Transfers never add `--delete`, so unrelated destination files remain. Normal rsync behavior can still replace same-named destination files. A failed executed transfer can leave a partial destination; rsync exit code 23 specifically reports that some files or attributes were not transferred.

Inspect the bounded transfer output, correct the filesystem or configuration problem, run a fresh dry run, and review it before executing again. Do not retry automatically with changed permission-preservation flags. The detailed rules are in [shared storage access](shared-storage-access.md#check-preview-and-transfer).
