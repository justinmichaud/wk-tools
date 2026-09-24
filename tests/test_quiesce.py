"""`wk quiesce`: lib/wk/quiet.py's on/off/status and the readings it judges a Mac by, against a fake
machine whose privileged helper, `sudo`, `defaults`, `tmutil` and in-target bash calls answer as a
real one would -- nothing here runs the real helper or signals a real daemon. Also the one state
directory, the helper's bound on a stopped daemon, `killpoints[quiesce]`, a dry run printing the
wet run's plan, and the live rows `quiesce.readback[<m>]` and `quiesce.classified[<m>]`.

Run: python3 tests/run.py -k tests.test_quiesce
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import pty
import subprocess
import sys
import types
import unittest
from unittest import mock

from tests.killpoints import converges
from tests.support import REPO, WkTest, bash, func_body, requires_machine

sys.path.insert(0, str(REPO / "lib"))
from wk import act, fleet, quiet  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import TIMED_OUT, Fake, Result  # noqa: E402

ROOT = str(REPO)
STATE = "/state/wk/quiesce"
PRIV = quiet.PRIV


def lib(rel, fn, *args):
    return tuple(quiet.lib_argv(ROOT, rel, fn, *args))


PAUSE = ("sudo", "-n") + lib(quiet.DESKTOP, "wk_quiet_daemons_pause")
RESUME = ("sudo", "-n") + lib(quiet.DESKTOP, "wk_quiet_daemons_resume")
USER = lib(quiet.DESKTOP, "wk_quiet_desktop_user")
RAISER_ON = lib(quiet.RAISER, "mac_raiser_on", STATE)
RAISER_OFF = lib(quiet.RAISER, "mac_raiser_off", STATE)
TMUTIL = ("tmutil", "destinationinfo")
UPDATES = ("sudo", "-n", "defaults", "read", "/Library/Preferences/com.apple.SoftwareUpdate", "AutomaticCheckEnabled")
HOSTS = lib(quiet.HOSTS, "wk_bench_hosts_present", "/etc/hosts")
RENDER = lib(quiet.COMMON, quiet.RENDER)


class World(Fake):
    """A Mac: the helper's verbs, the pause, the raiser and the desktop settings move `files` the way the real ones move the machine."""

    def __init__(self, macos=True, bench=False, helper=True):
        super().__init__("mac")
        self.macos = macos
        self.env = {"WK_QUIESCE_STATE": STATE, "TMPDIR": "/tmp", "HOME": "/home/u", "WK_IMAGE_MARKER": "/etc/wk-image"}
        self.clock = FakeClock()
        if helper:
            self.files[PRIV] = ""
        if bench:
            self.files["/etc/wk-image"] = "id=x\n"
        self.react(("sudo", "-n", PRIV), self.helper)
        self.answer(("sudo", "-n", "true"))
        self.react(PAUSE, lambda a, f: self.put("/daemons", "stopped"))
        self.react(RESUME, lambda a, f: self.put("/daemons", "running"))
        self.react(USER, lambda a, f: self.put("/desktop", "quiet"))
        self.react(RAISER_ON, self.raiser_on)
        self.react(RAISER_OFF, self.raiser_off)
        self.answer(lib(quiet.DESKTOP, "wk_quiet_desktop_probe"), out="appnap=1\n")
        self.answer(lib(quiet.DESKTOP, "wk_quiet_cpu_findings"), out="ok\ton AC\t\n")
        self.answer(lib(quiet.DESKTOP, "wk_quiet_desktop_findings"), out="ok\tApp Nap is off\t\n")
        self.answer(lib(quiet.DESKTOP, "wk_quiet_daemons_findings"), out="")
        self.answer(RENDER)
        self.answer(TMUTIL, 1, "", "No destinations configured\n")
        self.answer(UPDATES, out="0\n")
        self.answer(HOSTS)
        self.answer(("defaults", "read", "org.webkit.MiniBrowser", "NSAppSleepDisabled"), out="1\n")

    def put(self, path, text):
        self.files[path] = text
        return Result(0)

    def helper(self, argv, fake):
        verb = argv[3]
        if verb == "status":
            return Result(0, "governor:  performance\n")
        self.files["/priv"] = verb
        return Result(0, "%s done\n" % verb)

    def raiser_on(self, argv, fake):
        if STATE + "/caffeinate.pid" not in self.files:
            self._set_file(STATE + "/caffeinate.pid", "4242\n")
            self.pids.add(4242)
        return Result(0)

    def raiser_off(self, argv, fake):
        self.files.pop(STATE + "/caffeinate.pid", None)
        self.pids.discard(4242)
        return Result(0)

    def q(self):
        return quiet.Quiesce(ROOT, self, self.clock, self.env, macos=self.macos)

    def state(self):
        return dict(self.files), sorted(d for d in self.dirs if d.startswith(STATE))

    def ran(self, argv):
        return any(e[0] == "run" and e[1] == tuple(argv) for e in self.effects)


