"""An A/B's rounds and its back half: the stopping rule every system asks between rounds (--rounds the floor,
--detect the precision to resolve, --max-rounds the ceiling; lib/wk/bench/board_ab.py and report.py), the A/B summary
a Mac's benchmark install writes (report.ab_summary), and a planted Mac A/B read back -- --status, --progress,
--collect (lib/wk/bench/mac.py's MacAB) -- against FakeMac and the fake clock.

Run: python3 tests/run.py --unit -k test_mac_ab_rounds
"""
import base64
import contextlib
import io
import json
import os
import sys
import tarfile
import unittest
from unittest import mock

from tests.support import REPO, WkTest, requires_machine, scratch_dir, temp_store
from tests.test_ab_precision import speedometer_doc, write_runs
from tests.test_mac_ab_driver import ready, said, world

sys.path.insert(0, str(REPO / "lib"))
from wk import targets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.bench import ab, board_ab, mac, record, report  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402

QUIET = [100.0, 100.02, 99.98, 100.0, 100.01, 99.99]
NOISY = [99.0, 101.0, 99.0, 101.0, 99.0, 101.0]
STAMP = "20260908T000000Z"


def refused(fn, *args):
    got, err = said(fn, *args)
    assert got is Refused, err
    return err


class TestTheStoppingRule(WkTest):
    def test_a_board_runs_its_rounds_exactly_unless_told_a_precision(self):
        self.assertEqual(board_ab.stopping({}, 5), (5, 0.0))

    def test_a_precision_asked_for_goes_on_up_to_the_ceiling(self):
        self.assertEqual(board_ab.stopping({"detect": "0.3"}, 5), (board_ab.MAX_ROUNDS, 0.3))
        self.assertEqual(board_ab.stopping({"detect": "0.3", "max_rounds": "12"}, 5), (12, 0.3))

    def test_a_mac_resolves_a_third_of_a_per_cent_by_default_and_0_turns_it_off(self):
        self.assertEqual(board_ab.stopping({}, 5, mac.DETECT), (board_ab.MAX_ROUNDS, 0.3))
        self.assertEqual(board_ab.stopping({"detect": "0.0"}, 5, mac.DETECT), (5, 0.0))

    def test_a_ceiling_below_the_floor_and_a_non_percentage_are_refused(self):
        self.assertIn("below --rounds", refused(board_ab.stopping, {"detect": "0.3", "max_rounds": "4"}, 9))
        self.assertIn("not a percentage", refused(board_ab.stopping, {"detect": "a lot"}, 5))
        self.assertIn("not a percentage", refused(board_ab.stopping, {"detect": "-1"}, 5))
        self.assertIn("takes a number", refused(board_ab.stopping, {"detect": "0.3", "max_rounds": "x"}, 5))

    def test_a_fleet_ab_hands_the_rule_to_each_board_as_the_command_it_prints(self):
        with temp_store() as store:
            reg = targets.Registry(REPO, env={"WK_STORE": store["WK_STORE"], "HOME": "/nonexistent"}, machine=Fake())
            a = ab.AB(REPO, reg, FakeClock(), "", {"devices": "rpi5", "systems": "a,b", "detect": "0.5", "max_rounds": "9"})
            a.check()

            class Dev:
                name, lanes = "rpi5", [("ws", "")]
            _, o = a.bench_options(Dev())
            self.assertEqual((o["max_rounds"], o["detect"]), ("9", "0.5"))
            self.assertIn("--max-rounds 9 --detect 0.5", a.bench_command(Dev(), "speedometer3"))


