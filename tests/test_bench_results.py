"""A task's results: where a task lives (its workspace's directory, and nowhere else), what its
report confirms (the commit each arm measured, each check's verdict, how to restart it), and `wk bench export`,
the deliverables as one zip (lib/wk/bench/record.py, report.py, cli.py).

Run: python3 tests/run.py -k test_bench_results
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import types
import zipfile
from pathlib import Path
from unittest import mock

from tests.fakes import FakeTarget
from tests.killpoints import converges
from tests.support import REPO, WkTest, run, scratch_dir
from tests.test_bench_report import in_process
from tests.test_bench_task import TASK, add_run, make_task, refusal, registry

sys.path.insert(0, str(REPO / "lib"))
from wk import status, targets  # noqa: E402
from wk.bench import cli, record, report  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Fake, Local, Planted, PodmanVm, Ssh  # noqa: E402

HEAD, BASE = "afa2ed9e70103469f02732323fa7b874378d6a5d", "04abe09851b03b7ce6c8b9c208659c23a5ab7138"
READING = {"benchmarks": {}, "combined": {}, "compressed": {}, "missing": [], "libraries": [], "plans": []}


def complete_task(bench_dir, name=TASK):
    d = make_task(bench_dir, rounds=1, name=name)
    for slot, arm, sha in (("base", "a", BASE), ("pr1725", "b", HEAD)):
        record.write_env(str(add_run(d, slot, 1, arm, "ok") / "env.json"), ["webkit_sha=" + sha], update=True)
    (d / "warmup").mkdir()
    (d / "warmup" / "rpi3-a.profile.json").write_text("{}")
    (d / "runs" / os.listdir(d / "runs")[0] / "run.log").write_text("not a deliverable\n")
    return d


def clean_env():
    return mock.patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                        if k not in ("WK_YES", "WK_FORCE", "WK_DRY_RUN", "WK_DESTRUCTIVE", "WK_CONFIRMED")}, clear=True)


class TestATaskLivesInItsWorkspace(WkTest):
    def test_a_workspace_this_store_holds_keeps_its_tasks_and_one_it_does_not_is_refused(self):
        with scratch_dir() as tmp:
            (tmp / "ws" / "w").mkdir(parents=True)
            home = (Local(), str(tmp / "ws" / "w" / "bench"))
            self.assertEqual(record.held(home, "w"), home)
            self.assertIn("no workspace 'elsewhere'", refusal(record.held, (Local(), str(tmp / "ws" / "elsewhere" / "bench")), "elsewhere"))
            self.assertFalse((tmp / "ws" / "elsewhere").exists(), "no workspace directory is made for a task")

    def test_every_task_is_found_wherever_it_lives(self):
        with scratch_dir() as tmp:
            reg = registry(tmp, env={"WK_ROW_LABEL": "here"})
            in_ws = make_task(tmp / "ws" / "w" / "bench", name="20260901T000000Z-w")
            in_other = make_task(tmp / "ws" / "v" / "bench")
            make_task(tmp / "bench", name="20260101T000000Z-stray")
            self.assertEqual(record.homes(reg.store), {TASK: str(in_other), "20260901T000000Z-w": str(in_ws)}, "a store's bench/ is no home")
            b = cli.Bench(REPO, reg, FakeClock())
            listed = in_process(b.ls, True).stdout
            self.assertIn(str(in_ws), listed)
            self.assertIn(str(in_other), listed)
            self.assertIn("data      %s" % in_ws, in_process(b.report, ["20260901T000000Z-w"], "", True).stdout)
            (rec,) = status.bench_records(reg.store, "here", lambda pid: False)
            self.assertEqual((rec["task"], rec["path"]), ("20260901T000000Z-w", str(in_ws)))


class TestTheReportConfirmsWhatWasBuiltAndChecked(WkTest):
    def report(self, d):
        return in_process(report.task_report, str(d), False, text=True).stdout

    def test_each_arm_names_the_commit_it_measured_against_the_tasks(self):
        with scratch_dir() as tmp:
            d = complete_task(tmp)
            out = self.report(d)
            self.assertIn("slot base: 04abe09851b0  ok, the task's base", out)
            self.assertIn("slot pr1725: afa2ed9e7010  ok, the task's head", out)
            self.assertIn("ok       preflight     every run passed it", out)
            self.assertNotIn("PGO", out, "no run is a PGO build, so there is no PGO check")

    def test_a_run_of_another_commit_or_none_is_named(self):
        with scratch_dir() as tmp:
            d = complete_task(tmp)
            for r in (d / "runs").iterdir():
                arm = json.loads((r / "env.json").read_text())["ab"]["arm"]
                record.write_env(str(r / "env.json"), ["webkit_sha=" + ("1" * 40 if arm == "b" else "")], update=True)
            out = self.report(d)
            self.assertIn("slot base: ?  unknown -- no run recorded the commit it measured", out)
            self.assertIn("slot pr1725: 111111111111  FAIL -- the task names afa2ed9e7010", out)

    def test_a_forced_preflight_fails_its_check(self):
        with scratch_dir() as tmp:
            d = complete_task(tmp)
            r = sorted((d / "runs").iterdir())[0]
            record.write_env(str(r / "env.json"), ["preflight_notes=cpu governor: powersave; "], bool_fields=["forced=1"], update=True)
            self.assertIn("FAIL     preflight     1 of 2 runs forced past failing checks: cpu governor: powersave;", self.report(d))

    def test_a_pgo_build_is_judged_by_the_reading_its_run_carries(self):
        with scratch_dir() as tmp:
            d = complete_task(tmp)
            runs = sorted((d / "runs").iterdir())
            for r in runs:
                record.write_env(str(r / "env.json"), ["config=mac-release-pgo"], update=True)
            self.assertIn("unknown  PGO profile   2 of 2 PGO runs carry no profile-check.json reading", self.report(d))
            for r in runs:
                (r / "profile-check.json").write_text(json.dumps(READING))
            self.assertIn("ok       PGO profile   every one of 2 PGO runs' readings passes", self.report(d))
            (runs[0] / "profile-check.json").write_text(json.dumps(dict(READING, missing=["output/WebCore.profdata"])))
            self.assertIn("FAIL     PGO profile   output/WebCore.profdata is missing", self.report(d))
            (runs[0] / "profile-check.json").write_text("{\"other\": 1}")
            self.assertIn("not a profile-check reading", self.report(d))

    def test_a_board_pgo_slot_and_the_warmup_round_are_checks_too(self):
        with scratch_dir() as tmp:
            d = complete_task(tmp)
            for r in (d / "runs").iterdir():
                record.write_env(str(r / "env.json"), ["build_config=wpe-cross-pgo-use"], update=True)
            (d / "warmup" / "rpi3-a.evidence.json").write_text(json.dumps({"elf": {}, "gl": {}, "jit": {}, "problems": []}))
            out = self.report(d)
            self.assertIn("unknown  PGO profile", out)
            self.assertIn("FAIL     warmup        rpi3: arm B produced no warmup evidence", out)

    def test_a_task_stopped_short_says_how_to_restart_it(self):
        with scratch_dir() as tmp:
            d = make_task(tmp)
            doc = json.loads((d / "task.json").read_text())
            (d / "task.json").write_text(json.dumps(dict(doc, restart="wk bench ab wpe:1725 --devices rpi3 --task " + TASK)))
            out = self.report(d)
            self.assertIn("restart   wk bench ab wpe:1725 --devices rpi3 --task " + TASK, out)
            self.assertIn("built:\n  no runs yet", out)


class ExportTest(WkTest):
    def setUp(self):
        super().setUp()
        env = clean_env()
        env.start()
        self.addCleanup(env.stop)

    def bench(self, machine=None):
        reg = registry(self.tmp / "store", env={"HOME": str(self.tmp / "home"), "WK_ROW_LABEL": "here"})
        if machine is not None:
            reg.machine = machine
        return cli.Bench(REPO, reg, FakeClock())

    def export(self, b, task=TASK, to=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = b.export(task, to)
        return rc, out.getvalue(), err.getvalue()


class TestExport(ExportTest):
    def test_the_deliverables_land_as_one_zip_in_downloads(self):
        d = complete_task(self.tmp / "store" / "ws" / "w" / "bench")
        rc, out, _ = self.export(self.bench())
        dest = self.tmp / "home" / "Downloads" / (TASK + ".zip")
        self.assertEqual((rc, out.strip()), (0, str(dest)))
        names = zipfile.ZipFile(str(dest)).namelist()
        runs = sorted(os.listdir(d / "runs"))
        want = ["report.txt", "task.json", "report-rpi3-speedometer2.1.html", "warmup/rpi3-a.profile.json"] + \
            ["runs/%s/%s" % (r, f) for r in runs for f in ("env.json", "result.json")]
        self.assertEqual(sorted(names), sorted(TASK + "/" + n for n in want))
        text = zipfile.ZipFile(str(dest)).read(TASK + "/report.txt").decode()
        self.assertIn("the task's head", text)
        self.assertEqual((d / record.EXPORT_RECORD).read_text(), str(dest) + "\n", "the task records where it went")

    def test_to_names_the_directory_and_makes_it(self):
        complete_task(self.tmp / "store" / "ws" / "w" / "bench")
        rc, out, _ = self.export(self.bench(), to=str(self.tmp / "out" / "here"))
        self.assertTrue((self.tmp / "out" / "here" / (TASK + ".zip")).is_file(), out)

    def test_a_task_not_complete_is_refused_and_force_exports_it(self):
        make_task(self.tmp / "store" / "ws" / "w" / "bench")
        b = self.bench()
        self.assertIn("is incomplete, so its report is partial", refusal(b.export, TASK, ""))
        self.assertFalse((self.tmp / "home" / "Downloads").exists())
        os.environ["WK_FORCE"] = "1"
        self.assertEqual(self.export(b)[0], 0)
        self.assertTrue((self.tmp / "home" / "Downloads" / (TASK + ".zip")).is_file())

    def test_an_existing_archive_is_replaced_only_when_asked(self):
        complete_task(self.tmp / "store" / "ws" / "w" / "bench")
        dest = self.tmp / "home" / "Downloads" / (TASK + ".zip")
        dest.parent.mkdir(parents=True)
        dest.write_text("older")
        b = self.bench()
        self.assertIn("declining (no terminal", refusal(b.export, TASK, ""))
        self.assertEqual(dest.read_text(), "older")
        os.environ["WK_YES"] = "1"
        self.assertEqual(self.export(b)[0], 0)
        self.assertTrue(zipfile.is_zipfile(str(dest)))

    def test_a_dry_run_writes_nothing(self):
        d = complete_task(self.tmp / "store" / "ws" / "w" / "bench")
        os.environ["WK_DRY_RUN"] = "1"
        rc, _, err = self.export(self.bench())
        self.assertEqual(rc, 0)
        self.assertIn("would write: %s" % (self.tmp / "home" / "Downloads" / (TASK + ".zip")), err)
        self.assertFalse((self.tmp / "home" / "Downloads").exists())
        self.assertFalse(list(d.glob("report-*.html")), "the html reports are written only by a real export")

    def test_the_refusals_name_their_remedy(self):
        b = self.bench()
        self.assertIn("usage: wk bench export", refusal(b.export, "", ""))
        self.assertIn("stays on the machine that took it", refusal(b.export, "nosuch", ""))


class TestAOneArmReportNamesRunsWhereTheTaskIs(ExportTest):
    def test_an_exported_report_names_the_runs_under_the_task_and_not_the_staging_copy(self):
        d = make_task(self.tmp / "store" / "ws" / "w" / "bench", rounds=1, slots=("base",))
        add_run(d, "base", 1, "a", "ok")
        rc, out, err = self.export(self.bench())
        text = zipfile.ZipFile(out.strip()).read(TASK + "/report.txt").decode()
        self.assertIn("not an A/B", text, err)
        self.assertIn("  ok  %s/runs/" % d, text)
        self.assertNotIn("wk-export-", text)


class TestExportKillPoints(ExportTest):
    def test_an_export_killed_after_any_effect_and_rerun_converges(self):
        """`killpoints[bench export]`: the archive is written whole or not at all, and a re-run replaces a partial one."""
        d = complete_task(self.tmp / "store" / "ws" / "w" / "bench")
        dest = str(self.tmp / "home" / "Downloads" / (TASK + ".zip"))
        os.environ["WK_YES"] = "1"

        def world():
            fake = Fake("here")
            for p in d.rglob("*"):
                if p.is_file():
                    fake._set_file(str(p), p.read_bytes())
            return types.SimpleNamespace(fake=fake)

        def run_once(w):
            self.export(self.bench(w.fake))

        def final(w):
            data = w.fake.files.get(dest)
            return sorted(zipfile.ZipFile(io.BytesIO(data)).namelist()) if data else None
        converges(self, world, run_once, final)


class TestThroughWk(ExportTest):
    def test_export_is_declared_with_its_destination_and_its_dry_run(self):
        store = self.tmp / "store"
        complete_task(store / "ws" / "w" / "bench")
        env = {"WK_STORE": str(store), "WK_LOCK_DIR": str(store / "locks"), "WK_TARGET": "local", "HOME": str(self.tmp / "home")}
        dry = run("bench", "export", TASK, "--to", str(self.tmp / "out"), "--dry-run", env=env, timeout=60)
        self.assertEqual(dry.returncode, 0, dry.stdout)
        self.assertFalse((self.tmp / "out").exists(), dry.stdout)
        wet = run("bench", "export", TASK, "--to", str(self.tmp / "out"), env=env, timeout=60)
        self.assertEqual(wet.returncode, 0, wet.stdout)
        self.assertTrue(zipfile.is_zipfile(str(self.tmp / "out" / (TASK + ".zip"))), wet.stdout)
        again = run("bench", "export", TASK, "--to", str(self.tmp / "out"), "--yes", env=env, timeout=60)
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertIn("usage", run("bench", "export", TASK, "--nosuch", env=env, timeout=60).stdout)


def far_target(name, far, side="answering"):
    return FakeTarget(name, side=side, far_store=(far, "/var/lib/wk"))


class FarBox(Fake, Ssh):
    """A machine reached over ssh, whose files answer from memory."""


class TestWhereALegRecords(WkTest):
    """record.leg_home: a leg records into its workspace's bench/, a named task there, and on this machine."""

    def reg(self, far):
        d = complete_task(Path(tempfile.mkdtemp(dir=str(self.tmp))))
        for p in d.rglob("*"):
            if p.is_file():
                far._set_file("/var/lib/wk/ws/w/bench/%s/%s" % (TASK, p.relative_to(d)), p.read_bytes())
        far.dirs.add("/var/lib/wk/ws/w")
        vm = far_target("vm", far)
        vm.results = lambda ws: (far, "/var/lib/wk/ws/%s/bench" % ws)
        return registry(self.tmp / "store", [vm])

    def refused(self, *args):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(Refused):
            record.leg_home(*args)
        return err.getvalue()

    def test_a_leg_records_into_its_workspaces_bench_through_the_machine_holding_it(self):
        far = Fake("vm")
        reg = self.reg(far)
        self.assertEqual(record.leg_home(reg, "w"), (far, "/var/lib/wk/ws/w/bench"))
        self.assertEqual(record.leg_home(reg, "w", TASK), (far, "/var/lib/wk/ws/w/bench"))

    def test_a_task_its_workspace_does_not_hold_is_refused(self):
        self.assertIn("no such task 'nope' in workspace 'w'", self.refused(self.reg(Fake("vm")), "w", "nope"))

    def test_a_workspace_on_a_machine_reached_over_ssh_is_refused_naming_it(self):
        self.assertIn("run it on box", self.refused(self.reg(FarBox("box")), "w", TASK))


