import shlex
import unittest
from unittest import mock

from tests.world import World
from wk.core import seams
from wk.core.act import die
from wk.core.machine import Result
from wk.core.vars import VARS
from wk.creds import human, rotation
from wk.workspace import driver
from wk.workspace.drivers import ENTER

SRC = "/home/u/wk/ws/x/src"
HELD = "/home/u/wk/secrets/credentials.sh"


class TestEnter(unittest.TestCase):
    """A shell in the workspace, on a server that already holds a person's credentials."""

    def setUp(self):
        self.w = World(here="lin")
        self.w.workspace("x")
        self.w.machines["lin"].files[HELD] = "export GH_TOKEN=t\n"

    def test_at_a_terminal_it_is_the_persons_shell_on_that_terminal(self):
        r = self.w.wk("enter", "x", tty=True)
        self.assertEqual((r.code, r.effects), (0, ["[lin] fake-enter -t x"]))

    def test_at_a_terminal_a_command_is_run_by_that_shell(self):
        r = self.w.wk("enter", "x", "--", "echo", "a b", tty=True)
        self.assertEqual(r.effects, ["[lin] fake-enter -t x echo 'a b'"])

    def test_without_a_terminal_a_command_is_run_by_the_same_shell_and_its_exit_code_comes_back(self):
        self.w.machines["lin"].on(("fake-enter",), Result(3))
        r = self.w.wk("enter", "x", "--", "make", "-j", "4")
        self.assertEqual((r.code, r.effects), (3, ["[lin] fake-enter x make -j 4"]))

    def test_typed_on_a_workstation_it_is_refused_naming_the_servers(self):
        r = World().wk("enter", "x", "--", "true")
        self.assertEqual((r.code, r.calls), (1, []))
        self.assertIn("error: wk enter runs on a Linux server or a Mac server (box, lin, mac), and this is desk, a workstation", r.err)

    def test_on_each_real_driver_with_and_without_a_terminal_it_is_the_one_wrapper_that_sources_the_delivered_environment(self):
        for on, before in (("lin", "podman exec --interactive%s --workdir " + SRC + " wk-x env SHELL=/bin/bash "), ("mac", "%.0s")):
            w = World(here=on, seams={"driver": driver})
            w.workspace("x", on=on)
            w.machines[on].files[HELD] = "export GH_TOKEN=t\n"
            w.machines[on].on(("podman", "container", "exists"), Result(0))
            w.machines[on].on(("podman", "container", "inspect"), "running\n")
            for tty in (False, True):
                wrapper = "[%s] %ssh -c %s %s /home/u/wk/secrets/env.sh" % (on, before % (" --tty" * tty), shlex.quote(ENTER), SRC)
                self.assertEqual(w.wk("enter", "x", "--", "git", "push", tty=tty).effects, [wrapper + " -c 'git push'"])
            self.assertEqual(w.wk("enter", "x", tty=True).effects, [wrapper])

    def test_a_shell_without_a_terminal_is_refused_naming_the_command_form(self):
        r = self.w.wk("enter", "x")
        self.assertEqual((r.code, r.err, r.calls),
                         (4, "error: a shell needs a terminal and there is none: name a command, `wk enter x -- <command>`\n", []))

    def test_a_drivers_part_that_is_not_up_is_made_first(self):
        self.w.made.clear()
        r = self.w.wk("enter", "x", "--", "true")
        self.assertEqual((r.code, r.effects), (0, ["[lin] driver create x", "[lin] fake-enter x true"]))

    def test_a_name_the_server_does_not_hold_is_refused(self):
        r = self.w.wk("enter", "y", "--", "true")
        self.assertEqual((r.code, r.err), (1, "error: no workspace named y on lin: `wk ls` lists them, `wk new y` makes it\n"))

    def test_a_half_made_workspace_is_refused(self):
        self.w.workspace("h", state=seams.BROKEN)
        r = self.w.wk("enter", "h", "--", "true")
        self.assertEqual((r.code, r.effects), (1, []))
        self.assertEqual(r.err, "error: workspace h on lin is not made (broken): wait for its `wk new`, or `wk new h` remakes a broken one\n")

    def test_a_dry_run_prints_the_command_and_runs_nothing(self):
        r = self.w.wk("--dry-run", "enter", "x", "--", "true")
        self.assertEqual((r.plan, r.effects), (["[lin] fake-enter x true"], []))


