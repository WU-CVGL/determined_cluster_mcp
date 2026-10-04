# Compute service reference

[English](compute-service.md) | [简体中文](compute-service.zh.md)

This document describes the configuration and public MCP interface of the local
Determined compute service. For an agent-neutral sequence for preparing, launching,
and checking work, see [Agent workflow](agent-workflow.md).

## Architecture and trust boundary

```mermaid
flowchart LR
    U[Any local stdio MCP client] --> M[16 MCP tools]
    M --> C[ComputeService]
    C --> D[(local SQLite database)]
    C --> A[Determined API]
    A --> K[Determined cluster]
    P[compute profile] --> C
    M --> S[shared-storage adapter]
    S --> H[mapped shared storage]
```

The MCP server is a local stdio service for one trusted user. It binds `owner` at
startup; no tool accepts an owner argument. Separate processes can use separate owner
names with one database, while collaborators can deliberately share a name. This is a
namespace boundary, not multi-user authentication. A remotely exposed service needs
its own authenticated transport.

`ComputeService` owns planning, idempotent submission, status, logs, usage measurements,
cancellation, pause and resume, discovery, adoption, and conservative
reconciliation. Its local `task_id`
remains stable across restarts and is distinct from the Determined `remote_id`. Keep the
SQLite database on durable local storage. Keep source, data, packages, checkpoints,
logs, and outputs on mapped shared storage.

## Compute profile

Pass the profile with `--profile PATH` or `DETERMINED_COMPUTE_PROFILE`. Its schema is:

```yaml
cluster_identity: optional-deployment-label
mounts:
  - host_path: /shared/projects
    container_path: /workspace
  - host_path: /shared/reference
    container_path: /reference
    read_only: true
defaults:
  image: your-image
  pool: your-pool
  slots: 1
shell_inactivity_seconds: 7200
```

At least one mount is required. `host_path` is the path on cluster agents and need not
exist on the MCP client machine. Requests use `container_path`; container roots cannot
overlap, so each container path maps through one corresponding mount. When validating
a host-path alias against overlapping host roots, the most specific root controls and
read-only wins a tie. `workdir`, `output_dir`, and explicit checkpoint targets must be
under writable mounts; reading reference data under a read-only mount remains valid.
These checks are service policy and do not replace filesystem permissions.

The image, resource pool, and slot count are defaults that a request can override.
`slots` must be a non-negative integer; zero asks for CPU-only auxiliary capacity when
the pool supports it. `shell_inactivity_seconds` is optional and advisory. The service
does not enforce an idle timeout.

`cluster_identity` is an optional operator-facing label. Submitted local records bind
to the profile fingerprint and the resolved Determined endpoint, including this label.
Changing that binding prevents later status, log, usage, cancellation, and
reconciliation operations on those records.

## Request object and planning

`compute_plan` and `compute_launch` accept the same request object:

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | string | Optional display name, at most 128 characters |
| `description` | string or null | Optional display description, at most 2,048 characters |
| `allow_queue` | boolean | Allow submission when current capacity is insufficient; default `false` |
| `kind` | `auto`, `command`, `shell`, `generic`, or `experiment` | Execution mode; default `auto` |
| `interactive` | boolean | Requires shell mode; in auto mode selects `shell` |
| `overnight` | boolean | In auto mode selects `experiment` |
| `command` | string or string array | Command, generic task, or experiment entrypoint; shell mode rejects it |
| `workdir` | absolute container path | Working directory under a writable configured mount |
| `output_dir` | absolute container path | Output directory under a writable configured mount |
| `slots` | non-negative integer | Requested slots; defaults to the profile value |
| `pool`, `image` | string | Optional overrides of profile defaults |
| `code_revision` | string or null | Caller-provided revision or content identifier |
| `experiment_config` | object | Extra experiment configuration; requires experiment mode |
| `parent` | string or null | Generic only: local `task_id` of a generic parent task in the same owner namespace |
| `inherit_context` | boolean | Generic only: inherit the parent's context directory; requires `parent`; default `false` |
| `pausable` | boolean | Generic only: the task can be paused, and resuming reruns it from the start; default `false` |
| `preemption_timeout` | non-negative integer | Generic only: seconds a task gets to stop after a pause request; Determined's default is 0. An experiment sets it in `experiment_config` |

