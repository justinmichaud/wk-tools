"""Building the golden macOS base.

`wk vm base --rebuild` is hours -- an image pull, Xcode's first launch, a
WebKit mirror and checkout -- and the completion marker is written after all
of it. Anything fatal in the late steps therefore leaves a fully provisioned
base with no marker, which the next run deletes as rubble (_ensure_base), so
what is knowable up front is checked up front and the marker records what
the base actually got.

The password is not one of those steps: macOS Tahoe 26.5 refuses the
only change form the account itself can run, so the guest keeps the password
its image ships and every command that hands a guest over states it.

Hermetic: the driver's own functions are run against a stub `tart` and stubbed
helpers -- no VM, no guest, no ssh.

Run: python3 -m unittest tests.test_vm_base -v
"""
import os
import subprocess
import platform
import unittest

from tests.support import REPO, WkTest, bash, func_body, stub_path

PROVISION = REPO / "vm" / "provision-base.sh"
VM = REPO / "targets" / "vm.sh"

# A base that exists and is stopped; every other tart verb succeeds.
TART = '''#!/bin/sh
case "$1" in
  list) echo '[{"Name":"wk-base","Source":"local","State":"stopped"}]' ;;
  *)    exit 0 ;;
esac
'''

# The driver, loaded against a scratch store: what every case below starts from.
DRIVER = '''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/resources.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target vm >/dev/null 2>&1
'''


class TestDeletingAVMReapsWhatRanIt(WkTest):
    """`tart delete` frees the disk and leaves the `tart run` process, which
    goes on holding one of macOS's few Virtualization framework slots. Two of
    those, left by interrupted builds, made a base build die in base.run.log
    with "The number of VMs exceeds the system limit" (measured 2026-09-04)."""

    def _run(self, script, leak=True):
        d = self.tmp
        (d / "bin").mkdir(exist_ok=True)
        # A stand-in for the runner: it matches `_vm_runners`' pattern and
        # sleeps until killed, so the reap is measured rather than asserted.
        runner = d / "bin" / "tart"
        # `sleep`, not `exec sleep`: exec replaces the command line the
        # reaper matches on, which is the thing under test.
        runner.write_text('#!/bin/sh\ncase "$1" in run) sleep 20 ;; '
                          'list) echo \'[]\' ;; *) exit 0 ;; esac\n')
        runner.chmod(0o755)
        pre = ""
        if leak:
            # >/dev/null 2>&1: a background process holding the captured pipe
            # keeps this bash's own reader open, and the test waits on it.
            pre = (f'"{runner}" run --no-graphics wk-demo >/dev/null 2>&1 & disown\n'
                   'sleep 0.3\n')
        return bash(f'{DRIVER}\ntart_bin() {{ printf %s "{runner}"; }}\n'
                    f'{pre}{script}')

    def test_the_runner_is_reaped_with_the_vm(self):
        cp = self._run('before=$(_vm_runners wk-demo | wc -l | tr -d " ")\n'
                       '_vm_delete wk-demo >/dev/null 2>&1\n'
                       'after=$(_vm_runners wk-demo | wc -l | tr -d " ")\n'
                       'echo "before=$before after=$after"')
        self.assertIn("before=1 after=0", cp.stdout, cp.stdout + cp.stderr)

    def test_a_runner_for_another_vm_is_left_alone(self):
        """`wk-demo` must not match `wk-demo2`: the pattern anchors on the
        whole final argument."""
        cp = self._run('_vm_delete wk-demo2 >/dev/null 2>&1\n'
                       'echo "still=$(_vm_runners wk-demo | wc -l | tr -d " ")"')
        self.assertIn("still=1", cp.stdout, cp.stdout + cp.stderr)

    def test_no_runner_is_not_an_error(self):
        cp = self._run('_vm_delete wk-demo >/dev/null 2>&1; echo "rc=$?"',
                       leak=False)
        self.assertIn("rc=0", cp.stdout, cp.stdout + cp.stderr)

    def test_every_delete_in_the_tree_goes_through_it(self):
        """One implementation, or a path that leaks a runner survives."""
        for f in (VM, REPO / "cmd" / "vm"):
            with self.subTest(file=f.name):
                bare = [l for l in f.read_text().splitlines()
                        if "_tart delete" in l and "_vm_delete" not in l]
                self.assertEqual(1 if f is VM else 0, len(bare), bare)


@unittest.skipUnless(platform.system() == "Darwin",
                     "the unblocker imports pyobjc's ApplicationServices, which is a "
                     "macOS framework -- the rule is that a test needing a machine "
                     "skips by name when it is absent")
