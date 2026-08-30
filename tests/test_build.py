"""`wk build`: help/list surface, the reproducible `running:` command line,
per-target WK_BUILD_ARGS defaults (and --no-defaults), and the low-parallelism
warning in lib/resources.sh. Each docstring is the phrase of the behaviour it
checks.

Nothing here builds WebKit or touches real hardware: --dry-run, a
FakeWorkspace (an empty directory standing in for a checkout), and direct
calls into lib/resources.sh / build/configs.sh cover the logic without it.

Run: python3 -m unittest tests.test_build -v
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, bash, fake_workspace, run


class TestHelpAndList(WkTest):
    def test_build_list_shows_all_configs(self):
        """`wk build --list` prints the available configs"""
        cp = run("build", "--list")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("jsc-release", cp.stdout)
        self.assertIn("mac-release", cp.stdout)

    def test_build_help_mentions_list_dry_run_no_defaults(self):
        """`wk build -h` names --list, --dry-run and --no-defaults"""
        cp = run("build", "-h")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("--list", cp.stdout)
        self.assertIn("--dry-run", cp.stdout)
        self.assertIn("--no-defaults", cp.stdout)
        # The header's whole point for this defect: it says what --list and
        # --dry-run actually do, not just that they exist.
        self.assertIn("running:", cp.stdout)
        self.assertIn("WK_BUILD_ARGS", cp.stdout)


class TestDryRunRunningLine(WkTest):
    """A dry run against a fake workspace resolves everything and prints the
    exact command line, prefixed `running:`, with nothing built."""

    def test_dry_run_prints_running_line(self):
        """`wk build <config> --dry-run` prints a `running:` command line"""
        with fake_workspace() as ws:
            cp = ws.run("build", "jsc-release", "--dry-run")
            self.assertEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("dry run -- nothing was built", cp.stdout)
            lines = [l for l in cp.stdout.splitlines() if "running:" in l]
            self.assertEqual(len(lines), 1, cp.stdout)
            # One line, pastable: it names the checkout and the actual
            # build-webkit invocation, not the internal env/build-in-target.sh
            # plumbing above it.
            self.assertIn("cd ", lines[0])
            self.assertIn("build-webkit", lines[0])
            self.assertIn("--jsc-only", lines[0])

    def test_no_defaults_flag_is_consumed_not_passed_through(self):
        """--no-defaults is recognised, not forwarded to build-webkit"""
        with fake_workspace() as ws:
            cp = ws.run("build", "jsc-release", "--dry-run", "--no-defaults")
            self.assertEqual(cp.returncode, 0, cp.stdout)
            running = [l for l in cp.stdout.splitlines() if "running:" in l][0]
            self.assertNotIn("--no-defaults", running)


class TestTargetBuildArgsDefaults(unittest.TestCase):
    """Item 2: a target's conf can set WK_BUILD_ARGS, the same mechanism
    WK_TARGET_CMAKE already uses (targets/hosts/<name>.conf, load_target).
    Exercised at the two points that actually implement it, rather than
    through a full `wk new --target <remote>` (which needs a real machine):
    load_target's conf read, and config_build_env's use of it.
    """

    def test_load_target_reads_WK_BUILD_ARGS_from_conf(self):
        """load_target sources a target's WK_BUILD_ARGS the same way as WK_TARGET_CMAKE"""
        name = "wk-test-build-args-probe"
        # A registry of this one machine (WK_TARGET_REGISTRY, lib/target.sh):
        # the conf load_target reads is the behaviour under test, and the real
        # targets/hosts is left alone.
        registry = Path(tempfile.mkdtemp(prefix="wk-test-registry-"))
        self.addCleanup(shutil.rmtree, registry, True)
        (registry / f"{name}.conf").write_text(
            'WK_TARGET_KIND=remote\n'
            'WK_REMOTE_HOST=nonexistent.invalid\n'
            'WK_BUILD_ARGS="--no-fatal-warnings --extra-flag"\n'
        )
        cp = bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/resources.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/target.sh"