class Rounds:
    """board_ab.AB's round loop alone: each leg answers `legs`, and `resolves` says after which round the rounds so far
    resolve the target."""

    def __init__(self, rounds, max_rounds, detect, resolves_at=None, lost=()):
        self.ab = board_ab.AB.__new__(board_ab.AB)
        self.ab.rounds, self.ab.max_rounds, self.ab.detect = rounds, max_rounds, detect
        self.ab.systems, self.ab.labels = False, ("a", "b")
        self.asked, self.legs = [], []
        self.ab.arm_leg = lambda arm, o: self.legs.append(int(o["round"])) or int(o["round"]) not in lost
        self.ab.resolved = lambda: self.asked.append(max(self.legs)) or (resolves_at is not None and max(self.legs) >= resolves_at)

    def run(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            kept, lost = self.ab.measured()
        return kept, lost, sorted(set(self.legs)), err.getvalue()


class TestTheRoundsStopWhenTheyResolve(WkTest):
    def test_detect_zero_runs_exactly_the_rounds_asked_for_and_asks_nothing(self):
        r = Rounds(2, 2, 0.0)
        kept, _, rounds, _ = r.run()
        self.assertEqual((kept, rounds, r.asked), (2, [1, 2], []))

    def test_the_rule_is_not_asked_before_the_floor(self):
        r = Rounds(3, 40, 0.3, resolves_at=1)
        _, _, rounds, err = r.run()
        self.assertEqual(rounds, [1, 2, 3])
        self.assertEqual(r.asked, [3])
        self.assertIn("resolved", err)

    def test_it_stops_at_the_round_that_resolves(self):
        _, _, rounds, _ = Rounds(2, 40, 0.3, resolves_at=6).run()
        self.assertEqual(rounds, [1, 2, 3, 4, 5, 6])

    def test_a_target_it_cannot_reach_stops_at_the_ceiling_and_says_so(self):
        _, _, rounds, err = Rounds(2, 4, 0.3).run()
        self.assertEqual(rounds, [1, 2, 3, 4])
        self.assertIn("--max-rounds 4 reached", err)

    def test_lost_rounds_still_end_it_early(self):
        kept, lost, rounds, _ = Rounds(2, 40, 0.3, lost=(3, 4, 5)).run()
        self.assertEqual((kept, lost, rounds[-1]), (2, 3, 5))


class TestResolvedIsThePrecisionRule(WkTest):
    def test_quiet_rounds_resolve_and_noisy_ones_do_not(self):
        with scratch_dir() as tmp:
            self.assertTrue(report.resolved(write_runs(tmp, "qa", QUIET), write_runs(tmp, "qb", QUIET[::-1]), 0.3))
            self.assertFalse(report.resolved(write_runs(tmp, "na", NOISY), write_runs(tmp, "nb", [v + 0.5 for v in NOISY]), 0.3))

    def test_rounds_with_no_score_resolve_nothing(self):
        with scratch_dir() as tmp:
            self.assertFalse(report.resolved([str(tmp / "absent")], [str(tmp / "absent")], 0.3))

    def test_a_board_asks_it_of_its_own_task(self):
        """Paired the way the report pairs them: a round both arms finished, from this task's runs."""
        with scratch_dir() as tmp:
            taskdir = tmp / "t"
            record.task_write(str(taskdir), ["task=t", "requested=x", "devices=rpi5", "plans=speedometer3", "rounds=2", "slots=a,b"], ["wk"])
            for i, (va, vb) in enumerate(zip(QUIET, QUIET[::-1]), 1):
                for arm, v in (("a", va), ("b", vb)):
                    d = taskdir / "runs" / ("r%d%s" % (i, arm))
                    d.mkdir(parents=True)
                    (d / "result.json").write_text(json.dumps(speedometer_doc([[v]])))
                    (d / "env.json").write_text(json.dumps({"machine": "rpi5", "plan": "speedometer3", "ab": {"round": str(i), "arm": arm}}))
            a = board_ab.AB.__new__(board_ab.AB)
            a.taskdir, a.name, a.plan, a.labels, a.detect = str(taskdir), "rpi5", "speedometer3", ("a", "b"), 0.3
            self.assertTrue(a.resolved())
            a.plan = "jetstream3"
            self.assertFalse(a.resolved())


def volume(root, legs):
    """A benchmark volume's results/ and its run map: `legs` is (round, arm, staged, run, clean, plan, score)."""
    rows = []
    for rnd, arm, sid, rid, clean, plan, score in legs:
        d = root / "results" / rid
        d.mkdir(parents=True)
        (d / "result.json").write_text(json.dumps(speedometer_doc([[score]])))
        (d / "env.json").write_text(json.dumps({"plan": plan, "workspace": "wk-bench", "wall_time_s": "60"}))
        rows.append("\t".join((rnd, arm, sid, rid, clean, plan)))
    runs = root / "ab" / STAMP / "runs.tsv"
    runs.parent.mkdir(parents=True)
    runs.write_text("\n".join(rows) + "\n")
    return runs


def ab_legs(values_a, values_b, plan="speedometer3", same=False):
    out = [("0", "A", "sid-a", "w0", "clean", plan, 1.0), ("0", "B", "sid-b", "w1", "clean", plan, 1.0)]
    for i, (va, vb) in enumerate(zip(values_a, values_b), 1):
        out += [(str(i), "A", "sid-a", "a%d" % i, "clean", plan, va), (str(i), "B", "sid-a" if same else "sid-b", "b%d" % i, "clean", plan, vb)]
    return out


class TestTheSummary(WkTest):
    """The verdict a Mac's install writes beside its results: per plan, the precision the rounds stopped on, then A
    against B."""

    def summary(self, legs, out=""):
        with scratch_dir() as root:
            runs = volume(root, legs)
            buf = io.StringIO()
            with contextlib.redirect_stderr(io.StringIO()):
                rc = report.ab_summary(str(runs), str(root), "2026-09-08T00:00:00Z", str(root / out) if out else "", out=buf)
            written = {p.name: p.read_text() for p in root.iterdir() if p.is_file()}
            return rc, buf.getvalue(), written

    def test_each_plan_reports_its_precision_and_the_comparison(self):
        rc, text, _ = self.summary(ab_legs(QUIET, QUIET[::-1]))
        self.assertEqual(rc, 0)
        self.assertIn("================ speedometer3 ================", text)
        self.assertIn("    met=yes", text)
        self.assertIn("comparing arm A against arm B", text)

    def test_the_warmup_round_is_left_out(self):
        _, text, _ = self.summary(ab_legs(QUIET, QUIET[::-1]))
        self.assertIn("arm A: 6 run(s)", text)
        self.assertIn("    n_a=6", text)

    def test_a_scanned_leg_is_named_and_kept(self):
        legs = ab_legs(QUIET, QUIET[::-1])
        legs[2] = legs[2][:4] + ("scanned",) + legs[2][5:]
        _, text, _ = self.summary(legs)
        self.assertIn("round 1 arm A", text)
        self.assertIn("arm A: 6 run(s)", text)

    def test_one_staged_build_on_both_arms_is_called_an_a_a_control(self):
        _, text, _ = self.summary(ab_legs(QUIET, QUIET, same=True))
        self.assertIn("A/A control", text)

    def test_one_arm_is_nothing_to_compare(self):
        _, text, _ = self.summary([("1", "A", "sid-a", "a1", "clean", "speedometer3", 1.0)])
        self.assertIn("nothing to compare", text)
        self.assertNotIn("comparing", text)

    def test_out_writes_the_same_text_and_a_page_per_plan(self):
        _, text, written = self.summary(ab_legs(QUIET, QUIET[::-1]), out="summary.txt")
        self.assertEqual(written["summary.txt"], text)
        self.assertIn("summary-speedometer3.html", written)

    def test_the_command_refuses_a_missing_run_map(self):
        from wk.bench import cli
        bench = cli.Bench(REPO, targets.Registry(REPO, env={"HOME": "/nonexistent"}, machine=Fake()), FakeClock())
        self.assertIn("no run map", refused(bench.ab_summary, "/nonexistent/runs.tsv", "/var/wk", ""))


@contextlib.contextmanager
def planted(legs=(), state=None, **o):
    """A Mac A/B planted and read back: the volume a scratch directory, served off FakeMac's host channel under the
    path the driver resolves, and this machine's extraction of a leg real."""
    with world(**o) as m, scratch_dir() as vol:
        ready(m)
        root = m.d.bench_root()
        runs = volume(vol, legs) if legs else None
        m.fake.files[root + "/autorun.state"] = state if state is not None else "job_stamp=%s\nphase=done\noutcome=ran\n" % STAMP
        if runs:
            m.fake.files["%s/ab/%s/runs.tsv" % (root, STAMP)] = runs.read_text()

        def packed(key):
            rid = key.split()[-1]
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as t:
                t.add(str(vol / "results" / rid), arcname=rid)
            return Result(0, base64.b64encode(buf.getvalue()).decode() + "\n")
        m.fake.answer(r"^tar ", packed)

        def unpack(argv, fake):
            with tarfile.open(fileobj=io.BytesIO(base64.b64decode(fake.files[argv[4]]))) as t:
                t.extractall(argv[5])
            return Result(0)
        m.here_fake.react(["sh", "-c", 'base64 -d < "$1" | tar -xf - -C "$2"'], unpack)
        m.root_ = root
        yield m


class TestTheCollectRecordsOntoTheTask(WkTest):
    def collect(self, m):
        m.create_task(STAMP)
        m.lock.release_all()
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            rc = m.read_collect()
        err = err.getvalue()
        runs = os.path.join(m.taskdir, "runs")
        recorded = {d: record.load(os.path.join(runs, d, "env.json")) for d in sorted(os.listdir(runs)) if os.path.isdir(os.path.join(runs, d))}
        return rc, recorded, err, out.getvalue()

    def test_each_clean_leg_lands_as_a_run_paired_with_its_round_and_arm(self):
        with planted(ab_legs(QUIET[:2], QUIET[:2])) as m:
            rc, recorded, err, _ = self.collect(m)
        self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(recorded), ["a1", "a2", "b1", "b2"])
        self.assertEqual(recorded["a1"]["ab"], {"round": "1", "arm": "a", "staged": "sid-a"})
        self.assertEqual((recorded["b2"]["machine"], recorded["b2"]["plan"]), ("mbp", "speedometer3"))

    def test_the_warmup_round_and_a_contaminated_leg_are_not_recorded(self):
        legs = ab_legs(QUIET[:2], QUIET[:2])
        legs[2] = legs[2][:4] + ("scanned",) + legs[2][5:]
        with planted(legs) as m:
            _, recorded, err, _ = self.collect(m)
        self.assertEqual(sorted(recorded), ["a2", "b1", "b2"], err)

    def test_the_task_is_reported_out_of_the_legs_it_recorded(self):
        with planted(ab_legs(QUIET, QUIET[::-1])) as m:
            _, _, err, out = self.collect(m)
        self.assertIn("rounds: 6 usable", out, err)

    def test_the_same_collect_reads_a_run_from_bench_mode(self):
        """One implementation, two channels: in bench mode the install is read over its own node at /var/wk."""
        with planted(ab_legs(QUIET[:2], QUIET[:2])) as m:
            m.fake.enter_bench()
            m.d.probe()
            root = m.d.bench_root()
            self.assertEqual(root, "/var/wk")
            for k in [k for k in m.fake.files if k.startswith(m.root_)]:
                m.fake.files[root + k[len(m.root_):]] = m.fake.files[k]
            _, recorded, err, _ = self.collect(m)
        self.assertEqual(sorted(recorded), ["a1", "a2", "b1", "b2"], err)

    def test_no_run_map_records_nothing_and_names_the_status(self):
        with planted() as m:
            m.create_task(STAMP)
            rc, err = said(m.read_collect)
        self.assertEqual(rc, 1)
        self.assertIn("no run map", err)
        self.assertIn("wk bench ab --devices mbp --status", err)

    def test_a_job_with_no_task_here_is_refused(self):
        with planted(ab_legs(QUIET[:2], QUIET[:2])) as m:
            got, err = said(m.read_collect)
        self.assertIs(got, Refused)
        self.assertIn("has no task", err)

    def test_a_dry_run_records_nothing(self):
        with planted(ab_legs(QUIET[:2], QUIET[:2])) as m, mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
            m.create_task(STAMP)
            os.makedirs(os.path.join(m.taskdir, "runs"))
            rc, err = said(m.read_collect)
            self.assertEqual(os.listdir(os.path.join(m.taskdir, "runs")), [], err)


