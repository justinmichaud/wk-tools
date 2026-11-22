import json
import shlex
import unittest

from tests import agent_toy
from tests.world import CONFS, World
from wk.agent import session
from wk.agent.wrapper import wrap
from wk.core.errors import Unreachable
from wk.core.machine import Result

HOME = "/home/u/wk"
SRC = HOME + "/ws/x/src"
INSIDE = ("fake-exec", "x", SRC)
TOOL = {"lin": "bwrap", "mac": "sandbox-exec", "box": "env"}
WRAPPED = {"lin": ("bwrap",), "mac": ("sandbox-exec",), "box": ("env", "-i")}
KIND = {"lin": "linux", "mac": "mac", "box": "borrowed"}
SEEN = "HOME=/home/u\0USER=u\0PATH=/usr/bin:/bin\0TERM=dumb\0LANG=C.UTF-8\0LC_CTYPE=C\0SSH_AUTH_SOCK=/tmp/a\0GITHUB_TOKEN=t\0WK_BUILD=1\0"
ENV = {"HOME": "/home/u", "USER": "u", "PATH": HOME + "/shims:" + HOME + "/bin:/usr/bin:/bin", "TERM": "dumb", "LANG": "C.UTF-8", "LC_CTYPE": "C",
       "WK_AGENT": "1", "WK_WORKSPACE": "x"}
CLAUDE_ENV = dict(ENV, HTTPS_PROXY="http://vault:3128", https_proxy="http://vault:3128", NODE_EXTRA_CA_CERTS=HOME + "/injector-ca.pem",
                  CLAUDE_SECURESTORAGE_CONFIG_DIR=HOME + "/claude-login", DISABLE_AUTOUPDATER="1")
CLAUDE = [session.claude_bin(HOME), "--dangerously-skip-permissions", "--remote-control", "x"]
PI = [session.pi_bin(HOME)]
ANSWERED = session.answered(None, SRC)
PORT = 24583       # 20000 + crc32("ai claude x") % 10000


def make(on="lin", listing=Result(0, ""), dump=Result(0, "HOME=/home/u\0PWD=%s\0" % SRC), state="ready", confs=CONFS):
    """A world whose server `on` holds the workspace x, both agents, and a wrapper under which the stand-in sees
    `listing` and `dump`."""
    w = World(commands=[agent_toy], confs=confs)
    w.workspace("x", on=on, state=state)
    m = w.machines[on]
    m.files.update({session.claude_bin(HOME): "claude", session.pi_bin(HOME): "pi"})
    m.on(INSIDE + ("env", "-0"), SEEN)
    m.on(INSIDE + ("sh", "-c"), "")
    m.on(INSIDE + WRAPPED[on], lambda call: listing if "ls" in call.argv else dump)
    return w


def line(on, argv, env, tty=False):
    return "[%s] %s" % (on, shlex.join(["fake-exec"] + ["-t"] * tty + ["x", SRC] + wrap(KIND[on], argv, [HOME + "/secrets"], env)))


def probes(on, env):
    return ["[%s] fake-exec x %s env -0" % (on, SRC),
            "[%s] fake-exec x %s sh -c 'command -v %s'" % (on, SRC, TOOL[on]),
            line(on, ["ls", "-A", HOME + "/secrets"], dict(env, LC_ALL="C")), line(on, ["env", "-0"], env)]


