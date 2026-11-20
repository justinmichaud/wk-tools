"""`wk ai <agent> <ws>`: a coding agent's session in a workspace, started only once the wall's checks (`wk doctor`'s)
pass, typed into a world holding workspace `ws` on a container, a macOS guest or a build box."""
import io
import os
import re
import shlex
import signal
import sys
import types
import unittest
from unittest import mock

from tests import fakes
from tests.fakes import Result
from tests.test_doctor_wall import HEALTHY
from tests.test_wk_workspace import FAR_ROOT, Flow, World, said

CLAUDE = "/home/u/.local/bin/claude"
BRIDGE = "/opt/wk-tools/container/proxy/ensure-bridge.sh"
WALL = "bwrap --dev-bind / / %s --" % " ".join("--ro-bind-try /src/WebKit/.git/{0} /src/WebKit/.git/{0}".format(p) for p in (
    "objects", "refs", "logs", "HEAD", "packed-refs", "index.lock", "ORIG_HEAD"))
SESSION = "env WK_AGENT=claude WK_WORKSPACE=ws WK_SESSION=<id>"
BOX_SRC = FAR_ROOT + "/ws/ws/WebKit"
GH_LOGIN = 'test -e "$HOME/.config/gh/hosts.yml" || { command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; }'
NOT_INTACT = "the sandbox around 'ws' is not intact (see above), and relaxed"


def unwrap(text):
    """The command an `sh -c` was wrapped around."""
    words = shlex.split(text)
    return unwrap(words[2]) if len(words) == 3 and words[:2] == ["sh", "-c"] else text


class Walled(World):
    """Workspace `ws` behind a healthy wall: in it each probe answers from `wall` (tests.test_doctor_wall's table; a
    `person:` key answers a person's run), the agents script's verbs from `agents`, `command -v` from `tools` and the
    session from `session`, each recorded in `asked`; gh is logged out, podman rootless, the egress proxy up, a guest
    filtered by an installed Softnet. Its registry reads the process environment, as the dispatcher's own does."""

    def __init__(self, tmp, kinds=None):
        self.wall = dict([("gh auth status", Result(1))] + HEALTHY)
        self.agents = {"find claude": Result(0, CLAUDE + "\n"), "find pi": Result(0, "/home/u/.local/bin/pi\n")}
        self.tools, self.session, self.asked = {"bwrap"}, Result(0), []
        super().__init__(tmp, kinds=kinds)
        self.reg.env = os.environ
        self.answer(["podman", "info"], out="true\n")
        self.answer(["systemctl", "--user"])
        self.make("ws")
        self.effects = []

    def inside(self, words, person=False):
        if any(w.startswith("WK_AGENT=") for w in words):
            self.asked.append("session")
            return self.session(words) if callable(self.session) else self.session
        if "container/agents.sh" in words:
            verb = " ".join(words[words.index("container/agents.sh") + 1:])
            self.asked.append("agents.sh " + verb)
            return self.agents.get(verb, Result(1))
        if words[:3] == ["sh", "-c", 'command -v "$1" >/dev/null']:
            self.asked.append("have " + words[-1])
            return Result(0 if words[-1] in self.tools else 1)
        cmd = words[2] if words[:2] in (["bash", "-lc"], ["sh", "-c"]) else " ".join(words)
        self.asked.append(("person: " if person else "") + cmd)
        found = [v for k, v in self.wall.items() if k.startswith("person:") and person and k[7:] in cmd]
        found += [v for k, v in self.wall.items() if not k.startswith("person:") and k in cmd]
        v = (found[0](cmd) if callable(found[0]) else found[0]) if found else ""
        return v if isinstance(v, Result) else Result(0, v)

    def _in_ws(self, argv, f):
        person = argv[2].endswith("/wk-person")
        return self.inside(argv[3 if person else 2:], person)

    def _ssh(self, argv, f):
        """A command in the workspace is run in its checkout; any other is the box's own."""
        text = unwrap(argv[-1])
        return self.inside(shlex.split(text.split(" && ", 1)[1])) if text.startswith("cd ") else super()._ssh(argv, f)

    def _vm(self):
        super()._vm()
        self.react([self.tart, "exec"], lambda a, f: f.inside(shlex.split(a[-1].split("; ", 1)[1] if a[-1].startswith("cd ") else a[-1])))
        self.answer(["ifconfig"], out="bridge100: flags=8863<UP>\n\tinet 192.168.2.1 netmask 0xffffff00\n")
        for argv in (["test", "-x"], ["python3"]):
            self.answer(argv)
        self.wall["git config --global --get core.sshCommand"] = "/Users/admin/wk-tools/container/push/wk-push-client.py"

    def make(self, name="ws", **kw):
        super().make(name, **kw)
        self.files.pop(os.path.join(self.env.get("WK_VM_STORE", ""), "vm", name + ".unfiltered"), None)


