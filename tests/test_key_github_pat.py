"""`wk key set github-pat` end to end, the arms that need a terminal under a pty (the value is read with `read -rs`)."""
import os
import pty
import select
import subprocess
import termios
import time
import unittest

from tests.support import REPO, WkTest, clean_env, stub_path
from tests.test_credcheck import CLASSIC, FINE, POLICY, FakeGitHub, serve

KEY = REPO / "cmd" / "key"

FORKS = {"justinmichaud/WebKit": "WebKit/WebKit", "justinmichaud/WPEWebKit": "WebPlatformForEmbedded/WPEWebKit"}


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
        for d in (self.store, self.secrets, self.held):
            d.mkdir()
        self.extra_env = {}

    def _env(self, binp):
        reg = self.tmp / "no-registry"
        reg.mkdir(exist_ok=True)
        return clean_env({"WK_HOST_SECRETS": str(self.secrets), "WK_STORE": str(self.store), "WK_MACHINES_DIR": str(reg),
                          "PATH": f"{binp}:/usr/bin:/bin:/usr/sbin:/sbin", **self.extra_env})

    def key(self, *args, stubs=None):
        with stub_path({"podman": PODMAN_TRAP, **(stubs or {})}) as binp:
            return subprocess.run([str(KEY), *args], cwd=str(REPO), env=self._env(binp), capture_output=True, text=True,
                                  timeout=120)

    def key_tty(self, *args, paste="", answer="y"):
        """The same command with a real terminal on stdin, and <paste> typed at the prompt."""
        with stub_path({"podman": PODMAN_TRAP}) as binp:
            master, slave = pty.openpty()
            p = subprocess.Popen([str(KEY), *args], cwd=str(REPO), env=self._env(binp), stdin=slave, stdout=slave,
                                 stderr=slave, close_fds=True)
            os.close(slave)
            out, sent, answered = b"", False, False
            while True:
                r, _, _ = select.select([master], [], [], 30)
                try:
                    chunk = os.read(master, 4096) if r else b""
                except OSError:
                    chunk = b""
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

    def holding(self, token):
        self.pat().write_text(token + "\n")
        self.pat().chmod(0o600)

    def pasted(self, args, paste, kept, ok, phrases=(), answer="y"):
        """A paste at the prompt: `kept` is what the store holds after, None nothing; the paste is never echoed."""
        rc, out = self.key_tty("set", "github-pat", *args, paste=paste, answer=answer)
        self.assertEqual(ok, rc == 0, out)
        self.assertEqual(kept, self.pat().read_text().strip() if self.pat().exists() else None, out)
        if paste:
            self.assertNotIn(paste, out)
        for phrase in phrases:
            self.assertIn(phrase, out)
        return out

    def github(self, user_status):
        self.api = serve(FakeGitHub, self.addCleanup)
        FakeGitHub.reset(user_status=user_status, repos=list(FORKS), pulls=dict.fromkeys(list(FORKS) + list(FORKS.values()), 422),
                         parents=dict(FORKS), repo_message=POLICY)
        self.extra_env = {"WK_GITHUB_API": self.api}


class TestNothingStoredYet(_PatRun):
    def test_without_a_terminal_or_anything_to_replace_it_is_refused(self):
        for args, phrase in (((), "Re-run interactively"), (("--replace",), str(self.pat()))):
            with self.subTest(args=args):
                cp = self.key("set", "github-pat", *args)
                self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertIn(phrase, cp.stderr)
                self.assertFalse(self.pat().exists())

    def test_an_empty_answer_stores_nothing(self):
        self.pasted((), "", None, False)

    def test_it_lands_unechoed_where_nothing_mounts_and_its_status_then_verdict_are_the_last_lines(self):
        out = self.pasted((), TOKEN, TOKEN, True)
        self.assertEqual(0o600, self.pat().stat().st_mode & 0o777)
        self.assertEqual(0o700, self.held.stat().st_mode & 0o777)
        self.assertFalse((self.secrets / "github-pat").exists())
        lines = [l for l in out.splitlines() if l.strip()]
        for word in ("github-pat", "stored", str(self.pat())):
            self.assertIn(word, lines[-2])
        self.assertIn("unverified", lines[-1])


class TestReplacingOne(_PatRun):
    def setUp(self):
        super().setUp()
        self.holding("ghp_theoldone")

    def test_a_bare_set_reports_it_rather_than_asking_again_and_replace_without_a_terminal_declines(self):
        cp = self.key("set", "github-pat")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("ghp_theoldone", cp.stdout + cp.stderr)
        self.assertRegex(cp.stderr, r"github-pat\s+stored\s+%s" % str(self.pat()))
        cp = self.key("set", "github-pat", "--replace")
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual("ghp_theoldone", self.pat().read_text().strip())

    def test_replace_asks_first_removes_the_old_one_and_says_to_revoke_it(self):
        self.pasted(("--replace",), TOKEN, "ghp_theoldone", False, answer="n")
        self.pasted(("--replace",), TOKEN, TOKEN, True, ("revoke it too if it is still live",))

    def test_replace_with_an_empty_answer_leaves_none(self):
        self.pasted(("--replace",), "", None, False)


