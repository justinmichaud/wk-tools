"""`wk key backup` (lib/wk/backup.py) against a Fake machine: nothing here runs a real `defaults`, `dconf` or `plutil`."""

import contextlib
import io
import os
import types
import unittest
from unittest import mock

from tests.killpoints import converges
from tests.support import REAL_MACHINES, REPO
from tests.test_doctor import fake_doctor

import sys
sys.path.insert(0, str(REPO / "lib"))
from wk import backup  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Fake, Local, Result  # noqa: E402

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
    def test_the_junk_goes_and_the_settings_stay(self):
        out = backup.dconf_filter(FAKE_DUMP + "\n[org/gnome/shell]\nwelcome-dialog-last-shown-version='46.0'\n"
                                  "last-selected-power-profile='performance'\nfavorite-apps=['firefox_firefox.desktop']\n"
                                  "\n[org/gnome/shell/looking-glass]\nlooking-glass-history=['1 + 1']\n")
        for junk in ("weather", "Edmonton", "nm-applet", "2adb305e", "last-folder-path", "Ptyxis/Profiles", "491ed247", "last-used",
                     "welcome-dialog-last-shown-version", "last-selected-power-profile", "looking-glass-history"):
            self.assertNotIn(junk, out)
        for kept in ("org/gnome/desktop/interface", "color-scheme='prefer-dark'", "two-finger-scrolling-enabled=true",
                     "org/gnome/Ptyxis/Shortcuts", "copy-clipboard=", "favorite-apps="):
            self.assertIn(kept, out)


class TestAtomicUpdate(unittest.TestCase):
    def test_it_writes_only_what_differs(self):
        f = Fake("here")
        f.files["/conf/same"], f.files["/conf/old"] = "same\n", "old\n"
        self.assertEqual([False, True, True], [backup.atomic_update(f, "/conf/" + n, c, "label")
                                               for n, c in (("same", "same\n"), ("old", "new\n"), ("absent", "brand new\n"))])
        self.assertEqual(["/conf/old", "/conf/absent"], [e[1] for e in f.effects if e[0] == "write"])
        self.assertEqual((f.files["/conf/old"], f.files["/conf/absent"]), ("new\n", "brand new\n"))


class TestWritePipelineNeverTruncates(unittest.TestCase):
    """A write that cannot complete (a read-only directory fails as a full disk would) leaves the old file, byte for byte."""

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


def mac(conf, reads, hotkeys=None):
    """A Mac whose defaults.conf is `conf` and whose `defaults read <domain> <key>` answers `reads`."""
    f = Fake("here")
    f.files["/root/host/macos/defaults.conf"] = conf
    for (domain, key), (rc, out) in reads.items():
        f.answer(["defaults", "read", domain, key], rc, out)
    f.answer(["defaults", "export", "com.apple.symbolichotkeys"], 0)
    f.answer(["plutil", "-convert", "xml1"], 0)
    f.files["/tmp/wk-backup-hotkeys.%d" % os.getpid()] = "<plist/>\n"
    if hotkeys:
        f.files["/root/host/macos/symbolichotkeys.plist"] = hotkeys
    return f


class TestMacosBackup(unittest.TestCase):
    def test_refreshes_values_and_keeps_comments_ordering_and_reasons(self):
        f = mac("# a comment\n\nNSGlobalDomain AppleShowAllExtensions bool true\ncom.apple.dock tilesize int 36 the person's choice\n",
                {("NSGlobalDomain", "AppleShowAllExtensions"): (0, "0\n"), ("com.apple.dock", "tilesize"): (0, "48\n")})
        self.assertEqual(2, backup.macos_backup(f, "/root"))
        self.assertEqual(["# a comment", "NSGlobalDomain AppleShowAllExtensions bool false", "com.apple.dock tilesize int 48 the person's choice"],
                         [l for l in f.files["/root/host/macos/defaults.conf"].splitlines() if l])

    def test_a_value_no_longer_set_keeps_the_recorded_one(self):
        f = mac("com.example.app somekey string was\n", {("com.example.app", "somekey"): (1, "")})
        backup.macos_backup(f, "/root")
        self.assertIn("com.example.app somekey string was", f.files["/root/host/macos/defaults.conf"])

    def test_no_changes_reports_unchanged(self):
        f = mac("com.example.app flag bool true\n", {("com.example.app", "flag"): (0, "1\n")}, hotkeys="<plist/>\n")
        self.assertEqual(0, backup.macos_backup(f, "/root"))


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


