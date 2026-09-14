"""Tests for integrations/platforms.py — remote/PR-URL/thread-id parsing."""
import pytest

from canopy.integrations.platforms import (
    PlatformNotConfiguredError, RemoteRef, ThreadRef,
    format_bitbucket_thread_id, parse_pr_url, parse_remote, parse_thread_id,
)


@pytest.mark.parametrize("url,expected", [
    ("git@github.com:ashmitb/canopy.git", RemoteRef("github", "ashmitb", "canopy")),
    ("git@github.com:org/repo", RemoteRef("github", "org", "repo")),
    ("https://github.com/ashmitb/canopy.git", RemoteRef("github", "ashmitb", "canopy")),
    ("https://github.com/org/repo", RemoteRef("github", "org", "repo")),
    ("https://arsene1@bitbucket.org/filoventeam/report.server.git",
     RemoteRef("bitbucket", "filoventeam", "report.server")),
    ("https://bitbucket.org/filoventeam/filo.common", RemoteRef("bitbucket", "filoventeam", "filo.common")),
    ("git@bitbucket.org:filoventeam/api_server.git", RemoteRef("bitbucket", "filoventeam", "api_server")),
    ("ssh://git@bitbucket.org/filoventeam/filo.bd.git", RemoteRef("bitbucket", "filoventeam", "filo.bd")),
    ("HTTPS://BitBucket.org/ws/slug.git", RemoteRef("bitbucket", "ws", "slug")),
])
def test_parse_remote_recognised(url, expected):
    assert parse_remote(url) == expected


@pytest.mark.parametrize("url", [
    "https://gitlab.com/org/repo.git", "", "not-a-url", "file:///tmp/repo",
    "https://bitbucket.example.com/scm/proj/repo.git",   # Data Center — out of scope
])
def test_parse_remote_unrecognised(url):
    assert parse_remote(url) is None


def test_parse_pr_url_github():
    assert parse_pr_url("https://github.com/owner/repo-a/pull/1287") == (
        RemoteRef("github", "owner", "repo-a"), 1287)


def test_parse_pr_url_bitbucket():
    assert parse_pr_url("https://bitbucket.org/filoventeam/report.server/pull-requests/26") == (
        RemoteRef("bitbucket", "filoventeam", "report.server"), 26)


def test_parse_pr_url_bitbucket_with_suffix():
    url = "https://bitbucket.org/filoventeam/report.server/pull-requests/26/diff"
    assert parse_pr_url(url) == (RemoteRef("bitbucket", "filoventeam", "report.server"), 26)


def test_parse_pr_url_rejects_other():
    assert parse_pr_url("https://github.com/owner/repo/issues/5") is None
    assert parse_pr_url("repo-a#42") is None


def test_parse_thread_id_github():
    ref = parse_thread_id("PRRT_kwDOAbc123")
    assert ref == ThreadRef("github", "PRRT_kwDOAbc123", None, None, None)


def test_parse_thread_id_bitbucket():
    ref = parse_thread_id("bb:filoventeam/report.server#27/860145471")
    assert ref == ThreadRef(
        "bitbucket", None, RemoteRef("bitbucket", "filoventeam", "report.server"), 27, 860145471)


def test_format_bitbucket_thread_id_roundtrips():
    tid = format_bitbucket_thread_id("filoventeam", "report.server", 27, 860145471)
    assert tid == "bb:filoventeam/report.server#27/860145471"
    assert parse_thread_id(tid).comment_id == 860145471


@pytest.mark.parametrize("bad", ["", "abc", "bb:", "bb:ws/slug#x/1", "bb:ws#1/2", "PRRT"])
def test_parse_thread_id_invalid(bad):
    with pytest.raises(ValueError):
        parse_thread_id(bad)


def test_platform_error_carries_payload():
    err = PlatformNotConfiguredError(payload={"code": "x", "what": "nope", "fix_actions": []})
    assert err.payload["code"] == "x"
    assert str(err) == "nope"


def test_github_error_is_a_platform_error():
    from canopy.integrations.github import GitHubNotConfiguredError
    assert issubclass(GitHubNotConfiguredError, PlatformNotConfiguredError)
    assert GitHubNotConfiguredError("boom").payload == {}
