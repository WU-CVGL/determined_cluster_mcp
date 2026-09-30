# Determined Cluster MCP

[English](README.md) | [简体中文](README.zh.md)

Run Determined `command`, `shell`, and `experiment` tasks through a local stdio MCP server. Code, data, checkpoints, and outputs stay on mapped shared storage. Any MCP client that can start a local stdio server can use the service with its own model.

## Install

Requires Python 3.10+, a Determined account, and deployment settings supplied by your cluster administrator or project configuration. The master must be a build of the Determined fork that carries the job ledger: `GET /api/v1/master` must report `submission_protocol` 1 or later. Upstream Determined releases report no submission protocol, and the server refuses them.

```bash
git clone https://github.com/WU-CVGL/determined_cluster_mcp.git
cd determined_cluster_mcp
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[mcp]'
```

## Configure

```bash
mkdir -p .local
cp cfg/compute-profile.example.yaml .local/profile.yaml
cp cfg/examples/command_request.json .local/request.json
```

Set the API URL and account credentials in `.local/credentials.env`:

```dotenv
DET_MASTER=https://determined.example.org
DET_API_TOKEN=replace-with-your-token
```

`DET_USERNAME` and `DET_PASSWORD` are also supported. Do not commit the credentials file. A secrets file that names `DET_MASTER` sends its credentials to that master only, and the server refuses to start when the environment names another one. Fill `profile.yaml`, the policy, with the administrator-provided image, resource pool, cluster-agent host paths, and container mount paths. Use container paths in task requests. The server keeps no local state: the Determined master records every job.

Compute tasks do not need a client storage configuration. Storage tools automatically use a local shared path when it matches the configured `host_path`. For a custom local mapping or login-node SSH, copy `cfg/storage-access.example.yaml` to `.local/storage.yaml`, edit it, and add `--storage-config /absolute/path/to/.local/storage.yaml` to the MCP arguments.

## Connect a stdio MCP client

Add this server in the syntax used by your MCP client. Replace every example value with an absolute local path or a deployment value:

```json
{
  "command": "/absolute/path/to/determined_cluster_mcp/.venv/bin/determined-compute-mcp",
  "args": [
    "--profile", "/absolute/path/to/determined_cluster_mcp/.local/profile.yaml",
    "--secrets-file", "/absolute/path/to/determined_cluster_mcp/.local/credentials.env",
    "--verify-ssl"
  ]
}
```

Add `"--storage-config", "/absolute/path/to/.local/storage.yaml"` when storage access needs its own file, and use `--no-verify-ssl` only for a deployment without TLS. The credentials select the Determined account, which owns every job the server launches. At startup the server checks the master's submission protocol and exits with status 2 when the master is older than protocol 1 or the configuration is invalid; an unreachable master does not stop it. If SSH or a private CA is required, pass the needed environment to the stdio process; see troubleshooting below.

## Upgrade from an earlier release

Release 1.0 keeps no local state: the master records every job, and `job_id` is its only handle. To upgrade:

1. Upgrade the master first. The new server exits with status 2 against a master without submission protocol 1.
2. Remove `--db` and `--owner`, and the `DETERMINED_COMPUTE_DB` and `DETERMINED_COMPUTE_OWNER` variables, from every MCP client configuration, together with `--repo-root` and the `--consultation-*` options. The server takes none of them and exits with status 2 on an unknown argument.
3. Remove `cluster_identity` and `shell_inactivity_seconds` from the policy, and rename `shared_mounts` to `mounts`; an unknown key is `invalid_policy`. The policy gains `pools`, `max_slots`, and `allow_overwrite`; see the [policy reference](docs/compute-service.md#policy).
4. Update the checkout and reinstall with `python -m pip install -e '.[mcp]'`, then restart every MCP process in every client and session. A running process keeps the code it loaded, so it would keep serving the old tools and writing the old database.

The old SQLite database (`--db`, by default `~/.local/state/determined-compute/tasks.sqlite3`) is no longer read or written; keep it as a record or delete it. Job IDs now come from the master, so the local `task_id` values it held no longer work. `compute_list` lists every job of the account from every client, including jobs launched before the upgrade, which have no `request_id`; take their `job_id` from there. Requests are now a `TaskSpec` (see `cfg/examples/`), and `compute_list` replaces `compute_list_tasks`, `compute_reconcile`, `compute_discover`, and `compute_adopt`. `storage_sync` and `storage_fetch` now keep existing destination files unless `overwrite=true`, which the policy must allow.

## Documentation

- [Agent workflow](docs/agent-workflow.md): prepare, plan, launch, monitor, and accept work
- [Compute service reference](docs/compute-service.md): the policy, TaskSpec, the eleven tools, usage measurements, and errors
- [Shared storage access](docs/shared-storage-access.md): local mounts, SSH, dry runs, and transfers
- [Troubleshooting](docs/troubleshooting.md): startup, the protocol gate, authentication, TLS, specs, paths, git planning, queued jobs, plan binding, uncertain launches, usage measurements, cancellation, and transfers
