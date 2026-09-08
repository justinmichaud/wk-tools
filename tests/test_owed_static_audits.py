"""Static audits over the tree's shell: four shapes that are invisible in
review and fatal at run time, each pinned to what the tree looks like today so
a *new* one fails here rather than shipping quietly.

The first: every command file runs under `set -euo pipefail`, and a function
whose *last* statement is an unguarded `&&` chain returns the left side's
failure as its own exit status -- called as a plain statement (not inside
`if`/`||`), that kills the whole script. A function whose return value is the
answer is exempt and is named below; anything else ends in `return 0`.

Functions are found the way every `_lift`-style helper in this suite already
assumes shell code here is written -- `name() {` alone on a line, closing
`}` alone on a line -- so this reuses that convention rather than parsing
shell in general.

Run: python3 -m unittest tests.test_owed_static_audits -v
"""
import re
import unittest

from tests.support import REPO

# A trailing comment on the definition line is this tree's way of stating a
# function's contract, so it cannot hide a function from this audit.
FUNC_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{\s*(#.*)?$')

# Predicates whose return value is the point, not commands whose failure
# would surprise a caller under `set -e`; a function that ends in an `&&`
# chain and is not a predicate gets `return 0` instead of a place here.
DELIBERATE_PREDICATES = {
    ("lib/common.sh", "gh_authenticated"),
    ("lib/common.sh", "lock_alive"),
    ("lib/resources.sh", "is_headless"),
    ("lib/target.sh", "ws_on_target"),
    ("lib/store.sh", "store_is_local"),
    ("cmd/sync", "snapshot_current"),
    ("cmd/doctor", "podman_machine_running"),
    ("cmd/doctor", "git_speed_ok"),
    ("cmd/ab", "ab_slot_has"),
    ("cmd/push", "_in_vm_driver"),
    ("cmd/sysimage", "_ws_building"),
    ("admin/wk-card-priv", "_slot_present"),
    ("bench/mac-bench-volume.sh", "volume_is_system"),
    ("boot/disk.sh", "_image_wants_wifi"),
    ("container/proxy/ensure-bridge.sh", "bridge_alive"),
    ("image/yocto-build.sh", "bb"),
}


# Every directory holding shell in this tree. Audit 3's SCRIPT_ROOTS is
# narrower on purpose: it asks a question only a standalone script can answer.
SHELL_ROOTS = ("admin", "bench", "boot", "bridge", "build", "cmd", "container",
               "host", "image", "lib", "targets", "vm")
SHELL_SHEBANG_LINE = re.compile(r'^#!.*\b(bash|sh|dash|ksh)\b')

# --- an assignment whose value comes out of a command that reports absence --------------------------
#
# `grep` exits 1 when it matches nothing and `ls` exits nonzero when a path is
# not there, and under `set -euo pipefail` that
# status is the assignment's -- at any stage of the pipeline. So the very case
# the code below then handles (`[ -z "$x" ]`, a `*)` arm, a `pass` line) is the
# one that never arrives: the script dies at the assignment instead. The fix is
# `|| x=""`, which is how the rest of the tree writes it.
#
# The command word only, so `git ls-remote` -- whose failure a caller does mean
# to inherit -- is not this. And only a command this statement itself runs counts. One inside a quoted argument
# belongs to another shell -- `inside`'s own `|| true`, a remote pipeline ending
# in `head` -- and its status never reaches here, so quoted spans are blanked
# out the way audit 1 blanks them.
GREP_ASSIGN_RE = re.compile(
    r'^(local\s+|export\s+|declare\s+)?[A-Za-z_][A-Za-z0-9_]*=\$\(')

# At the head of the substitution or of a pipeline stage: `$(grep …`, `| ls …`.
ABSENCE_CMD_RE = re.compile(r'(?:\$\(|\||;|^)\s*(grep|ls)\s')


def _without_strings(s):
    """The statement with quoted spans blanked and everything else -- the
    substitution's own pipeline included -- left alone. Audit 1 wants the
    opposite (`_without_data` blanks whole substitutions), because the
    question there is which operators are at the *statement's* level."""
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
    """The rule, over any shell text: an assignment from a substitution whose
    own pipeline runs one of those, with nothing to absorb its status."""
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
        """A positive control: the rule is a regex over shell, so a passing
        audit above has to be a clean tree and not a broken scan."""
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
    """Shell by content, not by extension: a `cmd/*` dispatch file and a
    sourced `lib/*.sh` are both shell, and `wk` has no suffix either."""
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
    """The quote still open at the end of `text`, if any. Comments are skipped
    whole: half the apostrophes in this tree are in prose."""
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
    """One entry per statement: continuations joined, a quoted span that runs
    across lines kept whole, a heredoc body skipped. A shell script this tree
    *prints* (boot/pi-mbr.sh's `b_self_disarm_sh`, `printf` of a whole
    self-disarm hook) is one argument, not code at this level."""
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
    """The statement with quoted spans and command substitutions blanked out,
    length preserved so an offset into the result still points into `s`.

    An `&&` inside a string wk hands to another shell, or inside `$( )`, is not
    a chain at this statement's level: its falsiness never becomes the
    function's exit status.
    """
    # Substitutions first, by paren depth: inside `$( )` quoting restarts, so a
    # single left-to-right pass over quotes closes the outer one too early.
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

    # Then quoted spans in what is left.
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


# The compounds whose exit status is their body's last statement. A group's `{`
# is a word of its own, so `${x}` and `find … {} \;` are not one, and its `}`
# follows a space or a `;`.
BODY_TOKEN_RE = re.compile(
    r'(?<![\w$])\{(?=\s)|(?<=[\s;])\}|\bdo\b|\bdone\b|\bthen\b|\bfi\b')
TAIL_CLOSER_RE = re.compile(r'(\}|\bdone\b|\bfi\b)\s*;?\s*$')
BODY_SPLIT_RE = re.compile(BODY_TOKEN_RE.pattern + r'|;')


def _last_statement(body):
    """The statement whose exit status the body returns. `_statements` splits a
    compound at its line breaks, so the `while …; do` and the `done` come back
    as separate entries; joining them back from the tail leaves a statement
    whose end is the group or the loop rather than a bare `done`."""
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
    """The body's last statement: what follows the last `;` that is outside any
    compound nested in it."""
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
    """`masked` with every compound body blanked out, leaving the statement's
    own tail: an `&&` inside a body decides that body's status and reaches the
    function only if the body is what the statement ends with."""
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
    """The last statement of the compound that closes at the end of `stmt`, or
    None when the tail is an ordinary command."""
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
    """The statement whose exit status the function returns. A compound at the
    tail -- whatever pipeline it ends -- hands out the status of the last
    statement of its own body, so a `||` anywhere else in the statement absorbs
    nothing: the last iteration's condition is what `set -e` reads."""
    while True:
        inner = _tail_body(stmt)
        if inner is None:
            return stmt
        stmt = inner


def deciding_and_chain(body):
    """The unguarded `&&` chain this function body returns the status of, or ""."""
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
        """The hole a statement-level scan leaves: the `||` belongs to another
        part of the statement, while the last iteration's condition is what the
        function returns."""
        self.assertEqual(deciding_and_chain([
            '{ have gh || true; } | while read -r n; do',
            '    [ -n "$n" ] && printf \'%s\\n\' "$n"',
            'done']), '[ -n "$n" ] && printf \'%s\\n\' "$n"')
        self.assertEqual(deciding_and_chain([
            'if [ -n "$mac" ]; then',
            '    [ -e "$p" ] && echo "$p"',
            'fi']), '[ -e "$p" ] && echo "$p"')

    def test_a_body_the_statement_does_not_end_with_is_not_the_status(self):
        """The negative controls that keep the rule from firing on every `&&`
        anywhere in a compound: a pipeline stage after `done` and a statement
        after the chain are what the function actually returns."""
        self.assertEqual(deciding_and_chain([
            'for app in /Applications/Install\\ macOS*.app; do',
            '    [ -x "$app/Contents/Resources/startosinstall" ] && printf %s "$app"',
            'done | tail -1']), "")
        self.assertEqual(deciding_and_chain([
            'while :; do',
            '    [ "$mode" != "$last" ] && { log "waiting"; last="$mode"; }',
            '    sleep 10',
            'done']), "")

    def test_gh_authenticated_is_a_deliberate_predicate(self):
        # A concrete example that the pattern is not automatically a bug:
        # `gh_authenticated` is read only as `if gh_authenticated; then ...`.
        text = (REPO / "lib" / "common.sh").read_text()
        self.assertRegex(text, r"gh_authenticated\(\)\s*\{\s*\n\s*have gh && gh api user")


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
    """The tree's remote-script convention: a heredoc body executed by a
    remote `bash -s`/`sh -s` opens with `set -u` (or `set -e`), so a bad
    substitution or an unset variable fails loudly instead of a
    silently-backgrounded launch failing opaquely three commands later.
    Pinned the same way the audits above are: every heredoc found today must
    comply, so a *new* one that skips it fails here rather than shipping
    quietly."""

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

# Empty: every candidate script sets it. The set stays so a script that stops
# setting it names itself here rather than passing unnoticed -- and a genuinely
# exempt one belongs in DELIBERATE_EXCLUSIONS above, with its reason.
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
