import unittest

from wk.core.errors import Refused
from wk.core.fakes import Fake
from wk.core.machine import Result
from wk.tasks.sizing import PROBE, Reading, jobs, measure, parse, size

GB = 1024


class TestParse(unittest.TestCase):
    def test_a_linux_reading_is_cores_and_available_memory(self):
        self.assertEqual(parse("cores 16\navailable_kb 33554432\n", "lin"), Reading(16, 32 * GB))

    def test_a_cgroups_memory_and_cpu_ceilings_are_read_and_max_is_none(self):
        text = "cores 64\navailable_kb 268435456\n"
        self.assertEqual(parse(text + "memory.max 17179869184\ncpu.max 800000 100000\n", "lin"), Reading(64, 256 * GB, 16 * GB, 8))
        self.assertEqual(parse(text + "memory.max max\ncpu.max max 100000\n", "lin"), Reading(64, 256 * GB))

    def test_a_cpu_ceiling_under_one_core_is_one(self):
        self.assertEqual(parse("cores 4\navailable_kb 8388608\ncpu.max 50000 100000\n", "lin").ceiling_cores, 1)

    def test_a_mac_reading_is_total_memory(self):
        self.assertEqual(parse("cores 10\ntotal_bytes 34359738368\n", "mac"), Reading(10, 32 * GB))

    def test_each_reading_that_cannot_be_taken_is_fatal(self):
        whole = {"cores": "cores 16", "available_kb": "available_kb 33554432", "memory.max": "memory.max max",
                 "cpu.max": "cpu.max max 100000"}
        for key, bad in (("cores", "cores"), ("cores", ""), ("available_kb", "available_kb "), ("memory.max", "memory.max "),
                         ("memory.max", "memory.max lots"), ("cpu.max", "cpu.max 100000"), ("cpu.max", "cpu.max ")):
            text = "\n".join(dict(whole, **{key: bad}).values())
            with self.assertRaisesRegex(Refused, "cannot read %s on lin .*nothing is built from a guess\n    the probe is PROBE" % key, msg=bad):
                parse(text, "lin")


class TestMeasure(unittest.TestCase):
    def test_the_probe_runs_once_where_it_is_told_to(self):
        m = Fake("lin")
        m.on(["in-ws"], "cores 8\navailable_kb 16777216\n")
        self.assertEqual((measure(m, ["in-ws"] + PROBE), [c.argv[:3] for c in m.calls]), (Reading(8, 16 * GB), [("in-ws", "sh", "-c")]))

    def test_a_probe_that_fails_is_fatal(self):
        m = Fake("lin")
        m.on(["sh"], Result(126, "", "container is not running"))
        self.assertRaisesRegex(Refused, r"cannot measure lin \(the sizing probe exited 126: container is not running\): nothing is built from a guess",
                               measure, m, PROBE)


class TestJobs(unittest.TestCase):
    def test_the_table(self):
        for why, want, reading, mb, cap in (
                ("cores bind", 16, Reading(16, 64 * GB), 1536, None),
                ("memory binds: (32768 - 2048) // 4608", 6, Reading(16, 32 * GB), 4608, None),
                ("the cap binds", 4, Reading(16, 64 * GB), 1536, 4),
                ("a cap over the cores does not raise them", 16, Reading(16, 64 * GB), 1536, 99),
                ("never fewer than one", 1, Reading(16, 3 * GB), 4608, None),
                ("the cgroup's cpu.max is the ceiling", 8, Reading(32, 256 * GB, None, 8), 1536, None),
                ("the cgroup's memory.max, not the machine's memory", 9, Reading(64, 256 * GB, 16 * GB), 1536, None),
                ("a ceiling above what is available changes nothing", 20, Reading(64, 32 * GB, 256 * GB), 1536, None),
                ("a Mac: (total - reserve) // 3072", 4, Reading(10, 16 * GB), 3072, None)):
            self.assertEqual(jobs(reading, mb, cap), want, why)

    def test_the_environment_overrides_the_figure_and_caps_the_count(self):
        reading = Reading(16, 32 * GB)
        self.assertEqual([size(reading, 4608, env) for env in ({}, {"WK_MB_PER_JOB": "10240"}, {"WK_MAX_JOBS": "2"},
                                                                      {"WK_MAX_JOBS": "5", "WK_MB_PER_JOB": "1024"})],
                         [(6, 4608), (3, 10240), (2, 4608), (5, 1024)])
