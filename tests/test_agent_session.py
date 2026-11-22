import shlex
import unittest

from tests import agent_toy
from tests.world import CONFS, World
from wk.agent import install
from wk.agent.wrapper import wrap
from wk.core.machine import Result

HOME = "/home/u/wk"
SRC = HOME + "/ws/x/src"
INSIDE = ("fake-exec", "x", SRC)
TOOL = {"lin": "bwrap", "mac": "sandbox-exec", "box": "env"}
WRAPPED = {"lin": ("bwrap",), "mac": ("sandbox-exec",), "box": ("env", "-i")}
KIND = {"lin": "linux", "mac": "mac", "box": "borrowed"}
SEEN = "HOME=/home/u\0USER=u\0PATH=/usr/bin:/bin\0TERM=dumb\0LANG=C.UTF-8\0LC_CTYPE=C\0SSH_AUTH_SOCK=/tmp/a\0GITHUB_TOKEN=t\0WK_BUILD=1\0"
ENV = {"HOME": "/home/u", "USER": "u", "PATH": HOME + "/shims:/usr/bin:/bin", "TERM": "dumb", "LANG": "C.UTF-8", "LC_CTYPE": "C",
       "WK_AGENT": "1", "WK_WORKSPACE": "x"}
CLAUDE_ENV = dict(ENV, HTTPS_PROXY="http://vault:8443", https_proxy="http://vault:8443", NODE_EXTRA_CA_CERTS=HOME + "/injector-ca.pem",
                  CLAUDE_SECURESTORAGE_CONFIG_DIR="/home/u/.claude", DISABLE_AUTOUPDATER="1")
CLAUDE = [install.claude_bin(HOME), "--dangerously-skip-permissions", "--remote-control", "x"]
PI = [install.pi_bin(HOME)]
ANSWERED = install.answered(None, SRC)


def make(on="lin", listing=Result(0, ""), dump=Result(0, "HOME=/home/u\0PWD=%s\0" % SRC), state="ready", confs=CONFS):
    """A world whose server `on` holds the workspace x, both agents, and a wrapper under which the stand-in sees
    `listing` and `dump`."""
    w = World(commands=[agent_toy], confs=confs)
    w.workspace("x", on=on, state=state)
    m = w.machines[on]
    m.files.update({install.claude_bin(HOME): "claude", install.pi_bin(HOME): "pi"})
    m.on(INSIDE + ("env", "-0"), SEEN)
    m.on(INSIDE + ("sh", "-c"), "")
    m.on(INSIDE + WRAPPED[on], lambda call: listing if "ls" in call.argv else dump)
    return w


def line(on, argv, env, tty=False):
    return "[%s] %s" % (on, shlex.join(["fake-exec"] + ["-t"] * tty + ["x", SRC] + wrap(KIND[on], argv, [HOME + "/secrets"], env)))


def probes(on, env):
    return ["[%s] wk ls --json" % on, "[%s] fake-exec x %s env -0" % (on, SRC),
            "[%s] fake-exec x %s sh -c 'command -v %s'" % (on, SRC, TOOL[on]),
            line(on, ["ls", "-A", HOME + "/secrets"], dict(env, LC_ALL="C")), line(on, ["env", "-0"], env)]


