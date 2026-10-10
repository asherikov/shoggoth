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
    def __init__(self):
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
        self.token = (secrets_store.read("gitea/server-token", key="token")
                      or secrets_store.read("gitea/slave-token", key="token")
                      or "")
        if not self.token:
            hostname = urlparse(self.api_url).hostname or ""
            if hostname.startswith("api."):
                log("No Gitea client token configured; relying on api service proxy to inject Authorization")
            else:
                die("Neither gitea-server-token nor gitea-slave-token is readable "
                    "from K8s; grant the script's SA `get` on those Secrets")
        self.domain = params.shoggoth_domain
        self._payload = None

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

    def resolve_comment(self, repo, comment_id):
        self.post(f"repos/{repo}/pulls/comments/{comment_id}/resolve", {})

    def get_pr(self, repo, pr_number):
        return self.get(f"repos/{repo}/pulls/{pr_number}")

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

    def remove_requested_reviewer(self, repo, pr_number, username):
        return self.delete(
            f"repos/{repo}/pulls/{pr_number}/requested_reviewers",
            {"reviewers": [username]})

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

        Mirrors ``remove_requested_reviewer`` (which uses DELETE) for
        the symmetric POST case. Used by ``PrUpdateCommand``'s
        ``/review`` slash command so the user-driven path and the
        existing ``pull_request_review_request`` workflow trigger
        converge on the same Gitea API call.
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
