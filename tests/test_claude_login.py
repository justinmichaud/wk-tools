"""`wk key set claude-login`: capturing the claude.ai account credential.

Remote control refuses anything but a full-scope login -- measured in the
Claude CLI itself, which answers a `claude setup-token` token with "Remote
Control requires a full-scope login token. Long-lived tokens ... are limited
to inference-only for security reasons." So the credential a workspace needs
is the one `claude auth login` leaves behind, and this command reads it from
where the CLI put it rather than asking anyone to paste a JSON document:

    macOS   a login-Keychain item, service "Claude Code-credentials"
    Linux   ${CLAUDE_CONFIG_DIR:-~/.claude}/.credentials.json

Two things are held shut here. The credential never becomes an argument (`ps`
shows those to everyone on the machine) and is never printed; and what is
stored is checked first, so a token that cannot do the job is refused with the
remedy instead of stored and discovered three commands later.

`security` is a recording stub on PATH, so the capture under test is the real
one and no test reads this machine's own Keychain.

Run: python3 -m unittest tests.test_claude_login -v
"""
import contextlib
import json
import os
import platform
import subprocess
import unittest

from tests.support import REPO, WkTest, run, stub_path
from tests.test_pi_agent import FILE_ROWS, store_path

MACOS = platform.system() == "Darwin"

# Not a credential, and deliberately nothing like one. The shape is the CLI's
# own: its stored object is claudeAiOauth: {accessToken, refreshToken,
# expiresAt, scopes, ...}.
SECRET = "placeholder-value-for-this-test"
ROW = FILE_ROWS[0]


def login(**over):
    d = {"accessToken": SECRET, "refreshToken": SECRET + "-r",
         "expiresAt": 1, "scopes": ["user:inference", "user:profile"]}
    d.update(over)
    return json.dumps({"claudeAiOauth": d})


# `security`, as far as cmd/key can tell: records its whole argv, then answers
# with whatever the test put in $WK_TEST_KEYCHAIN -- or nothing, exiting 44
# (errSecItemNotFound), which is what a machine with no login answers.
FAKE_SECURITY = '''#!/bin/sh
printf '%s\\n' "$*" >> "$WK_TEST_SECURITY_LOG"
if [ -s "$WK_TEST_KEYCHAIN" ]; then cat "$WK_TEST_KEYCHAIN"; exit 0; fi
exit 44
'''


class _Capture(WkTest):
    """A scratch secrets/agent-rw pair, and the platform's own hiding place
    for the credential faked out: a stub `security` on macOS, a scratch HOME
    on Linux. `wk key set claude-login` is the real command either way, so
    every assertion below runs on whichever arm this machine has -- and the
    arm it does *not* have is driven, lifted out of the file, further down.
    """

    def setUp(self):
        super().setUp()
        self.secrets = self.tmp / "secrets"
        self.secrets.mkdir(parents=True)
        self.home = self.tmp / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.keychain = self.tmp / "keychain"
        self.keychain.write_text("")
        self.seclog = self.tmp / "security.log"
        self.seclog.write_text("")

    def stored(self):
        return store_path(self.tmp, ROW)

    def hold(self, text):
        """What `claude auth login` left on this machine."""
        if MACOS:
            self.keychain.write_text(text)
        else:
            (self.home / ".claude" / ".credentials.json").write_text(text)

    def _env(self, binp, **over):
        env = {
            "PATH": f"{binp}:{os.environ['PATH']}",
            "HOME": str(self.home),
            "WK_HOST_SECRETS": str(self.secrets),
            "WK_TEST_KEYCHAIN": str(self.keychain),
            "WK_TEST_SECURITY_LOG": str(self.seclog),
            # A store of its own, so nothing here goes near the real one.
            "WK_STORE": str(self.tmp / "store"),
        }
        env.update(over)
        return env

    @contextlib.contextmanager
    def _stubs(self):
        with stub_path({"security": FAKE_SECURITY}) as binp:
            yield binp

    def key(self, *args, **over):
        with self._stubs() as binp:
            env = self._env(binp, **over)
            env.pop("CLAUDE_CONFIG_DIR", None)
            return run("key", *args, env=env)


