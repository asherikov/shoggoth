"""Module-level singletons shared by every consumer in the package.

The original ``shoggoth.py`` constructed ``params = Parameters()`` and
``secrets_store = SecretStore()`` at module top level and every
downstream module reached for them by bare name. The split preserves
that idiom: each new module does ``from .state import params,
secrets_store`` instead of constructing its own copy. The
``WORKFLOW_COMMANDS`` / ``MAINTENANCE_COMMANDS`` sets move here too —
they're read by both ``argparse`` (``main()``) and the dispatcher
(``_run()``), and the command names are part of the script's public
contract.
"""


from .config import Parameters, SecretStore

# Eagerly construct the singletons. The original script did the same;
# making this explicit here documents that downstream modules are
# allowed to import these names and expect a populated instance
# (not a lazy factory). A missing required env var (currently only
# ``SHOGGOTH_DOMAIN``) raises via ``die()`` at import time — same
# fail-fast behavior as before the split.
params = Parameters()
secrets_store = SecretStore()

# Script's public command-line surface. Lives at module level (rather
# than inside a class) because it's read by both ``argparse`` (in
# ``main()``) and the ``_run`` dispatch below — neither of which has
# any other shared state worth a class instance. The two sets are
# disjoint so callers can decide which init path to take (workflow
# commands need the git-cred sidecar; maintenance commands don't).
#
# ``dispatch`` and ``sandbox`` are NOT in WORKFLOW_COMMANDS:
#   * ``dispatch`` is reached via the standalone
#     ``shoggoth/workflow/scripts/dispatch.py`` CLI utility (the
#     ``dispatch`` step of the ``coding-agent-dispatcher`` DAG),
#     not via ``shoggoth.py``. The dispatcher has a different
#     dependency surface (no agent, no git-cred sidecar) and
#     runs in a separate ``shoggoth_tools:alpine`` pod.
#   * ``sandbox`` is the placeholder for the future /sandbox
#     slash command; the dispatcher handles it via
#     ``DispatchCommand._handle_sandbox`` (in
#     ``shoggoth/workflow/scripts/shoggoth/commands/dispatch.py``)
#     and emits ``should_run_worker=false`` so the worker step
#     is skipped. It is never invoked directly on the worker, so
#     it doesn't belong in WORKFLOW_COMMANDS.
WORKFLOW_COMMANDS = {"task", "ci-failure", "pr-update"}
MAINTENANCE_COMMANDS = {
    "argo-webhooks", "redmine-webhooks", "redmine-argo-webhooks",
    "github-mirror-sync", "slave-access", "slave-token", "runner-token",
    "ssh-key",
    "slave-test",
    "gitea-ldap-sync", "redmine-ldap-sync",
    "pushsecret-check",
}
