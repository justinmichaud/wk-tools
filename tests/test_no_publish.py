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
import os
import subprocess
import tempfile
import unittest

from tests.support import REPO
from tests.test_push_switch import PUSH, Fleet, PushTest
from tests.test_wk_secrets import SOCK

PROXY = REPO / "container" / "proxy" / "wk-proxy.py"


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


class TestPushOnEndsAnyRunningAgent(PushTest):
    """cmd/push's session gate, driven over the fake machine tests/test_push_switch.py builds."""

    def setUp(self):
        super().setUp()
        self.w.seed()
        self.box.claude = {"a": [], "b": ["4242"]}

    def sessions(self):
        return PUSH.Push(Fleet(self.w, self.boxes), self.w.sec(), self.clock).agent_sessions()

    def test_names_the_workspaces_with_a_claude_process(self):
        self.assertEqual(["b"], self.sessions())

    def test_no_session_is_silence(self):
        self.box.claude = {"a": []}
        self.assertEqual([], self.sessions())

    def test_the_scan_is_plain_sh(self):
        cp = subprocess.run(["sh", "-n", "-c", PUSH.AGENT_PID_SCAN], capture_output=True, text=True)
        self.assertEqual(0, cp.returncode, cp.stderr)

    def test_on_ends_them_before_loading_the_agent(self):
        os.environ["WK_YES"] = "1"
        self.push("on")
        acts = [e[1] for e in self.w.acts() if e[0] == "act"]
        self.assertLess(acts.index(("exec", "b", "sh", "-c", "kill 4242 2>/dev/null; exit 0")),
                        next(i for i, a in enumerate(acts) if "ssh-add -" in a[-1]))

    def test_ending_them_is_asked_first(self):
        """Killing a session is destructive, so it is asked through the one yes/no helper and the command
        declares itself to the dispatcher."""
        os.environ["WK_DESTRUCTIVE"] = "1"
        rc, _, err = self.push("on")
        self.assertIn("end the claude session(s) in b? -- declining", err)
        self.assertEqual(["4242"], self.box.claude["b"])
        self.assertIn("# wk: destructive on", (REPO / "cmd" / "push").read_text())

    def test_a_declined_prompt_leaves_the_keys_out(self):
        rc, _, err = self.push("on")
        self.assertEqual(1, rc)
        self.assertIn("push stays off", err)
        self.assertEqual(set(), self.w.agents[SOCK])


if __name__ == "__main__":
    unittest.main()
