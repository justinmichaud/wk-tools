"""Every `WK_*` override read with a default under wk/lib/cmd/build"""
TIER = "lint"
import re
import unittest

from tests.support import REPO
from wk.targets import CONF_ENV

SCOPE_NAMES = ("wk", "lib", "cmd", "build")

VAR_RE = re.compile(r'\$\{(WK_[A-Z_]+):-')

def _scope_files():
    out = []
    for name in SCOPE_NAMES:
        p = REPO / name
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            out.extend(f for f in sorted(p.rglob("*")) if f.is_file())
    return out


def _all_repo_files():
    for f in sorted(REPO.rglob("*")):
        if not f.is_file():
            continue
        parts = f.parts
        if ".git" in parts or "__pycache__" in parts or parts[len(REPO.parts)] == "tests":
            continue
        yield f


def collect_vars():
    found = {}
    for f in _scope_files():
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for m in VAR_RE.finditer(text):
            found.setdefault(m.group(1), set()).add(str(f.relative_to(REPO)))
    return found


def is_documented(var, all_files):
    word_re = re.compile(r'\b' + re.escape(var) + r'\b')
    for f in all_files:
        try:
            text = f.read_text(errors="replace")
        except (OSError, UnicodeDecodeError):
            continue
        if f.name == "README.md":
            if word_re.search(text):
                return True
            continue
        for line in text.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#") and word_re.search(stripped):
                return True
    return False


def find_undocumented():
    all_files = list(_all_repo_files())
    found = collect_vars()
    return sorted(
        f"{var} (read in {sorted(files)[0]})"
        for var, files in found.items()
        if var not in CONF_ENV.values() and not is_documented(var, all_files)
    )


class TestEveryWkOverrideIsDocumentedOrRemoved(unittest.TestCase):
    def test_no_wk_override_is_read_with_a_default_and_never_explained(self):
        self.assertEqual(find_undocumented(), [])


if __name__ == "__main__":
    unittest.main()
