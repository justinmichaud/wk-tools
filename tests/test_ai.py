"""`wk ai <agent> <ws>` (cmd/ai) as a flow over a fake machine: the wall's
checks are `wk doctor <ws>`'s own (lib/wk/wall.py), run against the healthy
container tests/test_doctor_wall.py answers for, and each refusal is driven by
taking one answer away. The session itself is never started: `foreground` is
replaced, and what it would have run is what is asserted."""
import contextlib
import io
import os
import shlex
import signal
import sys
import unittest
from unittest import mock

from tests.fakes import FakeRegistry, WsDriver
from tests.support import REPO, load_cmd, run
from tests.test_doctor_wall import _Wall

sys.path.insert(0, str(REPO / "lib"))
from wk import act, places, wall  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402

AI = load_cmd("ai")
WK = os.path.join(AI.ROOT, "wk")


class SimDriver(WsDriver):
    """A WsDriver whose commands answer by substring (`answers`), recorded as lines in `asked`; a handed-over argv
    names its terminal."""

    def __init__(self, machine, env, kind="container", name=None):
        super().__init__(name or kind, str(REPO), env, machine, kind)
        self.answers, self.asked = {}, []
        self.filtered = True
        self.remedy = "the place's own remedy"
        self.is_local = False

    def tools(self, ws):
        return "/opt/wk-tools"

    def egress_filtered(self, ws):
        return self.filtered

    def exec(self, ws, argv, tty=False, timeout=None):
        line = " ".join(argv)
        self.asked.append(line)
        for key, value in self.answers.items():
            if key in line:
                return value(argv) if callable(value) else value
        return Result(0)

    def exec_argv(self, ws, argv, tty=False):
        return ["exec", ws, "tty" if tty else "no-tty"] + list(argv), None

    def agent_secret_remedy(self, ws, secret):
        return self.remedy


def sim_registry(env, machine, driver):
    """A registry loading `place` under any name, on this host even when the suite runs in a workspace."""
    env.setdefault("WK_MARKER", "/nonexistent/wk-marker")
    return FakeRegistry(env, machine, lambda n, e: driver)


def quiet_env():
    """os.environ as a test of this command needs it: no answer given in advance, no barrier already crossed."""
    p = mock.patch.dict(os.environ, {}, clear=False)
    p.start()
    for v in ("WK_FORCE", "WK_YES", "WK_DRY_RUN", "WK_QUIET", "WK_DESTRUCTIVE", "WK_NAME", "WK_PLACE", "WK_DRIVER"):
        os.environ.pop(v, None)
    return p


class _Flow(unittest.TestCase):
    """Driving main() and reading what it said, what it ran and what it handed over."""

    def setUpFlow(self):
        env = quiet_env()
        self.addCleanup(env.stop)
        self.addCleanup(act._forced.clear)
        self.handed = []
        fg = mock.patch.object(AI, "foreground", side_effect=lambda machine, argv, cwd: self.handed.append(argv) or 0)
        fg.start()
        self.addCleanup(fg.stop)
        self.fg = fg
        self.terminal = mock.patch.object(AI, "on_a_terminal", return_value=False)
        self.terminal.start()
        self.addCleanup(self.terminal.stop)

    def ai(self, *argv, force=False):
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

    def switches(self):
        """What `wk ai` asked of any push switch: nothing, there is none."""
        return [e[1] for e in self.fake.effects if e[0] in ("run", "act") and e[1][:2] == (WK, "key")]

    def line(self):
        self.assertEqual(1, len(self.handed), self.handed)
        return self.handed[0][-1]


class _Host(_Flow, _Wall):
    """A healthy container, reached from the host."""

    def setUp(self):
        _Wall.setUp(self)
        self.setUpFlow()
        os.unlink(self.env["WK_MARKER"])
        self.env.update(WK_NAME="demo", WK_PLACE="container")
        self.fake.answer(["podman", "inspect", "wk-demo"], out="running\n")
        self.fake.files[os.path.join(self.driver.store.ws_dir("demo"), "home", places.READY_MARKER)] = ""
        self.fake.answer(["podman", "info"], out="true\n")
        self.fake.answer(["systemctl", "--user"])
        p = mock.patch.object(AI.Ai, "agent_bin", return_value="claude")
        p.start()
        self.addCleanup(p.stop)


