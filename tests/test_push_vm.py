"""`wk push` reaches a macOS guest.

A container reads the deploy keys live through its read-only /secrets mount, so
moving them in the store is the whole switch for one. A macOS guest mounts
nothing of ours: it holds a *copy*, written from the host on every start
(targets/vm.sh's _write_deploy_keys) and converged again whenever the switch is
thrown (cmd/push's guest half). So there are three things to hold to:

    the guest gets what the store has, on every start
    the guest loses it the moment the store has none -- push off, no key
      registered, or a store this host cannot read
    ssh in the guest can actually reach github.com, which needs a
      ProxyCommand: Softnet allows one address, the host's own, where
      wk-proxy listens

Nothing here touches a real guest, a real key or the podman machine: `tart`,
`ssh` and `podman` are stubs on PATH, the "guest" is a scratch directory the
fake ssh runs its commands against, and the "key" is a placeholder string.

Run: python3 -m unittest tests.test_push_vm -v
"""
import os
import re
import subprocess
import unittest
from pathlib import Path

from tests.support import assert_guest_start_converges, REPO, WkTest, bash, scratch_dir, stub_path

# Not a key, and deliberately nothing like one.
PLACEHOLDER = "placeholder-not-a-key-for-this-test"

TOUCHED = (
    "cmd/push", "lib/store.sh", "targets/vm.sh",
    "vm/provision-base.sh", "vm/shell-rc.sh", "container/firstrun.sh",
)

# `tart`: one running guest called wk-demo, at an address, and nothing else.
# `tart list` mixes local VMs with cached OCI images, so Source matters.
FAKE_TART = '''
case "$1" in
list) echo '[{"Name":"wk-demo","State":"running","Source":"local"}]' ;;
ip)   echo 1.2.3.4 ;;
*)    exit 1 ;;
esac
'''

# `tart` with the same guest stopped.
FAKE_TART_STOPPED = '''
case "$1" in
list) echo '[{"Name":"wk-demo","State":"stopped","Source":"local"}]' ;;
ip)   exit 1 ;;
*)    exit 1 ;;
esac
'''

# `ssh`: the guest, as a directory. targets/vm.sh's _ssh hands the remote
# command as the last argument and everything the remote end writes it writes
# under $HOME, so running that command with HOME pointed at a scratch
# directory exercises the real umask, mkdir, redirect and rm -- not a
# transcript of them. Every invocation is appended to $WK_TEST_SSH_LOG, which
# is how a test asks whether a private key was ever an argument.
FAKE_SSH = '''
for a in "$@"; do last="$a"; done
printf '%s\\n' "$*" >> "$WK_TEST_SSH_LOG"
HOME="$WK_TEST_GUEST" sh -c "$last"; rc=$?
# A real ssh forwards local stdin to the far side until EOF whether or not
# the remote command reads it, so a caller inside a `while read` loop that
# forgets </dev/null loses the rest of its list. Behave the same.
[ -t 0 ] || cat >/dev/null
exit $rc
'''

# `podman`: a machine that exists and is stopped, logging every verb so a test
# can say whether it was started.
FAKE_PODMAN = '''
printf '%s\\n' "$*" >> "$WK_TEST_PODMAN_LOG"
case "$*" in
    *"machine inspect"*"State"*) echo stopped ;;
    *"machine inspect"*)         echo '{}' ;;
    *"machine start"*)           ;;
    *"machine ssh"*)             ;;
esac
exit 0
'''


# `ssh` that cannot reach the guest: what a guest that stopped answering
# between the store half and the guest half looks like from here.
FAKE_SSH_UNREACHABLE = '''
[ -t 0 ] || cat >/dev/null
echo "ssh: connect to host 1.2.3.4 port 22: Operation timed out" >&2
exit 255
'''


def _guest(tmp, name="demo"):
    """A scratch guest home plus the host-side workspace directory and ready
    marker targets/vm.sh's t_created reads -- without which t_info says
    `creating`, not `running`."""
    home = tmp / "guest-home"
    (home / ".ssh").mkdir(parents=True, exist_ok=True)
    vmstore = tmp / "vmstore"
    ws = vmstore / "ws" / name
    ws.mkdir(parents=True, exist_ok=True)
    (ws / ".wk-ready").write_text("")
    return home, vmstore


def _store(tmp, keys=()):
    """A scratch $WK_STORE with a secrets directory holding one placeholder
    private key per named fork."""
    d = tmp / "store"
    (d / "secrets").mkdir(parents=True, exist_ok=True)
    for fork in keys:
        p = d / "secrets" / f"build_key_{fork}"
        p.write_text(f"{PLACEHOLDER}-{fork}\n")
        p.chmod(0o600)
    return d


