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
        """A run is named by its directory, the way `wk bench report` and
        `wk bench precision` name one: `wkdata ab-precision` appends the file
        inside it, so a caller that appends one too names nothing."""
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
            self.assertEqual(out, "/var/wk/results/r1")

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
            "detect_off() { awk -v d=\"${DETECT:-0}\" 'BEGIN { exit !(d + 0 == 0) }'; }\n"
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
    """Provisioning is the settings every leg's preflight then measures, and
    it removes its own daemon and reboots at the end. So what says whether
    this volume can be measured on is the settings themselves, read here by
    the one probe and judged by the one set of findings -- and a daemon still
    in flight is a different question from a volume that drifted."""

    def _autorun(self, func, plist=True, settled=True, running=False):
        """One of the autorun's own functions, as the agent runs it: `set -e`,
        sudo's output discarded, and the daemon's files really there."""
        text = AUTORUN.read_text()
        with scratch_dir() as tmp:
            fb, self_sh, killed = tmp / "plist", tmp / "self", tmp / "killed"
            if plist:
                fb.write_text("<plist/>")
                self_sh.write_text("#!/bin/bash\n")
            rows = ("ok\tthe screen does not lock\t\n" if settled
                    else "wrong\tnot so: the screen does not lock "
                         "(askforpassword reads '1')\tfix\n")
            quiet = tmp / "quiet.sh"
            quiet.write_text(
                "wk_quiet_desktop_probe() { printf 'askforpassword=1\\n'; }\n"
                "wk_quiet_desktop_findings() { cat <<'ROWS'\n" + rows + "ROWS\n}\n")
            cp = sh(
                f'set -euo pipefail\n'
                f'FB_PLIST={fb}; FB_SELF={self_sh}; QUIET_DESKTOP={quiet}\n'
                f'say() {{ printf "%s\\n" "$*"; }}\n'
                f'cancel_pending_reboot() {{ say CANCELLED; }}\n'
                f'leave_bench() {{ say "LEAVE: $1"; }}\n'
                f'pgrep() {{ return {0 if running else 1}; }}\n'
                f'pkill() {{ : > {killed}; }}\n'
                f'sudo() {{ shift; "$@"; }}\n'
                f'_stay=""\n'
                f'stand_aside_if_provisioning() {{{func_body(text, "stand_aside_if_provisioning")}}}\n'
                f'refuse_unprovisioned() {{{func_body(text, "refuse_unprovisioned")}}}\n'
                f'defuse_firstboot() {{{func_body(text, "defuse_firstboot")}}}\n'
                f'if {func}; then ret=0; else ret=$?; fi\n'
                f'printf "RET=%s\\n" "$ret"\n')
            return cp, cp.stdout + cp.stderr, fb.exists(), killed.exists()

    def test_it_stands_aside_while_provisioning_is_running(self):
        cp, out, still_there, killed = self._autorun(
            "stand_aside_if_provisioning", settled=False, running=True)
        self.assertEqual(cp.returncode, 0, out)
        self.assertNotIn("RET=", out, "it ran on past provisioning:\n" + out)
        self.assertIn("standing aside", out, out)
        self.assertTrue(still_there, "it removed a daemon that had not finished:\n" + out)
        self.assertFalse(killed, "it killed provisioning:\n" + out)
        self.assertNotIn("CANCELLED", out,
                         "it cancelled the reboot provisioning ends with:\n" + out)
        self.assertNotIn("LEAVE", out, out)

    def test_a_volume_nothing_will_finish_hands_the_machine_back(self):
        """A setting that is not a measured Mac's refuses every leg, one leg
        after another, on a machine with no network to say so -- so it refuses
        the job instead, once, and says whether a daemon is there to fix it."""
        for plist in (True, False):
            cp, out, _, _ = self._autorun("refuse_unprovisioned",
                                          plist=plist, settled=False, running=False)
            self.assertEqual(cp.returncode, 0, out)
            self.assertNotIn("RET=", out, "it went on to measure:\n" + out)
            self.assertIn(f"daemon installed: {'yes' if plist else 'no'}", out, out)
            self.assertIn("the screen does not lock", out, out)
            self.assertIn("mac-volume --repair", out, out)
            self.assertIn("LEAVE: ", out, "it did not hand the machine back:\n" + out)

    def test_a_provisioned_volume_is_measured_on(self):
        cp, out, _, _ = self._autorun("refuse_unprovisioned", settled=True)
        self.assertIn("RET=0", out, out)
        self.assertNotIn("LEAVE", out, out)

    def test_a_daemon_that_outlived_its_provisioning_is_defused(self):
        cp, out, still_there, killed = self._autorun(
            "defuse_firstboot", running=True)
        self.assertIn("RET=0", out, out)
        self.assertFalse(still_there, "it left a completed daemon installed:\n" + out)
        self.assertTrue(killed, "its re-run reboots the machine mid-round:\n" + out)
        self.assertIn("CANCELLED", out, out)

    def test_no_daemon_at_all_is_an_ordinary_boot(self):
        cp, out, _, _ = self._autorun("defuse_firstboot", plist=False)
        self.assertIn("RET=0", out, out)
        self.assertIn("CANCELLED", out, out)

    def test_each_question_is_asked_where_its_answer_is_true(self):
        """Standing aside comes before the job is read, because defusing a
        daemon still provisioning is the thing it prevents. The judgment comes
        after `wk quiesce on`, because the user half of those rows does not
        survive this account's session starting and quiesce is what writes them
        again -- asked first, it refused a volume on rows the same boot was
        about to set (job 20260909T042343Z, 2026-09-09)."""
        text = AUTORUN.read_text()
        self.assertLess(text.index("\nstand_aside_if_provisioning\n"),
                        text.index('if [ ! -f "$JOB" ]'),
                        "the job is read before the daemon in flight is noticed")
        self.assertLess(text.index('"$TOOLS/wk" quiesce on'),
                        text.index("\nrefuse_unprovisioned\n"),
                        "the volume is judged before the quiesce that writes it")

    def test_one_path_names_the_log_and_the_rest_agree(self):
        """Three readers and two writers of one record: the daemon's plist
        tells launchd where to write, and nothing else may spell it
        differently. The autorun is not among them -- it runs on the volume,
        where the settings themselves can be read."""
        volume = (REPO / "bench" / "mac-bench-volume.sh").read_text()
        path = "/var/log/wk-bench-firstboot.log"
        self.assertEqual(volume.count(f"<string>{path}</string>"), 4,
                         "the plists no longer name that log twice each")
        self.assertIn(f"fblog={path}", volume)
        self.assertIn("/log/wk-bench-firstboot.log", MACAB.read_text())
        self.assertNotIn(path, AUTORUN.read_text())

    def test_a_dry_provision_does_not_promise_the_record(self):
        """The readback is what decides it, and a dry run has one: saying
        "would record" over a probe full of `--` lines is the same overstating
        the A/B's own dry run did."""
        volume = (REPO / "bench" / "mac-bench-volume.sh").read_text()
        self.assertIn("would record 'provisioning complete' in $fblog -- but only on a readback",
                      volume)

    def test_provisioning_by_hand_records_what_it_verified(self):
        """`wk bench mac-volume --provision` is the by-hand equivalent of the
        daemon, and refuses to run anywhere but on the volume -- so it writes
        the same line, and only when the readback is clean."""
        volume = (REPO / "bench" / "mac-bench-volume.sh").read_text()
        self.assertIn("provisioning complete (wk bench mac-volume --provision", volume)
        self.assertIn('elif [ "$quiet_ok" = yes ]; then', volume)
        self.assertLess(volume.index("quiet_ok=no"),
                        volume.index("provisioning complete (wk bench"),
                        "the record is written before the settings are read back")

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

    def test_the_refusal_can_be_crossed_to_deliver_the_fix(self):
        """The autorun that stands aside for provisioning reaches the volume
        only in a plant, so the refusal is a `barrier` -- forceable, recorded
        and warned about again at the end -- and not a die."""
        text = MACAB.read_text()
        self.assertIn("barrier ", text, "the preflight failure is not a barrier")
        self.assertNotIn('die "preflight failed', text)

    def test_force_is_one_flag_however_it_is_spelled(self):
        """`barrier` reads WK_FORCE, which the dispatcher sets; this command
        parses `--force` itself as well, and both mean the one thing. Spelt
        `:-`, because `${WK_...:+}` is `wk_forwarded_env`'s alone (tests/
        test_peer.py) and every use of FORCE here is an emptiness test."""
        text = MACAB.read_text()
        self.assertIn('FORCE="${WK_FORCE:-}"', text)
        self.assertIn("--force)    FORCE=1; WK_FORCE=1; export WK_FORCE", text)

    def test_the_preflight_refuses_the_plant_rather_than_noting_it(self):
        """A note is read by nobody at 4am; a failed check is what stops the
        plant, and the refusal arrives while the volume is still mountable."""
        body = func_body(MACAB.read_text(), "preflight")
        self.assertIn('ck no "provisioned"', body)
        self.assertIn('ck yes "provisioned"', body)


