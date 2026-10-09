"""What a command still parses for itself, and what the dispatcher decides instead."""
TIER = "lint"
import os
import re
import sys
import unittest
from unittest import mock

from tests.support import REPO, run

sys.path.insert(0, str(REPO / "lib"))
from wk import decl as D  # noqa: E402
from wk import dispatch  # noqa: E402
from tests.test_cli_shape import declared_opts, literal_opts  # noqa: E402


def commands():
    for f in sorted((REPO / "cmd").iterdir()):
        if f.is_file() and os.access(f, os.X_OK):
            yield f


def header(path):
    return [line.rstrip("\n") for line in D.leading_block(path)]


def synopsis(path):
    for line in header(path)[:5]:
        m = re.match(r"^# wk \S+ ?(.*?) -- ", line)
        if m:
            return m.group(1)
    return ""


def declaration(path):
    out = []
    for line in header(path):
        if not line.startswith("# wk:"):
            continue
        rest = line[len("# wk:"):]
        if rest.startswith(" sub ") or rest.startswith(" flag "):
            continue
        out += rest.split()
    return out


def decl_value(path, key, default=""):
    for tok in declaration(path):
        if tok.startswith(key + "="):
            return tok[len(key) + 1:]
    return default


def parses_flag(path, flag):
    return flag in literal_opts(path.read_text(errors="replace"))


def takes_a_subverb(path):
    if any(l.startswith("# wk: sub ") for l in header(path)):
        return True
    syn = synopsis(path)
    if re.search(r"<verb>|<sub>", syn):
        return True
    return bool(re.search(r"(^|[ |])[a-z][a-z0-9-]*\|[a-z0-9-]+",
                          re.sub(r"<[^>]*>", "", syn)))


SHARED_SPELLING = ("sysimage",)


def takes_a_preset(path):
    if path.name in SHARED_SPELLING:
        return False
    if decl_value(path, "preset"):
        return parses_flag(path, "--preset") or '"--preset"' in path.read_text(errors="replace")
    return "--preset" in declared_opts(path) or "<preset>" in synopsis(path)


def audit(path):
    own = []
    name = decl_value(path, "name", "none").split("@")[0]
    if name != "none" and "ws_name(" not in path.read_text(errors="replace"):
        own.append("name")
    if takes_a_preset(path):
        own.append("preset")
    if takes_a_subverb(path) and not decl_value(path, "verbs"):
        own.append("subverb")
    if parses_flag(path, "--on"):
        own.append("--on")
    return own


class TestWhatTheDispatcherAlreadyDecides(unittest.TestCase):
    def test_no_command_parses_what_the_dispatcher_decides(self):
        got = {c.name: audit(c) for c in commands() if audit(c)}
        self.assertEqual(got, {})


