import contextlib
import io
import json
import os
import shlex
import subprocess
import unittest

from tests import ROOT
from tests.pueue import FakePueue, bare, iso
from wk.core import act, seams
from wk.core.errors import Refused
from wk.core.fakes import Fake, FakeClock
from wk.core.machine import Result
from wk.tasks import tasks
from wk.tasks.pueue import CLEAN, MTIMES, job, jobs, stamp

BIN = "/home/u/wk/cache/pueue-4.0.5/pueue --config /home/u/wk/pueue/pueue.yml"


def row(status, **more):
    return dict({"id": 7, "created_at": "2024-07-18T14:55:02+02:00", "command": "make", "group": "build", "label": "x:build:jsc-release",
                 "status": status}, **more)


class TestParse(unittest.TestCase):
    def test_the_status_pueue_prints_becomes_tasks_oldest_first(self):
        with open(os.path.join(ROOT, "tests", "data", "pueue-status.json")) as f:
            found = jobs(json.load(f))
        self.assertEqual([(j.id, j.state, j.rc, j.group, j.label, j.command) for j in found],
                         [(0, "done", 0, "default", "", "ls"), (1, "failed", 2, "default", "", "ls"), (2, "queued", None, "default", "", "ls"),
                          (3, "running", None, "default", "", "sleep 9000000"), (4, "queued", None, "default", "", "pueue sleep 60"),
                          (5, "queued", None, "default", "", "days ls")])
        self.assertEqual([round(j.at, 3) for j in found[:4]], [1721307303.192, 1721307310.418, 1721307316.045, 1721307323.365])

    def test_a_killed_task_has_no_exit_code_and_a_label_is_kept(self):
        done = {"enqueued_at": "2024-07-18T14:55:02Z", "start": "2024-07-18T14:55:03Z", "end": "2024-07-18T14:55:09Z"}
        self.assertEqual(job(row({"Done": dict(done, result="Killed")})),
                         seams.Task(7, "x:build:jsc-release", "build", seams.KILLED, "make", None, 1721314509.0))

    def test_a_task_that_ended_any_other_way_failed_with_exit_1(self):
        done = {"enqueued_at": "2024-07-18T14:55:02Z", "start": "2024-07-18T14:55:03Z", "end": "2024-07-18T14:55:09Z"}
        for result in ({"FailedToSpawn": "no such file"}, "Errored", "DependencyFailed"):
            self.assertEqual((job(row({"Done": dict(done, result=result)})).state, job(row({"Done": dict(done, result=result)})).rc), ("failed", 1))

    def test_a_paused_task_is_running_and_a_locked_one_queued(self):
        times = {"enqueued_at": "2024-07-18T14:55:02Z", "start": "2024-07-18T14:55:03Z"}
        self.assertEqual((job(row({"Paused": times})).state, job(row({"Paused": times})).at), ("running", 1721314503.0))
        self.assertEqual(job(row({"Locked": {"previous_status": {"Queued": {"enqueued_at": "2024-07-18T14:55:02Z"}}}})).state, "queued")

    def test_a_time_is_read_with_nanoseconds_milliseconds_none_and_z(self):
        self.assertEqual([stamp(t) for t in ("2024-07-18T14:55:02.656354132+02:00", "2024-07-18T12:55:02.656Z", "2024-07-18T12:55:02Z")],
                         [1721307302.656354, 1721307302.656, 1721307302.0])


