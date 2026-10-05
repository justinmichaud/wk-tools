"""lib/wk/places.py: the registry and every driver over a fake machine."""
import contextlib
import inspect
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes import WsDriver
from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import act, git, machine, places, project, record, secrets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import TIMED_OUT, Fake, Result, lib_argv  # noqa: E402
from wk.store import Store  # noqa: E402
from wk.sysimage import guestbase  # noqa: E402

IS_MACOS = os.uname().sysname == "Darwin"
LINUX_MEMINFO = "MemTotal:       32806140 kB\nMemFree:         1000000 kB\nMemAvailable:   20480000 kB\n"


class DriversTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-places-"))
        self.registry_dir = self.tmp / "hosts"
        self.registry_dir.mkdir()
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"),
                    "WK_MACHINES_DIR": str(self.registry_dir), "WK_IN_VM": "1", "PATH": os.environ.get("PATH", "")}
        (self.tmp / "home").mkdir()
        self.fake = Fake("here")
        self.reg = places.Registry(REPO, env=self.env, machine=self.fake)

    def tearDown(self):
        os.system("rm -rf %s" % self.tmp)

    def conf(self, name, text):
        kind = "" if "kind=" in text.replace("driver=", "") else \
            "kind=%s\n" % ("peer" if "peer=1" in text else "build")
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
    def test_only_a_real_socket_under_the_runtime_dir_is_present(self):
        import socket
        tmp = Path(tempfile.mkdtemp(prefix="wk-test-session-socket-"))
        self.addCleanup(lambda: os.system("rm -rf %s" % tmp))
        env = {"XDG_RUNTIME_DIR": str(tmp)}
        path = Path(places.session_socket_path(env))
        self.assertFalse(places.session_socket_present(env))
        path.parent.mkdir(parents=True)
        path.write_text("")
        self.assertFalse(places.session_socket_present(env))
        path.unlink()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(s.close)
        s.bind(str(path))
        self.assertTrue(places.session_socket_present(env))


class TestRegistry(DriversTest):
    def host_registry(self):
        env = {k: v for k, v in self.env.items() if k != "WK_IN_VM"}
        return places.Registry(REPO, env=env, machine=self.fake)

    def test_the_builtins_and_every_conf_are_places(self):
        self.conf("box1", "kind=build\ndriver=remote\nhost=box1\n")
        self.conf("peer", "# a peer\npeer=1\n")
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
        self.conf(me.upper(), "host=%s\n" % me)
        self.conf("box1", "host=box1\n")
        self.assertEqual(self.host_registry().machines(), ["box1"])

    def test_the_far_end_of_a_target_lists_no_conf(self):
        self.conf("box1", "host=box1\n")
        self.assertEqual(self.reg.all(), ["container"])   # WK_IN_VM
        env = {k: v for k, v in self.env.items() if k != "WK_IN_VM"}
        env["WK_REMOTE_MARKER"] = str(self.tmp / "wk-remote")
        (self.tmp / "wk-remote").write_text("place=box2\nroot=/home/u/wk\n")
        self.conf("box2", "hostname=box2-host\n")
        self.fake.answer(["hostname", "-s"], out="box2-host\n")
        self.assertEqual(places.Registry(REPO, env=env, machine=self.fake).all(), ["container", "box2"])

    def test_inside_a_workspace_the_default_is_local(self):
        marker = self.tmp / "marker"
        marker.write_text("name=ws\nsrc=/src/WebKit\narch=armhf\n")
        self.env["WK_MARKER"] = str(marker)
        self.assertEqual(self.reg.default(), "local")
        t = self.reg.load("local")
        self.assertEqual(t.list(), [("ws", "running")])
        self.assertEqual((t.info("ws"), t.info("other"), t.arch("ws")), ("running", "absent", "armhf"))

    def test_a_build_box_defaults_to_its_own_place(self):
        rm = self.tmp / "remote-marker"
        rm.write_text("place=buildbox\nroot=/home/u/wk\n")
        self.env["WK_REMOTE_MARKER"] = str(rm)
        self.conf("buildbox", "host=buildbox\n")
        self.fake.answer(["hostname", "-s"], out="buildbox\n")
        self.assertEqual(self.reg.default(), "buildbox")
        self.assertIn("buildbox", self.reg.all())

    def test_vm_is_listed_only_on_a_macos_host_with_a_store_of_its_own(self):
        self.assertFalse(self.reg.vm_listed())   # WK_IN_VM
        env = dict(self.env)
        env.pop("WK_IN_VM")
        reg = places.Registry(REPO, env=env, machine=self.fake)
        self.assertEqual(reg.vm_listed(), False)   # a scratch store is the container's
        env["WK_VM_STORE"] = str(self.tmp / "vmstore")
        reg = places.Registry(REPO, env=env, machine=self.fake)
        self.assertEqual(reg.vm_listed(), IS_MACOS)


