"""`wk key set github-pat` -- the one credential that never enters a workspace.

It lives beside the private deploy-key halves in the directory nothing mounts
(wk_push_held_dir, lib/store.sh); the only thing that ever reads it is the
credential injector on the machine that runs the workspaces, and `wk push`
is what hands it over and takes it away.

Every arm of `wk key set github-pat` is here, including the two that need a
terminal: the value is asked for with `read -rs`, so those run the real
command under a pty rather than a stub of the prompt.

Run: python3 -m unittest tests.test_key_github_pat -v
"""
import os
import pty
import re
import select
import subprocess
import termios
import threading
import time
import unittest
from http.server import HTTPServer

from tests.support import REPO, WkTest, stub_path
from tests.test_credcheck import CLASSIC, FINE, FakeGitHub

KEY = REPO / "cmd" / "key"


def _wait_for_echo_off(fd, timeout=5.0):
    """The prompt is printed before `read -rs` turns the terminal's echo off,
    so a paste written the instant it appears is echoed by the tty itself --
    which is the very thing these tests assert against. Wait for the flag the
    command sets rather than for a length of time."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not termios.tcgetattr(fd)[3] & termios.ECHO:
            return
        time.sleep(0.005)
    raise AssertionError("`read -rs` never turned the terminal's echo off")
PODMAN_TRAP = '#!/bin/sh\necho "podman was called" >&2\nexit 1\n'
TOKEN = "ghp_thisisnotarealtoken0123456789"


class _PatRun(WkTest):
    def setUp(self):
        super().setUp()
        self.secrets = self.tmp / "secrets"
        self.held = self.tmp / "push-keys"
        self.store = self.tmp / "store"
        self.secrets.mkdir()
        self.held.mkdir()
        # A store this process can write is a machine `push_agent_exec` runs
        # on directly (store_is_local, lib/store.sh), which is what makes the
        # read token's delivery observable here without a podman machine.
        self.store.mkdir()
        self.extra_env = {}

    def _env(self, binp):
        env = dict(os.environ)
        for var in ("WK_NAME", "WK_TARGET", "WK_TARGET_KIND", "WK_MARKER",
                    "WK_STORE", "WK_IN_VM"):
            env.pop(var, None)
        reg = self.tmp / "no-registry"
        reg.mkdir(exist_ok=True)
        env.update({"WK_HOST_SECRETS": str(self.secrets),
                    "WK_STORE": str(self.store),
                    "WK_TARGET_REGISTRY": str(reg),
                    "PATH": f"{binp}:/usr/bin:/bin:/usr/sbin:/sbin"})
        env.update(self.extra_env)
        return env

    def key(self, *args):
        """No terminal: what a script, a hook or a headless run gets."""
        with stub_path({"podman": PODMAN_TRAP}) as binp:
            return subprocess.run([str(KEY), *args], cwd=str(REPO),
                                  env=self._env(binp), capture_output=True,
                                  text=True, timeout=120)

    def key_tty(self, *args, paste=""):
        """The same command with a real terminal on stdin, and <paste> typed
        at the prompt -- the only way through `read -rs`, which is what keeps
        the value out of argv and out of the shell's history."""
        with stub_path({"podman": PODMAN_TRAP}) as binp:
            master, slave = pty.openpty()
            p = subprocess.Popen([str(KEY), *args], cwd=str(REPO),
                                 env=self._env(binp), stdin=slave,
                                 stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            out, sent = b"", False
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
        self.assertIn("no GitHub token here to replace", cp.stderr)
        self.assertIn(str(self.pat()), cp.stderr)

    def test_an_unknown_flag_is_the_usage(self):
        cp = self.key("set", "github-pat", "--rotate")
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("usage: wk key", cp.stderr)
        self.assertFalse(self.pat().exists())

    def test_an_empty_answer_stores_nothing_and_says_what_that_costs(self):
        rc, out = self.key_tty("set", "github-pat", paste="")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("nothing stored", out)
        self.assertIn("401", out)
        self.assertFalse(self.pat().exists())

    def test_with_no_terminal_it_says_to_re_run_interactively(self):
        """A hook or a headless run cannot be asked; it must not look like a
        token was stored."""
        cp = self.key("set", "github-pat")
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("Re-run interactively", cp.stderr)
        self.assertFalse(self.pat().exists())


class TestStoringOne(_PatRun):
    def test_it_lands_in_the_directory_nothing_mounts_and_only_this_user_reads(self):
        rc, out = self.key_tty("set", "github-pat", paste=TOKEN)
        self.assertEqual(rc, 0, out)
        self.assertEqual(TOKEN, self.pat().read_text().strip())
        self.assertEqual(0o600, self.pat().stat().st_mode & 0o777)
        self.assertEqual(0o700, self.held.stat().st_mode & 0o777)
        self.assertFalse((self.secrets / "github-pat").exists(),
                         "the token is in the directory every workspace mounts")

    def test_it_reports_what_the_switch_will_do_with_it(self):
        rc, out = self.key_tty("set", "github-pat", paste=TOKEN)
        self.assertEqual(rc, 0, out)
        self.assertIn("wk push on", out)

    def test_the_value_is_never_echoed_back(self):
        """`read -rs` and a redirect, not an argument and not a report: the
        one place the bytes appear is the file."""
        rc, out = self.key_tty("set", "github-pat", paste=TOKEN)
        self.assertEqual(rc, 0, out)
        self.assertNotIn(TOKEN, out)

    def test_the_value_is_never_an_argument(self):
        """An argument is in `ps` for everyone on the machine, so the value is
        written by a redirect inside a subshell that sets the umask first --
        never handed to a command."""
        text = (REPO / "cmd" / "key").read_text()
        written = r'''( umask 077; printf '%s\n' "$_val" > "$(wk_github_pat_path)" )'''
        self.assertIn(written, text)


class TestReplacingOne(_PatRun):
    def setUp(self):
        super().setUp()
        self.pat().write_text("ghp_theoldone\n")
        self.pat().chmod(0o600)

    def test_a_bare_set_reports_it_rather_than_asking_again(self):
        """Present is the answer, and it must not silently overwrite: a token
        already handed to the injector is one `wk push on` is using."""
        cp = self.key("set", "github-pat")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("github-pat: present", cp.stderr)
        self.assertIn("--replace", cp.stderr)
        self.assertEqual("ghp_theoldone", self.pat().read_text().strip())

    def test_the_report_never_prints_the_token(self):
        cp = self.key("set", "github-pat")
        self.assertNotIn("ghp_theoldone", cp.stdout + cp.stderr)

    def test_replace_removes_the_old_one_first_and_says_to_revoke_it(self):
        """Removing it here does not revoke it on GitHub, and a rotation that
        left the old one live would be a credential nobody is tracking."""
        rc, out = self.key_tty("set", "github-pat", "--replace", paste=TOKEN)
        self.assertEqual(rc, 0, out)
        self.assertIn("revoke it on GitHub", out)
        self.assertEqual(TOKEN, self.pat().read_text().strip())

    def test_replace_with_an_empty_answer_leaves_none(self):
        """The old one is gone the moment --replace is given: that is the
        point of it, and the refusal says what the workspace loses."""
        rc, out = self.key_tty("set", "github-pat", "--replace", paste="")
        self.assertNotEqual(rc, 0, out)
        self.assertFalse(self.pat().exists())
        self.assertIn("nothing stored", out)


class TestTheStandingReadTokenReachesTheMachine(_PatRun):
    """Reading GitHub is open whatever position `wk push` is in, so the machine
    that runs the workspaces keeps a standing copy of this token. Storing,
    rotating or withdrawing one converges that copy here, because ./setup is
    the only other place that does and nobody re-runs it to fix a read."""

    def read_pat(self):
        return self.store / "read-github-pat"

    def test_storing_one_delivers_it(self):
        rc, out = self.key_tty("set", "github-pat", paste=TOKEN)
        self.assertEqual(rc, 0, out)
        self.assertEqual(TOKEN, self.read_pat().read_text().strip())
        self.assertEqual(0o600, self.read_pat().stat().st_mode & 0o777)

    def test_rotating_one_delivers_the_new_one(self):
        self.pat().write_text("ghp_theoldone\n")
        self.read_pat().write_text("ghp_theoldone\n")
        rc, out = self.key_tty("set", "github-pat", "--replace", paste=TOKEN)
        self.assertEqual(rc, 0, out)
        self.assertEqual(TOKEN, self.read_pat().read_text().strip())

    def test_withdrawing_one_removes_it_there_too(self):
        """`--replace` with an empty answer leaves this device with no token,
        and a machine still reading GitHub with the old one would be the
        withdrawal not having happened."""
        self.pat().write_text("ghp_theoldone\n")
        self.read_pat().write_text("ghp_theoldone\n")
        rc, out = self.key_tty("set", "github-pat", "--replace", paste="")
        self.assertNotEqual(rc, 0, out)
        self.assertFalse(self.read_pat().exists())

    def test_the_value_is_never_an_argument_on_the_way_there_either(self):
        rc, out = self.key_tty("set", "github-pat", paste=TOKEN)
        self.assertEqual(rc, 0, out)
        self.assertNotIn(TOKEN, out)

    def test_a_machine_that_cannot_take_it_is_a_warning_naming_the_other_delivery(self):
        """Best effort: the token is stored either way, and ./setup converges
        the machine. A `die` here would refuse to keep a credential the person
        has already pasted."""
        self.extra_env = {"WK_STORE": str(self.tmp / "not-a-store")}
        rc, out = self.key_tty("set", "github-pat", paste=TOKEN)
        self.assertEqual(rc, 0, out)
        self.assertEqual(TOKEN, self.pat().read_text().strip())
        self.assertIn("did not take the read token", out)
        self.assertIn("./setup", out)


class TestWhatTheTokenCanDoDecidesWhetherItIsKept(_PatRun):
    """The whole point of asking: a token that cannot open a pull request is
    refused here rather than discovered hours later by `git-webkit pr`, and one
    that reaches further than wk spends it is kept with that reach named.

    The same stub GitHub tests/test_credcheck.py drives the rule with, driven
    here through the real command and its terminal.
    """

    def setUp(self):
        super().setUp()
        self.server = HTTPServer(("127.0.0.1", 0), FakeGitHub)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        FakeGitHub.user_status = 200
        FakeGitHub.scopes = ""
        FakeGitHub.expiry = ""
        FakeGitHub.pulls = {}
        FakeGitHub.seen = []
        self.extra_env = {
            "WK_GITHUB_API": "http://127.0.0.1:%d" % self.server.server_port}

    def test_a_token_that_can_open_a_pull_request_is_stored(self):
        rc, out = self.key_tty("set", "github-pat", paste=FINE)
        self.assertEqual(rc, 0, out)
        self.assertEqual(FINE, self.pat().read_text().strip())
        self.assertIn("can open a pull request on justinmichaud/WebKit", out)

    def test_a_token_without_pull_request_write_stores_nothing(self):
        FakeGitHub.pulls = {"justinmichaud/WebKit": 403}
        rc, out = self.key_tty("set", "github-pat", paste=FINE)
        self.assertNotEqual(rc, 0, out)
        self.assertFalse(self.pat().exists(),
                         "a token GitHub refuses was stored anyway")
        self.assertIn("Pull requests: write", out)
        self.assertIn("personal-access-tokens/new", out)

    def test_a_token_that_could_delete_a_repository_stores_nothing(self):
        FakeGitHub.scopes = "repo, delete_repo"
        rc, out = self.key_tty("set", "github-pat", paste=CLASSIC)
        self.assertNotEqual(rc, 0, out)
        self.assertFalse(self.pat().exists())
        self.assertIn("delete_repo", out)

    def test_a_classic_token_is_kept_and_its_reach_named(self):
        FakeGitHub.scopes = "repo"
        rc, out = self.key_tty("set", "github-pat", paste=CLASSIC)
        self.assertEqual(rc, 0, out)
        self.assertEqual(CLASSIC, self.pat().read_text().strip())
        self.assertIn("reaches further than wk spends it", out)

    def test_an_unreachable_api_stores_it_and_says_it_is_unverified(self):
        """Offline is a state: refusing here would leave the machine with no
        token at all, and every reader asks again."""
        self.extra_env = {"WK_GITHUB_API": "http://127.0.0.1:1"}
        rc, out = self.key_tty("set", "github-pat", paste=FINE)
        self.assertEqual(rc, 0, out)
        self.assertEqual(FINE, self.pat().read_text().strip())
        self.assertIn("unverified", out)

    def test_a_stored_token_is_reported_from_a_fresh_answer(self):
        """Never from a record: the token was fine when it was stored and the
        report says what GitHub says now."""
        self.pat().write_text(FINE + "\n")
        self.pat().chmod(0o600)
        FakeGitHub.pulls = {"justinmichaud/WebKit": 403}
        cp = self.key("set", "github-pat")
        self.assertEqual(1, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("Pull requests: write", cp.stderr)

    def test_the_token_is_never_printed_by_any_of_it(self):
        for scopes, pulls in (("", {}), ("repo, delete_repo", {}),
                              ("", {"justinmichaud/WebKit": 403})):
            with self.subTest(scopes=scopes):
                FakeGitHub.scopes = scopes
                FakeGitHub.pulls = pulls
                _rc, out = self.key_tty("set", "github-pat", paste=FINE)
                self.assertNotIn(FINE, out)


if __name__ == "__main__":
    unittest.main()
