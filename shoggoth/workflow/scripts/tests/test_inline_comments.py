"""Unit tests for the inline-comment formatter in ``shoggoth.agent``.

The formatter (``Agent._format_review_json``) renders a qwen
review JSON payload as a Markdown review body and a list of
inline comments. The CRITICAL contract is that
``new_position`` in each inline comment is a FILE LINE NUMBER,
not a unified-diff position — the Gitea API field is named
``new_position`` but the struct comment is explicit ("if
comment to new file line or 0") and the swagger maps it to
``NewLineNum``. Gitea uses the value to cut the diff context
around that line (``git.GetFileDiffCutAroundLine``) and stores
it as ``LineNum`` in the comment. Sending a diff position
instead of a line number is the root cause of the
"comment at file:493 lands at line 12" bug.

These tests pin the new behaviour: line numbers flow through
unchanged, the path is normalised (leading ``./`` and ``/``
stripped so ``./README.md`` matches ``README.md``), the
inline comment body carries the ``path:line`` location, and
findings posted inline are not duplicated in the review body.
"""
import os
import sys

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, SCRIPTS_DIR)
os.environ.setdefault("SHOGGOTH_DOMAIN", "example.test")

from shoggoth.agent import Agent  # noqa: E402


# ----- _format_review_json (line-number-based positioning) ------

class TestFormatReviewJsonWithInline:
    def test_no_diff_text_returns_string_for_backwards_compat(self):
        data = {"verdict": "OK", "findings": [], "summary": "Looks good."}
        result = Agent._format_review_json(data, "title", "url")
        assert isinstance(result, str)

    def test_with_diff_text_returns_body_and_inline_list(self):
        data = {"verdict": "OK", "findings": [], "summary": "Looks good."}
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+line2\n"
        )
        body, comments = Agent._format_review_json(
            data, "title", "url", diff_text=diff)
        assert isinstance(body, str)
        assert comments == []

    def test_finding_new_position_is_line_number(self):
        # CRITICAL: Gitea's ``new_position`` is a file line
        # number, not a diff position. The agent reports the
        # line it sees in the file; that value flows
        # through unchanged into ``new_position``.
        data = {"verdict": "OK", "findings": [
            {"severity": "Critical", "path": "foo.py", "line": 493,
             "body": "Issue."},
        ]}
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+line2\n"
        )
        _, comments = Agent._format_review_json(
            data, "title", "url", diff_text=diff)
        assert len(comments) == 1
        assert comments[0]["path"] == "foo.py"
        assert comments[0]["new_position"] == 493

    def test_comment_body_includes_path_and_line(self):
        # The inline comment body must carry the
        # ``path:line`` location so reviewers can see the
        # context at the anchor without cross-referencing
        # the rolled-up list.
        data = {"verdict": "OK", "findings": [
            {"severity": "Critical", "path": "foo.py", "line": 2,
             "body": "Issue."},
        ]}
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+line2\n"
        )
        _, comments = Agent._format_review_json(
            data, "title", "url", diff_text=diff)
        assert "foo.py:2" in comments[0]["body"]
        assert "Issue." in comments[0]["body"]

    def test_inline_posted_finding_not_duplicated_in_body(self):
        # A finding that becomes an inline comment must
        # NOT also be printed in full under ``### Findings``
        # — that would show the same text twice. The body
        # only shows a reference; the full text lives in
        # the inline comment itself.
        data = {"verdict": "OK", "findings": [
            {"severity": "Critical", "path": "foo.py", "line": 2,
             "body": "DUPLICATE-MARKER-XYZ"},
        ]}
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+line2\n"
        )
        body, comments = Agent._format_review_json(
            data, "title", "url", diff_text=diff)
        assert len(comments) == 1
        assert "DUPLICATE-MARKER-XYZ" in comments[0]["body"]
        assert "DUPLICATE-MARKER-XYZ" not in body
        assert "see inline comment" in body
        assert "foo.py:2" in body

    def test_finding_without_line_number_stays_in_body_only(self):
        data = {"verdict": "OK", "findings": [
            {"severity": "Suggestion", "path": "foo.py",  # no line
             "body": "Whole-file concern."},
        ]}
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+line2\n"
        )
        body, comments = Agent._format_review_json(
            data, "title", "url", diff_text=diff)
        assert comments == []

    def test_finding_with_zero_or_negative_line_stays_in_body_only(self):
        # 0 or negative line numbers are treated as
        # "no inline comment" — Gitea interprets them as
        # the old file (negative) or as "no line" (0).
        data = {"verdict": "OK", "findings": [
            {"severity": "Critical", "path": "foo.py", "line": 0,
             "body": "BODY-MARKER-ZERO"},
            {"severity": "Critical", "path": "foo.py", "line": -5,
             "body": "BODY-MARKER-NEG"},
        ]}
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+line2\n"
        )
        body, comments = Agent._format_review_json(
            data, "title", "url", diff_text=diff)
        assert comments == []
        assert "BODY-MARKER-ZERO" in body
        assert "BODY-MARKER-NEG" in body

    def test_severity_uppercased_in_comment_body(self):
        data = {"verdict": "OK", "findings": [
            {"severity": "suggestion", "path": "foo.py", "line": 1,
             "body": "Body text."},
        ]}
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,1 +1,1 @@\n"
            " line1\n"
        )
        _, comments = Agent._format_review_json(
            data, "title", "url", diff_text=diff)
        assert comments[0]["body"].startswith("**[SUGGESTION]**")

    def test_mixed_inline_and_non_inline_findings(self):
        # One finding has a line, the other doesn't. The
        # one with a line becomes an inline comment
        # (with the full text in the comment, NOT in the
        # body); the one without a line stays in the body.
        data = {"verdict": "OK", "findings": [
            {"severity": "Critical", "path": "foo.py", "line": 2,
             "body": "INLINE-TEXT"},
            {"severity": "Suggestion", "path": "foo.py",  # no line
             "body": "BODY-ONLY-TEXT"},
        ]}
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+line2\n"
        )
        body, comments = Agent._format_review_json(
            data, "title", "url", diff_text=diff)
        assert len(comments) == 1
        assert "INLINE-TEXT" in comments[0]["body"]
        assert "INLINE-TEXT" not in body
        assert "BODY-ONLY-TEXT" in body
        assert "BODY-ONLY-TEXT" not in comments[0]["body"]

    def test_string_line_normalised_to_int(self):
        # The agent sometimes reports ``line`` as a
        # string ("260"). Gitea's API expects an int.
        data = {"verdict": "OK", "findings": [
            {"severity": "Critical", "path": "foo.py", "line": "2",
             "body": "Issue."},
        ]}
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+line2\n"
        )
        _, comments = Agent._format_review_json(
            data, "title", "url", diff_text=diff)
        assert len(comments) == 1
        assert comments[0]["new_position"] == 2

    def test_path_with_leading_dot_slash_normalised(self):
        # The agent sometimes reports the path with a
        # leading ``./``. Normalise before sending to
        # Gitea (which expects the path relative to the
        # repo root, without prefix).
        data = {"verdict": "OK", "findings": [
            {"severity": "Critical", "path": "./foo.py", "line": 2,
             "body": "Issue."},
        ]}
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+line2\n"
        )
        _, comments = Agent._format_review_json(
            data, "title", "url", diff_text=diff)
        assert len(comments) == 1
        # Path is normalised (no leading ``./``).
        assert comments[0]["path"] == "foo.py"

    def test_path_with_leading_slash_normalised(self):
        data = {"verdict": "OK", "findings": [
            {"severity": "Critical", "path": "/foo.py", "line": 2,
             "body": "Issue."},
        ]}
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+line2\n"
        )
        _, comments = Agent._format_review_json(
            data, "title", "url", diff_text=diff)
        assert len(comments) == 1
        assert comments[0]["path"] == "foo.py"


