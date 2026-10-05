"""lint.no_wall_clock_assertions: no unit test asserts a wall-clock bound of its"""
TIER = "lint"
import ast
import inspect
import os
import unittest

from tests.support import REPO

CLOCKS = {"time", "monotonic", "perf_counter", "monotonic_ns", "time_ns", "perf_counter_ns"}
LIVE_GATES = {"requires", "requires_container_target", "requires_machine"}


def reads_clock(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and f.attr in CLOCKS and isinstance(f.value, ast.Name) and f.value.id == "time":
                return True
            if isinstance(f, ast.Name) and f.id in ("monotonic", "perf_counter"):
                return True
    return False


def is_live(node):
    for d in getattr(node, "decorator_list", ()):
        f = d.func if isinstance(d, ast.Call) else d
        if isinstance(f, ast.Name) and f.id in LIVE_GATES:
            return True
    return False


def is_duration(node):
    return any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.Sub) and (reads_clock(n.left) or reads_clock(n.right))
               for n in ast.walk(node))


def names_in(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def timers(tree):
    out = {}
    for f in ast.walk(tree):
        if isinstance(f, ast.FunctionDef):
            for r in ast.walk(f):
                if isinstance(r, ast.Return) and r.value is not None and is_duration(r.value):
                    elts = r.value.elts if isinstance(r.value, ast.Tuple) else None
                    out[f.name] = None if elts is None else {i for i, e in enumerate(elts) if is_duration(e)}
    return out


def durations_bound(assign, timed):
    if is_duration(assign.value):
        return {n.id for t in assign.targets for n in ast.walk(t) if isinstance(n, ast.Name)}
    call = assign.value if isinstance(assign.value, ast.Call) else None
    name = call and getattr(call.func, "attr", getattr(call.func, "id", ""))
    if not name or name not in timed:
        return set()
    out = set()
    for t in assign.targets:
        if isinstance(t, ast.Name) and timed[name] is None:
            out.add(t.id)
        elif isinstance(t, ast.Tuple) and timed[name] is not None:
            out |= {e.id for i, e in enumerate(t.elts) if i in timed[name] and isinstance(e, ast.Name)}
    return out


def fails(stmts):
    for st in stmts:
        for n in ast.walk(st):
            if isinstance(n, ast.Raise) and "AssertionError" in ast.unparse(n.exc or n):
                return True
            if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "fail":
                return True
    return False


def deadlines(fn):
    for n in ast.walk(fn):
        if isinstance(n, ast.If) and reads_clock(n.test) and fails(n.body):
            yield n
        for body in (getattr(n, f, None) for f in ("body", "orelse", "finalbody")):
            if not isinstance(body, list):
                continue
            for st, after in zip(body, body[1:] + [None]):
                if isinstance(st, ast.While) and reads_clock(st.test) and (fails(st.orelse) or (after is not None and fails([after]))):
                    yield st


def start(fn):
    return min([fn.lineno] + [d.lineno for d in fn.decorator_list])


def static_units(tree):
    if any(isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "TIER" for t in n.targets)
           and getattr(n.value, "value", "") == "live" for n in tree.body):
        return set()
    out = set()

    def visit(node, live):
        live = live or is_live(node)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test") and not live:
            out.add(start(node))
        for child in ast.iter_child_nodes(node):
            visit(child, live)
    visit(tree, False)
    return out


def referenced(fn):
    return {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}


def offences(source, path="<test>", units=None):
    tree = ast.parse(source)
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    units = static_units(tree) if units is None else units
    checked = [f for f in funcs if start(f) in units]
    names = set().union(*(referenced(f) for f in checked)) if checked else set()
    while True:
        more = [f for f in funcs if f.name in names and f not in checked and not f.name.startswith("test")]
        if not more:
            break
        checked += more
        names |= set().union(*(referenced(f) for f in more))
    timed = timers(tree)
    out = []
    for fn in checked:
        took = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign):
                took |= durations_bound(n, timed)
        for n in ast.walk(fn):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr.startswith("assert")
                    and any(is_duration(a) or names_in(a) & took for a in n.args)):
                out.append((n.lineno, ast.unparse(n)[:120]))
        out += [(n.lineno, ast.unparse(n).splitlines()[0][:120]) for n in deadlines(fn)]
    return ["%s:%d: %s" % (path, line, text) for line, text in sorted(set(out))]


def unit_tests_by_file():
    from tests.run import flatten, test_tier
    found = unittest.TestLoader().discover(str(REPO / "tests"), top_level_dir=str(REPO))
    out = {}
    for t in flatten(found):
        fn = getattr(type(t), getattr(t, "_testMethodName", ""), None)
        code = getattr(inspect.unwrap(fn), "__code__", None) if fn is not None else None
        if code is not None and test_tier(t) == "unit":
            out.setdefault(os.path.realpath(code.co_filename), set()).add(code.co_firstlineno)
    return out


class TestNoWallClockAssertions(unittest.TestCase):
    def test_the_rule_catches_a_bound_and_passes_a_live_timing_and_a_fake_clock(self):
        bad = ("import time\nclass T:\n    def test_a(self):\n        t0 = time.monotonic()\n        wait(f)\n"
               "        self.assertLess(time.monotonic() - t0, 2)\n"
               "    def took(self):\n        s = time.time()\n        return time.time() - s, 0\n"
               "    def test_b(self):\n        took, _ = self.took()\n        self.assertLess(took, 1)\n        poll(g)\n"
               "def wait(p):\n    deadline = time.monotonic() + 10\n    while not p():\n"
               "        if time.monotonic() > deadline:\n            raise AssertionError('never')\n"
               "def poll(p):\n    deadline = time.monotonic() + 10\n    while time.monotonic() < deadline:\n"
               "        if p():\n            return\n    raise AssertionError('never')\n"
               "def unused(p):\n    if time.monotonic() > 1:\n        raise AssertionError('never')\n")
        self.assertEqual([o.split(":")[1] for o in offences(bad)], ["6", "12", "17", "21"])
        good = ("import time\nclass T:\n    @requires_machine('box')\n    def test_a(self):\n        t0 = time.time()\n"
                "        self.assertLess(time.time() - t0, 9)\n"
                "    def test_b(self):\n        clock = FakeClock()\n        self.assertEqual(clock.now(), 0)\n"
                "        deadline = time.monotonic() + 5\n        self.assertTrue(done)\n"
                "        self.assertGreater(token['expires'], time.time())\n")
        self.assertEqual(offences(good), [])
        self.assertEqual(offences("TIER = 'live'\n" + bad), [])
        self.assertEqual(offences(bad, units={3}), ["<test>:6: self.assertLess(time.monotonic() - t0, 2)",
                                                    "<test>:17: if time.monotonic() > deadline:"])

    def test_no_unit_test_asserts_a_wall_clock_bound(self):
        units = unit_tests_by_file()
        found = []
        for p in sorted((REPO / "tests").glob("*.py")):
            found += offences(p.read_text(), str(p.relative_to(REPO)), units.get(os.path.realpath(p), set()))
        self.assertEqual(found, [], "the runner's budget is the bound; prove an ordering with a FakeClock or a handshake")


if __name__ == "__main__":
    unittest.main()
