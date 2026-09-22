"""lib/wk/targets.py: the registry over the conf files, and the container,
guest and remote drivers over a fake machine -- what each answers for a
workspace's state from what podman, tart and ssh say and what the store holds.

Run: python3 tests/run.py -k tests.test_wk_targets
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import record, targets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import TIMED_OUT, Fake, Result  # noqa: E402

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
    def host_registry(self):
        env = {k: v for k, v in self.env.items() if k != "WK_IN_VM"}
        return targets.Registry(REPO, env=env, machine=self.fake)

    def test_the_builtins_and_every_conf_are_targets(self):
        self.conf("box1", "WK_TARGET_KIND=remote\nWK_REMOTE_HOST=box1\n")
        self.conf("peer", "# a peer\nWK_REMOTE_PEER=1\n")
        reg = self.host_registry()
        self.assertEqual(reg.all(), ["container", "box1", "peer"])
        self.assertEqual(reg.machines(), ["box1", "peer"])
        self.assertEqual(reg.kind("peer"), "remote")
        self.assertIsNone(reg.kind("nosuch"))
        with self.assertRaises(LookupError) as cm:
            reg.load("nosuch")
        self.assertIn("The machines here: box1 peer", str(cm.exception))

    def test_a_conf_named_after_this_machine_is_not_a_peer(self):
        me = record.machine_name({})
        self.conf(me.upper(), "WK_REMOTE_HOST=%s\n" % me)
        self.conf("box1", "WK_REMOTE_HOST=box1\n")
        self.assertEqual(self.host_registry().machines(), ["box1"])

    def test_the_far_end_of_a_target_lists_no_conf(self):
        self.conf("box1", "WK_REMOTE_HOST=box1\n")
        self.assertEqual(self.reg.all(), ["container"])   # WK_IN_VM
        env = {k: v for k, v in self.env.items() if k != "WK_IN_VM"}
        env["WK_REMOTE_MARKER"] = str(self.tmp / "wk-remote")
        (self.tmp / "wk-remote").write_text("target=box2\nroot=/home/u/wk\n")
        self.assertEqual(targets.Registry(REPO, env=env, machine=self.fake).all(), ["container", "box2"])

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


LINUX_PROBE = """/home/u
Linux
8
0.52 0.58 0.61 2/1234 56789
===MEM===
MemTotal:       32806140 kB
MemAvailable:   20480000 kB
===IONICE===
yes
"""


class SshFake(Fake):
    """This host, as the remote driver sees it: every far-side call is one `ssh <opts> <dest> <command>` run here,
    answered by what the command contains."""

    def __init__(self):
        super().__init__("host")
        self.remote = []

    def answer_remote(self, needle, rc=0, out="", err=""):
        self.remote.append((needle, Result(rc, out, err)))

    def run(self, argv, input=None, timeout=None):
        if argv[0] == "ssh":
            for needle, r in self.remote:
                if needle in argv[-1]:
                    self.effects.append(("run", tuple(argv)))
                    return r
        return super().run(argv, input=input, timeout=timeout)

    def ssh_calls(self, needle=""):
        return [e[1] for e in self.effects if e[0] == "run" and e[1][0] == "ssh" and needle in e[1][-1]]


class TestRemote(TargetsTest):
    def setUp(self):
        super().setUp()
        self.fake = SshFake()
        self.env.update({"XDG_STATE_HOME": str(self.tmp / "state"), "WK_PROBE_SECONDS": "2", "WK_SSH_TIMEOUT": "3"})
        del self.env["WK_IN_VM"]   # a host with machines in its registry
        self.conf("box", "WK_REMOTE_HOST=box.example\nWK_REMOTE_ROOT=/home/u/wk\n")
        self.reg = targets.Registry(REPO, env=self.env, machine=self.fake)
        self.t = self.reg.load("box")
        self.fake.answer_remote("uname -s", out=LINUX_PROBE)
        self.fake.answer_remote("test -f $HOME/.wk-remote", rc=0)

    def test_the_probe_is_one_round_trip_and_memoised(self):
        self.assertEqual(self.t.probe(), ("answering", ""))
        self.assertEqual((self.t.home(), self.t.os(), self.t.cores(), self.t.load(), self.t.mem_mb()), ("/home/u", "linux", 8, 0, 20000))
        self.assertEqual((self.t.tools(""), self.t.src("a")), ("/home/u/wk/tools", "/home/u/wk/ws/a/WebKit"))
        self.assertEqual(self.t.answers(), (True, ""))
        self.assertTrue(self.t.has_wk() and self.t.delegates())
        self.assertEqual(len(self.fake.ssh_calls("uname -s")), 1)
        self.assertEqual(len(self.fake.ssh_calls()), 2)   # the probe and the one `test` for a wk there
        argv = self.fake.ssh_calls("uname -s")[0]
        self.assertIn(targets.PROBE_SCRIPT, argv[-1])

    def test_the_root_defaults_to_wk_under_the_far_home(self):
        self.conf("bare", "WK_REMOTE_HOST=bare.example\n")
        t = self.reg.load("bare")
        self.assertEqual((t.tools(""), t.src("a")), ("/home/u/wk/tools", "/home/u/wk/ws/a/WebKit"))
        self.conf("tools", "WK_REMOTE_HOST=bare.example\nWK_REMOTE_TOOLS=my/tools\n")
        self.assertEqual(self.reg.load("tools").tools(""), "/home/u/my/tools")

    def test_a_darwin_machine_is_read_from_sysctl_and_vm_stat(self):
        self.fake.remote = []
        self.fake.answer_remote("uname -s", out="/Users/u\nDarwin\n10\n{ 1.23 1.87 2.01 }\n===MEM===\n"
                                "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free:      123456.\n"
                                "Pages inactive:  345678.\nPages speculative: 45678.\n===IONICE===\nno\n")
        self.assertEqual((self.t.os(), self.t.cores(), self.t.load(), self.t.mem_mb()), ("macos", 10, 1, 8043))

    def test_an_unreachable_machine_is_named_with_why(self):
        self.fake.remote = []
        self.fake.answer_remote("uname -s", rc=255, err="ssh: Could not resolve hostname box.example: nodename nor servname provided, or not known\n\n")
        self.assertEqual(self.t.probe(), ("unreachable", "Could not resolve hostname box.example: nodename nor servname provided, or not known"))
        self.assertEqual((self.t.far_side(), self.t.info("a"), self.t.list(), self.t.has_wk()), ("unreachable", "unreachable", [], False))
        self.assertEqual(len(self.fake.ssh_calls()), 1)
        for rc, err, why in ((255, "", "ssh exited 255 and said nothing"), (TIMED_OUT, "", "timed out after 2s"),
                             (255, "kex_exchange_identification: Connection closed by remote host\n", "Connection closed by remote host")):
            t = self.reg.load("box")
            self.fake.remote = [("uname -s", Result(rc, "", err))]
            self.assertEqual(t.probe(), ("unreachable", why))
        with self.assertRaises(Refused):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                t.home()
        self.assertIn("cannot reach 'box.example' over ssh: Connection closed by remote host", err.getvalue())

    def test_far_side_for_a_machine_with_no_wk_and_for_this_machine(self):
        self.fake.remote = [("uname -s", Result(0, LINUX_PROBE)), ("test -f $HOME/.wk-remote", Result(1))]
        self.assertEqual((self.t.far_side(), self.t.probe(), self.t.delegates()), ("no-wk", ("no-wk", ""), False))
        self.conf("me", "WK_REMOTE_LOCAL=1\nWK_REMOTE_ROOT=%s\n" % (self.tmp / "rr"))
        me = self.reg.load("me")
        self.assertTrue(me.is_here() and me.needs_base)
        self.assertEqual((me.far_side(), me.probe(), me.answers(), me.has_wk(), me.delegates()), ("none", ("none", ""), (True, ""), False, False))
        self.assertEqual(me.store.root(), str(self.tmp / "rr"))
        self.assertEqual(self.fake.ssh_calls(), self.fake.ssh_calls("uname -s") + self.fake.ssh_calls("test -f"))

    def test_list_info_and_created(self):
        self.fake.answer_remote("ls -1A /home/u/wk/ws", out="a\nb\n.stray\n")
        self.fake.answer_remote("ws/a ]", out="present\n")
        self.fake.answer_remote("ws/b ]", out="creating\n")
        self.fake.answer_remote("ws/c ]", out="absent\n")
        self.assertEqual(self.t.list(), [("a", "present"), ("b", "present")])
        self.assertEqual((self.t.info("a"), self.t.info("b"), self.t.info("c")), ("present", "creating", "absent"))
        self.assertEqual((self.t.created("a"), self.t.created("b")), (True, False))
        self.assertEqual(len(self.fake.ssh_calls("ws/a ]")), 2)   # info is one round trip, and asked each time

    def test_exec_builds_the_ssh_command_line_with_the_drivers_options(self):
        self.fake.answer_remote("git status", out="clean\n")
        self.assertEqual(self.t.exec("a", ["git", "status"]).out, "clean\n")
        argv = self.fake.ssh_calls("git status")[0]
        opts = " ".join(argv)
        for o in ("BatchMode=yes", "ConnectTimeout=3", "ServerAliveInterval=15", "ServerAliveCountMax=4",
                  "ControlMaster=auto", "ControlPath=%s/wk/ssh/%%h-%%p-%%r" % self.env["XDG_STATE_HOME"], "ControlPersist=60"):
            self.assertIn(o, opts)
        self.assertEqual(argv[-2], "box.example")
        self.assertIn("cd /home/u/wk/ws/a/WebKit && git status", argv[-1])

    def test_wk_is_the_far_machines_own_with_the_flags_as_environment(self):
        self.fake.answer_remote("tools/wk", out="==> no task is running on box\n")
        rc, out = self.t.wk("stop", "--tasks", env={"WK_YES": "1", "WK_ROW_LABEL": "box", "WK_NO_DELEGATE": "1", "HOME": "/x"})
        self.assertEqual((rc, out), (0, "==> no task is running on box\n"))
        cmd = self.fake.ssh_calls("tools/wk")[0][-1]
        self.assertIn("cd $HOME && WK_YES=1 WK_ROW_LABEL=box WK_NO_DELEGATE=1 /home/u/wk/tools/wk stop --tasks 2>&1", cmd)
        self.t.wk("version", env={}, quiet=True)
        self.assertNotIn("2>&1", self.fake.ssh_calls("tools/wk")[1][-1])

    def test_a_peer_is_asked_not_driven(self):
        self.conf("peer", "WK_REMOTE_PEER=1\nWK_REMOTE_TOOLS=/opt/wk-tools\n")
        self.fake.answer_remote("test -x", rc=0)
        self.fake.answer_remote("wk ls --json", out=json.dumps({"workspaces": [{"name": "pw", "state": "running"}, {"name": "half", "state": "creating"}]}))
        t = self.reg.load("peer")
        self.assertEqual((t.far_side(), t.delegates()), ("answering", True))
        self.assertEqual(t.list(), [("pw", "running"), ("half", "creating")])
        self.assertEqual((t.info("pw"), t.info("half"), t.info("ghost")), ("present", "creating", "absent"))
        self.assertEqual(len(self.fake.ssh_calls("wk ls --json")), 1)
        self.assertIn("WK_NO_DELEGATE=1 /opt/wk-tools/wk ls --json", self.fake.ssh_calls("wk ls --json")[0][-1])
        self.assertTrue(all("test -f $HOME/.wk-remote" not in c[-1] for c in self.fake.ssh_calls()))

    def test_locating_a_workspace_names_a_machine_that_did_not_answer(self):
        self.fake.remote = [("uname -s", Result(255, "", "ssh: Connection refused\n"))]
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.reg.locate("ws"), [])
        self.assertIn("could not ask box over ssh: Connection refused -- what is there is not in this answer", err.getvalue())
        self.assertEqual(len(self.fake.ssh_calls()), 1)
        self.fake.remote = [("uname -s", Result(0, LINUX_PROBE)), ("ws/ws ]", Result(0, "present\n"))]
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.reg.ws_target("ws"), "box")
        self.assertEqual(err.getvalue(), "")

    def test_a_target_with_no_host_refuses_and_names_the_conf(self):
        t = self.reg.load("remote")
        with self.assertRaises(Refused):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                t.probe()
        self.assertIn("target 'remote' has no host to reach", err.getvalue())
        self.assertIn(self.reg.conf_path("remote"), err.getvalue())
        self.assertEqual(self.fake.ssh_calls(), [])

    def test_start_and_stop_have_nothing_to_act_on(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertTrue(self.t.start("a"))
            self.assertFalse(self.t.stop("a"))
        self.assertIn("'box' has no notion of starting a single workspace -- nothing to bring up for 'a'", err.getvalue())
        self.assertIn("the 'box' target has no notion of stopping a single workspace -- 'a' is left running", err.getvalue())
        self.assertEqual(self.fake.effects, [])


if __name__ == "__main__":
    unittest.main()
