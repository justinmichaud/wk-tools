"""Every source file's code body stays under 5% prose comment lines.

The ceiling is CLAUDE.md's ("Every line of comment, help text and documentation
earns its place"). It is measured rather than reviewed because reviewing it let
the tree drift to 24% twice: prose arrives one defensible line at a time, and
nobody rejects a single line.

Four things are deliberately not prose, because counting them would make the
bar demand their deletion:

Data. A `#` inside a heredoc body or a string literal belongs to whatever reads
it -- a patch payload, a generated config, a script run on a board. Shell is
scanned heredoc-aware and python through `tokenize`, so a measurement never
asks anyone to restructure a string.

Directives. `# wk:` is parsed by the dispatcher out of a command's first 15
lines, `# shellcheck disable` suppresses a real lint, and `# noqa` / `# type:`
are read by tooling.

A cmd/* file's leading `#` run. That block is what `wk <cmd> -h` prints
(`explain_cmd` in `wk`), so it is help text, held to the help-text rule -- what
a flag does -- rather than to a ratio.

A symlink. container/bin's build wall is one script under ten names; it is
measured once, at its target.

Run: python3 -m unittest tests.test_comment_density -v
"""
import ast
import io
import os
import re
import subprocess
import tokenize
import unittest

from tests.support import REPO

MAX_BODY_PROSE = 0.05

# Read by a tool, so not prose.
DIRECTIVE = re.compile(r"^#\s*(wk:|shellcheck\b|noqa\b|type:\s|pylint|pragma|-\*-|!)")

HEREDOC = re.compile(r"""<<-?\s*(["']?)([A-Za-z_][A-Za-z0-9_]*)\1""")

SOURCE_SUFFIXES = (".sh", ".py", ".yaml", ".yml", ".conf")

# The three privileged helpers and their installer. Their comments state the
# grant boundary -- the fixed verb list, "a usb or mmc whole disk only", "never
# one this machine is running from" -- which is the artifact a reviewer reads to
# confirm the privilege is bounded. At 5%, admin/wk-boot-priv would get two
# comment lines for a file whose reason to exist is a documented refusal set.
# Exempt by name, so the exemption is itself reviewable.
GRANT_STATEMENTS = {
    "admin/wk-card-priv",
    "admin/wk-quiesce-priv",
    "admin/wk-boot-priv",
    "admin/install.sh",
}


def tracked_source_files():
    out = subprocess.run(["git", "ls-files"], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.split()
    for rel in out:
        if rel.startswith(("tests/", "docs/")) or rel in GRANT_STATEMENTS:
            continue
        path = REPO / rel
        if path.is_symlink() or not path.is_file():
            continue
        if rel.endswith(SOURCE_SUFFIXES):
            yield rel, path
        elif not os.path.splitext(rel)[1]:
            try:
                with path.open("rb") as f:
                    shebang = f.read(2) == b"#!"
            except OSError:
                continue
            if shebang:
                yield rel, path


def shell_counts(lines):
    """(non-blank lines, prose lines), skipping heredoc bodies."""
    nonblank = prose = 0
    terminator = None
    pending = []
    for line in lines:
        if terminator is not None:
            if line.strip() == terminator:
                terminator = pending.pop(0) if pending else None
                nonblank += 1
            continue
        stripped = line.strip()
        if not stripped:
            continue
        nonblank += 1
        if stripped.startswith("#") and not DIRECTIVE.match(stripped):
            prose += 1
        if not stripped.startswith("#"):
            opened = [m.group(2) for m in HEREDOC.finditer(line)]
            if opened:
                terminator, pending = opened[0], opened[1:]
    return nonblank, prose


def python_counts(src):
    lines = src.splitlines()
    nonblank = len([l for l in lines if l.strip()])
    prose = 0
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type != tokenize.COMMENT:
                continue
            text = tok.string.strip()
            if DIRECTIVE.match(text):
                continue
            # A trailing comment rides on a code line and is not a prose line.
            if lines[tok.start[0] - 1].strip().startswith("#"):
                prose += 1
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return nonblank, prose
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
           and isinstance(first.value.value, str):
            prose += first.value.end_lineno - first.value.lineno + 1
    return nonblank, prose


def body_ratio(rel, path):
    """(non-blank body lines, prose lines) for one file."""
    src = path.read_text(encoding="utf-8", errors="replace")
    lines = src.splitlines()
    start = 1 if lines and lines[0].startswith("#!") else 0

    if rel.endswith(".py"):
        return python_counts("\n".join(lines[start:]))

    end = start
    if rel.startswith("cmd/"):          # the help block `wk <cmd> -h` prints
        while end < len(lines) and (lines[end].lstrip().startswith("#")
                                    or not lines[end].strip()):
            end += 1
    return shell_counts(lines[end:])


class TestCommentDensity(unittest.TestCase):
    def test_no_source_file_body_is_more_than_5_percent_prose(self):
        over = []
        for rel, path in tracked_source_files():
            body, prose = body_ratio(rel, path)
            if not body:
                continue
            # One line is always allowed, so a short file is held to the same
            # rule rather than exempted by its size.
            allowed = max(1, int(MAX_BODY_PROSE * body))
            if prose > allowed:
                over.append(f"  {rel}: {prose}/{body} = "
                            f"{100 * prose / body:.1f}% (allowed {allowed})")
        self.assertEqual(
            over, [],
            "these bodies carry more than 5% prose -- delete what the code already\n"
            "says, or rename so that the code says it:\n" + "\n".join(over))

    def test_the_tree_as_a_whole_is_under_5_percent(self):
        body_total = prose_total = 0
        for rel, path in tracked_source_files():
            body, prose = body_ratio(rel, path)
            body_total += body
            prose_total += prose
        ratio = prose_total / body_total
        self.assertLess(ratio, MAX_BODY_PROSE,
                        f"tree body prose is {100 * ratio:.1f}% "
                        f"({prose_total}/{body_total} lines)")

    def test_the_exempt_files_are_the_privileged_helpers_and_still_exist(self):
        """An exemption nobody can see is an exemption that grows. Each name
        must still be a file, so a rename cannot silently exempt something
        else."""
        for rel in sorted(GRANT_STATEMENTS):
            with self.subTest(exempt=rel):
                self.assertTrue((REPO / rel).is_file(),
                                f"{rel} is exempt from the comment bar but does not exist")


if __name__ == "__main__":
    unittest.main()
