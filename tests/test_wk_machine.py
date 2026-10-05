"""lib/wk/machine.py: the seam every effect goes through, over the local, fake, ssh and tart transports."""
import io
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from tests.support import REPO, requires_tool

sys.path.insert(0, str(REPO / "lib"))
from wk import machine  # noqa: E402
from tests.killpoints import TestConverges  # noqa: E402,F401  discovery reads test_*.py only

INTERFACE = ("run", "run_tty", "read", "exists", "isdir", "listdir", "alive", "act_run",
             "write", "remove", "mkdir", "kill", "spawn",
             "readlink", "mtime", "read_bytes", "symlink", "rename", "remove_now", "mkdir_now",
             "copy_in", "copy_out", "copy_tree_in", "copy_tree_out")


class TestConformance(unittest.TestCase):
    def test_every_transport_implements_the_whole_interface(self):
        for cls in (machine.Local, machine.Ssh, machine.Fake):
            missing = [n for n in INTERFACE if not callable(getattr(cls, n, None))]
            self.assertEqual(missing, [], cls.__name__)


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

    def ssh_calls(self, calls, m=None, method="run", answer=lambda argv: machine.Result(0)):
        """What `calls(m)` hands this host's `method`, over `m` (an Ssh to box.example)."""
        m = m or machine.Ssh("box.example", timeout=3)
        seen = []

        def fake(self_, argv, **kw):
            seen.append(argv)
            return answer(argv)
        with mock.patch.object(machine.Local, method, fake):
            calls(m)
        return seen


class LockEffectsConformance:
    """The primitives `Lock` takes its exclusion from, real under --dry-run, over a subclass's `self.m`/`self.path`."""

    def test_symlink_is_atomic_create_or_fail_without_its_parent_too_and_readlink_reads_it_back(self):
        p = self.path("a")
        self.assertTrue(self.m.symlink("target", p))
        self.assertFalse(self.m.symlink("other", p))
        self.assertEqual(self.m.readlink(p), "target")
        self.assertIsNone(self.m.readlink(self.path("nope")))
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
    """The copies, over a subclass's `self.m`, `self.path` (on that machine) and `self.real_tmp` (on this host)."""

    def test_a_file_round_trips_byte_for_byte(self):
        blob = os.urandom(4096)
        (Path(self.real_tmp) / "in.dat").write_bytes(blob)
        self.m.copy_in(os.path.join(self.real_tmp, "in.dat"), self.path("out.dat"))
        self.m.copy_out(self.path("out.dat"), os.path.join(self.real_tmp, "out.dat"))
        self.assertEqual((Path(self.real_tmp) / "out.dat").read_bytes(), blob)

    @requires_tool("rsync")
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

    @requires_tool("rsync")
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
        (Path(self.real_tmp) / "x.dat").write_bytes(b"x")
        self.m.copy_in(os.path.join(self.real_tmp, "x.dat"), self.path("x.dat"))
        self.assertFalse(self.m.exists(self.path("x.dat")))


class LogReadConformance:
    """mtime/read_bytes, over a subclass's `self.m`, `self.path` and `self.put(path, data, mtime)`."""

    def test_read_bytes_is_the_slice_from_start(self):
        p = self.path("log")
        self.put(p, b"0123456789", 1000)
        self.assertEqual(self.m.read_bytes(p), b"0123456789")
        self.assertEqual(self.m.read_bytes(p, 4), b"456789")
        self.assertEqual(self.m.read_bytes(p, -3), b"789")
        self.assertEqual(self.m.read_bytes(p, -64), b"0123456789")
        self.assertEqual(self.m.read_bytes(p, 10), b"")

    def test_read_bytes_carries_bytes_a_cut_splits(self):
        p = self.path("log")
        self.put(p, "a\u00e9b".encode(), 1000)
        self.assertEqual(self.m.read_bytes(p, 2), b"\xa9b")

    def test_mtime_is_the_files_and_an_absent_file_raises(self):
        p = self.path("log")
        self.put(p, b"x", 1234567890)
        self.assertEqual(int(self.m.mtime(p)), 1234567890)
        with self.assertRaises(OSError):
            self.m.mtime(self.path("nope"))
        with self.assertRaises(OSError):
            self.m.read_bytes(self.path("nope"), -10)


