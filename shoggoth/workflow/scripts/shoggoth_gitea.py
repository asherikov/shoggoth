#!/usr/bin/env python3
import argparse
import os
import re
import subprocess
import sys
import tempfile
import shutil
import base64
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from urllib.parse import urlencode
from urllib.request import Request, urlopen, HTTPCookieProcessor, build_opener
from urllib.error import URLError, HTTPError

import json

HTTP_TIMEOUT = 30
VERBOSE = False


def log(msg):
    if VERBOSE:
        print(f"[shoggoth-gitea] {msg}", file=sys.stderr, flush=True)


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


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
        body = e.read().decode(errors="replace")[:2000]
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
        body = e.read().decode(errors="replace")[:2000]
        print(f"WARNING: HTTP POST {url} failed: {e.code} {e.reason}: {body}", file=sys.stderr)
        return None
    except (URLError, OSError) as e:
        print(f"WARNING: HTTP POST {url} failed: {e}", file=sys.stderr)
        return None
    except json.JSONDecodeError as e:
        print(f"WARNING: HTTP POST {url} returned invalid JSON: {e}", file=sys.stderr)
        return None


def http_delete(url, headers=None):
    log(f"DELETE {url}")
    req = Request(url, headers=headers or {}, method="DELETE")
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            log(f"DELETE {url} -> {resp.status}")
            return True
    except HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        print(f"WARNING: HTTP DELETE {url} failed: {e.code} {e.reason}: {body}", file=sys.stderr)
        return False
    except (URLError, OSError) as e:
        print(f"WARNING: HTTP DELETE {url} failed: {e}", file=sys.stderr)
        return False


def http_patch_json(url, payload, headers=None):
    data = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    log(f"PATCH {url}")
    req = Request(url, data=data, headers=hdrs, method="PATCH")
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode(errors="replace")[:2000]
        print(f"WARNING: HTTP PATCH {url} failed: {e.code} {e.reason}: {body}", file=sys.stderr)
        return None
    except (URLError, OSError) as e:
        print(f"WARNING: HTTP PATCH {url} failed: {e}", file=sys.stderr)
        return None
    except json.JSONDecodeError as e:
        print(f"WARNING: HTTP PATCH {url} returned invalid JSON: {e}", file=sys.stderr)
        return None


