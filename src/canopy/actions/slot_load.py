"""slot_load / slot_clear / slot_swap — slot-targeted operations (T16, T17).

These complement `switch` by letting the caller manipulate warm slots
without changing canonical. Useful for the dashboard's load/clear/swap
buttons and for pre-warming.
"""
from __future__ import annotations

from typing import Any

from ..git import repo as git
from ..workspace.workspace import Workspace
from .aliases import resolve_feature, repos_for_feature
from .errors import BlockerError, FixAction
from .externals import ensure_external_links
from . import slots as slots_mod


def _ensure_consistent_slot_state(workspace: Workspace) -> None:
    """Refuse slot operations when a prior op left an in_flight marker.

    Mirrors switch._ensure_consistent — a prior slot op partially failed
    and the workspace is in a half-flipped state. Continuing would
    compound the inconsistency. Surface a structured blocker and let the
    user run `canopy doctor` to inspect.
    """
    state = slots_mod.read_state(workspace)
    if state is None or state.in_flight is None:
        return
    inf = state.in_flight
    raise BlockerError(
        code="slot_state_inconsistent",
        what=(
            f"a prior {inf.get('operation', 'slot op')} failed in "
            f"repo '{inf.get('failed_repo')}' — workspace is partially flipped"
        ),
        details={"in_flight": dict(inf)},
        fix_actions=[
            FixAction(
                action="doctor",
                args={},
                safe=True,
                preview=(
                    "run `canopy doctor` to inspect slots.json and the "
                    "in_flight marker; resolve manually, then clear it"
                ),
            ),
        ],
    )


