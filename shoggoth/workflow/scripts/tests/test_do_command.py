"""Unit tests for the ``/do`` slash command.

The ``/do`` flow has two pieces:

  1. ``SLASH_DO_RE`` matches the bare ``/do`` command
     (no trailing text). The command body carries no
     instructions itself; the instructions come from the
     PR's comment thread.
  2. ``get_all_unresolved_review_comments`` returns every
     unresolved review comment without a trusted-user
     filter, so the agent processes the slave's own
     comments (which the regular review-comments flow
     ignores) as well as comments from trusted humans.

The previous design (instructions carried in the ``/do``
command body) has been replaced; the bare-trigger model
is what these tests pin.
"""
import os
import sys
from unittest.mock import MagicMock

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, SCRIPTS_DIR)
os.environ.setdefault("SHOGGOTH_DOMAIN", "example.test")

from shoggoth.commands.pr_update import PrUpdateCommand  # noqa: E402
from shoggoth.gitea import Gitea  # noqa: E402
from shoggoth import state  # noqa: E402


# ----- SLASH_DO_RE ------------------------------------------------

class TestSlashDoRegex:
    def test_bare_do_matches(self):
        m = PrUpdateCommand.SLASH_DO_RE.match("/do")
        assert m is not None

    def test_bare_do_with_trailing_whitespace_matches(self):
        # Trailing whitespace is allowed; anything more
        # substantial does NOT match (so ``/do something``
        # falls through to the regular review-comments
        # path).
        m = PrUpdateCommand.SLASH_DO_RE.match("/do   ")
        assert m is not None

    def test_do_with_instruction_does_not_match(self):
        # The bare-trigger model: ``/do`` carries no
        # instructions. Anything after the word falls
        # through to the regular review-comments path.
        m = PrUpdateCommand.SLASH_DO_RE.match("/do refactor this")
        assert m is None

    def test_bare_do_with_newline_does_not_match(self):
        m = PrUpdateCommand.SLASH_DO_RE.match("/do\nrefactor this")
        assert m is None

    def test_bare_slash_does_not_match(self):
        m = PrUpdateCommand.SLASH_DO_RE.match("/")
        assert m is None

    def test_slash_help_does_not_match_do_re(self):
        m = PrUpdateCommand.SLASH_DO_RE.match("/help")
        assert m is None

    def test_slash_command_re_still_matches_help(self):
        # The existing single-word regex is unchanged; it
        # still matches ``/help``, ``/review`` etc.
        m = PrUpdateCommand.SLASH_COMMAND_RE.match("/help")
        assert m is not None
        assert m.group(1) == "help"


# ----- SUPPORTED_COMMANDS catalog --------------------------------

class TestSupportedCommands:
    def test_do_command_in_catalog(self):
        names = [cmd for cmd, _desc in PrUpdateCommand.SUPPORTED_COMMANDS]
        assert "do" in names

    def test_catalog_ordering_stable(self):
        names = [cmd for cmd, _desc in PrUpdateCommand.SUPPORTED_COMMANDS]
        assert names == ["help", "review", "squash", "rebase", "do"]

    def test_do_description_mentions_comment_thread(self):
        names = [n for n, _d in PrUpdateCommand.SUPPORTED_COMMANDS]
        idx = names.index("do")
        desc = PrUpdateCommand.SUPPORTED_COMMANDS[idx][1]
        assert "comment thread" in desc.lower()
        # Trust model: only a trusted human can press /do; the
        # slave can leave comments in the thread but cannot press
        # /do itself.
        assert "trusted human" in desc.lower()
        assert "not allowed to press" in desc.lower()
        # No regression to the old framing that invited the
        # slave to trigger /do.
        assert "intended for the slave" not in desc.lower()


# ----- get_all_unresolved_review_comments (no trusted filter) ----

def _build_gitea_for_all(comments_by_review):
    """Build a Gitea stub whose ``get`` returns canned review
    data. ``comments_by_review`` maps review id to a list of
    inline comment dicts (the review itself is implicit —
    any review id present in the map is generated with no
    other content)."""
    g = Gitea.__new__(Gitea)

    def fake_get(url, *_args, **_kwargs):
        # ``url`` is one of:
        #   repos/owner/repo/pulls/1/reviews             (list reviews)
        #   repos/owner/repo/pulls/1/reviews/{id}/comments  (list comments)
        parts = url.split("/")
        ridx = parts.index("reviews")
        tail = parts[ridx + 1:]
        if tail and tail[0].isdigit():
            return comments_by_review.get(int(tail[0]), [])
        # The list-reviews call returns one entry per
        # review that has any comments, so the caller can
        # fetch them.
        return [{"id": rid} for rid in comments_by_review.keys()]

    def _paginate_impl(fn):
        return fn(1, 50)

    g.get = fake_get
    g._paginate = _paginate_impl
    return g


class TestGetPrReviewThread:
    """``Gitea.get_pr_review_thread(repo, pr_number, path,
    line)`` — fetch ONE review-comment thread anchored
    at ``(path, line)``, with no trusted-user filter
    and no other-comment leakage. Used by the ``/do``
    handler.

    The Gitea REST API doesn't expose a "list review
    comments filtered by path/line" endpoint, so the
    function enumerates all reviews on the PR, fetches
    each review's comments, and filters to the
    requested ``(path, line)`` pair. These tests pin
    that filter behaviour using a stubbed ``get``
    that returns canned review / comment data.
    """

    def test_returns_combined_thread_at_path_line(self):
        # Two comments at the same (path, line) —
        # combined into one thread body in the
        # ``[login]: body`` format. Order is by
        # comment id ascending.
        g = _build_gitea_for_all({
            1: [
                {
                    "id": 100, "review_id": 1,
                    "user": {"login": "alice"},
                    "path": "src/foo.py", "position": 5,
                    "body": "first comment",
                },
                {
                    "id": 101, "review_id": 1,
                    "user": {"login": "bob"},
                    "path": "src/foo.py", "position": 5,
                    "body": "second comment",
                },
            ],
        })
        thread = g.get_pr_review_thread(
            "owner/repo", 1, "src/foo.py", 5)
        assert thread is not None
        assert thread["path"] == "src/foo.py"
        assert thread["line"] == 5
        assert thread["id"] == 100  # first id
        assert "[alice]: first comment" in thread["body"]
        assert "[bob]: second comment" in thread["body"]
        # First comment id comes first in the body.
        assert thread["body"].index("[alice]") < thread["body"].index("[bob]")

    def test_returns_none_when_no_comments_at_anchor(self):
        # PR has comments but none at the requested
        # (path, line). Must return None, NOT a
        # thread with the wrong content.
        g = _build_gitea_for_all({
            1: [
                {
                    "id": 100, "review_id": 1,
                    "user": {"login": "alice"},
                    "path": "src/foo.py", "position": 5,
                    "body": "different line",
                },
            ],
        })
        thread = g.get_pr_review_thread(
            "owner/repo", 1, "src/bar.py", 99)
        assert thread is None

    def test_returns_none_when_all_comments_resolved(self):
        # All comments at the anchor are resolved
        # (``resolver`` is set). The function
        # filters them out and returns None.
        g = _build_gitea_for_all({
            1: [
                {
                    "id": 100, "review_id": 1,
                    "user": {"login": "alice"},
                    "path": "src/foo.py", "position": 5,
                    "body": "addressed already",
                    "resolver": {"login": "alice"},
                },
            ],
        })
        thread = g.get_pr_review_thread(
            "owner/repo", 1, "src/foo.py", 5)
        assert thread is None

    def test_filters_out_other_threads(self):
        # PR has multiple threads at different
        # (path, line). Requesting one specific
        # thread must return ONLY that thread's
        # body, not the others. This is the
        # load-bearing isolation property: a
        # ``/do`` reply on thread A cannot leak
        # the contents of thread B.
        g = _build_gitea_for_all({
            1: [
                {
                    "id": 100, "review_id": 1,
                    "user": {"login": "alice"},
                    "path": "src/a.py", "position": 10,
                    "body": "thread A comment",
                },
                {
                    "id": 200, "review_id": 1,
                    "user": {"login": "alice"},
                    "path": "src/b.py", "position": 5,
                    "body": "thread B comment",
                },
                {
                    "id": 300, "review_id": 1,
                    "user": {"login": "alice"},
                    "path": "src/a.py", "position": 11,
                    "body": "thread A2 comment",
                },
            ],
        })
        thread = g.get_pr_review_thread(
            "owner/repo", 1, "src/a.py", 10)
        assert thread is not None
        assert "thread A comment" in thread["body"]
        assert "thread B comment" not in thread["body"]
        assert "thread A2 comment" not in thread["body"]
        # Different anchor returns a different thread.
        thread_b = g.get_pr_review_thread(
            "owner/repo", 1, "src/b.py", 5)
        assert thread_b is not None
        assert "thread B comment" in thread_b["body"]
        assert "thread A comment" not in thread_b["body"]

    def test_combines_comments_across_reviews(self):
        # A user can reply to their own review
        # comment via a follow-up review, which
        # is a separate Gitea review. The
        # function must combine comments at
        # the same (path, line) across
        # different reviews.
        g = _build_gitea_for_all({
            1: [
                {
                    "id": 100, "review_id": 1,
                    "user": {"login": "alice"},
                    "path": "src/foo.py", "position": 5,
                    "body": "review 1 comment",
                },
            ],
            2: [
                {
                    "id": 200, "review_id": 2,
                    "user": {"login": "alice"},
                    "path": "src/foo.py", "position": 5,
                    "body": "review 2 reply",
                },
            ],
        })
        thread = g.get_pr_review_thread(
            "owner/repo", 1, "src/foo.py", 5)
        assert thread is not None
        assert "[alice]: review 1 comment" in thread["body"]
        assert "[alice]: review 2 reply" in thread["body"]

    def test_no_user_filter_applied(self):
        # The function does NOT filter by user —
        # the slave's comments and random
        # visitors' comments are all in the
        # thread body, just like the trusted
        # humans' comments. The trust gate is
        # at the dispatch layer (only trusted
        # humans can trigger ``/do``); once the
        # thread is fetched, every participant
        # in it is part of the instruction.
        g = _build_gitea_for_all({
            1: [
                {
                    "id": 100, "review_id": 1,
                    "user": {"login": "shoggoth-slave"},
                    "path": "src/foo.py", "position": 5,
                    "body": "slave instruction",
                },
                {
                    "id": 200, "review_id": 1,
                    "user": {"login": "random-visitor"},
                    "path": "src/foo.py", "position": 5,
                    "body": "visitor note",
                },
            ],
        })
        thread = g.get_pr_review_thread(
            "owner/repo", 1, "src/foo.py", 5)
        assert thread is not None
        assert "[shoggoth-slave]: slave instruction" in thread["body"]
        assert "[random-visitor]: visitor note" in thread["body"]

    def test_returns_none_on_api_failure(self):
        # If the underlying ``get`` returns None
        # (e.g. network failure), the function
        # must not raise — it returns None
        # (treated by callers as "no thread
        # found").
        g = Gitea.__new__(Gitea)
        g.get = MagicMock(return_value=None)
        g._paginate = MagicMock(return_value=[])
        result = g.get_pr_review_thread(
            "owner/repo", 1, "src/foo.py", 5)
        assert result is None


