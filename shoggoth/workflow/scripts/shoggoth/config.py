"""Module split out of shoggoth.py; see package __init__."""
import os

from .util import die

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
            ("shoggoth_trusted_users", "SHOGGOTH_TRUSTED_USERS", False, "admin"),
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
        # ``K8s`` is imported lazily to break the
        # ``state -> config -> k8s -> state`` cycle: ``state``
        # imports ``Parameters`` and ``SecretStore`` from here,
        # and a top-level ``from .k8s import K8s`` would close
        # the loop. The original monolithic ``shoggoth.py`` had
        # no module boundary, so the cycle didn't exist; the
        # split needs the indirection.
        from .k8s import K8s as _K8s
        k8s = k8s or _K8s()
        return k8s.read_secret(kv_path, key=key)
