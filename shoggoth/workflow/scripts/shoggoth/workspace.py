"""Workspace materialization, shared by the worker checkout and the sandbox.

A ccws SOURCE SPACE is the directory that holds a project's
repositories (``WORKSPACE_SRC``, default ``/ccws/workspace/src``). Its
shape decides where a clone lands:

  * a PLAIN repository -- a standalone project repo, one repository and
    nothing else -- is cloned into a SUBDIRECTORY of the source space,
    which leaves the source space root free to carry a workspace list;
  * a WORKSPACE -- a repository carrying a repository list (``.repos``)
    -- takes the source space over: its own clone becomes the source
    space root, which is what makes the directory a wshandler source
    space (``wshandler is_source_space`` looks for the list there), and
    ``wshandler update`` fills the listed sub-repositories into
    subdirectories of it.

The worker (PR comment / Redmine task) and the sandbox pod materialize
through this module, so the two cannot drift apart.
"""
import os
import re
import shutil
import sys
import time

from .git import Git, WsHandler
from .util import die, log, run

SOURCE_SPACE_DEFAULT = "/ccws/workspace/src"
REPO_TYPE_PLAIN = "standalone"
REPO_TYPE_WORKSPACE = "ccws"


def source_space():
    """The ccws source space directory."""
    return os.environ.get("WORKSPACE_SRC") or SOURCE_SPACE_DEFAULT


def repo_dir_name(repo):
    """The directory name a plain repository gets in the source space.

    ``owner/name`` and a clone URL both reduce to ``name``.
    """
    name = repo.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    name = re.sub(r"[^A-Za-z0-9._-]", "-", name)
    return name or "repo"


def clear_dir(path):
    """Empty a directory, keeping the directory itself.

    The directory itself is kept: it is the source space root, and
    the ccws tooling looks for the repository list there.
    """
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
        return
    for entry in os.listdir(path):
        target = os.path.join(path, entry)
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target, ignore_errors=True)
        else:
            try:
                os.unlink(target)
            except OSError:
                pass


def materialize(url, dest, repo_type=REPO_TYPE_PLAIN, branch=None,
                url_rewrites=None, prefer_version=None, install_deps=True,
                label="checkout"):
    """Clone ``url`` into ``dest`` and materialize what belongs there.

    For a workspace the clone is the source space root and
    ``wshandler update`` fills the listed sub-repositories into
    subdirectories of it; the workspace's dependencies are then
    installed the way the worker installs them.

    ``url_rewrites`` are the ``wshandler -s`` rules that rewrite the
    repository list's URLs: the worker rewrites GitHub URLs to the
    in-cluster SSH remote (its git-cred sidecar provides an ssh-agent),
    the sandbox rewrites them to the in-cluster HTTP remote (its
    git-cred sidecar provides a credential cache keyed on that host).

    ``prefer_version`` is the version ``wshandler update`` prefers over
    the one the repository list pins.

    Returns the path of the cloned repository.
    """
    parent = os.path.dirname(dest.rstrip("/"))
    if parent:
        os.makedirs(parent, exist_ok=True)
    if not os.path.isdir(dest):
        os.makedirs(dest, exist_ok=True)
    log(f"{label}: type={repo_type} url={url} dest={dest} branch={branch}")

    g = Git()
    if branch:
        result = g.clone(url, dest, branch=branch, check=False)
        if result.returncode != 0 and repo_type == REPO_TYPE_WORKSPACE:
            log(f"{label}: branch {branch!r} not found, "
                "cloning the default branch")
            result = g.clone(url, dest, check=False)
    else:
        result = g.clone(url, dest, check=False)
    if result.returncode != 0:
        die(f"git clone failed for {url}: {result.stderr}")

    if repo_type == REPO_TYPE_WORKSPACE:
        update_workspace(dest, url_rewrites=url_rewrites,
                         prefer_version=prefer_version,
                         install_deps=install_deps, label=label)
    return dest


def update_workspace(root, url_rewrites=None, prefer_version=None,
                     install_deps=True, label="checkout"):
    """Fill a workspace with its listed sub-repositories and dependencies.

    ``wshandler update`` clones the repositories the workspace's list
    names into subdirectories of ``root``. The workspace's own
    dependencies are then installed the way the worker installs them.
    """
    args = []
    for rewrite in (url_rewrites or []):
        args += ["-s", rewrite]
    args += ["-p", "shallow"]
    if prefer_version:
        args += ["-P", prefer_version]
    args.append("update")
    started = time.time()
    WsHandler(root).run(args)
    log(f"{label}: wshandler update took {int(time.time() - started)}s")

    log(f"{label}: post-update repository list:")
    WsHandler(root).status(quiet=True)

    if not install_deps:
        return

    apt_update = run(["sudo", "-S", "apt", "update"], check=False,
                     input="ccws\n")
    if apt_update.stdout:
        print(apt_update.stdout, file=sys.stderr, flush=True)
    if apt_update.stderr:
        print(apt_update.stderr, file=sys.stderr, flush=True)
    if apt_update.returncode != 0:
        die(f"apt update failed with exit code {apt_update.returncode}: "
            f"{(apt_update.stderr or '').strip()[-500:]}")

    dep_install = run(["make", "dep_install"], cwd="/ccws",
                      input="ccws\n", check=False)
    if dep_install.stdout:
        print(dep_install.stdout, file=sys.stderr, flush=True)
    if dep_install.stderr:
        print(dep_install.stderr, file=sys.stderr, flush=True)
    if dep_install.returncode != 0:
        die(f"make dep_install failed with exit code "
            f"{dep_install.returncode}: "
            f"{(dep_install.stderr or '').strip()[-500:]}")


def apply_pr_ref(repo_dir, ref, pr_number=None, label="checkout"):
    """Check the PR's head ref out in ``repo_dir``.

    The ref is fetched from the repository's own origin: the branch
    first, then the platform's ``pull/<n>/head`` ref, which is where a
    fork's PR branch is reachable from the base repository. The result
    is checked out as a local ``pr-<n>`` branch, so the user can commit
    on it in the sandbox terminal.
    """
    safe = re.sub(r"[^A-Za-z0-9_./-]", "-", ref or "")
    if not safe or safe.startswith("-") or ".." in safe:
        die(f"{label}: refusing unsafe ref {ref!r}")
    if pr_number:
        local_ref = f"refs/remotes/origin/pr-{pr_number}"
    else:
        local_ref = f"refs/remotes/origin/{safe}"
    g = Git(repo_dir)
    result = g.fetch(remote="origin", ref=ref, local_ref=local_ref,
                     check=False)
    if result.returncode != 0 and pr_number:
        log(f"{label}: origin/{ref} not found, "
            f"trying pull/{pr_number}/head")
        result = g.fetch(remote="origin", ref=f"pull/{pr_number}/head",
                         local_ref=local_ref, check=False)
    if result.returncode != 0:
        die(f"{label}: cannot fetch the PR's head ref {ref!r}: "
            f"{result.stderr}")

    branch = f"pr-{pr_number}" if pr_number else safe
    checkout = g.raw(
        "-c", "advice.detachedHead=false", "checkout",
        "-B", branch, local_ref, check=False)
    if checkout.returncode != 0:
        die(f"{label}: cannot check out {local_ref}: {checkout.stderr}")
    log(f"{label}: checked out {branch} at {local_ref}")
    return branch
