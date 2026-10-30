#!/usr/bin/env python3
"""sandbox — Python entrypoints for the sandbox pod's containers.

Replaces the four inline shell scripts that previously lived as
module-level constants (SANDBOX_WAIT_BOOTSTRAP_SCRIPT,
SANDBOX_INIT_GITCRED_PERMS_SCRIPT, SANDBOX_TTYD_SCRIPT,
SANDBOX_CLEANUP_SCRIPT) in shoggoth/workflow/scripts/shoggoth/commands/dispatch.py.

Subcommands
-----------
  wait-bootstrap   init container (tools image): wait for the K8s
                   externalsecrets the git-cred sidecar reads
                   (gitea-slave-token, ssh-slave-private-key) to
                   reach the Ready condition.
  init-perms       init container (tools image): chown+chmod the
                   git-cred emptyDir before the sidecar starts.
  sandbox          main container (slave image): the whole
                   sandbox in one process tree -- wait for the
                   git-cred sidecar's ready flag, materialize the
                   project's ccws workspace (a plain repository goes
                   into a subdirectory of the source space, a ccws
                   workspace takes the source space over), check out
                   the PR's head ref, then run the SSH server and the
                   web terminal side by side and supervise them.
  cleanup          sidecar (tools image): watch the sandbox
                   container's state, kubectl delete pod once it is
                   dead.

One container, not two
----------------------
The main container used to be a ``ttyd`` container and an ``sshd``
container, two entries in the same pod, started side by side by
the kubelet. They were split because sshd was the root process of
the pod: it bound the privileged port 22 and did OpenSSH's
privilege separation, while the web terminal had to be the
unprivileged ``ccws`` user. Neither server needs that any more --
both run as ``ccws``, both on unprivileged ports, and the
container itself runs as ``ccws`` too.

The split cost a lot. The two containers had to agree on when the
workspace was ready, which needed a shared volume and a pair of
flag files; and because pod Ready is a conjunction over every
container, a dead container took the pod out of Ready, which made
the *surviving* one unreachable too -- a healthy sshd serving
sessions that nobody could reach, and a web terminal that looked
fine in ``kubectl get pod`` while the sandbox was dead.

One container, one process tree, one sequence: materialize, then
serve. No handshake, no shared state volume, and nothing to go
out of Ready but the container itself.

Mounting
--------
The sandbox pod's spec mounts /shoggoth/workflow-scripts from the
host (the workflow-scripts:workflow/scripts entry in K3S_HOST_PATHS
in the Makefile rsyncs ./workflow/scripts/ to
/var/lib/rancher/k3s/storage/${INSTANCE}/workflow-scripts/ on the
cluster node), so the same absolute path is visible to every
container in the pod after `make sync`. The init containers and
the cleanup sidecar use the tools image (alpine) which doesn't
bake the workflow scripts in; the hostPath mount is how they
access this module. The sandbox container uses the slave image
(noble) which bakes the workflow scripts in at a different path
(/shoggoth/workflow/scripts/) for historical reasons; the
hostPath mount is still added to the sandbox container for symmetry
and to make this file the authoritative copy of the lifecycle
logic.
"""
import argparse
import json
import os
import pwd
import shlex
import signal
import subprocess
import sys
import time

# External-secrets the git-cred sidecar reads; the pod's RBAC
# grants the sandbox SA `get` on these.
WAIT_BOOTSTRAP_SECRETS = ("gitea-slave-token", "ssh-slave-private-key")
WAIT_BOOTSTRAP_TIMEOUT_S = 600
WAIT_BOOTSTRAP_POLL_S = 10

# Path the git-cred sidecar touch()es after both ssh-agent and
# git-credential-cache--daemon are bound and have accepted their
# first identity / store request (with a 3 s grace). The
# sandbox container polls this single file rather than each
# daemon's socket (matches the slave-term pattern and the
# workflow _wait_for_git_cred helper).
SANDBOX_CRED_SOCK = "/shoggoth/git-cred/git_credential_sock"
SANDBOX_READY_FLAG = "/shoggoth/git-cred/ready"

SANDBOX_CRED_READY_TIMEOUT_S = 90


def _env_int(name, default):
    """An int from the environment, or ``default`` if it is unset
    or not a number."""
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


# The dispatcher's budget for the sandbox pod to become Ready,
# passed into the pod by the dispatcher (see ``_apply_sandbox_pod``
# in shoggoth/commands/dispatch.py). The sandbox pod's own
# lifetimes are derived from it, so the two sides cannot drift
# apart: the cleanup sidecar's not-ready watchdog has to outlast
# the dispatcher's wait, or the sandbox is torn down while the
# dispatcher is still waiting for it -- and the dispatcher's
# post-mortem then finds no pod left to describe.
DISPATCHER_READY_TIMEOUT_S = _env_int("SHOGGOTH_READY_TIMEOUT_S", 900)
# How much longer the sandbox pod may live than the dispatcher's
# wait: enough for the dispatcher to run its post-wait probes
# against a pod that is still there.
SANDBOX_LIFETIME_MARGIN_S = 300

# The account the user's sessions run as. The container itself runs
# as this user too: the workspace, the web terminal and the SSH
# server (and every session it opens) all belong to it.
SANDBOX_USER = "ccws"
SANDBOX_HOME = "/home/ccws"

# The name of the pod's main container. The dispatcher knows it
# (it writes the pod spec) and passes it to the cleanup sidecar
# through the environment, so the sidecar watches the container
# the dispatcher actually created.
SANDBOX_CONTAINER = os.environ.get("SANDBOX_CONTAINER", "sandbox")

