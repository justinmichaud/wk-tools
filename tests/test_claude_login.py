"""`wk key set claude-login`: the login the containers share.

Remote control refuses anything but a full-scope login -- measured in the
Claude CLI itself, which answers a `claude setup-token` token with "Remote
Control requires a full-scope login token. Long-lived tokens ... are limited
to inference-only for security reasons." So the credential a workspace needs
is the one `claude auth login` leaves behind, and this command makes one *of
its own* in the directory every container reads
(`CLAUDE_SECURESTORAGE_CONFIG_DIR`, `~/.config/wk/agent-rw`).

A copy of this machine's own login is refused by the CLI within the hour: a
refresh token rotates when it is spent, so two holders of one token means the
second refresh answers "OAuth session expired and could not be refreshed" and
the file every container shares is rewritten with `expiresAt` 0.

Two things are held shut here. The credential is never an argument (`ps` shows
those to everyone on the machine) and is never printed; and what the CLI left
is judged before the command reports success.

`claude` is a recording stub on PATH, so the arm under test is the real one
and no test opens a browser or touches this machine's own login.

Run: python3 -m unittest tests.test_claude_login -v
"""
import json
import os
import pty
import shutil
import subprocess
import unittest

from tests.support import REPO, WK, WkTest, _clean_env, run, stub_path
from tests.test_pi_agent import FILE_ROWS, store_path

# Not a credential, and deliberately nothing like one. The shape is the CLI's
# own: its stored object is claudeAiOauth: {accessToken, refreshToken,
# expiresAt, scopes, ...}.
SECRET = "placeholder-value-for-this-test"
ROW = FILE_ROWS[0]

# Enough PATH for the command to run and not enough for it to find the CLI.
BARE_PATH = "/usr/bin:/bin"


def login(**over):
    d = {"accessToken": SECRET, "refreshToken": SECRET + "-r",
         "expiresAt": 1, "scopes": ["user:inference", "user:profile"]}
    d.update(over)
    return json.dumps({"claudeAiOauth": d})


# `claude`, as far as cmd/key can tell: records its whole argv and the variable
# that says where to store a login, then writes what the test put in
# $WK_TEST_LOGIN there -- which is what the real one does at the end of a
# browser login. An empty $WK_TEST_LOGIN writes nothing.
FAKE_CLAUDE = '''#!/bin/sh
{
  printf 'argv: %s\\n' "$*"
  printf 'store: %s\\n' "$CLAUDE_SECURESTORAGE_CONFIG_DIR"
} >> "$WK_TEST_CLAUDE_LOG"
if [ -s "$WK_TEST_LOGIN" ]; then
    mkdir -p "$CLAUDE_SECURESTORAGE_CONFIG_DIR"
    cat "$WK_TEST_LOGIN" > "$CLAUDE_SECURESTORAGE_CONFIG_DIR/.credentials.json"
fi
exit ${WK_TEST_CLAUDE_EXIT:-0}
'''


class _Login(WkTest):
    """A scratch store, and a `claude` that records what it was asked to do
    instead of opening a browser."""

    def setUp(self):
        super().setUp()
        # wk_secrets_dir (lib/store.sh) reads WK_HOST_SECRETS on a macOS host
        # and $WK_STORE/secrets everywhere else; one directory under both names.
        self.store = self.tmp / "store"
        self.secrets = self.store / "secrets"
        self.secrets.mkdir(parents=True)
        self.agent_rw = self.store / "agent-rw"
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.log = self.tmp / "claude.log"
        self.log.write_text("")
        self.answer = self.tmp / "what-the-login-leaves"
        self.answer.write_text("")

    def stored(self):
        return store_path(self.store, ROW)

    def leaves(self, text):
        """What the browser login ends by writing."""
        self.answer.write_text(text)

    def _env(self, binp, **over):
        env = {
            "PATH": f"{binp}:{os.environ['PATH']}",
            "HOME": str(self.home),
            "WK_HOST_SECRETS": str(self.secrets),
            "WK_TEST_CLAUDE_LOG": str(self.log),
            "WK_TEST_LOGIN": str(self.answer),
            # A store of its own, so nothing here goes near the real one.
            "WK_STORE": str(self.store),
        }
        env.update(over)
        return env

    def key(self, *args, terminal=True, **over):
        """The login needs a terminal, so `./wk` is given a pty for stdin --
        except where the refusal without one is what is under test."""
        with stub_path({"claude": FAKE_CLAUDE}) as binp:
            env = _clean_env(self._env(binp, **over))
            if not terminal:
                return run("key", *args, env=env)
            master, slave = pty.openpty()
            try:
                cp = subprocess.run([str(WK), "key", *args], cwd=str(REPO),
                                    env=env, stdin=slave,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    timeout=120)
            finally:
                os.close(slave)
                os.close(master)
            cp.stderr = ""
            return cp


