"""lib/wk/places.py: the registry (a place's kind, a workspace's place, the default preset), every driver over a fake
machine and its conformance, where tart is, a home mountpoint, and a clobbered workspace reading broken."""
import contextlib
import inspect
import io
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import fakes
from tests.fakes import Fake, FakeClock, WsDriver
from tests.support import rand_suffix, REPO, scratch_dir, stub_path, temp_store

sys.path.insert(0, str(REPO / "lib"))
from wk import act, machine, places, record  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Local, Result, TIMED_OUT  # noqa: E402
from wk.record import Records  # noqa: E402
from wk.store import Store  # noqa: E402


IS_MACOS = os.uname().sysname == "Darwin"
HOST_NAME = record.host_name


class DriversTest(fakes.FlowTest):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(record, "host_name", HOST_NAME)   # the fake machine answers this host's name
        p.start()
        self.addCleanup(p.stop)
        self.registry_dir = self.tmp / "hosts"
        self.registry_dir.mkdir()
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"),
                    "WK_MACHINES_DIR": str(self.registry_dir), "WK_IN_VM": "1", "PATH": os.environ.get("PATH", "")}
        (self.tmp / "home").mkdir()
        self.fake = Fake("here")
        self.reg = places.Registry(REPO, env=self.env, machine=self.fake)

    def conf(self, name, text):
        kind = "" if "kind=" in text.replace("driver=", "") else \
            "kind=%s\n" % ("peer" if "peer=1" in text else "build")
        (self.registry_dir / (name + ".conf")).write_text(kind + text.replace("peer=1\n", ""))

    def stderr_of(self, fn):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            result = fn()
        return result, err.getvalue()

    def refused(self, fn):
        with self.assertRaises(Refused):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                fn()
        return err.getvalue()


class TestSessionSocket(unittest.TestCase):
    def test_only_a_real_socket_under_the_runtime_dir_is_present(self):
        import socket
        tmp = Path(tempfile.mkdtemp(prefix="wk-test-session-socket-"))
        self.addCleanup(lambda: os.system("rm -rf %s" % tmp))
        env = mock.Mock(machine=Local(), env={"XDG_RUNTIME_DIR": str(tmp)})
        path = Path(places.session_socket_path(env.env))
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

    def test_sdk_local_is_the_pulled_image_and_its_pull_date_and_sdk_upstream_the_registrys_tags_or_none(self):
        self.fake.answer(["podman", "images"], out="docker.io/library/busybox:latest\n")
        self.assertIsNone(self.t.sdk_local())
        self.fake.answer(["podman", "images"], out="ghcr.io/igalia/wkdev-sdk:2.53-v9-abc0000\n")
        self.fake.answer(["podman", "image", "inspect"], out="2026-08-01T12:00:00Z\n")
        self.assertEqual(self.t.sdk_local(), {"image": "ghcr.io/igalia/wkdev-sdk:2.53-v9-abc0000", "created": "2026-08-01"})
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
    def test_the_transport_bridges_stdio_to_the_guests_own_sshd_on_loopback(self):
        with mock.patch.object(os, "execvp") as ex:
            self.t.ssh_transport("mac")
        (prog, argv), _ = ex.call_args
        self.assertEqual((prog, argv), (self.t.tart(), [self.t.tart(), "exec", "-i", "wk-mac", "/usr/bin/nc", "127.0.0.1", "22"]))

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


class TestTheContract(unittest.TestCase):
    DRIVERS = (places.Container, places.Vm, places.LocalWorkspace, places.Remote, places.Peer)

    def test_every_driver_answers_every_method_of_the_contract(self):
        for cls in self.DRIVERS:
            for name in places.Driver.CONTRACT:
                with self.subTest(driver=cls.__name__, method=name):
                    self.assertTrue(callable(getattr(cls, name, None)))


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


class TestPeerConformance(RemoteTest, DriverConformance):
    cls, down = places.Peer, "present"

    def stopped(self):
        self.conf("pal", "kind=peer\nhost=pal.example\ntools=/opt/wk-tools\n")
        self.fake.answer_remote("wk ls --json", out=json.dumps({"workspaces": [{"name": "ws", "state": "stopped"}]}))
        return self.reg.load("pal")

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


