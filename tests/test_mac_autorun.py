"""The script a macOS benchmark install runs by itself (bench/mac-bench-autorun.sh)
and the readings it judges the install by (bench/mac-quiet-desktop.sh).

It runs with nobody in the room and no network, so anything it cannot decide
by itself it cannot ask about: a reading that hangs hangs the experiment, a
refusal nobody reads has to power the machine off, and a run that ends any way
at all has to leave its verdict on the volume. Every function and every
decision here is exercised without a Mac.

Run: python3 -m unittest tests.test_mac_autorun -v
"""
import json
import shlex
import re
import subprocess
import time
import unittest

from tests.support import REPO, WkTest, func_body, scratch_dir

AUTORUN = REPO / "bench" / "mac-bench-autorun.sh"
QUIET = REPO / "bench" / "mac-quiet-desktop.sh"
NOISE = REPO / "lib" / "quiet.sh"


def sh(script, cwd=None, timeout=120):
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          timeout=timeout, cwd=cwd)


class TestAReadingCannotHangALeg(WkTest):
    """`wk_quiet_daemons_pause` holds mds SIGSTOPped and a stopped daemon
    answers no XPC request, so `mdutil -s /` never returns: one leg of one
    round hung for 2727s that way and the watchdog took the whole experiment
    with it. macOS ships no timeout(1), so the bound is this file's own."""

    def _read(self, script):
        return sh(f'set -euo pipefail\n. {str(QUIET)!r}\n{script}\n')

    def test_a_reading_that_answers_comes_back_whole(self):
        cp = self._read('_wk_qd_read 10 printf "one\\ntwo\\n"')
        self.assertEqual("one\ntwo\n", cp.stdout, cp.stderr)
        self.assertEqual(0, cp.returncode, cp.stderr)

    def test_a_reading_that_answers_nothing_is_empty_and_not_a_timeout(self):
        """A command that failed for its own reason answered nothing, which is
        a different fact from a daemon that never answers."""
        for command in ("true", "false", "nosuchcommandanywhere"):
            with self.subTest(command=command):
                cp = self._read(f'_wk_qd_read 10 {command}')
                self.assertEqual("", cp.stdout, cp.stderr)

    def test_a_reading_that_does_not_answer_is_given_up_on_inside_the_bound(self):
        t0 = time.monotonic()
        cp = self._read('_wk_qd_read 1 sleep 120')
        took = time.monotonic() - t0
        self.assertEqual("!timeout", cp.stdout, cp.stderr)
        self.assertLess(took, 30, f"the bound did not hold: {took:.0f}s")

    def test_the_sentinel_is_not_a_value_a_reading_can_answer(self):
        cp = self._read('printf "%s" "$_WK_QD_TIMEOUT"')
        self.assertEqual("!timeout", cp.stdout)

    def test_stderr_is_part_of_the_reading_when_it_is_asked_for(self):
        """`tmutil destinationinfo` says "No destinations configured" on
        stderr, so a reader that dropped stderr would read a configured
        destination on every machine that has none."""
        cp = self._read("""_wk_qd_read -e 10 sh -c 'printf out; printf err >&2'""")
        self.assertEqual("outerr", cp.stdout, cp.stderr)

    def test_stderr_is_dropped_unless_it_is_asked_for(self):
        cp = self._read("""_wk_qd_read 10 sh -c 'printf out; printf err >&2'""")
        self.assertEqual("out", cp.stdout, cp.stderr)

    def test_the_merged_form_is_bounded_through_a_grandchild_too(self):
        """The kill reaches the direct child only, so the hang can outlive it:
        a reading collected through a pipe would then wait on the grandchild
        holding that pipe open, which is the bound not holding at all."""
        with scratch_dir() as tmp:
            binp = tmp / "bin"
            binp.mkdir()
            hang = binp / "tmutil"
            hang.write_text('#!/bin/sh\nprintf "starting\\n" >&2\nsleep 120 & wait\n')
            hang.chmod(0o755)
            t0 = time.monotonic()
            cp = sh(f'set -euo pipefail\nPATH={binp}:$PATH\n. {str(QUIET)!r}\n'
                    f'_wk_qd_read -e 1 tmutil destinationinfo\n')
            took = time.monotonic() - t0
        self.assertEqual("!timeout", cp.stdout, cp.stderr)
        self.assertLess(took, 30, f"the bound did not hold: {took:.0f}s")

    def test_there_is_still_one_bounded_reader_in_the_tree(self):
        self.assertEqual(1, QUIET.read_text().count("_wk_qd_read() {"))
        self.assertNotIn("_wk_qd_read() {", NOISE.read_text())

    def test_the_reading_that_hung_and_the_one_beside_it_share_the_bound(self):
        """One implementation: `mdutil` is the measured one, and the 0600
        analytics plist is read through `sudo` off the same paused daemon."""
        text = QUIET.read_text()
        probe = text[text.index("wk_quiet_desktop_probe()"):]
        self.assertEqual(2, len(re.findall(r"_wk_qd_read ", probe)), probe)
        self.assertIn('_wk_qd_read "$_WK_QD_READ_SECS" mdutil -s /', probe)
        self.assertIn('_wk_qd_read "$_WK_QD_READ_SECS" sudo -n defaults read', probe)
        self.assertEqual(1, text.count("_wk_qd_read() {"))

    def test_the_probe_says_which_of_the_two_spotlight_did(self):
        """A read that timed out and a read that answered nothing are one
        value in the probe's output otherwise, and the findings below have to
        tell them apart."""
        with scratch_dir() as tmp:
            binp = tmp / "bin"
            binp.mkdir()
            hang = binp / "mdutil"
            hang.write_text("#!/bin/sh\nexec sleep 120\n")   # exec: the bound kills the direct child
            hang.chmod(0o755)
            cp = sh(f'set -euo pipefail\nPATH={binp}:$PATH\n. {str(QUIET)!r}\n'
                    f'_WK_QD_READ_SECS=1\nwk_quiet_desktop_probe | grep "^spotlight="\n')
            self.assertEqual("spotlight=!timeout\n", cp.stdout, cp.stderr)
        cp = sh(f'set -euo pipefail\n. {str(QUIET)!r}\n'
                f'wk_quiet_desktop_probe | grep "^spotlight="\n')
        self.assertEqual("spotlight=\n", cp.stdout, cp.stderr)