# The web terminal's port inside the container. Deliberately
# unprivileged: the terminal runs as the unprivileged ``ccws``
# user, and a root -> non-root uid transition clears the
# capability set, so a process that has dropped to ``ccws``
# cannot bind a port below 1024 any more -- not even with
# CAP_NET_BIND_SERVICE, which is gone with the rest of the set.
# The Service maps the user-facing port 80 onto this one, so the
# user still reaches the terminal on port 80.
SANDBOX_TTYD_PORT = _env_int("SANDBOX_TTYD_PORT", 8080)

# The SSH server's port inside the container. Unprivileged for the
# same reason as the web terminal's (see SANDBOX_TTYD_PORT): the
# daemon runs as the unprivileged ``ccws`` user, so it has no
# capabilities left and cannot bind a port below 1024. The Service
# maps the user-facing port 22 onto this one, so the user's
# ``ssh ccws@<sandbox-host>`` is unchanged.
SANDBOX_SSHD_PORT = _env_int("SANDBOX_SSHD_PORT", 2222)

# The SSH server's configuration and its host key. The config is
# the image's (shoggoth/dockerfiles/slave writes it); the host key
# is generated per sandbox, in the account's own directory, because
# an unprivileged daemon cannot read a root-owned host key and two
# sandboxes must not share one.
#
# The pid file joins the host key in the account's own directory: an
# unprivileged daemon cannot write into /run.
SANDBOX_SSHD_CONFIG = "/etc/ssh/sshd_config"
SANDBOX_SSHD_HOST_KEY = "/home/ccws/.ssh/ssh_host_ed25519_key"
SANDBOX_SSHD_PID_FILE = "/home/ccws/.ssh/sshd.pid"

# How long the supervisor waits for the second server to stop
# after the first one exited, before it kills it.
SANDBOX_STOP_GRACE_S = 10

# The shell's startup file marker. The snippet below is appended
# to the ccws user's ~/.bashrc, so every interactive shell -- the
# web terminal's and the SSH session's alike -- starts in the
# materialized workspace instead of the account's home directory.
SANDBOX_BASHRC_MARKER = "# ccws sandbox: start in the materialized workspace"

# Note: the slave image's sshd_config locks the daemon to the
# ``ccws`` user (``AllowUsers ccws`` in shoggoth/dockerfiles/slave)
# and names the port the sandbox's sshd binds (SANDBOX_SSHD_PORT
# above); the dispatcher's per-sandbox Service exposes the
# user-facing port 22 and points it at that port. These literals
# are kept in sync by hand -- no parametrization -- so the
# constants that used to live here (SLAVE_USER, SLAVE_SSHD_PORT)
# were dead code (referenced only by one log line) and have been
# removed.

# Cleanup budgets (matching SANDBOX_CLEANUP_SCRIPT):
#   MAX_LIFETIME  hard cap so a script bug in the detection logic
#                 can't leak the pod past the workflow's
#                 activeDeadlineSeconds (14400 s = 4 h).
#   GRACE         hold the pod alive after the first termination
#                 signal long enough for the dispatcher's
#                 post-Ready diagnostics (`kubectl wait` +
#                 `kubectl describe` + per-container `kubectl
#                 logs` chain, typically a few seconds; the
#                 dispatcher's `_wait_sandbox_ready` refreshes
#                 its snapshot every 30 s, so the grace only has
#                 to cover the gap between two snapshots).
#   NOT_READY     sustained not-ready threshold for a sandbox
#                 container that accepted the TCP connection but
#                 never responded to the HTTP probe (kubelet's
#                 livenessProbe budget is 30 s once the web
#                 terminal is up, but the container is not ready at
#                 all while it materializes the workspace --
#                 minutes, not seconds -- so the threshold has to
#                 cover that too). It has to outlast the
#                 dispatcher's own wait (DISPATCHER_READY_TIMEOUT_S
#                 above), otherwise the cleanup reaps a merely-slow
#                 sandbox while the dispatcher is still waiting
#                 for it -- and the dispatcher's post-mortem
#                 then finds no pod to describe.
CLEANUP_MAX_LIFETIME_S = 14400
CLEANUP_GRACE_S = 180
CLEANUP_NOT_READY_BUDGET_S = (DISPATCHER_READY_TIMEOUT_S
                              + SANDBOX_LIFETIME_MARGIN_S)
CLEANUP_POLL_S = 10


def log(msg):
    """stderr log; the kubelet captures the init/main/cleanup
    container stderr into the workflow log via Argo's log
    archival, so this is the user-visible diagnostic channel."""
    print(f"sandbox: {msg}", file=sys.stderr, flush=True)


def die(msg):
    log(msg)
    sys.exit(1)


def run(args, **kwargs):
    """subprocess.run with check=True; the child's output is carried
    along with the failure.

    The failure text is what the sshd container hands to the
    dispatcher (through the repo-failed flag) and what ends up in
    the PR comment: a bare "Command '[...]' returned non-zero exit
    status 1" says nothing about what the command actually
    printed, and the printed text is the part that says what went
    wrong. So stdout/stderr are captured and attached to the
    exception, and stderr is echoed to this container's stderr so
    the container log keeps it too.
    """
    kwargs.pop("capture_output", None)
    kwargs.pop("text", None)
    proc = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, end="", flush=True)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, args, output=proc.stdout, stderr=proc.stderr)
    return proc


