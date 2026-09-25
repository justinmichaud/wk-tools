"""lint.no_tombstone_remedy: no string in the Python sources sends the user to
a command the dispatcher refuses as a tombstone; only the TOMBSTONES table
itself names one."""
TIER = "lint"
import ast
import re
import unittest

from tests.support import REPO
from tests.test_lint_machine_name import python_sources
from wk import dispatch


def tombstone_table(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "TOMBSTONES" for t in node.targets):
            return node
    return None


def named_tombstones(path, pattern):
    tree = ast.parse(path.read_text(errors="replace"))
    table = tombstone_table(tree)
    exempt = {id(n) for n in ast.walk(table)} if table is not None else set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in exempt:
            for m in pattern.finditer(node.value):
                yield "%s:%d %s" % (path.relative_to(REPO), node.lineno, m.group(0))


class TestNoTombstoneRemedy(unittest.TestCase):
    def test_no_string_names_a_tombstoned_command(self):
        pattern = re.compile(r"\bwk (%s)(?![\w-])" % "|".join(map(re.escape, sorted(dispatch.TOMBSTONES))))
        found = [hit for p in python_sources() for hit in named_tombstones(p, pattern)]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
