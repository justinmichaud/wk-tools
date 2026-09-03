"""shell/path.sh -- the one decision about what a wk shell has on PATH, and
the bin/ directory it exposes.

Run: python3 -m unittest tests.test_shell_path -v
"""
import os
import shutil
import subprocess
import unittest

from tests.support import REPO, WkTest


def path_from(rc, home):
    """The PATH a shell ends up with after sourcing `rc`, from a stripped
    environment. Non-interactive, so shell/bashrc's zsh exec is skipped and
    section 6 -- the part under test -- still runs."""
    cp = subprocess.run(
        ["bash", "-c", f'. "{rc}" && printf %s "$PATH"'],
        cwd=str(REPO),
        env={"HOME": home, "PATH": "/usr/bin:/bin"},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
    )
    assert cp.returncode == 0, cp.stdout
    return cp.stdout.split(":")


class TestBinDir(WkTest):
    def test_bin_holds_only_wk(self):
        """bin/ holds exactly one entry, `wk`, so PATH exposes one name"""
        self.assertEqual(sorted(p.name for p in (REPO / "bin").iterdir()), ["wk"])

    def test_bin_wk_is_a_relative_symlink_to_the_dispatcher(self):
        """bin/wk is a relative symlink to ./wk, so any copy of the tree works"""
        link = REPO / "bin" / "wk"
        self.assertTrue(link.is_symlink(), "bin/wk is not a symlink")
        self.assertEqual(os.readlink(link), "../wk")
        self.assertEqual(link.resolve(), (REPO / "wk").resolve())

    def test_an_overlay_root_of_symlinks_stays_its_own_root(self):
        """a WK_ROOT built out of symlinks to another checkout is still the
        root that was meant: its own registry answers, not the real fleet's.
        tests/test_peer.py builds exactly such a root, and resolving `wk`
        through it would silently ask about this machine's real machines."""
        root = self.tmp / "overlay"
        (root / "targets" / "hosts").mkdir(parents=True)
        for entry in REPO.iterdir():
            if entry.name in ("targets", ".git", "__pycache__"):
                continue
            (root / entry.name).symlink_to(entry)
        for sh in (REPO / "targets").glob("*.sh"):
            (root / "targets" / sh.name).symlink_to(sh)
        (root / "targets" / "hosts" / "overlaybox.conf").write_text(
            "WK_TARGET_KIND=remote\nWK_REMOTE_HOST=overlaybox\nWK_REMOTE_ROOT=/tmp/x\n")

        env = {"HOME": str(self.tmp), "PATH": "/usr/bin:/bin"}
        # Known only to the overlay: resolved there, unknown in the real tree.
        cp = subprocess.run([str(root / "wk"), "push", "status", "--target", "overlaybox"],
                            cwd="/", env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, timeout=60)
        self.assertNotIn("unknown target", cp.stdout)
        # And the real machines are not in this root's registry at all.
        real = sorted(f.stem for f in (REPO / "targets" / "hosts").glob("*.conf"))
        cp = subprocess.run([str(root / "wk"), "push", "status", "--target", "nosuchbox"],
                            cwd="/", env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, timeout=60)
        self.assertIn("overlaybox", cp.stdout)
        for n in real:
            self.assertNotIn(n, cp.stdout, f"the real fleet's {n} leaked into the overlay root")

    def test_the_tree_hash_survives_a_root_of_symlinked_directories(self):
        """`wk version --tree` hashes a link by its target text, so a link to
        a directory is not fed to a program that would follow it"""
        root = self.tmp / "overlay2"
        root.mkdir()
        for entry in REPO.iterdir():
            if entry.name in (".git", "__pycache__"):
                continue
            (root / entry.name).symlink_to(entry)
        cp = subprocess.run([str(root / "wk"), "version", "--tree"], cwd="/",
                            env={"HOME": str(self.tmp), "PATH": "/usr/bin:/bin"},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertNotIn("Is a directory", cp.stdout)

    def test_wk_finds_its_root_through_the_symlink(self):
        """`wk` invoked as bin/wk resolves WK_ROOT to the checkout, not bin/"""
        cp = subprocess.run(
            [str(REPO / "bin" / "wk"), "version"],
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
        """sourcing shell/bashrc puts bin/, container/bin and ~/.local/bin on PATH"""
        got = path_from(REPO / "shell" / "bashrc", self.home)
        for want in (str(REPO / "bin"), str(REPO / "container" / "bin"),
                     os.path.join(self.home, ".local", "bin")):
            self.assertIn(want, got)

    def test_the_checkout_root_never_goes_on_path(self):
        """the checkout root stays off PATH: zsh execs a directory it finds
        there, so `claude` would be the claude/ directory, not the CLI"""
        self.assertNotIn(str(REPO), path_from(REPO / "shell" / "bashrc", self.home))

    def test_path_is_added_once(self):
        """sourcing twice does not repeat an entry"""
        cp = subprocess.run(
            ["bash", "-c", f'. "{REPO}/shell/bashrc" && . "{REPO}/shell/bashrc" && printf %s "$PATH"'],
            cwd=str(REPO), env={"HOME": self.home, "PATH": "/usr/bin:/bin"},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
        )
        self.assertEqual(cp.returncode, 0, cp.stdout)
        entries = cp.stdout.split(":")
        self.assertEqual(entries.count(str(REPO / "bin")), 1, cp.stdout)

    def test_an_inherited_entry_is_moved_to_the_front(self):
        """The wall only works while container/bin is *ahead* of /usr/bin. An
        entry already anywhere on PATH used to be left where it was, so a
        shell that inherited it behind /usr/bin reached the real ninja and
        said nothing about it."""
        binp = str(REPO / "container" / "bin")
        cp = subprocess.run(
            ["bash", "-c", f'. "{REPO}/shell/bashrc" && printf %s "$PATH"'],
            cwd=str(REPO),
            env={"HOME": self.home, "PATH": f"/usr/bin:{binp}:/bin"},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
        )
        self.assertEqual(cp.returncode, 0, cp.stdout)
        entries = cp.stdout.split(":")
        self.assertEqual(1, entries.count(binp), cp.stdout)
        self.assertLess(entries.index(binp), entries.index("/usr/bin"), cp.stdout)

    def test_the_wall_answers_from_an_inherited_path(self):
        """The same property said as the thing it protects: which `ninja` a
        shell resolves."""
        cp = subprocess.run(
            ["bash", "-c", f'. "{REPO}/shell/bashrc" && command -v ninja'],
            cwd=str(REPO),
            env={"HOME": self.home,
                 "PATH": f"/usr/bin:{REPO / 'container' / 'bin'}:/bin"},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
        )
        self.assertEqual(str(REPO / "container" / "bin" / "ninja"),
                         cp.stdout.strip().splitlines()[-1], cp.stdout)

    def test_the_rest_of_the_path_keeps_its_order(self):
        """Only the entry being added moves; nothing else is reshuffled."""
        got = path_from(REPO / "shell" / "bashrc", self.home)
        self.assertLess(got.index("/usr/bin"), got.index("/bin"), got)

    def test_local_bin_absent_is_not_added(self):
        """a machine with no ~/.local/bin gets no phantom entry"""
        shutil.rmtree(os.path.join(self.home, ".local"))
        got = path_from(REPO / "shell" / "bashrc", self.home)
        self.assertNotIn(os.path.join(self.home, ".local", "bin"), got)

    @unittest.skipIf(shutil.which("zsh") is None, "no zsh on this machine")
    def test_zsh_does_not_exec_a_repo_directory_as_a_command(self):
        """under this PATH, zsh does not exec claude/ -- the "permission
        denied: claude" a workspace terminal reported"""
        got = ":".join(path_from(REPO / "shell" / "bashrc", self.home))
        cp = subprocess.run(
            ["zsh", "-f", "-c", "claude --version"],
            cwd=str(REPO), env={"HOME": self.home, "PATH": got},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
        )
        self.assertNotEqual(cp.returncode, 126, cp.stdout)
        self.assertNotIn("permission denied", cp.stdout.lower())


class TestOnePathDecision(WkTest):
    def test_only_path_sh_writes_the_path(self):
        """shell/bashrc and the container exec wrapper both defer to
        shell/path.sh rather than keeping a second list"""
        for rel in ("shell/bashrc", "container/proxy/ensure-bridge.sh"):
            text = (REPO / rel).read_text()
            self.assertIn("shell/path.sh", text, f"{rel} does not source shell/path.sh")
            self.assertNotIn('PATH="$HOME/.local/bin:$PATH"', text,
                             f"{rel} sets PATH itself")

    def test_firstrun_does_not_write_a_path_into_the_workspace_rc(self):
        """container/firstrun.sh leaves PATH to the rc it sources"""
        text = (REPO / "container" / "firstrun.sh").read_text()
        self.assertNotIn('export PATH="%s:$HOME/.local/bin:$PATH"', text)
