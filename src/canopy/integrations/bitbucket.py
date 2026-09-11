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
from .platforms import PlatformNotConfiguredError, format_bitbucket_thread_id

API_ROOT = "https://api.bitbucket.org/2.0"
_TOKEN_URL = "https://id.atlassian.com/manage-profile/security/api-tokens"


class BitbucketNotConfiguredError(PlatformNotConfiguredError):
    """No Bitbucket credentials found, or the ones found were rejected."""

    def __init__(self, message: str = "", *, payload: dict | None = None):
        super().__init__(message or (payload or {}).get("what", "Bitbucket not configured"), payload=payload)


class BitbucketApiError(Exception):
    """Non-auth HTTP failure. ``status`` is 0 for network-level errors."""

    def __init__(self, status: int, body: str):
        super().__init__(f"bitbucket api {status}: {body[:200]}")
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


def _paginate(path: str, params: dict | None = None) -> list[dict]:
    """Collect ``values`` across every page. Follows ``next`` by re-issuing
    the same path with the query parameters the ``next`` URL carries."""
    query = dict(params or {})
    query.setdefault("pagelen", 100)
    out: list[dict] = []
    while True:
        data = _request("GET", path, params=query) or {}
        out.extend(data.get("values") or [])
        nxt = data.get("next")
        if not nxt:
            return out
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(nxt).query))


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
