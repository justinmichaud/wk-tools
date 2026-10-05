"""lint.layering: README's layers -- a lab module names no WebKit and imports no wk or field module; nothing imports field."""
TIER = "lint"
import ast
import re
import unittest

from tests.support import REPO

# Longest prefix wins. cli is cmd/* and the lib modules only a command runs: the CLI over every layer.
LAYERS = {
    "lab": ("lib/common.sh", "lib/credcheck.py", "lib/secretfile.py", "lib/treehash.py", "lib/wkdata.py", "lib/wk/"),
    "wk": ("lib/contributors.py", "lib/wk/build.py", "lib/wk/git.py", "lib/wk/pgo.py", "lib/wk/pr.py", "lib/wk/presets.py",
           "lib/wk/profile.py", "lib/wk/slot.py", "lib/wk/webkit.py", "lib/wk/bench/ab.py", "lib/wk/bench/autorun.py",
           "lib/wk/bench/board_ab.py", "lib/wk/bench/board_driver.py", "lib/wk/bench/mac_ab.py", "lib/wk/bench/mac_pgo.py",
           "lib/wk/bench/plans.py", "lib/wk/bench/scores.py", "lib/wk/bench/seed.py",
           # The image recipes: WebKit's own Tools/yocto and cross-toolchain-helper, and buildroot's wpewebkit package.
           "lib/wk/sysimage/buildroot.py", "lib/wk/sysimage/buildroot_ws.py", "lib/wk/sysimage/yocto.py", "lib/wk/sysimage/yocto_ws.py",
           # `wk sync`: the mirror, its snapshots, each checkout's upstream and fork wiring, PR heads and git-webkit setup.
           "lib/wk/sync.py"),
    "field": ("lib/wk/bench/report.py",),
    "cli": ("cmd/", "lib/wk/__main__.py", "lib/wk/dispatch.py", "lib/wk/completion.py", "lib/wk/bench/cli.py", "lib/wk/sysimage/cli.py"),
}

WEBKIT = re.compile(r"webkit|javascriptcore|minibrowser|tools/scripts|\bjsc\b", re.I)


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
        self.assertEqual(["%s %s: %s" % (rel, rule, found[(rel, rule)]) for rel, rule in sorted(found)], [],
                         "move the WebKit knowledge or the import into a wk module, and hand it in (README.md, Layers)")


if __name__ == "__main__":
    unittest.main()
