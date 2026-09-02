# Windows Native Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** canopy's CLI, MCP server, git hook, Claude Code hooks and doctor run on native Windows with the full test suite green there and on Linux.

**Architecture:** One platform seam module `src/canopy/compat.py` owns every OS-conditional (file lock, shell dispatch, home dir, detached spawn, path equality). Callers migrate to it; the standalone git-hook template embeds the same lock shim inline. All text I/O is UTF-8 explicit; code never compares path strings.

**Tech Stack:** Python 3.10+ (`msvcrt` on Windows, `fcntl` elsewhere), Git for Windows bash, pytest, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-02-windows-port-design.md`

## Global Constraints

- Python `>=3.10`; no new runtime dependencies.
- `fcntl`, `msvcrt`, `sys.platform` / `os.name` checks appear ONLY in `src/canopy/compat.py` and `src/canopy/git/templates/post-checkout.py`.
- Every `read_text` / `write_text` / text-mode `open` / `subprocess.run(text=True)` passes `encoding="utf-8"`.
- Code never compares path strings; use `Path(...).resolve()` equality or `compat.same_path`.
- Linux behaviour must not change — existing tests stay green on Linux.
- `mcp>=1.0,<2`.
- Commit messages: `type: short description`, imperative, lowercase, ≤72 chars, no attribution trailers.
- Run tests from the repo root `C:\Dev\Tools\canopy` with `python -m pytest`. Windows full suite takes ~22 min; run only the named files per task, full suite in Task 12.

---

### Task 1: `compat.py` platform seam

**Files:**
- Create: `src/canopy/compat.py`
- Test: `tests/test_compat.py`

**Interfaces:**
- Produces:
  - `IS_WINDOWS: bool`
  - `lock(f) -> None`, `unlock(f) -> None` — `f` is an open file object or an int fd
  - `user_home() -> Path`
  - `find_bash() -> Path | None`
  - `run_shell(cmd: str, *, cwd: Path | str | None = None, **kw) -> subprocess.CompletedProcess`
  - `detached_popen_kwargs() -> dict`
  - `same_path(a, b) -> bool`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_compat.py
"""Platform seam — the only module allowed to branch on the OS."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from canopy import compat


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_compat.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'canopy.compat'`

- [ ] **Step 3: Write the implementation**

```python
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
        msvcrt.locking(_fd(f), msvcrt.LK_LOCK, 1)

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_compat.py -v`
Expected: all PASS (the `find_bash` test is skipped on Linux).

- [ ] **Step 5: Commit**

```bash
git add src/canopy/compat.py tests/test_compat.py
git commit -m "feat: add compat platform seam for windows support"
```

---

### Task 2: file locks in `slots.py` and `historian.py`

**Files:**
- Modify: `src/canopy/actions/slots.py:30,189,192`
- Modify: `src/canopy/management/historian.py:30,86,89`
- Modify: `tests/test_import_boundary.py`
- Test: `tests/test_slots.py`, `tests/test_historian.py`, `tests/test_import_boundary.py`

**Interfaces:**
- Consumes: `canopy.compat.lock`, `canopy.compat.unlock`

- [ ] **Step 1: Confirm the failure on Windows (or note it on Linux)**

Run: `python -c "import canopy.actions.slots, canopy.management.historian"`
Expected on Windows: `ModuleNotFoundError: No module named 'fcntl'`. On Linux: OK (the change must keep it OK).

- [ ] **Step 2: Replace the imports and calls**

In `src/canopy/actions/slots.py` replace `import fcntl` with `from .. import compat`, and in the lock context manager:

```python
    f = open(lock_path, "w", encoding="utf-8")
    try:
        compat.lock(f)
        yield
    finally:
        compat.unlock(f)
        f.close()
```

In `src/canopy/management/historian.py` replace `import fcntl` with `from .. import compat`, and:

```python
    with open(path, "a", encoding="utf-8") as f:
        try:
            compat.lock(f)
            yield f
        finally:
            compat.unlock(f)
```

Update the historian docstring sentence "writes use ``fcntl.flock``" to "writes use ``compat.lock``".

- [ ] **Step 3: Run the tests**

Run: `python -m pytest tests/test_slots.py tests/test_historian.py -q`
Expected: PASS on both platforms.

- [ ] **Step 4: Add the import-boundary guard**

Append to `tests/test_import_boundary.py`:

