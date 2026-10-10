"""Unit tests for the pure helpers in ``shoggoth.util``.

The helpers tested here (``log``, ``die``, ``_redact_argv_for_log``,
``run``, ``normalize_for_branch``) are the leaf-most layer of the
package — no HTTP, no Gitea, no K8s. The tests run without any
external service, which is the whole point: the only thing that
matters is that the helpers do what their docstring says, even
after the file was split from the original 7.5k-line monolith.

Capture note: ``log()`` resolves its default ``file=sys.stderr``
at call time (a small fix from the original
``file=sys.stderr``-as-default-arg, which made the function
impervious to ``sys.stderr`` replacement after import). That
fix is what lets these tests use ``capsys`` cleanly.
"""
import io
import os
import subprocess
import sys

import pytest

# Make the package importable when running ``pytest`` from the
# scripts directory (no install step; the package lives alongside
# the shim). Insert the scripts dir, not the parent, so
# ``import shoggoth`` resolves to the package rather than the
# shim file.
SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, SCRIPTS_DIR)

from shoggoth.util import (  # noqa: E402
    _redact_argv_for_log,
    die,
    log,
    normalize_for_branch,
    run,
)


# ----- log --------------------------------------------------------

class TestLog:
    def test_single_line(self, capsys):
        log("hello")
        captured = capsys.readouterr()
        assert captured.err == "[shoggoth] hello\n"

    def test_multiline_each_line_prefixed(self, capsys):
        log("a\nb\nc")
        captured = capsys.readouterr()
        assert captured.err == "[shoggoth] a\n[shoggoth] b\n[shoggoth] c\n"

    def test_empty_string_emits_one_blank_line(self, capsys):
        log("")
        captured = capsys.readouterr()
        # `log("")` -> "".splitlines() == [] -> falsy -> or [""] ->
        # one loop iteration with line="".
        assert captured.err == "[shoggoth] \n"

    def test_writes_to_stdout_when_asked(self, capsys):
        log("hi", file=sys.stdout)
        captured = capsys.readouterr()
        assert captured.out == "[shoggoth] hi\n"
        assert captured.err == ""

    def test_explicit_stringio(self):
        # The ``file=`` override path stays available for tests
        # and for any future caller that wants to redirect.
        buf = io.StringIO()
        log("redirected", file=buf)
        assert buf.getvalue() == "[shoggoth] redirected\n"


# ----- die --------------------------------------------------------

class TestDie:
    def test_exits_with_code_one(self, capsys):
        with pytest.raises(SystemExit) as exc:
            die("boom")
        assert exc.value.code == 1

    def test_message_prefixed_with_error(self, capsys):
        with pytest.raises(SystemExit):
            die("boom")
        captured = capsys.readouterr()
        assert captured.err == "ERROR: boom\n"

    def test_passes_message_through_unchanged(self, capsys):
        with pytest.raises(SystemExit):
            die("openldap-ldapauth-password is required for LDAP bind "
                "(grant the script's SA `get` on the K8s Secret)")
        captured = capsys.readouterr()
        assert "openldap-ldapauth-password is required" in captured.err


# ----- _redact_argv_for_log ---------------------------------------