class TestScriptsParse(unittest.TestCase):
    """`bash -n` is the cheapest thing that catches a quoting mistake in a
    heredoc, which is most of what these files are."""

    def test_bash_n(self):
        for f in TOUCHED:
            with self.subTest(script=f):
                cp = subprocess.run(["bash", "-n", str(REPO / f)],
                                    capture_output=True, text=True, timeout=60)
                self.assertEqual(cp.returncode, 0, cp.stderr)


class TestOneAliasBlock(WkTest):
    """wk_ssh_alias_blocks is the one implementation of "the ssh config the
    forks need". Three machines read it and each names the identity file its
    own way; only the identity path and the ProxyCommand may differ."""

    def _blocks(self, args):
        cp = bash(f'. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/lib/store.sh"; '
                  f'wk_ssh_alias_blocks {args}')
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stdout

    def test_a_build_machine_reads_the_store_directly(self):
        """remote/provision.sh passes a directory and nothing else, and gets
        exactly what it got before the guest needed a say in it."""
        out = self._blocks("/wk/secrets")
        self.assertIn("Host github-webkit", out)
        self.assertIn("IdentityFile /wk/secrets/build_key_fork", out)
        self.assertNotIn("ProxyCommand", out)

    def test_a_guest_names_its_own_copy_and_carries_a_proxy(self):
        out = self._blocks("'~/.ssh' id_ 'nc -X connect -x 10.0.0.1:3128 %h %p'")
        self.assertIn("IdentityFile ~/.ssh/id_fork", out)
        self.assertIn("IdentityFile ~/.ssh/id_forkwpe", out)
        self.assertIn("ProxyCommand nc -X connect -x 10.0.0.1:3128 %h %p", out)

    def test_every_fork_gets_a_block(self):
        cp = bash('. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/lib/store.sh"; '
                  'wk_push_forks | awk "NF {print \\$3}"')
        aliases = cp.stdout.split()
        self.assertTrue(aliases)
        out = self._blocks("/d")
        for a in aliases:
            self.assertIn(f"Host {a}\n", out)

    def test_the_arg_sets_differ_only_where_they_must(self):
        """Byte-identical modulo the identity path and the ProxyCommand: the
        three callers must not drift into offering different
        StrictHostKeyChecking, User or HostName."""
        def norm(text):
            out = []
            for line in text.splitlines():
                if line.strip().startswith("IdentityFile"):
                    out.append("    IdentityFile <identity>")
                elif line.strip().startswith("ProxyCommand"):
                    continue
                else:
                    out.append(line)
            return "\n".join(out)

        container = self._blocks("'~/.ssh' id_")
        guest = self._blocks("'~/.ssh' id_ 'nc %h %p'")
        store = self._blocks("/wk/secrets")
        self.assertEqual(norm(container), norm(guest))
        self.assertEqual(norm(container), norm(store))

    def test_a_container_calls_it_rather_than_writing_its_own(self):
        """container/firstrun.sh is the third caller. It held a copy of the
        blocks while another session was editing it; the copy is gone, so
        there is nothing left to hold byte-identical."""
        text = (REPO / "container" / "firstrun.sh").read_text()
        self.assertIn("wk_ssh_alias_blocks", text)
        self.assertIn("_alias_blocks > \"$_blocks\"", text)
        self.assertNotIn("HostName github.com", text,
                         "firstrun.sh writes its own alias blocks again")

    def test_a_container_that_cannot_read_the_blocks_says_so(self):
        """The read is under `2>/dev/null`, so a tooling tree it cannot read
        emits nothing -- and the id_ symlinks below are made anyway, leaving a
        workspace whose push fails against a host ssh never heard of. The
        block is captured and its emptiness reported by name."""
        text = (REPO / "container" / "firstrun.sh").read_text()
        fn = "_alias_blocks() {" + \
            text.split("_alias_blocks() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
        start = text.index('_blocks=$(mktemp)')
        end = text.index('rm -f "$_blocks"') + len('rm -f "$_blocks"')

        home = self.tmp / "fake-home"
        (home / ".ssh").mkdir(parents=True)
        (home / ".ssh" / "config").write_text("")
        cp = bash("\n".join([
            'warn() { printf "warning: %s\\n" "$*" >&2; }',
            f'HOME={home}',
            f'WK_TOOLS={self.tmp / "no-tooling-here"}',
            fn,
            text[start:end],
        ]))
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("no fork ssh aliases", cp.stderr, cp.stderr)
        self.assertEqual("", (home / ".ssh" / "config").read_text())

    def test_the_container_call_produces_the_blocks(self):
        """Its `_alias_blocks` lifted out and run against this tree: the
        container reaches lib/store.sh through the mounted tooling
        ($WK_TOOLS), which is the part a source-level check cannot see."""
        text = (REPO / "container" / "firstrun.sh").read_text()
        block = "_alias_blocks() {" + \
            text.split("_alias_blocks() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
        cp = bash(f'WK_TOOLS="$WK_ROOT"\n{block}\n_alias_blocks')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("Host github-webkit", cp.stdout)
        self.assertIn("IdentityFile ~/.ssh/id_fork", cp.stdout)
        # A container's catch-all `Host *` block carries the ProxyCommand, and
        # ssh takes the first value it sees: a second one here would win.
        self.assertNotIn("ProxyCommand", cp.stdout)


class TestAGuestGetsTheKeysOnStart(WkTest):
    """The real _write_deploy_keys, against a fake guest: what it writes is
    what a guest would end up holding."""

    def _write(self, store, home, vmstore, extra=None):
        log = self.tmp / "ssh.log"
        log.write_text("")
        with stub_path({"ssh": FAKE_SSH, "tart": FAKE_TART}) as binp:
            env = {
                "PATH": f"{binp}:{os.environ['PATH']}",
                "WK_TEST_GUEST": str(home),
                "WK_TEST_SSH_LOG": str(log),
                "WK_STORE": str(store),
                "WK_VM_STORE": str(vmstore),
                "WK_VM_PROXY_ADDR": "192.168.2.1",
            }
            if extra:
                env.update(extra)
            cp = bash(f'''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target vm >/dev/null 2>&1
_write_deploy_keys demo 1.2.3.4
''', env=env)
        self.log = log.read_text()
        return cp

    def test_the_key_in_the_store_lands_in_the_guest(self):
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp, keys=("fork",))
        cp = self._write(store, home, vmstore)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual((home / ".ssh" / "id_fork").read_text(),
                         f"{PLACEHOLDER}-fork\n")

    def test_a_fork_with_no_key_leaves_none_behind(self):
        """Only `fork` is registered here, so the guest must not end up with
        an id_forkwpe from anywhere."""
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp, keys=("fork",))
        self._write(store, home, vmstore)
        self.assertFalse((home / ".ssh" / "id_forkwpe").exists())

    def test_the_key_is_unreadable_to_anyone_else(self):
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp, keys=("fork",))
        self._write(store, home, vmstore)
        mode = (home / ".ssh" / "id_fork").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, oct(mode))

    def test_a_key_file_already_there_is_tightened_too(self):
        """`umask` decides the mode of a file the write *creates*; one already
        at 0644 keeps it, and ssh then refuses the key it was handed."""
        home, vmstore = _guest(self.tmp)
        stale = home / ".ssh" / "id_fork"
        stale.write_text("older, and world-readable\n")
        stale.chmod(0o644)
        self._write(_store(self.tmp, keys=("fork",)), home, vmstore)
        mode = stale.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, oct(mode))

    def test_a_store_with_no_keys_withdraws_what_the_guest_holds(self):
        """The one state this must not leave behind: a guest holding a key the
        host has taken away. `wk push off` is exactly this case."""
        home, vmstore = _guest(self.tmp)
        (home / ".ssh" / "id_fork").write_text("stale key\n")
        (home / ".ssh" / "id_forkwpe").write_text("stale key\n")
        cp = self._write(_store(self.tmp), home, vmstore)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertFalse((home / ".ssh" / "id_fork").exists())
        self.assertFalse((home / ".ssh" / "id_forkwpe").exists())

    def test_the_config_names_the_aliases_and_a_route_to_github(self):
        """Both forks are on github.com, so the key is selected by alias; and
        Softnet allows one address, so port 22 is reached by CONNECT through
        the proxy at that address or not at all."""
        home, vmstore = _guest(self.tmp)
        cfg = self._write(_store(self.tmp, keys=("fork",)), home, vmstore)
        self.assertEqual(cfg.returncode, 0, cfg.stdout + cfg.stderr)
        text = (home / ".ssh" / "config").read_text()
        self.assertIn("Host github-webkit", text)
        self.assertIn("Host github-wpe", text)
        self.assertIn("IdentityFile ~/.ssh/id_fork", text)
        self.assertIn("-X connect -x 192.168.2.1:3128 %h %p", text)

    def test_the_config_is_written_even_with_no_key_behind_it(self):
        """A dangling IdentityFile is the off position and says so as `no such
        identity` -- the same thing a container's dangling symlink does. So the
        two halves never have to agree about anything."""
        home, vmstore = _guest(self.tmp)
        self._write(_store(self.tmp), home, vmstore)
        self.assertIn("Host github-webkit", (home / ".ssh" / "config").read_text())

    def test_it_is_idempotent(self):
        """It runs on every start; a guest started fifty times holds one key
        and one config, not fifty appended blocks."""
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp, keys=("fork",))
        for _ in range(3):
            self._write(store, home, vmstore)
        text = (home / ".ssh" / "config").read_text()
        self.assertEqual(1, text.count("Host github-webkit"), text)
        self.assertEqual((home / ".ssh" / "id_fork").read_text(),
                         f"{PLACEHOLDER}-fork\n")

    def test_the_key_is_never_an_argument(self):
        """An argument is in `ps` for everyone on the machine, and the ssh
        command line is also what a `--debug` run prints. The bytes go over
        stdin; the log of every ssh invocation must not contain them."""
        home, vmstore = _guest(self.tmp)
        self._write(_store(self.tmp, keys=("fork", "forkwpe")), home, vmstore)
        self.assertNotIn(PLACEHOLDER, self.log, self.log)

    def test_it_is_wired_into_both_start_paths(self):
        """t_start has two arms -- a guest that is already running is
        converged, one that is not is booted first -- and a key delivered on
        only one of them is a switch that half works. Both arms run one
        `_converge_guest`, which writes the keys once."""
        assert_guest_start_converges(self, '_write_deploy_keys "$name" "$ip"')

    def test_the_source_streams_it_rather_than_quoting_it(self):
        """Source-level twin of the test above, so the property survives a
        rewrite that the fake ssh happens not to exercise."""
        vm = (REPO / "targets" / "vm.sh").read_text()
        self.assertIn('| _ssh "$ip" "umask 077 && cat > $idf && chmod 600 $idf"', vm)
        self.assertNotIn('sh_quote "$key"', vm)


