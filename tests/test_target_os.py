"""t_os (lib/target.sh): the platform a build in a target runs on, `linux` or
`macos`. It is not decoration -- build/configs.sh reads it to decide a
config's build system, because Xcode is the only one on macOS -- so every
driver has to answer it, and answer it from evidence rather than from a name.

Run: python3 -m unittest tests.test_target_os -v
"""
import platform
import unittest

from tests.support import REPO, bash, fake_workspace


def _t_os(target):
    cp = bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/target.sh"
load_target {target} >/dev/null 2>&1
t_os
''')
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return cp.stdout.strip()


class TestEveryDriverAnswers(unittest.TestCase):
    def test_the_container_is_linux(self):
        """The SDK image is Fedora, whatever the workstation holding it is."""
        self.assertEqual(_t_os("container"), "linux")

    def test_a_macos_guest_is_macos(self):
        """targets/vm.sh exists to build the Apple ports; there is no other
        kind of guest it makes."""
        self.assertEqual(_t_os("vm"), "macos")

    def test_a_workspace_answers_for_itself(self):
        """targets/local.sh: inside a workspace `uname` is the truth -- a
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
        """lib/target.sh's default is the container's answer, the same way
        t_src's is. The three drivers that can be something else say so, and a
        fourth added without one is caught here rather than by a build."""
        for driver, expected in (("vm.sh", True), ("local.sh", True),
                                 ("remote.sh", True), ("container.sh", False)):
            src = (REPO / "targets" / driver).read_text()
            with self.subTest(driver=driver):
                self.assertEqual("t_os()" in src, expected)


if __name__ == "__main__":
    unittest.main()