class TestLocal(MachineTest, LockEffectsConformance, CopyConformance, LogReadConformance):
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

    def put(self, path, data, mtime):
        Path(path).write_bytes(data)
        os.utime(path, (mtime, mtime))

    def test_a_started_child_is_watched_to_its_status(self):
        with open(self.path("log"), "wb") as out:
            p = self.m.start(["sh", "-c", "echo hi; exit 3"], out)
        self.assertEqual((p.wait(), Path(self.path("log")).read_text()), (3, "hi\n"))

    def test_exec_replaces_this_process_with_the_command(self):
        with mock.patch("os.execvp") as ex:
            self.m.exec(["true", "x"])
        ex.assert_called_once_with("true", ["true", "x"])

    def test_run_captures_status_and_both_streams(self):
        r = self.m.run(["sh", "-c", "echo out; echo err >&2; exit 3"])
        self.assertEqual((r.rc, r.out, r.err, r.ok), (3, "out\n", "err\n", False))

    def test_a_missing_program_is_127_and_a_timeout_its_own_status_under_both_runs(self):
        for run in (self.m.run, self.m.run_tty):
            with self.subTest(run=run.__name__):
                self.assertEqual(run(["no-such-program-zz"]).rc, 127)
                self.assertEqual(run(["sleep", "5"], timeout=0.2).rc, machine.TIMED_OUT)

    def test_run_tty_inherits_stdio_honours_cwd_and_returns_only_a_status(self):
        r = self.m.run_tty([sys.executable, "-c", "import os, sys; sys.exit(5 if os.path.realpath(os.getcwd()) == %r else 1)"
                            % os.path.realpath(self.tmp)], cwd=self.tmp)
        self.assertEqual((r.rc, r.out, r.err), (5, "", ""))

    def test_files_round_trip_write_is_atomic_and_remove_takes_a_tree(self):
        p = os.path.join(self.tmp, "f")
        self.m.write(p, "hello")
        self.assertEqual(self.m.read(p), "hello")
        self.assertTrue(self.m.exists(p) and not self.m.isdir(p))
        self.assertEqual(self.m.listdir(self.tmp), ["f"])
        self.assertFalse(any(n.startswith("f.tmp") for n in os.listdir(self.tmp)))
        self.m.remove(p)
        self.assertFalse(self.m.exists(p))
        d = os.path.join(self.tmp, "d")
        self.m.mkdir(os.path.join(d, "sub"))
        self.m.write(os.path.join(d, "sub", "f"), "x")
        self.m.remove(d)
        self.assertFalse(os.path.exists(d))

    def test_spawn_detaches_and_alive_follows_the_pid(self):
        log = os.path.join(self.tmp, "log")
        pid = self.m.spawn(["sh", "-c", "echo started; sleep 30"], log)
        self.addCleanup(lambda: self.m.alive(pid) and os.kill(pid, signal.SIGKILL))
        self.assertTrue(self.m.alive(pid))
        for _ in range(50):
            if "started" in self.m.read(log):
                break
            time.sleep(0.05)
        self.assertIn("started", self.m.read(log))
        self.assertTrue(self.m.kill(pid, signal.SIGKILL))
        self.assertFalse(self.m.kill(999999))

    def test_a_spawned_driver_that_exited_is_not_alive(self):
        pid = self.m.spawn(["true"], os.path.join(self.tmp, "log"))
        deadline = time.monotonic() + 0.4
        while self.m.alive(pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.m.alive(pid))

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

    def test_a_streamed_effect_reaches_stderr_while_it_runs(self):
        log, flag, done = self.path("log"), self.path("flag"), threading.Event()
        script = ('echo out; echo err >&2; i=0; while [ ! -e "$1" ] && [ $i -lt 400 ]; do sleep 0.05; i=$((i+1)); done; test -e "$1"')

        def watch():
            while not done.is_set():
                with open(log) as f:
                    seen = f.read()
                if "out\n" in seen and "err\n" in seen:
                    open(flag, "w").close()
                    return
                done.wait(0.02)
        saved, fd = os.dup(2), os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        watcher = threading.Thread(target=watch)
        try:
            os.dup2(fd, 2)
            watcher.start()
            r = self.m.act_run(["sh", "-c", script, "sh", flag], stream=True)
        finally:
            os.dup2(saved, 2)
            os.close(saved)
            os.close(fd)
            done.set()
            watcher.join()
        self.assertEqual((r.rc, r.out, r.err), (0, "", ""), "the output was held until the command ended")


