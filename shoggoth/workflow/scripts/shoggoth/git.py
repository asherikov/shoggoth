"""Module split out of shoggoth.py; see package __init__."""
import os
import time

from .util import die, log, run

class Git:
    """Thin wrapper around ``git`` CLI invocations used by the
    workflow script.

    The script used to spawn ``git`` via ``run([...])`` at every
    call site (55 of them at the time of writing). That format
    repeatedly re-states the same three facts — which ``git``,
    which ``-C <dir>``, which subcommand — and there is no single
    place to add a global guard (e.g. ``GIT_TERMINAL_PROMPT=0`` for
    CI, ``--no-pager`` for log hygiene, future per-invocation
    metrics). This wrapper centralises the ``git`` argv and lets
    each call site speak in domain terms (``add_all``, ``commit``,
    ``push_force_with_lease``) instead of ``run(["git", "-C",
    self.repo_dir, "commit", "-m", msg], check=False)``.

    Scope and contract:

    * Constructor takes an optional ``repo_dir``. When ``None``
      (e.g. for ``clone`` / ``ls-remote``), the wrapper does not
      emit ``-C`` and the caller is expected to pass the working
      directory via the underlying ``run`` (or to be running from
      the right cwd already).
    * Methods return a ``subprocess.CompletedProcess``-like object
      from ``run()``, so callers keep reading ``.stdout``,
      ``.stderr``, ``.returncode`` (and ``check=False`` overrides
      keep working unchanged).
    * ``check=None`` (the default) means "use ``run()``'s default"
      (raises on non-zero rc); passing ``check=False`` preserves
      the original callsite's error-handling.
    * ``env=`` (where the caller really does need it — only the
      rebase path wants ``GIT_EDITOR=true``) is forwarded to
      ``run()``; ``-c <key>=<value>`` git-level config is folded
      into the relevant convenience methods (``clone``,
      ``push_with_credential_helper``) rather than added as a
      generic knob.
    """

    def __init__(self, repo_dir=None):
        self.repo_dir = repo_dir

    # ----- internal argv builder ------------------------------------

    def _argv(self, *args):
        if self.repo_dir is None:
            return ["git", *args]
        return ["git", "-C", self.repo_dir, *args]

    def _invoke(self, *args, env=None, check=None):
        kwargs = {}
        if env is not None:
            kwargs["env"] = env
        if check is not None:
            kwargs["check"] = check
        return run(self._argv(*args), **kwargs)

    # ----- generic escape hatch -------------------------------------

    def raw(self, *args, env=None, check=None):
        """Run an arbitrary ``git`` subcommand.

        Used by the few callsites whose argv shape does not recur
        elsewhere (e.g. ``for-each-ref``, ``tag --list``). New
        callers should prefer the named methods below; this
        escape hatch is here so the wrapper does not block adding
        one-off invocations during the cutover.
        """
        return self._invoke(*args, env=env, check=check)

    # ----- remote / clone / fetch -----------------------------------

    def clone(self, url, dest, depth="1", branch=None,
              credential_helper=None, check=None):
        """``git clone [--depth 1] [--branch <b>] [--credential.helper=…]
        <url> <dest>``.

        ``credential_helper`` is the shoggoth-sidecar helper string
        (e.g. ``"!qwen-redis-cred-helper.sh"``). It is rendered as
        ``-c credential.helper=<helper>`` so the URL never has to
        be embedded in the credential helper's stdin — that
        pattern is what the ``github-mirror-sync`` flow uses for
        sub-repo pushes.
        """
        argv = ["git"]
        if credential_helper:
            argv += ["-c", f"credential.helper={credential_helper}"]
        argv += ["clone", "--depth", str(depth)]
        if branch:
            argv += ["--branch", branch]
        argv += [url, dest]
        kwargs = {}
        if check is not None:
            kwargs["check"] = check
        return run(argv, **kwargs)

    def fetch(self, remote="origin", ref=None, local_ref=None,
              unshallow=False, tags=False, check=None):
        """``git fetch <remote> [<ref>[:<local_ref>]] [--unshallow]
        [--tags]``.

        With the defaults — ``unshallow=False``, ``ref=None``,
        ``tags=False`` — the call reduces to ``git fetch origin``,
        the historical invocation. ``_unshallow`` passes
        ``unshallow=True`` (which adds ``--unshallow``) and the
        tagging flows pass ``tags=True`` (which adds ``--tags``).
        """
        argv = ["fetch"]
        if unshallow:
            argv.append("--unshallow")
        if tags:
            argv.append("--tags")
        argv.append(remote)
        if ref and local_ref is not None:
            argv.append(f"{ref}:{local_ref}")
        elif ref:
            argv.append(ref)
        return self._invoke(*argv, check=check)

    def fetch_url_refspecs(self, url, refspec, depth=None, tags=False,
                            credential_helper=None, quiet=False, check=None):
        """``git fetch [--quiet] [--tags] [--depth <n>] [--credential.
        helper=…] <url> <refspec>``.

        Used by the ``github-mirror-sync`` flow (``GithubMirrorSync.
        _sync_existing_repo``) to mirror an upstream branch into a
        local clone — the URL is not the local ``origin`` (which
        may not exist yet), so this gets its own helper rather than
        overloading ``fetch``. ``self.repo_dir`` still routes the
        fetch into the local clone's ``.git``.
        """
        if credential_helper:
            argv = self._argv("-c", f"credential.helper={credential_helper}",
                                "fetch")
        else:
            argv = self._argv("fetch")
        if quiet:
            argv.append("--quiet")
        if tags:
            argv.append("--tags")
        if depth is not None:
            argv += ["--depth", str(depth)]
        argv += [url, refspec]
        return run(argv, check=check)

    # ----- working-tree mutations ----------------------------------

    def status_porcelain(self, check=None):
        return self._invoke("status", "--porcelain", check=check)

    def diff(self, *args, check=None):
        """``git diff <args>`` — caller passes spec and any flags.

        Use ``diff_cached`` (or pass ``--cached`` yourself) for
        staged diffs; ``diff_name_only`` is the dedicated helper
        for ``--diff-filter=U`` lookups in the rebase loop.
        """
        return self._invoke("diff", *args, check=check)

    def diff_cached_name_only(self, check=None):
        """``git diff --cached --name-only``.

        Used by ``_commit_only`` (standalone + ccws) and the
        squash handler to detect staged changes after a soft
        reset. Trimming the argv into a dedicated method means
        a future change to the staging query (e.g. ``-z`` for NUL
        safety) lands in one place.
        """
        return self._invoke("diff", "--cached", "--name-only", check=check)

    def diff_name_only_filter(self, filter_flag, check=None):
        """``git diff --name-only --diff-filter=<flag>``.

        ``filter_flag`` is one of ``U`` (unmerged), ``A``
        (added), ``M`` (modified), etc. Used by the rebase loop
        to enumerate files with conflict markers still present.
        """
        return self._invoke("diff", "--name-only",
                             "--diff-filter", filter_flag, check=check)

    def add_all(self, check=None):
        return self._invoke("add", "-A", check=check)

    def add(self, *paths, check=None):
        return self._invoke("add", *paths, check=check)

    def reset_soft(self, target, check=None):
        return self._invoke("reset", "--soft", target, check=check)

    def commit(self, message, check=None):
        """``git commit -m <message>``.

        ``message`` is passed as a single argv element to avoid
        shell-quoting pitfalls; git handles multi-line commit
        messages natively so ``-m `` with a literal newline is
        fine and we do not need ``-F <tmpfile>``.
        """
        return self._invoke("commit", "-m", message, check=check)

    def checkout_new_b(self, branch, check=None):
        """``git checkout -B <branch>`` — force-create/reset a branch."""
        return self._invoke("checkout", "-B", branch, check=check)

    # ----- refs ----------------------------------------------------

    def rev_parse(self, ref, check=None):
        """``git rev-parse <ref>`` — resolve a ref (no ``--verify``)."""
        return self._invoke("rev-parse", ref, check=check)

    def rev_parse_verify(self, ref, check=None):
        """``git rev-parse --verify <ref>`` — fail loudly on missing.
        """
        return self._invoke("rev-parse", "--verify", ref, check=check)

    def rev_parse_verify_quiet(self, ref, check=None):
        """``git rev-parse --verify -q <ref>`` — quiet variant (rc only).
        """
        return self._invoke("rev-parse", "--verify", "-q", ref, check=check)

    def rev_parse_git_path(self, marker, check=None):
        """``git rev-parse --git-path <marker>`` — print the path
        of a marker under the git dir (``rebase-merge`` /
        ``rebase-apply``). Used by ``_rebase_in_progress``.
        """
        return self._invoke("rev-parse", "--git-path", marker, check=check)

    def branch(self, name, ref, force=False, check=None):
        """``git branch [-f] <name> <ref>`` — create or reset a branch.
        """
        argv = ["branch"]
        if force:
            argv.append("-f")
        argv += [name, ref]
        return self._invoke(*argv, check=check)

    # ----- worktrees ------------------------------------------------

    def worktree_add(self, wt_path, ref, detach=False, check=None):
        """``git worktree add [--detach] <wt_path> <ref>``.

        ``detach=True`` is the path ``_apply_pr_to_worktree`` uses
        so the worktree does not auto-checkout a fresh branch on
        top of the PR's base SHA.
        """
        argv = ["worktree", "add"]
        if detach:
            argv.append("--detach")
        argv += [wt_path, ref]
        return self._invoke(*argv, check=check)

    def worktree_remove(self, wt_path, force=False, check=None):
        argv = ["worktree", "remove"]
        if force:
            argv.append("--force")
        argv.append(wt_path)
        return self._invoke(*argv, check=check)

    def worktree_prune(self, check=None):
        return self._invoke("worktree", "prune", check=check)

    # ----- apply (patch via stdin) ----------------------------------

    def apply_stdin(self, patch_text, three_way=False,
                     whitespace_fix=True, recount=False, check=None):
        """``git apply [--whitespace=fix] [--recount] [--3way] -``.

        The patch is passed via stdin (``-``) rather than as a
        argv path because the workflow script computes the patch
        text in-memory (it may have been re-routed from the Gitea
        ``.diff`` fallback or rewritten by ``_rewrite_patch_paths``
        for ccws sub-repos). ``whitespace_fix`` defaults to True
        to match the existing flow's
        ``--whitespace=fix`` arg.
        """
        argv = ["apply"]
        if whitespace_fix:
            argv.append("--whitespace=fix")
        if recount:
            argv.append("--recount")
        if three_way:
            argv.append("--3way")
        argv.append("-")
        kwargs = {"input": patch_text}
        if check is not None:
            kwargs["check"] = check
        return run(self._argv(*argv), **kwargs)

    # ----- rebase (needs GIT_EDITOR=true) ---------------------------

    def rebase(self, upstream, env=None, check=None):
        """``git rebase <upstream>`` — caller-supplied ``env`` must
        contain ``GIT_EDITOR=true`` (or equivalent) to avoid the
        command hanging on a commit-message prompt.
        """
        return self._invoke("rebase", upstream, env=env, check=check)

    def rebase_abort(self, env=None, check=None):
        return self._invoke("rebase", "--abort", env=env, check=check)

    def rebase_continue(self, env=None, check=None):
        return self._invoke("rebase", "--continue", env=env, check=check)

    def rebase_skip(self, env=None, check=None):
        return self._invoke("rebase", "--skip", env=env, check=check)

    # ----- push ----------------------------------------------------

    def push(self, remote, *refs, force_with_lease=False,
             set_upstream=False, credential_helper=None,
             dry_run=False, check=None):
        """``git push [--force-with-lease] [-u] [--dry-run] <remote> <refs…>``
        (or ``-c credential.helper=… git push …`` when a helper
        is supplied). ``force_with_lease`` matches the existing
        flow's refusal-to-overwrite-an-unseen-divergence policy;
        do not add a raw ``--force`` here — use ``force_with_lease``
        or reach for ``raw()`` so the choice is visible at the
        call site.

        ``refs`` is variadic: the standalone/squash/rebase flows
        pass a single branch (``push("origin", pr_branch,
        force_with_lease=True)``); the github-mirror-sync flow
        passes the full list of changed branches (``push("origin",
        *changed_branches, credential_helper=cred_helper)``).
        ``--tags`` is not handled here — tag pushes are rare and
        sufficiently different that they go through ``raw()``.

        ``dry_run`` adds ``--dry-run`` so the push is exercised
        end-to-end (server talks back, lease check runs) without
        mutating the remote or the local branch. Used by
        ``_preflight_push`` to fail fast before ``/squash`` /
        ``/rebase`` commit to a destructive local state whose
        push the server would reject anyway.
        """
        if credential_helper:
            argv = self._argv("-c", f"credential.helper={credential_helper}",
                                "push")
        else:
            argv = self._argv("push")
        if dry_run:
            argv.append("--dry-run")
        if force_with_lease:
            argv.append("--force-with-lease")
        if set_upstream:
            argv.append("-u")
        argv.append(remote)
        argv.extend(refs)
        return run(argv, check=check)

    # ----- merge-base ----------------------------------------------

    def merge_base_is_ancestor(self, ancestor, descendant, check=None):
        """``git merge-base --is-ancestor <a> <b>`` — returns
        ``.returncode == 0`` iff ``a`` is an ancestor of ``b``.
        """
        return self._invoke(
            "merge-base", "--is-ancestor", ancestor, descendant,
            check=check)

    # ----- ls-remote (no -C, runs against a URL) -------------------

    def ls_remote(self, url, *refs, env=None, check=None):
        """``git ls-remote [<env SSH vars>] <url> [<refs>…]``.

        ``ls-remote`` does not take ``-C`` (it operates against a
        URL, not a local checkout), so this method never uses
        ``self.repo_dir``. ``env`` carries SSH-side vars
        (``SSH_AUTH_SOCK``, ``GIT_SSH_COMMAND``) for the ssh
        clone path.
        """
        return self._invoke("ls-remote", url, *refs, env=env, check=check)
