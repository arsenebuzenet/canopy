"""``.code-workspace`` renderer for canopy worktrees (M6).

Pure function: given a feature name + the per-repo worktree paths +
optional ``ide_settings`` overrides per repo, return the JSON content
of a VS Code multi-root workspace file.

The atomic writer is in ``actions/bootstrap.py`` — keeping the renderer
side-effect-free makes it trivially unit-testable.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..workspace.workspace import Workspace


def render_code_workspace(
    workspace: Workspace,
    feature_name: str,
    worktree_paths: dict[str, Path],
) -> str:
    """Return the JSON body for ``<feature>.code-workspace``.

    ``worktree_paths`` maps canopy repo names to absolute worktree
    directories. Per-repo ``ide_settings`` from canopy.toml are merged
    into the folder's ``settings`` block — useful for things like
    ``python.defaultInterpreterPath = "${workspaceFolder}/.venv/bin/python"``.
    """
    folders = []
    for repo_name in sorted(worktree_paths.keys()):
        path = worktree_paths[repo_name]
        try:
            state = workspace.get_repo(repo_name)
        except KeyError:
            state = None
        ide_settings = state.config.ide_settings if state else {}
        folder: dict = {
            "name": f"{repo_name} ({feature_name})",
            "path": Path(path).as_posix(),
        }
        if ide_settings:
            folder["settings"] = dict(ide_settings)
        folders.append(folder)

    return json.dumps(
        {"folders": folders, "settings": {"canopy.feature": feature_name}},
        indent=2,
    )