class TestExportReachesATaskOnAnotherMachine(ExportTest):
    """A task in the podman VM's store or on a build box: read through that machine's read_tree, the zip built here."""

    def far(self):
        d = complete_task(self.tmp / "disk")
        far = Fake("vm")
        for p in d.rglob("*"):
            if p.is_file():
                far._set_file("/var/lib/wk/ws/w/bench/%s/%s" % (TASK, p.relative_to(d)), p.read_bytes())
        return far

    def bench_with(self, target):
        reg = registry(self.tmp / "store", [target], env={"HOME": str(self.tmp / "home"), "WK_ROW_LABEL": "here"})
        return cli.Bench(REPO, reg, FakeClock())

    def test_the_zip_is_built_here_from_reads_and_the_task_records_it(self):
        far = self.far()
        rc, out, err = self.export(self.bench_with(far_target("container", far)))
        dest = self.tmp / "home" / "Downloads" / (TASK + ".zip")
        self.assertEqual((rc, out.strip()), (0, str(dest)), err)
        names = zipfile.ZipFile(str(dest)).namelist()
        self.assertIn(TASK + "/warmup/rpi3-a.profile.json", names)
        self.assertIn(TASK + "/report-rpi3-speedometer2.1.html", names)
        text = zipfile.ZipFile(str(dest)).read(TASK + "/report.txt").decode()
        self.assertIn("data      /var/lib/wk/ws/w/bench/" + TASK, text)
        self.assertEqual(far.files["/var/lib/wk/ws/w/bench/%s/%s" % (TASK, record.EXPORT_RECORD)], str(dest) + "\n")
        self.assertEqual([e for e in far.effects if e[0].startswith("copy")], [], "nothing is copied out of that machine")

    def test_a_report_reaches_it_the_same_way(self):
        """`wk bench report <task>` runs on this host, and a Mac's container task is in the podman machine's store:
        it was 'no such task' about a task `wk bench ls` listed one line up (measured 2026-09-27)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.bench_with(far_target("container", self.far())).task_report(TASK, False, True)
        self.assertEqual(0, rc, err.getvalue())
        self.assertIn("data      /var/lib/wk/ws/w/bench/" + TASK, out.getvalue())

    def test_two_of_its_runs_compare_from_here(self):
        """The run directories `wk bench ls` prints are that machine's paths; the two-run form reads them the same way."""
        far = self.far()
        runs = sorted({p.split("/runs/")[1].split("/")[0] for p in far.files if "/runs/" in p})
        a, b = ("/var/lib/wk/ws/w/bench/%s/runs/%s" % (TASK, r) for r in runs[:2])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.bench_with(far_target("container", far)).report([a, b], False, True)
        self.assertEqual(0, rc, err.getvalue())
        self.assertIn("Elm-TodoMVC", out.getvalue())

    def test_a_machine_that_does_not_answer_is_not_asked(self):
        target = far_target("buildbox", self.far(), side="unreachable")
        self.assertIn("stays on the machine that took it", refusal(self.bench_with(target).export, TASK, ""))


