"""The planted A/B a macOS bench install runs by itself (lib/wk/bench/autorun.py): the machine readied, the job's AB
handed what the job names, the machine handed back or off however it ends; and the readings it judges the install by
(bench/mac-quiet-desktop.sh)."""
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.support import REPO, WkTest, scratch_dir

sys.path.insert(0, str(REPO / "lib"))
from wk import screen  # noqa: E402
from wk.bench import ab, autorun, mac  # noqa: E402
from wk.images import MARKER  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.kv import kv  # noqa: E402
from wk.machine import Fake, Killed, Result  # noqa: E402

QUIET = REPO / "bench" / "mac-quiet-desktop.sh"


def sh(script, cwd=None, timeout=120):
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          timeout=timeout, cwd=cwd)


class TestAReadingCannotHangALeg(WkTest):

    def _read(self, script):
        return sh(f'set -euo pipefail\n. {str(QUIET)!r}\n{script}\n')

    def test_a_reading_that_answers_comes_back_whole(self):
        cp = self._read('_wk_qd_read 10 printf "one\\ntwo\\n"')
        self.assertEqual("one\ntwo\n", cp.stdout, cp.stderr)
        self.assertEqual(0, cp.returncode, cp.stderr)

    def test_a_reading_that_answers_nothing_is_empty_and_not_a_timeout(self):
        for command in ("true", "false", "nosuchcommandanywhere"):
            with self.subTest(command=command):
                cp = self._read(f'_wk_qd_read 10 {command}')
                self.assertEqual("", cp.stdout, cp.stderr)

    def test_a_reading_that_does_not_answer_is_given_up_on_inside_the_bound(self):
        cp = self._read('_wk_qd_read 1 sleep 120')
        self.assertEqual("!timeout", cp.stdout, cp.stderr)

    def test_stderr_is_part_of_the_reading_only_when_it_is_asked_for(self):
        for flag, want in (("-e ", "outerr"), ("", "out")):
            with self.subTest(flag=flag):
                cp = self._read("""_wk_qd_read %s10 sh -c 'printf out; printf err >&2'""" % flag)
                self.assertEqual(want, cp.stdout, cp.stderr)

    def test_the_merged_form_is_bounded_through_a_grandchild_too(self):
        with scratch_dir() as tmp:
            binp = tmp / "bin"
            binp.mkdir()
            hang = binp / "tmutil"
            hang.write_text('#!/bin/sh\nprintf "starting\\n" >&2\nsleep 120 & wait\n')
            hang.chmod(0o755)
            cp = sh(f'set -euo pipefail\nPATH={binp}:$PATH\n. {str(QUIET)!r}\n'
                    f'_wk_qd_read -e 1 tmutil destinationinfo\n')
        self.assertEqual("!timeout", cp.stdout, cp.stderr)

    def test_the_probe_says_which_of_the_two_spotlight_did(self):
        # exec: the bound kills the direct child; the silent one stands in for this host's own mdutil.
        for mdutil, bound, want in (("exec sleep 120", "_WK_QD_READ_SECS=1\n", "spotlight=!timeout\n"),
                                    ("exit 0", "", "spotlight=\n")):
            with self.subTest(want=want), scratch_dir() as tmp:
                binp = tmp / "bin"
                binp.mkdir()
                (binp / "mdutil").write_text("#!/bin/sh\n%s\n" % mdutil)
                (binp / "mdutil").chmod(0o755)
                cp = sh(f'set -euo pipefail\nPATH={binp}:$PATH\n. {str(QUIET)!r}\n'
                        f'{bound}wk_quiet_desktop_probe | grep "^spotlight="\n')
                self.assertEqual(want, cp.stdout, cp.stderr)


