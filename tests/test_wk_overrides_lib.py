"""WK_* override audit -- lib/ and boot/ (every WK_* read with a default is"""
import os
import sys
import types
import unittest
from unittest import mock

from tests.fakes import FakeRegistry
from tests.support import REAL_MACHINES, REPO, WkTest, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import git, places, reach, resources, screen  # noqa: E402
from wk.boot.mac import GuestChannel  # noqa: E402
from wk.clock import Clock  # noqa: E402
from wk.lock import Lock  # noqa: E402
from wk.machine import Fake, Local, lib_argv  # noqa: E402
from wk.store import Store  # noqa: E402


class TestCommonLib(WkTest):
    def test_wk_ssh_timeout_default_and_override(self):
        cp = self.bash('''
. "$WK_ROOT/lib/common.sh"
[ "$(wk_ssh_timeout)" = 10 ] || { echo "default: got $(wk_ssh_timeout)"; exit 1; }
WK_SSH_TIMEOUT=42
[ "$(wk_ssh_timeout)" = 42 ] || { echo "override: got $(wk_ssh_timeout)"; exit 1; }
echo PASS
''')
        self.assertIn("PASS", cp.stdout, cp.stdout + cp.stderr)

    def test_wk_session_mode_file_overrides_the_marker_file(self):
        sys.path.insert(0, str(REPO / "lib"))
        from wk import session
        marker = self.tmp / "session-mode"
        seat = session.here(str(REPO), {"WK_SESSION_MODE_FILE": str(marker)})
        self.assertEqual("none", seat.mode())
        marker.write_text("gpu\n")
        self.assertEqual("gpu", seat.mode())

    def test_wk_lock_dir_override_is_where_locks_actually_go(self):
        lockdir = self.tmp / "locks"
        lock = Lock(Store({"HOME": str(self.tmp), "WK_LOCK_DIR": str(lockdir)}), Local(), Clock())
        lock.hold("testresource", 0)
        try:
            self.assertEqual([p.name.split("@")[0] for p in lockdir.iterdir()], ["testresource"])
        finally:
            lock.release_all()


class TestScreenBlocker(unittest.TestCase):

    PANE = ("Setup Assistant:0:800x600;Setup Assistant:-1:1417x805;"
            "Notification Center:21:1417x805;Terminal:0:863x499;")
    CLEAN = "Notification Center:21:1417x805;Terminal:0:863x499;"

    def _blocker(self, reading, expected=None):
        m = Fake()
        m.answer(lib_argv(REPO, screen.WINDOWS, "wk_window_probe"), out="windows=%s\n" % reading)
        m.react(lib_argv(REPO, screen.WINDOWS, "wk_window_unexpected"), lambda argv, f: Local().run(argv))
        env = {k: v for k, v in os.environ.items() if k != "WK_SCREEN_EXPECTED"}
        if expected:
            env["WK_SCREEN_EXPECTED"] = expected
        with mock.patch.dict(os.environ, env, clear=True):
            return "[%s]" % screen.blocker(m, REPO)

    def test_a_pane_over_the_window_is_named(self):
        self.assertEqual("[Setup Assistant]", self._blocker(self.PANE))

    def test_a_screen_with_only_wk_s_own_windows_is_free(self):
        self.assertEqual("[]", self._blocker(self.CLEAN))

    def test_the_menu_bar_and_the_dock_are_not_blockers(self):
        self.assertNotIn("Notification Center", self._blocker(self.PANE))

    def test_what_wk_puts_there_is_overridable(self):
        self.assertEqual("[Terminal]", self._blocker(self.CLEAN, "Finder|Safari"))

    def test_a_screen_that_could_not_be_read_is_not_reported_as_free(self):
        self.assertEqual("[?]", self._blocker("?"))


