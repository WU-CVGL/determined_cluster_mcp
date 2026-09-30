"""Every server entry the documentation shows parses with the server's own arguments."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

from determined_compute.mcp_server import build_parser

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = "determined-compute-mcp"
_FENCE = re.compile(r"```(\w*)\n(.*?)```", re.S)


def server_entries() -> Iterator[Tuple[str, List[str]]]:
    """The arguments of each fenced JSON entry or shell command that starts the server."""

    documents = sorted([*ROOT.glob("README*.md"), *(ROOT / "docs").glob("*.md")])
    for path in documents:
        for language, body in _FENCE.findall(path.read_text(encoding="utf-8")):
            if PROGRAM not in body:
                continue
            if language == "json":
                entry = json.loads(body)
                if entry["command"].endswith(PROGRAM):
                    yield path.name, entry["args"]
                continue
            for line in body.replace("\\\n", " ").splitlines():
                words = shlex.split(line)
                if words and words[0].endswith(PROGRAM) and words[1:] != ["--help"]:
                    yield path.name, words[1:]


ENTRIES = list(server_entries())


def test_the_documents_show_the_server_entry_in_both_languages():
    names = {name for name, _args in ENTRIES}
    for document in ("README", "compute-service", "troubleshooting"):
        assert {f"{document}.md", f"{document}.zh.md"} <= names


@pytest.mark.parametrize("name, args", ENTRIES, ids=[name for name, _args in ENTRIES])
def test_a_documented_server_entry_parses(name, args):
    try:
        parsed = build_parser().parse_args(args)
    except SystemExit:
        pytest.fail(f"{name} shows arguments the server does not take: {args}")
    assert parsed.profile
