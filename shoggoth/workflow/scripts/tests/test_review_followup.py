"""Unit tests for the review follow-up logic.

When a review is posted with inline comments, the review
body has "see inline comment" placeholders. Gitea doesn't
expose per-comment URLs in the POST review response and
doesn't support editing a posted review body, so a
follow-up issue comment is posted with a clickable index
of the inline comments.

These tests pin:

  - ``Gitea.get_pr_review_comments`` is called with the
    right URL
  - the follow-up body includes verdict, summary, and
    one markdown link per inline comment
  - the link is built from the canonical
    ``pull_request_url`` returned by the POST response
    (not the per-comment ``html_url`` from GET), matched
    by ``(path, line)`` against the inline comment we
    sent
  - the canonical ``pull_request_url`` is preferred over
    any legacy ``html_url`` returned in the GET response,
    so a stale per-comment URL cannot break the index
  - the full text of each finding is NOT duplicated in
    the follow-up (it lives in the inline comments)
  - missing ``pull_request_url`` falls back to plain
    text (the lines are still listed, just not
    clickable)
  - failures (fetch error, post error, empty list) are
    logged but don't abort the workflow
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, SCRIPTS_DIR)
os.environ.setdefault("SHOGGOTH_DOMAIN", "example.test")

from shoggoth.commands.pr_update import PrUpdateCommand  # noqa: E402
from shoggoth.gitea import Gitea  # noqa: E402


def _make_cmd():
    """Build a ``PrUpdateCommand`` with stubbed collaborators
    for the follow-up tests. The stubs are minimal — only
    ``gitea`` and ``agent`` are exercised here."""
    cmd = PrUpdateCommand.__new__(PrUpdateCommand)
    cmd.gitea = MagicMock()
    cmd.agent = MagicMock()
    cmd.shoggoth = MagicMock()
    return cmd


class TestPostReviewFollowupWithLinks:
    def _review_data(self):
        return {
            "verdict": "request_changes",
            "summary": "Two issues need addressing.",
            "findings": [
                {"severity": "Critical", "path": "Makefile",
                 "line": 493, "body": "Wrong flag."},
                {"severity": "info", "path": "README.md",
                 "line": 260, "body": "Typo."},
            ],
        }

    def _inline_comments(self):
        return [
            {"path": "Makefile", "new_position": 493,
             "body": "**[CRITICAL]** at `Makefile:493`\n\nWrong flag."},
            {"path": "README.md", "new_position": 260,
             "body": "**[INFO]** at `README.md:260`\n\nTypo."},
        ]

    def _gitea_comments(self):
        # Gitea returns ``position`` (= LineNum, the file
        # line number) and ``id`` for each comment. The
        # ``html_url`` field is also present but we now
        # construct the URL from ``pr_url`` + ``id`` so
        # the canonical PR URL is used.
        return [
            {"id": 1001, "path": "Makefile", "position": 493,
             "html_url": "https://gitea.example.com/o/r/pulls/42#issuecomment-1001"},
            {"id": 1002, "path": "README.md", "position": 260,
             "html_url": "https://gitea.example.com/o/r/pulls/42#issuecomment-1002"},
        ]

    def _pr_url(self):
        # ``pull_request_url`` from the POST review
        # response — the canonical PR URL.
        return "https://gitea.example.com/o/r/pulls/42"

    def test_happy_path_posts_followup_with_links(self):
        cmd = _make_cmd()
        cmd.gitea.get_pr_review_comments = MagicMock(
            return_value=self._gitea_comments())
        cmd.gitea.post_pr_comment = MagicMock(return_value={"id": 999})
        otlp = MagicMock()

        cmd._post_review_followup_with_links(
            "owner/repo", 42, self._pr_url(), 7,
            self._review_data(), self._inline_comments(), otlp)

        # The fetch was made with the right URL.
        cmd.gitea.get_pr_review_comments.assert_called_once_with(
            "owner/repo", 42, 7)
        # The follow-up was posted.
        cmd.gitea.post_pr_comment.assert_called_once()
        call_args = cmd.gitea.post_pr_comment.call_args
        assert call_args.args[0] == "owner/repo"
        assert call_args.args[1] == 42
        body = call_args.args[2]
        # Verdict and summary are in the body.
        assert "**Verdict:** request_changes" in body
        assert "Two issues need addressing." in body
        # One clickable link per inline comment. The
        # URL is constructed from ``pr_url`` (the
        # canonical base from the POST response) +
        # ``#issuecomment-{id}`` (from the GET
        # response).
        assert "[`Makefile:493`](https://gitea.example.com/o/r/pulls/42#issuecomment-1001)" in body
        assert "[`README.md:260`](https://gitea.example.com/o/r/pulls/42#issuecomment-1002)" in body
        # Full text is NOT duplicated.
        assert "Wrong flag." not in body
        assert "Typo." not in body
        # OTLP got status lines.
        assert any("posting follow-up summary"
                    in str(c) for c in otlp._push_line.call_args_list)
        assert any("follow-up summary posted"
                    in str(c) for c in otlp._push_line.call_args_list)

    def test_url_constructed_from_canonical_pr_url(self):
        # The URL must be constructed from ``pr_url``
        # (the canonical base from the POST response)
        # and the per-comment ``id`` from the GET
        # response — NOT from the ``html_url`` field
        # in the GET response (which we no longer
        # trust as the canonical URL).
        cmd = _make_cmd()
        # GET response has a different ``html_url``
        # than what we'd build from ``pr_url`` + id
        # (e.g. a CDN URL or a legacy format). The
        # follow-up must use the canonical
        # ``{pr_url}#issuecomment-{id}`` form.
        cmd.gitea.get_pr_review_comments = MagicMock(
            return_value=[
                {"id": 1001, "path": "Makefile", "position": 493,
                 "html_url": "https://legacy.example.com/x"},
            ])
        cmd.gitea.post_pr_comment = MagicMock(return_value={"id": 999})
        otlp = MagicMock()

        cmd._post_review_followup_with_links(
            "owner/repo", 42, self._pr_url(), 7,
            self._review_data(), self._inline_comments(), otlp)

        body = cmd.gitea.post_pr_comment.call_args.args[2]
        # The follow-up uses the canonical URL form,
        # not the legacy ``html_url`` from the GET
        # response.
        assert "https://gitea.example.com/o/r/pulls/42#issuecomment-1001" in body
        assert "https://legacy.example.com/x" not in body

    def test_unmatched_path_falls_back_to_plain_text(self):
        # If the Gitea response doesn't include a
        # matching (path, line), the line is still
        # listed but as plain text (no markdown link).
        cmd = _make_cmd()
        # Only return a comment for Makefile; README.md
        # is missing from the response.
        cmd.gitea.get_pr_review_comments = MagicMock(
            return_value=[self._gitea_comments()[0]])
        cmd.gitea.post_pr_comment = MagicMock(return_value={"id": 999})
        otlp = MagicMock()

        cmd._post_review_followup_with_links(
            "owner/repo", 42, self._pr_url(), 7,
            self._review_data(), self._inline_comments(), otlp)

        body = cmd.gitea.post_pr_comment.call_args.args[2]
        # Makefile has its link.
        assert "issuecomment-1001" in body
        # README.md is listed but as plain text.
        assert "- `README.md:260`" in body
        # And it does NOT have a fabricated link.
        assert "issuecomment-1002" not in body

    def test_missing_pr_url_falls_back_to_plain_text(self):
        # If the POST review response didn't include
        # ``pull_request_url`` (defensive — should
        # always be present), the follow-up still
        # posts but all entries are plain text (no
        # links, because we have no base URL).
        cmd = _make_cmd()
        cmd.gitea.get_pr_review_comments = MagicMock(
            return_value=self._gitea_comments())
        cmd.gitea.post_pr_comment = MagicMock(return_value={"id": 999})
        otlp = MagicMock()

        cmd._post_review_followup_with_links(
            "owner/repo", 42, "", 7,
            self._review_data(), self._inline_comments(), otlp)

        body = cmd.gitea.post_pr_comment.call_args.args[2]
        # No links — both lines are plain text.
        assert "[" not in body
        assert "issuecomment-1001" not in body
        assert "issuecomment-1002" not in body
        # But the lines are still listed.
        assert "`Makefile:493`" in body
        assert "`README.md:260`" in body

    def test_missing_verdict_and_summary_still_posts(self):
        # If the review data has no verdict or summary,
        # the follow-up is still posted (with the
        # inline-comment index).
        cmd = _make_cmd()
        cmd.gitea.get_pr_review_comments = MagicMock(
            return_value=self._gitea_comments())
        cmd.gitea.post_pr_comment = MagicMock(return_value={"id": 999})
        otlp = MagicMock()

        cmd._post_review_followup_with_links(
            "owner/repo", 42, self._pr_url(), 7, {},
            self._inline_comments(), otlp)

        body = cmd.gitea.post_pr_comment.call_args.args[2]
        # No verdict, no summary — just the index.
        assert "**Verdict:**" not in body
        assert "### Inline comments" in body
        assert "issuecomment-1001" in body
        assert "issuecomment-1002" in body

    def test_fetch_failure_logs_and_returns(self):
        # If the comments fetch raises, the follow-up
        # is skipped (the inline comments are already
        # posted — only the follow-up links are
        # missing). The workflow continues.
        cmd = _make_cmd()
        cmd.gitea.get_pr_review_comments = MagicMock(
            side_effect=RuntimeError("network down"))
        cmd.gitea.post_pr_comment = MagicMock()
        otlp = MagicMock()

        # Must not raise.
        cmd._post_review_followup_with_links(
            "owner/repo", 42, self._pr_url(), 7,
            self._review_data(), self._inline_comments(), otlp)

        # No follow-up was posted.
        cmd.gitea.post_pr_comment.assert_not_called()
        # A warning was logged.
        assert any("failed to fetch"
                    in str(c) for c in otlp._push_line.call_args_list)

    def test_empty_comments_response_logs_and_returns(self):
        # Gitea returned an empty list (e.g. the review
        # had no inline comments after all). Skip the
        # follow-up rather than posting an empty index.
        cmd = _make_cmd()
        cmd.gitea.get_pr_review_comments = MagicMock(return_value=[])
        cmd.gitea.post_pr_comment = MagicMock()
        otlp = MagicMock()

        cmd._post_review_followup_with_links(
            "owner/repo", 42, self._pr_url(), 7,
            self._review_data(), self._inline_comments(), otlp)

        cmd.gitea.post_pr_comment.assert_not_called()
        assert any("no comments returned"
                    in str(c) for c in otlp._push_line.call_args_list)

    def test_get_path_prefix_normalised_to_match_agent(self):
        # Regression: the GET response uses ``path``
        # exactly as Gitea stored it (which can carry
        # a leading ``./`` or ``/``), while the agent
        # stashes the normalised form on each inline
        # comment. Both sides must apply
        # ``_normalise_diff_path`` so the
        # ``(path, line)`` lookup matches. Otherwise a
        # comment at, say, ``./Makefile:493`` (agent)
        # would miss the GET entry at ``./Makefile:493``
        # when the agent actually sent ``Makefile:493``,
        # silently degrading the follow-up to plain
        # text. The follow-up ends with a clickable
        # ``{pr_url}#issuecomment-{id}`` link anyway.
        cmd = _make_cmd()
        cmd.gitea.get_pr_review_comments = MagicMock(
            return_value=[
                {"id": 1001, "path": "./Makefile",
                 "position": 493},
            ])
        cmd.gitea.post_pr_comment = MagicMock(return_value={"id": 999})
        otlp = MagicMock()

        cmd._post_review_followup_with_links(
            "owner/repo", 42, self._pr_url(), 7,
            self._review_data(), self._inline_comments(), otlp)

        body = cmd.gitea.post_pr_comment.call_args.args[2]
        # The lookup successfully matched the agent's
        # normalised form against the GET response's
        # prefixed form and built the canonical link.
        assert "https://gitea.example.com/o/r/pulls/42#issuecomment-1001" in body
        # Not the plain-text fallback.
        assert "- `Makefile:493`\n" not in body

    def test_post_failure_logs_but_does_not_raise(self):
        # If posting the follow-up fails (e.g. Gitea
        # rejects the comment), the workflow continues
        # — the review itself was already posted.
        cmd = _make_cmd()
        cmd.gitea.get_pr_review_comments = MagicMock(
            return_value=self._gitea_comments())
        cmd.gitea.post_pr_comment = MagicMock(return_value=None)
        otlp = MagicMock()

        # Must not raise.
        cmd._post_review_followup_with_links(
            "owner/repo", 42, self._pr_url(), 7,
            self._review_data(), self._inline_comments(), otlp)

        assert any("failed to post follow-up"
                    in str(c) for c in otlp._push_line.call_args_list)


class TestGiteaGetPrReviewComments:
    """The new ``get_pr_review_comments`` helper on the
    Gitea client. Wraps ``GET
    /repos/{repo}/pulls/{n}/reviews/{id}/comments`` and
    returns the parsed list of comment dicts (each with
    ``id``, ``path``, ``position``, ``html_url``)."""

    def test_calls_correct_url(self):
        g = Gitea.__new__(Gitea)
        g.get = MagicMock(return_value=[
            {"id": 1001, "path": "foo.py", "position": 5,
             "html_url": "https://gitea.example.com/o/r/pulls/1#issuecomment-1001"},
        ])
        result = g.get_pr_review_comments("owner/repo", 1, 7)
        g.get.assert_called_once_with(
            "repos/owner/repo/pulls/1/reviews/7/comments")
        assert len(result) == 1
        assert result[0]["id"] == 1001
        assert result[0]["path"] == "foo.py"
        assert result[0]["position"] == 5
        assert "issuecomment-1001" in result[0]["html_url"]

    def test_returns_none_on_failure(self):
        g = Gitea.__new__(Gitea)
        g.get = MagicMock(return_value=None)
        result = g.get_pr_review_comments("owner/repo", 1, 7)
        assert result is None
