#!/usr/bin/env python3
import argparse
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import shutil
import base64
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from urllib.parse import urlencode, urlsplit
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


def k8s_upsert_secret(name, namespace, data):
    """Create or patch a Kubernetes Secret via kubectl.

    `data` is a dict of key -> raw string value; base64 encoding is handled
    by kubectl. The caller is responsible for RBAC (the
    shoggoth-maintenance-secret-writer ServiceAccount grants namespaced
    create/update/patch on the specific Secret names in
    shoggoth/k3s/maintenance-secret-writer.yaml).
    """
    if not name or not namespace:
        print("ERROR: k8s_upsert_secret requires name and namespace", file=sys.stderr)
        return False
    args = ["kubectl", "create", "secret", "generic", name,
            f"--namespace={namespace}",
            "--type=Opaque",
            "--output=json",
            "--dry-run=client"]
    for key, value in data.items():
        args.append(f"--from-literal={key}={value}")
    create = run(args, check=False)
    if create.returncode != 0:
        print(f"ERROR: kubectl create secret (dry-run) failed: {create.stderr.strip()}", file=sys.stderr)
        return False
    apply = run(["kubectl", "apply", "-f", "-"], input=create.stdout, check=False)
    if apply.returncode != 0:
        print(f"ERROR: kubectl apply secret failed: {apply.stderr.strip()}", file=sys.stderr)
        return False
    return True


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