class Tty(io.StringIO):
    def isatty(self):
        return True


class AiTest(Flow):
    world_class = Walled

    def ai(self, *argv, w=None):
        (w or self.w).asked.clear()
        return self.typed("ai", *argv, w=w)

    def on_a_terminal(self):
        for p in (mock.patch.object(sys, "stdin", Tty()), mock.patch.object(fakes, "io", types.SimpleNamespace(StringIO=Tty))):
            p.start()
            self.addCleanup(p.stop)

    def session(self, w=None):
        """The one session started, its id made `<id>`: from the container's name on, or ssh's or tart's line."""
        (argv,) = [e[1] for e in (w or self.w).effects if e[0] == "run_tty"]
        argv = [re.sub(r"WK_SESSION=[0-9a-f]{32}", "WK_SESSION=<id>", a) for a in argv]
        if "--name" in argv:
            return argv[argv.index("--name"):]
        return [argv[0], unwrap(shlex.split(argv[-1])[-1]) if argv[0] == "ssh" else argv[-1]]

    def misses(self, err):
        """The wall's failed rows, without their remedy."""
        lines = re.sub(r"\x1b\[[0-9;]*m", "", err).splitlines()
        return [re.sub(r"\s+->.*", "", l[len("  --"):].strip()) for l in lines if l.startswith("  --")]

    def forced(self, err):
        return [l[len("warning: FORCED past a barrier: "):] for l in err.splitlines() if l.startswith("warning: FORCED")]


