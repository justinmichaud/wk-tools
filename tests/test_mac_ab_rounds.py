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

    def _rounds_run(self, detect, rounds=2, max_rounds=40, resolves=False):
        """The real loop, with the leg and the precision check stubbed: how
        long an experiment is is the only thing this part of the script decides."""
        text = AUTORUN.read_text()
        loop = text[text.index('any_ok=""'):text.index("# No `wk quiesce off`")]
        prelude = (
            "DETECT=%s; ROUNDS=%s; MAX_ROUNDS=%s\n"
            "PLANS=jetstream3; NARMS=2\n"
            "leg() { printf 'round %%s\\n' \"$1\" >&2; return 0; }\n"
            "say() { :; }\n"
            "state_set() { printf 'state %%s=%%s\\n' \"$1\" \"$2\" >&2; }\n"
            "plan_resolves() { return %s; }\n"
        ) % (detect, rounds, max_rounds, 0 if resolves else 1)
        return sh(prelude + loop)

    @staticmethod
    def _rounds_done(cp):
        return [l.split("=", 1)[1] for l in cp.stderr.splitlines()
                if l.startswith("state rounds_done=")]

    def test_detect_zero_runs_exactly_the_rounds_asked_for(self):
        """`--detect 0` turns the stopping rule off; without this the loop ran
        to --max-rounds, so a one-round smoke test was forty rounds long."""
        cp = self._rounds_run(detect=0, rounds=2, max_rounds=40)
        self.assertEqual(self._rounds_done(cp), ["1", "2"], cp.stderr)
        self.assertIn("state outcome=rounds-done", cp.stderr)

    def test_a_target_it_cannot_reach_stops_at_the_ceiling(self):
        cp = self._rounds_run(detect="0.3", rounds=2, max_rounds=4, resolves=False)
        self.assertEqual(self._rounds_done(cp), ["1", "2", "3", "4"], cp.stderr)
        self.assertIn("state outcome=hit-max-rounds", cp.stderr)

    def test_a_target_it_reaches_stops_at_the_floor(self):
        """--rounds is the floor: the precision check is not consulted before it."""
        cp = self._rounds_run(detect="0.3", rounds=3, max_rounds=40, resolves=True)
        self.assertEqual(self._rounds_done(cp), ["1", "2", "3"], cp.stderr)
        self.assertIn("state outcome=resolved-at-round-3", cp.stderr)


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


