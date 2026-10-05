"""The agents' credentials (AGENT_SECRETS): shell/bashrc exports the token, a container links the read-only
/secrets mount, a macOS guest is written a copy on every start, a build box at `wk machine setup`; the Claude CLI's
login is one shared file and is never copied. Values here are placeholders.

Run: python3 -m unittest tests.test_agent_token -v
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from types import SimpleNamespace
from unittest import mock

from tests.support import guest_step, REPO, WkTest, bash, stub_path
from tests.test_pi_agent import FILE_ROWS, TABLE, VALUE_ROWS, store_path

sys.path.insert(0, str(REPO / "lib"))
from wk import guest, places  # noqa: E402
from wk.machine import Fake, Local, Result  # noqa: E402

RC = REPO / "shell" / "bashrc"
VAR = "CLAUDE_CODE_OAUTH_TOKEN"


def delivered_to(kind, rows=TABLE):
    """The rows the delivery column sends to one kind of place."""
    return [r for r in rows if kind in r[5].split(",")]


CONTAINER_ROWS = delivered_to("container", VALUE_ROWS)
VM_ROWS = delivered_to("vm", VALUE_ROWS)
REMOTE_ROWS = delivered_to("remote", VALUE_ROWS)

# Not a token, and deliberately nothing like one.
PLACEHOLDER = "placeholder-value-for-this-test"

class TestTheShellExportsIt(WkTest):

    SHELLS = {
        "editor terminal pane": ("zsh", ["-i", "-c"]),
        "login zsh": ("zsh", ["-l", "-c"]),
        "bash -lc (every Driver.exec)": ("bash", ["-lc"]),
        "non-interactive bash": ("bash", ["-c"]),
    }

    def _home(self, contents=None):
        h = self.tmp / "home"
        h.mkdir(exist_ok=True)
        if contents is not None:
            (h / ".wk-agent-token").write_text(contents)
        return h

    def _value(self, shell, args, home):
        cp = subprocess.run(
            [shell, *args, f'. "{RC}"; echo "{VAR}=${VAR}"'],
            cwd=str(REPO),
            env={"HOME": str(home), "TERM": "dumb",
                 "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=120,
        )
        for line in cp.stdout.splitlines():
            k, _, v = line.partition("=")
            if k == VAR:
                return v
        return None

    def test_every_shell_exports_it(self):
        home = self._home(PLACEHOLDER + "\n")
        for what, (shell, args) in self.SHELLS.items():
            if not shutil.which(shell):
                continue
            with self.subTest(shell=what):
                self.assertEqual(self._value(shell, args, home), PLACEHOLDER)

    def test_no_file_means_no_variable(self):
        home = self._home()
        for what, (shell, args) in self.SHELLS.items():
            if not shutil.which(shell):
                continue
            with self.subTest(shell=what):
                self.assertEqual(self._value(shell, args, home), "")

    def test_a_dangling_symlink_means_no_token(self):
        home = self._home()
        (home / ".wk-agent-token").symlink_to(home / "nothing-here")
        self.assertEqual(self._value("bash", ["-c"], home), "")

    def test_the_comment_line_is_skipped(self):
        home = self._home("# wk: written by lib/wk/guest.py\n" + PLACEHOLDER + "\n")
        self.assertEqual(self._value("bash", ["-c"], home), PLACEHOLDER)

class TestTheVmDriverFindsTheMachinesToken(unittest.TestCase):
    """The vm driver keeps its own store (WK_VM_STORE) and still reads this device's keyring."""

    def test_the_vm_driver_itself_still_finds_it(self):
        env = {"WK_HOST_SECRETS": "/this/device/secrets", "WK_STORE": "/the/machine/store",
               "WK_STORE_DEFAULT": "/the/machine/store", "WK_VM_STORE": "/some/vm/state", "HOME": "/nonexistent"}
        with mock.patch("wk.store.os.uname", return_value=mock.Mock(sysname="Darwin")):
            vm = places.Registry(REPO, env, Fake("here")).load("vm")
            self.assertEqual("/some/vm/state", vm.store.store_dir())
            self.assertEqual("/this/device/secrets/claude-token", guest.Host(vm).secrets.cred_path("claude"))


