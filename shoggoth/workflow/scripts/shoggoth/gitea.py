"""Module split out of shoggoth.py; see package __init__."""
import base64
import io
import json
import sys
import zipfile
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from .http import HttpApiClient
from .util import die, log
from .state import params, secrets_store

class Gitea(HttpApiClient):
    def __init__(self, read_tokens=True):
        # Lazy validation: ``Parameters`` doesn't make GITEA_SERVER_URL
        # required because the Redmine-only maintenance steps
        # (``redmine-argo-webhooks``, ``redmine-ldap-sync``) deliberately
        # don't inject it — they never instantiate a ``Gitea`` client.
        # When the program does construct one, the env var must be set
        # or every API call would silently target ``/api/v1`` (a
        # nonsensical URL on the pod's empty API base). Fail loudly
        # at this natural boundary instead.
        if not params.gitea_server_url:
            die("GITEA_SERVER_URL is required for Gitea-using commands")
        self.api_url = params.gitea_server_url + "/api/v1"
        # Read the Gitea token from K8s. The ``server`` token carries
        # the site-admin scopes the maintenance scripts need
        # (``gitea-ldap-sync``, ``slave-access``, ``ssh-key`` etc.);
        # the ``slave`` token is the fallback for contexts that don't
        # need admin scope (``github-mirror-sync``). When the script
        # runs against the web-internal api-proxy (``api.${DOMAIN}``)
        # the proxy replaces whatever Authorization header we send
        # with the real slave token, so an empty token is acceptable
        # in that case — see the lenience block below.
        #
        # ``read_tokens=False`` skips the two ``kubectl get
        # secret`` round-trips. The dispatcher
        # (``shoggoth/workflow/scripts/dispatch.py``) opts into
        # this because (a) its pod runs under
        # ``shoggoth-sandbox-spawner`` SA which is
        # scoped to pods/services CRUD only — no ``get`` on
        # gitea-slave-token / gitea-server-token — so each read
        # would 403 with "secrets 'gitea-server-token' is
        # forbidden", (b) the dispatcher's Gitea usage is
        # limited to payload introspection (read on the
        # in-memory payload, no API calls that need a token) and
        # the sandbox branch's ``post_pr_comment`` call goes
        # through the api-proxy at api.${DOMAIN}/gitea, where
        # web-internal injects the argo-webhook-receiver /
        # gitea-slave bearer token for the dispatcher. Skipping
        # the reads keeps the dispatcher pod logs free of the
        # two 403 lines and avoids the wasted kubectl
        # round-trips on every webhook delivery.
        if read_tokens:
            self.token = (secrets_store.read("gitea/server-token", key="token")
                          or secrets_store.read("gitea/slave-token", key="token")
                          or "")
        else:
            self.token = ""
        if not self.token:
            hostname = urlparse(self.api_url).hostname or ""
            if hostname.startswith("api."):
                log("No Gitea client token configured; relying on api service proxy to inject Authorization")
            else:
                die("Neither gitea-server-token nor gitea-slave-token is readable "
                    "from K8s; grant the script's SA `get` on those Secrets")
        self.domain = params.shoggoth_domain
        # Initialise to ``{}`` (not ``None``) so the ``get_*`` payload
        # accessors (``get_repo_full_name`` / ``get_pr_number`` / etc.)
        # don't ``AttributeError: 'NoneType' object has no attribute
        # 'get'`` if ``load_payload`` is never called. The pre-fix
        # ``None`` initialiser surfaced as a runtime crash in the
        # dispatcher's ``_handle_sandbox`` branch — ``DispatchCommand``
        # intentionally skips ``load_payload`` for ``sandbox`` because
        # the sandbox handler's payload accessors all read keys that
        # may legitimately be absent (and should return a default
        # empty string / None rather than crashing). Maintenance
        # commands that don't need the payload never call
        # ``load_payload`` either, so this default matters for them
        # too.
        self._payload = {}

    def _auth_header(self):
        """Gitea auth scheme: ``Authorization: token <T>``.

        Returned in two roles:
          - merged into ``_headers()`` (base class) for JSON endpoints
          - called directly by endpoints that build their own Request
            (e.g. ``.diff`` which wants accept: application/vnd.gitea.diff)
        """
        if self.token:
            return {"Authorization": f"token {self.token}"}
        return {}

    def _basic_headers(self):
        """HTTP Basic auth for endpoints that don't accept token auth.

        Used for /users/{name}/tokens, which requires basic auth in
        addition to the admin role.
        """
        if not self.token:
            return {"Content-Type": "application/json",
                    "accept": "application/json"}
        creds = base64.b64encode(f"admin:{self.token}".encode()).decode()
        return {"Authorization": f"Basic {creds}",
                "Content-Type": "application/json",
                "accept": "application/json"}

    # ----- payload (workflow context) --------------------------------

    def load_payload(self):
        self._payload = json.loads(params.gitea_payload)

    def get_repo_full_name(self):
        return self._payload.get("repository", {}).get("full_name", "")

    def get_pr_action(self):
        return self._payload.get("action", "")

    def get_pr_url(self):
        return self._payload.get("pull_request", {}).get("html_url", "")

    def get_pr_number(self):
        return self._payload.get("pull_request", {}).get("number")

    def get_pr_branch(self):
        return self._payload.get("pull_request", {}).get("head", {}).get("ref", "")

    def has_review(self):
        # Gitea's PullRequestPayload struct serialises the `Review *ReviewPayload`
        # field WITHOUT `omitempty` (modules/structs/hook.go in gitea/gitea),
        # so pull_request_review_request events (action=review_requested /
        # review_request_removed) arrive here as `"review": null` — the key is
        # present but the value is None. `dict.get("review")` returns None in
        # that case (the default-{} only fires when the key is *absent*); a
        # truthiness check is the predicate the rest of this class already
        # relies on via `_payload.get("review") or {}`. Without this fix,
        # is_review_by_slave_user() crashes with
        # AttributeError: 'NoneType' object has no attribute 'get' before
        # the action=="review_requested" branch (the one actually meant
        # for review-request events) can run.
        return bool(self._payload.get("review"))

    def is_review_by_slave_user(self):
        slave_user = params.shoggoth_slave_user
        # `or {}` (not `.get("review", {})`): on pull_request_review_request
        # events Gitea serialises the `Review *ReviewPayload` field WITHOUT
        # `omitempty`, so `"review": null` arrives — the key is present but
        # the value is None. `.get(key, {})` only falls back when the key
        # is *absent*; the `or {}` coercion handles both. Without it this
        # method raises AttributeError on the very payloads the predicate
        # is meant to safely return False for.
        review = self._payload.get("review") or {}
        if review.get("user", {}).get("login") == slave_user:
            return True
        sender = self._payload.get("sender", {})
        if sender.get("login") == slave_user:
            return True
        return False

    def is_slave_user_reviewer(self):
        slave_user = params.shoggoth_slave_user
        candidates = {u for u in (slave_user,) if u}
        pr = self._payload.get("pull_request", {})
        requested = pr.get("requested_reviewers", [])
        if any(r.get("login") in candidates for r in requested):
            return True
        return False

    def get_ci_conclusion(self):
        return self._payload.get("workflow_run", {}).get("conclusion", "")

    def get_ci_sha(self):
        return self._payload.get("workflow_run", {}).get("head_sha", "")

    def get_ci_branch(self):
        return self._payload.get("workflow_run", {}).get("head_branch", "")

    def get_ci_run_url(self):
        return self._payload.get("workflow_run", {}).get("html_url", "")

    def get_ci_run_id(self):
        return self._payload.get("workflow_run", {}).get("id")

    def get_ci_workflow_name(self):
        return self._payload.get("workflow", {}).get("name", "")

    # get/post/patch/delete/put/status inherited from HttpApiClient.

    # ----- CI logs (workflow) ----------------------------------------

    def get_ci_logs(self, repo, run_id):
        if not run_id:
            return "CI logs unavailable: no run ID in payload"
        url = f"{self.api_url}/repos/{repo}/actions/runs/{run_id}/logs"
        req = Request(url, headers=self._auth_header())
        try:
            with urlopen(req, timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
                data = resp.read()
        except (URLError, HTTPError, OSError) as e:
            return f"CI logs unavailable: {e}"
        try:
            buf = io.BytesIO(data)
            with zipfile.ZipFile(buf) as zf:
                parts = []
                for name in sorted(zf.namelist()):
                    if name.endswith(".txt") or name.endswith(".log"):
                        parts.append(f"=== {name} ===\n{zf.read(name).decode(errors='replace')}")
                return "\n".join(parts) if parts else "CI logs unavailable: no log files in archive"
        except Exception as e:
            return f"CI logs unavailable: failed to parse zip: {e}"

    # ----- repo file lookup (workflow) -------------------------------

    def get_file(self, repo, filepath, ref=None):
        params = {}
        if ref:
            params["ref"] = ref
        data = self.get(f"repos/{repo}/contents/{filepath}", params=params)
        if data is None or "content" not in data:
            return None
        try:
            return base64.b64decode(data["content"]).decode()
        except Exception:
            return None

    # ----- repo helpers (workflow) -----------------------------------

    def find_repo(self, query, limit=50):
        def fetch_page(page, limit):
            data = self.get("repos/search",
                            params={"q": query, "limit": limit, "page": page})
            if data is None:
                return None
            return data.get("data", [])
        repos = self._paginate(fetch_page, limit)

        if len(repos) == 0:
            die(f"no repository found in gitea for project '{query}'")
        if len(repos) > 1:
            names = ", ".join(r.get("full_name", "?") for r in repos)
            die(f"multiple repositories found for project '{query}': {names}")

        clone_url = repos[0].get("ssh_url")
        if not clone_url:
            die(f"repository '{repos[0].get('full_name', '?')}' has no SSH clone URL")
        return clone_url, repos[0].get("full_name")

    def get_default_branch(self, repo_full):
        data = self.get(f"repos/{repo_full}")
        if data is None:
            return "main"
        return data.get("default_branch", "main")

    def get_repo(self, repo_full):
        return self.get(f"repos/{repo_full}")

    def can_push(self, repo_full):
        """True if the authenticated user has push permission on
        ``repo_full``. Reads the Gitea ``permissions`` block in the
        repo metadata (``GET /repos/{owner}/{repo}``); the field is
        present when the caller has any access to the repo and
        names ``pull`` / ``push`` / ``admin`` / ``maintain`` /
        ``triage`` booleans.

        Returns ``None`` (not False) on lookup failure — the caller
        distinguishes "Gitea said no" from "we don't know". The
        ``_preflight_push`` consumer treats ``None`` as
        fail-closed (it would rather over-refuse than commit to a
        destructive local op on a permission state it cannot
        read).
        """
        data = self.get_repo(repo_full)
        if not data:
            return None
        perms = data.get("permissions") or {}
        return bool(perms.get("push"))

    def ensure_pull_requests_enabled(self, repo_full):
        data = self.get(f"repos/{repo_full}")
        if data is None:
            return
        if data.get("has_pull_requests"):
            return
        log(f"ensure_pull_requests_enabled: enabling pulls on {repo_full}")
        result = self.patch(f"repos/{repo_full}", {"has_pull_requests": True})
        if result is None:
            print(f"WARNING: failed to enable pulls on {repo_full}", file=sys.stderr)
            return
        return result

    def get_unresolved_review_comments(self, repo, pr_number):
        slave_user = params.shoggoth_slave_user
        all_reviews = self._paginate(lambda page, limit: self.get(
            f"repos/{repo}/pulls/{pr_number}/reviews",
            params={"page": page, "limit": limit}))

        all_comments = []
        for review in all_reviews:
            review_id = review.get("id")
            if review_id is None:
                continue
            comments = self._paginate(lambda page, limit: self.get(
                f"repos/{repo}/pulls/{pr_number}/reviews/{review_id}/comments",
                params={"page": page, "limit": limit}))
            all_comments.extend(comments)

        unresolved = [c for c in all_comments if not c.get("resolver")]

        trusted_users = set(
            params.shoggoth_trusted_users.split(","))
        trusted_users.discard(slave_user)

        threads = {}
        for c in unresolved:
            path = c.get("path", "unknown")
            line = c.get("position")  # Gitea API uses "position", not "line"
            if line is not None:
                thread_key = (path, line)
            else:
                thread_key = (path, None, c.get("id"))
            threads.setdefault(thread_key, []).append(c)

        result = []
        for thread_key, thread_comments in threads.items():
            path = thread_key[0]
            line = thread_key[1] if len(thread_key) == 2 else None
            has_trusted = any(
                c.get("user", {}).get("login") in trusted_users
                for c in thread_comments)
            if not has_trusted:
                continue

            thread_comments.sort(key=lambda c: c.get("id", 0))
            combined_body = "\n\n".join(
                f"[{c.get('user', {}).get('login', 'unknown')}]: {c.get('body', '')}"
                for c in thread_comments)
            root_comment = thread_comments[0]
            result.append({
                "id": root_comment.get("id"),
                "review_id": root_comment.get("review_id"),
                "path": path,
                "line": line,
                "body": combined_body,
            })

        return result

    def get_pr_review_thread(self, repo, pr_number, path, line):
        """Fetch a SINGLE review-comment thread on a PR,
        identified by its ``(path, line)`` anchor. Returns
        a thread dict with the combined body of all
        participants, or ``None`` if no such thread
        exists (or all its comments are resolved).

        Used by the ``/do`` slash command, which is
        reply-only: the triggering ``/do`` comment must
        be a reply to a specific review comment (so the
        thread anchor is recoverable by id-lookup
        against the per-review comments endpoint — see
        ``find_review_comment_by_id``, since the webhook
        payload itself only carries the reply's ``id``;
        the dispatcher feeds those recovered
        ``path``/``position`` values into this function
        as the anchor). The workflow then asks Gitea
        for JUST that thread and feeds its body to the
        agent — no other comments on the PR are visible
        to the agent. This is the load-bearing change
        for the "``/do`` should never apply to all
        comments in the MR" requirement: a function that
        returns the full PR comment list (even if
        filtered client-side afterwards) makes it
        easy for the agent to be re-introduced to
        the rest of the comments via MCP reads; a
        function that returns ONE thread, by
        contract, cannot leak the rest.

        The Gitea REST API does not expose a
        "list review comments filtered by
        ``(path, line)``" endpoint — the closest
        endpoints are
        ``GET /repos/{owner}/{repo}/pulls/{index}/reviews``
        (list reviews) and
        ``GET /repos/{owner}/{repo}/pulls/{index}/reviews/{id}/comments``
        (list comments in a review). We therefore
        enumerate all reviews, fetch each review's
        comments, and filter to the requested
        ``(path, line)`` pair. The returned thread
        combines every matching comment (root +
        replies, across all reviews — a user can
        reply to their own review comment via a
        follow-up review, which is a separate
        Gitea review with its own comment).

        Returned thread dict shape (matches
        ``get_unresolved_review_comments`` so the
        downstream ``/do`` handler can use either
        source interchangeably):

          - ``id``: the id of the FIRST comment in
            the thread (used by ``resolve_comment``
            to mark the thread resolved)
          - ``review_id``: the review id of the
            first comment
          - ``path``: the path (always equal to the
            ``path`` arg)
          - ``line``: the line (always equal to the
            ``line`` arg)
          - ``body``: combined body, format
            ``[login1]: body1\\n\\n[login2]: body2\\n…``,
            sorted by comment id ascending

        Returns ``None`` if the PR has no review
        comments at the requested ``(path, line)``
        pair, or all such comments are resolved.
        """
        all_reviews = self._paginate(lambda page, limit: self.get(
            f"repos/{repo}/pulls/{pr_number}/reviews",
            params={"page": page, "limit": limit}))

        thread_comments = []
        for review in all_reviews:
            review_id = review.get("id")
            if review_id is None:
                continue
            comments = self._paginate(lambda page, limit: self.get(
                f"repos/{repo}/pulls/{pr_number}/reviews/{review_id}/comments",
                params={"page": page, "limit": limit}))
            for c in comments:
                if c.get("resolver"):
                    continue
                if c.get("path") != path:
                    continue
                if c.get("position") != line:
                    continue
                thread_comments.append(c)

        if not thread_comments:
            return None

        thread_comments.sort(key=lambda c: c.get("id", 0))
        combined_body = "\n\n".join(
            f"[{c.get('user', {}).get('login', 'unknown')}]: {c.get('body', '')}"
            for c in thread_comments)
        root_comment = thread_comments[0]
        return {
            "id": root_comment.get("id"),
            "review_id": root_comment.get("review_id"),
            "path": path,
            "line": line,
            "body": combined_body,
        }

    def find_review_comment_by_id(self, repo, pr_number, comment_id):
        """Return the ``PullReviewComment`` dict for the given
        database id, or ``None`` if no review comment with that
        id exists on the PR.

        Used by the ``/do`` slash command's dispatcher to
        recover the inline anchor ``(path, position,
        pull_request_review_id)`` for the comment that triggered
        the webhook. The webhook's
        ``IssueCommentPayload.Comment`` struct
        (``modules/structs/issue_comment.go:11``) carries only
        the ``id`` — ``path``/``position`` are not serialised —
        because the reply path
        (``routers/api/v1/repo/pull_review.go:266`` →
        ``CreateCodeComment`` → ``notify_service.CreateIssueComment``
        → ``convert.ToAPIComment``) goes through the
        stripped-down ``*api.Comment`` converter
        (``services/convert/issue_comment.go:18``) instead of
        the ``*api.PullReviewComment`` converter used by the
        ``/reviews/{id}/comments`` REST endpoint
        (``services/convert/pull_review.go:103``, which DOES
        carry ``Path``/``LineNum``). The dispatcher therefore
        has to recover the anchor via id lookup, not from the
        payload directly.

        Gitea does not expose a "review comment by id"
        endpoint — the only list-comment route is
        ``GET /repos/{o}/{r}/pulls/{n}/reviews/{id}/comments``,
        which requires the review id in the path. This method
        enumerates reviews and per-review comments looking for
        a match, paying the same N+1 cost as
        ``get_pr_review_thread`` (the follow-up thread fetch
        after this lookup also enumerates). Single-shot match
        by ``comment.id`` is the cheapest reliable signal; the
        alternative — relying on a heuristic like "newest code
        comment in this PR" — would race against pending agent
        runs from a parallel trigger.

        Returns ``None`` when:
          - ``comment_id`` is falsy (no id on the webhook, e.g.
            a deleted-comment payload, or a non-PR-comment
            event that shares the dispatch path),
          - the PR has no reviews (so the enumeration is empty),
          - the id does not appear in any review's comment list
            — typical for a top-level PR comment whose
            ``Type == CommentTypeComment`` and which therefore
            is never returned by ``/reviews/{id}/comments``.
        The caller treats all three cases as "no inline anchor
        recoverable, drop the trigger silently" — matching the
        existing top-level-comment rejection policy.
        """
        if not comment_id:
            return None
        all_reviews = self._paginate(lambda page, limit: self.get(
            f"repos/{repo}/pulls/{pr_number}/reviews",
            params={"page": page, "limit": limit}))
        for review in all_reviews:
            review_id = review.get("id")
            if review_id is None:
                continue
            comments = self._paginate(lambda page, limit: self.get(
                f"repos/{repo}/pulls/{pr_number}/reviews/{review_id}/comments",
                params={"page": page, "limit": limit}))
            for c in comments:
                if c.get("id") == comment_id:
                    return c
        return None

    def resolve_comment(self, repo, comment_id):
        self.post(f"repos/{repo}/pulls/comments/{comment_id}/resolve", {})

    def get_pr(self, repo, pr_number):
        return self.get(f"repos/{repo}/pulls/{pr_number}")

    def list_pr_reviews(self, repo, pr_number):
        """Fetch every review on the PR, including pending
        drafts.

        Hits ``GET /repos/{repo}/pulls/{n}/reviews``
        (handler ``ListPullReviews`` at
        ``routers/api/v1/repo/pull_review.go:27``,
        paginated by the same ``_paginate`` pattern used
        by the other list endpoints in this class — the
        route returns the full list in one page on Gitea,
        but paging future-proofs the call against a
        higher review count).

        Each list item carries at least:
          - ``id`` (int64)
          - ``user`` with ``login``
          - ``state`` — one of the ``ReviewStateType``
            constants defined in
            ``modules/structs/pull_review.go``: ``PENDING``
            (a draft the user hasn't submitted; does NOT
            count as "review submitted"), ``COMMENT``,
            ``APPROVED``, ``REQUEST_CHANGES``,
            ``REQUEST_REVIEW`` (a re-request; Gitea uses
            this state for a reviewer who's been
            re-requested after their previous submission,
            so it does NOT indicate a fresh review is
            available — see the dispatch in
            ``commands/pr_update.py:execute``)
          - ``commit_id`` (the head SHA the review was
            posted against; only present for submitted
            reviews — pending drafts omit it)
          - ``submitted_at``

        The dispatcher uses this list to gate
        ``_pr_review`` on the slave user's review
        status: "review requested" (in
        ``requested_reviewers``) does NOT trigger a
        re-review when the slave already has a
        ``COMMENT`` / ``APPROVED`` /
        ``REQUEST_CHANGES`` review on the current head
        SHA. ``REQUEST_REVIEW`` and ``PENDING`` are
        explicitly treated as not-submitted so a
        pending draft or a previously-dismissed re-
        request leaves the door open for a fresh review.

        Returns ``[]`` on transport failure (the
        caller treats that as "no submitted review" —
        safe because the dispatch already gates on
        ``_pr_review``'s own state check after this
        check passes; we err on the side of running
        again rather than skipping a real first
        request).
        """
        reviews = self._paginate(lambda page, limit: self.get(
            f"repos/{repo}/pulls/{pr_number}/reviews",
            params={"page": page, "limit": limit}))
        return reviews if reviews is not None else []

    # States that count as "review submitted" per the
    # ``ReviewStateType`` enum in Gitea's
    # ``modules/structs/pull_review.go``. ``PENDING`` and
    # ``REQUEST_REVIEW`` are intentionally excluded:
    #   - ``PENDING`` is a draft the user hasn't published.
    #   - ``REQUEST_REVIEW`` is the placeholder row Gitea
    #     creates when a reviewer is requested; a
    #     re-request can also yield a fresh ``REQUEST_REVIEW``
    #     row even after a previous submission, so trusting
    #     it would incorrectly skip a re-review of a new
    #     head.
    _SUBMITTED_REVIEW_STATES = frozenset(
        {"COMMENT", "APPROVED", "REQUEST_CHANGES"})

    def slave_has_submitted_review_on_head(self, slave_user, head_sha):
        """Return ``True`` iff ``slave_user`` has at least
        one submitted review on the PR's current head
        SHA.

        "Submitted" is the union of the three states that
        Gitea's ``ReviewStateType`` enum marks as final
        reviewer output (``COMMENT`` / ``APPROVED`` /
        ``REQUEST_CHANGES``). The check is additionally
        scoped to ``commit_id == head_sha`` so that a
        previously-submitted review against an older head
        does NOT count as covering the current head — a
        push between the submission and the re-request
        drops the old review out of scope, even though
        the row still sits in the table.

        Used by ``PrUpdateCommand.execute`` to gate the
        ``_pr_review`` dispatch: a re-request webhook
        (``action == "review_requested"``) fires every
        time a reviewer is added to ``requested_reviewers``,
        including re-requests after the slave already
        submitted. Without this check the slave would
        re-review the same head on every push of a
        "request changes" button; with it, the slave
        only re-reviews when the head has actually moved
        past the previously-submitted commit, which is
        the F2 requirement.

        Returns ``False`` when:
          - ``list_pr_reviews`` returned ``[]`` (e.g.
            the API call failed — the caller treats that
            as "no submitted review" and runs the review
            once, which is the safe default for a first
            request),
          - the slave has no rows at all on this PR,
          - every slave row is a ``PENDING`` /
            ``REQUEST_REVIEW`` placeholder,
          - every submitted row targets a different head
            SHA.
        """
        if not slave_user or not head_sha:
            return False
        pr = self._payload.get("pull_request", {}) or {}
        repo = pr.get("base", {}).get("repo", {}).get("full_name", "")
        pr_number = pr.get("number")
        if not repo or not pr_number:
            return False
        reviews = self.list_pr_reviews(repo, pr_number) or []
        for r in reviews:
            if (r.get("state") in self._SUBMITTED_REVIEW_STATES
                    and r.get("commit_id") == head_sha
                    and (r.get("user") or {}).get("login") == slave_user):
                return True
        return False

    def get_pr_files(self, repo, pr_number):
        return self._paginate(lambda page, limit: self.get(
            f"repos/{repo}/pulls/{pr_number}/files",
            params={"page": page, "limit": limit}))

    def get_pr_diff_text(self, repo, pr_number):
        """Fetch the unified diff rendered by Gitea at .diff endpoint.
        Returns the diff as a string, or None on failure.
        """
        url = f"{self.api_url}/repos/{repo}/pulls/{pr_number}.diff"
        req = Request(url, headers={**self._auth_header(),
                                     "accept": "application/vnd.gitea.diff"})
        try:
            with urlopen(req, timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
                return resp.read().decode()
        except (HTTPError, URLError, OSError) as e:
            log(f"get_pr_diff_text: failed to fetch {url}: {e}")
            return None

    def post_pr_review(self, repo, pr_number, body, event="COMMENT"):
        return self.post(f"repos/{repo}/pulls/{pr_number}/reviews", {
            "body": body,
            "event": event,
        })

    def post_pr_review_with_inline(self, repo, pr_number, body, event,
                                    comments, commit_id):
        """Post a single review carrying the summary body plus a list
        of inline comments anchored to diff positions.

        ``comments`` is a list of dicts, each ``{path, body,
        new_position}`` (Gitea also accepts ``old_position`` or
        ``position``; the format helper here only emits
        ``new_position`` because the agent's ``line`` is the new-
        file line number). ``commit_id`` is the head SHA the
        comments anchor against; without it Gitea 422s on the
        first inline comment.

        Returns the parsed JSON response on success, or
        ``None`` on any HTTP / network failure (handled by
        ``http_post_json``). Gitea rejects the whole review
        when any inline comment references a non-existent
        ``(path, position)``, so a failure here means every
        inline comment in the batch was rejected — the caller
        falls back to ``post_pr_review_chunked`` (body only)
        and the inline comments are lost. A per-comment
        retry loop is a future improvement; today the helper
        is a pass-through.
        """
        payload = {
            "body": body,
            "event": event,
            "commit_id": commit_id,
            "comments": comments,
        }
        return self.post(f"repos/{repo}/pulls/{pr_number}/reviews",
                          payload)

    def get_pr_review_comments(self, repo, pr_number, review_id):
        """Fetch all inline comments for a review.

        Calls ``GET /repos/{repo}/pulls/{n}/reviews/{id}/comments``
        (route registered in
        ``routers/api/v1/repo/pull_review.go:158``, handler
        ``GetPullReviewComments``). Returns a list of
        comment dicts, each with at least ``id``, ``path``,
        ``position`` (= the file line number, mapped from
        ``LineNum`` in the response — see the
        ``PullReviewComment`` struct), and ``html_url``
        (format: ``{issue_url}#issuecomment-{id}``).

        Used by the review follow-up to build a clickable
        index of inline comments posted in a review — the
        review body has "see inline comment" placeholders
        (the review API doesn't expose per-comment URLs
        in the POST response, and Gitea doesn't support
        editing a posted review body), so the follow-up
        comment provides the actual links.

        Returns ``None`` on HTTP / network failure.
        """
        return self.get(
            f"repos/{repo}/pulls/{pr_number}/reviews/{review_id}/comments")

    def post_pr_review_chunked(self, repo, pr_number, body, event="COMMENT",
                               chunk_size=60000):
        if len(body) <= chunk_size:
            return self.post_pr_review(repo, pr_number, body, event)
        parts = []
        remaining = body
        while remaining:
            if len(remaining) <= chunk_size:
                parts.append(remaining)
                break
            cut = remaining.rfind("\n\n", 0, chunk_size)
            if cut == -1:
                cut = remaining.rfind("\n", 0, chunk_size)
            if cut == -1:
                cut = chunk_size
            parts.append(remaining[:cut])
            remaining = remaining[cut:].lstrip("\n")
        total = len(parts)
        for i, part in enumerate(parts):
            prefix = f"**Part {i+1}/{total}**\n\n" if total > 1 else ""
            evt = event if i == 0 else "COMMENT"
            log(f"pr-review: posting review part {i+1}/{total} "
                f"(length={len(part)})")
            result = self.post_pr_review(repo, pr_number, prefix + part, evt)
            if result is None:
                return None
        return True

    # ----- slash command helpers (workflow) --------------------------

    def request_reviewer(self, repo, pr_number, username):
        """Request a review from ``username`` on the given PR.

        Used by ``PrUpdateCommand``'s ``/review`` slash command
        so the user-driven path and the existing
        ``pull_request_review_request`` workflow trigger
        converge on the same Gitea API call. The dispatch
        path no longer uses a symmetric DELETE to clear the
        request — re-requests are now handled by gating
        ``_pr_review`` on the slave's existing review state
        (see ``slave_has_submitted_review_on_head``).
        """
        return self.post(
            f"repos/{repo}/pulls/{pr_number}/requested_reviewers",
            {"reviewers": [username]})

    def post_pr_comment(self, repo, pr_number, body):
        """Post an issue-style comment on a PR.

        Distinct from ``post_pr_review`` (which posts a review with a
        verdict on the PR's review tab). Issue comments are top-level
        timeline comments used by the ``/help`` / ``/review`` /
        ``/squash`` / ``/rebase`` slash-command responses.
        """
        return self.post(f"repos/{repo}/issues/{pr_number}/comments",
                          {"body": body})

    def get_pr_comment_body(self):
        """Return the body of the comment in the current PR payload.

        Returns an empty string when the payload has no comment field
        (e.g. a non-comment PR event). Caller is responsible for
        stripping whitespace.
        """
        comment = self._payload.get("comment") or {}
        return comment.get("body") or ""

    def get_pr_commenter_login(self):
        """Return the login of the user who submitted the comment
        that triggered the workflow event.

        Reads ``payload.sender.login`` — Gitea populates ``sender``
        for every ``pull_request_comment`` / ``pull_request_review``
        / ``issues`` / ``pull_request`` event (and the
        ``command-handler`` bindings for ``/sandbox``, ``/help``,
        ``/do``, ``/review``, ``/squash``, ``/rebase`` all carry a
        comment field whose ``action == "created"`` was posted by a
        human user). Returns the empty string when the payload has
        no ``sender`` (e.g. a synthetic / non-user event); the
        caller is expected to treat empty as "no attribution
        available" and skip the prefix.
        """
        return self._payload.get("sender", {}).get("login", "")

    def post_pr_comment_attributed(self, repo, pr_number, body):
        """Post a slash-command response PR comment that
        references the commenter who triggered the command.

        Prefixes the body with ``@user,`` so the PR-thread
        reader (and the Gitea notification fan-out, which
        fires on the ``@`` mention) can tell at a glance
        which slash command invocation the bot is
        responding to. Important for ``/sandbox`` in
        particular — multiple users can post ``/sandbox`` on
        the same PR over its lifetime, and each comment-
        thread response is otherwise indistinguishable
        without the attribution.

        Used by ``DispatchCommand._handle_sandbox`` (the
        ``/sandbox`` command, where each workflow run maps to
        one commenter's request) and by every
        ``_slash_<command>`` handler in
        ``shoggoth/commands/pr_update.py`` via
        ``_post_slash_response`` (the ``/help`` / ``/do`` /
        ``/review`` / ``/squash`` / ``/rebase`` commands).
        Non-slash-command callers (e.g. the pr-review follow-up
        summary at ``pr_update.py:619``) keep the plain
        ``post_pr_comment`` so the bot's review summary isn't
        misattributed to whichever user last commented.

        Empty ``sender`` (synthetic event, unrecognised
        payload) falls through to a plain ``post_pr_comment`` —
        the prefix is suppressed rather than emitting a stray
        ``@``.

        Returns the ``post_pr_comment`` result unchanged (the
        underlying comment JSON or ``None`` on failure).
        """
        commenter = self.get_pr_commenter_login()
        if not commenter:
            return self.post_pr_comment(repo, pr_number, body)
        attributed = f"@{commenter}, {body}"
        return self.post_pr_comment(repo, pr_number, attributed)

    # ----- orgs / org-hooks (maintenance) ----------------------------

    def list_orgs(self):
        return self._paginate(lambda page, limit: self.get(
            "orgs", params={"page": page, "limit": limit}))

    def list_org_repos(self, org):
        return self._paginate(lambda page, limit: self.get(
            f"orgs/{org}/repos", params={"page": page, "limit": limit}))

    def list_org_hooks(self, org):
        return self._paginate(lambda page, limit: self.get(
            f"orgs/{org}/hooks", params={"page": page, "limit": limit}))

    def delete_org_hook(self, org, hook_id):
        return self.delete(f"orgs/{org}/hooks/{hook_id}")

    def create_org_hook(self, org, url, events, secret=None):
        config = {"content_type": "json", "url": url}
        if secret:
            config["secret"] = secret
        return self.post(f"orgs/{org}/hooks", {
            "active": True,
            "config": config,
            "events": events,
            "type": "gitea",
        })

    def create_org(self, username, full_name=None):
        return self.post("orgs", {"username": username, "full_name": full_name or username})

    def list_branches(self, repo_full):
        return self._paginate(lambda page, limit: self.get(
            f"repos/{repo_full}/branches", params={"page": page, "limit": limit}))

    def list_tags(self, repo_full):
        return self._paginate(lambda page, limit: self.get(
            f"repos/{repo_full}/tags", params={"page": page, "limit": limit}))

    # ----- branch protection (maintenance) ---------------------------
    #
    # Gitea stores per-branch protection rules in
    # ``/repos/{owner}/{repo}/branch_protections``. The relevant
    # fields for the slave's force-push path are ``enable_push``
    # (whether push is restricted to the whitelist), ``enable_force_push``
    # (whether force-push is allowed for those who can push),
    # ``whitelist_users`` (the explicit allow-list of users when
    # ``enable_push`` is true) and ``whitelist_teams`` (same, for
    # teams). The list endpoint paginates, the get/patch endpoints
    # are keyed on the protected branch name and *not* on a numeric
    # id — the path is ``/branch_protections/{name}`` with ``name``
    # URL-encoded. PATCH on an existing rule is a partial update;
    # any field omitted from the body is left unchanged.
    #
    # The ``slave-access`` step calls these to ensure the slave can
    # force-push to any protected branch in any mirrored repo: it
    # sets ``enable_force_push=true`` (so a ``write`` user can
    # force-push when ``enable_push`` is false) and appends the
    # slave to ``whitelist_users`` (so the slave can force-push
    # when ``enable_push`` is true and the whitelist is the only
    # path to push at all).

    def list_branch_protections(self, repo_full):
        return self._paginate(lambda page, limit: self.get(
            f"repos/{repo_full}/branch_protections",
            params={"page": page, "limit": limit}))

    def get_branch_protection(self, repo_full, branch_name):
        return self.get(
            f"repos/{repo_full}/branch_protections/{branch_name}")

    def update_branch_protection(self, repo_full, branch_name, **fields):
        """PATCH ``/repos/{owner}/{repo}/branch_protections/{branch}``.

        Accepts any subset of the Gitea protection fields. Pass
        ``whitelist_users=[...]`` to replace the list (Gitea
        treats the field as a full set on PATCH, not a diff); the
        caller is responsible for computing the desired end state
        and passing the merged list in one call.
        """
        return self.patch(
            f"repos/{repo_full}/branch_protections/{branch_name}",
            fields)

    def is_org_member(self, org, username):
        return self.status(f"orgs/{org}/members/{username}") == 204

    def list_org_teams(self, org):
        return self._paginate(lambda page, limit: self.get(
            f"orgs/{org}/teams", params={"page": page, "limit": limit}))

    def add_org_member(self, org, username):
        teams = self.list_org_teams(org)
        if not teams:
            print(f"WARNING: No teams found for org '{org}'", file=sys.stderr)
            return False
        ok = True
        for team in teams:
            permission = team.get("permission", "")
            if permission in ("owner", "admin"):
                continue
            team_id = team.get("id")
            if team_id is None:
                continue
            if not self.put(f"teams/{team_id}/members/{username}"):
                ok = False
        return ok

    def find_team_by_name(self, org, name):
        """Return the team dict for ``org``/``name`` or ``None``.

        Uses ``GET /orgs/{org}/teams/search?q={name}`` and matches on
        the exact lowercased team name. Gitea's search endpoint
        returns up to 10 teams whose name, description, or lower_name
        contain the query; the case-insensitive equality check is
        the only safe way to avoid a "shoggoth-slave-push" prefix
        matching "shoggoth-slave-push-archive". Returns ``None``
        on 404, on a non-200 status, on a transport failure, or when
        the search returns no team with that exact name — callers
        treat every ``None`` outcome identically and fall through to
        the create path. Distinguishing "found a team" from
        "anything else" is the caller's only contract here.

        The response is the ``SearchTeam`` envelope
        ``{"data": [...], "ok": true}`` (Gitea wraps the
        /orgs/:org/teams/search result the same way the users
        search endpoint does — without unwrapping this, a 200 OK
        with a real team is misread as "no team found" and the
        422-recovery path in SlaveAccess._ensure_push_teams
        fails to recover from a concurrent create).
        """
        data = self.get(f"orgs/{org}/teams/search",
                        params={"q": name, "limit": 10})
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            data = data["data"]
        if not isinstance(data, list):
            return None
        want = name.lower()
        for team in data:
            if (team.get("name", "").lower() == want
                    or team.get("lower_name") == want):
                return team
        return None

    def create_team(self, org, name, permission="write",
                    includes_all_repositories=True,
                    units=None):
        """Create ``name`` in ``org`` with the given permission and
        team-repo scope. Returns the new team dict on 201, ``None``
        on any other outcome (the 422 "team already exists" is the
        expected outcome on a re-run and the caller must check
        ``find_team_by_name`` to recover).

        ``includes_all_repositories=True`` (the default) makes the
        team auto-grant access to every existing and future repo in
        the org, so the script does not have to add the team to each
        repo individually. ``NewTeam`` walks the org repos and
        calls ``addRepositoryToTeam`` (which calls
        ``RecalculateTeamAccesses``) for each one, so the
        ``Access`` table is populated for every existing repo at
        create-time. Future repos picked up by the
        ``includes_all_repositories`` watch are not visible here,
        but the in-org mirror-sync adds them to the team's
        ``team_repo`` rows anyway and triggers a recalc through
        the same path.
        """
        payload = {
            "name": name,
            "permission": permission,
            "includes_all_repositories": includes_all_repositories,
            "units": units or ["repo.code", "repo.issues",
                               "repo.pulls", "repo.releases"],
        }
        return self.post(f"orgs/{org}/teams", payload)

    def update_team(self, team_id, **fields):
        """PATCH ``/teams/{id}`` with ``fields`` (permission, units,
        includes_all_repositories, etc.). Returns the parsed
        response body on 200, ``None`` on any other outcome. Used
        to upgrade an existing team to ``includes_all_repositories``
        if it was created without that flag.
        """
        return self.patch(f"teams/{team_id}", fields)

    def migrate_repo(self, clone_addr, repo_name, repo_owner, description=""):
        return self.post("repos/migrate", {
            "clone_addr": clone_addr,
            "description": description,
            "issues": False,
            "labels": False,
            "milestones": False,
            "mirror": False,
            "private": False,
            "pull_requests": False,
            "releases": False,
            "repo_name": repo_name,
            "repo_owner": repo_owner,
            "service": "git",
            "wiki": False,
        })

    def add_repo_collaborator(self, repo_full, username, permission="write"):
        return self.put(f"repos/{repo_full}/collaborators/{username}",
                        payload={"permission": permission})

    def is_repo_collaborator(self, repo_full, username):
        code = self.status(f"repos/{repo_full}/collaborators/{username}")
        return code == 204

    def delete_repo_collaborator(self, repo_full, username):
        """``DELETE /repos/{owner}/{repo}/collaborators/{username}``.

        Returns ``True`` on 204 (the user was a collaborator and
        is now gone) and ``True`` on 404 (the user was not a
        collaborator — idempotent). ``False`` on any other
        transport / status error. Used by the maintenance pass
        to force a ``RecalculateUserAccess`` on the next add
        when the ``Access`` table is suspected stale (e.g.
        after a team removal that did not propagate).
        """
        # ``_send_status`` returns ``True`` on 2xx and ``None``
        # (via ``_http_warn``) on HTTPError/URLError/OSError.
        # Gitea returns 204 on a successful delete and 404 when
        # the user was not a collaborator; both are acceptable
        # outcomes for the force-refresh path. We can't tell 204
        # and 404 apart with the boolean ``_send_status`` returns
        # (it collapses 2xx to True and 4xx/5xx to None), so a
        # more nuanced check is not possible without a status-code
        # variant. The boolean is enough for the caller: the
        # "force-refresh" semantic only needs the request to
        # reach Gitea and either delete the collaborator or
        # confirm absence.
        return bool(self._send_status(
            "DELETE", f"repos/{repo_full}/collaborators/{username}"))

    def get_repo_collaborator_permission(self, repo_full, username):
        """Return ``username``'s effective permission on ``repo_full``.

        Calls ``GET /repos/{owner}/{repo}/collaborators/{collaborator}/permission``
        which returns the *effective* role — the maximum of the direct
        collaborator permission and any team-based permission the user
        inherits through org membership. Gitea documents the role as
        one of ``read`` / ``write`` / ``admin`` / ``owner``.

        Returns the role string when the user has any access; ``None``
        when the user is not a collaborator (404) or the lookup failed
        (HTTP/URL/OS error). Callers that need to distinguish "not a
        collaborator" from "lookup failed" should fall through to the
        ``add_repo_collaborator`` path: the PUT endpoint is idempotent
        and accepts an existing collaborator, so retrying with a write
        payload upgrades their permission on success and surfaces the
        underlying transport error on failure.
        """
        url, hdrs = self._resolve(
            f"repos/{repo_full}/collaborators/{username}/permission")
        try:
            with urlopen(Request(url, headers=hdrs),
                         timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
                body = resp.read().decode()
                log(f"GET {url} -> {resp.status}")
                if not body:
                    return None
                data = json.loads(body)
        except HTTPError as e:
            if e.code == 404:
                log(f"GET {url} -> 404 (not a collaborator)")
                return None
            self._http_warn(f"GET {url}", e)
            return None
        except (URLError, OSError, json.JSONDecodeError) as e:
            self._http_warn(f"GET {url}", e)
            return None
        role = data.get("role") if isinstance(data, dict) else None
        return role if isinstance(role, str) else None

    # ----- user tokens / SSH keys / admin (maintenance) --------------

    def list_user_tokens(self, username):
        return self._paginate(lambda page, limit: self.get(
            f"users/{username}/tokens",
            headers=self._basic_headers(),
            params={"page": page, "limit": limit}))

    def create_user_token(self, username, name, scopes):
        return self.post(
            f"users/{username}/tokens",
            {"name": name, "scopes": scopes},
            headers=self._basic_headers())

    def delete_user_token(self, username, token_id):
        return self.delete(
            f"users/{username}/tokens/{token_id}",
            headers=self._basic_headers())

    def list_user_ssh_keys(self, username):
        return self._paginate(lambda page, limit: self.get(
            f"users/{username}/keys",
            params={"page": page, "limit": limit}))

    def create_user_ssh_key(self, username, title, key):
        url = f"{self.api_url}/admin/users/{username}/keys"
        status, body, parsed = self.http_post_json_with_status(
            url,
            {"title": title, "key": key},
            headers={"Authorization": f"token {self.token}"})
        if 200 <= status < 300:
            return True, status, body, parsed
        print(f"WARNING: HTTP POST {url} failed: {status}: {body}",
              file=sys.stderr)
        return False, status, body, parsed

    def list_admin_keys(self):
        return self._paginate(lambda page, limit: self.get(
            "admin/keys", params={"page": page, "limit": limit}))

    def delete_admin_key(self, key_id):
        return self.delete(f"admin/keys/{key_id}")

    def list_admin_users(self):
        first = self.get("admin/users", params={"page": 1, "limit": 1})
        if not isinstance(first, list):
            return None
        if len(first) == 0:
            return []
        return self._paginate(lambda page, limit: self.get(
            "admin/users", params={"page": page, "limit": limit}))
