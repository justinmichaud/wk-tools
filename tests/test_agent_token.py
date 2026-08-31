"""The agent's own credential: one token per machine, in the store, reaching
every workspace this machine makes.

`wk key claude` stores it at $WK_STORE/secrets/claude-token. Each target driver
puts it where the workspace can read it, and `shell/bashrc` is the only reader,
exporting CLAUDE_CODE_OAUTH_TOKEN so a workspace starts authenticated instead
of asking for /login -- which on a macOS guest cannot be answered at all
through an editor's remote server, since no login Keychain is unlocked there.

Three targets, three deliveries, one path in the workspace:

    container   a symlink onto the read-only /secrets mount -- live, so a
                rotation reaches every container without rebuilding one
    macOS guest a copy written by the host on every start
    build box   a copy written by `wk remote setup`

Nothing here uses a real token: the value is a placeholder string, and what is
under test is the plumbing, never the credential.

Run: python3 -m unittest tests.test_agent_token -v
"""
import shutil
import subprocess
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, bash

RC = REPO / "shell" / "bashrc"
VAR = "CLAUDE_CODE_OAUTH_TOKEN"

# Not a token, and deliberately nothing like one.
PLACEHOLDER = "placeholder-value-for-this-test"

EDITED = (
    "cmd/key", "cmd/remote", "lib/store.sh", "lib/common.sh",
    "shell/bashrc", "container/firstrun.sh", "targets/vm.sh",
    "vm/shell-rc.sh", "vm/provision-base.sh",
)


class TestScriptsParse(unittest.TestCase):
    """Every script this touched still parses. `bash -n` is the cheapest thing
    that catches a quoting mistake in a heredoc, which is most of what these
    files are."""

    def test_bash_n(self):
        for f in EDITED:
            with self.subTest(script=f):
                cp = subprocess.run(["bash", "-n", str(REPO / f)],
                                    capture_output=True, text=True, timeout=60)
                self.assertEqual(cp.returncode, 0, cp.stderr)


class TestTheShellExportsIt(WkTest):
    """shell/bashrc is the only reader, and it has to work in every shell a
    person or `wk` starts -- an editor's terminal pane above all, which is not
    a login shell."""

    SHELLS = {
        "editor terminal pane": ("zsh", ["-i", "-c"]),
        "login zsh": ("zsh", ["-l", "-c"]),
        "bash -lc (every t_exec)": ("bash", ["-lc"]),
        "non-interactive bash": ("bash", ["-c"]),
    }

    def _home(self, contents=None):
        h = self.tmp / "home"
        h.mkdir(exist_ok=True)
        # The rc is sourced by hand here rather than through the four rc files:
        # which file each target wires is tests/test_egress.py's subject, and
        # this one is about what the rc does once it is read.
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
        """The rc is shared by every machine in the fleet, so absence has to
        mean absence -- a workstation must not acquire a token variable."""
        home = self._home()
        for what, (shell, args) in self.SHELLS.items():
            if not shutil.which(shell):
                continue
            with self.subTest(shell=what):
                self.assertEqual(self._value(shell, args, home), "")

    def test_a_dangling_symlink_means_no_token(self):
        """What a container has before `wk key claude` is ever run: firstrun
        makes the link unconditionally so that storing a token later needs no
        rebuild, which means the link spends that time pointing at nothing."""
        home = self._home()
        (home / ".wk-agent-token").symlink_to(home / "nothing-here")
        self.assertEqual(self._value("bash", ["-c"], home), "")

    def test_the_comment_line_is_skipped(self):
        home = self._home("# wk: written by targets/vm.sh\n" + PLACEHOLDER + "\n")
        self.assertEqual(self._value("bash", ["-c"], home), PLACEHOLDER)

    def test_reading_it_does_not_fork(self):
        """It runs in every shell; `cat` here would be a process each. The
        comments are stripped first -- they name the commands this must not
        run, which is the point of them."""
        text = RC.read_text()
        block = text[text.index("# --- 6b. The agent's credential"):
                     text.index("# --- 7. Completion")]
        code = "\n".join(l for l in block.splitlines() if not l.lstrip().startswith("#"))
        for forker in ("cat ", "sed ", "awk ", "$(", "`"):
            self.assertNotIn(forker, code, f"{forker!r} in the credential block")