class TestStart(unittest.TestCase):
    def test_claude_is_measured_then_started_on_the_terminal_through_the_injector_with_remote_control_named_for_the_workspace(self):
        w = make()
        r = w.wk("ai", "claude", "x", on="lin")
        self.assertEqual(r.calls, probes("lin", CLAUDE_ENV) + ["[lin] write /home/u/.claude.json", line("lin", CLAUDE, CLAUDE_ENV, tty=True)])
        self.assertEqual((r.code, r.effects), (0, r.calls[-2:]))
        self.assertEqual((w.machines["lin"].read("/home/u/.claude.json"), w.machines["lin"].modes["/home/u/.claude.json"]), (ANSWERED, "600"))
        self.assertEqual(r.err, "==> starting claude in x: credentials are hidden by bwrap\n")

    def test_pi_gets_no_proxy_no_ca_and_no_claude_state(self):
        r = make().wk("ai", "pi", "x", on="lin")
        self.assertEqual(r.calls, probes("lin", ENV) + [line("lin", PI, ENV, tty=True)])
        self.assertNotIn("PROXY", "".join(r.calls).upper())

    def test_on_a_mac_the_wrapper_is_the_seatbelt_profile(self):
        for agent, argv, env in (("claude", CLAUDE, CLAUDE_ENV), ("pi", PI, ENV)):
            r = make("mac").wk("ai", agent, "x", on="mac")
            self.assertEqual([c for c in r.calls if "write" not in c], probes("mac", env) + [line("mac", argv, env, tty=True)])
            self.assertIn("sandbox-exec -p '(version 1)(allow default)(deny file-read* (subpath \"/home/u/wk/secrets\"))' env -i ", r.calls[-1])
            self.assertIn("credentials are hidden by a Seatbelt profile\n", r.err)

    def test_on_a_borrowed_box_the_wrapper_is_the_minimal_environment_and_it_says_so(self):
        auto = [CLAUDE[0], "--permission-mode", "auto", "--remote-control", "x"]
        for agent, argv, env in (("claude", auto, CLAUDE_ENV), ("pi", PI, ENV)):
            r = make("box").wk("ai", agent, "x", on="box")
            self.assertEqual([c for c in r.calls if "write" not in c], probes("box", env) + [line("box", argv, env, tty=True)])
            self.assertIn("[box] fake-exec -t x %s env -i " % SRC, r.calls[-1])
            self.assertEqual(r.err, "==> starting %s in x: no credential is delivered to a borrowed box, so the wrapper is the "
                                    "minimal environment alone\n" % agent)

    def test_the_words_after_the_dashes_go_to_the_agent_and_its_exit_code_comes_back(self):
        w = make()
        w.machines["lin"].on(("fake-exec", "-t"), Result(3))
        r = w.wk("ai", "claude", "x", "--", "-r", "--model", "opus", on="lin")
        self.assertEqual((r.code, r.calls[-1]), (3, line("lin", CLAUDE + ["-r", "--model", "opus"], CLAUDE_ENV, tty=True)))

    def test_the_terminal_is_this_one_not_the_workspaces(self):
        r = make().wk("ai", "pi", "x", on="lin", env={"TERM": "xterm-256color", "GITHUB_TOKEN": "t"})
        self.assertEqual(r.calls[-1], line("lin", PI, dict(ENV, TERM="xterm-256color"), tty=True))

    def test_the_injectors_port_is_the_credential_servers_conf_when_it_names_one(self):
        r = make(confs=dict(CONFS, vault=CONFS["vault"] + "\ninjector_port=9000")).wk("ai", "claude", "x", on="lin")
        self.assertIn("--setenv HTTPS_PROXY http://vault:9000 ", r.calls[-1])
        self.assertIn("--setenv https_proxy http://vault:9000 ", r.calls[-1])

    def test_typed_on_the_client_it_is_handed_to_the_server_that_holds_the_workspace(self):
        r = make("mac").wk("ai", "pi", "x", "--", "-c")
        self.assertIn("[mac] wk ai pi x -- -c", r.calls)
        self.assertEqual(r.calls[-1], line("mac", PI + ["-c"], ENV, tty=True))

    def test_answers_claude_already_holds_are_not_written_again(self):
        w = make()
        w.machines["lin"].files["/home/u/.claude.json"] = ANSWERED
        self.assertEqual(w.wk("ai", "claude", "x", on="lin").effects, [line("lin", CLAUDE, CLAUDE_ENV, tty=True)])

    def test_a_dry_run_measures_and_starts_nothing(self):
        r = make().wk("--dry-run", "ai", "claude", "x", on="lin")
        self.assertEqual((r.code, r.effects, r.plan), (0, [], ["[lin] write /home/u/.claude.json", line("lin", CLAUDE, CLAUDE_ENV, tty=True)]))