class TestItLogsInWhereTheContainersRead(_Login):
    def test_the_cli_is_pointed_at_the_shared_directory(self):
        self.leaves(login())
        cp = self.key("set", "claude-login")
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertIn("argv: auth login", self.log.read_text())
        self.assertIn("store: %s" % self.agent_rw, self.log.read_text())

    def test_the_login_lands_in_the_writable_directory_not_the_read_only_one(self):
        """The CLI rewrites this file when it spends the refresh token in it,
        and the secrets directory is mounted read-only into every workspace."""
        self.leaves(login())
        self.key("set", "claude-login")
        self.assertEqual(login(), self.stored().read_text())
        self.assertFalse((self.secrets / ROW[1]).exists())

    def test_nothing_on_this_machine_is_read_for_it(self):
        """A copy would be a second holder of one refresh token; the login is
        made rather than captured, so no Keychain item and no ~/.claude file
        is anywhere in this command."""
        key = (REPO / "cmd" / "key").read_text()
        self.assertNotIn("find-generic-password", key)
        self.assertNotIn(".credentials.json", key)

    def test_the_credential_is_never_an_argument(self):
        self.leaves(login())
        self.key("set", "claude-login")
        self.assertNotIn(SECRET, self.log.read_text(), self.log.read_text())

    def test_it_is_never_printed(self):
        self.leaves(login())
        cp = self.key("set", "claude-login")
        self.assertNotIn(SECRET, cp.stdout, cp.stdout)

    def test_the_report_names_the_scopes_which_are_not_secret(self):
        """What was stored is reported by what it can do, since that is the
        question ("can an agent spend this?") and the answer is not the
        credential."""
        self.leaves(login())
        cp = self.key("set", "claude-login")
        self.assertIn("user:profile", cp.stdout, cp.stdout)

    def test_reading_it_back_reports_it_without_logging_in_again(self):
        self.leaves(login())
        self.key("set", "claude-login")
        self.log.write_text("")
        cp = self.key("set", "claude-login")
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertRegex(cp.stdout, r"claude-login\s+stored\s+\S")
        self.assertIn("user:profile", cp.stdout)
        self.assertEqual("", self.log.read_text())
        self.assertNotIn(SECRET, cp.stdout, cp.stdout)

    def test_the_check_reads_it_from_the_shared_directory(self):
        """`wk key check` asks the same one file what it can do. cmd/key is
        run directly, the way tests/test_key.py runs it: the report needs a
        GitHub login the dispatcher demands before any subverb."""
        self.leaves(login())
        self.key("set", "claude-login")
        with stub_path({"claude": FAKE_CLAUDE}) as binp:
            env = _clean_env(self._env(binp, WK_NTFY_API="http://127.0.0.1:1"))
            cp = subprocess.run([str(REPO / "cmd" / "key"), "check"],
                                cwd=str(REPO), env=env, capture_output=True,
                                text=True, timeout=120)
        self.assertRegex(cp.stdout + cp.stderr,
                         r"claude-login\s+scopes: user:inference user:profile")

    def test_the_check_reports_nothing_stored_once_that_file_goes(self):
        """The other half of the pair above: the row is that one file."""
        self.leaves(login())
        self.key("set", "claude-login")
        self.stored().unlink()
        with stub_path({"claude": FAKE_CLAUDE}) as binp:
            env = _clean_env(self._env(binp, WK_NTFY_API="http://127.0.0.1:1"))
            cp = subprocess.run([str(REPO / "cmd" / "key"), "check"],
                                cwd=str(REPO), env=env, capture_output=True,
                                text=True, timeout=120)
        self.assertRegex(cp.stdout + cp.stderr, r"claude-login\s+nothing stored")


