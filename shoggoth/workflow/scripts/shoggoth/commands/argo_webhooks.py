"""Module split out of shoggoth.py; see package __init__."""
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

from ..util import die
from .base import GiteaCommand
from ..state import params

class SetupArgoWebhooks(GiteaCommand):
    """Configure Gitea org webhooks to deliver events to Argo Workflows.

    Replaces the Kestra-based equivalent that pointed at
    /api/v1/main/executions/webhook/. Argo's events endpoint is
    /api/v1/events/{namespace}/{discriminator} and accepts an arbitrary
    JSON body (no path-suffix key). The discriminator must match the
    WorkflowEventBinding selector (gitea-pr-update / gitea-ci-failure).
    """

    def __init__(self, gitea):
        super().__init__(gitea)
        # Webhook URLs always go through the web-internal gateway at
        # api.{domain}/argo/ — the gateway injects the
        # argo-webhook-receiver SA token before proxying to argo:80,
        # so the v4 webhook interceptor (which has no gitea parser)
        # is short-circuited by the Authorization header and
        # argo-server's gatekeeper does bearer validation against k8s.
        # See shoggoth/k3s/web-internal.yaml for the gateway contract.
        self.namespace = params.shoggoth_namespace

    def execute(self, projects=None):
        projects = self._resolve_orgs(projects)
        if not projects:
            print("No Gitea projects found")
            return

        webhooks = [
            # gitea-ci-failure is intentionally NOT registered: the
            # deleted Kestra flow `main_shoggoth_gitea-ci-failure.yml`
            # carried `disabled: true`, and the WorkflowEventBinding
            # kind has no equivalent kill switch (active by default
            # once registered). To re-enable CI-failure dispatch in
            # the future, add the WorkflowEventBinding under
            # shoggoth/workflow/argo/bindings/ and re-introduce the
            # entry below — with the maintainer understanding that
            # every workflow_run: failure event will spawn a
            # coding-agent-dispatcher pod.
            # Webhook URL points at api.<DOMAIN>/argo/, not argo.<DOMAIN>:
            # web-internal (shoggoth/k3s/web-internal.yaml) injects the
            # argo-webhook-receiver SA token before proxying to argo:80,
            # so the v4 webhook interceptor (which has no gitea parser and
            # would 403 raw gitea payloads — webhookParsers at
            # server/auth/webhook/interceptor.go:25-30) is short-circuited
            # by the Authorization header and argo-server's gatekeeper does
            # bearer validation against k8s instead.
            (f"http://api.{self.gitea.domain}/argo/api/v1/events/{self.namespace}/gitea-pr-update",
             ["pull_request_review", "pull_request_review_request", "pull_request_comment"]),
        ]

        for project in projects:
            existing = self.gitea.list_org_hooks(project)
            if existing:
                for hook in existing:
                    hook_id = hook.get("id")
                    if hook_id is not None:
                        print(f"Removing webhook from {project} (id={hook_id})")
                        if not self.gitea.delete_org_hook(project, hook_id):
                            self.errors += 1

            for url, events in webhooks:
                print(f"Adding webhook to {project}: {url}")
                if not self.gitea.create_org_hook(project, url, events):
                    self.errors += 1

        if self.errors:
            die(f"argo-webhooks completed with {self.errors} error(s)")
