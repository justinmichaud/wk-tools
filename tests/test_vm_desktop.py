"""A macOS guest fit to look at and to measure in: what `wk doctor <guest>` and every start read of its desktop, what
vm/desktop.sh writes, and what accumulates in a guest that stays up.

An occluded window is a throttled window, so a benchmark behind the screen lock, the screen saver, display sleep or a
modal pane measures something else rather than failing. The findings are driven from captured probe output: what the
probe says about a real guest is a fact about that guest, not about this code. The probe and the writer are held to
the two properties that make them safe on somebody's live guest -- the writer kills nothing, the probe changes nothing.

Run: python3 tests/run.py --unit -k test_vm_desktop
"""
import contextlib
import functools
import inspect
import io
import os
import re
import sys
import unittest
from unittest import mock

from tests.support import REPO, assert_guest_start_converges, live_selected, repo_files

sys.path.insert(0, str(REPO / "lib"))
from wk import doctor, guest, targets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Local, Result  # noqa: E402
from wk.store import Store  # noqa: E402

DESKTOP = REPO / "vm" / "desktop.sh"
PROBE = REPO / "vm" / "desktop-probe.sh"
REBUILD = guest.BASE_BUILD + " --rebuild"

# A real reading of a settled guest with every table row in force. The pyobjc pin
# is read from bench/mac-pyobjc.sh, so a re-pin changes no behaviour here.
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

# A real reading of a clone behind Setup Assistant, with the screen saver armed
# (a base's ByHost setting does not survive a clone). It carries only the keys an
# older probe printed: a guest that answers nothing about a row is unknown, never
# settled.
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

# A guest whose base never turned Software Update off: an update that downloads
# itself, macOS updates set to install themselves -- which reboots the guest,
# mid-build if that is when one lands -- and a Setup Assistant that has not seen
# this macOS, so Buddy shows its "what is new" pane at login.
UPDATE_ON = (SETTLED
             .replace("update_download_system=0", "update_download_system=1")
             .replace("update_autoinstall_system=0", "update_autoinstall_system=1")
             .replace("setupassistant_seen_product=26.4",
                      "setupassistant_seen_product=26.3"))



@functools.lru_cache(maxsize=None)
def _bench(argv):
    return Local().run(list(argv))


class Here(Fake):
    """This host: the bench libraries run for real, and nothing else answers."""

    def __init__(self):
        super().__init__("here")
        self.react(["bash", "-c"], lambda a, f: _bench(tuple(a)))


def desktop(probe):
    return guest.Desktop(str(REPO), Here(), probe)


def findings(probe):
    return [tuple(l.split("\t")) for l in desktop(probe).findings().splitlines()]


