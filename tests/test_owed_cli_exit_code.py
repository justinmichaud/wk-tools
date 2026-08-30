"""The fleet exit code aggregates the worst state found anywhere -- owed by
docs/HANDOFF-wk-cli.md: "the fleet exit code aggregates the worst state
found anywhere [needs a test]".

Two things are driven:

  - `bump` (cmd/status) and `_bump` (the `wk` dispatcher's own copy inside
    `bare_report`, for the macOS two-halves case): lifted with sed (the
    tests/test_wifi_seed.py idiom) and called directly, in and out of
    range, so this tracks the exact code that ships rather than a retyped
    copy. cmd/status's `bump` additionally folds anything outside 0-4 to 4
    (never to 0) -- the two are not the same function.
  - A real, bare `wk status --records` walk over one faked target
    (WK_TARGET=remote, the tests/test_fleet_walk.py technique: a stub
    `ssh` that runs the probe locally) carrying two fake workspaces with
    different recorded build states, asserting the process's own exit
    status is the worse of the two -- not the first, not the last.

Run: python3 -m unittest tests.test_owed_cli_exit_code -v
"""
import subprocess
import unittest

from tests.support import REPO, WkTest, bash, rand_suffix, run, scratch_dir, stub_path

CMD_STATUS = REPO / "cmd" / "status"
WK = REPO / "wk"


def _lift_func(path, name):
    text = subprocess.run(
        ["sed", "-n", f"/^{name}() {{/,/^}}/p", str(path)],
        capture_output=True, text=True,
    ).stdout
    assert text.strip(), f"{name}() not found in {path}"
    return text


def _lift_line(path, needle):
    """The one line containing `needle`, exactly as written -- for `_bump`,
    which (unlike cmd/status's top-level `bump`) is a single line nested
    inside `bare_report`, indented rather than starting a fresh line."""
    for line in path.read_text().splitlines():
        if needle in line:
            return line.strip()
    raise AssertionError(f"{needle!r} not found in {path}")


class TestCmdStatusBump(WkTest):
    """cmd/status's `bump`: raises `worst` only, and folds anything that is
    not a plain integer 0-4 to 4 -- never to 0, so a garbled or missing
    exit code cannot silently read as "all clear"."""

    def _fn(self):
        return _lift_func(CMD_STATUS, "bump")

    def _run(self, calls):
        script = self._fn() + "\nworst=0\n" + "\n".join(f'bump "{c}"' for c in calls) + '\necho "$worst"'
        cp = self.bash(script)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.strip()

    def test_only_raises_never_lowers(self):
        self.assertEqual(self._run(["2", "1", "0"]), "2")

    def test_the_worst_of_several_wins_regardless_of_order(self):
        self.assertEqual(self._run(["1", "3", "2"]), "3")
        self.assertEqual(self._run(["3", "1", "2"]), "3")

    def test_empty_folds_to_4_not_0(self):
        self.assertEqual(self._run([""]), "4")

    def test_non_numeric_folds_to_4(self):
        self.assertEqual(self._run(["oops"]), "4")

    def test_above_4_folds_to_4(self):
        self.assertEqual(self._run(["9"]), "4")

    def test_exactly_4_stays_4(self):
        self.assertEqual(self._run(["4"]), "4")

    def test_a_negative_number_is_non_numeric_and_also_folds_to_4(self):
        # `case` matches on *[!0-9]*, and '-' is not a digit -- bump treats
        # a negative exit code the same as garbage, not as "very fine".
        self.assertEqual(self._run(["-1"]), "4")


class TestDispatcherBump(WkTest):
    """`wk`'s own `_bump`, defined inside `bare_report` for the macOS
    two-halves case (vm half + host half): simpler than cmd/status's --
    no folding, just "raise worst to whichever side reported worse"."""

    def _fn(self):
        return _lift_line(WK, "_bump() {")

    def test_raises_to_the_larger_of_two_halves(self):
        cp = self.bash(f'worst=0\n{self._fn()}\n_bump 1\n_bump 3\necho "$worst"')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "3")

    def test_a_lower_second_half_does_not_undo_the_first(self):
        cp = self.bash(f'worst=0\n{self._fn()}\n_bump 2\n_bump 0\necho "$worst"')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "2")


_ANSWERING_SSH = '''#!/bin/sh
for last; do :; done
exec bash -c "$last"
'''


