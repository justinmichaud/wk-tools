import json
import shlex
import unittest
from unittest import mock

from tests import agent_toy
from tests.world import CONFS, World
from wk.agent import session
from wk.core.act import die
from wk.core.machine import Result
from wk.core.vars import VARS
from wk.creds import rotation

HOME = "/home/u/wk"
SRC = HOME + "/ws/x/src"
INJECTED = {"CLAUDE_SECURESTORAGE_CONFIG_DIR": HOME + "/claude-login", "DISABLE_AUTOUPDATER": "1", "HTTPS_PROXY": "http://vault:3128",
            "NODE_EXTRA_CA_CERTS": HOME + "/injector-ca.pem", "https_proxy": "http://vault:3128"}
CLAUDE = [session.claude_bin(HOME), "--dangerously-skip-permissions", "--remote-control", "x"]
PI = [session.pi_bin(HOME)]
ANSWERED = session.answered(None, SRC)
DEPLOY = {"%s/secrets/deploy/%s" % (HOME, fork.replace("/", "--")): "key" for fork in VARS["wk_forks"]}
ASK = "[vault] POST /revoke"


def revoked(on, held=()):
    """What a revocation does on the server `on`: each of a person's credentials held there removed, then the
    controller asked."""
    return ["[%s] remove %s" % (on, path) for path in held] + [ASK]


def revoking(case, revoke=lambda inv, server: None):
    """The controller's revocation, scripted for the test `case`; the real one asks GitHub."""
    patch = mock.patch.object(rotation, "revoke", revoke)
    patch.start()
    case.addCleanup(patch.stop)


def make(on="lin", state="ready", confs=CONFS):
    """A world whose server `on` holds the workspace x and both agents, and is where the commands are typed."""
    w = World(here=on, commands=[agent_toy], confs=confs)
    w.workspace("x", on=on, state=state)
    w.machines[on].files.update({session.claude_bin(HOME): "claude", session.pi_bin(HOME): "pi"})
    return w


def line(on, argv, env, tty=False):
    """The shell `wk enter` opens, running the agent under `env`."""
    return "[%s] %s" % (on, shlex.join(["fake-enter"] + ["-t"] * tty + ["x", "env", "WK_WORKSPACE=x"] + ["%s=%s" % kv for kv in sorted(env.items())] + argv))


class TestStart(unittest.TestCase):
    def setUp(self):
        revoking(self)

    def test_claude_is_started_in_the_login_shell_on_the_terminal_through_the_injector_with_remote_control_named_for_the_workspace(self):
        w = make()
        r = w.wk("ai", "claude", "x", tty=True)
        self.assertEqual(r.calls, ["[lin] write /home/u/.claude.json", "[lin] tailscale ip -4 vault"] + revoked("lin")
                         + [line("lin", CLAUDE, INJECTED, tty=True)])
        self.assertEqual((r.code, r.effects), (0, [c for c in r.calls if "tailscale" not in c]))
        self.assertEqual((w.machines["lin"].read("/home/u/.claude.json"), w.machines["lin"].modes["/home/u/.claude.json"]), (ANSWERED, "600"))
        self.assertEqual(r.err, "==> starting claude in x\n")

    def test_pi_gets_no_proxy_no_ca_and_no_claude_state(self):
        r = make().wk("ai", "pi", "x", tty=True)
        self.assertEqual(r.effects, revoked("lin") + [line("lin", PI, {}, tty=True)])
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
        self.assertEqual(w.wk("ai", "claude", "x", tty=True).effects, revoked("lin") + [line("lin", CLAUDE, INJECTED, tty=True)])

    def test_a_dry_run_removes_nothing_asks_for_no_revocation_and_starts_nothing(self):
        r = make().wk("--dry-run", "ai", "claude", "x", tty=True)
        self.assertEqual((r.code, r.effects, r.plan), (0, [], ["[lin] write /home/u/.claude.json"] + revoked("lin")
                                                                                   + [line("lin", CLAUDE, INJECTED, tty=True)]))


class TestRevocation(unittest.TestCase):
    def test_a_persons_credentials_here_are_removed_before_the_agent_starts(self):
        revoking(self)
        w = make()
        held = dict({HOME + "/secrets/credentials.sh": "export GH_TOKEN=t\n"}, **DEPLOY)
        w.machines["lin"].files.update(held)
        r = w.wk("ai", "pi", "x", tty=True)
        self.assertEqual(r.effects, revoked("lin", [HOME + "/secrets/credentials.sh"] + sorted(DEPLOY)) + [line("lin", PI, {}, tty=True)])
        self.assertEqual([path for path in held if path in w.machines["lin"].files], [])

    def test_the_controller_revokes_the_keys_of_the_server_that_asks(self):
        asked = []
        revoking(self, lambda inv, server: asked.append((inv.fleet.hostname, server)))
        make("mac").wk("ai", "pi", "x", tty=True, on="mac")
        self.assertEqual(asked, [("vault", "mac")])

    def test_a_revocation_that_fails_refuses_the_session_with_the_credentials_removed_here(self):
        def failing(inv, server):
            die("GET /repos/o/r/keys answered HTTP 401: Bad credentials")
        revoking(self, failing)
        r = make().wk("ai", "pi", "x", tty=True)
        self.assertEqual((r.code, r.effects), (4, revoked("lin")))
        self.assertEqual(r.err, "error: the controller, vault, answered HTTP 502: GET /repos/o/r/keys answered HTTP 401: Bad credentials\n"
                                "    so lin's push keys are still registered on the forks; forced, the session starts with them removed here\n"
                                "    --force crosses this\n")

    def test_a_controller_that_does_not_answer_refuses_the_session(self):
        w = make()
        w.machines["vault"].down = True
        r = w.wk("ai", "pi", "x", tty=True)
        self.assertEqual((r.code, r.effects), (4, []))
        self.assertIn("error: the controller, vault, did not answer (the fake is down)\n", r.err)

    def test_force_starts_the_session_with_the_credentials_removed_here_and_says_so_at_exit(self):
        w = make()
        w.machines["vault"].down = True
        r = w.wk("ai", "pi", "x", "--force", tty=True)
        self.assertEqual((r.code, r.effects), (0, [] + [line("lin", PI, {}, tty=True)]))
        self.assertIn("warning: this command was forced past: the controller, vault, did not answer", r.err)


class TestRefusals(unittest.TestCase):
    def setUp(self):
        revoking(self)

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
