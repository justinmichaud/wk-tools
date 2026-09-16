"""The plan as a graph, and the one scheduler that runs it.

`wk ab` no longer holds its plan as a list of command strings re-parsed by
prefix: it declares steps -- an id, one command, the machine it runs on, what
it needs, what it holds exclusively, and how to ask whether it is already done
-- and lib/sched.py schedules them. The graph and its scheduling are Python
(CLAUDE.md: structured data is not handled in bash), so the half that decides
what runs when is driven here against fake steps that run no command at all,
and the half that runs shell is driven against `true`, `false` and files in a
scratch directory.

The `wk ab` end is driven as a real dry run against a mirror, a fleet and a
GitHub of this test's own: a scratch git repository, machine confs under
WK_MACHINES_DIR, and a `git` on PATH that rewrites every github.com URL to
that repository. Nothing here reaches a board, a network or a build.

Run: python3 -m unittest tests.test_sched -v
"""
import contextlib
import importlib.util
import os
import shutil
import subprocess
import threading
import unittest

from tests.support import REPO, WkTest, bash, run, scratch_dir, stub_path

SHA = "d" * 40


def _load_sched():
    """By path, under a name of its own: `sched` is a stdlib module."""
    spec = importlib.util.spec_from_file_location("wk_sched", REPO / "lib" / "sched.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sched = _load_sched()


def step(id, machine="m", needs="", holds="", done="", command="true"):
    return sched.Step(id, machine, needs, holds, done, command)


def records(*rows):
    return "".join("\t".join(r) + "\n" for r in rows)


class FakeRuns:
    """A runner that runs nothing: it records what was asked, how many steps
    were in flight at once, and which pairs overlapped."""

    def __init__(self, codes=None, block=()):
        self.codes = dict(codes or {})
        self.block = set(block)
        self.started, self.together, self.live = [], set(), set()
        self.peak = 0
        self.lock = threading.Lock()
        self.gate = threading.Event()

    def __call__(self, step):
        with self.lock:
            self.started.append(step.id)
            self.live.add(step.id)
            self.peak = max(self.peak, len(self.live))
            for other in self.live:
                if other != step.id:
                    self.together.add(frozenset((step.id, other)))
        if step.id in self.block:
            self.gate.wait(10)
        with self.lock:
            self.live.discard(step.id)
        code = self.codes.get(step.id, 0)
        if isinstance(code, list):
            code = code.pop(0) if code else 0
        return code

    def overlapped(self, a, b):
        return frozenset((a, b)) in self.together


class TestTheRecords(WkTest):
    """A malformed graph is refused before anything runs, naming the step."""

    def refusal(self, text):
        with self.assertRaises(SystemExit) as caught:
            sched.parse_steps(text)
        return str(caught.exception)

    def test_a_record_is_six_fields(self):
        self.assertIn("has 4 fields, not 6", self.refusal("a\tm\t\tx\n"))

    def test_a_step_needing_one_nothing_declares_is_named(self):
        msg = self.refusal(records(("a", "m", "ghost", "", "", "true")))
        self.assertIn("'a' needs 'ghost'", msg)

    def test_an_id_names_one_step(self):
        msg = self.refusal(records(("a", "m", "", "", "", "true"),
                                   ("a", "m", "", "", "", "true")))
        self.assertIn("declared 2 times", msg)

    def test_a_step_with_no_command_is_refused(self):
        self.assertIn("declares no command", self.refusal(records(("a", "m", "", "", "", ""))))

    def test_steps_that_need_each_other_are_refused_by_name(self):
        msg = self.refusal(records(("a", "m", "b", "", "", "true"),
                                   ("b", "m", "a", "", "", "true")))
        self.assertIn("need each other", msg)
        self.assertIn("a -> b -> a", msg)

    def test_needs_and_holds_are_read_as_lists(self):
        steps = sched.parse_steps(records(("a", "m", "", "", "", "true"),
                                          ("b", "m", "", "", "", "true"),
                                          ("c", "m", "a,b", "lane:x,device:d", "", "true")))
        self.assertEqual(steps[2].needs, ("a", "b"))
        self.assertEqual(steps[2].holds, ("lane:x", "device:d"))


class TestWhatRunsAtOnce(WkTest):
    def waves(self, steps, done=()):
        return [[s.id for s in wave] for wave in sched.waves(steps, done)]

    def test_independent_steps_are_one_wave(self):
        self.assertEqual(self.waves([step("a"), step("b")]), [["a", "b"]])

    def test_a_need_is_a_later_wave(self):
        self.assertEqual(self.waves([step("a"), step("b", needs="a")]), [["a"], ["b"]])

    def test_one_resource_is_one_step_at_a_time(self):
        self.assertEqual(self.waves([step("a", holds="lane:x"), step("b", holds="lane:x")]),
                         [["a"], ["b"]])

    def test_a_board_and_a_lane_interleave(self):
        """The shape the A/B wants: while one arm is deployed to the board the
        other arm is still building in the lane."""
        steps = [step("build-a", holds="lane:x"),
                 step("build-b", holds="lane:x"),
                 step("deploy-a", needs="build-a", holds="device:d")]
        self.assertEqual(self.waves(steps), [["build-a"], ["build-b", "deploy-a"]])

    def test_a_step_already_done_is_not_in_any_wave(self):
        self.assertEqual(self.waves([step("a"), step("b", needs="a")], done={"a"}), [["b"]])


class TestWhatIsWorthRunning(WkTest):
    """A step exists to produce something. When the thing it feeds is already
    there, it is not run again -- which is what makes a profile-guided cycle
    re-runnable without collecting on the board a second time."""

    def needed(self, steps, done=()):
        return sorted(sched.needed(steps, done))

    def test_a_phase_whose_result_is_already_there_is_not_run(self):
        steps = [step("instr"), step("collect", needs="instr"),
                 step("slot", needs="collect", done="true")]
        self.assertEqual(self.needed(steps, {"slot"}), [])

    def test_the_phases_of_an_unfinished_one_all_are(self):
        steps = [step("instr"), step("collect", needs="instr"),
                 step("slot", needs="collect", done="true")]
        self.assertEqual(self.needed(steps), ["collect", "instr", "slot"])

    def test_a_step_something_unfinished_still_needs_stays(self):
        """The slot is built, so its build does not run again -- but what
        deploys it has not, and still needs it."""
        steps = [step("slot", done="true"), step("deploy", needs="slot")]
        self.assertEqual(self.needed(steps, {"slot"}), ["deploy"])

    def test_the_scheduler_runs_none_of_a_pruned_branch(self):
        runs = FakeRuns()
        steps = [step("instr"), step("collect", needs="instr"),
                 step("slot", needs="collect", done="true"), step("deploy", needs="slot")]
        s = sched.Scheduler(steps, runs, lambda step: step.id == "slot")
        self.assertEqual(s.run_all(), 0)
        self.assertEqual(runs.started, ["deploy"])
        self.assertEqual([x.id for x in s.unneeded], ["instr", "collect"])


class TestTheScheduler(WkTest):
    """Against steps that run nothing: what starts, what waits, what is never
    run at all, and what a failure or a refusal does to the rest."""

    def sched(self, steps, runs, done=(), retry_exit=sched.RETRY_EXIT):
        s = sched.Scheduler(steps, runs, lambda step: step.id in done, retry_exit=retry_exit)
        return s, s.run_all()

    def test_everything_ready_starts_at_once(self):
        runs = FakeRuns(block=("a", "b"))
        steps = [step("a"), step("b")]
        s = sched.Scheduler(steps, runs, lambda step: False)
        thread = threading.Thread(target=s.run_all)
        thread.start()
        for _ in range(100):
            if runs.peak >= 2:
                break
            threading.Event().wait(0.02)
        runs.gate.set()
        thread.join(20)
        self.assertEqual(runs.peak, 2, "the two independent steps did not run at once")

    def test_a_held_resource_keeps_two_steps_apart(self):
        runs = FakeRuns()
        s, rc = self.sched([step("a", holds="lane:x"), step("b", holds="lane:x")], runs)
        self.assertEqual(rc, 0)
        self.assertFalse(runs.overlapped("a", "b"), "both held lane:x")
        self.assertEqual(sorted(runs.started), ["a", "b"])

    def test_a_step_that_answers_done_is_never_run(self):
        runs = FakeRuns()
        steps = [step("a", done="true"), step("b", needs="a")]
        s, rc = self.sched(steps, runs, done={"a"})
        self.assertEqual(rc, 0)
        self.assertEqual(runs.started, ["b"], "the done step ran anyway")
        self.assertEqual([x.id for x in s.already], ["a"])

    def test_a_predicate_is_only_asked_of_a_step_that_declares_one(self):
        asked = []

        def is_done(step):
            asked.append(step.id)
            return False

        s = sched.Scheduler([step("a"), step("b", done="false")], FakeRuns(), is_done)
        s.run_all()
        self.assertEqual(asked, ["b"])

    def test_a_failure_stops_what_needed_it_and_nothing_else(self):
        runs = FakeRuns(codes={"a": 3})
        steps = [step("a"), step("b", needs="a"), step("c")]
        s, rc = self.sched(steps, runs)
        self.assertEqual(rc, 1)
        self.assertEqual([x.id for x, _ in s.failed], ["a"])
        self.assertEqual([x.id for x in s.skipped], ["b"])
        self.assertIn("c", runs.started, "an independent step was abandoned over another's failure")

    def test_a_refusal_is_not_now_and_the_step_keeps_its_place(self):
        """A machine's own admission control refuses a build that will not fit
        beside the ones running; that is not a failure, and the step is tried
        again once something else ends."""
        runs = FakeRuns(codes={"b": [sched.RETRY_EXIT, 0]}, block=("a",))
        steps = [step("a"), step("b")]
        s = sched.Scheduler(steps, runs, lambda step: False)
        thread = threading.Thread(target=s.run_all)
        thread.start()
        for _ in range(200):
            if runs.started.count("b") == 1:
                break
            threading.Event().wait(0.02)
        runs.gate.set()
        thread.join(20)
        self.assertEqual(runs.started.count("b"), 2, "the refused step was not tried again")
        self.assertEqual([x.id for x in s.ran], ["a", "b"] if s.ran[0].id == "a" else ["b", "a"])
        self.assertEqual(s.failed, [])

    def test_a_refusal_with_nothing_left_running_ends_the_run(self):
        """Nothing is running that could free what the step wants, so it is
        left rather than retried forever, and the run says so."""
        runs = FakeRuns(codes={"a": sched.RETRY_EXIT})
        s, rc = self.sched([step("a")], runs)
        self.assertEqual(rc, 1)
        self.assertEqual([x.id for x in s.left], ["a"])
        self.assertEqual(runs.started, ["a"])

    def test_a_whole_graph_that_works_exits_zero(self):
        runs = FakeRuns()
        steps = [step("a"), step("b", needs="a"), step("c", needs="a,b")]
        s, rc = self.sched(steps, runs)
        self.assertEqual(rc, 0)
        self.assertEqual(runs.started, ["a", "b", "c"])


class TestThroughTheShell(WkTest):
    """The other half: the commands and predicates are shell, run in a shell
    that sources the preludes named."""

    def sched_py(self, mode, text, *args):
        return subprocess.run(
            ["python3", str(REPO / "lib" / "sched.py"), mode, *args],
            input=text, capture_output=True, text=True, timeout=120)

    def test_run_runs_every_command_and_reports_the_order(self):
        with scratch_dir() as tmp:
            text = records(
                ("a", "m", "", "r", "", "touch %s/a" % tmp),
                ("b", "m", "a", "r", "", "touch %s/b" % tmp))
            cp = self.sched_py("run", text)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertTrue((tmp / "a").exists() and (tmp / "b").exists())
            self.assertIn("[1/2] a", cp.stderr)

    def test_a_done_predicate_that_answers_yes_keeps_the_command_from_running(self):
        with scratch_dir() as tmp:
            text = records(("a", "m", "", "", "true", "touch %s/ran" % tmp))
            cp = self.sched_py("run", text)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertFalse((tmp / "ran").exists(), "a done step ran")
            self.assertIn("already done", cp.stderr)

    def test_a_failing_command_fails_the_run_and_names_what_did_not_run(self):
        text = records(("a", "m", "", "", "", "exit 4"),
                       ("b", "m", "a", "", "", "true"))
        cp = self.sched_py("run", text)
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("failed: a (exit 4)", cp.stderr)
        self.assertIn("not run: b", cp.stderr)

    def test_a_prelude_is_sourced_before_every_command(self):
        with scratch_dir() as tmp:
            (tmp / "prelude.sh").write_text("greet() { echo hello > %s/said; }\n" % tmp)
            cp = self.sched_py("run", records(("a", "m", "", "", "", "greet")),
                               "--prelude", str(tmp / "prelude.sh"))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual((tmp / "said").read_text().strip(), "hello")

    def test_one_log_per_resource_so_a_boards_work_reads_in_order(self):
        with scratch_dir() as tmp:
            text = records(("deploy", "m", "", "device:rpi4", "", "echo deployed"),
                           ("bench", "m", "deploy", "device:rpi4", "", "echo benched"),
                           ("report", "m", "bench", "", "", "echo reported"))
            cp = self.sched_py("run", text, "--log-dir", str(tmp))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual((tmp / "rpi4.log").read_text(), "deployed\nbenched\n")
            self.assertEqual((tmp / "report.log").read_text(), "reported\n")

    def test_the_on_start_hook_is_told_where_the_step_is_in_the_plan(self):
        """What keeps one task record stepping through a graph: `wk status`
        reads a line number into the flat plan `steps` prints."""
        with scratch_dir() as tmp:
            text = records(("a", "m", "", "", "", "true"), ("b", "m", "a", "", "", "true"))
            cp = self.sched_py("run", text, "--on-start", "echo {step} {id} >> %s/steps" % tmp)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual((tmp / "steps").read_text(), "1 a\n2 b\n")

    def test_the_flat_plan_is_the_commands_in_schedule_order(self):
        text = records(("b", "m", "a", "", "", "second"), ("a", "m", "", "", "", "first"))
        cp = self.sched_py("steps", text)
        self.assertEqual(cp.stdout.split(), ["first", "second"])

    def test_plan_says_which_steps_are_done_and_which_are_not_needed(self):
        text = records(("instr", "m", "", "", "false", "true"),
                       ("slot", "m", "instr", "", "true", "true"))
        cp = self.sched_py("plan", text)
        rows = {l.split()[0]: l for l in cp.stdout.splitlines() if "[" in l}
        self.assertIn("[already done]", rows["slot"])
        self.assertIn("[not needed]", rows["instr"])
        self.assertIn("nothing: every step is already done", cp.stdout)

    def test_plan_prints_the_graph_and_the_schedule_and_runs_nothing(self):
        with scratch_dir() as tmp:
            text = records(("a", "moose", "", "lane:x", "", "touch %s/ran" % tmp),
                           ("b", "moose", "a", "lane:x", "", "true"))
            cp = self.sched_py("plan", text)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertFalse((tmp / "ran").exists(), "plan ran a command")
            self.assertIn("on moose", cp.stdout)
            self.assertIn("holds lane:x", cp.stdout)
            self.assertIn("1. a", cp.stdout)
            self.assertIn("2. b", cp.stdout)


class TestTheBashSide(WkTest):
    """lib/sched.sh: bash declares steps and calls the scheduler. It parses
    nothing."""

    def sh(self, script, **kw):
        return bash('. "%s/lib/sched.sh"\n%s' % (REPO, script), **kw)

    def test_a_step_is_one_record_of_six_fields(self):
        with scratch_dir() as tmp:
            cp = self.sh('sched_begin %s/steps\n'
                         'sched_step id m "a,b" "lane:x" "true" "wk sysimage build p"\n'
                         % tmp)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual((tmp / "steps").read_text().split("\t"),
                             ["id", "m", "a,b", "lane:x", "true", "wk sysimage build p\n"])

    def test_a_step_declared_with_no_graph_open_is_refused(self):
        cp = self.sh('sched_step a m "" "" "" "true"')
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("no graph is open", cp.stdout + cp.stderr)

    def test_a_steps_command_is_the_command_a_person_types(self):
        """`wk` in a step is this checkout's, so what the plan prints is what
        runs."""
        with scratch_dir() as tmp:
            cp = self.sh('sched_begin %s/steps\n'
                         'sched_step v m "" "" "" "wk sysimage --list"\n'
                         'sched_run --log-dir %s\n' % (tmp, tmp))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("wpewebkit-2.38-buildroot-rpi3-32", (tmp / "v.log").read_text())

    def test_the_plan_goes_where_a_dry_runs_reporting_goes(self):
        with scratch_dir() as tmp:
            cp = self.sh('sched_begin %s/steps\n'
                         'sched_step a m "" "" "" "true"\n'
                         'sched_plan\n' % tmp)
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertIn("graph -- 1 step", cp.stderr)
            self.assertEqual(cp.stdout, "")


GIT_STUB = '''#!/usr/bin/env python3
import os, re, sys
real = os.environ["WK_TEST_REAL_GIT"]
fake = os.environ["WK_TEST_FAKE_GITHUB"]
args = [re.sub(r"^https://github\\.com/[^/]+/([A-Za-z]+)(\\.git)?$",
               fake + r"/\\1.git", a) for a in sys.argv[1:]]
os.execv(real, ["git"] + args)
'''

MACHINE_CONF = '''NODE_SSH=fakeboard-%(name)s
NODE_DRIVER=no-such-driver
NODE_DEVICE=/dev/null
NODE_PROFILE=%(profile)s
NODE_ROLE=bench-device
NODE_NOTE="a board that is not there"
'''

# One branch only WebKit carries, and two both do: which fork repository has a
# branch is the question `wk ab <owner>:<branch>` answers before it fetches.
FAKE_BRANCHES = {"WebKit": ("webkitglib/2.52", "wpe-2.38", "feature-x"),
                 "WPEWebKit": ("webkitglib/2.52", "wpe-2.38")}


@contextlib.contextmanager
def ab_env(boards):
    """A mirror, a fleet and a GitHub of this test's own. `boards` is
    {device: profile}; the fake GitHub is two repositories of one commit, and
    the stub `git` rewrites every github.com URL to them, so a fetch, an
    ls-remote and the merge-base all answer offline."""
    with scratch_dir(prefix="wk-test-ab-") as tmp:
        state, store, machines = tmp / "state", tmp / "store", tmp / "machines"
        (state / "wk" / "git").mkdir(parents=True)
        store.mkdir()
        machines.mkdir()
        for name, profile in boards.items():
            (machines / ("%s.conf" % name)).write_text(
                MACHINE_CONF % {"name": name, "profile": profile})
        git = shutil.which("git")

        def g(*args, cwd=tmp):
            return subprocess.run([git, *args], cwd=str(cwd), check=True,
                                  capture_output=True, text=True).stdout.strip()

        src = tmp / "src"
        src.mkdir()
        g("init", "-q", cwd=src)
        g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
          "--allow-empty", "-m", "the base commit", cwd=src)
        head = g("rev-parse", "HEAD", cwd=src)
        github = tmp / "github"
        github.mkdir()
        for repo, branches in FAKE_BRANCHES.items():
            path = github / ("%s.git" % repo)
            g("init", "-q", "--bare", str(path))
            for branch in branches:
                g("push", "-q", str(path), "HEAD:refs/heads/%s" % branch, cwd=src)
        mirror = state / "wk" / "git" / "WebKit.git"
        g("init", "-q", "--bare", str(mirror))
        for name, repo in (("origin", "WebKit"), ("fork", "WebKit"),
                           ("wpe", "WPEWebKit"), ("forkwpe", "WPEWebKit")):
            g("-C", str(mirror), "fetch", "-q", str(github / ("%s.git" % repo)),
              "+refs/heads/*:refs/remotes/%s/*" % name)
        with stub_path({"git": GIT_STUB}) as binp:
            yield {
                "head": head,
                "env": {"XDG_STATE_HOME": str(state), "WK_STORE": str(store),
                        "WK_MACHINES_DIR": str(machines),
                        "WK_TEST_REAL_GIT": git, "WK_TEST_FAKE_GITHUB": str(github),
                        "PATH": "%s:%s" % (binp, os.environ["PATH"])},
            }


