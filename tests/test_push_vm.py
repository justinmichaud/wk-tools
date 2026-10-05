"""`wk push` reaches a macOS guest: the guest holds the ssh config and public halves, its agent is on this host and
reaches it through one `ssh -N -R` per running guest, and no private key byte is ever written into it. `tart` and
`ssh` are stubs whose guest is a scratch directory; the keys and the ssh-agent are real.

Run: python3 -m unittest tests.test_push_vm -v
"""
import contextlib
import io
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

from tests.support import (guest_step, REPO,
                           WkTest, stub_path)

sys.path.insert(0, str(REPO / "lib"))
from wk import guest, secrets, targets  # noqa: E402
from wk.store import Store  # noqa: E402

# `tart`: one running guest called wk-demo, and the guest itself as a directory. `tart exec` hands the guest's
# command as the last argument for a login shell, and everything it writes it writes under $HOME, so running that command with HOME
# pointed at a scratch directory exercises the real umask, mkdir, redirect and rm -- not a transcript of them.
# /Users/admin is rewritten to that directory for the same reason: the agent socket's path has to be absolute on the
# guest, because that is what `ssh -R` binds. `tart list` mixes local VMs with cached OCI images, so Source matters.
# Every invocation is appended to $WK_TEST_LOG, which is how a test asks whether a private key was ever an argument.
FAKE_TART = '''
case "$1" in
list) echo '[{"Name":"wk-demo","State":"running","Source":"local"}]' ;;
ip)   echo 1.2.3.4 ;;
exec)
    printf '%s\\n' "$*" >> "$WK_TEST_LOG"
    for a in "$@"; do last="$a"; done
    cmd=$(printf '%s' "$last" | sed "s|/Users/admin|$WK_TEST_GUEST|g")
    HOME="$WK_TEST_GUEST" SHELL=$(command -v bash) bash -lc "$cmd"; rc=$?
    [ -t 0 ] || cat >/dev/null
    exit $rc ;;
*)    exit 1 ;;
esac
'''

# `tart` with the same guest stopped.
FAKE_TART_STOPPED = '''
case "$1" in
list) echo '[{"Name":"wk-demo","State":"stopped","Source":"local"}]' ;;
*)    exit 1 ;;
esac
'''

# `ssh`: only the socket forward, which rides the guest's sshd on tart exec and stays up -- what sshd does at the far
# end of `-R <remote>:<local>` is bind the remote socket, and `test -S` on it is how this host reports that a guest
# reaches the agent. Anything else is a network ssh into a guest, which nothing makes.
FAKE_SSH = '''
printf '%s\\n' "$*" >> "$WK_TEST_LOG"
case " $* " in
    *" -N "*)
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
echo "ssh: a guest is reached through tart exec, never the network" >&2
exit 255
'''

def _guest(tmp, name="demo", claude=()):
    """A scratch guest home plus the host-side workspace directory and ready
    marker Vm.created reads (lib/wk/targets.py) -- without which Vm.info says
    `creating`, not `running`. The guest's `ps` lists the `claude` pids given:
    its commands run on this host, whose own processes are not the guest's."""
    home = tmp / "guest-home"
    (home / ".ssh").mkdir(parents=True, exist_ok=True)
    (home / "bin").mkdir(exist_ok=True)
    (home / "bin" / "ps").write_text("#!/bin/sh\n" + "".join("echo '%s /Users/admin/.local/bin/claude'\n" % p for p in claude))
    (home / "bin" / "ps").chmod(0o755)
    (home / ".bash_profile").write_text('PATH="$HOME/bin:$PATH"\n')
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


def _forward_pidfile(vmstore, name="demo"):
    """`wk push on` starts one tunnel per guest, a daemon whose pidfile is in the vm target's own store."""
    return pathlib.Path(vmstore) / "vm" / ("%s.agent-forward.pid" % name)


def _kill_forward(vmstore, name="demo"):
    _kill_pidfile(_forward_pidfile(vmstore, name))