def http_post_json_with_status(url, payload, headers=None):
    data = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    log(f"POST {url}")
    req = Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode(errors="replace")[:2000]
            log(f"POST {url} -> {resp.status}")
            try:
                return resp.status, body, json.loads(body) if body else None
            except json.JSONDecodeError:
                return resp.status, body, None
    except HTTPError as e:
        body = e.read().decode(errors="replace")[:2000]
        log(f"POST {url} -> {e.code}")
        return e.code, body, None
    except (URLError, OSError) as e:
        return 0, str(e), None


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

    def list_branches(self, repo_full):
        return _paginate(lambda page, limit: self.get(
            f"repos/{repo_full}/branches", params={"page": page, "limit": limit}))

    def list_tags(self, repo_full):
        return _paginate(lambda page, limit: self.get(
            f"repos/{repo_full}/tags", params={"page": page, "limit": limit}))

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
        url = f"{self.api_url}/admin/users/{username}/keys"
        status, body, parsed = http_post_json_with_status(
            url,
            {"title": title, "key": key},
            headers={"Authorization": f"token {self.token}"})
        if 200 <= status < 300:
            return True, status, body, parsed
        print(f"WARNING: HTTP POST {url} failed: {status}: {body}",
              file=sys.stderr)
        return False, status, body, parsed

    def ensure_user_active(self, username):
        if self.patch(f"admin/users/{username}",
                      {"active": True, "login_name": username}) is None:
            print(f"WARNING: failed to ensure '{username}' is active",
                  file=sys.stderr)
            return False
        return True

    def list_admin_keys(self):
        return _paginate(lambda page, limit: self.get(
            "admin/keys", params={"page": page, "limit": limit}))

    def delete_admin_key(self, key_id):
        return self.delete(f"admin/keys/{key_id}")

    def list_admin_users(self):
        first = self.get("admin/users", params={"page": 1, "limit": 1})
        if not isinstance(first, list):
            return None
        if len(first) == 0:
            return []
        return _paginate(lambda page, limit: self.get(
            "admin/users", params={"page": page, "limit": limit}))


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
            # client token is unnecessary. For direct redmine access (e.g.
            # http://redmine.<DOMAIN>) the token is mandatory — fail loudly
            # rather than silently issuing unauthenticated requests that 401 later.
            from urllib.parse import urlparse
            hostname = urlparse(self.api_url).hostname or ""
            if hostname.startswith("api."):
                log("No Redmine client token configured; relying on api service proxy to inject X-Redmine-API-Key")
            else:
                die(f"Redmine client token is required to access {self.api_url} "
                    "(set REDMINE_API_TOKEN or ensure redmine/slave-token is in OpenBao)")
        else:
            masked = self.token[:4] + "..." + self.token[-4:] if len(self.token) > 8 else "***"
            log(f"Redmine token: {masked} (len={len(self.token)})")
        self._session_cookie = None
        self._admin_session_cookie = None
        self._admin_csrf_token = None
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

    def _admin_login(self):
        if self._admin_session_cookie is not None:
            return
        if self._openbao is None:
            self._openbao = OpenBao()
        password = self._openbao.get_value("openldap/admin-password")
        if not password:
            die("openldap/admin-password OpenBao secret is required for redmine admin session auth")
        jar = CookieJar()
        opener = build_opener(HTTPCookieProcessor(jar))
        login_url = f"{self.api_url}/login"
        try:
            with opener.open(Request(login_url), timeout=HTTP_TIMEOUT) as resp:
                html = resp.read().decode()
            csrf_token = self._extract_csrf_token(html)
            post_data = urlencode({
                "username": "admin",
                "password": password,
                "authenticity_token": csrf_token,
            }).encode()
            req = Request(login_url, data=post_data, method="POST",
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
            with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
                if resp.status not in (200, 302):
                    die(f"Redmine admin login failed: HTTP {resp.status}")
                post_login_html = resp.read().decode()
            cookie_header = ""
            for cookie in jar:
                if cookie.name == "_redmine_session":
                    cookie_header = f"_redmine_session={cookie.value}"
                    break
            if not cookie_header:
                die("Redmine admin login succeeded but no session cookie received")
            self._admin_session_cookie = cookie_header
            self._admin_csrf_token = self._extract_csrf_token(post_login_html)
            log("Redmine admin session auth established")
        except (HTTPError, URLError, OSError) as e:
            die(f"Redmine admin login failed: {e}")

    def _admin_session_post(self, path, form_fields,
                            success_url_contains="/users/",
                            failure_url_contains="/users/new"):
        url = f"{self.api_url}/{path}"
        if self._admin_session_cookie is None:
            self._admin_login()
        headers = {"Cookie": self._admin_session_cookie,
                   "X-CSRF-Token": self._admin_csrf_token}
        form_fields["authenticity_token"] = self._admin_csrf_token
        data = urlencode(form_fields, doseq=True).encode()
        log(f"Redmine admin session POST {url}")
        req = Request(url, data=data, headers=headers, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                final_url = resp.geturl() if hasattr(resp, "geturl") else url
                body = resp.read().decode(errors="replace")
                log(f"POST {url} -> {resp.status} (final={final_url})")
                if resp.status in (200, 302) and success_url_contains in final_url and failure_url_contains not in final_url:
                    return True, body
                return False, body
        except HTTPError as e:
            body = e.read().decode(errors="replace")[:2000]
            print(f"WARNING: Redmine admin session POST {url} failed: {e.code} {e.reason}: {body}", file=sys.stderr)
            return False, body
        except (URLError, OSError) as e:
            print(f"WARNING: Redmine admin session POST {url} failed: {e}", file=sys.stderr)
            return False, ""

    def _admin_session_get(self, path, params=None):
        url = f"{self.api_url}/{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        if self._admin_session_cookie is None:
            self._admin_login()
        headers = {"Cookie": self._admin_session_cookie,
                   "X-CSRF-Token": self._admin_csrf_token}
        log(f"Redmine admin session GET {url}")
        req = Request(url, headers=headers, method="GET")
        try:
            with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                body = resp.read().decode(errors="replace")
                log(f"GET {url} -> {resp.status}")
                return body
        except HTTPError as e:
            body = e.read().decode(errors="replace")[:2000]
            print(f"WARNING: Redmine admin session GET {url} failed: "
                  f"{e.code} {e.reason}: {body}", file=sys.stderr)
            return None
        except (URLError, OSError) as e:
            print(f"WARNING: Redmine admin session GET {url} failed: {e}",
                  file=sys.stderr)
            return None

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
    # Unauthenticated GitHub REST access is capped at 60 requests/hour per
    # IP: requests are paced and retried once on quota responses; per-repo
    # branch/tag listing is done via `git ls-remote` (see GithubMirrorSync),
    # which does not count against the REST quota.
    RATE_LIMIT_MAX_WAIT = 900

    def __init__(self):
        self.api_url = "https://api.github.com"
        self.api_delay = float(os.environ.get("SHOGGOTH_GITHUB_API_DELAY", "2"))

    def get(self, path, params=None):
        url = f"{self.api_url}/{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        for attempt in (1, 2):
            time.sleep(self.api_delay)
            log(f"GET {url}")
            req = Request(url, headers={"accept": "application/json"})
            try:
                with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                    data = json.loads(resp.read().decode())
                    log(f"GET {url} -> {resp.status}")
                    return data
            except HTTPError as e:
                if attempt == 1 and e.code in (403, 429) and self._wait_for_quota(e, url):
                    continue
                body = e.read().decode(errors="replace")[:2000]
                print(f"WARNING: HTTP GET {url} failed: {e.code} {e.reason}: {body}", file=sys.stderr)
                return None
            except (URLError, OSError) as e:
                print(f"WARNING: HTTP GET {url} failed: {e}", file=sys.stderr)
                return None
            except json.JSONDecodeError as e:
                print(f"WARNING: HTTP GET {url} returned invalid JSON: {e}", file=sys.stderr)
                return None
        return None

    def _wait_for_quota(self, error, url):
        headers = error.headers or {}
        retry_after = headers.get("Retry-After")
        reset = headers.get("X-RateLimit-Reset")
        try:
            if retry_after is not None:
                wait = float(retry_after)
            elif reset is not None:
                wait = max(0.0, int(reset) - time.time())
            else:
                return False
        except ValueError:
            return False
        if wait > self.RATE_LIMIT_MAX_WAIT:
            return False
        print(f"GitHub rate limit hit, waiting {wait:.0f}s before retrying {url}",
              file=sys.stderr, flush=True)
        time.sleep(wait)
        return True

    def list_repos(self, account, account_type):
        return _paginate(lambda page, limit: self.get(
            f"{account_type}/{account}/repos",
            params={"page": page, "per_page": limit, "type": "public"}), limit=100)


class SetupArgoWebhooks:
    """Configure Gitea org webhooks to deliver events to Argo Workflows.

    Replaces the Kestra-based equivalent that pointed at
    /api/v1/main/executions/webhook/. Argo's events endpoint is
    /api/v1/events/{namespace}/{discriminator} and accepts an arbitrary
    JSON body (no path-suffix key). The discriminator must match the
    WorkflowEventBinding selector (gitea-pr-update / gitea-ci-failure).
    """

    def __init__(self, gitea):
        self.gitea = gitea
        # Webhook URLs always go through the web-internal gateway at
        # api.{domain}/argo/ — the gateway injects the
        # argo-webhook-receiver SA token before proxying to argo:80,
        # so the v4 webhook interceptor (which has no gitea parser)
        # is short-circuited by the Authorization header and
        # argo-server's gatekeeper does bearer validation against k8s.
        # See shoggoth/k3s/web-internal.yaml for the gateway contract.
        self.namespace = os.environ.get("SHOGGOTH_NAMESPACE", "shoggoth")
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
            # gitea-ci-failure is intentionally NOT registered: the
            # deleted Kestra flow `main_shoggoth_gitea-ci-failure.yml`
            # carried `disabled: true`, and the WorkflowEventBinding
            # kind has no equivalent kill switch (active by default
            # once registered). To re-enable CI-failure dispatch in
            # the future, add the WorkflowEventBinding under
            # shoggoth/workflow/argo/bindings/ and re-introduce the
            # entry below — with the maintainer understanding that
            # every workflow_run: failure event will spawn a
            # coding-agent-dispatcher pod.
            # Webhook URL points at api.<DOMAIN>/argo/, not argo.<DOMAIN>:
            # web-internal (shoggoth/k3s/web-internal.yaml) injects the
            # argo-webhook-receiver SA token before proxying to argo:80,
            # so the v4 webhook interceptor (which has no gitea parser and
            # would 403 raw gitea payloads — webhookParsers at
            # server/auth/webhook/interceptor.go:25-30) is short-circuited
            # by the Authorization header and argo-server's gatekeeper does
            # bearer validation against k8s instead.
            (f"http://api.{self.gitea.domain}/argo/api/v1/events/{self.namespace}/gitea-pr-update",
             ["pull_request_review", "pull_request_review_request", "pull_request_comment"]),
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
            die(f"argo-webhooks completed with {self.errors} error(s)")


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


class SetupRedmineArgoWebhooks:
    EVENTS = ["issue.created", "issue.updated"]

    def __init__(self, redmine):
        self.redmine = redmine
        self.namespace = os.environ.get("SHOGGOTH_NAMESPACE", "shoggoth")
        domain = os.environ.get("SHOGGOTH_DOMAIN", "")
        if not domain:
            die("SHOGGOTH_DOMAIN is required for argo-webhook URL")
        # Same pattern as SetupArgoWebhooks above: deliver through
        # api.<DOMAIN>/argo/ so web-internal injects the argo-webhook-receiver
        # bearer token (shoggoth/k3s/web-internal.yaml + argo-webhook-receiver.yaml)
        # before forwarding to argo:80. Going directly (http://argo:80/...)
        # would trip the v4 interceptor's gitea/redmine signature matcher
        # absence (webhookParsers at interceptor.go:25-30 has no entry for
        # either) and 403 the request.
        self.WEBHOOK_URL = (
            f"http://api.{domain}/argo/api/v1/events/{self.namespace}/redmine-task-processor"
        )
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
            die(f"redmine-argo-webhooks completed with {self.errors} error(s)")
        print("redmine-argo-webhooks: webhooks configured successfully")


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

        account = self.github.get(f"users/{github_org}")
        if account is None:
            msg = "GitHub account lookup failed (not found or rate limited)"
            print(f"ERROR: {msg}")
            self._record_failure(github_org, msg)
            return
        account_type = "orgs" if account.get("type") == "Organization" else "users"

        print(f"GitHub account type: {account_type}")

        repos = self.github.list_repos(github_org, account_type)
        if not repos:
            print(f"WARNING: No repos found for GitHub {account_type} '{github_org}' "
                  f"(or the repo listing failed)")
            return

        self.tmpdir = tempfile.mkdtemp()
        try:
            for idx, repo in enumerate(repos):
                if idx:
                    time.sleep(self.github.api_delay)
                self._sync_repo(github_org, repo)
        finally:
            shutil.rmtree(self.tmpdir, ignore_errors=True)

        print(f"=== Sync complete for '{github_org}' ===")

    _NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

    def _sync_repo(self, github_org, repo_info):
        repo_name = repo_info.get("name", "")
        repo_desc = repo_info.get("description") or ""
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
            status = self._sync_existing_repo(github_org, repo_name, repo_data)
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

    def _github_refs(self, github_org, repo_name):
        """Branch and tag heads from GitHub via `git ls-remote`.

        One unauthenticated git request replaces the per-repo branches/tags
        REST calls, which exhausted the GitHub API quota for orgs with more
        than ~30 repos (2 unauthenticated calls per repo, 60/hour limit).
        """
        ls_remote = run(["git", "ls-remote",
                         f"https://github.com/{github_org}/{repo_name}.git"], check=False)
        if ls_remote.returncode != 0:
            return None
        branches, tags = {}, {}
        for line in ls_remote.stdout.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.startswith("refs/heads/"):
                branches[ref[len("refs/heads/"):]] = sha
            elif ref.startswith("refs/tags/"):
                ref = ref[len("refs/tags/"):]
                if ref.endswith("^{}"):
                    tags[ref[:-len("^{}")]] = sha
                else:
                    tags.setdefault(ref, sha)
        return branches, tags

    def _sync_existing_repo(self, github_org, repo_name, repo_data):
        gitea_repo = f"{github_org}/{repo_name}"

        github_refs = self._github_refs(github_org, repo_name)
        if github_refs is None:
            self._record_failure(gitea_repo, "failed to list refs from GitHub (git ls-remote)")
            return "failed"
        github_by_name, github_tag_sha = github_refs

        gitea_branches = self.gitea.list_branches(gitea_repo) or []

        if not github_by_name:
            self._record_failure(gitea_repo, "GitHub repository has no branches")
            return "failed"
        if not gitea_branches:
            self._record_failure(gitea_repo, "Gitea list_branches returned no branches")
            return "failed"

        gitea_tags = self.gitea.list_tags(gitea_repo) or []

        gitea_by_name = {b["name"]: b["commit"]["id"] for b in gitea_branches}
        gitea_tag_sha = {t["name"]: t["commit"]["sha"] for t in gitea_tags}

        out_of_sync = [
            name for name in github_by_name
            if github_by_name[name] != gitea_by_name.get(name)
        ]
        out_of_sync += [
            name for name in github_tag_sha
            if github_tag_sha[name] != gitea_tag_sha.get(name)
        ]

        if not out_of_sync:
            print(f"Repository {github_org}/{repo_name} is up to date "
                  f"({len(github_by_name)} branch(es), {len(github_tag_sha)} tag(s) match), "
                  f"skipping")
            return "skipped"

        print(f"Repository {github_org}/{repo_name}: {len(out_of_sync)} branch(es) differ "
              f"from Gitea, syncing...")

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
        self.reviewer_user = os.environ.get("SHOGGOTH_REVIEWER_USER", "slave-reviewer")
        self.automation_users = [u for u in (self.slave_user, self.reviewer_user) if u]
        self.errors = 0

    def execute(self):
        orgs = self.gitea.list_orgs()
        for user in self.automation_users:
            self._add_to_orgs(orgs, user)
            self._add_to_mirrored_repos(orgs, user)
        if self.errors:
            die(f"slave-access completed with {self.errors} error(s)")

    def _add_to_orgs(self, orgs, user):
        if not orgs:
            print("No organizations found")
            return

        for org in orgs:
            org_name = org.get("username")
            if not org_name:
                continue
            print(f"Checking '{user}' membership in org '{org_name}'")
            if not self.gitea.is_org_member(org_name, user):
                print(f"Adding '{user}' to org '{org_name}'")
                if not self.gitea.add_org_member(org_name, user):
                    self.errors += 1
            else:
                print(f"  '{user}' is already a member of '{org_name}'")

    def _add_to_mirrored_repos(self, orgs, user):
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

                if not self.gitea.is_repo_collaborator(full_name, user):
                    print(f"Adding '{user}' as collaborator to repo '{full_name}'")
                    if not self.gitea.add_repo_collaborator(full_name, user, "write"):
                        self.errors += 1
                else:
                    print(f"  '{user}' already has access to '{full_name}'")


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
                        self._attempted_slave_key_id = key_id
                        if not self.gitea.delete(f"admin/users/{self.slave_user}/keys/{key_id}"):
                            self.errors += 1
                    break

        if self._register(public_key):
            print(f"ssh-key: public key registered successfully")

        if self.errors:
            die(f"ssh-key completed with {self.errors} error(s)")

    def _register(self, public_key):
        print(f"Registering SSH key '{self.KEY_TITLE}' for user '{self.slave_user}'")
        ok, status, body, _ = self.gitea.create_user_ssh_key(
            self.slave_user, self.KEY_TITLE, public_key)
        if ok:
            return True
        if status == 422 and "non-deploy key" in body:
            if self._cleanup_existing_key(public_key):
                print(f"Retrying SSH key registration after key cleanup")
                ok, _, _, _ = self.gitea.create_user_ssh_key(
                    self.slave_user, self.KEY_TITLE, public_key)
                if ok:
                    return True
        self.errors += 1
        return False

    def _cleanup_existing_key(self, public_key):
        target = public_key.strip()
        removed = 0

        admin_keys = self.gitea.list_admin_keys() or []
        for entry in admin_keys:
            entry_key = (entry.get("key") or "").strip()
            if entry_key != target:
                continue
            key_id = entry.get("id")
            if key_id is None:
                continue
            user_obj = entry.get("user")
            if isinstance(user_obj, dict) and user_obj.get("login"):
                owner = user_obj["login"]
                print(f"Removing existing SSH key id={key_id} "
                      f"title={entry.get('title')!r} from user '{owner}' "
                      f"matching slave public key")
                if self.gitea.delete(f"admin/users/{owner}/keys/{key_id}"):
                    removed += 1
            elif user_obj is None:
                print(f"Removing orphan deploy key id={key_id} "
                      f"title={entry.get('title')!r} matching slave public key")
                if self.gitea.delete_admin_key(key_id):
                    removed += 1

        users = self.gitea.list_admin_users() or []
        for user in users:
            username = user.get("login")
            if not username:
                continue
            user_keys = self.gitea.list_user_ssh_keys(username) or []
            for k in user_keys:
                entry_key = (k.get("key") or "").strip()
                if entry_key != target:
                    continue
                key_id = k.get("id")
                if key_id is None:
                    continue
                if (username == self.slave_user
                        and k.get("title") == self.KEY_TITLE
                        and key_id == getattr(self, "_attempted_slave_key_id", None)):
                    continue
                print(f"Removing duplicate SSH key id={key_id} "
                      f"title={k.get('title')!r} from user '{username}' "
                      f"matching slave public key")
                if self.gitea.delete(f"admin/users/{username}/keys/{key_id}"):
                    removed += 1

        if removed == 0:
            print(f"WARNING: Gitea reports key already exists as a non-deploy key, "
                  f"but no matching key was found via /admin/keys or per-user scan",
                  file=sys.stderr)
        return removed > 0


class SlaveTest:
    SCRATCH_REPO = "slave-test-scratch"
    SSH_BRANCH = "slave-test-ssh"

    def __init__(self):
        self.errors = 0
        self._openbao = None
        self.slave_user = os.environ.get("SHOGGOTH_SLAVE_USER", "slave")
        # `gitea_url` is the HTTP API base. The slave-test template wires
        # this to the web-internal API gateway (api.${DOMAIN}/gitea),
        # which replaces our Authorization header with the real slave
        # token before forwarding to Gitea (see web-internal.yaml,
        # `proxy_set_header Authorization "Bearer ${GITEA_SLAVE_TOKEN}"`).
        # The script therefore doesn't need to acquire the slave token
        # for HTTP API calls.
        self.gitea_url = (os.environ.get("GITEA_SERVER_URL") or "").rstrip("/")
        # `git_url` is the direct HTTPS URL used by `git clone` /
        # `git push` / `git ls-remote`. Separate from gitea_url because
        # git-cred-bootstrap populates the credential cache for
        # `host=git.${DOMAIN}` (see shoggoth/k3s/git-cred-bootstrap.yaml),
        # so a clone through the gateway at `host=api.${DOMAIN}` would
        # have no matching credential and git would fail. Falls back to
        # `gitea_url` if the template doesn't set GITEA_GIT_URL (legacy
        # setups that put the gateway URL in GITEA_SERVER_URL and never
        # clone over HTTPS in this script).
        self.git_url = (os.environ.get("GITEA_GIT_URL")
                        or self.gitea_url).rstrip("/")
        self.ssh_host = os.environ.get("GITEA_SSH_HOST", "git")
        self.ssh_port = os.environ.get("GITEA_SSH_PORT", "22")
        self.checks = {
            "gitea-api": self._check_gitea_api,
            "redmine-api": self._check_redmine_api,
            "git-http": self._check_git_http,
            "git-ssh": self._check_git_ssh,
            "embeddings": self._check_embeddings,
            "mcp": self._check_mcp,
            "telemetry": self._check_telemetry,
            "pip-cache": self._check_pip_cache,
        }

    @property
    def openbao(self):
        if self._openbao is None:
            self._openbao = OpenBao()
        return self._openbao

    def _fail(self, msg):
        print(f"FAIL: {msg}", file=sys.stderr, flush=True)
        self.errors += 1

    def _slave_token(self):
        """Return the Gitea slave token for HTTP API calls.

        Prefers the `GITEA_ADMIN_TOKEN` env var (workflow templates
        source this from the `gitea-slave-token` K8s Secret — the same
        Secret the git-cred sidecar reads, so the token is already on
        the pod; no OpenBao round-trip needed). Also accepts
        `GITEA_SLAVE_TOKEN` as an alias (matches the env var name the
        git-cred-bootstrap sidecar uses internally and what
        shoggoth_workflow.py reads). Falls back to OpenBao only when
        neither env var is set, and logs a WARNING so the missing
        workflow-template secretRef is visible.
        """
        token = (os.environ.get("GITEA_ADMIN_TOKEN")
                 or os.environ.get("GITEA_SLAVE_TOKEN"))
        if token:
            log(f"_slave_token: using env var "
                f"({'GITEA_ADMIN_TOKEN' if os.environ.get('GITEA_ADMIN_TOKEN') else 'GITEA_SLAVE_TOKEN'}), "
                f"len={len(token)}")
            return token
        log("WARNING: GITEA_ADMIN_TOKEN and GITEA_SLAVE_TOKEN env vars both "
            "unset; falling back to OpenBao gitea/slave-token. The workflow "
            "template's secretRef wiring is missing for this run.")
        token = self.openbao.get_value("gitea/slave-token")
        if not token:
            die("gitea/slave-token is missing in OpenBao (run 'slave-token' "
                "maintenance first) and no env var was set either")
        return token

    def _slave_headers(self, token):
        return {"Authorization": f"token {token}"}

    def _git_env(self, extra=None):
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_CONFIG_COUNT"] = "2"
        env["GIT_CONFIG_KEY_0"] = "user.name"
        env["GIT_CONFIG_VALUE_0"] = self.slave_user
        env["GIT_CONFIG_KEY_1"] = "user.email"
        env["GIT_CONFIG_VALUE_1"] = f"{self.slave_user}@shoggoth"
        if extra:
            for key, value in extra.items():
                idx = int(env["GIT_CONFIG_COUNT"])
                env[f"GIT_CONFIG_KEY_{idx}"] = key
                env[f"GIT_CONFIG_VALUE_{idx}"] = value
                env["GIT_CONFIG_COUNT"] = str(idx + 1)
        return env

    def _git(self, args, env):
        result = run(["git"] + args, env=env, check=False)
        if result.returncode != 0:
            self._fail(f"git {args[0]}: {result.stderr.strip()[:400]}")
        return result

    def _ensure_scratch_repo(self, token):
        if not self.gitea_url:
            die("GITEA_SERVER_URL is required")
        headers = self._slave_headers(token)
        repo_full = f"{self.slave_user}/{self.SCRATCH_REPO}"
        status = http_status(f"{self.gitea_url}/api/v1/repos/{repo_full}", headers=headers)
        if status == 200:
            print(f"scratch repo '{repo_full}' exists")
            return True
        if status != 404:
            self._fail(f"GET repos/{repo_full} with slave token: HTTP {status}")
            return False
        admin_token = os.environ.get("GITEA_ADMIN_TOKEN")
        if not admin_token:
            self._fail("GITEA_ADMIN_TOKEN is required to create the scratch repo "
                       "(the slave token deliberately has no write:user scope)")
            return False
        created = http_post_json(f"{self.gitea_url}/api/v1/admin/users/{self.slave_user}/repos",
                                 {"name": self.SCRATCH_REPO, "private": True, "auto_init": False},
                                 headers={"Authorization": f"token {admin_token}"})
        if created is None:
            self._fail(f"cannot create scratch repo '{repo_full}' with admin token")
            return False
        print(f"scratch repo '{repo_full}' created")
        return True

    # The git-cred sidecar (injected via workflowDefaults.podSpecPatch)
    # populates /shoggoth/git-cred in parallel with the main container's
    # startup, so the first git operation in the check can outrun
    # ssh-keyscan + ssh-agent + ssh-add + git-credential-cache--daemon.
    # Poll the sidecar's outputs for up to ~30s before failing — most
    # failures here are races, not actual wiring bugs. The probe message
    # then surfaces ENOENT vs PermissionError distinctly so we can tell
    # "sidecar crashed before this output" apart from "init container /
    # runAsUser wiring missing on the Argo podSpecPatch" (the latter
    # leaves /shoggoth/git-cred drwx------ owned by root, which the main
    # ccws container can't traverse).
    #
    # Both git-http and git-ssh wait on the SINGLE flag
    # /shoggoth/git-cred/ready that the sidecar touch()es after both
    # daemons are alive (with a 3 s grace — see
    # shoggoth/k3s/git-cred-bootstrap.yaml "Ready flag"). Polling one
    # file instead of three sockets (known_hosts, ssh_auth_sock,
    # git_credential_sock) means the helper below stays generic — pass
    # it any path that appears on disk when the sidecar is ready. The
    # per-socket post-checks (reading known_hosts, `ssh-add -l`) still
    # run after the wait to surface wiring bugs that pass the flag
    # check but break individual operations.
    #
    # The podSpecPatch also sets `restartPolicy: Always` and a readinessProbe
    # on git-cred (see shoggoth/k3s/argo-workflows.yaml). `restartPolicy:
    # Always` is what makes git-cred a native sidecar (long-running, started
    # before any non-sidecar container). The readinessProbe is purely
    # diagnostic — `kubectl describe pod` and Events surface git-cred
    # failures — and the per-check waits below are the actual gate. We do
    # not set K8s 1.28+ `startOrder` because shoggoth's running k3s build
    # (v1.34.3+k3s3) does not expose it in its apiserver OpenAPI v2 and
    # `kubectl apply` would reject it with "field not declared in schema".
    GIT_CRED_READY_PATH = "/shoggoth/git-cred/ready"
    GIT_CRED_READY_TIMEOUT = 30
    GIT_CRED_POLL_INTERVAL = 0.5

    def _wait_for_git_cred(self, path):
        deadline = time.time() + self.GIT_CRED_READY_TIMEOUT
        while time.time() < deadline:
            try:
                os.stat(path)
                return True
            except (FileNotFoundError, PermissionError):
                time.sleep(self.GIT_CRED_POLL_INTERVAL)
        return False

    def _git_cred_probe_message(self, path):
        """Run a single stat() on `path` and turn the OSError into a
        diagnostic that distinguishes the three failure modes."""
        try:
            os.stat(path)
            return f"{path} exists but _wait_for_git_cred returned False " \
                f"(timed out after {self.GIT_CRED_READY_TIMEOUT}s)"
        except FileNotFoundError:
            return (f"{path} does not exist after waiting "
                    f"{self.GIT_CRED_READY_TIMEOUT}s "
                    f"(git-cred sidecar didn't produce this output — "
                    f"check 'kubectl logs <pod> -c git-cred' for the FATAL line; "
                    f"also confirm the pod has an initContainer named "
                    f"init-git-cred-perms and a sidecar named git-cred)")
        except PermissionError as e:
            return (f"{path} is not accessible from the main container: {e} "
                    f"(git-cred sidecar ran but /shoggoth/git-cred is "
                    f"drwx------ and the main ccws container can't traverse "
                    f"it; the Argo podSpecPatch is missing init-git-cred-perms "
                    f"+ securityContext.runAsUser: 1000 on the sidecar — "
                    f"see shoggoth/k3s/argo-workflows.yaml)")
        except OSError as e:
            return f"{path}: {e}"

    # Verbose preflight dump — prints everything we'd otherwise need
    # `kubectl exec` to see. Each git-* check calls this once at start so
    # the workflow log itself tells us what state /shoggoth/git-cred is
    # in from the main container's perspective (uid, ownership, perms,
    # socket kinds, env var values, credential-cache probe result). Lets a
    # failing run self-diagnose without needing shell access to the pod.
    def _preflight_git_cred(self, label):
        log(f"=== preflight[{label}]: effective UID = {os.getuid()}")
        log(f"=== preflight[{label}]: SSH_AUTH_SOCK = "
            f"{os.environ.get('SSH_AUTH_SOCK')!r}")
        log(f"=== preflight[{label}]: GITEA_SLAVE_TOKEN present = "
            f"{bool(os.environ.get('GITEA_SLAVE_TOKEN'))} "
            f"(len={len(os.environ.get('GITEA_SLAVE_TOKEN') or '')})")

        who = run(["whoami"], check=False)
        log(f"=== preflight[{label}]: whoami rc={who.returncode} "
            f"stdout={who.stdout.strip()!r} "
            f"stderr={who.stderr.strip()[:200]!r}")
        ident = run(["id"], check=False)
        log(f"=== preflight[{label}]: id rc={ident.returncode} "
            f"stdout={ident.stdout.strip()!r}")

        ssh_sock = os.environ.get("SSH_AUTH_SOCK")

        # Probe ssh-agent identity (mirrors what ssh-add would do)
        if ssh_sock and os.path.exists(ssh_sock):
            identities = run(["ssh-add", "-l"],
                             env={**os.environ, "SSH_AUTH_SOCK": ssh_sock},
                             check=False)
            log(f"=== preflight[{label}]: ssh-add -l rc={identities.returncode} "
                f"stdout={identities.stdout.strip()[:200]!r} "
                f"stderr={identities.stderr.strip()[:200]!r}")

        # Dump the git-cred sidecar's bootstrap log. The bootstrap
        # script mirrors its stderr to
        # `/shoggoth/git-cred/bootstrap.log` (a file in the emptyDir
        # shared between the sidecar and this main container), so a
        # FATAL line from ssh-add / SSH_ID_RSA validation / ssh-agent
        # identity check / credential-cache etc. surfaces here even
        # though Argo's workflow log only archives stdout+stderr from
        # the MAIN container. Without this dump, the symptom of a
        # sidecar failure is just "ssh-add -l says no identities" /
        # "git_credential_sock doesn't exist" with no clue WHY —
        # the operator has to `kubectl logs <pod> -c git-cred` to
        # see the FATAL line, and that log isn't part of the
        # workflow output (which is what gets grep'd/jq'd by the
        # maintenance scripts). ENOENT is the common case (sidecar
        # hasn't started writing yet — preflight runs immediately
        # on workflow step start); PermissionError would mean the
        # init-git-cred-perms chown 1000:1000 didn't apply. Cap the
        # dump at the last 200 lines so a sidecar that's been
        # looping on a crash for the full 30s wait budget doesn't
        # spam the workflow log with N copies of the same FATAL.
        bootstrap_log_path = "/shoggoth/git-cred/bootstrap.log"
        try:
            with open(bootstrap_log_path) as f:
                log_lines = f.read().splitlines()
            log(f"=== preflight[{label}]: {bootstrap_log_path} "
                f"({len(log_lines)} lines, last 200):")
            for line in log_lines[-200:]:
                log(f"=== preflight[{label}]:   {line}")
        except FileNotFoundError:
            log(f"=== preflight[{label}]: {bootstrap_log_path}: ENOENT "
                f"(sidecar hasn't written anything yet — bootstrap.sh "
                f"may not have started, or fatal'd before the redirect "
                f"line in mkdir/chmod)")
        except PermissionError as e:
            log(f"=== preflight[{label}]: {bootstrap_log_path}: EACCES {e} "
                f"(init-git-cred-perms chown 1000:1000 didn't apply)")
        except OSError as e:
            log(f"=== preflight[{label}]: {bootstrap_log_path}: OSError {e}")

    def _check_gitea_api(self):
        if not self.gitea_url:
            die("GITEA_SERVER_URL is required")
        token = self._slave_token()
        headers = self._slave_headers(token)
        print(f"Gitea API URL: {self.gitea_url}")
        user = http_get(f"{self.gitea_url}/api/v1/user", headers=headers)
        if user is None:
            self._fail("GET /api/v1/user with slave token")
            return
        login = user.get("login", "?")
        if login != self.slave_user:
            self._fail(f"slave token authenticates as '{login}', expected '{self.slave_user}'")
        else:
            print(f"OK: authenticated as '{login}' (id={user.get('id', '?')})")

    def _check_redmine_api(self):
        redmine = Redmine(self.openbao)
        print(f"Redmine API URL: {redmine.api_url}")
        account = redmine.get("my/account.json")
        if account is None:
            self._fail("GET /my/account.json with redmine/slave-token")
            return
        user = account.get("user", {})
        print(f"OK: authenticated as '{user.get('login', '?')}' (id={user.get('id', '?')})")
        projects = redmine.list_projects()
        if projects is None:
            self._fail("GET /projects.json")
        else:
            print(f"OK: {len(projects)} project(s) visible")

    # Snapshot of /shoggoth/git-cred — called immediately after each
    # git-* check's wait loop completes, so the listing reflects the
    # populated state, not a half-ready sidecar. Same shape from
    # _check_git_http and _check_git_ssh so the two checks' logs are
    # directly comparable. whoami + ls -lan are always run and always
    # print *something* — an empty dir shows `total 0` instead of
    # silently hiding the failure mode. Each line of multi-line
    # stdout goes on its own log row (no \n-escapes). Allow-listed
    # env propagation check (no raw env dump — see the allow_list
    # block below). Let a failing run self-diagnose propagation of
    # SSH_AUTH_SOCK / GITEA_* / SHOGGOTH_* without `kubectl exec` and
    # without leaking secret env values to the workflow log.
    def _log_git_cred_listing(self, label):
        log(f"[{label}]: whoami + ls of /shoggoth/git-cred + env (allow-list)")
        who = run(["whoami"], check=False)
        log(f"[{label}]: whoami rc={who.returncode} "
            f"stdout={who.stdout.strip()!r} "
            f"stderr={who.stderr.strip()[:200]!r}")
        ls = run(["ls", "-lan", "/shoggoth/git-cred"], check=False)
        log(f"[{label}]: ls -lan /shoggoth/git-cred rc={ls.returncode} "
            f"stderr={ls.stderr.strip()[:200]!r}")
        # Per-line stdout: splitlines() gives each ls entry its own log
        # row. An empty directory shows as a single blank row — that's
        # the diagnostic signal we want when the sidecar hasn't
        # populated /shoggoth/git-cred yet.
        for line in ls.stdout.splitlines():
            log(f"[{label}]:   {line}")

        # Diagnostic env dump — allow-list only. NEVER dump the raw
        # env output: SHOGGOTH_VAULT_TOKEN is bound to the OpenBao
        # root token (full vault access — gitea/slave-token, ssh/slave-
        # private-key, every */admin path), and the workflow log is
        # the durable record on a failing run. The list below is the
        # contract the comment at the function header promised; if a
        # new diagnostic needs to land here, add it explicitly and
        # consider whether its value is secret.
        allow_list = [
            "SSH_AUTH_SOCK",
            "GITEA_SERVER_URL", "GITEA_GIT_URL", "GITEA_INSTANCE_SSH_HOST",
            "REDMINE_SERVER",
            "SHOGGOTH_DOMAIN", "SHOGGOTH_NAMESPACE", "SHOGGOTH_GITHUB_ORG",
            "OPENBAO_ADDR",
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "ARGO_WEBHOOK_TOKEN",
        ]
        log(f"[{label}]: env (allow-list, {len(allow_list)} vars)")
        for name in allow_list:
            value = os.environ.get(name, "")
            # Presence + length, never the value. The presence/empty
            # distinction is the only signal that matters for a
            # propagation check; printing the value would defeat the
            # whole allow-list. Match the GITEA_SLAVE_TOKEN pattern at
            # the preflight above (line 1758-1760).
            log(f"[{label}]: {name}={'<set>' if value else '<unset>'} "
                f"(len={len(value)})")

    def _dump_gitconfig(self, label):
        # Dump the slave image's baked-in /etc/gitconfig. git-http
        # relies on this for `credential.helper = cache --socket=...`
        # (without it the cache helper has no socket to talk to, so
        # git falls back to prompting and the clone fails); git-ssh
        # relies on it for any global [user] / [core] settings.
        # Missing file is logged but non-fatal — a non-zero rc +
        # `cat: ...: No such file or directory` shows up clearly in
        # the workflow log without aborting the check, since SSH-only
        # operations don't strictly need /etc/gitconfig and the user
        # can decide whether the absence is a bug or expected.
        gitconfig = "/etc/gitconfig"
        log(f"[{label}]: cat {gitconfig} (system git config)")
        cat = run(["cat", gitconfig], check=False)
        log(f"[{label}]: cat {gitconfig} rc={cat.returncode} "
            f"stderr={cat.stderr.strip()[:200]!r}")
        for line in cat.stdout.splitlines():
            log(f"[{label}]:   {line}")

    def _check_git_http(self):
        # Validate the git-cred sidecar's credential cache daemon
        # (/shoggoth/git-cred/git_credential_sock). The slave image's
        # `git config --system credential.helper 'cache --socket=...'`
        # makes git use this socket for HTTP auth, so this check exercises
        # the same path the coding-agent-dispatcher workflow relies on for
        # git operations that go over HTTP.
        # Distinguish ENOENT (sidecar never produced the file) from
        # PermissionError (sidecar ran but main container cannot traverse
        # /shoggoth/git-cred because the init container / runAsUser wiring
        # in the Argo podSpecPatch is missing).
        log("[git-http]: preflight dump of /shoggoth/git-cred")
        self._preflight_git_cred("git-http")

        ready_flag = self.GIT_CRED_READY_PATH
        log(f"[git-http]: waiting up to {self.GIT_CRED_READY_TIMEOUT}s for "
            f"git-cred ready flag at {ready_flag}")
        if not self._wait_for_git_cred(ready_flag):
            log(f"[git-http]: timed out, probing errno")
            self._fail(self._git_cred_probe_message(ready_flag))
            return
        log(f"[git-http]: git-cred ready flag present; daemons observable")
        self._log_git_cred_listing("git-http")

        # HTTP API auth comes from the web-internal gateway (see
        # self.gitea_url comment in SlaveTest.__init__): the gateway
        # replaces whatever Authorization header we send with the real
        # slave token before forwarding. So we don't need to acquire
        # the slave token here — pass a dummy "gateway" string for
        # _ensure_scratch_repo (which uses it to build the
        # Authorization header), and the gateway replaces it on the
        # way to Gitea. This is the user-visible win: git-http no
        # longer depends on either the GITEA_ADMIN_TOKEN env var or
        # the OpenBao gitea/slave-token round-trip.
        log(f"[git-http]: HTTP API via gateway {self.gitea_url} "
            f"(auth injected by gateway); git via direct URL "
            f"{self.git_url} with image-baked credential helper")
        token = "gateway"

        log(f"[git-http]: ensuring scratch repo {self.slave_user}/{self.SCRATCH_REPO}")
        if not self._ensure_scratch_repo(token):
            log(f"[git-http]: scratch repo not available, aborting")
            return
        log(f"[git-http]: scratch repo ready")

        # Clone from the direct git URL (not the gateway) so the
        # image-baked credential helper finds a cache entry: the
        # sidecar populates the cache for `host=git.${DOMAIN}`, and a
        # clone through the gateway at `host=api.${DOMAIN}` would have
        # no matching credential.
        clone_url = f"{self.git_url}/{self.slave_user}/{self.SCRATCH_REPO}.git"
        workdir = tempfile.mkdtemp(prefix="slave-test-http-")
        repo_dir = os.path.join(workdir, "repo")
        env = self._git_env()
        self._dump_gitconfig("git-http")
        log(f"[git-http]: git clone {clone_url} → {repo_dir} "
            f"(credential helper cache --socket hits "
            f"/shoggoth/git-cred/git_credential_sock)")
        clone = self._git(["clone", clone_url, repo_dir], env)
        log(f"[git-http]: clone rc={clone.returncode}")
        if clone.returncode != 0:
            log(f"[git-http]: clone failed; stderr={clone.stderr.strip()[:500]!r}")
            return
        log(f"[git-http]: clone OK")

        branch = "main"
        for args in (["checkout", "-B", branch],
                     ["commit", "--allow-empty", "-m", "slave-test git-http"],
                     ["push", "origin", branch]):
            log(f"[git-http]: git -C {repo_dir} {' '.join(args)}")
            r = self._git(["-C", repo_dir] + args, env)
            log(f"[git-http]: rc={r.returncode} "
                f"stderr={r.stderr.strip()[:300]!r}")
            if r.returncode != 0:
                return

        log(f"[git-http]: git ls-remote {clone_url} refs/heads/{branch}")
        result = run(["git", "ls-remote", clone_url, f"refs/heads/{branch}"], env=env, check=False)
        log(f"[git-http]: ls-remote rc={result.returncode} "
            f"stdout={result.stdout.strip()[:200]!r} "
            f"stderr={result.stderr.strip()[:200]!r}")
        if result.returncode != 0 or not result.stdout.strip():
            self._fail(f"git ls-remote over https: pushed branch '{branch}' not found")
            return

        print(f"OK: clone + commit + push + ls-remote over https via credential cache "
              f"({branch} -> {result.stdout.split()[0][:12]})")
        log("[git-http]: ALL STEPS PASSED")

    def _check_git_ssh(self):
        # Validate the git-cred sidecar's ssh-agent socket and known_hosts
        # file. The slave image's /etc/ssh/ssh_config.d/shoggoth.conf points
        # UserKnownHostsFile=/shoggoth/git-cred/known_hosts and SSH_AUTH_SOCK
        # is set in its ENV block, so this check exercises the same SSH path
        # as coding-agent-dispatcher clones.
        log("[git-ssh]: preflight dump of /shoggoth/git-cred")
        self._preflight_git_cred("git-ssh")

        sock_path = os.environ.get("SSH_AUTH_SOCK")
        log(f"[git-ssh]: SSH_AUTH_SOCK env var = {sock_path!r}")
        if not sock_path:
            self._fail("SSH_AUTH_SOCK is not set (git-cred sidecar not running?)")
            return
        ready_flag = self.GIT_CRED_READY_PATH
        log(f"[git-ssh]: waiting up to {self.GIT_CRED_READY_TIMEOUT}s for "
            f"git-cred ready flag at {ready_flag}")
        if not self._wait_for_git_cred(ready_flag):
            log(f"[git-ssh]: timed out, probing errno")
            self._fail(self._git_cred_probe_message(ready_flag))
            return
        log(f"[git-ssh]: git-cred ready flag present; daemons observable")
        self._log_git_cred_listing("git-ssh")

        known_hosts = "/shoggoth/git-cred/known_hosts"
        log(f"[git-ssh]: reading known_hosts {known_hosts}")
        try:
            with open(known_hosts) as f:
                kh_lines = [l for l in f.read().splitlines() if l.strip()]
        except OSError as e:
            log(f"[git-ssh]: cannot read {known_hosts}: {e}")
            self._fail(f"cannot read {known_hosts}: {e} "
                       f"(git-cred sidecar running as wrong uid?)")
            return
        log(f"[git-ssh]: known_hosts has {len(kh_lines)} entries "
            f"(first line prefix: {kh_lines[0][:80] if kh_lines else '<empty>'!r})")
        if not kh_lines:
            self._fail(f"{known_hosts} is empty "
                       f"(git-cred sidecar's ssh-keyscan failed)")
            return

        log(f"[git-ssh]: ssh-add -l (verify agent has identities)")
        ssh_add = run(["ssh-add", "-l"],
                      env={**os.environ, "SSH_AUTH_SOCK": sock_path},
                      check=False)
        log(f"[git-ssh]: ssh-add rc={ssh_add.returncode} "
            f"stdout={ssh_add.stdout.strip()[:200]!r} "
            f"stderr={ssh_add.stderr.strip()[:200]!r}")
        if (ssh_add.returncode != 0
                or "no identities" in ssh_add.stdout.lower()
                or not ssh_add.stdout.strip()):
            # Probe the agent socket for staleness — the AF_UNIX socket
            # file persists in emptyDir across container restarts, so a
            # dead ssh-agent from a previous bootstrap run can leave a
            # socket behind that ssh-add -l talks to (with no listener
            # attached). Without the stat() we can't tell "agent alive
            # but empty" from "socket is a tombstone".
            socket_info = "missing"
            try:
                st = os.stat(sock_path)
                socket_info = (f"size={st.st_size} mtime={int(st.st_mtime)} "
                               f"uid={st.st_uid} gid={st.st_gid}")
            except OSError as e:
                socket_info = f"stat failed: {e}"
            self._fail(
                f"ssh-agent has no identities. "
                f"ssh-add -l rc={ssh_add.returncode} "
                f"stdout={ssh_add.stdout.strip()[:200]!r} "
                f"stderr={ssh_add.stderr.strip()[:200]!r}. "
                f"socket={sock_path} ({socket_info}). "
                f"Inspect the git-cred sidecar logs with "
                f"`kubectl logs <pod> -c git-cred` — the bootstrap "
                f"script writes one `git-cred-bootstrap: FATAL: ...` "
                f"line naming the failing step "
                f"(SSH_ID_RSA empty → Secret 'ssh-slave-private-key' "
                f"key 'ssh-id-rsa' missing; ssh-add failed → key "
                f"permissions / agent refused; ssh-agent has no "
                f"identities after ssh-add → identity count check "
                f"hit zero — see git-cred-bootstrap.yaml)."
            )
            return
        print(f"git-cred: ssh-agent has identities, "
              f"known_hosts has {len(kh_lines)} entries")

        log(f"[git-ssh]: acquiring slave token for HTTP API (env var or "
            f"OpenBao fallback — see _slave_token log above)")
        token = self._slave_token()
        log(f"[git-ssh]: ensuring scratch repo {self.slave_user}/{self.SCRATCH_REPO}")
        if not self._ensure_scratch_repo(token):
            log(f"[git-ssh]: scratch repo not available, aborting")
            return
        log(f"[git-ssh]: scratch repo ready")

        workdir = tempfile.mkdtemp(prefix="slave-test-ssh-")
        repo_dir = os.path.join(workdir, "repo")
        clone_url = f"ssh://git@{self.ssh_host}:{self.ssh_port}/{self.slave_user}/{self.SCRATCH_REPO}.git"
        env = self._git_env()
        self._dump_gitconfig("git-ssh")
        log(f"[git-ssh]: git clone {clone_url} → {repo_dir} "
            f"(will use image-baked ssh_config and SSH_AUTH_SOCK)")
        clone = self._git(["clone", clone_url, repo_dir], env)
        log(f"[git-ssh]: clone rc={clone.returncode} "
            f"stderr={clone.stderr.strip()[:500]!r}")
        if clone.returncode != 0:
            return
        log(f"[git-ssh]: clone OK")
        for args in (["checkout", "-B", self.SSH_BRANCH],
                     ["commit", "--allow-empty", "-m", "slave-test git-ssh"],
                     ["push", "--force", "origin", self.SSH_BRANCH]):
            log(f"[git-ssh]: git -C {repo_dir} {' '.join(args)}")
            r = self._git(["-C", repo_dir] + args, env)
            log(f"[git-ssh]: rc={r.returncode} stderr={r.stderr.strip()[:300]!r}")
            if r.returncode != 0:
                return
        result = run(["git", "ls-remote", clone_url, f"refs/heads/{self.SSH_BRANCH}"], env=env, check=False)
        log(f"[git-ssh]: ls-remote rc={result.returncode} "
            f"stdout={result.stdout.strip()[:200]!r}")
        if result.returncode != 0 or not result.stdout.strip():
            self._fail(f"git ls-remote over ssh: pushed branch '{self.SSH_BRANCH}' not found")
            return
        print(f"OK: clone + commit + push + ls-remote over ssh via ssh-agent "
              f"({self.SSH_BRANCH} -> {result.stdout.split()[0][:12]})")
        log("[git-ssh]: ALL STEPS PASSED")

    def _check_embeddings(self):
        api = os.environ.get("EMBEDDINGS_API", "http://litellm:80/v1").rstrip("/")
        model = os.environ.get("EMBEDDINGS_MODEL", "nomic-embed-text")
        api_key = os.environ.get("EMBEDDINGS_API_KEY", "localai")
        print(f"Embeddings API: {api} (model={model})")
        resp = http_post_json(f"{api}/embeddings",
                              {"model": model, "input": "shoggoth slave verify"},
                              headers={"Authorization": f"Bearer {api_key}"})
        if not resp:
            self._fail(f"POST {api}/embeddings (model={model})")
            return
        try:
            embedding = resp["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError):
            self._fail(f"embeddings response has no vector: {json.dumps(resp)[:300]}")
            return
        if not embedding:
            self._fail("embeddings vector is empty")
            return
        print(f"OK: embedding dim={len(embedding)} head={[round(v, 6) for v in embedding[:4]]}")

    def _mcp_post(self, url, payload, headers, session_id=None):
        hdrs = dict(headers)
        if session_id:
            hdrs["Mcp-Session-Id"] = session_id
        req = Request(url, data=json.dumps(payload).encode(), headers=hdrs, method="POST")
        try:
            with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.status, resp.headers.get("Mcp-Session-Id"), resp.read().decode(errors="replace")
        except HTTPError as e:
            body = e.read().decode(errors="replace")[:2000]
            return e.code, e.headers.get("Mcp-Session-Id"), body
        except (URLError, OSError) as e:
            return 0, None, str(e)

    def _mcp_parse(self, body):
        body = body.strip()
        if not body:
            return None
        if body.startswith("{"):
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return None
        parsed = None
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                try:
                    parsed = json.loads(line[len("data:"):].strip())
                except json.JSONDecodeError:
                    continue
        return parsed

    def _check_mcp_server(self, name, url, token=None):
        print(f"\n--- MCP {name}: {url} ---")
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        status, session, body = self._mcp_post(url, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "shoggoth-slave-test", "version": "1"}}}, headers)
        if status != 200:
            self._fail(f"MCP {name}: initialize HTTP {status}: {body[:300]}")
            return
        server_info = (((self._mcp_parse(body) or {}).get("result") or {}).get("serverInfo")) or {}
        print(f"OK: initialize (server={server_info.get('name', '?')} {server_info.get('version', '?')}, "
              f"session={'yes' if session else 'none'})")
        if session:
            self._mcp_post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                           headers, session)
        status, _, body = self._mcp_post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                                         headers, session)
        if status != 200:
            self._fail(f"MCP {name}: tools/list HTTP {status}: {body[:300]}")
            return
        tools = (((self._mcp_parse(body) or {}).get("result") or {}).get("tools")) or []
        if not tools:
            self._fail(f"MCP {name}: no tools returned: {body[:300]}")
            return
        print(f"OK: {len(tools)} tool(s): {', '.join(t.get('name', '?') for t in tools[:10])}")

    def _check_mcp(self):
        token = self._slave_token()
        self._check_mcp_server("mcp-gitea",
                               os.environ.get("MCP_GITEA_URL", "http://mcp-gitea:80/mcp"), token)
        self._check_mcp_server("basic-memory",
                               os.environ.get("MCP_BASIC_MEMORY_URL", "http://basic-memory:80/mcp"))

    def _check_telemetry(self):
        endpoint = (os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "http://otelcol:4318").rstrip("/")
        print(f"OTLP endpoint: {endpoint}")
        host = urlsplit(endpoint).hostname or "otelcol"
        grpc_port = int(os.environ.get("OTEL_GRPC_PORT", "4317"))
        print(f"\n--- OTLP gRPC connect {host}:{grpc_port} ---")
        try:
            with socket.create_connection((host, grpc_port), timeout=HTTP_TIMEOUT):
                print(f"OK: {host}:{grpc_port} accepts connections")
        except OSError as e:
            self._fail(f"cannot connect to OTLP gRPC {host}:{grpc_port}: {e}")
        print(f"\n--- OTLP/HTTP POST {endpoint}/v1/traces ---")
        now_ns = time.time_ns()
        trace_id = secrets.token_bytes(16)
        payload = {
            "resourceSpans": [{
                "resource": {"attributes": [
                    {"key": "service.name", "value": {"stringValue": "shoggoth-slave-test"}}]},
                "scopeSpans": [{
                    "scope": {"name": "shoggoth-slave-test"},
                    "spans": [{
                        "traceId": trace_id.hex(),
                        "spanId": secrets.token_bytes(8).hex(),
                        "name": "slave-test telemetry",
                        "kind": 1,
                        "startTimeUnixNano": str(now_ns),
                        "endTimeUnixNano": str(now_ns + 1000000),
                    }],
                }],
            }],
        }
        status, body, _ = http_post_json_with_status(f"{endpoint}/v1/traces", payload)
        if status != 200:
            self._fail(f"POST {endpoint}/v1/traces: HTTP {status}: {body[:300]}")
            return
        print(f"OK: trace exported (traceId={trace_id.hex()})")

    def _check_pip_cache(self):
        index_url = (os.environ.get("PIP_INDEX_URL") or "").rstrip("/")
        print(f"Pip index URL: {index_url or '(unset)'}")
        if "python-cache" not in index_url:
            self._fail("PIP_INDEX_URL is not set or does not point to the python-cache proxy")
            return
        workdir = tempfile.mkdtemp(prefix="slave-test-pip-")
        result = run(["pip", "download", "--no-deps", "--disable-pip-version-check",
                      "--no-cache-dir", "--quiet", "-d", workdir, "six"], check=False)
        files = sorted(os.listdir(workdir)) if os.path.isdir(workdir) else []
        shutil.rmtree(workdir, ignore_errors=True)
        if result.returncode != 0:
            self._fail(f"pip download via {index_url}: {result.stderr.strip()[-300:]}")
            return
        if not files:
            self._fail(f"pip download via {index_url} produced no files")
            return
        print(f"OK: pip download through python-cache ({files[0]})")

    def execute(self, services=None):
        selected = services or list(self.checks)
        for name in selected:
            if name not in self.checks:
                die(f"unknown slave-test service '{name}' (valid: {', '.join(self.checks)})")
        for name in selected:
            print(f"\n=== slave-test: {name} ===")
            self.checks[name]()
        print(f"\nslave-test: {len(selected) - self.errors}/{len(selected)} check(s) passed")
        if self.errors:
            die(f"slave-test completed with {self.errors} error(s)")
        print("slave-test: all checks passed")