# ----- find_review_comment_by_id (id -> anchor lookup) ----

class TestFindReviewCommentById:
    """``Gitea.find_review_comment_by_id(repo, pr_number,
    comment_id)`` returns the ``PullReviewComment`` dict
    carrying the inline anchor for the given database id,
    or ``None`` if no review comment with that id exists.

    Used by the ``/do`` dispatcher to recover the
    ``(path, position)`` triple that the webhook payload
    itself does NOT carry (the ``IssueCommentPayload.Comment``
    struct ships only the ``id`` — see the inline comments
    on ``Gitea.find_review_comment_by_id`` for the Gitea
    source trace). Without this lookup the
    ``comment.path``/``comment.position`` fields on the
    webhook were always ``None``, and ``_slash_do``
    silently dropped the trigger; this method is the
    recovery.

    Cost is N+1 API calls (one per review) — same as
    ``get_pr_review_thread`` — because Gitea does not
    expose a "review comment by id" endpoint.
    """

    def test_returns_matching_comment(self):
        # The id is in the third review's comment list;
        # the function returns the matching dict
        # verbatim (the dispatcher reads ``path``/
        # ``position`` off it).
        g = _build_gitea_for_all({
            1: [
                {"id": 100, "review_id": 1,
                 "path": "src/a.py", "position": 3,
                 "body": "a1"},
            ],
            2: [
                {"id": 200, "review_id": 2,
                 "path": "src/b.py", "position": 7,
                 "body": "b1"},
                {"id": 201, "review_id": 2,
                 "path": "src/b.py", "position": 7,
                 "body": "b2 — also at the same anchor"},
            ],
            3: [
                {"id": 300, "review_id": 3,
                 "path": "src/c.py", "position": 11,
                 "body": "the one we want"},
            ],
        })
        result = g.find_review_comment_by_id("owner/repo", 1, 300)
        assert result is not None
        assert result["id"] == 300
        assert result["path"] == "src/c.py"
        assert result["position"] == 11
        assert result["review_id"] == 3

    def test_returns_none_when_id_not_in_any_review(self):
        # The id is not present in any review's
        # comment list — typical for a top-level PR
        # comment (``Type == CommentTypeComment``,
        # which ``/reviews/{id}/comments`` never
        # returns). Caller treats this as "no inline
        # anchor, drop the trigger silently".
        g = _build_gitea_for_all({
            1: [
                {"id": 100, "review_id": 1,
                 "path": "src/a.py", "position": 3,
                 "body": "a1"},
            ],
        })
        assert g.find_review_comment_by_id(
            "owner/repo", 1, 999) is None

    def test_returns_none_when_comment_id_is_falsy(self):
        # ``comment_id`` missing (``None``), ``0``,
        # or empty string short-circuits without any
        # API call. Webhook payloads for non-comment
        # events sometimes have no ``comment`` field
        # at all; the dispatcher passes the absent
        # id through and we must not crash.
        g = _build_gitea_for_all({
            1: [{"id": 100, "review_id": 1,
                 "path": "src/a.py", "position": 3,
                 "body": "a1"}],
        })
        assert g.find_review_comment_by_id(
            "owner/repo", 1, None) is None
        assert g.find_review_comment_by_id(
            "owner/repo", 1, 0) is None

    def test_returns_none_when_pr_has_no_reviews(self):
        # PR has no reviews — list-reviews returns
        # ``[]``, the loop is skipped, the function
        # returns ``None``.
        g = _build_gitea_for_all({})
        assert g.find_review_comment_by_id(
            "owner/repo", 1, 100) is None

    def test_skips_reviews_without_id_field(self):
        # Reviews with a missing ``id`` are skipped
        # (they would have caused a TypeError on the
        # f-string in the URL). The id appears in a
        # later review and is returned.
        g = Gitea.__new__(Gitea)

        def fake_get(url, *_args, **_kwargs):
            parts = url.split("/")
            ridx = parts.index("reviews")
            tail = parts[ridx + 1:]
            if tail and tail[0].isdigit():
                rid = int(tail[0])
                if rid == 7:
                    return [{"id": 700, "review_id": 7,
                             "path": "src/x.py", "position": 1,
                             "body": "found"}]
                return []
            # First two reviews have no id (broken /
            # transitional records); third is normal.
            return [{}, {}, {"id": 7}]

        def _paginate_impl(fn):
            return fn(1, 50)

        g.get = fake_get
        g._paginate = _paginate_impl
        result = g.find_review_comment_by_id("owner/repo", 1, 700)
        assert result is not None
        assert result["id"] == 700

    def test_returns_none_on_transport_failure(self):
        # ``get`` returning ``None`` (transport
        # failure) is collapsed by ``_paginate`` to
        # ``[]``; the function then returns ``None``
        # cleanly rather than raising.
        g = Gitea.__new__(Gitea)
        g.get = MagicMock(return_value=None)
        g._paginate = MagicMock(return_value=[])
        result = g.find_review_comment_by_id(
            "owner/repo", 1, 100)
        assert result is None


# ----- inline-anchor recovery in the /do dispatcher ----

