"""Module split out of shoggoth.py; see package __init__."""
import sys
from datetime import datetime, timezone

from ..http import HttpApiClient
from ..k8s import K8s
from ..state import params
from ..util import die
from .base import GiteaCommand

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
