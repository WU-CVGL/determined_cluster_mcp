# Architecture

[English](architecture.md) | [简体中文](architecture.zh.md)

This document describes the target architecture of the compute MCP and the changes it
needs in the Determined fork. It is written for maintainers of both repositories. For the
current MCP interface, see [Compute service reference](compute-service.md).

Fork paths are relative to the fork root at `8e26a69` (release 0.40.1). MCP paths are
relative to this repository at `2404d0d`. "PR #1" is the unmerged
`feat/research-workflow-support` branch. Citations come from reading the source, not from
running it.

## Principles

1. **Each capability lives where its source of truth lives.**
   - The master owns identity, idempotency, admission, placement, and failure class.
   - The agent owns node facts.
   - The workload creates its own directories.
   - The MCP owns research intent, per-call policy that is narrower than RBAC, the user's
     working tree, and the interpretation of results.
2. **The job row is the ledger.** `tasks.job_id`, `experiments.job_id`, and
   `jobs.owner_id` already exist (`master/pkg/model/task.go:78`, `experiment.go:336`,
   `job.go:87-94`). `job_id` is the only handle.
3. **One task contract.** The four existing create requests are the contract, and an
   evaluation is a `dry_run` of the same request.
4. **The scheduler decides.** Evaluation and immediate admission reuse the pool's own
   `Schedule` and `findFits`. Nothing re-derives placement.
5. **Failures are typed where they happen and recorded once.** Every allocation that ends
   gets exactly one exit class.
6. **No compatibility paths.** There is one minimum fork version and one version check,
   with no probing and no client-side substitutes.

## Layers and responsibilities

| Capability | Owner | What the MCP keeps |
|---|---|---|
| Identity, owner, submitted request | new columns on the `jobs` row | the `job_id` |
| Idempotent submit | master, unique on `(owner_id, idempotency_key)` | the `request_id` minted by `compute_plan` |
| Spec merge, defaults, bind mounts | master create path, pool `task_container_defaults`, templates | `TaskSpec` rendered to a create request, with explicit pool and slots |
| Capacity and placement answer | RM evaluation, served by `dry_run` | presents the verdict |
| Queue or immediate admission | RM scheduler tick, under `rp.mu` | maps `allow_queue` to admission |
| GPU model, total memory, single node | scheduler hard constraints | spec fields |
| Free GPU memory, utilization, mount sources | agent, before `CreateContainer` | nothing |
| Output directories | the workload | renders `mkdir -p` |
| Exit class and retries | allocation, trial, command | explains the class |
| Status, list, cancel across clients | `GetSubmission`, `ListSubmissions`, `CancelSubmission` | pass-through |
| Code delivery | existing context directory or model definition | git enumeration, secret rules, manifest |
| Data, outputs, checkpoints | shared storage via pool bind mounts; `checkpoint_storage` | file transfer (`storage_sync`, `storage_fetch`) |
| Condition-triggered launches | a separate service over the create envelope, if ever built | nothing |
| Pool allow-list, max slots, `allow_queue`, `overwrite` | MCP | all of it |
| Usage interpretation | MCP | all of it |
| Quotas across clients | existing group `max_slots` and config policies | nothing |

## Platform changes

### Ledger columns on `jobs`

The migration is `2026MMDD000000_job-submissions.tx.up.sql`. It has a `.down.sql`,
following the fork's own precedent.

```sql
ALTER TABLE jobs
  ADD COLUMN idempotency_key text,
  ADD COLUMN request_digest  text,
  ADD COLUMN request         jsonb,                        -- canonical, redacted
  ADD COLUMN admission       text NOT NULL DEFAULT 'QUEUE';
CREATE UNIQUE INDEX jobs_owner_idempotency_key ON jobs (owner_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;
```

- **No backfill.** Jobs created before the upgrade show NULL. Their submit time comes from
  `tasks.start_time` or `experiments.start_time`.
- **Managed creates only.** Keys are accepted only there, because `PutExperiment` leaks a
  job row on replay (`master/internal/db/postgres_experiments.go:436-462`).
- **Replay semantics** copy dynamic pools: the same hash replays and a different hash
  conflicts (`master/internal/db/postgres_dynamic_resource_pools.go:56-116`).

### Create envelope

`LaunchCommand`, `LaunchShell`, `CreateGenericTask`, and `CreateExperiment` each gain one
optional field. No create endpoint is added.

```proto
enum Admission { ADMISSION_UNSPECIFIED = 0; ADMISSION_QUEUE = 1; ADMISSION_IMMEDIATE = 2; }
message SubmitOptions {
  string idempotency_key = 1;  // optional; <= 128 chars of [A-Za-z0-9._:-]
  Admission admission = 2;     // UNSPECIFIED means QUEUE
  bool dry_run = 3;
}
message SubmitResult {
  string job_id = 1;                           // empty on dry_run
  bool replayed = 2;
  string request_digest = 3;
  AdmissionOutcome outcome = 4;                // QUEUED | PLACED | REJECTED | PENDING
  SchedulingEvaluation evaluation = 5;         // dry_run only
  google.protobuf.Struct effective_config = 6; // dry_run only, secrets stripped
}
// <Create>Request  += SubmitOptions submit
// <Create>Response += SubmitResult  submission   (on replay only this field is set)
```

`validate_only` stays as an alias for `dry_run`, because the CLI sends it
(`harness/determined/cli/experiment.py:188`).

