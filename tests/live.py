"""Settings for the tests that need a real master, named by DETERMINED_COMPUTE_TEST_MASTER.

The variable holds the path of a KEY=VALUE file in the secrets file format with DET_MASTER,
DET_USERNAME and DET_PASSWORD, and optionally DETERMINED_COMPUTE_TEST_POOL (``default`` when
absent). The master needs submission protocol 1. A pool without agents is enough: every job is
launched with zero slots and cancelled before its test ends. Without the variable these tests
are skipped.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

import pytest

from determined_compute.client import Client
from determined_compute.utils.secrets import load_secrets

VARIABLE = "DETERMINED_COMPUTE_TEST_MASTER"
requires_master = pytest.mark.skipif(not os.environ.get(VARIABLE), reason=f"{VARIABLE} is not set")
_REQUIRED = ("DET_MASTER", "DET_USERNAME", "DET_PASSWORD")
# Anything the caller's environment could add to or put ahead of the settings file.
_AMBIENT = ("DET_MASTER", "DET_MASTER_ADDR", "DET_MASTER_HOST", "DET_API_TOKEN")


def settings_path() -> Path:
    return Path(os.environ[VARIABLE]).expanduser()


def settings() -> Dict[str, str]:
    values = load_secrets(settings_path())
    missing = [name for name in _REQUIRED if not values.get(name)]
    if missing:
        pytest.fail(f"the {VARIABLE} file lacks {', '.join(missing)}")
    return values


def pool() -> str:
    return settings().get("DETERMINED_COMPUTE_TEST_POOL") or "default"


def client(monkeypatch: pytest.MonkeyPatch) -> Client:
    """A client configured by the settings file alone."""

    for name in _AMBIENT:
        monkeypatch.delenv(name, raising=False)
    return Client(settings()["DET_MASTER"], secrets_path=settings_path())
