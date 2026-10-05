"""`wk run <ws> --profile` (lib/wk/profile.py) against a Fake world."""
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes import FakeRegistry, WsDriver
from tests.support import REPO, as_dispatched, load_cmd

sys.path.insert(0, str(REPO / "lib"))
from wk import decl, ldpath, profile  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402

RUN = decl.Decl(REPO / "cmd" / "run")


class World(Fake):
    def __init__(self, tmp):
        super().__init__("here")
        self.tmp = Path(tmp)
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"),
                    "XDG_STATE_HOME": str(self.tmp / "state"), "WK_NAME": "ws", "WK_IN_VM": "1"}
        self.answer(["exec", "ws", "test", "-x"], rc=0)   # the binary is built
        self.place_os = "linux"
        self.reg = FakeRegistry(self.env, self, lambda n, e: WsDriver("box", str(REPO), e, self, os=self.place_os), ws_place=lambda ws: "box")


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk-test-profile-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        osenv = mock.patch.dict(os.environ, {}, clear=False)
        osenv.start()
        self.addCleanup(osenv.stop)
        for v in ("WK_DRY_RUN", "WK_NAME", "WK_PRESET"):
            os.environ.pop(v, None)
        os.environ["WK_NAME"] = "ws"
        self.w = World(self.tmp)

    def run_(self, *argv):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = profile.main(decl.Args(RUN, as_dispatched("run", argv, os.environ)), "run", self.w.reg)
        return rc, err.getvalue()

    def refused(self, *argv):
        with self.assertRaises(Refused) as cm:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                profile.main(decl.Args(RUN, as_dispatched("run", argv, os.environ)), "run", self.w.reg)
        return cm.exception, err.getvalue()

    def ready(self, level=1):
        self.w.answer(["exec", "ws", "cat", "/proc/sys/kernel/perf_event_paranoid"], out="%d\n" % level)
        self.w.answer(["exec", "ws", "sh", "-c"], rc=0)   # `command -v <profiler>` succeeds
        self.w.answer(["exec-tty", "ws", "bash", "-lc"], out="")


class TestPerfEventParanoidGate(ProfileTest):
    """The host-side refusal samply, sysprof and rr share (`ldpath.perf_events`), unreachable under --dry-run."""

    def test_paranoid_1_or_less_never_refuses(self):
        self.ready(1)
        rc, err = self.run_("--preset", "gtk-release", "--profile=samply", "--", "x.js")
        self.assertEqual(rc, 0, err)

    def test_high_paranoid_off_linux_names_the_sysctl_remedy(self):
        self.ready(2)
        with mock.patch.object(ldpath, "is_linux", return_value=False):
            e, err = self.refused("--preset", "gtk-release", "--profile=samply", "--", "x.js")
        self.assertEqual(e.status, 1)
        self.assertIn("perf_event_paranoid is 2 in 'ws'", err)
        self.assertIn("not namespaced", err)
        self.assertIn("echo 1 | sudo tee /proc/sys/kernel/perf_event_paranoid", err)

    def test_high_paranoid_on_linux_without_the_helper_names_setup(self):
        self.ready(2)
        with mock.patch.object(ldpath, "is_linux", return_value=True), \
             mock.patch("os.access", return_value=False):
            e, err = self.refused("--preset", "gtk-release", "--profile=samply", "--", "x.js")
        self.assertEqual(e.status, 1)
        self.assertIn("privileged helper", err)
        self.assertIn("./setup --stage quiesce", err)

    def test_high_paranoid_the_helper_could_not_fix_names_setup_too(self):
        self.ready(2)
        with mock.patch.object(ldpath, "is_linux", return_value=True), \
             mock.patch("os.access", return_value=True), \
             mock.patch.object(ldpath.Local, "act_run") as run:
            e, err = self.refused("--preset", "gtk-release", "--profile=samply", "--", "x.js")
        self.assertTrue(run.called)
        self.assertEqual(e.status, 1)
        self.assertIn("did not bring it down", err)