class TestReachLib(WkTest):
    def test_wk_tailscale_timeout_bounds_a_wedged_cli(self):
        with stub_path({"tailscale": "#!/bin/sh\nsleep 600\n"}) as binp, \
                mock.patch.dict(os.environ, {"PATH": f"{binp}:{os.environ['PATH']}"}):
            peers = reach.Reach(Local(), {"WK_TAILSCALE_TIMEOUT": "1", "WK_ROOT": str(REPO)}).peers()
        self.assertEqual(peers, [])


class TestResourcesLib(WkTest):
    def test_wk_cgroup_mb_clamps_available_memory(self):
        fake = Fake()
        fake.files["/proc/meminfo"] = "MemAvailable:   20480000 kB\n"
        self.assertEqual(1, resources.Resources(fake, {"WK_CGROUP_MB": "1"}, "linux").avail_mem_mb())

    def test_wk_reserve_cores_and_mb_shrink_the_envelope(self):
        fake = Fake()
        fake.answer(["nproc"], out="16\n")
        fake.files["/proc/meminfo"] = "MemTotal:       32768000 kB\n"
        r = resources.Resources(fake, {"WK_RESERVE_CORES": "3", "WK_RESERVE_MB": "4096", "HOME": "/h"}, "linux")
        self.assertEqual((13, 32000 - 4096), (r.envelope_cores(), r.envelope_mem_mb()))

    def test_wk_headless_reserve_cores_and_mb_apply_when_headless(self):
        fake = Fake()
        fake.files["/s/.headless"] = ""
        r = resources.Resources(fake, {"WK_STORE": "/s", "WK_HEADLESS_RESERVE_CORES": "0",
                                       "WK_HEADLESS_RESERVE_MB": "111"}, "linux")
        self.assertEqual((0, 111), (r.reserve_cores(), r.reserve_mb()))


class TestStoreLib(unittest.TestCase):
    def test_wk_ccache_maxsize_renders_into_the_conf(self):
        t = places.Registry(REPO, env={"HOME": "/nonexistent", "WK_CCACHE_MAXSIZE": "12G"}, machine=Fake()).load("container")
        self.assertEqual(t.ccache_conf(), "max_size = 12G\n")

    def test_wk_mirror_branches_replaces_the_derived_list(self):
        self.assertIn("main", git.mirror_branches({"HOME": "/nonexistent"}))
        self.assertEqual(git.mirror_branches({"WK_MIRROR_BRANCHES": "main release/1.0"}), ["main", "release/1.0"])


class TestPlaceLib(WkTest):
    def test_wk_no_delegate_stops_a_fleet_walk_asking_a_remote_place(self):
        env = {"HOME": str(self.tmp), "WK_MARKER": str(self.tmp / "no-such-marker")}
        fleet = ["container", "vm", "buildbox1"]
        self.assertEqual(FakeRegistry(env, Fake(), names=fleet).walk(), fleet)
        self.assertEqual(FakeRegistry(dict(env, WK_NO_DELEGATE="1"), Fake(), names=fleet).walk(), ["container", "vm"])

    def test_wk_remote_marker_overrides_the_remote_host_marker(self):
        marker = self.tmp / "remote-marker"
        reg = places.Registry(REPO, env={"HOME": str(self.tmp), "WK_REMOTE_MARKER": str(marker)}, machine=Fake())
        self.assertFalse(reg.in_remote_host(), "should be false before the marker exists")
        marker.write_text("place=devbox\nroot=/home/x/wk\n")
        self.assertTrue(reg.in_remote_host(), "should be true once the marker exists")


class TestBootMacGuest(unittest.TestCase):
    def test_wk_bench_guest_overrides_the_guest_workspace_name(self):
        self.assertEqual(GuestChannel(REPO, {}, env={"WK_BENCH_GUEST": "my-custom-guest"}).ws, "my-custom-guest")


class TestPlacesContainer(unittest.TestCase):
    def test_wk_container_user_overrides_the_workspace_owner(self):
        t = places.Registry(REPO, env={"HOME": "/nonexistent", "WK_CONTAINER_USER": "customuser"}, machine=Fake()).load("container")
        self.assertEqual(t.user(), "customuser")