def _kill_pidfile(path):
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


class TestOneAliasBlock(WkTest):
    """IdentityFile never carries `.pub`: OpenSSH 10 then loads the public file as the private key (10.2p1)."""

    CONTAINER = ("/secrets", "build_key_", "/run/wk/ssh-agent.sock")
    GUEST = ("~/.ssh", "id_", "/a/sock", "nc %h %p")

    def _blocks(self, args):
        return secrets.alias_blocks(secrets.forks(), *args)

    def test_a_build_box_names_no_identity_and_refuses_by_name(self):
        out = secrets.box_alias_blocks(secrets.forks())
        self.assertIn("Host github-webkit", out)
        self.assertNotIn("IdentityFile", out)
        self.assertNotIn("IdentityAgent", out)
        self.assertIn("wk pr open", out)

    def test_a_container_names_the_identity_and_the_mounted_socket(self):
        out = self._blocks(self.CONTAINER)
        self.assertIn("IdentityFile /secrets/build_key_fork\n", out)
        self.assertIn("IdentityAgent /run/wk/ssh-agent.sock", out)
        self.assertIn("IdentitiesOnly yes", out)
        self.assertNotIn("ProxyCommand", out)

    def test_a_guest_names_its_own_public_copy_and_carries_a_proxy(self):
        out = self._blocks(("/Users/admin/.ssh", "id_", "/Users/admin/.wk-ssh-agent.sock",
                            "nc -X connect -x 10.0.0.1:3128 %h %p"))
        self.assertIn("IdentityFile /Users/admin/.ssh/id_fork\n", out)
        self.assertIn("IdentityAgent /Users/admin/.wk-ssh-agent.sock", out)
        self.assertIn("ProxyCommand nc -X connect -x 10.0.0.1:3128 %h %p", out)

    def test_no_identity_line_ever_carries_the_pub_suffix(self):
        """The suffix is what made OpenSSH 10 read the public file as a
        private key; ssh appends `.pub` itself."""
        for args in (self.CONTAINER, self.GUEST):
            with self.subTest(args=args):
                for line in self._blocks(args).splitlines():
                    if line.strip().startswith("IdentityFile"):
                        self.assertFalse(line.strip().endswith(".pub"), line)

    def test_every_fork_gets_a_block(self):
        aliases = [r[2] for r in secrets.forks()]
        self.assertTrue(aliases)
        out = self._blocks(("/d",))
        for a in aliases:
            self.assertIn(f"Host {a}\n", out)

    def test_the_arg_sets_differ_only_where_they_must(self):
        """Byte-identical modulo the identity, the agent and the
        ProxyCommand: the callers must not drift into offering different
        StrictHostKeyChecking, User or HostName."""
        def norm(text):
            out = []
            for line in text.splitlines():
                s = line.strip()
                if s.startswith(("IdentityFile", "IdentitiesOnly", "ProxyCommand", "IdentityAgent")):
                    continue
                else:
                    out.append(line)
            return "\n".join(out)

        container = self._blocks(self.CONTAINER)
        guest = self._blocks(self.GUEST)
        box = secrets.box_alias_blocks(secrets.forks())
        self.assertEqual(norm(container), norm(guest))
        self.assertEqual(norm(container), norm(box))

