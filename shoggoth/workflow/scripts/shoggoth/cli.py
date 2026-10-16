"""Module split out of shoggoth.py; see package __init__."""
import argparse
import sys

from .agent import Agent
from .commands.argo_webhooks import (
    SetupArgoWebhooks,
    SetupRedmineArgoWebhooks,
    SetupRedmineWebhooks,
)
from .commands.ci_failure import CiFailureCommand
from .commands.gitea_ldap_sync import GiteaLdapSync
from .commands.mirror_sync import GithubMirrorSync
from .commands.pr_update import PrUpdateCommand
from .commands.push_secret_check import PushSecretCheck
from .commands.redmine_ldap_sync import RedmineLdapSync
from .commands.runner_token import RunnerToken
from .commands.slave_access import SlaveAccess
from .commands.slave_test import SlaveTest
from .commands.slave_token import SlaveToken
from .commands.ssh_key import SshKey
from .commands.task import TaskCommand
from .git import GitCredentials
from .gitea import Gitea
from .github import Github
from .k8s import K8s
from .redmine import Redmine
from .runner import Shoggoth
from .state import (
    MAINTENANCE_COMMANDS,
    WORKFLOW_COMMANDS,
    params,
    secrets_store,
)
from .util import die


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
        # Workflow commands all share the same init path:
        # gate on the git-cred sidecar's readiness flag,
        # load the Gitea webhook payload (except ``task``,
        # which reads the Redmine task ID from the CLI
        # instead), check out the repo, instantiate an
        # ``Agent``, and dispatch to one of the
        # ``*Command`` classes.
        #
        # ``dispatch`` is NOT a shoggoth.py subcommand —
        # the dispatcher is its own CLI utility
        # (``shoggoth/workflow/scripts/dispatch.py``) and is
        # reached via the ``dispatch`` step of the
        # ``coding-agent-dispatcher`` DAG (see
        # ``shoggoth/workflow/argo/templates/coding-agent-dispatcher.yaml``).
        # ``sandbox`` is the placeholder for the future
        # /sandbox slash command; it's handled by the
        # dispatcher's ``_handle_sandbox`` (in
        # ``shoggoth/workflow/scripts/shoggoth/commands/dispatch.py``)
        # and never reaches the worker step. Neither belongs in
        # ``WORKFLOW_COMMANDS`` — see ``state.py`` for the
        # rationale.
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
        elif args.command == "pr-update":
            cmd = PrUpdateCommand(shoggoth, gitea, redmine, agent)
        else:
            # Defensive: WORKFLOW_COMMANDS is exactly
            # {task, ci-failure, pr-update} per ``state.py``,
            # so the membership check above already filtered
            # out anything else. A future addition to
            # WORKFLOW_COMMANDS without a matching branch
            # here would land in this ``else``; fail closed
            # with a clear message rather than silently
            # routing to ``Agent().run()``.
            die(f"shoggoth.py: worker step reached with command "
                f"{args.command!r}; the worker step only handles "
                f"'task', 'ci-failure', 'pr-update' (see "
                f"WORKFLOW_COMMANDS in state.py)")

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
        cmd = RedmineLdapSync(
            redmine, ldap_url, ldap_bind_dn, ldap_bind_password,
            ldap_user_base_dn)
        cmd.execute()
    else:
        die(f"shoggoth.py: no handler for command {args.command!r} — "
            "cli.py:_run is out of sync with state.py")


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
                        help="Redmine task ID (required for `task`); "
                             "slave-test check name")
    parser.add_argument("args", nargs="*", default=[],
                        help="Project names for `argo-webhooks` / "
                             "`redmine-webhooks`")

    parsed = parser.parse_args()

    _run(parsed)