class TestClient(unittest.TestCase):
    def setUp(self):
        act.begin(act.Flags())
        self.clock = FakeClock()
        self.m = Fake("srv")
        self.daemon = FakePueue(self.m, self.clock)
        self.t = tasks(bare(self.clock), self.m)

    def effects(self):
        return [line.replace(BIN, "pueue") for line in self.m.journal.effects()]

    def test_wk_home_places_the_binaries_the_config_and_the_logs(self):
        self.assertEqual(self.t.argv("status"), ["/home/u/wk/cache/pueue-4.0.5/pueue", "--config", "/home/u/wk/pueue/pueue.yml", "status"])

    def test_add_queues_one_shell_string_in_the_group_under_the_label_and_returns_the_id(self):
        self.assertEqual((self.t.add(["make", "a b"], "x:test:jsc-release"), self.t.add(["ls"], "y", cwd="/w")), (0, 1))
        self.assertEqual(self.effects(), ["[srv] pueue add --print-task-id --group default --label x:test:jsc-release --working-directory /home/u -- "
                                          + shlex.quote(CLEAN + " " + shlex.join(["make", "a b"])),
                                          "[srv] pueue add --print-task-id --group default --label y --working-directory /w -- " + shlex.quote(CLEAN + " ls")])
        self.assertEqual(self.t.status(), [seams.Task(0, "x:test:jsc-release", "default", "running", CLEAN + " " + shlex.join(["make", "a b"]), None, 1000.0),
                                           seams.Task(1, "y", "default", "queued", CLEAN + " ls", None, 1000.0)])

    def test_a_task_starts_from_the_systems_path_and_a_few_of_the_logins_variables_alone(self):
        login = {"PATH": "/old/wk/bin:/usr/bin", "HOME": "/home/u", "LANG": "C.UTF-8", "SDKROOT": "/sdk", "CPATH": "/inc", "CLAUDECODE": "1",
                 "SHELL": "/bin/zsh"}
        r = subprocess.run(["/bin/sh", "-c", CLEAN + " env"], stdout=subprocess.PIPE, universal_newlines=True, env=login)
        self.assertEqual(sorted(r.stdout.splitlines()), ["HOME=/home/u", "LANG=C.UTF-8", "PATH=/usr/bin:/bin:/usr/sbin:/sbin", "SHELL=/bin/zsh"])

    def test_the_first_build_makes_the_build_group_with_a_limit_of_one(self):
        self.t.add(["make"], "x", group="build")
        self.t.add(["make"], "y", group="build")
        self.assertEqual([e.split(" -- ")[0] for e in self.effects()],
                         ["[srv] pueue group add build --parallel 1", "[srv] pueue add --print-task-id --group build --label x --working-directory /home/u",
                          "[srv] pueue add --print-task-id --group build --label y --working-directory /home/u"])
        self.assertEqual([j.state for j in self.t.status()], ["running", "queued"])

    def test_a_build_group_with_another_limit_is_set_back_to_one(self):
        self.t.limit("build", 3)
        self.t.limit("build", 1)
        self.t.limit("build", 1)
        self.assertEqual(self.effects()[1:], ["[srv] pueue parallel 1 --group build"])

    def test_a_dry_run_adds_nothing_and_has_no_id(self):
        act.begin(act.Flags(dry_run=True))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual((self.t.add(["make"], "x", group="build"), self.daemon.tasks), (None, {}))

    def test_a_daemon_that_does_not_answer_is_refused_naming_setup(self):
        del self.m.files[self.daemon.path]
        with self.assertRaisesRegex(Refused, "pueue does not answer on srv: Error: Failed to connect to the daemon\n    `wk machine setup srv` installs and starts it"):
            self.t.status()

    def test_a_dry_run_plans_the_build_group_a_running_daemon_lacks(self):
        act.begin(act.Flags(dry_run=True))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.t.limit("build", 1)
        self.assertEqual(err.getvalue(), "would: [srv] %s group add build --parallel 1\n" % BIN)

    def test_a_refused_add_is_refused_with_pueues_words(self):
        with self.assertRaisesRegex(Refused, 'pueue add failed on srv: Group "nightly" does not exist'):
            self.t.add(["make"], "x", group="nightly")

    def test_kill_kills_a_running_task_removes_a_queued_one_and_leaves_an_ended_one(self):
        a, b = self.t.add(["make"], "x"), self.t.add(["make"], "y")
        self.t.kill(b)
        self.t.kill(a)
        self.t.kill(a)
        self.assertEqual(self.effects()[2:], ["[srv] pueue remove 1", "[srv] pueue kill 0"])
        self.assertEqual([(j.id, j.state) for j in self.t.status()], [(0, "killed")])

    def test_wait_polls_on_the_clock_until_the_task_ends_or_the_timeout_passes(self):
        a = self.t.add(["make"], "x")
        self.assertEqual((self.t.wait(a, timeout=6).state, self.clock.now()), ("running", 1006.0))
        self.clock.after(31, lambda: self.daemon.finish(a, rc=2))
        self.assertEqual((self.t.wait(a).rc, self.clock.now()), (2, 1038.0))

    def test_log_is_the_whole_output_so_far(self):
        a = self.t.add(["make"], "x")
        self.daemon.finish(a, log="one\ntwo\n")
        self.assertEqual((self.t.log(a), self.m.calls[-1].argv[3:]), ("one\ntwo\n", ("log", "0", "--json", "--full")))

    def test_the_log_of_a_task_pueue_does_not_have_is_refused(self):
        self.m.on(self.t.argv("log"), Result(1, "", "no such task"))
        self.assertRaisesRegex(Refused, "pueue has no log of task 9 on srv: no such task", self.t.log, 9)

    def test_follow_streams_the_log_to_the_terminal(self):
        a = self.t.add(["make"], "x")
        self.daemon.logs[a] = "[1/2] compiling\n"
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.t.follow(a)
        self.assertEqual((out.getvalue(), self.m.calls[-1].argv[3:]), ("[1/2] compiling\n", ("follow", "0")))

    def test_quiet_is_how_long_ago_each_log_grew(self):
        a = self.t.add(["make"], "x")
        self.clock.sleep(40)
        self.daemon.grew[a] = 1010.5
        self.assertEqual((self.t.quiet(), self.m.calls[-1].argv[4:]), ({0: 29.5}, ("/home/u/wk/pueue/task_logs",)))

    def test_logs_whose_ages_cannot_be_read_are_refused(self):
        self.m.on(["python3", "-I", "-c", MTIMES], Result(1, "", "No such file or directory"))
        self.assertRaisesRegex(Refused, "cannot read when the task logs on srv last grew: No such file", self.t.quiet)

    def test_the_fake_daemon_tells_time_as_pueue_does(self):
        self.assertEqual(stamp(iso(1000.25)), 1000.25)

