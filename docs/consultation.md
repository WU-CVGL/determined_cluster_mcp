# Optional consultation backend

[English](consultation.md) | [简体中文](consultation.zh.md)

Any local stdio MCP client can plan with its own agent and call the deterministic
compute and storage tools. The standard [agent workflow](agent-workflow.md) does not
require Codex or server-side consultation.

Consultation is an optional read-only advice service. The built-in backend runs Codex
in an independent worker, gives it a repository compute skill plus caller-curated
context, and persists its result. Advice never launches, submits, cancels, or edits
anything. The backend model is separate from the calling client's model.

## Enable the Codex backend

The MCP server defaults to `--consultation-backend none`. In that mode it exposes the
15 base tools, does not import the consultation worker, and does not require Codex,
`--repo-root`, or `skills/intensive-compute-runner/SKILL.md`.

To enable the backend, install and sign in to Codex on the server machine. The selected
repository root must exist and contain
`skills/intensive-compute-runner/SKILL.md`. Start the server with:

```bash
determined-compute-mcp \
  --profile /absolute/path/to/profile.yaml \
  --db /absolute/local/path/to/tasks.sqlite3 \
  --owner your-owner \
  --secrets-file /absolute/path/to/credentials.env \
  --verify-ssl \
  --repo-root /absolute/path/to/repository \
  --consultation-backend codex \
  --consultation-model MODEL_ID \
  --consultation-codex-bin /absolute/path/to/codex
```

`--consultation-model` is optional; the current default is `gpt-5.6-sol`.
`--consultation-codex-bin` is also optional and defaults to `codex` on `PATH`.
`--repo-root` can instead be supplied through `DETERMINED_COMPUTE_REPO_ROOT`. These
are deployment options, not MCP tool arguments. Consultation adds
`compute_consult` and `workflow_status`, bringing the total to 17 tools.

The worker stores its tables in the same local SQLite file passed with `--db`. Protect
that file as service state because it contains questions, curated context, lifecycle
logs, and results. The repository and database must remain accessible to the detached
worker process.

## MCP interface

`compute_consult(question, request_id, context?)` validates and persists one request,
starts a detached worker, and immediately returns a small acceptance object containing
`workflow_id`, `request_id`, `status`, `deduplicated`, and `created_at`. It does not wait
for Codex.

`workflow_status(workflow_id)` returns the owner-scoped persisted state, timestamps,
up to 100 ordered lifecycle log records, final `result` or sanitized `error`, plus
`stale` and `recoverable`. Looking up another owner's workflow behaves like a missing
workflow. The service derives owner from the MCP process; callers cannot supply it.

Statuses are `queued`, `running`, `succeeded`, `failed`, and `timed_out`. Poll until a
terminal status. A client disconnect does not stop the independent worker.

The pair `(owner, request_id)` is the idempotency key. Repeating the same question and
context returns the existing workflow. Reusing the key for different content returns
`workflow_conflict`.

Questions are limited to 16 KiB and final results to 64 KiB. Context is an optional
finite JSON object limited to 64 KiB. Keys that look like passwords, tokens, API keys,
cookies, private keys, authorization, secrets, or credentials are rejected recursively. This
key check is not a secret scanner: never put secrets in the question or under an
innocent-looking context key. The repository skill is limited to 128 KiB.

## Lifecycle and isolation

`WorkflowManager.submit()` commits the queued row before spawning a detached Python
worker. The worker transactionally claims one workflow, marks it running, and starts
Codex. Heartbeats and recorded process IDs support conservative recovery; they do not
indicate Determined experiment health or keep a shell alive.

The worker invokes Codex with an argument array and the generated prompt on standard
input, so no shell evaluates caller text. It uses `--ignore-user-config`,
`--ignore-rules`, `--ephemeral`, `--sandbox read-only`, JSON event mode, the configured
model, and an explicit `mcp_servers={}` override. The repository root is its working
directory.

The prompt tells Codex to diagnose or plan only, forbids launch, cancellation,
submission, mutation, edits, MCP calls, and delegation, and includes the repository
compute skill, question, and curated context. The read-only sandbox and empty MCP
configuration limit model-side actions, but do not replace host filesystem permissions.

The worker rebuilds the environment from a small allowlist: normal user, locale, path,
TLS, temporary-directory variables, and `CODEX_HOME` for authentication. It omits
Determined, storage, and other service credentials. This also prevents recursive calls
to the service's MCP server.

Each run has a wall-clock timeout, currently 900 seconds by default. On timeout, the
worker terminates the whole Codex process group and persists `timed_out`. A nonzero
Codex exit persists `failed` with a bounded error. Successful output is read from the
last-message file and truncated at 64 KiB when necessary.

## Recovery and operations

A queued workflow becomes recoverable after its dispatch heartbeat is stale, currently
120 seconds by default. Repeating the same idempotent submission dispatches a
replacement worker. The transactional claim permits only one worker to start Codex.

A running workflow is recoverable only when its heartbeat is stale and both its worker
and Codex process IDs are no longer alive. There is no automatic retry loop. An
operator can explicitly run the worker command after checking the persisted state:

```bash
python -m determined_compute.agent_worker worker \
  --db /absolute/local/path/to/tasks.sqlite3 \
  --repo-root /absolute/path/to/repository \
  --workflow-id WORKFLOW_ID \
  --codex-bin /absolute/path/to/codex \
  --model MODEL_ID \
  --timeout-seconds 900 \
  --stale-after-seconds 120
```

The claim ignores a duplicate worker while a valid run is active. When reclaiming a
stale running workflow, it first verifies that both recorded processes have exited and
writes a recovery event to the persisted log. PID reuse can conservatively delay
recovery until an operator inspects the record. A hard crash can cause a read-only
consultation to run again after the stale interval.

SQLite provides durable state and transactional claiming on one host; it is not a
distributed queue. Use a process supervisor to revisit queued records after a host
restart, and keep detached workers on the host that owns the local database and
repository. Do not put this database on shared NFS.

Codex output is advisory and untrusted. Review it before passing any proposed operation
to `compute_launch`, `compute_cancel`, or a storage transfer. When upgrading Codex,
verify the installed CLI contract with `codex exec --help`.
