"""Subprocess spawning for canopy.

Children must not inherit canopy's own stdin. Under the MCP stdio server
stdin is a pipe the host holds open, and a git child that inherits it never
exits on Windows — ``subprocess.run`` then blocks forever in ``communicate``,
hanging the tool call. Detaching stdin costs nothing for a captured command
and removes the whole class of hang.
"""
from __future__ import annotations

import subprocess
from typing import Any


def run(args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run`` with stdin detached unless the caller sets it."""
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(args, **kwargs)
