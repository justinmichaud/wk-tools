"""Target.os (lib/wk/targets.py): the platform a build in a target runs on,
`linux` or `macos`. It is not decoration -- the build reads it to decide a
config's build system, because Xcode is the only one on macOS -- so every
driver has to answer it, and answer it from evidence rather than from a name.

Run: python3 -m unittest tests.test_target_os -v
"""
import contextlib
import inspect
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
        """The SDK image is Fedora, whatever the workstation holding it is."""
        self.assertEqual(_os("container"), "linux")

    def test_a_macos_guest_is_macos(self):
        """The vm target exists to build the Apple ports; there is no other
        kind of guest it makes."""
        self.assertEqual(_os("vm"), "macos")

    def test_a_workspace_answers_for_itself(self):
        """LocalWorkspace: inside a workspace `uname` is the truth -- a
        Fedora container says Linux, a macOS guest says Darwin -- so
        `wk build <config>` typed in there picks the same build system the
        host would have picked for it. Driven through `wk build --dry-run`,
        the whole path, since the driver refuses to load without a marker."""
        want_darwin = platform.system() == "Darwin"
        with fake_workspace() as ws:
            cp = ws.run("build", "jsc-release", "--dry-run")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            line = [l for l in cp.stdout.splitlines() if "config:" in l][0]
        self.assertIn("xcode" if want_darwin else "cmake", line)


class TestTheDefaultIsNotAFallback(unittest.TestCase):
    def test_every_driver_states_its_own_or_inherits_linux_on_purpose(self):
        """Target's default is the container's answer, the same way `src`'s
        is. The three drivers that can be something else say so, and a fourth
        added without one is caught here rather than by a build."""
        for cls, expected in ((targets.Vm, True), (targets.LocalWorkspace, True),
                              (targets.Remote, True), (targets.Container, False)):
            with self.subTest(driver=cls.__name__):
                self.assertEqual("os" in cls.__dict__, expected)


class _Target:
    def __init__(self, os_, store):
        self._os = os_
        self.store = Store({"WK_STORE": store})
        self.env = {"WK_STORE": store}

    def os(self):
        return self._os


class TestTheDefaultConfigIsDerived(unittest.TestCase):
    """Registry.default_config: the config a command uses when none is given
    is the last build's, from its task record, and otherwise the target
    platform's. Neither is recorded anywhere else: the workspace marker names
    the workspace, not a build."""

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

    def test_the_last_build_wins_and_says_so(self):
        config, err = self._default("mac-debug", "macos")
        self.assertEqual(config, "mac-debug")
        self.assertIn("last built with", err)

    def test_a_macos_target_with_no_build_defaults_to_the_apple_port(self):
        config, _ = self._default("", "macos")
        self.assertEqual(config, "mac-release")

    def test_a_linux_target_with_no_build_defaults_to_jsc(self):
        config, _ = self._default("", "linux")
        self.assertEqual(config, "jsc-release")

    def test_no_marker_records_a_config(self):
        """The three writers of a workspace marker (Vm.write_marker,
        container/firstrun.sh, tests/support.py) name the workspace only."""
        writers = {"Vm.write_marker": inspect.getsource(targets.Vm.write_marker),
                   "container/firstrun.sh": (REPO / "container/firstrun.sh").read_text(),
                   "tests/support.py": (REPO / "tests/support.py").read_text()}
        for name, text in writers.items():
            with self.subTest(writer=name):
                self.assertNotIn("config=", text)


if __name__ == "__main__":
    unittest.main()
