"""Benchmark *tasks*: the unit `wk ab`, `wk pi bench` and `wk bench` produce
and `wk bench ls`, `wk bench report` and `wk status` speak in
(lib/wk/bench/record.py; `wk bench`'s verbs in lib/wk/bench/cli.py).

A synthetic task -- task.json plus runs whose env.json carries the round and
arm `wk pi bench --ab` records -- read in-process against a scratch store and
a fake registry: no board, no workspace, no machine asked. The fleet walk is
driven through fake targets; two tests go through ./wk to hold the
declaration (`ls` runs here, reads this store and starts nothing).

Run: python3 -m unittest tests.test_bench_task -v
"""
import json
import subprocess
import sys
import unittest

from tests.support import REPO, WkTest, bash, clean_env, run, scratch_dir, temp_store
from tests.test_bench_report import in_process

sys.path.insert(0, str(REPO / "lib"))
from wk.act import Refused  # noqa: E402
from wk.bench import cli, record, report  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.lock import Lock  # noqa: E402
from wk import record as task_record  # noqa: E402
from wk.machine import Local  # noqa: E402
from wk.store import Store  # noqa: E402

TASK = "20260830T120000Z-wpe-pr1725"


def make_task(bench_dir, rounds=3, slots=("base", "pr1725"), name=TASK):
    d = bench_dir / name
    record.task_write(str(d), [
        f"task={name}", "requested=2026-08-30T12:00:00Z",
        "subject.kind=pull", "subject.spec=wpe:1725",
        "subject.head=afa2ed9e70103469f02732323fa7b874378d6a5d",
        "subject.base=04abe09851b03b7ce6c8b9c208659c23a5ab7138",
        "devices=rpi3=wpewebkit-2.38-buildroot-rpi3-32", "plans=speedometer2.1",
        f"rounds={rounds}", "slots=" + ",".join(slots), "count=1", "timeout=1200"],
        ["wk pi deploy wpewebkit-2.38-buildroot-rpi3-32 rpi3 --slot base",
         f"wk pi bench rpi3 speedometer2.1 --ab base,pr1725 --rounds {rounds} --task {name}"])
    return d


def add_run(task_dir, slot, rnd, arm, outcome, vals=(100.0, 102.0, 99.0)):
    """outcome: ok (result.json), failed (ended, no result), running (neither)."""
    d = task_dir / "runs" / f"20260830T12{rnd:02d}{'00' if arm == 'a' else '30'}Z-speedometer2.1-rpi3-{slot}"
    d.mkdir(parents=True)
    record.write_env(str(d / "env.json"), [
        "plan=speedometer2.1", "config=wpewebkit-2.38-buildroot-rpi3-32", "machine=rpi3",
        f"build_slot={slot}", "webkit_sha=abcdef1234567890", "runner=browser", "arch=armv7l",
        "bench_host=image", f"task={task_dir.name}", f"ab.round={rnd}", f"ab.arm={arm}",
        "ab.slot_a=base", "ab.slot_b=pr1725"])
    if outcome != "running":
        record.write_env(str(d / "env.json"), ["wall_time_s=400"], update=True)
    if outcome == "ok":
        (d / "result.json").write_text(json.dumps({"Speedometer-2": {"tests": {"Elm-TodoMVC": {
            "metrics": {"Time": {"Total": {"current": list(vals)}}}}}}}))
    if outcome == "running":
        (d / "run.log").write_text("INFO - Start the iteration 1 of 1 for current benchmark\n")
    return d


def status(d, running=False):
    return dict(l.split("=", 1) for l in record.status_lines(record.task_state(str(d), running)))


class FakeTarget:
    def __init__(self, name, kind="remote", is_local=False, side="answering", rc=0, out=""):
        self.name, self.kind, self.is_local = name, kind, is_local
        self.side, self.rc, self.out = side, rc, out
        self.asked = []

    def probe(self):
        return self.side, ""

    def wk(self, *args, env=None, quiet=False):
        self.asked.append((args, dict(env or {}), quiet))
        return self.rc, self.out


