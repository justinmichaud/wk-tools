"""Benchmark *tasks*: the unit `wk ab` and `wk bench` produce
and `wk bench ls`, `wk bench report` and `wk status` speak in
(lib/wk/bench/record.py; `wk bench`'s verbs in lib/wk/bench/cli.py)."""
import json
import os
import subprocess
import sys
import unittest

from tests.fakes import FakeRegistry, FakeTarget
from tests.support import REPO, WkTest, clean_env, run, scratch_dir, temp_store
from tests.test_bench_report import in_process

sys.path.insert(0, str(REPO / "lib"))
from wk.act import Refused  # noqa: E402
from wk.bench import cli, record, report  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.lock import Lock  # noqa: E402
from wk import record as task_record, samply  # noqa: E402
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


def found(bench_dir):
    """One directory's tasks as homes() names them."""
    return {t: str(bench_dir / t) for t in record.tasks(str(bench_dir))}


def status(d, running=False):
    st = record.task_state(str(d), running)
    out = {k: str(st[k]) for k in ("state", "planned", "ended", "ok", "failed", "usable", "summary")}
    return dict(out, subject=record.subject_line(st["doc"]), current=st["current"]["id"] if st["current"] else "")


def registry(store_dir, targets=(), env=None):
    """What cli.Bench and record.Listing ask of the registry: a store, the walk, and each target."""
    env = dict(env or {}, WK_STORE=str(store_dir), WK_LOCK_DIR=str(store_dir / "locks"))
    ts = {t.name: t for t in targets}
    return FakeRegistry(env, Local(), lambda n, e: ts[n], names=list(ts),
                        ws_target=lambda ws: {"cws": "container"}.get(ws, "vm"), in_workspace=lambda: False)


def bench(store_dir, targets=(), env=None):
    return cli.Bench(REPO, registry(store_dir, targets, dict(env or {}, WK_ROW_LABEL="here")), FakeClock())


class TestTaskState(WkTest):

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


class TestOneRecord(WkTest):

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
            rows = record.ls_rows(found(tmp), where="tolken")
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

    def test_lists_tasks_with_run_paths_and_states(self):
        with scratch_dir() as tmp:
            d = make_task(tmp)
            a = add_run(d, "base", 1, "a", "ok")
            b = add_run(d, "pr1725", 1, "b", "failed")
            lines = record.ls_rows(found(tmp), running={TASK})
            self.assertTrue(lines[0].startswith(TASK + "  A/B wpe:1725"), lines[0])
            self.assertIn("running", lines[1])
            self.assertIn(str(d), lines[2])
            self.assertTrue(any(str(a) in l and l.rstrip().endswith("ok") for l in lines), lines)
            self.assertTrue(any(str(b) in l and l.rstrip().endswith("failed") for l in lines), lines)

    def test_an_empty_store_has_no_rows(self):
        with scratch_dir() as tmp:
            self.assertEqual(record.ls_rows(found(tmp)), [])
            self.assertEqual(record.ls_rows(found(tmp / "absent")), [])

    def test_the_machine_holding_the_store_is_printed_against_each_task(self):
        with scratch_dir() as tmp:
            make_task(tmp)
            self.assertIn("[moose]", record.ls_rows(found(tmp), where="moose")[0])
            self.assertNotIn("[", record.ls_rows(found(tmp))[0])


class TestTheFleetListing(WkTest):

    def listing(self, tmp, targets, warned):
        reg = registry(tmp, targets)
        return record.Listing(reg, reg.store, lambda path: False, "here", warned.append)

    def test_each_answering_machine_adds_its_own_rows_after_this_stores(self):
        with scratch_dir() as tmp:
            make_task(tmp / "ws" / "w" / "bench")
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

    def store(self, s):
        bench_dir = s["path"] / "ws" / "w" / "bench"
        bench_dir.mkdir(parents=True)
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

    def test_a_running_tasks_report_reads_its_progress_off_the_staged_copy(self):
        with temp_store() as s:
            d = self.store(s)
            add_run(d, "base", 2, "a", "running")
            (d / "status").write_text("stage=running round 2\n")
            b = bench(s["path"])
            with Lock(b.reg.store, Local(), FakeClock()).held("bench-task-" + TASK):
                self.assertIn("(iteration 1/1)", in_process(b.report, [TASK], "", True).stdout)

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

    def test_each_verb_answers_where_it_runs(self):
        with scratch_dir() as tmp:
            reg = registry(tmp)
            self.assertEqual(cli.where(reg, ["ls"]), "local")
            self.assertEqual(cli.where(reg, ["ls", "--continued"]), "store")
            self.assertEqual(cli.where(reg, ["seed", "cws", "jetstream3"]), "workspace")
            self.assertEqual(cli.where(reg, ["seed", "guest", "jetstream3"]), "host")
            self.assertEqual(cli.where(reg, ["report", "t"]), "host")

    def test_a_workspace_no_target_answers_for_is_asked_for_here(self):
        with scratch_dir() as tmp:
            reg = registry(tmp)

            def nowhere(ws):
                raise LookupError(ws)
            reg.ws_target = nowhere
            self.assertEqual(cli.where(reg, ["seed", "gone", "p"]), "host")


