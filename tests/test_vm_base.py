"""Building the golden macOS base: the last step must not be able to throw
away the first ones.

`wk vm base --rebuild` is hours -- an image pull, Xcode's first launch, a
WebKit mirror and checkout, then a full mac-release build -- and the completion
marker is written after all of it. Anything fatal in the late steps therefore
leaves a fully provisioned base with no marker, which the next run deletes as
rubble (_ensure_base). So: what is knowable up front is checked up front, the
prebuild cannot fail the provisioning it is the last step of, and the marker
records what the base actually got.

The password is not one of those steps any more: macOS Tahoe 26.5 refuses the
only change form the account itself can run, so the guest keeps the password
its image ships and every command that hands a guest over states it.

Hermetic: the driver's own functions are run against a stub `tart` and stubbed
helpers -- no VM, no guest, no ssh.

Run: python3 -m unittest tests.test_vm_base -v
"""
import os
import re
import subprocess
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


class TestThePrebuildCannotUnmakeTheBase(WkTest):
    def _drive(self, body, env=None):
        store = self.tmp / "store"
        store.mkdir(exist_ok=True)
        with stub_path({"tart": TART}) as binp:
            e = {"WK_VM_STORE": str(store),
                 "PATH": f"{binp}:{os.environ['PATH']}"}
            if env:
                e.update(env)
            cp = bash(DRIVER + body, env=e)
        return cp, store / "vm" / "base.ready"

    def test_a_prebuild_that_cannot_start_warns_and_succeeds(self):
        """The realistic failure: the tooling push into the base fails, and
        with it the build. A warm build tree is an optimisation, so the base is
        still sealed and the marker still written."""
        cp, _ = self._drive('''
_vm_get()     { echo 8; }
build_jobs()  { echo 2; }
_push_tools() { echo "push failed" >&2; return 1; }
_prebuild_base 10.0.0.1 && echo "returned 0" || echo "returned $?"
echo "prebuilt=${_base_prebuilt:-none}"
''')
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("returned 0", cp.stdout, out)
        self.assertIn("prebuilt=none", cp.stdout, out)
        self.assertIn("every workspace will pay for a cold build", out)

    def test_a_prebuild_that_will_not_launch_warns_and_succeeds(self):
        cp, _ = self._drive('''
_vm_get()          { echo 8; }
build_jobs()       { echo 2; }
_push_tools()      { :; }
config_build_env() { CFG_ENV=(X=1); }
detach_remote()    { return 1; }
_prebuild_base 10.0.0.1 && echo "returned 0" || echo "returned $?"
echo "prebuilt=${_base_prebuilt:-none}"
''')
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("returned 0", cp.stdout, out)
        self.assertIn("prebuilt=none", cp.stdout, out)

    def test_a_prebuild_that_fails_still_leaves_the_base_marked(self):
        """The marker is what tells a finished base from rubble, so it is
        written whether or not the build in it worked -- and it records what
        the base got, not what was asked for: a marker naming the config
        would promise every workspace a warm build it will not get."""
        cp, marker = self._drive('''
_vm_get()     { echo 8; }
build_jobs()  { echo 2; }
_push_tools() { return 1; }
_prebuild_base 10.0.0.1
_base_mark_ready
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("prebuild=none", marker.read_text())

    def test_a_prebuild_that_worked_is_what_the_marker_names(self):
        cp, marker = self._drive('''
_vm_get()             { echo 8; }
build_jobs()          { echo 2; }
_push_tools()         { :; }
config_build_env()    { CFG_ENV=(X=1); }
detach_remote()       { :; }
detach_wait_remote()  { echo 0; }
scp()                 { :; }
_prebuild_base 10.0.0.1
_base_mark_ready
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("prebuild=mac-release", marker.read_text())

    def test_a_build_that_ran_and_failed_warns_and_succeeds(self):
        """The arm that costs the most to reach for real: an hour of building
        that ends non-zero. The base is sealed anyway -- the alternative is
        hours of provisioning thrown away over a warm build tree."""
        cp, _ = self._drive('''
_vm_get()             { echo 8; }
build_jobs()          { echo 2; }
_push_tools()         { :; }
config_build_env()    { CFG_ENV=(X=1); }
detach_remote()       { :; }
detach_wait_remote()  { echo 1; }
scp()                 { :; }
_prebuild_base 10.0.0.1 && echo "returned 0" || echo "returned $?"
echo "prebuilt=${_base_prebuilt:-none}"
''')
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("returned 0", cp.stdout, out)
        self.assertIn("prebuilt=none", cp.stdout, out)
        self.assertIn("base prebuild FAILED", out)
        self.assertIn("base-build.log", out)

    def test_a_failed_build_shows_the_first_error_when_that_is_loadable(self):
        """first_error is in lib/watchdog.sh, which not every caller of this
        driver has sourced -- so the report is the compiler's first line when
        it is there and the warning alone when it is not, never a
        `command not found` in place of the failure."""
        common = '''
_vm_get()             { echo 8; }
build_jobs()          { echo 2; }
_push_tools()         { :; }
config_build_env()    { CFG_ENV=(X=1); }
detach_remote()       { :; }
detach_wait_remote()  { echo 1; }
scp()                 { :; }
'''
        cp, _ = self._drive(common + '''
first_error() { echo "error: no member named foo"; }
_prebuild_base 10.0.0.1
''')
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("no member named foo", out)

        cp, _ = self._drive(common + '_prebuild_base 10.0.0.1\n')
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("base prebuild FAILED", out)
        self.assertNotIn("command not found", out)

    def test_the_prebuild_can_be_turned_off_and_nothing_is_asked_of_the_guest(self):
        """WK_VM_BASE_PREBUILD empty: the base is provisioned and sealed with
        a cold tree, and nothing reaches the guest at all.

        Emptied after the driver loaded, because the driver's own default
        (`${WK_VM_BASE_PREBUILD:-mac-release}`) fills an empty value back in
        -- so this drives the arm, not the way a person would reach it."""
        cp, _ = self._drive('''
WK_VM_BASE_PREBUILD=""
_push_tools() { echo "REACHED THE GUEST"; }
_prebuild_base 10.0.0.1 && echo "returned 0" || echo "returned $?"
echo "prebuilt=${_base_prebuilt:-none}"
''')
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("returned 0", cp.stdout, out)
        self.assertIn("prebuilt=none", cp.stdout, out)
        self.assertIn("base prebuild disabled", out)
        self.assertNotIn("REACHED THE GUEST", cp.stdout)

    def test_provisioning_stops_and_marks_the_base_unconditionally(self):
        """Source-level, because the steps between are hours of real work: the
        prebuild is not the marker's condition. `_prebuild_base ... || die`,
        or an `if` around what follows it, is the defect coming back."""
        body = func_body(VM.read_text(), "_provision_base")
        tail = body.split("_prebuild_base", 1)[1]
        self.assertNotIn("|| die", tail.splitlines()[0])
        for step in ('_tart stop "$WK_VM_BASE"', "_base_mark_ready"):
            self.assertIn(step, tail, f"_provision_base no longer runs {step}")
        self.assertNotIn("if ", tail, tail)


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
        return bash(f'{DRIVER}\n_tart_bin() {{ printf %s "{runner}"; }}\n'
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


class TestProvisioningOutlivesItsConnection(WkTest):
    """The base's first act is cloning all of WebKit, which is over an hour.
    Run in the foreground it dies with the ssh session (measured 2026-09-04:
    "Read from remote host: Connection reset by peer" took the clone with it),
    so it is detached and polled -- the shape _prebuild_base already uses."""

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

    def test_both_long_jobs_share_one_ssh_fn(self):
        text = VM.read_text()
        self.assertIn("_base_ssh() {", text)
        self.assertNotIn("_prebuild_ssh", text)


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


class TestTheConfigIsCheckedBeforeTheHours(WkTest):
    def _check(self, config):
        store = self.tmp / "store"
        store.mkdir(exist_ok=True)
        with stub_path({"tart": TART}) as binp:
            return bash(DRIVER + "_check_prebuild_config && echo accepted",
                        env={"WK_VM_STORE": str(store),
                             "WK_VM_BASE_PREBUILD": config,
                             "PATH": f"{binp}:{os.environ['PATH']}"})

    def test_a_config_that_builds_in_a_guest_is_accepted(self):
        cp = self._check("mac-release")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("accepted", cp.stdout)

    def test_no_prebuild_at_all_is_accepted(self):
        cp = self._check("")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("accepted", cp.stdout)

    def test_a_config_that_is_not_one_is_refused_by_name(self):
        cp = self._check("no-such-config")
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        out = cp.stdout + cp.stderr
        self.assertIn("no-such-config", out)
        self.assertIn("wk build --list", out)

    def test_the_question_is_the_one_config_load_answers(self):
        """Not a list of names kept here: the check is the same call the
        prebuild makes hours later, with the same platform, so the two cannot
        disagree about what is a config."""
        body = func_body(VM.read_text(), "_check_prebuild_config")
        self.assertIn('config_load "$WK_VM_BASE_PREBUILD" "$(t_os)"', body)

    def test_both_entry_points_check_it(self):
        """`wk vm base` reaches _ensure_base, `wk vm base --refresh` reaches
        _provision_base directly, and each is hours of work."""
        text = VM.read_text()
        for func in ("_ensure_base", "_provision_base"):
            with self.subTest(func=func):
                self.assertIn("_check_prebuild_config", func_body(text, func))
        # Before any work, not merely somewhere in it: the first thing
        # _ensure_base does to a machine is delete an unfinished base and pull
        # an image.
        body = func_body(text, "_ensure_base")
        self.assertLess(body.index("_check_prebuild_config"), body.index("_tart"),
                        "the config is checked after the base is touched")

    def test_every_config_load_in_the_driver_names_the_platform(self):
        """The defect: the one call in the tree that omitted it. config_load
        dies without a platform (build/configs.sh), and this driver's calls
        run at the very end of provisioning."""
        calls = re.findall(r"config_load\s+([^\n]*)", VM.read_text())
        self.assertTrue(calls, "no config_load in targets/vm.sh")
        for call in calls:
            with self.subTest(call=call):
                self.assertIn("t_os", call,
                              "config_load without the target's platform")


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
