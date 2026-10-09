#!/usr/bin/env python3
"""Shoggoth: workflow automation + cluster maintenance.

Consolidated from shoggoth_workflow.py and shoggoth_maintenance.py. The
two scripts shared a substantial utility layer (logging, HTTP helpers,
git-cred readiness polling, OpenBao/Gitea/Redmine clients) and were
trivially composable; merging them into a single dispatch removes the
duplication and lets one binary handle both webhook-driven workflows
(task / ci-failure / pr-update) and cron-style cluster maintenance
(webhooks / mirror-sync / slave-access / tokens / ssh-key / slave-test /
ldap-sync).

Secret reads come from Kubernetes Secrets via the ``K8s`` class. The
SecretStore (shoggoth/k3s/secret-store.yaml) keeps the same K8s Secret
names in sync with OpenBao via ExternalSecrets, so this script no
longer talks to OpenBao directly — the kv paths in
``SecretStore.K8S_PATHS`` are translated to the canonical Secret
name + data key at read time.

Usage:
  shoggoth.py [-v] <command> [args...]

Workflow commands (require a webhook payload via GITEA_PAYLOAD or
REDMINE_PAYLOAD):
  task <task_id>
  ci-failure
  pr-update

Maintenance commands:
  argo-webhooks [org ...]
  redmine-webhooks [org ...]
  redmine-argo-webhooks
  github-mirror-sync (reads SHOGGOTH_GITHUB_ORG env var)
  slave-access
  slave-token
  runner-token
  ssh-key
  slave-test [check_name ...]
  gitea-ldap-sync
  redmine-ldap-sync
"""

import argparse
import base64
import binascii
import io
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from urllib.parse import urlencode, urlsplit, urlparse
from urllib.request import Request, urlopen, HTTPCookieProcessor, build_opener
from urllib.error import URLError, HTTPError


# ---------------------------------------------------------------------
# Parameters: single source of truth for every environment variable the
# script reads. Constructed once at module import (``params`` below) so
# every consumer observes the same snapshot. Callers reference
# ``params.gitea_server_url`` instead of ``os.environ["GITEA_SERVER_URL"]``
# scattered across modules — this is the audit surface for what the
# script actually depends on, and it removes the temptation to re-read
# the same env var with different defaults in different call sites.
#
# Required (process will die on import if unset): vars that gate the
# entire script. ``SHOGGOTH_DOMAIN`` is the only such var now —
# every code path uses it (DN construction, webhook URL building,
# namespace resolution). ``GITEA_SERVER_URL`` used to be here, but
# the maintenance steps that consume only Redmine
# (``redmine-argo-webhooks``, ``redmine-ldap-sync``) omit it entirely
# and never instantiate a ``Gitea`` client, so making it required
# would prevent those commands from booting. It's now declared optional
# and validated lazily inside ``Gitea.__init__``, mirroring the
# pattern already in use in ``Redmine.__init__``. A missing required
# var produces the canonical ``die()`` message; logging in ``__init__``
# would happen after the process has already decided to exit, so we
# accept the traceback in favour of early failure.
#
# Optional (with default): vars consumed in narrow contexts where the
# absence is meaningful at the call site (e.g. ``PIP_INDEX_URL`` is only
# checked in the slave-test ``pip-cache`` probe).
# ---------------------------------------------------------------------


class Parameters:
    GROUPS = {
        "Gitea": [
            ("gitea_server_url", "GITEA_SERVER_URL", False, ""),
            ("gitea_payload", "GITEA_PAYLOAD", False, ""),
            ("gitea_git_url", "GITEA_GIT_URL", False, ""),
            ("gitea_ssh_host", "GITEA_SSH_HOST", False, "git"),
            ("gitea_ssh_port", "GITEA_SSH_PORT", False, "22"),
        ],
        "Redmine": [
            ("redmine_server", "REDMINE_SERVER", False, ""),
        ],
        "LDAP": [
            ("ldap_url", "LDAP_URL", False, "ldap://openldap:389"),
        ],
        "Shoggoth": [
            ("shoggoth_domain", "SHOGGOTH_DOMAIN", True, ""),
            ("shoggoth_namespace", "SHOGGOTH_NAMESPACE", False, "shoggoth"),
            ("shoggoth_github_org", "SHOGGOTH_GITHUB_ORG", False, ""),
            ("shoggoth_github_api_delay", "SHOGGOTH_GITHUB_API_DELAY", False, "2"),
            ("shoggoth_slave_user", "SHOGGOTH_SLAVE_USER", False, "slave"),
            ("shoggoth_review_user", "SHOGGOTH_REVIEW_USER", False, "admin"),
            ("shoggoth_trusted_reviewers", "SHOGGOTH_TRUSTED_REVIEWERS", False, "admin"),
            ("shoggoth_argo_run_url", "SHOGGOTH_ARGO_RUN_URL", False, ""),
            ("workspace_src", "WORKSPACE_SRC", False, "/ccws/workspace/src"),
        ],
        "Telemetry": [
            ("otel_endpoint", "OTEL_EXPORTER_OTLP_ENDPOINT", False, ""),
            ("otel_grpc_port", "OTEL_GRPC_PORT", False, "4317"),
        ],
        "Embeddings": [
            ("embeddings_api", "EMBEDDINGS_API", False, "http://litellm:80/v1"),
            ("embeddings_api_key", "EMBEDDINGS_API_KEY", False, "localai"),
            ("embeddings_model", "EMBEDDINGS_MODEL", False, "nomic-embed-text"),
        ],
        "MCP": [
            # The litellm MCP proxy is the only MCP ingress the
            # slave-test verifies (see ``_check_mcp``). The direct
            # ``mcp-gitea`` and ``basic-memory`` backends are not
            # exercised here — they're reachable only through the
            # proxy, and the proxy is the surface the agents call.
            # URL matches the ``BM_MCP_URL`` baked into the slave
            # image (``shoggoth/dockerfiles/slave``).
            ("mcp_litellm_url", "MCP_LITELLM_URL", False, "http://litellm/mcp"),
        ],
        "Pip": [
            ("pip_index_url", "PIP_INDEX_URL", False, ""),
        ],
        "SSH": [
            ("ssh_auth_sock", "SSH_AUTH_SOCK", False, ""),
        ],
    }

    def __init__(self):
        for group, entries in self.GROUPS.items():
            for attr, env_var, required, default in entries:
                value = os.environ.get(env_var, "")
                if required and not value:
                    die(f"{env_var} is required")
                setattr(self, attr, value or default)

        # Derived/composite parameters after the per-group loop so the
        # raw strings above are also available for one-off callers.
        self.shoggoth_github_api_delay = float(self.shoggoth_github_api_delay)

    def resolve_ldap_dn(self, base):
        """Return ``ou=people,dc=<shoggoth_domain_components>``.

        LDAP DNs are assembled from SHOGGOTH_DOMAIN by replacing dots
        with ``,dc=`` separators (``example.org`` -> ``dc=example,dc=org``)
        and prefixing with the conventional `ou=people`. Pulled out as
        a helper because ``ldap-sync`` callers previously hand-rolled
        the same string.
        """
        if not self.shoggoth_domain:
            die("SHOGGOTH_DOMAIN is required for LDAP DN construction")
        return f"ou=people,dc={self.shoggoth_domain.replace('.', ',dc=')}"

    def api_delay(self):
        return self.shoggoth_github_api_delay


# ---------------------------------------------------------------------
# Secrets: tokens and passwords the script needs to do work. Acquired
# exclusively from K8s via ``K8s.read_secret`` — the env-var fallback
# path is intentionally absent. Acquisition path by execution context:
#
#   * Jobs in the ``slave:noble`` image (the ``slave-test`` workflow)
#     do not need secrets: the Gitea/Redmine HTTP API calls route
#     through the web-internal api-proxy which injects auth headers
#     server-side. The script's HTTP client accepts an empty token
#     when the URL host starts with ``api.``.
#
#   * Jobs in the ``shoggoth_tools:alpine`` image (maintenance
#     workflows, ``github-mirror-sync``) have ``kubectl`` plus the
#     RBAC Roles in ``shoggoth/k3s/maintenance-secret-writer.yaml`` /
#     ``argo-workflows.yaml`` and read directly from K8s.
#
# No template wires secret env vars any more; if a workflow template
# starts wiring one, the right fix is to widen the RBAC instead of
# adding an env-var passthrough here.
# ---------------------------------------------------------------------


class SecretStore:
    # Map of OpenBao-style kv path -> (K8s Secret name, data key).
    # Centralised so writers (K8s.upsert_secret) and readers
    # (K8s.read_secret, SecretStore.read) agree on the names. SSO
    # synced the kv values into these Secrets via the
    # ``creationPolicy: Owner`` / ``creationPolicy: Orphan`` rules in
    # shoggoth/k3s/external-secrets/.
    K8S_PATHS = {
        "gitea/server-token": ("gitea-server-token", "token"),
        "gitea/slave-token": ("gitea-slave-token", "token"),
        "redmine/slave-token": ("redmine-slave-token", "token"),
        "openldap/slave-password": ("openldap-slave-password", "password"),
        "openldap/admin-password": ("openldap-admin-password", "password"),
        "openldap/ldapauth-password": ("openldap-ldapauth-password", "password"),
        "ssh/slave-public-key": ("ssh-slave-public-key", "value"),
    }

    def read(self, kv_path, key="value", k8s=None):
        """Read a secret from K8s.

        ``kv_path`` is the OpenBao-style path; the canonical K8s
        Secret name + data key live in ``K8S_PATHS``. ``key``
        overrides the data key from ``K8S_PATHS`` when a Secret has
        more than one entry (the default of ``"value"`` is rarely
        correct — most call sites pass ``key="token"`` or
        ``key="password"``).

        Returns the decoded value, or ``None`` when the Secret is
        absent, the data key is missing, or ``kubectl`` is
        unavailable. Callers that need a value to proceed should
        ``die()`` on ``None``; callers that route through the
        web-internal api-proxy can accept ``None`` and let the
        proxy inject the auth header.
        """
        k8s = k8s or K8s()
        return k8s.read_secret(kv_path, key=key)