class SlaveToken:
    TOKEN_NAME = "shoggoth-slave"
    TOKEN_SCOPES = ["write:repository", "write:issue", "read:user"]
    K8S_SECRET_NAME = "gitea-slave-token"
    OPENBAO_PATH = "gitea/slave-token"

    def __init__(self, gitea):
        self.gitea = gitea
        self.namespace = os.environ.get("SHOGGOTH_NAMESPACE", "")
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

    def _has_outdated_scopes(self, token):
        return sorted(token.get("scopes") or []) != sorted(self.TOKEN_SCOPES)

    def _stored_token_is_usable(self):
        stored = OpenBao().get_value(self.OPENBAO_PATH)
        if not stored:
            return True
        status = http_status(f"{self.gitea.api_url}/api/v1/user",
                             headers={"Authorization": f"token {stored}"})
        if status == 200:
            return True
        print(f"Token stored in OpenBao is not usable: GET /api/v1/user -> HTTP {status}")
        return False

    def execute(self):
        existing_tok = self._find_existing_token()
        if existing_tok is not None:
            expired = self._is_token_expired(existing_tok)
            outdated = self._has_outdated_scopes(existing_tok)
            unusable = not self._stored_token_is_usable()
            if expired or outdated or unusable:
                tok_id = existing_tok.get("id")
                if tok_id is not None:
                    reason = ("expired" if expired
                              else "with outdated scopes" if outdated
                              else "with unusable stored value")
                    print(f"Deleting token '{self.TOKEN_NAME}' {reason} (id={tok_id})")
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

        print(f"Storing token in OpenBao '{self.OPENBAO_PATH}'")
        if not OpenBao().put_value(self.OPENBAO_PATH, token_value):
            print(f"ERROR: Failed to write OpenBao '{self.OPENBAO_PATH}'", file=sys.stderr)
            self.errors += 1

        if not self.namespace:
            print("ERROR: SHOGGOTH_NAMESPACE is required for K8s Secret write", file=sys.stderr)
            self.errors += 1
        else:
            print(f"Storing token in K8s Secret '{self.K8S_SECRET_NAME}' in namespace '{self.namespace}'")
            if not k8s_upsert_secret(self.K8S_SECRET_NAME, self.namespace, {"token": token_value}):
                print(f"ERROR: Failed to write K8s Secret '{self.K8S_SECRET_NAME}'", file=sys.stderr)
                self.errors += 1

        if self.errors:
            die(f"slave-token completed with {self.errors} error(s)")
        print(f"slave-token: token created and stored successfully")


