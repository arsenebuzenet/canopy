# tests/test_compat.py
"""Platform seam — the only module allowed to branch on the OS."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from canopy import compat

_CHILD_LOCK_HOLDER = """
import sys
import time
from canopy import compat

path = sys.argv[1]
with open(path, "r+b") as f:
    compat.lock(f)
    print("locked", flush=True)
    time.sleep(1.5)
    compat.unlock(f)
"""

_CHILD_APPEND_LOCK_HOLDER = """
import sys
import time
from canopy import compat

path = sys.argv[1]
with open(path, "a", encoding="utf-8") as f:
    compat.lock(f)
    f.write("child\\n")
    f.flush()
    print("locked", flush=True)
    time.sleep(1.5)
    compat.unlock(f)
"""


def test_is_windows_matches_sys_platform():
    assert compat.IS_WINDOWS is sys.platform.startswith("win")


def test_lock_unlock_roundtrip_on_file_object(tmp_path):
    p = tmp_path / "x.lock"
    with open(p, "w", encoding="utf-8") as f:
        compat.lock(f)
        compat.unlock(f)


def test_lock_unlock_roundtrip_on_fd(tmp_path):
    p = tmp_path / "x.lock"
    with open(p, "w", encoding="utf-8") as f:
        compat.lock(f.fileno())
        compat.unlock(f.fileno())


def test_lock_on_append_mode_file(tmp_path):
    """historian appends; msvcrt must accept locking at EOF."""
    p = tmp_path / "mem.md"
    p.write_text("seed\n", encoding="utf-8")
    with open(p, "a", encoding="utf-8") as f:
        compat.lock(f.fileno())
        f.write("more\n")
        compat.unlock(f.fileno())
    assert p.read_text(encoding="utf-8") == "seed\nmore\n"


def test_lock_blocks_until_holder_releases(tmp_path):
    """`lock()` must block like `flock(LOCK_EX)`, not give up after ~10s (msvcrt's LK_LOCK)."""
    p = tmp_path / "contend.lock"
    p.write_text("x", encoding="utf-8")
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD_LOCK_HOLDER, str(p)],
        stdout=subprocess.PIPE, text=True, encoding="utf-8",
    )
    try:
        line = child.stdout.readline()
        assert line.strip() == "locked"

        start = time.monotonic()
        with open(p, "r+b") as f:
            compat.lock(f)
            elapsed = time.monotonic() - start
            compat.unlock(f)

        assert elapsed >= 1.0
    finally:
        assert child.wait(timeout=5) == 0


def test_lock_serialises_append_mode_writers(tmp_path):
    """historian holds an append handle whose offset is EOF and keeps moving —
    two writers must still contend on ONE byte, not on a byte each."""
    p = tmp_path / "mem.md"
    p.write_text("seed\n" * 40, encoding="utf-8")
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD_APPEND_LOCK_HOLDER, str(p)],
        stdout=subprocess.PIPE, text=True, encoding="utf-8",
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        start = time.monotonic()
        with open(p, "a", encoding="utf-8") as f:
            compat.lock(f)
            elapsed = time.monotonic() - start
            f.write("parent\n")
            f.flush()
            compat.unlock(f)
        assert elapsed >= 1.0
    finally:
        assert child.wait(timeout=10) == 0
    assert p.read_text(encoding="utf-8").splitlines()[-2:] == ["child", "parent"]


def test_lock_reraises_non_contention_oserror():
    """A bad fd must fail fast, not spin forever in the Windows poll loop."""
    start = time.monotonic()
    with pytest.raises(OSError):
        compat.lock(-1)
    assert time.monotonic() - start < 1.0


def test_user_home_honours_HOME_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert compat.user_home() == tmp_path


def test_user_home_falls_back_to_path_home(monkeypatch):
    monkeypatch.delenv("HOME", raising=False)
    assert compat.user_home() == Path.home()


def test_run_shell_posix_semantics(tmp_path):
    """`;`, `>&2` and `exit N` must work on every platform (Git Bash on Windows)."""
    r = compat.run_shell("echo hi; echo err >&2; exit 3", cwd=tmp_path,
                         capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 3
    assert r.stdout.strip() == "hi"
    assert r.stderr.strip() == "err"


def test_run_shell_uses_cwd(tmp_path):
    marker = tmp_path / "marker.txt"
    compat.run_shell("echo x > marker.txt", cwd=tmp_path,
                     capture_output=True, text=True, encoding="utf-8")
    assert marker.exists()


@pytest.mark.skipif(not compat.IS_WINDOWS, reason="Windows only")
def test_find_bash_is_not_wsl_launcher():
    bash = compat.find_bash()
    assert bash is not None
    assert "system32" not in str(bash).lower()


def test_detached_popen_kwargs_spawns(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-c", "print('ok')"],
        cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **compat.detached_popen_kwargs(),
    )
    assert proc.wait(timeout=30) == 0


def test_same_path_normalises_separators_and_case(tmp_path):
    a = tmp_path / "Repo"
    a.mkdir()
    fwd = str(a).replace("\\", "/")
    assert compat.same_path(a, fwd)
    if compat.IS_WINDOWS:
        assert compat.same_path(a, fwd.upper())
    assert not compat.same_path(a, tmp_path / "other")
