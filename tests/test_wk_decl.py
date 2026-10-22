"""lib/wk/decl.py: a command's table, the one argv parser built from it, and Args, the one reader of what it parsed."""
import sys
import unittest

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import decl  # noqa: E402
from wk.decl import Command, Opt, Usage  # noqa: E402

PR = Command(name="optional", default="checkout", subs={
    "checkout": dict(takes=("<spec>",)), "rebase": {},
    "open": dict(where="host", opts=(Opt("--draft", "d"),))})
BUILD = Command(name="required", takes=("<preset>",), preset="arg", tail="--", opts=(
    Opt("--list", "l", sets=dict(where="local", name="none", takes=())), Opt("--on", "o", value="<p>"),
    Opt("--env", "e", value="N=V"), Opt("--profile", "p", value="<m>", optional=True)))
QUIESCE = Command(where="host", default="status", subs={"on": {}, "status": dict(readonly=True), "session": dict(
    default="status", subs={"on": dict(opts=(Opt("--bmc", "b"),)), "status": dict(readonly=True)})})


def parse(table, *words, inside=False):
    return decl.parse("probe", table, list(words), inside)


def refusal(table, *words, inside=False):
    with unittest.TestCase().assertRaises(Usage) as cm:
        parse(table, *words, inside=inside)
    return str(cm.exception)


class TestVerbs(unittest.TestCase):
    def verbs(self, table, *words):
        t, args, _ = parse(table, *words)
        return args.verbs, args.positionals[len(args.verbs):], args.name

    def test_a_declared_verb_is_first_and_a_word_that_is_none_is_the_default_verbs(self):
        self.assertEqual(self.verbs(PR, "rebase", "ws"), (["rebase"], [], "ws"))
        self.assertEqual(self.verbs(PR, "1234"), (["checkout"], ["1234"], None))
        self.assertEqual(self.verbs(PR, "ws", "1234"), (["checkout"], ["1234"], "ws"))
        self.assertEqual(self.verbs(QUIESCE), (["status"], [], None))
        self.assertEqual(self.verbs(QUIESCE, "session", "on", "--bmc"), (["session", "on"], [], None))
        self.assertEqual(self.verbs(QUIESCE, "session"), (["session", "status"], [], None))

    def test_a_missing_unknown_or_late_verb_is_refused(self):
        self.assertIn("unknown verb: of (one of on, status, session)", refusal(QUIESCE, "of"))
        self.assertIn("'wk probe' needs one of: on, status", refusal(Command(subs={"on": {}, "status": {}})))
        self.assertIn("--draft comes before the verb", refusal(PR, "--draft", "open"))
        self.assertEqual(self.verbs(PR, "--force", "open", "ws")[0], ["open"])
        self.assertIn("unknown option: --draft", refusal(PR, "rebase", "--draft"))

    def test_a_verb_and_a_given_option_change_the_table(self):
        self.assertEqual(parse(PR, "open")[0].where, "host")
        self.assertEqual(parse(QUIESCE, "session", "status")[0].readonly, True)
        t, args, _ = parse(BUILD, "--list")
        self.assertEqual((t.where, t.name, t.takes, args.name, args.preset), ("local", "none", (), None, None))


