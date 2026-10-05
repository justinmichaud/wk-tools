"""`wk new` makes a home mountpoint (the mirror's, where the store is under $HOME) as the home's owner before podman
makes it as root (`Container._ensure_home_mountpoint`), against scratch directories."""
import os
import sys
import unittest

from tests.support import REPO, WkTest

sys.path.insert(0, str(REPO / "lib"))
from wk import places  # noqa: E402
from wk.machine import Local  # noqa: E402


def _ensure(ws, dest, user="wsuser"):
    c = places.Container("container", str(REPO), dict(os.environ, WK_CONTAINER_USER=user), Local())
    c._ensure_home_mountpoint(str(ws), str(dest))


class TestTheMountpointsInsideTheHomeAreMadeHere(WkTest):
    HOME = "/home/wsuser"

    def setUp(self):
        super().setUp()
        self.ws = self.tmp / "ws"
        (self.ws / "home").mkdir(parents=True)

    def test_a_destination_under_the_home_is_made_in_the_workspaces_home(self):
        _ensure(self.ws, f"{self.HOME}/.local/share/wk/git")
        made = self.ws / "home" / ".local" / "share" / "wk" / "git"
        self.assertTrue(made.is_dir(), "podman is left to make the mountpoint, as root")
        self.assertEqual((self.ws / "home" / ".local").owner(),
                         (self.ws / "home").owner(),
                         "the mountpoint's parents are not the home's owner's")

    def test_a_destination_outside_the_home_is_left_to_podman(self):
        for dest in ("/var/lib/wk/git", "/ccache", "/opt/wk-tools", "/home/someone-else/git"):
            with self.subTest(dest=dest):
                _ensure(self.ws, dest)
        self.assertEqual(sorted(p.name for p in (self.ws / "home").iterdir()), [])

    def test_the_home_itself_is_not_a_destination_it_acts_on(self):
        _ensure(self.ws, self.HOME)
        self.assertEqual(sorted(p.name for p in (self.ws / "home").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
