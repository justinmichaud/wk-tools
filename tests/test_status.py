"""`wk status` and the other reports: the walk (lib/wk/status.py), the renderer (lib/wk/statusview.py), the fleet
fan-out (lib/wk/fleet.py), `--log`, the base a workspace tracks, and that a report changes nothing, driven in process
over fakes."""
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes import Fake, FakeClock, FakeDriver, FakeRegistry
from tests.support import BLIND_FLEET, REPO, WkTest, as_dispatched, clean_env, fake_workspace, load_cmd, requires_container_place, run

sys.path.insert(0, str(REPO / "lib"))
from wk import dispatch, fleet, git, places, record, status, statusview  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Local  # noqa: E402
from wk.record import Records  # noqa: E402

CMD = load_cmd("status")


def render(records):
    buf = io.StringIO()
    statusview.render_text_stream(iter(records), buf, False)
    return buf.getvalue()


def machine_rec(name, **extra):
    return dict({"kind": "machine", "name": name}, **extra)


def ws_rec(machine, name, state="running", ws="present", **extra):
    return dict({"kind": "workspace", "machine": machine, "method": "container", "name": name, "state": state, "ws": ws}, **extra)


class TestBump(unittest.TestCase):
    def test_the_worst_wins_and_garbage_reads_4(self):
        self.assertEqual(status.bump(status.bump(status.bump(0, "2"), 1), "0"), 2)
        for c in ("", "abc", "7", "-1", "255"):
            self.assertEqual(status.bump(0, c), 4, c)


class TestWait(unittest.TestCase):
    """`--wait` polls while the answer is busy (2), and another answer ends it only once the next poll repeats it;
    `--timeout` is elapsed time, and ends the wait unanswered."""

    def wait(self, answers, timeout):
        clock, said, it = FakeClock(), [], iter(answers)

        def poll():
            clock.t += 0.5   # a walk costs time of its own
            return next(it)
        return status.wait(poll, timeout, clock, "ws", said.append, said.append), said, clock

    def test_only_a_repeated_answer_other_than_busy_ends_it(self):
        for answers, want in (([0], 0), ([4], 4), ([2, 2, 0, 0], 0), ([2, 4, 2, 0, 0], 0), ([2, 4, 4], 4)):
            rc, said, clock = self.wait(answers, 0)
            self.assertEqual(rc, want, answers)
            self.assertEqual(clock.slept, [status.WAIT_SECONDS] * (len(answers) - 1))

    def test_the_timeout_is_elapsed_time_and_says_the_work_goes_on(self):
        rc, said, clock = self.wait([2] * 100, 10)
        self.assertEqual(rc, 2)
        self.assertLess(clock.now() - FakeClock().now(), 20)
        self.assertIn("still busy after 10s", said[-1])

    def test_the_exit_is_the_waits_verdict_not_the_walk_after_it(self):
        cmd = CMD

        class FlakyWalk:
            worst = 4

            def __init__(self, *a, **kw):
                pass

            def worst_only(self):
                return 4

            def records(self, markers=True):
                return iter(())
        with mock.patch.object(cmd, "Walk", FlakyWalk), mock.patch.object(cmd, "wait", return_value=0), \
                mock.patch.dict(os.environ, {"WK_NAME": "ws"}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, cmd.main(as_dispatched("status", ["--wait", "--records"]), None))
            self.assertEqual(4, cmd.main(as_dispatched("status", ["--records"]), None))


class TaskTest(WkTest):
    def setUp(self):
        super().setUp()
        self.store, self.answers, self.clock = str(self.tmp / "store"), {}, FakeClock()

    def begin(self, where, name, *plan, kind="build", abort=None, log="/nolog"):
        env = {"WK_STORE": self.store, **({"WK_ABORT_SECONDS": str(abort)} if abort else {})}
        return Records(self.store, clock=self.clock, env=env).begin(kind, where, name, "wk %s %s --kill" % (kind, name), log, list(plan))

    def reported(self, only=None):
        return status.task_records(Records(self.store, clock=self.clock, ask_place=lambda n, pid, cap: self.answers.get(n),
                                           env={"WK_STORE": self.store}), only, self.clock)