class Recording(World):
    """Every act_run as ("act", argv) in both modes, so a dry run's plan can be held against a wet run's."""

    def act_run(self, argv, **kw):
        self.effects.append(("act", tuple(argv)))
        if act.dry_run():
            return Result(0)
        return super().act_run(argv, **kw)

    def mutations(self):
        return [e for e in self.effects if e[0] in ("act", "write", "mkdir", "remove")]


class QuiesceTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {}, clear=False)
        p.start()
        self.addCleanup(p.stop)
        for v in ("WK_DRY_RUN", "WK_QUIET", "WK_DESTRUCTIVE", "WK_CONFIRMED", "WK_SETTLE_SECONDS"):
            os.environ.pop(v, None)

    def quiet_run(self, fn):
        with contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()) as out:
            rc = fn()
        return rc, out.getvalue() + err.getvalue()

    def refused(self, fn):
        with self.assertRaises(Refused):
            with contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
                fn()
        return err.getvalue()


class TestOn(QuiesceTest):
    def test_a_mac_is_quieted_by_both_halves_and_settles(self):
        w = World()
        rc, out = self.quiet_run(w.q().on)
        self.assertEqual(0, rc, out)
        self.assertEqual("on", w.files["/priv"])
        self.assertEqual("stopped", w.files["/daemons"])
        self.assertIn(STATE + "/daemons_paused", w.files)
        self.assertIn(STATE + "/caffeinate.pid", w.files)
        self.assertEqual([30], w.clock.slept)
        self.assertIn("quiesced; settle time elapsed", out)

    def test_the_settle_time_is_the_callers(self):
        w = World()
        w.env["WK_SETTLE_SECONDS"] = "3"
        self.quiet_run(w.q().on)
        self.assertEqual([3], w.clock.slept)

    def test_the_user_half_is_written_only_on_a_bench_install(self):
        """A first boot's write to a protected domain does not survive the session starting, so quiesce
        writes it again in the session -- and only in bench mode, since a workstation's accessibility
        settings are not this command's to rewrite."""
        bench, desk = World(bench=True), World()
        self.quiet_run(bench.q().on)
        self.quiet_run(desk.q().on)
        self.assertEqual("quiet", bench.files.get("/desktop"))
        self.assertNotIn("/desktop", desk.files)

    def test_a_pause_that_was_refused_leaves_no_flag_and_says_what_it_needs(self):
        w = World()
        w.answer(PAUSE, 1)
        rc, out = self.quiet_run(w.q().on)
        self.assertEqual(0, rc)
        self.assertNotIn(STATE + "/daemons_paused", w.files)
        self.assertIn("needs passwordless root", out)

    def test_linux_is_the_privileged_half_and_the_settle_alone(self):
        w = World(macos=False)
        rc, out = self.quiet_run(w.q().on)
        self.assertEqual("on", w.files["/priv"])
        self.assertFalse(w.ran(PAUSE) or w.ran(RAISER_ON) or w.ran(TMUTIL))
        self.assertEqual([30], w.clock.slept)

    def test_an_interrupted_on_undoes_itself(self):
        w = World()

        def interrupted(argv, fake):
            fake.files["/daemons"] = "stopped"
            fake._set_file(STATE + "/daemons_paused", "")
            raise KeyboardInterrupt
        w.react(PAUSE, interrupted)
        with self.assertRaises(KeyboardInterrupt):
            self.quiet_run(w.q().on)
        self.assertEqual("off", w.files["/priv"])
        self.assertEqual("running", w.files["/daemons"])
        self.assertNotIn(STATE + "/daemons_paused", w.files)

    def test_quiesce_keeps_no_list_of_its_own(self):
        """A second list is a daemon that gets paused and never resumed: the pause and the resume are the
        table's own functions, and nothing here signals a process itself."""
        w = World()
        self.quiet_run(w.q().on)
        self.quiet_run(w.q().off)
        self.assertTrue(w.ran(PAUSE) and w.ran(RESUME))
        self.assertEqual([], [e for e in w.effects if e[0] == "kill"])
        text = (REPO / "lib" / "wk" / "quiet.py").read_text()
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotIn("launchctl", code)