class TestContainer(DriversTest):
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
        self.fake.write(os.path.join(home, places.READY_MARKER), "")
        self.assertEqual(self.t.info("a"), "running")
        self.assertEqual(self.t.info("c"), "absent")

    def test_state_reads_the_record_and_the_environment_together(self):
        ws = os.path.join(self.env["WK_STORE"], "ws")
        self.assertEqual(self.t.state("c"), "absent")
        self.fake.mkdir(os.path.join(ws, "c"))
        self.assertEqual(self.t.state("c"), "creating")           # a directory, no marker, no container
        self.fake.write(os.path.join(ws, "c", "home", places.READY_MARKER), "")
        self.assertEqual(self.t.state("c"), "broken")             # created, and the container is gone
        self.assertEqual(self.t.state("a"), "broken")             # a container with no directory, nothing creating it
        self.fake.write(os.path.join(ws, "a", "home", places.READY_MARKER), "")
        self.assertEqual(self.t.state("a"), "creating")           # no base-id yet
        self.fake.write(os.path.join(ws, "a", "base-id"), "main-1\n")
        self.assertEqual(self.t.state("a"), "present")
        self.assertEqual(self.t.display_state("a"), "running")

    def test_a_workspace_exists_by_its_directory_its_environment_or_a_creation(self):
        self.assertFalse(self.reg.exists_on(self.t, "c"))
        self.assertTrue(self.reg.exists_on(self.t, "a"))          # podman knows it, whatever the store holds
        self.fake.mkdir(os.path.join(self.env["WK_STORE"], "ws", "c"))
        self.assertTrue(self.reg.exists_on(self.t, "c"))

    def test_exec_goes_through_the_sdk_and_the_bridge_and_only_exec_tty_carries_a_tty(self):
        self.fake.answer(["env"], out="ok\n")
        self.assertEqual(self.t.exec("a", ["git", "rev-parse", "HEAD"]).out, "ok\n")
        argv = self.fake.effects[-1][1]
        self.assertEqual(argv[0], "env")
        self.assertIn("WKDEV_SDK=%s" % self.t.sdk(), argv)
        self.assertIn(os.path.join(self.t.sdk(), "scripts", "host-only", "wkdev-enter"), argv)
        self.assertIn("--no-tty", argv)
        self.assertEqual(argv[-3:], ("git", "rev-parse", "HEAD"))
        self.assertIn("ensure-bridge.sh", argv[argv.index("--") + 1])
        self.assertEqual(self.t.exec_tty("a", ["lldb", "-o", "attach"]).rc, 0)
        kind, argv, cwd = self.fake.effects[-1]
        self.assertEqual((kind, argv[-3:], cwd), ("run_tty", ("lldb", "-o", "attach"), None))
        self.assertNotIn("--no-tty", argv)

    def test_wk_is_the_podman_vms_own_wk_over_machine_ssh(self):
        self.fake.answer(["podman", "machine", "ssh", "wk", "--"], out='{"kind": "workspace"}\n')
        env = dict(self.env, WK_ROW_LABEL="tolken", WK_NO_DELEGATE="1")
        env.pop("WK_IN_VM")
        self.assertEqual(self.t.wk("status", "--records", "ws", env=env, quiet=True), (0, '{"kind": "workspace"}\n'))
        words = shlex.split(self.fake.effects[-1][1][-1])
        self.assertLessEqual({"WK_IN_VM=1", "WK_HOST_SELF=1", "WK_ROW_LABEL=tolken", "WK_NO_DELEGATE=1"}, set(words))
        self.assertEqual(words[-4:], ["/opt/wk-tools/wk", "status", "--records", "ws"])
        self.t.wk("doctor", env=env)
        self.assertTrue(self.fake.effects[-1][1][-1].endswith(" 2>&1"))

    def test_stop_is_an_effect(self):
        self.fake.answer(["podman", "stop"], out="")
        self.assertTrue(self.t.stop("a"))
        self.assertEqual(self.fake.effects[-1][1][:3], ("podman", "stop", "--time"))

    def test_the_arch_is_recorded_at_creation(self):
        self.assertEqual(self.t.arch("a"), "native")
        self.fake.write(os.path.join(self.env["WK_STORE"], "ws", "a", "arch"), "armhf\n")
        self.assertEqual(self.t.arch("a"), "armhf")

    def test_sdk_local_reads_the_pulled_image_and_its_pull_date_or_none(self):
        self.fake.answer(["podman", "images"], out="docker.io/library/busybox:latest\n")
        self.assertIsNone(self.t.sdk_local())
        self.fake.answer(["podman", "images"], out="ghcr.io/igalia/wkdev-sdk:2.53-v9-abc0000\n")
        self.fake.answer(["podman", "image", "inspect"], out="2026-08-01T12:00:00Z\n")
        self.assertEqual(self.t.sdk_local(), {"image": "ghcr.io/igalia/wkdev-sdk:2.53-v9-abc0000", "created": "2026-08-01"})

    def test_sdk_upstream_is_the_registrys_tags_past_the_header_row_or_none(self):
        self.fake.answer(["podman", "search", "--list-tags"], out="NAME\tTAG\nghcr.io/igalia/wkdev-sdk\t2.53-v9-abc0000\n"
                                                                    "ghcr.io/igalia/wkdev-sdk\t24.04_arm32\n")
        self.assertEqual(self.t.sdk_upstream(), ["2.53-v9-abc0000", "24.04_arm32"])
        self.fake.answer(["podman", "search", "--list-tags"], rc=1, err="timed out")
        self.assertIsNone(self.t.sdk_upstream(timeout=4))


class VmTest(DriversTest):
    def setUp(self):
        super().setUp()
        self.env.pop("WK_IN_VM")
        self.env["WK_VM_STORE"] = str(self.tmp / "vmstore")
        (self.tmp / "bin").mkdir()
        (self.tmp / "bin" / "tart").write_text("")
        (self.tmp / "bin" / "tart").chmod(0o755)
        self.env["PATH"] = str(self.tmp / "bin")
        # A guest is driven from a macOS host; the store rule that says so is held by test_wk_store.
        mac = mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=True)
        mac.start()
        self.addCleanup(mac.stop)
        self.reg = places.Registry(REPO, env=self.env, machine=self.fake)
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
        self.fake.write(os.path.join(self.env["WK_VM_STORE"], "ws", "mac", places.READY_MARKER), "")
        self.assertEqual(self.t.info("mac"), "running")
        self.assertEqual(self.t.info("gone"), "absent")
        self.fake.answer([self.t.tart(), "exec"], out="hi\n")
        r = self.t.exec("mac", ["echo", "hi"])
        self.assertEqual(r.out, "hi\n")
        argv = self.fake.effects[-1][1]
        self.assertEqual(argv, (self.t.tart(), "exec", "-i", "wk-mac", "/bin/zsh", "-lc", "echo hi"), "the guest agent, no address")
        self.assertEqual(self.t.exec("gone", ["true"]).rc, 1)

    def test_exec_tty_allocates_a_pty_through_the_guest_agent(self):
        self.fake.answer([self.t.tart(), "exec"], out="ignored\n")
        r = self.t.exec_tty("mac", ["lldb", "-o", "attach"])
        self.assertEqual(r.rc, 0)
        kind, argv, cwd = self.fake.effects[-1]
        self.assertEqual(kind, "run_tty")
        self.assertEqual(argv[:5], (self.t.tart(), "exec", "-i", "-t", "wk-mac"))
        self.assertEqual(argv[-1], "lldb -o attach")
        self.assertIsNone(cwd)

    def test_a_stopped_guest_is_refused_naming_its_start(self):
        for fn in (lambda: self.t.exec_argv("gone", ["true"]), lambda: self.t.pull("gone", "/a", "/b"),
                   lambda: self.t.path_kind("gone", "/a")):
            self.assertIn("'gone' is not running (wk start gone)", self.refused(fn))

    def test_the_editor_alias_reaches_sshd_through_tart_exec_never_the_network(self):
        self.fake.answer(["chmod"])
        self.t.ssh_prepare("mac")
        text = self.fake.read(places.sshalias.alias_path(self.env))
        self.assertIn("Host wk-mac\n    # generated by wk -- removed by wk rm\n    HostName wk-mac.vm.invalid\n", text)
        self.assertIn("ProxyCommand %s vm mac" % os.path.join(str(REPO), "container", "ssh-transport"), text)
        self.assertIn("IdentityFile " + self.t.key(), text)
        self.assertEqual((self.t.ssh_host("mac"), self.t.ssh_host("gone"), self.t.ssh_user("mac")), ("wk-mac", None, "admin"))

    def test_the_transport_bridges_stdio_to_the_guests_own_sshd_on_loopback(self):
        with mock.patch.object(os, "execvp") as ex:
            self.t.ssh_transport("mac")
        (prog, argv), _ = ex.call_args
        self.assertEqual((prog, argv), (self.t.tart(), [self.t.tart(), "exec", "-i", "wk-mac", "/usr/bin/nc", "127.0.0.1", "22"]))

    def test_the_socket_forwards_ride_the_same_transport(self):
        argv = self.t.ssh_argv("mac")
        self.assertEqual(argv[-1], "wk-mac.vm.invalid")
        self.assertIn("ProxyCommand=" + self.t.ssh_proxy("mac"), argv)
        self.assertEqual(argv[argv.index("-i") + 1], self.t.key())


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
        return [tuple(e[1][:-1]) + (shlex.split(e[1][-1])[-1],) for e in self.effects
                if e[0] == "run" and e[1][0] == "ssh" and needle in e[1][-1]]