class TestRefusals(unittest.TestCase):
    def refused(self, w, want, *words):
        r = w.wk(*(words or ("ai", "claude", "x")), on="lin")
        self.assertEqual((r.code, r.err, r.effects), (1, "error: %s\n" % want, []))

    def test_an_agent_that_is_neither(self):
        self.refused(make(), "no agent named codex: the agents are claude and pi", "ai", "codex", "x")

    def test_a_workspace_this_server_does_not_hold(self):
        self.refused(make(), "no workspace named y on lin: `wk ls` lists them, `wk new y` makes it", "ai", "claude", "y")

    def test_a_workspace_that_is_not_ready(self):
        self.refused(make(state="broken"), "workspace x is broken, not ready\n    wait for the `wk new` making it, or: wk rm x && wk new x")

    def test_a_server_whose_listing_cannot_be_read(self):
        w = make()
        w.machines["lin"].on(("wk", "ls", "--json"), "Traceback")
        self.refused(w, "lin did not answer `wk ls --json`, so whether it holds x is unknown")

    def test_an_agent_that_is_not_installed(self):
        for agent, path in (("claude", install.claude_bin(HOME)), ("pi", install.pi_bin(HOME))):
            w = make()
            del w.machines["lin"].files[path]
            self.refused(w, "%s is not installed on lin: there is no %s\n    wk machine setup lin" % (agent, path), "ai", agent, "x")

    def test_a_workspace_without_bwrap(self):
        w = make()
        w.machines["lin"].on(INSIDE + ("sh", "-c"), Result(1))
        self.refused(w, "the credential wrapper cannot run for x on lin: the workspace has no bwrap, which the wrapper is made of\n"
                        "    wk machine setup lin")

    def test_a_wrapper_under_which_a_credential_is_listed(self):
        self.refused(make(listing=Result(0, "github-token\n")),
                     "the credential wrapper does not hold for x on lin: /home/u/wk/secrets is readable under the wrapper: it lists "
                     "github-token\n    no agent starts until it holds: this is a fault in wk (lib/wk/agent/wrapper.py) to report with the line above")

    def test_a_wrapper_under_which_the_ssh_agent_is_inherited(self):
        w = make(dump=Result(0, "HOME=/home/u\0SSH_AUTH_SOCK=/tmp/a\0"))
        self.assertIn("error: the credential wrapper does not hold for x on lin: the environment under the wrapper carries SSH_AUTH_SOCK\n",
                      w.wk("ai", "pi", "x", on="lin").err)

    def test_a_probe_that_died_is_not_a_wrapper_that_holds(self):
        self.refused(make(listing=Result(1, "", "bwrap: No permissions to create new namespace\n")),
                     "the credential wrapper could not be measured for x on lin: the stand-in could not list /home/u/wk/secrets (exit 1): "
                     "bwrap: No permissions to create new namespace\n    check that the workspace runs: wk enter x")

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
        self.assertEqual(r.calls[-4:], probes("lin", ENV)[1:])

    def test_a_missing_tool_and_a_leak_are_missing_and_a_dead_probe_is_unknown(self):
        absent = make("mac")
        absent.machines["mac"].on(INSIDE + ("sh", "-c"), Result(1))
        self.assertEqual(self.row(absent, "mac"), "credential wrapper | missing | the workspace has no sandbox-exec, which the wrapper is made of | wk machine setup mac\n")
        self.assertEqual(self.row(make(listing=Result(0, "k\n"))).split(" | ")[1:3], ["missing", "/home/u/wk/secrets is readable under the wrapper: it lists k"])
        self.assertEqual(self.row(make(dump=Result(1, "", "died"))),
                         "credential wrapper | unknown | the stand-in could not print its environment (exit 1): died | check that the workspace runs: wk enter x\n")