class TestOnAContainer(AiTest):
    """`unit ai.verifies_wall`: the agent is found, every check of the wall is measured, then the session starts."""

    def test_a_healthy_container_is_measured_then_the_agent_starts_walled_off_git_history(self):
        rc, err = self.ai("claude", "ws", "-r")
        self.assertEqual((rc, said(err)), (0, "==> starting Claude in 'ws' (permissions relaxed; the workspace is the boundary)"))
        asked = self.w.asked
        self.assertEqual((asked[0], asked[-2:]), ("agents.sh find claude", ["have bwrap", "session"]))
        for probe in ("https://github.com/ ", "example.com", "192.168.1.1", "1.1.1.1", "/proc/net/dev", "ls -d /host/home", "mktemp -d /tmp/wk-wall",
                      "PRIVATE KEY", "GITHUB_COM_TOKEN", "hosts.yml", "test -r /secrets/", "ssh-agent.sock", "core.sshCommand", "git ls-remote",
                      "https://api.github.com/ ", "api.github.com/user", "/pulls", "rest/version", "rest/bug", "webkitscmpy.setup",
                      "claude auth status", "gpu-probe.sh", "/.credentials.json", "sk-ant-o", "api.anthropic.com", ".wk-write-probe"):
            self.assertTrue([a for a in asked[1:-2] if probe in a and not a.startswith("person: ")], probe)
        self.assertEqual(sorted(a.split("git ls-remote")[0].split("https")[0] for a in asked if a.startswith("person: ")),
                         ["person: GIT_TERMINAL_PROMPT=0 ", "person: curl -sS -m 40 --suppress-connect-headers -D - -o /dev/null -w '%{http_code}' -X POST -d '{}' "])
        self.assertEqual(self.session(), ["--name", "wk-ws", "--no-tty", "--exec", "--", BRIDGE, *SESSION.split(), "bash", "-lc",
                                          "cd /src/WebKit && exec %s %s --dangerously-skip-permissions -r" % (WALL, CLAUDE)])
        self.assertEqual([e[1][:2] for e in self.w.effects if e[0] == "run" and e[1][0] != "env"],
                         [("podman", "inspect")] * 3 + [("podman", "info"), ("systemctl", "--user")])

    def test_the_agents_own_arguments_reach_it_verbatim(self):
        self.ai("claude", "ws", "--continue", "two words")
        self.assertEqual(shlex.split(self.session()[-1])[-2:], ["--continue", "two words"])

    def test_a_stopped_proxy_is_a_barrier_and_force_crosses_it_saying_so(self):
        self.w.answer(["systemctl", "--user"], rc=3)
        rc, err = self.ai("claude", "ws")
        self.assertEqual((rc, said(err), self.misses(err), "session" in self.w.asked),
                         (1, "error: " + NOT_INTACT, ["egress proxy is not running"], False))
        rc, err = self.ai("claude", "--force", "ws")
        self.assertEqual((rc, self.forced(err), self.w.asked[-1]), (0, [NOT_INTACT], "session"))

    def test_a_probe_that_dies_is_unmeasured_and_a_barrier(self):
        def gone(cmd):
            raise OSError("ssh went away")
        self.w.wall["gpu-probe.sh"] = gone
        rc, err = self.ai("claude", "ws")
        self.assertEqual((rc, said(err), self.misses(err), "session" in self.w.asked),
                         (1, "error: " + NOT_INTACT, ["the 'gpu' probe died before it reported anything (ssh went away), so what it measures is unmeasured"], False))

    def test_a_host_the_proxy_lets_through_is_a_barrier_naming_it(self):
        self.w.wall["example.com"] = ""
        rc, err = self.ai("claude", "ws")
        self.assertEqual((rc, said(err), self.misses(err)), (1, "error: " + NOT_INTACT, ["example.com was NOT refused"]))

    def test_a_way_to_publish_stops_the_session_and_force_does_not_cross_it(self):
        self.w.wall["git ls-remote"] = ""
        for force in ([], ["--force"]):
            rc, err = self.ai("claude", *force, "ws")
            self.assertEqual((rc, said(err), self.misses(err), "session" in self.w.asked),
                             (1, "error: refusing to run: an agent in 'ws' could publish (see above).",
                              ["a push from wk's own run was not refused: nothing said"], False), force)

    def test_no_bwrap_refuses_the_session_and_force_does_not_cross_it(self):
        self.w.tools.clear()
        rc, err = self.ai("claude", "--force", "ws")
        self.assertEqual((rc, said(err), self.w.asked[-1]), (1, "error: the commit wall needs bwrap in the workspace image, and 'ws' has none.", "have bwrap"))

    def test_a_workspace_without_the_agent_is_refused_before_anything_is_measured(self):
        self.w.agents.clear()
        rc, err = self.ai("claude", "ws")
        self.assertEqual((rc, said(err), self.w.asked), (1, "error: there is no claude in 'ws' that runs: the agents are installed when a workspace",
                                                         ["agents.sh find claude"]))
        self.assertIn("    is made, and this one was made without it. Make it again:  wk rm ws && wk new ws", err.splitlines())

    def test_the_one_repository_an_agent_publishes_is_not_walled_and_the_session_says_so(self):
        """A push of wk-tools is served from wk's own run too: the push service's answer for an agent's repository."""
        self.w.files[os.path.join(self.w.ws_dir(), "home", ".wk-workspace")] = "name=ws\nrepo=wk-tools\n"
        self.w.answer(["git", "-c", "safe.directory=*"], out="git@github.com:u/wk-tools.git\n")
        self.w.wall["git ls-remote"] = "0123abcd\trefs/heads/main"
        rc, err = self.ai("claude", "ws")
        self.assertEqual((rc, "have bwrap" in self.w.asked, self.session()[-1]), (0, False, "cd /src/wk-tools && exec %s --dangerously-skip-permissions" % CLAUDE))
        self.assertIn("==> commits and pushes of wk-tools are this session's own: the one repository an agent publishes", err.splitlines())

    def test_a_dry_run_measures_the_wall_and_prints_the_session_without_starting_it(self):
        rc, err = self.ai("claude", "--dry-run", "ws")
        self.assertEqual((rc, [e for e in self.w.effects if e[0] == "run_tty"], self.w.asked[-1]), (0, [], "have bwrap"))
        self.assertRegex(err.splitlines()[-1], r"^would run: env .* --name wk-ws --no-tty --exec -- %s env WK_AGENT=claude .*--dangerously-skip-permissions'$" % BRIDGE)


class TestOnATerminal(AiTest):
    """`unit ai.remote_control`: a person's Claude session starts with Remote Control, named after the workspace."""

    def test_claude_on_a_terminal_starts_with_remote_control_named_after_the_workspace(self):
        self.on_a_terminal()
        rc, err = self.ai("claude", "ws", "-r")
        self.assertEqual((rc, self.session()[-1]), (0, "cd /src/WebKit && exec %s %s --dangerously-skip-permissions --remote-control ws -r" % (WALL, CLAUDE)))
        self.assertEqual([e for e in self.w.effects if e[0] == "run" and e[1][1:2] == ("key",)], [])

    def test_pi_on_a_terminal_and_claude_without_one_have_none(self):
        self.ai("claude", "ws", "-p", "fix it")
        self.assertEqual(self.session()[-1], "cd /src/WebKit && exec %s %s --dangerously-skip-permissions -p 'fix it'" % (WALL, CLAUDE))
        self.on_a_terminal()
        rc, err = self.ai("pi", "ws")
        self.assertEqual((rc, self.w.asked[0], self.session()[-1]), (0, "agents.sh find pi", "cd /src/WebKit && exec %s /home/u/.local/bin/pi" % WALL))