class TestTheFindings(unittest.TestCase):
    def test_a_settled_guest_is_all_ok(self):
        self.assertEqual({f[0] for f in findings(SETTLED)} - {"note"}, {"ok"}, findings(SETTLED))

    def test_the_guest_as_it_was_found_reports_each_fault(self):
        wrong = [x[1] for x in findings(AS_FOUND) if x[0] == "wrong"]
        self.assertTrue(any("screen saver" in w for w in wrong), wrong)
        self.assertTrue(any("Setup Assistant will put a modal pane" in w for w in wrong), wrong)
        self.assertTrue(any("nothing wk runs put it there" in w for w in wrong), wrong)

    def test_a_guest_that_says_nothing_about_software_update_is_not_called_ok(self):
        f = [x for x in findings(AS_FOUND) if "Software Update" in x[1]]
        self.assertTrue(f)
        self.assertNotIn("ok", [x[0] for x in f], f)
        self.assertTrue(any("wk start" in x[2] for x in f), f)

    def test_an_update_that_reboots_or_downloads_itself_is_wrong(self):
        """The per-user domain is what System Settings shows; softwareupdated obeys /Library/Preferences."""
        f = findings(UPDATE_ON)
        wrong = [x[1] for x in f if x[0] == "wrong"]
        for what in ("install themselves", "download themselves", "what is new in macOS"):
            self.assertTrue(any(what in w for w in wrong), (what, wrong))
        self.assertTrue(all(REBUILD in x[2] for x in f if "install themselves" in x[1]))

    def test_the_check_flag_no_guest_can_set_is_not_judged(self):
        self.assertNotIn("update_check_system", PROBE.read_text())
        self.assertNotIn("softwareupdate --schedule", PROBE.read_text())
        for probe in (SETTLED, AS_FOUND, UPDATE_ON):
            self.assertFalse([x for x in findings(probe) if "scheduled update check" in x[1]])

    def test_a_daemon_is_not_a_covered_window(self):
        f = findings(SETTLED.replace("frontapp=com.apple.Finder", "frontapp=com.apple.Terminal"))
        self.assertEqual([], [x for x in f if x[0] == "wrong"], f)

    def test_a_pane_over_the_window_is_named_once_with_its_size_and_the_furniture_is_not(self):
        named = [x for x in findings(AS_FOUND) if "nothing wk runs put it there" in x[1]]
        self.assertTrue(named)
        self.assertIn("Setup Assistant:0:800x600", named[0][1])
        self.assertNotIn("Notification Center", named[0][1])
        self.assertIn(guest.BASE_BUILD, named[0][2], "a pane on screen is cleared on the base, not by a restart")
        self.assertNotIn("wk stop", named[0][2])

    def test_a_screen_nobody_could_ask_about_is_not_reported_as_clean(self):
        blind = "\n".join("windows=?" if l.startswith("windows=") else l for l in SETTLED.splitlines())
        self.assertEqual(["note"], [x[0] for x in findings(blind) if "window server" in x[1]])

    def test_the_probe_asks_only_about_keys_the_settle_writes(self):
        asked = set(re.findall(r"DidSee[A-Za-z0-9]+", PROBE.read_text()))
        self.assertEqual(set(), asked - set(re.findall(r"DidSee[A-Za-z0-9]+", DESKTOP.read_text())))

    def test_nothing_claims_to_stop_the_update_pane(self):
        for f in (DESKTOP, PROBE, REPO / "bench" / "mac-quiet-desktop.sh"):
            with self.subTest(file=f.name):
                self.assertNotIn("DidSeeAutoUpdatePrompt", f.read_text())
                self.assertNotIn("mbuseragent", f.read_text().replace("has no launchd label", ""))

    def test_a_login_window_is_no_desktop_at_all(self):
        self.assertTrue([x for x in findings(LOGIN_WINDOW) if x[0] == "wrong" and "nobody is logged in" in x[1]])

    def test_the_login_names_the_account_and_not_its_password(self):
        note = [x for x in findings(SETTLED) if "logs in as" in x[1]]
        self.assertEqual("note", note[0][0])
        self.assertNotIn("admin / ", note[0][1])
        self.assertIn("password", note[0][2])

    def test_every_finding_is_one_line_of_three_fields(self):
        for probe in (SETTLED, AS_FOUND, UPDATE_ON, LOGIN_WINDOW):
            for line in desktop(probe).findings().splitlines():
                self.assertEqual(3, len(line.split("\t")), line)
        for f in load_findings(_load_sample()) + load_findings(IDLE_SAMPLE):
            self.assertEqual(3, len(f), f)


class TestWhatIsInFrontOfTheDesktop(unittest.TestCase):
    """The faults a guest is refused over: a guest with the wrong pyobjc still builds; one behind a pane does not."""

    def test_a_settled_guest_has_nothing_in_front(self):
        self.assertEqual([], desktop(SETTLED).blockers())

    def test_a_pane_and_an_empty_login_window_each_block(self):
        self.assertTrue([b for b in desktop(AS_FOUND).blockers() if "Setup Assistant:0:800x600" in b])
        self.assertTrue([b for b in desktop(LOGIN_WINDOW).blockers() if "nobody is logged in" in b])

    def test_the_wrong_pyobjc_does_not_block(self):
        self.assertEqual([], desktop(SETTLED.replace("pyobjc=" + PYOBJC_VERSION, "pyobjc=9.0")).blockers())


class TestTheWriterIsSafeToRunOnALiveGuest(unittest.TestCase):
    def test_it_kills_nothing(self):
        for bad in ("pkill", "killall", "kill -"):
            self.assertNotIn(bad, DESKTOP.read_text())

    def test_it_never_offers_a_password_it_has_not_checked(self):
        text = DESKTOP.read_text()
        self.assertLess(text.index("dscl . -authonly"), text.index("sysadminctl -screenLock off"))

    def test_the_per_clone_settings_are_its_own(self):
        """`defaults -currentHost` is keyed by the hardware UUID `tart clone` changes."""
        self.assertIn("setting\tidletime\t@com.apple.screensaver\tidleTime", (REPO / "bench" / "quiet" / "macos.tsv").read_text())

    def test_the_base_and_every_start_run_this_file_and_the_password_rides_in_the_script(self):
        self.assertIn("vm/desktop.sh", (REPO / "vm" / "provision-base.sh").read_text())
        settle = inspect.getsource(guest.Guest.settle_desktop)
        self.assertIn('"vm/desktop.sh"', settle)
        self.assertIn('"WK_VM_PASSWORD=%s\\n"', settle, "the password travels as an argument, in `ps` on both machines")
        self.assertNotIn("DidSeeSiriSetup", (REPO / "vm" / "provision-base.sh").read_text())


