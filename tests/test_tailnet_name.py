"""The tailnet name a system `wk sysimage write` seeds onto a card is the
board's, by role: a bench system joins as NODE_BENCH_SSH (`<board>-bench`), a
rescue as NODE_SSH -- `<board>-rescue` on a bench device, the workstation's
own name on a workstation (rpi5), whose own install is never written. Two
names because each written system is its own tailnet node, and a second
join under a name already on the tailnet comes up renamed -- boot/machines.sh,
cmd/sysimage (_tailnet_name_for).

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
    def _name_for(self, machine, role="bench"):
        cp = bash(f'''
. "{REPO}/lib/common.sh"; . "{REPO}/boot/machines.sh"
{_lift(REPO / "cmd" / "sysimage", "_tailnet_name_for")}
_tailnet_name_for {machine!r} {role!r}
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.strip()

    def test_every_pi_bench_system_joins_as_board_bench(self):
        """rpi3, rpi4 and rpi5 bench systems join as <board>-bench"""
        for board in ("rpi3", "rpi4", "rpi5"):
            with self.subTest(board=board):
                self.assertEqual(self._name_for(board), f"{board}-bench")
                self.assertEqual(self._name_for(board, "bench"), f"{board}-bench")

    def test_a_bench_devices_rescue_has_its_own_name(self):
        """a rescue written for rpi3/rpi4 joins as <board>-rescue: a different node from the bench system beside it"""
        for board in ("rpi3", "rpi4"):
            with self.subTest(board=board):
                self.assertEqual(self._name_for(board, "rescue"), f"{board}-rescue")
                self.assertNotEqual(self._name_for(board, "rescue"), self._name_for(board, "bench"))

    def test_the_rpi5_workstation_keeps_its_own_name(self):
        """the rpi5's own install stays `rpi5`: only the stick is renamed"""
        cp = bash(f'. "{REPO}/lib/common.sh"; . "{REPO}/boot/machines.sh"; machine_load rpi5; echo "$NODE_SSH"')
        self.assertEqual(cp.stdout.strip(), "rpi5", cp.stdout + cp.stderr)

    def test_a_bench_device_declares_both_names(self):
        for board in ("rpi3", "rpi4"):
            with self.subTest(board=board):
                cp = bash(f'. "{REPO}/lib/common.sh"; . "{REPO}/boot/machines.sh"; machine_load {board}; echo "$NODE_ROLE $NODE_SSH $NODE_BENCH_SSH"')
                self.assertEqual(cp.stdout.split(), ["bench-device", f"{board}-rescue", f"{board}-bench"], cp.stdout + cp.stderr)

    def test_a_machine_with_no_written_system_has_no_name_to_seed(self):
        """benchvm (a guest, reached through the host) has nothing to seed"""
        self.assertEqual(self._name_for("benchvm"), "")


if __name__ == "__main__":
    unittest.main()