class FakeRegistry:
    """What cli.Bench and record.Listing ask of targets.Registry: a store, the walk, and each target."""

    def __init__(self, store_dir, targets=(), env=None):
        self.env = dict(env or {}, WK_STORE=str(store_dir), WK_LOCK_DIR=str(store_dir / "locks"))
        self.store = Store(self.env)
        self.machine = Local()
        self.targets = {t.name: t for t in targets}

    def walk(self):
        return list(self.targets)

    def load(self, name):
        if name not in self.targets:
            raise LookupError("unknown target '%s'" % name)
        return self.targets[name]

    def ws_target(self, ws):
        return {"cws": "container"}.get(ws, "vm")


def bench(store_dir, targets=(), env=None):
    return cli.Bench(REPO, FakeRegistry(store_dir, targets, dict(env or {}, WK_ROW_LABEL="here")), FakeClock())


class TestTaskState(WkTest):
    """planned, ended, usable, complete: recomputed from task.json and the
    runs; running is what the caller says the lock says."""

    def test_a_fresh_task_is_incomplete_with_nothing_run(self):
        with scratch_dir() as tmp:
            st = status(make_task(tmp))
            self.assertEqual(st["state"], "incomplete")
            self.assertEqual(st["planned"], "6")
            self.assertEqual(st["ended"], "0")
            self.assertIn("wpe:1725", st["subject"])
            self.assertIn("3 rounds", st["subject"])

    def test_running_is_the_lock_not_the_files(self):
        with scratch_dir() as tmp:
            d = make_task(tmp)
            add_run(d, "base", 1, "a", "ok")
            add_run(d, "pr1725", 1, "b", "running")
            st = status(d, running=True)
            self.assertEqual(st["state"], "running")
            self.assertIn("now speedometer2.1 rpi3 pr1725", st["summary"])
            self.assertIn("iteration 1/1", st["summary"])
            # The same files with no lock: a driver that died mid-run.
            st = status(d)
            self.assertEqual(st["state"], "incomplete")
            self.assertIn("died with their driver", st["summary"])

    def test_complete_when_every_planned_run_ended_even_failed_ones(self):
        with scratch_dir() as tmp:
            d = make_task(tmp, rounds=2)
            add_run(d, "base", 1, "a", "ok"); add_run(d, "pr1725", 1, "b", "ok")
            add_run(d, "base", 2, "a", "failed"); add_run(d, "pr1725", 2, "b", "ok")
            st = status(d)
            self.assertEqual(st["state"], "complete")
            self.assertEqual(st["ended"], "4")
            self.assertEqual(st["failed"], "1")
            self.assertEqual(st["usable"], "1", "a round with one failed arm is not usable")

    def test_task_write_refuses_a_task_missing_its_shape(self):
        with scratch_dir() as tmp:
            with self.assertRaises(SystemExit) as e:
                record.task_write(str(tmp / "t"), ["task=t", "requested=now", "plans=p", "slots=a"], [])
            self.assertIn("devices is required", str(e.exception.code))

    def test_a_directory_without_task_json_is_not_a_task(self):
        with scratch_dir() as tmp:
            (tmp / "junk").mkdir()
            with self.assertRaises(SystemExit) as e:
                record.task_state(str(tmp / "junk"), False)
            self.assertIn("no task.json", str(e.exception.code))

    def test_the_cli_bash_callers_use_answers_the_same(self):
        """`wkdata task-write` / `task-status`: what bench_task_new and the Mac lane call."""
        with scratch_dir() as tmp:
            d = tmp / "t"
            wk = [sys.executable, str(REPO / "lib" / "wkdata.py")]
            cp = subprocess.run(wk + ["task-write", str(d), "task=t", "requested=now", "devices=rpi3=p",
                                      "plans=p", "rounds=1", "slots=a,b", "--command", "wk x"],
                                capture_output=True, text=True, timeout=30)
            self.assertEqual(cp.returncode, 0, cp.stderr)
            cp = subprocess.run(wk + ["task-status", str(d)], capture_output=True, text=True, timeout=30)
            self.assertIn("planned=2", cp.stdout, cp.stderr)
            self.assertIn("subject=a vs b", cp.stdout)