class TestStatusAndProgressRead(WkTest):
    def test_status_shows_the_job_the_state_the_legs_and_the_log(self):
        with planted() as m:
            m.fake.files[m.root_ + "/job.json"] = '{"stamp": "%s"}' % STAMP
            m.fake.answer(r"ab-legs", Result(0, "3 of 14 planned\n"))
            m.fake.answer(r"^tail -20", Result(0, "leg 3 ended\n"))
            rc, err = said(m.read_status)
        self.assertEqual(rc, 0, err)
        for line in ('  {"stamp": "%s"}' % STAMP, "  outcome=ran", "    3 of 14 planned", "    leg 3 ended"):
            self.assertIn(line, err.splitlines())
        self.assertIn("host mode", err)

    def test_progress_off_both_nodes_is_the_run_under_way(self):
        with world() as m:
            m.fake.up = False
            ready(m)
            rc, err = said(m.read_progress)
        self.assertEqual(rc, 0)
        self.assertIn("[~] 1. the run is under way", err)

    def test_progress_in_bench_mode_is_the_run_itself(self):
        with world() as m:
            m.fake.enter_bench()
            ready(m)
            _, err = said(m.read_progress)
        self.assertIn("[x] 1. the machine is in bench mode", err)

    def test_progress_in_host_mode_walks_every_step_and_changes_nothing(self):
        with planted() as m:
            m.fake.answer(r"^PlistBuddy", Result(0, "26.1\n"))
            m.fake.answer(r"^gated", Result(0))
            for ident in ("sid-a", "sid-b"):
                m.fake.files["%s/staged/%s/stage.json" % (m.root_, ident)] = '{"webkit_sha": "%s"}' % ("c" * 40)
            m.fake.files[m.root_ + "/job.json"] = json.dumps({"arms": [{"id": "sid-a"}, {"id": "sid-b"}]})
            m.fake.files[m.root_ + "/autorun.state"] = "rounds_done=1\nrounds_done=4\noutcome=resolved-at-round-4\n"
            _, err = said(m.read_progress)
            self.assertEqual([e for e in m.fake.effects if e[0] != "push"], [])
        for want in ("[x] 1. the benchmark volume exists", "[x] 2. it is provisioned", "[x] 3. two arms are built and staged",
                     "[x] 4. each arm can prove how it was collected", "[ ] 5. the machine is in bench mode",
                     "[x] 6. a job is planted for the staged arms", "[x] 7. the rounds are done", "[ ] 8. the result is read back"):
            self.assertIn("  " + want, err.splitlines())
        self.assertIn("4 round(s), outcome resolved-at-round-4", err)

    def test_a_job_for_arms_no_longer_staged_is_an_older_experiments(self):
        with planted() as m:
            m.fake.answer(r"^PlistBuddy", Result(0, "26.1\n"))
            m.fake.files[m.root_ + "/job.json"] = json.dumps({"arms": [{"id": "sid-old"}]})
            _, err = said(m.read_progress)
        self.assertIn("older experiment's", err)
        self.assertIn("nothing has run for these arms", err)


