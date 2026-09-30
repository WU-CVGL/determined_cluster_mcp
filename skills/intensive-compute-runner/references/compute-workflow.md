# Compute workflow reference

Load this reference when writing a TaskSpec, copying a workspace to shared storage, or diagnosing a launch or a failed job.

## TaskSpec fields

`compute_plan(spec)` and `compute_launch(spec, request_id, request_digest)` take a TaskSpec with these fields. Unknown fields are rejected.

| Field | Meaning |
| --- | --- |
| `kind` | `command`, `shell`, or `experiment` |
| `name` | Short task-specific display name, at most 128 characters; never an internal ID |
| `command` | Required for a command or an experiment; a shell has none |
| `code` | `{source: git, repo, revision}`, `{source: context, repo, revision, include, exclude}`, `{source: path, dir}`, or omitted |
| `workdir` | Directory relative to the code root, without `..`; default `.`; needs `code` |
| `output_dir` | Required for a command or an experiment: an absolute container path under a policy mount that is not `read_only`. Created before the command runs and exported as `COMPUTE_OUTPUT_DIR` |
| `slots`, `pool`, `image` | Optional overrides of the policy defaults |
| `env` | Environment variables; the `COMPUTE_` prefix is reserved |
| `workspace`, `project` | Names; a command or shell takes only a workspace, an experiment both or neither |
| `experiment` | Experiment config; only for an experiment |
| `admission` | `queue`, the default and only supported value; `immediate` returns `admission_unsupported` and creates nothing |

A command that evaluates a commit on shared storage looks like this; the paths are placeholders for container paths from the deployment's policy:

```json
{
  "kind": "command",
  "name": "evaluate-checkpoint",
  "command": "python scripts/evaluate.py --output \"$COMPUTE_OUTPUT_DIR/metrics.json\"",
  "code": {"source": "git", "repo": "/shared-container/project/repo", "revision": "main"},
  "output_dir": "/shared-container/project/results/evaluate-checkpoint",
  "slots": 1
}
```

`experiment` may not set what the MCP renders: `entrypoint` (use `command`), `name`, `workspace`, `project`, `resources.resource_pool`, `resources.slots_per_trial`, `environment.image`, or `environment.environment_variables` (use the top-level fields), nor `bind_mounts`. A search must set `searcher.max_concurrent_trials`, because the policy's `max_slots` bounds slots times concurrent trials. Checkpoints use `checkpoint_storage: {type: shared_fs, storage_path: <relative path>}` and inherit `host_path` from the workspace or master default. A legacy `module:Class` command is refused. Determined validates the rest of the config in the plan's dry run.

## Code sources

| `code.source` | Fields | Code root | Delivery |
| --- | --- | --- | --- |
| `git` | `repo` (container path on shared storage), `revision` (default `HEAD`) | `/run/determined/code` | The task clones `repo` at the pinned commit; nothing is uploaded |
| `context` | `repo` (absolute local path of a working tree), `revision`, `include`, `exclude` | `/run/determined/workdir` | Tracked files at `revision` plus the `include` paths, uploaded as the task context, at most 99,614,720 bytes |
| `path` | `dir` (container path on shared storage) | `dir` | Runs in place; never pinned |

For durable runs, prefer `git` or `context`, so the plan pins exactly what runs; reserve `path` for interactive shells and debugging.

- `git`: the plan reads the repository through a local mount of the machine running the MCP server; without one it returns `storage_not_local`, and it never plans over SSH. The commit must be on a branch or tag (`commit_not_on_ref`), because the task's clone borrows objects from the repository. Partial clones, linked worktrees, and repositories with alternates are refused, and planning needs git 2.32 or later. The image must provide `git`, and `git-lfs` when the commit has LFS files.
- `context`: hard secret matches are never uploaded; secret-like names are uploaded only when `include` names them, and both appear in the plan's `excluded` list. Anyone who can read the job can read its context.
- Every source: the rendered command runs `<prelude> || exit $?` and then the command. The prelude delivers the code, creates `output_dir`, and enters `workdir`; if a step fails, including a workdir that resolves outside the code root, it prints one line starting with `compute:` and no user statement runs.

## Plan binding

`compute_plan` pins the revision to a full commit, applies the policy, and dry-runs the exact request on the master; nothing is created and no key is bound. It returns the resolved `spec` (commit pinned; `pool`, `slots`, and `image` explicit), a new `request_id`, the master's `request_digest`, the `commit` and `content_digest` (`unpinned` for `path`), a `code` summary, the `effective_config` as observed now, `warnings` such as `path_not_bind_mounted`, `lfs_required`, `secret_like_included`, or `current_slots_exceeded`, and `placement: not evaluated`.

`compute_launch` renders the returned spec again and creates the job with `request_id` as its idempotency key, bound to `request_digest`. The digest covers the request and its code: the rendered config, the `context` file manifest, the workspace, and the project; for `path` only the directory string. Master and pool defaults are not bound and apply as they stand at launch. The launch returns `job_id`, `request_id`, `replayed`, `outcome`, `submitted_at`, and the job's `state` with an `explanation`; an active experiment whose trials wait for resources reads `running`.

## Shared storage

Every runtime dependency and output must be reachable through a policy mapping, which the administrator also binds on every agent:

```yaml
mounts:
  - host_path: /workspace/<user>
    container_path: /run/determined/workdir/home
```

Cluster host paths need not be mounted on the MCP client machine. Translate paths by replacing the matching host prefix with its container prefix. Do not assume that old image names, pool names, master addresses, or site paths are current; read them from the deployment's policy or the user.

