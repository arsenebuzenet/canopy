#!/usr/bin/env python3
# __CANOPY_HOOK_MARKER__ post-checkout v1
"""Canopy post-checkout hook — records HEAD state to .canopy/state/heads.json.

Installed by `canopy hooks install`. Never blocks git operations on errors.
Chains to a pre-existing post-checkout.canopy-chained if present.
"""
import errno
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl

    def _lock(fd):
        fcntl.flock(fd, fcntl.LOCK_EX)
        return True
except ImportError:  # Windows
    import msvcrt

    _LOCK_TIMEOUT = 10.0

    def _lock(fd):
        # LK_LOCK gives up after ~10s and raises OSError, unlike flock(LOCK_EX),
        # which blocks indefinitely — so poll LK_NBLCK ourselves. The poll is
        # bounded and only retries contention (EACCES/EDEADLOCK): an infinite
        # loop is not an exception, so the fail-open wrapper below would not
        # catch it and a wedged holder would hang `git checkout` outright.
        deadline = time.monotonic() + _LOCK_TIMEOUT
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return True
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EDEADLOCK):
                    raise
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.05)

# Substituted at install time.
CANOPY_REPO = "__CANOPY_REPO__"
CANOPY_WORKSPACE_ROOT = Path("__CANOPY_WORKSPACE_ROOT__")


def _record_state() -> None:
    if len(sys.argv) < 4:
        return
    prev_sha, new_sha, is_branch_checkout = sys.argv[1], sys.argv[2], sys.argv[3]
    # Only record on branch checkouts (not file checkouts).
    if is_branch_checkout != "1":
        return

    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, encoding="utf-8", check=False,
    ).stdout.strip()
    if not branch or branch == "HEAD":
        return  # detached; skip

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = {"branch": branch, "sha": new_sha, "prev_sha": prev_sha, "ts": ts}

    state_dir = CANOPY_WORKSPACE_ROOT / ".canopy" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "heads.json"
    lock_file = state_dir / "heads.json.lock"

    with open(lock_file, "w", encoding="utf-8") as lock:
        if not _lock(lock.fileno()):
            return          # lock wedged: drop this record rather than stall git
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            state = {}
        state[CANOPY_REPO] = entry
        tmp = state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, state_file)


def _chain_existing() -> None:
    chained = Path(__file__).parent / "post-checkout.canopy-chained"
    if not (chained.is_file() and os.access(chained, os.X_OK)):
        return
    if os.name != "nt":
        os.execv(str(chained), [str(chained), *sys.argv[1:]])
        return
    # Windows has no kernel-level shebang dispatch, so os.execv can't run a
    # script directly (CreateProcess raises "Exec format error"). Resolve
    # the interpreter from the shebang line ourselves and shell out to it.
    with open(chained, encoding="utf-8") as f:
        first_line = f.readline()
    interpreter = None
    if first_line.startswith("#!"):
        parts = first_line[2:].strip().split()
        if parts:
            is_env = os.path.basename(parts[0]) == "env"
            interpreter = parts[-1] if is_env else os.path.basename(parts[0])
    if (interpreter and interpreter.startswith("python")
            and shutil.which(interpreter) is None):
        interpreter = sys.executable    # `python3` is rarely on a Windows PATH
    cmd = [interpreter, str(chained)] if interpreter else [str(chained)]
    try:
        result = subprocess.run(cmd + sys.argv[1:], check=False)
    except Exception:
        return                          # never block git on a broken chain
    sys.exit(result.returncode)


if __name__ == "__main__":
    try:
        _record_state()
    except Exception:
        pass  # never block git on hook failure
    _chain_existing()