class TestATimedOutReadingIsUnknownAndNotAFault(WkTest):
    """152 legs read Spotlight while the daemons were paused and one did not,
    so the deadlock is intermittent: a flaky reading must say what it could
    not establish and must not refuse a leg for it."""

    def _judge(self, probe):
        cp = sh(f'set -euo pipefail\n. {str(QUIET)!r}\n'
                f'wk_quiet_desktop_findings {probe!r} "the remedy"\n')
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        return [l.split("\t") for l in cp.stdout.splitlines() if l]

    def _about(self, probe, word):
        return [f for f in self._judge(probe) if word in f[1]]

    def test_spotlight_timing_out_is_a_note_that_names_the_deadlock(self):
        found = self._about("spotlight=!timeout", "Spotlight")
        self.assertEqual(["note"], [f[0] for f in found], found)
        self.assertIn("held stopped", found[0][1])
        self.assertIn("XPC", found[0][1])

    def test_spotlight_answering_nothing_stays_its_own_finding(self):
        found = self._about("spotlight=", "Spotlight")
        self.assertEqual(["note"], [f[0] for f in found], found)
        self.assertNotIn("held stopped", found[0][1])

    def test_spotlight_indexing_is_still_a_fault(self):
        found = self._about("spotlight=Indexing enabled.", "Spotlight")
        self.assertEqual(["wrong"], [f[0] for f in found], found)

    def test_any_timed_out_reading_is_a_note_that_names_the_deadlock(self):
        """One arm, not one per reading: `_wk_qf_judge` is what every table
        row goes through."""
        found = self._about("analytics=!timeout", "diagnostics")
        self.assertEqual(["note"], [f[0] for f in found], found)
        self.assertIn("XPC", found[0][1])

    def test_that_same_reading_answering_nothing_is_still_a_fault(self):
        found = self._about("analytics=", "diagnostics")
        self.assertEqual(["wrong"], [f[0] for f in found], found)


class TestEveryPausedDaemonIsReadThroughTheBound(WkTest):
    """`macos_noise` runs in every leg's preflight (cmd/bench) and inside
    `wk quiesce on` (cmd/quiesce) -- both after `wk_quiet_daemons_pause` -- so
    a reading of a daemon it holds stopped hangs the leg exactly the way the
    Spotlight one did."""

    def _noise(self, timing_out):
        answers = ('*tmutil*|*AutomaticCheckEnabled*) printf "%s" "$_WK_QD_TIMEOUT" ;;'
                   if timing_out else
                   '*tmutil*) printf "No destinations configured\n" ;;'
                   '*AutomaticCheckEnabled*) printf 0 ;;')
        cp = sh(f'set -euo pipefail\n. {str(NOISE)!r}\n'
                f'_wk_qd_read() {{ case "$*" in {answers} *) printf "" ;; esac; }}\n'
                f'macos_noise || true\n'
                f'printf "FAULTS=%s\\n" "$MACOS_NOISE_FAULTS"\n')
        return cp.stdout + cp.stderr

    def test_both_readings_go_through_the_one_bounded_reader(self):
        text = NOISE.read_text()
        self.assertIn('_wk_qd_read -e "$_WK_QD_READ_SECS" tmutil destinationinfo', text)
        self.assertIn('_wk_qd_read "$_WK_QD_READ_SECS" sudo -n defaults read', text)
        self.assertNotIn("tmutil destinationinfo 2>&1", text)

    def test_a_machine_that_answers_is_judged_as_before(self):
        out = self._noise(timing_out=False)
        self.assertIn("no destination configured", out, out)
        self.assertIn("automatic checking off", out, out)
        self.assertIn("FAULTS=0", out, out)

    def test_a_reading_that_times_out_is_unknown_and_names_the_daemon(self):
        out = self._noise(timing_out=True)
        self.assertIn("backupd did not answer inside its bound", out, out)
        self.assertIn("softwareupdated did not answer inside its bound", out, out)
        for line in out.splitlines():
            if "did not answer inside its bound" in line:
                self.assertIn("unknown", line, line)

    def test_a_reading_that_times_out_refuses_no_leg(self):
        """An intermittent XPC deadlock must not turn into a refused
        measurement: the deadlock is the fault, not the machine."""
        out = self._noise(timing_out=True)
        self.assertIn("FAULTS=0", out, out)
        self.assertNotIn("timemachine: a destination is configured", out, out)
        self.assertNotIn("updates:    automatic checking is on", out, out)