class TestEveryLegIsJudgedOnTheDeclaredDisplay(WkTest):
    """The once-per-boot browser check cannot see a panel attached between two
    legs, and MotionMark's score is the area it draws. So the expectation the
    job carries reaches every leg, not just the check at the top of the boot."""

    BENCH = REPO / "cmd" / "bench"

    def test_the_leg_passes_the_jobs_display_to_the_runner(self):
        body = func_body(AUTORUN.read_text(), "leg")
        self.assertIn('--expect-display "$DISPLAY_EXPECT"', body)

    def test_the_runner_takes_it_and_judges_it(self):
        text = self.BENCH.read_text()
        self.assertIn('--expect-display) EXPECT_DISPLAY="${2:-}"', text)
        self.assertIn("--displays-only", text)
        self.assertIn('check no "the display"', text)

    def test_the_rule_is_not_reimplemented_in_the_runner(self):
        """One implementation: the runner asks bench/mac-browser-check.py, the
        same file the once-per-boot check asks, rather than counting panels
        itself."""
        text = self.BENCH.read_text()
        self.assertNotIn("CGGetOnlineDisplayList", text)
        self.assertNotIn("displays are online, not one", text)

    def test_the_result_records_what_it_was_judged_against(self):
        """A stored run says which display it was compared on, so re-reading it
        reaches the same verdict with no argument."""
        self.assertIn('display_declared="$EXPECT_DISPLAY"', self.BENCH.read_text())

    def test_the_declared_mode_reaches_the_job_from_the_machine(self):
        """Through the driver, so the one machine whose mode is not in a conf
        of its own -- a guest, whose target declares it -- reaches the job the
        same way."""
        text = MACAB.read_text()
        self.assertIn("declared=$(b_display)", text)
        self.assertIn('WK_JOB_DISPLAY="$declared"', text)