```python
def test_only_compat_and_hook_template_touch_platform_apis():
    """fcntl/msvcrt/sys.platform live in compat.py (and the standalone hook template) only."""
    import re
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "src" / "canopy"
    allowed = {src / "compat.py", src / "git" / "templates" / "post-checkout.py"}
    pattern = re.compile(
        r"^\s*(import fcntl|import msvcrt|from fcntl|from msvcrt)|sys\.platform|os\.name\b",
        re.M,
    )
    offenders = [
        str(p.relative_to(src)) for p in src.rglob("*.py")
        if p not in allowed and pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []
```

Run: `python -m pytest tests/test_import_boundary.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/canopy/actions/slots.py src/canopy/management/historian.py tests/test_import_boundary.py
git commit -m "fix: route slot and historian file locks through compat"
```

---

### Task 3: `post-checkout` hook template and installer on Windows

**Files:**
- Modify: `src/canopy/git/templates/post-checkout.py:8,44-48`
- Modify: `src/canopy/git/hooks.py:171-173`
- Modify: `tests/test_hooks.py:46`
- Test: `tests/test_hooks.py`

**Interfaces:**
- Consumes: `canopy.compat.IS_WINDOWS` (installer only — the template is standalone)

- [ ] **Step 1: Run the hook tests to see the Windows failures**

Run: `python -m pytest tests/test_hooks.py -q`
Expected on Windows: `test_install_creates_hook` fails (`str(root.resolve()) in content` — the template embeds the path JSON-escaped, `C:\\Users`), and every checkout-based test fails with `CalledProcessError` because the hook dies on `import fcntl`.

- [ ] **Step 2: Fix the assertion to match how the path is embedded**

`tests/test_hooks.py:46`:

```python
    assert json.dumps(str(root.resolve())) in content
```

Add `import json` at the top of the file if missing.

- [ ] **Step 3: Make the template self-contained on both platforms**

In `src/canopy/git/templates/post-checkout.py` replace `import fcntl` with:

```python
try:
    import fcntl

    def _lock(fd):
        fcntl.flock(fd, fcntl.LOCK_EX)
except ImportError:  # Windows
    import msvcrt

    def _lock(fd):
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
```

and in `_record_state` replace `fcntl.flock(lock.fileno(), fcntl.LOCK_EX)` with `_lock(lock.fileno())`. Add `encoding="utf-8"` to `open(lock_file, "w")`, to `state_file.read_text()`, to `tmp.write_text(...)`, and to the `subprocess.run([... "rev-parse" ...], text=True)` call.

- [ ] **Step 4: Make `_make_executable` a no-op on Windows**

`src/canopy/git/hooks.py`:

```python
def _make_executable(path: Path) -> None:
    if compat.IS_WINDOWS:
        return  # NTFS has no execute bit; Git for Windows runs hooks via sh regardless
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
```

Add `from .. import compat` to the imports.

- [ ] **Step 5: Run the hook tests**

Run: `python -m pytest tests/test_hooks.py -q`
Expected: PASS on Windows and Linux.

- [ ] **Step 6: Commit**

```bash
git add src/canopy/git/templates/post-checkout.py src/canopy/git/hooks.py tests/test_hooks.py
git commit -m "fix: make post-checkout hook run on windows"
```

---

### Task 4: explicit UTF-8 on every text I/O

**Files:**
- Modify: every `*.py` under `src/canopy/` and `tests/` with `read_text()`, `write_text(...)`, text-mode `open(...)`, or `subprocess.run(..., text=True)` lacking `encoding=`
- Create: `tests/test_encoding_guard.py`
- Test: `tests/test_encoding_guard.py`, `tests/test_agent_setup.py`

**Interfaces:** none.

- [ ] **Step 1: Write the guard test**

