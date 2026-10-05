"""`wk key set github-pat` end to end, the arms that need a terminal under a pty (the value is read with `read -rs`).
The token is held beside the private deploy-key halves, in the directory nothing mounts.

Run: python3 -m unittest tests.test_key_github_pat -v
"""
import os
import pty
import select
import subprocess
import termios
import threading
import time
import unittest
from http.server import HTTPServer

from tests.support import REPO, WkTest, clean_env, stub_path
from tests.test_credcheck import CLASSIC, FINE, POLICY, FakeGitHub

KEY = REPO / "cmd" / "key"

FORKS = {"justinmichaud/WebKit": "WebKit/WebKit",
         "justinmichaud/WPEWebKit": "WebPlatformForEmbedded/WPEWebKit"}


def _wait_for_echo_off(fd):
    """A paste written before `read -rs` turns echo off is echoed by the tty itself."""
    while termios.tcgetattr(fd)[3] & termios.ECHO:
        time.sleep(0.005)


PODMAN_TRAP = '#!/bin/sh\necho "podman was called" >&2\nexit 1\n'
TOKEN = "ghp_thisisnotarealtoken0123456789"


class _PatRun(WkTest):
    def setUp(self):
        super().setUp()
        self.store = self.tmp / "store"
        self.secrets = self.store / "secrets"
        self.held = self.store / "push-keys"
        self.store.mkdir()
        self.secrets.mkdir()
        self.held.mkdir()
        self.extra_env = {}

    def _env(self, binp):
        reg = self.tmp / "no-registry"
        reg.mkdir(exist_ok=True)
        return clean_env({"WK_HOST_SECRETS": str(self.secrets),
                          "WK_STORE": str(self.store),
                          "WK_MACHINES_DIR": str(reg),
                          "PATH": f"{binp}:/usr/bin:/bin:/usr/sbin:/sbin",
                          **self.extra_env})

    def key(self, *args):
        with stub_path({"podman": PODMAN_TRAP}) as binp:
            return subprocess.run([str(KEY), *args], cwd=str(REPO),
                                  env=self._env(binp), capture_output=True,
                                  text=True, timeout=120)

    def key_tty(self, *args, paste="", answer="y"):
        """The same command with a real terminal on stdin, and <paste> typed at the prompt."""
        with stub_path({"podman": PODMAN_TRAP}) as binp:
            master, slave = pty.openpty()
            p = subprocess.Popen([str(KEY), *args], cwd=str(REPO),
                                 env=self._env(binp), stdin=slave,
                                 stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            out, sent, answered = b"", False, False
            while True:
                r, _, _ = select.select([master], [], [], 30)
                if not r:
                    break
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
                if not answered and b"[y/N]" in out:
                    os.write(master, (answer + "\n").encode())
                    answered = True
                if not sent and b"paste it" in out:
                    _wait_for_echo_off(master)
                    os.write(master, (paste + "\n").encode())
                    sent = True
            os.close(master)
            rc = p.wait(timeout=30)
        return rc, out.decode(errors="replace")

    def pat(self):
        return self.held / "github-pat"


class TestNothingStoredYet(_PatRun):
    def test_replace_with_nothing_to_replace_names_the_path(self):
        cp = self.key("set", "github-pat", "--replace")
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn(str(self.pat()), cp.stderr)

    def test_an_empty_answer_stores_nothing_and_says_what_that_costs(self):
        rc, out = self.key_tty("set", "github-pat", paste="")
        self.assertNotEqual(rc, 0, out)
        self.assertFalse(self.pat().exists())

    def test_with_no_terminal_it_says_to_re_run_interactively(self):
        cp = self.key("set", "github-pat")
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("Re-run interactively", cp.stderr)
        self.assertFalse(self.pat().exists())


class TestStoringOne(_PatRun):
    def test_it_lands_in_the_directory_nothing_mounts_unechoed_and_only_this_user_reads(self):
        rc, out = self.key_tty("set", "github-pat", paste=TOKEN)
        self.assertEqual(rc, 0, out)
        self.assertNotIn(TOKEN, out)
        self.assertEqual(TOKEN, self.pat().read_text().strip())
        self.assertEqual(0o600, self.pat().stat().st_mode & 0o777)
        self.assertEqual(0o700, self.held.stat().st_mode & 0o777)
        self.assertFalse((self.secrets / "github-pat").exists())

    def test_it_prints_where_it_went_then_what_the_rule_says(self):
        """Two lines: the one-line status, then the verdict (unverified, with no GitHub to ask)."""
        rc, out = self.key_tty("set", "github-pat", paste=TOKEN)
        self.assertEqual(rc, 0, out)
        lines = [l for l in out.splitlines() if l.strip()]
        self.assertIn("github-pat", lines[-2])
        self.assertIn("stored", lines[-2])
        self.assertIn(str(self.pat()), lines[-2])
        self.assertIn("unverified", lines[-1])

class TestReplacingOne(_PatRun):
    def setUp(self):
        super().setUp()
        self.pat().write_text("ghp_theoldone\n")
        self.pat().chmod(0o600)

    def test_a_bare_set_reports_it_rather_than_asking_again(self):
        cp = self.key("set", "github-pat")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("ghp_theoldone", cp.stdout + cp.stderr)
        self.assertRegex(cp.stderr, r"github-pat\s+stored\s+%s" % str(self.pat()))
        self.assertEqual("ghp_theoldone", self.pat().read_text().strip())

    def test_replace_removes_the_old_one_first_and_says_to_revoke_it(self):
        rc, out = self.key_tty("set", "github-pat", "--replace", paste=TOKEN)
        self.assertEqual(rc, 0, out)
        self.assertIn("revoke it too if it is still live", out)
        self.assertEqual(TOKEN, self.pat().read_text().strip())

    def test_replace_asks_first_and_a_no_keeps_the_old_one(self):
        rc, out = self.key_tty("set", "github-pat", "--replace", paste=TOKEN, answer="n")
        self.assertNotEqual(rc, 0, out)
        self.assertEqual("ghp_theoldone", self.pat().read_text().strip())

    def test_replace_without_a_terminal_declines(self):
        cp = self.key("set", "github-pat", "--replace")
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual("ghp_theoldone", self.pat().read_text().strip())

    def test_replace_with_an_empty_answer_leaves_none(self):
        rc, out = self.key_tty("set", "github-pat", "--replace", paste="")
        self.assertNotEqual(rc, 0, out)
        self.assertFalse(self.pat().exists())


class TestTheStandingReadTokenReachesTheMachine(_PatRun):
    """Storing, rotating and withdrawing converge the read copy (tests/test_push_agent.py); these are the edges."""

    def read_pat(self):
        return self.store / "read-github-pat"

    @unittest.skipUnless(os.uname().sysname == "Darwin",
                         "the injector that serves the guests is a macOS host's")
    def test_the_guests_injector_takes_it_from_this_store_and_no_other(self):
        """Measured: the guests' half once wrote the real injector's copy, not this store's."""
        rc, out = self.key_tty("set", "github-pat", paste=TOKEN)
        self.assertEqual(rc, 0, out)
        self.assertEqual(TOKEN,
                         (self.store / "vm" / "read-github-pat").read_text().strip())

    def test_a_machine_that_cannot_take_it_is_a_warning_naming_the_other_delivery(self):
        self.extra_env = {
            "WK_PUSH_READ_PAT_FILE": str(self.tmp / "not-a-store" / "read-pat")}
        rc, out = self.key_tty("set", "github-pat", paste=TOKEN)
        self.assertEqual(rc, 0, out)
        self.assertEqual(TOKEN, self.pat().read_text().strip())
        self.assertIn("./setup", out)


class TestWhatTheTokenCanDoDecidesWhetherItIsKept(_PatRun):
    """The rule's verdict (tests/test_credcheck.py holds every branch) decides whether the pasted token is kept."""

    def setUp(self):
        super().setUp()
        self.server = HTTPServer(("127.0.0.1", 0), FakeGitHub)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        FakeGitHub.reset(user_status=200, repos=list(FORKS),
                         pulls=dict.fromkeys(
                             list(FORKS) + list(FORKS.values()), 422),
                         parents=dict(FORKS), repo_message=POLICY)
        self.extra_env = {
            "WK_GITHUB_API": "http://127.0.0.1:%d" % self.server.server_port}

    def test_a_token_that_can_open_a_pull_request_is_stored(self):
        rc, out = self.key_tty("set", "github-pat", paste=FINE)
        self.assertEqual(rc, 0, out)
        self.assertEqual(FINE, self.pat().read_text().strip())
        self.assertNotIn(FINE, out)

    def test_a_token_without_pull_request_write_stores_nothing(self):
        FakeGitHub.pulls = {"justinmichaud/WebKit": 403}
        rc, out = self.key_tty("set", "github-pat", paste=FINE)
        self.assertNotEqual(rc, 0, out)
        self.assertFalse(self.pat().exists())
        self.assertIn("settings/tokens/new", out)
        self.assertNotIn(FINE, out)

    def test_a_classic_token_is_kept_and_its_reach_named(self):
        FakeGitHub.scopes = "repo"
        rc, out = self.key_tty("set", "github-pat", paste=CLASSIC)
        self.assertEqual(rc, 0, out)
        self.assertEqual(CLASSIC, self.pat().read_text().strip())

    def test_an_unreachable_api_stores_it_and_says_it_is_unverified(self):
        self.extra_env = {"WK_GITHUB_API": "http://127.0.0.1:1"}
        rc, out = self.key_tty("set", "github-pat", paste=FINE)
        self.assertEqual(rc, 0, out)
        self.assertEqual(FINE, self.pat().read_text().strip())
        self.assertIn("unverified", out)

    def test_a_stored_token_is_reported_from_a_fresh_answer(self):
        self.pat().write_text(FINE + "\n")
        self.pat().chmod(0o600)
        FakeGitHub.pulls = {"justinmichaud/WebKit": 403}
        cp = self.key("set", "github-pat")
        self.assertEqual(1, cp.returncode, cp.stdout + cp.stderr)

if __name__ == "__main__":
    unittest.main()


class TestATokenGitHubRefusesIsReplaced(_PatRun):
    """A stored token GitHub answers 401 for: `wk key setup` without a terminal names the remedy and keeps the file."""

    def setUp(self):
        super().setUp()
        self.server = HTTPServer(("127.0.0.1", 0), FakeGitHub)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        FakeGitHub.reset(user_status=401, repos=list(FORKS),
                         pulls=dict.fromkeys(
                             list(FORKS) + list(FORKS.values()), 422),
                         parents=dict(FORKS), repo_message=POLICY)
        self.extra_env = {
            "WK_GITHUB_API": "http://127.0.0.1:%d" % self.server.server_port}
        self.pat().write_text("ghp_revokedone\n")
        self.pat().chmod(0o600)

    def _key_with_gh_refusing(self, *args):
        from tests.test_key import GH_REFUSES
        with stub_path({"podman": PODMAN_TRAP, "gh": GH_REFUSES}) as binp:
            return subprocess.run([str(KEY), *args], cwd=str(REPO),
                                  env=self._env(binp), capture_output=True,
                                  text=True, timeout=120)

    def test_setup_names_the_refused_token_and_how_to_replace_it(self):
        cp = self._key_with_gh_refusing("setup")
        out = cp.stdout + cp.stderr
        self.assertIn("wk key set github-pat --replace", out)
        self.assertNotRegex(out, r"github-pat\s+stored\s")
        self.assertEqual("ghp_revokedone", self.pat().read_text().strip(),
                         "a run with no terminal removed the token unasked")

    def test_deploy_leaves_the_token_alone_and_still_reports_it(self):
        cp = self._key_with_gh_refusing("deploy")
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertEqual("ghp_revokedone", self.pat().read_text().strip())

    def test_a_token_github_accepts_is_left_alone(self):
        FakeGitHub.user_status = 200
        FakeGitHub.pulls = {"justinmichaud/WebKit": 201, "justinmichaud/WPEWebKit": 201}
        cp = self._key_with_gh_refusing("setup")
        self.assertRegex(cp.stdout + cp.stderr, r"github-pat\s+stored\s")


class TestTheMachineTakesTheTokenOnEveryStart(unittest.TestCase):
    def test_a_container_start_converges_the_read_token(self):
        from unittest import mock
        from wk import secrets, targets
        from wk.machine import Fake
        c = targets.Container("container", str(REPO), {"HOME": "/nonexistent", "WK_STORE": "/nonexistent/store"}, Fake("here"))
        with mock.patch.object(secrets.Secrets, "pat_converge_machine") as converge:
            c.start("demo")
        self.assertEqual(1, converge.call_count, "'wk start <container workspace>' does not converge the read token")
