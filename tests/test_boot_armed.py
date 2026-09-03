"""`machine_armed_barrier` (boot/machines.sh): the one check that stops a
mutating command from racing a machine between `wk boot <machine>` (which
leaves an arming record on the machine) and the reboot that record is
waiting for. Driven here with a stubbed `record_read`/`b_probe`/`b_boot_id`
-- no ssh, no hardware -- and, separately, a static check that each of the
three call sites (`wk sysimage write`, `wk pi deploy`, `wk pi boot-order`)
actually calls it, in command position, rather than the check existing but
nothing using it.

Run: python3 -m unittest tests.test_boot_armed -v
"""
import re
import unittest

from tests.support import REPO, WkTest


class TestMachineArmedBarrier(WkTest):
    """Lifts `machine_armed_barrier` out of boot/machines.sh and drives it
    against a fake machine: `b_probe` and `b_boot_id` stand in for the ssh
    probe, `record_read` for the record's own read (both real reads in the
    file this test does not touch)."""

    @staticmethod
    def _sq(text):
        """Quote `text` as one bash single-quoted word (may contain real
        newlines, which single quotes preserve literally)."""
        return "'" + text.replace("'", "'\\''") + "'"

    def _script(self, *, mode, record, boot_id, what="this command"):
        record_text = "\n".join(record)
        return f'''
. "{REPO}/lib/common.sh"
. "{REPO}/boot/machines.sh"
NODE_NAME=testmach
b_probe() {{ MODE={self._sq(mode)}; }}
record_read() {{ printf '%s\\n' {self._sq(record_text)}; }}
b_boot_id() {{ printf '%s' {self._sq(boot_id)}; }}
machine_armed_barrier {self._sq(what)}
echo "exit=$?"
'''

    def test_unarmed_machine_passes(self):
        cp = self.bash(self._script(mode="host", record=[], boot_id="boot-1"))
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("exit=0", cp.stdout, cp.stdout + cp.stderr)

    def test_armed_and_not_yet_rebooted_refuses_with_remedy(self):
        cp = self.bash(self._script(
            mode="host",
            record=["image=demo-system", "armed_boot_id=boot-1"],
            boot_id="boot-1",  # same boot id: not yet spent
            what="Doing the thing now would race the reboot.",
        ))
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        out = cp.stdout + cp.stderr
        self.assertIn("testmach", out, out)
        self.assertIn("demo-system", out, out)
        self.assertIn("Doing the thing now would race the reboot.", out, out)
        self.assertIn("wk boot testmach --disarm", out, out)
        self.assertIn("wk boot testmach --status", out, out)

    def test_spent_arming_passes(self):
        # The machine has rebooted since it was armed (a different boot id):
        # the one-shot is consumed, so there is nothing left to race.
        cp = self.bash(self._script(
            mode="host",
            record=["image=demo-system", "armed_boot_id=boot-old"],
            boot_id="boot-new",
        ))
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("exit=0", cp.stdout, cp.stdout + cp.stderr)

    def test_not_in_host_mode_passes(self):
        # machine_armed_barrier only guards a mutation from host mode: a
        # machine already answering as the bench system is not "about to"
        # leave host mode, it already has.
        cp = self.bash(self._script(
            mode="bench demo-system",
            record=["image=demo-system", "armed_boot_id=boot-1"],
            boot_id="boot-1",
        ))
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("exit=0", cp.stdout, cp.stdout + cp.stderr)

    def test_force_proceeds_with_a_warning(self):
        script = self._script(
            mode="host",
            record=["image=demo-system", "armed_boot_id=boot-1"],
            boot_id="boot-1",
        )
        cp = self.bash(script, env={"WK_FORCE": "1"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("exit=0", cp.stdout, cp.stdout + cp.stderr)
        self.assertIn("FORCED past a barrier", cp.stdout + cp.stderr, cp.stdout + cp.stderr)


class TestEveryMutatingPathCallsTheBarrier(unittest.TestCase):
    """Static check: `machine_armed_barrier` is called, in command position
    (not just mentioned in a comment), from each of the three paths the
    handoff names -- `wk sysimage write --disk`, `wk pi deploy`, and
    `wk pi boot-order`. A grep, not an execution: the point is that nobody
    can delete the call and leave the docstring believing it is still there."""

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

    def test_sysimage_write_calls_it(self):
        calls = self._calls_in("cmd/sysimage")
        self.assertTrue(calls, "cmd/sysimage has no live call to machine_armed_barrier")

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
        for path in ("boot/machines.sh", "cmd/sysimage", "cmd/pi", "cmd/boot", "cmd/status"):
            text = (REPO / path).read_text()
            hits += len(re.findall(r'^machine_armed_barrier\s*\(\)\s*\{', text, re.MULTILINE))
        self.assertEqual(hits, 1, "machine_armed_barrier should be defined exactly once")


if __name__ == "__main__":
    unittest.main()
