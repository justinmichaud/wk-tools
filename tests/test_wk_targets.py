"""lib/wk/targets.py: the registry over the conf files, and the container,
guest, workspace-local and remote drivers over a fake machine -- what each
answers for a workspace's state from what podman, tart and ssh say and what
the store holds, and what each does to create and destroy one.

Run: python3 tests/run.py -k tests.test_wk_targets
"""
import contextlib
import inspect
import io
import json
import os
import re
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import buildconf, git, record, secrets, shell, targets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import TIMED_OUT, Fake, Result  # noqa: E402
from wk.store import Store  # noqa: E402
from wk.sysimage import guestbase  # noqa: E402

IS_MACOS = os.uname().sysname == "Darwin"
WRITE_INTERFACE = ("store_init", "create", "ready", "destroy", "sdk_refresh", "create_log")
SHARED = ("ready", "sdk_refresh", "create_log")
LINUX_MEMINFO = "MemTotal:       32806140 kB\nMemFree:         1000000 kB\nMemAvailable:   20480000 kB\n"


class TargetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-targets-"))
        self.registry_dir = self.tmp / "hosts"
        self.registry_dir.mkdir()
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"),
                    "WK_MACHINES_DIR": str(self.registry_dir), "WK_IN_VM": "1", "PATH": os.environ.get("PATH", "")}
        (self.tmp / "home").mkdir()
        self.fake = Fake("here")
        self.reg = targets.Registry(REPO, env=self.env, machine=self.fake)

    def tearDown(self):
        os.system("rm -rf %s" % self.tmp)

    def conf(self, name, text):
        kind = "" if "KIND=" in text.replace("WK_TARGET_KIND=", "") else \
            "KIND=%s\n" % ("peer" if "WK_REMOTE_PEER=1" in text else "build")
        (self.registry_dir / (name + ".conf")).write_text(kind + text)

    def stderr_of(self, fn):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            result = fn()
        return result, err.getvalue()

    def refused(self, fn):
        with self.assertRaises(Refused):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                fn()
        return err.getvalue()

    def dry_run(self):
        p = mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"})
        p.start()
        self.addCleanup(p.stop)


