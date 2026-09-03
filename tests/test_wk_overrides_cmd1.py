"""WK_* override coverage for cmd/bench, cmd/bridge and cmd/build (the
docs/HANDOFF-test-runner.md item: "every WK_* override read with a default is
documented where the user meets it and covered by a test, or removed").

Each test lifts the exact expression or function it exercises out of the
shell file with sed/regex rather than retyping it, so the test tracks the
real source instead of a copy that can drift (the way tests/test_wifi_seed.py
lifts _netplan_wifi). Nothing here touches real hardware, a podman VM, or a
workspace; WK_LOCK_DIR/WK_BENCH_ROOT/WK_IMAGE_MARKER point every test at a
scratch directory.

Two things these tests pin down at the source level:

  - cmd/bench raises WK_STALL_SECONDS/WK_ABORT_SECONDS to 900/5400 *before*
    sourcing lib/watchdog.sh (whose own `:-300`/`:-1800` defaults must find
    these already set, not lock in first).
  - bench_padded_path uses two separate `local` statements for `dir` and
    `link`, because a single `local a=.. b=..` expands every RHS before
    assigning any of them, so `link="$dir/Y"` would read $dir's old value in
    every bash tested (3.2 and 5.2).

Run: python3 -m unittest tests.test_wk_overrides_cmd1 -v
"""
import re
import subprocess
import unittest

from tests.support import REPO, WkTest, bash, fake_workspace, run, scratch_dir

BENCH = REPO / "cmd" / "bench"
BRIDGE = REPO / "cmd" / "bridge"
BUILD = REPO / "cmd" / "build"


def _lift_func(path, name):
    """A function's body, sed'd out of a shell file (tests/test_wifi_seed.py's
    _lift) so the test calls the exact code that ships, not a hand copy."""
    text = subprocess.run(
        ["sed", "-n", f"/^{name}() {{/,/^}}/p", str(path)],
        capture_output=True, text=True,
    ).stdout
    assert text.strip(), f"{name}() not found in {path}"
    return text


def _lift_range(path, start_needle, end_needle, end_exact=False):
    """Lines from the first line containing start_needle through the next
    line containing (or, if end_exact, equal to, stripped) end_needle
    (inclusive), sliced out of the file in Python rather than shelling out
    to sed: BSD sed's own delimiter (`/`) and interval syntax (`\\{`)
    collide with slashes and braces that show up in real shell (a path,
    `${VAR}`), so a plain substring search here is the more literal lift."""
    lines = path.read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if start_needle in l)
    if end_exact:
        end = next(i for i in range(start, len(lines)) if lines[i].strip() == end_needle)
    else:
        end = next(i for i in range(start, len(lines)) if end_needle in lines[i])
    return "\n".join(lines[start:end + 1])


def _extract_expr(path, needle_re):
    """The one line in path matching needle_re, exactly as written -- so a
    test of a `${WK_X:-default}` read exercises the real default, not a
    retyped one that can drift out of sync."""
    text = path.read_text()
    m = re.search(needle_re, text, re.M)
    assert m, f"{needle_re!r} not found in {path}"
    return m.group(0)