class TestSetupAssistantIsDrivenOverAccessibility(WkTest):
    """No preference the guest can write stops Setup Assistant drawing: measured
    2026-09-09 on a clone carrying every DidSee key its own binary reads plus
    ~/.skipbuddy, Buddy still launched and still drew its AutoUpdate pane. So it
    is driven, over the Accessibility API, which answers a plain ssh session
    because the guest runs with SIP disabled.

    Elements are chosen by AXIdentifier, never by where they draw: the panes put
    "Only Download Automatically" and "Restart" where a coordinate sweep would
    land, and answering one of those takes a guest down."""

    def _unblock(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "wk_unblock", REPO / "vm" / "desktop-unblock.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _pick(self, pairs):
        mod = self._unblock()
        return mod._pick([(i, t, object()) for i, t in pairs])[0]

    def test_a_confirmation_sheet_outranks_the_pane_behind_it(self):
        """Declining the account pane opens a Skip/Don't Skip sheet over it. The
        sheet has to be answered first or the press lands on the dead pane."""
        self.assertEqual("action-button-1", self._pick([
            ("Next Button", "Continue"), ("action-button-1", "Skip"),
            ("action-button-2", "Don’t Skip")]))

    def test_the_decline_is_never_the_dont_skip_button(self):
        """Both sit in the same sheet and only their identifiers tell them
        apart; pressing Don't Skip walks straight back into the pane."""
        self.assertNotEqual("action-button-2", self._pick([
            ("action-button-2", "Don’t Skip"), ("action-button-1", "Skip")]))

    def test_the_account_pane_is_declined_through_its_own_menu_item(self):
        """Its Continue never enables -- measured, AXEnabled False -- so the way
        past is the alternate button's popup and the decline inside it."""
        self.assertEqual("userDeclinediCloud", self._pick([
            ("Alternate Button", "Other Sign-In Options"),
            ("userDeclinediCloud", "Sign in Later in Settings")]))

    def test_an_ordinary_pane_takes_its_primary_button(self):
        self.assertEqual("Next Button", self._pick([
            ("Next Button", "Continue"),
            ("Alternate Button", "Only Download Automatically")]))

    def test_the_flow_is_never_walked_backwards(self):
        self.assertIsNone(self._pick([("Previous Button", "Back")]))

    def test_a_pane_offering_nothing_is_not_guessed_at(self):
        """Every control is disabled while a pane settles the last answer. A
        press picked out of that reading lands on whatever happens to be there."""
        self.assertIsNone(self._pick([("", "")]))

    def test_a_guest_that_goes_quiet_is_not_driven(self):
        body = func_body(VM.read_text(), "_unblock_desktop")
        self.assertIn("_setup_assistant_state", body)

    def test_the_base_is_driven_before_it_is_judged(self):
        body = func_body(VM.read_text(), "_provision_base")
        self.assertLess(body.index("_unblock_desktop"), body.index("_check_base_screen"))

    def test_the_desktop_is_settled_again_after_the_flow(self):
        """Driving it turns diagnostic submission on; re-settling afterwards is
        what keeps that out of every clone."""
        body = func_body(VM.read_text(), "_provision_base")
        self.assertLess(body.index("_unblock_desktop"), body.index("_settle_desktop"))
        self.assertIn("AutoSubmit", (REPO / "bench" / "mac-quiet-desktop.sh").read_text())

    def test_the_base_is_judged_on_the_screen_a_login_brings_up(self):
        """A pane that was only dismissed comes back at the next login, so the
        screen the flow leaves behind proves nothing."""
        body = func_body(VM.read_text(), "_provision_base")
        i = body.index("rebooting the base")
        self.assertLess(body.index("_unblock_desktop"), i)
        self.assertLess(i, body.index("_check_base_screen"))

    def test_a_base_is_never_sealed_behind_a_pane(self):
        body = func_body(VM.read_text(), "_check_base_screen")
        self.assertIn("die", body)
        self.assertNotIn("warn", body)

    def test_a_pane_that_comes_back_at_the_next_login_is_not_sealed(self):
        """The one reading that proves the flow finished. A base sealed on the
        screen the flow left behind is how every clone inherited a pane."""
        cp = bash('''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/resources.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target vm >/dev/null 2>&1
WK_VM_LOGIN_SETTLE=6
_setup_assistant_state() { echo up; }
_wait_login_settled 1.2.3.4 && echo "SEALED" || echo "REFUSED"
''')
        self.assertIn("REFUSED", cp.stdout, cp.stdout + cp.stderr)

    def test_a_login_that_stays_clear_seals(self):
        cp = bash('''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/resources.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target vm >/dev/null 2>&1
WK_VM_LOGIN_SETTLE=6
_setup_assistant_state() { echo gone; }
_wait_login_settled 1.2.3.4 && echo "SEALED" || echo "REFUSED"
''')
        self.assertIn("SEALED", cp.stdout, cp.stdout + cp.stderr)

    def test_the_base_is_watched_across_the_login_before_it_is_judged(self):
        body = func_body(VM.read_text(), "_provision_base")
        self.assertLess(body.index("_wait_login_settled"), body.index("_check_base_screen"))

    def test_the_base_boots_one_way_only(self):
        """Measured 2026-09-09: the proof-reboot went through `_boot`, which
        applies softnet, while provisioning boots the base open. The subnet
        changed, `tart ip` answered with the lease from before it, and the
        rebuild died at 90 minutes ssh-ing an address nothing was on."""
        body = func_body(VM.read_text(), "_provision_base")
        self.assertNotIn("_boot ", body)
        self.assertEqual(2, body.count("_start_base"), body)

    def test_the_base_is_never_booted_behind_the_egress_filter(self):
        """Its account pane needs Apple's servers and its provisioning needs
        PyPI and a WebKit clone; a workspace is the thing that gets the filter."""
        self.assertNotIn("_softnet_flags", func_body(VM.read_text(), "_start_base"))

    def test_a_reboot_that_never_answers_is_not_read_as_a_clear_screen(self):
        body = func_body(VM.read_text(), "_provision_base")
        self.assertLess(body.index("_wait_ssh"), body.index("_wait_login_settled"))

    def test_a_clone_keeps_the_base_s_serial(self):
        """The measured cause of the whole thing. A changed serial is a new
        machine to macOS, so Buddy runs again -- and a clone is the one guest
        that cannot answer it, its account pane needing Apple's servers that
        the egress filter refuses. A/B on one clone of a sealed base, 2026-09-09:
        without --random-serial the desktop is clear, and flipping only that
        flag brings the pane back."""
        run = [l for l in func_body(VM.read_text(), "t_create").splitlines()
               if not l.lstrip().startswith("#")]
        self.assertNotIn("--random-serial", "\n".join(run))

    def test_a_clone_still_gets_its_own_mac(self):
        """Two guests on one network need distinct MACs, and the A/B above
        shows the MAC is not what moves the pane."""
        run = [l for l in func_body(VM.read_text(), "t_create").splitlines()
               if not l.lstrip().startswith("#")]
        self.assertIn("--random-mac", "\n".join(run))

    def test_the_rfb_console_client_is_gone(self):
        """One implementation per behaviour: the coordinate clicker it drove is
        what AXIdentifier replaced."""
        self.assertFalse((REPO / "vm" / "console-keys.py").exists())
        self.assertNotIn("vnc", VM.read_text().lower())

class TestTheBaseIsAskedWhatIsOnItsScreen(WkTest):
    """A pane on the base's screen is a pane on every guest cloned from it, and
    no preference a guest writes takes it away (docs/defects lists what was
    tried). So provisioning asks once, at the end, where somebody is already
    waiting on a build -- and it must not end the build to do it: every command
    in this driver runs under `set -euo pipefail`, so an unanswered base would
    otherwise abort the last step after the hours, saying nothing."""

    def _check(self, ssh_body):
        return bash(f'''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/resources.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target vm >/dev/null 2>&1
_ssh() {{ {ssh_body}; }}
_check_base_screen 1.2.3.4 2>&1
echo "rc=$?"
''')

    def test_a_base_that_does_not_answer_is_not_sealed(self):
        """An unread screen is not a clear one, and the base is the one artifact
        whose mistakes every clone inherits."""
        cp = self._check("return 1")
        self.assertNotIn("rc=0", cp.stdout, cp.stdout + cp.stderr)
        self.assertIn("could not ask", cp.stdout)

    def test_a_clear_screen_says_so(self):
        cp = self._check('cat >/dev/null; echo "windows=Terminal:0:800x600;"')
        self.assertIn("rc=0", cp.stdout, cp.stdout + cp.stderr)
        self.assertIn("screen is clear", cp.stdout)

    def test_a_pane_is_named_and_the_base_is_not_sealed_behind_it(self):
        cp = self._check('cat >/dev/null; echo "windows=Setup Assistant:0:800x600;Terminal:0:800x600;"')
        self.assertNotIn("rc=0", cp.stdout, cp.stdout + cp.stderr)
        self.assertIn("Setup Assistant:0:800x600", cp.stdout)
        self.assertNotIn("Terminal", cp.stdout.split("nothing wk put there:")[1])

    def test_provisioning_asks_before_it_seals_the_base(self):
        """After the check the base is stopped and marked ready; a clone taken
        from it carries whatever was on that screen."""
        body = func_body(VM.read_text(), "_provision_base")
        self.assertIn("_check_base_screen", body)
        self.assertLess(body.index("_check_base_screen"), body.index("_base_mark_ready"))


class TestTheVMLimitCountsEveryVMOnTheHost(WkTest):
    """Virtualization.framework has one limit for the whole host, and the
    podman machine that carries the container workspaces spends a slot of it.
    Counting only `tart list` let a third VM be started and refused: the loser
    said "The number of VMs exceeds the system limit" in its own run log and
    nowhere a person looks (measured 2026-09-04)."""

    def _count(self, tart_running, podman_state, trailer=""):
        vms = ",".join('{"Name":"wk-g%d","Source":"local","State":"running"}' % i
                       for i in range(tart_running))
        tart = "case \"$1\" in list) echo '[%s]' ;; *) exit 0 ;; esac\n" % vms
        podman = 'echo %s\n' % podman_state
        with stub_path({"tart": tart, "podman": podman}) as binp:
            return bash(f'{DRIVER}\necho "n=$(_running_count)"\n'
                        f'{trailer}\n_check_guest_limit 2>&1 || true',
                        env={"PATH": f"{binp}:/usr/bin:/bin",
                             "WK_VM_MAX": "2"})

    def test_a_running_podman_machine_is_one_of_the_two(self):
        cp = self._count(1, "running")
        self.assertIn("n=2", cp.stdout, cp.stdout + cp.stderr)

    def test_a_stopped_podman_machine_is_not_counted(self):
        cp = self._count(1, "stopped")
        self.assertIn("n=1", cp.stdout, cp.stdout + cp.stderr)

    def test_counting_succeeds_when_the_podman_machine_is_down(self):
        """The count is taken under `set -euo pipefail` by every caller. A
        stopped machine that leaves a 1 behind ends `wk vm new` right after the
        staleness warning, having created nothing and having said nothing."""
        cp = self._count(1, "stopped", trailer='_running_vms >/dev/null; echo "vms_rc=$?"\n'
                                                '_running_count >/dev/null; echo "count_rc=$?"')
        self.assertIn("vms_rc=0", cp.stdout, cp.stdout + cp.stderr)
        self.assertIn("count_rc=0", cp.stdout, cp.stdout + cp.stderr)

    def test_the_refusal_names_what_is_holding_the_slots(self):
        """A refusal that says '2 VM(s) are already running' while `wk vm ls`
        shows one is a refusal nobody can act on."""
        cp = self._count(1, "running")
        out = cp.stdout + cp.stderr
        self.assertIn("g0", out, out)
        self.assertIn("podman machine", out, out)
        self.assertIn("podman machine stop", out, out)

    def test_one_guest_alone_is_let_through(self):
        cp = self._count(1, "stopped")
        self.assertNotIn("already running on this host", cp.stdout + cp.stderr)


class TestProvisioningOutlivesItsConnection(WkTest):
    """The base's first act is cloning all of WebKit, which is over an hour.
    Run in the foreground it dies with the ssh session (measured 2026-09-04:
    "Read from remote host: Connection reset by peer" took the clone with it),
    so it is detached and polled."""

    def test_provisioning_is_detached_and_waited_for(self):
        body = func_body(VM.read_text(), "_provision_base")
        self.assertIn("detach_remote _base_ssh", body)
        self.assertIn("detach_wait_remote _base_ssh", body)
        self.assertNotIn('_ssh "$ip" "env WK_VM_DISPLAY', body,
                         "provisioning is back on a foreground ssh")

    def test_its_log_is_fetched_and_named_in_the_failure(self):
        body = func_body(VM.read_text(), "_provision_base")
        self.assertIn("base-provision.log", body)
        self.assertIn("wk vm base --refresh", body)


class TestAnIdlePodmanMachineIsNotAReasonToRefuse(WkTest):
    """It holds the whole envelope whether or not anything runs in it, and a
    stop costs nothing a workspace notices. Anything actually running in there
    is named instead -- stopping a machine underneath a build is the mistake
    this guards."""

    def test_the_check_stops_it_only_when_nothing_runs_in_it(self):
        body = func_body(VM.read_text(), "_check_memory_budget")
        self.assertIn("_podman_containers_running", body)
        self.assertLess(body.index("_podman_containers_running"),
                        body.index("podman machine stop"),
                        "the machine is stopped before anything asks what is in it")

    def test_an_unreadable_answer_counts_as_busy(self):
        """A machine that will not say what it holds is not one to stop."""
        body = func_body(VM.read_text(), "_podman_containers_running")
        self.assertIn("|| echo 1", body)


class TestADirtyTreeIsRefusedBeforeTheBaseIsDestroyed(WkTest):
    """A base is given a commit (tools_committed, lib/tools.sh), so a dirty
    tree cannot provision one. Asked before the delete: measured, refusing
    afterwards costs a `tart delete`, a clone, a 140GB -> 320GB grow and a boot
    to reach a verdict that is local and free."""

    CMD = REPO / "cmd" / "vm"

    def test_the_refusal_precedes_every_destructive_step(self):
        arm = self.CMD.read_text().split("--rebuild)", 1)[1].split("--rm)", 1)[0]
        self.assertIn("tools_committed", arm)
        self.assertLess(arm.index("tools_committed"), arm.index("_vm_delete"),
                        "the base is deleted before the tree is checked")
        self.assertLess(arm.index("tools_committed"), arm.index("confirm "),
                        "the prompt comes before the check it would waste")

    def test_it_says_nothing_was_deleted(self):
        arm = self.CMD.read_text().split("--rebuild)", 1)[1].split("--rm)", 1)[0]
        self.assertIn("Nothing has been deleted", arm)


class TestTheGuestKeepsTheImagesPassword(WkTest):
    """Measured on macOS Tahoe 26.5: the only form the account itself can run,
    `sysadminctl -oldPassword`, exits 0 having changed nothing. So the password
    is not changed at all, and one variable names it."""

    def test_provisioning_attempts_no_password_change(self):
        """Code, not prose: the comment above the variable names the very flag
        that is not used, and explains why."""
        code = "\n".join(l for l in PROVISION.read_text().splitlines()
                         if not l.lstrip().startswith("#"))
        self.assertNotIn("-newPassword", code)
        self.assertNotIn("-oldPassword", code)
        self.assertNotIn("_set_password", code)

    def test_the_password_defaults_to_the_one_the_image_ships(self):
        for path in (PROVISION, REPO / "targets" / "vm.sh"):
            src = path.read_text()
            self.assertIn('WK_VM_PASSWORD="${WK_VM_PASSWORD:-admin}"', src, path.name)

    def test_one_variable_names_it(self):
        """A second name for the same fact is a thing that can disagree. Built
        rather than written out, so this file is not its own only match."""
        gone = "WK_VM_IMAGE" + "_PASSWORD"
        out = subprocess.run(["git", "grep", "-l", gone],
                             cwd=REPO, capture_output=True, text=True).stdout
        self.assertEqual("", out.strip(), f"{gone} survives in: {out}")


class TestEveryHandoverStatesTheLogin(WkTest):
    """`wk start <guest>` said nothing about the login before this: it goes
    through t_start, which did not state it, while `wk vm start` stated it in
    the command instead. One exit in t_start is what makes both say it."""

    def test_t_start_states_the_login_on_its_one_exit(self):
        body = func_body((REPO / "targets" / "vm.sh").read_text(), "t_start")
        self.assertEqual(1, body.count("vm_login_note"), body)
        # A second `echo "$ip"` would be a return path that skips the note.
        self.assertEqual(1, body.count('echo "$ip"'), body)

    def test_the_note_is_not_restated_by_the_command_that_starts_a_guest(self):
        """cmd/vm's start arm calls t_start, so a call of its own prints it
        twice."""
        src = (REPO / "cmd" / "vm").read_text()
        arm = src.split("\nstart)", 1)[1].split("\nstop)", 1)[0]
        self.assertNotIn("vm_login_note", arm, arm)

    def test_the_attach_paths_state_it_themselves(self):
        """`wk zed` and `wk vm enter` never call t_start -- they attach to a
        guest that is already up -- so each states it directly."""
        self.assertIn("vm_login_note", (REPO / "cmd" / "zed").read_text())
        enter = (REPO / "cmd" / "vm").read_text().split("\nenter)", 1)[1]
        self.assertIn("vm_login_note", enter.split("\nsync)", 1)[0])


class TestSyncRefreshesEachGuestsMirror(WkTest):
    """`wk sync --tools` reaches t_sync, this target's furniture: a guest's
    tooling copy and its mirror. Each guest's mirror is its own -- inherited
    from the base by copy-on-write and diverging from there -- so this is the
    only thing that brings one up to date, and `wk sync <workspace>` then
    fetches the checkout out of it with no network round trip.

    Driven with the ssh replaced by a recorder that answers as a guest would."""

    def _sync(self, ssh_body):
        store = self.tmp / "store"
        store.mkdir(exist_ok=True)
        with stub_path({"tart": TART}) as binp:
            return bash(DRIVER + f'''
target_workspaces() {{ echo mya; }}
t_info()            {{ echo running; }}
t_sync_tools()      {{ :; }}
_ip()               {{ echo 10.0.0.9; }}
_ssh() {{ shift; printf '%s\\n' "$1" > {str(self.tmp / 'sent')!r}
{ssh_body}
}}
t_sync
''', env={"WK_VM_STORE": str(store),
          "PATH": f"{binp}:{os.environ['PATH']}"})

    def test_it_refreshes_the_mirror_the_driver_names(self):
        cp = self._sync('echo "mirror-fetch origin ok"\necho "mirror-fetch fork FAILED"')
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        sent = (self.tmp / "sent").read_text()
        self.assertIn("/Users/admin/WebKit.git", sent)
        # The one refresh snippet, not a second spelling of what a mirror is.
        self.assertIn("mirror-fetch $r ok", sent)
        self.assertIn("config remote.forkwpe.tagOpt --no-tags", sent)
        self.assertIn("mirror origin ok", out)
        self.assertIn("mirror fork FAILED", out, "an unreachable fork is not the guest's failure")
        self.assertIn("mya", out)

    def test_a_guest_with_no_mirror_is_told_what_puts_one_there(self):
        """A guest predating the mirror is not upgraded in place -- a 19 GB
        clone is not a side effect of `wk sync --tools`, and a guest is what
        the golden base produced."""
        cp = self._sync('echo mirror-absent')
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("no mirror in this guest", out)
        self.assertIn("wk vm base --rebuild", out)

    def test_a_guest_that_is_not_running_is_skipped(self):
        store = self.tmp / "store"
        store.mkdir(exist_ok=True)
        with stub_path({"tart": TART}) as binp:
            cp = bash(DRIVER + '''
target_workspaces() { echo mya; }
t_info()            { echo exited; }
_ssh()              { echo "ssh should not have run" >&2; exit 1; }
t_sync
''', env={"WK_VM_STORE": str(store), "PATH": f"{binp}:{os.environ['PATH']}"})
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("not running -- skipped", out)
        self.assertNotIn("should not have run", out)

    def test_a_refresh_that_could_not_run_fails_the_sync(self):
        cp = self._sync("exit 1")
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)


