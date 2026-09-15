"""Tests for the ``commit`` MCP tool wrapper in canopy.mcp.server.

The wrapper is the surface agents actually call. It must forward only
keywords the action accepts — a stale kwarg makes every call fail with
``TypeError`` before any commit logic runs.
"""
from __future__ import annotations

import inspect
import json
from unittest.mock import patch

import canopy.mcp.server as srv
from canopy.actions.commit import commit as commit_action
from canopy.workspace.config import RepoConfig, WorkspaceConfig
from canopy.workspace.workspace import Workspace


def _make_workspace(workspace_dir) -> Workspace:
    config = WorkspaceConfig(
        name="test",
        repos=[
            RepoConfig(name=name, path=f"./{name}", role="x", lang="x")
            for name in ("repo-a", "repo-b")
        ],
        root=workspace_dir,
    )
    return Workspace(config)


def test_wrapper_signature_matches_action():
    """Every wrapper parameter must exist on the action (minus workspace)."""
    action_params = set(inspect.signature(commit_action).parameters) - {"workspace"}
    wrapper_params = set(inspect.signature(srv.commit).parameters)
    assert wrapper_params <= action_params


def test_wrapper_commits_with_default_arguments(workspace_with_feature):
    canopy_dir = workspace_with_feature / ".canopy"
    canopy_dir.mkdir(exist_ok=True)
    (canopy_dir / "features.json").write_text(json.dumps({
        "auth-flow": {"repos": ["repo-a", "repo-b"], "status": "active"},
    }), encoding="utf-8")
    (workspace_with_feature / "repo-a" / "src" / "models.py").write_text(
        "class User:\n    new_field: str\n", encoding="utf-8"
    )
    ws = _make_workspace(workspace_with_feature)

    with patch.object(srv, "_get_workspace", return_value=ws):
        result = srv.commit("mcp wrapper test", feature="auth-flow")

    assert result["feature"] == "auth-flow"
    assert result["results"]["repo-a"]["status"] == "ok"
    assert result["results"]["repo-b"]["status"] == "nothing"
