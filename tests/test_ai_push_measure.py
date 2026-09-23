"""`wk ai <agent>` started *inside* a workspace: there is no switch to throw
in there, so the question is left to a measurement -- `wk doctor`'s inside
half, whose push-keys, GitHub-write and Bugzilla-write checks are
tests/test_doctor_wall.py's. Outside a workspace it throws the host's switch.

Run: python3 -m unittest tests.test_ai_push_measure -v
"""
import contextlib
import io
import os
import unittest

from tests.support import WkTest
from tests.test_ai import AI, WK, SimRegistry, SimTarget
from wk.machine import Fake


class TestOnlyAWorkspaceMeasuresInsteadOfSwitching(WkTest):
    """Outside, it throws the host's switch; inside, where `wk push` is refused
    and reading that refusal as "push is off" would be wrong, it leaves the
    question to the checks."""

    def _hold_back(self, marker):
        fake = Fake()
        fake.answer([WK, "push"], rc=1)
        env = {"WK_MARKER": str(self.tmp / "wk-marker")}
        if marker:
            (self.tmp / "wk-marker").write_text("name=demo\nsrc=/src/WebKit\n")
        reg = SimRegistry(env, fake, SimTarget(fake, env))
        with contextlib.redirect_stderr(io.StringIO()):
            AI.Ai(AI.ROOT, env, reg, reg.target, "claude", "demo").push_hold_back()
        return [e[1] for e in fake.effects if e[1][:1] == (WK,)]

    def test_outside_a_workspace_it_throws_the_switch(self):
        self.assertEqual([(WK, "push", "status")], self._hold_back(marker=False))

    def test_inside_one_it_never_asks_the_switch(self):
        self.assertEqual([], self._hold_back(marker=True))


if __name__ == "__main__":
    unittest.main()