def slot_load(
    workspace: Workspace,
    feature: str,
    *,
    slot_id: str | None = None,
    replace: bool = False,
    bootstrap: bool = False,
) -> dict[str, Any]:
    """Warm a cold feature into a slot without changing canonical.

    Raises BlockerError for:
      - feature_is_canonical: feature is already canonical
      - feature_already_warm: feature is already in a warm slot
      - slot_occupied: target slot is occupied and replace=False
      - unknown_slot: slot_id is out of range
      - worktree_cap_reached: all slots full and slot_id was not given
    """
    _ensure_consistent_slot_state(workspace)
    feature_name = resolve_feature(workspace, feature)
    state = slots_mod.read_state(workspace) or slots_mod.SlotState(
        slot_count=workspace.config.slots,
    )

    # Refuse if already canonical — it's loaded more strongly than warm.
    if state.canonical and state.canonical.feature == feature_name:
        raise BlockerError(
            code="feature_is_canonical",
            what=f"feature '{feature_name}' is already canonical — use `switch` to move it",
        )

    # Refuse if already warm in any slot.
    existing_slot = slots_mod.slot_for_feature(workspace, feature_name)
    if existing_slot is not None:
        raise BlockerError(
            code="feature_already_warm",
            what=f"feature '{feature_name}' is already warm in {existing_slot}",
            details={"current_slot": existing_slot, "requested_slot": slot_id},
            fix_actions=[
                FixAction(
                    action="slot_swap",
                    args={"slot_a": existing_slot, "slot_b": slot_id or "?"},
                    safe=False,
                    preview="use `slot swap` to move between slots",
                ),
            ],
        )

    # Resolve slot id — pick lowest free, or use caller's choice.
    if slot_id is None:
        chosen = slots_mod.allocate_slot(state)
        if chosen is None:
            raise BlockerError(
                code="worktree_cap_reached",
                what=f"all {state.slot_count} slots are occupied",
                fix_actions=[
                    FixAction(
                        action="slot_clear",
                        args={"slot_id": "<LRU>"},
                        safe=False,
                        preview="clear an LRU slot first",
                    ),
                ],
            )
        slot_id = chosen

    # Validate slot id range.
    valid_slots = {f"worktree-{i}" for i in range(1, state.slot_count + 1)}
    if slot_id not in valid_slots:
        raise BlockerError(
            code="unknown_slot",
            what=f"slot '{slot_id}' out of range (cap={state.slot_count})",
        )

    # If occupied: evict with replace=True, else refuse.
    evicted: dict | None = None
    if slot_id in state.slots:
        if not replace:
            raise BlockerError(
                code="slot_occupied",
                what=f"{slot_id} is occupied by '{state.slots[slot_id].feature}'",
                details={"slot": slot_id, "occupant": state.slots[slot_id].feature},
                fix_actions=[
                    FixAction(
                        action="slot_load",
                        args={"feature": feature_name, "slot_id": slot_id, "replace": True},
                        safe=False,
                        preview="evict occupant to cold and load this feature",
                    ),
                ],
            )
        evicted = slot_clear(workspace, slot_id)

    # Re-read state after potential eviction.
    state = slots_mod.read_state(workspace) or slots_mod.SlotState(
        slot_count=workspace.config.slots,
    )

    # Add worktrees per repo — iterate repos_for_feature (respects partial scope).
    # Refuse to auto-allocate "all repos" for unregistered features — that
    # silently over-scopes partial-scope work. Force the user to declare
    # intent via `canopy feature create <name> --repos <list>` first.
    repo_branches = repos_for_feature(workspace, feature_name)
    if not repo_branches:
        raise BlockerError(
            code="ambiguous_feature_scope",
            what=(
                f"feature '{feature_name}' is not yet registered — "
                f"run `canopy feature create <name> --repos <list>` first"
            ),
            details={"feature": feature_name},
        )
    # Links first: a slot whose repos cannot resolve their siblings is not
    # loadable, and a BlockerError here leaves no half-built slot behind.
    ensure_external_links(workspace)

    per_repo: list[dict] = []
    for repo_name, branch in repo_branches.items():
        try:
            repo = workspace.get_repo(repo_name)
        except KeyError:
            continue
        if not git.branch_exists(repo.abs_path, branch):
            git.create_branch(repo.abs_path, branch,
                              start_point=repo.config.default_branch)
        dest = slots_mod.slot_worktree_path(workspace, slot_id, repo_name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        git.worktree_add(repo.abs_path, dest, branch, create_branch=False)
        per_repo.append({
            "repo": repo_name,
            "branch": branch,
            "worktree_path": str(dest.resolve()),
        })

    # Persist slot entry + bump last_touched.
    now = slots_mod.now_iso()
    state.slots[slot_id] = slots_mod.SlotEntry(feature=feature_name, occupied_at=now)
    state.last_touched[feature_name] = now
    slots_mod.write_state(workspace, state)

    # Optional bootstrap.
    bootstrap_result = None
    if bootstrap or getattr(workspace.config, "bootstrap_default", False):
        try:
            from . import bootstrap as bs
            bootstrap_result = bs.bootstrap_feature(workspace, feature_name)
        except (ImportError, AttributeError) as e:
            bootstrap_result = {"skipped": f"bootstrap module not available: {e}"}
        except Exception as e:
            bootstrap_result = {"error": str(e)}

    return {
        "feature": feature_name,
        "slot_id": slot_id,
        "per_repo": per_repo,
        "evicted": evicted,
        "bootstrap": bootstrap_result,
    }


def slot_clear(workspace: Workspace, slot_id: str) -> dict[str, Any]:
    """Evict a feature from a slot to cold.

    Creates a feature-tagged stash for any dirty work before removing the
    worktree (best-effort — stash failure does not block removal). The branch
    is kept; only the warm worktree is removed.
    """
    _ensure_consistent_slot_state(workspace)
    state = slots_mod.read_state(workspace)
    if state is None or slot_id not in state.slots:
        raise BlockerError(
            code="empty_slot",
            what=f"slot '{slot_id}' is empty — nothing to clear",
        )
    feature = state.slots[slot_id].feature
    repo_branches = repos_for_feature(workspace, feature) or {
        r.config.name: feature for r in workspace.repos
    }
    cleared: list[dict] = []
    for repo_name in repo_branches:
        try:
            repo = workspace.get_repo(repo_name)
        except KeyError:
            continue
        slot_path = slots_mod.slot_worktree_path(workspace, slot_id, repo_name)
        if not slot_path.exists():
            cleared.append({"repo": repo_name, "status": "missing", "slot_path": str(slot_path)})
            continue
        # Tag any dirty work with a feature-tagged stash before removing the worktree.
        # Critical: if the slot is dirty AND stash fails, refuse to remove the
        # worktree — silent data loss is never acceptable.
        stash_ref = None
        stash_failed = False
        try:
            from . import evacuate as evac
            stash_ref = evac.stash_for_evacuation(workspace, feature, repo_name, slot_path)
        except Exception:
            stash_failed = True
        if stash_failed:
            try:
                dirty = git.is_dirty(slot_path)
            except Exception:
                dirty = True  # conservative: assume dirty when we can't tell
            if dirty:
                raise BlockerError(
                    code="evict_stash_failed",
                    what=(
                        f"slot '{slot_id}' repo '{repo_name}' is dirty but stash failed; "
                        f"refusing to remove worktree"
                    ),
                    details={"slot": slot_id, "repo": repo_name,
                             "slot_path": str(slot_path)},
                )
        try:
            git.worktree_remove(repo.abs_path, slot_path, force=True)
        except Exception as e:
            cleared.append({"repo": repo_name, "status": "remove_failed",
                             "slot_path": str(slot_path), "error": str(e)})
            continue
        cleared.append({
            "repo": repo_name, "status": "cleared",
            "slot_path": str(slot_path), "stash_ref": stash_ref,
        })
    # Re-read to get latest state, then remove slot entry.
    state = slots_mod.read_state(workspace) or state
    if slot_id in state.slots:
        del state.slots[slot_id]
    slots_mod.write_state(workspace, state)
    return {"slot_id": slot_id, "feature": feature, "repos": cleared}


def slot_swap(workspace: Workspace, slot_a: str, slot_b: str) -> dict[str, Any]:
    """Exchange the occupants of two slots.

    Performs two parallel branch checkouts inside the slot worktrees and
    updates slots.json — no worktree_add or worktree_remove involved.
    Raises swap_scope_mismatch when the two features touch different repo sets.

    On a phase-2 checkout failure, attempts to re-checkout each slot's
    original branch (best-effort rollback) and persists an ``in_flight``
    marker so the next slot op refuses to operate on a half-swapped state.
    """
    _ensure_consistent_slot_state(workspace)
    state = slots_mod.read_state(workspace)
    if state is None:
        raise BlockerError(code="no_slot_state", what="no slots.json")
    if slot_a not in state.slots:
        raise BlockerError(code="empty_slot",
                           what=f"slot '{slot_a}' is empty — cannot swap")
    if slot_b not in state.slots:
        raise BlockerError(code="empty_slot",
                           what=f"slot '{slot_b}' is empty — cannot swap")

    feat_a = state.slots[slot_a].feature
    feat_b = state.slots[slot_b].feature

    branches_a = repos_for_feature(workspace, feat_a) or {}
    branches_b = repos_for_feature(workspace, feat_b) or {}

    # v1 swap requires identical repo scope on both features.
    if set(branches_a.keys()) != set(branches_b.keys()):
        raise BlockerError(
            code="swap_scope_mismatch",
            what=(f"features '{feat_a}' and '{feat_b}' touch different repo sets — "
                  "v1 swap requires identical scope"),
            details={
                "feat_a": feat_a, "feat_a_repos": sorted(branches_a.keys()),
                "feat_b": feat_b, "feat_b_repos": sorted(branches_b.keys()),
            },
        )

    # Per repo, swap the checked-out branches inside each slot's worktree.
    # Git won't allow a branch to be checked out in two worktrees simultaneously,
    # so we detach both slots first to free the branch locks, then do the checkouts.
    per_repo: list[dict] = []
    repo_names = sorted(branches_a.keys())
    # Phase 1: detach every slot's repo HEAD so the branches are free.
    for repo_name in repo_names:
        slot_a_path = slots_mod.slot_worktree_path(workspace, slot_a, repo_name)
        slot_b_path = slots_mod.slot_worktree_path(workspace, slot_b, repo_name)
        git.checkout_detach(slot_a_path)
        git.checkout_detach(slot_b_path)

    # Phase 2: adopt the swapped branches. On failure, attempt to re-checkout
    # each slot's ORIGINAL branch (rollback) and persist an in_flight marker
    # so the next slot op refuses to run on a half-flipped state.
    try:
        for repo_name in repo_names:
            slot_a_path = slots_mod.slot_worktree_path(workspace, slot_a, repo_name)
            slot_b_path = slots_mod.slot_worktree_path(workspace, slot_b, repo_name)
            # slot A's worktree adopts feat_b's branch; slot B's worktree adopts feat_a's branch.
            git.checkout(slot_a_path, branches_b[repo_name])
            git.checkout(slot_b_path, branches_a[repo_name])
            per_repo.append({"repo": repo_name,
                              "slot_a_now": branches_b[repo_name],
                              "slot_b_now": branches_a[repo_name]})
    except Exception as e:
        failed_repo = repo_name  # last iterated value
        # Best-effort rollback: put every slot back on its ORIGINAL branch.
        for rn in repo_names:
            slot_a_path = slots_mod.slot_worktree_path(workspace, slot_a, rn)
            slot_b_path = slots_mod.slot_worktree_path(workspace, slot_b, rn)
            try:
                git.checkout(slot_a_path, branches_a[rn])
            except Exception:
                pass
            try:
                git.checkout(slot_b_path, branches_b[rn])
            except Exception:
                pass
        # Persist in_flight marker — slots.json otherwise unchanged.
        cur = slots_mod.read_state(workspace) or state
        cur.in_flight = {
            "operation": "slot_swap",
            "slot_a": slot_a, "slot_b": slot_b,
            "feat_a": feat_a, "feat_b": feat_b,
            "started_at": slots_mod.now_iso(),
            "failed_repo": failed_repo,
            "error_what": str(e),
        }
        slots_mod.write_state(workspace, cur)
        raise

    now = slots_mod.now_iso()
    state = slots_mod.read_state(workspace) or state
    state.slots[slot_a] = slots_mod.SlotEntry(feature=feat_b, occupied_at=now)
    state.slots[slot_b] = slots_mod.SlotEntry(feature=feat_a, occupied_at=now)
    state.last_touched[feat_a] = now
    state.last_touched[feat_b] = now
    # Clear any in_flight marker — this swap completed cleanly.
    state.in_flight = None
    slots_mod.write_state(workspace, state)

    return {
        "swapped": [f"{feat_a}↔{feat_b}"],
        "slot_a": slot_a, "slot_b": slot_b,
        "per_repo": per_repo,
    }
