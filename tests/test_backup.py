"""`wk key backup`: lib/wk/backup.py's dconf junk filter, the atomic (write only
when content differs) writer, the macOS and Linux flows, and the --candidates
scanner -- all against a Fake machine, so nothing here runs a real `defaults`,
`dconf` or `plutil`.

Run: python3 -m unittest tests.test_backup -v
"""

import os
import unittest

from tests.support import REPO

import sys
sys.path.insert(0, str(REPO / "lib"))
from wk import backup, decl  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Fake, Local  # noqa: E402

CMD_KEY = REPO / "cmd" / "key"


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
    def test_strips_the_four_known_junk_kinds(self):
        out = backup.dconf_filter(FAKE_DUMP)
        self.assertNotIn("weather", out, "weather location not stripped")
        self.assertNotIn("Edmonton", out, "weather location not stripped")
        self.assertNotIn("nm-applet", out, "WiFi 802.1X UUID section not stripped")
        self.assertNotIn("2adb305e", out, "WiFi UUID not stripped")
        self.assertNotIn("last-folder-path", out, "GTK last-folder not stripped")
        self.assertNotIn("Ptyxis/Profiles", out, "Ptyxis profile UUID not stripped")
        self.assertNotIn("491ed247", out, "Ptyxis profile UUID not stripped")
        self.assertNotIn("last-used", out, "Ptyxis profile timestamp not stripped")

    def test_keeps_real_settings(self):
        out = backup.dconf_filter(FAKE_DUMP)
        self.assertIn("org/gnome/desktop/interface", out)
        self.assertIn("color-scheme='prefer-dark'", out)
        self.assertIn("two-finger-scrolling-enabled=true", out)
        self.assertIn("org/gnome/Ptyxis/Shortcuts", out)
        self.assertIn("copy-clipboard=", out)

    def test_known_line_level_junk_still_filtered(self):
        dump = (
            "[org/gnome/shell]\n"
            "welcome-dialog-last-shown-version='46.0'\n"
            "last-selected-power-profile='performance'\n"
            "favorite-apps=['firefox_firefox.desktop']\n"
            "\n"
            "[org/gnome/shell/looking-glass]\n"
            "looking-glass-history=['1 + 1']\n"
        )
        out = backup.dconf_filter(dump)
        self.assertNotIn("welcome-dialog-last-shown-version", out)
        self.assertNotIn("last-selected-power-profile", out)
        self.assertNotIn("looking-glass-history", out)
        self.assertIn("favorite-apps=", out)


# --- atomic_update ----------------------------------------------------------

class TestAtomicUpdate(unittest.TestCase):
    def test_identical_content_leaves_target_untouched(self):
        f = Fake("here")
        f.files["/conf/target"] = "same content\n"
        changed = backup.atomic_update(f, "/conf/target", "same content\n", "label")
        self.assertFalse(changed)
        self.assertEqual(f.files["/conf/target"], "same content\n")
        self.assertEqual([e for e in f.effects if e[0] == "write"], [])

    def test_different_content_replaces_target(self):
        f = Fake("here")
        f.files["/conf/target"] = "old content\n"
        changed = backup.atomic_update(f, "/conf/target", "new content\n", "label")
        self.assertTrue(changed)
        self.assertEqual(f.files["/conf/target"], "new content\n")

    def test_missing_target_is_created(self):
        f = Fake("here")
        changed = backup.atomic_update(f, "/conf/target", "brand new\n", "label")
        self.assertTrue(changed)
        self.assertEqual(f.files["/conf/target"], "brand new\n")


