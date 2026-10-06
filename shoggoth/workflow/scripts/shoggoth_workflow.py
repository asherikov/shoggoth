#!/usr/bin/env python3
import argparse
import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from urllib.parse import urlparse, urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

HTTP_TIMEOUT = 30
AGENT_TIMEOUT = 1200
VERBOSE = False

SECRET_ENV_KEYS = frozenset([
    "GITEA_ADMIN_TOKEN",
    "SHOGGOTH_VAULT_TOKEN", "REDMINE_TOKEN",
    "OPENBAO_ADDR", "SHOGGOTH_AI_DEFAULT_TOKEN",
    "REDMINE_WEBHOOK_SECRET",
])

ANTI_LEAK_DIRECTIVE = (
    "Do not emit thinking markup (e.g., <thinking>, <thought>, or any "
    "XML/HTML tags that resemble reasoning traces). Output only the final "
    "answer.\n\n"
)


TOOL_PROFILES = {
    "full": [],
    "pr-review": [
        "edit",
        "write_file",
        "notebook_edit",
    ],
    "pr-comment": [],
}

QWEN_DENY_PATTERNS = [
    "Bash(git commit*)",
    "Bash(git commit)",
    "Bash(git push*)",
    "Bash(git push)",
    "Bash(git tag*)",
    "Bash(git tag)",
    "Bash(git -C * commit*)",
    "Bash(git -C * push*)",
    "Bash(git -C * tag*)",
    "Bash(wsh *commit*)",
    "Bash(wsh *push*)",
    "Bash(wsh *tag*)",
    "Bash(wshandler commit*)",
    "Bash(wshandler push*)",
    "Bash(wshandler tag*)",
    "Bash(wshandler tag)",
]

TOOL_PROFILE_BY_SESSION = {
    "task": "full",
    "ci-failure": "full",
    "pr-review": "pr-review",
    "pr-comment": "pr-comment",
}


def _filter_qwen_env():
    return {k: v for k, v in os.environ.items() if k not in SECRET_ENV_KEYS}


def _format_review_json(review_data, pr_title, pr_url):
    """Render a qwen review run --json result as a Markdown review body
    suitable for posting as a single Gitea PR review.
    """
    if not isinstance(review_data, dict):
        return ""
    findings = review_data.get("findings") or []
    verdict = review_data.get("verdict") or ""
    summary = (review_data.get("summary")
               or review_data.get("summary_md")
               or review_data.get("body") or "")
    parts = [f"## Code review: {pr_title}", f"PR: {pr_url}", ""]
    if verdict:
        parts.append(f"**Verdict:** {verdict}")
        parts.append("")
    if summary and not findings:
        parts.append(summary.strip())
        parts.append("")
    if findings:
        parts.append("### Findings")
        parts.append("")
        for f in findings:
            sev = (f.get("severity") or "info").upper()
            path = f.get("path") or "?"
            line = f.get("line") or f.get("start_line")
            loc = f"{path}:{line}" if line else path
            parts.append(f"- **[{sev}]** `{loc}`")
            body = (f.get("body") or f.get("message") or "").strip()
            if body:
                for bl in body.splitlines():
                    parts.append(f"  {bl}")
            parts.append("")
    elif not summary:
        parts.append("_No issues found._")
    return "\n".join(parts)


def _extract_review_json(text):
    """Find and parse a JSON review object from agent assistant text.

    Looks for the first ```json ... ``` fenced block (or a bare JSON object
    ending at the END_OF_REVIEW marker), validates it has the expected
    shape, and returns the parsed dict. Returns None if nothing usable
    is found.
    """
    if not text:
        return None
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        candidate = m.group(1)
    else:
        end = text.find("END_OF_REVIEW")
        if end != -1:
            head = text[:end]
        else:
            head = text
        depth = 0
        start = -1
        for i, ch in enumerate(head):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start != -1:
                    candidate = head[start:i + 1]
                    break
        else:
            return None
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if "verdict" not in data and "findings" not in data and "summary" not in data:
        return None
    return data


def log(msg):
    if VERBOSE:
        print(f"[shoggoth] {msg}", file=sys.stderr, flush=True)