```python
# tests/test_encoding_guard.py
"""Windows defaults text I/O to cp1252; canopy must always say utf-8."""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "canopy"

# read_text( / write_text( / open( whose argument list closes without encoding=
_CALL = re.compile(
    r"\.(read_text|write_text)\(([^()]*(?:\([^()]*\)[^()]*)*)\)"
    r"|(?<![\w.])open\(([^()]*(?:\([^()]*\)[^()]*)*)\)"
)


def _offenders():
    out = []
    for p in sorted(SRC.rglob("*.py")):
        text = p.read_text(encoding="utf-8")
        for m in _CALL.finditer(text):
            args = m.group(2) if m.group(2) is not None else m.group(3)
            if args is None or "encoding=" in args:
                continue
            if any(mode in args for mode in ('"rb"', "'rb'", '"wb"', "'wb'")):
                continue
            line = text.count("\n", 0, m.start()) + 1
            out.append(f"{p.relative_to(SRC)}:{line}: {m.group(0)[:60]}")
    return out


def test_all_text_io_declares_utf8():
    assert _offenders() == []


def test_all_text_subprocess_calls_declare_utf8():
    out = []
    for p in sorted(SRC.rglob("*.py")):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if "text=True" in line and "encoding=" not in line:
                out.append(f"{p.relative_to(SRC)}:{i}")
    assert out == []
```

- [ ] **Step 2: Run it to see the offender list**

Run: `python -m pytest tests/test_encoding_guard.py -v`
Expected: FAIL listing ~47 read/write sites and ~28 `text=True` sites.

- [ ] **Step 3: Fix every offender in `src/`**

Rules, applied mechanically:
- `x.read_text()` → `x.read_text(encoding="utf-8")`; `x.read_text("utf-8")` is already fine but normalise to the keyword form.
- `x.write_text(s)` → `x.write_text(s, encoding="utf-8")`.
- `open(p, "w")` / `open(p, "a")` / `open(p)` → add `encoding="utf-8"`.
- `subprocess.run(..., text=True, ...)` → `subprocess.run(..., text=True, encoding="utf-8", ...)`. Where the call is spread over lines, put `encoding="utf-8"` on the same line as `text=True` so the guard sees it.
- Binary opens stay as they are.

Then apply the same rules to `tests/*.py` (not enforced by the guard, but the `SKILL.md` fixture is read by tests on Windows and cp1252 breaks it).

- [ ] **Step 4: Run the guard and the setup tests**

Run: `python -m pytest tests/test_encoding_guard.py tests/test_agent_setup.py tests/test_import_boundary.py -q`
Expected: guard PASS; on Windows `test_agent_setup.py` still has the `HOME` failures (Task 5) but no `UnicodeDecodeError`.

- [ ] **Step 5: Commit**

```bash
git add -A src tests
git commit -m "fix: declare utf-8 on all text io and subprocess calls"
```

---

### Task 5: honour `HOME` for the user skills dir

**Files:**
- Modify: `src/canopy/agent_setup/__init__.py:27-29`
- Test: `tests/test_agent_setup.py`, `tests/test_doctor.py`

**Interfaces:**
- Consumes: `canopy.compat.user_home()`

- [ ] **Step 1: Run the failing tests**

Run: `python -m pytest tests/test_agent_setup.py tests/test_doctor.py -q -k "skill or setup_agent or check_status"`
Expected on Windows: `assert 'skipped' == 'installed'`, `assert True is False`, `assert 0 == 1` — the tests set `HOME` but `Path.home()` reads `USERPROFILE`, so the real user's skill dir is inspected.

- [ ] **Step 2: Use the seam**

```python
from .. import compat


def _user_skills_dir() -> Path:
    """Resolved at call time so tests that monkeypatch ``HOME`` work."""
    return compat.user_home() / ".claude" / "skills"
```

- [ ] **Step 3: Run the tests**

Run: `python -m pytest tests/test_agent_setup.py tests/test_doctor.py -q -k "skill or setup_agent or check_status"`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/canopy/agent_setup/__init__.py
git commit -m "fix: resolve user skills dir through compat.user_home"
```

---

### Task 6: shell dispatch for `run`, `install_cmd`, `preflight_cmd`

**Files:**
- Modify: `src/canopy/agent/runner.py:46-53`
- Modify: `src/canopy/actions/bootstrap.py:264-270`
- Modify: `src/canopy/integrations/precommit.py:102-121`
- Modify: `tests/test_precommit.py:55-61`, `tests/test_runner.py:63`
- Test: `tests/test_runner.py`, `tests/test_bootstrap.py`, `tests/test_precommit.py`

**Interfaces:**
- Consumes: `canopy.compat.run_shell(cmd, *, cwd, **kw)`

- [ ] **Step 1: Run the failing tests**

Run: `python -m pytest tests/test_runner.py tests/test_precommit.py tests/test_bootstrap.py -q`
Expected on Windows: `test_returns_exit_code_stdout_stderr_cwd_duration` (`assert 0 == 3` — cmd.exe ignores `;`), `test_run_precommit_runs_in_repo_cwd` (`sh` writes `/c/...` and the `C:\...` marker path was mangled), `test_feature_with_worktree_uses_worktree_path` (backslash path substring).

- [ ] **Step 2: Fix the tests' platform assumptions**

`tests/test_runner.py:63`:

```python
    assert f"/.canopy/worktrees/{slot_id}/repo-a" in Path(result["cwd"]).as_posix()
