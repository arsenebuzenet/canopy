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