Storage access is a separate file passed to the server with `--storage-config`. Its `mode` is `auto`, `local`, or `ssh`; `local_mounts` maps a policy host root to a local path, and `ssh.host` names a login-node alias from the SSH config. Without the file, the service uses a local path that matches a policy `host_path`, and a storage call with neither local access nor SSH returns `configuration_required`. The repository's `docs/shared-storage-access.md` covers SSH agent, password, and keyring setup and connection reuse.

When the client does not mount shared storage, use `storage_check`, then preview `storage_sync` or `storage_fetch`. Shared paths use the container namespace. `storage_check` reports a `viewpoint`: the backend and the user it runs as, whose permissions are not the container user's. Execute a transfer only after checking the resolved endpoints and exclusions. Without `overwrite`, existing files and the attributes of existing directories are kept; `overwrite=true` needs `allow_overwrite: true` in the policy, or it returns `overwrite_not_allowed`. Transfers never delete extra destination files.

Keep `preserve_permissions: true` unless a verified mount rejects owner, group, permission, or directory-time preservation. For that mount, an operator may set it to `false`; never switch it automatically. Rsync exit 23 can leave a partial copy, so inspect and fix the cause, preview again, and do not retry blindly.

For a manual local copy, use an explicit destination and avoid `--delete`:

```bash
rsync -a --safe-links \
  --exclude '.git/' \
  --exclude '.local/' \
  --exclude '.cache/' \
  --exclude 'cache/' \
  --exclude '.env' \
  --exclude '*.env' \
  --exclude '.env.*' \
  --exclude '*.env.*' \
  --exclude '.determined_compute.env' \
  --exclude '.secrets*' \
  --exclude '.ssh/' \
  --exclude '.aws/' \
  --exclude '.config/gcloud/' \
  --exclude '.netrc' \
  --exclude '.npmrc' \
  --exclude '.pypirc' \
  --exclude '.venv/' \
  --exclude '*.pem' \
  --exclude '*.key' \
  --exclude 'id_ed25519' \
  --exclude 'id_rsa' \
  --exclude '.credentials/' \
  --exclude 'credentials/' \
  --exclude '*credentials*' \
  --exclude '*token*' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude '*.pyc' \
  <source>/ <shared-task-directory>/repo/
```

Review project-specific secret filenames before copying. A `context` upload applies its own secret rules and lists what it left out as `excluded`; review that list in the plan.

## Errors and failures

A failed tool call returns `{"error": {"code", "message", "retryable", "details"}}`.

| Code | Meaning | What to do |
| --- | --- | --- |
| `unavailable`, `invalid_response` on a launch | The outcome is unknown | Repeat the identical launch, or find the `request_id` in `compute_list` |
| `internal` on a launch | The job may exist | Repeat the identical launch once; the same error again means nothing was created |
| `plan_changed` | The code or request moved since the plan; nothing was created | Plan again and review `details.commit` |
| `key_conflict` | The `request_id` names the job in `details.job_id`, whose request differs | Read that job with `compute_status`; otherwise plan again |
| `admission_unsupported` | `admission: immediate`; nothing was created | Use `queue` |
| `protocol_unsupported` | The master lacks submission protocol 1 | Stop and report: the master needs upgrading |
| `invalid_request` | The spec, an argument, or the master's validation refused the request | Fix the request and plan again |
| `pool_not_allowed`, `slots_exceed_limit`, `path_not_mounted`, `read_only_storage` | The policy refused the request | Stop and report, or choose within the policy with the user |
| `storage_not_local`, `commit_not_on_ref`, `lfs_object_missing`, `context_too_large`, `unsafe_symlink`, and other code checks | Code planning refused the source | Fix the repository or the code fields |
| `not_found`, `permission_denied` | The job or resource is missing, or the account may not use it | Check the `job_id` and the account |

Never plan a new `request_id` while a launch outcome is uncertain, and never relaunch automatically after a failure, a cancel, or `plan_changed`. Repeating the identical launch is safe: the master keeps every job under its `request_id` and returns it with `replayed: true`, even if the working tree changed since.

A job's `exit_class` says why it ended: `workload_failed` for a workload error, including a failed prelude that prints a `compute:` line in the logs, for example when git code delivery fails or the workdir leaves the code root; `workload_initialization_failed` when the container failed before the workload started; `infrastructure_failed` when an agent or its connection was lost; `none` when it completed or was cancelled. An ended state is not success: check the logs, the expected outputs, and the success criteria, and report a failure as a failure.

`compute_usage(job_id)` reads measured CPU, memory, and GPU use; `measurement: "unmeasured"` means the master has no task-resources integration, not an idle job. A null value is a missing measurement, never zero, and GPU metrics cover the whole device. Inspect its `warnings` before concluding that a job is underusing or saturating its resources. Use its `gpus` entries (`utilization_spread_percent`, `least_utilized_gpu_uuid`, `idle_fraction`, and `gpu_count` against `requested_slots`) to spot idle or straggling GPUs, and treat an experiment's `trial.batches_per_second_lower_bound` as a lifetime floor; a `total_batches_processed` of 0 is expected when the workload does not report through Determined's Core API (or has not reported yet), and does not mean it made no progress.

Authentication failure is a configuration failure. Do not run the workload locally as a fallback and do not expose credentials while diagnosing it.

Logs and reports may contain commands, paths, IDs, states, and sanitized errors. They must not include tokens, passwords, authorization headers, environment-file contents, or copied secret values.

## Shell lifetime

A deployment may reclaim inactive shells through an external scheduler or watchdog; consult the cluster policy for the actual inactivity definition and enforcement. Save work on mapped shared storage so it survives shell termination.
