"""`wk boot --status`'s quiet siblings (lib/wk/boot/cli.py's Boot.quiet_siblings): a board is unreachable for two
very different reasons -- the board, or the thing that carries it -- and when every fleet device sharing one network
is quiet at once, the network is the suspect.

The tailnet view is given, so these run with no tailnet and no boards.

Run: python3 -m unittest tests.test_quiet_siblings -v
"""
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "lib"))

from wk.boot import cli  # noqa: E402


class TestQuietSiblings(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="wk-test-quiet-"))
        self.addCleanup(shutil.rmtree, str(self.dir), True)

    def _machine(self, name, net="wifi", bridge="", bench=True):
        (self.dir / f"{name}.conf").write_text(
            f'NODE_SSH="{name}-rescue"\n'
            + (f'NODE_BENCH_SSH="{name}-bench"\n' if bench else "")
            + f'NODE_NET={net}\nNODE_BRIDGE="{bridge}"\n'
            'KIND=board\nNODE_DRIVER=pi-sd\nNODE_DEVICE=/dev/mmcblk0\nNODE_ROOT=/dev/mmcblk0p2\n'
            f'NODE_NOTE="{name}, a test fixture"\n'
        )

    def _run(self, me, peers):
        """peers: (name, up?) as the tailnet reports them."""
        env = {"WK_MACHINES_DIR": str(self.dir), "HOME": "/nonexistent"}
        rows = [(n, "100.0.0.1", "up" if u else "down") for n, u in peers]
        b = cli.Boot(REPO, cli.load_conf(REPO, me, env), None, env=env, peers=types.SimpleNamespace(peers=lambda: rows))
        return b.quiet_siblings()

    def test_every_sibling_quiet_is_reported_as_all_of_all(self):
        for n in ("rpi3", "rpi4", "rpi5"):
            self._machine(n)
        got = self._run("rpi4", [("rpi3-rescue", False), ("rpi3-bench", False), ("rpi5-rescue", False), ("rpi5-bench", False)])
        self.assertEqual(got, (2, 2))

    def test_a_sibling_that_answers_clears_the_network(self):
        for n in ("rpi3", "rpi4", "rpi5"):
            self._machine(n)
        got = self._run("rpi4", [("rpi3-rescue", False), ("rpi3-bench", False), ("rpi5-rescue", True), ("rpi5-bench", False)])
        self.assertEqual(got, (1, 2))

    def test_either_of_a_boards_two_names_counts_as_up(self):
        self._machine("rpi3")
        self._machine("rpi4")
        self.assertEqual(self._run("rpi4", [("rpi3-bench", True)]), (0, 1))

    def test_only_devices_on_the_same_network_are_siblings(self):
        self._machine("rpi3", net="wifi")
        self._machine("rpi4", net="wifi")
        self._machine("moose", net="cable")
        got = self._run("rpi4", [("rpi3-rescue", False), ("rpi3-bench", False), ("moose-rescue", False)])
        self.assertEqual(got, (1, 1))

    def test_a_board_behind_a_different_bridge_is_not_a_sibling(self):
        self._machine("rpi3", net="wifi", bridge="phone-a")
        self._machine("rpi4", net="wifi", bridge="phone-b")
        self.assertEqual(self._run("rpi4", [("rpi3-rescue", False)]), (0, 0))

    def test_no_tailnet_view_claims_nothing(self):
        self._machine("rpi3")
        self._machine("rpi4")
        self.assertEqual(self._run("rpi4", []), (0, 0))


if __name__ == "__main__":
    unittest.main()
