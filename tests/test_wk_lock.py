"""lib/wk/lock.py, the one lock (CLAUDE.md rule 4): one holder at a time, a
lock that dies with its holder, a live holder waited for by the clock and
given up on, and reporting commands that take none.

Run: python3 tests/run.py -k tests.test_wk_lock
"""
import ast
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

from tests.support import REPO, run, scratch_dir

sys.path.insert(0, str(REPO / "lib"))
from wk import machine, store  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.lock import Lock  # noqa: E402
from wk.store import Store  # noqa: E402

PAYLOAD = re.compile(r"^pid=(\d+) tok=[0-9a-f]{8} at=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ cmd=\S+$")
DEAD = "pid=4242 tok=deadbeef at=2000-01-01T00:00:00Z cmd=wk"


class TestOneLockReader(unittest.TestCase):
    """lint.one_lock_reader: a lock's payload is parsed in lib/wk/lock.py alone."""
    wk_tier = "lint"

    def test_only_lock_py_defines_the_payload_reader(self):
        defs = []
        for path in sorted((REPO / "lib" / "wk").glob("*.py")):
            tree = ast.parse(path.read_text(), str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in ("holder_pid", "_pid_of"):
                    defs.append(path.name)
        self.assertEqual(set(defs), {"lock.py"}, "a lock's payload is read in lib/wk/lock.py alone: %s" % defs)
        self.assertFalse(hasattr(store, "lock_holder_pid"))


class LockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk-test-lock-")
        self.addCleanup(machine.Local().remove, self.tmp)
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        for f in ("WK_DRY_RUN", "WK_QUIET", "WK_CMD"):
            os.environ.pop(f, None)
        self.store = Store({"HOME": self.tmp, "WK_LOCK_DIR": os.path.join(self.tmp, "locks"), "WK_CMD": "new"})
        self.path = self.store.lock_path("r")

    def stderr(self, fn):
        buf = io.StringIO()
        with redirect_stderr(buf):
            fn()
        return buf.getvalue()


class TestOnTheFake(LockTest):
    def setUp(self):
        super().setUp()
        self.fake = machine.Fake("box")
        self.clock = FakeClock()
        self.lock = Lock(self.store, self.fake, self.clock)

    def links(self):
        return [e for e in self.fake.effects if e[0] == "symlink"]

    def test_a_held_lock_is_a_symlink_to_the_payload_and_release_takes_it_away(self):
        self.lock.hold("r")
        self.assertRegex(self.fake.files[self.path], PAYLOAD)
        self.assertEqual(self.fake.files[self.path], self.lock.payload)
        self.assertTrue(self.fake.files[self.path].endswith(" cmd=new"))
        self.assertEqual(self.lock.holder_pid("r"), os.getpid())
        self.assertEqual(self.fake.applied, 2)   # mkdir_now the lock dir, then the symlink itself
        self.lock.release_all()
        self.assertNotIn(self.path, self.fake.files)
        self.assertIsNone(self.lock.holder_pid("r"))
        self.assertEqual(self.lock.holding, [])

    def test_holding_a_lock_twice_in_one_process_is_one_hold(self):
        self.lock.hold("r")
        self.lock.hold("r")
        self.assertEqual(len(self.links()), 1)
        self.lock.release_all()
        self.assertNotIn(self.path, self.fake.files)

    def test_a_dead_holder_is_broken_without_waiting(self):
        self.fake.dirs.add(os.path.dirname(self.path))
        self.fake.files[self.path] = DEAD
        self.lock.hold("r")
        self.assertEqual(self.fake.files[self.path], self.lock.payload)
        self.assertEqual([p for p in self.fake.files if p != self.path], [])
        self.assertEqual(self.clock.slept, [])

    def test_a_live_holder_is_waited_for_and_given_up_on(self):
        self.fake.dirs.add(os.path.dirname(self.path))
        self.fake.files[self.path] = DEAD
        self.fake.pids.add(4242)
        err = self.stderr(lambda: self.assertRaises(Refused, self.lock.hold, "r", timeout=3))
        self.assertEqual(err.count("waiting for the r lock (held by pid 4242)"), 1)
        self.assertIn("could not take the r lock within 3s -- pid 4242 still holds it", err)
        self.assertEqual(self.clock.slept, [1, 1, 1])
        self.assertEqual(self.fake.files[self.path], DEAD)
        self.assertEqual(self.lock.holding, [])

    def test_a_live_holder_that_lets_go_is_followed(self):
        fake, path = self.fake, self.path

        class Releasing(FakeClock):
            def sleep(self, seconds):
                super().sleep(seconds)
                if len(self.slept) == 2:
                    del fake.files[path]
        clock = Releasing()
        lock = Lock(self.store, self.fake, clock)
        fake.dirs.add(os.path.dirname(path))
        fake.files[path] = DEAD
        fake.pids.add(4242)
        self.stderr(lambda: lock.hold("r"))
        self.assertEqual(clock.slept, [1, 1])
        self.assertEqual(fake.files[path], lock.payload)

    def test_a_lock_naming_no_holder_is_cleared_with_a_warning(self):
        self.fake.dirs.add(os.path.dirname(self.path))
        self.fake.files[self.path] = "garbage"
        err = self.stderr(lambda: self.lock.hold("r"))
        self.assertIn("clearing a lock file with no holder in it: %s" % self.path, err)
        self.assertEqual(self.fake.files[self.path], self.lock.payload)

    def test_an_unreadable_holder_is_kept_not_cleared(self):
        """A lock that cannot be read is not evidence it is free: unlike a readable
        payload with no pid in it, deleting one could take a hold a transient read
        failure only hid, so it is kept and waited out like a live one."""
        self.fake.dirs.add(os.path.dirname(self.path))
        self.fake.files[self.path] = DEAD
        original = self.fake.readlink
        self.fake.readlink = lambda p: None if p == self.path else original(p)
        err = self.stderr(lambda: self.assertRaises(Refused, self.lock.hold, "r", timeout=2))
        self.assertIn("waiting for the r lock (its holder cannot be read)", err)
        self.assertIn("could not take the r lock within 2s -- its holder cannot be read", err)
        self.assertEqual(self.fake.files[self.path], DEAD)
        self.assertEqual(self.clock.slept, [1, 1])

    def test_a_directory_at_the_lock_path_is_a_holder_only_while_its_pid_lives(self):
        self.fake.dirs.add(self.path)
        self.fake.files[self.path + "/pid"] = "4242\n"
        self.fake.pids.add(4242)
        err = self.stderr(lambda: self.assertRaises(Refused, self.lock.hold, "r", timeout=1))
        self.assertIn("pid 4242 still holds it", err)
        self.assertIn(self.path, self.fake.dirs)
        self.fake.pids.discard(4242)
        self.lock.hold("r")
        self.assertNotIn(self.path, self.fake.dirs)
        self.assertEqual(self.fake.files[self.path], self.lock.payload)

    def test_a_directory_naming_no_pid_is_cleared(self):
        self.fake.dirs.add(self.path)
        self.lock.hold("r")
        self.assertEqual(self.fake.files[self.path], self.lock.payload)

    def test_holder_pid_of_a_directory_is_none(self):
        self.fake.dirs.add(self.path)
        self.assertIsNone(self.lock.holder_pid("r"))

    def test_a_breaker_left_by_a_dead_process_is_removed_and_a_live_one_is_waited_out(self):
        breaker = self.path + ".breaking"
        reads = []
        original = self.fake.readlink

        def readlink_breaker(path):
            if path == breaker:
                reads.append(1)
                if len(reads) == 2:
                    self.fake.pids.discard(77)
            return original(path)
        self.fake.readlink = readlink_breaker
        self.fake.dirs.add(os.path.dirname(self.path))
        self.fake.files[self.path] = DEAD
        self.fake.files[breaker] = "pid=77 tok=0000ffff at=2000-01-01T00:00:00Z cmd=wk"
        self.fake.pids.add(77)
        self.lock.hold("r")
        self.assertEqual(len(reads), 2)
        self.assertEqual(self.fake.files, {self.path: self.lock.payload})

    def test_the_break_is_a_compare_and_swap_that_retries_until_it_lands(self):
        breaker, path, other = self.path + ".breaking", self.path, DEAD.replace("4242", "4243")
        breaks, moves = [], []
        original_symlink, original_rename = self.fake.symlink, self.fake.rename

        def symlink(target, p):
            if p != breaker:
                return original_symlink(target, p)
            breaks.append(1)
            ok = original_symlink(target, p)
            if ok and len(breaks) == 1:
                self.fake.files[path] = other   # another taker races in between the breaker and the compare
            return ok

        def rename(src, dst):
            if dst != path:
                return original_rename(src, dst)
            moves.append(1)
            if len(moves) == 1:
                return False   # a transient "mv: busy"
            return original_rename(src, dst)
        self.fake.symlink, self.fake.rename = symlink, rename
        self.fake.dirs.add(os.path.dirname(path))
        self.fake.files[path] = DEAD
        self.lock.hold("r")
        self.assertEqual((len(breaks), len(moves)), (3, 2))
        self.assertEqual(self.fake.files, {path: self.lock.payload})
        self.assertEqual(self.clock.slept, [])

    def test_the_context_manager_releases_on_every_way_out(self):
        with self.lock.held("r"):
            self.assertEqual(self.fake.files[self.path], self.lock.payload)
        self.assertNotIn(self.path, self.fake.files)
        with self.assertRaises(ValueError):
            with self.lock.held("r"):
                raise ValueError
        self.assertNotIn(self.path, self.fake.files)
        self.assertEqual(self.lock.holding, [])

    def test_a_lock_is_taken_under_dry_run_too(self):
        os.environ["WK_DRY_RUN"] = "1"
        self.lock.hold("r")
        self.assertEqual(self.fake.files[self.path], self.lock.payload)
        self.lock.release_all()
        self.assertNotIn(self.path, self.fake.files)

    def test_release_leaves_a_lock_somebody_else_took_over(self):
        self.lock.hold("r")
        self.fake.files[self.path] = DEAD
        self.lock.release_all()
        self.assertEqual(self.fake.files[self.path], DEAD)


HOLDER = """
import os, sys, time
sys.path.insert(0, %r)
from wk.clock import Clock
from wk.lock import Lock
from wk.machine import Local
from wk.store import Store
lock = Lock(Store(os.environ), Local(), Clock())
lock.hold(sys.argv[1], int(sys.argv[2]))
mode = sys.argv[3]
if mode == "count":
    n = int(open(sys.argv[4]).read())
    time.sleep(0.2)
    open(sys.argv[4], "w").write(str(n + 1))
elif mode == "hold":
    print("held", flush=True)
    time.sleep(60)
elif mode == "exit":
    sys.exit(7)
""" % str(REPO / "lib")


class TestRealProcesses(LockTest):
    """Takers in separate processes on this host, every lock under a scratch WK_LOCK_DIR."""

    def spawn(self, resource, mode, *rest, timeout=20):
        env = dict(os.environ, WK_LOCK_DIR=self.store.lock_dir(), HOME=self.tmp)
        return subprocess.Popen([sys.executable, "-c", HOLDER, resource, str(timeout), mode] + list(rest),
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_a_lock_dies_with_its_holder(self):
        p = self.spawn("r", "hold")
        self.assertEqual(p.stdout.readline().strip(), "held")
        self.assertTrue(self.path.startswith(self.store.lock_dir() + os.sep))
        self.assertEqual(Lock(self.store, machine.Local(), FakeClock()).holder_pid("r"), p.pid)
        p.kill()
        p.communicate()
        clock = FakeClock()
        lock = Lock(self.store, machine.Local(), clock)
        lock.hold("r", timeout=0)
        self.assertEqual(os.readlink(self.path), lock.payload)
        self.assertEqual(clock.slept, [])
        lock.release_all()

    def test_a_live_holder_in_another_process_keeps_the_lock(self):
        p = self.spawn("r", "hold")
        self.addCleanup(p.communicate)
        self.addCleanup(p.kill)
        self.assertEqual(p.stdout.readline().strip(), "held")
        lock = Lock(self.store, machine.Local(), FakeClock())
        err = self.stderr(lambda: self.assertRaises(Refused, lock.hold, "r", timeout=0))
        self.assertIn("could not take the r lock within 0s -- pid %d still holds it" % p.pid, err)
        self.assertEqual(lock.holding, [])

    def test_takers_of_one_lock_one_at_a_time_past_a_dead_holder(self):
        os.makedirs(self.store.lock_dir())
        os.symlink(DEAD.replace("4242", "4194304"), self.path)
        counter = os.path.join(self.tmp, "n")
        with open(counter, "w") as f:
            f.write("0")
        takers = [self.spawn("r", "count", counter, timeout=60) for _ in range(4)]
        for t in takers:
            out, err = t.communicate(timeout=60)
            self.assertEqual(t.returncode, 0, err)
        with open(counter) as f:
            self.assertEqual(f.read(), "4")
        self.assertEqual(os.listdir(self.store.lock_dir()), [])

    def test_a_holder_that_exits_releases_the_lock_and_keeps_its_status(self):
        p = self.spawn("r", "exit")
        p.communicate(timeout=20)
        self.assertEqual(p.returncode, 7)
        self.assertFalse(os.path.lexists(self.path))


class TestReportingCommandsTakeNoLock(unittest.TestCase):
    """CLAUDE.md rule 6: a reporting command takes no lock."""

    def test_status_and_ls_take_no_lock(self):
        # --no-fleet: a bare `wk status` walks the fleet over ssh, which is not what this asks.
        with scratch_dir(prefix="wk-test-lock-") as d:
            lockdir = d / "locks"
            run("status", "--no-fleet", env={"WK_LOCK_DIR": str(lockdir)}, timeout=60)
            run("ls", env={"WK_LOCK_DIR": str(lockdir)}, timeout=60)
            left = list(lockdir.glob("*")) if lockdir.exists() else []
            self.assertEqual(left, [], "'wk status'/'wk ls' left a lock behind: %s" % left)


if __name__ == "__main__":
    unittest.main()
