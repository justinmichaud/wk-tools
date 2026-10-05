"""`wk quiesce`: lib/wk/quiet.py against a fake Mac, the privileged helper's shell, and live readbacks."""

import contextlib
import io
import os
import pty
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.killpoints import converges
from tests.support import REPO, WkTest, as_dispatched, bash, func_body, load_cmd, requires_machine, run

sys.path.insert(0, str(REPO / "lib"))
from wk import act, decl, fleet, quiet  # noqa: E402
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
        self.react(RAISER_ON, lambda a, f: self.caffeinate(True))
        self.react(RAISER_OFF, lambda a, f: self.caffeinate(False))
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
        if argv[3] == "status":
            return Result(0, "governor:  performance\n")
        return self.put("/priv", argv[3])

    def caffeinate(self, on):
        if on:
            self._set_file(STATE + "/caffeinate.pid", "4242\n")
            self.pids.add(4242)
        else:
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

    def quiesced(self, cls=World, **kw):
        w = cls(**kw)
        self.quiet_run(w.q().on)
        w.effects, w.applied = [], 0
        return w


class TestOn(QuiesceTest):
    def test_a_mac_is_quieted_by_both_halves_and_settles_for_the_callers_time(self):
        w = World()
        rc, out = self.quiet_run(w.q().on)
        self.assertEqual(0, rc, out)
        self.assertEqual(("on", "stopped"), (w.files["/priv"], w.files["/daemons"]))
        self.assertIn(STATE + "/daemons_paused", w.files)
        self.assertIn(STATE + "/caffeinate.pid", w.files)
        self.assertEqual([30], w.clock.slept)
        self.assertIn("quiesced; settle time elapsed", out)
        w = World()
        w.env["WK_SETTLE_SECONDS"] = "3"
        self.quiet_run(w.q().on)
        self.assertEqual([3], w.clock.slept)

    def test_the_user_half_is_written_only_on_a_bench_install_and_linux_is_the_privileged_half_alone(self):
        bench, desk, linux = World(bench=True), World(), World(macos=False)
        for w in (bench, desk, linux):
            self.quiet_run(w.q().on)
        self.assertEqual("quiet", bench.files.get("/desktop"))
        self.assertNotIn("/desktop", desk.files)
        self.assertEqual("on", linux.files["/priv"])
        self.assertFalse(linux.ran(PAUSE) or linux.ran(RAISER_ON) or linux.ran(TMUTIL))
        self.assertEqual([30], linux.clock.slept)

    def test_a_pause_that_was_refused_leaves_no_flag_and_says_what_it_needs(self):
        w = World()
        w.answer(PAUSE, 1)
        rc, out = self.quiet_run(w.q().on)
        self.assertEqual(0, rc)
        self.assertNotIn(STATE + "/daemons_paused", w.files)
        self.assertIn("needs passwordless root", out)

    def test_an_interrupted_on_undoes_itself(self):
        w = World()

        def interrupted(argv, fake):
            fake.files["/daemons"] = "stopped"
            fake._set_file(STATE + "/daemons_paused", "")
            raise KeyboardInterrupt
        w.react(PAUSE, interrupted)
        with self.assertRaises(KeyboardInterrupt):
            self.quiet_run(w.q().on)
        self.assertEqual(("off", "running"), (w.files["/priv"], w.files["/daemons"]))
        self.assertNotIn(STATE + "/daemons_paused", w.files)


class TestOff(QuiesceTest):
    def test_off_undoes_what_on_did(self):
        w = self.quiesced()
        rc, out = self.quiet_run(w.q().off)
        self.assertEqual(0, rc, out)
        self.assertEqual(("off", "running"), (w.files["/priv"], w.files["/daemons"]))
        self.assertNotIn(STATE + "/daemons_paused", w.files)
        self.assertNotIn(STATE + "/caffeinate.pid", w.files)
        self.assertEqual([], [e for e in w.effects if e[0] == "kill"], "quiesce keeps no list of its own to kill")

    def test_off_with_nothing_paused_resumes_nothing(self):
        w = World()
        self.quiet_run(w.q().off)
        self.assertFalse(w.ran(RESUME) or w.ran(RAISER_OFF))
        self.assertEqual("off", w.files["/priv"])

    def test_a_resume_that_failed_says_a_reboot_does_it(self):
        w = self.quiesced()
        w.answer(RESUME, 1)
        self.assertIn("a reboot does it", self.quiet_run(w.q().off)[1])
        self.assertNotIn(STATE + "/daemons_paused", w.files)