class TestTasks(TaskTest):
    def test_a_name_reports_its_tasks_and_one_that_ended_as_asked_is_not_reported(self):
        self.begin("here", "ws1", "compile")
        self.begin("here", "ws2", "jsc", kind="test")
        self.assertEqual(sorted(r["name"] for r in self.reported()[0]), ["ws1", "ws2"])
        self.assertEqual([r["name"] for r in self.reported("ws1")[0]], ["ws1"])
        for word, reported in (("0", False), ("cancelled", False), ("refused", False), ("3", True), ("stalled", True)):
            self.begin("here", "ws3", "compile").end(word)
            self.assertEqual(bool(self.reported("ws3")[0]), reported, word)

    def test_a_pid_in_a_workspace_is_asked_of_it_and_reported_on_its_machine(self):
        self.begin("place", "ws4", "compile").pid(4242, "farbox")
        for answer, state, code in ((True, "running", 2), (False, "died", 4), (None, "unanswered", 4)):
            self.answers["ws4"] = answer
            (rec,), worst = self.reported("ws4")
            self.assertEqual((rec["state"], worst, rec["machine"]), (state, code, "farbox"))

    def test_the_record_carries_its_subject_plan_steps_holds_and_kill(self):
        t = self.begin("here", "ws5", "layers", "fetch", "image", kind="yocto")
        t.step(2)
        t.set("subject", "slot base")
        t.set("holds", "ws:ws5\nbuild:here")
        (rec,), _ = self.reported("ws5")
        self.assertEqual((rec["task_kind"], rec["subject"], rec["kill"], rec["holds"]), ("yocto", "slot base", "wk yocto ws5 --kill", "ws:ws5 build:here"))
        self.assertEqual((rec["plan"], rec["steps"], rec["state"]), (["layers", "fetch", "image"], ["done", "running", "pending"], "running"))

    def test_each_verdict_is_its_exit_code(self):
        log = self.tmp / "build.log"
        for end, age, state, code in ((None, 0, "running", 2), (None, 600, "silent", 2), (None, 4000, "silent", 4), (1, 0, "failed", 1),
                                      ("stalled", 0, "stalled", 3), ("oom", 0, "oom", 3), ("gave-up", 0, "gave-up", 1)):
            with self.subTest(end=end, age=age):
                log.write_text("[9/4200] cc\n")
                os.utime(log, (self.clock.now() - age, self.clock.now() - age))
                t = self.begin("here", "w", "compile", abort=1800, log=str(log))
                if end is not None:
                    t.end(end)
                (rec,), worst = self.reported("w")
                self.assertEqual((rec["state"], worst), (state, code))
        self.begin("here", "gone", "compile").pid(4194304)
        self.assertEqual(self.reported("gone")[1], 4)


class TestTheWalk(unittest.TestCase):
    """A place whose own wk answers is asked for its records; one that cannot be asked is a line saying why."""

    def walk(self, *drivers, name=None):
        by_name = {d.name: d for d in drivers}
        reg = FakeRegistry({}, Fake(), lambda n, env: by_name[n], names=list(by_name), ws_place=lambda ws: drivers[0].name)
        w = status.Walk(REPO, name=name, env={"WK_ROW_LABEL": "here"}, reg=reg)
        return [r for r in w.records(markers=False) if r["kind"] != "exit"], w.worst

    def test_an_answering_place_is_asked_for_its_records_under_its_label(self):
        far = [machine_rec("box"), ws_rec("box", "ws", state="absent", ws="absent")]
        box = FakeDriver("box", rc=2, out="".join(json.dumps(r) + "\n" for r in far + [{"kind": "plan", "jobs": []}]))
        box.host = "box.example"
        recs, worst = self.walk(box, name="ws")
        self.assertEqual([r["kind"] for r in recs], ["machine", "machine", "workspace"])
        self.assertEqual(worst, 2)
        args, env, _ = box.asked[0]
        self.assertEqual((args, env["WK_ROW_LABEL"], env["WK_NO_DELEGATE"]), (("status", "--records", "ws"), "box", "1"))

    def test_a_place_that_cannot_be_asked_is_a_line_and_unreachable_is_4(self):
        for side, worst in (("unreachable", 4), ("stopped", 0)):
            d = FakeDriver("container", kind="container", side=side, why="timed out")
            recs, w = self.walk(d)
            self.assertEqual(([r["text"] for r in recs if r["kind"] == "raw"], w),
                             (["container: " + fleet.reason(d, side, "timed out")], worst))


