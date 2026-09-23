"""Nothing an agent runs can publish. The proxy now *allows* api.github.com and
hands it to the credential injector, so the refusal has moved: the token the
injector would add is not there while push is off, the deploy keys are in an
ssh-agent nothing in a workspace can take a key out of, `wk doctor <ws>` measures
both from inside, `wk ai claude` holds push back before it verifies (and
refuses a build box that holds a gh login: tests/test_ai.py), and `wk push on` is refused while a
claude process runs in any workspace is ended first. The person at the keyboard
is the only publisher.

Run: python3 -m unittest tests.test_no_publish -v
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, bash

PROXY = REPO / "container" / "proxy" / "wk-proxy.py"
PUSH = (REPO / "cmd" / "push").read_text()


def _policy():
    spec = importlib.util.spec_from_file_location("wkproxy", str(PROXY))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.Policy(tempfile.mkdtemp(prefix="wk-test-store-"))


class TestProxyRefusesGitHubsApi(unittest.TestCase):
    def test_uploads_is_still_refused_with_the_reason(self):
        """The upload API publishes release assets and has no injector: it is
        the one GitHub host that stayed on the denied list."""
        p = _policy()
        ok, why = p.host_allowed("uploads.github.com", 443)
        self.assertFalse(ok)
        self.assertIn("refused", why)

    def test_the_api_is_allowed_only_on_443_and_only_through_the_injector(self):
        """A tunnel on 22 or 80 would be a way around the injector, and the
        generic `github.com` suffix would grant both if this were not an exact
        match checked before it."""
        p = _policy()
        ok, why = p.host_allowed("api.github.com", 443)
        self.assertTrue(ok, why)
        self.assertIn("injector", why)
        for port in (22, 80):
            with self.subTest(port=port):
                ok, why = p.host_allowed("api.github.com", port)
                self.assertFalse(ok, why)

    def test_a_lookalike_is_not_the_injected_host(self):
        p = _policy()
        for host in ("evilapi.github.com.attacker.net", "api.github.com.attacker.net"):
            with self.subTest(host=host):
                ok, _ = p.host_allowed(host, 443)
                self.assertFalse(ok, host)

    def test_github_itself_and_codeload_stay_allowed(self):
        p = _policy()
        for host, port in (("github.com", 443), ("github.com", 22), ("codeload.github.com", 443),
                           ("raw.githubusercontent.com", 443)):
            with self.subTest(host=host, port=port):
                ok, _ = p.host_allowed(host, port)
                self.assertTrue(ok, host)


class TestPushOnEndsAnyRunningAgent(unittest.TestCase):
    def _sessions(self, t_list_out, running):
        # agent_pids runs the scan with `t_exec "$ws" sh -c '...'` and reads
        # its stdout; the stub prints a pid for the named workspace, standing
        # in for "a claude exe is running in that container".
        return bash(f'''
. "{REPO}/lib/common.sh"
t_list() {{ printf '%b' "{t_list_out}"; }}
t_exec() {{ case "$1" in {running}) echo 4242 ;; esac; }}
{_lift("AGENT_PID_SCAN=")}
{_lift("agent_pids")}
{_lift("agent_sessions")}
agent_sessions
''')

    def test_names_the_workspaces_with_a_claude_process(self):
        cp = self._sessions("a\\tUp 2 hours\\nb\\tUp 1 hour\\n", "b")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.split(), ["b"])

    def test_no_session_is_silence(self):
        cp = self._sessions("a\\tUp 2 hours\\n", "none")
        self.assertEqual(cp.stdout.strip(), "")

    def test_on_ends_them_before_loading_the_agent(self):
        """`on)` asks the gate about every session it found, and does it
        before the keys reach the agent; the gate is where the ending is."""
        on = PUSH[PUSH.index("\non)\n"):PUSH.index("\noff)\n")]
        self.assertLess(on.index("push_end_sessions_first $(agent_sessions)"),
                        on.index("push_agent_load"))
        self.assertIn("end_agent_sessions", _lift("push_end_sessions_first()"))

    def test_ending_them_is_asked_first(self):
        """Killing a session is destructive, so it goes through the one
        yes/no helper and the command declares itself to the dispatcher."""
        gate = _lift("push_end_sessions_first()")
        self.assertLess(gate.index('confirm "'), gate.index("end_agent_sessions"))
        self.assertIn("# wk: destructive on", PUSH)

    def test_a_declined_prompt_leaves_the_keys_out(self):
        self.assertIn('die "push stays off', _lift("push_end_sessions_first()"))


def _lift(name):
    """One shell definition out of cmd/push, by name: a `name()` function up to
    its closing brace, or a `NAME=` assignment up to the line closing its
    single-quoted value."""
    import subprocess
    rng = f"/^{name}/,/^}}/p" if name.endswith("()") or "=" not in name else f"/^{name}/,/^done'$/p"
    text = subprocess.run(["sed", "-n", rng, str(REPO / "cmd" / "push")],
                          capture_output=True, text=True).stdout
    assert text.strip(), name
    return text


if __name__ == "__main__":
    unittest.main()
