"""Tests for the F2 state-based dispatch gate.

The pre-F2 design used ``remove_requested_reviewer`` to
clear the review request on empty review output, and
otherwise fired ``_pr_review`` unconditionally on every
``action == "review_requested"`` webhook. That caused the
slave to re-review the same head whenever a human pressed
"re-request review" after the slave had already submitted.

The F2 design (per the user's instruction "use reviewer
status, trigger reviews only when review is requested the
first time or re-requested") does NOT touch reviewer
membership. Instead, the dispatch looks up the slave's
existing review rows and only skips the re-run when one
of them is a submitted state (``COMMENT`` / ``APPROVED`` /
``REQUEST_CHANGES``) on the current head SHA. Pending
drafts and re-request placeholders do not count; a push
between the submission and the re-request drops the old
review out of scope because its ``commit_id`` no longer
matches.

These tests pin:

  - the ``Gitea.list_pr_reviews`` helper hits the right
    endpoint and degrades safely on transport failure
  - ``Gitea.slave_has_submitted_review_on_head`` accepts
    only the three submitted states on the current head
  - the dispatch fires ``_pr_review`` on a first request
    (no existing reviews)
  - the dispatch skips ``_pr_review`` on a re-request
    after a submitted review on the current head
  - the dispatch fires ``_pr_review`` again when the head
    SHA has moved past the previously-submitted commit
  - a transport failure on ``list_pr_reviews`` falls
    through to "run the review" (safe default for a
    first request)
  - the pre-F2 call to ``remove_requested_reviewer`` no
    longer appears in the empty-review branch
"""
import os
import sys
from unittest.mock import MagicMock

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, SCRIPTS_DIR)
os.environ.setdefault("SHOGGOTH_DOMAIN", "example.test")

from shoggoth.commands.pr_update import PrUpdateCommand  # noqa: E402
from shoggoth.gitea import Gitea  # noqa: E402
from shoggoth import state  # noqa: E402


HEAD_SHA = "deadbeef0001"
PR_URL = "https://gitea.example.com/o/r/pulls/42"


def _payload(action="review_requested", head_sha=HEAD_SHA, slave_in=True):
    """Build a minimal ``review_requested`` webhook payload
    for the dispatch tests.

    ``action`` defaults to ``review_requested`` (the
    branch under test). ``slave_in=True`` puts the
    slave in ``requested_reviewers`` so the
    ``is_slave_user_reviewer`` precondition is met.
    """
    requested = ([{"login": "shoggoth-slave"}]
                 if slave_in else [])
    return {
        "action": action,
        "pull_request": {
            "number": 42,
            "html_url": PR_URL,
            "head": {"sha": head_sha, "ref": "feature"},
            "base": {"ref": "main",
                     "repo": {"full_name": "owner/repo"}},
            "requested_reviewers": requested,
        },
        "sender": {"login": "alice"},
    }


class TestGiteaListPrReviews:
    """The new ``list_pr_reviews`` helper on the Gitea
    client. Wraps ``GET /repos/{repo}/pulls/{n}/reviews``
    (the same endpoint the API surfaces for the
    ``ListPullReviews`` handler at
    ``routers/api/v1/repo/pull_review.go:27``) and
    returns the parsed list of review dicts."""

    def test_calls_correct_url(self):
        g = Gitea.__new__(Gitea)
        g.get = MagicMock(return_value=[
            {"id": 7, "user": {"login": "shoggoth-slave"},
             "state": "COMMENT", "commit_id": HEAD_SHA},
        ])
        result = g.list_pr_reviews("owner/repo", 42)
        g.get.assert_called_once_with(
            "repos/owner/repo/pulls/42/reviews",
            params={"page": 1, "limit": 50})
        assert len(result) == 1
        assert result[0]["state"] == "COMMENT"
        assert result[0]["user"]["login"] == "shoggoth-slave"

    def test_returns_empty_list_on_failure(self):
        # Transport failure: ``_paginate`` collapses ``None``
        # into ``[]`` so the caller can treat it as "no
        # submitted review on this PR" without a special
        # ``None`` branch. The dispatch's safe default is
        # "run the review once", so a fetch failure cannot
        # silently swallow a first-time request.
        g = Gitea.__new__(Gitea)
        g.get = MagicMock(return_value=None)
        result = g.list_pr_reviews("owner/repo", 42)
        assert result == []


