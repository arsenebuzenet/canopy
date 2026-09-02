# src/canopy/compat.py
"""Platform seam — the only module that branches on the operating system.

Windows has no ``fcntl``, no ``sh``, and ``Path.home()`` ignores ``HOME``.
Everything canopy needs from the OS that differs between POSIX and Windows
goes through here so the rest of the code base stays platform-blind.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

IS_WINDOWS = sys.platform.startswith("win")


# ── file locking ─────────────────────────────────────────────────────

def _fd(f: Any) -> int:
    return f if isinstance(f, int) else f.fileno()


if IS_WINDOWS:
    import msvcrt

    def lock(f: Any) -> None:
        # msvcrt locks a byte range from the current position; one byte is
        # enough to serialise writers, and locking past EOF is allowed, so
        # append-mode handles work too.
        # LK_LOCK gives up after ~10s and raises OSError, unlike flock(LOCK_EX),
        # which blocks indefinitely — so poll LK_NBLCK ourselves to match it.
        fd = _fd(f)
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(0.05)

    def unlock(f: Any) -> None:
        try:
            msvcrt.locking(_fd(f), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def lock(f: Any) -> None:
        fcntl.flock(_fd(f), fcntl.LOCK_EX)

    def unlock(f: Any) -> None:
        fcntl.flock(_fd(f), fcntl.LOCK_UN)


# ── home directory ───────────────────────────────────────────────────

def user_home() -> Path:
    """``$HOME`` when set (Git Bash sets it; tests monkeypatch it), else ``Path.home()``.

    ``Path.home()`` reads ``USERPROFILE`` on Windows and ignores ``HOME``.
    """
    home = os.environ.get("HOME")
    return Path(home) if home else Path.home()


# ── shell dispatch ───────────────────────────────────────────────────

def find_bash() -> Path | None:
    """Git for Windows' bash. ``System32\\bash.exe`` is WSL's launcher — never that."""
    if not IS_WINDOWS:
        return None
    git = shutil.which("git")
    if git:
        root = Path(git).resolve().parent.parent
        for rel in ("bin/bash.exe", "usr/bin/bash.exe"):
            candidate = root / rel
            if candidate.exists():
                return candidate
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return Path(found)
    return None


def run_shell(cmd: str, *, cwd: Path | str | None = None, **kw: Any) -> subprocess.CompletedProcess:
    """Run a POSIX shell command line the same way on every platform.

    Claude Code's Bash tool on Windows is Git Bash, so agents write POSIX
    command lines; cmd.exe would break ``;``, ``&&``, ``>&2``, ``$VAR``.
    """
    if IS_WINDOWS:
        bash = find_bash()
        if bash is not None:
            return subprocess.run([str(bash), "-c", cmd], cwd=cwd, **kw)
    return subprocess.run(cmd, cwd=cwd, shell=True, **kw)


def detached_popen_kwargs() -> dict[str, Any]:
    """Extra ``Popen`` kwargs to fully detach a child from this process."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


# ── path equality ────────────────────────────────────────────────────

def same_path(a: Path | str, b: Path | str) -> bool:
    """True if ``a`` and ``b`` name the same location (separators/case-insensitive on Windows)."""
    ra, rb = Path(a).resolve(), Path(b).resolve()
    return os.path.normcase(str(ra)) == os.path.normcase(str(rb))
