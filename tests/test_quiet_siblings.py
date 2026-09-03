"""`machine_quiet_siblings` (boot/machines.sh) and the line `wk boot --status`
draws from it: a board is unreachable for two very different reasons -- the
board, or the thing that carries it -- and when every fleet device sharing
one network is quiet at once, the network is the suspect.

`wk boot --status` already said this for a board behind a bridge. A board on
wifi has the identical failure and said nothing, so an access point going
down read exactly like a dead board (measured 2026-08-31: three boards, one
untouched for hours, all quiet together).

The tailnet view is stubbed, so these run with no tailnet and no boards.

Run: python3 -m unittest tests.test_quiet_siblings -v
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def bash(script, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e)


class TestQuietSiblings(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="wk-test-quiet-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.dir)]))

    def _machine(self, name, net="wifi", bridge="", bench=True):
        (self.dir / f"{name}.conf").write_text(
            f'NODE_SSH="{name}-rescue"\n'
            + (f'NODE_BENCH_SSH="{name}-bench"\n' if bench else "")
            + f'NODE_NET={net}\nNODE_BRIDGE="{bridge}"\n'
            'NODE_DRIVER=pi-sd\nNODE_DEVICE=/dev/mmcblk0\nNODE_ROOT=/dev/mmcblk0p2\n'
            # machine_load requires a note; without one the conf is refused
            # and the walk silently finds no siblings at all.
            f'NODE_NOTE="{name}, a test fixture"\n'
        )

    def _run(self, me, net, bridge, peers):
        """peers: list of (name, up?) the tailnet reports."""
        rows = "\\n".join(f"{n}\\t100.0.0.1\\t{'up' if u else 'down'}" for n, u in peers)
        return bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/boot/machines.sh"
wk_tailscale_peers() {{ printf '{rows}\\n'; }}
machine_quiet_siblings "{me}" "{net}" "{bridge}"
''', env={"WK_MACHINES_DIR": str(self.dir)})

    def test_every_sibling_quiet_is_reported_as_all_of_all(self):
        for n in ("rpi3", "rpi4", "rpi5"):
            self._machine(n)
        cp = self._run("rpi4", "wifi", "", [("rpi3-rescue", False), ("rpi3-bench", False),
                                            ("rpi5-rescue", False), ("rpi5-bench", False)])
        self.assertEqual(cp.stdout.strip(), "2 2", cp.stdout + cp.stderr)

    def test_a_sibling_that_answers_clears_the_network(self):
        """one board up means the network carries traffic, so the quiet one is
        the board's own problem -- the line must not fire."""
        for n in ("rpi3", "rpi4", "rpi5"):
            self._machine(n)
        cp = self._run("rpi4", "wifi", "", [("rpi3-rescue", False), ("rpi3-bench", False),
                                            ("rpi5-rescue", True), ("rpi5-bench", False)])
        self.assertEqual(cp.stdout.strip(), "1 2", cp.stdout + cp.stderr)

    def test_either_of_a_boards_two_names_counts_as_up(self):
        """a board answers as its rescue or as its bench system; either is the
        board being reachable."""
        self._machine("rpi3")
        self._machine("rpi4")
        cp = self._run("rpi4", "wifi", "", [("rpi3-bench", True)])
        self.assertEqual(cp.stdout.strip(), "0 1", cp.stdout + cp.stderr)

    def test_only_devices_on_the_same_network_are_siblings(self):
        """a cabled board says nothing about a wifi board's access point."""
        self._machine("rpi3", net="wifi")
        self._machine("rpi4", net="wifi")
        self._machine("moose", net="cable")
        cp = self._run("rpi4", "wifi", "", [("rpi3-rescue", False), ("rpi3-bench", False),
                                            ("moose-rescue", False)])
        self.assertEqual(cp.stdout.strip(), "1 1", cp.stdout + cp.stderr)

    def test_a_board_behind_a_different_bridge_is_not_a_sibling(self):
        self._machine("rpi3", net="wifi", bridge="phone-a")
        self._machine("rpi4", net="wifi", bridge="phone-b")
        cp = self._run("rpi4", "wifi", "phone-b", [("rpi3-rescue", False)])
        self.assertEqual(cp.stdout.strip(), "0 0", cp.stdout + cp.stderr)

    def test_no_tailnet_view_claims_nothing(self):
        """a machine that cannot see the tailnet knows nothing about the
        siblings, and must not report them quiet."""
        self._machine("rpi3")
        self._machine("rpi4")
        cp = bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/boot/machines.sh"
wk_tailscale_peers() {{ printf ''; }}
machine_quiet_siblings rpi4 wifi ""
''', env={"WK_MACHINES_DIR": str(self.dir)})
        self.assertEqual(cp.stdout.strip(), "0 0", cp.stdout + cp.stderr)

    def test_the_caller_does_not_lose_its_own_machine(self):
        """machine_load overwrites NODE_* as it walks, so the helper runs in a
        subshell; a caller that lost its own values would then report about
        the wrong board."""
        self._machine("rpi3")
        self._machine("rpi4")
        cp = bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/boot/machines.sh"
wk_tailscale_peers() {{ printf 'rpi3-rescue\\t100.0.0.1\\tdown\\n'; }}
NODE_SSH=rpi4-rescue
machine_quiet_siblings rpi4 wifi "" >/dev/null
echo "$NODE_SSH"
''', env={"WK_MACHINES_DIR": str(self.dir)})
        self.assertEqual(cp.stdout.strip(), "rpi4-rescue", cp.stdout + cp.stderr)


class TestWiring(unittest.TestCase):
    def test_status_draws_the_line_only_when_every_sibling_is_quiet(self):
        text = (REPO / "cmd" / "boot").read_text()
        self.assertIn("machine_quiet_siblings", text)
        self.assertIn("rule the network out before the board", text)

    def test_cmd_boot_can_see_the_tailnet(self):
        """the helper reads wk_tailscale_peers; cmd/boot has to source it."""
        self.assertIn('. "$WK_ROOT/lib/reach.sh"', (REPO / "cmd" / "boot").read_text())


if __name__ == "__main__":
    unittest.main()
