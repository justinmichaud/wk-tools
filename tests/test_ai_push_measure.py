"""`wk ai <agent>` started *inside* a workspace: there is no switch to throw
in there, so what runs instead is the measurement (push_measure_here, cmd/ai).

Two facts decide whether an agent in this workspace could publish -- whether
any deploy key reaches it through the agent socket its own ssh config names,
and whether an authenticated call to the GitHub API is refused -- and either
one going the wrong way is a refusal, not a warning: a session that starts
with a working push is the failure the whole arrangement exists to prevent,
and an agent could not fix it from in there anyway.

cmd/ai is sourced in library mode (WK_CLAUDE_LIB=1, the guard it defines for
exactly this, the way tests/test_claude_rc.py does) and `ssh`, `ssh-add` and
`curl` are stubs on PATH, so each arm is driven without a workspace, a key or
the network.

Run: python3 -m unittest tests.test_ai_push_measure -v
"""
import os
import unittest

from tests.support import REPO, WkTest, bash

# `ssh -G <alias>`: the one question asked of ssh itself, so that the socket
# under test is the one a push would really use.
SSH = '''#!/bin/sh
[ "$1" = "-G" ] || exit 0
[ -n "$WK_TEST_SOCK" ] || exit 0
echo "identityagent $WK_TEST_SOCK"
'''

# `ssh-add -l` against that socket: one line per identity, and the exact words
# a real agent uses for none.
SSH_ADD = '''#!/bin/sh
[ "$WK_TEST_IDENTS" -gt 0 ] || { echo "The agent has no identities."; exit 1; }
i=0
while [ "$i" -lt "$WK_TEST_IDENTS" ]; do
    echo "256 SHA256:xxx wk-key (ED25519)"
    i=$((i + 1))
done
'''

CURL = '''#!/bin/sh
printf '%s' "$WK_TEST_CODE"
'''


class _Measure(WkTest):
    def _run(self, sock="", idents=0, code="401", body="push_measure_here"):
        binp = self.tmp / "bin"
        binp.mkdir(exist_ok=True)
        for name, text in (("ssh", SSH), ("ssh-add", SSH_ADD), ("curl", CURL)):
            p = binp / name
            p.write_text(text)
            p.chmod(0o755)
        secrets = self.tmp / "secrets"
        secrets.mkdir(exist_ok=True)
        return bash(f'''
set -euo pipefail
export WK_CLAUDE_LIB=1
. "{REPO}/cmd/ai"
{body}
''', env={
            "PATH": f"{binp}:{os.environ['PATH']}",
            "WK_HOST_SECRETS": str(secrets),
            "WK_STORE": str(self.tmp / "store"),
            "WK_TEST_SOCK": sock,
            "WK_TEST_IDENTS": str(idents),
            "WK_TEST_CODE": code,
        })


class TestAKeyThatReachesTheWorkspaceIsARefusal(_Measure):
    def test_it_names_the_socket_and_the_host_side_remedy(self):
        cp = self._run(sock="/run/wk/ssh-agent.sock", idents=2)
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("2 deploy key(s) reach this workspace", cp.stderr)
        self.assertIn("/run/wk/ssh-agent.sock", cp.stderr)
        self.assertIn("wk push off", cp.stderr)

    def test_an_empty_agent_is_not_a_key(self):
        """The switch off is an agent that answers and holds nothing, which is
        the normal state a session starts in -- not an error."""
        cp = self._run(sock="/run/wk/ssh-agent.sock", idents=0)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("the agent holds nothing", cp.stderr)

    def test_no_socket_at_all_is_not_a_key_either(self):
        """A target whose ssh config names no IdentityAgent has nowhere for a
        key to come from; the API half still has to answer."""
        cp = self._run(sock="")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("the agent holds nothing", cp.stderr)


class TestTheApiHalf(_Measure):
    def test_a_401_is_what_it_requires(self):
        cp = self._run(code="401")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("the API refuses", cp.stderr)

    def test_an_authenticated_answer_is_a_refusal(self):
        cp = self._run(code="200")
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("answered '200' where it must answer 401", cp.stderr)

    def test_no_answer_at_all_is_a_refusal_that_says_so(self):
        """Unreachable is not proof of anything, and reading it as "off" is
        the silent failure this refusal exists for."""
        cp = self._run(code="")
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("answered 'nothing'", cp.stderr)


class TestOnlyAWorkspaceMeasuresInsteadOfSwitching(_Measure):
    """push_hold_back has the two: outside, it throws the host's switch;
    inside, where `wk push` is refused and push_switch would read that refusal
    as "push is off", it measures instead."""

    # The switch itself is not what is under test here, and calling it for
    # real would reach this device's own agent.
    STUB = 'push_switch() { echo "SWITCH:$1"; return 1; }\nPUSH_WAS_ON=""\n'

    def test_outside_a_workspace_it_throws_the_switch(self):
        cp = self._run(sock="/run/wk/ssh-agent.sock", idents=1,
                       body=self.STUB + "push_hold_back demo")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("SWITCH:status", cp.stdout)
        self.assertNotIn("deploy key(s) reach this workspace", cp.stderr)

    def test_inside_one_it_measures_and_never_asks_the_switch(self):
        marker = self.tmp / "wk-marker"
        marker.write_text("name=demo\nsrc=/src/WebKit\n")
        cp = self._run(sock="/run/wk/ssh-agent.sock", idents=1,
                       body=f'export WK_MARKER={marker}\n'
                            + self.STUB + "push_hold_back demo")
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("deploy key(s) reach this workspace", cp.stderr)
        self.assertNotIn("SWITCH:", cp.stdout)


if __name__ == "__main__":
    unittest.main()
