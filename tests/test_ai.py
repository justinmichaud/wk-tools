"""`wk ai <agent> <ws>` (cmd/ai) as a flow over a fake machine: the wall's
checks are `wk doctor <ws>`'s own (lib/wk/wall.py), run against the healthy
container tests/test_doctor_wall.py answers for, and each refusal is driven by
taking one answer away. The session itself is never started: `foreground` is
replaced, and what it would have run is what is asserted.

Run: python3 tests/run.py --unit -k test_ai
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import shlex
import signal
import sys
import time
import unittest
from unittest import mock

from tests.support import REPO
from tests.test_doctor_wall import _Wall

sys.path.insert(0, str(REPO / "lib"))
from wk import act, targets, wall  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402


def load_ai():
    """cmd/ai as a module: a real file with no extension needs its loader spelled out."""
    path = str(REPO / "cmd" / "ai")
    loader = importlib.machinery.SourceFileLoader("wk_cmd_ai", path)
    spec = importlib.util.spec_from_file_location("wk_cmd_ai", path, loader=loader)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


AI = load_ai()
WK = os.path.join(AI.ROOT, "wk")


class SimTarget(targets.Target):
    """A workspace whose exec answers from `answers`, keyed by a substring of the command line (the first key found
    answers; a callable is given the argv), every command line in `asked`."""

    def __init__(self, machine, env, kind="container", name=None):
        super().__init__(name or kind, str(REPO), env, machine)
        self.kind = kind
        self.answers = {}
        self.asked = []
        self.filtered = True
        self.present, self.remedy = True, "the target's own remedy"

    def home(self):
        return "/home/u"

    def src(self, ws):
        return "/src/WebKit"

    def tools(self, ws):
        return "/opt/wk-tools"

    def egress_filtered(self, ws):
        return self.filtered

    def info(self, ws):
        return "running"

    def exec(self, ws, argv, tty=False, timeout=None):
        line = " ".join(argv)
        self.asked.append(line)
        for key, value in self.answers.items():
            if key in line:
                return value(argv) if callable(value) else value
        return Result(0)

    def exec_argv(self, ws, argv, tty=False):
        return ["exec", ws, "tty" if tty else "no-tty"] + list(argv), None

    def agent_secret_present(self, ws, secret):
        return self.present

    def agent_secret_remedy(self, ws, secret):
        return self.remedy


class SimRegistry(targets.Registry):
    def __init__(self, env, machine, target):
        env.setdefault("WK_MARKER", "/nonexistent/wk-marker")   # this host, even when the suite runs in a workspace
        super().__init__(str(REPO), env=env, machine=machine)
        self.target = target

    def load(self, name):
        return self.target


def quiet_env():
    """os.environ as a test of this command needs it: no answer given in advance, no barrier already crossed."""
    p = mock.patch.dict(os.environ, {}, clear=False)
    p.start()
    for v in ("WK_FORCE", "WK_YES", "WK_DRY_RUN", "WK_QUIET", "WK_DESTRUCTIVE", "WK_NAME", "WK_TARGET", "WK_TARGET_KIND"):
        os.environ.pop(v, None)
    return p


class _Flow(unittest.TestCase):
    """Driving main() and reading what it said, what it ran and what it handed over."""

    def setUpFlow(self):
        env = quiet_env()
        self.addCleanup(env.stop)
        self.addCleanup(act._forced.clear)
        self.handed = []
        fg = mock.patch.object(AI, "foreground", side_effect=lambda argv, cwd: self.handed.append(argv) or 0)
        fg.start()
        self.addCleanup(fg.stop)
        self.terminal = mock.patch.object(AI, "on_a_terminal", return_value=False)
        self.terminal.start()
        self.addCleanup(self.terminal.stop)

    def ai(self, *argv, force=False):
        """(status, stderr)."""
        if force:
            os.environ["WK_FORCE"] = "1"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                status = AI.main(list(argv), env=self.env, reg=self.reg)
            except Refused as e:
                status = e.status
        os.environ.pop("WK_FORCE", None)
        return status, err.getvalue()

    def pushes(self):
        return [" ".join(e[1][1:]) for e in self.fake.effects if e[0] == "run" and e[1][:2] == (WK, "push")]

    def line(self):
        """The login-shell text the session was handed."""
        self.assertEqual(1, len(self.handed), self.handed)
        return self.handed[0][-1]


class _Host(_Flow, _Wall):
    """A healthy container, reached from the host, with the switch off."""

    def setUp(self):
        _Wall.setUp(self)
        self.setUpFlow()
        os.unlink(self.env["WK_MARKER"])
        self.env.update(WK_NAME="demo", WK_TARGET="container")
        self.fake.answer(["podman", "inspect", "wk-demo"], out="running\n")
        self.fake.files[os.path.join(self.target.store.ws_dir("demo"), "home", targets.READY_MARKER)] = ""
        self.fake.answer([WK, "push", "status"], rc=1)
        self.fake.answer([WK, "push"])
        self.fake.answer(["podman", "info"], out="true\n")
        self.fake.answer(["systemctl", "--user"])


class TestItVerifiesTheWall(_Host):
    """`unit ai.verifies_wall`."""

    def test_a_healthy_container_gets_doctors_report_and_the_session(self):
        status, err = self.ai("claude", "-r")
        self.assertEqual(0, status, err)
        for w in ("checking workspace 'demo' (container)", "workspace running", "the host says push is OFF", "commit wall",
                  "podman is rootless", "egress proxy running", "sandbox intact"):
            self.assertIn(w, err)
        self.assertIn("--dangerously-skip-permissions -r", self.line())

    def test_the_checks_run_at_once(self):
        with mock.patch.object(wall, "run_at_once", wraps=wall.run_at_once) as at_once:
            self.ai("claude")
        self.assertEqual(1, at_once.call_count)
        names = [n for n, _ in at_once.call_args[0][0]]
        for n in ("egress-github", "isolation", "commit-wall", "github-write", "agent-credential"):
            self.assertIn(n, names)

    def test_a_stopped_proxy_is_refused_and_force_repeats_the_warning_at_exit(self):
        self.fake.answer(["systemctl", "--user"], rc=3)
        status, err = self.ai("claude")
        self.assertEqual(1, status, err)
        self.assertIn("egress proxy is not running", err)
        self.assertIn("the sandbox around 'demo' is not intact (see above)", err)
        self.assertIn("--force proceeds anyway", err)
        self.assertEqual([], self.handed)
        status, err = self.ai("claude", force=True)
        self.assertEqual(0, status, err)
        self.assertIn("FORCED past a barrier", err)
        with contextlib.redirect_stderr(io.StringIO()) as summary:
            act.forced_summary()
        self.assertIn("this command was forced past 1 barrier(s):\n- the sandbox around 'demo' is not intact", summary.getvalue())

    def test_a_probe_that_fails_to_answer_is_a_refusal(self):
        with mock.patch.object(wall.Wall, "gpu", side_effect=OSError("ssh went away")):
            status, err = self.ai("claude")
        self.assertEqual(1, status, err)
        self.assertIn("the 'gpu' probe died before it reported anything (ssh went away)", err)
        self.assertEqual([], self.handed)

    def test_a_host_the_proxy_lets_through_is_a_refusal_that_says_so(self):
        self.set("example.com", "")
        status, err = self.ai("claude")
        self.assertEqual(1, status, err)
        self.assertIn("example.com was NOT refused", err)
        self.assertIn("the allowlist is not being enforced", err)

    def test_the_keys_are_held_back_before_anything_is_measured(self):
        self.fake.answer([WK, "push", "status"], rc=0)
        self.ai("claude")
        runs = [e[1] for e in self.fake.effects if e[0] == "run"]
        off = runs.index((WK, "push", "off"))
        self.assertLess(off, runs.index(("podman", "inspect", "wk-demo", "--format", "{{.State.Status}}")))

    def test_the_commit_wall_goes_in_front_of_the_agent(self):
        self.ai("claude")
        words = shlex.split(self.line())
        self.assertEqual(["cd", "/src/WebKit", "&&", "exec"], words[:4])
        prefix = wall.commit_wall_prefix(AI.ROOT, "/src/WebKit")
        self.assertEqual(prefix, words[4:4 + len(prefix)])
        self.assertEqual(["claude", "--dangerously-skip-permissions"], words[4 + len(prefix):])

    def test_every_hand_over_names_the_agent_and_the_workspace(self):
        self.ai("claude")
        self.assertEqual(["env", "WK_AGENT=claude", "WK_WORKSPACE=demo", "bash", "-lc"], self.handed[0][-6:-1])

    def test_a_checkout_path_with_a_space_survives_the_login_shell(self):
        with mock.patch.object(targets.Container, "src", return_value="/src/Web Kit"):
            self.ai("claude")
        words = shlex.split(self.line())
        self.assertEqual("/src/Web Kit", words[1])
        self.assertIn("--ro-bind-try", words)
        self.assertIn("/src/Web Kit/.git/objects", words)

    def test_the_agents_own_arguments_reach_it_verbatim(self):
        self.ai("claude", "--continue", "two words")
        self.assertEqual(["--continue", "two words"], shlex.split(self.line())[-2:])

    def test_no_bwrap_refuses_the_session(self):
        self.set("command -v bwrap", Result(1, "", ""))
        status, err = self.ai("claude", force=True)
        self.assertEqual(1, status, err)
        self.assertIn("the commit wall needs bwrap in the workspace image, and 'demo' has none", err)
        self.assertEqual([], self.handed)

    def test_the_session_records_claudes_answers_first(self):
        self.ai("claude")
        self.assertTrue([a for a in self.asked if "claude/workspace-config.py /src/WebKit" in a])

    def test_answers_that_cannot_be_recorded_refuse_the_session(self):
        self.set("workspace-config.py", Result(1, "", "not readable as JSON"))
        status, err = self.ai("claude")
        self.assertEqual(1, status, err)
        self.assertIn("could not record Claude's start-up answers in 'demo'", err)
        self.assertIn("    not readable as JSON", err)
        self.assertEqual([], self.handed)

class TestRemoteControl(_Host):
    """A Claude session a person is at starts with Remote Control named after the workspace (`unit ai.remote_control`)."""

    def on_a_terminal(self):
        self.terminal.stop()
        p = mock.patch.object(AI, "on_a_terminal", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def test_a_terminal_session_is_named_after_the_workspace(self):
        self.on_a_terminal()
        status, err = self.ai("claude", "-r")
        self.assertEqual(0, status, err)
        words = shlex.split(self.line())
        self.assertEqual(["claude", "--dangerously-skip-permissions", "--remote-control", "demo", "-r"], words[words.index("claude"):])

    def test_a_headless_session_has_none(self):
        """The babysitter's `-p` fix attempt has no terminal and no one to join it."""
        self.ai("claude", "-p", "fix it")
        self.assertNotIn("--remote-control", shlex.split(self.line()))

    def test_pi_has_none(self):
        self.on_a_terminal()
        with mock.patch.object(AI.Ai, "pi_ensure", return_value="/home/u/.local/bin/pi"):
            status, err = self.ai("pi")
        self.assertEqual(0, status, err)
        self.assertNotIn("--remote-control", shlex.split(self.line()))