class TestTheSwitchReachesTheGuests(WkTest):
    """`wk push on|off` end to end on this host, with the store scratch and
    local (so nothing forwards) and one fake running guest."""

    def _push(self, action, store, home, vmstore, tart=FAKE_TART, ssh=FAKE_SSH):
        log = self.tmp / "ssh.log"
        log.write_text("")
        with stub_path({"ssh": ssh, "tart": tart}) as binp:
            env = {
                "PATH": f"{binp}:{os.environ['PATH']}",
                "WK_TEST_GUEST": str(home),
                "WK_TEST_SSH_LOG": str(log),
                "WK_STORE": str(store),
                "WK_VM_STORE": str(vmstore),
                "WK_VM_PROXY_ADDR": "192.168.2.1",
            }
            cp = self.run_wk("push", action, env=env)
        self.log = log.read_text()
        return cp

    @unittest.skipUnless(os.uname().sysname == "Darwin",
                         "guests are a macOS-host thing (tart)")
    def test_off_takes_the_key_out_of_a_running_guest(self):
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp, keys=("fork",))
        (home / ".ssh" / "id_fork").write_text("stale key\n")
        cp = self._push("off", store, home, vmstore)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertFalse((home / ".ssh" / "id_fork").exists(), cp.stdout)
        self.assertIn("demo", cp.stdout)
        self.assertTrue((store / "push-keys" / "build_key_fork").exists())

    @unittest.skipUnless(os.uname().sysname == "Darwin",
                         "guests are a macOS-host thing (tart)")
    def test_on_puts_it_back_without_a_restart(self):
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp)
        (store / "push-keys").mkdir()
        (store / "push-keys" / "build_key_fork").write_text(f"{PLACEHOLDER}-fork\n")
        cp = self._push("on", store, home, vmstore)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual((home / ".ssh" / "id_fork").read_text(),
                         f"{PLACEHOLDER}-fork\n", cp.stdout)

    @unittest.skipUnless(os.uname().sysname == "Darwin",
                         "guests are a macOS-host thing (tart)")
    def test_status_reads_the_guest_and_writes_nothing(self):
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp, keys=("fork",))
        (home / ".ssh" / "id_fork").write_text(f"{PLACEHOLDER}-fork\n")
        before = sorted((p.name, p.read_text()) for p in (home / ".ssh").iterdir())
        cp = self._push("status", store, home, vmstore)
        self.assertIn("guest demo", cp.stdout)
        self.assertIn("holds fork", cp.stdout)
        after = sorted((p.name, p.read_text()) for p in (home / ".ssh").iterdir())
        self.assertEqual(before, after, cp.stdout)

    @unittest.skipUnless(os.uname().sysname == "Darwin",
                         "guests are a macOS-host thing (tart)")
    def test_a_stopped_guest_is_reported_not_started(self):
        """Booting a guest to look inside it is a side effect nobody asked
        `status` for, and rule 6 says a reporting command starts nothing."""
        home, vmstore = _guest(self.tmp)
        cp = self._push("status", _store(self.tmp, keys=("fork",)), home, vmstore,
                        tart=FAKE_TART_STOPPED)
        self.assertIn("guest demo", cp.stdout)
        self.assertIn("stopped", cp.stdout)
        self.assertEqual("", self.log, self.log)

    @unittest.skipUnless(os.uname().sysname == "Darwin",
                         "guests are a macOS-host thing (tart)")
    def test_a_guest_that_did_not_answer_fails_the_switch_and_is_named(self):
        """The measured hole: `converge_guests` swallowed its failure into a
        warning and `exit "$STORE_RC"` reported only the store half, so
        `wk push off` against an unreachable guest moved the keys in the store,
        left the guest's copy alone and exited 0 -- which `cmd/ai`'s
        `push_switch status || return 0` reads as a closed switch."""
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp, keys=("fork",))
        (home / ".ssh" / "id_fork").write_text("stale key\n")
        cp = self._push("off", store, home, vmstore, ssh=FAKE_SSH_UNREACHABLE)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("demo", cp.stdout)
        self.assertIn("may still hold a deploy key", cp.stdout)
        # The store half did its part; the guest's copy is what is left over.
        self.assertTrue((store / "push-keys" / "build_key_fork").exists())
        self.assertTrue((home / ".ssh" / "id_fork").exists())

    @unittest.skipUnless(os.uname().sysname == "Darwin",
                         "guests are a macOS-host thing (tart)")
    def test_status_is_on_while_any_running_guest_holds_a_key(self):
        """The switch is on when *anything* can push. A store with its keys
        held back while a running guest still has its copy is `on`, and must
        exit 0 -- the store's half is not the whole of it."""
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp)
        (store / "push-keys").mkdir()
        (store / "push-keys" / "build_key_fork").write_text(f"{PLACEHOLDER}-fork\n")
        (home / ".ssh" / "id_fork").write_text(f"{PLACEHOLDER}-fork\n")

        cp = self._push("status", store, home, vmstore)
        self.assertIn("holds fork", cp.stdout)
        self.assertIn("push is ON", cp.stdout)
        self.assertEqual(cp.returncode, 0, cp.stdout)

    @unittest.skipUnless(os.uname().sysname == "Darwin",
                         "guests are a macOS-host thing (tart)")
    def test_status_is_off_when_no_guest_holds_one_either(self):
        """The other side of the same rule: nothing holds a key, so the exit
        status stays the store's `off`."""
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp)
        (store / "push-keys").mkdir()
        (store / "push-keys" / "build_key_fork").write_text(f"{PLACEHOLDER}-fork\n")
        cp = self._push("status", store, home, vmstore)
        self.assertIn("push is OFF", cp.stdout)
        self.assertEqual(cp.returncode, 1, cp.stdout)

    @unittest.skipUnless(os.uname().sysname == "Darwin",
                         "guests are a macOS-host thing (tart)")
    def test_a_guests_own_private_key_is_not_read_as_a_fork_key(self):
        """`ls id_*` counts any private key in there. An account's own
        id_ed25519 is not a deploy key, and reporting it as one would say a
        push is possible that wk cannot make."""
        home, vmstore = _guest(self.tmp)
        (home / ".ssh" / "id_ed25519").write_text("somebody's own key\n")
        cp = self._push("status", _store(self.tmp), home, vmstore)
        self.assertIn("guest demo", cp.stdout)
        self.assertIn("no key", cp.stdout)
        self.assertNotIn("ed25519", cp.stdout)

    def test_status_stays_declared_readonly_and_the_guest_half_reads(self):
        src = (REPO / "cmd" / "push").read_text()
        self.assertIn("# wk: readonly status", src)
        # The guest half of `status` is one `ls`, in the driver, and nothing else.
        state = re.search(r"vm_push_keys_state\(\) \{.*?\n\}",
                          (REPO / "targets" / "vm.sh").read_text(), re.S).group(0)
        for writer in ("cat >", "rm -f", "umask", "tart run", "t_start"):
            self.assertNotIn(writer, state, f"{writer!r} in vm_push_keys_state")