class TestOff(QuiesceTest):
    def test_off_undoes_what_on_did(self):
        w = World()
        self.quiet_run(w.q().on)
        rc, out = self.quiet_run(w.q().off)
        self.assertEqual(0, rc, out)
        self.assertEqual("off", w.files["/priv"])
        self.assertEqual("running", w.files["/daemons"])
        self.assertNotIn(STATE + "/daemons_paused", w.files)
        self.assertNotIn(STATE + "/caffeinate.pid", w.files)

    def test_off_with_nothing_paused_resumes_nothing(self):
        w = World()
        self.quiet_run(w.q().off)
        self.assertFalse(w.ran(RESUME) or w.ran(RAISER_OFF))
        self.assertEqual("off", w.files["/priv"])

    def test_a_resume_that_failed_says_a_reboot_does_it(self):
        w = World()
        self.quiet_run(w.q().on)
        w.answer(RESUME, 1)
        rc, out = self.quiet_run(w.q().off)
        self.assertIn("a reboot does it", out)
        self.assertNotIn(STATE + "/daemons_paused", w.files)


class TestThePrivilegedHalf(QuiesceTest):
    def test_no_helper_is_refused_naming_the_setup_stage(self):
        err = self.refused(World(helper=False).q().on)
        self.assertIn("is not installed", err)
        self.assertIn("./setup --stage quiesce", err)

    def test_a_grant_that_wants_a_password_is_named_as_the_grant(self):
        w = World()
        w.answer(("sudo", "-n", PRIV), 1, "", "sudo: a password is required\n")
        w.answer(("sudo", "-n", "true"), 1)
        self.assertIn("passwordless sudo is not set up", self.refused(w.q().on))

    def test_a_helper_that_failed_under_a_working_grant_is_named_as_the_helper(self):
        """Reporting the grant for both sent an operator to ./setup for a helper killed mid-verb."""
        w = World()
        w.answer(("sudo", "-n", PRIV), 1)
        err = self.refused(w.q().off)
        self.assertIn("this is the helper and not the grant", err)


