import unittest

from tests import toy
from tests.killpoints import converges, dry_run_is_the_plan
from tests.world import World


def make():
    return World(commands=[toy])


class TestConverges(unittest.TestCase):
    def test_a_command_that_redoes_every_effect_converges(self):
        converges(self, make, "poke")

    def test_a_command_that_trusts_a_half_made_thing_fails_naming_the_kill(self):
        with self.assertRaisesRegex(AssertionError, "killed after effect 1"):
            converges(self, make, "trust")

    def test_a_command_that_fails_uninterrupted_fails_the_check(self):
        with self.assertRaisesRegex(AssertionError, "the uninterrupted run fails"):
            converges(self, make, "misc", "--die", "1")

    def test_a_command_with_more_effects_than_the_limit_fails(self):
        with self.assertRaisesRegex(AssertionError, "more than 3 effects"):
            converges(self, make, "flood", limit=3)


class TestDryRunIsThePlan(unittest.TestCase):
    def test_a_dry_run_lists_exactly_the_wet_runs_effects(self):
        dry_run_is_the_plan(self, make, "poke")

    def test_a_destructive_command_is_checked_with_yes(self):
        def precious():
            w = make()
            w.machines["desk"].files["/precious"] = "x"
            return w
        dry_run_is_the_plan(self, precious, "wipe", "--yes")
        converges(self, precious, "wipe", "--yes")

    def test_a_command_with_no_effect_fails_the_check(self):
        with self.assertRaisesRegex(AssertionError, "the wet run changes nothing"):
            dry_run_is_the_plan(self, make, "desk")