# ---------------------------------------------------------------------
# K8s: read and write Kubernetes Secrets via ``kubectl``. Centralises
# the previously-scattered ``kubectl create secret`` plumbing and the
# OpenBao kv-v2 reads (renamed: read from the K8s Secret that ESO synced
# the kv value into).
# ---------------------------------------------------------------------


class K8s:
    """Read/write a Kubernetes Secret via ``kubectl``.

    The SecretStore reads kv values from OpenBao and materialises them
    in the cluster namespace; this class talks to those Secrets directly
    instead of round-tripping through OpenBao. The ``upsert_secret``
    helper predates the class and is folded in here.
    """

    def __init__(self, namespace=None):
        self.namespace = namespace or params.shoggoth_namespace

    def read_secret(self, kv_path, key="value"):
        """Read a K8s Secret and return the decoded value.

        ``kv_path`` is the OpenBao kv path (``gitea/slave-token``,
        ``openldap/slave-password``); the canonical K8s Secret name +
        data key live in ``SecretStore.K8S_PATHS`` so writers and readers
        agree without restating the convention at each call site.
        Returns ``None`` when the Secret is absent, the data key is
        missing, or ``kubectl`` is unavailable (e.g. the ``slave:noble``
        image used by ``slave-test``). Same contract the legacy
        ``OpenBao.get_value`` had.
        """
        if kv_path not in secrets_store.K8S_PATHS:
            die(f"K8s.read_secret: unknown kv path {kv_path!r} "
                f"(add an entry to SecretStore.K8S_PATHS)")
        name, default_key = secrets_store.K8S_PATHS[kv_path]
        key = key or default_key
        result = run(
            ["kubectl", "get", "secret", "-n", self.namespace,
             name, "-o", f"jsonpath={{.data.{key}}}"],
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            return base64.b64decode(result.stdout.strip()).decode()
        except (binascii.Error, UnicodeDecodeError) as e:
            print(f"WARNING: K8s.read_secret: failed to decode "
                  f"{self.namespace}/{name}/{key}: {e}", file=sys.stderr)
            return None

    def upsert_secret(self, name, data, labels=None):
        """Create or patch a Kubernetes Secret via kubectl.

        ``data`` is a dict of key -> raw string value; base64 encoding
        is done here (the apply path takes a full Secret manifest, not
        a ``--from-literal`` round-trip).

        ``labels`` is an optional dict of metadata labels to set on
        the Secret. ``kubectl apply`` is a strategic merge, so
        pre-existing labels are preserved.

        The caller is responsible for RBAC (the
        shoggoth-maintenance-secret-writer ServiceAccount grants
        namespaced create/update/patch on the specific Secret
        names in shoggoth/k3s/maintenance-secret-writer.yaml).
        """
        if not name or not self.namespace:
            print("ERROR: K8s.upsert_secret requires name and namespace",
                  file=sys.stderr)
            return False
        manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": name,
                "namespace": self.namespace,
            },
            "type": "Opaque",
            "data": {k: base64.b64encode(v.encode()).decode()
                     for k, v in data.items()},
        }
        if labels:
            manifest["metadata"]["labels"] = labels
        apply = run(["kubectl", "apply", "-f", "-"],
                    input=json.dumps(manifest), check=False)
        if apply.returncode != 0:
            print(f"ERROR: kubectl apply secret failed: "
                  f"{apply.stderr.strip()}", file=sys.stderr)
            return False
        return True


# ---------------------------------------------------------------------
# Ldap: thin wrapper around ``ldap3`` for the openldap user search used
# by ``redmine-ldap-sync``. The class owns the bind, the connection
# lifecycle, and the result-shape normalisation; specific filters and
# attribute lists live with their callers (separation of concerns:
# reusable connection + per-call arguments).
# ---------------------------------------------------------------------


class Ldap:
    """LDAP client for the shoggoth openldap server.

    Constructed with the bind credentials; ``search_users`` runs a
    single ``inetOrgPerson`` lookup and returns the list of dicts the
    caller wants. ``rediscover-ldap`` style operations that need a
    second bind are not currently required and live elsewhere.
    """

    EXCLUDED_LOGINS = ("admin", "ldapauth", "config-admin")

    def __init__(self, url, bind_dn, bind_password, user_base_dn):
        import ldap3
        self._ldap3 = ldap3
        self.url = url
        self.bind_dn = bind_dn
        self.bind_password = bind_password
        self.user_base_dn = user_base_dn

    def _bind(self):
        server = self._ldap3.Server(self.url, get_info=self._ldap3.NONE)
        return self._ldap3.Connection(
            server,
            user=self.bind_dn,
            password=self.bind_password,
            auto_bind=True,
        )

    def search_users(self, extra_filter=""):
        """Enumerate ``inetOrgPerson`` entries under the user base DN.

        ``extra_filter`` is concatenated inside the outer AND so callers
        can narrow by additional attributes without redefining the
        exclusion list. Returns a list of dicts (``login``,
        ``firstname``, ``lastname``, ``mail``); entries without a
        ``uid`` are skipped.

        Set ``mail`` to ``"<uid>@<shoggoth_domain>"`` when LDAP has no
        mail attribute so downstream Redmine user creation always has
        an RFC-5322 address to write.
        """
        exclusions = "".join(f"(!(uid={login}))" for login in self.EXCLUDED_LOGINS)
        base_filter = f"(&(objectClass=inetOrgPerson){exclusions}{extra_filter})"
        conn = self._bind()
        try:
            ok = conn.search(
                search_base=self.user_base_dn,
                search_filter=base_filter,
                attributes=["uid", "cn", "sn", "mail"],
                size_limit=0,
            )
            if not ok:
                print(f"WARNING: LDAP search returned no result "
                      f"for base={self.user_base_dn}", file=sys.stderr)
                return []
            users = []
            for entry in conn.entries:
                if "uid" not in entry:
                    continue
                uid = str(entry.uid)
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
                    "mail": mail or f"{uid}@{params.shoggoth_domain or 'localhost'}",
                })
            return users
        finally:
            try:
                conn.unbind()
            except Exception:
                pass

    def search_filter_for(self, template):
        """Return a base LDAP filter excluding well-known internal UIDs.

        ``template`` is the caller-specific filter expression (e.g.
        ``GiteaLdapSync``'s ``(&(uid=%[1]s)(mail=%[1]s))``) that should
        be AND-composed with the exclusion list. Kept here so the
        exclusion set lives in one place — ``admin`` / ``ldapauth`` /
        ``config-admin`` are not real workflow accounts.
        """
        exclusions = "".join(f"(!(uid={login}))" for login in self.EXCLUDED_LOGINS)
        return f"(&(objectClass=inetOrgPerson){exclusions}{template})"


def log(msg):
    """Print ``msg`` with one ``[shoggoth]`` prefix per line.
    """
    for line in msg.splitlines() or [""]:
        print(f"[shoggoth] {line}", file=sys.stderr, flush=True)


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


# Module-level singleton. Constructed eagerly at import time so a
# missing required env var fails the whole process with the canonical
# ``die()`` message rather than surfacing mid-workflow when the first
# consumer hits it. Placed after the utility functions (log/die/run)
# so the constructor's ``die()`` call resolves at module-init time.
params = Parameters()
secrets_store = SecretStore()


def _redact_argv_for_log(cmd):
    """Return a copy of ``cmd`` with secret-bearing tokens masked.

    Used only for log output — never for execution. ``run()`` calls
    this on its raw argv so the execution path remains untouched and
    the redactor can be tightened without affecting behavior.
    """
    out = []
    redact_eq = {"--from-literal", "--from-file", "--header",
                 "--user", "--password", "--token", "-u", "-p"}
    redact_kv_prefixes = ("--from-literal=", "--header=",
                          "Authorization:", "X-Vault-Token:",
                          "X-Redmine-API-Key:", "--token=")
    for arg in cmd:
        a = str(arg)
        if any(a.startswith(p) for p in redact_kv_prefixes):
            out.append(a.split("=", 1)[0] + "=***")
        elif a in redact_eq:
            out.append(a)
        elif len(out) and out[-1] in redact_eq:
            out.append("***")
        else:
            out.append(a)
    return out


def run(cmd, **kwargs):
    """Run ``cmd`` via ``subprocess.run`` with capture_output/text on,
    and unconditionally emit one log row per channel. The argv is
    passed verbatim to ``subprocess.run`` (and therefore to the
    process); the log row, however, uses a redacted copy via
    ``_redact_argv_for_log`` so secret-bearing tokens
    (``--from-literal=KEY=VALUE``, ``Authorization: token X``, ``-p
    password``) never land in the Argo workflow log.
    """
    kwargs.setdefault("check", True)
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    log(f"run: {' '.join(_redact_argv_for_log(cmd))}")
    try:
        result = subprocess.run(cmd, **kwargs)
    except FileNotFoundError as e:
        # The first element of ``cmd`` (the executable) is missing
        # from PATH. Returning a synthetic non-zero CompletedProcess
        # lets callers treat this the same as a normal failure —
        # e.g. ``K8s.read_secret`` returns ``None`` when kubectl
        # isn't installed (the ``slave:noble`` image used by
        # ``slave-test`` doesn't ship kubectl and falls through to
        # the web-internal api-proxy for auth).
        log(f"run: executable not found: {cmd[0] if cmd else '?'} ({e})")
        return subprocess.CompletedProcess(cmd, returncode=127,
                                             stdout="",
                                             stderr=f"{cmd[0] if cmd else '?'}: not found")
    except subprocess.TimeoutExpired as e:
        log(f"run: TIMEOUT after {e.timeout}s: {' '.join(_redact_argv_for_log(cmd))}")
        stderr = e.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace") if stderr else ""
        if stderr:
            log(f"run: timeout stderr: {stderr[:500]}")
        return subprocess.CompletedProcess(cmd, returncode=124,
                                             stdout=e.stdout or "",
                                             stderr=e.stderr or "")
    log(f"run: rc={result.returncode}")
    if result.stdout:
        log(f"run: stdout: {result.stdout.strip()[:500]}")
    if result.stderr:
        log(f"run: stderr: {result.stderr.strip()[:500]}")
    return result


