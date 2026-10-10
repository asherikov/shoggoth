"""Module split out of shoggoth.py; see package __init__."""
import sys

from ..util import die
from .base import GiteaCommand
from ..state import params

class SlaveAccess(GiteaCommand):
    # Branches the slave is NEVER allowed to push (regular or force)
    # to. These are the production branches — protecting them against
    # force-push is the whole point of branch protection, and the
    # maintenance script must not silently undo that protection. All
    # other protected branches are patched: ``enable_force_push`` is
    # flipped to true and the slave is appended to ``whitelist_users``
    # whenever ``enable_push`` is true.
    ALWAYS_EXCLUDE = frozenset({"main", "master"})

    # Team created in every org to grant the slave ``write`` access to
    # every existing and future repo in the org. The team is created
    # with ``includes_all_repositories=True`` and ``permission=write``,
    # so the script does not have to enumerate repos to add the team
    # to each one — the team auto-grants access at org scope. The
    # direct write-collaborator grant set up by
    # ``_add_to_mirrored_repos`` is layered on top, partly as
    # belt-and-braces (the team membership also requires the user to
    # actually be in the org, which ``AddTeamMember`` does NOT refresh
    # against the ``Access`` table) and partly to make the slave's
    # access show up correctly in the per-repo collaborators view.
    PUSH_TEAM_NAME = "shoggoth-slave-push"
    PUSH_TEAM_PERMISSION = "write"
    PUSH_TEAM_UNITS = ("repo.code", "repo.issues",
                        "repo.pulls", "repo.releases")

    def __init__(self, gitea):
        super().__init__(gitea)
        self.slave_user = params.shoggoth_slave_user
        self.automation_users = [u for u in (self.slave_user,) if u]

    def execute(self):
        orgs = self.gitea.list_orgs()
        for user in self.automation_users:
            self._ensure_push_team(orgs, user)
            self._add_to_orgs(orgs, user)
            self._add_to_mirrored_repos(orgs, user)
            self._ensure_force_push_access(orgs, user)
        if self.errors:
            die(f"slave-access completed with {self.errors} error(s)")

    def _ensure_push_team(self, orgs, user):
        """Create ``PUSH_TEAM_NAME`` in each org and add ``user`` to
        it. Idempotent: existing teams are reused, existing
        memberships are no-ops.

        Why this exists: the slave was previously a member of the
        org's ``Owners`` team, which Gitea grants implicit
        ``AccessModeOwner`` on every repo in the org. The user
        was removed from ``Owners`` (clean-up of the prior
        impersonation-by-Owners setup) and is now a direct
        write-collaborator on each repo. The receive-pack
        permission check (``routers/private/hook_pre_receive.go``
        → ``Permission.CanWrite(unit.TypeCode)``) reads the
        ``Access`` table cache, and that table was left stale
        after the team removal (``RecalculateUserAccess`` runs
        on team add / direct-collaborator add / update, but
        ``AddTeamMember`` is NOT one of those triggers — Gitea
        1.27.3's ``services/org/team.go::AddTeamMember`` only
        inserts a ``TeamUser`` row and does not touch the
        ``Access`` table for the team's repos). A dedicated
        push team gives the slave a persistent team-based
        access path that survives the direct-collaborator being
        removed by a future operator action.

        Gitea's behaviour for the create path matters here:
        ``NewTeam`` with ``includes_all_repositories=True`` walks
        every org repo and calls ``addRepositoryToTeam`` per repo
        (which in turn calls ``RecalculateTeamAccesses``), so
        the ``Access`` table is populated for every existing
        repo at create-time — but only for users who are
        ALREADY team members at that point. The slave is added
        to the team right after, so the recalc sees an empty
        team. The script therefore does not rely on the
        create-time recalc; the direct-collaborator path in
        ``_add_to_mirrored_repos`` is what populates the
        ``Access`` table for the slave on each existing repo,
        and this team's role is the durable access path for
        future runs.
        """
        if not orgs:
            print("No organizations found (push-team pass)")
            return

        for org in orgs:
            org_name = org.get("username")
            if not org_name:
                continue
            team = self.gitea.find_team_by_name(
                org_name, self.PUSH_TEAM_NAME)
            if team is None:
                print(f"Creating push team '{self.PUSH_TEAM_NAME}' "
                      f"in org '{org_name}' "
                      f"(permission={self.PUSH_TEAM_PERMISSION}, "
                      f"includes_all_repositories=true)")
                created = self.gitea.create_team(
                    org_name, self.PUSH_TEAM_NAME,
                    permission=self.PUSH_TEAM_PERMISSION,
                    includes_all_repositories=True,
                    units=list(self.PUSH_TEAM_UNITS))
                if created is None:
                    # 422 here means a concurrent run (or a manual
                    # create) got there first — re-fetch.
                    team = self.gitea.find_team_by_name(
                        org_name, self.PUSH_TEAM_NAME)
                    if team is None:
                        print(f"ERROR: failed to create or find push "
                              f"team '{self.PUSH_TEAM_NAME}' in org "
                              f"'{org_name}'", file=sys.stderr)
                        self.errors += 1
                        continue
                else:
                    team = created
            else:
                # If the team exists but is missing
                # ``includes_all_repositories`` (e.g. it was created
                # by an earlier version of this script before that
                # flag was added), patch the team to set it. The
                # patch does NOT re-run ``addRepositoryToTeam`` for
                # the existing repos (see ``UpdateTeam`` in
                # ``services/org/team.go``), so this is a no-op for
                # the ``Access`` table — the
                # ``_add_to_mirrored_repos`` direct-collaborator
                # pass is what populates that.
                if not team.get("includes_all_repositories"):
                    team_id = team.get("id")
                    if team_id is not None:
                        print(f"Upgrading push team "
                              f"'{self.PUSH_TEAM_NAME}' in org "
                              f"'{org_name}' to "
                              f"includes_all_repositories=true")
                        if self.gitea.update_team(
                                team_id,
                                includes_all_repositories=True) is None:
                            print(f"ERROR: failed to upgrade push "
                                  f"team '{self.PUSH_TEAM_NAME}' "
                                  f"in org '{org_name}'",
                                  file=sys.stderr)
                            self.errors += 1
                            continue

            team_id = team.get("id") if isinstance(team, dict) else None
            if team_id is None:
                print(f"ERROR: push team '{self.PUSH_TEAM_NAME}' in "
                      f"org '{org_name}' has no id", file=sys.stderr)
                self.errors += 1
                continue
            print(f"Adding '{user}' to push team "
                  f"'{self.PUSH_TEAM_NAME}' (id={team_id}) in org "
                  f"'{org_name}'")
            if not self.gitea.put(
                    f"teams/{team_id}/members/{user}"):
                print(f"ERROR: failed to add '{user}' to push team "
                      f"'{self.PUSH_TEAM_NAME}' in org '{org_name}'",
                      file=sys.stderr)
                self.errors += 1

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
            repos, repos_err = self.gitea.get_list(f"orgs/{org_name}/repos")
            if repos_err is not None:
                print(f"ERROR: could not list repos for org "
                      f"'{org_name}': {repos_err}",
                      file=sys.stderr)
                self.errors += 1
                continue
            if not repos:
                continue

            for repo in repos:
                repo_name = repo.get("name")
                full_name = repo.get("full_name")
                if not full_name:
                    continue

                # Check the *effective* permission (max of direct
                # collaborator + team-based via org membership). The
                # previous logic only checked whether the user was a
                # collaborator at all, which meant a user added with
                # ``read`` by a team — or with ``read`` from a prior
                # run of an older maintenance script — was never
                # escalated to ``write``. ``/squash`` and ``/rebase``
                # force-push and need at least ``write``; ``read`` is
                # the symptom the user reported as "User permission
                # denied for writing" on the force-push.
                role = self.gitea.get_repo_collaborator_permission(
                    full_name, user)
                if role in ("write", "admin", "owner"):
                    # Force-refresh the ``Access`` table cache. The
                    # ``get_repo_collaborator_permission`` result is
                    # computed live (reads ``Collaboration`` +
                    # ``TeamUser`` + ``Access`` per request), but
                    # the receive-pack permission check
                    # (``routers/private/hook_pre_receive.go`` →
                    # ``Permission.CanWrite(unit.TypeCode)``)
                    # reads the ``Access`` table cache directly
                    # and short-circuits on stale state. If the
                    # user was previously in the org's ``Owners``
                    # team (or any team whose removal cleared the
                    # ``Access`` row via
                    # ``RecalculateUserAccess`` → ``accessMode <
                    # minMode`` → DELETE without re-INSERT) and is
                    # now only a direct write-collaborator, the
                    # ``Access`` table for the user on the repo can
                    # be empty even though the direct collaborator
                    # row is present. ``AddOrUpdateCollaborator``
                    # early-returns when the requested mode equals
                    # the current ``Collaboration.Mode`` and does
                    # NOT call ``RecalculateUserAccess``, so a
                    # no-op PUT leaves the stale ``Access`` table
                    # in place. Forcing the mode through ``read``
                    # → ``write`` always changes the mode (the
                    # first PUT goes from ``write`` to ``read``;
                    # the second from ``read`` to ``write``), so
                    # both PUTs reach ``RecalculateUserAccess`` and
                    # the ``Access`` row is rewritten with the
                    # correct mode. The intermediate ``read``
                    # state is brief (a single network round-trip
                    # apart from the second PUT) and the user has
                    # only direct access to this repo — no other
                    # workflow path pushes during the maintenance
                    # step — so the brief downgrade is not
                    # observable. Cheaper than a ``delete + add``
                    # dance (``RecalculateAccesses`` recomputes for
                    # every user on the repo) and avoids a window
                    # where the user has no access at all.
                    print(f"  '{user}' already has {role!r} on "
                          f"'{full_name}'; force-refreshing "
                          f"the Access table cache via read+write")
                    if not self.gitea.add_repo_collaborator(
                            full_name, user, "read"):
                        print(f"ERROR: failed force-refresh step 1 "
                              f"(downgrade to read) for '{user}' on "
                              f"'{full_name}'", file=sys.stderr)
                        self.errors += 1
                        continue
                    if not self.gitea.add_repo_collaborator(
                            full_name, user, "write"):
                        print(f"ERROR: failed force-refresh step 2 "
                              f"(upgrade to write) for '{user}' on "
                              f"'{full_name}'", file=sys.stderr)
                        self.errors += 1
                        continue
                    print(f"  '{user}' Access table refreshed to "
                          f"'write' on '{full_name}'")
                    continue
                if role is None:
                    # ``None`` covers both "not a collaborator" (404)
                    # and "lookup failed" — the PUT endpoint is
                    # idempotent and accepts an existing
                    # collaborator, so retrying with a write payload
                    # upgrades the permission on the success path
                    # and surfaces the underlying transport error on
                    # the failure path. Treating the two cases the
                    # same here is the right call: we'd want to
                    # write the permission either way.
                    action = "Adding"
                else:
                    # ``role`` is one of ``read`` (or a future
                    # Gitea value) — escalate.
                    action = f"Upgrading '{user}' on '{full_name}' " \
                             f"from {role!r} to 'write'"
                if not self.gitea.add_repo_collaborator(
                        full_name, user, "write"):
                    print(f"ERROR: failed to set write permission for "
                          f"'{user}' on '{full_name}'",
                          file=sys.stderr)
                    self.errors += 1
                    continue
                if action == "Adding":
                    print(f"{action} '{user}' as write collaborator "
                          f"to repo '{full_name}'")
                else:
                    print(action)

    def _ensure_force_push_access(self, orgs, user):
        """Patch branch protection on every protected branch in every
        mirrored repo so the slave can push (regular and force).

        ``_add_to_mirrored_repos`` covers the *collaborator* half of
        the permission story — it guarantees ``role >= write`` on
        each repo. The other half is *branch protection*: Gitea's
        receive-pack returns ``User permission denied for writing``
        when one of:

        * ``enable_push`` is true and the user is not in
          ``whitelist_users`` / ``whitelist_teams`` (push is
          restricted to the allow-list) — even a ``write``
          collaborator is rejected, and this applies to REGULAR
          pushes (the ``pr-comment`` flow's `git push` of a
          per-comment commit) as well as force-pushes;
        * ``enable_force_push`` is false and the push is a
          non-fast-forward (force-push is denied to everyone,
          regardless of write).

        This method walks every branch-protection rule on every
        mirrored repo and applies the minimum patch:

        * Append ``user`` to ``whitelist_users`` whenever
          ``enable_push`` is true and the user is not already
          listed (regular push).
        * Set ``enable_force_push=true`` whenever it is not
          already (force-push).

        The only branches excluded from this are ``main`` and
        ``master`` (``ALWAYS_EXCLUDE``) — branch protection on the
        production branches is the whole point of the protection
        feature, and silently undoing it from the maintenance
        script would defeat that. Anything else, including
        previously out-of-scope patterns like
        ``wshandler/release-policies``, is patched.

        The list is computed by the caller and passed in full to
        ``update_branch_protection`` — Gitea's PATCH treats
        ``whitelist_users`` as a complete set, not a diff.

        A read failure on a single rule records an error and
        continues; the next maintenance run will retry it. A read
        failure on the whole repo's rule list is logged and
        skipped — the read failures are visible in the workflow
        log and recoverable by re-running.
        """
        if not orgs:
            print("No organizations found (branch protection pass)")
            return

        for org in orgs:
            org_name = org.get("username")
            if not org_name:
                continue
            repos, repos_err = self.gitea.get_list(f"orgs/{org_name}/repos")
            if repos_err is not None:
                print(f"ERROR: could not list repos for org "
                      f"'{org_name}': {repos_err}",
                      file=sys.stderr)
                self.errors += 1
                continue
            if not repos:
                continue

            for repo in repos:
                full_name = repo.get("full_name")
                if not full_name:
                    continue

                protections, protections_err = self.gitea.get_list(
                    f"repos/{full_name}/branch_protections")
                if protections_err is not None:
                    print(f"ERROR: could not list branch protections "
                          f"on '{full_name}': {protections_err} (skipping)",
                          file=sys.stderr)
                    self.errors += 1
                    continue
                if not protections:
                    continue

                for prot in protections:
                    branch = (prot.get("branch_name")
                              or prot.get("name"))
                    if not branch:
                        print(f"ERROR: branch protection on "
                              f"'{full_name}' has no branch_name; "
                              f"skipping",
                              file=sys.stderr)
                        self.errors += 1
                        continue

                    if branch in self.ALWAYS_EXCLUDE:
                        print(f"  Skipping protected branch "
                              f"'{full_name}:{branch}' "
                              f"(production branch, ALWAYS_EXCLUDE)")
                        continue

                    enable_push = bool(prot.get("enable_push"))
                    enable_force_push = bool(
                        prot.get("enable_force_push"))
                    whitelist = list(prot.get("whitelist_users") or [])

                    patch = {}
                    if enable_push and user not in whitelist:
                        patch["whitelist_users"] = whitelist + [user]
                    if not enable_force_push:
                        patch["enable_force_push"] = True
                    if not patch:
                        print(f"  '{user}' already has push access to "
                              f"protected branch '{full_name}:{branch}'")
                        continue

                    desc = []
                    if "whitelist_users" in patch:
                        desc.append(f"whitelist_users +'{user}'")
                    if "enable_force_push" in patch:
                        desc.append("enable_force_push: false -> true")
                    print(f"Patching branch protection on "
                          f"'{full_name}:{branch}' "
                          f"({'; '.join(desc)})")
                    updated = self.gitea.update_branch_protection(
                        full_name, branch, **patch)
                    if updated is None:
                        print(f"ERROR: failed to update branch "
                              f"protection on '{full_name}:{branch}'",
                              file=sys.stderr)
                        self.errors += 1
