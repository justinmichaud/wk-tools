"""A macOS guest fit to look at and to measure in: what `wk vm check` reads,
what vm/desktop.sh writes, and what accumulates in a guest that stays up.

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

The second report is what is resident in there: a guest holds its whole memory
allocation whether or not it is busy, so a long-lived one runs out of memory
because something stayed -- an editor's remote server and the shell each of its
terminal panes left. vm/load-probe.sh gathers the raw `ps` rows and
vm_load_findings does the arithmetic, which is what a captured sample can drive.

Run: python3 -m unittest tests.test_vm_desktop -v
"""
import os
import platform
import unittest

from tests.support import (REPO, repo_files, WkTest, assert_guest_start_converges, bash,
                           func_body, run, stub_path)

DESKTOP = REPO / "vm" / "desktop.sh"
PROBE = REPO / "vm" / "desktop-probe.sh"


def _src(*parts):
    return REPO.joinpath(*parts).read_text()


def _defines(func):
    """Every shell file in the tree that defines `func`: what a
    one-implementation claim is checked against."""
    return [f for f in sorted(repo_files())
            if f.suffix not in (".py", ".pyc")
            and f"{func}() {{" in f.read_text(errors="replace")]

SETTLED = """console_user=admin
screenlock=off
idletime=0
askforpassword=0
displaysleep=0
setupassistant_pending=
update_check=0
update_download=0
update_check_system=0
update_autoinstall_system=0
update_schedule=off
setupassistant_seen_product=26.5
os_product=26.5
panels=
user=admin
"""

# What `wk vm check mya` actually reported on 2026-08-31: a screen saver armed
# because the base's ByHost setting does not survive a clone, two Setup
# Assistant panes this macOS added, and one of them on screen. A capture, so it
# carries only the keys the probe printed that day -- which is itself a case
# worth keeping: a guest that answers nothing about Software Update must be
# reported as unknown, never as settled.
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

# A guest whose base never turned Software Update off: checks on where
# softwareupdated reads them, a schedule that brings the offer back on its own,
# macOS updates set to install themselves -- which reboots the guest, mid-build
# if that is when one lands -- and a Setup Assistant that has not seen this
# macOS, so Buddy shows its "what is new" pane at login.
UPDATE_ON = (SETTLED
             .replace("update_check_system=0", "update_check_system=1")
             .replace("update_autoinstall_system=0", "update_autoinstall_system=1")
             .replace("update_schedule=off", "update_schedule=on")
             .replace("setupassistant_seen_product=26.5",
                      "setupassistant_seen_product=26.4"))


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
        self.assertTrue(any("on the desktop right now" in w for w in wrong), wrong)

    def test_a_guest_that_says_nothing_about_software_update_is_not_called_ok(self):
        """The capture above answers none of the Software Update keys. Silence
        is not off: it is reported, with the start that asks again."""
        f = [x for x in findings(AS_FOUND) if "Software Update" in x[1]]
        self.assertTrue(f, findings(AS_FOUND))
        self.assertNotIn("ok", [x[0] for x in f], f)
        self.assertTrue(any("wk vm start" in x[2] for x in f), f)

    def test_the_update_offer_is_judged_where_softwareupdated_reads_it(self):
        """The per-user domain is what System Settings shows a person; the one
        that puts a panel on the desktop is /Library/Preferences, which is
        root's -- so a guest can read 0 in the first and 1 in the second."""
        f = findings(UPDATE_ON)
        wrong = [x[1] for x in f if x[0] == "wrong"]
        self.assertTrue(any("Software Update will offer an upgrade" in w for w in wrong), wrong)
        self.assertTrue(any("scheduled update check is on" in w for w in wrong), wrong)
        self.assertTrue(any("install themselves" in w for w in wrong), wrong)
        self.assertTrue(any("what is new in macOS" in w for w in wrong), wrong)
        for x in f:
            if "Software Update will offer" in x[1]:
                self.assertIn("wk vm base --rebuild", x[2])

    def test_a_panel_on_screen_is_remedied_by_a_restart_not_a_kill(self):
        """Killing Setup Assistant ends the desktop session with it -- that is
        how a guest ended up at its login window while this was being written."""
        f = [x for x in findings(AS_FOUND) if "on the desktop right now" in x[1]]
        self.assertTrue(f)
        self.assertIn("wk vm stop", f[0][2])

    def test_a_login_window_is_reported_as_no_desktop_at_all(self):
        f = [x for x in findings(LOGIN_WINDOW) if x[0] == "wrong"]
        self.assertTrue(any("nobody is logged in" in x[1] for x in f), f)

    def test_the_login_names_the_account_and_not_its_password(self):
        """These findings are printed on every `wk vm start`, so the password
        must not be in them: vm_login_note is the one place it is stated, and
        this note points at it."""
        note = [x for x in findings(SETTLED) if x[0] == "note" and "logs in as" in x[1]]
        self.assertTrue(note, findings(SETTLED))
        self.assertIn("admin", note[0][1])
        self.assertNotIn("admin / ", note[0][1])
        self.assertIn("password", note[0][2])

    def test_the_password_is_stated_by_vm_login_note_alone(self):
        vm = _src("targets", "vm.sh")
        uses = [l for l in vm.splitlines()
                if "$WK_VM_PASSWORD" in l and not l.lstrip().startswith("#")]
        in_note = [l for l in uses if "logs in as" in l]
        self.assertEqual(1, len(in_note), uses)

    def test_the_password_never_travels_as_an_ssh_argument(self):
        """`env WK_VM_PASSWORD=... bash -s` puts it in `ps` on both machines.
        _settle_desktop streams it as the script's own first line instead."""
        body = func_body(_src("targets", "vm.sh"), "_settle_desktop")
        self.assertNotIn("env WK_VM_PASSWORD=", body)
        self.assertIn("printf 'WK_VM_PASSWORD=%s\\n'", body)