class TestOneRecord(WkTest):
    """`unit bench.one_record[container]`: a workspace run's env.json, written
    by the one writer with its provenance, is all `wk bench ls` and the report
    read -- nothing else in the run directory is consulted."""

    FIELDS = ["plan=jetstream3", "workspace=w", "config=jsc-release", "class=cpu", "runner=jsc",
              "arch=native", "bench_host=container", "webkit_sha=0123456789abcdef", "count=4",
              "host.kernel=6.8.0", "host.kernel_arch=x86_64", "host.cores=16",
              "host.root_device=nvme0n1 (nvme, ssd, trim)", "cores.set=0-3", "configuration.aslr=off"]

    def _run(self, task_dir, name, extra=(), score=(10.0, 11.0, 12.0, 13.0)):
        d = task_dir / "runs" / name
        d.mkdir(parents=True)
        record.write_env(str(d / "env.json"), self.FIELDS + list(extra), bool_fields=["cores.pinned=0-3"])
        record.write_env(str(d / "env.json"), ["wall_time_s=12"], update=True)
        (d / "result.json").write_text(json.dumps({"JetStream3.0": {"tests": {"t": {"metrics": {
            "Score": {"current": list(score)}}}}}}))
        return d

    def _task(self, tmp):
        d = tmp / "20260901T000000Z-w"
        record.task_write(str(d), ["task=20260901T000000Z-w", "requested=now", "subject.kind=workspace",
                                   "subject.spec=w", "devices=container=jsc-release", "plans=jetstream3",
                                   "rounds=1", "slots=w", "count=4"], ["wk bench w jetstream3"])
        return d

    def test_the_record_carries_its_provenance_and_the_default_axes(self):
        with scratch_dir() as tmp:
            env = json.loads((self._run(self._task(tmp), "r1") / "env.json").read_text())
            self.assertEqual(env["host"]["root_device"], "nvme0n1 (nvme, ssd, trim)")
            self.assertEqual(env["cores"], {"set": "0-3", "pinned": True})
            self.assertEqual(env["wall_time_s"], "12")
            self.assertEqual(env["configuration"]["aslr"], "off")
            self.assertEqual(env["configuration"]["path_len"], 0, "an axis nobody set reads as uncontrolled")

    def test_ls_reads_the_run_from_its_record_alone(self):
        with scratch_dir() as tmp:
            d = self._run(self._task(tmp), "r1")
            (d / "run-1.log").write_text("Score: 999\n")
            rows = record.ls_rows(str(tmp), where="tolken")
            self.assertIn("w jsc-release · jetstream3  [tolken]", rows[0])
            self.assertIn("complete  1/1 runs ended, 1 ok, 0 failed", rows[1])
            self.assertTrue(rows[3].endswith("jetstream3 jsc-release jsc 0123456789 ok"), rows[3])

    def test_compare_and_report_warn_from_the_records(self):
        with scratch_dir() as tmp:
            t = self._task(tmp)
            a = self._run(t, "ra")
            b = self._run(t, "rb", extra=["host.root_device=mmcblk0 (sd, rotational, no-trim)", "cores.set=4-7"])
            cp = in_process(report.two_runs, [str(a)], [str(b)])
            self.assertIn("different root storage", cp.stdout)
            self.assertIn("different core pins (0-3 vs 4-7)", cp.stdout)
            self.assertIn("aslr=off", cp.stdout)


