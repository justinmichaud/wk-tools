import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from wk.tasks import guard

PS = """    1     0  4096
  100     1  2048
  200   100 1048576
  201   200 524288
  202   201 524288
  300     1 9999999
"""
SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]


class TestReadings(unittest.TestCase):
    def test_the_tree_is_the_root_and_everything_under_it_at_any_depth(self):
        self.assertEqual(guard.tree(PS, 200), (2048, [200, 201, 202]))
        self.assertEqual(guard.tree(PS, 300), (9765, [300]))

    def test_a_cgroup_holds_its_anonymous_and_shmem_pages(self):
        self.assertEqual(guard.held_mb("anon 3221225472\nfile 99999999999\nshmem 1073741824\nanon_thp 5\n"), 4096)

    def test_available_memory_is_memavailable_and_none_where_there_is_none(self):
        self.assertEqual((guard.available_mb("MemTotal: 9 kB\nMemAvailable:    4194304 kB\n"), guard.available_mb(None)), (4096, None))

    def test_the_v2_line_of_proc_self_cgroup_is_the_own_cgroup(self):
        self.assertEqual([guard.own_cgroup(t) for t in ("0::/user.slice/a.scope\n", "0::/\n", "12:memory:/x\n", None)],
                         ["/user.slice/a.scope", "/", None, None])

    def test_oom_kills_are_counted_from_memory_events(self):
        self.assertEqual([guard.oom_kills(t) for t in ("low 0\noom 3\noom_kill 2\noom_group_kill 1\n", "oom_kill 0\n", None)], [2, 0, 0])


class TestVerdict(unittest.TestCase):
    def test_a_run_within_budget_on_a_machine_with_room_goes_on(self):
        self.assertEqual((guard.verdict(8192, 8192, 2048), guard.verdict(100, 8192, None)), (None, None))

    def test_over_budget_is_killed(self):
        self.assertEqual(guard.verdict(8193, 8192, 99999), "this run is using 8193 MB, over its 8192 MB budget")

    def test_a_machine_under_the_floor_is_reason_enough(self):
        self.assertEqual(guard.verdict(100, 8192, 2047), "the machine has 2047 MB available, under the 2048 MB floor, and this run is using 100 MB of it")


