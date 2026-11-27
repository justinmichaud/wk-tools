import json
import unittest

from tests import toy
from tests.world import World
from wk.core.errors import Bug


class Typed(unittest.TestCase):
    def setUp(self):
        self.w = World(commands=[toy])

    def args(self, *words):
        r = self.w.wk(*words)
        self.assertEqual((r.code, r.err), (0, ""))
        return json.loads(r.out)


class TestParsing(Typed):
    def test_positionals_options_and_the_tail_reach_main_as_declared(self):
        self.assertEqual(self.args("echo", "a", "--loud", "b", "--to-path", "/p", "--", "-x", "--yes"),
                         {"first": "a", "second": "b", "loud": True, "to_path": "/p", "tail": ["-x", "--yes"]})

    def test_what_is_left_out_is_none_false_or_empty(self):
        self.assertEqual(self.args("echo", "a"), {"first": "a", "second": None, "loud": False, "to_path": None, "tail": []})

    def refused(self, why, *words, usage="usage: wk echo [--loud] [--to-path <path>] <first> [<second>] [-- <args>]"):
        r = self.w.wk(*words)
        self.assertEqual((r.code, r.out, r.err), (1, "", "error: %s\n%s\n" % (why, usage)))

    def test_an_unknown_command_is_refused_with_the_usage_line(self):
        self.refused("unknown command: bogus", "bogus", usage="usage: wk <command> [<args>]    (`wk help` lists the commands)")

    def test_an_undeclared_option_is_refused(self):
        self.refused("unknown option: --bogus", "echo", "a", "--bogus")

    def test_a_missing_positional_is_refused(self):
        self.refused("missing <first>", "echo")

    def test_an_extra_positional_is_refused(self):
        self.refused("unexpected argument: c", "echo", "a", "b", "c")

    def test_an_option_without_its_value_is_refused(self):
        self.refused("--to-path needs a value: --to-path <path>", "echo", "a", "--to-path")

    def test_a_tail_is_refused_where_none_is_declared(self):
        self.refused("nothing follows `--` here", "desk", "--", "x", usage="usage: wk desk")

    def test_help_prints_the_usage_the_docstring_and_the_options_and_runs_nothing(self):
        r = self.w.wk("echo", "-h")
        self.assertEqual(r.code, 0)
        self.assertTrue(r.out.startswith("usage: wk echo [--loud] [--to-path <path>] <first> [<second>] [-- <args>]\n\n"
                                         "Print the arguments as parsed.\n\nThe second paragraph.\n\n"
                                         "options:\n  --loud     say it loudly\n  --to-path  where to\n\nglobal flags:\n"), r.out)

    def test_a_bare_wk_lists_the_commands(self):
        self.assertIn("wk machine ls", self.w.wk().out)


class TestVerbs(Typed):
    def test_a_verb_is_a_module_named_group_verb(self):
        self.assertEqual(self.w.wk("grp", "two").out, "two\n")

    def test_a_bare_group_runs_its_default_verb(self):
        self.assertEqual(self.w.wk("grp").out, "one\n")

    def test_the_default_verb_takes_its_options_bare(self):
        self.assertEqual(self.w.wk("grp", "--twice").out, "oneone\n")

    def test_an_unknown_verb_is_refused_naming_the_verbs(self):
        self.assertIn("error: wk grp needs one of: one, two\n", self.w.wk("grp", "bogus").err)

    def test_a_group_with_no_default_refuses_to_run_bare(self):
        self.assertIn("error: wk lone needs one of: verb\n", self.w.wk("lone").err)


class TestGlobalFlags(Typed):
    def test_dry_run_prints_each_effect_and_makes_none(self):
        r = self.w.wk("poke", "--dry-run")
        self.assertEqual((r.code, r.effects, self.w.machines["desk"].files), (0, [], {}))
        self.assertEqual(r.plan, ["[desk] write /poke/a", "[desk] touch /poke", "[desk] write /poke/b"])

    def test_n_before_the_command_is_dry_run(self):
        self.assertEqual(len(self.w.wk("-n", "poke").plan), 3)

    def test_a_wet_run_records_its_effects_in_order(self):
        r = self.w.wk("poke")
        self.assertEqual(r.effects, ["[desk] write /poke/a", "[desk] touch /poke", "[desk] write /poke/b"])
        self.assertEqual(self.w.machines["desk"].files, {"/poke/a": "1", "/poke/b": "1"})

    def test_quiet_keeps_warnings_and_drops_progress(self):
        self.assertEqual(self.w.wk("misc").err, "==> starting\nwarning: careful\n")
        self.assertEqual(self.w.wk("misc", "--quiet").err, "warning: careful\n")

    def test_a_global_flag_after_the_separator_belongs_to_the_tail(self):
        self.assertEqual(self.w.wk("echo", "a", "--", "-n").plan, [])


