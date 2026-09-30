# Architecture

[English](architecture.md) | [简体中文](architecture.zh.md)

This document describes the target architecture of the compute MCP and the changes it
needs in the Determined fork. It is written for maintainers of both repositories. For the
current MCP interface, see [Compute service reference](compute-service.md).

Fork paths are relative to the fork root at `8e26a69` (release 0.40.1). MCP paths are
relative to this repository at `2404d0d`. "PR #1" is the unmerged
`feat/research-workflow-support` branch. Citations come from reading the source, not from
running it. They point to those base commits and are not maintained as the code changes.
They were re-checked at fork `ec9a865`, which differs from `8e26a69` only under
`harness/determined/deploy`.

## What changes for users

- **Code comes from one of three explicit sources.** `code.source` selects it.
  - `git` names a repository on shared storage and a revision. The plan pins the commit,
    and the container clones it into local scratch and checks it out. Nothing is uploaded
    and there is no size cap. The commit must be on a branch or tag, and the image needs
    `git` (and `git-lfs` for LFS files).
  - `context` ships the tracked files at a revision plus explicit includes as Determined's
    task context, up to about 95 MiB. It suits small repositories and uncommitted changes.
  - `path` runs in place from a directory on shared storage. That code is mutable, so it
    is an explicit opt-in, for shells and debugging.

  `workdir` is now relative to the code root.
- **Storage roots belong to the administrator.** A job never names a bind source. Data and
  outputs live in run directories that the job creates inside mounted roots. A checkpoint
  sets only a relative `checkpoint_storage.storage_path`; `host_path` is an administrator
  setting.
- **One handle.** `job_id` replaces the local `task_id`, and `--owner` and `--db` go away.
  Every client of the same account sees and controls the same jobs.
- **A launch is bound to its plan.** `compute_launch` sends the digest that `compute_plan`
  returned. If the code or config changed since the plan, it returns `plan_changed` and
  creates nothing. Every plan contacts the master.
- **Admission is explicit.** `admission=queue` is the default and queues like `det`; on
  `main` the MCP defaults to not queuing (`compute/service.py:279`). Only `queue`
  exists: `admission=immediate` is refused as `admission_unsupported`, never silently
  downgraded. `compute_plan` validates and renders the exact request through the master's
  `dry_run`, but does not evaluate placement; the scheduler decides after submit.
- **Fewer tools.** Reconcile, discover, adopt, and the `determined-compute` CLI are
  removed; `compute_list` covers their uses. The read-only `compute_resources` and
  `storage_check` stay as pass-throughs. The consultation worker is removed: the official
  W&B MCP covers experiment analysis.

## Principles

