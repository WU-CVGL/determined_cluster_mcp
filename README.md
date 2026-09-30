# Determined Cluster MCP

[English](README.md) | [简体中文](README.zh.md)

Run Determined `command`, `shell`, and `experiment` tasks through a local stdio MCP server. Code, data, checkpoints, and outputs stay on mapped shared storage. Any MCP client that can start a local stdio server can use the service; the client's model is independent of the optional server-side consultation backend.

## Install

Requires Python 3.10+, a Determined account, and deployment settings supplied by your cluster administrator or project configuration.

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

`DET_USERNAME` and `DET_PASSWORD` are also supported. Do not commit the credentials file. Fill `profile.yaml` with the administrator-provided image, resource pool, cluster-agent host paths, and container mount paths. Use container paths in task requests. Keep the SQLite database on local durable disk, not shared NFS.

Compute tasks do not need a client storage configuration unless a request uses `create_directories` and its profile host root is not detected as a mount point on this machine; then map that root with `local_mounts`, to itself if the path is the same, or configure SSH. Storage tools automatically use a local shared path when it matches the configured `host_path`. For a custom local mapping or login-node SSH, copy `cfg/storage-access.example.yaml` to `.local/storage.yaml`, edit it, and add `--storage-config /absolute/path/to/.local/storage.yaml` to the MCP arguments.

## Connect a stdio MCP client

Add this server in the syntax used by your MCP client. Replace every example value with an absolute local path or a deployment value:

```json
{
  "command": "/absolute/path/to/determined_cluster_mcp/.venv/bin/determined-compute-mcp",
  "args": [
    "--profile", "/absolute/path/to/determined_cluster_mcp/.local/profile.yaml",
    "--db", "/absolute/local/path/to/tasks.sqlite3",
    "--owner", "your-owner",
    "--secrets-file", "/absolute/path/to/determined_cluster_mcp/.local/credentials.env",
    "--verify-ssl"
  ]
}
```

The `owner` is a local task namespace, not authentication. The credentials select the Determined account. If SSH or a private CA is required, pass the needed environment to the stdio process; see troubleshooting below.

## Documentation

- [Agent workflow](docs/agent-workflow.md): prepare, plan, launch, monitor, and accept work
- [Compute service reference](docs/compute-service.md): profiles, requests, launch-path checks, GPU admission, tools, usage measurements, task identity, and recovery
- [Shared storage access](docs/shared-storage-access.md): local mounts, SSH, dry runs, transfers, and code snapshots
- [Optional consultation](docs/consultation.md): server-side Codex backend and model configuration
- [Troubleshooting](docs/troubleshooting.md): startup, authentication, TLS, paths, GPU admission, capacity, uncertain submissions, usage measurements, cancellation, and binding mismatches

For JSON CLI usage, run `determined-compute --help`.
