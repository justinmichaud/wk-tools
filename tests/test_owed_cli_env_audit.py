"""Every `WK_*` override read with a default under wk/lib/cmd/targets/build
is documented where the user meets it, or removed -- docs/HANDOFF-wk-cli.md
names the three this repo cannot reach from here.

A static audit, not a retyped list: every `${WK_[A-Z_]+:-...}` occurrence
under wk, lib/, cmd/, targets/, build/ is collected by regex (the same
`${VAR:-default}` shape bash actually uses for "read with a default"), and
each variable name is then searched for, as a whole word, across every file
in the tree -- a comment line (any file whose stripped line starts with
`#`, which is where a cmd/* header's own flag/env-var help text lives) or
anywhere in README.md counts as "documented where the user meets it". A
variable that turns up in neither is only ever read, never explained.

ALLOWED_UNDOCUMENTED is the one exception the audit admits, each entry a
name and a one-line reason, given here rather than in a suppressed
assertion so that a new name added to it is a visible diff. It is empty.

Run: python3 -m unittest tests.test_owed_cli_env_audit -v
"""
import re
import unittest

from tests.support import REPO

SCOPE_NAMES = ("wk", "lib", "cmd", "targets", "build")

VAR_RE = re.compile(r'\$\{(WK_[A-Z_]+):-')

# Each read only in a file this module does not own, with the reason its
# owner (not this audit) is the one who documents or removes it.
ALLOWED_UNDOCUMENTED = {}


def _scope_files():
    """Every file under the paths the handoff item names -- `wk` itself is
    a single file, the rest are directories walked recursively."""
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
        if ".git" in parts or "__pycache__" in parts:
            continue
        yield f


def collect_vars():
    """{var_name: set of files it is read with a default in}, restricted to
    the scope the handoff item names."""
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
    """A comment line (any file) or any line of README.md naming this
    variable -- the two places a user actually meets a `WK_*` override:
    a command's own header prose, or the top-level doc."""
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
        if not is_documented(var, all_files)
    )


class TestEveryWkOverrideIsDocumentedOrRemoved(unittest.TestCase):
    def test_no_wk_override_is_read_with_a_default_and_never_explained(self):
        """every WK_* override read with a `${VAR:-default}` default is
        documented in a comment or README line somewhere in the tree, except
        the three named in ALLOWED_UNDOCUMENTED"""
        undocumented = find_undocumented()
        names = {entry.split(" ", 1)[0] for entry in undocumented}
        unexpected = names - set(ALLOWED_UNDOCUMENTED)
        self.assertEqual(unexpected, set(),
                          f"undocumented and not on the allowlist: {sorted(unexpected)}")

    def test_allowlist_holds_only_variables_still_actually_undocumented(self):
        """ALLOWED_UNDOCUMENTED names an open gap, not a permanent exemption
        -- a name documented since is dropped from it, not kept as dead weight"""
        undocumented = find_undocumented()
        names = {entry.split(" ", 1)[0] for entry in undocumented}
        stale = set(ALLOWED_UNDOCUMENTED) - names
        self.assertEqual(stale, set(),
                          f"documented now -- drop from ALLOWED_UNDOCUMENTED: {sorted(stale)}")


if __name__ == "__main__":
    unittest.main()