Unknown request fields and upload/context fields are rejected. In auto mode,
`interactive` selects `shell`, then `overnight` or `experiment_config` selects
`experiment`, and all other requests select `command`. Auto mode never selects
`generic`. An explicit `kind` is retained; an overnight command therefore stays a
command and receives an advisory. The four generic-only fields are rejected for every
other kind.

Planning is offline and does not authenticate, inspect capacity, create projects, or
submit work. It returns `kind`, `name`, `description`, `allow_queue`, rendered `config`,
`code_revision`, and `advisories`. If `name` is omitted, the service creates one and
adds an advisory. Commands and shells place the name on the first description line;
experiments and generic tasks use their native name and description fields. Top-level
display metadata overrides matching experiment fields.

Command, generic, and experiment entrypoints create `output_dir`, change to `workdir`,
and then run the command through `/bin/bash -lc`. Command, generic, and shell configs
use `resources.slots`; experiments use `resources.slots_per_trial`. The service supplies
profile bind mounts and manages `COMPUTE_WORKDIR`, `COMPUTE_OUTPUT_DIR`,
`COMPUTE_CODE_REVISION`, and the private submission marker. A request cannot override
those variables or bind mounts.

Experiments require `command` or `experiment_config.entrypoint`, but not both. An
explicit `checkpoint_storage` must have `type: shared_fs`, a writable mapped
`host_path`, and an optional `storage_path` that remains inside that host path. Legacy
`checkpoint_path` and `tensorboard_path` aliases are rejected. If checkpoint storage is
omitted, Determined applies its cluster default, which offline planning cannot inspect.

### Generic tasks

A generic task is Determined's lower-level task type: one container that runs an
entrypoint, with no trials, searcher, or checkpoint lifecycle, which can have child tasks
and, when launched with `pausable: true`, be paused and resumed. It requires a Determined master from the
research-cluster fork 0.40.1 or later. Its plan is a command plan with these
differences:

- The config carries `name`, `description` when set, and `preemption_timeout` when set,
  next to the same `entrypoint`, `resources`, `environment`, and `bind_mounts` as a
  command.
- The plan has a `task_options` object with `parent`, `inherit_context`, and
  `pausable`. Plans of other kinds have no such key.
- A pausable plan has the `generic_restart_safety` advisory.

At launch, `parent` must name a generic task in the same owner namespace that is bound
to a remote ID and to the current profile and endpoint; it is sent as that remote ID.
Otherwise the launch fails before a local record is created. The task is submitted with
an empty context directory, because code and data stay on shared mounts, so
`inherit_context` inherits nothing from a parent launched through this service. No
project is sent, so Determined places the task in its default project, as it does for
experiments launched by this service. Capacity admission uses `resources.slots`, as for
a command.

Generic task lifecycle:

- Exit status 0 ends the task as `COMPLETED`. A non-zero exit or a lost agent ends it as
  `ERROR`. Determined never restarts a generic task automatically.
- Only a task launched with `pausable: true` can be paused; pausing any other generic
  task fails and leaves it running, so it runs once. Pausing stops the task's container.
  The workload is notified through Determined's
  Core API preemption signal and gets `preemption_timeout` seconds (default 0, an
  immediate stop) to exit. A plain script that does not use the Core API is simply
  stopped.
- Resuming starts a new container under the same task ID and runs the entrypoint again
  from the beginning. The workload must be restart-safe: skip outputs that are already
  complete, and resume or clean up partial ones.
- Killing (`compute_cancel`) acts on the task and all its descendants. Pausing acts on
  the task and its pausable descendants, and resuming resumes the paused ones; a child
  that is not pausable keeps running when its parent is paused. The service always sends
  Determined's `noPause` as the opposite of `pausable`, because masters differ in how they
  treat an unset value.

