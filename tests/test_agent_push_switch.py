"""`wk ai <agent> <ws>` holds the push keys back on every place before control is handed over: cmd/ai runs
in-process against tests/test_ai.py's SimDriver of each kind, and a failed `push off` is cmd/ai's refusal to run."""
import contextlib
import io
import os
import unittest
from unittest import mock

from tests.test_ai import AI, WK, sim_registry, SimDriver
from tests.support import WkTest
from wk.act import Refused
from wk.machine import Fake, Result


class _AiRun(unittest.TestCase):
    def _ai(self, place, agent="claude", push_status=0, push_off=1):
        """(status, stderr) of `wk ai <agent> probe-ws` on a workspace of that kind; the `wk key push` calls it made are self.calls."""
        fake, self.calls = Fake(), []

        def push(argv, f):
            self.calls.append(" ".join(argv[2:]))
            return Result({"status": push_status, "off": push_off}.get(argv[3], 0))
        fake.react([WK, "key", "push"], push)
        env = {"WK_NAME": "probe-ws", "WK_PLACE": place}
        t = SimDriver(fake, env, kind=place)
        t.answers["find " + agent] = Result(0, "/c\n")
        with mock.patch.dict(os.environ, {"WK_FORCE": "1"}), mock.patch.object(AI, "foreground", return_value=0), \
                mock.patch.object(AI.Ai, "checks"), mock.patch.object(AI.Ai, "guest_egress"), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                status = AI.main([agent], env=env, reg=sim_registry(env, fake, t))
            except Refused as e:
                status = e.status
        return status, err.getvalue()


PLACES = ("container", "vm", "remote")


class TestTheSwitchIsThrownForEveryPlace(_AiRun):
    CALLS = {"container": ["push status", "push off"], "vm": ["push status", "push off"],
             "remote": ["push status --on remote", "push off --on remote"]}

    def test_push_is_turned_off_before_control_is_handed_over(self):
        for place in PLACES:
            with self.subTest(place=place):
                status, err = self._ai(place)
                self.assertEqual(self.CALLS[place], self.calls)
                # The `off` was made to fail, so cmd/ai refuses to run at all
                # rather than handing over an agent that can publish.
                self.assertNotEqual(status, 0, err)
                self.assertIn("refusing to run", err)

    def test_the_second_agent_gets_the_same_treatment(self):
        """The switch is a property of handing over control, not of Claude."""
        for place in PLACES:
            with self.subTest(place=place):
                self._ai(place, agent="pi")
                self.assertTrue(any(c.startswith("push off") for c in self.calls),
                                f"{place}: {self.calls}")

    def test_a_switch_already_off_is_left_alone(self):
        for place in PLACES:
            with self.subTest(place=place):
                self._ai(place, push_status=1)
                self.assertEqual(self.CALLS[place][:1], self.calls)


class TestAnUnmeasuredSwitchIsARefusal(_AiRun):
    """4 (no keys anywhere) is a measured off; 3 and 5 refuse (tests/test_ai.py)."""

    def test_no_keys_and_no_token_anywhere_is_a_measured_off(self):
        """4 is `wk key deploy` never having been run here: there is nothing
        to hold back, and refusing would stop every session on a machine that
        cannot publish at all."""
        _, err = self._ai("container", push_status=4)
        self.assertEqual(["push status"], self.calls, self.calls)
        self.assertNotIn("refusing to run", err)


class TestOneShapeAndNoPlaceNames(unittest.TestCase):
    """The switch is thrown first, and each place's own sandbox gate still runs behind it."""

    def test_every_target_s_sandbox_gate_still_runs_after_it(self):
        for kind, gate in (("container", "checks"), ("vm", "guest_egress"), ("remote", "gh")):
            with self.subTest(kind=kind):
                order = []
                fake = Fake()
                fake.react([WK, "key", "push"], lambda argv, f: order.append("push") or Result(1))
                env = {"WK_NAME": "demo", "WK_PLACE": kind}
                place = SimDriver(fake, env, kind=kind)
                place.answers["find claude"] = Result(0, "/c\n")
                place.answers["gh auth status"] = lambda argv: order.append("gh") or Result(0)
                reg = sim_registry(env, fake, place)
                with mock.patch.dict(os.environ, {"WK_FORCE": "1"}), mock.patch.object(AI, "foreground", return_value=0), \
                        mock.patch.object(AI.Ai, "checks", side_effect=lambda: order.append("checks")), \
                        mock.patch.object(AI.Ai, "guest_egress", side_effect=lambda: order.append("guest_egress")), \
                        contextlib.redirect_stderr(io.StringIO()):
                    try:
                        AI.main(["claude"], env=env, reg=reg)
                    except Refused:
                        pass
                self.assertEqual("push", order[0], order)
                self.assertIn(gate, order)


class TestOnlyAWorkspaceMeasuresInsteadOfSwitching(WkTest):
    """Inside a workspace `wk key push` is refused, so `wk ai` leaves the question to `wk doctor`'s checks."""

    def _hold_back(self, marker):
        fake = Fake()
        fake.answer([WK, "key", "push"], rc=1)
        env = {"WK_MARKER": str(self.tmp / "wk-marker")}
        if marker:
            (self.tmp / "wk-marker").write_text("name=demo\nsrc=/src/WebKit\n")
        place = SimDriver(fake, env)
        with contextlib.redirect_stderr(io.StringIO()):
            AI.Ai(AI.ROOT, env, sim_registry(env, fake, place), place, "claude", "demo").push_hold_back()
        return [e[1] for e in fake.effects if e[1][:1] == (WK,)]

    def test_outside_a_workspace_it_throws_the_switch_and_inside_never_asks_it(self):
        self.assertEqual([(WK, "key", "push", "status")], self._hold_back(marker=False))
        self.assertEqual([], self._hold_back(marker=True))
