# Local workstation runs

## Contents

- When a local run is allowed
- Before launching
- Run
- Record and report
- Compare with care

## When a local run is allowed

Run on the local workstation only when the user has authorized it for the work at hand, and only for a short single-GPU job whose cluster slot would sit mostly idle (minutes of GPU time, one process). Training, RAM-heavy work and anything that wants many parallel CPU processes stay on the cluster; bulk CPU work goes there as a zero-slot task. An authentication or capacity problem on the cluster is never a reason to run locally.

## Before launching

Copy this checklist and check it off:

```
Local run:
- [ ] The user authorized a local run for this work
- [ ] nvidia-smi: the chosen GPU's other users and free memory are known and leave room for the job
- [ ] MemAvailable (/proc/meminfo) exceeds the container memory limit plus headroom for the host
- [ ] No other local run container is active (one at a time)
- [ ] The request's image is present locally and its workdir and output_dir are on the mounted shared storage
```

The workstation's own services (model servers, browsers, the desktop) keep their memory; the headroom is for them. If the check fails, submit to the cluster instead.

## Run

`scripts/run_local.sh <request.json> <gpu-uuid> [--memory <limit>] [--output-dir DIR] [--dry]` runs the request's command in the request's image with:

- the shared storage mounted at the same path as on the cluster, so the command's paths resolve unchanged;
- the request's environment variables, plus a `DET_EXPERIMENT_ID` of the form `local-<unix seconds>` for the command's cache paths;
- a hard `--memory` limit with swap disabled, so an overrun stops the container and not the host;
- a memory sampler that logs `MemAvailable` while the job runs and stops the container when it falls under a floor;
- `--dry`, which prints the docker command and the launch record and runs nothing.

It refuses a non-empty output directory (outputs are write-once), a missing image, an unknown GPU UUID, and a second concurrent run.

## Record and report

The runner writes `<output_dir>/local-launch.json` before the run (host, GPU UUID, name and driver, image reference and local image id, the request file and its sha256, the request id, start time, the docker command) and completes it afterwards (end time, exit code, peak memory). This record takes the place of a Determined ID. Keep the outputs where a cluster job would put them, so the same readers and evidence records apply, and report the record's fields the way a cluster launch is reported.

## Compare with care

A local card is a different class from the cluster's cards. Never compare local rows bitwise with cluster rows; label every cross-card comparison as such; when a comparison needs the same card, run the matched control locally too.