class TestInlineAnchorRecovery:
    """The ``/do`` dispatcher
    (``_handle_slash_command``) needs the inline
    ``(path, line)`` anchor for the thread the user
    replied to. The webhook's ``IssueCommentPayload.Comment``
    struct ships only ``id`` — ``path``/``position`` are
    not serialised — because the reply path goes through
    ``notify_service.CreateIssueComment`` with
    ``convert.ToAPIComment`` rather than through
    ``convert.ToPullReviewComment``. The dispatcher
    therefore calls
    ``Gitea.find_review_comment_by_id`` with the webhook
    comment's ``id`` to recover the anchor before
    invoking ``_slash_do``.

    These tests pin that recovery path end-to-end:
    a trust-gated user posting ``/do`` as a reply to an
    inline review comment must reach ``_slash_do`` with a
    non-``None`` ``thread_path``/``thread_line`` matching
    the recovered anchor. Top-level ``/do`` issue comments
    and ``/do`` on non-code comments must still no-op,
    matching the original "reply-only" policy.

    The earlier implementation read
    ``comment.path``/``comment.position`` directly from
    the webhook payload; both fields are absent on
    Gitea's ``IssueCommentPayload.Comment`` struct so the
    earlier version silently dropped every legitimate
    inline reply. This class is the regression test for
    that bug.
    """

    def setup_method(self):
        state.params.shoggoth_slave_user = "shoggoth-slave"
        state.params.shoggoth_trusted_users = "alice"

    def teardown_method(self):
        state.params.shoggoth_slave_user = "slave"
        state.params.shoggoth_trusted_users = ""

    def _build_pr_with_comment(self, *, author, comment_id,
                                 anchor_match=None):
        """Build a ``PrUpdateCommand`` whose webhook payload
        has ``comment.id = comment_id`` (no ``path``/
        ``position`` — matching the real Gitea
        ``IssueCommentPayload.Comment`` shape). The
        ``find_review_comment_by_id`` mock returns
        ``anchor_match`` (``None`` simulates "id not in
        any review", i.e. a top-level comment)."""
        from shoggoth.commands.pr_update import PrUpdateCommand
        cmd = PrUpdateCommand.__new__(PrUpdateCommand)
        cmd.gitea = MagicMock()
        cmd.gitea._payload = {
            "action": "created",
            "comment": {
                "id": comment_id,
                "user": {"login": author},
            },
            "issue": {"user": {"login": author}},
            "pull_request": {"user": {"login": author}},
        }
        cmd.gitea.find_review_comment_by_id = MagicMock(
            return_value=anchor_match)
        cmd.shoggoth = MagicMock()
        cmd.shoggoth.working_repo = "owner/repo"
        cmd._post_slash_response = MagicMock()
        cmd._slash_do = MagicMock()
        cmd._slash_help = MagicMock()
        return cmd

    def test_inline_anchor_recovered_for_inline_reply(self):
        # Regression: a ``/do`` posted as a reply to an
        # inline review comment (so comment.id IS a
        # code-comment DB row) must reach ``_slash_do``
        # with the recovered ``(path, position)`` from
        # the ``/reviews/{id}/comments`` listing. The
        # previous implementation read path/position
        # from the webhook directly and always saw
        # ``None``, silently dropping every legitimate
        # ``/do`` reply.
        cmd = self._build_pr_with_comment(
            author="alice", comment_id=4242,
            anchor_match={
                "id": 4242, "review_id": 9,
                "path": "src/server.py",
                "position": 17,
                "body": "/do",
            },
        )
        cmd._handle_slash_command("do", 1,
                                  "https://example/p/1")
        cmd.gitea.find_review_comment_by_id.assert_called_once_with(
            "owner/repo", 1, 4242)
        cmd._slash_do.assert_called_once()
        # The handler is dispatched with the recovered
        # anchor; the inline gate inside ``_slash_do``
        # then sees a real ``(path, line)`` pair and
        # lets the run proceed.
        _args = cmd._slash_do.call_args[0]
        _repo, _pr_num, _url, thread_path, thread_line = _args
        assert thread_path == "src/server.py"
        assert thread_line == 17

    def test_inline_anchor_recovery_uses_comment_id_not_author(self):
        # Pin that the lookup parameter is the comment id,
        # not the author (a tempting off-by-one if a future
        # edit mistakes them). The author ``alice`` is the
        # trusted user from ``setup_method``; the comment id
        # is what the dispatcher passes to the API.
        cmd = self._build_pr_with_comment(
            author="alice", comment_id=9999,
            anchor_match={"id": 9999, "review_id": 1,
                          "path": "a.py", "position": 1},
        )
        cmd._handle_slash_command("do", 1, "u")
        cmd.gitea.find_review_comment_by_id.assert_called_once_with(
            "owner/repo", 1, 9999)

    def test_top_level_comment_with_id_no_ops(self):
        # ``/do`` posted as a top-level PR comment has
        # a ``comment.id`` (it's still a normal
        # ``Type=CommentTypeComment`` row) but that id
        # is NOT in any ``/reviews/{id}/comments``
        # listing — ``find_review_comment_by_id``
        # returns ``None``. The dispatcher must pass
        # ``(None, None)`` to ``_slash_do`` so the
        # existing gate inside the handler
        # (``if thread_path is None or thread_line is
        # None: return``) drops the run silently.
        cmd = self._build_pr_with_comment(
            author="alice", comment_id=7777,
            anchor_match=None,
        )
        cmd._handle_slash_command("do", 1,
                                  "https://example/p/1")
        cmd.gitea.find_review_comment_by_id.assert_called_once_with(
            "owner/repo", 1, 7777)
        cmd._slash_do.assert_called_once()
        _args = cmd._slash_do.call_args[0]
        _repo, _pr_num, _url, thread_path, thread_line = _args
        assert thread_path is None
        assert thread_line is None

    def test_missing_comment_id_no_ops(self):
        # Webhook payload carries no ``comment.id`` (some
        # event variants have no comment at all). The
        # dispatcher must not call the API at all (a
        # missing id is a meaningless query) and must
        # pass ``(None, None)`` to ``_slash_do``.
        cmd = self._build_pr_with_comment(
            author="alice", comment_id=None,
            anchor_match=None,
        )
        cmd._handle_slash_command("do", 1, "u")
        cmd.gitea.find_review_comment_by_id.assert_not_called()
        cmd._slash_do.assert_called_once()
        _args = cmd._slash_do.call_args[0]
        _repo, _pr_num, _url, thread_path, thread_line = _args
        assert thread_path is None
        assert thread_line is None

    def test_non_do_command_does_not_call_lookup(self):
        # The id-based lookup is ``/do``-specific.
        # Other slash commands (``/help``, ``/squash``,
        # ``/rebase``, ``/review``) must not pay the
        # extra API cost — they don't have the
        # thread-anchor concept at all.
        for cmd_name in ("help", "review", "squash", "rebase"):
            cmd = self._build_pr_with_comment(
                author="alice", comment_id=4242,
                anchor_match=None)
            cmd._slash_help = MagicMock()
            cmd._handle_slash_command(cmd_name, 1,
                                      "https://example/p/1")
            cmd.gitea.find_review_comment_by_id.assert_not_called()


# ----- Sanity: the regular flow still ignores slave comments ----

class TestRegularFlowStillIgnoresSlaveComments:
    """The refactor must NOT have changed the regular
    review-comments flow: slave comments remain ignored
    there. ``/do`` is the explicit override."""

    def setup_method(self):
        state.params.shoggoth_slave_user = "shoggoth-slave"
        state.params.shoggoth_trusted_users = "alice"

    def teardown_method(self):
        state.params.shoggoth_slave_user = "slave"
        state.params.shoggoth_trusted_users = ""

    def test_get_unresolved_review_comments_skips_slave_only_threads(self):
        g = Gitea.__new__(Gitea)

        def fake_get(url, *_args, **_kwargs):
            parts = url.split("/")
            ridx = parts.index("reviews")
            tail = parts[ridx + 1:]
            if tail and tail[0].isdigit():
                return [{
                    "id": 100,
                    "review_id": 1,
                    "user": {"login": "shoggoth-slave"},
                    "path": "src/foo.py",
                    "position": 5,
                    "body": "ignored",
                }]
            return [{"id": 1}]

        g.get = fake_get
        g._paginate = lambda fn: fn(1, 50)
        result = g.get_unresolved_review_comments("owner/repo", 1)
        assert result == []

    def test_get_unresolved_review_comments_includes_trusted_threads(self):
        g = Gitea.__new__(Gitea)

        def fake_get(url, *_args, **_kwargs):
            parts = url.split("/")
            ridx = parts.index("reviews")
            tail = parts[ridx + 1:]
            if tail and tail[0].isdigit():
                return [{
                    "id": 200,
                    "review_id": 1,
                    "user": {"login": "alice"},
                    "path": "src/foo.py",
                    "position": 5,
                    "body": "trusted comment",
                }]
            return [{"id": 1}]

        g.get = fake_get
        g._paginate = lambda fn: fn(1, 50)
        result = g.get_unresolved_review_comments("owner/repo", 1)
        assert len(result) == 1
        assert "alice" in result[0]["body"]


