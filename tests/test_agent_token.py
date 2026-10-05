"""The agents' credentials (AGENT_SECRETS): shell/bashrc exports the token, a container links the read-only
/secrets mount, a macOS guest is written a copy on every start, a build box at `wk machine setup`; the claude.ai
login stays with the injector, and a container or guest holds its placeholder. Values here are placeholders."""
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
from tests.test_pi_agent import TABLE, store_path

sys.path.insert(0, str(REPO / "lib"))
from wk import claudelogin, guest, places, secrets  # noqa: E402
from wk.machine import Fake, Local, Result  # noqa: E402

RC = REPO / "shell" / "bashrc"
VAR = "CLAUDE_CODE_OAUTH_TOKEN"


def delivered_to(kind, rows=TABLE):
    """The rows the delivery column sends to one kind of place."""
    return [r for r in rows if kind in r[4].split(",")]


CONTAINER_ROWS = delivered_to("container")
VM_ROWS = delivered_to("vm")
REMOTE_ROWS = delivered_to("remote")

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
        (h / ".wk-agent-token").unlink(missing_ok=True)
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

    def test_every_shell_exports_it_and_no_file_means_no_variable(self):
        for contents, want in ((PLACEHOLDER + "\n", PLACEHOLDER), (None, "")):
            home = self._home(contents)
            for what, (shell, args) in self.SHELLS.items():
                if shutil.which(shell):
                    with self.subTest(shell=what, contents=contents):
                        self.assertEqual(self._value(shell, args, home), want)

    def test_a_dangling_symlink_means_no_token(self):
        home = self._home()
        (home / ".wk-agent-token").symlink_to(home / "nothing-here")
        self.assertEqual(self._value("bash", ["-c"], home), "")

    def test_the_comment_line_is_skipped(self):
        home = self._home("# wk: written by lib/wk/guest.py\n" + PLACEHOLDER + "\n")
        self.assertEqual(self._value("bash", ["-c"], home), PLACEHOLDER)


class TestAWorkspaceHoldsThePlaceholderLogin(WkTest):
    """container/firstrun.sh writes it where shell/bashrc points the CLI, and a machine with none points it nowhere."""

    def test_a_container_is_made_holding_it(self):
        text = (REPO / "container" / "firstrun.sh").read_text()
        block = text.split("# The claude.ai login is a placeholder", 1)[1].split("\n", 1)[1].split("_agent_secrets()", 1)[0]
        home = self.tmp / "home"
        home.mkdir()
        cp = bash('WK_TOOLS="$WK_ROOT"; HOME=%s\n%s' % (home, block))
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        held = home / secrets.LOGIN_DIR / ".credentials.json"
        self.assertEqual(claudelogin.placeholder(), held.read_text())
        self.assertEqual(0o600, held.stat().st_mode & 0o777)

    def test_the_shell_points_the_cli_at_it_only_where_it_is(self):
        home = self.tmp / "home"
        home.mkdir()
        show = '. "%s"; printf %%s "${CLAUDE_SECURESTORAGE_CONFIG_DIR-unset}"' % RC
        env = {"HOME": str(home), "TERM": "dumb", "PATH": "/usr/bin:/bin"}
        self.assertEqual("unset", subprocess.run(["bash", "-c", show], env=env, capture_output=True, text=True).stdout)
        (home / secrets.LOGIN_DIR).mkdir()
        self.assertEqual(str(home / secrets.LOGIN_DIR),
                         subprocess.run(["bash", "-c", show], env=env, capture_output=True, text=True).stdout)


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

    def test_every_kind_a_row_names_is_a_kind_that_exists(self):
        kinds = set(secrets.LOGIN_KINDS)
        for row in TABLE:
            kinds.update(row[4].split(","))
        self.assertEqual(set(), kinds - {"container", "vm", "remote"}, kinds)

    def test_a_kind_given_the_placeholder_login_is_given_no_token(self):
        for kind in secrets.LOGIN_KINDS:
            with self.subTest(kind=kind):
                self.assertNotIn("claude", [r[0] for r in delivered_to(kind)])

    def test_a_row_a_container_is_not_given_is_taken_away(self):
        """container/firstrun.sh's own loop, lifted and run against a scratch home."""
        text = (REPO / "container" / "firstrun.sh").read_text()
        block = text.split("_agent_secrets() {", 1)[1]
        block = "_agent_secrets() {" + block.split("\nEOF\n", 1)[0] + "\nEOF\n"
        with tempfile.TemporaryDirectory(prefix="wk-test-firstrun-") as home:
            home = Path(home)
            for row in TABLE:      # what an older container linked
                (home / row[2]).symlink_to("/secrets/" + row[1])
            cp = bash(f'''
log() {{ printf '%s\\n' "$*"; }}
WK_TOOLS="$WK_ROOT"
HOME={home}
{block}
''')
            self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
            for row in TABLE:
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


def ask(driver, secret):
    return SimpleNamespace(stdout=driver.agent_secret_remedy("demo", secret), stderr="")


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

    def _write(self, store, home):
        with stub_path({"ssh": FAKE_SSH, "tart": FAKE_TART}) as binp:
            env = self._env(store, home,
                            {"PATH": f"{binp}:{os.environ['PATH']}",
                             "WK_VM_STORE": str(self.tmp / "vmstore")})
            return guest_step(env, "write_agent_secrets")

    def _rc(self, home):
        return subprocess.run(["bash", str(REPO / "vm" / "shell-rc.sh"), str(REPO)],
                              env={"HOME": str(home), "PATH": os.environ["PATH"]},
                              capture_output=True, text=True, timeout=120)

    def _guest(self):
        home = self._home()
        cp = self._rc(home)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        return home


