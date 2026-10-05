"""Build accounting (lib/wk/resources.py), and the rule that a refused reading reaches the caller."""
import contextlib
import io
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.support import REPO, WkTest, bash, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import places, resources  # noqa: E402
from wk.act import RETRY_EXIT, Refused  # noqa: E402
from wk.machine import Fake, Local  # noqa: E402
from wk.sysimage import guestbase, yocto  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
