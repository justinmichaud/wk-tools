"""Nothing an agent runs can publish: the proxy's GitHub rules."""
import importlib.util
import tempfile
import unittest

from tests.support import REPO

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
                                    ("github.com", 22, False), ("codeload.github.com", 443, True),
                                    ("raw.githubusercontent.com", 443, True)):
            with self.subTest(host=host, port=port):
                self.assertEqual(p.host_allowed(host, port)[0], allowed)


if __name__ == "__main__":
    unittest.main()