class TestTheMachineEndsUpOff(WkTest):
    """The bench volume is the firmware default, so a reboot lands back on it
    and starts the agent again. Every way this script can end ends with the
    machine powered off."""

    def _leave(self, host="/Volumes/Macintosh HD", group="HOSTGRP", firmware=None):
        """`leave_bench` with the firmware, the host install and sudo all faked.
        `firmware` is what `wkmac.py boot-volume` answers after the bless --
        None means the bless took."""
        with scratch_dir() as tmp:
            calls = tmp / "sudo"
            answers = "HOSTGRP:%s" % (group if firmware is None else firmware)
            hostfn = ('host_install() { printf %s; }\n' % (shlex.quote(host))
                      if host else 'host_install() { return 1; }\n')
            cp = sh(f'set -euo pipefail\n_left=""\nSTATE={tmp}/state\nTOOLS={tmp}\n'
                    f'hold_for_a_reader() {{ :; }}\n'   # its own tests below
                    f'say() {{ printf "%s\\n" "$*"; }}\n'
                    f'state_set() {{ printf "state %s=%s\\n" "$1" "$2"; }}\n'
                    f'sudo() {{ printf "%s\\n" "$*" >> {calls}; }}\n'
                    f'{hostfn}'
                    # wkmac.py stands in for both readings: the group of the host
                    # install, and what the firmware names after the bless.
                    f'python3() {{ case "$*" in *volume-group*) printf %s {shlex.quote(group)} ;;'
                    f' *boot-volume*) printf %s {shlex.quote(answers)} ;; esac; }}\n'
                    f'leave_bench() {{{func_body(AUTORUN.read_text(), "leave_bench")}}}\n'
                    f'leave_bench "the reason"\nleave_bench "a second reason"\n')
            return cp.stdout + cp.stderr, calls.read_text() if calls.exists() else ""

    def test_a_bless_that_took_hands_the_machine_back(self):
        """The only human step should be a login, so it reboots into host mode
        rather than halting and waiting for a power button."""
        out, calls = self._leave()
        self.assertIn("-n /sbin/reboot", calls, out)
        self.assertNotIn("/sbin/halt", calls, out)
        self.assertIn("comes up in host mode", out)

    def test_it_blesses_before_it_reads_the_firmware_back(self):
        out, calls = self._leave()
        lines = calls.splitlines()
        self.assertTrue(lines[0].startswith("-n bless --mount"), lines)
        self.assertIn("--setBoot", lines[0])

    def test_a_bless_that_did_not_take_halts_instead(self):
        """This volume is the firmware default: rebooting with it still default
        lands back here and measures again, so a reboot is taken only once the
        firmware reads back as the host install."""
        out, calls = self._leave(firmware="STILL-THE-BENCH-VOLUME")
        self.assertIn("-n /sbin/halt", calls, out)
        self.assertNotIn("/sbin/reboot", calls, out)
        self.assertIn("would land back here", out)

    def test_no_host_install_to_hand_back_to_halts(self):
        out, calls = self._leave(host="")
        self.assertIn("-n /sbin/halt", calls, out)
        self.assertNotIn("/sbin/reboot", calls, out)
        self.assertIn("nothing to hand back to", out)

    def test_the_host_install_is_the_one_with_no_bench_marker(self):
        """The mirror of wk-boot-priv's gate, and exactly one: two candidates is
        not a machine this may guess about."""
        body = func_body(AUTORUN.read_text(), "host_install")
        self.assertIn('[ -f "$v/etc/wk-image" ] && continue', body)
        self.assertIn('[ "$n" -eq 1 ] || return 1', body)
        self.assertIn("stat -f %d", body)

    def test_the_first_reason_is_the_one_recorded(self):
        """A trap runs after whatever already decided to leave, and the second
        caller must not turn one departure into two. Counted in transitions,
        not sudo calls: handing the machine back is a bless and then a reboot."""
        out, calls = self._leave()
        self.assertIn("the reason", out)
        self.assertNotIn("a second reason", out)
        leaving = [l for l in calls.splitlines()
                   if "/sbin/reboot" in l or "/sbin/halt" in l]
        self.assertEqual(1, len(leaving), calls)

    def test_every_reboot_is_guarded_against_landing_back_here(self):
        """This volume is the firmware default, so a reboot lands back here and
        measures again unless something changed first. Exactly two paths reboot,
        and each is guarded by a different thing being true: the display-mode
        write, bounded by the state it records, and the hand-back, taken only
        once the firmware reads back as naming the host install. The reboot the
        first-boot daemon schedules is what cancel_pending_reboot cancels."""
        text = AUTORUN.read_text()
        code = [l for l in text.splitlines() if not l.lstrip().startswith("#")]
        self.assertEqual(2, len([l for l in code if "/sbin/reboot" in l]), code)
        mode = func_body(text, "converge_display_mode")
        back = func_body(text, "leave_bench")
        self.assertIn("/sbin/reboot", mode)
        self.assertIn("/sbin/reboot", back)
        # The mode path's guard is its record; the hand-back's is the read-back.
        self.assertIn('state_get mode_declared', mode)
        self.assertIn('[ "$grp" = "$want" ]', back)
        self.assertIn("/sbin/halt", back)
        block = text[text.index("converge_display_mode() {"):
                     text.index("converge_display_mode\n")]
        self.assertIn('[ "$(state_get mode_declared)" = "$want" ]', block)
        self.assertIn('state_set mode_declared "$want"', block)
        self.assertIn('state_set attempts "$((ATTEMPTS - 1))"', block)
        # The bounded arm leaves rather than looping.
        self.assertIn('leave_bench "the declared display mode cannot be set"', block)
        self.assertLess(block.index("leave_bench \"the declared display mode cannot be set\""),
                        block.index('state_set mode_declared'), block)
        self.assertNotIn("leave_bench reboot", text)
        self.assertNotIn("leave_bench halt", text)

    def test_every_call_names_one_reason(self):
        """`leave_bench <why>`: one behaviour, so no caller chooses one."""
        calls = re.findall(r"^\s*leave_bench (.*)$", AUTORUN.read_text(), re.M)
        self.assertTrue(calls)
        for call in calls:
            with self.subTest(call=call):
                self.assertRegex(call, r'^"[^"]+"$')

    def test_the_finished_job_powers_off_rather_than_staying_up(self):
        """A finished A/B on a machine that stays up is a machine nothing can
        reach: this install has no network at all."""
        text = AUTORUN.read_text()
        self.assertNotIn("booted_is_default", text)
        self.assertNotIn("stayed-up", text)
        self.assertTrue(text.rstrip().endswith('leave_bench "job finished"'),
                        text.rstrip()[-200:])

    def test_host_mode_is_the_one_exit_that_touches_nothing(self):
        """The same agent file can be left on the host install, where
        /etc/wk-image is absent -- and powering that off is powering off the
        machine the maintainer is using."""
        text = AUTORUN.read_text()
        block = text[text.index("if [ ! -f /etc/wk-image ]"):]
        block = block[:block.index("\nfi\n")]
        self.assertIn("remove_agent", block)
        self.assertNotIn("leave_bench", block)


