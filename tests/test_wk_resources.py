"""lib/wk/resources.py: the envelope a place is sized from, the budget `wk build` sizes against, and the rule that a
refused reading reaches the caller."""
import contextlib
import io
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.fakes import Fake, FlowTest
from tests.support import REPO, WkTest, bash, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import places, resources  # noqa: E402
from wk.act import RETRY_EXIT, Refused  # noqa: E402
from wk.machine import Local  # noqa: E402
from wk.sysimage import guestbase, yocto  # noqa: E402

MEMINFO = "MemTotal:       32806140 kB\nMemFree:         1000000 kB\nMemAvailable:   20480000 kB\n"


class ResourcesTest(FlowTest):
    def setUp(self):
        super().setUp()
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store")}
        self.fake = Fake()

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
        self.assertEqual(resources.Resources(self.fake, {"WK_STORE": "/s"}, "linux").headless_marker(), "/s/.headless")

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
        self.assertEqual((self.linux({"WK_MAX_JOBS": "3"}).max_jobs(), r.avail_override(), r.max_jobs()), (3, None, None))
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
        self.assertIn("nproc", err.getvalue())
        with self.assertRaises(Refused):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                r.envelope_mem_mb()
        self.assertIn("/proc/meminfo", err.getvalue())
        self.fake.files[resources.CGROUP_MEM_MAX] = "lots\n"
        self.fake.files["/proc/meminfo"] = MEMINFO
        with self.assertRaises(Refused):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                r.avail_mem_mb()
        self.assertIn(resources.CGROUP_MEM_MAX, err.getvalue())


class TestBudget(ResourcesTest):
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
        self.assertIn("parallelism:", err)
        self.assertNotIn("parallelism:", self.explain(8, 100000, 1536)[1])
        self.assertNotIn("parallelism:", self.explain(10, 100000, 1536, max_jobs=1)[1])

    def test_a_polite_count_discounts_a_stale_load_and_trusts_a_real_one(self):
        b = self.budget()
        self.assertGreater(b.jobs(10, 100000, 1536, load=9), 1)
        self.assertEqual(b.jobs(20, 23040, 1536, load=18), 2)
        self.assertEqual(b.jobs(12, 100000, 1000, load=0), 6)   # never more than half a shared box
        self.assertIn("load average", self.explain(12, 10000, 1000, load=10)[1])

    def test_the_live_builds_holding_this_machines_budget_are_spoken_for(self):
        from wk import record
        from tests.fakes import FakeClock
        recs = record.Records("/s", clock=FakeClock(), machine=self.fake, env={})
        b = self.budget()
        self.fake.pids.update({7, 9})
        for name, pid, machine in (("live", 7, "here"), ("dead", 8, "here"), ("elsewhere", 9, "there")):
            recs.begin("build", "here", name, "k", "/l", ["x"], pid=pid, holds=["build:" + machine],
                       fields={"jobs": 4, "budget_mb": 40000})
        running = b.running(recs)
        self.assertEqual(running, [("build live", 4, 40000)])
        self.assertEqual(b.jobs(24, 88000, 2000, running=running), 20)
        self.assertEqual(self.budget({"WK_BUILD_MACHINE": "there"}).running(recs), [("build elsewhere", 4, 40000)])

    def test_the_admissions_are_barriers_that_force_crosses(self):
        b = self.budget()
        with self.assertRaises(Refused) as cm:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                b.admit("this build", 8, [("wk build a (gtk-release)", 6, 9216)])
        self.assertEqual(cm.exception.status, RETRY_EXIT)
        self.assertIn("wk build a (gtk-release)", err.getvalue())
        self.assertIn("--force", err.getvalue())
        with self.assertRaises(Refused) as cm:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                b.disk_admit("this build", 25, 10, "/s's filesystem")
        self.assertEqual(cm.exception.status, 1)
        self.assertIn("wk gc", err.getvalue())
        b.disk_admit("this build", 25, None, "x")   # no answer from df is not evidence of a full disk
        b.admit("this build", 8, [])
        with mock.patch.dict(os.environ, {"WK_FORCE": "1"}):
            with contextlib.redirect_stderr(io.StringIO()):
                b.disk_admit("this build", 25, 10, "x")


