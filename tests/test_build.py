"""`wk build`: help/list surface, the reproducible `running:` command line,
per-target WK_BUILD_ARGS defaults (and --no-defaults), and the low-parallelism
warning in lib/resources.sh. Each docstring is the phrase of the behaviour it
checks.

Nothing here builds WebKit or touches real hardware: --dry-run, a
FakeWorkspace (an empty directory standing in for a checkout), and direct
calls into lib/resources.sh / build/configs.sh cover the logic without it.

Run: python3 -m unittest tests.test_build -v
"""
import platform
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
            # Tools/Scripts invocation, not the internal env/build-in-target.sh
            # plumbing above it. Which script, and whether there is a port
            # flag, is the platform's answer -- a fake workspace is the `local`
            # target, so it is this machine's (see TestMacJscUsesXcode).
            self.assertIn("cd ", lines[0])
            if platform.system() == "Darwin":
                self.assertIn("build-jsc", lines[0])
                self.assertNotIn("--jsc-only", lines[0])
            else:
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
config_load jsc-release linux
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
config_load jsc-release linux
config_build_env /src/WebKit 4 10 native
for e in "${{CFG_ENV[@]}}"; do case "$e" in WK_BUILD_ARGS=*) echo "$e" ;; esac; done
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        # The config's port and type, plus the all-config defaults
        # (_CFG_DEFAULT_ARGS, build/configs.sh) -- and nothing from a machine.
        self.assertEqual(cp.stdout.strip(),
                         "WK_BUILD_ARGS=--jsc-only --no-fatal-warnings --release")


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


class TestAllConfigDefaults(unittest.TestCase):
    """_CFG_DEFAULT_ARGS / _CFG_DEFAULT_CMAKE (build/configs.sh): the flags
    that are a property of how this repository builds WebKit rather than of any
    one config. Every CMake config starts with them, its own flags are appended
    after and win, and the Xcode configs get neither -- xcodebuild takes no -D
    flags."""

    def _flags(self, config, os="linux"):
        cp = bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/arch.sh"