def wait_bootstrap():
    """Init container: poll for externalsecret Ready state.

    Replaces SANDBOX_WAIT_BOOTSTRAP_SCRIPT. The original used
    `kubectl get ... -o json | jq -e '...' >/dev/null 2>&1` in a
    bash for-loop; the Python version reads the JSON directly
    and is the same code path with no jq subprocess invocations.
    """
    namespace = os.environ["SHOGGOTH_NAMESPACE"]
    for secret_name in WAIT_BOOTSTRAP_SECRETS:
        log(f"wait-bootstrap: waiting for externalsecret/{secret_name} "
            f"Ready in namespace {namespace}...")
        for i in range(1,
                       (WAIT_BOOTSTRAP_TIMEOUT_S // WAIT_BOOTSTRAP_POLL_S) + 1):
            try:
                proc = run(
                    ["kubectl", "get", "externalsecret", secret_name,
                     "-n", namespace, "-o", "json"],
                    capture_output=True, text=True,
                )
                es = json.loads(proc.stdout)
                conditions = es.get("status", {}).get("conditions", [])
                if any(c.get("type") == "Ready" and c.get("status") == "True"
                       for c in conditions):
                    log(f"wait-bootstrap: externalsecret/{secret_name} "
                        f"Ready (after {i * WAIT_BOOTSTRAP_POLL_S}s)")
                    break
            except (subprocess.CalledProcessError, json.JSONDecodeError,
                    KeyError) as e:
                log(f"wait-bootstrap: get externalsecret/{secret_name} "
                    f"failed: {e}; retrying")
            time.sleep(WAIT_BOOTSTRAP_POLL_S)
        else:
            die(f"wait-bootstrap: externalsecret/{secret_name} not Ready "
                f"after {WAIT_BOOTSTRAP_TIMEOUT_S}s")
    log("wait-bootstrap: all K8s Secrets Ready")


def init_perms():
    """Init container: chown+chmod the git-cred emptyDir.

    Replaces SANDBOX_INIT_GITCRED_PERMS_SCRIPT. The git-cred
    sidecar runs as uid 1000 (securityContext.runAsUser) and
    needs to chmod the mountpoint to 0700 so git-credential-cache
    accepts the directory (it refuses any mode looser than 0700
    with "permissions on your socket directory are too loose").
    uid 1000 cannot chmod a root-owned directory, so this runs
    as root (the init container's default user is root on the
    tools image) before the sidecar starts. The sidecar
    re-applies chmod 0700 defensively, which is then a no-op
    because the directory is now owned by uid 1000.
    """
    git_cred_dir = "/shoggoth/git-cred"
    os.chown(git_cred_dir, 1000, 1000)
    os.chmod(git_cred_dir, 0o700)
    log("init-perms: /shoggoth/git-cred chown 1000:1000 chmod 0700")


def _wait_for_git_cred():
    """Block until the git-cred sidecar's ready flag is set.

    Matches the slave-term `term.command` pattern and the workflow
    `_wait_for_git_cred` helper:
    poll a single sentinel file the sidecar touch()es after
    ssh-agent and git-credential-cache--daemon are both bound.
    90 s budget matches the kubelet readinessProbe failureThreshold
    (60) plus headroom for a freshly-spawned gitea-ssh pod.
    """
    for i in range(SANDBOX_CRED_READY_TIMEOUT_S):
        if os.path.exists(SANDBOX_READY_FLAG):
            log(f"sandbox: git-cred ready after {i}s")
            return
        time.sleep(1)
    die(f"sandbox: git-cred not ready after "
        f"{SANDBOX_CRED_READY_TIMEOUT_S}s -- "
        f"check 'kubectl logs <pod> -c git-cred'")


def _clone_url(repo):
    """Build the in-pod HTTP clone URL for the PR's repo.

    Use the user-facing form http://git.${SHOGGOTH_DOMAIN}/<repo>.git
    when the pod's DNS is wired (SHOGGOTH_DNS_IP set, meaning the
    pod's dnsConfig carries the custom CoreDNS at
    params.shoggoth_dns_ip that rewrites *.${SHOGGOTH_DOMAIN} ->
    *.${SHOGGOTH_NAMESPACE}.svc.cluster.local) so the URL host
    matches the credential cache key the sidecar populates
    (`host=git.${SHOGGOTH_DOMAIN}`). Fall back to the short
    in-cluster form http://git.${SHOGGOTH_NAMESPACE}/<repo>.git
    on test/dev runs where the custom DNS is not wired (the
    kubelet's resolv.conf search path resolves the short form
    without the CoreDNS rewrite).
    """
    domain = os.environ.get("SHOGGOTH_DOMAIN", "")
    namespace = os.environ.get("SHOGGOTH_NAMESPACE", "")
    if domain and os.environ.get("SHOGGOTH_DNS_IP"):
        return f"http://git.{domain}/{repo}.git"
    if domain or namespace:
        return f"http://git.{namespace or domain}/{repo}.git"
    die("sandbox: neither SHOGGOTH_DOMAIN nor SHOGGOTH_NAMESPACE is set; "
        "cannot build clone URL")


def _workspace_url_rewrites():
    """The URL rewrites for the workspace's repository list.

    A ccws workspace's list names GitHub URLs; the sandbox's git-cred
    sidecar provides a credential cache keyed on the in-cluster HTTP
    host, so the list's URLs are rewritten to that host. (The worker
    rewrites them to the in-cluster SSH remote instead -- its git-cred
    sidecar provides an ssh-agent.)
    """
    domain = os.environ.get("SHOGGOTH_DOMAIN", "")
    org = os.environ.get("SHOGGOTH_GITHUB_ORG", "")
    if not domain or not org:
        return []
    org_re = org.replace(".", r"\.")
    return [
        f"s|https://github.com/{org_re}|http://git.{domain}/{org_re}|g",
        f"s|git@github.com:{org_re}|http://git.{domain}/{org_re}|g",
        f"s|ssh://git@github.com/{org_re}|http://git.{domain}/{org_re}|g",
        f"s|git+ssh://git@github.com/{org_re}|http://git.{domain}/{org_re}|g",
    ]


def _drop_privileges():
    """Become the unprivileged ``ccws`` user in this process.

    The container runs as ``ccws`` already (the pod's securityContext
    says so), so this is normally a no-op -- it is here for the
    case where the container was started as root, and it also fixes
    the environment up, so the children see the account's home and
    name whichever way the container was started.

    Everything the *user* touches must not be. The workspace has
    to belong to the user whose session opens it (git refuses to
    operate in a repository owned by somebody else -- the
    "dubious ownership" guard -- and the user has to be able to
    write in it), and the web terminal IS the user's shell.

    Dropping the uid clears the capability set: on a root ->
    non-root transition the kernel clears the effective set and
    reduces the permitted set to the intersection of the
    inheritable set and the bounding set, and the container's
    inheritable set is empty. The dropped process therefore ends up
    with no capabilities at all -- which is what the unprivileged
    work wants, and why the web terminal and the SSH server listen
    on unprivileged ports (see ``SANDBOX_TTYD_PORT`` and
    ``SANDBOX_SSHD_PORT``).
    """
    if os.getuid() == 0:
        entry = pwd.getpwnam(SANDBOX_USER)
        os.setgroups(os.getgrouplist(SANDBOX_USER, entry.pw_gid))
        os.setgid(entry.pw_gid)
        os.setuid(entry.pw_uid)
    os.environ["HOME"] = SANDBOX_HOME
    os.environ["USER"] = SANDBOX_USER
    os.environ["LOGNAME"] = SANDBOX_USER


def _bashrc_snippet(target):
    """The shell startup snippet that lands a session in ``target``."""
    quoted = shlex.quote(target)
    return f"""
{SANDBOX_BASHRC_MARKER}
# The workspace is materialized before the web terminal and the
# SSH server are started, so the directory is known here and
# baked into the snippet -- both terminals land in the same
# place.
if [ -d {quoted} ]; then
    cd {quoted} || true
fi
"""


def _failure_reason(exc):
    """A failure reason that carries the failing command's output.

    ``CalledProcessError.__str__`` is just "Command '[...]'
    returned non-zero exit status 1" -- the text the command
    printed is the part that says what actually went wrong, and it
    is the part the dispatcher shows the user. ``run()`` attaches
    it to the exception, so it is picked up here.
    """
    reason = f"{type(exc).__name__}: {exc}"
    stderr = getattr(exc, "stderr", None)
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    if stderr and stderr.strip():
        reason += f" | stderr: {stderr.strip()}"
    return reason


def _install_workspace_shell(target):
    """Make the user's shells start in ``target``.

    The snippet is appended to the ccws user's ~/.bashrc, so every
    interactive shell -- the web terminal's and the SSH session's
    alike -- starts in the materialized workspace instead of the
    account's home directory.

    The home is the image's own (the container's root filesystem
    is writable, so there is nothing to copy into a volume), and
    the snippet is written as the user, so the file stays the
    user's own.
    """
    bashrc = os.path.join(SANDBOX_HOME, ".bashrc")
    try:
        with open(bashrc) as fh:
            existing = fh.read()
    except OSError:
        existing = ""
    if SANDBOX_BASHRC_MARKER not in existing:
        with open(bashrc, "a") as fh:
            fh.write(_bashrc_snippet(target))
    log(f"sandbox: shell startup file {bashrc} -> {target}")


def _materialize_workspace(repo, repo_type, branch, pr_number, sub_repo):
    """Materialize the workspace; return the directory to work in.

    The materialization runs in a forked child that drops to the
    unprivileged ``ccws`` user first. The container is root (sshd
    needs it), but the workspace has to belong to the user whose
    session opens it: git refuses to operate in a repository owned
    by somebody else (the "dubious ownership" guard), and the user
    has to be able to write in it. Running the clone as the user
    makes the files the user's by construction -- there is no chown
    pass over the tree afterwards.

    The child reports the outcome over a pipe: the target
    directory on success, the reason on failure.
    """
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        code = 0
        try:
            payload = _materialize_workspace_as_user(
                repo, repo_type, branch, pr_number, sub_repo)
        except BaseException as e:
            # ``BaseException`` because ``die()`` raises
            # ``SystemExit``; the reason is what the user gets to
            # read, so it has to carry the failing command's own
            # output.
            payload = {"ok": False, "reason": _failure_reason(e)}
            code = 1
        try:
            os.write(write_fd, json.dumps(payload).encode())
        except OSError:
            pass
        os.close(write_fd)
        os._exit(code)
    os.close(write_fd)
    with os.fdopen(read_fd, "rb") as fh:
        raw = fh.read()
    _, status = os.waitpid(pid, 0)
    try:
        payload = json.loads(raw.decode())
    except (ValueError, UnicodeDecodeError):
        payload = {"ok": False,
                   "reason": "the materialization child died "
                             f"(wait status {status})"}
    if not payload.get("ok"):
        die("sandbox: the workspace materialization failed: "
            f"{payload.get('reason', 'unknown reason')}")
    return payload["target"]


def _materialize_workspace_as_user(repo, repo_type, branch, pr_number,
                                   sub_repo):
    """The materialization itself, run as the unprivileged user.

    The workspace is materialized the way the PR-comment and the
    Redmine-task worker materialize it (through the shared
    ``shoggoth.workspace`` module): a plain repository goes into a
    subdirectory of the source space, and a ccws workspace takes
    the source space over -- its own clone becomes the source space
    root (that is where the repository list lives) and
    ``wshandler update`` fills the listed sub-repositories into
    subdirectories of it. The PR's head ref is then checked out in
    the repository it belongs to, so the terminal opens on the PR's
    code inside a full ccws environment.

    The workspace's *dependencies* are not installed here (see the
    ``install_deps=False`` below): the sandbox pod has no
    ``/cache`` volume to install them into, and the worker that
    builds the code owns that step.
    """
    # Imported here rather than at module scope: the shoggoth package
    # builds its parameter singleton at import time and dies without
    # SHOGGOTH_DOMAIN, which the cleanup sidecar and the
    # wait-bootstrap init container do not set.
    from shoggoth.workspace import (REPO_TYPE_WORKSPACE, apply_pr_ref,
                                    clear_dir, materialize, repo_dir_name,
                                    source_space)

    _drop_privileges()
    src = source_space()
    if repo_type == REPO_TYPE_WORKSPACE:
        # A workspace takes the source space over: its own clone is
        # the source space root, and the listed sub-repositories land
        # in subdirectories of it.
        clear_dir(src)
        dest = src
    else:
        # A plain repository is one project among the source space's
        # repositories: it goes into a subdirectory of its own.
        dest = os.path.join(src, repo_dir_name(repo))
    log(f"sandbox: materializing {repo} (type={repo_type}) in {dest}")
    started = time.time()
    # ``install_deps=False``: the sandbox pod does NOT install the
    # workspace's dependencies.
    #
    # The installation is ``make dep_install``, which resolves the
    # dependency list with rosdep and installs it with apt/pip. It
    # needs the shared ``ci-cache`` PVC mounted at /cache -- the
    # slave image's ``CCWS_CACHE=/cache/ccws/`` points there, and
    # /cache itself only exists in the image as a build-time BuildKit
    # cache mount, so it is not there at runtime. The sandbox pod
    # deliberately does not mount that PVC: it is ReadWriteOnce, and
    # the sandbox pod can be scheduled on a different node than the
    # pod that already holds it, which leaves the sandbox pod stuck
    # in ContainerCreating. The worker -- the CI actor that actually
    # builds the code -- is the pod that mounts the PVC and installs
    # the dependencies.
    #
    # Installing them here would also blow the budget: the
    # resolution + install is minutes, and the dispatcher's wait for
    # the sandbox to come up is 900 s.
    materialize(_clone_url(repo), dest, repo_type=repo_type,
                url_rewrites=_workspace_url_rewrites(),
                install_deps=False, label="sandbox")
    log(f"sandbox: workspace materialized in "
        f"{int(time.time() - started)}s")

    # The PR's head ref belongs to the repository the PR is against:
    # the project itself for a plain repository, the sub-repository
    # it touches for a workspace.
    target = os.path.join(dest, sub_repo) if sub_repo else dest
    if branch:
        log(f"sandbox: checking out PR ref {branch!r} "
            f"(pr={pr_number or '-'}) in {target}")
        apply_pr_ref(target, branch, pr_number or None, label="sandbox")
    else:
        log(f"sandbox: no PR ref to check out; "
            f"staying on the default branch of {target}")
    # Mark the checked-out repository as safe so subsequent git
    # operations in the user's session don't refuse with the
    # "dubious ownership" guard.
    run(["git", "config", "--global", "--add", "safe.directory", target])
    # The shell's landing directory is installed before the servers
    # are started: the SSH session's shell reads the startup file,
    # and it has to be in place before anybody can log in.
    _install_workspace_shell(target)
    log(f"sandbox: workspace ready in {target}")
    return {"ok": True, "target": target}


def sandbox(repo, repo_type, branch="", pr_number="", sub_repo=""):
    """Main container: materialize the workspace, then serve it.

    The whole sandbox in one process tree:

      1. wait for the git-cred sidecar's ready flag -- the
         credential cache the clone authenticates against is
         keyed on the in-cluster host, so the clone cannot start
         before it is live;
      2. materialize the workspace as the unprivileged ``ccws``
         user and check the PR's head ref out in it;
      3. start the SSH server and the web terminal, both as the
         unprivileged ``ccws`` user;
      4. supervise them: the first one to exit ends the sandbox
         and the other is stopped with it.

    Nothing in the sandbox needs root. The container itself runs
    as ``ccws`` (the pod's securityContext says so), and so do
    both servers and every session they open: the SSH daemon has
    no privileges left to switch to another account, which is
    exactly why it only serves ``ccws``.

    ``repo`` is the PROJECT repository the PR belongs to, not the
    PR's head repository: the project carries the workspace layout,
    and the PR's head ref is checked out on top of it. The
    dispatcher resolves it.

    Note: ``repo``, ``branch`` and the rest are passed in by
    ``main()`` after the outer subparser has already consumed
    ``--repo`` and the other options from ``sys.argv``. Re-parsing
    ``sys.argv`` inside this function would re-see the literal
    subcommand token ``"sandbox"`` as a positional and fail with
    "unrecognized arguments: sandbox" (the exact bug this
    signature was introduced to fix).
    """
    _wait_for_git_cred()
    target = _materialize_workspace(repo, repo_type, branch, pr_number,
                                    sub_repo)
    children = {
        "sshd": _start_sshd(),
        "ttyd": _start_ttyd(target),
    }
    _install_termination_handler(children)
    return _supervise(children)


def _ensure_sshd_host_key():
    """Make sure the sandbox has a host key of its own.

    The image ships a host key, but it is root-owned (an
    unprivileged daemon cannot read it) and shared by every sandbox
    (so one sandbox could impersonate another). A key generated here
    belongs to the account and to this sandbox alone.
    """
    if os.path.exists(SANDBOX_SSHD_HOST_KEY):
        return
    # The directory is created here rather than assumed: the image and
    # the mounted scripts are versioned separately, so the launcher
    # cannot rely on the image having prepared the account's .ssh.
    os.makedirs(os.path.dirname(SANDBOX_SSHD_HOST_KEY), mode=0o700,
                exist_ok=True)
    result = subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "",
         "-f", SANDBOX_SSHD_HOST_KEY],
        capture_output=True, text=True)
    if result.returncode != 0:
        die(f"sandbox: cannot generate the SSH host key: "
            f"{(result.stderr or result.stdout).strip()}")