class TestAGuestGetsTheConfigOnStart(WkTest):
    """The real Guest.write_deploy_keys, against a fake guest: what it writes is
    what a guest would end up holding -- and what it must never write."""

    def _write(self, store, home, vmstore, extra=None):
        log = self.tmp / "guest.log"
        log.write_text("")
        with stub_path({"ssh": FAKE_SSH, "tart": FAKE_TART}) as binp:
            env = {
                "PATH": f"{binp}:{os.environ['PATH']}",
                "WK_TEST_GUEST": str(home),
                "WK_TEST_LOG": str(log),
                "WK_STORE": str(store),
                # The keys are this device's own directory, which on macOS is
                # not under $WK_STORE; a test must not read the real one.
                "WK_HOST_SECRETS": str(store / "secrets"),
                "WK_VM_STORE": str(vmstore),
                "WK_VM_PROXY_ADDR": "192.0.2.1",
            }
            if extra:
                env.update(extra)
            cp = guest_step(env, "write_deploy_keys")
        self.log = log.read_text()
        return cp

    def test_the_public_half_lands_in_the_guest_and_no_private_key_byte_does(self):
        home, vmstore = _guest(self.tmp)
        cp = self._write(_store(self.tmp, keys=("fork", "forkwpe")), home, vmstore)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("ssh-ed25519 ", (home / ".ssh" / "id_fork.pub").read_text())
        self.assertFalse((home / ".ssh" / "id_fork").exists())
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
        home, vmstore = _guest(self.tmp)
        (home / ".ssh" / "id_fork.pub").write_text("stale\n")
        (home / ".ssh" / "id_forkwpe.pub").write_text("stale\n")
        cp = self._write(_store(self.tmp), home, vmstore)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertFalse((home / ".ssh" / "id_fork.pub").exists())
        self.assertFalse((home / ".ssh" / "id_forkwpe.pub").exists())

    def test_the_config_names_the_aliases_the_agent_and_a_route_to_github(self):
        home, vmstore = _guest(self.tmp)
        cfg = self._write(_store(self.tmp, keys=("fork",)), home, vmstore)
        self.assertEqual(cfg.returncode, 0, cfg.stdout + cfg.stderr)
        text = (home / ".ssh" / "config").read_text()
        self.assertIn("Host github-webkit", text)
        self.assertIn("Host github-wpe", text)
        self.assertIn("IdentityFile /Users/admin/.ssh/id_fork\n", text)
        self.assertIn("IdentityAgent /Users/admin/.wk-ssh-agent.sock", text)
        self.assertIn("-X connect -x 192.0.2.1:3128 %h %p", text)

    def test_the_config_is_written_once_however_often_and_with_no_key_behind_it(self):
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp)
        for _ in range(3):
            self._write(store, home, vmstore)
        text = (home / ".ssh" / "config").read_text()
        self.assertEqual(1, text.count("Host github-webkit"), text)