class RemoteTest(DriversTest):
    def setUp(self):
        super().setUp()
        self.fake = SshFake()
        self.env.update({"XDG_STATE_HOME": str(self.tmp / "state"), "WK_PROBE_SECONDS": "2", "WK_SSH_TIMEOUT": "3"})
        del self.env["WK_IN_VM"]   # a host with machines in its registry
        self.conf("box", "host=box.example\nroot=/home/u/wk\n")
        self.reg = places.Registry(REPO, env=self.env, machine=self.fake)
        self.t = self.reg.load("box")
        self.fake.answer_remote("uname -s", out=LINUX_PROBE)
        self.fake.answer_remote("test -f $HOME/.wk-remote", rc=0)
        self.tools_at("abc1234def")

    def tools_at(self, theirs, mine="abc1234def"):
        """The box's wk-tools at `theirs`, and this checkout at `mine`."""
        self.fake.answer(["git", "-C", str(REPO), "rev-parse", "HEAD"], out=mine + "\n")
        self.fake.answer_remote("/wk doctor --probe-tools", out="sha=%s\ndirty=no\n" % theirs)


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
        self.assertIn(places.PROBE_SCRIPT, argv[-1])

    def test_exec_tty_over_ssh_runs_through_here_not_the_ssh_machine(self):
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
        self.conf("me", "local=1\nroot=%s\n" % (self.tmp / "rr"))
        me = self.reg.load("me")
        self.fake.answer(["sh"], out=LINUX_PROBE)   # is_local still probes itself, over a plain shell, not ssh
        self.fake.answer(["true"], out="")
        r = me.exec_tty("a", ["true"])
        self.assertEqual(r.rc, 0)
        self.assertEqual(self.fake.effects[-1], ("run_tty", ("true",), me.src("a")))

    def test_the_root_defaults_to_wk_under_the_far_home(self):
        self.conf("bare", "host=bare.example\n")
        t = self.reg.load("bare")
        self.assertEqual((t.tools(""), t.src("a")), ("/home/u/wk/tools", "/home/u/wk/ws/a/WebKit"))
        self.conf("tools", "host=bare.example\ntools=my/tools\n")
        self.assertEqual(self.reg.load("tools").tools(""), "/home/u/my/tools")

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
        self.conf("me", "local=1\nroot=%s\n" % (self.tmp / "rr"))
        me = self.reg.load("me")
        self.assertTrue(me.is_here())
        self.assertEqual((me.far_side(), me.probe(), me.answers(), me.has_wk(), me.delegates()), ("none", ("none", ""), (True, ""), False, False))
        self.assertEqual(me.store.store_dir(), str(self.tmp / "rr"))
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

    def test_the_ssh_option_getter_makes_no_directory_and_the_first_ssh_does(self):
        state = self.tmp / "state" / "wk" / "ssh"
        self.t.ssh_opts()
        self.assertFalse(state.exists())
        self.assertNotIn(("mkdir", str(state)), self.fake.effects)
        self.fake.answer_remote("git status", out="clean\n")
        self.t.exec("a", ["git", "status"])
        self.assertIn(("mkdir", str(state)), self.fake.effects)

    def test_wk_is_the_far_machines_own_with_the_flags_as_environment(self):
        self.fake.answer_remote("tools/wk", out="==> no task is running on box\n")
        rc, out = self.t.wk("stop", "--tasks", env={"WK_YES": "1", "WK_ROW_LABEL": "box", "WK_NO_DELEGATE": "1", "HOME": "/x"})
        self.assertEqual((rc, out), (0, "==> no task is running on box\n"))
        line = shlex.split(self.fake.ssh_calls("tools/wk")[0][-1])[-1]
        self.assertTrue(line.startswith("cd $HOME && "), line)
        self.assertLessEqual({"WK_YES=1", "WK_ROW_LABEL=box", "WK_NO_DELEGATE=1"}, set(line.split()))
        self.assertTrue(line.endswith(" /home/u/wk/tools/wk stop --tasks 2>&1"), line)
        self.t.wk("doctor", env={}, quiet=True)
        self.assertNotIn("2>&1", self.fake.ssh_calls("tools/wk")[1][-1])

    def test_a_peer_is_asked_not_driven(self):
        self.conf("peer", "peer=1\ntools=/opt/wk-tools\n")
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
            self.assertEqual(self.reg.ws_place("ws"), "box")
        self.assertEqual(err.getvalue(), "")

    def test_a_target_with_no_host_refuses_and_names_the_conf(self):
        t = self.reg.load("remote")
        with self.assertRaises(Refused):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                t.probe()
        self.assertIn("place 'remote' has no host to reach", err.getvalue())
        self.assertIn(self.reg.conf_path("remote"), err.getvalue())
        self.assertEqual(self.fake.ssh_calls(), [])

    def test_start_and_stop_have_nothing_to_act_on(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertTrue(self.t.start("a"))
            self.assertFalse(self.t.stop("a"))
        self.assertIn("'box' has no notion of starting a single workspace -- nothing to bring up for 'a'", err.getvalue())
        self.assertIn("the 'box' place has no notion of stopping a single workspace -- 'a' is left running", err.getvalue())
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


class DriverConformance:
    """One body over every driver: `stopped()` makes `ws` there and not running; `down` is what `info` reads."""

    cls = None
    ws = "ws"
    down = None
    platform = "linux"

    def test_the_driver_implements_the_whole_interface(self):
        for name, fn in inspect.getmembers(places.Driver, inspect.isfunction):
            if "raise NotImplementedError" in inspect.getsource(fn):
                with self.subTest(method=name):
                    self.assertIsNot(getattr(self.cls, name), fn, "%s forgot %s" % (self.cls.__name__, name))

    def test_a_stopped_workspace_reads_stopped_is_present_and_is_started(self):
        t = self.stopped()
        self.assertIs(type(t), self.cls)
        self.assertEqual(t.info(self.ws), self.down)
        self.assertEqual(t.state(self.ws), "present")
        self.assertEqual(t.os(), self.platform)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(t.start(self.ws))
        self.assertTrue(self.brought_up(t))


class TestContainerConformance(DriversTest, DriverConformance):
    cls, down = places.Container, "exited"

    def stopped(self):
        self.up = False
        self.fake.react(["podman", "inspect", "wk-ws"], lambda a, f: Result(0, "running\n" if self.up else "exited\n"))
        self.fake.react(["podman", "start"], lambda a, f: (setattr(self, "up", True), Result(0))[1])
        d = os.path.join(self.env["WK_STORE"], "ws", "ws")
        self.fake.write(os.path.join(d, "home", places.READY_MARKER), "")
        self.fake.write(os.path.join(d, "base-id"), "main-1\n")
        return self.reg.load("container")

    def brought_up(self, t):
        return t.info(self.ws) == "running"


class TestRemoteConformance(RemoteTest, DriverConformance):
    cls, down = places.Remote, "present"

    def stopped(self):
        self.fake.answer_remote("ws/ws ]", out="present\n")
        return self.t

    def brought_up(self, t):
        return t.info(self.ws) == "present"


class TestLocalConformance(DriversTest, DriverConformance):
    cls, down, platform = places.LocalWorkspace, "running", "macos" if IS_MACOS else "linux"

    def stopped(self):
        marker = self.tmp / "marker"
        marker.write_text("name=ws\nsrc=/src/WebKit\n")
        self.env["WK_MARKER"] = str(marker)
        return self.reg.load("local")

    def brought_up(self, t):
        return t.info(self.ws) == "running" and self.fake.effects == []


class TestContainerWrite(DriversTest):
    def setUp(self):
        super().setUp()
        self.t = self.reg.load("container")
        self.base = "main-1"
        self.fake.answer(["nproc"], out="8\n")
        self.fake.files["/proc/meminfo"] = LINUX_MEMINFO
        self.fake.dirs.add(self.t.store.snapshot_tree(self.base))
        self.fake.answer(["podman", "container", "exists"], rc=1)
        self.fake.answer(["podman", "container", "exists", "wk-a"], rc=0)
        self.fake.answer(lib_argv(self.t.root, "host/linux/gpu.sh", "gpu_flags")[:3], out="--device /dev/dri")
        self.fake.answer(["env"], out="")
        self.fake.answer(["install"], out="")

    def wkdev_create(self):
        return next(e[1] for e in self.fake.effects if e[0] == "run" and any(a.endswith("wkdev-create") for a in e[1]))

    def flag_pairs(self, argv):
        flags = argv[argv.index("--additional-flags") + 1].split()
        return list(zip(flags[0::2], flags[1::2]))

    def test_create_hands_wkdev_create_every_flag_a_workspace_needs(self):
        _, err = self.stderr_of(lambda: self.t.create("new", self.base))
        argv = self.wkdev_create()
        u, ws_dir = self.t.user(), self.t.store.ws_dir("new")
        pairs = self.flag_pairs(argv)
        for pair in (("--volume", "%s:/src/WebKit:O,upperdir=%s/changes,workdir=%s/overlay-work" % (self.t.store.snapshot_tree(self.base), ws_dir, ws_dir)),
                     ("--volume", "%s:/opt/wk-tools:ro" % self.t.tools_src()), ("--memory", "19749m"), ("--cpus", "7"),
                     ("--volume", "%s:/secrets:ro" % self.t.store.keyring_view_dir("container")), ("--env", "WK_WORKSPACE=new"),
                     ("--env", "https_proxy=http://127.0.0.1:3128"), ("--device", "/dev/dri")):
            self.assertIn(pair, pairs)
        head = argv[argv.index("--network"):argv.index("--additional-flags")]
        self.assertEqual(head, ("--network", "none", "--isolated", "--name", "wk-new", "--shell", "/bin/bash", "--user", u, "--group", u,
                                "--home", os.path.join(ws_dir, "home")))
        self.assertEqual(argv[0], "env")
        self.assertIn("WKDEV_SDK=%s" % self.t.sdk(), argv)
        for d in ("changes", "overlay-work", "home", "build"):
            self.assertIn(os.path.join(ws_dir, d), self.fake.dirs)
        self.assertEqual(self.fake.files[os.path.join(ws_dir, "arch")], "native\n")
        install = next(e[1] for e in self.fake.effects if e[0] == "run" and e[1][0] == "install")
        self.assertEqual(install, ("install", "-m", "0755", os.path.join(self.t.root, "container", "firstrun.sh"), os.path.join(ws_dir, "home", ".wkdev-firstrun")))
        self.assertEqual(self.fake.effects[-1], ("write", os.path.join(ws_dir, "base-id")))
        self.assertEqual(self.fake.files[os.path.join(ws_dir, "base-id")], "main-1\n")
        self.assertEqual(self.fake.streamed, [argv], "`unit machine.streams_long_effects`: wkdev-create streams to the task log")

    def test_an_armhf_workspace_names_the_arm_image_and_gets_no_gpu(self):
        self.stderr_of(lambda: self.t.create("new", self.base, "armhf"))
        argv = self.wkdev_create()
        image = project.get("SDK_IMAGE_ARMHF")
        self.assertEqual(argv[argv.index("--arch"):argv.index("--name")], ("--arch", "arm", "--image", image))
        self.assertNotIn(("--device", "/dev/dri"), self.flag_pairs(argv))
        self.assertIn(("--env", "WK_ARCH=armhf"), self.flag_pairs(argv))

    def sdk_asks_for(self, tag, published):
        self.fake.answer(["env", "WKDEV_SDK=%s" % self.t.sdk()], out=tag + "\n")
        rows = "".join("ghcr.io/igalia/wkdev-sdk\t%s\n" % t for t in published)
        self.fake.answer(["podman", "search", "--list-tags"], out="NAME\tTAG\n" + rows)

    def test_an_unpublished_sdk_tag_is_refused_naming_the_newest_of_its_series_before_anything_is_made(self):
        self.sdk_asks_for("2.55-v2-8434060", ["2.55-v2-8434060_amd64", "2.54-v9-aaa", "2.55-v1-1111111", "2.55-v10-2222222", "2.55-v2-3333333"])
        err = self.refused(lambda: self.t.create("new", self.base))
        self.assertIn("ghcr.io/igalia/wkdev-sdk:2.55-v2-8434060, which upstream has not published", err)
        self.assertIn("WK_SDK_IMAGE=ghcr.io/igalia/wkdev-sdk:2.55-v10-2222222 wk new", err)
        self.assertEqual([e for e in self.fake.effects if e[0] != "run"], [])

    def test_a_published_tag_or_a_registry_that_cannot_be_asked_goes_on_to_wkdev_create(self):
        for ws, search in (("published", Result(0, "NAME\tTAG\nghcr.io/igalia/wkdev-sdk\t2.55-v2-8434060\n")),
                           ("unasked", Result(1, "", "timed out"))):
            with self.subTest(ws):
                self.sdk_asks_for("2.55-v2-8434060", [])
                self.fake.answer(["podman", "search", "--list-tags"], rc=search.rc, out=search.out, err=search.err)
                self.fake.effects = []
                self.stderr_of(lambda: self.t.create(ws, self.base))
                self.assertTrue(self.wkdev_create())

    def test_an_image_override_skips_the_registry_check(self):
        self.sdk_asks_for("2.55-v2-8434060", [])
        self.env["WK_SDK_IMAGE"] = "ghcr.io/x/sdk:tag"
        self.stderr_of(lambda: self.reg.load("container").create("other", self.base))
        self.assertFalse([e for e in self.fake.effects if e[1][:2] == ("podman", "search")])
        self.assertIn("ghcr.io/x/sdk:tag", self.wkdev_create())

    def test_create_refuses_without_a_base_or_over_a_container_and_makes_nothing(self):
        err = self.refused(lambda: self.t.create("new", "main-9"))
        self.assertIn("snapshot main-9 not found; run 'wk sync' first", err)
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
        t = places.Registry(REPO, env=env, machine=self.fake).load("container")
        self.fake.dirs.add(t.store.snapshot_tree(self.base))
        self.stderr_of(lambda: t.create("new", self.base))
        self.assertIn(os.path.join(t.store.ws_dir("new"), "home", ".local", "share", "wk", "git"), self.fake.dirs)
        self.assertNotIn(os.path.join(self.t.store.ws_dir("new"), "home", "git"), self.fake.dirs)
        self.assertEqual(t.mirror_dir(), t.store.mirror_dir(), "the workspace sees the mirror at this machine's own path")

    def test_ready_polls_the_marker_by_the_clock_until_it_is_there(self):
        marker = os.path.join(self.t.store.ws_dir("a"), "home", places.READY_MARKER)
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
        self.fake.write(os.path.join(ws_dir, "home", places.READY_MARKER), "")
        self.fake.dirs.add(ws_dir)
        self.fake.answer(["podman", "rm"], out="")
        self.fake.answer(["podman", "unshare"], out="")
        self.fake.effects = []
        _, err = self.stderr_of(lambda: self.t.destroy("a"))
        acts = [e for e in self.fake.effects if e[0] != "run" or e[1][:2] in (("podman", "rm"), ("podman", "unshare"))]
        self.assertEqual(acts, [("run", ("podman", "rm", "-f", "wk-a")), ("run", ("podman", "unshare", "rm", "-rf", ws_dir)), ("remove", ws_dir)])
        self.assertFalse(self.fake.isdir(ws_dir))
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
        root = self.t.store.store_dir()
        with mock.patch.object(secrets.Secrets, "store_publish") as sp:
            self.t.store_init()
            sp.assert_called_once_with()
        for d in ("git", "base", "ws", "cache/ccache", "cache/yocto/downloads", "cache/buildroot/ccache", "cache/bench", "skills"):
            self.assertIn(os.path.join(root, d), self.fake.dirs)
        conf = os.path.join(root, "cache", "ccache", "ccache.conf")
        self.assertEqual(self.fake.files[conf], "max_size = 40G\n")
        chmods = [e[1] for e in self.fake.effects if e[0] == "run" and e[1][0] == "chmod"]
        self.assertEqual(chmods, [("chmod", "0700", self.t.store.keyring_dir())])
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
        self.assertEqual(self.fake.streamed, [("bash", script, self.t.sdk())])
        self.fake.answer(["bash", script], rc=1, err="fetch failed\n")
        err = self.refused(self.t.sdk_refresh)
        self.assertIn("fetch failed", err)
        self.assertIn("refreshing the webkit-container-sdk checkout failed", err)

    def test_the_creation_log_is_under_the_store(self):
        self.assertEqual(self.t.create_log("a"), os.path.join(self.t.store.store_dir(), "log", "new-a.log"))


class TestVmWrite(VmTest):
    def setUp(self):
        super().setUp()
        self.tart = self.t.tart()
        self.fake.answer(["sysctl", "-n", "hw.ncpu"], out="10\n")
        self.fake.answer(["sysctl", "-n", "hw.memsize"], out="34359738368\n")
        self.fake.answer([self.tart, "clone"], out="")
        self.fake.answer([self.tart, "set"], out="")
        self.fake.dirs.add(self.t.store.mirror_dir())
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
        self.assertEqual(self.fake.effects[-2:], [("mkdir", ws_dir), ("write", os.path.join(ws_dir, places.READY_MARKER))])
        self.assertEqual([a[:2] for a in self.fake.streamed], [(self.tart, "clone"), (self.tart, "set")])
        self.assertEqual(1, self.base_ensure.call_count)
        self.assertTrue(self.t.created("new"))

    def test_create_takes_the_sizing_and_display_overrides(self):
        self.env.update({"WK_VM_CPUS": "4", "WK_VM_MEM_MB": "8192", "WK_VM_DISPLAY": "1920x1080"})
        self.stderr_of(lambda: self.reg.load("vm").create("new"))
        self.assertEqual(self.tart_calls()[1][3:], ("--cpu", "4", "--memory", "8192", "--random-mac", "--display", "1920x1080", "--display-refit"))

    def test_create_refuses_an_existing_guest_a_missing_mirror_and_a_base_that_did_not_build(self):
        err = self.refused(lambda: self.t.create("mac"))
        self.assertIn("workspace 'mac' already exists", err)
        self.fake.dirs.discard(self.t.store.mirror_dir())
        err = self.refused(lambda: self.t.create("new"))
        self.assertIn("no WebKit mirror on this machine for 'new'", err)
        self.assertIn("wk sync    makes it", err)
        self.fake.dirs.add(self.t.store.mirror_dir())
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
        self.fake.answer(["podman", "machine", "inspect"], out=json.dumps([{"State": "running", "Resources": {"Memory": 8192}}]))
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
                                *[("remove", os.path.join(vm_dir, "mac." + f)) for f in ("run.log", "unfiltered", "agent-forward.log", "broker-forward.log")],
                                ("remove", ws_dir)])
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
                         [("remove", os.path.join(self.t.vm_dir(), "gone." + f)) for f in ("run.log", "unfiltered", "agent-forward.log", "broker-forward.log")])

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
        before = len(self.fake.effects)
        self.t.store_init()
        root = self.t.store.store_dir()
        self.assertEqual(self.fake.effects[before:], [("mkdir", root), ("mkdir", os.path.join(root, "ws")), ("mkdir", self.t.vm_dir()),
                                             ("run", ("find", self.t.vm_dir(), "-maxdepth", "0", "-perm", "0700")), ("run", ("chmod", "0700", self.t.vm_dir()))])

    def test_ready_is_the_shared_poll_of_info(self):
        marker = os.path.join(self.t.store.ws_dir("mac"), places.READY_MARKER)
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
        t = places.Registry(REPO, env=env, machine=self.fake).load("vm")
        self.assertFalse(t.vm_store_apart())
        err = self.refused(lambda: t.created("mac"))
        self.assertIn("set WK_VM_STORE apart from WK_STORE", err)
        self.assertEqual(t.list(), [("mac", "running")])

    def test_off_a_macos_host_the_vm_place_holds_no_name_and_refuses_to_be_driven(self):
        with mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=False):
            t = self.reg.load("vm")
            self.assertFalse(self.reg.exists_on(t, "mac"))
            self.assertFalse(self.reg.on_place("vm", "mac"))
            err = self.refused(lambda: t.create("new"))
        self.assertIn("exists only on a macOS host", err)
        self.assertNotIn("WK_VM_STORE", err)