class TestFake(MachineTest, LockEffectsConformance, CopyConformance, LogReadConformance):
    def setUp(self):
        super().setUp()
        self.m = machine.Fake("box")
        self.real_tmp = tempfile.mkdtemp(prefix="wk-test-machine-fake-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.real_tmp]))

    def path(self, *parts):
        return "/" + "/".join(parts)

    def put(self, path, data, mtime):
        self.m.files[path] = data
        self.m.mtimes[path] = mtime

    def test_a_started_child_writes_its_answer_and_has_finished(self):
        self.m.answer(["make"], rc=2, out="built\n")
        out = io.BytesIO()
        p = self.m.start(["make", "all"], out)
        self.assertEqual((p.poll(), p.wait(), out.getvalue()), (2, 2, b"built\n"))
        self.assertEqual([("start", ("make", "all"))], self.m.effects)

    def test_an_exec_is_an_effect_and_a_dry_run_prints_it_and_ends(self):
        self.m.exec(["gh", "pr"], "/src")
        self.assertEqual([("exec", ("gh", "pr"), "/src")], self.m.effects)
        os.environ["WK_DRY_RUN"] = "1"
        buf = io.StringIO()
        with redirect_stderr(buf), self.assertRaises(SystemExit):
            self.m.exec(["gh", "pr"], "/src")
        self.assertEqual(("would run: cd /src && gh pr\n", 1), (buf.getvalue(), len(self.m.effects)))

    def test_the_longest_registered_prefix_answers_run_and_run_tty_which_records_its_own_effect(self):
        self.m.answer(["podman"], rc=1, err="generic")
        self.m.answer(["podman", "ps"], rc=0, out="wk-a\n")
        self.assertEqual(self.m.run(["podman", "ps", "-a"]).out, "wk-a\n")
        self.assertEqual(self.m.run(["podman", "rm"]).rc, 1)
        self.assertEqual(self.m.run(["tart", "list"]).rc, 127)
        self.assertEqual(self.m.effects[0], ("run", ("podman", "ps", "-a")))
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
        (Path(self.real_tmp) / "in.dat").write_bytes(b"payload")
        self.m.copy_in(os.path.join(self.real_tmp, "in.dat"), self.path("out.dat"))
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

    def test_a_streamed_run_prints_its_answer_and_returns_only_the_status(self):
        self.m.answer(["wkdev-create"], rc=2, out="pulling\n", err="failed\n")
        r, err = self.stderr(lambda: self.m.act_run(["wkdev-create", "--name", "x"], stream=True))
        self.assertEqual((r.rc, r.out, r.err, err), (2, "", "", "pulling\nfailed\n"))
        self.assertEqual(self.m.streamed, [("wkdev-create", "--name", "x")])
        self.assertEqual(self.m.effects, [("run", ("wkdev-create", "--name", "x"))])

    def test_a_dry_act_run_is_not_an_effect_a_kill_can_land_on(self):
        os.environ["WK_DRY_RUN"] = "1"
        self.m.stop_after = 0
        self.assertIn("would run on box", self.stderr(lambda: self.m.act_run(["rm", "-rf", "/d"]))[1])
        self.assertEqual(self.m.applied, 0)


class TestTartExec(MachineTest, CopyConformance):
    """tar or cat on both ends of `tart exec -i`, against a tart that runs the guest's half on this host."""

    def setUp(self):
        super().setUp()
        self.real_tmp = tempfile.mkdtemp(prefix="wk-test-tart-copy-")
        self.addCleanup(subprocess.run, ["rm", "-rf", self.real_tmp])
        tart = Path(self.real_tmp) / "tart"
        tart.write_text('#!/bin/sh\n[ "$1" = exec ] || exit 2; shift\nwhile [ "${1#-}" != "$1" ]; do shift; done\nshift\nexec "$@"\n')
        tart.chmod(0o755)
        os.makedirs(os.path.join(self.real_tmp, "guest"))
        self.m = machine.TartExec(str(tart), "wk-demo", via=machine.Local())

    def path(self, *parts):
        return os.path.join(self.real_tmp, "guest", *parts)

    def test_a_failed_copy_raises_and_a_port_forward_is_refused(self):
        with self.assertRaises(OSError):
            self.m.copy_out(self.path("absent"), os.path.join(self.real_tmp, "out"))
        with self.assertRaises(NotImplementedError):
            self.m.forward(1234)


class TestHave(MachineTest):
    def test_here_a_tool_is_one_on_path_over_ssh_one_quoted_command_v_and_a_fake_answers_it_by_the_probe(self):
        self.assertEqual((machine.Local().have("sh"), machine.Local().have("wk-no-such-tool")), (True, False))
        via = machine.Fake("here")
        m = machine.Ssh("box.example", via=via)
        via.answer(m.argv(shlex.join(machine.HAVE + ("x y",))))
        self.assertTrue(m.have("x y"))
        self.assertFalse(m.have("z"))
        fake = machine.Fake()
        fake.answer(machine.HAVE + ("xz",))
        self.assertEqual((fake.have("xz"), fake.have("kpartx")), (True, False))


class TestReplaceFile(unittest.TestCase):
    def test_the_mode_is_the_umasks_unless_named(self):
        d = tempfile.mkdtemp(prefix="wk-test-replace-")
        self.addCleanup(subprocess.run, ["rm", "-rf", d])
        p = os.path.join(d, "f")
        old = os.umask(0o022)
        try:
            machine.replace_file(p, "a")
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o644)
            machine.replace_file(p, b"b", mode=0o600)
        finally:
            os.umask(old)
        self.assertEqual((os.stat(p).st_mode & 0o777, Path(p).read_text()), (0o600, "b"))