class TestContainerWait(DriversTest):
    def test_its_own_wait_stops_when_the_container_vanished(self):
        clock = FakeClock()
        self.refused(lambda: self.reg.load("container").wait_ready("gone", clock, own=True))
        self.assertEqual(clock.slept, [])


class TestRemoteWrite(RemoteTest):
    def setUp(self):
        super().setUp()
        self.conf("ref", "host=ref.example\nroot=/home/u/wk\n")
        self.ref = self.reg.load("ref")
        self.fake.answer_remote("ws/half ]", out="creating\n")
        self.fake.answer_remote("ws/there ]", out="present\n")

    def test_create_refuses_what_is_there_and_names_the_remedy(self):
        err = self.refused(lambda: self.ref.create("half"))
        self.assertIn("'half' on ref.example is a checkout that never finished being", err)
        self.assertIn("rm -rf /home/u/wk/ws/half", err)
        err = self.refused(lambda: self.ref.create("there"))
        self.assertIn("workspace 'there' already exists on ref.example", err)
        self.assertEqual(self.fake.ssh_calls("git clone"), [])

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


class TestLocalWrite(DriversTest):
    def setUp(self):
        super().setUp()
        marker = self.tmp / "marker"
        marker.write_text("name=ws\nsrc=/src/WebKit\n")
        self.env["WK_MARKER"] = str(marker)
        self.t = self.reg.load("local")

    def test_store_init_makes_its_own_record(self):
        self.t.store_init()
        self.assertEqual(self.fake.effects, [("mkdir", self.t.store.ws_dir("ws"))])

    def test_exec_tty_is_a_local_exec_through_a_bridge_of_its_own_in_a_container(self):
        bridge = os.path.join(self.t.root, "container", "proxy", "ensure-bridge.sh")
        self.fake.answer(["bash"], out="")
        self.fake.answer([bridge], out="")
        for system, want in (("macos", ("bash", "-lc", "exec true")), ("linux", (bridge, "bash", "-lc", "exec true"))):
            with self.subTest(system=system), mock.patch.object(self.t, "os", return_value=system):
                self.assertEqual(self.t.exec_tty("ws", ["true"]).rc, 0)
                self.assertEqual(self.fake.effects[-1], ("run_tty", want, None))


class TestBuildSide(RemoteTest):
    def test_the_ccache_and_the_size_are_the_machines_own(self):
        self.assertEqual(self.t.ccache_dir("a"), "/home/u/wk/cache/ccache")
        self.assertEqual(self.reg.load("container").ccache_dir("a"), "/var/lib/wk/ws/a/ccache")
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


class TestPidAliveIsTheOneAnswer(DriversTest):
    def test_pid_alive_reads_kill_dash_0_true_false_or_no_answer(self):
        for rc, want in ((0, True), (1, False), (TIMED_OUT, None)):
            t = WsDriver("t", str(REPO), {}, Fake("here"))
            t.machine.answer(["exec", "a", "kill", "-0", "123"], rc=rc)
            with mock.patch.object(t.machine, "run", wraps=t.machine.run) as run:
                self.assertEqual(want, t.pid_alive("a", 123, 5))
            run.assert_called_once_with(["exec", "a", "kill", "-0", "123"], timeout=5)


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
        self.assertEqual(places.main(["tart"], env=env), 0)
        with mock.patch.object(places.guest, "podman_admit", side_effect=Refused(1)), self.assertRaises(Refused):
            places.main(["podman-start"], env=self.env)
        with mock.patch.object(places.guest, "podman_admit"), mock.patch.object(places.Local, "act_run", return_value=Result(0)) as start:
            self.assertEqual((places.main(["podman-start"], env=self.env), start.call_args[0][0]), (0, ["podman", "machine", "start", "wk"]))

    def test_the_named_form_prints_shell_assignments_wk_eval_can_read(self):
        rec = {"Resources": {"CPUs": 9, "Memory": 20480}, "SSHConfig": {"Port": 50123}}
        out = io.StringIO()
        with mock.patch.object(places, "podman_vm", return_value=rec), contextlib.redirect_stdout(out):
            rc = places.main(["podman-vm", "_cpus=Resources.CPUs", "_disk=Resources.DiskSize"], env=self.env)
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "_cpus=9\n_disk=''\n")