class TestTheStoreIsTheOnePlace(WkTest):
    """One token per machine, read and written through one pair of functions,
    so no command has its own idea of where it lives."""

    def _store(self):
        d = self.tmp / "store"
        (d / "secrets").mkdir(parents=True)
        return d

    def _sh(self, script, store):
        return bash(f'''
. "$WK_ROOT/lib/common.sh"
WK_STORE={store}
. "$WK_ROOT/lib/store.sh"
{script}
''')

    def test_absent_reads_as_nothing_and_is_not_an_error(self):
        cp = self._sh('printf "[%s]\\n" "$(wk_agent_token)"; echo "rc=$?"', self._store())
        self.assertIn("[]", cp.stdout, cp.stdout + cp.stderr)
        self.assertIn("rc=0", cp.stdout, cp.stdout + cp.stderr)

    def test_stored_then_read_back(self):
        store = self._store()
        cp = self._sh(
            f'printf "%s\\n" {PLACEHOLDER} | wk_agent_token_store\n'
            'printf "[%s]\\n" "$(wk_agent_token)"',
            store)
        self.assertIn(f"[{PLACEHOLDER}]", cp.stdout, cp.stdout + cp.stderr)

    def test_it_is_written_unreadable_to_anyone_else(self):
        store = self._store()
        self._sh(f'printf "%s\\n" {PLACEHOLDER} | wk_agent_token_store', store)
        mode = (store / "secrets" / "claude-token").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, oct(mode))

    def test_a_driver_moving_wk_store_does_not_move_the_token(self):
        """There is one token per *machine*. targets/vm.sh points $WK_STORE at
        its own state directory, so resolving the token against $WK_STORE would
        send `wk vm start` looking somewhere `wk key claude` never writes --
        and find nothing, silently, in the command that most needs it."""
        cp = bash('''
. "$WK_ROOT/lib/common.sh"
WK_STORE=/the/machine/store
. "$WK_ROOT/lib/store.sh"
# What load_target records before a driver overrides $WK_STORE.
WK_STORE_DEFAULT=/the/machine/store
WK_STORE=/some/drivers/own/state
printf "path=%s\\n" "$(wk_agent_token_path)"
''')
        self.assertIn("path=/the/machine/store/secrets/claude-token",
                      cp.stdout, cp.stdout + cp.stderr)

    def test_the_vm_driver_itself_still_finds_it(self):
        """The same thing through the real driver rather than a stand-in."""
        cp = bash('''
. "$WK_ROOT/lib/common.sh"
WK_STORE=/the/machine/store
. "$WK_ROOT/lib/store.sh"
WK_STORE_DEFAULT=/the/machine/store
WK_VM_STORE=/some/vm/state
. "$WK_ROOT/targets/vm.sh"
printf "store=%s path=%s\\n" "$WK_STORE" "$(wk_agent_token_path)"
''')
        self.assertIn("store=/some/vm/state", cp.stdout, cp.stdout + cp.stderr)
        self.assertIn("path=/the/machine/store/secrets/claude-token",
                      cp.stdout, cp.stdout + cp.stderr)

    def test_clearing_withdraws_it(self):
        store = self._store()
        cp = self._sh(
            f'printf "%s\\n" {PLACEHOLDER} | wk_agent_token_store\n'
            'wk_agent_token_clear\n'
            'printf "[%s]\\n" "$(wk_agent_token)"',
            store)
        self.assertIn("[]", cp.stdout, cp.stdout + cp.stderr)
        self.assertFalse((store / "secrets" / "claude-token").exists())


class TestEveryTargetDeliversIt(unittest.TestCase):
    """Source-level: the three deliveries, none of which a hermetic call can
    reach -- each needs a real container, guest or build machine."""

    def test_a_container_links_the_live_store_mount(self):
        text = (REPO / "container" / "firstrun.sh").read_text()
        self.assertIn("ln -sfn /secrets/claude-token", text)

    def test_a_guest_is_written_on_every_start(self):
        vm = (REPO / "targets" / "vm.sh").read_text()
        self.assertEqual(2, vm.count('_write_agent_token "$name" "$ip"'))
        self.assertIn("umask 077", vm)

    def test_a_build_box_gets_one_at_setup(self):
        text = (REPO / "cmd" / "remote").read_text()
        self.assertIn(".wk-agent-token", text)
        self.assertIn("umask 077", text)

    def test_the_token_is_never_an_argument(self):
        """An argument is in `ps` for everyone on the machine. Every writer
        takes it on stdin instead."""
        for f in ("cmd/key", "cmd/remote", "lib/store.sh"):
            text = (REPO / f).read_text()
            with self.subTest(script=f):
                self.assertNotIn("--token", text)
                self.assertNotIn("echo $_tok", text)
                self.assertNotIn("echo \"$_agent_tok\"", text)

    def test_withdrawing_it_reaches_the_copies(self):
        """A container follows the link, so it needs nothing. The two that hold
        copies must delete them, or a machine keeps a credential the store no
        longer has."""
        self.assertIn('rm -f "$HOME/.wk-agent-token"', (REPO / "targets" / "vm.sh").read_text())
        self.assertIn('rm -f "$HOME/.wk-agent-token"', (REPO / "cmd" / "remote").read_text())

    def test_the_old_credentials_file_is_gone(self):
        """It was Linux-only and needed a `claude login` from inside a
        workspace to exist at all, so it could never be the answer for a macOS
        guest or a build box. Two ways to authenticate is one too many."""
        for f in ("container/firstrun.sh", "cmd/doctor"):
            self.assertNotIn("claude-credentials.json", (REPO / f).read_text(), f)


if __name__ == "__main__":
    unittest.main()
