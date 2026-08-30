"""What a host installs: host/linux/apt.txt names the command that needs
each package, and Tart is verified, never installed.

Run: python3 -m unittest tests.test_host_tools -v
"""
import re
import unittest

from tests.support import REPO

APT_TXT = REPO / "host" / "linux" / "apt.txt"


def parse_apt_blocks(text):
    """Group host/linux/apt.txt into (comment_lines, package_lines) blocks,
    split on blank lines. A chunk with no package line (the file's leading
    header) is dropped -- there is nothing for its comment to justify."""
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
    """Every host/linux/apt.txt block names the `wk` command or repo file
    that needs it, and that thing actually exists (CLAUDE.md: 'no apt.txt
    line without earning its place')."""

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
                # A path-shaped token that isn't a real repo path (e.g. a
                # URL fragment or a sysfs path) is not itself a failure --
                # only the absence of *any* checkable mention is.

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
        """Every non-comment, non-blank line is a single bare package name --
        the shape lib/... tools.sh's own parser (host/linux/tools.sh) assumes."""
        for line in APT_TXT.read_text().splitlines():
            if line.strip() == "" or line.lstrip().startswith("#"):
                continue
            self.assertNotIn(" ", line.strip(), f"not a bare package name: {line!r}")


TART_MENTION_RE = re.compile(r"\btart\b", re.IGNORECASE)
TART_APP_CREATE_VERBS = ("mkdir", "cp ", "cp\t", "mv ", "ditto", "unzip", "tar -x", "touch ")


def _tart_lines(path):
    return [l for l in path.read_text().splitlines() if TART_MENTION_RE.search(l)]


class TestTartVerifiedNeverInstalled(unittest.TestCase):
    """`./setup` and the macOS host stages only ever check for Tart
    (~/.local/share/tart/tart.app, ~/.local/bin/tart symlinked into the
    bundle); nothing here installs it (docs/HANDOFF-reprovision.md)."""

    def _files(self):
        files = [REPO / "setup"]
        files += sorted((REPO / "host" / "macos").glob("*.sh"))
        return [f for f in files if f.is_file()]

    def test_presence_check_exists(self):
        found = False
        for f in self._files():
            for line in _tart_lines(f):
                if "have tart" in line or ".local/bin/tart" in line:
                    found = True
        self.assertTrue(
            found, "no 'have tart' / '$HOME/.local/bin/tart' presence check found"
        )

    def test_no_brew_install(self):
        bad = []
        for f in self._files():
            for line in _tart_lines(f):
                if re.search(r"brew\s+install\s+tart\b", line, re.IGNORECASE):
                    bad.append(f"{f}: {line.strip()}")
        self.assertEqual(bad, [], f"found a 'brew install tart' line: {bad}")

    def test_no_download_line_naming_tart(self):
        bad = []
        for f in self._files():
            for line in _tart_lines(f):
                low = line.lower()
                if "curl" in low or "download" in low:
                    bad.append(f"{f}: {line.strip()}")
        self.assertEqual(
            bad, [], f"found a curl/download line naming tart: {bad}"
        )

    def test_no_tart_app_being_created(self):
        bad = []
        for f in self._files():
            for line in _tart_lines(f):
                low = line.lower()
                if "tart.app" in low and any(v in low for v in TART_APP_CREATE_VERBS):
                    bad.append(f"{f}: {line.strip()}")
        self.assertEqual(bad, [], f"found a line creating tart.app: {bad}")


if __name__ == "__main__":
    unittest.main()
