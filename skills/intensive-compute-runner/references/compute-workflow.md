# Compute workflow reference

Load this reference when preparing a service request, copying a workspace to shared storage, or diagnosing an uncertain launch.

## Request fields

`ComputeService.plan` and `ComputeService.launch` accept a request mapping with these fields:

| Field | Meaning |
| --- | --- |
| `kind` | `auto`, `command`, `shell`, `generic`, or `experiment` |
| `name` | Short task-specific display name; never an internal ID |
| `description` | Purpose, config, or other useful human context |
| `interactive` | Selects `shell` when `kind` is `auto` |
| `overnight` | Selects `experiment` when `kind` is `auto` |
| `allow_queue` | Per-call queue opt-in; defaults to `false` |
| `command` | Command string or argument list |
| `workdir` | Absolute container path covered by a configured mount |
| `output_dir` | Absolute container path covered by a configured mount |
| `slots` | Requested slot count |
| `pool`, `image` | Optional overrides of profile defaults |
| `code_revision` | Stable revision or content identifier for reproducibility |
| `experiment_config` | Experiment-only configuration; selects `experiment` in auto mode |
| `parent`, `inherit_context`, `pausable`, `preemption_timeout` | Generic-only options; `pausable` defaults to `false`; see the compute reference |

Auto mode otherwise resolves to `command` and never selects `generic`. Call `plan` before `launch`; planning is read-only.

Call `compute_resources(slots=1, pool=None)` with the requested values before launch; `slots=0` checks auxiliary capacity. With `allow_queue: false`, launch admits a new request only when capacity is known and currently sufficient; a rejection submits nothing. `allow_queue: true` explicitly permits scheduler queueing for that request. Capacity is a race-prone snapshot rather than a reservation, and the service never switches pools or execution locations automatically.

## Shared storage

Every runtime dependency and output must be reachable through a profile mapping:

```yaml
mounts:
  - host_path: /workspace/<user>
    container_path: /run/determined/workdir/home
```

Configured shared roots may include `/SSD`, `/SSD_home`, `/SSD_datasets`, `/SSD3`, `/SSD3_home`, `/SSD3_datasets`, and `/UNSAFE_SSD4`. Cluster host paths need not be mounted on the MCP client machine.

Translate paths by replacing the matching host prefix with its container prefix. Do not assume that old image names, pool names, master addresses, or site paths are current; read them from the deployment profile or the user.

For an unattended or durable run, copy or check out the exact revision into a revision-specific directory such as `/workspace/<user>/compute/runs/<project>/<revision>/repo`. Record the revision in the request. Reserve a mutable directory such as `/workspace/<user>/compute/debug/<project>` for interactive shells.

When the client does not mount shared storage, use `storage_check`, then preview `storage_sync` or `storage_fetch`. Shared paths use the container namespace. Execute only after checking the resolved endpoints and exclusions. Read [the shared-storage access guide](../../../docs/shared-storage-access.md) for the separate storage config, SSH agent/password/keyring setup, and connection reuse.

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

Review project-specific secret filenames before copying. Never use an experiment `modelDefinition`, project archive, or upload option; the Determined payload should contain mapped paths only.

## Failure handling

Every `compute_launch` call is a new submission, and the service never retries one. If submission times out after the connection opened or the master answers with a server error, the launch returns `submission_uncertain` with `kind` and `submission_marker` in the error details. Call `compute_list(kind, marker=...)` with a small `limit` before anything else: a returned task is the submission, so continue with its ID; if none is found, the task was not created and a new launch is safe. If a duplicate starts anyway, cancel the extra task. A `transport_error` (the master could not be reached at all) means nothing was sent; launching again is safe. Tools act only on tasks owned by the configured account; `ownership_mismatch` or `ownership_unavailable` means the task cannot be managed with these credentials.

`compute_usage(kind, id)` reads measured CPU, memory, and GPU use when the Determined master provides task resources; `task_resources_disabled` or `task_resources_unsupported` means no measurements, not an idle task. A null value is a missing measurement, never zero, and GPU metrics cover the whole device. Inspect its `warnings` before concluding that a job is underusing or saturating its resources. Use its `gpus` entries (`utilization_spread_percent`, `least_utilized_gpu_uuid`, `idle_fraction`, and `gpu_count` against `requested_slots`) to spot idle or straggling GPUs, and treat an experiment's `trial.batches_per_second_lower_bound` as a lifetime floor; a `total_batches_processed` of 0 is expected when the workload does not report through Determined's Core API (or has not reported yet), and does not mean it made no progress.

Authentication failure is a configuration failure. Do not run the workload locally as a fallback and do not expose credentials while diagnosing it.

Logs and reports may contain commands, paths, IDs, states, and sanitized errors. They must not include tokens, passwords, authorization headers, environment-file contents, or copied secret values.

## Shell lifetime

A deployment may reclaim inactive shells through an external scheduler or watchdog. Read the configured `shell_inactivity_seconds` as an advisory and consult the cluster policy for the actual inactivity definition and enforcement. Save work on mapped shared storage so it survives shell termination.