class TestSshLogReads(MachineTest, LogReadConformance):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="wk-test-machine-ssh-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.tmp]))
        via = machine.Fake("here")
        via.react(["ssh"], lambda argv, fake: machine.Local().run(["sh", "-c", argv[-1]]))
        self.m = machine.Ssh("box.example", via=via)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    put = TestLocal.put


class TestSsh(MachineTest):
    def test_every_call_is_one_bounded_non_interactive_ssh(self):
        def calls(m):
            m.run(["ls", "-1", "/tmp/a b"])
            self.assertTrue(m.exists("/x"))
            self.assertTrue(m.alive(42))
            self.assertEqual(m.spawn(["sleep", "9"], "/tmp/log"), 1234)
        seen = self.ssh_calls(calls, answer=lambda argv: machine.Result(0, "1234\n"))
        for argv in seen:
            self.assertEqual((argv[0], argv[-2]), ("ssh", "box.example"))
            self.assertIn("BatchMode=yes", argv)
            self.assertIn("ConnectTimeout=3", argv)
        self.assertEqual(shlex.split(seen[0][-1]), ["$SHELL", "-lc", "ls -1 '/tmp/a b'"])
        self.assertIn("kill -0 42", shlex.split(seen[2][-1])[-1])
        self.assertIn("nohup sleep 9 > /tmp/log", shlex.split(seen[3][-1])[-1])

    def test_every_probe_runs_under_a_login_shell(self):
        """A non-interactive ssh's PATH lacks what a login shell finds (tart in ~/.local/bin)."""
        for name, call in (("run", lambda m: m.run(["tart", "list"])), ("have", lambda m: m.have("tart")),
                           ("exists", lambda m: m.exists("/x"))):
            with self.subTest(call=name):
                via = machine.Fake("here")
                via.react(["ssh"], lambda argv, f: machine.Result(0, ""))
                call(machine.Ssh("box", via=via))
                sent = [e[1][-1] for e in via.effects if e[0] == "run"]
                self.assertTrue(sent and all(s.startswith('"$SHELL" -lc ') for s in sent), sent)

    def test_an_effect_over_ssh_is_an_effect_on_the_machine_that_drives_it(self):
        via = machine.Fake("here")
        via.answer(["ssh"])
        m = machine.Ssh("box.example", timeout=3, via=via)
        via.stop_after = 0
        self.assertTrue(m.run(["true"]).ok)
        with self.assertRaises(machine.Killed):
            m.act_run(["rm", "-rf", "/d"])
        via.stop_after = None
        m.act_run(["tart", "clone", "a", "b"], stream=True)
        self.assertEqual(via.applied, 1)
        self.assertEqual(via.effects[-1], ("run", tuple(m.argv("tart clone a b"))))
        self.assertEqual(via.streamed, [tuple(m.argv("tart clone a b"))])

    def test_an_effect_over_ssh_refuses_before_a_destructive_command_asked(self):
        os.environ["WK_DESTRUCTIVE"] = "1"
        via = machine.Fake("here")
        via.answer(["ssh"])
        from wk.act import Refused
        with self.assertRaises(Refused):
            self.stderr(lambda: machine.Ssh("box.example", via=via).act_run(["rm", "-rf", "/d"]))
        self.assertEqual(via.effects, [])

    def test_run_tty_allocates_a_pty_and_honours_cwd(self):
        (argv,) = self.ssh_calls(lambda m: m.run_tty(["lldb", "-o", "attach"], cwd="/src/WebKit"), method="run_tty")
        self.assertEqual((argv[0], argv[-2]), ("ssh", "box.example"))
        self.assertIn("-t", argv)
        self.assertEqual(shlex.split(argv[-1]), ["$SHELL", "-lc", "cd /src/WebKit && lldb -o attach"])

    def test_the_lock_effects_are_one_command_each_and_readlink_answers(self):
        def calls(m):
            self.assertTrue(m.symlink("target", "/locks/r.lock"))
            self.assertEqual(m.readlink("/locks/r.lock"), "target")
            self.assertTrue(m.rename("/locks/r.lock.new", "/locks/r.lock"))
            m.remove_now("/locks/r.lock")
            m.mkdir_now("/locks")
        seen = self.ssh_calls(calls, answer=lambda argv: machine.Result(
            0, "target\n" if shlex.split(argv[-1])[-1].startswith("readlink") else ""))
        self.assertEqual([shlex.split(c[-1])[-1] for c in seen], ["ln -s target /locks/r.lock", "readlink /locks/r.lock",
                         "mv -f /locks/r.lock.new /locks/r.lock", "rm -rf /locks/r.lock", "mkdir -p /locks"])

    def test_copies_are_scp_and_rsync_over_this_sshs_own_opts_naming_both_ends(self):
        def calls(m):
            m.copy_in("/local/a", "/remote/a")
            m.copy_out("/remote/b", "/local/b")
            m.copy_tree_in("/local/tree", "/remote/tree")
            m.copy_tree_out("/remote/tree", "/local/tree", exclude=("*.a", "DerivedSources"))
        seen = self.ssh_calls(calls, m=machine.Ssh("box.example", opts=["-i", "key"], timeout=3))
        self.assertEqual([a[0] for a in seen], ["scp", "scp", "rsync", "rsync"])
        self.assertEqual([a[-2:] for a in seen], [["/local/a", "box.example:/remote/a"],
                                                  ["box.example:/remote/b", "/local/b"],
                                                  ["/local/tree/", "box.example:/remote/tree/"],
                                                  ["box.example:/remote/tree/", "/local/tree/"]])
        for argv in seen[2:]:
            self.assertIn("--chmod=go-w", argv)
            self.assertIn("--delete", argv)
            self.assertIn("-i key", argv[argv.index("-e") + 1])
        self.assertIn(["--exclude", "*.a", "--exclude", "DerivedSources"], [seen[3][i:i + 4] for i in range(len(seen[3]))])

    def test_a_copy_that_fails_raises_and_a_dry_run_copies_nothing(self):
        with self.assertRaises(OSError):
            self.ssh_calls(lambda m: m.copy_out("/remote/a", "/local/a"),
                           answer=lambda argv: machine.Result(1, "", "no such file"))
        os.environ["WK_DRY_RUN"] = "1"
        seen, _ = self.stderr(lambda: self.ssh_calls(lambda m: (m.copy_in("/local/a", "/remote/a"),
                                                              m.copy_tree_out("/remote/tree", "/local/tree"))))
        self.assertEqual(seen, [], "ssh ran under --dry-run")