class TestThePrivilegedHalf(QuiesceTest):
    def test_each_failure_is_named_by_which_half_failed(self):
        no_helper = World(helper=False)
        password = World()
        password.answer(("sudo", "-n", PRIV), 1, "", "sudo: a password is required\n")
        password.answer(("sudo", "-n", "true"), 1)
        failed = World()
        failed.answer(("sudo", "-n", PRIV), 1)
        for w, verb, words in ((no_helper, "on", ("is not installed", "./setup --stage quiesce")),
                               (password, "on", ("passwordless sudo is not set up",)),
                               (failed, "off", ("this is the helper and not the grant",))):
            with self.subTest(words=words[0]):
                err = self.refused(getattr(w.q(), verb))
                for word in words:
                    self.assertIn(word, err)


class TestStatus(QuiesceTest):
    def status(self, w):
        rc, out = self.quiet_run(w.q().status)
        self.assertEqual(0, rc, out)
        return out

    def test_it_changes_nothing_and_shows_the_helpers_own_account(self):
        w = World()
        before = w.state()
        out = self.status(w)
        self.assertEqual(before, w.state())
        self.assertEqual([], [e for e in w.effects if e[0] not in ("run", "run_tty")])
        self.assertIn("the privileged half's own account:", out)
        self.assertIn("    governor:  performance", out)
        self.assertIn("is not installed", self.status(World(helper=False)))

    def test_a_pid_is_running_only_while_it_lives(self):
        w = World()
        self.assertIn("caffeinate: no", self.status(w))
        w._set_file(STATE + "/caffeinate.pid", "999\n")
        w._set_file(STATE + "/raiser.pid", "998\n")
        out = self.status(w)
        self.assertIn("caffeinate: no", out)
        self.assertIn("raiser:     no", out)
        out = self.status(self.quiesced())
        self.assertIn("caffeinate: running", out)
        self.assertIn("'quiesce on' paused them", out)

    def test_app_nap_is_read_from_the_browsers_domain_and_linux_reports_no_mac_half(self):
        w = World()
        self.assertIn("disabled for MiniBrowser", self.status(w))
        w.answer(("defaults", "read", "org.webkit.MiniBrowser", "NSAppSleepDisabled"), 1)
        self.assertIn("app nap:    default", self.status(w))
        out = self.status(World(macos=False))
        self.assertNotIn("raiser", out)
        self.assertNotIn("timemachine", out)

    def test_the_state_directory_is_the_one_status_reads(self):
        w = World()
        w.env = {"HOME": "/home/u"}
        self.assertEqual("/home/u/.local/state/wk/quiesce", w.q().state)


class TestTheReadings(QuiesceTest):
    def noise(self, w):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            bad = w.q().noise()
        return bad, err.getvalue()

    def test_each_reading_is_bounded_and_judged(self):
        calls = []
        w = World()
        real = w.run
        w.run = lambda argv, input=None, timeout=None: calls.append((tuple(argv), timeout)) or real(
            argv, input=input, timeout=timeout)
        bad, out = self.noise(w)
        self.assertEqual(0, bad, out)
        self.assertIn("no destination configured", out)
        self.assertIn("automatic checking off", out)
        self.assertIn((TMUTIL, quiet.READ_SECS), calls)
        self.assertIn((UPDATES, quiet.READ_SECS), calls)
        w = World()
        w.answer(TMUTIL, 0, "Name: Backups\n")
        w.answer(UPDATES, out="1\n")
        self.assertEqual(2, self.noise(w)[0])

    def test_a_reading_that_times_out_is_unknown_names_the_daemon_and_refuses_nothing(self):
        w = World()
        w.answer(TMUTIL, TIMED_OUT)
        w.answer(UPDATES, TIMED_OUT)
        bad, out = self.noise(w)
        self.assertEqual(0, bad, out)
        timed = [l for l in out.splitlines() if "did not answer inside its bound" in l]
        self.assertEqual(2, len(timed), out)
        self.assertTrue(any("backupd" in l for l in timed) and any("softwareupdated" in l for l in timed), out)
        self.assertTrue(all("unknown" in l for l in timed), out)

    def test_the_findings_are_drawn_by_the_one_renderer_and_counted(self):
        w = World()
        w.answer(RENDER, 2)
        bad, out = self.noise(w)
        self.assertEqual(2, bad, out)
        self.assertEqual([RENDER + ("ok\ton AC\t\n",)], [e[1] for e in w.effects if e[0] == "run_tty"])

    def test_the_renderer_colours_only_a_terminal(self):
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
        with contextlib.suppress(OSError):
            while chunk := os.read(fd, 4096):
                out += chunk
        os.waitpid(pid, 0)
        self.assertIn(b"\033[31m--", out)

    def test_a_workstation_is_judged_on_its_clock_alone_and_a_bench_install_on_everything(self):
        w = World()
        self.noise(w)
        asked = [e[1] for e in w.effects if e[0] == "run"]
        self.assertFalse([a for a in asked if "wk_quiet_desktop_findings" in " ".join(a)])
        self.assertNotIn(HOSTS, asked)
        w = World(bench=True)
        bad, out = self.noise(w)
        self.assertEqual(0, bad, out)
        self.assertIn("update endpoints denied", out)
        w.answer(HOSTS, 1)
        bad, out = self.noise(w)
        self.assertEqual(1, bad, out)
        self.assertIn("NOT denied", out)


