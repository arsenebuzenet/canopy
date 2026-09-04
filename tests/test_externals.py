"""[[externals]] — unmanaged sibling dirs linked so slot repos resolve them."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from canopy.workspace.config import ConfigError, load_config, make_external


def _norm(p: Path) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


# ── config ───────────────────────────────────────────────────────────

def test_make_external_computes_target_and_link(tmp_path):
    root = tmp_path / "ws"
    ext = make_external(root, "../v2.jr.core")
    assert ext.name == "v2.jr.core"
    assert _norm(ext.target) == _norm(tmp_path / "v2.jr.core")
    assert _norm(ext.link) == _norm(root / ".canopy" / "worktrees" / "v2.jr.core")


def test_make_external_two_levels_up_lands_in_dot_canopy(tmp_path):
    root = tmp_path / "a" / "ws"
    ext = make_external(root, "../../lib")
    assert _norm(ext.target) == _norm(tmp_path / "lib")
    assert _norm(ext.link) == _norm(root / ".canopy" / "lib")


def test_make_external_explicit_name(tmp_path):
    ext = make_external(tmp_path / "ws", "../v2.jr.core", name="core")
    assert ext.name == "core"


def test_make_external_rejects_target_inside_workspace(tmp_path):
    with pytest.raises(ConfigError, match="inside the workspace"):
        make_external(tmp_path / "ws", "shared-assets")


def test_make_external_rejects_climbing_past_dot_canopy(tmp_path):
    with pytest.raises(ConfigError, match="climbs too far"):
        make_external(tmp_path / "a" / "b" / "ws", "../../../lib")


def test_make_external_rejects_worktrees_root(tmp_path):
    with pytest.raises(ConfigError, match="climbs too far"):
        make_external(tmp_path / "ws", "..")


def test_make_external_rejects_dot_canopy(tmp_path):
    with pytest.raises(ConfigError, match="climbs too far"):
        make_external(tmp_path / "ws", "../..")


def test_make_external_rejects_absolute_path(tmp_path):
    abs_path = str(tmp_path / "lib")
    with pytest.raises(ConfigError, match="must be relative"):
        make_external(tmp_path / "ws", abs_path)


def test_load_config_parses_externals(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "canopy.toml").write_text("""
[workspace]
name = "t"

[[repos]]
name = "repo-a"
path = "repo-a"

[[externals]]
path = "../v2.jr.core"

[[externals]]
path = "../other"
name = "oth"
""", encoding="utf-8")
    cfg = load_config(root)
    assert [e.name for e in cfg.externals] == ["v2.jr.core", "oth"]
    assert _norm(cfg.externals[0].link) == _norm(root / ".canopy" / "worktrees" / "v2.jr.core")


def test_load_config_defaults_to_no_externals(canopy_toml):
    assert load_config(canopy_toml).externals == []


def test_load_config_rejects_duplicate_external_name(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "canopy.toml").write_text("""
[workspace]
name = "t"

[[repos]]
name = "repo-a"
path = "repo-a"

[[externals]]
path = "../x"
name = "dup"

[[externals]]
path = "../y"
name = "dup"
""", encoding="utf-8")
    with pytest.raises(ConfigError, match="Duplicate external name"):
        load_config(root)


def test_load_config_rejects_duplicate_external_name_case_insensitive(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "canopy.toml").write_text("""
[workspace]
name = "t"

[[repos]]
name = "repo-a"
path = "repo-a"

[[externals]]
path = "../Foo"

[[externals]]
path = "../foo"
""", encoding="utf-8")
    with pytest.raises(ConfigError, match="Duplicate external name"):
        load_config(root)


def test_load_config_rejects_externals_with_nested_repo_path(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "canopy.toml").write_text("""
[workspace]
name = "t"

[[repos]]
name = "api"
path = "services/api"

[[externals]]
path = "../lib"
""", encoding="utf-8")
    with pytest.raises(ConfigError, match="directly under the workspace root"):
        load_config(root)


def test_load_config_accepts_dot_slash_repo_path_with_externals(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "repo-a").mkdir()
    (root / "canopy.toml").write_text("""
[workspace]
name = "t"

[[repos]]
name = "repo-a"
path = "./repo-a"

[[externals]]
path = "../lib"
""", encoding="utf-8")
    cfg = load_config(root)
    assert cfg.repos[0].path == "./repo-a"
    assert [e.name for e in cfg.externals] == ["lib"]


def test_load_config_rejects_external_without_path(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "canopy.toml").write_text("""
[workspace]
name = "t"

[[repos]]
name = "repo-a"
path = "repo-a"

[[externals]]
name = "x"
""", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"\[\[externals\]\] entry 0 missing 'path'"):
        load_config(root)


# ── actions/externals ────────────────────────────────────────────────

from canopy import compat
from canopy.actions.errors import BlockerError
from canopy.workspace.workspace import Workspace


@pytest.fixture
def workspace_with_external(canopy_toml_for_workspace):
    """workspace_with_feature + a sibling dir <tmp>/ext-lib declared as an external."""
    root = canopy_toml_for_workspace
    ext_dir = root.parent / "ext-lib"
    (ext_dir / "pkg").mkdir(parents=True)
    (ext_dir / "pkg" / "lib.txt").write_text("lib", encoding="utf-8")
    toml = root / "canopy.toml"
    toml.write_text(toml.read_text(encoding="utf-8") + """
