"""WK_* override coverage for cmd/bench, cmd/machine and cmd/build (the
docs/PLAN.md item: "every WK_* override read with a default is
documented where the user meets it and covered by a test, or removed").

Each test lifts the exact expression or function it exercises out of the
shell file with sed/regex rather than retyping it, so the test tracks the
real source instead of a copy that can drift (the way tests/test_wifi_seed.py
lifts _netplan_wifi). Nothing here touches real hardware, a podman VM, or a
workspace; WK_LOCK_DIR/WK_BENCH_ROOT/WK_IMAGE_MARKER point every test at a
scratch directory.

Run: python3 -m unittest tests.test_wk_overrides_cmd1 -v
"""
import re
import sys
import unittest

from tests.support import REPO, WkTest, bash, fake_workspace, run
from tests.test_bench_pipeline import BenchTest, World

from wk.act import Refused  # noqa: E402

BENCH = REPO / "lib" / "bench-arms.sh"
BUILD_PY = REPO / "lib" / "wk" / "build.py"


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


class TestBenchRunKnobs(BenchTest):
    """`wk bench run`'s own reads (lib/wk/bench/pipeline.py): WK_BENCH_MAX_LOAD is the 1-minute load
    average, rounded, above which preflight calls the machine busy (default 4); WK_BENCH_ASLR=off runs
    the benchmark under setarch -R, and unset runs it as it is."""

    def _load(self, load, env=None):
        self.w = World(self.tmp)
        self.w.files["/proc/loadavg"] = "%s 1.00 1.00 1/100 1\n" % load
        try:
            self.run_(extra=env)
        except Refused:
            return "busy"
        return "idle"

    def test_default_threshold_is_4(self):
        self.assertEqual((self._load("5.00"), self._load("4.00")), ("busy", "idle"))

    def test_override_raises_the_threshold(self):
        self.assertEqual(self._load("5.00", {"WK_BENCH_MAX_LOAD": "10"}), "idle")

    def test_aslr_off_prefixes_setarch(self):
        self.run_(extra={"WK_BENCH_ASLR": "off"})
        self.assertIn("setarch $(uname -m) -R -- ", self.w.watched[0][-1])

    def test_aslr_default_is_empty(self):
        self.run_()
        self.assertNotIn("setarch", self.w.watched[0][-1])


class TestTheWorkspaceLockIsRefusedNotWaitedOut(WkTest):
    """`wk build` refuses a second build in a workspace at once and names
    `wk build <ws> --kill`: an hour on a lock names no remedy. An image stage
    refuses the same way (lib/wk/sysimage/task.py's Stage.admit).
    """

    def test_build_asks_for_the_lock_with_no_wait_at_all(self):
        text = BUILD_PY.read_text()
        self.assertIn('lock.hold("ws-" + name, timeout=0)', text)
        self.assertNotIn("WK_BUILD_LOCK_WAIT", text)

    def test_the_refusal_names_the_command_that_stops_the_other_build(self):
        """The named refusal comes before the lock's own wait message could."""
        text = BUILD_PY.read_text()
        refusal = text[text.index('holder = lock.holder_pid("ws-" + name)'):]
        refusal = refusal[:refusal.index("lock.hold(")]
        self.assertIn("already building", refusal)
        self.assertIn("self.kill", refusal)


class TestBuildBabysitDefaults(WkTest):
    """--babysit's model/attempts defaults: WK_BABYSIT_MODEL is the model, WK_BABYSIT_ATTEMPTS how
    many fixes it tries before giving up (tests/test_wk_build.py runs both overridden)."""

    def test_babysit_model_and_attempts_defaults(self):
        sys.path.insert(0, str(REPO / "lib"))
        from wk import build
        self.assertEqual((build.BABYSIT_MODEL, build.BABYSIT_ATTEMPTS), ("haiku", 5))
        text = BUILD_PY.read_text()
        self.assertIn('env.get("WK_BABYSIT_MODEL") or BABYSIT_MODEL', text)
        self.assertIn('env.get("WK_BABYSIT_ATTEMPTS") or BABYSIT_ATTEMPTS', text)


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
    (Resources, lib/wk/resources.py). Set anyway (a leftover
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
            "WK_BABYSIT_MODEL", "WK_BABYSIT_ATTEMPTS", "WK_MEM_INTERVAL",
        ):
            self.assertIn(name, cp.stdout, f"{name} missing from `wk build -h`")


if __name__ == "__main__":
    unittest.main()
