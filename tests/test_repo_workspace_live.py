"""The live half of `wk new --repo wk-tools`: a real container holding wk-tools, the lint tier run in it, Claude
started in its checkout and its origin fetched, then removed; and its git transport, the push service, asked by a
container. A re-run joins a workspace already there.

    wk selftest --live ToolsWorkspaceLive
"""
import subprocess
import sys
import time
import unittest

from tests import support
from tests.support import REPO
from tests.test_dev_integration import PROMPT, REPLY, WK, place_unready, real_env, records, tail, wk, workspace_record

sys.path.insert(0, str(REPO / "lib"))
from wk import repos, secrets, wall  # noqa: E402

WS = "integ-tools"


@support.requires(place_unready, "container")
class ToolsWorkspaceLive(unittest.TestCase):
    def ok(self, *args, timeout=600):
        r = wk(*args, timeout=timeout)
        self.assertEqual(0, r.rc, "'wk %s' exited %d:\n%s" % (" ".join(args), r.rc, tail(r.out)))
        return r

    @support.owed("a push from a wk-tools workspace, with no agent running in it, reaching its repository through the push "
                  "service and its deploy key has not been run against GitHub")
    def test_a_push_reaches_the_tools_repository_through_its_deploy_key(self):
        """`origin` is wired to push through the wk-tools deploy key's alias, and `git push` there is the push service's."""
        self.made()
        self.ok("enter", WS, "--", "git", "-C", repos.Repo("wk-tools").src, "push", "--dry-run", "origin", "HEAD:refs/heads/wk-push-probe")

    def made(self):
        if workspace_record(wk("status", WS, "--records", timeout=300).out, WS) is None:
            self.ok("new", WS, "--repo", "wk-tools", timeout=1500)

    def ls_remote(self, ws, agent=False):
        line = "git ls-remote --heads $(git -C %s config remote.origin.pushurl) 2>&1" % repos.Repo("wk-tools").src
        return wk("enter", ws, "--", "bash", "-lc", wall.WITH_AGENT % line if agent else line, timeout=120)

    def test_the_push_service_names_a_container_caller_and_refuses_only_while_an_agent_runs_in_it(self):
        """`live push.container_cgroup`: the service maps the peer's pid through its cgroup to the workspace."""
        self.made()
        r = self.ls_remote(WS)
        self.assertEqual(0, r.rc, tail(r.out))
        self.assertIn("refs/heads/", r.out)
        r = self.ls_remote(WS, agent=True)
        self.assertIn("runs in", r.out, tail(r.out))
        other = next((x["name"] for x in records(wk("status", "--records", "--no-fleet", timeout=300).out)
                      if x.get("kind") == "workspace" and x.get("name", "").startswith("integ-") and x.get("name") != WS
                      and x.get("state") == "running" and x.get("method") == "container"), None)
        if other is None:
            self.skipTest("an agent in another workspace needs a second running integ-* container workspace (test_dev_integration's)")
        with subprocess.Popen([str(WK), "enter", other, "--", "bash", "-lc", wall.WITH_AGENT % "sleep 20"], env=real_env(),
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as p:
            time.sleep(5)
            r = self.ls_remote(WS)
            p.wait(60)
        self.assertEqual(0, r.rc, "an agent in '%s' refused a push from '%s':\n%s" % (other, WS, tail(r.out)))

    def test_the_proxy_gives_a_person_the_write_socket_and_an_agent_the_reading_one(self):
        """`live push.vm_keys`, its proxy half: an empty pull request is GitHub's to refuse (422) from a person, and the
        injector's (412) while an agent runs in the workspace."""
        self.made()
        fork = next(r[1] for r in secrets.forks())
        line = wall.http("https://api.github.com/repos/%s/pulls" % fork, "-X POST -d '{}' ")
        person = wk("enter", WS, "--", "bash", "-lc", line, timeout=120)
        agent = wk("enter", WS, "--", "bash", "-lc", wall.WITH_AGENT % line, timeout=120)
        self.assertEqual(("422", "412"), (wall.status_of(person.out), wall.status_of(agent.out)), tail(person.out + agent.out))

    def test_a_wk_tools_workspace_lints_runs_claude_and_fetches(self):
        self.made()
        self.ok("enter", WS, "--", "bash", "-lc", "cd %s && ./wk selftest --lint" % repos.Repo("wk-tools").src)
        r = self.ok("ai", "claude", WS, "-p", PROMPT, timeout=900)
        self.assertIn(REPLY, [l.strip() for l in r.out.splitlines()], tail(r.out))
        self.ok("sync", WS, timeout=300)
        self.ok("rm", WS, "--yes", timeout=900)


if __name__ == "__main__":
    unittest.main()