class TestWritePipelineNeverTruncates(unittest.TestCase):
    """`Machine.write` (lib/wk/machine.py) is itself a tmp-file-then-`os.replace`
    on the same filesystem, so a write that cannot complete -- simulated here
    with a read-only directory, which fails exactly the way a full disk or a
    permission error would in the field -- must never touch the target: it is
    the old file, byte for byte, or the new one, never a partial write."""

    def setUp(self):
        import shutil
        import tempfile
        self.d = tempfile.mkdtemp(prefix="wk-test-backup-atomicity-")
        self.addCleanup(lambda: (os.chmod(self.d, 0o755), shutil.rmtree(self.d, ignore_errors=True)))

    def test_a_write_that_cannot_complete_leaves_the_target_whole(self):
        target = os.path.join(self.d, "config.dconf")
        original = "[org/gnome/desktop/interface]\ncolor-scheme='prefer-dark'\n"
        with open(target, "w") as f:
            f.write(original)
        before = os.stat(target)

        os.chmod(self.d, 0o555)  # read+execute only: the tmp file cannot be created
        local = Local()
        with self.assertRaises(OSError):
            backup.atomic_update(local, target, "new content\n", "config.dconf")

        os.chmod(self.d, 0o755)
        after = os.stat(target)
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
        with open(target) as f:
            self.assertEqual(f.read(), original, "target was touched despite the failure")


# --- macos_backup / linux_backup --------------------------------------------

class TestMacosBackup(unittest.TestCase):
    def test_refreshes_values_and_keeps_comments_and_ordering(self):
        f = Fake("here")
        conf = "# a comment\n\nNSGlobalDomain AppleShowAllExtensions bool true\ncom.apple.dock tilesize int 36\n"
        f.files["/root/host/macos/defaults.conf"] = conf
        f.answer(["defaults", "read", "NSGlobalDomain", "AppleShowAllExtensions"], 0, "0\n")
        f.answer(["defaults", "read", "com.apple.dock", "tilesize"], 0, "48\n")
        f.answer(["defaults", "export", "com.apple.symbolichotkeys"], 0)
        f.answer(["plutil", "-convert", "xml1"], 0)
        f.files["/tmp/wk-backup-hotkeys.%d" % os.getpid()] = "<plist/>\n"

        n = backup.macos_backup(f, "/root")
        self.assertEqual(n, 2)
        new_conf = f.files["/root/host/macos/defaults.conf"]
        self.assertIn("# a comment", new_conf)
        self.assertIn("NSGlobalDomain AppleShowAllExtensions bool false", new_conf)
        self.assertIn("com.apple.dock tilesize int 48", new_conf)
        # order preserved: the comment line is still first, the dock line still last
        lines = [l for l in new_conf.splitlines() if l]
        self.assertEqual(lines[0], "# a comment")
        self.assertEqual(lines[-1], "com.apple.dock tilesize int 48")

    def test_a_value_no_longer_set_keeps_the_recorded_one(self):
        f = Fake("here")
        f.files["/root/host/macos/defaults.conf"] = "com.example.app somekey string was\n"
        f.answer(["defaults", "read", "com.example.app", "somekey"], 1, "")
        f.answer(["defaults", "export", "com.apple.symbolichotkeys"], 0)
        f.answer(["plutil", "-convert", "xml1"], 0)
        f.files["/tmp/wk-backup-hotkeys.%d" % os.getpid()] = "<plist/>\n"

        backup.macos_backup(f, "/root")
        self.assertIn("com.example.app somekey string was", f.files["/root/host/macos/defaults.conf"])

    def test_no_changes_reports_unchanged(self):
        f = Fake("here")
        f.files["/root/host/macos/defaults.conf"] = "com.example.app flag bool true\n"
        f.answer(["defaults", "read", "com.example.app", "flag"], 0, "1\n")
        f.answer(["defaults", "export", "com.apple.symbolichotkeys"], 0)
        f.answer(["plutil", "-convert", "xml1"], 0)
        hk_path = "/root/host/macos/symbolichotkeys.plist"
        f.files[hk_path] = "<plist/>\n"
        f.files["/tmp/wk-backup-hotkeys.%d" % os.getpid()] = "<plist/>\n"

        n = backup.macos_backup(f, "/root")
        self.assertEqual(n, 0)


