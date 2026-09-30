# Agent instructions

[English](AGENTS.md) | [简体中文](AGENTS.zh.md)

Follow the caller's requested scope. Read only the route needed for the task:

- Cluster workload or storage operation: [agent workflow](docs/agent-workflow.md)
- TaskSpec fields, tools, job handles, or errors: [compute reference](docs/compute-service.md)
- Local mount, SSH, or file transfer: [shared storage access](docs/shared-storage-access.md)
- Startup or runtime failure: [troubleshooting](docs/troubleshooting.md)

Use the administrator or project configuration for the API URL, account, image, pool, and mount paths. Never invent deployment values or expose credentials.

Documentation or code explanation does not require cluster access. For an ordinary agent task, perform read-only live queries when the caller's requested scope needs current task, log, usage, storage, or capacity information; use mutating tools only when the requested work requires them.

This repository is the canonical documentation for MCP installation, configuration, workflow, API use, and troubleshooting; keep English and Chinese synchronized, use relative links, and keep these instructions independent of any specific deployment or sibling repository.