class TestItRefusesWhatCannotBeUsed(_Login):
    def test_without_a_terminal_it_refuses_and_names_the_command(self):
        """The login is a browser flow: there is nothing to fall back to, so
        the refusal says what to re-run interactively."""
        self.leaves(login())
        cp = self.key("set", "claude-login", terminal=False)
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("terminal", cp.stdout)
        self.assertIn("wk key set claude-login", cp.stdout)
        self.assertEqual("", self.log.read_text())
        self.assertFalse(self.stored().exists())

    def test_a_login_that_does_not_finish_stores_nothing(self):
        cp = self.key("set", "claude-login", WK_TEST_CLAUDE_EXIT="1")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("claude auth login", cp.stdout)
        self.assertFalse(self.stored().exists())

    def test_a_login_that_leaves_nothing_names_the_file_it_did_not_write(self):
        cp = self.key("set", "claude-login")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn(str(self.stored()), cp.stdout)
        self.assertFalse(self.stored().exists())

    @unittest.skipIf(shutil.which("claude", path=BARE_PATH),
                     "a `claude` in %s would be found by this" % BARE_PATH)
    def test_no_claude_cli_refuses_rather_than_reporting_nothing_stored(self):
        with stub_path({}) as binp:
            env = _clean_env(self._env(binp, PATH=BARE_PATH))
            cp = run("key", "set", "claude-login", env=env)
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("Claude CLI", cp.stdout)

    def test_an_inference_only_token_is_refused_by_name(self):
        """What `claude setup-token` produces has no refresh token; a store
        pointed at one is reported as unusable rather than as stored."""
        self.leaves(json.dumps({"claudeAiOauth": {
            "accessToken": SECRET, "scopes": ["user:inference"]}}))
        cp = self.key("set", "claude-login")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("inference-only", cp.stdout)

    def test_a_login_without_the_scope_remote_control_needs_is_refused(self):
        """Measured in the CLI: remote control checks for user:profile and
        says so. Refusing here is the difference between one clear message and
        a server that starts and dies."""
        self.leaves(login(scopes=["user:inference"]))
        cp = self.key("set", "claude-login")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("user:profile", cp.stdout)


class TestReplace(_Login):
    def test_replacing_nothing_is_refused_and_names_the_remedy(self):
        cp = self.key("set", "claude-login", "--replace")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("wk key set claude-login", cp.stdout)

    def test_the_old_file_goes_before_the_login_runs(self):
        """A replace that is then abandoned must not leave behind a file
        claiming to be current: the login the containers hold is withdrawn
        first and the new one is whatever the browser leaves."""
        self.leaves(login())
        self.key("set", "claude-login")
        self.answer.write_text("")
        cp = self.key("set", "claude-login", "--replace")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertFalse(self.stored().exists())

    def test_it_rotates_to_the_new_one(self):
        self.leaves(login())
        self.key("set", "claude-login")
        self.leaves(login(accessToken=SECRET + "-second"))
        cp = self.key("set", "claude-login", "--replace")
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertIn("-second", self.stored().read_text())
        self.assertNotIn("-second", cp.stdout, cp.stdout)


class TestNothingElseLearnedTheShape(unittest.TestCase):
    """One place decides what a usable login is, and one place makes one."""

    KEY = (REPO / "cmd" / "key").read_text()

    def test_the_check_is_in_one_function(self):
        """What a usable login is, is one row of lib/credcheck.py -- the same
        table every other credential's rule is in -- and cmd/key knows only
        how one is made."""
        rules = (REPO / "lib" / "credcheck.py").read_text()
        self.assertEqual(1, rules.count("def _claude_login("))
        self.assertNotIn("claudeAiOauth", self.KEY)
        self.assertEqual(1, self.KEY.count("_claude_login_run() {"))

    def test_the_login_is_made_where_the_containers_read_it(self):
        """The variable and the directory are one expression, so no second
        idea of where a container's login lives can drift in."""
        self.assertIn('CLAUDE_SECURESTORAGE_CONFIG_DIR="$(wk_agent_rw_dir)" '
                      'claude auth login', self.KEY)

    def test_no_other_file_names_the_keychain_item(self):
        for f in ("cmd/ai", "cmd/verify", "lib/store.sh", "targets/vm.sh",
                  "container/firstrun.sh", "shell/bashrc"):
            with self.subTest(script=f):
                self.assertNotIn("find-generic-password", (REPO / f).read_text())

    def test_a_value_row_is_written_on_stdin(self):
        """An argument is in `ps` for everyone on the machine."""
        self.assertIn('printf \'%s\\n\' "$_val" | wk_cred_store "$_name"', self.KEY)


if __name__ == "__main__":
    unittest.main()