class TestStart(unittest.TestCase):
    def test_claude_is_measured_then_started_on_the_terminal_through_the_injector_with_remote_control_named_for_the_workspace(self):
        w = make()
        r = w.wk("ai", "claude", "x", on="lin", tty=True)
        self.assertEqual(r.calls, probes("lin", CLAUDE_ENV) + ["[lin] write /home/u/.claude.json", line("lin", CLAUDE, CLAUDE_ENV, tty=True)])
        self.assertEqual((r.code, r.effects), (0, r.calls[-2:]))
        self.assertEqual((w.machines["lin"].read("/home/u/.claude.json"), w.machines["lin"].modes["/home/u/.claude.json"]), (ANSWERED, "600"))
        self.assertEqual(r.err, "==> starting claude in x: credentials are hidden by bwrap\n")

    def test_pi_gets_no_proxy_no_ca_and_no_claude_state(self):
        r = make().wk("ai", "pi", "x", on="lin", tty=True)
        self.assertEqual(r.calls, probes("lin", ENV) + [line("lin", PI, ENV, tty=True)])
        self.assertNotIn("PROXY", "".join(r.calls).upper())

    def test_on_a_mac_the_wrapper_is_the_seatbelt_profile(self):
        for agent, argv, env in (("claude", CLAUDE, CLAUDE_ENV), ("pi", PI, ENV)):
            r = make("mac").wk("ai", agent, "x", on="mac", tty=True)
            self.assertEqual([c for c in r.calls if "write" not in c], probes("mac", env) + [line("mac", argv, env, tty=True)])
            self.assertIn("sandbox-exec -p '(version 1)(allow default)(deny file-read* (subpath \"/home/u/wk/secrets\"))' env -i ", r.calls[-1])
            self.assertIn("credentials are hidden by a Seatbelt profile\n", r.err)

    def test_on_a_borrowed_box_claude_reaches_the_injector_through_the_port_the_clients_ssh_forwards(self):
        w = make("box")
        r = w.wk("ai", "claude", "x", tty=True)
        handed = [c for c in w.machines["box"].calls if c.argv[:2] == ("wk", "ai")][0]
        self.assertEqual((handed.forward, handed.env), ((PORT, "vault:3128"), {"WK_FORWARD": str(PORT)}))
        env = dict(CLAUDE_ENV, HTTPS_PROXY="http://127.0.0.1:%d" % PORT, https_proxy="http://127.0.0.1:%d" % PORT)
        auto = [CLAUDE[0], "--permission-mode", "auto", "--remote-control", "x"]
        self.assertEqual((r.code, r.calls[-1]), (0, line("box", auto, env, tty=True)))
        self.assertIn("[box] fake-exec -t x %s env -i " % SRC, r.calls[-1])
        self.assertEqual(r.err, "==> starting claude in x: no credential is delivered to a borrowed box, so the wrapper is the "
                                "minimal environment alone\n")

    def test_an_ssh_that_ends_before_the_command_ran_is_the_box_not_answering_or_the_forward_refused(self):
        w = make("box")

        def ended(call):
            raise Unreachable("box", "")
        w.machines["box"].on(("wk", "ai"), ended)
        r = w.wk("ai", "claude", "x", tty=True)
        self.assertEqual(r.code, 4)
        self.assertEqual(r.err, "error: ssh to box ended before `wk ai claude x` ran there: box did not answer, or its port 127.0.0.1:%d is taken "
                                "and the forward was refused (ssh said which, above)\n    a taken port is this same command still running from "
                                "another terminal: end it, then type this again\n" % PORT)

    def test_claude_typed_on_the_borrowed_box_itself_has_no_forward_and_is_refused_naming_the_client(self):
        r = make("box").wk("ai", "claude", "x", on="box", tty=True)
        self.assertEqual((r.code, r.effects), (4, []))
        self.assertEqual(r.err, "error: box is a borrowed box, off the tailnet: a session there reaches the injector through the ssh session "
                                "that starts it\n    start it from the client: wk ai claude x\n")

    def test_pi_on_a_borrowed_box_needs_no_injector_and_starts_where_it_is_typed(self):
        r = make("box").wk("ai", "pi", "x", on="box", tty=True)
        self.assertEqual(r.calls, probes("box", ENV) + [line("box", PI, ENV, tty=True)])

    def test_a_server_of_the_owners_is_on_the_tailnet_and_is_handed_no_forward(self):
        w = make()
        w.wk("ai", "claude", "x", tty=True)
        handed = [c for c in w.machines["lin"].calls if c.argv[:2] == ("wk", "ai")][0]
        self.assertEqual((handed.forward, handed.env), (None, {}))

    def test_the_words_after_the_dashes_go_to_the_agent_and_its_exit_code_comes_back(self):
        w = make()
        w.machines["lin"].on(("fake-exec", "-t"), Result(3))
        r = w.wk("ai", "claude", "x", "--", "-r", "--model", "opus", on="lin", tty=True)
        self.assertEqual((r.code, r.calls[-1]), (3, line("lin", CLAUDE + ["-r", "--model", "opus"], CLAUDE_ENV, tty=True)))

    def test_given_words_and_no_terminal_the_agent_runs_without_one(self):
        w = make()
        r = w.wk("ai", "claude", "x", "--", "-p", "say OK")
        self.assertEqual((r.code, r.calls[-1]), (0, line("lin", CLAUDE + ["-p", "say OK"], CLAUDE_ENV)))
        self.assertNotIn("fake-exec -t", r.calls[-1])

    def test_no_words_and_no_terminal_is_refused_naming_the_prompt_form_before_anything_is_measured(self):
        r = make().wk("ai", "claude", "x", on="lin")
        self.assertEqual((r.code, r.calls), (4, []))
        self.assertEqual(r.err, "error: an agent's session needs a terminal and there is none: give the agent its prompt, "
                                "`wk ai claude x -- -p <prompt>`\n")

    def test_the_terminal_is_this_one_not_the_workspaces(self):
        r = make().wk("ai", "pi", "x", on="lin", env={"TERM": "xterm-256color", "GITHUB_TOKEN": "t"}, tty=True)
        self.assertEqual(r.calls[-1], line("lin", PI, dict(ENV, TERM="xterm-256color"), tty=True))

    def test_the_injectors_port_is_the_credential_servers_conf_when_it_names_one(self):
        r = make(confs=dict(CONFS, vault=CONFS["vault"] + "\ninjector_port=9000")).wk("ai", "claude", "x", on="lin", tty=True)
        self.assertIn("--setenv HTTPS_PROXY http://vault:9000 ", r.calls[-1])
        self.assertIn("--setenv https_proxy http://vault:9000 ", r.calls[-1])

    def test_typed_on_the_client_it_is_handed_to_the_server_that_holds_the_workspace(self):
        r = make("mac").wk("ai", "pi", "x", "--", "-c", tty=True)
        self.assertIn("[mac] wk ai pi x -- -c", r.calls)
        self.assertEqual(r.calls[-1], line("mac", PI + ["-c"], ENV, tty=True))

    def test_answers_claude_already_holds_are_not_written_again(self):
        w = make()
        w.machines["lin"].files["/home/u/.claude.json"] = ANSWERED
        self.assertEqual(w.wk("ai", "claude", "x", on="lin", tty=True).effects, [line("lin", CLAUDE, CLAUDE_ENV, tty=True)])

    def test_a_dry_run_measures_and_starts_nothing(self):
        r = make().wk("--dry-run", "ai", "claude", "x", on="lin", tty=True)
        self.assertEqual((r.code, r.effects, r.plan), (0, [], ["[lin] write /home/u/.claude.json", line("lin", CLAUDE, CLAUDE_ENV, tty=True)]))