class TestRemoteWrite(RemoteTest):
    def setUp(self):
        super().setUp()
        self.conf("ref", "host=ref.example\nroot=/home/u/wk\nreference=/srv/WebKit\n")
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
        self.assertIn("cd /home/u/wk/ws/a/WebKit\n", acts[1])
        self.assertIn("git remote add shared /srv/WebKit", acts[1])
        self.assertIn("ssh -F /home/u/wk/ssh/config", acts[1])
        self.assertIn("[ -f /home/u/wk/cache/ccache/ccache.conf ] || printf %s 'max_size = 40G", acts[2])
        self.assertIn("touch /home/u/wk/ws/a/.wk-ready", acts[3])
        self.assertEqual(shlex.split(shlex.split(self.fake.effects[-1][1][-1])[-1])[-1], acts[3])
        self.assertIn(("mkdir", self.ref.store.ws_dir("a")), self.fake.effects[:-1])

    def test_create_without_a_shared_checkout_refreshes_the_mirror_first(self):
        _, err = self.stderr_of(lambda: self.t.create("a"))
        acts = self.acts()
        self.assertEqual(len(acts), 5)
        self.assertIn("M=/home/u/wk/mirror\n", acts[0])
        self.assertIn("git clone --quiet --shared -b main /home/u/wk/mirror /home/u/wk/ws/a/WebKit", acts[1])
        self.assertIn("git remote add mirror /home/u/wk/mirror", acts[2])
        self.assertIn("touch /home/u/wk/ws/a/.wk-ready", acts[4])
        self.assertIn("  origin   ok\n  wpe      FAILED\n", err)
        self.assertEqual(len(self.fake.ssh_calls("/etc/motd")), 1)

    def test_create_refuses_what_is_there_and_names_the_remedy(self):
        err = self.refused(lambda: self.ref.create("half"))
        self.assertIn("'half' on ref.example is a checkout that never finished being", err)
        self.assertIn("rm -rf /home/u/wk/ws/half", err)
        err = self.refused(lambda: self.ref.create("there"))
        self.assertIn("workspace 'there' already exists on ref.example", err)
        self.fake.remote = [("uname -s", Result(255, "", "ssh: Connection refused\n"))]
        err = self.refused(lambda: self.reg.load("ref").create("a"))
        self.assertIn("cannot reach 'ref.example' over ssh: Connection refused", err)
        self.assertEqual(self.acts(), [])

    def test_a_failed_round_trip_ends_the_creation_where_it_stood(self):
        for mod, name, text in ((git, "wiring_script", "git remote set-url origin x"), (places.Driver, "ccache_conf", "max_size = 40G\n")):
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
        self.assertIn("re-run 'wk new a --on ref'", err)
        self.fake.answer_remote("touch", out="")
        self.fake.answer_remote("git remote set-url origin", rc=1)
        _, err = self.stderr_of(lambda: self.ref.create("a"))
        self.assertIn("could not wire the remotes in /home/u/wk/ws/a/WebKit", err)
        self.assertIn("touch", self.acts()[-1])

    def test_a_command_is_handed_over_whole_to_the_peers_own_wk(self):
        self.conf("peer", "peer=1\ntools=/opt/wk-tools\n")
        self.fake.answer_remote("test -x /opt/wk-tools/wk", rc=0)
        self.tools_at("0000stale000")   # a peer is handed over at any commit
        argv = self.reg.load("peer").hand_over("new", ["a", "--no-wait"], tty=False)
        self.assertEqual((argv[0], argv[-2]), ("ssh", "peer"))
        self.assertNotIn("-t", argv)
        self.assertIn("WK_ROW_LABEL=peer /opt/wk-tools/wk new a --no-wait", argv[-1])
        self.assertEqual(self.fake.ssh_calls("wk new"), [])
        self.fake.answer_remote("test -x /opt/wk-tools/wk", rc=1)
        err = self.refused(lambda: self.reg.load("peer").hand_over("new", ["a"], tty=False))
        self.assertIn("'new' acts on a workspace on peer, which has no wk-tools of its own", err)

    def test_a_box_at_another_wk_tools_commit_is_refused_the_hand_over_naming_the_sync_and_asked_again_each_time(self):
        self.tools_at("0000stale000")
        err = self.refused(lambda: self.t.hand_over("build", ["a"], tty=False))
        self.assertIn("wk sync --tools box", err)
        self.assertEqual([], self.fake.ssh_calls("wk build"))
        self.tools_at("abc1234")
        self.assertIn("wk build a", self.t.hand_over("build", ["a"], tty=False)[-1])
        self.assertEqual(2, len(self.fake.ssh_calls("/wk doctor --probe-tools")))

    def test_force_crosses_the_refusal_and_says_so(self):
        self.tools_at("0000stale000")
        with mock.patch.dict(os.environ, {"WK_FORCE": "1"}), mock.patch.object(act, "_forced", [None]):
            argv, err = self.stderr_of(lambda: self.t.hand_over("build", ["a"], tty=False))
        self.assertIn("wk build a", argv[-1])
        self.assertIn("FORCED", err)

    def test_a_read_only_command_is_handed_over_and_told_of_the_difference(self):
        self.tools_at("0000stale000")
        argv, err = self.stderr_of(lambda: self.t.hand_over("status", ["a"], tty=False, readonly=True))
        self.assertIn("wk status a", argv[-1])
        self.assertIn("wk sync --tools box", err)

    def test_a_peers_workspace_is_destroyed_by_its_own_wk(self):
        self.conf("peer", "peer=1\ntools=/opt/wk-tools\n")
        t = self.reg.load("peer")
        self.fake.answer_remote("test -x /opt/wk-tools/wk", rc=0)
        self.fake.answer_remote("wk ls --json", out=json.dumps({"workspaces": [{"name": "a", "state": "running"}]}))
        self.fake.answer_remote("wk rm a", out="==> workspace 'a' destroyed\n")
        self.tools_at("0000stale000")
        _, err = self.stderr_of(lambda: t.destroy("a"))
        line = shlex.split(self.fake.ssh_calls("wk rm a")[0][-1])
        self.assertEqual(line[-3:], ["/opt/wk-tools/wk", "rm", "a"])
        self.assertIn("WK_YES=1", line)
        self.assertNotIn("WK_EXPORTS_READ=1", line)
        self.assertEqual(self.fake.effects[-1], ("remove", t.store.ws_dir("a")))
        self.assertIn("==> workspace 'a' destroyed", err)
        self.fake.answer_remote("wk rm a", rc=1, out="error: has work running in it\n")
        self.fake.effects = []
        err = self.refused(lambda: t.destroy("a"))
        self.assertIn("peer did not destroy 'a'", err)
        self.assertIn("has work running in it", err)
        self.assertEqual([e for e in self.fake.effects if e[0] == "remove"], [])

    def test_store_init_makes_the_record_here(self):
        self.t.store_init()
        self.assertEqual(self.fake.effects, [("mkdir", self.t.store.store_dir()), ("mkdir", os.path.join(self.t.store.store_dir(), "ws"))])

    def test_a_dry_run_reads_the_machine_and_runs_nothing_there(self):
        self.dry_run()
        _, err = self.stderr_of(lambda: self.ref.create("a"))
        self.assertEqual(self.acts(), [])
        self.assertIn("would run on ref.example: sh -c", err)
        self.assertIn("touch /home/u/wk/ws/a/.wk-ready", err)
        self.assertEqual(len(self.fake.ssh_calls("ws/a ]")), 1)
        self.fake.answer_remote("ws/a ]", out="present\n")
        _, err = self.stderr_of(lambda: self.t.destroy("a"))
        self.assertIn("would run on host: ssh", err)
        self.assertIn("/home/u/wk/tools/wk rm a", err)
        self.assertEqual(self.fake.ssh_calls("wk rm a"), [])

    def test_this_machine_as_the_remote_runs_every_round_trip_in_a_local_shell(self):
        self.conf("me", "local=1\nroot=%s\nreference=/srv/WebKit\n" % (self.tmp / "rr"))
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