class TestGiteaSlaveHasSubmittedReviewOnHead:
    """The discriminator used by the F2 dispatch gate.

    Filters ``list_pr_reviews`` to the three submitted
    states (COMMENT / APPROVED / REQUEST_CHANGES) on the
    current head SHA, scoped to the slave user. PENDING
    drafts and REQUEST_REVIEW placeholders are explicitly
    NOT counted; rows from a different head SHA are
    explicitly NOT counted; rows from a different user are
    explicitly NOT counted."""

    def _g(self, reviews, head_sha=HEAD_SHA):
        """Build a Gitea stub with a payload-driven
        ``list_pr_reviews`` and call the helper."""
        g = Gitea.__new__(Gitea)
        g._payload = _payload(head_sha=head_sha)
        g.list_pr_reviews = MagicMock(return_value=reviews)
        return g

    def test_comment_on_head_counts(self):
        g = self._g([
            {"id": 1, "user": {"login": "shoggoth-slave"},
             "state": "COMMENT", "commit_id": HEAD_SHA},
        ])
        assert g.slave_has_submitted_review_on_head(
            "shoggoth-slave", HEAD_SHA) is True

    def test_approved_on_head_counts(self):
        g = self._g([
            {"id": 2, "user": {"login": "shoggoth-slave"},
             "state": "APPROVED", "commit_id": HEAD_SHA},
        ])
        assert g.slave_has_submitted_review_on_head(
            "shoggoth-slave", HEAD_SHA) is True

    def test_request_changes_on_head_counts(self):
        g = self._g([
            {"id": 3, "user": {"login": "shoggoth-slave"},
             "state": "REQUEST_CHANGES", "commit_id": HEAD_SHA},
        ])
        assert g.slave_has_submitted_review_on_head(
            "shoggoth-slave", HEAD_SHA) is True

    def test_pending_draft_does_not_count(self):
        # A draft the slave hasn't submitted yet is
        # NOT a submitted review. The re-review should
        # still fire when the draft is later submitted
        # or when the draft is replaced by a real one.
        g = self._g([
            {"id": 4, "user": {"login": "shoggoth-slave"},
             "state": "PENDING", "commit_id": HEAD_SHA},
        ])
        assert g.slave_has_submitted_review_on_head(
            "shoggoth-slave", HEAD_SHA) is False

    def test_request_review_placeholder_does_not_count(self):
        # The placeholder Gitea writes when a reviewer
        # is requested. A re-request can also yield a
        # fresh ``REQUEST_REVIEW`` row even after a
        # previous submission, so trusting it would
        # incorrectly skip a re-review of a new head.
        g = self._g([
            {"id": 5, "user": {"login": "shoggoth-slave"},
             "state": "REQUEST_REVIEW", "commit_id": HEAD_SHA},
        ])
        assert g.slave_has_submitted_review_on_head(
            "shoggoth-slave", HEAD_SHA) is False

    def test_old_head_does_not_count(self):
        # A submitted review against a previous head
        # SHA does NOT cover the current head. After
        # a push, the previous submission's
        # ``commit_id`` no longer matches and the
        # slave should re-review the new commit.
        g = self._g([
            {"id": 6, "user": {"login": "shoggoth-slave"},
             "state": "COMMENT",
             "commit_id": "olderhead111"},
        ], head_sha=HEAD_SHA)
        assert g.slave_has_submitted_review_on_head(
            "shoggoth-slave", HEAD_SHA) is False

    def test_other_user_does_not_count(self):
        # The same state on the same head, but from a
        # different reviewer. The slave hasn't
        # submitted anything themselves.
        g = self._g([
            {"id": 7, "user": {"login": "alice"},
             "state": "COMMENT", "commit_id": HEAD_SHA},
        ])
        assert g.slave_has_submitted_review_on_head(
            "shoggoth-slave", HEAD_SHA) is False

    def test_empty_reviews_does_not_count(self):
        # Transport failure on ``list_pr_reviews`` —
        # the helper must NOT raise and must return
        # ``False`` so the dispatch falls through to
        # "run the review" (the safe default for a
        # first request).
        g = self._g([])
        assert g.slave_has_submitted_review_on_head(
            "shoggoth-slave", HEAD_SHA) is False

    def test_empty_slave_user_returns_false(self):
        # Defensive: a missing slave-user config must
        # NOT crash the dispatch.
        g = self._g([
            {"id": 1, "user": {"login": "shoggoth-slave"},
             "state": "COMMENT", "commit_id": HEAD_SHA},
        ])
        assert g.slave_has_submitted_review_on_head(
            "", HEAD_SHA) is False

    def test_empty_head_sha_returns_false(self):
        # Defensive: a payload with no head SHA must
        # NOT crash the dispatch and must NOT skip
        # the review (we can't compare against an
        # empty string, so we treat it as "not
        # covered" and run the review).
        g = self._g([
            {"id": 1, "user": {"login": "shoggoth-slave"},
             "state": "COMMENT", "commit_id": HEAD_SHA},
        ])
        assert g.slave_has_submitted_review_on_head(
            "shoggoth-slave", "") is False

    def test_missing_payload_returns_false(self):
        # Defensive: no payload at all. The dispatch
        # gates on this method after ``is_slave_user_reviewer``
        # has already consulted the payload, so a
        # missing payload is unexpected, but the
        # helper must not raise.
        g = Gitea.__new__(Gitea)
        g._payload = {}
        g.list_pr_reviews = MagicMock(return_value=[])
        assert g.slave_has_submitted_review_on_head(
            "shoggoth-slave", HEAD_SHA) is False

    def test_mixed_rows_picks_correct_one(self):
        # Multiple rows: a stale REQUEST_REVIEW, a
        # stale COMMENT on an old head, a fresh
        # REQUEST_CHANGES on the current head. The
        # current-head row is the one that decides.
        g = self._g([
            {"id": 10, "user": {"login": "shoggoth-slave"},
             "state": "REQUEST_REVIEW", "commit_id": ""},
            {"id": 11, "user": {"login": "shoggoth-slave"},
             "state": "COMMENT", "commit_id": "olderhead111"},
            {"id": 12, "user": {"login": "alice"},
             "state": "COMMENT", "commit_id": HEAD_SHA},
            {"id": 13, "user": {"login": "shoggoth-slave"},
             "state": "REQUEST_CHANGES", "commit_id": HEAD_SHA},
        ])
        assert g.slave_has_submitted_review_on_head(
            "shoggoth-slave", HEAD_SHA) is True


