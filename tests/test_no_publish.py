"""Nothing an agent runs can publish: the proxy's GitHub rules, and `wk push on` ending any running claude session first."""
import importlib.util
import os
import subprocess
import tempfile
import unittest

from tests.support import REPO
from tests.test_push_switch import PUSH, PushTest, registry
from tests.test_wk_secrets import SOCK
from wk import pushswitch  # noqa: E402
from wk.machine import Result  # noqa: E402

PROXY = REPO / "container" / "proxy" / "wk-proxy.py"


def _policy():
    spec = importlib.util.spec_from_file_location("wkproxy", str(PROXY))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.Policy(tempfile.mkdtemp(prefix="wk-test-store-"))


class TestProxyRefusesGitHubsApi(unittest.TestCase):
    def test_uploads_is_refused(self):
        self.assertFalse(_policy().host_allowed("uploads.github.com", 443)[0])

    def test_the_api_is_allowed_only_on_443_and_only_through_the_injector(self):
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

    def setUp(self):
        super().setUp()
        self.w.seed()
        self.box.claude = {"a": [], "b": ["4242"]}

    def sessions(self):
        return [ws for _, ws, _ in PUSH.Push(registry(self.w, self.boxes), self.w.sec(), self.clock).agent_sessions()]

    def test_names_the_workspaces_with_a_claude_process(self):
        self.assertEqual(["b"], self.sessions())

    def test_no_session_is_silence(self):
        self.box.claude = {"a": []}
        self.assertEqual([], self.sessions())

    def test_the_scan_is_plain_sh(self):
        cp = subprocess.run(["sh", "-n", "-c", pushswitch.AGENT_PID_SCAN], capture_output=True, text=True)
        self.assertEqual(0, cp.returncode, cp.stderr)

    def test_on_ends_them_before_loading_the_agent(self):
        os.environ["WK_YES"] = "1"
        self.push("on")
        acts = [e[1] for e in self.w.acts() if e[0] == "act"]
        self.assertLess(acts.index(("exec", "b", "sh", "-c", "kill 4242 2>/dev/null; exit 0")),
                        next(i for i, a in enumerate(acts) if "ssh-add -" in a[-1]))

    def test_ending_them_is_asked_first(self):
        os.environ["WK_DESTRUCTIVE"] = "1"
        rc, _, err = self.push("on")
        self.assertEqual(1, rc)
        self.assertEqual(["4242"], self.box.claude["b"])

    def test_a_declined_prompt_leaves_the_keys_out(self):
        rc, _, err = self.push("on")
        self.assertEqual(1, rc)
        self.assertEqual(set(), self.w.agents[SOCK])

    def unaskable(self, state):
        self.box.exec = lambda ws, argv, tty=False, timeout=None: Result(125, "", "exec failed")
        self.box.info = lambda ws: state

    def test_a_running_workspace_whose_scan_fails_refuses_on_naming_it(self):
        self.unaskable("running")
        os.environ["WK_YES"] = "1"
        rc, _, err = self.push("on")
        self.assertEqual(1, rc)
        self.assertIn("b", err)
        self.assertEqual(set(), self.w.agents[SOCK])

    def test_force_crosses_it_and_says_so(self):
        self.unaskable("running")
        os.environ.update(WK_YES="1", WK_FORCE="1")
        rc, _, err = self.push("on")
        self.assertEqual(0, rc, err)
        self.assertIn("FORCED", err)

    def test_a_stopped_workspace_that_cannot_be_asked_runs_nothing(self):
        self.unaskable("exited")
        self.assertEqual([], self.sessions())

    def test_only_a_pid_reaches_kill(self):
        self.box.claude = {"b": ["4242", "1;reboot", "$(id)"]}
        os.environ["WK_YES"] = "1"
        self.push("on")
        acts = [e[1] for e in self.w.acts() if e[0] == "act"]
        self.assertIn(("exec", "b", "sh", "-c", "kill 4242 2>/dev/null; exit 0"), acts)


if __name__ == "__main__":
    unittest.main()
