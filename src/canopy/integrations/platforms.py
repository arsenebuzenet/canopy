"""Code-review platform vocabulary shared by every backend and the façade.

Pure types and parsers — no canopy imports — so ``github.py``,
``bitbucket.py`` and ``review.py`` can all depend on it without cycles.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


GITHUB = "github"
BITBUCKET = "bitbucket"


@dataclass(frozen=True)
class RemoteRef:
    """Where a repo lives: platform + the two path segments the API needs.

    ``owner`` is the GitHub owner or the Bitbucket workspace id; ``slug``
    is the repo name with any ``.git`` suffix stripped.
    """
    platform: str
    owner: str
    slug: str


@dataclass(frozen=True)
class ThreadRef:
    """A parsed review-thread id. GitHub ids are global node ids; Bitbucket
    ids carry the repo + PR + root comment they belong to."""
    platform: str
    node_id: str | None
    remote: RemoteRef | None
    pr_number: int | None
    comment_id: int | None


class PlatformNotConfiguredError(Exception):
    """No credentials / transport for the platform a repo lives on.

    ``payload`` is ``{code, what, fix_actions}`` — the same shape a
    ``BlockerError`` takes, so callers can convert without re-deriving
    install hints.
    """

    def __init__(self, message: str = "", *, payload: dict | None = None):
        super().__init__(message or (payload or {}).get("what", "review platform not configured"))
        self.payload = payload or {}


_REMOTE_PATTERNS = (
    (GITHUB, re.compile(r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?/?$", re.IGNORECASE)),
    (GITHUB, re.compile(r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", re.IGNORECASE)),
    (BITBUCKET, re.compile(r"^https?://(?:[^@/]+@)?bitbucket\.org/([^/]+)/([^/]+?)(?:\.git)?/?$", re.IGNORECASE)),
    (BITBUCKET, re.compile(r"^git@bitbucket\.org:([^/]+)/([^/]+?)(?:\.git)?/?$", re.IGNORECASE)),
    (BITBUCKET, re.compile(r"^ssh://git@bitbucket\.org/([^/]+)/([^/]+?)(?:\.git)?/?$", re.IGNORECASE)),
)

_PR_URL_PATTERNS = (
    (GITHUB, re.compile(r"^https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)", re.IGNORECASE)),
    (BITBUCKET, re.compile(r"^https?://bitbucket\.org/([^/]+)/([^/]+)/pull-requests/(\d+)", re.IGNORECASE)),
)

_BB_THREAD_ID = re.compile(r"^bb:([^/#]+)/([^/#]+)#(\d+)/(\d+)$")


def parse_remote(url: str) -> RemoteRef | None:
    for platform, pattern in _REMOTE_PATTERNS:
        m = pattern.match(url or "")
        if m:
            return RemoteRef(platform, m.group(1), m.group(2))
    return None


def parse_pr_url(url: str) -> tuple[RemoteRef, int] | None:
    for platform, pattern in _PR_URL_PATTERNS:
        m = pattern.match(url or "")
        if m:
            return RemoteRef(platform, m.group(1), m.group(2)), int(m.group(3))
    return None


def format_bitbucket_thread_id(owner: str, slug: str, pr_number: int, comment_id: int) -> str:
    return f"bb:{owner}/{slug}#{pr_number}/{comment_id}"


def parse_thread_id(thread_id: str) -> ThreadRef:
    """Raises ``ValueError`` when the id matches neither platform's form."""
    tid = thread_id or ""
    if tid.startswith("PRRT_") and len(tid) > len("PRRT_"):
        return ThreadRef(GITHUB, tid, None, None, None)
    m = _BB_THREAD_ID.match(tid)
    if m:
        remote = RemoteRef(BITBUCKET, m.group(1), m.group(2))
        return ThreadRef(BITBUCKET, None, remote, int(m.group(3)), int(m.group(4)))
    raise ValueError(
        f"thread_id must be a GitHub node id (PRRT_...) or a Bitbucket id "
        f"(bb:<workspace>/<repo>#<pr>/<comment>); got {thread_id!r}"
    )


def build_comments_from_threads(threads: list[dict]) -> tuple[list[dict], int]:
    """Flatten ``list_review_threads`` output into the normalized comment list.

    Resolved threads are counted and dropped; every surviving comment carries
    its ``thread_id``. Shape matches what the temporal classifier consumes.
    """
    comments: list[dict] = []
    resolved_count = 0
    for t in threads:
        if t["is_resolved"]:
            resolved_count += 1
            continue
        for c in t["comments"]:
            comments.append({
                "id": c["comment_id"],
                "path": c["path"] or "",
                "line": c["line"] or 0,
                "body": c["body"] or "",
                "author": c["author"],
                "author_type": c.get("author_type", ""),
                "state": "",
                "created_at": c["created_at"] or "",
                "url": c["url"] or "",
                "in_reply_to_id": None,
                "commit_id": c.get("commit_id") or "",
                "thread_id": t["thread_id"],
            })
    return comments, resolved_count