def step_lines(out):
    """The `<id>  [state]  on <machine>...` lines of the graph, by id."""
    rows = {}
    for line in out.splitlines():
        if "[to run]" in line or "[already done]" in line:
            rows[line.split()[0]] = line.strip()
    return rows


def wave_lines(out):
    waves = []
    for line in out.splitlines():
        stripped = line.strip()
        if stripped[:1].isdigit() and ". " in stripped:
            waves.append([w.strip() for w in stripped.split(". ", 1)[1].split(",")])
    return waves


class TestTheAbGraph(WkTest):
    """`wk ab --dry-run` prints the graph and the schedule, and runs nothing."""

    def dry_run(self, spec, *args, boards=None, **kw):
        with ab_env(boards or {"rpi3": "wpewebkit-2.38-buildroot-rpi3-32"}) as env:
            spec = env["head"] if spec == "HEAD" else spec
            cp = run("ab", spec, "--dry-run", *args, env=env["env"], timeout=240, **kw)
            return cp, env

    def test_the_steps_declare_what_they_need_and_what_they_hold(self):
        cp, _ = self.dry_run("HEAD", "--release", "2.38", "--devices", "rpi3")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        rows = step_lines(cp.stdout)
        profile = "wpewebkit-2.38-buildroot-rpi3-32"
        self.assertIn("holds lane:%s" % profile, rows["image:%s" % profile])
        self.assertIn("needs image:%s" % profile, rows["slot:%s:base" % profile])
        self.assertIn("holds device:rpi3", rows["deploy:rpi3:base"])
        self.assertIn("needs deploy:rpi3:base deploy:rpi3:pr", rows["bench:rpi3:speedometer3"])
        self.assertIn("needs bench:rpi3:speedometer3", rows["report"])

    def test_the_second_arm_builds_while_the_first_is_on_the_board(self):
        """What the old plan could not do: its order was every build in turn,
        then every board."""
        cp, _ = self.dry_run("HEAD", "--release", "2.38", "--devices", "rpi3")
        profile = "wpewebkit-2.38-buildroot-rpi3-32"
        together = [w for w in wave_lines(cp.stdout)
                    if "deploy:rpi3:base" in w and "slot:%s:pr" % profile in w]
        self.assertTrue(together, cp.stdout)

    def test_a_profile_guided_arm_is_the_cycles_phases_not_one_build(self):
        """The lane declares them (image/pgo.sh), so one arm's collection on
        the board and the other arm's instrumented build are separate steps and
        run at once."""
        cp, _ = self.dry_run("HEAD", "--release", "2.52", "--builder", "yocto",
                             "--devices", "rpi5-64",
                             boards={"rpi5": "webkit-2.52-yocto-rpi5-64"})
        self.assertEqual(cp.returncode, 0, cp.stdout)
        profile = "webkit-2.52-yocto-rpi5-64"
        rows = step_lines(cp.stdout)
        self.assertIn("holds lane:%s" % profile, rows["instr:%s:base" % profile])
        self.assertIn("holds device:rpi5", rows["collect:rpi5:base:speedometer3"])
        together = [w for w in wave_lines(cp.stdout)
                    if "deploy:rpi5:base-instr" in w and "instr:%s:pr" % profile in w]
        self.assertTrue(together, cp.stdout)

    def test_two_boards_are_two_lanes_and_two_pipelines(self):
        cp, _ = self.dry_run("HEAD", "--release", "2.52", "--builder", "yocto",
                             "--devices", "rpi4-64,rpi5-64",
                             boards={"rpi4": "webkit-2.52-yocto-rpi4-64",
                                     "rpi5": "webkit-2.52-yocto-rpi5-64"})
        self.assertEqual(cp.returncode, 0, cp.stdout)
        first = wave_lines(cp.stdout)[0]
        self.assertEqual(sorted(first), ["image:webkit-2.52-yocto-rpi4-64",
                                         "image:webkit-2.52-yocto-rpi5-64"])

    def test_a_dry_run_creates_no_task_and_runs_nothing(self):
        cp, env = self.dry_run("HEAD", "--release", "2.38", "--devices", "rpi3")
        self.assertIn("dry run -- nothing was built or run", cp.stdout)
        self.assertFalse(os.path.isdir(os.path.join(env["env"]["WK_STORE"], "bench")))