def _start_sshd():
    """Start the SSH server; return its pid.

    The daemon runs as the unprivileged ``ccws`` user -- the same
    user its sessions run as, which is the only user an
    unprivileged sshd can serve: it has no privileges left to
    switch to another account, so the kernel refuses the switch
    ("Failed to set uids to N") and the login is refused. Nothing
    in the sandbox needs a root process any more.

    Authentication is by password. PAM verifies it through the
    setuid-root ``unix_chkpwd`` helper, so the daemon never reads
    /etc/shadow itself; the account's password is the image's
    (see shoggoth/dockerfiles/slave), and the PR comment carries
    it. Public key authentication is off: the account has no
    authorized_keys, and a key in the PR comment would be a
    credential in a place a credential does not belong.

    The daemon listens on an unprivileged port (see
    ``SANDBOX_SSHD_PORT``) and writes its pid file into the
    account's own directory, because an unprivileged daemon cannot
    write into /run.

    The config is validated before the daemon is started, so a
    misformatted sshd_config is reported instead of leaving a
    half-broken server behind that refuses every connection.
    """
    # The paths the daemon needs are passed on the command line
    # instead of being taken from the image's config. The image is
    # versioned separately from these scripts (the scripts are
    # mounted from the host into the pod), so the config in the
    # image can be older than the code that generates the host key
    # -- and a config naming a host key the unprivileged daemon
    # cannot read leaves sshd with no host key at all: "no hostkeys
    # available -- exiting." The command line wins over the config
    # for the pid file and adds to the config's host key list, so the
    # per-sandbox key is always the one the daemon serves.
    sshd_options = [
        "-o", f"Port={SANDBOX_SSHD_PORT}",
        "-o", f"HostKey={SANDBOX_SSHD_HOST_KEY}",
        "-o", f"PidFile={SANDBOX_SSHD_PID_FILE}",
    ]
    pid = os.fork()
    if pid == 0:
        _drop_privileges()
        try:
            _ensure_sshd_host_key()
            # ``sshd -t`` parses sshd_config and returns non-zero on a
            # parse error (or when the host key is missing). It runs
            # with the same options as the daemon below, so it tests
            # the configuration that is actually going to be used.
            test = subprocess.run(
                ["/usr/sbin/sshd", "-t", "-f", SANDBOX_SSHD_CONFIG]
                + sshd_options,
                capture_output=True, text=True)
            if test.returncode != 0:
                die(f"sandbox: sshd -t failed: "
                    f"{(test.stderr or test.stdout).strip()}")
            # ``-D`` foreground (so the kubelet's PID 1 monitoring
            # works); ``-e`` routes the auth log to stderr (the kubelet
            # captures stderr into the container log + the
            # FallbackToLogsOnError termination message). The port is
            # passed as ``-o Port=...``: sshd's ``-p`` is the *port*
            # flag too, but ``-o`` keeps the override next to the
            # config it belongs to.
            os.execv("/usr/sbin/sshd",
                     ["/usr/sbin/sshd", "-D", "-e",
                      "-f", SANDBOX_SSHD_CONFIG] + sshd_options)
        except OSError as e:
            log(f"sandbox: exec sshd failed: {e}")
            os._exit(1)
    return pid


