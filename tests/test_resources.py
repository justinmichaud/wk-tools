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


class TestDiskAdmit(WkTest):
    """disk_admit (lib/resources.sh): a build refuses before it starts when
    the store's filesystem cannot take it, so nobody has to read `wk disk`
    first. build_admit asks on the way through, which is what puts the check
    on every build path -- `wk build`, `wk test`, and both image builders."""

    def _bash(self, script, free_gb):
        env = {"XDG_STATE_HOME": str(self.tmp / "state"),
               "WK_AVAIL_MB": "100000", "WK_CGROUP_CORES": "64",
               "WK_BUILD_MACHINE": "testbox", "FREE_GB": str(free_gb)}
        # store_free_gb is what df answers; stubbed so the test does not
        # depend on the machine it runs on.
        pre = (f'set -euo pipefail\n. "{REPO}/lib/common.sh"\n'
               f'. "{REPO}/lib/resources.sh"\n'
               'store_free_gb() { printf "%s" "$FREE_GB"; }\n')
        return bash(pre + script, env=env, timeout=60)

    def test_it_refuses_when_the_disk_cannot_take_the_build(self):
        cp = self._bash('( disk_admit "this image build" 60 ) && echo admitted || echo refused', 40)
        self.assertIn("refused", cp.stdout)
        self.assertIn("40 GB free", cp.stdout + cp.stderr)
        self.assertIn("wk gc", cp.stdout + cp.stderr, "the refusal names the reclaim")
        self.assertNotIn("admitted", cp.stdout)

    def test_it_admits_when_there_is_room(self):
        cp = self._bash('disk_admit "this image build" 60 && echo admitted', 61)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("admitted", cp.stdout)

    def test_no_answer_from_df_is_not_read_as_a_full_disk(self):
        cp = self._bash('disk_admit "this build" 60 && echo admitted', "")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("admitted", cp.stdout)

    def test_every_build_path_asks_because_build_admit_does(self):
        # No running builds at all: the memory half returns early, and the
        # disk half must still have been asked.
        cp = self._bash('( build_admit "this build" 64 60 ) && echo admitted || echo refused', 10)
        self.assertIn("refused", cp.stdout)
        self.assertIn("10 GB free", cp.stdout + cp.stderr)


if __name__ == "__main__":
    unittest.main()
