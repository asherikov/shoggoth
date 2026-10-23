"""Module split out of shoggoth.py; see package __init__."""
import base64
import binascii
import json
import sys

from .util import die, run
from .state import params, secrets_store

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
        names it writes).
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
