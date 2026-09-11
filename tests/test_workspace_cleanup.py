"""A test that makes a workspace removes it, however the test ends.

`wk selftest --quick` left `wk-test-<rnd>` running on the container target:
the class that made it removed it in the body of its one test, so an
assertion failing first -- or the `wk rm` under test not converging -- left
the workspace, its container and its build tree behind for a person to find
with `wk ls`. A workspace is expensive and shared; the removal belongs
somewhere the test framework runs either way.

So: a class that calls `wk new` registers the removal in `setUp`/`tearDown`
(`addCleanup`, or a `tearDown` that removes), or removes it in a `finally`
of the same function. This reads the test sources rather than running them:
the classes it is about need a podman VM, and the rule has to hold on a
machine that skips every one of them.

Run: python3 -m unittest tests.test_workspace_cleanup -v
"""
import ast
import unittest

from tests.support import REPO

WK_RUNNERS = ("run", "run_wk")


def _wk_verb(node):
    """The verb of a `run("<verb>", ...)` call, else None."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
    if name not in WK_RUNNERS or not node.args:
        return None
    first = node.args[0]
    return first.value if isinstance(first, ast.Constant) else None


def _runs(node, verb):
    return any(_wk_verb(n) == verb for n in ast.walk(node))


# `wk new <name> --kill` stops a creation and `wk new -h` prints the help
# block; neither leaves a workspace to remove.
MAKES_NOTHING = {"--kill", "-h", "--help"}


def _creates_a_workspace(node):
    for n in ast.walk(node):
        if _wk_verb(n) != "new":
            continue
        words = {a.value for a in n.args if isinstance(a, ast.Constant)}
        if not words & MAKES_NOTHING:
            return True
    return False


def _registers_a_cleanup(cls):
    for n in ast.walk(cls):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "addCleanup":
            return True
        if isinstance(n, ast.FunctionDef) and n.name == "tearDown" and _runs(n, "rm"):
            return True
    return False


def _removes_in_a_finally(cls):
    return any(isinstance(n, ast.Try) and n.finalbody
               and any(_runs(f, "rm") for f in n.finalbody)
               for n in ast.walk(cls))


def creators():
    """(module, class) for every test class that creates a workspace."""
    for path in sorted((REPO / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and _creates_a_workspace(node):
                yield path.name, node


class TestEveryWorkspaceATestMakesIsRemoved(unittest.TestCase):
    def test_a_class_that_creates_one_removes_it_outside_the_test_body(self):
        missing = [f"  {mod}: {cls.name}" for mod, cls in creators()
                   if not (_registers_a_cleanup(cls) or _removes_in_a_finally(cls))]
        self.assertFalse(missing, "a failing assertion in these leaves a "
                                  "workspace behind -- addCleanup in setUp, a "
                                  "tearDown that removes, or a finally:\n"
                         + "\n".join(missing))

    def test_this_rule_is_about_classes_that_exist(self):
        """A reader of the rule above can check it found them: the creators
        are the real integration tests, and there are some."""
        self.assertTrue(list(creators()))

    def test_a_class_that_only_stops_a_creation_creates_nothing(self):
        """`wk new <name> --kill` and `wk new -h` are not creation, so a class
        that only calls those owes no removal."""
        tree = ast.parse(
            'class T(unittest.TestCase):\n'
            '    def test_a(self):\n'
            '        run("new", "ws1", "--kill")\n'
            '        run("new", "-h")\n')
        cls = tree.body[0]
        self.assertFalse(_creates_a_workspace(cls))
        tree = ast.parse(
            'class T(unittest.TestCase):\n'
            '    def test_a(self):\n'
            '        run("new", "ws1", "--no-wait")\n')
        self.assertTrue(_creates_a_workspace(tree.body[0]))


if __name__ == "__main__":
    unittest.main()