## Start the MCP server

Start one persistent stdio process per configured client:

```bash
determined-compute-mcp \
  --profile /absolute/path/to/compute-profile.yaml \
  --db /absolute/local/path/to/tasks.sqlite3 \
  --owner your-owner \
  --secrets-file /absolute/path/to/credentials.env \
  --verify-ssl
```

`--profile`, `--db`, and `--owner` correspond to
`DETERMINED_COMPUTE_PROFILE`, `DETERMINED_COMPUTE_DB`, and
`DETERMINED_COMPUTE_OWNER`. `--storage-config` corresponds to
`DETERMINED_COMPUTE_STORAGE`; `--secrets-file` can instead be supplied through
`DETERMINED_COMPUTE_SECRETS`. The API URL, token, and TLS verification default to
`DET_MASTER`, `DET_API_TOKEN`, and `DET_VERIFY_SSL`; keep credentials in the existing
provider or secrets file rather than the profile, database, tool arguments, or reports.

The default database path is `~/.local/state/determined-compute/tasks.sqlite3`, but
MCP deployments should specify an absolute local path. MCP rejects `:memory:`. After an upgrade, restart every MCP
process that shares the database so all processes load the same tool set and additive
schema.

Optional client-side access to mapped storage uses the same profile and a separate
storage configuration. See [Shared-storage access](shared-storage-access.md).

## MCP API

The server exposes 16 tools. The `owner` below is always the startup-bound
namespace and never a tool argument.

| Tool | Arguments | Return value and effect |
| --- | --- | --- |
| `compute_plan` | `request` | Offline normalized plan; no cluster or database mutation |
| `compute_launch` | `request`, `request_id` | Persisted task record; may submit once |
| `compute_status` | `task_id` | Local task record, refreshed remote state, and remote entity when bound |
| `compute_logs` | `task_id`, optional `tail=200` | Chronological list of the newest remote log records |
| `compute_usage` | `task_id`, optional `window_seconds=3600`, `allocation_id`, `trial_id`, `metrics`, `include_samples=false` | Read-only summary of one task's measured CPU, memory, and GPU use |
| `compute_cancel` | `task_id` | Updated record, remote cancellation response, and acknowledgement |
| `compute_pause` | `task_id` | Experiments and generic tasks: record, remote response, and `pause_acknowledged` |
| `compute_resume` | `task_id` | Experiments and generic tasks: record, remote response, and `resume_acknowledged` |
| `compute_reconcile` | `task_id`, `remote_id` | Record bound only after marker verification |
| `compute_list_tasks` | none | Local records in the bound owner namespace |
| `compute_discover` | `kind`, optional `limit=50`, `offset=0` | One current-account remote page; no local mutation |
| `compute_adopt` | `kind`, `remote_id` | Idempotently registered local record; no remote submission |
| `compute_resources` | optional `slots=1`, `pool` | Current scheduler capacity and candidate pools |
| `storage_check` | `path` | Access information for a mapped container path |
| `storage_sync` | `local_dir`, `shared_dir`, optional `dry_run=true` | Preview or copy local directory contents to shared storage |
| `storage_fetch` | `shared_dir`, `local_dir`, optional `dry_run=true` | Preview or copy shared directory contents locally |

### Plan, capacity, and launch

Call `compute_plan` first and review resolved paths, mode, image, pool, slots, and
advisories. `compute_resources` is a live snapshot, not a reservation. Positive slot
requests inspect schedulable agent slots; zero checks auxiliary-container capacity.
Candidate pools are suggestions and are never substituted automatically.

`compute_launch` checks capacity unless `allow_queue` is explicitly true. Its
`request_id` is an idempotency key within the bound owner. Repeating the same ID and
equivalent request returns the established record. Reusing it with different content
returns `idempotency_conflict`. Once a local row has claimed an ID, a retry cannot
submit a second remote task, even after restart.

