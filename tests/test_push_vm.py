"""`wk push` reaches a macOS guest.

A container reads the ssh-agent's socket through its /run/wk mount, so the
machine half is the whole switch for one. A guest mounts nothing of ours and
cannot see a unix socket across the hypervisor, so its half is an ssh-agent on
*this host* and one `ssh -N -R` per running guest (targets/vm.sh). No private
key byte is ever written into a guest. Four things to hold to:

    the guest gets the ssh config and the *public* halves on every start, and
      never a private one
    a guest reaches the agent exactly while push is on, and the forward is a
      process this host holds and can be seen to hold
    ssh in the guest can actually reach github.com, which needs a
      ProxyCommand: Softnet allows one address, the host's own, where
      wk-proxy listens
    the guest's trust for the API injector's CA goes in with its egress, and
      what it holds as a token is the placeholder

Nothing here touches a real guest or the podman machine: `tart`, `ssh` and
`podman` are stubs on PATH and the "guest" is a scratch directory the fake ssh
runs its commands against. The keys and the ssh-agent are real -- `ssh-add`
will not load a placeholder, and an agent is the thing under test.

Run: python3 -m unittest tests.test_push_vm -v
"""
import os
import re
import shutil
import signal
import subprocess
import unittest

from tests.support import (assert_guest_start_converges, func_body, REPO,
                           WkTest, bash, stub_path)

TOUCHED = (
    "cmd/push", "lib/store.sh", "targets/vm.sh",
    "vm/provision-base.sh", "vm/shell-rc.sh", "container/firstrun.sh",
    "container/proxy/ensure-bridge.sh",
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
# transcript of them. /Users/admin is rewritten to that directory for the same
# reason: the agent socket's path has to be absolute on the guest, because that
# is what `ssh -R` binds.
#
# `-N` is the agent forward, which has no remote command at all and is meant to
# stay up: it sleeps, so this host's own liveness check sees a live process.
#
# Every invocation is appended to $WK_TEST_SSH_LOG, which is how a test asks
# whether a private key was ever an argument.
FAKE_SSH = '''
for a in "$@"; do last="$a"; done
printf '%s\\n' "$*" >> "$WK_TEST_SSH_LOG"
case " $* " in
    *" -N "*)
        # What sshd does at the far end of `-R <remote>:<local>`: it binds the
        # remote socket. `test -S` on it is how this host reports that a guest
        # reaches the agent, so a stub that only slept would report a switch
        # that is not thrown.
        fwd=""
        for a in "$@"; do
            case "$a" in
                *.wk-ssh-agent.sock:*) fwd=$(printf '%s' "${a%%:*}" | sed "s|/Users/admin|$WK_TEST_GUEST|") ;;
            esac
        done
        [ -n "$fwd" ] || exec sleep 30
        exec python3 -c 'import socket,sys,time
s = socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.listen(1); time.sleep(30)' "$fwd"
        ;;
esac
cmd=$(printf '%s' "$last" | sed "s|/Users/admin|$WK_TEST_GUEST|g")
HOME="$WK_TEST_GUEST" sh -c "$cmd"; rc=$?
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
esac
exit 0
'''

# `ssh` that cannot reach the guest: what a guest that stopped answering
# between the two halves looks like from here.
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
    """A scratch store, and beside its secrets directory the one that nothing
    mounts. Real keys, because `ssh-add` will not take anything else: private
    halves where they live for good, public halves where a workspace reads
    them."""
    d = tmp / "store"
    secrets = d / "secrets"
    held = d / "push-keys"
    secrets.mkdir(parents=True, exist_ok=True)
    held.mkdir(parents=True, exist_ok=True)
    for fork in keys:
        priv = held / f"build_key_{fork}"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                        "-C", f"wk deploy key for {fork}", "-f", str(priv)],
                       check=True)
        shutil.move(str(priv) + ".pub", str(secrets / f"build_key_{fork}.pub"))
    return d