class TestTheReadingsAreOneAtATime(WkTest):
    def test_two_readings_or_a_plant_option_beside_one_are_refused(self):
        with world(systems="", status=True, collect=True) as m:
            self.assertIn("one reading at a time", refused(m.back))
        with world(status=True) as m:
            self.assertIn("takes only --devices", refused(m.back))

    def test_a_board_is_refused_a_macs_reading(self):
        with temp_store() as store:
            reg = targets.Registry(REPO, env={"WK_STORE": store["WK_STORE"], "HOME": "/nonexistent"}, machine=Fake())
            err = refused(ab.run, REPO, reg, FakeClock(), "", {"devices": "rpi5", "systems": "a,b", "status": True})
        self.assertIn("--status is a Mac A/B's", err)

    def test_mac_ab_is_a_tombstone_naming_the_readings(self):
        from wk.bench import cli
        bench = cli.Bench(REPO, targets.Registry(REPO, env={"HOME": "/nonexistent"}, machine=Fake()), FakeClock())
        self.assertIn("wk bench ab --devices <mac> --preflight|--progress|--status|--collect", refused(bench.mac_ab))


class TestTheLiveRows(unittest.TestCase):
    """Read-only halves: the planted job on mbp as its steps and its legs. The rows themselves -- a PR-sized delta
    resolved with --count and --rounds varied, and the warmup's profile captured and symbolicated on every system --
    spend hours of each machine, and are the live tier's to run."""

    def back(self, reading):
        reg = targets.Registry(REPO, machine=None)
        m = mac.MacAB(REPO, reg, FakeClock(), "", {"devices": "mbp", reading: True})
        got, err = said(m.back)
        self.assertIn(got, (0, 1, Refused), err)
        return err

    @requires_machine("tolken")
    def test_ab_resolution_mbp(self):
        """`live ab.resolution[mbp]`, its read-only half."""
        self.assertIn("step by step", self.back("progress"))

    @requires_machine("tolken")
    def test_bench_warmup_profile_mbp(self):
        """`live bench.warmup_profile[<s>]` for mbp, its read-only half: --status names the warmup's captures."""
        err = self.back("status")
        self.assertTrue("warmup captures" in err or "nothing on mbp is readable" in err, err)


if __name__ == "__main__":
    unittest.main()