class HereOverSsh(Ssh):
    """The far-side scripts run by this host's own sh, so what they do is measured rather than assumed."""

    def argv(self, remote, tty=False):
        return ["sh", "-c", remote]


SECRET = b"-----BEGIN OPENSSH PRIVATE KEY-----\n"


class PlantedTest(ExportTest):
    """An agent in a workspace writes its own tasks (the workspace directory is mounted read-write), so a link it
    plants there must never make this host read a file of its own into a report or a zip, nor write through one."""

    def setUp(self):
        super().setUp()
        self.key = self.tmp / "push-keys" / "build_key_fork"
        self.key.parent.mkdir()
        self.key.write_bytes(SECRET)
        self.task = complete_task(self.tmp / "store" / "ws" / "w" / "bench")
        self.run_dir = sorted((self.task / "runs").iterdir())[0]

    def plant(self, at, target=None):
        os.symlink(str(target or self.key), str(at))
        return at

    def assertNothingHolds(self, *where):
        for p in where:
            if p.exists():
                self.assertNotIn(SECRET, p.read_bytes(), p)


class TestAPlantedLinkIsRefused(PlantedTest):
    def test_export_refuses_a_link_among_the_runs_naming_it(self):
        link = self.plant(self.run_dir / "x.json")
        why = refusal(self.bench().export, TASK, "")
        self.assertIn("%s is a symbolic link" % link, why)
        self.assertIn("rm %s" % link, why)
        self.assertFalse((self.tmp / "home" / "Downloads").exists())

    def test_export_refuses_a_link_in_place_of_the_task_or_its_bench_directory(self):
        elsewhere = self.tmp / "elsewhere"
        (self.tmp / "store" / "ws" / "w" / "bench").rename(elsewhere)
        link = self.plant(self.tmp / "store" / "ws" / "w" / "bench", elsewhere)
        self.assertIn("%s is a symbolic link" % link, refusal(self.bench().export, TASK, ""))
        os.unlink(str(link))
        (self.tmp / "store" / "ws" / "w" / "bench").mkdir()
        link = self.plant(self.tmp / "store" / "ws" / "w" / "bench" / TASK, elsewhere / TASK)
        self.assertIn("%s is a symbolic link" % link, refusal(self.bench().export, TASK, ""))

    def test_a_link_to_a_directory_is_not_walked(self):
        link = self.plant(self.task / "warmup" / "keys", self.key.parent)
        self.assertIn("%s is a symbolic link" % link, refusal(self.bench().export, TASK, ""))

    def test_a_special_file_where_a_deliverable_is_named_is_refused(self):
        os.mkfifo(str(self.run_dir / "x.json"))
        self.assertIn("not a regular file", refusal(self.bench().export, TASK, ""))

    def test_the_task_report_refuses_it_too(self):
        self.plant(self.task / "warmup" / "rpi3-b.profile.json")
        self.assertIn("is a symbolic link", refusal(self.bench().task_report, TASK, True, True))
        self.assertFalse(list(self.task.glob("report-*.html")))

    def test_the_two_run_report_refuses_a_planted_result(self):
        a, b = sorted((self.task / "runs").iterdir())
        (b / "result.json").unlink()
        link = self.plant(b / "result.json", a / "result.json")
        why = refusal(self.bench().report, [str(a), str(b)], str(self.tmp / "r.html"), False)
        self.assertIn("%s is a symbolic link" % link, why)
        self.assertFalse((self.tmp / "r.html").exists())

    def test_archive_refuses_it_on_its_own(self):
        self.plant(self.task / "report-rpi3-x.html")
        with self.assertRaises(Refused):
            with contextlib.redirect_stderr(io.StringIO()):
                cli.archive(str(self.task), False)

    def test_rm_counts_it_as_unexported_naming_it(self):
        link = self.plant(self.run_dir / "x.json")
        (why,) = record.unexported(Local(), str(self.task.parent), str(self.tmp / "home" / "Downloads"))
        self.assertEqual(TASK, why[0])
        self.assertIn("%s is a symbolic link" % link, why[1])