load_target "{name}"
echo "WK_BUILD_ARGS=[$WK_BUILD_ARGS]"
''', env={"WK_TARGET_REGISTRY": str(registry)})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("WK_BUILD_ARGS=[--no-fatal-warnings --extra-flag]", cp.stdout)

    def test_config_build_env_folds_in_target_build_args(self):
        """config_build_env appends WK_TARGET_BUILD_ARGS to the config's own flags"""
        cp = bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/arch.sh"
. "{REPO}/build/configs.sh"
config_load jsc-release
WK_TARGET_BUILD_ARGS="--no-fatal-warnings" config_build_env /src/WebKit 4 10 native
for e in "${{CFG_ENV[@]}}"; do case "$e" in WK_BUILD_ARGS=*) echo "$e" ;; esac; done
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("--jsc-only", cp.stdout)
        self.assertIn("--no-fatal-warnings", cp.stdout)

    def test_config_build_env_omits_it_when_unset(self):
        """no WK_TARGET_BUILD_ARGS (as --no-defaults arranges) -- nothing extra is added"""
        cp = bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/arch.sh"
. "{REPO}/build/configs.sh"
config_load jsc-release
config_build_env /src/WebKit 4 10 native
for e in "${{CFG_ENV[@]}}"; do case "$e" in WK_BUILD_ARGS=*) echo "$e" ;; esac; done
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "WK_BUILD_ARGS=--jsc-only --release")


class TestLowParallelismWarning(unittest.TestCase):
    """Item 3(a): explain_jobs (lib/resources.sh) warns, once, before the
    build, when the derived job count is under half the target's cores --
    forced here through the same env knobs cmd/build sets from a real target
    (export_target_resources): WK_CGROUP_CORES and WK_AVAIL_MB.
    """

    def _explain(self, script_env):
        env = "\n".join(f'{k}={v}' for k, v in script_env.items())
        cp = bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/resources.sh"
{env}
explain_jobs
''')
        return cp

    def test_warns_when_memory_starved(self):
        """a small memory envelope forcing few jobs warns with the reason"""
        cp = self._explain({"WK_CGROUP_CORES": 10, "WK_AVAIL_MB": 1000})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("parallelism:", cp.stderr)
        self.assertIn("under half of 10 cores", cp.stderr)

    def test_no_warning_with_ample_resources(self):
        """plenty of cores and memory: no warning"""
        cp = self._explain({"WK_CGROUP_CORES": 8, "WK_AVAIL_MB": 100000})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("parallelism:", cp.stderr)

    def test_no_warning_when_WK_MAX_JOBS_is_the_reason(self):
        """a deliberate WK_MAX_JOBS ceiling is not reported as a problem"""
        cp = self._explain({"WK_CGROUP_CORES": 10, "WK_AVAIL_MB": 100000, "WK_MAX_JOBS": 1})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("parallelism:", cp.stderr)


class TestStaleLoadAverage(unittest.TestCase):
    """Item 3(b): a killed build's dead compilers keep the 1-minute load
    average elevated for up to a minute. build_jobs treats a high load
    average as stale (and halves it) only when the memory envelope already
    looks idle -- the signature of a kill, not of a second build genuinely
    running (which would also be holding memory)."""

    def _jobs(self, script_env):
        env = "\n".join(f'{k}={v}' for k, v in script_env.items())
        cp = bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/resources.sh"
{env}
build_jobs polite
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return int(cp.stdout.strip())

    def test_stale_load_with_idle_memory_is_discounted(self):
        """high load + memory-idle machine -- not clamped to the load's face value"""
        stale = self._jobs({"WK_CGROUP_CORES": 10, "WK_AVAIL_MB": 100000, "WK_LOAD": 9})
        # Without the fix this would be 1 (10 cores - 9 load, still under the
        # half-a-box cap); the memory envelope says the machine is idle, so
        # the stale load is halved instead of trusted outright.
        self.assertGreater(stale, 1, "a stale-looking load average was trusted outright")

    def test_genuine_load_with_matching_memory_use_still_throttles(self):
        """high load + memory actually in use -- the load is trusted, not discounted"""
        busy = self._jobs({"WK_CGROUP_CORES": 20, "WK_AVAIL_MB": 23040, "WK_LOAD": 18})
        self.assertEqual(busy, 2, "a genuinely busy machine's load was discounted")


if __name__ == "__main__":
    unittest.main()
