import os
import contextlib
import io
import select
import signal
import stat
import tempfile
import unittest
from unittest import mock

from tests import REACHED
from wk.core import act, machine
from wk.core.errors import Refused, Unreachable
from wk.core.machine import SSH, Local, Ssh


class OnDisk(unittest.TestCase):
    def setUp(self):
        act.begin(act.Flags())
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir, self.m = tmp.name, Local()

    def fifo(self):
        """A fifo held open for reading: it reads as ended only once no process holds it open for writing."""
        path = os.path.join(self.dir, "fifo")
        os.mkfifo(path)
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, fd)
        return path, fd

    def ended(self, fd):
        return bool(select.select([fd], [], [], 10)[0]) and os.read(fd, 100) == b""


class TestLocal(OnDisk):
    def test_run_returns_the_status_and_both_streams(self):
        r = self.m.run(["sh", "-c", "echo out; echo err >&2; exit 3"])
        self.assertEqual((r.rc, r.out, r.err, r.ok), (3, "out\n", "err\n", False))

    def test_input_arrives_on_stdin_and_cwd_and_env_apply(self):
        r = self.m.run(["sh", "-c", 'cat; pwd -P; echo "$WK_T"'], input="in\n", cwd=os.path.realpath(self.dir), env={"WK_T": "v"})
        self.assertEqual(r.out, "in\n%s\nv\n" % os.path.realpath(self.dir))

    def test_a_program_that_does_not_exist_is_127(self):
        self.assertEqual(self.m.run(["/no/such/program"]).rc, 127)

    def test_a_timeout_is_124_and_kills_the_whole_process_group(self):
        path, fd = self.fifo()
        r = self.m.run(["sh", "-c", 'sleep 60 >"$0" & wait', path], timeout=0.5)
        self.assertEqual((r.rc, r.err), (124, "timed out after 0.5s"))
        self.assertTrue(self.ended(fd), "the grandchild outlives the timeout")

    def test_an_interactive_run_times_out_too(self):
        self.assertEqual(self.m.run(["sleep", "60"], stream=True, tty=True, timeout=0.2).rc, 124)

    def test_a_streamed_run_captures_nothing(self):
        r = self.m.run(["true"], stream=True)
        self.assertEqual((r.rc, r.out), (0, ""))

    def test_an_interrupt_kills_the_child_before_it_propagates(self):
        path, fd = self.fifo()

        def interrupted(*args, **kwargs):
            self.assertTrue(select.select([fd], [], [], 10)[0])
            self.assertEqual(os.read(fd, 100), b"up\n")
            raise KeyboardInterrupt
        with mock.patch("subprocess.Popen.communicate", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.m.run(["sh", "-c", 'exec 3>"$0"; echo up >&3; exec sleep 60', path])
        self.assertTrue(self.ended(fd))

    def test_write_makes_the_parents_sets_the_mode_and_read_returns_it(self):
        path = os.path.join(self.dir, "a", "b", "f")
        self.m.write(path, "text\n", mode="600")
        self.assertEqual((self.m.read(path), stat.S_IMODE(os.stat(path).st_mode)), ("text\n", 0o600))
        self.assertEqual(self.m.listdir(os.path.dirname(path)), ["f"])

    def test_write_carries_bytes_on_stdin_and_never_in_an_argv(self):
        with mock.patch("wk.core.machine.local_run", wraps=machine.local_run) as run:
            self.m.write(os.path.join(self.dir, "key"), "s3cret")
        argv, kwargs = run.call_args
        self.assertNotIn("s3cret", " ".join(argv[0]))
        self.assertEqual(kwargs["input"], "s3cret")

    def test_a_write_that_cannot_be_made_is_refused_with_the_reason(self):
        self.m.write(os.path.join(self.dir, "file"), "")
        with self.assertRaisesRegex(Refused, "cannot write .*file/under on here: "):
            self.m.write(os.path.join(self.dir, "file", "under"), "")

    def test_an_absent_file_reads_as_none_does_not_exist_and_lists_nothing(self):
        path = os.path.join(self.dir, "absent")
        self.assertEqual((self.m.read(path), self.m.exists(path), self.m.listdir(path)), (None, False, []))

    def test_listdir_is_sorted_and_includes_dotfiles(self):
        for name in ("b", ".a", "c"):
            self.m.write(os.path.join(self.dir, name), "")
        self.assertEqual(self.m.listdir(self.dir), [".a", "b", "c"])

    def test_remove_takes_a_tree_and_an_absent_path_alike(self):
        self.m.write(os.path.join(self.dir, "t", "f"), "")
        self.m.remove(os.path.join(self.dir, "t"))
        self.m.remove(os.path.join(self.dir, "t"))
        self.assertFalse(self.m.exists(os.path.join(self.dir, "t")))

    def test_copy_carries_bytes_unchanged_to_another_machine(self):
        src, dst = os.path.join(self.dir, "src"), os.path.join(self.dir, "d", "dst")
        self.m.write(src, b"\xff\x00\r\n")
        self.m.copy(src, Local(), dst, mode="755")
        with open(dst, "rb") as f:
            self.assertEqual((f.read(), stat.S_IMODE(os.stat(dst).st_mode)), (b"\xff\x00\r\n", 0o755))

    def test_spawn_returns_while_the_program_runs_on_writing_its_log(self):
        path, fd = self.fifo()
        self.m.spawn(["sh", "-c", "echo $$; exec sleep 60"], path)
        self.assertTrue(select.select([fd], [], [], 10)[0])
        pid = int(os.read(fd, 100))
        os.kill(pid, signal.SIGKILL)
        self.assertTrue(self.ended(fd))

    def test_home_is_the_logins_home(self):
        self.assertEqual(self.m.home, os.environ["HOME"])

    def test_under_dry_run_a_mutator_prints_and_changes_nothing_and_a_read_still_runs(self):
        act.begin(act.Flags(dry_run=True))
        path, err = os.path.join(self.dir, "f"), io.StringIO()
        with contextlib.redirect_stderr(err):
            self.m.write(path, "x")
            self.m.remove(self.dir)
            self.m.spawn(["touch", path], "/dev/null")
            self.assertEqual(self.m.act(["touch", path]).rc, 0)
        self.assertEqual(err.getvalue(), "would: [here] write {0}\nwould: [here] remove {1}\nwould: [here] spawn touch {0}\n"
                                         "would: [here] touch {0}\n".format(path, self.dir))
        self.assertEqual((self.m.exists(path), self.m.run(["echo", "read"]).out), (False, "read\n"))


STUB = """#!/bin/sh
printf '%s\\n' "$@" >"$STUB/argv"
cat >"$STUB/stdin"
printf 'one\\r\\ntwo\\r\\n'
exit "$(cat "$STUB/rc")"
"""


class TestSsh(OnDisk):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.dir, "ssh"), "w") as f:
            f.write(STUB)
        os.chmod(os.path.join(self.dir, "ssh"), 0o755)
        self.rc(0)
        patch = mock.patch.dict(os.environ, {"PATH": self.dir + os.pathsep + os.environ["PATH"], "STUB": self.dir})
        patch.start()
        self.addCleanup(patch.stop)
        self.m = Ssh("srv")

    def rc(self, n):
        with open(os.path.join(self.dir, "rc"), "w") as f:
            f.write(str(n))

    def sent(self, name):
        with open(os.path.join(self.dir, name)) as f:
            return f.read()

    def test_the_real_ssh_is_a_shim_that_fails_loudly(self):
        del os.environ["STUB"]
        os.environ["PATH"] = os.environ["PATH"].split(os.pathsep, 1)[1]
        r = Local().run(["ssh", "srv", "true"])
        self.assertEqual((r.rc, r.err), (REACHED, "a unit test reached the real ssh: srv true\n"))

    def test_a_command_goes_to_the_tailnet_name_wrapped_in_the_login_shell(self):
        self.m.run(["git", "status", "a b"])
        self.assertEqual(self.sent("argv").splitlines(), SSH + ["srv", "\"$SHELL\" -lc 'git status '\"'\"'a b'\"'\"''"])

    def test_the_options_bound_a_dead_address_and_a_vanished_session_and_share_one_connection(self):
        for option in ("BatchMode=yes", "ConnectTimeout=10", "ServerAliveInterval=15", "ServerAliveCountMax=3",
                       "ControlMaster=auto", "ControlPath=~/.ssh/wk-%C", "ControlPersist=60"):
            self.assertIn(option, SSH)

    def test_cwd_and_env_become_part_of_the_remote_command(self):
        self.assertEqual(self.m.argv(["wk", "build"], cwd="/w s", env={"WK_YES": "1", "WK_DRY_RUN": "1"})[-1],
                         "\"$SHELL\" -lc 'cd '\"'\"'/w s'\"'\"' && env WK_DRY_RUN=1 WK_YES=1 wk build'")

    def test_a_forward_is_a_reverse_forward_on_a_connection_of_its_own_that_fails_if_the_port_is_taken(self):
        argv = self.m.argv(["wk", "ai", "claude", "x"], tty=True, forward=(20123, "vault:3128"))
        self.assertEqual(argv[:7], ["ssh", "-o", "ControlPath=none", "-o", "ExitOnForwardFailure=yes", "-R", "127.0.0.1:20123:vault:3128"])
        self.assertEqual((argv[7:-3], argv[-3:-1]), (SSH, ["-t", "srv"]))
        self.m.run(["true"], forward=(20123, "vault:3128"))
        self.assertIn("-R\n127.0.0.1:20123:vault:3128\n", self.sent("argv"))

    def test_an_interactive_run_asks_for_a_terminal(self):
        self.assertEqual(self.m.argv(["wk", "enter", "x"], tty=True)[-3:-1], ["-t", "srv"])

    def test_carriage_returns_are_stripped_and_the_status_is_the_far_commands(self):
        self.rc(7)
        r = self.m.run(["true"])
        self.assertEqual((r.rc, r.out), (7, "one\ntwo\n"))

    def test_sshs_own_failure_is_unreachable_never_an_answer(self):
        self.rc(255)
        for ask in (lambda: self.m.run(["true"]), lambda: self.m.exists("/x"), lambda: self.m.read("/x"),
                    lambda: self.m.listdir("/x"), lambda: self.m.write("/x", "")):
            with self.assertRaises(Unreachable) as caught:
                ask()
            self.assertEqual(caught.exception.status, 4)

    def test_a_written_secret_travels_on_stdin_and_never_in_the_argv(self):
        self.m.write("/home/u/wk/secrets/token", "s3cret", mode="600")
        self.assertEqual(self.sent("stdin"), "s3cret")
        self.assertNotIn("s3cret", self.sent("argv"))
        self.assertIn("/home/u/wk/secrets/token 600", self.sent("argv"))
