"""What a running build is of, and what a watchdog is measuring."""
import os
import subprocess
import sys
import unittest

from tests.support import REPO, WkTest, bash, builds_on_the_books_env, func_body, run

sys.path.insert(0, str(REPO / "lib"))
from wk.sysimage import yocto  # noqa: E402
from wk.clock import Clock  # noqa: E402

SHA = "a" * 40
WS = "yocto-p"


class TestABuildSaysWhatItIsOf(WkTest):
    """yocto.build_subject; the instrumented slot and the mix stage are tests/test_images.py's."""


    def test_the_two_are_not_the_same_words(self):
        self.assertNotEqual(yocto.build_subject(WS, "webkit", "base-instr", SHA, "wpe-cross-pgo-collect"),
                            yocto.build_subject(WS, "webkit", "base", SHA, "wpe-cross-pgo-use"))

    def test_an_image_stage_says_the_stage_and_the_image_workspace(self):
        self.assertEqual(yocto.build_subject(WS, "image", "", "", ""), "image stage of " + WS)

    def test_the_image_workspace_is_named_every_time(self):
        for stage in ("webkit", "pgo-mix", "image"):
            with self.subTest(stage=stage):
                self.assertIn(WS, yocto.build_subject(WS, stage, "s", SHA, "wpe-cross"))


class TestTheRecordCarriesIt(WkTest):


    def test_a_reader_sees_it_against_the_running_build(self):
        subject = ("slot base in yocto-p at 6f7bb97a3e06 -- instrumented, "
                   "to collect a profile from -- not a measurement")
        from tests.test_status import render
        cp = render([{"kind": "machine", "name": "moose"},
                     {"kind": "task", "machine": "moose", "task": "yocto-x", "task_kind": "yocto", "name": "yocto-p",
                      "state": "running", "since": "2026-09-16T20:00:00Z", "subject": subject, "steps": ["running"],
                      "plan": ["wk sysimage webkit ..."], "kill": "wk ... --stop"}])
        lines = [l.strip() for l in cp.stdout.splitlines() if l.strip()]
        self.assertIn(subject, lines)
        self.assertLess(lines.index(subject), lines.index("[>] wk sysimage webkit ..."))


class TestTheWatchdogMeasuresWhatDetached(WkTest):

    WATCHDOG = REPO / "build" / "mem-watchdog.sh"

    def _lift(self, func):
        return func_body(self.WATCHDOG.read_text(), func)

    def test_the_cgroup_reading_is_in_megabytes(self):
        cp = bash('_cgroup_read() { echo 13421772800; }\n'
                  + self._lift("_cgroup_mb").replace(
                      '[ -r /sys/fs/cgroup/memory.current ] || return 0\n'
                      "    awk '{ printf \"%d\\n\", $1 / 1048576 }' /sys/fs/cgroup/memory.current",
                      "_cgroup_read | awk '{ printf \"%d\\n\", $1 / 1048576 }'")
                  + "\n_cgroup_mb")
        self.assertEqual(cp.stdout.strip(), "12800", cp.stdout + cp.stderr)

    def test_a_build_that_ended_unreaped_ends_the_watchdog(self):
        """tart's guest agent reaps the build only after the watchdog lets go of the session's output."""
        zombie = subprocess.Popen(["true"])
        try:
            self.assertTrue(Clock().wait_until(lambda: subprocess.run(["ps", "-o", "stat=", "-p", str(zombie.pid)],
                                                                      capture_output=True, text=True).stdout[:1] == "Z", 10, 0.05))
            cp = subprocess.run(["bash", str(self.WATCHDOG), str(zombie.pid), "999999"], capture_output=True, text=True,
                                timeout=10, env=dict(os.environ, WK_MEM_INTERVAL="1"))
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

    def test_a_killed_builds_record_is_not_on_the_books(self):
        from tests.support import _BUILD_RECORDS
        books = self.tmp / "state" / "wk" / "builds"
        books.mkdir(parents=True)
        dead = subprocess.Popen(["true"]); dead.wait()
        (books / "a").write_text("label=killed\nholder=pid:%d\n" % dead.pid)
        (books / "b").write_text("label=running\nholder=pid:%d\n" % os.getpid())
        (books / "c").write_text("label=in-a-workspace\nholder=ws:w:build.pid\n")
        out = subprocess.run(["bash", "-c", _BUILD_RECORDS], capture_output=True, text=True,
                             env=dict(os.environ, XDG_STATE_HOME=str(self.tmp / "state"))).stdout
        labels = [l.split("=", 1)[1] for l in out.splitlines() if l.startswith("label=")]
        self.assertEqual(sorted(labels), ["in-a-workspace", "running"])

    def test_the_tiers_that_need_no_machine_run_beside_a_build(self):
        env = builds_on_the_books_env(self.tmp, "wk-test-fake-build")
        cp = run("selftest", "test_the_two_are_not_the_same_words", env=env)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("tiers: lint,unit  tests: 1 ", cp.stdout)


if __name__ == "__main__":
    unittest.main()