class TestSysprof(ProfileTest):
    def script(self):
        return [e[1][-1] for e in self.w.effects if e[0] == "run_tty" and e[1][:2] == ("exec-tty", "ws")][0]

    def test_sysprof_wraps_jsc_with_the_jit_dump_and_markers_on(self):
        self.ready()
        rc, err = self.run_("--preset", "gtk-release", "--profile=sysprof", "--", "x.js")
        self.assertEqual(rc, 0, err)
        cmd = self.script()
        self.assertRegex(cmd, r"sysprof-cli --force /home/u/wk-profile/\S+-sysprof/capture.syscap -- \S+/bin/jsc x.js")
        for env in ("JSC_useJITDump=1", "JSC_useTextMarkers=1", "JSC_jitDumpDirectory=/home/u/wk-profile/"):
            self.assertIn(env, cmd)

    def test_sysprof_asks_for_sysprof_cli_and_the_perf_events(self):
        self.ready(level=2)
        self.w.answer(["exec", "ws", "sh", "-c"], rc=1)
        e, err = self.refused("--preset", "gtk-release", "--profile=sysprof", "--", "x.js")
        self.assertIn("sysprof-cli is not installed in 'ws'", err)
        self.w.answer(["exec", "ws", "sh", "-c"], rc=0)
        with mock.patch.object(ldpath, "is_linux", return_value=False):
            e, err = self.refused("--preset", "gtk-release", "--profile=sysprof", "--", "x.js")
        self.assertIn("sysprof needs 1 or less", err)

    def test_sysprof_refuses_an_apple_port_and_an_attach(self):
        self.ready()
        e, err = self.refused("--preset", "gtk-release", "--profile=sysprof", "--attach", "123")
        self.assertIn("there is no attach", err)
        self.w.place_os = "macos"
        e, err = self.refused("--preset", "mac-release", "--profile=sysprof", "--", "x.js")
        self.assertIn("'mac-release' is an Apple-port build", err)

    def test_sysprof_against_the_browser_prefixes_minibrowser_and_refuses_process(self):
        self.ready()
        rc, err = self.run_("--preset", "gtk-release", "--profile=sysprof", "--browser", "about:blank")
        self.assertEqual(rc, 0, err)
        self.assertIn("WEBKIT_MINI_BROWSER_PREFIX='sysprof-cli --force", self.script())
        e, err = self.refused("--preset", "gtk-release", "--profile=sysprof", "--browser", "--process", "web")
        self.assertIn("--process is meaningless with --profile=sysprof", err)


class TestTheBrowserAndItsProcesses(ProfileTest):
    """`--browser` under each mode and `--process`, read off the dry run's plan."""

    NAMES = {"wpe-release": {"web": "WPEWebProcess", "network": "WPENetworkProcess", "gpu": "WPEGPUProcess"},
             "gtk-release": {"web": "WebKitWebProcess", "network": "WebKitNetworkProcess", "gpu": "WebKitGPUProcess"}}

    def setUp(self):
        super().setUp()
        os.environ["WK_DRY_RUN"] = "1"

    def test_an_unknown_mode_or_process_is_refused_naming_the_valid_ones(self):
        for argv, words in ((("--profile=bogus",), ("no such mode",) + profile.MODES),
                            (("--profile", "--process", "bogus"), ("no such process", "ui", "web", "network", "gpu")),
                            (("--profile", "--process", "ui"), ("--browser",))):
            with self.subTest(argv=argv):
                _, err = self.refused(*argv)
                for w in words:
                    self.assertIn(w, err)

    def test_the_ui_process_is_prefixed_through_minibrowsers_variable(self):
        for preset in self.NAMES:
            with self.subTest(preset=preset):
                rc, err = self.run_("--preset", preset, "--browser", "--process", "ui", "--profile=samply")
                self.assertEqual(rc, 0, err)
                self.assertIn("WEBKIT_MINI_BROWSER_PREFIX=", err)
                self.assertNotIn("samply", next(l for l in err.splitlines() if "run-minibrowser" in l))

    def test_a_child_process_is_attached_to_by_pid_after_launch(self):
        for preset, names in self.NAMES.items():
            for process, name in names.items():
                with self.subTest(preset=preset, process=process):
                    rc, err = self.run_("--preset", preset, "--browser", "--profile=samply", "--process", process)
                    self.assertEqual(rc, 0, err)
                    for w in (name, "pgrep", "samply record", " -p "):
                        self.assertIn(w, err)

    def test_the_apple_port_profiles_minibrowser_itself_and_refuses_process(self):
        self.w.place_os = "macos"
        rc, err = self.run_("--preset", "mac-release", "--browser", "--profile")
        self.assertEqual(rc, 0, err)
        self.assertIn("MiniBrowser.app/Contents/MacOS/MiniBrowser", err)
        self.assertNotIn("WEBKIT_MINI_BROWSER_PREFIX", err)
        _, err = self.refused("--preset", "mac-release", "--browser", "--process", "web", "--profile")
        self.assertIn("not wired up for the Apple ports", err)

    def test_a_mode_that_covers_the_whole_tree_or_must_start_first_is_refused(self):
        _, err = self.refused("--preset", "gtk-release", "--browser", "--process", "ui", "--profile=sampling")
        self.assertIn("meaningless", err)
        for mode in ("heaptrack", "massif"):
            _, err = self.refused("--preset", "gtk-release", "--browser", "--profile=" + mode)
            self.assertIn("not wired up", err)


