"""WK_* override audit -- lib/ and boot/ (every WK_* read with a default is
documented where the user meets it and covered by a test, or removed).

Each test below either (a) drives a real override end to end through the
function that reads it, with no hardware and no real machine touched -- a
PATH stub, a Fake machine, a scratch file -- or
(b) is a cheap regression guard on a source-level fact (two files must not
disagree on one name's default). Vars this agent decided to REMOVE
(WK_IMAGE_ARMHF, WK_DETACH_POLL_SECONDS, WK_SWEEP_TIMEOUT, WK_RPI3_SSH,
WK_RPI4_SSH, WK_MAC_SSH, WK_MAC_BENCH_SSH) are checked absent, so a later
re-add is a decision, not a drift.

Run: python3 -m unittest tests.test_wk_overrides_lib -v
"""
import os
import sys
import unittest
from unittest import mock

from tests.support import REPO, WkTest, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import git, reach, resources, screen, targets  # noqa: E402
from wk.boot.mac import GuestChannel  # noqa: E402
from wk.clock import Clock  # noqa: E402
from wk.lock import Lock  # noqa: E402
from wk.machine import Fake, Local, lib_argv  # noqa: E402
from wk.store import Store  # noqa: E402


def _src(*parts):
    return (REPO.joinpath(*parts)).read_text()


def _readers(name):
    """Every file under lib/, cmd/ and boot/ that names `name`."""
    return [str(f.relative_to(REPO)) for top in ("lib", "cmd", "boot") for f in (REPO / top).rglob("*")
            if f.is_file() and name in f.read_text(errors="ignore")]


class TestRemovedOverridesStayRemoved(unittest.TestCase):
    """Source-level regression guards: an override this agent removed because
    nothing used it should not silently come back."""

    def test_wk_image_armhf_is_pinned_not_overridable(self):
        self.assertNotIn("WK_IMAGE_ARMHF", _src("lib", "wk", "buildconf.py"))

    def test_wk_detach_poll_seconds_removed(self):
        self.assertEqual(_readers("WK_DETACH_POLL_SECONDS"), [])

    def test_wk_sweep_timeout_removed(self):
        self.assertEqual(_readers("WK_SWEEP_TIMEOUT"), [])

    def test_fleet_conf_ssh_names_not_overridable(self):
        """machines/*.conf: a fleet device is renamed by editing the
        conf, not by an environment variable nothing sets."""
        self.assertNotIn("WK_RPI3_SSH", _src("machines", "rpi3.conf"))
        self.assertNotIn("WK_RPI4_SSH", _src("machines", "rpi4.conf"))
        mbp = _src("machines", "mbp.conf")
        self.assertNotIn("WK_MAC_SSH", mbp)
        self.assertNotIn("WK_MAC_BENCH_SSH", mbp)
        # WK_BENCH_VOLUME survives, in code: tests/test_host_only.py drives it.
        self.assertNotIn("WK_BENCH_VOLUME", mbp)
        self.assertIn("WK_BENCH_VOLUME", _src("lib", "wk", "fleet.py"))


class TestSharedTimingDefaultsAgree(unittest.TestCase):
    """CLAUDE.md: 'same name read in several files: one default.' The watched run and the far-side poll read
    WK_STALL_SECONDS and WK_HEARTBEAT_SECONDS in lib/wk/job.py alone, and no bash file restates them."""

    def test_stall_and_heartbeat_seconds_have_one_default(self):
        self.assertEqual(_readers("WK_STALL_SECONDS:-"), [])
        self.assertEqual(_readers("WK_HEARTBEAT_SECONDS:-"), [])
        job = _src("lib", "wk", "job.py")
        self.assertEqual(2, job.count('"WK_STALL_SECONDS", 300'))
        self.assertEqual(2, job.count('"WK_HEARTBEAT_SECONDS", 300'))