def _start_ttyd(target):
    """Start the web terminal; return its pid.

    The terminal runs as the unprivileged ``ccws`` user: it IS the
    user's shell. It listens on an unprivileged port for the same
    reason (see ``SANDBOX_TTYD_PORT``); the Service maps the
    user-facing port 80 onto it.

    The web terminal's own shell is started in ``target``
    explicitly; the shell's startup file covers the SSH session
    and the tmux windows the user opens later.
    """
    pid = os.fork()
    if pid == 0:
        _drop_privileges()
        try:
            os.execvp("ttyd", [
                "ttyd", "--max-clients", "1",
                "--port", str(SANDBOX_TTYD_PORT),
                "--writable", "-t", "disableReconnect=true",
                "--once", "tmux", "-u", "new-session",
                "bash -lc 'cd " + shlex.quote(target) + " && exec bash'",
            ])
        except OSError as e:
            # ttyd missing, or the binary cannot be executed. That
            # is a real failure -- the web terminal is the
            # sandbox's front door -- so it is left to fail rather
            # than dressed up as a normal exit.
            log(f"sandbox: exec ttyd failed: {e}")
            os._exit(1)
    return pid


def _signal(pid, sig):
    """Signal a child process, ignoring an already-dead one."""
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def _supervise(children):
    """Wait for the servers; the first exit ends the sandbox.

    The pod's restartPolicy is Never, so the pod ends when its
    main container exits. One container, two servers: the first
    one to exit ends the sandbox and the other is stopped with it
    -- the semantics the two-container layout had, where the
    kubelet stopped the surviving container when the first one
    exited. A user closing the browser tab ends the sandbox (ttyd
    --once exits), exactly as it did before.

    Returns the exit code of the first server to exit: that is
    the container's exit code.
    """
    # ``children`` maps a server's name to its pid; ``alive`` is the
    # reverse, so a pid can be looked up when it exits.
    alive = {pid: name for name, pid in children.items()}
    first = None
    kill_deadline = None
    while alive:
        if kill_deadline is not None and time.time() > kill_deadline:
            for pid, name in list(alive.items()):
                log(f"sandbox: killing {name} (it ignored SIGTERM)")
                _signal(pid, signal.SIGKILL)
            kill_deadline = None
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if pid == 0:
            # No server exited yet. A short sleep keeps this from
            # spinning on the CPU while the sandbox is idle.
            time.sleep(0.5)
            continue
        name = alive.pop(pid, None)
        if name is None:
            continue
        code = os.waitstatus_to_exitcode(status)
        log(f"sandbox: {name} exited (exit_code={code})")
        if first is None:
            first = (name, code)
            for other_pid, other_name in list(alive.items()):
                log(f"sandbox: stopping {other_name} ({name} exited)")
                _signal(other_pid, signal.SIGTERM)
            kill_deadline = time.time() + SANDBOX_STOP_GRACE_S
    if first is None:
        return 0
    return first[1]


