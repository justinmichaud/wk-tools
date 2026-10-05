"""`wk ai <agent> <ws>` holds the push keys back on every target before control is handed over: cmd/ai runs
in-process against tests/test_ai.py's SimTarget of each kind, and a failed `push off` is cmd/ai's refusal to run."""
import contextlib
import io
import os
import unittest
from unittest import mock

from tests.test_ai import AI, WK, sim_registry, SimTarget
from tests.support import WkTest
from wk.act import Refused
from wk.machine import Fake, Result


class _AiRun(unittest.TestCase):
    def _ai(self, target, agent="claude", push_status=0, push_off=1):
        """(status, stderr) of `wk ai <agent> probe-ws` on a workspace of that kind; the `wk key push` calls it made are self.calls."""
        fake, self.calls = Fake(), []

        def push(argv, f):
            self.calls.append(" ".join(argv[2:]))
            return Result({"status": push_status, "off": push_off}.get(argv[3], 0))
        fake.react([WK, "key", "push"], push)
        env = {"WK_NAME": "probe-ws", "WK_TARGET": target}
        t = SimTarget(fake, env, kind=target)
        t.answers["find " + agent] = Result(0, "/c\n")
        with mock.patch.dict(os.environ, {"WK_FORCE": "1"}), mock.patch.object(AI, "foreground", return_value=0), \
                mock.patch.object(AI.Ai, "checks"), mock.patch.object(AI.Ai, "guest_egress"), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                status = AI.main([agent], env=env, reg=sim_registry(env, fake, t))
            except Refused as e:
                status = e.status
        return status, err.getvalue()


TARGETS = ("container", "vm", "remote")


class TestTheSwitchIsThrownForEveryTarget(_AiRun):
    CALLS = {"container": ["push status", "push off"], "vm": ["push status", "push off"],
             "remote": ["push status --target remote", "push off --target remote"]}

    def test_push_is_turned_off_before_control_is_handed_over(self):
        for target in TARGETS:
            with self.subTest(target=target):
                status, err = self._ai(target)
                self.assertTrue(any(c.startswith("push off") for c in self.calls),
                                f"{target}: {self.calls}")
                # The `off` was made to fail, so cmd/ai refuses to run at all
                # rather than handing over an agent that can publish.
                self.assertNotEqual(status, 0, err)
                self.assertIn("refusing to run", err)

    def test_a_guest_is_not_the_exception(self):
        """The defect verbatim: `wk ai claude <guest>` on a vm target left the
        switch on while the guest held a copy of the key."""
        self._ai("vm")
        self.assertEqual(["push status", "push off"], self.calls)

    def test_the_second_agent_gets_the_same_treatment(self):
        """The switch is a property of handing over control, not of Claude."""
        for target in TARGETS:
            with self.subTest(target=target):
                self._ai(target, agent="pi")
                self.assertTrue(any(c.startswith("push off") for c in self.calls),
                                f"{target}: {self.calls}")

    def test_a_build_box_has_it_thrown_on_its_own_store(self):
        """It keeps its keys under its own wk root, so the switch is named
        rather than assumed to be this machine's."""
        self._ai("remote")
        self.assertEqual(["push status --target remote",
                          "push off --target remote"], self.calls)

    def test_every_other_target_uses_this_machine_s_store(self):
        for target in ("container", "vm"):
            with self.subTest(target=target):
                self._ai(target)
                for call in self.calls:
                    self.assertNotIn("--target", call)

    def test_a_switch_already_off_is_left_alone(self):
        for target in TARGETS:
            with self.subTest(target=target):
                self._ai(target, push_status=1)
                self.assertEqual(self.CALLS[target][:1], self.calls)


class TestAnUnmeasuredSwitchIsARefusal(_AiRun):
    """3 (did not answer) and 5 (no switch) are not "off"; 4 (no keys anywhere) is."""

    def test_a_machine_that_did_not_answer_stops_the_command(self):
        status, out = self._ai("remote", push_status=3)
        self.assertNotEqual(status, 0, out)
        self.assertIn("refusing to run", out)
        self.assertIn("wk key push status --target remote", out)
        self.assertEqual(["push status --target remote"], self.calls, self.calls)

    def test_a_machine_with_no_switch_stops_it_too(self):
        status, err = self._ai("container", push_status=5)
        self.assertNotEqual(status, 0, err)
        self.assertIn("refusing to run", err)
        self.assertEqual(["push status"], self.calls, self.calls)

    def test_no_keys_and_no_token_anywhere_is_a_measured_off(self):
        """4 is `wk key deploy` never having been run here: there is nothing
        to hold back, and refusing would stop every session on a machine that
        cannot publish at all."""
        _, err = self._ai("container", push_status=4)
        self.assertEqual(["push status"], self.calls, self.calls)
        self.assertNotIn("refusing to run", err)


class TestTheSwitchComesBackOnlyForAPerson(unittest.TestCase):
    """restore_push: a headless run leaves the switch off; only a person at a terminal gets it back."""

    def _restore(self, was_on, terminal):
        fake = Fake()
        fake.answer([WK, "key", "push"])
        env = {}
        target = SimTarget(fake, env)
        reg = sim_registry(env, fake, target)
        ai = AI.Ai(AI.ROOT, env, reg, target, "claude", "demo")
        ai.push_was_on = was_on
        with contextlib.redirect_stderr(io.StringIO()) as err:
            ai.restore_push(terminal)
        return [" ".join(e[1][2:]) for e in fake.effects], err.getvalue()

    def test_a_headless_session_leaves_it_off(self):
        self.assertEqual([], self._restore(True, terminal=False)[0])

    def test_a_person_at_a_terminal_gets_it_back(self):
        self.assertEqual(["push on"], self._restore(True, terminal=True)[0])

    def test_a_switch_this_command_did_not_throw_is_not_touched(self):
        for terminal in (False, True):
            self.assertEqual(([], ""), self._restore(False, terminal))


class TestOneShapeAndNoTargetNames(unittest.TestCase):
    """The switch is thrown first, and each target's own sandbox gate still runs behind it."""

    def test_every_target_s_sandbox_gate_still_runs_after_it(self):
        for kind, gate in (("container", "checks"), ("vm", "guest_egress"), ("remote", "gh")):
            with self.subTest(kind=kind):
                order = []
                fake = Fake()
                fake.react([WK, "key", "push"], lambda argv, f: order.append("push") or Result(1))
                env = {"WK_NAME": "demo", "WK_TARGET": kind}
                target = SimTarget(fake, env, kind=kind)
                target.answers["find claude"] = Result(0, "/c\n")
                target.answers["gh auth status"] = lambda argv: order.append("gh") or Result(0)
                reg = sim_registry(env, fake, target)
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
        target = SimTarget(fake, env)
        with contextlib.redirect_stderr(io.StringIO()):
            AI.Ai(AI.ROOT, env, sim_registry(env, fake, target), target, "claude", "demo").push_hold_back()
        return [e[1] for e in fake.effects if e[1][:1] == (WK,)]

    def test_outside_a_workspace_it_throws_the_switch_and_inside_never_asks_it(self):
        self.assertEqual([(WK, "key", "push", "status")], self._hold_back(marker=False))
        self.assertEqual([], self._hold_back(marker=True))
