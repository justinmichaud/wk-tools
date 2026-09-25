"""`--ab-systems`'s boot per leg (lib/wk/bench/board_ab.py's AB.boot): a leg runs on the system it names -- proven from
the marker the running system serves, never the arming record -- or it does not run. Against the fake board of
tests/test_bench_board.py holding two systems on its stick, driven through the real rpi5 driver and `wk boot`'s own
arming (lib/wk/boot/cli.py's Boot), with the fake clock under every wait. The whole A/B on it is
tests/test_bench_board.py's TestTwoSystemsOnFake.

Run: python3 tests/run.py -k tests.test_pi_ab_systems
"""
import contextlib
import io
import os
import sys
import unittest

from tests.support import REPO
from tests.test_bench_board import BOARD, BoardTest, PipelineReg

sys.path.insert(0, str(REPO / "lib"))
from wk.bench import board_ab  # noqa: E402
from wk.boot.driver import part  # noqa: E402
from wk.boot.pi import Rpi5Usb  # noqa: E402
from wk.machine import Result  # noqa: E402

SYS_A, SYS_B = part("/dev/sda", 2), part("/dev/sda", 4)


class ArmsFromBench(Rpi5Usb):
    """A board whose arming is taken where it stands, as pi-sd's is."""
    arm_from_bench = True


class SystemBootTest(BoardTest):
    def ab(self, driver=Rpi5Usb, running=SYS_A):
        w = self.world()
        w.board.write_system(part("/dev/sda", 3), "sys-b")
        w.board.running = running
        self.landed = []
        reboot = w.board.reboot

        def logged(tryboot=False):
            r = reboot(tryboot)
            if self.misroute and w.board.running == SYS_B:
                self.misroute -= 1
                w.board.running = SYS_A
            self.landed.append(w.board.running)
            return r
        w.board.reboot, self.misroute = logged, 0
        d = driver(REPO, w.board.conf, w.board)
        self.w = w
        with w.patches():
            return board_ab.AB(str(REPO), PipelineReg(w), "ws", "jetstream3", {"system": BOARD, "ab_systems": "sys-a,sys-b"},
                               w.clock, w.popen, driver=d)

    def boot(self, ab, want):
        err = io.StringIO()
        with self.w.patches(), contextlib.redirect_stderr(err):
            ok = ab.boot(want)
        self.err = err.getvalue()
        return ok

    def armings(self):
        return sum(1 for e in self.w.board.effects if e[:2] == ("card_priv", "autoboot"))