class TestTheStandingReadTokenReachesTheMachine(_PatRun):
    """Storing, rotating and withdrawing converge the read copy (tests/test_push_keys.py); these are the edges."""

    @unittest.skipUnless(os.uname().sysname == "Darwin", "the injector that serves the guests is a macOS host's")
    def test_the_guests_injector_takes_it_from_this_store_and_no_other(self):
        self.pasted((), TOKEN, TOKEN, True)
        self.assertEqual(TOKEN, (self.store / "vm" / "read-github-pat").read_text().strip())

    def test_a_machine_that_cannot_take_it_is_a_warning_naming_the_other_delivery(self):
        self.extra_env = {"WK_PUSH_READ_PAT_FILE": str(self.tmp / "not-a-store" / "read-pat")}
        self.pasted((), TOKEN, TOKEN, True, ("./setup",))


class TestWhatTheTokenCanDoDecidesWhetherItIsKept(_PatRun):
    """The rule's verdict (tests/test_credcheck.py holds every branch) decides whether the pasted token is kept."""

    def test_a_token_is_kept_unless_the_rule_refuses_it(self):
        for token, setup, kept, phrases in ((FINE, {}, True, ()),
                                            (FINE, {"pulls": {"justinmichaud/WebKit": 403}}, False, ("settings/tokens/new",)),
                                            (CLASSIC, {"scopes": "repo"}, True, ()),
                                            (FINE, {"api": "http://127.0.0.1:1"}, True, ("unverified",))):
            with self.subTest(token=token, setup=setup):
                self.github(200)
                if "api" in setup:
                    self.extra_env = {"WK_GITHUB_API": setup.pop("api")}
                for k, v in setup.items():
                    setattr(FakeGitHub, k, v)
                if self.pat().exists():
                    self.pat().unlink()
                self.pasted((), token, token if kept else None, kept, phrases)

    def test_a_stored_token_is_reported_from_a_fresh_answer(self):
        self.github(200)
        self.holding(FINE)
        FakeGitHub.pulls = {"justinmichaud/WebKit": 403}
        cp = self.key("set", "github-pat")
        self.assertEqual(1, cp.returncode, cp.stdout + cp.stderr)


class TestATokenGitHubRefusesIsReplaced(_PatRun):
    """A stored token GitHub answers 401 for: `wk key setup` without a terminal names the remedy and keeps the file."""

    def setUp(self):
        super().setUp()
        self.github(401)
        self.holding("ghp_revokedone")

    def _key_with_gh_refusing(self, *args):
        from tests.test_key import GH_REFUSES
        return self.key(*args, stubs={"gh": GH_REFUSES})

    def test_setup_names_the_refused_token_and_how_to_replace_it(self):
        cp = self._key_with_gh_refusing("setup")
        out = cp.stdout + cp.stderr
        self.assertIn("wk key set github-pat --replace", out)
        self.assertNotRegex(out, r"github-pat\s+stored\s")
        self.assertEqual("ghp_revokedone", self.pat().read_text().strip(), "a run with no terminal removed the token unasked")

    def test_deploy_leaves_the_token_alone_and_still_reports_it(self):
        cp = self._key_with_gh_refusing("deploy")
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual("ghp_revokedone", self.pat().read_text().strip())

    def test_a_token_github_accepts_is_left_alone(self):
        FakeGitHub.user_status = 200
        FakeGitHub.pulls = {"justinmichaud/WebKit": 201, "justinmichaud/WPEWebKit": 201}
        cp = self._key_with_gh_refusing("setup")
        self.assertRegex(cp.stdout + cp.stderr, r"github-pat\s+stored\s")


class TestTheMachineTakesTheTokenOnEveryStart(unittest.TestCase):
    def test_a_container_start_converges_the_read_token(self):
        from unittest import mock
        from wk import places, secrets
        from wk.machine import Fake
        c = places.Container("container", str(REPO), {"HOME": "/nonexistent", "WK_STORE": "/nonexistent/store"}, Fake("here"))
        with mock.patch.object(secrets.Secrets, "push_converge_machine") as converge:
            c.start("demo")
        self.assertEqual(1, converge.call_count, "'wk start <container workspace>' does not converge the credentials")


if __name__ == "__main__":
    unittest.main()
