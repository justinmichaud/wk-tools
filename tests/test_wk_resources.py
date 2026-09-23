"""lib/wk/resources.py: the envelope a target is sized from, read through a
machine -- the same arithmetic as lib/resources.sh over the same readings.

Run: python3 tests/run.py -k tests.test_wk_resources
"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import resources  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Fake, Local  # noqa: E402

IS_MACOS = os.uname().sysname == "Darwin"
MEMINFO = "MemTotal:       32806140 kB\nMemFree:         1000000 kB\nMemAvailable:   20480000 kB\n"


class ResourcesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-resources-"))
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store")}
        self.fake = Fake()

    def tearDown(self):
        os.system("rm -rf %s" % self.tmp)

    def linux(self, env=None, meminfo=MEMINFO, cores="8\n"):
        self.fake.answer(["nproc"], out=cores)
        self.fake.files["/proc/meminfo"] = meminfo
        return resources.Resources(self.fake, dict(self.env, **(env or {})), "linux")

    def macos(self, env=None):
        self.fake.answer(["sysctl", "-n", "hw.ncpu"], out="10\n")
        self.fake.answer(["sysctl", "-n", "hw.memsize"], out="34359738368\n")
        return resources.Resources(self.fake, dict(self.env, **(env or {})), "macos")


class TestEnvelope(ResourcesTest):
    def test_a_desktop_keeps_one_core_and_twelve_gigabytes(self):
        r = self.linux()
        self.assertEqual((r.host_cores(), r.host_mem_mb()), (8, 32037))
        self.assertEqual((r.envelope_cores(), r.envelope_mem_mb()), (7, 19749))
        self.assertFalse(r.is_headless())

    def test_a_headless_machine_keeps_two_gigabytes_and_no_core(self):
        r = self.linux()
        self.fake.files[os.path.join(self.env["WK_STORE"], ".headless")] = ""
        self.assertTrue(r.is_headless())
        self.assertEqual((r.envelope_cores(), r.envelope_mem_mb()), (8, 29989))
        del self.fake.files[os.path.join(self.env["WK_STORE"], ".headless")]
        self.fake.files[os.path.join(self.env["HOME"], ".wk-workspace")] = "name=ws\n"
        self.assertTrue(r.is_headless())
        self.assertEqual(resources.Resources(self.fake, {}, "linux").headless_marker(), "/var/lib/wk/.headless")

    def test_a_small_machine_gives_half_rather_than_nothing(self):
        r = self.linux(meminfo="MemTotal:        8388608 kB\nMemAvailable:    4000000 kB\n")
        self.assertEqual((r.host_mem_mb(), r.envelope_mem_mb()), (8192, 4096))
        self.assertEqual(self.linux(cores="1\n").envelope_cores(), 1)

    def test_every_reserve_is_overridable(self):
        r = self.linux({"WK_RESERVE_CORES": "2", "WK_RESERVE_MB": "1000", "WK_MB_PER_JOB": "2000"})
        self.assertEqual((r.envelope_cores(), r.envelope_mem_mb(), r.mb_per_job()), (6, 31037, 2000))
        self.assertEqual(self.linux().mb_per_job(), resources.MB_PER_JOB)
        r = self.linux({"WK_HEADLESS_RESERVE_CORES": "3", "WK_HEADLESS_RESERVE_MB": "4096"})
        self.fake.files[os.path.join(self.env["WK_STORE"], ".headless")] = ""
        self.assertEqual((r.envelope_cores(), r.envelope_mem_mb()), (5, 27941))

    def test_a_macos_machine_is_read_from_sysctl(self):
        r = self.macos()
        self.assertEqual((r.host_cores(), r.host_mem_mb()), (10, 32768))
        self.assertEqual((r.envelope_cores(), r.envelope_mem_mb(), r.avail_mem_mb()), (9, 20480, 20480))

    def test_what_a_build_may_take_is_free_memory_under_the_cgroup(self):
        r = self.linux()
        self.assertEqual(r.avail_mem_mb(), 20000)
        self.assertEqual(self.linux({"WK_CGROUP_MB": "8000"}).avail_mem_mb(), 8000)
        self.assertEqual(self.linux({"WK_CGROUP_MB": "80000"}).avail_mem_mb(), 20000)
        self.assertEqual(self.linux({"WK_AVAIL_MB": "123"}).avail_mem_mb(), 123)
        self.fake.files[resources.CGROUP_MEM_MAX] = "max\n"
        self.assertEqual(r.avail_mem_mb(), 20000)
        self.fake.files[resources.CGROUP_MEM_MAX] = "4294967296\n"
        self.assertEqual(r.avail_mem_mb(), 4096)

    def test_a_reading_the_machine_did_not_give_sizes_nothing(self):
        self.fake.answer(["nproc"], rc=127, err="nproc: not found")
        r = resources.Resources(self.fake, self.env, "linux")
        with self.assertRaises(Refused):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                r.envelope_cores()
        self.assertIn("cannot read the core count (nproc) on this linux machine", err.getvalue())
        self.assertIn("it builds nothing from a guess", err.getvalue())
        with self.assertRaises(Refused):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                r.envelope_mem_mb()
        self.assertIn("cannot read total memory (/proc/meminfo MemTotal)", err.getvalue())
        self.fake.files[resources.CGROUP_MEM_MAX] = "lots\n"
        self.fake.files["/proc/meminfo"] = MEMINFO
        with self.assertRaises(Refused):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                r.avail_mem_mb()
        self.assertIn("the cgroup memory limit (%s)" % resources.CGROUP_MEM_MAX, err.getvalue())

    @unittest.skipUnless(IS_MACOS, "compares against lib/resources.sh on this Mac")
    def test_the_bash_and_the_python_agree_on_this_machine(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("WK_")}
        env.update(self.env, XDG_STATE_HOME=str(self.tmp / "state"))
        cp = subprocess.run(["bash", "-c", '. lib/common.sh; . lib/resources.sh; envelope_cores; envelope_mem_mb; host_cores; host_mem_mb; avail_mem_mb'],
                            cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        r = resources.Resources(Local(), env, "macos")
        self.assertEqual([int(x) for x in cp.stdout.split()],
                         [r.envelope_cores(), r.envelope_mem_mb(), r.host_cores(), r.host_mem_mb(), r.avail_mem_mb()])


class TestBudget(ResourcesTest):
    """The budget half `wk build` sizes against: other builds' records on this machine, the job count,
    the warning when it is low, and the two admissions."""

    def budget(self, env=None):
        self.fake.answer(["hostname"], out="here\n")
        return resources.Budget(self.fake, dict(self.env, XDG_STATE_HOME=str(self.tmp / "state"), **(env or {})))

    def explain(self, *a, **kw):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            jobs = self.budget().explain(*a, **kw)
        return jobs, err.getvalue()

    def test_a_low_count_warns_once_with_its_reason(self):
        jobs, err = self.explain(10, 1000, 1536)
        self.assertEqual(jobs, 1)
        self.assertIn("parallelism: 1 jobs is under half of 10 cores -- the memory", err)
        self.assertNotIn("parallelism:", self.explain(8, 100000, 1536)[1])
        self.assertNotIn("parallelism:", self.explain(10, 100000, 1536, max_jobs=1)[1])
        self.assertIn("is\n  this target's own ceiling", self.explain(10, 100000, 1536, running=[("x", 8, 0)])[1])

    def test_a_polite_count_discounts_a_stale_load_and_trusts_a_real_one(self):
        b = self.budget()
        self.assertGreater(b.jobs(10, 100000, 1536, load=9), 1)
        self.assertEqual(b.jobs(20, 23040, 1536, load=18), 2)
        self.assertEqual(b.jobs(12, 100000, 1000, load=0), 6)   # never more than half a shared box
        _, err = self.explain(12, 10000, 1000, load=10)
        self.assertIn("parallelism: 2 jobs is under half of 12 cores -- load average\n  10 is treated as that many cores already spoken for", err)

    def test_other_builds_on_this_machine_are_spoken_for_and_a_dead_ones_record_goes(self):
        b = self.budget()
        self.fake.pids.add(7)
        b.record("live", 4, 40000, "pid:7")
        b.record("dead", 4, 40000, "pid:8")
        other = b.record("elsewhere", 4, 40000, "pid:7")
        self.fake.files[other] = self.fake.files[other].replace("machine=here", "machine=there")
        alive = lambda h: self.fake.alive(int(h[4:]))
        running = b.running(alive)
        self.assertEqual(running, [("live", 4, 40000)])
        self.assertEqual(len([p for p in self.fake.files if "/builds/" in p]), 2)
        self.assertEqual(b.jobs(24, 88000, 2000, running=running), 20)

    def test_reaping_a_dead_record_is_dry_run_safe(self):
        """A read-and-reap during `wk build --dry-run` must not delete a build record for
        real: `Budget.running` goes through `machine.remove`, not the lock-only `remove_now`."""
        b = self.budget()
        b.record("dead", 4, 40000, "pid:8")
        before = {p for p in self.fake.files if "/builds/" in p}
        with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
            running = b.running(lambda h: self.fake.alive(int(h[4:])))
        self.assertEqual(running, [])
        self.assertEqual({p for p in self.fake.files if "/builds/" in p}, before)

    def test_the_admissions_are_barriers_that_force_crosses(self):
        b = self.budget()
        with self.assertRaises(Refused) as cm:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                b.admit("this build", 8, [("wk build a (gtk-release)", 6, 9216)])
        self.assertEqual(cm.exception.status, 75)
        self.assertIn("here is already building:\n      wk build a (gtk-release) (6 jobs, 9216 MB)\n    this build wants 8 job(s)", err.getvalue())
        with self.assertRaises(Refused):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                b.disk_admit("this build", 25, 10, "/s's filesystem")
        self.assertIn("10 GB free on /s's filesystem; this build wants about 25 GB.", err.getvalue())
        b.disk_admit("this build", 25, None, "x")   # no answer from df is not evidence of a full disk
        b.admit("this build", 8, [])
        with mock.patch.dict(os.environ, {"WK_FORCE": "1"}):
            with contextlib.redirect_stderr(io.StringIO()):
                b.disk_admit("this build", 25, 10, "x")

    def test_df_is_read_in_the_one_spelling_both_dfs_have(self):
        self.fake.answer(["df", "-Pk", "/s"], out="Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/d 1 1 1048577 1% /\n")
        self.assertEqual(self.budget().free_gb("/s"), 2)
        self.assertIsNone(resources.parse_df("garbage"))


if __name__ == "__main__":
    unittest.main()
