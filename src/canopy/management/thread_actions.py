"""Thread action wrappers — resolve, reply, unresolve a review thread.

Each wrapper calls the review-platform façade and records the event locally
in ``.canopy/state/thread_resolutions.json`` so the resume brief can
attribute "resolved by canopy" vs "resolved on the platform directly".

Thread ids are GitHub node ids (``PRRT_…``) or Bitbucket coordinates
(``bb:<workspace>/<repo>#<pr>/<comment>``); the façade dispatches on the form.
"""
from __future__ import annotations

from ..workspace.workspace import Workspace
from ..actions.errors import ActionError, BlockerError
from ..integrations import review
from . import thread_resolutions as tr


def _validate_thread_id(thread_id: str) -> None:
    try:
        review.parse_thread_id(thread_id)
    except ValueError as e:
        raise BlockerError(code="invalid_thread_id", what=str(e))


def resolve_thread(
    workspace: Workspace,
    thread_id: str,
    *,
    feature: str,
    via_command: str = "resolve",
    via_commit_sha: str | None = None,
) -> dict:
    """Resolve a review thread and record it locally.

    Raises:
        BlockerError: if ``thread_id`` matches neither platform's form.
    """
    _validate_thread_id(thread_id)
    result = review.resolve_thread(workspace.config.root, thread_id)
    log_entry = tr.record(
        workspace.config.root,
        thread_id=thread_id,
        feature=feature,
        via_command=via_command,
        via_commit_sha=via_commit_sha,
    )
    return {**result, "logged": log_entry}


def reply_to_thread(
    workspace: Workspace,
    thread_id: str,
    body: str,
    *,
    feature: str,
    resolve_after: bool = False,
) -> dict:
    """Post a reply to a review thread, optionally resolving it.

    Returns a dict with ``posted`` and optionally ``resolved`` keys.

    Raises:
        BlockerError: if ``thread_id`` matches neither platform's form.
    """
    _validate_thread_id(thread_id)
    posted = review.reply_to_thread(workspace.config.root, thread_id, body)
    result: dict = {"posted": posted}
    if resolve_after:
        try:
            res = resolve_thread(
                workspace, thread_id, feature=feature, via_command="reply_resolve",
            )
            result["resolved"] = res
        except ActionError as e:
            result["resolved"] = {"error": e.to_dict()}
    return result
