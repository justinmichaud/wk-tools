import unittest

from tests.killpoints import converges, dry_run_is_the_plan
from tests.provision import HOME, lanes, server
from tests.world import World
from wk.cmd import gc
from wk.core.machine import Result

SMALL = {"desk": "kind=client", "lin": "kind=linux\nrole=credential", "mac": "kind=mac"}
LISTING = "lin      ccache                       9 GB    (--deep takes it)\nlin      tmp                          2 MB\n"


def make():
    w = World(confs=SMALL)
    w.machines["lin"].files.update({"/junk/tmp": "2 MB|", "/junk/ccache": "9 GB|--deep"})
    w.machines["mac"].files.update({"/junk/old": "1 MB|"})
    return w


class TestGc(unittest.TestCase):
    def setUp(self):
        lanes(self, "tests.provision.toy")
        self.w = make()
        self.lin = self.w.machines["lin"]

    def test_it_lists_each_row_with_its_size_asks_once_and_reclaims_those_that_need_no_flag(self):
        r = self.w.wk("gc", on="lin", stdin="y\n", tty=True)
        self.assertEqual((r.code, r.out, r.err), (0, LISTING, "reclaim the 1 rows not marked with a flag? [y/N] "))
        self.assertEqual((r.effects, sorted(self.lin.files)), (["[lin] remove /junk/tmp"], ["/junk/ccache"]))

    def test_a_row_that_needs_a_flag_is_taken_when_the_flag_is_given(self):
        r = self.w.wk("gc", "--deep", "--yes", on="lin")
        self.assertEqual((r.out, r.effects), (LISTING.replace("    (--deep takes it)", ""), ["[lin] remove /junk/ccache", "[lin] remove /junk/tmp"]))

    def test_declined_it_changes_nothing(self):
        r = self.w.wk("gc", on="lin", stdin="n\n", tty=True)
        self.assertEqual((r.code, r.effects), (1, []))
        self.assertIn("error: declined: nothing is changed", r.err)

    def test_without_a_terminal_it_does_not_ask_and_names_yes(self):
        r = self.w.wk("gc", on="lin")
        self.assertEqual((r.code, r.effects), (4, []))
        self.assertIn("pass --yes", r.err)

    def test_with_nothing_to_reclaim_it_says_so_and_asks_nothing(self):
        del self.lin.files["/junk/tmp"]
        r = self.w.wk("gc", on="lin")
        self.assertEqual((r.code, r.out, r.err), (0, LISTING.splitlines()[0] + "\nnothing to reclaim\n", ""))

    def test_from_the_client_every_server_is_listed_in_turn_and_one_question_covers_them(self):
        r = self.w.wk("gc", stdin="y\n", tty=True)
        self.assertEqual((r.out, r.err), (LISTING + "mac      old                          1 MB\n", "reclaim the 2 rows not marked with a flag? [y/N] "))
        self.assertEqual(r.effects, ["[lin] remove /junk/tmp", "[mac] remove /junk/old"])

    def test_a_listing_that_failed_is_named_and_the_rest_is_still_reclaimed(self):
        self.lin.files["/junk-unlisted"] = "1"
        r = self.w.wk("gc", "--yes")
        self.assertEqual((r.code, r.out, r.effects), (1, "lin      not listed: du died in /junk\nmac      old                          1 MB\n", ["[mac] remove /junk/old"]))

    def test_a_server_that_does_not_answer_is_named_and_needs_a_person(self):
        self.w.machines["mac"].down = True
        r = self.w.wk("gc", "--yes")
        self.assertEqual((r.code, r.out.splitlines()[-1], r.effects), (4, "mac      not listed: cannot reach mac: the fake is down", ["[lin] remove /junk/tmp"]))

    def test_a_dry_run_lists_asks_nothing_and_removes_nothing(self):
        r = self.w.wk("gc", "--dry-run", on="lin")
        self.assertEqual((r.code, r.out, r.plan, r.effects), (0, LISTING, ["[lin] remove /junk/tmp"], []))
        self.assertIn("would ask: reclaim the 1 rows not marked with a flag? [y/N]\n", r.err)

    def test_the_dry_run_is_the_plan_and_a_killed_run_converges(self):
        dry_run_is_the_plan(self, make, "gc", "--deep", "--yes")
        converges(self, make, "gc", "--deep", "--yes")

    def test_the_flags_are_the_lanes_and_any_other_is_refused_with_the_usage(self):
        r = self.w.wk("gc", "--everything")
        self.assertEqual((r.code, r.err), (1, "error: unknown option: --everything\nusage: wk gc [--deep]\n"))
        self.assertIn("  --deep  take what costs a rebuild\n", self.w.wk("gc", "-h").out)

    def test_the_module_has_no_other_made_up_attribute(self):
        self.assertRaises(AttributeError, getattr, gc, "TABLE")


class TestStaleDownloads(unittest.TestCase):
    def setUp(self):
        lanes(self, "tests.provision.own")
        self.w = World(confs=SMALL)
        self.lin = self.w.machines["lin"]
        server(self.lin)
        self.lin.files.update({HOME + "/cache/pueue-4.0.0": "xxxx", HOME + "/cache/pueue-3.4.1": "xxx"})

    def test_a_download_no_lane_pins_is_listed_with_its_size_and_reclaimed_and_a_pinned_one_is_kept(self):
        r = self.w.wk("gc", "--yes", on="lin")
        self.assertEqual((r.code, r.out), (0, "lin      cache/pueue-3.4.1            3 MB\n"))
        self.assertEqual((r.effects, self.lin.listdir(HOME + "/cache")), (["[lin] remove %s/cache/pueue-3.4.1" % HOME], ["pueue-4.0.0"]))

    def test_a_download_that_cannot_be_sized_fails_the_listing(self):
        self.lin.on(("du", "-sk"), Result(1, "", "du: Permission denied\n"))
        r = self.w.wk("gc", "--yes", on="lin")
        self.assertEqual((r.code, r.out, r.effects), (1, "lin      not listed: cannot size %s/cache/pueue-3.4.1 on lin: du: Permission denied\n"
                                                         "nothing to reclaim\n" % HOME, []))