class TestStatus(QuiesceTest):
    def status(self, w):
        rc, out = self.quiet_run(w.q().status)
        self.assertEqual(0, rc, out)
        return out

    def test_it_changes_nothing(self):
        w = World()
        before = w.state()
        self.status(w)
        self.assertEqual(before, w.state())
        self.assertEqual([], [e for e in w.effects if e[0] not in ("run", "run_tty")])

    def test_a_dead_pid_file_is_not_running(self):
        w = World()
        w._set_file(STATE + "/caffeinate.pid", "999\n")
        w._set_file(STATE + "/raiser.pid", "998\n")
        out = self.status(w)
        self.assertIn("caffeinate: no", out)
        self.assertIn("raiser:     no", out)

    def test_a_live_pid_is_running(self):
        w = World()
        self.quiet_run(w.q().on)
        out = self.status(w)
        self.assertIn("caffeinate: running", out)
        self.assertIn("'quiesce on' paused them", out)

    def test_no_pid_file_is_not_running(self):
        self.assertIn("caffeinate: no", self.status(World()))

    def test_the_helpers_own_account_is_shown(self):
        out = self.status(World())
        self.assertIn("the privileged half's own account:", out)
        self.assertIn("    governor:  performance", out)

    def test_a_missing_helper_is_its_account(self):
        self.assertIn("is not installed", self.status(World(helper=False)))

    def test_an_older_wk_tools_state_is_named_and_not_read(self):
        """One state directory: a directory an older wk-tools left at $TMPDIR/wk-quiesce is not read,
        undone or migrated, but named, so a person can remove it."""
        w = World()
        w._set_file("/tmp/wk-quiesce/caffeinate.pid", "4242\n")
        w.pids.add(4242)
        out = self.status(w)
        self.assertIn("stale: /tmp/wk-quiesce", out)
        self.assertIn("caffeinate: no", out)

    def test_no_stale_directory_names_nothing(self):
        self.assertNotIn("stale", self.status(World()))

    def test_app_nap_is_read_from_the_browsers_domain(self):
        w = World()
        self.assertIn("disabled for MiniBrowser", self.status(w))
        w.answer(("defaults", "read", "org.webkit.MiniBrowser", "NSAppSleepDisabled"), 1)
        self.assertIn("app nap:    default", self.status(w))

    def test_linux_reports_no_mac_half(self):
        out = self.status(World(macos=False))
        self.assertNotIn("raiser", out)
        self.assertNotIn("timemachine", out)

    def test_the_state_directory_is_the_one_status_reads(self):
        w = World()
        w.env = {"HOME": "/home/u"}
        self.assertEqual("/home/u/.local/state/wk/quiesce", w.q().state)


class TestTheReadings(QuiesceTest):
    """`Quiesce.noise` runs in every leg's preflight (wk bench staged) and inside `wk quiesce on`, both after
    the daemons are paused, so a reading of one held stopped must be bounded and must refuse no leg."""

    def noise(self, w):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            bad = w.q().noise()
        return bad, err.getvalue()

    def test_a_machine_that_answers_is_judged(self):
        bad, out = self.noise(World())
        self.assertEqual(0, bad, out)
        self.assertIn("no destination configured", out)
        self.assertIn("automatic checking off", out)

    def test_a_reading_that_times_out_is_unknown_names_the_daemon_and_refuses_nothing(self):
        w = World()
        w.answer(TMUTIL, TIMED_OUT)
        w.answer(UPDATES, TIMED_OUT)
        bad, out = self.noise(w)
        self.assertEqual(0, bad, out)
        self.assertIn("backupd did not answer inside its bound", out)
        self.assertIn("softwareupdated did not answer inside its bound", out)
        for line in out.splitlines():
            if "did not answer inside its bound" in line:
                self.assertIn("unknown", line)

    def test_both_readings_are_bounded(self):
        calls = []
        w = World()
        real = w.run

        def run(argv, input=None, timeout=None):
            calls.append((tuple(argv), timeout))
            return real(argv, input=input, timeout=timeout)
        w.run = run
        self.noise(w)
        self.assertIn((TMUTIL, quiet.READ_SECS), calls)
        self.assertIn((UPDATES, quiet.READ_SECS), calls)

    def test_a_backup_destination_and_automatic_checks_are_faults(self):
        w = World()
        w.answer(TMUTIL, 0, "Name: Backups\n")
        w.answer(UPDATES, out="1\n")
        bad, out = self.noise(w)
        self.assertEqual(2, bad, out)

    def test_the_findings_are_drawn_by_the_one_renderer_and_counted(self):
        w = World()
        w.answer(RENDER, 2)
        bad, out = self.noise(w)
        self.assertEqual(2, bad, out)
        drawn = [e[1] for e in w.effects if e[0] == "run_tty"]
        self.assertEqual([RENDER + ("ok\ton AC\t\n",)], drawn)

    def test_the_renderer_colours_only_a_terminal(self):
        """Drawn on the real stderr, so colour follows what reads it."""
        argv = quiet.lib_argv(ROOT, quiet.COMMON, quiet.RENDER, "wrong\tbad\tfix\n")
        cp = subprocess.run(argv, capture_output=True, text=True)
        self.assertEqual(1, cp.returncode, cp.stderr)
        self.assertIn("  --    bad", cp.stderr)
        self.assertNotIn("\033[", cp.stderr)
        pid, fd = pty.fork()
        if pid == 0:
            os.dup2(1, 2)
            os.execvp(argv[0], argv)
        out = b""
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        os.waitpid(pid, 0)
        self.assertIn(b"\033[31m--", out)

    def test_a_workstation_is_judged_on_its_clock_alone(self):
        """The rest is what a bench install is and a workstation never will be; a red line nothing there
        can clear teaches a reader to skip the list."""
        w = World()
        self.noise(w)
        asked = [e[1] for e in w.effects if e[0] == "run"]
        self.assertFalse([a for a in asked if "wk_quiet_desktop_findings" in " ".join(a)])
        self.assertNotIn(HOSTS, asked)

    def test_a_bench_install_is_judged_on_everything_including_the_hosts_denial(self):
        w = World(bench=True)
        bad, out = self.noise(w)
        self.assertEqual(0, bad, out)
        self.assertIn("update endpoints denied", out)
        w.answer(HOSTS, 1)
        bad, out = self.noise(w)
        self.assertEqual(1, bad, out)
        self.assertIn("NOT denied", out)