class TestTheGuestHalfOfTheSwitch(WkTest):
    """`wk push` end to end with one fake guest and the real ssh-agent the code under test starts."""

    def _push(self, action, store, home, vmstore, tart=FAKE_TART):
        log = self.tmp / "guest.log"
        log.write_text("")
        self.addCleanup(_kill_pidfile, vmstore / "vm" / "ssh-agent.pid")
        self.addCleanup(_kill_forward, vmstore)
        with stub_path({"ssh": FAKE_SSH, "tart": tart}) as binp:
            env = {
                "PATH": f"{binp}:{os.environ['PATH']}",
                "WK_TEST_GUEST": str(home),
                "WK_TEST_LOG": str(log),
                "WK_STORE": str(store),
                "WK_HOST_SECRETS": str(store / "secrets"),
                "WK_VM_STORE": str(vmstore),
                "WK_VM_PROXY_ADDR": "192.0.2.1",
                "WK_PUSH_AGENT_SOCK": str(self.tmp / "no-machine-agent.sock"),
                "WK_PUSH_PAT_FILE": str(self.tmp / "machine-pat"),
                "WK_MACHINE": "wk-no-such-machine",
            }
            cp = self.run_wk("push", action, env=env)
        self.log = log.read_text()
        return cp

    def _forward_is_up(self, vmstore, name="demo"):
        return _forward_pidfile(vmstore, name).exists()

    @unittest.skipUnless(os.uname().sysname == "Darwin",
                         "guests are a macOS-host thing (tart)")
    def test_on_stops_at_a_claude_session_in_a_guest(self):
        """The guest's own `ps` names a claude pid, which `on` asks to end before loading anything."""
        home, vmstore = _guest(self.tmp, claude=("999999",))
        store = _store(self.tmp, keys=("fork",))
        cp = self._push("on", store, home, vmstore)
        self.assertIn("demo", cp.stdout)
        self.assertFalse(self._forward_is_up(vmstore))

    @unittest.skipUnless(os.uname().sysname == "Darwin",
                         "guests are a macOS-host thing (tart)")
    def test_on_loads_the_hosts_agent_and_forwards_it_into_the_guest(self):
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp, keys=("fork",))
        cp = self._push("on", store, home, vmstore)
        sock = vmstore / "vm" / "ssh-agent.sock"
        listed = subprocess.run(["ssh-add", "-l"], text=True,
                                env={**os.environ, "SSH_AUTH_SOCK": str(sock)},
                                stdout=subprocess.PIPE).stdout
        self.assertIn("SHA256:", listed)
        self.assertTrue(self._forward_is_up(vmstore), cp.stdout)
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
        sock = vmstore / "vm" / "ssh-agent.sock"
        listed = subprocess.run(["ssh-add", "-l"], text=True,
                                env={**os.environ, "SSH_AUTH_SOCK": str(sock)},
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT).stdout
        self.assertIn("no identities", listed)
        self.assertFalse(self._forward_is_up(vmstore), cp.stdout)

    @unittest.skipUnless(os.uname().sysname == "Darwin",
                         "guests are a macOS-host thing (tart)")
    def test_a_stopped_guest_is_reported_not_started(self):
        home, vmstore = _guest(self.tmp)
        cp = self._push("status", _store(self.tmp, keys=("fork",)), home, vmstore,
                        tart=FAKE_TART_STOPPED)
        self.assertIn("stopped", cp.stdout)
        self.assertEqual("", self.log, self.log)

    @unittest.skipUnless(os.uname().sysname == "Darwin",
                         "guests are a macOS-host thing (tart)")
    def test_status_is_on_while_the_guests_agent_holds_a_key_with_no_guest_up(self):
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp, keys=("fork",))
        self._push("on", store, home, vmstore)
        cp = self._push("status", store, home, vmstore, tart=FAKE_TART_STOPPED)
        self.assertIn("in the agent this host runs for them and its own pushes", cp.stdout)
        self.assertIn("push is ON", cp.stdout)
        self.assertEqual(0, cp.returncode, cp.stdout)

    @unittest.skipUnless(os.uname().sysname == "Darwin",
                         "guests are a macOS-host thing (tart)")
    def test_off_is_not_reported_until_that_agent_is_empty(self):
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp, keys=("fork",))
        self._push("on", store, home, vmstore)
        self._push("off", store, home, vmstore)
        cp = self._push("status", store, home, vmstore, tart=FAKE_TART_STOPPED)
        self.assertEqual(1, cp.returncode, cp.stdout)

    @unittest.skipUnless(os.uname().sysname == "Darwin",
                         "guests are a macOS-host thing (tart)")
    def test_status_is_off_when_no_guest_reaches_it_either(self):
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp, keys=("fork",))
        cp = self._push("status", store, home, vmstore)
        self.assertEqual(cp.returncode, 1, cp.stdout)