class TestTheBenchSideRefusesBeforeItActs(WkTest):
    """Order is the safety here. The display is judged before the mode is
    written, because with a second panel attached there is no single built-in
    mode to converge to; and neither refusal spends an attempt, because a boot
    that measured nothing is not a try."""

    def test_the_display_is_judged_before_the_mode_is_written(self):
        text = AUTORUN.read_text()
        self.assertLess(text.index("\nrefuse_wrong_displays\n"),
                        text.index("\nconverge_display_mode\n"))

    def test_it_asks_the_one_file_that_holds_the_rule_about_the_topology_only(self):
        """The mode is converge_display_mode's job. Asking for it here would
        refuse the very state that function exists to fix, and the install
        would power off instead of converging."""
        body = func_body(AUTORUN.read_text(), "refuse_wrong_displays")
        self.assertIn("mac-browser-check.py", body)
        self.assertIn("--displays-only", body)
        self.assertNotIn("--expect-display", body)

    def test_neither_refusal_spends_an_attempt(self):
        text = AUTORUN.read_text()
        for func in ("refuse_wrong_displays", "converge_display_mode"):
            with self.subTest(func=func):
                self.assertIn('state_set attempts "$((ATTEMPTS - 1))"',
                              func_body(text, func))

    def test_a_wrong_display_powers_off_rather_than_rebooting(self):
        """A reboot lands back on this volume, and a monitor is not unplugged by
        one: it would be a loop that spends the machine and measures nothing."""
        body = func_body(AUTORUN.read_text(), "refuse_wrong_displays")
        self.assertIn("leave_bench", body)
        self.assertNotIn("reboot", body)


class TestADryRunClaimsNothing(WkTest):
    """`--dry-run` reports the plan and then says what it did, which is
    nothing: no claim of having planted, notified or rebooted may sit above the
    exit a dry run takes."""

    def test_one_place_decides_what_a_dry_run_says(self):
        text = MACAB.read_text()
        self.assertEqual(text.count("dry run -- nothing on $MACHINE was changed"), 1)

    def test_the_dry_exit_comes_before_every_claim_of_having_acted(self):
        text = MACAB.read_text()
        dry = text.index('if [ -n "$DRY" ]; then\n    [ "$ACTION" = plant ] || phase_go')
        for claim in ('info "planted and not started.',
                      'notify "mac-ab planted on $MACHINE"',
                      "came_back=$(phase_wait"):
            self.assertLess(dry, text.index(claim),
                            f"a dry run reaches `{claim[:40]}`")

    def test_it_says_which_gate_it_did_not_evaluate(self):
        """This dry run resolves the plant, from host mode. The gate that
        refuses a leg reads the running bench install, so a clean dry run here
        is not evidence that a leg would pass -- and it names the command that
        is."""
        text = MACAB.read_text()
        self.assertIn("not checked here: whether a leg would pass", text)
        self.assertIn("wk bench staged --plan jetstream3 --dry-run", text)

    def test_nothing_downstream_still_expects_a_dry_answer(self):
        """phase_wait cannot be reached in a dry run now, so neither its own
        `dry` answer nor the arm that read it may survive."""
        text = MACAB.read_text()
        self.assertNotIn("printf 'dry'", text)
        self.assertNotIn("    dry)", text)


