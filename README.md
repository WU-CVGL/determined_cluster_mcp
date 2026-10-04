# Determined Cluster MCP

[English](README.md) | [简体中文](README.zh.md)

Run Determined commands, shells, generic tasks, and experiments through a local stdio MCP server, and pause and resume experiments and generic tasks. Code, data, checkpoints, and outputs stay on mapped shared storage. Any MCP client that can start a local stdio server can use the service with its own model.

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
DET_MASTER=http://determined.example.org:8080
DET_API_TOKEN=replace-with-your-token
```

`DET_USERNAME` and `DET_PASSWORD` are also supported. Do not commit the credentials file. Fill `profile.yaml` with the administrator-provided image, resource pool, cluster-agent host paths, and container mount paths. Use container paths in task requests.

HTTPS is optional: set `DET_MASTER=https://determined.example.org`, add `--verify-ssl` to the MCP arguments, and in the client's `env` pass `REQUESTS_CA_BUNDLE` for a private CA and `NO_PROXY` when a proxy cannot reach the master. See [optional HTTPS](docs/compute-service.md#optional-https).

Compute tasks do not need a client storage configuration. Storage tools automatically use a local shared path when it matches the configured `host_path`. For a custom local mapping or login-node SSH, copy `cfg/storage-access.example.yaml` to `.local/storage.yaml`, edit it, and add `--storage-config /absolute/path/to/.local/storage.yaml` to the MCP arguments.

## Connect a stdio MCP client

Add this server in the syntax used by your MCP client. Replace every example value with an absolute local path or a deployment value:

```json
{
  "command": "/absolute/path/to/determined_cluster_mcp/.venv/bin/determined-compute-mcp",
  "args": [
    "--profile", "/absolute/path/to/determined_cluster_mcp/.local/profile.yaml",
    "--secrets-file", "/absolute/path/to/determined_cluster_mcp/.local/credentials.env"
  ]
}
```

The credentials select the Determined account. The server acts only on that account's tasks and keeps no task records: tools address tasks by Determined's own IDs, and keeping a record of submitted work is up to the caller. If SSH or a private CA is required, pass the needed environment to the stdio process; see troubleshooting below.

When upgrading from a version with a task database, remove `--db` and `--owner` from the client configuration and `cluster_identity` from the profile; old database files can be deleted. See [upgrading](docs/compute-service.md#upgrading-from-a-version-with-a-task-database).

## Install the agent skill (optional)

The MCP tools work without it. The `intensive-compute-runner` skill adds task guidance for agents that load skills, such as Codex and Claude Code. From the repository root, link the skill directory into the agent's skills directory:

```bash
# Codex
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
ln -s "$PWD/skills/intensive-compute-runner" "${CODEX_HOME:-$HOME/.codex}/skills/"

# Claude Code
mkdir -p "$HOME/.claude/skills"
ln -s "$PWD/skills/intensive-compute-runner" "$HOME/.claude/skills/"
```

Start a new agent session to load it. A link keeps the skill current after `git pull`, and the skill's relative links reach this checkout's `docs/` through it, so link rather than copy and keep the checkout in place. To use the skill in one Claude Code project only, link it into that project's `.claude/skills/` instead. The skill expects the MCP server above to be connected. Remove the link to uninstall.

## Documentation

- [Agent workflow](docs/agent-workflow.md): prepare, plan, launch, monitor, and accept work
- [Compute service reference](docs/compute-service.md): profiles, requests, tools, usage measurements, task identity and ownership, and unconfirmed launches
- [Shared storage access](docs/shared-storage-access.md): local mounts, SSH, dry runs, and transfers
- [Troubleshooting](docs/troubleshooting.md): startup, authentication, TLS, proxies, paths, capacity, unconfirmed launches, usage measurements, and refused task operations
