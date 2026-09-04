"""One-shot migration: pre-3.0 feature-named worktrees → 3.0 generic slots.

Refuses to run if slots.json already exists. Idempotent only in the
"nothing to do" sense — once migrated, calling again raises.

Steps:
  1. Read old active_feature.json (preserve last_touched + canonical).
  2. Scan .canopy/worktrees/<feature>/<repo>/ on disk.
  3. Allocate sequential slot ids (worktree-1, worktree-2, ...).
  4. `git worktree move` each repo dir into its slot.
  5. Rewrite canopy.toml: max_worktrees → slots.
  6. Write slots.json.
  7. Delete active_feature.json.
  8. rmdir the now-empty feature parent dirs.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[import-not-found]

from ..git import repo as git
from . import slots as slots_mod
from .errors import BlockerError


class AlreadyMigratedError(Exception):
    pass


class NotLegacyError(Exception):
    """No old state and no feature-named worktrees — nothing to migrate."""


def migrate(workspace_root: Path) -> dict[str, Any]:
    """Migrate a pre-3.0 canopy workspace to the 3.0 slot layout.

    Takes a path (not a Workspace) because the legacy canopy.toml has
    max_worktrees which load_config rejects after T3.

    Returns:
        {moved: [{from, to}, ...], slots: {slot_id: feature}, canonical, slot_count}

    Raises:
        AlreadyMigratedError: slots.json already exists.
        NotLegacyError: nothing to migrate (no active_feature.json and no feature dirs).
    """
    root = Path(workspace_root)
    slots_json = root / ".canopy/state/slots.json"
    if slots_json.exists():
        raise AlreadyMigratedError(f"slots.json already exists at {slots_json}")

    toml_path = root / "canopy.toml"
    if not toml_path.exists():
        raise NotLegacyError(f"no canopy.toml at {toml_path}")

    # Parse the toml directly (load_config rejects max_worktrees per T3).
    with open(toml_path, "rb") as f:
        toml_data = tomllib.load(f)
    repos_cfg = toml_data.get("repos", [])
    repo_paths_by_name: dict[str, Path] = {}
    for r in repos_cfg:
        name = r.get("name")
        rel_path = r.get("path", name)
        if name:
            repo_paths_by_name[name] = root / rel_path

    old_active = root / ".canopy/state/active_feature.json"
    wt_base = root / ".canopy/worktrees"

    if not old_active.exists() and not wt_base.is_dir():
        raise NotLegacyError("no active_feature.json and no .canopy/worktrees/")

    old: dict[str, Any] = {}
    if old_active.exists():
        try:
            old = json.loads(old_active.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            old = {}

    # 1. Inventory feature-named dirs (skip any already-named worktree-N dirs)
    legacy: dict[str, list[str]] = {}  # feature → list of repos
    if wt_base.is_dir():
        for feat_dir in sorted(wt_base.iterdir()):
            if not feat_dir.is_dir():
                continue
            if re.fullmatch(r"worktree-\d+", feat_dir.name):
                raise AlreadyMigratedError(
                    f"found slot dir {feat_dir.name} without slots.json"
                )
            repos = sorted(d.name for d in feat_dir.iterdir()
                           if d.is_dir() and (d / ".git").exists())
            if repos:
                legacy[feat_dir.name] = repos

    # 2. Allocate slot ids
    slot_assignment: dict[str, str] = {}
    for i, feature in enumerate(sorted(legacy.keys()), start=1):
        slot_assignment[feature] = f"worktree-{i}"

    # 3a. Dry-run preflight: validate every move target BEFORE touching disk.
    # Avoids the half-migrated state that wedges the user (some dirs at the
    # new slot path, others at the old feature path, no slots.json yet).
    preflight_issues: list[dict[str, Any]] = []
    for feature, repos in legacy.items():
        slot_id = slot_assignment[feature]
        for repo_name in repos:
            old_path = wt_base / feature / repo_name
            new_path = wt_base / slot_id / repo_name
            main_repo = repo_paths_by_name.get(repo_name)
            if main_repo is None or not main_repo.exists():
                preflight_issues.append({
                    "kind": "main_repo_missing", "repo": repo_name,
                    "feature": feature, "main_repo": str(main_repo) if main_repo else None,
                })
                continue
            if not old_path.exists():
                preflight_issues.append({
                    "kind": "source_missing", "repo": repo_name,
                    "feature": feature, "path": str(old_path),
                })
                continue
            if not (old_path / ".git").exists():
                preflight_issues.append({
                    "kind": "source_not_a_worktree", "repo": repo_name,
                    "feature": feature, "path": str(old_path),
                })
                continue
            if new_path.exists():
                preflight_issues.append({
                    "kind": "destination_exists", "repo": repo_name,
                    "feature": feature, "path": str(new_path),
                })
                continue
            # Validate the worktree is registered with git (catches locked worktrees).
            try:
                listed = git.worktree_list(main_repo)
                listed_paths = {Path(w.get("path", "")).resolve() for w in listed}
                if old_path.resolve() not in listed_paths:
                    preflight_issues.append({
                        "kind": "worktree_unregistered", "repo": repo_name,
                        "feature": feature, "path": str(old_path),
                    })
            except Exception as e:  # noqa: BLE001 — surface as a single issue
                preflight_issues.append({
                    "kind": "worktree_list_failed", "repo": repo_name,
                    "feature": feature, "error": str(e),
                })

    if preflight_issues:
        raise BlockerError(
            code="migration_preflight_failed",
            what=(
                f"{len(preflight_issues)} issue(s) detected before migration could begin — "
                f"refusing to start so the workspace stays in the pre-3.0 layout"
            ),
            details={"issues": preflight_issues},
        )

    # 3b. Move each repo dir via `git worktree move`. If any move fails
    # mid-loop, attempt to undo the completed ones so the user lands back
    # on the pre-3.0 layout rather than a half-migrated wedge.
    moved: list[dict[str, str]] = []
    try:
        for feature, repos in legacy.items():
            slot_id = slot_assignment[feature]
            (wt_base / slot_id).mkdir(parents=True, exist_ok=True)
            for repo_name in repos:
                old_path = wt_base / feature / repo_name
                new_path = wt_base / slot_id / repo_name
                main_repo = repo_paths_by_name.get(repo_name)
                if main_repo is None or not main_repo.exists():
                    continue
                git.worktree_move(main_repo, old_path, new_path)
                moved.append({"from": str(old_path), "to": str(new_path)})
    except Exception as move_err:  # noqa: BLE001
        # Best-effort rollback: move each completed entry back to its old path.
        unrolled: list[dict[str, str]] = []
        rollback_failures: list[dict[str, str]] = []
        for m in reversed(moved):
            new_path = Path(m["to"])
            old_path = Path(m["from"])
            # Figure out which repo this was so we can address the main repo.
            repo_name = new_path.name
            main_repo = repo_paths_by_name.get(repo_name)
            if main_repo is None:
                rollback_failures.append({**m, "error": "no main repo"})
                continue
            try:
                old_path.parent.mkdir(parents=True, exist_ok=True)
                git.worktree_move(main_repo, new_path, old_path)
                unrolled.append({"from": str(new_path), "to": str(old_path)})
            except Exception as e:  # noqa: BLE001
                rollback_failures.append({**m, "error": str(e)})
        # Best-effort cleanup of empty slot dirs we created during the failed pass.
        for feature in legacy:
            slot_id = slot_assignment[feature]
            slot_dir = wt_base / slot_id
            try:
                if slot_dir.exists() and not any(slot_dir.iterdir()):
                    slot_dir.rmdir()
            except OSError:
                pass
        if rollback_failures:
            raise BlockerError(
                code="migration_partial",
                what=(
                    "migration failed mid-loop AND rollback could not return"
                    " every dir to its pre-3.0 location — manual cleanup required"
                ),
                details={
                    "underlying_error": str(move_err),
                    "moved_dirs": moved,
                    "unrolled_dirs": unrolled,
                    "rollback_failures": rollback_failures,
                },
            )
        raise BlockerError(
            code="migration_aborted",
            what=(
                "migration failed mid-loop; reverted to pre-3.0 layout — "
                "re-run after resolving the underlying error"
            ),
            details={
                "underlying_error": str(move_err),
                "rolled_back_dirs": unrolled,
            },
        )

    # 4. Rewrite canopy.toml: max_worktrees → slots
    text = toml_path.read_text(encoding="utf-8")
    new_text, n = re.subn(
        r"(?m)^(\s*)max_worktrees(\s*=\s*\d+)\s*$",
        r"\1slots\2",
        text,
    )
    if n == 0 and not re.search(r"(?m)^\s*slots\s*=", text):
        # Insert default `slots = 2` under [workspace]
        new_text = re.sub(
            r"(?m)^(\[workspace\][^\n]*\n(?:[^\[\n][^\n]*\n)*)",
            r"\1slots = 2\n",
            text, count=1,
        )
    toml_path.write_text(new_text, encoding="utf-8")

    # 5. Build slots.json
    canonical_feature = old.get("feature")
    canonical: slots_mod.CanonicalEntry | None = None
    if canonical_feature:
        per_repo = old.get("per_repo_paths") or {}
        if isinstance(per_repo, dict) and all(Path(p).exists() for p in per_repo.values()):
            canonical = slots_mod.CanonicalEntry(
                feature=canonical_feature,
                activated_at=old.get("activated_at", slots_mod.now_iso()),
                per_repo_paths=dict(per_repo),
            )

    slot_entries = {
        slot_assignment[feat]: slots_mod.SlotEntry(
            feature=feat, occupied_at=slots_mod.now_iso(),
        )
        for feat in legacy
    }

    last_touched = {
        str(k): str(v) for k, v in (old.get("last_touched") or {}).items()
    }

    # Re-parse the rewritten toml for slot_count
    with open(toml_path, "rb") as f:
        new_toml_data = tomllib.load(f)
    slot_count = int(new_toml_data.get("workspace", {}).get("slots", 2))

    state = slots_mod.SlotState(
        slot_count=slot_count,
        canonical=canonical,
        previous_canonical=old.get("previous_feature"),
        slots=slot_entries,
        last_touched=last_touched,
    )

    # Write slots.json directly (write_state requires a Workspace object)
    state_path = root / ".canopy/state/slots.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
    tmp.replace(state_path)

    # 6. Delete active_feature.json
    if old_active.exists():
        old_active.unlink()

    # 7. rmdir the now-empty feature parent dirs
    for feature in legacy:
        old_dir = wt_base / feature
        try:
            old_dir.rmdir()
        except OSError:
            pass  # not empty — leave for the user to clean up

    return {
        "moved": moved,
        "slots": {sid: e.feature for sid, e in slot_entries.items()},
        "canonical": canonical_feature,
        "slot_count": slot_count,
    }