```

(add `from pathlib import Path` if missing.)

`tests/test_precommit.py:55-61` — Git Bash `pwd` prints `/c/…`; `pwd -W` prints the Windows form, and the marker path must be POSIX-quoted:

```python
def test_run_precommit_runs_in_repo_cwd(tmp_path: Path):
    """Custom command's $PWD is the repo path."""
    from canopy.compat import IS_WINDOWS
    marker = tmp_path / "marker.txt"
    pwd = "pwd -W" if IS_WINDOWS else "pwd"
    cmd = f"{pwd} > '{marker.as_posix()}'"
    run_precommit(tmp_path, augments={"preflight_cmd": cmd})
    assert marker.exists()
    assert Path(marker.read_text(encoding="utf-8").strip()).resolve() == tmp_path.resolve()
```

- [ ] **Step 3: Route the three call sites through `compat.run_shell`**

`src/canopy/agent/runner.py` — replace the `subprocess.run(command, cwd=cwd, shell=True, ...)` call:

```python
        proc = compat.run_shell(
            command,
            cwd=cwd,
            capture_output=True,
            text=True, encoding="utf-8",
            timeout=timeout_seconds,
        )
```

with `from .. import compat` added to the imports (keep `import subprocess` — `TimeoutExpired` is still caught).

`src/canopy/actions/bootstrap.py:_run_install`:

```python
    proc = compat.run_shell(
        install_cmd, cwd=worktree_path,
        capture_output=not interactive, text=True, encoding="utf-8",
    )
```

`src/canopy/integrations/precommit.py:_run_custom_preflight` — replace the `["sh", "-c", command]` call:

```python
    result = compat.run_shell(
        command,
        cwd=repo_path,
        capture_output=True,
        text=True, encoding="utf-8",
        timeout=120,
    )
```

Update that function's docstring: "by passing through the platform shell (``sh -c`` on POSIX, Git Bash on Windows)".

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_runner.py tests/test_precommit.py tests/test_bootstrap.py tests/test_import_boundary.py -q`
Expected: PASS (except `test_render_code_workspace_includes_per_repo_settings` on Windows — Task 8).

- [ ] **Step 5: Commit**

```bash
git add src/canopy/agent/runner.py src/canopy/actions/bootstrap.py src/canopy/integrations/precommit.py tests/test_runner.py tests/test_precommit.py
git commit -m "fix: dispatch agent shell commands through compat.run_shell"
```

---

### Task 7: git gate understands Windows and msys paths

**Files:**
- Modify: `src/canopy/actions/hook_gate.py:172-195`
- Test: `tests/test_hook_gate.py`

**Interfaces:**
- Consumes: `canopy.compat.IS_WINDOWS`
- Produces: `hook_gate.normalize_command_paths(command: str) -> str`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hook_gate.py`:

```python
def test_backslash_paths_survive_parsing(tmp_path):
    """Windows agents type C:\\Users\\... — shlex(posix=True) must not eat the backslashes."""
    from canopy.actions.hook_gate import resolve_segments
    from canopy.compat import IS_WINDOWS
    if not IS_WINDOWS:
        pytest.skip("backslash is an escape on POSIX")
    win = str(tmp_path / "ui").replace("/", "\\")
    segs = resolve_segments(f"git -C {win} commit -m 'x'", cwd=tmp_path)
    assert segs[0].effective_dir.resolve() == (tmp_path / "ui").resolve()


def test_msys_drive_prefix_is_rewritten():
    """Git Bash spells C:\\x as /c/x; the gate maps it back on Windows."""
    from canopy.actions.hook_gate import normalize_command_paths
    from canopy.compat import IS_WINDOWS
    out = normalize_command_paths("cd /c/Dev/ws/api && git push")
    if IS_WINDOWS:
        assert out == "cd C:/Dev/ws/api && git push"
    else:
        assert out == "cd /c/Dev/ws/api && git push"