class TestFetch(ProfileTest):
    def test_fetch_copies_the_run_directory_out(self):
        fetch_dir = os.path.join(self.tmp, "fetched")

        def make_dir_then_run(argv, fake):
            fake.dirs.add(re.search(r"/home/u/wk-profile/\S+", argv[-1]).group(0))
            return Result(0)
        self.w.react(["exec-tty", "ws", "bash", "-lc"], make_dir_then_run)
        rc, err = self.run_("--preset", "gtk-release", "--profile=sampling", "--fetch", fetch_dir, "--", "x.js")
        self.assertEqual(rc, 0, err)
        copies = [e for e in self.w.effects if e[0] == "copy_tree_out"]
        self.assertEqual(len(copies), 1, self.w.effects)
        self.assertEqual(copies[0][2], fetch_dir)
        self.assertIn("copied to %s" % fetch_dir, err)

    def test_a_fetch_that_finds_nothing_to_copy_warns_rather_than_dies(self):
        self.w.answer(["exec-tty", "ws", "bash", "-lc"], out="")
        rc, err = self.run_("--preset", "gtk-release", "--profile=sampling", "--fetch", os.path.join(self.tmp, "f"), "--", "x.js")
        self.assertEqual(rc, 0, err)
        self.assertIn("could not copy", err)


class TestOutputReachesTheTerminal(ProfileTest):
    def test_the_run_and_post_go_through_exec_tty_not_the_capturing_exec(self):
        self.w.answer(["exec-tty", "ws", "bash", "-lc"], out="")
        rc, err = self.run_("--preset", "gtk-release", "--profile=bytecode", "--", "x.js")
        self.assertEqual(rc, 0, err)
        tty_runs = [e for e in self.w.effects if e[0] == "run_tty" and e[1][:2] == ("exec-tty", "ws")]
        self.assertEqual(2, len(tty_runs), self.w.effects)   # the run itself, and bytecode's `post`
        capturing = [e for e in self.w.effects if e[0] == "run" and e[1][:2] == ("exec", "ws") and "bash" in e[1]]
        self.assertEqual([], capturing, "the run and post must not go through the capturing exec")

    def test_the_next_step_it_names_is_the_command_that_ran(self):
        self.w.answer(["exec-tty", "ws", "bash", "-lc"], out="")
        _, err = self.run_("--preset", "gtk-release", "--profile=sampling", "--", "x.js")
        self.assertIn("wk run ws --profile=bytecode", err)


class TestRunAndTestHandItOver(ProfileTest):
    """`wk run --profile` and `wk test --profile` are one profiler; a profiler flag needs --profile, and --profile runs alone."""

    def test_both_commands_reach_the_one_profiler(self):
        for cmd in ("run", "test"):
            with self.subTest(cmd=cmd), mock.patch.object(profile, "main", return_value=0) as main:
                load_cmd(cmd).main(as_dispatched(cmd, ["--profile=samply", "--", "x.js"], os.environ))
                args, which = main.call_args[0][:2]
                self.assertEqual((which, args.value("--profile"), args.tail), (cmd, "samply", ["x.js"]))

    def test_a_profiler_flag_alone_or_a_flag_of_the_command_beside_it_is_refused(self):
        for cmd, argv, said in (("run", ["--browser"], "--browser is --profile's"),
                                ("run", ["--profile", "--lldb"], "--profile runs on its own; drop --lldb"),
                                ("test", ["--profile=bytecode", "--layout"], "--profile runs on its own; drop --layout")):
            with self.subTest(cmd=cmd, argv=argv), self.assertRaises(Refused), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                load_cmd(cmd).main(as_dispatched(cmd, argv, os.environ))
            self.assertIn(said, err.getvalue())


if __name__ == "__main__":
    unittest.main()
