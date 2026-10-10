"""Module split out of shoggoth.py; see package __init__."""
import re
import secrets
import sys

from ..k8s import Ldap
from ..util import die, log

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
        """Enumerate the LDAP ``inetOrgPerson`` users to mirror into Redmine.

        Lets any exception propagate to ``execute()``'s outer
        ``try/except`` block (which calls ``die(...)``) so the step
        fails fast on LDAP outage / bind failure / malformed
        response rather than silently returning an empty user list.
        A legitimately empty LDAP still returns ``[]`` cleanly with
        no exception.
        """
        return self.ldap.search_users()

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