def _install_termination_handler(children):
    """Forward a termination signal to the servers.

    The kubelet sends SIGTERM when the pod is deleted; the servers
    have to be stopped with it, or they are killed by the
    runtime's SIGKILL a few seconds later, with the SSH session's
    pty left dangling.
    """
    def handler(signum, _frame):
        log(f"sandbox: signal {signum} -- stopping the servers")
        for pid, name in children.items():
            _signal(pid, signal.SIGTERM)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def _container_state(pod_json, name):
    """Extract a container's state from the pod JSON.

    Returns a dict with keys: terminated (bool), waiting (bool),
    ready (bool), waiting_reason (str), exit_code (int).
    Returns None if the container is not yet visible in
    containerStatuses (the sidecar may start polling before the
    other containers' status is populated).
    """
    pod = json.loads(pod_json)
    for cs in pod.get("status", {}).get("containerStatuses", []):
        if cs.get("name") != name:
            continue
        state = cs.get("state", {})
        waiting = state.get("waiting") or {}
        terminated = state.get("terminated") or {}
        return {
            "terminated": bool(terminated),
            "waiting": bool(waiting),
            "ready": bool(cs.get("ready")),
            "waiting_reason": waiting.get("reason", ""),
            "exit_code": terminated.get("exitCode"),
        }
    return None


