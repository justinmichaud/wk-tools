"""`wk build`'s per-place build_args defaults and a real host conf's flags, and lib/wk/resources.py's readings.
The presets themselves are tests/test_presets.py's; the flow is tests/test_wk_build.py's."""
import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import FLEET_ENV, REAL_MACHINES, REPO, WkTest, container_side, requires_container_place

sys.path.insert(0, str(REPO / "lib"))
from wk import fleet, places, presets, resources  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Fake  # noqa: E402


class TestPlaceBuildArgsDefaults(unittest.TestCase):

    def test_load_target_reads_WK_BUILD_ARGS_from_conf(self):
        name = "wk-test-build-args-probe"
        # A registry of this one machine (WK_MACHINES_DIR): the real machines/ is left alone.
        registry = Path(tempfile.mkdtemp(prefix="wk-test-registry-"))
        self.addCleanup(shutil.rmtree, registry, True)
        (registry / f"{name}.conf").write_text(
            'kind=build\ndriver=remote\n'
            'host=nonexistent.invalid\n'
            'build_args="--no-fatal-warnings --extra-flag"\n'
        )
        t = places.Registry(REPO, dict(FLEET_ENV, WK_MACHINES_DIR=str(registry)), Fake()).load(name)
        self.assertEqual("--no-fatal-warnings --extra-flag", t.env["WK_BUILD_ARGS"])


class TestStaleLoadAverage(unittest.TestCase):

    def _jobs(self, cores, avail_mb, load):
        return resources.Budget(Fake(), {}).jobs(cores, avail_mb, 1536, load=load)

    def test_stale_load_with_idle_memory_is_discounted(self):
        stale = self._jobs(10, 100000, 9)
        self.assertGreater(stale, 1, "a stale-looking load average was trusted outright")

    def test_genuine_load_with_matching_memory_use_still_throttles(self):
        busy = self._jobs(20, 23040, 18)
        self.assertEqual(busy, 2, "a genuinely busy machine's load was discounted")


class TestARealHostConfReachesTheBuildFlags(WkTest):

    def _cmake(self, driver, preset="jsc-release"):
        env = dict(FLEET_ENV, WK_MACHINES_DIR=str(REAL_MACHINES), XDG_STATE_HOME=str(self.tmp / "state"))
        t = places.Registry(REPO, env, Fake()).load(driver)
        return t.kind, presets.resolve(preset, "linux", t.kind, t.env).cmake

    def test_buildbox4s_conf_turns_libcxx_off_and_libbacktrace_with_it(self):
        kind, cmake = self._cmake("buildbox4")
        self.assertEqual("remote", kind)
        self.assertNotIn("-stdlib=libc++", cmake)
        self.assertIn("-DUSE_LIBBACKTRACE=OFF", cmake)

    def test_a_machine_whose_conf_says_1_gets_libcxx(self):
        _kind, cmake = self._cmake("devbox-arm64-2")
        self.assertIn("-stdlib=libc++", cmake)

    def test_every_host_conf_carries_a_value_the_loader_accepts(self):
        for name in fleet.Fleet(REPO, FLEET_ENV).names(fleet.PLACE_KINDS):
            with self.subTest(machine=name):
                self._cmake(name)


# `sysctl -n <name>`: every reading lib/wk/resources.py takes on a Mac.
SYSCTL = {"hw.ncpu": "12\n", "hw.memsize": "17179869184\n", "vm.loadavg": "{ 3.41 2.20 1.90 }\n"}


