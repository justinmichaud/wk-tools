"""lint.layering: README's layers -- a lab module names no WebKit and imports no wk or field module; nothing imports field."""
TIER = "lint"
import ast
import re
import unittest

from tests.support import REPO, owed

# Longest prefix wins. cli is cmd/* and the lib modules only a command runs: the CLI over every layer.
LAYERS = {
    "lab": ("lib/common.sh", "lib/credcheck.py", "lib/secretfile.py", "lib/treehash.py", "lib/wkdata.py", "lib/wk/"),
    "wk": ("lib/contributors.py", "lib/wk/build.py", "lib/wk/git.py", "lib/wk/pgo.py", "lib/wk/pr.py", "lib/wk/presets.py",
           "lib/wk/profile.py", "lib/wk/slot.py", "lib/wk/bench/ab.py", "lib/wk/bench/autorun.py", "lib/wk/bench/board_ab.py",
           "lib/wk/bench/board_driver.py", "lib/wk/bench/seed.py"),
    "field": ("lib/wk/bench/report.py",),
    "cli": ("cmd/", "lib/wk/dispatch.py", "lib/wk/completion.py", "lib/wk/bench/cli.py", "lib/wk/sysimage/cli.py"),
}

WEBKIT = re.compile(r"webkit|javascriptcore|minibrowser|tools/scripts|\bjsc\b", re.I)

# Violations still to be moved into wk modules, by path and rule; each one has to fail still, or it is struck.
NAMES, IMPORTS = ("names",), ("imports",)
BOTH = NAMES + IMPORTS
OWED = {
    # The bench pipeline's mechanics: plans, payloads, run-benchmark and the browser are wk's, handed in.
    "lib/wk/bench/pipeline.py": BOTH, "lib/wk/bench/systems.py": NAMES, "lib/wk/bench/board.py": BOTH, "lib/wk/bench/mac.py": BOTH,
    "lib/wk/bench/record.py": NAMES,
    # The A/B reads its stopping rule and warmup check from the report; they move out of field.
    "lib/wk/bench/board_ab.py": IMPORTS,
    # The image builders' WebKit stage and slots: the recipe moves to wk, sysimage keeps the stage machinery.
    "lib/wk/sysimage/__init__.py": IMPORTS, "lib/wk/sysimage/buildroot.py": BOTH, "lib/wk/sysimage/buildroot_ws.py": NAMES,
    "lib/wk/sysimage/ls.py": BOTH, "lib/wk/sysimage/task.py": IMPORTS, "lib/wk/sysimage/yocto.py": BOTH,
    "lib/wk/sysimage/yocto_ws.py": NAMES, "lib/wk/images.py": NAMES,
    # Places and the store: the checkout, mirror and build-tree names, and the default preset, are wk's.
    "lib/wk/places.py": BOTH, "lib/wk/store.py": NAMES, "lib/wk/workspace.py": BOTH, "lib/wk/sync.py": BOTH, "lib/wk/guest.py": BOTH,
    "lib/wk/record.py": NAMES, "lib/wk/disk.py": NAMES, "lib/wk/gc.py": BOTH, "lib/wk/doctor.py": BOTH, "lib/wk/status.py": NAMES,
    "lib/wk/resources.py": IMPORTS, "lib/wk/machine_cmd/build.py": NAMES, "lib/wk/machine_cmd/deps.py": NAMES,
    # The PR flow's credentials and the browser a measured Mac keeps frontmost.
    "lib/credcheck.py": NAMES, "lib/wk/secrets.py": NAMES, "lib/wk/key/creds.py": NAMES, "lib/wk/pushswitch.py": NAMES,
    "lib/wk/wall.py": NAMES, "lib/wk/quiet.py": NAMES,
}


def layer_of(rel):
    best = max((p for ps in LAYERS.values() for p in ps if rel == p or (p.endswith("/") and rel.startswith(p))),
               key=len, default=None)
    return next((layer for layer, ps in LAYERS.items() if best in ps), None)


def sources():
    for p in sorted((REPO / "lib").rglob("*")):
        if p.suffix in (".py", ".sh"):
            yield p.relative_to(REPO).as_posix(), p
    for p in sorted((REPO / "cmd").iterdir()):
        if p.is_file():
            yield p.relative_to(REPO).as_posix(), p


def module_path(dotted):
    base = REPO / "lib" / dotted.replace(".", "/")
    for p in (base.with_suffix(".py"), base / "__init__.py"):
        if p.is_file():
            return p.relative_to(REPO).as_posix()
    return None


def imported(rel, path):
    """Every lib module `path` imports, at any depth in its body."""
    tree = ast.parse(path.read_text(), str(path))
    pkg = ".".join(rel.split("/")[1:-1]) if rel.startswith("lib/") else ""
    found = set()
    for n in ast.walk(tree):
        names = []
        if isinstance(n, ast.Import):
            names = [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            base = n.module or ""
            if n.level:
                parts = pkg.split(".")[:len(pkg.split(".")) - (n.level - 1)]
                base = ".".join(parts + ([n.module] if n.module else []))
            names = [base] + ["%s.%s" % (base, a.name) for a in n.names]
        found.update(m for m in map(module_path, names) if m)
    found.discard(rel)
    return found


def violations():
    out = {}
    for rel, path in sources():
        layer = layer_of(rel)
        text = path.read_text(errors="replace")
        if layer == "lab":
            words = sorted({m.group(0).lower() for m in WEBKIT.finditer(text)})
            if words:
                out[(rel, "names")] = ", ".join(words)
        if path.suffix == ".sh":
            continue
        below = {"lab": ("wk", "field", "cli"), "wk": ("field", "cli"), "field": ("cli",), "cli": ()}[layer]
        bad = sorted(m for m in imported(rel, path) if layer_of(m) in below and layer_of(m) != layer)
        if bad:
            out[(rel, "imports")] = ", ".join(bad)
    return out


class Layering(unittest.TestCase):

    def test_every_module_has_a_layer(self):
        self.assertEqual([rel for rel, _ in sources() if layer_of(rel) is None], [])

    def test_no_module_uses_a_layer_below_it(self):
        found = violations()
        new = ["%s %s: %s" % (rel, rule, found[(rel, rule)]) for rel, rule in sorted(found) if rule not in OWED.get(rel, ())]
        self.assertEqual(new, [], "move the WebKit knowledge or the import into a wk module (README.md, Layers)")

    def test_every_owed_entry_still_fails(self):
        found = violations()
        self.assertEqual(["%s %s" % (rel, rule) for rel, rules in sorted(OWED.items()) for rule in rules
                          if (rel, rule) not in found], [], "struck from OWED: it passes now")

    @owed("the lab modules in OWED still name WebKit or import wk/field (README.md, Layers)")
    def test_owed_is_empty(self):
        self.assertEqual(OWED, {})


if __name__ == "__main__":
    unittest.main()