class TestTaskReport(WkTest):
    """The task report: partial data reported as partial, paired rounds only,
    one html per device x plan, named for the task."""

    def test_partial_task_reports_usable_rounds_and_names_the_missing(self):
        with scratch_dir() as tmp:
            d = make_task(tmp, rounds=5)
            add_run(d, "base", 1, "a", "ok", (100.0, 101.0, 99.0))
            add_run(d, "pr1725", 1, "b", "ok", (95.0, 96.0, 94.0))
            add_run(d, "base", 2, "a", "failed")
            add_run(d, "pr1725", 2, "b", "ok")
            add_run(d, "base", 3, "a", "ok", (100.5, 100.0, 99.5))
            add_run(d, "pr1725", 3, "b", "running")
            cp = in_process(report.task_report, str(d), True, html=True, text=True)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            out = cp.stdout
            self.assertIn("state     running", out)
            self.assertIn("rounds: 1 usable of 3 attempted (5 planned)", out)
            self.assertIn("round 2 (base: failed)", out)
            self.assertIn("round 3 (pr1725: running)", out)
            self.assertIn("A = slot base, B = slot pr1725", out)
            self.assertIn("Elm-TodoMVC", out)
            html = d / "report-rpi3-speedometer2.1.html"
            self.assertTrue(html.exists(), "the html report is named for the device and plan, inside the task")
            self.assertIn(f"wrote {html}", out)
            text = html.read_text()
            self.assertIn("Elm-TodoMVC", text)
            self.assertIn("1 usable of 3 attempted", text)
            # Only the paired round's values are in the table: round 3's
            # base run (100.5, 100.0, 99.5) has no partner.
            self.assertNotIn("100.500", out.split("subtests:")[1])

    def test_nothing_paired_yet_says_so_without_failing(self):
        with scratch_dir() as tmp:
            d = make_task(tmp)
            add_run(d, "base", 1, "a", "ok")
            cp = in_process(report.task_report, str(d), False, text=True)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("no round has both arms yet", cp.stdout)
            self.assertIn("state     incomplete", cp.stdout)

    def test_a_single_slot_task_is_not_an_ab(self):
        with scratch_dir() as tmp:
            d = make_task(tmp, rounds=1, slots=("base",), name="20260830T120000Z-rpi3-base")
            cp = in_process(report.task_report, str(d), False, text=True)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("not an A/B", cp.stdout)


class TestLs(WkTest):
    """One store's rows: every task, its state and each run's directory."""

    def test_lists_tasks_with_run_paths_and_states(self):
        with scratch_dir() as tmp:
            d = make_task(tmp)
            a = add_run(d, "base", 1, "a", "ok")
            b = add_run(d, "pr1725", 1, "b", "failed")
            lines = record.ls_rows(str(tmp), running={TASK})
            self.assertTrue(lines[0].startswith(TASK + "  A/B wpe:1725"), lines[0])
            self.assertIn("running", lines[1])
            self.assertIn(str(d), lines[2])
            self.assertTrue(any(str(a) in l and l.rstrip().endswith("ok") for l in lines), lines)
            self.assertTrue(any(str(b) in l and l.rstrip().endswith("failed") for l in lines), lines)

    def test_an_empty_store_has_no_rows(self):
        """The "no tasks anywhere" line belongs to the listing that merged the stores."""
        with scratch_dir() as tmp:
            self.assertEqual(record.ls_rows(str(tmp)), [])
            self.assertEqual(record.ls_rows(str(tmp / "absent")), [])

    def test_the_machine_holding_the_store_is_printed_against_each_task(self):
        with scratch_dir() as tmp:
            make_task(tmp)
            self.assertIn("[moose]", record.ls_rows(str(tmp), where="moose")[0])
            self.assertNotIn("[", record.ls_rows(str(tmp))[0])


class TestTheFleetListing(WkTest):
    """`wk bench ls` walks the fleet: this store's rows first, then each target
    whose machine answers for a store of its own, in walk order, asked through
    its own wk with the label it is to print and no walk of its own."""

    def listing(self, tmp, targets, warned):
        return record.Listing(FakeRegistry(tmp, targets), str(tmp), lambda r: str(tmp / r),
                              lambda path: False, "here", warned.append)

    def test_each_answering_machine_adds_its_own_rows_after_this_stores(self):
        with scratch_dir() as tmp:
            make_task(tmp)
            moose = FakeTarget("moose", out="T-moose  x  [moose]\r\n    complete\n")
            vm = FakeTarget("vm", kind="vm", out="T-vm  y  [here]\n")
            rows = self.listing(tmp, [moose, vm], []).rows()
            self.assertTrue(rows[0].startswith(TASK))
            self.assertEqual(rows[-3:], ["T-moose  x  [moose]", "    complete", "T-vm  y  [here]"])
            args, env, quiet = moose.asked[0]
            self.assertEqual(args, ("bench", "ls", "--continued"))
            self.assertEqual((env["WK_ROW_LABEL"], env["WK_NO_DELEGATE"]), ("moose", "1"))
            self.assertEqual(vm.asked[0][1]["WK_ROW_LABEL"], "here", "a machine behind this one is labelled as this one")

    def test_a_stopped_machine_is_named_with_its_remedy(self):
        with scratch_dir() as tmp:
            warned = []
            self.assertEqual(self.listing(tmp, [FakeTarget("vm", kind="vm", side="stopped")], warned).rows(), [])
            self.assertIn("'wk start' brings it up", warned[0])

    def test_one_that_does_not_answer_the_listing_is_named(self):
        with scratch_dir() as tmp:
            warned = []
            self.listing(tmp, [FakeTarget("moose", rc=2)], warned).rows()
            self.assertIn("wk sync --tools moose", warned[0])

    def test_one_with_no_store_of_its_own_is_not_asked(self):
        with scratch_dir() as tmp:
            quiet = [FakeTarget("c", kind="container", side="none"), FakeTarget("far", side="unreachable")]
            warned = []
            self.assertEqual(self.listing(tmp, quiet, warned).rows(), [])
            self.assertEqual(warned, [])
            self.assertEqual([t.asked for t in quiet], [[], []])