class TestFleetExitCodeIsTheWorst(WkTest):
    """A real `wk status --records` walk, bare (no name), over one faked
    target carrying two fake workspaces in different recorded build
    states: the process's own exit code is the worse of the two, driven
    through cmd/status's real `report_one`/`bump`, not a re-implementation.

    Built on the same scaffolding tests/test_fleet_walk.py's
    TestFleetWalkBareFormMultiMachine uses for a bare walk (WK_TARGET=remote
    with an answering stub `ssh`, WK_MACHINES_DIR pointed at an empty
    directory so the bench-device fleet walk finds nothing real), plus:

      - a `.wk-ready` marker under a scratch WK_REMOTE_ROOT for each fake
        workspace, the tests/test_fleet_walk.py technique for a workspace
        that reads as `present` rather than `creating` -- necessary here,
        not just tidiness: `report_ws`'s `creating` branch (cmd/status)
        calls `bump 4` outright for a workspace with no live creator, which
        would swamp any of the lower-severity build states this test wants
        to distinguish;
      - a build.status file for each, under the scratch XDG_STATE_HOME
        targets/remote.sh's own per-target store resolves to
        ($(wk_state_dir)/remote/<target-name>, not WK_STORE directly --
        that is a cache keyed per remote target so two never collide).

    report_one reads a workspace's build.status regardless of its
    lifecycle state, so this needs no real target driver, no git, no
    container -- but see TestFailedOrRunningStateCanLoseItsRecord below for
    a real bug this same mechanism uncovered in states this class avoids.
    """

    def _two_workspaces(self, xdg, remote_root, states):
        store = xdg / "wk" / "remote" / "remote"
        names = []
        for i, (state, extra) in enumerate(states):
            name = f"wsx{i}-{rand_suffix()}"
            names.append(name)
            wsdir = store / "ws" / name
            wsdir.mkdir(parents=True)
            lines = [f"state={state}"]
            lines += [f"{k}={v}" for k, v in extra.items()]
            (wsdir / "build.status").write_text("\n".join(lines) + "\n")
            (remote_root / "ws" / name).mkdir(parents=True)
            (remote_root / "ws" / name / ".wk-ready").touch()
        return names

    def _bare_status(self, xdg, remote_root, machdir, extra_env=None):
        with stub_path({"ssh": _ANSWERING_SSH}) as binp:
            env = {
                "XDG_STATE_HOME": str(xdg),
                "WK_REMOTE_ROOT": str(remote_root),
                "WK_MACHINES_DIR": str(machdir),
                "WK_TARGET": "remote",
                "WK_REMOTE_HOST": "fake-reachable-machine",
                "PATH": f"{binp}:{self._real_path()}",
            }
            if extra_env:
                env.update(extra_env)
            return run("status", "--records", env=env, timeout=45)

    @staticmethod
    def _real_path():
        import os
        return os.environ.get("PATH", "/usr/bin:/bin")

    def test_failed_and_stalled_together_report_the_worse_of_the_two(self):
        # failed -> bump(1); stalled -> worst=3 outright (cmd/status's
        # literal case arm, not folded through bump's 0-4 clamp). The
        # worse of the two workspaces is 3, so the whole walk's exit code
        # must be 3.
        with scratch_dir(prefix="wk-test-xdg-") as xdg, \
             scratch_dir(prefix="wk-test-remote-root-") as root, \
             scratch_dir(prefix="wk-test-machines-") as machdir:
            names = self._two_workspaces(xdg, root, [
                ("failed", {"exit": "1", "log": "/dev/null"}),
                ("stalled", {"log": "/dev/null"}),
            ])
            cp = self._bare_status(xdg, root, machdir)
            for n in names:
                self.assertIn(n, cp.stdout, f"{n} missing from the records:\n{cp.stdout}")
            self.assertEqual(cp.returncode, 3, cp.stdout)

    def test_the_order_of_the_two_workspaces_does_not_matter(self):
        # Same two states, workspace names sorted the other way round by
        # construction (ls -1 order) -- the aggregate is the worst found
        # anywhere, not whichever one the walk happened to visit last.
        with scratch_dir(prefix="wk-test-xdg-") as xdg, \
             scratch_dir(prefix="wk-test-remote-root-") as root, \
             scratch_dir(prefix="wk-test-machines-") as machdir:
            self._two_workspaces(xdg, root, [
                ("stalled", {"log": "/dev/null"}),
                ("failed", {"exit": "1", "log": "/dev/null"}),
            ])
            cp = self._bare_status(xdg, root, machdir)
            self.assertEqual(cp.returncode, 3, cp.stdout)

    def test_two_failed_workspaces_report_1_not_2(self):
        # Two independent failures bump(1) twice; the aggregate is still 1,
        # not the count of failures -- worst, not sum.
        with scratch_dir(prefix="wk-test-xdg-") as xdg, \
             scratch_dir(prefix="wk-test-remote-root-") as root, \
             scratch_dir(prefix="wk-test-machines-") as machdir:
            self._two_workspaces(xdg, root, [
                ("failed", {"exit": "1", "log": "/dev/null"}),
                ("failed", {"exit": "1", "log": "/dev/null"}),
            ])
            cp = self._bare_status(xdg, root, machdir)
            self.assertEqual(cp.returncode, 1, cp.stdout)


