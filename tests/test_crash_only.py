"""Crash-only convergence (CLAUDE.md rule 2): a killed mutating command re-runs to the declared final state --
`wk new`/`wk rm` over a killed creation driver, and ./setup's home-scoped stages killed with SIGKILL."""
import os
import re
import subprocess
import sys
import time
import unittest

from tests.support import (
    REPO,
    WkTest,
    container_side,
    rand_suffix,
    requires_container_place,
    run,
    scratch_dir,
)

sys.path.insert(0, str(REPO / "lib"))
from wk import record, workspace  # noqa: E402
from wk.machine import Local  # noqa: E402


def _wait_dead(pid, timeout=60):
    """Poll until <pid>, where the container place runs it, is no longer alive."""
    waited = 0
    while waited < timeout:
        cp = container_side(f"kill -0 {pid} 2>/dev/null")
        if cp.returncode != 0:
            return True
        time.sleep(1)
        waited += 1
    return False


def _wait_registered(name, timeout=600):
    """Poll until <name> exists at all, so a kill leaves rubble rather than nothing."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if run("status", name, "--json").returncode == 0:
            return True
        time.sleep(0.5)
    return False


@requires_container_place()
class TestWkNewKilledMidway(WkTest):

    def setUp(self):
        super().setUp()
        self.name = f"wk-test-{rand_suffix()}"
        self._created = False

    def tearDown(self):
        if self._created:
            cp = run("rm", self.name, env={"WK_YES": "1"})
            if cp.returncode != 0:
                print(f"[teardown] 'wk rm {self.name}' exited {cp.returncode}: {cp.stdout}")
        super().tearDown()

    def test_killed_driver_then_rerun_converges(self):
        cp = run("new", self.name, "--on", "container", "--no-wait", timeout=120)
        self.assertEqual(cp.returncode, 0, f"'wk new --no-wait' failed: {cp.stdout}")

        m = re.search(r"detached as pid (\d+)", cp.stdout)
        self.assertIsNotNone(m, f"'wk new --no-wait' did not report a driver pid: {cp.stdout}")
        pid = m.group(1)

        container_side(f"kill -9 {pid}")
        self.assertTrue(_wait_dead(pid), f"driver pid {pid} did not die")

        self._created = True

        st = run("status", self.name, "--json")
        self.assertNotIn('"state":"present"', st.stdout.replace(" ", ""), st.stdout)
        cp2 = run("new", self.name, "--on", "container", timeout=600)
        self.assertEqual(cp2.returncode, 0, cp2.stdout)
        self.assertIn(self.name, run("ls").stdout)


@requires_container_place()
class TestWkRmOfRubble(WkTest):

    def setUp(self):
        super().setUp()
        self.name = f"wk-test-{rand_suffix()}"
        self.addCleanup(self._remove)

    def _remove(self):
        cp = run("rm", self.name, env={"WK_YES": "1"}, timeout=180)
        if cp.returncode != 0 and "no such workspace" not in cp.stdout:
            print(f"[teardown] 'wk rm {self.name}' exited {cp.returncode}: {cp.stdout}")

    def test_rm_converges_on_a_killed_creation(self):
        cp = run("new", self.name, "--on", "container", "--no-wait", timeout=120)
        self.assertEqual(cp.returncode, 0, f"'wk new --no-wait' failed: {cp.stdout}")
        m = re.search(r"detached as pid (\d+)", cp.stdout)
        self.assertIsNotNone(m, cp.stdout)
        pid = m.group(1)

        self.assertTrue(_wait_registered(self.name))
        container_side(f"kill -9 {pid}")
        self.assertTrue(_wait_dead(pid), f"driver pid {pid} did not die")

        cp2 = run("rm", self.name, env={"WK_YES": "1"}, timeout=180)
        self.assertEqual(cp2.returncode, 0, cp2.stdout)
        self.assertNotIn(self.name, run("ls").stdout)


class TestRmTakesTheWorkspacesRecordsWithIt(unittest.TestCase):
    """`wk rm` takes a workspace's task records, and names a running job's kill command instead."""

    def _records(self, tmp):
        return record.Records(tmp, env={"WK_STORE": str(tmp)}, machine=Local())

    def test_a_running_job_is_named_with_the_command_that_stops_it(self):
        with scratch_dir(prefix="wk-test-rm-records-") as tmp:
            recs = self._records(tmp)
            recs.begin("build", "here", "ws1", "wk build ws1 --kill", "/nolog", ["compile"])
            recs.begin("test", "here", "ws1", "wk test ws1 --kill", "/nolog", ["jsc"], pid=4194304)
            out = workspace.live_task_lines(recs, "ws1")
            self.assertIn("wk build ws1 --kill", out)
            self.assertNotIn("wk test ws1 --kill", out, "a record whose pid is gone is not a running job")

    def test_every_record_of_that_workspace_goes_and_no_others(self):
        with scratch_dir(prefix="wk-test-rm-records-") as tmp:
            recs = self._records(tmp)
            recs.begin("build", "here", "ws1", "wk build ws1 --kill", "/nolog", ["compile"], pid=4194304)
            recs.begin("test", "here", "ws1", "wk test ws1 --kill", "/nolog", ["jsc"], pid=4194304)
            recs.begin("build", "here", "ws2", "wk build ws2 --kill", "/nolog", ["compile"], pid=4194304)
            workspace.remove_task_records(recs, "ws1")
            left = [t.id for t in recs.list()]
            self.assertEqual(len(left), 1, left)
            self.assertTrue(left[0].startswith("build-ws2-"), left)

# A scratch HOME is a whole machine for these stages; the rest change the machine itself.
HOME_SCOPED = ("dotfiles", "claude")
NEEDS_THE_MACHINE = ("tools", "settings", "sharing", "machine",
                     "vmtools", "softnet", "sdk", "broker", "quiesce")


class TestSetupStagesConverge(WkTest):

    def _home(self):
        home = self.tmp / f"home-{rand_suffix()}"
        (home / ".config").mkdir(parents=True)
        return home

    def _env(self, home):
        env = dict(os.environ)
        env.update({"HOME": str(home), "XDG_STATE_HOME": str(home / ".state"),
                    "XDG_CONFIG_HOME": str(home / ".config"),
                    "XDG_DATA_HOME": str(home / ".data"),
                    "CLAUDE_CONFIG_DIR": str(home / ".claude")})
        return env

    def _setup(self, home, stage, kill_after=None):
        proc = subprocess.Popen(
            [str(REPO / "setup"), "--stage", stage], cwd=str(REPO),
            env=self._env(home), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)
        try:
            out, _ = proc.communicate(timeout=kill_after)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        return proc.returncode, out

    def test_a_stage_run_twice_reports_no_changes_the_second_time(self):
        for stage in HOME_SCOPED:
            with self.subTest(stage=stage):
                home = self._home()
                rc, first = self._setup(home, stage)
                self.assertEqual(0, rc, first)
                self.assertIn("change(s) applied", first, first)
                rc, again = self._setup(home, stage)
                self.assertEqual(0, rc, again)
                self.assertIn("no changes", again, again)

    def test_a_stage_killed_at_any_point_converges_on_a_re_run(self):
        for stage in HOME_SCOPED:
            for after in (0.02, 0.05, 0.1, 0.2, 0.4):
                with self.subTest(stage=stage, killed_after=after):
                    home = self._home()
                    self._setup(home, stage, kill_after=after)
                    rc, out = self._setup(home, stage)
                    self.assertEqual(0, rc, out)
                    rc, out = self._setup(home, stage)
                    self.assertEqual(0, rc, out)
                    self.assertIn("no changes", out)

    def test_a_half_made_link_is_replaced_rather_than_accepted(self):
        for wrong in ("dangling", "a real file", "a directory"):
            with self.subTest(shape=wrong):
                home = self._home()
                rc, out = self._setup(home, "dotfiles")
                self.assertEqual(0, rc, out)
                link = home / ".lldbinit"
                link.unlink()
                if wrong == "dangling":
                    link.symlink_to(home / "gone")
                elif wrong == "a real file":
                    link.write_text("someone else's\n")
                else:
                    link.mkdir()
                rc, out = self._setup(home, "dotfiles")
                self.assertEqual(0, rc, out)
                self.assertEqual((REPO / "dotfiles" / "lldbinit").resolve(),
                                 link.resolve(), out)
                rc, out = self._setup(home, "dotfiles")
                self.assertIn("no changes", out, out)

    def test_every_stage_setup_runs_is_covered_or_named_as_owed(self):
        stages = re.findall(r"^run_stage\s+(\S+)", (REPO / "setup").read_text(),
                            re.M)
        self.assertTrue(stages)
        self.assertEqual(sorted(stages),
                         sorted(set(HOME_SCOPED) | set(NEEDS_THE_MACHINE)))


if __name__ == "__main__":
    unittest.main()
