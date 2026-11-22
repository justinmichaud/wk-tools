import unittest

from tests.world import World
from wk.core import seams
from wk.core.machine import Result

SRC = "/home/u/wk/ws/x/src"


class TestEnter(unittest.TestCase):
    def setUp(self):
        self.w = World(here="lin")
        self.w.workspace("x")

    def test_at_a_terminal_it_is_the_drivers_login_shell(self):
        r = self.w.wk("enter", "x", tty=True)
        self.assertEqual((r.code, r.calls), (0, ["[lin] fake-enter x"]))

    def test_at_a_terminal_a_command_is_run_by_that_shell(self):
        r = self.w.wk("enter", "x", "--", "echo", "a b", tty=True)
        self.assertEqual(r.calls, ["[lin] fake-enter x -c 'echo '\"'\"'a b'\"'\"''"])

    def test_without_a_terminal_a_command_runs_in_the_checkout_and_its_exit_code_comes_back(self):
        self.w.machines["lin"].on(("fake-exec",), Result(3))
        r = self.w.wk("enter", "x", "--", "make", "-j", "4")
        self.assertEqual((r.code, r.calls), (3, ["[lin] fake-exec x %s make -j 4" % SRC]))

    def test_typed_on_the_client_it_runs_on_the_workspaces_server(self):
        w = World()
        w.workspace("x", on="mac")
        self.assertEqual(w.wk("enter", "x", "--", "true").effects, ["[mac] fake-exec x %s true" % SRC])

    def test_a_shell_without_a_terminal_is_refused_naming_the_command_form(self):
        r = self.w.wk("enter", "x")
        self.assertEqual((r.code, r.err, r.calls),
                         (4, "error: a shell needs a terminal and there is none: name a command, `wk enter x -- <command>`\n", []))

    def test_a_drivers_part_that_is_not_up_is_made_first(self):
        self.w.made.clear()
        r = self.w.wk("enter", "x", "--", "true")
        self.assertEqual((r.code, r.effects), (0, ["[lin] driver create x", "[lin] fake-exec x %s true" % SRC]))

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
        self.assertEqual((r.plan, r.effects), (["[lin] fake-exec x %s true" % SRC], []))