class TestATimedOutReadingIsUnknownAndNotAFault(WkTest):

    def _judge(self, probe):
        cp = sh(f'set -euo pipefail\n. {str(QUIET)!r}\n'
                f'wk_quiet_desktop_findings {probe!r} "the remedy"\n')
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        return [l.split("\t") for l in cp.stdout.splitlines() if l]

    def test_a_timed_out_reading_is_a_note_and_an_empty_one_keeps_its_own_verdict(self):
        for probe, word, kind, says, not_says in (
                ("spotlight=!timeout", "Spotlight", "note", "held stopped", ""),
                ("spotlight=!timeout", "Spotlight", "note", "XPC", ""),
                ("spotlight=", "Spotlight", "note", "", "held stopped"),
                ("spotlight=Indexing enabled.", "Spotlight", "wrong", "", ""),
                ("analytics=!timeout", "diagnostics", "note", "XPC", ""),
                ("analytics=", "diagnostics", "wrong", "", "")):
            with self.subTest(probe=probe):
                found = [f for f in self._judge(probe) if word in f[1]]
                self.assertEqual([kind], [f[0] for f in found], found)
                self.assertIn(says, found[0][1])
                if not_says:
                    self.assertNotIn(not_says, found[0][1])


ROOT = "/var/wk"
TOOLS = autorun.TREE
HOME = "/Users/bench"
AGENT = HOME + "/Library/LaunchAgents/com.wk.bench-ab.plist"
STATE = ROOT + "/autorun.state"
STATE_TEXT = "phase=planted\ntask=T1\nattempts=0\n"
TASK = ROOT + "/results/T1"
HOST = "/Volumes/Macintosh HD"
WKMAC = ["python3", TOOLS + "/" + mac.WKMAC]
CHECK = ["/usr/bin/python3", TOOLS + "/" + mac.CHECK]
HALT = ("sudo", "-n", "/sbin/halt")
REBOOT = ("sudo", "-n", "/sbin/reboot")


def lib(rel, fn):
    return ["bash", "-c", '. "$0"; %s "$@"' % fn, TOOLS + "/" + rel]


def job(**over):
    d = {"task": "T1", "workspace": "mac-rel", "machine": "mbp", "plans": ["jetstream3"], "rounds": "2", "max_rounds": "4",
         "detect": "0.3", "timeout": "1800", "count": "", "display": "builtin 1512x982", "settle": "90", "rehearsal": "", "aslr": "",
         "env_pad": "", "path_pad": "", "shared_cache": "",
         "arms": [{"label": "A", "id": "sa", "browser_args": ""}, {"label": "B", "id": "sb", "browser_args": ""}]}
    d.update(over)
    return d


