# -*- coding: utf-8 -*-
"""Module split out of shoggoth.py; see package __init__."""
import hashlib
import json
import os
import secrets as _secrets
import sys
import time

from ..util import die, log, run
from .base import CommandBase
from ..state import params
from . import slash_commands


# ----- sandbox runtime -----------------------------------------------
# The four sandbox-pod lifecycle scripts (init: wait-bootstrap +
# init-git-cred-perms; main: ttyd; sidecar: cleanup) used to live
# as inline shell constants in this module. They are now a single
# Python module at ``shoggoth/workflow/scripts/sandbox.py``
# (subcommands: ``wait-bootstrap``, ``init-perms``, ``ttyd``,
# ``cleanup``), mounted into the sandbox pod at
# ``/shoggoth/workflow-scripts/`` via a hostPath volume (the
# ``workflow-scripts:workflow/scripts`` K3S_HOST_PATHS entry in
# the Makefile rsyncs the repo's ``workflow/scripts/`` to the
# cluster node). Each container invokes python3 with the
# subcommand and (for the ttyd subcommand) the user-controlled
# ``--repo`` and ``--branch`` values as separate argv items
# -- so the pre-fix shell-injection RCE (the branch name
# interpolated via Python ``.format()`` into a double-quoted
# shell arg, reachable from any Gitea user with write access on
# the base repo) is closed by construction: there is no shell
# any more. The slave image (used by the ttyd container) bakes
# the workflow scripts in at a different path for historical
# reasons; the hostPath mount is the authoritative copy. The
# tools image (used by the init containers and the cleanup
# sidecar) has no workflow scripts at all, so the hostPath
# mount is the only way those containers can invoke this module.
SANDBOX_PATH = "/shoggoth/workflow-scripts/sandbox.py"


class _SandboxPodNotReadyError(Exception):
    """Raised when the sandbox pod never became Ready.

    Surfaced as a regular ``Exception`` (not via ``die()`` /
    ``SystemExit``) so the surrounding ``except Exception``
    clause in ``_handle_sandbox`` runs and the diagnostic
    reaches the dispatcher's stdout (Argo captures it in
    the workflow log) before the load-bearing ``die()`` runs.
    ``die()`` raises ``SystemExit``, which inherits from
    ``BaseException`` (NOT ``Exception``), so a pre-fix
    ``die()`` inside ``_wait_sandbox_ready`` propagated
    straight through the except block and the diagnostic
    was never written anywhere -- the user had to dig
    through the Argo logs to find out the sandbox failed,
    and even then the post-mortem was racing the cleanup
    sidecar's deletion.

    The exception carries the diagnostic blob (describe
    output + per-container logs + pre-wait snapshot
    fallback) so the dispatcher has something concrete to
    log on the failure path. The PR comment is a short
    plain pointer -- the user is sent to the Argo
    workflow log for the actual diagnostic.
    """

    def __init__(self, sandbox_id, diagnostic):
        self.sandbox_id = sandbox_id
        self.diagnostic = diagnostic
        super().__init__(
            f"sandbox pod {sandbox_id} did not become Ready "
            f"within the dispatcher's wait budget")


