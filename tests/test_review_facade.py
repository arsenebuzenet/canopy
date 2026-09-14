"""Tests for integrations/review.py — dispatch by RemoteRef / thread id."""
from pathlib import Path

import pytest

from canopy.integrations import review, github, bitbucket
from canopy.integrations.platforms import RemoteRef

GH = RemoteRef("github", "acme", "api")
BB = RemoteRef("bitbucket", "filoventeam", "report.server")


def test_find_pull_request_dispatches_github(monkeypatch):
    monkeypatch.setattr(github, "find_pull_request", lambda root, o, s, b: {"number": 1, "via": (o, s, b)})
    monkeypatch.setattr(bitbucket, "find_pull_request", lambda *a: pytest.fail("wrong backend"))
    assert review.find_pull_request(Path("."), GH, "main")["via"] == ("acme", "api", "main")


def test_find_pull_request_dispatches_bitbucket(monkeypatch):
    monkeypatch.setattr(bitbucket, "find_pull_request", lambda root, o, s, b: {"number": 2, "via": (o, s, b)})
    monkeypatch.setattr(github, "find_pull_request", lambda *a: pytest.fail("wrong backend"))
    assert review.find_pull_request(Path("."), BB, "dev")["via"] == ("filoventeam", "report.server", "dev")


def test_create_pr_forwards_kwargs(monkeypatch):
    seen = {}

    def fake(root, o, s, **kw):
        seen.update(kw)
        return {"number": 3}

    monkeypatch.setattr(bitbucket, "create_pr", fake)
    review.create_pr(Path("."), BB, branch="b", base="dev", title="t", body="x", draft=True, reviewers=["a"])
    assert seen == {"branch": "b", "base": "dev", "title": "t", "body": "x", "draft": True, "reviewers": ["a"]}


def test_thread_ops_dispatch_on_id_prefix(monkeypatch):
    monkeypatch.setattr(github, "resolve_thread", lambda root, tid: {"thread_id": tid, "is_resolved": True})
    monkeypatch.setattr(bitbucket, "resolve_thread",
                        lambda root, o, s, pr, cid: {"thread_id": f"bb:{o}/{s}#{pr}/{cid}", "is_resolved": True})
    assert review.resolve_thread(Path("."), "PRRT_x")["thread_id"] == "PRRT_x"
    out = review.resolve_thread(Path("."), "bb:filoventeam/report.server#27/5")
    assert out["thread_id"] == "bb:filoventeam/report.server#27/5"


def test_reply_to_thread_bitbucket(monkeypatch):
    monkeypatch.setattr(bitbucket, "reply_to_thread",
                        lambda root, o, s, pr, cid, body: {"comment_id": 9, "url": body})
    assert review.reply_to_thread(Path("."), "bb:ws/slug#1/2", "hi") == {"comment_id": 9, "url": "hi"}


def test_thread_ops_reject_bad_id():
    with pytest.raises(ValueError):
        review.resolve_thread(Path("."), "nonsense")


def test_is_configured_per_platform(monkeypatch):
    monkeypatch.setattr(github, "is_github_configured", lambda root: False)
    monkeypatch.setattr(bitbucket, "is_configured", lambda: True)
    assert review.is_configured(Path("."), GH) is False
    assert review.is_configured(Path("."), BB) is True


def test_unavailable_blocker_codes():
    assert review.unavailable_blocker(GH)["code"] == "github_not_configured"
    assert review.unavailable_blocker(BB)["code"] == "bitbucket_not_configured"


def test_unknown_platform_raises():
    with pytest.raises(ValueError):
        review.find_pull_request(Path("."), RemoteRef("gitlab", "o", "s"), "b")


def test_platform_label():
    assert review.platform_label("github") == "GitHub"
    assert review.platform_label("bitbucket") == "Bitbucket"


def test_reexports():
    assert review.PlatformNotConfiguredError is not None
    assert review.PullRequestNotFoundError is github.PullRequestNotFoundError
    assert review.parse_remote("git@github.com:a/b.git") == RemoteRef("github", "a", "b")