class TestTheVerbIsTheDispatchers(unittest.TestCase):
    def test_an_unknown_or_missing_verb_is_refused_by_the_dispatcher(self):
        for c in commands():
            if not decl_value(c, "verbs") or decl_value(c, "default"):
                continue
            with self.subTest(cmd=c.name):
                cp = run(c.name, "zz-no-such-verb")
                self.assertEqual(cp.returncode, 2, cp.stdout)
                self.assertIn("unknown verb: zz-no-such-verb", cp.stdout)
                self.assertIn("usage: wk %s" % c.name, cp.stdout)
                cp = run(c.name)
                self.assertEqual(cp.returncode, 2, cp.stdout)
                self.assertIn("'wk %s' needs one of:" % c.name, cp.stdout)

    def test_the_verb_is_handed_over_first(self):
        def first(cmd, *args):
            return dispatch.Invocation(cmd, D.Decl(REPO / "cmd" / cmd), list(args)).verb_first()
        self.assertEqual(first("key", "sudo", "status", "--on", "box"), ["sudo", "status", "--on", "box"])
        self.assertEqual(first("key", "--rotate"), ["check", "--rotate"])
        self.assertEqual(first("pr", "ws", "rebase"), ["checkout", "ws", "rebase"])
        self.assertEqual(first("pr", "rebase", "ws"), ["rebase", "ws"])
        self.assertEqual(first("quiesce"), ["status"])
        self.assertEqual(first("pr", "ws", "1234"), ["checkout", "ws", "1234"])
        self.assertEqual(first("sysimage", "presets"), ["presets"])

    def test_an_option_before_the_verb_is_refused(self):
        for argv in (("key", "--on", "box", "sudo"), ("key", "--rotate", "setup"), ("pr", "--draft", "open")):
            with self.subTest(argv=argv):
                cp = run(*argv)
                self.assertEqual(cp.returncode, 2, cp.stdout)
                self.assertIn("comes before the verb", cp.stdout)

    def test_a_sibling_verbs_option_is_refused(self):
        cp = run("key", "set", "--rotate")
        self.assertEqual(cp.returncode, 2, cp.stdout)
        self.assertIn("unknown option: --rotate", cp.stdout)

    def test_a_mistyped_verb_is_named_where_the_default_takes_no_argument(self):
        for argv in (("key", "chek"), ("quiesce", "of")):
            with self.subTest(argv=argv):
                cp = run(*argv)
                self.assertEqual(cp.returncode, 2, cp.stdout)
                self.assertIn("unknown verb: %s (one of" % argv[1], cp.stdout)

    def test_the_far_machine_is_handed_argv_as_typed(self):
        inv = dispatch.Invocation("pr", D.Decl(REPO / "cmd" / "pr"), ["ws", "1234"])
        inv.args = inv.verb_first()
        self.assertEqual((inv.args, inv.typed), (["checkout", "ws", "1234"], ["ws", "1234"]))


class TestTheNameInAWorkspaceAfterAVerb(unittest.TestCase):
    def test_it_is_left_for_the_drop_the_name_refusal(self):
        inv = dispatch.Invocation("pr", D.Decl(REPO / "cmd" / "pr"), ["checkout", "ws", "1234"])
        with mock.patch.object(dispatch, "in_workspace", return_value=True), \
                mock.patch.object(dispatch, "wk_self", return_value="ws"):
            self.assertEqual(inv.argv_check(), ["checkout", "ws", "1234"])


class TestAnAllPassthroughIsTheOtherPrograms(unittest.TestCase):

    def tail(self, *argv, inside=False):
        return dispatch.tail_from(D.Decl(REPO / "cmd" / "ai"), list(argv), inside)

    def test_the_tail_starts_after_the_name(self):
        self.assertEqual(self.tail("pi", "ws", "--help"), 2)
        self.assertEqual(self.tail("--force", "claude", "ws", "--force", "-r"), 3)

    def test_in_a_workspace_it_starts_after_the_agent(self):
        self.assertEqual(self.tail("claude", "--help", inside=True), 1)

    def test_help_after_the_name_is_not_wks(self):
        with mock.patch.object(dispatch, "explain", side_effect=AssertionError("explained")), \
                mock.patch.object(dispatch, "in_workspace", return_value=False), \
                mock.patch.object(dispatch.Invocation, "where", side_effect=dispatch.Exit(0)), \
                mock.patch.dict(os.environ, {}):
            os.environ.pop("WK_FORCE", None)
            with self.assertRaises(dispatch.Exit):
                dispatch.main(["ai", "pi", "ws", "--help", "--force"])
            self.assertNotIn("WK_FORCE", os.environ)

    def test_wks_own_force_goes_before_the_name(self):
        with mock.patch.object(dispatch.Invocation, "where", side_effect=dispatch.Exit(0)), \
                mock.patch.object(dispatch, "in_workspace", return_value=False), \
                mock.patch.dict(os.environ, {}):
            os.environ.pop("WK_FORCE", None)
            with self.assertRaises(dispatch.Exit):
                dispatch.main(["ai", "--force", "claude", "ws"])
            self.assertEqual(os.environ.get("WK_FORCE"), "1")


class TestPythonCommandsReadTheirOptionsThroughArgs(unittest.TestCase):
    def test_every_python_command_reads_its_options_through_args(self):
        by_hand = [c.name for c in commands() if literal_opts(c.read_text()) & declared_opts(c)]
        self.assertEqual(by_hand, [])


if __name__ == "__main__":
    unittest.main()
