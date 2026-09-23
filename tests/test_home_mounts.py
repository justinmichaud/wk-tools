"""A workspace user owns their home, mountpoints included
(`Container._ensure_home_mountpoint`, lib/wk/targets.py).

A container workspace's home is a directory on the machine, mounted in at
/home/<user>, and the machine's mirror is mounted at the machine's own path so
a `--shared` snapshot's alternates resolve on both sides (tests/test_mirror_path
.py). Where the store is under $HOME -- the default on a workstation with no
writable /var/lib/wk -- that path is *inside* the home: the mirror's mountpoint
is a directory in the workspace's own home.

podman makes a missing mount destination as container root, so left to podman
that is a root-owned ~/.local the workspace user cannot write: no ~/.local/bin,
so `claude install` fails and no session in the workspace can run at all (what
`wk doctor` reports as "no 'claude' on $PATH").

So `wk new` makes any home mountpoint itself, before podman can, as the user
whose home it is.

Hermetic: the one method is called against scratch directories. No
container, no podman, no network.

Run: python3 -m unittest tests.test_home_mounts -v
"""
import os
import sys
import unittest

from tests.support import REPO, WkTest

sys.path.insert(0, str(REPO / "lib"))
from wk import targets  # noqa: E402
from wk.machine import Local  # noqa: E402


def _ensure(ws, dest, user="wsuser"):
    c = targets.Container("container", str(REPO), dict(os.environ, WK_CONTAINER_USER=user), Local())
    c._ensure_home_mountpoint(str(ws), str(dest))


class TestTheMountpointsInsideTheHomeAreMadeHere(WkTest):
    HOME = "/home/wsuser"

    def setUp(self):
        super().setUp()
        self.ws = self.tmp / "ws"
        (self.ws / "home").mkdir(parents=True)

    def test_a_destination_under_the_home_is_made_in_the_workspaces_home(self):
        """The real case: this machine's store is under $HOME, so the mirror
        mounts at /home/<user>/.local/share/wk/git, and podman would make
        .local as root on the way to it."""
        _ensure(self.ws, f"{self.HOME}/.local/share/wk/git")
        made = self.ws / "home" / ".local" / "share" / "wk" / "git"
        self.assertTrue(made.is_dir(), "podman is left to make the mountpoint, as root")
        self.assertEqual((self.ws / "home" / ".local").owner(),
                         (self.ws / "home").owner(),
                         "the mountpoint's parents are not the home's owner's")

    def test_the_user_can_write_every_directory_it_made(self):
        """What the root-owned copy costs: `claude install` writes
        ~/.local/bin, and npm writes ~/.local/lib for `wk ai pi`."""
        _ensure(self.ws, f"{self.HOME}/.local/share/wk/git")
        (self.ws / "home" / ".local" / "bin").mkdir()
        self.assertTrue((self.ws / "home" / ".local" / "bin").is_dir())

    def test_a_destination_outside_the_home_is_left_to_podman(self):
        """A machine with a writable /var/lib/wk mounts the mirror there, and
        every other mount of a container workspace -- /src/WebKit, /ccache,
        /secrets, /run/wk -- is outside the home too. Making a directory for
        one of those under the home would be a directory that is never a
        mountpoint, in the user's home, for ever."""
        for dest in ("/var/lib/wk/git", "/ccache", "/opt/wk-tools", "/home/someone-else/git"):
            with self.subTest(dest=dest):
                _ensure(self.ws, dest)
        self.assertEqual(sorted(p.name for p in (self.ws / "home").iterdir()), [])

    def test_the_home_itself_is_not_a_destination_it_acts_on(self):
        """$ws/home is made by `wk new` and mounted at the home: the arm that
        matches has a path under it, so the home is not re-derived here."""
        _ensure(self.ws, self.HOME)
        self.assertEqual(sorted(p.name for p in (self.ws / "home").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