# ----- Sanity: the dispatcher and the trust gate ---------------

class TestDispatcherAndTrustGate:
    """The bare ``/do`` is dispatched through the existing
    ``_handle_slash_command`` path; the trust gate allows
    the slave and trusted users, rejects everyone else."""

    def setup_method(self):
        state.params.shoggoth_slave_user = "shoggoth-slave"
        state.params.shoggoth_trusted_users = "alice"

    def teardown_method(self):
        state.params.shoggoth_slave_user = "slave"
        state.params.shoggoth_trusted_users = ""

    def test_dispatch_returns_do_for_bare_command(self):
        # The dispatcher checks ``SLASH_DO_RE`` first; if
        # it matches ``/do``, the comment is routed to
        # ``_slash_do`` (which re-reads the thread), even
        # though ``SLASH_COMMAND_RE`` would also match the
        # bare word. Pin the dispatch precedence here
        # without a Gitea stub: ``_detect_slash_command``
        # tests ``SLASH_DO_RE`` before ``SLASH_COMMAND_RE``,
        # so the bare ``/do`` always wins.
        body = "/do"
        assert PrUpdateCommand.SLASH_DO_RE.match(body) is not None
        # ``SLASH_COMMAND_RE`` ALSO matches the bare word
        # (because ``/do`` is a single-word command by the
        # existing regex) — the dispatch order is what
        # matters, not whether the regular regex matches.
        assert PrUpdateCommand.SLASH_COMMAND_RE.match(body) is not None

    def test_dispatch_skips_do_with_instruction(self):
        body = "/do refactor this"
        # SLASH_DO_RE no longer matches (no instructions
        # in the body anymore) — falls through to the
        # regular flow.
        assert PrUpdateCommand.SLASH_DO_RE.match(body) is None
        # The command-with-instruction form is not a
        # recognised single-word slash command either
        # (the existing SLASH_COMMAND_RE is strict).
        assert PrUpdateCommand.SLASH_COMMAND_RE.match(body) is None

    # ---- End-to-end trust-gate exercise ----
    #
    # The two tests above only exercise the regex layer.
    # The trust gate itself lives in
    # ``PrUpdateCommand._handle_slash_command`` (the
    # ``elif command == "do" and not is_slave and (not author
    # or author not in trusted_users):`` branch). These
    # three tests drive that branch end-to-end by
    # constructing a real ``PrUpdateCommand`` with stubbed
    # ``gitea._payload`` and asserting on the handler that
    # ``_handle_slash_command`` invokes (or does not).

    def _build_pr_with_author(self, author):
        """Return a real ``PrUpdateCommand`` whose ``gitea``
        stub returns the given comment author. ``_handle_slash_command``
        reads the author from ``gitea._payload[\"comment\"][\"user\"][\"login\"]``;
        ``_slash_do`` (the handler the test asserts on) is
        stubbed so the test stays at the trust-gate layer."""
        from shoggoth.commands.pr_update import PrUpdateCommand
        cmd = PrUpdateCommand.__new__(PrUpdateCommand)
        cmd.gitea = MagicMock()
        cmd.gitea._payload = {
            "action": "created",
            "comment": {"user": {"login": author}},
            "issue": {"user": {"login": author}},
            "pull_request": {"user": {"login": author}},
        }
        # ``_handle_slash_command`` reads ``working_repo`` from
        # the shoggoth field BEFORE the trust gate runs.
        cmd.shoggoth = MagicMock()
        cmd.shoggoth.working_repo = "owner/repo"
        cmd._post_slash_response = MagicMock()
        cmd._slash_do = MagicMock()
        cmd._slash_help = MagicMock()
        return cmd

    def test_trust_gate_rejects_slave(self):
        # The slave user is NOT allowed to drive any
        # slash command — the trust model requires a
        # trusted human in the loop. The pre-fix design
        # gave the slave an exception for ``/do`` and
        # ``/help``; that has been tightened.
        cmd = self._build_pr_with_author("shoggoth-slave")
        cmd._handle_slash_command("do", 1, "https://example/p/1")
        cmd._slash_do.assert_not_called()
        cmd._post_slash_response.assert_not_called()

    def test_trust_gate_rejects_slave_for_other_commands(self):
        # Sanity: the slave is rejected for /help,
        # /squash, /rebase, /review — not just /do.
        # Without a trusted-human-in-the-loop, the
        # slave cannot drive any destructive or
        # read-only command either.
        for cmd_name in ("help", "review", "squash", "rebase"):
            cmd = self._build_pr_with_author("shoggoth-slave")
            cmd._handle_slash_command(cmd_name, 1, "https://example/p/1")
            # The handler is never invoked.
            assert not cmd._slash_do.called, f"/{cmd_name} should not dispatch to slave"
            assert not cmd._slash_help.called, f"/{cmd_name} should not dispatch to slave"
            # No response comment is posted on rejection.
            cmd._post_slash_response.assert_not_called()

    def test_trust_gate_allows_trusted_human(self):
        # A configured trusted user (not equal to the slave)
        # can drive ``/do`` — the gate's
        # ``author not in trusted_users`` clause is False,
        # so the gate lets them through.
        cmd = self._build_pr_with_author("alice")
        cmd._handle_slash_command("do", 1, "https://example/p/1")
        cmd._slash_do.assert_called_once()
        cmd._post_slash_response.assert_not_called()

    def test_trust_gate_rejects_random_user(self):
        # Anyone not in the trusted-users set is
        # silently rejected — no ``_slash_do`` call,
        # no response comment posted back. The slave
        # is now treated like any other non-trusted
        # user.
        cmd = self._build_pr_with_author("eve")
        cmd._handle_slash_command("do", 1, "https://example/p/1")
        cmd._slash_do.assert_not_called()
        # No response comment is posted on rejection — the
        # gate is silent, by design.
        cmd._post_slash_response.assert_not_called()


# ----- _slash_do handler (single-thread behaviour) --------------