### Submit handler order

One shared `submission` package implements this order for all four handlers.

1. **Digest.** Canonicalize and digest the client request. Nothing with side effects runs.
2. **Replay.** If a key is present, look up `(owner_id, key)`. With the same digest, check
   read authz on the stored job and return it with `replayed=true`. With a different
   digest, return `ALREADY_EXISTS` naming the existing `job_id`.
3. **Parse.** Parse, merge, authorize, and apply config policy. Session minting and shell
   key generation move after step 4. Today they run too early:
   - `master/internal/core_experiment.go:401`, before the `ValidateOnly` return at
     `api_experiment.go:1655`;
   - `api_command.go:166-170`;
   - `api_generic_tasks.go:153-158`;
   - `api_shell.go:263-269`.
4. **Dry run.** If `dry_run` is set, evaluate and return. Nothing has been written.
5. **Commit.** One transaction writes:
   - the job row, with key, digest, request, and admission;
   - for tasks, the task row, the context directory, `allocation_workspace_info`, the first
     allocation row in `PENDING`, and `command_state` (including `generic_task_spec`).
     Today `command_state` is written after `StartAllocation` (`command/command.go:181-186`,
     `api_generic_tasks.go:378`). A crash in between leaves a task that is never restored.
     Because the row now exists, `requestResources` loads it instead of inserting it
     (`task/allocation.go:523-529`).
   - for experiments with `activate=true`, the experiment row, committed as `ACTIVE`.

   On a unique-index violation, roll back, delete the minted session and the
   `GroupPriorityChangeRegistry` entry (`command/command.go:117-119`), and return to
   step 2. The index is the guard; `cs.mu` is not.
6. **Start.** Call `StartAllocation` or `e.Start`. If this fails in-process, close the
   `PENDING` allocation with `INFRASTRUCTURE_FAILED` and end the task, or mark the
   experiment `ERROR`. A replay then returns a terminal record.
7. **Wait (IMMEDIATE only).** Once the launch call returns, outside `cs.mu`
   (`command/command_service.go:95`), poll the allocation through the allocation
   service's read lock until it leaves `PENDING`. The wait is capped at 5 s, after which
   the outcome is `PENDING`.

**Restore rule.** An allocation with `start_time IS NULL` was never placed.
`RestoreAllCommands` and `restoreGenericTasks` (`command_service.go:55-72`,
`core.go:880-955`) handle it by admission:

- QUEUE: re-request it fresh under the same allocation ID.
- IMMEDIATE: end it with `PLACEMENT_UNSATISFIED`.

This mirrors `IsReattachableOnlyAfterStarted` for trials (`trial.go:807-809`). It also
fixes today's `RestoreError` "0 container snapshots" for commands that were queued at
restart (`rm/agentrm/resource_pool.go:189-190`).

### Durable reads and cancel

```proto
rpc GetSubmission(GetSubmissionRequest) returns (GetSubmissionResponse);          // GET  /api/v1/submissions/{job_id}
rpc ListSubmissions(ListSubmissionsRequest) returns (ListSubmissionsResponse);    // GET  /api/v1/submissions
rpc CancelSubmission(CancelSubmissionRequest) returns (CancelSubmissionResponse); // POST /api/v1/submissions/{job_id}/cancel

message ListSubmissionsRequest { optional int32 owner_id = 1; /* default: caller */
  Kind kind = 2; State state = 3; google.protobuf.Timestamp submitted_after = 4;
  int32 limit = 5; string page_token = 6; }
message Submission {
  string job_id = 1; Kind kind = 2;          // COMMAND | SHELL | GENERIC | EXPERIMENT
  string entity_id = 3;                      // task_id or experiment id
  int32 owner_id = 4; string owner = 5; int32 workspace_id = 6; optional int32 project_id = 7;
  string name = 8;
  optional string idempotency_key = 9;       // owner and admins only
  optional string request_digest = 10;       // owner and admins only
  google.protobuf.Struct request = 11;       // redacted; absent before the upgrade
  Admission admission = 12;
  google.protobuf.Timestamp submitted_at = 13; optional google.protobuf.Timestamp ended_at = 14;
  State state = 15;        // QUEUED RUNNING PAUSED COMPLETED FAILED CANCELED DELETED
  ExitClass exit_class = 16; string exit_reason = 17;   // from the allocation that ended the job
  repeated SubmissionTask tasks = 18;        // task_id, optional trial_id, repeated taskv1.Allocation
}
// taskv1.Allocation += resource_pool, exit_class, google.protobuf.Struct exit_detail,
//                      repeated Placement placements  /* node, accelerator_uuids */
```

- **Source.** A named query under `master/static/srv/` reads the database only. It joins
  `jobs` to tasks or experiments, allocations, and `allocation_accelerators`, and filters
  `job_type IN (COMMAND, SHELL, GENERIC, EXPERIMENT)`.
- **Workspace** is derived, not stored: through the project for experiments (which can
  move), and through `command_state` for tasks.
- **Task state** comes from `end_time`, `task_state`, and the last allocation. A task with
  no live allocation whose last allocation has ended counts as ended, with its state taken
  from that allocation's class, so it never shows QUEUED forever.
- **Experiment state** comes from `experiments.state`. A deleted experiment is `DELETED`,
  because `DeleteExperiments` keeps the job row (`db/postgres_experiments.go:783-806`).