class TestSessionSocket(unittest.TestCase):
    """The one path and presence check `cmd/enter` and `cmd/gui` share for the
    benchmark session's Wayland socket."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-session-socket-"))
        self.addCleanup(lambda: os.system("rm -rf %s" % self.tmp))
        self.env = {"XDG_RUNTIME_DIR": str(self.tmp)}

    def sock_path(self):
        return Path(targets.session_socket_path(self.env))

    def test_the_path_is_under_the_runtime_dir(self):
        self.assertEqual(self.sock_path(), self.tmp / "wk" / "display" / "wayland-0")

    def test_absent_is_not_present(self):
        self.assertFalse(targets.session_socket_present(self.env))

    def test_a_real_socket_is_present(self):
        import socket
        self.sock_path().parent.mkdir(parents=True)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(s.close)
        s.bind(str(self.sock_path()))
        self.assertTrue(targets.session_socket_present(self.env))

    def test_a_plain_file_of_the_same_name_is_not_present(self):
        self.sock_path().parent.mkdir(parents=True)
        self.sock_path().write_text("")
        self.assertFalse(targets.session_socket_present(self.env))

    def test_no_xdg_runtime_dir_falls_back_to_run_user_uid(self):
        self.assertEqual(targets.session_socket_path({}),
                         os.path.join("/run/user/%d" % os.getuid(), "wk", "display", "wayland-0"))


class TestRegistry(TargetsTest):
    def host_registry(self):
        env = {k: v for k, v in self.env.items() if k != "WK_IN_VM"}
        return targets.Registry(REPO, env=env, machine=self.fake)

    def test_the_builtins_and_every_conf_are_targets(self):
        self.conf("box1", "KIND=build\nWK_TARGET_KIND=remote\nWK_REMOTE_HOST=box1\n")
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
        self.conf("box2", "WK_REMOTE_HOSTNAME=box2-host\n")
        self.fake.answer(["hostname", "-s"], out="box2-host\n")
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
        self.conf("buildbox", "WK_REMOTE_HOST=buildbox\n")
        self.fake.answer(["hostname", "-s"], out="buildbox\n")
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
        ws = os.path.join(self.env["WK_STORE"], "ws")
        self.assertEqual(self.t.state("c"), "absent")
        self.fake.mkdir(os.path.join(ws, "c"))
        self.assertEqual(self.t.state("c"), "creating")           # a directory, no marker, no container
        self.fake.write(os.path.join(ws, "c", "home", targets.READY_MARKER), "")
        self.assertEqual(self.t.state("c"), "broken")             # created, and the container is gone
        self.assertEqual(self.t.state("a"), "broken")             # a container with no directory, nothing creating it
        self.fake.write(os.path.join(ws, "a", "home", targets.READY_MARKER), "")
        self.assertEqual(self.t.state("a"), "creating")           # no base-id yet
        self.fake.write(os.path.join(ws, "a", "base-id"), "main-1\n")
        self.assertEqual(self.t.state("a"), "present")
        self.assertEqual(self.t.display_state("a"), "running")

    def test_a_workspace_exists_by_its_directory_its_environment_or_a_creation(self):
        self.assertFalse(self.reg.exists_on(self.t, "c"))
        self.assertTrue(self.reg.exists_on(self.t, "a"))          # podman knows it, whatever the store holds
        self.fake.mkdir(os.path.join(self.env["WK_STORE"], "ws", "c"))
        self.assertTrue(self.reg.exists_on(self.t, "c"))

    def test_exec_goes_through_the_sdk_and_the_bridge_without_a_tty(self):
        """wkdev-enter refuses to run without WKDEV_SDK in its environment, so the argv carries it."""
        self.fake.answer(["env"], out="ok\n")
        r = self.t.exec("a", ["git", "rev-parse", "HEAD"])
        self.assertEqual(r.out, "ok\n")
        argv = self.fake.effects[-1][1]
        self.assertEqual(argv[0], "env")
        self.assertIn("WKDEV_SDK=%s" % self.t.sdk(), argv)
        self.assertIn(os.path.join(self.t.sdk(), "scripts", "host-only", "wkdev-enter"), argv)
        self.assertIn("--no-tty", argv)
        self.assertEqual(argv[-3:], ("git", "rev-parse", "HEAD"))
        self.assertIn("ensure-bridge.sh", argv[argv.index("--") + 1])

    def test_wk_is_the_podman_vms_own_wk_over_machine_ssh(self):
        """A workstation's `wk status` asks the VM's wk for its container workspaces; the answer is what it says."""
        self.fake.answer(["podman", "machine", "ssh", "wk", "--"], out='{"kind": "workspace"}\n')
        env = dict(self.env, WK_ROW_LABEL="tolken", WK_NO_DELEGATE="1")
        env.pop("WK_IN_VM")
        self.assertEqual(self.t.wk("status", "--records", "ws", env=env, quiet=True), (0, '{"kind": "workspace"}\n'))
        words = shlex.split(self.fake.effects[-1][1][-1])
        self.assertLessEqual({"WK_IN_VM=1", "WK_HOST_SELF=1", "WK_ROW_LABEL=tolken", "WK_NO_DELEGATE=1"}, set(words))
        self.assertEqual(words[-4:], ["/opt/wk-tools/wk", "status", "--records", "ws"])
        self.t.wk("version", env=env)
        self.assertTrue(self.fake.effects[-1][1][-1].endswith(" 2>&1"))

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

    def test_exec_tty_carries_a_tty_through_the_sdk_and_the_bridge(self):
        self.fake.answer(["env"], out="ok\n")
        r = self.t.exec_tty("a", ["lldb", "-o", "attach"])
        self.assertEqual(r.rc, 0)
        kind, argv, cwd = self.fake.effects[-1]
        self.assertEqual(kind, "run_tty")
        self.assertNotIn("--no-tty", argv)
        self.assertEqual(argv[-3:], ("lldb", "-o", "attach"))
        self.assertIsNone(cwd)

    def test_the_arch_is_recorded_at_creation(self):
        self.assertEqual(self.t.arch("a"), "native")
        self.fake.write(os.path.join(self.env["WK_STORE"], "ws", "a", "arch"), "armhf\n")
        self.assertEqual(self.t.arch("a"), "armhf")

    def test_sdk_local_is_none_with_no_image_pulled(self):
        self.fake.answer(["podman", "images"], out="docker.io/library/busybox:latest\n")
        self.assertIsNone(self.t.sdk_local())

    def test_sdk_local_reads_the_pulled_image_and_its_pull_date(self):
        self.fake.answer(["podman", "images"], out="ghcr.io/igalia/wkdev-sdk:2.53-v9-abc0000\n")
        self.fake.answer(["podman", "image", "inspect"], out="2026-08-01T12:00:00Z\n")
        self.assertEqual(self.t.sdk_local(), {"image": "ghcr.io/igalia/wkdev-sdk:2.53-v9-abc0000", "created": "2026-08-01"})

    def test_sdk_upstream_is_the_registrys_tags_past_the_header_row(self):
        self.fake.answer(["podman", "search", "--list-tags"], out="NAME\tTAG\nghcr.io/igalia/wkdev-sdk\t2.53-v9-abc0000\n"
                                                                    "ghcr.io/igalia/wkdev-sdk\t24.04_arm32\n")
        self.assertEqual(self.t.sdk_upstream(), ["2.53-v9-abc0000", "24.04_arm32"])

    def test_sdk_upstream_is_none_when_the_registry_does_not_answer(self):
        self.fake.answer(["podman", "search", "--list-tags"], rc=1, err="timed out")
        self.assertIsNone(self.t.sdk_upstream(timeout=4))


class VmTest(TargetsTest):
    def setUp(self):
        super().setUp()
        self.env.pop("WK_IN_VM")
        self.env["WK_VM_STORE"] = str(self.tmp / "vmstore")
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "tart").write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / "tart").chmod(0o755)
        self.env["PATH"] = "%s:%s" % (bin_dir, os.environ.get("PATH", ""))
        path = mock.patch.dict(os.environ, {"PATH": self.env["PATH"]})
        path.start()
        self.addCleanup(path.stop)
        # A guest is driven from a macOS host; the store rule that says so is held by test_wk_store.
        mac = mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=True)
        mac.start()
        self.addCleanup(mac.stop)
        self.reg = targets.Registry(REPO, env=self.env, machine=self.fake)
        self.t = self.reg.load("vm")
        listing = [{"Name": "wk-base", "State": "stopped", "Source": "local"},
                   {"Name": "wk-mac", "State": "running", "Source": "local"},
                   {"Name": "ghcr.io/x", "State": "", "Source": "OCI"}]
        self.fake.answer([self.t.tart(), "list"], out=json.dumps(listing))
        self.fake.answer([self.t.tart(), "ip"], out="192.168.64.9\n")


class TestVm(VmTest):
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

    def test_exec_tty_allocates_a_pty_over_ssh_to_the_guest(self):
        self.fake.answer(["ssh"], out="ignored\n")
        r = self.t.exec_tty("mac", ["lldb", "-o", "attach"])
        self.assertEqual(r.rc, 0)
        kind, argv, cwd = self.fake.effects[-1]
        self.assertEqual(kind, "run_tty")
        self.assertEqual(argv[0], "ssh")
        self.assertIn("-t", argv)
        self.assertIn("admin@192.168.64.9", argv)
        self.assertTrue(argv[-1].startswith("bash -lc "), argv[-1])
        self.assertIsNone(cwd)


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
            for needle, r in reversed(self.remote):
                if needle in argv[-1]:
                    self.record_run(argv)
                    return r
        return super().run(argv, input=input, timeout=timeout)

    def ssh_calls(self, needle=""):
        return [e[1] for e in self.effects if e[0] == "run" and e[1][0] == "ssh" and needle in e[1][-1]]


class RemoteTest(TargetsTest):
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


class TestRemote(RemoteTest):
    def test_a_machine_that_did_not_answer_is_no_absence(self):
        self.fake.answer_remote("uname -s", rc=255, err="ssh: connect to host box.example port 22: Operation timed out")
        self.assertTrue(self.reg.exists_on(self.reg.load("box"), "ws"))

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

    def test_exec_tty_over_ssh_runs_through_here_not_the_ssh_machine(self):
        """`exec_argv` already resolves to a literal `ssh ...` invocation; exec_tty
        has to run it with `self.here` (the plain local machine), not `self.machine`
        (an `Ssh` onto the same host for every other call), or it would ssh twice."""
        self.fake.answer(["ssh"], out="ignored\n")
        r = self.t.exec_tty("a", ["lldb", "-o", "attach"])
        self.assertEqual(r.rc, 0)
        kind, argv, cwd = self.fake.effects[-1]
        self.assertEqual(kind, "run_tty")
        self.assertEqual(argv[0], "ssh")
        self.assertIn("-t", argv)
        self.assertEqual(argv[-2], "box.example")
        self.assertIn("cd /home/u/wk/ws/a/WebKit && lldb -o attach", argv[-1])
        self.assertIsNone(cwd)

    def test_exec_tty_when_local_runs_directly_with_the_source_as_cwd(self):
        self.conf("me", "WK_REMOTE_LOCAL=1\nWK_REMOTE_ROOT=%s\n" % (self.tmp / "rr"))
        me = self.reg.load("me")
        self.fake.answer(["sh"], out=LINUX_PROBE)   # is_local still probes itself, over a plain shell, not ssh
        self.fake.answer(["true"], out="")
        r = me.exec_tty("a", ["true"])
        self.assertEqual(r.rc, 0)
        self.assertEqual(self.fake.effects[-1], ("run_tty", ("true",), me.src("a")))

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
        line = shlex.split(self.fake.ssh_calls("tools/wk")[0][-1])[-1]
        self.assertTrue(line.startswith("cd $HOME && "), line)
        self.assertLessEqual({"WK_YES=1", "WK_ROW_LABEL=box", "WK_NO_DELEGATE=1"}, set(line.split()))
        self.assertTrue(line.endswith(" /home/u/wk/tools/wk stop --tasks 2>&1"), line)
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


class MarkerClock(FakeClock):
    """A clock whose n-th sleep is when the workspace's marker appears."""

    def __init__(self, fake, path, after):
        super().__init__()
        self.fake, self.path, self.after = fake, path, after

    def sleep(self, seconds):
        super().sleep(seconds)
        if len(self.slept) == self.after:
            self.fake.files[self.path] = ""