def plant(home, rel):
    p = home / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


class TestThePythonLocator(fakes.FlowTest):
    def setUp(self):
        super().setUp()
        self.home = self.tmp

    def test_the_link_setup_makes_off_the_path_is_found_and_resolved_to_the_bundle(self):
        real = plant(self.home, ".local/share/tart/tart.app/Contents/MacOS/tart")
        link = self.home / ".local" / "bin" / "tart"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(real)
        self.assertEqual(places.tart_path({"HOME": str(self.home), "PATH": "/usr/bin:/bin"}), os.path.realpath(real))

    def test_one_on_the_path_wins(self):
        onpath = plant(self.home, "bin/tart")
        plant(self.home, ".local/bin/tart")
        self.assertEqual(places.tart_path({"HOME": str(self.home), "PATH": str(self.home / "bin")}), os.path.realpath(onpath))

    def test_a_bundle_nothing_links_is_not_guessed_at_and_the_refusal_names_the_link(self):
        plant(self.home, ".local/share/tart/tart.app/Contents/MacOS/tart")
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}
        self.assertIsNone(places.tart_path(env))
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(Refused):
            places.Vm("vm", str(REPO), env, Fake()).tart_or_die()
        self.assertIn("ln -sfn ~/.local/share/tart/tart.app/Contents/MacOS/tart ~/.local/bin/tart", err.getvalue())


class Clobbered(fakes.FlowTest):
    def state(self, kind, env, binp, name):
        with mock.patch.dict(os.environ, {"PATH": "%s:%s" % (binp, os.environ["PATH"])}):
            env = dict(env, HOME=str(self.tmp), PATH=os.environ["PATH"])
            return places.Registry(REPO, env=env).load(kind).state(name)


class TestPodmanRmByHand(Clobbered):
    def test_a_container_removed_by_hand_reads_broken(self):
        fake_podman = "case \"$*\" in *inspect*) exit 1 ;; *) exit 0 ;; esac\n"
        with temp_store() as store, stub_path({"podman": fake_podman}) as binp:
            name = f"demo-{rand_suffix()}"
            ws = store["path"] / "ws" / name
            (ws / "home").mkdir(parents=True)
            (ws / "home" / places.READY_MARKER).write_text("")
            (ws / "base-id").write_text("deadbeef\n")
            st = self.state("container", {"WK_STORE": store["WK_STORE"], "WK_IN_VM": "1"}, binp, name)
            self.assertEqual(st, "broken")


class TestTartDeleteByHand(Clobbered):
    def test_a_guest_deleted_by_hand_reads_broken(self):
        fake_tart = 'case "$1" in list) echo "[]" ;; *) exit 1 ;; esac\n'
        with temp_store() as store, stub_path({"tart": fake_tart}) as binp, \
                mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=True):
            name = f"demo-{rand_suffix()}"
            ws = store["path"] / "ws" / name
            ws.mkdir(parents=True)
            (ws / places.READY_MARKER).write_text("")
            st = self.state("vm", {"WK_VM_STORE": store["WK_STORE"]}, binp, name)
            self.assertEqual(st, "broken")


def _ensure(ws, dest, user="wsuser"):
    c = places.Container("container", str(REPO), dict(os.environ, WK_CONTAINER_USER=user), Local())
    c._ensure_home_mountpoint(str(ws), str(dest))


class TestTheMountpointsInsideTheHomeAreMadeHere(fakes.FlowTest):
    HOME = "/home/wsuser"

    def setUp(self):
        super().setUp()
        self.ws = self.tmp / "ws"
        (self.ws / "home").mkdir(parents=True)

    def test_a_destination_under_the_home_is_made_in_the_workspaces_home(self):
        _ensure(self.ws, f"{self.HOME}/.local/share/wk/git")
        made = self.ws / "home" / ".local" / "share" / "wk" / "git"
        self.assertTrue(made.is_dir(), "podman is left to make the mountpoint, as root")
        self.assertEqual((self.ws / "home" / ".local").owner(),
                         (self.ws / "home").owner(),
                         "the mountpoint's parents are not the home's owner's")

    def test_a_destination_outside_the_home_or_the_home_itself_is_left_to_podman(self):
        for dest in ("/var/lib/wk/git", "/ccache", "/opt/wk-tools", "/home/someone-else/git", self.HOME):
            _ensure(self.ws, dest)
        self.assertEqual([], list((self.ws / "home").iterdir()))