class TestBenchWatchdogOrdering(WkTest):
    """cmd/bench raises the build watchdog's defaults for a benchmark's
    longer legitimate silence; the raise has to land before lib/watchdog.sh
    is sourced, not after, or its own :-300/:-1800 defaults win."""

    def _snippet(self):
        return _lift_range(
            BENCH,
            'WK_STALL_SECONDS="${WK_STALL_SECONDS:-900}"',
            '. "$WK_ROOT/lib/watchdog.sh"',
        )

    def test_defaults_are_the_bench_values_not_the_build_watchdogs(self):
        """WK_STALL_SECONDS=900, WK_ABORT_SECONDS=5400 by default in cmd/bench"""
        script = f'. "{REPO}/lib/common.sh"\n{self._snippet()}\necho "$WK_STALL_SECONDS $WK_ABORT_SECONDS"'
        cp = bash(script)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "900 5400", cp.stdout + cp.stderr)

    def test_an_explicit_override_still_wins(self):
        """WK_STALL_SECONDS/WK_ABORT_SECONDS set in the environment survive the source"""
        script = f'. "{REPO}/lib/common.sh"\n{self._snippet()}\necho "$WK_STALL_SECONDS $WK_ABORT_SECONDS"'
        cp = bash(script, env={"WK_STALL_SECONDS": "12", "WK_ABORT_SECONDS": "34"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "12 34", cp.stdout + cp.stderr)


class TestBenchVarianceKnobs(WkTest):
    """WK_BENCH_ASLR, WK_BENCH_ENV_PAD, WK_BENCH_PATH_PAD and
    WK_BENCH_SHARED_CACHE: documented in cmd/bench's -h header (bench/*.sh
    and cmd/pi read the same names, owned by whoever owns those files)."""

    def test_aslr_off_prefixes_setarch(self):
        fn = _lift_func(BENCH, "bench_aslr_wrap")
        cp = bash(f"{fn}\nWK_BENCH_ASLR=off bench_aslr_wrap")
        self.assertIn("setarch", cp.stdout, cp.stdout + cp.stderr)

    def test_aslr_default_is_empty(self):
        fn = _lift_func(BENCH, "bench_aslr_wrap")
        cp = bash(f"{fn}\nbench_aslr_wrap; echo END")
        self.assertEqual(cp.stdout.strip(), "END", cp.stdout + cp.stderr)

    def test_env_pad_exports_a_dummy_of_the_requested_size(self):
        fn = _lift_func(BENCH, "bench_env_pad_prelude")
        cp = bash(f'{fn}\nWK_BENCH_ENV_PAD=200 bench_env_pad_prelude')
        self.assertIn("head -c 200", cp.stdout, cp.stdout + cp.stderr)

    def test_env_pad_default_is_nothing(self):
        fn = _lift_func(BENCH, "bench_env_pad_prelude")
        cp = bash(f"{fn}\nbench_env_pad_prelude; echo END")
        self.assertEqual(cp.stdout.strip(), "END", cp.stdout + cp.stderr)

    def test_path_pad_creates_a_symlink_under_the_padded_directory(self):
        """regression test for the `local dir=X link=$dir/Y` bug: link must
        land under $dir, not at the filesystem root"""
        fn = _lift_func(BENCH, "bench_padded_path")
        cp = bash(f'{fn}\nbench_padded_path "" /tmp/wk-test-pad-target; printf "\\n"',
                   env={"WK_BENCH_PATH_PAD": "5"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        link = cp.stdout.strip()
        self.assertTrue(
            link.startswith("/tmp/wk-bench-pad-"),
            f"padded link should live under /tmp/wk-bench-pad-*, got {link!r}",
        )
        self.assertTrue(link.endswith("/wk-test-pad-target"), link)

    def test_path_pad_default_leaves_the_target_unchanged(self):
        fn = _lift_func(BENCH, "bench_padded_path")
        cp = bash(f'{fn}\nbench_padded_path "" /tmp/wk-test-pad-target; printf "\\n"')
        self.assertEqual(cp.stdout.strip(), "/tmp/wk-test-pad-target", cp.stdout + cp.stderr)

    def test_shared_cache_avoid_sets_dyld_shared_region(self):
        expr = _extract_expr(
            BENCH, r'\[ "\$\{WK_BENCH_SHARED_CACHE:-\}" = avoid \].*$',
        )
        cp = bash(f'shared_cache_env=""\n{expr}\necho "$shared_cache_env"',
                   env={"WK_BENCH_SHARED_CACHE": "avoid"})
        self.assertIn("DYLD_SHARED_REGION=avoid", cp.stdout, cp.stdout + cp.stderr)

    def test_shared_cache_default_leaves_it_unset(self):
        expr = _extract_expr(
            BENCH, r'\[ "\$\{WK_BENCH_SHARED_CACHE:-\}" = avoid \].*$',
        )
        cp = bash(f'shared_cache_env=""\n{expr}\necho "[$shared_cache_env]"')
        self.assertEqual(cp.stdout.strip(), "[]", cp.stdout + cp.stderr)


class TestBenchRootAndMachine(WkTest):
    """staged_root: WK_BENCH_ROOT is the escape hatch host mode uses to
    reach bench mode's code path without rebooting into it (tested already
    by tests/test_quick.py's test_bench_role_required_or_it_does_not_run);
    WK_BENCH_MACHINE is the only way host mode can tell which fleet Mac it
    is running on -- there is no default, by design."""

    def _fn(self):
        return _lift_func(BENCH, "staged_root")

    def test_bench_root_wins_over_everything_else(self):
        with scratch_dir(prefix="wk-test-bench-root-") as d:
            explicit = d / "explicit"
            cp = bash(
                f'. "{REPO}/lib/common.sh"\n{self._fn()}\nstaged_root; printf "\\n"',
                env={"WK_BENCH_ROOT": str(explicit), "WK_IMAGE_MARKER": str(d / "no-marker")},
            )
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual(cp.stdout.strip(), str(explicit))

    def test_bench_machine_unset_refuses_with_its_own_reason(self):
        with scratch_dir(prefix="wk-test-bench-machine-") as d:
            cp = bash(
                f'. "{REPO}/lib/common.sh"\n{self._fn()}\nrc=0; staged_root || rc=$?; echo "rc=$rc"',
                env={"WK_IMAGE_MARKER": str(d / "no-marker"), "WK_DEBUG": "1"},
            )
            self.assertIn("rc=1", cp.stdout, cp.stdout + cp.stderr)
            self.assertIn("WK_BENCH_MACHINE is not set", cp.stderr, cp.stdout + cp.stderr)

    def test_bench_machine_set_takes_a_different_path(self):
        """with WK_BENCH_MACHINE set, staged_root gets past the "not set"
        early return -- it still fails (no such fleet machine here), but not
        for the same reason, which is the observable effect of the override"""
        with scratch_dir(prefix="wk-test-bench-machine-") as d:
            cp = bash(
                f'. "{REPO}/lib/common.sh"\n{self._fn()}\nrc=0; staged_root || rc=$?; echo "rc=$rc"',
                env={
                    "WK_IMAGE_MARKER": str(d / "no-marker"),
                    "WK_DEBUG": "1",
                    "WK_BENCH_MACHINE": "wk-test-nonexistent-machine",
                },
            )
            self.assertIn("rc=1", cp.stdout, cp.stdout + cp.stderr)
            self.assertNotIn("WK_BENCH_MACHINE is not set", cp.stderr, cp.stdout + cp.stderr)


class TestBenchPython(WkTest):
    """staged_python: WK_BENCH_PYTHON overrides the search for a python3
    with `objc` importable, and a bad override falls back rather than
    breaking the run."""

    def _fn(self):
        return _lift_func(BENCH, "staged_python")

    def test_override_is_tried_first_and_used_if_it_works(self):
        with scratch_dir(prefix="wk-test-bench-python-") as d:
            fake = d / "fake-python3"
            fake.write_text("#!/bin/sh\nexit 0\n")
            fake.chmod(0o755)
            cp = bash(f'{self._fn()}\nstaged_python; printf "\\n"',
                       env={"WK_BENCH_PYTHON": str(fake)})
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual(cp.stdout.strip(), str(fake))

    def test_a_broken_override_falls_back_instead_of_failing(self):
        with scratch_dir(prefix="wk-test-bench-python-") as d:
            cp = bash(f'{self._fn()}\nstaged_python; printf "\\n"',
                       env={"WK_BENCH_PYTHON": str(d / "does-not-exist")})
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertNotEqual(cp.stdout.strip(), str(d / "does-not-exist"))


class TestBenchMaxLoad(WkTest):
    """preflight's idle check: WK_BENCH_MAX_LOAD is the 1-minute load
    average above which the machine is called busy (default 4)."""

    def _expr(self):
        return _extract_expr(BENCH, r'\[ "\$\(printf .*WK_BENCH_MAX_LOAD:-4\}" \]')

    def test_default_threshold_is_4(self):
        expr = self._expr()
        busy = bash(f'load=5\n{expr} && echo busy || echo idle')
        idle = bash(f'load=4\n{expr} && echo busy || echo idle')
        self.assertEqual(busy.stdout.strip(), "busy", busy.stdout + busy.stderr)
        self.assertEqual(idle.stdout.strip(), "idle", idle.stdout + idle.stderr)

    def test_override_raises_the_threshold(self):
        expr = self._expr()
        cp = bash(f'load=5\n{expr} && echo busy || echo idle', env={"WK_BENCH_MAX_LOAD": "10"})
        self.assertEqual(cp.stdout.strip(), "idle", cp.stdout + cp.stderr)


class TestBridgeAuthkey(WkTest):
    """cmd/bridge: WK_BRIDGE_AUTHKEY is the scriptable escape hatch for an
    otherwise hands-only step (pasting a tailnet auth key), and takes
    precedence over the fleet's own key file."""

    def _snippet(self):
        return _lift_range(
            BRIDGE,
            'if [ -n "${WK_BRIDGE_AUTHKEY:-}" ]; then',
            "fi",
            end_exact=True,
        )

    def test_env_var_wins_and_the_fleet_key_is_not_even_consulted(self):
        snippet = self._snippet()
        cp = bash(
            'log() { :; }\n'
            'wk_tailscale_authkey() { echo CALLED >&2; return 1; }\n'
            'authkey=""; keyfile=""; BR_NAME=probe\n'
            f'{snippet}\n'
            'echo "$authkey"',
            env={"WK_BRIDGE_AUTHKEY": "secret-from-env"},
        )
        self.assertEqual(cp.stdout.strip(), "secret-from-env", cp.stdout + cp.stderr)
        self.assertNotIn("CALLED", cp.stderr, "the fleet key lookup ran despite the override")

    def test_unset_falls_back_to_the_fleet_key(self):
        with scratch_dir(prefix="wk-test-bridge-authkey-") as d:
            keyfile = d / "key"
            keyfile.write_text("fleet-key-value\n")
            snippet = self._snippet()
            cp = bash(
                'log() { :; }\n'
                f'wk_tailscale_authkey() {{ echo {keyfile}; }}\n'
                'authkey=""; keyfile=""; BR_NAME=probe\n'
                f'{snippet}\n'
                'echo "$authkey"',
            )
            self.assertEqual(cp.stdout.strip(), "fleet-key-value", cp.stdout + cp.stderr)


class TestBuildLockWait(WkTest):
    """WK_BUILD_LOCK_WAIT: how long `wk build` waits for another build on
    the same workspace before giving up (default 3600s). image/yocto.sh
    reads the same name for its own lock, at the same 3600 default."""

    def _expr(self):
        return _extract_expr(BUILD, r'hold_lock "ws-\$NAME".*$')

    def test_default_is_3600(self):
        text = BUILD.read_text()
        self.assertIn('hold_lock "ws-$NAME" -w "${WK_BUILD_LOCK_WAIT:-3600}"', text)

    def test_override_actually_bounds_the_wait(self):
        """a build that cannot get the lock within WK_BUILD_LOCK_WAIT gives
        up in that many seconds, not lib/common.sh's own 600s default"""
        with scratch_dir(prefix="wk-test-build-lock-") as d:
            lockdir = d / "locks"
            holder = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
export WK_LOCK_DIR="{lockdir}"
hold_lock ws-probe -w 20
sleep 30
'''
            (d / "holder.sh").write_text(holder)
            proc = subprocess.Popen(["bash", str(d / "holder.sh")])
            self.addCleanup(proc.kill)
            try:
                import time
                time.sleep(0.5)
                start = time.monotonic()
                cp = bash(
                    f'set -euo pipefail\n. "{REPO}/lib/common.sh"\nNAME=probe\n{self._expr()}',
                    env={"WK_LOCK_DIR": str(lockdir), "WK_BUILD_LOCK_WAIT": "2"},
                    timeout=15,
                )
                elapsed = time.monotonic() - start
            finally:
                proc.kill()
                proc.wait(timeout=5)
            self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("within 2s", cp.stdout + cp.stderr)
            self.assertLess(elapsed, 10, "should have given up around 2s, not waited for lib/common.sh's 600s default")


class TestBuildBabysitDefaults(WkTest):
    """--babysit's model/attempts defaults: WK_BABYSIT_MODEL is what bare
    `--babysit` (no `=model`) uses, WK_BABYSIT_ATTEMPTS how many fixes it
    tries before giving up."""

    def test_babysit_model_default_and_override(self):
        expr = _extract_expr(BUILD, r'BABYSIT="\$\{WK_BABYSIT_MODEL:-haiku\}"')
        default = bash(f'{expr}\necho "$BABYSIT"')
        override = bash(f'{expr}\necho "$BABYSIT"', env={"WK_BABYSIT_MODEL": "sonnet"})
        self.assertEqual(default.stdout.strip(), "haiku")
        self.assertEqual(override.stdout.strip(), "sonnet")

    def test_babysit_attempts_default_and_override(self):
        expr = _extract_expr(BUILD, r'"\$\{WK_BABYSIT_ATTEMPTS:-5\}"')
        default = bash(f'echo {expr}')
        override = bash(f'echo {expr}', env={"WK_BABYSIT_ATTEMPTS": "9"})
        self.assertEqual(default.stdout.strip(), "5")
        self.assertEqual(override.stdout.strip(), "9")


class TestBuildMemInterval(WkTest):
    """WK_MEM_INTERVAL: how often the memory watchdog samples (default 30s,
    documented in `wk build -h`). No flag of its own -- read directly at
    the one place that actually sleeps on it, build/mem-watchdog.sh."""

    def test_watchdog_interval_default_and_override(self):
        expr = _extract_expr(
            REPO / "build" / "mem-watchdog.sh", r'INTERVAL="\$\{WK_MEM_INTERVAL:-30\}"',
        )
        default = bash(f'{expr}\necho "$INTERVAL"')
        override = bash(f'{expr}\necho "$INTERVAL"', env={"WK_MEM_INTERVAL": "5"})
        self.assertEqual(default.stdout.strip(), "30")
        self.assertEqual(override.stdout.strip(), "5")


class TestRemoteMaxJobsTombstone(WkTest):
    """WK_REMOTE_MAX_JOBS: a name no conf sets any more -- the job count is
    always derived per build from what the target has free
    (export_target_resources, lib/resources.sh). Set anyway (a leftover
    conf line), cmd/build warns and names the fix rather than silently
    reading it -- CLAUDE.md's tombstone shape, a name the tooling still
    refuses, naming its replacement."""

    def test_set_in_the_environment_warns_and_names_the_remedy(self):
        with fake_workspace() as ws:
            cp = ws.run("build", "jsc-release", "--dry-run", env={"WK_REMOTE_MAX_JOBS": "8"})
            self.assertEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("WK_REMOTE_MAX_JOBS is set", cp.stdout)
            self.assertIn("ignored", cp.stdout)

    def test_unset_prints_nothing_about_it(self):
        with fake_workspace() as ws:
            cp = ws.run("build", "jsc-release", "--dry-run")
            self.assertEqual(cp.returncode, 0, cp.stdout)
            self.assertNotIn("WK_REMOTE_MAX_JOBS", cp.stdout)


class TestBenchBrowserRemoved(WkTest):
    """WK_BENCH_BROWSER was a second implementation of --browser (same
    knob, two ways); removed rather than documented, per CLAUDE.md's "one
    implementation per behaviour". `--browser` alone now sets it."""

    def test_no_env_var_fallback_remains_in_the_source(self):
        text = BENCH.read_text()
        self.assertNotIn("WK_BENCH_BROWSER", text, "WK_BENCH_BROWSER should be fully removed, not just undocumented")

    def test_browser_flag_still_documented_and_wired(self):
        cp = run("bench", "-h")
        self.assertIn("browser-args", cp.stdout)


class TestHeaderDocumentsTheKnobsThisModuleTests(unittest.TestCase):
    """Every tunable this module keeps is explained in its command's -h
    header (`wk <cmd> -h`), per CLAUDE.md: "documented where the user meets
    it" -- not just in a runtime warn/log string."""

    def test_bench_header_names_its_tunables(self):
        cp = run("bench", "-h")
        for name in (
            "WK_BENCH_ASLR", "WK_BENCH_ENV_PAD", "WK_BENCH_PATH_PAD",
            "WK_BENCH_SHARED_CACHE", "WK_BENCH_MACHINE", "WK_BENCH_ROOT",
            "WK_BENCH_PYTHON", "WK_BENCH_MAX_LOAD",
            "WK_STALL_SECONDS", "WK_ABORT_SECONDS",
        ):
            self.assertIn(name, cp.stdout, f"{name} missing from `wk bench -h`")

    def test_build_header_names_its_tunables(self):
        cp = run("build", "-h")
        for name in (
            "WK_BABYSIT_MODEL", "WK_BABYSIT_ATTEMPTS", "WK_BUILD_LOCK_WAIT",
            "WK_MEM_INTERVAL",
        ):
            self.assertIn(name, cp.stdout, f"{name} missing from `wk build -h`")

    def test_bridge_header_names_its_tunable(self):
        cp = run("bridge", "-h")
        self.assertIn("WK_BRIDGE_AUTHKEY", cp.stdout)


if __name__ == "__main__":
    unittest.main()
