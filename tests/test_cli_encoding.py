"""CLI output must survive being piped, not just run in an interactive
terminal.

On Windows a piped stdout defaults to the console codepage (cp1252), not
UTF-8 — only an interactive Windows Terminal negotiates UTF-8 on its own.
`cli/main.py` prints glyphs (checkmarks, arrows, box-drawing separators)
that cp1252 cannot encode, so any pipe — `canopy status | findstr x`, a
CI log redirect, or an agent's shell tool capturing output as a pipe —
crashed with UnicodeEncodeError before `main()` forced UTF-8 stdio.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys


def _piped_env() -> dict:
    """The current environment with the escape hatches removed, so the
    subprocess exercises the real default codepage rather than one already
    forced to UTF-8 by the outer test runner's environment."""
    env = dict(os.environ)
    env.pop("PYTHONUTF8", None)
    env.pop("PYTHONIOENCODING", None)
    return env


def test_status_output_survives_a_pipe(canopy_toml):
    result = subprocess.run(
        [sys.executable, "-m", "canopy.cli.main", "status"],
        cwd=canopy_toml, env=_piped_env(), capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    stdout = result.stdout.decode("utf-8")
    assert "test-workspace" in stdout


def test_context_json_output_survives_a_pipe(canopy_toml):
    result = subprocess.run(
        [sys.executable, "-m", "canopy.cli.main", "context", "--json"],
        cwd=canopy_toml, env=_piped_env(), capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    data = json.loads(result.stdout.decode("utf-8"))
    assert data
