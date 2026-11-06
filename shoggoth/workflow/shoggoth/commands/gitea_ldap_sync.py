"""Module split out of shoggoth.py; see package __init__."""
import sys
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from ..http import HttpApiClient
from ..util import die, log
from .base import GiteaCommand

class GiteaLdapSync(GiteaCommand):
    def _user_count(self):
        users = self.gitea.list_admin_users()
        if users is None:
            return None
        return len(users)

    def execute(self):
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
