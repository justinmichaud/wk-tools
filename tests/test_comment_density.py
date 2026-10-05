"""Every source file's code body stays under 5% prose comment lines."""
TIER = "lint"
import ast
import io
import os
import re
import subprocess
import tempfile
import tokenize
import unittest
from pathlib import Path

from tests.support import REPO

MAX_BODY_PROSE = 0.05

DIRECTIVE = re.compile(r"^#\s*(wk:|shellcheck\b|noqa\b|type:\s|pylint|pragma|-\*-|!)")

HEREDOC = re.compile(r"""<<-?\s*(["']?)([A-Za-z_][A-Za-z0-9_]*)\1""")

SOURCE_SUFFIXES = (".sh", ".py", ".yaml", ".yml", ".conf")

GRANT_STATEMENTS = {
    "admin/wk-card-priv",
    "admin/wk-quiesce-priv",
    "admin/wk-boot-priv",
    "admin/install.sh",
}


def tracked_source_files():
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         cwd=REPO, capture_output=True, text=True, check=True).stdout.split()
    for rel in sorted(set(out)):
        if rel.startswith(("tests/", "docs/", "claude/skills/synced/")) \
                or rel in GRANT_STATEMENTS:
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


def prints_its_own_usage(rel, src):
    return rel.startswith("cmd/") or 'usage_block "$0"' in src


def is_python_source(rel, lines):
    if rel.endswith(".py"):
        return True
    return bool(lines) and lines[0].startswith("#!") and "python3" in lines[0]


def body_ratio(rel, path):
    src = path.read_text(encoding="utf-8", errors="replace")
    lines = src.splitlines()
    start = 1 if lines and lines[0].startswith("#!") else 0

    end = start
    if prints_its_own_usage(rel, src):
        while end < len(lines) and (lines[end].lstrip().startswith("#")
                                    or not lines[end].strip()):
            end += 1

    if is_python_source(rel, lines):
        return python_counts("\n".join(lines[end:]))
    return shell_counts(lines[end:])


class TestCommentDensity(unittest.TestCase):
    def test_no_source_file_body_is_more_than_5_percent_prose(self):
        over = []
        for rel, path in tracked_source_files():
            body, prose = body_ratio(rel, path)
            if not body:
                continue
            allowed = max(1, int(MAX_BODY_PROSE * body))
            if prose > allowed:
                over.append(f"  {rel}: {prose}/{body} = "
                            f"{100 * prose / body:.1f}% (allowed {allowed})")
        if over:
            self.fail(
                f"{len(over)} bodies carry more than 5% prose -- delete what the "
                "code already says, or rename so that the code says it:\n"
                + "\n".join(over))


    def test_a_trailing_comment_counts_as_prose(self):
        nonblank, prose = python_counts("x = 1  # explains why x has to be 1\n")
        self.assertEqual((nonblank, prose), (1, 1))

    def test_a_cmd_python_files_docstring_counts_and_its_help_block_does_not(self):
        src = (
            "#!/usr/bin/env python3\n"
            "#\n"
            "# wk thing <workspace> -- do a thing\n"
            "# wk: where=workspace name=required\n"
            "\n"
            "def f():\n"
            "    \"\"\"Explains what f does.\"\"\"\n"
            "    return 1\n"
        )
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "thing"
            path.write_text(src)
            nonblank, prose = body_ratio("cmd/thing", path)
        self.assertEqual((nonblank, prose), (3, 1))

    def test_the_exempt_files_are_the_privileged_helpers_and_still_exist(self):
        for rel in sorted(GRANT_STATEMENTS):
            with self.subTest(exempt=rel):
                self.assertTrue((REPO / rel).is_file(),
                                f"{rel} is exempt from the comment bar but does not exist")


if __name__ == "__main__":
    unittest.main()