def _kill_pidfile(path):
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


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
    forks need". Three machines read it: two name a public half and an agent
    socket, and the third -- a shared build box, a plain checkout with no
    container and nothing to keep a key away from -- names the private half
    and gets no IdentityAgent line."""

    def _blocks(self, args):
        cp = bash(f'. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/lib/store.sh"; '
                  f'wk_ssh_alias_blocks {args}')
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stdout

    def test_a_build_machine_names_the_private_half_and_no_agent(self):
        out = self._blocks("/wk/secrets")
        self.assertIn("Host github-webkit", out)
        self.assertIn("IdentityFile /wk/secrets/build_key_fork", out)
        self.assertNotIn("IdentityAgent", out)
        self.assertNotIn("ProxyCommand", out)

    def test_a_container_names_a_public_half_and_the_mounted_socket(self):
        out = self._blocks("/secrets build_key_ .pub /run/wk/ssh-agent.sock")
        self.assertIn("IdentityFile /secrets/build_key_fork.pub", out)
        self.assertIn("IdentityAgent /run/wk/ssh-agent.sock", out)
        self.assertIn("IdentitiesOnly yes", out)
        self.assertNotIn("ProxyCommand", out)

    def test_a_guest_names_its_own_public_copy_and_carries_a_proxy(self):
        out = self._blocks("'/Users/admin/.ssh' id_ .pub "
                           "'/Users/admin/.wk-ssh-agent.sock' "
                           "'nc -X connect -x 10.0.0.1:3128 %h %p'")
        self.assertIn("IdentityFile /Users/admin/.ssh/id_fork.pub", out)
        self.assertIn("IdentityAgent /Users/admin/.wk-ssh-agent.sock", out)
        self.assertIn("ProxyCommand nc -X connect -x 10.0.0.1:3128 %h %p", out)

    def test_no_caller_ever_names_a_private_half_beside_an_agent(self):
        """IdentitiesOnly with a private IdentityFile would let ssh sign with
        the file rather than the agent, which is the whole thing this avoids."""
        out = self._blocks("/secrets build_key_ .pub /run/wk/ssh-agent.sock")
        for line in out.splitlines():
            if line.strip().startswith("IdentityFile"):
                self.assertTrue(line.strip().endswith(".pub"), line)

    def test_every_fork_gets_a_block(self):
        cp = bash('. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/lib/store.sh"; '
                  'wk_push_forks | awk "NF {print \\$3}"')
        aliases = cp.stdout.split()
        self.assertTrue(aliases)
        out = self._blocks("/d")
        for a in aliases:
            self.assertIn(f"Host {a}\n", out)

    def test_the_arg_sets_differ_only_where_they_must(self):
        """Byte-identical modulo the identity path, the agent and the
        ProxyCommand: the callers must not drift into offering different
        StrictHostKeyChecking, User or HostName."""
        def norm(text):
            out = []
            for line in text.splitlines():
                s = line.strip()
                if s.startswith("IdentityFile"):
                    out.append("    IdentityFile <identity>")
                elif s.startswith(("ProxyCommand", "IdentityAgent")):
                    continue
                else:
                    out.append(line)
            return "\n".join(out)

        container = self._blocks("/secrets build_key_ .pub /run/wk/ssh-agent.sock")
        guest = self._blocks("'~/.ssh' id_ .pub /a/sock 'nc %h %p'")
        store = self._blocks("/wk/secrets")
        self.assertEqual(norm(container), norm(guest))
        self.assertEqual(norm(container), norm(store))

    def test_a_container_includes_the_file_rather_than_writing_the_blocks(self):
        """firstrun.sh runs once per workspace, and the switch is thrown many
        times after that: an Include of a file `wk push on|off` regenerates
        reaches every workspace that already exists at once."""
        text = (REPO / "container" / "firstrun.sh").read_text()
        self.assertIn("Include /secrets/ssh_config", text)
        self.assertNotIn("HostName github.com", text,
                         "firstrun.sh writes its own alias blocks again")
        self.assertNotIn("/secrets/build_key_", text,
                         "firstrun.sh links a key into the workspace again")

    def test_the_switch_writes_that_file_from_the_same_function(self):
        src = (REPO / "lib" / "store.sh").read_text()
        body = src[src.index("push_agent_publish_config() {"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("wk_ssh_alias_blocks", body)


class TestAGuestGetsTheConfigOnStart(WkTest):
    """The real _write_deploy_keys, against a fake guest: what it writes is
    what a guest would end up holding -- and what it must never write."""

    def _write(self, store, home, vmstore, extra=None):
        log = self.tmp / "ssh.log"
        log.write_text("")
        with stub_path({"ssh": FAKE_SSH, "tart": FAKE_TART}) as binp:
            env = {
                "PATH": f"{binp}:{os.environ['PATH']}",
                "WK_TEST_GUEST": str(home),
                "WK_TEST_SSH_LOG": str(log),
                "WK_STORE": str(store),
                # The keys are this device's own directory, which on macOS is
                # not under $WK_STORE; a test must not read the real one.
                "WK_HOST_SECRETS": str(store / "secrets"),
                "WK_VM_STORE": str(vmstore),
                "WK_VM_PROXY_ADDR": "192.168.2.1",
            }
            if extra:
                env.update(extra)
            cp = bash('''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target vm >/dev/null 2>&1
_write_deploy_keys demo 1.2.3.4
''', env=env)
        self.log = log.read_text()
        return cp

    def test_the_public_half_lands_in_the_guest_and_the_private_one_never_does(self):
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp, keys=("fork",))
        cp = self._write(store, home, vmstore)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("ssh-ed25519 ", (home / ".ssh" / "id_fork.pub").read_text())
        self.assertFalse((home / ".ssh" / "id_fork").exists(),
                         "a private key half was written into the guest")

    def test_no_private_key_byte_reaches_the_guest_at_all(self):
        """The property the whole arrangement rests on, measured against
        everything the fake guest ends up holding."""
        home, vmstore = _guest(self.tmp)
        self._write(_store(self.tmp, keys=("fork", "forkwpe")), home, vmstore)
        for path in home.rglob("*"):
            if path.is_file():
                self.assertNotIn("PRIVATE KEY", path.read_text(errors="replace"),
                                 f"{path} holds key material")
        self.assertNotIn("PRIVATE KEY", self.log, self.log)

    def test_a_fork_with_no_key_leaves_none_behind(self):
        home, vmstore = _guest(self.tmp)
        self._write(_store(self.tmp, keys=("fork",)), home, vmstore)
        self.assertFalse((home / ".ssh" / "id_forkwpe.pub").exists())

    def test_a_public_half_withdrawn_here_is_withdrawn_there(self):
        """A guest naming an identity this host no longer has offers a dead
        key, which reads as a GitHub permission problem rather than as a
        missing key."""
        home, vmstore = _guest(self.tmp)
        (home / ".ssh" / "id_fork.pub").write_text("stale\n")
        (home / ".ssh" / "id_forkwpe.pub").write_text("stale\n")
        cp = self._write(_store(self.tmp), home, vmstore)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertFalse((home / ".ssh" / "id_fork.pub").exists())
        self.assertFalse((home / ".ssh" / "id_forkwpe.pub").exists())

    def test_the_config_names_the_aliases_the_agent_and_a_route_to_github(self):
        """Both forks are on github.com, so the key is selected by alias;
        Softnet allows one address, so port 22 is reached by CONNECT through
        the proxy at that address or not at all; and the signature is the
        agent's, reached through the forwarded socket."""
        home, vmstore = _guest(self.tmp)
        cfg = self._write(_store(self.tmp, keys=("fork",)), home, vmstore)
        self.assertEqual(cfg.returncode, 0, cfg.stdout + cfg.stderr)
        text = (home / ".ssh" / "config").read_text()
        self.assertIn("Host github-webkit", text)
        self.assertIn("Host github-wpe", text)
        self.assertIn("IdentityFile /Users/admin/.ssh/id_fork.pub", text)
        self.assertIn("IdentityAgent /Users/admin/.wk-ssh-agent.sock", text)
        self.assertIn("-X connect -x 192.168.2.1:3128 %h %p", text)

    def test_the_config_is_written_even_with_nothing_behind_it(self):
        """An IdentityAgent pointing at a socket that is not there is the off
        position and says so as `Permission denied (publickey)` -- the same
        thing a container's empty agent does. So the two halves never have to
        agree about anything."""
        home, vmstore = _guest(self.tmp)
        self._write(_store(self.tmp), home, vmstore)
        self.assertIn("Host github-webkit", (home / ".ssh" / "config").read_text())

    def test_it_is_idempotent(self):
        """It runs on every start; a guest started fifty times holds one
        config, not fifty appended blocks."""
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp, keys=("fork",))
        for _ in range(3):
            self._write(store, home, vmstore)
        text = (home / ".ssh" / "config").read_text()
        self.assertEqual(1, text.count("Host github-webkit"), text)

    def test_it_is_wired_into_both_start_paths(self):
        """t_start has two arms -- a guest that is already running is
        converged, one that is not is booted first -- and a step delivered on
        only one of them is a switch that half works."""
        assert_guest_start_converges(self, '_write_deploy_keys "$name" "$ip"')

    def test_the_forward_is_converged_from_both_start_paths_too(self):
        assert_guest_start_converges(self, '_agent_converge_guest "$name" "$ip"')

    def test_the_source_writes_no_private_half(self):
        """Source-level twin of the tests above, so the property survives a
        rewrite the fake ssh happens not to exercise."""
        vm = (REPO / "targets" / "vm.sh").read_text()
        self.assertNotIn("wk_push_key", vm)
        self.assertNotIn('chmod 600 $idf', vm)