class TestTheWatchdogLeavesAVerdict(WkTest):
    """A watchdog firing is not a reason to throw the measurement away: the
    run that hung had sixteen rounds of real numbers on the volume and no
    summary.txt, because only the clean path ever summarised."""

    def _fire(self, phase="", stall="10"):
        text = AUTORUN.read_text()
        cp = sh(f'set -euo pipefail\nSTALL={stall}\nLOG=/dev/null\n'
                f'sleep() {{ :; }}\n'
                f'date() {{ printf 1000; }}\n'
                f'stat() {{ printf 0; }}\n'
                f'say() {{ printf "%s\\n" "$*"; }}\n'
                f'state_get() {{ printf "%s" {phase!r}; }}\n'
                f'state_set() {{ printf "state %s=%s\\n" "$1" "$2"; }}\n'
                f'summarise() {{ printf "SUMMARISED\\n"; }}\n'
                f'leave_bench() {{ printf "OFF: %s\\n" "$1"; }}\n'
                f'watchdog() {{{func_body(text, "watchdog")}}}\n'
                f'watchdog; printf "RET=%s\\n" "$?"\n')
        return cp, cp.stdout + cp.stderr

    def test_it_summarises_the_rounds_that_did_land_and_then_powers_off(self):
        cp, out = self._fire()
        self.assertEqual(0, cp.returncode, out)
        self.assertIn("WATCHDOG FIRED", out)
        self.assertIn("SUMMARISED", out)
        self.assertIn("OFF: ", out)
        self.assertIn("RET=0", out)

    def test_the_state_is_advanced_before_the_summary(self):
        """A power cut inside the summary must not let the next boot repeat
        the attempt."""
        _, out = self._fire()
        lines = out.splitlines()
        self.assertLess(lines.index("state phase=done"), lines.index("SUMMARISED"), out)
        self.assertLess(lines.index("state outcome=watchdog"), lines.index("SUMMARISED"), out)
        self.assertLess(lines.index("SUMMARISED"),
                        [i for i, l in enumerate(lines) if l.startswith("OFF: ")][0], out)

    def test_a_finished_job_retires_it_without_a_second_summary(self):
        cp, out = self._fire(phase="done")
        self.assertEqual("RET=0\n", out, out)

    def test_one_summary_step_serves_both_endings(self):
        """Two copies could summarise into different files, or one of them
        could stop being run."""
        text = AUTORUN.read_text()
        self.assertEqual(1, text.count("summarise() {"))
        self.assertEqual(1, text.count('"$TOOLS/wk" bench ab-summary'))
        self.assertEqual(2, len(re.findall(r"^\s*summarise\s*(?:#.*)?$", text, re.M)))

    def test_the_clean_ending_advances_the_state_before_it_summarises_too(self):
        text = AUTORUN.read_text()
        tail = text[text.index('say "leaving the machine quiesced'):]
        self.assertLess(tail.index("state_set phase done"), tail.index("\nsummarise\n"), tail)

    def test_it_is_armed_with_somewhere_to_write_the_verdict(self):
        """The summary goes into $RUNS, so the run map has to exist before the
        watchdog that can call it."""
        text = AUTORUN.read_text()
        self.assertLess(text.index('RUNS="$WK_AB_ROOT/ab/'), text.index("watchdog() {"))
        self.assertLess(text.index("summarise() {"), text.index("watchdog() {"))
        self.assertLess(text.index("\nwatchdog &"), text.index('say "warmup round'))

    def test_the_watchdog_and_its_summary_are_defined_before_it_is_armed(self):
        """`watchdog &` before `watchdog() {` is "command not found" in a
        subshell: the boot goes on with no watchdog at all and $! still sets
        WATCHDOG, so nothing says so (measured, job 20260909T132948Z)."""
        text = AUTORUN.read_text()
        for define in ('RUNS="$WK_AB_ROOT/ab/', "summarise() {", "watchdog() {"):
            with self.subTest(defined=define):
                self.assertLess(text.index(define), text.index("\nwatchdog &"))

    def test_it_is_armed_before_every_step_that_can_block(self):
        """Joining a tailnet, writing a display mode, quiescing and launching a
        browser all wait on the system for as long as it takes. A boot that
        reaches one of those with neither the watchdog nor the hand-back trap
        armed is a machine left in bench mode with nothing able to report it and
        no way back -- which is what an install whose Wi-Fi never came up did,
        sitting in `tailscale up` with no deadline (measured 2026-09-09)."""
        text = AUTORUN.read_text()
        armed = text.index("\nwatchdog &")
        trap = text.index("\ntrap 'kill \"$WATCHDOG\"")
        self.assertLess(armed, trap, "the trap is armed with the watchdog")
        for step in ("\nconverge_self\n", "\nhold_auto_brightness ", "\nrefuse_wrong_displays\n",
                     "\nconverge_display_mode\n", '"$TOOLS/wk" quiesce on',
                     "\ndim_display\n", "\nrefuse_throttled_browser\n"):
            with self.subTest(step=step.strip()):
                self.assertLess(trap, text.index(step))

    def test_the_two_exits_that_want_the_machine_up_say_so(self):
        """The trap hands the machine back, so the branches whose whole point is
        a reboot of their own have to be able to opt out: the first-boot daemon's
        provisioning, and the display-mode write that needs one boot to take."""
        text = AUTORUN.read_text()
        self.assertIn('[ -n "$_stay" ] || leave_bench', text)
        self.assertEqual(2, len([l for l in text.splitlines()
                                 if l.strip().startswith("_stay=1")]), text)
        mode = text[text.index("converge_display_mode() {"):]
        self.assertLess(mode.index("_stay=1"), mode.index("/sbin/reboot"),
                        "the stay is set before the reboot, not after")

    def test_the_join_cannot_wait_forever(self):
        """`tailscale up` without --timeout waits for the backend to reach
        Running for as long as that takes."""
        text = (REPO / "bench" / "mac-tailnet.sh").read_text()
        self.assertRegex(text, r"tailscale\" up --timeout=\d+s ")


class TestTheStoppingRuleIsGivenRunDirectories(WkTest):
    """`wk bench report` and `wk bench precision` name run directories; the
    stopping rule reads the same rows through the same convention."""

    def _arm_results(self, plan, label):
        with scratch_dir() as tmp:
            (tmp / "runs.tsv").write_text(
                "1\tA\tsid-a\tr1\tclean\tjetstream3\n"
                "1\tB\tsid-b\tr2\tclean\tjetstream3\n"
                "2\tA\tsid-a\tr3\tclean\tjetstream3\n"
                "1\tA\tsid-a\tr4\tclean\tmotionmark\n"
                "2\tB\tsid-b\tr5\tscanned\tjetstream3\n")
            body = func_body(AUTORUN.read_text(), "arm_results")
            return sh(f'WK_AB_ROOT=/var/wk\nRUNS={tmp}\n'
                      f'arm_results() {{{body}}}\narm_results {plan} {label}').stdout

    def test_it_emits_directories_and_not_files_inside_them(self):
        self.assertEqual("/var/wk/results/r1,/var/wk/results/r3",
                         self._arm_results("jetstream3", "A"))

    def test_nothing_appends_a_file_name_anywhere_on_the_way(self):
        self.assertNotIn("result.json", AUTORUN.read_text())

    def test_a_contaminated_round_still_reaches_nothing(self):
        """A software-update scan across an arm is a number to drop, and the
        rule must not be talked into stopping by one."""
        self.assertEqual("/var/wk/results/r2", self._arm_results("jetstream3", "B"))