class TestTheVerbs(WkTest):
    """`wk bench ls` and `report <task>` / `report <run-a> <run-b>` over a
    scratch store: the lock decides running, and the refusals name their remedy."""

    def store(self, s):
        bench_dir = s["path"] / "bench"
        bench_dir.mkdir()
        d = make_task(bench_dir)
        add_run(d, "base", 1, "a", "ok", (100.0, 101.0, 99.0))
        add_run(d, "pr1725", 1, "b", "ok", (95.0, 96.0, 94.0))
        return d

    def test_ls_lists_the_store_and_says_where_each_task_is(self):
        with temp_store() as s:
            self.store(s)
            cp = in_process(bench(s["path"]).ls, False)
            self.assertIn(TASK, cp.stdout)
            self.assertIn("[here]", cp.stdout.splitlines()[0])
            self.assertIn("incomplete", cp.stdout, "no lock is held, so the task is not running")
            self.assertIn("each task is on the machine that took it", cp.stderr)

    def test_a_continued_listing_is_rows_alone(self):
        with temp_store() as s:
            self.store(s)
            cp = in_process(bench(s["path"]).ls, True)
            self.assertIn(TASK, cp.stdout)
            self.assertEqual(cp.stderr, "")

    def test_an_empty_fleet_says_so(self):
        with temp_store() as s:
            cp = in_process(bench(s["path"]).ls, False)
            self.assertEqual(cp.stdout, "")
            self.assertIn("(no tasks on any machine this one knows)", cp.stderr)

    def test_report_reads_the_task_and_writes_its_html_into_it(self):
        with temp_store() as s:
            d = self.store(s)
            cp = in_process(bench(s["path"]).report, [TASK], True, False)
            self.assertIn("rounds: 1 usable of 1 attempted (3 planned)", cp.stdout)
            self.assertTrue((d / "report-rpi3-speedometer2.1.html").exists())

    def test_a_held_lock_makes_the_task_running(self):
        with temp_store() as s:
            self.store(s)
            b = bench(s["path"])
            lock = Lock(b.reg.store, Local(), FakeClock())
            with lock.held("bench-task-" + TASK):
                self.assertIn("state     running", in_process(b.report, [TASK], "", True).stdout)
                self.assertIn("    running", in_process(b.ls, True).stdout)

    def test_two_run_directories_need_no_task(self):
        with temp_store() as s:
            d = self.store(s)
            runs = sorted(str(p) for p in (d / "runs").iterdir())
            cp = in_process(bench(s["path"]).report, runs, "", False)
            self.assertIn("Elm-TodoMVC", cp.stdout)
            rel = sorted("%s/runs/%s" % (TASK, p.name) for p in (d / "runs").iterdir())
            self.assertIn("Elm-TodoMVC", in_process(bench(s["path"]).compare, rel, "").stdout,
                          "a run is also named relative to the store")

    def test_a_run_of_one_iteration_is_warned_about(self):
        with temp_store() as s:
            d = self.store(s)
            runs = sorted((d / "runs").iterdir())
            for r in runs:
                record.write_env(str(r / "env.json"), ["count=1"], update=True)
            cp = in_process(bench(s["path"]).report, [str(r) for r in runs], "", False)
            self.assertIn("has count=1: no p-value can be computed", cp.stderr)

    def test_the_refusals_name_their_remedy(self):
        with temp_store() as s:
            self.store(s)
            b = bench(s["path"])
            for args, said in ((([TASK], "out.html", False), "--html takes no file here"),
                               ((["nosuch-task"], "", False), "stays on the machine that took it"),
                               ((["/nowhere", "/nowhere"], "", False), "no such run in -a: /nowhere"),
                               ((["a", "b"], True, False), "writes where it is told: --html out.html"),
                               (([], "", False), "usage: wk bench report")):
                with self.subTest(args=args):
                    err = refusal(b.report, *args)
                    self.assertIn(said, err)
            self.assertIn("usage: wk bench compare", refusal(b.compare, ["one"], ""))
            self.assertIn("usage: wk bench precision", refusal(b.precision, ["one"], "0.3"))
            self.assertIn("is not a percentage", refusal(b.precision, ["a", "b"], "lots"))

    def test_an_unknown_task_names_the_fleet_rows_that_do(self):
        with temp_store() as s:
            (s["path"] / "bench").mkdir()
            other = FakeTarget("moose", out="T-elsewhere  x  [moose]\n")
            self.assertIn("    T-elsewhere  x  [moose]", refusal(bench(s["path"], [other]).report, ["T-elsewhere"], "", False))


