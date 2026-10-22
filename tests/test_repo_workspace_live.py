"""The live half of `wk new --repo wk-tools`: a real container holding wk-tools, the lint tier run in it, Claude
started in its checkout and its origin fetched, then removed; and its git transport, the push service, asked by a
container as a person, as wk's own run and as an agent. A re-run joins a workspace already there.

    wk selftest --live ToolsWorkspaceLive
"""
import sys
import unittest

from tests import support
from tests.support import REPO
from tests.test_dev_integration import PROMPT, REPLY, place_unready, tail, wk, workspace_record

sys.path.insert(0, str(REPO / "lib"))
from wk import repos, secrets, wall  # noqa: E402

WS = "integ-tools"


@support.requires(place_unready, "container")
class ToolsWorkspaceLive(unittest.TestCase):
    def ok(self, *args, timeout=600):
        r = wk(*args, timeout=timeout)
        self.assertEqual(0, r.rc, "'wk %s' exited %d:\n%s" % (" ".join(args), r.rc, tail(r.out)))
        return r

    def test_a_push_reaches_the_tools_repository_through_its_deploy_key(self):
        """`origin` is wired to push through the wk-tools deploy key's alias, and `git push` there is the push service's."""
        self.made()
        self.ok("enter", WS, "--", "git", "-C", repos.Repo("wk-tools").src, "push", "--dry-run", "origin", "HEAD:refs/heads/wk-push-probe")

    def made(self):
        if workspace_record(wk("status", WS, "--records", timeout=300).out, WS) is None:
            self.ok("new", WS, "--repo", "wk-tools", timeout=1500)

    def ls_remote_line(self):
        return "git ls-remote --heads $(git -C %s config remote.origin.pushurl) 2>&1" % repos.Repo("wk-tools").src

    def test_the_push_service_serves_the_tools_repository_to_a_person_to_wks_own_run_and_to_an_agent(self):
        """`live push.container_cgroup`: the service maps the peer's pid through its cgroup to the workspace, and wk-tools is
        the one repository it serves to anyone there, an agent session included."""
        self.made()
        for who, r in (("a person's shell", wk("enter", WS, "--", "bash", "-lc", self.ls_remote_line(), timeout=120)),
                       ("wk's own run", wk("enter", WS, "--own", "--", "bash", "-lc", self.ls_remote_line(), timeout=120))):
            self.assertEqual(0, r.rc, "%s was refused:\n%s" % (who, tail(r.out)))
            self.assertIn("refs/heads/", r.out)
        r = wk("ai", "claude", WS, "-p", "Use your Bash tool to run exactly this command, then reply with its complete output verbatim "
               "and nothing else:\n" + self.ls_remote_line(), timeout=900)
        self.assertEqual(0, r.rc, tail(r.out))
        self.assertIn("refs/heads/", r.out, "the agent's own ls-remote of wk-tools was not served:\n%s" % tail(r.out))

    def test_a_fork_is_not_a_tools_workspaces_to_push(self):
        self.made()
        _, fork, alias = secrets.forks()[0]
        r = wk("enter", WS, "--", "bash", "-lc", "git ls-remote --heads git@%s:%s.git 2>&1" % (alias, fork), timeout=120)
        self.assertNotEqual(0, r.rc)
        self.assertIn("is not a push or fetch of this workspace's repositories", r.out)

    def test_the_proxy_gives_a_person_the_write_socket_and_wks_own_run_the_reading_one(self):
        """`live push.vm_keys`, its proxy half: an empty pull request is GitHub's to refuse (422) from a person, and the
        injector's (412) from a run of wk's own, the same rule the push service answers for a container's chain."""
        self.made()
        fork = next(r[1] for r in secrets.forks())
        line = wall.http("https://api.github.com/repos/%s/pulls" % fork, "-X POST -d '{}' ")
        person = wk("enter", WS, "--", "bash", "-lc", line, timeout=120)
        own = wk("enter", WS, "--own", "--", "bash", "-lc", line, timeout=120)
        self.assertEqual(("422", "412"), (wall.status_of(person.out), wall.status_of(own.out)), tail(person.out + own.out))

    def test_a_wk_tools_workspace_lints_runs_claude_and_fetches(self):
        self.made()
        self.ok("enter", WS, "--", "bash", "-lc", "cd %s && ./wk selftest --lint" % repos.Repo("wk-tools").src)
        r = self.ok("ai", "claude", WS, "-p", PROMPT, timeout=900)
        self.assertIn(REPLY, [l.strip() for l in r.out.splitlines()], tail(r.out))
        self.ok("sync", WS, timeout=300)
        self.ok("rm", WS, "--yes", timeout=900)


if __name__ == "__main__":
    unittest.main()
