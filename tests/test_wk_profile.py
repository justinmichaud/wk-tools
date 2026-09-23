"""`wk profile` (cmd/profile) against a Fake world: the host-side
perf_event_paranoid gate (samply's own refusal, not reachable under
--dry-run) and `--fetch` copying the recording out byte for byte.

test_profile_debug.py already drives the mode/browser/process argv and
refusal shapes through the real dispatcher against a FakeWorkspace; this
file covers what only a Fake machine can reach: a host setting samply
depends on, and a real (non-dry) run's artifact copy.

Run: python3 tests/run.py -k test_wk_profile
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import targets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402

CMD_LOADER = importlib.machinery.SourceFileLoader("cmd_profile", str(REPO / "cmd" / "profile"))
CMD = importlib.util.module_from_spec(importlib.util.spec_from_loader("cmd_profile", CMD_LOADER))
CMD_LOADER.exec_module(CMD)


class ProfileTarget(targets.Target):
    def __init__(self, name, root, env, machine, kind="container"):
        super().__init__(name, root, env, machine)
        self.kind = kind

    def info(self, ws):
        return "running"

    def state(self, ws, info=None):
        return "present"

    def os(self):
        return "linux"

    def src(self, ws):
        return "/src/WebKit"

    def home(self):
        return "/home/u"

    def exec(self, ws, argv, tty=False, timeout=None):
        return self.machine.run(["exec", ws] + list(argv))

    def exec_tty(self, ws, argv, timeout=None):
        return self.machine.run_tty(["exec-tty", ws] + list(argv))


class Reg(targets.Registry):
    def __init__(self, world):
        super().__init__(REPO, env=world.env, machine=world)
        self.world = world

    def load(self, name):
        return ProfileTarget("box", self.root, dict(self.env), self.world)

    def ws_target(self, ws):
        return "box"


class World(Fake):
    def __init__(self, tmp):
        super().__init__("here")
        self.tmp = Path(tmp)
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"),
                    "XDG_STATE_HOME": str(self.tmp / "state"), "WK_NAME": "ws", "WK_IN_VM": "1"}
        self.answer(["exec", "ws", "test", "-x"], rc=0)   # the binary is built
        self.reg = Reg(self)


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk-test-profile-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        osenv = mock.patch.dict(os.environ, {}, clear=False)
        osenv.start()
        self.addCleanup(osenv.stop)
        for v in ("WK_DRY_RUN", "WK_NAME", "WK_CONFIG"):
            os.environ.pop(v, None)
        os.environ["WK_NAME"] = "ws"
        self.w = World(self.tmp)

    def run_(self, *argv):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = CMD.main(list(argv), self.w.reg)
        return rc, err.getvalue()

    def refused(self, *argv):
        with self.assertRaises(Refused) as cm:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                CMD.main(list(argv), self.w.reg)
        return cm.exception, err.getvalue()


class TestPerfEventParanoidGate(ProfileTest):
    """samply's own host-side refusal: `_check_perf_paranoid`, unreachable under
    --dry-run (the mode block that calls it is skipped entirely)."""

    def _paranoid(self, level):
        self.w.answer(["exec", "ws", "cat", "/proc/sys/kernel/perf_event_paranoid"], out="%d\n" % level)
        self.w.answer(["exec", "ws", "sh", "-c"], rc=0)   # `command -v samply` succeeds

    def test_paranoid_1_or_less_never_refuses(self):
        self._paranoid(1)
        self.w.answer(["exec-tty", "ws", "bash", "-lc"], out="")
        rc, err = self.run_("--config", "gtk-release", "--mode", "samply", "--", "x.js")
        self.assertEqual(rc, 0, err)

    def test_high_paranoid_off_linux_names_the_sysctl_remedy(self):
        self._paranoid(2)
        with mock.patch.object(CMD, "is_linux", return_value=False):
            e, err = self.refused("--config", "gtk-release", "--mode", "samply", "--", "x.js")
        self.assertEqual(e.status, 1)
        self.assertIn("perf_event_paranoid is 2 in 'ws'", err)
        self.assertIn("not namespaced", err)
        self.assertIn("echo 1 | sudo tee /proc/sys/kernel/perf_event_paranoid", err)

    def test_high_paranoid_on_linux_without_the_helper_names_setup(self):
        self._paranoid(2)
        with mock.patch.object(CMD, "is_linux", return_value=True), \
             mock.patch("os.access", return_value=False):
            e, err = self.refused("--config", "gtk-release", "--mode", "samply", "--", "x.js")
        self.assertEqual(e.status, 1)
        self.assertIn("privileged helper", err)
        self.assertIn("./setup --stage quiesce", err)

    def test_high_paranoid_the_helper_could_not_fix_names_setup_too(self):
        self._paranoid(2)
        with mock.patch.object(CMD, "is_linux", return_value=True), \
             mock.patch("os.access", return_value=True), \
             mock.patch("subprocess.run") as run:
            e, err = self.refused("--config", "gtk-release", "--mode", "samply", "--", "x.js")
        self.assertTrue(run.called)
        self.assertEqual(e.status, 1)
        self.assertIn("did not bring it down", err)


class TestFetch(ProfileTest):
    def test_fetch_copies_the_run_directory_out(self):
        self.w.answer(["exec-tty", "ws", "bash", "-lc"], out="")
        fetch_dir = os.path.join(self.tmp, "fetched")

        def make_dir_then_run(argv, fake):
            fake.dirs.add(re.search(r"/home/u/wk-profile/\S+", argv[-1]).group(0))
            return Result(0)
        self.w.react(["exec-tty", "ws", "bash", "-lc"], make_dir_then_run)
        rc, err = self.run_("--config", "gtk-release", "--mode", "sampling", "--fetch", fetch_dir, "--", "x.js")
        self.assertEqual(rc, 0, err)
        copies = [e for e in self.w.effects if e[0] == "copy_tree_out"]
        self.assertEqual(len(copies), 1, self.w.effects)
        self.assertEqual(copies[0][2], fetch_dir)
        self.assertIn("copied to %s" % fetch_dir, err)

    def test_a_fetch_that_finds_nothing_to_copy_warns_rather_than_dies(self):
        self.w.answer(["exec-tty", "ws", "bash", "-lc"], out="")
        rc, err = self.run_("--config", "gtk-release", "--mode", "sampling", "--fetch", os.path.join(self.tmp, "f"), "--", "x.js")
        self.assertEqual(rc, 0, err)
        self.assertIn("could not copy", err)


class TestOutputReachesTheTerminal(ProfileTest):
    """The run (and `post`, for a mode that has one) inherit this process's own
    stdio through exec_tty rather than being captured and dropped by exec."""

    def test_the_run_and_post_go_through_exec_tty_not_the_capturing_exec(self):
        self.w.answer(["exec-tty", "ws", "bash", "-lc"], out="")
        rc, err = self.run_("--config", "gtk-release", "--mode", "bytecode", "--", "x.js")
        self.assertEqual(rc, 0, err)
        tty_runs = [e for e in self.w.effects if e[0] == "run_tty" and e[1][:2] == ("exec-tty", "ws")]
        self.assertEqual(2, len(tty_runs), self.w.effects)   # the run itself, and bytecode's `post`
        capturing = [e for e in self.w.effects if e[0] == "run" and e[1][:2] == ("exec", "ws") and "bash" in e[1]]
        self.assertEqual([], capturing, "the run and post must not go through the capturing exec")

    def test_sampling_default_mode_also_streams(self):
        self.w.answer(["exec-tty", "ws", "bash", "-lc"], out="")
        rc, err = self.run_("--config", "gtk-release", "--mode", "sampling", "--", "x.js")
        self.assertEqual(rc, 0, err)
        self.assertTrue([e for e in self.w.effects if e[0] == "run_tty" and e[1][:2] == ("exec-tty", "ws")], self.w.effects)


if __name__ == "__main__":
    unittest.main()