The adapter sends command and shell configs as mappings. It serializes experiment
configs as YAML and requests activation. It serializes generic task configs as YAML and
sends them with an empty `contextDirectory`, no `projectId`, and the resolved `parentId`,
`inheritContext`, and `noPause` options. It rejects source upload aliases, never
creates a project, removes API envelopes, sanitizes retained identity material, and
returns an entity with an `id`.

The generic task `name` and `description` config fields exist only on newer masters;
an older master rejects them as unknown fields while strictly parsing the config. That
parse happens before the master stores anything, so when the create request fails with
HTTP 400 or 500 and the message names the unknown field `name` or `description`, the
adapter retries once without both fields. The launch result then carries `warnings` with
a `generic_task_metadata_unsupported` entry, and the local record keeps both values. No
other failure is retried. Master launch warnings, such as a request exceeding current
slots, appear in `warnings` with code `launch_warning`. An idempotent repeat of the
launch returns the stored record without `warnings`.

### Task records, status, logs, and cancellation

A public task record includes `task_id`, `request_id`, `owner`, `origin`, `kind`, local
`state`, `remote_id`, `remote_state`, display metadata, paths, revision, cluster/account
binding fields, an optional fixed `error_code`, and timestamps. Internal request hashes,
profile hashes, and submission markers are never public. The service stores no full
request body, generated config, API response, logs, or raw exception text in a task
record.

`compute_status` returns local state without contacting Determined when no remote ID is
bound. Otherwise it fetches the entity, updates `remote_state`, and includes the
sanitized entity as `remote`. For a generic task, the entity combines the task record
(`GET /api/v1/tasks/{id}`) with its submitted config (`GET /api/v1/tasks/{id}/config`),
whose environment variables are redacted, and adds `resourcePool`, `name`, and
`description` from that config. Its `taskState` is reported in `remote_state` with the
`GENERIC_TASK_STATE_` prefix replaced by `STATE_`, the experiment vocabulary:

| `remote_state` | Meaning |
| --- | --- |
| `STATE_ACTIVE` | Queued or running |
| `STATE_STOPPING_PAUSED` | Pause requested; the container is stopping |
| `STATE_PAUSED` | Paused; not terminal, and `compute_resume` can continue it |
| `STATE_STOPPING_COMPLETED`, `STATE_STOPPING_ERROR`, `STATE_STOPPING_CANCELED` | Ending |
| `STATE_COMPLETED` | Terminal: the entrypoint exited with status 0 |
| `STATE_ERROR` | Terminal: non-zero exit or lost agent |
| `STATE_CANCELED` | Terminal: killed |

The entity's `allocations` list each run of the task; a resumed task has one allocation
per run. A stale `pending` or `submitting` row becomes
`submission_uncertain`; this never causes automatic resubmission.

`compute_logs` requires a positive `tail`. Command, shell, and generic task logs come
from their task log API; a resumed generic task's logs include every run. Experiment logs come from the highest numeric trial ID, which a server-side
sort selects even when an experiment has more than 100 trials; an experiment with no
trials returns an empty list. Results are ordered oldest to newest. A task with no
remote ID also returns an empty list.

`compute_cancel` uses the task kill endpoint for commands and shells, the experiment
cancel endpoint for experiments, and the generic task kill endpoint, which also kills
the task's descendants but never its ancestors, for generic tasks. It requires a bound remote ID and returns
`cancellation_acknowledged: true` when the API call completes. Remote termination alone
does not prove success; inspect exit information and expected shared-storage artifacts.

### Pause and resume

`compute_pause(task_id)` and `compute_resume(task_id)` apply to experiments and generic
tasks; a command or shell returns `unsupported_kind`. They apply the same owner,
binding, and adoption checks as `compute_cancel`, and a record without a remote ID
returns `remote_id_unknown`. Each returns the local record, the remote acknowledgement
as `remote`, and `pause_acknowledged` or `resume_acknowledged`; poll `compute_status`
for the resulting state.

