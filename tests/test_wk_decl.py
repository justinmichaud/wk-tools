"""lib/wk/decl.py and the argv arithmetic in lib/wk/dispatch.py."""
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import decl as D  # noqa: E402
from wk import dispatch  # noqa: E402


def declare(tmp, name, *lines):
    p = Path(tmp) / name
    p.write_text("#!/usr/bin/env bash\n# wk %s <x> -- a probe\n%s\n" % (name, "\n".join(lines)))
    p.chmod(0o755)
    return D.Decl(p)


class TestDeclarations(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(prefix="wk-test-decl-")
        self.addCleanup(t.cleanup)
        self.tmp = t.name

    def test_defaults_are_a_workspace_command_taking_no_name(self):
        d = declare(self.tmp, "probe", "# wk: group=other")
        self.assertEqual((d.where, d.name_decl, d.takes, d.group), ("workspace", "none", "0", "other"))
        self.assertFalse(d.is_readonly())
        self.assertFalse(d.is_destructive([]))

    def test_every_key_is_read(self):
        d = declare(self.tmp, "probe",
                    "# wk: where=host name=required@2 takes=1 ready=yes group=hosts lifecycle "
                    "readonly ls destructive rm,--purge dryrun opts --a,--b= passthrough=tail "
                    "broker * outside forward=no bare=merged post=zed values=--list needs gh,ssh")
        self.assertEqual(d.where, "host")
        self.assertEqual(d.name_decl, "required@2")
        self.assertTrue(d.ready and d.lifecycle and d.outside and not d.forward)
        self.assertTrue(d.is_readonly(["ls"]) and not d.is_readonly(["rm"]))
        self.assertTrue(d.is_destructive(["rm"]) and d.is_destructive(["--purge=x"]))
        self.assertFalse(d.is_destructive(["ls"]))
        self.assertTrue(d.honours_dryrun(["anything"]))
        self.assertFalse(d.nodryrun)
        self.assertTrue(declare(self.tmp, "probe", "# wk: where=host nodryrun").nodryrun)
        self.assertEqual(d.passthrough, "tail")
        self.assertEqual(d.broker, "*")
        self.assertEqual((d.bare, d.post, d.values, d.needs), ("merged", "zed", "--list", "gh,ssh"))

    def test_repos_names_the_repos_a_command_serves_and_no_other(self):
        self.assertTrue(declare(self.tmp, "probe", "# wk: group=other").serves("anything"))
        d = declare(self.tmp, "probe", "# wk: repos=webkit")
        self.assertTrue(d.serves("webkit"))
        self.assertFalse(d.serves("wk-tools"))
        self.assertIn("names no repo", self._refused("# wk: repos=webkit,nonesuch"))

    def _refused(self, *lines):
        with self.assertRaises(D.DeclError) as cm:
            declare(self.tmp, "probe", *lines)
        return str(cm.exception)

    def test_options_and_flags_of_a_command_with_verbs_belong_on_a_declared_verb(self):
        declare(self.tmp, "probe", "# wk: verbs=a,b", "# wk: sub a opts=--x")
        for lines, words in ((["# wk: verbs=a,b opts --x"], "opts belong on the verbs"),
                             (["# wk: verbs=a,b", "# wk: flag --list takes=0"], "no verb's"),
                             (["# wk: verbs=a,b", "# wk: sub c opts=--x"], "names no verb"),
                             (["# wk: opts --x", "# wk: sub a opts=--x"], "declares no verbs")):
            with self.subTest(lines=lines):
                self.assertIn(words, self._refused(*lines))

    def test_an_unknown_word_or_value_is_refused_by_name(self):
        self.assertIn("frobnicate", self._refused("# wk: where=host frobnicate"))
        self.assertIn("elsewhere", self._refused("# wk: where=elsewhere"))
        self.assertIn("maybe", self._refused("# wk: name=maybe"))

    def test_a_flag_override_beats_a_subverb_override_beats_the_default(self):
        d = declare(self.tmp, "probe",
                    "# wk: where=workspace name=required takes=1 opts --list",
                    "# wk: flag --list where=local name=none takes=0 opts=--list")
        self.assertEqual(d.where_for(["build"]), "workspace")
        self.assertEqual(d.where_for(["--list"]), "local")
        self.assertEqual(d.name_for(["--list=x"]), "none")
        d = declare(self.tmp, "probe", "# wk: where=workspace name=required takes=1 verbs=ls,run",
                    "# wk: sub ls where=store name=none takes=0")
        self.assertEqual(d.where_for(["run"]), "workspace")
        self.assertEqual(d.where_for(["ls"]), "store")
        self.assertEqual(d.takes_for(["ls"]), "0")

    def test_a_second_word_override_beats_its_verbs(self):
        d = declare(self.tmp, "probe", "# wk: where=host needs helper verbs=push,check readonly check",
                    "# wk: sub push needs=", "# wk: sub push on,off needs=helper", "# wk: sub push status readonly=yes")
        self.assertTrue(d.is_readonly(["push", "--all", "status"]) and d.is_readonly(["check"]))
        self.assertFalse(d.is_readonly(["push", "on"]) or d.is_readonly(["push"]))
        self.assertEqual([d.needs_for(a) for a in (["push", "on"], ["push", "status"], ["push"], ["check"])],
                         ["helper", "", "", "helper"])
        self.assertIn(("push on,off", {"needs": "helper"}), d.overrides())

    def test_a_subverb_overrides_destructive_and_dryrun(self):
        d = declare(self.tmp, "probe",
                    "# wk: where=host verbs=setup,inner destructive setup,--purge dryrun setup",
                    "# wk: sub inner destructive= dryrun=--list")
        self.assertTrue(d.is_destructive(["setup"]))
        self.assertFalse(d.is_destructive(["inner", "setup"]))
        self.assertTrue(d.honours_dryrun(["inner", "--list"]))
        self.assertFalse(d.honours_dryrun(["inner", "setup"]))

    def test_the_whole_leading_comment_block_declares(self):
        d = declare(self.tmp, "probe", *(["#"] * 30 + ["# wk: where=host"]))
        self.assertEqual(d.where, "host")

    def test_a_wk_line_past_the_leading_comment_block_does_not_declare(self):
        d = declare(self.tmp, "probe", "import os", "# wk: where=host")
        self.assertEqual(d.where, "workspace")

    def test_the_synopsis_is_the_wk_line(self):
        d = declare(self.tmp, "probe", "# wk: group=other")
        self.assertEqual(d.synopsis_line(), "probe <x>")
        self.assertEqual(d.summary(), "a probe")


class TestArgvArithmetic(unittest.TestCase):
    def test_name_slot(self):
        self.assertEqual(D.name_slot("none"), 0)
        self.assertEqual(D.name_slot("derived"), 0)
        self.assertEqual(D.name_slot("required"), 1)
        self.assertEqual(D.name_slot("optional@2"), 2)

    def test_positionals_ignore_options_and_their_joined_values(self):
        args = ["--on=vm", "ws", "--count=3", "cfg"]
        self.assertEqual(dispatch.positionals(args), ["ws", "cfg"])
        self.assertEqual(dispatch.positional(2, args), "cfg")
        self.assertIsNone(dispatch.positional(3, args))
        self.assertEqual(dispatch.without_positional(1, args), ["--on=vm", "--count=3", "cfg"])

    def test_argv_name_needs_every_declared_positional(self):
        self.assertIsNone(dispatch.argv_name(1, "1", ["ws"]))
        self.assertEqual(dispatch.argv_name(1, "1", ["ws", "cfg"]), "ws")
        self.assertEqual(dispatch.argv_name(1, "*", ["ws"]), "ws")

    def test_argv_split_hands_the_command_two_words_per_declared_option(self):
        out = dispatch.argv_split("--count=,--list", ["--count=3", "--list", "--other=x", "--", "--count=9"])
        self.assertEqual(out, ["--count", "3", "--list", "--other=x", "--", "--count=9"])

    def test_args_before_name(self):
        self.assertEqual(dispatch.args_before_name(2, ["claude", "ws", "-r"]), " claude")
        self.assertEqual(dispatch.args_before_name(1, ["ws"]), "")


class TestArgvCheck(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(prefix="wk-test-decl-")
        self.addCleanup(t.cleanup)
        self.tmp = t.name
        env = mock.patch.dict(os.environ, {"WK_MARKER": os.path.join(self.tmp, "no-marker")})
        env.start()
        self.addCleanup(env.stop)

    def _check(self, decl_lines, args):
        d = declare(self.tmp, "probe", *decl_lines)
        inv = dispatch.Invocation("probe", d, args)
        try:
            return inv.argv_check(), None
        except dispatch.Exit as e:
            return None, e.status

    def test_a_declared_option_passes_joined_to_its_value(self):
        out, rc = self._check(["# wk: name=required takes=1 opts --count=,--list"],
                              ["ws", "cfg", "--count", "3", "--list"])
        self.assertIsNone(rc)
        self.assertEqual(out, ["ws", "cfg", "--count=3", "--list"])

    def test_a_malformed_argv_is_refused_with_usage(self):
        for decl, args in (("takes=1 opts --list", ["ws", "cfg", "--bogus"]), ("opts --list", ["ws", "--list=yes"]),
                           ("opts --count=", ["ws", "--count"]), ("takes=1", ["ws", "cfg", "extra"])):
            with self.subTest(args=args):
                self.assertEqual(self._check(["# wk: name=required " + decl], args), (None, 2))

    def test_takes_star_takes_everything(self):
        out, rc = self._check(["# wk: name=required takes=*"], ["ws", "a", "b", "c"])
        self.assertEqual(out, ["ws", "a", "b", "c"])

    def test_a_tail_passthrough_stops_checking_at_the_last_positional(self):
        out, rc = self._check(["# wk: name=required takes=1 passthrough=tail"],
                              ["ws", "script.js", "--not-declared", "x"])
        self.assertEqual(out, ["ws", "script.js", "--not-declared", "x"])

    def test_double_dash_needs_a_passthrough_declaration(self):
        out, rc = self._check(["# wk: name=required"], ["ws", "--", "x"])
        self.assertEqual(rc, 2)
        out, rc = self._check(["# wk: name=required passthrough"], ["ws", "--", "-e", "x"])
        self.assertEqual(out, ["ws", "--", "-e", "x"])

    def test_inside_a_workspace_the_name_slot_is_implicit(self):
        Path(os.environ["WK_MARKER"]).write_text("name=here\n")
        out, rc = self._check(["# wk: name=required takes=1"], ["cfg"])
        self.assertEqual(out, ["cfg"])
        out, rc = self._check(["# wk: name=required takes=1"], ["cfg", "extra"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
