"""Lightweight console-script entries for Claude Code hooks.

Deliberately separate from cli/main.py: these run on EVERY Bash tool call,
so they must not import argparse/rich/the full CLI. Keep module-level
imports to stdlib-minimum; canopy modules load lazily inside the functions.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _utf8_stdio() -> None:
    """Force UTF-8 on stdout/stderr so blocked-reason text (e.g. em dashes)
    survives Windows' cp1252 default console encoding intact for the parent
    process and for Claude Code, which reads hook output as UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def gate_main() -> None:
    """PreToolUse shim. Exit 0 = allow; exit 2 = block, reason on stderr."""
    _utf8_stdio()
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        sys.exit(0)
    try:
        from .actions.hook_gate import run_gate
        code, message = run_gate(payload)
    except Exception:
        sys.exit(0)                     # fail open: even import errors
    if message:
        print(message, file=sys.stderr)
    sys.exit(code)


def context_main() -> None:
    """SessionStart shim. Prints the workspace brief to stdout (→ context)."""
    _utf8_stdio()
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        cwd = Path(payload.get("cwd") or Path.cwd())
        from .actions.hook_gate import _load_workspace_from
        from .actions.hook_context import context_brief
        workspace = _load_workspace_from(cwd)
        if workspace is not None:
            print(context_brief(workspace))
    except Exception:
        pass
    sys.exit(0)
