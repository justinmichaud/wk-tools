import importlib
import os
import sys
import tempfile
import unittest

from tests import ROOT
from tests.world import World
from wk.core import hooks
from wk.core.fakes import Fake
from wk.core.fleet import Fleet
from wk.core.inv import SEAMS, Inv, lane
from wk.core.machine import Local, Result


class TestHelp(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def test_help_lists_every_command_with_its_usage_and_summary_and_the_topics(self):
        out = self.w.wk("help").out
        self.assertIn("wk help [<topic>]                            List the commands, or print a section of the design.\n", out)
        listed = out.split("\n\n")[0].splitlines()
        self.assertEqual(len(listed), len(self.w.commands))
        for key, line in zip(sorted(self.w.commands), listed):
            summary = importlib.import_module(self.w.commands[key]).__doc__.split("\n")[0]
            self.assertTrue(line.startswith("wk %s" % " ".join(key)) and line.endswith(" " + summary) and summary.rstrip(".:`"), line)
        self.assertIn("\ntopics (wk help <topic>): design, status, decisions, vocabulary, shape, rules every command obeys, seams,", out)

    def test_a_topic_prints_the_section_whose_heading_starts_with_it(self):
        out = self.w.wk("help", "Rules").out
        self.assertTrue(out.startswith("## Rules every command obeys\n"))
        self.assertNotIn("## Seams", out)

    def test_design_prints_all_of_it(self):
        with open(os.path.join(ROOT, "DESIGN.md")) as f:
            self.assertEqual(self.w.wk("help", "design").out, f.read().rstrip() + "\n")

    def test_status_prints_what_is_implemented_tested_and_verified(self):
        with open(os.path.join(ROOT, "STATUS.md")) as f:
            self.assertEqual(self.w.wk("help", "status").out, f.read().rstrip() + "\n")

    def test_an_unknown_topic_is_refused_naming_the_topics(self):
        r = self.w.wk("help", "bogus")
        self.assertEqual(r.code, 1)
        self.assertIn("error: no topic starts with bogus: the topics are design, status, decisions,", r.err)


class TestSelftest(unittest.TestCase):
    def test_it_runs_the_suite_isolated_from_the_checkout_and_returns_its_status(self):
        w = World()
        w.machines["desk"].on((sys.executable,), Result(1))
        r = w.wk("selftest")
        self.assertEqual((r.code, r.calls), (1, ["[desk] %s -I -m unittest discover -t %s -s %s/tests" % (sys.executable, ROOT, ROOT)]))

    def test_a_pattern_selects_tests(self):
        w = World()
        w.machines["desk"].on((sys.executable,), "")
        self.assertTrue(w.wk("selftest", "conf").calls[0].endswith("/tests -k conf"))


class TestMachineLs(unittest.TestCase):
    def test_it_lists_each_machine_with_its_kind(self):
        r = World(here="vault").wk("machine", "ls")
        self.assertEqual(r.out.splitlines()[:2] + r.out.splitlines()[-1:],
                         ["box                  mac", "desk                 workstation", "vault                controller"])

    def test_machine_alone_is_machine_ls(self):
        w = World(here="vault")
        self.assertEqual(w.wk("machine").out, w.wk("machine", "ls").out)


class TestLauncher(unittest.TestCase):
    def run_wk(self, *words, **env):
        return Local().run([sys.executable, os.path.join(ROOT, "wk")] + list(words), env=env)

    def test_it_runs_isolated_so_a_directory_named_wk_cannot_shadow_the_package(self):
        with tempfile.TemporaryDirectory() as decoy:
            os.mkdir(os.path.join(decoy, "wk"))
            with open(os.path.join(decoy, "wk", "__init__.py"), "w") as f:
                f.write("raise SystemExit('the decoy was imported')\n")
            r = self.run_wk("help", "seams", PYTHONPATH=decoy)
        self.assertEqual((r.rc, r.err, r.out.splitlines()[0]), (0, "", "## Seams"))

    def test_a_symlink_to_it_finds_the_checkout_and_the_machines_directory_is_overridable(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.symlink(os.path.join(ROOT, "wk"), os.path.join(tmp, "wk"))
            r = Local().run([os.path.join(tmp, "wk"), "machine", "ls"], env={"WK_MACHINES_DIR": os.path.join(tmp, "absent")})
        self.assertEqual(r.rc, 1)
        self.assertIn("error: no machines directory at %s" % os.path.join(tmp, "absent"), r.err)


class TestInv(unittest.TestCase):
    def test_a_lanes_factory_is_imported_when_first_asked_for(self):
        self.assertEqual(lane("tests.lanes", "factory")(1, 2), ("made", (1, 2)))

    def test_the_seams_are_made_by_the_three_lanes(self):
        self.assertEqual(sorted(SEAMS), ["driver", "secrets", "tasks"])

    def test_inv_hands_itself_to_each_factory(self):
        inv = Inv("/", {}, None, None, None, {}, {k: lambda *args, k=k: (k,) + args for k in SEAMS})
        self.assertEqual((inv.driver("plain", "m"), inv.tasks("m"), inv.secrets()), (("driver", inv, "plain", "m"), ("tasks", inv, "m"), ("secrets", inv)))

    def test_wk_home_is_under_the_home_unless_the_environment_says(self):
        fleet = Fleet(dict, "srv", None, Fake("srv"))
        self.assertEqual([Inv("/", env, fleet, None, None, {}).wk_home for env in ({}, {"WK_HOME": "/srv/wk"})], ["/home/u/wk", "/srv/wk"])


class TestHooks(unittest.TestCase):
    def test_doctor_rows_are_collected_from_every_lane_that_has_hooks_in_lane_order(self):
        rows = hooks.collect("doctor", None, "srv", None, package="tests.lanes")
        self.assertEqual(rows, [hooks.DoctorRow("alpha", hooks.OK), hooks.DoctorRow("gamma", hooks.UNKNOWN, "the probe died", "wk machine setup srv")])
        self.assertEqual(hooks.MISSING, "missing")

    def test_a_lane_without_a_hook_yields_no_row(self):
        rows = hooks.collect("gc", None, "srv", package="tests.lanes")
        self.assertEqual([(r.name, r.detail, r.flag, r.reclaim()) for r in rows], [("alpha-cache", "3 files", "", None)])

    def test_the_real_tree_is_searched_by_default(self):
        self.assertEqual(hooks.collect("a_hook_no_lane_defines"), [])