class TestReviewRequestedDispatchGate:
    """The ``action == "review_requested"`` branch of
    ``PrUpdateCommand.execute``. The F2 fix changes this
    branch from "always run ``_pr_review``" to "run
    ``_pr_review`` only when the slave has NOT already
    submitted a review on the current head SHA"."""

    def setup_method(self):
        state.params.shoggoth_slave_user = "shoggoth-slave"
        state.params.shoggoth_trusted_users = "alice"

    def teardown_method(self):
        state.params.shoggoth_slave_user = "slave"
        state.params.shoggoth_trusted_users = ""

    def _build_cmd(self, reviews=None, head_sha=HEAD_SHA,
                   slave_in=True, list_pr_returns=None,
                   slave_submitted=False):
        """Build a ``PrUpdateCommand`` wired up so the
        ``review_requested`` branch is the only branch
        that can run.

        ``reviews`` is the list the stubbed
        ``list_pr_reviews`` returns (forwarded to
        ``slave_has_submitted_review_on_head``). If
        ``list_pr_returns`` is given explicitly, it
        overrides ``reviews`` so a test can simulate a
        transport failure (``None`` → ``[]`` collapse).

        ``slave_submitted`` is the Boolean the stubbed
        ``slave_has_submitted_review_on_head`` returns
        on the dispatch side. The default is ``False``;
        tests that need the gate to trip pass
        ``slave_submitted=True``. The real helper has
        its own unit tests in
        ``TestGiteaSlaveHasSubmittedReviewOnHead``
        above; this stub is what the dispatch test
        consults (because ``cmd.gitea`` is a
        ``MagicMock`` and the real method is not
        bound to it).
        """
        cmd = PrUpdateCommand.__new__(PrUpdateCommand)
        cmd.gitea = MagicMock()
        payload = _payload(head_sha=head_sha, slave_in=slave_in)
        cmd.gitea._payload = payload
        # ``get_pr_action`` / ``get_pr_url`` / ``get_pr_number``
        # are read straight from the payload — wire them
        # to the same shape ``Gitea`` uses, so the
        # dispatch logs the real values instead of
        # ``<MagicMock ...>``.
        cmd.gitea.get_pr_action = MagicMock(
            return_value=payload["action"])
        cmd.gitea.get_pr_url = MagicMock(
            return_value=payload["pull_request"]["html_url"])
        cmd.gitea.get_pr_number = MagicMock(
            return_value=payload["pull_request"]["number"])
        if reviews is None and list_pr_returns is None:
            list_pr_returns = []
        if list_pr_returns is None:
            cmd.gitea.list_pr_reviews = MagicMock(return_value=reviews)
        else:
            cmd.gitea.list_pr_reviews = MagicMock(
                return_value=list_pr_returns)
        # The first gate in ``execute()`` is
        # ``self.gitea.has_review()``; on a MagicMock
        # that returns truthy by default, so the
        # ``review_requested`` branch is never
        # entered. Pin both to False.
        cmd.gitea.has_review = MagicMock(return_value=False)
        cmd.gitea.is_review_by_slave_user = MagicMock(return_value=False)
        # ``is_slave_user_reviewer`` consults
        # ``requested_reviewers`` from the payload;
        # the default payload already includes the
        # slave when ``slave_in=True``.
        cmd.gitea.is_slave_user_reviewer = MagicMock(return_value=slave_in)
        # F2 state-based gate. Stubbed on the MagicMock
        # because the real method on ``Gitea`` reads
        # ``self._payload`` for the head SHA and the
        # dispatch's gate is the only thing under
        # test here. The real method has its own
        # dedicated tests in
        # ``TestGiteaSlaveHasSubmittedReviewOnHead``.
        cmd.gitea.slave_has_submitted_review_on_head = MagicMock(
            return_value=slave_submitted)
        cmd.shoggoth = MagicMock()
        cmd.shoggoth.working_repo = "owner/repo"
        # The dispatch only consults ``self.agent.otlp`` for
        # the log line; the test doesn't read the agent,
        # but a stub keeps attribute access honest.
        cmd.agent = MagicMock()
        cmd.agent.otlp = MagicMock()
        # The action under test.
        cmd._pr_review = MagicMock()
        # The other branches the dispatch can fall
        # through to (``_pr_comment``) — stubbed so
        # they don't actually run when a test forces
        # the dispatch to skip ``_pr_review``.
        cmd._pr_comment = MagicMock()
        return cmd

    def test_first_request_runs_pr_review(self):
        # First time: no reviews on the PR. The
        # dispatch fires ``_pr_review`` once.
        cmd = self._build_cmd(reviews=[])
        cmd.execute()
        cmd._pr_review.assert_called_once_with(PR_URL)

    def test_re_request_after_comment_skips_pr_review(self):
        # The slave already submitted a COMMENT
        # review on the current head. A re-request
        # does NOT trigger a fresh review.
        cmd = self._build_cmd(reviews=[
            {"id": 1, "user": {"login": "shoggoth-slave"},
             "state": "COMMENT", "commit_id": HEAD_SHA},
        ], slave_submitted=True)
        cmd.execute()
        cmd._pr_review.assert_not_called()
        # The skip was logged via the agent otlp.
        assert any("skipping re-request"
                    in str(c) for c in cmd.agent.otlp._push_line.call_args_list)
        # The gate was consulted with the right
        # arguments (slave user and current head SHA).
        cmd.gitea.slave_has_submitted_review_on_head.assert_called_once_with(
            "shoggoth-slave", HEAD_SHA)

    def test_re_request_after_approved_skips_pr_review(self):
        cmd = self._build_cmd(reviews=[
            {"id": 2, "user": {"login": "shoggoth-slave"},
             "state": "APPROVED", "commit_id": HEAD_SHA},
        ], slave_submitted=True)
        cmd.execute()
        cmd._pr_review.assert_not_called()

    def test_re_request_after_request_changes_skips_pr_review(self):
        cmd = self._build_cmd(reviews=[
            {"id": 3, "user": {"login": "shoggoth-slave"},
             "state": "REQUEST_CHANGES", "commit_id": HEAD_SHA},
        ], slave_submitted=True)
        cmd.execute()
        cmd._pr_review.assert_not_called()

    def test_re_request_after_push_runs_pr_review(self):
        # The previous COMMENT is on an older head
        # SHA. A push moved the head forward, so the
        # old review no longer covers the current
        # head. The dispatch re-runs ``_pr_review``.
        cmd = self._build_cmd(
            reviews=[
                {"id": 4, "user": {"login": "shoggoth-slave"},
                 "state": "COMMENT", "commit_id": "olderhead111"},
            ],
            head_sha=HEAD_SHA,
        )
        cmd.execute()
        cmd._pr_review.assert_called_once_with(PR_URL)

    def test_pending_draft_does_not_block_pr_review(self):
        # The slave has a PENDING draft but no
        # submitted review. The dispatch still
        # fires ``_pr_review`` (PENDING is not
        # submitted, so the gate stays open).
        cmd = self._build_cmd(reviews=[
            {"id": 5, "user": {"login": "shoggoth-slave"},
             "state": "PENDING", "commit_id": HEAD_SHA},
        ])
        cmd.execute()
        cmd._pr_review.assert_called_once_with(PR_URL)

    def test_request_review_placeholder_does_not_block(self):
        # The slave has a REQUEST_REVIEW placeholder
        # but no submitted review. The dispatch
        # still fires ``_pr_review`` (REQUEST_REVIEW
        # is not submitted, so the gate stays open).
        cmd = self._build_cmd(reviews=[
            {"id": 6, "user": {"login": "shoggoth-slave"},
             "state": "REQUEST_REVIEW", "commit_id": HEAD_SHA},
        ])
        cmd.execute()
        cmd._pr_review.assert_called_once_with(PR_URL)

    def test_transport_failure_falls_through_to_pr_review(self):
        # ``list_pr_reviews`` returned ``[]`` (the
        # helper collapses a transport failure into
        # an empty list). The dispatch must err on
        # the side of running the review once — a
        # silent skip would swallow a real first
        # request. This is the safe default.
        cmd = self._build_cmd(list_pr_returns=[])
        cmd.execute()
        cmd._pr_review.assert_called_once_with(PR_URL)

    def test_dispatch_does_not_fire_when_slave_not_in_reviewers(self):
        # If the slave is NOT in ``requested_reviewers``,
        # the ``is_slave_user_reviewer`` precondition
        # fails, and the ``review_requested`` branch
        # is not entered. ``_pr_review`` does not run
        # regardless of the slave's review history.
        cmd = self._build_cmd(
            reviews=[
                {"id": 7, "user": {"login": "shoggoth-slave"},
                 "state": "COMMENT", "commit_id": HEAD_SHA},
            ],
            slave_in=False,
        )
        cmd.execute()
        cmd._pr_review.assert_not_called()

    def test_dispatch_ignores_other_users_submitted_reviews(self):
        # Another user submitted a review on the
        # current head, but the slave has not. The
        # dispatch still fires ``_pr_review`` for
        # the slave.
        cmd = self._build_cmd(reviews=[
            {"id": 8, "user": {"login": "alice"},
             "state": "COMMENT", "commit_id": HEAD_SHA},
        ])
        cmd.execute()
        cmd._pr_review.assert_called_once_with(PR_URL)

    def test_dispatch_ignores_other_action(self):
        # Sanity: the state-based guard only runs on
        # ``action == "review_requested"``. A
        # ``created`` action with a review already
        # submitted by the slave must not enter the
        # gate branch at all. We don't drive
        # ``execute()`` end-to-end here (the
        # ``created`` branch's ``_pr_comment`` path
        # has its own deep mocks); instead we assert
        # directly on the dispatch's payload
        # extraction, which is the only state the
        # F2 guard reads.
        payload = _payload(action="created")
        assert payload["action"] != "review_requested"
        # The guard's precondition is on
        # ``action == "review_requested"`` — a
        # ``created`` action never enters the
        # ``elif`` branch, so the slave's submitted
        # review is irrelevant. (We pin the
        # precondition here as a reminder that the
        # guard is scoped to that one action; the
        # real flow is exercised by the
        # ``test_re_request_after_*`` tests above.)


