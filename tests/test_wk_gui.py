"""`wk gui`: MiniBrowser in the benchmark seat, typed into a world holding workspace `ws` on a Linux x86_64 host."""
import os
import unittest
from unittest import mock

from tests.test_wk_workspace import Result, World, WorkspaceTest

MODE = "/run/wk-session-mode"


def uname(sysname, machine="x86_64"):
    real = os.uname()
    return lambda: os.uname_result((sysname, real.nodename, real.release, real.version, machine))


class Seat(World):
    """A host with a display session (`session`, its mode in /run/wk-session-mode) whose workspace answers whether a
    browser is built (`browser`); a guest bridge has its address."""

    def __init__(self, tmp, **kw):
        super().__init__(tmp, **kw)
        self.session, self.browser = True, True
        self.react(["test", "-S"], lambda a, f: Result(0 if f.session else 1))
        self.answer(["ifconfig"], out="bridge100: flags=8863<UP>\n\tinet 192.168.2.1 netmask 0xffffff00\n")
        if getattr(self, "tart", None):   # a guest's `test -x <browser>`
            self.react([self.tart, "exec"], lambda a, f: Result(0 if f.browser else 1))

    def _in_ws(self, argv, f):
        return Result(0 if f.browser else 1) if argv[2:4] == ["test", "-x"] else super()._in_ws(argv, f)


class GuiTest(WorkspaceTest):
    world_class = Seat
    HOST = ("Linux", "x86_64")

    def setUp(self):
        super().setUp()
        self.w.make("ws")
        p = mock.patch("os.uname", uname(*self.HOST))
        p.start()
        self.addCleanup(p.stop)

    def world(self, kind):
        w = self.make_world(kinds={"fakebox": kind})
        w.make("ws")
        return w

    def typed(self, cmd, *argv, w=None):
        """`wk <cmd> ws <argv>`: (exit status, the line acted on -- an error, else a dry run's, else the first -- and the
        command run in the workspace or None)."""
        w = w or self.w
        os.environ.pop("WK_PRESET", None)   # the dispatcher exports the last call's preset into this process
        w.effects = []
        rc, _, err = self.wk(w, cmd, "ws", *argv)
        last = w.effects[-1] if w.effects else (None, ())
        lines = err.splitlines() or [""]
        return rc, next((x for x in lines if x.startswith(("error: ", "would run: "))), lines[0]), last[1][-1] if last[0] == "exec" else None


class TestRefusals(GuiTest):
    def test_a_remote_place_is_refused_before_anything_runs_in_it(self):
        """`unit gui.refuses_remote`"""
        self.assertEqual(self.typed("gui", "--preset", "gtk-release", w=self.world("remote")),
                         (1, "error: wk gui needs a benchmark seat -- a container, a guest or the workspace itself:", None))

    def test_a_preset_with_no_browser_is_refused_naming_the_ones_that_have_one(self):
        self.assertEqual(self.typed("gui", "--preset", "jsc-release"), (1, "error: 'jsc-release' builds JavaScriptCore alone -- there is no browser in it.", None))
        with mock.patch("os.uname", uname("Darwin", "arm64")):
            self.assertEqual(self.typed("gui", "--preset", "ios-sim-release", w=self.world("vm")),
                             (1, "error: 'ios-sim-release' builds no browser to drive -- use a wpe-*, gtk-* or mac-* preset", None))

    def test_no_minibrowser_built_is_refused_naming_the_build(self):
        self.w.browser = False
        self.assertEqual(self.typed("gui", "--preset", "gtk-release"),
                         (1, "error: no MiniBrowser at /src/WebKit/WebKitBuild/GTK/Release/bin/MiniBrowser -- wk build ws gtk-release", None))

    def test_a_container_on_a_macos_host_has_no_display(self):
        with mock.patch("os.uname", uname("Darwin", "arm64")):
            self.assertEqual(self.typed("gui", "--preset", "gtk-release"),
                             (1, "error: a container workspace on a macOS host has no display, and cannot get one:", None))

    def test_no_graphical_session_is_refused_naming_the_command_that_makes_one(self):
        self.w.session = False
        self.assertEqual(self.typed("gui", "--preset", "gtk-release"), (1, "error: no graphical session -- 'wk quiesce session on', "
                                                                 "or 'wk quiesce session on --bmc' to watch it over the BMC", None))


class TestLaunch(GuiTest):
    def test_each_port_launches_minibrowser_fullscreen_with_its_own_flag(self):
        for preset, port, flag in (("wpe-release", "--wpe", "--fullscreen"), ("gtk-release", "--gtk", "--full-screen")):
            self.assertEqual(self.typed("gui", "--preset", preset), (0, "==> MiniBrowser (%s) in 'ws' -> about:blank" % preset,
                             "cd /src/WebKit && WPE_BROWSER=minibrowser Tools/Scripts/run-minibrowser %s --release -- %s about:blank " % (port, flag)))
        self.assertTrue(self.typed("gui", "--preset", "gtk-release", "--no-fullscreen")[2].endswith("--release --  about:blank "))

    def test_the_apple_port_opens_the_url_with_no_fullscreen_flag(self):
        with mock.patch("os.uname", uname("Darwin", "arm64")):
            cmd = self.typed("gui", "https://webkit.org", w=self.world("vm"))[2]
        self.assertRegex(cmd, r"/MiniBrowser\.app/Contents/MacOS/MiniBrowser --url https://webkit\.org '$")

    def test_a_bmc_seat_pins_the_browser_to_software_rendering_on_the_ast(self):
        self.w.files[MODE] = "bmc\n"
        self.w.dirs.add("/sys/class/drm")
        self.w.files["/sys/class/drm/card1/device/driver"] = "../../../bus/pci/drivers/ast"
        cmd = self.typed("gui", "--preset", "wpe-release")[2]
        self.assertIn("WPE_BROWSER=minibrowser WEBKIT_DISABLE_DMABUF_RENDERER=1 LIBGL_ALWAYS_SOFTWARE=1 ", cmd)
        self.assertIn(" WPE_DRM_DEVICE=/dev/dri/card1 Tools/Scripts/run-minibrowser", cmd)

    def test_a_dry_run_prints_the_launch_and_runs_nothing(self):
        rc, line, cmd = self.typed("gui", "--dry-run", "--preset", "gtk-release")
        self.assertEqual((rc, cmd), (0, None))
        self.assertRegex(line, r"^would run: env .*/wkdev-enter .*/run-minibrowser --gtk --release -- --full-screen about:blank '$")


if __name__ == "__main__":
    unittest.main()