class TestAReadingTheMachineWillNotGive(WkTest):

    def res(self, os_name, sysctl=None, files=None, env=None, nproc=None):
        m = Fake()
        for k, v in (sysctl or {}).items():
            m.answer(["sysctl", "-n", k], out=v)
        if nproc is not None:
            m.answer(["nproc"], out=nproc)
        m.files.update(files or {})
        return resources.Resources(m, env or {"HOME": "/h"}, os_name)

    def refusal(self, fn):
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            fn()
        self.assertNotIn("Traceback", err.getvalue())
        return err.getvalue()

    def test_each_reading_comes_from_the_kernel_the_platform_names(self):
        r = self.res("macos", SYSCTL)
        self.assertEqual((12, 16384, 3), (r.host_cores(), r.host_mem_mb(), r.host_load()))

    def test_the_linux_arms_answer_from_proc_and_nproc(self):
        r = self.res("linux", nproc="8\n", files={"/proc/meminfo": "MemTotal:       16777216 kB\n",
                                                   "/proc/loadavg": "2.70 1.00 0.50 1/100 42\n"})
        self.assertEqual((8, 16384, 2), (r.host_cores(), r.host_mem_mb(), r.host_load()))

    def test_a_core_count_that_did_not_come_back_refuses_and_names_it(self):
        self.assertIn("the core count (nproc)", self.refusal(self.res("linux").host_cores))
        self.assertIn("the core count (sysctl hw.ncpu)", self.refusal(self.res("macos").host_cores))

    def test_a_memory_reading_that_did_not_come_back_refuses_and_names_it(self):
        self.assertIn("total memory (/proc/meminfo MemTotal)", self.refusal(self.res("linux").host_mem_mb))
        self.assertIn("total memory (sysctl hw.memsize)", self.refusal(self.res("macos").host_mem_mb))

    def test_a_polite_build_reads_this_machines_load_when_no_caller_measured_one(self):
        env = {"WK_CGROUP_CORES": "12", "WK_AVAIL_MB": "100000", "WK_MB_PER_JOB": "1000"}
        r = self.res("macos", SYSCTL, env=env)
        # 12 cores, load 3.41 -> 3 spoken for, and never more than half a box.
        self.assertEqual(6, resources.Budget(r.machine, env).jobs(r.cores(), r.avail_mem_mb(), r.mb_per_job(),
                                                                  load=r.load()))

    def test_free_memory_on_a_mac_is_the_total_less_the_reserve(self):
        self.assertEqual(12288, self.res("macos", SYSCTL, env={"WK_RESERVE_MB": "4096"}).avail_mem_mb())

    def test_free_memory_that_did_not_come_back_refuses_and_names_it(self):
        self.assertIn("free memory (/proc/meminfo MemAvailable)", self.refusal(self.res("linux").avail_mem_mb))

    def test_a_cgroup_limit_clamps_free_memory_and_max_is_no_limit(self):
        meminfo = {"/proc/meminfo": "MemAvailable:   20480000 kB\n"}
        r = self.res("linux", files=dict(meminfo, **{resources.CGROUP_MEM_MAX: "1073741824\n"}))
        self.assertEqual(1024, r.avail_mem_mb())
        r = self.res("linux", files=dict(meminfo, **{resources.CGROUP_MEM_MAX: "max\n"}), env={"WK_CGROUP_MB": "2048"})
        self.assertEqual(2048, r.avail_mem_mb())

    def test_a_cgroup_limit_it_could_not_read_refuses_rather_than_ignoring_it(self):
        r = self.res("linux", files={"/proc/meminfo": "MemAvailable:   20480000 kB\n", resources.CGROUP_MEM_MAX: ""})
        self.assertIn("the cgroup memory limit", self.refusal(r.avail_mem_mb))


class TestSdkImageCarriesLibbacktrace(unittest.TestCase):

    @requires_container_place()
    def test_sdk_image_has_libbacktrace(self):
        img_cp = container_side(
            "podman images --format '{{.Repository}}:{{.Tag}}' "
            "| grep '^ghcr.io/igalia/wkdev-sdk:' | head -1"
        )
        img = img_cp.stdout.strip()
        if not img:
            self.skipTest("no ghcr.io/igalia/wkdev-sdk image pulled for the container place")
        cp = container_side(
            f"podman run --rm {img} sh -c "
            "'pkg-config --exists libbacktrace || test -f /usr/include/backtrace.h'"
        )
        self.assertEqual(cp.returncode, 0,
            f"the wkdev SDK image ({img}) carries no libbacktrace -- "
            f"USE_LIBBACKTRACE=ON (lib/wk/presets.py, container/vm/local kinds) "
            f"would fail to configure: {cp.stdout}{cp.stderr}")


if __name__ == "__main__":
    unittest.main()