class TestTheGuestHalfOfTheSwitch(WkTest):
    """`wk push on|off|status` end to end on this host, with a scratch store,
    one fake running guest and a real ssh-agent started by the code under
    test."""

    def _push(self, action, store, home, vmstore, tart=FAKE_TART, ssh=FAKE_SSH):
        log = self.tmp / "ssh.log"
        log.write_text("")
        self.addCleanup(_kill_pidfile, vmstore / "vm" / "ssh-agent.pid")
        for name in ("demo",):
            self.addCleanup(_kill_pidfile, vmstore / "vm" / f"{name}.agent-forward")
        with stub_path({"ssh": ssh, "tart": tart}) as binp:
            env = {
                "PATH": f"{binp}:{os.environ['PATH']}",
                "WK_TEST_GUEST": str(home),
                "WK_TEST_SSH_LOG": str(log),
                "WK_STORE": str(store),
                "WK_HOST_SECRETS": str(store / "secrets"),
                "WK_VM_STORE": str(vmstore),
                "WK_VM_PROXY_ADDR": "192.168.2.1",
                # The machine half is not what this file is about: point it at
                # an agent that is not there, so it fails loudly rather than
                # reaching this developer's own.
                "WK_PUSH_AGENT_SOCK": str(self.tmp / "no-machine-agent.sock"),
                "WK_PUSH_PAT_FILE": str(self.tmp / "machine-pat"),
                "WK_MACHINE": "wk-no-such-machine",
            }
            cp = self.run_wk("push", action, env=env)
        self.log = log.read_text()
        return cp

    def _forward_status(self, vmstore, name="demo"):
        return vmstore / "vm" / f"{name}.agent-forward"

    @unittest.skipUnless(os.uname().sysname == "Darwin",
                         "guests are a macOS-host thing (tart)")
    def test_on_loads_the_hosts_agent_and_forwards_it_into_the_guest(self):
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp, keys=("fork",))
        cp = self._push("on", store, home, vmstore)
        self.assertIn("demo", cp.stdout)
        self.assertIn("reaches the agent on this host", cp.stdout)

        sock = vmstore / "vm" / "ssh-agent.sock"
        listed = subprocess.run(["ssh-add", "-l"], text=True,
                                env={**os.environ, "SSH_AUTH_SOCK": str(sock)},
                                stdout=subprocess.PIPE).stdout
        self.assertIn("SHA256:", listed)
        self.assertTrue(self._forward_status(vmstore).exists(), cp.stdout)
        self.assertRegex(self.log, r"-N .*-R /Users/admin/\.wk-ssh-agent\.sock:")

    @unittest.skipUnless(os.uname().sysname == "Darwin",
                         "guests are a macOS-host thing (tart)")
    def test_no_key_byte_is_ever_on_an_ssh_command_line(self):
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp, keys=("fork",))
        self._push("on", store, home, vmstore)
        self.assertNotIn("PRIVATE KEY", self.log, self.log)
        priv = (store / "push-keys" / "build_key_fork").read_text()
        for line in priv.splitlines():
            if "PRIVATE KEY" not in line and line.strip():
                self.assertNotIn(line, self.log)

    @unittest.skipUnless(os.uname().sysname == "Darwin",
                         "guests are a macOS-host thing (tart)")
    def test_off_empties_the_agent_and_ends_the_forward(self):
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp, keys=("fork",))
        self._push("on", store, home, vmstore)
        cp = self._push("off", store, home, vmstore)
        self.assertIn("a push in there is refused", cp.stdout)
        sock = vmstore / "vm" / "ssh-agent.sock"
        listed = subprocess.run(["ssh-add", "-l"], text=True,
                                env={**os.environ, "SSH_AUTH_SOCK": str(sock)},
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT).stdout
        self.assertIn("no identities", listed)
        self.assertFalse(self._forward_status(vmstore).exists(), cp.stdout)

    @unittest.skipUnless(os.uname().sysname == "Darwin",
                         "guests are a macOS-host thing (tart)")
    def test_the_private_halves_never_move(self):
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp, keys=("fork",))
        self._push("on", store, home, vmstore)
        self._push("off", store, home, vmstore)
        self.assertTrue((store / "push-keys" / "build_key_fork").exists())
        self.assertFalse((store / "secrets" / "build_key_fork").exists())

    @unittest.skipUnless(os.uname().sysname == "Darwin",
                         "guests are a macOS-host thing (tart)")
    def test_status_reads_both_ends_and_writes_nothing(self):
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp, keys=("fork",))
        self._push("on", store, home, vmstore)
        before = sorted((p.name, p.read_text()) for p in (home / ".ssh").iterdir())
        cp = self._push("status", store, home, vmstore)
        self.assertIn("guest demo", cp.stdout)
        self.assertIn("through the agent on this host", cp.stdout)
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
        warning and the command reported only the machine half, so `wk push
        off` against an unreachable guest exited 0 -- which cmd/ai's
        `push_switch status || return 0` reads as a closed switch."""
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp, keys=("fork",))
        cp = self._push("off", store, home, vmstore, ssh=FAKE_SSH_UNREACHABLE)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("demo", cp.stdout)
        self.assertIn("may still reach the agent", cp.stdout)

    @unittest.skipUnless(os.uname().sysname == "Darwin",
                         "guests are a macOS-host thing (tart)")
    def test_status_is_on_while_any_running_guest_reaches_the_agent(self):
        """The switch is on when *anything* can push. The machine's own agent
        is unreachable in these tests, so a running guest that reaches the
        host's is the whole of `on` -- and it must exit 0."""
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp, keys=("fork",))
        self._push("on", store, home, vmstore)
        cp = self._push("status", store, home, vmstore)
        self.assertIn("push is ON", cp.stdout)
        self.assertEqual(cp.returncode, 0, cp.stdout)

    @unittest.skipUnless(os.uname().sysname == "Darwin",
                         "guests are a macOS-host thing (tart)")
    def test_status_is_off_when_no_guest_reaches_it_either(self):
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp, keys=("fork",))
        cp = self._push("status", store, home, vmstore)
        self.assertIn("push is OFF", cp.stdout)
        self.assertEqual(cp.returncode, 1, cp.stdout)

    def test_status_stays_declared_readonly_and_the_guest_half_reads(self):
        src = (REPO / "cmd" / "push").read_text()
        self.assertIn("# wk: readonly status", src)
        state = re.search(r"vm_push_keys_state\(\) \{.*?\n\}",
                          (REPO / "targets" / "vm.sh").read_text(), re.S).group(0)
        for writer in ("cat >", "rm -f", "umask", "tart run", "t_start",
                       "push_agent_load", "detach_run"):
            self.assertNotIn(writer, state, f"{writer!r} in vm_push_keys_state")


