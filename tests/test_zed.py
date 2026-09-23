"""`wk zed` -- reaching a workspace's checkout in Zed.

`ssh_host` answers a target's `Host wk-<name>` alias or its direct address,
`ssh_prepare` is what writes the alias (an sshd installed and an identity
authorised for a container, `wk zed --route` asked of the peer itself for a
peer's), and `cmd/zed`'s own refusal for a broken workspace names the repair
rather than a driver's word. Nothing here starts a real container, guest or
machine: `targets.py`'s drivers run over a fake machine.

Run: python3 -m unittest tests.test_zed -v
"""
import importlib.machinery
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, run

sys.path.insert(0, str(REPO / "lib"))
from wk import sshalias, targets  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402


def _load_cmd_zed():
    """cmd/zed as a module, its own top-level `ROOT = ... or dirname(__file__)`
    never reached: WK_ROOT is already in the environment every test in this
    tree runs under (tests/support.py)."""
    path = str(REPO / "cmd" / "zed")
    loader = importlib.machinery.SourceFileLoader("cmd_zed", path)
    spec = importlib.util.spec_from_loader("cmd_zed", loader, origin=path)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = path
    loader.exec_module(mod)
    return mod


os.environ.setdefault("WK_ROOT", str(REPO))
ZED = _load_cmd_zed()


class DriverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-zed-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"),
                    "WK_TARGET_REGISTRY": str(self.tmp / "hosts"), "WK_IN_VM": "1",
                    "WK_ZED_PUBKEY": "ssh-ed25519 AAAAstub test",
                    "PATH": os.environ.get("PATH", "")}
        (self.tmp / "home").mkdir()
        (self.tmp / "hosts").mkdir()
        self.fake = Fake("here")
        self.reg = targets.Registry(REPO, env=self.env, machine=self.fake)

    def conf(self, name, text):
        (self.tmp / "hosts" / (name + ".conf")).write_text(text)


class TestContainerAlias(DriverTest):
    """A container workspace is reached through its generated `Host
    wk-<name>` alias: the default `ssh_host` (lib/target.sh's `wk-$1`) and
    `ssh_prepare` writing it, with a ProxyCommand into the podman transport."""

    def setUp(self):
        super().setUp()
        self.t = self.reg.load("container")
        self.fake.answer(["podman", "inspect", "wk-demo", "--format", "{{.Config.WorkingDir}}"], out="/home/dev\n")
        self.fake.answer(["podman", "exec", "wk-demo", "test", "-x", "/usr/sbin/sshd"], rc=0)
        self.fake.answer(["podman", "exec", "--user", "dev", "wk-demo", "/bin/sh"], out="")
        self.fake.answer(["podman", "exec", "--user", "dev", "wk-demo", "grep"], rc=1)
        self.fake.answer(["podman", "exec", "-i", "--user", "dev", "wk-demo", "/bin/sh"], out="")
        self.fake.answer(["chmod"], out="")

    def test_ssh_host_is_the_generated_alias(self):
        self.assertEqual(self.t.ssh_host("demo"), "wk-demo")

    def test_ssh_prepare_writes_a_proxycommand_alias(self):
        self.t.ssh_prepare("demo")
        text = self.fake.read(sshalias.alias_path(self.env))
        self.assertIn("Host wk-demo", text)
        self.assertIn("ProxyCommand %s demo" % os.path.join(str(REPO), "container", "ssh-transport.sh"), text)
        self.assertIn("IdentityFile", text)
        self.assertIn(targets.zed_key_path(self.env), text)

    def test_ssh_prepare_refuses_a_container_podman_does_not_know(self):
        self.fake.answer(["podman", "inspect", "wk-gone", "--format", "{{.Config.WorkingDir}}"], rc=125)
        from wk.act import Refused
        with self.assertRaises(Refused):
            self.t.ssh_prepare("gone")

    def test_no_ssh_installed_yet_is_installed_once(self):
        calls = {"n": 0}

        def sshd_test(argv, fake):
            calls["n"] += 1
            return Result(0 if calls["n"] > 1 else 1)
        self.fake.react(["podman", "exec", "wk-demo", "test", "-x", "/usr/sbin/sshd"], sshd_test)
        self.fake.answer(["podman", "exec", "wk-demo", "/opt/wk-tools/container/proxy/ensure-bridge.sh"], out="")
        self.t.ssh_prepare("demo")
        installed = [e for e in self.fake.effects if e[0] == "run" and any("ensure-bridge.sh" in a for a in e[1])]
        self.assertTrue(installed)


class SshFake(Fake):
    """This host, as a `Remote` driver over ssh sees it: every far-side call
    is one `ssh <opts> <dest> <command>` run here, answered by what the
    command contains -- for the probe script and for `t_wk zed ... --route`."""

    def __init__(self):
        super().__init__("host")
        self.remote = []

    def answer_remote(self, needle, rc=0, out="", err=""):
        self.remote.append((needle, Result(rc, out, err)))

    def run(self, argv, input=None, timeout=None):
        if argv and argv[0] == "ssh":
            for needle, r in reversed(self.remote):
                if needle in argv[-1]:
                    self.record_run(argv)
                    return r
        return super().run(argv, input=input, timeout=timeout)


