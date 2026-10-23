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
  ttyd             main container (slave image): wait for the
                   git-cred sidecar's ready flag, git-clone the
                   PR's branch, touch the repo-ready flag the
                   sshd container polls, exec ttyd.
  sshd             main container (slave image): wait for
                   git-cred + the repo-ready flag, then exec
                   sshd (the slave image bakes a no-password
                   sshd_config + a cleared ccws account, so
                   no per-sandbox credential is needed here).
  cleanup          sidecar (tools image): watch the ttyd
                   container's state, kubectl delete pod once
                   ttyd is dead.

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
access this module. The ttyd container uses the slave image
(noble) which bakes the workflow scripts in at a different path
(/shoggoth/workflow/scripts/) for historical reasons; the
hostPath mount is still added to the ttyd container for symmetry
and to make this file the authoritative copy of the lifecycle
logic.
"""
import argparse
import json
import os
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
# ttyd container polls this single file rather than each
# daemon's socket (matches the slave-term pattern and the
# workflow _wait_for_git_cred helper).
SANDBOX_CRED_SOCK = "/shoggoth/git-cred/git_credential_sock"
SANDBOX_READY_FLAG = "/shoggoth/git-cred/ready"

TTYD_READY_TIMEOUT_S = 90
REPO_MOUNT = "/repo"
# Touched by the ``ttyd`` container after the git clone
# finishes, polled by the ``sshd`` container so it knows /repo
# is populated before it lets users log in. Lives in the
# shared emptyDir that both containers mount (alongside
# /repo), so it survives the kubelet's parallel-container
# startup order. 120 s matches the ttyd subcommand's
# worst-case clone budget (large repo + cold image pull).
# Cross-container readiness signal: the ttyd subcommand
# touch()es this file under the shared `sandbox-state`
# emptyDir (mounted at /shoggoth/sandbox-state in both
# containers per dispatch.py:_apply_sandbox_pod); the sshd
# subcommand polls the same path. Must live under the
# emptyDir's mount point -- a path outside (e.g. /shoggoth/)
# would land in the writer's overlay, invisible to the
# reader, and sshd would always time out at REPO_READY_TIMEOUT_S.
REPO_READY_FLAG = "/shoggoth/sandbox-state/repo-ready"
REPO_READY_TIMEOUT_S = 180
# Note: the sandbox-pod sshd binds 22 (OpenSSH default), the
# slave image's sshd_config locks the daemon to the ``ccws``
# user (``AllowUsers ccws`` in shoggoth/dockerfiles/slave),
# and the dispatcher's per-sandbox Service exposes port 22.
# These three literals are kept in sync by hand -- no
# parametrization -- so the constants that used to live here
# (SLAVE_USER, SLAVE_SSHD_PORT) were dead code (referenced
# only by one log line) and have been removed.

# Cleanup budgets (matching SANDBOX_CLEANUP_SCRIPT):
#   MAX_LIFETIME  hard cap so a script bug in the detection logic
#                 can't leak the pod past the workflow's
#                 activeDeadlineSeconds (14400 s = 4 h).
#   GRACE         hold the pod alive after the first termination
#                 signal long enough for the dispatcher's
#                 post-Ready diagnostics (`kubectl wait` +
#                 `kubectl describe` + per-container `kubectl
#                 logs` chain, typically a few seconds; the
#                 dispatcher's `_wait_sandbox_ready` is 120 s,
#                 so 180 s covers the worst case).
#   NOT_READY     sustained not-ready threshold for a ttyd that
#                 accepted the TCP connection but never responded
#                 to the HTTP probe (kubelet's livenessProbe
#                 budget is 30 s; 120 s is well past that with
#                 90 s of headroom before declaring the pod hung).
CLEANUP_MAX_LIFETIME_S = 14400
CLEANUP_GRACE_S = 180
CLEANUP_NOT_READY_BUDGET_S = 120
CLEANUP_POLL_S = 10

# Container `waiting` reasons that are a normal pre-start state, not a
# failure. The kubelet reports a container this way from the moment the pod is
# created until the image is pulled and the process starts; containers in the
# same pod start in parallel, so the cleanup sidecar routinely polls and sees
# sshd waiting this way while ttyd is already up. Treating it as terminal
# armed the grace timer on a healthy sandbox.
CLEANUP_TRANSIENT_WAITING_REASONS = frozenset({
    "ContainerCreating",
    "PodInitializing",
})


def log(msg):
    """stderr log; the kubelet captures the init/main/cleanup
    container stderr into the workflow log via Argo's log
    archival, so this is the user-visible diagnostic channel."""
    print(f"sandbox: {msg}", file=sys.stderr, flush=True)


def die(msg):
    log(msg)
    sys.exit(1)


def run(args, **kwargs):
    """subprocess.run with check=True; surface stderr on failure."""
    return subprocess.run(args, check=True, **kwargs)


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
    for i in range(TTYD_READY_TIMEOUT_S):
        if os.path.exists(SANDBOX_READY_FLAG):
            log(f"ttyd: git-cred ready after {i}s")
            return
        time.sleep(1)
    die(f"ttyd: git-cred not ready after {TTYD_READY_TIMEOUT_S}s -- "
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
    die("ttyd: neither SHOGGOTH_DOMAIN nor SHOGGOTH_NAMESPACE is set; "
        "cannot build clone URL")


def _wait_for_repo_ready():
    """Block until the ttyd container's repo-ready flag is set.

    Used by the ``sshd`` container to gate user logins on
    the git clone having finished. The flag lives in the
    shared ``sandbox-state`` emptyDir (see
    ``REPO_READY_FLAG``), so the kubelet's parallel
    container startup order is irrelevant: whichever
    container is ready first simply blocks on the flag.
    Budget is ``REPO_READY_TIMEOUT_S`` (180 s, well past
    the ttyd subcommand's 90 s ready + 10-60 s clone
    path documented in the ttyd subcommand).
    """
    for i in range(REPO_READY_TIMEOUT_S):
        if os.path.exists(REPO_READY_FLAG):
            log(f"sshd: repo ready after {i}s")
            return
        time.sleep(1)
    die(f"sshd: /repo not ready after {REPO_READY_TIMEOUT_S}s "
        f"-- check 'kubectl logs <pod> -c ttyd'")


def ttyd(repo, branch):
    """Main container (ttyd): wait for git-cred, git clone,
    touch the repo-ready flag, exec ttyd.

    Replaces SANDBOX_TTYD_SCRIPT. Two correctness improvements
    over the shell version:

      1. The pre-fix shell used Python `.format()` to interpolate
         the user-controlled branch / repo name into a
         double-quoted shell arg; git ref-name rules permit
         `$`, `(`, `)`, `;`, `&`, `>`, `_`, `.` and other
         shell metacharacters, and `$(...)` expands inside
         double-quoted shell. The shell-injection RCE was
         exploitable from any Gitea user with write access on
         the base repo. The Python version uses subprocess.run
         with a list argv (no shell) so the branch name
         cannot inject shell metacharacters.
      2. The pre-fix script used the in-cluster short form
         `http://git.${SHOGGOTH_NAMESPACE}/...` unconditionally,
         but the git-cred sidecar's credential cache is keyed
         on `host=git.${SHOGGOTH_DOMAIN}`. On private repos
         the credential lookup misses and the clone 401s.
         The Python version uses the user-facing form when
         SHOGGOTH_DNS_IP is wired (which is the production
         path), matching the cache key.

    Note: `repo` and `branch` are passed in by ``main()`` after
    the outer subparser has already consumed ``--repo`` and
    ``--branch`` from ``sys.argv``. Re-parsing ``sys.argv``
    inside this function would re-see the literal subcommand
    token ``"ttyd"`` as a positional and fail with
    "unrecognized arguments: ttyd" (the exact bug this
    signature was introduced to fix).

    The new ``REPO_READY_FLAG`` touch()ed below is the
    signal the parallel ``sshd`` container waits on; see
    ``_wait_for_repo_ready``.
    """
    _wait_for_git_cred()
    clone_url = _clone_url(repo)
    log(f"ttyd: cloning {clone_url} (branch={branch}) "
        f"to {REPO_MOUNT}")
    # subprocess with list argv -- no shell, no interpolation.
    run(["git", "clone", "--depth", "1", "--branch", branch,
         clone_url, REPO_MOUNT])
    # Mark the cloned repo as safe so subsequent git operations
    # in the user's ttyd session don't refuse with the
    # "dubious ownership" guard.
    run(["git", "config", "--global", "--add", "safe.directory",
         REPO_MOUNT])
    # Publish that /repo is populated. The sshd container
    # is in a parallel ``containers:`` entry in the same
    # pod; the kubelet starts both at the same time, so
    # sshd needs an explicit signal that the clone
    # finished (the emptyDir is freshly created and
    # empty when the pod starts). Touch AFTER the
    # git config above so a user who SSHes in and runs
    # ``git push`` immediately after seeing the
    # ready-flag has the safe.directory already wired
    # in their config.
    with open(REPO_READY_FLAG, "w") as f:
        f.write("ready\n")
    log(f"sandbox: ready in {REPO_MOUNT} on branch {branch}; "
        f"touched {REPO_READY_FLAG}")
    # User exit is expected behavior, not a fault. ttyd --once
    # exits when the browser WebSocket closes; tmux's session
    # also ends. exec ttyd so it becomes the container PID 1;
    # the `|| true; exit 0` wrapper is the slave-term pattern
    # (avoids CrashLoopBackOff on the kubelet for a non-zero
    # ttyd exit).
    try:
        os.execvp("ttyd", [
            "ttyd", "--max-clients", "1", "--port", "80",
            "--writable", "-t", "disableReconnect=true",
            "--once", "tmux", "-u", "new-session",
            "bash -lc 'cd /repo && exec bash'",
        ])
    except OSError as e:
        log(f"ttyd: exec ttyd failed: {e}")
        sys.exit(0)


def sshd():
    """Main container (sshd): wait for git-cred + repo, exec sshd.

    Parallel sibling of the ``ttyd`` container in the same
    pod (the dispatcher writes both entries into
    ``spec.containers``). Shares the slave image (so sshd
    is already installed) and the ``repo`` / ``sandbox-state``
    / ``git-cred`` emptyDirs.

    Lifecycle:

      1. ``_wait_for_git_cred`` -- sshd starts before the
         user can authenticate, but we want SSH-based git
         pushes from inside the sandbox to work, so the
         ssh-agent socket under /shoggoth/git-cred must
         be live first.
      2. ``_wait_for_repo_ready`` -- the ttyd container
         touch()es REPO_READY_FLAG after the git clone
         finishes; we gate user logins on it so an SSH
         user doesn't land in an empty /repo.
      3. ``exec sshd -D -e`` -- foreground; kubelet
         ``terminationMessagePolicy: FallbackToLogsOnError``
         surfaces sshd's auth log to ``kubectl describe
         pod`` on a non-zero exit.

    The sshd the slave image (shoggoth/dockerfiles/slave)
    bakes needs no per-sandbox credential: it sets
    ``PasswordAuthentication yes`` with
    ``PermitEmptyPasswords yes``, and the ccws account
    keeps the password the base image assigned
    (``ccws/ccws``, from ccws/examples/Dockerfile), so a
    login supplies that password at the prompt.

    The pod also has to grant sshd enough capabilities
    to finish *session* setup, which is a separate step
    from reaching the password prompt -- see the
    ``capabilities.add`` rationale in
    shoggoth/commands/dispatch.py for CHOWN and
    AUDIT_WRITE.

    ``runAsUser: 0`` is required so sshd can bind port
    22 (the dispatcher's per-sandbox Service maps host
    port 22 to the pod's 22 and the slave image's
    sshd_config does not set ``Port > 1024``). sshd + PAM
    drop privileges to ccws after the password check
    passes (``UsePAM yes`` in sshd_config, the upstream
    Debian default). The container's
    ``readOnlyRootFilesystem: true`` (set by the
    dispatcher) is safe here -- we do not write to
    ``/etc/shadow`` or any other mutable path.
    """
    _wait_for_git_cred()
    _wait_for_repo_ready()
    # /run/sshd is the privilege-separation chroot on
    # Debian/Ubuntu; sshd refuses to start without it. The slave
    # image ships it, but the sandbox pod built by dispatch.py mounts
    # a writable emptyDir over this exact path (the container runs with
    # readOnlyRootFilesystem, so /run itself cannot be written), and
    # kubelet creates emptyDirs mode 0777 -- which sshd rejects with
    # "/run/sshd must be owned by root and not group or
    # world-writable". A dedicated init container chowns it to
    # root:root and chmods 0755 before the sshd container starts, so
    # exist_ok=True is a no-op that simply keeps this working if that
    # init container is ever dropped.
    os.makedirs("/run/sshd", exist_ok=True)
    # ``sshd -t`` parses sshd_config and returns non-zero
    # on a parse error. Catches a misformatted
    # sshd_config before exec'ing sshd in the foreground
    # (a half-broken sshd would otherwise refuse all
    # connections and we'd lose the diagnostic).
    test = subprocess.run(
        ["/usr/sbin/sshd", "-t"],
        capture_output=True, text=True)
    if test.returncode != 0:
        die(f"sshd: sshd -t failed: "
            f"{(test.stderr or test.stdout).strip()}")
    log(f"sshd: launching on port 22 "
        f"(user=ccws, empty-password auth)")
    # -D foreground (so kubelet PID 1 monitoring works);
    # -e routes the auth log to stderr (the kubelet
    # captures stderr into the container log + the
    # FallbackToLogsOnError termination message).
    # PidFile keeps the pid file inside the writable
    # /run/sshd emptyDir; the default /run/sshd.pid sits
    # on the read-only rootfs and makes sshd log "Couldn't
    # create pid file" on every start. Note this must be
    # `-o PidFile=...`: sshd's `-p` is the *port* flag, so
    # `-p /run/sshd/sshd.pid` makes sshd abort with "Bad
    # port number.".
    os.execv("/usr/sbin/sshd",
             ["/usr/sbin/sshd", "-D", "-e",
              "-o", "PidFile=/run/sshd/sshd.pid"])


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


def _pod_ready(pod_json):
    """True if the pod carries a Ready=True condition.

    Pod Ready is a conjunction over every container, so a single
    dead container keeps the whole pod out of the Service's ready
    endpoints -- which makes *every* container unreachable, not just
    the dead one. The cleanup watchdog uses this to tell "this
    container is broken" apart from "this sandbox is unreachable".
    """
    pod = json.loads(pod_json)
    for cond in pod.get("status", {}).get("conditions", []):
        if cond.get("type") == "Ready":
            return cond.get("status") == "True"
    return False


def cleanup():
    """Sidecar: watch the access containers, delete the pod once none
    of them can serve.

    Watches both ``ttyd`` and ``sshd``. Pod Ready is a conjunction
    over every container, so a container that dies takes the whole
    pod out of the Service's ready endpoints and makes *both* access
    paths unreachable -- which is why an sshd failure has to end the
    pod even though ttyd is still serving.

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
    #
    # `seen_terminated_at` belongs to the ttyd branches alone. The sshd
    # branch keeps its own clock so that a signal which later clears -- or
    # which was never real -- cannot leave a stale timestamp behind that
    # silently consumes the next branch's grace window.
    seen_terminated_at = 0.0
    seen_sshd_down_at = 0.0
    seen_not_ready_at = 0.0
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
        state = _container_state(pod_json, "ttyd")
        if state is None:
            # ttyd container not yet visible -- wait.
            time.sleep(CLEANUP_POLL_S)
            continue
        # 0) sshd is gone. sshd has no readinessProbe, so while it is
        # alive it counts as Ready and the pod can go Ready; the
        # moment it exits, the pod can never be Ready again (there is
        # no restartPolicy to bring it back). A not-Ready pod is kept
        # in the Service's notReadyAddresses, so this takes *both*
        # access paths down -- ttyd keeps serving happily and stays
        # unreachable. This is the "neither ssh nor ttyd usable" case
        # and it is terminal, so it does not wait on the
        # not-ready budget: the pod can never recover.
        #
        # Only a genuinely terminated sshd, or one waiting for a reason
        # that will not resolve on its own, is fatal. A `running` sshd is
        # the normal startup state (it blocks in _wait_for_git_cred and
        # _wait_for_repo_ready before exec'ing sshd), and the transient
        # waiting reasons (ContainerCreating, PodInitializing) are how the
        # kubelet reports every container between pod creation and process
        # start -- the normal pre-start state, not a failure. This runs on
        # its own clock: a signal that clears below resets it, so it can
        # never pre-arm the ttyd branches' shared grace window.
        sshd = _container_state(pod_json, "sshd")
        sshd_down = False
        if sshd is not None:
            if sshd["terminated"]:
                sshd_down = True
            elif (sshd["waiting"]
                  and sshd["waiting_reason"]
                  not in CLEANUP_TRANSIENT_WAITING_REASONS):
                sshd_down = True
        if sshd_down:
            reason = (f"exit_code={sshd['exit_code']}"
                      if sshd["terminated"]
                      else f"waiting_reason={sshd['waiting_reason']}")
            if seen_sshd_down_at == 0:
                seen_sshd_down_at = time.time()
                log(f"cleanup: sshd is down ({reason}) and pod Ready="
                    f"{_pod_ready(pod_json)}; without sshd the pod can "
                    f"never go Ready, so neither ttyd nor ssh is "
                    f"reachable. Deleting after {CLEANUP_GRACE_S}s "
                    f"grace. Check 'kubectl logs {pod_name} -c sshd'")
            if (time.time() - seen_sshd_down_at) >= CLEANUP_GRACE_S:
                break
            time.sleep(5)
            continue
        seen_sshd_down_at = 0.0
        # 1) ttyd exited cleanly. Common path: user closed the
        # browser, ttyd --once exits, the kubelet marks the
        # container terminated. Wait CLEANUP_GRACE_S so the
        # dispatcher's post-Ready diagnostics can read the pod
        # state.
        if state["terminated"]:
            if seen_terminated_at == 0:
                seen_terminated_at = time.time()
                log(f"cleanup: ttyd terminated, waiting {CLEANUP_GRACE_S}s "
                    f"grace before delete")
            if (time.time() - seen_terminated_at) >= CLEANUP_GRACE_S:
                break
            time.sleep(5)
            continue
        # 2) ttyd in waiting with a non-transient reason.
        # CrashLoopBackOff, ImagePullBackOff, ErrImagePull,
        # CreateContainerError all surface here -- any of them
        # means ttyd is not running and never will be without
        # operator intervention.
        if state["waiting"]:
            if seen_terminated_at == 0:
                seen_terminated_at = time.time()
                log(f"cleanup: ttyd waiting "
                    f"(reason={state['waiting_reason']}), "
                    f"waiting {CLEANUP_GRACE_S}s grace before delete")
            if (time.time() - seen_terminated_at) >= CLEANUP_GRACE_S:
                break
            time.sleep(5)
            continue
        # 3) ttyd is alive but unresponsive. The kubelet's
        # livenessProbe budget is 30 s (initialDelay 120 +
        # period 10 + failureThreshold 3); not-ready for >120 s
        # is well past that budget and indicates ttyd is hung
        # in a way the kubelet hasn't restarted yet (e.g.
        # accepted the TCP connection but never responded to
        # the HTTP probe).
        if not state["ready"]:
            if seen_not_ready_at == 0:
                seen_not_ready_at = time.time()
            not_ready_elapsed = time.time() - seen_not_ready_at
            if not_ready_elapsed >= CLEANUP_NOT_READY_BUDGET_S:
                if seen_terminated_at == 0:
                    seen_terminated_at = time.time()
                    log(f"cleanup: ttyd not-ready for "
                        f"{int(not_ready_elapsed)}s, waiting "
                        f"{CLEANUP_GRACE_S}s grace before delete")
                if (time.time() - seen_terminated_at) >= CLEANUP_GRACE_S:
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
    p_ttyd = sub.add_parser(
        "ttyd",
        help="main container: wait for git-cred, clone, exec ttyd",
    )
    p_ttyd.add_argument("--repo", required=True,
                        help="Gitea repo full name")
    p_ttyd.add_argument("--branch", required=True,
                        help="Branch name to clone")
    p_sshd = sub.add_parser(
        "sshd",
        help="main container: wait for git-cred + repo, exec sshd",
    )
    sub.add_parser(
        "cleanup",
        help="sidecar: watch ttyd, delete pod on ttyd death",
    )
    args = parser.parse_args()
    {"wait-bootstrap": lambda: wait_bootstrap(),
     "init-perms": lambda: init_perms(),
     "ttyd": lambda: ttyd(args.repo, args.branch),
     "sshd": lambda: sshd(),
     "cleanup": lambda: cleanup()}[args.cmd]()


if __name__ == "__main__":
    main()
