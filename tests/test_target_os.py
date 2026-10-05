"""Target.os (lib/wk/targets.py): the platform a build in a target runs on, which decides a config's build system."""
import contextlib
import io
import platform
import sys
import tempfile
import unittest

from tests.support import REPO, fake_workspace

sys.path.insert(0, str(REPO / "lib"))
from wk import targets  # noqa: E402
from wk.machine import Fake  # noqa: E402
from wk.record import Records  # noqa: E402
from wk.store import Store  # noqa: E402


def _os(target):
    return targets.Registry(REPO, env={"HOME": "/nonexistent"}, machine=Fake()).load(target).os()


class TestEveryDriverAnswers(unittest.TestCase):
    def test_the_container_is_linux(self):
        self.assertEqual(_os("container"), "linux")

    def test_a_macos_guest_is_macos(self):
        self.assertEqual(_os("vm"), "macos")

    def test_a_workspace_answers_for_itself(self):
        """Inside a workspace `uname` decides, driven through `wk build --dry-run`."""
        want_darwin = platform.system() == "Darwin"
        with fake_workspace() as ws:
            cp = ws.run("build", "jsc-release", "--dry-run")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            line = [l for l in cp.stdout.splitlines() if "config:" in l][0]
        self.assertIn("xcode" if want_darwin else "cmake", line)


class _Target:
    def __init__(self, os_, store):
        self._os = os_
        self.store = Store({"WK_STORE": store})
        self.env = {"WK_STORE": store}

    def os(self):
        return self._os


class TestTheDefaultConfigIsDerived(unittest.TestCase):
    """Registry.default_config: the last build's config, from its task record, else the target platform's."""

    def _default(self, last_built, os_):
        with tempfile.TemporaryDirectory(prefix="wk-test-default-config-") as store:
            if last_built:
                t = Records(store, env={"WK_STORE": store}).begin("build", "here", "demo", "wk build demo --kill",
                                                                   "/nolog", ["building"])
                t.set("config", last_built)
            reg = targets.Registry(REPO, env={"HOME": "/nonexistent"}, machine=Fake())
            reg.ws_target = lambda name: "t"
            reg.load = lambda name: _Target(os_, store)
            with contextlib.redirect_stderr(io.StringIO()) as err:
                return reg.default_config("demo"), err.getvalue()

    def test_the_last_build_wins(self):
        self.assertEqual(self._default("mac-debug", "macos")[0], "mac-debug")

    def test_a_macos_target_with_no_build_defaults_to_the_apple_port(self):
        config, _ = self._default("", "macos")
        self.assertEqual(config, "mac-release")

    def test_a_linux_target_with_no_build_defaults_to_jsc(self):
        config, _ = self._default("", "linux")
        self.assertEqual(config, "jsc-release")



if __name__ == "__main__":
    unittest.main()