class TestTwoStartsOfOneForwardDoNotFightOverIt(WkTest):
    """`wk push on`, `wk vm start` and a second `wk push on` all reach
    _agent_forward_start, and two of them interleaved used to do real damage:
    both passed the liveness check, the second removed the socket the first had
    bound (leaving the first's `ssh -R` with nothing behind it), and the
    second's failure path then removed the *first's* status file -- after which
    `wk push off` had no pid to kill and the guest kept reaching the agent.

    The lock is the guest's own (hold_lock, lib/common.sh)."""

    # A forward that binds and stays up, so the first start is live while the
    # second runs. Every invocation is logged, which is how "the second was a
    # no-op" is measured.
    FAKE_SSH_FORWARD = (
        "printf '%s\\n' \"$*\" >> \"$WK_TEST_SSH_LOG\"\n"
        "case \" $* \" in\n"
        "    *\" -N \"*) exec sleep 30 ;;\n"
        "esac\n"
        "[ -t 0 ] || cat >/dev/null\n"
        "exit 0\n"
    )

    DRIVER = (
        '. "$WK_ROOT/lib/common.sh"\n'
        '. "$WK_ROOT/lib/store.sh"\n'
        '. "$WK_ROOT/lib/target.sh"\n'
        'load_target vm >/dev/null 2>&1\n'
    )

    def _driver(self, body, vmstore):
        (self.tmp / "ssh.log").write_text("")
        with stub_path({"ssh": self.FAKE_SSH_FORWARD, "tart": FAKE_TART}) as binp:
            env = {
                "PATH": f"{binp}:{os.environ['PATH']}",
                "WK_TEST_SSH_LOG": str(self.tmp / "ssh.log"),
                "WK_VM_STORE": str(vmstore),
                "WK_LOCK_DIR": str(self.tmp / "locks"),
                "XDG_STATE_HOME": str(self.tmp / "state"),
                "WK_STORE": str(self.tmp / "store"),
                "WK_HOST_SECRETS": str(self.tmp / "store" / "secrets"),
            }
            return bash(self.DRIVER + body, env=env, timeout=120)

    def test_a_second_start_while_one_is_alive_is_a_no_op(self):
        home, vmstore = _guest(self.tmp)
        sf = vmstore / "vm" / "demo.agent-forward"
        self.addCleanup(_kill_pidfile, sf)
        cp = self._driver(
            "_agent_forward_start demo 1.2.3.4 &\n"
            "_agent_forward_start demo 1.2.3.4 &\n"
            "wait\n", vmstore)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertTrue(sf.exists(),
                        "the second start deleted the first's status file")
        log = (self.tmp / "ssh.log").read_text()
        self.assertEqual(1, len([l for l in log.splitlines() if " -N " in l]),
                         "two forwards were started for one guest:\n" + log)

    def test_a_failed_start_removes_only_its_own_record(self):
        """The failure path's `rm -f`: with an overlapping run's pid in the
        file, removing it is what leaves `wk push off` nothing to stop."""
        home, vmstore = _guest(self.tmp)
        sf = vmstore / "vm" / "demo.agent-forward"
        cp = self._driver(
            '. "$WK_ROOT/lib/detach.sh"\n'
            'mkdir -p "$WK_VM_DIR"\n'
            "detach_run() { echo 999001; }\n"
            "status_write() { printf 'state=running\\npid=999002\\n' > \"$1\"; }\n"
            "_agent_forward_start demo 1.2.3.4 && echo UNEXPECTED-OK\n", vmstore)
        self.assertNotIn("UNEXPECTED-OK", cp.stdout, cp.stdout + cp.stderr)
        self.assertTrue(sf.exists(),
                        "it removed a status file another run had written")
        self.assertIn("pid=999002", sf.read_text())

    def test_its_own_record_is_still_cleaned_up(self):
        """The other side of the same rule: a forward this run started and that
        died leaves nothing behind for `wk push status` to believe."""
        home, vmstore = _guest(self.tmp)
        sf = vmstore / "vm" / "demo.agent-forward"
        cp = self._driver(
            '. "$WK_ROOT/lib/detach.sh"\n'
            'mkdir -p "$WK_VM_DIR"\n'
            "detach_run() { echo 999001; }\n"
            "status_write() { printf 'state=running\\npid=999001\\n' > \"$1\"; }\n"
            "_agent_forward_start demo 1.2.3.4 && echo UNEXPECTED-OK\n", vmstore)
        self.assertNotIn("UNEXPECTED-OK", cp.stdout, cp.stdout + cp.stderr)
        self.assertFalse(sf.exists(), cp.stdout + cp.stderr)

    def test_both_ends_of_the_switch_take_the_same_lock(self):
        """A stop that runs while a start is mid-flight would kill a pid the
        start is about to overwrite; one resource, both callers."""
        vm = (REPO / "targets" / "vm.sh").read_text()
        for fn in ("_agent_forward_start", "_agent_forward_stop"):
            with self.subTest(fn=fn):
                self.assertIn('with_lock "vm-agent-forward-$1" -- %s_locked' % fn,
                              func_body(vm, fn))