class SetupRedmineWebhooks(GiteaCommand):
    def __init__(self, gitea):
        super().__init__(gitea)
        redmine_server = params.redmine_server
        if not redmine_server:
            domain = self.gitea.domain
            if not domain:
                die("REDMINE_SERVER or SHOGGOTH_DOMAIN is required")
            redmine_server = f"http://redmine.{domain}"
        self.redmine_host = redmine_server.replace("https://", "").replace("http://", "").rstrip("/")
        # Webhook signing is intentionally disabled: the
        # ``redmine_forgejo_webhook`` plugin reads its
        # ``Setting.plugin_redmine_forgejo_webhook['secret_token']``
        # from the Redmine plugin settings (DB), not an env var or
        # K8s Secret. There is no shared-secret source for the script
        # to mirror here, so the Gitea org hook is created with
        # ``secret=None`` and Redmine accepts unsigned webhooks. If
        # shared-secret signing is needed in the future, add a K8s
        # Secret and ``SecretStore.K8S_PATHS`` entry, then read it
        # here via ``K8s.read_secret``.
        self.webhook_secret = None
        self.webhook_url = f"http://{self.redmine_host}/forgejo/webhook"

    def execute(self, projects=None):
        projects = self._resolve_orgs(projects)
        if not projects:
            print("No Gitea projects found")
            return

        for project in projects:
            existing = self.gitea.list_org_hooks(project)
            hook_id = None
            if existing:
                for hook in existing:
                    url = hook.get("config", {}).get("url", "")
                    if url.startswith(self.webhook_url):
                        hook_id = hook.get("id")
                        break

            if hook_id is not None:
                print(f"Replacing Redmine webhook in {project} (id={hook_id})")
                if not self.gitea.delete_org_hook(project, hook_id):
                    self.errors += 1
            else:
                print(f"Adding Redmine webhook to {project}")

            if not self.gitea.create_org_hook(
                    project, self.webhook_url,
                    ["push", "pull_request", "issues"],
                    secret=self.webhook_secret or None):
                self.errors += 1

        if self.errors:
            die(f"redmine-webhooks completed with {self.errors} error(s)")
class SetupRedmineArgoWebhooks:
    EVENTS = ["issue.created", "issue.updated"]

    def __init__(self, redmine):
        self.redmine = redmine
        self.namespace = params.shoggoth_namespace
        domain = params.shoggoth_domain
        if not domain:
            die("SHOGGOTH_DOMAIN is required for argo-webhook URL")
        # Same pattern as SetupArgoWebhooks above: deliver through
        # api.<DOMAIN>/argo/ so web-internal injects the argo-webhook-receiver
        # bearer token (shoggoth/k3s/web-internal.yaml + argo-webhook-receiver.yaml)
        # before forwarding to argo:80. Going directly (http://argo:80/...)
        # would trip the v4 interceptor's gitea/redmine signature matcher
        # absence (webhookParsers at interceptor.go:25-30 has no entry for
        # either) and 403 the request.
        self.WEBHOOK_URL = (
            f"http://api.{domain}/argo/api/v1/events/{self.namespace}/redmine-task-processor"
        )
        self.errors = 0

    def execute(self):
        # Enumerate projects visible to slave. Slave's /projects.json scope
        # only returns projects slave is a member of (set in redmine-init), so
        # projects created after pod start without explicit membership are
        # missed. Trade-off accepted: avoid requiring a Redmine admin API key.
        projects = self.redmine.list_projects()
        if projects is None:
            die("Failed to list Redmine projects")
        project_ids = [str(p["id"]) for p in projects if p.get("id")]
        if not project_ids:
            die("No Redmine projects found — refusing to create webhook "
                "with empty scope (Redmine webhooks fire only for explicitly "
                "listed projects)")

        existing = self.redmine.list_webhooks()
        if existing is None:
            die("Failed to list Redmine webhooks")

        for hook in existing:
            hook_id = hook.get("id")
            if hook_id is not None:
                print(f"Removing existing webhook (id={hook_id})")
                self.redmine.delete_webhook(hook_id)

        print(f"Creating webhook: {self.WEBHOOK_URL} for {len(project_ids)} project(s)")
        result = self.redmine.create_webhook(self.WEBHOOK_URL, self.EVENTS,
                                             project_ids=project_ids)
        if result is None:
            self.errors += 1

        if self.errors:
            die(f"redmine-argo-webhooks completed with {self.errors} error(s)")
        print("redmine-argo-webhooks: webhooks configured successfully")
