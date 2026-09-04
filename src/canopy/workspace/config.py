"""
Parse and validate canopy.toml workspace configuration.
"""
from __future__ import annotations

import os
import sys
if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigNotFoundError(Exception):
    """No canopy.toml found in the directory tree."""


class ConfigError(Exception):
    """Invalid canopy.toml content."""


@dataclass
class RepoConfig:
    """Configuration for a single repository in the workspace."""
    name: str
    path: str               # relative path from workspace root
    role: str = ""           # optional: backend, frontend, shared, infra
    lang: str = ""           # optional: primary language
    default_branch: str = "main"
    is_worktree: bool = False       # True if this is a linked worktree
    worktree_main: str | None = None  # path to main working tree (if worktree)
    augments: dict[str, Any] = field(default_factory=dict)  # per-repo augment overrides (M2)
    # M6 worktree-bootstrap fields. All optional — missing means "skip
    # this step." See docs/plans/worktree-bootstrap.md.
    env_files: list[str] = field(default_factory=list)
    install_cmd: str = ""
    ide_settings: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExternalConfig:
    """A directory the repos reference by relative path but canopy does not manage.

    ``link`` is where canopy plants a directory link so the same relative
    reference resolves from inside a warm slot. Computed lexically from
    ``worktree-0`` — any path that climbs above the slot dir lands at the
    same place for every slot id, so one link serves all slots.
    """
    name: str
    path: str
    target: Path
    link: Path


def make_external(root: Path, path: str, name: str | None = None) -> ExternalConfig:
    """Build an ``ExternalConfig`` from a ``[[externals]]`` entry, validating geometry."""
    if not isinstance(path, str) or not path:
        raise ConfigError(f"[[externals]] path must be a non-empty string, got: {path!r}")
    root = Path(os.path.normpath(str(root)))
    target = Path(os.path.normpath(str(root / path)))
    link = Path(os.path.normpath(str(root / ".canopy" / "worktrees" / "worktree-0" / path)))
    ext_name = name or target.name
    if not ext_name:
        raise ConfigError(f"[[externals]] cannot derive a name from path {path!r}; set 'name'")
    if _is_under(target, root):
        raise ConfigError(
            f"[[externals]] '{ext_name}': path {path!r} is inside the workspace root; "
            "only directories outside the workspace are supported"
        )
    if not _is_under(link, root / ".canopy"):
        raise ConfigError(
            f"[[externals]] '{ext_name}': path {path!r} climbs too far above the workspace "
            "(its slot-relative link would leave .canopy/)"
        )
    return ExternalConfig(name=ext_name, path=path, target=target, link=link)


def _is_under(child: Path, parent: Path) -> bool:
    c = os.path.normcase(str(child))
    p = os.path.normcase(str(parent))
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


@dataclass
class IssueProviderConfig:
    """Per-workspace issue provider selection (M5).

    Parsed from the ``[issue_provider]`` block in canopy.toml. The
    ``options`` dict carries provider-specific settings from the
    ``[issue_provider.<name>]`` sub-table.

    When the block is missing entirely, defaults to Linear with a
    deprecation warning logged once per session — explicit config will
    be required in a future release.
    """
    name: str = "linear"
    options: dict[str, Any] = field(default_factory=dict)
    # Set to True when the parser fell back to the Linear default because
    # no [issue_provider] block was present. The action layer logs a
    # one-time deprecation notice.
    is_default_fallback: bool = False


@dataclass
class WorkspaceConfig:
    """Parsed workspace configuration."""
    name: str
    repos: list[RepoConfig]
    root: Path              # absolute path to workspace root
    slots: int = 2          # warm slot count (canonical is separate); default 2
    issue_provider: IssueProviderConfig = field(default_factory=IssueProviderConfig)
    augments: dict[str, Any] = field(default_factory=dict)  # workspace-level augment defaults (M2)
    # M6 — IDE workspace template + per-workspace bootstrap default.
    ide: str = "none"                   # "vscode" | "none" (default)
    bootstrap_default: bool = False     # if true, --bootstrap is implicit on create/warm
    externals: list[ExternalConfig] = field(default_factory=list)


def load_config(path: Path | None = None) -> WorkspaceConfig:
    """Find and parse canopy.toml.

    If no path is given, walks up from cwd looking for canopy.toml.
    Raises ConfigNotFoundError if none is found.
    Raises ConfigError if the file is malformed.
    """
    if path is not None:
        toml_path = path if path.name == "canopy.toml" else path / "canopy.toml"
    else:
        toml_path = _find_config()

    if not toml_path.exists():
        raise ConfigNotFoundError(f"No canopy.toml found at {toml_path}")

    try:
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Invalid TOML in {toml_path}: {e}") from e

    return _parse_config(data, toml_path.parent.resolve())


