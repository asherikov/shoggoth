"""Module split out of shoggoth.py; see package __init__."""
import json
import os
import sys
import time

from ..util import die, run
from ..state import params

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
        try:
            self.wait_seconds = int(os.environ.get(
                "SHOGGOTH_PUSHSECRET_CHECK_WAIT",
                self.DEFAULT_WAIT_SECONDS))
        except ValueError:
            self.wait_seconds = self.DEFAULT_WAIT_SECONDS

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