class TestOneClaudeCredentialPerPlace(unittest.TestCase):
    """Claude Code takes $CLAUDE_CODE_OAUTH_TOKEN over a stored login, and remote control refuses the token."""

    CLAUDE_ROWS = [r for r in TABLE if r[0].startswith("claude")]

    def test_the_two_claude_rows_reach_no_target_in_common(self):
        self.assertEqual(2, len(self.CLAUDE_ROWS), self.CLAUDE_ROWS)
        first, second = (set(r[5].split(",")) for r in self.CLAUDE_ROWS)
        self.assertEqual(set(), first & second,
                         "both Claude credentials reach %s" % (first & second))

    def test_every_kind_a_row_names_is_a_kind_that_exists(self):
        kinds = set()
        for row in TABLE:
            kinds.update(row[5].split(","))
        self.assertEqual(set(), kinds - {"container", "vm", "remote"}, kinds)

    def test_the_login_goes_only_where_this_machines_bytes_go(self):
        for row in FILE_ROWS:
            with self.subTest(name=row[0]):
                self.assertEqual(["container", "vm"], row[5].split(","))

    def test_a_kind_given_the_login_is_given_no_token(self):
        for kind in ("container", "vm"):
            with self.subTest(kind=kind):
                self.assertNotIn("claude", [r[0] for r in delivered_to(kind, VALUE_ROWS)])

    def test_a_row_a_container_is_not_given_is_taken_away(self):
        """container/firstrun.sh's own loop, lifted and run against a scratch home."""
        text = (REPO / "container" / "firstrun.sh").read_text()
        block = text.split("_agent_secrets() {", 1)[1]
        block = "_agent_secrets() {" + block.split("\nEOF\n", 1)[0] + "\nEOF\n"
        with tempfile.TemporaryDirectory(prefix="wk-test-firstrun-") as home:
            home = Path(home)
            for row in VALUE_ROWS:      # what an older container linked
                (home / row[2]).symlink_to("/secrets/" + row[1])
            cp = bash(f'''
log() {{ printf '%s\\n' "$*"; }}
WK_TOOLS="$WK_ROOT"
HOME={home}
{block}
''')
            self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
            for row in VALUE_ROWS:
                with self.subTest(name=row[0]):
                    link = home / row[2]
                    if row in CONTAINER_ROWS:
                        self.assertTrue(link.is_symlink(), row[2])
                    else:
                        self.assertFalse(link.is_symlink(), row[2])
                        self.assertFalse(link.exists(), row[2])


FAKE_SSH = '''
have_stdin=1
for a in "$@"; do
    [ "$a" = "-n" ] && have_stdin=0
    last="$a"
done
tmp=$(mktemp)
if [ "$have_stdin" = 1 ]; then cat > "$tmp"; else : > "$tmp"; fi
printf 'stdin=%s cmd=%s\n' "$(wc -c < "$tmp" | tr -d " ")" "$last" >> "$WK_TEST_SSH_LOG"
HOME="$WK_TEST_GUEST" sh -c "$last" < "$tmp"
rc=$?
rm -f "$tmp"
exit "$rc"
'''

# `tart`: one running guest, whose `exec` is FAKE_SSH's far side run as the guest's login shell.
FAKE_TART = '''
case "$1" in
list) echo '[{"Name":"wk-demo","State":"running","Source":"local"}]' ;;
exec)
    for a in "$@"; do last="$a"; done
    tmp=$(mktemp)
    cat > "$tmp"
    printf 'stdin=%s cmd=%s\\n' "$(wc -c < "$tmp" | tr -d " ")" "$last" >> "$WK_TEST_SSH_LOG"
    HOME="$WK_TEST_GUEST" bash -lc "$last" < "$tmp"
    rc=$?
    rm -f "$tmp"
    exit "$rc" ;;
*)    exit 1 ;;
esac
'''


def ask(driver, fn, secret):
    """The place's answer as the shell probes printed it: YES or NO for `present`, the text for `remedy`."""
    if fn == "present":
        out = "YES" if driver.agent_secret_present("demo", secret) else "NO"
    else:
        out = driver.agent_secret_remedy("demo", secret)
    return SimpleNamespace(stdout=out, stderr="")


class _Plain(places.Driver):
    """The base driver's contract, minus the hop: a real exec reaches the place over podman or ssh and runs the
    probe in a login shell, which is what decides where CLAUDE_SECURESTORAGE_CONFIG_DIR points."""

    def __init__(self, env):
        super().__init__("plain", str(REPO), env, Local())

    def exec(self, ws, argv, tty=False, timeout=None):
        guest = self.env["WK_TEST_GUEST"]
        cp = subprocess.run(argv, capture_output=True, text=True,
                            env=dict(self.env, HOME=guest, CLAUDE_SECURESTORAGE_CONFIG_DIR=guest + "/.claude"))
        return Result(cp.returncode, cp.stdout, cp.stderr)