class TestCrashOnly(QuiesceTest):
    def world(self, action, cls):
        return self.quiesced(cls, bench=True) if action == "off" else cls(bench=True)

    def test_on_and_off_killed_after_any_effect_and_rerun_converge(self):
        """`killpoints[quiesce]`: `on`, then `off`, each killed after every effect in turn and re-run."""
        for action in ("on", "off"):
            with self.subTest(action=action):
                converges(self, lambda: types.SimpleNamespace(fake=self.world(action, World)),
                          lambda w: self.quiet_run(getattr(w.fake.q(), action)), lambda w: w.fake.state())

    def test_a_dry_run_is_the_wet_runs_plan_and_touches_nothing(self):
        for action in ("on", "off"):
            with self.subTest(action=action):
                wet, dry = self.world(action, Recording), self.world(action, Recording)
                self.quiet_run(getattr(wet.q(), action))
                before, slept = dry.state(), list(dry.clock.slept)
                with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
                    self.quiet_run(getattr(dry.q(), action))
                self.assertEqual(wet.mutations(), dry.mutations())
                self.assertGreaterEqual(len(dry.mutations()), 3)
                self.assertEqual((before, slept), (dry.state(), dry.clock.slept))


class TestTheCommand(QuiesceTest):
    def test_bare_is_status_and_anything_else_is_refused(self):
        m = load_cmd("quiesce")
        with mock.patch.object(m.quiet, "Quiesce") as q, mock.patch.object(m.act, "terminate_as_interrupt"):
            m.main(as_dispatched("quiesce", [], {}))
            m.main(["on"])
        self.assertEqual(["status", "on"], [c[0] for c in q.return_value.method_calls])
        for bad in (["up"], ["on", "off"]):
            with self.subTest(argv=bad):
                cp = run("quiesce", *bad)
                self.assertEqual(2, cp.returncode, cp.stdout)
                self.assertIn("usage: wk quiesce", cp.stdout)

    def test_a_session_change_needs_the_helper_and_its_status_only_reads(self):
        d = decl.Decl(REPO / "cmd" / "quiesce")
        for verb in ("on", "gdm", "off"):
            self.assertEqual("quiesce-helper", d.needs_for(["session", verb]), verb)
            self.assertFalse(d.is_readonly(["session", verb]), verb)
        self.assertEqual("", d.needs_for(["session", "status"]))
        self.assertTrue(d.is_readonly(["session", "status"]))


PRIV_SCRIPT = REPO / "admin" / "wk-quiesce-priv"


class APrivilegedVerbNeverBlocksOnAStoppedDaemon(unittest.TestCase):
    def test_a_call_that_never_returns_is_killed_and_reported_and_one_that_answers_is_not_waited_out(self):
        body = func_body(PRIV_SCRIPT.read_text(), "bounded")
        for call, went_on, waited in (("bounded 2 sleep 600", "WENT ON rc=0", True), ("bounded 30 true", "WENT ON", False)):
            with self.subTest(call=call):
                cp = bash('set -euo pipefail\nbounded() {%s}\n%s\nprintf "WENT ON rc=%%s\\n" "$?"\n' % (body, call))
                out = cp.stdout + cp.stderr
                self.assertIn(went_on, out, out)
                self.assertEqual(waited, "did not answer in 2s" in out, out)

    def test_the_bound_takes_no_argument_from_argv(self):
        """The grant is bounded by shape: every command it runs is a literal in the file."""
        for line in PRIV_SCRIPT.read_text().splitlines():
            if line.strip().startswith("bounded "):
                with self.subTest(line=line.strip()):
                    self.assertNotIn("$", line, "a bounded call takes a variable")


