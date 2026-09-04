"""Nothing an agent runs can publish. The proxy now *allows* api.github.com and
hands it to the credential injector, so the refusal has moved: the token the
injector would add is not there while push is off, the deploy keys are in an
ssh-agent nothing in a workspace can take a key out of, `wk verify` measures
both from inside, `wk ai claude` holds push back before it verifies (and
refuses a build box that holds a gh login), and `wk push on` is refused while a
claude process runs in any workspace. The person at the keyboard is the only
publisher.

Run: python3 -m unittest tests.test_no_publish -v
"""
import importlib.util
import re
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, bash

PROXY = REPO / "container" / "proxy" / "wk-proxy.py"
VERIFY = (REPO / "cmd" / "verify").read_text()
CLAUDE = (REPO / "cmd" / "ai").read_text()
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


class TestVerifyMeasuresThatNothingCanPublish(unittest.TestCase):
    """What each probe answers is tests/test_verify_credentials.py's subject,
    driven. What is held here is that the probes exist at all -- reaching the
    API, reading it, the write table, and the switch over writing."""

    def test_every_side_of_the_api_is_measured(self):
        """Reaching api.github.com is the state and not a fault, and neither is
        an authenticated read: what is measured is that a write outside the
        table never leaves the injector, and that an allowed one is
        authenticated only while push is on."""
        self.assertIn("https://github.com/", VERIFY)
        self.assertRegex(VERIFY, r"curl [^\n]*https://api\.github\.com/ ")
        self.assertIn("https://api.github.com/user", VERIFY)
        self.assertIn("https://api.github.com/repos/$FORK/keys", VERIFY)
        self.assertIn("https://api.github.com/repos/$FORK/pulls", VERIFY)
        self.assertIn("401", VERIFY)
        self.assertIn("422", VERIFY)

    def test_the_old_claim_that_the_api_is_refused_is_gone(self):
        self.assertNotIn("api.github.com is refused", VERIFY)

    def test_key_material_and_gh_credentials_fail_the_sandbox(self):
        self.assertIn("PRIVATE KEY", VERIFY)
        self.assertIn("ssh-add -l", VERIFY)
        self.assertIn("~/.config/gh/hosts.yml", VERIFY)
        self.assertIn("GITHUB_TOKEN|GH_ENTERPRISE_TOKEN", VERIFY)
        self.assertIn("wk-injects-this", VERIFY)

    def test_gh_holding_the_placeholder_is_what_is_measured_now(self):
        """`gh` reaches GitHub through the injector like everything else, so
        the check is that its token is the placeholder -- not that it has
        none, which would be `gh` unable to read a pull request."""
        self.assertIn("for _var in GITHUB_COM_TOKEN GH_TOKEN", VERIFY)
        self.assertNotIn("gh auth status", VERIFY)


class TestClaudeHoldsPushBackBeforeVerifying(unittest.TestCase):
    """The switch itself -- that it is thrown for every target, and what it
    records -- is tests/test_agent_push_switch.py's subject, driven rather than
    read. What is held here is the *order*: the keys are gone before anything
    measures the mount or hands over control."""

    def test_push_off_precedes_wk_verify(self):
        off = CLAUDE.index('push_hold_back "$NAME"')
        verify = CLAUDE.index('WK_NAME="$NAME" "$WK_ROOT/cmd/verify"')
        self.assertLess(off, verify, "the keys must be gone before wk verify measures the mount")

    def test_the_switch_is_thrown_on_a_build_box_too(self):
        """A build box keeps its own keys under its own wk root, so every
        `wk push` this command makes carries the target when there is one."""
        self.assertIn('push "$1" ${PUSH_TARGET:+--target "$PUSH_TARGET"}', CLAUDE)
        self.assertIn('[ "$WK_TARGET_KIND" != remote ] || PUSH_TARGET="$TARGET"', CLAUDE)

    def test_a_gh_login_on_a_build_box_is_a_refusal_not_a_barrier(self):
        m = re.search(r'\[ "\$WK_TARGET_KIND" = remote \][^\n]*\n[^\n]*gh auth status[^\n]*\n\s*die ', CLAUDE)
        self.assertIsNotNone(m, "no `die` on a gh login found in the remote path")
        self.assertIn("gh auth logout", CLAUDE)

    def test_remote_control_never_turns_push_back_on(self):
        """A background server left running unattended: there is no foreground
        moment to notice a session ending, so the keys stay held back and
        `restore_push` is never armed for it."""
        rc = CLAUDE[CLAUDE.index("# --- wk ai claude <ws> --rc"):
                    CLAUDE.index("wk_atexit restore_push")]
        self.assertNotIn("push_switch on", rc, "remote-control must leave push off")
        self.assertIn('PUSH_WAS_ON=""', rc, "remote-control must disarm restore_push")


class TestPushOnRefusesWhileAnAgentRuns(unittest.TestCase):
    def _sessions(self, t_list_out, running):
        # agent_sessions now probes with `t_exec "$ws" sh -c '...'`; the stub
        # answers by workspace name ($1), standing in for "a claude exe is
        # running in that container".
        return bash(f'''
. "{REPO}/lib/common.sh"
t_list() {{ printf '%b' "{t_list_out}"; }}
t_exec() {{ case "$1" in {running}) return 0 ;; *) return 1 ;; esac; }}
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

    def test_on_asks_before_loading_the_agent(self):
        on = PUSH[PUSH.index("\non)\n"):PUSH.index("\noff)\n")]
        self.assertLess(on.index("agent_sessions"), on.index("push_agent_load"))
        # a forceable barrier, not an unconditional die: `wk push on --force`
        # crosses it while a session runs.
        self.assertIn("barrier ", on)


def _lift(func):
    import subprocess
    text = subprocess.run(["sed", "-n", f"/^{func}()/,/^}}/p", str(REPO / "cmd" / "push")],
                          capture_output=True, text=True).stdout
    assert text.strip(), func
    return text


if __name__ == "__main__":
    unittest.main()
