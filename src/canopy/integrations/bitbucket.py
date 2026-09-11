"""
Bitbucket Cloud integration — REST API 2.0 over urllib.

Mirrors the function surface of ``integrations/github.py`` with
``(workspace_root, owner, slug, ...)`` signatures so ``integrations/review.py``
can dispatch on the platform without callers branching. ``owner`` is the
Bitbucket workspace id.

Bitbucket Cloud only. Data Center has a different API and comment model.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .. import compat
from .platforms import (
    PlatformNotConfiguredError, ReviewApiError, build_comments_from_threads,
    format_bitbucket_thread_id,
)

API_ROOT = "https://api.bitbucket.org/2.0"
_TOKEN_URL = "https://id.atlassian.com/manage-profile/security/api-tokens"


class BitbucketNotConfiguredError(PlatformNotConfiguredError):
    """No Bitbucket credentials found, or the ones found were rejected."""

    def __init__(self, message: str = "", *, payload: dict | None = None):
        super().__init__(message or (payload or {}).get("what", "Bitbucket not configured"), payload=payload)


class BitbucketApiError(ReviewApiError):
    """Non-auth HTTP failure. ``status`` is 0 for network-level errors."""

    def __init__(self, status: int, body: str):
        Exception.__init__(self, f"bitbucket api {status}: {body[:200]}")
        self.status = status
        self.body = body


# ── credentials ───────────────────────────────────────────────────────────

def _token_file() -> Path:
    return compat.user_home() / ".canopy" / "bitbucket.json"


def _read_token_file() -> dict:
    path = _token_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _credentials() -> tuple[str, str] | None:
    """``("bearer", token)`` or ``("basic", "email:token")`` or None.

    Precedence: access-token env → email+api-token env → token file.
    """
    access = os.environ.get("BITBUCKET_ACCESS_TOKEN")
    if access:
        return "bearer", access
    email = os.environ.get("BITBUCKET_EMAIL")
    api_token = os.environ.get("BITBUCKET_API_TOKEN")
    if email and api_token:
        return "basic", f"{email}:{api_token}"
    data = _read_token_file()
    if data.get("access_token"):
        return "bearer", str(data["access_token"])
    if data.get("email") and data.get("api_token"):
        return "basic", f"{data['email']}:{data['api_token']}"
    return None


def is_configured() -> bool:
    """True when some credential source is present. No network call."""
    return _credentials() is not None


def bitbucket_unavailable_blocker(rejected: bool = False) -> dict:
    """``{code, what, fix_actions}`` for the no-credentials (or bad-credentials) case."""
    what = (
        "Bitbucket credentials rejected (401/403). Check the token and its scopes."
        if rejected else
        "Bitbucket access not configured. Provide an access token or an "
        "Atlassian API token."
    )
    return {
        "code": "bitbucket_not_configured",
        "what": what,
        "fix_actions": [
            {"action": "set BITBUCKET_ACCESS_TOKEN", "args": {}, "safe": True,
             "preview": "Repository/workspace access token with pullrequest:write — sent as Bearer."},
            {"action": "set BITBUCKET_EMAIL + BITBUCKET_API_TOKEN", "args": {}, "safe": True,
             "preview": f"Set BITBUCKET_EMAIL and BITBUCKET_API_TOKEN — an Atlassian "
                        f"account API token from {_TOKEN_URL} — sent as HTTP Basic."},
            {"action": "write ~/.canopy/bitbucket.json", "args": {}, "safe": True,
             "preview": '{"email": "...", "api_token": "..."} or {"access_token": "..."}'},
        ],
    }


def _auth_header() -> str:
    creds = _credentials()
    if creds is None:
        raise BitbucketNotConfiguredError(payload=bitbucket_unavailable_blocker())
    kind, value = creds
    if kind == "bearer":
        return f"Bearer {value}"
    return "Basic " + base64.b64encode(value.encode("utf-8")).decode("ascii")


# ── transport ─────────────────────────────────────────────────────────────

def _request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    timeout: float = 15.0,
) -> Any:
    """One JSON round-trip. Returns the decoded body, or None when empty.

    401/403 raise ``BitbucketNotConfiguredError`` (credentials problem);
    any other non-2xx or network failure raises ``BitbucketApiError``.
    """
    url = API_ROOT + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", _auth_header())
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise BitbucketNotConfiguredError(payload=bitbucket_unavailable_blocker(rejected=True))
        text = ""
        if e.fp is not None:
            text = e.fp.read().decode("utf-8", "replace")
        raise BitbucketApiError(e.code, text)
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        raise BitbucketApiError(0, str(e))
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def _paginate(path: str, params: dict | None = None, *, limit: int | None = None) -> list[dict]:
    """Collect ``values`` across every page, stopping early once ``limit`` is met.
    Follows ``next`` by re-issuing the same path with the query parameters the
    ``next`` URL carries."""
    query = dict(params or {})
    query.setdefault("pagelen", 100)
    out: list[dict] = []
    while True:
        data = _request("GET", path, params=query) or {}
        out.extend(data.get("values") or [])
        if limit is not None and len(out) >= limit:
            return out[:limit]
        nxt = data.get("next")
        if not nxt:
            return out
        nxt_query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(nxt).query))
        if nxt_query == {k: str(v) for k, v in query.items()}:
            # A ``next`` pointing at the query we just sent would spin forever.
            return out
        query = nxt_query


_USER_UUID: str | None = None


def current_user_uuid() -> str:
    """UUID (with braces) of the authenticated user — the ``@me`` of Bitbucket."""
    global _USER_UUID
    if _USER_UUID is None:
        _USER_UUID = str((_request("GET", "/user") or {}).get("uuid") or "")
    return _USER_UUID


def _reset_cache() -> None:
    """Test-only."""
    global _USER_UUID
    _USER_UUID = None


# ── pull requests ─────────────────────────────────────────────────────────

# Partial-response spec appended to list calls: Bitbucket's list payloads
# omit participants/reviewers, which review_decision needs.
PR_FIELDS = "+values.participants,+values.reviewers"

_PR_STATE = {"OPEN": "open", "MERGED": "merged", "DECLINED": "closed", "SUPERSEDED": "closed"}


def _repo_path(owner: str, slug: str) -> str:
    return f"/repositories/{owner}/{slug}"


def _quote(value: str) -> str:
    """Quote a value for Bitbucket's ``q`` filter language."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _review_decision(data: dict) -> str:
    participants = data.get("participants") or []
    states = {(p.get("state") or "").lower() for p in participants}
    if "changes_requested" in states:
        return "CHANGES_REQUESTED"
    if "approved" in states or any(p.get("approved") for p in participants):
        return "APPROVED"
    if data.get("reviewers"):
        return "REVIEW_REQUIRED"
    return ""