[[externals]]
path = "../ext-lib"
""", encoding="utf-8")
    return Workspace(load_config(root))


def _ext(ws: Workspace):
    return ws.config.externals[0]


def test_status_missing_when_nothing_at_link(workspace_with_external):
    from canopy.actions.externals import external_status
    (st,) = external_status(workspace_with_external)
    assert st["name"] == "ext-lib"
    assert st["state"] == "missing"
    assert _norm(Path(st["link"])) == _norm(_ext(workspace_with_external).link)


def test_ensure_creates_link_and_reports_ok(workspace_with_external):
    from canopy.actions.externals import ensure_external_links, external_status
    out = ensure_external_links(workspace_with_external)
    assert out[0]["state"] == "ok"
    link = _ext(workspace_with_external).link
    assert compat.is_dir_link(link)
    assert (link / "pkg" / "lib.txt").read_text(encoding="utf-8") == "lib"
    assert external_status(workspace_with_external)[0]["state"] == "ok"


def test_ensure_is_idempotent(workspace_with_external):
    from canopy.actions.externals import ensure_external_links
    ensure_external_links(workspace_with_external)
    ensure_external_links(workspace_with_external)
    assert compat.is_dir_link(_ext(workspace_with_external).link)


def test_status_stale_and_ensure_recreates(workspace_with_external, tmp_path):
    from canopy.actions.externals import ensure_external_links, external_status
    ext = _ext(workspace_with_external)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    ext.link.parent.mkdir(parents=True, exist_ok=True)
    compat.make_dir_link(ext.link, elsewhere)
    assert external_status(workspace_with_external)[0]["state"] == "stale"
    ensure_external_links(workspace_with_external)
    assert compat.same_path(compat.read_dir_link(ext.link), ext.target)
    assert elsewhere.is_dir()


def test_status_shadowed_and_ensure_raises(workspace_with_external):
    from canopy.actions.externals import ensure_external_links, external_status
    ext = _ext(workspace_with_external)
    ext.link.mkdir(parents=True)
    assert external_status(workspace_with_external)[0]["state"] == "shadowed"
    with pytest.raises(BlockerError) as e:
        ensure_external_links(workspace_with_external)
    assert e.value.code == "external_link_shadowed"
    assert e.value.details["name"] == "ext-lib"


def test_status_target_missing_and_ensure_raises(workspace_with_external):
    import shutil
    from canopy.actions.externals import ensure_external_links, external_status
    shutil.rmtree(_ext(workspace_with_external).target)
    assert external_status(workspace_with_external)[0]["state"] == "target_missing"
    with pytest.raises(BlockerError) as e:
        ensure_external_links(workspace_with_external)
    assert e.value.code == "external_target_missing"


def test_ensure_names_filter_skips_others(workspace_with_external, tmp_path):
    """A broken external must not block repairing a different one."""
    import shutil
    from canopy.actions.externals import ensure_external_links
    root = workspace_with_external.config.root
    other = root.parent / "other-lib"
    other.mkdir()
    toml = root / "canopy.toml"
    toml.write_text(toml.read_text(encoding="utf-8") + """