class TestABranchIsASpec(WkTest):
    """`wk ab <owner>:<branch>`: the branch's head is resolved in the mirror
    the way a pull request's is, so nothing has to be turned into a sha by
    hand first."""

    def ab(self, *args, boards=None):
        with ab_env(boards or {"rpi3": "wpewebkit-2.38-buildroot-rpi3-32"}) as env:
            cp = run("ab", *args, env=env["env"], timeout=240)
            return cp, env

    def test_a_forks_branch_is_measured(self):
        cp, env = self.ab("alice:feature-x", "--release", "2.38",
                          "--devices", "rpi3", "--dry-run")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("branch of alice/", cp.stdout)
        self.assertIn(env["head"][:12], cp.stdout)
        self.assertIn("--commit %s --slot pr" % env["head"], cp.stdout)

    def test_a_branch_needs_the_release_named(self):
        """Only a pull request has a base branch to read the image off."""
        cp, _ = self.ab("alice:feature-x", "--devices", "rpi3", "--dry-run")
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("--release is required for a commit or a branch", cp.stdout)

    def test_a_branch_no_repository_of_that_user_has_is_refused_by_name(self):
        cp, _ = self.ab("alice:no-such-branch", "--release", "2.38",
                        "--devices", "rpi3", "--dry-run")
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("no branch 'no-such-branch' under 'alice'", cp.stdout)
        self.assertIn("https://github.com/alice/WebKit.git", cp.stdout)

    def test_a_branch_two_of_the_users_repositories_carry_is_refused(self):
        """WebKit and WPEWebKit are different projects, and a name in both says
        nothing about which one is meant."""
        cp, _ = self.ab("alice:wpe-2.38", "--release", "2.38",
                        "--devices", "rpi3", "--dry-run")
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("more than one of alice's repositories", cp.stdout)


if __name__ == "__main__":
    unittest.main()
