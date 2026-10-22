"""A macOS guest's desktop and load, as `wk doctor <guest>` and every start read them from captured probe output;
the writer (vm/desktop.sh) kills nothing and the probes change nothing."""
import contextlib
import inspect
import io
import os
import re
import sys
import types
import unittest
from unittest import mock

from tests.fakes import BenchHere
from tests.support import REPO, live_selected, repo_files

sys.path.insert(0, str(REPO / "lib"))
from wk import doctor, guest, places  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402
from wk.store import Store  # noqa: E402

DESKTOP = REPO / "vm" / "desktop.sh"
PROBE = REPO / "vm" / "desktop-probe.sh"
REBUILD = guest.BASE_BUILD + " --rebuild"

# A real reading of a settled guest with every table row in force.
PYOBJC_VERSION = re.search(
    r'WK_PYOBJC_VERSION="\$\{WK_PYOBJC_VERSION:-([^}]*)\}"',
    (REPO / "bench" / "mac-pyobjc.sh").read_text()).group(1)

SETTLED = f"""console_user=admin
pyobjc={PYOBJC_VERSION}
screenlock=off
widgets_desktop=1
widgets_stage=1
reduce_motion=1
reduce_transparency=1
appnap=1
window_anim=0
askforpassword=0
askforpassworddelay=0
idletime=0
desktop_icons=0
dock_launchanim=0
dock_recents=0
crash_dialog=none
quarantine=0
timemachine_offer=1
personalised_ads=0
widgets_agent=off
notifications=off
notification_daemon=off
spotlight_menu=off
siri=off
siri_knowledge=off
siri_inference=off
suggestions=off
spotlight_suggestions=off
knowledge=off
proactive=off
photo_analysis=off
media_analysis=off
icloud_drive=off
icloud_photos=off
music_library=off
screentime=off
usage_tracking=off
experiments=off
sharing=off
tips=off
spotlight_content=running
softwareupdate=running
softwareupdate_helper=running
malware_scan=absent
malware_service=absent
malware_daemon=running
timemachine=running
timemachine_helper=running
analytics_daemon=running
analytics_helper=running
diagnostics=absent
crash_reporter=running
hang_sampler=running
power_records=absent
process_stats=running
icloud=running
icloud_defaults=running
downloads=running
findmy=running
experiments_system=running
power_displaysleep=0
power_disksleep=0
power_sleep=0
power_disablesleep=1
power_lowpowermode=
power_highpowermode=
spotlight=Indexing disabled.
analytics=0
power_source=AC Power
cpu_speed_limit=
setupassistant_pending=
update_check=0
update_download=0
update_download_system=0
update_autoinstall_system=0
setupassistant_seen_product=26.4
os_product=26.4
frontapp=com.apple.Finder
windows=Notification Center:21:1024x768@0,0;Dock:20:1024x768@0,0;Terminal:0:863x499@40,51;
securityagent=down
user=admin
"""

# A real reading of a clone behind Setup Assistant with the screen saver armed; a key it lacks is unknown, not settled.
AS_FOUND = """console_user=admin
screenlock=off
widgets_desktop=?
widgets_stage=?
reduce_motion=?
reduce_transparency=?
appnap=?
askforpassword=0
askforpassworddelay=0
idletime=?
widgets_agent=on
notifications=on
spotlight=Indexing enabled.
displaysleep=0
setupassistant_pending=DidSeeTrueTone DidSeeSyncSetup
update_check=?
update_download=?
windows=Setup Assistant:0:800x600;Setup Assistant:-1:1417x805;Notification Center:21:1417x805;Terminal:0:863x499;
securityagent=down
user=admin
"""

LOGIN_WINDOW = SETTLED.replace("console_user=admin", "console_user=root")

# A base that never turned Software Update off, and a Setup Assistant that has not seen this macOS.
UPDATE_ON = (SETTLED
             .replace("update_download_system=0", "update_download_system=1")
             .replace("update_autoinstall_system=0", "update_autoinstall_system=1")
             .replace("setupassistant_seen_product=26.4",
                      "setupassistant_seen_product=26.3"))


