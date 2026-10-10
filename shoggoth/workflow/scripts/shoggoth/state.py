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
WORKFLOW_COMMANDS = {"task", "ci-failure", "pr-update"}
MAINTENANCE_COMMANDS = {
    "argo-webhooks", "redmine-webhooks", "redmine-argo-webhooks",
    "github-mirror-sync", "slave-access", "slave-token", "runner-token",
    "ssh-key",
    "slave-test",
    "gitea-ldap-sync", "redmine-ldap-sync",
    "pushsecret-check",
}