def cleanup():
    """Sidecar: watch the sandbox container, delete the pod once it
    is gone.

    The pod's main container runs both servers (the web terminal
    and the SSH server), so there is one container to watch: the
    sidecar deletes the pod when it terminates. The normal path is
    the user closing the browser, which ends ttyd and with it the
    sandbox.

    Replaces SANDBOX_CLEANUP_SCRIPT. The shell version used
    `kubectl get -o json` + `printf | python3 -c` invocations
    (four subprocesses per poll) + `awk`/`jq` for state
    extraction; the Python version is one subprocess per
    poll with the state extraction in-process.
    """
    namespace = os.environ["SHOGGOTH_NAMESPACE"]
    pod_name = os.environ["SHOGGOTH_POD_NAME"]
    started_at = time.time()
    deadline = started_at + CLEANUP_MAX_LIFETIME_S
    # Track the first time each termination signal was observed.
    # The first signal starts the grace timer; once that timer
    # elapses, the pod is deleted.
    seen_terminated_at = 0.0
    seen_not_ready_at = 0.0
    # Why the pod is being deleted. Set by the branch that breaks
    # the loop, and logged right before the delete so the
    # dispatcher's diagnostic (and the container's own log) says
    # which condition ended the sandbox.
    delete_reason = "cleanup deadline reached"
    while time.time() < deadline:
        try:
            proc = run(["kubectl", "get", "pod", pod_name, "-n", namespace,
                        "-o", "json"], capture_output=True, text=True)
            pod_json = proc.stdout
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr.decode() if isinstance(e.stderr, bytes)
                      else (e.stderr or ""))
            log(f"cleanup: kubectl get pod failed: {stderr.strip()}; "
                f"retrying")
            time.sleep(10)
            continue
        # Pod was deleted by something else (operator action,
        # CronJob, etc.) -- exit so the sidecar doesn't loop
        # forever recreating it.
        if not pod_json or pod_json.strip() in ("", "{}"):
            log("cleanup: pod gone, exiting")
            return
        state = _container_state(pod_json, SANDBOX_CONTAINER)
        if state is None:
            # the container is not yet visible -- wait.
            time.sleep(CLEANUP_POLL_S)
            continue
        # 1) the container exited cleanly. Common path: the user
        # closed the browser, ttyd --once exited, the supervisor
        # stopped the SSH server with it and the container
        # terminated. Wait CLEANUP_GRACE_S so the dispatcher's
        # post-Ready diagnostics can read the pod state.
        if state["terminated"]:
            if seen_terminated_at == 0:
                seen_terminated_at = time.time()
                log(f"cleanup: {SANDBOX_CONTAINER} terminated, waiting "
                    f"{CLEANUP_GRACE_S}s grace before delete")
            if (time.time() - seen_terminated_at) >= CLEANUP_GRACE_S:
                delete_reason = (f"{SANDBOX_CONTAINER} terminated "
                                 f"(exit_code={state['exit_code']})")
                break
            time.sleep(5)
            continue
        # 2) the container is waiting with a non-transient reason.
        # CrashLoopBackOff, ImagePullBackOff, ErrImagePull,
        # CreateContainerError all surface here -- any of them
        # means it is not running and never will be without
        # operator intervention.
        if state["waiting"]:
            if seen_terminated_at == 0:
                seen_terminated_at = time.time()
                log(f"cleanup: {SANDBOX_CONTAINER} waiting "
                    f"(reason={state['waiting_reason']}), "
                    f"waiting {CLEANUP_GRACE_S}s grace before delete")
            if (time.time() - seen_terminated_at) >= CLEANUP_GRACE_S:
                delete_reason = (f"{SANDBOX_CONTAINER} waiting "
                                 f"(reason={state['waiting_reason']})")
                break
            time.sleep(5)
            continue
        # 3) the container is alive but unresponsive. The kubelet's
        # livenessProbe budget is 30 s (initialDelay 900 +
        # period 10 + failureThreshold 3); not-ready for longer
        # than the not-ready budget is well past that budget and
        # indicates the container is hung in a way the kubelet
        # hasn't restarted yet (e.g. accepted the TCP connection
        # but never responded to the HTTP probe).
        if not state["ready"]:
            if seen_not_ready_at == 0:
                seen_not_ready_at = time.time()
            not_ready_elapsed = time.time() - seen_not_ready_at
            if not_ready_elapsed >= CLEANUP_NOT_READY_BUDGET_S:
                if seen_terminated_at == 0:
                    seen_terminated_at = time.time()
                    log(f"cleanup: {SANDBOX_CONTAINER} not-ready for "
                        f"{int(not_ready_elapsed)}s, waiting "
                        f"{CLEANUP_GRACE_S}s grace before delete")
                if (time.time() - seen_terminated_at) >= CLEANUP_GRACE_S:
                    delete_reason = (f"{SANDBOX_CONTAINER} not-ready for "
                                     f"{int(not_ready_elapsed)}s")
                    break
                time.sleep(5)
                continue
        else:
            seen_not_ready_at = 0
        time.sleep(CLEANUP_POLL_S)
    # --grace-period=0 makes the kubelet skip the graceful-
    # shutdown SIGTERM phase, so K8s removes the pod from the
    # API server promptly even though the kubectl binary is
    # running inside one of its containers (a graceful delete
    # would have stalled waiting for the cleanup container
    # itself to terminate, deadlocking the deletion).
    log(f"cleanup: deleting pod {pod_name} after "
        f"{int(time.time() - started_at)}s -- {delete_reason}")
    try:
        run(["kubectl", "delete", "pod", pod_name, "-n", namespace,
             "--grace-period=0", "--ignore-not-found"])
    except subprocess.CalledProcessError as e:
        # The pod was already deleted (operator action, race
        # with the kubelet's own GC). Treat as success.
        stderr = (e.stderr.decode() if isinstance(e.stderr, bytes)
                  else (e.stderr or ""))
        log(f"cleanup: kubectl delete pod returned non-zero "
            f"({stderr.strip()}); assuming already deleted")


