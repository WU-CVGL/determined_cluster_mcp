# Local workstation runs

## Contents

- When a local run is allowed
- Before launching
- Running `scripts/run_local.sh`
- Refusals
- What a run does
- The launch record
- Host driver libraries
- Image CUDA archs
- Exit codes and the ledger
- Comparing across cards

## When a local run is allowed

Run on the local workstation only when the user has authorized a local run for the work at hand, and only for a short single-GPU job whose cluster slot would mostly sit idle (minutes of GPU time, one process). Training, RAM-heavy work and anything that wants many parallel CPU processes stay on the cluster; bulk CPU work goes there as a zero-slot task. A cluster authentication or capacity problem is never a reason to run locally. One local container runs at a time, and the GPU's other processes are never touched.

## Before launching

Copy this checklist and check it off:

```
Local run:
- [ ] The user authorized a local run for this work
- [ ] nvidia-smi: the chosen GPU's other users and free memory are known and leave room for the job
- [ ] MemAvailable (/proc/meminfo) exceeds the container memory limit plus the floor
- [ ] No other local run is active (docker ps --filter label=run-local)
- [ ] The image is present locally by digest; the workdir and the job's OUT lie under a --shared-root
- [ ] The request carries a fresh `Request <id>.` token and an unused output directory
- [ ] `--dry` exits 0 and its record shows the expected GPU, CUDA-arch match, CPU and memory limits
```

The workstation's own services keep their memory and CPUs; the floor and the CPU limit leave room for them. If a check fails, submit to the cluster instead.

## Running `scripts/run_local.sh`

`run_local.sh <request.json> <gpu-uuid> [options]` runs the request's command (the request JSON the compute service would receive) in the request's image, in the foreground. It needs docker with the NVIDIA runtime registered as `nvidia` (NVIDIA Container Toolkit in CDI mode), `nvidia-smi` and `python3`. When the agent's shell tool has a time limit shorter than the job, start the runner in the background and poll its record: a runner killed with SIGKILL cannot stop its container.

- `--memory 16g`: hard container limit with swap disabled (`--memory-swap` equals it). `/dev/shm` use counts against it; the request's `shm_size` is only a ceiling.
- `--cpus N`: docker CPU limit. The default is half the host's CPUs, rounded down, at least 4, so one container never saturates the workstation.
- `--mem-floor-gb 12`: refuse when MemAvailable is below the limit plus the floor; stop the container when it falls below the floor during the run.
- `--min-gpu-free-gb 6`: refuse when the GPU has less memory free.
- `--shared-root DIR` (repeatable, required at least once): each root is mounted at its own path, as the compute service mounts shared storage at the request's paths; the workdir and the job's OUT must lie under one. Take the roots from the compute profile's `mounts`.
- `--record-env-prefix PREFIX` (repeatable, default none): request variables with these name prefixes pass by value in the docker argv and the record. Others pass through the docker CLI's environment, so only their names are recorded; the request file (recorded by path and sha256) holds the values.
- `--keep-image-driver-libs`: keep the image's LD_LIBRARY_PATH (see Host driver libraries).
- `--output-dir DIR`: smoke tests only. It moves `local-launch.json` and the `local-*.log` files, but the job still writes to its own `OUT=`.
- `--dry`: prints the docker command and the launch record. It starts no GPU container; an uncached image gets its one GPU-less arch probe.
- `RUN_LOCAL_SCRATCH`: holds the lock, the container-id files and the arch cache. The default is `${XDG_RUNTIME_DIR:-/tmp}/run_local-<uid>`, which must belong to you. Nothing in it is a run record.

## Refusals

These checks refuse with exit 2 in both modes, and nothing is started or written:
- The request is unreadable, lacks `image`/`workdir`/`output_dir`/`command`, has a non-argv command, a non-digest image, a malformed environment entry, no positive `shm_size`, or no `Request <id>.` token.
- A shared root is not an existing absolute directory, the workdir is not under one, or `--cpus` is outside 0 < N ≤ the host's CPUs.
- The output directory exists and is not empty (outputs are write-once).
- The image is not present locally, the GPU UUID is not in `nvidia-smi -L`, the GPU has too little free memory, or MemAvailable is below the limit plus the floor.
- A `run-local` container is already running.
- The image's torch has no code that runs on the GPU's arch (see Image CUDA archs).

These checks refuse only a real run (`--dry` prints them and exits 3):
- The job's `OUT=` differs from `--output-dir`, or the job's OUT is not empty.
- The job's OUT lies outside every shared root, so its files would vanish with the container.
- Another `run_local.sh` holds the lock.

## What a run does