class TestCrashOnly(QuiesceTest):
    def test_on_and_off_killed_after_any_effect_and_rerun_converge(self):
        """`killpoints[quiesce]`: `on`, then `off`, each killed after every effect in turn and re-run."""
        for action, quiesced in (("on", False), ("off", True)):
            with self.subTest(action=action):
                def world():
                    w = World(bench=True)
                    if quiesced:
                        self.quiet_run(w.q().on)
                    w.effects, w.applied = [], 0
                    return w

                def run_once(w):
                    self.quiet_run(getattr(w.q(), action))
                converges(self, lambda: types.SimpleNamespace(fake=world()), lambda w: run_once(w.fake),
                          lambda w: w.fake.state())

    def test_a_dry_run_is_the_wet_runs_plan_and_touches_nothing(self):
        for action, quiesced in (("on", False), ("off", True)):
            with self.subTest(action=action):
                def world():
                    w = Recording(bench=True)
                    if quiesced:
                        self.quiet_run(w.q().on)
                    w.effects = []
                    return w
                wet = world()
                self.quiet_run(getattr(wet.q(), action))
                dry = world()
                before, slept = dry.state(), list(dry.clock.slept)
                os.environ["WK_DRY_RUN"] = "1"
                try:
                    self.quiet_run(getattr(dry.q(), action))
                finally:
                    del os.environ["WK_DRY_RUN"]
                self.assertEqual(wet.mutations(), dry.mutations())
                self.assertGreaterEqual(len(dry.mutations()), 3)
                self.assertEqual(before, dry.state())
                self.assertEqual(slept, dry.clock.slept)


class TestTheCommand(QuiesceTest):
    def load(self):
        path = str(REPO / "cmd" / "quiesce")
        loader = importlib.machinery.SourceFileLoader("wk_cmd_quiesce", path)
        spec = importlib.util.spec_from_file_location("wk_cmd_quiesce", path, loader=loader)
        m = importlib.util.module_from_spec(spec)
        loader.exec_module(m)
        return m

    def test_bare_is_status_and_anything_else_is_refused(self):
        m = self.load()
        self.assertEqual("status", m.parse([]))
        self.assertEqual("on", m.parse(["on"]))
        for bad in (["up"], ["on", "off"]):
            with self.subTest(argv=bad):
                self.assertIn("usage: wk quiesce", self.refused(lambda: m.parse(bad)))


