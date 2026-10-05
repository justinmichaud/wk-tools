"""What a host installs: host/linux/apt.txt names the command that needs each package."""
import re
import unittest

from tests.support import REPO

APT_TXT = REPO / "host" / "linux" / "apt.txt"


def parse_apt_blocks(text):
    """host/linux/apt.txt as (comment lines, package lines) per blank-line-separated block that has packages."""
    blocks = [[l for l in chunk.splitlines() if l.strip()] for chunk in re.split(r"\n\s*\n", text)]
    return [([l for l in b if l.lstrip().startswith("#")], [l.strip() for l in b if not l.lstrip().startswith("#")])
            for b in blocks if any(not l.lstrip().startswith("#") for l in b)]


WK_CMD_RE = re.compile(r"\bwk ([A-Za-z][A-Za-z0-9_-]*)")
PATH_RE = re.compile(r"\b[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\b")


class TestAptTxtNamesItsConsumers(unittest.TestCase):
    def test_every_block_names_an_existing_wk_command_or_repo_file_that_needs_it(self):
        blocks = parse_apt_blocks(APT_TXT.read_text())
        self.assertTrue(blocks)
        unchecked = []
        for comment, pkgs in blocks:
            text = "\n".join(comment)
            cmds = [m.group(1) for m in WK_CMD_RE.finditer(text)]
            for cmd in cmds:
                self.assertTrue((REPO / "cmd" / cmd).is_file(), f"{pkgs} names 'wk {cmd}', which does not exist")
            paths = [m.group(0).rstrip(").,:;") for m in PATH_RE.finditer(text)]
            if not ("./setup" in text or cmds or any((REPO / p).exists() for p in paths)):
                unchecked.append(", ".join(pkgs))
        self.assertEqual([], unchecked, "no 'wk <cmd>', './setup' or existing repo path")

    def test_apt_txt_is_comment_blocks_and_bare_package_names(self):
        for line in APT_TXT.read_text().splitlines():
            if line.strip() == "" or line.lstrip().startswith("#"):
                continue
            self.assertNotIn(" ", line.strip(), f"not a bare package name: {line!r}")


class TestPersistentSettingsRecordAReason(unittest.TestCase):
    """Every host/macos/defaults.conf entry is `domain key type value reason`, the shape host/macos/settings.sh reads."""

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
