"""`machine_armed_barrier` (lib/wk/boot/driver.py's armed_barrier): the one check that stops a mutating command
from racing a machine between `wk boot <machine>`, which leaves an arming record on it, and the reboot that record
waits for -- driven with a stubbed probe, record and boot id; and each call site calls it, in command position.

Run: python3 tests/run.py --unit -k test_boot_armed
"""
import contextlib
import io
import os
import re
import sys
import unittest
from unittest import mock

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))

from wk import act  # noqa: E402
from wk.boot.driver import Driver  # noqa: E402


class TestMachineArmedBarrier(unittest.TestCase):
    def barrier(self, mode, record, boot_id, what="this command", force=False):
        d = Driver(REPO, {"NODE_NAME": "testmach"}, None)
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
        """a barrier may not skip in silence: a board in bench mode answers a workspace, one in host mode does not."""
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


class TestEveryMutatingPathCallsTheBarrier(unittest.TestCase):
    """Static check: `machine_armed_barrier` is called, in command position
    (not just mentioned in a comment), from `wk pi deploy` and
    `wk pi boot-order`. A grep, not an execution: the point is that nobody
    can delete the call and leave the docstring believing it is still there.
    `wk sysimage write` is tests/test_sysimage_write.py's
    test_a_board_armed_for_a_one_shot_boot_is_not_written_under."""

    # A call is a bare invocation or one gated by a `[ ... ] &&`/`if` guard --
    # never inside a `#` comment line.
    _CALL = re.compile(r'(?:^\s*|&&\s*|;\s*)machine_armed_barrier\b')

    @staticmethod
    def _live_lines(text):
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            yield line

    def _calls_in(self, path):
        text = (REPO / path).read_text()
        return [l for l in self._live_lines(text) if self._CALL.search(l)]

    def test_pi_deploy_and_boot_order_each_call_it(self):
        calls = self._calls_in("cmd/pi")
        self.assertGreaterEqual(
            len(calls), 2,
            f"cmd/pi should call machine_armed_barrier from both cmd_deploy and "
            f"cmd_boot_order; found {len(calls)} live call(s): {calls}",
        )

    def test_defined_once_in_boot_machines(self):
        # One implementation per rule (CLAUDE.md): a second definition
        # elsewhere would be a second, driftable copy of the same refusal.
        hits = 0
        for path in ("boot/machines.sh", "lib/sysimage-arms.sh", "cmd/pi", "cmd/boot", "cmd/status"):
            text = (REPO / path).read_text()
            hits += len(re.findall(r'^machine_armed_barrier\s*\(\)\s*\{', text, re.MULTILINE))
        self.assertEqual(hits, 1, "machine_armed_barrier should be defined exactly once")


if __name__ == "__main__":
    unittest.main()