def readings(probe):
    """What a start and `wk doctor <guest>` make of a captured probe: this tree's libraries run for real."""
    return guest.desktop_readings(str(REPO), BenchHere(), types.SimpleNamespace(run=lambda argv, input=None: Result(0, probe)))


def rows(probe):
    return doctor.desktop_rows(readings(probe), "demo")


class TestTheDesktopRows(unittest.TestCase):
    def test_a_settled_guest_misses_nothing(self):
        self.assertEqual([], [r for r in rows(SETTLED) if r[0] == doctor.MISS], rows(SETTLED))

    def test_each_fault_is_a_miss(self):
        for probe, what in ([(AS_FOUND, w) for w in ("screen saver", "Setup Assistant will put a modal pane", "nothing wk runs put it there")]
                            + [(UPDATE_ON, w) for w in ("install themselves", "download themselves", "what is new in macOS")]
                            + [(LOGIN_WINDOW, "nobody is logged in")]):
            with self.subTest(what):
                self.assertTrue([r for r in rows(probe) if r[0] == doctor.MISS and what in r[1]], rows(probe))

    def test_what_the_guest_did_not_say_is_unknown_not_ok(self):
        self.assertEqual([doctor.UNK], [r[0] for r in rows(AS_FOUND) if r[1].startswith("Software Update")][:1])
        blind = "\n".join("windows=?" if l.startswith("windows=") else l for l in SETTLED.splitlines())
        self.assertEqual([doctor.UNK], [r[0] for r in rows(blind) if "window server" in r[1]])

    def test_a_pane_over_the_window_is_named_with_its_size_and_cleared_on_the_base(self):
        (named,) = [r for r in rows(AS_FOUND) if "nothing wk runs put it there" in r[1]]
        self.assertIn("Setup Assistant:0:800x600", named[1])
        self.assertNotIn("Notification Center", named[1])
        self.assertIn(guest.BASE_BUILD, named[2])

    def test_only_what_no_clone_can_clear_blocks_a_start(self):
        self.assertEqual([], guest.blockers(readings(SETTLED)))
        self.assertEqual([], guest.blockers(readings(SETTLED.replace("pyobjc=" + PYOBJC_VERSION, "pyobjc=9.0"))))
        self.assertEqual(1, len(guest.blockers(readings(LOGIN_WINDOW))))

    def test_the_probe_asks_only_about_keys_the_settle_writes(self):
        asked = set(re.findall(r"DidSee[A-Za-z0-9]+", PROBE.read_text()))
        self.assertEqual(set(), asked - set(re.findall(r"DidSee[A-Za-z0-9]+", DESKTOP.read_text())))


class TestTheScriptsAreSafeOnALiveGuest(unittest.TestCase):
    def test_the_writer_kills_nothing_and_the_probes_only_read_and_source_nothing(self):
        for bad in ("pkill", "killall", "kill -"):
            self.assertNotIn(bad, DESKTOP.read_text())
        for bad in ("defaults write", "defaults -currentHost write", "pkill", "killall", "softwareupdate --schedule off",
                    "softwareupdate --schedule on", "sysadminctl -screenLock off", "pmset -a"):
            self.assertNotIn(bad, PROBE.read_text())
        for bad in ("pkill", "killall", "kill -", "defaults write", "sudo"):
            self.assertNotIn(bad, (REPO / "vm" / "load-probe.sh").read_text())
        for rel in ("vm/desktop-probe.sh", "vm/load-probe.sh"):
            for bad in ("lib/common.sh", "$WK_ROOT", "$WK_TOOLS_DIR"):
                self.assertNotIn(bad, (REPO / rel).read_text(), rel)

    def test_it_answers_about_every_key_the_findings_read(self):
        quiet = (REPO / "bench" / "mac-quiet-desktop.sh").read_text()
        table = (REPO / "bench" / "quiet" / "macos.tsv").read_text()
        keys = set(re.findall(r"printf '([a-z_]+)=", PROBE.read_text() + (REPO / "bench" / "mac-window-probe.sh").read_text()))
        keys |= set(re.findall(r"^[a-z]+\t([a-z_]+)\t", table, re.M)) | set(re.findall(r"printf '([a-z_]+)=", quiet))
        read = set(re.findall(r'v\("([a-z_]+)"', inspect.getsource(doctor.desktop_rows))) | {row[0] for row in doctor.DESKTOP}
        read -= {"pin", "uninvited", "quiet"}   # desktop_readings' own, not the probe's
        self.assertTrue(read)
        self.assertEqual(set(), read - keys)