def test_normalize_is_identity_for_posix_commands():
    from canopy.actions.hook_gate import normalize_command_paths
    from canopy.compat import IS_WINDOWS
    cmd = "cd /home/me/ws && git commit -m 'a\\nb'"
    if not IS_WINDOWS:
        assert normalize_command_paths(cmd) == cmd
```

Add `import pytest` at the top of the file if missing.

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_hook_gate.py -q`
Expected: `ImportError: cannot import name 'normalize_command_paths'`; on Windows also `test_git_dash_c_overrides_dir`, `test_git_dash_c_config_then_dash_C`, `test_absolute_cd` fail.

- [ ] **Step 3: Implement**

In `src/canopy/actions/hook_gate.py`, add after `_UNRESOLVABLE`:

```python
_MSYS_DRIVE = _re.compile(r"(?<![\w/])/([a-zA-Z])/")


def normalize_command_paths(command: str) -> str:
    """On Windows, make the command parseable by ``shlex(posix=True)``.

    Backslash separators would be consumed as escapes, and Git Bash writes
    ``C:\\x`` as ``/c/x``. Both are rewritten to ``C:/x`` before parsing.
    On POSIX the command is returned untouched — a backslash there IS an
    escape and ``/c/`` is an ordinary directory.
    """
    if not compat.IS_WINDOWS:
        return command
    out = command.replace("\\", "/")
    return _MSYS_DRIVE.sub(lambda m: f"{m.group(1).upper()}:/", out)
```

Add `from .. import compat` to the imports. In `resolve_segments`, change the loop header to `for part in split_top_level(normalize_command_paths(command)):`.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_hook_gate.py -q`
Expected: PASS on both platforms.

- [ ] **Step 5: Commit**

```bash
git add src/canopy/actions/hook_gate.py tests/test_hook_gate.py
git commit -m "fix: normalise windows and msys paths in the git gate"
```

---

### Task 8: path outputs and path assertions

**Files:**
- Modify: `src/canopy/actions/ide_workspace.py:40`
- Modify: `src/canopy/features/coordinator.py:537-538`
- Modify: `tests/test_bootstrap.py:143-145`, `tests/test_triage.py:306,353`, `tests/test_coordinator.py:434`, `tests/test_evacuate.py:39`, `tests/test_worktree_features.py:150`
- Test: the files above

**Interfaces:** none new.

- [ ] **Step 1: Run the failing tests**

Run: `python -m pytest tests/test_bootstrap.py tests/test_triage.py tests/test_coordinator.py tests/test_evacuate.py tests/test_worktree_features.py -q`
Expected on Windows: 7 failures, all `C:\\…` vs `…/…` string comparisons.

- [ ] **Step 2: Fix code that emits paths from git output**

`src/canopy/actions/ide_workspace.py` — VS Code accepts forward slashes on every OS; a `.code-workspace` written from git-listed paths must not mix separators:

```python
            "path": Path(path).as_posix(),
```

(add `from pathlib import Path` if missing.)

`src/canopy/features/coordinator.py` Priority 2 branch — git prints `C:/…`, `str(Path)` prints `C:\…`; canonicalise so callers get one form:

```python
            if repo_state.get("worktree_path"):
                paths[repo_name] = str(Path(repo_state["worktree_path"]).resolve())
```

- [ ] **Step 3: Fix the tests' string assertions**

- `tests/test_bootstrap.py:143-145` — expected `"path": Path("/wt/repo-a").as_posix()` and `Path("/wt/repo-b").as_posix()`.
- `tests/test_triage.py:306` — `assert Path(info["path"]).as_posix().endswith(f"/{r}")`.
- `tests/test_triage.py:353` — `assert ".canopy/worktrees/" in Path(info["path"]).as_posix()`.
- `tests/test_coordinator.py:434` — `assert Path(paths["repo-a"]).as_posix().endswith("worktree-1/repo-a")`.
- `tests/test_evacuate.py:39` — `assert Path(result["worktree_path"]).as_posix().endswith("worktree-1/repo-a")`.
- `tests/test_worktree_features.py:150` — `assert Path(paths["repo-a"]).resolve() == (workspace_with_feature / "repo-a").resolve()`.

Add `from pathlib import Path` where missing.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_bootstrap.py tests/test_triage.py tests/test_coordinator.py tests/test_evacuate.py tests/test_worktree_features.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/canopy/actions/ide_workspace.py src/canopy/features/coordinator.py tests/test_bootstrap.py tests/test_triage.py tests/test_coordinator.py tests/test_evacuate.py tests/test_worktree_features.py
git commit -m "fix: canonicalise git-reported paths and compare paths as paths in tests"
```