class TestSshTimeoutReadInOnePlace(unittest.TestCase):
    """lib/common.sh's wk_ssh_timeout() is the one place the
    WK_SSH_TIMEOUT default lives; every caller reads it through that
    function instead of repeating `${WK_SSH_TIMEOUT:-10}`."""

    def test_no_other_file_reads_the_default_inline(self):
        owner = REPO / "lib" / "common.sh"
        offenders = []
        for top in ("cmd", "lib", "boot", "image", "host", "targets", "bench"):
            d = REPO / top
            if not d.is_dir():
                continue
            for path in d.rglob("*"):
                if not path.is_file() or path == owner:
                    continue
                if "WK_SSH_TIMEOUT:-" in path.read_text(errors="ignore"):
                    offenders.append(str(path.relative_to(REPO)))
        wk = REPO / "wk"
        if wk.is_file() and "WK_SSH_TIMEOUT:-" in wk.read_text(errors="ignore"):
            offenders.append("wk")
        self.assertEqual(offenders, [], f"WK_SSH_TIMEOUT:- read inline outside lib/common.sh: {offenders}")


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
    """`screen.blocker` (lib/wk/screen.py) names what is covering the window, from the window
    server's own list rather than from a list of application names: a pane
    nobody has met yet is caught the first time it draws. WK_SCREEN_EXPECTED is
    the other half -- what wk itself put there."""

    # `windows=` as vm/desktop-probe.sh prints it. Captured from a Tahoe 26.4
    # guest on 2026-09-05 with Setup Assistant's "Update Mac Automatically" pane
    # up: the pane at layer 0, its own full-screen backdrop at -1, Notification
    # Centre's click-catcher at 21, and the shell wk itself started.
    PANE = ("Setup Assistant:0:800x600;Setup Assistant:-1:1417x805;"
            "Notification Center:21:1417x805;Terminal:0:863x499;")
    CLEAN = "Notification Center:21:1417x805;Terminal:0:863x499;"

    def _blocker(self, reading, expected=None):
        """The window server answers `reading`; bench/mac-window-probe.sh's own filter judges it, for real."""
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
        """Notification Centre's click-catcher is full-screen and always there;
        judging by size or by presence would call every clean screen busy."""
        self.assertNotIn("Notification Center", self._blocker(self.PANE))

    def test_what_wk_puts_there_is_overridable(self):
        self.assertEqual("[Terminal]", self._blocker(self.CLEAN, "Finder|Safari"))

    def test_a_screen_that_could_not_be_read_is_not_reported_as_free(self):
        """`?` is "nobody asked the window server", which is not "nothing is
        there". An empty answer would make every machine with no compiler read
        as a clear screen, and a run that times out with no error is exactly
        this and nothing else."""
        self.assertEqual("[?]", self._blocker("?"))


class TestReachLib(WkTest):
    def test_wk_tailscale_timeout_bounds_a_wedged_cli(self):
        """A wedged tailscale CLI costs the walk WK_TAILSCALE_TIMEOUT, not its own hang (Reach.peers): the stub outlasts the
        runner's budget."""
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
        t = targets.Registry(REPO, env={"HOME": "/nonexistent", "WK_CCACHE_MAXSIZE": "12G"}, machine=Fake()).load("container")
        self.assertEqual(t.ccache_conf(), "max_size = 12G\n")

    def test_wk_mirror_branches_replaces_the_derived_list(self):
        self.assertIn("main", git.mirror_branches({"HOME": "/nonexistent"}))
        self.assertEqual(git.mirror_branches({"WK_MIRROR_BRANCHES": "main release/1.0"}), ["main", "release/1.0"])


class _Walk(targets.Registry):
    def all(self):
        return ["container", "vm", "buildbox1"]


class TestTargetLib(WkTest):
    def test_wk_no_delegate_stops_a_fleet_walk_asking_a_remote_target(self):
        env = {"HOME": str(self.tmp), "WK_MARKER": str(self.tmp / "no-such-marker")}
        self.assertEqual(_Walk(REPO, env=env, machine=Fake()).walk(), ["container", "vm", "buildbox1"])
        self.assertEqual(_Walk(REPO, env=dict(env, WK_NO_DELEGATE="1"), machine=Fake()).walk(), ["container", "vm"])

    def test_wk_remote_marker_overrides_the_remote_host_marker(self):
        marker = self.tmp / "remote-marker"
        reg = targets.Registry(REPO, env={"HOME": str(self.tmp), "WK_REMOTE_MARKER": str(marker)}, machine=Fake())
        self.assertFalse(reg.in_remote_host(), "should be false before the marker exists")
        marker.write_text("target=devbox\nroot=/home/x/wk\n")
        self.assertTrue(reg.in_remote_host(), "should be true once the marker exists")
        self.assertEqual(reg.remote_marker_field("target"), "devbox")


class TestBootMacGuest(unittest.TestCase):
    def test_wk_bench_guest_overrides_the_guest_workspace_name(self):
        self.assertEqual(GuestChannel(REPO, {}, env={"WK_BENCH_GUEST": "my-custom-guest"}).ws, "my-custom-guest")


class TestTargetsContainer(unittest.TestCase):
    def test_wk_container_user_overrides_the_workspace_owner(self):
        t = targets.Registry(REPO, env={"HOME": "/nonexistent", "WK_CONTAINER_USER": "customuser"}, machine=Fake()).load("container")
        self.assertEqual(t.user(), "customuser")


class TestTargetsLocal(WkTest):
    def test_wk_local_store_overrides_the_bind_mounted_store(self):
        store = self.tmp / "customstore"
        t = targets.LocalWorkspace("local", str(REPO), {"HOME": str(self.tmp), "WK_LOCAL_STORE": str(store)}, Fake())
        self.assertEqual(t.store.root(), str(store))


class TestTheGuestOverrides(unittest.TestCase):
    """Every WK_VM_*/WK_HOST_* a guest or its base reads reaches what it names; lib/wk/guest.py's daemons' own
    are tests/test_guest.py's, and a guest's size and display tests/test_wk_targets.py's."""

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
        from wk import targets
        e = {"HOME": "/h", "WK_STORE": "/st", "WK_VM_STORE": "/vs", "XDG_STATE_HOME": "/h/st", **env}
        vm = targets.Registry(str(REPO), env=e, machine=self.fake).load("vm")
        p = mock.patch.object(targets.Vm, "tart", lambda s: "/t/tart")
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


if __name__ == "__main__":
    unittest.main()
