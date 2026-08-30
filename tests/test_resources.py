"""Build accounting (lib/resources.sh): every build wk starts leaves a
budget record while it runs, the next build is sized against what is left,
and a machine spoken for refuses rather than oversubscribes. Records are
lock-shaped -- a dead holder's record is pruned on the next read.

Run: python3 -m unittest tests.test_resources -v
"""
import os
import subprocess
import unittest

from tests.support import REPO, WkTest, bash


class TestBuildRecords(WkTest):
    def _bash(self, script):
        env = {"XDG_STATE_HOME": str(self.tmp / "state"), "WK_AVAIL_MB": "100000",
               "WK_CGROUP_CORES": "64", "WK_MB_PER_JOB": "1000", "WK_BUILD_MACHINE": "testbox"}
        return bash(f'set -euo pipefail\n. "{REPO}/lib/common.sh"\n. "{REPO}/lib/resources.sh"\n' + script, env=env, timeout=60)

    def test_a_live_record_is_subtracted_and_a_dead_one_pruned(self):
        cp = self._bash('''
sleep 300 & live=$!
build_record "live build" 20 40000 "pid:$live"
build_record "dead build" 30 50000 "pid:99999999"
echo "reserved=$(build_reserved_mb) jobs=$(build_reserved_jobs)"
echo "records=$(ls "$(builds_dir)" | wc -l | tr -d ' ')"   # BSD wc pads its count
echo "next=$(build_jobs)"
kill $live
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("reserved=40000 jobs=20", cp.stdout)
        self.assertIn("records=1", cp.stdout, "the dead holder's record is pruned on read")
        # (100000 - 40000) / 1000 = 60 by memory, 64 - 20 = 44 by cores
        self.assertIn("next=44", cp.stdout)

    def test_a_machine_spoken_for_refuses_without_force(self):
        cp = self._bash('''
sleep 300 & live=$!
trap 'kill $live' EXIT
build_record "big build" 60 98000 "pid:$live"
jobs=$(build_jobs); echo "jobs=$jobs"
( build_admit "a second build" "$jobs" ) && echo admitted || echo refused
''')
        self.assertIn("jobs=2", cp.stdout)
        self.assertIn("--force proceeds anyway", cp.stdout + cp.stderr)
        self.assertNotIn("admitted", cp.stdout)

    def test_another_machines_builds_do_not_count(self):
        cp = self._bash('''
sleep 300 & live=$!
WK_BUILD_MACHINE=elsewhere build_record "remote build" 60 98000 "pid:$live"
echo "reserved=$(build_reserved_mb)"
kill $live
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("reserved=0", cp.stdout)


if __name__ == "__main__":
    unittest.main()