class RunnerToken:
    K8S_SECRET_NAME = "gitea-runner-token"

    def __init__(self, gitea):
        self.gitea = gitea
        self.org = os.environ.get("SHOGGOTH_GITHUB_ORG", "")
        self.namespace = os.environ.get("SHOGGOTH_NAMESPACE", "")
        self.errors = 0

    def _fetch_registration_token(self):
        if not self.org:
            print("ERROR: SHOGGOTH_GITHUB_ORG is required", file=sys.stderr)
            return None
        return self.gitea.post(f"orgs/{self.org}/actions/runners/registration-token", {})

    def execute(self):
        if not self.namespace:
            print("ERROR: SHOGGOTH_NAMESPACE is required for K8s Secret write", file=sys.stderr)
            self.errors += 1
        else:
            print(f"Fetching runner registration token for org '{self.org}'")
            result = self._fetch_registration_token()
            if result is None or not result.get("token"):
                print(f"ERROR: Failed to fetch runner registration token", file=sys.stderr)
                self.errors += 1
            else:
                token_value = result["token"]
                print(f"Storing token in K8s Secret '{self.K8S_SECRET_NAME}' in namespace '{self.namespace}'")
                if not k8s_upsert_secret(self.K8S_SECRET_NAME, self.namespace, {"token": token_value}):
                    print(f"ERROR: Failed to write K8s Secret '{self.K8S_SECRET_NAME}'", file=sys.stderr)
                    self.errors += 1
        if self.errors:
            die(f"runner-token completed with {self.errors} error(s)")
        print(f"runner-token: token fetched and stored successfully")