class TestForward(MachineTest):
    def test_ssh_holds_one_reverse_forward_and_kills_it_on_the_way_out(self):
        via = machine.Fake("here")
        m = machine.Ssh("box.example", opts=["-l", "root"], timeout=3, via=via)
        with m.forward(4567, "/tmp/tunnel.log") as pid:
            self.assertIn(pid, via.pids)
        ((kind, argv, log),) = [e for e in via.effects if e[0] == "spawn"]
        self.assertEqual((argv[:3], argv[-4:], log), (("ssh", "-l", "root"), ("-N", "-R", "127.0.0.1:4567:127.0.0.1:4567", "box.example"),
                                                      "/tmp/tunnel.log"))
        self.assertIn("ExitOnForwardFailure=yes", argv)
        self.assertIn(("kill", pid, int(signal.SIGTERM)), via.effects)
        self.assertNotIn(pid, via.pids)

    def test_a_dry_run_opens_nothing_and_says_so(self):
        os.environ["WK_DRY_RUN"] = "1"

        def held():
            with machine.Ssh("box.example", timeout=3).forward(4567) as pid:
                return pid
        pid, err = self.stderr(held)
        self.assertEqual(pid, 0)
        self.assertIn("would start: ssh", err)
        self.assertIn("-R 127.0.0.1:4567:127.0.0.1:4567", err)

    def test_the_fake_holds_a_pid_while_the_forward_is_held_and_a_raise_still_closes_it(self):
        f = machine.Fake()
        with f.forward(4567) as pid:
            self.assertIn(pid, f.pids)
        self.assertNotIn(pid, f.pids)
        self.assertEqual(f.effects, [("forward", 4567)])
        with self.assertRaises(RuntimeError):
            with f.forward(4567):
                raise RuntimeError("the run died")
        self.assertEqual(f.pids, {os.getpid()})