class TestRenderer(unittest.TestCase):
    def test_a_partial_row_a_raw_line_and_an_empty_machine(self):
        out = render([machine_rec("m"), ws_rec("m", "bare"), machine_rec("box"), {"kind": "raw", "machine": "box", "text": "box: unreachable"},
                      machine_rec("empty")])
        self.assertRegex(out, r"(?m)^\s+bare\s+running\s+-\s+\?\s+clean")
        self.assertIn("box: unreachable", out)
        self.assertEqual(out.count("(no workspaces on it)"), 1)

    def test_a_task_is_its_state_steps_and_lines(self):
        t = {"kind": "task", "machine": "m", "task_kind": "build", "name": "ws", "state": "running", "since": "now",
             "plan": ["sync", "compile"], "steps": ["done", "running"], "holds": "ws:ws", "kill": "wk build ws --kill", "log": "/l"}
        out = render([machine_rec("m"), t])
        for want in ("[x] sync", "[>] compile", "holds: ws:ws", "kill: wk build ws --kill", "log:  /l"):
            self.assertIn(want, out)

    def test_a_machine_is_drawn_when_its_last_job_flushes_and_an_unreadable_line_is_skipped(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            recs = list(statusview.records_from_lines(['{"kind":"machine","name":"m"}', "{not json"]))
        self.assertIn("unreadable record", err.getvalue())
        out = io.StringIO()
        stream = [{"kind": "plan", "jobs": [{"job": "a", "machine": "m"}, {"job": "b", "machine": "m"}]}] + recs + [{"kind": "flush", "job": "a"}]
        statusview.render_text_stream(iter(stream), out, False)
        self.assertEqual(out.getvalue().count("\nm\n"), 1)


class TestHelpLeadsWithTheSelfLine(unittest.TestCase):
    def test_help_leads_with_role_and_mode(self):
        out = io.StringIO()
        with mock.patch.object(dispatch.os, "isatty", return_value=False), contextlib.redirect_stdout(out), \
                self.assertRaises(dispatch.Exit):
            dispatch.help_doc("")
        self.assertEqual(out.getvalue().splitlines()[0], fleet.self_line(str(REPO), os.environ))


FAILED = "Building place foo\nfoo.cpp:10:5: error: use of undeclared identifier 'x'\nninja: build stopped: subcommand failed.\n"
CLEAN = "Building place foo\n-- no error: handling here, everything is fine\nninja: no work to do.\n"


def status_cmd(env, *argv):
    """`wk status <argv>` in process under `env`: (exit, everything it printed)."""
    out = io.StringIO()
    with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            rc = CMD.main(as_dispatched("status", list(argv)), None)
        except Refused as e:
            rc = e.status
    return rc, out.getvalue()


class TestLogsPicksTheRightFile(WkTest):
    """`wk status <ws> --log`, as the machine that holds the store: build.log, else the image stage log its builder
    wrote under home/, and the errors it names."""

    def _run(self, name, files):
        """`files` is {path under the workspace's directory: text}; the output, and whether it exited 0."""
        wsdir = self.tmp / "ws" / name
        wsdir.mkdir(parents=True)
        for rel, text in files.items():
            (wsdir / rel).parent.mkdir(parents=True, exist_ok=True)
            (wsdir / rel).write_text(text)
        env = {"WK_NAME": name, "WK_PLACE": "container", "WK_IN_VM": "1", "WK_STORE": str(self.tmp)}
        rc, out = status_cmd(env, "--log")
        return out, rc == 0

    def test_which_log_is_shown_and_what_it_says(self):
        for name, files, ok, shown, hidden in (
                ("yoctows", {"home/yocto-image.log": "the yocto stage output\n"}, True,
                 ("the yocto stage output", "home/yocto-image.log"), ()),
                ("buildrootws", {"home/buildroot-image.log": "the buildroot stage output\n"}, True,
                 ("the buildroot stage output", "home/buildroot-image.log"), ()),
                ("bothws", {"home/yocto-image.log": "stale image-stage output\n", "build.log": "ninja: building places\n"}, True,
                 ("ninja: building places",), ("stale image-stage output",)),
                ("emptyws", {}, False, ("wk build", "wk sysimage build"), ()),
                ("badws", {"build.log": FAILED}, True, ("ninja: build stopped",), ("(none)",))):
            with self.subTest(name):
                out, rc_ok = self._run(name, files)
                self.assertEqual(rc_ok, ok, out)
                for text in shown:
                    self.assertIn(text, out)
                for text in hidden:
                    self.assertNotIn(text, out)

    def test_a_message_containing_the_word_error_mid_sentence_is_not_reported(self):
        out, ok = self._run("goodws", {"build.log": CLEAN})
        self.assertTrue(ok, out)
        self.assertIn("(none)", out, out)
        self.assertNotIn("no error: handling here", out.split("errors:", 1)[1].split("last output:", 1)[0], out)


GOOD_BROWSER = {
    "accelerator": "IOAccelerator", "dpr": 2, "focused": True, "raf_hz": 59.7,
    "renderer": "Apple M1 Pro", "screen": [1512, 982], "webgl": "WebGL 2.0",
    "webkit_gpu_clients": {"913": "com.apple.WebKit.GPU"},
}
LIBRARIES = ("JavaScriptCore", "WebCore", "WebKit")
BENCHMARKS = ("speedometer3", "jetstream3", "motionmark")
GOOD_PROFILE = {
    "profile_dir": "/Users/wk/pgo", "arch": "arm64", "missing": [],
    "combined": {lib: {"total_functions": 40000, "maximum_function_count": 900000}
                 for lib in LIBRARIES},
    "compressed": {lib: 1234567 for lib in LIBRARIES},
    "benchmarks": {b: {lib: {"total_functions": 24000, "maximum_function_count": 4200}
                       for lib in LIBRARIES} for b in BENCHMARKS},
}


class TestTheReadingsTravelWithTheBuild(WkTest):
    """What `wk status <ws> --log --gates` says about the readings a build left beside its products."""

    def _gates(self, browser=None, profile=None, pins=None):
        with fake_workspace() as ws:
            products = ws.ws_dir / "WebKit" / "WebKitBuild" / "Release-mac-release-pgo"
            products.mkdir(parents=True)
            if browser is not None:
                (products / "wk-browser-check.json").write_text(json.dumps(browser))
            if profile is not None:
                (products / "wk-profile-check.json").write_text(json.dumps(profile))
            if pins is not None:
                (products / "wk-payload-pins").write_text(pins)
            return status_cmd(ws.env({"WK_NAME": "selftest-ws", "WK_PLACE": "local"}), "--log", "--gates")

    def test_a_build_with_no_readings_says_which_configs_have_them(self):
        rc, out = self._gates()
        self.assertEqual(rc, 0, out)
        self.assertIn("no readings under", out, out)

    def test_the_readings_are_shown_from_beside_the_products(self):
        rc, out = self._gates(browser=GOOD_BROWSER, profile=GOOD_PROFILE,
                              pins="speedometer3 9f3c1a\nmotionmark 44ab02\n")
        self.assertEqual(rc, 0, out)
        self.assertIn("raf_hz=59.7", out, out)
        self.assertIn("JavaScriptCore: functions=40000", out, out)
        self.assertIn("speedometer3 9f3c1a", out, out)

    def test_a_reading_that_did_not_justify_a_measurement_says_so_again(self):
        bad = dict(GOOD_BROWSER, raf_hz=8.0, webgl=None, webkit_gpu_clients={})
        rc, out = self._gates(browser=bad)
        self.assertEqual(rc, 0, out)
        self.assertIn("this window is throttled", out, out)
        self.assertIn("no WebGL context", out, out)


def _plan(*jobs):
    """<(job, machine)...>: a job with no machine completes nobody's block."""
    return dict(kind="plan", jobs=[{"job": j, "machine": m} if m else {"job": j} for j, m in jobs])


def _machine_lines(name):
    """One job's records: its machine, one workspace, and the flush that ends it."""
    return [dict(kind="machine", name=name, self=(name == "alpha")),
            dict(kind="workspace", machine=name, method="container", name="ws-" + name, state="present", ws="present"),
            dict(kind="flush", job=name)]


class _Tap:
    """The renderer's output, each line stamped with how many records it had consumed when the line appeared."""

    def __init__(self, records):
        self.records, self.consumed, self.lines = records, 0, []

    def feed(self):
        for r in self.records:
            self.consumed += 1
            yield r

    def write(self, text):
        for line in text.rstrip("\n").split("\n"):
            self.lines.append((self.consumed, line))

    def flush(self):
        pass

    def when(self, predicate):
        for consumed, line in self.lines:
            if predicate(line):
                return consumed
        raise AssertionError("no line matched:\n" + "\n".join(l for _, l in self.lines))


def _render(records):
    tap = _Tap(records)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        statusview.render_text_stream(tap.feed(), tap, False)
    return tap, err.getvalue()


class TestTextStreamsAsRecordsArrive(unittest.TestCase):
    def test_first_machine_prints_before_second_machines_records_are_consumed(self):
        records = [_plan(("alpha", "alpha"), ("beta", "beta"))] + _machine_lines("alpha") + _machine_lines("beta") + [dict(kind="exit", code=0)]
        tap, err = _render(records)
        alpha_at = tap.when(lambda l: l.startswith("alpha"))
        self.assertLessEqual(alpha_at, 4, "alpha's block waited for beta's records -- not streaming")
        self.assertGreater(tap.when(lambda l: l.startswith("beta")), 4)
        self.assertEqual(err, "")

    def test_a_planned_machine_shows_as_probing_before_it_answers(self):
        records = [_plan(("slowbox", "slowbox"))] + _machine_lines("slowbox") + [dict(kind="exit", code=0)]
        tap, _ = _render(records)
        self.assertEqual(tap.when(lambda l: "probing slowbox" in l), 1)
        self.assertGreater(tap.when(lambda l: l.strip() == "slowbox"), 1)

    def test_a_delegated_machines_heading_comes_before_its_rows_even_when_its_rows_lead_or_its_job_never_flushes(self):
        for records in ([_plan(("far", "far")), dict(kind="workspace", machine="far", method="native", name="ws-far", state="present", ws="present"),
                         dict(kind="machine", name="far"), dict(kind="flush", job="far"), dict(kind="exit", code=0)],
                        [_plan(("far", "far")), dict(kind="machine", name="far"),
                         dict(kind="workspace", machine="far", method="native", name="ws-far", state="present", ws="present"),
                         dict(kind="exit", code=0)]):
            tap, _ = _render(records)
            out = [l for _, l in tap.lines]
            heading = next(i for i, l in enumerate(out) if l.strip() == "far")
            self.assertLess(heading, next(i for i, l in enumerate(out) if "ws-far" in l), "\n".join(out))


class TestABlockWaitsForEveryPlannedJob(unittest.TestCase):
    """A macOS host feeds one machine from two jobs, the podman VM's container place and the tart vm place."""

    def test_the_first_job_flushing_empty_does_not_draw_the_machine(self):
        tap, err = _render([
            _plan(("container", "host"), ("vm", "host")), dict(kind="machine", name="host", self=True), dict(kind="flush", job="container"),
            dict(kind="machine", name="host", self=True),
            dict(kind="workspace", machine="host", method="macOS guest", name="ws-v", state="stopped", ws="present"),
            dict(kind="flush", job="vm"), dict(kind="exit", code=0)])
        out = "\n".join(l for _, l in tap.lines)
        self.assertEqual(err, "")
        self.assertEqual(len([l for l in out.splitlines() if l.startswith("host ")]), 1, out)
        self.assertIn("ws-v", out)
        self.assertNotIn("no workspaces", out)
        self.assertLess(out.index("probing vm"), out.index("host "))

    def test_a_flush_for_a_job_outside_the_plan_is_reported_and_draws_nothing(self):
        tap, err = _render([
            _plan(("container", "host")), dict(kind="machine", name="host", self=True), dict(kind="flush", job="vm"),
            dict(kind="workspace", machine="host", method="container", name="ws-c", state="present", ws="present"),
            dict(kind="flush", job="container"), dict(kind="exit", code=0)])
        out = "\n".join(l for _, l in tap.lines)
        self.assertIn("'vm' ended without being in the plan", err)
        self.assertIn("ws-c", out)
        self.assertNotIn("no workspaces", out)


class TestJsonModeUnchangedByStreamMarkers(unittest.TestCase):
    def test_json_output_equals_the_merge_of_the_same_stream_without_markers(self):
        records = ([_plan(("alpha", "alpha"), ("beta", "beta"))] + _machine_lines("alpha") + _machine_lines("beta")
                   + [dict(kind="exit", code=2)])
        with_markers = statusview.merge(records)
        without = statusview.merge([r for r in records if r["kind"] not in ("plan", "flush")])
        self.assertEqual(with_markers, without)
        self.assertEqual(with_markers["exit"], 2)
        self.assertEqual([m["name"] for m in with_markers["machines"]], ["alpha", "beta"])
        self.assertEqual(json.loads(json.dumps(with_markers)), with_markers)


class TestEveryWorkspaceIsInTheListing(WkTest):
    @requires_container_place()
    def test_bare_status_lists_every_workspace_ls_lists(self):
        ls = run("ls", "--json", timeout=120)
        self.assertEqual(ls.returncode, 0, ls.stdout)
        names = [w["name"] for w in json.loads(ls.stdout)["workspaces"]]
        st = run("status", env={"NO_COLOR": "1"}, timeout=180)
        self.assertIn(st.returncode, (0, 2, 4), st.stdout)
        for n in names:
            self.assertRegex(st.stdout, r"(?m)^\s+%s\s" % n, f"'{n}' missing from a bare 'wk status':\n{st.stdout}")


ESC = "\033["


def _render_coloured(colour):
    records = [
        {"kind": "machine", "name": "buildbox4"},
        {"kind": "workspace", "machine": "buildbox4", "method": "container", "name": "demo",
         "state": "running", "branch": "main", "base": "main"},
        {"kind": "exit", "code": 0},
    ]
    out = io.StringIO()
    statusview.render_text_stream(iter(records), out, colour)
    return out.getvalue()


class TestColourDecision(unittest.TestCase):
    def test_only_a_terminal_without_no_color_gets_colour(self):
        for tty, env, want in ((True, {}, True), (True, {"NO_COLOR": "1"}, False), (False, {}, False),
                               (False, {"NO_COLOR": "1"}, False)):
            with self.subTest(tty=tty, env=env):
                self.assertEqual(statusview.colour_wanted(types.SimpleNamespace(isatty=lambda: tty), env), want)

    def test_the_renderer_honours_the_flag(self):
        self.assertIn(ESC, _render_coloured(True))
        self.assertNotIn(ESC, _render_coloured(False))


def fleet_registry(drivers):
    by_name = {d.name: d for d in drivers}
    return FakeRegistry({}, Fake(), lambda name, env: by_name[name], names=list(by_name))


def answering(name, rc, *rows):
    d = FakeDriver(name, rc=rc, out="".join(json.dumps(r) + "\n" for r in [machine_rec(name), *rows, {"kind": "exit", "code": rc}]))
    d.host = name + ".tailnet"
    return d


def wk_status(drivers, *argv, host="here"):
    """`wk status <argv>` through the dispatcher into a pipe, typed on workstation `host` of a fleet of `drivers`: (exit, output)."""
    by, out = {d.name: d for d in drivers}, io.StringIO()
    with mock.patch.dict(os.environ, clean_env({"WK_IN_VM": "1"}), clear=True), \
            mock.patch.object(dispatch, "_registry", None), mock.patch.object(record, "host_name", return_value=host), \
            mock.patch.object(places, "Registry", lambda *a, **kw: FakeRegistry(dict(os.environ), Fake(), lambda n, e: by[n], names=list(by))), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            dispatch.main(["status", *argv])
        except Refused as e:
            return e.status, out.getvalue()


class TestTheFleetViewIsOne(unittest.TestCase):
    """`unit status.fleet_is_one`, less the clause a decision is owed for.
    TODO: a name alive on two machines is a conflict `--on` disambiguates."""

    def test_the_exit_is_the_worst_state_found_anywhere_in_every_mode(self):
        fleet = [answering("alpha", 1, ws_rec("alpha", "x", state="failed")), answering("beta", 3, ws_rec("beta", "y"))]
        torn = fleet + [answering("gamma", 0, ws_rec("gamma", "z"), ws_rec("gamma", "z", state="absent", ws="absent"))]
        for argv in ([], ["--json"], ["--records"], ["--wait"]):
            self.assertEqual((wk_status(fleet, *argv)[0], wk_status(torn, *argv)[0]), (3, 4), argv)

    def test_two_workstations_reaching_one_box_see_one_state_and_a_disagreement_names_both_views(self):
        box = answering("box", 2, ws_rec("box", "ws"))
        left, right = (json.loads(wk_status([box], "--json", host=h)[1]) for h in ("left", "right"))
        self.assertEqual((left, {(a, e["WK_ROW_LABEL"]) for a, e, _ in box.asked}), (right, {(("status", "--records"), "box")}))
        rc, out = wk_status([answering("box", 2, ws_rec("box", "ws"), ws_rec("box", "ws", state="absent", ws="absent"))])
        self.assertEqual((rc, re.findall(r"disagree \(\w+ vs \w+\)", out)), (4, ["disagree (running vs absent)"]))
        self.assertNotIn(ESC, out, "a pipe is no terminal, so the text has no colour")


class TestEach(unittest.TestCase):
    def test_each_place_is_asked_for_its_own_rows_under_its_label(self):
        box, vm = FakeDriver("box", out="row\r\n"), FakeDriver("container", kind="container")
        answers = fleet.each(fleet_registry([box, vm]), ["ls", "--json"])
        self.assertEqual([(a.name, a.rc, a.out, a.why) for a in answers], [("box", 0, "row\n", ""), ("container", 0, "", "")])
        args, env, quiet = box.asked[0]
        self.assertEqual((args, env["WK_ROW_LABEL"], env["WK_NO_DELEGATE"], quiet), (("ls", "--json"), "box", "1", True))
        self.assertNotIn("WK_ROW_LABEL", vm.asked[0][1], "the podman machine labels its rows as this machine's itself")

    def test_a_place_not_asked_says_why_and_one_with_no_far_side_is_the_callers(self):
        drivers = [FakeDriver("here", side="none"), FakeDriver("far", side="unreachable", why="timed out"),
                   FakeDriver("ctr", kind="container", side="stopped"), FakeDriver("bare", side="no-wk")]
        answers = fleet.each(fleet_registry(drivers), ["status"])
        self.assertEqual([(a.side, a.rc) for a in answers], [("none", None), ("unreachable", None), ("stopped", None), ("no-wk", None)])
        self.assertEqual([a.why for a in answers], ["", "unreachable over ssh: timed out",
                                                    "the podman machine 'wk' is stopped -- 'wk start' brings it up",
                                                    "no wk-tools there yet -- 'wk machine setup bare'"])
        self.assertEqual([d.asked for d in drivers], [[], [], [], []])

    def test_an_unknown_place_is_an_answer_naming_it(self):
        reg = fleet_registry([])
        a, = fleet.each(reg, ["status"], names=["ghost"])
        self.assertEqual((a.side, a.driver), ("unknown", None))
        self.assertIn("ghost", a.why)


class TestListing(unittest.TestCase):
    def test_rows_of_every_answering_place_and_a_warning_for_each_other(self):
        warned = []
        drivers = [FakeDriver("box", out="a\n\nb\n"), FakeDriver("old", rc=2), FakeDriver("far", side="unreachable"),
                   FakeDriver("here", side="none")]
        self.assertEqual(fleet.listing(fleet_registry(drivers), ["bench", "ls", "--continued"], "tasks", warned.append), ["a", "b"])
        self.assertEqual(len(warned), 2)
        self.assertIn("'old' did not answer the listing", warned[0])
        self.assertIn("far: unreachable over ssh, so the tasks", warned[1])


class TestResolution(unittest.TestCase):
    def registry(self, names, holding, ws):
        fake = Fake()
        fake.answer(["podman", "inspect"], rc=125)

        def make(name, env):
            return places.Container(name, str(REPO), dict(env, WK_STORE="/store/" + name, WK_IN_VM="1"), fake)
        reg = FakeRegistry({}, fake, make, names=names)
        for name in holding:
            fake.dirs.add(reg.load(name).store.ws_dir(ws))
        return reg

    def test_a_name_on_two_places_refuses_naming_both(self):
        with self.assertRaises(LookupError) as e:
            self.registry(["alpha", "beta", "gamma"], {"alpha", "beta"}, "demo").ws_place("demo")
        self.assertIn("alpha beta", str(e.exception))
        self.assertNotIn("gamma", str(e.exception))

    def test_a_name_on_one_place_resolves(self):
        self.assertEqual(self.registry(["alpha", "beta"], {"beta"}, "demo").ws_place("demo"), "beta")


CONFIGS = REPO / "image" / "presets"


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo(repo):
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "a@example.invalid")
    _git(repo, "config", "user.name", "wk-tools test")
    (repo / "f").write_text("one\n")
    _git(repo, "add", "f")
    _git(repo, "commit", "-qm", "init")


