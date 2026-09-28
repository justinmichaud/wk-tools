"""tests/support.py's own environment scrub: every test that shells out
through run()/bash() gets a WK_MACHINES_DIR holding no build machine or peer
and a WK_HOST_SECRETS in a scratch directory, never the real fleet or the real
~/.config/wk/secrets -- a test that forgot to pass its own would otherwise
read or write machine state silently.

Run: python3 -m unittest tests.test_support -v
"""
TIER = "lint"
import os
import stat
import subprocess
import unittest

from tests.support import BLIND_FLEET, NO_CONFIG, NO_SECRETS, _clean_env, bash, stub_path


class TestCleanEnvScrubsMachineState(unittest.TestCase):
    def test_the_default_fleet_holds_no_target(self):
        self.assertEqual(_clean_env()["WK_MACHINES_DIR"], BLIND_FLEET)
        kinds = {l for p in os.listdir(BLIND_FLEET)
                 for l in open(os.path.join(BLIND_FLEET, p)).read().splitlines() if l.startswith("kind=")}
        self.assertTrue(kinds)
        self.assertFalse(kinds & {"kind=build", "kind=peer"})

    def test_no_test_sees_this_machines_config_home(self):
        self.assertEqual(_clean_env()["XDG_CONFIG_HOME"], NO_CONFIG)
        self.assertNotIn("wk", os.listdir(NO_CONFIG))

    def test_no_test_is_the_far_end_of_a_target(self):
        self.assertFalse(os.path.exists(_clean_env()["WK_REMOTE_MARKER"]))

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


@unittest.skipUnless(os.environ.get("WK_TEST_SHIMS"), "the machine-tool shims are tests/run.py's, outside the live tier")
class TestAPathATestHandsInStillReachesNoMachine(unittest.TestCase):
    """A test that hid tart by dropping every PATH entry holding one dropped the runner's shim directory with it,
    and `wk boot benchvm` reached the real podman machine (a hung guest made it a 60s timeout, 2026-09-27)."""

    def test_system_directories_alone_get_the_shims_back(self):
        cp = bash("ssh somehost true; podman machine ssh wk -- true", env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin"})
        self.assertIn("unit tier reached ssh somehost true", cp.stderr)

    def test_a_tool_the_path_lacks_stays_absent(self):
        cp = bash("command -v tart || echo absent", env={"PATH": "/usr/bin:/bin"})
        self.assertEqual("absent\n", cp.stdout, cp.stderr)

    def test_a_tests_own_stub_stays_first(self):
        with stub_path({"podman": "echo stubbed"}) as binp:
            cp = bash("podman machine ssh wk", env={"PATH": "%s:/usr/bin:/bin" % binp})
        self.assertEqual("stubbed\n", cp.stdout, cp.stderr)


if __name__ == "__main__":
    unittest.main()