class TestTheGuestGetsTheInjectorsCa(WkTest):

    def _egress(self, home, vmstore, ca_text=None, bugzilla=None, **extra):
        log = self.tmp / "guest.log"
        log.write_text("")
        vmdir = vmstore / "vm"
        vmdir.mkdir(parents=True, exist_ok=True)
        if ca_text is not None:
            (vmdir / "wk-github-ca.pem").write_text(ca_text)
        with stub_path({"ssh": FAKE_SSH, "tart": FAKE_TART}) as binp:
            return guest_step({
                "PATH": f"{binp}:{os.environ['PATH']}",
                "WK_TEST_GUEST": str(home),
                "WK_TEST_LOG": str(log),
                "WK_STORE": str(self.tmp / "store"),
                "WK_HOST_SECRETS": str(self.tmp / "store" / "secrets"),
                "WK_VM_STORE": str(vmstore),
                "WK_VM_PROXY_ADDR": "192.0.2.1",
                "XDG_STATE_HOME": str(self.tmp / "state"),
                **extra,
            }, "set_guest_egress", secrets={"bugzilla_user": lambda s: bugzilla})

    def test_the_bugzilla_placeholder_goes_in_with_the_login_and_not_without(self):
        home, vmstore = _guest(self.tmp)
        ca = "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n"
        self._egress(home, vmstore, ca_text=ca)
        self.assertNotIn("BUGS_WEBKIT_ORG", (home / ".wk-egress").read_text())
        self._egress(home, vmstore, ca_text=ca, bugzilla="me@example.test")
        rc = (home / ".wk-egress").read_text()
        self.assertIn("export BUGS_WEBKIT_ORG_USERNAME=me@example.test\n", rc)
        self.assertIn("export BUGS_WEBKIT_ORG_PASSWORD=wk-injects-this\n", rc)

    def test_the_ca_and_the_placeholder_go_in_with_the_proxy(self):
        home, vmstore = _guest(self.tmp)
        self._egress(home, vmstore, ca_text="-----BEGIN CERTIFICATE-----\nx\n"
                                            "-----END CERTIFICATE-----\n")
        rc = (home / ".wk-egress").read_text()
        self.assertIn("http_proxy=http://192.0.2.1:3128", rc)
        self.assertIn("GITHUB_COM_TOKEN=wk-injects-this", rc)
        self.assertIn("GITHUB_COM_USERNAME=justinmichaud", rc)
        self.assertIn("PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring", rc)
        self.assertIn("REQUESTS_CA_BUNDLE=", rc)
        self.assertIn("CURL_CA_BUNDLE=", rc)
        self.assertIn("GIT_SSL_CAINFO=", rc)
        self.assertIn("BEGIN CERTIFICATE", (home / ".wk-github-ca.pem").read_text())

    def test_an_unfiltered_guest_gets_neither(self):
        home, vmstore = _guest(self.tmp)
        (home / ".wk-github-ca.pem").write_text("stale\n")
        self._egress(home, vmstore, ca_text="x\n", WK_VM_UNFILTERED="1")
        self.assertFalse((home / ".wk-github-ca.pem").exists())
        self.assertFalse((home / ".wk-ca-bundle.pem").exists())


# TEST-NET-1: a real Host kills whatever listens on its proxy address, so never the live guest bridge's.
def _host(case, **env):
    """A Host over this machine and a scratch vm store, as a macOS host has one."""
    mac = mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=True)
    mac.start()
    case.addCleanup(mac.stop)
    base = {"HOME": str(case.tmp / "home"), "PATH": os.environ["PATH"], "WK_STORE": str(case.tmp / "store"),
            "WK_HOST_SECRETS": str(case.tmp / "store" / "secrets"), "WK_VM_STORE": str(case.tmp / "vmstore"),
            "WK_LOCK_DIR": str(case.tmp / "locks"), "XDG_STATE_HOME": str(case.tmp / "state")}
    base.update(env)
    return guest.Host(targets.Registry(str(REPO), env=base).load("vm"))


def _quiet(fn, *args):
    with contextlib.redirect_stderr(io.StringIO()) as err:
        result = fn(*args)
    return result, err.getvalue()


class TestTheInjectorReadinessProbeAnswersOnThisPlatform(WkTest):
    """`nc -z -U` answers 1 for a served unix socket on macOS (measured 2026-09-05, macOS 26.6.2)."""

    def _running(self, sock):
        host = _host(self)
        with mock.patch.object(host, "path", lambda name: str(sock)):
            return "YES" if host.inject_running() else "NO"

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
        import socket as sk
        sock = self.tmp / "dead.sock"
        srv = sk.socket(sk.AF_UNIX)
        srv.bind(str(sock))
        srv.close()
        self.assertEqual("NO", self._running(sock))

    def test_no_socket_at_all_reads_as_not_running(self):
        self.assertEqual("NO", self._running(self.tmp / "absent.sock"))


