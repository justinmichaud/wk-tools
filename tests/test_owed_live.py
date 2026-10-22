"""docs/PLAN.md's Owed table as owed stubs: every `live x.y` and `unit x.y` name in its test column that no test
holds gets one stub here, generated at import, which fails until a body holds the name and the runner counts as owed.
A name is held by a test it names (`unit doctor.TestDeviceRows`, `live dev_integration.test_05_...`, a module, or a
`def test_<x>_<y>`), or by a test naming it in backticks -- a live name only by a live-gated or owed-marked one, so a
unit test of a live row's half holds nothing; `[<placeholder>]` is held by any instance."""
import ast
import re
import unittest

from tests import support

TESTS = support.REPO / "tests"
LIVE_GATES = {"requires", "requires_container_place", "requires_machine"}


def owed_rows():
    """{name: behaviour} for every `live`/`unit` name in the Owed table's test column, in table order."""
    text = (support.REPO / "docs" / "PLAN.md").read_text()
    table = text[text.index("\n## Owed"):]
    table = table[:table.index("\n### ")]
    rows = {}
    for line in table.splitlines():
        if line.startswith("| ") and not line.startswith(("| ---", "| owed behaviour")):
            cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
            for name in re.findall(r"`((?:live|unit) [^`]+)`", cells[-1]):
                rows.setdefault(name, cells[0])
    return rows


def _decorator(d):
    f = d.func if isinstance(d, ast.Call) else d
    return getattr(f, "id", getattr(f, "attr", None))


def _live(node):
    return any(_decorator(d) in LIVE_GATES for d in node.decorator_list) or any(
        ast.unparse(s) == "wk_tier = 'live'" for s in node.body)


def holders():
    """(each module's raw text, {module: names it defines}, [(docstring and owed reasons, live or owed)])."""
    raw, defs, held = [], {}, []
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name == "test_owed_live.py":
            continue
        text = path.read_text()
        raw.append(text)
        tree = ast.parse(text)
        live_module = any(ast.unparse(n) == "TIER = 'live'" for n in tree.body)
        names = defs.setdefault(path.stem, set())
        held.append((ast.get_docstring(tree) or "", live_module))

        def visit(node, live):
            for c in ast.iter_child_nodes(node):
                if isinstance(c, (ast.FunctionDef, ast.ClassDef)):
                    names.add(c.name)
                    reasons = [a.value for d in c.decorator_list if isinstance(d, ast.Call) and _decorator(d) == "owed"
                               for a in d.args if isinstance(a, ast.Constant)]
                    said = "\n".join([ast.get_docstring(c) or ""] + ["`%s` %s" % (r.split(":")[0], r) for r in reasons])
                    held.append((said, live or live_module or _live(c) or bool(reasons)))
                    visit(c, live or _live(c))
        visit(tree, False)
    return raw, defs, held


def is_held(name, raw, defs, held):
    rest = name.split(" ", 1)[1]
    parts = re.sub(r"\[[^]]*\]", "", rest).split(".")
    if "[" not in rest or len(parts) > 1:
        for mod in (parts[0], "test_" + parts[0]):
            if mod in defs and all(c in defs[mod] or "test_" + c in defs[mod] for c in parts[1:]):
                return True
    if "test_" + "_".join(parts) in set().union(*defs.values()):
        return True
    said = re.compile("`%s`" % re.sub(r"\\\[<[^>]*>\\\]", r"\\[[^]]+\\]", re.escape(name)))
    if name.startswith("unit "):
        return any(said.search(t) for t in raw)
    return any(live and said.search(text) for text, live in held)


def shortened(behaviour, limit=110):
    plain = re.sub(r"`([^`]*)`", r"\1", behaviour)
    return plain if len(plain) <= limit else plain[:limit].rsplit(" ", 1)[0] + " ..."


def stub(name, behaviour):
    reason = "%s: %s" % (name, shortened(behaviour))

    def test(self):
        self.fail("not yet proven: " + reason)
    test.__doc__ = "`%s`" % name
    return support.owed(reason)(test)


class TestOwedLiveRows(unittest.TestCase):
    wk_tier = "live"


class TestOwedUnitRows(unittest.TestCase):
    pass


def generate():
    raw, defs, held = holders()
    for name, behaviour in owed_rows().items():
        if not is_held(name, raw, defs, held):
            cls = TestOwedLiveRows if name.startswith("live ") else TestOwedUnitRows
            setattr(cls, "test_" + re.sub(r"\W+", "_", name.split(" ", 1)[1]).strip("_"), stub(name, behaviour))


generate()

if __name__ == "__main__":
    unittest.main()
