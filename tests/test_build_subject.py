"""What a running build is of, and what a watchdog is measuring."""
import subprocess
import sys
import unittest

from tests.support import REPO, WkTest, bash, builds_on_the_books_env, func_body, run

sys.path.insert(0, str(REPO / "lib"))
from wk.clock import Clock  # noqa: E402


class TestTheRecordCarriesIt(WkTest):
    def test_a_reader_sees_it_against_the_running_build(self):
        subject = ("slot base in yocto-p at 6f7bb97a3e06 -- instrumented, "
                   "to collect a profile from -- not a measurement")
        from tests.test_status import render
        out = render([{"kind": "machine", "name": "moose"},
                     {"kind": "task", "machine": "moose", "task": "yocto-x", "task_kind": "yocto", "name": "yocto-p",
                      "state": "running", "since": "2026-09-16T20:00:00Z", "subject": subject, "steps": ["running"],
                      "plan": ["wk sysimage webkit ..."], "kill": "wk ... --stop"}])
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        self.assertIn(subject, lines)
        self.assertLess(lines.index(subject), lines.index("[>] wk sysimage webkit ..."))


class TestTheWatchdogMeasuresWhatDetached(WkTest):
    WATCHDOG = REPO / "build" / "mem-watchdog.sh"

    def _lift(self, func):
        return func_body(self.WATCHDOG.read_text(), func)

    def test_the_cgroup_reading_is_in_megabytes(self):
        cp = bash('_cgroup_read() { printf "anon 12884901888\\nfile 99999999999\\nshmem 536870912\\nslab 8888888888\\n"; }\n'
                  + self._lift("_cgroup_mb").replace(
                      '[ -r /sys/fs/cgroup/memory.stat ] || return 0\n'
                      "    awk '$1 == \"anon\" || $1 == \"shmem\" { s += $2 } END { printf \"%d\\n\", s / 1048576 }' /sys/fs/cgroup/memory.stat",
                      "_cgroup_read | awk '$1 == \"anon\" || $1 == \"shmem\" { s += $2 } END { printf \"%d\\n\", s / 1048576 }'")
                  + "\n_cgroup_mb")
        self.assertEqual(cp.stdout.strip(), "12800", cp.stdout + cp.stderr)

    def test_a_build_that_ended_unreaped_ends_the_watchdog(self):
        """tart's guest agent reaps the build only after the watchdog lets go of the session's output."""
        zombie = subprocess.Popen(["true"])
        try:
            self.assertTrue(Clock().wait_until(lambda: subprocess.run(["ps", "-o", "stat=", "-p", str(zombie.pid)],
                                                                      capture_output=True, text=True).stdout[:1] == "Z", 10, 0.05))
            cp = subprocess.run(["bash", str(self.WATCHDOG), str(zombie.pid), "999999"], capture_output=True, text=True, timeout=10)
            self.assertEqual(0, cp.returncode, cp.stderr)
        finally:
            zombie.wait()

    def test_nothing_comes_back_where_there_is_no_cgroup(self):
        cp = bash(self._lift("_cgroup_mb") + "\necho \"[$(_cgroup_mb)]\"")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)


class TestSelftestRefusesBesideABuild(WkTest):
    def test_a_live_run_is_a_barrier_naming_the_build_and_the_way_on(self):
        env = builds_on_the_books_env(self.tmp, "wk-test-fake-build")
        cp = run("selftest", "--live", "nosuchtestzz", env=env)
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("a build is on this machine's books", cp.stdout)
        self.assertIn("wk-test-fake-build", cp.stdout)
        self.assertIn("--force proceeds anyway", cp.stdout)
        self.assertIn("wk selftest\n", cp.stdout)
        self.assertNotIn("tiers:", cp.stdout)


if __name__ == "__main__":
    unittest.main()