def _find_config() -> Path:
    """Walk up from cwd looking for canopy.toml."""
    current = Path.cwd().resolve()
    while True:
        candidate = current / "canopy.toml"
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            raise ConfigNotFoundError(
                "No canopy.toml found in current directory or any parent."
            )
        current = parent


def _parse_config(data: dict[str, Any], root: Path) -> WorkspaceConfig:
    """Parse raw TOML dict into WorkspaceConfig."""
    workspace = data.get("workspace", {})
    name = workspace.get("name")
    if not name:
        raise ConfigError("Missing [workspace] name in canopy.toml")

    repos_data = data.get("repos", [])
    if not repos_data:
        raise ConfigError("No [[repos]] entries in canopy.toml")

    repos = []
    seen_names: set[str] = set()
    for i, entry in enumerate(repos_data):
        repo_name = entry.get("name")
        if not repo_name:
            raise ConfigError(f"[[repos]] entry {i} missing 'name'")
        if not entry.get("path"):
            raise ConfigError(f"[[repos]] entry '{repo_name}' missing 'path'")
        if repo_name in seen_names:
            raise ConfigError(f"Duplicate repo name: '{repo_name}'")
        seen_names.add(repo_name)

        repo_augments = entry.get("augments")
        if repo_augments is not None and not isinstance(repo_augments, dict):
            raise ConfigError(
                f"[[repos]] entry '{repo_name}' augments must be a table, got: {type(repo_augments).__name__}",
            )
        env_files = entry.get("env_files") or []
        if env_files and not (
            isinstance(env_files, list) and all(isinstance(p, str) for p in env_files)
        ):
            raise ConfigError(
                f"[[repos]] entry '{repo_name}' env_files must be a list of strings",
            )
        ide_settings = entry.get("ide_settings")
        if ide_settings is not None and not isinstance(ide_settings, dict):
            raise ConfigError(
                f"[[repos]] entry '{repo_name}' ide_settings must be a table",
            )
        repos.append(RepoConfig(
            name=repo_name,
            path=entry["path"],
            role=entry.get("role", ""),
            lang=entry.get("lang", ""),
            default_branch=entry.get("default_branch", "main"),
            augments=dict(repo_augments) if repo_augments else {},
            env_files=list(env_files),
            install_cmd=entry.get("install_cmd", "") or "",
            ide_settings=dict(ide_settings) if ide_settings else {},
        ))

    externals: list[ExternalConfig] = []
    seen_ext: set[str] = set()
    for i, entry in enumerate(data.get("externals", [])):
        if not isinstance(entry, dict) or "path" not in entry:
            raise ConfigError(f"[[externals]] entry {i} missing 'path'")
        ext = make_external(root, entry["path"], entry.get("name"))
        if ext.name in seen_ext:
            raise ConfigError(f"Duplicate external name: '{ext.name}'")
        seen_ext.add(ext.name)
        externals.append(ext)

    if "max_worktrees" in workspace:
        raise ConfigError(
            "max_worktrees was renamed to `slots` in canopy 3.0 — "
            "run `canopy migrate-slots` to update canopy.toml"
        )
    slots_count = workspace.get("slots", 2)
    if not isinstance(slots_count, int) or slots_count < 1:
        raise ConfigError(f"slots must be a positive integer, got: {slots_count!r}")
    ide_choice = workspace.get("ide", "none")
    if not isinstance(ide_choice, str):
        raise ConfigError(f"[workspace] ide must be a string, got {type(ide_choice).__name__}")
    bootstrap_default = bool(workspace.get("bootstrap_default", False))
    issue_provider = _parse_issue_provider(data)
    augments = _parse_augments(data)

    return WorkspaceConfig(
        name=name,
        repos=repos,
        root=root,
        slots=slots_count,
        issue_provider=issue_provider,
        augments=augments,
        ide=ide_choice,
        bootstrap_default=bootstrap_default,
        externals=externals,
    )


def _parse_augments(data: dict[str, Any]) -> dict[str, Any]:
    """Parse the ``[augments]`` block from canopy.toml (M2).

    Schema::

        [augments]
        preflight_cmd = "make check"
        test_cmd = "pytest"
        review_bots = ["coderabbit", "korbit"]

    Lenient: missing block returns empty dict; unknown keys preserved
    so future augments don't require parser changes. Validation that
    catches typos is deferred to ``canopy doctor`` (see plan §non-goals).
    """
    block = data.get("augments")
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise ConfigError(
            f"[augments] must be a table, got: {type(block).__name__}",
        )
    return dict(block)