class TestRefusals(unittest.TestCase):
    def refused(self, w, want, *words):
        r = w.wk(*(words or ("ai", "claude", "x")), on="lin", tty=True)
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
            self.refused(w, "%s is not installed on lin: there is no %s\n    %swk machine setup lin" % (agent, path, needs), "ai", agent, "x")

    def test_a_workspace_without_bwrap(self):
        w = make()
        w.machines["lin"].on(INSIDE + ("sh", "-c"), Result(1))
        self.refused(w, "the credential wrapper cannot run for x on lin: the workspace has no bwrap, which the wrapper is made of\n"
                        "    wk machine setup lin")

    def test_a_wrapper_under_which_a_credential_is_listed(self):
        self.refused(make(listing=Result(0, "github-token\n")),
                     "the credential wrapper does not hold for x on lin: /home/u/wk/secrets is readable under the wrapper: it lists "
                     "github-token\n    no agent starts until it holds: this is a fault in wk (lib/wk/agent/wrapper.py) to report with the line above\n    --force crosses this")

    def test_a_wrapper_under_which_the_ssh_agent_is_inherited(self):
        w = make(dump=Result(0, "HOME=/home/u\0SSH_AUTH_SOCK=/tmp/a\0"))
        self.assertIn("error: the credential wrapper does not hold for x on lin: the environment under the wrapper carries SSH_AUTH_SOCK\n",
                      w.wk("ai", "pi", "x", on="lin", tty=True).err)

    def test_a_probe_that_died_is_not_a_wrapper_that_holds(self):
        self.refused(make(listing=Result(1, "", "bwrap: No permissions to create new namespace\n")),
                     "the credential wrapper could not be measured for x on lin: the stand-in could not list /home/u/wk/secrets (exit 1): "
                     "bwrap: No permissions to create new namespace\n    check that the workspace runs: wk enter x\n    --force crosses this")

    def test_force_starts_the_agent_under_a_wrapper_that_does_not_hold_and_says_so_again_at_exit(self):
        r = make(listing=Result(0, "github-token\n")).wk("ai", "pi", "x", "--force", on="lin", tty=True)
        self.assertEqual((r.code, r.effects), (0, [line("lin", PI, ENV, tty=True)]))
        self.assertIn("warning: this command was forced past: the credential wrapper does not hold for x on lin", r.err)

    def test_a_workspace_whose_environment_cannot_be_read(self):
        w = make()
        w.machines["lin"].on(INSIDE + ("env", "-0"), Result(125, "", "no such container"))
        self.refused(w, "the credential wrapper could not be measured for x on lin: `env -0` in the workspace printed no HOME and PATH\n"
                        "    check that the workspace runs: wk enter x")

    def test_a_claude_json_that_is_not_claudes(self):
        for text in ("not json", "[]", '{"projects": {"%s": []}}' % SRC):
            w = make()
            w.machines["lin"].files["/home/u/.claude.json"] = text
            self.refused(w, "/home/u/.claude.json is not the JSON object Claude Code keeps there, so its start-up answers cannot be recorded\n"
                            "    remove it and start again")


