"""Module-level singletons shared by every consumer in the package.

``params`` and ``secrets_store`` are constructed once here; every
consumer imports them (``from .state import params, secrets_store``)
instead of constructing its own copy. The ``WORKFLOW_COMMANDS`` /
``MAINTENANCE_COMMANDS`` sets live here too — they're read by both
``argparse`` (``main()``) and the dispatcher (``_run()``), and the
command names are part of the script's public contract.
"""


from .config import Parameters, SecretStore

# Eagerly construct the singletons: downstream modules import these
# names and expect a populated instance (not a lazy factory). A
# missing required env var (currently only ``SHOGGOTH_DOMAIN``)
# raises via ``die()`` at import time.
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
#     ``shoggoth/workflow/dispatch.py`` CLI utility, not via
#     ``shoggoth.py``. The dispatcher has a different dependency
#     surface (no agent, no git-cred sidecar).
#   * ``sandbox`` is the placeholder for the /sandbox slash
#     command; it is handled by ``DispatchCommand._handle_sandbox``
#     (in ``shoggoth/workflow/shoggoth/commands/dispatch.py``),
#     which emits ``should_run_worker=false``. It is never invoked
#     directly on the worker, so it doesn't belong in
#     WORKFLOW_COMMANDS.
WORKFLOW_COMMANDS = {"task", "ci-failure", "pr-update"}
MAINTENANCE_COMMANDS = {
    "argo-webhooks", "redmine-webhooks", "redmine-argo-webhooks",
    "github-mirror-sync", "slave-access", "slave-token", "runner-token",
    "ssh-key",
    "slave-test",
    "gitea-ldap-sync", "redmine-ldap-sync",
    "pushsecret-check",
    "container-pull",
}
