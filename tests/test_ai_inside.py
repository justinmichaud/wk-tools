"""`wk ai claude` typed *inside* a workspace: a macOS guest's, whose wall is measured from in there."""
import os
import shlex
import types
import unittest
from unittest import mock

from tests.support import REPO, WkTest, bash
from tests.test_ai import CLAUDE, AiTest, Walled
from tests.test_wk_workspace import said
from wk import repos, wall  # noqa: E402  (tests.fakes puts lib/ on the path)

MAC = os.uname_result(("Darwin", "guest", "25.0.0", "", "arm64"))


class Inside(Walled):
    """In guest `ws`, where no agent runs: a command runs in a login shell here, the push client is wk-tools' own, and the
    push service and the injector serve this process as a person's shell."""

    def _local(self):
        super()._local()
        self.react(["bash", "-lc"], lambda a, f: f.inside(shlex.split(a[2])[1:]))
        self.wall.update({"git config --global --get core.sshCommand": "%s/container/push/wk-push-client.py" % REPO,
                          "git ls-remote": "0123abcd\trefs/heads/main", "/pulls": "422"})


class InsideTest(AiTest):
    world_class = Inside

    def setUp(self):
        p = mock.patch("os.uname", return_value=MAC)
        p.start()
        self.addCleanup(p.stop)
        super().setUp()
        self.w = self.make_world(kinds={"fakebox": "local"})

    def session(self, w=None):
        (argv,) = [e[1] for e in self.w.effects if e[0] == "run_tty"]
        return argv[:2] + (argv[2].split(" WK_SESSION=")[0],) + (shlex.split(argv[2])[-1],)


class TestFromInside(InsideTest):
    def test_the_wall_is_measured_from_in_here_and_the_session_starts_unwalled(self):
        rc, err = self.ai("claude")
        self.assertEqual((rc, err.splitlines()[0], said(err)), (0, "==> checking workspace 'ws' from inside it",
                                                                "==> starting Claude in 'ws' (permissions relaxed; the workspace is the boundary)"))
        self.assertEqual((self.w.asked[0], self.w.asked[-1], "have bwrap" in self.w.asked), ("agents.sh find claude", "session", False))
        self.assertEqual(self.session(), ("bash", "-lc", "exec env WK_AGENT=claude WK_WORKSPACE=ws",
                                          "cd /src/WebKit && exec %s --dangerously-skip-permissions" % CLAUDE))

    def test_a_sandbox_failure_is_a_barrier_wk_force_crosses(self):
        """In here every word after the agent's name is the agent's, so the barrier is crossed as `WK_FORCE=1 wk ai claude`."""
        self.w.wall["rest/version"] = "000"
        rc, err = self.ai("claude")
        self.assertEqual((rc, said(err), self.misses(err)), (1, "error: the sandbox around 'ws' is not intact (see above), and relaxed",
                                                             ["bugs.webkit.org answered '000' -- the injector is not in the path for it"]))
        self.w.env["WK_FORCE"] = "1"
        self.assertEqual((self.ai("claude")[0], self.w.asked[-1]), (0, "session"))

    def test_a_way_to_publish_is_not_forceable_and_names_the_host_side_doctor(self):
        self.w.wall["/pulls"] = "412"
        for force in ("", "1"):
            self.w.env["WK_FORCE"] = force
            rc, err = self.ai("claude")
            self.assertEqual((rc, said(err), "session" in self.w.asked), (1, "error: refusing to run: an agent in 'ws' could publish (see above).", False), force)
            self.assertIn("    Nothing in here can fix it and --force does not cross it; 'wk doctor ws' on the host says which part fails", err.splitlines())


class TestTheFunctionDoesNotShadowTheRealCli(WkTest):
    def _what_claude_is(self, interactive, workspace=True):
        home = self.tmp / f"home-{interactive}-{workspace}"
        (home / ".local" / "bin").mkdir(parents=True, exist_ok=True)
        if workspace:
            (home / ".wk-workspace").write_text("name=demo\nsrc=/src\n")
        real = home / ".local" / "bin" / "claude"
        real.write_text("#!/bin/sh\necho REAL-CLI\n")
        real.chmod(0o755)
        flags = "-ic" if interactive else "-c"
        cp = bash(
            f'bash {flags} \'. "{REPO}/shell/bashrc" >/dev/null 2>&1; '
            f'type -t claude; type claude\' 2>/dev/null',
            env={"HOME": str(home), "NO_ZSH": "1", "TERM": "dumb",
                 "PATH": f"{home}/.local/bin:/usr/bin:/bin"})
        return cp.stdout

    def test_only_an_interactive_shell_in_a_workspace_gets_the_measured_start(self):
        for interactive, workspace in ((True, True), (False, True), (True, False)):
            with self.subTest(interactive=interactive, workspace=workspace):
                out = self._what_claude_is(interactive, workspace)
                if interactive and workspace:
                    self.assertIn("function", out)
                    self.assertIn("wk ai claude", out)
                else:
                    self.assertIn("file", out)
                    self.assertNotIn("wk ai claude", out)


class TestNotReachableFromTheCli(unittest.TestCase):
    """Typed in a Linux workspace, the wall's push rows read this process's own /proc, which no world answers."""

    def test_a_linux_workspace_walls_its_own_sessions_commits_unless_it_holds_the_agents_repository(self):
        for repo, walled in ((repos.default(), True), (repos.Repo("wk-tools"), False)):
            driver = types.SimpleNamespace(kind="local", os=lambda: "linux", repo=lambda ws, r=repo: r)
            self.assertEqual(wall.commit_walled(driver, "ws"), walled, repo.name)


if __name__ == "__main__":
    unittest.main()