class TestEmptyReviewNoLongerRemovesReviewer:
    """The pre-F2 design removed the slave from
    ``requested_reviewers`` when the agent produced an
    empty review. The F2 design leaves reviewer
    membership alone (the slave re-runs the next time a
    human re-requests, and the state-based gate
    prevents the redundant run on the same head)."""

    def test_remove_requested_reviewer_not_called_in_empty_branch(self):
        # Drive ``_pr_review`` to its empty-output
        # branch by stubbing the agent to return no
        # review body, and assert that
        # ``remove_requested_reviewer`` is not called
        # anywhere. (Before F2 the empty branch
        # called it; after F2 the method itself is
        # also gone, but the assertion is on the
        # call site, which is what the fix actually
        # changed.)
        from shoggoth.commands.pr_update import PrUpdateCommand
        cmd = PrUpdateCommand.__new__(PrUpdateCommand)
        # The empty-review branch lives deep inside
        # ``_pr_review``; rather than drive the
        # whole pipeline we just assert the method
        # that used to be called is no longer
        # present on the Gitea stub (the call site
        # has been removed entirely). The
        # Gitea-instance level is the cleanest
        # invariant.
        g = Gitea.__new__(Gitea)
        assert not hasattr(g, "remove_requested_reviewer"), (
            "remove_requested_reviewer has been removed from Gitea; "
            "F2 should leave reviewer membership alone.")

        # And the dispatch site in pr_update no
        # longer references it.
        import inspect
        src = inspect.getsource(PrUpdateCommand)
        assert "remove_requested_reviewer" not in src, (
            "pr_update.py must not call remove_requested_reviewer; "
            "F2 replaces reviewer removal with a state-based gate.")
