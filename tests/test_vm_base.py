"""Building the golden macOS base: the last step must not be able to throw
away the first ones.

`wk vm base --rebuild` is hours -- an image pull, Xcode's first launch, a
WebKit mirror and checkout, then a full mac-release build -- and the completion
marker is written after all of it. Anything fatal in the late steps therefore
leaves a fully provisioned base with no marker, which the next run deletes as
rubble (_ensure_base). So: what is knowable up front is checked up front, the
prebuild cannot fail the provisioning it is the last step of, and the marker
records what the base actually got.

The password is the other thing every later step depends on: `sysadminctl`
exited 0 while leaving the account's password as the image's, which is why the
screen lock, `wk vm enter`'s note and `wk vm check` all disagreed with the run
that set it. Only `dscl . -authonly` decides here.

Hermetic: the driver's own functions are run against a stub `tart` and stubbed
helpers, and the password step is lifted out of vm/provision-base.sh and run
against stub `dscl`/`sysadminctl` -- no VM, no guest, no ssh.

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


class TestThePasswordIsProvedNotAssumed(WkTest):
    """vm/provision-base.sh's `_set_password`, lifted and run against stubs.
    `sysadminctl`'s exit status decides nothing; `dscl . -authonly` is the
    proof, taken before and after."""

    def _run(self, dscl_before, dscl_after, sysadminctl_rc=0):
        """<dscl_before>/<dscl_after>: whether the wanted password
        authenticates before and after the change is attempted. A stub `dscl`
        that answers differently on its second call is the case a reading of
        `sysadminctl`'s exit status alone gets wrong."""
        lifted = subprocess.run(
            ["sed", "-n", "/^_set_password()/,/^}/p", str(PROVISION)],
            capture_output=True, text=True).stdout
        self.assertTrue(lifted.strip(), "_set_password not found in vm/provision-base.sh")
        count = self.tmp / "dscl-calls"
        dscl = f'''#!/bin/sh
n=$(cat {str(count)!r} 2>/dev/null || echo 0)
n=$((n + 1)); echo "$n" > {str(count)!r}
[ "$n" = 1 ] && exit {0 if dscl_before else 1}
exit {0 if dscl_after else 1}
'''
        with stub_path({"dscl": dscl,
                        "sysadminctl": f"exit {sysadminctl_rc}"}) as binp:
            cp = bash(f'''
set -euo pipefail
say() {{ printf '==> %s\\n' "$*" >&2; }}
WK_VM_USER=admin
WK_VM_IMAGE_PASSWORD=admin
WK_VM_PASSWORD=1
{lifted}
_set_password
echo "password=$WK_VM_PASSWORD"
''', env={"PATH": f"{binp}:{os.environ['PATH']}"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout + cp.stderr

    def test_a_change_that_did_not_take_is_a_downgrade_not_a_success(self):
        """The measured case: exit status 0, password unchanged. Reported as
        what it is, and WK_VM_PASSWORD becomes what the account actually has,
        so vm/desktop.sh is handed the truth rather than the intent."""
        out = self._run(dscl_before=False, dscl_after=False, sysadminctl_rc=0)
        self.assertIn("still the image's", out, out)
        self.assertNotIn("password set for", out, out)
        self.assertIn("password=admin", out, out)

    def test_a_change_that_took_is_reported_once(self):
        out = self._run(dscl_before=False, dscl_after=True, sysadminctl_rc=0)
        self.assertIn("password set for admin", out, out)
        self.assertIn("password=1", out, out)

    def test_a_failing_tool_that_changed_it_anyway_counts_as_set(self):
        """The exit status decides nothing in either direction; the account
        authenticating with the wanted password is the whole test."""
        out = self._run(dscl_before=False, dscl_after=True, sysadminctl_rc=1)
        self.assertIn("password set for admin", out, out)

    def test_a_re_provision_offers_nothing(self):
        """sysadminctl needs the *current* password, so a base being
        re-provisioned must not offer the image's -- it is already changed."""
        out = self._run(dscl_before=True, dscl_after=True)
        self.assertIn("password already set", out, out)
        self.assertIn("password=1", out, out)

    def test_an_image_whose_password_is_already_the_wanted_one_is_left_alone(self):
        """The first line, and the only arm that touches nothing: when the two
        are the same string there is no change to make, and attempting one
        would need a current password that is also the new one."""
        calls = self.tmp / "calls"
        logger = f'#!/bin/sh\necho "$0 $*" >> {str(calls)!r}\n'
        lifted = subprocess.run(
            ["sed", "-n", "/^_set_password()/,/^}/p", str(PROVISION)],
            capture_output=True, text=True).stdout
        with stub_path({"dscl": logger, "sysadminctl": logger}) as binp:
            cp = bash(f'''
set -euo pipefail
say() {{ printf '==> %s\\n' "$*" >&2; }}
WK_VM_USER=admin
WK_VM_IMAGE_PASSWORD=1
WK_VM_PASSWORD=1
{lifted}
_set_password
echo "password=$WK_VM_PASSWORD"
''', env={"PATH": f"{binp}:{os.environ['PATH']}"})
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("password=1", out)
        self.assertFalse(calls.exists(),
                         f"the guest was asked something: {calls.read_text() if calls.exists() else ''}")

    def test_the_account_changes_its_own_password(self):
        """No sudo and no -adminUser: the reset form needs an admin
        credential, and this script runs as the account itself."""
        body = func_body(PROVISION.read_text(), "_set_password")
        self.assertIn("-oldPassword", body)
        self.assertNotIn("-resetPasswordFor", body)
        self.assertNotIn("sudo", body)


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
        self.assertIn("WK_VM_MIRROR=$(sh_quote \"$(t_mirror_dir",
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