class TestWriteInterface(unittest.TestCase):
    def test_every_driver_implements_the_whole_write_side(self):
        for cls in (targets.Container, targets.Vm, targets.LocalWorkspace, targets.Remote):
            for name in WRITE_INTERFACE:
                with self.subTest(cls=cls.__name__, method=name):
                    own, base = getattr(cls, name), getattr(targets.Target, name)
                    if name in SHARED:
                        self.assertNotIn("NotImplementedError", inspect.getsource(base))
                    else:
                        self.assertIsNot(own, base, "%s inherits %s unimplemented" % (cls.__name__, name))


class TestContainerWrite(TargetsTest):
    def setUp(self):
        super().setUp()
        self.t = self.reg.load("container")
        self.base = "main-1"
        self.fake.answer(["nproc"], out="8\n")
        self.fake.files["/proc/meminfo"] = LINUX_MEMINFO
        self.fake.dirs.add(self.t.store.base_path(self.base))
        self.fake.answer(["podman", "container", "exists"], rc=1)
        self.fake.answer(["podman", "container", "exists", "wk-a"], rc=0)
        self.fake.answer(shell.argv(self.t.root, shell.GPU_FLAGS_FN)[:3], out="--device /dev/dri")
        self.fake.answer(["env"], out="")
        self.fake.answer(["install"], out="")

    def wkdev_create(self):
        return next(e[1] for e in self.fake.effects if e[0] == "run" and any(a.endswith("wkdev-create") for a in e[1]))

    def flag_pairs(self, argv):
        flags = argv[argv.index("--additional-flags") + 1].split()
        return list(zip(flags[0::2], flags[1::2]))

    def expected_flag_pairs(self, ws, arch, mem, cpus, gpu):
        """What wkdev-create is handed, in order: the mounts, the caches, the envelope, the ccache and yocto
        environment, then the sandbox's proxy, and the GPU last."""
        st, ws_dir, mirror = self.t.store, self.t.store.ws_dir(ws), self.t.store.mirror()
        root, proxy = st.root(), "http://127.0.0.1:3128"
        pairs = [("--volume", "%s:/opt/wk-tools:ro" % self.t.tools_src()),
                 ("--volume", "%s:%s:ro" % (os.path.dirname(mirror), os.path.dirname(mirror))), ("--env", "WK_MIRROR=%s" % mirror),
                 ("--volume", "%s:/src/WebKit:O,upperdir=%s/changes,workdir=%s/overlay-work" % (st.base_path(self.base), ws_dir, ws_dir)),
                 ("--volume", "%s/build:/src/WebKit/WebKitBuild" % ws_dir), ("--volume", "%s:/var/lib/wk/ws/%s" % (ws_dir, ws))]
        pairs += [("--volume", "%s/%s:%s" % (root, sub, dest)) for sub, dest in (
            ("cache/ccache", "/ccache"), ("cache/yocto", "/cache/yocto"), ("cache/buildroot", "/cache/buildroot"),
            ("cache/bench", "/cache/bench"), ("bench", "/bench"), ("skills", "/skills"))]
        pairs += [("--volume", "%s:/secrets:ro" % st.secrets_view_dir("container")), ("--volume", "%s/agent-rw:/agent-rw" % root),
                  ("--memory", "%dm" % mem), ("--cpus", str(cpus))]
        pairs += [("--env", kv) for kv in (
            "CCACHE_DIR=/ccache", "CCACHE_MAXSIZE=40G", "CCACHE_BASEDIR=/src/WebKit",
            "CCACHE_SLOPPINESS=pch_defines,time_macros,include_file_mtime,include_file_ctime", "CCACHE_PCH_EXTSUM=true",
            "CCACHE_DEPEND=true", "CCACHE_NOHASHDIR=true", "DL_DIR=/cache/yocto/downloads", "SSTATE_DIR=/cache/yocto/sstate",
            "BR2_DL_DIR=/cache/buildroot/dl", "BR2_CCACHE_DIR=/cache/buildroot/ccache", "WK_WORKSPACE=%s" % ws, "WK_ARCH=%s" % arch,
            "WKDEV_OFFLINE=1", "WK_LOCAL_STORE=/var/lib/wk")]
        pairs += [("--volume", "%s:/run/wk" % self.t.runtime_dir()), ("--env", "WK_PROXY_SOCKET=/run/wk/proxy.sock")]
        pairs += [("--env", "%s=%s" % (v, proxy)) for v in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")]
        pairs += [("--env", "%s=localhost,127.0.0.1,::1" % v) for v in ("no_proxy", "NO_PROXY")]
        return pairs + [("--env", "WAYLAND_DISPLAY=/run/wk/display/wayland-0")] + gpu

    def test_create_hands_wkdev_create_every_flag_a_workspace_needs(self):
        _, err = self.stderr_of(lambda: self.t.create("new", self.base))
        argv = self.wkdev_create()
        self.assertEqual(self.flag_pairs(argv), self.expected_flag_pairs("new", "native", 19749, 7, [("--device", "/dev/dri")]))
        u, ws_dir = self.t.user(), self.t.store.ws_dir("new")
        head = argv[argv.index("--network"):argv.index("--additional-flags")]
        self.assertEqual(head, ("--network", "none", "--isolated", "--name", "wk-new", "--shell", "/bin/bash", "--user", u, "--group", u,
                                "--home", os.path.join(ws_dir, "home")))
        self.assertEqual(argv[0], "env")
        self.assertIn("WKDEV_SDK=%s" % self.t.sdk(), argv)
        self.assertIn("creating workspace 'new' from base main-1 (rootless-proxy, native)", err)
        for d in ("changes", "overlay-work", "home", "build"):
            self.assertIn(os.path.join(ws_dir, d), self.fake.dirs)
        self.assertEqual(self.fake.files[os.path.join(ws_dir, "arch")], "native\n")
        install = next(e[1] for e in self.fake.effects if e[0] == "run" and e[1][0] == "install")
        self.assertEqual(install, ("install", "-m", "0755", os.path.join(self.t.root, "container", "firstrun.sh"), os.path.join(ws_dir, "home", ".wkdev-firstrun")))
        self.assertEqual(self.fake.effects[-1], ("write", os.path.join(ws_dir, "base-id")))
        self.assertEqual(self.fake.files[os.path.join(ws_dir, "base-id")], "main-1\n")

    def test_an_armhf_workspace_names_the_arm_image_and_gets_no_gpu(self):
        self.stderr_of(lambda: self.t.create("new", self.base, "armhf"))
        argv = self.wkdev_create()
        image = buildconf.IMAGE_ARMHF
        self.assertEqual(argv[argv.index("--arch"):argv.index("--name")], ("--arch", "arm", "--image", image))
        self.assertNotIn(("--device", "/dev/dri"), self.flag_pairs(argv))
        self.assertIn(("--env", "WK_ARCH=armhf"), self.flag_pairs(argv))
        self.env["WK_SDK_IMAGE"] = "ghcr.io/x/sdk:tag"
        self.fake.effects = []
        _, err = self.stderr_of(lambda: self.reg.load("container").create("other", self.base))
        self.assertIn("--image", self.wkdev_create())
        self.assertIn("ghcr.io/x/sdk:tag", self.wkdev_create())
        self.assertIn("using workspace image ghcr.io/x/sdk:tag (WK_SDK_IMAGE)", err)

    def test_create_refuses_without_a_base_or_over_a_container_and_makes_nothing(self):
        err = self.refused(lambda: self.t.create("new", "main-9"))
        self.assertIn("base snapshot main-9 not found; run 'wk sync' first", err)
        err = self.refused(lambda: self.t.create("a", self.base))
        self.assertIn("workspace 'a' already exists", err)
        self.assertEqual([e for e in self.fake.effects if e[0] != "run"], [])
        self.fake.answer(["env"], rc=3, err="no such image\n")
        err = self.refused(lambda: self.t.create("new", self.base))
        self.assertIn("no such image", err)
        self.assertNotIn(os.path.join(self.t.store.ws_dir("new"), "base-id"), self.fake.files)

    def test_create_dies_if_installing_firstrun_fails_and_writes_no_completion_marker(self):
        self.fake.answer(["install"], rc=1, err="install: cannot stat: No such file or directory\n")
        err = self.refused(lambda: self.t.create("new", self.base))
        self.assertIn("installing firstrun.sh into 'new' failed", err)
        self.assertIn("wk rm new", err)
        self.assertNotIn(os.path.join(self.t.store.ws_dir("new"), "base-id"), self.fake.files)

    def test_the_mirrors_parent_under_the_container_home_is_made_as_the_user(self):
        env = dict(self.env, WK_STORE="/home/%s/.local/share/wk" % self.t.user())
        t = targets.Registry(REPO, env=env, machine=self.fake).load("container")
        self.fake.dirs.add(t.store.base_path(self.base))
        self.stderr_of(lambda: t.create("new", self.base))
        self.assertIn(os.path.join(t.store.ws_dir("new"), "home", ".local", "share", "wk", "git"), self.fake.dirs)
        self.assertNotIn(os.path.join(self.t.store.ws_dir("new"), "home", "git"), self.fake.dirs)
        self.assertEqual(t.mirror_dir(), t.store.mirror(), "the workspace sees the mirror at this machine's own path")

    def test_ready_polls_the_marker_by_the_clock_until_it_is_there(self):
        marker = os.path.join(self.t.store.ws_dir("a"), "home", targets.READY_MARKER)
        clock = MarkerClock(self.fake, marker, 3)
        self.assertTrue(self.t.ready("a", clock))
        self.assertEqual(clock.slept, [1, 1, 1])

    def test_ready_gives_up_at_the_timeout_and_shows_the_containers_last_words(self):
        self.fake.answer(["podman", "logs", "wk-a"], out="one\n\ntwo\n")
        clock = FakeClock()
        ok, err = self.stderr_of(lambda: self.t.ready("a", clock, timeout=4))
        self.assertFalse(ok)
        self.assertEqual(clock.slept, [1, 1, 1, 1])
        self.assertIn("initialisation did not complete", err)
        self.assertIn("    one\n    two\n", err)
        self.env["WK_READY_TIMEOUT"] = "2"
        clock = FakeClock()
        self.stderr_of(lambda: self.reg.load("container").ready("a", clock))
        self.assertEqual(clock.slept, [1, 1])

    def test_ready_stops_waiting_when_the_container_vanished(self):
        clock = FakeClock()
        ok, _ = self.stderr_of(lambda: self.t.ready("gone", clock))
        self.assertFalse(ok)
        self.assertEqual(clock.slept, [])

    def test_destroy_removes_the_container_then_the_directory(self):
        ws_dir = self.t.store.ws_dir("a")
        self.fake.write(os.path.join(ws_dir, "home", targets.READY_MARKER), "")
        self.fake.dirs.add(ws_dir)
        self.fake.answer(["podman", "rm"], out="")
        self.fake.answer(["podman", "unshare"], out="")
        self.fake.effects = []
        _, err = self.stderr_of(lambda: self.t.destroy("a"))
        acts = [e for e in self.fake.effects if e[0] != "run" or e[1][:2] in (("podman", "rm"), ("podman", "unshare"))]
        self.assertEqual(acts, [("run", ("podman", "rm", "-f", "wk-a")), ("run", ("podman", "unshare", "rm", "-rf", ws_dir)), ("remove", ws_dir)])
        self.assertFalse(self.fake.isdir(ws_dir))
        self.assertIn("removed container wk-a", err)
        self.assertIn("removed %s" % ws_dir, err)
        self.fake.effects = []
        self.stderr_of(lambda: self.t.destroy("gone"))
        self.assertEqual([e for e in self.fake.effects if e[0] != "run"], [])

    def test_a_dry_run_prints_would_run_and_changes_nothing(self):
        self.dry_run()
        ws_dir = self.t.store.ws_dir("a")
        self.fake.dirs.add(ws_dir)
        before = (dict(self.fake.files), set(self.fake.dirs))
        _, err = self.stderr_of(lambda: self.t.create("new", self.base))
        self.assertIn("would run: env ", err)
        self.assertIn("wkdev-create --network none --isolated", err)
        self.assertIn("would run: install -m 0755", err)
        _, err = self.stderr_of(lambda: self.t.destroy("a"))
        self.assertIn("would run: podman rm -f wk-a", err)
        self.assertIn("would run: podman unshare rm -rf %s" % ws_dir, err)
        self.assertNotIn("could not fully remove", err)
        self.assertEqual((self.fake.files, self.fake.dirs), before)

    def test_store_init_makes_the_tree_once_and_publishes_the_secrets(self):
        root = self.t.store.root()
        with mock.patch.object(secrets.Secrets, "store_publish") as sp:
            self.t.store_init()
            sp.assert_called_once_with()
        for d in ("git", "base", "ws", "cache/ccache", "cache/yocto/downloads", "cache/buildroot/ccache", "cache/bench", "bench", "skills"):
            self.assertIn(os.path.join(root, d), self.fake.dirs)
        conf = os.path.join(root, "cache", "ccache", "ccache.conf")
        self.assertEqual(self.fake.files[conf], "max_size = 40G\n")
        chmods = [e[1] for e in self.fake.effects if e[0] == "run" and e[1][0] == "chmod"]
        self.assertEqual(chmods, [("chmod", "0700", self.t.store.secrets_dir()), ("chmod", "0700", self.t.store.agent_rw_dir())])
        self.fake.effects = []
        with mock.patch.object(secrets.Secrets, "store_publish"):
            self.t.store_init()
        self.assertNotIn(("write", conf), self.fake.effects)
        with mock.patch.object(secrets.Secrets, "store_publish", side_effect=Refused(1)):
            with self.assertRaises(Refused):
                self.t.store_init()

    def test_sdk_refresh_runs_the_refresh_script_on_the_sdk_checkout(self):
        script = os.path.join(self.t.root, "container", "sdk-refresh.sh")
        self.fake.answer(["bash", script], out="")
        self.assertTrue(self.t.sdk_refresh())
        self.assertEqual(self.fake.effects[-1], ("run", ("bash", script, self.t.sdk())))
        self.fake.answer(["bash", script], rc=1, err="fetch failed\n")
        err = self.refused(self.t.sdk_refresh)
        self.assertIn("fetch failed", err)
        self.assertIn("refreshing the webkit-container-sdk checkout failed", err)

    def test_the_creation_log_is_under_the_store(self):
        self.assertEqual(self.t.create_log("a"), os.path.join(self.t.store.root(), "log", "new-a.log"))


class TestVmWrite(VmTest):
    def setUp(self):
        super().setUp()
        self.tart = self.t.tart()
        self.fake.answer(["sysctl", "-n", "hw.ncpu"], out="10\n")
        self.fake.answer(["sysctl", "-n", "hw.memsize"], out="34359738368\n")
        self.fake.answer([self.tart, "clone"], out="")
        self.fake.answer([self.tart, "set"], out="")
        self.fake.dirs.add(self.t.store.mirror())
        for name, value in (("ensure", None), ("stale", "")):
            p = mock.patch.object(guestbase.Base, name, return_value=value)
            setattr(self, "base_" + name, p.start())
            self.addCleanup(p.stop)

    def tart_calls(self):
        return [e[1] for e in self.fake.effects if e[0] == "run" and e[1][0] == self.tart and e[1][1] != "list"]

    def test_create_clones_the_base_then_sets_the_guest_up_and_marks_it_ready(self):
        ws_dir = self.t.store.ws_dir("new")
        _, err = self.stderr_of(lambda: self.t.create("new"))
        self.assertEqual(self.tart_calls(), [(self.tart, "clone", "wk-base", "wk-new"),
                                             (self.tart, "set", "wk-new", "--cpu", "9", "--memory", "20480", "--random-mac", "--display", "1280x800", "--display-refit")])
        self.assertEqual(self.fake.effects[-2:], [("mkdir", ws_dir), ("write", os.path.join(ws_dir, targets.READY_MARKER))])
        self.assertIn("cloning wk-base -> wk-new (APFS copy-on-write)", err)
        self.assertEqual(1, self.base_ensure.call_count)
        self.assertTrue(self.t.created("new"))

    def test_the_guest_sees_the_mirror_through_the_tart_share(self):
        self.assertEqual(self.t.mirror_dir(), "/Volumes/My Shared Files/mirror/WebKit.git")

    def test_create_takes_the_sizing_and_display_overrides(self):
        self.env.update({"WK_VM_CPUS": "4", "WK_VM_MEM_MB": "8192", "WK_VM_DISPLAY": "1920x1080"})
        self.stderr_of(lambda: self.reg.load("vm").create("new"))
        self.assertEqual(self.tart_calls()[1][3:], ("--cpu", "4", "--memory", "8192", "--random-mac", "--display", "1920x1080", "--display-refit"))

    def test_create_refuses_an_existing_guest_a_missing_mirror_and_a_base_that_did_not_build(self):
        err = self.refused(lambda: self.t.create("mac"))
        self.assertIn("workspace 'mac' already exists", err)
        self.fake.dirs.discard(self.t.store.mirror())
        err = self.refused(lambda: self.t.create("new"))
        self.assertIn("no WebKit mirror on this machine for 'new'", err)
        self.assertIn("wk sync    makes it", err)
        self.fake.dirs.add(self.t.store.mirror())
        self.base_ensure.side_effect = Refused(3)
        with self.assertRaises(Refused) as cm:
            self.t.create("new")
        self.assertEqual(cm.exception.status, 3)
        self.assertEqual(self.tart_calls(), [])

    def test_a_stale_base_is_refused_unless_forced(self):
        self.base_stale.return_value = "WK_VM_IMAGE has changed since it was built"
        err = self.refused(lambda: self.t.create("new"))
        self.assertIn("'wk-base' predates its own provisioning inputs: WK_VM_IMAGE has changed since it was built", err)
        self.assertIn("WK_VM_FORCE=1 clones it anyway", err)
        self.assertEqual(self.tart_calls(), [])
        self.env["WK_VM_FORCE"] = "1"
        _, err = self.stderr_of(lambda: self.reg.load("vm").create("new"))
        self.assertIn("WK_VM_FORCE=1 -- 'new' is cloned from a base that", err)
        self.assertEqual(len(self.tart_calls()), 2)

    def test_a_full_host_is_warned_about_with_the_podman_machine_counted(self):
        self.env["WK_VM_MAX"] = "2"
        self.fake.answer(["podman", "machine", "inspect"], out="running\n")
        _, err = self.stderr_of(lambda: self.reg.load("vm").create("new"))
        self.assertIn("2 VM(s) already running on this host; you will have to stop one before starting 'new':\n      mac\n      podman machine wk", err)
        self.assertEqual(len(self.tart_calls()), 2)

    def test_destroy_refuses_the_golden_base(self):
        err = self.refused(lambda: self.t.destroy("base"))
        self.assertIn("refusing to delete the golden base (wk sysimage build macos-guest-base --rm)", err)
        self.assertEqual(self.tart_calls(), [])

    def test_destroy_deletes_the_guest_its_runner_and_every_file_of_it(self):
        ws_dir, vm_dir = self.t.store.ws_dir("mac"), self.t.vm_dir()
        self.fake.dirs.add(ws_dir)
        self.fake.pids.add(4242)
        self.fake.answer([self.tart, "stop"], out="")
        self.fake.answer([self.tart, "delete"], out="")
        self.fake.react(["pgrep"], lambda argv, fake: Result(0, "4242\n") if 4242 in fake.pids else Result(1))
        self.fake.effects = []
        _, err = self.stderr_of(lambda: self.t.destroy("mac"))
        acts = [e for e in self.fake.effects if e[0] != "run" or e[1][0] == self.tart and e[1][1] != "list"]
        self.assertEqual(acts, [("run", (self.tart, "stop", "wk-mac")), ("run", (self.tart, "delete", "wk-mac")), ("kill", 4242, 15),
                                ("remove", ws_dir), ("remove", os.path.join(vm_dir, "mac.run.log")), ("remove", os.path.join(vm_dir, "mac.unfiltered"))])
        self.assertIn("deleted VM wk-mac", err)
        self.assertNotIn("still alive", err)
        self.fake.pids.add(4242)
        self.fake.react(["pgrep"], lambda argv, fake: Result(0, "4242\n"))
        self.fake.kill = lambda pid, sig=15: False
        _, err = self.stderr_of(lambda: self.t.destroy("mac"))
        self.assertIn("a 'tart run' for 'wk-mac' is still alive (pid 4242)", err)

    def test_destroy_of_an_absent_guest_only_clears_its_files(self):
        self.fake.answer([self.tart, "list"], out="[]")
        self.fake.effects = []
        self.stderr_of(lambda: self.t.destroy("gone"))
        self.assertEqual([e for e in self.fake.effects if e[0] != "run"],
                         [("remove", os.path.join(self.t.vm_dir(), "gone.run.log")), ("remove", os.path.join(self.t.vm_dir(), "gone.unfiltered"))])

    def test_a_dry_run_prints_would_run_and_changes_nothing(self):
        self.dry_run()
        self.fake.dirs.update((self.t.store.ws_dir("mac"), self.t.store.lock_dir()))
        before = (dict(self.fake.files), set(self.fake.dirs))
        _, err = self.stderr_of(lambda: self.t.create("new"))
        self.assertIn("would run: %s clone wk-base wk-new" % self.tart, err)
        _, err = self.stderr_of(lambda: self.t.destroy("mac"))
        self.assertIn("would run: %s delete wk-mac" % self.tart, err)
        self.assertEqual((self.fake.files, self.fake.dirs), before)

    def test_store_init_makes_the_store_and_a_private_vm_dir(self):
        self.t.store_init()
        root = self.t.store.root()
        self.assertEqual(self.fake.effects, [("mkdir", root), ("mkdir", os.path.join(root, "ws")), ("mkdir", self.t.vm_dir()),
                                             ("run", ("find", self.t.vm_dir(), "-maxdepth", "0", "-perm", "0700")), ("run", ("chmod", "0700", self.t.vm_dir()))])

    def test_ready_is_the_shared_poll_of_info(self):
        marker = os.path.join(self.t.store.ws_dir("mac"), targets.READY_MARKER)
        clock = MarkerClock(self.fake, marker, 2)
        self.assertTrue(self.t.ready("mac", clock))
        self.assertEqual(clock.slept, [1, 1])
        clock = FakeClock()
        self.assertFalse(self.t.ready("gone", clock))
        self.assertEqual(clock.slept, [])
        self.fake.files.pop(marker)
        self.assertFalse(self.t.ready("mac", clock, timeout=3))
        self.assertEqual(clock.slept, [1, 1, 1])

    def test_a_guest_without_a_store_of_its_own_refuses_to_be_driven(self):
        env = {k: v for k, v in self.env.items() if k != "WK_VM_STORE"}
        t = targets.Registry(REPO, env=env, machine=self.fake).load("vm")
        self.assertIsNone(t.vm_store())
        err = self.refused(lambda: t.created("mac"))
        self.assertIn("set WK_VM_STORE apart from WK_STORE", err)
        self.assertEqual(t.list(), [("mac", "running")])


class TestRemoteWrite(RemoteTest):
    def setUp(self):
        super().setUp()
        self.conf("ref", "WK_REMOTE_HOST=ref.example\nWK_REMOTE_ROOT=/home/u/wk\nWK_REMOTE_REFERENCE=/srv/WebKit\n")
        self.ref = self.reg.load("ref")
        self.fake.answer_remote("ws/a ]", out="absent\n")
        self.fake.answer_remote("ws/half ]", out="creating\n")
        self.fake.answer_remote("ws/there ]", out="present\n")
        self.fake.answer_remote("cat /etc/motd", out="")
        self.fake.answer_remote("git clone", out="")
        self.fake.answer_remote("git init --bare", out="mirror-fetch origin ok\nmirror-fetch wpe FAILED\n")
        self.fake.answer_remote("git remote set-url origin", out="")
        self.fake.answer_remote("ccache.conf", out="")
        self.fake.answer_remote("touch", out="")
        self.fake.answer_remote("rm -rf", out="")

    def acts(self):
        """What the far shell was handed to run, in order, the reads left out."""
        texts = [shlex.split(c[-1])[-1] for c in self.fake.ssh_calls()]
        return [t for t in texts if not any(n in t for n in ("uname -s", "echo absent", "test -f $HOME", "/etc/motd"))]

    def test_create_from_a_shared_checkout_is_four_round_trips_with_the_marker_last(self):
        _, err = self.stderr_of(lambda: self.ref.create("a"))
        acts = self.acts()
        self.assertEqual(len(acts), 4)
        self.assertIn("mkdir -p /home/u/wk/ws /home/u/wk/cache/ccache\n git clone --quiet -b main /srv/WebKit /home/u/wk/ws/a/WebKit", acts[0])
        self.assertIn("cd '/home/u/wk/ws/a/WebKit'", acts[1])
        self.assertIn("git remote add shared '/srv/WebKit'", acts[1])
        self.assertIn("ssh -F /home/u/wk/ssh/config", acts[1])
        self.assertIn("[ -f /home/u/wk/cache/ccache/ccache.conf ] || printf %s 'max_size = 40G", acts[2])
        self.assertIn("touch /home/u/wk/ws/a/.wk-ready", acts[3])
        self.assertEqual(shlex.split(self.fake.effects[-1][1][-1])[-1], acts[3])
        self.assertIn(("mkdir", self.ref.store.ws_dir("a")), self.fake.effects[:-1])
        self.assertIn("cloning from /srv/WebKit (this machine's shared WebKit, hardlinked)", err)
        self.assertIn("remote workspace 'a' created on ref.example (/home/u/wk/ws/a)", err)

    def test_create_without_a_shared_checkout_refreshes_the_mirror_first(self):
        _, err = self.stderr_of(lambda: self.t.create("a"))
        acts = self.acts()
        self.assertEqual(len(acts), 5)
        self.assertIn("M='/home/u/wk/mirror'", acts[0])
        self.assertIn("git clone --quiet --shared -b main /home/u/wk/mirror /home/u/wk/ws/a/WebKit", acts[1])
        self.assertIn("git remote add mirror '/home/u/wk/mirror'", acts[2])
        self.assertIn("touch /home/u/wk/ws/a/.wk-ready", acts[4])
        self.assertIn("updating the WebKit mirror on box.example (first run clones it)", err)
        self.assertIn("  origin   ok\n  wpe      FAILED\n", err)
        self.assertEqual(len(self.fake.ssh_calls("/etc/motd")), 1)

    def test_create_refuses_what_is_there_and_names_the_remedy(self):
        err = self.refused(lambda: self.ref.create("half"))
        self.assertIn("'half' on ref.example is a checkout that never finished being", err)
        self.assertIn("ssh ref.example rm -rf /home/u/wk/ws/half", err)
        err = self.refused(lambda: self.ref.create("there"))
        self.assertIn("workspace 'there' already exists on ref.example", err)
        self.fake.remote = [("uname -s", Result(255, "", "ssh: Connection refused\n"))]
        err = self.refused(lambda: self.reg.load("ref").create("a"))
        self.assertIn("cannot reach 'ref.example' over ssh: Connection refused", err)
        self.assertEqual(self.acts(), [])

    def test_a_failed_round_trip_ends_the_creation_where_it_stood(self):
        for mod, name, text in ((git, "wiring_script", "git remote set-url origin x"), (shell, "ccache_conf", "max_size = 40G\n")):
            p = mock.patch.object(mod, name, return_value=text)
            p.start()
            self.addCleanup(p.stop)
        self.fake.answer_remote("git clone", rc=128, err="fatal: not a git repository\n")
        err = self.refused(lambda: self.ref.create("a"))
        self.assertIn("could not clone /srv/WebKit on ref.example", err)
        self.assertEqual(len(self.acts()), 1)
        self.fake.answer_remote("git clone", out="")
        self.fake.answer_remote("touch", rc=255)
        err = self.refused(lambda: self.ref.create("a"))
        self.assertIn("could not mark 'a' ready on ref.example -- treat it as half-made", err)
        self.assertIn("re-run 'wk new a --target ref'", err)
        self.fake.answer_remote("touch", out="")
        self.fake.answer_remote("git remote set-url origin", rc=1)
        _, err = self.stderr_of(lambda: self.ref.create("a"))
        self.assertIn("could not wire the remotes in /home/u/wk/ws/a/WebKit", err)
        self.assertIn("touch", self.acts()[-1])

    def test_a_peer_is_not_a_build_machine_and_says_where_to_create(self):
        self.conf("peer", "WK_REMOTE_PEER=1\nWK_REMOTE_TOOLS=/opt/wk-tools\n")
        err = self.refused(lambda: self.reg.load("peer").create("newws"))
        self.assertIn("'peer' is a workstation, not a build machine for this one.", err)
        self.assertIn("ssh peer wk new newws", err)
        self.assertEqual(self.fake.ssh_calls(), [])

    def test_destroy_removes_the_far_checkout_then_the_record_here(self):
        ws_dir = self.t.store.ws_dir("a")
        self.fake.dirs.add(ws_dir)
        _, err = self.stderr_of(lambda: self.t.destroy("a"))
        self.assertEqual(self.acts(), ["rm -rf /home/u/wk/ws/a"])
        self.assertEqual(self.fake.effects[-1], ("remove", ws_dir))
        self.assertIn("removed remote workspace 'a' from box.example", err)
        self.fake.answer_remote("rm -rf", rc=255, err="ssh: Connection closed\n")
        self.fake.effects = []
        err = self.refused(lambda: self.t.destroy("a"))
        self.assertIn("could not remove /home/u/wk/ws/a on box.example", err)
        self.assertIn("The record of 'a' here is kept", err)
        self.assertNotIn(("remove", ws_dir), self.fake.effects)

    def test_a_peers_workspace_is_destroyed_by_its_own_wk(self):
        self.conf("peer", "WK_REMOTE_PEER=1\nWK_REMOTE_TOOLS=/opt/wk-tools\n")
        t = self.reg.load("peer")
        self.fake.answer_remote("wk rm a", out="==> workspace 'a' destroyed\n")
        _, err = self.stderr_of(lambda: t.destroy("a"))
        cmd = self.fake.ssh_calls("wk rm a")[0][-1]
        self.assertIn("WK_YES=1 /opt/wk-tools/wk rm a 2>&1", cmd)
        self.assertEqual(self.fake.effects[-1], ("remove", t.store.ws_dir("a")))
        self.assertIn("==> workspace 'a' destroyed", err)
        self.assertIn("'a' destroyed on peer, by that machine's own wk", err)
        self.fake.answer_remote("wk rm a", rc=1, out="error: has work running in it\n")
        self.fake.effects = []
        err = self.refused(lambda: t.destroy("a"))
        self.assertIn("peer did not destroy 'a'", err)
        self.assertIn("has work running in it", err)
        self.assertEqual([e for e in self.fake.effects if e[0] == "remove"], [])

    def test_store_init_makes_the_record_here(self):
        self.t.store_init()
        self.assertEqual(self.fake.effects, [("mkdir", self.t.store.root()), ("mkdir", os.path.join(self.t.store.root(), "ws"))])

    def test_a_dry_run_reads_the_machine_and_runs_nothing_there(self):
        self.dry_run()
        _, err = self.stderr_of(lambda: self.ref.create("a"))
        self.assertEqual(self.acts(), [])
        self.assertIn("would run on ref.example: sh -c", err)
        self.assertIn("touch /home/u/wk/ws/a/.wk-ready", err)
        self.assertEqual(len(self.fake.ssh_calls("ws/a ]")), 1)
        _, err = self.stderr_of(lambda: self.t.destroy("a"))
        self.assertIn("would run on box.example: sh -c 'rm -rf /home/u/wk/ws/a'", err)

    def test_this_machine_as_the_remote_runs_every_round_trip_in_a_local_shell(self):
        self.conf("me", "WK_REMOTE_LOCAL=1\nWK_REMOTE_ROOT=%s\nWK_REMOTE_REFERENCE=/srv/WebKit\n" % (self.tmp / "rr"))
        me = self.reg.load("me")

        def sh(argv, fake):
            text = argv[2]
            if "uname -s" in text:
                return Result(0, LINUX_PROBE)
            return Result(0, "absent\n" if "echo absent" in text else "")

        self.fake.react(["sh", "-c"], sh)
        self.stderr_of(lambda: me.create("a"))
        acts = [e[1][2] for e in self.fake.effects if e[0] == "run" and e[1][:2] == ("sh", "-c") and "uname -s" not in e[1][2] and "echo absent" not in e[1][2]]
        self.assertEqual(len(acts), 4)
        self.assertIn("git clone --quiet -b main /srv/WebKit", acts[0])
        self.assertIn("touch %s/ws/a/.wk-ready" % (self.tmp / "rr"), acts[3])
        self.assertEqual(self.fake.ssh_calls(), [])


class TestLocalWrite(TargetsTest):
    def setUp(self):
        super().setUp()
        marker = self.tmp / "marker"
        marker.write_text("name=ws\nsrc=/src/WebKit\n")
        self.env["WK_MARKER"] = str(marker)
        self.t = self.reg.load("local")

    def test_a_workspace_neither_creates_nor_destroys_and_names_the_host_command(self):
        err = self.refused(lambda: self.t.create("x"))
        self.assertIn("a workspace cannot create a workspace -- run 'wk new x' on the host", err)
        err = self.refused(lambda: self.t.destroy("ws"))
        self.assertIn("a workspace cannot destroy itself -- run 'wk rm ws' on the host", err)
        self.assertEqual(self.fake.effects, [])

    def test_store_init_makes_its_own_record(self):
        self.t.store_init()
        self.assertEqual(self.fake.effects, [("mkdir", self.t.store.ws_dir("ws"))])

    def test_exec_tty_is_a_plain_local_exec(self):
        self.fake.answer(["bash"], out="")
        r = self.t.exec_tty("ws", ["true"])
        self.assertEqual(r.rc, 0)
        self.assertEqual(self.fake.effects[-1], ("run_tty", ("bash", "-lc", "exec true"), None))


class TestBridges(TargetsTest):
    """What stays bash is reached by name, with the environment scoped to this test."""

    def test_the_renderers_return_the_bash_text(self):
        env = dict(self.env)
        self.assertEqual(shell.ccache_conf(str(REPO), env), "max_size = 40G\n")
        self.assertEqual(shell.ccache_conf(str(REPO), dict(env, WK_CCACHE_MAXSIZE="5G")), "max_size = 5G\n")

    def test_the_scripts_are_the_bash_text_for_the_far_shell(self):
        env = dict(self.env)
        branches = git.mirror_branches(env)
        script = git.mirror_refresh_script("/m", branches)
        self.assertIn("M='/m'", script)
        self.assertIn('echo "mirror-fetch $r ok"', script)
        wiring = git.wiring_script("/src", "/m", [], branches, "shared", "/srv/WebKit", "/r/ssh/config")
        self.assertTrue(wiring.startswith("set -e\ncd '/src'\n"))
        self.assertIn("git remote add shared '/srv/WebKit'", wiring)
        self.assertIn("git config core.sshCommand 'ssh -F /r/ssh/config'", wiring)

    def test_gpu_flags_are_read_where_the_container_is_made(self):
        self.assertEqual(shell.gpu_flags(str(REPO), self.fake), [])
        self.fake.answer(shell.argv(str(REPO), shell.GPU_FLAGS_FN)[:3], out="--device /dev/dri --device nvidia.com/gpu=all\n")
        self.assertEqual(shell.gpu_flags(str(REPO), self.fake), ["--device", "/dev/dri", "--device", "nvidia.com/gpu=all"])
        self.assertEqual(self.fake.effects[-1][1][:2], ("bash", "-c"))


class TestBuildSide(RemoteTest):
    """What `wk build` asks of a driver: where ccache lives, the command the build runs as, the machine it
    is sized from, and where its record has to be for that machine's `wk status`."""

    def test_the_ccache_and_the_size_are_the_machines_own(self):
        self.assertEqual(self.t.ccache_dir("a"), "/home/u/wk/cache/ccache")
        self.assertEqual(self.reg.load("container").ccache_dir("a"), "/ccache")
        self.assertEqual(self.t.build_size("a"), (8, 20000, 0))

    def test_the_build_is_serialised_niced_and_teed_on_the_far_side(self):
        argv, cwd = self.t.build_argv("a", ["env", "A=1", "/t/build-in-target.sh"])
        self.assertIsNone(cwd)
        self.assertEqual(argv[0], "ssh")
        self.assertEqual(argv[-2], "box.example")
        text = shlex.split(argv[-1])[2]
        self.assertTrue(text.startswith("set -o pipefail\ncd /home/u/wk/ws/a/WebKit && "))
        self.assertIn("/home/u/wk/tools/lib/lockrun.sh remote-build -w 3600 -- nice -n 19 ionice -c3 env A=1 /t/build-in-target.sh", text)
        self.assertTrue(text.endswith("2>&1 | tee /home/u/wk/ws/a/build.log"))

    def test_the_record_is_shipped_with_the_far_log_and_host(self):
        rec = record.Records(self.tmp / "rec", clock=FakeClock(), env=self.env, machine=self.fake)
        with mock.patch.object(record, "host_name", return_value="here"):
            t = rec.begin("build", "here", "a", "wk build a --kill", "/here/build.log", ["compile"], pid=4242)
        self.fake.answer_remote("mkdir -p")
        self.t.task_put("a", t)
        (call,) = self.fake.ssh_calls("mkdir -p")
        text = shlex.split(call[-1])[2]
        self.assertIn("/home/u/wk/task/%s.new" % t.id, text)
        self.assertIn("printf '%%s' 'compile\n' > /home/u/wk/task/%s.new/plan" % t.id, text)
        self.assertIn("/home/u/wk/ws/a/build.log", text)
        self.assertIn("printf '%%s\\n' box.example > /home/u/wk/task/%s.new/machine" % t.id, text)
        self.assertTrue(text.endswith("mv /home/u/wk/task/%s.new /home/u/wk/task/%s" % (t.id, t.id)))
        self.fake.answer_remote("mkdir -p", rc=255)
        _, err = self.stderr_of(lambda: self.t.task_put("a", t))
        self.assertIn("could not record 'a's build state on box.example", err)

    def test_a_target_on_this_machine_ships_nothing_and_runs_the_build_here(self):
        self.conf("me", "WK_REMOTE_LOCAL=1\nWK_REMOTE_ROOT=%s\n" % (self.tmp / "rr"))
        me = self.reg.load("me")
        me._probed = {"home": "/h", "cores": 2, "load": 0, "mem_mb": 100, "ionice": "no", "os": "linux", "root": str(self.tmp / "rr")}
        argv, _ = me.build_argv("a", ["true"])
        self.assertEqual(argv[:2], ["bash", "-c"])
        self.assertNotIn("tee", argv[2])
        self.assertNotIn("ionice", argv[2])
        before = list(self.fake.effects)
        me.task_put("a", None)
        self.assertEqual(self.fake.effects, before)


class TestBuildSize(TargetsTest):
    def test_a_workspace_is_sized_by_its_own_cgroup_else_the_machine(self):
        marker = self.tmp / "marker"
        marker.write_text("name=ws\nsrc=/src/WebKit\n")
        self.env["WK_MARKER"] = str(marker)
        t = self.reg.load("local")
        self.fake.files["/sys/fs/cgroup/cpu.max"] = "200000 100000\n"
        self.fake.files["/sys/fs/cgroup/memory.max"] = "4294967296\n"
        self.assertEqual(t.build_size("ws"), (2, 4096, None))
        self.fake.files["/sys/fs/cgroup/cpu.max"] = "max 100000\n"
        self.fake.files["/sys/fs/cgroup/memory.max"] = "max\n"
        self.fake.answer(["nproc"], out="4\n")
        self.fake.answer(["sysctl", "-n", "hw.ncpu"], out="4\n")
        self.fake.answer(["sysctl", "-n", "hw.memsize"], out="8589934592\n")
        self.fake.files["/proc/meminfo"] = "MemTotal: 8388608 kB\n"
        self.assertEqual(t.build_size("ws"), (4, 8192, None))

    def test_a_guest_is_sized_as_it_is_configured_and_else_as_this_host_would_size_one(self):
        with mock.patch.object(targets.Vm, "tart", lambda s: "/t/tart"):
            vm = targets.Vm("vm", str(REPO), self.env, self.fake)
            self.fake.answer(["/t/tart", "get", "wk-g"], out='{"CPU": 6, "Memory": 12288}')
            self.assertEqual(vm.build_size("g"), (6, 12288, None))
            self.fake.answer(["/t/tart", "get", "wk-g"], rc=1)
            self.env.update({"WK_VM_CPUS": "3", "WK_VM_MEM_MB": "4096"})
            self.assertEqual(vm.build_size("g"), (3, 4096, None))


class TestBuildBridges(TargetsTest):
    def test_a_branch_fetch_asks_the_mirror_first_or_origin_alone(self):
        self.assertEqual(git.origin_branch_fetch_step("b", "").strip(), "git fetch -q origin 'b'")
        self.assertIn("git fetch -q '/m' '+refs/heads/b:refs/remotes/origin/b'", git.origin_branch_fetch_step("b", "/m"))


class _KillExecTarget(targets.Target):
    """A minimal Target whose `exec` answers a canned `kill -0`, to test the
    base class's `pid_alive` in isolation from any driver's own exec."""

    def __init__(self, result):
        super().__init__("t", str(REPO), {}, Fake("here"))
        self.result = result
        self.asked = None

    def exec(self, ws, argv, tty=False, timeout=None):
        self.asked = (ws, argv, timeout)
        return self.result


class TestPidAliveIsTheOneAnswer(TargetsTest):
    """shell.target_pid_alive (a bash round trip), record.of_target's own
    closure and the drivers' own `kill -0` calls were three answers for "is
    this pid alive in the target"; Target.pid_alive is the one now, and
    cmd/stop, cmd/status, record.of_target and workspace.py (through it) all
    ask it."""

    def test_pid_alive_reads_kill_dash_0_true_false_or_no_answer(self):
        alive = _KillExecTarget(Result(0))
        self.assertTrue(alive.pid_alive("a", 123, 5))
        self.assertEqual(alive.asked, ("a", ["kill", "-0", "123"], 5))
        self.assertFalse(_KillExecTarget(Result(1)).pid_alive("a", 123, 5))
        self.assertIsNone(_KillExecTarget(Result(TIMED_OUT)).pid_alive("a", 123, 5))

    def test_of_target_asks_the_targets_pid_alive(self):
        t = self.reg.load("container")
        self.assertEqual(record.of_target(t).ask_target, t.pid_alive)

    def test_stop_and_status_ask_the_targets_pid_alive(self):
        for cmd in ("stop", "status"):
            text = (REPO / "cmd" / cmd).read_text()
            self.assertIn(".pid_alive(n, pid, cap)", text, cmd)
            self.assertNotIn("shell.target_pid_alive", text, cmd)


if __name__ == "__main__":
    unittest.main()
