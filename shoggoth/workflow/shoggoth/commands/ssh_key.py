"""Module split out of shoggoth.py; see package __init__."""
import sys

from ..k8s import K8s
from ..util import die
from .base import GiteaCommand
from ..state import params, secrets_store

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