class TestLocalWrite(DriversTest):
    def setUp(self):
        super().setUp()
        marker = self.tmp / "marker"
        marker.write_text("name=ws\nsrc=/src/WebKit\n")
        self.env["WK_MARKER"] = str(marker)
        self.t = self.reg.load("local")

    def test_a_workspace_neither_creates_destroys_nor_stops_itself_and_names_the_host_command(self):
        err = self.refused(lambda: self.t.create("x"))
        self.assertIn("a workspace cannot create a workspace -- run 'wk new x' on the host", err)
        err = self.refused(lambda: self.t.destroy("ws"))
        self.assertIn("a workspace cannot destroy itself -- run 'wk rm ws' on the host", err)
        err = self.refused(lambda: self.t.stop("ws"))
        self.assertIn("a workspace cannot stop itself -- run 'wk stop ws' on the host", err)
        self.assertEqual(self.fake.effects, [])

    def test_store_init_makes_its_own_record(self):
        self.t.store_init()
        self.assertEqual(self.fake.effects, [("mkdir", self.t.store.ws_dir("ws"))])

    def test_exec_tty_is_a_plain_local_exec(self):
        self.fake.answer(["bash"], out="")
        r = self.t.exec_tty("ws", ["true"])
        self.assertEqual(r.rc, 0)
        self.assertEqual(self.fake.effects[-1], ("run_tty", ("bash", "-lc", "exec true"), None))


