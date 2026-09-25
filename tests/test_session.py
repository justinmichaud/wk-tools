"""`wk session`: lib/wk/session.py's on/gdm/off/status against a fake Linux machine -- a GPU and the
BMC's `ast` chip under /sys/class/drm, loginctl's sessions, systemctl's units and a privileged helper
whose session verbs move the socket and the mode the way the real one does. Nothing here runs the real
helper, systemctl, loginctl or gdm. Also `killpoints[session]`, a dry run printing the wet run's plan,
and the live row `session.modes[moose]`.

Run: python3 tests/run.py -k tests.test_session
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import subprocess
import sys
import types
import unittest
from unittest import mock

from tests.killpoints import converges
from tests.support import REPO, WkTest, requires_machine

sys.path.insert(0, str(REPO / "lib"))
from wk import act, fleet, quiet, session  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402

ROOT = str(REPO)
PRIV = quiet.PRIV
RUN = "/run/user/1000"
SOCKET = RUN + "/wk/display/wayland-0"
DRM = session.SYS_DRM
MODE_FILE = "/run/wk-session-mode"


class World(Fake):
    """moose: card0 the GPU (DP-1, lit), card1 the ast (VGA-1); nobody logged in."""

    def __init__(self, passwordless=True):
        super().__init__("moose")
        self.clock = FakeClock()
        self.env = {"XDG_RUNTIME_DIR": RUN}
        self.files[PRIV] = ""
        self.card("card0", "nvidia", {"DP-1": ("connected", "enabled", "On")})
        self.card("card1", "ast", {"VGA-1": ("connected", "disabled", "Off")})
        self.sessions = []
        self.unit = False
        self.react(("sudo", "-n", PRIV, "session-status"), lambda a, f: Result(0 if passwordless else 1, "modeset:   Y\ndesktop:   none\n"))
        for sudo in (("sudo", "-n"), ("sudo",)):
            for verb in ("session-on", "session-on-bmc", "session-stop", "session-gdm", "session-gdm-bmc", "session-off"):
                self.react(sudo + (PRIV, verb), self.helper)
        self.react(("systemctl", "is-active", "--quiet"), lambda a, f: Result(0 if self.unit and a[3] == session.UNIT else 3))
        self.react(("systemctl", "is-active"), lambda a, f: Result(0, "active\n") if self.unit and a[2] == session.UNIT
                   else Result(3, "inactive\n"))
        self.answer(("systemctl", "show", session.UNIT, "-p", "MainPID", "--value"), out="0\n")
        self.answer(("sh", "-c", 'command -v "$1"', "sh"))
        self.react(("loginctl", "list-sessions", "--no-legend"),
                   lambda a, f: Result(0, "".join("%s 1000 u seat0 tty2\n" % s[0] for s in self.sessions)))
        self.react(("loginctl", "show-session"), self.show)
        self.react(("env", "WAYLAND_DISPLAY=" + SOCKET, "wayland-info"), self.wayland_info)

    def card(self, card, driver, conns):
        self.dirs.update({DRM, "%s/%s" % (DRM, card)})
        self.files["%s/%s/device/driver" % (DRM, card)] = "../../../bus/pci/drivers/" + driver
        for name, (status, enabled, dpms) in conns.items():
            base = "%s/%s-%s" % (DRM, card, name)
            self.dirs.add(base)
            self.files.update({base + "/status": status + "\n", base + "/enabled": enabled + "\n", base + "/dpms": dpms + "\n"})

    def light(self, conn, on):
        self.files[conn + "/enabled"] = "enabled\n" if on else "disabled\n"
        self.files[conn + "/dpms"] = "On\n" if on else "Off\n"

    def helper(self, argv, fake):
        verb = argv[-1]
        gpu, bmc = DRM + "/card0-DP-1", DRM + "/card1-VGA-1"
        self.sessions = [s for s in self.sessions if s[1] != "greeter"]
        if verb in ("session-on", "session-on-bmc", "session-off"):
            mode = {"session-on": "gpu", "session-on-bmc": "bmc", "session-off": "off"}[verb]
            self._set_file(SOCKET, "")
            self.files[MODE_FILE] = mode
            self.unit = True
            self.light(gpu, mode == "gpu")
            self.light(bmc, mode == "bmc")
        else:
            self.files.pop(SOCKET, None)
            self.files.pop(MODE_FILE, None)
            self.unit = False
            if verb.startswith("session-gdm"):
                self.sessions.append(("c1", "greeter", "wayland"))
        return Result(0, "%s done\n" % verb)

    def show(self, argv, fake):
        sid, prop = argv[2], argv[4]
        for s in self.sessions:
            if s[0] == sid:
                return Result(0, {"Class": s[1], "Type": s[2], "Leader": "1"}.get(prop, "") + "\n")
        return Result(1)

    def wayland_info(self, argv, fake):
        if SOCKET not in self.files:
            return Result(1)
        name = {"gpu": "DP-1", "bmc": "VGA-1"}.get(self.files.get(MODE_FILE), "")
        return Result(0, "\tname: %s\n\twidth: 1920 px, height: 1080 px, refresh: 60.000 Hz\n" % name if name else "")

    def s(self):
        return session.Session(ROOT, self, self.clock, self.env)

    def priv_verbs(self):
        return [e[1][-1] for e in self.effects if e[0] == "run" and PRIV in e[1] and e[1][-1] != "session-status"]

    def state(self):
        return (self.files.get(MODE_FILE), SOCKET in self.files, self.unit, tuple(self.sessions),
                self.files[DRM + "/card0-DP-1/dpms"])


class Recording(World):
    def act_run(self, argv, **kw):
        self.effects.append(("act", tuple(argv)))
        if act.dry_run():
            return Result(0)
        return super().act_run(argv, **kw)

    def mutations(self):
        return [e for e in self.effects if e[0] == "act"]


class SessionTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {}, clear=False)
        p.start()
        self.addCleanup(p.stop)
        for v in ("WK_DRY_RUN", "WK_QUIET", "WK_DESTRUCTIVE", "WK_CONFIRMED"):
            os.environ.pop(v, None)

    def go(self, fn, *args):
        with contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()) as out:
            rc = fn(*args)
        return rc, out.getvalue() + err.getvalue()

    def refused(self, fn, *args):
        with self.assertRaises(Refused):
            with contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
                fn(*args)
        return err.getvalue()


class TestTheReadings(SessionTest):
    def test_the_driver_tells_the_chips_apart_not_the_card_number(self):
        w = World()
        w.files[DRM + "/card0/device/driver"] = "../drivers/ast"
        w.files[DRM + "/card1/device/driver"] = "../drivers/nvidia"
        s = w.s()
        self.assertEqual(["DP-1 connected"], s.connectors("ast"))
        self.assertEqual(["VGA-1 connected"], s.connectors("not-ast"))

    def test_a_card_with_no_driver_is_neither_chip(self):
        w = World()
        del w.files[DRM + "/card0/device/driver"]
        self.assertEqual([], w.s().connectors("not-ast"))
        self.assertEqual("none", w.s().driver("card0"))

    def test_lit_needs_connected_enabled_and_on(self):
        """`dpms` alone reads On for a connector whose CRTC was never enabled."""
        w = World()
        self.assertEqual(["DP-1"], w.s().lit())
        w.files[DRM + "/card0-DP-1/enabled"] = "disabled\n"
        self.assertEqual([], w.s().lit())

    def test_the_outputs_and_mode_come_from_the_compositor(self):
        w = World()
        self.go(w.s().on, False)
        self.assertEqual(["DP-1"], w.s().outputs())
        self.assertEqual("1920x1080 @ 60.000Hz", w.s().display_mode())

    def test_the_greeter_is_not_somebodys_desktop(self):
        w = World()
        w.sessions = [("c1", "greeter", "wayland")]
        self.assertEqual("", w.s().foreign())
        w.sessions.append(("2", "user", "x11"))
        self.assertEqual("2 x11", w.s().foreign())

    def test_our_own_compositor_is_not_foreign(self):
        w = World()
        w.sessions = [("5", "user", "wayland")]
        w.answer(("systemctl", "show", session.UNIT, "-p", "MainPID", "--value"), out="1\n")
        self.assertEqual("", w.s().foreign())

    def test_a_greeter_that_never_appears_is_waited_for_and_given_up_on(self):
        w = World()
        self.assertEqual("", w.s().greeter())
        self.assertEqual(39, len(w.clock.slept))


class TestOn(SessionTest):
    def test_it_starts_the_gpu_compositor(self):
        w = World()
        rc, out = self.go(w.s().on, False)
        self.assertEqual(0, rc, out)
        self.assertEqual(["session-on"], w.priv_verbs())
        self.assertIn("session up: %s (mode gpu)" % SOCKET, out)
        self.assertIn("outputs: DP-1", out)

    def test_the_mode_already_running_is_left_alone(self):
        w = World()
        self.go(w.s().on, False)
        w.effects = []
        rc, out = self.go(w.s().on, False)
        self.assertEqual([], w.priv_verbs())
        self.assertIn("already running", out)

    def test_another_mode_running_is_restarted_as_the_one_asked_for(self):
        """"already running" answered for a software session is an afternoon of numbers that measured llvmpipe."""
        w = World()
        self.go(w.s().on, True)
        w.effects = []
        rc, out = self.go(w.s().on, False)
        self.assertEqual(["session-stop", "session-on"], w.priv_verbs())
        self.assertEqual("gpu", w.files[MODE_FILE])
        self.assertIn("restarting it as 'gpu'", out)

    def test_the_bmc_session_is_checked_for_a_bmc_output(self):
        w = World()
        rc, out = self.go(w.s().on, True)
        self.assertEqual(["session-on-bmc"], w.priv_verbs())
        self.assertNotIn("not on a BMC connector", out)
        self.assertIn("SLOW SESSION", out)
        w2 = World()
        w2.react(("env", "WAYLAND_DISPLAY=" + SOCKET, "wayland-info"), lambda a, f: Result(0, "\tname: DP-1\n"))
        rc, out = self.go(w2.s().on, True)
        self.assertIn("not on a BMC connector (outputs: DP-1)", out)

    def test_somebodys_desktop_is_left_alone(self):
        w = World()
        w.sessions = [("2", "user", "wayland")]
        rc, out = self.go(w.s().on, False)
        self.assertEqual(0, rc)
        self.assertEqual([], w.priv_verbs())
        self.assertIn("already active on seat0 (session 2 wayland)", out)

    def test_a_stale_socket_is_named_and_replaced(self):
        w = World()
        w._set_file(SOCKET, "")
        rc, out = self.go(w.s().on, False)
        self.assertIn("stale compositor socket", out)
        self.assertEqual(["session-on"], w.priv_verbs())

    def test_no_wayland_info_is_refused_before_anything_starts(self):
        w = World()
        w.answer(("sh", "-c", 'command -v "$1"', "sh", "wayland-info"), 1)
        self.assertIn("wayland-info missing", self.refused(w.s().on, False))
        self.assertEqual([], w.priv_verbs())

    def test_a_compositor_that_left_no_socket_is_refused(self):
        w = World()
        w.react(("sudo", "-n", PRIV, "session-on"), lambda a, f: Result(0))
        self.assertIn("no Wayland socket", self.refused(w.s().on, False))

    def test_a_password_is_asked_for_when_the_grant_is_missing(self):
        w = World(passwordless=False)
        rc, out = self.go(w.s().on, False)
        self.assertIn("passwordless sudo unavailable", out)
        self.assertIn(("run", ("sudo", PRIV, "session-on")), w.effects)

    def test_no_helper_is_refused(self):
        w = World()
        del w.files[PRIV]
        self.assertIn("is not installed", self.refused(w.s().on, False))


class TestGdmAndOff(SessionTest):
    def test_gdm_starts_a_wayland_greeter(self):
        w = World()
        rc, out = self.go(w.s().gdm, False)
        self.assertEqual(["session-gdm"], w.priv_verbs())
        self.assertIn("wayland greeter", out)

    def test_a_greeter_on_xorg_says_the_mode_is_not_enforced(self):
        w = World()
        w.react(("sudo", "-n", PRIV, "session-gdm-bmc"),
                lambda a, f: (w.sessions.append(("c1", "greeter", "x11")), Result(0))[1])
        rc, out = self.go(w.s().gdm, True)
        self.assertIn("came up on x11, not wayland -- the mode is not enforced", out)

    def test_off_turns_the_outputs_off_and_says_so(self):
        w = World()
        rc, out = self.go(w.s().off)
        self.assertEqual(["session-off"], w.priv_verbs())
        self.assertIn("screen off", out)

    def test_off_that_left_the_screen_lit_says_what_darkens_it(self):
        w = World()
        w.react(("sudo", "-n", PRIV, "session-off"), lambda a, f: Result(0))
        rc, out = self.go(w.s().off)
        self.assertIn("the screen is black but still lit: DP-1", out)

    def test_a_failed_helper_verb_is_refused(self):
        w = World()
        w.answer(("sudo", "-n", PRIV, "session-off"), 1)
        self.assertIn("failed to turn the screen off", self.refused(w.s().off))


class TestStatus(SessionTest):
    def test_it_reports_every_row_and_changes_nothing(self):
        w = World()
        self.go(w.s().on, False)
        before = w.state()
        w.effects = []
        out = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()):
            w.s().status(out)
        text = out.getvalue()
        for row in ("session:   active", "socket:    " + SOCKET, "dm:        inactive", "modeset:   Y", "mode:      gpu",
                    "lit:       DP-1", "gpu:       DP-1 connected", "bmc:       VGA-1 connected",
                    "outputs:   DP-1", "display:   1920x1080 @ 60.000Hz"):
            self.assertIn(row, text)
        self.assertEqual(before, w.state())
        self.assertEqual([], [e for e in w.effects if e[0] != "run"])


class TestCrashOnly(SessionTest):
    CASES = (("on gpu from bmc", lambda s: s.on(False), "bmc"), ("gdm", lambda s: s.gdm(False), None),
             ("off", lambda s: s.off(), "gpu"))

    def world(self, start, cls=World):
        w = cls()
        if start:
            self.go(w.s().on, start == "bmc")
        w.effects, w.applied = [], 0
        return w

    def test_each_verb_killed_after_any_effect_and_rerun_converges(self):
        """`killpoints[session]`: the helper verbs each mode takes, from the mode before it."""
        for what, verb, start in self.CASES:
            with self.subTest(verb=what):
                converges(self, lambda: types.SimpleNamespace(fake=self.world(start)),
                          lambda w: self.go(verb, w.fake.s()), lambda w: w.fake.state())

    def test_a_dry_run_is_the_wet_runs_plan_and_touches_nothing(self):
        for what, verb, start in self.CASES:
            with self.subTest(verb=what):
                wet = self.world(start, Recording)
                self.go(verb, wet.s())
                dry = self.world(start, Recording)
                before = dry.state()
                os.environ["WK_DRY_RUN"] = "1"
                try:
                    self.go(verb, dry.s())
                finally:
                    del os.environ["WK_DRY_RUN"]
                self.assertEqual(wet.mutations(), dry.mutations())
                self.assertTrue(dry.mutations())
                self.assertEqual(before, dry.state())


class TestTheCommand(SessionTest):
    def load(self):
        path = str(REPO / "cmd" / "session")
        loader = importlib.machinery.SourceFileLoader("wk_cmd_session", path)
        spec = importlib.util.spec_from_file_location("wk_cmd_session", path, loader=loader)
        m = importlib.util.module_from_spec(spec)
        loader.exec_module(m)
        return m

    def test_the_words_it_takes(self):
        m = self.load()
        self.assertEqual(("status", False), m.parse([]))
        self.assertEqual(("on", True), m.parse(["on", "--bmc"]))
        self.assertEqual(("gdm", True), m.parse(["gdm", "--mirror"]))
        self.assertIn("usage: wk session", self.refused(m.parse, ["up"]))

    def test_it_is_refused_off_linux(self):
        m = self.load()
        with mock.patch.object(m, "is_linux", return_value=False):
            self.assertIn("Linux-only", self.refused(m.main, ["status"]))


class TestOnMoose(WkTest):
    @requires_machine("moose")
    def test_modes_moose(self):
        """`live session.modes[moose]`: read-only, so what `status` reads there; driving each mode from
        each half-state is the owed half of the row."""
        tools = fleet.Fleet(REPO).load("moose").get("WK_REMOTE_TOOLS") or "Development/wk-tools"
        cp = subprocess.run(["ssh", "-o", "BatchMode=yes", "moose", "cd %s && ./wk session status" % tools],
                            capture_output=True, text=True, timeout=120)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        for row in ("session:", "mode:", "gpu:", "bmc:", "lit:"):
            self.assertIn(row, cp.stdout)


if __name__ == "__main__":
    unittest.main()