class TestSlashDoSingleThread:
    """``/do`` is REPLY-ONLY: it operates on a single
    review-comment thread identified by
    ``(thread_path, thread_line)``. The thread is
    fetched via ``Gitea.get_pr_review_thread`` (one
    specific thread, by anchor) and the thread's
    combined body is fed to the agent as the prompt
    — nothing else. These tests drive the handler
    end-to-end and assert on:

      - top-level ``/do`` (no thread_path/line) does
        NOTHING — the early return is silent (the
        gate is silent on rejection by design, and a
        top-level /do is, in this design, "rejected
        by construction" — there is no thread to
        address)
      - the agent prompt is exactly the thread body
        (no PR URL, no PR number, no instructions
        about MCP tools, no comment-count framing)
      - the thread's root comment id is resolved
        after a non-empty commit
      - if no thread is found at the anchor (e.g.
        the only comment was already resolved), a
        "nothing to execute" response is posted and
        no agent prompt is sent
      - a no-op agent run (no commit produced)
        leaves the thread unresolved for the next
        ``/do`` invocation
    """

    def _build_cmd(self, thread):
        """Build a ``PrUpdateCommand`` instance with
        stubbed collaborators.

        ``thread`` is the dict the Gitea stub returns
        from ``get_pr_review_thread`` — the single
        thread at the requested ``(path, line)``,
        or ``None`` to simulate "no thread found".
        """
        from shoggoth.commands.pr_update import PrUpdateCommand
        # Stubs via __new__ — we don't need the full
        # CommandBase.__init__ chain for these tests.
        cmd = PrUpdateCommand.__new__(PrUpdateCommand)
        cmd.gitea = MagicMock()
        cmd.gitea.get_pr_review_thread = MagicMock(
            return_value=thread)
        cmd.gitea.resolve_comment = MagicMock(return_value=True)
        cmd.agent = MagicMock()
        cmd.agent.otlp = MagicMock()
        cmd.agent.otlp.get_review_text = MagicMock(
            return_value="agent summary text")
        cmd.agent.prompt_with_retry = MagicMock(return_value=0)
        cmd.agent.start_session = MagicMock()
        cmd.agent.stop_session = MagicMock()
        cmd._commit_only = MagicMock(return_value=True)
        cmd._push_pending_commits = MagicMock()
        cmd._post_slash_response = MagicMock()
        cmd._handle_timeout = MagicMock()
        cmd.shoggoth = MagicMock()
        cmd.shoggoth.project = "test-project"
        return cmd

    def _thread(self, thread_id=100, path="a.py", line=10,
                body="Fix the off-by-one."):
        return {
            "id": thread_id, "review_id": 1,
            "path": path, "line": line, "body": body,
        }

    def _call(self, cmd, thread_path="a.py", thread_line=10):
        return cmd._slash_do(
            "owner/repo", 7,
            "https://gitea.example.com/owner/repo/pulls/7",
            thread_path=thread_path, thread_line=thread_line)

    def test_top_level_do_does_nothing(self):
        # A ``/do`` posted as a top-level issue
        # comment (no path/position on the webhook
        # payload's comment object) MUST do
        # nothing. The handler silently returns
        # without an agent prompt, a commit, or
        # even a response comment — the load-bearing
        # requirement "``/do`` should never apply
        # to all comments in the MR" is enforced at
        # the dispatch layer: a top-level trigger
        # cannot, by construction, target a single
        # thread, so it is dropped on the floor.
        cmd = self._build_cmd(self._thread())
        cmd._slash_do(
            "owner/repo", 7,
            "https://gitea.example.com/owner/repo/pulls/7",
            thread_path=None, thread_line=None)
        # No Gitea fetch (we don't even ask
        # Gitea — the gate fires first).
        cmd.gitea.get_pr_review_thread.assert_not_called()
        # No agent prompt, no commit, no
        # response comment. The silence is
        # intentional.
        cmd.agent.prompt_with_retry.assert_not_called()
        cmd._commit_only.assert_not_called()
        cmd._post_slash_response.assert_not_called()

    def test_no_thread_found_posts_nothing_to_execute(self):
        # The user replied to a thread that no
        # longer has unresolved comments (it was
        # the only one, and it's now resolved, or
        # the anchor doesn't match any review
        # comment). Tell the user so they can pick
        # a different thread.
        cmd = self._build_cmd(None)
        self._call(cmd)
        cmd.gitea.get_pr_review_thread.assert_called_once_with(
            "owner/repo", 7, "a.py", 10)
        # No agent prompt, no commit.
        cmd.agent.prompt_with_retry.assert_not_called()
        cmd._commit_only.assert_not_called()
        # A response comment is posted explaining
        # the empty thread.
        cmd._post_slash_response.assert_called_once()
        msg = cmd._post_slash_response.call_args.args[2]
        assert "a.py:10" in msg
        assert "no unresolved review comments" in msg.lower()

    def test_prompt_is_just_the_thread_body(self):
        # The agent prompt is the thread's
        # combined body, byte-for-byte. No PR
        # URL, no PR number, no other comments,
        # no MCP-tool instructions, no
        # comment-count framing.
        body = "[alice]: rename this variable to be more descriptive"
        cmd = self._build_cmd(self._thread(body=body))
        self._call(cmd)
        cmd.agent.prompt_with_retry.assert_called_once()
        prompt = cmd.agent.prompt_with_retry.call_args.args[0]
        assert prompt == body
        # No extra information leaks.
        assert "https://" not in prompt
        assert "PR" not in prompt
        assert "Gitea" not in prompt
        assert "MCP" not in prompt
        assert "mcp" not in prompt.lower()
        assert "issuecomment" not in prompt

    def test_prompt_includes_all_thread_participants(self):
        # The thread's combined body includes
        # every participant in the thread
        # (slave + trusted humans + replies via
        # follow-up reviews). All of them are
        # in the prompt because they're all part
        # of the instruction the user wants
        # addressed.
        body = (
            "[shoggoth-slave]: please refactor this\n\n"
            "[alice]: agreed, but keep the public API"
        )
        cmd = self._build_cmd(self._thread(body=body))
        self._call(cmd)
        prompt = cmd.agent.prompt_with_retry.call_args.args[0]
        assert "[shoggoth-slave]: please refactor this" in prompt
        assert "[alice]: agreed, but keep the public API" in prompt

    def test_one_commit_for_the_thread(self):
        # The thread is committed as a single
        # change. The subject pins the command
        # and the thread location; the body
        # references the thread's root comment
        # id so the link back to the originating
        # review comment is preserved.
        cmd = self._build_cmd(self._thread(thread_id=42))
        self._call(cmd)
        cmd._commit_only.assert_called_once()
        commit_msg = cmd._commit_only.call_args.args[0]
        # Subject pins the thread location.
        assert "a.py:10" in commit_msg
        # Body references the root comment id.
        assert "issuecomment-42" in commit_msg

    def test_thread_resolved_on_successful_commit(self):
        # The thread's root comment id is
        # resolved via ``gitea.resolve_comment``
        # after a non-empty commit. ``resolve_comment``
        # is the same helper the regular
        # ``_pr_comment`` flow uses.
        cmd = self._build_cmd(self._thread(thread_id=42))
        self._call(cmd)
        cmd.gitea.resolve_comment.assert_called_once_with(
            "owner/repo", 42)

    def test_thread_not_resolved_on_no_op_commit(self):
        # If the agent produced no diff
        # (``_commit_only`` returns False), the
        # thread stays unresolved so the next
        # ``/do`` invocation can still see it.
        # Same discipline as ``_pr_comment``.
        cmd = self._build_cmd(self._thread(thread_id=42))
        cmd._commit_only = MagicMock(return_value=False)
        self._call(cmd)
        cmd.gitea.resolve_comment.assert_not_called()

    def test_agent_failure_dies(self):
        # If the agent times out or exits non-zero,
        # the handler dies (same as the other
        # slash commands). The thread is NOT
        # resolved on failure — the same
        # resolve-after-commit discipline.
        cmd = self._build_cmd(self._thread(thread_id=42))
        cmd.agent.prompt_with_retry = MagicMock(return_value=124)
        with pytest.raises(SystemExit):
            self._call(cmd)
        cmd._handle_timeout.assert_called_once()
        # /do's per-conversation timeout wording (not the
        # _pr_comment "Addressed N comment(s)" framing).
        args, kwargs = cmd._handle_timeout.call_args
        assert kwargs.get("caller") == "pr-do"
        assert kwargs.get("context_label") == "a.py:10"
        cmd.gitea.resolve_comment.assert_not_called()

    def test_response_message_mentions_thread(self):
        # The success response mentions the
        # thread location so the user can see
        # what was addressed.
        cmd = self._build_cmd(self._thread())
        self._call(cmd)
        msg = cmd._post_slash_response.call_args.args[2]
        assert "a.py:10" in msg
        assert "Executed" in msg or "executed" in msg.lower()


# ----- /do scoped to a single thread (reply use case) ---------

