"""Static audits over the tree's shell: four shapes that are invisible in"""
TIER = "lint"
import re
import unittest

from tests.support import REPO

FUNC_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{\s*(#.*)?$')

DELIBERATE_PREDICATES = {
    ("admin/wk-card-priv", "_slot_present"),
    ("container/proxy/ensure-bridge.sh", "bridge_alive"),
}


SHELL_ROOTS = ("admin", "bench", "boot", "bridge", "build", "cmd", "container",
               "host", "image", "lib", "vm")
SHELL_SHEBANG_LINE = re.compile(r'^#!.*\b(bash|sh|dash|ksh)\b')

GREP_ASSIGN_RE = re.compile(
    r'^(local\s+|export\s+|declare\s+)?[A-Za-z_][A-Za-z0-9_]*=\$\(')

ABSENCE_CMD_RE = re.compile(r'(?:\$\(|\||;|^)\s*(grep|ls)\s')


def _without_strings(s):
    out, quote, i = [], None, 0
    while i < len(s):
        c = s[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = None
            out.append(" ")
            i += 1
            continue
        if c in "'\"":
            quote = c
            out.append(" ")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _grep_assignments_in(text):
    found = []
    for stmt in _statements(text.splitlines()):
        if not GREP_ASSIGN_RE.match(stmt):
            continue
        bare = _without_strings(stmt)
        if "||" in bare or "&&" in bare:
            continue
        if ABSENCE_CMD_RE.search(bare):
            found.append(stmt)
    return found


def find_grep_assignments():
    found = []
    for path in _iter_shell_files():
        text = path.read_text(errors="replace")
        if "set -e" not in text:
            continue
        rel = str(path.relative_to(REPO))
        found += [(rel, stmt) for stmt in _grep_assignments_in(text)]
    return found


class TestGrepAssignmentAudit(unittest.TestCase):
    def test_no_assignment_takes_an_unprotected_exit_status(self):
        found = find_grep_assignments()
        self.assertEqual(
            found, [],
            "these assignments die under `set -euo pipefail` the moment their "
            "grep matches nothing or their ls finds no path, which is the case "
            "the code around them "
            f"handles: {found}. Write `|| name=\"\"`.",
        )

    def test_the_audit_sees_the_shape_it_is_for(self):
        self.assertEqual(len(_grep_assignments_in(
            'blanket=$(printf "%s" "$rules" | grep -E NOPASSWD | tail -1)\n')), 1)
        self.assertEqual(len(_grep_assignments_in(
            'dir=$(ls -1d "$root"/WebKitBuild/*/ 2>/dev/null | head -1)\n')), 1)

    def test_the_two_ways_of_absorbing_it_are_not_reported(self):
        self.assertEqual(_grep_assignments_in(
            'blanket=$(printf "%s" "$rules" | grep -E NOPASSWD) || blanket=""\n'
            'material=$(inside "grep -rl KEY $HOME | head -5")\n'
            'sha=$(git ls-remote "$r" "$ref" | awk \'{print $1}\')\n'), [])


HEREDOC_OP_RE = re.compile(r'<<(?!<)(-)?\s*([\'"]?)([A-Za-z_][A-Za-z0-9_]*)\2')


def _iter_shell_files():
    for root in SHELL_ROOTS:
        for p in sorted((REPO / root).rglob("*")):
            if not p.is_file() or "__pycache__" in p.parts:
                continue
            if p.suffix in (".py", ".pyc", ".md", ".json", ".conf", ".plist"):
                continue
            if p.suffix == ".sh":
                yield p
                continue
            with p.open(errors="replace") as handle:
                if SHELL_SHEBANG_LINE.match(handle.readline()):
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


def _open_quote(text, quote=None):
    i = 0
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c == "#" and (i == 0 or text[i - 1] in " \t"):
            break
        if c in "'\"":
            quote = c
        elif c == "\\":
            i += 1
        i += 1
    return quote


def _statements(body):
    out, buf, heredoc = [], "", None
    for raw in body:
        if heredoc is not None:
            if raw.strip() == heredoc:
                heredoc = None
            continue
        s = raw.strip()
        if not buf and (not s or s.startswith("#")):
            continue
        buf = f"{buf} {s}" if buf else s
        if buf.endswith("\\"):
            buf = buf[:-1]
            continue
        if _open_quote(buf) is not None:
            continue
        out.append(buf)
        m = HEREDOC_OP_RE.search(buf)
        if m:
            heredoc = m.group(3)
        buf = ""
    if buf:
        out.append(buf)
    return out


def _without_data(s):
    out = list(s)
    depth = 0
    i = 0
    while i < len(s):
        if s.startswith("$(", i):
            out[i] = " "
            out[i + 1] = " "
            depth += 1
            i += 2
            continue
        if depth:
            if s[i] == ")":
                depth -= 1
            elif s[i] == "(":
                depth += 1
            out[i] = " "
        i += 1

    quote = None
    for i, c in enumerate(out):
        if quote:
            out[i] = " "
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
            out[i] = " "
    return "".join(out)


BODY_TOKEN_RE = re.compile(
    r'(?<![\w$])\{(?=\s)|(?<=[\s;])\}|\bdo\b|\bdone\b|\bthen\b|\bfi\b')
TAIL_CLOSER_RE = re.compile(r'(\}|\bdone\b|\bfi\b)\s*;?\s*$')
BODY_SPLIT_RE = re.compile(BODY_TOKEN_RE.pattern + r'|;')


def _last_statement(body):
    stmts = _statements(body)
    depth = 0
    for i in range(len(stmts) - 1, -1, -1):
        tokens = [m.group(0) for m in BODY_TOKEN_RE.finditer(_without_data(stmts[i]))]
        depth += sum(1 for t in tokens if t in ("}", "done", "fi"))
        depth -= sum(1 for t in tokens if t in ("{", "do", "then"))
        if depth <= 0:
            return "; ".join(stmts[i:])
    return stmts[-1] if stmts else ""


def _last_in_body(body, masked):
    depth, cut = 0, 0
    for m in BODY_SPLIT_RE.finditer(masked):
        tok = m.group(0)
        if tok in ("{", "do", "then"):
            depth += 1
        elif tok in ("}", "done", "fi"):
            depth -= 1
        elif depth == 0 and masked[m.end():].strip():
            cut = m.end()
    return body[cut:].strip().rstrip(";").strip()


def _without_bodies(masked):
    out = list(masked)
    stack = []
    for m in BODY_TOKEN_RE.finditer(masked):
        if m.group(0) in ("{", "do", "then"):
            stack.append(m.end())
        elif stack:
            for i in range(stack.pop(), m.start()):
                out[i] = " "
    return "".join(out)


def _tail_body(stmt):
    masked = _without_data(stmt)
    if not TAIL_CLOSER_RE.search(masked):
        return None
    span, stack = None, []
    for m in BODY_TOKEN_RE.finditer(masked):
        if m.group(0) in ("{", "do", "then"):
            stack.append(m.end())
        elif stack:
            span = (stack.pop(), m.start())
    if span is None:
        return None
    return _last_in_body(stmt[span[0]:span[1]], masked[span[0]:span[1]])


def _decisive(stmt):
    while True:
        inner = _tail_body(stmt)
        if inner is None:
            return stmt
        stmt = inner


def deciding_and_chain(body):
    last = _last_statement(body)
    if not last or last.endswith("\\"):
        return ""
    stmt = _decisive(last)
    bare = _without_bodies(_without_data(stmt))
    if "&&" not in bare or "||" in bare:
        return ""
    if stmt.startswith(("return", "exit")):
        return ""
    return stmt


def find_offenders():
    offenders = []
    for f in _iter_shell_files():
        rel = str(f.relative_to(REPO))
        for name, body in _functions(f):
            stmt = deciding_and_chain(body)
            if stmt:
                offenders.append((rel, name, stmt))
    return offenders


class TestTrailingAndChainAudit(unittest.TestCase):
    def test_the_only_trailing_and_chains_are_the_deliberate_predicates(self):
        offenders = find_offenders()
        found = {(rel, name) for rel, name, _ in offenders}
        self.assertEqual(
            found, DELIBERATE_PREDICATES,
            f"the trailing-&&-chain audit found a different set than the "
            f"deliberate predicates (new: {found - DELIBERATE_PREDICATES}; "
            f"gone, so drop it from DELIBERATE_PREDICATES: "
            f"{DELIBERATE_PREDICATES - found}). A new one either ends in "
            f"`return 0` or belongs in DELIBERATE_PREDICATES -- "
            f"full detail: {offenders}",
        )

    def test_a_loop_body_decides_the_status_a_stray_or_does_not_absorb(self):
        self.assertEqual(deciding_and_chain([
            '{ have gh || true; } | while read -r n; do',
            '    [ -n "$n" ] && printf \'%s\\n\' "$n"',
            'done']), '[ -n "$n" ] && printf \'%s\\n\' "$n"')
        self.assertEqual(deciding_and_chain([
            'if [ -n "$mac" ]; then',
            '    [ -e "$p" ] && echo "$p"',
            'fi']), '[ -e "$p" ] && echo "$p"')

    def test_a_body_the_statement_does_not_end_with_is_not_the_status(self):
        self.assertEqual(deciding_and_chain([
            'for app in /Applications/Install\\ macOS*.app; do',
            '    [ -x "$app/Contents/Resources/startosinstall" ] && printf %s "$app"',
            'done | tail -1']), "")
        self.assertEqual(deciding_and_chain([
            'while :; do',
            '    [ "$mode" != "$last" ] && { log "waiting"; last="$mode"; }',
            '    sleep 10',
            'done']), "")


SCRIPT_ROOTS = (
    "cmd", "lib", "vm", "boot", "build", "host", "bench",
    "container/bin", "container/proxy", "admin",
)
SET_EUO_PIPEFAIL_RE = re.compile(r'(?m)^\s*set\s+-euo\s+pipefail\s*$')

DELIBERATE_EXCLUSIONS = {
    "bench/mac-quiet-hosts.sh":
        "sourced, not run: its own header says it is `.`-read by "
        "lib/wk/sysimage/macvolume.py's provision and by mac-bench-firstboot.sh; "
        "the shebang is for a person reading the file, not an exec path",
    "build/mem-watchdog.sh":
        "a background watchdog that loops for the life of a build "
        "(`while kill -0 \"$PID\"; do ... sleep; done`) -- a daemon, not a "
        "one-shot command. `-e` would let one transient `ps`/awk reading "
        "kill the safety net silently instead of the polite failure its own "
        "header describes, so it deliberately keeps `set -uo pipefail`",
}


def _iter_candidate_scripts():
    for root in SCRIPT_ROOTS:
        for p in sorted((REPO / root).rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                with p.open(errors="replace") as f:
                    if SHELL_SHEBANG_LINE.match(f.readline()):
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
        self.assertEqual(missing, set(), "scripts without `set -euo pipefail`")

    def test_the_deliberate_exclusions_exist_lack_it_and_have_a_reason(self):
        for rel, reason in DELIBERATE_EXCLUSIONS.items():
            self.assertTrue(reason.strip(), rel)
            p = REPO / rel
            self.assertTrue(p.is_file(), f"{rel} no longer exists; drop it from DELIBERATE_EXCLUSIONS")
            self.assertFalse(
                SET_EUO_PIPEFAIL_RE.search(p.read_text(errors="replace")),
                f"{rel} now sets `set -euo pipefail` -- drop it from DELIBERATE_EXCLUSIONS",
            )


class TestEveryCrossMachinePushNormalisesTheMode(unittest.TestCase):

    REMOTE = re.compile(r'rsync\s[^\n]*(-e\s+"ssh|\$\w+:|@\$)')

    def test_no_cross_machine_rsync_carries_the_local_umask(self):
        bad = []
        for path in _iter_shell_files():
            rel = str(path.relative_to(REPO))
            for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#") or "rsync" not in stripped:
                    continue
                if not self.REMOTE.search(stripped):
                    continue
                if "--chmod=" not in stripped:
                    bad.append(f"{rel}:{n}: {stripped[:90]}")
        self.assertEqual([], bad, "cross-machine rsync without --chmod:\n" + "\n".join(bad))


if __name__ == "__main__":
    unittest.main()
