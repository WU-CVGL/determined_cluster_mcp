"""Read-only package isolation checks; SDK installation is confined to job user-site."""
import argparse
import importlib.metadata as md
import json
import os
from pathlib import Path
import site
import subprocess
import sys


def packages():
    return {d.metadata["Name"].lower().replace("_", "-"): d.version for d in md.distributions()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("snapshot", "compare", "sdk"))
    parser.add_argument("path", help="snapshot path, or injected SDK wheel directory")
    parser.add_argument("--sdk-version", default="0.42.1", help="declared service SDK version")
    args = parser.parse_args()
    mode = args.mode
    target = Path(args.path)
    if mode == "snapshot":
        assert not site.ENABLE_USER_SITE, "science must disable user-site"
        assert "determined" not in packages(), "SDK must not be installed in science"
        with target.open("x") as stream:
            json.dump(packages(), stream, sort_keys=True)
    elif mode == "compare":
        assert packages() == json.loads(target.read_text()), "scientific packages changed"
        assert not site.ENABLE_USER_SITE, "science must disable user-site"
        print("Scientific package versions unchanged")
    elif mode == "sdk":
        assert os.getuid() != 0, "run admission as the job's unprivileged UID"
        assert os.environ.get("LOGNAME"), "set LOGNAME for a UID without a passwd entry"
        assert site.ENABLE_USER_SITE is True
        assert md.version("pip") == "26.2.1"
        assert md.version("requests") == "2.31.0"
        wheels = list(target.glob("determined-" + args.sdk_version + "-*.whl"))
        assert len(wheels) == 1, "supply the actual service-injected SDK wheel"
        subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--no-index", "--no-deps", str(wheels[0])], check=True)
        subprocess.run([sys.executable, "-m", "pip", "check"], check=True)
        subprocess.run([sys.executable, "-c", "import importlib.metadata as m; import determined; assert m.version('determined') == " + repr(args.sdk_version)], check=True)
        subprocess.run([sys.executable, "-m", "determined.exec.prep_container", "--help"], check=True, timeout=60)
        print("Injected Determined " + args.sdk_version + " imports and prep_container help succeeds offline as job UID")
    else:
        raise ValueError(mode)


if __name__ == "__main__":
    main()
