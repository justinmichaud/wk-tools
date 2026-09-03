"""The agent's own credential: one token per machine, in the store, reaching
every workspace this machine makes.

`wk key set claude` stores it at $WK_STORE/secrets/claude-token. Each target driver
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
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from tests.support import func_body
from tests.support import assert_guest_start_converges, REPO, WkTest, bash, stub_path
from tests.test_pi_agent import TABLE

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
        block = text[text.index("# --- 6b. The agents' credentials"):
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
        cp = self._sh('printf "[%s]\\n" "$(wk_agent_secret claude)"; echo "rc=$?"', self._store())
        self.assertIn("[]", cp.stdout, cp.stdout + cp.stderr)
        self.assertIn("rc=0", cp.stdout, cp.stdout + cp.stderr)

    def test_stored_then_read_back(self):
        store = self._store()
        cp = self._sh(
            f'printf "%s\\n" {PLACEHOLDER} | wk_agent_secret_store claude\n'
            'printf "[%s]\\n" "$(wk_agent_secret claude)"',
            store)
        self.assertIn(f"[{PLACEHOLDER}]", cp.stdout, cp.stdout + cp.stderr)

    def test_it_is_written_unreadable_to_anyone_else(self):
        store = self._store()
        self._sh(f'printf "%s\\n" {PLACEHOLDER} | wk_agent_secret_store claude', store)
        mode = (store / "secrets" / "claude-token").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, oct(mode))

    def test_a_driver_moving_wk_store_does_not_move_the_token(self):
        """There is one token per *machine*. targets/vm.sh points $WK_STORE at
        its own state directory, so resolving the token against $WK_STORE would
        send `wk vm start` looking somewhere `wk key set claude` never writes --
        and find nothing, silently, in the command that most needs it."""
        cp = bash('''
. "$WK_ROOT/lib/common.sh"
WK_STORE=/the/machine/store
. "$WK_ROOT/lib/store.sh"
# What load_target records before a driver overrides $WK_STORE.
WK_STORE_DEFAULT=/the/machine/store
WK_STORE=/some/drivers/own/state
printf "path=%s\\n" "$(wk_agent_secret_path claude)"
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
printf "store=%s path=%s\\n" "$WK_STORE" "$(wk_agent_secret_path claude)"
''')
        self.assertIn("store=/some/vm/state", cp.stdout, cp.stdout + cp.stderr)
        self.assertIn("path=/the/machine/store/secrets/claude-token",
                      cp.stdout, cp.stdout + cp.stderr)

    def test_clearing_withdraws_it(self):
        store = self._store()
        cp = self._sh(
            f'printf "%s\\n" {PLACEHOLDER} | wk_agent_secret_store claude\n'
            'wk_agent_secret_clear claude\n'
            'printf "[%s]\\n" "$(wk_agent_secret claude)"',
            store)
        self.assertIn("[]", cp.stdout, cp.stdout + cp.stderr)
        self.assertFalse((store / "secrets" / "claude-token").exists())


class TestEveryTargetDeliversIt(unittest.TestCase):
    """Source-level: the wiring of the three deliveries. Each is *driven*
    below, against a scratch home a fake ssh runs the real remote commands
    against; what stays here is what only the source can say."""

    def test_a_container_links_the_live_store_mount(self):
        """One link per row of wk_agent_secrets, the claude token among them --
        the table is the authority and tests/test_pi_agent.py holds every
        reader to it."""
        text = (REPO / "container" / "firstrun.sh").read_text()
        self.assertIn('ln -sfn "/secrets/$_sfile"', text)
        self.assertIn("claude-token", (REPO / "lib" / "store.sh").read_text())

    def test_a_guest_is_written_on_every_start(self):
        """t_start has two arms -- a guest already running is converged, one
        that is not is booted first -- and a credential delivered on only one
        of them is half a delivery. Both arms call one `_converge_guest`,
        which writes the credentials once."""
        assert_guest_start_converges(self, '_write_agent_secrets "$name" "$ip"')
        self.assertIn("umask 077", (REPO / "targets" / "vm.sh").read_text())

    def test_one_reader_serves_every_secret_in_the_store(self):
        """A deploy key and an agent credential are the same read -- a file
        under the machine store's secrets/, here or through the podman machine
        that holds it. Two copies of it drift in the case that is hardest to
        test: the forwarded one."""
        text = (REPO / "lib" / "store.sh").read_text()
        self.assertEqual(1, text.count("_wk_secret_read() {"))
        for caller in ("wk_push_key", "wk_agent_secret"):
            with self.subTest(caller=caller):
                body = func_body(text, caller)
                self.assertIn("_wk_secret_read", body)
                self.assertNotIn("podman machine ssh", body)

    def test_every_writer_loops_the_one_table(self):
        """A name added to wk_agent_secrets reaches all three targets with
        nothing else to change, so none of them may name a row of its own."""
        for f in ("container/firstrun.sh", "targets/vm.sh", "cmd/remote"):
            text = (REPO / f).read_text()
            with self.subTest(script=f):
                self.assertIn("wk_agent_secrets", text)
                self.assertNotIn(".wk-agent-token", text)

    def test_the_token_is_never_an_argument(self):
        """An argument is in `ps` for everyone on the machine. Every writer
        takes it on stdin instead."""
        for f in ("cmd/key", "cmd/remote", "lib/store.sh", "targets/vm.sh"):
            text = (REPO / f).read_text()
            with self.subTest(script=f):
                self.assertNotIn("--token", text)
                self.assertNotIn("echo $_tok", text)
                self.assertNotIn("echo \"$_agent_tok\"", text)
        # The guest's copy is streamed, never quoted into the command.
        self.assertIn('| _ssh "$ip" "umask 077 && cat > \\$HOME/$(sh_quote "$shome")"',
                      (REPO / "targets" / "vm.sh").read_text())

    def test_a_build_box_streams_it_rather_than_asking_for_stdin_back(self):
        """`_rsh_q` is `ssh -n` -- stdin from /dev/null -- so a value piped
        into it lands as an empty file while the command reports success.
        The credential goes through `_rsh`, which is the one wrapper with a
        stdin. Driven by TestABuildBoxGetsThem below; this is the shape."""
        text = (REPO / "cmd" / "remote").read_text()
        self.assertIn('| _rsh "umask 077 && cat > \\$HOME/$(sh_quote "$_shome")"', text)

    def test_the_old_credentials_file_is_gone(self):
        """It was Linux-only and needed a `claude login` from inside a
        workspace to exist at all, so it could never be the answer for a macOS
        guest or a build box. Two ways to authenticate is one too many."""
        for f in ("container/firstrun.sh", "cmd/doctor"):
            self.assertNotIn("claude-credentials.json", (REPO / f).read_text(), f)


# --- the two targets that hold a copy, driven ---------------------------------
# A container follows a symlink into the read-only /secrets mount, so nothing
# has to be delivered to one and tests/test_pi_agent.py drives the linking. The
# other two copy, over ssh, and that is what these fake: the "machine" is a
# scratch directory the fake ssh runs the real remote command against, so the
# umask, the redirect and the rm are the real ones -- not a transcript of them.

# Records the command and how many bytes of stdin the far side was given, then
# runs it. `ssh -n` gives it /dev/null, so `stdin=0` is how a test says "this
# credential would have landed as an empty file".
FAKE_SSH = '''
have_stdin=1
for a in "$@"; do
    [ "$a" = "-n" ] && have_stdin=0
    last="$a"
done
tmp=$(mktemp)
# `-n` is what `_rsh_q` adds, and it is the whole of the defect this holds
# shut: the real ssh gives the far side /dev/null, so this one must too.
if [ "$have_stdin" = 1 ]; then cat > "$tmp"; else : > "$tmp"; fi
printf 'stdin=%s cmd=%s\n' "$(wc -c < "$tmp" | tr -d " ")" "$last" >> "$WK_TEST_SSH_LOG"
HOME="$WK_TEST_GUEST" sh -c "$last" < "$tmp"
rc=$?
rm -f "$tmp"
exit "$rc"
'''

# `tart`: enough for load_target vm to answer about one running guest.
FAKE_TART = '''
case "$1" in
list) echo '[{"Name":"wk-demo","State":"running","Source":"local"}]' ;;
ip)   echo 1.2.3.4 ;;
*)    exit 1 ;;
esac
'''


class _Delivery(WkTest):
    """A scratch machine home, a scratch store, and the ssh log."""

    def _home(self):
        h = self.tmp / "machine-home"
        h.mkdir(exist_ok=True)
        return h

    def _store(self, values=()):
        d = self.tmp / "store"
        (d / "secrets").mkdir(parents=True, exist_ok=True)
        for name, sfile, _shome, _var in TABLE:
            p = d / "secrets" / sfile
            if name in values:
                p.write_text(f"{PLACEHOLDER}-{name}\n")
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
            "XDG_STATE_HOME": str(self.tmp / "state"),
        }
        if extra:
            env.update(extra)
        return env

    def _ssh_lines(self):
        return [l for l in self.log.read_text().splitlines() if l.strip()]


class TestAGuestGetsThemOnStart(_Delivery):
    """targets/vm.sh's _write_agent_secrets, the real function: a guest mounts
    nothing of ours, so it holds a copy the host writes on every start and
    takes away again the moment the store has none."""

    def _write(self, store, home):
        with stub_path({"ssh": FAKE_SSH, "tart": FAKE_TART}) as binp:
            env = self._env(store, home,
                            {"PATH": f"{binp}:{os.environ['PATH']}",
                             "WK_VM_STORE": str(self.tmp / "vmstore")})
            return bash('''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target vm >/dev/null 2>&1
_write_agent_secrets demo 1.2.3.4
''', env=env)

    def test_every_named_secret_in_the_store_lands_in_the_guest(self):
        home = self._home()
        cp = self._write(self._store(values=[n for n, *_ in TABLE]), home)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for name, _sfile, shome, _var in TABLE:
            with self.subTest(name=name):
                self.assertEqual((home / shome).read_text(),
                                 f"{PLACEHOLDER}-{name}\n")

    def test_it_is_unreadable_to_anyone_else_in_the_guest(self):
        home = self._home()
        self._write(self._store(values=["claude"]), home)
        mode = (home / TABLE[0][2]).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, oct(mode))

    def test_a_store_with_none_withdraws_what_the_guest_holds(self):
        """The one state this must not leave behind: a guest holding a
        credential the host has taken away."""
        home = self._home()
        for _name, _sfile, shome, _var in TABLE:
            (home / shome).write_text("stale\n")
        cp = self._write(self._store(), home)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for _name, _sfile, shome, _var in TABLE:
            self.assertFalse((home / shome).exists(), shome)

    def test_one_absent_row_does_not_cost_the_next_one(self):
        """The loop reads the table on stdin, and every ssh that has nothing
        to stream has to be given /dev/null -- otherwise it drinks the rest of
        the table and the remaining rows are never delivered."""
        home = self._home()
        last = TABLE[-1]
        self._write(self._store(values=[last[0]]), home)
        self.assertEqual((home / last[2]).read_text(),
                         f"{PLACEHOLDER}-{last[0]}\n")
        self.assertEqual(len(TABLE), len(self._ssh_lines()), self.log.read_text())

    def test_the_value_is_never_an_argument(self):
        """An argument is in `ps` inside the guest, and in what a --debug run
        prints. The bytes go over stdin."""
        self._write(self._store(values=[n for n, *_ in TABLE]), self._home())
        text = self.log.read_text()
        self.assertNotIn(PLACEHOLDER, text, text)


class TestABuildBoxGetsThemAtSetup(_Delivery):
    """cmd/remote's credential block, lifted and run against a fake machine.

    The measured defect this holds shut: the block used `_rsh_q`, which is
    `ssh -n` -- stdin from /dev/null -- so the file landed EMPTY and the
    command still reported that it had written the token."""

    BLOCK = None

    @classmethod
    def setUpClass(cls):
        text = (REPO / "cmd" / "remote").read_text()
        start = text.index("while read -r _sname _ _shome _; do")
        end = text.index("unset _sname _shome _sval")
        cls.BLOCK = text[start:end]

    def _setup(self, store, home):
        with stub_path({"ssh": FAKE_SSH}) as binp:
            env = self._env(store, home,
                            {"PATH": f"{binp}:{os.environ['PATH']}",
                             "WK_REMOTE_HOST": "fakebox"})
            return bash(f'''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
# Through load_target, not by sourcing the driver: it is what records this
# machine's own store in $WK_STORE_DEFAULT before the driver points $WK_STORE
# at the remote root, and the credentials are in the former.
load_target remote >/dev/null 2>&1
r_host=fakebox
{self.BLOCK}
''', env=env)

    def test_the_credential_arrives_with_its_bytes(self):
        home = self._home()
        cp = self._setup(self._store(values=[n for n, *_ in TABLE]), home)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for name, _sfile, shome, _var in TABLE:
            with self.subTest(name=name):
                self.assertEqual((home / shome).read_text(),
                                 f"{PLACEHOLDER}-{name}\n",
                                 "empty file: the value went to an `ssh -n`")

    def test_the_far_side_is_given_stdin_at_all(self):
        """The twin of the above, said about the wrapper rather than the
        result: every write hands the machine bytes."""
        self._setup(self._store(values=[n for n, *_ in TABLE]), self._home())
        writes = [l for l in self._ssh_lines() if "cat >" in l]
        self.assertEqual(len(TABLE), len(writes), self.log.read_text())
        for l in writes:
            self.assertNotIn("stdin=0", l, l)

    def test_it_is_unreadable_to_anyone_else_on_a_shared_machine(self):
        home = self._home()
        self._setup(self._store(values=["claude"]), home)
        mode = (home / TABLE[0][2]).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, oct(mode))

    def test_a_store_with_none_takes_the_copy_off_the_machine(self):
        """It is a machine other people are on, so a credential left behind
        after it was withdrawn here is the state that would matter."""
        home = self._home()
        for _name, _sfile, shome, _var in TABLE:
            (home / shome).write_text("stale\n")
        cp = self._setup(self._store(), home)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for _name, _sfile, shome, _var in TABLE:
            self.assertFalse((home / shome).exists(), shome)

    def test_one_absent_row_does_not_cost_the_next_one(self):
        """The removal goes through `_rsh_q` for the same reason the write
        does not: the loop's stdin is the table, and a plain ssh would drink
        the rest of it."""
        home = self._home()
        last = TABLE[-1]
        self._setup(self._store(values=[last[0]]), home)
        self.assertEqual((home / last[2]).read_text(),
                         f"{PLACEHOLDER}-{last[0]}\n")
        self.assertEqual(len(TABLE), len(self._ssh_lines()), self.log.read_text())

    def test_the_value_is_never_an_argument(self):
        self._setup(self._store(values=[n for n, *_ in TABLE]), self._home())
        text = self.log.read_text()
        self.assertNotIn(PLACEHOLDER, text, text)


if __name__ == "__main__":
    unittest.main()
