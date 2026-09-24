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
from tests.killpoints import TestConverges  # noqa: E402,F401  discovery reads test_*.py only

INTERFACE = ("run", "run_tty", "read", "exists", "isdir", "listdir", "alive", "act_run",
             "write", "remove", "mkdir", "kill", "spawn",
             "readlink", "symlink", "rename", "remove_now", "mkdir_now",
             "copy_in", "copy_out", "copy_tree_in", "copy_tree_out")


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


class LockEffectsConformance:
    """symlink/readlink/rename/remove_now/mkdir_now, over whatever `self.m`
    and `self.path` a subclass gives: the primitives `Lock` takes its
    exclusion from, real under --dry-run since a lock is not workspace mutation."""

    def test_symlink_is_atomic_create_or_fail_and_readlink_reads_it_back(self):
        p = self.path("a")
        self.assertTrue(self.m.symlink("target", p))
        self.assertFalse(self.m.symlink("other", p))
        self.assertEqual(self.m.readlink(p), "target")

    def test_readlink_of_an_absent_path_is_none(self):
        self.assertIsNone(self.m.readlink(self.path("nope")))

    def test_symlink_fails_without_its_parent_directory(self):
        self.assertFalse(self.m.symlink("target", self.path("nodir", "a")))

    def test_rename_replaces_the_destination_atomically_and_fails_without_a_source(self):
        a, b = self.path("a"), self.path("b")
        self.assertFalse(self.m.rename(a, b))
        self.m.symlink("one", a)
        self.m.symlink("two", b)
        self.assertTrue(self.m.rename(a, b))
        self.assertEqual(self.m.readlink(b), "one")
        self.assertIsNone(self.m.readlink(a))

    def test_remove_now_and_mkdir_now_act_under_dry_run_too(self):
        os.environ["WK_DRY_RUN"] = "1"
        d = self.path("d")
        self.m.mkdir_now(d)
        p = os.path.join(d, "l")
        self.assertTrue(self.m.symlink("x", p))
        self.m.remove_now(p)
        self.assertIsNone(self.m.readlink(p))


class CopyConformance:
    """copy_in/copy_out/copy_tree_in/copy_tree_out, over whatever `self.m`,
    `self.path` (a path as this machine sees it) and `self.real_tmp` (a real
    directory on the driving host) a subclass gives: the one `Machine` copy
    a workspace, a board and a card all move bytes through."""

    def test_a_file_round_trips_byte_for_byte(self):
        blob = os.urandom(4096)
        real_src = os.path.join(self.real_tmp, "in.dat")
        with open(real_src, "wb") as f:
            f.write(blob)
        dest = self.path("out.dat")
        self.m.copy_in(real_src, dest)
        real_dest = os.path.join(self.real_tmp, "out.dat")
        self.m.copy_out(dest, real_dest)
        with open(real_dest, "rb") as f:
            self.assertEqual(f.read(), blob)

    def test_a_tree_replaces_rather_than_merges(self):
        src = Path(self.real_tmp) / "tree"
        (src / "sub").mkdir(parents=True)
        (src / "a").write_bytes(b"a\n")
        (src / "sub" / "c").write_bytes(b"c\n")
        dest = self.path("tree")
        self.m.copy_tree_in(str(src), dest)
        out = Path(self.real_tmp) / "out"
        self.m.copy_tree_out(dest, str(out))
        self.assertEqual((out / "a").read_bytes(), b"a\n")
        self.assertEqual((out / "sub" / "c").read_bytes(), b"c\n")

    def test_a_tree_copied_out_leaves_what_it_excludes_at_any_depth(self):
        src = Path(self.real_tmp) / "excl"
        (src / "Release" / "DerivedSources").mkdir(parents=True)
        (src / "Release" / "keep").write_bytes(b"k\n")
        (src / "Release" / "libWTF.a").write_bytes(b"a\n")
        (src / "Release" / "DerivedSources" / "x.h").write_bytes(b"x\n")
        dest = self.path("excl")
        self.m.copy_tree_in(str(src), dest)
        out = Path(self.real_tmp) / "excl-out"
        self.m.copy_tree_out(dest, str(out), exclude=("DerivedSources", "*.a"))
        self.assertEqual(sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()), ["Release/keep"])

    def test_dry_run_copies_nothing(self):
        os.environ["WK_DRY_RUN"] = "1"
        real_src = os.path.join(self.real_tmp, "x.dat")
        with open(real_src, "wb") as f:
            f.write(b"x")
        dest = self.path("x.dat")
        self.m.copy_in(real_src, dest)
        self.assertFalse(self.m.exists(dest))