class TestAPlantedLinkIsNotWrittenThrough(PlantedTest):
    def test_the_record_replaces_a_link_at_task_json_and_its_old_temp(self):
        self.plant(self.task / "task.json.tmp")
        (self.task / "task.json").unlink()
        self.plant(self.task / "task.json")
        make_task(self.task.parent)
        self.assertEqual(SECRET, self.key.read_bytes())
        self.assertFalse((self.task / "task.json").is_symlink())

    def test_an_env_record_replaces_a_link(self):
        (self.run_dir / "env.json").unlink()
        self.plant(self.run_dir / "env.json")
        record.write_env(str(self.run_dir / "env.json"), ["plan=x"])
        self.assertEqual(SECRET, self.key.read_bytes())

    def test_a_task_html_report_replaces_a_link(self):
        self.plant(self.task / "report-rpi3-speedometer2.1.html")
        in_process(report.task_report, str(self.task), False, html=True)
        self.assertEqual(SECRET, self.key.read_bytes())
        self.assertIn(b"<svg", (self.task / "report-rpi3-speedometer2.1.html").read_bytes())

    def test_local_write_never_opens_a_predictable_temp(self):
        dest = self.task / record.EXPORT_RECORD
        self.plant(self.task / ("%s.tmp.%d" % (record.EXPORT_RECORD, os.getpid())))
        Local().write(str(dest), "/x.zip\n")
        self.assertEqual(SECRET, self.key.read_bytes())
        self.assertEqual("/x.zip\n", dest.read_text())
        self.assertEqual(oct(0o666 & ~_umask()), oct(dest.stat().st_mode & 0o777))

    def test_the_far_side_refuses_to_write_through_a_link_and_writes_a_file_otherwise(self):
        link = self.plant(self.task / record.EXPORT_RECORD, self.key.parent)
        far = HereOverSsh("box", via=Local())
        with self.assertRaises(OSError) as e:
            far.write(str(link), "/x.zip\n")
        self.assertIn("is a symbolic link", str(e.exception))
        self.assertEqual(["build_key_fork"], os.listdir(str(self.key.parent)))
        os.unlink(str(link))
        far.write(str(link), "/x.zip\n")
        self.assertEqual("/x.zip\n", link.read_text())
        self.assertEqual(oct(0o666 & ~_umask()), oct(link.stat().st_mode & 0o777))
        self.assertEqual([], [n for n in os.listdir(str(self.task)) if n.startswith(".wk-write")])


