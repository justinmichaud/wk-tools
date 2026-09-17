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
import sys
import unittest

from tests.support import REPO, WkTest, bash, func_body, run, scratch_dir, temp_store

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

    def test_an_empty_store_prints_nothing(self):
        """One store's rows, so several can be concatenated into one listing:
        the "no tasks anywhere" line belongs to the command that merged them
        (cmd/bench), which is the only one that knows there were none."""
        with scratch_dir() as tmp:
            cp = wkdata("ls", str(tmp))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual(cp.stdout.strip(), "")

    def test_the_machine_holding_the_store_is_printed_against_each_task(self):
        with scratch_dir() as tmp:
            make_task(tmp)
            cp = wkdata("ls", str(tmp), "--where", "moose")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("[moose]", cp.stdout.splitlines()[0])

    def test_without_one_no_machine_is_claimed(self):
        with scratch_dir() as tmp:
            make_task(tmp)
            cp = wkdata("ls", str(tmp))
            self.assertNotIn("[", cp.stdout.splitlines()[0])


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
    """`wk bench ls` was declared `where=workspace`, so on a macOS host it was
    handed to the podman VM -- which meant reading a store with no tasks in it,
    and, because it was not declared read-only, **booting a 20GB VM to do it**.
    Running the test suite started the machine that way.

    It walks the fleet now, so it meets the VM again as one machine among
    several; what must not come back is the boot."""

    def _decl(self, key):
        text = (REPO / "cmd" / "bench").read_text()
        return [l for l in text.splitlines() if l.startswith("# wk:") and key in l]

    def _where(self, *args):
        cp = subprocess.run([str(REPO / "cmd" / "bench"), "--where", *args],
                            capture_output=True, text=True, timeout=120,
                            env={"WK_ROOT": str(REPO), "HOME": "/tmp",
                                 "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.strip()

    def test_a_walk_runs_where_it_was_typed(self):
        """It reads this machine's store and then asks every other machine,
        so it is this machine's command and not one store's."""
        self.assertEqual(self._where("ls"), "local")

    def test_answering_another_machines_walk_reads_this_machines_store(self):
        """--continued is the half a walk asked for, so it is the store's --
        the podman VM's on a macOS workstation, like any other read of it."""
        self.assertEqual(self._where("ls", "--continued"), "store")

    def test_report_and_compare_still_read_one_store(self):
        host = [l for l in self._decl("where=host") if l.startswith("# wk: sub ")]
        self.assertTrue(host, self._decl("where=host"))
        subs = host[0].split()[3].split(",")
        for verb in ("report", "compare"):
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
            cp = run("bench", "ls", env={"WK_STORE": store["WK_STORE"]}, timeout=300)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn(TASK, cp.stdout)
            self.assertNotIn("starting podman machine", cp.stdout + cp.stderr)

    def test_the_listing_names_the_machine_each_task_is_on(self):
        """A measurement is recorded once, on the machine that took it; the
        listing is merged on every read rather than copied between machines,
        so a reader is told which machine to go to."""
        with temp_store() as store:
            bench = store["path"] / "bench"
            bench.mkdir()
            make_task(bench)
            cp = run("bench", "ls", env={"WK_STORE": store["WK_STORE"]}, timeout=300)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            row = [l for l in cp.stdout.splitlines() if l.startswith(TASK)]
            self.assertTrue(row, cp.stdout)
            self.assertRegex(row[0], r"\[[^\]]+\]$")

    def test_a_task_no_machine_has_is_refused_with_where_to_look(self):
        with temp_store() as store:
            (store["path"] / "bench").mkdir()
            cp = run("bench", "report", "nosuchtask",
                     env={"WK_STORE": store["WK_STORE"]}, timeout=300)
            self.assertNotEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("stays on the machine that took it", cp.stdout)


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
. "{REPO}/lib/task.sh"
. "{REPO}/lib/profiler.sh"
echo "RECORD=$(wk_record_dir)"
echo "ARTIFACT=$(wk_artifact_dir)"
echo "BENCH=$BENCH_DIR"
echo "TASK=$(task_root)"
echo "SEED=$SEED_DIR"
echo "RUNNER=$RUNNER_DIR"
echo "SAMPLY=$(samply_store_dir aarch64-apple-darwin)"
''', env=env)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        return dict(l.split("=", 1) for l in cp.stdout.strip().splitlines())

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
        """cmd/status and cmd/doctor read it without sourcing lib/bench.sh, so
        the path is a function rather than a second `$WK_STORE/bench`."""
        for rel in ("cmd/status", "cmd/doctor", "lib/bench.sh"):
            with self.subTest(file=rel):
                text = (REPO / rel).read_text()
                self.assertIn("wk_bench_dir", text)
                self.assertNotIn("$WK_STORE/bench", text)


class TestAStageThatCannotFinishLeavesNothing(WkTest):
    """A stage delivers gigabytes and writes its manifest last, so a failure in
    between leaves a directory that nothing can run and nothing reclaims.
    Measured 2026-09-06: a payload path that did not exist on the staging
    machine left 5.6 GB with no stage.json on the benchmark volume."""

    def test_a_payload_that_is_not_there_is_refused_before_anything_is_copied(self):
        body = func_body((REPO / "cmd" / "bench").read_text(), "cmd_stage")
        refusal = body.index("no such payload directory")
        self.assertLess(refusal, body.index("the build product"),
                        "the payloads are checked before the products are pulled")
        self.assertIn("Nothing has been staged", body)

    def test_the_refusal_is_made_once_and_not_in_the_copy_loop(self):
        body = func_body((REPO / "cmd" / "bench").read_text(), "cmd_stage")
        self.assertEqual(body.count("no such payload directory"), 1)

    def test_a_half_made_stage_goes_with_the_command_that_failed(self):
        text = (REPO / "cmd" / "bench").read_text()
        self.assertIn("wk_atexit stage_drop_half", func_body(text, "cmd_stage"))
        drop = func_body(text, "stage_drop_half")
        self.assertIn("stage.json", drop, "what makes it usable is what it checks for")
        self.assertIn("rm -rf", drop)

    def _drop(self, make):
        return bash('. "$WK_ROOT/lib/common.sh"\n'
                    + func_body((REPO / "cmd" / "bench").read_text(), "stage_drop_half")
                    .join(["stage_drop_half() {", "}\n"])
                    + 'd=$(mktemp -d); _WK_STAGE_HALF="$d"\n'
                    + make +
                    'stage_drop_half 2>/dev/null\n'
                    '[ -d "$d" ] && echo KEPT || echo GONE\n'
                    'rm -rf "$d"\n')

    def test_the_cleanup_keeps_a_stage_that_finished(self):
        cp = self._drop('echo "{}" > "$d/stage.json"\n')
        self.assertIn("KEPT", cp.stdout, cp.stdout + cp.stderr)

    def test_the_cleanup_removes_one_that_did_not(self):
        cp = self._drop('mkdir -p "$d/WebKitBuild"\n')
        self.assertIn("GONE", cp.stdout, cp.stdout + cp.stderr)


class TestAStageWorksOnACleanTree(WkTest):
    """It recorded which wk-tools staged the build with a trailing `&&`, whose
    status became the assignment's. On a clean checkout that test is false, so
    `wk bench stage` ended at exit 1 having printed nothing -- on every properly
    deployed machine, and succeeding only where the tree happened to be dirty.
    Measured 2026-09-06: two stages onto the benchmark volume left gigabytes of
    products and no manifest."""

    def _record(self, dirty):
        """The real block, lifted, with cmd/version's answer stubbed."""
        body = func_body((REPO / "cmd" / "bench").read_text(), "cmd_stage")
        block = 'if [ -n "$tools_ver" ]; then' \
                + body.split('if [ -n "$tools_ver" ]; then')[1].split("\n    fi")[0] \
                + "\n    fi\n"
        return bash('set -euo pipefail\n'
                    'kv_get() { sed -n "s/^$1=//p"; }\n'
                    f'tools_ver="sha=abc\ndirty={dirty}"\n'
                    'wk_tools=unknown\n'
                    + block
                    + 'echo "wk_tools=$wk_tools"\n')

    def test_a_clean_tree_records_its_sha_and_carries_on(self):
        cp = self._record("no")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("wk_tools=abc", cp.stdout)

    def test_a_dirty_tree_says_so(self):
        cp = self._record("yes")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("wk_tools=abc+dirty", cp.stdout)

    def test_the_record_is_not_built_by_a_trailing_and(self):
        """The shape that caused it: a `&&` list whose status becomes the
        assignment's, under `set -e`."""
        body = func_body((REPO / "cmd" / "bench").read_text(), "cmd_stage")
        for line in body.splitlines():
            if "wk_tools=" in line and "kv_get" in line:
                self.assertNotIn("&&", line, line)
