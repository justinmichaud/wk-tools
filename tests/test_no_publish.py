"""Nothing an agent runs can publish: the proxy's GitHub rules, and `wk key push on` ending any running claude session first."""
import importlib.util
import os
import tempfile
import unittest

from tests.support import REPO
from tests.test_push_switch import PushTest, registry
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
    def test_the_api_is_allowed_only_on_443_and_only_through_the_injector(self):
        ok, why = _policy().host_allowed("api.github.com", 443)
        self.assertTrue(ok, why)
        self.assertIn("injector", why)

    def test_uploads_other_api_ports_and_lookalikes_are_refused_and_github_itself_stays_allowed(self):
        p = _policy()
        for host, port, allowed in (("uploads.github.com", 443, False), ("api.github.com", 22, False),
                                    ("api.github.com", 80, False), ("evilapi.github.com.attacker.net", 443, False),
                                    ("api.github.com.attacker.net", 443, False), ("github.com", 443, True),
                                    ("github.com", 22, True), ("codeload.github.com", 443, True),
                                    ("raw.githubusercontent.com", 443, True)):
            with self.subTest(host=host, port=port):
                self.assertEqual(p.host_allowed(host, port)[0], allowed)


class TestPushOnEndsAnyRunningAgent(PushTest):
    def setUp(self):
        super().setUp()
        self.w.seed()
        self.box.claude = {"a": [], "b": ["4242"]}

    def sessions(self):
        return [ws for _, ws, _ in pushswitch.Push(registry(self.w, self.boxes), self.w.sec(), self.clock).agent_sessions()]

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
        self.assertFalse([a for a in acts if "reboot" in " ".join(a) or "$(id)" in " ".join(a)], acts)


if __name__ == "__main__":
    unittest.main()