class World:
    """A planted bench install: marker, job, state, agent, one host install to hand back to; the job's AB a recorder."""

    def __init__(self, job_doc=None, env=None, outcome="resolved-at-round-2", legs=2):
        self.fake, self.clock = Fake("bench"), FakeClock()
        self.env = dict({"HOME": HOME, "WK_BENCH_ROOT": ROOT}, **(env or {}))
        self.armed, self.abs, self.outcome, self.n_legs = [], [], outcome, legs
        f = self.fake
        f._set_file(MARKER, "id=perf-macos-tolken\n")
        if job_doc is not False:
            f._set_file(ROOT + "/job.json", json.dumps(job() if job_doc is None else job_doc))
        f._set_file(STATE, STATE_TEXT)
        f._set_file(AGENT, "<plist/>")
        f._set_file(TOOLS + "/wk", "")
        f._set_file(HOST + "/System/Library/CoreServices/SystemVersion.plist", "")
        f.dirs.update({"/var/folders/T", TASK + "/runs"})
        answers = {("sysctl",): "{ sec = 1757000000, usec = 0 } Thu", ("stat", "-f", "%d", "/"): "1",
                   ("stat", "-f", "%d"): "2", tuple(WKMAC + ["brightness"]): "0.0", tuple(WKMAC + ["auto-brightness"]): "off",
                   tuple(WKMAC + ["display-mode"]): "1512x982", tuple(WKMAC + ["volume-group"]): "HOSTGRP",
                   tuple(WKMAC + ["boot-volume"]): "AAA:HOSTGRP", tuple(CHECK + ["--displays-only"]): "displays=built-in",
                   ("getconf",): "/var/folders/T", tuple(lib(screen.WINDOWS, "wk_window_probe")): "windows=MiniBrowser:Speedometer\n",
                   tuple(lib(screen.WINDOWS, "wk_window_unexpected")): ""}
        for prefix, out in answers.items():
            f.answer(list(prefix), out=out)
        for prefix in (("test",), ("sudo", "-n"), ("sync",), ("env",)):
            f.answer(list(prefix))
        f.answer(["pgrep"], rc=1)
        f.answer(["sudo", "-n", "launchctl", "print"], rc=1)

    def ab(self, root, reg, clock, ws, names, o, kind, arms, reporter, defaults):
        """The job's A/B as the autorun hands it over: each leg a run under the task the plant wrote."""
        world = self
        world.abs.append(dict(ws=ws, plans=names, o=o, kind=kind, arms=[(a.label, a.slot, a.args) for a in arms], defaults=defaults,
                              env=reg.env, system=arms[0].system))

        class Recorded:
            outcome = ""

            def go(self):
                for i in range(0 if os.environ.get("WK_DRY_RUN") else world.n_legs):
                    world.fake._set_file("%s/runs/r%d/env.json" % (TASK, i), "{}")
                self.outcome = world.outcome
                return 0
        return Recorded()

    def run(self):
        with contextlib.redirect_stderr(io.StringIO()) as self.err, contextlib.redirect_stdout(io.StringIO()) as self.out, \
                mock.patch.object(autorun, "AB", self.ab):
            return autorun.Autorun(self.fake, self.clock, self.env, background=self.armed.append).run()

    def state(self):
        return kv(self.fake.files.get(STATE, ""))

    def calls(self, kind="run"):
        return [e[1] for e in self.fake.effects if e[0] == kind]

    def power(self):
        return [a for a in self.calls() if a in (HALT, REBOOT)]

    def log(self):
        return self.out.getvalue()


class TestTheJobRuns(unittest.TestCase):
    def test_a_planted_job_measures_into_its_task_and_hands_the_machine_back(self):
        w = World()
        self.assertEqual(0, w.run(), w.log())
        s = w.state()
        self.assertEqual(("done", "resolved-at-round-2", "1"), (s["phase"], s["outcome"], s["attempts"]))
        self.assertEqual([REBOOT], w.power())
        self.assertIn(AGENT, w.fake.files, "a finished job's next boot removes the agent, not this one")

    def test_the_ab_is_the_jobs_plans_arms_and_rule_over_this_volume_under_the_plants_task(self):
        w = World(job(count="3", plans=["jetstream3", "speedometer3"], arms=[{"label": "A", "id": "sa", "browser_args": "--x"},
                                                                             {"label": "B", "id": "sb"}]))
        w.run()
        (a,) = w.abs
        self.assertEqual((a["ws"], a["plans"], a["kind"], a["defaults"]), ("mac-rel", ["jetstream3", "speedometer3"], "slot", ab.PLANTED))
        self.assertEqual(a["o"], {"rounds": "2", "max_rounds": "4", "detect": "0.3", "count": "3", "timeout": "1800", "task": "T1"})
        self.assertEqual(a["arms"], [("A", "sa", "--x"), ("B", "sb", "")])
        self.assertEqual((a["system"].kind, a["system"].machine_name, a["env"]["WK_STORE"]), ("mac-volume", "mbp", ROOT))
        self.assertNotIn("WK_FORCE", a["env"])

    def test_the_variance_rides_on_the_env_and_only_a_rehearsal_forces_a_leg(self):
        w = World(job(aslr="off", rehearsal="1"))
        w.run()
        env = w.abs[0]["env"]
        self.assertEqual((env["WK_BENCH_ASLR"], env["WK_FORCE"]), ("off", "1"))

    def test_an_ab_that_ends_with_nothing_still_hands_back_and_records_it_failed(self):
        w = World(outcome="")
        w.run()
        self.assertEqual("failed", w.state()["outcome"])
        self.assertEqual(1, len(w.power()))

    def test_the_install_converges_itself_before_the_display_gates(self):
        w = World()
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

    def test_a_window_in_front_is_closed_but_setup_assistant_is_left_and_an_auth_panel_is_killed(self):
        w = World()
        w.fake.answer(lib(screen.WINDOWS, "wk_window_unexpected"),
                      out="Setup Assistant:Welcome;Feedback Assistant:Report;Feedback Assistant:Other;")
        w.fake.answer(["pgrep", "-x", "SecurityAgent"])
        w.run()
        self.assertIn(("sudo", "-n", "pkill", "-f", "Feedback Assistant.app"), w.calls())
        self.assertNotIn(("sudo", "-n", "pkill", "-f", "Setup Assistant.app"), w.calls())
        self.assertIn(("sudo", "-n", "killall", "-9", "SecurityAgent"), w.calls())


