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


HEREDOC_OP_RE = re.compile(r'<<(?!<)(-)?\s*([\'"]?)([A-Za-z_][A-Za-z0-9_]*)\2')
SHELL_DASH_S_RE = re.compile(r'\b(bash|sh)\s+-s\b')


def _heredoc_script_roots():
    for p in sorted((REPO / "cmd").iterdir()):
        if p.is_file():
            yield p
    for p in sorted((REPO / "lib").glob("*.sh")):
        yield p
    for p in sorted((REPO / "targets").glob("*.sh")):
        yield p


def find_remote_script_heredocs():
    """Every heredoc whose opening line names a shell reading a script from
    stdin (`bash -s`/`sh -s`, however it is reached -- `rsh`, `ssh`, `i_ssh`,
    `t_exec` and `_ssh` all end up invoking one of those two forms in this
    tree), paired with the first non-comment line of its body.

    A heredoc that hands a remote `cat` literal *file content* (no `-s`
    shell on the line) is not this: `set -u` means nothing to a config file.
    That is why the trigger is `bash -s`/`sh -s` themselves, not the wrapper
    names alone -- `targets/vm.sh`'s `_write_deploy_keys` calls `_ssh` to
    write `~/.ssh/config` this way and is correctly not one of these.
    """
    found = []
    for path in _heredoc_script_roots():
        rel = str(path.relative_to(REPO))
        lines = path.read_text(errors="replace").splitlines()
        i = 0
        stmt_start = 0
        while i < len(lines):
            line = lines[i]
            if i == 0 or not lines[i - 1].rstrip().endswith("\\"):
                stmt_start = i
            m = HEREDOC_OP_RE.search(line)
            if m and SHELL_DASH_S_RE.search("\n".join(lines[stmt_start:i + 1])):
                delim = m.group(3)
                j = i + 1
                body = []
                while j < len(lines) and lines[j].rstrip() != delim:
                    body.append(lines[j])
                    j += 1
                first = ""
                for b in body:
                    s = b.strip()
                    if not s or s.startswith("#"):
                        continue
                    first = s
                    break
                found.append((rel, i + 1, delim, first))
                i = j + 1
                continue
            i += 1
    return found


class TestRemoteScriptHeredocsSetDashU(unittest.TestCase):
    """The tree's remote-script convention (targets/vm.sh:824,901;
    cmd/bridge:1435,1486; cmd/pi's pi_setup_start_tailscaled): a heredoc body
    executed by a remote `bash -s`/`sh -s` opens with `set -u` (or `set -e`),
    so a bad substitution or an unset variable fails loudly instead of a
    silently-backgrounded launch failing opaquely three commands later
    (docs/defects, the tailscaled-on-rpi5 bug this closes). Pinned the same
    way the audits above are: every heredoc found today must comply, so a
    *new* one that skips it fails here rather than shipping quietly."""

    def test_every_remote_script_heredoc_opens_with_set_dash_u_or_e(self):
        found = find_remote_script_heredocs()
        self.assertTrue(found, "found no rsh/ssh/bash -s heredocs at all -- "
                                "the scan itself is broken")
        bad = [(rel, line, delim, first) for rel, line, delim, first in found
               if not re.match(r'^set\s+-[a-zA-Z]*[eu]', first)]
        self.assertEqual(
            bad, [],
            "these remote-script heredocs do not open with `set -u`/`set -e`: "
            f"{bad}",
        )


# --- every non-sourced, non-daemon script sets `set -euo pipefail` -----------
#
# A file with no shell shebang is read only via `.` (a library) in this tree
# (verified by hand for every file below); that is what "non-sourced" means
# here, and it excludes lib/*.sh, targets/*.sh, boot/*.sh and most of host/*
# without naming any of them. What is left are scripts that run standalone
# (as `wk`'s dispatcher, a LaunchDaemon/LaunchAgent, or someone's `bash
# foo.sh`) or are fed to a remote shell -- and those set -euo pipefail unless
# named below, with the one reason each that earns the exception.
SCRIPT_ROOTS = (
    "cmd", "lib", "targets", "vm", "boot", "build", "host", "bench",
    "container/bin", "container/proxy", "admin",
)
SHELL_SHEBANG_RE = re.compile(r'^#!.*\b(bash|sh|dash|ksh)\b')
SET_EUO_PIPEFAIL_RE = re.compile(r'(?m)^\s*set\s+-euo\s+pipefail\s*$')