class TestUsage(AiTest):
    def test_no_agent_or_an_unknown_one_is_refused_before_anything_runs(self):
        for argv, line in (((), "warning: 'wk ai' needs one of: claude, pi"), (("gpt", "ws"), "warning: unknown verb: gpt (one of claude, pi)")):
            rc, err = self.ai(*argv)
            self.assertEqual((rc, err.splitlines()[0], self.w.effects), (2, line, []), argv)

    def test_no_such_workspace_or_an_invalid_name_is_refused_by_name_and_nothing_is_asked_in_one(self):
        for name in ("nope", "a/b"):
            rc, err = self.ai("claude", name)
            self.assertEqual((rc, err.splitlines()[0], self.w.asked), (2, "warning: no such workspace: %s -- 'wk ls' lists them" % name, []))


class OnABox(AiTest):
    def setUp(self):
        super().setUp()
        self.w = self.make_world(kinds={"fakebox": "remote"})


class TestOnABuildBox(OnABox):
    """A build box has no sandbox: a barrier, a gh login there a refusal no --force crosses, no wall to measure."""

    def test_it_is_a_barrier_and_nothing_is_asked_there(self):
        rc, err = self.ai("claude", "ws")
        self.assertEqual((rc, said(err), self.w.asked), (1, "error: 'ws' is on the shared build machine 'fakebox', which has no sandbox at", []))

    def test_forced_claude_runs_in_auto_mode_unwalled_in_its_checkout(self):
        rc, err = self.ai("claude", "--force", "ws")
        self.assertEqual((rc, self.w.asked, said(err)), (0, ["agents.sh find claude", GH_LOGIN, "session"],
                                                         "==> starting Claude in 'ws' (permissions relaxed; the workspace is the boundary)"))
        self.assertEqual(self.session(), ["ssh", "cd %s && %s bash -lc 'cd %s && exec %s --permission-mode auto'" % (BOX_SRC, SESSION, BOX_SRC, CLAUDE)])
        self.assertIn("==> Claude on fakebox runs in auto mode (this machine is not a sandbox)", err.splitlines())

    def test_a_checkout_path_with_a_space_survives_the_login_shell(self):
        w = self.make_world(kinds={"fakebox": "remote"})
        w.env["WK_REMOTE_ROOT"] = "/home/u/w k"
        w.far.add("/home/u/w k/ws/ws")
        self.ai("claude", "--force", "ws", w=w)
        line = shlex.split(self.session(w)[-1])
        self.assertEqual((line[:2], shlex.split(line[-1])[:2]), (["cd", "/home/u/w k/ws/ws/WebKit"], ["cd", "/home/u/w k/ws/ws/WebKit"]))

    def test_on_a_terminal_its_setup_token_starts_claude_without_remote_control_and_says_why(self):
        self.on_a_terminal()
        rc, err = self.ai("claude", "--force", "ws")
        self.assertEqual((rc, self.session()[-1].endswith("exec %s --permission-mode auto'" % CLAUDE)), (0, True))
        self.assertIn("warning: Remote Control is off for this session: a remote place authenticates with the", err.splitlines())

    def test_a_gh_login_there_is_a_refusal_force_does_not_cross(self):
        self.w.wall["gh auth status"] = Result(0)
        rc, err = self.ai("claude", "--force", "ws")
        self.assertEqual((rc, said(err), self.w.asked[-1]), (1, "error: 'fakebox' holds a GitHub credential in the account the agent would run as", GH_LOGIN))
        self.assertIn("    may post as you. Remedy, on fakebox:  gh auth logout", err.splitlines())

    def test_a_workspace_made_without_the_agent_is_refused_naming_the_remedy_and_nothing_is_installed(self):
        self.w.agents.clear()
        rc, err = self.ai("claude", "--force", "ws")
        self.assertEqual((rc, said(err), self.w.asked), (1, "error: there is no claude in 'ws' that runs: the agents are installed when a workspace",
                                                         ["agents.sh find claude"]))

    def test_pi_where_the_box_has_no_node_names_the_machines_setup(self):
        del self.w.agents["find pi"]
        rc, err = self.ai("pi", "--force", "ws")
        self.assertEqual((rc, said(err), self.w.asked), (1, "error: there is no pi in 'ws': it needs node >= 22.19 and npm, and fakebox has none.",
                                                         ["agents.sh find pi", "agents.sh node"]))
        self.assertIn("    Install one:  wk machine setup fakebox", err.splitlines())


