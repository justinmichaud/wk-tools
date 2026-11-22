import json
import os
import unittest

from tests import ROOT, agent_toy
from tests.world import World
from wk.agent import hooks, session
from wk.core.machine import Result
from wk.core.vars import VARS

HOME = "/home/u/wk"
FILES = os.path.join(ROOT, "ansible", "roles", "agents")
FIX = "wk machine setup lin"


def make(node="v22.19.0\n"):
    w = World(commands=[agent_toy])
    w.machines["lin"].on(("node", "--version"), node)
    return w


def installed(w, home=HOME):
    """What the playbook's agents role leaves on lin."""
    w.machines["lin"].files.update(dict({session.claude_bin(home): "claude", session.pi_bin(home): "pi", "/home/u/.claude/CLAUDE.md": "",
                                         "/home/u/.claude/settings.json": "{}"}, **{"%s/shims/%s" % (home, tool): "" for tool in VARS["wk_build_tools"]}))


class TestDoctor(unittest.TestCase):
    def rows(self, w, *words, **how):
        return [line.split(" | ") for line in w.wk("agentdoctor", *words, **how).out.splitlines()]

    def test_a_fresh_server_misses_everything_and_each_row_names_setup(self):
        self.assertEqual(self.rows(make(), "lin"), [
            ["claude", "missing", "no Claude Code 2.1.308 in the cache", FIX],
            ["node", "ok", "v22.19.0", ""],
            ["pi", "missing", "no pi in the cache", FIX],
            ["build-tool shims", "missing", "a shim is absent from /home/u/wk/shims", FIX],
            ["agent config", "missing", "CLAUDE.md or settings.json is absent from ~/.claude", FIX]])

    def test_after_the_agents_role_every_row_is_ok(self):
        w = make()
        installed(w)
        self.assertEqual([row[1:] for row in self.rows(w, "lin")], [["ok", "", ""], ["ok", "v22.19.0", ""]] + [["ok", "", ""]] * 3)

    def test_one_shim_absent_is_the_shims_missing(self):
        w = make()
        installed(w)
        del w.machines["lin"].files[HOME + "/shims/ninja"]
        self.assertEqual(self.rows(w, "lin")[3][1], "missing")

    def test_node_is_missing_when_absent_or_old_and_unknown_when_its_version_cannot_be_read(self):
        fix = "install node >= 22.19 and npm on lin, then: wk machine setup lin"
        for node, row in ((Result(127), ["node", "missing", "no node runs", fix]), ("v20.1.0\n", ["node", "missing", "node v20.1.0 is older than 22.19", fix]),
                          ("?\n", ["node", "unknown", "node --version printed '?'", fix])):
            self.assertEqual(self.rows(make(node=node), "lin")[1], row)
        self.assertEqual(self.rows(make(node="v24.7.0\n"), "lin")[1][1], "ok")

    def test_on_the_server_itself_the_rows_are_about_its_own_wk_home(self):
        row = self.rows(make(), "lin", on="lin", env={"WK_HOME": "/srv/wk"})[3]
        self.assertEqual(row[2], "a shim is absent from /srv/wk/shims")

    def test_the_agents_cache_is_kept_by_gc(self):
        self.assertEqual(hooks.pins(None, None), ["agents"])


class TestWhatTheRoleInstalls(unittest.TestCase):
    def read(self, *path):
        with open(os.path.join(FILES, *path)) as f:
            return f.read()

    def test_the_settings_allow_the_shell_and_the_network_and_deny_each_build_tool_by_name_and_by_path(self):
        template = self.read("templates", "settings.json.j2")
        loop = template[template.index("{% for tool in wk_build_tools %}"):template.index("{% endfor %}") + len("{% endfor %}")]
        deny = ", ".join('"Bash(%s *)", "Bash(*/%s *)"' % (tool, tool) for tool in VARS["wk_build_tools"])
        doc = json.loads(template.replace(loop, deny))
        self.assertEqual(doc["permissions"]["allow"][:3], ["Bash(*)", "WebSearch", "WebFetch"])
        self.assertEqual(doc["permissions"]["deny"][:2] + doc["permissions"]["deny"][-2:],
                         ["Bash(build-webkit *)", "Bash(*/build-webkit *)", "Bash(run-javascriptcore-tests *)", "Bash(*/run-javascriptcore-tests *)"])
        self.assertIn('"Bash({{ tool }} *)", "Bash(*/{{ tool }} *)"', loop)

    def test_the_agents_instructions_name_the_wk_commands_and_forbid_a_push(self):
        text = self.read("files", "CLAUDE.md")
        for said in ("wk build $WK_WORKSPACE <preset>", "wk run $WK_WORKSPACE -- <args>", "wk test $WK_WORKSPACE -- <tests>",
                     "wk status $WK_WORKSPACE", "Commit freely", "You cannot push, and must not try.", "The network is not restricted."):
            self.assertIn(said, text)
        for tool in VARS["wk_build_tools"]:
            self.assertIn("`%s`" % tool, text)