class TestKillpointsKeyBackup(unittest.TestCase):
    """`killpoints[key backup]`: killed after any effect and re-run, a backup reaches the files an uninterrupted one writes, each whole or unchanged."""

    def world(self, macos):
        f = Fake("here")
        if macos:
            f.files["/root/host/macos/defaults.conf"] = "# c\ncom.apple.dock tilesize int 36\n"
            f.answer(["defaults", "read", "com.apple.dock", "tilesize"], 0, "48\n")
            tmp = "/tmp/wk-backup-hotkeys.%d" % os.getpid()

            def export(argv, fake):
                fake.files[tmp] = "<plist/>\n"
                return Result(0, "", "")
            f.react(["defaults", "export", "com.apple.symbolichotkeys"], export)
            f.answer(["plutil", "-convert", "xml1"], 0)
        else:
            f.files["/root/host/linux/config.dconf"] = "# head\n\n[a]\nx=1\n"
            f.answer(["dconf", "dump", "/"], 0, "[a]\nx=2\n")
        return types.SimpleNamespace(fake=f)

    def test_each_platform_converges_after_a_kill_at_every_effect(self):
        for macos in (False, True):
            def run_once(w):
                with contextlib.redirect_stderr(io.StringIO()):
                    backup.main("/root", False, w.fake, macos)
            converges(self, lambda: self.world(macos), run_once, lambda w: dict(w.fake.files))

    def test_a_dry_run_records_the_wet_runs_writes_and_makes_none(self):
        for macos in (False, True):
            with self.subTest(macos=macos):
                wet, dry = self.world(macos).fake, self.world(macos).fake
                before = dict(dry.files)
                with contextlib.redirect_stderr(io.StringIO()):
                    backup.main("/root", False, wet, macos)
                    with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
                        backup.main("/root", False, dry, macos)
                self.assertTrue([e for e in wet.effects if e[0] == "write"])
                self.assertEqual(wet.effects, dry.effects)
                self.assertEqual(before, dry.files)


class TestRpi5TuningIsBackedUp(unittest.TestCase):
    """rpi5-setup.sh cannot re-make the NUMA kernel build or the ssh key beside it; the machine-local section declares both backed-up."""

    def rows(self, name):
        doc = fake_doctor(False, env={"WK_MACHINES_DIR": str(REAL_MACHINES), "WK_IN_VM": "1", "WK_ROW_LABEL": name})
        return [r[1] for r in doc.machine_local() if r[2].startswith("backed-up")]

    def test_the_rpi5_declares_its_key_and_kernel_build(self):
        text = " ".join(self.rows("rpi5"))
        self.assertIn("host/linux/rpi5/id_ed25519", text)
        self.assertIn("~/kbuild", text)

    def test_another_machine_declares_neither(self):
        text = " ".join(self.rows("elsewhere"))
        self.assertNotIn("id_ed25519", text)
        self.assertNotIn("kbuild", text)


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
        self.assertEqual([e for e in f.effects if e[0] != "run"], [], "it only reads")


class TestBackupMain(unittest.TestCase):
    def test_candidates_refuses_off_macos(self):
        with self.assertRaises(Refused):
            backup.main("/root", True, Fake("here"), macos=False)


if __name__ == "__main__":
    unittest.main()
