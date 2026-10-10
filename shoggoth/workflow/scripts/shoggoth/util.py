"""Module split out of shoggoth.py; see package __init__."""
import re
import subprocess
import sys



def log(msg, file=None):
    """Print ``msg`` with one ``[shoggoth]`` prefix per line.

    The default for ``file`` resolves to ``sys.stderr`` at call
    time, not at function-definition time. The original
    ``shoggoth.py`` had ``file=sys.stderr`` (a default-argument
    value, evaluated once at import). That's fine in production
    but breaks any test framework that replaces ``sys.stderr``
    after import: ``log`` would still hold a reference to the
    pre-replacement file object, and writes through the default
    would bypass the replacement. Resolving at call time costs
    one ``getattr`` per call and keeps both the production and
    test paths working.
    """
    if file is None:
        file = sys.stderr
    for line in msg.splitlines() or [""]:
        print(f"[shoggoth] {line}", file=file, flush=True)
def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)
def _redact_argv_for_log(cmd):
    """Return a copy of ``cmd`` with secret-bearing tokens masked.

    Used only for log output — never for execution. ``run()`` calls
    this on its raw argv so the execution path remains untouched and
    the redactor can be tightened without affecting behavior.
    """
    out = []
    redact_eq = {"--from-literal", "--from-file", "--header",
                 "--user", "--password", "--token", "-u", "-p"}
    redact_eq_prefixes = ("--from-literal=", "--header=", "--token=")
    redact_colon_prefixes = ("Authorization:", "X-Vault-Token:",
                             "X-Redmine-API-Key:")
    for arg in cmd:
        a = str(arg)
        if any(a.startswith(p) for p in redact_eq_prefixes):
            out.append(a.split("=", 1)[0] + "=***")
        elif any(a.startswith(p) for p in redact_colon_prefixes):
            out.append(a.split(":", 1)[0] + ": ***")
        elif a in redact_eq:
            out.append(a)
        elif len(out) and out[-1] in redact_eq:
            out.append("***")
        else:
            out.append(a)
    return out
def run(cmd, **kwargs):
    """Run ``cmd`` via ``subprocess.run`` with capture_output/text on,
    and unconditionally emit one log row per channel. The argv is
    passed verbatim to ``subprocess.run`` (and therefore to the
    process); the log row, however, uses a redacted copy via
    ``_redact_argv_for_log`` so secret-bearing tokens
    (``--from-literal=KEY=VALUE``, ``Authorization: token X``, ``-p
    password``) never land in the Argo workflow log.
    """
    kwargs.setdefault("check", True)
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    log(f"run: {' '.join(_redact_argv_for_log(cmd))}")
    try:
        result = subprocess.run(cmd, **kwargs)
    except FileNotFoundError as e:
        # The first element of ``cmd`` (the executable) is missing
        # from PATH. Returning a synthetic non-zero CompletedProcess
        # lets callers treat this the same as a normal failure —
        # e.g. ``K8s.read_secret`` returns ``None`` when kubectl
        # isn't installed (the ``slave:noble`` image used by
        # ``slave-test`` doesn't ship kubectl and falls through to
        # the web-internal api-proxy for auth).
        log(f"run: executable not found: {cmd[0] if cmd else '?'} ({e})")
        return subprocess.CompletedProcess(cmd, returncode=127,
                                             stdout="",
                                             stderr=f"{cmd[0] if cmd else '?'}: not found")
    except subprocess.TimeoutExpired as e:
        log(f"run: TIMEOUT after {e.timeout}s: {' '.join(_redact_argv_for_log(cmd))}")
        stderr = e.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace") if stderr else ""
        if stderr:
            log(f"run: timeout stderr: {stderr[:500]}")
        return subprocess.CompletedProcess(cmd, returncode=124,
                                             stdout=e.stdout or "",
                                             stderr=e.stderr or "")
    log(f"run: rc={result.returncode}")
    if result.stdout:
        log(f"run: stdout: {result.stdout.strip()[:500]}")
    if result.stderr:
        log(f"run: stderr: {result.stderr.strip()[:500]}")
    return result
def normalize_for_branch(text):
    text = text.lower()
    text = re.sub(r"[^a-z0-9]", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")
