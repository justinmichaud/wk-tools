"""lint.vocabulary: one spelling per concept, in no file outside docs/, CLAUDE.md"""
TIER = "lint"
import re
import subprocess
import unittest

from tests.support import REPO

RETIRED = {
    "say 'image workspace' (image_ws), or the precise word for the meaning": re.compile(r"(?i)\blanes?\b"),
    "say 'bench task'": re.compile(r"(?i)\bbenchmark tasks?\b"),
    "say 'snapshot'": re.compile(r"(?i)\bbase[ -]snapshots?\b"),
    "say 'keyring'": re.compile(r"(?i)\bsecrets?[ _]dir(?:ector(?:y|ies))?\b|\bcredential stores?\b"),
    "say the part's name from README's Overrides (lib/wk/store.py)": re.compile(
        r"\b(?:record_dir|artifact_dir|base_dir|base_path|base_sha_file|ws_base_id|"
        r"secrets_view_dir|agent_rw_dir|push_held_dir|held_dir|broker_socket|provisioned_root|machine_store|"
        r"named_root|vm_root|container_mirror)\b"),
}
EXEMPT = ("docs/", "claude/skills/")
README_MARKER = "*** Claude edit below here ***"


def hits():
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         cwd=REPO, capture_output=True, text=True, check=True).stdout.splitlines()
    for rel in sorted(set(out)):
        path = REPO / rel
        if rel in ("CLAUDE.md", "tests/test_lint_vocabulary.py") or rel.startswith(EXEMPT) or path.is_symlink() or not path.is_file():
            continue
        text, skipped = path.read_text(errors="replace"), 0
        if rel == "README.md":  # above the marker is the user's spec
            head, _, text = text.partition(README_MARKER)
            skipped = head.count("\n")
        for n, line in enumerate(text.splitlines(), skipped + 1):
            for why, pattern in RETIRED.items():
                if pattern.search(line):
                    yield "%s:%d: %s (%s)" % (rel, n, line.strip(), why)


class TestVocabulary(unittest.TestCase):
    def test_the_patterns_catch_the_retired_words(self):
        word, long = "la" + "ne", "bench" + "mark task"
        cases = [  # (the rule, retired spellings, spellings that stay)
            ("image workspace", ["each %s builds" % word, "two %ss" % word.upper(), "<%s>" % word], ["a plane, %sway" % word]),
            ("bench task", ["a " + long, ("a " + long + "s").title()], ["a bench task, a benchmark"]),
            ("'snapshot'", ["the base" + " snapshot", "Base" + "-snapshots"], ["a snapshot", "the base image"]),
            ("'keyring'", ["the secrets" + " directory", "secrets" + "_dir", "a credential" + " store"],
             ["the keyring", "a credential stored here", "WK_HOST_SECRETS"]),
            ("part's name", ["store." + "record_dir()", "s." + "agent_rw_dir()", "Store." + "broker_socket"],
             ["store.records_dir()", "store.keyring_agent_rw_dir()", "runtime_socket", "GUEST_BROKER_SOCKET"]),
        ]
        for key, bad, good in cases:
            pattern = next(p for why, p in RETIRED.items() if key in why)
            for text in bad:
                self.assertRegex(text, pattern)
            for text in good:
                self.assertNotRegex(text, pattern)

    def test_no_file_uses_a_retired_word(self):
        self.assertEqual([], list(hits()))


if __name__ == "__main__":
    unittest.main()
