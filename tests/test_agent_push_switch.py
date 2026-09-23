"""`wk ai <agent> <ws>` holds the push keys back, on every target.

The rule (cmd/push): only the person at the
keyboard pushes. So before control is handed to an agent, the switch is thrown
off -- and it has to be thrown for *every* target, because every one of them
can push:

    container   links the live /secrets mount
    macOS guest holds a copy the host wrote on start (targets/vm.sh's
                _write_deploy_keys), which `wk push off` converges
    build box   keeps its own keys under its own wk root, so the switch is
                thrown there, by name

The measured defect this closes: cmd/ai named `container` and `remote` only, so
`wk ai claude <guest>` handed an agent a guest that still held a deploy key.

Nothing here reaches a real workspace, a real key or a real `wk push`: cmd/ai
runs against a scratch $WK_ROOT whose `wk` is a script that records what it was
asked for, and the recorded `push off` is made to fail -- which is cmd/ai's own
refusal to run, and stops the command right there, before any driver is used.

Run: python3 -m unittest tests.test_agent_push_switch -v
"""
import contextlib
import io
import os
import unittest
from unittest import mock

from tests.support import REPO, WkTest, bash, stub_path
from tests.test_ai import AI, WK, SimRegistry, SimTarget
from wk.act import Refused
from wk.machine import Fake, Result

# The recording `wk`. `push status` answers "on" by default, and `push off` is
# made to fail by the caller, which is what turns cmd/ai's refusal into the
# stopping point of the run.
FAKE_WK = '''
printf '%s\\n' "$*" >> "$WK_TEST_WK_LOG"
case "$*" in
    "push status"*) exit "${WK_TEST_PUSH_STATUS:-0}" ;;
    "push off"*)    exit "${WK_TEST_PUSH_OFF:-0}" ;;
esac
exit 0
'''

# A build box, faked to the depth cmd/ai's remote arm reaches before the
# switch: one ssh that runs the command here, and a `claude` in the account's
# own ~/.local that answers `--version` (the property cmd/ai tests for).
FAKE_SSH = '''
for a in "$@"; do last="$a"; done
sh -c "$last"
'''
FAKE_CLAUDE = '''
echo 9.9.9
'''


class _AiRun(WkTest):
    """cmd/ai against a scratch $WK_ROOT: every path in the tree, except `wk`
    itself, which is the recorder."""

    def _root(self):
        root = self.tmp / "root"
        root.mkdir(exist_ok=True)
        for p in REPO.iterdir():
            if p.name == "wk":
                continue
            link = root / p.name
            if not link.exists():
                link.symlink_to(p)
        wk = root / "wk"
        wk.write_text("#!/bin/sh\n" + FAKE_WK)
        wk.chmod(0o755)
        return root

    def _ai(self, target, agent="claude", push_status=0, push_off=1):
        """Run `cmd/ai <agent> probe-ws` for one target kind. Returns the
        CompletedProcess; the calls it made are self.calls."""
        root = self._root()
        home = self.tmp / "home"
        (home / ".local" / "bin").mkdir(parents=True, exist_ok=True)
        claude = home / ".local" / "bin" / "claude"
        claude.write_text("#!/bin/sh\n" + FAKE_CLAUDE)
        claude.chmod(0o755)
        # What t_src resolves to on a build box; cmd/ai's probe cds into it.
        (self.tmp / "rroot" / "ws" / "probe-ws" / "WebKit").mkdir(parents=True,
                                                                  exist_ok=True)
        log = self.tmp / "wk.log"
        log.write_text("")

        with stub_path({"ssh": FAKE_SSH}) as binp:
            env = {
                "PATH": f"{binp}:{os.environ['PATH']}",
                "HOME": str(home),
                "WK_ROOT": str(root),
                "WK_NAME": "probe-ws",
                "WK_TARGET": target,
                "WK_STORE": str(self.tmp / "store"),
                "XDG_STATE_HOME": str(self.tmp / "state"),
                "WK_TEST_WK_LOG": str(log),
                "WK_TEST_PUSH_STATUS": str(push_status),
                "WK_TEST_PUSH_OFF": str(push_off),
                # The remote arm is a barrier (a machine with no sandbox);
                # forcing past it is what lets the rest of the arm run.
                "WK_FORCE": "1",
                "WK_REMOTE_HOST": "fakebox",
                "WK_REMOTE_ROOT": str(self.tmp / "rroot"),
            }
            cp = bash(f'exec "$WK_AI_ROOT/cmd/ai" {agent} probe-ws',
                      env={**env, "WK_AI_ROOT": str(root)})
        self.calls = [l for l in log.read_text().splitlines() if l.strip()]
        return cp


TARGETS = ("container", "vm", "remote")