class TestTheDisplayIsPinnedByTheJob(WkTest):
    """Two runs at different resolutions are two different measurements, and
    nothing downstream reads the screen they were taken at."""

    def _refuse(self, expect):
        text = AUTORUN.read_text()
        cp = sh(f'set -euo pipefail\nDISPLAY_EXPECT={expect!r}\n'
                f'say() {{ printf "%s\\n" "$*"; }}\n'
                f'state_set() {{ printf "state %s=%s\\n" "$1" "$2"; }}\n'
                f'remove_agent() {{ printf "AGENT REMOVED\\n"; }}\n'
                f'leave_bench() {{ printf "OFF: %s\\n" "$1"; }}\n'
                f'refuse_unpinned_display() {{{func_body(text, "refuse_unpinned_display")}}}\n'
                f'refuse_unpinned_display\nprintf "RAN THE ROUNDS\\n"\n')
        return cp.stdout + cp.stderr

    def test_a_job_that_pins_no_display_measures_nothing(self):
        out = self._refuse("")
        self.assertNotIn("RAN THE ROUNDS", out, "it measured at an unknown screen:\n" + out)
        self.assertIn("OFF: ", out)
        self.assertIn("NODE_DISPLAY", out, out)
        self.assertIn("state phase=done", out, out)

    def test_a_job_that_pins_one_runs_on(self):
        out = self._refuse("builtin 1470x956")
        self.assertIn("RAN THE ROUNDS", out, out)
        self.assertNotIn("OFF: ", out)

    def test_the_expectation_comes_out_of_the_job(self):
        self.assertIn("DISPLAY_EXPECT=$(jf display)", AUTORUN.read_text())

    def test_it_is_read_before_anything_is_measured(self):
        text = AUTORUN.read_text()
        self.assertLess(text.index("\nrefuse_unpinned_display\n"),
                        text.index('say "settling for'))


class TestTheBrowserCheckIsToldWhatToExpect(WkTest):
    def _check(self, expect="builtin 1470x956", passes=True):
        text = AUTORUN.read_text()
        with scratch_dir() as tmp:
            root = tmp / "var-wk"
            (root / "staged" / "sid-a" / "WebKitBuild" / "Release").mkdir(parents=True)
            runs = root / "ab" / "stamp"
            runs.mkdir(parents=True)
            argv = tmp / "argv"
            checker = tmp / "tools" / "bench" / "mac-browser-check.py"
            checker.parent.mkdir(parents=True)
            checker.write_text(
                "import sys, pathlib\n"
                f"pathlib.Path({str(argv)!r}).write_text('\\n'.join(sys.argv[1:]))\n"
                f"raise SystemExit({0 if passes else 1})\n")
            cp = sh(f'set -euo pipefail\n'
                    f'WK_AB_ROOT={root}; RUNS={runs}; TOOLS={tmp}/tools; LOG=/dev/null\n'
                    f'DISPLAY_EXPECT={expect!r}\n'
                    f'say() {{ printf "%s\\n" "$*"; }}\n'
                    f'jf() {{ printf sid-a; }}\n'
                    f'leave_bench() {{ printf "OFF: %s\\n" "$1"; }}\n'
                    f'refuse_throttled_browser() {{'
                    f'{func_body(text, "refuse_throttled_browser")}}}\n'
                    f'refuse_throttled_browser\nprintf "RAN THE ROUNDS\\n"\n')
            got = argv.read_text().splitlines() if argv.exists() else []
            return cp.stdout + cp.stderr, got

    def test_the_pinned_display_reaches_the_check(self):
        out, argv = self._check()
        self.assertIn("RAN THE ROUNDS", out, out)
        self.assertIn("--expect-display", argv, argv)
        self.assertEqual("builtin 1470x956", argv[argv.index("--expect-display") + 1], argv)

    def test_the_reading_still_travels_with_the_experiment(self):
        _, argv = self._check()
        self.assertIn("--json", argv, argv)
        self.assertIn("--build-directory", argv, argv)

    def test_a_display_the_check_refuses_powers_the_machine_off(self):
        """The one refusal carries the new fault too: a second monitor, a
        mirrored panel or an unpinned mode is measured by that script."""
        out, _ = self._check(passes=False)
        self.assertNotIn("RAN THE ROUNDS", out, "it measured anyway:\n" + out)
        self.assertIn("OFF: ", out)


class TestTheDisplayIsLeftAtMinimumBrightness(WkTest):
    def _dim(self, rc=0, value="0.0"):
        text = AUTORUN.read_text()
        with scratch_dir() as tmp:
            args = tmp / "args"
            cp = sh(f'set -euo pipefail\nTOOLS={tmp}/tools\n'
                    f'say() {{ printf "%s\\n" "$*"; }}\n'
                    f'leave_bench() {{ printf "OFF: %s\\n" "$1"; }}\n'
                    f'python3() {{ printf "%s\\n" "$*" >> {args}; printf {value!r}; '
                    f'return {rc}; }}\n'
                    f'dim_display() {{{func_body(text, "dim_display")}}}\n'
                    f'dim_display\nprintf "RAN THE ROUNDS\\n"\n')
            return cp.stdout + cp.stderr, args.read_text() if args.exists() else ""

    def test_it_asks_for_the_minimum_through_the_one_interface(self):
        out, args = self._dim()
        self.assertIn("lib/wkmac.py brightness --set 0", args, args)
        self.assertIn("RAN THE ROUNDS", out, out)

    def test_the_value_it_reached_is_logged(self):
        out, _ = self._dim(value="0.0")
        self.assertIn("0.0", out, out)

    def test_a_display_that_will_not_dim_measures_nothing(self):
        """The read-back is what makes this a refusal rather than a wish: a
        set that did not take exits nonzero."""
        out, _ = self._dim(rc=1, value="")
        self.assertNotIn("RAN THE ROUNDS", out, "it measured at whatever the panel was:\n" + out)
        self.assertIn("OFF: ", out)

    def test_nothing_restores_it_afterwards(self):
        text = AUTORUN.read_text()
        self.assertEqual(1, len(re.findall(r"brightness --set", text)), text)
        self.assertLess(text.index("\ndim_display\n"),
                        text.index("\nrefuse_throttled_browser\n"))