class TestTheGuestGetsTheInjectorsCa(WkTest):
    """api.github.com is terminated on this host for a guest, so the guest has
    to trust the injector's CA and hold the placeholder -- delivered with the
    proxy address, because both are properties of this host and neither may be
    baked into an image."""

    def _egress(self, home, vmstore, ca_text=None):
        log = self.tmp / "ssh.log"
        log.write_text("")
        vmdir = vmstore / "vm"
        vmdir.mkdir(parents=True, exist_ok=True)
        if ca_text is not None:
            (vmdir / "wk-github-ca.pem").write_text(ca_text)
        with stub_path({"ssh": FAKE_SSH, "tart": FAKE_TART}) as binp:
            cp = bash('''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target vm >/dev/null 2>&1
_set_guest_egress demo 1.2.3.4 || true
''', env={
                "PATH": f"{binp}:{os.environ['PATH']}",
                "WK_TEST_GUEST": str(home),
                "WK_TEST_SSH_LOG": str(log),
                "WK_STORE": str(self.tmp / "store"),
                "WK_HOST_SECRETS": str(self.tmp / "store" / "secrets"),
                "WK_VM_STORE": str(vmstore),
                "WK_VM_PROXY_ADDR": "192.168.2.1",
            })
        return cp

    def test_the_ca_and_the_placeholder_go_in_with_the_proxy(self):
        home, vmstore = _guest(self.tmp)
        self._egress(home, vmstore, ca_text="-----BEGIN CERTIFICATE-----\nx\n"
                                            "-----END CERTIFICATE-----\n")
        rc = (home / ".wk-egress").read_text()
        self.assertIn("http_proxy=http://192.168.2.1:3128", rc)
        self.assertIn("GITHUB_COM_TOKEN=wk-injects-this", rc)
        self.assertIn("GITHUB_COM_USERNAME=justinmichaud", rc)
        self.assertIn("REQUESTS_CA_BUNDLE=", rc)
        self.assertIn("CURL_CA_BUNDLE=", rc)
        self.assertIn("GIT_SSL_CAINFO=", rc)
        self.assertIn("BEGIN CERTIFICATE", (home / ".wk-github-ca.pem").read_text())

    def test_the_bundle_is_the_systems_plus_that_ca_and_not_that_ca_alone(self):
        """Those variables replace the trust store outright: a bundle holding
        one certificate would fail every other HTTPS request in the guest."""
        vm = (REPO / "targets" / "vm.sh").read_text()
        self.assertIn('cat /etc/ssl/cert.pem "\\$HOME/.wk-github-ca.pem" '
                      '> "\\$HOME/.wk-ca-bundle.pem"', vm)

    def test_an_unfiltered_guest_gets_neither(self):
        """WK_VM_UNFILTERED means no proxy and so no injector: a guest left
        trusting a CA nothing terminates with would be a certificate anybody
        who obtained the key could use against it."""
        home, vmstore = _guest(self.tmp)
        (home / ".wk-github-ca.pem").write_text("stale\n")
        self._egress(home, vmstore, ca_text="x\n")
        # The real function reads WK_VM_UNFILTERED; run it again with that set.
        with stub_path({"ssh": FAKE_SSH, "tart": FAKE_TART}) as binp:
            bash('''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target vm >/dev/null 2>&1
_set_guest_egress demo 1.2.3.4 || true
''', env={
                "PATH": f"{binp}:{os.environ['PATH']}",
                "WK_TEST_GUEST": str(home),
                "WK_TEST_SSH_LOG": str(self.tmp / "ssh2.log"),
                "WK_STORE": str(self.tmp / "store"),
                "WK_HOST_SECRETS": str(self.tmp / "store" / "secrets"),
                "WK_VM_STORE": str(vmstore),
                "WK_VM_UNFILTERED": "1",
            })
        self.assertFalse((home / ".wk-github-ca.pem").exists())
        self.assertFalse((home / ".wk-ca-bundle.pem").exists())