class TestProvisioningIsNotSomethingToKill(WkTest):
    """The first-boot daemon provisions the volume -- including the desktop
    quieting every leg's preflight then requires -- and removes itself at the
    end, so an installed daemon means provisioning is unfinished. Both sides
    of the experiment read the one record of that: the completion line in the
    volume's own first-boot log."""

    def _defuse(self, plist=True, complete=False, running=False):
        """The function as the agent runs it: `set -e`, sudo's output
        discarded, and the daemon's files really there to remove."""
        text = AUTORUN.read_text()
        with scratch_dir() as tmp:
            log = tmp / "log"
            log.write_text("[wk-bench] installing Tailscale\n"
                           + ("=== first boot provisioning complete ===\n" if complete else ""))
            fb, self_sh, killed = tmp / "plist", tmp / "self", tmp / "killed"
            if plist:
                fb.write_text("<plist/>")
                self_sh.write_text("#!/bin/bash\n")
            cp = sh(
                f'set -euo pipefail\n'
                f'FB_PLIST={fb}; FB_SELF={self_sh}; FB_LOG={log}\n'
                f'say() {{ printf "%s\\n" "$*"; }}\n'
                f'cancel_pending_reboot() {{ say CANCELLED; }}\n'
                f'pgrep() {{ return {0 if running else 1}; }}\n'
                f'pkill() {{ : > {killed}; }}\n'
                f'sudo() {{ shift; "$@"; }}\n'
                f'fb_provisioned() {{{func_body(text, "fb_provisioned")}}}\n'
                f'defuse_firstboot() {{{func_body(text, "defuse_firstboot")}}}\n'
                f'if defuse_firstboot; then ret=0; else ret=$?; fi\n'
                f'printf "RET=%s\\n" "$ret"\n')
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            return cp.stdout + cp.stderr, fb.exists(), killed.exists()

    def test_it_stands_aside_while_provisioning_is_unfinished(self):
        out, still_there, killed = self._defuse(complete=False, running=True)
        self.assertIn("RET=1", out, out)
        self.assertTrue(still_there, "it removed a daemon that had not finished:\n" + out)
        self.assertFalse(killed, "it killed provisioning:\n" + out)
        self.assertNotIn("CANCELLED", out,
                         "it cancelled the reboot provisioning ends with:\n" + out)

    def test_a_stopped_unfinished_daemon_names_the_repair(self):
        out, _, _ = self._defuse(complete=False, running=False)
        self.assertIn("mac-volume --repair", out, out)

    def test_a_completed_daemon_that_could_not_remove_itself_is_defused(self):
        out, still_there, killed = self._defuse(complete=True, running=True)
        self.assertIn("RET=0", out, out)
        self.assertFalse(still_there, "it left a completed daemon installed:\n" + out)
        self.assertTrue(killed, "its re-run reboots the machine mid-round:\n" + out)
        self.assertIn("CANCELLED", out, out)

    def test_no_daemon_at_all_is_an_ordinary_boot(self):
        out, _, _ = self._defuse(plist=False)
        self.assertIn("RET=0", out, out)
        self.assertIn("CANCELLED", out, out)

    def test_the_autorun_measures_nothing_on_a_boot_it_stood_aside_on(self):
        text = AUTORUN.read_text()
        self.assertIn("if ! defuse_firstboot; then", text,
                      "defuse_firstboot's refusal is not acted on")

    def test_the_driver_reads_the_same_record(self):
        """One rule, two readers: the volume's log, not a proxy for it."""
        text = MACAB.read_text()
        self.assertIn("provisioning complete", text)
        self.assertIn("wk-bench-firstboot.log", text)

    def _provisioned(self, log_lines):
        text = MACAB.read_text()
        with scratch_dir() as tmp:
            (tmp / "private" / "var" / "log").mkdir(parents=True)
            (tmp / "private" / "var" / "log" / "wk-bench-firstboot.log").write_text(log_lines)
            cp = sh(
                f'set -euo pipefail\n'
                f'. {REPO}/lib/common.sh\n'
                f'mac() {{ bash -c "$1"; }}\n'
                f'bench_root() {{ printf "%s" {tmp}/private/var/wk; }}\n'
                f'firstboot_log() {{{func_body(text, "firstboot_log")}}}\n'
                f'volume_provisioned() {{{func_body(text, "volume_provisioned")}}}\n'
                f'if volume_provisioned; then echo YES; else echo NO; fi\n')
            self.assertEqual(cp.returncode, 0,
                             f"the reader itself failed: {cp.stdout}{cp.stderr}")
            return cp.stdout.strip().splitlines()[-1]

    def test_a_volume_that_never_finished_reads_as_unprovisioned(self):
        self.assertEqual(self._provisioned("[wk-bench] installing Tailscale\n"), "NO")

    def test_a_grep_that_finds_nothing_is_not_a_failure_of_the_reader(self):
        """`grep -c` exits 1 with no match, and under `set -e` that would kill
        the preflight rather than fail one check in it."""
        self.assertEqual(self._provisioned(""), "NO")

    def test_a_completed_first_boot_reads_as_provisioned(self):
        self.assertEqual(
            self._provisioned("=== first boot provisioning complete ===\n"), "YES")

    def test_the_preflight_refuses_the_plant_rather_than_noting_it(self):
        """A note is read by nobody at 4am; a failed check is what stops the
        plant, and the refusal arrives while the volume is still mountable."""
        body = func_body(MACAB.read_text(), "preflight")
        self.assertIn('ck no "provisioned"', body)
        self.assertIn('ck yes "provisioned"', body)