For an experiment, pause and resume call Determined's experiment pause and activate
endpoints. The experiment reports `STATE_PAUSED` as soon as the pause is accepted, while
its trials receive the preemption signal and get the experiment's `preemption_timeout`
(one hour by default) to save a checkpoint and exit. Resuming continues each trial from
its latest checkpoint, or from the beginning when it has none. Determined refuses a
pause or resume of an experiment in an incompatible state with HTTP 400.

For a generic task, pause and resume call the task pause and unpause endpoints, which
also act on its pausable descendants; see [Generic tasks](#generic-tasks). The task
reports `STATE_STOPPING_PAUSED`, then `STATE_PAUSED`. Resume is accepted only for a task
in `STATE_PAUSED` whose descendants have finished stopping, and it runs the entrypoint
again from the start. A master with the research-cluster fork's generic-task fixes
refuses a pause, resume, or kill with HTTP 404 for a missing task, HTTP 400 for a state
that does not allow it (such as pausing a paused task or one that is not pausable),
and HTTP 409 while another pause, resume, or kill is in progress; these are ordinary
errors that carry the master's reason. An older master reports the same refusals as
server errors, which arrive as `submission_uncertain` errors whose message includes the
master's reason; check `compute_status` before repeating the call.

For a running shell, use the sanitized `reconnectCommand`, currently
`det shell show_ssh_command <remote-id>`. The adapter removes `privateKey`; never put
private key material in task records or reports.

### Task usage measurements

`compute_usage` is read-only and summarizes the measured CPU, memory, and GPU use of one
owned task; `compute_resources` describes scheduler capacity instead. It requires a
Determined master from the research-cluster fork 0.40.1 or later on which an
administrator has configured `integrations.task_resources` (`prometheus_url` and
`det_cluster`).

The service validates arguments first and then applies the same owner and binding
checks as `compute_status`. A record without a remote ID returns `remote_id_unknown`;
reconcile it first. An adopted task's remote owner is verified again. The service then
asks the master whether task resources are available: a disabled integration returns
`task_resources_disabled`, and a master without the API returns
`task_resources_unsupported`. Neither is retryable.

For a command, shell, or generic task, `determined_task_id` is the remote ID. An experiment reports one
trial: the highest-ID trial by default, or `trial_id` when given. A requested trial that
belongs to another experiment returns `trial_not_found`; a nonexistent or inaccessible
trial ID returns Determined's HTTP 404. Other kinds reject `trial_id`. The service
measures the selected trial's newest Determined task. An experiment with no trial, or a
trial with no task, returns `task_not_started`. `allocation_id` restricts results to one
allocation listed for that task; any other value returns `allocation_not_found`.

`window_seconds` must be 60 through 604,800 (seven days); the default is 3,600. The
window ends at the task end time for an ended task, capped at the current time, and
otherwise at the current time. If every selected allocation ended before that, as for a
paused trial or an earlier allocation named by `allocation_id`, the window ends when the
last of them ended instead. It starts `window_seconds` earlier, but never before the
task start or the requested allocation's start, and always at least one second before
its end. `window.anchor` reports which end applied: `task_end`, `allocation_end`, or
`now`. The step is the larger of 15 seconds and the window length divided by 1,439,
rounded up to a whole second, so no series exceeds 1,440 points. `metrics` is a
non-empty list drawn from `allocation_active` (count), `cpu_cores` (cores),
`memory_working_set_bytes` and `memory_rss_bytes` (bytes), `gpu_utilization_percent`
(percent), `gpu_memory_used_bytes` (bytes), `gpu_power_watts` (watts), and
`gpu_temperature_celsius` (celsius); omit it to keep every returned series.

The result contains:

| Field | Meaning |
| --- | --- |
| `task_id`, `kind`, `remote_id` | Local task identity |
| `determined_task_id` | Determined task whose measurements were read |
| `trial` | `null` for commands, shells, and generic tasks; otherwise `id`, `state`, `selection` (`latest` or `requested`), `experiment_trial_count` (`null` when `trial_id` was given), `task_count`, and the trial progress and summary-metric fields described below |
| `resource_pool` | The task's pool as `name` and the operator-written `description` from Determined, trimmed and truncated to 4,096 characters. `description` is `null` when the pool has none or is not in the pool list returned to this account. The whole field is `null` when the pool name is unknown |
| `task_start_time`, `task_end_time` | Lifetime of the Determined task |
| `allocations` | Each allocation's `allocation_id`, `state`, `is_ready`, UTC `start_time` and `end_time`, `slots`, `exit_reason` (at most 1,024 characters), and `status_code` |
| `allocation_details_limit` | Present, as 8, only when the task has more than 8 allocations; see below |
| `allocation_id` | Requested allocation filter, or `null` |
| `window` | `start` and `end` in Unix seconds, `step` in seconds, plus `start_at`, `end_at`, `anchor`, and `expected_points` |
| `series` | One summary per metric and label set |
| `gpus` | One GPU comparison per allocation; see below |
| `warnings` | Determined's `{code, message}` warnings, passed through unchanged |
| `context_unavailable` | Context lookups that failed: `resource_pool`, `allocation_details`, or `gpu_models` |
| `explanation`, `advisory` | How to read this result |
| `observed_at` | Time the service built the result |

Each series has `metric`, `unit`, the labels `allocation_id`, `node`, and `gpu_uuid`,
`gpu_model`, `points`, `available_points`, `first_at`, `last_at`, and `last`, `min`,
`max`, `mean`, `p50`, and `p95` over the available samples. `p50` and `p95` are
nearest-rank percentiles. `gpu_model` is the model name the Determined agent reports for
`gpu_uuid`, or `null` for a non-GPU series or an unknown device. A
`gpu_utilization_percent` series also has `idle_fraction`, the share of its available
samples below 10%.

Values are point samples taken every `step` seconds, so `min`, `max`, `mean`, and the
percentiles describe those samples rather than every instant. A null or missing value
means no measurement, never zero use. `allocation_active` above zero means the
allocation was running. CPU and memory series are per allocation and node; GPU series
are per GPU UUID and cover the whole assigned device, which can include other processes.
Inspect `warnings`, such as `rss_unverified` or `gpu_full_device`, before drawing
conclusions. An empty `series` list means no data for the window, not an idle task; if a
`metrics` filter removed every returned series, `explanation` names the metrics that
were returned. When `trial_id` is omitted and the experiment has several trials,
`explanation` states how many exist and which one is reported.

`gpus` compares the GPUs within each allocation. It has one entry per `allocation_id`
with GPU utilization or memory series and uses every such series returned for the
window, even those a `metrics` filter hides from `series`; with `allocation_id`, it
covers that allocation only. Each entry has `gpu_count` (distinct GPU UUIDs with a
returned utilization or memory series, even if every sample is null), `requested_slots`
(the allocation's slot count, or `null` when unknown), `gpu_models` (distinct known
model names, possibly empty), and statistics of per-GPU mean utilization:
`mean_utilization_percent` averages the per-GPU means so each GPU counts equally,
`min_gpu_mean_utilization_percent` and `max_gpu_mean_utilization_percent` are the lowest
and highest, `utilization_spread_percent` is their difference, and
`least_utilized_gpu_uuid` names the lowest, with ties going to the lexicographically
first UUID. These utilization statistics include only GPUs with at least one available
utilization sample, so they can cover fewer GPUs than `gpu_count`. `idle_fraction` is
instead the share of all the allocation's utilization samples below
`idle_threshold_percent` (10). `max_memory_used_bytes` is the largest single-GPU memory
sample in the allocation, not a true peak; GPU memory capacity is not reported. A large
spread points to an idle or straggling GPU, starting with `least_utilized_gpu_uuid`, and
a high `idle_fraction` means the GPUs spent much of the window below the threshold. A
`gpu_count` below `requested_slots` means fewer GPUs returned a series than the
allocation holds; check `warnings` and monitoring coverage before calling the rest
unused.

For an experiment, `trial` also carries Determined's values for the whole trial, not for
the measurement window: `total_batches_processed`, `wall_clock_seconds`, and `restarts`,
each `null` when missing or malformed. `total_batches_processed` is the highest reported
`steps_completed`, and `restarts` is capped at the experiment's `max_restarts`.
`batches_per_second_lower_bound` divides batches by wall-clock seconds and is `null`
when either is unknown or wall-clock time is zero; its unit is whatever the workload
reports as `steps_completed`. It is a floor, because `wall_clock_seconds` adds up each
allocation from when Determined first reports its resources pulling or running until it
ends, or until now while it runs. That can include image pull, startup, initialization,
and allocations lost to restarts; scheduler queue time and gaps between allocations,
such as pauses, are not counted. `summary_metrics` keeps Determined's per-group,
per-metric statistics, reduced to `type` and the finite numeric `count`, `sum`, `min`,
`max`, `last`, and `mean`; group names such as `avg_metrics` (training) and
`validation_metrics` are Determined's and are passed through unchanged. At most 100
metric entries are kept: `validation_metrics` first, then `avg_metrics`, then other groups
by name, with metrics in name order within each group; `summary_metrics_truncated` is
true when more existed. These fields depend on the
workload reporting through Determined's Core API. A `total_batches_processed` of 0,
which `explanation` then notes, is expected for a workload that does not, such as a
plain bash entrypoint, or that has not reported yet, and does not mean it made no
progress. `summary_metrics` is `{}`
when the workload reports no metrics.

Pool, allocation-detail, and GPU-model context is best-effort and read after the
measurements. A Determined API error in one of these lookups, including a transport
failure or malformed response, adds `resource_pool`, `allocation_details`, or
`gpu_models` to `context_unavailable` and leaves the affected fields empty or `null`;
the measurements are still returned. After a transport failure, the remaining lookups are
skipped and reported the same way, so an unresponsive master delays the result by one
timeout rather than one per lookup. A submitted task needs one extra entity read, which
`compute_logs` does not make, to learn its pool name, and a failure there reports
`resource_pool`; an adopted task reuses the entity read for its ownership check.
Determined serves an ended command or shell for only 24 hours after it ends and not
after a master restart; for such a task, `resource_pool` is then `null` without a
`context_unavailable` entry. The
pool list is read only when the pool name is known. Allocation details are read for the
first eight allocations in Determined's order (allocations without an end time, such as
queued or running ones, first, then most recently ended) plus a requested
`allocation_id`; other allocations keep `null` details, and `allocation_details_limit`
reports the limit. The agent list, which supplies GPU model names, is read only when a
GPU series exists. When RBAC hides device UUIDs from the current account, `gpu_model` is
`null` and `gpu_models` is empty without any `context_unavailable` entry.

