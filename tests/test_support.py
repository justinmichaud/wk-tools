"""tests/support.py's own environment scrub: every test that shells out
through run()/bash() gets a WK_TARGET_REGISTRY and a WK_HOST_SECRETS that
point at scratch directories, never at the real fleet registry or the real
~/.config/wk/secrets -- a test that forgot to pass its own would otherwise
read or write machine state silently.

Run: python3 -m unittest tests.test_support -v
"""
import os
import stat
import subprocess
import unittest

from tests.support import NO_REGISTRY, NO_SECRETS, _clean_env


class TestCleanEnvScrubsMachineState(unittest.TestCase):
    def test_wk_target_registry_defaults_to_the_empty_fake_registry(self):
        self.assertEqual(_clean_env()["WK_TARGET_REGISTRY"], NO_REGISTRY)

    def test_wk_host_secrets_defaults_to_a_scratch_dir(self):
        env = _clean_env()
        self.assertEqual(env["WK_HOST_SECRETS"], NO_SECRETS)
        self.assertNotIn(".config/wk", env["WK_HOST_SECRETS"])

    def test_a_callers_own_value_still_wins(self):
        env = _clean_env({"WK_HOST_SECRETS": "/explicit/scratch/secrets"})
        self.assertEqual(env["WK_HOST_SECRETS"], "/explicit/scratch/secrets")


class TestNothingATestStartsReadsAStartupFile(unittest.TestCase):
    """A bash built with SSH_SOURCE_BASHRC sources ~/.bashrc in a
    non-interactive shell whose stdin is a connected socket and whose SHLVL is
    below 2 -- so with a socketpair on the runner's stdin the machine's rc
    rewrote the PATH of every command a test handed a hand-built env. Both
    halves: the fd tests/support.py guarantees, and what a test observes."""

    def test_the_suites_stdin_is_not_a_socket(self):
        self.assertFalse(stat.S_ISSOCK(os.fstat(0).st_mode))

    def test_a_bash_a_test_starts_keeps_the_path_it_was_handed(self):
        cp = subprocess.run(["bash", "-c", 'printf %s "$PATH"'],
                            env={"PATH": "/usr/bin:/bin"},
                            capture_output=True, text=True, timeout=60)
        self.assertEqual("/usr/bin:/bin", cp.stdout, cp.stderr)


if __name__ == "__main__":
    unittest.main()
