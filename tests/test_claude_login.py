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

import threading
from http.server import HTTPServer

from tests.support import REPO, WK, WkTest, _clean_env, run, stub_path
from tests.test_credcheck import FakeAnthropic
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
         "expiresAt": 4102444800000, "refreshTokenExpiresAt": 4102444800000,
         "scopes": ["user:inference", "user:profile"]}
    d.update(over)
    return json.dumps({"claudeAiOauth": d})


ORG = "11111111-2222-3333-4444-555555555555"


def record(**over):
    """The account record the CLI writes into its config file at login: what
    remote control reads the organization from."""
    d = {"accountUuid": "a-" + ORG, "emailAddress": "someone@example.invalid",
         "organizationUuid": ORG, "organizationName": "Example Org"}
    d.update(over)
    return json.dumps({"oauthAccount": d})


# `claude`, as far as cmd/key can tell: records its whole argv, the variable
# that says where to store a login and the one that says where its config home
# is, then writes what the test put in $WK_TEST_LOGIN and $WK_TEST_RECORD to
# the two files the real one writes at the end of a browser login -- the
# credential into the store, the account record into the config file. An
# empty one writes nothing.
FAKE_CLAUDE = '''#!/bin/sh
{
  printf 'argv: %s\\n' "$*"
  printf 'store: %s\\n' "$CLAUDE_SECURESTORAGE_CONFIG_DIR"
  printf 'config: %s\\n' "$CLAUDE_CONFIG_DIR"
  printf 'security: %s\\n' "$(security show-keychain-info >/dev/null 2>&1; echo $?)"
} >> "$WK_TEST_CLAUDE_LOG"
if [ -s "$WK_TEST_LOGIN" ]; then
    mkdir -p "$CLAUDE_SECURESTORAGE_CONFIG_DIR"
    cat "$WK_TEST_LOGIN" > "$CLAUDE_SECURESTORAGE_CONFIG_DIR/.credentials.json"
fi
if [ -s "$WK_TEST_LOGIN" ] && [ -s "$WK_TEST_RECORD" ] && [ -n "$CLAUDE_CONFIG_DIR" ]; then
    mkdir -p "$CLAUDE_CONFIG_DIR/backups"
    cat "$WK_TEST_RECORD" > "$CLAUDE_CONFIG_DIR/.claude.json"
    : > "$CLAUDE_CONFIG_DIR/.claude.json.lock"
fi
exit ${WK_TEST_CLAUDE_EXIT:-0}
'''