class TestThePushSwitch(_Host):
    """In an agent session nothing can push; a person's terminal gets the switch back."""

    def setUp(self):
        super().setUp()
        self.push_on = True
        for verb in ("status", "off", "on"):
            self.fake.react([WK, "push", verb], self._push)

    def _push(self, argv, fake):
        if argv[2] == "status":
            return Result(0 if self.push_on else 1)
        self.push_on = argv[2] == "on"
        return Result(0)

    def test_a_terminal_session_turns_push_back_on_at_exit(self):
        self.terminal.stop()
        with mock.patch.object(AI, "on_a_terminal", return_value=True):
            status, err = self.ai("claude")
        self.terminal.start()
        self.assertEqual(0, status, err)
        self.assertEqual(["push status", "push off", "push status", "push on"], self.pushes(), "the doctor reads the switch once")
        self.assertIn("git push turned back on", err)

    def test_a_sigterm_during_the_session_still_restores_the_push_switch(self):
        """SIGTERM has no Python-level default (unlike SIGINT's KeyboardInterrupt) and
        would otherwise end the process before `finally` runs and turns the switch back on."""
        self.terminal.stop()

        def killed_mid_session(argv, cwd):
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.2)   # gives the pending signal a chance to be delivered before returning
            return 0
        with mock.patch.object(AI, "on_a_terminal", return_value=True), \
                mock.patch.object(AI, "foreground", side_effect=killed_mid_session):
            with self.assertRaises(SystemExit):
                self.ai("claude")
        self.terminal.start()
        self.assertEqual(["push status", "push off", "push status", "push on"], self.pushes())

    def test_a_headless_session_leaves_it_off(self):
        status, err = self.ai("claude")
        self.assertEqual(0, status, err)
        self.assertEqual(["push status", "push off", "push status"], self.pushes())
        self.assertIn("git push stays off (headless session", err)

    def test_a_refused_session_after_the_switch_still_says_where_push_is(self):
        self.set("workspace-config.py", Result(1, "", "boom"))
        _, err = self.ai("claude")
        self.assertIn("git push stays off", err)

    def test_a_switch_that_would_not_go_off_refuses(self):
        self.fake.answer([WK, "push", "status"], rc=0)
        self.fake.answer([WK, "push", "off"], rc=1)
        status, err = self.ai("claude")
        self.assertEqual(1, status, err)
        self.assertIn("refusing to run: could not hold back the push keys ('wk push status')", err)
        self.assertEqual([], self.handed)

    def test_an_unmeasured_switch_refuses(self):
        for st in (3, 5):
            with self.subTest(status=st):
                self.fake.answer([WK, "push", "status"], rc=st)
                status, err = self.ai("claude")
                self.assertEqual(1, status, err)
                self.assertIn("'wk push status' exited %d rather" % st, err)


