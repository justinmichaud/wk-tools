"""Every entry point starts in a fresh interpreter with only lib/ on its path, importing nothing first: each `python3 -m`
module the tree runs answers --help with its usage, and each cmd/* and each script that imports lib loads."""
import re
import subprocess
import sys
import unittest

from tests.support import REPO, clean_env

LIB = str(REPO / "lib")
SKIP = (".git", ".claude", "tests", "docs")
# `-m wk.x`, `wk_py wk.x`, `wk_eval wk.x`, `"-m", "wk.x"` and isolated_module(<lib>, "wk.x"): every way the tree runs a module.
MODULE = re.compile(r'(?:-m |wk_py |wk_eval |"-m", "|isolated_module\([^\n]*?")((?:wk|credcheck)(?:\.[a-z_]+)*)\b')
ISOLATED = "import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); runpy.run_module(sys.argv.pop(1), run_name='__main__', alter_sys=True)"
GUARD = re.compile(r'^if __name__ == "__main__":', re.M)
IMPORTS_LIB = re.compile(r"^ *(from|import) (wk|credcheck)\b", re.M)
LOAD = "import runpy, sys; sys.path.insert(0, sys.argv[1]); runpy.run_path(sys.argv[2], run_name='wk_entry_probe')"


def files():
    tracked = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=REPO,
                             capture_output=True, text=True, check=True).stdout.split()
    for rel in sorted(tracked):
        p = REPO / rel
        if p.is_file() and rel.split("/")[0] not in SKIP and p.suffix not in (".md", ".pyc", ".png", ".json"):
            yield p


def modules():
    found = set()
    for p in files():
        found.update(MODULE.findall(p.read_text(errors="replace")))
    return sorted(m for m in found if m != "wk")


def scripts():
    """Every python file outside lib/wk that imports lib: the dispatcher, cmd/*, lib's own scripts, the container's daemons."""
    out = []
    for p in files():
        rel = p.relative_to(REPO).as_posix()
        if rel.startswith("lib/wk/"):
            continue
        with open(p, "rb") as f:
            head = f.readline()
        if (p.suffix == ".py" or (head.startswith(b"#!") and b"python" in head)) and IMPORTS_LIB.search(p.read_text(errors="replace")):
            out.append(p)
    return out


def child(argv):
    return subprocess.run([sys.executable, "-I", "-c"] + argv, capture_output=True, text=True, timeout=60, env=clean_env(), cwd=str(REPO))


class EntryPoints(unittest.TestCase):

    def test_the_tree_runs_modules_and_scripts(self):
        self.assertIn("wk.git", modules())
        self.assertIn(REPO / "cmd" / "new", scripts())

    def test_every_module_the_tree_runs_answers_help(self):
        for m in modules():
            with self.subTest(module=m):
                cp = child([ISOLATED, LIB, m, "--help"])
                self.assertNotIn("Traceback", cp.stderr)
                self.assertIn("usage", (cp.stdout + cp.stderr).lower())

    def test_every_script_loads_or_answers_help(self):
        """One with a __main__ guard is loaded without running it; one without runs to its usage."""
        for p in scripts():
            with self.subTest(script=str(p.relative_to(REPO))):
                if GUARD.search(p.read_text()):
                    cp = child([LOAD, LIB, str(p)])
                    self.assertEqual((0, ""), (cp.returncode, cp.stderr))
                else:
                    cp = subprocess.run([sys.executable, "-I", str(p), "--help"], capture_output=True, text=True, timeout=60,
                                        env=clean_env(), cwd=str(REPO))
                    self.assertNotIn("Traceback", cp.stderr)
                    self.assertIn("usage", (cp.stdout + cp.stderr).lower())


if __name__ == "__main__":
    unittest.main()