class TestTheBrowserIsMeasuredBeforeTheRounds(WkTest):
    """`wk bench staged` judges 35 settings; the browser check judges the
    window itself -- WebGL, requestAnimationFrame, the screen it is on, and
    whether a WebKit GPU process held a client while it drew. The settings are
    the proxy; this is the thing, and an A/B that skips it can report a
    throttled window's numbers as the patch's."""

    def _check(self, staged=True, passes=True):
        text = AUTORUN.read_text()
        with scratch_dir() as tmp:
            root = tmp / "var-wk"
            products = root / "staged" / "sid-a" / "WebKitBuild" / "Release"
            if staged:
                products.mkdir(parents=True)
            runs = root / "ab" / "stamp"
            runs.mkdir(parents=True)
            left = tmp / "left"
            cp = sh(
                f'set -euo pipefail\n'
                f'WK_AB_ROOT={root}; RUNS={runs}; TOOLS={tmp}/tools; LOG=/dev/null\n'
                f'DISPLAY_EXPECT="builtin 1470x956"\n'
                f'mkdir -p "$TOOLS/bench"\n'
                f'printf "raise SystemExit({0 if passes else 1})\\n" '
                f'  > "$TOOLS/bench/mac-browser-check.py"\n'
                f'say() {{ printf "%s\\n" "$*"; }}\n'
                f'jf() {{ printf sid-a; }}\n'
                f'leave_bench() {{ printf "LEAVE: %s\\n" "$1"; : > {left}; }}\n'
                f'refuse_throttled_browser() {{{func_body(text, "refuse_throttled_browser")}}}\n'
                f'refuse_throttled_browser\n'
                f'printf "RAN THE ROUNDS\\n"\n')
            return cp.stdout + cp.stderr, left.exists()

    def test_an_unthrottled_browser_lets_the_rounds_run(self):
        out, left = self._check(passes=True)
        self.assertIn("RAN THE ROUNDS", out, out)
        self.assertFalse(left, out)

    def test_a_throttled_browser_stops_the_job_before_round_one(self):
        out, left = self._check(passes=False)
        self.assertNotIn("RAN THE ROUNDS", out, "it measured anyway:\n" + out)
        self.assertIn("LEAVE: browser check failed", out, out)
        self.assertTrue(left, out)

    def test_an_arm_with_no_products_is_not_measured_around(self):
        out, left = self._check(staged=False)
        self.assertNotIn("RAN THE ROUNDS", out, out)
        self.assertIn("LEAVE: arm A is not staged", out, out)

    def test_it_runs_before_the_warmup_and_after_the_quiescing(self):
        text = AUTORUN.read_text()
        self.assertLess(text.index('say "quiescing"'), text.index("refuse_throttled_browser()"))
        self.assertLess(text.index("\nrefuse_throttled_browser\n"),
                        text.index('say "warmup round'))

    def test_the_reading_travels_with_the_experiment(self):
        self.assertIn('--json "$RUNS/browser-check.json"', AUTORUN.read_text())


class TestDetectZeroMeansZero(WkTest):
    """`--detect 0` is documented as running `--rounds` exactly. The job is
    JSON, so the driver writes it through `float()` and the install reads
    `0.0`; a string test against `0` read that as "stopping rule on" and a run
    asked for one round took forty (measured 2026-09-07)."""

    def _off(self, value):
        body = func_body(AUTORUN.read_text(), "detect_off")
        return sh(f'DETECT={value}\ndetect_off() {{{body}}}\n'
                  f'if detect_off; then echo OFF; else echo ON; fi').stdout.strip()

    def test_every_spelling_of_zero_turns_the_rule_off(self):
        for value in ("0", "0.0", "0.00", ".0"):
            with self.subTest(detect=value):
                self.assertEqual("OFF", self._off(value))

    def test_a_real_target_leaves_it_on(self):
        for value in ("0.3", "0.05", "1"):
            with self.subTest(detect=value):
                self.assertEqual("ON", self._off(value))

    def test_the_ceiling_and_the_check_ask_the_same_question(self):
        text = AUTORUN.read_text()
        self.assertIn("if detect_off; then CEILING=", text)
        self.assertIn('&& ! detect_off; then', text)
        stale = [l.strip() for l in text.splitlines()
                 if '"$DETECT" = 0' in l or '"$DETECT" != 0' in l]
        self.assertEqual([], stale, "a string test against 0 is left")
