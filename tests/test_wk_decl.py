"""lib/wk/decl.py and the argv arithmetic in lib/wk/dispatch.py, driven in
process: a declaration is data, and every answer it gives is a pure function
of the header and the arguments.

Run: python3 tests/run.py -k tests.test_wk_decl
"""
import os
import sys
import tempfile
import unittest
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
        self.tmp = tempfile.mkdtemp(prefix="wk-test-decl-")

    def tearDown(self):
        for p in Path(self.tmp).iterdir():
            p.unlink()
        os.rmdir(self.tmp)

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
        self.assertTrue(d.is_readonly("ls") and not d.is_readonly("rm"))
        self.assertTrue(d.is_destructive(["rm"]) and d.is_destructive(["--purge=x"]))
        self.assertFalse(d.is_destructive(["ls"]))
        self.assertTrue(d.honours_dryrun(["anything"]))
        self.assertEqual(d.passthrough, "tail")
        self.assertEqual(d.broker, "*")
        self.assertEqual((d.bare, d.post, d.values, d.needs), ("merged", "zed", "--list", "gh,ssh"))

    def test_an_unknown_word_is_refused_by_name(self):
        with self.assertRaises(D.DeclError) as cm:
            declare(self.tmp, "probe", "# wk: where=host frobnicate")
        self.assertIn("frobnicate", str(cm.exception))

    def test_where_and_name_are_closed_vocabularies(self):
        with self.assertRaises(D.DeclError):
            declare(self.tmp, "probe", "# wk: where=elsewhere")
        with self.assertRaises(D.DeclError):
            declare(self.tmp, "probe", "# wk: name=maybe")

    def test_a_flag_override_beats_a_subverb_override_beats_the_default(self):
        d = declare(self.tmp, "probe",
                    "# wk: where=workspace name=required takes=1 opts --list",
                    "# wk: sub ls where=store name=none takes=0",
                    "# wk: flag --list where=local name=none takes=0 opts=--list")
        self.assertEqual(d.where_for(["build"]), "workspace")
        self.assertEqual(d.where_for(["ls"]), "store")
        self.assertEqual(d.where_for(["ls", "--list"]), "local")
        self.assertEqual(d.takes_for(["ls"]), "0")
        self.assertEqual(d.name_for(["--list=x"]), "none")

    def test_only_the_first_fifteen_lines_declare(self):
        d = declare(self.tmp, "probe", *(["#"] * 14 + ["# wk: where=host"]))
        self.assertEqual(d.where, "workspace")

    def test_the_synopsis_is_the_wk_line(self):
        d = declare(self.tmp, "probe", "# wk: group=other")
        self.assertEqual(d.synopsis_line(), "probe <x>")
        self.assertEqual(d.summary(), "a probe")

    def test_every_real_command_declares_cleanly(self):
        decls = list(D.all_commands(REPO))
        self.assertGreater(len(decls), 30)
        for d in decls:
            self.assertIn(d.where, D.WHERE_VALUES, d.name)
            self.assertIn(d.name_decl.split("@")[0], D.NAME_VALUES, d.name)
            self.assertTrue(d.synopsis, "%s has no `# wk` synopsis line" % d.name)


class TestArgvArithmetic(unittest.TestCase):
    def test_name_slot(self):
        self.assertEqual(D.name_slot("none"), 0)
        self.assertEqual(D.name_slot("derived"), 0)
        self.assertEqual(D.name_slot("required"), 1)
        self.assertEqual(D.name_slot("optional@2"), 2)

    def test_positionals_ignore_options_and_their_joined_values(self):
        args = ["--target=vm", "ws", "--count=3", "cfg"]
        self.assertEqual(dispatch.positionals(args), ["ws", "cfg"])
        self.assertEqual(dispatch.positional(2, args), "cfg")
        self.assertIsNone(dispatch.positional(3, args))
        self.assertEqual(dispatch.without_positional(1, args), ["--target=vm", "--count=3", "cfg"])

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
        self.tmp = tempfile.mkdtemp(prefix="wk-test-decl-")
        self.marker = os.environ.get("WK_MARKER")
        os.environ["WK_MARKER"] = os.path.join(self.tmp, "no-marker")

    def tearDown(self):
        if self.marker is None:
            os.environ.pop("WK_MARKER", None)
        else:
            os.environ["WK_MARKER"] = self.marker
        for p in Path(self.tmp).iterdir():
            p.unlink()
        os.rmdir(self.tmp)

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

    def test_an_undeclared_option_is_refused_with_usage(self):
        out, rc = self._check(["# wk: name=required takes=1 opts --list"], ["ws", "cfg", "--bogus"])
        self.assertEqual(rc, 2)

    def test_a_value_where_none_is_taken_is_refused(self):
        out, rc = self._check(["# wk: name=required opts --list"], ["ws", "--list=yes"])
        self.assertEqual(rc, 2)

    def test_a_missing_value_is_refused(self):
        out, rc = self._check(["# wk: name=required opts --count="], ["ws", "--count"])
        self.assertEqual(rc, 2)

    def test_a_positional_past_takes_is_refused(self):
        out, rc = self._check(["# wk: name=required takes=1"], ["ws", "cfg", "extra"])
        self.assertEqual(rc, 2)

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