LDAP_EXCLUDED_LOGINS = ("admin", "ldapauth", "config-admin")


class GiteaLdapSync:
    def __init__(self, gitea):
        self.gitea = gitea
        self.errors = 0

    def _user_count(self):
        users = self.gitea.list_admin_users()
        if users is None:
            return None
        return len(users)

    def _ldap_source_filter(self):
        exclusions = "".join(f"(!(uid={login}))" for login in LDAP_EXCLUDED_LOGINS)
        return (f"(&(objectClass=inetOrgPerson)"
                f"(|(uid=%[1]s)(mail=%[1]s))"
                f"{exclusions})")

    def _update_ldap_source_filter(self):
        data = self.gitea.get("admin/ldap")
        if not isinstance(data, list):
            print("WARNING: gitea-ldap-sync: could not list LDAP sources",
                  file=sys.stderr)
            return
        source = next((s for s in data if s.get("name") == "openldap"), None)
        if source is None:
            log("gitea-ldap-sync: openldap source not found, skipping filter update")
            return
        source_id = source.get("id")
        new_filter = self._ldap_source_filter()
        if source.get("user_filter") == new_filter:
            log(f"gitea-ldap-sync: LDAP source filter already excludes "
                f"{list(LDAP_EXCLUDED_LOGINS)}")
            return
        log(f"gitea-ldap-sync: updating LDAP source filter to exclude "
            f"{list(LDAP_EXCLUDED_LOGINS)}")
        if self.gitea.patch(f"admin/ldap/{source_id}",
                            {"user_filter": new_filter}) is None:
            print("ERROR: gitea-ldap-sync: failed to update LDAP source filter",
                  file=sys.stderr)
            self.errors += 1

    def execute(self):
        self._update_ldap_source_filter()
        before = self._user_count()
        log(f"gitea-ldap-sync: user count before sync_external_users: {before}")
        url = f"{self.gitea.api_url}/admin/cron/sync_external_users"
        req = Request(url, method="POST",
                      headers={"Authorization": f"token {self.gitea.token}",
                               "accept": "application/json"})
        try:
            with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                log(f"POST {url} -> {resp.status}")
                if resp.status != 204:
                    print(f"WARNING: gitea sync_external_users returned HTTP {resp.status}", file=sys.stderr)
                    self.errors += 1
        except HTTPError as e:
            body = e.read().decode(errors="replace")[:500]
            print(f"ERROR: gitea sync_external_users failed: {e.code} {e.reason}: {body}", file=sys.stderr)
            self.errors += 1
        except (URLError, OSError) as e:
            print(f"ERROR: gitea sync_external_users failed: {e}", file=sys.stderr)
            self.errors += 1
        after = self._user_count()
        log(f"gitea-ldap-sync: user count after sync_external_users: {after}")
        if before is not None and after is not None:
            delta = after - before
            if delta > 0:
                print(f"gitea-ldap-sync: provisioned {delta} new user(s) (total {before} -> {after})")
            else:
                print(f"gitea-ldap-sync: no new users (total {after})")
        if self.errors:
            die(f"gitea-ldap-sync completed with {self.errors} error(s)")
        print("gitea-ldap-sync: done")


