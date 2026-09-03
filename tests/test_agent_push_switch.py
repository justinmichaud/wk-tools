"""`wk ai <agent> <ws>` holds the push keys back, on every target.

The rule (cmd/push, docs/HANDOFF-sandboxing.md): only the person at the
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
import os
import unittest

from tests.support import REPO, WkTest, bash, stub_path

AI = (REPO / "cmd" / "ai").read_text()

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


class TestTheSwitchComesBackOnlyForAPerson(WkTest):
    """restore_push, driven in cmd/ai's library mode: a headless run re-enters
    this command every few minutes via the babysitter's fix loop, and leaving
    push on between attempts would be the switch flapping open unattended."""

    def _restore(self, was_on, target=""):
        log = self.tmp / "wk.log"
        log.write_text("")
        root = self.tmp / "root"
        root.mkdir(exist_ok=True)
        for p in REPO.iterdir():
            if p.name != "wk" and not (root / p.name).exists():
                (root / p.name).symlink_to(p)
        wk = root / "wk"
        wk.write_text("#!/bin/sh\n" + FAKE_WK)
        wk.chmod(0o755)
        cp = bash(f'''
export WK_CLAUDE_LIB=1
. "{root}/cmd/ai"
PUSH_WAS_ON={was_on!r}
PUSH_TARGET={target!r}
restore_push
''', env={"WK_ROOT": str(root), "WK_TEST_WK_LOG": str(log),
                  "XDG_STATE_HOME": str(self.tmp / "state"),
                  "WK_STORE": str(self.tmp / "store")})
        self.calls = [l for l in log.read_text().splitlines() if l.strip()]
        return cp

    def test_a_headless_session_leaves_it_off(self):
        """bash() gives the script no terminal, which is exactly the
        babysitter's condition."""
        cp = self._restore("1")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual([], self.calls, self.calls)
        self.assertIn("stays off", cp.stdout + cp.stderr)

    def test_a_switch_this_command_did_not_throw_is_not_touched(self):
        cp = self._restore("")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual([], self.calls, self.calls)


class TestOneShapeAndNoTargetNames(unittest.TestCase):
    """Source-level twin: the switch is thrown by one unconditional call, so a
    fourth target kind cannot arrive without it. A `case` or an `if` over
    WK_TARGET_KIND around it is how the guest was missed."""

    def test_the_call_is_unconditional(self):
        calls = [l for l in AI.splitlines()
                 if l.strip() == 'push_hold_back "$NAME"']
        self.assertEqual(1, len(calls), calls)
        self.assertEqual('push_hold_back "$NAME"', calls[0],
                         "the call is indented, so it is inside a conditional")

    def test_only_the_store_s_owner_is_named_by_kind(self):
        """The one thing that differs by target is which machine holds the
        keys, so the only line that names a kind is the one that says a build
        box keeps its own. Comments are stripped: the prose explains all four
        targets, which is the point of it."""
        code = [l.strip() for l in AI.splitlines()
                if l.strip() and not l.strip().startswith("#")]
        naming = [l for l in code if "PUSH_TARGET=" in l and "WK_TARGET_KIND" in l]
        self.assertEqual(['[ "$WK_TARGET_KIND" != remote ] || PUSH_TARGET="$TARGET"'],
                         naming, naming)

    def test_every_target_s_sandbox_gate_still_runs_after_it(self):
        """The switch goes first -- `wk verify` measures the keys' absence in
        a container -- and each target's own gate is still there behind it:
        `wk verify` for a container, the Softnet checks for a guest, the gh
        refusal for a build box."""
        call = AI.index('push_hold_back "$NAME"')
        for gate in ('WK_NAME="$NAME" "$WK_ROOT/cmd/verify"',
                     'if [ "$WK_TARGET_KIND" = vm ]; then',
                     "gh auth logout"):
            with self.subTest(gate=gate):
                self.assertLess(call, AI.index(gate), gate)

    def test_the_functions_are_in_the_library_block(self):
        """`WK_CLAUDE_LIB=1 . cmd/ai` has to define them, or the driven test
        above is testing a copy."""
        lib = AI[:AI.index('[ "${WK_CLAUDE_LIB:-}" != 1 ] || return 0')]
        for fn in ("push_switch()", "push_hold_back()", "restore_push()"):
            self.assertIn(fn, lib, fn)


if __name__ == "__main__":
    unittest.main()
