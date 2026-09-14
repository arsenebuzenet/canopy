"""
Code-review platform façade — GitHub or Bitbucket Cloud, chosen per repo.

Same function names and return shapes as ``integrations/github.py``, but
every repo-scoped call takes a :class:`RemoteRef` (from
``platforms.parse_remote`` on the repo's origin URL) instead of
``owner, slug``. Thread mutations take only a thread id and dispatch on its
form (``PRRT_…`` vs ``bb:…``).

Backends are looked up as module attributes at call time so tests that
monkeypatch ``canopy.integrations.github.<fn>`` keep working through here.
"""
from __future__ import annotations

from pathlib import Path

from . import bitbucket, github
from .bitbucket import UnknownReviewerError
from .github import PullRequestNotFoundError
from .platforms import (
    BITBUCKET, GITHUB, PlatformNotConfiguredError, RemoteRef, ReviewApiError, ThreadRef,
    build_comments_from_threads, format_bitbucket_thread_id, parse_pr_url,
    parse_remote, parse_thread_id,
)

__all__ = [
    "RemoteRef", "ThreadRef", "PlatformNotConfiguredError", "PullRequestNotFoundError",
    "ReviewApiError", "UnknownReviewerError", "parse_remote", "parse_pr_url", "parse_thread_id",
    "format_bitbucket_thread_id", "build_comments_from_threads", "platform_label",
    "is_configured", "unavailable_blocker", "find_pull_request",
    "get_pull_request_by_number", "list_open_prs", "create_pr", "update_pr_body",
    "get_pr_checks", "list_review_threads", "get_review_comments",
    "resolve_thread", "unresolve_thread", "reply_to_thread",
]

_LABELS = {GITHUB: "GitHub", BITBUCKET: "Bitbucket"}


def platform_label(platform: str) -> str:
    return _LABELS.get(platform, platform)


def _backend(platform: str):
    if platform == GITHUB:
        return github
    if platform == BITBUCKET:
        return bitbucket
    raise ValueError(f"unsupported review platform: {platform!r}")


def is_configured(workspace_root: Path, remote: RemoteRef) -> bool:
    if remote.platform == GITHUB:
        return github.is_github_configured(workspace_root)
    if remote.platform == BITBUCKET:
        return bitbucket.is_configured()
    return False


def unavailable_blocker(remote: RemoteRef) -> dict:
    if remote.platform == BITBUCKET:
        return bitbucket.bitbucket_unavailable_blocker()
    return github.github_unavailable_blocker()


# ── repo-scoped reads / writes ────────────────────────────────────────────

def find_pull_request(workspace_root: Path, remote: RemoteRef, branch: str) -> dict | None:
    return _backend(remote.platform).find_pull_request(workspace_root, remote.owner, remote.slug, branch)


def get_pull_request_by_number(workspace_root: Path, remote: RemoteRef, pr_number: int) -> dict | None:
    return _backend(remote.platform).get_pull_request_by_number(
        workspace_root, remote.owner, remote.slug, pr_number)


def list_open_prs(
    workspace_root: Path, remote: RemoteRef, author: str | None = None, limit: int = 50,
) -> list[dict]:
    return _backend(remote.platform).list_open_prs(
        workspace_root, remote.owner, remote.slug, author=author, limit=limit)


def create_pr(
    workspace_root: Path,
    remote: RemoteRef,
    *,
    branch: str,
    base: str,
    title: str,
    body: str,
    draft: bool = False,
    reviewers: list[str] | None = None,
) -> dict:
    return _backend(remote.platform).create_pr(
        workspace_root, remote.owner, remote.slug,
        branch=branch, base=base, title=title, body=body, draft=draft, reviewers=reviewers,
    )


def update_pr_body(workspace_root: Path, remote: RemoteRef, pr_number: int, body: str) -> None:
    _backend(remote.platform).update_pr_body(workspace_root, remote.owner, remote.slug, pr_number, body)


def get_pr_checks(workspace_root: Path, remote: RemoteRef, pr_number: int) -> tuple[dict, list[dict]]:
    return _backend(remote.platform).get_pr_checks(workspace_root, remote.owner, remote.slug, pr_number)


def list_review_threads(workspace_root: Path, remote: RemoteRef, pr_number: int) -> list[dict]:
    return _backend(remote.platform).list_review_threads(workspace_root, remote.owner, remote.slug, pr_number)


def get_review_comments(workspace_root: Path, remote: RemoteRef, pr_number: int) -> tuple[list[dict], int]:
    return _backend(remote.platform).get_review_comments(workspace_root, remote.owner, remote.slug, pr_number)


# ── thread mutations (dispatch on the id form) ────────────────────────────

def resolve_thread(workspace_root: Path, thread_id: str) -> dict:
    ref = parse_thread_id(thread_id)
    if ref.platform == GITHUB:
        return github.resolve_thread(workspace_root, ref.node_id)
    return bitbucket.resolve_thread(workspace_root, ref.remote.owner, ref.remote.slug, ref.pr_number, ref.comment_id)


def unresolve_thread(workspace_root: Path, thread_id: str) -> dict:
    ref = parse_thread_id(thread_id)
    if ref.platform == GITHUB:
        return github.unresolve_thread(workspace_root, ref.node_id)
    return bitbucket.unresolve_thread(workspace_root, ref.remote.owner, ref.remote.slug, ref.pr_number, ref.comment_id)


def reply_to_thread(workspace_root: Path, thread_id: str, body: str) -> dict:
    ref = parse_thread_id(thread_id)
    if ref.platform == GITHUB:
        return github.reply_to_thread(workspace_root, ref.node_id, body)
    return bitbucket.reply_to_thread(
        workspace_root, ref.remote.owner, ref.remote.slug, ref.pr_number, ref.comment_id, body)