class GitCredentials:
    """Readiness probe + diagnostics for the git-cred k8s sidecar.
    """

    READY_PATH = "/shoggoth/git-cred/ready"
    READY_TIMEOUT = 30
    POLL_INTERVAL = 0.5

    def _wait_for_path(self, path, timeout=None):
        deadline = time.time() + (timeout if timeout is not None
                                  else self.READY_TIMEOUT)
        while time.time() < deadline:
            try:
                os.stat(path)
                return True
            except (FileNotFoundError, PermissionError):
                time.sleep(self.POLL_INTERVAL)
        return False

    def _probe_message(self, path):
        try:
            os.stat(path)
            return f"{path} exists but _wait_for_path returned False " \
                f"(timed out after {self.READY_TIMEOUT}s)"
        except FileNotFoundError:
            return (f"{path} does not exist after waiting "
                    f"{self.READY_TIMEOUT}s ")
        except PermissionError as e:
            return (f"{path} is not accessible (permissions) from the main container: {e}")
        except OSError as e:
            return f"{path}: {e}"

    def wait_for_diag(self, label):
        """Wait on the sidecar's readiness flag and return
        ``(ready, probe_message)``. On timeout, ``probe_message`` is the
        diagnostic string for the failure mode (see ``_probe_message``).
        On success, ``probe_message`` is None. ``label`` is a short
        prefix used in the log lines (e.g. ``"git-http"``,
        ``"git-ssh"``, ``"workflow"``).
        """
        log(f"[{label}]: waiting up to {self.READY_TIMEOUT}s for "
            f"git-cred ready flag at {self.READY_PATH}")
        if self._wait_for_path(self.READY_PATH):
            log(f"[{label}]: git-cred ready flag present; daemons observable")
            return True, None
        log(f"[{label}]: timed out, probing errno")
        return False, self._probe_message(self.READY_PATH)

    def wait_until_ready(self):
        """Wait on the sidecar's single readiness flag. On timeout,
        die with a diagnostic so a run without shell access to the
        pod can self-diagnose.
        """
        ready, _ = self.wait_for_diag("workflow")
        if not ready:
            die(
                f"git-cred sidecar did not touch {self.READY_PATH} within "
                f"{self.READY_TIMEOUT}s "
                "(check 'kubectl logs <pod> -c git-cred' for the FATAL line; "
                "verify the pod has an initContainer named init-git-cred-perms "
                "and a sidecar named git-cred)"
            )
        log("git-cred ready")

    def log_listing(self, label):
        run(["whoami"], check=False)
        run(["ls", "-lan", "/shoggoth/git-cred"], check=False)

        allow_list = [
            "SSH_AUTH_SOCK",
            "GITEA_SERVER_URL", "GITEA_GIT_URL", "GITEA_INSTANCE_SSH_HOST",
            "REDMINE_SERVER",
            "SHOGGOTH_DOMAIN", "SHOGGOTH_NAMESPACE", "SHOGGOTH_GITHUB_ORG",
            "OPENBAO_ADDR",
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "ARGO_WEBHOOK_TOKEN",
        ]
        log(f"[{label}]: env (allow-list, {len(allow_list)} vars)")
        for name in allow_list:
            value = os.environ.get(name, "")
            log(f"[{label}]: {name}={'<set>' if value else '<unset>'} "
                f"(len={len(value)})")
