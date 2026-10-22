"""Every command has the same shape, declared once in its table and enforced by the dispatcher."""
TIER = "lint"
import contextlib
import io
import os
import re
import sys
import unittest
from unittest import mock

from tests.support import REPO, WkTest, bash, clean_env, run

sys.path.insert(0, str(REPO / "lib"))
from wk import act, decl, dispatch, repos  # noqa: E402

TABLES = [(name, m) for name, m in decl.commands(REPO)]
GLOBAL_OPTS = set(decl.GLOBALS) | set(decl.HELP)


def declared(table):
    return {o.flag for _, t, opts in decl.variants("x", table) for o in tuple(opts) + tuple(t.opts)}


def code_opts(path):
    """Every option the code tests for, reads off Args, or iterates over to read: never another program's argv."""
    text = path.read_text()
    listed = re.findall(r'(?:^[A-Z_]+ = |in )\(([^)]*)\)', text, re.M)
    return set(re.findall(r'"(-{1,2}[a-z_][a-z_-]*)" in ', text) + re.findall(r'== "(-{1,2}[a-z_][a-z_-]*)"', text) +
               re.findall(r'\.(?:flag|value|values)\("(-{1,2}[a-z_][a-z_-]*)"\)', text) +
               [o for group in listed for o in re.findall(r'"(--?[a-z_][a-z_-]*)"', group)])


def refused(*argv):
    out = io.StringIO()
    with mock.patch.dict(os.environ, clean_env(wk_root=False), clear=True), mock.patch.object(dispatch, "_registry", None), \
            contextlib.redirect_stderr(out), contextlib.redirect_stdout(out):
        try:
            dispatch.Invocation(argv[0], list(argv[1:]))
        except act.Refused as e:
            return e.status, out.getvalue()
    return 0, out.getvalue()


class TestTheTablesAreWellFormed(unittest.TestCase):
    def test_every_field_holds_a_value_the_dispatcher_reads(self):
        for name, m in TABLES:
            for label, t, _ in decl.variants(name, m.COMMAND):
                with self.subTest(variant=label):
                    self.assertTrue(callable(t.where) or t.where in ("host", "store", "local", "workspace"))
                    self.assertTrue(callable(t.name) or t.name in ("required", "optional", "none"))
                    self.assertIn(t.tail, decl.TAILS)
                    self.assertIn(t.group, ("workspaces", "sources", "hosts", "other"))
                    self.assertTrue(set(t.repos) <= set(repos.names()))
                    self.assertTrue(t.default in t.subs if t.default else True)

    def test_the_declared_options_are_the_ones_the_code_reads(self):
        for name, m in TABLES:
            path = REPO / "cmd" / name
            with self.subTest(cmd=name):
                ours = declared(m.COMMAND) - {"--preset"}
                code = code_opts(path) - GLOBAL_OPTS
                package = [f.read_text() for f in (REPO / "lib" / "wk" / name).glob("*.py")]
                generic = "cli.options(args" in path.read_text() or "profile.main" in path.read_text()
                read = code | {o for text in package for o in re.findall(r'"(--[a-z][a-z-]*)"', text)}
                self.assertEqual(set() if generic else ours - read, set(), "declared but not read")
                self.assertEqual(code - ours - {"--preset"}, set(), "read but not declared")

    def test_no_command_parses_the_global_flags_itself(self):
        self.assertEqual({n: code_opts(REPO / "cmd" / n) & GLOBAL_OPTS for n, _ in TABLES if code_opts(REPO / "cmd" / n) & GLOBAL_OPTS}, {})


class TestArgumentsAreRefusedOnce(unittest.TestCase):
    def test_an_unknown_option_and_an_argument_past_the_table_are_refused_with_the_usage_line(self):
        for name, m in TABLES:
            verbs, t = [], m.COMMAND
            while t.subs:
                verbs.append(next(iter(t.subs)))
                t = decl.under(t, t.subs[verbs[-1]])
            for words, why in ((verbs + ["--no-such-option-zz"], "unknown option: --no-such-option-zz"),):
                with self.subTest(cmd=name):
                    status, out = refused(name, *words)
                    self.assertEqual(status, 2, out)
                    self.assertIn(why, out)
                    self.assertIn("usage: wk %s" % name, out)
        self.assertIn("unexpected argument: zz3", refused("scp", "ws", "zz1", "zz2", "zz3")[1])

    def test_every_option_in_every_help_example_is_accepted(self):
        offenders = []
        for name, m in TABLES:
            for line in decl.body(m).splitlines():
                ex = re.match(r"^\s+wk %s(\s.*)?$" % name, line)
                words = [w.strip("[]").split("|")[0] for w in (ex.group(1) or "").split()] if ex else []
                words = [re.sub(r"<[^>]*>", "X", w).strip("\"'") for w in words if w not in ("...", "options")]
                if any(w.startswith("--") for w in words):
                    status, out = refused(name, *words, "-h")
                    if "unknown option" in out:
                        offenders.append("wk %s %s" % (name, " ".join(words)))
        self.assertEqual(offenders, [])


class TestEveryRemovalAsks(unittest.TestCase):
    REMOVING = re.compile(r"delete_vm|\"podman\", *\"(rm|rmi)\"|\"image\", \"prune\"|\"volume\", \"prune\"|unshare\", \"rm\"|"
                          r"tart, \"delete\"")
    REMOVERS = {"lib/wk/gc.py": ("gc",), "lib/wk/sysimage/guestbase.py": ("sysimage",), "lib/wk/places.py": ("rm", "gc", "stop")}

    def test_every_removing_effect_is_reached_only_from_commands_that_ask(self):
        found = {str(p.relative_to(REPO)) for p in (REPO / "lib" / "wk").rglob("*.py") if self.REMOVING.search(p.read_text())}
        self.assertEqual(found, set(self.REMOVERS))
        asking = {n for n, m in TABLES if any(t.destructive for _, t, _ in decl.variants(n, m.COMMAND))}
        self.assertEqual(sorted({c for cs in self.REMOVERS.values() for c in cs} - asking), [])


class TestTheSharedRulesInBash(WkTest):
    def test_quiet_drops_narration_and_keeps_warnings(self):
        cp = self.bash('. lib/common.sh\ninfo "an info line"\nlog "a log line"\nwarn "a warning"\n', env={"WK_QUIET": "1"})
        self.assertEqual(("an info line" in cp.stderr, "a log line" in cp.stderr, "a warning" in cp.stderr), (False, False, True))
        (self.tmp / "machines").mkdir()
        cp = run("machine", "ls", "--quiet", env={"WK_MACHINES_DIR": str(self.tmp / "machines")})
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertNotIn("==>", cp.stdout)

    def test_a_dry_run_prints_the_command_and_asks_nothing_and_an_answer_lets_it_act(self):
        out = bash('. lib/common.sh; export WK_DRY_RUN=1; f=$(mktemp -u)\nact touch "$f"; [ -e "$f" ] && echo EXISTS || echo ABSENT\n'
                   "confirm 'erase it?' </dev/null && echo YES")
        self.assertEqual(("would run: touch" in out.stderr, "would ask: erase it? [y/N]" in out.stderr, out.stdout.split()),
                         (True, True, ["ABSENT", "YES"]))
        out = bash(". lib/common.sh; export WK_DESTRUCTIVE=1 WK_YES=1\nconfirm 'go?'; act echo RAN")
        self.assertIn("RAN", out.stdout)


if __name__ == "__main__":
    unittest.main()