1. **Fail reliably.** This sets the scope of the first release (see
   [Deferred designs](#deferred-designs)).
   - A job may fail, but a failure is never reported as success.
   - A user may resubmit, but the system never silently re-executes work whose outcome is
     uncertain.
   - A capability may be missing, but its absence never harms other jobs, resource
     ownership, or cancellation.
   - New complexity must pass one gate: can the case be handled by an explicit rejection,
     a clear failure, a manual fix, or a query by the same identity, without new state,
     background work, or automatic retries? Only a risk of wrong execution, duplicated side
     effects, unclear resource ownership, lost cancellation, or data leakage justifies a
     stronger protocol.
2. **Each capability lives where its source of truth lives.**
   - The master owns identity, idempotency, plan binding, admission, placement, and
     failure class.
   - The agent owns node facts.
   - The administrator owns storage roots. The workload creates its own run directories
     inside them and never creates a bind source.
   - The MCP owns research intent, per-call policy that is narrower than RBAC, the user's
     working tree, code delivery, and the interpretation of results.
3. **The job row is the ledger.** `tasks.job_id`, `experiments.job_id`, and
   `jobs.owner_id` already exist (`master/pkg/model/task.go:78`, `experiment.go:336`,
   `job.go:87-94`). `job_id` is the only handle. A job's next step (start, retry, end,
   cancel) is committed before it acts, and restore finishes whatever a crash interrupted.
4. **One task contract.** The four existing create requests are the contract, and a plan
   is a `dry_run` of the same request.
5. **The scheduler decides.** Nothing re-derives placement. The deferred evaluation and
   immediate admission reuse the pool's own `Schedule` and `findFits`.
6. **Failures are typed where they happen and recorded once.** Every allocation that ends
   gets exactly one exit class.
7. **No compatibility paths.** There is one minimum submission protocol number and one
   check, with no probing and no client-side substitutes.

## Layers and responsibilities

| Capability | Owner | What the MCP keeps |
|---|---|---|
| Identity, owner, key, digest, admission, cancel request | new columns on the `jobs` row | the `job_id` |
| Idempotent submit | master, unique on `(owner_id, idempotency_key)` | the `request_id` minted by `compute_plan` |
| Plan binding | master, `expected_digest` checked after replay | the `request_digest` from the plan |
| Spec merge, defaults | master create path, pool `task_container_defaults`, templates | `TaskSpec` rendered to a create request, with explicit pool and slots |
| Capacity and placement answer | the RM scheduler, after submit; `dry_run` validates but does not evaluate placement (evaluation deferred) | nothing; `compute_resources` stays a projection |
| Queue admission (immediate deferred) | RM scheduler tick, under `rp.mu`; `immediate` returns `UNIMPLEMENTED` | passes `admission` through; refuses `immediate` as `admission_unsupported` |
| GPU model, total memory, single node | scheduler hard constraints (model and memory deferred, F5) | the pool; spec fields deferred (M4) |
| Free GPU memory, utilization | agent, before `CreateContainer` (deferred, F5) | nothing |
| Bind sources | administrator: `task_container_defaults.bind_mounts`, workspace or master `checkpoint_storage` | the container-to-host map |
| Run directories | the workload inside those roots; the harness for `storage_path` | renders `mkdir -p` |
| Exit class and retries | allocation, trial, command (command retries deferred, F4) | explains the class |
| Status, list, cancel across clients | `GetSubmission`, `ListSubmissions`, `CancelSubmission` | pass-through |
| Pool and device facts | `GetResourcePools`, `GetAgents` | `compute_resources`, a projection |
| Code delivery | the rendered command (`git`, `path`) or the existing context (`context`); no fork change | revision pinning, enumeration, secret rules, manifest, the prelude |
| Data, outputs, checkpoints | shared storage under administrator roots; `checkpoint_storage` | `storage_sync`, `storage_fetch`, `storage_check` |
| Experiment record: config, metrics, artifacts, lineage, reports | W&B, linked to the job ([Experiment tracking with W&B](#experiment-tracking-with-wb)) | nothing; analysis goes through the official W&B MCP |
| Condition-triggered launches | W&B Automations through a narrow submit adapter, if ever built | nothing |
| Pool allow-list, max slots, `overwrite` | MCP | all of it |
| Usage interpretation | MCP | all of it |
| Slot caps | per job only: an experiment's `resources.max_slots`, a scheduler group keyed by job (`rm/agentrm/resource_pool.go:47`) that only the fair-share scheduler enforces (`fair_share.go:214-216`), not the default priority scheduler (`config/scheduler_config.go:28-37`); and config policies, which reject one submission over a workspace or global limit (`configpolicy/task_config_policy.go:45-60`) | per-request pool allow-list and max slots |

## Platform changes

### Ledger columns on `jobs`

The migration is `2026MMDD000000_job-submissions.tx.up.sql`. It has a `.down.sql`,
following the fork's own precedent.

```sql
ALTER TABLE jobs
  ADD COLUMN idempotency_key text,
  ADD COLUMN request_digest  text,
  ADD COLUMN cancel_requested_at timestamptz,
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
  string expected_digest = 4;  // optional; the request_digest a dry run returned
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

**First release.** `SubmitResult.evaluation` is reserved and stays empty: a dry run
validates the request but does not evaluate placement (see
[Scheduling evaluation](#scheduling-evaluation)). `ADMISSION_IMMEDIATE` returns
`UNIMPLEMENTED`, or `INVALID_ARGUMENT` for an experiment.

**Protocol version.** `GetMasterResponse` gains `int32 submission_protocol = 17;`
(`proto/src/determined/api/v1/master.proto:49-101`). `GetMaster` needs no login
(`master/internal/grpcutil/auth.go:47-51`). Masters without the field report 0. The number
becomes 1 once F1 and F2 are on fork `main`; the PR that completes the pair sets it. Each
deferred capability raises it when it lands.

### Submit handler order

One shared `submission` package implements this order for all four handlers.

1. **Digest.** Canonicalize and digest the client request. Nothing with side effects runs.
2. **Replay.** If a key is present, look up `(owner_id, key)`. Compare the stored digest
   with `expected_digest` when it is set, otherwise with the computed digest. If they are
   equal, check read authz on the stored job, pass it to `dispatch` (step 7), and return it
   with `replayed=true`. If not, return `ALREADY_EXISTS` naming the existing `job_id`.
3. **Plan check.** If `expected_digest` is set and differs from the computed digest,
   return `FAILED_PRECONDITION` with `plan_changed`. Nothing is written, and the key stays
   free.
4. **Parse.** Parse, merge, authorize, and apply config policy. Session minting and shell
   key generation move after step 5. Today they run too early:
   - `master/internal/core_experiment.go:401`, before the `ValidateOnly` return at
     `api_experiment.go:1655`;
   - `api_command.go:166-170`;
   - `api_generic_tasks.go:153-158`;
   - `api_shell.go:263-269`.
5. **Dry run.** If `dry_run` is set, return without evaluating placement. Nothing has been
   written.
6. **Commit.** One transaction writes:
   - the job row, with key, digest, and admission;
   - for tasks, the task row, the context directory, `allocation_workspace_info`, the first
     allocation row in `PENDING`, and `command_state` (including `generic_task_spec`).
     Today `command_state` is written after `StartAllocation` (`command/command.go:181-186`,
     `api_generic_tasks.go:378`). A crash in between leaves a task that is never restored.
     Because the row now exists, `requestResources` loads it instead of inserting it
     (`task/allocation.go:523-529`).
   - for experiments with `activate=true`, the experiment row, committed as `ACTIVE`.

   On a unique-index violation, roll back, delete the minted session and the
   `GroupPriorityChangeRegistry` entry (`command/command.go:117-119`), and return to
   step 2. The index is the guard; `cs.mu` is not. An error that leaves the commit's
   outcome unknown, such as a connection lost during `COMMIT`, is not a rollback: nothing
   is cleaned up, and the handler looks the key up again. If the job exists, it goes on to
   step 7; otherwise it returns a retryable `UNAVAILABLE`.
7. **Dispatch.** Pass the job to `dispatch(job_id)`, which runs detached from the request's
   context, so a client that disconnects after the commit cannot stop it. `dispatch` is
   idempotent and serialized per job. It reads the attempt that `command_state` names, or
   the experiment, and does nothing if that attempt has ended or is already registered
   with the allocation service (for an experiment, running in the experiment registry).
   Otherwise it calls `StartAllocation` or `e.Start`, then re-reads `cancel_requested_at`
   and kills the job if it is set. The allocation service refuses a second registration of
   an allocation ID, so each ID has at most one runtime instance. If the start fails
   in-process, `dispatch` closes the `PENDING` allocation with `INFRASTRUCTURE_FAILED` and
   sets `tasks.end_time`, or marks the experiment `ERROR`, so a replay returns a terminal
   record. It has three callers: the handler after commit, every replay, and a
   master-owned sweep that every 30 s dispatches committed jobs whose current attempt is
   `PENDING`, unregistered, and older than the interval. A committed submission therefore
   always progresses without a master restart.

**Restore.** Placement is the allocation's own `ASSIGNED` write (`task/allocation.go:630`),
which always comes before a launch (`:723`). `start_time` is not evidence either way: it is
set only at Pulling (`:749-753`), and `CloseOpenAllocations` stamps it on open rows
(`db/postgres_tasks.go:314-315`). `RestoreAllCommands` and `restoreGenericTasks`
(`command/command_service.go:55-72`, `core.go:880-955`) decide an open attempt on the
persisted `allocations.state`:

- **`PENDING`: never placed.** Delete the attempt's `allocation_resources` rows, which
  cascade to `resourcemanagers_agent_containers`, because the agent RM writes them in the
  tick before the allocation acknowledges them (`rm/agentrm/resource_pool.go:443-454`).
  Then it is re-requested under the same allocation ID. With `cancel_requested_at` set,
  the task ends instead.
- **Any later state: placed,** even if `start_time` is NULL. It is restored with
  `Restore=true` and reconciled through agent reattach, and the cancel check after
  registration kills it if `cancel_requested_at` is set. It is never re-requested. If it is
  not reattached, it ends with `INFRASTRUCTURE_FAILED`.

`RestoreAllCommands` selects its tasks with `tasks.end_time IS NULL` through
`command_state`, not through open allocations, and takes the attempt that `command_state`
names. Two more cases apply there:

- **Ended attempt:** it gets the exit decision that the crash interrupted. In the first
  release that decision ends the task: there are no system retries (see
  [System retries](#system-retries), deferred). The same case ends pre-upgrade tasks left
  open.
- **Failed restore:** one transaction closes the attempt as `INFRASTRUCTURE_FAILED` and
  sets `tasks.end_time`, as experiments do (`core.go:832-837`). Today it is only logged
  (`command_service.go:71-81`).

`Command.Start` uses the stored allocation ID instead of `<task>.1`
(`command/command.go:120`). Four agent-RM and DB fixes make the placed branch sound:

- The launch record is persisted before `StartContainer` is sent
  (`rm/agentrm/agent.go:219-223`), so any container that may exist is in the agent
  snapshot. If the write fails, nothing is sent.
- A container snapshot with no recorded launch is ended with `RestoreError` at restore.
- A reattach state mismatch carries a failure (`agent.go:823-829`), so the killed container
  classifies `INFRASTRUCTURE_FAILED` instead of "stopped early".
- `CloseOpenAllocations` stamps `start_time` only on the rows it closes.

This also fixes today's `RestoreError` "0 container snapshots" for commands that were
queued at restart (`resource_pool.go:189-190`). Only the agent RM was assessed.

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
  Admission admission = 11;
  google.protobuf.Timestamp submitted_at = 12; optional google.protobuf.Timestamp ended_at = 13;
  State state = 14;        // QUEUED RUNNING PAUSED COMPLETED FAILED CANCELED DELETED
  ExitClass exit_class = 15; string exit_reason = 16;   // from the allocation that ended the job
  repeated SubmissionTask tasks = 17;        // task_id, optional trial_id, repeated taskv1.Allocation
}
// taskv1.Allocation += resource_pool, exit_class, google.protobuf.Struct exit_detail,
//                      repeated Placement placements  /* node, accelerator_uuids */
```

- **Source.** A named query under `master/static/srv/` reads the database only. It joins
  `jobs` to tasks or experiments, allocations, and `allocation_accelerators`, and filters
  `job_type IN (COMMAND, SHELL, GENERIC, EXPERIMENT)`.
- **Workspace** is derived, not stored: through the project for experiments (which can
  move), and through `command_state` for tasks.
- **Task state.** A task's job has ended exactly when `tasks.end_time` is set, except that
  a GENERIC task in `PAUSED` or `STOPPING_PAUSED` is `PAUSED`, because pause writes
  `end_time` (`db/postgres_tasks.go:145-156`). A live task is `RUNNING` when the attempt
  named by `command_state` is placed and has not ended, and `QUEUED` otherwise, including
  between attempts and during an unpause. An attempt has ended when its `end_time` is set
  or its state is `TERMINATED`. An ended job with `cancel_requested_at` set is `CANCELED`;
  cancel never sets it on an ended job. Otherwise its state comes from its last attempt's
  class: `NONE` is `COMPLETED`, a failure class is `FAILED`, and a pre-upgrade `UNSPECIFIED` is
  `FAILED` only if the allocation recorded an `exit_error`. Every path that ends a task
  writes `end_time`: the exit decision, restore, a failed restore, and cancel.
- **Experiment state** comes from `experiments.state`. A deleted experiment is `DELETED`,
  because `DeleteExperiments` keeps the job row (`db/postgres_experiments.go:783-806`).
- **Authorization.** Every row passes the existing per-kind read authz. A `DELETED`
  experiment has no experiment row left to check (`db/postgres_experiments.go:800-803`),
  so only its owner and admins see it.
- **`get_task.sql`** also selects `slots`, `exit_reason`, and `status_code`. These are
  existing `taskv1.Allocation` fields (`proto/src/determined/task/v1/task.proto:93-98`).
- **`CancelSubmission`** is durable first. It authorizes from the database
  (`jobs.owner_id`, and the workspace through `command_state` or the project), locks the
  job row, returns an ended job unchanged, and otherwise sets `cancel_requested_at`. After
  commit it signals the current attempt through the allocation service, not the command
  registry, or kills the experiment; a missing allocation is not an error. Every
  allocation start (first dispatch, restore, experiment start, and any later system retry)
  checks the flag after it registers the allocation, so one of the two always sees the other. A
  GENERIC task with no live allocation, such as a paused one, is ended `CANCELED`
  directly. `KillCommand`, `KillShell`, and `KillGenericTask` take the same path; today the
  first two fail with `NotFound` when the registry misses (`api_command.go:259-262`,
  `api_shell.go:126-129`). The exit decision and cancel lock the same job row, so a late
  cancel never turns `COMPLETED` into `CANCELED`.
- **GENERIC cancel.** Every GENERIC task, parent or child, has its own job, and cancelling
  one covers its task and every descendant, as `KillGenericTask` does today
  (`api_generic_tasks.go`, `generic_task_resume.go`). It resolves the subtree, authorizes
  every member before changing anything, and skips members already `COMPLETED` or
  `CANCELED`. One transaction sets `cancel_requested_at` on each member's job and cancels
  the members' unfinished `generic_task_resume` rows. After commit it signals each
  member's current or intended allocation. A member with neither, such as a paused parent
  whose `no_pause` child still runs, ends `CANCELED` directly while the child is killed.
  `KillGenericTask` with `kill_from_root` resolves the root and cancels the root's job the
  same way. Restore never continues a resume whose task has `cancel_requested_at`; it ends
  the task `CANCELED`.

### Exit classes

```proto
enum ExitClass {
  EXIT_CLASS_UNSPECIFIED = 0;                // ended before the upgrade
  EXIT_CLASS_NONE = 1;                       // did not fail: completed, stopped, preempted, killed, aborted before placement
  EXIT_CLASS_PLACEMENT_UNSATISFIED = 2;
  EXIT_CLASS_NODE_PREFLIGHT_FAILED = 3;
  EXIT_CLASS_WORKLOAD_INITIALIZATION_FAILED = 4;
  EXIT_CLASS_WORKLOAD_FAILED = 5;
  EXIT_CLASS_INFRASTRUCTURE_FAILED = 6;
}
```

`allocations` gains `exit_class text` and `exit_detail jsonb`. The first release produces
`NONE`, `WORKLOAD_FAILED`, and `INFRASTRUCTURE_FAILED`. `PLACEMENT_UNSATISFIED`,
`NODE_PREFLIGHT_FAILED`, and `WORKLOAD_INITIALIZATION_FAILED` are reserved for the
[deferred designs](#deferred-designs).

**Exit record.** `finalize` writes the whole exit record in one UPDATE before it changes
anything else: `state = TERMINATED`, `end_time`, `exit_reason`, `exit_error`,
`status_code`, `exit_class`, and `exit_detail`. Only after that commits does it purge and
release the restorable resources, and only then does the task-level exit decision run.
Purging, releasing, and the exit notification are repeatable, so a crash at any point
leaves either an open allocation that restore handles or a complete record. Today
`finalize` writes `TERMINATED`, purges, and writes the exit status last
(`task/allocation.go:584-588,1074-1095`); a crash between them leaves a terminated
allocation without a class, whose evidence is already gone.

Startup restores first and then closes every allocation
that restore did not keep (`core.go:1432-1444`). Restore classifies the attempts it
decides. `closeOpenAllocations` (`core.go:957-963`) classifies the rest by state, in the
same UPDATE that closes them: `NONE` for `PENDING`, typically a queued trial allocation
that restore replaced (`trial.go:806-808`), and `INFRASTRUCTURE_FAILED` for any later
state. The class is an outcome, not a cause: who asked to stop a job is recorded once, as
`jobs.cancel_requested_at`. The classifier is total:

| Exit | Class |
|---|---|
| No error, including a kill, a preemption, or an abort before start (`TaskAborted`, `ResourcesAborted`) | `NONE` |
| `PlacementUnsatisfied` | `PLACEMENT_UNSATISFIED`; deferred with [immediate admission](#immediate-admission) (F3b) |
| `PreflightFailed` | `NODE_PREFLIGHT_FAILED`; deferred with the [agent preflight](#agent-preflight) (F5) |
| `SpecRejected` | deferred with F5: `WORKLOAD_INITIALIZATION_FAILED`, detail `{reason: spec_rejected, node, error}` |
| `ResourcesFailed` or `TaskError` | `WORKLOAD_FAILED`. The deferred [initialization boundary](#initialization-boundary) refines this: `WORKLOAD_INITIALIZATION_FAILED` if the allocation reports workload start and the failing resource has no `workload_started_at` |
| All others: agent errors, `RestoreError`, `ResourcesMissing`, handler errors, unknown | `INFRASTRUCTURE_FAILED` |

**One change for every layer.** `calculateExitStatus` panics on unlisted types
(`allocation.go:1219-1220`), so all of the following land together:

- `aproto.PreflightFailed` (`master/pkg/aproto/exit.go:95-120`);
- the `sproto` constants with their `Proto`/`From` mappings (`sproto/resources.go:250-330`);
- the `taskv1` enum;
- the switch cases, including the missing `ResourcesMissing` case and a default that
  classifies instead of panicking.

The same change maps unknown agent types to `UnknownError` (`resources.go:327-328`). It
also changes `a.crash(msg)` to `a.crash(*msg)` at `allocation.go:921`: today the pointer
misses the value-typed switch and lands in "handler crashed". The deferred F5 adds
`aproto.SpecRejected` the same way, through every layer at once.

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
| Submit | master create handler | schema, authz, config policy, key and digest, plan digest | error; nothing is created, and the key stays free |
| Place (deferred, F3b) | RM tick | IMMEDIATE only: placed this pass without preemption | `PLACEMENT_UNSATISFIED` |
| Node accept (deferred, F5) | agent: pull, preflight, `CreateContainer` | GPU thresholds on the allocated UUIDs | `NODE_PREFLIGHT_FAILED`; a spec Docker rejects at create, such as a missing bind source, and other pull or create errors are `WORKLOAD_INITIALIZATION_FAILED` |
| Init (deferred, F4) | container: `task-setup.sh`, then `prep_container` | Python, wheel, context download, proxy, workload-start post | `WORKLOAD_INITIALIZATION_FAILED` |
| Workload | startup hooks, then the rendered command: the MCP's prelude and the user command | exit code | `WORKLOAD_FAILED` |
| Any | agent or connection | agent loss, restore | `INFRASTRUCTURE_FAILED` |

Rows marked deferred belong to the [deferred designs](#deferred-designs). Until they land,
a failure during node accept or init classifies through the [Exit classes](#exit-classes)
table, as `WORKLOAD_FAILED` or `INFRASTRUCTURE_FAILED`.

### Restart accounting

In the first release, trials keep `max_restarts` and today's handling, and nothing else
retries automatically. The table is the deferred plan (see
[System retries](#system-retries)).

| Class | Counts against `max_restarts` | Automatic retry | Blocks the node |
|---|---|---|---|
| `PLACEMENT_UNSATISFIED` | no | none | no |
| `NODE_PREFLIGHT_FAILED` | no | new allocation within `max_system_retries` | yes |
| `WORKLOAD_INITIALIZATION_FAILED` | no | new allocation within `max_system_retries`, except a rejected spec | no |
| `WORKLOAD_FAILED` | trials only | trials, as today | through log policies, as today |
| `INFRASTRUCTURE_FAILED` | no | as today (transient for trials) | no |
| `NONE` | no | none | no |

In that plan, system retries apply to trials, commands, and shells.

### Idempotency and digest scope

- **Scope.** A key is scoped to `(jobs.owner_id, key)`, so all of a user's clients share
  it. Keys never expire, just as job rows are never deleted.
- **Binding.** A dry run never binds a key. An error before commit binds nothing,
  including `plan_changed`. Any committed submit consumes the key, including one that
  fails, so a new attempt needs a new key.
- **Plan binding.** A launch carries the plan's digest as `expected_digest`, and the
  master rejects a request whose digest differs. The check runs after replay, so a retry
  of a launch whose response was lost still returns its job, even if the working tree has
  changed since.
- **What the plan binds.** `expected_digest` binds the client request, the code, and the
  template's content. Master and pool defaults, such as `task_container_defaults`, are not
  bound: they are administrator policy, and a change between plan and launch applies to
  the launch. The plan therefore labels its effective config as observed at plan time,
  not frozen.
- **Digest.** The digest is SHA-256 over the master's canonical JSON of the client request
  (sorted keys, UTF-8, no HTML escaping), taken before master defaults. Only the master
  computes it, so no client has to reproduce the form and no canonicalization library is
  needed. The merged spec cannot be the identity: petnames, sshd ports,
  SSH keys, session tokens, and pool defaults vary between identical requests.

  | Included | Excluded |
  |---|---|
  | kind, workspace, project, template name and content | `idempotency_key`, `dry_run`, `expected_digest` |
  | config, parsed from YAML or Struct, then canonicalized | file `mtime`, `uid`, `gid` |
  | file manifest `{path, type, mode, sha256}` | master defaults and merged values |
  | parent, fork, inherit, `no_pause`, `activate`, `admission` | |

- **Code in the digest.** For `git`, the pinned SHA, which the rendered command and
  `COMPUTE_CODE_COMMIT` carry. For `context`, the file manifest, including
  `.code-provenance.json`. For `path`, only the directory string, so the plan reports the
  content as `unpinned`.
- **Key reuse.** A key replays only when its stored digest equals `expected_digest`, if
  set, or else the request's digest; any other digest returns 409. `admission` is part of
  the digest.
- **No stored request.** The job keeps only the digest. The effective config is read from
  the task or experiment, as today, so no second copy of a request that may carry secrets
  is kept.
- **Key and digest visibility.** Only the owner and admins see them, so a plain digest
  exposes nothing that could be brute-forced by other readers.

### Ownership

The owner is the authenticated user, recorded in `jobs.owner_id`. The MCP's `--owner`
namespace is removed. Control uses the fork's existing rules: owner or admin under basic
authz (`master/internal/api_ntsc_control.go:15-26`), and workspace permissions under
RBAC. A replay re-checks read authz on the stored job, so revoked access is honoured.

### Code and storage

- **Sources.** `code.source` selects how code reaches the container. It lives entirely in
  the MCP's rendered command; the fork is unchanged.

  | Source | Code root (`$COMPUTE_CODE_ROOT`) | Delivery | Mutable | Size |
  |---|---|---|---|---|
  | `git` | `/run/determined/code` | The prelude clones `repo` with `--shared --no-checkout` into container-local scratch and checks out the pinned commit with `--detach`. Nothing is uploaded. | no | no cap; node disk |
  | `context` | `/run/determined/workdir` | Tracked files at `revision` plus explicit `include` paths, sent as the task context (`files` or `model_definition`). Determined extracts it before the startup hooks (`harness/determined/exec/prep_container.py:28-36`). | no | 99,614,720 bytes |
  | `path` | `dir` on shared storage | Runs in place. | yes | none |

- **Working directory.** The MCP never sends `work_dir`. Determined rejects it together
  with a context (`master/internal/spec_util.go:98-103`), trials ignore it
  (`master/pkg/tasks/task_trial.go:52`), and it becomes the task user's home
  (`master/pkg/tasks/task.go:384-388`). The rendered command changes directory itself, so
  a pool default `work_dir` cannot move the code. `workdir` is relative to the code root
  and never escapes it. The plan checks `workdir` only lexically (relative, without `..`)
  and does not resolve symlinks, so the guarantee is a physical check at run time: after
  entering `workdir`, the prelude compares `pwd -P` with the resolved code root and, if the
  directory lies outside, fails before the first user statement with
  `compute: the workdir resolves to X, outside the code root`. A symlink that stays inside
  the tree works; one that leaves it, committed or on shared storage, fails. For `path`,
  the resolved `DIR` is the root, so `dir` may itself be a symlink. The check sets no
  variable the command can see. Experiments using `context` also get a copy under
  `/run/determined/train/model`.
- **Rendering.** Commands run under `bash -lc` and experiment entrypoints under `sh -c`
  (`harness/determined/exec/launch.py:43-44`), so the rendered text is POSIX sh with one
  shape for every source:

  ```sh
  <prelude> || exit $?
  <command>
  ```

  - `git`: one subshell delivers the code, then the prelude creates `OUT` and enters `WD`.
    The prelude is one line; here each step has its own line, and `…` marks elided text:

    ```sh
    ( fail() { printf '%s\n' "compute: git code delivery failed: $1" >&2; exit 1; }
      <unset every exported GIT_* variable> || fail …
      export HOME=/dev/null/home XDG_CONFIG_HOME=/dev/null/home GIT_CONFIG_NOSYSTEM=1 GIT_ATTR_NOSYSTEM=1 || fail …
      git -c safe.directory=R clone -q --template= --shared --no-checkout -- R /run/determined/code || fail …
      git -C /run/determined/code checkout -q --detach SHA || fail …
      test "$(git -C /run/determined/code rev-parse --show-toplevel)" = "$(cd -- /run/determined/code && pwd -P)" || fail …
      test "$(git -C /run/determined/code rev-parse HEAD)" = SHA || fail … ) &&
    mkdir -p -- OUT && cd -- /run/determined/code/WD && CHECK
    ```

    The subshell unsets every exported `GIT_*` variable, whose names it lists from `env`
    with `sed`; a probe variable proves that step ran, so an image without `env` or `sed`
    fails delivery instead of skipping it. It points `HOME` and `XDG_CONFIG_HOME` below
    `/dev/null`, where nothing can exist, and turns off system config and system
    attributes, so no user or system setting applies, even with a git too old for
    `GIT_CONFIG_GLOBAL`. The clone takes an empty template, so the image's default
    template seeds no hooks, config, attributes or refs. After the checkout, the work
    tree's toplevel must be `/run/determined/code` and `HEAD` must be `SHA`. So no `GIT_*`
    variable, user or system config, system attributes, or clone template, whether it comes
    from the image, a startup hook, or `TaskSpec.env`, can move or alter the delivered
    tree: the pinned tree is at the root with `HEAD` at `SHA`, or delivery prints one
    `compute: git code delivery failed: …` line and the prelude fails before any user
    statement. The isolation applies only to delivery, never to the workload: it ends with
    the subshell, so the command sees its own `GIT_*` variables, `HOME`, and config
    unchanged.
    When the revision has LFS pointers, the checkout runs with `GIT_LFS_SKIP_SMUDGE=0` and
    adds `-c filter.lfs.process='git-lfs filter-process' -c filter.lfs.required=true -c
    lfs.fetchinclude= -c lfs.fetchexclude=`, so a missing `git-lfs` fails instead of
    leaving pointers, and no image or environment setting skips the smudge. Submodules are
    never recursed. The clone target is not the workdir, because `git clone` needs an empty
    target and `/run/determined/workdir` can be the task user's `HOME`
    (`master/pkg/tasks/task.go:384-388`, `task-setup.sh:49-54`), where startup hooks may
    write. `/run/determined` belongs to the task user (`task.go:336`).
  - `context`: `mkdir -p -- OUT && cd -- /run/determined/workdir/WD && CHECK`.
  - `path`: `mkdir -p -- OUT && cd -- DIR/WD && CHECK`.

  `CHECK` is the workdir check described above; it is left out when `WD` is `.`, whose
  physical path is the resolved root. A failed prelude stops the job before any user
  statement, whatever the command's form (`a; b`, `a & b`, several lines). On `main`,
  `mkdir -p OUT && cd WD && CMD` runs later statements after a failure and can exit 0
  (`compute/service.py:585-599`). The prelude runs after the init boundary, so its
  failures classify `WORKLOAD_FAILED` and are never system-retried; the MCP reports them
  as code-delivery failures from the exit code and log. Shells run `sshd` and have no
  command, so they accept only `context` and `path` and get no prelude. A legacy
  `module:Class` experiment entrypoint is rejected, because any prefix breaks it
  (`launch.py:32-40`).
- **Plan checks for `git`.** `compute_plan` runs read-only `git` against the repository
  through a local view of it: a local mount, mapped from the container path by the policy.
  With SSH-only storage access the plan fails with `storage_not_local` and names the
  remedies, a local mount or the `context` source; planning `git` over SSH is deferred.
  `repo` must lie under a
  bind-mount target. `revision` must resolve to a commit, which is pinned as a full SHA.
  The pin fixes the code's identity; it does not keep the commit's objects available
  (risk 5).
  The commit must be contained in a branch or tag (`commit_not_on_ref`), because the clone
  borrows objects through alternates and `git gc` in the source prunes unreachable ones.
  Partial clones are rejected, because a lazy fetch needs the network, and so are linked
  worktrees and repositories with alternates, whose objects may sit at paths the container
  cannot resolve. A missing LFS
  object is an error (`lfs_object_missing`), and a submodule gets a
  `submodule_not_checked_out` warning. The image must provide `git`, and the container
  user must be able to read the repository; the plan cannot check either.
- **Read-only planning.** The plan runs `git` from an argument list, without user or system
  config, with every filter driver blanked and lazy fetches disabled, so no command a
  repository configures runs. It trusts the repository through `safe.directory`, as the
  container clone does. Planning needs git 2.32 or later, which it checks before running
  any command against a repository (`git_too_old`, naming the version found and the one
  required). Missing objects are found with a listing that never fetches, before any
  object is read, and every transport is refused, so no git version lazy-fetches or
  contacts a remote during a plan.
- **Plan checks for `context`.** Size is counted over the final payload exactly as the
  harness's `v1File_size` counts it (`harness/determined/common/v1file_utils.py:9-13`):
  every record with content, a symlink's target and `.code-provenance.json` included,
  counts the length of its base64 content times 3/4, which is its size rounded up to a
  multiple of three. A total over 99,614,720 bytes (`context.py:19-28`,
  `constants.py:5-18`) returns `context_too_large` with the total, the limit, the largest
  paths, and a hint to use `git` or shared storage, and makes no create call. The MCP does
  not simulate the master's limit on the whole request, a 96 MiB gRPC message
  (`master/internal/grpcutil/api.go:81-85`): an oversize request fails at the master and
  nothing is created. Relative symlinks that resolve inside the tree are kept;
  absolute or escaping ones are a plan error (`unsafe_symlink`), because the harness
  rejects the whole archive at init (`harness/determined/common/tarfile_utils.py:38-76`).
  The harness drops archive ownership and masks modes to 0755 (`tarfile_utils.py:78-89`).
  Hard secret rules are never uploaded, soft ones only when named in `include`, and both
  are listed as `excluded`. LFS pointers get an `lfs_pointer` warning, and a tracked root
  `startup-hook.sh`, which Determined sources before the command, gets `startup_hook`. The
  context is readable by anyone who can read the job; under basic authz that is any
  viewer of an experiment (`experiment/authz_basic_impl.go:26-30`). This is why secret
  rules apply only to `context`.
- **Provenance.** The config carries `COMPUTE_CODE_SOURCE`, `COMPUTE_CODE_ROOT`, and, for
  `git` and `context`, `COMPUTE_CODE_COMMIT`. They replace `COMPUTE_WORKDIR` and
  `COMPUTE_CODE_REVISION` (`compute/service.py:426-431`). `context` also ships
  `.code-provenance.json` with `{commit, dirty, included, excluded, skipped}` and no
  timestamps, so an unchanged tree renders the same digest. For `path`, the plan reports
  the HEAD and dirty state it observed, labelled unverified.
- **Storage roots.** The administrator mounts every bind source:
  `task_container_defaults.bind_mounts` and the `shared_fs.host_path` of the workspace or
  master `checkpoint_storage`. Each exists on every agent of the pool before any job runs.
  A job never names a bind source.
- **Data and outputs** live in run directories that the workload creates inside those
  roots; `storage_sync` and `storage_fetch` move them. A `git` repository and a `path`
  directory also lie under a root.
- **Checkpoints** set only a relative `storage_path`. `host_path` is inherited from the
  workspace default and then the master default (`master/internal/core_experiment.go:335-349`);
  without a `shared_fs` default, submit and `dry_run` fail the completeness check (`:371`).
  The harness creates the directory (`harness/determined/common/storage/base.py:58-80`),
  and checkpoint GC mounts the same root (`master/pkg/tasks/task_gc.go:126-136`).
- **Not in this design:** a snapshot service, an object store, or `snapshot_id`.

### Defaults

| Setting | Default | Set in |
|---|---|---|
| `admission` | `queue` | `TaskSpec`, matching `ADMISSION_UNSPECIFIED` |
| Idempotency key | at most 128 characters of `[A-Za-z0-9._:-]` | API |
| Task context size | 99,614,720 bytes as the harness counts them (`MAX_CONTEXT_SIZE`, existing) | harness constant, checked by the MCP |
| Minimum submission protocol | 1 for MCP 1.0 | MCP |

### What remains unverified

Identity, owner, state, placement, and exit class are authoritative, and
`cross_profile_unverifiable` is gone. These remain unverified, and each is labelled
wherever it appears:

- where a job will be placed, since placement is not evaluated before submit;
- a plan's effective config, whose master defaults can change before launch;
- pre-upgrade jobs, which have no key or digest and cannot be replayed;
- `path` code, which is mutable; the plan reports what it observed;
- `unmeasured` usage, when task-resources data is unavailable.

### Choices between alternatives

| Question | Decision | Rationale |
|---|---|---|
| First-release scope | F1 and F2 only | Reliable submit, observe, and cancel come first; evaluation, immediate admission, automatic retries, and preflight wait for demand (the fail-reliably principle). |
| Where the ledger lives | nullable columns on `jobs` | `jobs` is rarely updated (only `q_position`, `api_experiment.go:1524-1528`). No new table, no backfill, no FK that blocks deletes. |
| Create API | envelope on the four RPCs, no `Submit` oneof | One create path for every client, and no oneof body shape that the generators have never handled. |
| Handle | `job_id` only | One handle for every read and verb. |
| Plan binding | `expected_digest`, checked after replay | A client cannot recompute the master's digest, and a lost-response retry must still replay. |
| What a plan freezes | the request, the code, and the template; not master defaults | Defaults are administrator policy. Binding the merged config would need a list of volatile fields (petnames, ports, session tokens) that breaks silently when the master changes. |
| Job end | `tasks.end_time`, written by the exit decision | Inferring the end from allocations misreads the retry gap and pause. |
| Capacity vs placement | one class with a `static` or `busy` detail | One post-commit path. |
| Retry mechanism | new allocation, derived budget | Restore keys on state; reusing an allocation ID after `ASSIGNED` would mix two launches' resource rows. |
| Agent loss | unchanged | A budget of 3 would end long trials during maintenance. |
| Command and shell retries | added | The batch kind stays command, and blocked-node rows are per task. |
| Generic task retries | deferred | The global mutation lock (`generic_task_resume.go:25-28`) and resume ordinals need rework first. |
| Where the init boundary is stored | per resource, classified before purge | One node cannot mask another's failure. |
| Init evidence across the upgrade | `allocations.reports_workload_start` | A container keeps the entrypoint it was created with, so a NULL start is evidence only where it would have been reported. |
| Missing bind source | `WORKLOAD_INITIALIZATION_FAILED`, unrecoverable, no node block | It is a request error; blocked nodes are per task; Docker cannot see an unmounted filesystem whose mountpoint exists. |
| Directories | created by the workload inside administrator roots | A bind source must exist before the container is created. |
| Code | three explicit sources: `git`, `context`, `path` | `git` has no upload and no cap, `context` carries uncommitted changes, and `path` is mutable only by opt-in. No store, no GC, no dangling references. |
| Digest | plain SHA-256, owner-only | No new master secret. |
| Version gate | integer `submission_protocol` | A release string does not identify a feature set. |
| DB fallback for `GetCommand` | deferred | `GetSubmission` is the durable read for every client. |
| Consultation, `compute_cli.py` | removed | Experiment analysis belongs to the official W&B MCP, and `det` and the WebUI are the other clients. |

## Experiment tracking with W&B

A deployment may run W&B next to Determined. The two keep different records, linked
explicitly:

| Record | Owner | Answers |
|---|---|---|
| Execution: job, trial, task, allocation, exit class, placement, usage | Determined, through this MCP | Was it submitted, where does it run, why did it not start, what does it hold |
| Experiment: config, metrics, code and data references, checkpoints, lineage, reports | W&B, through its official MCP | How well did it do, which code and inputs produced it, which version is best |

- **Linking.** Every task container has `DET_TASK_ID` and `DET_ALLOCATION_ID`
  (`master/pkg/tasks/task.go:199-200`), and trials also have `DET_EXPERIMENT_ID` and
  `DET_TRIAL_ID` (`task_trial.go:115-116`). F2 adds `DET_JOB_ID`. `DET_CLUSTER_ID` is
  already set: the agent adds it to every container it starts
  (`agent/internal/containers/spec.go:219`), and Kubernetes and the dispatcher set it too
  (`kubernetesrm/spec.go:137`, `dispatcher_task.go:800`). A workload that uses W&B records
  these in the run config, groups runs by job, and derives a stable run ID from the cluster
  and the trial (or the task for commands), so a restarted trial resumes the same run. A
  multi-trial experiment maps to several runs; trials are never mixed into one run.
- **Code provenance.** The run records the code the MCP pinned: the commit for `git`, the
  manifest digest from `.code-provenance.json` for `context`, or the unpinned directory
  for `path`. It does not collect code again from a working tree that may have changed.
- **Data and checkpoints.** Large files stay on shared storage. W&B reference artifacts can
  record their paths and checksums without uploading them, but a reference neither freezes
  the files nor proves that every node can reach them.
- **Triggers.** Research events, such as a new artifact version or alias, can drive W&B
  Automations. A narrow adapter validates the event, selects an approved template, pins the
  artifact version, and submits through the create envelope with an idempotency key derived
  from the event, so a duplicate or delayed webhook never creates a second job.
  Cancellation, dispatch, and retries stay with Determined; a W&B `Crashed` run never
  restarts a job.
- **Searches.** Each experiment has one search controller, the Determined searcher by
  default. W&B Sweeps may drive trials instead, but never together with it.
- **Not adopted.** W&B Launch has no Determined backend, and its own backends would add a
  second execution controller on the same GPUs.
- **Deployment check.** Automations, Registry, and run-state triggers depend on the W&B
  version, deployment type, and license. Confirm them on the deployment before relying on
  them.
- **Credentials.** W&B keys stay out of task specs and the job ledger, like other secrets.

## The MCP after the refactor

### Tools

The tools fall into four groups: plan (`compute_plan`, `compute_launch`); observe,
read-only (`compute_status`, `compute_list`, `compute_logs`, `compute_usage`,
`compute_resources`, `storage_check`); control (`compute_cancel`); and transfer
(`storage_sync`, `storage_fetch`). There are 11, down from 16 on `main` (14 base tools plus
2 consultation tools). The count is not a goal: a read-only tool stays when it passes
platform or storage facts through without deriving new ones.

| Tool | Behaviour |
|---|---|
| `compute_plan(spec: TaskSpec)` | Resolves `code.revision` to a commit SHA, renders the spec, applies policy, and makes one create call with `dry_run`. Returns the resolved spec, a new UUIDv4 `request_id`, the master's `request_digest` (opaque to clients), the commit, the content digest (manifest for `context`, SHA for `git`, `unpinned` for `path`), the effective config summary, and warnings, including paths outside the effective bind-mount targets. Placement is not evaluated; the scheduler decides after launch. Every plan contacts the master. |
| `compute_launch(spec, request_id, request_digest)` | Renders the spec again and makes one create call with `idempotency_key=request_id` and `expected_digest=request_digest`. A spec pinned to a SHA is never re-resolved. If the content changed, for example because the spec still names a moving revision, it returns `plan_changed` with the new commit and content digest and creates nothing. A retry with the same arguments replays the job. Returns `job_id`, `replayed`, `submitted_at`, and `outcome`. |
| `compute_status(job_id)` | `GetSubmission`, plus an explanation of the exit class. |
| `compute_list(kind=None, state=None, limit=50, cursor=None)` | `ListSubmissions` for the caller, covering every client. |
| `compute_logs(job_id, trial_id=None, tail=200)` | Task logs. |
| `compute_usage(job_id, trial_id=None, allocation_id=None, window_seconds=3600, metrics=None, include_samples=False)` | Task resources, plus interpretation. |
| `compute_resources(pool=None)` | Pools from `GetResourcePools` (name, type, agents, slots available and used, slot type, slots per agent, aux capacity) and device models from `GetAgents`, stamped with `observed_at`. A projection with no verdict; placement is decided by the scheduler after launch. |
| `storage_check(path)` | Whether a container path exists, its type, and whether it is readable and writable, with the viewpoint: the backend (local mount or SSH host) and the user it runs as. The permissions are the viewpoint's, not the container user's. |
| `compute_cancel(job_id)` | `CancelSubmission`. |
| `storage_sync(local_dir, shared_dir, dry_run=True, overwrite=False)` | rsync; without `overwrite`, it adds `--ignore-existing`. |
| `storage_fetch(shared_dir, local_dir, dry_run=True, overwrite=False)` | rsync in the other direction. |

### `TaskSpec`

`TaskSpec` is a pydantic model, published as the input schema. It has these fields:

- `kind`: `command`, `shell`, or `experiment`.
- `name` and `command`.
- `code`, or omitted for no code:
  - `{source: git, repo, revision}`: `repo` is a container path on shared storage;
  - `{source: context, repo, revision, include, exclude}`: `repo` is a local working tree.
    Tracked files come from git objects at `revision`; `include` paths come from the
    working tree, and `dirty` records whether the working tree differs;
  - `{source: path, dir}`: `dir` is a container path on shared storage.

  `revision` defaults to `HEAD`, and the plan returns it pinned to a full SHA.
- `workdir`: relative to the code root (default `.`). `output_dir`: a container path
  inside a mounted root. The prelude runs `mkdir -p` on `output_dir`, then `cd` into
  `workdir`, and fails if its physical path lies outside the code root.
- `admission`: `queue` (the default and the only value); `immediate` is refused with
  `admission_unsupported`.
- `image`, `pool`, and `slots`. Pool and slots are always sent explicitly, from the policy
  defaults.
- `env`, `workspace`, and `project`.
- `experiment`: the experiment config. The MCP types its own outer fields and applies its
  narrower policy here; Determined validates the config itself (full schema, defaults,
  config policy) through `dry_run`, and the MCP does not vendor the expconf schemas. Keys
  that duplicate top-level fields (`resources.resource_pool`, `resources.slots_per_trial`,
  `environment.image`, `environment.environment_variables`) are rejected, so the compiled
  request is unambiguous and cannot bypass the MCP's limits. A search must set
  `max_concurrent_trials`, so that the MCP's per-request max slots can bound slots times
  concurrency. The MCP rejects `bind_mounts`, a `checkpoint_storage` `host_path` or
  `container_path`, and the legacy `checkpoint_path` and `tensorboard_path`;
  `storage_path` must be relative, without `..`.

There is no bind-mount field. `accelerators` stays out until the deferred M4.

### Modules

| Module | Content | Source |
|---|---|---|
| `mcp_server.py` | tool table | rewritten |
| `spec.py` | `TaskSpec`, compiler, and the prelude renderer | spec bucket of `compute/service.py`; `_render_entrypoint` (`:585-599`) is replaced |
| `policy.py` | defaults, pool allow-list, max slots, `overwrite`, container-to-host map | the kept part of `compute/profile.py` |
| `client.py` | transport, auth, redaction, protocol gate, create routes, submissions, logs, trials, task-resources, pool and agent reads for `compute_resources` and usage | `core/api_client.py`, minus deletions |
| `code.py` | revision pinning and `git` plan checks, `context` enumeration, include and secret rules, manifest, provenance | PR #1 `storage/snapshot.py:1-513,736-995` |
| `usage.py` | usage interpretation | `compute/service.py:67-103,176-230,772-1190` |
| `storage/` | sync, fetch, check with its viewpoint, read-only `git` access, config, auth, askpass | current package |
| `utils/secrets.py` | unchanged | – |

### Local state and version gate

- **No local state.** There is no SQLite, and `--owner` and `--db` are removed.
  Configuration holds the API URL and credentials, policy, storage access, and the
  container-to-host map. There are no local aliases or drafts: `compute_plan` returns the
  resolved spec and its digest, the caller sends both back to `compute_launch`, and
  `compute_list` recovers a lost `job_id`. Nothing depends on a local cache, so one can be
  added later without a contract change.
- **Version gate.** At startup the MCP reads `GET /api/v1/master`, which needs no login,
  and refuses to serve when `submission_protocol` is below its minimum: 1 for MCP 1.0. A
  later MCP that needs a deferred capability raises its minimum. The release string
  appears only in errors. It cannot be the gate: local builds report the previous tag
  (`version.sh:69-114`), and PR candidates report the target release before its phase is
  complete (`.github/workflows/fork-release.yml:52-65`).

### Deletion list

| Target | What goes |
|---|---|
| Whole files | `compute/store.py`, `compute/admission.py`, `compute_cli.py` with its `determined-compute` script in `pyproject.toml`. `agent_worker.py`, its tables, the `compute_consult` and `workflow_status` tools, and `docs/consultation.md` with its Chinese version are removed. |
| `compute/models.py` | `TaskRecord` (`:32-70`) |
| `compute/service.py` (dissolved) | upload-field rejection `:53-64,109-120`; claim, mark, and uncertainty `:166-175,601-661,725-735`; path validation `:489-515,569-583`; capacity hook `:662-668`; idempotency and legacy hash `:669-724,1435-1452`; capability check `:831-835`; remote identity `:1212-1294`; discover, adopt, and reconcile `:1295-1434`; binding `:1453-1526`; markers `:1527-1573` |
| `core/api_client.py` | kind, user, and cluster helpers `:220-264`; `list_remote_tasks` `:265-321`; markers and upload fields `:381-465`; per-kind dispatch `:466-521`, which becomes four create routes plus `CancelSubmission`; unsupported fallback `:567-582`. `list_resource_pools` and `list_gpu_devices` (`:647-690`) stay, because usage calls them (`compute/service.py:922,942`) and `allocation_accelerators` stores no GPU model; the pool read is widened for `compute_resources`. |
| `compute/profile.py` | path validators (`:161-199`), `cluster_identity`, and `fingerprint` (`:200-215`); the mount list stays as the container-to-host map |
| `mcp_server.py` | the reconcile, list, discover, and adopt tools (`:142-176`); the `--owner` and `--db` wiring |
| Tests | `test_adoption_store`, `test_remote_adoption`, `test_compute_legacy_retry`, `test_owner_namespace`, `test_admission`, `test_compute_cli`; the ledger and binding parts of `test_compute_service` and `test_api_client` |
| PR #1 | Closed unmerged. Dropped: `gpu_admission.py` with its NVML branch, `storage/paths.py`, cross-profile code, launch-path validation, `create_directories`, the store half of `snapshot.py` (`:514-735,996-1097`), and their tests. Salvaged: enumeration, secret rules, about 565 test lines, and `resolve_api_url`. |

The expected result is about 3,000 lines of source, compared with about 6,300 on `main`
and 8,600 with PR #1.

## Delivery plan

### Phasing

The first release makes submit, observe, and cancel reliable and nothing more: fork 0.41.0
carries F1 and F2 at submission protocol 1, and MCP 1.0 carries M1, M2, and M3. The
[deferred designs](#deferred-designs) have no release; each is scheduled only when real
demand appears.

| Fork release | Fork PRs | Submission protocol | MCP release |
|---|---|---|---|
| 0.41.0 | F1, F2 | 1 | 1.0: M1, M2, M3 |
| deferred | F3a, F3b, F4, F5 | raised by each | M4 |

### Fork pull requests

| PR | Content | Depends on |
|---|---|---|
| F1 Exit classes and fixes | `ExitClass` across all layers; new failure types; a total classifier; the `closeOpenAllocations` class by state; `UnknownError` mapping; `crash(*msg)`; `allocations.exit_class` and `exit_detail`; the exit record in one UPDATE before the purge; the `IdentifyTask` fix. Until F4 adds the init boundary, `ResourcesFailed` and `TaskError` classify as `WORKLOAD_FAILED`; after F4 they still do for allocations created before it | – |
| F2 Ledger | `jobs` migration; `SubmitOptions` (with `expected_digest`) and `SubmitResult`; handler order with the plan check, side-effect-free `dry_run` (no evaluation yet), and the `validate_only` alias; `ADMISSION_IMMEDIATE` returns `UNIMPLEMENTED` until F3b (`INVALID_ARGUMENT` for an experiment); the single commit transaction; `dispatch` with its three callers and the unknown-commit branch; the cancel check after every start; `ACTIVE` experiments; restore through `command_state`, keyed on state, with the `PENDING` purge, launch write-ahead, the unlaunched-snapshot end, the mismatch failure, the scoped `start_time` stamp, and `Command.Start` with the stored ID; `Get`/`List`/`CancelSubmission`, with the GENERIC subtree and resume handling; `cancel_requested_at`, written by `CancelSubmission` and the existing kill endpoints (`KillCommand`, `KillShell`, `KillGenericTask`, `api.proto:1539,1490,2645`); `get_task.sql` fields; `DET_JOB_ID` in task containers; `submission_protocol` 1 | F1 |
| F3a Evaluation | `TaskList.Clone`; `rp.Evaluate`; the static fit behind the old checks, keeping each call site's outcome; `dry_run` evaluation and its rate limit; `Unimplemented` on other RMs; raises `submission_protocol` | Deferred; F2 |
| F3b Immediate admission | the tick decision and handler wait; `allocations.immediate` and the `ADMITTING` state; IMMEDIATE restore; the provider-pool rejection; raises `submission_protocol` | Deferred; F3a |
| F4 Init boundary and retries | `workload_started_at` and `allocations.reports_workload_start`; the internal RPC; `prep_container --workload-start` in every entrypoint; `max_system_retries` with a derived budget; the trial case; the one-transaction exit decision for commands and shells, scoped to the ended allocation; per-allocation retry eligibility with closed workload-start posts; blocked-node rows; raises `submission_protocol` | Deferred; F1, F3b |
| F5 Devices and preflight | memory detection beside `device.Device`; `resources.accelerators`; `deviceSatisfied`; `cproto.Preflight`; the agent hook; the agent-version gate; `SpecRejected` mapping; raises `submission_protocol` | Deferred; F3a, F4 |

Master and agents are upgraded together. The fork keeps the reattach path compatible.
`device.Device` and the reconnect compare are unchanged, because a mismatch shuts the agent
down (`rm/agentrm/agent.go:609-634`). The container label version is unchanged too
(`agent/internal/containers/spec.go:171-175`). A running container is reattached only if
its agent reconnects within `agent_reconnect_wait` (default 150 s, `aproto/net.go:9-17`)
of the new master starting, and the container is still in the state recorded before the
stop (`containers/manager.go:287-295`). Otherwise it is killed, and its allocation ends
with `RestoreError`, classified `INFRASTRUCTURE_FAILED`. A reattached container keeps its
old entrypoint and wheel. The new master keeps serving the previous release's task APIs,
and the container's allocation is classified without the init boundary.

### MCP pull requests

| PR | Content | Depends on |
|---|---|---|
| M1 Narrow the server | remove the consultation worker; delete `compute_cli.py`; close PR #1 | – |
| M2 Cut to the ledger | `client.py`, the protocol gate, and the `job_id` handle; launch, status, list, logs, usage, and cancel over submissions; delete the store, markers, reconcile, discover, adopt, binding, and owner namespace | F2 |
| M3 Typed spec | `TaskSpec`, `spec.py`, `policy.py`, and `code.py`; the three code sources and the prelude renderer; plan through `dry_run` and launch bound by `expected_digest`; `admission` passed through; `compute_resources` as a projection and `storage_check` with its viewpoint; delete `admission.py` and path validation | F2 |
| M2 + M3 wiring | One cutover PR carries M2 and the M3 wiring (`policy.py`, the compiler, plan and launch, `compute_resources`, `storage_check`, and the M3 deletions), stacked on the M3 core (`TaskSpec`, `code.py`, and the prelude renderer) | F2 |
| M4 Accelerators | `accelerators` in `TaskSpec` | Deferred; F3b, F5 |

- **Merge order.** An MCP PR merges only after its fork dependencies are on fork `main`.
  Integration tests run against pre-release builds of the target fork release.
- **Release.** MCP 1.0 is not released until the fork tags 0.41.0.
- **Docs.** Each PR updates the English and Chinese docs it touches. M3 rewrites
  [Compute service reference](compute-service.md), [Agent workflow](agent-workflow.md),
  [Troubleshooting](troubleshooting.md), and the `AGENTS` routes.

## Deferred designs

These designs are complete but not scheduled. Each is built only when real demand appears
and it passes the complexity gate in [Principles](#principles), and each raises
`submission_protocol` when it lands. Planning `git` code over SSH-only storage access is
also deferred; it needs no master change.

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
- **Handler wait.** A step 8 follows dispatch in the
  [submit handler order](#submit-handler-order): once the launch call returns, outside
  `cs.mu` (`command/command_service.go:95`), the handler polls the allocation through the
  allocation service's read lock until it leaves `PENDING`. The wait is capped at 5 s,
  after which the outcome is `PENDING`.
- **Scope.** IMMEDIATE applies to every allocation that the submit itself creates,
  including system retries. Later user-driven allocations, such as a generic resume,
  queue. Each allocation records its own admission in
  `allocations.immediate boolean NOT NULL DEFAULT false`, set when it is inserted. Restore
  and state derivation read it, never `jobs.admission`, so a queued resume of an
  IMMEDIATE job restores as queued.
- **Restore.** A `PENDING` attempt marked `immediate` ends with `PLACEMENT_UNSATISFIED`
  after the purge, instead of being re-requested. A placed attempt never ends
  `PLACEMENT_UNSATISFIED`.
- **State.** Until its first decision, an immediate attempt reads `ADMITTING`, which F3b
  adds to `Submission.State` and to the task state rules. It never reads `QUEUED`: it is
  placed, or it ends `PLACEMENT_UNSATISFIED`.
- **Rejected at submit.** Every experiment, single-trial ones included, gets
  `INVALID_ARGUMENT` (see [Admission](#admission)). Provider-backed pools get
  `FAILED_PRECONDITION`.

### Initialization boundary

- **Columns.** `allocation_resources` gains `workload_started_at`. `allocations` gains
  `reports_workload_start boolean NOT NULL DEFAULT false`, which F4 sets on every
  allocation row it inserts (`task/allocation.go:527`). It is left out of
  `AddAllocation`'s `ON CONFLICT` list (`db/postgres_tasks.go:190-193`), so it never
  changes after insert.
- **Why a marker.** A container keeps the entrypoint and wheel it was created with
  (`master/pkg/tasks/task.go:151-159`, `copy.go:35-37`), and a restored allocation starts
  no containers (`allocation.go:666-693`). Allocations that run across the upgrade
  therefore never post. A NULL `workload_started_at` means "not reported"; it means "not
  started" only when the allocation reports workload start. Otherwise `exit_detail`
  carries `init_boundary: "unknown"`.
- **RPC.** `PostAllocationWorkloadStarted{allocation_id, resources_id}`, authorized like
  `AllocationReady` (`api.proto:921`). It sets the time once and returns OK on a repeat.
  It fails for an unknown or closed allocation, or for a resource of another allocation.
- **Who posts it.** `prep_container --workload-start`, as the last step of the call before
  the startup hooks, in every entrypoint that calls `prep_container`:
  `master/static/srv/command-entrypoint.sh:11`, `shell-entrypoint.sh:7`,
  `generic-task-entrypoint.sh:13`, `entrypoint.sh:9`, `notebook-entrypoint.sh:14`,
  `tensorboard-entrypoint.sh:12`, and `gc-checkpoints-entrypoint.sh:7`. The flag is
  explicit because Slurm's `task-setup.sh:40` calls `prep_container` earlier. The trial's
  later `--rendezvous` call does not post. Hooks are user code.
- **Fail closed.** A failed post raises, `prep_container` exits non-zero, and `set -e`,
  already on at every call site, stops the script before the hooks. A harness older than
  F4 that comes from the image (`DET_SKIP_PIP_INSTALL`, `task-setup.sh:26-33`) rejects the
  flag and fails the same way; such images must carry the F4 harness. A post that commits
  but loses its response ends as `WORKLOAD_FAILED`, which never re-runs user code.
- **When the class is set.** The class is computed from `workload_started_at` before the
  exit record is written, and the rows are purged only after it commits (see
  [Exit classes](#exit-classes)). These rows live exactly as long as the allocation, because startup purges only
  the rows of closed allocations (`taskmodel/resources.go:53-63`).

### System retries

- **New allocation per retry.** A retry is always a new allocation `<task>.<n+1>`. An
  allocation is never re-placed.
- **Budget.** `resources.max_system_retries` (master default 3) bounds
  `NODE_PREFLIGHT_FAILED` and `WORKLOAD_INITIALIZATION_FAILED`, except a rejected spec,
  which is unrecoverable: F5 adds `ResourcesFailedError{SpecRejected}` to
  `sproto.IsUnrecoverableSystemError` (`sproto/resources.go:342-349`). The budget is
  derived by counting the task's allocations with those classes, so it survives restarts
  without a counter.
- **Blocked nodes.** Only `NODE_PREFLIGHT_FAILED` writes `(task, node, "preflight:<check>")`
  into the existing blocked-node table, which is keyed by task ID with no FK
  (`logpattern/logpattern.go:133-159`).
- **Stopping.** Retries stop when the budget is spent or when the blocked nodes leave no
  static fit.
- **Trials.** A new case between `trial.go:612` and `:621` re-allocates without
  incrementing `restarts`. The unrecoverable check at `:603` comes first, so a rejected
  spec ends the trial in `ERROR` without incrementing `restarts`.
- **Commands and shells.** These gain re-allocation through one exit decision in
  `OnExit`. One transaction locks the job row, reads the ended attempt's class (already
  durable, because the exit record commits before `onExit`) and the budget, and
  then either inserts `<task>.<n+1>` as `PENDING` with its workspace record and
  blocked-node rows and points `command_state` at it, or sets `tasks.end_time`. It retries
  only when `cancel_requested_at` is NULL. The new attempt starts only after commit, loads
  the existing row, and then checks the cancel flag. A retrying exit keeps the session
  token and the registry entry and schedules no garbage collection; only the end branch
  runs today's `OnExit` tail (`command.go:239-279`). A crash at any point leaves a state
  that restore finishes.
- **Eligibility is per allocation.** A system retry requires that the allocation reports
  workload start and that none of its resources has `workload_started_at`. The exit
  decision first closes the allocation to workload-start posts (the RPC fails once the
  allocation is exiting), so no resource crosses the boundary after the decision. If any
  resource has crossed, the allocation keeps its class, taken from the failing resource,
  but is not system-retried: commands and shells end, and a trial follows `max_restarts`.
  A multi-node allocation whose first node already ran its startup hooks is therefore
  never re-run as an initialization retry.
- **Decision scope.** The exit decision acts only while `command_state`, or the trial's
  current allocation, names the ended allocation. A late or repeated exit of `.1` after
  `.2` exists is a no-op: it neither ends `.2` nor inserts `.3`. The budget is counted
  from rows, so nothing is spent twice.
- **Unchanged.** `INFRASTRUCTURE_FAILED` keeps today's transient handling
  (`sproto/resources.go:353-371`). Generic tasks end with their class, as they do today
  (`spec_util.go:151-160`).

### Placement constraints (scheduler)

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
- **Upgrade gate.** An agent fits a request with `accelerators` only if its
  `AgentStarted.Version` (`master/pkg/aproto/master_message.go:80`) supports device memory
  and preflight. Older agents never receive such a request, and `dry_run` lists them in
  `blocked_nodes` with the reason `agent_upgrade_pending`, so enabling accelerators needs
  no flag day across the pool.
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
  releasing memory. GPU thresholds are its only checks.
- **What it does not do.** It never runs `stat` on a host path or uses
  `--query-compute-apps`, because the documented agent container sees only `docker.sock`
  and its config (`docs/setup-cluster/on-prem/options/docker.rst:117-171`).
- **Rejected specs.** A bind source must exist before `CreateContainer`. Bind mounts are
  `mount.TypeBind` (`master/pkg/tasks/mounts.go:19-27`), and Docker validates them at
  create, returning `InvalidParameter`. The agent maps any `CreateContainer` error that
  satisfies `errdefs.IsInvalidParameter` to `aproto.SpecRejected`; today it is a
  `TaskError` (`container.go:341-342`). This is not a preflight check and never blocks a
  node. The fork never sets `BindOptions.CreateMountpoint`, because Docker would then
  create a missing root on local disk.
- **Failure detail.** A failure carries `{check, device, observed, required}`, both in the
  exit detail and in a container log line.

### Admission

- **`queue`** is the default, matching `ADMISSION_UNSPECIFIED`, so `det` and the WebUI are
  unchanged. The MCP passes `TaskSpec.admission` through as `SubmitOptions.admission` in
  both the dry run and the launch. It has no evaluate-then-submit path.
- **`immediate`.** The pool's scheduler places the request in the tick that decides it,
  without preemption. Otherwise it ends `PLACEMENT_UNSATISFIED` (`static` or `busy`) and
  is never queued. Until F3b the master returns `UNIMPLEMENTED`, which the MCP reports as
  `admission_unsupported`.
- **Static infeasibility** stays a warning for `queue`.
- **Experiments.** `immediate` is rejected with `INVALID_ARGUMENT` for every experiment,
  single-trial ones included. IMMEDIATE decides only allocations requested inside the
  submit call. An experiment's trials each request their own allocation once the
  experiment activates (`api_experiment.go:1674-1687`, `trial.go:371-374`), later trials
  are created as earlier ones exit, and restarts are new allocations (`trial.go:692-700`).
  Deciding the first allocations would admit part of a search and queue the rest.
  `dry_run` reports `placeable_now` out of N as information only.

### Placement constraints (semantics)

- **Static facts are hard constraints:** GPU model, total memory, and `is_single_node`,
  which is unchanged. Commands stay single-agent (`command.go:161-176`).
- **Dynamic facts are preflight checks:** free memory and utilization. Neither is ever a
  placement fact.
- **No reservations.** A preflight pass reserves nothing, and an evaluation is a snapshot
  taken at `evaluated_at`.

### Deferred choices

These choices apply to [immediate admission](#immediate-admission).

| Question | Decision | Rationale |
|---|---|---|
| When IMMEDIATE is decided | next tick, in a scratch pass | A pass inside `rp.Allocate` would hold the allocation-service write lock. |
| IMMEDIATE for experiments | rejected, single-trial ones included | An experiment requests its allocations after the submit call, and there is no gang scheduling. |
| IMMEDIATE on retries | inherited by allocations the submit creates | Only commands, shells, and generic tasks use it, and none has workload restarts. |

### Deferred acceptance tests

These rows apply once the deferred PR that owns each is built.

| Scenario | Required result | Owner |
|---|---|---|
| Crash during the retry transition: after `.1` ends, before the decision; or after the retry commit, before the start | Restore runs the decision and inserts `.2`; or it re-requests `.2` by ID. No `.3`, `.2` is not closed, and restore reattaches `.2`, not `.1`. | F4 |
| Polling across a system retry | Never `FAILED` followed by `QUEUED`. | F4 |
| Retry still running 25 h after the first attempt ended | Still registered, killable, and holding its session token. | F4 |
| Pre-upgrade running allocation fails in user code after the upgrade | `WORKLOAD_FAILED` with `init_boundary: "unknown"`, no `<task>.<n+1>`, and its side effect happens once; a legacy trial counts against `max_restarts`. | F4 |
| Classifier over `reports_workload_start` × `workload_started_at` | Only (true, NULL) yields `WORKLOAD_INITIALIZATION_FAILED`. | F4 |
| Workload-start post fails; post commits but the response is lost; image harness older than F4 | Hooks and command never run, class `WORKLOAD_INITIALIZATION_FAILED` with a retry in budget; `WORKLOAD_FAILED` with no system retry (commands and shells do not rerun; a trial follows `max_restarts`); exit before hooks, `WORKLOAD_INITIALIZATION_FAILED`. | F4 |
| Missing bind source (command, and experiment `host_path`) | One allocation, `WORKLOAD_INITIALIZATION_FAILED` with `spec_rejected` and the host path, no blocked-node row, and a trial in `ERROR` with `restarts` unchanged. A GPU preflight failure still blocks the node and retries. | F5 |
| Two IMMEDIATE requests race for the last slots | Exactly one is placed; the other ends `PLACEMENT_UNSATISFIED` (`busy`); neither ever reads `QUEUED`. | F3b |
| An IMMEDIATE GENERIC job's queued resume is `PENDING` at a restart | It restores as queued and never ends `PLACEMENT_UNSATISFIED`. | F3b |
| Crash after the exit record, after the purge, and before the exit decision | Restore keeps the retry budget. | F4 |
| A repeated exit of `.1` arrives after `.2` runs | `.2` keeps running; no `.3`; the budget is unchanged. | F4 |
| Resource A posted workload start and ran a hook with a side effect; resource B then fails preflight | No system retry, and A's hook runs once; the class is `NODE_PREFLIGHT_FAILED`, and a trial follows `max_restarts`. | F4, F5 |
| An agent older than F5 in a pool that receives `accelerators` requests | It is never placed for them; `dry_run` lists it as `agent_upgrade_pending`. | F5 |

### Deferred open questions

1. **Scheduler latency.** `dry_run` and the immediate scratch pass each add at most one
   pass under `rp.mu`. Add a latency metric alongside the rate limit.
2. **Pass disagreement.** If the scratch and real passes disagree, the safety net rejects
   the request; it never queues it.
3. **Strict IMMEDIATE.** Because IMMEDIATE never preempts or overtakes, it refuses often on
   a busy cluster. The alternative is `admission=queue`.
4. **Reconnect window.** Treating agents inside the reconnect window as enabled needs their
   stashed state (`agent.go:80-83`). Verify in F3a.
5. **Retry budget.** Is 3 the right default for `max_system_retries`?
6. **Upgrade window.** Allocations that run across the F4 upgrade report no workload
   start. On failure they classify `WORKLOAD_FAILED` and are never system-retried.

## Acceptance tests

Each row is a required result. The owner is the PR that must prove it.

| Scenario | Required result | Owner |
|---|---|---|
| Concurrent submits with the same key and content | One job row. The loser hits the unique index, rolls back, deletes its session and registry entry, and returns the winner's job with `replayed=true`. | F2 |
| Same key, different digest (content, `admission`, or `expected_digest`) | `ALREADY_EXISTS` naming the existing `job_id`; nothing is written. | F2 |
| Repeated `dry_run` | No job, task, or allocation row, session, shell key, or registry entry; the key stays free; every call returns the same `request_digest`. | F2 |
| Plan drift: HEAD moves or an included file changes after the plan | The pinned spec still submits the planned SHA. A spec whose content changed returns `plan_changed`; no row is written and a fresh plan and key succeed. | F2, M3 |
| Lost launch response, then the tree changes and the client retries | `replayed=true` with the same `job_id`; no duplicate. | F2, M3 |
| Master crash after `ASSIGNED`, before Pulling | Restored with `Restore=true` under the same allocation ID; no second allocation row and no new `StartContainer`. | F2 |
| `PENDING` allocation with tick-written resource rows at restart | The rows are purged before the re-request under the same ID; a later restart restores exactly one container. | F2 |
| Launch write-ahead | `StartContainer` is sent only after the agent snapshot lists the container; if that write fails, nothing is sent. | F2 |
| Reattach finds a changed container state | The container is killed and the allocation ends `INFRASTRUCTURE_FAILED`, not `NONE`. | F2 |
| Queued command across two restarts | Re-requested under the same ID with `start_time` still NULL; no "0 container snapshots". | F2 |
| Cancel before the in-memory object exists (after commit, before registration; or a job missing from the registry) | The allocation is killed right after registration and the job ends `CANCELED`. `KillCommand` and `KillShell` never return `NotFound` for a live job. | F2 |
| Cancel, then crash before the kill lands | Restore ends the task `CANCELED` and does not re-request the attempt. | F2 |
| Cancel racing a successful completion | `COMPLETED` if the end commits first, `CANCELED` only if the flag commits first. | F2 |
| Failed restore | The attempt is `INFRASTRUCTURE_FAILED` and `tasks.end_time` is set in one transaction; `GetSubmission` reads `FAILED`. | F2 |
| Paused and unpausing generic tasks | `PAUSED` and `STOPPING_PAUSED` read `PAUSED`; an unpause in flight reads `QUEUED`; cancelling a paused task ends it `CANCELED`. | F2 |
| Relative checkpoint `storage_path` | Lands under the inherited `host_path`; with no `shared_fs` default, submit and `dry_run` fail completeness and create nothing. | F2, M3 |
| `admission=immediate` | `admission_unsupported` and nothing created; experiments also get `INVALID_ARGUMENT` at the dry run. | M3, F2 |
| Multi-statement command after a failed prelude, for each source | `a; b`, `false \|\| b`, two lines, and `a & b; wait` exit with the prelude's status and run no user statement, under `sh -c` and `bash -lc`. | M3 |
| Renderer shape | Exactly `<prelude> \|\| exit $?`, a newline, and the command; no `work_dir` in any config; `module:Class` rejected. | M3 |
| Hostile `GIT_*` variables or image git config at delivery | The pinned tree is at the root with `HEAD` at the pinned SHA, or the prelude fails before any user statement; the workload environment is unchanged. | M3 |
| A workdir through a symlink that leaves the tree | The prelude fails before any user statement; an in-tree symlink works. | M3 |
| `git` plan checks | Pinned SHA; `commit_not_on_ref`; partial clone rejected; `lfs_object_missing`; shell with `git` rejected. | M3 |
| Context limits | A context counted at 99,614,718 bytes passes; one file of 99,614,719 bytes counts as 99,614,721 and returns `context_too_large` with no create call; an escaping symlink returns `unsafe_symlink`; an unchanged tree renders the same digest; the counted size equals the harness count of the final payload, symlinks included. | M3 |
| Protocol gate | A master without `submission_protocol`, or below the minimum, is refused, whatever its release string. | M2 |
| `COMMIT` succeeds but the handler sees an error, or the client disconnects after the commit; the master keeps running | The job starts without a restart, through the handler's re-check, a replay, or the sweep; concurrent replays register one allocation and one job. | F2 |
| Cancel a paused GENERIC parent whose `no_pause` child runs and whose resume is unfinished, then restart | Every member ends `CANCELED`, the resume is not continued, and every member was authorized before any change. | F2 |
| The template changes between plan and launch | `plan_changed`; a change to master defaults applies without it. | F2, M3 |
| Crash after the exit record, after the purge, and before the exit decision | The record is complete and restore keeps the class. | F1 |
| Observation tools | `compute_resources` returns only projected fields and `observed_at`; `storage_check` always states its viewpoint. | M3 |

**Carried from PR #1.** Four PR #1 behaviours must be re-verified in the layer that now
owns them:

| Behaviour | New owner |
|---|---|
| A GPU mismatch never runs user code | in the first release, the GPU model is chosen by choosing a pool; nothing checks the model or free memory before user code runs. Scheduler hard constraints and the agent preflight before `CreateContainer` are deferred (F5). |
| A failed prelude never runs later statements | the MCP renderer, `<prelude> \|\| exit $?` (M3) |
| Plan drift never masquerades as the reviewed plan | the master's `expected_digest` check over the request, code, and template (F2), with SHA pinning in the plan (M3); master defaults are observed, not bound |
| Identity checks are never bypassed for convenience | master authz: replay re-checks read authz, and `CancelSubmission` authorizes from the database (F2) |

## Out of scope

- **Generic tasks as the only kind.** This includes collapsing kinds into GENERIC,
  generic re-allocation, and reworking the generic lock.
- **Searches.** Gang scheduling and strict admission for searches.
- **Snapshots and artifacts.** There is no snapshot or artifact service. If one is ever
  needed, it must scope keys to `(owner_id, snapshot_id)`, authorize every reference, and
  hold a lease from upload to first reference.
- **Aggregate quotas.** Per-user or per-workspace totals. The platform and the MCP cap
  each job, not a user's total.
- **Condition-triggered launches.** The adapter behind W&B Automations is not part of this
  plan. Whatever submits uses the create envelope with its own idempotency keys, never the
  scheduler loop.
- **Legacy reads.** A database fallback for `det cmd` and the WebUI task list.
- **Other resource managers.** Kubernetes and dispatcher RMs, including their restore
  reconciliation, and ROCm and MIG preflight.
- **Mixed GPUs.** Device filtering on mixed-GPU agents.
- **Checkpoint GC.** Read authz for checkpoint-GC tasks (`master/internal/api_tasks.go:65-69`).

## Risks and open questions

1. **Experiments committed `ACTIVE`.** The create path must skip `ActivateExperiment`
   (`api_experiment.go:1682-1687`) and start the experiment the way restore does; restore
   already handles a nil snapshot (`restore.go:118-124`). Verify in F2.
2. **Generic cancel.** `CancelSubmission` on a generic task can hit the global mutation lock
   (`api_generic_tasks.go:580-583`). It returns a retryable `UNAVAILABLE`.
3. **Secrets in commands.** Secrets typed into a command line are stored in the task or
   experiment config, as today. The docs must say so.
4. **Legacy rows.** Allocations that ended before the upgrade show
   `EXIT_CLASS_UNSPECIFIED`, and jobs from before the upgrade have no key or digest. F2
   tests `GetSubmission` and `ListSubmissions` against such rows.
5. **Source prune.** A `git` clone borrows objects from the source repository. If the
   branch or tag that contained a pinned commit moves or is deleted and the source runs
   `git gc`, a later start of that job fails.
6. **Clone target.** Resolved in M3: `git` code clones into `/run/determined/code`, not
   the workdir, which can be the task user's `HOME` (see [Code and storage](#code-and-storage)).
