"""Maintenance and workflow command implementations.

The original ``shoggoth.py`` defined every command class at module
top level alongside the dispatcher. The split moves each command to
its own file (so e.g. ``SlaveAccess``'s 400-line ``execute`` body
sits alone instead of crowding ``SlaveTest``); this ``__init__``
re-exports the same names ``shoggoth.py`` had so the dispatcher
can still import them by class name.

Adding a new command:
  1. Create ``commands/<name>.py`` with the class.
  2. Add the import here.
  3. Add the command to ``MAINTENANCE_COMMANDS`` (or
     ``WORKFLOW_COMMANDS``) in ``state.py``.
  4. Add the dispatch branch in ``cli.py:_run``.
"""


from .argo_webhooks import (
    SetupArgoWebhooks,
    SetupRedmineArgoWebhooks,
    SetupRedmineWebhooks,
)
from .base import CommandBase, GiteaCommand
from .ci_failure import CiFailureCommand
from .dispatch import DispatchCommand
from .gitea_ldap_sync import GiteaLdapSync
from .mirror_sync import GithubMirrorSync
from .pr_update import PrUpdateCommand
from .push_secret_check import PushSecretCheck
from .redmine_ldap_sync import RedmineLdapSync
from .runner_token import RunnerToken
from .slave_access import SlaveAccess
from .slave_test import SlaveTest
from .slave_token import SlaveToken
from .ssh_key import SshKey
from .task import TaskCommand
