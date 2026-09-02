# Windows native support — design

**Goal.** canopy runs on native Windows (Python 3.10+, Git for Windows, Claude
Code for Windows) with full feature parity: CLI, `canopy-mcp`, git
`post-checkout` hook, Claude Code enforcement hooks, doctor. Success = `pytest
tests/` green on Windows without `PYTHONUTF8=1`, and CI green on
`windows-latest` alongside the existing Linux matrix.

**Baseline.** Upstream `0049916` on Windows: `import fcntl` at module level in
`actions/slots.py`, `management/historian.py` and the `post-checkout` template
aborts every slot operation and every git checkout in a hooked repo. With a
throwaway `fcntl` shim + `PYTHONUTF8=1` + `mcp<2`: 31 failed / 956 passed.

## 1. One platform seam: `src/canopy/compat.py`

All OS-conditional code lives in this module. Nothing else may import
`fcntl`, `msvcrt`, or branch on `sys.platform`.

| API | POSIX | Windows |
|---|---|---|
| `IS_WINDOWS` | `False` | `True` |
| `lock(f)` / `unlock(f)` — `f` is a file object or fd | `fcntl.flock(LOCK_EX / LOCK_UN)` | `msvcrt.locking(LK_NBLCK / LK_UNLCK, 1)` on byte 0 (offset saved and restored, so append-mode handles contend on the same byte); the poll retries only `EACCES`/`EDEADLOCK`; unlock swallows `OSError` |
| `run_shell(cmd: str, cwd, **subprocess_kw)` | `subprocess.run(cmd, shell=True, ...)` | `subprocess.run([bash, "-c", cmd], ...)` where `bash` = `find_bash()`; raises `BlockerError(code="bash_not_found")` if no bash — never a silent cmd.exe fallback |
| `find_bash() -> Path \| None` | `None` (unused) | Git for Windows' bash: found by walking up from `git` on PATH (up to 4 parents, checking `bin/bash.exe` and `usr/bin/bash.exe` at each — `git` may resolve to `<Git>/cmd/` or `<Git>/mingw64/bin/`), then `shutil.which("bash")` **excluding** `System32\bash.exe` (that is WSL's launcher) |
| `user_home() -> Path` | `Path.home()` | `Path(os.environ["HOME"])` if `HOME` set, else `Path.home()` — `Path.home()` ignores `HOME` on Windows, so tests that monkeypatch `HOME` and Git-Bash users both get the expected dir |
| `same_path(a, b) -> bool` | `Path(a).resolve() == Path(b).resolve()` | idem — case-insensitive via `os.path.normcase` |
| `detached_popen_kwargs() -> dict` | `{"start_new_session": True}` | `{"creationflags": DETACHED_PROCESS \| CREATE_NEW_PROCESS_GROUP}` |

Why bash and not cmd.exe on Windows: Claude Code's Bash tool on Windows is Git
Bash, so the agent writes POSIX command lines. `run`, `install_cmd` and
`preflight_cmd` must interpret them identically. cmd.exe would break `;`,
`&&`, `>&2`, `$VAR`.

## 2. Callers migrated to the seam

- `actions/slots.py`, `management/historian.py` — `compat.lock/unlock`.
- `agent/runner.py`, `actions/bootstrap.py:_run_install`,
  `integrations/precommit.py:_run_custom_preflight` — `compat.run_shell`.
- `agent_setup/__init__.py:_user_skills_dir` and any other `Path.home()` that
  a test monkeypatches via `HOME` — `compat.user_home()`. `mcp/client.py`
  token cache keeps `Path.home()` (real home, never tested via `HOME`).
- `actions/slot_bootstrap.py:_spawn_deps_background` — `compat.detached_popen_kwargs()`.
- `actions/doctor.py` orphan-MCP check: `ps -eo pid,ppid,command` and
  "PPID == 1" have no Windows equivalent. On Windows the check reports no
  orphans and the repair is a no-op; `SIGKILL` → `signal.SIGTERM` on Windows
  (`os.kill` maps it to `TerminateProcess`).
- `actions/ide_workspace.py` — folder paths written with `Path.as_posix()`;
  VS Code accepts forward slashes on every OS and a `.code-workspace` built
  from git-reported paths must not mix separators.

## 3. Git `post-checkout` hook template

The template is a standalone script; it cannot import `canopy.compat`. It
embeds the same lock shim inline (`try: import fcntl … except ImportError:
import msvcrt`). Shebang stays `#!/usr/bin/env python3` — Git for Windows runs
hooks through its own `sh`, which resolves `python3` from PATH. The
installer's `_make_executable` is a no-op on Windows (`chmod` bits are
meaningless there); the test asserting "chained hook not executable ⇒ unsafe"
is skipped on Windows because `os.access(X_OK)` is always true there.

## 4. Text I/O encoding

Every `read_text()` / `write_text()` / text-mode `open()` passes
`encoding="utf-8"`. Windows defaults to cp1252 and the bundled `SKILL.md`
contains non-cp1252 bytes, so without this `canopy setup-agent` crashes.
`subprocess.run(..., text=True)` calls that read git/gh output pass
`encoding="utf-8"` too (git emits UTF-8 regardless of console code page).
Applies across `src/` and `tests/`.

## 5. Path comparison rules

Code never compares path **strings**. Git prints forward-slash paths
(`C:/Users/…`) while `str(Path)` yields backslashes; string equality or
`in` checks silently fail. Rules:

- Comparisons: `compat.same_path(a, b)` or `Path(a).resolve()` equality.
- Containment: `Path(child).resolve().is_relative_to(Path(parent).resolve())`.
- Known sites from the Windows run: `git/repo.py` worktree-list parsing,
  `actions/migrate_slots.py`, `actions/evacuate.py`, `management/triage.py`,
  `features/coordinator.py:resolve_paths`, `actions/registry.py`.
- Tests that assert `'.canopy/worktrees/' in path` or `path.endswith('/repo-a')`
  are rewritten to compare `Path(x).as_posix()` or `Path` objects.

## 6. Claude Code git gate (`actions/hook_gate.py`)

The gate receives the Bash tool's command string and `cwd`. On Windows the
command may contain `C:\Users\…` (backslashes), `C:/Users/…`, or msys
`/c/Users/…` paths. `shlex.split(posix=True)` eats backslashes. Before
parsing, on Windows the gate normalises every backslash to `/` and rewrites
`^/([a-zA-Z])/` msys drive prefixes to `X:/`. Resolution and comparison then go
through the rules in §5.

## 7. Dependencies and CI

- `pyproject.toml`: `mcp>=1.0,<2` — `mcp` 2.x renamed `FastMCP` to
  `MCPServer`; the server cannot start on 2.x today. Migration to 2.x is out
  of scope.
- `.github/workflows/ci.yml`: add `windows-latest` to the matrix. Windows job
  sets `git config --global core.autocrlf false` so the fixtures' byte-level
  assertions hold. No `PYTHONUTF8` — §4 makes it unnecessary.

## 8. Out of scope

- WSL interop, mixed Windows/WSL git on the same checkout.
- `mcp` 2.x API migration.
- Long-path (`>260`) support beyond what Python/Git already do with
  `LongPathsEnabled`.

## 9. Testing

TDD per task: reproduce on Windows with a failing test (or the existing failing
test), implement, run the affected test file, keep the Linux behaviour
identical (CI proves it). Full `pytest tests/` on Windows (~22 min) once at the
end; CI on both OSes is the acceptance gate.