class APrivilegedVerbNeverBlocksOnAStoppedDaemon(unittest.TestCase):
    """A stopped daemon answers no XPC request: `tmutil stopbackup` against one hung `wk quiesce on`,
    and every later step of a benchmark boot, for 37 minutes (bench install, 2026-09-09). macOS ships
    no `timeout`, so the bound is in the helper."""

    PRIV = REPO / "admin" / "wk-quiesce-priv"

    def _bounded(self, script, timeout=30):
        body = func_body(self.PRIV.read_text(), "bounded")
        return bash("set -euo pipefail\nbounded() {" + body + "}\n" + script, timeout=timeout)

    def test_every_daemon_asking_verb_is_bounded(self):
        text = self.PRIV.read_text()
        for verb in ("tmutil stopbackup", "softwareupdate --schedule off", "softwareupdate --schedule on",
                     "mdutil -a -i off", "mdutil -a -i on"):
            with self.subTest(verb=verb):
                self.assertRegex(text, r"bounded \d+ " + verb)

    def test_a_call_that_never_returns_is_killed_and_reported(self):
        cp = self._bounded('bounded 2 sleep 600\nprintf "WENT ON rc=%s\\n" "$?"\n')
        out = cp.stdout + cp.stderr
        self.assertIn("WENT ON rc=0", out, out)
        self.assertIn("did not answer in 2s", out, out)

    def test_a_call_that_answers_is_not_waited_out(self):
        cp = self._bounded('bounded 30 true\nprintf "WENT ON\\n"\n', timeout=20)
        out = cp.stdout + cp.stderr
        self.assertIn("WENT ON", out, out)
        self.assertNotIn("did not answer", out)

    def test_the_bound_takes_no_argument_from_argv(self):
        """The grant is bounded by shape: every command it runs is a literal in the file."""
        for line in self.PRIV.read_text().splitlines():
            if line.strip().startswith("bounded "):
                with self.subTest(line=line.strip()):
                    self.assertNotIn("$", line, "a bounded call takes a variable")


def _ssh(dest, command, input=None, timeout=300):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", dest, command], input=input,
                          capture_output=True, text=True, timeout=timeout)


def _tools(name):
    return fleet.Fleet(REPO).load(name).get("WK_REMOTE_TOOLS") or "Development/wk-tools"


class TestOnRealMachines(WkTest):
    """Read-only: `requires_machine` never mutates a machine, so these read what `status` and the probe
    say. `on`/`off` against a real machine is the owed half of `quiesce.readback`."""

    def _readback(self, name):
        cp = _ssh(name, "cd %s && ./wk quiesce status" % _tools(name))
        out = cp.stdout + cp.stderr
        self.assertEqual(0, cp.returncode, out)
        self.assertIn("the privileged half's own account:", out)
        self.assertIn("measured here, now:", out)
        return out

    @requires_machine("moose")
    def test_readback_moose(self):
        """`live quiesce.readback[moose]`"""
        self.assertIn("governor:", self._readback("moose"))

    @requires_machine("tolken")
    def test_readback_tolken(self):
        """`live quiesce.readback[tolken]`"""
        self.assertIn("app nap:", self._readback("tolken"))

    @requires_machine("tolken-bench")
    def test_classified_mbp(self):
        """`live quiesce.classified[mbp]`: every row of the table is answered on the bench install, as
        running, stopped or absent, and no reading wedged on a stopped daemon."""
        script = bash('. "$WK_ROOT/bench/mac-quiet-desktop.sh"; wk_quiet_desktop_script').stdout
        cp = _ssh("tolken-bench", "bash -s", input=script + "\nwk_quiet_desktop_probe\n")
        self.assertEqual(0, cp.returncode, cp.stderr)
        probe = dict(line.split("=", 1) for line in cp.stdout.splitlines() if "=" in line)
        rows = bash('. "$WK_ROOT/bench/mac-quiet-desktop.sh"; wk_quiet_desktop_stopped; '
                    'wk_quiet_desktop_expected').stdout.split("\n")
        for name in (r.split()[0] for r in rows if r.split()):
            with self.subTest(row=name):
                self.assertIn(probe.get(name), ("running", "stopped", "absent"))
        self.assertNotIn("!timeout", cp.stdout)


if __name__ == "__main__":
    unittest.main()
