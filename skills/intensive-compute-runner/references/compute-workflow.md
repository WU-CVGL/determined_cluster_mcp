# Compute workflow reference

Load this reference when preparing a service request, copying a workspace to shared storage, or diagnosing an uncertain launch.

## Request fields

`ComputeService.plan` and `ComputeService.launch` accept a request mapping with these fields:

| Field | Meaning |
| --- | --- |
| `kind` | `auto`, `command`, `shell`, or `experiment` |
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
| `create_directories` | `output_dir` and/or `checkpoint_storage` to create on shared storage before submission |
| `gpu_admission` | Optional in-container GPU policy (`count`, `names`, `driver_versions`, `min_free_mib`, `min_total_mib`, `receipt`) checked before the workload |

Auto mode otherwise resolves to `command`. Call `plan` before `launch`; planning is read-only. Through the CLI or MCP server, the plan's `path_checks` shows whether bind mounts, `workdir`, an experiment checkpoint directory, and `output_dir` exist; `unverified` means the service cannot see the path, and a missing required path fails with `path_not_found`. An experiment checkpoint `host_path` is bind-mounted when the container starts, so create it first or request `create_directories: ["checkpoint_storage"]`.

With `gpu_admission`, the task writes a JSON receipt and a `.jsonl` history under `output_dir` and exits 86 before the workload when the GPUs that NVML reports inside the container (not filtered by `CUDA_VISIBLE_DEVICES`) do not match the policy; the task image needs `python3` and the `nvidia-ml-py` package. A multi-slot experiment needs `experiment_config.resources.is_single_node: true`. Exit code 86 alone is a hint, since the workload can exit 86 too; confirm by the `determined-compute gpu_admission: failed` log line or the `.jsonl` record whose `determined.allocation_id` matches, because the `.json` receipt is overwritten by any trial or task sharing `output_dir`. In an experiment each failure consumes a restart; use `max_restarts: 0` to fail once.

Call `compute_resources(slots=1, pool=None)` with the requested values before launch; `slots=0` checks auxiliary capacity. With `allow_queue: false`, launch admits a new request only when capacity is known and currently sufficient; a rejection creates no task or remote submission. `allow_queue: true` explicitly permits scheduler queueing for that request. Capacity is a race-prone snapshot rather than a reservation, and the service never switches pools or execution locations automatically.

## Shared storage

Every runtime dependency and output must be reachable through a profile mapping:

```yaml
mounts:
  - host_path: /workspace/<user>
    container_path: /run/determined/workdir/home
```

Configured shared roots may include `/SSD`, `/SSD_home`, `/SSD_datasets`, `/SSD3`, `/SSD3_home`, `/SSD3_datasets`, and `/UNSAFE_SSD4`. Cluster host paths need not be mounted on the MCP client machine.

Translate paths by replacing the matching host prefix with its container prefix. Do not assume that old image names, pool names, master addresses, or site paths are current; read them from the deployment profile or the user.

For an unattended or durable run, publish the exact revision with `storage_snapshot(repo_dir, revision)` (CLI: `determined-compute snapshot REPO_DIR --revision REV`) when the storage configuration sets `snapshots.root`. Preview first, review `excluded` and `warnings`, then publish with `dry_run=false` (`--execute`) and copy `request_fields.workdir` and `request_fields.code_revision` into the request. The snapshot reads tracked files from git, reuses an existing tree when the content is identical, and is read-only, so write results under `output_dir`; add generated or untracked inputs with `include`. With the default `link_mode: auto` on storage without reflink support, each new tree is a full copy; identical files are stored once only with reflink or an explicitly configured `hardlink`, which shares inodes across trees. Without a snapshot root, check out the exact revision into a revision-specific directory such as `/workspace/<user>/compute/runs/<project>/<revision>/repo` and record the revision in the request. Reserve a mutable directory such as `/workspace/<user>/compute/debug/<project>` for interactive shells.

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
  --exclude '*credential*' \
  --exclude 'token' \
  --exclude '.token' \
  --exclude '*.token' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude '*.pyc' \
  <source>/ <shared-task-directory>/repo/
```

Review project-specific secret filenames, such as `secrets.yaml`, and exclude them before copying; `*credential*` also leaves out source files such as `test_credentials_parser.py`, so copy those separately if the workload needs them. Never use an experiment `modelDefinition`, project archive, or upload option; the Determined payload should contain mapped paths only.

## Failure handling

`request_id` makes a known launch retry idempotent. It does not justify resubmitting after an unknown outcome. If submission times out, retain the local record and call `compute_reconcile` only with a verified remote ID. The service will require the remote submission marker to match. If there is no trustworthy link, report the task as uncertain and require investigation before another launch.

`compute_usage(task_id)` reads measured CPU, memory, and GPU use when the Determined master provides task resources; `task_resources_disabled` or `task_resources_unsupported` means no measurements, not an idle task. A null value is a missing measurement, never zero, and GPU metrics cover the whole device. Inspect its `warnings` before concluding that a job is underusing or saturating its resources. Use its `gpus` entries (`utilization_spread_percent`, `least_utilized_gpu_uuid`, `idle_fraction`, and `gpu_count` against `requested_slots`) to spot idle or straggling GPUs, and treat an experiment's `trial.batches_per_second_lower_bound` as a lifetime floor; a `total_batches_processed` of 0 is expected when the workload does not report through Determined's Core API (or has not reported yet), and does not mean it made no progress.

Authentication failure is a configuration failure. Do not run the workload locally as a fallback and do not expose credentials while diagnosing it.

Logs and reports may contain commands, paths, IDs, states, and sanitized errors. They must not include tokens, passwords, authorization headers, environment-file contents, or copied secret values.

## Shell lifetime

A deployment may reclaim inactive shells through an external scheduler or watchdog. Read the configured `shell_inactivity_seconds` as an advisory and consult the cluster policy for the actual inactivity definition and enforcement. Save work on mapped shared storage so it survives shell termination.
