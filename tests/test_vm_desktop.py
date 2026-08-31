"""A macOS guest on an empty desktop: what `wk vm check` reads, and what
vm/desktop.sh writes.

A guest is the only workspace kind with a real GPU and a window meant to be
looked at, and four things can get in front of that window -- the screen lock,
the screen saver, display sleep, and a modal panel (Setup Assistant, a Software
Update offer). An occluded window is a throttled window, so a benchmark in
there measures something else rather than failing.

The findings are exercised against captured probe output: what the probe says
about a real guest is a fact about that guest, not about this code. The probe
and the writer are checked for the two properties that make them safe to run
against somebody's live guest -- the writer kills nothing, and the probe changes
nothing.

Run: python3 -m unittest tests.test_vm_desktop -v
"""
import unittest

from tests.support import REPO, WkTest, bash

DESKTOP = REPO / "vm" / "desktop.sh"
PROBE = REPO / "vm" / "desktop-probe.sh"

SETTLED = """console_user=admin
screenlock=off
idletime=0
askforpassword=0
displaysleep=0
setupassistant_pending=
update_check=0
update_download=0
panels=
user=admin
"""

# What `wk vm check mya` actually reported on 2026-08-31, before the fix: a
# screen saver armed because the base's ByHost setting does not survive a
# clone, two Setup Assistant panes this macOS added, and one of them on screen.
AS_FOUND = """console_user=admin
screenlock=off
idletime=?
askforpassword=0
displaysleep=0
setupassistant_pending=DidSeeTrueTone DidSeeSyncSetup
update_check=?
update_download=?
panels=/System/Library/CoreServices/Setup Assistant.app/Contents/MacOS/Setup Assistant -MiniBuddyYes;
user=admin
"""

LOGIN_WINDOW = SETTLED.replace("console_user=admin", "console_user=root")


def findings(probe):
    cp = bash(f'''
set -euo pipefail
WK_ROOT={str(REPO)!r}
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/resources.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target vm
probe=$(cat <<'PROBE_EOF'
{probe}
PROBE_EOF
)
vm_desktop_findings "$probe"
''')
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return [tuple(l.split("\t")) for l in cp.stdout.splitlines()
            if len(l.split("\t")) == 3]


class TestTheFindings(WkTest):
    def test_a_settled_guest_is_all_ok(self):
        states = {f[0] for f in findings(SETTLED)}
        self.assertEqual(states - {"note"}, {"ok"}, findings(SETTLED))

    def test_the_guest_as_it_was_found_reports_each_fault(self):
        f = findings(AS_FOUND)
        wrong = [x[1] for x in f if x[0] == "wrong"]
        self.assertTrue(any("screen saver" in w for w in wrong), wrong)
        self.assertTrue(any("Setup Assistant" in w for w in wrong), wrong)
        self.assertTrue(any("Software Update" in w for w in wrong), wrong)
        self.assertTrue(any("on the desktop right now" in w for w in wrong), wrong)

    def test_a_panel_on_screen_is_remedied_by_a_restart_not_a_kill(self):
        """Killing Setup Assistant ends the desktop session with it -- that is
        how a guest ended up at its login window while this was being written."""
        f = [x for x in findings(AS_FOUND) if "on the desktop right now" in x[1]]
        self.assertTrue(f)
        self.assertIn("wk vm stop", f[0][2])

    def test_a_login_window_is_reported_as_no_desktop_at_all(self):
        f = [x for x in findings(LOGIN_WINDOW) if x[0] == "wrong"]
        self.assertTrue(any("nobody is logged in" in x[1] for x in f), f)

    def test_the_login_is_stated_once_and_read_from_the_driver(self):
        """The password is one fact in one place (targets/vm.sh), and it is
        said where a person is about to look at the guest's window."""
        note = [x for x in findings(SETTLED) if x[0] == "note" and "logs in as" in x[1]]
        self.assertTrue(note, findings(SETTLED))
        self.assertIn("admin / 1", note[0][1])


class TestTheWriterIsSafeToRunOnALiveGuest(WkTest):
    def test_it_kills_nothing(self):
        text = DESKTOP.read_text()
        for bad in ("pkill", "killall", "kill -"):
            self.assertNotIn(bad, text,
                             f"vm/desktop.sh runs {bad}, which can take the "
                             f"desktop session with it")

    def test_it_never_offers_a_password_it_has_not_checked(self):
        """`sysadminctl -screenLock` takes the account's current password;
        offering a wrong one was observed to leave the lock armed."""
        text = DESKTOP.read_text()
        auth = text.index("dscl . -authonly")
        lock = text.index("sysadminctl -screenLock off")
        self.assertLess(auth, lock,
                        "the password is offered to sysadminctl before it is checked")

    def test_it_writes_the_per_clone_settings_itself(self):
        """`defaults -currentHost` is keyed by hardware UUID and `tart clone`
        changes it, so these cannot live only in the golden base."""
        self.assertIn("-currentHost write com.apple.screensaver idleTime",
                      DESKTOP.read_text())

    def test_both_callers_run_this_file_rather_than_a_copy(self):
        base = (REPO / "vm" / "provision-base.sh").read_text()
        driver = (REPO / "targets" / "vm.sh").read_text()
        self.assertIn("vm/desktop.sh", base,
                      "vm/provision-base.sh no longer runs the one desktop script")
        self.assertIn("vm/desktop.sh", driver,
                      "t_start no longer settles the desktop on every start")
        for text, who in ((base, "vm/provision-base.sh"), (driver, "targets/vm.sh")):
            self.assertNotIn("DidSeeSiriSetup", text,
                             f"{who} carries its own copy of the Setup Assistant keys")


class TestTheProbeChangesNothing(WkTest):
    def test_it_only_reads(self):
        text = PROBE.read_text()
        for bad in ("defaults write", "defaults -currentHost write", "pkill",
                    "killall", "softwareupdate --schedule", "sysadminctl -screenLock off",
                    "pmset -a"):
            self.assertNotIn(bad, text, f"vm/desktop-probe.sh runs {bad}")

    def test_it_sources_nothing(self):
        """It must answer about a guest whose wk-tools copy is older than it."""
        text = PROBE.read_text()
        for bad in ("lib/common.sh", "$WK_ROOT", "$WK_TOOLS_DIR"):
            self.assertNotIn(bad, text, f"vm/desktop-probe.sh reaches for {bad}")

    def test_it_answers_about_every_key_the_findings_read(self):
        """Every `_v <key>` in vm_desktop_findings is a key the probe prints,
        or the report is silently reading an empty string."""
        import re
        probe_keys = set(re.findall(r"printf '([a-z_]+)=", PROBE.read_text()))
        driver = (REPO / "targets" / "vm.sh").read_text()
        body = driver[driver.index("vm_desktop_findings() {"):]
        body = body[:body.index("\n}\n")]
        read = set(re.findall(r'_v ([a-z_]+)\)', body))
        self.assertTrue(read, "the findings read no keys at all")
        self.assertEqual(read - probe_keys, set(),
                         f"the findings read keys the probe never prints: {read - probe_keys}")