def upstream_line(repo):
    """The probe `wk ls` and `wk status` run inside a workspace, run in `repo`."""
    return git.probe(Local().run, str(repo))["wsbase"]


class TestUpstreamLine(unittest.TestCase):
    """The base a workspace tracks: the upstream WebKit line a checkout's HEAD descends from (git.PROBE's `wsbase`),
    against real disposable repositories."""

    def setUp(self):
        self.repo = self.new_repo()

    def new_repo(self):
        tmp = tempfile.TemporaryDirectory(prefix="wk-base-")
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name) / "r"
        _init_repo(repo)
        return repo

    def test_the_line_a_tracked_branch_names(self):
        for branch, remote, ref, want in (("main", "origin", "main", "main"), ("work", "wpe", "webkitglib/2.52", "2.52"),
                                          ("eng/stringimpl-2.38", "fork", "eng/stringimpl-2.38", "?")):
            with self.subTest(ref=ref):
                repo = self.new_repo()
                _git(repo, "checkout", "-qB", branch)
                _git(repo, "remote", "add", remote, "https://example.invalid/%s.git" % remote)
                _git(repo, "update-ref", "refs/remotes/%s/%s" % (remote, ref), "HEAD")
                _git(repo, "branch", "--set-upstream-to=%s/%s" % (remote, ref), branch)
                self.assertEqual(upstream_line(repo), want)

    def test_detached_with_nothing_reachable_is_unknown(self):
        _git(self.repo, "checkout", "-q", "--detach", "HEAD")
        self.assertEqual(upstream_line(self.repo), "?")

    def test_detached_but_reachable_from_two_releases_picks_the_newer(self):
        repo = self.repo
        base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        (repo / "f").write_text("two\n")
        _git(repo, "commit", "-qam", "second")
        _git(repo, "update-ref", "refs/remotes/origin/webkitglib/2.46", base)
        (repo / "f").write_text("three\n")
        _git(repo, "commit", "-qam", "third")
        _git(repo, "update-ref", "refs/remotes/origin/webkitglib/2.52", "HEAD")
        _git(repo, "checkout", "-q", "--detach", base)
        self.assertEqual(upstream_line(repo), "2.52")