class TestTheCountEveryLegAsksFor(WkTest):
    """Every leg of the run that hung warned "count=1: no p-value can be
    computed": its p-values were across rounds only, and the within-run
    statistics were given up for nothing."""

    def _count(self, doc):
        text = AUTORUN.read_text()
        line = [l for l in text.splitlines() if l.startswith("COUNT=")]
        self.assertEqual(1, len(line), line)
        with scratch_dir() as tmp:
            job = tmp / "job.json"
            job.write_text(json.dumps(doc))
            return sh(f'set -euo pipefail\nJOB={job}\n'
                      f'jf() {{{func_body(text, "jf")}}}\n'
                      f'{line[0]}\nprintf "%s" "$COUNT"\n').stdout

    def test_a_job_that_names_no_count_measures_two_runs(self):
        self.assertEqual("2", self._count({"plans": ["jetstream3"]}))

    def test_the_empty_string_the_driver_writes_is_no_count(self):
        self.assertEqual("2", self._count({"count": ""}))

    def test_a_job_that_names_one_keeps_it(self):
        self.assertEqual("4", self._count({"count": 4}))

    def test_every_leg_passes_the_count_it_settled_on(self):
        body = func_body(AUTORUN.read_text(), "leg")
        self.assertIn('set -- "$@" --count "$COUNT"', body)
        self.assertNotIn('[ -n "$COUNT" ]', body)


class TestProvisionedMeansTheSettingsThemselves(WkTest):
    """`wk bench staged` measures the settings provisioning applies before
    every leg, so a volume that drifted afterwards read as provisioned here
    and was then refused leg by leg, unobservably, on a machine with no
    network. One probe, one judge, one finding, up front."""

    OK = "ok\tApp Nap cannot throttle a backgrounded browser\t\n"
    WRONG = "wrong\tnot so: the screen does not lock (askforpassword reads '1')\tfix\n"

    def _refuse(self, findings=OK, running=False, table=True):
        text = AUTORUN.read_text()
        with scratch_dir() as tmp:
            quiet = tmp / "quiet.sh"
            if table:
                quiet.write_text(
                    "wk_quiet_desktop_probe() { printf 'askforpassword=1\\n'; }\n"
                    "wk_quiet_desktop_findings() { cat <<'ROWS'\n"
                    + findings + "ROWS\n}\n")
            cp = sh(f'set -euo pipefail\nQUIET_DESKTOP={quiet}\n'
                    f'FB_PLIST={tmp}/plist; FB_SELF={tmp}/self\n'
                    f'say() {{ printf "%s\\n" "$*"; }}\n'
                    f'leave_bench() {{ printf "OFF: %s\\n" "$1"; }}\n'
                    f'pgrep() {{ return {0 if running else 1}; }}\n'
                    f'refuse_unprovisioned() {{'
                    f'{func_body(text, "refuse_unprovisioned")}}}\n'
                    f'refuse_unprovisioned\nprintf "RAN THE ROUNDS\\n"\n')
            return cp, cp.stdout + cp.stderr

    def test_a_volume_whose_settings_are_a_measured_macs_is_measured_on(self):
        cp, out = self._refuse()
        self.assertEqual(0, cp.returncode, out)
        self.assertIn("RAN THE ROUNDS", out, out)
        self.assertNotIn("OFF: ", out)

    def test_a_setting_that_drifted_refuses_the_whole_job_once(self):
        cp, out = self._refuse(findings=self.OK + self.WRONG)
        self.assertEqual(0, cp.returncode, out)
        self.assertNotIn("RAN THE ROUNDS", out, "every leg would be refused instead:\n" + out)
        self.assertIn("the screen does not lock", out, out)
        self.assertIn("mac-volume --repair", out, out)
        self.assertIn("OFF: ", out)

    def test_it_stands_aside_while_provisioning_is_running(self):
        """A daemon in flight is not a volume that drifted: it applies the
        settings and reboots, and this agent starts again on that boot. Its own
        function, because it has to run before defuse_firstboot while the
        judgment has to run after `wk quiesce on`."""
        text = AUTORUN.read_text()
        cp = sh('set -euo pipefail\n'
                'say() { printf "%s\\n" "$*"; }\n'
                'leave_bench() { printf "OFF: %s\\n" "$1"; }\n'
                'pgrep() { return 0; }\n'
                '_stay=""\n'
                'stand_aside_if_provisioning() {'
                + func_body(text, "stand_aside_if_provisioning") + '}\n'
                'stand_aside_if_provisioning\nprintf "WENT ON\\n"\n')
        out = cp.stdout + cp.stderr
        self.assertEqual(0, cp.returncode, out)
        self.assertIn("standing aside", out, out)
        self.assertNotIn("WENT ON", out, out)
        self.assertNotIn("OFF: ", out, "it powered off a volume mid-provisioning:\n" + out)

    def test_a_daemon_that_is_not_running_does_not_stand_aside(self):
        cp = sh('set -euo pipefail\n'
                'say() { printf "%s\\n" "$*"; }\n'
                'pgrep() { return 1; }\n'
                '_stay=""\n'
                'stand_aside_if_provisioning() {'
                + func_body(AUTORUN.read_text(), "stand_aside_if_provisioning") + '}\n'
                'stand_aside_if_provisioning\nprintf "WENT ON\\n"\n')
        self.assertIn("WENT ON", cp.stdout + cp.stderr, cp.stdout + cp.stderr)

    def test_a_volume_with_no_table_to_be_judged_by_measures_nothing(self):
        cp, out = self._refuse(table=False)
        self.assertNotIn("RAN THE ROUNDS", out, out)
        self.assertIn("OFF: ", out)

    def test_it_asks_the_probe_every_leg_asks(self):
        text = AUTORUN.read_text()
        self.assertIn('QUIET_DESKTOP="$TOOLS/bench/mac-quiet-desktop.sh"', text)
        body = func_body(text, "refuse_unprovisioned")
        self.assertIn("wk_quiet_desktop_probe", body)
        self.assertIn("wk_quiet_desktop_findings", body)

    def test_no_second_definition_of_provisioned_is_left(self):
        """A log line saying provisioning once finished is not the question:
        what a leg refuses is what the machine reads now."""
        text = AUTORUN.read_text()
        self.assertNotIn("provisioning complete", text)
        self.assertNotIn("fb_provisioned", text)

    def test_a_machine_that_is_not_a_measured_mac_is_refused_by_the_real_table(self):
        """The whole path -- source, probe, judge, report -- against
        bench/mac-quiet-desktop.sh itself. Nothing this test runs on has a
        measured Mac's settings."""
        text = AUTORUN.read_text()
        cp = sh(f'set -euo pipefail\nQUIET_DESKTOP={str(QUIET)!r}\n'
                f'FB_PLIST=/nonexistent; FB_SELF=/nonexistent\n'
                f'say() {{ printf "%s\\n" "$*"; }}\n'
                f'leave_bench() {{ printf "OFF: %s\\n" "$1"; }}\n'
                f'pgrep() {{ return 1; }}\n'
                f'refuse_unprovisioned() {{'
                f'{func_body(text, "refuse_unprovisioned")}}}\n'
                f'refuse_unprovisioned\nprintf "RAN THE ROUNDS\\n"\n')
        out = cp.stdout + cp.stderr
        self.assertNotIn("RAN THE ROUNDS", out, out)
        self.assertIn("OFF: ", out)
        self.assertIn("not set up as a measured Mac", out, out)