class TestWhatIsWkTheAgentNever(_Flow):
    """The words wk refuses before anything is loaded."""

    def setUp(self):
        self.setUpFlow()
        self.fake = Fake()
        self.env = {"WK_NAME": "demo", "WK_TARGET": "container"}
        self.reg = SimRegistry(self.env, self.fake, SimTarget(self.fake, self.env))

    def test_each_refusal(self):
        for argv, said in (((), "usage: wk ai claude|pi <workspace>"),
                           (("gpt",), "unknown agent 'gpt'. This checkout knows: claude, pi")):
            with self.subTest(argv=argv):
                status, err = self.ai(*argv)
                self.assertEqual(1, status, err)
                self.assertIn(said, err)
        self.assertEqual([], self.fake.effects)

    def test_an_invalid_name_is_refused(self):
        self.env["WK_NAME"] = "-x"
        self.assertIn("invalid name '-x'", self.ai("claude")[1])


class TestABuildBox(_Flow):
    """No sandbox at all: a barrier, the switch thrown on its own store, and a gh login there a refusal."""

    def setUp(self):
        self.setUpFlow()
        self.fake = Fake()
        self.fake.answer([WK, "push"], rc=1)
        self.env = {"WK_NAME": "demo", "WK_TARGET": "box"}
        self.target = SimTarget(self.fake, self.env, kind="remote", name="box")
        self.target.answers["command -v claude"] = Result(0, "/home/u/.local/bin/claude\r\n")
        self.target.answers["gh auth status"] = Result(1)
        self.reg = SimRegistry(self.env, self.fake, self.target)

    def test_it_is_a_barrier(self):
        status, err = self.ai("claude")
        self.assertEqual(1, status, err)
        self.assertIn("'demo' is on the shared build machine 'box', which has no sandbox", err)
        self.assertEqual([], self.fake.effects)

    def test_forced_it_runs_in_auto_mode_with_the_switch_named(self):
        status, err = self.ai("claude", force=True)
        self.assertEqual(0, status, err)
        self.assertEqual(["push status --target box"], self.pushes())
        self.assertIn("'wk doctor demo' is not run for 'box'", err)
        self.assertIn("using /home/u/.local/bin/claude on box, in auto mode", err)
        self.assertIn("exec /home/u/.local/bin/claude --permission-mode auto", self.line())
        self.assertNotIn("bwrap", self.line())

    def test_the_inference_token_there_starts_the_session_without_remote_control_and_says_why(self):
        self.target.answers['"${CLAUDE_CODE_OAUTH_TOKEN:+set}"'] = Result(0, "set\r\n")
        with mock.patch.object(AI, "on_a_terminal", return_value=True):
            status, err = self.ai("claude", force=True)
        self.assertEqual(0, status, err)
        self.assertIn("Remote Control is off for this session: $CLAUDE_CODE_OAUTH_TOKEN authenticates it", err)
        self.assertNotIn("--remote-control", self.line())

    def test_a_login_there_gets_remote_control(self):
        with mock.patch.object(AI, "on_a_terminal", return_value=True):
            status, err = self.ai("claude", force=True)
        self.assertEqual(0, status, err)
        self.assertIn("--permission-mode auto --remote-control demo", self.line())
        self.assertNotIn("Remote Control is off", err)

    def test_a_gh_login_there_is_a_refusal_force_does_not_cross(self):
        self.target.answers["gh auth status"] = Result(0)
        status, err = self.ai("claude", force=True)
        self.assertEqual(1, status, err)
        self.assertIn("holds a GitHub credential", err)
        self.assertIn("Remedy, on box:  gh auth logout", err)
        self.assertEqual([], self.handed)

    def test_no_claude_there_is_installed_and_asked_again(self):
        found = iter([Result(1), Result(0, "/home/u/.local/bin/claude\n")])
        del self.target.answers["command -v claude"]
        self.target.answers["command -v claude 2>/dev/null)\"; do"] = lambda argv: next(found)
        self.target.answers["install.sh"] = Result(0, "installed\n")
        status, err = self.ai("claude", force=True)
        self.assertEqual(0, status, err)
        self.assertIn("there is no Claude CLI on box that runs", err)
        self.assertIn("installing the Claude CLI on box", err)
        self.assertIn("ssh box claude --version", [a for a in self.target.asked if "on PATH" in a][0])

    def test_an_installer_that_fails_refuses_with_the_command(self):
        del self.target.answers["command -v claude"]
        self.target.answers["command -v claude 2>/dev/null)\"; do"] = Result(1)
        self.target.answers["install.sh"] = Result(1)
        status, err = self.ai("claude", force=True)
        self.assertEqual(1, status, err)
        self.assertIn("the installer failed on box. By hand:\n    ssh box 'curl -fsSL https://claude.ai/install.sh | bash'", err)