1. It creates the output directory with mode 0777 (cluster output dirs are 0777, and NFS squashes the container's root user) and writes `local-launch.json`.
2. It runs `docker run --rm --pull never --runtime nvidia` with `NVIDIA_VISIBLE_DEVICES=runtime.nvidia.com/gpu=<uuid>` in place of the request's value. The toolkit's just-in-time CDI device is built from the live driver; `--gpus` is refused in CDI mode, and static CDI specs go stale. The run also sets:
   - `--entrypoint ''` (Determined replaces the image entrypoint) and `-w <workdir>`;
   - `DET_EXPERIMENT_ID=local-<unix s>` and `DET_TRIAL_ID=0`, with the request's `DET_*` values dropped;
   - `COMPUTE_CODE_REVISION`, `COMPUTE_WORKDIR` and `COMPUTE_OUTPUT_DIR`, as the service sets them;
   - the labels `run-local` and `run-local.request=<id>`.
3. It tees the container's output to `local-run.log`; the job keeps its own logs under its OUT.
4. Every 5 s it appends to `local-memory.log`: host MemAvailable, the container's cgroup `memory.current`/`memory.peak`, and the GPU's memory and utilization. It stops the container when MemAvailable falls below the floor. Once the container exists, it reads the applied limits with `docker inspect`.
5. SIGINT, SIGTERM or SIGHUP runs `docker stop --time 45` on its own container only; a second signal kills it.
6. It completes the record, and leaves no container behind (`--rm`).

## The launch record

`<output_dir>/local-launch.json` takes the place of a Determined ID. Report its fields the way a cluster launch is reported, and keep it with the job's outputs as run evidence.
- **Written before the start:** `launched_by`, `mode`, `host`, `gpu` (UUID, name, driver, compute capability, memory in use, PCI bus), `gpu_request`, `card_class`, `image` (reference, local image id, tags), `cuda_arch`, `request` (path, sha256, request id, name, code revision, workdir, output_dir, cluster pool, command sha256), `output_dir`, `job_out`, `shared_roots`, `det_experiment_id`, `container_name`, `shm_size_bytes`, `memory` (limit, swap, floor, MemAvailable at launch), `cpus` (limit, host CPUs, source), `driver_libraries`, `environment` (names, recorded values, substitutions, dropped names, service values), `docker_argv`, `notes`, `start_utc`.
- **Added at the end:** `end_utc`, `exit_code`, `container_id`, `duration_s`, `interrupted_by`, `low_memory_stop`, `container_inspect` (Runtime, NanoCpus, Memory, MemorySwap, ShmSize, Binds and LD_LIBRARY_PATH as docker applied them), and `usage` (container memory peak, host MemAvailable minimum, and the GPU's peak memory and mean utilization, both whole-device).

## Host driver libraries

The NVIDIA runtime mounts the host's user-mode driver libraries into the container at their host paths. Some images bundle their own copies in a different directory, for example a `libnvidia-gl` package under `/usr/lib/x86_64-linux-gnu`. When the host keeps its driver elsewhere (for example `/usr/lib`), those bundled copies shadow the mounted ones. The loader then mixes two driver versions, Vulkan fails with `ERROR_INCOMPATIBLE_DRIVER`, and renderers such as Isaac Sim hang at startup.

By default the runner sets `LD_LIBRARY_PATH` to the host's driver directory (where the host's ldconfig cache finds `libcuda.so.1`, else `/usr/lib`), followed by a base path. The base is the request's LD_LIBRARY_PATH, else the image's own (read from the image config), else `/usr/local/cuda/lib64`. `--keep-image-driver-libs` turns this off. `driver_libraries` records the choice, and `container_inspect` records the value docker applied. To confirm it inside a job, run `vulkaninfo --summary 2>&1 | grep -E 'deviceName|driverInfo|ERROR'`: it should list the GPU with the host's driver version and no ERROR line.

## Image CUDA archs

The first time an image is used, the runner starts it once without a GPU (`--runtime runc --network none`) and reads `torch._C._cuda_getArchFlags()`. It caches the answer under the scratch directory by local image id. The image id is the hash of the image config, so a cache entry cannot describe another image, whereas a tag can be repointed; delete the entry to re-probe.

- `sm_XY` is a binary for compute capability X.Y. It also runs on later minor versions of the same major, never on another major.
- `compute_XY` is PTX, which the driver JIT-compiles for any capability ≥ X.Y.

A GPU covered by neither is refused, because the image's CUDA kernels cannot run on it. A failed probe, for example an image without torch, adds a note but does not refuse. `cuda_arch` records the flags, the torch version and the entry that matched.

## Exit codes and the ledger

- `2`: refused; nothing started or written.
- `--dry`: `0` when the run would launch; `3` when only a real-run check would refuse it.
- **Real run:** the container's exit code.
  - `0` means success.
  - `124` is the job's own `timeout`.
  - `137` means killed, for example at `--memory`; check `local-memory.log` and `low_memory_stop`.
  - `143` means stopped: `interrupted_by` names a signal, and `low_memory_stop` records a stop at the floor.
  - `125` means docker could not start the container; see `local-run.log`.
- The exit code covers the container, not each step. A job that appends one ledger line per step (for example `$OUT/ledger.txt`) shows an OOM kill of one step as that step's `exit 137` line while the container goes on and may exit 0. Read the ledger and each step's outputs before calling a run complete. A ledger line records the step's exit status only; check the step's own evidence.

## Comparing across cards

A local card is a different class from the cluster's cards, and local timings are not cluster timings. Never compare local results bitwise with cluster results, and label every cross-card comparison as such. When a comparison needs the same card, run the matched control locally too.