class TestThroughWk(WkTest):

    def test_bench_ls_and_report_read_a_task_in_a_workspace(self):
        with temp_store() as store:
            bench_dir = store["path"] / "ws" / "w" / "bench"
            bench_dir.mkdir(parents=True)
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

    def test_staged_ls_answers_only_on_a_mac(self):
        with scratch_dir() as tmp:
            cp = run("bench", "staged", "--ls", env={"WK_BENCH_ROOT": str(tmp)}, timeout=60)
            if sys.platform == "darwin":
                self.assertEqual(cp.returncode, 0, cp.stdout)
                self.assertIn("nothing staged on", cp.stdout)
            else:
                self.assertIn("is macOS bench mode", cp.stdout)


class TestReadingTasksStartsNothing(WkTest):

    def test_cmd_bench_answers_where_itself(self):
        cp = subprocess.run([str(REPO / "cmd" / "bench"), "--where", "ls", "--continued"],
                            capture_output=True, text=True, timeout=60,
                            env={"WK_ROOT": str(REPO), "HOME": "/tmp", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
        self.assertEqual((cp.returncode, cp.stdout.strip()), (0, "store"), cp.stderr)


class TestArtifactsLandWhereTheMachineCanReadThem(WkTest):

    def _dir(self, store, extra=None):
        return self._ask({"WK_STORE": str(store), **(extra or {})})

    def _dir_default(self, extra=None):
        """No WK_STORE at all, so Store.default decides -- which on a macOS workstation is the podman machine's."""
        return self._ask(dict(extra or {}))

    def _ask(self, env):
        st = Store(clean_env(env))
        return {"RECORD": st.record_dir(), "ARTIFACT": st.artifact_dir(),
                "SAMPLY": samply.store_dir(st.artifact_dir(), "aarch64-apple-darwin"),
                "TASK": str(task_record.Records(env=clean_env(env)).root),
                "SEED": os.path.join(st.artifact_dir(), "bench")}   # where wk.bench.seed pins a payload

    def test_a_writable_store_keeps_them(self):
        with scratch_dir() as tmp:
            f = self._dir(tmp, {"XDG_STATE_HOME": str(tmp / "state")})
            self.assertEqual(f["RECORD"], str(tmp))
            self.assertEqual(f["ARTIFACT"], f"{tmp}/cache")
            self.assertEqual(f["TASK"], f"{tmp}/task")
            self.assertEqual(f["SEED"], f"{tmp}/cache/bench")
            self.assertTrue(f["SAMPLY"].startswith(f"{tmp}/cache/samply/"), f["SAMPLY"])

    @unittest.skipUnless(sys.platform == "darwin",
                         "only a macOS workstation keeps the store off this machine")
    def test_the_one_store_no_host_command_can_write_sends_them_to_its_own_state(self):
        with scratch_dir() as tmp:
            # No WK_STORE: the default is the one that is the VM's.
            f = self._dir_default({"XDG_STATE_HOME": str(tmp / "state")})
            state = f"{tmp}/state/wk"
            self.assertEqual(f["RECORD"], state)
            for key in ("ARTIFACT", "TASK"):
                self.assertTrue(f[key].startswith(state + "/"), f"{key}={f[key]}")
            runs = os.path.join(f["RECORD"], "ws", "w", "bench", "probe", "runs")
            os.makedirs(runs)
            self.assertTrue(os.path.isdir(runs))

    def test_a_store_this_machine_was_pointed_at_keeps_its_own_records(self):
        with scratch_dir() as tmp:
            named = tmp / "somewhere" / "store"        # not created: a store is made on demand
            f = self._dir(named, {"XDG_STATE_HOME": str(tmp / "state")})
            self.assertEqual(f["RECORD"], str(named))
            self.assertEqual(f["TASK"], f"{named}/task")