- **Authorization.** Every row passes the existing per-kind read authz. A `DELETED`
  experiment has no experiment row left to check (`db/postgres_experiments.go:800-803`),
  so only its owner and
  admins see it.
- **`get_task.sql`** also selects `slots`, `exit_reason`, and `status_code`. These are
  existing `taskv1.Allocation` fields (`proto/src/determined/task/v1/task.proto:93-98`).
- **`CancelSubmission`** is idempotent: an ended job is returned unchanged, and a live job
  uses the existing kill path and its authz. It needs no registry fallback, because a live
  job is always in the registry.

### Scheduling evaluation

`rp.Evaluate(reqs)` holds `rp.mu` and runs the pool's own `scheduler.Schedule` on a
scratch pool (`rm/agentrm/`):

- **Task list.** `TaskList.Clone()` copies requests by value, because `priority.go:134`
  writes through the shared pointer, and shares allocation pointers.
- **Groups.** It copies the groups and adds a synthetic group with a priority, which avoids
  the nil-priority panic at `priority.go:293-296`. It never calls `getOrCreateGroup`,
  which registers a callback (`resource_pool.go:340-345`).
- **Static feasibility.** `findFits` runs on deep copies of the agents with their
  containers removed. Agents inside the reconnect window count as enabled
  (`agent.go:80-83`).
- **N.** N is 1 for tasks. For searches it is
  `min(max_concurrent_trials or max_trials, max_trials)`.

```proto
message SchedulingEvaluation {
  string resource_pool = 1;
  Verdict verdict = 2;          // PLACEABLE_NOW | WOULD_PREEMPT | WOULD_QUEUE | INFEASIBLE
  int32 requested = 3; int32 placeable_now = 4;
  repeated EvaluatedPlacement placements = 5;   // agent_id, slots, device brands
  repeated string blocked_nodes = 6; repeated string reasons = 7;
  google.protobuf.Timestamp evaluated_at = 8;
}
```

- **Rate limit.** `dry_run` evaluation is rate-limited per user (master config, default
  one per second), because it delays one tick (`resource_pool.go:348-385`).
- **Validation shares the fit.** `ValidateResources` and `CapacityCheck`
  (`resource_pool.go:568-661`) are rebuilt on the static fit. That brings in the
  multiple-of-per-agent-slots rule and the idle-agent rule (`fitting.go:123,176-186`).
  Provider-backed pools keep today's instance arithmetic (`:576-577,632-647`), because a
  static fit over the agents that happen to be up would report no capacity.
- **Each call site keeps its outcome:**
  - Creates keep theirs: single-node requests get an error
    (`agent_resource_manager.go:589-600`), others a warning (`:602-607`) that
    `launch_error` makes fatal (`spec_util.go:51-53`).
  - Experiment restore logs and never fails, where today `restore.go:86-93` treats an
    error as fatal.
  - `checkResourcePoolRemainingCapacity` (`trial.go:670-684`) adopts the correct rule.
- **Other resource managers.** The Kubernetes and dispatcher RMs return `Unimplemented`.

### Immediate admission

- **Flag.** `AllocateRequest.Immediate` (`sproto/task.go:25-54`) marks the request.
- **Enqueueing.** `rp.Allocate` still only enqueues and sets `reschedule`
  (`resource_pool.go:118-124`). It never runs a pass, because `StartAllocation` holds the
  allocation-service write lock across it (`task/allocation_service.go:55-67`).
- **Decision.** In `schedulerTick`, just before the real `Schedule` (`resource_pool.go:368`),
  one scratch pass covers every undecided immediate request.
  - Requests missing from its `toAllocate` are removed and published as
    `ResourcesFailedError{PlacementUnsatisfied}`, with detail `static` or `busy`.
  - Safety net: a request decided in this tick that the real pass does not place is also
    rejected.
- **Rejection path.** Rejections take the existing asynchronous exit path
  (`allocation.go:265-266,894-921`), which sets `end_time` on the `PENDING` row
  (`:913-919`).
- **Scope.** IMMEDIATE applies to every allocation that the submit itself creates,
  including system retries. Later user-driven allocations, such as a generic resume,
  queue.
- **Rejected at submit.** Experiments get `INVALID_ARGUMENT`. Provider-backed pools get
  `FAILED_PRECONDITION`.

### Exit classes

```proto
enum ExitClass {
  EXIT_CLASS_UNSPECIFIED = 0;                // allocations that ended before the upgrade
  EXIT_CLASS_NONE = 1;                       // completed, preempted, or paused
  EXIT_CLASS_CANCELED = 2;
  EXIT_CLASS_PLACEMENT_UNSATISFIED = 3;
  EXIT_CLASS_NODE_PREFLIGHT_FAILED = 4;
  EXIT_CLASS_WORKLOAD_INITIALIZATION_FAILED = 5;
  EXIT_CLASS_WORKLOAD_FAILED = 6;
  EXIT_CLASS_INFRASTRUCTURE_FAILED = 7;
}
```

`allocations` gains `exit_class text` and `exit_detail jsonb`, written by `SetExitStatus`
(`task/allocation.go:1074-1095`). `closeOpenAllocations` (`core.go:957-963`), which closes
allocations without that call, writes `INFRASTRUCTURE_FAILED`. The classifier is total:

| Exit | Class |
|---|---|
| User-requested kill (the kill endpoints pass a flag through `Signal`, `allocation.go:317-327`, which preemption timeouts and `crash` also reach) | `CANCELED` |
| No error | `NONE` |
| `PlacementUnsatisfied` | `PLACEMENT_UNSATISFIED` |
| `PreflightFailed` | `NODE_PREFLIGHT_FAILED` |
| `ResourcesFailed` or `TaskError` | `WORKLOAD_INITIALIZATION_FAILED` if the failing resource never posted workload start, otherwise `WORKLOAD_FAILED` |
| All others: agent errors, `RestoreError`, `ResourcesMissing`, aborts, unknown | `INFRASTRUCTURE_FAILED` |

**One change for every layer.** `calculateExitStatus` panics on unlisted types
(`allocation.go:1219-1220`), so all of the following land together:

- `aproto.PreflightFailed` (`master/pkg/aproto/exit.go:95-120`);
- the `sproto` constants with their `Proto`/`From` mappings (`sproto/resources.go:250-330`);
- the `taskv1` enum;
- the switch cases, including the missing `ResourcesMissing` case and a default that
  classifies instead of panicking.

The same change maps unknown agent types to `UnknownError` (`resources.go:327-328`). It
also changes `a.crash(msg)` to `a.crash(*msg)` at `allocation.go:921`: today the pointer
misses the value-typed switch and lands in "handler crashed".

### Initialization boundary

- **Column.** `allocation_resources` gains `workload_started_at`.
- **RPC.** A new internal RPC, `PostAllocationWorkloadStarted{allocation_id, resources_id}`,
  is authorized like `AllocationReady` (`api.proto:921`).
- **Who posts it.** The first `prep_container` call posts it as its last step, after the
  context download and before the startup hooks run. The call sites are
  `master/static/srv/command-entrypoint.sh:11`, `shell-entrypoint.sh:7`,
  `generic-task-entrypoint.sh:13` and `entrypoint.sh:9`. The trial's later
  `--rendezvous` call does not post. Hooks are user code.
- **When the class is set.** Today `finalize` calls `SetExitStatus` after
  `purgeRestorableResources` (`allocation.go:584-588`); the class is computed before the
  purge. These rows live exactly as long as the allocation, because startup purges only
  the rows of closed allocations (`taskmodel/resources.go:53-63`).

### System retries

- **New allocation per retry.** A retry is always a new allocation `<task>.<n+1>`. An
  allocation is never re-placed.
- **Budget.** `resources.max_system_retries` (master default 3) bounds
  `NODE_PREFLIGHT_FAILED` and `WORKLOAD_INITIALIZATION_FAILED`. The budget is derived by
  counting the task's allocations with those classes, so it survives restarts without a
  counter.
- **Blocked nodes.** Only `NODE_PREFLIGHT_FAILED` writes `(task, node, "preflight:<check>")`
  into the existing blocked-node table, which is keyed by task ID with no FK
  (`logpattern/logpattern.go:133-159`).
- **Stopping.** Retries stop when the budget is spent or when the blocked nodes leave no
  static fit.
- **Trials.** A new case before the transient check (`trial.go:621`) re-allocates without
  incrementing `restarts`.
- **Commands and shells.** These gain re-allocation. Before `OnExit` completes the task
  and deletes its session token (`command.go:239-263`), the command starts `<task>.<n+1>`
  with its blocked nodes, updates `command_state.allocation_id`, and writes the workspace
  record.
- **Unchanged.** `INFRASTRUCTURE_FAILED` keeps today's transient handling
  (`sproto/resources.go:353-371`). Generic tasks end with their class, as they do today
  (`spec_util.go:151-160`).

### Placement constraints

- **Config.** One `resources.accelerators` block is added to expconf
  (`master/pkg/schemas/expconf/experiment_config.go:203-217` plus the JSON schema) and to
  `model.ResourcesConfig` (`master/pkg/model/experiment_config.go:85-97`). It cannot be
  called `devices`: `resources.devices` is already the host-device mount list (`:216`,
  `:96`).

  ```yaml
  accelerators:
    models: ["NVIDIA A100*"]      # static: scheduler
    min_memory_mib: 40000         # static: scheduler
    min_free_memory_mib: 30000    # dynamic: agent preflight
    max_utilization_percent: 10   # dynamic: agent preflight
  ```

- **Scheduler.** `FittingRequirements` (`sproto/scheduler.go:4-7`) gains `DeviceModels`
  and `MinDeviceMemoryMiB`. A `deviceSatisfied` hard constraint joins both inline lists
  (`fitting.go:123`, `:227`).
  - In v1, all of an agent's enabled devices must match, because `allocateFreeDevices` has
    no device filter (`agent_state.go:158-186`).
  - The new fields are plumbed to `trial.go:412,459`, `command.go:162`,
    `api_generic_tasks.go:358`, and `generic_task_resume.go:371`.
- **Detection.** The nvidia-smi query adds `memory.total`, so the parser's field count goes
  from 3 to 4 (`agent/internal/detect/nvidia.go:25-27,92-94`). MIG devices report 0.
  Memory travels beside the device, as `AgentStarted.DeviceMemoryMiB map[device.ID]int`
  kept on each `slot`, and is replaced on every `AgentStarted`. `device.Device`
  (`master/pkg/device/device.go:43-48`) is unchanged, because it is the key of
  `agentState.Devices` (`agent_state.go:49`) and is persisted in container snapshots that
  restore looks up by value (`:526-530`). The reconnect compare
  (`agent_state.go:261-292`) is therefore unchanged too.