class RedmineLdapSync:
    def __init__(self, redmine, ldap_url, ldap_bind_dn, ldap_bind_password, ldap_user_base_dn):
        import ldap3
        self._ldap3 = ldap3
        self.redmine = redmine
        self.ldap_url = ldap_url
        self.ldap_bind_dn = ldap_bind_dn
        self.ldap_bind_password = ldap_bind_password
        self.ldap_user_base_dn = ldap_user_base_dn
        self.errors = 0

    def _ldap_search_filter(self):
        exclusions = "".join(f"(!(uid={login}))" for login in LDAP_EXCLUDED_LOGINS)
        return f"(&(objectClass=inetOrgPerson){exclusions})"

    def _auth_source_id(self):
        body = self.redmine._admin_session_get("auth_sources")
        if body is None:
            return None
        if not isinstance(body, str):
            print(f"WARNING: redmine-ldap-sync: unexpected response type from "
                  f"/auth_sources: {type(body).__name__}", file=sys.stderr)
            return None
        for match in re.finditer(
                r'<tr[^>]*\bid="auth-source-(\d+)"[^>]*>(.*?)</tr>',
                body, re.DOTALL):
            auth_source_id = int(match.group(1))
            row_html = match.group(2)
            if re.search(r'>\s*openldap\s*<', row_html):
                return auth_source_id
        return None

    def _ldap_users(self):
        server = self._ldap3.Server(self.ldap_url, get_info=self._ldap3.NONE)
        conn = self._ldap3.Connection(server,
                                     user=self.ldap_bind_dn,
                                     password=self.ldap_bind_password,
                                     auto_bind=True)
        try:
            search_filter = self._ldap_search_filter()
            ok = conn.search(search_base=self.ldap_user_base_dn,
                             search_filter=search_filter,
                             attributes=["uid", "cn", "sn", "mail"],
                             size_limit=0)
            if not ok:
                print(f"WARNING: LDAP search returned no result for base={self.ldap_user_base_dn}", file=sys.stderr)
                return []
            users = []
            for entry in conn.entries:
                uid = str(entry.uid) if "uid" in entry else ""
                if not uid:
                    continue
                cn = str(entry.cn) if "cn" in entry else ""
                sn = str(entry.sn) if "sn" in entry else ""
                mail = str(entry.mail) if "mail" in entry else ""
                firstname, lastname = (sn or cn or uid), (sn or cn or uid)
                if cn and sn:
                    parts = cn.split(None, 1)
                    firstname = parts[0]
                    lastname = sn or (parts[1] if len(parts) > 1 else cn)
                elif cn:
                    firstname, lastname = cn, cn
                users.append({
                    "login": uid,
                    "firstname": firstname,
                    "lastname": lastname,
                    "mail": mail or f"{uid}@{os.environ.get('SHOGGOTH_DOMAIN', 'localhost')}",
                })
            return users
        finally:
            try:
                conn.unbind()
            except Exception:
                pass

    def _developer_role_id(self):
        data = self.redmine.get("roles.json")
        if not isinstance(data, dict):
            return None
        for role in data.get("roles", []):
            if role.get("name") == "Developer":
                return role.get("id")
        return None

    def _user_id(self, login):
        body = self.redmine._admin_session_get("users")
        if body is None:
            return None
        for m in re.finditer(r'href="/users/(\d+)/edit"[^>]*>([^<]+)<', body):
            if m.group(2).strip() == login:
                return int(m.group(1))
        return None

    def _add_to_active_projects(self, user_id, login):
        role_id = self._developer_role_id()
        if role_id is None:
            print(f"WARNING: redmine-ldap-sync: 'Developer' role not found",
                  file=sys.stderr)
            self.errors += 1
            return
        projects = self.redmine.list_projects()
        if projects is None:
            print(f"WARNING: redmine-ldap-sync: could not list projects",
                  file=sys.stderr)
            self.errors += 1
            return
        active_projects = [p for p in projects if p.get("status") == 1]
        if not active_projects:
            print("redmine-ldap-sync: no active projects found")
            return
        added = 0
        already_member = 0
        for project in active_projects:
            project_id = project.get("id")
            if not project_id:
                continue
            form_fields = {
                "membership[user_id]": str(user_id),
                "membership[role_ids][]": str(role_id),
            }
            ok, body = self.redmine._admin_session_post(
                f"projects/{project_id}/memberships",
                form_fields,
                success_url_contains="/settings/members",
                failure_url_contains="/memberships/new")
            if ok:
                added += 1
                continue
            if body and re.search(
                    r'already\s+a\s+member|already\s+exists|already\s+in\s+this\s+project',
                    body, re.IGNORECASE):
                already_member += 1
                continue
            print(f"WARNING: redmine-ldap-sync: failed to add '{login}' "
                  f"to project_id={project_id}: "
                  f"{(body or '')[:200]}", file=sys.stderr)
            self.errors += 1
        print(f"redmine-ldap-sync: '{login}' added to {added} active "
              f"project(s), {already_member} already a member")

    def execute(self):
        auth_source_id = self._auth_source_id()
        if auth_source_id is None:
            die("redmine-ldap-sync: openldap auth source not found in redmine (run redmine-init first?)")
        log(f"redmine-ldap-sync: AuthSourceLdap id={auth_source_id}")

        try:
            ldap_users = self._ldap_users()
        except Exception as e:
            die(f"redmine-ldap-sync: failed to enumerate LDAP users: {e}")
        if not ldap_users:
            print("redmine-ldap-sync: no inetOrgPerson entries found in LDAP")
            return
        log(f"redmine-ldap-sync: LDAP has {len(ldap_users)} inetOrgPerson entries: "
            f"{[u['login'] for u in ldap_users]}")

        created = 0
        skipped = 0
        for u in ldap_users:
            random_pw = secrets.token_urlsafe(24)
            form_fields = {
                "user[login]": u["login"],
                "user[firstname]": u["firstname"],
                "user[lastname]": u["lastname"],
                "user[mail]": u["mail"],
                "user[password]": random_pw,
                "user[password_confirmation]": random_pw,
                "user[must_change_passwd]": "0",
                "user[auth_source_id]": str(auth_source_id),
                "user[generate_password]": "0",
            }
            print(f"redmine-ldap-sync: creating user '{u['login']}' "
              f"(firstname='{u['firstname']}', mail='{u['mail']}')")
            ok, response_body = self.redmine._admin_session_post("users", form_fields)
            if ok:
                created += 1
            elif response_body and re.search(r'already\s+taken|has\s+already\s+been\s+taken',
                                             response_body, re.IGNORECASE):
                skipped += 1
                print(f"redmine-ldap-sync: user '{u['login']}' already exists, skipping")
            else:
                error_hint = ""
                if response_body:
                    err_match = re.search(r'<div[^>]*class="[^"]*error[^"]*"[^>]*>([^<]+)</div>',
                                          response_body, re.IGNORECASE)
                    if err_match:
                        error_hint = f": {err_match.group(1).strip()}"
                hint = f" (body: {response_body[:200]!r})" if response_body else ""
                print(f"ERROR: failed to create user '{u['login']}'{error_hint}{hint}",
                      file=sys.stderr)
                self.errors += 1
                continue

            user_id = self._user_id(u["login"])
            if user_id is None:
                if ok:
                    print(f"ERROR: POST /users returned success for '{u['login']}' "
                          f"but user not found on /users list "
                          f"(body: {response_body[:200]!r})", file=sys.stderr)
                else:
                    print(f"ERROR: 'already taken' detected for '{u['login']}' "
                          f"but user not found on /users list "
                          f"(body: {response_body[:200]!r})", file=sys.stderr)
                self.errors += 1
                continue

            self._add_to_active_projects(user_id, u["login"])
        print(f"redmine-ldap-sync: created={created}, skipped={skipped}, ldap_total={len(ldap_users)}")
        if self.errors:
            die(f"redmine-ldap-sync completed with {self.errors} error(s)")
        print("redmine-ldap-sync: done")