class TestTheLegsSystem(SystemBootTest):
    def test_a_board_already_on_the_system_boots_nothing(self):
        ab = self.ab()
        self.assertTrue(self.boot(ab, "sys-a"), self.err)
        self.assertEqual(self.landed, [])

    def test_a_board_that_arms_where_it_stands_is_armed_there(self):
        """One boot, and no trip through the rescue."""
        ab = self.ab(driver=ArmsFromBench)
        self.assertTrue(self.boot(ab, "sys-b"), self.err)
        self.assertEqual(self.landed, [SYS_B])

    def test_a_board_that_arms_only_from_its_rescue_goes_back_to_it_first(self):
        ab = self.ab()
        self.assertTrue(self.boot(ab, "sys-b"), self.err)
        self.assertEqual(self.landed, [self.w.board.conf["NODE_ROOT"], SYS_B])
        self.assertEqual(self.armings(), 1)

    def test_the_rescue_is_armed_from_directly(self):
        ab = self.ab(running="/dev/mmcblk0p2")
        self.assertTrue(self.boot(ab, "sys-b"), self.err)
        self.assertEqual(self.landed, [SYS_B])

    def test_a_board_that_comes_up_wrong_is_armed_again(self):
        ab = self.ab(driver=ArmsFromBench)
        self.misroute = 2
        self.assertTrue(self.boot(ab, "sys-b"), self.err)
        self.assertEqual(self.landed, [SYS_A, SYS_A, SYS_B])

    def test_the_last_arming_is_read_before_the_leg_is_given_up(self):
        """The passes are one more than the armings: a board that comes up right on the final arming has won its leg."""
        ab = self.ab(driver=ArmsFromBench)
        self.misroute = board_ab.SYSTEM_TRIES - 1
        self.assertTrue(self.boot(ab, "sys-b"), self.err)
        self.assertEqual(self.armings(), board_ab.SYSTEM_TRIES)

    def test_a_board_that_never_lands_loses_the_leg_after_the_last_try(self):
        ab = self.ab(driver=ArmsFromBench)
        self.misroute = 99
        self.assertFalse(self.boot(ab, "sys-b"))
        self.assertIn("the leg is lost", self.err)
        self.assertEqual(self.armings(), board_ab.SYSTEM_TRIES)

    def test_an_arming_that_will_not_take_is_retried_after_a_pause_and_bounded(self):
        ab = self.ab(driver=ArmsFromBench)
        self.w.board.stuck = True
        self.assertFalse(self.boot(ab, "sys-b"))
        self.assertEqual(self.w.clock.slept.count(board_ab.ARM_RETRY), board_ab.SYSTEM_TRIES)
        self.assertEqual(self.landed, [])

    def test_a_board_between_systems_is_waited_for_not_counted(self):
        ab = self.ab()
        back = self.w.clock.now() + 100
        answer = self.w.board.answer
        self.w.board.answer = lambda side, argv, input=None: (answer(side, argv, input) if self.w.clock.now() >= back
                                                              else Result(255, "", "ssh: connect: no route"))
        self.assertTrue(self.boot(ab, "sys-a"), self.err)
        self.assertIn("not answering yet", self.err)
        self.assertEqual((self.landed, self.armings()), ([], 0))
        self.assertIn(board_ab.POLL, self.w.clock.slept)

    def test_a_leg_that_never_comes_up_is_never_claimed(self):
        ab = self.ab(driver=ArmsFromBench)
        self.misroute = 99
        self.boot(ab, "sys-b")
        self.assertFalse(self.w.board.kept)

    def test_a_dry_run_arms_nothing(self):
        for driver, said in ((ArmsFromBench, "would arm testboard"), (Rpi5Usb, "would reboot testboard back")):
            with self.subTest(driver=driver.__name__):
                ab = self.ab(driver=driver)
                os.environ["WK_DRY_RUN"] = "1"
                try:
                    self.assertFalse(self.boot(ab, "sys-b"))
                finally:
                    del os.environ["WK_DRY_RUN"]
                self.assertIn(said, self.err)
                self.assertEqual((self.landed, self.armings()), ([], 0))


class TestTheWidth(unittest.TestCase):
    """A system id is <profile>-<hash>, and the width is the profile's."""

    def test_a_system_id_reads_its_profiles_width(self):
        self.assertEqual(board_ab.width("webkit-2.52-yocto-rpi3-32-ebb646f3bf67", os.environ), 32)
        self.assertEqual(board_ab.width("webkit-2.52-yocto-rpi5-64-cddf63dc0d4b", os.environ), 64)

    def test_a_profile_name_is_its_own_width(self):
        self.assertEqual(board_ab.width("webkit-2.52-yocto-rpi4-32", os.environ), 32)

    def test_the_narrow_width_selects_the_exclusions_and_the_wide_one_none(self):
        self.assertIn("argon2-wasm", [n for n, _ in board_ab.exclusions(str(REPO), "jetstream3", 32)])
        self.assertEqual(board_ab.exclusions(str(REPO), "jetstream3", 64), [])

    def test_every_exclusion_names_a_width_and_a_reason(self):
        for line in (REPO / board_ab.EXCLUSIONS).read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                plan, bits, name, why = line.split(None, 3)
                self.assertIn(bits, ("32", "64"), name)
                self.assertTrue(why.strip(), name)


class TestTheKeptSubtests(unittest.TestCase):
    PLAN = {"subtests": {"": ["a-wasm", "argon2-wasm", "b", "c"]}}

    def kept(self, drop):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                return board_ab.kept(self.PLAN, drop)
            except board_ab.Refused:
                return err.getvalue()

    def test_the_kept_set_is_the_plan_minus_the_exclusions(self):
        self.assertEqual(self.kept(["argon2-wasm"]), ["a-wasm", "b", "c"])

    def test_a_name_the_plan_does_not_have_refuses(self):
        self.assertIn("names no subtest", self.kept(["nope"]))

    def test_excluding_everything_refuses(self):
        self.assertIn("every subtest", self.kept(["a-wasm", "argon2-wasm", "b", "c"]))


if __name__ == "__main__":
    unittest.main()
