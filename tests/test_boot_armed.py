"""Driver.armed_barrier (lib/wk/boot/driver.py): the one check that stops a mutating command
from racing a machine between `wk boot <machine>`, which leaves an arming record on it, and the reboot that record
waits for -- driven with a stubbed probe, record and boot id.

Run: python3 tests/run.py --unit -k test_boot_armed
"""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))

from wk import act  # noqa: E402
from wk.boot.driver import Driver  # noqa: E402


class TestMachineArmedBarrier(unittest.TestCase):
    def barrier(self, mode, record, boot_id, what="this command", force=False):
        d = Driver(REPO, {"name": "testmach"}, None)
        d.probe = lambda: mode
        d.record_read = lambda: "\n".join(record)
        d.boot_id = lambda: boot_id
        env = {"WK_FORCE": "1"} if force else {}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                d.armed_barrier(what)
                return 0, err.getvalue()
            except act.Refused as e:
                return e.status, err.getvalue()
            finally:
                act._forced.clear()

    def test_unarmed_machine_passes(self):
        self.assertEqual(self.barrier("host", [], "boot-1")[0], 0)

    def test_armed_and_not_yet_rebooted_refuses_with_remedy(self):
        rc, out = self.barrier("host", ["image=demo-system", "armed_boot_id=boot-1"], "boot-1",
                               what="Doing the thing now would race the reboot.")
        self.assertNotEqual(rc, 0, out)
        for want in ("testmach", "demo-system", "Doing the thing now would race the reboot.",
                     "wk boot testmach --disarm", "wk boot testmach --status"):
            self.assertIn(want, out)

    def test_spent_arming_passes(self):
        """a different boot id: the one-shot is consumed, so there is nothing left to race."""
        self.assertEqual(self.barrier("host", ["image=demo-system", "armed_boot_id=boot-old"], "boot-new")[0], 0)

    def test_not_in_host_mode_passes(self):
        """a machine answering as its bench system is not about to leave host mode: it already has."""
        self.assertEqual(self.barrier("bench demo-system", ["image=demo-system", "armed_boot_id=boot-1"], "boot-1")[0], 0)

    def test_a_board_that_could_not_be_probed_is_not_read_as_unarmed(self):
        rc, out = self.barrier("unreachable", [], "boot-1", what="Doing the thing now would race the reboot.")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("could not tell what testmach is running", out)
        self.assertIn("wk boot testmach --status", out)

    def test_force_crosses_either_barrier_with_a_warning(self):
        for mode, record in (("unreachable", []), ("host", ["image=demo-system", "armed_boot_id=boot-1"])):
            with self.subTest(mode=mode):
                rc, out = self.barrier(mode, record, "boot-1", force=True)
                self.assertEqual(rc, 0, out)
                self.assertIn("FORCED past a barrier", out)


if __name__ == "__main__":
    unittest.main()