class Rooted(unittest.TestCase):
    """A root directory standing in for /: proc/self/cgroup, proc/meminfo and sys/fs/cgroup are files under it."""

    def setUp(self):
        self.root = tempfile.mkdtemp() + "/"
        self.addCleanup(shutil.rmtree, self.root)
        self.base = self.root + "sys/fs/cgroup/ws"
        os.makedirs(self.root + "proc/self")
        os.makedirs(self.base)

    def write(self, path, text):
        with open(self.root + path, "w") as f:
            f.write(text)

    def main(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = guard.main(list(argv), self.root)
        return rc, out.getvalue()


class TestCgroup(Rooted):
    def test_a_child_cgroup_is_made_with_the_budget_as_its_ceiling(self):
        path = guard.make_cgroup(self.base, 8192)
        self.assertEqual((path, guard.read(path + "/memory.max")), ("%s/wk-guard-%d" % (self.base, os.getpid()), str(8192 * 1024 * 1024)))

    def test_where_the_directory_cannot_be_made_there_is_none(self):
        self.assertIsNone(guard.make_cgroup(self.root + "absent", 8192))

    def test_where_the_ceiling_cannot_be_written_the_child_is_removed(self):
        with mock.patch("builtins.open", side_effect=PermissionError):
            self.assertIsNone(guard.make_cgroup(self.base, 8192))
        self.assertEqual(os.listdir(self.base), [])

    def test_a_ceiling_that_reads_back_different_is_not_trusted(self):
        with mock.patch.object(guard, "read", return_value="max\n"), mock.patch.object(guard, "remove") as remove:
            self.assertIsNone(guard.make_cgroup(self.base, 8192))
        remove.assert_called_once_with("%s/wk-guard-%d" % (self.base, os.getpid()))

    def test_the_empty_cgroups_a_killed_guard_left_are_removed_and_a_busy_one_is_kept(self):
        for name in ("wk-guard-1", "wk-guard-2/busy", "other"):
            os.makedirs(self.base + "/" + name)
        guard.make_cgroup(self.base, 8192)
        self.assertEqual(set(os.listdir(self.base)), {"other", "wk-guard-%d" % os.getpid(), "wk-guard-2"})


class TestMain(Rooted):
    def test_without_a_cgroup_it_says_it_watches_the_tree_and_returns_the_commands_status(self):
        rc, out = self.main("8192", "0", sys.executable, "-c", "import sys; sys.exit(7)")
        self.assertEqual((rc, out), (7, "wk: memory guard: no child cgroup can be made here, so watching the process tree every 30 s: "
                                        "budget 8192 MB, floor 2048 MB\n"))

    def test_a_workspaces_own_cgroup_is_watched_too_and_what_it_holds_over_budget_kills_the_run(self):
        self.write("proc/self/cgroup", "0::/ws\n")
        self.write("sys/fs/cgroup/ws/memory.stat", "anon 10737418240\nshmem 0\n")
        with mock.patch.object(guard, "make_cgroup", return_value=None):
            rc, out = self.main("8192", "1", *SLEEP)
        self.assertEqual((rc, out.splitlines()), (99, [
            "wk: memory guard: no child cgroup can be made here, so watching the process tree and the workspace's cgroup every 30 s: "
            "budget 8192 MB, floor 2048 MB",
            "wk: MEMORY LIMIT: this run is using 10240 MB, over its 8192 MB budget; killing it rather than leaving the OOM killer to choose"]))

    def test_a_workspace_with_no_v2_cgroup_of_its_own_is_watched_by_its_tree_alone(self):
        self.assertIn("watching the process tree every", self.main("8192", "1", sys.executable, "-c", "pass")[1])

    def test_a_machine_under_the_floor_kills_the_run(self):
        self.write("proc/meminfo", "MemAvailable: 1048576 kB\n")
        rc, out = self.main("999999", "0", *SLEEP)
        self.assertEqual(rc, 99)
        self.assertRegex(out, r"wk: MEMORY LIMIT: the machine has 1024 MB available, under the 2048 MB floor, and this run is using \d+ MB of it;")

    def test_a_command_a_signal_killed_exits_as_a_shell_reports_it(self):
        self.assertEqual(self.main("8192", "0", sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)")[0], 137)

    def test_in_a_cgroup_it_says_so_puts_the_command_in_it_and_removes_it_after(self):
        self.write("proc/self/cgroup", "0::/ws\n")
        entered = []

        def remove(cgroup):
            entered.append(guard.read(cgroup + "/cgroup.procs"))
            shutil.rmtree(cgroup)
        with mock.patch.object(guard, "remove", side_effect=remove):
            rc, out = self.main("8192", "0", sys.executable, "-c", "import os; open(os.devnull, 'w').write(str(os.getpid()))")
        self.assertEqual((rc, out, entered[0].isdigit(), os.listdir(self.base)), (0, "wk: memory guard: a cgroup with memory.max 8192 MB\n", True, []))

    def test_a_kernel_kill_inside_the_cgroup_is_a_memory_limit(self):
        self.write("proc/self/cgroup", "0::/ws\n")

        def killed(cgroup):
            with open(cgroup + "/memory.events", "w") as f:
                f.write("oom 1\noom_kill 1\n")
        with mock.patch.object(guard, "enter", side_effect=killed), mock.patch.object(guard, "remove"):
            rc, out = self.main("8192", "0", sys.executable, "-c", "raise SystemExit(1)")
        self.assertEqual((rc, out.splitlines()[1]), (99, "wk: MEMORY LIMIT: the kernel killed part of this run at its 8192 MB cgroup ceiling"))


class TestWatch(unittest.TestCase):
    def test_a_ps_that_prints_nothing_kills_the_run_whose_memory_is_then_unknown(self):
        child = subprocess.Popen(SLEEP)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = guard.watch(child, 8192, None, "/absent", ps=("true",))
        self.assertEqual((rc, child.poll() is not None), (99, True))
        self.assertIn("MEMORY LIMIT: its memory cannot be read: ps printed nothing;", out.getvalue())

    def test_a_command_that_ignores_term_is_killed_after_the_grace(self):
        child = subprocess.Popen([sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print(1, flush=True); time.sleep(60)"],
                                 stdout=subprocess.PIPE)
        child.stdout.readline()
        with mock.patch.object(guard, "GRACE", 0.2):
            guard.stop(child, [child.pid, 2 ** 22 + 12345])
        child.stdout.close()
        self.assertEqual(child.returncode, -9)

    def test_it_samples_again_after_the_interval_until_the_command_ends(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"])
        with mock.patch.object(guard, "INTERVAL", 0.1):
            self.assertEqual(guard.watch(child, 999999, None, "/absent"), 0)

    def test_the_file_runs_alone_under_an_isolated_python(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            shutil.copy(guard.__file__, elsewhere + "/guard.py")
            r = subprocess.run([sys.executable, "-I", elsewhere + "/guard.py", "999999", "0", sys.executable, "-c", "raise SystemExit(3)"],
                               stdout=subprocess.PIPE, universal_newlines=True)
        self.assertEqual((r.returncode, r.stdout.startswith("wk: memory guard: ")), (3, True))