class TestExitCodes(Typed):
    def test_main_returning_nothing_is_zero_and_a_number_is_the_code(self):
        self.assertEqual((self.w.wk("misc").code, self.w.wk("misc", "--code", "3").code), (0, 3))

    def test_a_refusal_prints_its_reason_and_exits_with_its_status(self):
        for status in (1, 2, 3, 4, 75):
            r = self.w.wk("misc", "--die", str(status))
            self.assertEqual((r.code, r.err.splitlines()[-1]), (status, "error: it is busy: wait for it"))

    def test_an_interrupt_is_130(self):
        self.assertEqual(self.w.wk("misc", "--interrupt").code, 130)


class TestConfirm(Typed):
    def setUp(self):
        super().setUp()
        self.w.machines["desk"].files["/precious"] = "x"

    def test_without_a_terminal_it_declines_and_names_the_remedy(self):
        r = self.w.wk("wipe")
        self.assertEqual((r.code, r.effects), (4, []))
        self.assertIn("re-run in a terminal, or pass --yes", r.err)

    def test_a_yes_at_the_terminal_proceeds(self):
        r = self.w.wk("wipe", stdin="y\n", tty=True)
        self.assertEqual((r.code, r.effects, r.err), (0, ["[desk] remove /precious"], "remove /precious? [y/N] "))

    def test_anything_else_at_the_terminal_declines(self):
        for answer in ("\n", "n\n", ""):
            r = self.w.wk("wipe", stdin=answer, tty=True)
            self.assertEqual((r.code, r.effects), (1, []))
            self.assertIn("error: declined: nothing is changed", r.err)

    def test_yes_answers_it(self):
        self.assertEqual(self.w.wk("wipe", "-y").effects, ["[desk] remove /precious"])

    def test_a_dry_run_says_what_it_would_ask(self):
        r = self.w.wk("wipe", "--dry-run")
        self.assertEqual(r.err, "would ask: remove /precious? [y/N]\nwould: [desk] remove /precious\n")

    def test_a_destructive_command_that_acts_before_asking_is_a_bug(self):
        with self.assertRaisesRegex(Bug, "acts before it asks"):
            self.w.wk("wipe", "--early")
        self.assertIn("/precious", self.w.machines["desk"].files)

    def test_a_readonly_command_that_acts_is_a_bug(self):
        with self.assertRaisesRegex(Bug, "readonly command changes state"):
            self.w.wk("peek")


class TestBarrier(Typed):
    def test_a_barrier_refuses_and_names_force(self):
        r = self.w.wk("misc", "--barrier")
        self.assertEqual(r.code, 1)
        self.assertIn("error: the rule says no\n    --force crosses this\n", r.err)

    def test_a_barrier_that_lifts_is_refused_for_now(self):
        self.assertEqual(self.w.wk("misc", "--retry").code, 75)

    def test_force_crosses_it_and_says_so_again_at_exit(self):
        r = self.w.wk("misc", "--barrier", "--force")
        self.assertEqual(r.code, 0)
        self.assertEqual(r.err.splitlines()[-2:], ["warning: forced past: the rule says no",
                                                   "warning: this command was forced past: the rule says no"])


class TestWhere(Typed):
    def test_a_command_runs_on_a_kind_it_declares(self):
        self.assertEqual(self.w.wk("desk").out, "at the desk\n")

    def test_on_another_kind_it_is_refused_naming_where_it_runs_and_what_this_machine_is(self):
        r = self.w.wk("desk", on="lin")
        self.assertEqual((r.code, r.err, r.calls), (1, "error: wk desk runs on a workstation (desk), and this is lin, a Linux server: type it there\n", []))

    def test_a_server_command_runs_on_a_server_and_asks_no_one(self):
        r = self.w.wk("far", "x", "--", "-j", "4", on="mac")
        self.assertEqual((r.code, r.out, r.calls), (0, "ran on mac: x ['-j', '4']\n", ["[mac] write /far/x"]))

    def test_a_server_command_is_refused_on_the_workstation_and_on_the_controller_naming_the_servers(self):
        for name, what in (("desk", "a workstation"), ("vault", "the controller")):
            r = self.w.wk("far", "x", on=name)
            self.assertEqual((r.code, r.err, r.calls), (1, "error: wk far runs on a Linux server or a Mac server (box, lin, mac), and this is %s, %s: "
                                                           "type it there\n" % (name, what), []))

    def test_a_kind_no_machine_has_is_said_to_be_none(self):
        w = World(commands=[toy], confs={"lin": "kind=linux"})
        self.assertEqual(w.wk("desk", on="lin").err,
                         "error: wk desk runs on a workstation (machines/ has none), and this is lin, a Linux server: type it there\n")

    def test_a_command_that_runs_anywhere_needs_no_conf_for_this_machine(self):
        self.assertEqual(self.w.wk("grp", on="stranger").out, "one\n")

    def test_a_machine_with_no_conf_is_told_to_add_one(self):
        r = self.w.wk("desk", on="stranger")
        self.assertEqual((r.code, r.err), (1, "error: this machine, stranger, has no conf: add machines/stranger.conf with its kind=\n"))
