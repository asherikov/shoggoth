"""Unit tests for ``shoggoth.config`` (Parameters, SecretStore).

The Parameters class is the canonical map of every env var the
script reads. The tests pin the schema: a missing required var
dies at construction time, optional vars pick up their defaults,
and derived/composite values (numeric coercion, LDAP DN builder)
behave as the docstring says.

SecretStore is a thin wrapper around K8s.read_secret and is
exercised by integration tests; here we only test the schema
constants (K8S_PATHS coverage of the documented kv paths) because
the read path requires a live cluster.
"""
import os
import sys

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, SCRIPTS_DIR)

# Importing shoggoth triggers Parameters() construction (which reads
# the env). The tests below mutate os.environ to control what
# Parameters sees, but the package import happens first — so we
# set SHOGGOTH_DOMAIN before importing the package, then reload
# Parameters after each env-var change.
os.environ.setdefault("SHOGGOTH_DOMAIN", "example.test")

from shoggoth import config as config_mod  # noqa: E402
from shoggoth.config import Parameters, SecretStore  # noqa: E402


# ----- Parameters schema -----------------------------------------

class TestParametersSchema:
    """Pin the GROUPS schema: required vars fail on empty, optional
    vars take their declared default.
    """

    def test_all_required_vars_die_when_unset(self, monkeypatch, capsys):
        # Find every (attr, env_var, required, default) entry with
        # required=True and confirm the constructor dies without it.
        required_entries = [
            (attr, env)
            for entries in Parameters.GROUPS.values()
            for (attr, env, required, _default) in entries
            if required
        ]
        assert required_entries, "no required entries declared"
        for attr, env in required_entries:
            monkeypatch.delenv(env, raising=False)
            with pytest.raises(SystemExit) as exc:
                Parameters()
            assert exc.value.code == 1
            captured = capsys.readouterr()
            assert env in captured.err
            # Re-set so subsequent iterations / other tests can
            # construct Parameters() normally.
            monkeypatch.setenv(env, "dummy")

    def test_optional_var_picks_up_default(self, monkeypatch):
        # ``gitea_ssh_host`` is optional with default "git".
        monkeypatch.delenv("GITEA_SSH_HOST", raising=False)
        monkeypatch.setenv("SHOGGOTH_DOMAIN", "example.test")
        p = Parameters()
        assert p.gitea_ssh_host == "git"

    def test_optional_var_overrides_default(self, monkeypatch):
        monkeypatch.setenv("GITEA_SSH_HOST", "git.example.com")
        monkeypatch.setenv("SHOGGOTH_DOMAIN", "example.test")
        p = Parameters()
        assert p.gitea_ssh_host == "git.example.com"

    def test_empty_optional_var_falls_back_to_default(self, monkeypatch):
        # os.environ.get returns "" when the var is set-but-empty;
        # the constructor's `value or default` clause then picks the
        # default. This is the behavior the original script relies
        # on for the workflow pods that pass empty env vars.
        monkeypatch.setenv("GITEA_SSH_HOST", "")
        monkeypatch.setenv("SHOGGOTH_DOMAIN", "example.test")
        p = Parameters()
        assert p.gitea_ssh_host == "git"

    def test_github_api_delay_coerced_to_float(self, monkeypatch):
        # The constructor runs ``float(self.shoggoth_github_api_delay)``
        # so callers can multiply/divide by it. A typo'd "2s" should
        # raise, surfacing the misconfiguration at import time
        # rather than at the first API call.
        monkeypatch.setenv("SHOGGOTH_GITHUB_API_DELAY", "2")
        monkeypatch.setenv("SHOGGOTH_DOMAIN", "example.test")
        p = Parameters()
        assert p.shoggoth_github_api_delay == 2.0
        assert isinstance(p.shoggoth_github_api_delay, float)

    def test_invalid_api_delay_raises(self, monkeypatch):
        monkeypatch.setenv("SHOGGOTH_GITHUB_API_DELAY", "two-seconds")
        monkeypatch.setenv("SHOGGOTH_DOMAIN", "example.test")
        with pytest.raises(ValueError):
            Parameters()


# ----- Parameters.resolve_ldap_dn --------------------------------