### Agent preflight

- **Spec.** `cproto.Spec` gains `Preflight{MinFreeMemoryMiB, MaxUtilizationPercent, Timeout}`
  (`master/pkg/cproto/spec.go:14-18`). `ToDockerSpec` fills it in
  (`master/pkg/tasks/task.go:261`).
- **Placement in the launch path.** The preflight runs after `PullImage` and before
  `CreateContainer` and `c.spec = nil` (`agent/internal/container/container.go:198-222`).
  It cannot run in `manager.StartContainer`, whose errors are only logged
  (`agent/internal/agent.go:183-185`).
- **Checks.** It samples `nvidia-smi --query-gpu=uuid,memory.used,memory.total,utilization.gpu`
  on the allocated UUIDs once a second. It passes when the thresholds hold and fails after
  `Timeout` (master default 30 s). This grace period covers the task's previous container
  releasing memory.
- **What it does not do.** It never runs `stat` on a host path or uses
  `--query-compute-apps`, because the documented agent container sees only `docker.sock`
  and its config (`docs/setup-cluster/on-prem/options/docker.rst:117-171`).
- **Missing mount sources.** Bind mounts are `mount.TypeBind` (`master/pkg/tasks/mounts.go:19-27`),
  so Docker fails `CreateContainer` when a host source is missing. The agent maps that
  error, matched with Docker errdefs, to `PreflightFailed{check: mount_source, subject: <host path>}`.
  Today it ends up as a `TaskError` (`container.go:336-343`).
- **Failure detail.** A failure carries `{check, device, observed, required}`, both in the
  exit detail and in a container log line.

### Carried fixes

- **`IdentifyTask`.** It resolves GENERIC tasks' workspace from `generic_task_spec`
  through a `COALESCE` (`command/postgres_command.go:46-60`). Today it resolves them to
  workspace 0.
- **Generated bindings.** Each fork PR commits the regenerated `bindings.py` and
  `api-ts-sdk`.

## Semantics

### Phases and failure classes

| Phase | Where | Checks | Failure |
|---|---|---|---|
| Submit | master create handler | schema, authz, config policy, key and digest | error; nothing is created, and the key stays free |
| Place | RM tick | IMMEDIATE only: placed this pass without preemption | `PLACEMENT_UNSATISFIED` |
| Node accept | agent: pull, preflight, `CreateContainer` | GPU thresholds on the allocated UUIDs, bind sources | `NODE_PREFLIGHT_FAILED`; other pull or create errors are `WORKLOAD_INITIALIZATION_FAILED` |
| Init | container: `task-setup.sh`, then `prep_container` | Python, wheel, context download, proxy | `WORKLOAD_INITIALIZATION_FAILED` |
| Workload | startup hooks and user command | exit code | `WORKLOAD_FAILED` |
| Any | agent or connection | agent loss, restore | `INFRASTRUCTURE_FAILED` |

### Restart accounting

| Class | Counts against `max_restarts` | Automatic retry | Blocks the node |
|---|---|---|---|
| `PLACEMENT_UNSATISFIED` | no | none | no |
| `NODE_PREFLIGHT_FAILED` | no | new allocation within `max_system_retries` | yes |
| `WORKLOAD_INITIALIZATION_FAILED` | no | new allocation within `max_system_retries` | no |
| `WORKLOAD_FAILED` | trials only | trials, as today | through log policies, as today |
| `INFRASTRUCTURE_FAILED` | no | as today (transient for trials) | no |
| `CANCELED`, `NONE` | no | none | no |

System retries apply to trials, commands, and shells.

### Idempotency and digest scope

- **Scope.** A key is scoped to `(jobs.owner_id, key)`, so all of a user's clients share
  it. Keys never expire, just as job rows are never deleted.
- **Binding.** A dry run never binds a key. An error before commit binds nothing. Any
  committed submit consumes the key, including one that ends `PLACEMENT_UNSATISFIED`, so
  a new attempt needs a new key.
- **Digest.** The digest is SHA-256 over JCS-canonical JSON of the client request, taken
  before master defaults. The merged spec cannot be the identity: petnames, sshd ports,
  SSH keys, session tokens, and pool defaults vary between identical requests.

  | Included | Excluded |
  |---|---|
  | kind, workspace, project, template name | `idempotency_key`, `dry_run` |
  | config, parsed from YAML or Struct, then canonicalized | file `mtime`, `uid`, `gid` |
  | file manifest `{path, type, mode, sha256}` | master defaults and merged values |
  | parent, fork, inherit, `no_pause`, `activate`, `admission` | |

- **Admission is part of the digest,** so retrying the same key with a different
  `allow_queue` returns 409.
- **Stored request.** Environment values are replaced with `"<redacted>"`, and files are
  stored as a manifest. The stored request has the same visibility as config.
- **Key and digest visibility.** Only the owner and admins see them, so a plain digest
  exposes nothing that could be brute-forced by other readers.

### Ownership

The owner is the authenticated user, recorded in `jobs.owner_id`. The MCP's `--owner`
namespace is removed. Control uses the fork's existing rules: owner or admin under basic
authz (`master/internal/api_ntsc_control.go:15-26`), and workspace permissions under
RBAC. A replay re-checks read authz on the stored job, so revoked access is honoured.

