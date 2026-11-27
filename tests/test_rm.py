import unittest

from tests.killpoints import converges, dry_run_is_the_plan
from tests.world import World
from tests.ws import RELEASE, WIPE
from wk.core import seams


def make(**how):
    w = World(here="lin", **how)
    w.workspace("x")
    return w


class TestRm(unittest.TestCase):
    def setUp(self):
        self.w = make()

    def test_ready_goes_first_then_the_drivers_part_then_the_directory(self):
        r = self.w.wk("rm", "x", "--yes")
        self.assertEqual((r.code, r.effects), (0, WIPE + [RELEASE]))
        self.assertIn("==> removed workspace x from lin\n", r.err)
        self.assertEqual((self.w.machines["lin"].files, self.w.made, self.w.wk("ls").out), ({}, set(), ""))

    def test_it_asks_once_and_a_no_changes_nothing(self):
        r = self.w.wk("rm", "x", stdin="n\n", tty=True)
        self.assertEqual((r.code, r.effects), (1, []))
        self.assertEqual(r.err, "remove workspace x on lin, with its checkout and its build? [y/N] error: declined: nothing is changed\n")

    def test_a_yes_at_the_terminal_removes_it(self):
        self.assertEqual(self.w.wk("rm", "x", stdin="y\n", tty=True).effects, WIPE + [RELEASE])

    def test_without_a_terminal_it_declines(self):
        r = self.w.wk("rm", "x")
        self.assertEqual((r.code, r.effects), (4, []))

    def test_a_half_made_workspace_is_removed_too(self):
        w = World(here="lin")
        w.workspace("x", state=seams.BROKEN)
        self.assertEqual(w.wk("rm", "x", "--yes").effects, WIPE + [RELEASE])
        self.assertEqual(w.machines["lin"].files, {})

    def test_a_running_or_queued_task_of_the_workspace_refuses_it_as_busy(self):
        for label, state in (("x:build", seams.RUNNING), ("x", seams.QUEUED)):
            w = make()
            w.tasks("lin").tasks.append(seams.Task(0, label, "build", state, "make"))
            r = w.wk("rm", "x", "--yes")
            self.assertEqual((r.code, r.effects), (2, []))
            self.assertIn("error: workspace x has a %s task (%s): wait for it, or end it with `wk stop --tasks`\n" % (state, label), r.err)

    def test_an_ended_task_and_another_workspaces_task_do_not(self):
        self.w.tasks("lin").tasks += [seams.Task(0, "x:build", "build", seams.DONE, "make", 0),
                                      seams.Task(1, "xy:build", "build", seams.RUNNING, "make")]
        self.assertEqual(self.w.wk("rm", "x", "--yes").code, 0)

    def test_one_that_a_wk_new_is_making_is_refused_as_busy(self):
        w = World(here="lin")
        w.workspace("x", state=seams.BROKEN)
        w.machines["lin"].on(("pgrep",), "4242\n")
        r = w.wk("rm", "x", "--yes")
        self.assertEqual((r.code, r.effects), (2, []))
        self.assertIn("error: a `wk new x` is making it on lin: wait for it to end\n", r.err)
        self.assertEqual(w.wk("rm", "x", "--yes", "--force").code, 0)
        self.assertEqual(w.machines["lin"].listdir("/home/u/wk/ws"), [])

    def test_a_name_the_server_does_not_hold_is_refused(self):
        r = self.w.wk("rm", "y", "--yes")
        self.assertEqual((r.code, r.err, r.effects), (1, "error: no workspace named y on lin: `wk ls` lists them\n", []))

    def test_the_name_is_released_last_and_the_controllers_record_is_left_without_it(self):
        r = self.w.wk("rm", "x", "--yes")
        self.assertEqual(r.effects[-1], RELEASE)
        self.assertEqual(self.w.machines["vault"].listdir("/home/u/wk/ws"), [])

    def test_a_name_another_server_holds_is_left_with_a_warning(self):
        self.w.reserve("x", "mac")
        r = self.w.wk("rm", "x", "--yes")
        self.assertEqual((r.code, r.effects[-1]), (0, RELEASE))
        self.assertIn("warning: the name x is mac's, so it is left as it is\n", r.err)
        self.assertEqual(self.w.machines["vault"].files["/home/u/wk/ws/x"], "mac")

    def test_a_workspace_made_with_no_reservation_is_removed_and_nothing_is_released(self):
        del self.w.machines["vault"].files["/home/u/wk/ws/x"]
        r = self.w.wk("rm", "x", "--yes")
        self.assertEqual((r.code, r.effects, r.err), (0, WIPE + [RELEASE], "==> removed workspace x from lin\n"))

    def test_a_controller_that_does_not_answer_the_release_is_a_barrier_after_the_removal(self):
        self.w.machines["vault"].down = True
        r = self.w.wk("rm", "x", "--yes")
        self.assertEqual((r.code, r.effects), (4, WIPE))
        self.assertIn("error: the controller, vault, did not answer (the fake is down)\n    so the name x is still reserved: "
                      "`wk rm x` again releases it\n    --force crosses this\n", r.err)
        self.w.machines["vault"].down = False
        again = self.w.wk("rm", "x", "--yes")
        self.assertEqual((again.code, again.effects, again.err), (0, [RELEASE], "==> released the name x\n"))
        self.assertEqual(self.w.machines["vault"].listdir("/home/u/wk/ws"), [])

    def test_a_name_held_here_with_no_workspace_asks_before_it_is_released(self):
        self.w.reserve("y", "lin")
        r = self.w.wk("rm", "y", stdin="n\n", tty=True)
        self.assertEqual((r.code, r.effects), (1, []))
        self.assertIn("release the name y, which lin holds with no workspace? [y/N]", r.err)

    def test_with_no_workspace_and_no_answer_from_the_controller_it_needs_a_person(self):
        self.w.machines["vault"].down = True
        r = self.w.wk("rm", "y", "--yes")
        self.assertEqual((r.code, r.effects), (4, []))
        self.assertIn("error: the controller, vault, did not answer (the fake is down)\n    so which server holds which workspace is unknown", r.err)

    def test_killed_after_any_effect_and_typed_again_it_converges(self):
        converges(self, make, "rm", "x", "--yes")

    def test_the_dry_run_is_the_plan(self):
        dry_run_is_the_plan(self, make, "rm", "x", "--yes")