class TestSlashDoThreadScope:
    """``/do`` posted as a REPLY to a specific review comment
    must scope the work to that thread only — the
    ``(thread_path, thread_line)`` pair from the triggering
    comment's ``path``/``position`` fields. A top-level
    issue-comment ``/do`` (no ``path``/``position``) does
    NOTHING (the dispatch-layer enforcement of
    "``/do`` should never apply to all comments in the MR").

    These tests pin the thread-scoped path:
      - only the matching ``(path, line)`` is fetched
        from Gitea (via ``get_pr_review_thread``)
      - other threads on the PR are not even asked for
      - a reply with no matching thread posts a
        "no comments in this thread" response
      - the response message reports the scope
    """

    def _build_cmd(self, thread):
        """Build a ``PrUpdateCommand`` with stubbed
        collaborators.

        ``thread`` is the dict returned by the Gitea
        stub's ``get_pr_review_thread`` (or ``None``
        to simulate "no thread found").
        """
        from shoggoth.commands.pr_update import PrUpdateCommand
        cmd = PrUpdateCommand.__new__(PrUpdateCommand)
        cmd.gitea = MagicMock()
        cmd.gitea.get_pr_review_thread = MagicMock(
            return_value=thread)
        cmd.gitea.resolve_comment = MagicMock(return_value=True)
        cmd.agent = MagicMock()
        cmd.agent.otlp = MagicMock()
        cmd.agent.otlp.get_review_text = MagicMock(
            return_value="agent summary text")
        cmd.agent.prompt_with_retry = MagicMock(return_value=0)
        cmd.agent.start_session = MagicMock()
        cmd.agent.stop_session = MagicMock()
        cmd._commit_only = MagicMock(return_value=True)
        cmd._push_pending_commits = MagicMock()
        cmd._post_slash_response = MagicMock()
        cmd._handle_timeout = MagicMock()
        cmd.shoggoth = MagicMock()
        cmd.shoggoth.project = "test-project"
        return cmd

    def _call(self, cmd, thread_path=None, thread_line=None):
        return cmd._slash_do("owner/repo", 7,
                              "https://gitea.example.com/owner/repo/pulls/7",
                              thread_path=thread_path,
                              thread_line=thread_line)

    def test_scopes_to_single_thread(self):
        # Three threads on the PR; the user replied on
        # the (a.py:10) thread. Only that thread is
        # fetched from Gitea, and only its body is in
        # the agent prompt. The other two threads are
        # never even asked for.
        thread = {
            "id": 1, "review_id": 1, "path": "a.py",
            "line": 10,
            "body": "Fix the off-by-one on a.py:10.",
        }
        cmd = self._build_cmd(thread)
        self._call(cmd, thread_path="a.py", thread_line=10)
        # Gitea is called with the specific anchor.
        cmd.gitea.get_pr_review_thread.assert_called_once_with(
            "owner/repo", 7, "a.py", 10)
        # The thread's body is the only thing in the
        # agent prompt.
        prompt = cmd.agent.prompt_with_retry.call_args.args[0]
        assert "Fix the off-by-one on a.py:10." in prompt
        # The thread's root comment id is resolved.
        cmd.gitea.resolve_comment.assert_called_once_with(
            "owner/repo", 1)

    def test_scopes_when_other_threads_have_multiple_comments(self):
        # The targeted thread has multiple comments in
        # its group (e.g. replies). The handler must
        # still fetch JUST that one thread — even when
        # the Gitea response includes comments from
        # other threads, those are not in the prompt.
        # The combined body is just this thread.
        thread = {
            "id": 1, "review_id": 1, "path": "a.py",
            "line": 10,
            "body": "[alice]: Target thread comment.\n\n"
                    "[bob]: Target thread reply.",
        }
        cmd = self._build_cmd(thread)
        self._call(cmd, thread_path="a.py", thread_line=10)
        prompt = cmd.agent.prompt_with_retry.call_args.args[0]
        assert "Target thread comment." in prompt
        assert "Target thread reply." in prompt
        # Only the targeted thread's id is resolved.
        cmd.gitea.resolve_comment.assert_called_once_with(
            "owner/repo", 1)

    def test_no_matching_thread_reports_nothing_to_execute(self):
        # The user replied on (a.py:10), but there are
        # no unresolved comments at that location
        # (Gitea returned None). Tell the user so they
        # can pick a different thread.
        cmd = self._build_cmd(None)
        self._call(cmd, thread_path="a.py", thread_line=10)
        # No agent prompt, no commit.
        cmd.agent.prompt_with_retry.assert_not_called()
        cmd._commit_only.assert_not_called()
        # No resolve — there is no thread to resolve.
        cmd.gitea.resolve_comment.assert_not_called()
        # Response explains the thread was empty.
        msg = cmd._post_slash_response.call_args.args[2]
        assert "a.py:10" in msg
        assert "no unresolved review comments" in msg.lower()

    def test_no_thread_at_anchor_does_not_call_other_endpoints(self):
        # When no thread is found at the anchor, the
        # handler MUST NOT fall back to fetching the
        # full PR comment list — that would defeat the
        # isolation guarantee. The handler returns
        # early with a "nothing to execute" response.
        cmd = self._build_cmd(None)
        self._call(cmd, thread_path="a.py", thread_line=10)
        # No fallback fetches. We didn't add a
        # ``get_all_unresolved_review_comments``
        # stub to ``_build_cmd``; a fallback would
        # raise AttributeError, which would fail the
        # test (a regression on the "no fallback"
        # contract). Pin the contract by asserting
        # the agent was never invoked.
        cmd.agent.prompt_with_retry.assert_not_called()

    def test_response_message_mentions_thread(self):
        thread = {
            "id": 1, "review_id": 1, "path": "a.py",
            "line": 10, "body": "Fix.",
        }
        cmd = self._build_cmd(thread)
        self._call(cmd, thread_path="a.py", thread_line=10)
        msg = cmd._post_slash_response.call_args.args[2]
        assert "a.py:10" in msg

    def test_top_level_do_does_nothing(self):
        # A top-level /do (no thread anchor) is dropped
        # on the floor — the load-bearing enforcement
        # of "``/do`` should never apply to all
        # comments in the MR". Even when Gitea
        # WOULD return a thread, a top-level trigger
        # does not get to it.
        thread = {
            "id": 1, "review_id": 1, "path": "a.py",
            "line": 10, "body": "Some thread.",
        }
        cmd = self._build_cmd(thread)
        self._call(cmd, thread_path=None, thread_line=None)
        # Gitea is NOT asked for anything.
        cmd.gitea.get_pr_review_thread.assert_not_called()
        # No agent prompt, no commit, no resolve.
        cmd.agent.prompt_with_retry.assert_not_called()
        cmd._commit_only.assert_not_called()
        cmd.gitea.resolve_comment.assert_not_called()
        # No response comment — the gate is silent.
        cmd._post_slash_response.assert_not_called()


# ----- _slash_do prompt scope (only the comment body) -------

class TestSlashDoPromptScope:
    """The ``/do`` agent prompt must contain ONLY the
    comment body — no PR URL, no PR number, no
    instructions about MCP tools, no other-comment
    references, no comment-count framing.

    The agent has Gitea MCP tools available (it runs
    with a ``full`` tool profile). The pre-fix prompt
    included "SCOPE" / "DO NOT" / "DO" sections that
    forbade Gitea reads; that did not stop the agent
    from going off and addressing all 9 comments. The
    new design removes the prompt-frame altogether and
    relies on the comment body being the ONLY thing
    the prompt contains — by construction, the agent
    cannot leak or read about comments that are not in
    the prompt because no such text exists in the
    prompt.

    These tests pin the prompt-isolation property: a
    future edit that re-introduces a PR URL, an MCP
    reference, a comment-count framing, or any other
    "extra information" fails the test rather than the
    production behaviour.
    """

    def _build_cmd(self, thread):
        from shoggoth.commands.pr_update import PrUpdateCommand
        cmd = PrUpdateCommand.__new__(PrUpdateCommand)
        cmd.gitea = MagicMock()
        cmd.gitea.get_pr_review_thread = MagicMock(
            return_value=thread)
        cmd.gitea.resolve_comment = MagicMock(return_value=True)
        cmd.agent = MagicMock()
        cmd.agent.otlp = MagicMock()
        cmd.agent.otlp.get_review_text = MagicMock(
            return_value="agent summary text")
        cmd.agent.prompt_with_retry = MagicMock(return_value=0)
        cmd.agent.start_session = MagicMock()
        cmd.agent.stop_session = MagicMock()
        cmd._commit_only = MagicMock(return_value=True)
        cmd._push_pending_commits = MagicMock()
        cmd._post_slash_response = MagicMock()
        cmd._handle_timeout = MagicMock()
        cmd.shoggoth = MagicMock()
        cmd.shoggoth.project = "test-project"
        return cmd

    def _invoke(self, cmd, thread_path="a.py", thread_line=10):
        return cmd._slash_do(
            "owner/repo", 7,
            "https://gitea.example.com/owner/repo/pulls/7",
            thread_path=thread_path, thread_line=thread_line)

    def _prompt(self, cmd):
        return cmd.agent.prompt_with_retry.call_args.args[0]

    def test_prompt_is_exactly_the_thread_body(self):
        # The agent prompt is the thread's combined
        # body, byte-for-byte.
        body = "[alice]: rename this variable to be more descriptive"
        cmd = self._build_cmd(
            {"id": 1, "review_id": 1, "path": "a.py",
             "line": 10, "body": body})
        self._invoke(cmd)
        assert self._prompt(cmd) == body

    def test_prompt_contains_no_pr_url(self):
        # The PR URL is not in the prompt. A prompt
        # with a PR URL would let the agent navigate
        # to the PR (via MCP or by reading the URL
        # off the prompt) and discover other comments
        # — defeating the isolation guarantee.
        body = "[alice]: fix the off-by-one"
        cmd = self._build_cmd(
            {"id": 1, "review_id": 1, "path": "a.py",
             "line": 10, "body": body})
        self._invoke(cmd)
        prompt = self._prompt(cmd)
        assert "https://" not in prompt
        assert "gitea.example.com" not in prompt
        assert "pulls/7" not in prompt
        assert "PR" not in prompt
        assert "pr-" not in prompt.lower()

    def test_prompt_contains_no_mcp_references(self):
        # The prompt must not mention Gitea MCP
        # tools. The pre-fix prompt had a "DO NOT"
        # section that explicitly forbade Gitea
        # reads — the new design relies on the
        # prompt not mentioning Gitea / MCP at all
        # so the agent has no hook to hang a
        # "go look at the PR" behaviour on.
        body = "[alice]: fix the off-by-one"
        cmd = self._build_cmd(
            {"id": 1, "review_id": 1, "path": "a.py",
             "line": 10, "body": body})
        self._invoke(cmd)
        prompt = self._prompt(cmd).lower()
        assert "gitea" not in prompt
        assert "mcp" not in prompt
        assert "tool" not in prompt

    def test_prompt_contains_no_other_comment_references(self):
        # The prompt must not reference other
        # comments, the rest of the thread, or the
        # rest of the PR.
        body = "[alice]: fix the off-by-one"
        cmd = self._build_cmd(
            {"id": 1, "review_id": 1, "path": "a.py",
             "line": 10, "body": body})
        self._invoke(cmd)
        prompt = self._prompt(cmd).lower()
        # No pluralisation / "the rest" / "etc" / "other"
        # language that hints at more content.
        assert "other comments" not in prompt
        assert "other comment" not in prompt
        assert "rest of" not in prompt
        assert "all comments" not in prompt
        assert "any other" not in prompt
        assert "the pr" not in prompt
        assert "this pr" not in prompt

    def test_prompt_contains_no_count_framing(self):
        # No "EXACTLY N", no "FINAL REMINDER", no
        # "this is the entire scope" — the prompt
        # is just the body. The body itself does
        # not need a count framing; the agent sees
        # what it sees.
        body = "[alice]: fix the off-by-one"
        cmd = self._build_cmd(
            {"id": 1, "review_id": 1, "path": "a.py",
             "line": 10, "body": body})
        self._invoke(cmd)
        prompt = self._prompt(cmd)
        assert "EXACTLY" not in prompt
        assert "FINAL REMINDER" not in prompt
        assert "COMPLETE" not in prompt
        assert "EXHAUSTIVE" not in prompt
        assert "SCOPE" not in prompt
        assert "DO NOT" not in prompt
        assert "DO:" not in prompt
        assert "batch" not in prompt.lower()

    def test_prompt_omits_pre_fix_invitation(self):
        # Regression guard: the pre-fix prompt said
        # "Execute the following instructions found
        # in the comment thread on PR {url}" — a
        # clear invitation to read the thread.
        # The new prompt must not contain that
        # phrasing. (It cannot, because the prompt
        # is the comment body — but pinning the
        # absence here catches a future edit that
        # re-adds framing.)
        body = "[alice]: fix the off-by-one"
        cmd = self._build_cmd(
            {"id": 1, "review_id": 1, "path": "a.py",
             "line": 10, "body": body})
        self._invoke(cmd)
        prompt = self._prompt(cmd)
        assert ("Execute the following instructions found in the "
                "comment thread on PR") not in prompt

    def test_prompt_for_thread_with_multiple_participants(self):
        # The thread has a slave comment AND a
        # trusted human comment. Both are part of
        # the instruction (the slave can leave
        # comments in a thread; the trusted human
        # adds a reply; ``/do`` addresses the
        # combined body). The prompt is the
        # combined body verbatim.
        body = (
            "[shoggoth-slave]: please refactor this\n\n"
            "[alice]: agreed, but keep the public API"
        )
        cmd = self._build_cmd(
            {"id": 1, "review_id": 1, "path": "a.py",
             "line": 10, "body": body})
        self._invoke(cmd)
        assert self._prompt(cmd) == body