def _parse_issue_provider(data: dict[str, Any]) -> IssueProviderConfig:
    """Parse the ``[issue_provider]`` block from canopy.toml.

    Schema::

        [issue_provider]
        name = "linear"   # or "github_issues"

        [issue_provider.linear]      # optional sub-table
        api_key_env = "LINEAR_API_KEY"

        [issue_provider.github_issues]
        repo = "owner/repo"

    Returns ``IssueProviderConfig(name="linear", is_default_fallback=True)``
    when the block is missing — preserves backward compatibility with
    pre-M5 canopy.toml files.
    """
    block = data.get("issue_provider")
    if not isinstance(block, dict):
        return IssueProviderConfig(name="linear", is_default_fallback=True)
    name = block.get("name", "linear")
    if not isinstance(name, str) or not name:
        raise ConfigError(
            f"[issue_provider] name must be a non-empty string, got: {name!r}",
        )
    sub_table = block.get(name)
    options: dict[str, Any] = sub_table if isinstance(sub_table, dict) else {}
    return IssueProviderConfig(name=name, options=options)


# ── Workspace settings (keys under [workspace]) ────────────────────────

# Settings that can be read/written via `canopy config`
WORKSPACE_SETTINGS = {
    "name": str,
    "slots": int,
}


def get_config_value(root: Path, key: str) -> Any:
    """Read a single workspace setting from canopy.toml."""
    if key not in WORKSPACE_SETTINGS:
        raise ConfigError(
            f"Unknown setting: '{key}'. "
            f"Available: {', '.join(sorted(WORKSPACE_SETTINGS))}"
        )
    toml_path = root / "canopy.toml"
    if not toml_path.exists():
        raise ConfigNotFoundError(f"No canopy.toml at {root}")
    with open(toml_path, "rb") as f:
        data = tomllib.load(f)
    return data.get("workspace", {}).get(key)


def set_config_value(root: Path, key: str, value: str) -> Any:
    """Write a single workspace setting to canopy.toml.

    Handles type coercion based on WORKSPACE_SETTINGS.
    Returns the coerced value.
    """
    if key not in WORKSPACE_SETTINGS:
        raise ConfigError(
            f"Unknown setting: '{key}'. "
            f"Available: {', '.join(sorted(WORKSPACE_SETTINGS))}"
        )

    expected_type = WORKSPACE_SETTINGS[key]
    try:
        if expected_type == int:
            coerced = int(value)
        else:
            coerced = value
    except (ValueError, TypeError):
        raise ConfigError(f"Invalid value for '{key}': expected {expected_type.__name__}")

    toml_path = root / "canopy.toml"
    if not toml_path.exists():
        raise ConfigNotFoundError(f"No canopy.toml at {root}")

    content = toml_path.read_text(encoding="utf-8")

    # Try to update existing key under [workspace]
    import re
    # Match: key = value (with optional quotes for strings)
    pattern = rf'^({re.escape(key)}\s*=\s*).*$'

    # Find lines within the [workspace] section
    lines = content.split("\n")
    in_workspace = False
    updated = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "[workspace]":
            in_workspace = True
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            # Hit a new section — if we haven't updated yet, insert before this
            if in_workspace and not updated:
                # Insert the key before this section
                formatted = _format_toml_value(key, coerced)
                lines.insert(i, formatted)
                updated = True
            in_workspace = False
            continue
        if in_workspace and re.match(pattern, stripped):
            lines[i] = _format_toml_value(key, coerced)
            updated = True
            break

    # If still not updated, append to [workspace] section
    if not updated:
        # Find the [workspace] line and append after it
        for i, line in enumerate(lines):
            if line.strip() == "[workspace]":
                lines.insert(i + 1, _format_toml_value(key, coerced))
                updated = True
                break

    if not updated:
        raise ConfigError("Could not find [workspace] section in canopy.toml")

    toml_path.write_text("\n".join(lines), encoding="utf-8")
    return coerced


def get_all_config(root: Path) -> dict[str, Any]:
    """Read all workspace settings from canopy.toml."""
    toml_path = root / "canopy.toml"
    if not toml_path.exists():
        raise ConfigNotFoundError(f"No canopy.toml at {root}")
    with open(toml_path, "rb") as f:
        data = tomllib.load(f)
    ws = data.get("workspace", {})
    return {k: ws.get(k) for k in WORKSPACE_SETTINGS}


def _format_toml_value(key: str, value: Any) -> str:
    """Format a key = value line for TOML."""
    if isinstance(value, int):
        return f"{key} = {value}"
    elif isinstance(value, str):
        return f'{key} = "{value}"'
    return f"{key} = {value}"


def validate_config(config: WorkspaceConfig) -> list[str]:
    """Validate a WorkspaceConfig and return a list of warnings.

    Returns an empty list if everything is valid.
    """
    warnings = []

    for repo in config.repos:
        abs_path = (config.root / repo.path).resolve()
        if not abs_path.exists():
            warnings.append(f"Repo '{repo.name}': path does not exist: {abs_path}")
        elif not (abs_path / ".git").exists():
            warnings.append(f"Repo '{repo.name}': not a git repository: {abs_path}")

    return warnings
