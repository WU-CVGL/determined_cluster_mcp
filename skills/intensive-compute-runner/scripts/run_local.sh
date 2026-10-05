#!/usr/bin/env bash
# run_local.sh <request.json> <gpu-uuid> [--memory LIMIT] [--cpus N] [--mem-floor-gb N] [--min-gpu-free-gb N]
#              [--shared-root DIR]... [--record-env-prefix PREFIX]... [--keep-image-driver-libs]
#              [--output-dir DIR] [--dry]
#
# Runs one compute-service request JSON (the file the cluster service consumes) in one local Docker container on one
# workstation GPU, in the foreground, for a short single-GPU job the user has authorized to run locally.  A local card
# is a different card class from the cluster's, so local results are never compared bitwise with cluster results.
# The skill's local-runs reference explains when a local run is allowed and how to read its record.
#
# Needs: docker with the NVIDIA runtime registered as "nvidia" (NVIDIA Container Toolkit, CDI mode), nvidia-smi,
# python3 (standard library only).
#
# Exit codes: a real run exits with the container's code (124: the job's own timeout; 137: killed, e.g. at the
# --memory limit; 143: stopped by a signal or the memory floor).  2: refused, in both modes, for the request, image,
# GPU, memory, CUDA-arch and running-container checks (nothing started, nothing written).  --dry: 0 when a real run
# with the same arguments would launch, 3 when only a real-run check would refuse it (the job's OUT differs from
# --output-dir or lies outside every shared root, or the lock is held); the reasons are printed.
# A job that writes one ledger line per step shows an OOM kill of a single step as that step's "exit 137" ledger line
# while the container goes on, so read the job's ledger as well as the exit code.
#
# Environment: RUN_LOCAL_SCRATCH overrides the scratch directory (lock, container-id files, arch-probe cache); the
# default is ${XDG_RUNTIME_DIR:-/tmp}/run_local-<uid>.
set -euo pipefail