class TestTheProbeChangesNothing(unittest.TestCase):
    def test_it_only_reads(self):
        for bad in ("defaults write", "defaults -currentHost write", "pkill", "killall", "softwareupdate --schedule off",
                    "softwareupdate --schedule on", "sysadminctl -screenLock off", "pmset -a"):
            self.assertNotIn(bad, PROBE.read_text())

    def test_it_sources_nothing(self):
        for rel in ("vm/desktop-probe.sh", "vm/load-probe.sh"):
            for bad in ("lib/common.sh", "$WK_ROOT", "$WK_TOOLS_DIR"):
                self.assertNotIn(bad, (REPO / rel).read_text(), rel)

    def test_it_answers_about_every_key_the_findings_read(self):
        quiet = (REPO / "bench" / "mac-quiet-desktop.sh").read_text()
        table = (REPO / "bench" / "quiet" / "macos.tsv").read_text()
        keys = set(re.findall(r"printf '([a-z_]+)=", PROBE.read_text() + (REPO / "bench" / "mac-window-probe.sh").read_text()))
        keys |= set(re.findall(r"^[a-z]+\t([a-z_]+)\t", table, re.M)) | set(re.findall(r"printf '([a-z_]+)=", quiet))
        read = set(re.findall(r'v\("([a-z_]+)"\)', inspect.getsource(guest.Desktop)))
        self.assertTrue(read)
        self.assertEqual(set(), read - keys)

    def test_the_load_probe_only_reads(self):
        for bad in ("pkill", "killall", "kill -", "defaults write", "sudo"):
            self.assertNotIn(bad, (REPO / "vm" / "load-probe.sh").read_text())


def _load_sample(shells=40, free_pct=6, swap_used="4096.00M"):
    """A guest a fortnight up with an editor attached, as `ps -Ao rss=,comm=` and macOS's own readings put it."""
    rows = ["proc=24880 /sbin/launchd", "proc=1048576 /System/Library/Frameworks/WebKit.framework/jsc",
            "proc=512000 /Users/admin/.zed_server/stable-0.1/zed-remote-server",
            "proc=221000 /Users/admin/.local/share/claude/versions/2.0.1/claude",
            "proc=61000 /usr/sbin/sshd-session", "proc=61000 /usr/sbin/sshd-session"] + ["proc=8192 /bin/zsh"] * shells
    return "\n".join(["mem_total_mb=32768", "mem_free_pct=%d" % free_pct,
                      "swapusage=total = 8192.00M  used = %s  free = 4096.00M  (encrypted)" % swap_used] + rows) + "\n"


IDLE_SAMPLE = _load_sample(shells=2, free_pct=71, swap_used="0.00M")


def load_findings(probe, env=None):
    return [tuple(l.split("\t")) for l in guest.load_findings(probe, env or {}).splitlines()]


class TestWhatIsResidentInThere(unittest.TestCase):
    def test_an_accumulation_is_reported_and_the_culprit_named(self):
        shells = [x for x in load_findings(_load_sample()) if x[0] == "wrong" and "shells are resident" in x[1]]
        self.assertIn("40 shells", shells[0][1])
        self.assertIn("editor remote server", shells[0][1])
        self.assertIn("wk stop", shells[0][2])

    def test_the_editor_remote_server_is_named_as_outliving_its_window(self):
        f = [x for x in load_findings(_load_sample()) if "editor remote server" in x[1] and x[0] == "note"]
        self.assertIn("outlives the editor window", f[0][2])

    def test_low_memory_is_wrong_and_names_the_biggest_processes(self):
        f = [x for x in load_findings(_load_sample()) if x[0] == "wrong" and "free" in x[1]]
        self.assertIn("6%", f[0][1])
        self.assertIn("jsc", f[0][1])

    def test_swap_in_a_fixed_allocation_is_reported(self):
        self.assertIn("4096 MB of swap", [x for x in load_findings(_load_sample()) if "swap" in x[1]][0][1])

    def test_an_idle_guest_is_all_ok(self):
        f = load_findings(IDLE_SAMPLE)
        self.assertEqual({x[0] for x in f} - {"note"}, {"ok"}, f)
        self.assertTrue(any("2 shells resident" in x[1] for x in f))

    def test_the_thresholds_are_the_documented_overrides(self):
        f = load_findings(_load_sample(), {"WK_VM_SHELLS_WARN": "100", "WK_VM_MEM_FREE_WARN_PCT": "1"})
        self.assertFalse([x for x in f if "shells are resident" in x[1] or "macOS calls that pressure" in x[1]], f)

    def test_nothing_wk_runs_in_a_guest_leaves_a_shell_behind(self):
        """No ControlPersist master holds a session open, and `wk enter` execs its ssh rather than backgrounding it."""
        vm = targets.Vm("vm", str(REPO), {}, Fake())
        self.assertNotIn("ControlPersist", " ".join(vm._guest_ssh_opts()))
        self.assertIn("exec_into(argv, cwd)", (REPO / "cmd" / "enter").read_text())


