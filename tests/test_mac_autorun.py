"""The script a macOS benchmark install runs by itself (lib/wk/bench/autorun.py)
and the readings it judges the install by (bench/mac-quiet-desktop.sh).

It runs with nobody in the room and no network, so anything it cannot decide
by itself it cannot ask about: a reading that hangs hangs the experiment, a
refusal nobody reads has to power the machine off, and a run that ends any way
at all has to leave its verdict on the volume. The autorun runs here against
the fake machine and the fake clock.

Run: python3 tests/run.py --unit -k autorun
"""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest

from tests.support import REPO, WkTest, scratch_dir

sys.path.insert(0, str(REPO / "lib"))
from wk.bench import autorun, mac  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.kv import kv  # noqa: E402
from wk.machine import Fake, Killed, Result  # noqa: E402

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
        # Answered, nothing to say -- not a hang, and not this host's own
        # mdutil, which has an opinion about indexing / whether it is run.
        with scratch_dir() as tmp:
            binp = tmp / "bin"
            binp.mkdir()
            silent = binp / "mdutil"
            silent.write_text("#!/bin/sh\nexit 0\n")
            silent.chmod(0o755)
            cp = sh(f'set -euo pipefail\nPATH={binp}:$PATH\n. {str(QUIET)!r}\n'
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




ROOT = "/var/wk"
TOOLS = ROOT + "/wk-tools"
HOME = "/Users/bench"
AGENT = HOME + "/Library/LaunchAgents/com.wk.bench-ab.plist"
STATE = ROOT + "/autorun.state"
RUNS = ROOT + "/ab/S1"
HOST = "/Volumes/Macintosh HD"
WKMAC = ["python3", TOOLS + "/" + mac.WKMAC]
CHECK = ["/usr/bin/python3", TOOLS + "/" + mac.CHECK]
WK = ["env", "WK_STORE=" + ROOT, TOOLS + "/wk"]
OK_ROWS = "ok\tApp Nap cannot throttle a backgrounded browser\t\n"
WRONG_ROWS = "wrong\tnot so: the screen does not lock (askforpassword reads '1')\tfix\n"
HALT = ("sudo", "-n", "/sbin/halt")
REBOOT = ("sudo", "-n", "/sbin/reboot")


def lib(rel, fn):
    return ["bash", "-c", '. "$0"; %s "$@"' % fn, TOOLS + "/" + rel]


def job(**over):
    d = {"plans": ["jetstream3"], "rounds": 2, "max_rounds": 4, "detect_pct": 0.3, "timeout": 1800, "count": "",
         "display": "builtin 1512x982", "settle": 90, "n_arms": 2, "rehearsal": "", "aslr": "", "env_pad": "",
         "path_pad": "", "shared_cache": "",
         "arms": [{"label": "A", "id": "sa", "browser_args": ""}, {"label": "B", "id": "sb", "browser_args": ""}]}
    d.update(over)
    return d


class World:
    """A planted bench install: marker, job, state, agent, a staged arm A, one host install to hand back to."""

    def __init__(self, job_doc=None, env=None):
        self.fake, self.clock, self.out = Fake("bench"), FakeClock(), io.StringIO()
        self.env = dict({"HOME": HOME, "WK_AB_ROOT": ROOT}, **(env or {}))
        self.armed, self.results, self.stamp = [], 0, "one"
        f = self.fake
        f._set_file(mac.MARKER, "id=perf-macos-tolken\n")
        if job_doc is not False:
            f._set_file(ROOT + "/job.json", json.dumps(job() if job_doc is None else job_doc))
        f._set_file(STATE, "phase=planted\njob_stamp=S1\nattempts=0\n")
        f._set_file(AGENT, "<plist/>")
        for rel in ("wk", "bench/" + "mac-quiet-desktop.sh", "lib/quiet.sh"):
            f._set_file(TOOLS + "/" + rel, "")
        f._set_file(HOST + "/System/Library/CoreServices/SystemVersion.plist", "")
        for d in (ROOT + "/staged/sa/WebKitBuild/Release", ROOT + "/results", "/var/folders/T"):
            while d != "/":
                f.dirs.add(d)
                d = os.path.dirname(d)
        answers = {("sysctl",): "{ sec = 1757000000, usec = 0 } Thu", ("stat", "-f", "%d", "/"): "1",
                   ("stat", "-f", "%d"): "2", tuple(WKMAC + ["brightness"]): "0.0", tuple(WKMAC + ["auto-brightness"]): "off",
                   tuple(WKMAC + ["display-mode"]): "1512x982", tuple(WKMAC + ["volume-group"]): "HOSTGRP",
                   tuple(WKMAC + ["boot-volume"]): "AAA:HOSTGRP", tuple(CHECK + ["--displays-only"]): "displays=built-in",
                   ("getconf",): "/var/folders/T", tuple(lib("lib/quiet.sh", "screen_blocker")): "",
                   tuple(lib(autorun.DESKTOP, "wk_quiet_desktop_probe")): "askforpassword=0\n",
                   tuple(lib(autorun.DESKTOP, "wk_quiet_desktop_findings")): OK_ROWS,
                   ("/usr/bin/python3", TOOLS + "/lib/wkdata.py"): "met=yes\n"}
        for prefix, out in answers.items():
            f.answer(list(prefix), out=out)
        for prefix in (("test",), ("sudo", "-n"), ("sync",), tuple(CHECK + ["--build-directory"]), ("env",)):
            f.answer(list(prefix))
        f.answer(["pgrep"], rc=1)
        f.answer(["sudo", "-n", "launchctl", "print"], rc=1)
        f.react(["/usr/bin/plutil"], lambda a, f: Result(0, '  "LastFullCheckDate" => %s\n' % self.stamp))
        f.react(WK + ["bench", "staged"], self.staged)
        f.react(["env", "WK_STORE=" + ROOT, "WK_BENCH_ASLR=off"], self.staged)

    def staged(self, argv, fake):
        if "staged" not in argv:
            return Result(0)
        self.results += 1
        fake.dirs.add("%s/results/r%03d" % (ROOT, self.results))
        return Result(0)

    def autorun(self):
        return autorun.Autorun(self.fake, self.clock, self.env, tools=TOOLS, out=self.out,
                               thread=lambda target, daemon: type("T", (), {"start": lambda s: self.armed.append(target)})())

    def run(self):
        with contextlib.redirect_stderr(io.StringIO()) as self.err:
            return self.autorun().run()

    def state(self):
        return kv(self.fake.files.get(STATE, ""))

    def runs(self):
        return self.fake.files.get(RUNS + "/runs.tsv", "")

    def calls(self, kind="run"):
        return [e[1] for e in self.fake.effects if e[0] == kind]

    def legs(self):
        return [a for a in self.calls("run_tty") if a[0] == "env" and TOOLS + "/wk" in a and "staged" in a]

    def arm_ids(self):
        return [a[a.index("--id") + 1] for a in self.legs()]

    def power(self):
        return [a for a in self.calls() if a in (HALT, REBOOT)]

    def log(self):
        return self.out.getvalue()


class TestTheJobRuns(unittest.TestCase):
    def test_a_planted_job_measures_and_hands_the_machine_back(self):
        w = World()
        self.assertEqual(0, w.run(), w.log())
        s = w.state()
        self.assertEqual(("done", "resolved-at-round-2", "1"), (s["phase"], s["outcome"], s["attempts"]))
        self.assertEqual([REBOOT], w.power())
        self.assertIn(AGENT, w.fake.files, "a finished job's next boot removes the agent, not this one")

    def test_the_arms_are_counterbalanced_after_a_warmup_of_each(self):
        w = World()
        w.run()
        self.assertEqual(["sa", "sb", "sa", "sb", "sb", "sa"], w.arm_ids())

    def test_the_warmup_is_profiled_and_never_compared(self):
        w = World()
        w.run()
        profiled = [a[a.index("--profile") + 1] for a in w.legs() if "--profile" in a]
        self.assertEqual([RUNS + "/warmup/jetstream3-A.json.gz", RUNS + "/warmup/jetstream3-B.json.gz"], profiled)
        rows = [l.split("\t") for l in w.runs().splitlines()]
        self.assertEqual(["1", "1", "2", "2"], [r[0] for r in rows])
        self.assertEqual({"clean"}, {r[4] for r in rows})

    def test_the_stopping_rule_is_given_each_arms_clean_run_directories(self):
        w = World()
        w.run()
        ask = [a for a in w.calls() if "ab-precision" in a][0]
        self.assertEqual("%s/results/r003,%s/results/r006" % (ROOT, ROOT), ask[ask.index("--a") + 1])
        self.assertEqual("0.3", ask[ask.index("--target") + 1])

    def test_detect_zero_runs_exactly_the_rounds_asked_for(self):
        for zero in (0, 0.0, "0"):
            w = World(job(detect_pct=zero, rounds=3))
            w.run()
            self.assertEqual(2 + 3 * 2, len(w.legs()), zero)
            self.assertEqual("rounds-done", w.state()["outcome"])
            self.assertFalse([a for a in w.calls() if "ab-precision" in a])

    def test_a_target_it_cannot_reach_stops_at_the_ceiling(self):
        w = World()
        w.fake.answer(["/usr/bin/python3", TOOLS + "/lib/wkdata.py"], out="met=no\n")
        w.run()
        self.assertEqual(("hit-max-rounds", "4"), (w.state()["outcome"], w.state()["rounds_done"]))

    def test_a_scanned_arm_is_kept_and_left_out_of_the_comparison(self):
        w = World()

        def scanning(argv, fake):
            if w.results == 3:
                w.stamp = "two"
            return w.staged(argv, fake)
        w.fake.react(WK + ["bench", "staged"], scanning)
        w.run()
        self.assertIn("\tscanned\t", w.runs())
        ask = [a for a in w.calls() if "ab-precision" in a][0]
        self.assertNotIn("r004", ask[ask.index("--b") + 1])

    def test_every_arm_failing_stops_after_the_first_round(self):
        w = World()
        w.fake.answer(WK + ["bench", "staged"], rc=3)
        w.run()
        self.assertEqual("all-failed-round-1", w.state()["outcome"])
        self.assertEqual("3", w.state()["fail_jetstream3_A_1"])
        self.assertEqual(4, len(w.legs()))

    def test_each_leg_carries_the_jobs_display_count_and_variance(self):
        w = World(job(count="3", aslr="off", arms=[{"label": "A", "id": "sa", "browser_args": "--x"},
                                                  {"label": "B", "id": "sb"}]))
        w.run()
        leg = w.legs()[-1]
        self.assertEqual(["env", "WK_STORE=" + ROOT, "WK_BENCH_ASLR=off"], list(leg[:3]))
        self.assertEqual("builtin 1512x982", leg[leg.index("--expect-display") + 1])
        self.assertEqual("3", leg[leg.index("--count") + 1])
        self.assertIn("--x", w.legs()[0])
        self.assertNotIn("--force", leg)

    def test_a_job_that_names_no_count_measures_two_runs(self):
        w = World()
        w.run()
        self.assertEqual({"2"}, {a[a.index("--count") + 1] for a in w.legs()})

    def test_only_a_rehearsal_forces_a_leg(self):
        w = World(job(rehearsal="1"))
        w.run()
        self.assertTrue(all("--force" in a for a in w.legs()))

    def test_every_leg_re_pauses_the_daemons_first(self):
        w = World()
        w.run()
        pause = ("sudo", "-n") + tuple(lib(autorun.DESKTOP, "wk_quiet_daemons_pause"))
        tty = w.calls("run_tty")
        self.assertTrue(all(tty[tty.index(leg) - 1] == pause for leg in w.legs()))

    def test_the_state_is_done_before_the_summary_runs(self):
        w = World()
        seen = []
        w.fake.react(WK + ["bench", "ab-summary"], lambda a, f: seen.append(w.state()["phase"]) or Result(0))
        w.run()
        self.assertEqual(["done"], seen)

    def test_the_install_converges_itself_before_the_display_gates(self):
        w = World()
        w.fake.dirs.add(ROOT + "/tailnet")
        w.run()
        tty = [a for a in w.calls("run_tty")]
        py = ("sudo", "-n", "env", "PYTHONPATH=%s/lib" % TOOLS, "python3", "-m")
        self.assertEqual([py + ("wk.sysimage.macvolume", "stage-payload", "/"),
                          py + ("wk.sysimage.mactailnet", "install", "/", ROOT + "/tailnet"),
                          ("sudo", "-n", TOOLS + "/bench/mac-tailnet.sh", "join")], tty[:3])

    def test_the_software_update_scanner_is_stopped(self):
        w = World()
        w.run()
        for svc in autorun.UPDATERS:
            self.assertIn(("sudo", "-n", "launchctl", "bootout", svc), w.calls())

    def test_a_window_in_front_is_closed_but_setup_assistant_is_left(self):
        w = World()
        w.fake.answer(lib("lib/quiet.sh", "screen_blocker"), out="Setup Assistant,Feedback Assistant\n")
        w.run()
        self.assertIn(("sudo", "-n", "pkill", "-f", "Feedback Assistant.app"), w.calls())
        self.assertNotIn(("sudo", "-n", "pkill", "-f", "Setup Assistant.app"), w.calls())

    def test_a_modal_authentication_panel_is_killed(self):
        w = World()
        w.fake.answer(["pgrep", "-x", "SecurityAgent"])
        w.run()
        self.assertIn(("sudo", "-n", "killall", "-9", "SecurityAgent"), w.calls())


class TestTheSettle(unittest.TestCase):
    def test_it_settles_for_the_jobs_time_once_the_phase_is_running(self):
        w = World(job(settle=37))
        slept = []
        w.clock.sleep = lambda s: slept.append((s, w.state().get("phase")))
        w.run()
        self.assertIn((37, "running"), slept)

    def test_it_waits_for_the_per_user_temp_directory(self):
        w = World()
        answers = iter(["", ""])
        w.fake.react(["getconf"], lambda a, f: Result(0, next(answers, "/var/folders/T")))
        w.run()
        self.assertEqual(2, w.clock.slept.count(autorun.TEMP_POLL))
        self.assertIn("temp: /var/folders/T", w.log())


class TestTheMachineEndsUpOff(unittest.TestCase):
    def leave(self, w):
        a = w.autorun()
        with contextlib.redirect_stderr(io.StringIO()):
            a.leave("the reason")
            a.leave("a second reason")
        return w

    def test_a_bless_that_took_hands_the_machine_back(self):
        w = self.leave(World())
        self.assertIn(("sudo", "-n", "bless", "--mount", HOST, "--setBoot"), w.calls())
        self.assertEqual([REBOOT], w.power())

    def test_a_bless_that_did_not_take_halts_instead(self):
        w = World()
        w.fake.answer(WKMAC + ["boot-volume"], out="AAA:BENCHGRP")
        self.assertEqual([HALT], self.leave(w).power())

    def test_no_host_install_to_hand_back_to_halts(self):
        w = World()
        w.fake._drop("/Volumes")
        self.assertEqual([HALT], self.leave(w).power())

    def test_a_volume_carrying_the_bench_marker_is_not_the_host_install(self):
        w = World()
        w.fake._set_file(HOST + mac.MARKER, "id=x\n")
        self.assertEqual([HALT], self.leave(w).power())

    def test_two_host_installs_are_not_one_to_hand_back_to(self):
        w = World()
        w.fake._set_file("/Volumes/Other/System/Library/CoreServices/SystemVersion.plist", "")
        self.assertEqual([HALT], self.leave(w).power())

    def test_the_first_reason_is_the_one_recorded(self):
        w = self.leave(World())
        self.assertIn("the reason", w.log())
        self.assertNotIn("a second reason", w.log())

    def test_host_mode_is_the_one_exit_that_touches_nothing_but_the_agent(self):
        w = World()
        w.fake._drop(mac.MARKER)
        w.run()
        self.assertEqual([], w.power())
        self.assertNotIn(AGENT, w.fake.files)
        self.assertEqual([("remove", AGENT)], [e for e in w.fake.effects if e[0] in ("write", "remove")])

    def test_an_unexpected_failure_after_arming_still_leaves(self):
        w = World()
        w.fake.react(["getconf"], lambda a, f: 1 / 0)
        with self.assertRaises(ZeroDivisionError):
            w.run()
        self.assertEqual([REBOOT], w.power())


class TestTheRefusals(unittest.TestCase):
    def refused(self, w, rc=0):
        self.assertEqual(rc, w.run(), w.log())
        self.assertEqual([], w.legs())
        self.assertEqual(1, len(w.power()), w.power())
        return w

    def test_no_job_removes_the_agent_and_powers_off(self):
        w = self.refused(World(job_doc=False))
        self.assertNotIn(AGENT, w.fake.files)

    def test_a_finished_job_booting_again_retires_the_agent(self):
        w = World()
        w.fake._set_file(STATE, "phase=done\nattempts=1\n")
        w = self.refused(w)
        self.assertNotIn(AGENT, w.fake.files)
        self.assertEqual("1", w.state()["attempts"])

    def test_the_fourth_attempt_abandons_the_job(self):
        w = World()
        w.fake._set_file(STATE, "phase=running\nattempts=3\n")
        self.assertEqual(("done", "abandoned"), (self.refused(w).state()["phase"], w.state()["outcome"]))

    def test_it_stands_aside_while_provisioning_is_running(self):
        w = World()
        w.fake.answer(["pgrep", "-f", "wk-bench-firstboot"])
        self.assertEqual(0, w.run())
        self.assertEqual([], w.power())
        self.assertEqual([], [e for e in w.fake.effects if e[0] in ("write", "remove")])

    def test_a_first_boot_daemon_that_outlived_provisioning_is_defused(self):
        w = World()
        w.fake._set_file(autorun.FB_PLIST, "")
        w.run()
        self.assertIn(("sudo", "-n", "rm", "-f", autorun.FB_PLIST, autorun.FB_SELF), w.calls())

    def test_a_reboot_someone_else_scheduled_is_cancelled(self):
        w = World()
        w.fake.answer(["pgrep", "-x", "shutdown"])
        w.run()
        self.assertIn(("sudo", "-n", "pkill", "-x", "shutdown"), w.calls())

    def test_a_display_that_will_not_dim_measures_nothing(self):
        w = World()
        w.fake.answer(WKMAC + ["brightness"], rc=1, out="0.4")
        self.refused(w)

    def test_a_job_that_pins_no_display_measures_nothing(self):
        w = self.refused(World(job(display="")))
        self.assertEqual("no-display-expectation", w.state()["outcome"])
        self.assertNotIn(AGENT, w.fake.files)

    def test_ambient_light_that_will_not_let_go_spends_no_attempt(self):
        w = World()
        w.fake.answer(WKMAC + ["auto-brightness"], out="on")
        self.assertEqual("0", self.refused(w).state()["attempts"])

    def test_ambient_light_is_judged_by_its_reading_and_not_by_the_write(self):
        w = World()
        w.fake.answer(WKMAC + ["auto-brightness", "--off"], out="off")
        w.fake.answer(WKMAC + ["auto-brightness"], out="on")
        self.refused(w)

    def test_a_panel_with_no_sensor_is_not_a_refusal(self):
        w = World()
        w.fake.answer(WKMAC + ["auto-brightness"], out="none")
        w.run()
        self.assertTrue(w.legs())

    def test_a_wrong_display_spends_no_attempt_and_writes_no_mode(self):
        w = World()
        w.fake.answer(CHECK + ["--displays-only"], rc=1, out="displays=2 online")
        w = self.refused(w)
        self.assertEqual("0", w.state()["attempts"])
        self.assertFalse([a for a in w.calls() if "--declare" in a])

    def test_a_mode_that_is_not_the_declared_one_is_written_and_the_boot_repeated(self):
        w = World()
        w.fake.answer(WKMAC + ["display-mode"], out="1800x1169")
        self.assertEqual(0, w.run())
        self.assertIn(("sudo", "-n") + tuple(WKMAC) + ("display-mode", "--declare", "1512x982"), w.calls())
        self.assertEqual([REBOOT], w.power())
        self.assertNotIn(("sudo", "-n", "bless", "--mount", HOST, "--setBoot"), w.calls())
        self.assertEqual(("0", "1512x982"), (w.state()["attempts"], w.state()["mode_declared"]))

    def test_a_mode_write_that_did_not_take_is_refused_the_second_time(self):
        w = World()
        w.fake.answer(WKMAC + ["display-mode"], out="1800x1169")
        w.fake._set_file(STATE, "phase=planted\njob_stamp=S1\nattempts=0\nmode_declared=1512x982\n")
        self.assertEqual("display-mode-unsettable", self.refused(w).state()["outcome"])

    def test_a_mode_the_configuration_will_not_take_is_refused(self):
        w = World()
        w.fake.answer(WKMAC + ["display-mode"], out="1800x1169")
        w.fake.answer(["sudo", "-n"] + WKMAC, rc=1)
        self.assertEqual("display-mode-unwritable", self.refused(w).state()["outcome"])

    def test_a_setting_that_drifted_refuses_the_whole_job_once_after_the_quiesce(self):
        w = World()
        w.fake.answer(lib(autorun.DESKTOP, "wk_quiet_desktop_findings"), out=OK_ROWS + WRONG_ROWS)
        w = self.refused(w)
        self.assertIn("not so: the screen does not lock", w.log())
        self.assertIn("wk sysimage build perf-macos-tolken --repair", w.log())
        order = [e[1][:len(WK) + 1] for e in w.fake.effects if e[0] in ("run", "run_tty")]
        self.assertLess(order.index(tuple(WK) + ("quiesce",)), order.index(tuple(lib(autorun.DESKTOP, "wk_quiet_desktop_probe"))[:4]))

    def test_a_volume_with_no_table_to_be_judged_by_measures_nothing(self):
        w = World()
        w.fake._drop(TOOLS + "/" + autorun.DESKTOP)
        self.refused(w)

    def test_a_throttled_browser_stops_the_job_before_round_one(self):
        w = World()
        w.fake.answer(CHECK + ["--build-directory"], rc=1)
        w = self.refused(w)
        check = [a for a in w.calls("run_tty") if "--build-directory" in a][0]
        self.assertEqual(ROOT + "/staged/sa/WebKitBuild/Release", check[check.index("--build-directory") + 1])
        self.assertEqual(RUNS + "/browser-check.json", check[check.index("--json") + 1])

    def test_an_arm_with_no_products_is_not_measured_around(self):
        w = World()
        w.fake._drop(ROOT + "/staged")
        self.refused(w)

    def test_a_tree_with_no_wk_is_the_one_failing_exit(self):
        w = World()
        w.fake._drop(TOOLS + "/wk")
        self.assertEqual("no-wk-tools", self.refused(w, rc=1).state()["outcome"])


class TestANumberlessBootIsHeld(unittest.TestCase):
    def test_a_boot_with_no_runs_is_held_for_a_reader(self):
        w = World(env={"WK_MAC_BENCH_HOLD": "120"})
        w.fake.answer(CHECK + ["--build-directory"], rc=1)
        w.run()
        self.assertIn(120, w.clock.slept)
        self.assertIn("tail -120 %s/autorun.log" % ROOT, w.log())

    def test_a_boot_whose_legs_landed_hands_back_at_once(self):
        w = World(env={"WK_MAC_BENCH_HOLD": "120"})
        w.run()
        self.assertNotIn(120, w.clock.slept)

    def test_a_boot_that_never_reached_the_job_is_not_held(self):
        w = World(job_doc=False, env={"WK_MAC_BENCH_HOLD": "120"})
        w.run()
        self.assertNotIn(120, w.clock.slept)

    def test_it_can_be_turned_off(self):
        w = World(env={"WK_MAC_BENCH_HOLD": "0"})
        w.fake.answer(CHECK + ["--build-directory"], rc=1)
        w.run()
        self.assertEqual([REBOOT], w.power())
        self.assertNotIn("Holding", w.log())


class TestTheWatchdog(unittest.TestCase):
    def watch(self, w, mtime):
        a = w.autorun()
        a.stall, a.runs = 1000, RUNS
        w.fake.react(["stat", "-f", "%m"], lambda argv, f: Result(0, str(mtime(a))))
        with contextlib.redirect_stderr(io.StringIO()):
            a.watchdog()
        return a

    def test_silence_past_the_bound_summarises_and_powers_off(self):
        w = World()
        self.watch(w, lambda a: int(w.clock.now()) - 1001)
        self.assertEqual(("done", "watchdog"), (w.state()["phase"], w.state()["outcome"]))
        self.assertTrue([a for a in w.calls("run_tty") if "ab-summary" in a])
        self.assertEqual(1, len(w.power()))

    def test_a_log_still_being_written_is_left_running_until_the_job_is_done(self):
        w = World()

        def fresh(a):
            if len(w.clock.slept) == 3:
                w.fake._set_file(STATE, "phase=done\n")
            return int(w.clock.now()) - 10
        self.watch(w, fresh)
        self.assertEqual([autorun.WATCH_POLL] * 4, w.clock.slept)
        self.assertEqual([], w.power())

    def test_its_bound_is_the_leg_timeout_and_a_grace(self):
        w = World(job(timeout=600))
        w.run()
        self.assertIn("watchdog: %ds of silence" % (600 + autorun.STALL_GRACE), w.log())

    def test_it_is_armed_before_the_panel_is_touched(self):
        w = World()
        order = []
        w.fake.react(WKMAC + ["brightness"], lambda a, f: order.append(len(w.armed)) or Result(0, "0.0"))
        w.run()
        self.assertEqual([1], order)


class TestAKilledRunResumes(unittest.TestCase):
    """A kill is a power cut: the next boot starts the agent again and the job still ends done, off, with no warmup row compared."""

    def test_a_run_killed_after_any_effect_finishes_on_the_next_boot(self):
        for n in range(400):
            w = World()
            w.fake.stop_after = n
            try:
                w.run()
            except Killed:
                pass
            else:
                return
            w.fake.stop_after = None
            w.fake.effects = []
            w.run()
            with self.subTest(killed_after=n):
                self.assertEqual("done", w.state().get("phase"))
                self.assertEqual(1, len(w.power()))
                self.assertFalse([l for l in w.runs().splitlines() if l.startswith("0\t")])
        self.fail("the run made more than 400 effects")

    def test_a_resumed_run_counts_its_attempt(self):
        w = World()
        w.fake._set_file(STATE, "phase=running\njob_stamp=S1\nattempts=1\n")
        w.run()
        self.assertEqual("2", w.state()["attempts"])


class TestDryRun(unittest.TestCase):
    def setUp(self):
        os.environ["WK_DRY_RUN"] = "1"
        self.addCleanup(os.environ.pop, "WK_DRY_RUN", None)

    def test_a_dry_run_changes_nothing_and_names_the_power_off(self):
        w = World()
        files, dirs = dict(w.fake.files), set(w.fake.dirs)
        w.run()
        self.assertEqual((files, dirs), (w.fake.files, w.fake.dirs))
        self.assertEqual([], w.calls("run_tty"))
        self.assertRegex(w.err.getvalue(), r"would run[^:\n]*: sudo -n /sbin/reboot\n")
        self.assertIn("would run: env WK_STORE=/var/wk %s/wk bench staged" % TOOLS, w.err.getvalue())


class TestTheOverrides(unittest.TestCase):
    def test_wk_ab_root_moves_every_path(self):
        a = autorun.Autorun(Fake(), FakeClock(), {"WK_AB_ROOT": "/tmp/wk-selftest-ab"}, tools=TOOLS)
        self.assertEqual(("/tmp/wk-selftest-ab/job.json", "/tmp/wk-selftest-ab/autorun.state", "/tmp/wk-selftest-ab/autorun.log"),
                         (a.job_path, a.state_path, a.log_path))

    def test_it_defaults_to_the_bench_root(self):
        self.assertEqual(mac.BENCH_ROOT, autorun.Autorun(Fake(), FakeClock(), {}, tools=TOOLS).root)

    def test_it_takes_no_arguments(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, autorun.main(["--now"]))


class TestTheAgentRunsThisFile(unittest.TestCase):
    def test_the_agent_starts_this_module_in_the_planted_tree(self):
        self.assertEqual(mac.BENCH_ROOT + "/wk-tools/lib/wk/bench/autorun.py", mac.AUTORUN)
        self.assertTrue((REPO / "lib" / "wk" / "bench" / "autorun.py").is_file())
        self.assertIn("<string>/usr/bin/python3</string>", mac.PLIST)

    def test_the_join_cannot_wait_forever(self):
        """`tailscale up` without --timeout waits for the backend to reach Running for as long as that takes."""
        text = (REPO / "bench" / "mac-tailnet.sh").read_text()
        self.assertRegex(text, r"tailscale\" up --timeout=\d+s ")


if __name__ == "__main__":
    unittest.main()
