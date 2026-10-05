"""lint.command_names: every `wk <command> [<verb>]` a shipped file tells someone to run names a command that exists"""
TIER = "lint"
import re
import subprocess
import sys
import unittest

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import decl as D  # noqa: E402

BUILTINS = ("help", "completion")
REF = re.compile(r"""(?:['`]|:\s+|\w=\S*\s+|/\s)wk ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?""")
EXEMPT = ("docs/", "tests/", "CLAUDE.md")
README_MARKER = "*** Claude edit below here ***"


def stale(text, decls):
    """(line, words) for each reference naming no command, or no verb of a command that takes only its verbs."""
    for m in REF.finditer(text):
        cmd, verb = m.groups()
        d = decls.get(cmd)
        if cmd in BUILTINS or d and not (d.verbs and not d.default and verb and not D.in_list(verb, d.verbs)):
            continue
        yield text.count("\n", 0, m.start()) + 1, " ".join(filter(None, m.groups()))


def hits():
    decls = {d.name: d for d in D.all_commands(REPO)}
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         cwd=REPO, capture_output=True, text=True, check=True).stdout.splitlines()
    for rel in sorted(set(out)):
        path = REPO / rel
        if rel.startswith(EXEMPT) or path.is_symlink() or not path.is_file():
            continue
        text = path.read_text(errors="replace")
        if rel == "README.md":   # above the marker is the user's spec
            head, _, rest = text.partition(README_MARKER)
            text = "\n" * head.count("\n") + rest
        for n, words in stale(text, decls):
            yield "%s:%d: wk %s" % (rel, n, words)


class TestCommandNames(unittest.TestCase):
    def test_a_retired_command_or_verb_is_caught(self):
        decls = {d.name: d for d in D.all_commands(REPO)}
        said = "run 'wk vm start <name>'\n  then:  wk bench mac <ws> --preflight\nWK_X=1 wk start ws\n'wk help push'"
        self.assertEqual([w for _, w in stale(said, decls)], ["vm start", "bench mac"])

    def test_every_command_a_file_names_exists(self):
        self.assertEqual(list(hits()), [])


if __name__ == "__main__":
    unittest.main()