class TestArguments(unittest.TestCase):
    PATH = "some dir/a=b:c.txt"

    def test_an_option_is_given_spaced_or_joined_and_every_value_is_kept_in_order(self):
        _, a, _ = parse(BUILD, "ws", "--env", "A=1", "jsc-release", "--on=" + self.PATH, "--env", "--x=1")
        self.assertEqual((a.name, a.preset, a.value("--on"), a.values("--env")), ("ws", "jsc-release", self.PATH, ["A=1", "--x=1"]))
        self.assertEqual((a.order, a.flag("--on"), a.value("--list"), a.values("--nope")), (["--env", "--on", "--env"], False, None, []))
        self.assertEqual(a.argv(), ["--env", "A=1", "--on", self.PATH, "--env", "--x=1"])

    def test_an_optional_value_is_only_ever_joined(self):
        _, a, _ = parse(BUILD, "ws", "--profile", "x.js")
        self.assertEqual((a.flag("--profile"), a.value("--profile"), a.preset), (True, None, "x.js"))
        _, a, _ = parse(BUILD, "ws", "--profile=samply", "x.js")
        self.assertEqual((a.flag("--profile"), a.value("--profile")), (False, "samply"))

    def test_what_the_table_does_not_declare_is_refused_naming_it(self):
        for words, why in ((("ws", "p", "--bogus"), "unknown option: --bogus"), (("ws", "p", "extra"), "unexpected argument: extra"),
                           (("ws", "p", "--on"), "--on needs a value"), (("ws", "p", "--list=yes"), "--list takes no value"),
                           (("ws",), ""), (("--list", "ws"), "unexpected argument: ws")):
            with self.subTest(words=words):
                self.assertEqual(refusal(BUILD, *words), why)
        self.assertEqual(refusal(PR, "ws", "1", "--", "x"), "'--' means nothing to this command")

    def test_the_tail_is_the_other_programs(self):
        self.assertEqual(parse(BUILD, "ws", "p", "--", "--on", "-n")[1].tail, ["--on", "-n"])
        rest = Command(name="required", tail="rest", opts=(Opt("--own", "o"),))
        for words, own, tail in ((("ws", "--own", "--", "git", "status"), True, ["git", "status"]),
                                 (("ws", "git", "--own", "--force"), False, ["git", "--own", "--force"])):
            _, a, _ = parse(rest, *words)
            self.assertEqual((a.flag("--own"), a.tail, a.globals), (own, tail, []))
        every = Command(subs={"pi": dict(name="required", tail="all")})
        _, a, argv = parse(every, "--force", "pi", "ws", "--help", "--force")
        self.assertEqual((a.globals, a.help, a.tail, argv), (["--force"], False, ["--help", "--force"], ["pi", "ws", "--help", "--force"]))
        self.assertEqual(parse(every, "pi", "-r", inside=True)[1].tail, ["-r"])

    def test_the_global_flags_are_read_once_and_a_far_wk_is_handed_the_rest_as_typed(self):
        _, a, argv = parse(BUILD, "-n", "ws", "--yes", "p", "--on", "x", "--quiet", "-h")
        self.assertEqual((a.globals, a.help, argv), (["-n", "--yes", "--quiet"], True, ["ws", "p", "--on", "x", "-h"]))

    def test_inside_a_workspace_there_is_no_name_slot(self):
        self.assertEqual(parse(BUILD, "p", inside=True)[1].preset, "p")
        self.assertEqual(refusal(BUILD, "ws", "p", inside=True), "unexpected argument: p")
        self.assertEqual(parse(Command(name="required", takes=("[<n>...]",)), "a", "b", "c")[1].positionals, ["b", "c"])


class TestHelp(unittest.TestCase):
    def test_the_usage_line_and_every_variant_come_from_the_table(self):
        self.assertEqual(decl.usage("build", BUILD), "wk build <workspace> <preset> [options] [-- <args>]")
        self.assertEqual(decl.usage("pr", PR), "wk pr checkout|rebase|open ...")
        self.assertEqual([label for label, _, _ in decl.variants("q", QUIESCE)],
                         ["wk q on|status|session ...", "wk q on", "wk q status", "wk q session on|status ...",
                          "wk q session on [options]", "wk q session status"])
        self.assertEqual([t.where for _, t, _ in decl.variants("b", BUILD)], ["workspace", "local"])


class TestTheTables(unittest.TestCase):
    def test_every_command_is_a_module_with_a_table_a_function_and_a_summary(self):
        names = [name for name, _ in decl.commands(REPO)]
        self.assertEqual(names, sorted(p.name for p in (REPO / "cmd").iterdir() if p.is_file()))
        for name, m in decl.commands(REPO):
            with self.subTest(cmd=name):
                self.assertIsInstance(m.COMMAND, Command)
                self.assertEqual(m.main.__code__.co_varnames[:2], ("args", "inv"))
                self.assertTrue(decl.summary(m) and not decl.summary(m).endswith("."), decl.summary(m))


if __name__ == "__main__":
    unittest.main()