class TestTheStoreHalfAndTheGuestHalf(WkTest):
    """Which machine each half of the switch runs on, and what it says about
    the hop it makes."""

    def test_the_actions_run_here_and_the_store_half_is_the_hop(self):
        src = (REPO / "cmd" / "push").read_text()
        self.assertIn("# wk: sub on,off,status where=local", src)
        self.assertIn("# wk: flag --store where=store", src)
        self.assertIn('"$WK_ROOT/wk" push "$ACTION" --store', src)

    def test_the_store_half_leaves_the_guests_to_the_caller(self):
        """`--store` runs on the machine holding the keys -- the podman
        machine, where there are no guests at all."""
        src = (REPO / "cmd" / "push").read_text()
        body = src[src.index("STORE_RC=0"):]
        self.assertLess(body.index('[ -z "$STORE_ONLY" ] || exit "$STORE_RC"'),
                        body.index("converge_guests"))

    # The refusal exists because the store's other half is a podman machine:
    # where the store is local -- every Linux host -- `--store` is readable
    # here and running it is correct, so there is nothing to refuse.
    @unittest.skipUnless(os.uname().sysname == "Darwin",
                         "the store half is a hop only where the store lives in the podman VM")
    def test_store_only_refuses_rather_than_hopping_again(self):
        """The dispatcher forwards `--store` to the machine that holds the
        keys. If it ever did not, hopping again would be an endless loop, so
        it refuses and names the command that does the hop.

        cmd/push is run directly rather than through `wk`, because going
        through `wk` is the very forward this is the last line of defence
        behind -- and it would reach the real podman machine to make it."""
        with scratch_dir() as d:
            cp = subprocess.run(
                [str(REPO / "cmd" / "push"), "off", "--store"],
                cwd=str(REPO), env={**os.environ, "WK_STORE": str(d / "nothing-here")},
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=60)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("cannot be read from here", cp.stdout)

    def test_a_workspace_is_still_refused(self):
        """where=local means the dispatcher no longer refuses this one, so
        cmd/push's own refusal is the whole of the rule -- which is where it
        belongs: the keys are on the host either way."""
        marker = self.tmp / "wk-workspace"
        marker.write_text("name=probe\ntarget=container\n")
        for action in ("on", "off", "status"):
            with self.subTest(action=action):
                cp = self.run_wk("push", action, env={"WK_MARKER": str(marker)})
                self.assertNotEqual(cp.returncode, 0, cp.stdout)
                self.assertIn("workspace", cp.stdout)


