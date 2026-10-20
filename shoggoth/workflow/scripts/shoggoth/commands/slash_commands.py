"""slash_commands -- single source of truth for the slash-command catalog.

The dispatcher (``shoggoth/workflow/scripts/shoggoth/commands/dispatch.py``)
owns the canonical list of slash commands and the ``/help`` message
renderer (``format_help_text()``). The worker does NOT consult this
catalog: it implements handlers for the worker-owned commands directly
(``/review``, ``/squash``, ``/rebase``, ``/drop``, ``/do``) and
treats every other command name as a silent no-op. This keeps the
worker free of catalog imports (no round-trip dependency on
``SUPPORTED_COMMANDS`` or ``format_help_text``) and removes the
historical defense-in-depth ``_slash_help`` fallback that mirrored
the dispatcher's catalog post.

Handler field
-------------
Each ``SlashCommand`` carries a ``handler`` field, one of:

  * ``"dispatcher"`` -- handled entirely in the dispatcher's
    ``command-handler`` DAG step. The worker step is skipped via
    ``should_run_worker=false``. Examples today: ``/sandbox`` (which
    kubectl-applies a per-PR pod + Service) and ``/help`` (which
    posts a static catalog). These commands are fast: the dispatcher
    is a ``shoggoth_tools:alpine`` pod with no agent / no
    image-pull of ``slave:noble``, so end-to-end latency is ~2-5 s
    instead of the worker's ~60-90 s on a cold cache.

  * ``"worker"`` -- handled by the ``slave:noble`` worker step in
    the same DAG. The dispatcher is a near-pass-through (it emits
    ``should_run_worker=true`` and the worker runs the actual
    command). Examples today: ``/review``, ``/squash``,
    ``/rebase``, ``/drop``, ``/do``.

The dispatcher's ``_handle_pr_update`` body-shape router is the
authoritative gate for which commands are recognised at all. The
single ``gitea-pr-update`` binding only filters by cheap shape
(drop dismissed/deleted/slave-authored); commands not in the
catalog are still routed through the dispatcher (as
``command: pr-update``), which falls through to pass-through, and
the worker's ``_handle_slash_command`` returns a silent no-op for
unknown names (a PR-visible "not a command" reply would leak
command-name existence to untrusted users and turn the comment
thread into a probe channel).

Adding a new slash command
--------------------------

  1. Append a ``SlashCommand`` to ``SUPPORTED_COMMANDS`` below.
  2. If ``handler == "dispatcher"``:
     - Add a ``_handle_<name>`` method on
       ``DispatchCommand`` that does the work and posts any
       PR comment.
     - The handler MUST call
       ``self._emit_output("should_run_worker", "false")`` so
       the DAG skips the worker step.
     - Extend ``_handle_pr_update`` (or ``_detect_slash_command_from_body``
       if more commands land here) so the body's exact-match
       routes to the new ``_handle_<name>``. The single
       ``gitea-pr-update`` binding subscribes to every Gitea
       event class; body-shape routing is the dispatcher's
       responsibility, not a binding-level filter.
  3. If ``handler == "worker"``:
     - Add a ``_slash_<name>`` method on
       ``PrUpdateCommand`` that does the work. The worker
       does not consult this catalog; if the command name
       doesn't match any ``_slash_<name>`` method, the
       worker's ``_handle_slash_command`` silently no-ops,
       which is the right answer for any command the
       dispatcher should have caught but didn't.
"""
from collections import namedtuple


# Use a namedtuple rather than a plain tuple so accidental
# positional access (``cmd[0]`` vs ``cmd.name``) surfaces as a
# ``NameError`` at import time, not a silent type confusion in
# the rendered help text. ``name`` and ``handler`` are the two
# fields the dispatcher consults at runtime; ``description``
# is the user-facing one-liner rendered by ``format_help_text``.
SlashCommand = namedtuple("SlashCommand", [
    "name",
    "handler",
    "description",
])


# Catalog order = the order the commands appear in ``/help``
# output (and in the dispatcher's own internal routing
# tables). Group dispatcher-handled commands first so the
# "fast path" surface is up-front; worker-handled commands
# follow.
SUPPORTED_COMMANDS = (
    SlashCommand(
        name="help",
        handler="dispatcher",
        description="List available slash commands",
    ),
    SlashCommand(
        name="sandbox",
        handler="dispatcher",
        description="Spawn a per-PR sandbox pod (ttyd + sshd "
                    "terminals exposed at "
                    "/sandbox/<id>/; press Enter at the SSH "
                    "password prompt). Auto-destroys when the "
                    "terminal session closes; multiple "
                    "sandboxes can run concurrently",
    ),
    SlashCommand(
        name="review",
        handler="worker",
        description="Request a code review from the slave user "
                    "(identical to assigning a review request)",
    ),
    SlashCommand(
        name="squash",
        handler="worker",
        description="Squash all commits in this PR into a single "
                    "commit; the commit message is generated by "
                    "an agent from the cumulative diff",
    ),
    SlashCommand(
        name="rebase",
        handler="worker",
        description="Rebase this PR on top of the target branch; "
                    "conflicts are resolved by an agent",
    ),
    SlashCommand(
        name="drop",
        handler="worker",
        description="Remove the specified commits from this PR's "
                    "branch. Usage: ``/drop <sha1> [<sha2> ...]`` "
                    "(space-separated). The commits must all be "
                    "exclusive to the PR (not on the base "
                    "branch). Triggers a force-push; the local "
                    "branch rewrites non-cooperatively (conflicts "
                    "abort the drop)",
    ),
    SlashCommand(
        name="do",
        handler="worker",
        description="Re-read the PR's comment thread and execute "
                    "the instructions left there. The trigger "
                    "must be a reply to a specific inline review "
                    "comment",
    ),
)


def _validate_catalog():
    """Validate the catalog at import time.

    Any malformed entry (unknown ``handler`` value, duplicate
    ``name``) raises ``ValueError`` here rather than surfacing
    as a silent routing error at runtime. The set-of-names
    return value is unused by callers but the validation is
    still useful as a sanity check on the static catalog.
    """
    seen = set()
    for cmd in SUPPORTED_COMMANDS:
        if cmd.handler not in ("dispatcher", "worker"):
            raise ValueError(
                f"slash_commands: {cmd.name!r} has unknown "
                f"handler {cmd.handler!r} (expected 'dispatcher' "
                f"or 'worker')")
        if cmd.name in seen:
            raise ValueError(
                f"slash_commands: duplicate command name "
                f"{cmd.name!r}")
        seen.add(cmd.name)
    return seen


_validate_catalog()


def format_help_text():
    """Render the ``/help`` PR comment body.

    Returns the markdown body the dispatcher (``_handle_help``)
    posts as a PR comment in response to a ``/help``
    invocation. Single source of truth: the renderer lives here
    so a catalog edit propagates to the user-facing help text
    without a separate edit.

    The user-facing format intentionally omits the internal
    ``handler`` tag — a user who wants to know whether a
    command is fast or slow can read the catalog source or the
    command-handler template's docs. Surfacing the tag would
    expose the dispatcher/worker split as a user-visible
    concept, which is unnecessary for the supported-command
    catalog.
    """
    lines = ["**Supported slash commands:**", ""]
    for cmd in SUPPORTED_COMMANDS:
        lines.append(f"- `/{cmd.name}` — {cmd.description}")
    return "\n".join(lines)