MB_PER_JOB = yocto.WEBKIT_MB_PER_JOB


class TestTheDefaultsAreThePythons(WkTest):
    def test_the_bash_variables_are_the_python_constants(self):
        cp = bash(f'. "{REPO}/lib/common.sh"\nWK_RESERVE_MB=7\neval "$(wk_py wk.resources --os linux defaults)"\n'
                  'echo "$WK_RESERVE_MB $WK_RESERVE_CORES $WK_MB_PER_JOB $WK_BUILD_DISK_GB $WK_RETRY_EXIT"')
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(cp.stdout.split(), ["7", str(resources.RESERVE_CORES), str(resources.MB_PER_JOB),
                                             str(resources.DISK_GB), str(RETRY_EXIT)])


class TestTheLoadCoresAndJobCount(unittest.TestCase):
    def res(self, os_name, env=None, answers=(), files=None):
        m = Fake()
        for argv, out in answers:
            m.answer(argv, out=out)
        m.files.update(files or {})
        return resources.Resources(m, env or {}, os_name)

    def refusal(self, fn):
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            fn()
        return err.getvalue()

    def test_a_load_average_is_whole_cores_from_either_kernel(self):
        mac = self.res("macos", answers=[(["sysctl", "-n", "vm.loadavg"], "{ 3.41 2.20 1.90 }\n")])
        linux = self.res("linux", files={"/proc/loadavg": "5.93 4.00 3.00 2/900 12345\n"})
        self.assertEqual((mac.host_load(), linux.host_load()), (3, 5))

    def test_a_load_average_that_did_not_come_back_refuses_and_names_it(self):
        self.assertIn("vm.loadavg", self.refusal(self.res("macos").host_load))
        self.assertIn("/proc/loadavg", self.refusal(self.res("linux").host_load))

    def test_both_core_kinds_are_named_for_a_person_or_neither_is(self):
        both = [(["sysctl", "-n", "hw.perflevel0.logicalcpu"], "8\n"), (["sysctl", "-n", "hw.perflevel1.logicalcpu"], "4\n")]
        self.assertEqual(self.res("macos", answers=both).describe_cores(), "8 P + 4 E")
        self.assertEqual(self.res("macos", answers=[(["sysctl", "-n", "hw.ncpu"], "12\n")]).describe_cores(), "12 cores")
        self.refusal(self.res("macos", answers=both[:1]).describe_cores)

    def test_the_job_count_is_the_memory_and_cores_not_spoken_for(self):
        env = {"WK_CGROUP_CORES": "64", "WK_AVAIL_MB": "100000", "WK_MB_PER_JOB": "1000"}
        res = self.res("linux", env)
        budget = resources.Budget(res.machine, env)
        self.assertEqual(resources.build_jobs(res, budget, [("live build", 20, 40000)]), 44)

    def test_a_polite_count_reads_this_machines_load_when_no_caller_measured_one(self):
        env = {"WK_CGROUP_CORES": "12", "WK_AVAIL_MB": "100000", "WK_MB_PER_JOB": "1000"}
        res = self.res("macos", env, answers=[(["sysctl", "-n", "vm.loadavg"], "{ 3.41 2.20 1.90 }\n")])
        # 12 cores, load 3 spoken for, and never more than half a box.
        self.assertEqual(self.polite(res, env), 6)
        # A measured load stands, and with memory busy it is not read as a killed build's stale average.
        env = dict(env, WK_LOAD="9", WK_AVAIL_MB="8000")
        self.assertEqual(self.polite(self.res("macos", env), env), 3)

    def polite(self, res, env):
        return resources.Budget(res.machine, env).jobs(res.cores(), res.avail_mem_mb(), res.mb_per_job(), load=res.load())