class ADaemonThatCameBackIsPausedAgainBeforeEachLeg(WkTest):
    """macOS restarts the daemons the lane holds stopped, on demand. One that
    came back between the quiesce and a leg fails that leg on the gate
    `wk bench staged` asks -- XProtect did, and took all four legs of job
    20260909T154515Z with it, each in two seconds."""

    def test_every_leg_re_pauses_first(self):
        body = func_body(AUTORUN.read_text(), "leg")
        self.assertIn("repause_daemons", body)
        self.assertLess(body.index("repause_daemons"), body.index("bench staged"))

    def test_it_is_the_quiesces_own_function_and_not_a_second_one(self):
        body = func_body(AUTORUN.read_text(), "repause_daemons")
        self.assertIn("wk_quiet_daemons_pause", body)
        self.assertIn("$QUIET_DESKTOP", body)

    def test_a_pause_that_failed_does_not_skip_the_leg(self):
        """The gate judges what the pause left, so a daemon that cannot be
        paused refuses the leg there and says which -- this must not become a
        second place that decides."""
        body = func_body(AUTORUN.read_text(), "repause_daemons")
        self.assertIn("WARNING", body)
        self.assertNotIn("leave_bench", body)
        self.assertNotIn("return 1", body)


class TheAgentRunsTheTreeThePlantVerified(WkTest):
    """The plant verifies the whole tree onto the volume file for file. A second
    copy of the autorun under `bin/` was a second thing to keep in step, and it
    stopped being in step the moment anything pushed the tree without
    re-planting -- the volume then ran an older lane than it was told it had."""

    def test_the_agent_points_at_the_planted_tree(self):
        text = (REPO / "bench" / "mac-ab.sh").read_text()
        self.assertIn("/var/wk/wk-tools/bench/mac-bench-autorun.sh", text)
        self.assertNotIn("/var/wk/bin/mac-bench-autorun.sh", text)

    def test_no_second_copy_is_installed(self):
        text = (REPO / "bench" / "mac-ab.sh").read_text()
        self.assertNotIn('put_file "$WK_ROOT/bench/mac-bench-autorun.sh"', text)

    def test_the_plant_refuses_a_tree_with_no_autorun_in_it(self):
        text = (REPO / "bench" / "mac-ab.sh").read_text()
        self.assertIn("carries no executable bench/mac-bench-autorun.sh", text)


class ThePanelGoesDownBeforeAnythingThatCanStall(WkTest):
    """The panel is a load on the package under measurement, and a panel left
    lit is a panel being spent. It stayed at 0.85 for tens of minutes on
    2026-09-09 because the dim ran after the quiesce, and the quiesce was
    deadlocked."""

    def test_it_is_dimmed_before_the_join_the_mode_the_quiesce_and_the_browser(self):
        text = AUTORUN.read_text()
        dim = text.index("\ndim_display\n")
        for later in ("\nconverge_self\n", "\nhold_auto_brightness ",
                      "\nconverge_display_mode\n", '"$TOOLS/wk" quiesce on',
                      "\nrefuse_throttled_browser\n"):
            with self.subTest(after=later.strip()):
                self.assertLess(dim, text.index(later))

    def test_it_is_dimmed_once_and_nothing_later_raises_it(self):
        text = AUTORUN.read_text()
        self.assertEqual(1, len([l for l in text.splitlines() if l.strip() == "dim_display"]))
        after = text[text.index("\ndim_display\n") + 1:]
        self.assertNotIn("brightness --set", after.replace("brightness --set 0", ""))

    def test_it_is_armed_after_the_watchdog_so_a_refusal_can_report(self):
        text = AUTORUN.read_text()
        self.assertLess(text.index("\nwatchdog &"), text.index("\ndim_display\n"))


class ANumberlessBootIsHeldWhereItCanBeRead(WkTest):
    """Booting this Mac into *host* mode needs a password typed at the machine
    and booting the benchmark volume does not, so handing back is exactly what
    makes a refusal unreadable: three boots refused in their first minute and
    each cost a trip to the keyboard to find out why (2026-09-09). A boot that
    produced no number holds the machine, reachable, for a bounded window
    first."""

    def _leave(self, runs=None, had_job="1", hold="7"):
        text = AUTORUN.read_text()
        return sh(f'set -euo pipefail\n'
                  f'_had_job={had_job!r}\nRUNS={runs or "/nonexistent"}\n'
                  f'BENCH_HOLD={hold}\nLOG=/dev/null\nWK_AB_ROOT=/nonexistent\n'
                  f'say() {{ printf "%s\\n" "$*"; }}\n'
                  f'sleep() {{ printf "SLEPT %s\\n" "$1"; }}\n'
                  f'hold_for_a_reader() {{{func_body(text, "hold_for_a_reader")}}}\n'
                  f'hold_for_a_reader\n')

    def test_a_boot_with_no_runs_is_held(self):
        out = self._leave().stdout
        self.assertIn("no number came out of this boot", out, out)
        self.assertIn("SLEPT 7", out, out)

    def test_a_boot_whose_legs_landed_hands_back_at_once(self):
        with scratch_dir() as tmp:
            (tmp / "runs.tsv").write_text("1\tA\tsid\trid\tclean\tjetstream3\n")
            out = self._leave(runs=str(tmp)).stdout
        self.assertNotIn("SLEPT", out, out)

    def test_a_boot_that_never_reached_the_job_is_not_held(self):
        """No job, or one already finished: nothing about this boot is a
        refusal to read, and a fifteen-minute hold would be for nothing."""
        out = self._leave(had_job="").stdout
        self.assertNotIn("SLEPT", out, out)

    def test_the_hold_is_the_first_thing_leave_bench_does(self):
        """After the bless it is too late: the machine is already going."""
        body = func_body(AUTORUN.read_text(), "leave_bench")
        self.assertLess(body.index("hold_for_a_reader"), body.index("host_install"))

    def test_it_can_be_turned_off(self):
        self.assertNotIn("SLEPT", self._leave(hold="0").stdout)
        self.assertIn('BENCH_HOLD="${WK_MAC_BENCH_HOLD:-', AUTORUN.read_text())