def _normalize_pr(data: dict) -> dict:
    source = data.get("source") or {}
    dest = data.get("destination") or {}
    return {
        "number": data.get("id"),
        "title": data.get("title") or "",
        "url": ((data.get("links") or {}).get("html") or {}).get("href", ""),
        "state": _PR_STATE.get((data.get("state") or "").upper(), "open"),
        "head_branch": (source.get("branch") or {}).get("name", ""),
        "base_branch": (dest.get("branch") or {}).get("name", ""),
        "head_sha": (source.get("commit") or {}).get("hash", ""),
        "body": data.get("description") or "",
        "review_decision": _review_decision(data),
        "mergeable": "",
        "draft": bool(data.get("draft")),
    }


def find_pull_request(workspace_root: Path, owner: str, slug: str, branch: str) -> dict | None:
    """Open PR whose source branch is ``branch``; None when there is none.

    Re-fetches the PR by id when ``PR_FIELDS`` didn't take effect on the list
    payload, so ``review_decision`` never silently degrades to ``""``.
    """
    query = f"source.branch.name = {_quote(branch)} AND state = \"OPEN\""
    data = _request("GET", f"{_repo_path(owner, slug)}/pullrequests",
                    params={"q": query, "fields": PR_FIELDS, "pagelen": 5}) or {}
    values = data.get("values") or []
    if not values:
        return None
    return _normalize_pr(_with_participants(owner, slug, values[0]))


def _with_participants(owner: str, slug: str, listed: dict) -> dict:
    """Re-fetch a PR from a list payload when ``PR_FIELDS`` didn't take effect,
    so ``review_decision`` never silently degrades to ``""``."""
    if "participants" in listed:
        return listed
    return _request("GET", f"{_repo_path(owner, slug)}/pullrequests/{listed['id']}") or listed


def get_pull_request_by_number(workspace_root: Path, owner: str, slug: str, pr_number: int) -> dict | None:
    try:
        data = _request("GET", f"{_repo_path(owner, slug)}/pullrequests/{pr_number}")
    except BitbucketApiError as e:
        if e.status == 404:
            return None
        raise
    return _normalize_pr(data) if data else None