[[externals]]
path = "../other-lib"
""", encoding="utf-8")
    ws = Workspace(load_config(root))
    shutil.rmtree(ws.config.externals[0].target)  # ext-lib gone
    out = ensure_external_links(ws, names=["other-lib"])
    assert [o["name"] for o in out] == ["other-lib"]
    assert out[0]["state"] == "ok"


def test_no_externals_is_a_noop(workspace_with_canonical_only):
    from canopy.actions.externals import ensure_external_links, external_status
    assert external_status(workspace_with_canonical_only) == []
    assert ensure_external_links(workspace_with_canonical_only) == []
    assert not (workspace_with_canonical_only.config.root / ".canopy" / "worktrees").exists()


# ── slot_load / switch ───────────────────────────────────────────────

def _make_canonical(ws_root: Path) -> Workspace:
    """Same setup as the workspace_with_canonical_only fixture, on a workspace that has an external."""
    import subprocess
    from canopy.actions import slots as sm
    ws = Workspace(load_config(ws_root))
    for repo in ("repo-a", "repo-b"):
        subprocess.run(["git", "branch", "X"], cwd=ws_root / repo, check=True)
        subprocess.run(["git", "checkout", "X"], cwd=ws_root / repo, check=True)
        subprocess.run(["git", "branch", "Y"], cwd=ws_root / repo, check=True)
    sm.write_state(ws, sm.SlotState(
        slot_count=2,
        canonical=sm.CanonicalEntry(
            feature="X", activated_at=sm.now_iso(),
            per_repo_paths={r: str(ws_root / r) for r in ("repo-a", "repo-b")},
        ),
    ))
    return ws


def test_slot_load_plants_external_link(workspace_with_external):
    from canopy.actions.slot_load import slot_load
    ws = _make_canonical(workspace_with_external.config.root)
    result = slot_load(ws, "Y")
    slot_repo = Path(result["per_repo"][0]["worktree_path"])
    # repo-a → worktree-1 → worktrees → ext-lib : the same ../../ext-lib a canonical repo uses
    via_slot = (slot_repo / ".." / ".." / "ext-lib" / "pkg" / "lib.txt")
    assert via_slot.read_text(encoding="utf-8") == "lib"


def test_switch_plants_external_link(workspace_with_external):
    from canopy.actions.switch import switch
    ws = _make_canonical(workspace_with_external.config.root)
    switch(ws, "Y", evict_to="worktree-1")  # X evacuates into worktree-1
    link = ws.config.externals[0].link
    assert compat.is_dir_link(link)
    assert (ws.config.root / ".canopy" / "worktrees" / "worktree-1" / "repo-a"
            / ".." / ".." / "ext-lib" / "pkg" / "lib.txt").read_text(encoding="utf-8") == "lib"


def test_slot_load_blocks_when_external_target_missing(workspace_with_external):
    import shutil
    from canopy.actions.slot_load import slot_load
    ws = _make_canonical(workspace_with_external.config.root)
    shutil.rmtree(ws.config.externals[0].target)
    with pytest.raises(BlockerError) as e:
        slot_load(ws, "Y")
    assert e.value.code == "external_target_missing"
    assert not (ws.config.root / ".canopy" / "worktrees" / "worktree-1").exists()


def test_switch_blocks_before_mutation_when_external_target_missing(workspace_with_external):
    import shutil
    import subprocess
    from canopy.actions import slots as sm
    from canopy.actions.switch import switch
    ws = _make_canonical(workspace_with_external.config.root)
    shutil.rmtree(ws.config.externals[0].target)
    with pytest.raises(BlockerError) as e:
        switch(ws, "Y", evict_to="worktree-1")
    assert e.value.code == "external_target_missing"

    repo_a = ws.config.root / "repo-a"
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo_a, capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout.strip()
    assert branch == "X"
    stash_list = subprocess.run(
        ["git", "stash", "list"],
        cwd=repo_a, capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout.strip()
    assert stash_list == ""
    assert not (ws.config.root / ".canopy" / "worktrees" / "worktree-1").exists()
    assert sm.read_state(ws).in_flight is None


# ── doctor ───────────────────────────────────────────────────────────

def _codes(ws):
    from canopy.actions.doctor import doctor
    return {i["code"]: i for i in doctor(ws)["issues"]}


def test_doctor_reports_missing_link(workspace_with_external):
    issues = _codes(workspace_with_external)
    issue = issues["external_link_missing"]
    assert issue["severity"] == "warn"
    assert issue["auto_fixable"] is True
    assert issue["details"]["name"] == "ext-lib"


def test_doctor_reports_stale_link(workspace_with_external, tmp_path):
    ext = _ext(workspace_with_external)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    ext.link.parent.mkdir(parents=True, exist_ok=True)
    compat.make_dir_link(ext.link, elsewhere)
    assert _codes(workspace_with_external)["external_link_stale"]["auto_fixable"] is True


def test_doctor_reports_shadowed_link_not_fixable(workspace_with_external):
    _ext(workspace_with_external).link.mkdir(parents=True)
    issue = _codes(workspace_with_external)["external_link_shadowed"]
    assert issue["severity"] == "error"
    assert issue["auto_fixable"] is False


def test_doctor_reports_target_missing_not_fixable(workspace_with_external):
    import shutil
    shutil.rmtree(_ext(workspace_with_external).target)
    issue = _codes(workspace_with_external)["external_target_missing"]
    assert issue["severity"] == "error"
    assert issue["auto_fixable"] is False
    assert "external_link_missing" not in _codes(workspace_with_external)


def test_doctor_fix_creates_link(workspace_with_external):
    from canopy.actions.doctor import doctor
    report = doctor(workspace_with_external, fix=True, fix_categories=["externals"])
    assert any(f["code"] == "external_link_missing" and f["success"] for f in report["fixed"])
    assert compat.is_dir_link(_ext(workspace_with_external).link)
    assert "external_link_missing" not in _codes(workspace_with_external)


def test_doctor_clean_when_link_ok(workspace_with_external):
    from canopy.actions.externals import ensure_external_links
    ensure_external_links(workspace_with_external)
    assert not any(c.startswith("external_") for c in _codes(workspace_with_external))


def test_cli_fix_category_accepts_externals():
    from canopy.cli.main import main
    import sys
    from unittest.mock import patch
    # argparse must accept the choice; the command itself needs a workspace, so stop at parse time.
    with patch.object(sys, "argv", ["canopy", "doctor", "--fix-category", "externals", "--help"]):
        with pytest.raises(SystemExit) as e:
            main()
    assert e.value.code == 0
