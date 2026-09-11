"""Tests for integrations/bitbucket.py — Bitbucket Cloud REST backend.

Every test routes ``bitbucket._request`` through ``FakeApi`` so nothing
touches the network. Fixture payloads are trimmed captures from a real
Bitbucket Cloud workspace.
"""
from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from canopy.integrations import bitbucket as bb
from canopy.integrations.platforms import PlatformNotConfiguredError


class FakeApi:
    """Route table keyed on (method, path). A route value is either a dict
    (returned as-is) or a callable ``(params, body) -> dict``."""

    def __init__(self):
        self.routes: dict[tuple[str, str], object] = {}
        self.calls: list[tuple[str, str, dict | None, dict | None]] = []

    def __call__(self, method, path, *, params=None, body=None, timeout=15.0):
        self.calls.append((method, path, params, body))
        route = self.routes.get((method, path))
        if route is None:
            raise bb.BitbucketApiError(404, json.dumps({"error": {"message": "not found"}}))
        return route(params, body) if callable(route) else route


@pytest.fixture
def api(monkeypatch):
    fake = FakeApi()
    monkeypatch.setattr(bb, "_request", fake)
    bb._reset_cache()
    return fake


@pytest.fixture
def no_creds(monkeypatch, tmp_path):
    for var in ("BITBUCKET_ACCESS_TOKEN", "BITBUCKET_EMAIL", "BITBUCKET_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("canopy.compat.user_home", lambda: tmp_path)
    return tmp_path


# ── credentials ──────────────────────────────────────────────────────────

def test_not_configured_without_any_credentials(no_creds):
    assert bb.is_configured() is False
    with pytest.raises(bb.BitbucketNotConfiguredError) as exc_info:
        bb._auth_header()
    payload = exc_info.value.payload
    assert payload["code"] == "bitbucket_not_configured"
    assert any("BITBUCKET_API_TOKEN" in fa["preview"] for fa in payload["fix_actions"])
    assert isinstance(exc_info.value, PlatformNotConfiguredError)


def test_access_token_wins(no_creds, monkeypatch):
    monkeypatch.setenv("BITBUCKET_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("BITBUCKET_EMAIL", "a@b.c")
    monkeypatch.setenv("BITBUCKET_API_TOKEN", "apitok")
    assert bb.is_configured() is True
    assert bb._auth_header() == "Bearer tok"


def test_email_and_api_token_use_basic(no_creds, monkeypatch):
    monkeypatch.setenv("BITBUCKET_EMAIL", "a@b.c")
    monkeypatch.setenv("BITBUCKET_API_TOKEN", "apitok")
    import base64
    expected = "Basic " + base64.b64encode(b"a@b.c:apitok").decode("ascii")
    assert bb._auth_header() == expected


def test_email_alone_is_not_configured(no_creds, monkeypatch):
    monkeypatch.setenv("BITBUCKET_EMAIL", "a@b.c")
    assert bb.is_configured() is False


def test_token_file_fallback(no_creds):
    cfg = no_creds / ".canopy"
    cfg.mkdir()
    (cfg / "bitbucket.json").write_text(json.dumps({"email": "x@y.z", "api_token": "filetok"}), encoding="utf-8")
    assert bb.is_configured() is True
    assert bb._auth_header().startswith("Basic ")


def test_token_file_access_token(no_creds):
    cfg = no_creds / ".canopy"
    cfg.mkdir()
    (cfg / "bitbucket.json").write_text(json.dumps({"access_token": "ft"}), encoding="utf-8")
    assert bb._auth_header() == "Bearer ft"


def test_corrupt_token_file_is_ignored(no_creds):
    cfg = no_creds / ".canopy"
    cfg.mkdir()
    (cfg / "bitbucket.json").write_text("{not json", encoding="utf-8")
    assert bb.is_configured() is False


# ── transport ────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, payload: bytes):
        self._payload = payload
    def read(self):
        return self._payload
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_request_builds_url_and_headers(monkeypatch):
    monkeypatch.setenv("BITBUCKET_ACCESS_TOKEN", "tok")
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["auth"] = req.get_header("Authorization")
        seen["ctype"] = req.get_header("Content-type")
        seen["data"] = req.data
        return _Resp(b'{"ok": true}')

    monkeypatch.setattr(bb.urllib.request, "urlopen", fake_urlopen)
    out = bb._request("POST", "/repositories/ws/slug/pullrequests",
                      params={"q": 'state="OPEN"'}, body={"title": "t"})
    assert out == {"ok": True}
    assert seen["url"] == "https://api.bitbucket.org/2.0/repositories/ws/slug/pullrequests?q=state%3D%22OPEN%22"
    assert seen["method"] == "POST"
    assert seen["auth"] == "Bearer tok"
    assert seen["ctype"] == "application/json"
    assert json.loads(seen["data"]) == {"title": "t"}


def test_request_empty_body_returns_none(monkeypatch):
    monkeypatch.setenv("BITBUCKET_ACCESS_TOKEN", "tok")
    monkeypatch.setattr(bb.urllib.request, "urlopen", lambda req, timeout=None: _Resp(b""))
    assert bb._request("DELETE", "/x") is None


def test_request_401_becomes_not_configured(monkeypatch):
    monkeypatch.setenv("BITBUCKET_ACCESS_TOKEN", "bad")

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(bb.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(bb.BitbucketNotConfiguredError) as exc_info:
        bb._request("GET", "/user")
    assert "rejected" in exc_info.value.payload["what"]


def test_request_other_http_error(monkeypatch):
    monkeypatch.setenv("BITBUCKET_ACCESS_TOKEN", "tok")
    import io

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b'{"error":{"message":"gone"}}'))

    monkeypatch.setattr(bb.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(bb.BitbucketApiError) as exc_info:
        bb._request("GET", "/x")
    assert exc_info.value.status == 404
    assert "gone" in exc_info.value.body


def test_paginate_follows_next(api):
    def page(params, body):
        if (params or {}).get("page") == "2":
            return {"values": [{"id": 3}]}
        return {"values": [{"id": 1}, {"id": 2}],
                "next": "https://api.bitbucket.org/2.0/x?pagelen=100&page=2"}
    api.routes[("GET", "/x")] = page
    assert bb._paginate("/x") == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert api.calls[0][2]["pagelen"] == 100
    assert api.calls[1][2]["page"] == "2"


def test_current_user_uuid_is_cached(api):
    api.routes[("GET", "/user")] = {"uuid": "{u-1}", "display_name": "Me"}
    assert bb.current_user_uuid() == "{u-1}"
    assert bb.current_user_uuid() == "{u-1}"
    assert len(api.calls) == 1


# ── PR reads ─────────────────────────────────────────────────────────────

WS, SLUG = "filoventeam", "report.server"
REPO = f"/repositories/{WS}/{SLUG}"

PR_26 = {
    "id": 26,
    "title": "#11205 retours de test : editeur de carte",
    "description": "Retours de test de l'editeur de carte.",
    "state": "MERGED",
    "draft": False,
    "author": {"display_name": "Arsene Buzenet", "uuid": "{e399dcb1}", "type": "user"},
    "source": {"branch": {"name": "feature/#11205-retour-test"},
               "commit": {"hash": "3df742df3d63"}},
    "destination": {"branch": {"name": "dev"}, "commit": {"hash": "0b1f3cfc5e0e"}},
    "links": {"html": {"href": "https://bitbucket.org/filoventeam/report.server/pull-requests/26"}},
    "reviewers": [],
    "participants": [{"user": {"uuid": "{e399dcb1}"}, "role": "PARTICIPANT", "approved": False, "state": None}],
}


def _pr(**over):
    data = json.loads(json.dumps(PR_26))
    data.update(over)
    return data


def test_normalize_pr_shape():
    pr = bb._normalize_pr(PR_26)
    assert pr == {
        "number": 26,
        "title": "#11205 retours de test : editeur de carte",
        "url": "https://bitbucket.org/filoventeam/report.server/pull-requests/26",
        "state": "merged",
        "head_branch": "feature/#11205-retour-test",
        "base_branch": "dev",
        "head_sha": "3df742df3d63",
        "body": "Retours de test de l'editeur de carte.",
        "review_decision": "",
        "mergeable": "",
        "draft": False,
    }


@pytest.mark.parametrize("state,expected", [
    ("OPEN", "open"), ("MERGED", "merged"), ("DECLINED", "closed"), ("SUPERSEDED", "closed"),
])
def test_normalize_pr_state_mapping(state, expected):
    assert bb._normalize_pr(_pr(state=state))["state"] == expected


def test_review_decision_changes_requested_wins():
    data = _pr(state="OPEN", reviewers=[{"uuid": "{r}"}], participants=[
        {"user": {"uuid": "{a}"}, "role": "REVIEWER", "approved": True, "state": "approved"},
        {"user": {"uuid": "{b}"}, "role": "REVIEWER", "approved": False, "state": "changes_requested"},
    ])
    assert bb._normalize_pr(data)["review_decision"] == "CHANGES_REQUESTED"


def test_review_decision_approved():
    data = _pr(state="OPEN", reviewers=[{"uuid": "{r}"}], participants=[
        {"user": {"uuid": "{a}"}, "role": "REVIEWER", "approved": True, "state": "approved"},
    ])
    assert bb._normalize_pr(data)["review_decision"] == "APPROVED"


def test_review_decision_review_required_when_reviewers_pending():
    data = _pr(state="OPEN", reviewers=[{"uuid": "{r}"}], participants=[
        {"user": {"uuid": "{r}"}, "role": "REVIEWER", "approved": False, "state": None},
    ])
    assert bb._normalize_pr(data)["review_decision"] == "REVIEW_REQUIRED"


def test_review_decision_empty_without_reviewers():
    assert bb._normalize_pr(_pr(state="OPEN"))["review_decision"] == ""


def test_find_pull_request_queries_branch(api):
    api.routes[("GET", f"{REPO}/pullrequests")] = {"values": [_pr(state="OPEN")]}
    pr = bb.find_pull_request(Path("."), WS, SLUG, "feature/#11205-retour-test")
    assert pr["number"] == 26 and pr["state"] == "open"
    _, _, params, _ = api.calls[0]
    assert params["q"] == 'source.branch.name = "feature/#11205-retour-test" AND state = "OPEN"'
    assert params["fields"] == bb.PR_FIELDS


def test_find_pull_request_none_when_empty(api):
    api.routes[("GET", f"{REPO}/pullrequests")] = {"values": []}
    assert bb.find_pull_request(Path("."), WS, SLUG, "nope") is None


def test_get_pull_request_by_number(api):
    api.routes[("GET", f"{REPO}/pullrequests/26")] = PR_26
    pr = bb.get_pull_request_by_number(Path("."), WS, SLUG, 26)
    assert pr["number"] == 26 and pr["head_sha"] == "3df742df3d63"


def test_get_pull_request_by_number_404_is_none(api):
    assert bb.get_pull_request_by_number(Path("."), WS, SLUG, 999) is None


def test_list_open_prs_me_filters_by_uuid(api):
    api.routes[("GET", "/user")] = {"uuid": "{me}"}
    api.routes[("GET", f"{REPO}/pullrequests")] = {"values": [_pr(state="OPEN", id=1), _pr(state="OPEN", id=2)]}
    prs = bb.list_open_prs(Path("."), WS, SLUG, author="@me")
    assert [p["number"] for p in prs] == [1, 2]
    q = [c for c in api.calls if c[1] == f"{REPO}/pullrequests"][0][2]["q"]
    assert q == 'state = "OPEN" AND author.uuid = "{me}"'


def test_list_open_prs_no_author(api):
    api.routes[("GET", f"{REPO}/pullrequests")] = {"values": [_pr(state="OPEN")]}
    prs = bb.list_open_prs(Path("."), WS, SLUG)
    assert len(prs) == 1
    assert api.calls[0][2]["q"] == 'state = "OPEN"'


def test_list_open_prs_respects_limit(api):
    api.routes[("GET", f"{REPO}/pullrequests")] = {"values": [_pr(state="OPEN", id=i) for i in range(5)]}
    assert len(bb.list_open_prs(Path("."), WS, SLUG, limit=2)) == 2


def test_list_open_prs_stops_paging_once_limit_reached(api):
    def page(params, body):
        n = int((params or {}).get("page", "1"))
        return {"values": [_pr(state="OPEN", id=n)],
                "next": f"https://api.bitbucket.org/2.0/x?pagelen=1&page={n + 1}"}
    api.routes[("GET", f"{REPO}/pullrequests")] = page
    prs = bb.list_open_prs(Path("."), WS, SLUG, limit=2)
    assert [p["number"] for p in prs] == [1, 2]
    assert len([c for c in api.calls if c[1] == f"{REPO}/pullrequests"]) == 2
