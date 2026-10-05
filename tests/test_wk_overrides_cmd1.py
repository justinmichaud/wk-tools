"""WK_* override coverage for cmd/bench, cmd/machine and cmd/build (the"""
import unittest

from tests.support import WkTest, fake_workspace
from tests.test_bench_pipeline import BenchTest, World

from wk.act import Refused  # noqa: E402


class TestBenchRunKnobs(BenchTest):

    def _load(self, load, env=None):
        self.w = World(self.tmp)
        self.w.files["/proc/loadavg"] = "%s 1.00 1.00 1/100 1\n" % load
        try:
            self.run_(extra=env)
        except Refused:
            return "busy"
        return "idle"

    def test_default_threshold_is_4(self):
        self.assertEqual((self._load("5.00"), self._load("4.00")), ("busy", "idle"))

    def test_override_raises_the_threshold(self):
        self.assertEqual(self._load("5.00", {"WK_BENCH_MAX_LOAD": "10"}), "idle")

    def test_aslr_off_prefixes_setarch(self):
        self.run_(extra={"WK_BENCH_ASLR": "off"})
        self.assertIn("setarch $(uname -m) -R -- ", self.w.watched[0][-1])

    def test_aslr_default_is_empty(self):
        self.run_()
        self.assertNotIn("setarch", self.w.watched[0][-1])


class TestBuildDryRunKnobs(WkTest):
    def test_the_memory_interval_is_read(self):
        with fake_workspace() as ws:
            cp = ws.run("build", "jsc-release", "--dry-run", env={"WK_MEM_INTERVAL": "7"})
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("watched every 7s", cp.stdout)


if __name__ == "__main__":
    unittest.main()
