"""`wk ai <agent>` started *inside* a workspace: there is no switch to throw
in there, so what runs instead is a measurement (probe_push_here, cmd/ai).

Three facts decide whether an agent in this workspace could publish, and any of
them going the wrong way is a refusal, not a warning: a session that starts
with a working push is the failure the whole arrangement exists to prevent, and
an agent could not fix it from in there anyway. Two of the three -- a GitHub
write and a Bugzilla write having to be refused by the injector -- are the same
question `wk verify` asks from the host, so cmd/ai runs *its* probes rather
than asking again; only the third, whether a deploy key reaches the agent
socket this workspace's own ssh config names, has no host-side twin and lives
here. A read is not one of the three: the injector authenticates it from a
standing token in either switch position, by design, so GET /user answers 200
with push off and says nothing about the switch.

The probe reports in cmd/verify's shape -- verdicts to fd 3, its exit status
the number that went the wrong way -- so cmd/ai runs it beside the rest in one
parallel pass and refuses once. Both files are sourced in library mode
(WK_CLAUDE_LIB=1 / WK_VERIFY_LIB=1, the guards they define for exactly this)
and `ssh` and `ssh-add` are stubs on PATH, so each arm is driven without a
workspace, a key or the network.

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


class _Measure(WkTest):
    def _run(self, sock="", idents=0, body="probe_push_here", marker=True):
        binp = self.tmp / "bin"
        binp.mkdir(exist_ok=True)
        for name, text in (("ssh", SSH), ("ssh-add", SSH_ADD)):
            p = binp / name
            p.write_text(text)
            p.chmod(0o755)
        secrets = self.tmp / "secrets"
        secrets.mkdir(exist_ok=True)
        env = {
            "PATH": f"{binp}:{os.environ['PATH']}",
            "WK_HOST_SECRETS": str(secrets),
            "WK_STORE": str(self.tmp / "store"),
            "WK_TEST_SOCK": sock,
            "WK_TEST_IDENTS": str(idents),
        }
        if marker:
            mk = self.tmp / "wk-marker"
            mk.write_text("name=demo\nsrc=/src/WebKit\n")
            env["WK_MARKER"] = str(mk)
        # cmd/verify's library half is where pass/fail/note live; the probe
        # reports through them, as every other probe in that set does.
        return bash(f'''
set -euo pipefail
export WK_CLAUDE_LIB=1 WK_VERIFY_LIB=1
. "{REPO}/cmd/ai"
. "{REPO}/cmd/verify"
{body}
''', env=env)


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
        key to come from; the API halves still have to answer."""
        cp = self._run(sock="")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("the agent holds nothing", cp.stderr)


class TestTheWriteHalvesAreVerifysOwnProbes(_Measure):
    """Not asked twice. cmd/ai names probe_github_api and probe_bugzilla_api in
    the same parallel pass, and counts their failures as ways to publish -- so
    the 412 the injector answers with the switch off is asserted in one place,
    with one wording, whichever side the check was started from."""

    AI = (REPO / "cmd" / "ai").read_text()

    def test_it_runs_verifys_probes_rather_than_repeating_them(self):
        body = self.AI[self.AI.index("checks_here() {"):]
        body = body[:body.index("\n}\n")]
        for probe in ("probe_github_api", "probe_bugzilla_api"):
            self.assertIn(probe, body, probe)
        self.assertNotIn("api.github.com", self.AI,
                         "cmd/ai asks GitHub itself; cmd/verify's probe is the one asking")
        self.assertNotIn("bugs.webkit.org", self.AI)

    def test_their_failures_are_ways_to_publish_and_not_sandbox_faults(self):
        body = self.AI[self.AI.index("checks_here() {"):]
        body = body[:body.index("\n}\n")]
        line = [l for l in body.splitlines() if "publish=$((publish" in l]
        self.assertEqual(1, len(line), line)
        for job in ("push-keys", "github-api", "bugzilla-api"):
            self.assertIn(job, line[0], job)


class TestOnlyAWorkspaceMeasuresInsteadOfSwitching(_Measure):
    """push_hold_back has the two: outside, it throws the host's switch;
    inside, where `wk push` is refused and push_switch would read that refusal
    as "push is off", it leaves the question to checks_here."""

    # The switch itself is not what is under test here, and calling it for
    # real would reach this device's own agent.
    STUB = 'push_switch() { echo "SWITCH:$1"; return 1; }\nPUSH_WAS_ON=""\n'

    def test_outside_a_workspace_it_throws_the_switch(self):
        cp = self._run(marker=False, body=self.STUB + "push_hold_back demo")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("SWITCH:status", cp.stdout)

    def test_inside_one_it_never_asks_the_switch(self):
        cp = self._run(body=self.STUB + "push_hold_back demo")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("SWITCH:", cp.stdout)


if __name__ == "__main__":
    unittest.main()