class TestEachPlaceGetsThemWhereItIsMade(_Delivery):
    """A guest is written a copy of each of its value rows on every start (write_agent_secrets), a build box at
    `wk machine setup` (over a fake ssh whose `-n` gives the far side /dev/null); a store with none withdraws it."""

    def guest(self, store, home):
        self._write(store, home)
        return VM_ROWS, len(TABLE) + 1

    def build_box(self, store, home):
        from wk import machine_cmd
        with stub_path({"ssh": FAKE_SSH}) as binp:
            env = self._env(store, home, {"PATH": f"{binp}:{os.environ['PATH']}"})
            with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(io.StringIO()):
                # The credentials are in this machine's store (WK_STORE_DEFAULT), not the place's remote root.
                t = places.Remote("fakebox", str(REPO), dict(os.environ), Local())
                machine_cmd.Machines(REPO, env=dict(os.environ)).credentials(t, "fakebox")
        return REMOTE_ROWS, len(REMOTE_ROWS)

    def places(self):
        return (("guest", self.guest), ("build box", self.build_box))

    def test_every_value_row_lands_at_mode_600_and_never_as_an_argument(self):
        for place, deliver in self.places():
            with self.subTest(place):
                home = self.tmp / place
                home.mkdir()
                rows, _ = deliver(self._store(values=[n for n, *_ in TABLE]), home)
                for name, _sfile, shome, *_ in rows:
                    self.assertEqual((home / shome).read_text(), f"{PLACEHOLDER}-{name}\n")
                    self.assertEqual(0o600, (home / shome).stat().st_mode & 0o777)
                self.assertNotIn(PLACEHOLDER, self.log.read_text(), "a value is never an argument")

    def test_a_store_with_none_withdraws_what_the_place_holds(self):
        for place, deliver in self.places():
            with self.subTest(place):
                home = self.tmp / place
                for _name, _sfile, shome, *_ in TABLE:
                    (home / shome).parent.mkdir(parents=True, exist_ok=True)
                    (home / shome).write_text("stale\n")
                rows, _ = deliver(self._store(), home)
                for _name, _sfile, shome, *_ in rows:
                    self.assertFalse((home / shome).exists(), shome)

    def test_one_absent_row_does_not_cost_the_next_one(self):
        for place, deliver in self.places():
            with self.subTest(place):
                home = self.tmp / place
                home.mkdir()
                last = (VM_ROWS if place == "guest" else REMOTE_ROWS)[-1]
                _, calls = deliver(self._store(values=[last[0]]), home)
                self.assertEqual((home / last[2]).read_text(), f"{PLACEHOLDER}-{last[0]}\n")
                self.assertEqual(calls, len(self._ssh_lines()), self.log.read_text())

    def test_a_guest_holds_the_placeholder_login_and_a_build_box_none(self):
        store = self._store(values=[n for n, *_ in TABLE])
        login = Path(secrets.Secrets(REPO, self._env(store, self._home())).cred_path("claude-login"))
        login.parent.mkdir(parents=True, exist_ok=True)
        login.write_text('{"claudeAiOauth": {"accessToken": "sk-ant-oat01-REAL", "refreshToken": "sk-ant-ort01-REAL"}}')
        home = self._home()
        self.guest(store, home)
        held = home / secrets.LOGIN_DIR / ".credentials.json"
        self.assertEqual(claudelogin.placeholder(), held.read_text())
        self.assertEqual(0o600, held.stat().st_mode & 0o777)
        self.assertNotIn("REAL", self.log.read_text())
        box = self.tmp / "box"
        box.mkdir()
        self.build_box(store, box)
        self.assertFalse((box / secrets.LOGIN_DIR).exists())
        self.assertNotIn(".credentials.json", self.log.read_text())


class TestAGuestRcNamesNoLoginDirectory(_Delivery):
    """shell/bashrc names the placeholder's directory; the guest's rc drops what an older one exported."""

    def test_an_older_rc_stanza_is_taken_away(self):
        for old in ('export CLAUDE_SECURESTORAGE_CONFIG_DIR="$HOME/.claude-login"\n',
                    'export CLAUDE_SECURESTORAGE_CONFIG_DIR="/Volumes/My Shared Files/agent-rw"\n'):
            with self.subTest(old=old):
                home = self.tmp / ("old-home" + str(len(old)))
                home.mkdir()
                (home / ".zshrc").write_text("\n# wk-tools: the claude.ai login this host shares over virtiofs\n" + old)
                cp = self._rc(home)
                self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
                self.assertNotIn("CLAUDE_SECURESTORAGE_CONFIG_DIR", (home / ".zshrc").read_text())


class TestWhoIsAskedWhetherAWorkspaceCanAuthenticate(_Delivery):
    """A guest's remedy is this machine's store, which delivers its secrets."""

    def _ask(self, store, home, secret):
        with stub_path({"ssh": FAKE_SSH, "tart": FAKE_TART}) as binp:
            env = dict(os.environ, **self._env(store, home, {"PATH": f"{binp}:{os.environ['PATH']}",
                                                             "WK_VM_STORE": str(self.tmp / "vmstore")}))
            with mock.patch.dict(os.environ, env), \
                    mock.patch("wk.store.Store.macos_host", new_callable=mock.PropertyMock, return_value=True):
                return ask(places.Registry(str(REPO), env=env, machine=Local()).load("vm"), secret)

    def test_a_value_rows_remedy_is_this_machines_store(self):
        name = VM_ROWS[0][0]
        cp = self._ask(self._store(), self._guest(), name)
        self.assertIn(f"wk key set {name}", cp.stdout, cp.stdout + cp.stderr)


if __name__ == "__main__":
    unittest.main()
