"""`wk quiesce session`: lib/wk/session.py against a fake Linux machine with a GPU and the BMC's `ast` chip."""

import contextlib
import io
import os
import subprocess
import sys
import types
import unittest
from unittest import mock

from tests.killpoints import converges
from tests.support import REPO, WkTest, as_dispatched, load_cmd, requires_machine

sys.path.insert(0, str(REPO / "lib"))
from wk import act, fleet, quiet, session  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import HAVE, Fake, Result  # noqa: E402

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
        for verb in ("session-on", "session-on-bmc", "session-stop", "session-gdm", "session-gdm-bmc", "session-off"):
            self.react(("sudo", "-n", PRIV, verb), self.helper)
        self.react(("systemctl", "is-active", "--quiet"), lambda a, f: Result(0 if self.unit and a[3] == session.UNIT else 3))
        self.react(("systemctl", "is-active"), lambda a, f: Result(0, "active\n") if self.unit and a[2] == session.UNIT
                   else Result(3, "inactive\n"))
        self.answer(("systemctl", "show", session.UNIT, "-p", "MainPID", "--value"), out="0\n")
        self.answer(HAVE)
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

    def world(self, start=None, cls=World):
        w = cls()
        if start:
            self.go(w.s().on, start == "bmc")
        w.effects, w.applied = [], 0
        return w


class TestTheReadings(SessionTest):
    def test_the_driver_tells_the_chips_apart_not_the_card_number(self):
        w = World()
        w.files[DRM + "/card0/device/driver"] = "../drivers/ast"
        w.files[DRM + "/card1/device/driver"] = "../drivers/nvidia"
        self.assertEqual((["DP-1 connected"], ["VGA-1 connected"]), (w.s().connectors("ast"), w.s().connectors("not-ast")))
        del w.files[DRM + "/card1/device/driver"]
        self.assertEqual(([], "none"), (w.s().connectors("not-ast"), w.s().driver("card1")))

    def test_lit_needs_connected_enabled_and_on(self):
        w = World()
        self.assertEqual(["DP-1"], w.s().lit())
        w.files[DRM + "/card0-DP-1/enabled"] = "disabled\n"
        self.assertEqual([], w.s().lit())

    def test_neither_the_greeter_nor_our_own_compositor_is_somebodys_desktop(self):
        w = World()
        w.sessions = [("c1", "greeter", "wayland")]
        self.assertEqual("", w.s().foreign())
        w.sessions.append(("2", "user", "x11"))
        self.assertEqual("2 x11", w.s().foreign())
        w.sessions = [("5", "user", "wayland")]
        w.answer(("systemctl", "show", session.UNIT, "-p", "MainPID", "--value"), out="1\n")
        self.assertEqual("", w.s().foreign())

    def test_a_greeter_that_never_appears_is_waited_for_and_given_up_on(self):
        w = World()
        self.assertEqual("", w.s().greeter())
        self.assertEqual(39, len(w.clock.slept))


class TestOn(SessionTest):
    def test_each_starting_state_ends_in_the_mode_asked_for(self):
        stale = World()
        stale._set_file(SOCKET, "")
        theirs = World()
        theirs.sessions = [("2", "user", "wayland")]
        for name, w, bmc, verbs, words in (
                ("fresh", World(), False, ["session-on"], ["session up: %s (mode gpu)" % SOCKET, "outputs: DP-1"]),
                ("same mode", self.world("gpu"), False, [], ["already running"]),
                ("other mode", self.world("bmc"), False, ["session-stop", "session-on"], ["restarting it as 'gpu'"]),
                ("bmc", World(), True, ["session-on-bmc"], ["SLOW SESSION"]),
                ("somebody's desktop", theirs, False, [], ["already active on seat0 (session 2 wayland)"]),
                ("stale socket", stale, False, ["session-on"], ["stale compositor socket"])):
            with self.subTest(name):
                rc, out = self.go(w.s().on, bmc)
                self.assertEqual(0, rc, out)
                self.assertEqual(verbs, w.priv_verbs())
                for word in words:
                    self.assertIn(word, out)
                self.assertNotIn("not on a BMC connector", out)
                if verbs:
                    self.assertEqual("bmc" if bmc else "gpu", w.files[MODE_FILE])

    def test_a_bmc_session_on_another_output_says_so(self):
        w = World()
        w.react(("env", "WAYLAND_DISPLAY=" + SOCKET, "wayland-info"), lambda a, f: Result(0, "\tname: DP-1\n"))
        self.assertIn("not on a BMC connector (outputs: DP-1)", self.go(w.s().on, True)[1])

    def test_what_it_cannot_start_is_refused_naming_why(self):
        no_info = World()
        no_info.answer(HAVE + ("wayland-info",), 1)
        no_socket = World()
        no_socket.react(("sudo", "-n", PRIV, "session-on"), lambda a, f: Result(0))
        no_helper = World()
        del no_helper.files[PRIV]
        for w, words, started in ((no_info, "wayland-info missing", False), (no_socket, "no Wayland socket", True),
                                  (World(passwordless=False), "./setup --stage quiesce", False),
                                  (no_helper, "is not installed", False)):
            with self.subTest(words):
                self.assertIn(words, self.refused(w.s().on, False))
                self.assertEqual(started, bool(w.priv_verbs()))


