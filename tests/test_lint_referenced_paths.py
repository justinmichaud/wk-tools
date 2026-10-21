"""A path a file names under /opt/wk-tools/ or container/bin/ is a tracked file, so an untracked one cannot leave a
workspace's entry broken after a push, which carries commits alone (`lint.referenced_paths_exist`)."""
TIER = "lint"
import re
import subprocess
import unittest

from tests.support import REPO, repo_files

RUNTIME = {".wk-write-probe"}   # lib/wk/wall.py touches and removes it to measure the mount, so no file holds it
REFERENCE = re.compile(r"(?:/opt/wk-tools/|(?<![\w/])container/bin/)([A-Za-z0-9_][A-Za-z0-9_./-]*[A-Za-z0-9_])")


class ReferencedPaths(unittest.TestCase):
    def test_every_referenced_tree_path_is_tracked(self):
        tracked = set(subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--cached"], capture_output=True,
                                     text=True, check=True).stdout.split("\0"))
        missing = []
        for path in repo_files():
            if path.suffix in (".png", ".jpg", ".gz", ".xz", ".zip") or ".git" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for m in REFERENCE.finditer(text):
                name = m.group(1)
                rel = ("container/bin/" + name) if m.group(0).startswith("container/bin/") else name
                if "<" in rel or "*" in rel or "%" in rel or "$" in rel or rel.split("/")[-1] in RUNTIME:
                    continue
                if rel not in tracked and not any(t.startswith(rel + "/") for t in tracked):
                    missing.append("%s: %s" % (path.relative_to(REPO), rel))
        self.assertEqual([], sorted(set(missing)), "named under /opt/wk-tools or container/bin but not a tracked file:\n" + "\n".join(sorted(set(missing))))


if __name__ == "__main__":
    unittest.main()
