"""Module split out of shoggoth.py; see package __init__."""
import re
from http.cookiejar import CookieJar
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen, HTTPCookieProcessor, build_opener
from urllib.error import URLError, HTTPError

from .http import HttpApiClient
from .k8s import K8s
from .util import die, log, normalize_for_branch
from .state import params, secrets_store

class Redmine(HttpApiClient):
    def __init__(self, k8s=None, read_tokens=True):
        server = params.redmine_server
        if not server:
            if not params.shoggoth_domain:
                die("REDMINE_SERVER or SHOGGOTH_DOMAIN is required")
            server = f"http://redmine.{params.shoggoth_domain}"
        self.api_url = server.rstrip("/")
        log(f"Redmine API URL: {self.api_url}")
        # Read the Redmine slave token from K8s. The maintenance SA
        # inherits ``get`` on ``redmine-slave-token`` via the
        # ``shoggoth-redmine-slave-token-read`` Role. The slave-test
        # workflow uses the web-internal api-proxy URL
        # (``api.${DOMAIN}/redmine``) and accepts an empty token.
        #
        # ``read_tokens=False`` skips the read. The dispatcher
        # (``shoggoth/workflow/scripts/dispatch.py``) opts into
        # this because its pod runs under
        # ``shoggoth-coding-agent-sandbox-spawner`` SA which is
        # scoped to pods/services CRUD only — no ``get`` on
        # redmine-slave-token — so the read would 403. The
        # dispatcher's Redmine usage is limited to
        # ``get_issue(task_id)`` for the ``_handle_task``
        # validation, and that call goes through the api-proxy
        # at ``api.${DOMAIN}/redmine`` where web-internal
        # injects the bearer token. Same reasoning as the
        # matching ``Gitea(read_tokens=False)`` opt-in.
        if read_tokens:
            self.token = secrets_store.read(
                "redmine/slave-token", key="token")
        else:
            self.token = ""
        if not self.token:
            # When REDMINE_SERVER is the api service (http://api.<DOMAIN>/redmine),
            # the web-internal proxy injects X-Redmine-API-Key automatically, so a
            # client token is unnecessary. For direct redmine access (e.g.
            # http://redmine.<DOMAIN>) the token is mandatory — fail loudly
            # rather than silently issuing unauthenticated requests that 401 later.
            hostname = urlparse(self.api_url).hostname or ""
            if hostname.startswith("api."):
                log("No Redmine client token configured; relying on api service proxy to inject X-Redmine-API-Key")
            else:
                die(f"Redmine client token is required to access {self.api_url} "
                    "(grant the script's SA `get` on the "
                    "redmine-slave-token K8s Secret)")
        else:
            masked = self.token[:4] + "..." + self.token[-4:] if len(self.token) > 8 else "***"
            log(f"Redmine token: {masked} (len={len(self.token)})")
        self._session_cookie = None
        self._admin_session_cookie = None
        self._admin_csrf_token = None
        self._k8s = k8s
        self._projects = None
        self._statuses = None
        self._user_logins = {}

    def _get_slave_password(self):
        # Read the LDAP slave user's bind password from K8s. The
        # maintenance SA's
        # ``shoggoth-maintenance-secret-writer-ldap-ssh-read`` Role
        # grants ``get`` on the four LDAP/SSH Secrets (see
        # ``shoggoth/k3s/maintenance-secret-writer.yaml``).
        k8s = self._k8s or K8s()
        password = secrets_store.read("openldap/slave-password", key="password",
                                     k8s=k8s)
        if password:
            log("Using Redmine slave password from K8s Secret")
            return password
        die("openldap-slave-password is required for session auth "
            "(grant the script's SA `get` on the "
            "openldap-slave-password K8s Secret)")

    def _extract_csrf_token(self, html):
        match = re.search(r'name="authenticity_token"\s+value="([^"]+)"', html)
        if not match:
            die("Failed to extract CSRF token from Redmine page")
        return match.group(1)

    def _login(self):
        if self._session_cookie is not None:
            return
        self._password_login(
            username="slave",
            password=self._get_slave_password(),
            label="login",
            cookie_attr="_session_cookie",
            csrf_attr="_csrf_token",
            established_log="Redmine session auth established",
        )

    def _admin_login(self):
        if self._admin_session_cookie is not None:
            return
        # Read the LDAP admin user's bind password from K8s — same
        # pattern as ``_get_slave_password``.
        k8s = self._k8s or K8s()
        admin_password = secrets_store.read("openldap/admin-password", key="password",
                                            k8s=k8s)
        if not admin_password:
            die("openldap-admin-password is required for redmine admin "
                "session auth (grant the script's SA `get` on the "
                "openldap-admin-password K8s Secret)")
        self._password_login(
            username="admin",
            password=admin_password,
            label="admin login",
            cookie_attr="_admin_session_cookie",
            csrf_attr="_admin_csrf_token",
            established_log="Redmine admin session auth established",
        )

    def _password_login(self, *, username, password, label,
                        cookie_attr, csrf_attr, established_log):
        """Establish a Redmine session cookie via the /login CSRF flow.

        Used by ``_login`` (slave user) and ``_admin_login`` (admin user).
        ``cookie_attr``/``csrf_attr`` are the instance attribute names the
        result is written to so each login path keeps its own session
        storage; ``label`` prefixes the ``Redmine <label> login failed``
        diagnostic.
        """
        jar = CookieJar()
        opener = build_opener(HTTPCookieProcessor(jar))
        login_url = f"{self.api_url}/login"
        try:
            with opener.open(Request(login_url), timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
                html = resp.read().decode()
            csrf_token = self._extract_csrf_token(html)
            post_data = urlencode({
                "username": username,
                "password": password,
                "authenticity_token": csrf_token,
            }).encode()
            req = Request(login_url, data=post_data, method="POST",
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
            with opener.open(req, timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
                if resp.status not in (200, 302):
                    die(f"Redmine {label} failed: HTTP {resp.status}")
                post_login_html = resp.read().decode()
            cookie_header = ""
            for cookie in jar:
                if cookie.name == "_redmine_session":
                    cookie_header = f"_redmine_session={cookie.value}"
                    break
            if not cookie_header:
                die(f"Redmine {label} succeeded but no session cookie received")
            setattr(self, cookie_attr, cookie_header)
            setattr(self, csrf_attr, self._extract_csrf_token(post_login_html))
            log(established_log)
        except (HTTPError, URLError, OSError) as e:
            die(f"Redmine {label} failed: {e}")

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
            with urlopen(req, timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
                final_url = resp.geturl() if hasattr(resp, "geturl") else url
                body = resp.read().decode(errors="replace")
                log(f"POST {url} -> {resp.status} (final={final_url})")
                if resp.status in (200, 302) and success_url_contains in final_url and failure_url_contains not in final_url:
                    return True, body
                return False, body
        except HTTPError as e:
            body = e.read().decode(errors="replace")[:2000]
            log(f"Redmine admin session POST {url} -> {e.code}")
            return False, body
        except (URLError, OSError) as e:
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
            with urlopen(req, timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
                body = resp.read().decode(errors="replace")
                log(f"GET {url} -> {resp.status}")
                return body
        except (HTTPError, URLError, OSError) as e:
            self._http_warn(f"Redmine admin session GET {url}", e, body_chars=2000)
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
            with urlopen(req, timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
                return resp.read().decode()
        except (HTTPError, URLError, OSError) as e:
            return self._http_warn(f"Redmine session GET {url}", e, body_chars=2000)

    def _session_post(self, path, form_fields):
        url = f"{self.api_url}/{path}"
        headers = self._session_headers()
        form_fields["authenticity_token"] = self._csrf_token
        data = urlencode(form_fields, doseq=True).encode()
        log(f"Redmine session POST {url}")
        req = Request(url, data=data, headers=headers, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urlopen(req, timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
                return resp.read().decode()
        except (HTTPError, URLError, OSError) as e:
            return self._http_warn(f"Redmine session POST {url}", e, body_chars=2000)

    def _auth_header(self):
        """Redmine auth scheme: ``X-Redmine-API-Key: <T>``."""
        if self.token:
            return {"X-Redmine-API-Key": self.token}
        return {}

    # get/post/patch/delete inherited from HttpApiClient.

    # ----- workflow helpers (token auth) ----------------------------

    def get_user_login(self, user_id):
        if user_id in self._user_logins:
            return self._user_logins[user_id]
        data = self.get(f"users/{user_id}.json")
        if data is None:
            log(f"get_user_login: failed to fetch user #{user_id}")
            return None
        user = data.get("user", data)
        login = user.get("login")
        if login:
            self._user_logins[user_id] = login
        return login

    def _put(self, path, payload):
        """PUT with a Redmine-specific payload-truncation log line.

        Truncates the ``issue.notes`` field to 200 chars + ellipsis in
        the log line — the unredacted notes can include secrets in
        workflow input. Delegates the HTTP call to ``put_json`` on the
        base class.
        """
        logged = payload
        if isinstance(payload, dict) and isinstance(payload.get("issue"), dict):
            notes = payload["issue"].get("notes")
            if isinstance(notes, str) and len(notes) > 200:
                logged = {**payload,
                          "issue": {**payload["issue"],
                                    "notes": notes[:200] + "…"}}
        log(f"PUT {self.api_url}/{path} payload={logged}")
        return self.put_json(path, payload)

    def _resolve_status_id(self, name):
        if self._statuses is None:
            data = self.get("issue_statuses.json")
            if data is None:
                die("failed to list redmine issue statuses")
            self._statuses = {s["name"]: s["id"] for s in data.get("issue_statuses", [])}
        status_id = self._statuses.get(name)
        if status_id is None:
            die(f"unknown Redmine status: {name}")
        return status_id

    def match_project(self, normalized):
        if self._projects is None:
            data = self.get("projects.json", params={"limit": 100})
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
        data = self.get(f"issues/{task_id}.json",
                         params={"include": "journals,children"})
        if data is None:
            die(f"failed to get redmine issue #{task_id}")
        return data.get("issue", data)

    def get_project_repo(self, project_id, domain):
        data = self.get(f"projects/{project_id}.json")
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
        data = self.get("issues.json", params={"project_id": project, "limit": 100})
        if data is None:
            return None
        return data.get("issues", [])

    # ----- session-auth wrappers (maintenance) -----------------------

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
