"""`wk zed` -- reaching a workspace's checkout in Zed."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import REPO, WkTest, load_cmd, run

sys.path.insert(0, str(REPO / "lib"))
from wk import places, sshalias  # noqa: E402
from wk.machine import HAVE, Fake, Result  # noqa: E402




os.environ.setdefault("WK_ROOT", str(REPO))
ZED = load_cmd("zed")


class DriverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-zed-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"),
                    "WK_MACHINES_DIR": str(self.tmp / "hosts"), "WK_IN_VM": "1",
                    "WK_ZED_PUBKEY": "ssh-ed25519 AAAAstub test",
                    "PATH": os.environ.get("PATH", "")}
        (self.tmp / "home").mkdir()
        (self.tmp / "hosts").mkdir()
        self.fake = Fake("here")
        self.reg = places.Registry(REPO, env=self.env, machine=self.fake)

    def conf(self, name, text):
        kind = "kind=%s\n" % ("peer" if "peer=1" in text else "build")
        (self.tmp / "hosts" / (name + ".conf")).write_text(kind + text)


class TestContainerAlias(DriverTest):

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
        self.assertIn("ProxyCommand %s container demo" % os.path.join(str(REPO), "container", "ssh-transport"), text)
        self.assertIn("IdentityFile", text)
        self.assertIn(places.zed_key_path(self.env), text)

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

    def test_a_dry_prepare_on_a_machine_without_its_zed_key_prints_every_step(self):
        self.t.env.pop("WK_ZED_PUBKEY")
        before = (dict(self.fake.files), set(self.fake.dirs))
        with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}), mock.patch("sys.stderr") as err:
            self.t.ssh_prepare("demo")
        said = "".join(c.args[0] for c in err.write.call_args_list)
        self.assertIn("would run: ssh-keygen -q -t ed25519", said)
        self.assertIn("would run: podman exec -i --user dev wk-demo /bin/sh -c", said)
        self.assertIn(("write", sshalias.alias_path(self.env)), self.fake.effects)
        self.assertEqual(before, (self.fake.files, self.fake.dirs))


class TestToolsPlaceResolvedOnce(TestContainerAlias):

    def setUp(self):
        super().setUp()
        self.fake.dirs.add(self.reg.load("container").store.ws_dir("demo"))
        self.fake.answer(["podman", "exec", "wk-demo", "/opt/wk-tools/container/proxy/ensure-bridge.sh"], out="")

    def test_locate_is_called_once(self):
        calls = {"n": 0}
        real_locate = places.Registry.locate

        def counting_locate(reg_self, ws):
            calls["n"] += 1
            return real_locate(reg_self, ws)

        with mock.patch.object(places.Registry, "locate", counting_locate), \
                mock.patch.object(ZED.places, "Registry", return_value=self.reg), \
                mock.patch.object(ZED.places, "zed_cli", return_value="/usr/bin/zed"), \
                mock.patch.object(ZED, "emit") as emit:
            ZED.main(["--tools", "demo"])
        self.assertEqual(calls["n"], 1)
        emit.assert_called_once_with(False, "/usr/bin/zed", "ssh://wk-demo/opt/wk-tools")


class SshFake(Fake):

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

    def setUp(self):
        super().setUp()
        self.fake = SshFake()
        self.env["XDG_STATE_HOME"] = str(self.tmp / "state")
        del self.env["WK_IN_VM"]
        self.conf("peer", "host=peer.example\npeer=1\n")
        self.reg = places.Registry(REPO, env=self.env, machine=self.fake)
        self.t = self.reg.load("peer")
        self.fake.answer_remote("uname -s", out=PROBE)
        self.fake.answer_remote("zed demo --route", out="user=dev\nsrc=/src/WebKit\nproxy=/opt/wk-tools/container/ssh-transport demo\n")
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
        self.assertIn("ProxyCommand ssh peer.example /opt/wk-tools/container/ssh-transport demo", text)


class TestBrokenRefusesNamingTheRepair(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-zed-broken-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"),
                    "WK_MACHINES_DIR": str(self.tmp / "hosts"), "WK_IN_VM": "1",
                    "PATH": os.environ.get("PATH", "")}
        (self.tmp / "home").mkdir()
        (self.tmp / "hosts").mkdir()
        self.fake = Fake("here")
        self.reg = places.Registry(REPO, env=self.env, machine=self.fake)
        os.makedirs(os.path.join(self.env["WK_STORE"], "ws", "demo", "home"))
        self.fake.write(os.path.join(self.env["WK_STORE"], "ws", "demo", "home", places.READY_MARKER), "")

    def _resolve(self, name):
        tname = self.reg.ws_place(name)
        return self.reg.load(tname), tname

    def test_broken_reason_names_wk_rm_and_wk_new(self):
        # No podman answer: `podman inspect` comes back 127, read as absent.
        driver, _ = self._resolve("demo")
        self.assertEqual("broken", driver.state("demo"))
        reason = driver.broken_words("demo")
        self.assertIn("wk rm demo", reason)
        self.assertIn("wk new demo", reason)

    def test_a_present_workspace_is_not_broken(self):
        self.fake.answer(["podman", "inspect", "wk-fine", "--format", "{{.State.Status}}"], out="running\n")
        os.makedirs(os.path.join(self.env["WK_STORE"], "ws", "fine", "home"))
        self.fake.write(os.path.join(self.env["WK_STORE"], "ws", "fine", "home", places.READY_MARKER), "")
        with open(os.path.join(self.env["WK_STORE"], "ws", "fine", "base-id"), "w") as f:
            f.write("main-1\n")
        driver, _ = self._resolve("fine")
        self.assertNotEqual("broken", driver.state("fine"))


class TestZedRoute(WkTest):

    def test_route_needs_no_zed_on_path(self):
        cp = run("zed", "no-such-workspace-abcxyz", "--route", env={"PATH": "/usr/bin:/bin"})
        self.assertNotEqual(cp.returncode, 0)
        self.assertNotIn("zed is not installed", cp.stdout)

    def test_route_and_tools_together_are_refused(self):
        cp = run("zed", "demo", "--route", "--tools")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("--route describes a workspace", cp.stdout)


class TestZedCli(unittest.TestCase):

    def test_a_zed_on_path_wins(self):
        fake = Fake()
        fake.answer(HAVE + ("zed",))
        self.assertEqual(places.zed_cli(fake), "zed")

    def test_a_drag_installed_bundle_with_no_path_symlink_is_found(self):
        fake = Fake()
        fake.answer(["test", "-x", "/Applications/Zed.app/Contents/MacOS/cli"], rc=0)
        self.assertEqual(places.zed_cli(fake), "/Applications/Zed.app/Contents/MacOS/cli")

    def test_neither_is_not_installed(self):
        fake = Fake()
        fake.answer(["test", "-x", "/Applications/Zed.app/Contents/MacOS/cli"], rc=1)
        self.assertIsNone(places.zed_cli(fake))


if __name__ == "__main__":
    unittest.main()
