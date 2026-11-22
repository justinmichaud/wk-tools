import json
import unittest

from tests import agent_toy
from tests.killpoints import converges, dry_run_is_the_plan
from tests.world import World
from wk.agent import install
from wk.core.machine import Result

HOME = "/home/u/wk"
CACHE = HOME + "/cache/agents"
STAGE = CACHE + "/claude/.stage"
VERSION = install.CLAUDE_VERSION
FRESH = "claude: changed\npi: changed\nbuild-tool shims: changed\nagent config: changed\n"


def make(on="lin", node="v22.19.0\n"):
    """A world whose server answers the installers as the real ones do."""
    w = World(commands=[agent_toy])
    m = w.machines[on]

    def move(call):
        for path in [p for p in m.files if p.startswith(call.argv[4] + "/")]:
            m.files[call.argv[5] + path[len(call.argv[4]):]] = m.files.pop(path)
    m.on(("curl",), lambda call: m.files.update({call.argv[4]: "installer"}))
    m.on(("bash",), lambda call: m.files.update({"%s/.local/share/claude/versions/%s" % (call.env["HOME"], call.argv[2]): "claude"}))
    m.on(("sh", "-c", install.MOVE), move)
    m.on(("node", "--version"), node)
    m.on(("npm",), lambda call: m.files.update({call.argv[4] + "/node_modules/.bin/pi": "pi"}))
    return w


class TestSetup(unittest.TestCase):
    def test_a_fresh_server_gets_claude_pi_the_shims_and_the_config_in_that_order(self):
        w = make()
        r = w.wk("agentsetup", "lin")
        self.assertEqual((r.code, r.out), (0, FRESH))
        self.assertEqual(r.effects[:6], [
            "[lin] remove %s" % STAGE,
            "[lin] mkdir -p %s" % STAGE,
            "[lin] curl -sSL --fail-with-body -o %s/install.sh https://claude.ai/install.sh" % STAGE,
            "[lin] bash %s/install.sh %s" % (STAGE, VERSION),
            "[lin] sh -c '%s' sh %s %s/claude/%s %s" % (install.MOVE, STAGE, CACHE, VERSION, VERSION),
            "[lin] npm install --ignore-scripts --prefix %s/pi @earendil-works/pi-coding-agent" % CACHE])
        self.assertEqual(r.effects[6:], ["[lin] write %s/shims/%s" % (HOME, tool) for tool in sorted(install.TOOLS)]
                         + ["[lin] write /home/u/.claude/CLAUDE.md", "[lin] write /home/u/.claude/settings.json"])
        m = w.machines["lin"]
        self.assertEqual([c.env for c in m.calls if c.argv[0] == "bash"], [{"HOME": STAGE}])
        self.assertEqual((m.exists(install.claude_bin(HOME)), m.exists(install.pi_bin(HOME)), m.exists(STAGE)), (True, True, False))
        self.assertEqual({m.modes["%s/shims/%s" % (HOME, tool)] for tool in install.TOOLS}, {"755"})

    def test_a_second_run_changes_nothing(self):
        w = make()
        w.wk("agentsetup", "lin")
        r = w.wk("agentsetup", "lin")
        self.assertEqual((r.out, r.effects), (FRESH.replace("changed", "unchanged"), []))

    def test_killed_after_any_effect_and_run_again_it_converges(self):
        converges(self, make, "agentsetup", "lin")

    def test_a_dry_run_is_the_plan(self):
        dry_run_is_the_plan(self, make, "agentsetup", "lin")

    def test_a_changed_shim_or_config_file_is_written_again_alone(self):
        w = make()
        w.wk("agentsetup", "lin")
        w.machines["lin"].files[HOME + "/shims/ninja"] = "edited"
        del w.machines["lin"].files["/home/u/.claude/settings.json"]
        self.assertEqual(w.wk("agentsetup", "lin").effects, ["[lin] write %s/shims/ninja" % HOME, "[lin] write /home/u/.claude/settings.json"])

    def test_a_step_of_the_claude_install_that_fails_is_refused_with_its_error_and_the_remedy(self):
        for tool, said in (("curl", "curl: (22) The requested URL returned error: 403"), ("bash", "unsupported platform")):
            w = make()
            w.machines["lin"].on((tool,), Result(22, "", said + "\n"))
            r = w.wk("agentsetup", "lin")
            self.assertEqual(r.code, 1)
            self.assertIn("error: could not install Claude Code %s on lin: `%s` failed (exit 22): %s\n"
                          "    claude.ai must answer from lin; then: wk machine setup lin\n" % (VERSION, tool, said), r.err)
            self.assertFalse(w.machines["lin"].exists(install.claude_bin(HOME)))

    def test_an_installer_that_left_no_binary_is_not_moved_into_place(self):
        w = make()
        w.machines["lin"].on(("sh", "-c", install.MOVE), Result(1))
        r = w.wk("agentsetup", "lin")
        self.assertIn("`sh` failed (exit 1)", r.err)

    def test_without_a_new_enough_node_pi_is_left_out_with_a_warning_and_the_rest_goes_on(self):
        for node, why in ((Result(127, "", "no node"), "no node runs"), ("v22.18.1\n", "node v22.18.1 is older than 22.19"),
                          ("nonsense\n", "node --version printed 'nonsense'")):
            w = make(node=node)
            r = w.wk("agentsetup", "lin")
            self.assertEqual((r.code, r.out), (0, FRESH.replace("pi: changed", "pi: unchanged")))
            self.assertIn("warning: pi is not installed on lin: it needs node >= 22.19 and npm, and %s\n" % why, r.err)
            self.assertFalse([e for e in r.effects if "npm" in e])

    def test_a_newer_major_node_is_enough(self):
        self.assertEqual(make(node="v24.7.0\n").wk("agentsetup", "lin").out, FRESH)

    def test_an_npm_that_fails_is_refused_with_its_error_and_the_remedy(self):
        w = make()
        w.machines["lin"].on(("npm",), Result(1, "", "npm error network\n"))
        r = w.wk("agentsetup", "lin")
        self.assertIn("error: could not install pi on lin: npm failed (exit 1): npm error network\n"
                      "    registry.npmjs.org must answer from lin; then: wk machine setup lin\n", r.err)

    def test_a_mac_and_a_borrowed_box_are_set_up_the_same_way(self):
        for on in ("mac", "box"):
            self.assertEqual(make(on).wk("agentsetup", on).out, FRESH)

    def test_the_client_and_the_credential_server_run_no_agent_and_get_nothing(self):
        for name in ("desk", "vault"):
            r = make().wk("agentsetup", name)
            self.assertEqual((r.out, r.calls), ("", []))


