# src/canopy/compat.py
"""Platform seam — the only module allowed to import ``fcntl``/``msvcrt`` or
test ``sys.platform``/``os.name`` (the standalone hook template aside), as
``tests/test_import_boundary.py`` enforces. ``platform.system()`` stays
permitted elsewhere for install hints — ``cli/main.py`` and
``integrations/github.py`` word their advice per OS without behaving
differently.

Windows has no ``fcntl``, no ``sh``, and ``Path.home()`` ignores ``HOME``.
Everything canopy needs from the OS that differs between POSIX and Windows
goes through here so the rest of the code base stays platform-blind.
"""
from __future__ import annotations

import errno
import os
import shutil
import stat
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

    # msvcrt.locking reports an already-held range as EACCES; EDEADLOCK is its
    # documented companion. Anything else (EBADF, ENOENT) is a real fault and
    # must escape the retry loop rather than spin the CPU forever.
    _LOCK_CONTENTION_ERRNOS = frozenset({errno.EACCES, errno.EDEADLOCK})

    def _lock_byte_zero(fd: int, mode: int) -> None:
        """Apply ``mode`` to byte 0 whatever the handle's offset is.

        msvcrt locks a range starting at the CURRENT offset, and callers pass
        append-mode handles that sit at a moving EOF — two writers would each
        lock a different byte and never see each other. Byte 0 is the one
        address they agree on; O_APPEND still writes at EOF regardless.
        """
        pos = os.lseek(fd, 0, os.SEEK_CUR)
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, mode, 1)
        finally:
            os.lseek(fd, pos, os.SEEK_SET)

    def lock(f: Any) -> None:
        # LK_LOCK gives up after ~10s and raises OSError, unlike flock(LOCK_EX),
        # which blocks indefinitely — so poll LK_NBLCK ourselves to match it.
        fd = _fd(f)
        while True:
            try:
                _lock_byte_zero(fd, msvcrt.LK_NBLCK)
                return
            except OSError as exc:
                if exc.errno not in _LOCK_CONTENTION_ERRNOS:
                    raise
                time.sleep(0.05)

    def unlock(f: Any) -> None:
        try:
            _lock_byte_zero(_fd(f), msvcrt.LK_UNLCK)
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
    """Git for Windows' bash. System32 and WindowsApps ``bash.exe`` are WSL
    launchers (the second is the App Execution Alias) — never those."""
    if not IS_WINDOWS:
        return None
    git = shutil.which("git")
    if git:
        # PATH may expose either …\Git\cmd\git.exe or, under Git Bash,
        # …\Git\mingw64\bin\git.exe — the install root is one to three levels
        # up, so try every parent instead of assuming a fixed depth.
        for root in list(Path(git).resolve().parents)[:4]:
            for rel in ("bin/bash.exe", "usr/bin/bash.exe"):
                candidate = root / rel
                if candidate.exists():
                    return candidate
    found = shutil.which("bash")
    if found and not any(p in found.lower() for p in ("system32", "windowsapps")):
        return Path(found)
    return None


def run_shell(cmd: str, *, cwd: Path | str | None = None, **kw: Any) -> subprocess.CompletedProcess:
    """Run a POSIX shell command line the same way on every platform.

    Claude Code's Bash tool on Windows is Git Bash, so agents write POSIX
    command lines; cmd.exe would break ``;``, ``&&``, ``>&2``, ``$VAR``.
    """
    if IS_WINDOWS:
        bash = find_bash()
        if bash is None:
            # Falling back to cmd.exe here would mangle the command line into
            # something that fails obscurely later; say so up front instead.
            # Deferred: canopy.actions imports compat transitively at import time.
            from .actions.errors import BlockerError, FixAction

            raise BlockerError(
                code="bash_not_found",
                what="Git for Windows bash not found; canopy runs shell "
                     "commands through Git Bash on Windows",
                fix_actions=[FixAction(
                    action="install", args={}, safe=True,
                    preview="winget install --id Git.Git",
                )],
            )
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


# ── stdio encoding ───────────────────────────────────────────────────

def utf8_stdio() -> None:
    """Force UTF-8 on stdin/stdout/stderr.

    A piped stdio on Windows defaults to the console codepage (cp1252),
    not UTF-8 — only an interactive Windows Terminal happens to negotiate
    UTF-8 on its own. Every glyph canopy prints (``✓``, `→`, box-drawing
    separators) then raises ``UnicodeEncodeError`` the moment stdout is
    redirected to a file, a pipe, or (critically) an agent's shell tool.
    POSIX locales are already UTF-8, so calling this there is a no-op.

    Call before the first read/write of any of the three streams."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


# ── directory links ──────────────────────────────────────────────────
#
# Windows junctions need no privilege, unlike Windows symlinks, so they are
# the link type there. ``os.rmdir`` on a junction drops the reparse point and
# leaves the target untouched; ``os.unlink`` is the POSIX symlink equivalent.

if IS_WINDOWS:
    import _winapi

    _REPARSE_TAGS = {stat.IO_REPARSE_TAG_MOUNT_POINT, stat.IO_REPARSE_TAG_SYMLINK}

    def is_dir_link(path: Path | str) -> bool:
        """True for a junction or a directory symlink; False for anything else or a missing path."""
        try:
            st = os.lstat(path)
        except OSError:
            return False
        return getattr(st, "st_reparse_tag", 0) in _REPARSE_TAGS

    def make_dir_link(link: Path | str, target: Path | str) -> None:
        # Private CPython API (CPython's own test suite relies on it too); no
        # public equivalent short of shelling out to `mklink /J`.
        _winapi.CreateJunction(str(target), str(link))

    def read_dir_link(link: Path | str) -> Path:
        raw = os.readlink(link)
        if raw.startswith("\\\\?\\"):
            raw = raw[4:]
        return Path(raw)

    def remove_dir_link(link: Path | str) -> None:
        os.rmdir(link)

else:

    def is_dir_link(path: Path | str) -> bool:
        """True for a symlink (of any kind); False otherwise or for a missing path."""
        return os.path.islink(path)

    def make_dir_link(link: Path | str, target: Path | str) -> None:
        os.symlink(str(target), str(link), target_is_directory=True)

    def read_dir_link(link: Path | str) -> Path:
        return Path(os.readlink(link))

    def remove_dir_link(link: Path | str) -> None:
        os.unlink(link)