SETUP = """
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/resources.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target vm >/dev/null 2>&1
"""


class TestTheInjectorReadinessProbeAnswersOnThisPlatform(WkTest):
    """`nc -z -U` answers 1 for a unix socket that is being served on macOS
    (measured 2026-09-05, macOS 26.6.2), which is where every guest runs. That
    false negative started a second injector over a live one: the warning said
    the guest had no injector, and api.github.com answered 000 in the guest for
    the rest of the session."""

    def _running(self, sock):
        return bash(
            SETUP + "_inject_sock() { printf %s " + repr(str(sock))
            + "; }\n_inject_running && echo YES || echo NO\n").stdout.strip()

    def test_a_served_socket_reads_as_running(self):
        import socket as sk
        sock = self.tmp / "served.sock"
        srv = sk.socket(sk.AF_UNIX)
        srv.bind(str(sock))
        srv.listen(1)
        try:
            self.assertEqual("YES", self._running(sock))
        finally:
            srv.close()

    def test_a_socket_nothing_listens_on_reads_as_not_running(self):
        """What a crashed injector leaves behind: the path is still a socket."""
        import socket as sk
        sock = self.tmp / "dead.sock"
        srv = sk.socket(sk.AF_UNIX)
        srv.bind(str(sock))
        srv.close()
        self.assertEqual("NO", self._running(sock))

    def test_no_socket_at_all_reads_as_not_running(self):
        self.assertEqual("NO", self._running(self.tmp / "absent.sock"))