class TestImageBase(unittest.TestCase):
    """An image workspace's base is its preset's own CFG_RELEASE, read from the real conf this checkout ships."""

    def test_an_image_workspace_reads_its_presets_release(self):
        for kind, preset in (("buildroot", "webkit-2.52-buildroot-rpi3-32"), ("yocto", "wpewebkit-2.46-yocto-rpi3-32")):
            with self.subTest(kind=kind):
                release = re.search(r"^CFG_RELEASE=(\S+)", (CONFIGS / (preset + ".conf")).read_text(), re.M).group(1)
                self.assertEqual(places.image_base(str(REPO), "%s-%s" % (kind, preset)), release)

    def test_a_plain_checkout_name_is_not_an_image_workspace(self):
        self.assertIsNone(places.image_base(str(REPO), "stringimpl238"))
        self.assertIsNone(places.image_base(str(REPO), "yocto-no-such-preset"))


class TestJsonCarriesTheBase(unittest.TestCase):
    """One merged document feeds every view: a field present in text is present in json."""

    def test_workspace_base_survives_into_json(self):
        recs = [machine_rec("moose"), dict(kind="workspace", machine="moose", method="container", name="stringimpl238", state="running",
                                           ws="present", branch="eng/stringimpl-2.38", base="?")]
        self.assertIn("stringimpl238", render(recs))
        self.assertEqual(statusview.merge(recs)["machines"][0]["methods"][0]["workspaces"][0]["base"], "?")


