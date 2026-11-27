import json
import shlex
import unittest

from tests import agent_toy
from tests.world import CONFS, World
from wk.agent import session
from wk.core.machine import ALIVE, Result
from wk.core.vars import VARS

HOME = "/home/u/wk"
SRC = HOME + "/ws/x/src"
INJECTED = {"CLAUDE_SECURESTORAGE_CONFIG_DIR": HOME + "/claude-login", "DISABLE_AUTOUPDATER": "1", "HTTPS_PROXY": "http://vault:3128",
            "NODE_EXTRA_CA_CERTS": HOME + "/injector-ca.pem", "https_proxy": "http://vault:3128"}
CLAUDE = [session.claude_bin(HOME), "--dangerously-skip-permissions", "--remote-control", "x"]
PI = [session.pi_bin(HOME)]
ANSWERED = session.answered(None, SRC)
KEYS = {fork: "-----BEGIN OPENSSH PRIVATE KEY-----\n%s\n-----END OPENSSH PRIVATE KEY-----\n" % fork for fork in VARS["wk_forks"]}
ASK = ["ssh", "-i", HOME + "/secrets/rotate-key", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes"] + ALIVE + ["vault"]


def renewed(on):
    """What a rotation does on the server `on`: the controller asked, and each fork's key written."""
    return (["[%s] %s" % (on, shlex.join(ASK))]
            + ["[%s] write %s/secrets/deploy/%s" % (on, HOME, fork.replace("/", "--")) for fork in sorted(KEYS)])


def make(on="lin", state="ready", confs=CONFS):
    """A world whose server `on` holds the workspace x and both agents, and is where the commands are typed."""
    w = World(here=on, commands=[agent_toy], confs=confs)
    w.workspace("x", on=on, state=state)
    w.machines[on].files.update({session.claude_bin(HOME): "claude", session.pi_bin(HOME): "pi"})
    w.machines[on].on(("ssh", "-i"), json.dumps(KEYS))
    return w


def line(on, argv, env, tty=False):
    """The shell `wk enter` opens, running the agent under `env`."""
    return "[%s] %s" % (on, shlex.join(["fake-enter"] + ["-t"] * tty + ["x", "env", "WK_WORKSPACE=x"] + ["%s=%s" % kv for kv in sorted(env.items())] + argv))


class TestStart(unittest.TestCase):
    def test_claude_is_started_in_the_login_shell_on_the_terminal_through_the_injector_with_remote_control_named_for_the_workspace(self):
        w = make()
        r = w.wk("ai", "claude", "x", tty=True)
        self.assertEqual(r.calls, ["[lin] write /home/u/.claude.json"] + renewed("lin") + [line("lin", CLAUDE, INJECTED, tty=True)])
        self.assertEqual((r.code, r.effects), (0, r.calls))
        self.assertEqual((w.machines["lin"].read("/home/u/.claude.json"), w.machines["lin"].modes["/home/u/.claude.json"]), (ANSWERED, "600"))
        self.assertEqual(r.err, "==> rotated the push keys of %s\n==> starting claude in x\n" % ", ".join(sorted(KEYS)))

    def test_pi_gets_no_proxy_no_ca_and_no_claude_state(self):
        r = make().wk("ai", "pi", "x", tty=True)
        self.assertEqual(r.calls, renewed("lin") + [line("lin", PI, {}, tty=True)])
        self.assertNotIn("PROXY", "".join(r.calls).upper())

    def test_a_mac_starts_it_the_same_way(self):
        r = make("mac").wk("ai", "claude", "x", tty=True)
        self.assertEqual(r.calls[-1], line("mac", CLAUDE, INJECTED, tty=True))

    def test_the_words_after_the_dashes_go_to_the_agent_and_its_exit_code_comes_back(self):
        w = make()
        w.machines["lin"].on(("fake-enter", "-t"), Result(3))
        r = w.wk("ai", "claude", "x", "--", "-r", "--model", "opus", tty=True)
        self.assertEqual((r.code, r.calls[-1]), (3, line("lin", CLAUDE + ["-r", "--model", "opus"], INJECTED, tty=True)))

    def test_given_words_and_no_terminal_the_agent_runs_without_one(self):
        r = make().wk("ai", "claude", "x", "--", "-p", "say OK")
        self.assertEqual((r.code, r.calls[-1]), (0, line("lin", CLAUDE + ["-p", "say OK"], INJECTED)))
        self.assertNotIn("fake-enter -t", r.calls[-1])

    def test_no_words_and_no_terminal_is_refused_naming_the_prompt_form(self):
        r = make().wk("ai", "claude", "x")
        self.assertEqual((r.code, r.calls), (4, []))
        self.assertEqual(r.err, "error: an agent's session needs a terminal and there is none: give the agent its prompt, "
                                "`wk ai claude x -- -p <prompt>`\n")

    def test_the_injectors_port_is_the_controllers_conf_when_it_names_one(self):
        r = make(confs=dict(CONFS, vault=CONFS["vault"] + "\ninjector_port=9000")).wk("ai", "claude", "x", tty=True)
        self.assertIn("HTTPS_PROXY=http://vault:9000 ", r.calls[-1])
        self.assertIn("https_proxy=http://vault:9000 ", r.calls[-1])

    def test_it_is_refused_on_a_workstation(self):
        r = World(commands=[agent_toy]).wk("ai", "claude", "x", tty=True)
        self.assertEqual((r.code, r.calls), (1, []))
        self.assertIn("error: wk ai runs on a Linux server or a Mac server (box, lin, mac), and this is desk", r.err)

    def test_answers_claude_already_holds_are_not_written_again(self):
        w = make()
        w.machines["lin"].files["/home/u/.claude.json"] = ANSWERED
        self.assertEqual(w.wk("ai", "claude", "x", tty=True).effects, renewed("lin") + [line("lin", CLAUDE, INJECTED, tty=True)])

    def test_a_dry_run_asks_for_no_rotation_and_starts_nothing(self):
        r = make().wk("--dry-run", "ai", "claude", "x", tty=True)
        self.assertEqual((r.code, r.effects, r.plan), (0, [], ["[lin] write /home/u/.claude.json"] + renewed("lin")[:1]
                                                                                   + [line("lin", CLAUDE, INJECTED, tty=True)]))


class TestRotation(unittest.TestCase):
    def test_the_keys_are_written_whole_0600_where_the_ssh_aliases_name_them(self):
        w = make()
        w.wk("ai", "pi", "x", tty=True)
        for fork, key in KEYS.items():
            path = "%s/secrets/deploy/%s" % (HOME, fork.replace("/", "--"))
            self.assertEqual((w.machines["lin"].files[path], w.machines["lin"].modes[path]), (key, "600"))

    def test_the_controller_is_reached_as_its_confs_user(self):
        w = make(confs=dict(CONFS, vault=CONFS["vault"] + "\nuser=ops"))
        self.assertEqual(w.wk("ai", "pi", "x", tty=True).calls[0], "[lin] %s" % shlex.join(ASK[:-1] + ["ops@vault"]))

    def test_a_rotation_that_fails_refuses_the_session_and_writes_no_key(self):
        for answer, said in ((Result(255, "", "Permission denied (publickey).\n"), "(exit 255): Permission denied (publickey)."),
                             ("not json", "(exit 0): not json"), (json.dumps(["k"]), '(exit 0): ["k"]'),
                             (json.dumps({"no fork": "k"}), '(exit 0): {"no fork": "k"}')):
            w = make()
            w.machines["lin"].on(("ssh", "-i"), answer)
            r = w.wk("ai", "pi", "x", tty=True)
            self.assertEqual((r.code, r.effects), (1, renewed("lin")[:1]))
            self.assertIn("error: the controller did not rotate lin's push keys %s\n    `wk machine setup lin --stage credentials`, typed on vault, "
                          "remakes the rotation key; forced, the session starts with the keys this server has\n    --force crosses this\n" % said, r.err)

    def test_force_starts_the_session_with_the_keys_the_server_has_and_says_so_at_exit(self):
        w = make()
        w.machines["lin"].on(("ssh", "-i"), Result(255, "", "timed out\n"))
        r = w.wk("ai", "pi", "x", "--force", tty=True)
        self.assertEqual((r.code, r.effects), (0, renewed("lin")[:1] + [line("lin", PI, {}, tty=True)]))
        self.assertIn("warning: this command was forced past: the controller did not rotate lin's push keys", r.err)


class TestRefusals(unittest.TestCase):
    def refused(self, w, want, *words):
        r = w.wk(*(words or ("ai", "claude", "x")), tty=True)
        self.assertEqual((r.code, r.err, r.effects), (1, "error: %s\n" % want, []))

    def test_an_agent_that_is_neither(self):
        self.refused(make(), "no agent named codex: the agents are claude and pi", "ai", "codex", "x")

    def test_a_workspace_this_server_does_not_hold(self):
        self.refused(make(), "no workspace named y on lin: `wk ls` lists them, `wk new y` makes it", "ai", "claude", "y")

    def test_a_workspace_that_is_not_ready(self):
        self.refused(make(state="broken"), "workspace x on lin is not made (broken): wait for its `wk new`, or `wk new x` remakes a broken one")

    def test_an_agent_that_is_not_installed(self):
        for agent, path, needs in (("claude", session.claude_bin(HOME), ""), ("pi", session.pi_bin(HOME), "install node >= 22.19 and npm on lin, then: ")):
            w = make()
            del w.machines["lin"].files[path]
            self.refused(w, "%s is not installed on lin: there is no %s\n    %swk machine setup lin, typed on vault" % (agent, path, needs), "ai", agent, "x")

    def test_a_claude_json_that_is_not_claudes(self):
        for text in ("not json", "[]", '{"projects": {"%s": []}}' % SRC):
            w = make()
            w.machines["lin"].files["/home/u/.claude.json"] = text
            self.refused(w, "/home/u/.claude.json is not the JSON object Claude Code keeps there, so its start-up answers cannot be recorded\n"
                            "    remove it and start again")


class TestStartUpAnswers(unittest.TestCase):
    def test_the_start_up_answers_are_added_to_what_claude_already_keeps(self):
        new = json.loads(session.answered('{"numStartups": 3, "projects": {"/other": {"x": 1}}}', "/w/src"))
        self.assertEqual(new, {"numStartups": 3, "hasCompletedOnboarding": True, "remoteDialogSeen": True,
                               "projects": {"/other": {"x": 1}, "/w/src": {"hasTrustDialogAccepted": True}}})

    def test_no_file_is_an_empty_one_and_an_answered_one_is_left(self):
        text = session.answered(None, "/w/src")
        self.assertEqual((sorted(json.loads(text)), session.answered(text, "/w/src")), (["hasCompletedOnboarding", "projects", "remoteDialogSeen"], None))
