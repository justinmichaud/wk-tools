"""lib/wk/machine.py: the seam every effect goes through. The fake answers
as told and records each effect; the local machine does the real thing; both
honour --dry-run by printing and changing nothing; and every transport
implements the whole interface.

Run: python3 tests/run.py -k tests.test_wk_machine
"""
import io
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import machine  # noqa: E402

INTERFACE = ("run", "read", "exists", "isdir", "listdir", "alive", "act_run",
             "write", "remove", "mkdir", "kill", "spawn")


class TestConformance(unittest.TestCase):
    def test_every_transport_implements_the_whole_interface(self):
        for cls in (machine.Local, machine.Ssh, machine.Fake):
            for name in INTERFACE:
                with self.subTest(cls=cls.__name__, method=name):
                    own = getattr(cls, name, None)
                    base = getattr(machine.Machine, name)
                    if name == "act_run":
                        continue   # the one shared implementation, through act
                    self.assertIsNot(own, base, "%s inherits %s unimplemented" % (cls.__name__, name))


class MachineTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for f in ("WK_DRY_RUN", "WK_DESTRUCTIVE", "WK_CONFIRMED"):
            os.environ.pop(f, None)

    def tearDown(self):
        self.env.stop()

    def stderr(self, fn):
        buf = io.StringIO()
        with redirect_stderr(buf):
            result = fn()
        return result, buf.getvalue()


class TestLocal(MachineTest):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="wk-test-machine-")
        self.m = machine.Local()

    def tearDown(self):
        machine.Local().remove(self.tmp)
        super().tearDown()

    def test_run_captures_status_and_both_streams(self):
        r = self.m.run(["sh", "-c", "echo out; echo err >&2; exit 3"])
        self.assertEqual((r.rc, r.out, r.err, r.ok), (3, "out\n", "err\n", False))

    def test_a_missing_program_is_127_not_an_exception(self):
        self.assertEqual(self.m.run(["no-such-program-zz"]).rc, 127)

    def test_a_timeout_is_its_own_status(self):
        r = self.m.run(["sleep", "5"], timeout=0.2)
        self.assertEqual(r.rc, machine.TIMED_OUT)

    def test_files_round_trip_and_write_is_atomic(self):
        p = os.path.join(self.tmp, "f")
        self.m.write(p, "hello")
        self.assertEqual(self.m.read(p), "hello")
        self.assertTrue(self.m.exists(p) and not self.m.isdir(p))
        self.assertEqual(self.m.listdir(self.tmp), ["f"])
        self.assertFalse(any(n.startswith("f.tmp") for n in os.listdir(self.tmp)))
        self.m.remove(p)
        self.assertFalse(self.m.exists(p))

    def test_remove_takes_a_tree(self):
        d = os.path.join(self.tmp, "d")
        self.m.mkdir(os.path.join(d, "sub"))
        self.m.write(os.path.join(d, "sub", "f"), "x")
        self.m.remove(d)
        self.assertFalse(os.path.exists(d))

    def test_spawn_detaches_and_alive_follows_the_pid(self):
        log = os.path.join(self.tmp, "log")
        pid = self.m.spawn(["sh", "-c", "echo started; sleep 30"], log)
        try:
            self.assertTrue(self.m.alive(pid))
            for _ in range(50):
                if "started" in self.m.read(log):
                    break
                import time
                time.sleep(0.05)
            self.assertIn("started", self.m.read(log))
            self.assertTrue(self.m.kill(pid, signal.SIGKILL))
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.assertFalse(self.m.kill(999999))

    def test_dry_run_prints_every_effect_and_changes_nothing(self):
        os.environ["WK_DRY_RUN"] = "1"
        p = os.path.join(self.tmp, "f")
        _, err = self.stderr(lambda: (self.m.write(p, "x"), self.m.mkdir(os.path.join(self.tmp, "d")),
                                      self.m.remove(self.tmp), self.m.kill(os.getpid()),
                                      self.m.spawn(["true"], p), self.m.act_run(["touch", p])))
        self.assertFalse(os.path.exists(p))
        self.assertTrue(os.path.isdir(self.tmp))
        for word in ("would write", "would create", "would remove", "would signal", "would start", "would run"):
            self.assertIn(word, err)

    def test_act_run_refuses_a_destructive_command_that_did_not_ask(self):
        os.environ["WK_DESTRUCTIVE"] = "1"
        from wk.act import Refused
        with self.assertRaises(Refused):
            self.stderr(lambda: self.m.act_run(["true"]))
        os.environ["WK_CONFIRMED"] = "1"
        r, _ = self.stderr(lambda: self.m.act_run(["true"]))
        self.assertTrue(r.ok)


