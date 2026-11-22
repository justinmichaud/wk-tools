import shlex
import unittest

from tests.world import World
from wk.core import seams
from wk.core.machine import Result
from wk.workspace import driver
from wk.workspace.drivers import ENTER

SRC = "/home/u/wk/ws/x/src"


class TestEnter(unittest.TestCase):
    def setUp(self):
        self.w = World(here="lin")
        self.w.workspace("x")

    def test_at_a_terminal_it_is_the_persons_shell_on_that_terminal(self):
        r = self.w.wk("enter", "x", tty=True)
        self.assertEqual((r.code, r.calls), (0, ["[lin] fake-enter -t x"]))

    def test_at_a_terminal_a_command_is_run_by_that_shell(self):
        r = self.w.wk("enter", "x", "--", "echo", "a b", tty=True)
        self.assertEqual(r.calls, ["[lin] fake-enter -t x echo 'a b'"])

    def test_without_a_terminal_a_command_is_run_by_the_same_shell_and_its_exit_code_comes_back(self):
        self.w.machines["lin"].on(("fake-enter",), Result(3))
        r = self.w.wk("enter", "x", "--", "make", "-j", "4")
        self.assertEqual((r.code, r.calls), (3, ["[lin] fake-enter x make -j 4"]))

    def test_typed_on_the_client_it_runs_on_the_workspaces_server(self):
        w = World()
        w.workspace("x", on="mac")
        self.assertEqual(w.wk("enter", "x", "--", "true").effects, ["[mac] fake-enter x true"])

    def test_on_each_real_driver_with_and_without_a_terminal_it_is_the_one_wrapper_that_sources_the_delivered_environment(self):
        for on, before in (("lin", "podman exec --interactive%s --workdir " + SRC + " wk-x env SHELL=/bin/bash "), ("mac", "%.0s"), ("box", "%.0s")):
            w = World(here=on, seams={"driver": driver})
            w.workspace("x", on=on)
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
