"""Shoggoth workflow + maintenance package.

Originally a single ``shoggoth.py`` (~7.5k lines, 28 classes) that
served both the webhook-driven workflow pod (task / ci-failure /
pr-update) and the cron-style maintenance pod (webhook setup, mirror
sync, slave user lifecycle, ldap sync). Split into this package so
each subsystem can be read in isolation; the ``shoggoth.py`` shim
at the parent directory re-exports ``main()`` and is the entrypoint
every workflow manifest invokes.

Public surface (re-exported for backward compat):
  * ``Gitea`` / ``Redmine`` / ``Github`` — HTTP clients.
  * ``Agent`` / ``Shoggoth`` — workflow engine.
  * Every ``*Command`` / ``*Sync`` class — the dispatch targets.
  * ``params`` / ``secrets_store`` — module-level singletons.
  * ``WORKFLOW_COMMANDS`` / ``MAINTENANCE_COMMANDS`` — argparse choices.
  * ``main`` / ``_run`` — CLI entry.
"""
import argparse
import base64
import binascii
import io
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from urllib.parse import urlencode, urlsplit, urlparse
from urllib.request import Request, urlopen, HTTPCookieProcessor, build_opener
from urllib.error import URLError, HTTPError

# Re-exports: the original shoggoth.py exported every top-level name
# into the script's namespace; callers (and the workflow manifests
# that use the shim) don't care which submodule the name came from.
# Keep the list explicit so adding a new public class is a conscious
# decision rather than an accidental import.
from .state import (
    MAINTENANCE_COMMANDS,
    WORKFLOW_COMMANDS,
    params,
    secrets_store,
)
from .agent import Agent
from .cli import _run, main
from .commands import (
    CiFailureCommand,
    CommandBase,
    GithubMirrorSync,
    GiteaCommand,
    GiteaLdapSync,
    PrUpdateCommand,
    PushSecretCheck,
    RedmineLdapSync,
    RunnerToken,
    SetupArgoWebhooks,
    SetupRedmineArgoWebhooks,
    SetupRedmineWebhooks,
    SlaveAccess,
    SlaveTest,
    SlaveToken,
    SshKey,
    TaskCommand,
)
from .config import Parameters, SecretStore
from .git import Git, GitCredentials, WsHandler
from .github import Github
from .gitea import Gitea
from .http import HttpApiClient
from .k8s import K8s, Ldap
from .otlp import OtlpLogger
from .redmine import Redmine
from .runner import Shoggoth
from .util import die, log, normalize_for_branch, run