### `allow_queue`

- **Mapping.** `allow_queue=false` means IMMEDIATE, and `true` means QUEUE. An unspecified
  admission means QUEUE, so `det` and the WebUI are unchanged.
- **IMMEDIATE.** The request is placed by the pool's scheduler in the deciding tick, with
  no preemption. Otherwise it fails as `PLACEMENT_UNSATISFIED` (`static` or `busy`) and is
  never queued.
- **Static infeasibility** stays a warning for QUEUE.
- **Searches.** IMMEDIATE is rejected, because trials are created asynchronously and there
  is no gang scheduling. `dry_run` reports `placeable_now` out of N as information, and
  the MCP requires `allow_queue=true` for experiments.

### Placement constraints

- **Static facts are hard constraints:** GPU model, total memory, and `is_single_node`,
  which is unchanged. Commands stay single-agent (`command.go:161-176`).
- **Dynamic facts are preflight checks:** free memory and utilization. Neither is ever a
  placement fact.
- **No reservations.** A preflight pass reserves nothing, and an evaluation is a snapshot
  taken at `evaluated_at`.

### Artifact references

- **Code** travels as the existing context: `files` for tasks and `model_definition` for
  experiments.
  - It is committed in the job transaction and capped by `MAX_CONTEXT_SIZE`, about 95 MiB
    (`harness/determined/common/constants.py:5-18`).
  - The manifest in the digest identifies it exactly.
  - The MCP adds `.code-provenance.json` with `{commit, dirty, excluded}`.
- **Data and outputs** live on shared storage under pool bind mounts. `storage_sync` and
  `storage_fetch` move them.
- **Checkpoints** use `checkpoint_storage`, as today.
- **Not in this design:** a snapshot service, an object store, or `snapshot_id`.

### What remains unverified

Identity, owner, state, placement, and exit class are authoritative, and
`cross_profile_unverifiable` is gone. Four things remain unverified, and each is labelled
wherever it appears:

- an evaluation, which is a snapshot at `evaluated_at`;
- a preflight pass, which reserves nothing;
- pre-upgrade jobs, which have no request or digest and cannot be replayed;
- `unmeasured` usage, when task-resources data is unavailable.

### Choices between alternatives

| Question | Decision | Rationale |
|---|---|---|
| Where the ledger lives | nullable columns on `jobs` | `jobs` is rarely updated (only `q_position`, `api_experiment.go:1524-1528`). No new table, no backfill, no FK that blocks deletes. |
| Create API | envelope on the four RPCs, no `Submit` oneof | One create path for every client, and no oneof body shape that the generators have never handled. |
| Handle | `job_id` only | One handle for every read and verb. |
| When IMMEDIATE is decided | next tick, in a scratch pass | A pass inside `rp.Allocate` would hold the allocation-service write lock. |
| IMMEDIATE for searches | rejected | No atomic guarantee exists without gang scheduling. |
| IMMEDIATE on retries | inherited by allocations the submit creates | Only commands, shells, and generic tasks use it, and none has workload restarts. |
| Capacity vs placement | one class with a `static` or `busy` detail | One post-commit path. |
| Retry mechanism | new allocation, derived budget | A re-placed allocation looks started to restore (`start_time` is set at Pulling, `allocation.go:749-753`). |
| Agent loss | unchanged | A budget of 3 would end long trials during maintenance. |
| Command and shell retries | added | The batch kind stays command, and blocked-node rows are per task. |
| Generic task retries | deferred | The global mutation lock (`generic_task_resume.go:25-28`) and resume ordinals need rework first. |
| Where the init boundary is stored | per resource, classified before purge | One node cannot mask another's failure. |
| Mount-source check | Docker bind error | A containerized agent cannot stat host paths. |
| Directories | the workload's own `mkdir -p` | One less config field and no path policy. |
| Code | existing context | No store, no GC, no dangling references. |
| Digest | plain SHA-256, owner-only | No new master secret. |
| DB fallback for `GetCommand` | deferred | `GetSubmission` is the durable read for every client. |
| Consultation, `compute_cli.py` | moved out, deleted | Neither is about compute, and `det` and the WebUI are the other clients. |

## The MCP after the refactor

### Tools

There are 9 tools, down from 16 on `main` (14 base tools plus 2 consultation tools).

| Tool | Behaviour |
|---|---|
| `compute_plan(spec: TaskSpec, evaluate=True)` | Compiles the spec, applies policy, and mints a UUIDv4 `request_id`. With `evaluate`, it calls create with `dry_run` and returns the effective config summary, digest, evaluation, and warnings, including paths outside the effective bind-mount targets. Otherwise it renders offline. |
| `compute_launch(spec, request_id, allow_queue=False)` | Checks policy, then makes one create call with `SubmitOptions`. Requires a UUID `request_id`. Returns `job_id`, `replayed`, `submitted_at`, and `outcome`. Experiments need `allow_queue=True`. |
| `compute_status(job_id)` | `GetSubmission`, plus an explanation of the exit class. |
| `compute_list(kind=None, state=None, limit=50, cursor=None)` | `ListSubmissions` for the caller, covering every client. |
| `compute_logs(job_id, trial_id=None, tail=200)` | Task logs. |
| `compute_usage(job_id, trial_id=None, allocation_id=None, window_seconds=3600, metrics=None, include_samples=False)` | Task resources, plus interpretation. |
| `compute_cancel(job_id)` | `CancelSubmission`. |
| `storage_sync(local_dir, shared_dir, dry_run=True, overwrite=False)` | rsync; without `overwrite`, it adds `--ignore-existing`. |
| `storage_fetch(shared_dir, local_dir, dry_run=True, overwrite=False)` | rsync in the other direction. |