class TestTheGuestCarriesAMirror(unittest.TestCase):
    """Provisioning builds the mirror every guest cloned from this base
    inherits, at the path the driver names (t_mirror_dir) -- handed over
    rather than spelled again in the guest."""

    def test_the_path_is_the_drivers_and_is_required(self):
        text = PROVISION.read_text()
        self.assertIn("WK_VM_MIRROR:?", text,
                      "the guest script invents a mirror path when given none")
        self.assertIn('WK_VM_MIRROR="$(t_mirror_dir "$WK_VM_BASE")"',
                      VM.read_text(),
                      "targets/vm.sh no longer hands the mirror path over")

    def test_it_is_made_by_the_one_refresh_script(self):
        text = PROVISION.read_text()
        self.assertIn("mirror_refresh_script", text)
        self.assertNotIn("github.com", text,
                         "provisioning names an upstream URL of its own")

    def test_the_checkout_shares_the_mirrors_objects(self):
        """--shared, so the history is stored once in the base and the
        per-workspace cost of both is what copy-on-write makes it."""
        self.assertIn("git clone --quiet --shared", PROVISION.read_text())

    def test_nothing_is_seeded_from_the_host(self):
        """One path to a checkout: the mirror. A seed rsynced off the host is a
        second one, taken only when a host happens to have a checkout at a path
        nothing else in this repo knows about."""
        for path in (PROVISION, VM):
            with self.subTest(file=path.name):
                self.assertNotIn("wk-seed", path.read_text())
                self.assertNotIn("WK_HOST_WEBKIT", path.read_text())


if __name__ == "__main__":
    unittest.main()
