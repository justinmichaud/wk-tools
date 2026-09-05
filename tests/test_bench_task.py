"""Benchmark *tasks*: the unit `wk ab`, `wk pi bench` and `wk bench` produce
and `wk bench ls`, `wk bench report` and `wk status` speak in (lib/bench.sh
"tasks", lib/wkdata.py task_state).

Unit tests build a synthetic task -- task.json plus runs whose env.json
carries the round and arm `wk pi bench --ab` records -- and drive
`lib/wkdata.py` exactly as cmd/bench, cmd/pi and cmd/status do: as a
subprocess. No board, no workspace. The bash half (the task's lock deciding
"running", `wk bench report <task>` through cmd/bench, the refusals) is
exercised through ./wk against a scratch store.

Run: python3 -m unittest tests.test_bench_task -v
"""
import json
import subprocess
import unittest

from tests.support import REPO, WkTest, bash, run, scratch_dir, temp_store

WKDATA = REPO / "lib" / "wkdata.py"
TASK = "20260830T120000Z-wpe-pr1725"


def wkdata(*args, timeout=30):
    return subprocess.run(["python3", str(WKDATA), *args], cwd=str(REPO),
                          capture_output=True, text=True, timeout=timeout)


def make_task(bench_dir, rounds=3, slots=("base", "pr1725"), name=TASK):
    d = bench_dir / name
    cp = wkdata("task-write", str(d), f"task={name}", "requested=2026-08-30T12:00:00Z",
                "subject.kind=pull", "subject.spec=wpe:1725",
                "subject.head=afa2ed9e70103469f02732323fa7b874378d6a5d",
                "subject.base=04abe09851b03b7ce6c8b9c208659c23a5ab7138",
                "devices=rpi3=wpewebkit-2.38-buildroot-rpi3-32", "plans=speedometer2.1",
                f"rounds={rounds}", "slots=" + ",".join(slots), "count=1", "timeout=1200",
                "--command", "wk pi deploy wpewebkit-2.38-buildroot-rpi3-32 rpi3 --slot base",
                "--command", f"wk pi bench rpi3 speedometer2.1 --ab base,pr1725 --rounds {rounds} --task {name}")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return d


def add_run(task_dir, slot, rnd, arm, outcome, vals=(100.0, 102.0, 99.0)):
    """outcome: ok (result.json), failed (ended, no result), running (neither)."""
    d = task_dir / "runs" / f"20260830T12{rnd:02d}{'00' if arm == 'a' else '30'}Z-speedometer2.1-rpi3-{slot}"
    d.mkdir(parents=True)
    fields = ["plan=speedometer2.1", "config=wpewebkit-2.38-buildroot-rpi3-32", "machine=rpi3",
              f"build_slot={slot}", "webkit_sha=abcdef1234567890", "runner=browser", "arch=armv7l",
              "bench_host=image", f"task={task_dir.name}", f"ab.round={rnd}", f"ab.arm={arm}",
              "ab.slot_a=base", "ab.slot_b=pr1725"]
    cp = wkdata("env-record", str(d / "env.json"), *fields)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    if outcome != "running":
        wkdata("env-record", str(d / "env.json"), "--update", "wall_time_s=400")
    if outcome == "ok":
        (d / "result.json").write_text(json.dumps({"Speedometer-2": {"tests": {"Elm-TodoMVC": {
            "metrics": {"Time": {"Total": {"current": list(vals)}}}}}}}))
    if outcome == "running":
        (d / "run.log").write_text("INFO - Start the iteration 1 of 1 for current benchmark\n")
    return d


def kv(text):
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