class ALinuxQuiesceIsReadBack(unittest.TestCase):
    FUNCS = ("put_sys", "put_sysctl", "can_boost", "tune", "linux_on", "linux_off")
    FILES = ("devices/system/cpu/cpu0/cpufreq/scaling_governor", "devices/system/cpu/cpu1/cpufreq/scaling_governor",
             "devices/system/cpu/intel_pstate/no_turbo", "devices/system/cpu/cpufreq/boost")
    GOV0, GOV1, NO_TURBO, BOOST = FILES
    STUB = ('sysctl() {\n    case "$1" in\n        -q) [ "$2" = -w ] || return 2; local key="${3%%=*}" val="${3#*=}"\n'
            '            %s printf "%%s\\n" "$val" > "$STORE/$key" ;;\n        -n) cat "$STORE/$2" ;;\n    esac\n}\n'
            'systemctl() { :; }\n')

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-quiesce-"))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.sys = self.tmp / "sys"
        for f in self.FILES:
            self.put(f, "schedutil" if f.endswith("governor") else "0")
        (self.tmp / "sysctl").mkdir()

    def put(self, rel, value):
        (self.sys / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.sys / rel).write_text("%s\n" % value)

    def _cppc(self, highest, nominal):
        for cpu in ("cpu0", "cpu1"):
            self.put("devices/system/cpu/%s/cpufreq/scaling_driver" % cpu, "cppc_cpufreq")
            self.put("devices/system/cpu/%s/acpi_cppc/highest_perf" % cpu, highest)
            self.put("devices/system/cpu/%s/acpi_cppc/nominal_perf" % cpu, nominal)

    def helper(self, verb, sysctl_sticks=True, rc=0):
        text = PRIV_SCRIPT.read_text()
        lifted = "".join("%s() {%s}\n" % (f, func_body(text, f)) for f in self.FUNCS)
        stub = self.STUB % ("" if sysctl_sticks else "val=2;")
        cp = bash("set -euo pipefail\nSYS=%s\nSTORE=%s\n%s%s%s\n" % (self.sys, self.tmp / "sysctl", stub, lifted, verb))
        self.assertEqual(rc, cp.returncode, cp.stdout + cp.stderr)
        return cp

    def read(self, rel):
        return (self.sys / rel).read_text().strip()

    def test_on_sets_every_knob_and_off_restores_them(self):
        cp = self.helper("linux_on")
        self.assertIn("quiesced: performance governor", cp.stdout)
        self.assertEqual(("performance", "1", "0"), (self.read(self.GOV1), self.read(self.NO_TURBO), self.read(self.BOOST)))
        self.assertNotIn("boost left alone", cp.stdout)
        self.assertEqual("0", (self.tmp / "sysctl" / "kernel.randomize_va_space").read_text().strip())
        self.assertIn("restored:", self.helper("linux_off").stdout)
        self.assertEqual(("schedutil", "1"), (self.read(self.GOV0), self.read(self.BOOST)))

    @unittest.skipIf(os.geteuid() == 0, "root writes a read-only file")
    def test_a_refused_sysfs_write_fails_naming_the_file(self):
        gov = self.sys / self.GOV1
        gov.chmod(0o444)
        self.addCleanup(gov.chmod, 0o644)
        for verb, claim, boost in (("linux_on", "quiesced", "0"), ("linux_off", "restored", "1")):
            with self.subTest(verb=verb):
                cp = self.helper(verb, rc=1)
                self.assertIn(str(gov), cp.stderr)
                self.assertNotIn(claim, cp.stdout)
                self.assertEqual(boost, self.read(self.BOOST), "a failed step skips the steps after it")

    def test_a_sysctl_that_reads_back_otherwise_fails_naming_it(self):
        cp = self.helper("linux_on", sysctl_sticks=False, rc=1)
        self.assertIn("kernel.perf_event_paranoid", cp.stderr)
        self.assertNotIn("quiesced", cp.stdout)

    def test_boost_is_left_alone_only_on_a_platform_with_no_headroom(self):
        self._cppc(highest=300, nominal=300)
        for verb, claim in (("linux_on", "quiesced"), ("linux_off", "restored")):
            with self.subTest(verb=verb):
                cp = self.helper(verb)
                self.assertIn(claim, cp.stdout)
                self.assertIn("boost left alone", cp.stdout)
                self.assertIn("the platform has none", cp.stdout)
                self.assertEqual("0", self.read(self.BOOST), "boost was never written")
        self._cppc(highest=380, nominal=300)
        self.assertNotIn("boost left alone", self.helper("linux_off").stdout)
        self.assertEqual("1", self.read(self.BOOST))


def _ssh(dest, command, input=None, timeout=300):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", dest, command], input=input,
                          capture_output=True, text=True, timeout=timeout)


def _tools(name):
    return fleet.Fleet(REPO).load(name).get("tools") or "Development/wk-tools"


class TestOnRealMachines(WkTest):
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
