"""Where tart is, asked once.

tart ships as a signed .app (it needs the virtualization entitlement) and is
reached through ~/.local/bin, which a non-interactive ssh session's PATH does
not carry -- and driving this fleet's Mac from another machine is exactly a
non-interactive ssh session. So every reader of "is tart here" has to give the
same answer as the one that runs it, or `wk vm` refuses a command it could
have run.

Run: python3 -m unittest tests.test_tart_locator -v
"""
import os
import stat
import unittest

from tests.support import REPO, WkTest, bash, scratch_dir


def locate(home, path_dirs=""):
    return bash(
        '. "$WK_ROOT/lib/common.sh"; tart_bin && echo FOUND || echo MISSING',
        env={"HOME": str(home), "PATH": path_dirs or "/usr/bin:/bin"},
    )


def plant(home, rel):
    p = home / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


class TestTheLocator(WkTest):
    def setUp(self):
        self._scratch = scratch_dir()
        self.home = self._scratch.__enter__()

    def tearDown(self):
        self._scratch.__exit__(None, None, None)

    def test_a_bundle_off_the_path_is_found(self):
        """The arrangement ./setup makes and every ssh session sees."""
        plant(self.home, ".local/share/tart/tart.app/Contents/MacOS/tart")
        cp = locate(self.home)
        self.assertIn("FOUND", cp.stdout, cp.stdout + cp.stderr)

    def test_the_symlink_off_the_path_is_found_and_resolved(self):
        real = plant(self.home, ".local/share/tart/tart.app/Contents/MacOS/tart")
        link = self.home / ".local" / "bin" / "tart"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(real)
        cp = locate(self.home)
        self.assertIn("FOUND", cp.stdout)
        self.assertIn(str(real), cp.stdout, "the .app binary itself is what runs")

    def test_a_machine_without_tart_says_so(self):
        cp = locate(self.home)
        self.assertIn("MISSING", cp.stdout, cp.stdout + cp.stderr)

    def test_one_on_the_path_wins(self):
        onpath = plant(self.home, "bin/tart")
        cp = locate(self.home, path_dirs=f"{self.home}/bin:/usr/bin:/bin")
        self.assertIn(str(onpath), cp.stdout)


class TestEveryReaderAsksIt(WkTest):
    """A second spelling of "is tart here" is a command that refuses work it
    could do: the dispatcher's gate ran `command -v` while the vm target ran
    the bundle, so `wk vm start` over ssh was refused with tart installed."""

    READERS = ("wk", "targets/vm.sh", "boot/mac-guest.sh",
               "host/macos/tools.sh", "host/macos/softnet.sh")

    def test_no_reader_spells_it_for_itself(self):
        for rel in self.READERS:
            text = (REPO / rel).read_text()
            self.assertNotIn('-x "$HOME/.local/bin/tart"', text, rel)
            self.assertNotIn("command -v tart", text, rel)
            self.assertIn("tart_bin", text, rel)

    def test_the_dispatcher_gate_is_the_locator(self):
        """`needs tart` on cmd/vm is what refuses; it must not fall to the
        generic `command -v` arm."""
        from tests.support import func_body
        body = func_body((REPO / "wk").read_text(), "check_needs")
        self.assertIn("tart_bin", body)


if __name__ == "__main__":
    unittest.main()