class TestTaskState(WkTest):
    """planned, ended, usable, complete: recomputed from task.json and the
    runs; running is what the caller says the lock says."""

    def test_a_fresh_task_is_incomplete_with_nothing_run(self):
        with scratch_dir() as tmp:
            d = make_task(tmp)
            st = kv(wkdata("task-status", str(d)).stdout)
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
            st = kv(wkdata("task-status", str(d), "--running").stdout)
            self.assertEqual(st["state"], "running")
            self.assertIn("now speedometer2.1 rpi3 pr1725", st["summary"])
            self.assertIn("iteration 1/1", st["summary"])
            # The same files with no lock: a driver that died mid-run.
            st = kv(wkdata("task-status", str(d)).stdout)
            self.assertEqual(st["state"], "incomplete")
            self.assertIn("died with their driver", st["summary"])

    def test_complete_when_every_planned_run_ended_even_failed_ones(self):
        with scratch_dir() as tmp:
            d = make_task(tmp, rounds=2)
            add_run(d, "base", 1, "a", "ok"); add_run(d, "pr1725", 1, "b", "ok")
            add_run(d, "base", 2, "a", "failed"); add_run(d, "pr1725", 2, "b", "ok")
            st = kv(wkdata("task-status", str(d)).stdout)
            self.assertEqual(st["state"], "complete")
            self.assertEqual(st["ended"], "4")
            self.assertEqual(st["failed"], "1")
            self.assertEqual(st["usable"], "1", "a round with one failed arm is not usable")

    def test_task_write_refuses_a_task_missing_its_shape(self):
        with scratch_dir() as tmp:
            cp = wkdata("task-write", str(tmp / "t"), "task=t", "requested=now", "plans=p", "slots=a")
            self.assertNotEqual(cp.returncode, 0)
            self.assertIn("devices is required", cp.stdout + cp.stderr)

    def test_a_directory_without_task_json_is_not_a_task(self):
        with scratch_dir() as tmp:
            (tmp / "junk").mkdir()
            cp = wkdata("task-status", str(tmp / "junk"))
            self.assertNotEqual(cp.returncode, 0)
            self.assertIn("no task.json", cp.stdout + cp.stderr)


class TestTaskReport(WkTest):
    """`wkdata.py task-report`: partial data reported as partial, paired
    rounds only, one html per device x plan, named for the task."""

    def test_partial_task_reports_usable_rounds_and_names_the_missing(self):
        with scratch_dir() as tmp:
            d = make_task(tmp, rounds=5)
            add_run(d, "base", 1, "a", "ok", (100.0, 101.0, 99.0))
            add_run(d, "pr1725", 1, "b", "ok", (95.0, 96.0, 94.0))
            add_run(d, "base", 2, "a", "failed")
            add_run(d, "pr1725", 2, "b", "ok")
            add_run(d, "base", 3, "a", "ok", (100.5, 100.0, 99.5))
            add_run(d, "pr1725", 3, "b", "running")
            cp = wkdata("task-report", str(d), "--running", "--html", "--text")
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
            cp = wkdata("task-report", str(d), "--text")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("no round has both arms yet", cp.stdout)
            self.assertIn("state     incomplete", cp.stdout)

    def test_a_single_slot_task_is_not_an_ab(self):
        with scratch_dir() as tmp:
            d = make_task(tmp, rounds=1, slots=("base",), name="20260830T120000Z-rpi3-base")
            cp = wkdata("task-report", str(d), "--text")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("not an A/B", cp.stdout)


class TestLs(WkTest):
    """`wkdata.py ls`: every task, its state and each run's directory."""

    def test_lists_tasks_with_run_paths_and_states(self):
        with scratch_dir() as tmp:
            d = make_task(tmp)
            a = add_run(d, "base", 1, "a", "ok")
            b = add_run(d, "pr1725", 1, "b", "failed")
            cp = wkdata("ls", str(tmp), "--running", TASK)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            lines = cp.stdout.splitlines()
            self.assertTrue(lines[0].startswith(TASK + "  A/B wpe:1725"), lines[0])
            self.assertIn("running", lines[1])
            self.assertIn(str(d), lines[2])
            self.assertTrue(any(str(a) in l and l.rstrip().endswith("ok") for l in lines), cp.stdout)
            self.assertTrue(any(str(b) in l and l.rstrip().endswith("failed") for l in lines), cp.stdout)

    def test_an_empty_store_says_so(self):
        with scratch_dir() as tmp:
            cp = wkdata("ls", str(tmp))
            self.assertEqual(cp.stdout.strip(), "(no tasks yet)")