class TestTheSettle(unittest.TestCase):
    def test_it_settles_for_the_jobs_time_once_the_phase_is_running(self):
        w = World(job(settle="37"))
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
        a = autorun.Autorun(w.fake, w.clock, w.env, background=w.armed.append)
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()) as w.out:
            a.leave("the reason")
            a.leave("a second reason")
        return w

    def test_a_bless_that_took_hands_the_machine_back(self):
        w = self.leave(World())
        self.assertIn(("sudo", "-n", "bless", "--mount", HOST, "--setBoot"), w.calls())
        self.assertEqual([REBOOT], w.power())
        self.assertIn("the reason", w.log())
        self.assertNotIn("a second reason", w.log())

    def test_with_no_one_host_install_to_hand_back_to_it_halts(self):
        for why, setup in (("a bless that did not take", lambda f: f.answer(WKMAC + ["boot-volume"], out="AAA:BENCHGRP")),
                           ("no host install", lambda f: f._drop("/Volumes")),
                           ("a volume carrying the bench marker", lambda f: f._set_file(HOST + MARKER, "id=x\n")),
                           ("two host installs", lambda f: f._set_file("/Volumes/Other/System/Library/CoreServices/SystemVersion.plist", ""))):
            with self.subTest(why):
                w = World()
                setup(w.fake)
                self.assertEqual([HALT], self.leave(w).power())

    def test_host_mode_is_the_one_exit_that_touches_nothing_but_the_agent(self):
        w = World()
        w.fake._drop(MARKER)
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
        self.assertEqual([], w.abs)
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

    def test_a_job_that_cannot_run_ends_with_its_outcome_and_retires_the_agent(self):
        """A mode written once that still does not come up is unsettable; a tree with no wk is the one failing exit."""
        mode = lambda f: f.answer(WKMAC + ["display-mode"], out="1800x1169")   # noqa: E731
        for outcome, w, setup, rc in (
                ("abandoned", World(), lambda f: f._set_file(STATE, "phase=running\ntask=T1\nattempts=3\n"), 0),
                ("no-display-expectation", World(job(display="")), lambda f: None, 0),
                ("display-mode-unsettable", World(), lambda f: (mode(f), f._set_file(STATE, STATE_TEXT + "mode_declared=1512x982\n")), 0),
                ("display-mode-unwritable", World(), lambda f: (mode(f), f.answer(["sudo", "-n"] + WKMAC, rc=1)), 0),
                ("no-wk-tools", World(), lambda f: f._drop(TOOLS + "/wk"), 1)):
            with self.subTest(outcome):
                setup(w.fake)
                self.assertEqual(("done", outcome), (self.refused(w, rc).state()["phase"], w.state()["outcome"]))
                self.assertNotIn(AGENT, w.fake.files)

    def test_each_unmeasurable_state_measures_nothing(self):
        for why, setup in (("a panel that will not dim", lambda f: f.answer(WKMAC + ["brightness"], rc=1, out="0.4")),
                           ("ambient light judged by its reading", lambda f: (f.answer(WKMAC + ["auto-brightness", "--off"], out="off"),
                                                                              f.answer(WKMAC + ["auto-brightness"], out="on")))):
            with self.subTest(why):
                w = World()
                setup(w.fake)
                self.refused(w)

    def test_ambient_light_that_will_not_let_go_spends_no_attempt_and_a_panel_with_no_sensor_is_none(self):
        w = World()
        w.fake.answer(WKMAC + ["auto-brightness"], out="on")
        self.assertEqual("0", self.refused(w).state()["attempts"])
        w = World()
        w.fake.answer(WKMAC + ["auto-brightness"], out="none")
        w.run()
        self.assertTrue(w.abs)

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