class TestLocal(MachineTest, LockEffectsConformance, CopyConformance):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="wk-test-machine-")
        self.real_tmp = tempfile.mkdtemp(prefix="wk-test-machine-real-")
        self.m = machine.Local()

    def tearDown(self):
        super().tearDown()
        machine.Local().remove(self.tmp)
        machine.Local().remove(self.real_tmp)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def test_run_captures_status_and_both_streams(self):
        r = self.m.run(["sh", "-c", "echo out; echo err >&2; exit 3"])
        self.assertEqual((r.rc, r.out, r.err, r.ok), (3, "out\n", "err\n", False))

    def test_a_missing_program_is_127_not_an_exception(self):
        self.assertEqual(self.m.run(["no-such-program-zz"]).rc, 127)

    def test_a_timeout_is_its_own_status(self):
        r = self.m.run(["sleep", "5"], timeout=0.2)
        self.assertEqual(r.rc, machine.TIMED_OUT)

    def test_run_tty_inherits_stdio_and_returns_only_a_status(self):
        r = self.m.run_tty([sys.executable, "-c", "import sys; sys.exit(5)"])
        self.assertEqual((r.rc, r.out, r.err), (5, "", ""))

    def test_run_tty_honours_cwd(self):
        want = os.path.realpath(self.tmp)
        r = self.m.run_tty([sys.executable, "-c",
                            "import os, sys; sys.exit(0 if os.path.realpath(os.getcwd()) == %r else 1)" % want],
                           cwd=self.tmp)
        self.assertEqual(r.rc, 0)

    def test_run_tty_a_missing_program_is_127(self):
        self.assertEqual(self.m.run_tty(["no-such-program-zz"]).rc, 127)

    def test_run_tty_a_timeout_is_its_own_status(self):
        r = self.m.run_tty(["sleep", "5"], timeout=0.2)
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