class TestDoctorRow(unittest.TestCase):
    def row(self, w, on="lin", **how):
        return w.wk("agentdoctor", on, "x", **how).out

    def test_a_wrapper_that_holds_is_ok_measured_from_the_client_under_the_bare_session_environment(self):
        w = make()
        r = w.wk("agentdoctor", "lin", "x")
        self.assertEqual(r.out, "credential wrapper | ok | credentials are hidden by bwrap | \n")
        self.assertEqual(r.calls[-4:], probes("lin", ENV))

    def test_a_missing_tool_and_a_leak_are_missing_and_a_dead_probe_is_unknown(self):
        absent = make("mac")
        absent.machines["mac"].on(INSIDE + ("sh", "-c"), Result(1))
        self.assertEqual(self.row(absent, "mac"), "credential wrapper | missing | the workspace has no sandbox-exec, which the wrapper is made of | wk machine setup mac\n")
        self.assertEqual(self.row(make(listing=Result(0, "k\n"))).split(" | ")[1:3], ["missing", "/home/u/wk/secrets is readable under the wrapper: it lists k"])
        self.assertEqual(self.row(make(dump=Result(1, "", "died"))),
                         "credential wrapper | unknown | the stand-in could not print its environment (exit 1): died | check that the workspace runs: wk enter x\n")


class TestStartUpAnswers(unittest.TestCase):
    def test_the_start_up_answers_are_added_to_what_claude_already_keeps(self):
        new = json.loads(session.answered('{"numStartups": 3, "projects": {"/other": {"x": 1}}}', "/w/src"))
        self.assertEqual(new, {"numStartups": 3, "hasCompletedOnboarding": True, "remoteDialogSeen": True,
                               "projects": {"/other": {"x": 1}, "/w/src": {"hasTrustDialogAccepted": True}}})

    def test_no_file_is_an_empty_one_and_an_answered_one_is_left(self):
        text = session.answered(None, "/w/src")
        self.assertEqual((sorted(json.loads(text)), session.answered(text, "/w/src")), (["hasCompletedOnboarding", "projects", "remoteDialogSeen"], None))