. "{REPO}/build/configs.sh"
config_load {config} {os}
echo "ARGS=$CFG_ARGS"
echo "CMAKE=$CFG_CMAKE"
''')
        assert cp.returncode == 0, cp.stdout + cp.stderr
        out = dict(l.split("=", 1) for l in cp.stdout.strip().splitlines())
        return out["ARGS"], out["CMAKE"]

    # Which build system a config uses is a property of the *platform* as well
    # as the config, so each list carries the one it is that on.
    CMAKE_CONFIGS = ("jsc-debug", "jsc-release", "jsc-release-asan",
                     "gtk-debug", "gtk-release", "gtk-release-asan",
                     "wpe-release")
    XCODE_CONFIGS = ("mac-debug", "mac-release", "mac-release-asan",
                     "ios-sim-release")

    def test_every_cmake_config_starts_with_them(self):
        for config in self.CMAKE_CONFIGS:
            args, cmake = self._flags(config)
            with self.subTest(config=config):
                self.assertTrue(args.startswith("--no-fatal-warnings "), args)
                for flag in ("-DDEVELOPER_MODE=ON", "-DUSE_VULKAN=OFF",
                             "-DENABLE_THUNDER=OFF", "-DUSE_LIBBACKTRACE=OFF"):
                    self.assertIn(flag, cmake, f"{config}: {flag} missing")

    def test_no_xcode_config_gets_them(self):
        """build-webkit's Apple path has no --no-fatal-warnings and xcodebuild
        ignores -D, so a default reaching them would be a broken build."""
        for config in self.XCODE_CONFIGS:
            args, cmake = self._flags(config, "macos")
            with self.subTest(config=config):
                self.assertNotIn("--no-fatal-warnings", args)
                self.assertEqual(cmake, "")

    def test_a_config_overrides_a_default_by_stating_the_opposite(self):
        """cmake takes the last repeated -D, so a config's own flag wins:
        gtk-debug wants libbacktrace where the default turns it off."""
        _, cmake = self._flags("gtk-debug")
        self.assertLess(cmake.index("-DUSE_LIBBACKTRACE=OFF"),
                        cmake.index("-DUSE_LIBBACKTRACE=ON"),
                        "the config's flag does not come last, so the default wins")

    def test_no_config_repeats_a_default_it_agrees_with(self):
        """A config states only what differs: a second copy of a default is a
        line somebody has to keep in step with the default forever."""
        for config in self.CMAKE_CONFIGS:
            args, cmake = self._flags(config)
            with self.subTest(config=config):
                self.assertEqual(args.count("--no-fatal-warnings"), 1, args)
                for flag in ("-DDEVELOPER_MODE=ON", "-DUSE_VULKAN=OFF",
                             "-DENABLE_THUNDER=OFF"):
                    self.assertEqual(cmake.count(flag), 1,
                                     f"{config} states {flag} as well as the default")


class TestMacJscUsesXcode(unittest.TestCase):
    """build/configs.sh: on macOS a config does not choose its own build
    system. Xcode is the only one there -- build-webkit's CMake path needs a
    Swift-capable generator -- so the three jsc-* configs build the Apple
    port's JavaScriptCore through Tools/Scripts/build-jsc, and the Apple
    configs are refused anywhere else."""

    JSC_CONFIGS = ("jsc-debug", "jsc-release", "jsc-release-asan")

    def _load(self, config, os):
        return bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/arch.sh"
. "{REPO}/build/configs.sh"
config_load {config} {os}
echo "BUILDSYS=$CFG_BUILDSYS"
echo "SCRIPT=$CFG_SCRIPT"
echo "PORT=$CFG_PORT"
echo "CC=$CFG_CC"
echo "ARGS=$CFG_ARGS"
echo "DIR=$(config_build_dir /src/WebKit)"
config_jsc_only && echo "JSCONLY=1" || echo "JSCONLY="
echo "WEB=$(config_web_process_name)"
''')

    def _fields(self, config, os):
        cp = self._load(config, os)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        return dict(l.split("=", 1) for l in cp.stdout.strip().splitlines())

    def test_macos_jsc_configs_build_with_xcode(self):
        for config in self.JSC_CONFIGS:
            f = self._fields(config, "macos")
            with self.subTest(config=config):
                self.assertEqual(f["BUILDSYS"], "xcode")
                self.assertEqual(f["SCRIPT"], "Tools/Scripts/build-jsc")
                # No --jsc-only: it would send build-jsc down the CMake path
                # that has no working generator in a macOS guest.
                self.assertEqual(f["PORT"], "")
                # Xcode's toolchain is the only one; CC=clang would override it.
                self.assertEqual(f["CC"], "")

    def test_linux_jsc_configs_still_build_the_jsconly_port(self):
        for config in self.JSC_CONFIGS:
            f = self._fields(config, "linux")
            with self.subTest(config=config):
                self.assertEqual(f["BUILDSYS"], "cmake")
                self.assertEqual(f["SCRIPT"], "Tools/Scripts/build-webkit")
                self.assertEqual(f["PORT"], "--jsc-only")

    def test_macos_jsc_shares_the_apple_products_directory(self):
        """Same configuration as mac-debug, built to a smaller depth -- so the
        same tree, not a second one built with the same settings."""
        self.assertEqual(self._fields("jsc-debug", "macos")["DIR"],
                         self._fields("mac-debug", "macos")["DIR"])
        self.assertEqual(self._fields("jsc-release", "macos")["DIR"],
                         self._fields("mac-release", "macos")["DIR"])

    def test_macos_jsc_asan_gets_a_tree_of_its_own(self):
        """build-jsc takes ASAN=YES where build-webkit takes --asan;
        config_build_dir reads both, or an instrumented build lands half on top
        of an uninstrumented one."""
        asan = self._fields("jsc-release-asan", "macos")
        self.assertNotEqual(self._fields("jsc-release", "macos")["DIR"], asan["DIR"])
        self.assertTrue(asan["DIR"].endswith("-asan"), asan["DIR"])
        self.assertIn("ASAN=YES", asan["ARGS"])

    def test_a_jsc_config_names_no_web_process_on_either_platform(self):
        """config_jsc_only: nothing in a JSC-only build serves a page, and a
        name here does not fail, it makes a debugger wait forever."""
        for os in ("linux", "macos"):
            for config in self.JSC_CONFIGS:
                f = self._fields(config, os)
                with self.subTest(config=config, os=os):
                    self.assertEqual(f["JSCONLY"], "1")
                    self.assertEqual(f["WEB"], "")

    def test_an_apple_config_is_refused_off_macos(self):
        """xcodebuild is not on a Linux build box, and saying so beats a
        `command not found` from inside a perl script."""
        for config in ("mac-debug", "mac-release", "mac-release-asan",
                       "ios-sim-release"):
            cp = self._load(config, "linux")
            with self.subTest(config=config):
                self.assertNotEqual(cp.returncode, 0)
                self.assertIn("Xcode", cp.stdout + cp.stderr)

    def test_the_platform_is_required(self):
        """A config_load with no platform is a caller that has not loaded its
        target yet -- refused, rather than silently answered for Linux."""
        cp = bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/arch.sh"
. "{REPO}/build/configs.sh"
config_load jsc-debug
''')
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("load_target", cp.stdout + cp.stderr)


class TestPerConfigMachineFlags(unittest.TestCase):
    """config_target_var (build/configs.sh): a machine may carry flags for one
    config, beside the machine-wide pair, for a quirk that would be wrong on
    that machine's other configs. Narrowest last, so it wins."""

    def _flags(self, config, env, os="linux"):
        cp = bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/arch.sh"
. "{REPO}/build/configs.sh"
config_load {config} {os}
config_build_env /src/WebKit 4 10 native
for e in "${{CFG_ENV[@]}}"; do
    case "$e" in WK_BUILD_CMAKE=*) echo "$e" ;; WK_BUILD_ARGS=*) echo "$e" ;; esac
done
''', env=env)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        return cp.stdout

    def test_a_machines_flags_for_one_config_reach_that_config(self):
        out = self._flags("wpe-release", {
            "WK_TARGET_CMAKE": "-DMACHINEWIDE=1",
            "WK_TARGET_CMAKE_wpe_release": "-DONECONFIG=1",
            "WK_BUILD_ARGS_wpe_release": "--one-config",
        })
        self.assertIn("-DONECONFIG=1", out)
        self.assertIn("--one-config", out)
        self.assertLess(out.index("-DMACHINEWIDE=1"), out.index("-DONECONFIG=1"),
                        "the per-config flag does not come last, so it cannot win")

    def test_another_config_on_the_same_machine_does_not_get_them(self):
        out = self._flags("jsc-release", {
            "WK_TARGET_CMAKE_wpe_release": "-DONECONFIG=1",
            "WK_BUILD_ARGS_wpe_release": "--one-config",
        })
        self.assertNotIn("-DONECONFIG=1", out)
        self.assertNotIn("--one-config", out)
