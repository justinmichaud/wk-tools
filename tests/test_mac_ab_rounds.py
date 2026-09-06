"""The unattended macOS A/B's round loop (bench/mac-bench-autorun.sh) and what
`wk bench mac-ab` plants for it (bench/mac-ab.sh).

The script itself only runs on a benchmark install, so what is exercised here
is every part of it that is a function or a decision: the job it reads, the
run map it writes, the arm order, and the stopping rule's inputs.

Run: python3 -m unittest tests.test_mac_ab_rounds -v
"""
import json
import re
import subprocess
import unittest

from tests.support import REPO, WkTest, func_body, scratch_dir

AUTORUN = REPO / "bench" / "mac-bench-autorun.sh"
MACAB = REPO / "bench" / "mac-ab.sh"
SUMMARY = REPO / "bench" / "mac-ab-summary.sh"


def sh(script, cwd=None):
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          timeout=60, cwd=cwd)


class TestTheJobItReads(WkTest):
    """job.json is written by the driver and read on a machine with no network
    to ask about it, so the two have to agree exactly."""

    def _jf(self, doc, verb, field):
        text = AUTORUN.read_text()
        with scratch_dir() as tmp:
            job = tmp / "job.json"
            job.write_text(json.dumps(doc))
            body = func_body(text, "jf") if verb == "jf" else func_body(text, "jf_list")
            return sh(f'JOB={job}\n{verb}() {{{body}}}\n{verb} {field}').stdout.strip()

    def test_the_plans_come_back_as_a_list(self):
        got = self._jf({"plans": ["jetstream3", "speedometer3", "motionmark"]},
                       "jf_list", "plans")
        self.assertEqual(got, "jetstream3 speedometer3 motionmark")

    def test_a_plans_field_that_is_not_a_list_reads_as_absent(self):
        self.assertEqual(self._jf({"plans": "speedometer3"}, "jf_list", "plans"), "")

    def test_the_stopping_rule_is_carried_in_the_job(self):
        doc = {"plans": ["jetstream3"], "detect_pct": 0.3, "max_rounds": 40, "rounds": 5}
        self.assertEqual(self._jf(doc, "jf", "detect_pct"), "0.3")
        self.assertEqual(self._jf(doc, "jf", "max_rounds"), "40")

    def test_the_driver_writes_exactly_those_fields(self):
        text = MACAB.read_text()
        for field in ('"plans"', '"detect_pct"', '"max_rounds"', '"rounds"'):
            self.assertIn(field, text, f"mac-ab.sh never writes {field}")


class TestTheRunMap(WkTest):
    """runs.tsv is the only record of which result belongs to which arm and
    plan; the summary and the stopping rule both read it."""

    COLUMNS = 6

    def test_a_row_names_its_plan(self):
        line = [l for l in AUTORUN.read_text().splitlines()
                if "runs.tsv" in l and "printf" in l]
        self.assertEqual(len(line), 1, line)
        self.assertEqual(line[0].count("%s"), self.COLUMNS)
        self.assertIn('"$plan"', line[0])

    def test_only_uncontaminated_rounds_reach_the_stopping_rule(self):
        """A software-update scan across an arm is a number to drop, and the
        rule must not be talked into stopping by one."""
        body = func_body(AUTORUN.read_text(), "arm_results")
        self.assertIn('$5 == "clean"', body)
        self.assertIn("$6 == p", body)

    def test_arm_results_selects_by_plan_and_arm(self):
        with scratch_dir() as tmp:
            runs = tmp / "runs.tsv"
            runs.write_text(
                "1\tA\tsid-a\tr1\tclean\tjetstream3\n"
                "1\tB\tsid-b\tr2\tclean\tjetstream3\n"
                "1\tA\tsid-a\tr3\tclean\tmotionmark\n"
                "2\tA\tsid-a\tr4\tscanned\tjetstream3\n")
            body = func_body(AUTORUN.read_text(), "arm_results")
            out = sh(f'WK_AB_ROOT=/var/wk\nRUNS={tmp}\n'
                     f'arm_results() {{{body}}}\narm_results jetstream3 A').stdout
            self.assertEqual(out, "/var/wk/results/r1/result.json")

    def test_the_warmup_round_is_removed_before_anything_is_compared(self):
        text = AUTORUN.read_text()
        self.assertIn("grep -v '^0\t' \"$RUNS/runs.tsv\"", text)


class TestTheRounds(WkTest):
    def test_the_arms_are_counterbalanced_and_not_merely_alternated(self):
        """Alternating is not counterbalancing: if one arm always goes first,
        a monotonic drift lands entirely on the other."""
        text = AUTORUN.read_text()
        self.assertIn("r % 2", text)
        expr = [l for l in text.splitlines() if "NARMS - 1 - i" in l]
        self.assertEqual(len(expr), 1, expr)

    def test_the_order_it_produces_is_abba(self):
        out = sh('''
NARMS=2
for r in 1 2 3 4; do
  i=0
  while [ "$i" -lt "$NARMS" ]; do
    if [ $((r % 2)) -eq 1 ]; then arm=$i; else arm=$((NARMS - 1 - i)); fi
    printf '%s' "$arm"
    i=$((i + 1))
  done
done''').stdout.strip()
        self.assertEqual(out, "01100110")

    def test_the_watchdog_watches_silence_and_not_a_total(self):
        """The round count is decided by the numbers as they arrive, so there
        is no total to budget; what a hang looks like is a log that stops."""
        text = AUTORUN.read_text()
        self.assertNotIn("DEADLINE=", text)
        line = [l for l in text.splitlines() if l.startswith("STALL=")]
        self.assertEqual(len(line), 1, line)
        self.assertIn("TIMEOUT", line[0])
        self.assertIn("stat -f %m", text)

    def test_it_stops_on_precision_and_says_so_when_it_could_not(self):
        text = AUTORUN.read_text()
        self.assertIn("ab-precision", text)
        self.assertIn("hit-max-rounds", text)
        self.assertIn("resolved-at-round-", text)

    def test_detect_zero_runs_a_fixed_number_of_rounds(self):
        self.assertIn('[ "$DETECT" != 0 ]', AUTORUN.read_text())


class TestOneStatistic(WkTest):
    """The verdict and the stopping rule are the same computation; a second
    copy in the summary could say the experiment was fine enough to stop and
    then report a different threshold."""

    def test_the_summary_asks_wkdata_rather_than_computing_its_own(self):
        text = SUMMARY.read_text()
        self.assertIn("ab-precision", text)
        self.assertNotIn("statistics", text)
        self.assertNotIn("variance(", text)

    def test_the_summary_reports_per_plan(self):
        text = SUMMARY.read_text()
        self.assertIn("for plan in $plans", text)


if __name__ == "__main__":
    unittest.main()