def main():
    parser = argparse.ArgumentParser(prog="sandbox")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser(
        "wait-bootstrap",
        help="init container: wait for K8s externalsecrets to reach "
             "Ready",
    )
    sub.add_parser(
        "init-perms",
        help="init container: chown+chmod the git-cred emptyDir",
    )
    p_sandbox = sub.add_parser(
        "sandbox",
        help="main container: wait for git-cred, materialize the "
             "workspace, run the SSH server and the web terminal",
    )
    p_sandbox.add_argument("--repo", required=True,
                           help="Project repository full name (owner/name)")
    p_sandbox.add_argument("--type", required=True,
                           choices=["standalone", "ccws"],
                           help="Whether the project repository is a plain "
                                "repository or a ccws workspace")
    p_sandbox.add_argument("--branch", default="",
                           help="PR head branch to check out")
    p_sandbox.add_argument("--pr", default="",
                           help="PR number (the pull/<n>/head fallback)")
    p_sandbox.add_argument("--subrepo", default="",
                           help="Sub-repository of a workspace the PR "
                                "touches")
    sub.add_parser(
        "cleanup",
        help="sidecar: watch the sandbox container, delete the pod when "
             "it is dead",
    )
    args = parser.parse_args()
    code = {
        "wait-bootstrap": lambda: wait_bootstrap(),
        "init-perms": lambda: init_perms(),
        "sandbox": lambda: sandbox(args.repo, args.type, args.branch,
                                    args.pr, args.subrepo),
        "cleanup": lambda: cleanup(),
    }[args.cmd]()
    # The sandbox subcommand returns the exit code of the server
    # that ended the sandbox; the others return None (exit 0).
    sys.exit(code)


if __name__ == "__main__":
    main()