@unittest.skipUnless(os.uname().sysname == "Darwin",
                     "the store is only somewhere else on a macOS host")
class TestThePodmanStartIsExplicit(WkTest):
    """The measured complaint: `wk push on` starts the podman machine even
    when the work is about a tart guest. It has to -- the key bytes exist only
    in that store -- so it says so, and `status` still starts nothing."""

    def _push(self, *args):
        plog = self.tmp / "podman.log"
        plog.write_text("")
        home, vmstore = _guest(self.tmp)
        with stub_path({"podman": FAKE_PODMAN, "ssh": FAKE_SSH,
                        "tart": FAKE_TART_STOPPED}) as binp:
            cp = self.run_wk("push", *args, env={
                "PATH": f"{binp}:{os.environ['PATH']}",
                # The real store path, which this host cannot read: what makes
                # the hop happen at all.
                "WK_STORE": "/var/lib/wk",
                "WK_VM_STORE": str(vmstore),
                "WK_TEST_GUEST": str(home),
                "WK_TEST_SSH_LOG": str(self.tmp / "ssh.log"),
                "WK_TEST_PODMAN_LOG": str(plog),
            })
        return cp, plog.read_text()

    def test_on_starts_it_and_says_why(self):
        cp, plog = self._push("on")
        self.assertIn("machine start", plog, plog)
        self.assertIn("the deploy keys live in the podman machine's store", cp.stdout)

    def test_status_does_not_start_it(self):
        cp, plog = self._push("status")
        self.assertNotIn("machine start", plog, plog)
        self.assertIn("stopped", cp.stdout)


if __name__ == "__main__":
    unittest.main()