### `TaskSpec`

`TaskSpec` is a pydantic model, published as the input schema. It has these fields:

- `kind`: `command`, `shell`, or `experiment`.
- `name` and `command`.
- `workdir` and `output_dir`: container paths. The rendered command runs `mkdir -p` on
  `output_dir`, then `cd` into `workdir`.
- `image`, `pool`, and `slots`. Pool and slots are always sent explicitly, from the policy
  defaults.
- `accelerators`.
- `env`, `workspace`, and `project`.
- `code`: `repo_dir`, `revision` (default `HEAD`), `include`, and `exclude`, packed into
  the context. Tracked files come from git objects at `revision`; `include` paths come
  from the working tree, and `dirty` records whether the working tree differs.
- `experiment`: typed by the fork's expconf JSON schemas, vendored at the pinned version.
  A search must set `max_concurrent_trials`, so that `max_slots` can bound slots times
  concurrency.

### Modules

| Module | Content | Source |
|---|---|---|
| `mcp_server.py` | tool table | rewritten |
| `spec.py` | `TaskSpec` and compiler | spec bucket of `compute/service.py`, including `_render_entrypoint` (`:585-599`) |
| `policy.py` | defaults, pool allow-list, max slots, `overwrite`, container-to-host map | the kept part of `compute/profile.py` |
| `client.py` | transport, auth, redaction, version gate, create routes, submissions, logs, trials, task-resources, pool and GPU-model lookups for usage | `core/api_client.py`, minus deletions |
| `context.py` | git enumeration, include and secret rules, manifest, provenance | PR #1 `storage/snapshot.py:1-513,736-995` |
| `usage.py` | usage interpretation | `compute/service.py:67-103,176-230,772-1190` |
| `storage/` | sync, fetch, config, auth, askpass | current package, minus `check` |
| `utils/secrets.py` | unchanged | – |

### Local state and version gate

- **No local state.** There is no SQLite, and `--owner` and `--db` are removed.
  Configuration holds the API URL and credentials, policy, storage access, and the
  container-to-host map. There are no local aliases or drafts: `compute_plan` returns the
  plan, the caller sends it back to `compute_launch`, and `compute_list` recovers a lost
  `job_id`. Nothing depends on a local cache, so one can be added later without a contract
  change.
- **Version gate.** At startup the MCP calls `GET /api/v1/master` once. It refuses to
  serve if the fork's release version is below 0.41.0; pre-release builds of 0.41.0 pass.

### Deletion list

| Target | What goes |
|---|---|
| Whole files | `compute/store.py`, `compute/admission.py`, `compute_cli.py` with its `determined-compute` script in `pyproject.toml`. `agent_worker.py` and the `compute_consult` and `workflow_status` tools move to their own entry point. |
| `compute/models.py` | `TaskRecord` (`:32-70`) |
| `compute/service.py` (dissolved) | upload-field rejection `:53-64,109-120`; claim, mark, and uncertainty `:166-175,601-661,725-735`; path validation `:489-515,569-583`; capacity hook `:662-668`; idempotency and legacy hash `:669-724,1435-1452`; capability check `:831-835`; remote identity `:1212-1294`; discover, adopt, and reconcile `:1295-1434`; binding `:1453-1526`; markers `:1527-1573` |
| `core/api_client.py` | kind, user, and cluster helpers `:220-264`; `list_remote_tasks` `:265-321`; markers and upload fields `:381-465`; per-kind dispatch `:466-521`, which becomes four create routes plus `CancelSubmission`; unsupported fallback `:567-582`. `list_resource_pools` and `list_gpu_devices` (`:647-690`) stay, because usage calls them (`compute/service.py:922,942`) and `allocation_accelerators` stores no GPU model. |
| `storage/service.py` | `check` and `_ssh_check` (`:70-108,429-466`) |
| `compute/profile.py` | path validators (`:161-199`), `cluster_identity`, and `fingerprint` (`:200-215`); the mount list stays as the container-to-host map |
| `mcp_server.py` | the reconcile, list, discover, adopt, resources, and `storage_check` tools (`:142-192`); the `--owner` and `--db` wiring |
| Tests | `test_adoption_store`, `test_remote_adoption`, `test_compute_legacy_retry`, `test_owner_namespace`, `test_admission`, `test_compute_cli`; the ledger and binding parts of `test_compute_service` and `test_api_client` |
| PR #1 | Closed unmerged. Dropped: `gpu_admission.py` with its NVML branch, `storage/paths.py`, cross-profile code, launch-path validation, `create_directories`, the store half of `snapshot.py` (`:514-735,996-1097`), and their tests. Salvaged: enumeration, secret rules, about 565 test lines, and `resolve_api_url`. |

The expected result is about 3,000 lines of source, compared with about 6,300 on `main`
and 8,600 with PR #1.

## Delivery plan

### Fork (tag 0.41.0)

