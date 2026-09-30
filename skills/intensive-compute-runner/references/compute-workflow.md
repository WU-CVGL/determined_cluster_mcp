# Compute workflow reference

Load this reference when writing a TaskSpec, copying a workspace to shared storage, or diagnosing an uncertain launch.

## TaskSpec fields

`compute_plan(spec)` and `compute_launch(spec, request_id, request_digest)` take a TaskSpec with these fields:

| Field | Meaning |
| --- | --- |
| `kind` | `command`, `shell`, or `experiment` |
| `name` | Short task-specific display name; never an internal ID |
| `command` | Command string for a command or an experiment; a shell has none |
| `code` | `{source: git, repo, revision}`, `{source: context, repo, revision, include, exclude}`, `{source: path, dir}`, or omitted |
| `workdir` | Directory relative to the code root; default `.` |
| `output_dir` | Absolute container path on writable shared storage; created before the command runs and exported as `COMPUTE_OUTPUT_DIR` |
| `slots`, `pool`, `image` | Optional overrides of the policy defaults |
| `env` | Environment variables; the `COMPUTE_` prefix is reserved |
| `workspace`, `project` | Names; an experiment sets both or neither |
| `experiment` | Experiment config; the MCP renders `entrypoint`, pool, slots, image, and environment variables itself |
| `admission` | `queue`, the only supported value |

`compute_plan` pins the revision, applies the policy, and dry-runs the exact request; nothing is created. Launch with the resolved `spec`, `request_id`, and `request_digest` it returned. A search must set `searcher.max_concurrent_trials`. Checkpoints use `checkpoint_storage: {type: shared_fs, storage_path: <relative path>}` and inherit `host_path` from the workspace or master default.

Placement is not evaluated before launch. `compute_resources(pool)` projects the pools and device models as a snapshot; a job waits in the queue until its slots are free, and the service never switches pools or execution locations.

## Shared storage

Every runtime dependency and output must be reachable through a policy mapping:

```yaml
mounts:
  - host_path: /workspace/<user>
    container_path: /run/determined/workdir/home
```

Cluster host paths need not be mounted on the MCP client machine. Translate paths by replacing the matching host prefix with its container prefix. Do not assume that old image names, pool names, master addresses, or site paths are current; read them from the deployment's policy or the user.

For durable runs, prefer `git` code at a commit on a branch or tag, or `context` code, so the plan pins exactly what runs. Planning `git` code needs the repository's root on a local mount of the machine running the MCP server; over SSH it returns `storage_not_local`. Reserve `path` code, which is never pinned, for interactive shells and debugging.

When the client does not mount shared storage, use `storage_check`, then preview `storage_sync` or `storage_fetch`. Shared paths use the container namespace. Execute only after checking the resolved endpoints and exclusions. Without `overwrite`, existing files and the attributes of existing directories are kept. Read [the shared-storage access guide](../../../docs/shared-storage-access.md) for the separate storage config, SSH agent/password/keyring setup, and connection reuse.

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

## Failure handling

The master keeps every job under its `request_id`, so repeating a launch with the same `spec`, `request_id`, and `request_digest` never duplicates it. After a lost answer, a timeout, or `unavailable`, repeat the launch: it returns the job with `replayed: true`, even if the working tree changed. After `internal`, repeat it once; the same error again means nothing was created. `plan_changed` means the content differs from the plan and nothing was created: plan again. `key_conflict` means the `request_id` already names another request's job. Never plan a new `request_id` while an outcome is uncertain; `compute_list` shows every job of the account with its `request_id`.

A job's `exit_class` says why it ended. `workload_failed` includes a failed prelude, which prints a line starting with `compute:` in the logs, for example when git code delivery fails or the workdir leaves the code root.

`compute_usage(job_id)` reads measured CPU, memory, and GPU use; `measurement: "unmeasured"` means the master has no task-resources integration, not an idle job. A null value is a missing measurement, never zero, and GPU metrics cover the whole device. Inspect its `warnings` before concluding that a job is underusing or saturating its resources. Use its `gpus` entries (`utilization_spread_percent`, `least_utilized_gpu_uuid`, `idle_fraction`, and `gpu_count` against `requested_slots`) to spot idle or straggling GPUs, and treat an experiment's `trial.batches_per_second_lower_bound` as a lifetime floor; a `total_batches_processed` of 0 is expected when the workload does not report through Determined's Core API (or has not reported yet), and does not mean it made no progress.

Authentication failure is a configuration failure. Do not run the workload locally as a fallback and do not expose credentials while diagnosing it.

Logs and reports may contain commands, paths, IDs, states, and sanitized errors. They must not include tokens, passwords, authorization headers, environment-file contents, or copied secret values.

## Shell lifetime

A deployment may reclaim inactive shells through an external scheduler or watchdog; consult the cluster policy for the actual inactivity definition and enforcement. Save work on mapped shared storage so it survives shell termination.