def refusal(fn, *args):
    """What a verb that refuses says on stderr; it must refuse."""
    import contextlib
    import io
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        try:
            fn(*args)
        except Refused:
            return err.getvalue()
    raise AssertionError("%s did not refuse" % fn.__name__)


class TestWhere(WkTest):
    """Where each Python verb runs: seed where its workspace's store is, ls
    where it was typed and --continued in this machine's store."""

    def test_each_verb_answers_where_it_runs(self):
        with scratch_dir() as tmp:
            reg = FakeRegistry(tmp)
            self.assertEqual(cli.where(reg, ["ls"]), "local")
            self.assertEqual(cli.where(reg, ["ls", "--continued"]), "store")
            self.assertEqual(cli.where(reg, ["seed", "cws", "jetstream3"]), "workspace")
            self.assertEqual(cli.where(reg, ["seed", "guest", "jetstream3"]), "host")
            self.assertEqual(cli.where(reg, ["report", "t"]), "host")

    def test_a_workspace_no_target_answers_for_is_asked_for_here(self):
        with scratch_dir() as tmp:
            reg = FakeRegistry(tmp)

            def nowhere(ws):
                raise LookupError(ws)
            reg.ws_target = nowhere
            self.assertEqual(cli.where(reg, ["seed", "gone", "p"]), "host")


