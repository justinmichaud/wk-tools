"""A static audit owed by docs/HANDOFF-test-runner.md: "a trailing
`[ -n "$x" ] && ...` as a function's last statement under `set -e` (catches:
the function's exit status becoming 1 and killing an unguarded caller)".

Every command file here runs under `set -euo pipefail`. A function whose
*last* statement is an unguarded `&&` chain returns the left side's failure
as its own exit status; called as a plain statement (not inside `if`/`||`),
that kills the whole script. Some of the tree's `&&`-terminated functions are
deliberate predicates (their return value *is* the answer, e.g.
`store_is_local`) -- this audit cannot tell those apart from a real bug, so
it does not classify, only counts and names what it finds, the same way
docs/HANDOFF-test-runner.md asks: assert the current count so a *new* one
introduced later is caught, without fixing the ones already here.

Functions are found the way every `_lift`-style helper in this suite already
assumes shell code here is written -- `name() {` alone on a line, closing
`}` alone on a line -- so this reuses that convention rather than parsing
shell in general.

Run: python3 -m unittest tests.test_owed_static_audits -v
"""
import re
import unittest

from tests.support import REPO

FUNC_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{\s*$')

# Predicates whose return value is the point, not commands whose failure
# would surprise a caller under `set -e`; a function that ends in an `&&`
# chain and is not a predicate gets `return 0` instead of a place here.
KNOWN_OFFENDERS = {
    ("lib/common.sh", "gh_authenticated"),
    ("lib/resources.sh", "is_headless"),
    ("lib/target.sh", "ws_on_target"),
    ("lib/store.sh", "store_is_local"),
    ("cmd/sync", "snapshot_current"),
    ("cmd/doctor", "podman_machine_running"),
    ("cmd/doctor", "git_speed_ok"),
    # Not predicates -- called for effect, in a context (a plain statement,
    # or inside `$(...)`, which set -e treats as a simple command) where an
    # empty/false condition makes the function itself "fail" and can end its
    # caller's script. These are the ones a fix belongs to; this audit only
    # counts them.
}


def _iter_shell_files():
    for p in sorted((REPO / "lib").glob("*.sh")):
        yield p
    for p in sorted((REPO / "cmd").iterdir()):
        if p.is_file():
            yield p


def _functions(path):
    lines = path.read_text(errors="replace").splitlines()
    i = 0
    out = []
    while i < len(lines):
        m = FUNC_RE.match(lines[i])
        if not m:
            i += 1
            continue
        name = m.group(1)
        depth = 1
        j = i + 1
        body = []
        while j < len(lines) and depth > 0:
            line = lines[j]
            depth += line.count("{") - line.count("}")
            if depth <= 0:
                break
            body.append(line)
            j += 1
        out.append((name, body))
        i = j + 1
    return out


def _last_statement(body):
    for line in reversed(body):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        return s
    return ""


def find_offenders():
    offenders = []
    for f in _iter_shell_files():
        rel = str(f.relative_to(REPO))
        for name, body in _functions(f):
            last = _last_statement(body)
            if not last or "&&" not in last or "||" in last:
                continue
            if last.startswith(("return", "exit")):
                continue
            if last.endswith("\\"):
                continue  # a continued line; not really the last statement
            offenders.append((rel, name, last))
    return offenders


class TestTrailingAndChainAudit(unittest.TestCase):
    def test_the_known_offenders_are_exactly_what_is_here_today(self):
        offenders = find_offenders()
        found = {(rel, name) for rel, name, _ in offenders}
        self.assertEqual(
            found, KNOWN_OFFENDERS,
            f"the trailing-&&-chain audit found a different set than expected "
            f"(new: {found - KNOWN_OFFENDERS}, gone: {KNOWN_OFFENDERS - found}); "
            f"a new one is owed work, a gone one should be dropped from KNOWN_OFFENDERS -- "
            f"full detail: {offenders}",
        )

    def test_gh_authenticated_is_a_deliberate_predicate(self):
        # A concrete example that the pattern is not automatically a bug:
        # `gh_authenticated` is read only as `if gh_authenticated; then ...`.
        text = (REPO / "lib" / "common.sh").read_text()
        self.assertRegex(text, r"gh_authenticated\(\)\s*\{\s*\n\s*have gh && gh api user")


if __name__ == "__main__":
    unittest.main()