class TestCredentials(unittest.TestCase):
    """A person's credentials, asked of the controller by a shell on a server that holds none."""

    def setUp(self):
        self.w = World(here="lin")
        self.w.workspace("x")
        self.w.secrets.values.update({"github-token": "ghp_t", "bugzilla-user": "me@example.org"})
        patch = mock.patch.object(rotation, "rotate", lambda inv, name: {fork: "key %s of %s" % (fork, name) for fork in VARS["wk_forks"]})
        patch.start()
        self.addCleanup(patch.stop)

    def test_they_are_asked_for_written_0600_and_said_before_the_shell(self):
        r = self.w.wk("enter", "x", "--", "true")
        lin = self.w.machines["lin"]
        deploy = ["/home/u/wk/secrets/deploy/" + fork.replace("/", "--") for fork in sorted(VARS["wk_forks"])]
        self.assertEqual(r.effects, ["[vault] POST /credentials"] + ["[lin] write " + p for p in deploy + [HELD]] + ["[lin] fake-enter x true"])
        self.assertEqual([lin.modes[p] for p in deploy + [HELD]], ["600"] * (len(deploy) + 1))
        self.assertEqual(lin.files[deploy[0]], "key %s of lin" % sorted(VARS["wk_forks"])[0])
        self.assertEqual(lin.files[HELD], human.script({"GITHUB_COM_TOKEN": "ghp_t", "GH_TOKEN": "ghp_t", human.BUGZILLA + "_USERNAME": "me@example.org"}))
        self.assertIn("export GH_TOKEN=ghp_t\n", lin.files[HELD])
        self.assertIn("==> delivered this session's push keys for", r.err)

    def test_held_ones_are_not_asked_for_again(self):
        self.w.wk("enter", "x", "--", "true")
        self.assertNotIn("[vault] POST /credentials", self.w.wk("enter", "x", "--", "true").effects)

    def test_a_running_agent_session_is_named_and_the_shell_still_opens(self):
        self.w.machines["lin"].on(("pgrep", "-a", "-f", human.AGENT), "4242 /home/u/wk/cache/agents/claude/2/claude --remote-control x\n")
        r = self.w.wk("enter", "x", "--", "true")
        self.assertEqual(r.code, 0)
        self.assertIn("warning: an agent session runs on lin and can read this shell's credentials:\n"
                      "    4242 /home/u/wk/cache/agents/claude/2/claude --remote-control x\n", r.err)
        self.assertIn("[lin] write " + HELD, r.effects)

    def test_a_controller_that_cannot_give_them_is_said_and_the_shell_opens_without(self):
        with mock.patch.object(rotation, "rotate", lambda inv, name: die("github-token is gone")):
            r = self.w.wk("enter", "x", "--", "true")
        self.assertEqual((r.code, r.effects), (0, ["[vault] POST /credentials", "[lin] fake-enter x true"]))
        self.assertIn("warning: the controller, vault, answered HTTP 502: github-token is gone\n"
                      "    so this shell has no push keys and no GitHub or Bugzilla login", r.err)

    def test_an_agents_shell_is_given_none(self):
        r = self.w.wk("enter", "x", "--", "true", env={"WK_AGENT": "claude"})
        self.assertEqual((r.code, r.effects), (0, ["[lin] fake-enter x true"]))
        self.assertIn("==> this shell is in claude's session (WK_AGENT), so it is given no credentials", r.err)