class TestTheFindingsAreOneLineEach(WkTest):
    """Every renderer reads a finding a line at a time, so a remedy wrapped
    across two lines is a remedy whose second half nobody ever sees."""

    def _raw(self, probe):
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
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout

    def test_no_finding_wraps(self):
        for name, probe in (("settled", SETTLED), ("as found", AS_FOUND),
                            ("update on", UPDATE_ON), ("login window", LOGIN_WINDOW)):
            with self.subTest(probe=name):
                for line in self._raw(probe).splitlines():
                    self.assertEqual(3, len(line.split("\t")), line)

    def test_the_load_findings_are_one_line_each_too(self):
        for f in load_findings(_load_sample()) + load_findings(IDLE_SAMPLE):
            self.assertEqual(3, len(f), f)


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
        # `softwareupdate --schedule` with no argument is the read; `off`/`on`
        # are the writes, and only vm/desktop.sh may spell those.
        for bad in ("defaults write", "defaults -currentHost write", "pkill",
                    "killall", "softwareupdate --schedule off",
                    "softwareupdate --schedule on", "sysadminctl -screenLock off",
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


# A guest that has been up for a fortnight with an editor attached: a
# zed-remote-server, forty shells behind it, and the memory readings macOS
# itself reports. `ps -Ao rss=,comm=` is what vm/load-probe.sh sends up, one row
# per process, and the arithmetic on it is what is being tested.
def _load_sample(shells=40, free_pct=6, swap_used="4096.00M"):
    rows = [
        "proc=24880 /sbin/launchd",
        "proc=1048576 /System/Library/Frameworks/WebKit.framework/jsc",
        "proc=512000 /Users/admin/.zed_server/stable-0.1/zed-remote-server",
        "proc=221000 /Users/admin/.local/share/claude/versions/2.0.1/claude",
        "proc=61000 /usr/sbin/sshd-session",
        "proc=61000 /usr/sbin/sshd-session",
    ]
    rows += ["proc=8192 /bin/zsh"] * shells
    return "\n".join([
        "mem_total_mb=32768",
        f"mem_free_pct={free_pct}",
        f"swapusage=total = 8192.00M  used = {swap_used}  free = 4096.00M  (encrypted)",
        *rows,
    ]) + "\n"


IDLE_SAMPLE = _load_sample(shells=2, free_pct=71, swap_used="0.00M")


def load_findings(probe):
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
vm_load_findings "$probe"
''')
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return [tuple(l.split("\t")) for l in cp.stdout.splitlines()
            if len(l.split("\t")) == 3]


class TestWhatIsResidentInThere(WkTest):
    """The measurement `wk vm check` adds for a guest that runs out of memory:
    what stayed, how much of it there is, and which of them keeps the rest
    open. Nothing wk runs in a guest outlives its ssh session, so an
    accumulation is something with a process of its own."""

    def test_an_accumulation_is_reported_and_the_culprit_named(self):
        f = load_findings(_load_sample())
        wrong = [x for x in f if x[0] == "wrong"]
        shells = [x for x in wrong if "shells are resident" in x[1]]
        self.assertTrue(shells, f)
        self.assertIn("40 shells", shells[0][1])
        self.assertIn("editor remote server", shells[0][1])
        self.assertIn("wk vm stop", shells[0][2])

    def test_the_editor_remote_server_is_named_as_outliving_its_window(self):
        f = [x for x in load_findings(_load_sample())
             if "editor remote server" in x[1] and x[0] == "note"]
        self.assertTrue(f, load_findings(_load_sample()))
        self.assertIn("outlives the editor window", f[0][2])

    def test_low_memory_is_wrong_and_names_the_biggest_processes(self):
        f = [x for x in load_findings(_load_sample()) if x[0] == "wrong"
             and "free" in x[1]]
        self.assertTrue(f, load_findings(_load_sample()))
        self.assertIn("6%", f[0][1])
        self.assertIn("jsc", f[0][1])

    def test_swap_in_a_fixed_allocation_is_reported(self):
        f = [x for x in load_findings(_load_sample()) if "swap" in x[1]]
        self.assertTrue(f, load_findings(_load_sample()))
        self.assertIn("4096 MB of swap", f[0][1])

    def test_an_idle_guest_is_all_ok(self):
        f = load_findings(IDLE_SAMPLE)
        self.assertEqual({x[0] for x in f} - {"note"}, {"ok"}, f)
        self.assertTrue(any("2 shells resident" in x[1] for x in f), f)
        self.assertTrue(any("71%" in x[1] for x in f), f)

    def test_the_thresholds_are_the_documented_overrides(self):
        """WK_VM_SHELLS_WARN and WK_VM_MEM_FREE_WARN_PCT reach the arithmetic,
        or the numbers in targets/vm.sh's header are decoration."""
        cp = bash(f'''
set -euo pipefail
WK_ROOT={str(REPO)!r}
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/resources.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target vm
probe=$(cat <<'PROBE_EOF'
{_load_sample()}
PROBE_EOF
)
vm_load_findings "$probe"
''', env={"WK_VM_SHELLS_WARN": "100", "WK_VM_MEM_FREE_WARN_PCT": "1"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("shells are resident", cp.stdout, cp.stdout)
        self.assertNotIn("macOS calls that pressure", cp.stdout, cp.stdout)


class TestTheLoadProbeChangesNothing(WkTest):
    def test_it_only_reads(self):
        text = (REPO / "vm" / "load-probe.sh").read_text()
        for bad in ("pkill", "killall", "kill -", "defaults write", "sudo"):
            self.assertNotIn(bad, text, f"vm/load-probe.sh runs {bad}")

    def test_it_sources_nothing(self):
        """Like the desktop probe: it must answer about a guest whose wk-tools
        copy is older than it."""
        text = (REPO / "vm" / "load-probe.sh").read_text()
        for bad in ("lib/common.sh", "$WK_ROOT", "$WK_TOOLS_DIR"):
            self.assertNotIn(bad, text, f"vm/load-probe.sh reaches for {bad}")

    def test_nothing_in_this_repo_leaves_a_shell_behind(self):
        """The claim the report rests on: wk's own ways into a guest end with
        their ssh session -- no ControlPersist master to hold one open, and
        both interactive forms exec the ssh rather than backgrounding it."""
        vm = _src("targets", "vm.sh")
        opts = vm[vm.index("_ssh_opts() {"):]
        opts = opts[:opts.index("\n}\n")]
        self.assertNotIn("ControlPersist", opts,
                         "a multiplexed master would hold a session open in "
                         "the guest after the command that made it ended")
        for fn in ("t_exec_tty", "t_enter"):
            body = vm[vm.index(f"{fn}() {{"):]
            body = body[:body.index("\n}\n")]
            self.assertIn("exec ssh -t", body, fn)


class TestOneRendererForEveryReport(unittest.TestCase):
    """`wk vm start`, `wk vm ls` and `wk vm check` print these findings, and a
    second renderer is a second voice about the same guest."""

    def test_the_renderer_is_defined_once(self):
        self.assertEqual(_defines("vm_render_findings"),
                         [REPO / "targets" / "vm.sh"],
                         _defines("vm_render_findings"))

    def test_both_t_start_branches_print_the_findings(self):
        """A running guest is converged by the same start, so it gets the same
        report: the settle changes things and says nothing, and this is the
        measurement of what it left."""
        assert_guest_start_converges(self, '_report_desktop "$name"')
        # After the settle, or it reports the state the settle was fixing.
        body = func_body(_src("targets", "vm.sh"), "_converge_guest")
        i = body.index('_settle_desktop "$name" "$ip"')
        self.assertIn("_report_desktop", body[i:])

    def test_the_report_is_the_probe_and_not_a_second_opinion(self):
        vm = _src("targets", "vm.sh")
        body = vm[vm.index("_report_desktop() {"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("vm_desktop_probe", body)
        self.assertIn("vm_desktop_findings", body)
        self.assertIn("vm_render_findings", body)

    def test_check_reports_the_base_the_desktop_and_the_load(self):
        cmd = _src("cmd", "vm")
        for fn in ("vm_base_findings", "vm_desktop_findings", "vm_load_findings"):
            self.assertIn(fn, cmd, fn)
        self.assertNotIn("printf '  \\033[32mok", cmd,
                         "cmd/vm renders findings itself instead of through "
                         "vm_render_findings")


class TestTheStartReport(WkTest):
    """`wk vm start` settles the desktop and then says what it left, because
    the person running it is about to look at that window. Driven against a
    canned probe: `_ssh` answers with a capture and `_ip` with an address, so
    the real function runs with no guest anywhere."""

    def _report(self, sample):
        canned = self.tmp / "probe.txt"
        canned.write_text(sample)
        cp = bash(f'''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/resources.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target vm >/dev/null 2>&1
_ip()  {{ echo 10.0.0.2; }}
_ssh() {{ cat {str(canned)!r}; }}
_report_desktop demo
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout + cp.stderr

    def test_every_finding_reaches_the_console(self):
        out = self._report(AS_FOUND)
        self.assertIn("the screen saver is armed", out)
        self.assertIn("Setup Assistant", out)
        self.assertIn("something is on the desktop right now", out)
        self.assertIn("wk vm check demo", out)

    def test_a_wrong_finding_brings_its_remedy_with_it(self):
        out = self._report(AS_FOUND)
        self.assertIn("wk vm base --rebuild", out)

    def test_a_settled_guest_still_gets_the_report(self):
        """Silence on a good start would mean nobody ever learns what the
        report says, or that it ran at all."""
        out = self._report(SETTLED)
        self.assertIn("screen lock off", out)
        self.assertIn("nothing modal on screen now", out)

    def test_the_report_goes_to_stderr(self):
        """t_start's stdout is the guest's address; a report on it would be
        parsed as one."""
        canned = self.tmp / "probe.txt"
        canned.write_text(SETTLED)
        cp = bash(f'''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/resources.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target vm >/dev/null 2>&1
_ip()  {{ echo 10.0.0.2; }}
_ssh() {{ cat {str(canned)!r}; }}
_report_desktop demo
''')
        self.assertEqual("", cp.stdout, cp.stdout)
        self.assertIn("screen lock off", cp.stderr)

    def test_a_guest_that_does_not_answer_does_not_fail_the_start(self):
        """A guest that came up is up; a probe that did not answer is not a
        reason to refuse to report the address."""
        cp = bash('''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/resources.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target vm >/dev/null 2>&1
_ip()  { echo 10.0.0.2; }
_ssh() { return 1; }
_report_desktop demo && echo "rc=0"
''')
        self.assertIn("rc=0", cp.stdout + cp.stderr, cp.stdout + cp.stderr)


# `tart` answering about a base and one running guest, and `ssh` answering with
# whichever capture the probe on its stdin asks for: `wk vm check` end to end
# with no guest anywhere. The two probes are told apart by what they print,
# which is the only thing either side of that ssh knows about the other.
TART_RUNNING = """#!/bin/sh
case "$1" in
  list) echo '[{"Name":"wk-base","Source":"local","State":"stopped"},
                {"Name":"wk-demo","Source":"local","State":"running"}]' ;;
  ip)   echo 10.0.0.2 ;;
  *)    exit 0 ;;
esac
"""


def _canned_ssh(desktop, load_arm):
    """A stub `ssh` that answers the probe on its stdin: the load probe is the
    one that prints mem_total_mb, which is all either side knows of the other."""
    return f"""#!/bin/sh
script=$(cat)
case "$script" in
  *mem_total_mb*)
{load_arm}
    ;;
  *)
cat <<'WK_DESKTOP'
{desktop}
WK_DESKTOP
    ;;
esac
"""


@unittest.skipUnless(platform.system() == "Darwin", "wk vm needs a macOS host")
class TestCheckEndToEnd(WkTest):
    """What a person actually runs. The base, the desktop and what is resident
    in the guest, one renderer, one exit code."""

    def _check(self, desktop=None, load=None, load_fails=False):
        store = self.tmp / "store"
        (store / "vm").mkdir(parents=True, exist_ok=True)
        cp = bash('''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/resources.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target vm >/dev/null 2>&1
_base_mark_ready
''', env={"WK_VM_STORE": str(store)})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

        load_arm = ("exit 1" if load_fails
                    else f"cat <<'WK_LOAD'\n{load}\nWK_LOAD")
        with stub_path({"tart": TART_RUNNING,
                        "ssh": _canned_ssh(desktop, load_arm)}) as binp:
            return run("vm", "check", "demo",
                       env={"WK_VM_STORE": str(store),
                            "PATH": f"{binp}:{os.environ['PATH']}"})

    def test_a_good_guest_passes_with_all_three_reports(self):
        cp = self._check(desktop=SETTLED, load=IDLE_SAMPLE)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("matches its provisioning inputs", cp.stdout)
        self.assertIn("screen lock off", cp.stdout)
        self.assertIn("shells resident", cp.stdout)

    def test_a_guest_out_of_memory_fails_and_names_what_stayed(self):
        cp = self._check(desktop=SETTLED, load=_load_sample())
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("shells are resident", cp.stdout)
        self.assertIn("editor remote server", cp.stdout)
        self.assertIn("macOS calls that pressure", cp.stdout)

    def test_a_guest_that_will_not_say_what_is_resident_says_so(self):
        """Unknown is reported, never quietly dropped."""
        cp = self._check(desktop=SETTLED, load_fails=True)
        self.assertIn("did not answer the load probe", cp.stdout)


if __name__ == "__main__":
    unittest.main()