# Structural: these have a shell shebang but are never executed through it.
DELIBERATE_EXCLUSIONS = {
    "bench/mac-quiet-hosts.sh":
        "sourced, not run: its own header says it is `.`-read by "
        "mac-bench-volume.sh's do_provision and by mac-bench-firstboot.sh; "
        "the shebang is for a person reading the file, not an exec path",
    "build/mem-watchdog.sh":
        "a background watchdog that loops for the life of a build "
        "(`while kill -0 \"$PID\"; do ... sleep; done`) -- a daemon, not a "
        "one-shot command. `-e` would let one transient `ps`/awk reading "
        "kill the safety net silently instead of the polite failure its own "
        "header describes, so it deliberately keeps `set -uo pipefail`",
}

# Owed, not deliberate: nothing here has decided these are right, only that
# they are today's reality. Named so a *new* script that skips `set -euo
# pipefail` is caught immediately instead of joining this list unnoticed.
NOT_YET_COMPLIANT = set()


def _is_shell_script(path):
    try:
        with path.open(errors="replace") as f:
            first_line = f.readline()
    except OSError:
        return False
    return bool(SHELL_SHEBANG_RE.match(first_line))


# A file fed whole to a remote shell (`_ssh ... 'bash -s' < "$WK_ROOT/vm/
# desktop-probe.sh"`, targets/vm.sh) runs as a script exactly like one with
# its own shebang -- the shebang is simply irrelevant when the caller already
# named the interpreter -- so it is a candidate too, found the same way
# audit 1 above finds a heredoc's `bash -s`/`sh -s`.
REMOTE_FED_SCRIPT_RE = re.compile(r'(?:bash|sh)\s+-s[\'"]?\s*<\s*"\$WK_ROOT/([^"]+)"')


def _remote_fed_script_paths():
    paths = set()
    for root in ("cmd", "lib", "targets"):
        base = REPO / root
        if not base.exists():
            continue
        candidates = base.iterdir() if root == "cmd" else base.glob("*.sh")
        for p in candidates:
            if not p.is_file():
                continue
            paths.update(REMOTE_FED_SCRIPT_RE.findall(p.read_text(errors="replace")))
    return paths


def _iter_candidate_scripts():
    seen = set()
    for root in SCRIPT_ROOTS:
        base = REPO / root
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or "__pycache__" in p.parts:
                continue
            if _is_shell_script(p):
                seen.add(p)
                yield p
    for rel in sorted(_remote_fed_script_paths()):
        p = REPO / rel
        if p.is_file() and p not in seen:
            seen.add(p)
            yield p


class TestEveryScriptSetsEuoPipefail(unittest.TestCase):
    def test_every_non_excluded_script_sets_euo_pipefail(self):
        missing = set()
        for p in _iter_candidate_scripts():
            rel = str(p.relative_to(REPO))
            if rel in DELIBERATE_EXCLUSIONS:
                continue
            if SET_EUO_PIPEFAIL_RE.search(p.read_text(errors="replace")):
                continue
            missing.add(rel)
        self.assertEqual(
            missing, NOT_YET_COMPLIANT,
            f"scripts without `set -euo pipefail` changed since NOT_YET_COMPLIANT "
            f"was pinned (new: {missing - NOT_YET_COMPLIANT}; now fixed, drop from "
            f"NOT_YET_COMPLIANT: {NOT_YET_COMPLIANT - missing})",
        )

    def test_the_deliberate_exclusions_exist_lack_it_and_have_a_reason(self):
        for rel, reason in DELIBERATE_EXCLUSIONS.items():
            self.assertTrue(reason.strip(), rel)
            p = REPO / rel
            self.assertTrue(p.is_file(), f"{rel} no longer exists; drop it from DELIBERATE_EXCLUSIONS")
            self.assertFalse(
                SET_EUO_PIPEFAIL_RE.search(p.read_text(errors="replace")),
                f"{rel} now sets `set -euo pipefail` -- drop it from DELIBERATE_EXCLUSIONS",
            )


if __name__ == "__main__":
    unittest.main()