def http_status(url, headers=None):
    req = Request(url, headers=headers or {}, method="GET")
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.status
    except HTTPError as e:
        return e.code
    except (URLError, OSError):
        return 0


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
        self.token = os.environ.get("GITEA_ADMIN_TOKEN")
        if not self.token:
            die("GITEA_ADMIN_TOKEN is required")
        self.domain = os.environ.get("SHOGGOTH_DOMAIN", "")

    def _headers(self):
        return {"Authorization": f"token {self.token}",
                "Content-Type": "application/json",
                "accept": "application/json"}

    def _basic_headers(self):
        creds = base64.b64encode(f"admin:{self.token}".encode()).decode()
        return {"Authorization": f"Basic {creds}",
                "Content-Type": "application/json",
                "accept": "application/json"}

    def get(self, path, params=None):
        return http_get(f"{self.api_url}/{path}", headers=self._headers(), params=params)

    def post(self, path, payload):
        return http_post_json(f"{self.api_url}/{path}", payload, headers={"Authorization": f"token {self.token}"})

    def patch(self, path, payload):
        return http_patch_json(f"{self.api_url}/{path}", payload, headers={"Authorization": f"token {self.token}"})

    def delete(self, path):
        return http_delete(f"{self.api_url}/{path}", headers={"Authorization": f"token {self.token}"})

    def status(self, path):
        return http_status(f"{self.api_url}/{path}", headers={"Authorization": f"token {self.token}"})

    def list_orgs(self):
        return _paginate(lambda page, limit: self.get(
            "orgs", params={"page": page, "limit": limit}))

    def list_org_repos(self, org):
        return _paginate(lambda page, limit: self.get(
            f"orgs/{org}/repos", params={"page": page, "limit": limit}))

    def list_org_members(self, org):
        return _paginate(lambda page, limit: self.get(
            f"orgs/{org}/members", params={"page": page, "limit": limit}))

    def list_org_hooks(self, org):
        return _paginate(lambda page, limit: self.get(
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

    def list_user_repos(self, username):
        return _paginate(lambda page, limit: self.get(
            f"users/{username}/repos", params={"page": page, "limit": limit}))

    def is_org_member(self, org, username):
        return self.status(f"orgs/{org}/members/{username}") == 204

    def list_org_teams(self, org):
        return _paginate(lambda page, limit: self.get(
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

    def put(self, path, payload=None):
        url = f"{self.api_url}/{path}"
        data = json.dumps(payload).encode() if payload else None
        hdrs = self._headers()
        log(f"PUT {url}")
        req = Request(url, data=data, headers=hdrs, method="PUT")
        try:
            with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                log(f"PUT {url} -> {resp.status}")
                return True
        except HTTPError as e:
            body = e.read().decode(errors="replace")[:500]
            print(f"WARNING: HTTP PUT {url} failed: {e.code} {e.reason}: {body}", file=sys.stderr)
            return False
        except (URLError, OSError) as e:
            print(f"WARNING: HTTP PUT {url} failed: {e}", file=sys.stderr)
            return False

    def get_repo(self, repo_full):
        return self.get(f"repos/{repo_full}")

    def repo_exists(self, repo_full):
        code = self.status(f"repos/{repo_full}")
        return code == 200

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

    def list_repo_collaborators(self, repo_full):
        return _paginate(lambda page, limit: self.get(
            f"repos/{repo_full}/collaborators", params={"page": page, "limit": limit}))

    def add_repo_collaborator(self, repo_full, username, permission="write"):
        return self.put(f"repos/{repo_full}/collaborators/{username}",
                        payload={"permission": permission})

    def is_repo_collaborator(self, repo_full, username):
        code = self.status(f"repos/{repo_full}/collaborators/{username}")
        return code == 204

    def get_pr(self, repo_full, pr_number):
        return self.get(f"repos/{repo_full}/pulls/{pr_number}")

    def list_user_tokens(self, username):
        return _paginate(lambda page, limit: http_get(
            f"{self.api_url}/users/{username}/tokens",
            headers=self._basic_headers(),
            params={"page": page, "limit": limit}))

    def create_user_token(self, username, name, scopes):
        return http_post_json(
            f"{self.api_url}/users/{username}/tokens",
            {"name": name, "scopes": scopes},
            headers=self._basic_headers())

    def delete_user_token(self, username, token_id):
        return http_delete(
            f"{self.api_url}/users/{username}/tokens/{token_id}",
            headers=self._basic_headers())

    def list_user_ssh_keys(self, username):
        return _paginate(lambda page, limit: self.get(
            f"users/{username}/keys",
            params={"page": page, "limit": limit}))

    def create_user_ssh_key(self, username, title, key):
        return self.post(f"admin/users/{username}/keys", {
            "title": title,
            "key": key,
        })


class OpenBao:
    def __init__(self):
        self.addr = os.environ.get("OPENBAO_ADDR", "http://openbao:80")
        self.token = os.environ.get("SHOGGOTH_VAULT_TOKEN")
        if not self.token:
            die("SHOGGOTH_VAULT_TOKEN is required")

    def get_value(self, path):
        url = f"{self.addr}/v1/secret/data/{path}"
        headers = {"X-Vault-Token": self.token}
        log(f"GET {url}")
        req = Request(url, headers=headers, method="GET")
        try:
            with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                data = json.loads(resp.read().decode())
                return data.get("data", {}).get("data", {}).get("value")
        except HTTPError as e:
            if e.code == 404:
                return None
            body = e.read().decode(errors="replace")[:500]
            print(f"WARNING: OpenBao GET {path} failed: {e.code} {e.reason}: {body}", file=sys.stderr)
            return None
        except (URLError, OSError, json.JSONDecodeError) as e:
            print(f"WARNING: OpenBao GET {path} failed: {e}", file=sys.stderr)
            return None

    def put_value(self, path, value):
        url = f"{self.addr}/v1/secret/data/{path}"
        payload = json.dumps({"data": {"value": value}}).encode()
        headers = {"X-Vault-Token": self.token, "Content-Type": "application/json"}
        log(f"POST {url}")
        req = Request(url, data=payload, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                log(f"POST {url} -> {resp.status}")
                return True
        except HTTPError as e:
            body = e.read().decode(errors="replace")[:500]
            print(f"WARNING: OpenBao PUT {path} failed: {e.code} {e.reason}: {body}", file=sys.stderr)
            return False
        except (URLError, OSError) as e:
            print(f"WARNING: OpenBao PUT {path} failed: {e}", file=sys.stderr)
            return False


class Redmine:
    def __init__(self, openbao=None):
        server = os.environ.get("REDMINE_SERVER", "")
        if not server:
            domain = os.environ.get("SHOGGOTH_DOMAIN", "")
            if not domain:
                die("REDMINE_SERVER or SHOGGOTH_DOMAIN is required")
            server = f"http://redmine.{domain}"
        self.api_url = server.rstrip("/")
        log(f"Redmine API URL: {self.api_url}")
        self.token = os.environ.get("REDMINE_API_TOKEN")
        if not self.token and os.environ.get("SHOGGOTH_VAULT_TOKEN"):
            if openbao is None:
                openbao = OpenBao()
            self.token = openbao.get_value("redmine/slave-token")
            if self.token:
                log("Using Redmine slave token from OpenBao")
        if not self.token:
            # When REDMINE_SERVER is the api service (http://api.<DOMAIN>/redmine),
            # the web-internal proxy injects X-Redmine-API-Key automatically, so a
            # client token is unnecessary. Allow tokenless mode in that case.
            log("No Redmine client token configured; relying on api service proxy to inject X-Redmine-API-Key")
        else:
            masked = self.token[:4] + "..." + self.token[-4:] if len(self.token) > 8 else "***"
            log(f"Redmine token: {masked} (len={len(self.token)})")
        self._session_cookie = None
        self._openbao = openbao

    def _get_slave_password(self):
        if self._openbao is None:
            self._openbao = OpenBao()
        password = self._openbao.get_value("openldap/slave-password")
        if password:
            log("Using Redmine slave password from OpenBao")
            return password
        die("openldap/slave-password OpenBao secret is required for session auth")

    def _extract_csrf_token(self, html):
        match = re.search(r'name="authenticity_token"\s+value="([^"]+)"', html)
        if not match:
            die("Failed to extract CSRF token from Redmine page")
        return match.group(1)

    def _login(self):
        if self._session_cookie is not None:
            return
        password = self._get_slave_password()
        jar = CookieJar()
        opener = build_opener(HTTPCookieProcessor(jar))
        login_url = f"{self.api_url}/login"
        try:
            with opener.open(Request(login_url), timeout=HTTP_TIMEOUT) as resp:
                html = resp.read().decode()
            csrf_token = self._extract_csrf_token(html)
            post_data = urlencode({
                "username": "slave",
                "password": password,
                "authenticity_token": csrf_token,
            }).encode()
            req = Request(login_url, data=post_data, method="POST",
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
            with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
                if resp.status not in (200, 302):
                    die(f"Redmine login failed: HTTP {resp.status}")
                post_login_html = resp.read().decode()
            cookie_header = ""
            for cookie in jar:
                if cookie.name == "_redmine_session":
                    cookie_header = f"_redmine_session={cookie.value}"
                    break
            if not cookie_header:
                die("Redmine login succeeded but no session cookie received")
            self._session_cookie = cookie_header
            self._csrf_token = self._extract_csrf_token(post_login_html)
            log("Redmine session auth established")
        except (HTTPError, URLError, OSError) as e:
            die(f"Redmine login failed: {e}")

    def _session_headers(self):
        self._login()
        return {"Cookie": self._session_cookie,
                "X-CSRF-Token": self._csrf_token}

    def _session_get(self, path, params=None):
        url = f"{self.api_url}/{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        log(f"Redmine session GET {url}")
        req = Request(url, headers=self._session_headers())
        try:
            with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.read().decode()
        except HTTPError as e:
            body = e.read().decode(errors="replace")[:2000]
            print(f"WARNING: Redmine session GET {url} failed: {e.code} {e.reason}: {body}", file=sys.stderr)
            return None
        except (URLError, OSError) as e:
            print(f"WARNING: Redmine session GET {url} failed: {e}", file=sys.stderr)
            return None

    def _session_post(self, path, form_fields):
        url = f"{self.api_url}/{path}"
        headers = self._session_headers()
        form_fields["authenticity_token"] = self._csrf_token
        data = urlencode(form_fields, doseq=True).encode()
        log(f"Redmine session POST {url}")
        req = Request(url, data=data, headers=headers, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.read().decode()
        except HTTPError as e:
            body = e.read().decode(errors="replace")[:2000]
            print(f"WARNING: Redmine session POST {url} failed: {e.code} {e.reason}: {body}", file=sys.stderr)
            return None
        except (URLError, OSError) as e:
            print(f"WARNING: Redmine session POST {url} failed: {e}", file=sys.stderr)
            return None

    def _headers(self):
        headers = {"Content-Type": "application/json",
                   "accept": "application/json"}
        if self.token:
            headers["X-Redmine-API-Key"] = self.token
        return headers

    def get(self, path, params=None):
        url = f"{self.api_url}/{path}"
        log(f"Redmine GET {url}")
        return http_get(url, headers=self._headers(), params=params)

    def post(self, path, payload):
        url = f"{self.api_url}/{path}"
        log(f"Redmine POST {url}")
        return http_post_json(url, payload, headers=self._headers())

    def patch(self, path, payload):
        url = f"{self.api_url}/{path}"
        log(f"Redmine PATCH {url}")
        return http_patch_json(url, payload, headers=self._headers())

    def delete(self, path):
        return http_delete(f"{self.api_url}/{path}", headers=self._headers())

    def list_webhooks(self):
        html = self._session_get("webhooks")
        if html is None:
            return None
        return [{"id": int(m)} for m in re.findall(r'id="webhook_(\d+)"', html)]

    def list_projects(self):
        data = self.get("projects.json")
        if data is None:
            return None
        return data.get("projects", [])

    def create_webhook(self, url, events, project_ids=None, active=True):
        fields = {
            "webhook[url]": url,
            "webhook[active]": "1" if active else "0",
            "sudo_password": self._get_slave_password(),
            "webhook[events][]": list(events),
        }
        if project_ids is not None:
            fields["webhook[project_ids][]"] = [str(pid) for pid in project_ids]
        return self._session_post("webhooks", fields)

    def delete_webhook(self, webhook_id):
        return self._session_post(f"webhooks/{webhook_id}", {
            "_method": "delete",
            "sudo_password": self._get_slave_password(),
        })


class Github:
    def __init__(self):
        self.api_url = "https://api.github.com"
        self.token = os.environ.get("GITHUB_TOKEN")
        self._headers_cache = None

    def _headers(self):
        if self._headers_cache is None:
            hdrs = {"accept": "application/json"}
            if self.token:
                hdrs["Authorization"] = f"token {self.token}"
            self._headers_cache = hdrs
        return self._headers_cache

    def get(self, path, params=None):
        return http_get(f"{self.api_url}/{path}", headers=self._headers(), params=params)

    def status(self, path):
        url = f"{self.api_url}/{path}"
        req = Request(url, headers=self._headers(), method="GET")
        try:
            with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.status
        except HTTPError as e:
            return e.code
        except (URLError, OSError):
            return 0

    def list_repos(self, account, account_type):
        return _paginate(lambda page, limit: self.get(
            f"{account_type}/{account}/repos",
            params={"page": page, "per_page": limit, "type": "public"}))


class SetupKestraWebhooks:
    def __init__(self, gitea):
        self.gitea = gitea
        self.kestra_host = os.environ.get("KESTRA_HOST")
        if not self.kestra_host:
            domain = self.gitea.domain
            if not domain:
                die("KESTRA_HOST or SHOGGOTH_DOMAIN is required")
            self.kestra_host = f"kestra.{domain}"
        self.errors = 0

    def execute(self, projects=None):
        if not projects:
            orgs = self.gitea.list_orgs()
            if not orgs:
                print("No Gitea projects found")
                return
            projects = [o.get("username") for o in orgs if o.get("username")]

        if not projects:
            print("No Gitea projects found")
            return

        webhooks = [
            (f"http://{self.kestra_host}/api/v1/main/executions/webhook/shoggoth/gitea-pr-update/key",
             ["pull_request_review", "pull_request_review_request", "pull_request_comment"]),
            (f"http://{self.kestra_host}/api/v1/main/executions/webhook/shoggoth/gitea-ci-failure/key",
             ["workflow_run"]),
        ]

        for project in projects:
            existing = self.gitea.list_org_hooks(project)
            if existing:
                for hook in existing:
                    hook_id = hook.get("id")
                    if hook_id is not None:
                        print(f"Removing webhook from {project} (id={hook_id})")
                        if not self.gitea.delete_org_hook(project, hook_id):
                            self.errors += 1

            for url, events in webhooks:
                print(f"Adding webhook to {project}: {url}")
                if not self.gitea.create_org_hook(project, url, events):
                    self.errors += 1

        if self.errors:
            die(f"kestra-webhooks completed with {self.errors} error(s)")


class SetupRedmineWebhooks:
    def __init__(self, gitea):
        self.gitea = gitea
        redmine_server = os.environ.get("REDMINE_SERVER", "")
        if not redmine_server:
            domain = self.gitea.domain
            if not domain:
                die("REDMINE_SERVER or SHOGGOTH_DOMAIN is required")
            redmine_server = f"http://redmine.{domain}"
        self.redmine_host = redmine_server.replace("https://", "").replace("http://", "")
        self.webhook_secret = os.environ.get("REDMINE_WEBHOOK_SECRET", "")
        self.webhook_url = f"http://{self.redmine_host}/forgejo/webhook"
        self.errors = 0

    def execute(self, projects=None):
        if not projects:
            orgs = self.gitea.list_orgs()
            if not orgs:
                print("No Gitea projects found")
                return
            projects = [o.get("username") for o in orgs if o.get("username")]

        if not projects:
            print("No Gitea projects found")
            return

        for project in projects:
            existing = self.gitea.list_org_hooks(project)
            hook_id = None
            if existing:
                for hook in existing:
                    url = hook.get("config", {}).get("url", "")
                    if url.startswith(self.webhook_url):
                        hook_id = hook.get("id")
                        break

            if hook_id is not None:
                print(f"Replacing Redmine webhook in {project} (id={hook_id})")
                if not self.gitea.delete_org_hook(project, hook_id):
                    self.errors += 1
            else:
                print(f"Adding Redmine webhook to {project}")

            if not self.gitea.create_org_hook(
                    project, self.webhook_url,
                    ["push", "pull_request", "issues"],
                    secret=self.webhook_secret or None):
                self.errors += 1

        if self.errors:
            die(f"redmine-webhooks completed with {self.errors} error(s)")


class SetupRedmineKestraWebhooks:
    WEBHOOK_URL = "http://kestra/api/v1/main/executions/webhook/shoggoth/redmine-task-processor/key"
    EVENTS = ["issue.created", "issue.updated"]

    def __init__(self, redmine):
        self.redmine = redmine
        self.errors = 0

    def execute(self):
        # Enumerate projects visible to slave. Slave's /projects.json scope
        # only returns projects slave is a member of (set in redmine-init), so
        # projects created after pod start without explicit membership are
        # missed. Trade-off accepted: avoid requiring a Redmine admin API key.
        projects = self.redmine.list_projects()
        if projects is None:
            die("Failed to list Redmine projects")
        project_ids = [str(p["id"]) for p in projects if p.get("id")]
        if not project_ids:
            die("No Redmine projects found — refusing to create webhook "
                "with empty scope (Redmine webhooks fire only for explicitly "
                "listed projects)")

        existing = self.redmine.list_webhooks()
        if existing is None:
            die("Failed to list Redmine webhooks")

        for hook in existing:
            hook_id = hook.get("id")
            if hook_id is not None:
                print(f"Removing existing webhook (id={hook_id})")
                self.redmine.delete_webhook(hook_id)

        print(f"Creating webhook: {self.WEBHOOK_URL} for {len(project_ids)} project(s)")
        result = self.redmine.create_webhook(self.WEBHOOK_URL, self.EVENTS,
                                             project_ids=project_ids)
        if result is None:
            self.errors += 1

        if self.errors:
            die(f"redmine-kestra-webhooks completed with {self.errors} error(s)")
        print("redmine-kestra-webhooks: webhooks configured successfully")


class GithubMirrorSync:
    def __init__(self, gitea, github):
        self.gitea = gitea
        self.github = github
        self.gitea_host = f"git.{self.gitea.domain}"
        self.tmpdir = None
        self.errors = 0
        self.failures = []
        self.conflicts = []
        self.repos_total = 0
        self.repos_succeeded = 0
        self.repos_skipped = 0
        self.repos_failed = 0

    def execute(self, github_orgs):
        for org in github_orgs:
            self._sync_org(org)
        self._print_summary()
        if self.failures:
            die(self._failure_summary())
        elif self.conflicts:
            die(self._conflict_summary())

    def _sync_org(self, github_org):
        print(f"=== Syncing GitHub '{github_org}' to Gitea org '{github_org}' ===")

        if self.github.status(f"orgs/{github_org}") == 200:
            account_type = "orgs"
        elif self.github.status(f"users/{github_org}") == 200:
            account_type = "users"
        else:
            msg = "GitHub account is neither an organization nor a user"
            print(f"ERROR: {msg}")
            self._record_failure(github_org, msg)
            return

        print(f"GitHub account type: {account_type}")

        repos = self.github.list_repos(github_org, account_type)
        if not repos:
            print(f"WARNING: No repos found for GitHub {account_type} '{github_org}'")
            return

        self.tmpdir = tempfile.mkdtemp()
        try:
            for repo in repos:
                self._sync_repo(github_org, repo)
        finally:
            shutil.rmtree(self.tmpdir, ignore_errors=True)

        print(f"=== Sync complete for '{github_org}' ===")

    _NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

    def _sync_repo(self, github_org, repo_info):
        repo_name = repo_info.get("name", "")
        repo_desc = repo_info.get("description") or ""
        repo_pushed_at = repo_info.get("pushed_at") or ""
        gitea_repo = f"{github_org}/{repo_name}"

        self.repos_total += 1

        if not self._NAME_RE.match(github_org) or not self._NAME_RE.match(repo_name):
            msg = "invalid org/repo name"
            print(f"ERROR: {msg}: {gitea_repo}")
            self._record_failure(gitea_repo, msg)
            return

        print(f"--- Processing {github_org}/{repo_name} ---")

        repo_data = self.gitea.get_repo(gitea_repo)

        if repo_data is None:
            if self._migrate_repo(github_org, repo_name, repo_desc):
                self.repos_succeeded += 1
            else:
                self.repos_failed += 1
        else:
            status = self._sync_existing_repo(github_org, repo_name, repo_pushed_at, repo_data)
            if status == "ok":
                self.repos_succeeded += 1
            elif status == "skipped":
                self.repos_skipped += 1
            else:
                self.repos_failed += 1

    def _migrate_repo(self, github_org, repo_name, repo_desc):
        print(f"Repository {github_org}/{repo_name} does not exist in Gitea, creating...")

        if self.gitea.status(f"orgs/{github_org}") == 404:
            print(f"Creating Gitea organization: {github_org}")
            self.gitea.create_org(github_org, github_org)

        result = self.gitea.migrate_repo(
            f"https://github.com/{github_org}/{repo_name}",
            repo_name, github_org, repo_desc)
        if result is not None:
            print(f"Migrated {github_org}/{repo_name}")
            return True
        self._record_failure(f"{github_org}/{repo_name}", "migration to Gitea failed")
        return False

    def _sync_existing_repo(self, github_org, repo_name, repo_pushed_at, repo_data):
        gitea_repo = f"{github_org}/{repo_name}"
        gitea_updated_at = repo_data.get("updated_at", "") if repo_data else ""

        if repo_pushed_at and gitea_updated_at and repo_pushed_at <= gitea_updated_at:
            print(f"Repository {github_org}/{repo_name} is up to date "
                  f"(GitHub pushed {repo_pushed_at} <= Gitea updated {gitea_updated_at}), skipping")
            return "skipped"

        print(f"Repository {github_org}/{repo_name} exists, syncing branches and tags...")

        clone_url = f"http://{self.gitea_host}/{github_org}/{repo_name}.git"
        clone_dir = os.path.join(self.tmpdir, repo_name)

        cred_file = os.path.join(self.tmpdir, f".git-credentials-{repo_name}")
        with open(cred_file, "w") as f:
            f.write(f"http://token:{self.gitea.token}@{self.gitea_host}\n")
        os.chmod(cred_file, 0o600)

        cred_helper = f"store --file={cred_file}"

        clone = run(["git", "-c", f"credential.helper={cred_helper}",
                     "clone", "--quiet", "--bare", clone_url, clone_dir], check=False)
        if clone.returncode != 0:
            self._record_failure(gitea_repo, "failed to clone from Gitea")
            os.unlink(cred_file)
            return "failed"

        fetch = run(["git", "-C", clone_dir, "fetch", "--quiet", "--tags",
                     f"https://github.com/{github_org}/{repo_name}.git",
                     "+refs/heads/*:refs/remotes/github/*"], check=False)
        if fetch.returncode != 0:
            self._record_failure(gitea_repo, "failed to fetch from GitHub")
            os.unlink(cred_file)
            return "failed"

        changed_branches = []
        repo_had_conflicts = False
        refs = run(["git", "-C", clone_dir, "for-each-ref",
                    "--format=%(refname)", "refs/remotes/github/"], check=False)
        for ref in refs.stdout.strip().splitlines() if refs.stdout else []:
            branch_name = ref.replace("refs/remotes/github/", "")
            if branch_name == "HEAD":
                continue

            remote_sha = run(["git", "-C", clone_dir, "rev-parse", ref],
                              check=False).stdout.strip()

            local_check = run(["git", "-C", clone_dir, "rev-parse", "--verify", "-q",
                               f"refs/heads/{branch_name}"], check=False)
            if local_check.returncode == 0:
                local_sha = local_check.stdout.strip()
                if local_sha == remote_sha:
                    print(f"  Branch '{branch_name}' already up to date")
                    continue
                ancestor = run(["git", "-C", clone_dir, "merge-base", "--is-ancestor",
                                f"refs/heads/{branch_name}", ref], check=False)
                if ancestor.returncode == 0:
                    print(f"  Branch '{branch_name}': fast-forwarding")
                    run(["git", "-C", clone_dir, "branch", "-f", branch_name, ref],
                        check=False)
                    changed_branches.append(branch_name)
                else:
                    print(f"  WARNING: Branch '{branch_name}' has diverged from GitHub, skipping (manual reconciliation needed)")
                    repo_had_conflicts = True
            else:
                run(["git", "-C", clone_dir, "branch", branch_name, ref], check=False)
                changed_branches.append(branch_name)

        status = "ok"
        if changed_branches:
            print(f"  Pushing {len(changed_branches)} changed branch(es) to Gitea")
            push = run(["git", "-C", clone_dir, "-c", f"credential.helper={cred_helper}",
                        "push", "--quiet", "origin"] +
                        changed_branches, check=False)
            if push.returncode != 0:
                self._record_failure(gitea_repo, "failed to push branches to Gitea")
                status = "failed"
        else:
            print(f"  No changed branches to push")

        if status != "failed":
            tags = run(["git", "-C", clone_dir, "tag", "--list"], check=False)
            if tags.stdout.strip():
                print(f"  Pushing tags to Gitea")
                tag_push = run(["git", "-C", clone_dir, "-c", f"credential.helper={cred_helper}",
                               "push", "--quiet", "--tags", "origin"], check=False)
                if tag_push.returncode != 0:
                    self._record_failure(gitea_repo, "failed to push tags to Gitea")
                    status = "failed"
            else:
                print(f"  No tags to push")

        os.unlink(cred_file)
        if repo_had_conflicts:
            self.conflicts.append(gitea_repo)
        print(f"Synced {github_org}/{repo_name}")
        return status

    def _record_failure(self, repo, reason):
        self.errors += 1
        self.failures.append((repo, reason))
        print(f"WARNING: {repo}: {reason}")

    def _print_summary(self):
        print(f"=== Mirror sync summary ===")
        print(f"  Repos processed: {self.repos_total}")
        print(f"  Succeeded:       {self.repos_succeeded}")
        if self.repos_skipped:
            print(f"  Skipped (up to date): {self.repos_skipped}")
        if self.conflicts:
            print(f"  With diverged branches: {len(self.conflicts)} (skipped, manual reconciliation needed)")
        if self.repos_failed:
            print(f"  Failed:          {self.repos_failed}")
        if self.conflicts:
            print(f"\nRepos with diverged branches (no automatic fix possible):")
            for repo in self.conflicts:
                print(f"  - {repo}")

    def _failure_summary(self):
        lines = [f"github-mirror-sync completed with {self.errors} error(s):"]
        for repo, reason in self.failures:
            lines.append(f"  - {repo}: {reason}")
        if self.conflicts:
            lines.append("")
            lines.append(f"Additionally, {len(self.conflicts)} repo(s) have diverged branches (manual reconciliation needed):")
            for repo in self.conflicts:
                lines.append(f"  - {repo}")
        return "\n".join(lines)

    def _conflict_summary(self):
        lines = [f"github-mirror-sync completed with {len(self.conflicts)} repo(s) containing diverged branches (manual reconciliation needed):"]
        for repo in self.conflicts:
            lines.append(f"  - {repo}")
        return "\n".join(lines)


class SlaveAccess:
    def __init__(self, gitea):
        self.gitea = gitea
        self.slave_user = os.environ.get("SHOGGOTH_SLAVE_USER", "slave")
        self.errors = 0

    def execute(self):
        orgs = self.gitea.list_orgs()
        self._add_to_orgs(orgs)
        self._add_to_mirrored_repos(orgs)
        if self.errors:
            die(f"slave-access completed with {self.errors} error(s)")

    def _add_to_orgs(self, orgs):
        if not orgs:
            print("No organizations found")
            return

        for org in orgs:
            org_name = org.get("username")
            if not org_name:
                continue
            print(f"Checking slave user membership in org '{org_name}'")
            if not self.gitea.is_org_member(org_name, self.slave_user):
                print(f"Adding {self.slave_user} to org '{org_name}'")
                if not self.gitea.add_org_member(org_name, self.slave_user):
                    self.errors += 1
            else:
                print(f"  {self.slave_user} is already a member of '{org_name}'")

    def _add_to_mirrored_repos(self, orgs):
        if not orgs:
            print("No organizations found")
            return

        for org in orgs:
            org_name = org.get("username")
            if not org_name:
                continue
            repos = self.gitea.list_org_repos(org_name)
            if not repos:
                continue

            for repo in repos:
                repo_name = repo.get("name")
                full_name = repo.get("full_name")
                if not full_name:
                    continue

                if not self.gitea.is_repo_collaborator(full_name, self.slave_user):
                    print(f"Adding {self.slave_user} as collaborator to repo '{full_name}'")
                    if not self.gitea.add_repo_collaborator(full_name, self.slave_user, "write"):
                        self.errors += 1
                else:
                    print(f"  {self.slave_user} already has access to '{full_name}'")


class SshKey:
    KEY_TITLE = "shoggoth-slave"
    OPENBAO_PATH = "ssh/slave-public-key"

    def __init__(self, gitea, openbao):
        self.gitea = gitea
        self.openbao = openbao
        self.slave_user = os.environ.get("SHOGGOTH_SLAVE_USER", "slave")
        self.errors = 0

    def execute(self):
        public_key = self.openbao.get_value(self.OPENBAO_PATH)
        if not public_key:
            print(f"ERROR: SSH public key not found in OpenBao at '{self.OPENBAO_PATH}'", file=sys.stderr)
            self.errors += 1
            die(f"ssh-key completed with {self.errors} error(s)")

        existing_keys = self.gitea.list_user_ssh_keys(self.slave_user)
        if existing_keys:
            for key in existing_keys:
                if key.get("title") == self.KEY_TITLE:
                    existing_content = key.get("key", "").strip()
                    if existing_content == public_key.strip():
                        print(f"SSH key '{self.KEY_TITLE}' is already registered for '{self.slave_user}', skipping")
                        return
                    print(f"SSH key '{self.KEY_TITLE}' found but content differs, replacing...")
                    key_id = key.get("id")
                    if key_id is not None:
                        if not self.gitea.delete(f"admin/users/{self.slave_user}/keys/{key_id}"):
                            self.errors += 1
                    break

        print(f"Registering SSH key '{self.KEY_TITLE}' for user '{self.slave_user}'")
        if not self.gitea.create_user_ssh_key(self.slave_user, self.KEY_TITLE, public_key):
            self.errors += 1

        if self.errors:
            die(f"ssh-key completed with {self.errors} error(s)")
        print(f"ssh-key: public key registered successfully")


class VerifyApi:
    def __init__(self):
        self.errors = 0

    def _check_redmine(self):
        openbao = OpenBao() if not os.environ.get("REDMINE_API_TOKEN") else None
        redmine = Redmine(openbao)
        print(f"Redmine API URL: {redmine.api_url}")
        masked = redmine.token[:4] + "..." + redmine.token[-4:] if len(redmine.token) > 8 else "***"
        print(f"Redmine token: {masked} (len={len(redmine.token)})")

        print("\n--- Redmine GET / (connectivity) ---")
        status = http_status(redmine.api_url + "/")
        if status == 200:
            print(f"OK (HTTP {status})")
        else:
            print(f"FAIL: HTTP {status}")
            self.errors += 1

        print("\n--- Redmine GET /my/account.json (token auth check) ---")
        account = redmine.get("my/account.json")
        if account is None:
            print("FAIL: /my/account.json — token may be invalid or expired")
            self.errors += 1
        else:
            user = account.get("user", {})
            print(f"OK: authenticated as '{user.get('login', '?')}' (id={user.get('id', '?')})")

        print("\n--- Redmine GET /projects.json ---")
        projects = redmine.list_projects()
        if projects is None:
            print("FAIL: /projects.json")
            self.errors += 1
        else:
            print(f"OK: {len(projects)} project(s)")

        print("\n--- Redmine GET /webhooks (session auth) ---")
        webhooks = redmine.list_webhooks()
        if webhooks is None:
            print("FAIL: /webhooks — webhooks may not be enabled in Settings > Integrations or session auth failed")
            self.errors += 1
        else:
            print(f"OK: {len(webhooks)} webhook(s) found")
            for hook in webhooks:
                print(f"  id={hook.get('id')}")

    def _check_gitea(self):
        gitea = Gitea()
        print(f"\nGitea API URL: {gitea.api_url}")
        masked = gitea.token[:4] + "..." + gitea.token[-4:] if len(gitea.token) > 8 else "***"
        print(f"Gitea token: {masked} (len={len(gitea.token)})")

        print("\n--- Gitea GET /api/v1/version ---")
        version = gitea.get("version")
        if version is None:
            print("FAIL: /api/v1/version returned no data")
            self.errors += 1
        else:
            print(f"OK: Gitea version {version.get('version', '?')}")

    def execute(self, services=None):
        if not services or "redmine" in services:
            self._check_redmine()
        if not services or "gitea" in services:
            self._check_gitea()

        if self.errors:
            die(f"verify-api completed with {self.errors} error(s)")
        print("\nverify-api: all checks passed")


class SlaveToken:
    TOKEN_NAME = "shoggoth-slave"
    TOKEN_SCOPES = ["write:repository", "write:issue"]
    OPENBAO_PATH = "gitea/slave-token"

    def __init__(self, gitea, openbao):
        self.gitea = gitea
        self.openbao = openbao
        self.slave_user = os.environ.get("SHOGGOTH_SLAVE_USER", "slave")
        self.errors = 0

    def _is_token_expired(self, token):
        expires_at = token.get("expires_at", "")
        if not expires_at:
            return False
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return True
        return expiry <= datetime.now(timezone.utc)

    def _find_existing_token(self):
        existing = self.gitea.list_user_tokens(self.slave_user)
        if not existing:
            return None
        for tok in existing:
            if tok.get("name") == self.TOKEN_NAME:
                return tok
        return None

    def execute(self):
        existing_tok = self._find_existing_token()
        if existing_tok is not None:
            if self._is_token_expired(existing_tok):
                tok_id = existing_tok.get("id")
                if tok_id is not None:
                    print(f"Deleting expired token '{self.TOKEN_NAME}' (id={tok_id})")
                    if not self.gitea.delete_user_token(self.slave_user, tok_id):
                        self.errors += 1
                        return
            else:
                print(f"Token '{self.TOKEN_NAME}' is still valid, skipping creation")
                return

        print(f"Creating token '{self.TOKEN_NAME}' for user '{self.slave_user}'")
        result = self.gitea.create_user_token(self.slave_user, self.TOKEN_NAME, self.TOKEN_SCOPES)
        if result is None or not result.get("sha1"):
            print(f"ERROR: Failed to create token for {self.slave_user}", file=sys.stderr)
            self.errors += 1
            if self.errors:
                die(f"slave-token completed with {self.errors} error(s)")
            return

        token_value = result["sha1"]

        print(f"Storing token in OpenBao at '{self.OPENBAO_PATH}'")
        if not self.openbao.put_value(self.OPENBAO_PATH, token_value):
            print(f"ERROR: Failed to store token in OpenBao", file=sys.stderr)
            self.errors += 1

        if self.errors:
            die(f"slave-token completed with {self.errors} error(s)")
        print(f"slave-token: token created and stored successfully")


def main():
    parser = argparse.ArgumentParser(
        prog="shoggoth_gitea.py",
        description="Shoggoth Gitea maintenance: webhooks, mirror sync, and slave user access",
    )
    parser.add_argument("command",
                        choices=["kestra-webhooks", "redmine-webhooks", "redmine-kestra-webhooks",
                                 "github-mirror-sync", "slave-access", "slave-token", "ssh-key",
                                 "verify-api"],
                        help="Command to execute")
    parser.add_argument("args", nargs="*", default=[],
                        help="Command arguments (project names for webhooks, github orgs for mirror-sync)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose logging to stderr")

    parsed = parser.parse_args()

    global VERBOSE
    VERBOSE = parsed.verbose

    if parsed.command == "kestra-webhooks":
        gitea = Gitea()
        cmd = SetupKestraWebhooks(gitea)
        cmd.execute(parsed.args or None)
    elif parsed.command == "redmine-webhooks":
        gitea = Gitea()
        cmd = SetupRedmineWebhooks(gitea)
        cmd.execute(parsed.args or None)
    elif parsed.command == "redmine-kestra-webhooks":
        openbao = OpenBao()
        redmine = Redmine(openbao)
        cmd = SetupRedmineKestraWebhooks(redmine)
        cmd.execute()
    elif parsed.command == "github-mirror-sync":
        if not parsed.args:
            die("github-mirror-sync requires at least one github organization name")
        gitea = Gitea()
        github = Github()
        cmd = GithubMirrorSync(gitea, github)
        cmd.execute(parsed.args)
    elif parsed.command == "slave-access":
        gitea = Gitea()
        cmd = SlaveAccess(gitea)
        cmd.execute()
    elif parsed.command == "slave-token":
        gitea = Gitea()
        openbao = OpenBao()
        cmd = SlaveToken(gitea, openbao)
        cmd.execute()
    elif parsed.command == "ssh-key":
        gitea = Gitea()
        openbao = OpenBao()
        cmd = SshKey(gitea, openbao)
        cmd.execute()
    elif parsed.command == "verify-api":
        cmd = VerifyApi()
        cmd.execute(parsed.args or None)


if __name__ == "__main__":
    main()