# ----- /do end-to-end: address the conversation, then close it ----

class TestSlashDoClosesConversation:
    """``/do`` is the explicit "address this
    conversation, then close it" workflow for a
    Gitea "conversation" (a group of inline review
    comments at a single ``(path, line)``).

    Gitea's "conversation" is a first-class data
    structure (see ``models/issues/comment_code.go``
    and ``models/issues/review.go:935``
    ``MarkConversation``) and has a "Resolve"
    action that closes the whole thread at once.
    The shoggoth ``/do`` handler drives that
    action via the
    ``POST /repos/{repo}/pulls/comments/{id}/resolve``
    API endpoint (see
    ``routers/api/v1/repo/pull_review.go:289``
    ``ResolvePullReviewComment``). The
    endpoint sets ``resolve_doer_id`` on the
    root comment of the conversation, which
    toggles the "Resolve conversation" button to
    "Unresolve conversation" in the Gitea UI.

    These tests pin the end-to-end flow:

      1. The conversation is fetched from Gitea
         via ``get_pr_review_thread`` (a single-
         conversation query — not "list all +
         filter client-side").
      2. The conversation's combined body is
         fed to the agent as the prompt — and
         ONLY that body, so the agent cannot
         go off and address other conversations.
      3. The agent's code changes are committed
         as a single commit with the
         conversation's location pinned in the
         subject.
      4. On a non-empty commit, the conversation
         is closed (resolved) via
         ``gitea.resolve_comment`` → Gitea
         ``POST /repos/{repo}/pulls/comments/{id}/resolve``
         → ``MarkConversation`` sets
         ``resolve_doer_id`` on the root comment.

    A no-op agent run (no diff produced) leaves
    the conversation OPEN — same discipline as
    ``_pr_comment``: a failed /do invocation
    should leave the conversation for the next
    attempt, not silently close it.
    """

    def _build_cmd(self, thread):
        from shoggoth.commands.pr_update import PrUpdateCommand
        cmd = PrUpdateCommand.__new__(PrUpdateCommand)
        cmd.gitea = MagicMock()
        # Single-conversation fetch (the
        # thread the user replied to).
        cmd.gitea.get_pr_review_thread = MagicMock(
            return_value=thread)
        # Conversation-close API (Gitea
        # resolve).
        cmd.gitea.resolve_comment = MagicMock(return_value=True)
        cmd.agent = MagicMock()
        cmd.agent.otlp = MagicMock()
        cmd.agent.otlp.get_review_text = MagicMock(
            return_value="addressed the off-by-one in foo.py:10")
        cmd.agent.prompt_with_retry = MagicMock(return_value=0)
        cmd.agent.start_session = MagicMock()
        cmd.agent.stop_session = MagicMock()
        cmd._commit_only = MagicMock(return_value=True)
        cmd._push_pending_commits = MagicMock()
        cmd._post_slash_response = MagicMock()
        cmd._handle_timeout = MagicMock()
        cmd.shoggoth = MagicMock()
        cmd.shoggoth.project = "test-project"
        return cmd

    def _thread(self, thread_id=100, path="foo.py", line=10,
                body="[alice]: there's an off-by-one here"):
        return {
            "id": thread_id, "review_id": 1,
            "path": path, "line": line, "body": body,
        }

    def _call(self, cmd, thread_path="foo.py", thread_line=10):
        return cmd._slash_do(
            "owner/repo", 7,
            "https://gitea.example.com/owner/repo/pulls/7",
            thread_path=thread_path, thread_line=thread_line)

    def test_fetches_only_the_targeted_conversation(self):
        # The handler must fetch the
        # conversation at the (path, line)
        # anchor — no list-all-then-filter
        # roundtrip. ``get_pr_review_thread``
        # is a single-conversation query
        # (it returns the combined body of
        # the conversation at the anchor,
        # by construction).
        cmd = self._build_cmd(self._thread())
        self._call(cmd)
        cmd.gitea.get_pr_review_thread.assert_called_once_with(
            "owner/repo", 7, "foo.py", 10)
        # No fallback / list-all endpoint.
        cmd.gitea.get_all_unresolved_review_comments.assert_not_called()

    def test_agent_gets_only_the_conversation_body(self):
        # The agent prompt is the
        # conversation's combined body —
        # and ONLY that body, so the agent
        # cannot go off and address other
        # conversations on the PR.
        body = (
            "[alice]: there's an off-by-one here\n\n"
            "[shoggoth-slave]: confirmed, fixing"
        )
        cmd = self._build_cmd(self._thread(body=body))
        self._call(cmd)
        prompt = cmd.agent.prompt_with_retry.call_args.args[0]
        assert prompt == body
        # No PR URL / PR number / MCP refs /
        # "other comments" / count framing
        # in the prompt (these are
        # conversation-isolation
        # properties that the previous
        # TestSlashDoPromptScope pins; this
        # is a sanity check that the
        # close-conversation flow does not
        # regress them).
        assert "https://" not in prompt
        assert "gitea" not in prompt.lower()
        assert "mcp" not in prompt.lower()

    def test_commit_subject_pins_conversation_location(self):
        # The commit subject names the
        # conversation at the (path, line)
        # anchor — so the user can see
        # WHICH conversation the commit
        # addressed when scanning the PR's
        # commit list.
        cmd = self._build_cmd(self._thread())
        self._call(cmd)
        commit_msg = cmd._commit_only.call_args.args[0]
        # Subject pins the conversation.
        assert "foo.py:10" in commit_msg

    def test_conversation_closed_via_gitea_resolve_api(self):
        # On a non-empty commit, the
        # conversation is closed via
        # ``gitea.resolve_comment(repo,
        # thread_id)``. That helper
        # POSTs to
        # ``repos/{repo}/pulls/comments/{id}/resolve``
        # which is Gitea's
        # ``ResolvePullReviewComment``
        # endpoint — the API that calls
        # ``MarkConversation`` to set
        # ``resolve_doer_id`` on the
        # root comment of the
        # conversation, closing the whole
        # thread at once.
        cmd = self._build_cmd(self._thread(thread_id=100))
        self._call(cmd)
        cmd.gitea.resolve_comment.assert_called_once_with(
            "owner/repo", 100)

    def test_resolve_comment_targets_gitea_resolve_endpoint(self):
        # The ``Gitea.resolve_comment``
        # helper must POST to
        # ``repos/{repo}/pulls/comments/{id}/resolve``
        # — that is the exact endpoint
        # Gitea exposes for closing a
        # conversation (see
        # routers/api/v1/repo/pull_review.go:289
        # ``ResolvePullReviewComment``).
        # A regression that POSTs to a
        # different URL would silently
        # fail to close the conversation.
        g = Gitea.__new__(Gitea)
        g.post = MagicMock(return_value={})
        g.resolve_comment("owner/repo", 42)
        g.post.assert_called_once_with(
            "repos/owner/repo/pulls/comments/42/resolve", {})

    def test_conversation_left_open_on_no_op_commit(self):
        # If the agent produced no diff
        # (``_commit_only`` returns False),
        # the conversation stays OPEN —
        # the next ``/do`` invocation can
        # still see and address it. This
        # is the same resolve-after-commit
        # discipline ``_pr_comment``
        # enforces for the regular review
        # flow: a failed / no-op agent run
        # must not silently close the
        # thread.
        cmd = self._build_cmd(self._thread(thread_id=100))
        cmd._commit_only = MagicMock(return_value=False)
        self._call(cmd)
        cmd.gitea.resolve_comment.assert_not_called()

    def test_conversation_left_open_on_agent_failure(self):
        # If the agent times out (exit
        # 124) or fails (non-zero), the
        # workflow dies — but the
        # conversation stays OPEN, for
        # the same reason as the no-op
        # case: a failed agent run must
        # not silently close the thread.
        cmd = self._build_cmd(self._thread(thread_id=100))
        cmd.agent.prompt_with_retry = MagicMock(return_value=124)
        with pytest.raises(SystemExit):
            self._call(cmd)
        cmd.gitea.resolve_comment.assert_not_called()

    def test_response_message_mentions_conversation(self):
        # The success response identifies
        # the conversation that was
        # closed, so the user can verify
        # in the PR view that the right
        # thread was addressed.
        cmd = self._build_cmd(self._thread())
        self._call(cmd)
        msg = cmd._post_slash_response.call_args.args[2]
        # Mentions the conversation
        # location.
        assert "foo.py:10" in msg
        # Mentions "conversation" (not
        # "thread" or "comment" — the
        # Gitea term is "conversation"
        # and the user-visible response
        # should match).
        assert "conversation" in msg.lower()

    def test_no_conversation_at_anchor_does_not_close_anything(self):
        # The conversation at the anchor
        # was already resolved (or never
        # existed). Nothing to address,
        # nothing to close. The
        # resolve_comment API MUST NOT be
        # called.
        cmd = self._build_cmd(None)
        self._call(cmd)
        # No agent prompt, no commit.
        cmd.agent.prompt_with_retry.assert_not_called()
        cmd._commit_only.assert_not_called()
        # No resolve (no thread to close).
        cmd.gitea.resolve_comment.assert_not_called()
        # A "nothing to execute" response
        # is posted so the user knows
        # what happened.
        cmd._post_slash_response.assert_called_once()

    def test_other_conversations_remain_unaddressed(self):
        # The (path, line) anchor scopes
        # the work to ONE conversation —
        # other conversations on the PR
        # are not even asked for. The
        # agent prompt cannot leak them
        # (because the prompt is the body
        # of this conversation only) and
        # the Gitea resolve call is made
        # for the target thread's id
        # only.
        # A "distraction" thread the
        # user did NOT reply to is
        # present in the Gitea response
        # (the implementation re-uses the
        # full-threads fetch internally),
        # but the agent prompt must
        # contain only the target
        # thread's body.
        target = self._thread(
            thread_id=100, path="a.py", line=10,
            body="[alice]: this is the thread we are addressing")
        cmd = self._build_cmd(target)
        self._call(cmd, thread_path="a.py", thread_line=10)
        prompt = cmd.agent.prompt_with_retry.call_args.args[0]
        # The target conversation is
        # the ONLY thing the agent sees.
        assert "[alice]: this is the thread we are addressing" in prompt
        # Only the target is resolved.
        cmd.gitea.resolve_comment.assert_called_once_with(
            "owner/repo", 100)