class TestItVerifiesTheWall(_Host):
    """`unit ai.verifies_wall`."""

    def test_a_healthy_container_gets_doctors_report_and_the_session(self):
        status, err = self.ai("claude", "-r")
        self.assertEqual(0, status, err)
        for w in ("checking workspace 'demo' (container)", "workspace running", "a push is refused while an agent runs", "commit wall",
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

    def test_no_switch_is_thrown_and_the_session_is_not_wrapped_in_one(self):
        self.ai("claude")
        self.assertEqual([], self.switches())
        self.assertEqual(1, len(self.handed))

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
        with mock.patch.object(places.Container, "src", return_value="/src/Web Kit"):
            self.ai("claude")
        words = shlex.split(self.line())
        self.assertEqual("/src/Web Kit", words[1])
        self.assertIn("--ro-bind-try", words)
        self.assertIn("/src/Web Kit/.git/objects", words)

    def test_the_agents_own_arguments_reach_it_verbatim(self):
        self.ai("claude", "--continue", "two words")
        self.assertEqual(["--continue", "two words"], shlex.split(self.line())[-2:])

    def test_no_bwrap_refuses_the_session(self):
        self.set("bwrap", Result(1, "", ""))   # Machine.have's probe ends in the tool
        status, err = self.ai("claude", force=True)
        self.assertEqual(1, status, err)
        self.assertIn("the commit wall needs bwrap in the workspace image, and 'demo' has none", err)
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
        self.ai("claude", "-p", "fix it")
        self.assertNotIn("--remote-control", shlex.split(self.line()))

    def test_pi_has_none(self):
        self.on_a_terminal()
        status, err = self.ai("pi")
        self.assertEqual(0, status, err)
        self.assertNotIn("--remote-control", shlex.split(self.line()))


class TestNoPushSwitch(_Host):
    """A push is the host's service's to refuse while an agent runs, so `wk ai` throws nothing and restores nothing."""

    def test_a_terminal_session_and_a_headless_one_ask_nothing_of_any_switch(self):
        for terminal in (True, False):
            with self.subTest(terminal=terminal):
                self.terminal.stop()
                with mock.patch.object(AI, "on_a_terminal", return_value=terminal):
                    status, err = self.ai("claude")
                self.terminal.start()
                self.assertEqual(0, status, err)
                self.assertEqual([], self.switches())
                self.assertNotIn("git push", err)

    def test_a_workspace_without_the_agent_refuses_before_anything_is_measured(self):
        with mock.patch.object(AI.Ai, "agent_bin", side_effect=Refused(1)):
            status, err = self.ai("claude")
        self.assertEqual(1, status, err)
        self.assertEqual([], self.switches())

    def test_a_push_the_wall_would_not_refuse_stops_the_session_and_no_force_crosses_it(self):
        self.set("git ls-remote", "")
        for force in (False, True):
            with self.subTest(force=force):
                status, err = self.ai("claude", force=force)
                self.assertEqual(1, status, err)
                self.assertIn("an agent in 'demo' could publish", err)
                self.assertEqual([], self.handed)


class TestWhatIsWkTheAgentNever(_Flow):
    """The words wk refuses before anything is loaded."""

    def setUp(self):
        self.setUpFlow()
        self.fake = Fake()
        self.env = {"WK_NAME": "demo", "WK_PLACE": "container"}
        self.reg = sim_registry(self.env, self.fake, SimDriver(self.fake, self.env))

    def test_each_refusal(self):
        for argv, said in (((), "'wk ai' needs one of: claude, pi"),
                           (("gpt", "demo"), "unknown verb: gpt (one of claude, pi)")):
            with self.subTest(argv=argv):
                cp = run("ai", *argv)
                self.assertEqual(2, cp.returncode, cp.stdout)
                self.assertIn(said, cp.stdout)
                self.assertIn("usage: wk ai claude|pi <workspace>", cp.stdout)

    def test_an_invalid_name_is_refused(self):
        self.env["WK_NAME"] = "-x"
        self.assertIn("invalid name '-x'", self.ai("claude")[1])


class _Box(_Flow):
    def setUp(self):
        self.setUpFlow()
        self.fake = Fake()
        self.env = {"WK_NAME": "demo", "WK_PLACE": "box"}
        self.driver = SimDriver(self.fake, self.env, kind="remote", name="box")
        self.driver.answers["find claude"] = Result(0, "/home/u/.local/bin/claude\r\n")
        self.driver.answers["gh auth status"] = Result(1)
        self.reg = sim_registry(self.env, self.fake, self.driver)


class TestABuildBox(_Box):
    """No sandbox at all: a barrier, and a gh login there a refusal."""

    def test_it_is_a_barrier(self):
        status, err = self.ai("claude")
        self.assertEqual(1, status, err)
        self.assertIn("'demo' is on the shared build machine 'box', which has no sandbox", err)
        self.assertEqual([], self.fake.effects)

    def test_forced_it_runs_in_auto_mode(self):
        status, err = self.ai("claude", force=True)
        self.assertEqual(0, status, err)
        self.assertIn("'wk doctor demo' is not run for 'box'", err)
        self.assertIn("Claude on box runs in auto mode", err)
        self.assertIn("exec /home/u/.local/bin/claude --permission-mode auto", self.line())
        self.assertNotIn("bwrap", self.line())

    def test_the_setup_token_it_is_given_starts_the_session_without_remote_control_and_says_why(self):
        with mock.patch.object(AI, "on_a_terminal", return_value=True):
            status, err = self.ai("claude", force=True)
        self.assertEqual(0, status, err)
        self.assertIn("Remote Control is off for this session", err)
        self.assertNotIn("--remote-control", self.line())

    def test_a_gh_login_there_is_a_refusal_force_does_not_cross(self):
        self.driver.answers["gh auth status"] = Result(0)
        status, err = self.ai("claude", force=True)
        self.assertEqual(1, status, err)
        self.assertIn("holds a GitHub credential", err)
        self.assertIn("Remedy, on box:  gh auth logout", err)
        self.assertEqual([], self.handed)

    def test_a_workspace_made_without_the_agent_is_refused_naming_the_remedy(self):
        self.driver.answers["find claude"] = Result(1)
        status, err = self.ai("claude", force=True)
        self.assertEqual(1, status, err)
        self.assertIn("there is no claude in 'demo' that runs", err)
        self.assertIn("wk rm demo && wk new demo", err)
        self.assertEqual([], self.handed)
        self.assertFalse([a for a in self.driver.asked if "install.sh" in a and "find" not in a])

    def test_pi_on_a_box_without_node_names_the_setup(self):
        self.driver.answers["find pi"] = Result(1)
        self.driver.answers["agents.sh node"] = Result(1)
        status, err = self.ai("pi", force=True)
        self.assertEqual(1, status, err)
        self.assertIn("wk machine setup", err)
        self.assertNotIn("Make it again", err)


class TestTheSessionIsAnEffect(_Box):
    """The session starts through the Machine, so --dry-run prints it."""

    def setUp(self):
        super().setUp()
        self.fg.stop()
        self.fake.answer(["exec"])

    def sessions(self):
        return [e for e in self.fake.effects if e[0] == "run_tty"]

    def test_a_wet_run_starts_the_session_as_an_effect_and_gives_ctrl_c_back(self):
        before = signal.getsignal(signal.SIGINT)
        status, err = self.ai("claude", force=True)
        self.assertEqual(0, status, err)
        [(_, argv, cwd)] = self.sessions()
        self.assertEqual(("exec", "demo", "no-tty"), argv[:3])
        self.assertIn("exec /home/u/.local/bin/claude --permission-mode auto", argv[-1])
        self.assertEqual([], self.switches())
        self.assertIs(before, signal.getsignal(signal.SIGINT))

    def test_a_dry_run_prints_the_session_and_does_not_start_it(self):
        os.environ["WK_DRY_RUN"] = "1"
        status, err = self.ai("claude", force=True)
        self.assertEqual(0, status, err)
        self.assertRegex(err, r"would run on fake: exec demo no-tty .*exec /home/u/.local/bin/claude --permission-mode auto")
        self.assertNotIn("would run in demo", err)
        self.assertEqual([], self.sessions())


class TestAGuest(_Flow):
    """A macOS guest's egress is Softnet's, applied on the host at boot."""

    def setUp(self):
        self.setUpFlow()
        self.fake = Fake()
        self.fake.answer(["test", "-x"])
        self.env = {"WK_NAME": "demo", "WK_PLACE": "vm"}
        self.driver = SimDriver(self.fake, self.env, kind="vm")
        self.driver.answers["find claude"] = Result(0, "claude\n")
        self.reg = sim_registry(self.env, self.fake, self.driver)
        p = mock.patch.object(AI.Ai, "checks")
        p.start()
        self.addCleanup(p.stop)

    def test_a_filtered_guest_starts_and_says_what_its_boundary_is(self):
        status, err = self.ai("claude")
        self.assertEqual(0, status, err)
        self.assertIn("egress is filtered on the host by Softnet", err)
        self.assertNotIn("bwrap", self.line())

    def test_one_booted_unfiltered_is_a_barrier(self):
        self.driver.filtered = False
        status, err = self.ai("claude")
        self.assertEqual(1, status, err)
        self.assertIn("'demo' was booted with NO egress filter", err)

    def test_no_softnet_is_a_barrier(self):
        self.fake.answer(["test", "-x"], rc=1)
        status, err = self.ai("claude")
        self.assertEqual(1, status, err)
        self.assertIn("softnet is not installed", err)

    def test_the_older_spelling_is_a_warning_and_the_same_decision(self):
        self.driver.filtered = False
        self.env["WK_VM_UNFILTERED"] = "1"
        status, err = self.ai("claude")
        self.assertEqual(0, status, err)
        self.assertIn("WK_VM_UNFILTERED=1: 'demo' has the open network", err)


if __name__ == "__main__":
    unittest.main()