# A copy's transport named outside the seam: an argv headed scp or rsync, a shell line starting one, podman's cp, shutil's copies.
COPY = re.compile(r"\[\s*[\"'](scp|rsync)[\"']|[\"'](scp|rsync) -|podman\(\)\s*\+\s*\[[\"']cp[\"']|\bshutil\.copy\w*\(")
COPIES_ELSEWHERE = {
    "lib/wk/places.py": "the container driver's half of the one copy: `podman cp` is how bytes cross into a container",
    "lib/wk/bench/mac.py": "pins a payload into the staging tree: both ends are this host's own filesystem",
    "lib/wk/sysimage/macvolume.py": "stages wk-tools onto the bench volume, mounted on the machine that runs the copy",
}


class TestOneCopyPath(unittest.TestCase):
    def test_nothing_outside_the_machine_copies(self):
        files = list((REPO / "lib" / "wk").rglob("*.py"))
        files += [p for p in (REPO / "cmd").iterdir()
                  if p.is_file() and p.read_text(errors="replace").startswith("#!/usr/bin/env python3")]
        found = set()
        for p in files:
            rel = str(p.relative_to(REPO))
            if rel != "lib/wk/machine.py" and any(COPY.search(l) and not l.lstrip().startswith("#")
                                                  for l in p.read_text(errors="replace").splitlines()):
                found.add(rel)
        self.assertEqual(sorted(found - set(COPIES_ELSEWHERE)), [], "copies outside lib/wk/machine.py")
        self.assertEqual(sorted(set(COPIES_ELSEWHERE) - found), [], "listed, and no longer copying")


class TestADetachedJobIsInitsChild(unittest.TestCase):
    def test_the_job_outlives_its_starter_as_a_child_of_init(self):
        """A parent that never reaps (tart's guest agent) would leave the job a zombie that answers `kill -0`.
        Init adopts it, or a subreaper above this process standing in for init (a container's)."""
        reapers, p = {"1"}, str(os.getpid())
        while p not in ("0", "1"):
            reapers.add(p)
            p = subprocess.run(["ps", "-o", "ppid=", "-p", p], capture_output=True, text=True).stdout.strip() or "0"
        with tempfile.TemporaryDirectory() as d:
            line = machine.far_side_start("sleep 5", os.path.join(d, "log"), "echo $!")
            pid = int(subprocess.run(["sh", "-c", line], capture_output=True, text=True, timeout=10).stdout)
            try:
                ppid = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
                self.assertIn(ppid, reapers)
            finally:
                os.kill(pid, signal.SIGKILL)
