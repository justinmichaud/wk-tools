"""The tailnet name a system `wk sysimage write` seeds onto a card is the
board's MACH_BENCH_SSH (`<board>-bench`), so a workstation that is also a
fleet device (rpi5) keeps its own name for its own install and needs no
special case anywhere -- boot/machines.sh, cmd/sysimage (_tailnet_name_for).

Run: python3 -m unittest tests.test_tailnet_name -v
"""
import re
import subprocess
import unittest

from tests.support import REPO, WkTest, bash

MACHINES = REPO / "boot" / "machines"


def _lift(path, func):
    return subprocess.run(["sed", "-n", f"/^{func}()/,/^}}/p", str(path)],
                          capture_output=True, text=True).stdout


class TestBenchName(WkTest):
    def _name_for(self, machine):
        cp = bash(f'''
. "{REPO}/lib/common.sh"; . "{REPO}/boot/machines.sh"
{_lift(REPO / "cmd" / "sysimage", "_tailnet_name_for")}
_tailnet_name_for {machine!r}
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.strip()

    def test_every_pi_card_joins_as_board_bench(self):
        """rpi3, rpi4 and rpi5 cards join as <board>-bench"""
        for board in ("rpi3", "rpi4", "rpi5"):
            with self.subTest(board=board):
                self.assertEqual(self._name_for(board), f"{board}-bench")

    def test_the_rpi5_workstation_keeps_its_own_name(self):
        """the rpi5's own install stays `rpi5`: only the stick is renamed"""
        cp = bash(f'. "{REPO}/lib/common.sh"; . "{REPO}/boot/machines.sh"; machine_load rpi5; echo "$MACH_SSH"')
        self.assertEqual(cp.stdout.strip(), "rpi5", cp.stdout + cp.stderr)

    def test_a_bench_device_answers_by_its_bench_name(self):
        """a board whose every system wk wrote is reached by the bench name in both roles"""
        for board in ("rpi3", "rpi4"):
            with self.subTest(board=board):
                cp = bash(f'. "{REPO}/lib/common.sh"; . "{REPO}/boot/machines.sh"; machine_load {board}; echo "$MACH_ROLE $MACH_SSH $MACH_BENCH_SSH"')
                self.assertEqual(cp.stdout.split(), ["bench-device", f"{board}-bench", f"{board}-bench"], cp.stdout + cp.stderr)

    def test_no_machine_is_seeded_with_its_own_install_name(self):
        """_tailnet_name_for reads MACH_BENCH_SSH, never MACH_SSH"""
        body = _lift(REPO / "cmd" / "sysimage", "_tailnet_name_for")
        self.assertIn("MACH_BENCH_SSH", body)
        self.assertNotRegex(body, r"\bMACH_SSH\b")

    def test_a_machine_with_no_written_system_has_no_name_to_seed(self):
        """benchvm (a guest, reached through the host) has nothing to seed"""
        self.assertEqual(self._name_for("benchvm"), "")


if __name__ == "__main__":
    unittest.main()