REPORTS = (("status",), ("ls",), ("status", "nosuch", "--log"), ("doctor",))
PLACES = {"store": {"WK_IN_VM": "1"}, "workstation": {}}
TOOLS = ("ssh", "scp", "rsync", "podman", "tart", "tailscale", "sudo", "systemctl", "launchctl", "gh", "nmap")
# A tool followed by one of these words acts; the power verbs and the copies act whatever follows.
ACTS = {"podman": {"start", "run", "create", "rm", "rmi", "stop", "kill", "restart", "pull", "build", "init", "exec", "set"},
        "tart": {"run", "clone", "set", "stop", "delete", "pull", "push", "create", "import", "prune"},
        "tailscale": {"up", "down", "set", "login", "logout", "switch", "cert"},
        "systemctl": {"start", "stop", "restart", "enable", "disable", "kill", "reboot", "poweroff", "daemon-reload"},
        "launchctl": {"load", "unload", "bootstrap", "bootout", "kickstart", "start", "stop", "enable", "disable"}}
ALWAYS = {"reboot", "shutdown", "poweroff", "scp", "rsync"}
HELPERS = {"wk-boot-priv", "wk-card-priv", "wk-quiesce-priv"}   # the privileged helpers: `status` is their one read


def readonly_verbs():
    from wk import decl
    return {name for name, m in decl.commands(REPO) if any(t.readonly for _, t, _ in decl.variants(name, m.COMMAND))}