class TestTheSwitchIsThrownForEveryTarget(_AiRun):
    """One shape for every target, and the guest is the one that was missing."""

    def test_push_is_turned_off_before_control_is_handed_over(self):
        for target in TARGETS:
            with self.subTest(target=target):
                cp = self._ai(target)
                self.assertTrue(any(c.startswith("push off") for c in self.calls),
                                f"{target}: {self.calls}")
                # The `off` was made to fail, so cmd/ai refuses to run at all
                # rather than handing over an agent that can publish.
                self.assertNotEqual(cp.returncode, 0, cp.stdout)
                self.assertIn("refusing to run", cp.stdout + cp.stderr)

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
        """`wk push status` says off (exit 1), so there is nothing to hold
        back and nothing to turn back on afterwards."""
        for target in TARGETS:
            with self.subTest(target=target):
                self._ai(target, push_status=1)
                self.assertEqual([c for c in self.calls if c.startswith("push")],
                                 [c for c in self.calls if c.startswith("push status")],
                                 self.calls)


class TestAnUnmeasuredSwitchIsARefusal(_AiRun):
    """`wk push status` has more answers than on and off: 3 is a machine that
    did not answer and 5 a machine with no switch (cmd/push). Reading either as
    "off" hands an agent a session whose push may be live -- the measured
    defect was `push_switch status || return 0`, which treated every non-zero
    exit as a closed switch."""

    def test_a_machine_that_did_not_answer_stops_the_command(self):
        cp = self._ai("remote", push_status=3)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        out = cp.stdout + cp.stderr
        self.assertIn("refusing to run", out)
        self.assertIn("wk push status --target remote", out)
        self.assertEqual(["push status --target remote"], self.calls, self.calls)

    def test_a_machine_with_no_switch_stops_it_too(self):
        cp = self._ai("container", push_status=5)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("refusing to run", cp.stdout + cp.stderr)
        self.assertEqual(["push status"], self.calls, self.calls)

    def test_no_keys_and_no_token_anywhere_is_a_measured_off(self):
        """4 is `wk key deploy` never having been run here: there is nothing
        to hold back, and refusing would stop every session on a machine that
        cannot publish at all."""
        cp = self._ai("container", push_status=4)
        self.assertEqual(["push status"], self.calls, self.calls)
        self.assertNotIn("refusing to run", cp.stdout + cp.stderr)


class TestTheSwitchComesBackOnlyForAPerson(unittest.TestCase):
    """restore_push: a headless run re-enters this command every few minutes
    via the babysitter's fix loop, and leaving push on between attempts would
    be the switch flapping open unattended."""

    def _restore(self, was_on, terminal):
        fake = Fake()
        fake.answer([WK, "push"])
        env = {}
        reg = SimRegistry(env, fake, SimTarget(fake, env))
        ai = AI.Ai(AI.ROOT, env, reg, reg.target, "claude", "demo")
        ai.push_was_on = was_on
        with contextlib.redirect_stderr(io.StringIO()) as err:
            ai.restore_push(terminal)
        return [" ".join(e[1][1:]) for e in fake.effects], err.getvalue()

    def test_a_headless_session_leaves_it_off(self):
        calls, err = self._restore(True, terminal=False)
        self.assertEqual([], calls)
        self.assertIn("stays off", err)

    def test_a_person_at_a_terminal_gets_it_back(self):
        calls, err = self._restore(True, terminal=True)
        self.assertEqual(["push on"], calls)
        self.assertIn("git push turned back on", err)

    def test_a_switch_this_command_did_not_throw_is_not_touched(self):
        for terminal in (False, True):
            self.assertEqual(([], ""), self._restore(False, terminal))


class TestOneShapeAndNoTargetNames(unittest.TestCase):
    """The switch is thrown by one unconditional step, so a fourth target kind
    cannot arrive without it, and each target's own gate still runs behind it:
    `wk doctor <ws>`'s checks, the Softnet checks for a guest, the gh refusal
    for a build box."""

    def test_every_target_s_sandbox_gate_still_runs_after_it(self):
        for kind, gate in (("container", "checks"), ("vm", "guest_egress"), ("remote", "gh")):
            with self.subTest(kind=kind):
                order = []
                fake = Fake()
                fake.react([WK, "push"], lambda argv, f: order.append("push") or Result(1))
                env = {"WK_NAME": "demo", "WK_TARGET": kind}
                target = SimTarget(fake, env, kind=kind)
                target.answers["command -v claude"] = Result(0, "/c\n")
                target.answers["gh auth status"] = lambda argv: order.append("gh") or Result(0)
                reg = SimRegistry(env, fake, target)
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


if __name__ == "__main__":
    unittest.main()
