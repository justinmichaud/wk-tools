"""Driver.os (lib/wk/places.py): the platform a build in a place runs on, which decides a preset's build system."""
import contextlib
import io
import platform
import sys
import tempfile
import unittest

from tests.support import REPO, fake_workspace

sys.path.insert(0, str(REPO / "lib"))
from wk import places, presets  # noqa: E402
from wk.machine import Fake  # noqa: E402
from wk.record import Records  # noqa: E402
from wk.store import Store  # noqa: E402


class TestEveryDriverAnswers(unittest.TestCase):
    def test_a_workspace_answers_for_itself(self):
        """Inside a workspace `uname` decides, driven through `wk build --dry-run`."""
        want_darwin = platform.system() == "Darwin"
        with fake_workspace() as ws:
            cp = ws.run("build", "jsc-release", "--dry-run")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            line = [l for l in cp.stdout.splitlines() if "preset:" in l][0]
        self.assertIn("xcode" if want_darwin else "cmake", line)


class _Target:
    def __init__(self, os_, store):
        self._os = os_
        self.store = Store({"WK_STORE": store})
        self.env = {"WK_STORE": store}

    def os(self):
        return self._os


class TestTheDefaultConfigIsDerived(unittest.TestCase):
    """Registry.default_preset: the last build's preset, from its task record, else the place platform's."""

    def _default(self, last_built, os_):
        with tempfile.TemporaryDirectory(prefix="wk-test-default-preset-") as store:
            if last_built:
                t = Records(store, env={"WK_STORE": store}).begin("build", "here", "demo", "wk build demo --kill",
                                                                   "/nolog", ["building"])
                t.set("preset", last_built)
            reg = places.Registry(REPO, env={"HOME": "/nonexistent"}, machine=Fake())
            reg.ws_place = lambda name: "t"
            reg.load = lambda name: _Target(os_, store)
            with contextlib.redirect_stderr(io.StringIO()) as err:
                return presets.default_preset(reg, "demo"), err.getvalue()

    def test_the_last_build_wins(self):
        self.assertEqual(self._default("mac-debug", "macos")[0], "mac-debug")

    def test_a_macos_target_with_no_build_defaults_to_the_apple_port(self):
        preset, _ = self._default("", "macos")
        self.assertEqual(preset, "mac-release")

    def test_a_linux_target_with_no_build_defaults_to_jsc(self):
        preset, _ = self._default("", "linux")
        self.assertEqual(preset, "jsc-release")



if __name__ == "__main__":
    unittest.main()