class DispatchCommand(CommandBase):
    """Control-plane pod for the `command-handler` DAG.

    The dispatcher runs in a `shoggoth_tools:alpine` pod with
    kubectl available. Its job is to validate the inbound webhook
    event and decide whether the worker step (a separate
    `slave:noble` pod that does the heavy lifting -- git clone, agent
    run, git push) is actually needed.

    For most commands this is a near-pass-through -- the validation
    that used to run in the worker is duplicated here so we can
    `sys.exit(0)` before spending 5-10s booting the agent image.
    The worker keeps the same checks as defence-in-depth in case the
    state changes between dispatch and worker start.

    The dispatcher also writes two argo workflow outputs read by
    the DAG's `worker` step:

      * ``command`` -- the original command string (`pr-update`,
        `task`, `ci-failure`). Echoed back to the worker so the
        worker's argv still carries the command name.
      * ``should_run_worker`` -- the string `"true"` or `"false"`.
        The worker step is gated on this via the DAG `when:`
        condition. `sandbox` sets it to `"false"` because the
        sandbox lifecycle is fully owned by the dispatcher (the
        dispatcher kubectl-applies a sandbox pod, posts its URL,
        waits for terminal, cleans up).

    Failure mode (exit 1, NOT exit 0): the ``sandbox`` handler
    ``die()``s on any kubectl / payload / post_pr_comment
    failure so the Argo workflow is marked Failed. This is
    deliberate -- a silent exit 0 with no sandbox running is
    exactly the misleading-success failure mode the user
    reported (``dispatcher logs an error ... but succeeds``),
    so every sandbox bringup error path terminates with a
    load-bearing ``die()``. The other handlers (``task`` /
    ``ci-failure`` / ``pr-update``) exit 0 on the legitimate
    "not actionable" path; only true errors (``missing task
    ID``, ``unknown command``) ``die()``.

    Output emission: the dispatcher writes each output value to a
    file at the path declared by the dispatch template's
    `outputs.parameters[*].valueFrom.path`. The paths are static
    (one per declared output) so they can be referenced from the
    workflow template; ``_emit_output`` writes to the matching
    ``OUTPUT_PATHS[name]``.
    """

    # Dispatcher's workflow outputs. Keyed by the argo
    # ``outputs.parameters[*].name`` in the
    # `command-handler` DAG.
    OUTPUTS = ("command", "should_run_worker")

    # File paths the dispatcher writes each output to.
    OUTPUT_PATHS = {
        "command": "/tmp/dispatch-cmd",
        "should_run_worker": "/tmp/dispatch-should-run-worker",
    }

    def __init__(self, args, gitea, redmine):
        self.args = args
        self.gitea = gitea
        self.redmine = redmine
        # The original command (pr-update, task, ci-failure,
        # sandbox) is passed via the SHOGGOTH_ORIGINAL_COMMAND
        # env var by the dispatch template's `dispatch` task.
        self.original_command = os.environ.get(
            "SHOGGOTH_ORIGINAL_COMMAND", "")
        if not self.original_command:
            die("dispatch: SHOGGOTH_ORIGINAL_COMMAND env var is "
                 "required (the dispatch template's `dispatch` "
                 "task sets it from inputs.parameters.command)")
        # The Gitea payload is needed for every command except
        # ``task`` (which reads the Redmine task ID via CLI
        # instead -- see ``cli.py`` -- and never inspects the
        # Gitea webhook body). ``ci-failure`` reads
        # ``workflow_run.conclusion``; ``pr-update`` reads the
        # comment action / body / sender; ``sandbox`` reads
        # ``repository.full_name`` / ``pull_request.number`` /
        # ``pull_request.head.ref`` to populate the per-sandbox
        # pod manifest and Service URL; ``help`` reads the
        # same to determine the PR to post the catalog comment
        # to. The pre-fix logic excluded ``sandbox`` from the
        # load list and the sandbox handler then crashed with
        # ``AttributeError: 'NoneType' object has no attribute
        # 'get'`` from ``Gitea.get_repo_full_name`` on the
        # uninitialised ``_payload`` (``Gitea._payload`` is now
        # initialised to ``{}`` as a defence-in-depth default,
        # but the payload still needs to be loaded to populate
        # the actual fields -- loading an empty / Redmine-only
        # payload is a no-op cost).
        if self.original_command in ("ci-failure", "pr-update",
                                      "sandbox", "help"):
            self.gitea.load_payload()

    def execute(self):
        log(f"dispatch: original_command={self.original_command}")
        self._emit_output("command", self.original_command)

        # The single Gitea binding (gitea-pr-update-binding.yaml)
        # always passes ``original_command == "pr-update"``; slash-
        # command body-shape routing for ``/sandbox`` and ``/help``
        # happens inside ``_handle_pr_update`` so the body is
        # inspected only once (the dispatcher pod is already
        # running) and not in the binding's expr-lang selector.
        # ``task`` and ``ci-failure`` still arrive as distinct
        # ``original_command`` values from their own bindings.
        if self.original_command == "task":
            self._handle_task()
            return
        if self.original_command == "ci-failure":
            self._handle_ci_failure()
            return
        if self.original_command == "pr-update":
            self._handle_pr_update()
            return
        die(f"dispatch: unknown command {self.original_command!r} "
            f"(add a branch in DispatchCommand.execute)")

    # ----- per-command validation -------------------------------------

    def _handle_task(self):
        """Validate the Redmine task before booting the worker.

        Mirrors the worker's ``Shoggoth._check_task_actionable``:
        status must be ``In Progress`` AND the assignee must be
        the slave. The worker keeps its own copy of the check
        (defence-in-depth). The check here is fast-fail -- a
        non-actionable task exits 0 from the dispatcher, the
        worker is never started, and the argo workflow is marked
        Succeeded (not Failed).
        """
        task_id = self.args.task_id
        if task_id is None or not str(task_id).isdigit():
            die("dispatch: task command requires a numeric task ID")
        log(f"dispatch: task: validating task_id={task_id}")
        task = self.redmine.get_issue(task_id)
        if task is None:
            log(f"dispatch: task: redmine.get_issue returned None "
                f"for task_id={task_id}; exiting 0 (not actionable)")
            self._emit_output("should_run_worker", "false")
            sys.exit(0)
        status_name = (task.get("status") or {}).get("name")
        slave_user_login = params.shoggoth_slave_user
        assigned_to = task.get("assigned_to") or {}
        assigned_to_id = assigned_to.get("id")
        assigned_to_login = assigned_to.get("login")
        if assigned_to_login is None and assigned_to_id is not None:
            assigned_to_login = self.redmine.get_user_login(
                assigned_to_id)
        if status_name != "In Progress":
            log(f"dispatch: task: task_id={task_id} status={status_name!r} "
                f"(expected 'In Progress'); exiting 0 (not actionable)")
            self._emit_output("should_run_worker", "false")
            sys.exit(0)
        if assigned_to_login != slave_user_login:
            log(f"dispatch: task: task_id={task_id} "
                f"assignee_login={assigned_to_login!r} "
                f"(expected {slave_user_login!r}); exiting 0 "
                f"(not actionable)")
            self._emit_output("should_run_worker", "false")
            sys.exit(0)
        log(f"dispatch: task: task_id={task_id} is actionable")
        self._emit_output("should_run_worker", "true")

    def _handle_ci_failure(self):
        """Validate the CI workflow_run payload before booting
        the worker.

        Mirrors the worker's ``CiFailureCommand.execute`` first
        three lines: ``conclusion`` must be ``"failure"``. The
        worker keeps its own copy (defence-in-depth).
        """
        conclusion = self.gitea.get_ci_conclusion()
        log(f"dispatch: ci-failure: conclusion={conclusion!r}")
        if conclusion != "failure":
            log("dispatch: ci-failure: conclusion is not 'failure'; "
                "exiting 0 (not actionable)")
            self._emit_output("should_run_worker", "false")
            sys.exit(0)
        self._emit_output("should_run_worker", "true")

    def _handle_pr_update(self):
        """Route a pr-update event by comment body shape.

        The ``gitea-pr-update`` binding now subscribes to all
        three Gitea event classes (review, review_request,
        comment) and its selector is a pure cheap pre-filter
        (drop dismissed/deleted/slave-authored). Slash-command
        body-shape routing for ``/sandbox`` and ``/help``
        happens here so the dispatcher's pod is the only
        place that inspects the body:

          * ``/sandbox`` -> ``_handle_sandbox`` (worker skipped,
            dispatcher kubectl-applies the sandbox pod and
            posts the URL).
          * ``/help`` -> ``_handle_help`` (worker skipped,
            dispatcher posts the static catalog).
          * Everything else (reviews, review_requests, plain
            comments, ``/do`` / ``/review`` / ``/squash`` /
            ``/rebase``) -> pass-through. The remaining
            per-event validation lives in the worker
            because it depends on Gitea state at the moment
            the worker runs.
        """
        # Non-comment events (review / review_request) have
        # no ``comment`` field; the helper returns ``None``
        # and we fall through to pass-through.
        slash = self._detect_slash_command_from_body()
        if slash == "sandbox":
            self._handle_sandbox()
            return
        if slash == "help":
            self._handle_help()
            return
        log("dispatch: pr-update: pass-through")
        self._emit_output("should_run_worker", "true")

    def _detect_slash_command_from_body(self):
        """Return ``"sandbox"`` / ``"help"`` / ``None``.

        Reads ``payload.comment.body`` and matches the
        dispatcher's slash-command surface exactly:

          * ``"/sandbox"`` -> ``"sandbox"`` (exact match; no
            trailing args, mirroring the previous dedicated
            binding's contract).
          * ``"/help"`` -> ``"help"`` (exact match; same
            contract).
          * Anything else (other comments, reviews,
            review_requests) -> ``None``.

        Body is trimmed before comparison so leading / trailing
        whitespace doesn't silently break the exact-match
        contract — a user who types ``"/help "`` (with a
        trailing space) still triggers the help post.
        """
        comment = self.gitea._payload.get("comment") or {}
        body = (comment.get("body") or "").strip()
        if body == "/sandbox":
            return "sandbox"
        if body == "/help":
            return "help"
        return None

    def _handle_help(self):
        """Post the slash-command catalog as a PR comment.

        Triggered by a ``/help`` PR comment. The dispatcher
        is the SOLE owner of the catalog (see
        ``shoggoth/workflow/scripts/shoggoth/commands/slash_commands.py``
        -- ``SUPPORTED_COMMANDS`` and ``format_help_text()``).
        The pr-update worker does NOT have a ``_slash_help``
        handler, so a ``/help`` body that reaches the worker
        path (e.g. during a brief dispatcher-code rollout
        window where ``_detect_slash_command_from_body``
        hasn't been redeployed yet) silently no-ops rather
        than rendering a duplicate catalog post.

        Like ``_handle_sandbox``, this is a dispatcher-only
        handler: the worker step is skipped via
        ``should_run_worker=false``. End-to-end latency is
        ~2-5 s on the ``shoggoth_tools:alpine`` pod -- the
        worker path would otherwise spend ~60-90 s on a cold
        ``slave:noble`` image pull + agent init, which is
        unacceptable for a static catalog post.

        Trust gate: mirrors the pr-update worker's
        ``_handle_slash_command`` exactly. Posting the
        catalog is read-only, but the catalog itself names
        the surface (e.g. ``/drop <sha> [<sha> ...]`` syntax,
        the ``/sandbox`` sshd endpoint) -- a non-trusted
        user with PR-write access who could otherwise trigger
        ``/help`` would gain a probe channel to enumerate
        what the trusted humans can do, and the worker
        code's own comment cited exactly that
        ("posting a PR-visible 'not authorized' message
        would leak the existence of the command names and
        give unauthenticated users a probe channel") --
        so the rejection is silent, just like the worker.
        The check is a duplication of the worker's because
        the dispatcher route bypasses the worker entirely;
        the only alternative is a binding-level filter,
        but the binding's expr-lang cannot read
        ``params.shoggoth_trusted_users`` (a runtime
        config value), so the check has to live here.
        """
        # Trust gate (mirrors pr_update.PrUpdateCommand.
        # _handle_slash_command's gate; the dispatcher
        # route bypasses the worker, so the check has to
        # be duplicated here).
        comment = self.gitea._payload.get("comment") or {}
        author = (comment.get("user") or {}).get("login") or ""
        trusted_users = set(
            (params.shoggoth_trusted_users or "").split(","))
        trusted_users.discard("")
        if not author or author not in trusted_users:
            log(f"dispatch: help: rejecting /help from "
                f"non-trusted user {author!r} "
                f"(allowed={sorted(trusted_users)})",
                file=sys.stderr)
            return

        repo_full_name = self.gitea.get_repo_full_name()
        pr_number = self.gitea.get_pr_number()
        body = slash_commands.format_help_text()
        result = self.gitea.post_pr_comment_attributed(
            repo_full_name, pr_number, body)
        if result is None:
            # Same fail-loud contract as ``_handle_sandbox``:
            # the dispatcher is the entire lifecycle of the
            # ``/help`` command, and a silent success on a
            # failed post would leave the user with no
            # discoverability surface -- the very thing
            # ``/help`` is supposed to fix. ``die()``
            # surfaces the failure in the Argo log and
            # marks the workflow Failed so the operator
            # can investigate (Gitea API auth / quota /
            # repo-permission issues all land here).
            log("dispatch: help: WARNING failed to post PR "
                "comment with the help catalog",
                file=sys.stderr)
            die("help: pod is Ready but PR comment with the "
                "catalog was not posted -- user has no way "
                "to discover the supported slash commands")
        # Worker step is unnecessary for a static catalog
        # post; skipping it avoids the 60-90 s
        # ``slave:noble`` image pull on a cold cache.
        self._emit_output("should_run_worker", "false")

    def _handle_sandbox(self):
        """Launch a per-request sandbox pod + Service and post its URL.

        Triggered by a ``/sandbox`` PR comment. The dispatcher
        creates a unique ``sandbox-<id>`` Pod (git-cred sidecar +
        ttyd container + init container that clones the PR's
        repo) and a matching ``sandbox-<id>`` Service. The URL
        is posted as a PR comment so the requesting user can
        open the sandbox in a browser.

        Lifecycle:

          * User opens URL -> browser resolves
            ``sandbox-<id>.${SHOGGOTH_DOMAIN}`` against
            the host's DNS (an external CoreDNS rewrite maps the
            user-facing suffix to the in-cluster Service) -> kube-proxy
            DNATs to the pod's port 80 -> ttyd WebSocket
            on the pod. No nginx hop.
          * User closes the browser -> ttyd ``--once`` exits.
          * Main container exits 0; pod's ``restartPolicy:
            Never`` keeps it from restarting.
          * The ``cleanup`` sidecar (the ``cleanup`` subcommand
            of ``shoggoth/workflow/scripts/sandbox.py``,
            invoked via ``SANDBOX_PATH``) sees the
            pod's ``status.phase`` transition to a terminal
            state and runs ``kubectl delete pod
            --grace-period=0``, removing the pod from the
            API server. The sandbox pod no longer relies on
            K8s 1.23+ ``spec.ttlSecondsAfterFinished``
            (rejected by older API servers as "unknown
            field"), so the
            cleanup contract holds on every cluster version
            shoggoth supports.
          * Service's ``ownerReferences`` point at the pod, so
            K8s GC deletes the Service when the pod goes.

        No parallel or lifetime limits: each ``/sandbox`` comment
        creates a fresh pod+service; nothing prunes pods that
        are still Running.

        The handler exits 0 with ``should_run_worker=false`` --
        the dispatcher is the entire sandbox lifecycle.
        """
        log("dispatch: sandbox: launching per-request sandbox pod")
        try:
            sandbox_id = self._sandbox_id()
            sandbox_url = self._sandbox_url_for(sandbox_id)
            # PR-comment repo (base) -- this is where the
            # /sandbox URL is posted and where the comment
            # is visible. Stays as the base repo on every
            # PR (Gitea posts PR comments under the base
            # repo, not the head).
            repo_full_name = self.gitea.get_repo_full_name()
            # git-clone repo (head) -- the ``ttyd`` subcommand
            # of ``shoggoth/workflow/scripts/sandbox.py``
            # receives this as ``--repo`` and the branch as
            # ``--branch`` (separate argv items; no shell
            # interpolation, so the branch name cannot
            # inject shell metacharacters). For fork PRs
            # the head branch is physically hosted in the
            # contributor's fork
            # (``contributor/myrepo``), so cloning from
            # the base repo with ``--branch <head-branch>``
            # fails with "Remote branch not found in upstream
            # origin" -- the pre-fix code used
            # branch is physically hosted in the contributor's
            # fork (``contributor/myrepo``), so cloning from
            # the base repo with ``--branch <head-branch>``
            # fails with "Remote branch not found in upstream
            # origin" -- the pre-fix code used
            # ``get_repo_full_name()`` here, which returns
            # the base repo, and the sandbox pod's ttyd
            # container hit the failure on its 90 s startup
            # ``git-clone not ready`` timeout. Use
            # ``get_pr_head_repo_full_name()`` (falls back
            # to the base repo for same-repo PRs and for
            # the rare deleted-fork case where Gitea
            # serialises ``head.repo`` as null).
            head_repo_full_name = (
                self.gitea.get_pr_head_repo_full_name())
            pr_number = self.gitea.get_pr_number()
            pr_branch = self.gitea.get_pr_branch()
            log(f"dispatch: sandbox: id={sandbox_id} "
                f"repo={repo_full_name} "
                f"head_repo={head_repo_full_name} "
                f"pr={pr_number} branch={pr_branch}")

            # No per-sandbox credential is generated or
            # passed: the slave image (shoggoth/dockerfiles/slave)
            # bakes a no-password sshd_config
            # (``PermitEmptyPasswords yes`` + the ccws
            # account's shadow hash cleared via
            # ``passwd -d ccws`` at build time) and a
            # rewritten ``/etc/pam.d/sshd`` that uses
            # ``pam_unix.so nullok`` (the default Ubuntu
            # ``nullok_secure`` blocks empty passwords
            # on remote ttys). The user just runs
            # ``ssh ccws@<sandbox-host>`` and presses
            # Enter at the password prompt. Anyone who
            # can reach the per-sandbox Service (cluster
            # + the external DNS rewrite) can log in --
            # the same blast-radius as the existing ttyd
            # web terminal, which is also reachable by
            # anyone with the PR comment URL.
            self._apply_sandbox_pod(
                sandbox_id, head_repo_full_name, pr_branch)
            self._apply_sandbox_service(sandbox_id)
            # Snapshot the pod state immediately after apply,
            # BEFORE the wait starts. If the pod fails and the
            # cleanup sidecar deletes it during the 120 s wait
            # window, the post-wait diagnostic probes return
            # NotFound and the user gets a "pod was deleted"
            # diagnostic with no real data. The pre-wait
            # snapshot is a fallback: even if it's older than
            # the post-wait snapshot would have been, it
            # surfaces the most common failure modes
            # (init-container errors, image-pull failures,
            # ssh-keyscan timeouts) that all manifest in the
            # early lifecycle. The snapshot is fetched after
            # a short sleep so the kubelet has time to
            # populate container statuses (the pod's
            # ``status.containerStatuses`` is empty
            # immediately after ``kubectl apply``).
            pre_wait_state = self._snapshot_sandbox_pod(sandbox_id)
            self._wait_sandbox_ready(
                sandbox_id, pre_wait_state=pre_wait_state)

            body = self._format_sandbox_pr_comment(
                sandbox_url, sandbox_id)
            result = self.gitea.post_pr_comment_attributed(
                repo_full_name, pr_number, body)
            if result is None:
                # The pod is up but the user has no way to
                # know the URL -- fail loud so the workflow is
                # marked Failed and the user sees the
                # /sandbox action as broken rather than
                # silently succeeding. We log the WARNING
                # first so the operator can see the
                # post_pr_comment return code in the Argo log;
                # ``die`` raises SystemExit which propagates
                # through the outer ``except Exception`` and
                # is NOT swallowed (SystemExit is a
                # BaseException, not an Exception).
                log("dispatch: sandbox: WARNING failed to post "
                    "PR comment with sandbox URL", file=sys.stderr)
                die("sandbox: pod is Ready but PR comment "
                    "with the URL was not posted -- user has "
                    "no way to access the sandbox")
        except _SandboxPodNotReadyError as e:
            # ``_wait_sandbox_ready`` raises this on
            # timeout (NOT die() -- ``die()`` would raise
            # SystemExit, which the ``except Exception``
            # block cannot catch because SystemExit
            # inherits from BaseException, not Exception).
            # The exception carries the diagnostic blob
            # the wait built (``kubectl describe pod`` +
            # per-container ``kubectl logs`` + pre-wait
            # snapshot fallback). Per the user's sandbox
            # error-feedback contract: the PR comment is a
            # short, plain pointer (no diagnostic blob,
            # no code block, no Argo-log redirection -- the
            # user follows the pointer in the argo UI); the
            # full diagnostic goes to the dispatcher's
            # stdout (Argo captures it in the workflow
            # log) where an operator or a future diagnostic
            # tooling run can read it without a 30k char
            # PR-comment size cap.
            log(f"dispatch: sandbox: pod {e.sandbox_id} did "
                f"not become Ready", file=sys.stderr)
            # Emit the FULL diagnostic to the dispatcher
            # pod's stdout (which Argo captures in the
            # workflow log). No cap -- the diagnostic is
            # already bounded by the upstream kubectl
            # call output sizes, and the only consumer
            # here is a human/operator reading the log.
            log("dispatch: sandbox: diagnostic:\n"
                f"{e.diagnostic}", file=sys.stderr)
            try:
                # Short, plain pointer. No diagnostic blob
                # -- it's in the argo log. No Argo-log
                # pointer -- this comment IS the PR-side
                # pointer. The user (and any other
                # participant subscribed to the PR) sees
                # the failure category; the operator
                # investigating dives into the argo log
                # for the diagnostic.
                self.gitea.post_pr_comment_attributed(
                    self.gitea.get_repo_full_name(),
                    self.gitea.get_pr_number(),
                    f"Sandbox creation failed. "
                    f"See the Argo workflow log for "
                    f"details (pod "
                    f"`sandbox-{e.sandbox_id}` in "
                    f"namespace "
                    f"`{params.shoggoth_namespace}`).")
            except Exception:
                # Dead Gitea must NOT mask the load-bearing
                # die() below (the user wants the workflow
                # Failed, not a silent success).
                pass
            die(f"sandbox: pod {e.sandbox_id} did not become "
                f"Ready within the wait budget; diagnostic "
                f"emitted to dispatcher's stdout (argo log)")
        except Exception as e:
            log(f"dispatch: sandbox: ERROR bringup failed: {e}",
                file=sys.stderr)
            # Best-effort: post an error PR comment so the user
            # has a pointer in the PR thread. We deliberately
            # swallow any exception from the post itself -- a
            # dead Gitea at this point must NOT mask the
            # load-bearing die() below (the user wants the
            # workflow Failed, not a silent success).
            try:
                self.gitea.post_pr_comment_attributed(
                    self.gitea.get_repo_full_name(),
                    self.gitea.get_pr_number(),
                    f"Sandbox failed to start: `{e}`\n\n"
                    f"Check the workflow log for details.")
            except Exception:
                pass
            # Fail the workflow, not just the command. The PR
            # comment above gives the user a clean pointer to
            # the reason; die() surfaces it in the Argo log
            # (which Argo uses to mark the step Failed) and
            # stops the rest of the dispatch path from running
            # on a half-completed state. The pre-fix behaviour
            # silently continued and the workflow was marked
            # Succeeded with no sandbox running -- exactly the
            # misleading-success failure mode the user
            # reported ("dispatcher logs an error ... but
            # succeeds"). Exit 1 here is the load-bearing
            # signal that ``argo-server`` translates to a
            # Failed workflow status in the UI.
            die(f"sandbox: bringup failed: {e}")
        self._emit_output("should_run_worker", "false")

    # ----- sandbox helpers -------------------------------------------

    # Sandbox ID format: 12 hex chars derived from the Gitea
    # comment id + a per-call random nonce. The comment id
    # gives within-Gitea uniqueness; the nonce prevents
    # collisions when two comments land in the same webhook
    # burst. 12 hex chars is enough entropy for a single
    # cluster -- K8s object names are limited to 63 chars and
    # we still need room for the ``sandbox-`` prefix.
    SANDBOX_ID_LEN = 12
    SANDBOX_NAME_PREFIX = "sandbox-"

    def _sandbox_id(self):
        """Generate a unique sandbox identifier."""
        comment = (self.gitea._payload or {}).get("comment") or {}
        comment_id = str(comment.get("id") or "")
        nonce = _secrets.token_hex(8)
        digest = hashlib.sha256(
            f"{comment_id}:{nonce}".encode()).hexdigest()
        return digest[:self.SANDBOX_ID_LEN]

    def _sandbox_name(self, sandbox_id):
        return f"{self.SANDBOX_NAME_PREFIX}{sandbox_id}"

    def _sandbox_url_for(self, sandbox_id):
        """Return the user-facing URL for ``sandbox_id``.

        Points directly at the ``sandbox-<id>`` Service's
        ClusterIP via the user-facing DNS form
        ``sandbox-<id>.${SHOGGOTH_DOMAIN}`` -- PR
        comments are rendered as Markdown and clicked
        from the user's browser, which resolves the
        hostname against the host network's DNS, NOT the
        cluster's. The in-cluster
        ``sandbox-<id>.${SHOGGOTH_NAMESPACE}`` form would
        only resolve from inside the cluster, so anyone
        reading the PR comment outside the k3s network
        (or on a workstation whose DNS isn't wired to the
        cluster) would see a broken link. The
        user-facing form ``sandbox-<id>.${SHOGGOTH_DOMAIN}``
        requires the host to have the external DNS
        suffix wired in (the same constraint the
        ``git.${SHOGGOTH_DOMAIN}`` URL relies on for
        clients on the k3s network). The reverse-proxy
        route in web-internal is intentionally absent
        (removed because direct Service access works for
        the k3s-network case) -- the URL is the
        Service's ClusterIP via the external DNS
        rewrite, the kube-proxy DNATs to the pod's port
        80, and ttyd's WebSocket upgrade travels
        end-to-end without an nginx hop in the middle.
        """
        return (
            f"http://sandbox-{sandbox_id}."
            f"{params.shoggoth_domain}/"
        )

    def _format_sandbox_pr_comment(self, sandbox_url, sandbox_id):
        """Build the PR comment body for a successful sandbox.

        Includes the ttyd web URL and the SSH access
        details. No password is shown -- the slave image
        bakes a no-password sshd_config
        (``PermitEmptyPasswords yes`` + ``passwd -d
        ccws`` at build time) and a rewritten
        ``/etc/pam.d/sshd`` that uses ``pam_unix.so
        nullok``, so the user just runs the SSH command
        and presses Enter at the password prompt. The
        comment deliberately does NOT include the
        sandbox id's host key fingerprint (the user
        will see a "host key changed" warning on first
        connect -- acceptable for the per-cluster
        host-key model).
        """
        host = f"sandbox-{sandbox_id}.{params.shoggoth_domain}"
        return (
            f"Sandbox ready.\n\n"
            f"**Web terminal (ttyd):** {sandbox_url}\n\n"
            f"**SSH:** `ssh -o "
            f"StrictHostKeyChecking=accept-new ccws@{host}`\n\n"
            f"Press Enter when prompted for a password "
            f"(the ccws account has no password).\n"
        )

    def _apply_sandbox_pod(self, sandbox_id, repo_full_name, branch):
        """kubectl-apply the sandbox Pod manifest.

        Pod layout:

          * ``git-cred`` sidecar -- same shared sidecar as the
            worker pod / slave-term Deployment; populates
            ``/shoggoth/git-cred`` with ssh-keyscan,
            ssh-agent, and git-credential-cache.
          * ``git-clone`` init container -- uses the ready
            git-cred sidecar to ``git clone`` the PR's repo at
            the right branch into ``/repo``.
          * ``ttyd`` container -- runs ``ttyd --once tmux new-session bash``
            on port 80. The existing slave-term pattern
            (``|| true; exit 0``) ensures the exit is
            non-error so kubelet doesn't enter
            CrashLoopBackOff.

        ``restartPolicy: Never`` + the ``cleanup`` sidecar is
        the cleanup contract (the pre-fix
        ``spec.ttlSecondsAfterFinished`` field is rejected by
        clusters running K8s < 1.23).
        """
        namespace = params.shoggoth_namespace
        domain = params.shoggoth_domain
        pod_name = self._sandbox_name(sandbox_id)
        manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": pod_name,
                "namespace": namespace,
                "labels": {
                    "app": "shoggoth-coding-agent-sandbox",
                    "kind": "sandbox",
                    "sandbox-id": sandbox_id,
                },
            },
            "spec": {
                "serviceAccountName":
                    "shoggoth-sandbox-spawner",
                "restartPolicy": "Never",
                # Use the same custom DNS as the slave
                # Deployments. The default
                # ``dnsPolicy: ClusterFirst`` would resolve
                # the in-cluster short form
                # ``git.${SHOGGOTH_NAMESPACE}`` via the
                # kubelet's auto-generated search path, but it doesn't carry the
                # CoreDNS rewrite rules that map the user-facing
                # ``git.${SHOGGOTH_DOMAIN}`` form to its in-cluster equivalent -- so
                # anyone who runs ``git clone`` from the
                # sandbox terminal against the user-facing
                # URL (the GITEA_SERVER_URL / GITEA_SSH_HOST
                # env vars) would hit the same
                # ``Could not resolve host`` failure the
                # original ``/sandbox`` error reported. Wired
                # only when ``shoggoth_dns_ip`` is set; on
                # test/dev runs where the Argo template
                # doesn't propagate the IP the pod falls
                # back to the default ClusterFirst policy
                # (the in-cluster clone URL still works
                # there).
                **(
                    {"dnsPolicy": "None",
                     "dnsConfig": {
                         "nameservers": [params.shoggoth_dns_ip],
                         "searches": [
                             f"{namespace}.svc.cluster.local",
                             "svc.cluster.local",
                             "cluster.local",
                         ],
                     }} if params.shoggoth_dns_ip else {}),
                # No ``spec.ttlSecondsAfterFinished`` -- the
                # Pod-level field was added in K8s 1.23
                # (feature-gated until 1.29 GA) and the
                # shoggoth cluster's API server rejects it
                # with "unknown field
                # spec.ttlSecondsAfterFinished" on
                # ``kubectl apply``. The cleanup contract
                # (delete the pod shortly after ttyd exits)
                # is fulfilled by the ``cleanup`` sidecar
                # container below, which polls for
                # ``status.phase == Succeeded|Failed`` and
                # ``kubectl delete pod`` once it sees the
                # terminal phase. See
                # ``shoggoth/workflow/scripts/sandbox.py``
                # (the ``cleanup`` subcommand) for the script
                # body and the rationale for using a sidecar
                # instead of a CronJob / external watcher.
                "initContainers": [
                    {
                        "name": "wait-bootstrap",
                        "image": "asherikov/shoggoth_tools:alpine",
                        "imagePullPolicy": "IfNotPresent",
                        "env": [
                            {"name": "SHOGGOTH_NAMESPACE",
                             "value": namespace},
                        ],
                        "command": ["/usr/bin/python3"],
                        "args": [SANDBOX_PATH, "wait-bootstrap"],
                        "volumeMounts": [
                            {"name": "workflow-scripts",
                             "mountPath": "/shoggoth/workflow-scripts"},
                        ],
                        "resources": {
                            "requests": {"memory": "32Mi", "cpu": "50m"},
                            "limits": {"memory": "64Mi", "cpu": "200m"},
                        },
                    },
                    {
                        "name": "init-git-cred-perms",
                        "image": "asherikov/shoggoth_tools:alpine",
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["/usr/bin/python3"],
                        "args": [SANDBOX_PATH, "init-perms"],
                        "volumeMounts": [
                            {"name": "git-cred",
                             "mountPath": "/shoggoth/git-cred"},
                            {"name": "workflow-scripts",
                             "mountPath": "/shoggoth/workflow-scripts"},
                        ],
                        "resources": {
                            "requests": {"memory": "16Mi", "cpu": "10m"},
                            "limits": {"memory": "32Mi", "cpu": "50m"},
                        },
                    },
                    {
                        # OpenSSH's privilege-separation chroot.
                        # The sshd container runs with
                        # readOnlyRootFilesystem, so it cannot
                        # create /run/sshd itself -- sandbox.py's
                        # defensive `os.makedirs` raises EROFS and
                        # the container dies before sshd ever
                        # starts. Mounting an emptyDir over
                        # /run/sshd gives sshd a writable path
                        # without relaxing readOnlyRootFilesystem.
                        # kubelet creates emptyDirs mode 0777 and
                        # sshd refuses a group/world-writable
                        # privsep dir, so the perms are fixed here
                        # (the same emptyDir mounted at a staging
                        # path, since chmod must target the
                        # volume, not the mount point).
                        # Runs as root -- chown needs CAP_CHOWN.
                        "name": "init-sshd-run",
                        "image": "asherikov/shoggoth_tools:alpine",
                        "imagePullPolicy": "IfNotPresent",
                        "securityContext": {"runAsUser": 0},
                        "command": ["/bin/sh", "-c"],
                        "args": ["set -eu\nchown root:root /shoggoth/sshd-run\nchmod 0755 /shoggoth/sshd-run"],
                        "volumeMounts": [
                            {"name": "sshd-run",
                             "mountPath": "/shoggoth/sshd-run"},
                        ],
                        "resources": {
                            "requests": {"memory": "16Mi", "cpu": "10m"},
                            "limits": {"memory": "32Mi", "cpu": "50m"},
                        },
                    },
                    # The pre-fix design had a ``git-clone``
                    # init container here. That container
                    # was waiting on the ``git-cred`` sidecar's
                    # ready flag, but init containers run
                    # BEFORE all regular containers (including
                    # sidecars) -- so the sidecar couldn't
                    # start until the init container finished,
                    # and the init container couldn't finish
                    # until the sidecar's ready flag was set.
                    # The 90s ``git-cred not ready`` timeout
                    # was the visible failure mode. The clone
                    # is now inlined into the ``ttyd``
                    # container's startup (the ``ttyd``
                    # subcommand of
                    # ``shoggoth/workflow/scripts/sandbox.py``)
                    # so it waits inline for the sidecar after
                    # both containers are running.
                ],
                "containers": [
                    {
                        "name": "git-cred",
                        "image": "asherikov/shoggoth_tools:alpine",
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["/bin/bash",
                                    "/git-cred/bootstrap.sh"],
                        "env": [
                            {"name": "SHOGGOTH_DOMAIN",
                             "value": domain},
                            {"name": "SHOGGOTH_NAMESPACE",
                             "value": namespace},
                            {"name": "SSH_ID_RSA",
                             "valueFrom": {
                                 "secretKeyRef": {
                                     "name": "ssh-slave-private-key",
                                     "key": "ssh-id-rsa",
                                     "optional": True}}},
                            {"name": "GITEA_SLAVE_TOKEN",
                             "valueFrom": {
                                 "secretKeyRef": {
                                     "name": "gitea-slave-token",
                                     "key": "token",
                                     "optional": True}}},
                        ],
                        "volumeMounts": [
                            {"name": "git-cred",
                             "mountPath": "/shoggoth/git-cred"},
                            {"name": "git-cred-bootstrap",
                             "mountPath": "/git-cred",
                             "readOnly": True},
                        ],
                        "resources": {
                            "requests": {"memory": "32Mi", "cpu": "20m"},
                            "limits": {"memory": "64Mi", "cpu": "100m"},
                        },
                        "terminationMessagePolicy":
                            "FallbackToLogsOnError",
                        "securityContext": {"runAsUser": 1000},
                        "restartPolicy": "Always",
                        "readinessProbe": {
                            "exec": {
                                "command": ["sh", "-c",
                                            "test -f "
                                            "/shoggoth/git-cred/ready"],
                            },
                            "initialDelaySeconds": 0,
                            "periodSeconds": 1,
                            "timeoutSeconds": 1,
                            "successThreshold": 1,
                            "failureThreshold": 60,
                        },
                    },
                    {
                        "name": "ttyd",
                        "image": f"registry.{domain}/slave:noble",
                        "imagePullPolicy": "IfNotPresent",
                        "env": [
                            {"name": "SHOGGOTH_DOMAIN",
                             "value": domain},
                            {"name": "SHOGGOTH_NAMESPACE",
                             "value": namespace},
                            {"name": "GITEA_SSH_HOST",
                             "value": f"git.{domain}"},
                            {"name": "GITEA_SERVER_URL",
                             "value": f"http://git.{domain}"},
                            {"name": "REDMINE_SERVER",
                             "value": f"http://redmine.{domain}"},
                            # Wired only when params.shoggoth_dns_ip
                            # is set (i.e. the pod's dnsConfig
                            # carries the custom CoreDNS at
                            # params.shoggoth_dns_ip that
                            # rewrites *.${SHOGGOTH_DOMAIN} ->
                            # *.${SHOGGOTH_NAMESPACE}.svc.cluster.local).
                            # sandbox.py's _clone_url
                            # switches from the in-cluster short
                            # form `git.${SHOGGOTH_NAMESPACE}` to
                            # the user-facing
                            # `git.${SHOGGOTH_DOMAIN}` only when
                            # this is set -- the user-facing form
                            # matches the credential cache key the
                            # git-cred sidecar populates
                            # (`host=git.${SHOGGOTH_DOMAIN}`).
                            *([{"name": "SHOGGOTH_DNS_IP",
                                "value": params.shoggoth_dns_ip}]
                              if params.shoggoth_dns_ip else []),
                        ],
                        "command": ["/usr/bin/python3"],
                        "args": [
                            SANDBOX_PATH, "ttyd",
                            "--repo", repo_full_name,
                            "--branch", branch,
                        ],
                        "ports": [{"containerPort": 80, "name": "http"}],
                        "resources": {
                            "requests": {"memory": "256Mi",
                                         "cpu": "200m"},
                            "limits": {"memory": "1Gi", "cpu": "1"},
                        },
                        "readinessProbe": {
                            "httpGet": {"path": "/", "port": 80},
                            "initialDelaySeconds": 5,
                            "periodSeconds": 5,
                            "failureThreshold": 12,
                        },
                        # ``initialDelaySeconds: 120`` covers
                        # the worst-case startup: 90 s for the
                        # git-cred ready flag + 10-60 s for
                        # the git clone + a few seconds for
                        # ttyd to bind port 80. The pre-fix
                        # value of 30 s was too tight -- on a
                        # slow clone the kubelet's liveness
                        # probe started failing before ttyd
                        # was even up, the failureThreshold
                        # elapsed, and the kubelet killed the
                        # container before the user could
                        # connect. Once ttyd is up, the
                        # ``periodSeconds: 10`` /
                        # ``failureThreshold: 3`` (30 s of
                        # sustained failures) is the kill
                        # path for a hung ttyd -- faster
                        # than the pre-fix 90 s budget so the
                        # cleanup sidecar (which has its own
                        # not-ready watchdog) sees the
                        # termination signal sooner.
                        "livenessProbe": {
                            "httpGet": {"path": "/", "port": 80},
                            "initialDelaySeconds": 120,
                            "periodSeconds": 10,
                            "failureThreshold": 3,
                        },
                        "volumeMounts": [
                            {"name": "git-cred",
                             "mountPath": "/shoggoth/git-cred"},
                            {"name": "repo",
                             "mountPath": "/repo"},
                            {"name": "sandbox-state",
                             "mountPath": "/shoggoth/sandbox-state"},
                            {"name": "workflow-scripts",
                             "mountPath": "/shoggoth/workflow-scripts"},
                        ],
                    },
                    {
                        # SSH sidecar. Parallel sibling of the
                        # ``ttyd`` container in the same pod
                        # (both are regular ``containers:``
                        # entries, started by the kubelet at
                        # the same time, sharing the
                        # ``repo`` / ``sandbox-state`` /
                        # ``git-cred`` emptyDirs).
                        #
                        # The container:
                        #   1. waits for the git-cred ready flag
                        #      (so SSH users can run ``git
                        #      push`` from the sandbox),
                        #   2. waits for the repo-ready flag the
                        #      ttyd container touch()es after
                        #      the git clone (so the user
                        #      doesn't land in an empty /repo),
                        #   3. execs sshd in the foreground.
                        #
                        # No per-sandbox credential is needed
                        # here -- the slave image bakes a
                        # no-password sshd_config
                        # (``PermitEmptyPasswords yes``) and
                        # a cleared ccws account (built
                        # ``passwd -d ccws``), so any
                        # network-reachable user can log in
                        # as ccws by pressing Enter. The args
                        # list is fixed (no per-sandbox
                        # values), keeping the manifest
                        # diff-friendly across sandbox
                        # invocations.
                        #
                        # ``runAsUser: 0`` is required so sshd
                        # can bind port 22 (the slave
                        # image's sshd_config does not set a
                        # non-privileged port). ``AllowUsers
                        # ccws`` in the slave image's
                        # sshd_config locks the accepted
                        # user. ``readOnlyRootFilesystem:
                        # true`` is safe -- the sandbox.py
                        # ``sshd`` subcommand does not write
                        # to any mutable path (the previous
                        # chpasswd step was removed when the
                        # credential model was simplified).
                        "name": "sshd",
                        "image":
                            f"registry.{domain}/slave:noble",
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["/usr/bin/python3"],
                        "args": [
                            SANDBOX_PATH, "sshd",
                        ],
                        "env": [
                            {"name": "SHOGGOTH_DOMAIN",
                             "value": domain},
                            {"name": "SHOGGOTH_NAMESPACE",
                             "value": namespace},
                        ],
                        "ports": [{"containerPort": 22,
                                   "name": "ssh",
                                   "protocol": "TCP"}],
                        "securityContext": {
                            # sshd needs root to bind 22 on
                            # Linux; the slave image's
                            # sshd_config does not set a
                            # non-privileged port. The
                            # ``readOnlyRootFilesystem: true``
                            # is the strict default -- the
                            # sandbox.py ``sshd`` subcommand
                            # does not write to ``/etc/shadow``
                            # or any other mutable path
                            # (the per-sandbox password /
                            # authorized_keys flow was
                            # removed when the credential
                            # model was simplified to
                            # "no password, just press
                            # Enter" -- see the slave
                            # Dockerfile for the rationale).
                            # sshd needs ``CAP_NET_BIND_SERVICE``
                            # to bind 22 (the Linux kernel's
                            # bind(2) on a privileged port
                            # checks the capability, NOT the
                            # UID -- root without the cap is
                            # functionally identical to a
                            # non-root user for ports < 1024).
                            # ``capabilities.drop: [ALL]``
                            # strips every cap, so the bind
                            # would EACCES without an explicit
                            # ``add: [NET_BIND_SERVICE]``.
                            # Project precedent: the
                            # workflow controller uses the same
                            # ``drop: [ALL]`` + ``add:
                            # [NET_BIND_SERVICE]`` shape for
                            # containers binding privileged
                            # ports. All other hardening (cap
                            # drop, seccomp,
                            # no-priv-escalation) stays in
                            # place.
                            "runAsUser": 0,
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "capabilities": {
                                "drop": ["ALL"],
                                # ``DAC_OVERRIDE`` is required, not
                                # optional hardening. ``drop: [ALL]``
                                # strips it, and without it uid 0 is
                                # subject to *normal* permission
                                # checks -- root gets no automatic
                                # bypass. Two things break without
                                # it:
                                #   1. sandbox.py's ``_wait_for_git_cred``
                                #      polls /shoggoth/git-cred/ready,
                                #      but that emptyDir is 0700 owned
                                #      by uid 1000 (set by the
                                #      init-git-cred-perms init
                                #      container, because
                                #      git credential-cache refuses
                                #      a looser directory). The wait
                                #      can never succeed, so sshd
                                #      exits 1 after TTYD_READY_TIMEOUT_S.
                                #   2. sshd itself needs it to read
                                #      /etc/shadow (0640 root:shadow)
                                #      for PAM auth.
                                # Consequence of (1) beyond sshd:
                                # pod Ready is a conjunction over all
                                # containers, and sshd has no
                                # readinessProbe, so a dead sshd means
                                # the pod never goes Ready, the
                                # Service keeps the pod IP in
                                # notReadyAddresses, and *ttyd becomes
                                # unreachable too* even though it is
                                # serving happily. The sandbox looks
                                # alive in `kubectl get pod` and is
                                # dead for the user.
                            #
                            # Verified on k3s v1.35.8: a uid-0
                            # container with CapEff 0x400 (this cap
                            # set) cannot stat a 0700 uid-1000 dir;
                            # the same container with CapEff 0x402
                            # (+DAC_OVERRIDE) can.
                            #
                            # ``SYS_CHROOT`` + ``SETUID`` + ``SETGID``
                            # are OpenSSH privilege separation:
                            # privsep chroots the unprivileged monitor
                            # into /run/sshd and then drops root ->
                            # ccws. Each missing cap is its own preauth
                            # connection reset, in this order:
                            #   without SYS_CHROOT:
                            #     chroot("/run/sshd"): Operation not
                            #     permitted [preauth]
                            #   with SYS_CHROOT, without SETGID:
                            #     setgroups: Operation not permitted
                            #     [preauth]
                            # Only with all three does the connection
                            # reach PAM authentication. (NET_BIND_SERVICE
                            # alone -- the old set -- never got past
                            # the git-cred wait, so this was masked.)
                            #
                            # Verified on k3s v1.35.8 by connecting to
                            # a probe sshd per cap set and reading the
                            # preauth failure out of the container log.
                            #
                            # ``CHOWN`` and ``AUDIT_WRITE`` are
                            # required for the *interactive session*
                            # (not for reaching PAM auth). Without
                            # them, `mm_answer_pty` fails and the
                            # client gets "Connection to ... closed
                            # by remote host" immediately after a
                            # successful password prompt:
                            #   without CHOWN:
                            #     chown(/dev/pts/N, <uid>, 5)
                            #     failed: Operation not permitted
                            #   without AUDIT_WRITE (CHOWN present):
                            #     linux_audit_write_entry failed:
                            #     Operation not permitted
                            # Both abort the session teardown before
                            # the shell starts, so the user never gets
                            # a prompt while `kubectl get pod` still
                            # shows sshd running. Neither is a
                            # privilege-escalation risk here: CHOWN
                            # only applies to the fresh PTY sshd
                            # creates for this login, and AUDIT_WRITE
                            # is a log-write capability (the
                            # audit-*CONTROL* caps, which actually
                            # allow reconfiguration, stay dropped).
                            #
                            # Only these seven are re-added;
                            # everything else stays dropped (and
                            # seccomp, no-priv-escalation,
                            # readOnlyRootFilesystem remain in place).
                            "add": ["NET_BIND_SERVICE", "DAC_OVERRIDE",
                                    "SYS_CHROOT", "SETUID", "SETGID",
                                    "CHOWN", "AUDIT_WRITE"],
                            },
                            "seccompProfile":
                                {"type": "RuntimeDefault"},
                        },
                        "resources": {
                            "requests": {"memory": "64Mi",
                                         "cpu": "50m"},
                            "limits": {"memory": "128Mi",
                                       "cpu": "200m"},
                        },
                        # The kubelet's ``httpGet`` /
                        # ``tcpSocket`` / ``exec`` probes
                        # all require a port the kubelet can
                        # hit; the dispatcher's
                        # ``_wait_sandbox_ready`` blocks on
                        # the ttyd container's readinessProbe,
                        # which is sufficient for the user-
                        # facing Ready signal. sshd does not
                        # expose a health endpoint, so a probe
                        # here is omitted and the
                        # ``cleanup`` sidecar's not-ready
                        # watchdog (``CLEANUP_NOT_READY_BUDGET_S``
                        # in sandbox.py) is the de-facto
                        # liveness check for the whole pod.
                        # Without a probe, a hung sshd (e.g.
                        # crashed during a config reload)
                        # would not be restarted by the
                        # kubelet -- but the pod's
                        # ``restartPolicy: Never`` means the
                        # whole pod goes away once the ttyd
                        # container exits, so a per-container
                        # restart buys us nothing here.
                        "volumeMounts": [
                            {"name": "git-cred",
                             "mountPath": "/shoggoth/git-cred"},
                            {"name": "repo",
                             "mountPath": "/repo"},
                            {"name": "sandbox-state",
                             "mountPath": "/shoggoth/sandbox-state"},
                            {"name": "workflow-scripts",
                             "mountPath": "/shoggoth/workflow-scripts"},
                            # Writable privilege-separation chroot
                            # (chowned root:root 0755 by the
                            # init-sshd-run init container).
                            # readOnlyRootFilesystem means /run
                            # itself cannot be written.
                            {"name": "sshd-run",
                             "mountPath": "/run/sshd"},
                        ],
                    },
                    {
                        # Cleanup sidecar. Polls for
                        # ``status.phase`` to reach Succeeded
                        # or Failed (which happens after ttyd
                        # exits because the pod's
                        # ``restartPolicy: Never`` stops the
                        # pod) and ``kubectl delete pod`` with
                        # ``--grace-period=0`` to remove the
                        # pod promptly. The ``cleanup``
                        # container itself runs inside the pod
                        # it's deleting, so ``--grace-period=0``
                        # is critical -- without it the API
                        # server would wait for the cleanup
                        # container's own SIGTERM to fire
                        # before finalising the deletion,
                        # deadlocking the kubelet.
                        #
                        # The Downward API env vars
                        # (``metadata.name`` /
                        # ``metadata.namespace``) come from the
                        # pod spec's ``metadata`` block, so the
                        # script doesn't need to take them as
                        # argv.
                        "name": "cleanup",
                        "image":
                            "asherikov/shoggoth_tools:alpine",
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["/usr/bin/python3"],
                        "args": [SANDBOX_PATH, "cleanup"],
                        "env": [
                            {"name": "SHOGGOTH_NAMESPACE",
                             "valueFrom": {
                                 "fieldRef": {
                                     "fieldPath":
                                         "metadata.namespace"}}},
                            {"name": "SHOGGOTH_POD_NAME",
                             "valueFrom": {
                                 "fieldRef": {
                                     "fieldPath":
                                         "metadata.name"}}},
                        ],
                        "volumeMounts": [
                            {"name": "workflow-scripts",
                             "mountPath": "/shoggoth/workflow-scripts"},
                        ],
                        "resources": {
                            "requests": {"memory": "16Mi",
                                         "cpu": "10m"},
                            "limits": {"memory": "32Mi",
                                       "cpu": "50m"},
                        },
                    },
                ],
                "volumes": [
                    {"name": "git-cred", "emptyDir": {}},
                    {"name": "git-cred-bootstrap",
                     "configMap": {
                         "name": "git-cred-bootstrap",
                         "defaultMode": 493,
                     }},
                    {"name": "sshd-run", "emptyDir": {}},
                    {"name": "repo", "emptyDir": {}},
                    # Shared between the ttyd and sshd
                    # containers; the ttyd container
                    # touch()es /shoggoth/sandbox-state/repo-ready
                    # after the git clone, the sshd
                    # container polls for it. Lives in a
                    # separate emptyDir from /repo so a
                    # ``rm -rf /repo/.ready`` by the user
                    # can't break the readiness signal.
                    {"name": "sandbox-state", "emptyDir": {}},
                    # HostPath of the cluster-node's workflow-
                    # scripts directory. The Makefile's
                    # `workflow-scripts:workflow/scripts`
                    # K3S_HOST_PATHS entry rsyncs the repo's
                    # `workflow/scripts/` to
                    # `/var/lib/rancher/k3s/storage/${INSTANCE}/workflow-scripts/`
                    # on the cluster node at `make sync` time,
                    # so the same absolute path is visible to
                    # every container in the pod after sync.
                    # The init containers and the cleanup
                    # sidecar use the tools image which has no
                    # workflow scripts baked in; this is the
                    # only way those containers can invoke
                    # SANDBOX_PATH. The ttyd container
                    # uses the slave image which bakes the
                    # scripts in at a different historical
                    # path; the mount is still added for
                    # symmetry and to make this module the
                    # authoritative copy of the lifecycle.
                    {"name": "workflow-scripts",
                     "hostPath": {
                         "path": f"/var/lib/rancher/k3s/storage/"
                                 f"{namespace}/workflow-scripts/",
                     }},
                ],
            },
        }
        result = run(["kubectl", "apply", "-f", "-"],
                      input=json.dumps(manifest), check=False)
        if result.returncode != 0:
            die(f"sandbox: kubectl apply pod failed: "
                f"{result.stderr.strip()}")

    def _apply_sandbox_service(self, sandbox_id):
        """kubectl-apply the per-sandbox Service.

        The Service selects the unique sandbox pod via the
        ``sandbox-id`` label. ``ownerReferences`` point at the
        pod so K8s GC deletes the Service when the pod is
        deleted by the sandbox pod's ``cleanup`` sidecar.
        """
        namespace = params.shoggoth_namespace
        service_name = self._sandbox_name(sandbox_id)
        manifest = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": service_name,
                "namespace": namespace,
                "labels": {
                    "app": "shoggoth-coding-agent-sandbox",
                    "kind": "sandbox",
                    "sandbox-id": sandbox_id,
                },
                # Pod UID is unknown at apply time -- the
                # dispatcher patches it in via a second
                # ``kubectl patch`` after the pod exists.
                "ownerReferences": [],
            },
            "spec": {
                "selector": {"sandbox-id": sandbox_id},
                "ports": [
                    {"name": "http", "port": 80, "targetPort": 80},
                    # sshd is exposed on the same Service
                    # (and therefore the same user-facing
                    # hostname ``sandbox-<id>.${SHOGGOTH_DOMAIN}``)
                    # so the user only needs the single
                    # PR-comment URL + the single ``ssh
                    # ccws@...`` command. The
                    # ``ssh`` port name matches the
                    # container's named port in
                    # ``_apply_sandbox_pod`` so kube-proxy
                    # DNATs correctly. Per-sandbox port
                    # (targetPort 22) is required because
                    # the slave image's sshd_config binds
                    # 22 (no Port directive override);
                    # changing it would require baking a
                    # new slave image and rebuilding every
                    # sandbox. The Service port is also 22
                    # (cluster-local) so clients on the
                    # cluster network reach sshd on its
                    # native port. The user-facing
                    # ``ssh`` command omits ``-p 22``
                    # because sshd listens on the default
                    # SSH port.
                    {"name": "ssh", "port": 22,
                     "targetPort": 22, "protocol": "TCP"},
                ],
                "type": "ClusterIP",
            },
        }
        result = run(["kubectl", "apply", "-f", "-"],
                      input=json.dumps(manifest), check=False)
        if result.returncode != 0:
            die(f"sandbox: kubectl apply service failed: "
                f"{result.stderr.strip()}")

        # Patch the ownerReference onto the Service now that
        # the Pod exists.
        pod_name = service_name
        result = run(
            ["kubectl", "get", "pod", "-n", namespace,
             pod_name, "-o", "jsonpath={.metadata.uid}"],
            check=False)
        if result.returncode != 0 or not result.stdout.strip():
            # Pod-UID lookup failure is fatal -- without the
            # UID we can't set the ownerReference, and the
            # Service would leak past the pod's lifetime
            # (K8s GC only cascades-delete child services
            # with an ownerReference pointing at the
            # deleted pod). The pre-fix code logged a
            # WARNING and continued; the Service would
            # silently linger in the cluster. Fail loud so
            # the workflow is marked Failed and the user
            # can retry.
            die(f"sandbox: could not look up pod UID for "
                f"ownerReference patch (rc={result.returncode}, "
                f"err={result.stderr.strip()!r}); refusing to "
                f"leave a Service without ownerReference behind")
        pod_uid = result.stdout.strip()
        patch = [{
            "op": "add",
            "path": "/metadata/ownerReferences",
            "value": [{
                "apiVersion": "v1",
                "kind": "Pod",
                "name": pod_name,
                "uid": pod_uid,
                "blockOwnerDeletion": False,
            }],
        }]
        result = run(
            ["kubectl", "patch", "-n", namespace, "service",
             service_name, "--type=json",
             "-p", json.dumps(patch)],
            check=False)
        if result.returncode != 0:
            # Same reasoning as the UID-lookup die() above:
            # a Service without the ownerReference leaks
            # when the pod is TTL'd. The pre-fix code
            # logged WARNING and continued, leaving a
            # ghost Service in the cluster for every
            # /sandbox invocation that hit a transient
            # patch failure (auth hiccup, RBAC mismatch,
            # etc.). Fail loud so the workflow is marked
            # Failed and the user retries.
            die(f"sandbox: kubectl patch ownerReference "
                f"failed: {result.stderr.strip()!r}; refusing "
                f"to leave a Service without ownerReference "
                f"behind (it would linger past the pod's "
                f"lifetime)")

    def _wait_sandbox_ready(self, sandbox_id, pre_wait_state=None):
        """Block until the sandbox pod is Ready (or fail loud).

        Budget 120 s: covers the slowest observed path
        (image pull + git-cred bootstrap + git clone of a
        typical PR-sized repo). ``kubectl wait`` returns
        non-zero on timeout, which surfaces as a failure
        comment via the outer ``try/except`` in
        ``_handle_sandbox``.

        ``pre_wait_state`` is an optional ``(describe,
        per_container_logs)`` tuple captured by the caller
        BEFORE the wait starts (see ``_handle_sandbox``).
        When the post-wait probes return empty (the pod was
        deleted during the wait window by the cleanup
        sidecar), the pre-wait snapshot is used as a fallback
        -- it's not as fresh as a post-wait snapshot but it's
        strictly better than the "pod was deleted before logs
        could be fetched" message that hid the real failure
        reason before.

        Diagnostic output on timeout:

        The pre-fix version called ``kubectl logs
        --all-containers`` and bailed when the pod was
        gone (the cleanup sidecar sometimes deletes the
        pod before the dispatcher's wait returns, leaving
        ``NotFound`` and an empty tail). The new version
        chains three probes in order of diagnostic value:

          1. ``kubectl describe pod`` -- the single best
             source of container exit codes / messages,
             init-container states, and the ``Events:``
             section (which captures image-pull failures,
             ``BackOff`` rate-limits, and
             ``Failed``/``Killing`` kubelet messages that
             never make it to container stdout/stderr).
          2. ``kubectl logs`` per individual container
             (``initContainers`` first, then
             ``containers``). Per-container logs are
             fetched in the order the kubelet would have
             surfaced them, and the dispatcher surfaces
             whichever it can -- a partially-deleted pod
             may still have ``git-cred`` logs reachable
             after ``ttyd`` is gone, and ``describe`` is
             guaranteed reachable for any pod that exists
             or existed during the wait.
          3. The best ``kubectl logs --all-containers``
             tail (kept for back-compat with the existing
             workflow-logs-viewer pattern).

        If every probe fails (the cleanup sidecar deleted
        the pod during the wait window), the dispatcher
        surfaces the pre-wait snapshot (if available) or a
        "pod was deleted" message so the user has
        *something* concrete to debug against.
        """
        namespace = params.shoggoth_namespace
        pod_name = self._sandbox_name(sandbox_id)
        result = run(
            ["kubectl", "wait", "--for=condition=Ready",
             f"pod/{pod_name}", "-n", namespace, "--timeout=120s"],
            check=False)
        if result.returncode != 0:
            log(f"sandbox: pod did not become Ready: "
                f"{result.stderr.strip()}",
                file=sys.stderr)
            # 1. ``kubectl describe pod`` -- best single
            #    source of truth for why a pod failed to
            #    become Ready. Includes container exit
            #    reasons, Events (image-pull failures,
            #    BackOff), and init-container status.
            describe_result = run(
                ["kubectl", "describe", "pod", pod_name,
                 "-n", namespace],
                check=False)
            describe_tail = (
                describe_result.stdout[-3000:]
                if describe_result.returncode == 0
                and describe_result.stdout else "")
            # 2. ``kubectl logs`` per individual container
            #    -- surfaces stdout/stderr even if the pod
            #    is being deleted (one container might
            #    still be reachable while another isn't).
            log_chunks = []
            containers_to_log = (
                self._pod_containers_to_log(
                    describe_result.stdout))
            for container_name in containers_to_log:
                c_log = run(
                    ["kubectl", "logs", "-n", namespace,
                     pod_name, "-c", container_name,
                     "--tail=50"],
                    check=False)
                if c_log.returncode == 0 and c_log.stdout:
                    log_chunks.append(
                        f"=== {container_name} ===\n"
                        + c_log.stdout.rstrip())
            # 3. ``kubectl logs --all-containers`` -- the
            #    pre-fix probe. Kept for back-compat with
            #    the workflow-logs-viewer pattern (the
            #    Argo UI groups per-container logs by
            #    container name, but the dispatcher
            #    surfaces a combined fallback when
            #    per-container probes are empty).
            all_log_result = run(
                ["kubectl", "logs", "-n", namespace, pod_name,
                 "--tail=50", "--all-containers"],
                check=False)
            all_log_tail = (
                all_log_result.stdout[-2000:]
                if all_log_result.returncode == 0
                and all_log_result.stdout else "")
            # Compose the diagnostic blob the user will
            # see in the PR failure comment and in the
            # Argo log.
            diag_parts = []
            if describe_tail:
                diag_parts.append(
                    "=== kubectl describe pod (post-wait) ===\n"
                    + describe_tail)
            if log_chunks:
                diag_parts.append(
                    "=== per-container logs (post-wait) ===\n"
                    + "\n\n".join(log_chunks))
            if all_log_tail and not log_chunks:
                # Only show the combined tail when the
                # per-container probes came back empty (no
                # duplication with #2 above).
                diag_parts.append(
                    "=== kubectl logs --all-containers (post-wait) ===\n"
                    + all_log_tail)
            # If the post-wait probes are empty (the
            # cleanup sidecar beat the dispatcher to the
            # post-mortem), fall back to the pre-wait
            # snapshot the caller captured before the
            # wait started. This is strictly better than
            # the "pod was deleted" message because the
            # user gets to see WHY the pod was failing at
            # the start of the wait -- the most common
            # failure modes (init-container errors,
            # image-pull failures, ssh-keyscan timeouts)
            # all surface in the early lifecycle.
            if not diag_parts and pre_wait_state is not None:
                pre_describe, pre_logs = pre_wait_state
                diag_parts.append(
                    "Pod was deleted during the wait window "
                    "(cleanup sidecar beat the dispatcher's "
                    "post-wait diagnostic). Falling back to "
                    "the pre-wait snapshot captured "
                    "immediately after `kubectl apply pod`.")
                if pre_describe:
                    diag_parts.append(
                        "=== kubectl describe pod (pre-wait) ===\n"
                        + pre_describe[-3000:])
                if pre_logs:
                    diag_parts.append(
                        "=== per-container logs (pre-wait) ===\n"
                        + "\n\n".join(pre_logs))
            if not diag_parts:
                # Last resort -- neither the post-wait nor
                # the pre-wait probes yielded anything. The
                # user knows to look at the Argo logs for
                # the dispatcher's earlier kubectl
                # invocations.
                diag_parts.append(
                    "pod was deleted before logs could be "
                    "fetched (cleanup sidecar beat the "
                    "dispatcher; check the Argo log for "
                    "the ``kubectl describe pod`` output "
                    "above for the failure reason)")
            diag = "\n\n".join(diag_parts)
            # Raise the exception (NOT die() -- see
            # ``_SandboxPodNotReadyError`` docstring). The
            # outer ``except Exception`` in
            # ``_handle_sandbox`` catches this, emits the
            # full diagnostic to the dispatcher's stdout
            # (Argo captures it in the workflow log), posts
            # a short plain pointer as a PR comment, and
            # re-dies. A pre-fix ``die()`` here raised
            # SystemExit, which is a BaseException (not
            # Exception) and propagated straight through
            # the except block -- the diagnostic was
            # never written anywhere and the user never
            # saw the failure on the PR.
            raise _SandboxPodNotReadyError(sandbox_id, diag)

    def _snapshot_sandbox_pod(self, sandbox_id):
        """Capture pod state for the diagnostic fallback chain.

        Called by ``_handle_sandbox`` after ``kubectl apply
        pod`` and before ``_wait_sandbox_ready`` so that even
        if the cleanup sidecar deletes the pod during the 120 s
        wait window, the dispatcher has a snapshot of the pod's
        early lifecycle -- the point where the most common
        failure modes (init-container errors, image-pull
        failures, ssh-keyscan timeouts) actually surface.

        The snapshot is just ``(describe_stdout,
        per_container_logs)`` -- the same shape
        ``_wait_sandbox_ready`` produces from its post-wait
        probes. ``_wait_sandbox_ready`` substitutes the
        snapshot into its diagnostic chain when the post-wait
        probes come back empty.

        Returns ``(None, None)`` if the snapshot can't be
        captured (e.g., the pod doesn't exist when we look --
        it shouldn't, but we'd rather the dispatcher continue
        than crash here).
        """
        namespace = params.shoggoth_namespace
        pod_name = self._sandbox_name(sandbox_id)
        # ``kubectl apply`` is asynchronous; the API server
        # has the pod immediately but the kubelet takes a
        # moment to populate ``status.containerStatuses``.
        # Wait briefly so the snapshot has meaningful data
        # rather than an empty ``status:`` block.
        time.sleep(2)
        describe_result = run(
            ["kubectl", "describe", "pod", pod_name,
             "-n", namespace],
            check=False)
        describe = (describe_result.stdout
                    if describe_result.returncode == 0 else "")
        per_container = []
        for container_name in self._pod_containers_to_log(
                describe):
            c_log = run(
                ["kubectl", "logs", "-n", namespace,
                 pod_name, "-c", container_name,
                 "--tail=50"],
                check=False)
            if c_log.returncode == 0 and c_log.stdout:
                per_container.append(
                    f"=== {container_name} ===\n"
                    + c_log.stdout.rstrip())
        return describe, per_container

    def _pod_containers_to_log(self, describe_stdout):
        """Best-effort list of container names to fetch logs from.

        Parses the ``Containers:`` and ``Init Containers:``
        sections of ``kubectl describe pod`` output and
        returns them in the order the dispatcher should try
        to fetch logs from. initContainers come first because
        their failures (externalsecret-not-Ready,
        ssh-keyscan-failed, etc.) are the most common
        reason the pod never becomes Ready -- fetching their
        logs first surfaces the root cause even when the
        pod is in the process of being deleted and some
        containers' logs are no longer reachable.
        """
        if not describe_stdout:
            return ()
        # ``kubectl describe pod`` uses two-space indent
        # for container names (``  git-cred:``) and
        # zero-space indent for top-level section
        # headers (``Containers:``, ``Conditions:``,
        # ``Volumes:``, ``Limits:``, ``Requests:``,
        # ``Environment:``, ``Mounts:``,
        # ``Node-Selectors:``, ``Tolerations:``,
        # ...). The earlier version of this function
        # accepted any ``<word>:`` line and never
        # exited the ``Containers:`` section, so it
        # captured ``Limits:``, ``Requests:``,
        # ``Environment:``, ``Mounts:``,
        # ``Conditions:``, and ``Volumes:`` as
        # "container names" and the per-container log
        # probes turned into a wall of
        # ``container Limits is not valid for pod ...``
        # errors that buried the actual failure
        # (init-container error, image-pull
        # failure, etc.). The state machine below
        # enforces: (a) two leading spaces for
        # container names, (b) ``Init Containers:``
        # transitions us into the init section, (c)
        # ``Containers:`` transitions us out of init
        # and into the main section, and (d) any
        # column-0 line after the main section ends
        # closes it permanently (no re-entry).
        names = []
        in_init = False
        in_containers = False
        for line in describe_stdout.splitlines():
            stripped = line.lstrip()
            if not (in_init or in_containers):
                # Pre-init sections (Name:, Namespace:,
                # Labels:, Annotations:, Status:, ...) or
                # post-containers sections (Limits:,
                # Conditions:, Volumes:, ...). The only
                # markers we care about are the two
                # section headers we want to enter.
                if stripped.startswith("Init Containers:"):
                    in_init = True
                elif stripped.startswith("Containers:"):
                    in_containers = True
                continue
            # ``Init Containers:`` -> ``Containers:``
            # transition. While inside the init
            # section the only column-0 marker that
            # can appear is the opening of the main
            # section; recognise it and switch.
            if in_init and stripped.startswith("Containers:"):
                in_init = False
                in_containers = True
                continue
            # Inside the main ``Containers:``
            # section, a column-0 line opens a new
            # top-level section (``Conditions:``,
            # ``Volumes:``, ``Limits:``,
            # ``Requests:``, ``Environment:``,
            # ``Mounts:``, ``Node-Selectors:``,
            # ``Tolerations:``, ...). Close the
            # section; no re-entry.
            if in_containers and line \
                    and not line.startswith(" "):
                in_containers = False
                continue
            # Container header line: exactly two
            # leading spaces (not zero, not
            # three-plus) so we reject the column-0
            # section headers above and the
            # four-space property lines
            # (``    Image: ...``, ``    State: ...``)
            # below. First char must be a letter or
            # digit so we don't capture
            # ``kubectl describe pod`` noise like
            # ``  - foo`` if it ever appears.
            if line.startswith("  ") \
                    and not line.startswith("   ") \
                    and stripped.endswith(":") \
                    and stripped[:-1] \
                    and stripped[0].isalnum():
                names.append(stripped[:-1])
        return tuple(names)

    # ----- output emission -------------------------------------------

    def _emit_output(self, name, value):
        """Write ``name``'s output to the path registered for it."""
        if name not in self.OUTPUTS:
            die(f"dispatch: _emit_output called with unknown "
                f"output {name!r} (not in DispatchCommand.OUTPUTS)")
        path = self.OUTPUT_PATHS[name]
        try:
            with open(path, "w") as f:
                f.write(str(value))
        except OSError as e:
            die(f"dispatch: failed to write output "
                f"{name}={value!r} to {path}: {e}")
