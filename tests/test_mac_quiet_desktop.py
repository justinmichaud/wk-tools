"""bench/mac-quiet-desktop.sh -- what is permanently true of a macOS desktop
that exists to be measured, and the one place it is written down.

A guest and a bench install are the same kind of machine for this purpose: a
window that gets looked at, nobody at the keyboard, and a number coming out at
the end. A widget that animates, a notification banner, a Setup Assistant pane
and a Spotlight scan each cost the measurement, and each used to be turned off
in one of those two places and not the other. So the settings are a table, the
appliers read the table, and this file measures the table and both callers.

The `defaults`, `launchctl` and `mdutil` calls run against stubs on PATH: what
is under test is which settings are asked for and how, not what macOS does with
them -- that is measured on a real guest by `wk vm check`.

Run: python3 -m unittest tests.test_mac_quiet_desktop -v
"""
import os
import re
import unittest

from tests.support import REPO, WkTest, bash, stub_path

QUIET = REPO / "bench" / "mac-quiet-desktop.sh"
DESKTOP = REPO / "vm" / "desktop.sh"
FIRSTBOOT = REPO / "bench" / "mac-bench-firstboot.sh"
VOLUME = REPO / "bench" / "mac-bench-volume.sh"
VM_DRIVER = REPO / "targets" / "vm.sh"

# Every call lands in one log, so a test reads what was asked for in order.
STUB = '#!/bin/sh\nprintf \'%s %s\\n\' "$(basename "$0")" "$*" >> "$WK_TEST_CALLS"\nexit 0\n'


def _rows(name):
    src = QUIET.read_text()
    body = src[src.index(f"wk_quiet_desktop_{name}() {{"):]
    body = body[body.index("<<'ROWS'") + 8:body.index("\nROWS\n")]
    return [l.split() for l in body.strip().splitlines()]


class TestTheTable(unittest.TestCase):
    def test_the_agents_that_draw_on_their_own_are_named(self):
        """chronod redraws a desktop widget on its own timer; NotificationCenter
        draws a banner over whatever is being measured. Both were measured
        stopping on a Tahoe 26.4 clone, 2026-09-05."""
        labels = [r[1] for r in _rows("agents")]
        self.assertIn("com.apple.chronod", labels)
        self.assertIn("com.apple.notificationcenterui", labels)

    def test_a_lever_that_was_disproved_is_not_carried(self):
        """Setup Assistant's MiniBuddy pane is submitted by runningboardd on
        behalf of loginwindow: `launchctl disable gui/<uid>/com.apple.mbuseragent`
        is recorded and ignored, measured on a clone that showed the pane on
        three consecutive boots with it disabled. A row that does nothing is
        machinery around a fault, and the window probe is what catches it."""
        self.assertNotIn("mbuseragent", QUIET.read_text())

    def test_every_row_is_complete(self):
        for r in _rows("rows"):
            with self.subTest(row=r):
                self.assertEqual(5, len(r), r)
                self.assertIn(r[3], ("bool", "int", "string"))

    def test_the_names_are_distinct(self):
        names = [r[0] for r in _rows("rows")] + [r[0] for r in _rows("agents")]
        self.assertEqual(len(names), len(set(names)), names)