PROBE = """/home/u
Linux
8
0.52 0.58 0.61 2/1234 56789
===MEM===
MemTotal:       32806140 kB
MemAvailable:   20480000 kB
===IONICE===
yes
"""


class TestPeerAlias(DriverTest):
    """A peer workstation's own workspace is reached through one more hop: the
    alias `ssh_prepare` writes carries a ProxyCommand that ssh's to the peer
    and runs the route `t_wk zed <name> --route` (its own answer) gave."""

    def setUp(self):
        super().setUp()
        self.fake = SshFake()
        self.env["XDG_STATE_HOME"] = str(self.tmp / "state")
        del self.env["WK_IN_VM"]
        self.conf("peer", "WK_REMOTE_HOST=peer.example\nWK_REMOTE_PEER=1\n")
        self.reg = targets.Registry(REPO, env=self.env, machine=self.fake)
        self.t = self.reg.load("peer")
        self.fake.answer_remote("uname -s", out=PROBE)
        self.fake.answer_remote("zed demo --route", out="user=dev\nsrc=/src/WebKit\nproxy=/opt/wk-tools/container/ssh-transport.sh demo\n")
        self.fake.answer(["chmod"], out="")

    def test_ssh_host_is_the_alias_for_a_named_workspace(self):
        self.assertEqual(self.t.ssh_host("demo"), "wk-demo")

    def test_ssh_host_of_the_machine_itself_is_its_direct_address(self):
        self.assertEqual(self.t.ssh_host(""), "peer.example")

    def test_ssh_prepare_writes_a_two_hop_proxycommand(self):
        self.t.ssh_prepare("demo")
        text = self.fake.read(sshalias.alias_path(self.env))
        self.assertIn("Host wk-demo", text)
        self.assertIn("HostName wk-demo.peer.invalid", text)
        self.assertIn("User dev", text)
        self.assertIn("ProxyCommand ssh peer.example /opt/wk-tools/container/ssh-transport.sh demo", text)


class TestBrokenRefusesNamingTheRepair(unittest.TestCase):
    """A workspace whose creation finished and whose environment is gone reads
    `broken`, and `wk zed` (like `wk new`) refuses it by name, naming `wk rm`
    rather than repeating the driver's own word for it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-zed-broken-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"),
                    "WK_TARGET_REGISTRY": str(self.tmp / "hosts"), "WK_IN_VM": "1",
                    "PATH": os.environ.get("PATH", "")}
        (self.tmp / "home").mkdir()
        (self.tmp / "hosts").mkdir()
        self.fake = Fake("here")
        self.reg = targets.Registry(REPO, env=self.env, machine=self.fake)
        # `Target.state` reads the workspace directory straight off disk
        # (owed: `unit killpoints[new]`), so the directory side of "broken"
        # is real while the container side stays on the fake.
        os.makedirs(os.path.join(self.env["WK_STORE"], "ws", "demo", "home"))
        self.fake.write(os.path.join(self.env["WK_STORE"], "ws", "demo", "home", targets.READY_MARKER), "")

    def _resolve(self, name):
        tname = self.reg.ws_target(name)
        return self.reg.load(tname), tname

    def test_broken_reason_names_wk_rm_and_wk_new(self):
        # No podman answer: `podman inspect` comes back 127, read as absent.
        target, tname = self._resolve("demo")
        reason = ZED.broken_reason(target, tname, "demo")
        self.assertIsNotNone(reason)
        self.assertIn("wk rm demo", reason)
        self.assertIn("wk new demo", reason)

    def test_a_present_workspace_is_not_broken(self):
        self.fake.answer(["podman", "inspect", "wk-fine", "--format", "{{.State.Status}}"], out="running\n")
        os.makedirs(os.path.join(self.env["WK_STORE"], "ws", "fine", "home"))
        self.fake.write(os.path.join(self.env["WK_STORE"], "ws", "fine", "home", targets.READY_MARKER), "")
        with open(os.path.join(self.env["WK_STORE"], "ws", "fine", "base-id"), "w") as f:
            f.write("main-1\n")
        target, tname = self._resolve("fine")
        self.assertIsNone(ZED.broken_reason(target, tname, "fine"))


class TestZedRoute(WkTest):
    """`wk zed <ws> --route` is what a peer's own `t_wk` calls to build the
    alias above; it never touches Zed itself (no `zed` on PATH is needed),
    and a workspace nothing here holds is refused like any other."""

    def test_route_needs_no_zed_on_path(self):
        cp = run("zed", "no-such-workspace-abcxyz", "--route", env={"PATH": "/usr/bin:/bin"})
        self.assertNotEqual(cp.returncode, 0)
        self.assertNotIn("zed is not installed", cp.stdout)

    def test_route_and_tools_together_are_refused(self):
        cp = run("zed", "demo", "--route", "--tools")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("--route describes a workspace", cp.stdout)


class TestDeclaration(WkTest):
    def test_the_listing_names_it(self):
        self.assertIn("zed <workspace>", run().stdout)

    def test_explain_answers_without_running_anything(self):
        cp = run("zed", "--explain")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("open a workspace's checkout", cp.stdout)

    def test_refused_inside_a_workspace(self):
        from tests.support import fake_workspace
        with fake_workspace() as ws:
            cp = ws.run("zed")
        self.assertNotEqual(cp.returncode, 0)


if __name__ == "__main__":
    unittest.main()