---

### Task 9: doctor on Windows

**Files:**
- Modify: `src/canopy/actions/doctor.py:348,881-913,1152,1227-1258`
- Modify: `tests/test_doctor.py:406-430` and the `chained_not_executable` test
- Test: `tests/test_doctor.py`

**Interfaces:**
- Consumes: `canopy.compat.IS_WINDOWS`

- [ ] **Step 1: Run the failing tests**

Run: `python -m pytest tests/test_doctor.py -q`
Expected on Windows: `test_check_cli_stale_when_older` (a `#!/bin/sh` fake cannot be exec'd), `test_hook_chained_unsafe_when_chained_not_executable` (`os.access(X_OK)` is always true), `test_doctor_repairs_slot_repo_worktree_missing` should pass now that Task 3 fixed the hook — re-check.

- [ ] **Step 2: Fix the tests**

Add a helper near the top of `tests/test_doctor.py`:

```python
def _fake_canopy(tmp_path, version: str) -> Path:
    """A runnable stand-in for `canopy --version` on the current platform."""
    from canopy.compat import IS_WINDOWS
    if IS_WINDOWS:
        fake = tmp_path / "canopy.cmd"
        fake.write_text(f"@echo canopy {version}\r\n", encoding="utf-8")
    else:
        fake = tmp_path / "canopy"
        fake.write_text(f"#!/bin/sh\necho 'canopy {version}'\n", encoding="utf-8")
        fake.chmod(0o755)
    return fake
```

Use `fake = _fake_canopy(tmp_path, "0.0.1")` in `test_check_cli_stale_when_older`, `fake = _fake_canopy(tmp_path, __version__)` in `test_check_cli_stale_when_current`, and the same helper in any `canopy-mcp` stale test that writes a shell fake.

Decorate `test_hook_chained_unsafe_when_chained_not_executable` with:

```python
@pytest.mark.skipif(sys.platform.startswith("win"), reason="no execute bit on NTFS")
```

(add `import sys` / `import pytest` if missing).

- [ ] **Step 3: Orphan-MCP check and reaper**

`_list_orphan_canopy_mcp_pids`: first line of the body:

```python
    if compat.IS_WINDOWS:
        return []   # no `ps`, and "reparented to PID 1" has no Windows equivalent
```

In the reaper (around line 1254) replace `signal.SIGKILL` with `signal.SIGTERM if compat.IS_WINDOWS else signal.SIGKILL` (Windows only defines `SIGTERM`; `os.kill` maps it to `TerminateProcess`). Add `from .. import compat`.

At `doctor.py:348` and `:1152`, guard the check with `not compat.IS_WINDOWS and not os.access(chained, os.X_OK)` so the "chained hook not executable" issue is never reported on Windows.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_doctor.py tests/test_import_boundary.py -q`
Expected: PASS (one skip on Windows).

- [ ] **Step 5: Commit**

```bash
git add src/canopy/actions/doctor.py tests/test_doctor.py
git commit -m "fix: make doctor process checks and version probes work on windows"
```

---

### Task 10: detached background bootstrap

**Files:**
- Modify: `src/canopy/actions/slot_bootstrap.py:44-50`
- Test: `tests/test_bootstrap.py`

**Interfaces:**
- Consumes: `canopy.compat.detached_popen_kwargs()`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_bootstrap.py`:

```python
def test_spawn_deps_background_uses_platform_detach(monkeypatch, workspace_with_bootstrap_config):
    import subprocess
    from canopy import compat
    from canopy.actions import slot_bootstrap
    from canopy.workspace.workspace import Workspace
    from canopy.workspace.config import load_config
    ws = Workspace(load_config(workspace_with_bootstrap_config))
    seen = {}

    def fake_popen(argv, **kw):
        seen.update(kw)
        class P:
            pid = 1
        return P()

    monkeypatch.delenv("CANOPY_NO_BG_BOOTSTRAP", raising=False)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    slot_bootstrap._spawn_deps_background(ws, "auth-flow", "worktree-1")
    for k, v in compat.detached_popen_kwargs().items():
        assert seen[k] == v
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_bootstrap.py -q -k spawn_deps_background`
Expected on Windows: `KeyError: 'creationflags'`. On Linux: PASS already (kwargs coincide) — fine; the test guards the Windows branch.

- [ ] **Step 3: Implement**

```python
    subprocess.Popen(
        [sys.executable, "-m", "canopy.cli.main", "worktree-bootstrap",
         "--deps", feature, "--_slot", sid],
        cwd=str(workspace.config.root),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **compat.detached_popen_kwargs(),
    )
```

with `from .. import compat`.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_bootstrap.py tests/test_import_boundary.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/canopy/actions/slot_bootstrap.py tests/test_bootstrap.py
git commit -m "fix: detach background bootstrap with platform-specific popen flags"
```

---

### Task 11: dependency pin, CI matrix, docs

**Files:**
- Modify: `pyproject.toml:24-28`
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md` (install section), `CHANGELOG.md`, `CLAUDE.md` (architecture tree + Important Implementation Details)

**Interfaces:** none.

- [ ] **Step 1: Pin `mcp`**

`pyproject.toml`:

```toml
dependencies = [
    "tomli>=2.0; python_version < '3.11'",
    "mcp>=1.0,<2",
    "rich>=13.0",
]
```

Verify: `pip install -e ".[dev]"` then `python -c "import canopy.mcp.server"` → no error.

- [ ] **Step 2: CI matrix**

`.github/workflows/ci.yml` — replace the `python-tests` job header and matrix:

```yaml
  python-tests:
    name: pytest (${{ matrix.os }}, Python ${{ matrix.python }})
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, windows-latest]
        python: ["3.10", "3.11", "3.12"]
```

and in the "Configure Git" step add `git config --global core.autocrlf false` after the `init.defaultBranch` line. Everything else stays.

- [ ] **Step 3: Docs**

- `README.md` install section: add one line — "Windows: supported natively (Python 3.10+, Git for Windows). `canopy run`, `install_cmd` and `preflight_cmd` execute through Git Bash."
- `CHANGELOG.md`: new top section `## Unreleased` with bullets: native Windows support (`compat` seam, hook template, UTF-8 I/O, gate path normalisation, doctor), `mcp` pinned `<2`, CI runs on `windows-latest`.
- `CLAUDE.md` → "Important Implementation Details": add bullet
  "**Platform seam:** `compat.py` is the only module allowed to import `fcntl`/`msvcrt` or check `sys.platform` (plus the standalone hook template). Shell commands go through `compat.run_shell` (Git Bash on Windows). All text I/O passes `encoding=\"utf-8\"`; both rules are enforced by `tests/test_import_boundary.py` and `tests/test_encoding_guard.py`."
  and in the architecture tree add `├── compat.py                # platform seam: locks, shell dispatch, home dir, detached spawn`.

- [ ] **Step 4: Verify and commit**

Run: `python -m pytest tests/test_import_boundary.py tests/test_encoding_guard.py -q`
Expected: PASS.

```bash
git add pyproject.toml .github/workflows/ci.yml README.md CHANGELOG.md CLAUDE.md
git commit -m "chore: pin mcp<2, add windows to ci matrix, document platform seam"
```

---

### Task 12: full Windows run and residual triage

**Files:**
- Whatever the run reveals (expected: none, or path comparisons per spec §5)

- [ ] **Step 1: Full suite on Windows**

Run: `python -m pytest tests/ -q -p no:cacheprovider > pytest-windows.log 2>&1; tail -3 pytest-windows.log`
Expected: `N passed, M skipped` with 0 failed. (~22 min.)

- [ ] **Step 2: If failures remain**

For each `FAILED`, read the `E ` lines. Classify:
- `C:\…` vs `…/…` comparison → apply spec §5: in code use `Path(x).resolve()`; in tests use `Path(x).as_posix()`.
- `fcntl` / `sh` / `ps` / `signal` → the offending call site missed a task above; route it through `compat`.
- `UnicodeDecodeError` → a text I/O site missed `encoding="utf-8"` (the guard test only covers `src/`; check `tests/`).

Fix, re-run only the failing file, commit with `fix: <what>`. Delete `pytest-windows.log` before committing (it is not part of the repo).

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin feature/windows-support
gh pr create --title "feat: native windows support" --body-file docs/superpowers/specs/2026-09-02-windows-port-design.md
```

Expected: CI green on `ubuntu-latest` and `windows-latest`.