def list_open_prs(
    workspace_root: Path, owner: str, slug: str, author: str | None = None, limit: int = 50,
) -> list[dict]:
    """Open PRs, optionally by author. ``@me`` resolves to the authenticated
    user; any other value is taken as a user uuid."""
    query = 'state = "OPEN"'
    if author:
        uuid = current_user_uuid() if author == "@me" else author
        query += f" AND author.uuid = {_quote(uuid)}"
    values = _paginate(f"{_repo_path(owner, slug)}/pullrequests",
                       params={"q": query, "fields": PR_FIELDS, "pagelen": min(limit, 50)},
                       limit=limit)
    return [_normalize_pr(_with_participants(owner, slug, v)) for v in values]


class UnknownReviewerError(Exception):
    """One or more reviewer names didn't match a workspace member."""

    def __init__(self, names: list[str]):
        super().__init__(f"unknown or ambiguous reviewers: {', '.join(names)}; pass the {{uuid}} instead")
        self.names = names


def resolve_reviewers(owner: str, names: list[str]) -> list[str]:
    """Map nicknames / display names to uuids via the workspace member list.
    Values already shaped like ``{uuid}`` pass through untouched; a name shared
    by two or more members (case-insensitively) is rejected as ambiguous."""
    pending = [n for n in names if not (n.startswith("{") and n.endswith("}"))]
    by_name: dict[str, set[str]] = {}
    if pending:
        for member in _paginate(f"/workspaces/{owner}/members"):
            user = member.get("user") or {}
            uuid = user.get("uuid") or ""
            for key in (user.get("nickname"), user.get("display_name")):
                if key:
                    by_name.setdefault(key.lower(), set()).add(uuid)
    out: list[str] = []
    unknown: list[str] = []
    for name in names:
        if name.startswith("{") and name.endswith("}"):
            out.append(name)
        else:
            uuids = by_name.get(name.lower())
            if uuids and len(uuids) == 1:
                out.append(next(iter(uuids)))
            else:
                unknown.append(name)
    if unknown:
        raise UnknownReviewerError(unknown)
    return out


def create_pr(
    workspace_root: Path,
    owner: str,
    slug: str,
    *,
    branch: str,
    base: str,
    title: str,
    body: str,
    draft: bool = False,
    reviewers: list[str] | None = None,
) -> dict:
    payload: dict[str, Any] = {
        "title": title,
        "description": body,
        "source": {"branch": {"name": branch}},
        "destination": {"branch": {"name": base}},
        "draft": draft,
    }
    if reviewers:
        payload["reviewers"] = [{"uuid": u} for u in resolve_reviewers(owner, reviewers)]
    data = _request("POST", f"{_repo_path(owner, slug)}/pullrequests", body=payload, timeout=30.0)
    return _normalize_pr(data or {})


def update_pr_body(workspace_root: Path, owner: str, slug: str, pr_number: int, body: str) -> None:
    # Bitbucket's PUT is a replace, not a merge: omitting ``reviewers`` clears
    # the reviewer list, and some tenants reject a PUT without ``title``. Read
    # both back and send them through unchanged.
    pr = _request("GET", f"{_repo_path(owner, slug)}/pullrequests/{pr_number}") or {}
    _request("PUT", f"{_repo_path(owner, slug)}/pullrequests/{pr_number}", body={
        "title": pr.get("title") or "",
        "description": body,
        "reviewers": [{"uuid": r["uuid"]} for r in (pr.get("reviewers") or []) if r.get("uuid")],
    })


# ── commit statuses (CI) ─────────────────────────────────────────────────

def get_pr_checks(workspace_root: Path, owner: str, slug: str, pr_number: int) -> tuple[dict, list[dict]]:
    """Roll the PR head commit's statuses up to the github-shaped summary.

    Best-effort like the GitHub path: any failure returns ``no_checks``
    rather than raising, so CI never bricks ``feature_state`` — credential
    failures included, so a missing token reads here as "no checks", not as
    a blocker.
    """
    try:
        pr = _request("GET", f"{_repo_path(owner, slug)}/pullrequests/{pr_number}") or {}
        sha = ((pr.get("source") or {}).get("commit") or {}).get("hash") or ""
        if not sha:
            return {"status": "no_checks"}, []
        raw = _paginate(f"{_repo_path(owner, slug)}/commit/{sha}/statuses")
    except Exception:
        return {"status": "no_checks"}, []
    if not raw:
        return {"status": "no_checks"}, []
    details_url = ((pr.get("links") or {}).get("html") or {}).get("href", "")
    return _rollup_statuses(raw, details_url=details_url), raw


