# Determined bootstrap layer

[English](README.md) | [简体中文](README.zh.md)

This versioned deployment recipe adds the dependency closure of the actual service-injected Determined 0.42.1 wheel to an independent `/usr/local/bin/python`. It keeps the scientific environment's complete package versions unchanged. The service supplies the SDK wheel under `/opt/determined/wheels`; the image does not embed it. `bootstrap.txt` freezes its 67-package dependency closure, including pip 26.2.1 and requests 2.31.0. Updating the service SDK requires a new reviewed closure and admission run.

Build from a committed recipe revision and an immutable, externally declared scientific image reference. Declare the scientific profile and its source revision separately for each image. The scientific interpreter must disable user-site, and `/usr/local/bin/python` must enable user-site and be independent of that environment. The wrapper sets `PIP_BREAK_SYSTEM_PACKAGES=1` only for its child. It never activates or installs into the scientific environment.

```sh
docker buildx create --name NEW_BUILDER --driver docker-container \
  --driver-opt memory=16g,cpu-period=100000,cpu-quota=800000 --bootstrap
docker inspect buildx_buildkit_NEW_BUILDER0 \
  --format '{{.HostConfig.Memory}} {{.HostConfig.CpuPeriod}} {{.HostConfig.CpuQuota}}'
timeout 20m docker buildx build --builder NEW_BUILDER --load \
  --build-arg BASE_IMAGE=SCIENTIFIC_IMAGE \
  --build-arg SCIENCE_PROFILE=SCIENTIFIC_PROFILE \
  --build-arg SCIENCE_REVISION=SCIENTIFIC_SOURCE_REVISION \
  --build-arg SDK_VERSION=0.42.1 \
  --build-arg SCIENCE_PYTHON=/opt/venv/bin/python \
  --tag NEW_RUNTIME_IMAGE .
```

Before building, refuse an existing output root or image tag and record the exact inputs in a new output root. Redirect the build log there and record its terminal exit result. Use a new builder name for each attempt. Before starting the build, inspect its container and require memory=17179869184, cpu-period=100000 and cpu-quota=800000; these configure a 16 GiB memory limit and an 8-CPU quota for the dedicated builder. The timeout bounds the client to 20 minutes. These are requested settings until the inspection confirms them. A failed attempt requires a new output directory and tag. Use a Docker daemon that enforces these build limits; after success, failure or client timeout, stop and remove the dedicated builder with `docker buildx rm NEW_BUILDER` and verify its container has stopped. Do not assume a timed-out client stopped daemon-side work. Deployment-specific CPU affinity, networking and proxy settings belong in the external execution configuration. Importing the admission program performs no build or cluster submission.

Before accepting an image, run `admit.py sdk /opt/determined/wheels` through `/usr/local/bin/bm-bootstrap-python` as the actual non-root job UID, including a UID absent from passwd. Supply `LOGNAME=bm-runtime`, a fresh job-owned `PYTHONUSERBASE`, and job-owned `XDG_CACHE_HOME`, `PIP_CACHE_DIR`, and `CUDA_CACHE_PATH`. Do not override HOME globally. Keep TMPDIR short (for example `/tmp`) because CUDA compilation paths have length limits. The SDK check installs only the injected wheel offline into user-site, checks its dependency closure, imports it, and runs `python -m determined.exec.prep_container --help` with a 60-second bound under that same UID and bootstrap environment. In the declared SDK, argparse exits for `--help` before reading task configuration or authenticating to the server. It does not change the image. Retain its output beside the job. Recheck scientific versions using the scientific interpreter's `admit.py compare /opt/determined-bootstrap/science-before.json` in that same job environment. GPU simulation acceptance is a separate task.

The build equality check covers installed distribution versions. It does not establish native simulator compatibility; scientific profile admission must also verify its interpreter, Torch/CUDA, simulator imports, and actual execution. Do not accept a profile based only on this SDK check.