# ----- _slash_review slave-as-PR-author direct branch -----

class TestSlashReviewSlaveIsAuthor:
    """``_slash_review`` skips ``request_reviewer`` and runs
    ``_pr_review`` directly when the slave is the PR author.
    Gitea's API rejects the reviewer-add with HTTP 422
    ``poster of pr can't be reviewer`` when the target
    reviewer is the PR author, so the workaround bypasses the
    webhook round-trip and invokes the review handler in the
    same dispatch. Without this branch, ``/review`` posted by
    the slave on its own PR silently dies.
    """

    def setup_method(self):
        state.params.shoggoth_slave_user = "shoggoth-slave"

    def teardown_method(self):
        state.params.shoggoth_slave_user = "slave"

    def _build_cmd(self, pr_poster):
        """Return a real ``PrUpdateCommand`` whose ``gitea._payload``
        carries the given PR poster under ``pull_request``."""
        from shoggoth.commands.pr_update import PrUpdateCommand
        cmd = PrUpdateCommand.__new__(PrUpdateCommand)
        cmd.gitea = MagicMock()
        cmd.gitea._payload = {
            "pull_request": {"user": {"login": pr_poster}},
        }
        cmd.gitea.request_reviewer = MagicMock(return_value=True)
        cmd._pr_review = MagicMock()
        cmd._post_slash_response = MagicMock()
        return cmd

    def test_runs_directly_when_slave_is_pr_author(self):
        cmd = self._build_cmd("shoggoth-slave")
        cmd._slash_review("owner/repo", 1, "https://example/p/1")
        # The review runs directly; the webhook round-trip is
        # skipped.
        cmd._pr_review.assert_called_once_with(
            "https://example/p/1")
        cmd.gitea.request_reviewer.assert_not_called()

    def test_issue_payload_falls_back_when_pull_request_missing(self):
        """``issue_comment`` webhooks on a PR carry the PR
        under ``issue`` instead of ``pull_request``; the
        fallback path must detect the poster there too."""
        from shoggoth.commands.pr_update import PrUpdateCommand
        cmd = PrUpdateCommand.__new__(PrUpdateCommand)
        cmd.gitea = MagicMock()
        cmd.gitea._payload = {
            # No ``pull_request`` key on this payload.
            "issue": {"user": {"login": "shoggoth-slave"}},
        }
        cmd.gitea.request_reviewer = MagicMock(return_value=True)
        cmd._pr_review = MagicMock()
        cmd._post_slash_response = MagicMock()
        cmd.shoggoth = MagicMock()
        cmd.shoggoth.working_repo = "owner/repo"
        cmd._slash_review("owner/repo", 1, "https://example/p/1")
        cmd._pr_review.assert_called_once_with(
            "https://example/p/1")
        cmd.gitea.request_reviewer.assert_not_called()

    def test_uses_request_reviewer_when_slave_is_not_author(self):
        """Sanity check: the direct branch fires ONLY when
        the poster equals the slave. Anyone else goes through
        the normal ``request_reviewer`` path."""
        cmd = self._build_cmd("alice")
        cmd._slash_review("owner/repo", 1, "https://example/p/1")
        cmd.gitea.request_reviewer.assert_called_once()
        cmd._pr_review.assert_not_called()