class TestAGuest(_Flow):
    """A macOS guest's egress is Softnet's, applied on the host at boot."""

    def setUp(self):
        self.setUpFlow()
        self.fake = Fake()
        self.fake.answer([WK, "push"], rc=1)
        self.fake.answer(["test", "-x"])
        self.env = {"WK_NAME": "demo", "WK_TARGET": "vm"}
        self.target = SimTarget(self.fake, self.env, kind="vm")
        self.reg = SimRegistry(self.env, self.fake, self.target)
        p = mock.patch.object(AI.Ai, "checks")
        p.start()
        self.addCleanup(p.stop)

    def test_a_filtered_guest_starts_and_says_what_its_boundary_is(self):
        status, err = self.ai("claude")
        self.assertEqual(0, status, err)
        self.assertIn("egress is filtered on the host by Softnet", err)
        self.assertNotIn("bwrap", self.line())

    def test_one_booted_unfiltered_is_a_barrier(self):
        self.target.filtered = False
        status, err = self.ai("claude")
        self.assertEqual(1, status, err)
        self.assertIn("'demo' was booted with NO egress filter", err)

    def test_no_softnet_is_a_barrier(self):
        self.fake.answer(["test", "-x"], rc=1)
        status, err = self.ai("claude")
        self.assertEqual(1, status, err)
        self.assertIn("softnet is not installed", err)

    def test_the_older_spelling_is_a_warning_and_the_same_decision(self):
        self.target.filtered = False
        self.env["WK_VM_UNFILTERED"] = "1"
        status, err = self.ai("claude")
        self.assertEqual(0, status, err)
        self.assertIn("WK_VM_UNFILTERED=1: 'demo' has the open network", err)


if __name__ == "__main__":
    unittest.main()
