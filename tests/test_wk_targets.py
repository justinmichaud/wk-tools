"""lib/wk/targets.py: the registry over the conf files, and the container
and guest drivers over a fake machine -- what each answers for a workspace's
state from what podman and tart say and what the store holds.

Run: python3 tests/run.py -k tests.test_wk_targets
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import targets  # noqa: E402
from wk.machine import Fake  # noqa: E402

IS_MACOS = os.uname().sysname == "Darwin"


class TargetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-targets-"))
        self.registry_dir = self.tmp / "hosts"
        self.registry_dir.mkdir()
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"),
                    "WK_TARGET_REGISTRY": str(self.registry_dir), "WK_IN_VM": "1", "PATH": os.environ.get("PATH", "")}
        (self.tmp / "home").mkdir()
        self.fake = Fake("box")
        self.reg = targets.Registry(REPO, env=self.env, machine=self.fake)

    def tearDown(self):
        os.system("rm -rf %s" % self.tmp)

    def conf(self, name, text):
        (self.registry_dir / (name + ".conf")).write_text(text)


class TestRegistry(TargetsTest):
    def test_the_builtins_and_every_conf_are_targets(self):
        self.conf("box1", "WK_TARGET_KIND=remote\nWK_REMOTE_HOST=box1\n")
        self.conf("peer", "# a peer\nWK_REMOTE_PEER=1\n")
        self.assertEqual(self.reg.all(), ["container", "box1", "peer"])
        self.assertEqual(self.reg.machines(), ["box1", "peer"])
        self.assertEqual(self.reg.kind("peer"), "remote")
        self.assertIsNone(self.reg.kind("nosuch"))
        with self.assertRaises(LookupError) as cm:
            self.reg.load("nosuch")
        self.assertIn("The machines here: box1 peer", str(cm.exception))

    def test_a_conf_is_parsed_as_shell_assignments(self):
        p = self.tmp / "x.conf"
        p.write_text("# c\nA=1\nB=\"two words\"\nC='q'\nnot an assignment\n D = 4\n")
        self.assertEqual(targets.read_conf(str(p)), {"A": "1", "B": "two words", "C": "q", "D": "4"})

    def test_inside_a_workspace_the_default_is_local(self):
        marker = self.tmp / "marker"
        marker.write_text("name=ws\nsrc=/src/WebKit\narch=armhf\n")
        self.env["WK_MARKER"] = str(marker)
        self.assertEqual(self.reg.default(), "local")
        t = self.reg.load("local")
        self.assertEqual(t.list(), [("ws", "running")])
        self.assertEqual((t.info("ws"), t.info("other"), t.arch("ws")), ("running", "absent", "armhf"))

    def test_a_build_box_defaults_to_its_own_target(self):
        rm = self.tmp / "remote-marker"
        rm.write_text("target=buildbox\nroot=/home/u/wk\n")
        self.env["WK_REMOTE_MARKER"] = str(rm)
        self.assertEqual(self.reg.default(), "buildbox")
        self.assertIn("buildbox", self.reg.all())

    def test_vm_is_listed_only_on_a_macos_host_with_a_store_of_its_own(self):
        self.assertFalse(self.reg.vm_listed())   # WK_IN_VM
        env = dict(self.env)
        env.pop("WK_IN_VM")
        reg = targets.Registry(REPO, env=env, machine=self.fake)
        self.assertEqual(reg.vm_listed(), False)   # a scratch store is the container's
        env["WK_VM_STORE"] = str(self.tmp / "vmstore")
        reg = targets.Registry(REPO, env=env, machine=self.fake)
        self.assertEqual(reg.vm_listed(), IS_MACOS)


class TestContainer(TargetsTest):
    def setUp(self):
        super().setUp()
        self.t = self.reg.load("container")
        self.fake.answer(["podman", "ps"], out="wk-a\tUp 2 hours\nwk-b\tExited (0) 3 days ago\n")
        self.fake.answer(["podman", "inspect", "wk-a"], out="running\n")
        self.fake.answer(["podman", "inspect", "wk-b"], out="exited\n")
        self.fake.answer(["podman", "inspect", "wk-c"], rc=125, err="no such container")

    def test_list_strips_the_prefix(self):
        self.assertEqual(self.t.list(), [("a", "Up 2 hours"), ("b", "Exited (0) 3 days ago")])

    def test_info_is_podmans_word_once_the_marker_is_there(self):
        home = os.path.join(self.env["WK_STORE"], "ws", "a", "home")
        self.assertEqual(self.t.info("a"), "creating")
        self.fake.write(os.path.join(home, targets.READY_MARKER), "")
        self.assertEqual(self.t.info("a"), "running")
        self.assertEqual(self.t.info("c"), "absent")

    def test_state_reads_the_record_and_the_environment_together(self):
        ws = Path(self.env["WK_STORE"]) / "ws"
        self.assertEqual(self.t.state("c"), "absent")
        (ws / "c").mkdir(parents=True)
        self.assertEqual(self.t.state("c"), "creating")           # a directory, no marker, no container
        self.fake.write(str(ws / "c" / "home" / targets.READY_MARKER), "")
        self.assertEqual(self.t.state("c"), "broken")             # created, and the container is gone
        self.fake.write(str(ws / "a" / "home" / targets.READY_MARKER), "")
        (ws / "a").mkdir(parents=True, exist_ok=True)
        self.assertEqual(self.t.state("a"), "creating")           # no base-id yet
        (ws / "a" / "base-id").write_text("main-1\n")
        self.assertEqual(self.t.state("a"), "present")
        self.assertEqual(self.t.display_state("a"), "running")

    def test_exec_goes_through_the_sdk_and_the_bridge_without_a_tty(self):
        self.fake.answer([os.path.join(self.t.sdk(), "scripts", "host-only", "wkdev-enter")], out="ok\n")
        r = self.t.exec("a", ["git", "rev-parse", "HEAD"])
        self.assertEqual(r.out, "ok\n")
        argv = self.fake.effects[-1][1]
        self.assertIn("--no-tty", argv)
        self.assertEqual(argv[-3:], ("git", "rev-parse", "HEAD"))
        self.assertIn("ensure-bridge.sh", argv[argv.index("--") + 1])

    def test_start_and_stop_are_effects(self):
        self.fake.answer(["podman", "stop"], out="")
        self.assertTrue(self.t.stop("a"))
        self.assertEqual(self.fake.effects[-1][1][:3], ("podman", "stop", "--time"))
        os.environ["WK_DRY_RUN"] = "1"
        try:
            self.assertTrue(self.t.stop("a"))
            self.assertEqual(self.fake.effects[-1][1][:3], ("podman", "stop", "--time"))
        finally:
            os.environ.pop("WK_DRY_RUN", None)

    def test_the_arch_is_recorded_at_creation(self):
        self.assertEqual(self.t.arch("a"), "native")
        self.fake.write(os.path.join(self.env["WK_STORE"], "ws", "a", "arch"), "armhf\n")
        self.assertEqual(self.t.arch("a"), "armhf")


class TestVm(TargetsTest):
    def setUp(self):
        super().setUp()
        self.env.pop("WK_IN_VM")
        self.env["WK_VM_STORE"] = str(self.tmp / "vmstore")
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "tart").write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / "tart").chmod(0o755)
        self.env["PATH"] = "%s:%s" % (bin_dir, os.environ.get("PATH", ""))
        os.environ["PATH"] = self.env["PATH"]
        self.reg = targets.Registry(REPO, env=self.env, machine=self.fake)
        self.t = self.reg.load("vm")
        listing = [{"Name": "wk-base", "State": "stopped", "Source": "local"},
                   {"Name": "wk-mac", "State": "running", "Source": "local"},
                   {"Name": "ghcr.io/x", "State": "", "Source": "OCI"}]
        self.fake.answer([self.t.tart(), "list"], out=json.dumps(listing))
        self.fake.answer([self.t.tart(), "ip"], out="192.168.64.9\n")

    def test_list_leaves_out_the_base_and_the_oci_cache(self):
        self.assertEqual(self.t.list(), [("mac", "running")])

    def test_info_and_exec(self):
        self.assertEqual(self.t.info("mac"), "creating")
        self.fake.write(os.path.join(self.env["WK_VM_STORE"], "ws", "mac", targets.READY_MARKER), "")
        self.assertEqual(self.t.info("mac"), "running")
        self.assertEqual(self.t.info("gone"), "absent")
        self.fake.answer(["ssh"], out="hi\n")
        r = self.t.exec("mac", ["echo", "hi"])
        self.assertEqual(r.out, "hi\n")
        argv = self.fake.effects[-1][1]
        self.assertEqual(argv[0], "ssh")
        self.assertIn("admin@192.168.64.9", argv)
        self.assertTrue(argv[-1].startswith("bash -lc "))
        self.assertEqual(self.t.exec("gone", ["true"]).rc, 1)


if __name__ == "__main__":
    unittest.main()
