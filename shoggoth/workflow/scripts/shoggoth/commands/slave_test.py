"""Module split out of shoggoth.py; see package __init__."""
import json
import os
import secrets
import shutil
import socket
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from ..git import Git, GitCredentials
from ..http import HttpApiClient
from ..k8s import K8s
from ..redmine import Redmine
from ..util import die, log, run
from ..state import params

class SlaveTest:
    SCRATCH_REPO = "slave-test-scratch"
    SSH_BRANCH = "slave-test-ssh"

    def __init__(self):
        self.errors = 0
        self._k8s = None
        self.git_cred = GitCredentials()
        self.slave_user = params.shoggoth_slave_user
        # `gitea_url` is the HTTP API base. The slave-test template wires
        # this to the web-internal API gateway (api.${DOMAIN}/gitea),
        # which replaces our Authorization header with the real slave
        # token before forwarding to Gitea (see web-internal.yaml,
        # `proxy_set_header Authorization "Bearer ${GITEA_SLAVE_TOKEN}"`).
        # The script therefore doesn't need to acquire the slave token
        # for HTTP API calls.
        self.gitea_url = (params.gitea_server_url or "").rstrip("/")
        # `git_url` is the direct HTTPS URL used by `git clone` /
        # `git push` / `git ls-remote`. Separate from gitea_url because
        # git-cred-bootstrap populates the credential cache for
        # `host=git.${DOMAIN}` (see shoggoth/k3s/git-cred-bootstrap.yaml),
        # so a clone through the gateway at `host=api.${DOMAIN}` would
        # have no matching credential and git would fail. Falls back to
        # `gitea_url` if the template doesn't set GITEA_GIT_URL (legacy
        # setups that put the gateway URL in GITEA_SERVER_URL and never
        # clone over HTTPS in this script).
        self.git_url = (params.gitea_git_url
                        or self.gitea_url).rstrip("/")
        self.ssh_host = params.gitea_ssh_host
        self.ssh_port = params.gitea_ssh_port
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
    def k8s(self):
        if self._k8s is None:
            self._k8s = K8s()
        return self._k8s

    def _fail(self, msg):
        print(f"FAIL: {msg}", file=sys.stderr, flush=True)
        self.errors += 1

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
        status = HttpApiClient.http_status(f"{self.gitea_url}/api/v1/repos/{repo_full}", headers=headers)
        if status == 200:
            print(f"scratch repo '{repo_full}' exists")
            return True
        if status != 404:
            self._fail(f"GET repos/{repo_full} with slave token: HTTP {status}")
            return False
        # Create the scratch repo under the authenticated (slave) user via
        # `/api/v1/user/repos` — the slave token's scopes (write:repository,
        # write:issue, read:user) cover this endpoint, and the resulting
        # repo lives in the slave namespace where the git-http/git-ssh
        # checks expect it. The previous /admin/users/{u}/repos path
        # required a site-admin token; the slave-test template sets
        # GITEA_ADMIN_TOKEN: "gateway" (the web-internal API gateway
        # replaces the Authorization header with the real slave token),
        # so the admin path was 401/403 on every first run.
        created = HttpApiClient.http_post_json(f"{self.gitea_url}/api/v1/user/repos",
                                 {"name": self.SCRATCH_REPO, "private": True, "auto_init": False},
                                 headers=headers)
        if created is None:
            self._fail(f"cannot create scratch repo '{repo_full}' with slave token")
            return False
        print(f"scratch repo '{repo_full}' created")
        return True

    def _check_gitea_api(self):
        if not self.gitea_url:
            die("GITEA_SERVER_URL is required")
        # The ``GITEA_SERVER_URL`` env var points at the web-internal
        # api-proxy (``http://api.${DOMAIN}/gitea``); the proxy
        # replaces whatever Authorization header we send with the real
        # slave token before forwarding, so the literal ``"gateway"``
        # string is enough to satisfy the Gitea client's auth-header
        # builder — the same pattern ``_check_git_http`` uses for the
        # scratch-repo HTTP API. No ``_slave_token()`` call (which
        # would die in the ``slave:noble`` container with no kubectl
        # and no env-var wired).
        token = "gateway"
        headers = self._slave_headers(token)
        print(f"Gitea API URL: {self.gitea_url}")
        user = HttpApiClient.http_get(f"{self.gitea_url}/api/v1/user", headers=headers)
        if user is None:
            self._fail("GET /api/v1/user via web-internal api-proxy")
            return
        login = user.get("login", "?")
        if login != self.slave_user:
            self._fail(f"slave token authenticates as '{login}', expected '{self.slave_user}'")
        else:
            print(f"OK: authenticated as '{login}' (id={user.get('id', '?')})")

    def _check_redmine_api(self):
        # The ``REDMINE_SERVER`` env var points at the web-internal
        # api-proxy (``http://api.${DOMAIN}/redmine``); the proxy
        # injects ``X-Redmine-API-Key`` so ``Redmine.__init__`` may
        # end up with an empty token. The HTTP call still
        # authenticates as the slave user.
        redmine = Redmine(self.k8s)
        print(f"Redmine API URL: {redmine.api_url}")
        account = redmine.get("my/account.json")
        if account is None:
            self._fail("GET /my/account.json via web-internal api-proxy")
            return
        user = account.get("user", {})
        print(f"OK: authenticated as '{user.get('login', '?')}' (id={user.get('id', '?')})")
        projects = redmine.list_projects()
        if projects is None:
            self._fail("GET /projects.json")
        else:
            print(f"OK: {len(projects)} project(s) visible")

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
        # rc / stderr for ``cat`` logged by ``run()``; this method
        # only adds the per-line stdout iteration below so each config
        # line is greppable independently.
        cat = run(["cat", gitconfig], check=False)
        for line in cat.stdout.splitlines():
            log(f"[{label}]:   {line}")

    def _check_git_http(self):
        # Validate the git-cred sidecar's credential cache daemon
        # (/shoggoth/git-cred/git_credential_sock). The slave image's
        # `git config --system credential.helper 'cache --socket=...'`
        # makes git use this socket for HTTP auth, so this check exercises
        # the same path the command-handler workflow relies on for
        # git operations that go over HTTP.
        # Distinguish ENOENT (sidecar never produced the file) from
        # PermissionError (sidecar ran but main container cannot traverse
        # /shoggoth/git-cred because the init container / runAsUser wiring
        # in the Argo podSpecPatch is missing).
        ready, probe = self.git_cred.wait_for_diag("git-http")
        if not ready:
            self._fail(probe)
            return
        self.git_cred.log_listing("git-http")

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
        if clone.returncode != 0:
            return
        log(f"[git-http]: clone OK")

        branch = "main"
        for args in (["checkout", "-B", branch],
                     ["commit", "--allow-empty", "-m", "slave-test git-http"],
                     ["push", "origin", branch]):
            log(f"[git-http]: git -C {repo_dir} {' '.join(args)}")
            r = self._git(["-C", repo_dir] + args, env)
            if r.returncode != 0:
                return

        result = Git().ls_remote(clone_url, f"refs/heads/{branch}",
                                  env=env, check=False)
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
        # as command-handler clones.
        sock_path = params.ssh_auth_sock
        log(f"[git-ssh]: SSH_AUTH_SOCK env var = {sock_path!r}")
        if not sock_path:
            self._fail("SSH_AUTH_SOCK is not set (git-cred sidecar not running?)")
            return
        ready, probe = self.git_cred.wait_for_diag("git-ssh")
        if not ready:
            self._fail(probe)
            return
        self.git_cred.log_listing("git-ssh")

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

        ssh_add = run(["ssh-add", "-l"],
                      env={**os.environ, "SSH_AUTH_SOCK": sock_path},
                      check=False)
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

        # The HTTP API scratch-repo creation goes through the
        # web-internal api-proxy (same as ``_check_gitea_api``); the
        # proxy replaces whatever Authorization header we send. Same
        # ``"gateway"`` literal as ``_check_git_http``.
        log(f"[git-ssh]: HTTP API via gateway {self.gitea_url} "
            f"(auth injected by gateway)")
        token = "gateway"
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
        if clone.returncode != 0:
            return
        log(f"[git-ssh]: clone OK")
        for args in (["checkout", "-B", self.SSH_BRANCH],
                     ["commit", "--allow-empty", "-m", "slave-test git-ssh"],
                     ["push", "--force", "origin", self.SSH_BRANCH]):
            log(f"[git-ssh]: git -C {repo_dir} {' '.join(args)}")
            r = self._git(["-C", repo_dir] + args, env)
            if r.returncode != 0:
                return
        result = Git().ls_remote(clone_url, f"refs/heads/{self.SSH_BRANCH}",
                                  env=env, check=False)
        if result.returncode != 0 or not result.stdout.strip():
            self._fail(f"git ls-remote over ssh: pushed branch '{self.SSH_BRANCH}' not found")
            return
        print(f"OK: clone + commit + push + ls-remote over ssh via ssh-agent "
              f"({self.SSH_BRANCH} -> {result.stdout.split()[0][:12]})")
        log("[git-ssh]: ALL STEPS PASSED")

    def _check_embeddings(self):
        api = params.embeddings_api.rstrip("/")
        model = params.embeddings_model
        api_key = params.embeddings_api_key
        print(f"Embeddings API: {api} (model={model})")
        resp = HttpApiClient.http_post_json(f"{api}/embeddings",
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
            with urlopen(req, timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
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
        data_parts = []
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith(":"):
                continue
            if stripped.startswith("data:"):
                data_parts.append(stripped[len("data:"):].lstrip())
            elif not stripped:
                if data_parts:
                    try:
                        parsed = json.loads("\n".join(data_parts))
                    except json.JSONDecodeError:
                        pass
                    data_parts = []
        if data_parts:
            try:
                parsed = json.loads("\n".join(data_parts))
            except json.JSONDecodeError:
                pass
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
        # Only the litellm MCP proxy is checked: it's the single
        # ingress for the in-cluster MCP traffic (gitea / basic-memory
        # / etc. all flow through it), so its reachability is the
        # meaningful signal. The direct ``mcp-gitea`` and
        # ``basic-memory`` URLs are intentionally not exercised here
        # — those backends may be reachable only through the proxy
        # and the proxy is the surface the agents actually call.
        self._check_mcp_server("litellm", params.mcp_litellm_url)

    def _check_telemetry(self):
        endpoint = (params.otel_endpoint or "http://otelcol:4318").rstrip("/")
        print(f"OTLP endpoint: {endpoint}")
        host = urlsplit(endpoint).hostname or "otelcol"
        grpc_port = int(params.otel_grpc_port)
        print(f"\n--- OTLP gRPC connect {host}:{grpc_port} ---")
        try:
            with socket.create_connection((host, grpc_port), timeout=HttpApiClient.HTTP_TIMEOUT):
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
        status, body, _ = HttpApiClient.http_post_json_with_status(f"{endpoint}/v1/traces", payload)
        if status != 200:
            self._fail(f"POST {endpoint}/v1/traces: HTTP {status}: {body[:300]}")
            return
        print(f"OK: trace exported (traceId={trace_id.hex()})")

    def _check_pip_cache(self):
        index_url = (params.pip_index_url or "").rstrip("/")
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