class TestTheGuestsInjectorGetsTheStandingReadToken(WkTest):
    """The injector a guest talks to runs on this host, so its standing read
    token is a file here. Reading is open whatever position `wk push` is in, so
    every `wk vm start` converges that file from what this host holds -- and
    `wk push on|off` never touches it."""

    START_INJECT = """
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target vm >/dev/null 2>&1
# Already up: what a start has to converge is the token, whether or not it
# also has to start the program.
_inject_running() { return 0; }
_start_host_inject
"""

    def _start_inject(self, vmstore, pat=None):
        store = self.tmp / "store"
        held = store / "push-keys"
        held.mkdir(parents=True, exist_ok=True)
        (store / "secrets").mkdir(parents=True, exist_ok=True)
        if pat is None:
            (held / "github-pat").unlink(missing_ok=True)
        else:
            (held / "github-pat").write_text(pat)
        return bash(self.START_INJECT,
                    env={"WK_STORE": str(store),
                         "WK_HOST_SECRETS": str(store / "secrets"),
                         "WK_VM_STORE": str(vmstore)})

    def read_pat(self, vmstore):
        return vmstore / "vm" / "read-github-pat"

    def test_a_start_writes_it_from_the_token_this_host_holds(self):
        _, vmstore = _guest(self.tmp)
        cp = self._start_inject(vmstore, pat="ghp-not-a-real-token\n")
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertEqual("ghp-not-a-real-token\n", self.read_pat(vmstore).read_text())
        self.assertEqual(0o600, self.read_pat(vmstore).stat().st_mode & 0o777)

    def test_a_token_withdrawn_on_this_host_is_gone_at_the_next_start(self):
        _, vmstore = _guest(self.tmp)
        self._start_inject(vmstore, pat="ghp-not-a-real-token\n")
        self._start_inject(vmstore, pat=None)
        self.assertFalse(self.read_pat(vmstore).exists())

    def test_the_injector_is_told_where_to_read_it(self):
        """A file nothing names is a file nothing reads: the program takes the
        path from WK_INJECT_READ_PAT (container/proxy/github-inject.py)."""
        body = func_body((REPO / "targets" / "vm.sh").read_text(), "_start_host_inject")
        self.assertIn('WK_INJECT_READ_PAT="$(_inject_read_pat)"', body)
        self.assertIn("github-inject.py", body)

    @unittest.skipUnless(os.uname().sysname == "Darwin",
                         "guests are a macOS-host thing (tart)")
    def test_neither_position_of_the_switch_touches_it(self):
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp, keys=("fork",))
        (store / "push-keys" / "github-pat").write_text("ghp-not-a-real-token\n")
        (vmstore / "vm").mkdir(parents=True, exist_ok=True)
        self.read_pat(vmstore).write_text("ghp-standing\n")
        for action in ("on", "off"):
            with self.subTest(action=action):
                TestTheGuestHalfOfTheSwitch._push(self, action, store, home, vmstore)
                self.assertEqual("ghp-standing\n", self.read_pat(vmstore).read_text())