class TestFake(MachineTest):
    def setUp(self):
        super().setUp()
        self.m = machine.Fake("box")

    def test_the_longest_registered_prefix_answers(self):
        self.m.answer(["podman"], rc=1, err="generic")
        self.m.answer(["podman", "ps"], rc=0, out="wk-a\n")
        self.assertEqual(self.m.run(["podman", "ps", "-a"]).out, "wk-a\n")
        self.assertEqual(self.m.run(["podman", "rm"]).rc, 1)
        self.assertEqual(self.m.run(["tart", "list"]).rc, 127)
        self.assertEqual(self.m.effects[0], ("run", ("podman", "ps", "-a")))

    def test_files_directories_and_processes_are_in_memory(self):
        self.m.write("/var/lib/wk/ws/a/base-id", "b1")
        self.assertTrue(self.m.exists("/var/lib/wk/ws/a/base-id"))
        self.assertTrue(self.m.isdir("/var/lib/wk/ws"))
        self.assertEqual(self.m.listdir("/var/lib/wk/ws"), ["a"])
        pid = self.m.spawn(["build"], "/log")
        self.assertTrue(self.m.alive(pid))
        self.assertTrue(self.m.kill(pid))
        self.assertFalse(self.m.alive(pid))
        self.m.remove("/var/lib/wk/ws/a")
        self.assertFalse(self.m.exists("/var/lib/wk/ws/a/base-id"))
        self.assertEqual([e[0] for e in self.m.effects], ["write", "spawn", "kill", "remove"])

    def test_dry_run_records_the_effect_and_applies_nothing(self):
        os.environ["WK_DRY_RUN"] = "1"
        self.m.write("/f", "x")
        self.m.mkdir("/d")
        self.assertFalse(self.m.exists("/f") or self.m.exists("/d"))
        self.assertEqual([e[0] for e in self.m.effects], ["write", "mkdir"])
        _, err = self.stderr(lambda: self.m.act_run(["rm", "-rf", "/d"]))
        self.assertIn("would run on box: rm -rf /d", err)
        self.assertEqual(self.m.effects[-1][0], "mkdir")   # a dry act_run runs nothing on the fake


class TestSsh(MachineTest):
    def test_every_call_is_one_bounded_non_interactive_ssh(self):
        m = machine.Ssh("box.example", timeout=3)
        seen = []

        def fake_run(self_, argv, input=None, timeout=None):
            seen.append(argv)
            return machine.Result(0, "1234\n")
        with mock.patch.object(machine.Local, "run", fake_run):
            m.run(["ls", "-1", "/tmp/a b"])
            self.assertTrue(m.exists("/x"))
            self.assertTrue(m.alive(42))
            self.assertEqual(m.spawn(["sleep", "9"], "/tmp/log"), 1234)
        for argv in seen:
            self.assertEqual(argv[0], "ssh")
            self.assertIn("BatchMode=yes", argv)
            self.assertIn("ConnectTimeout=3", argv)
            self.assertEqual(argv[-2], "box.example")
        self.assertEqual(seen[0][-1], "ls -1 '/tmp/a b'")
        self.assertIn("kill -0 42", seen[2][-1])
        self.assertIn("nohup sleep 9 > /tmp/log", seen[3][-1])


if __name__ == "__main__":
    unittest.main()
