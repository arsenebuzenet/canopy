"""Tests for actions/slot_load.py — slot_load + slot_clear + slot_swap (T16, T17)."""
import subprocess

import pytest


def test_slot_load_auto_allocates_lowest_free_slot(workspace_with_canonical_only):
    from canopy.actions.slot_load import slot_load
    result = slot_load(workspace_with_canonical_only, "Y")
    assert result["slot_id"] == "worktree-1"
    from canopy.actions import slots as sm
    state = sm.read_state(workspace_with_canonical_only)
    assert state.slots["worktree-1"].feature == "Y"


def test_slot_load_explicit_slot(workspace_with_canonical_only):
    from canopy.actions.slot_load import slot_load
    result = slot_load(workspace_with_canonical_only, "Y", slot_id="worktree-2")
    assert result["slot_id"] == "worktree-2"


def test_slot_load_refuses_when_feature_is_canonical(workspace_with_slots):
    from canopy.actions.slot_load import slot_load
    from canopy.actions.errors import BlockerError
    with pytest.raises(BlockerError) as e:
        slot_load(workspace_with_slots, "X")  # X is canonical
    assert e.value.code == "feature_is_canonical"


def test_slot_load_refuses_when_already_warm(workspace_with_slots):
    """Y is already warm in slot-1 — loading Y again is a no-op or error."""
    from canopy.actions.slot_load import slot_load
    from canopy.actions.errors import BlockerError
    with pytest.raises(BlockerError) as e:
        slot_load(workspace_with_slots, "Y", slot_id="worktree-2")
    assert e.value.code == "feature_already_warm"


def test_slot_clear_evicts_to_cold(workspace_with_slots):
    """slot-1 has Y; clear → Y goes cold, slot-1 empty, stash present if dirty."""
    from canopy.actions.slot_load import slot_clear
    result = slot_clear(workspace_with_slots, "worktree-1")
    assert result["feature"] == "Y"
    from canopy.actions import slots as sm
    state = sm.read_state(workspace_with_slots)
    assert "worktree-1" not in state.slots


def test_slot_swap_exchanges_occupants(workspace_with_two_warm):
    """slot-1=B, slot-2=A (fixture) → swap → slot-1=A, slot-2=B."""
    from canopy.actions.slot_load import slot_swap
    from canopy.actions import slots as sm
    # Capture pre-swap state
    before = sm.read_state(workspace_with_two_warm)
    feat_in_1 = before.slots["worktree-1"].feature
    feat_in_2 = before.slots["worktree-2"].feature
    result = slot_swap(workspace_with_two_warm, "worktree-1", "worktree-2")
    state = sm.read_state(workspace_with_two_warm)
    # After swap, the features inside each slot have swapped
    assert state.slots["worktree-1"].feature == feat_in_2
    assert state.slots["worktree-2"].feature == feat_in_1
    assert result["swapped"] == [f"{feat_in_1}↔{feat_in_2}"]


def test_slot_load_occupied_slot_replace(workspace_with_slots):
    """slot-1 has Y; load Z into slot-1 with --replace evicts Y first."""
    from canopy.actions.slot_load import slot_load
    # Pre-create Z as a cold branch in both repos
    for repo in ("repo-a", "repo-b"):
        subprocess.run(
            ["git", "branch", "Z"],
            cwd=workspace_with_slots.config.root / repo,
            check=True,
        )
    result = slot_load(workspace_with_slots, "Z", slot_id="worktree-1", replace=True)
    assert result["slot_id"] == "worktree-1"
    assert result["evicted"]["feature"] == "Y"


# ── T19.5 final gate fixes ───────────────────────────────────────────────


def test_slot_clear_refuses_when_dirty_and_stash_fails(workspace_with_slots, monkeypatch):
    """If the slot is dirty and stash fails, slot_clear raises rather than nuking work."""
    # Make the slot dirty
    slot_path = workspace_with_slots.config.root / ".canopy/worktrees/worktree-1/repo-a"
    (slot_path / "dirty.txt").write_text("uncommitted work", encoding="utf-8")
    # Force stash_for_evacuation to fail
    monkeypatch.setattr(
        "canopy.actions.evacuate.stash_for_evacuation",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stash failed")),
    )
    from canopy.actions.slot_load import slot_clear
    from canopy.actions.errors import BlockerError
    with pytest.raises(BlockerError) as e:
        slot_clear(workspace_with_slots, "worktree-1")
    assert e.value.code == "evict_stash_failed"
    # Critically: dirty.txt is STILL THERE (work was not destroyed)
    assert (slot_path / "dirty.txt").exists()


def test_slot_swap_rolls_back_and_marks_in_flight_on_failure(
    workspace_with_two_warm, monkeypatch,
):
    """If phase 2 fails mid-swap, slots.json gets an in_flight marker so
    subsequent slot ops refuse to operate on a partially-flipped workspace.
    """
    from canopy.actions.slot_load import slot_swap
    from canopy.actions.errors import BlockerError
    from canopy.actions import slots as sm
    from canopy.git import repo as git
    # Make phase 2 checkout fail on the third call (phase 1 = 4 detaches,
    # phase 2 starts with checkout calls). We patch git.checkout so the
    # third invocation raises.
    call_count = {"n": 0}
    real_checkout = git.checkout

    def flaky_checkout(path, branch):
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise RuntimeError("simulated checkout failure")
        return real_checkout(path, branch)

    monkeypatch.setattr(git, "checkout", flaky_checkout)
    with pytest.raises((BlockerError, RuntimeError)):
        slot_swap(workspace_with_two_warm, "worktree-1", "worktree-2")
    state_after = sm.read_state(workspace_with_two_warm)
    assert state_after.in_flight is not None
    assert state_after.in_flight.get("operation") == "slot_swap"


def test_slot_load_refuses_unknown_feature(workspace_with_canonical_only):
    """slot_load on a feature not in features.json and with no branch in any
    repo raises ambiguous_feature_scope (or unknown_feature upstream)."""
    from canopy.actions.slot_load import slot_load
    from canopy.actions.errors import BlockerError
    with pytest.raises(BlockerError) as e:
        slot_load(workspace_with_canonical_only, "UNREGISTERED-FEATURE-NAME")
    assert e.value.code in ("ambiguous_feature_scope", "unknown_feature", "unknown_alias")
