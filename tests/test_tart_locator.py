"""Where tart is, asked once.

tart ships as a signed .app (it needs the virtualization entitlement) and is
reached through ~/.local/bin, which a non-interactive ssh session's PATH does
not carry -- and driving this fleet's Mac from another machine is exactly a
non-interactive ssh session. So every reader of "is tart here" has to give the
same answer as the one that runs it, or a guest command refuses what it could
have run.

Run: python3 -m unittest tests.test_tart_locator -v
"""
import os
import stat
import unittest

from tests.support import REPO, WkTest, scratch_dir


def plant(home, rel):
    p = home / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


class TestThePythonLocator(WkTest):
    def setUp(self):
        self._scratch = scratch_dir()
        self.home = self._scratch.__enter__()
        import sys
        sys.path.insert(0, str(REPO / "lib"))
        from wk import targets
        self.targets = targets

    def tearDown(self):
        self._scratch.__exit__(None, None, None)

    def test_the_link_setup_makes_off_the_path_is_found_and_resolved_to_the_bundle(self):
        real = plant(self.home, ".local/share/tart/tart.app/Contents/MacOS/tart")
        link = self.home / ".local" / "bin" / "tart"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(real)
        self.assertEqual(self.targets.tart_path({"HOME": str(self.home), "PATH": "/usr/bin:/bin"}), os.path.realpath(real))

    def test_one_on_the_path_wins(self):
        onpath = plant(self.home, "bin/tart")
        plant(self.home, ".local/bin/tart")
        self.assertEqual(self.targets.tart_path({"HOME": str(self.home), "PATH": str(self.home / "bin")}), os.path.realpath(onpath))

    def test_a_bundle_nothing_links_is_not_guessed_at_and_the_refusal_names_the_link(self):
        plant(self.home, ".local/share/tart/tart.app/Contents/MacOS/tart")
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}
        self.assertIsNone(self.targets.tart_path(env))
        import contextlib
        import io
        from wk.act import Refused
        from wk.machine import Fake
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(Refused):
            self.targets.Vm("vm", str(REPO), env, Fake()).tart_or_die()
        self.assertIn("ln -sfn ~/.local/share/tart/tart.app/Contents/MacOS/tart ~/.local/bin/tart", err.getvalue())


class TestEveryReaderAsksIt(WkTest):
    """A second spelling of "is tart here" is a command that refuses work it
    could do: a `command -v` refuses a guest start over ssh with tart installed."""

    SHELL_READERS = ("host/macos/tools.sh", "host/macos/softnet.sh")

    def test_no_reader_spells_it_for_itself(self):
        for rel in ("lib/wk/targets.py",) + self.SHELL_READERS:
            text = (REPO / rel).read_text()
            self.assertNotIn('-x "$HOME/.local/bin/tart"', text, rel)
            self.assertNotIn("command -v tart", text, rel)
            self.assertNotIn('".local", "bin", "tart"', text, rel)
            self.assertNotIn('which("tart")', text, rel)
        for rel in self.SHELL_READERS:
            text = (REPO / rel).read_text()
            self.assertIn("wk.targets tart", text, rel)


if __name__ == "__main__":
    unittest.main()