class TestARehearsalGuestIsNotAlsoAWorkspace(unittest.TestCase):
    """A guest carrying /etc/wk-image stands in for a machine in bench mode, and every start writes the claim back."""

    def _write_marker(self, bench):
        g = Fake("guest")
        g.answer(["test", "-f", "/etc/wk-image"], rc=0 if bench else 1)
        vm = targets.Vm("vm", str(REPO), {"WK_VM_USER": "admin"}, Fake("here"))
        self.assertTrue(vm.write_marker("demo", g))
        return g

    def test_a_benchmark_install_has_the_claim_taken_off(self):
        g = self._write_marker(bench=True)
        self.assertIn(("remove", "/Users/admin/.wk-workspace"), g.effects)

    def test_an_ordinary_workspace_guest_still_gets_it(self):
        text = self._write_marker(bench=False).files["/Users/admin/.wk-workspace"]
        self.assertIn("name=demo", text)


class GuestAt(Here):
    """This host with one guest behind ssh, answering each probe streamed in with the capture it is given."""

    def __init__(self, desktop, load=None):
        super().__init__()
        self.desktop, self.load, self.inputs = desktop, load, []
        self.react(["ssh"], self._guest)

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
                  mock.patch.object(targets.Vm, "tart", lambda s: "/t/tart"), mock.patch.dict(os.environ, {}, clear=False)):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("WK_DRY_RUN", None)
        self.env = {"HOME": "/h", "WK_STORE": "/st", "WK_VM_STORE": "/vs", "XDG_STATE_HOME": "/h/st"}

    def vm(self, here):
        return targets.Registry(str(REPO), env=self.env, machine=here).load("vm")

    def report(self, sample):
        """(started, stdout, stderr) of the start's last step against a guest answering `sample`."""
        vm = self.vm(GuestAt(sample))
        g = guest.Guest(guest.Host(vm, FakeClock()), "demo", "10.0.0.2")
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

    def test_both_arms_of_a_start_report_and_after_the_settle(self):
        assert_guest_start_converges(self, '_report_desktop "$name"')
        steps = [s[0] for s in guest.STEPS]
        self.assertLess(steps.index("settle_desktop"), steps.index("report_desktop"))

    def test_one_renderer_for_every_report(self):
        """A second renderer is a second voice about the same guest."""
        self.assertEqual([REPO / "lib" / "common.sh"],
                         [f for f in sorted(repo_files()) if f.suffix != ".py" and "render_findings() {" in f.read_text(errors="replace")])
        self.assertIn("doctor.Report", inspect.getsource(guest.render))


class TestTheDoctorOfAGuest(ReportTest):
    """`wk doctor <guest>`: the base, the desktop and what is resident, in the one renderer, with one exit code."""

    def rows(self, desktop_sample, load=None):
        here = GuestAt(desktop_sample, load)
        here.answer(["/t/tart", "list"], out='[{"Name": "wk-demo", "Source": "local", "State": "running"}]')
        here.answer(["/t/tart", "ip"], out="10.0.0.2\n")
        return guest.check_rows(self.vm(here), "demo")

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
        """`live vm.desktop`: a running guest's desktop has nothing in front of it and nothing that arms a lock."""
        if not live_selected() or sys.platform != "darwin":
            self.skipTest("live tier not selected, or not a macOS host")
        vm = targets.Registry(str(REPO)).load("vm")
        up = [n for n, state in vm.list() if state == "running"] if vm.tart() else []
        if not up:
            self.skipTest("no macOS guest is running on this host")
        probe = guest.desktop_probe(vm.root, vm.machine, vm.guest_at(vm.ip(up[0])))
        self.assertTrue(probe, "the guest did not answer the desktop probe")
        d = guest.Desktop(vm.root, vm.machine, probe)
        self.assertEqual([], d.blockers())
        self.assertEqual("off", d.v("screenlock"))


if __name__ == "__main__":
    unittest.main()