class TestBothHalvesRunHere(WkTest):
    def test_the_whole_command_runs_here(self):
        src = (REPO / "cmd" / "push").read_text()
        self.assertIn("# wk: where=local", src)
        for gone in ("where=store", "--store", "STORE_ONLY"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, src)

    def test_the_machine_half_runs_before_the_guests_are_converged(self):
        """A guest converged first would be handed a forward to an agent whose
        contents the machine half is about to change."""
        src = (REPO / "cmd" / "push").read_text()
        body = src[src.index("SWITCH_RC=0"):]
        self.assertLess(body.index("switch_half"), body.index("converge_guests"))

    def test_the_action_reaches_the_guest_half(self):
        """The private halves no longer move, so the guest half cannot infer
        the position from what it can read: it is told."""
        src = (REPO / "cmd" / "push").read_text()
        self.assertIn('_in_vm_driver vm_push_keys_converge "$ACTION"', src)
        vm = (REPO / "targets" / "vm.sh").read_text()
        self.assertIn("vm_push_keys_converge() { # <on|off>", vm)

    def test_a_workspace_is_still_refused(self):
        marker = self.tmp / "wk-workspace"
        marker.write_text("name=probe\ntarget=container\n")
        for action in ("on", "off", "status"):
            with self.subTest(action=action):
                cp = self.run_wk("push", action, env={"WK_MARKER": str(marker)})
                self.assertNotEqual(cp.returncode, 0, cp.stdout)
                self.assertIn("workspace", cp.stdout)

    def test_stopping_a_guest_ends_its_forward(self):
        """A forward is a process on this host; one left holding a socket in a
        guest that is gone is a process nothing would ever reap."""
        vm = (REPO / "targets" / "vm.sh").read_text()
        body = vm[vm.index("t_stop() {"):]
        body = body[:body.index("\n}\n")]
        self.assertIn('_agent_forward_stop "$1"', body)
        self.assertLess(body.index("_agent_forward_stop"), body.index("_tart stop"))


@unittest.skipUnless(os.uname().sysname == "Darwin",
                     "the store is only somewhere else on a macOS host")
class TestNothingStartsThePodmanMachine(WkTest):
    """The credentials are on this host, so no part of the switch has to start
    the machine. Reaching an agent that is *in* it is one `podman machine ssh`,
    which is not the same thing as starting it."""

    def _push(self, *args):
        plog = self.tmp / "podman.log"
        plog.write_text("")
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp, keys=("fork",))
        self.addCleanup(_kill_pidfile, vmstore / "vm" / "ssh-agent.pid")
        with stub_path({"podman": FAKE_PODMAN, "ssh": FAKE_SSH,
                        "tart": FAKE_TART_STOPPED}) as binp:
            cp = self.run_wk("push", *args, env={
                "PATH": f"{binp}:{os.environ['PATH']}",
                # The real store path, which this host cannot read: what used
                # to make the hop happen at all.
                "WK_STORE": "/var/lib/wk",
                "WK_HOST_SECRETS": str(store / "secrets"),
                "WK_VM_STORE": str(vmstore),
                "WK_TEST_GUEST": str(home),
                "WK_TEST_SSH_LOG": str(self.tmp / "ssh.log"),
                "WK_TEST_PODMAN_LOG": str(plog),
            })
        return cp, plog.read_text()

    def test_off_asks_the_agent_and_starts_nothing(self):
        cp, plog = self._push("off")
        self.assertNotIn("machine start", plog, plog)
        self.assertIn("push is OFF", cp.stdout)

    def test_status_does_not_start_it(self):
        cp, plog = self._push("status")
        self.assertNotIn("machine start", plog, plog)
        self.assertIn("stopped", cp.stdout)


if __name__ == "__main__":
    unittest.main()
