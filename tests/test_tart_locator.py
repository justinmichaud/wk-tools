"""Where tart is: a signed .app linked from ~/.local/bin, which a non-interactive ssh's PATH lacks."""
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


if __name__ == "__main__":
    unittest.main()