def _rollup_statuses(raw: list[dict], *, details_url: str) -> dict:
    passed = failing = pending = skipped = 0
    failing_names: list[str] = []
    pending_names: list[str] = []
    for s in raw:
        state = (s.get("state") or "").upper()
        name = s.get("name") or s.get("key") or ""
        if state == "SUCCESSFUL":
            passed += 1
        elif state in ("FAILED", "STOPPED"):
            failing += 1
            if name:
                failing_names.append(name)
        else:
            # INPROGRESS and anything unknown: wait rather than claim green.
            pending += 1
            if name:
                pending_names.append(name)
    if failing:
        status = "failing"
    elif pending:
        status = "pending"
    elif passed:
        status = "passing"
    else:
        status = "no_checks"
    return {
        "status": status,
        "passed": passed,
        "failing": failing,
        "pending": pending,
        "skipped": skipped,
        "required_failing": failing_names,
        "required_pending": pending_names,
        "details_url": details_url,
    }


# ── review threads ───────────────────────────────────────────────────────
#
# Bitbucket has flat comments with an optional ``parent``; a thread is a
# root comment plus every comment whose parent chain leads to it.

def _author_type(user: dict) -> str:
    # Bitbucket types real people as "user"; apps/bots carry another type
    # (e.g. "app_user"). Downstream bot detection keys on "Bot".
    return "User" if (user.get("type") or "user") == "user" else "Bot"


def _normalize_comment(c: dict) -> dict:
    inline = c.get("inline") or {}
    user = c.get("user") or {}
    return {
        "comment_id": c.get("id"),
        "path": inline.get("path") or "",
        "line": inline.get("to") or inline.get("from") or 0,
        "body": (c.get("content") or {}).get("raw") or "",
        "created_at": c.get("created_on") or "",
        "url": ((c.get("links") or {}).get("html") or {}).get("href", ""),
        "author": user.get("display_name") or user.get("nickname") or "",
        "author_type": _author_type(user),
    }


def _is_resolved(c: dict) -> bool:
    res = c.get("resolution") or {}
    return bool(res.get("user") or res.get("created_on"))


def list_review_threads(workspace_root: Path, owner: str, slug: str, pr_number: int) -> list[dict]:
    raw = _paginate(f"{_repo_path(owner, slug)}/pullrequests/{pr_number}/comments")
    raw.sort(key=lambda c: (c.get("created_on") or "", c.get("id") or 0))
    # Parent links and resolution state come from every comment, deleted
    # ones included: Bitbucket keeps ``resolution`` on the root only, and a
    # deleted root must still resolve the thread its surviving replies form.
    by_id = {c["id"]: c for c in raw}
    parent_of = {c["id"]: (c.get("parent") or {}).get("id") for c in raw}

    def root_of(cid: int) -> int:
        seen = set()
        while parent_of.get(cid) is not None and cid not in seen:
            seen.add(cid)
            cid = parent_of[cid]
        return cid

    threads: dict[int, dict] = {}
    for c in raw:                        # sorted by created_on → root always precedes its replies
        if c.get("deleted"):
            continue
        root_id = root_of(c["id"])
        if root_id not in threads:
            root = by_id.get(root_id, c)
            res = root.get("resolution") or {}
            threads[root_id] = {
                "thread_id": format_bitbucket_thread_id(owner, slug, pr_number, root_id),
                "is_resolved": _is_resolved(root),
                "resolved_at": res.get("created_on") or None,
                "comments": [],
            }
        threads[root_id]["comments"].append(_normalize_comment(c))
    return list(threads.values())


def get_review_comments(workspace_root: Path, owner: str, slug: str, pr_number: int) -> tuple[list[dict], int]:
    return build_comments_from_threads(list_review_threads(workspace_root, owner, slug, pr_number))


def resolve_thread(workspace_root: Path, owner: str, slug: str, pr_number: int, comment_id: int) -> dict:
    _request("POST", f"{_repo_path(owner, slug)}/pullrequests/{pr_number}/comments/{comment_id}/resolve")
    return {"thread_id": format_bitbucket_thread_id(owner, slug, pr_number, comment_id), "is_resolved": True}


def unresolve_thread(workspace_root: Path, owner: str, slug: str, pr_number: int, comment_id: int) -> dict:
    _request("DELETE", f"{_repo_path(owner, slug)}/pullrequests/{pr_number}/comments/{comment_id}/resolve")
    return {"thread_id": format_bitbucket_thread_id(owner, slug, pr_number, comment_id), "is_resolved": False}


def reply_to_thread(
    workspace_root: Path, owner: str, slug: str, pr_number: int, comment_id: int, body: str,
) -> dict:
    data = _request(
        "POST", f"{_repo_path(owner, slug)}/pullrequests/{pr_number}/comments",
        body={"content": {"raw": body}, "parent": {"id": comment_id}},
    ) or {}
    return {
        "comment_id": data.get("id"),
        "url": ((data.get("links") or {}).get("html") or {}).get("href", ""),
    }
