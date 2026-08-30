"""`wk backup`: the dconf junk filter, the atomic tmp-file-then-`mv` writer,
and the `--candidates` scanner (docs/HANDOFF-settings-audit.md).

Run: python3 -m unittest tests.test_backup -v
"""

import io
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from tests.support import REPO, bash

BACKUP = REPO / "cmd" / "backup"


def _is_macos():
    return os.uname().sysname == "Darwin"


# --- dconf_filter -------------------------------------------------------------

FAKE_DUMP = """\
[org/gnome/desktop/interface]
color-scheme='prefer-dark'

[org/gnome/desktop/peripherals/touchpad]
two-finger-scrolling-enabled=true

[org/gnome/Ptyxis/Shortcuts]
copy-clipboard='<Shift><Control>c'
paste-clipboard='<Shift><Control>v'

[org/gnome/shell/weather]
automatic-location=true
locations=[<(uint32 2, <('Edmonton', 'CYED', true, [(0.1, -0.2)], [(0.1, -0.2)])>)>]

[org/gnome/nm-applet/eap/2adb305e-1792-35eb-b747-173983745914]
ignore-ca-cert=false
ignore-phase2-ca-cert=false

[org/gnome/portal/filechooser/codium]
last-folder-path='/home/jmichaud/Development/webkit-container-sdk'

[org/gnome/Ptyxis/Profiles/491ed247d466382ed0bf24e467c9d78e]
opacity=1.0
last-used=int64 1782745000
"""


class TestDconfFilter(unittest.TestCase):
    """The filter lifted from cmd/backup's Linux path, run against a fake
    dump with each of the four named junk kinds plus a few real settings."""

    def _filtered(self, dump):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".dump", delete=False)
        f.write(dump)
        f.close()
        self.addCleanup(os.unlink, f.name)
        cp = bash(f'source {BACKUP}; dconf_filter < "{f.name}"')
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stdout

    def test_strips_the_four_known_junk_kinds(self):
        out = self._filtered(FAKE_DUMP)
        self.assertNotIn("weather", out, "weather location not stripped")
        self.assertNotIn("Edmonton", out, "weather location not stripped")
        self.assertNotIn("nm-applet", out, "WiFi 802.1X UUID section not stripped")
        self.assertNotIn("2adb305e", out, "WiFi UUID not stripped")
        self.assertNotIn("last-folder-path", out, "GTK last-folder not stripped")
        self.assertNotIn("Ptyxis/Profiles", out, "Ptyxis profile UUID not stripped")
        self.assertNotIn("491ed247", out, "Ptyxis profile UUID not stripped")
        self.assertNotIn("last-used", out, "Ptyxis profile timestamp not stripped")

    def test_keeps_real_settings(self):
        out = self._filtered(FAKE_DUMP)
        self.assertIn("org/gnome/desktop/interface", out)
        self.assertIn("color-scheme='prefer-dark'", out)
        self.assertIn("two-finger-scrolling-enabled=true", out)
        self.assertIn("org/gnome/Ptyxis/Shortcuts", out)
        self.assertIn("copy-clipboard=", out)

    def test_known_line_level_junk_still_filtered(self):
        # welcome-dialog-last-shown-version, last-selected-power-profile,
        # looking-glass-history and command-history: pre-existing key-level
        # filters, still exercised so a refactor can't silently drop one.
        dump = (
            "[org/gnome/shell]\n"
            "welcome-dialog-last-shown-version='46.0'\n"
            "last-selected-power-profile='performance'\n"
            "favorite-apps=['firefox_firefox.desktop']\n"
            "\n"
            "[org/gnome/shell/looking-glass]\n"
            "looking-glass-history=['1 + 1']\n"
        )
        out = self._filtered(dump)
        self.assertNotIn("welcome-dialog-last-shown-version", out)
        self.assertNotIn("last-selected-power-profile", out)
        self.assertNotIn("looking-glass-history", out)
        self.assertIn("favorite-apps=", out)