class TestTheGuestsInjectorGetsTheStandingReadToken(WkTest):

    def _start_inject(self, vmstore, pat=None):
        store = self.tmp / "store"
        held = store / "push-keys"
        held.mkdir(parents=True, exist_ok=True)
        (store / "secrets").mkdir(parents=True, exist_ok=True)
        if pat is None:
            (held / "github-pat").unlink(missing_ok=True)
        else:
            (held / "github-pat").write_text(pat)
        host = _host(self, WK_VM_STORE=str(vmstore))
        with mock.patch.object(host, "inject_running", lambda: True):
            return _quiet(host.start_inject)

    def read_pat(self, vmstore):
        return vmstore / "vm" / "read-github-pat"

    def test_a_start_writes_it_from_the_token_this_host_holds(self):
        _, vmstore = _guest(self.tmp)
        ok, err = self._start_inject(vmstore, pat="ghp-not-a-real-token\n")
        self.assertTrue(ok, err)
        self.assertEqual("ghp-not-a-real-token\n", self.read_pat(vmstore).read_text())
        self.assertEqual(0o600, self.read_pat(vmstore).stat().st_mode & 0o777)

    def test_a_token_withdrawn_on_this_host_is_gone_at_the_next_start(self):
        _, vmstore = _guest(self.tmp)
        self._start_inject(vmstore, pat="ghp-not-a-real-token\n")
        self._start_inject(vmstore, pat=None)
        self.assertFalse(self.read_pat(vmstore).exists())

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


class TestEveryGuestStartConvergesTheReadToken(WkTest):

    def test_a_start_that_finds_the_proxy_up_still_delivers_the_token(self):
        _, vmstore = _guest(self.tmp)
        store = self.tmp / "store"
        (store / "push-keys").mkdir(parents=True, exist_ok=True)
        (store / "secrets").mkdir(parents=True, exist_ok=True)
        (store / "push-keys" / "github-pat").write_text("ghp-todays\n")
        read_pat = vmstore / "vm" / "read-github-pat"
        (vmstore / "vm").mkdir(parents=True, exist_ok=True)
        read_pat.write_text("ghp-yesterdays\n")
        host = _host(self, WK_VM_PROXY_ADDR="192.0.2.1")
        with mock.patch.object(host, "proxy_running", lambda: True), mock.patch.object(host, "inject_running", lambda: True):
            ok, err = _quiet(host.start_proxy)
        self.assertTrue(ok, err)
        self.assertEqual("ghp-todays\n", read_pat.read_text())

class TestAHostDaemonOlderThanItsSourceIsRestarted(WkTest):
    """A daemon whose pidfile is older than container/proxy's sources is restarted."""

    def _run(self, stamp):
        pidfile = self.tmp / "proxy.pid"
        pid = int(subprocess.run(["sh", "-c", "sleep 60 >/dev/null 2>&1 & echo $!"], capture_output=True,
                                 text=True).stdout.strip())
        self.addCleanup(lambda: subprocess.run(["kill", str(pid)], capture_output=True))
        pidfile.write_text("%d\n" % pid)
        os.utime(pidfile, (stamp, stamp))
        host = _host(self, WK_VM_PROXY_ADDR="192.0.2.1")
        _, err = _quiet(host.restart_if_stale, str(pidfile), "egress proxy", host.proxy_where())
        deadline = time.time() + 5
        while time.time() < deadline and subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0:
            time.sleep(0.1)
        alive = subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0
        return alive, pidfile.exists(), err

    def test_one_started_before_its_source_changed_is_stopped(self):
        alive, kept, err = self._run(time.mktime((2001, 1, 1, 0, 0, 0, 0, 0, -1)))
        self.assertFalse(alive, err)
        self.assertFalse(kept)
        self.assertIn("restarting the egress proxy", err, err)

    def test_one_started_after_is_left_alone(self):
        alive, kept, err = self._run(time.mktime((2030, 1, 1, 0, 0, 0, 0, 0, -1)))
        self.assertTrue(alive, err)
        self.assertTrue(kept)
        self.assertNotIn("restarting", err)

