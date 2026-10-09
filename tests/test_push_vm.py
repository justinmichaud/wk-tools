"""What a macOS guest is given by the host daemons it shares: the injector's CA and placeholders, its tokens, and a proxy kept on the program in the tree.
`tart` and `ssh` are stubs whose guest is a scratch directory.
"""
import contextlib
import io
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from tests.support import (guest_step, REPO,
                           WkTest, stub_path)

sys.path.insert(0, str(REPO / "lib"))
from wk import guest, places  # noqa: E402
from wk.store import Store  # noqa: E402

# `tart`: one running guest called wk-demo, and the guest itself as a directory. `tart exec` hands the guest's
# command as the last argument for a login shell, and everything it writes it writes under $HOME, so running that command with HOME
# pointed at a scratch directory exercises the real umask, mkdir, redirect and rm -- not a transcript of them.
# /Users/admin is rewritten to that directory for the same reason. `tart list` mixes local VMs with cached OCI images, so
# Source matters. Every invocation is appended to $WK_TEST_LOG.
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

def _guest(tmp, name="demo"):
    """A scratch guest home, and the ready marker without which Vm.info says `creating`."""
    home = tmp / "guest-home"
    home.mkdir(parents=True, exist_ok=True)
    vmstore = tmp / "vmstore"
    ws = vmstore / "ws" / name
    ws.mkdir(parents=True, exist_ok=True)
    (ws / ".wk-ready").write_text("")
    return home, vmstore



def _stub_env(binp, home, log, store, vmstore, **extra):
    return {"PATH": f"{binp}:{os.environ['PATH']}", "WK_TEST_GUEST": str(home), "WK_TEST_LOG": str(log),
            "WK_STORE": str(store), "WK_HOST_SECRETS": str(store / "secrets"), "WK_VM_STORE": str(vmstore),
            "WK_VM_PROXY_ADDR": "192.0.2.1", **extra}


class TestTheGuestGetsTheInjectorsCa(WkTest):

    def _egress(self, home, vmstore, ca_text=None, bugzilla=None, **extra):
        log = self.tmp / "guest.log"
        log.write_text("")
        vmdir = vmstore / "vm"
        vmdir.mkdir(parents=True, exist_ok=True)
        if ca_text is not None:
            (vmdir / "wk-github-ca.pem").write_text(ca_text)
        with stub_path({"tart": FAKE_TART}) as binp:
            env = _stub_env(binp, home, log, self.tmp / "store", vmstore, XDG_STATE_HOME=str(self.tmp / "state"), **extra)
            return guest_step(env, "set_guest_egress", secrets={"bugzilla_user": lambda s: bugzilla})

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
    return guest.Host(places.Registry(str(REPO), env=base).load("vm"))


def _quiet(fn, *args):
    with contextlib.redirect_stderr(io.StringIO()) as err:
        result = fn(*args)
    return result, err.getvalue()


class TestTheInjectorReadinessProbe(WkTest):
    """`nc -z -U` answers 1 for a served unix socket on macOS, so the probe connects in python."""

    def test_only_a_served_socket_reads_as_running(self):
        import socket as sk
        host = _host(self)
        served, dead = sk.socket(sk.AF_UNIX), sk.socket(sk.AF_UNIX)
        served.bind(str(self.tmp / "served.sock"))
        served.listen(1)
        self.addCleanup(served.close)
        dead.bind(str(self.tmp / "dead.sock"))
        dead.close()
        for name, want in (("served.sock", True), ("dead.sock", False), ("absent.sock", False)):
            with self.subTest(name=name), mock.patch.object(host, "path", lambda _n: str(self.tmp / name)):
                self.assertEqual(want, host.inject_running())


class TestTheGuestsInjectorGetsTheStandingCredentials(WkTest):

    def _start_inject(self, vmstore, pat=None, bz=None):
        store = self.tmp / "store"
        held = store / "push-keys"
        held.mkdir(parents=True, exist_ok=True)
        (store / "secrets").mkdir(parents=True, exist_ok=True)
        for name, value in (("github-pat", pat), ("bugzilla-api-key", bz)):
            if value is None:
                (held / name).unlink(missing_ok=True)
            else:
                (held / name).write_text(value)
        host = _host(self, WK_VM_STORE=str(vmstore))
        with mock.patch.object(host, "inject_running", lambda: True):
            return _quiet(host.start_inject)

    def files(self, vmstore):
        return {n: (vmstore / "vm" / n) for n in ("read-github-pat", "push-github-pat", "push-bugzilla-api-key")}

    def test_a_start_writes_them_from_what_this_host_holds(self):
        _, vmstore = _guest(self.tmp)
        ok, err = self._start_inject(vmstore, pat="ghp-not-a-real-token\n", bz="bz-not-a-real-key\n")
        self.assertTrue(ok, err)
        got = self.files(vmstore)
        self.assertEqual("ghp-not-a-real-token\n", got["read-github-pat"].read_text())
        self.assertEqual("ghp-not-a-real-token\n", got["push-github-pat"].read_text())
        self.assertEqual("bz-not-a-real-key\n", got["push-bugzilla-api-key"].read_text())
        self.assertEqual([0o600] * 3, [p.stat().st_mode & 0o777 for p in got.values()])

    def test_one_withdrawn_on_this_host_is_gone_at_the_next_start(self):
        _, vmstore = _guest(self.tmp)
        self._start_inject(vmstore, pat="ghp-not-a-real-token\n", bz="bz\n")
        self._start_inject(vmstore, pat=None, bz=None)
        self.assertEqual([False] * 3, [p.exists() for p in self.files(vmstore).values()])


class TestEveryGuestStartConvergesTheCredentials(WkTest):

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

if __name__ == "__main__":
    unittest.main()
