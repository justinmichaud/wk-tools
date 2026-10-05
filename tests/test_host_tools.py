"""What a host installs: host/linux/apt.txt names the command that needs each package."""
import re
import unittest

from tests.support import REPO

APT_TXT = REPO / "host" / "linux" / "apt.txt"


def parse_apt_blocks(text):
    """Group host/linux/apt.txt into (comment_lines, package_lines) blocks, split on blank lines."""
    blocks = []
    chunk = []
    for raw in text.splitlines():
        if raw.strip() == "":
            if chunk:
                blocks.append(chunk)
                chunk = []
            continue
        chunk.append(raw)
    if chunk:
        blocks.append(chunk)

    result = []
    for lines in blocks:
        comment = [l for l in lines if l.lstrip().startswith("#")]
        pkgs = [l.strip() for l in lines if not l.lstrip().startswith("#")]
        if pkgs:
            result.append((comment, pkgs))
    return result


# A `wk <cmd>` mention: "wk" followed by a bare word (stops at the first
# non-word character, so "wk-tools" and "wk new/build" both parse sanely).
WK_CMD_RE = re.compile(r"\bwk ([A-Za-z][A-Za-z0-9_-]*)")
# A repo-relative path: at least one '/' joining word/dot/dash segments.
PATH_RE = re.compile(r"\b[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\b")


class TestAptTxtNamesItsConsumers(unittest.TestCase):
    """Every host/linux/apt.txt block names the `wk` command or repo file that needs it, and that thing actually
    exists (CLAUDE.md: 'no apt.txt line without earning its place')."""

    def test_every_block_names_something_checkable(self):
        text = APT_TXT.read_text()
        blocks = parse_apt_blocks(text)
        self.assertTrue(blocks, "host/linux/apt.txt parsed to no blocks at all")

        unchecked = []
        for comment, pkgs in blocks:
            comment_text = "\n".join(comment)
            label = ", ".join(pkgs)

            found_anything = False

            if "./setup" in comment_text:
                found_anything = True

            for m in WK_CMD_RE.finditer(comment_text):
                cmd = m.group(1)
                found_anything = True
                self.assertTrue(
                    (REPO / "cmd" / cmd).is_file(),
                    f"block '{label}' names 'wk {cmd}', but cmd/{cmd} does not exist",
                )

            for m in PATH_RE.finditer(comment_text):
                path = m.group(0).rstrip(").,:;")
                if (REPO / path).exists():
                    found_anything = True

            if not found_anything:
                unchecked.append(label)

        self.assertEqual(
            unchecked,
            [],
            "host/linux/apt.txt block(s) name nothing checkable (no 'wk <cmd>', "
            "'./setup' mention, or existing repo path) for: "
            + "; ".join(unchecked),
        )

    def test_apt_txt_is_comment_blocks_and_bare_package_names(self):
        for line in APT_TXT.read_text().splitlines():
            if line.strip() == "" or line.lstrip().startswith("#"):
                continue
            self.assertNotIn(" ", line.strip(), f"not a bare package name: {line!r}")


class TestPersistentSettingsRecordAReason(unittest.TestCase):
    """A host setting persists only with a recorded reason: every host/macos/defaults.conf entry is `domain key
    type value reason`, the shape host/macos/settings.sh reads and refuses without the reason."""

    def test_every_default_names_its_reason(self):
        bare = []
        for line in (REPO / "host" / "macos" / "defaults.conf").read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                fields = line.split(None, 4)
                if len(fields) < 5 or fields[2] not in ("string", "bool", "int", "float"):
                    bare.append(line)
        self.assertEqual([], bare)


if __name__ == "__main__":
    unittest.main()