class TestRedactArgvForLog:
    def test_no_secrets_passthrough(self):
        cmd = ["kubectl", "get", "pods", "-n", "shoggoth"]
        assert _redact_argv_for_log(cmd) == cmd

    def test_kv_form_redacts_value(self):
        cmd = ["curl", "--header=Authorization: token SECRET",
               "https://example.test"]
        out = _redact_argv_for_log(cmd)
        assert out[0] == "curl"
        assert out[1] == "--header=***"
        assert out[2] == "https://example.test"

    def test_kv_form_redacts_redmine_api_key(self):
        # The redactor masks the value following a colon-separated
        # HTTP-header-style secret (``Authorization:``,
        # ``X-Vault-Token:``, ``X-Redmine-API-Key:``). The whole
        # ``Key: Value`` form is collapsed to ``Key: ***`` so
        # neither the colon-prefix nor the secret value lands in
        # the Argo workflow log. The test exercises the natural
        # ``curl -H "X-Redmine-API-Key: VALUE"`` form.
        cmd = ["curl", "-H", "X-Redmine-API-Key: VALUE"]
        out = _redact_argv_for_log(cmd)
        assert out == ["curl", "-H", "X-Redmine-API-Key: ***"]
        assert "VALUE" not in out[2]

    def test_eq_form_then_value(self):
        """--from-literal VALUE: the --from-literal is preserved
        and the next token is replaced with ***.
        """
        cmd = ["kubectl", "create", "secret", "generic", "foo",
               "--from-literal", "key=SECRET"]
        out = _redact_argv_for_log(cmd)
        assert out[:5] == ["kubectl", "create", "secret", "generic", "foo"]
        assert out[5] == "--from-literal"
        assert out[6] == "***"

    def test_short_flag_password(self):
        cmd = ["mysql", "-u", "root", "-p", "hunter2"]
        out = _redact_argv_for_log(cmd)
        assert out == ["mysql", "-u", "***", "-p", "***"]

    def test_user_password_flags_redact_next_arg(self):
        cmd = ["psql", "--user", "admin", "--password", "secret"]
        out = _redact_argv_for_log(cmd)
        assert out == ["psql", "--user", "***", "--password", "***"]

    def test_token_eq_form(self):
        cmd = ["gh", "auth", "login", "--token=ghp_xxx"]
        out = _redact_argv_for_log(cmd)
        assert out == ["gh", "auth", "login", "--token=***"]

    def test_does_not_mutate_input(self):
        cmd = ["kubectl", "--from-literal=KEY=VAL"]
        snapshot = list(cmd)
        _redact_argv_for_log(cmd)
        assert cmd == snapshot

    def test_unknown_long_flag_passes_through(self):
        cmd = ["git", "--no-pager", "log"]
        assert _redact_argv_for_log(cmd) == cmd

    def test_partial_kv_prefix_passes_through(self):
        # --from-literal-foo=bar does NOT match the
        # ``--from-literal=`` prefix because position 14 is
        # ``-``, not ``=``. The token passes through unredacted.
        # The test pins this so a future tweak (e.g. adding a
        # regex match instead of a literal prefix) is conscious.
        cmd = ["some-tool", "--from-literal-foo=bar"]
        out = _redact_argv_for_log(cmd)
        assert out == ["some-tool", "--from-literal-foo=bar"]


# ----- run --------------------------------------------------------

class TestRun:
    def test_returns_completed_process_with_check_true(self):
        result = run(["true"])
        assert isinstance(result, subprocess.CompletedProcess)
        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == ""

    def test_captures_stdout(self):
        result = run(["echo", "hello"])
        assert result.stdout.strip() == "hello"

    def test_captures_stderr(self):
        result = run(["sh", "-c", "echo bad >&2; exit 1"], check=False)
        assert result.returncode == 1
        assert "bad" in result.stderr

    def test_check_true_raises_on_nonzero(self):
        with pytest.raises(subprocess.CalledProcessError) as exc:
            run(["false"])
        assert exc.value.returncode == 1

    def test_check_false_does_not_raise(self):
        result = run(["false"], check=False)
        assert result.returncode == 1

    def test_file_not_found_returns_synthetic_127(self, capsys):
        # ``run`` swallows FileNotFoundError and returns a synthetic
        # CompletedProcess with rc=127 so callers can treat "kubectl
        # not installed" the same as "kubectl failed". This is the
        # contract ``K8s.read_secret`` relies on when the slave
        # image's container lacks kubectl.
        result = run(["definitely-not-a-real-binary-xyz"], check=False)
        assert result.returncode == 127
        assert "not found" in result.stderr
        # The synthetic CompletedProcess surfaces in the log
        # channel too (rc=127 line).
        captured = capsys.readouterr()
        assert "127" in captured.err or "not found" in captured.err

    def test_redacts_secrets_in_log_channel(self, capsys):
        # ``run()`` redacts the argv before logging the
        # ``[shoggoth] run: ...`` line so secret-bearing tokens
        # (``--from-literal=KEY=VAL``, ``-p password``, ...) never
        # land in the Argo log. The stdout row is *not* run
        # through the redactor — the redactor operates on argv
        # only, and the captured subprocess output is the
        # program's own output (echo just prints its argv, so
        # the unredacted form reappears). The test pins that
        # behavior so a future change to either side is
        # conscious.
        run(["echo", "--from-literal=KEY=VAL"])
        captured = capsys.readouterr()
        lines = captured.err.splitlines()
        run_line = next(line for line in lines
                        if line.startswith("[shoggoth] run: echo"))
        stdout_line = next(line for line in lines
                           if line.startswith("[shoggoth] run: stdout:"))
        # The argv-log row uses the redacted form.
        assert run_line == "[shoggoth] run: echo --from-literal=***"
        # The stdout row is the program output (echo prints the
        # raw argv) and therefore contains the unredacted form.
        # Pinning this prevents an over-eager future patch from
        # silently re-running the redactor on captured output.
        assert "KEY=VAL" in stdout_line

    def test_kwargs_forwarded_to_subprocess(self):
        result = run(["sh", "-c", "echo $MY_VAR"],
                     env={"MY_VAR": "hello", "PATH": os.environ["PATH"]})
        assert result.stdout.strip() == "hello"

    def test_truncates_long_stdout_in_log(self, capsys):
        # The stdout/stderr log rows are capped at 500 chars so a
        # runaway command doesn't blow up the Argo log. The "run:"
        # argv line is *not* truncated (it's bounded by what
        # ``_redact_argv_for_log`` produces, which for a single
        # echo arg is the literal arg). The test pins the
        # stdout-row truncation explicitly.
        long = "x" * 2000
        run(["echo", long])
        captured = capsys.readouterr()
        lines = captured.err.splitlines()
        stdout_line = next(line for line in lines if line.startswith(
            "[shoggoth] run: stdout:"))
        # The stdout row is capped at 500 chars. With a 2000-x
        # payload, the row contains 500 x's, not 2000. We
        # assert exactly 500 consecutive x's in the row.
        assert stdout_line.count("x") == 500
        # The "run:" line is not truncated, so it should contain
        # the full 2000-x payload.
        run_line = next(line for line in lines if line.startswith(
            "[shoggoth] run: echo"))
        assert run_line.count("x") == 2000