| PR | Content | Depends on |
|---|---|---|
| F1 Exit classes and fixes | `ExitClass` across all layers; new failure types; a total classifier; `closeOpenAllocations` class; `UnknownError` mapping; `crash(*msg)`; `allocations.exit_class` and `exit_detail`; the `IdentifyTask` fix. Until F4 adds the init boundary, `ResourcesFailed` and `TaskError` classify as `WORKLOAD_FAILED` | – |
| F2 Ledger | `jobs` migration; `SubmitOptions` and `SubmitResult`; handler order with side-effect-free `dry_run` (no evaluation yet) and the `validate_only` alias; `ADMISSION_IMMEDIATE` returns `UNIMPLEMENTED` until F3; the single commit transaction; `ACTIVE` experiments; the never-placed restore rule; `Get`/`List`/`CancelSubmission`; `get_task.sql` fields | F1 |
| F3 Evaluation and IMMEDIATE | `TaskList.Clone`; `rp.Evaluate`; the static fit behind the old checks, keeping each call site's outcome; `dry_run` evaluation and its rate limit; the tick decision and handler wait; IMMEDIATE restore; `Unimplemented` on other RMs; the provider-pool rejection | F2 |
| F4 Init boundary and retries | `workload_started_at`, the internal RPC, and the `prep_container` post; `max_system_retries` with a derived budget; the trial case; command and shell re-allocation; blocked-node rows | F1, F3 |
| F5 Devices and preflight | memory detection beside `device.Device`; `resources.accelerators`; `deviceSatisfied`; `cproto.Preflight`; the agent hook; mount-source mapping | F3, F4 |

Master and agents are upgraded together. Running containers survive, because
`device.Device` and the reconnect compare are unchanged.

### MCP (1.0 requires fork 0.41.0)

| PR | Content | Depends on |
|---|---|---|
| M1 Narrow the server | move consultation to its own entry point; delete `compute_cli.py`; close PR #1 | – |
| M2 Cut to the ledger | `client.py`, the version gate, and the `job_id` handle; launch, status, list, logs, usage, and cancel over submissions; delete the store, markers, reconcile, discover, adopt, binding, and owner namespace | F2, F3 |
| M3 Typed spec | `TaskSpec`, `spec.py`, `policy.py`, `context.py`, and `accelerators`; plan through `dry_run`; delete `admission.py`, `compute_resources`, `storage_check`, and path validation | F5 |

- **Merge order.** An MCP PR merges only after its fork dependencies are on fork `main`.
  Integration tests run against 0.41.0 pre-release builds.
- **Release.** MCP `main` is not released until the fork tags 0.41.0.
- **Docs.** Each PR updates the English and Chinese docs it touches. M3 rewrites
  [Compute service reference](compute-service.md), [Agent workflow](agent-workflow.md),
  [Troubleshooting](troubleshooting.md), and the `AGENTS` routes.

## Out of scope

- **Generic tasks as the only kind.** This includes collapsing kinds into GENERIC,
  generic re-allocation, and reworking the generic lock.
- **Searches.** Gang scheduling and strict admission for searches.
- **Snapshots and artifacts.** There is no snapshot or artifact service. If one is ever
  needed, it must scope keys to `(owner_id, snapshot_id)`, authorize every reference, and
  hold a lease from upload to first reference.
- **Other launch features.** Quota mechanisms beyond `max_slots` and config policies.
  Condition-triggered launches, if ever needed, are a separate service that submits
  through the same create envelope with its own idempotency keys, never part of the
  scheduler loop.
- **Legacy reads.** A database fallback for `det cmd` and the WebUI task list.
- **Other resource managers.** Kubernetes and dispatcher RMs, and ROCm and MIG preflight.
- **Mixed GPUs.** Device filtering on mixed-GPU agents.
- **Checkpoint GC.** Read authz for checkpoint-GC tasks (`master/internal/api_tasks.go:65-69`).
- **Consultation.** Redesigning consultation.

## Risks and open questions

1. **Scheduler latency.** `dry_run` and the immediate scratch pass each add at most one
   pass under `rp.mu`. Add a latency metric alongside the rate limit.
2. **Pass disagreement.** If the scratch and real passes disagree, the safety net rejects
   the request; it never queues it.
3. **Strict IMMEDIATE.** Because IMMEDIATE never preempts or overtakes, it refuses often on
   a busy cluster. The alternative is `allow_queue=true`.
4. **Experiments committed `ACTIVE`.** The create path must skip `ActivateExperiment`
   (`api_experiment.go:1682-1687`) and start the experiment the way restore does; restore
   already handles a nil snapshot (`restore.go:118-124`). Verify in F2.
5. **Docker bind errors.** Mount-source classification depends on Docker's bind error.
   Test it on the deployed Docker version.
6. **Reconnect window.** Treating agents inside the reconnect window as enabled needs their
   stashed state (`agent.go:80-83`). Verify in F3.
7. **Generic cancel.** `CancelSubmission` on a generic task can hit the global mutation lock
   (`api_generic_tasks.go:580-583`). It returns a retryable `UNAVAILABLE`.
8. **Secrets in commands.** Secrets typed into a command line are stored as submitted. The
   docs must say so.
9. **Retry budget.** Is 3 the right default for `max_system_retries`?
10. **Legacy allocations.** Allocations that ended before the upgrade show
    `EXIT_CLASS_UNSPECIFIED`, the only unclassified state.
