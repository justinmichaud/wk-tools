"""The tailnet name a system `wk sysimage write` seeds onto a card is the
board's, by role: a bench system joins as bench_ssh (`<board>-bench`), a
rescue as ssh -- `<board>-rescue` on a bench device, the workstation's
own name on a workstation (rpi5), whose own install is never written. Two
names because each written system is its own tailnet node, and a second
join under a name already on the tailnet comes up renamed --
lib/wk/sysimage/write.py (tailnet_name).

Run: python3 -m unittest tests.test_tailnet_name -v
"""
import sys
import unittest

from tests.support import FLEET_ENV, REPO, WkTest

sys.path.insert(0, str(REPO / "lib"))
from wk import fleet  # noqa: E402
from wk.sysimage import write  # noqa: E402


class TestBenchName(WkTest):
    def _conf(self, machine):
        return fleet.Fleet(REPO, FLEET_ENV).load(machine)

    def _name_for(self, machine, role="bench"):
        return write.tailnet_name(fleet.Fleet(REPO, FLEET_ENV), machine, role)

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
        self.assertEqual(self._conf("rpi5")["ssh"], "rpi5")

    def test_a_bench_device_declares_both_names(self):
        for board in ("rpi3", "rpi4"):
            with self.subTest(board=board):
                c = self._conf(board)
                self.assertEqual([c["role"], c["ssh"], c["bench_ssh"]],
                                 ["bench-device", f"{board}-rescue", f"{board}-bench"])

    def test_a_machine_with_no_written_system_has_no_name_to_seed(self):
        """benchvm (a guest, reached through the host) has nothing to seed"""
        self.assertEqual(self._name_for("benchvm"), "")


if __name__ == "__main__":
    unittest.main()