class TestPlacesLocal(WkTest):
    def test_wk_local_store_overrides_the_bind_mounted_store(self):
        store = self.tmp / "customstore"
        t = places.LocalWorkspace("local", str(REPO), {"HOME": str(self.tmp), "WK_LOCAL_STORE": str(store)}, Fake())
        self.assertEqual(t.store.store_dir(), str(store))


class TestTheGuestOverrides(unittest.TestCase):

    def setUp(self):
        from wk.machine import Fake, Result
        self.fake = Fake("here")
        self.fake.answer(["/t/tart", "list"], out="[]")
        self.fake.answer(["podman", "machine", "inspect"], rc=125)
        self.fake.answer(["sysctl", "-n", "hw.ncpu"], out="10\n")
        self.fake.answer(["sysctl", "-n", "hw.memsize"], out="34359738368\n")
        self.fake.answer(["df", "-Pk", "/"], out="F\n/d 1 1 104857600 1% /\n")
        self.result = Result

    def vm(self, **env):
        from wk import places
        e = {"HOME": "/h", "WK_STORE": "/st", "WK_VM_STORE": "/vs", "XDG_STATE_HOME": "/h/st", **env}
        vm = places.Registry(str(REPO), env=e, machine=self.fake).load("vm")
        p = mock.patch.object(places.Vm, "tart", lambda s: "/t/tart")
        p.start()
        self.addCleanup(p.stop)
        return vm

    def admitted(self, mine=1024, **env):
        import contextlib
        import io
        from wk import guest
        from wk.act import Refused
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                guest.admit(guest.Host(self.vm(**env)), "wk-g", mine)
                return True
            except Refused:
                return False

    def test_each_override_reaches_what_it_names(self):
        from wk.sysimage import guestbase
        vm = self.vm(WK_VM_IMAGE="custom-image:1", WK_VM_BASE="custom-base", WK_VM_USER="customuser",
                     WK_VM_BASE_CPUS="3", WK_VM_BASE_MEM_MB="4444")
        self.assertEqual("custom-image:1", guestbase.image(vm.env))
        self.assertEqual("custom-base", vm.base())
        self.assertEqual("/Users/customuser/WebKit", vm.src("g"))
        self.assertEqual(("3", "4444"), guestbase.Base(vm).sizing())
        self.assertEqual("/vs/vm", vm.vm_dir())

    def test_the_limits_refuse_and_the_share_crosses_the_memory_one(self):
        self.assertTrue(self.admitted())
        self.assertFalse(self.admitted(WK_VM_MAX="0"))
        self.assertFalse(self.admitted(WK_HOST_FREE_MIN_GB="200"))
        self.assertTrue(self.admitted(WK_HOST_FREE_MIN_GB="1", WK_HOST_FREE_WARN_GB="2"))
        self.assertFalse(self.admitted(mine=99999999))
        self.assertTrue(self.admitted(mine=99999999, WK_VM_SHARE="1"))