class TestOnePodmanWrapper(unittest.TestCase):
    """`Container.podman()` names the machine's connection from a macOS host."""

    def container(self, env, system):
        with mock.patch("wk.places.os.uname", return_value=mock.Mock(sysname=system)):
            return places.Container("container", str(REPO), env, Fake("here")).podman()

    def test_a_macos_host_names_the_machines_connection_and_a_local_daemon_is_plain(self):
        for env, system, want in (({}, "Darwin", ["podman", "-c", "wk"]),
                                  ({"WK_MACHINE": "other"}, "Darwin", ["podman", "-c", "other"]),
                                  ({"WK_IN_VM": "1"}, "Darwin", ["podman"]), ({}, "Linux", ["podman"])):
            self.assertEqual(want, self.container(env, system), (env, system))


class RegistryTest(fakes.FlowTest):
    def setUp(self):
        super().setUp()
        self.machines = os.path.join(self.tmp, "machines")
        os.makedirs(self.machines)

    def registry(self, **env):
        base = {"HOME": os.path.join(self.tmp, "home"), "XDG_STATE_HOME": os.path.join(self.tmp, "state"),
                "WK_MACHINES_DIR": self.machines, "WK_STORE": os.path.join(self.tmp, "store"),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        base.update(env)
        return places.Registry(REPO, env=base)


class TestWsPlaceDerivesFromTheStore(RegistryTest):
    def test_ws_target_resolves_an_unknown_name_to_container(self):
        self.assertEqual(self.registry().ws_place(f"nowhere-{rand_suffix()}"), "container")

    def _creating(self, name, pid):
        store = os.path.join(self.tmp, "fakebox-store")
        with open(os.path.join(self.machines, "fakebox.conf"), "w") as fh:
            fh.write("kind=build\ndriver=remote\nlocal=1\n"
                     "root=%s\nstore=%s\n" % (store, store))
        Records(store, env={"WK_STORE": store}).begin("new", "here", name, "wk new %s --kill" % name, "/nolog",
                                                      ["checking"], pid=pid)

    def test_ws_target_resolves_a_live_creating_record_to_its_target(self):
        name = f"creating-{rand_suffix()}"
        self._creating(name, os.getpid())
        self.assertEqual(self.registry().ws_place(name), "fakebox")

    def test_ws_target_ignores_a_dead_creating_record(self):
        name = f"dead-{rand_suffix()}"
        self._creating(name, 4194304)
        self.assertEqual(self.registry().ws_place(name), "container")


class TestPlaceKind(unittest.TestCase):
    def _kind(self, name, registry):
        return places.Registry(REPO, env={"WK_MACHINES_DIR": str(registry), "HOME": "/nonexistent"}, machine=Fake()).kind(name)

    def test_a_built_in_names_itself_a_conf_is_believed_and_nothing_is_guessed(self):
        cases = [(k, None, k) for k in ("container", "vm", "remote", "local")]
        cases += [("nosuchtarget", None, None), ("buildbox1", 'kind=build\ndriver="remote"\n', "remote"),
                  ("plainbox", 'kind=build\nhost="plainbox"\n', "remote")]
        for name, conf, want in cases:
            with self.subTest(name=name), scratch_dir() as reg:
                if conf:
                    (reg / (name + ".conf")).write_text(conf)
                self.assertEqual(self._kind(name, reg), want)


class TestRemoteIsLocal(unittest.TestCase):
    def test_local_exactly_when_the_remote_marker_says_this_is_the_machine(self):
        for remote_local in (True, False):
            with self.subTest(remote_local=remote_local), scratch_dir() as state:
                env = {"HOME": "/nonexistent", "XDG_STATE_HOME": str(state), "WK_REMOTE_MARKER": "/nonexistent/.wk-remote",
                       "WK_REMOTE_HOST": "buildbox1", **({"WK_REMOTE_LOCAL": "1"} if remote_local else {})}
                self.assertEqual(places.Remote("buildbox1", str(REPO), env, Fake()).is_local, remote_local)