class TestThroughWk(WkTest):
    """The bash half against a scratch store: `wk bench ls` and `wk bench
    report <task>` read the task the way `wk ab` writes it, the lock decides
    running, and the refusals name their remedy."""

    def test_bench_ls_and_report_read_a_task_in_the_store(self):
        with temp_store() as store:
            bench = store["path"] / "bench"
            bench.mkdir()
            d = make_task(bench)
            add_run(d, "base", 1, "a", "ok", (100.0, 101.0, 99.0))
            add_run(d, "pr1725", 1, "b", "ok", (95.0, 96.0, 94.0))
            env = {"WK_STORE": store["WK_STORE"], "WK_LOCK_DIR": str(store["path"] / "locks")}
            ls = run("bench", "ls", env=env, timeout=60)
            self.assertEqual(ls.returncode, 0, ls.stdout)
            self.assertIn(TASK, ls.stdout)
            self.assertIn("incomplete", ls.stdout, "no lock is held, so the task is not running")
            rep = run("bench", "report", TASK, "--html", env=env, timeout=60)
            self.assertEqual(rep.returncode, 0, rep.stdout)
            self.assertIn("rounds: 1 usable of 1 attempted (3 planned)", rep.stdout)
            self.assertTrue((d / "report-rpi3-speedometer2.1.html").exists())
            # Two run directories, the form every other lane uses, still work
            # -- and a run directory is what `wk bench ls` prints.
            runs = sorted(str(p) for p in (d / "runs").iterdir())
            two = run("bench", "report", runs[0], runs[1], env=env, timeout=60)
            self.assertEqual(two.returncode, 0, two.stdout)
            self.assertIn("Elm-TodoMVC", two.stdout)

    def test_a_held_lock_makes_the_task_running(self):
        with temp_store() as store:
            bench = store["path"] / "bench"
            bench.mkdir()
            d = make_task(bench)
            add_run(d, "base", 1, "a", "ok")
            lock_dir = store["path"] / "locks"
            # hold_lock in a process that stays alive while `wk bench ls` looks.
            cp = bash(f'''
                . lib/common.sh
                WK_LOCK_DIR={lock_dir}
                hold_lock bench-task-{TASK}
                WK_STORE={store["WK_STORE"]} WK_LOCK_DIR={lock_dir} ./wk bench ls
                WK_STORE={store["WK_STORE"]} WK_LOCK_DIR={lock_dir} ./wk bench report {TASK} --text
            ''', timeout=60)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("running", cp.stdout)
            self.assertIn("state     running", cp.stdout)

    def test_report_refuses_an_unknown_task_and_names_the_remedy(self):
        with temp_store() as store:
            (store["path"] / "bench").mkdir()
            cp = run("bench", "report", "nosuch-task", env={"WK_STORE": store["WK_STORE"]}, timeout=60)
            self.assertNotEqual(cp.returncode, 0)
            self.assertIn("no such task", cp.stdout)
            self.assertIn("wk bench ls", cp.stdout)

    def test_task_html_takes_no_file(self):
        with temp_store() as store:
            bench = store["path"] / "bench"
            bench.mkdir()
            make_task(bench)
            cp = run("bench", "report", TASK, "--html", "out.html", env={"WK_STORE": store["WK_STORE"]}, timeout=60)
            self.assertNotEqual(cp.returncode, 0)
            self.assertIn("--html takes no file here", cp.stdout)

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


if __name__ == "__main__":
    unittest.main()


class TestReadingTasksStartsNothing(WkTest):
    """`wk ab` and `wk pi bench` write a task on this host, so the task store is
    this host's. `wk bench ls` was declared `where=workspace` all the same, so on
    a macOS host it was handed to the podman VM -- which meant reading a store
    with no tasks in it, and, because it was not declared read-only, **booting a
    20GB VM to do it**. Running the test suite started the machine that way.

    Both halves are asserted here: where it runs, and that it changes nothing."""

    def _decl(self, key):
        import re
        text = (REPO / "cmd" / "bench").read_text()
        return [l for l in text.splitlines() if l.startswith("# wk:") and key in l]

    def test_ls_runs_where_its_siblings_do(self):
        """report and compare read the same store and are `where=host`."""
        host = [l for l in self._decl("where=host") if l.startswith("# wk: sub ")]
        self.assertTrue(host, self._decl("where=host"))
        subs = host[0].split()[3].split(",")
        for verb in ("ls", "report", "compare"):
            self.assertIn(verb, subs, host[0])

    def test_ls_is_declared_read_only(self):
        """The dispatcher starts the podman machine for anything that forwards
        and is not read-only (`wk`, the forward path). Reading a list of tasks
        is not a reason to boot a VM."""
        ro = self._decl("readonly")
        self.assertTrue(ro, "cmd/bench no longer declares anything read-only")
        self.assertIn("ls", ro[0].split()[-1].split(","), ro[0])

    def test_it_reads_the_store_it_is_pointed_at(self):
        """With the declaration wrong this needed WK_IN_VM=1 to stay on this
        host. Nothing sets it now, so this fails if the forward comes back."""
        with temp_store() as store:
            bench = store["path"] / "bench"
            bench.mkdir()
            make_task(bench)
            cp = run("bench", "ls", env={"WK_STORE": store["WK_STORE"]}, timeout=60)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn(TASK, cp.stdout)
            self.assertNotIn("starting podman machine", cp.stdout + cp.stderr)
