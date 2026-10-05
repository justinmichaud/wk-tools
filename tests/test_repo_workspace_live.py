"""The live half of `wk new --repo wk-tools`: a real container holding wk-tools, the lint tier run in it, Claude
started in its checkout and its origin fetched, then removed. A re-run joins a workspace already there.

    wk selftest --live ToolsWorkspaceLive
"""
import sys
import unittest

from tests import support
from tests.support import REPO
from tests.test_dev_integration import PROMPT, REPLY, place_unready, tail, wk, workspace_record

sys.path.insert(0, str(REPO / "lib"))
from wk import repos  # noqa: E402

WS = "integ-tools"


@support.requires(place_unready, "container")
class ToolsWorkspaceLive(unittest.TestCase):
    def ok(self, *args, timeout=600):
        r = wk(*args, timeout=timeout)
        self.assertEqual(0, r.rc, "'wk %s' exited %d:\n%s" % (" ".join(args), r.rc, tail(r.out)))
        return r

    def test_a_wk_tools_workspace_lints_runs_claude_and_fetches(self):
        if workspace_record(wk("status", WS, "--records", timeout=300).out, WS) is None:
            self.ok("new", WS, "--repo", "wk-tools", timeout=1500)
        self.ok("enter", WS, "--", "bash", "-lc", "cd %s && ./wk selftest --lint" % repos.Repo("wk-tools").src)
        r = self.ok("ai", "claude", WS, "-p", PROMPT, timeout=900)
        self.assertIn(REPLY, [l.strip() for l in r.out.splitlines()], tail(r.out))
        self.ok("sync", WS, timeout=300)
        self.ok("rm", WS, "--yes", timeout=900)


if __name__ == "__main__":
    unittest.main()