class TestBridges(DriversTest):
    def test_the_ccache_conf_is_the_places_size(self):
        def conf(env):
            return places.Registry(str(REPO), env=env, machine=self.fake).load("container").ccache_conf()
        self.assertEqual(conf(dict(self.env)), "max_size = 40G\n")
        self.assertEqual(conf(dict(self.env, WK_CCACHE_MAXSIZE="5G")), "max_size = 5G\n")

    def test_gpu_flags_are_read_where_the_container_is_made(self):
        t = self.reg.load("container")
        self.fake.answer(lib_argv(str(REPO), "host/linux/gpu.sh", "gpu_flags")[:3], rc=1)
        self.assertNotIn("--device", t.sandbox_flags("native"))
        self.fake.answer(lib_argv(str(REPO), "host/linux/gpu.sh", "gpu_flags")[:3], out="--device /dev/dri --device nvidia.com/gpu=all\n")
        self.assertEqual(t.sandbox_flags("native")[-4:], ["--device", "/dev/dri", "--device", "nvidia.com/gpu=all"])
        self.assertNotIn("--device", t.sandbox_flags("armhf"), "an armhf workspace gets no GPU")


class TestBuildSide(RemoteTest):
    def test_the_ccache_and_the_size_are_the_machines_own(self):
        self.assertEqual(self.t.ccache_dir("a"), "/home/u/wk/cache/ccache")
        self.assertEqual(self.reg.load("container").ccache_dir("a"), "/ccache")
        self.assertEqual(self.t.build_size("a"), (8, 20000, 0))

    def test_the_build_is_serialised_niced_and_teed_on_the_far_side(self):
        argv, cwd = self.t.build_argv("a", ["env", "A=1", "/t/build-in-workspace.sh"])
        self.assertIsNone(cwd)
        self.assertEqual(argv[0], "ssh")
        self.assertEqual(argv[-2], "box.example")
        text = shlex.split(shlex.split(argv[-1])[-1])[2]
        self.assertTrue(text.startswith("set -o pipefail\ncd /home/u/wk/ws/a/WebKit && "))
        self.assertIn("python3 -I -c %s /home/u/wk/tools/lib wk.lock run remote-build -w 3600 -- nice -n 19 ionice -c3 env "
                      "A=1 /t/build-in-workspace.sh" % shlex.quote(machine.ISOLATED), text)
        self.assertTrue(text.endswith("2>&1 | tee /home/u/wk/ws/a/build.log"))

    def test_a_wk_directory_in_the_checkout_does_not_shadow_wk_tools(self):
        with tempfile.TemporaryDirectory() as src:
            os.makedirs(os.path.join(src, "wk"))
            with open(os.path.join(src, "wk", "__init__.py"), "w") as f:
                f.write("raise SystemExit('the checkout was imported')\n")
            env = dict(os.environ, WK_STORE=os.path.join(src, "store"), WK_LOCK_DIR=os.path.join(src, "locks"), PYTHONPATH=src)
            cp = subprocess.run(machine.isolated_module(str(REPO / "lib"), "wk.lock") + ["run", "t", "--", "echo", "ran"],
                                cwd=src, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual((0, "ran\n"), (cp.returncode, cp.stdout), cp.stderr)

    def test_a_target_on_this_machine_runs_the_build_here(self):
        self.conf("me", "local=1\nroot=%s\n" % (self.tmp / "rr"))
        me = self.reg.load("me")
        me._probed = {"home": "/h", "cores": 2, "load": 0, "mem_mb": 100, "ionice": "no", "os": "linux", "root": str(self.tmp / "rr")}
        argv, _ = me.build_argv("a", ["true"])
        self.assertEqual(argv[:2], ["bash", "-c"])
        self.assertNotIn("tee", argv[2])
        self.assertNotIn("ionice", argv[2])


class TestBuildSize(DriversTest):
    def local(self, system):
        marker = self.tmp / "marker"
        marker.write_text("name=ws\nsrc=/src/WebKit\n")
        self.env["WK_MARKER"] = str(marker)
        p = mock.patch.object(places.LocalWorkspace, "os", lambda s: system)
        p.start()
        self.addCleanup(p.stop)
        self.fake.answer(["nproc"], out="4\n")
        self.fake.answer(["sysctl", "-n", "hw.ncpu"], out="4\n")
        self.fake.answer(["sysctl", "-n", "hw.memsize"], out="8589934592\n")
        self.fake.files["/proc/meminfo"] = "MemTotal: 8388608 kB\n"
        return self.reg.load("local")

    def test_a_workspace_is_sized_by_its_own_cgroup_else_the_machine(self):
        t = self.local("linux")
        self.fake.files["/sys/fs/cgroup/cpu.max"] = "200000 100000\n"
        self.fake.files["/sys/fs/cgroup/memory.max"] = "4294967296\n"
        self.assertEqual(t.build_size("ws"), (2, 4096, None))
        self.fake.files["/sys/fs/cgroup/cpu.max"] = "max 100000\n"
        self.fake.files["/sys/fs/cgroup/memory.max"] = "max\n"
        self.assertEqual(t.build_size("ws"), (4, 8192, None))

    def test_a_guest_has_no_cgroup_and_is_the_machines_size(self):
        self.assertEqual(self.local("macos").build_size("ws"), (4, 8192, None))

    def test_an_unreadable_or_garbled_cgroup_limit_is_refused_not_replaced_by_the_machines(self):
        t = self.local("linux")
        self.fake.files["/sys/fs/cgroup/memory.max"] = "max\n"
        err = self.refused(lambda: t.build_size("ws"))
        self.assertIn("cannot read /sys/fs/cgroup/cpu.max", err)
        self.fake.files["/sys/fs/cgroup/cpu.max"] = "lots\n"
        err = self.refused(lambda: t.build_size("ws"))
        self.assertIn("/sys/fs/cgroup/cpu.max reads 'lots', which is not what cgroup v2 writes there", err)

    def test_a_guest_is_sized_as_it_is_configured_and_else_as_this_host_would_size_one(self):
        with mock.patch.object(places.Vm, "tart", lambda s: "/t/tart"):
            vm = places.Vm("vm", str(REPO), self.env, self.fake)
            self.fake.answer(["/t/tart", "get", "wk-g"], out='{"CPU": 6, "Memory": 12288}')
            self.assertEqual(vm.build_size("g"), (6, 12288, None))
            self.fake.answer(["/t/tart", "get", "wk-g"], rc=1)
            self.env.update({"WK_VM_CPUS": "3", "WK_VM_MEM_MB": "4096"})
            self.assertEqual(vm.build_size("g"), (3, 4096, None))


class TestPidAliveIsTheOneAnswer(DriversTest):
    def test_pid_alive_reads_kill_dash_0_true_false_or_no_answer(self):
        for rc, want in ((0, True), (1, False), (TIMED_OUT, None)):
            t = WsDriver("t", str(REPO), {}, Fake("here"))
            t.machine.answer(["exec", "a", "kill", "-0", "123"], rc=rc)
            with mock.patch.object(t.machine, "run", wraps=t.machine.run) as run:
                self.assertEqual(want, t.pid_alive("a", 123, 5))
            run.assert_called_once_with(["exec", "a", "kill", "-0", "123"], timeout=5)


class TestAMacHostReadsTheContainerStoreInThePodmanMachine(unittest.TestCase):
    def test_a_finished_workspace_is_present_from_the_host(self):
        fake = Fake("here")
        fake.answer(["podman", "-c", "wk", "inspect"], out="running\n")
        fake.answer(["podman", "machine", "ssh", "wk", "--"])
        c = places.Container("container", str(REPO), {"HOME": "/nonexistent"}, fake)
        with mock.patch.object(places.os, "uname", return_value=mock.Mock(sysname="Darwin")):
            self.assertEqual("present", c.state("w"))
        asked = [e[1][-1] for e in fake.effects if e[1][:4] == ("podman", "machine", "ssh", "wk")]
        self.assertIn("test -d /var/lib/wk/ws/w", asked)

    def test_a_command_in_a_container_is_entered_by_the_podman_machines_own_wk(self):
        c = places.Container("container", str(REPO), {"HOME": "/nonexistent"}, Fake("here"))
        with mock.patch.object(places.os, "uname", return_value=mock.Mock(sysname="Darwin")), \
                mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
            argv, _ = c.exec_argv("w", ["sh", "-c", "echo $0", "a b"])
        self.assertEqual(["podman", "machine", "ssh", "wk", "--"], argv[:5])
        self.assertIn("/opt/wk-tools/wk enter w -- sh -c 'echo $0' 'a b'", argv[5])
        self.assertNotIn("WK_DRY_RUN", argv[5], "a read in a dry run is still a read; the far wk would refuse --dry-run")

class TestThePodmanMachineRecord(DriversTest):
    def test_the_record_and_its_ssh(self):
        self.assertIsNone(places.podman_vm(self.fake, "wk"))
        rec = {"Name": "wk", "State": "running", "SSHConfig": {"Port": 50123, "IdentityPath": "/k", "RemoteUsername": "core"}}
        self.fake.answer(["podman", "machine", "inspect", "wk"], out=json.dumps([rec]))
        self.assertEqual(places.podman_vm(self.fake, "wk"), rec)
        opts, dest = places.podman_vm_route(rec)
        self.assertEqual((opts[:4], dest), (["-p", "50123", "-i", "/k"], "core@127.0.0.1"))
        err = self.refused(lambda: places.podman_vm_route({"Name": "wk", "SSHConfig": {"Port": 0}}))
        self.assertIn("podman names no ssh port, key and user for its machine 'wk'", err)
        self.assertIn("./setup --stage machine", err)
        self.fake.answer(["podman", "machine", "inspect", "wk"], out="running\n")
        err = self.refused(lambda: places.podman_vm(self.fake, "wk"))
        self.assertIn("'podman machine inspect wk' answered what this end cannot read", err)

    def test_the_verbs_bash_asks(self):
        rec = {"State": "running", "Resources": {"Memory": 8192}}
        out = io.StringIO()
        with mock.patch.object(places, "podman_vm", return_value=rec), contextlib.redirect_stdout(out):
            rc = places.main(["podman-vm", "State", "Resources.Memory", "SSHConfig.Port"], env=self.env)
        self.assertEqual((rc, out.getvalue()), (0, "running\n8192\n\n"))
        with mock.patch.object(places, "podman_vm", return_value=None):
            self.assertEqual(places.main(["podman-vm", "State"], env=self.env), 1)
        env = dict(self.env, PATH=str(self.tmp / "bin"))
        self.assertEqual(places.main(["tart"], env=env), 1)
        (self.tmp / "bin").mkdir()
        (self.tmp / "bin" / "tart").write_text("")
        (self.tmp / "bin" / "tart").chmod(0o755)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(places.main(["tart"], env=env), 0)
        self.assertEqual(out.getvalue(), os.path.realpath(self.tmp / "bin" / "tart") + "\n")

    def test_the_named_form_prints_shell_assignments_wk_eval_can_read(self):
        rec = {"Resources": {"CPUs": 9, "Memory": 20480}, "SSHConfig": {"Port": 50123}}
        out = io.StringIO()
        with mock.patch.object(places, "podman_vm", return_value=rec), contextlib.redirect_stdout(out):
            rc = places.main(["podman-vm", "_cpus=Resources.CPUs", "_disk=Resources.DiskSize"], env=self.env)
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "_cpus=9\n_disk=''\n")


if __name__ == "__main__":
    unittest.main()
