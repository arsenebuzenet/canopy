"""[[externals]] — link unmanaged sibling directories next to the slots.

A repo's build files reference siblings by relative path (``..\\..\\lib``).
Inside a warm slot the repo sits one level deeper under ``worktree-N``, so
the same reference lands next to the slot dirs instead of next to the
workspace. canopy plants a directory link there (see
``workspace.config.make_external`` for the geometry) so the reference
resolves without per-slot setup.
"""
from __future__ import annotations

from typing import Any, Iterable

from .. import compat
from ..workspace.config import ExternalConfig
from ..workspace.workspace import Workspace
from .errors import BlockerError, FixAction

STATES = ("ok", "missing", "stale", "shadowed", "target_missing")


def _state(ext: ExternalConfig) -> str:
    if not ext.target.is_dir():
        return "target_missing"
    if compat.is_dir_link(ext.link):
        return "ok" if compat.same_path(compat.read_dir_link(ext.link), ext.target) else "stale"
    if ext.link.exists():
        return "shadowed"
    return "missing"


def _entry(ext: ExternalConfig) -> dict[str, Any]:
    return {
        "name": ext.name,
        "path": ext.path,
        "target": str(ext.target),
        "link": str(ext.link),
        "state": _state(ext),
    }


def external_status(workspace: Workspace) -> list[dict[str, Any]]:
    """One ``{name, path, target, link, state}`` per configured external."""
    return [_entry(ext) for ext in workspace.config.externals]


def ensure_external_links(
    workspace: Workspace, names: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Create or recreate each external's link; return the resulting status list.

    ``names`` restricts the work to those externals (doctor repairs one at a
    time so an unrelated broken external cannot block the fix).
    Raises ``BlockerError`` for the two states that need a human.
    """
    wanted = set(names) if names is not None else None
    out: list[dict[str, Any]] = []
    for ext in workspace.config.externals:
        if wanted is not None and ext.name not in wanted:
            continue
        state = _state(ext)
        if state == "target_missing":
            raise BlockerError(
                code="external_target_missing",
                what=f"external '{ext.name}' target does not exist",
                expected=f"directory at {ext.target}",
                actual="(missing)",
                details={"name": ext.name, "target": str(ext.target), "link": str(ext.link)},
                fix_actions=[FixAction(
                    action="manual", args={},
                    preview=f"restore {ext.target} or remove the [[externals]] entry '{ext.name}'",
                    safe=False,
                )],
            )
        if state == "shadowed":
            raise BlockerError(
                code="external_link_shadowed",
                what=f"external '{ext.name}' link path is occupied by a real directory or file",
                expected=f"directory link at {ext.link}",
                actual=str(ext.link),
                details={"name": ext.name, "target": str(ext.target), "link": str(ext.link)},
                fix_actions=[FixAction(
                    action="manual", args={},
                    preview=f"move {ext.link} out of the way, then rerun",
                    safe=False,
                )],
            )
        if state == "stale":
            compat.remove_dir_link(ext.link)
        if state in ("stale", "missing"):
            ext.link.parent.mkdir(parents=True, exist_ok=True)
            compat.make_dir_link(ext.link, ext.target)
        out.append(_entry(ext))
    return out