class TestApplyingIt(WkTest):
    def _run(self, script, path_extra=("defaults", "launchctl", "mdutil",
                                       "tmutil", "pmset", "sudo")):
        calls = self.tmp / "calls"
        calls.write_text("")
        with stub_path({n: STUB for n in path_extra}) as binp:
            cp = bash(f'. {str(QUIET)!r}\n{script}\n',
                      env={"PATH": f"{binp}:/usr/bin:/bin",
                           "WK_TEST_CALLS": str(calls)})
        return cp, calls.read_text()

    def test_sourcing_it_changes_nothing(self):
        """It is streamed into a guest ahead of another script; a side effect
        on source would fire wherever it is read."""
        _, calls = self._run("true")
        self.assertEqual("", calls)

    def test_every_row_is_written(self):
        cp, calls = self._run("wk_quiet_desktop_user")
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        for name, domain, key, type_, value in _rows("rows"):
            with self.subTest(setting=name):
                self.assertIn(f"write {domain.lstrip('@')} {key} -{type_} {value}", calls)

    def test_a_per_hardware_uuid_key_is_written_that_way(self):
        """`tart clone` remints the hardware UUID, so a `@` row set in the
        golden base does not reach the clone unless it is written again."""
        _, calls = self._run("wk_quiet_desktop_user")
        host = [r for r in _rows("rows") if r[1].startswith("@")]
        self.assertTrue(host, "no per-hardware-UUID row left in the table")
        for name, domain, key, _t, _v in host:
            with self.subTest(setting=name):
                self.assertRegex(calls, rf"defaults -currentHost write {domain[1:]} {key}")

    def test_each_agent_is_disabled_and_booted_out(self):
        """`disable` is what holds across logins; `bootout` is what takes it
        away from the session that is already up."""
        _, calls = self._run("wk_quiet_desktop_user")
        for row in _rows("agents"):
            with self.subTest(agent=row[1]):
                self.assertRegex(calls, rf"launchctl disable gui/\d+/{re.escape(row[1])}")
                self.assertRegex(calls, rf"launchctl bootout gui/\d+/{re.escape(row[1])}")

    def test_the_launchd_label_is_read_rather_than_assumed(self):
        """com.apple.notificationcenterui.plist declares
        com.apple.notificationcenterui.**agent**. Disabling the filename is
        recorded by launchd as cheerfully as the real thing and the agent goes
        on running -- measured on a Tahoe 26.4 guest, with the probe reporting
        "no banner can be drawn" while NotificationCenter was up."""
        src = QUIET.read_text()
        self.assertIn("Print :Label", src)
        body = src[src.index("wk_quiet_desktop_user()"):]
        self.assertIn("_wk_qd_label", body[:body.index("\nwk_quiet_desktop_system")])

    def test_the_probe_asks_the_process_not_launchd(self):
        """The same measurement: launchd's disabled list said `off` for an agent
        that was running. What settles it is whether the process is there."""
        body = QUIET.read_text()
        probe = body[body.index("wk_quiet_desktop_probe()"):]
        self.assertIn("pgrep -x", probe)
        self.assertNotIn("print-disabled", probe)

    def test_every_agent_row_names_the_process_to_look_for(self):
        for row in _rows("agents"):
            with self.subTest(agent=row[1]):
                self.assertEqual(3, len(row), row)

    def test_another_account_is_written_as_that_account(self):
        """A bench install's first boot runs as root before anyone has logged
        in; an unqualified write would land in root's own domain."""
        _, calls = self._run("wk_quiet_desktop_user nosuchuser || true")
        self.assertIn("sudo -u nosuchuser defaults", calls)

    def test_an_account_that_does_not_exist_is_refused(self):
        cp, _ = self._run("wk_quiet_desktop_user nosuchuser; echo rc=$?")
        self.assertIn("rc=1", cp.stdout)
        self.assertIn("no such account", cp.stdout + cp.stderr)

    def test_the_system_half_refuses_without_root(self):
        """It writes /Library/Preferences and stops Spotlight; saying so beats
        a run of `defaults` that quietly does nothing."""
        cp, calls = self._run("wk_quiet_desktop_system; echo rc=$?")
        self.assertIn("rc=1", cp.stdout)
        self.assertIn("needs root", cp.stdout + cp.stderr)
        self.assertEqual("", calls)

    def test_the_probe_answers_for_every_row(self):
        cp, _ = self._run("wk_quiet_desktop_probe")
        answered = {l.split("=", 1)[0] for l in cp.stdout.splitlines() if "=" in l}
        want = {r[0] for r in _rows("rows")} | {r[0] for r in _rows("agents")}
        self.assertEqual(set(), want - answered, want - answered)

    def test_the_probe_writes_nothing(self):
        _, calls = self._run("wk_quiet_desktop_probe")
        self.assertNotIn("write", calls)
        self.assertNotIn("bootout", calls)
        # `print-disabled` is the read; `launchctl disable` is the write.
        self.assertNotRegex(calls, r"launchctl disable")


class TestBothKindsOfMeasuredMacGetIt(unittest.TestCase):
    """The whole point of the file: a setting cannot be true of a guest and not
    of a bench install."""

    def test_a_guest_is_sent_it_with_the_script_that_uses_it(self):
        """vm/desktop.sh sources nothing -- it is streamed into a guest that has
        no wk-tools on disk -- so both callers send the two files together."""
        self.assertIn("wk_quiet_desktop_user", DESKTOP.read_text())
        for caller in (VM_DRIVER, REPO / "vm" / "provision-base.sh"):
            with self.subTest(caller=caller.name):
                self.assertIn("mac-quiet-desktop.sh", caller.read_text())

    def test_the_probe_is_sent_it_too(self):
        self.assertIn("wk_quiet_desktop_probe", (REPO / "vm" / "desktop-probe.sh").read_text())
        driver = VM_DRIVER.read_text()
        body = driver[driver.index("vm_desktop_probe() {"):]
        self.assertIn("mac-quiet-desktop.sh", body[:body.index("\n}\n")])

    def test_a_bench_install_gets_the_file_and_runs_it(self):
        self.assertIn("wk-bench-quiet-desktop.sh", VOLUME.read_text(),
                      "nothing installs it into the image")
        first = FIRSTBOOT.read_text()
        self.assertIn("wk_quiet_desktop_system", first)
        self.assertIn("wk_quiet_desktop_user", first)

    def test_the_bench_install_names_the_account_being_measured(self):
        """Its first boot is root, and the account that gets measured is the
        bench user -- not root, whose desktop nobody ever looks at."""
        first = FIRSTBOOT.read_text()
        self.assertIn('wk_quiet_desktop_user "$BENCH_USER"', first)

    def test_nothing_keeps_its_own_copy_of_a_setting(self):
        """A second spelling anywhere is a setting that can drift out of the
        table and be true of one kind of measured Mac and not the other."""
        for f in (DESKTOP, FIRSTBOOT, VOLUME, REPO / "vm" / "desktop-probe.sh"):
            text = f.read_text()
            with self.subTest(file=f.name):
                for _name, domain, key, _t, _v in _rows("rows"):
                    self.assertNotIn(f"{domain.lstrip('@')} {key}", text,
                                     f"{f.name} writes {key} itself")
                self.assertNotIn("mdutil", text, f"{f.name} turns Spotlight off itself")


if __name__ == "__main__":
    unittest.main()