class _Delivery(WkTest):
    """A scratch machine home, a scratch store, and the ssh log."""

    def _home(self):
        h = self.tmp / "machine-home"
        h.mkdir(exist_ok=True)
        return h

    def _store(self, values=()):
        d = self.tmp / "store"
        (d / "secrets").mkdir(parents=True, exist_ok=True)
        (d / "agent-rw").mkdir(parents=True, exist_ok=True)
        for row in TABLE:
            p = store_path(d, row)
            if row[0] in values:
                p.write_text(f"{PLACEHOLDER}-{row[0]}\n")
                p.chmod(0o600)
            elif p.exists():
                p.unlink()
        return d

    def _env(self, store, home, extra=None):
        self.log = self.tmp / "ssh.log"
        self.log.write_text("")
        env = {
            "WK_TEST_GUEST": str(home),
            "WK_TEST_SSH_LOG": str(self.log),
            "WK_STORE": str(store),
            "WK_HOST_SECRETS": str(store / "secrets"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
        }
        if extra:
            env.update(extra)
        return env

    def _ssh_lines(self):
        return [l for l in self.log.read_text().splitlines() if l.strip()]


class TestAGuestGetsThemOnStart(_Delivery):
    """write_agent_secrets: a guest holds a copy of every value row, withdrawn when the store has none."""

    def _write(self, store, home):
        with stub_path({"ssh": FAKE_SSH, "tart": FAKE_TART}) as binp:
            env = self._env(store, home,
                            {"PATH": f"{binp}:{os.environ['PATH']}",
                             "WK_VM_STORE": str(self.tmp / "vmstore")})
            return guest_step(env, "write_agent_secrets")

    def test_every_value_row_in_the_store_lands_in_the_guest_at_mode_600(self):
        home = self._home()
        cp = self._write(self._store(values=[n for n, *_ in TABLE]), home)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for name, _sfile, shome, *_ in VM_ROWS:
            with self.subTest(name=name):
                self.assertEqual((home / shome).read_text(), f"{PLACEHOLDER}-{name}\n")
                self.assertEqual(0o600, (home / shome).stat().st_mode & 0o777)

    def test_a_store_with_none_withdraws_what_the_guest_holds(self):
        home = self._home()
        for _name, _sfile, shome, *_ in TABLE:
            (home / shome).parent.mkdir(parents=True, exist_ok=True)
            (home / shome).write_text("stale\n")
        cp = self._write(self._store(), home)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for _name, _sfile, shome, *_ in TABLE:
            self.assertFalse((home / shome).exists(), shome)

    def test_one_absent_row_does_not_cost_the_next_one(self):
        home = self._home()
        last = VM_ROWS[-1]
        self._write(self._store(values=[last[0]]), home)
        self.assertEqual((home / last[2]).read_text(),
                         f"{PLACEHOLDER}-{last[0]}\n")
        self.assertEqual(len(TABLE), len(self._ssh_lines()), self.log.read_text())

    def test_the_value_is_never_an_argument(self):
        self._write(self._store(values=[n for n, *_ in TABLE]), self._home())
        text = self.log.read_text()
        self.assertNotIn(PLACEHOLDER, text, text)


class TestABuildBoxGetsThemAtSetup(_Delivery):
    """`wk machine setup`'s credential step, against a fake ssh whose `-n` gives the far side /dev/null."""

    def _setup(self, store, home):
        from wk import machine_cmd
        with stub_path({"ssh": FAKE_SSH}) as binp:
            env = self._env(store, home, {"PATH": f"{binp}:{os.environ['PATH']}"})
            err = io.StringIO()
            with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(err):
                # The credentials are in this machine's store (WK_STORE_DEFAULT), not the place's remote root.
                t = places.Remote("fakebox", str(REPO), dict(os.environ), Local())
                machine_cmd.Machines(REPO, env=dict(os.environ)).credentials(t, "fakebox")
        return SimpleNamespace(returncode=0, stdout="", stderr=err.getvalue())

    def test_the_credential_arrives_with_its_bytes_at_mode_600(self):
        home = self._home()
        cp = self._setup(self._store(values=[n for n, *_ in TABLE]), home)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for name, _sfile, shome, *_ in REMOTE_ROWS:
            with self.subTest(name=name):
                self.assertEqual((home / shome).read_text(), f"{PLACEHOLDER}-{name}\n")
                self.assertEqual(0o600, (home / shome).stat().st_mode & 0o777)

    def test_a_store_with_none_takes_the_copy_off_the_machine(self):
        home = self._home()
        for _name, _sfile, shome, *_ in REMOTE_ROWS:
            (home / shome).write_text("stale\n")
        cp = self._setup(self._store(), home)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for _name, _sfile, shome, *_ in REMOTE_ROWS:
            self.assertFalse((home / shome).exists(), shome)

    def test_one_absent_row_does_not_cost_the_next_one(self):
        home = self._home()
        last = REMOTE_ROWS[-1]
        self._setup(self._store(values=[last[0]]), home)
        self.assertEqual((home / last[2]).read_text(),
                         f"{PLACEHOLDER}-{last[0]}\n")
        self.assertEqual(len(REMOTE_ROWS), len(self._ssh_lines()),
                         self.log.read_text())

    def test_the_account_login_never_reaches_a_shared_machine(self):
        home = self._home()
        self._setup(self._store(values=[n for n, *_ in TABLE]), home)
        log = self.log.read_text()
        for row in FILE_ROWS:
            with self.subTest(name=row[0]):
                self.assertFalse((home / row[2]).exists(), row[2])
                self.assertNotIn(row[1], log, log)

    def test_the_value_is_never_an_argument(self):
        self._setup(self._store(values=[n for n, *_ in TABLE]), self._home())
        text = self.log.read_text()
        self.assertNotIn(PLACEHOLDER, text, text)


# Two lines, so a reader that took only the first would be caught.
FAKE_LOGIN = ('{"claudeAiOauth":{"accessToken":"' + PLACEHOLDER + '",\n'
              '"refreshToken":"' + PLACEHOLDER + '","scopes":["user:profile"]}}')


class TestAGuestMountsTheShare(_Delivery):
    """The login reaches a guest over the one virtiofs share `tart run` is given."""

    def test_the_rc_refuses_to_guess_the_share(self):
        home = self.tmp / "rc-home"
        home.mkdir()
        cp = subprocess.run(["bash", str(REPO / "vm" / "shell-rc.sh"), str(REPO)],
                            env={"HOME": str(home), "PATH": os.environ["PATH"]},
                            capture_output=True, text=True, timeout=60)
        self.assertNotEqual(0, cp.returncode)
        self.assertIn("lib/wk/guest.py", cp.stderr)
        self.assertFalse((home / ".zshrc").exists(), "it wrote an rc with no directory to name")

    def test_an_older_rc_stanza_is_converged_to_one_export(self):
        for old in ('export CLAUDE_SECURESTORAGE_CONFIG_DIR="$HOME/.claude-login"\n',
                    'export CLAUDE_SECURESTORAGE_CONFIG_DIR="$HOME/.claude"\n'):
            with self.subTest(old=old):
                home = self.tmp / ("old-home" + str(len(old)))
                home.mkdir()
                (home / ".zshrc").write_text("\n# wk-tools: the Claude credential, not a Keychain\n" + old)
                cp = subprocess.run(["bash", str(REPO / "vm" / "shell-rc.sh"), str(REPO), "/mnt/share"],
                                    env={"HOME": str(home), "PATH": os.environ["PATH"]},
                                    capture_output=True, text=True, timeout=60)
                self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
                exports = [l for l in (home / ".zshrc").read_text().splitlines()
                           if "CLAUDE_SECURESTORAGE_CONFIG_DIR" in l]
                self.assertEqual(['export CLAUDE_SECURESTORAGE_CONFIG_DIR="/mnt/share"'], exports)

    def _write(self, store, home):
        with stub_path({"ssh": FAKE_SSH, "tart": FAKE_TART}) as binp:
            env = self._env(store, home,
                            {"PATH": f"{binp}:{os.environ['PATH']}",
                             "WK_VM_STORE": str(self.tmp / "vmstore")})
            return guest_step(env, "write_agent_secrets")

    def _wired_home(self, mounted):
        home = self._home()
        store = home / "agent-rw"
        cp = subprocess.run(["bash", str(REPO / "vm" / "shell-rc.sh"), str(REPO), str(store)],
                            env={"HOME": str(home), "PATH": os.environ["PATH"]},
                            capture_output=True, text=True, timeout=60)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        if mounted:
            store.mkdir()
        return home

    def test_a_start_without_the_share_says_so_and_names_the_reboot(self):
        cp = self._write(self._store(), self._wired_home(mounted=False))
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("wk start demo", cp.stderr)

    def test_a_start_with_the_share_is_quiet(self):
        cp = self._write(self._store(), self._wired_home(mounted=True))
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertNotIn("not mounted", cp.stderr)


class TestAGuestIsNeverGivenACopyOfTheFileRow(_Delivery):
    """A credential its tool rewrites in place is never copied into a guest; an older copy is withdrawn."""

    def _store_with_login(self):
        d = self._store()
        for row in FILE_ROWS:
            store_path(d, row).write_text(FAKE_LOGIN)
            store_path(d, row).chmod(0o600)
        return d

    def _write(self, store, home):
        with stub_path({"ssh": FAKE_SSH, "tart": FAKE_TART}) as binp:
            env = self._env(store, home,
                            {"PATH": f"{binp}:{os.environ['PATH']}",
                             "WK_VM_STORE": str(self.tmp / "vmstore")})
            return guest_step(env, "write_agent_secrets")

    def test_a_store_that_holds_one_sends_none_of_its_bytes(self):
        home = self._home()
        cp = self._write(self._store_with_login(), home)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for row in FILE_ROWS:
            with self.subTest(name=row[0]):
                self.assertFalse((home / row[2]).exists(), row[2])
        self.assertEqual(len(TABLE), len(self._ssh_lines()), self.log.read_text())
        log = self.log.read_text()
        self.assertNotIn(PLACEHOLDER, log, log)
        self.assertNotIn("claudeAiOauth", log, log)
        for line in self._ssh_lines():
            if FILE_ROWS[0][2] in line:
                self.assertIn("stdin=0", line, line)

    def test_a_copy_an_older_start_left_behind_is_taken_away(self):
        home = self._home()
        for row in FILE_ROWS:
            (home / row[2]).parent.mkdir(parents=True, exist_ok=True)
            (home / row[2]).write_text("stale\n")
        cp = self._write(self._store_with_login(), home)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for row in FILE_ROWS:
            self.assertFalse((home / row[2]).exists(), row[2])

class TestWhoIsAskedWhetherAWorkspaceCanAuthenticate(_Delivery):
    """Driver.agent_secret_present asks the machine that will run the agent, through its own login shell."""

    def _guest(self, login=None, mounted=True):
        home = self.tmp / "guest-home"
        home.mkdir(exist_ok=True)
        store = home / "agent-rw"   # where the share would be mounted
        cp = subprocess.run(["bash", str(REPO / "vm" / "shell-rc.sh"), str(REPO), str(store)],
                            env={"HOME": str(home), "PATH": os.environ["PATH"]},
                            capture_output=True, text=True, timeout=120)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        if mounted:
            store.mkdir(exist_ok=True)
        if login is not None:
            (store / FILE_ROWS[0][1]).write_text(login)
        return home

    def _ask(self, store, home, fn, secret):
        with stub_path({"ssh": FAKE_SSH, "tart": FAKE_TART}) as binp:
            env = dict(os.environ, **self._env(store, home, {"PATH": f"{binp}:{os.environ['PATH']}",
                                                             "WK_VM_STORE": str(self.tmp / "vmstore")}))
            with mock.patch.dict(os.environ, env), \
                    mock.patch("wk.store.Store.macos_host", new_callable=mock.PropertyMock, return_value=True):
                return ask(places.Registry(str(REPO), env=env, machine=Local()).load("vm"), fn, secret)

    def test_a_guest_whose_share_holds_the_login_answers_yes(self):
        cp = self._ask(self._store(), self._guest(FAKE_LOGIN),
                       "present", FILE_ROWS[0][0])
        self.assertIn("YES", cp.stdout, cp.stdout + cp.stderr)

    def test_a_guest_that_has_not_answers_no_however_full_this_store_is(self):
        store = self._store()
        for row in FILE_ROWS:
            store_path(store, row).write_text(FAKE_LOGIN)
            store_path(store, row).chmod(0o600)
        cp = self._ask(store, self._guest(), "present",
                       FILE_ROWS[0][0])
        self.assertIn("NO", cp.stdout, cp.stdout + cp.stderr)

    def test_an_empty_credential_file_is_not_a_login(self):
        cp = self._ask(self._store(), self._guest(""),
                       "present", FILE_ROWS[0][0])
        self.assertIn("NO", cp.stdout, cp.stdout + cp.stderr)

    def test_a_value_row_is_asked_of_the_guest_too(self):
        row = VM_ROWS[0]
        guest = self._guest()
        (guest / row[2]).parent.mkdir(parents=True, exist_ok=True)
        (guest / row[2]).write_text(f"{PLACEHOLDER}-{row[0]}\n")
        with_it = self._ask(self._store(), guest,
                            "present", row[0])
        self.assertIn("YES", with_it.stdout, with_it.stdout + with_it.stderr)

        (guest / row[2]).unlink()
        without = self._ask(self._store(values=[row[0]]), guest,
                            "present", row[0])
        self.assertIn("NO", without.stdout, without.stdout + without.stderr)

    def test_a_mounted_empty_share_names_this_machines_store(self):
        cp = self._ask(self._store(), self._guest(), "remedy",
                       FILE_ROWS[0][0])
        self.assertIn("/login in a 'wk ai claude' session", cp.stdout, cp.stdout + cp.stderr)

    def test_a_guest_without_the_share_is_told_to_boot_again(self):
        cp = self._ask(self._store(), self._guest(mounted=False),
                       "remedy", FILE_ROWS[0][0])
        self.assertIn("wk start demo", cp.stdout, cp.stdout)
        self.assertNotIn("wk key set", cp.stdout, cp.stdout)

    def test_a_value_rows_remedy_is_this_machines_store(self):
        name = VM_ROWS[0][0]
        cp = self._ask(self._store(), self._guest(), "remedy", name)
        self.assertIn(f"wk key set {name}", cp.stdout, cp.stdout + cp.stderr)


class TestTheDefaultAsksThePlace(_Delivery):
    """The default the container and remote drivers inherit: the workspace is asked, not this store."""

    def _driver(self):
        h = self.tmp / "place-home"
        (h / ".claude").mkdir(parents=True, exist_ok=True)
        return h

    def _ask(self, store, driver, fn, secret):
        env = dict(os.environ, **self._env(store, driver))
        with mock.patch.dict(os.environ, env):
            return ask(_Plain(env), fn, secret)


    def test_a_login_in_the_workspace_is_a_yes(self):
        driver = self._driver()
        row = FILE_ROWS[0]
        (driver / ".claude" / row[1]).write_text(FAKE_LOGIN)
        cp = self._ask(self._store(), driver, "present", row[0])
        self.assertIn("YES", cp.stdout, cp.stdout + cp.stderr)

    def test_a_full_store_the_workspace_never_got_is_a_no(self):
        store = self._store()
        for row in FILE_ROWS:
            store_path(store, row).write_text(FAKE_LOGIN)
            store_path(store, row).chmod(0o600)
        cp = self._ask(store, self._driver(), "present",
                       FILE_ROWS[0][0])
        self.assertIn("NO", cp.stdout, cp.stdout + cp.stderr)

    def test_an_empty_credential_file_is_not_a_login(self):
        driver = self._driver()
        (driver / ".claude" / FILE_ROWS[0][1]).write_text("")
        cp = self._ask(self._store(), driver, "present",
                       FILE_ROWS[0][0])
        self.assertIn("NO", cp.stdout, cp.stdout + cp.stderr)

    def test_a_value_row_is_read_where_the_driver_delivered_it(self):
        row = VALUE_ROWS[0]
        driver = self._driver()
        cp = self._ask(self._store(values=[row[0]]), driver,
                       "present", row[0])
        self.assertIn("NO", cp.stdout, cp.stdout + cp.stderr)
        (driver / row[2]).write_text(f"{PLACEHOLDER}-{row[0]}\n")
        cp = self._ask(self._store(), driver, "present", row[0])
        self.assertIn("YES", cp.stdout, cp.stdout + cp.stderr)

    def test_the_remedy_names_the_login_that_makes_one(self):
        cp = self._ask(self._store(), self._driver(), "remedy",
                       FILE_ROWS[0][0])
        self.assertIn("/login in a 'wk ai claude' session", cp.stdout,
                      cp.stdout + cp.stderr)


if __name__ == "__main__":
    unittest.main()