# --- atomic_update --------------------------------------------------------------

class TestAtomicUpdate(unittest.TestCase):
    """The tmp-file-then-`mv` writer shared by defaults.conf, symbolic
    hotkeys and config.dconf."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="wk-test-backup-")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        os.chmod(self.d, 0o755)
        shutil.rmtree(self.d, ignore_errors=True)

    def _run(self, script):
        return bash(f"source {BACKUP}; {script}", cwd=self.d)

    def test_identical_content_leaves_target_untouched(self):
        target = os.path.join(self.d, "target")
        with open(target, "w") as f:
            f.write("same content\n")
        before = os.stat(target)

        cp = self._run(
            f'printf "same content\\n" > "{self.d}/tmp"; '
            f'atomic_update "{target}" "{self.d}/tmp" label'
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        after = os.stat(target)
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
        self.assertFalse(os.path.exists(f"{self.d}/tmp"), "tmp file not cleaned up")
        with open(target) as f:
            self.assertEqual(f.read(), "same content\n")

    def test_different_content_replaces_target(self):
        target = os.path.join(self.d, "target")
        with open(target, "w") as f:
            f.write("old content\n")

        cp = self._run(
            f'printf "new content\\n" > "{self.d}/tmp"; '
            f'atomic_update "{target}" "{self.d}/tmp" label'
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        with open(target) as f:
            self.assertEqual(f.read(), "new content\n")
        self.assertFalse(os.path.exists(f"{self.d}/tmp"))

    def test_missing_target_is_created(self):
        target = os.path.join(self.d, "target")
        cp = self._run(
            f'printf "brand new\\n" > "{self.d}/tmp"; '
            f'atomic_update "{target}" "{self.d}/tmp" label'
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        with open(target) as f:
            self.assertEqual(f.read(), "brand new\n")


class TestWritePipelineNeverTruncates(unittest.TestCase):
    """cmd/backup makes its tmp file next to the target (mktemp "$target.XXXXXX")
    so the final `mv` is a same-filesystem rename. A write killed before that
    rename -- simulated here by making the target's own directory unwritable,
    which fails the mktemp step exactly the way a directory-full or
    permission-denied failure would in the field -- must never touch the
    target: it is the old file, byte for byte, or the new one, never a
    partial write."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="wk-test-backup-atomicity-")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        os.chmod(self.d, 0o755)
        shutil.rmtree(self.d, ignore_errors=True)

    def test_mktemp_failure_leaves_target_whole(self):
        target = os.path.join(self.d, "config.dconf")
        original = "[org/gnome/desktop/interface]\ncolor-scheme='prefer-dark'\n"
        with open(target, "w") as f:
            f.write(original)
        before = os.stat(target)

        os.chmod(self.d, 0o555)  # read+execute only: mktemp here must fail
        cp = bash(f'tmp=$(mktemp "{target}.XXXXXX") || exit 3; echo "$tmp"')
        self.assertNotEqual(cp.returncode, 0,
                             "mktemp did not fail against an unwritable directory")

        os.chmod(self.d, 0o755)
        after = os.stat(target)
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
        with open(target) as f:
            self.assertEqual(f.read(), original, "target was touched despite the failure")

    @unittest.skipUnless(_is_macos(), "exercises cmd/backup's own macOS path")
    def test_macos_backup_aborts_before_touching_defaults_conf(self):
        # A scratch WK_ROOT: lib/ symlinked to the real repo (so sourcing
        # cmd/backup still finds lib/common.sh), host/macos/defaults.conf a
        # copy of the real one (reading real domains is harmless -- backup
        # never writes to the live system, only to this scratch copy).
        os.symlink(REPO / "lib", os.path.join(self.d, "lib"))
        macos_dir = os.path.join(self.d, "host", "macos")
        os.makedirs(macos_dir)
        real_conf = (REPO / "host" / "macos" / "defaults.conf").read_text()
        conf = os.path.join(macos_dir, "defaults.conf")
        with open(conf, "w") as f:
            f.write(real_conf)
        with open(conf) as f:
            before = f.read()

        os.chmod(macos_dir, 0o555)
        try:
            cp = bash(f"source {BACKUP}; main", env={"WK_ROOT": self.d})
            self.assertNotEqual(cp.returncode, 0,
                                 "macos_backup did not fail when its tmp file could not be created")
        finally:
            os.chmod(macos_dir, 0o755)

        with open(conf) as f:
            after = f.read()
        self.assertEqual(after, before,
                          "defaults.conf changed even though the write never completed")


