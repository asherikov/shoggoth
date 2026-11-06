#!/usr/bin/env python3
"""Dispatch CLI utility — command-handler control plane.

Standalone CLI binary invoked by the ``dispatch`` step of the
``command-handler`` Argo WorkflowTemplate (see
``shoggoth/chart/files/workflow/argo/templates/command-handler.yaml``).
The dispatcher runs in a ``shoggoth_tools:alpine`` pod with
kubectl and validates the inbound webhook event before deciding
whether the worker step (``shoggoth.py <command>`` in a separate
``slave:noble`` pod) should run.

Decoupled from ``shoggoth.py`` because the dispatcher's
dependency surface is different: no agent, no git-cred sidecar,
no ``Shoggoth().checkout()``. It instantiates just ``Gitea`` and
``Redmine`` clients (for the validation API calls) and reuses
the ``DispatchCommand`` class from ``shoggoth.commands.dispatch``
for the per-command validation / output-emission logic.

Contract with the dispatch template:

  * The original command (``pr-update`` / ``task`` /
    ``ci-failure`` / ``sandbox``) arrives as the first
    command-line argument. The dispatch template execs
    ``dispatch.py "${SHOGGOTH_ORIGINAL_COMMAND}"
    "${SHOGGOTH_TASK_ID}"``, so a value containing shell
    metacharacters still arrives as a single argv element rather
    than being interpolated into bash source.
  * The optional Redmine issue ID arrives as the second
    command-line argument; it is empty for the non-task
    commands, which never read it.
  * ``GITEA_PAYLOAD`` carries the raw webhook JSON (set from
    ``inputs.parameters.payload``; empty for ``task`` /
    ``sandbox`` which are Gitea-agnostic).
  * ``SHOGGOTH_DOMAIN`` / ``REDMINE_SERVER`` are required by
    the ``Gitea`` / ``Redmine`` constructors (see
    ``Parameters`` in ``shoggoth.config``).
  * Outputs are written to file paths declared by the dispatch
    template's ``outputs.parameters[*].valueFrom.path``; see
    ``DispatchCommand.OUTPUT_PATHS`` for the exact paths.

Exit codes:

  * 0 — validation passed, ``should_run_worker=true`` is
    written, the argo DAG continues to the worker step (gated
    by the ``when:`` clause).
  * 0 — validation rejected the event (non-actionable Redmine
    task, ``ci-failure`` whose conclusion isn't ``"failure"``,
    etc.). The dispatcher emits ``should_run_worker=false`` and
    exits 0 so the argo workflow is marked Succeeded (not
    Failed); the worker step is skipped by the DAG ``when:``
    clause.
  * 1 — dispatcher error (missing env var, output-write
    failure, unknown command). The argo workflow is marked
    Failed.
  * 2 — malformed invocation. argparse rejects a missing
    ``original_command``, an unknown flag, or an unexpected
    extra positional, and exits 2 before any env var is read
    or output file written, so the argo workflow is marked
    Failed with no ``should_run_worker`` output at all.
  * 0 — ``-h`` / ``--help``. argparse prints the usage and
    exits 0 before the ``Gitea`` / ``Redmine`` constructors
    run, so no ``/tmp/dispatch-*`` output file is written.
"""
import argparse
import os
import sys

# The shoggoth package lives alongside this shim. ``shoggoth.py``
# does the same sys.path insert so its
# ``from shoggoth.cli import main`` works; mirror it here so
# ``from shoggoth.commands.dispatch import DispatchCommand``
# resolves to the package the workflow scripts dir contains.
SCRIPTS_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, SCRIPTS_DIR)

from shoggoth.commands.dispatch import DispatchCommand  # noqa: E402
from shoggoth.gitea import Gitea  # noqa: E402
from shoggoth.redmine import Redmine  # noqa: E402


def main():
    # Both dispatcher inputs are positionals. The dispatch template
    # execs ``dispatch.py "${SHOGGOTH_ORIGINAL_COMMAND}"
    # "${SHOGGOTH_TASK_ID}"`` — the env vars exist only so the
    # Go-template values never land in bash source — so a value
    # containing shell metacharacters still arrives as a single
    # argv element. The ``command`` attribute is set to the
    # literal ``"dispatch"`` so any future logging / debugging
    # helper that prints ``args.command`` shows the actual
    # subcommand.
    parser = argparse.ArgumentParser(
        prog="dispatch.py",
        description="Shoggoth dispatcher: validate an inbound webhook "
                    "event and decide whether the command-handler worker "
                    "step should run.")
    parser.add_argument("original_command",
                        help="The webhook's original command (`pr-update` / "
                             "`task` / `ci-failure` / `sandbox`)")
    parser.add_argument("task_id", nargs="?", default=None,
                        help="Redmine task ID (used by the `task` command)")
    parsed = parser.parse_args()

    args = argparse.Namespace(
        command="dispatch",
        original_command=parsed.original_command,
        task_id=parsed.task_id,
    )

    # ``read_tokens=False`` skips the Gitea/Redmine ``kubectl
    # get secret`` round-trips the constructors otherwise
    # perform. The dispatcher pod runs under the
    # shoggoth-sandbox-spawner SA which is scoped
    # to pods/services CRUD only — no ``get`` on
    # gitea-slave-token / gitea-server-token / redmine-slave-
    # token — so each read would 403. The api-proxy
    # (web-internal at api.${DOMAIN}) injects the bearer token
    # for the dispatcher's actual API calls (load payload is
    # already done from GITEA_PAYLOAD env var; sandbox branch's
    # post_pr_comment / redmine get_issue go through the
    # proxy), so the skipped reads don't break anything.
    gitea = Gitea(read_tokens=False)
    redmine = Redmine(read_tokens=False)

    cmd = DispatchCommand(args, gitea, redmine)
    cmd.execute()


if __name__ == "__main__":
    sys.exit(main())