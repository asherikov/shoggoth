#!/usr/bin/env python3
import argparse
import base64
import io
import json
import os
import re
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


WSHANDLER_BIN = "/ccws/ccws/tools/bin/wshandler"


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
    req = Request(url, data=data, headers=hdrs, method="PATCH")
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        print(f"WARNING: HTTP PATCH {url} failed: {e.code} {e.reason}: {body}", file=sys.stderr)
        return None
    except (URLError, OSError) as e:
        print(f"WARNING: HTTP PATCH {url} failed: {e}", file=sys.stderr)
        return None
    except json.JSONDecodeError as e:
        print(f"WARNING: HTTP PATCH {url} returned invalid JSON: {e}", file=sys.stderr)
        return None


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
        self.token = os.environ["GITEA_SLAVE_TOKEN"]

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
        return "review" in self._payload

    def is_review_by_slave_user(self):
        slave_user = os.environ.get("SHOGGOTH_SLAVE_USER", "sslave")
        review = self._payload.get("review", {})
        if review.get("user", {}).get("login") == slave_user:
            return True
        sender = self._payload.get("sender", {})
        if sender.get("login") == slave_user:
            return True
        return False

    def is_slave_user_reviewer(self):
        slave_user = os.environ.get("SHOGGOTH_SLAVE_USER", "sslave")
        pr = self._payload.get("pull_request", {})
        requested = pr.get("requested_reviewers", [])
        if any(r.get("login") == slave_user for r in requested):
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

    def get_ci_logs(self, repo, run_id):
        if not run_id:
            return "CI logs unavailable: no run ID in payload"
        url = f"{self.api_url}/repos/{repo}/actions/runs/{run_id}/logs"
        req = Request(url, headers={"Authorization": f"token {self.token}"})
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
        headers = {"Authorization": f"token {self.token}",
                   "Content-Type": "application/json"}
        return http_get(f"{self.api_url}/{path}", headers=headers, params=params)

    def post(self, path, payload):
        headers = {"Authorization": f"token {self.token}"}
        return http_post_json(f"{self.api_url}/{path}", payload, headers=headers)

    def patch(self, path, payload):
        headers = {"Authorization": f"token {self.token}"}
        return http_patch_json(f"{self.api_url}/{path}", payload, headers=headers)

    def delete(self, path, payload):
        headers = {"Authorization": f"token {self.token}"}
        return http_delete_json(f"{self.api_url}/{path}", payload, headers=headers)

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
        self.patch(f"repos/{repo_full}", {"has_pull_requests": True})

    def get_unresolved_review_comments(self, repo, pr_number):
        slave_user = os.environ.get("SHOGGOTH_SLAVE_USER", "sslave")
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

        threads = {}
        for c in unresolved:
            path = c.get("path", "unknown")
            line = c.get("line")
            thread_key = (path, line)
            threads.setdefault(thread_key, []).append(c)

        result = []
        for thread_key, thread_comments in threads.items():
            path, line = thread_key
            has_non_slave = any(
                c.get("user", {}).get("login") != slave_user
                for c in thread_comments)
            if not has_non_slave:
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
                "line": line if line is not None else "unknown",
                "body": combined_body,
            })

        return result

    def resolve_comment(self, repo, comment_id):
        self.post(f"repos/{repo}/pulls/comments/{comment_id}/resolve", {})

    def reply_to_comment(self, repo, pr_number, comment_id, body):
        return self.post(
            f"repos/{repo}/pulls/{pr_number}/comments/{comment_id}/replies",
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


class Redmine:
    def __init__(self):
        self._projects = None

    def match_project(self, normalized):
        if self._projects is None:
            result = run(["redmine", "projects", "list", "--output=json"], check=False)
            if result.returncode != 0:
                die(f"failed to list redmine projects: {result.stderr}")
            try:
                self._projects = json.loads(result.stdout)
            except json.JSONDecodeError:
                die("failed to parse redmine projects list as JSON")

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
        result = run(["redmine", "issues", "get", str(task_id),
                      "--journals", "--children", "--output=json"], check=False)
        if result.returncode != 0:
            die(f"failed to get redmine issue #{task_id}: {result.stderr}")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            die(f"failed to parse redmine issue #{task_id} as JSON")

    def get_task_project(self, task_id):
        task = self.get_issue(task_id)
        task_project = task.get("project", {}).get("name", "")
        if not task_project:
            die(f"task #{task_id} has no project assigned")
        return normalize_for_branch(task_project)

    def get_project_repo(self, project_id, domain):
        result = run(
            ["redmine", "projects", "show", str(project_id), "--output=json"],
            check=False,
        )
        if result.returncode != 0:
            die(f"failed to fetch redmine project info: {result.stderr}")
        try:
            project_info = json.loads(result.stdout)
        except json.JSONDecodeError:
            die("failed to parse redmine project info as JSON")

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
        result = run(["redmine", "issues", "update", str(task_id)] + list(args), check=False)
        if result.returncode != 0:
            die(f"failed to update redmine issue #{task_id}: {result.stderr}")
        return result

    def list_issues(self, project):
        result = run(["redmine", "issues", "list", "--project", project,
                       "--limit=100", "--output=json"], check=False)
        if result.returncode != 0:
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return None


class Shoggoth:
    def __init__(self, redmine, gitea, args):
        self.project = None
        self.domain = os.environ["SHOGGOTH_DOMAIN"]
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
            wsh_args = ["-s", f"s|https://github.com|http://git.{self.domain}|g",
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

            log("checkout: running make dep_install")
            dep_install = run(["sudo", "-S", "make", "dep_install"], cwd="/ccws", input="ccws\n")
            if dep_install.stdout:
                print(dep_install.stdout, file=sys.stderr, flush=True)
            if dep_install.stderr:
                print(dep_install.stderr, file=sys.stderr, flush=True)

        self.repo_dir = workspace_dir


class OtlpLogger:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.session_id = None
        self.service_name = None
        self.log_thread = None
        self.last_result = None
        self.assistant_text = []

    def _push_line(self, line):
        ts = str(time.time_ns())
        body = {
            "resourceLogs": [{
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": self.service_name}},
                        {"key": "session.id", "value": {"stringValue": self.session_id}},
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
        http_post_json(f"{self.endpoint}/v1/logs", body, quiet=True)

    def start_session(self, event_type):
        self.session_id = str(uuid.uuid4())
        self.service_name = f"qwen-{event_type}"
        self.last_result = None
        self.assistant_text = []
        log(f"otlp: start_session session_id={self.session_id}")

    def stop_session(self):
        if self.log_thread is not None:
            self.log_thread.join(timeout=30)
            self.log_thread = None
        self.session_id = None
        log("otlp: stop_session done")

    def forward_stream(self, stream):
        try:
            for line in stream:
                line = line.decode(errors="replace").rstrip("\n")
                if line:
                    self._push_line(line)
                    self._capture_stream_json(line)
        except Exception as e:
            print(f"WARNING: otlp forward thread crashed: {e}", file=sys.stderr)

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
            if not event.get("is_error"):
                self.last_result = event.get("result", "")

    def get_review_text(self):
        if self.last_result:
            return self.last_result
        return "\n\n".join(self.assistant_text) if self.assistant_text else ""

    def start_forwarding(self, stream):
        self.log_thread = threading.Thread(
            target=self.forward_stream,
            args=(stream,),
            daemon=True,
        )
        self.log_thread.start()


class Agent:
    def __init__(self, shoggoth):
        self.shoggoth = shoggoth
        self.otlp = OtlpLogger(os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"])

    def start_session(self, event_type):
        log(f"start_session: event_type={event_type}")
        self.otlp.start_session(event_type)

        SECRET_ENV_KEYS = frozenset([
            "GITEA_ADMIN_TOKEN", "GITEA_SLAVE_TOKEN",
            "SHOGGOTH_VAULT_TOKEN", "REDMINE_TOKEN",
            "OPENBAO_ADDR", "OLLAMA_CLOUD_TOKEN",
            "GITHUB_TOKEN", "REDMINE_WEBHOOK_SECRET",
        ])
        self.qwen_env = {k: v for k, v in os.environ.items()
                         if k not in SECRET_ENV_KEYS}

        plugin_url = f"http://{self.shoggoth.domain}/plugin.tar.gz"
        log(f"start_session: downloading plugin from {plugin_url}")
        plugin_archive = run(["curl", "-sfS", "--max-time", "30", "-o", "/tmp/plugin.tar.gz", plugin_url],
                             check=False, capture_output=True, text=True, timeout=60)
        if plugin_archive.returncode != 0:
            print(f"WARNING: failed to download plugin from {plugin_url}: {plugin_archive.stderr}", file=sys.stderr)
        else:
            log(f"start_session: installing plugin from /tmp/plugin.tar.gz")
            run(["qwen", "extensions", "install", "/tmp/plugin.tar.gz",
                 "--scope", "user", "--consent"],
                check=False, capture_output=True, text=True, timeout=60)

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

    def prompt(self, text, resume=False, timeout=None):
        if self.shoggoth.type == "ccws":
            os.chdir("/ccws")
        else:
            os.chdir(self.shoggoth.repo_dir)

        cmd = ["qwen", "--yolo", "--output-format", "stream-json"]
        if resume:
            cmd.extend(["--resume", self.otlp.session_id])
        else:
            cmd.extend(["--session-id", self.otlp.session_id])
        cmd.extend(["--prompt", text])

        log(f"prompt: resume={resume} session={self.otlp.session_id} timeout={timeout}")
        log(f"prompt: cmd={' '.join(cmd[:6])}... (prompt length={len(text)})")
        log("prompt: starting qwen subprocess")

        qwen = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.qwen_env,
        )
        self.otlp.start_forwarding(qwen.stdout)
        timed_out = False
        try:
            stderr = qwen.communicate(timeout=timeout)[1]
        except subprocess.TimeoutExpired:
            log(f"prompt: TIMEOUT after {timeout}s, killing qwen")
            qwen.kill()
            timed_out = True
            stderr = qwen.communicate()[1]
        self.otlp.log_thread.join(timeout=10)
        if stderr:
            print(f"qwen stderr: {stderr.decode(errors='replace')}", file=sys.stderr)
        if timed_out:
            log("prompt: qwen killed due to timeout")
            return 124
        log(f"prompt: qwen exited with code {qwen.returncode}")
        return qwen.returncode


    def stop_session(self):
        log("stop_session")
        self.otlp.stop_session()


class CommandBase:
    def __init__(self, gitea, redmine, agent, shoggoth=None):
        self.gitea = gitea
        self.redmine = redmine
        self.agent = agent
        self.shoggoth = shoggoth


class TaskCommand(CommandBase):
    def __init__(self, task_id, shoggoth, gitea, redmine, agent):
        super().__init__(gitea, redmine, agent, shoggoth=shoggoth)
        self.task_id = task_id

    @staticmethod
    def _sanitize_subject(subject):
        return subject.replace("\n", " ").replace("\r", "")[:200]

    def create_pull_request(self, repo, branch, task_id, title, base="main"):
        self.gitea.ensure_pull_requests_enabled(repo)
        slave_user = os.environ.get("SHOGGOTH_SLAVE_USER", "sslave")
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

        run(["git", "-C", repo_dir, "checkout", "-B", branch], check=False)
        add = run(["git", "-C", repo_dir, "add", "-A"], check=False)
        if add.returncode != 0:
            die(f"failed to stage changes: {add.stderr}")

        staged = run(["git", "-C", repo_dir, "diff", "--cached", "--name-only"], check=False)
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
            run(["git", "-C", repo_path, "add", "-A"], check=False)

        wsh(repo_dir, ["commit", commit_msg], check=False)
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

        self.agent.start_session("task")

        task_data = {
            "subject": task.get("subject", ""),
            "description": task.get("description", ""),
            "custom_fields": task.get("custom_fields", []),
        }

        prompt = (
            f"Execute the following task: {task_subject}\n\n"
            f"{json.dumps(task_data, indent=2)}\n\n"
            f"Use the codebase-memory-mcp MCP tools (search_graph, "
            f"get_code_snippet, trace_path) to explore the codebase and "
            f"understand the relevant code before making changes.\n\n"
            f"Leave all changes uncommitted in the working tree.\n\n"
            f"Update basic memory with any new information learned about "
            f"the project {self.shoggoth.project} and task \"{task_subject}\"."
        )

        rc = self.agent.prompt(prompt)
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
            redmine_update_args = ["--note", "No changes produced by agent"]

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
            f"Use the codebase-memory-mcp MCP tools (search_graph, "
            f"get_code_snippet, trace_path) to understand the code related "
            f"to the failure. Fix the code, commit, and push."
        )

        rc = self.agent.prompt(prompt)
        if rc != 0:
            die(f"qwen agent exited with code {rc}")

        self.agent.stop_session()


class PrUpdateCommand(CommandBase):
    def __init__(self, shoggoth, gitea, redmine, agent):
        super().__init__(gitea, redmine, agent, shoggoth=shoggoth)

    def execute(self):
        action = self.gitea.get_pr_action()
        log(f"pr-update: action={action}")
        if action in ("deleted", "review_request_removed"):
            return

        pr_url = self.gitea.get_pr_url()
        log(f"pr-update: pr_url={pr_url}")

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
        pr_files = self.gitea.get_pr_files(pr_repo, pr_number)
        pr_commits = self.gitea.get_pr_commits(pr_repo, pr_number)

        pr_title = pr_data.get("title", "") if pr_data else ""
        pr_body = pr_data.get("body", "") if pr_data else ""
        pr_base = pr_data.get("base", {}).get("ref", "") if pr_data else ""
        pr_head = pr_data.get("head", {}).get("ref", "") if pr_data else ""

        files_summary = []
        if pr_files:
            for f in pr_files:
                files_summary.append(
                    f"  {f.get('status', '?')}: {f.get('filename', '?')} "
                    f"(+{f.get('additions', 0)} -{f.get('deletions', 0)})")
        files_text = "\n".join(files_summary) if files_summary else "N/A"

        commits_summary = []
        if pr_commits:
            for c in pr_commits:
                sha = c.get("sha", "?")[:8]
                msg = c.get("commit", {}).get("message", "").split("\n")[0]
                commits_summary.append(f"  {sha} {msg}")
        commits_text = "\n".join(commits_summary) if commits_summary else "N/A"

        self.agent.start_session("pr-review")

        prompt = (
            f"Review the pull request at {pr_url}.\n\n"
            f"Title: {pr_title}\n"
            f"Branch: {pr_head} -> {pr_base}\n"
            f"Description: {pr_body}\n\n"
            f"Changed files:\n{files_text}\n\n"
            f"Commits:\n{commits_text}\n\n"
            f"Use the codebase-memory-mcp MCP tools (search_graph, "
            f"get_code_snippet, trace_path) to understand the code context "
            f"and trace the impact of the changes. "
            f"Provide feedback on correctness, potential bugs, and design issues."
        )

        rc = self.agent.prompt(prompt)
        if rc != 0:
            die(f"qwen agent exited with code {rc}")

        review_text = self.agent.otlp.get_review_text()
        self.agent.stop_session()

        if review_text:
            log(f"pr-review: posting review to gitea (length={len(review_text)})")
            result = self.gitea.post_pr_review_chunked(pr_repo, pr_number, review_text)
            if result is None:
                die("pr-review: failed to post review to gitea")
        else:
            log("pr-review: no review text captured from agent")

        slave_user = os.environ.get("SHOGGOTH_SLAVE_USER", "sslave")
        log(f"pr-review: removing requested reviewer {slave_user}")
        self.gitea.remove_requested_reviewer(pr_repo, pr_number, slave_user)

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

    def _commit_and_push_standalone(self, repo_dir, branch, commit_msg):
        status = run(["git", "-C", repo_dir, "status", "--porcelain"], check=False)
        if status.returncode != 0:
            die(f"git status failed in {repo_dir}: {status.stderr}")
        if not status.stdout.strip():
            return False

        run(["git", "-C", repo_dir, "add", "-A"], check=False)
        staged = run(["git", "-C", repo_dir, "diff", "--cached", "--name-only"], check=False)
        if not staged.stdout.strip():
            return False

        commit = run(["git", "-C", repo_dir, "commit", "-m", commit_msg], check=False)
        if commit.returncode != 0:
            die(f"git commit failed: {commit.stderr}")

        push = run(["git", "-C", repo_dir, "push", "origin", branch], check=False)
        if push.returncode != 0:
            die(f"failed to push branch '{branch}': {push.stderr}")
        return True

    def _commit_and_push_ccws(self, repo_dir, branch, commit_msg):
        status = wsh_status(repo_dir, quiet=True)
        if not status.stdout.strip():
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
            return False

        for repo_name in modified_repos:
            repo_path = os.path.join(repo_dir, repo_name)
            run(["git", "-C", repo_path, "add", "-A"], check=False)

        wsh(repo_dir, ["commit", commit_msg], check=False)
        push = wsh(repo_dir, ["-p", "version", "push"] + modified_repos, check=False)
        if push.returncode != 0:
            die(f"failed to push branches: {push.stderr}")
        return True

    def _commit_and_push(self, commit_msg=None):
        repo_dir = self.shoggoth.repo_dir
        pr_branch = self.shoggoth.working_branch
        if commit_msg is None:
            commit_msg = f"Address review comments on PR#{self.gitea.get_pr_number()}"

        if self.shoggoth.type == "ccws":
            return self._commit_and_push_ccws(repo_dir, pr_branch, commit_msg)
        return self._commit_and_push_standalone(repo_dir, pr_branch, commit_msg)

    def _push_pending_commits(self):
        repo_dir = self.shoggoth.repo_dir
        pr_branch = self.shoggoth.working_branch
        if self.shoggoth.type == "ccws":
            wsh(repo_dir, ["-p", "version", "push"], check=False)
        else:
            run(["git", "-C", repo_dir, "push", "origin", pr_branch], check=False)

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

        self.agent.start_session("pr-comment")

        rc = self.agent.prompt(
            f"Load memories regarding the project {self.shoggoth.project} from basic memory. "
            f"Proceed if memory is not available.")
        if rc != 0:
            die(f"qwen agent exited with code {rc}")

        if task_subject:
            rc = self.agent.prompt(
                f"Load memories regarding task \"{task_subject}\" "
                f"in project {self.shoggoth.project} from basic memory. "
                f"Proceed if memory is not available.", resume=True)
            if rc != 0:
                die(f"qwen agent exited with code {rc}")

        resolved_comments = []
        for comment in unresolved:
            c_path = comment.get("path", "unknown")
            c_line = comment.get("line", "unknown")
            c_body = comment.get("body", "")
            prompt = (
                f"Address the following review comment on PR {pr_url} "
                f"(file: {c_path}, line: {c_line}): "
                f"{c_body}. Use the codebase-memory-mcp MCP tools "
                f"(search_graph, get_code_snippet, trace_path) to understand "
                f"the code context around the comment. "
                f"Leave all changes uncommitted in the working tree."
            )
            rc = self.agent.prompt(prompt, resume=True, timeout=AGENT_TIMEOUT)
            if rc == 124:
                self._handle_timeout(pr_repo, pr_number, pr_url, resolved_comments)
            if rc != 0:
                die(f"qwen agent exited with code {rc}")

            comment_id = comment.get("id")
            commit_msg = (
                f"Address review comment on PR#{pr_number} "
                f"({c_path}:{c_line})"
            )
            pushed = self._commit_and_push(commit_msg)
            if pushed:
                resolved_comments.append(comment)
                if comment_id is not None:
                    self.gitea.resolve_comment(pr_repo, comment_id)
                    log(f"pr-comment: resolved comment {comment_id}")
            elif comment_id is not None:
                self.gitea.reply_to_comment(
                    pr_repo, pr_number, comment_id,
                    "No changes produced for this comment.")
                log(f"pr-comment: no changes for comment {comment_id}, posted note")

        rc = self.agent.prompt(
            f"Finalize all remaining work. Update basic memory with any new information "
            f"learned about the project {self.shoggoth.project}.",
            resume=True, timeout=AGENT_TIMEOUT)
        if rc == 124:
            self._handle_timeout(pr_repo, pr_number, pr_url, resolved_comments)
        if rc != 0:
            die(f"qwen agent exited with code {rc}")

        if task_subject:
            rc = self.agent.prompt(
                f"Update basic memory with any new information learned about "
                f"the task \"{task_subject}\".", resume=True, timeout=AGENT_TIMEOUT)
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
            if changes_produced:
                self.redmine.update_issue(redmine_task_id,
                     "--status", "Resolved",
                     "--note", f"Review comments on {pr_url} have been addressed.")
            else:
                self.redmine.update_issue(redmine_task_id,
                     "--note", f"Review comments on {pr_url} have been addressed.")

    def _handle_timeout(self, pr_repo, pr_number, pr_url, resolved_comments):
        log(f"pr-comment: handling timeout, resolved so far={len(resolved_comments)}")
        self._push_pending_commits()
        self.agent.stop_session()
        resolved_count = len(resolved_comments)
        timeout_msg = (
            f"Agent timed out after {AGENT_TIMEOUT // 60} minutes. "
            f"Addressed {resolved_count} comment(s) before timeout. "
            f"Committed changes have been pushed."
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