class TestThroughWk(WkTest):
    """Through ./wk: the dispatcher reaches the Python verbs, with the options
    it normalised, and the unported arms still reach bash."""

    def test_bench_ls_and_report_read_a_task_in_the_store(self):
        with temp_store() as store:
            bench_dir = store["path"] / "bench"
            bench_dir.mkdir()
            d = make_task(bench_dir)
            add_run(d, "base", 1, "a", "ok", (100.0, 101.0, 99.0))
            add_run(d, "pr1725", 1, "b", "ok", (95.0, 96.0, 94.0))
            env = {"WK_STORE": store["WK_STORE"], "WK_LOCK_DIR": str(store["path"] / "locks"), "WK_TARGET": "local"}
            ls = run("bench", "ls", env=env, timeout=60)
            self.assertEqual(ls.returncode, 0, ls.stdout)
            self.assertIn(TASK, ls.stdout)
            self.assertNotIn("starting podman machine", ls.stdout)
            rep = run("bench", "report", TASK, "--text", env=env, timeout=60)
            self.assertEqual(rep.returncode, 0, rep.stdout)
            self.assertIn("rounds: 1 usable of 1 attempted (3 planned)", rep.stdout)
            runs = sorted(str(p) for p in (d / "runs").iterdir())
            html = store["path"] / "two.html"
            two = run("bench", "report", runs[0], runs[1], "--html", str(html), env=env, timeout=60)
            self.assertEqual(two.returncode, 0, two.stdout)
            self.assertTrue(html.exists(), two.stdout)

    def test_an_unported_arm_still_runs_in_bash(self):
        with scratch_dir() as tmp:
            cp = run("bench", "staged", "--ls", env={"WK_BENCH_ROOT": str(tmp)}, timeout=60)
            if sys.platform == "darwin":
                self.assertEqual(cp.returncode, 0, cp.stdout)
                self.assertIn("nothing staged on", cp.stdout)
            else:
                self.assertIn("is macOS bench mode", cp.stdout)

    def test_pi_bench_refuses_an_unknown_task_before_any_board(self):
        with temp_store() as store:
            (store["path"] / "bench").mkdir()
            cp = run("pi", "bench", "not-a-real-machine", "speedometer3", "--task", "nosuch",
                     env={"WK_STORE": store["WK_STORE"]}, timeout=30)
            self.assertNotEqual(cp.returncode, 0)
            self.assertIn("no such task 'nosuch'", cp.stdout)
            self.assertNotIn("no such machine", cp.stdout, "the task is checked before the board is looked up")

    def test_ab_refuses_an_unknown_task(self):
        with temp_store() as store:
            (store["path"] / "bench").mkdir()
            cp = run("ab", "wpe:1725", "--devices", "rpi3", "--task", "nosuch",
                     env={"WK_STORE": store["WK_STORE"]}, timeout=30)
            self.assertNotEqual(cp.returncode, 0)
            self.assertIn("no such task 'nosuch'", cp.stdout)

    def test_ab_timeout_is_seconds(self):
        cp = run("ab", "wpe:1725", "--devices", "rpi3", "--timeout", "soon", timeout=30)
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("--timeout takes seconds", cp.stdout)

    def test_ab_detach_and_dry_run_exclude_each_other(self):
        cp = run("ab", "wpe:1725", "--devices", "rpi3", "--dry-run", "--detach", timeout=30)
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("nothing to detach", cp.stdout)