class TestItCapturesRatherThanAsks(_Capture):
    def test_the_bytes_land_in_the_store_unchanged(self):
        """Byte for byte: its tool parses the document, and a newline the
        capture added would be a newline in it."""
        self.hold(login())
        cp = self.key("set", "claude-login")
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertEqual(login(), self.stored().read_text())

    def test_it_is_unreadable_to_anyone_else(self):
        self.hold(login())
        self.key("set", "claude-login")
        mode = self.stored().stat().st_mode & 0o777
        self.assertEqual(0o600, mode, oct(mode))

    def test_it_goes_in_the_writable_directory_not_the_read_only_one(self):
        """The CLI rewrites this file when it spends the refresh token in it,
        and the secrets directory is mounted read-only into every workspace."""
        self.hold(login())
        self.key("set", "claude-login")
        self.assertTrue(self.stored().exists(), self.stored())
        self.assertFalse((self.secrets / ROW[1]).exists())

    @unittest.skipUnless(MACOS, "the Keychain arm is macOS's")
    def test_the_credential_is_never_an_argument(self):
        """`security` is asked for it by name; the value comes back on its
        stdout. Nothing in this command's argv, or the stub's, is the
        credential."""
        self.hold(login())
        self.key("set", "claude-login")
        argv = self.seclog.read_text()
        self.assertIn("find-generic-password", argv)
        self.assertIn("Claude Code-credentials", argv)
        self.assertNotIn(SECRET, argv, argv)

    def test_it_is_never_printed(self):
        self.hold(login())
        cp = self.key("set", "claude-login")
        self.assertNotIn(SECRET, cp.stdout, cp.stdout)

    def test_the_report_names_the_scopes_which_are_not_secret(self):
        """What was stored is reported by what it can do, since that is the
        question ("can an agent spend this?") and the answer is not the
        credential."""
        self.hold(login())
        cp = self.key("set", "claude-login")
        self.assertIn("user:profile", cp.stdout, cp.stdout)

    def test_reading_it_back_reports_it_without_printing_it(self):
        self.hold(login())
        self.key("set", "claude-login")
        cp = self.key("set", "claude-login")
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertIn("present", cp.stdout)
        self.assertIn("user:profile", cp.stdout)
        self.assertNotIn(SECRET, cp.stdout, cp.stdout)


class TestItRefusesWhatCannotBeUsed(_Capture):
    def test_no_login_on_this_machine_names_the_remedy(self):
        cp = self.key("set", "claude-login")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("claude auth login", cp.stdout)
        self.assertFalse(self.stored().exists())

    def test_something_that_is_not_json_is_refused(self):
        self.hold("not json at all")
        cp = self.key("set", "claude-login")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("not JSON", cp.stdout)
        self.assertFalse(self.stored().exists())

    def test_json_that_is_not_a_credential_is_refused(self):
        self.hold(json.dumps({"something": "else"}))
        cp = self.key("set", "claude-login")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("claudeAiOauth", cp.stdout)
        self.assertFalse(self.stored().exists())

    def test_an_inference_only_token_is_refused_by_name(self):
        """What `claude setup-token` produces has no refresh token, and is the
        obvious wrong thing to hand this."""
        self.hold(json.dumps({"claudeAiOauth": {
            "accessToken": SECRET, "scopes": ["user:inference"]}}))
        cp = self.key("set", "claude-login")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("inference-only", cp.stdout)
        self.assertFalse(self.stored().exists())

    def test_a_login_without_the_scope_remote_control_needs_is_refused(self):
        """Measured in the CLI: remote control checks for user:profile and
        says so. Refusing here is the difference between one clear message and
        a server that starts and dies."""
        self.hold(login(scopes=["user:inference"]))
        cp = self.key("set", "claude-login")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("user:profile", cp.stdout)
        self.assertFalse(self.stored().exists())

    def test_a_refusal_leaves_no_half_written_file(self):
        """Checked before it is stored, not while: a document that turns out
        not to be a credential must not become a truncated one on disk.
        (`--replace` withdraws the old one first, deliberately -- a replace
        that is then abandoned must not leave something behind claiming to be
        current -- so what this asserts is that nothing partial is there.)"""
        self.hold(login())
        self.key("set", "claude-login")
        self.hold("not json at all")
        cp = self.key("set", "claude-login", "--replace")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertFalse(self.stored().exists())

    @unittest.skipUnless(MACOS, "the Keychain arm is macOS's")
    def test_a_config_dir_that_moves_the_keychain_item_is_refused(self):
        """The CLI appends a hash of CLAUDE_CONFIG_DIR to the Keychain service
        name, so with one set the item this reads is not the one it wrote.
        Refused rather than guessed at. (On Linux there is nothing to guess:
        the variable names the directory the file is in, and the read follows
        it -- see TestTheLinuxArmReadsTheFile.)"""
        self.hold(login())
        with self._stubs() as binp:
            cp = run("key", "set", "claude-login",
                     env=self._env(binp, CLAUDE_CONFIG_DIR=str(self.tmp / "elsewhere")))
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("CLAUDE_CONFIG_DIR", cp.stdout)
        self.assertFalse(self.stored().exists())


class TestReplace(_Capture):
    def test_replacing_nothing_is_refused_and_names_the_remedy(self):
        cp = self.key("set", "claude-login", "--replace")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("wk key set claude-login", cp.stdout)

    def test_it_rotates_to_the_new_one(self):
        self.hold(login())
        self.key("set", "claude-login")
        self.hold(login(accessToken=SECRET + "-second"))
        cp = self.key("set", "claude-login", "--replace")
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertIn("-second", self.stored().read_text())
        self.assertNotIn("-second", cp.stdout, cp.stdout)