def main():
    parser = argparse.ArgumentParser(
        prog="shoggoth_maintenance.py",
        description="Shoggoth Gitea maintenance: webhooks, mirror sync, and slave user access",
    )
    parser.add_argument("command",
                        choices=["argo-webhooks", "redmine-webhooks", "redmine-argo-webhooks",
                                 "github-mirror-sync", "slave-access", "slave-token", "runner-token",
                                 "ssh-key",
                                 "slave-test",
                                 "gitea-ldap-sync", "redmine-ldap-sync"],
                        help="Command to execute")
    parser.add_argument("args", nargs="*", default=[],
                        help="Command arguments (project names for webhooks, github orgs for mirror-sync)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose logging to stderr")

    parsed = parser.parse_args()

    global VERBOSE
    VERBOSE = parsed.verbose

    if parsed.command == "argo-webhooks":
        gitea = Gitea()
        cmd = SetupArgoWebhooks(gitea)
        cmd.execute(parsed.args or None)
    elif parsed.command == "redmine-webhooks":
        gitea = Gitea()
        cmd = SetupRedmineWebhooks(gitea)
        cmd.execute(parsed.args or None)
    elif parsed.command == "redmine-argo-webhooks":
        openbao = OpenBao()
        redmine = Redmine(openbao)
        cmd = SetupRedmineArgoWebhooks(redmine)
        cmd.execute()
    elif parsed.command == "github-mirror-sync":
        # Resolve the list of orgs to mirror. Precedence:
        #   1. positional CLI args (e.g. `-p github_orgs="orgA orgB"`
        #      or trailing argv when invoked outside Argo)
        #   2. SHOGGOTH_GITHUB_ORG env var (wired by the github-mirror-sync
        #      WorkflowTemplate at admission time from the
        #      shoggoth-workflow-config ConfigMap)
        #   3. /shoggoth/workflow-config/SHOGGOTH_GITHUB_ORG file (the
        #      same ConfigMap mounted as a volume by the template —
        #      works even when the cluster's WorkflowTemplate is stale
        #      and missing the env var wiring above, which happens when
        #      apply-workflows has not been re-run since the template
        #      file was updated; apply-workflows only runs on
        #      argo-workflow-controller pod start)
        # If all three are unavailable, fail with an actionable error
        # instead of KeyError so the operator knows what to do.
        orgs = parsed.args
        if not orgs:
            default_org = os.environ.get("SHOGGOTH_GITHUB_ORG", "")
            if not default_org:
                config_path = "/shoggoth/workflow-config/SHOGGOTH_GITHUB_ORG"
                try:
                    with open(config_path) as f:
                        default_org = f.read().strip()
                except OSError:
                    pass
            if not default_org:
                die(
                    "github-mirror-sync requires at least one github organization name.\n"
                    "  No positional orgs passed, SHOGGOTH_GITHUB_ORG env var is unset,\n"
                    "  and /shoggoth/workflow-config/SHOGGOTH_GITHUB_ORG is not mounted.\n"
                    "  Most likely the cluster's WorkflowTemplate is stale — the\n"
                    "  apply-workflows init container only refreshes it on\n"
                    "  argo-workflow-controller pod start.\n"
                    "  Quick fix:\n"
                    "    make sync\n"
                    "    kubectl delete pod -n $INSTANCE -l app=argo-workflow-controller\n"
                    "    argo submit --from workflowtemplate/github-mirror-sync -n $INSTANCE\n"
                    "  Or pass orgs explicitly to skip the template refresh:\n"
                    "    argo submit --from workflowtemplate/github-mirror-sync -n $INSTANCE \\\n"
                    "        -p github_orgs=\"YOUR_ORG\""
                )
            print(f"Using SHOGGOTH_GITHUB_ORG={default_org}")
            orgs = [default_org]
        gitea = Gitea()
        github = Github()
        cmd = GithubMirrorSync(gitea, github)
        cmd.execute(orgs)
    elif parsed.command == "slave-access":
        gitea = Gitea()
        cmd = SlaveAccess(gitea)
        cmd.execute()
    elif parsed.command == "slave-token":
        gitea = Gitea()
        cmd = SlaveToken(gitea)
        cmd.execute()
    elif parsed.command == "runner-token":
        gitea = Gitea()
        cmd = RunnerToken(gitea)
        cmd.execute()
    elif parsed.command == "ssh-key":
        gitea = Gitea()
        openbao = OpenBao()
        cmd = SshKey(gitea, openbao)
        cmd.execute()
    elif parsed.command == "slave-test":
        cmd = SlaveTest()
        cmd.execute(parsed.args or None)
    elif parsed.command == "gitea-ldap-sync":
        gitea = Gitea()
        cmd = GiteaLdapSync(gitea)
        cmd.execute()
    elif parsed.command == "redmine-ldap-sync":
        openbao = OpenBao()
        redmine = Redmine(openbao)
        ldap_bind_password = openbao.get_value("openldap/ldapauth-password")
        if not ldap_bind_password:
            die("openldap/ldapauth-password OpenBao secret is required for LDAP bind")
        domain = os.environ.get("SHOGGOTH_DOMAIN", "")
        if not domain:
            die("SHOGGOTH_DOMAIN is required")
        ldap_base_dn = "dc=" + domain.replace(".", ",dc=")
        ldap_url = os.environ.get("LDAP_URL", "ldap://openldap:389")
        ldap_bind_dn = f"uid=ldapauth,ou=people,{ldap_base_dn}"
        ldap_user_base_dn = f"ou=people,{ldap_base_dn}"
        os.environ.setdefault("LDAP_URL", ldap_url)
        os.environ.setdefault("LDAP_BIND_DN", ldap_bind_dn)
        os.environ.setdefault("LDAP_USER_BASE_DN", ldap_user_base_dn)
        cmd = RedmineLdapSync(redmine, ldap_url, ldap_bind_dn, ldap_bind_password, ldap_user_base_dn)
        cmd.execute()


if __name__ == "__main__":
    main()