class TestFake(MachineTest, LockEffectsConformance, CopyConformance):
    def setUp(self):
        super().setUp()
        self.m = machine.Fake("box")
        self.real_tmp = tempfile.mkdtemp(prefix="wk-test-machine-fake-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.real_tmp]))

    def path(self, *parts):
        return "/" + "/".join(parts)

    def test_the_longest_registered_prefix_answers(self):
        self.m.answer(["podman"], rc=1, err="generic")
        self.m.answer(["podman", "ps"], rc=0, out="wk-a\n")
        self.assertEqual(self.m.run(["podman", "ps", "-a"]).out, "wk-a\n")
        self.assertEqual(self.m.run(["podman", "rm"]).rc, 1)
        self.assertEqual(self.m.run(["tart", "list"]).rc, 127)
        self.assertEqual(self.m.effects[0], ("run", ("podman", "ps", "-a")))

    def test_run_tty_shares_the_answers_run_uses_and_records_its_own_effect(self):
        self.m.answer(["lldb"], rc=0, out="ignored -- run_tty captures nothing real")
        r = self.m.run_tty(["lldb", "-o", "attach"], cwd="/src")
        self.assertEqual(r.rc, 0)
        self.assertEqual(self.m.effects[-1], ("run_tty", ("lldb", "-o", "attach"), "/src"))
        self.assertEqual(self.m.run_tty(["no-such-thing"]).rc, 127)

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

    def test_copy_in_is_one_effect_not_two(self):
        """copy_in used to also call `write`, so one copy was two kill points a --dry-run
        rerun could land between; it sets `self.files` directly instead."""
        src = os.path.join(self.real_tmp, "in.dat")
        with open(src, "wb") as f:
            f.write(b"payload")
        self.m.copy_in(src, self.path("out.dat"))
        self.assertEqual([e[0] for e in self.m.effects], ["copy_in"])
        self.assertEqual(self.m.files[self.path("out.dat")], b"payload")

    def test_dry_run_records_the_effect_and_applies_nothing(self):
        os.environ["WK_DRY_RUN"] = "1"
        self.m.write("/f", "x")
        self.m.mkdir("/d")
        self.assertFalse(self.m.exists("/f") or self.m.exists("/d"))
        self.assertEqual([e[0] for e in self.m.effects], ["write", "mkdir"])
        _, err = self.stderr(lambda: self.m.act_run(["rm", "-rf", "/d"]))
        self.assertIn("would run on box: rm -rf /d", err)
        self.assertEqual(self.m.effects[-1][0], "mkdir")   # a dry act_run runs nothing on the fake

    def test_a_reaction_answers_from_the_state_its_effects_moved(self):
        def create(argv, fake):
            fake.files["/ws/%s/.wk-ready" % argv[-1]] = ""
            return machine.Result(0, "created %s\n" % argv[-1])
        self.m.react(["wkdev-create"], create)
        self.m.react(["podman", "container", "exists"],
                     lambda argv, fake: machine.Result(0 if fake.exists("/ws/%s/.wk-ready" % argv[-1]) else 1))
        self.assertEqual(self.m.run(["podman", "container", "exists", "a"]).rc, 1)
        self.assertEqual(self.m.act_run(["wkdev-create", "a"]).out, "created a\n")
        self.assertEqual(self.m.run(["podman", "container", "exists", "a"]).rc, 0)
        self.m.answer(["podman", "container", "exists"], rc=125)
        self.assertEqual(self.m.run(["podman", "container", "exists", "a"]).rc, 125)

    def test_a_kill_lands_on_the_effect_after_stop_after_and_never_on_a_read(self):
        self.m.answer(["true"])
        self.m.stop_after = 2
        self.m.write("/a", "1")
        self.m.act_run(["true"])
        self.assertEqual(self.m.run(["true"]).rc, 0)
        self.assertTrue(self.m.exists("/a") and self.m.read("/a") == "1")
        for effect in (lambda: self.m.write("/b", "2"), lambda: self.m.remove("/a"), lambda: self.m.mkdir("/d"),
                       lambda: self.m.kill(7), lambda: self.m.spawn(["x"], "/log"), lambda: self.m.act_run(["true"])):
            with self.assertRaises(machine.Killed):
                effect()
        self.assertEqual(self.m.files, {"/a": "1"})
        self.assertEqual(self.m.applied, 2)
        self.assertEqual([e[0] for e in self.m.effects], ["write", "run", "run"])
        self.m.stop_after = None
        self.m.write("/b", "2")
        self.assertEqual(self.m.applied, 3)

    def test_a_dry_act_run_is_not_an_effect_a_kill_can_land_on(self):
        os.environ["WK_DRY_RUN"] = "1"
        self.m.stop_after = 0
        _, err = self.stderr(lambda: self.m.act_run(["rm", "-rf", "/d"]))
        self.assertIn("would run on box", err)
        self.assertEqual(self.m.applied, 0)


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

    def test_run_tty_allocates_a_pty_and_honours_cwd(self):
        m = machine.Ssh("box.example", timeout=3)
        seen = []

        def fake_run_tty(self_, argv, cwd=None, timeout=None):
            seen.append(argv)
            return machine.Result(0)
        with mock.patch.object(machine.Local, "run_tty", fake_run_tty):
            m.run_tty(["lldb", "-o", "attach"], cwd="/src/WebKit")
        self.assertEqual(seen[0][0], "ssh")
        self.assertIn("-t", seen[0])
        self.assertEqual(seen[0][-2], "box.example")
        self.assertEqual(seen[0][-1], "cd /src/WebKit && lldb -o attach")

    def test_the_lock_effects_are_one_command_each_and_readlink_answers(self):
        m = machine.Ssh("box.example", timeout=3)
        seen = []

        def fake_run(self_, argv, input=None, timeout=None):
            seen.append(argv[-1])
            if argv[-1].startswith("readlink"):
                return machine.Result(0, "target\n")
            return machine.Result(0)
        with mock.patch.object(machine.Local, "run", fake_run):
            self.assertTrue(m.symlink("target", "/locks/r.lock"))
            self.assertEqual(m.readlink("/locks/r.lock"), "target")
            self.assertTrue(m.rename("/locks/r.lock.new", "/locks/r.lock"))
            m.remove_now("/locks/r.lock")
            m.mkdir_now("/locks")
        self.assertEqual(seen, ["ln -s target /locks/r.lock", "readlink /locks/r.lock",
                                 "mv -f /locks/r.lock.new /locks/r.lock", "rm -rf /locks/r.lock", "mkdir -p /locks"])

    def test_copy_in_and_out_are_scp_naming_the_destination(self):
        m = machine.Ssh("box.example", timeout=3)
        seen = []

        def fake_run(self_, argv, input=None, timeout=None):
            seen.append(argv)
            return machine.Result(0)
        with mock.patch.object(machine.Local, "run", fake_run):
            m.copy_in("/local/a", "/remote/a")
            m.copy_out("/remote/b", "/local/b")
        self.assertEqual(seen[0][0], "scp")
        self.assertEqual(seen[0][-2:], ["/local/a", "box.example:/remote/a"])
        self.assertEqual(seen[1][-2:], ["box.example:/remote/b", "/local/b"])

    def test_copy_tree_in_and_out_are_rsync_over_this_sshs_own_opts(self):
        m = machine.Ssh("box.example", opts=["-i", "key"], timeout=3)
        seen = []

        def fake_run(self_, argv, input=None, timeout=None):
            seen.append(argv)
            return machine.Result(0)
        with mock.patch.object(machine.Local, "run", fake_run):
            m.copy_tree_in("/local/tree", "/remote/tree")
            m.copy_tree_out("/remote/tree", "/local/tree")
        for argv in seen:
            self.assertEqual(argv[0], "rsync")
            self.assertIn("--chmod=go-w", argv)
            self.assertIn("--delete", argv)
            self.assertIn("-i key", argv[argv.index("-e") + 1])
        self.assertEqual(seen[0][-2:], ["/local/tree/", "box.example:/remote/tree/"])
        self.assertEqual(seen[1][-2:], ["box.example:/remote/tree/", "/local/tree/"])

    def test_copy_tree_out_hands_rsync_each_exclusion(self):
        m = machine.Ssh("box.example", timeout=3)
        seen = []
        with mock.patch.object(machine.Local, "run", lambda self_, argv, input=None, timeout=None: seen.append(argv) or machine.Result(0)):
            m.copy_tree_out("/remote/tree", "/local/tree", exclude=("*.a", "DerivedSources"))
        self.assertIn(["--exclude", "*.a", "--exclude", "DerivedSources"], [seen[0][i:i + 4] for i in range(len(seen[0]))])

    def test_a_copy_that_fails_raises(self):
        m = machine.Ssh("box.example", timeout=3)
        with mock.patch.object(machine.Local, "run", lambda self_, argv, input=None, timeout=None: machine.Result(1, "", "no such file")):
            with self.assertRaises(OSError):
                m.copy_out("/remote/a", "/local/a")

    def test_dry_run_copies_nothing_over_ssh(self):
        os.environ["WK_DRY_RUN"] = "1"
        m = machine.Ssh("box.example", timeout=3)
        with mock.patch.object(machine.Local, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ssh ran under --dry-run"))):
            m.copy_in("/local/a", "/remote/a")
            m.copy_tree_out("/remote/tree", "/local/tree")


if __name__ == "__main__":
    unittest.main()