class TestLinuxBackup(unittest.TestCase):
    def test_keeps_its_own_leading_comment_block(self):
        f = Fake("here")
        f.files["/root/host/linux/config.dconf"] = (
            "# generated by wk backup\n# do not edit by hand\n\n[org/gnome/desktop/interface]\ncolor-scheme='light'\n"
        )
        f.answer(["dconf", "dump", "/"], 0, "[org/gnome/desktop/interface]\ncolor-scheme='prefer-dark'\n")
        backup.linux_backup(f, "/root")
        out = f.files["/root/host/linux/config.dconf"]
        self.assertTrue(out.startswith("# generated by wk backup\n# do not edit by hand\n"))
        self.assertIn("color-scheme='prefer-dark'", out)

    def test_a_failed_dump_is_refused(self):
        f = Fake("here")
        f.answer(["dconf", "dump", "/"], 1, "", "dconf: no such directory")
        with self.assertRaises(Refused):
            backup.linux_backup(f, "/root")


# --- --candidates -------------------------------------------------------------

class TestCandidates(unittest.TestCase):
    def test_keeps_real_settings_drops_noise_and_known_entries(self):
        import plistlib
        f = Fake("here")
        f.answer(["defaults", "domains"], 0, "com.example.testapp")
        f.answer(["defaults", "export", "com.example.testapp", "-"], 0,
                 plistlib.dumps({
                     "AppleShowRealSetting": True,
                     "NSWindow Frame calculator": "500 200 0 0",
                     "LastCheckDate": "2024-01-01",
                     "SomeUUID": "1234-5678",
                     "SUEnableAutomaticChecks": True,
                     "AlreadyTracked": "keepme",
                     "NestedThing": {"a": 1},
                 }).decode("utf-8"))
        f.answer(["defaults", "export", "NSGlobalDomain", "-"], 0,
                 plistlib.dumps({"AnotherRealSetting": 42}).decode("utf-8"))

        results = backup.candidates(f, "com.example.testapp AlreadyTracked string keepme\n")
        self.assertIn(("com.example.testapp", "AppleShowRealSetting", True), results)
        self.assertIn(("NSGlobalDomain", "AnotherRealSetting", 42), results)
        keys = {k for _d, k, _v in results}
        for noisy in ("NSWindow Frame calculator", "LastCheckDate", "SomeUUID",
                      "SUEnableAutomaticChecks", "AlreadyTracked", "NestedThing"):
            self.assertNotIn(noisy, keys, "%r should have been filtered" % noisy)

    def test_read_only_makes_no_effect(self):
        """Every call `candidates` makes is a read (`defaults domains`/`export`): no write, install or removal."""
        f = Fake("here")
        f.answer(["defaults", "domains"], 0, "")
        backup.candidates(f, "")
        self.assertEqual([e for e in f.effects if e[0] != "run"], [])


class TestBackupMain(unittest.TestCase):
    def test_candidates_refuses_off_macos(self):
        with self.assertRaises(Refused):
            backup.main("/root", True, Fake("here"), macos=False)

    def test_dispatches_to_linux_backup_off_macos(self):
        f = Fake("here")
        f.answer(["dconf", "dump", "/"], 0, "[a]\nb=1\n")
        self.assertEqual(backup.main("/root", False, f, macos=False), 0)
        self.assertIn("/root/host/linux/config.dconf", f.files)


# --- documentation stays true -----------------------------------------------

class TestHeaderDocumentsCandidates(unittest.TestCase):
    def test_help_text_mentions_candidates(self):
        self.assertIn("wk key backup [--candidates]", decl.Decl(CMD_KEY).leading_comment(),
                      "cmd/key's help (what `wk key -h` prints) doesn't document backup --candidates")


if __name__ == "__main__":
    unittest.main()