# ----- normalize_for_branch --------------------------------------

class TestNormalizeForBranch:
    @pytest.mark.parametrize("text,expected", [
        ("feature/My-Feature", "feature-my-feature"),
        ("Hello World!", "hello-world"),
        ("  leading-and-trailing  ", "leading-and-trailing"),
        ("multiple---dashes", "multiple-dashes"),
        ("aBc123", "abc123"),
        ("special!@#chars", "special-chars"),
        ("", ""),
        ("--already-dashed--", "already-dashed"),
        ("UPPER CASE", "upper-case"),
        ("snake_case_name", "snake-case-name"),
    ])
    def test_table_driven(self, text, expected):
        assert normalize_for_branch(text) == expected

    def test_collapse_consecutive_dashes(self):
        # The intermediate regex strips non-alphanumerics, leaving
        # multiple dashes; the second regex collapses them.
        assert normalize_for_branch("a - b - c") == "a-b-c"

    def test_strip_leading_and_trailing_dashes(self):
        assert normalize_for_branch("---hello---") == "hello"


# ----- smoke: WORKFLOW/MAINTENANCE command set coverage ----------

class TestCommandSetDisjoint:
    """The script's command-line surface is the union of
    WORKFLOW_COMMANDS and MAINTENANCE_COMMANDS; the two sets must
    stay disjoint so the dispatch path can branch on
    ``args.command in WORKFLOW_COMMANDS`` without false positives.
    """

    def test_workflow_and_maintenance_sets_disjoint(self):
        from shoggoth.state import MAINTENANCE_COMMANDS, WORKFLOW_COMMANDS
        overlap = WORKFLOW_COMMANDS & MAINTENANCE_COMMANDS
        assert not overlap, f"overlap: {overlap}"

    def test_expected_workflow_commands(self):
        from shoggoth.state import WORKFLOW_COMMANDS
        assert WORKFLOW_COMMANDS == {"task", "ci-failure", "pr-update"}

    def test_expected_maintenance_commands(self):
        from shoggoth.state import MAINTENANCE_COMMANDS
        assert MAINTENANCE_COMMANDS == {
            "argo-webhooks", "redmine-webhooks", "redmine-argo-webhooks",
            "github-mirror-sync", "slave-access", "slave-token",
            "runner-token", "ssh-key", "slave-test", "gitea-ldap-sync",
            "redmine-ldap-sync", "pushsecret-check",
        }