class AmbientLightIsHeldRatherThanDeclined(WkTest):
    """A brightness the sensor can raise again is a load that varies, and the
    display gate refuses a run under it. macOS does expose the control -- the
    names are `DisplayServicesEnableAmbientLightCompensation` and
    `DisplayServicesAmbientLightCompensationEnabled`, read off `dyld_info
    -exports` on tolken (26.6.2, `Mac16,12`) -- so the lane holds the setting
    and only declines a machine where holding it fails."""

    def test_it_is_held_before_the_gate_that_judges_it(self):
        text = AUTORUN.read_text()
        self.assertLess(text.index("\nhold_auto_brightness "),
                        text.index("\nrefuse_wrong_displays\n"))

    def test_a_panel_with_no_sensor_is_not_a_refusal(self):
        """A guest's paravirtual panel has none, and "no sensor to hold" is not
        "under ambient-light control"."""
        text = AUTORUN.read_text()
        cp = sh('set -euo pipefail\nTOOLS=/nonexistent\nATTEMPTS=1\n'
                'say() { printf "%s\\n" "$*"; }\n'
                'state_set() { :; }\n'
                'leave_bench() { printf "OFF: %s\\n" "$1"; }\n'
                'python3() { printf "none\\n"; }\n'
                'hold_auto_brightness() {' + func_body(text, "hold_auto_brightness") + '}\n'
                'hold_auto_brightness; printf "WENT ON\\n"\n')
        out = cp.stdout + cp.stderr
        self.assertIn("no sensor to hold", out, out)
        self.assertIn("WENT ON", out, out)
        self.assertNotIn("OFF: ", out, out)

    def test_a_panel_that_will_not_let_go_hands_the_machine_back(self):
        text = AUTORUN.read_text()
        cp = sh('set -euo pipefail\nTOOLS=/nonexistent\nATTEMPTS=1\n'
                'say() { printf "%s\\n" "$*"; }\n'
                'state_set() { printf "state %s=%s\\n" "$1" "$2"; }\n'
                'leave_bench() { printf "OFF: %s\\n" "$1"; }\n'
                'python3() { printf "on\\n"; return 1; }\n'
                'hold_auto_brightness() {' + func_body(text, "hold_auto_brightness") + '}\n'
                'hold_auto_brightness; printf "WENT ON\\n"\n')
        out = cp.stdout + cp.stderr
        self.assertNotIn("WENT ON", out, "it measured under ambient-light control:\n" + out)
        self.assertIn("OFF: ", out, out)
        self.assertIn("state attempts=0", out, "a boot that measured nothing spent an attempt")

    def test_it_reads_back_and_does_not_trust_the_write(self):
        body = func_body(AUTORUN.read_text(), "hold_auto_brightness")
        self.assertIn("auto-brightness --off", body)
        self.assertIn("read back", body)


class TheVolumeIsJudgedAfterTheQuiesceThatWritesIt(WkTest):
    """`defaults write` for a protected domain does not survive this account's
    session starting, so `wk quiesce on` writes the user half again where it can
    take (cmd/quiesce says so, measured 2026-09-07). Judging those rows before
    it refuses a volume on rows the same boot is about to set -- which is what
    job 20260909T042343Z did, three seconds into its login, with the machine
    handed back before a single leg ran."""

    def test_the_judgment_comes_after_the_quiesce(self):
        text = AUTORUN.read_text()
        self.assertLess(text.index('"$TOOLS/wk" quiesce on'),
                        text.index("\nrefuse_unprovisioned\n"))

    def test_standing_aside_still_comes_before_the_daemon_is_defused(self):
        """The other half of the old function: defusing the first-boot daemon
        while it is provisioning is what the stand-aside exists to prevent, so
        it cannot move to after the quiesce with the judgment."""
        text = AUTORUN.read_text()
        self.assertLess(text.index("\nstand_aside_if_provisioning\n"),
                        text.index("\ndefuse_firstboot\n"))
        self.assertLess(text.index("\ndefuse_firstboot\n"),
                        text.index('"$TOOLS/wk" quiesce on'))

    def test_each_half_asks_one_question(self):
        text = AUTORUN.read_text()
        aside = func_body(text, "stand_aside_if_provisioning")
        self.assertIn("wk-bench-firstboot", aside)
        self.assertNotIn("wk_quiet_desktop_findings", aside)
        judge = func_body(text, "refuse_unprovisioned")
        self.assertIn("wk_quiet_desktop_findings", judge)
        self.assertNotIn("pgrep -f wk-bench-firstboot", judge)


class NoLegIsForcedExceptByARehearsal(WkTest):
    """`--force` at plant time crosses the driver's own barriers. It must not
    reach a leg: `wk bench staged` judges each leg's settings, and a forced leg
    records a number from a machine that is not a measured Mac's. What forces a
    leg is a field of its own, asked for by name -- `--rehearse`, for a machine
    that cannot pass those gates and is proving the path rather than a number."""

    def test_it_reads_no_force_out_of_the_job(self):
        self.assertNotIn("jf force", AUTORUN.read_text())

    def test_the_only_force_a_leg_gets_comes_from_the_rehearsal_field(self):
        text = AUTORUN.read_text()
        # The code half of each line: a comment naming the flag is prose about
        # the rule, not a leg that gets it.
        forcing = [l for l in text.splitlines() if '--force' in l.split("#", 1)[0]]
        self.assertEqual(['    [ -z "$REHEARSAL" ] || set -- "$@" --force'], forcing, forcing)
        self.assertIn('REHEARSAL=$(jf rehearsal)', text)

    def test_the_plant_sets_that_field_from_its_own_flag_and_not_from_force(self):
        text = (REPO / "bench" / "mac-ab.sh").read_text()
        self.assertIn("--rehearse) REHEARSE=1; shift ;;", text)
        self.assertIn('WK_JOB_REHEARSAL="$REHEARSE"', text)
        for line in text.splitlines():
            if "REHEARSE=1" in line:
                self.assertIn("--rehearse", line,
                              "nothing but --rehearse may set it")


if __name__ == "__main__":
    unittest.main()
