"""shell/path.sh: what a wk shell has on PATH, and the bin/ directory it exposes."""
import os
import shutil
import subprocess
import sys
import unittest

from tests.support import FLEET_ENV, REPO, WkTest

sys.path.insert(0, str(REPO / "lib"))
from wk import fleet  # noqa: E402


def path_from(rc, home):
    """The PATH a non-interactive shell ends up with after sourcing `rc` from a stripped environment."""
    cp = subprocess.run(
        ["bash", "-c", f'. "{rc}" && printf %s "$PATH"'],
        cwd=str(REPO),
        env={"HOME": home, "PATH": "/usr/bin:/bin"},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
    )
    assert cp.returncode == 0, cp.stdout
    return cp.stdout.split(":")


class TestBinDir(WkTest):
    def test_bin_holds_only_a_relative_wk_link(self):
        self.assertEqual(sorted(p.name for p in (REPO / "bin").iterdir()), ["wk"])
        self.assertEqual(os.readlink(REPO / "bin" / "wk"), "../wk")

    def test_an_overlay_root_of_symlinks_stays_its_own_root(self):
        """A WK_ROOT built out of symlinks to another checkout answers from its own registry."""
        root = self.tmp / "overlay"
        (root / "machines").mkdir(parents=True)
        for entry in REPO.iterdir():
            if entry.name in ("machines", ".git", "__pycache__"):
                continue
            (root / entry.name).symlink_to(entry)
        (root / "machines" / "overlaybox.conf").write_text(
            "kind=build\ndriver=remote\nhost=overlaybox\nroot=/tmp/x\n")

        env = {"HOME": str(self.tmp), "PATH": "/usr/bin:/bin"}
        cp = subprocess.run([str(root / "wk"), "key", "push", "status", "--on", "overlaybox"],
                            cwd="/", env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, timeout=60)
        self.assertNotIn("unknown place", cp.stdout)
        real = fleet.Fleet(REPO, FLEET_ENV).names(fleet.PLACE_KINDS)
        cp = subprocess.run([str(root / "wk"), "key", "push", "status", "--on", "nosuchbox"],
                            cwd="/", env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, timeout=60)
        self.assertIn("overlaybox", cp.stdout)
        for n in real:
            self.assertNotIn(n, cp.stdout)

    def test_wk_finds_its_root_through_the_symlink(self):
        cp = subprocess.run(
            [str(REPO / "bin" / "wk"), "doctor", "--probe-tools"],
            cwd="/", env={"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin"},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
        )
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("sha=", cp.stdout)


class TestPath(WkTest):
    def setUp(self):
        super().setUp()
        self.home = str(self.tmp)
        os.makedirs(os.path.join(self.home, ".local", "bin"))

    def test_bashrc_puts_bin_and_local_bin_on_path(self):
        got = path_from(REPO / "shell" / "bashrc", self.home)
        for want in (str(REPO / "bin"), str(REPO / "container" / "bin"),
                     os.path.join(self.home, ".local", "bin")):
            self.assertIn(want, got)

    def test_the_checkout_root_never_goes_on_path(self):
        self.assertNotIn(str(REPO), path_from(REPO / "shell" / "bashrc", self.home))

    def test_path_is_added_once(self):
        cp = subprocess.run(
            ["bash", "-c", f'. "{REPO}/shell/bashrc" && . "{REPO}/shell/bashrc" && printf %s "$PATH"'],
            cwd=str(REPO), env={"HOME": self.home, "PATH": "/usr/bin:/bin"},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
        )
        self.assertEqual(cp.returncode, 0, cp.stdout)
        entries = cp.stdout.split(":")
        self.assertEqual(entries.count(str(REPO / "bin")), 1, cp.stdout)

    def test_an_inherited_entry_moves_ahead_of_usr_bin_so_ninja_is_the_walls(self):
        binp = str(REPO / "container" / "bin")
        cp = subprocess.run(["bash", "-c", f'. "{REPO}/shell/bashrc" && printf "%s\\n" "$PATH" && command -v ninja'],
                            cwd=str(REPO), env={"HOME": self.home, "PATH": f"/usr/bin:{binp}:/bin"},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
        path, ninja = cp.stdout.strip().splitlines()[-2:]
        entries = path.split(":")
        self.assertEqual(1, entries.count(binp), cp.stdout)
        self.assertLess(entries.index(binp), entries.index("/usr/bin"), cp.stdout)
        self.assertEqual(binp + "/ninja", ninja)

    def test_the_rest_of_the_path_keeps_its_order(self):
        got = path_from(REPO / "shell" / "bashrc", self.home)
        self.assertLess(got.index("/usr/bin"), got.index("/bin"), got)

    def test_local_bin_absent_is_not_added(self):
        shutil.rmtree(os.path.join(self.home, ".local"))
        got = path_from(REPO / "shell" / "bashrc", self.home)
        self.assertNotIn(os.path.join(self.home, ".local", "bin"), got)

    @unittest.skipIf(shutil.which("zsh") is None, "no zsh on this machine")
    def test_zsh_does_not_exec_a_repo_directory_as_a_command(self):
        got = ":".join(path_from(REPO / "shell" / "bashrc", self.home))
        cp = subprocess.run(
            ["zsh", "-f", "-c", "claude --version"],
            cwd=str(REPO), env={"HOME": self.home, "PATH": got},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
        )
        self.assertNotEqual(cp.returncode, 126, cp.stdout)
        self.assertNotIn("permission denied", cp.stdout.lower())

    def test_the_workspace_only_tools_go_on_path_only_in_a_workspace(self):
        ws = str(REPO / "container" / "bin" / "ws")
        self.assertNotIn(ws, path_from(REPO / "shell" / "bashrc", self.home))
        open(os.path.join(self.home, ".wk-workspace"), "w").close()
        self.assertIn(ws, path_from(REPO / "shell" / "bashrc", self.home))