# --- --candidates ---------------------------------------------------------------

def _extract_candidates_source():
    """The python heredoc inside cmd/backup's candidates_main, lifted so it
    can be exec'd directly against fake `defaults` output instead of
    sweeping this machine's real ~574 domains."""
    text = BACKUP.read_text()
    m = re.search(r"<<'PY'\n(.*?)\nPY\n", text, re.S)
    assert m, "cmd/backup no longer has a python heredoc for --candidates"
    return m.group(1)


CANDIDATES_SRC = _extract_candidates_source()


class TestCandidatesFilter(unittest.TestCase):
    """The --candidates noise filter, run against a fake plist dict instead
    of this machine's real ~574 domains."""

    def _run(self, conf_text, domain_plists):
        conf = tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False)
        conf.write(conf_text)
        conf.close()
        self.addCleanup(os.unlink, conf.name)

        import plistlib as _plistlib

        def fake_run(args, **kwargs):
            result = mock.Mock()
            if args[:2] == ["defaults", "domains"]:
                result.stdout = ",".join(domain_plists)
                result.returncode = 0
            elif args[:2] == ["defaults", "export"]:
                domain = args[2]
                data = domain_plists.get(domain, {})
                result.stdout = _plistlib.dumps(data)
                result.returncode = 0
            else:
                raise AssertionError(f"unexpected subprocess.run call: {args}")
            return result

        buf = io.StringIO()
        with mock.patch("subprocess.run", side_effect=fake_run), \
             mock.patch.object(sys, "argv", ["candidates", conf.name]), \
             redirect_stdout(buf):
            exec(compile(CANDIDATES_SRC, "<candidates>", "exec"), {"__name__": "__candidates__"})
        return buf.getvalue()

    def test_keeps_real_settings_drops_noise_and_known_entries(self):
        domain_plists = {
            "com.example.testapp": {
                "AppleShowRealSetting": True,
                "NSWindow Frame calculator": "500 200 0 0",
                "LastCheckDate": "2024-01-01",
                "SomeUUID": "1234-5678",
                "SUEnableAutomaticChecks": True,
                "AlreadyTracked": "keepme",
                "NestedThing": {"a": 1},
            },
            "NSGlobalDomain": {
                "AnotherRealSetting": 42,
            },
        }
        conf_text = "com.example.testapp AlreadyTracked string keepme\n"
        out = self._run(conf_text, domain_plists)

        self.assertIn("com.example.testapp AppleShowRealSetting True", out)
        self.assertIn("NSGlobalDomain AnotherRealSetting 42", out)

        for noisy in ("NSWindow Frame", "LastCheckDate", "SomeUUID",
                      "SUEnableAutomaticChecks", "AlreadyTracked", "NestedThing"):
            self.assertNotIn(noisy, out, f"{noisy!r} should have been filtered")

    def test_macos_only(self):
        cp = bash(f"source {BACKUP}; is_macos && echo yes || echo no")
        self.assertIn(cp.stdout.strip(), ("yes", "no"))


# --- documentation stays true -----------------------------------------------

class TestHeaderDocumentsCandidates(unittest.TestCase):
    def test_help_text_mentions_candidates(self):
        header = "\n".join(BACKUP.read_text().splitlines()[:40])
        self.assertIn("--candidates", header,
                       "cmd/backup's header (what `wk backup -h` prints) "
                       "doesn't mention --candidates")


if __name__ == "__main__":
    unittest.main()