class TestRegressionLineNumberAnchoring:
    """Regression: the "comment at file:493 lands at line 12"
    bug was caused by sending a diff POSITION (12) to Gitea's
    ``new_position`` field, which actually expects a file LINE
    NUMBER. These tests pin the corrected behaviour."""

    def test_makefile_493_anchors_at_493(self):
        # The bug report: a finding at "Makefile:493" was
        # placed at line 12. Root cause: the diff position
        # (12) was sent instead of the line number (493).
        # After the fix, the line number flows through
        # unchanged.
        data = {"verdict": "OK", "findings": [
            {"severity": "info", "path": "Makefile", "line": 493,
             "body": "At line 493."},
        ]}
        diff = (
            "diff --git a/Makefile b/Makefile\n"
            "index abc..def 100644\n"
            "--- a/Makefile\n"
            "+++ b/Makefile\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+line2\n"
        )
        _, comments = Agent._format_review_json(
            data, "title", "url", diff_text=diff)
        assert len(comments) == 1
        assert comments[0]["path"] == "Makefile"
        assert comments[0]["new_position"] == 493

    def test_readme_260_anchors_at_260(self):
        # Second bug report: "review pointed at README.md:260
        # but the comment was added to line 116". Same root
        # cause — diff position sent instead of line number.
        data = {"verdict": "OK", "findings": [
            {"severity": "info", "path": "README.md", "line": 260,
             "body": "At line 260."},
        ]}
        diff = (
            "diff --git a/README.md b/README.md\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/README.md\n"
            "@@ -0,0 +1,260 @@\n"
            + "".join(f"+line {i}\n" for i in range(1, 261))
        )
        _, comments = Agent._format_review_json(
            data, "title", "url", diff_text=diff)
        assert len(comments) == 1
        assert comments[0]["path"] == "README.md"
        assert comments[0]["new_position"] == 260

    def test_line_outside_hunk_anchors_at_reported_line(self):
        # The agent might find a bug in unchanged code at
        # line 500 (not in any hunk). Gitea generates the
        # diff context for unchanged lines too
        # (``GeneratePatchForUnchangedLine``), so we just
        # pass the line number through — no hunk validation.
        data = {"verdict": "OK", "findings": [
            {"severity": "info", "path": "foo.py", "line": 500,
             "body": "Issue in unchanged code."},
        ]}
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+line2\n"
        )
        _, comments = Agent._format_review_json(
            data, "title", "url", diff_text=diff)
        assert len(comments) == 1
        assert comments[0]["new_position"] == 500