class TestReadingTasksStartsNothing(WkTest):
    """`wk bench ls` walks the fleet from where it is typed and reads a store
    without starting the machine that holds it."""

    def _decl(self, key):
        text = (REPO / "cmd" / "bench").read_text()
        return [l for l in text.splitlines() if l.startswith("# wk:") and key in l]

    def test_cmd_bench_answers_where_itself(self):
        cp = subprocess.run([str(REPO / "cmd" / "bench"), "--where", "ls", "--continued"],
                            capture_output=True, text=True, timeout=60,
                            env={"WK_ROOT": str(REPO), "HOME": "/tmp", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
        self.assertEqual((cp.returncode, cp.stdout.strip()), (0, "store"), cp.stderr)

    def test_report_and_compare_still_read_one_store(self):
        host = [l for l in self._decl("where=host") if l.startswith("# wk: sub ")]
        self.assertTrue(host, self._decl("where=host"))
        subs = host[0].split()[3].split(",")
        for verb in ("report", "compare"):
            self.assertIn(verb, subs, host[0])

    def test_ls_is_declared_read_only(self):
        """The dispatcher starts the podman machine for anything that forwards
        and is not read-only. Reading a list of tasks is not a reason to boot a VM."""
        ro = self._decl("readonly")
        self.assertTrue(ro, "cmd/bench no longer declares anything read-only")
        self.assertIn("ls", ro[0].split()[-1].split(","), ro[0])


class TestArtifactsLandWhereTheMachineCanReadThem(WkTest):
    """`wk_record_dir` (lib/store.sh): what this machine writes for itself and
    opens again -- a seeded benchmark payload, an exported runner tree, a
    downloaded profiler, a long-running command's task record, a bench task's
    directory. On a Linux host that is the store; on a macOS workstation the
    store is the podman VM's, root-owned and unwritable from this side, so they
    go in this machine's own state directory instead. `wk ab` and `wk pi bench`
    are host commands that record a task, and neither could run at all while
    the answer was the store (measured 2026-09-16: `mkdir /var/lib/wk/bench` is
    Permission denied)."""

    def _dir(self, store, extra=None):
        return self._ask({"WK_STORE": str(store), **(extra or {})})

    def _dir_default(self, extra=None):
        """No WK_STORE at all, so `_wk_default_store` decides -- which on a
        macOS workstation is the podman machine's."""
        return self._ask(dict(extra or {}))

    def _ask(self, env):
        cp = bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/bench.sh"
. "{REPO}/lib/profiler.sh"
echo "RECORD=$(wk_record_dir)"
echo "ARTIFACT=$(wk_artifact_dir)"
echo "BENCH=$BENCH_DIR"
echo "SEED=$SEED_DIR"
echo "RUNNER=$RUNNER_DIR"
echo "SAMPLY=$(samply_store_dir aarch64-apple-darwin)"
''', env=env)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        out = dict(l.split("=", 1) for l in cp.stdout.strip().splitlines())
        out["TASK"] = str(task_record.Records(env=clean_env(env)).root)   # what lib/task.sh writes through
        return out

    def test_a_writable_store_keeps_them(self):
        with scratch_dir() as tmp:
            f = self._dir(tmp, {"XDG_STATE_HOME": str(tmp / "state")})
            self.assertEqual(f["RECORD"], str(tmp))
            self.assertEqual(f["ARTIFACT"], f"{tmp}/cache")
            self.assertEqual(f["BENCH"], f"{tmp}/bench")
            self.assertEqual(f["TASK"], f"{tmp}/task")
            self.assertEqual(f["SEED"], f"{tmp}/cache/bench")
            self.assertEqual(f["RUNNER"], f"{tmp}/cache/bench-runner")
            self.assertTrue(f["SAMPLY"].startswith(f"{tmp}/cache/samply/"), f["SAMPLY"])

    @unittest.skipUnless(sys.platform == "darwin",
                         "only a macOS workstation keeps the store off this machine")
    def test_the_one_store_no_host_command_can_write_sends_them_to_its_own_state(self):
        """The podman machine's store, which is what this machine's default
        resolves to: the answer is the machine's own directory, and a task can
        actually be created there."""
        with scratch_dir() as tmp:
            # No WK_STORE: the default is the one that is the VM's.
            f = self._dir_default({"XDG_STATE_HOME": str(tmp / "state")})
            state = f"{tmp}/state/wk"
            self.assertEqual(f["RECORD"], state)
            for key in ("ARTIFACT", "BENCH", "TASK"):
                self.assertTrue(f[key].startswith(state + "/"), f"{key}={f[key]}")
            cp = bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/bench.sh"
ensure_dir "$(bench_task_dir probe)/runs" >/dev/null && echo MADE
''', env={"XDG_STATE_HOME": str(tmp / "state")})
            self.assertIn("MADE", cp.stdout, cp.stdout + cp.stderr)

    def test_a_store_this_machine_was_pointed_at_keeps_its_own_records(self):
        """Only the VM's store is diverted. A target's own store, or a test's
        scratch one, is where its records belong -- diverting those put a
        record where the command that wrote it would never look again."""
        with scratch_dir() as tmp:
            named = tmp / "somewhere" / "store"        # not created: a store is made on demand
            f = self._dir(named, {"XDG_STATE_HOME": str(tmp / "state")})
            self.assertEqual(f["RECORD"], str(named))
            self.assertEqual(f["TASK"], f"{named}/task")

    def test_they_are_all_under_the_one_directory(self):
        """One rule -- a second answer to 'where can this machine put a file'
        is where the macOS lane broke."""
        with scratch_dir() as tmp:
            f = self._dir(tmp, {"XDG_STATE_HOME": str(tmp / "state")})
            for key in ("ARTIFACT", "BENCH", "TASK", "SEED", "RUNNER", "SAMPLY"):
                self.assertTrue(f[key].startswith(f['RECORD'] + "/"),
                                f"{key}={f[key]} is not under {f['RECORD']}")

    def test_one_spelling_of_the_bench_directory(self):
        """status and doctor read it without sourcing lib/bench.sh, so the
        path is a function rather than a second `$WK_STORE/bench`."""
        text = (REPO / "lib" / "bench.sh").read_text()
        self.assertIn("wk_bench_dir", text)
        self.assertNotIn("$WK_STORE/bench", text)
        for rel in ("lib/wk/status.py", "lib/wk/doctor.py"):
            with self.subTest(file=rel):
                text = (REPO / rel).read_text()
                self.assertIn("store.bench_dir()", text)
                self.assertNotIn('record_dir(), "bench"', text)
