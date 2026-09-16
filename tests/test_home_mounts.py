"""A workspace user owns their home, mountpoints included
(_ensure_home_mountpoint, targets/container.sh).

A container workspace's home is a directory on the machine, mounted in at
/home/<user>, and the machine's mirror is mounted at the machine's own path so
a `--shared` snapshot's alternates resolve on both sides (tests/test_mirror_path
.py). Where the store is under $HOME -- the default on a workstation with no
writable /var/lib/wk -- that path is *inside* the home: the mirror's mountpoint
is a directory in the workspace's own home.

podman makes a missing mount destination as container root, so left to podman
that is a root-owned ~/.local the workspace user cannot write: no ~/.local/bin,
so `claude install` fails and no session in the workspace can run at all (what
`wk verify` reports as "no 'claude' on $PATH").

So `wk new` makes any home mountpoint itself, before podman can, as the user
whose home it is.

Hermetic: the driver is sourced and the one function called against scratch
directories. No container, no podman, no network.

Run: python3 -m unittest tests.test_home_mounts -v
"""
import unittest

from tests.support import REPO, WkTest, bash


def _ensure(ws, dest, user="wsuser"):
    cp = bash(f'''
set -euo pipefail
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/resources.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target container >/dev/null 2>&1
_ensure_home_mountpoint {str(ws)!r} {str(dest)!r}
''', env={"WK_CONTAINER_USER": user})
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return cp


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


class TestWkNewMakesThemRatherThanPodman(unittest.TestCase):
    def test_the_mirror_mountpoint_goes_through_it(self):
        """The mount and the mountpoint are one decision: a driver that mounts
        the mirror at the machine's own path asks for that path in the home."""
        text = (REPO / "targets" / "container.sh").read_text()
        self.assertIn('_ensure_home_mountpoint "$ws" "$(dirname "$(wk_mirror)")"', text,
                      "t_create leaves the mirror's mountpoint to podman")
        self.assertLess(text.index('_ensure_home_mountpoint "$ws"'),
                        text.index('--volume $(dirname "$(wk_mirror)")'),
                        "the mountpoint is made after the container is created")


if __name__ == "__main__":
    unittest.main()
