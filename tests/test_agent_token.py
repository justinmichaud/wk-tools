"""The agent's own credential: one token per machine, in this device's secrets
directory, reaching every workspace this machine makes.

`wk key set claude` stores it at <wk_secrets_dir>/claude-token -- on macOS a
directory on the host, which the podman machine mounts read-only at
$WK_STORE/secrets, so storing one needs no VM. Each target driver
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
import re
import shutil
import subprocess
import unittest
from pathlib import Path, PurePosixPath

from tests.support import func_body
from tests.support import assert_guest_start_converges, REPO, WkTest, bash, stub_path
from tests.test_pi_agent import FILE_ROWS, TABLE, VALUE_ROWS, store_path

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
        """The secrets directory is this device's own (wk_secrets_dir), which
        on macOS is not under $WK_STORE at all -- so a test that must not
        touch the real one points WK_HOST_SECRETS at its scratch copy. The two
        spellings are one directory, so pointing both at it is what a real
        machine looks like."""
        return bash(f'''
. "$WK_ROOT/lib/common.sh"
WK_STORE={store}
. "$WK_ROOT/lib/store.sh"
{script}
''', env={"WK_HOST_SECRETS": str(store / "secrets")})

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
        and find nothing, silently, in the command that most needs it.

        Two spellings of one directory, and neither follows a driver: on a
        macOS host it is this device's own path, and where the store is this
        machine's own it is the store recorded before the override."""
        for extra, want in (
            ('WK_HOST_SECRETS=/this/device/secrets\n',
             "path=/this/device/secrets/claude-token"),
            ('WK_IN_VM=1\n', "path=/the/machine/store/secrets/claude-token"),
        ):
            with self.subTest(want=want):
                cp = bash('''
. "$WK_ROOT/lib/common.sh"
WK_STORE=/the/machine/store
''' + extra + '''
. "$WK_ROOT/lib/store.sh"
# What load_target records before a driver overrides $WK_STORE.
WK_STORE_DEFAULT=/the/machine/store
WK_STORE=/some/drivers/own/state
printf "path=%s\\n" "$(wk_agent_secret_path claude)"
''')
                self.assertIn(want, cp.stdout, cp.stdout + cp.stderr)

    def test_the_vm_driver_itself_still_finds_it(self):
        """The same thing through the real driver rather than a stand-in: the
        driver moves $WK_STORE to its own state directory and the credential
        is still read from this device's."""
        cp = bash('''
. "$WK_ROOT/lib/common.sh"
WK_STORE=/the/machine/store
. "$WK_ROOT/lib/store.sh"
WK_STORE_DEFAULT=/the/machine/store
WK_VM_STORE=/some/vm/state
. "$WK_ROOT/targets/vm.sh"
printf "store=%s path=%s\\n" "$WK_STORE" "$(wk_agent_secret_path claude)"
''', env={"WK_HOST_SECRETS": "/this/device/secrets"})
        self.assertIn("store=/some/vm/state", cp.stdout, cp.stdout + cp.stderr)
        self.assertIn("path=/this/device/secrets/claude-token",
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

    def test_one_reader_serves_every_secret_here(self):
        """A deploy key and an agent credential are the same read -- a file in
        this machine's secrets directory -- through one reader, and nothing
        crosses into the podman machine to read or write one. The one hop left
        in the file is push_agent_exec, which drives the machine's ssh-agent
        and injector, never a secret file."""
        text = (REPO / "lib" / "store.sh").read_text()
        self.assertEqual(1, text.count("_wk_secret_read() {"))
        for fn in ("_wk_secret_read", "wk_agent_secret_store",
                   "wk_agent_secret_clear", "wk_push_key"):
            with self.subTest(no_hop_in=fn):
                self.assertNotIn("podman machine ssh", func_body(text, fn))
        self.assertEqual(1, text.count("podman machine ssh"))
        self.assertIn("podman machine ssh", func_body(text, "push_agent_exec"))
        for caller in ("wk_push_key", "wk_agent_secret"):
            with self.subTest(caller=caller):
                self.assertIn("_wk_secret_read", func_body(text, caller))
        for caller, where in (("wk_push_key", "wk_push_held_dir"),
                              ("wk_agent_secret_path", "wk_secrets_dir")):
            with self.subTest(dir_of=caller):
                self.assertIn(where, func_body(text, caller))

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
            # The credentials are this device's own directory, not the
            # store's, and a test must not read or write the real one.
            "WK_HOST_SECRETS": str(store / "secrets"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
        }
        if extra:
            env.update(extra)
        return env

    def _ssh_lines(self):
        return [l for l in self.log.read_text().splitlines() if l.strip()]


class TestAGuestGetsThemOnStart(_Delivery):
    """targets/vm.sh's _write_agent_secrets, the real function: a guest mounts
    nothing of ours, so it holds a copy of every *value* row, written by the
    host on every start and taken away again the moment the store has none.
    The file row is the class below."""

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

    def test_every_value_row_in_the_store_lands_in_the_guest(self):
        home = self._home()
        cp = self._write(self._store(values=[n for n, *_ in TABLE]), home)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for name, _sfile, shome, _var, _kind in VALUE_ROWS:
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
        for _name, _sfile, shome, _var, _kind in TABLE:
            (home / shome).parent.mkdir(parents=True, exist_ok=True)
            (home / shome).write_text("stale\n")
        cp = self._write(self._store(), home)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for _name, _sfile, shome, _var, _kind in TABLE:
            self.assertFalse((home / shome).exists(), shome)

    def test_one_absent_row_does_not_cost_the_next_one(self):
        """The loop reads the table on stdin, and every ssh that has nothing
        to stream has to be given /dev/null -- otherwise it drinks the rest of
        the table and the remaining rows are never delivered."""
        home = self._home()
        last = VALUE_ROWS[-1]
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
        start = text.index("while read -r _sname _ _shome _ _skind; do")
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
        for name, _sfile, shome, _var, _kind in VALUE_ROWS:
            with self.subTest(name=name):
                self.assertEqual((home / shome).read_text(),
                                 f"{PLACEHOLDER}-{name}\n",
                                 "empty file: the value went to an `ssh -n`")

    def test_the_far_side_is_given_stdin_at_all(self):
        """The twin of the above, said about the wrapper rather than the
        result: every write hands the machine bytes."""
        self._setup(self._store(values=[n for n, *_ in TABLE]), self._home())
        writes = [l for l in self._ssh_lines() if "cat >" in l]
        self.assertEqual(len(VALUE_ROWS), len(writes), self.log.read_text())
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
        for _name, _sfile, shome, _var, _kind in VALUE_ROWS:
            (home / shome).write_text("stale\n")
        cp = self._setup(self._store(), home)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for _name, _sfile, shome, _var, _kind in VALUE_ROWS:
            self.assertFalse((home / shome).exists(), shome)

    def test_one_absent_row_does_not_cost_the_next_one(self):
        """The removal goes through `_rsh_q` for the same reason the write
        does not: the loop's stdin is the table, and a plain ssh would drink
        the rest of it."""
        home = self._home()
        last = VALUE_ROWS[-1]
        self._setup(self._store(values=[last[0]]), home)
        self.assertEqual((home / last[2]).read_text(),
                         f"{PLACEHOLDER}-{last[0]}\n")
        self.assertEqual(len(VALUE_ROWS), len(self._ssh_lines()),
                         self.log.read_text())

    def test_the_account_login_never_reaches_a_shared_machine(self):
        """A file row is the claude.ai account login, and a build box is a
        machine other people are root on: an account credential there is
        theirs, and its refresh token would rotate out from under every other
        holder as well. Not delivered, and not even mentioned in the ssh."""
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


# --- the file row: one shared file, and no copies at all ---------------------
# The claude.ai login is not a value an agent reads out of a variable: it is a
# document the Claude CLI *rewrites*, spending the refresh token in it and
# storing the rotated one back. So the delivery is a directory, not a link, and
# it reaches only a workspace that can look at the very bytes this machine
# does. A guest could hold nothing but a copy, whose first refresh would
# invalidate the original, so a guest is given none and logs in for itself.

# A credential-shaped document, and deliberately nothing like a real one. Two
# lines, so a reader that took only the first would be caught.
FAKE_LOGIN = ('{"claudeAiOauth":{"accessToken":"' + PLACEHOLDER + '",\n'
              '"refreshToken":"' + PLACEHOLDER + '","scopes":["user:profile"]}}')


class TestAGuestIsNeverGivenTheFileRow(_Delivery):
    """targets/vm.sh's _write_agent_secrets, the real function: a credential
    its own tool rewrites in place is never copied into a guest, whatever this
    machine's store holds. A copy would be a second holder whose first refresh
    invalidates the bytes every container here shares; the guest logs in for
    itself instead. Delivered as absence, so a copy an older start left behind
    goes away like any withdrawn credential."""

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
            return bash('''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target vm >/dev/null 2>&1
_write_agent_secrets demo 1.2.3.4
''', env=env)

    def test_a_store_that_holds_one_still_delivers_nothing(self):
        home = self._home()
        cp = self._write(self._store_with_login(), home)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for row in FILE_ROWS:
            with self.subTest(name=row[0]):
                self.assertFalse((home / row[2]).exists(), row[2])

    def test_the_bytes_never_go_down_the_channel_at_all(self):
        """Not "written and removed": the copy is the whole exposure, so the
        document is never read out of the store or streamed over the ssh."""
        self._write(self._store_with_login(), self._home())
        log = self.log.read_text()
        self.assertNotIn(PLACEHOLDER, log, log)
        self.assertNotIn("claudeAiOauth", log, log)
        for line in self._ssh_lines():
            if FILE_ROWS[0][2] in line:
                self.assertIn("stdin=0", line, line)

    def test_a_copy_an_older_start_left_behind_is_taken_away(self):
        """Unconditional, so a guest converges on the next `wk vm start`
        rather than keeping a credential nothing here can revoke."""
        home = self._home()
        for row in FILE_ROWS:
            (home / row[2]).parent.mkdir(parents=True, exist_ok=True)
            (home / row[2]).write_text("stale\n")
        cp = self._write(self._store_with_login(), home)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for row in FILE_ROWS:
            self.assertFalse((home / row[2]).exists(), row[2])

    def test_the_removal_does_not_cost_the_value_rows_beside_it(self):
        """One ssh per row and no more: the loop reads the table on stdin, so
        a delivery that drank it would lose the rows after this one."""
        home = self._home()
        self._write(self._store_with_login(), home)
        self.assertEqual(len(TABLE), len(self._ssh_lines()), self.log.read_text())

    def test_the_rule_is_the_kind_column_and_not_a_name(self):
        """So a rotating credential added to wk_agent_secrets is kept out of a
        guest without an edit in the driver."""
        vm = (REPO / "targets" / "vm.sh").read_text()
        for row in FILE_ROWS:
            with self.subTest(name=row[0]):
                self.assertNotIn(row[0], vm, row[0])

    def test_the_guests_own_store_is_a_path_this_host_never_writes(self):
        """The arrangement that makes the removal above safe: what the host
        takes out of the guest and what a `claude auth login` in there writes
        are two directories, so no rule has to tell them apart."""
        rc = (REPO / "vm" / "shell-rc.sh").read_text()
        m = re.search(r'CLAUDE_SECURESTORAGE_CONFIG_DIR="\$HOME/([^"]+)"', rc)
        self.assertIsNotNone(m, rc)
        own = PurePosixPath(m.group(1))
        for row in FILE_ROWS:
            with self.subTest(name=row[0]):
                host_writes = PurePosixPath(row[2])
                self.assertNotEqual(own / row[1], host_writes)
                self.assertNotIn(own, host_writes.parents)

    def test_the_guest_reads_a_file_and_not_a_keychain(self):
        """A Mac's Claude CLI prefers a login-Keychain item, which no ssh
        session has unlocked. Naming the credential store directory also names
        that item -- the CLI appends a hash of the directory to the item's
        service name -- so the lookup misses and the file is what is used.

        In the guest's own rc and not the shared one: that is read on the
        workstation too, whose real Keychain item must keep working."""
        rc = (REPO / "vm" / "shell-rc.sh").read_text()
        self.assertIn('CLAUDE_SECURESTORAGE_CONFIG_DIR="$HOME/', rc)
        self.assertNotIn("CLAUDE_SECURESTORAGE_CONFIG_DIR",
                         (REPO / "shell" / "bashrc").read_text().split(
                             "if [ -d /agent-rw ]")[0])

    def test_an_rc_pointing_at_the_directory_the_host_clears_is_converged(self):
        """A guest whose shell names ~/.claude would read a store the host
        empties on every start, so the stanza is taken out before this file's
        own is added -- and exactly one export line is left."""
        home = self.tmp / "rc-home"
        home.mkdir(exist_ok=True)
        rcfile = home / ".zshrc"
        rcfile.write_text(
            "\n# wk-tools: the Claude credential the host writes here, not a Keychain\n"
            'export CLAUDE_SECURESTORAGE_CONFIG_DIR="$HOME/.claude"\n')
        cp = subprocess.run(["bash", str(REPO / "vm" / "shell-rc.sh"), str(REPO)],
                            env={"HOME": str(home), "PATH": os.environ["PATH"]},
                            capture_output=True, text=True, timeout=120)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        text = rcfile.read_text()
        self.assertEqual(1, text.count("CLAUDE_SECURESTORAGE_CONFIG_DIR"), text)
        self.assertNotIn('CLAUDE_SECURESTORAGE_CONFIG_DIR="$HOME/.claude"', text)


class TestWhoIsAskedWhetherAWorkspaceCanAuthenticate(_Delivery):
    """t_agent_secret_present: the question is put to the machine that will run
    the agent, not to whoever is asking. Every target reads a value row out of
    this machine's store, so the default answers from there; a guest holds the
    file row itself, so the vm driver asks the guest -- through its own login
    shell, which is the one authority on where its credential store is."""

    def _guest(self, login=None):
        """A guest home wired by the real vm/shell-rc.sh, so the probe and the
        rc agree about the directory or this fails."""
        home = self.tmp / "guest-home"
        home.mkdir(exist_ok=True)
        cp = subprocess.run(["bash", str(REPO / "vm" / "shell-rc.sh"), str(REPO)],
                            env={"HOME": str(home), "PATH": os.environ["PATH"]},
                            capture_output=True, text=True, timeout=120)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        m = re.search(r'CLAUDE_SECURESTORAGE_CONFIG_DIR="\$HOME/([^"]+)"',
                      (REPO / "vm" / "shell-rc.sh").read_text())
        store = home / m.group(1)
        store.mkdir(exist_ok=True)
        if login is not None:
            (store / FILE_ROWS[0][1]).write_text(login)
        return home

    def _ask(self, store, home, fn, secret):
        with stub_path({"ssh": FAKE_SSH, "tart": FAKE_TART}) as binp:
            env = self._env(store, home,
                            {"PATH": f"{binp}:{os.environ['PATH']}",
                             "WK_VM_STORE": str(self.tmp / "vmstore")})
            return bash(f'''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target vm >/dev/null 2>&1
if {fn} demo {secret}; then echo YES; else echo NO; fi
''', env=env)

    def test_a_guest_that_logged_in_for_itself_answers_yes(self):
        cp = self._ask(self._store(), self._guest(FAKE_LOGIN),
                       "t_agent_secret_present", FILE_ROWS[0][0])
        self.assertIn("YES", cp.stdout, cp.stdout + cp.stderr)

    def test_a_guest_that_has_not_answers_no_however_full_this_store_is(self):
        """The defect the hook exists for: this host's store is the wrong
        thing to ask about a guest, and a full store is the case that would
        have said yes."""
        store = self._store()
        for row in FILE_ROWS:
            store_path(store, row).write_text(FAKE_LOGIN)
            store_path(store, row).chmod(0o600)
        cp = self._ask(store, self._guest(), "t_agent_secret_present",
                       FILE_ROWS[0][0])
        self.assertIn("NO", cp.stdout, cp.stdout + cp.stderr)

    def test_an_empty_credential_file_is_not_a_login(self):
        """What a `claude auth login` that was interrupted leaves behind."""
        cp = self._ask(self._store(), self._guest(""),
                       "t_agent_secret_present", FILE_ROWS[0][0])
        self.assertIn("NO", cp.stdout, cp.stdout + cp.stderr)

    def test_a_value_row_is_still_this_machines_store(self):
        """A guest is handed those, so the store is where the answer is."""
        name = VALUE_ROWS[0][0]
        with_it = self._ask(self._store(values=[name]), self._guest(),
                            "t_agent_secret_present", name)
        self.assertIn("YES", with_it.stdout, with_it.stdout + with_it.stderr)
        without = self._ask(self._store(), self._guest(),
                            "t_agent_secret_present", name)
        self.assertIn("NO", without.stdout, without.stdout + without.stderr)

    def test_the_guests_remedy_is_to_log_in_in_there(self):
        cp = self._ask(self._store(), self._guest(), "t_agent_secret_remedy",
                       FILE_ROWS[0][0])
        self.assertIn("claude auth login", cp.stdout, cp.stdout + cp.stderr)
        self.assertNotIn("wk key set", cp.stdout, cp.stdout)

    def test_a_value_rows_remedy_is_this_machines_store(self):
        name = VALUE_ROWS[0][0]
        cp = self._ask(self._store(), self._guest(), "t_agent_secret_remedy", name)
        self.assertIn(f"wk key set {name}", cp.stdout, cp.stdout + cp.stderr)


class TestTheDefaultAsksThisMachinesStore(_Delivery):
    """Every target but the guest hands its workspaces what `wk key set` put
    here, so the default in lib/target.sh answers from the store -- for a file
    row too, which is the container's live mount of it."""

    def _ask(self, store, fn, secret):
        env = self._env(store, self._home())
        return bash(f'''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
if {fn} demo {secret}; then echo YES; else echo NO; fi
''', env=env)

    def test_a_login_in_the_store_is_a_yes(self):
        store = self._store()
        for row in FILE_ROWS:
            store_path(store, row).write_text(FAKE_LOGIN)
            store_path(store, row).chmod(0o600)
        cp = self._ask(store, "t_agent_secret_present", FILE_ROWS[0][0])
        self.assertIn("YES", cp.stdout, cp.stdout + cp.stderr)

    def test_an_empty_store_is_a_no(self):
        cp = self._ask(self._store(), "t_agent_secret_present", FILE_ROWS[0][0])
        self.assertIn("NO", cp.stdout, cp.stdout + cp.stderr)

    def test_the_remedy_names_the_command_that_stores_one(self):
        cp = self._ask(self._store(), "t_agent_secret_remedy", FILE_ROWS[0][0])
        self.assertIn(f"wk key set {FILE_ROWS[0][0]}", cp.stdout,
                      cp.stdout + cp.stderr)


class TestAContainerSharesOneWritableFile(unittest.TestCase):
    """Every container on this machine reads and writes one file, because the
    CLI rotates the refresh token in it: a copy each would be a copy the first
    refresh anywhere kills. So the delivery is a read-write mount of one
    directory, and the CLI is pointed at the directory rather than linked to
    the file -- it writes through a temp file and a rename, which would replace
    a symlink with a regular private copy."""

    CONTAINER = (REPO / "targets" / "container.sh").read_text()
    RC = (REPO / "shell" / "bashrc").read_text()
    MACHINE = (REPO / "host" / "macos" / "machine.sh").read_text()

    def test_the_container_gets_the_directory_read_write(self):
        self.assertIn("--volume $WK_STORE/agent-rw:/agent-rw", self.CONTAINER)
        # And the read-only one it sits beside is still read-only.
        self.assertIn("--volume $WK_STORE/secrets:/secrets:ro", self.CONTAINER)

    def test_the_shell_points_the_cli_at_it_rather_than_exporting_anything(self):
        block = self.RC[self.RC.index("# --- 6b. The agents' credentials"):
                        self.RC.index("# --- 7. Completion")]
        self.assertIn("export CLAUDE_SECURESTORAGE_CONFIG_DIR=/agent-rw", block)
        # Only where a mount put one: this rc is read on every machine in the
        # fleet, and a workstation must not acquire it.
        self.assertIn("if [ -d /agent-rw ]", block)

    def test_no_row_of_the_table_is_named_in_either(self):
        for name, text in (("targets/container.sh", self.CONTAINER),
                           ("shell/bashrc", self.RC)):
            with self.subTest(script=name):
                self.assertNotIn("claude-credentials", text)

    def test_the_machine_mounts_that_directory_and_only_that_one_writable(self):
        """The one read-write mount in the design, whose whole contents are a
        credential the workspaces are meant to rotate."""
        self.assertIn('_agent_rw_mount="$(wk_agent_rw_dir):$WK_STORE/agent-rw:rw"',
                      self.MACHINE)
        for mount in ('_secrets_mount="$(wk_secrets_dir):$WK_STORE/secrets:ro"',
                      '_tools_mount="$WK_ROOT:/var/opt/wk-tools:ro"'):
            with self.subTest(mount=mount):
                self.assertIn(mount, self.MACHINE)
        self.assertEqual(1, self.MACHINE.count(":rw\""), self.MACHINE)


if __name__ == "__main__":
    unittest.main()