class TestEachOverrideReachesWhatItNames(WkTest):
    def test_the_plain_readers(self):
        from wk import guest, status
        self.assertEqual((status.fleet_timeout({}), status.fleet_timeout({"WK_FLEET_TIMEOUT": "9"})), (4, 9))
        self.assertEqual(guest.password({"WK_VM_PASSWORD": "pw"}), "pw")
        t = places.Registry(REPO, env={"HOME": "/h", "WK_SDK": "/my/sdk"}, machine=Fake()).load("container")
        self.assertEqual(t.sdk(), "/my/sdk")

    def test_wk_bench_machine_names_the_volume_in_host_mode(self):
        from wk.bench.mac import Install
        seen = []
        env = {"HOME": "/h", "WK_MACHINES_DIR": str(REAL_MACHINES), "WK_BENCH_MACHINE": "mbp", "WK_ROOT": str(REPO)}
        install = Install(REPO, Fake(), env, lambda root, conf: seen.append(conf["name"]) or types.SimpleNamespace(bench_root=lambda: "/v"))
        self.assertEqual((install.staging_root(), seen), ("/v", ["mbp"]))

    def test_wk_bench_user_is_the_second_account_asked(self):
        asked = []
        survey = reach.Survey(reach.Reach(Fake(), {"WK_BENCH_USER": "benchy"}, peers=[]))
        with mock.patch.object(reach.Survey, "_ask", lambda self, dest, opts=(): asked.append(dest) or ""):
            survey.identify("10.0.0.9", "")
        self.assertEqual(asked[-1], "benchy@10.0.0.9")

    def test_wk_bridge_timeout_caps_each_health_check(self):
        from wk import status
        caps = []
        walk = types.SimpleNamespace(root=str(REPO), reach=lambda n: ("", ""),
                                     env={"HOME": "/h", "WK_MACHINES_DIR": str(REAL_MACHINES), "WK_BRIDGE_TIMEOUT": "3"})
        with mock.patch.object(status, "bridge_ssh", lambda name, probe, ts, connect, cap: caps.append(cap) or ""):
            status.Walk.bridges(walk)
        self.assertTrue(caps)
        self.assertEqual(set(caps), {3.0})


class TestStatusOverrides(WkTest):
    def test_a_wedged_bridge_ssh_cannot_outlive_the_ceiling(self):
        from wk import kv, status
        with stub_path({"ssh": "#!/bin/sh\nsleep 30\n"}) as binp, \
                mock.patch.dict(os.environ, {"PATH": "%s:%s" % (binp, os.environ["PATH"])}):
            out = status.bridge_ssh("testphone", status.BRIDGE_PROBE, True, 1, 1)
        self.assertEqual(out, "")
        self.assertEqual(status.bridge_record("testphone", {}, "x", kv.kv(out), lambda n: ("", ""))["state"], "unreachable")

    def test_wk_wait_timeout_is_the_waits_default_timeout(self):
        from tests.support import load_cmd
        cmd = load_cmd("status")
        seen = []

        def wait(probe, timeout, interval, *rest):
            seen.append((timeout, interval))
            raise SystemExit(0)
        with mock.patch.object(cmd, "wait_until_idle", wait), \
                mock.patch.dict(os.environ, {"WK_WAIT_TIMEOUT": "7", "WK_WAIT_INTERVAL": "2"}), self.assertRaises(SystemExit):
            cmd.main(["--wait"])
        self.assertEqual(seen, [(7, 2.0)])


class TestOtherOverrides(WkTest):
    def test_sudo_and_sync(self):
        from wk import sync
        from wk.sudo import Sudo
        self.assertEqual((Sudo(None, {}).timeout_desc, Sudo(None, {"WK_SUDO_TIMEOUT_MIN": "2"}).timeout_desc),
                         ("30 seconds", "120 seconds"))
        self.assertEqual((sync.publish_branch({}), sync.publish_branch({"WK_BRANCH": "wpe-2.44"})), ("origin/main", "wpe-2.44"))

    def session_user(self, conf):
        text = (REPO / "admin" / "wk-quiesce-priv").read_text()
        func = text[text.index("session_user() {"):]
        func = func[:func.index("\n}\n") + 3]
        return self.bash(func + "session_user", env={"WK_SESSION_CONF": str(conf), "WK_SESSION_USER": "hostile"})

    def test_quiesce_priv_takes_the_session_user_from_its_conf_never_the_caller(self):
        conf = self.tmp / "wk-session.env"
        conf.write_text("WK_SESSION_USER=root\n")
        cp = self.session_user(conf)
        self.assertEqual((cp.returncode, cp.stdout.strip()), (0, "root"), cp.stderr)
        self.assertNotEqual(self.session_user(self.tmp / "none").returncode, 0)


if __name__ == "__main__":
    unittest.main()
