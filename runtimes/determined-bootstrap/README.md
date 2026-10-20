# Determined bootstrap layer

[English](README.md) | [简体中文](README.zh.md)

This versioned deployment recipe adds the dependency closure of the actual service-injected Determined 0.42.1 wheel to an independent `/usr/local/bin/python`. It keeps the scientific environment's complete package versions unchanged. The service supplies the SDK wheel under `/opt/determined/wheels`; the image does not embed it. `bootstrap.txt` freezes its 67-package dependency closure, including pip 26.2.1 and requests 2.31.0. Updating the service SDK requires a new reviewed closure and admission run.

Build from a committed recipe revision and an immutable, externally declared scientific image reference. Declare the scientific profile and its source revision separately for each image. The scientific interpreter must disable user-site, and `/usr/local/bin/python` must enable user-site and be independent of that environment. The wrapper sets `PIP_BREAK_SYSTEM_PACKAGES=1` only for its child. It never activates or installs into the scientific environment.

```sh
timeout 20m docker build --cpus 8 --memory 16g \
  --build-arg BASE_IMAGE=SCIENTIFIC_IMAGE \
  --build-arg SCIENCE_PROFILE=SCIENTIFIC_PROFILE \
  --build-arg SCIENCE_REVISION=SCIENTIFIC_SOURCE_REVISION \
  --build-arg SDK_VERSION=0.42.1 \
  --build-arg SCIENCE_PYTHON=/opt/venv/bin/python \
  --tag NEW_RUNTIME_IMAGE .
```

Before building, refuse an existing output root or image tag and record the exact inputs in a new output root. Redirect the build log there and record its terminal exit result. The command limits the build to 20 minutes, 8 CPUs and 16 GiB. A failed attempt requires a new output directory and tag. Use a Docker daemon that enforces these build limits; daemon-side cancellation and cleanup remain the operator's responsibility after an interrupted client. Importing the admission program performs no build or cluster submission.

Before accepting an image, run `admit.py sdk /opt/determined/wheels` through `/usr/local/bin/bm-bootstrap-python` as the actual non-root job UID, including a UID absent from passwd. Supply `LOGNAME=bm-runtime`, a fresh job-owned `PYTHONUSERBASE`, and job-owned `XDG_CACHE_HOME`, `PIP_CACHE_DIR`, and `CUDA_CACHE_PATH`. Do not override HOME globally. Keep TMPDIR short (for example `/tmp`) because CUDA compilation paths have length limits. The SDK check installs only the injected wheel offline into user-site, checks its dependency closure and imports it. It does not change the image. Retain its output beside the job. Recheck scientific versions using the scientific interpreter's `admit.py compare /opt/determined-bootstrap/science-before.json` in that same job environment. GPU simulation acceptance is a separate task.

The build equality check covers installed distribution versions. It does not establish native simulator compatibility; scientific profile admission must also verify its interpreter, Torch/CUDA, simulator imports, and actual execution. Do not accept a profile based only on this SDK check.