def _umask():
    mask = os.umask(0)
    os.umask(mask)
    return mask


class TestTheSeamReadsATree(PlantedTest):
    PATTERNS = record.MEASURED

    def read(self, m, top=None):
        return m.read_tree(*record.guarded(str(top or self.task)), self.PATTERNS)

    def test_local_and_the_far_side_read_the_same_regular_files_byte_for_byte(self):
        (self.task / "warmup" / "b.bin").write_bytes(b"\x00\xffgz")
        here = self.read(Local())
        self.assertEqual(b"\x00\xffgz", here["warmup/b.bin"])
        self.assertNotIn("runs/%s/run.log" % self.run_dir.name, here)
        self.assertEqual(here, self.read(HereOverSsh("box", via=Local())))

    def test_the_far_side_refuses_a_link_in_the_tree_or_above_it(self):
        far = HereOverSsh("box", via=Local())
        link = self.plant(self.run_dir / "x.json")
        with self.assertRaises(Planted) as e:
            self.read(far)
        self.assertIn(str(link), str(e.exception))
        os.unlink(str(link))
        elsewhere = self.tmp / "elsewhere"
        self.task.rename(elsewhere)
        link = self.plant(self.task, elsewhere)
        with self.assertRaises(OSError) as e:
            self.read(far)
        self.assertIn("%s is a symbolic link" % link, str(e.exception))

    def test_the_podman_vm_reads_through_the_same_script_and_still_refuses_a_copy(self):
        via = Fake("here")
        via.answer(["podman", "machine", "ssh"], rc=1, err="no such directory")
        vm = PodmanVm("wk", via=via)
        with self.assertRaises(OSError):
            vm.read_tree("/var/lib/wk/ws/w", "bench/t", ("*",))
        self.assertIn("tar -cf", via.effects[-1][1][-1])
        with self.assertRaises(NotImplementedError):
            vm.copy_tree_out("/f", str(self.tmp / "x"))

    def test_a_fake_refuses_a_link_it_holds(self):
        f = Fake()
        f._set_file("/s/ws/w/bench/t/task.json", "x")
        self.assertEqual({"task.json": b"x"}, f.read_tree("/s/ws/w", "bench/t", ("task.json",)))
        f.symlink("/etc", "/s/ws/w/bench/t/runs")
        with self.assertRaises(OSError):
            f.read_tree("/s/ws/w", "bench/t", ("task.json",))

    def test_each_target_names_the_store_it_holds_apart_from_this_one(self):
        c = targets.Container("container", str(REPO), {"HOME": "/h", "WK_STORE": "/var/lib/wk"}, Fake())
        with mock.patch.object(targets.Container, "is_here", return_value=True):
            self.assertIsNone(c.task_store())
        with mock.patch.object(targets.Container, "is_here", return_value=False):
            m, root = c.task_store()
        self.assertEqual((type(m), root), (PodmanVm, "/var/lib/wk"))
        box = types.SimpleNamespace(peer=False, is_local=False, machine="ssh", root_there=lambda: "/srv/wk")
        self.assertEqual(targets.Remote.task_store(box), ("ssh", "/srv/wk"))
        for peer, local in ((True, False), (False, True)):
            self.assertIsNone(targets.Remote.task_store(types.SimpleNamespace(peer=peer, is_local=local)))

    def peer(self, rc=0, out=""):
        far = Fake("peer")
        far.answer(["sh", "-c"], rc=rc, out=out, err="" if rc == 0 else "ssh: connect to host peer1: refused")
        p = targets.Remote.__new__(targets.Remote)
        p.peer, p.is_local, p.machine, p.host, p.env = True, False, far, "peer1", {"WK_REMOTE_TOOLS": "/t"}
        return p, far

    def test_a_peer_names_its_tasks_home_through_the_hops_its_own_wk_reaches_it_by(self):
        """A Mac peer's container workspace keeps its tasks in that Mac's podman machine, reached through the Mac."""
        p, far = self.peer(out=json.dumps({"via": [["podman", "wk"]], "path": "/var/lib/wk/ws/w/bench"}))
        m, path = p.results("w")
        self.assertEqual((type(m), m.dest, m.via, path), (PodmanVm, "wk", far, "/var/lib/wk/ws/w/bench"))
        self.assertIn("wk.bench.record home w", far.effects[-1][1][-1])

    def test_a_peer_that_does_not_answer_is_named_with_what_it_said(self):
        for rc, out in ((255, ""), (0, "")):
            with self.subTest(rc=rc):
                p, _ = self.peer(rc=rc, out=out)
                err = io.StringIO()
                with contextlib.redirect_stderr(err), self.assertRaises(Refused):
                    p.results("w")
                self.assertIn("peer1 did not say where 'w' keeps its bench tasks", err.getvalue())
                self.assertIn("wk sync --tools", err.getvalue())

    def test_a_machine_is_named_as_its_hops_nearest_first(self):
        self.assertEqual(record.hops(Local()), [])
        self.assertEqual(record.hops(PodmanVm("wk", via=Ssh("box"))), [["ssh", "box"], ["podman", "wk"]])