class TestTheLinuxArmReadsTheFile(_Capture):
    """The same capture where the CLI keeps the credential in a file. Driven
    against a scratch HOME, so it is the real read and not a transcript."""

    def _read(self, home, config_dir=None):
        env = dict(os.environ)
        env["HOME"] = str(home)
        if config_dir:
            env["CLAUDE_CONFIG_DIR"] = str(config_dir)
        else:
            env.pop("CLAUDE_CONFIG_DIR", None)
        script = f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
{_fn(REPO, "_claude_login_read")}
is_macos() {{ return 1; }}
_claude_login_read
'''
        return subprocess.run(["bash", "-c", script], cwd=str(REPO), env=env,
                              capture_output=True, text=True, timeout=60)

    def test_it_reads_the_default_credentials_file(self):
        home = self.tmp / "linux-home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / ".credentials.json").write_text(login())
        cp = self._read(home)
        self.assertEqual(login(), cp.stdout, cp.stderr)

    def test_it_follows_claude_config_dir(self):
        home = self.tmp / "linux-home-2"
        elsewhere = self.tmp / "elsewhere"
        home.mkdir()
        elsewhere.mkdir()
        (elsewhere / ".credentials.json").write_text(login())
        cp = self._read(home, config_dir=elsewhere)
        self.assertEqual(login(), cp.stdout, cp.stderr)

    def test_no_file_reads_as_nothing_and_is_not_an_error(self):
        home = self.tmp / "linux-home-3"
        home.mkdir()
        cp = self._read(home)
        self.assertEqual("", cp.stdout, cp.stderr)


class TestTheMacosArmReadsTheKeychain(_Capture):
    """The mirror of the above: the Keychain read, lifted and driven with a
    stub `security`, so both arms are exercised on either kind of host."""

    def _read(self, config_dir=None):
        env = dict(os.environ)
        env.update({"HOME": str(self.home),
                    "WK_TEST_KEYCHAIN": str(self.keychain),
                    "WK_TEST_SECURITY_LOG": str(self.seclog)})
        if config_dir:
            env["CLAUDE_CONFIG_DIR"] = str(config_dir)
        else:
            env.pop("CLAUDE_CONFIG_DIR", None)
        with stub_path({"security": FAKE_SECURITY}) as binp:
            env["PATH"] = f"{binp}:{os.environ['PATH']}"
            script = f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
{_fn(REPO, "_claude_login_refuse_relocated_store")}
{_fn(REPO, "_claude_login_read")}
is_macos() {{ return 0; }}
_claude_login_refuse_relocated_store
_claude_login_read
'''
            return subprocess.run(["bash", "-c", script], cwd=str(REPO), env=env,
                                  capture_output=True, text=True, timeout=60)

    def test_it_reads_the_default_keychain_item(self):
        self.keychain.write_text(login())
        cp = self._read()
        self.assertEqual(login(), cp.stdout, cp.stderr)
        self.assertIn("Claude Code-credentials", self.seclog.read_text())

    def test_no_item_reads_as_nothing_and_is_not_an_error(self):
        cp = self._read()
        self.assertEqual(0, cp.returncode, cp.stderr)
        self.assertEqual("", cp.stdout, cp.stderr)

    def test_a_config_dir_is_refused_rather_than_guessed_at(self):
        self.keychain.write_text(login())
        cp = self._read(config_dir=self.tmp / "elsewhere")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("CLAUDE_CONFIG_DIR", cp.stdout + cp.stderr)
        self.assertNotIn(SECRET, cp.stdout, cp.stdout)


def _fn(repo, name):
    """One function lifted out of cmd/key, the way cmd/selftest lifts them:
    the arm under test is a platform arm, and this is how the other one is
    reachable from here."""
    text = (repo / "cmd" / "key").read_text()
    start = text.index(f"{name}() {{")
    return text[start:text.index("\n}\n", start) + 3]


class TestNothingElseLearnedTheShape(unittest.TestCase):
    """One place decides what a usable login is, and one place knows where the
    platform keeps it."""

    KEY = (REPO / "cmd" / "key").read_text()

    def test_the_check_is_in_one_function(self):
        self.assertEqual(1, self.KEY.count("_claude_login_check() {"))
        self.assertEqual(1, self.KEY.count("_claude_login_read() {"))
        self.assertEqual(1, self.KEY.count("_claude_login_refuse_relocated_store() {"))

    def test_the_refusal_is_not_inside_the_command_substitution(self):
        """`die` in a `$( )` ends only the subshell, and the caller carries on
        with an empty answer -- two error messages and the wrong exit path."""
        arm = self.KEY.split("_claude_login_refuse_relocated_store\n", 1)[1]
        self.assertTrue(arm.lstrip().startswith("_val=$(_claude_login_read)"), arm[:120])

    def test_no_other_file_names_the_keychain_item(self):
        for f in ("cmd/ai", "cmd/verify", "lib/store.sh", "targets/vm.sh",
                  "container/firstrun.sh", "shell/bashrc"):
            with self.subTest(script=f):
                self.assertNotIn("find-generic-password", (REPO / f).read_text())

    def test_the_store_is_written_on_stdin(self):
        """An argument is in `ps` for everyone on the machine."""
        self.assertIn('printf \'%s\' "$_val" | wk_agent_secret_store "$_name"', self.KEY)


if __name__ == "__main__":
    unittest.main()