`include_samples=true` adds `samples_omitted`. When `samples_omitted` is false, each
series also has `samples` as `[unix_seconds, value_or_null]` pairs. When the selected
series together exceed 2,880 points, the service omits samples and reports
`samples_limit`; narrow the window, select fewer metrics, or choose one allocation.

The master limits a query to a seven-day range, a 15-second minimum step, 1,440 points
per series, and a 10-second timeout, and runs at most four resource queries at once.
HTTP 503 means the measurement backend is busy or unavailable and is retryable. The
master rejects an end more than 60 seconds ahead of its own clock with HTTP 400, so a
client clock far ahead of the master can cause that error. HTTP 404 means the Determined
task, or a requested trial ID, is missing or inaccessible. See
[troubleshooting](troubleshooting.md#usage-measurements-are-unavailable-or-empty).

### Discover and adopt

`compute_discover` accepts `kind` equal to `command`, `shell`, `generic`, or `experiment`. `limit`
must be 1 through 100 and `offset` must be non-negative. It queries only tasks owned by
the currently authenticated Determined account and returns the actual cluster ID,
account identity, sanitized metadata, any matching `local_task_id`, and consistent
pagination including `next_offset`. It neither writes a local record nor submits work.
Command, shell, and generic task remote IDs are UUIDs; experiment remote IDs are positive
integers.

Generic tasks are listed and their owner verified through Determined's generic task list,
which the research-cluster fork has from WU-CVGL/determined#27 on. Against an older master,
discovering generic tasks fails with `unsupported`, and adopting one fails with
`ownership_unavailable`, because nothing else reports a generic task's owner.

`compute_adopt` fetches one remote task and verifies both its normalized ID and
`userId` against `/me` before writing. Administrative visibility cannot be used to
adopt another user's task. Registration identity is the local owner, actual cluster ID,
kind, and remote ID; the record also binds and verifies the authenticated user ID. A
previously submitted record in the same database is returned unchanged rather than
replaced.

New adopted records have `origin: "adopted"`, local `state: "adopted"`, and an internal
adoption request ID. They retain only whitelisted identity, state, name, and description
metadata. Unknown `workdir`, `output_dir`, and `code_revision` are exposed as `null`;
the store does not infer them or retain raw remote configuration. Later status, logs,
usage, and cancellation re-check the actual cluster and account binding. Adopted tasks
do not use the submitting profile as their authority and gain no storage permissions.

Use discovery and adoption for work created by the WebUI, native CLI, or another device
under the same account. Use reconciliation for a local submission whose acceptance was
uncertain. An adopted task cannot be reconciled or used as a launch retry.

### Reconciliation and recovery

A transport timeout can leave remote acceptance unknown. The service preserves the
local task and returns its `task_id` in error details. It does not resubmit that request
automatically. `compute_reconcile(task_id, remote_id)` fetches the proposed entity and
binds it only if its reserved `COMPUTE_SUBMISSION_MARKER` equals the local unguessable
marker. For a generic task the marker is read from the submitted config; find the task
ID in the WebUI or with `det task list`. A mismatch returns `identity_mismatch`. First-line description markers are
considered only for migrated legacy records without stored display metadata.

This marker separates reconciliation from adoption: an uncertain local submission with
a matching marker must be reconciled, while an independently created remote task can be
adopted. If evidence is unavailable, investigate rather than launching the same work
again.

Submitted local tasks remain bound to the original profile fingerprint and endpoint.
Adopted tasks remain bound to the actual cluster ID and authenticated user ID. These
checks prevent a changed profile or account from operating on an unrelated task.

### Errors

MCP failures use `isError: true`; their text content is compact JSON of this form:

```json
{"error":{"code":"invalid_request","message":"...","retryable":false,"details":{}}}
```

`retryable` and `details` appear only when available, and structured content is null.
Safe details can include the local task ID and capacity information. Authentication,
permission, transport, and response-shape failures are errors rather than empty
results. Error messages and reports may contain sanitized commands, paths, IDs, states,
and error classes, but must not include credentials or secret-file contents.

A Determined HTTP failure, including a gRPC-gateway error body, appears as
`<status> <message>`. HTTP 429 and 5xx responses other than 501 are retryable; 501 means
the master lacks the route. Usage-specific codes are described in
[Task usage measurements](#task-usage-measurements).

On the Determined fork 0.40.1 or later with basic authorization, only the task's
Determined owner or an administrator can kill or cancel commands, shells, and
experiments. For a task owned by another account, `compute_cancel` therefore returns
HTTP 403 for a command or shell and HTTP 404 `experiment '<id>' not found` for an
experiment. Submitted records bind to the profile and endpoint rather than the account,
so switching credentials to another account can produce these errors. Cancel with the
owning account or ask an administrator. Experiment pause and resume need the same
permission as cancelling the experiment. Generic task kill, pause, and resume are likewise
limited to the task's owner or an administrator and return HTTP 403 otherwise.
