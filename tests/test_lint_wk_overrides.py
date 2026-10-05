"""lint.wk_overrides: every `WK_*` environment variable the Python core reads"""
TIER = "lint"
import ast
import re
import unittest

from tests.support import INTERNAL_VARS, REPO
from tests.test_lint_machine_name import python_sources
from wk.targets import CONF_ENV

NAME = re.compile(r"WK_[A-Z0-9_]+")
SELF = "tests/test_lint_wk_overrides.py"


def _is_name(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and NAME.fullmatch(node.value) \
        and not node.value.endswith("_")


class _Reads(ast.NodeVisitor):

    def __init__(self):
        self.where = ["<module>"]
        self.found = {}

    def _mark(self, node):
        self.found.setdefault(node.value, set()).add(self.where[-1])

    def visit_FunctionDef(self, node):
        self.where.append(node.name)
        self.generic_visit(node)
        self.where.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        for arg in node.args[:2]:
            if _is_name(arg):
                self._mark(arg)
        self.generic_visit(node)

    def visit_Subscript(self, node):
        if isinstance(node.ctx, ast.Load) and _is_name(node.slice):
            self._mark(node.slice)
        self.generic_visit(node)

    def visit_Compare(self, node):
        if _is_name(node.left) and isinstance(node.ops[0], (ast.In, ast.NotIn)):
            self._mark(node.left)
        self.generic_visit(node)


def reads(sources):
    out = {}
    for rel, text in sources:
        r = _Reads()
        r.visit(ast.parse(text, rel))
        for var, fns in r.found.items():
            out.setdefault(var, set()).update((rel, fn) for fn in fns)
    return out


def help_block(text):
    lines = []
    for line in text.splitlines()[1:]:
        if not line.startswith("#"):
            break
        lines.append(line)
    return "\n".join(lines)


def read_sites():
    files = list(python_sources()) + [REPO / "wk"]
    return reads((str(p.relative_to(REPO)), p.read_text(errors="replace")) for p in files)


def user_overrides():
    return set(read_sites()) - set(INTERNAL_VARS) - set(CONF_ENV.values())


def documentation():
    texts = [(REPO / "README.md").read_text(errors="replace")]
    texts += [help_block(p.read_text(errors="replace")) for p in sorted((REPO / "cmd").iterdir()) if p.is_file()]
    return "\n".join(texts)


def test_text():
    return "\n".join(p.read_text(errors="replace") for p in sorted((REPO / "tests").glob("*.py"))
                     if str(p.relative_to(REPO)) != SELF)


def named_in(text):
    return set(NAME.findall(text))


class TestReadDetection(unittest.TestCase):
    def test_a_call_naming_the_variable_as_its_first_or_second_argument_reads_it(self):
        src = 'def a(env):\n    return _seconds(env, "WK_A", 1), self._setting("WK_B", 2), f(1, 2, "WK_C"), g(x=1, WK_D=2), h("WK_P_")\n'
        self.assertEqual(reads([("m.py", src)]), {"WK_A": {("m.py", "a")}, "WK_B": {("m.py", "a")}})

    def test_a_read_is_a_get_a_getenv_a_subscript_or_a_membership_test(self):
        src = ('import os\n'
               'def a(env):\n'
               '    return env.get("WK_A", "x"), os.getenv("WK_B"), os.environ["WK_C"], "WK_D" in env\n'
               'def b(env):\n'
               '    env["WK_E"] = "1"\n'
               '    return env.get("HOME")\n')
        self.assertEqual(reads([("m.py", src)]),
                         {"WK_A": {("m.py", "a")}, "WK_B": {("m.py", "a")},
                          "WK_C": {("m.py", "a")}, "WK_D": {("m.py", "a")}})

    def test_a_variable_read_from_two_functions_has_two_sites(self):
        src = 'def a(e):\n    return e.get("WK_A")\ndef b(e):\n    return e.get("WK_A")\n'
        self.assertEqual(len(reads([("m.py", src)])["WK_A"]), 2)

    def test_a_help_block_is_the_leading_comment_lines(self):
        self.assertEqual(help_block("#!/usr/bin/env python3\n# wk x\n# WK_A does y\nimport os  # WK_B\n"),
                         "# wk x\n# WK_A does y")


class TestEveryOverrideIsReadOnceDocumentedAndTested(unittest.TestCase):
    def test_each_override_is_read_in_one_function(self):
        many = sorted(v for v, s in read_sites().items() if len(s) > 1)
        self.assertEqual(many, [], "%d overrides are read in more than one function" % len(many))

    def test_each_override_is_documented_in_the_readme_or_a_help_block(self):
        missing = sorted(user_overrides() - named_in(documentation()))
        self.assertEqual(missing, [], "%d overrides are documented nowhere the user meets them" % len(missing))

    def test_each_override_is_named_in_a_test(self):
        missing = sorted(user_overrides() - named_in(test_text()))
        self.assertEqual(missing, [], "%d overrides no test names" % len(missing))


if __name__ == "__main__":
    unittest.main()
