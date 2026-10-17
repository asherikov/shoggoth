"""Module split out of shoggoth.py; see package __init__."""
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid

from .otlp import OtlpLogger
from .util import die, log, run
from .state import params


def _normalise_diff_path(path):
    """Strip prefixes the agent may prepend to a repo-relative path.

    The ``git diff`` ``+++ b/<path>`` header and Gitea's
    ``comments[].path`` both use the repo-root-relative
    form (no leading ``./``, no leading ``/``). The agent,
    however, sometimes reports paths with a leading ``./``
    (``./README.md`` from a relative-path read) or a
    leading ``/`` (``/README.md`` from an absolute read).
    Strip those so the inline-anchor lookup in
    ``commands/pr_update.py:_post_review_followup_with_links``
    keys the agent's normalised path against the GET
    response consistently.
    """
    if not path:
        return path
    p = path
    while p.startswith("./"):
        p = p[2:]
    if p.startswith("/"):
        p = p[1:]
    return p


class Agent:
    # Default per-invocation wall-clock cap for ``prompt()`` / the
    # ``pr-comment`` per-comment retries. The workflow template's
    # ``activeDeadlineSeconds`` (4 h on command-handler) is
    # the outer safety net; this is the per-prompt ceiling so a
    # single hung agent doesn't burn the whole pod's budget.
    AGENT_TIMEOUT = 1200

    # Forbidden bash glob patterns the agent must NEVER invoke — git
    # commits/pushes/tags, wsh and wshandler wrappers — encoded as
    # the Qwen Code permission-deny syntax. Pulled out of the module
    # top level because every consumer is inside this class.
    QWEN_DENY_PATTERNS = [
        "Bash(git commit*)",
        "Bash(git commit)",
        "Bash(git push*)",
        "Bash(git push)",
        "Bash(git tag*)",
        "Bash(git tag)",
        "Bash(git -C * commit*)",
        "Bash(git -C * push*)",
        "Bash(git -C * tag*)",
        "Bash(wsh *commit*)",
        "Bash(wsh *push*)",
        "Bash(wsh *tag*)",
        "Bash(wshandler commit*)",
        "Bash(wshandler push*)",
        "Bash(wshandler tag*)",
        "Bash(wshandler tag)",
    ]

    # Allowed tool sets per session kind. ``full`` is unrestricted;
    # ``pr-review`` / ``pr-comment`` exclude mutating tools because
    # those sessions are read-only reviews of work the dispatcher is
    # evaluating.
    TOOL_PROFILES = {
        "full": [],
        "pr-review": [
            "edit",
            "write_file",
            "notebook_edit",
        ],
        "pr-comment": [],
    }

    # Map Qwen session kind (``start_session(event_type=...)``) to
    # the corresponding ``TOOL_PROFILES`` key.
    TOOL_PROFILE_BY_SESSION = {
        "task": "full",
        "ci-failure": "full",
        "pr-review": "pr-review",
        "pr-comment": "pr-comment",
    }

    # Env-var names whose values must NEVER leak to the qwen subprocess.
    # Mirrors ``qwen_security``: stripping these from the inherited env
    # before Popen() keeps workflow-injected secrets (OpenBao address,
    # default AI token) out of the model context. The Gitea / Redmine /
    # LDAP / SSH tokens are no longer injected by workflow templates
    # (the script reads them from K8s directly), so they don't need
    # filtering here. SHOGGOTH_VAULT_TOKEN / OPENBAO_ADDR are kept as a
    # defensive measure — they are no longer read by this script after
    # the K8s refactor, but if a workflow template still injects them,
    # they shouldn't reach the agent either.
    SECRET_ENV_KEYS = frozenset([
        "SHOGGOTH_VAULT_TOKEN", "OPENBAO_ADDR",
        "SHOGGOTH_AI_DEFAULT_TOKEN",
    ])

    ANTI_LEAK_DIRECTIVE = (
        "Do not emit thinking markup (e.g., <thinking>, <thought>, or any "
        "XML/HTML tags that resemble reasoning traces). Output only the final "
        "answer.\n\n"
    )

    @staticmethod
    def _filter_qwen_env():
        return {k: v for k, v in os.environ.items()
                if k not in Agent.SECRET_ENV_KEYS}

    @staticmethod
    def _extract_review_json(text):
        """Find and parse a JSON review object from agent assistant text.

        Looks for the first ```json ... ``` fenced block (or a bare JSON
        object ending at the END_OF_REVIEW marker), validates it has the
        expected shape, and returns the parsed dict. Returns None if
        nothing usable is found.
        """
        return Agent._extract_agent_json(text, "END_OF_REVIEW",
                                          ("verdict", "findings", "summary"))

    @staticmethod
    def _extract_agent_json(text, end_marker, required_keys=()):
        """Generic JSON extractor from agent assistant text.

        The ``_extract_review_json`` shape check is too narrow for the
        slash-command prompts (squash wants ``subject``/``body``,
        rebase wants ``resolutions``); the JSON-locating logic is
        identical, so this helper does the locating once and lets the
        caller supply an end-marker name and the keys that make a
        payload "useful". ``required_keys`` is a tuple; the parsed
        dict passes if at least one of the keys is present, matching
        the lenient "any of these keys indicates a real review"
        contract ``_extract_review_json`` historically enforced.

        When the agent wraps the JSON in ```json ... ```
        fences the fenced block is preferred (the prompt asks
        for that format). When the agent omits the fence — the
        common real-world case when the model first emits
        reasoning prose ("With 2 high issues, the verdict should
        be ...") and then the JSON — we fall back to a brace
        search over the text up to ``end_marker``.

        The brace search iterates ``{`` positions in REVERSE,
        not forward. Rationale: the review JSON is almost always
        the LAST dict-shaped object the model emits in its
        response, and the prose that precedes it commonly
        contains stray ``{`` characters (template variables,
        JSON snippets the model is quoting as examples, brace
        quotes in English prose). Forward iteration would
        match those first and return a non-review object;
        reverse iteration skips past the prose and lands on
        the review object's opening brace. ``raw_decode``
        itself handles brace balancing, so the LAST ``{`` that
        decodes to a dict is the outermost object whose
        opening brace is at that position — typically the
        review JSON itself.

        If no fenced block is found and no ``{`` decodes to a
        usable dict, this returns ``None`` and the caller
        falls back to posting the raw assistant text as
        Markdown (see ``pr_update.py`` ``_pr_review`` — the
        "no JSON block found in agent output; posting raw
        assistant text as Markdown" log line is the user-
        visible signal that the fallback fired).
        """
        if not text:
            return None
        m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        candidate = None
        if m:
            candidate = m.group(1)
        else:
            end = text.find(end_marker)
            if end != -1:
                head = text[:end]
            else:
                head = text
            decoder = json.JSONDecoder()
            # Walk ``{`` positions from the END of the text
            # backwards. The review JSON is typically the
            # last object the model emits, and prose that
            # precedes it often contains stray ``{``
            # characters (template variables, quoted JSON
            # snippets, English prose with brace quotes).
            # Forward iteration would match the first stray
            # ``{`` and return a non-review object; reverse
            # iteration skips past the prose.
            brace_positions = [i for i, ch in enumerate(head)
                                if ch == "{"]
            for i in reversed(brace_positions):
                try:
                    obj, consumed = decoder.raw_decode(head, i)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                # When ``required_keys`` is set, the LAST
                # ``{`` is often the innermost object (e.g.
                # a single finding inside the review's
                # ``findings`` array), not the review
                # itself. ``raw_decode`` at that position
                # returns the inner object, which lacks the
                # review-level keys. Skip positions whose
                # decoded dict has none of the required
                # keys, and continue walking backwards to
                # the outer review object.
                if required_keys and not any(
                        key in obj for key in required_keys):
                    continue
                candidate = head[i:i + consumed]
                break
            else:
                return None
        if not candidate:
            return None
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        if required_keys and not any(key in data for key in required_keys):
            return None
        return data

    @staticmethod
    def _format_review_json(review_data, pr_title, pr_url,
                             diff_text=None):
        """Render a qwen review run --json result as a Markdown review body
        suitable for posting as a single Gitea PR review.

        When ``diff_text`` is provided, also returns a list of
        inline comments (``{path, body, new_position}``) for
        the findings that have a resolvable path/line.
        Returns the tuple ``(markdown_body, inline_comments)``
        in that case; returns just the markdown string when
        ``diff_text`` is ``None`` (backwards compatible).

        ``new_position`` is a FILE LINE NUMBER, not a
        unified-diff position. The Gitea API field is
        named ``new_position`` (see
        ``modules/structs/pull_review.go``) but the struct
        comment is explicit — "if comment to new file line
        or 0" — and the swagger maps it to ``NewLineNum``.
        Gitea uses the value to cut the diff context around
        that line (``git.GetFileDiffCutAroundLine``) and
        stores it as ``LineNum`` in the comment. The field
        name is misleading; treating it as a diff position
        is a common source of off-by-N bugs in review bots
        (the symptom: a comment at "file:493" lands at
        line 12 because the diff position 12 was sent
        instead of the line number 493).

        A finding becomes an inline comment when:
          - ``path`` and ``line`` are present
          - ``line`` is a positive integer

        Gitea generates the diff context for unchanged
        lines too (``GeneratePatchForUnchangedLine``), so
        there's no need to validate that the line falls in
        a changed hunk — Gitea handles that itself. If the
        line is invalid (e.g. past EOF), Gitea rejects
        the whole review and the caller falls back to
        body-only via ``post_pr_review_chunked``.

        Inline comment body always carries the full finding
        text plus the ``path:line`` location, so reviewers
        see the context at the anchor without cross-
        referencing the rolled-up list. The review body
        only shows a reference to the inline comment
        (not the full text) for findings that were posted
        inline — duplicating the full text in the body
        would be noisy. Findings without a resolvable
        path/line keep their full text in the body so
        reviewers still see them.
        """
        if not isinstance(review_data, dict):
            return ("", []) if diff_text is not None else ""
        findings = review_data.get("findings") or []
        verdict = review_data.get("verdict") or ""
        summary = (review_data.get("summary")
                   or review_data.get("summary_md")
                   or review_data.get("body") or "")
        parts = [f"## Code review: {pr_title}", f"PR: {pr_url}", ""]
        if verdict:
            parts.append(f"**Verdict:** {verdict}")
            parts.append("")
        if summary and not findings:
            parts.append(summary.strip())
            parts.append("")
        inline_comments = []
        if findings:
            parts.append("### Findings")
            parts.append("")
            for f in findings:
                sev = (f.get("severity") or "info").upper()
                path = f.get("path") or "?"
                raw_line = f.get("line") or f.get("start_line")
                # The agent sometimes reports ``line`` as a
                # string ("260") and Gitea's API expects
                # an int. Normalise to int before
                # sending.
                line = None
                if raw_line is not None:
                    try:
                        line = int(raw_line)
                    except (TypeError, ValueError):
                        line = None
                loc = f"{path}:{line}" if line is not None else path
                body = (f.get("body") or f.get("message") or "").strip()
                # ``new_position`` is a file line number
                # — see the Gitea API contract noted in
                # the docstring above. Send the line
                # directly; no diff-position lookup.
                if (diff_text is not None and path != "?"
                    and line is not None and line > 0):
                    norm_path = _normalise_diff_path(path)
                    # Body shows only a reference; the
                    # full text lives in the inline
                    # comment.
                    parts.append(
                        f"- **[{sev}]** `{path}:{line}` — "
                        f"see inline comment"
                    )
                    parts.append("")
                    comment_body = (
                        f"**[{sev}]** at `{path}:{line}`\n\n{body}"
                        if body else
                        f"**[{sev}]** at `{path}:{line}`"
                    )
                    inline_comments.append({
                        "path": norm_path,
                        "body": comment_body,
                        "new_position": line,
                    })
                else:
                    # No inline comment (no path/line, or
                    # non-positive line): include the full
                    # text in the body so reviewers still
                    # see it.
                    parts.append(f"- **[{sev}]** `{loc}`")
                    if body:
                        for bl in body.splitlines():
                            parts.append(f"  {bl}")
                    parts.append("")
        elif not summary:
            parts.append("_No issues found._")
        if diff_text is not None:
            return "\n".join(parts), inline_comments
        return "\n".join(parts)

    def __init__(self, shoggoth):
        self.shoggoth = shoggoth
        self.otlp = OtlpLogger(params.otel_endpoint)
        self.tool_profile = "full"
        self._qwen_restrict_path = None

    def start_session(self, event_type):
        self.tool_profile = Agent.TOOL_PROFILE_BY_SESSION.get(event_type, "full")
        log(f"start_session: event_type={event_type} "
            f"tool_profile={self.tool_profile}")
        self.otlp.start_session(event_type)

        self.qwen_env = self._filter_qwen_env()

        deny_rules = Agent.QWEN_DENY_PATTERNS
        if deny_rules:
            settings_path = f"/tmp/qwen-restrict-{self.otlp.session_id}.json"
            try:
                with open(settings_path, "w") as f:
                    json.dump({"permissions": {"deny": deny_rules}}, f)
                self.qwen_env["QWEN_CODE_SYSTEM_SETTINGS_PATH"] = settings_path
                self._qwen_restrict_path = settings_path
                log(f"start_session: wrote qwen restrictions to {settings_path} "
                    f"({len(deny_rules)} deny rules)")
            except OSError as e:
                print(f"WARNING: failed to write qwen restriction file "
                      f"{settings_path}: {e}", file=sys.stderr)
        else:
            self._qwen_restrict_path = None

        plugin_url = f"http://{self.shoggoth.domain}/plugin.tar.gz"
        plugin_archive = run(["curl", "-sfS", "--max-time", "30", "-o", "/tmp/plugin.tar.gz", plugin_url],
                             check=False, capture_output=True, text=True, timeout=60)
        if plugin_archive.returncode != 0:
            die(f"failed to download plugin from {plugin_url}: {plugin_archive.stderr.strip()}")

        plugin_install = run(["qwen", "extensions", "install", "/tmp/plugin.tar.gz",
                              "--scope", "user", "--consent"],
                             check=False, capture_output=True, text=True, timeout=60)
        if plugin_install.returncode != 0:
            err = (plugin_install.stderr or plugin_install.stdout or "").strip()
            die(f"failed to install plugin from {plugin_url}: {err}")

        index_path = self.shoggoth.repo_dir
        index_result = run(
            ["codebase-memory-mcp", "cli", "index_repository",
             json.dumps({"repo_path": index_path})],
            check=False, capture_output=True, text=True, timeout=120,
        )
        if index_result.returncode != 0:
            print(f"WARNING: codebase-memory-mcp indexing failed: "
                  f"{index_result.stderr.strip()}", file=sys.stderr)

    def prompt(self, text, resume=False, timeout=None, cwd=None):
        if cwd is None:
            if self.shoggoth.type == "ccws":
                cwd = "/ccws"
            else:
                cwd = self.shoggoth.repo_dir
        os.chdir(cwd)
        log(f"prompt: cwd={cwd}")

        cmd = ["qwen", "--yolo", "--output-format", "stream-json"]
        for excluded in Agent.TOOL_PROFILES.get(self.tool_profile, []):
            cmd.extend(["--exclude-tools", excluded])
        if resume:
            cmd.extend(["--resume", self.otlp.session_id])
        else:
            cmd.extend(["--session-id", self.otlp.session_id])
        cmd.extend(["--prompt", Agent.ANTI_LEAK_DIRECTIVE + text])

        log(f"prompt: resume={resume} session={self.otlp.session_id} "
            f"timeout={timeout} tool_profile={self.tool_profile}")
        log(f"prompt: cmd={' '.join(cmd[:6])}... (prompt length={len(text)})")
        log(f"prompt: text:\n{Agent.ANTI_LEAK_DIRECTIVE}{text}")
        log("prompt: starting qwen subprocess")

        def _pump(stream, label):
            try:
                for line in iter(stream.readline, b""):
                    decoded = line.decode(errors="replace")
                    if label == "stdout":
                        sys.stdout.write(decoded)
                        sys.stdout.flush()
                        stripped = decoded.rstrip("\n")
                        self.otlp._push_line(stripped)
                        self.otlp._capture_stream_json(stripped)
                    else:
                        sys.stderr.write(decoded)
                        sys.stderr.flush()
            except Exception as e:
                print(f"WARNING: stream pump {label} crashed: {e}", file=sys.stderr)
            finally:
                stream.close()

        self.otlp.reset()
        qwen = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.qwen_env,
        )
        stdout_thread = threading.Thread(
            target=_pump, args=(qwen.stdout, "stdout"), daemon=True)
        stderr_thread = threading.Thread(
            target=_pump, args=(qwen.stderr, "stderr"), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        timed_out = False
        try:
            qwen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log(f"prompt: TIMEOUT after {timeout}s, killing qwen")
            qwen.kill()
            timed_out = True
            qwen.wait()
        stdout_thread.join(timeout=10)
        stderr_thread.join(timeout=10)
        if timed_out:
            log("prompt: qwen killed due to timeout")
            return 124
        log(f"prompt: qwen exited with code {qwen.returncode}")
        if self.otlp.last_error:
            log(f"prompt: agent reported an error: {str(self.otlp.last_error)[:200]}")
            return 125
        return qwen.returncode

    def prompt_with_retry(self, text, resume=False, timeout=None, cwd=None,
                          max_retries=3, retry_label=None):
        """Wrap prompt() with retry on leaked-thinking-tag errors.

        The bundled qwen-code parser throws
        InvalidStreamError("Model response leaked thinking tags.",
        "PROTOCOL_TAG_LEAK") when the model emits content that resembles an
        unclosed thinking tag. After the bundled protocolTagLeakMaxRetries=2
        the error surfaces as
        [API Error: Model response leaked thinking tags.] in the stream-json
        result event; qwen then exits non-zero and OtlpLogger.last_error is
        set. This wrapper catches that, rotates to a fresh qwen session-id
        so the model has no carryover from the failed run (LLM output is
        non-deterministic — subsequent attempts usually succeed), and retries.
        """
        label = retry_label or "prompt"
        for attempt in range(max_retries + 1):
            if attempt > 0:
                delay = 2 * attempt
                self.otlp._push_line(
                    f"{label}: leaked-thinking-tag error "
                    f"attempt={attempt}/{max_retries} "
                    f"delay={delay}s; rotating session and retrying")
                time.sleep(delay)
                self.otlp.reset()
                self.otlp.session_id = str(uuid.uuid4())
                resume = False
            rc = self.prompt(text, resume=resume, timeout=timeout, cwd=cwd)
            if rc == 0:
                return rc
            err = str(self.otlp.last_error or "")
            if "leaked thinking tags" not in err:
                return rc
            if attempt >= max_retries:
                break
        self.otlp._push_line(
            f"{label}: gave up after {max_retries + 1} attempts "
            f"last_error={(str(self.otlp.last_error or ''))[:200]!r}")
        return rc


    def stop_session(self):
        log("stop_session")
        self.otlp.stop_session()
        if self._qwen_restrict_path:
            try:
                os.unlink(self._qwen_restrict_path)
                log(f"stop_session: removed qwen restrictions file "
                    f"{self._qwen_restrict_path}")
            except OSError as e:
                print(f"WARNING: failed to remove qwen restriction file "
                      f"{self._qwen_restrict_path}: {e}", file=sys.stderr)
            self._qwen_restrict_path = None