class WsHandler:
    """Wrapper around the wshandler CLI (``wshandler -r <repo_dir> <args>``).
    """

    def __init__(self, repo_dir):
        self.repo_dir = repo_dir

    def _cmd(self, args):
        """Build the ``wshandler -r <repo_dir> <args>`` command list."""
        return ["wshandler", "-r", self.repo_dir] + list(args)

    def status(self, quiet=False):
        args = ["-q", "status"] if quiet else ["status"]
        return run(self._cmd(args))

    def parse_modified_repos(self, status_output):
        """Extract modified sub-repo names from ``wshandler status`` output.
        """
        modified_repos = []
        for line in status_output.strip().splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            repo_name = parts[0]
            flags = parts[3]
            if "M" in flags:
                modified_repos.append(repo_name)
        return modified_repos

    _QUOTE_TRANSLATION = str.maketrans({
        "'": "\u2019",
        '"': "\u201d",
        "`": "\u2018",
    })

    def commit(self, msg, **kwargs):
        """Send ``wshandler commit <msg>`` with the message sanitized
        for the single-quoted bash string wshandler builds around it.
        """
        if msg is not None:
            msg = msg.translate(self._QUOTE_TRANSLATION)
        return run(self._cmd(["commit", msg]), **kwargs)

    def branch_new(self, name, **kwargs):
        return run(self._cmd(["branch", "new", name]), **kwargs)

    def push(self, repos, **kwargs):
        return run(self._cmd(["-p", "version", "push"] + list(repos)), **kwargs)