class TestDoctor(unittest.TestCase):
    def rows(self, w, *words, **how):
        return [line.split(" | ") for line in w.wk("agentdoctor", *words, **how).out.splitlines()]

    def test_a_fresh_server_misses_everything_and_each_row_names_setup(self):
        fix = "wk machine setup lin"
        self.assertEqual(self.rows(make(), "lin"), [
            ["claude", "missing", "no Claude Code %s in the cache" % VERSION, fix],
            ["node", "ok", "v22.19.0", ""],
            ["pi", "missing", "no pi in the cache", fix],
            ["build-tool shims", "missing", "a shim under /home/u/wk/shims is absent or differs", fix],
            ["agent config", "missing", "CLAUDE.md or settings.json under ~/.claude is absent or differs", fix]])

    def test_after_setup_every_row_is_ok(self):
        w = make()
        w.wk("agentsetup", "lin")
        self.assertEqual([row[1:] for row in self.rows(w, "lin")], [["ok", "", ""], ["ok", "v22.19.0", ""]] + [["ok", "", ""]] * 3)

    def test_node_is_missing_when_absent_or_old_and_unknown_when_its_version_cannot_be_read(self):
        fix = "install node >= 22.19 and npm on lin, then: wk machine setup lin"
        for node, row in ((Result(127), ["node", "missing", "no node runs", fix]), ("v20.1.0\n", ["node", "missing", "node v20.1.0 is older than 22.19", fix]),
                          ("?\n", ["node", "unknown", "node --version printed '?'", fix])):
            self.assertEqual(self.rows(make(node=node), "lin")[1], row)

    def test_on_the_server_itself_the_rows_are_about_its_own_wk_home(self):
        row = self.rows(make(), "lin", on="lin", env={"WK_HOME": "/srv/wk"})[3]
        self.assertEqual(row[2], "a shim under /srv/wk/shims is absent or differs")

    def test_a_machine_that_holds_no_workspace_has_no_rows(self):
        self.assertEqual(self.rows(make(), "vault"), [])


class TestConfig(unittest.TestCase):
    def test_settings_allow_the_shell_and_the_network_and_deny_each_build_tool_by_name_and_by_path(self):
        doc = json.loads(install.settings())
        self.assertEqual(doc["permissions"]["allow"][:3], ["Bash(*)", "WebSearch", "WebFetch"])
        self.assertEqual(doc["permissions"]["deny"][:2] + doc["permissions"]["deny"][-2:],
                         ["Bash(build-webkit *)", "Bash(*/build-webkit *)", "Bash(run-javascriptcore-tests *)", "Bash(*/run-javascriptcore-tests *)"])
        self.assertEqual(len(doc["permissions"]["deny"]), 2 * len(install.TOOLS))

    def test_the_agents_instructions_name_the_wk_commands_and_forbid_a_push(self):
        text = install.source("CLAUDE.md")
        for said in ("wk build $WK_WORKSPACE <preset>", "wk run $WK_WORKSPACE -- <args>", "wk test $WK_WORKSPACE <args>",
                     "wk status $WK_WORKSPACE", "Commit freely", "You cannot push, and must not try.", "The network is not restricted."):
            self.assertIn(said, text)

    def test_the_start_up_answers_are_added_to_what_claude_already_keeps(self):
        new = json.loads(install.answered('{"numStartups": 3, "projects": {"/other": {"x": 1}}}', "/w/src"))
        self.assertEqual(new, {"numStartups": 3, "hasCompletedOnboarding": True, "remoteDialogSeen": True,
                               "projects": {"/other": {"x": 1}, "/w/src": {"hasTrustDialogAccepted": True}}})

    def test_no_file_is_an_empty_one_and_an_answered_one_is_left(self):
        text = install.answered(None, "/w/src")
        self.assertEqual((sorted(json.loads(text)), install.answered(text, "/w/src")), (["hasCompletedOnboarding", "projects", "remoteDialogSeen"], None))