@unittest.skipUnless(os.uname().sysname == "Darwin",
                     "guests are a macOS-host thing (tart)")
@unittest.skipUnless(shutil.which("ssh-agent"), "needs ssh-agent")
class TestOneOffClearsEveryAgentThisMachineRuns(WkTest):
    """One `wk push off` empties both the containers' agent and the guests'."""

    def _machine_agent(self):
        sock = self.tmp / "machine-agent.sock"
        out = subprocess.run(["ssh-agent", "-s", "-a", str(sock)],
                             stdout=subprocess.PIPE, text=True,
                             check=True).stdout
        for part in out.split(";"):
            if "SSH_AGENT_PID=" in part:
                pid = int(part.split("=", 1)[1])
                self.addCleanup(lambda: os.kill(pid, signal.SIGTERM))
        return sock

    def _identities(self, sock):
        return subprocess.run(["ssh-add", "-l"], text=True,
                              env={**os.environ, "SSH_AUTH_SOCK": str(sock)},
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT).stdout

    def _push(self, action, store, home, vmstore, machine_sock):
        log = self.tmp / "guest.log"
        log.write_text("")
        self.addCleanup(_kill_pidfile, vmstore / "vm" / "ssh-agent.pid")
        self.addCleanup(_kill_forward, vmstore)
        with stub_path({"ssh": FAKE_SSH, "tart": FAKE_TART}) as binp:
            return self.run_wk("push", action, env={
                "PATH": f"{binp}:{os.environ['PATH']}",
                "WK_TEST_GUEST": str(home),
                "WK_TEST_LOG": str(log),
                "WK_STORE": str(store),
                "WK_HOST_SECRETS": str(store / "secrets"),
                "WK_VM_STORE": str(vmstore),
                "WK_VM_PROXY_ADDR": "192.0.2.1",
                "WK_PUSH_AGENT_SOCK": str(machine_sock),
                "WK_PUSH_PAT_FILE": str(self.tmp / "machine-pat"),
            })

    def test_on_loads_both_and_one_off_empties_both(self):
        home, vmstore = _guest(self.tmp)
        store = _store(self.tmp, keys=("fork",))
        machine = self._machine_agent()
        guest_agent = vmstore / "vm" / "ssh-agent.sock"

        cp = self._push("on", store, home, vmstore, machine)
        self.assertIn("SHA256:", self._identities(machine), cp.stdout)
        self.assertIn("SHA256:", self._identities(guest_agent), cp.stdout)

        cp = self._push("off", store, home, vmstore, machine)
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertIn("no identities", self._identities(machine))
        self.assertIn("no identities", self._identities(guest_agent))


class TestABoxPushReachesTheGuestsAgent(WkTest):
    def test_a_box_push_on_this_host_reaches_the_agent_the_guest_half_loads(self):
        """`wk pr open`'s push runs here, on the socket `vm_push_keys_converge` loads."""
        from wk.machine import Fake
        env = {"HOME": str(self.tmp), "WK_VM_STORE": str(self.tmp / "vs"), "WK_STORE": str(self.tmp / "st"), "PATH": "/usr/bin"}
        sec, sock = guest.push_agent(str(REPO), Fake(), env)
        self.assertEqual((sock, sec.agent_argv("true")), (str(self.tmp / "vs" / "vm" / "ssh-agent.sock"), ["sh", "-c", "true"]))


if __name__ == "__main__":
    unittest.main()
