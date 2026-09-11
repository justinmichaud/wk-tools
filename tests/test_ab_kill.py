"""`wk ab <task> --kill`: how an A/B that is running is stopped.

An A/B is hours of work across the boards, driven by one process whose
children are `wk sysimage`, `wk pi deploy` and `wk pi bench`. Killing the
recorded pid alone leaves those children measuring, so the kill is aimed at
the process group; the record converges to `cancelled` only once the process
is actually gone, since a record saying cancelled over a live A/B is what
sends a second one at the same board.

The stub A/B is a real process group: a session leader (os.setsid) with a
child, orphaned at birth so nothing here holds it as a zombie -- a zombie
answers `kill -0` and would read as alive.

Run: python3 -m unittest tests.test_ab_kill -v
"""
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from tests.support import REPO, bash, scratch_dir

AB = (REPO / "cmd" / "ab").read_text()

# The three functions, as one region of the file: func_body takes a body, and
# _ab_pgid is a one-liner whose `}` shares its line.
LIFTED = AB[AB.index("_ab_pgid() {"):AB.index("\nSTAMP=")]
for _name in ("_ab_pgid", "_ab_wait_gone", "ab_kill"):
    assert "\n%s() {" % _name in "\n" + LIFTED, "%s is not in cmd/ab's kill region" % _name

# bench_task_dir is lib/bench.sh's, named in the report ab_kill prints.
FUNCS = f'''set -euo pipefail
cd "{REPO}"
. lib/common.sh
. lib/task.sh
bench_task_dir() {{ printf '%s' "$WK_STORE/bench/$1"; }}
{LIFTED}
'''

# A process group nothing in this test is the parent of: fork, exit the
# parent, and the session leader is adopted by init.
SPAWN = '''
import os, signal, subprocess, sys, time
if os.fork() != 0:
    os._exit(0)
os.setsid()
if sys.argv[2] == "ignore":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen(["bash", "-c", "trap '' TERM; sleep 60"])
else:
    child = subprocess.Popen(["sleep", "60"])
open(sys.argv[1], "w").write("%d %d\\n" % (os.getpid(), child.pid))
time.sleep(60)
'''


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class TestAbKill(unittest.TestCase):
    def setUp(self):
        self.d = self.enterContext(scratch_dir(prefix="wk-test-ab-kill-"))
        self.store = self.d / "store"
        (self.store / "task").mkdir(parents=True)
        self.pids = []

    def tearDown(self):
        for pid in self.pids:
            try:
                os.killpg(pid, 9)
            except OSError:
                pass

    def spawn(self, mode="term"):
        """(leader pid, child pid) of a live stub A/B, its own process group."""
        marker = self.d / "pids"
        subprocess.run([sys.executable, "-c", SPAWN, str(marker), mode],
                       check=True, timeout=20)
        for _ in range(100):
            if marker.exists() and marker.read_text().strip():
                break
            time.sleep(0.05)
        leader, child = (int(x) for x in marker.read_text().split())
        self.pids.append(leader)
        return leader, child

    def run_kill(self, task, before="", env=None):
        e = {"WK_STORE": str(self.store)}
        e.update(env or {})
        return bash(FUNCS + before + f'\nab_kill {task!r} && echo "rc=0" || echo "rc=$?"\n',
                    env=e, timeout=120)

    def record(self, task, pid=None):
        """One `ab` task record, written through lib/task.sh the way cmd/ab
        writes it, with <pid> as the process doing the work."""
        cp = bash(FUNCS + f'''
dir=$(task_begin ab here {task!r} "wk ab {task} --kill" "$WK_STORE/bench/{task}/ab.log" \
    "wk sysimage webkit" "deploy and benchmark on rpi3" report)
{"task_pid \"$dir\" %d" % pid if pid else ""}
printf '%s' "$dir"
''', env={"WK_STORE": str(self.store)})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return Path(cp.stdout.strip())

    def test_the_process_group_goes_and_the_record_says_cancelled(self):
        leader, child = self.spawn()
        dir_ = self.record("t1", leader)
        cp = self.run_kill("t1")
        out = cp.stdout + cp.stderr
        self.assertIn("rc=0", cp.stdout, out)
        self.assertIn("process group", out)
        self.assertIn("cancelled", out)
        self.assertEqual("cancelled", (dir_ / "exit").read_text().strip())
        self.assertFalse(alive(leader), "the recorded process is still there")
        self.assertFalse(alive(child), "the A/B's child is still measuring")

    def test_a_process_that_ignores_term_is_killed_after_the_bound(self):
        """SIGTERM is what a board's run gets the chance to unwind on; a
        process group that does not take it is killed, and the bound is short
        enough that nobody waits for an hour."""
        leader, child = self.spawn(mode="ignore")
        dir_ = self.record("t2", leader)
        cp = self.run_kill("t2", env={"WK_AB_KILL_WAIT": "1"})
        out = cp.stdout + cp.stderr
        self.assertIn("rc=0", cp.stdout, out)
        self.assertIn("after SIGTERM", out)
        self.assertEqual("cancelled", (dir_ / "exit").read_text().strip())
        self.assertFalse(alive(leader), out)
        self.assertFalse(alive(child), out)

    def test_no_record_is_a_refusal_naming_where_the_tasks_are_listed(self):
        cp = self.run_kill("nosuchtask")
        out = cp.stdout + cp.stderr
        self.assertNotIn("rc=0", cp.stdout, out)
        self.assertIn("no A/B task 'nosuchtask'", out)
        self.assertIn("wk bench ls", out)

    def test_a_finished_task_is_not_killed_and_its_runs_are_named(self):
        dir_ = self.record("t3", os.getpid())
        bash(FUNCS + f'task_end {str(dir_)!r} 0', env={"WK_STORE": str(self.store)})
        cp = self.run_kill("t3")
        out = cp.stdout + cp.stderr
        self.assertNotIn("rc=0", cp.stdout, out)
        self.assertIn("is not running", out)
        self.assertIn("wk bench report t3", out)

    def test_a_record_pointing_at_this_command_own_group_is_refused(self):
        """The recorded process sharing this one's process group means the
        kill would take the killer with it before the record converged."""
        dir_ = self.record("t4")
        before = f'''
sleep 10 &
task_pid {str(dir_)!r} "$!"
'''
        cp = self.run_kill("t4", before=before)
        out = cp.stdout + cp.stderr
        self.assertNotIn("rc=0", cp.stdout, out)
        self.assertIn("own process group", out)
        self.assertIn("from another shell", out)
        self.assertEqual("", (dir_ / "exit").read_text() if (dir_ / "exit").exists() else "")

    def test_the_kill_command_the_record_names_is_this_one(self):
        """A task record's `kill` field is the literal a person types, and
        cmd/ab's is the flag implemented here."""
        self.assertIn('task_begin ab here "$TASK" "wk ab $TASK --kill"', AB)


if __name__ == "__main__":
    unittest.main()