class TestGdmAndOff(SessionTest):
    def test_each_verb_runs_its_helper_verb_and_says_what_it_left(self):
        xorg = World()
        xorg.react(("sudo", "-n", PRIV, "session-gdm-bmc"),
                   lambda a, f: (xorg.sessions.append(("c1", "greeter", "x11")), Result(0))[1])
        lit = World()
        lit.react(("sudo", "-n", PRIV, "session-off"), lambda a, f: Result(0))
        for w, verb, args, helper, words in (
                (World(), "gdm", (False,), "session-gdm", "wayland greeter"),
                (xorg, "gdm", (True,), "session-gdm-bmc", "came up on x11, not wayland -- the mode is not enforced"),
                (World(), "off", (), "session-off", "screen off"),
                (lit, "off", (), "session-off", "the screen is black but still lit: DP-1")):
            with self.subTest(words):
                rc, out = self.go(getattr(w.s(), verb), *args)
                self.assertEqual([helper], w.priv_verbs())
                self.assertIn(words, out)

    def test_a_failed_helper_verb_is_refused(self):
        w = World()
        w.answer(("sudo", "-n", PRIV, "session-off"), 1)
        self.assertIn("failed to turn the screen off", self.refused(w.s().off))


class TestStatus(SessionTest):
    def test_it_reports_every_row_and_changes_nothing(self):
        w = self.world("gpu")
        before = w.state()
        out = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()):
            w.s().status(out)
        for row in ("session:   active", "socket:    " + SOCKET, "dm:        inactive", "modeset:   Y", "mode:      gpu",
                    "lit:       DP-1", "gpu:       DP-1 connected", "bmc:       VGA-1 connected",
                    "outputs:   DP-1", "display:   1920x1080 @ 60.000Hz"):
            self.assertIn(row, out.getvalue())
        self.assertEqual(before, w.state())
        self.assertEqual([], [e for e in w.effects if e[0] != "run"])


class TestCrashOnly(SessionTest):
    CASES = (("on gpu from bmc", lambda s: s.on(False), "bmc"), ("gdm", lambda s: s.gdm(False), None),
             ("off", lambda s: s.off(), "gpu"))

    def test_each_verb_killed_after_any_effect_and_rerun_converges(self):
        for what, verb, start in self.CASES:
            with self.subTest(verb=what):
                converges(self, lambda: types.SimpleNamespace(fake=self.world(start)),
                          lambda w: self.go(verb, w.fake.s()), lambda w: w.fake.state())

    def test_a_dry_run_is_the_wet_runs_plan_and_touches_nothing(self):
        for what, verb, start in self.CASES:
            with self.subTest(verb=what):
                wet, dry = self.world(start, Recording), self.world(start, Recording)
                self.go(verb, wet.s())
                before = dry.state()
                with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
                    self.go(verb, dry.s())
                self.assertEqual(wet.mutations(), dry.mutations())
                self.assertTrue(dry.mutations())
                self.assertEqual(before, dry.state())


class TestTheCommand(SessionTest):
    def test_the_words_it_takes(self):
        m = load_cmd("quiesce")
        with mock.patch.object(m, "is_linux", return_value=True), mock.patch.object(m.session, "Session") as s:
            m.main(as_dispatched("quiesce", ["session"], {}))
            m.main(["session", "on", "--bmc"])
            m.main(["session", "gdm", "--bmc"])
        self.assertEqual([c[0] for c in s.return_value.method_calls], ["status", "on", "gdm"])
        self.assertEqual([c[1] for c in s.return_value.method_calls], [(), (True,), (True,)])
        self.assertIn("on, gdm, off or status", self.refused(m.main, ["session", "up"]))
        self.assertIn("--bmc moves the session", self.refused(m.main, ["session", "off", "--bmc"]))
        with mock.patch.object(m, "is_linux", return_value=False):
            self.assertIn("Linux-only", self.refused(m.main, ["session", "status"]))


class TestOnMoose(WkTest):
    @requires_machine("moose")
    def test_modes_moose(self):
        tools = fleet.Fleet(REPO).load("moose").get("tools") or "Development/wk-tools"
        cp = subprocess.run(["ssh", "-o", "BatchMode=yes", "moose", "cd %s && ./wk quiesce session status" % tools],
                            capture_output=True, text=True, timeout=120)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        for row in ("session:", "mode:", "gpu:", "bmc:", "lit:"):
            self.assertIn(row, cp.stdout)


if __name__ == "__main__":
    unittest.main()