class _Login(WkTest):
    """A scratch store, a `claude` that records what it was asked to do
    instead of opening a browser, and an Anthropic that answers for the login
    it leaves (tests/test_credcheck.py's FakeAnthropic): the rule asks it
    before anything is reported."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.anthropic = HTTPServer(("127.0.0.1", 0), FakeAnthropic)
        cls.anthropic_base = "http://127.0.0.1:%d" % cls.anthropic.server_port
        threading.Thread(target=cls.anthropic.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.anthropic.shutdown()
        cls.anthropic.server_close()
        super().tearDownClass()

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
        self.account = self.tmp / "what-the-login-records"
        self.account.write_text(record())

    def stored(self):
        return store_path(self.store, ROW)

    def recorded(self):
        return self.agent_rw / ".claude.json"

    def leaves(self, text, account=None):
        """What the browser login ends by writing: the credential, and the
        account record beside it unless a test takes that away."""
        self.answer.write_text(text)
        if account is not None:
            self.account.write_text(account)

    def _env(self, binp, **over):
        env = {
            "PATH": f"{binp}:{os.environ['PATH']}",
            "HOME": str(self.home),
            "WK_HOST_SECRETS": str(self.secrets),
            "WK_TEST_CLAUDE_LOG": str(self.log),
            "WK_TEST_LOGIN": str(self.answer),
            "WK_TEST_RECORD": str(self.account),
            # A store of its own, so nothing here goes near the real one.
            "WK_STORE": str(self.store),
            "WK_ANTHROPIC_API": self.anthropic_base,
            "WK_CLAUDE_OAUTH": self.anthropic_base,
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

    def test_the_shared_directory_is_the_config_home_for_that_login(self):
        """The account record remote control reads (oauthAccount) goes into the
        CLI's config file, not its credential store: pointed at the store alone,
        the CLI wrote it into this machine's own ~/.claude.json and every
        container read a login with no organization behind it."""
        self.leaves(login())
        cp = self.key("set", "claude-login")
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertIn("config: %s" % self.agent_rw, self.log.read_text())
        self.assertEqual(ORG, json.loads(self.recorded().read_text())
                         ["oauthAccount"]["organizationUuid"])
        self.assertFalse((self.home / ".claude.json").exists())

    def test_the_cli_is_shown_a_locked_keychain_so_it_writes_the_file(self):
        """Measured in the CLI: with `security` reaching a login Keychain it
        stores the login there, under a service name hashed from the store
        directory, and no workspace can read a Keychain. It has no switch to
        the file; a `security` answering as a locked Keychain does (exit 36,
        what an ssh session into a guest gets) is that switch, and it is the
        first thing on PATH for this one command."""
        self.leaves(login())
        cp = self.key("set", "claude-login")
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertIn("security: 36", self.log.read_text())
        shim = REPO / "lib" / "no-keychain" / "security"
        self.assertEqual(36, subprocess.run([str(shim), "show-keychain-info"]).returncode)

    def test_the_report_names_the_organization_which_is_not_secret(self):
        self.leaves(login())
        cp = self.key("set", "claude-login")
        self.assertIn("organization: Example Org", cp.stdout, cp.stdout)

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

    def test_a_login_with_no_account_record_beside_it_is_refused_by_name(self):
        """The failure measured 2026-09-11: a credential the CLI accepts and a
        remote-control server that exits at once with "Unable to determine your
        organization". The record is judged where the credential is, so the
        refusal is one line here and names the rotation that mends it."""
        self.leaves(login(), account=" ")
        cp = self.key("set", "claude-login")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("account record", cp.stdout)
        self.assertIn("wk key set claude-login --replace", cp.stdout)

    def test_a_record_with_no_organization_in_it_is_the_same_refusal(self):
        self.leaves(login(), account=json.dumps({"oauthAccount": {"emailAddress": "x"}}))
        cp = self.key("set", "claude-login")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("account record", cp.stdout)


class TestVerdictIsWhatAnotherWorkstationAsks(_Login):
    """`wk key check` on any workstation asks each peer `wk key verdict
    claude-login` -- the login is never fanned out, so the peer is the only one
    that can say whether it holds a usable one."""

    def test_a_stored_login_answers_ok_on_one_line(self):
        self.leaves(login())
        self.key("set", "claude-login")
        cp = self.key("verdict", "claude-login")
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertRegex(cp.stdout.splitlines()[0], r"^ok\tscopes: ")
        self.assertNotIn(SECRET, cp.stdout, cp.stdout)

    def test_nothing_stored_answers_absent(self):
        cp = self.key("verdict", "claude-login")
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertTrue(cp.stdout.startswith("absent\t"), cp.stdout)

    def test_a_login_the_cli_emptied_answers_bad(self):
        """What a second holder's refresh leaves behind: the CLI rewrites the
        file with empty tokens and expiresAt 0 (the docstring above)."""
        self.leaves(login())
        self.key("set", "claude-login")
        self.stored().write_text(login(accessToken="", refreshToken="", expiresAt=0))
        cp = self.key("verdict", "claude-login")
        self.assertTrue(cp.stdout.startswith("bad\t"), cp.stdout)

    def test_an_unknown_name_is_refused(self):
        cp = self.key("verdict", "nope")
        self.assertNotEqual(0, cp.returncode, cp.stdout)


class TestTheRemedyReadsTheStore(_Login):
    """`wk verify` and `wk ai claude` name the remedy when a workspace has no
    login, and it is derived from what this machine's store holds
    (agent_secret_store_remedy, lib/target.sh): nothing, one no workspace can
    use, or a usable one the workspace was made without."""

    def remedy(self):
        with stub_path({"claude": FAKE_CLAUDE}) as binp:
            env = self._env(binp)
        cp = self.bash(". lib/common.sh; . lib/store.sh; . lib/target.sh; "
                       "agent_secret_store_remedy claude-login", env=env)
        self.assertEqual(0, cp.returncode, cp.stderr)
        return cp.stdout

    def test_nothing_stored_names_the_command_that_stores_one(self):
        out = self.remedy()
        self.assertIn("holds no claude-login", out)
        self.assertIn("wk key set claude-login", out)
        self.assertNotIn("--replace", out)

    def test_an_emptied_login_names_the_rotation(self):
        self.leaves(login())
        self.key("set", "claude-login")
        self.stored().write_text(login(accessToken="", refreshToken="", expiresAt=0))
        out = self.remedy()
        self.assertIn("no workspace can use", out)
        self.assertIn("wk key set claude-login --replace", out)

    def test_a_usable_login_points_at_the_workspace_not_the_store(self):
        self.leaves(login())
        self.key("set", "claude-login")
        out = self.remedy()
        self.assertIn("usable claude-login", out)
        self.assertNotIn("--replace", out)


def bundle(credential, account=None):
    """What `wk key share` sends: the two files a login leaves, as a tar."""
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, text in ((".credentials.json", credential), (".claude.json", account)):
            if text is None:
                continue
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class TestAdoptTakesALoginMadeForThisWorkstation(_Login):
    """`wk key share` on another workstation logs in for this one and pipes
    the two files here; nothing is kept until the rule has passed them."""

    def adopt(self, data):
        with stub_path({"claude": FAKE_CLAUDE}) as binp:
            env = _clean_env(self._env(binp))
            return subprocess.run([str(WK), "key", "adopt", "claude-login"],
                                  cwd=str(REPO), env=env, input=data,
                                  capture_output=True, timeout=120)

    def test_the_two_files_land_where_the_containers_read_them(self):
        cp = self.adopt(bundle(login(), record()))
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertEqual(login(), self.stored().read_text())
        self.assertEqual(ORG, json.loads(self.recorded().read_text())
                         ["oauthAccount"]["organizationUuid"])
        self.assertEqual(0o600, self.stored().stat().st_mode & 0o777)
        self.assertNotIn(SECRET.encode(), cp.stdout + cp.stderr)
        self.assertTrue(cp.stdout.startswith(b"ok\t"), cp.stdout)

    def test_it_replaces_the_login_that_was_here(self):
        self.leaves(login())
        self.key("set", "claude-login")
        cp = self.adopt(bundle(login(accessToken=SECRET + "-made-elsewhere"), record()))
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("-made-elsewhere", self.stored().read_text())

    def test_a_login_the_rule_refuses_is_not_kept(self):
        cp = self.adopt(bundle(login(scopes=["user:inference"]), record()))
        self.assertNotEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn(b"user:profile", cp.stdout + cp.stderr)
        self.assertFalse(self.stored().exists())

    def test_a_bundle_without_the_account_record_is_refused(self):
        cp = self.adopt(bundle(login()))
        self.assertNotEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertFalse(self.stored().exists())

    def test_rubbish_on_stdin_is_refused_and_the_old_login_stays(self):
        self.leaves(login())
        self.key("set", "claude-login")
        cp = self.adopt(b"not a tar\n")
        self.assertNotEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn(b"login bundle", cp.stdout + cp.stderr)
        self.assertEqual(login(), self.stored().read_text())


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

    def test_the_record_and_the_clis_leftovers_go_with_the_old_file(self):
        """A withdrawn login leaves no account record claiming to be current,
        and none of the lock or backups the CLI wrote into its config home."""
        self.leaves(login())
        self.key("set", "claude-login")
        self.assertTrue(self.recorded().exists())
        self.assertTrue((self.agent_rw / "backups").is_dir())
        self.answer.write_text("")
        self.key("set", "claude-login", "--replace")
        self.assertFalse(self.recorded().exists())
        self.assertFalse((self.agent_rw / ".claude.json.lock").exists())
        self.assertFalse((self.agent_rw / "backups").exists())

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
        self.assertIn('_dir="${2:-$(wk_agent_rw_dir)}"', self.KEY)
        self.assertIn('CLAUDE_CONFIG_DIR="$_dir" CLAUDE_SECURESTORAGE_CONFIG_DIR="$_dir" '
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
