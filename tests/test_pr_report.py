"""`wk pr report`: the person's week on WebKit/WebKit, read through gh's JSON (lib/wk/pr.py `weekly`) from a Fake
machine answering each gh call."""
import contextlib
import datetime
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import unittest
from unittest import mock

from tests.support import REPO, as_dispatched

sys.path.insert(0, str(REPO / "lib"))
from wk import pr  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Fake  # noqa: E402

CMD_LOADER = importlib.machinery.SourceFileLoader("cmd_pr_report", str(REPO / "cmd" / "pr"))
CMD = importlib.util.module_from_spec(importlib.util.spec_from_loader("cmd_pr_report", CMD_LOADER))
CMD_LOADER.exec_module(CMD)

TODAY = datetime.date(2026, 10, 4)
REPO_ARGS = ["--repo", "WebKit/WebKit"]


def _pr(n, title, author="me", state="OPEN"):
    return {"number": n, "title": title, "url": "https://github.com/WebKit/WebKit/pull/%d" % n, "state": state,
            "author": {"login": author}, "createdAt": "", "updatedAt": ""}


def world(since="2026-09-27"):
    m = Fake()
    m.answer(["gh", "api", "user"], out=json.dumps({"login": "me"}))
    mine = ["gh", "pr", "list"] + REPO_ARGS + ["--author", "me", "--state", "all", "--search"]
    m.answer(mine + ["created:>=" + since], out=json.dumps([_pr(2, "new one")]))
    m.answer(mine + ["updated:>=" + since], out=json.dumps([_pr(2, "new one"), _pr(1, "old one", state="MERGED")]))
    search = ["gh", "search", "prs"] + REPO_ARGS
    m.answer(search + ["--reviewed-by", "me"], out=json.dumps([_pr(5, "theirs", "alice"), _pr(1, "old one")]))
    m.answer(search + ["--commenter", "me"], out=json.dumps([_pr(5, "theirs", "alice"), _pr(6, "asked", "bob")]))
    return m


class TestWeekly(unittest.TestCase):
    def report(self, m, since="2026-09-27"):
        out = io.StringIO()
        self.assertEqual(0, pr.weekly(m, since, TODAY, out))
        return out.getvalue()

    def section(self, text, title):
        body = text.split("\n%s: " % title, 1)[1].split("\n\n", 1)[0]
        return int(body.split("\n", 1)[0]), [int(l.split()[0][1:]) for l in body.splitlines()[1:] if l.lstrip().startswith("#")]

    def test_each_section_counts_its_own_pull_requests_once(self):
        text = self.report(world())
        self.assertTrue(text.startswith("@me on WebKit/WebKit, 2026-09-27 to 2026-10-04 (week 40)\n"), text)
        self.assertEqual(self.section(text, "pull requests created"), (1, [2]))
        self.assertEqual(self.section(text, "pull requests updated"), (1, [1]))
        self.assertEqual(self.section(text, "reviewed"), (1, [5]))
        self.assertEqual(self.section(text, "commented on"), (1, [6]))
        self.assertIn("#1      merged  old one", text)

    def test_every_gh_call_asks_for_json_since_the_date(self):
        m = world()
        self.report(m)
        calls = [e[1] for e in m.effects if e[0] == "run" and e[1][:2] != ("gh", "api")]
        self.assertEqual(len(calls), 4)
        for argv in calls:
            self.assertIn("--json", argv)
        self.assertEqual(sum(1 for a in calls if ">=2026-09-27" in a), 2)

    def test_a_failing_gh_names_the_call(self):
        m = Fake()
        m.answer(["gh", "api", "user"], rc=1, err="HTTP 401: Bad credentials")
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(Refused):
            pr.weekly(m, "2026-09-27", TODAY, io.StringIO())
        self.assertIn("gh api user failed: HTTP 401", err.getvalue())


class TestSince(unittest.TestCase):
    def test_the_default_is_seven_days_back_and_a_date_is_checked(self):
        self.assertEqual(pr.since_date(None, TODAY), "2026-09-27")
        self.assertEqual(pr.since_date("2026-09-01", TODAY), "2026-09-01")
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(Refused):
            pr.since_date("last week", TODAY)
        self.assertIn("--since takes a date as YYYY-MM-DD", err.getvalue())

    def test_the_command_reads_since_off_its_declaration(self):
        m = world("2026-09-01")
        out = io.StringIO()
        with mock.patch.object(CMD, "Local", return_value=m), contextlib.redirect_stdout(out):
            rc = CMD.main(as_dispatched("pr", ["report", "--since", "2026-09-01"], dict(os.environ)))
        self.assertEqual(rc, 0)
        self.assertIn("@me on WebKit/WebKit, 2026-09-01 to", out.getvalue())


if __name__ == "__main__":
    unittest.main()
