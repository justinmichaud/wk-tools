"""lint.one_machine_name_reader: this host's name is read in one place,
lib/wk/record.py's `host_name` (the primitive `record.machine_name`, the lock
path and `lib/wk/doctor.py`'s `Doctor.hostname` all build on); no other Python
source under lib/wk or cmd reads it, and no bash that runs beside wk-tools on a
host (lib/, cmd/, host/, setup) runs `hostname` -- lib/common.sh's
wk_machine_name asks record.machine_name. What runs elsewhere (a build box
being probed, a board's first boot, the prompt) names that machine, not this one.

Run: python3 tests/run.py --lint -k test_lint_machine_name
"""
TIER = "lint"
import ast
import re
import unittest

from tests.support import REPO, shell_files

READER = ("lib/wk/record.py", "host_name")


def python_sources():
    """Every lib/wk module, plus every cmd/* file that is Python (its shebang says so)."""
    yield from sorted((REPO / "lib" / "wk").rglob("*.py"))
    for p in sorted((REPO / "cmd").iterdir()):
        if p.is_file() and p.read_text(errors="replace").startswith("#!/usr/bin/env python3"):
            yield p


class _Runs(ast.NodeVisitor):
    """The enclosing function of every hostname-reading call or literal:
    `socket.gethostname()`, `os.uname().nodename`, `["hostname", ...]`,
    `platform.node()`."""

    def __init__(self):
        self.where = ["<module>"]
        self.found = set()

    def visit_FunctionDef(self, node):
        self.where.append(node.name)
        self.generic_visit(node)
        self.where.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _mark(self):
        self.found.add(self.where[-1])

    def visit_Attribute(self, node):
        if node.attr == "nodename":
            self._mark()
        self.generic_visit(node)

    def visit_Call(self, node):
        f = node.func
        dotted = (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                  and "%s.%s" % (f.value.id, f.attr))
        if dotted in ("socket.gethostname", "platform.node"):
            self._mark()
        self.generic_visit(node)

    def visit_List(self, node):
        if node.elts and isinstance(node.elts[0], ast.Constant) and node.elts[0].value == "hostname":
            self._mark()
        self.generic_visit(node)

    visit_Tuple = visit_List


class TestOneMachineNameReader(unittest.TestCase):
    def test_one_function_under_lib_wk_or_cmd_reads_the_hostname(self):
        found = []
        for path in python_sources():
            runs = _Runs()
            runs.visit(ast.parse(path.read_text(errors="replace"), str(path)))
            found += [(str(path.relative_to(REPO)), fn) for fn in sorted(runs.found)]
        self.assertEqual(found, [READER], "this machine's name is read by record.host_name; call it instead")

    def test_no_host_side_bash_runs_hostname(self):
        runs = re.compile(r"(^|[;&|({`$]|\bthen|\bdo)\s*hostname(\s+-s)?\s*(2>|\||\)|;|$)", re.M)
        found = []
        for path in shell_files():
            rel = str(path.relative_to(REPO))
            if not (rel.split("/")[0] in ("lib", "cmd", "host") or rel == "setup"):
                continue
            body = "\n".join(l for l in path.read_text(errors="replace").splitlines() if not l.lstrip().startswith("#"))
            if runs.search(body):
                found.append(rel)
        self.assertEqual(found, [], "lib/common.sh's wk_machine_name is the bash spelling of this machine's name")


if __name__ == "__main__":
    unittest.main()