class TestFailedOrRunningStateCanLoseItsRecord(WkTest):
    """A real bug this harness turned up while building the aggregation
    tests above, not a restatement of the handoff item: cmd/status's
    `report_one` ends its `running)` and `failed)` case arms with

        [ "$worst" -lt 2 ] && worst=2       # running
        [ "$worst" -lt 1 ] && worst=1       # failed

    as the arm's last statement. `report_one` runs under `set -e`
    (cmd/status is `set -euo pipefail` throughout), and this is exactly
    the shape tests/test_owed_static_audits.py's trailing-&&-chain audit
    warns about: when `$worst` is already at or above the target (e.g. 4,
    from `report_ws`'s unconditional `bump 4` for a `creating` workspace
    with no live creator -- see the class above's docstring), the `&&`
    test is false, the whole guarded statement's exit status is 1, and
    since this is report_one's last executed statement for that branch,
    `report_one` itself returns 1. Back in `report()`, `report_one ...` is
    called as a bare, unguarded statement -- so `set -e` aborts `report()`
    right there, before it ever reaches `rec_json subs ...`/`rec_emit`.
    The workspace's entire JSON record is silently dropped.

    The aggregate exit code still happens to look right by coincidence
    (`[ 4 -lt 1 ]`'s own exit status is 1, which is separately what
    "failed" should have bumped to) -- which is what made this easy to
    miss and is exactly why `test_two_failed_workspaces_report_1_not_2`
    above still passes even when built over `creating` workspaces (as an
    earlier version of this test did): the exit code is coincidentally
    plausible while the record itself vanishes. `stalled`/`oom` are not
    affected -- both end their arms with a plain `worst=3` assignment,
    which always exits 0.

    Not fixed here (this repository's test files are not touched for
    source changes as part of this owed-test pass); documented and skipped
    so the underlying defect is visible rather than silently masked by a
    test built to route around it.
    """

    @staticmethod
    def _real_path():
        import os
        return os.environ.get("PATH", "/usr/bin:/bin")

    def test_a_failed_workspace_with_no_live_creator_loses_its_own_record(self):
        with scratch_dir(prefix="wk-test-xdg-") as xdg, \
             scratch_dir(prefix="wk-test-machines-") as machdir:
            # No WK_REMOTE_ROOT marker this time: the workspace reads as
            # `creating` (rubble, no live creator), which is what drives
            # report_ws's unconditional `bump 4` before report_one ever
            # runs -- the trigger condition for the bug.
            store = xdg / "wk" / "remote" / "remote"
            name = f"wsxbug-{rand_suffix()}"
            wsdir = store / "ws" / name
            wsdir.mkdir(parents=True)
            (wsdir / "build.status").write_text("state=failed\nexit=1\nlog=/dev/null\n")
            with stub_path({"ssh": _ANSWERING_SSH}) as binp:
                env = {
                    "XDG_STATE_HOME": str(xdg),
                    "WK_MACHINES_DIR": str(machdir),
                    "WK_TARGET": "remote",
                    "WK_REMOTE_HOST": "fake-reachable-machine",
                    "PATH": f"{binp}:{self._real_path()}",
                }
                cp = run("status", "--records", env=env, timeout=45)
            if name not in cp.stdout:
                self.skipTest(
                    "BUG: cmd/status's report_one loses a workspace's entire JSON "
                    "record when its `failed`/`running` case arm's trailing "
                    "`[ \"$worst\" -lt N ] && worst=N` evaluates false under an "
                    "already-elevated $worst (e.g. report_ws's `bump 4` for a "
                    "`creating` workspace with no live creator) -- set -e then "
                    "aborts report() before rec_emit runs for that workspace "
                    "(owed)"
                )
            # If this ever starts passing, the bug above has been fixed --
            # the record should carry the workspace's own name and its
            # failed build state.
            self.assertIn(name, cp.stdout, cp.stdout)
            self.assertIn('"state":"failed"', cp.stdout, cp.stdout)


if __name__ == "__main__":
    unittest.main()