class TestStoreFreeGb(WkTest):
    def test_it_answers_the_one_spelling_both_dfs_have(self):
        free = resources.Budget(Local(), {}).free_gb(str(self.tmp))
        avail_k = int(subprocess.run(["df", "-Pk", str(self.tmp)], capture_output=True,
                                     text=True, check=True).stdout.splitlines()[1].split()[3])
        self.assertEqual(free, -(-avail_k // 1048576))

    def test_a_store_path_df_cannot_answer_for_is_not_a_refusal(self):
        budget = resources.Budget(Local(), {})
        free = budget.free_gb(str(self.tmp / "no" / "such"))
        self.assertIsNone(free)
        budget.disk_admit("this build", 60, free, "nowhere")


class TestImageStageBudget(unittest.TestCase):
    def test_each_stage_books_what_it_uses(self):
        for stage, want in (("layers", (79, 113000)), ("fetch", (79, 113000)), ("image", (79, 113000)),
                            ("toolchain", (79, 113000)), ("webkit", (8, 8 * MB_PER_JOB)), ("pgo-mix", (1, MB_PER_JOB))):
            self.assertEqual(yocto.stage_budget(stage, 79, 113000, 8), want, stage)

    def test_a_slot_build_leaves_jobs_and_an_image_build_leaves_none(self):
        def left(booked_mb):
            return resources.Budget(Fake(), {}).jobs(80, 113000, MB_PER_JOB, running=[("booked", 8, booked_mb)])
        self.assertGreaterEqual(left(8 * MB_PER_JOB), 4)
        self.assertLess(left(113000), 4)


CANNOT_REFUSE = {"headless-marker", "defaults"}


class TestTheExemptionsDoNotRefuse(unittest.TestCase):
    def test_a_deaf_machine_answers_every_exempt_verb(self):
        for verb in CANNOT_REFUSE:
            with self.subTest(verb=verb), mock.patch("wk.machine.here", return_value=Fake()), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(resources.main(["--os", "linux", verb], env={}), 0)


class TestAReadingRefusalReachesItsCaller(WkTest):
    DEAF = {"nproc": "exit 1", "sysctl": "exit 1"}

    COMPOSITE = ("envelope-cores", "describe-cores")

    def _res(self, script, stubs=None, env=None):
        e = {"XDG_STATE_HOME": str(self.tmp / "state"), "WK_STORE": str(self.tmp / "store"), **(env or {})}
        body = f'set -euo pipefail\n. "{REPO}/lib/common.sh"\n' + script
        with stub_path(stubs if stubs is not None else self.DEAF) as binp:
            e["PATH"] = f"{binp}:{os.environ['PATH']}"
            return bash(body, env=e)

    def test_a_reader_that_reads_through_another_one_still_refuses(self):
        for name in self.COMPOSITE:
            with self.subTest(reading=name):
                cp = self._res(f'v=$(wk_py wk.resources --os "$(wk_os)" {name}); echo "SURVIVED [$v]"')
                self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertNotIn("SURVIVED", cp.stdout)
                self.assertNotIn("syntax error", cp.stderr)

    def test_the_readings_still_answer_a_machine_that_does_reply(self):
        for name in self.COMPOSITE:
            with self.subTest(reading=name):
                cp = self._res(f'v=$(wk_py wk.resources --os "$(wk_os)" {name}); echo "ANSWERED [$v]"', stubs={})
                self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertRegex(cp.stdout, r"ANSWERED \[[0-9]")

    def _guest(self, env=None):
        p = mock.patch.object(places.Vm, "tart", lambda s: "/t/tart")
        p.start()
        self.addCleanup(p.stop)
        fake = Fake("here")
        vm = places.Vm("vm", str(REPO), dict(env or {}), fake)
        return vm, guestbase.Base(vm), fake

    def test_it_walks_out_through_the_target_drivers_wrappers(self):
        vm, base, _ = self._guest()
        with self.assertRaises(Refused):
            vm.cores("g")
        with self.assertRaises(Refused):
            vm.mem_mb("g")
        with self.assertRaises(Refused):
            base.sizing()

    def test_an_override_answers_without_asking_the_machine(self):
        vm, _, fake = self._guest({"WK_VM_CPUS": "7"})
        self.assertEqual(vm.cores("g"), 7)
        self.assertNotIn(("run", ("sysctl", "-n", "hw.ncpu")), fake.effects)

        _, base, fake2 = self._guest({"WK_VM_BASE_MEM_MB": "4444"})
        fake2.answer(["sysctl", "-n", "hw.ncpu"], out="20\n")
        self.assertEqual(base.sizing()[1], "4444")
        self.assertNotIn(("run", ("sysctl", "-n", "hw.memsize")), fake2.effects)