class GitCredentials:
    """Readiness probe + diagnostics for the git-cred k8s sidecar.
    """

    READY_PATH = "/shoggoth/git-cred/ready"
    READY_TIMEOUT = 30
    POLL_INTERVAL = 0.5

    def _wait_for_path(self, path, timeout=None):
        deadline = time.time() + (timeout if timeout is not None
                                  else self.READY_TIMEOUT)
        while time.time() < deadline:
            try:
                os.stat(path)
                return True
            except (FileNotFoundError, PermissionError):
                time.sleep(self.POLL_INTERVAL)
        return False

    def _probe_message(self, path):
        try:
            os.stat(path)
            return f"{path} exists but _wait_for_path returned False " \
                f"(timed out after {self.READY_TIMEOUT}s)"
        except FileNotFoundError:
            return (f"{path} does not exist after waiting "
                    f"{self.READY_TIMEOUT}s ")
        except PermissionError as e:
            return (f"{path} is not accessible (permissions) from the main container: {e}")
        except OSError as e:
            return f"{path}: {e}"

    def wait_for_diag(self, label):
        """Wait on the sidecar's readiness flag and return
        ``(ready, probe_message)``. On timeout, ``probe_message`` is the
        diagnostic string for the failure mode (see ``_probe_message``).
        On success, ``probe_message`` is None. ``label`` is a short
        prefix used in the log lines (e.g. ``"git-http"``,
        ``"git-ssh"``, ``"workflow"``).
        """
        log(f"[{label}]: waiting up to {self.READY_TIMEOUT}s for "
            f"git-cred ready flag at {self.READY_PATH}")
        if self._wait_for_path(self.READY_PATH):
            log(f"[{label}]: git-cred ready flag present; daemons observable")
            return True, None
        log(f"[{label}]: timed out, probing errno")
        return False, self._probe_message(self.READY_PATH)

    def wait_until_ready(self):
        """Wait on the sidecar's single readiness flag. On timeout,
        die with a diagnostic so a run without shell access to the
        pod can self-diagnose.
        """
        ready, _ = self.wait_for_diag("workflow")
        if not ready:
            die(
                f"git-cred sidecar did not touch {self.READY_PATH} within "
                f"{self.READY_TIMEOUT}s "
                "(check 'kubectl logs <pod> -c git-cred' for the FATAL line; "
                "verify the pod has an initContainer named init-git-cred-perms "
                "and a sidecar named git-cred)"
            )
        log("git-cred ready")

    def log_listing(self, label):
        run(["whoami"], check=False)
        run(["ls", "-lan", "/shoggoth/git-cred"], check=False)

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
            log(f"[{label}]: {name}={'<set>' if value else '<unset>'} "
                f"(len={len(value)})")


class WsHandler:
    """Wrapper around the wshandler CLI (``wshandler -r <repo_dir> <args>``).
    """

    def __init__(self, repo_dir):
        self.repo_dir = repo_dir

    def _cmd(self, args):
        """Build the ``wshandler -r <repo_dir> <args>`` command list."""
        return ["wshandler", "-r", self.repo_dir] + list(args)

    def status(self, quiet=False):
        args = ["-q", "status"] if quiet else ["status"]
        return run(self._cmd(args))

    def parse_modified_repos(self, status_output):
        """Extract modified sub-repo names from ``wshandler status`` output.
        """
        modified_repos = []
        for line in status_output.strip().splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            repo_name = parts[0]
            flags = parts[3]
            if "M" in flags:
                modified_repos.append(repo_name)
        return modified_repos

    _QUOTE_TRANSLATION = str.maketrans({
        "'": "\u2019",
        '"': "\u201d",
        "`": "\u2018",
    })

    def commit(self, msg, **kwargs):
        """Send ``wshandler commit <msg>`` with the message sanitized
        for the single-quoted bash string wshandler builds around it.
        """
        if msg is not None:
            msg = msg.translate(self._QUOTE_TRANSLATION)
        return run(self._cmd(["commit", msg]), **kwargs)

    def branch_new(self, name, **kwargs):
        return run(self._cmd(["branch", "new", name]), **kwargs)

    def push(self, repos, **kwargs):
        return run(self._cmd(["-p", "version", "push"] + list(repos)), **kwargs)