def acts_in(tokens, readonly):
    """The first call in `tokens` that acts, or "": a tool followed within two words by an acting verb, a wk that is
    not asked a readonly verb, or a privileged helper asked anything but its status."""
    for i, tok in enumerate(tokens):
        tool, rest = os.path.basename(tok), tokens[i + 1:]
        verbs = [a for a in rest if not a.startswith("-")][:2]
        if tool in ALWAYS or (ACTS.get(tool, set()) & set(verbs)):
            return " ".join(tokens[i:i + 4])
        if tok.endswith("/wk") and (verbs[:1] or [""])[0] not in readonly:
            return " ".join(tokens[i:i + 3])
        if tool in HELPERS and verbs[:1] != ["status"]:
            return " ".join(tokens[i:i + 3])
    return ""


def acts(argv, readonly):
    """What in one recorded call acts, or "": an ssh's far command line is read word by word, whatever quoting it is in."""
    if argv[0] == "ssh":
        return "" if "-G" in argv else acts_in(re.findall(r"[A-Za-z0-9_./:@=+-]+", " ".join(argv[1:])), readonly)
    return acts_in(argv, readonly)


def tree(root):
    out = {}
    for d, _, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns)
    return out


class TestTheClassifier(unittest.TestCase):
    def test_it_names_a_start_a_boot_and_a_far_mutation_and_passes_a_read(self):
        ro = {"status", "ls"}
        for argv in (["podman", "machine", "start", "wk"], ["tart", "run", "wk-a"], ["sudo", "reboot"], ["sudo", "-n", "/usr/local/sbin/wk-boot-priv", "reboot"],
                     ["ssh", "-o", "BatchMode=yes", "box", "cd /x && ./wk build a"], ["ssh", "box", "PATH=/x; tart clone a b"],
                     ["ssh", "box", "sh -c 'systemctl restart wk-proxy'"], ["tailscale", "up"]):
            self.assertTrue(acts(argv, ro), argv)
        for argv in (["podman", "machine", "inspect", "wk"], ["tart", "list", "--format", "json"], ["ssh", "-G", "box"],
                     ["ssh", "box", "cd /x && ./wk status --continued"], ["sudo", "-n", "-l"], ["tailscale", "status", "--json"]):
            self.assertFalse(acts(argv, ro), argv)