class TestTheSessionIsAnEffect(OnABox):
    """The session is the Machine's, so a dry run prints it; a TERM or HUP to `wk ai` ends that session's own agents."""

    def test_on_a_terminal_ctrl_c_is_the_agents_while_it_runs_and_given_back_after(self):
        self.on_a_terminal()
        before = signal.getsignal(signal.SIGINT)
        self.w.session = lambda words: Result(0 if signal.getsignal(signal.SIGINT) not in (before, signal.SIG_IGN) else 9)
        rc, err = self.ai("claude", "--force", "ws")
        self.assertEqual((rc, signal.getsignal(signal.SIGINT)), (0, before))

    def test_a_sigterm_ends_this_sessions_agents_and_exits_with_its_status(self):
        running = ["4242"]
        self.w.wall["ps -Ao"] = lambda cmd: "".join(p + "\n" for p in running if "WK_SESSION=" in cmd)
        self.w.wall["kill -TERM"] = lambda cmd: running.clear() or ""
        self.w.session = lambda words: os.kill(os.getpid(), signal.SIGTERM) or Result(0)
        before = signal.getsignal(signal.SIGTERM)
        rc, err = self.ai("claude", "--force", "ws")
        self.assertEqual((rc, [a for a in self.w.asked if a.startswith("kill")], signal.getsignal(signal.SIGTERM)), (143, ["kill -TERM 4242"], before))

    def test_under_nohup_a_hangup_stays_ignored(self):
        old = signal.signal(signal.SIGHUP, signal.SIG_IGN)
        self.addCleanup(signal.signal, signal.SIGHUP, old)
        self.w.session = lambda words: os.kill(os.getpid(), signal.SIGHUP) or Result(0)
        rc, err = self.ai("claude", "--force", "ws")
        self.assertEqual((rc, self.w.asked[-1]), (0, "session"))

    def test_a_dry_run_prints_the_session_and_does_not_start_it(self):
        rc, err = self.ai("claude", "--force", "--dry-run", "ws")
        self.assertEqual((rc, [e for e in self.w.effects if e[0] == "run_tty"]), (0, []))
        self.assertRegex(err.splitlines()[-1], r"^would run: ssh .* box.example .*exec %s --permission-mode auto" % CLAUDE)


class TestOnAGuest(AiTest):
    """A macOS guest's egress is Softnet's, applied on the host when it boots. The world's guest has no golden base, which
    the wall reports, so each session here is forced past that."""

    def setUp(self):
        super().setUp()
        self.w = self.make_world(kinds={"fakebox": "vm"})

    def unfiltered(self):
        self.w.files[os.path.join(self.w.env["WK_VM_STORE"], "vm", "ws.unfiltered")] = ""

    def test_a_filtered_guest_says_what_its_boundary_is_and_starts_unwalled(self):
        rc, err = self.ai("claude", "--force", "ws")
        self.assertEqual((rc, self.forced(err), self.misses(err)), (0, [NOT_INTACT], ["no golden base VM 'wk-base' to clone a guest from"]))
        self.assertIn("==> 'ws' is a macOS VM: egress is filtered on the host by Softnet,", err.splitlines())
        self.assertEqual(self.session(), [self.w.tart, "cd /Users/admin/WebKit 2>/dev/null; %s bash -lc 'cd /Users/admin/WebKit && exec %s --dangerously-skip-permissions'" % (SESSION, CLAUDE)])

    def test_one_booted_unfiltered_is_a_barrier_of_its_own(self):
        self.unfiltered()
        rc, err = self.ai("claude", "--force", "ws")
        self.assertEqual((rc, self.forced(err)), (0, [NOT_INTACT, "'ws' was booted with NO egress filter."]))
        self.assertIn("this guest was booted with WK_VM_UNFILTERED, so it has the open network", self.misses(err))

    def test_no_softnet_is_a_barrier(self):
        self.w.answer(["test", "-x"], rc=1)
        rc, err = self.ai("claude", "--force", "ws")
        self.assertEqual((rc, self.forced(err)), (0, [NOT_INTACT, "softnet is not installed, so this guest's egress is not filtered."]))

    def test_the_older_spelling_is_a_warning_and_the_same_decision(self):
        self.unfiltered()
        self.w.env["WK_VM_UNFILTERED"] = "1"
        rc, err = self.ai("claude", "--force", "ws")
        self.assertEqual((rc, self.forced(err)), (0, [NOT_INTACT]))
        self.assertIn("warning: WK_VM_UNFILTERED=1: 'ws' has the open network. Of the host filesystem", err.splitlines())


if __name__ == "__main__":
    unittest.main()