PY_SRC=$(cat <<'PY'
import argparse, datetime, fcntl, hashlib, json, os, platform, re, shlex, shutil, signal, socket, subprocess, sys
import threading, time

LAUNCHED_BY = "run_local.sh (local workstation GPU; user-authorized run)"
# Per-user scratch for the lock, the container-id files and the arch-probe cache.  XDG_RUNTIME_DIR is private to the
# user and cleared at logout, so everything here is disposable and none of it is a run record.
SCRATCH = os.environ.get("RUN_LOCAL_SCRATCH") or os.path.join(
    os.environ.get("XDG_RUNTIME_DIR") or "/tmp", f"run_local-{os.getuid()}")
# Default --shared-root: the cluster's shared storage, which the compute service mounts at the request's own paths;
# mounting it here at the same path lets the request's workdir, inputs and OUT resolve unchanged.
DEFAULT_SHARED_ROOT = None  # no deployment default: pass --shared-root for each storage root the job uses
LABEL = "run-local"  # on every container this script starts (runs and arch probes), so only one runs at a time
SAMPLE_S = 5         # memory/GPU sampling interval (s)
ELIDE = 400          # --dry elides argv elements longer than this many characters
GIB = 1 << 30
# Host driver libraries.  The NVIDIA runtime mounts the host's user-mode driver libraries into the container at their
# host paths.  An image that bundles its own copies (e.g. a libnvidia-gl package) in another directory can shadow
# them: the loader then mixes two driver versions and Vulkan/OpenGL fail (vkCreateInstance returns
# ERROR_INCOMPATIBLE_DRIVER, and a renderer such as Isaac Sim hangs at startup).  Putting the host's driver directory
# first in LD_LIBRARY_PATH makes the mounted host libraries win.  That directory is where the host's ldconfig cache
# finds libcuda.so.1; DRIVER_LIBDIR_FALLBACK when it cannot be found.  The rest of the path is the request's
# LD_LIBRARY_PATH, else the image's own (read from the image config, not assumed), else LD_FALLBACK_BASE.
DRIVER_LIBDIR_FALLBACK = "/usr/lib"
LD_FALLBACK_BASE = "/usr/local/cuda/lib64"
# Image CUDA arch probe.  torch carries GPU code only for the archs it was built for:
#   sm_XY       a binary (SASS) for compute capability X.Y; it also runs on X.Z with Z > Y, never on another major;
#               an arch-specific variant such as sm_90a runs on exactly that arch;
#   compute_XY  PTX, which the driver JIT-compiles for any capability >= X.Y, so it also covers newer GPUs.
# The probe runs the image once without a GPU (runc runtime, no network) and caches the answer per local image id.
# The image id is the hash of the image's config, so a cached answer can never describe a different image, whereas a
# tag can be repointed.  A failed probe (e.g. no torch in the image) is a warning, not a refusal.
PROBE_CODE = ("import json, torch; print(json.dumps({'arch_flags': torch._C._cuda_getArchFlags(), "
              "'torch': torch.__version__, 'torch_cuda': torch.version.cuda}))")
PROBE_LIMITS = ["--memory", "4g", "--cpus", "2"]
PROBE_TIMEOUT_S = 300


def refuse(msg):
    print(f"run_local: refused: {msg}", file=sys.stderr)
    sys.exit(2)


def utc_now():  # actual clock
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def occupied(path):
    if not os.path.lexists(path):
        return False
    if not os.path.isdir(path):
        return True
    with os.scandir(path) as it:
        return any(True for _ in it)


def under(path, root):
    p = os.path.normpath(path)
    return p == root or p.startswith(root + "/")


def parse_bytes(s):
    m = re.fullmatch(r"(\d+)([kmgt]?)b?", s.strip().lower())
    if not m or int(m.group(1)) == 0:
        raise argparse.ArgumentTypeError(f"bad size {s!r} (use e.g. 16g)")
    return int(m.group(1)) * {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}[m.group(2)]


def mem_available():
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    return None


def write_json_atomic(path, obj):
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_cid(cidfile):
    try:
        return open(cidfile).read().strip() or None
    except OSError:
        return None


def host_driver_libdir():
    arch = {"x86_64": "x86-64", "aarch64": "AArch64"}.get(platform.machine())
    try:
        out = subprocess.run([shutil.which("ldconfig") or "/sbin/ldconfig", "-p"], capture_output=True, text=True,
                             timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for line in out.splitlines():
        m = re.match(r"\s*libcuda\.so\.1 \(([^)]*)\) => (\S+)", line)
        if m and (arch is None or arch in m.group(1)):
            return os.path.dirname(m.group(2)), "host ldconfig cache (directory of libcuda.so.1)"
    return DRIVER_LIBDIR_FALLBACK, "fallback (libcuda.so.1 not in the host's ldconfig cache)"


def arch_cover(flags, cap):
    """The entry of `flags` whose code runs on compute capability `cap` (e.g. '8.6'), with how, or None."""
    major, minor = (int(x) for x in cap.split("."))
    exact = same_major = ptx = None
    for f in flags:
        m = re.fullmatch(r"(sm|compute)_(\d+)(\d)([a-z]?)", f)
        if not m:
            continue
        kind, fmaj, fmin, variant = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        if kind == "sm" and (fmaj, fmin) == (major, minor):
            exact = f
        elif kind == "sm" and not variant and fmaj == major and fmin < minor:
            same_major = f if same_major is None or f > same_major else same_major
        elif kind == "compute" and not variant and (fmaj, fmin) <= (major, minor):
            ptx = f
    if exact:
        return f"{exact} (binary for this arch)"
    if same_major:
        return f"{same_major} (binary for an earlier arch of the same major)"
    if ptx:
        return f"{ptx} (PTX, JIT-compiled by the driver)"
    return None


def image_arch(docker, image, image_id, may_probe):
    """(cache entry or None, cache path, status)."""
    cache = os.path.join(SCRATCH, "arch-cache", image_id.replace(":", "-") + ".json")
    try:
        with open(cache) as f:
            c = json.load(f)
        if c.get("image_id") == image_id and isinstance(c.get("arch_flags"), list):
            return c, cache, "cached"
    except (OSError, ValueError):
        pass
    if not may_probe:
        return None, cache, "not probed (another run_local.sh holds the lock)"
    name = f"{LABEL}-probe-{int(time.time())}"
    argv = [docker, "run", "--rm", "--pull", "never", "--name", name, "--label", LABEL, "--runtime", "runc",
            "--network", "none", *PROBE_LIMITS, "--entrypoint", "", image, "python", "-c", PROBE_CODE]
    print(f"run_local: probing the CUDA archs of image {image_id} (one GPU-less container)", file=sys.stderr)
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        subprocess.run([docker, "rm", "-f", name], capture_output=True)
        return None, cache, f"probe timed out after {PROBE_TIMEOUT_S} s"
    try:  # torch may print warnings; the answer is the last stdout line
        r = json.loads(p.stdout.strip().splitlines()[-1])
        flags = r["arch_flags"].split()
    except (IndexError, ValueError, KeyError, AttributeError):
        tail = (p.stderr.strip().splitlines() or ["no output"])[-1][:300]
        return None, cache, f"probe failed (exit {p.returncode}: {tail})"
    entry = {"image_id": image_id, "image_reference": image, "arch_flags": flags, "torch": r.get("torch"),
             "torch_cuda": r.get("torch_cuda"), "probed_utc": utc_now(), "probe_argv": argv[1:]}
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    write_json_atomic(cache, entry)
    return entry, cache, "probed"


def main():
    ap = argparse.ArgumentParser(prog="run_local.sh", description="Run one compute-service request locally.")
    ap.add_argument("request")
    ap.add_argument("gpu_uuid")
    ap.add_argument("--memory", type=parse_bytes, default=parse_bytes("16g"),
                    help="hard container memory limit, swap disabled; /dev/shm counts against it (default 16g)")
    ap.add_argument("--cpus", type=float,
                    help="docker --cpus limit (default: half the host's CPUs, rounded down, at least 4)")
    ap.add_argument("--mem-floor-gb", type=float, default=12.0,
                    help="host MemAvailable floor: refuse below limit+floor, stop the container below floor (12)")
    ap.add_argument("--min-gpu-free-gb", type=float, default=6.0, help="refuse when the GPU has less free (6)")
    ap.add_argument("--shared-root", action="append", metavar="DIR",
                    help=f"shared storage mounted at the same path; repeatable (default {DEFAULT_SHARED_ROOT}); "
                         "the request's workdir and the job's OUT must lie under one")
    ap.add_argument("--record-env-prefix", action="append", metavar="PREFIX",
                    help="request environment variables with this name prefix pass by value in the docker argv and "
                         "the record; others pass by name through the docker CLI's environment; repeatable")
    ap.add_argument("--keep-image-driver-libs", action="store_true",
                    help="do not put the host's driver library directory first in LD_LIBRARY_PATH")
    ap.add_argument("--output-dir", help="smoke tests only: where local-launch.json and local-*.log go")
    ap.add_argument("--dry", action="store_true",
                    help="print the docker command and launch record; starts no GPU container (an uncached image "
                         "gets its one GPU-less arch probe)")
    a = ap.parse_args(sys.argv[1:])
    floor = int(a.mem_floor_gb * GIB)

    docker = shutil.which("docker") or refuse("docker is not on PATH; install Docker or fix PATH")
    shutil.which("nvidia-smi") or refuse("nvidia-smi is not on PATH; the NVIDIA driver utilities are needed")
    try:
        os.makedirs(SCRATCH, mode=0o700, exist_ok=True)
        scratch_uid = os.stat(SCRATCH).st_uid
    except OSError as e:
        refuse(f"cannot create the scratch directory {SCRATCH} ({e}); set RUN_LOCAL_SCRATCH to a writable directory")
    if scratch_uid != os.getuid():
        refuse(f"scratch directory {SCRATCH} belongs to uid {scratch_uid}, not to you; set RUN_LOCAL_SCRATCH")

    # ---- request ------------------------------------------------------------------------------------
    req_path = os.path.abspath(a.request)
    try:
        raw = open(req_path, "rb").read()
        req = json.loads(raw)
    except (OSError, ValueError) as e:
        refuse(f"cannot read request {req_path}: {e}")
    req_sha = hashlib.sha256(raw).hexdigest()
    for k in ("image", "workdir", "output_dir", "command"):
        if k not in req:
            refuse(f"request has no '{k}'; pass the request JSON the compute service would receive")
    image, workdir, req_out, command = req["image"], req["workdir"], req["output_dir"], req["command"]
    if not (isinstance(command, list) and command and all(isinstance(x, str) for x in command)):
        refuse("request command is not a non-empty argv list of strings")
    if "@sha256:" not in image:
        refuse(f"image is not a digest reference (name@sha256:...), so the run could not be tied to one image: {image}")
    cfg = req.get("experiment_config") or {}
    env_spec = (cfg.get("environment") or {}).get("environment_variables") or []
    if isinstance(env_spec, dict):  # Determined also allows {cpu: [...], cuda: [...]}
        env_spec = env_spec.get("cuda") or env_spec.get("gpu") or []
    env_items = []
    for e in env_spec:
        if not isinstance(e, str) or "=" not in e or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", e.split("=", 1)[0]):
            refuse(f"bad environment entry {e!r} in experiment_config.environment.environment_variables")
        env_items.append(tuple(e.split("=", 1)))
    shm = (cfg.get("resources") or {}).get("shm_size")
    if not isinstance(shm, int) or shm <= 0:
        refuse(f"experiment_config.resources.shm_size missing or not a positive byte count: {shm!r}")
    ids = re.findall(r"\bRequest (\S+?)\.(?=\s|$)", req.get("description") or "")
    if not ids:
        refuse("description carries no 'Request <id>.' token; add the request id the record and container name use")
    request_id = ids[-1]

    roots = []
    for r in a.shared_root or [DEFAULT_SHARED_ROOT]:
        r = os.path.normpath(r)
        if not os.path.isabs(r) or r == "/" or not os.path.isdir(r):
            refuse(f"--shared-root {r} is not an existing absolute directory other than / "
                   "(is the shared storage mounted?)")
        if r not in roots:
            roots.append(r)
    in_roots = lambda p: any(under(p, r) for r in roots)
    if not os.path.isdir(workdir) or not in_roots(workdir):
        refuse(f"workdir {workdir} is not an existing directory under a shared root ({' '.join(roots)}); "
               "put the code on shared storage or pass --shared-root")

    # the job's own script writes to its hard-coded OUT; --output-dir does not redirect it
    script_out = None
    for x in command:
        m = re.search(r"(?m)^\s*OUT=['\"]?([^\s'\"]+)['\"]?\s*$", x)
        if m:
            script_out = os.path.normpath(m.group(1))
            break
    job_out = script_out or os.path.normpath(req_out)
    eff_out = os.path.normpath(os.path.abspath(a.output_dir or req_out))

    host_cpus = os.cpu_count() or 1
    cpus = a.cpus if a.cpus is not None else min(host_cpus, max(4, host_cpus // 2))
    if not 0 < cpus <= host_cpus:
        refuse(f"--cpus {cpus:g} is outside 0 < N <= {host_cpus} (this host's CPUs)")

    # ---- refusals (checked in both modes) -----------------------------------------------------------
    if occupied(eff_out):
        refuse(f"output_dir exists and is not empty (outputs are write-once); use a new directory: {eff_out}")
    p = subprocess.run([docker, "image", "inspect", image], capture_output=True, text=True)
    if p.returncode != 0:
        refuse(f"image not present locally ({image}): {p.stderr.strip()}; pull it first")
    info = json.loads(p.stdout)[0]
    image_id = info["Id"]
    image_env = dict(e.split("=", 1) for e in ((info.get("Config") or {}).get("Env") or []) if "=" in e)
    p = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True)
    if p.returncode != 0 or f"(UUID: {a.gpu_uuid})" not in p.stdout:
        refuse(f"GPU {a.gpu_uuid} is not listed by nvidia-smi -L; pass a UUID from that list")
    p = subprocess.run(["nvidia-smi", "-i", a.gpu_uuid, "--query-gpu=uuid,name,driver_version,compute_cap,"
                        "memory.total,memory.used,pci.bus_id", "--format=csv,noheader,nounits"],
                       capture_output=True, text=True)
    try:
        f = [s.strip() for s in p.stdout.strip().split(",")]
        gpu = {"uuid": f[0], "name": f[1], "driver": f[2], "compute_cap": f[3], "memory_total_mib": int(f[4]),
               "memory_used_mib_at_launch": int(f[5]), "pci_bus_id": f[6]}
    except (IndexError, ValueError):
        refuse(f"cannot query GPU {a.gpu_uuid} with nvidia-smi (exit {p.returncode}): {p.stderr.strip()[:300]}")
    sm = "sm_" + gpu["compute_cap"].replace(".", "")
    notes = []
    gpu_free_gib = (gpu["memory_total_mib"] - gpu["memory_used_mib_at_launch"]) / 1024
    if gpu_free_gib < a.min_gpu_free_gb:
        refuse(f"{gpu['name']} has {gpu_free_gib:.1f} GiB free (< --min-gpu-free-gb {a.min_gpu_free_gb}); "
               "check its users with nvidia-smi or choose another GPU")
    avail = mem_available()
    if avail is None or avail < a.memory + floor:
        refuse(f"host MemAvailable {(avail or 0) / GIB:.1f} GiB < container limit {a.memory / GIB:.1f} GiB "
               f"+ floor {a.mem_floor_gb} GiB; free memory or run on the cluster")
    p = subprocess.run([docker, "ps", "-q", "--filter", f"label={LABEL}"], capture_output=True, text=True)
    if p.returncode != 0:
        refuse(f"docker ps failed: {p.stderr.strip()[:300]}")
    if p.stdout.strip():
        refuse(f"another {LABEL} container is running ({' '.join(p.stdout.split())}); one local run at a time")

    # real-run-only refusals (the dry run reports them)
    would_refuse = []
    if eff_out != job_out:
        would_refuse.append(f"--output-dir {eff_out} differs from the job's own OUT {job_out}; the job would still "
                            "write there (a smoke test needs its own request whose command's OUT is that directory)")
    if eff_out != job_out and occupied(job_out):
        would_refuse.append(f"the job's OUT exists and is not empty (write-once): {job_out}")
    if not in_roots(job_out):
        would_refuse.append(f"the job's OUT {job_out} is not under a shared root ({' '.join(roots)}), so its files "
                            "would vanish with the container; pass --shared-root for its storage")
    lock = open(os.path.join(SCRATCH, "run_local.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
    except BlockingIOError:
        locked = False
        would_refuse.append(f"another run_local.sh holds the local run lock ({SCRATCH}/run_local.lock)")

    # ---- image CUDA archs (probed under the lock, so the probe container is the only one) ------------
    entry, cache, status = image_arch(docker, image, image_id, locked)
    arch = {"status": status, "cache_file": cache, "gpu_arch": sm,
            "arch_flags": entry and entry["arch_flags"], "torch": entry and entry.get("torch"), "match": None}
    if entry is None:
        notes.append(f"CUDA archs of image {image_id} unknown ({status}); the GPU may not run its torch kernels")
    elif not entry["arch_flags"]:
        notes.append(f"the image's torch reports no CUDA arch flags (a CPU-only build?): {entry.get('torch')}")
    else:
        arch["match"] = arch_cover(entry["arch_flags"], gpu["compute_cap"])
        if arch["match"] is None:
            refuse(f"{gpu['name']} is {sm}; the image's torch ({entry.get('torch')}) carries code for "
                   f"{' '.join(entry['arch_flags'])}: no binary for {sm} or an earlier arch of the same major and no "
                   "PTX (compute_XY) at or below it, so its CUDA kernels cannot run on this GPU; choose another GPU "
                   f"or an image built for {sm} (probe cache: {cache})")
    if a.dry and locked:
        fcntl.flock(lock, fcntl.LOCK_UN)
    elif not a.dry and would_refuse:
        refuse("; ".join(would_refuse))

    # ---- docker command -----------------------------------------------------------------------------
    # GPU selection: with --runtime nvidia, NVIDIA_VISIBLE_DEVICES=runtime.nvidia.com/gpu=<uuid> asks the toolkit's
    # just-in-time CDI kind to build the device edits from the live driver.  In CDI mode --gpus is refused, and a
    # static CDI spec (/etc/cdi) goes stale when cards or UUIDs change.
    gpu_request = f"runtime.nvidia.com/gpu={gpu['uuid']}"
    ts = int(time.time())
    det_exp = f"local-{ts}"
    name = f"{LABEL}-" + re.sub(r"[^A-Za-z0-9_.-]", "_", request_id)[:100] + f"-{ts}"
    cidfile = os.path.join(SCRATCH, "cid", f"{name}.cid")
    prefixes = tuple(a.record_env_prefix or ())
    env = {"names": [], "recorded_prefixes": list(prefixes), "recorded_values": {}, "substitutions": {},
           "dropped": []}
    docker_env = dict(os.environ)
    argv = [docker, "run", "--rm", "--pull", "never", "--name", name, "--label", LABEL,
            "--label", f"{LABEL}.request={request_id}", "--cidfile", cidfile, "--runtime", "nvidia",
            "--shm-size", str(shm), "--memory", str(a.memory), "--memory-swap", str(a.memory), "--cpus", f"{cpus:g}"]
    for r in roots:
        argv += ["-v", f"{r}:{r}"]
    request_ld = None
    for k, v in env_items:
        if k in ("DET_EXPERIMENT_ID", "DET_TRIAL_ID"):
            env["dropped"].append(k)
        elif k == "NVIDIA_VISIBLE_DEVICES":
            env["substitutions"][k] = {"request": v, "local": gpu_request}
        elif k == "LD_LIBRARY_PATH":
            request_ld = v
        else:
            env["names"].append(k)
            if prefixes and k.startswith(prefixes):
                env["recorded_values"][k] = v
                argv += ["-e", f"{k}={v}"]
            else:  # value handed to the docker CLI through its environment, so the argv records the name only
                docker_env[k] = v
                argv += ["-e", k]
    env["substitutions"].setdefault("NVIDIA_VISIBLE_DEVICES", {"request": None, "local": gpu_request})
    env["names"] += ["NVIDIA_VISIBLE_DEVICES", "DET_EXPERIMENT_ID", "DET_TRIAL_ID"]
    argv += ["-e", f"NVIDIA_VISIBLE_DEVICES={gpu_request}", "-e", f"DET_EXPERIMENT_ID={det_exp}", "-e", "DET_TRIAL_ID=0"]

    image_ld = image_env.get("LD_LIBRARY_PATH")
    if a.keep_image_driver_libs:
        container_ld = request_ld if request_ld is not None else image_ld
        driver_libs = {"mode": "image libraries as shipped (--keep-image-driver-libs)",
                       "request_ld_library_path": request_ld, "image_ld_library_path": image_ld,
                       "container_ld_library_path": container_ld}
        if request_ld is not None:
            argv += ["-e", f"LD_LIBRARY_PATH={request_ld}"]
            env["names"].append("LD_LIBRARY_PATH")
    else:
        libdir, libdir_source = host_driver_libdir()
        base, base_source = ((request_ld, "request") if request_ld else (image_ld, "image") if image_ld
                             else (LD_FALLBACK_BASE, "fallback"))
        container_ld = ":".join([libdir] + [x for x in base.split(":") if x and os.path.normpath(x) != libdir])
        driver_libs = {"mode": "host driver libraries first", "host_driver_libdir": libdir,
                       "host_driver_libdir_source": libdir_source, "request_ld_library_path": request_ld,
                       "image_ld_library_path": image_ld, "base_source": base_source,
                       "container_ld_library_path": container_ld}
        argv += ["-e", f"LD_LIBRARY_PATH={container_ld}"]
        env["names"].append("LD_LIBRARY_PATH")
    # what the compute service injects on the cluster (jobs may record these in their own identity files;
    # DET_ALLOCATION_ID stays unset, so such a record shows it null)
    service_env = {"COMPUTE_CODE_REVISION": req.get("code_revision"), "COMPUTE_WORKDIR": workdir,
                   "COMPUTE_OUTPUT_DIR": req_out}
    for k, v in service_env.items():
        if v and k not in env["names"]:
            env["names"].append(k)
            env["service_values"] = dict(env.get("service_values", {}), **{k: v})
            argv += ["-e", f"{k}={v}"]
    argv += ["-w", workdir, "--entrypoint", "", image] + command

    record = {
        "launched_by": LAUNCHED_BY,
        "mode": "dry" if a.dry else "run",
        "host": socket.gethostname(),
        "gpu": gpu,
        "gpu_request": gpu_request,
        "card_class": f"local workstation {gpu['name']}; a different card class from the cluster pool "
                      f"{req.get('pool')}; never compared bitwise with cluster results",
        "image": {"reference": image, "local_image_id": image_id, "repo_tags": info.get("RepoTags"),
                  "entrypoint": "reset with --entrypoint '' (Determined replaces the image entrypoint on the cluster)"},
        "cuda_arch": arch,
        "request": {"path": req_path, "sha256": req_sha, "request_id": request_id, "name": req.get("name"),
                    "code_revision": req.get("code_revision"), "workdir": workdir, "output_dir": req_out,
                    "cluster_pool": req.get("pool"),
                    "command_sha256": hashlib.sha256(json.dumps(command, ensure_ascii=False).encode()).hexdigest()},
        "output_dir": eff_out,
        "output_dir_overridden": a.output_dir is not None,
        "job_out": job_out,
        "shared_roots": roots,
        "det_experiment_id": det_exp,
        "det_trial_id": "0",
        "container_name": name,
        "shm_size_bytes": shm,
        "memory": {"limit_bytes": a.memory, "swap": "disabled (--memory-swap = --memory)",
                   "host_floor_bytes": floor, "host_mem_available_bytes_at_launch": avail,
                   "sample_interval_s": SAMPLE_S},
        "cpus": {"limit": cpus, "host_cpus": host_cpus,
                 "source": "--cpus" if a.cpus is not None else "default: half the host's CPUs, at least 4"},
        "driver_libraries": driver_libs,
        "environment": env,
        "docker_argv": argv,
        "notes": notes,
        "start_utc": utc_now(),
        "end_utc": None,
        "exit_code": None,
        "container_id": None,
    }

    if a.dry:
        def show(x, i):
            if len(x) <= ELIDE:
                return x
            ci = i - (len(argv) - len(command))
            src = f", = request command[{ci}]" if 0 <= ci < len(command) and command[ci] == x else ""
            return f"<elided: {len(x)} chars, sha256 {hashlib.sha256(x.encode()).hexdigest()}{src}>"
        disp = [show(x, i) for i, x in enumerate(argv)]
        print(f"== docker command (dry; argv elements over {ELIDE} chars elided; "
              "-e NAME values come from the request via the docker CLI environment)")
        print(shlex.join(disp))
        print(f"== launch record that would be written to {eff_out}/local-launch.json (docker_argv elided alike)")
        print(json.dumps(dict(record, docker_argv=disp), indent=2, ensure_ascii=False))
        for n in notes:
            print(f"note: {n}")
        if would_refuse:
            for r in would_refuse:
                print(f"real-run verdict: WOULD REFUSE: {r}")
            return 3
        print("real-run verdict: would launch")
        return 0

    # ---- run ----------------------------------------------------------------------------------------
    os.makedirs(eff_out, exist_ok=True)
    try:
        os.chmod(eff_out, 0o777)  # cluster output dirs are 0777; NFS squashes the container's root
    except OSError:
        pass
    os.makedirs(os.path.dirname(cidfile), exist_ok=True)
    rec_path = os.path.join(eff_out, "local-launch.json")
    write_json_atomic(rec_path, record)
    t0 = time.monotonic()
    log = open(os.path.join(eff_out, "local-run.log"), "ab")
    state = {"signal": None, "stops": 0, "low_memory_stop": None, "peak": None, "avail_min": avail,
             "gpu_mem_max": None, "gpu_util": [], "samples": 0, "inspect": None}
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=docker_env, start_new_session=True)

    def stop_container(kill=False):
        cid = read_cid(cidfile)
        if cid:  # only our own container: TERM to its timeout process, KILL after the grace
            subprocess.Popen([docker, "kill", cid] if kill else [docker, "stop", "--time", "45", cid],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            proc.terminate()

    def on_signal(signum, _frame):
        state["signal"] = signal.Signals(signum).name
        state["stops"] += 1
        stop_container(kill=state["stops"] > 1)

    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, on_signal)

    done = threading.Event()

    def inspect(cid):  # the limits docker actually applied, read from the running container
        q = subprocess.run([docker, "inspect", cid], capture_output=True, text=True)
        try:
            c = json.loads(q.stdout)[0]
        except (ValueError, IndexError):
            return
        hc = c.get("HostConfig") or {}
        ld = [e.split("=", 1)[1] for e in (c.get("Config") or {}).get("Env") or [] if e.startswith("LD_LIBRARY_PATH=")]
        state["inspect"] = {"utc": utc_now(), "Runtime": hc.get("Runtime"), "NanoCpus": hc.get("NanoCpus"),
                            "Memory": hc.get("Memory"), "MemorySwap": hc.get("MemorySwap"),
                            "ShmSize": hc.get("ShmSize"), "Binds": hc.get("Binds"),
                            "LD_LIBRARY_PATH": ld[0] if ld else None}

    def cgroup_dir(cid):
        for d in (f"/sys/fs/cgroup/system.slice/docker-{cid}.scope", f"/sys/fs/cgroup/docker/{cid}"):
            if os.path.isdir(d):
                return d
        return None

    def sampler():
        with open(os.path.join(eff_out, "local-memory.log"), "a") as mlog:
            mlog.write("utc\thost_mem_available_mib\tcontainer_memory_current_mib\tcontainer_memory_peak_mib\t"
                       "gpu_memory_used_mib\tgpu_util_percent\n")
            next_sample = time.monotonic() + SAMPLE_S
            while not done.wait(1 if state["inspect"] is None else max(0.0, next_sample - time.monotonic())):
                cid = read_cid(cidfile)
                if cid and state["inspect"] is None:
                    inspect(cid)
                if time.monotonic() < next_sample:
                    continue
                next_sample += SAMPLE_S
                av = mem_available()
                cur = peak = None
                cg = cgroup_dir(cid) if cid else None
                if cg:
                    try:
                        cur = int(open(f"{cg}/memory.current").read())
                        peak = int(open(f"{cg}/memory.peak").read())
                    except (OSError, ValueError):
                        pass
                g = subprocess.run(["nvidia-smi", "-i", gpu["uuid"], "--query-gpu=memory.used,utilization.gpu",
                                    "--format=csv,noheader,nounits"], capture_output=True, text=True)
                gm = gu = None
                try:
                    gm, gu = (int(x) for x in g.stdout.strip().split(","))
                except ValueError:
                    pass
                state["samples"] += 1
                if peak is not None:
                    state["peak"] = max(state["peak"] or 0, peak)
                if av is not None:
                    state["avail_min"] = min(state["avail_min"] or av, av)
                if gm is not None:
                    state["gpu_mem_max"] = max(state["gpu_mem_max"] or 0, gm)
                    state["gpu_util"].append(gu)
                mib = lambda v: "" if v is None else str(v // (1 << 20))
                mlog.write(f"{utc_now()}\t{mib(av)}\t{mib(cur)}\t{mib(peak)}\t"
                           f"{'' if gm is None else gm}\t{'' if gu is None else gu}\n")
                mlog.flush()
                if av is not None and av < floor and state["low_memory_stop"] is None:
                    state["low_memory_stop"] = f"{utc_now()} host MemAvailable {av // (1 << 20)} MiB < floor"
                    mlog.write(f"# LOW MEMORY: {state['low_memory_stop']}; stopping the container\n")
                    mlog.flush()
                    stop_container()

    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    echo = True
    rc = None
    try:
        while True:
            chunk = proc.stdout.read1(65536)
            if not chunk:
                break
            log.write(chunk)
            log.flush()
            if echo:
                try:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                except OSError:
                    echo = False
        rc = proc.wait()
    finally:
        done.set()
        th.join(timeout=30)
        log.close()
        util = state["gpu_util"]
        record.update(
            end_utc=utc_now(), exit_code=rc, container_id=read_cid(cidfile),
            duration_s=round(time.monotonic() - t0, 1), interrupted_by=state["signal"],
            low_memory_stop=state["low_memory_stop"], container_inspect=state["inspect"],
            usage={"container_memory_peak_bytes": state["peak"],
                   "container_memory_peak_note": f"cgroup memory.peak at the last {SAMPLE_S} s sample before exit",
                   "host_mem_available_min_bytes": state["avail_min"],
                   "gpu_memory_used_mib_max": state["gpu_mem_max"],
                   "gpu_util_percent_mean": round(sum(util) / len(util), 1) if util else None,
                   "gpu_note": "whole device (includes other processes on it)",
                   "samples": state["samples"]})
        write_json_atomic(rec_path, record)
    if rc is None:
        return 1
    return rc if rc >= 0 else 128 - rc


sys.exit(main())
PY
)

exec python3 -I -c "$PY_SRC" "$@"