def _load_sample(shells=40, free_pct=6, swap_used="4096.00M"):
    """A guest a fortnight up with an editor attached."""
    rows = ["proc=24880 /sbin/launchd", "proc=1048576 /System/Library/Frameworks/WebKit.framework/jsc",
            "proc=512000 /Users/admin/.zed_server/stable-0.1/zed-remote-server",
            "proc=221000 /Users/admin/.local/share/claude/versions/2.0.1/claude",
            "proc=61000 /usr/sbin/sshd-session", "proc=61000 /usr/sbin/sshd-session"] + ["proc=8192 /bin/zsh"] * shells
    return "\n".join(["mem_total_mb=32768", "mem_free_pct=%d" % free_pct,
                      "swapusage=total = 8192.00M  used = %s  free = 4096.00M  (encrypted)" % swap_used] + rows) + "\n"


IDLE_SAMPLE = _load_sample(shells=2, free_pct=71, swap_used="0.00M")


class TestWhatIsResidentInThere(unittest.TestCase):
    def test_an_accumulation_low_memory_and_swap_are_reported_with_the_culprits(self):
        f = doctor.load_rows(_load_sample(), "demo")
        for state, what in ((doctor.MISS, "40 shells"), (doctor.MISS, "editor remote server"), (doctor.MISS, "6%"), (doctor.MISS, "jsc"),
                            (doctor.NOTE, "4096 MB of swap")):
            with self.subTest(what):
                self.assertTrue([x for x in f if x[0] == state and what in x[1]], f)

    def test_an_idle_guest_is_all_ok(self):
        f = doctor.load_rows(IDLE_SAMPLE, "demo")
        self.assertEqual({x[0] for x in f}, {doctor.OK}, f)
        self.assertTrue(any("2 shells resident" in x[1] for x in f))

    def test_nothing_wk_runs_in_a_guest_leaves_a_shell_behind(self):
        with mock.patch.object(places.Vm, "tart", lambda s: "/t/tart"):
            argv = places.Vm("vm", str(REPO), {}, Fake()).guest_of("wk-demo").argv("true")
        self.assertEqual(["/t/tart", "exec"], argv[:2])
        self.assertNotIn("ControlPersist", " ".join(argv))


class GuestAt(BenchHere):
    """This host with one guest behind tart exec, answering each probe streamed in with the capture it is given."""

    def __init__(self, desktop, load=None):
        super().__init__()
        self.desktop, self.load, self.inputs = desktop, load, []
        self.react(["/t/tart", "exec"], self._guest)

    def run(self, argv, input=None, timeout=None):
        self.last = input or ""
        return super().run(argv, input=input, timeout=timeout)

    def _guest(self, argv, _):
        if argv[-1] != "bash -s":
            return Result(0)
        if "mem_total_mb" in self.last:
            return Result(0, self.load) if self.load else Result(255, "", "Connection closed")
        return Result(0, self.desktop) if self.desktop else Result(255, "", "Connection closed")


class ReportTest(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=True),
                  mock.patch.object(places.Vm, "tart", lambda s: "/t/tart"), mock.patch.dict(os.environ, {}, clear=False)):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("WK_DRY_RUN", None)
        self.env = {"HOME": "/h", "WK_STORE": "/st", "WK_VM_STORE": "/vs", "XDG_STATE_HOME": "/h/st"}

    def vm(self, here):
        return places.Registry(str(REPO), env=self.env, machine=here).load("vm")

    def report(self, sample):
        """(started, stdout, stderr) of the start's last step against a guest answering `sample`."""
        vm = self.vm(GuestAt(sample))
        g = guest.Guest(guest.Host(vm, FakeClock()), "demo", vm.guest_of("wk-demo"))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                ok = g.report_desktop()
            except Refused:
                ok = False
        return ok, out.getvalue(), err.getvalue()