def run(cmd, **kwargs):
    kwargs.setdefault("check", True)
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    log(f"run: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, **kwargs)
    except subprocess.TimeoutExpired as e:
        log(f"run: TIMEOUT after {e.timeout}s: {' '.join(cmd)}")
        if e.stderr:
            log(f"run: timeout stderr: {e.stderr.decode(errors='replace')[:500] if isinstance(e.stderr, bytes) else str(e.stderr)[:500]}")
        return subprocess.CompletedProcess(cmd, returncode=124,
                                             stdout=e.stdout or "",
                                             stderr=e.stderr or "")
    if VERBOSE and result.stderr:
        log(f"stderr: {result.stderr.strip()[:500]}")
    return result


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


# The git-cred-bootstrap sidecar (shoggoth/k3s/git-cred-bootstrap.yaml)
# populates /shoggoth/git-cred in parallel with this script: the kubelet
# starts both containers at the same time because shoggoth's running k3s
# build (v1.34.3+k3s3) does not expose K8s 1.28+ `startOrder` in its
# apiserver OpenAPI v2 (and we removed it from the manifests because
# `kubectl apply` rejected it with "field not declared in schema"). The
# sidecar eventually runs ssh-keyscan + ssh-agent + git-credential-cache
# --daemon as uid 1000 and writes known_hosts, ssh_auth_sock, and
# git_credential_sock into /shoggoth/git-cred. Until those three outputs
# appear, any `git clone` / `git push` we run will fail (ssh-add
# refuses to talk to a missing agent socket; git credential-cache
# refuses to talk to a missing cache socket; ssh-keyscan produces an
# empty known_hosts and StrictHostKeyChecking then rejects the host).
# Wait on the sidecar's single readiness flag — not on per-file
# existence. Mirrors shoggoth_maintenance.py:_wait_for_git_cred so
# both scripts gate on the same signal: shoggoth/k3s/git-cred-
# bootstrap.yaml touches /shoggoth/git-cred/ready 3 s after both
# ssh-agent has loaded an identity and the credential-cache daemon
# has accepted its first `store`. Per-file polling was racy —
# ssh-keyscan creates known_hosts with the > redirect BEFORE writing
# key data, so an empty known_hosts is a real file that passes
# os.stat — and the AF_UNIX sockets exist before their
# listeners are bound.
GIT_CRED_READY_PATH = "/shoggoth/git-cred/ready"
GIT_CRED_READY_TIMEOUT = 30
GIT_CRED_POLL_INTERVAL = 0.5


def _wait_for_git_cred(path):
    deadline = time.time() + GIT_CRED_READY_TIMEOUT
    while time.time() < deadline:
        try:
            os.stat(path)
            return True
        except (FileNotFoundError, PermissionError):
            time.sleep(GIT_CRED_POLL_INTERVAL)
    return False


def _wait_for_git_cred_ready():
    """Wait on the sidecar's single readiness flag. On timeout, fail
    with a diagnostic so a run without shell access to the pod can
    self-diagnose."""
    log(f"waiting for git-cred sidecar to touch {GIT_CRED_READY_PATH}")
    if not _wait_for_git_cred(GIT_CRED_READY_PATH):
        die(
            f"git-cred sidecar did not touch {GIT_CRED_READY_PATH} within "
            f"{GIT_CRED_READY_TIMEOUT}s "
            "(check 'kubectl logs <pod> -c git-cred' for the FATAL line; "
            "verify the pod has an initContainer named init-git-cred-perms "
            "and a sidecar named git-cred)"
        )
    log("git-cred ready")


WSHANDLER_BIN = "/ccws/ccws/tools/bin/wshandler"


_QUOTE_TRANSLATION = str.maketrans({
    "'": "\u2019",
    '"': "\u201d",
    "`": "\u2018",
})


def sanitize_commit_msg(msg):
    """Strip characters that would break the wshandler `commit` command.

    wshandler invokes git as `git commit -a -m '${msg}'` (single-quoted bash
    string). An unescaped `'` in msg terminates the string early, producing
    `sh: N: Syntax error: Unterminated quoted string`. Replace ASCII quotes
    with typographic equivalents so the message remains readable.
    """
    if msg is None:
        return msg
    return msg.translate(_QUOTE_TRANSLATION)


def wsh(repo_dir, args, log_output=False, **kwargs):
    cmd = [WSHANDLER_BIN, "-r", repo_dir] + args
    result = run(cmd, **kwargs)
    if log_output:
        if result.stdout:
            print(result.stdout, file=sys.stderr, flush=True)
        if result.stderr:
            print(result.stderr, file=sys.stderr, flush=True)
    return result


def wsh_status(repo_dir, quiet=False):
    args = ["status"]
    if quiet:
        args = ["-q"] + args
    return wsh(repo_dir, args, log_output=True)


def normalize_for_branch(text):
    text = text.lower()
    text = re.sub(r"[^a-z0-9]", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def http_get(url, headers=None, params=None):
    if params:
        url = f"{url}?{urlencode(params)}"
    log(f"GET {url}")
    req = Request(url, headers=headers or {})
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
            log(f"GET {url} -> {resp.status}")
            return data
    except HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        print(f"WARNING: HTTP GET {url} failed: {e.code} {e.reason}: {body}", file=sys.stderr)
        return None
    except (URLError, OSError) as e:
        print(f"WARNING: HTTP GET {url} failed: {e}", file=sys.stderr)
        return None
    except json.JSONDecodeError as e:
        print(f"WARNING: HTTP GET {url} returned invalid JSON: {e}", file=sys.stderr)
        return None


def http_post_json(url, payload, headers=None, quiet=False):
    data = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    if not quiet:
        log(f"POST {url}")
    req = Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            result = json.loads(resp.read().decode())
            if not quiet:
                log(f"POST {url} -> {resp.status}")
            return result
    except HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        print(f"WARNING: HTTP POST {url} failed: {e.code} {e.reason}: {body}", file=sys.stderr)
        return None
    except (URLError, OSError) as e:
        print(f"WARNING: HTTP POST {url} failed: {e}", file=sys.stderr)
        return None
    except json.JSONDecodeError as e:
        print(f"WARNING: HTTP POST {url} returned invalid JSON: {e}", file=sys.stderr)
        return None


def http_patch_json(url, payload, headers=None):
    data = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    log(f"PATCH {url}")
    req = Request(url, data=data, headers=hdrs, method="PATCH")
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode()
            log(f"PATCH {url} -> {resp.status}")
            if not body:
                return {}
            return json.loads(body)
    except HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        print(f"WARNING: HTTP PATCH {url} failed: {e.code} {e.reason}: {body}",
              file=sys.stderr)
        raise
    except (URLError, OSError) as e:
        print(f"WARNING: HTTP PATCH {url} failed: {e}", file=sys.stderr)
        raise


def http_put_json(url, payload, headers=None):
    data = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    log(f"PUT {url}")
    req = Request(url, data=data, headers=hdrs, method="PUT")
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode()
            log(f"PUT {url} -> {resp.status}")
            if not body:
                return {}
            return json.loads(body)
    except HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        print(f"WARNING: HTTP PUT {url} failed: {e.code} {e.reason}: {body}",
              file=sys.stderr)
        raise
    except (URLError, OSError) as e:
        print(f"WARNING: HTTP PUT {url} failed: {e}", file=sys.stderr)
        raise


def http_delete_json(url, payload, headers=None):
    data = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = Request(url, data=data, headers=hdrs, method="DELETE")
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            resp.read()
            log(f"DELETE {url} -> {resp.status}")
            return True
    except HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        print(f"WARNING: HTTP DELETE {url} failed: {e.code} {e.reason}: {body}", file=sys.stderr)
        return None
    except (URLError, OSError) as e:
        print(f"WARNING: HTTP DELETE {url} failed: {e}", file=sys.stderr)
        return None


def _paginate(fetch_page, limit=50):
    results = []
    page = 1
    while True:
        data = fetch_page(page, limit)
        if data is None or not data:
            break
        results.extend(data)
        if len(data) < limit:
            break
        page += 1
    return results


class Gitea:
    def __init__(self):
        self.api_url = os.environ["GITEA_SERVER_URL"] + "/api/v1"
        self.token = os.environ.get("GITEA_SLAVE_TOKEN", "")

        self._payload = None

    def load_payload(self):
        self._payload = json.loads(os.environ["GITEA_PAYLOAD"])

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
        slave_user = os.environ.get("SHOGGOTH_SLAVE_USER", "slave")
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
        slave_user = os.environ.get("SHOGGOTH_SLAVE_USER", "slave")
        reviewer_user = os.environ.get("SHOGGOTH_REVIEWER_USER", "slave-reviewer")
        candidates = {u for u in (slave_user, reviewer_user) if u}
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

    def _auth_headers(self):
        if self.token:
            return {"Authorization": f"token {self.token}"}
        return {}

    def get_ci_logs(self, repo, run_id):
        if not run_id:
            return "CI logs unavailable: no run ID in payload"
        url = f"{self.api_url}/repos/{repo}/actions/runs/{run_id}/logs"
        req = Request(url, headers=self._auth_headers())
        try:
            with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
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

    def get(self, path, params=None):
        headers = {**self._auth_headers(), "Content-Type": "application/json"}
        return http_get(f"{self.api_url}/{path}", headers=headers, params=params)

    def post(self, path, payload):
        return http_post_json(f"{self.api_url}/{path}", payload, headers=self._auth_headers())

    def patch(self, path, payload):
        return http_patch_json(f"{self.api_url}/{path}", payload, headers=self._auth_headers())

    def delete(self, path, payload):
        return http_delete_json(f"{self.api_url}/{path}", payload, headers=self._auth_headers())

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

    def find_repo(self, query, limit=50):
        def fetch_page(page, limit):
            data = self.get("repos/search",
                            params={"q": query, "limit": limit, "page": page})
            if data is None:
                return None
            return data.get("data", [])
        repos = _paginate(fetch_page, limit)

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

    def repo_exists(self, repo_full):
        data = self.get(f"repos/{repo_full}")
        return data is not None

    def get_ssh_url(self, repo_full):
        data = self.get(f"repos/{repo_full}")
        if data is None:
            return None
        return data.get("ssh_url")

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
        slave_user = os.environ.get("SHOGGOTH_SLAVE_USER", "slave")
        all_reviews = _paginate(lambda page, limit: self.get(
            f"repos/{repo}/pulls/{pr_number}/reviews",
            params={"page": page, "limit": limit}))

        all_comments = []
        for review in all_reviews:
            review_id = review.get("id")
            if review_id is None:
                continue
            comments = _paginate(lambda page, limit: self.get(
                f"repos/{repo}/pulls/{pr_number}/reviews/{review_id}/comments",
                params={"page": page, "limit": limit}))
            all_comments.extend(comments)

        unresolved = [c for c in all_comments if not c.get("resolver")]

        trusted_users = set(
            os.environ.get("SHOGGOTH_TRUSTED_REVIEWERS", "admin").split(","))
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

    def _reply_to_comment(self, repo, pr_number, comment_id, body):
        return self.post(
            f"repos/{repo}/issues/{pr_number}/comments",
            {"body": body})

    def get_pr(self, repo, pr_number):
        return self.get(f"repos/{repo}/pulls/{pr_number}")

    def get_pr_files(self, repo, pr_number):
        return _paginate(lambda page, limit: self.get(
            f"repos/{repo}/pulls/{pr_number}/files",
            params={"page": page, "limit": limit}))

    def get_pr_commits(self, repo, pr_number):
        return _paginate(lambda page, limit: self.get(
            f"repos/{repo}/pulls/{pr_number}/commits",
            params={"page": page, "limit": limit}))

    def get_pr_diff_text(self, repo, pr_number):
        """Fetch the unified diff rendered by Gitea at .diff endpoint.
        Returns the diff as a string, or None on failure.
        """
        url = f"{self.api_url}/repos/{repo}/pulls/{pr_number}.diff"
        req = Request(url, headers={**self._auth_headers(),
                                     "accept": "application/vnd.gitea.diff"})
        try:
            with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
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


def _fetch_openbao_secret(path, key):
    addr = os.environ.get("OPENBAO_ADDR", "")
    token = os.environ.get("SHOGGOTH_VAULT_TOKEN", "")
    if not addr or not token:
        return None
    url = f"{addr}/v1/{path}"
    req = Request(url, headers={"X-Vault-Token": token, "accept": "application/json"})
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
            return data.get("data", {}).get("data", {}).get(key)
    except Exception:
        return None


class Redmine:
    def __init__(self):
        domain = os.environ.get("SHOGGOTH_DOMAIN", "")
        if not domain:
            die("SHOGGOTH_DOMAIN is required")
        self.api_url = f"http://api.{domain}/redmine"
        self.token = os.environ.get("REDMINE_TOKEN", "")
        if not self.token:
            self.token = _fetch_openbao_secret("secret/data/redmine/slave-token", "value") or ""
        self._projects = None
        self._statuses = None
        self._user_logins = {}

    def get_user_login(self, user_id):
        if user_id in self._user_logins:
            return self._user_logins[user_id]
        data = self._get(f"users/{user_id}.json")
        if data is None:
            log(f"get_user_login: failed to fetch user #{user_id}")
            return None
        user = data.get("user", data)
        login = user.get("login")
        if login:
            self._user_logins[user_id] = login
        return login

    def _headers(self):
        headers = {"Content-Type": "application/json",
                   "accept": "application/json"}
        if self.token:
            headers["X-Redmine-API-Key"] = self.token
        return headers

    def _get(self, path, params=None):
        url = f"{self.api_url}/{path}"
        return http_get(url, headers=self._headers(), params=params)

    def _put(self, path, payload):
        url = f"{self.api_url}/{path}"
        logged = payload
        if isinstance(payload, dict) and isinstance(payload.get("issue"), dict):
            notes = payload["issue"].get("notes")
            if isinstance(notes, str) and len(notes) > 200:
                logged = {**payload,
                          "issue": {**payload["issue"],
                                    "notes": notes[:200] + "…"}}
        log(f"PUT {url} payload={logged}")
        return http_put_json(url, payload, headers=self._headers())

    def _resolve_status_id(self, name):
        if self._statuses is None:
            data = self._get("issue_statuses.json")
            if data is None:
                die("failed to list redmine issue statuses")
            self._statuses = {s["name"]: s["id"] for s in data.get("issue_statuses", [])}
        status_id = self._statuses.get(name)
        if status_id is None:
            die(f"unknown Redmine status: {name}")
        return status_id

    def match_project(self, normalized):
        if self._projects is None:
            data = self._get("projects.json", params={"limit": 100})
            if data is None:
                die("failed to list redmine projects")
            self._projects = data.get("projects", [])

        for proj in self._projects:
            identifier = proj.get("identifier", "")
            name = proj.get("name", "")
            if identifier == normalized or normalize_for_branch(name) == normalized:
                return proj
        return None

    def identify_project_from_branch(self, branch, repo_full):
        if branch and "/" in branch:
            branch_prefix = branch.split("/", 1)[0]
            normalized = normalize_for_branch(branch_prefix)
            proj = self.match_project(normalized)
            if proj:
                identifier = proj.get("identifier", "")
                if identifier:
                    return identifier
        if repo_full:
            repo_name = repo_full.split("/")[-1] if "/" in repo_full else repo_full
            return re.sub(r"\.git$", "", repo_name)
        return None

    def get_issue(self, task_id):
        data = self._get(f"issues/{task_id}.json",
                         params={"include": "journals,children"})
        if data is None:
            die(f"failed to get redmine issue #{task_id}")
        return data.get("issue", data)

    def get_task_project(self, task_id):
        task = self.get_issue(task_id)
        task_project = task.get("project", {}).get("name", "")
        if not task_project:
            die(f"task #{task_id} has no project assigned")
        return normalize_for_branch(task_project)

    def get_project_repo(self, project_id, domain):
        data = self._get(f"projects/{project_id}.json")
        if data is None:
            die("failed to fetch redmine project info")
        project_info = data.get("project", data)

        homepage = project_info.get("homepage", "") or ""
        if homepage:
            parsed = urlparse(homepage)
            if parsed.scheme and parsed.netloc:
                if parsed.netloc != f"git.{domain}":
                    die(f"homepage URL host '{parsed.netloc}' does not match expected git host")
                if "@" in parsed.netloc:
                    die("homepage URL must not contain embedded credentials")
                path = parsed.path.strip("/")
                if not path:
                    die(f"homepage URL has empty repository path: {homepage}")
                if ".." in path.split("/"):
                    die(f"invalid homepage path: {path}")
                return path
            if ".." in homepage.split("/"):
                die(f"invalid homepage path: {homepage}")
            stripped = homepage.strip("/")
            if not stripped:
                die(f"homepage has empty repository path: {homepage}")
            return stripped

        return None

    def update_issue(self, task_id, *args):
        issue = {}
        notes = None
        i = 0
        while i < len(args):
            if args[i] == "--status" and i + 1 < len(args):
                issue["status_id"] = self._resolve_status_id(args[i + 1])
                i += 2
            elif args[i] == "--note" and i + 1 < len(args):
                notes = args[i + 1]
                i += 2
            else:
                i += 1
        payload = {"issue": issue}
        if notes:
            payload["issue"]["notes"] = notes
        return self._put(f"issues/{task_id}.json", payload)

    def list_issues(self, project):
        data = self._get("issues.json", params={"project_id": project, "limit": 100})
        if data is None:
            return None
        return data.get("issues", [])


class Shoggoth:
    def __init__(self, redmine, gitea, args):
        self.project = None
        self.domain = os.environ["SHOGGOTH_DOMAIN"]
        self.github_org = os.environ["SHOGGOTH_GITHUB_ORG"]
        self.type = None
        self.working_repo = None
        self.working_branch = None
        self.project_repo = None
        self.clone_url = None
        self.repo_dir = None
        self.task_subject = None
        self.gitea = gitea

        self.identify_project(redmine, gitea, args)
        self.identify_project_repo(redmine, gitea)
        self.load_manifest(gitea)

    def identify_project(self, redmine, gitea, args):
        log(f"identify_project: command={args.command}")
        if args.command == "task":
            task = redmine.get_issue(args.task_id)
            self._check_task_actionable(task, redmine)
            self.task_subject = task.get("subject", "")
            task_project = task.get("project", {}).get("name", "")
            if not task_project:
                die(f"task #{args.task_id} has no project assigned")
            self.project = normalize_for_branch(task_project)
            log(f"identify_project: project={self.project} subject={self.task_subject}")
            return
        self.working_repo = gitea.get_repo_full_name()
        if args.command == "ci-failure":
            self.working_branch = gitea.get_ci_branch()
        else:
            self.working_branch = gitea.get_pr_branch()
        self.task_subject = ""
        if "/" in self.working_branch:
            self.task_subject = self.working_branch.split("/", 1)[1]
        self.project = redmine.identify_project_from_branch(self.working_branch, self.working_repo)
        log(f"identify_project: repo={self.working_repo} branch={self.working_branch} project={self.project}")

    def _check_task_actionable(self, task, redmine):
        task_id = task.get("id")
        status_name = (task.get("status") or {}).get("name")
        slave_user_login = os.environ.get("SHOGGOTH_SLAVE_USER", "slave")
        assigned_to = task.get("assigned_to") or {}
        assigned_to_id = assigned_to.get("id")
        assigned_to_login = assigned_to.get("login")
        if assigned_to_login is None and assigned_to_id is not None:
            assigned_to_login = redmine.get_user_login(assigned_to_id)
        if status_name != "In Progress":
            log(f"task: skipping #{task_id} status={status_name!r} (expected 'In Progress')")
            print(f"Task #{task_id} status is {status_name!r}, not 'In Progress' — skipping",
                  flush=True)
            sys.exit(0)
        if assigned_to_login != slave_user_login:
            log(f"task: skipping #{task_id} assignee_id={assigned_to_id!r} "
                f"assignee_login={assigned_to_login!r} (expected {slave_user_login!r})")
            print(f"Task #{task_id} assigned to {assigned_to_login!r}, "
                f"not {slave_user_login!r} — skipping",
                  flush=True)
            sys.exit(0)

    def identify_project_repo(self, redmine, gitea):
        normalized = normalize_for_branch(self.project)
        log(f"identify_project_repo: normalized={normalized}")
        proj = redmine.match_project(normalized)
        if not proj:
            die(f"could not find redmine project for '{self.project}'")
        project_id = proj.get("id")
        log(f"identify_project_repo: redmine project_id={project_id}")

        project_repo = redmine.get_project_repo(project_id, self.domain)

        if project_repo:
            self.project_repo = project_repo
            self.clone_url = f"ssh://git@git.{self.domain}/{project_repo}.git"
            log(f"identify_project_repo: project_repo={project_repo} clone_url={self.clone_url}")
            return

        log("identify_project_repo: no homepage repo, searching gitea")
        self.clone_url, self.project_repo = gitea.find_repo(self.project)
        log(f"identify_project_repo: found repo={self.project_repo} clone_url={self.clone_url}")

    def load_manifest(self, gitea):
        shoggoth_json = gitea.get_file(self.project_repo, "shoggoth.json")
        if shoggoth_json is not None:
            try:
                data = json.loads(shoggoth_json)
            except json.JSONDecodeError:
                die(f"invalid JSON in shoggoth.json for {self.project_repo}")
            config_type = data.get("type", "")
            if config_type in ("standalone", "ccws"):
                self.type = config_type
            elif config_type != "":
                die(f"unsupported checkout type '{config_type}' in shoggoth.json for {self.project_repo}")

        if self.type is None:
            repos_file = gitea.get_file(self.project_repo, ".repos")
            if repos_file is not None:
                self.type = "ccws"
            else:
                self.type = "standalone"

        log(f"load_manifest: type={self.type}")

    def checkout(self):
        workspace_dir = os.environ.get("WORKSPACE_SRC", "/ccws/workspace/src")
        os.makedirs(workspace_dir, exist_ok=True)

        log(f"checkout: type={self.type} branch={self.working_branch} url={self.clone_url} dest={workspace_dir}")

        if self.working_branch:
            result = run(["git", "clone", "--depth", "1", "--branch", self.working_branch,
                          self.clone_url, workspace_dir], check=False)
            if result.returncode != 0 and self.type == "ccws":
                log(f"checkout: branch {self.working_branch} not found, cloning default branch")
                result = run(["git", "clone", "--depth", "1",
                              self.clone_url, workspace_dir], check=False)
        else:
            result = run(["git", "clone", "--depth", "1",
                          self.clone_url, workspace_dir], check=False)

        if result.returncode != 0:
            die(f"git clone failed for {self.clone_url}: {result.stderr}")

        if self.type == "ccws":
            org_re = self.github_org.replace(".", r"\.")
            wsh_args = ["-s", f"s|https://github.com/{org_re}|ssh://git@git.{self.domain}/{org_re}|g",
                        "-s", f"s|git@github.com:{org_re}|ssh://git@git.{self.domain}/{org_re}|g",
                        "-s", f"s|ssh://git@github.com/{org_re}|ssh://git@git.{self.domain}/{org_re}|g",
                        "-s", f"s|git+ssh://git@github.com/{org_re}|ssh://git@git.{self.domain}/{org_re}|g",
                        "-p", "shallow"]
            if self.working_branch:
                wsh_args += ["-P", self.working_branch]
            wsh_args.append("update")
            log(f"checkout: running wshandler: {WSHANDLER_BIN} -r {workspace_dir} {' '.join(wsh_args)}")
            wsh(workspace_dir, wsh_args)

            log("checkout: post-update repository list:")
            wsh_status(workspace_dir, quiet=True)

            log("checkout: running apt update")
            apt_update = run(["sudo", "-S", "apt", "update"], check=False, input="ccws\n")
            if apt_update.stdout:
                print(apt_update.stdout, file=sys.stderr, flush=True)
            if apt_update.stderr:
                print(apt_update.stderr, file=sys.stderr, flush=True)
            if apt_update.returncode != 0:
                die(f"apt update failed with exit code "
                    f"{apt_update.returncode}: "
                    f"{(apt_update.stderr or '').strip()[-500:]}")

            log("checkout: running make dep_install")
            dep_install = run(["make", "dep_install"], cwd="/ccws",
                              input="ccws\n", check=False)
            if dep_install.stdout:
                print(dep_install.stdout, file=sys.stderr, flush=True)
            if dep_install.stderr:
                print(dep_install.stderr, file=sys.stderr, flush=True)
            if dep_install.returncode != 0:
                die(f"make dep_install failed with exit code "
                    f"{dep_install.returncode}: "
                    f"{(dep_install.stderr or '').strip()[-500:]}")

        self.repo_dir = workspace_dir

    def _unshallow(self, repo_dir):
        """Fetch full history if the local clone is shallow.
        Idempotent: no-op when the repo already has full history.
        """
        if not os.path.isdir(os.path.join(repo_dir, ".git")):
            log(f"_unshallow: {repo_dir} is not a git repo, skipping")
            return
        if not os.path.exists(os.path.join(repo_dir, ".git", "shallow")):
            log(f"_unshallow: {repo_dir} already full history")
            return
        log(f"_unshallow: fetching full history for {repo_dir}")
        result = run(["git", "-C", repo_dir, "fetch", "origin",
                      "--unshallow", "--tags"], check=False)
        if result.returncode != 0:
            log(f"_unshallow: warning: {result.stderr.strip()[:200]}")

    def _ensure_remote_ref(self, repo_dir, ref_name, pr_number=None):
        """Ensure refs/remotes/origin/{ref_name} exists locally.
        For PR head refs, falls back to fetch origin pull/{n}/head:pr-head.
        Returns True if the ref is available after the call.
        """
        if not ref_name:
            return False
        safe = re.sub(r"[^A-Za-z0-9_./-]", "-", ref_name)
        if not safe or safe.startswith("-") or ".." in safe:
            log(f"_ensure_remote_ref: refusing unsafe ref {ref_name!r}")
            return False
        result = run(["git", "-C", repo_dir, "fetch", "origin",
                      f"{ref_name}:refs/remotes/origin/{safe}"], check=False)
        if result.returncode == 0:
            return True
        if pr_number is not None:
            log(f"_ensure_remote_ref: trying pull/{pr_number}/head as fallback")
            result = run(["git", "-C", repo_dir, "fetch", "origin",
                          f"pull/{pr_number}/head:refs/remotes/origin/pr-{pr_number}"],
                         check=False)
            return result.returncode == 0
        return False

    def _write_pr_diff(self, repo_dir, base_ref, head_ref, diff_path):
        """Run git diff and write to diff_path. Returns True if non-empty.
        Tries several candidate specs in order:
            origin/{base}...{head}
            origin/{base}...HEAD
            HEAD~1...HEAD
        Empty result still creates an empty file at diff_path so the slash
        command has a target to inspect.
        """
        candidates = []
        if base_ref and head_ref:
            candidates.append(f"origin/{base_ref}...{head_ref}")
        if base_ref:
            candidates.append(f"origin/{base_ref}...HEAD")
        candidates.append("HEAD~1...HEAD")
        for spec in candidates:
            result = run(["git", "-C", repo_dir, "diff", spec,
                          "--binary", "--no-color"], check=False)
            if result.returncode == 0 and result.stdout.strip():
                with open(diff_path, "w") as f:
                    f.write(result.stdout)
                log(f"_write_pr_diff: wrote {len(result.stdout)} bytes via {spec}")
                return True
        open(diff_path, "w").close()
        log("_write_pr_diff: diff is empty, wrote empty file")
        return False

    def _apply_pr_to_worktree(self, repo_dir, base_ref, head_ref,
                              review_repo_dir, pr_number):
        """Create a worktree at base_ref and apply the PR diff as uncommitted
        changes. Returns (worktree_path, diff_source) where diff_source is
        one of "git-diff", "gitea-api", or "" (empty).

        The worktree is at /tmp/shoggoth-review-<n>-<rand>; cleanup is the
        caller's responsibility (use _remove_review_worktree).

        For ccws sub-repos, worktrees are created from the sub-repo's own
        .git (not the manifest) so the resulting tree matches the sub-repo
        layout the agent will review.
        """
        wt_path = (f"/tmp/shoggoth-review-{pr_number}-"
                   f"{uuid.uuid4().hex[:8]}")

        worktree_source = repo_dir
        if (review_repo_dir and review_repo_dir != repo_dir
                and os.path.isdir(os.path.join(review_repo_dir, ".git"))):
            worktree_source = review_repo_dir
            log(f"_apply_pr_to_worktree: using sub-repo {review_repo_dir} "
                f"as worktree source")

        base_sha = None
        for candidate in ([f"origin/{base_ref}", base_ref]
                          if base_ref else []):
            resolved = run(["git", "-C", worktree_source, "rev-parse",
                            "--verify", candidate], check=False,
                           capture_output=True, text=True)
            if resolved.returncode == 0 and resolved.stdout.strip():
                base_sha = resolved.stdout.strip()
                break

        if base_sha is None:
            log(f"_apply_pr_to_worktree: cannot resolve base ref "
                f"{base_ref!r} from {worktree_source}")
            return None, ""

        add_wt = run(["git", "-C", worktree_source, "worktree", "add",
                      "--detach", wt_path, base_sha], check=False,
                     capture_output=True, text=True)
        if add_wt.returncode != 0:
            log(f"_apply_pr_to_worktree: worktree add failed: "
                f"{add_wt.stderr.strip()[:200]}")
            return None, ""

        diff_text = None
        diff_source = ""
        for spec in ([f"origin/{base_ref}...{head_ref}",
                      f"origin/{base_ref}...HEAD",
                      "HEAD~1...HEAD"] if base_ref and head_ref else
                     [f"origin/{base_ref}...HEAD", "HEAD~1...HEAD"]
                     if base_ref else ["HEAD~1...HEAD"]):
            res = run(["git", "-C", worktree_source, "diff", spec,
                       "--binary", "--no-color"],
                      check=False, capture_output=True, text=True)
            if res.returncode == 0 and res.stdout.strip():
                diff_text = res.stdout
                diff_source = f"git-diff:{spec}"
                log(f"_apply_pr_to_worktree: got diff ({len(diff_text)} "
                    f"bytes) via {spec} (from {worktree_source})")
                break

        if diff_text is None:
            log("_apply_pr_to_worktree: trying Gitea .diff fallback")
            gitea_diff = self.gitea.get_pr_diff_text(
                self.working_repo, pr_number)
            if gitea_diff:
                diff_text = gitea_diff
                diff_source = "gitea-api"

        if not diff_text or not diff_text.strip():
            log("_apply_pr_to_worktree: no diff available")
            self._remove_review_worktree(wt_path)
            return None, ""

        if review_repo_dir != repo_dir:
            log(f"_apply_pr_to_worktree: rewriting patch paths "
                f"(review_repo_dir={review_repo_dir})")
            diff_text = self._rewrite_patch_paths(
                diff_text, review_repo_dir)

        apply = run(["git", "-C", wt_path, "apply", "--whitespace=fix",
                     "--recount", "-"],
                    check=False, input=diff_text,
                    capture_output=True, text=True)
        if apply.returncode != 0:
            log(f"_apply_pr_to_worktree: git apply failed "
                f"(will retry with -3): {apply.stderr.strip()[:300]}")
            apply3 = run(["git", "-C", wt_path, "apply", "--3way",
                          "--whitespace=fix", "-"],
                         check=False, input=diff_text,
                         capture_output=True, text=True)
            if apply3.returncode != 0:
                log(f"_apply_pr_to_worktree: git apply --3way also "
                    f"failed: {apply3.stderr.strip()[:300]}")
                self._remove_review_worktree(wt_path)
                return None, ""

        status = run(["git", "-C", wt_path, "status", "--porcelain"],
                     check=False, capture_output=True, text=True)
        log(f"_apply_pr_to_worktree: worktree {wt_path} ready; "
            f"diff_source={diff_source}; "
            f"changed_files={len([l for l in status.stdout.splitlines() if l.strip()])}")
        return wt_path, diff_source

    @staticmethod
    def _rewrite_patch_paths(patch_text, target_dir):
        """Rewrite `diff --git a/<path> b/<path>` headers so the patch can be
        applied at target_dir (a sub-repo) instead of the manifest root.
        Strips the longest common top-level directory prefix.
        """
        a_paths = []
        for line in patch_text.splitlines():
            if line.startswith("--- a/"):
                a_paths.append(line[len("--- a/"):])
        if not a_paths:
            return patch_text
        prefixes = {p.split("/", 1)[0] for p in a_paths if "/" in p}
        if len(prefixes) != 1:
            return patch_text
        top = next(iter(prefixes))
        top_slash = f"{top}/"
        stripped = []
        for line in patch_text.splitlines():
            if line.startswith("diff --git "):
                rest = line[len("diff --git "):]
                if rest.startswith("a/") and " b/" in rest:
                    a_path, b_path_with_marker = rest[2:].split(" b/", 1)
                    b_path = b_path_with_marker
                    if a_path.startswith(top_slash):
                        a_path = a_path[len(top_slash):]
                    if b_path.startswith(top_slash):
                        b_path = b_path[len(top_slash):]
                    line = f"diff --git a/{a_path} b/{b_path}"
            elif line.startswith(f"--- a/{top_slash}"):
                line = f"--- a/{line[len('--- a/' + top_slash):]}"
            elif line.startswith(f"+++ b/{top_slash}"):
                line = f"+++ b/{line[len('+++ b/' + top_slash):]}"
            stripped.append(line)
        return "\n".join(stripped)

    @staticmethod
    def _remove_review_worktree(wt_path):
        """Remove a worktree and prune the worktree list."""
        if not wt_path or not os.path.isdir(wt_path):
            return
        repo_dir = None
        try:
            with open(os.path.join(wt_path, ".git"), "r") as f:
                content = f.read().strip()
            if content.startswith("gitdir:"):
                gitdir = content[len("gitdir:"):].strip()
                wt_subdir = os.path.basename(wt_path)
                worktrees_dir = os.path.dirname(gitdir)
                repo_git_dir = os.path.dirname(worktrees_dir)
                repo_dir = os.path.dirname(repo_git_dir)
        except (OSError, ValueError):
            pass
        if repo_dir is None:
            log(f"_remove_review_worktree: cannot infer repo_dir from "
                f"{wt_path}; falling back to force remove")
            shutil.rmtree(wt_path, ignore_errors=True)
            return
        run(["git", "-C", repo_dir, "worktree", "remove", "--force",
             wt_path], check=False)
        run(["git", "-C", repo_dir, "worktree", "prune"], check=False)
        if os.path.isdir(wt_path):
            shutil.rmtree(wt_path, ignore_errors=True)

    def _resolve_review_repo_dir(self, pr_repo, pr_number):
        """If ccws and the PR touches exactly one sub-repo with its own .git,
        return that sub-repo's absolute path. Otherwise return self.repo_dir.
        """
        if self.type != "ccws":
            return self.repo_dir
        files = self.gitea.get_pr_files(pr_repo, pr_number) or []
        prefixes = {f["path"].split("/", 1)[0]
                    for f in files if "/" in f.get("path", "")}
        if len(prefixes) == 1:
            subrepo = next(iter(prefixes))
            path = os.path.join(self.repo_dir, subrepo)
            if os.path.isdir(os.path.join(path, ".git")):
                log(f"_resolve_review_repo_dir: scoped to {subrepo}")
                return path
        if len(prefixes) > 1:
            log(f"_resolve_review_repo_dir: PR touches {len(prefixes)} "
                f"sub-repos, reviewing at manifest level")
        return self.repo_dir


class OtlpLogger:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.session_id = None
        self.service_name = None
        self.log_thread = None
        self.last_result = None
        self.last_error = None
        self.assistant_text = []
        self._pushed_count = 0
        self._failed_count = 0
        self._logged_endpoint = False
        log(f"otlp: OtlpLogger created endpoint={self.endpoint}")

    def _push_line(self, line):
        if not self._logged_endpoint:
            log(f"otlp: endpoint={self.endpoint} (POST {self.endpoint}/v1/logs)")
            self._logged_endpoint = True
        ts = str(time.time_ns())
        body = {
            "resourceLogs": [{
                "resource": {
                    "attributes": [
                        {"key": "service.name",
                         "value": {"stringValue": self.service_name}},
                        {"key": "service",
                         "value": {"stringValue": self.service_name}},
                        {"key": "session.id",
                         "value": {"stringValue": self.session_id}},
                    ]
                },
                "scopeLogs": [{
                    "scope": {},
                    "logRecords": [{
                        "timeUnixNano": ts,
                        "observedTimeUnixNano": ts,
                        "severityNumber": 9,
                        "severityText": "INFO",
                        "body": {"stringValue": line},
                    }],
                }],
            }]
        }
        result = http_post_json(f"{self.endpoint}/v1/logs", body, quiet=True)
        self._pushed_count += 1
        if result is None:
            self._failed_count += 1
            log(f"otlp: POST failed (count={self._pushed_count} "
                f"failed={self._failed_count}); line: {line[:120]!r}")

    def start_session(self, event_type):
        self.session_id = str(uuid.uuid4())
        self.service_name = f"qwen-{event_type}"
        self.reset()
        self._pushed_count = 0
        self._failed_count = 0
        self._logged_endpoint = False
        log(f"otlp: start_session session_id={self.session_id} "
            f"service={self.service_name} endpoint={self.endpoint}")

    def reset(self):
        self.last_result = None
        self.last_error = None
        self.assistant_text = []

    def stop_session(self):
        log(f"otlp: stop_session pushed={self._pushed_count} "
            f"failed={self._failed_count}")
        self.session_id = None

    def _capture_stream_json(self, line):
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return
        etype = event.get("type")
        if etype == "assistant":
            content = event.get("message", {}).get("content", [])
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text", "")
                    if text:
                        self.assistant_text.append(text)
        elif etype == "result":
            result_text = event.get("result", "") or ""
            if event.get("is_error"):
                self.last_error = result_text
                self.last_result = None
            elif result_text.lstrip().startswith("[API Error") or result_text.lstrip().startswith("API Error"):
                self.last_error = result_text
                self.last_result = None
            else:
                self.last_result = result_text

    def get_review_text(self):
        if self.last_error:
            return ""
        if self.last_result:
            return str(self.last_result)
        return "\n\n".join(self.assistant_text) if self.assistant_text else ""


class Agent:
    def __init__(self, shoggoth):
        self.shoggoth = shoggoth
        self.otlp = OtlpLogger(os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"])
        self.tool_profile = "full"
        self._qwen_restrict_path = None

    def start_session(self, event_type):
        self.tool_profile = TOOL_PROFILE_BY_SESSION.get(event_type, "full")
        log(f"start_session: event_type={event_type} "
            f"tool_profile={self.tool_profile}")
        self.otlp.start_session(event_type)

        self.qwen_env = _filter_qwen_env()

        deny_rules = QWEN_DENY_PATTERNS
        if deny_rules:
            settings_path = f"/tmp/qwen-restrict-{self.otlp.session_id}.json"
            try:
                with open(settings_path, "w") as f:
                    json.dump({"permissions": {"deny": deny_rules}}, f)
                self.qwen_env["QWEN_CODE_SYSTEM_SETTINGS_PATH"] = settings_path
                self._qwen_restrict_path = settings_path
                log(f"start_session: wrote qwen restrictions to {settings_path} "
                    f"({len(deny_rules)} deny rules)")
            except OSError as e:
                print(f"WARNING: failed to write qwen restriction file "
                      f"{settings_path}: {e}", file=sys.stderr)
        else:
            self._qwen_restrict_path = None

        plugin_url = f"http://{self.shoggoth.domain}/plugin.tar.gz"
        log(f"start_session: downloading plugin from {plugin_url}")
        plugin_archive = run(["curl", "-sfS", "--max-time", "30", "-o", "/tmp/plugin.tar.gz", plugin_url],
                             check=False, capture_output=True, text=True, timeout=60)
        if plugin_archive.returncode != 0:
            die(f"failed to download plugin from {plugin_url}: {plugin_archive.stderr.strip()}")

        log(f"start_session: installing plugin from /tmp/plugin.tar.gz")
        plugin_install = run(["qwen", "extensions", "install", "/tmp/plugin.tar.gz",
                              "--scope", "user", "--consent"],
                             check=False, capture_output=True, text=True, timeout=60)
        if plugin_install.returncode != 0:
            err = (plugin_install.stderr or plugin_install.stdout or "").strip()
            die(f"failed to install plugin from {plugin_url}: {err}")

        index_path = self.shoggoth.repo_dir
        log(f"start_session: indexing repo at {index_path}")
        index_result = run(
            ["codebase-memory-mcp", "cli", "index_repository",
             json.dumps({"repo_path": index_path})],
            check=False, capture_output=True, text=True, timeout=120,
        )
        if index_result.returncode != 0:
            print(f"WARNING: codebase-memory-mcp indexing failed: "
                  f"{index_result.stderr.strip()}", file=sys.stderr)

    def prompt(self, text, resume=False, timeout=None, cwd=None):
        if cwd is None:
            if self.shoggoth.type == "ccws":
                cwd = "/ccws"
            else:
                cwd = self.shoggoth.repo_dir
        os.chdir(cwd)
        log(f"prompt: cwd={cwd}")

        cmd = ["qwen", "--yolo", "--output-format", "stream-json"]
        for excluded in TOOL_PROFILES.get(self.tool_profile, []):
            cmd.extend(["--exclude-tools", excluded])
        if resume:
            cmd.extend(["--resume", self.otlp.session_id])
        else:
            cmd.extend(["--session-id", self.otlp.session_id])
        cmd.extend(["--prompt", ANTI_LEAK_DIRECTIVE + text])

        log(f"prompt: resume={resume} session={self.otlp.session_id} "
            f"timeout={timeout} tool_profile={self.tool_profile}")
        log(f"prompt: cmd={' '.join(cmd[:6])}... (prompt length={len(text)})")
        log(f"prompt: text:\n{ANTI_LEAK_DIRECTIVE}{text}")
        log("prompt: starting qwen subprocess")

        def _pump(stream, label):
            try:
                for line in iter(stream.readline, b""):
                    decoded = line.decode(errors="replace")
                    if label == "stdout":
                        sys.stdout.write(decoded)
                        sys.stdout.flush()
                        stripped = decoded.rstrip("\n")
                        self.otlp._push_line(stripped)
                        self.otlp._capture_stream_json(stripped)
                    else:
                        sys.stderr.write(decoded)
                        sys.stderr.flush()
            except Exception as e:
                print(f"WARNING: stream pump {label} crashed: {e}", file=sys.stderr)
            finally:
                stream.close()

        self.otlp.reset()
        qwen = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.qwen_env,
        )
        stdout_thread = threading.Thread(
            target=_pump, args=(qwen.stdout, "stdout"), daemon=True)
        stderr_thread = threading.Thread(
            target=_pump, args=(qwen.stderr, "stderr"), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        timed_out = False
        try:
            qwen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log(f"prompt: TIMEOUT after {timeout}s, killing qwen")
            qwen.kill()
            timed_out = True
            qwen.wait()
        stdout_thread.join(timeout=10)
        stderr_thread.join(timeout=10)
        if timed_out:
            log("prompt: qwen killed due to timeout")
            return 124
        log(f"prompt: qwen exited with code {qwen.returncode}")
        if self.otlp.last_error:
            log(f"prompt: agent reported an error: {str(self.otlp.last_error)[:200]}")
            return 125
        return qwen.returncode

    def prompt_with_retry(self, text, resume=False, timeout=None, cwd=None,
                          max_retries=3, retry_label=None):
        """Wrap prompt() with retry on leaked-thinking-tag errors.

        The bundled qwen-code parser throws
        InvalidStreamError("Model response leaked thinking tags.",
        "PROTOCOL_TAG_LEAK") when the model emits content that resembles an
        unclosed thinking tag. After the bundled protocolTagLeakMaxRetries=2
        the error surfaces as
        [API Error: Model response leaked thinking tags.] in the stream-json
        result event; qwen then exits non-zero and OtlpLogger.last_error is
        set. This wrapper catches that, rotates to a fresh qwen session-id
        so the model has no carryover from the failed run (LLM output is
        non-deterministic — subsequent attempts usually succeed), and retries.
        """
        label = retry_label or "prompt"
        for attempt in range(max_retries + 1):
            if attempt > 0:
                delay = 2 * attempt
                self.otlp._push_line(
                    f"{label}: leaked-thinking-tag error "
                    f"attempt={attempt}/{max_retries} "
                    f"delay={delay}s; rotating session and retrying")
                time.sleep(delay)
                self.otlp.session_id = str(uuid.uuid4())
                self.otlp.reset()
                resume = False
            rc = self.prompt(text, resume=resume, timeout=timeout, cwd=cwd)
            if rc == 0:
                return rc
            err = str(self.otlp.last_error or "")
            if "leaked thinking tags" not in err:
                return rc
            if attempt >= max_retries:
                break
        self.otlp._push_line(
            f"{label}: gave up after {max_retries + 1} attempts "
            f"last_error={(str(self.otlp.last_error or ''))[:200]!r}")
        return rc


    def stop_session(self):
        log("stop_session")
        self.otlp.stop_session()
        if self._qwen_restrict_path:
            try:
                os.unlink(self._qwen_restrict_path)
                log(f"stop_session: removed qwen restrictions file "
                    f"{self._qwen_restrict_path}")
            except OSError as e:
                print(f"WARNING: failed to remove qwen restriction file "
                      f"{self._qwen_restrict_path}: {e}", file=sys.stderr)
            self._qwen_restrict_path = None


class CommandBase:
    def __init__(self, gitea, redmine, agent, shoggoth=None):
        self.gitea = gitea
        self.redmine = redmine
        self.agent = agent
        self.shoggoth = shoggoth

    def _commit_only_standalone(self, repo_dir, commit_msg):
        status = run(["git", "-C", repo_dir, "status", "--porcelain"], check=False)
        if status.returncode != 0:
            die(f"git status failed in {repo_dir}: {status.stderr}")
        if not status.stdout.strip():
            self._pending_modified_repos = []
            return False

        add = run(["git", "-C", repo_dir, "add", "-A"], check=False)
        if add.returncode != 0:
            die(f"failed to stage changes in {repo_dir}: {add.stderr}")
        staged = run(["git", "-C", repo_dir, "diff", "--cached", "--name-only"], check=False)
        if staged.returncode != 0:
            die(f"git diff --cached failed in {repo_dir}: {staged.stderr}")
        if not staged.stdout.strip():
            self._pending_modified_repos = []
            return False

        commit = run(["git", "-C", repo_dir, "commit", "-m", commit_msg], check=False)
        if commit.returncode != 0:
            die(f"git commit failed: {commit.stderr}")
        self._pending_modified_repos = []
        return True

    def _commit_only_ccws(self, repo_dir, commit_msg):
        status = wsh_status(repo_dir, quiet=True)
        if not status.stdout.strip():
            self._pending_modified_repos = []
            return False

        modified_repos = []
        for line in status.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            repo_name = parts[0]
            flags = parts[3]
            if "M" in flags:
                modified_repos.append(repo_name)
        if not modified_repos:
            self._pending_modified_repos = []
            return False

        for repo_name in modified_repos:
            repo_path = os.path.join(repo_dir, repo_name)
            add = run(["git", "-C", repo_path, "add", "-A"], check=False)
            if add.returncode != 0:
                die(f"failed to stage changes in {repo_name}: {add.stderr}")

        commit = wsh(repo_dir, ["commit", sanitize_commit_msg(commit_msg)], check=False)
        if commit.returncode != 0:
            die(f"git commit failed: {commit.stderr}")
        self._pending_modified_repos = modified_repos
        return True

    def _commit_only(self, commit_msg=None):
        repo_dir = self.shoggoth.repo_dir
        if commit_msg is None:
            commit_msg = f"Address review comments on PR#{self.gitea.get_pr_number()}"

        if self.shoggoth.type == "ccws":
            return self._commit_only_ccws(repo_dir, commit_msg)
        return self._commit_only_standalone(repo_dir, commit_msg)

    def _push_pending_commits(self):
        repo_dir = self.shoggoth.repo_dir
        pr_branch = self.shoggoth.working_branch
        if self.shoggoth.type == "ccws":
            modified_repos = getattr(self, "_pending_modified_repos", [])
            push = wsh(repo_dir, ["-p", "version", "push"] + modified_repos, check=False)
            if push.returncode != 0:
                die(f"failed to push branches: {push.stderr}")
        else:
            push = run(["git", "-C", repo_dir, "push", "origin", pr_branch], check=False)
            if push.returncode != 0:
                die(f"failed to push branch {pr_branch}: {push.stderr}")


class TaskCommand(CommandBase):
    def __init__(self, task_id, shoggoth, gitea, redmine, agent):
        super().__init__(gitea, redmine, agent, shoggoth=shoggoth)
        self.task_id = task_id

    @staticmethod
    def _sanitize_subject(subject):
        return subject.replace("\n", " ").replace("\r", "")[:200]

    def create_pull_request(self, repo, branch, task_id, title, base="main"):
        self.gitea.ensure_pull_requests_enabled(repo)
        slave_user = os.environ.get("SHOGGOTH_SLAVE_USER", "slave")
        review_user = os.environ.get("SHOGGOTH_REVIEW_USER", "admin")
        result = self.gitea.post(f"repos/{repo}/pulls", {
            "base": base,
            "head": branch,
            "title": title,
            "body": f"Task#{task_id}: {title}",
            "assignees": [slave_user],
            "reviewers": [review_user],
        })
        if result is None:
            print(f"WARNING: failed to create pull request for {repo} "
                  f"branch {branch} (base {base})", file=sys.stderr)
            return None
        return result.get("html_url")

    def commit_push_and_create_mr_standalone(self, branch, task_id, task_subject):
        repo_dir = self.shoggoth.repo_dir
        status = run(["git", "-C", repo_dir, "status", "--porcelain"])
        if not status.stdout.strip():
            print("No local changes, skipping commit and push")
            return False, []

        checkout = run(["git", "-C", repo_dir, "checkout", "-B", branch], check=False)
        if checkout.returncode != 0:
            die(f"git checkout -B {branch} failed: {checkout.stderr}")
        add = run(["git", "-C", repo_dir, "add", "-A"], check=False)
        if add.returncode != 0:
            die(f"failed to stage changes: {add.stderr}")

        staged = run(["git", "-C", repo_dir, "diff", "--cached", "--name-only"], check=False)
        if staged.returncode != 0:
            die(f"git diff --cached failed: {staged.stderr}")
        if not staged.stdout.strip():
            print("No staged changes, skipping commit and push")
            return False, []

        sanitized_subject = self._sanitize_subject(task_subject)
        commit = run(["git", "-C", repo_dir, "commit",
                      "-m", f"Task#{task_id}: {sanitized_subject}"], check=False)
        if commit.returncode != 0:
            die(f"git commit failed: {commit.stderr}")

        push = run(["git", "-C", repo_dir, "push", "-u", "origin", branch], check=False)
        if push.returncode != 0:
            die(f"failed to push branch '{branch}': {push.stderr}")

        repo_full = self.shoggoth.project_repo
        base = self.gitea.get_default_branch(repo_full)
        pr_url = self.create_pull_request(
            repo_full, branch, task_id, sanitized_subject, base,
        )
        return True, ([pr_url] if pr_url else [])

    def commit_push_and_create_mr_ccws(self, branch, task_id, task_subject):
        repo_dir = self.shoggoth.repo_dir
        log("commit_push: pre-commit repository status:")
        status = wsh_status(repo_dir)
        if not status.stdout.strip():
            print("No local changes, skipping commit and push")
            return False, []

        modified_repos = []
        for line in status.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            repo_name = parts[0]
            flags = parts[3]
            if "M" in flags:
                modified_repos.append(repo_name)
        if not modified_repos:
            print("No local changes, skipping commit and push")
            return False, []

        sanitized_subject = self._sanitize_subject(task_subject)
        commit_msg = f"Task#{task_id}: {sanitized_subject}"

        wsh(repo_dir, ["branch", "new", branch])

        for repo_name in modified_repos:
            repo_path = os.path.join(repo_dir, repo_name)
            add = run(["git", "-C", repo_path, "add", "-A"], check=False)
            if add.returncode != 0:
                die(f"failed to stage changes in {repo_name}: {add.stderr}")

        commit = wsh(repo_dir, ["commit", sanitize_commit_msg(commit_msg)], check=False)
        if commit.returncode != 0:
            die(f"git commit failed: {commit.stderr}")
        push = wsh(repo_dir, ["-p", "version", "push"] + modified_repos, check=False)
        if push.returncode != 0:
            die(f"failed to push branches: {push.stderr}")

        log("commit_push: post-push repository status:")
        status = wsh_status(repo_dir)
        repo_urls = {}
        for line in status.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            repo_urls[parts[0]] = parts[-1]

        pr_urls = []
        for repo_name in modified_repos:
            repo_url = repo_urls.get(repo_name)
            if not repo_url:
                continue
            parsed = urlparse(repo_url)
            path = parsed.path.strip("/")
            if path.endswith(".git"):
                path = path[:-4]
            if "/" not in path:
                continue
            repo_full = path
            base = self.gitea.get_default_branch(repo_full)
            pr_url = self.create_pull_request(
                repo_full, branch, task_id, sanitized_subject, base,
            )
            if pr_url:
                pr_urls.append(pr_url)

        return True, pr_urls

    def commit_push_and_create_mr(self, branch, task_id, task_subject):
        if self.shoggoth.type == "ccws":
            return self.commit_push_and_create_mr_ccws(
                branch, task_id, task_subject)
        return self.commit_push_and_create_mr_standalone(
            branch, task_id, task_subject)

    def execute(self):
        if not re.match(r"^\d+$", self.task_id):
            die(f"invalid task ID: {self.task_id}")

        task = self.redmine.get_issue(self.task_id)

        task_subject = self.shoggoth.task_subject
        normalized_subject = normalize_for_branch(task_subject)
        if not normalized_subject:
            normalized_subject = f"task-{self.task_id}"
        shoggoth_branch = f"{self.shoggoth.project}/{normalized_subject}"
        log(f"task: branch={shoggoth_branch} subject={task_subject}")
        print(f"=== TASK IMPLEMENTATION: task #{self.task_id} "
              f"project={self.shoggoth.project} subject={task_subject} ===",
              flush=True)

        log(f"task: switching #{self.task_id} status to 'Feedback'")
        self.redmine.update_issue(
            self.task_id,
            "--status", "Feedback",
            "--note",
            "Task execution triggered; status changed from 'In Progress' to 'Feedback'.",
        )

        self.agent.start_session("task")

        task_data = {
            "subject": task.get("subject", ""),
            "description": task.get("description", ""),
            "custom_fields": task.get("custom_fields", []),
        }

        prompt = (
            f"Execute the following task: {task_subject}\n\n"
            f"{json.dumps(task_data, indent=2)}\n\n"
            f"Use the codebase-memory skill to explore the codebase and "
            f"understand the relevant code before making changes.\n\n"
            f"Leave all changes uncommitted in the working tree.\n\n"
            f"Update basic memory with any new information learned about "
            f"the project {self.shoggoth.project} and task \"{task_subject}\"."
        )

        rc = self.agent.prompt_with_retry(prompt)
        if rc != 0:
            die(f"qwen agent exited with code {rc}")

        self.agent.stop_session()

        log("task: committing and creating MR")
        pushed, pr_urls = self.commit_push_and_create_mr(
            shoggoth_branch, self.task_id, task_subject,
        )

        if pr_urls:
            note = "Merge requests created: " + ", ".join(pr_urls)
            redmine_update_args = ["--status", "Resolved", "--note", note]
            for url in pr_urls:
                print(f"Pull request created: {url}")
        elif pushed:
            redmine_update_args = ["--note",
                                   f"Changes pushed to branch {shoggoth_branch} but MR creation failed"]
        else:
            redmine_update_args = ["--status", "Feedback",
                                   "--note", "Reverted to Feedback: agent produced no source code changes"]

        self.redmine.update_issue(self.task_id, *redmine_update_args)


class CiFailureCommand(CommandBase):
    def __init__(self, shoggoth, gitea, redmine, agent):
        super().__init__(gitea, redmine, agent, shoggoth=shoggoth)

    def execute(self):
        conclusion = self.gitea.get_ci_conclusion()
        log(f"ci-failure: conclusion={conclusion}")
        if conclusion != "failure":
            print(f"Ignoring workflow_run conclusion: {conclusion}")
            return

        ci_repo = self.shoggoth.working_repo
        ci_sha = self.gitea.get_ci_sha()
        ci_branch = self.shoggoth.working_branch
        ci_run_url = self.gitea.get_ci_run_url()
        ci_workflow = self.gitea.get_ci_workflow_name()
        ci_run_id = self.gitea.get_ci_run_id()
        log(f"ci-failure: repo={ci_repo} sha={ci_sha} branch={ci_branch} workflow={ci_workflow} run_id={ci_run_id}")
        print(f"=== CI FAILURE FIX: repo={ci_repo} workflow={ci_workflow} "
              f"branch={ci_branch} sha={ci_sha} ===", flush=True)

        ci_logs = self.gitea.get_ci_logs(ci_repo, ci_run_id)

        self.agent.start_session("ci-failure")

        prompt = (
            f"CI workflow '{ci_workflow}' failed on repository {ci_repo} "
            f"at commit {ci_sha} (branch {ci_branch}).\n"
            f"Run URL: {ci_run_url}\n\n"
            f"CI logs:\n{ci_logs}\n\n"
            f"Use the codebase-memory skill to understand the code related "
            f"to the failure. Fix the code. Leave all changes uncommitted "
            f"in the working tree; the workflow script handles commit and push."
        )

        rc = self.agent.prompt_with_retry(prompt)
        if rc != 0:
            die(f"qwen agent exited with code {rc}")

        self.agent.stop_session()

        commit_subject = f"Fix CI failure in {ci_workflow}"
        commit_msg = (
            f"{commit_subject}\n\n"
            f"Resolves CI failure on {ci_repo}@{ci_sha[:12]} "
            f"(branch {ci_branch}).\n"
            f"Run URL: {ci_run_url}"
        )
        committed = self._commit_only(commit_msg)
        if committed:
            log(f"ci-failure: committed fix on branch {ci_branch}")
            self._push_pending_commits()
        else:
            log(f"ci-failure: no source changes to commit")


class PrUpdateCommand(CommandBase):
    def __init__(self, shoggoth, gitea, redmine, agent):
        super().__init__(gitea, redmine, agent, shoggoth=shoggoth)

    def execute(self):
        action = self.gitea.get_pr_action()
        log(f"pr-update: action={action}")
        if action in ("review_request_removed", "synchronize"):
            return

        pr_url = self.gitea.get_pr_url()
        pr_number = self.gitea.get_pr_number()
        log(f"pr-update: pr_url={pr_url}")

        if action == "deleted":
            print(f"=== PR COMMENT CHECK AFTER COMMENT DELETION: pr={pr_url} "
                  f"repo={self.shoggoth.working_repo} ===", flush=True)
            self._pr_comment(pr_number, pr_url)
            return

        if self.gitea.has_review():
            if self.gitea.is_review_by_slave_user():
                log("pr-update: review is by slave user, skipping")
            else:
                print(f"=== PR COMMENT ADDRESSING: pr={pr_url} "
                      f"repo={self.shoggoth.working_repo} ===", flush=True)
                self._pr_comment(self.gitea.get_pr_number(), pr_url)
        elif action == "review_requested" and self.gitea.is_slave_user_reviewer():
            print(f"=== PR REVIEW: pr={pr_url} "
                  f"repo={self.shoggoth.working_repo} ===", flush=True)
            self._pr_review(pr_url)
        else:
            print(f"=== PR COMMENT CHECK: pr={pr_url} "
                  f"repo={self.shoggoth.working_repo} ===", flush=True)
            self._pr_comment(self.gitea.get_pr_number(), pr_url)

    def _pr_review(self, pr_url):
        pr_repo = self.shoggoth.working_repo
        pr_number = self.gitea.get_pr_number()
        log(f"pr-review: repo={pr_repo} pr={pr_number}")

        pr_data = self.gitea.get_pr(pr_repo, pr_number)
        pr_title = pr_data.get("title", "") if pr_data else ""
        pr_base = pr_data.get("base", {}).get("ref", "") if pr_data else ""
        pr_sha = pr_data.get("head", {}).get("sha", "") if pr_data else ""

        self.agent.start_session("pr-review")
        otlp = self.agent.otlp
        otlp._push_line(
            f"pr-review: starting review pr={pr_number} repo={pr_repo} "
            f"title={pr_title!r} base={pr_base} head={pr_sha}")

        review_repo_dir = self.shoggoth._resolve_review_repo_dir(pr_repo, pr_number)
        otlp._push_line(f"pr-review: resolved review_repo_dir={review_repo_dir}")

        self.shoggoth._unshallow(review_repo_dir)
        otlp._push_line(f"pr-review: _unshallow done for {review_repo_dir}")

        self.shoggoth._ensure_remote_ref(review_repo_dir, pr_base, pr_number)
        otlp._push_line(f"pr-review: _ensure_remote_ref base={pr_base} done")

        if pr_sha:
            self.shoggoth._ensure_remote_ref(review_repo_dir, pr_sha, pr_number)
            otlp._push_line(f"pr-review: _ensure_remote_ref head={pr_sha} done")

        diff_path = f"/tmp/shoggoth-pr-{pr_number}-{uuid.uuid4().hex[:8]}.diff"
        wrote = self.shoggoth._write_pr_diff(review_repo_dir, pr_base, pr_sha, diff_path)
        diff_size = os.path.getsize(diff_path) if os.path.exists(diff_path) else 0
        if not wrote:
            gitea_diff = self.gitea.get_pr_diff_text(pr_repo, pr_number)
            if gitea_diff:
                with open(diff_path, "w") as f:
                    f.write(gitea_diff)
                diff_size = len(gitea_diff)
                log(f"pr-review: using Gitea-rendered diff "
                    f"({diff_size} bytes)")
                otlp._push_line(
                    f"pr-review: Gitea .diff fallback wrote {diff_size} bytes")
            else:
                log("pr-review: Gitea .diff fallback also empty")
                otlp._push_line("pr-review: Gitea .diff fallback empty")
        else:
            otlp._push_line(
                f"pr-review: _write_pr_diff wrote {diff_size} bytes "
                f"to {diff_path}")

        wt_path, diff_source = self.shoggoth._apply_pr_to_worktree(
            self.shoggoth.repo_dir, pr_base, pr_sha,
            review_repo_dir, pr_number)
        if wt_path is None:
            otlp.last_error = "could not create review worktree with PR diff applied"
            self.agent.stop_session()
            die(f"pr-review: failed to set up review worktree")
        otlp._push_line(
            f"pr-review: worktree ready at {wt_path} "
            f"(diff_source={diff_source})")

        try:
            review_prompt = (
                f"You are reviewing PR {pr_url} (repo: {pr_repo}, "
                f"PR #{pr_number}).\n"
                f"Title: {pr_title}\n"
                f"Base: {pr_base}\n"
                f"Head SHA: {pr_sha}\n\n"
                f"The PR has been applied as UNCOMMITTED changes to a "
                f"git worktree at: {wt_path}\n"
                f"This is the source tree you must review. Read files from "
                f"this path; do NOT read from any other checkout.\n\n"
                f"The unified diff is also preserved at: {diff_path} "
                f"({diff_size} bytes) for line-number references.\n\n"
                f"To inspect the change:\n"
                f"  - `git -C {wt_path} status` to see modified files\n"
                f"  - `git -C {wt_path} diff` to see the full diff against "
                f"the base\n"
                f"  - read the modified files directly to understand "
                f"context, callers, and existing patterns\n\n"
                f"You may use the codebase-memory MCP tools "
                f"(search_graph, get_code_snippet, trace_path) for context "
                f"lookups; the indexed code path is "
                f"{self.shoggoth.repo_dir}, so cross-reference by reading "
                f"matching files from the worktree path.\n"
                f"Use basic_memory MCP (search, read_note) for prior "
                f"project decisions.\n\n"
                f"Produce a code review focused on:\n"
                f"- correctness (logic bugs, off-by-one, edge cases)\n"
                f"- security (input validation, secrets in code, race "
                f"conditions)\n"
                f"- maintainability (clarity, naming, coupling)\n"
                f"- test coverage gaps (does the change add or modify code "
                f"without tests?)\n"
                f"- adherence to project conventions (look at neighbouring "
                f"code in the same files)\n\n"
                f"Do NOT make code changes. Do NOT post comments or reviews "
                f"via Gitea MCP tools — only the workflow script posts the "
                f"final review.\n\n"
                f"When done, output a single JSON object inside a ```json "
                f"fenced block, followed by the literal line END_OF_REVIEW "
                f"on its own line. The JSON shape is:\n"
                f"```\n"
                f"{{\n"
                f'  "verdict": "approve" | "request_changes" | "comment",\n'
                f'  "summary": "one-paragraph summary of the change",\n'
                f'  "findings": [\n'
                f"    {{\n"
                f'      "severity": "critical" | "high" | "medium" | '
                f'"low" | "info",\n'
                f'      "path": "relative/path/to/file (relative to the '
                f'worktree)",\n'
                f'      "line": 42,\n'
                f'      "body": "description of the issue and concrete '
                f'fix"\n'
                f"    }}\n"
                f"  ]\n"
                f"}}\n"
                f"```\n\n"
                f"If the change is sound and small, output verdict "
                f'"approve" with an empty findings list. If you find any '
                f"critical or high-severity issue, output verdict "
                f'"request_changes".\n\n'
                f"Important: do not skip the END_OF_REVIEW line — it tells "
                f"the workflow script your review is complete."
            )

            log(f"pr-review: invoking agent prompt "
                f"(diff_source={diff_source}, diff={diff_size} bytes)")
            otlp._push_line(
                f"pr-review: invoking agent prompt "
                f"(diff_source={diff_source} diff_size={diff_size} "
                f"prompt_chars={len(review_prompt)})")

            t0 = time.time()
            rc = self.agent.prompt_with_retry(review_prompt,
                                              timeout=AGENT_TIMEOUT,
                                              cwd=wt_path,
                                              retry_label="pr-review")
            elapsed = time.time() - t0

            otlp._push_line(
                f"pr-review: agent prompt finished rc={rc} "
                f"elapsed={elapsed:.1f}s "
                f"assistant_chars="
                f"{sum(len(t) for t in otlp.assistant_text)}")

            if rc == 124:
                self._handle_timeout(pr_repo, pr_number, pr_url, [])
                return
            if rc != 0:
                otlp.last_error = f"agent prompt exited with code {rc}"
                otlp._push_line(
                    f"pr-review: ERROR agent prompt exited with code {rc} "
                    f"last_error={otlp.last_error}")
                self.agent.stop_session()
                die(f"pr-review: agent exited with code {rc}")

            review_text = otlp.get_review_text()
            otlp.last_result = review_text
            otlp._push_line(
                f"pr-review: extracted review_text length={len(review_text)}")

            review_data = _extract_review_json(review_text)
            if review_data is not None:
                review_md = _format_review_json(review_data, pr_title,
                                                pr_url)
                otlp._push_line(
                    f"pr-review: parsed JSON review "
                    f"verdict={review_data.get('verdict', '?')!r} "
                    f"findings="
                    f"{len(review_data.get('findings') or [])}")
            else:
                otlp._push_line(
                    "pr-review: no JSON block found in agent output; "
                    "posting raw assistant text as Markdown")
                review_md = review_text.strip() if review_text else ""

            if not review_md:
                log("pr-review: no review text captured from agent")
                otlp._push_line(
                    "pr-review: no review text captured from agent")
                slave_user = os.environ.get("SHOGGOTH_SLAVE_USER", "slave")
                self.gitea.remove_requested_reviewer(pr_repo, pr_number,
                                                    slave_user)
                self.agent.stop_session()
                return

            log(f"pr-review: posting review to gitea "
                f"(length={len(review_md)})")
            otlp._push_line(
                f"pr-review: posting review to gitea "
                f"(length={len(review_md)})")
            result = self.gitea.post_pr_review_chunked(pr_repo, pr_number,
                                                      review_md)
            if result is None:
                otlp.last_error = "failed to post review to gitea"
                self.agent.stop_session()
                die("pr-review: failed to post review to gitea")
            otlp._push_line("pr-review: review posted to gitea")
        finally:
            log(f"pr-review: removing review worktree {wt_path}")
            self.shoggoth._remove_review_worktree(wt_path)
            otlp._push_line(f"pr-review: removed worktree {wt_path}")

        slave_user = os.environ.get("SHOGGOTH_SLAVE_USER", "slave")
        reviewer_user = os.environ.get("SHOGGOTH_REVIEWER_USER",
                                        "slave-reviewer")
        log(f"pr-review: removing requested reviewers "
            f"{slave_user}, {reviewer_user}")
        self.gitea.remove_requested_reviewer(pr_repo, pr_number, slave_user)
        self.gitea.remove_requested_reviewer(pr_repo, pr_number,
                                            reviewer_user)
        otlp._push_line(
            f"pr-review: session done elapsed={time.time() - t0:.1f}s "
            f"review_chars={len(review_md)}")
        self.agent.stop_session()

    def _resolve_task(self, branch):
        issues = self.redmine.list_issues(self.shoggoth.project)
        if issues is None:
            return None
        if "/" not in branch:
            return None
        parts = branch.split("/", 1)
        branch_subject = normalize_for_branch(parts[1])
        for issue in issues:
            if normalize_for_branch(issue.get("subject", "")) == branch_subject:
                return issue.get("id")
        return None

    def _pr_comment(self, pr_number, pr_url):
        pr_repo = self.shoggoth.working_repo
        pr_branch = self.shoggoth.working_branch
        task_subject = self.shoggoth.task_subject
        log(f"pr-comment: repo={pr_repo} pr={pr_number} branch={pr_branch}")

        unresolved = self.gitea.get_unresolved_review_comments(pr_repo, pr_number)
        log(f"pr-comment: unresolved comments={len(unresolved) if unresolved else 0}")

        if not unresolved:
            print("No unresolved review comments")
            return

        resolved_comments = []
        deferred_resolves = []
        redmine_task_id = None
        redmine_note = None
        changes_produced = False

        self.agent.start_session("pr-comment")

        try:
            rc = self.agent.prompt_with_retry(
                f"Load memories regarding the project {self.shoggoth.project} from basic memory. "
                f"Proceed if memory is not available.",
                retry_label="pr-comment-mem-project")
            if rc != 0:
                die(f"qwen agent exited with code {rc}")

            if task_subject:
                rc = self.agent.prompt_with_retry(
                    f"Load memories regarding task \"{task_subject}\" "
                    f"in project {self.shoggoth.project} from basic memory. "
                    f"Proceed if memory is not available.", resume=True,
                    retry_label="pr-comment-mem-task")
                if rc != 0:
                    die(f"qwen agent exited with code {rc}")

            for comment in unresolved:
                c_path = comment.get("path", "unknown")
                c_line = comment.get("line")
                c_body = comment.get("body", "")
                comment_id = comment.get("id")
                if not c_body:
                    if comment_id is not None:
                        deferred_resolves.append(comment_id)
                    resolved_comments.append(comment)
                    continue
                location = f"{c_path}:{c_line}" if c_line is not None else c_path
                prompt = (
                    f"Address the following review comment on PR {pr_url} "
                    f"(file: {location}): "
                    f"{c_body}. Use the codebase-memory skill to understand "
                    f"the code context around the comment. "
                    f"Leave all changes uncommitted in the working tree. "
                    f"Do not post comments, reviews, or replies via Gitea MCP tools."
                )
                rc = self.agent.prompt_with_retry(prompt, resume=True,
                                                  timeout=AGENT_TIMEOUT,
                                                  retry_label="pr-comment")
                if rc == 124:
                    self._handle_timeout(pr_repo, pr_number, pr_url, resolved_comments)
                if rc != 0:
                    die(f"qwen agent exited with code {rc}")

                summary = self.agent.otlp.get_review_text().strip()
                summary_lines = summary.split("\n") if summary else []
                body_first_line = c_body.split("\n")[0].strip()
                commit_subject = body_first_line[:72]
                comment_link = f"{pr_url}#issuecomment-{comment_id}" if comment_id is not None else pr_url
                run_url = os.environ.get("SHOGGOTH_ARGO_RUN_URL", "")
                run_url_line = f"\nArgo run: {run_url}" if run_url else ""
                if summary_lines:
                    commit_body = summary[-1000:]
                    if len(summary) > 1000:
                        commit_body = "... (truncated)\n" + commit_body
                    commit_msg = f"{commit_subject}\n\n{comment_link}{run_url_line}\n\n{commit_body}"
                else:
                    commit_msg = f"{commit_subject}\n\n{comment_link}{run_url_line}"
                committed = self._commit_only(commit_msg)
                if committed:
                    resolved_comments.append(comment)
                    if comment_id is not None:
                        deferred_resolves.append(comment_id)

            rc = self.agent.prompt_with_retry(
                f"Finalize all remaining work. Update basic memory with any new information "
                f"learned about the project {self.shoggoth.project}.",
                resume=True, timeout=AGENT_TIMEOUT,
                retry_label="pr-comment-finalize")
            if rc == 124:
                self._handle_timeout(pr_repo, pr_number, pr_url, resolved_comments)
            if rc != 0:
                die(f"qwen agent exited with code {rc}")

            if task_subject:
                rc = self.agent.prompt_with_retry(
                    f"Update basic memory with any new information learned about "
                    f"the task \"{task_subject}\".", resume=True, timeout=AGENT_TIMEOUT,
                    retry_label="pr-comment-finalize-task")
                if rc == 124:
                    self._handle_timeout(pr_repo, pr_number, pr_url, resolved_comments)
                if rc != 0:
                    die(f"qwen agent exited with code {rc}")

            self.agent.stop_session()

            changes_produced = len(resolved_comments) > 0
            log(f"pr-comment: resolved_comments={len(resolved_comments)}")

            redmine_task_id = self._resolve_task(pr_branch)
            log(f"pr-comment: redmine_task_id={redmine_task_id} changes_produced={changes_produced}")
            if redmine_task_id:
                note = f"Review comments on {pr_url} have been addressed."
                run_url = os.environ.get("SHOGGOTH_ARGO_RUN_URL", "")
                if run_url:
                    note += f" Argo run: {run_url}."
                redmine_note = note
        finally:
            self._push_pending_commits()
            for comment_id in deferred_resolves:
                r = self.gitea.resolve_comment(pr_repo, comment_id)
                if r is None:
                    log(f"pr-comment: WARNING resolve failed for comment {comment_id}")
                else:
                    log(f"pr-comment: resolved comment {comment_id}")
            if redmine_task_id and redmine_note is not None:
                if changes_produced:
                    self.redmine.update_issue(redmine_task_id,
                         "--status", "Resolved",
                         "--note", redmine_note)
                else:
                    self.redmine.update_issue(redmine_task_id,
                         "--note", redmine_note)

    def _handle_timeout(self, pr_repo, pr_number, pr_url, resolved_comments):
        log(f"pr-comment: handling timeout, resolved so far={len(resolved_comments)}")
        self.agent.stop_session()
        resolved_count = len(resolved_comments)
        run_url = os.environ.get("SHOGGOTH_ARGO_RUN_URL", "")
        run_url_suffix = f" Argo run: {run_url}." if run_url else ""
        self._push_pending_commits()
        timeout_msg = (
            f"Agent timed out after {AGENT_TIMEOUT // 60} minutes. "
            f"Addressed {resolved_count} comment(s) before timeout. "
            f"Committed changes have been pushed."
            f"{run_url_suffix}"
        )
        self.gitea.post_pr_review_chunked(pr_repo, pr_number, timeout_msg)
        die(timeout_msg)


def main():
    parser = argparse.ArgumentParser(
        prog="shoggoth_workflow.py",
        description="Shoggoth workflow automation for Redmine tasks, CI failures, and PR updates",
    )
    parser.add_argument("command", choices=["task", "ci-failure", "pr-update"],
                        help="Command to execute")
    parser.add_argument("task_id", nargs="?", default=None,
                        help="Redmine task ID (required for 'task' command)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose logging to stderr")

    args = parser.parse_args()

    global VERBOSE
    VERBOSE = args.verbose

    # The git-cred sidecar runs in parallel with this script; gate every
    # git operation (clone, push, commit, fetch) on the sidecar producing
    # its three outputs. See _wait_for_git_cred_ready for details.
    _wait_for_git_cred_ready()

    if args.command == "task":
        if not args.task_id or not args.task_id.isdigit():
            parser.error("task command requires a numeric task ID")

    gitea = Gitea()
    redmine = Redmine()

    if args.command != "task":
        gitea.load_payload()

    shoggoth = Shoggoth(redmine, gitea, args)
    shoggoth.checkout()

    agent = Agent(shoggoth)

    if args.command == "task":
        cmd = TaskCommand(args.task_id, shoggoth, gitea, redmine, agent)
    elif args.command == "ci-failure":
        cmd = CiFailureCommand(shoggoth, gitea, redmine, agent)
    elif args.command == "pr-update":
        cmd = PrUpdateCommand(shoggoth, gitea, redmine, agent)

    cmd.execute()


if __name__ == "__main__":
    main()
