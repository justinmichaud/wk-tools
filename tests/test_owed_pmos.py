"""pmos build-host behaviour owed by docs/HANDOFF-test-runner.md:

  - `wk sysimage build <pmos profile>` prints something on failure, rather
    than `set -o pipefail` swallowing the remote build's own output
    (pmos_follow, image/pmos.sh)
  - a second `wk sysimage build` refuses while one runs, matched by `pgrep
    -f` against the ssh command line that carries the build script's own
    name (pmos_spawn, image/pmos.sh)

Item 15 in that handoff also names the yocto/buildroot side of "a second
build refuses while one runs" as another agent's (tests/test_yocto_stage.py);
this file does only the pmos half, which has its own, independent
`pgrep -f 'pmos-build[.]sh'` check.

Both are lifted fragments (`_pmos_ask`/`pmos_host`/`pmos_ssh`/`detach_wait_remote`
stubbed), not a full `pmos_build`: no ssh, no build host, no network.

Run: python3 -m unittest tests.test_owed_pmos -v
"""
import subprocess
import unittest

from tests.support import REPO, WkTest

PMOS = REPO / "image" / "pmos.sh"


def _lift_func(func):
    text = subprocess.run(
        ["sed", "-n", f"/^{func}()/,/^}}/p", str(PMOS)],
        capture_output=True, text=True,
    ).stdout
    assert text.strip(), f"could not lift {func} from {PMOS}"
    return text


def _lift_running_check(rest_of_the_function=""):
    """The one-build-at-a-time check inside pmos_spawn -- not a whole
    function, so lifted by its surrounding comment and closing quote rather
    than by name, and wrapped in a function of its own so its `local`
    survives outside pmos_spawn's body.

    `rest_of_the_function` matters, not just cosmetically: in the real
    pmos_spawn this check is a bare mid-function statement, followed by more
    of the function's own body -- exactly the shape `set -e` treats
    specially (a `[ x ] && y` whose `[ x ]` is false does *not* end the
    script when it is a plain statement with more statements to fall through
    to; wrapping it as a function's *only*/*last* statement changes that,
    since the function call's own exit status is then what -e sees). Passing
    something to run after it keeps this test honest about which shape it is
    checking.
    """
    # Anchored on the check itself, not on a comment above it: a comment is
    # not structure, and slicing by one made this test demand it keep existing.
    text = subprocess.run(
        ["sed", "-n",
         r"/^    local running$/,/build\.log\"$/p",
         str(PMOS)],
        capture_output=True, text=True,
    ).stdout
    assert "pgrep -f" in text, f"could not lift the running-build check from {PMOS}"
    return "_pmos_running_check() {\n" + text + "\n" + rest_of_the_function + "\n}\n"


class TestPmosFollowReportsAFailure(WkTest):
    """pmos_follow: the remote build's own exit code, read back after
    set -o pipefail would otherwise have hidden ssh's exit status behind a
    successful local pipe stage. `detach_wait_remote` (lib/detach.sh) is
    stubbed to hand back a failing code directly, so this is pmos_follow's
    own reporting, not detach_wait_remote's polling."""

    def _follow(self, rc):
        script = f'''
. "$WK_ROOT/lib/common.sh"
{_lift_func("pmos_follow")}
IMG_PROFILE=test-profile
pmos_host() {{ echo buildhost1; }}
pmos_log()  {{ echo /home/x/wk-pmos/out/$1/build.log; }}
pmos_rc()   {{ echo /home/x/wk-pmos/out/$1/build.rc; }}
detach_wait_remote() {{ echo {rc}; }}
pmos_follow test-profile-20260101T000000Z
'''
        return self.bash(script)

    def test_a_failed_remote_build_dies_naming_the_host_code_and_log(self):
        cp = self._follow(1)
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertIn("the build failed on buildhost1", out, out)
        self.assertIn("exit 1", out, out)
        self.assertIn("build.log", out, out)

    def test_a_successful_remote_build_reports_nothing_and_continues(self):
        cp = self._follow(0)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("failed", cp.stdout + cp.stderr)

    def test_a_lost_connection_names_resume_rather_than_a_bare_failure(self):
        """detach_wait_remote handing back nothing (never saw an rc file) is
        not the same as an exit code of 1 -- it gets its own message naming
        `--resume`, not "the build failed"."""
        cp = self._follow("")
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertIn("lost track of the build", out, out)
        self.assertIn("--resume", out, out)


class TestPmosRefusesASecondConcurrentBuild(WkTest):
    """pmos_spawn's own one-build-at-a-time check: `pgrep -f
    'pmos-build[.]sh'` on the build host, asked over the same ssh
    (`_pmos_ask`) that carries every other question this driver asks it."""

    def _preamble(self, ask_output):
        # Mirrors pmos_spawn's real shape: the check, then more of the
        # function's own body -- "would proceed to start a build" stands in
        # for `info "starting the build on ..."` and everything after it.
        return f'''
. "$WK_ROOT/lib/common.sh"
pmos_host() {{ echo buildhost1; }}
pmos_ssh()  {{ echo buildhost1; }}
_pmos_ask() {{ printf '%s' {ask_output!r}; }}
{_lift_running_check('echo "would proceed to start a build"')}
_pmos_running_check
'''

    def test_a_build_already_running_refuses_and_names_the_host(self):
        # `die` is `exit`, not `return` -- it ends the whole script (never
        # reaching the rest of the function), and the refusal is read off
        # the process the way `wk sysimage build` itself would fail.
        cp = self.bash(self._preamble("yes"))
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertIn("a pmos build is already running on buildhost1", out, out)
        self.assertNotIn("would proceed", out, out)

    def test_no_build_running_lets_a_new_one_proceed(self):
        cp = self.bash(self._preamble(""))
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("would proceed to start a build", out, out)
        self.assertNotIn("already running", out, out)

    def test_the_pattern_is_bracketed_so_the_asking_ssh_does_not_match_itself(self):
        """Static: the file's own comment explains why -- `pgrep -f` matches
        every process's full command line, including the ssh carrying this
        very check, which contains the plain spelling of the script name."""
        text = PMOS.read_text()
        self.assertIn("pgrep -f 'pmos-build[.]sh'", text)


if __name__ == "__main__":
    unittest.main()