class TestANumberlessBootIsHeld(unittest.TestCase):
    def test_a_boot_with_no_runs_is_held_for_a_reader_unless_turned_off(self):
        w = World(env={"WK_MAC_BENCH_HOLD": "120"}, legs=0)
        w.run()
        self.assertIn(120, w.clock.slept)
        self.assertIn("tail -120 %s/autorun.log" % ROOT, w.log())
        w = World(env={"WK_MAC_BENCH_HOLD": "0"}, legs=0)
        w.run()
        self.assertEqual([REBOOT], w.power())
        self.assertNotIn("Holding", w.log())

    def test_a_boot_whose_legs_landed_or_that_never_reached_the_job_is_not_held(self):
        for w in (World(env={"WK_MAC_BENCH_HOLD": "120"}), World(job_doc=False, env={"WK_MAC_BENCH_HOLD": "120"})):
            w.run()
            self.assertNotIn(120, w.clock.slept)


class TestTheWatchdog(unittest.TestCase):
    def watch(self, w, mtime):
        a = autorun.Autorun(w.fake, w.clock, w.env, background=w.armed.append)
        a.stall = 1000
        w.fake.react(["stat", "-f", "%m"], lambda argv, f: Result(0, str(mtime(a))))
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            a.watchdog()
        return a

    def test_silence_past_the_bound_powers_off(self):
        w = World()
        self.watch(w, lambda a: int(w.clock.now()) - 1001)
        self.assertEqual(("done", "watchdog"), (w.state()["phase"], w.state()["outcome"]))
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

    def test_its_bound_is_the_leg_timeout_and_a_grace_and_it_is_armed_before_the_panel_is_touched(self):
        w = World(job(timeout="600"))
        order = []
        w.fake.react(WKMAC + ["brightness"], lambda a, f: order.append(len(w.armed)) or Result(0, "0.0"))
        w.run()
        self.assertIn("watchdog: %ds of silence" % (600 + autorun.STALL_GRACE), w.log())
        self.assertEqual([1], order)


class TestAKilledRunResumes(unittest.TestCase):
    """A kill is a power cut: the next boot starts the agent again and the job still ends done and off."""

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
        self.fail("the run made more than 400 effects")

    def test_a_resumed_run_counts_its_attempt(self):
        w = World()
        w.fake._set_file(STATE, "phase=running\ntask=T1\nattempts=1\n")
        w.run()
        self.assertEqual("2", w.state()["attempts"])


class TestDryRun(unittest.TestCase):
    def test_a_dry_run_changes_nothing_and_names_the_power_off(self):
        w = World()
        files, dirs = dict(w.fake.files), set(w.fake.dirs)
        with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
            w.run()
        self.assertEqual((files, dirs), (w.fake.files, w.fake.dirs))
        self.assertEqual([], w.calls("run_tty"))
        self.assertRegex(w.err.getvalue(), r"would run[^:\n]*: sudo -n /sbin/reboot\n")

    def test_it_takes_no_arguments(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, autorun.main(["--now"]))


if __name__ == "__main__":
    unittest.main()