class TestTheStartReport(ReportTest):
    def test_every_finding_reaches_the_console_with_its_remedy(self):
        _, _, err = self.report(AS_FOUND)
        for what in ("the screen saver is disarmed", "Setup Assistant", "nothing wk runs put it there", "wk doctor demo", REBUILD):
            self.assertIn(what, err)

    def test_a_settled_guest_is_handed_over_and_still_gets_the_report_on_stderr(self):
        ok, out, err = self.report(SETTLED)
        self.assertTrue(ok, err)
        self.assertEqual("", out, "a start's stdout is the guest's address")
        self.assertIn("screen lock off", err)
        self.assertIn("com.apple.Finder has the focus", err)

    def test_a_guest_that_does_not_answer_does_not_fail_the_start(self):
        self.assertTrue(self.report(None)[0])

    def test_a_guest_behind_a_pane_or_with_nobody_at_the_window_is_refused(self):
        for sample, why in ((AS_FOUND, "Setup Assistant"), (LOGIN_WINDOW, "nobody is logged in")):
            ok, _, err = self.report(sample)
            self.assertFalse(ok)
            self.assertIn("not usable", err)
            self.assertIn(why, err)
            self.assertIn(REBUILD, err)

    def test_the_refusal_is_crossed_by_a_variable_that_says_so(self):
        self.env["WK_VM_FORCE"] = "1"
        ok, _, err = self.report(AS_FOUND)
        self.assertTrue(ok)
        self.assertIn("WK_VM_FORCE=1", err)

    def test_the_report_follows_the_settle(self):
        steps = [s[0] for s in guest.STEPS]
        self.assertLess(steps.index("settle_desktop"), steps.index("report_desktop"))

    def test_one_renderer_for_every_report(self):
        self.assertEqual([REPO / "lib" / "common.sh"],
                         [f for f in sorted(repo_files()) if f.suffix != ".py" and "render_findings() {" in f.read_text(errors="replace")])


class TestTheDoctorOfAGuest(ReportTest):
    def rows(self, desktop_sample, load=None):
        here = GuestAt(desktop_sample, load)
        here.answer(["/t/tart", "list"], out='[{"Name": "wk-demo", "Source": "local", "State": "running"}]')
        here.answer(["/t/tart", "ip"], out="10.0.0.2\n")
        return doctor.guest_rows(self.vm(here), "demo")

    def test_a_good_guest_has_all_three_reports(self):
        text = "\n".join(r[1] for r in self.rows(SETTLED, IDLE_SAMPLE))
        for what in ("golden base", "screen lock off", "shells resident"):
            self.assertIn(what, text)

    def test_a_guest_out_of_memory_misses_and_names_what_stayed(self):
        misses = [r for r in self.rows(SETTLED, _load_sample()) if r[0] == doctor.MISS]
        self.assertTrue([r for r in misses if "shells are resident" in r[1] and "editor remote server" in r[1]])
        self.assertTrue([r for r in misses if "macOS calls that pressure" in r[1]])
        self.assertTrue(all("<name>" not in r[2] for r in misses), "a remedy names the guest it is about")

    def test_a_guest_that_will_not_say_what_is_resident_says_so(self):
        self.assertTrue([r for r in self.rows(SETTLED) if r[0] == doctor.UNK and "did not answer the load probe" in r[1]])


class TestTheLiveDesktop(unittest.TestCase):
    wk_tier = "live"

    def test_vm_desktop(self):
        if not live_selected() or sys.platform != "darwin":
            self.skipTest("live tier not selected, or not a macOS host")
        vm = places.Registry(str(REPO)).load("vm")
        up = [n for n, state in vm.list() if state == "running"] if vm.tart() else []
        if not up:
            self.skipTest("no macOS guest is running on this host")
        rd = guest.desktop_readings(vm.root, vm.machine, vm.guest(up[0]))
        self.assertEqual([], guest.blockers(rd))
        self.assertEqual("off", rd["screenlock"])


if __name__ == "__main__":
    unittest.main()