class TestReportsAreReadOnly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk-test-readonly-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = os.path.join(self.tmp, "calls")
        shims = os.path.join(self.tmp, "bin")
        os.makedirs(shims)
        for tool in TOOLS:
            p = os.path.join(shims, tool)
            with open(p, "w") as f:
                f.write('#!/bin/sh\n{ printf "%%s\\0" %s "$@"; printf "\\n"; } >> %s\nexit 97\n' % (tool, shlex.quote(self.log)))
            os.chmod(p, 0o755)
        fleet = os.path.join(self.tmp, "fleet")
        os.makedirs(fleet)
        for conf in os.listdir(BLIND_FLEET):
            os.symlink(os.path.join(BLIND_FLEET, conf), os.path.join(fleet, conf))
        with open(os.path.join(fleet, "farbox.conf"), "w") as f:
            f.write("kind=build\ndriver=remote\nhost=farbox\n")
        self.watched = {k: os.path.join(self.tmp, k) for k in ("home", "state", "store", "secrets")}
        for d in self.watched.values():
            os.makedirs(d)
        self.env = {"PATH": shims + os.pathsep + os.environ["PATH"], "WK_MACHINES_DIR": fleet, "HOME": self.watched["home"],
                    "XDG_STATE_HOME": self.watched["state"], "WK_STORE": self.watched["store"],
                    "WK_HOST_SECRETS": self.watched["secrets"]}
        self.readonly = readonly_verbs()

    def calls(self):
        try:
            with open(self.log) as f:
                return [line.rstrip("\0").split("\0") for line in f.read().split("\0\n") if line]
        except OSError:
            return []

    def test_every_report_is_declared_readonly(self):
        self.assertLessEqual({r[0] for r in REPORTS}, self.readonly)

    def test_a_report_starts_boots_and_repairs_nothing_here_or_on_the_far_machine(self):
        for place, extra in PLACES.items():
            for argv in REPORTS:
                with self.subTest(place=place, cmd=argv[0]):
                    open(self.log, "w").close()
                    before = {k: tree(d) for k, d in self.watched.items()}
                    run(*argv, env=dict(self.env, **extra), timeout=60)
                    after = {k: tree(d) for k, d in self.watched.items()}
                    self.assertEqual([a for a in map(lambda c: acts(c, self.readonly), self.calls()) if a], [])
                    self.assertEqual(before, after, "wk %s wrote under the scratch %s" % (argv[0], place))


if __name__ == "__main__":
    unittest.main()