class TestResolveLdapDn:
    def test_single_component(self, monkeypatch):
        monkeypatch.setenv("SHOGGOTH_DOMAIN", "localhost")
        p = Parameters()
        assert p.resolve_ldap_dn("ignored") == "ou=people,dc=localhost"

    def test_two_components(self, monkeypatch):
        # The class fixture uses example.org; this test method
        # explicitly sets its own so the assertion matches what
        # the fixture would have provided.
        monkeypatch.setenv("SHOGGOTH_DOMAIN", "example.org")
        p = Parameters()
        assert p.resolve_ldap_dn("ignored") == "ou=people,dc=example,dc=org"

    def test_three_components(self, monkeypatch):
        monkeypatch.setenv("SHOGGOTH_DOMAIN", "a.b.c")
        p = Parameters()
        assert p.resolve_ldap_dn("ignored") == "ou=people,dc=a,dc=b,dc=c"

    def test_empty_domain_dies_at_parameters_construction(self, monkeypatch):
        # ``Parameters.__init__`` rejects empty required vars at
        # construction time, so the empty-domain path is enforced
        # one frame earlier than ``resolve_ldap_dn`` itself. The
        # test pins that — the resolve_ldap_dn-time check is
        # defensive (re-runs against an already-constructed
        # instance would also catch it, but in practice the
        # constructor is the gate).
        monkeypatch.setenv("SHOGGOTH_DOMAIN", "")
        with pytest.raises(SystemExit) as exc:
            Parameters()
        assert exc.value.code == 1

    def test_base_arg_is_ignored(self):
        # The ``base`` arg is reserved for a future API that takes
        # the RDN (e.g. ``ou=users``); today's contract is
        # ``ou=people,dc=...``. The arg is accepted but ignored —
        # the test pins that.
        p = Parameters()
        a = p.resolve_ldap_dn("anything")
        b = p.resolve_ldap_dn("something-else")
        assert a == b


# ----- Parameters.api_delay --------------------------------------

class TestApiDelay:
    def test_returns_configured_value(self, monkeypatch):
        monkeypatch.setenv("SHOGGOTH_DOMAIN", "example.test")
        monkeypatch.setenv("SHOGGOTH_GITHUB_API_DELAY", "3.5")
        p = Parameters()
        assert p.api_delay() == 3.5

    def test_default(self, monkeypatch):
        monkeypatch.delenv("SHOGGOTH_GITHUB_API_DELAY", raising=False)
        monkeypatch.setenv("SHOGGOTH_DOMAIN", "example.test")
        p = Parameters()
        assert p.api_delay() == 2.0


# ----- SecretStore.K8S_PATHS schema -----------------------------

class TestSecretStoreSchema:
    """The K8s<->OpenBao kv-path map is the contract between
    writers (K8s.upsert_secret) and readers (K8s.read_secret).
    A missing entry means a secret can't be read; an extra
    entry means a typo'd kv path is silently treated as real.
    """

    EXPECTED_PATHS = {
        "gitea/server-token": ("gitea-server-token", "token"),
        "gitea/slave-token": ("gitea-slave-token", "token"),
        "redmine/slave-token": ("redmine-slave-token", "token"),
        "openldap/slave-password": ("openldap-slave-password", "password"),
        "openldap/admin-password": ("openldap-admin-password", "password"),
        "openldap/ldapauth-password": ("openldap-ldapauth-password", "password"),
        "ssh/slave-public-key": ("ssh-slave-public-key", "value"),
    }

    def test_expected_paths_present(self):
        for path, expected in self.EXPECTED_PATHS.items():
            assert path in SecretStore.K8S_PATHS, \
                f"missing kv path: {path}"
            name, key = SecretStore.K8S_PATHS[path]
            assert name == expected[0], \
                f"wrong secret name for {path}: {name} != {expected[0]}"
            assert key == expected[1], \
                f"wrong data key for {path}: {key} != {expected[1]}"

    def test_no_unexpected_paths(self):
        # Symmetric: if someone adds a new path, both this test
        # and the EXPECTED_PATHS table need updating together.
        assert set(SecretStore.K8S_PATHS.keys()) == set(
            self.EXPECTED_PATHS.keys()
        ), (
            "extra kv paths in SecretStore.K8S_PATHS not declared in "
            "tests/test_config.py::TestSecretStoreSchema.EXPECTED_PATHS"
        )

    def test_all_values_are_two_tuples_of_strings(self):
        for path, value in SecretStore.K8S_PATHS.items():
            assert isinstance(value, tuple) and len(value) == 2, (
                f"{path}: K8S_PATHS values must be (name, key) tuples"
            )
            name, key = value
            assert isinstance(name, str) and name, (
                f"{path}: K8s Secret name must be a non-empty string"
            )
            assert isinstance(key, str) and key, (
                f"{path}: K8s data key must be a non-empty string"
            )