def normalize_for_branch(text):
    text = text.lower()
    text = re.sub(r"[^a-z0-9]", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


class HttpApiClient:
    """Base class for token-authenticated JSON REST clients.

    Subclasses must set ``self.api_url`` (the API base URL) and may set
    ``self.token`` in their own ``__init__``. The auth scheme is wired
    by overriding ``_auth_header`` to return the API-specific auth
    header dict (e.g. ``{"Authorization": "token ..."}`` for Gitea,
    ``{"X-Redmine-API-Key": "..."}`` for Redmine). Return an empty dict
    when no token is configured — the per-method URL construction and
    HTTP plumbing are inherited from this class.
    """

    # Default ``urlopen`` timeout for every request issued through
    # this client. Read by the static-method helpers (``http_get``,
    # ``http_post_json``, ``http_status``) so it must be a class
    # attribute, not an instance one. Bumped in derived classes
    # only when an endpoint genuinely needs more headroom (none
    # currently do).
    HTTP_TIMEOUT = 30

    @staticmethod
    def _http_warn(label, exc, body_chars=500):
        """Emit the canonical WARNING line for an HTTP failure.

        `label` is the operation prefix, e.g. ``"HTTP GET https://..."`` or
        ``"Redmine admin session POST https://..."``. ``body_chars`` caps the
        response-body fragment included with HTTPError messages (default 500;
        set to 2000 for endpoints whose bodies carry useful diagnostic data,
        e.g. GitHub API error envelopes).

        Always returns None so callers can ``return _http_warn(...)`` from
        their except blocks.
        """
        if isinstance(exc, HTTPError):
            try:
                body = exc.read().decode(errors="replace")[:body_chars]
            except Exception:
                body = ""
            print(f"WARNING: {label} failed: {exc.code} {exc.reason}: {body}",
                  file=sys.stderr)
        elif isinstance(exc, (URLError, OSError)):
            print(f"WARNING: {label} failed: {exc}", file=sys.stderr)
        elif isinstance(exc, json.JSONDecodeError):
            print(f"WARNING: {label} returned invalid JSON: {exc}",
                  file=sys.stderr)
        else:
            print(f"WARNING: {label} failed: {exc!r}", file=sys.stderr)
        return None

    @staticmethod
    def http_get(url, headers=None, params=None):
        if params:
            url = f"{url}?{urlencode(params)}"
        log(f"GET {url}")
        req = Request(url, headers=headers or {})
        try:
            with urlopen(req, timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
                data = json.loads(resp.read().decode())
                log(f"GET {url} -> {resp.status}")
                return data
        except (HTTPError, URLError, OSError, json.JSONDecodeError) as e:
            return HttpApiClient._http_warn(f"HTTP GET {url}", e)

    @staticmethod
    def _json_request(method, url, payload, headers=None, quiet=False):
        """Build a JSON Request for ``method`` (``POST``/``PATCH``/``PUT``/``DELETE``).

        Centralises the (data, headers, Request) construction that every JSON
        HTTP wrapper below repeats. ``payload`` may be ``None`` for endpoints
        that accept a JSON body or no body interchangeably; callers that need
        a real request without a body should use ``http_delete`` instead.
        When ``quiet`` is set the pre-send method log is suppressed (matches
        the original ``http_post_json(quiet=True)`` behaviour).
        """
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        data = json.dumps(payload).encode() if payload is not None else None
        if not quiet:
            log(f"{method} {url}")
        return Request(url, data=data, headers=hdrs, method=method)

    @staticmethod
    def http_post_json(url, payload, headers=None, quiet=False):
        req = HttpApiClient._json_request("POST", url, payload, headers, quiet=quiet)
        try:
            with urlopen(req, timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
                result = json.loads(resp.read().decode())
                if not quiet:
                    log(f"POST {url} -> {resp.status}")
                return result
        except (HTTPError, URLError, OSError, json.JSONDecodeError) as e:
            return HttpApiClient._http_warn(f"HTTP POST {url}", e)

    @staticmethod
    def http_post_json_with_status(url, payload, headers=None):
        req = HttpApiClient._json_request("POST", url, payload, headers)
        try:
            with urlopen(req, timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
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

    @staticmethod
    def http_status(url, headers=None):
        req = Request(url, headers=headers or {}, method="GET")
        try:
            with urlopen(req, timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
                return resp.status
        except HTTPError as e:
            return e.code
        except (URLError, OSError):
            return 0

    @staticmethod
    def http_get_with_status(url, headers=None):
        """GET ``url`` and return ``(status_code, parsed_json_or_None)``.

        Returns ``(0, None)`` on network error, ``(status, None)``
        for non-JSON bodies, ``(status, body_dict)`` for JSON bodies.
        The body is best-effort — callers that only need the status
        code should use ``http_status`` instead. This is the
        counterpart used by the token-validation checks (e.g.
        ``SlaveToken._api_rejects_token``) where the operator
        wants to know *who* the stored token authenticates as,
        not just that the call succeeded.
        """
        req = Request(url, headers=headers or {}, method="GET")
        try:
            with urlopen(req, timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
                raw = resp.read().decode(errors="replace")
                try:
                    return resp.status, json.loads(raw)
                except json.JSONDecodeError:
                    return resp.status, None
        except HTTPError as e:
            return e.code, None
        except (URLError, OSError):
            return 0, None

    def _auth_header(self):
        return {}

    def _headers(self):
        """Default headers for token-authenticated JSON requests.

        Includes Content-Type/accept because every public Gitea/Redmine
        endpoint expects application/json. Endpoints that need a
        different Accept (e.g. ``.diff``) build their own Request and
        pass headers via ``_auth_header`` directly.
        """
        hdrs = {"Content-Type": "application/json",
                "accept": "application/json"}
        hdrs.update(self._auth_header())
        return hdrs

    def _resolve(self, path, headers=None):
        """Return ``(url, hdrs)`` for ``path``: prepends ``api_url`` and
        falls back to ``self._headers()`` when ``headers`` is not given.

        Used by every public HTTP method on this class so the
        URL-composition / header-defaulting pair lives in one place.
        """
        url = f"{self.api_url}/{path}"
        hdrs = headers if headers is not None else self._headers()
        return url, hdrs

    def _send_json(self, method, path, payload, headers=None):
        """Send a JSON ``method`` and return the parsed response body.

        Returns ``{}`` on an empty body and re-raises on HTTP/URL/OS
        errors after logging the canonical ``HTTP <method> <url>``
        warning via ``_http_warn``. Shared by ``patch`` and ``put_json``
        which both want a parsed body (or raise) and were previously
        duplicated line-for-line apart from the verb.
        """
        url, hdrs = self._resolve(path, headers)
        req = self._json_request(method, url, payload, hdrs)
        try:
            with urlopen(req, timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
                body = resp.read().decode()
                log(f"{method} {url} -> {resp.status}")
                if not body:
                    return {}
                return json.loads(body)
        except (HTTPError, URLError, OSError) as e:
            self._http_warn(f"HTTP {method} {url}", e)
            raise

    def _send_status(self, method, path, payload=None, headers=None):
        """Send a JSON ``method`` and return ``True`` on success.

        On HTTP/URL/OS error logs the canonical warning and returns the
        ``_http_warn`` return value (``None`` — falsy). Used by ``put``
        and ``delete``: both only need the success/failure signal and
        were duplicated apart from the verb. ``payload`` defaults to
        ``None`` so ``delete`` (no body) and ``put`` (optional body)
        share the same plumbing.
        """
        url, hdrs = self._resolve(path, headers)
        req = self._json_request(method, url, payload, hdrs)
        try:
            with urlopen(req, timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
                resp.read()
                log(f"{method} {url} -> {resp.status}")
                return True
        except (HTTPError, URLError, OSError) as e:
            return self._http_warn(f"HTTP {method} {url}", e)

    def get(self, path, params=None, headers=None):
        url, hdrs = self._resolve(path, headers)
        return self.http_get(url, headers=hdrs, params=params)

    def post(self, path, payload, headers=None):
        url, hdrs = self._resolve(path, headers)
        return self.http_post_json(url, payload, headers=hdrs)

    def patch(self, path, payload, headers=None):
        return self._send_json("PATCH", path, payload, headers)

    def put_json(self, path, payload, headers=None):
        """PUT that returns the parsed response body (raises on HTTP error).

        Mirrors ``post``/``patch`` (which return bodies); the boolean
        variant ``put`` is the sibling for endpoints that only need the
        success signal.
        """
        return self._send_json("PUT", path, payload, headers)

    def put(self, path, payload=None, headers=None):
        """PUT without consuming a response body; returns True on success.

        Parallel to ``delete`` (also returns a boolean). Used by
        endpoints like ``PUT /teams/{id}/members/{user}`` where the
        success path only needs the status code, not a parsed body.
        """
        return self._send_status("PUT", path, payload, headers)

    def delete(self, path, payload=None, headers=None):
        return self._send_status("DELETE", path, payload, headers)

    def status(self, path, headers=None):
        url, hdrs = self._resolve(path, headers)
        return self.http_status(url, headers=hdrs)

    @staticmethod
    def _paginate(fetch_page, limit=50):
        """Drive a paginated JSON endpoint.

        ``fetch_page(page, limit)`` is called repeatedly with
        incrementing ``page`` until it returns None or an empty
        iterable. Iteration stops early when a page returns fewer than
        ``limit`` items (the API considered that the last page) so
        callers don't have to inspect the response to detect the
        terminal page.

        Marked staticmethod because it doesn't touch any per-instance
        state — it's a generic pagination loop shared by every
        subclass's list endpoint (``Gitea.list_orgs``, ``Gitea.list_branches``,
        ``Github.list_repos``, etc.). Static so subclasses can't
        accidentally inherit a hook into ``self``.
        """
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


# ---------------------------------------------------------------------
# Gitea: unified API client.
#
# Tokens are read from K8s via ``secrets_store.read`` (kv paths
# ``gitea/server-token`` and ``gitea/slave-token``). The script
# tolerates an empty token when the API URL host starts with
# ``api.`` — the web-internal proxy injects the Authorization
# header in that case.
#
# Token resolution: admin token first (full access), then slave token,
# then empty. The token list (``self._paginate``, `_basic_headers`) endpoints
# need HTTP Basic auth (admin:token) on /users/{name}/tokens — that's
# what `_basic_headers` is for; token-auth doesn't have the right
# scopes there.
# ---------------------------------------------------------------------


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
            params.shoggoth_trusted_reviewers.split(","))
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


# ---------------------------------------------------------------------
# Redmine: unified API + session-auth client.
#
# Workflow context uses token auth (REDMINE_TOKEN / OpenBao fallback)
# for read/update of issues; maintenance context additionally uses
# session-based admin auth (CSRF token + cookie + LDAP password) for
# operations that don't accept the API key (user creation, project
# membership).
# ---------------------------------------------------------------------


class Redmine(HttpApiClient):
    def __init__(self, k8s=None):
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
        self.token = secrets_store.read("redmine/slave-token", key="token")
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


# ---------------------------------------------------------------------
# GitHub: unauthenticated REST client (used by github-mirror-sync).
# ---------------------------------------------------------------------


class Github(HttpApiClient):
    """Unauthenticated GitHub REST client.

    Inherits the JSON REST plumbing from ``HttpApiClient``; only the
    rate-limit retry loop (specific to GitHub's 60-req/hour
    unauthenticated quota) and the unauthenticated accept header
    override ``get``. Future endpoints that don't need rate-limit
    handling (e.g. authenticated POSTs) can use the inherited
    ``post`` / ``put_json`` / ``delete`` directly.

    Per-repo branch/tag listing is done via ``git ls-remote`` (see
    ``GithubMirrorSync``), which does not count against the REST
    quota.
    """

    RATE_LIMIT_MAX_WAIT = 900

    def __init__(self):
        self.api_url = "https://api.github.com"
        self.api_delay = params.shoggoth_github_api_delay

    def get(self, path, params=None):
        # Override the base ``get`` because GitHub's quota policy
        # requires a retry-once-on-429/403 loop that needs the raw
        # ``HTTPError`` (to read ``Retry-After`` / ``X-RateLimit-Reset``
        # headers). The base ``get`` swallows HTTPError after
        # ``_http_warn`` so a 429 is indistinguishable from a 404 —
        # the retry loop has to live here with the urllib plumbing
        # inlined. Other methods (``post`` / ``put_json`` / etc.)
        # remain available from the base class without override.
        url = f"{self.api_url}/{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        for attempt in (1, 2):
            time.sleep(self.api_delay)
            log(f"GET {url}")
            req = Request(url, headers={"accept": "application/json"})
            try:
                with urlopen(req, timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
                    data = json.loads(resp.read().decode())
                    log(f"GET {url} -> {resp.status}")
                    return data
            except HTTPError as e:
                if attempt == 1 and e.code in (403, 429) and self._wait_for_quota(e, url):
                    continue
                return self._http_warn(f"HTTP GET {url}", e, body_chars=2000)
            except (URLError, OSError, json.JSONDecodeError) as e:
                return self._http_warn(f"HTTP GET {url}", e, body_chars=2000)
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
        return self._paginate(lambda page, limit: self.get(
            f"{account_type}/{account}/repos",
            params={"page": page, "per_page": limit, "type": "public"}), limit=100)

# ---------------------------------------------------------------------
# OtlpLogger: stream-json assistant output capture + OTLP HTTP push.
# ---------------------------------------------------------------------


class OtlpLogger:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.session_id = None
        self.service_name = None
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
        result = HttpApiClient.http_post_json(f"{self.endpoint}/v1/logs", body, quiet=True)
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
        self.session_id = None

    def stop_session(self):
        log(f"otlp: stop_session pushed={self._pushed_count} "
            f"failed={self._failed_count}")

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
    # Default per-invocation wall-clock cap for ``prompt()`` / the
    # ``pr-comment`` per-comment retries. The workflow template's
    # ``activeDeadlineSeconds`` (4 h on coding-agent-dispatcher) is
    # the outer safety net; this is the per-prompt ceiling so a
    # single hung agent doesn't burn the whole pod's budget.
    AGENT_TIMEOUT = 1200

    # Forbidden bash glob patterns the agent must NEVER invoke — git
    # commits/pushes/tags, wsh and wshandler wrappers — encoded as
    # the Qwen Code permission-deny syntax. Pulled out of the module
    # top level because every consumer is inside this class.
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

    # Allowed tool sets per session kind. ``full`` is unrestricted;
    # ``pr-review`` / ``pr-comment`` exclude mutating tools because
    # those sessions are read-only reviews of work the dispatcher is
    # evaluating.
    TOOL_PROFILES = {
        "full": [],
        "pr-review": [
            "edit",
            "write_file",
            "notebook_edit",
        ],
        "pr-comment": [],
    }

    # Map Qwen session kind (``start_session(event_type=...)``) to
    # the corresponding ``TOOL_PROFILES`` key.
    TOOL_PROFILE_BY_SESSION = {
        "task": "full",
        "ci-failure": "full",
        "pr-review": "pr-review",
        "pr-comment": "pr-comment",
    }

    # Env-var names whose values must NEVER leak to the qwen subprocess.
    # Mirrors ``qwen_security``: stripping these from the inherited env
    # before Popen() keeps workflow-injected secrets (OpenBao address,
    # default AI token) out of the model context. The Gitea / Redmine /
    # LDAP / SSH tokens are no longer injected by workflow templates
    # (the script reads them from K8s directly), so they don't need
    # filtering here. SHOGGOTH_VAULT_TOKEN / OPENBAO_ADDR are kept as a
    # defensive measure — they are no longer read by this script after
    # the K8s refactor, but if a workflow template still injects them,
    # they shouldn't reach the agent either.
    SECRET_ENV_KEYS = frozenset([
        "SHOGGOTH_VAULT_TOKEN", "OPENBAO_ADDR",
        "SHOGGOTH_AI_DEFAULT_TOKEN",
    ])

    ANTI_LEAK_DIRECTIVE = (
        "Do not emit thinking markup (e.g., <thinking>, <thought>, or any "
        "XML/HTML tags that resemble reasoning traces). Output only the final "
        "answer.\n\n"
    )

    @staticmethod
    def _filter_qwen_env():
        return {k: v for k, v in os.environ.items()
                if k not in Agent.SECRET_ENV_KEYS}

    @staticmethod
    def _extract_review_json(text):
        """Find and parse a JSON review object from agent assistant text.

        Looks for the first ```json ... ``` fenced block (or a bare JSON
        object ending at the END_OF_REVIEW marker), validates it has the
        expected shape, and returns the parsed dict. Returns None if
        nothing usable is found.
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

    @staticmethod
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

    def __init__(self, shoggoth):
        self.shoggoth = shoggoth
        self.otlp = OtlpLogger(params.otel_endpoint)
        self.tool_profile = "full"
        self._qwen_restrict_path = None

    def start_session(self, event_type):
        self.tool_profile = Agent.TOOL_PROFILE_BY_SESSION.get(event_type, "full")
        log(f"start_session: event_type={event_type} "
            f"tool_profile={self.tool_profile}")
        self.otlp.start_session(event_type)

        self.qwen_env = self._filter_qwen_env()

        deny_rules = Agent.QWEN_DENY_PATTERNS
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
        plugin_archive = run(["curl", "-sfS", "--max-time", "30", "-o", "/tmp/plugin.tar.gz", plugin_url],
                             check=False, capture_output=True, text=True, timeout=60)
        if plugin_archive.returncode != 0:
            die(f"failed to download plugin from {plugin_url}: {plugin_archive.stderr.strip()}")

        plugin_install = run(["qwen", "extensions", "install", "/tmp/plugin.tar.gz",
                              "--scope", "user", "--consent"],
                             check=False, capture_output=True, text=True, timeout=60)
        if plugin_install.returncode != 0:
            err = (plugin_install.stderr or plugin_install.stdout or "").strip()
            die(f"failed to install plugin from {plugin_url}: {err}")

        index_path = self.shoggoth.repo_dir
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
        for excluded in Agent.TOOL_PROFILES.get(self.tool_profile, []):
            cmd.extend(["--exclude-tools", excluded])
        if resume:
            cmd.extend(["--resume", self.otlp.session_id])
        else:
            cmd.extend(["--session-id", self.otlp.session_id])
        cmd.extend(["--prompt", Agent.ANTI_LEAK_DIRECTIVE + text])

        log(f"prompt: resume={resume} session={self.otlp.session_id} "
            f"timeout={timeout} tool_profile={self.tool_profile}")
        log(f"prompt: cmd={' '.join(cmd[:6])}... (prompt length={len(text)})")
        log(f"prompt: text:\n{Agent.ANTI_LEAK_DIRECTIVE}{text}")
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


# ---------------------------------------------------------------------
# Shoggoth: project identification + repo checkout + PR-diff machinery.
# ---------------------------------------------------------------------


class Shoggoth:
    def __init__(self, redmine, gitea, args):
        self.project = None
        self.domain = params.shoggoth_domain
        self.github_org = params.shoggoth_github_org
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
        slave_user_login = params.shoggoth_slave_user
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
        workspace_dir = params.workspace_src
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
                        "-s", f"s|ssh://git@github.com:{org_re}|ssh://git@git.{self.domain}/{org_re}|g",
                        "-s", f"s|git+ssh://git@github.com/{org_re}|ssh://git@git.{self.domain}/{org_re}|g",
                        "-p", "shallow"]
            if self.working_branch:
                wsh_args += ["-P", self.working_branch]
            wsh_args.append("update")
            ws_handler = WsHandler(workspace_dir)
            ws_handler.run(wsh_args)

            log("checkout: post-update repository list:")
            ws_handler.status(quiet=True)

            apt_update = run(["sudo", "-S", "apt", "update"], check=False, input="ccws\n")
            if apt_update.stdout:
                print(apt_update.stdout, file=sys.stderr, flush=True)
            if apt_update.stderr:
                print(apt_update.stderr, file=sys.stderr, flush=True)
            if apt_update.returncode != 0:
                die(f"apt update failed with exit code "
                    f"{apt_update.returncode}: "
                    f"{(apt_update.stderr or '').strip()[-500:]}")

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
    def _rewrite_patch_paths(patch_text):
        """Rewrite `diff --git a/<path> b/<path>` headers so the patch can be
        applied at a sub-repo path instead of the manifest root.
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


# ---------------------------------------------------------------------
# Workflow commands (task / ci-failure / pr-update).
# ---------------------------------------------------------------------


class CommandBase:
    def __init__(self, gitea, redmine, agent, shoggoth=None):
        self.gitea = gitea
        self.redmine = redmine
        self.agent = agent
        self.shoggoth = shoggoth

    @staticmethod
    def _stage_and_commit_ccws(repo_dir, modified_repos, commit_msg):
        """Stage each sub-repo's working tree and commit via the wshandler wrapper.

        Identical step sequence between `_commit_only_ccws` (review-comment
        fix) and `commit_push_and_create_mr_ccws` (task implementation):
        for every modified sub-repo `git add -A`, then one manifest-wide
        `wshandler commit` that walks all sub-repos and creates a commit per
        repo that has staged changes. Failure at any step is fatal — the
        workflow script's caller is a webhook (Argo WorkflowEventBinding),
        so partial commits would leave the PR in an unrecoverable state.
        """
        for repo_name in modified_repos:
            repo_path = os.path.join(repo_dir, repo_name)
            add = run(["git", "-C", repo_path, "add", "-A"], check=False)
            if add.returncode != 0:
                die(f"failed to stage changes in {repo_name}: {add.stderr}")
        commit = WsHandler(repo_dir).commit(commit_msg, check=False)
        if commit.returncode != 0:
            die(f"git commit failed: {commit.stderr}")

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
        ws_handler = WsHandler(repo_dir)
        status = ws_handler.status(quiet=True)
        if not status.stdout.strip():
            self._pending_modified_repos = []
            return False

        modified_repos = ws_handler.parse_modified_repos(status.stdout)
        if not modified_repos:
            self._pending_modified_repos = []
            return False

        self._stage_and_commit_ccws(repo_dir, modified_repos, commit_msg)
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
            push = WsHandler(repo_dir).push(modified_repos, check=False)
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
        slave_user = params.shoggoth_slave_user
        review_user = params.shoggoth_review_user
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
        ws_handler = WsHandler(repo_dir)
        log("commit_push: pre-commit repository status:")
        status = ws_handler.status()
        if not status.stdout.strip():
            print("No local changes, skipping commit and push")
            return False, []

        modified_repos = ws_handler.parse_modified_repos(status.stdout)
        if not modified_repos:
            print("No local changes, skipping commit and push")
            return False, []

        sanitized_subject = self._sanitize_subject(task_subject)
        commit_msg = f"Task#{task_id}: {sanitized_subject}"

        ws_handler.branch_new(branch)

        self._stage_and_commit_ccws(repo_dir, modified_repos, commit_msg)
        push = ws_handler.push(modified_repos, check=False)
        if push.returncode != 0:
            die(f"failed to push branches: {push.stderr}")

        log("commit_push: post-push repository status:")
        status = ws_handler.status()
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
                f"fix\"\n"
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
                                              timeout=Agent.AGENT_TIMEOUT,
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

            review_data = self.agent._extract_review_json(review_text)
            if review_data is not None:
                review_md = self.agent._format_review_json(
                    review_data, pr_title, pr_url)
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
                slave_user = params.shoggoth_slave_user
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

        slave_user = params.shoggoth_slave_user
        log(f"pr-review: removing requested reviewer {slave_user}")
        self.gitea.remove_requested_reviewer(pr_repo, pr_number, slave_user)
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
                                                  timeout=Agent.AGENT_TIMEOUT,
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
                run_url = params.shoggoth_argo_run_url
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
                resume=True, timeout=Agent.AGENT_TIMEOUT,
                retry_label="pr-comment-finalize")
            if rc == 124:
                self._handle_timeout(pr_repo, pr_number, pr_url, resolved_comments)
            if rc != 0:
                die(f"qwen agent exited with code {rc}")

            if task_subject:
                rc = self.agent.prompt_with_retry(
                    f"Update basic memory with any new information learned about "
                    f"the task \"{task_subject}\".", resume=True, timeout=Agent.AGENT_TIMEOUT,
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
                run_url = params.shoggoth_argo_run_url
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
        run_url = params.shoggoth_argo_run_url
        run_url_suffix = f" Argo run: {run_url}." if run_url else ""
        self._push_pending_commits()
        timeout_msg = (
            f"Agent timed out after {Agent.AGENT_TIMEOUT // 60} minutes. "
            f"Addressed {resolved_count} comment(s) before timeout. "
            f"Committed changes have been pushed."
            f"{run_url_suffix}"
        )
        self.gitea.post_pr_review_chunked(pr_repo, pr_number, timeout_msg)
        die(timeout_msg)


# ---------------------------------------------------------------------
# Maintenance commands: webhooks / mirror-sync / slave / tokens / ldap.
# ---------------------------------------------------------------------


class GiteaCommand:
    """Base for maintenance commands that operate against a Gitea instance.

    Stores the shared client reference and the per-command ``errors`` counter
    used by ``die(... completed with N error(s))`` at the end of each
    command's ``execute()``. Subclasses call ``super().__init__(gitea)``
    before their own env-var resolution.
    """

    def __init__(self, gitea):
        self.gitea = gitea
        self.errors = 0

    def _resolve_orgs(self, projects):
        """Resolve the list of Gitea org names to act on.

        Uses the explicit list if non-empty, otherwise enumerates every
        org the Gitea admin token can see. Returns ``None`` when no
        projects exist so the caller can exit early with a "No Gitea projects
        found" diagnostic. Returns an empty list (not ``None``) when an
        explicit empty list was passed — preserves the difference between
        "default fallback found nothing" and "operator explicitly passed
        no projects".
        """
        if projects:
            return list(projects)
        orgs = self.gitea.list_orgs()
        if not orgs:
            return None
        return [o.get("username") for o in orgs if o.get("username")]


class SetupArgoWebhooks(GiteaCommand):
    """Configure Gitea org webhooks to deliver events to Argo Workflows.

    Replaces the Kestra-based equivalent that pointed at
    /api/v1/main/executions/webhook/. Argo's events endpoint is
    /api/v1/events/{namespace}/{discriminator} and accepts an arbitrary
    JSON body (no path-suffix key). The discriminator must match the
    WorkflowEventBinding selector (gitea-pr-update / gitea-ci-failure).
    """

    def __init__(self, gitea):
        super().__init__(gitea)
        # Webhook URLs always go through the web-internal gateway at
        # api.{domain}/argo/ — the gateway injects the
        # argo-webhook-receiver SA token before proxying to argo:80,
        # so the v4 webhook interceptor (which has no gitea parser)
        # is short-circuited by the Authorization header and
        # argo-server's gatekeeper does bearer validation against k8s.
        # See shoggoth/k3s/web-internal.yaml for the gateway contract.
        self.namespace = params.shoggoth_namespace

    def execute(self, projects=None):
        projects = self._resolve_orgs(projects)
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


class SetupRedmineWebhooks(GiteaCommand):
    def __init__(self, gitea):
        super().__init__(gitea)
        redmine_server = params.redmine_server
        if not redmine_server:
            domain = self.gitea.domain
            if not domain:
                die("REDMINE_SERVER or SHOGGOTH_DOMAIN is required")
            redmine_server = f"http://redmine.{domain}"
        self.redmine_host = redmine_server.replace("https://", "").replace("http://", "")
        # Webhook signing is intentionally disabled: the
        # ``redmine_forgejo_webhook`` plugin reads its
        # ``Setting.plugin_redmine_forgejo_webhook['secret_token']``
        # from the Redmine plugin settings (DB), not an env var or
        # K8s Secret. There is no shared-secret source for the script
        # to mirror here, so the Gitea org hook is created with
        # ``secret=None`` and Redmine accepts unsigned webhooks. If
        # shared-secret signing is needed in the future, add a K8s
        # Secret and ``SecretStore.K8S_PATHS`` entry, then read it
        # here via ``K8s.read_secret``.
        self.webhook_secret = None
        self.webhook_url = f"http://{self.redmine_host}/forgejo/webhook"

    def execute(self, projects=None):
        projects = self._resolve_orgs(projects)
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
        self.namespace = params.shoggoth_namespace
        domain = params.shoggoth_domain
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


class SlaveAccess(GiteaCommand):
    def __init__(self, gitea):
        super().__init__(gitea)
        self.slave_user = params.shoggoth_slave_user
        self.automation_users = [u for u in (self.slave_user,) if u]

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


class SshKey(GiteaCommand):
    KEY_TITLE = "shoggoth-slave"
    K8S_KV_PATH = "ssh/slave-public-key"

    def __init__(self, gitea, k8s=None):
        GiteaCommand.__init__(self, gitea)
        self.k8s = k8s or K8s()
        self.slave_user = params.shoggoth_slave_user

    def execute(self):
        # Read the slave's public SSH key from K8s. The maintenance
        # SA's ``shoggoth-maintenance-secret-writer-ldap-ssh-read``
        # Role grants ``get`` on this Secret.
        public_key = secrets_store.read(self.K8S_KV_PATH, key="value", k8s=self.k8s)
        if not public_key:
            print(f"ERROR: SSH public key not found in K8s Secret '{self.K8S_KV_PATH}' "
                  "(grant the script's SA `get` on ssh-slave-public-key)",
                  file=sys.stderr)
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
        # the same path the coding-agent-dispatcher workflow relies on for
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

        result = run(["git", "ls-remote", clone_url, f"refs/heads/{branch}"], env=env, check=False)
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
        result = run(["git", "ls-remote", clone_url, f"refs/heads/{self.SSH_BRANCH}"], env=env, check=False)
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


class SlaveToken(GiteaCommand):
    TOKEN_NAME = "shoggoth-slave"
    TOKEN_SCOPES = ["write:repository", "write:issue", "read:user"]
    K8S_SECRET_NAME = "gitea-slave-token"

    def __init__(self, gitea):
        GiteaCommand.__init__(self, gitea)
        self.namespace = params.shoggoth_namespace
        self.slave_user = params.shoggoth_slave_user

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

    def _api_rejects_token(self):
        """Hit the Gitea API with ``token`` and return True if
        Gitea rejects it.

        Two failure modes drive a rotation:
          1. The K8s Secret ``gitea-slave-token`` has no value
             (the ExternalSecret was never materialised, the
             previous write failed, or the PushSecret to OpenBao
             hasn't converged yet). An empty stored value is
             treated as "rejected" — there is no point skipping
             rotation when the consumers have nothing to work
             with.
          2. ``GET /api/v1/user`` with the stored value returns a
             non-2xx status. Gitea returns 401 when the token is
             missing, revoked, or the user has been deleted; 403
             when the scopes don't cover the endpoint. Both are
             "Gitea says the stored value is bad" and trigger
             rotation.

        Returns True (rotate) when the API rejects the token or
        the K8s Secret is empty; False (keep) when the API
        authenticates the request with HTTP 200. The Gitea user
        identity returned by the endpoint is logged so the
        operator can confirm the stored value is binding to the
        right user, not just any 200.
        """
        stored = K8s().read_secret("gitea/slave-token", key="token")
        if not stored:
            print(f"API check: K8s Secret '{self.K8S_SECRET_NAME}' is empty "
                  f"(no token value) → will rotate")
            return True
        masked = (stored[:4] + "..." + stored[-4:]
                  if len(stored) > 8 else "***")
        # ``self.gitea.api_url`` is the constructor's
        # ``params.gitea_server_url + "/api/v1"`` — the
        # ``/api/v1`` prefix is already there. Appending another
        # ``/api/v1/user`` produced the malformed
        # ``/api/v1/api/v1/user`` URL that Gitea answered with
        # 404, which the script then misread as "token
        # rejected" and triggered an unnecessary rotation.
        url = f"{self.gitea.api_url}/user"
        print(f"API check: GET {url} with Authorization: token {masked}")
        status, body = HttpApiClient.http_get_with_status(
            url, headers={"Authorization": f"token {stored}"})
        if status == 200:
            login = (body or {}).get("login", "?")
            print(f"API check: HTTP 200 (authenticated as '{login}') — "
                  f"stored token is good")
            return False
        print(f"API check: HTTP {status} — stored token is REJECTED by Gitea "
              f"→ will rotate")
        return True

    def execute(self):
        existing_tok = self._find_existing_token()
        existing_tok_id = (existing_tok.get("id") if existing_tok else None)
        if existing_tok is not None:
            # Run the API check first — it's the most reliable
            # signal: it answers the actual question "can the
            # stored token talk to Gitea?" instead of inferring
            # it from metadata that may be stale or wrong.
            # ``_is_token_expired`` and ``_has_outdated_scopes``
            # are kept as belt-and-suspenders for the metadata
            # cases the API might still accept (e.g. the scopes
            # could be wrong but the user is still alive — the
            # API check passes, the scope check catches it).
            api_rejects = self._api_rejects_token()
            expired = self._is_token_expired(existing_tok)
            outdated = self._has_outdated_scopes(existing_tok)
            if api_rejects or expired or outdated:
                reasons = []
                if api_rejects: reasons.append("Gitea rejected the stored token")
                if expired:     reasons.append("expired")
                if outdated:    reasons.append("scopes drifted")
                reason = "; ".join(reasons)
                print(f"Will rotate token '{self.TOKEN_NAME}' "
                      f"({reason}, id={existing_tok_id})")
            else:
                print(f"Token '{self.TOKEN_NAME}' is still valid, "
                      "skipping creation")
                return

        # Gitea has no atomic "swap token" API — the access-token name
        # must be unique per user, so the create endpoint returns 400
        # "access token name has been used already" when an old token
        # with the same name is still present. We have to delete the
        # existing token first; only then can we create the new one.
        # This means there is an unavoidable window where consumers
        # (the K8s Secret, the git-cred sidecar, the web-internal
        # gateway proxy_set_header) still hold the old value while the
        # old token is gone — those consumers will see 401 from Gitea
        # until the write below completes. Re-running this command
        # after a mid-rotation failure is the recovery: by then the
        # old token is gone, so the create path succeeds and the write
        # overwrites the stale consumer value.
        if existing_tok_id is not None:
            print(f"Deleting existing token '{self.TOKEN_NAME}' (id={existing_tok_id})")
            if not self.gitea.delete_user_token(self.slave_user, existing_tok_id):
                print(f"ERROR: Failed to delete existing token (id={existing_tok_id}); "
                      "the name is in use and cannot create a replacement",
                      file=sys.stderr)
                self.errors += 1
                die(f"slave-token completed with {self.errors} error(s)")

        print(f"Creating token '{self.TOKEN_NAME}' for user '{self.slave_user}'")
        result = self.gitea.create_user_token(self.slave_user, self.TOKEN_NAME, self.TOKEN_SCOPES)
        if result is None or not result.get("sha1") or not result.get("id"):
            print(f"ERROR: Failed to create token for {self.slave_user}", file=sys.stderr)
            self.errors += 1
            die(f"slave-token completed with {self.errors} error(s)")

        new_tok_id = result["id"]
        token_value = result["sha1"]

        if not self.namespace:
            print("ERROR: SHOGGOTH_NAMESPACE is required for K8s Secret write", file=sys.stderr)
            self.errors += 1
            self._rollback_new_token(new_tok_id)
            die(f"slave-token completed with {self.errors} error(s)")

        print(f"Storing token in K8s Secret '{self.K8S_SECRET_NAME}' in namespace '{self.namespace}'")
        k8s_ok = K8s(self.namespace).upsert_secret(self.K8S_SECRET_NAME, {"token": token_value})
        if not k8s_ok:
            print(f"ERROR: Failed to write K8s Secret '{self.K8S_SECRET_NAME}'", file=sys.stderr)
            self.errors += 1
            self._rollback_new_token(new_tok_id)
            die(f"slave-token completed with {self.errors} error(s)")

        print(f"slave-token: token rotated and stored successfully")

    def _rollback_new_token(self, new_tok_id):
        """Best-effort delete of the just-created token.

        Called when the K8s write fails AFTER the create step, so
        consumers don't accumulate orphan tokens with no usable K8s
        value. We can't undo the delete-old-then-create step (the old
        sha1 is gone from Gitea by the time we reach this branch),
        but we CAN keep the Gitea state clean so the next run starts
        from a known "no token with this name" baseline.
        """
        if self.gitea.delete_user_token(self.slave_user, new_tok_id):
            print(f"Rolled back new token (id={new_tok_id}); K8s Secret "
                  "will see stale value until the next slave-token run succeeds")
        else:
            print(f"WARNING: rollback failed; orphan token (id={new_tok_id}) "
                  "for TOKEN_NAME remains in Gitea. The next slave-token run "
                  "will see it as `existing_tok` and attempt to delete it.",
                  file=sys.stderr)


class RunnerToken(GiteaCommand):
    K8S_SECRET_NAME = "gitea-runner-token"

    def __init__(self, gitea):
        super().__init__(gitea)
        self.org = params.shoggoth_github_org
        self.namespace = params.shoggoth_namespace

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
                if not K8s(self.namespace).upsert_secret(self.K8S_SECRET_NAME, {"token": token_value}):
                    print(f"ERROR: Failed to write K8s Secret '{self.K8S_SECRET_NAME}'", file=sys.stderr)
                    self.errors += 1
        if self.errors:
            die(f"runner-token completed with {self.errors} error(s)")
        print(f"runner-token: token fetched and stored successfully")


class PushSecretCheck:
    """Surface PushSecret controller errors as workflow failures.

    After ``slave-token`` / ``runner-token`` write a new value to
    the ``gitea-slave-token`` / ``gitea-runner-token`` K8s Secret,
    the external-secrets PushSecret controller picks up the change
    (within seconds; the 5-minute ``refreshInterval`` is the
    fallback, not the only path) and pushes the new value to OpenBao
    via the ``shoggoth-openbao-write`` SecretStore. When that
    SecretStore's write token — the K8s Secret
    ``shoggoth-openbao-eso-write-token`` — is invalid (revoked,
    expired, or scoped wrong) the controller records
    ``status.conditions[].status=False`` with reason like
    ``VaultError`` and message
    ``"cannot read secret data from Vault: Error making API
    request..."``.

    The maintenance workflow that wrote the K8s Secret still
    succeeds, because the write is to the K8s Secret, not to
    OpenBao. Argo doesn't watch the PushSecret's events, so the
    failure stays invisible until a downstream consumer
    (git-cred sidecar, web-internal proxy) starts 401-ing on the
    stale OpenBao value. This class makes the failure visible:
    it queries each known PushSecret's ``status.conditions`` and
    ``die()``s the workflow when any condition is not ``Ready``.

    RBAC: the maintenance SA needs ``get`` on ``pushsecrets`` in
    the namespace — see the
    ``shoggoth-maintenance-secret-writer-pushsecrets-read`` Role
    in ``shoggoth/k3s/maintenance-secret-writer.yaml``.
    """

    # PushSecrets whose OpenBao round-trip the maintenance workflow
    # should verify. Centralised so adding a new PushSecret to
    # ``shoggoth/k3s/external-secrets/`` is a one-line change here.
    # ``redmine-slave-token`` is included even though the redmine
    # pod's ``slave_token_init.rb`` POST caller — not the
    # maintenance workflow — is what writes its K8s Secret: the
    # three PushSecrets share a SecretStore and a write token, so a
    # single root-cause failure surfaces in all three and a single
    # check covers all three.
    PUSH_SECRETS = (
        "gitea-slave-token",
        "gitea-runner-token",
        "redmine-slave-token",
    )

    # Small delay before querying so the controller has time to
    # evaluate the freshly-written K8s Secret. Default 0 — the
    # controller reacts to Secret change events within seconds
    # (the 5-minute ``refreshInterval`` is the fallback), and the
    # upstream maintenance steps (gitea-ldap-sync, slave-access,
    # slave-token, runner-token, ssh-key, redmine-ldap-sync) take
    # much longer than that, so by the time the check runs the
    # conditions are already settled. Override via
    # ``SHOGGOTH_PUSHSECRET_CHECK_WAIT`` for fast-failing tests.
    DEFAULT_WAIT_SECONDS = 0

    def __init__(self):
        self.errors = 0
        self.namespace = params.shoggoth_namespace
        self.wait_seconds = int(os.environ.get(
            "SHOGGOTH_PUSHSECRET_CHECK_WAIT",
            self.DEFAULT_WAIT_SECONDS))

    def _query(self, name):
        """Return the parsed status.conditions for one PushSecret.

        ``None`` on success (caller can read ``self.errors``), or
        an error string on failure (already counted via
        ``self.errors``). Distinguishes the three error shapes:

          1. ``kubectl`` itself failed (RC != 0): the SA is missing
             ``get pushsecrets`` or the PushSecret doesn't exist.
          2. ``kubectl`` returned unparseable JSON: a controller
             bug or a CRD version mismatch.
          3. ``status.conditions`` is empty: the controller
             hasn't evaluated this PushSecret yet (genuine race
             only — the 5m refreshInterval bounds it).
        """
        result = run(
            ["kubectl", "get", "pushsecret", name,
             "-n", self.namespace, "-o", "json"],
            check=False,
        )
        if result.returncode != 0:
            self.errors += 1
            stderr = (result.stderr or "").strip().splitlines()
            tail = stderr[-1] if stderr else ""
            return (f"kubectl get pushsecret {name} -n {self.namespace} "
                    f"failed (rc={result.returncode}): {tail[:200]}")
        try:
            doc = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            self.errors += 1
            return f"failed to parse PushSecret {name} JSON: {e}"
        status = doc.get("status") or {}
        conditions = status.get("conditions") or []
        if not conditions:
            self.errors += 1
            return ("no status.conditions yet (the controller hasn't "
                    f"evaluated pushsecret/{name}; refreshInterval may "
                    "have been raised or the controller is wedged)")
        # external-secrets uses 'Ready' as the canonical overall
        # condition and 'Synced' for the per-sync status. Accept
        # either as authoritative — 'Ready' is what the controller
        # sets False with reason='VaultError' on a write failure;
        # 'Synced' mirrors it on a per-ref basis. A non-True value
        # on either surfaces a root-cause the operator must
        # address.
        for cond in conditions:
            ctype = cond.get("type", "")
            cstatus = cond.get("status", "")
            if ctype in ("Ready", "Synced") and cstatus != "True":
                self.errors += 1
                reason = cond.get("reason", "") or ""
                message = cond.get("message", "") or ""
                return (f"{ctype}={cstatus} reason={reason!r} "
                        f"message={message!r}")
        return None

    def execute(self):
        if not self.namespace:
            die("SHOGGOTH_NAMESPACE is required for pushsecret-check")
        if self.wait_seconds > 0:
            print(f"pushsecret-check: waiting {self.wait_seconds}s for "
                  "the PushSecret controller to evaluate the freshly "
                  "written K8s Secrets...")
            time.sleep(self.wait_seconds)
        for name in self.PUSH_SECRETS:
            err = self._query(name)
            if err:
                print(f"ERROR: pushsecret/{name} in namespace "
                      f"'{self.namespace}': {err}", file=sys.stderr)
            else:
                print(f"pushsecret-check: pushsecret/{name} is Ready")
        if self.errors:
            print("ERROR: pushsecret-check found errored PushSecret(s). "
                  "The maintenance workflow wrote the K8s Secret, but the "
                  "PushSecret controller failed to propagate the new value "
                  "to OpenBao. Three common causes, in order of likelihood: "
                  "(1) the write token at shoggoth-openbao-eso-write-token "
                  "is invalid (revoked, expired, or scoped wrong) — "
                  "recovery is to delete that K8s Secret and re-run openbao "
                  "init; (2) the shoggoth-eso-write policy is missing "
                  "``secret/metadata/<path>`` access — the Vault client "
                  "needs ``read`` for the pre-push ``managed-by`` check "
                  "AND ``create``+``update`` for the post-data metadata "
                  "write that self-asserts the marker on first push; "
                  "without the post-data grant the controller records "
                  "``set secret failed: ... PUT /v1/secret/metadata/<path> "
                  "Code: 403 permission denied`` even though the data "
                  "write itself succeeded — recovery is to re-run openbao "
                  "init (the policy is idempotent and re-applied "
                  "unconditionally; the token stays valid); (3) the OpenBao "
                  "secret at the PushSecret's ``remoteRef.remoteKey`` "
                  "exists (it was created by the openbao init bootstrap) "
                  "but does NOT carry "
                  "``custom_metadata.managed-by: external-secrets`` — the "
                  "external-secrets Vault client (see "
                  "``providers/v1/vault/client_push.go``) refuses to push "
                  "to any remote secret that isn't tagged as managed by ESO, "
                  "and the bootstrap's ``bao_put_value`` writes the data "
                  "endpoint only (kv-v2 keeps the data and metadata "
                  "endpoints separate) — recovery is to ``POST`` the "
                  "missing marker once per affected path: "
                  "``curl -X POST -H 'X-Vault-Token: <root>' -H "
                  "'Content-Type: application/json' -d "
                  "'{\"custom_metadata\":{\"managed-by\":\"external-secrets\"}}' "
                  "http://openbao:80/v1/secret/metadata/<path>`` (then "
                  "force the PushSecret to re-evaluate via "
                  "``kubectl annotate pushsecret <name> "
                  "force-sync=$(date +%s) --overwrite``). See "
                  "shoggoth/k3s/openbao.yaml Phase B.5 for procedures (1) "
                  "and (2); for a fresh cluster the bootstrap needs the "
                  "``bao_put_metadata`` helper added (the script can't fix "
                  "existing kv data).",
                  file=sys.stderr)
            die(f"pushsecret-check completed with {self.errors} error(s)")
        print("pushsecret-check: all PushSecrets are Ready")


class GiteaLdapSync(GiteaCommand):
    def _user_count(self):
        users = self.gitea.list_admin_users()
        if users is None:
            return None
        return len(users)

    def _ldap_source_filter(self):
        exclusions = "".join(f"(!(uid={login}))" for login in Ldap.EXCLUDED_LOGINS)
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
                f"{list(Ldap.EXCLUDED_LOGINS)}")
            return
        log(f"gitea-ldap-sync: updating LDAP source filter to exclude "
            f"{list(Ldap.EXCLUDED_LOGINS)}")
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
            with urlopen(req, timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
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
        self.redmine = redmine
        self.ldap = Ldap(ldap_url, ldap_bind_dn, ldap_bind_password, ldap_user_base_dn)
        self.ldap_url = ldap_url
        self.ldap_bind_dn = ldap_bind_dn
        self.ldap_bind_password = ldap_bind_password
        self.ldap_user_base_dn = ldap_user_base_dn
        self.errors = 0

    def _ldap_search_filter(self):
        return self.ldap.search_filter_for("")

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
        try:
            return self.ldap.search_users()
        except Exception as e:
            print(f"WARNING: LDAP search failed: {e}", file=sys.stderr)
            return []

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


# ---------------------------------------------------------------------
# Unified dispatcher.
# ---------------------------------------------------------------------

# Script's public command-line surface. Lives at module level (rather
# than inside a class) because it's read by both ``argparse`` (in
# ``main()``) and the ``_run`` dispatch below — neither of which has
# any other shared state worth a class instance. The two sets are
# disjoint so callers can decide which init path to take (workflow
# commands need the git-cred sidecar; maintenance commands don't).
WORKFLOW_COMMANDS = {"task", "ci-failure", "pr-update"}
MAINTENANCE_COMMANDS = {
    "argo-webhooks", "redmine-webhooks", "redmine-argo-webhooks",
    "github-mirror-sync", "slave-access", "slave-token", "runner-token",
    "ssh-key",
    "slave-test",
    "gitea-ldap-sync", "redmine-ldap-sync",
    "pushsecret-check",
}


def _run(args):
    """Dispatch ``args.command`` to its handler.

    Workflow commands (``task`` / ``ci-failure`` / ``pr-update``)
    share a common init path: gate on the git-cred sidecar's
    readiness flag, load the Gitea webhook payload (except
    ``task``, which reads the Redmine task ID from the CLI instead),
    check out the repo, instantiate an ``Agent``, and dispatch to
    one of the ``*Command`` classes.

    Maintenance commands (everything else) don't share init — each
    instantiates whatever clients it needs (Gitea, OpenBao, Github,
    Redmine, ...) and dispatches to its handler directly.

    ``task_id`` is validated up front so a bad input fails before the
    git-cred wait (which can take up to ``GIT_CRED_READY_TIMEOUT``
    seconds) — preserves the original fail-fast behaviour.
    """
    if args.command == "task":
        if not args.task_id or not args.task_id.isdigit():
            sys.stderr.write(
                "shoggoth.py: error: task command requires a numeric task ID\n")
            sys.exit(2)

    if args.command in WORKFLOW_COMMANDS:
        GitCredentials().wait_until_ready()

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
        else:  # "pr-update"
            cmd = PrUpdateCommand(shoggoth, gitea, redmine, agent)

        cmd.execute()
    elif args.command == "argo-webhooks":
        gitea = Gitea()
        cmd = SetupArgoWebhooks(gitea)
        cmd.execute(args.args or None)
    elif args.command == "redmine-webhooks":
        gitea = Gitea()
        cmd = SetupRedmineWebhooks(gitea)
        cmd.execute(args.args or None)
    elif args.command == "redmine-argo-webhooks":
        k8s = K8s()
        redmine = Redmine(k8s)
        cmd = SetupRedmineArgoWebhooks(redmine)
        cmd.execute()
    elif args.command == "github-mirror-sync":
        # Resolve the org list from the env var only. ``.split()`` (no
        # arg) splits on any whitespace and discards empty strings, so
        # both ``"myOrg"`` and ``"orgA orgB orgC"`` work — the latter
        # for one-shot overrides of the configured org via
        # ``-e SHOGGOTH_GITHUB_ORG=...``.
        orgs = params.shoggoth_github_org.split() if params.shoggoth_github_org else []
        if not orgs:
            die(
                "github-mirror-sync requires SHOGGOTH_GITHUB_ORG to be "
                "set to one or more github organization names."
            )
        print(f"Using SHOGGOTH_GITHUB_ORG={orgs}")
        gitea = Gitea()
        github = Github()
        cmd = GithubMirrorSync(gitea, github)
        cmd.execute(orgs)
    elif args.command == "slave-access":
        gitea = Gitea()
        cmd = SlaveAccess(gitea)
        cmd.execute()
    elif args.command == "slave-token":
        gitea = Gitea()
        cmd = SlaveToken(gitea)
        cmd.execute()
    elif args.command == "runner-token":
        gitea = Gitea()
        cmd = RunnerToken(gitea)
        cmd.execute()
    elif args.command == "ssh-key":
        gitea = Gitea()
        cmd = SshKey(gitea)
        cmd.execute()
    elif args.command == "pushsecret-check":
        # Verifies that the external-secrets PushSecret controller
        # successfully propagated the K8s Secret value (written by
        # the upstream ``slave-token`` / ``runner-token`` steps) to
        # OpenBao via the ``shoggoth-openbao-write`` SecretStore.
        # Surfaces "cannot read secret data from Vault" and similar
        # write-token failures as a workflow ``die()`` so the cron
        # is marked failed instead of silently leaving a stale kv
        # value that downstream consumers (git-cred sidecar,
        # web-internal proxy) only discover via 401s.
        PushSecretCheck().execute()
    elif args.command == "slave-test":
        cmd = SlaveTest()
        # Slave-test dispatchers pass the check name as the single
        # positional after `slave-test`. Argparse consumes it as
        # ``task_id`` (the next positional slot), so read it from there
        # rather than ``args.args`` — which is reserved for trailing
        # subcommands like the per-check names a future invocation
        # could pass.
        if args.task_id:
            cmd.execute([args.task_id])
        else:
            cmd.execute(None)
    elif args.command == "gitea-ldap-sync":
        gitea = Gitea()
        cmd = GiteaLdapSync(gitea)
        cmd.execute()
    elif args.command == "redmine-ldap-sync":
        k8s = K8s()
        redmine = Redmine(k8s)
        # Read the ``ldapauth`` user's bind password from K8s — same
        # RBAC story as the slave/admin passwords.
        ldap_bind_password = secrets_store.read("openldap/ldapauth-password",
                                                key="password", k8s=k8s)
        if not ldap_bind_password:
            die("openldap-ldapauth-password is required for LDAP bind "
                "(grant the script's SA `get` on the "
                "openldap-ldapauth-password K8s Secret)")
        if not params.shoggoth_domain:
            die("SHOGGOTH_DOMAIN is required")
        ldap_base_dn = "dc=" + params.shoggoth_domain.replace(".", ",dc=")
        ldap_url = params.ldap_url
        ldap_bind_dn = f"uid=ldapauth,ou=people,{ldap_base_dn}"
        ldap_user_base_dn = f"ou=people,{ldap_base_dn}"
        cmd = RedmineLdapSync(redmine, ldap_url, ldap_bind_dn, ldap_bind_password, ldap_user_base_dn)
        cmd.execute()


def main():
    parser = argparse.ArgumentParser(
        prog="shoggoth.py",
        description="Shoggoth workflow + maintenance: dispatch webhook-driven "
                    "workflows (task/ci-failure/pr-update) and cron-style "
                    "maintenance (webhooks, mirror sync, slave user, tokens, "
                    "ssh key, slave-test, ldap-sync) from one binary.",
    )
    parser.add_argument("command",
                        choices=sorted(WORKFLOW_COMMANDS | MAINTENANCE_COMMANDS),
                        help="Command to execute")
    parser.add_argument("task_id", nargs="?", default=None,
                        help="Redmine task ID (required for 'task' command)")
    parser.add_argument("args", nargs="*", default=[],
                        help="Command arguments (project names for webhooks, "
                             "github orgs for mirror-sync, check names for slave-test)")

    parsed = parser.parse_args()

    _run(parsed)


if __name__ == "__main__":
    main()
