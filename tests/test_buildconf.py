"""The build configs as data (lib/wk/buildconf.py), and build/configs.sh's
shim that hands the same fields to the bash commands still reading them.

`build.config_is_data`: `--cmakeargs` is refused (tests/test_wk_build.py), an
ASan config builds instrumented into its own dir, a profile-guided config
declares its disk need.

Run: python3 tests/run.py -k test_buildconf
"""
import contextlib
import io
import sys
import unittest

from tests.support import REPO, bash

sys.path.insert(0, str(REPO / "lib"))
from wk import buildconf  # noqa: E402
from wk.act import Refused  # noqa: E402

CMAKE_CONFIGS = ("jsc-debug", "jsc-release", "jsc-release-asan", "gtk-debug", "gtk-release", "gtk-release-asan", "wpe-release")
XCODE_CONFIGS = ("mac-debug", "mac-release", "mac-release-asan", "ios-sim-release")
JSC_CONFIGS = ("jsc-debug", "jsc-release", "jsc-release-asan")


def cfg(name, os="linux", kind="container", env=None):
    return buildconf.resolve(name, os, kind, env or {})


def refused(case, fn):
    with case.assertRaises(Refused):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            fn()
    return err.getvalue()


def env_of(c, env=None, jobs=4, nice=10, arch="native", **kw):
    return dict(e.split("=", 1) for e in buildconf.build_env(c, "/src/WebKit", jobs, nice, arch, "/ccache", env or {}, **kw))


class TestAllConfigDefaults(unittest.TestCase):
    """The flags of how this repository builds WebKit: every CMake config starts with them, its own
    flags come after and win, and the Xcode configs get neither -- xcodebuild takes no -D flags."""

    KINDS = {"container": "ON", "vm": "ON", "local": "ON", "remote": "OFF"}

    def test_every_cmake_config_starts_with_them(self):
        for name in CMAKE_CONFIGS:
            for kind, bt in self.KINDS.items():
                c = cfg(name, kind=kind)
                with self.subTest(config=name, kind=kind):
                    self.assertTrue(c.args.startswith("--no-fatal-warnings "), c.args)
                    for flag in ("-DDEVELOPER_MODE=ON", "-DUSE_VULKAN=OFF", "-DENABLE_THUNDER=OFF", "-DUSE_LIBBACKTRACE=" + bt):
                        self.assertIn(flag, c.cmake)

    def test_no_xcode_config_gets_them(self):
        for name in XCODE_CONFIGS:
            c = cfg(name, "macos", "vm")
            with self.subTest(config=name):
                self.assertNotIn("--no-fatal-warnings", c.args)
                self.assertEqual(c.cmake, "")

    def test_no_config_repeats_a_default_it_agrees_with(self):
        for name in CMAKE_CONFIGS:
            c = cfg(name)
            with self.subTest(config=name):
                self.assertEqual(c.args.count("--no-fatal-warnings"), 1)
                for flag in ("-DDEVELOPER_MODE=ON", "-DUSE_VULKAN=OFF", "-DENABLE_THUNDER=OFF", "-DUSE_LIBBACKTRACE="):
                    self.assertEqual(c.cmake.count(flag), 1, "%s states %s as well as the default" % (name, flag))

    def test_an_unknown_name_is_a_lookup_error_and_the_list_names_every_config(self):
        with self.assertRaises(LookupError):
            cfg("nope")
        for name in buildconf.names():
            self.assertIn(name, buildconf.LIST_TEXT)


class TestMacJscUsesXcode(unittest.TestCase):
    """On macOS the three jsc-* configs build the Apple port's JavaScriptCore through build-jsc,
    and an Apple config is refused anywhere else."""

    def test_macos_jsc_configs_build_with_xcode(self):
        for name in JSC_CONFIGS:
            c = cfg(name, "macos", "vm")
            with self.subTest(config=name):
                self.assertEqual((c.buildsys, c.script, c.port, c.cc), ("xcode", "Tools/Scripts/build-jsc", "", ""))

    def test_linux_jsc_configs_still_build_the_jsconly_port(self):
        for name in JSC_CONFIGS:
            c = cfg(name)
            self.assertEqual((c.buildsys, c.script, c.port), ("cmake", "Tools/Scripts/build-webkit", "--jsc-only"))

    def test_macos_jsc_shares_the_apple_products_directory(self):
        self.assertEqual(cfg("jsc-debug", "macos").build_dir(), cfg("mac-debug", "macos").build_dir())
        self.assertEqual(cfg("jsc-release", "macos").build_dir(), cfg("mac-release", "macos").build_dir())

    def test_an_asan_config_builds_instrumented_into_its_own_dir(self):
        """build-jsc takes ASAN=YES where build-webkit takes --asan; either way its own tree."""
        asan = cfg("jsc-release-asan", "macos")
        self.assertNotEqual(asan.build_dir(), cfg("jsc-release", "macos").build_dir())
        self.assertTrue(asan.build_dir().endswith("-asan"))
        self.assertIn("ASAN=YES", asan.args)
        self.assertTrue(cfg("mac-release-asan", "macos").build_dir().endswith("Release-asan"))
        self.assertIn("--asan", cfg("gtk-release-asan").args)

    def test_a_jsc_config_names_no_web_process_on_either_platform(self):
        for os in ("linux", "macos"):
            for name in JSC_CONFIGS:
                c = cfg(name, os)
                self.assertTrue(c.jsc_only)
                self.assertEqual((c.web_process_name(), c.test_runner_name()), ("", ""))

    def test_an_apple_config_is_refused_off_macos(self):
        for name in XCODE_CONFIGS:
            self.assertIn("Xcode", refused(self, lambda: cfg(name, "linux")))

    def test_the_platform_and_kind_are_required(self):
        self.assertIn("Target.os()", refused(self, lambda: buildconf.resolve("jsc-debug", "", "container", {})))
        self.assertIn("Target.kind", refused(self, lambda: buildconf.resolve("jsc-debug", "linux", None, {})))

    def test_an_unknown_kind_is_refused(self):
        self.assertIn("unknown target kind 'bogus'", refused(self, lambda: cfg("jsc-release", kind="bogus")))

    def test_the_kind_falls_back_to_WK_TARGET_KIND(self):
        c = buildconf.resolve("jsc-release", "linux", None, {"WK_TARGET_KIND": "remote"})
        self.assertIn("-DUSE_LIBBACKTRACE=OFF", c.cmake)

    def test_the_run_side_paths_per_build_system(self):
        c, m = cfg("wpe-release"), cfg("mac-release", "macos")
        self.assertEqual(c.jsc_path("/s"), "/s/WebKitBuild/WPE/Release/bin/jsc")
        self.assertEqual((c.run_var(), c.run_dir("/s")), ("LD_LIBRARY_PATH", "/s/WebKitBuild/WPE/Release/lib"))
        self.assertEqual(m.browser_path("/s"), "/s/WebKitBuild/Release/MiniBrowser.app/Contents/MacOS/MiniBrowser")
        self.assertEqual((m.browser_url_flag(), c.browser_url_flag(), c.browser_env("/s")), ("--url", "", ""))
        self.assertIn("__XPC_DYLD_FRAMEWORK_PATH=/s/WebKitBuild/Release", m.browser_env("/s"))
        self.assertEqual((c.web_process_name(), c.network_process_name(), c.gpu_process_name()),
                         ("WPEWebProcess", "WPENetworkProcess", "WPEGPUProcess"))
        self.assertEqual(cfg("gtk-debug").web_process_name(), "WebKitWebProcess")
        self.assertEqual(m.web_process_name(), "com.apple.WebKit.WebContent.Development")
        self.assertEqual(m.web_process_pause_env(), "__XPC_WEBKIT_PAUSE_WEB_PROCESS_ON_LAUNCH=1")
        self.assertEqual(cfg("ios-sim-release", "macos").build_dir("/s"), "/s/WebKitBuild/Release-iphonesimulator")


class TestADiskNeedIsDeclared(unittest.TestCase):
    def test_a_profile_guided_config_declares_more_than_the_default(self):
        self.assertEqual(cfg("mac-release", "macos").disk_gb, buildconf.DISK_GB)
        self.assertEqual(cfg("mac-release-pgo", "macos").disk_gb, buildconf.PGO_DISK_GB)
        self.assertGreater(buildconf.PGO_DISK_GB, buildconf.DISK_GB)
        self.assertEqual(cfg("jsc-release", env={"WK_BUILD_DISK_GB": "40"}).disk_gb, 40)

    def test_a_profile_guided_build_runs_without_a_compilation_cache(self):
        self.assertEqual(env_of(cfg("mac-release-pgo", "macos", "vm")).get("WK_NO_COMPILATION_CACHE"), "1")
        self.assertNotIn("WK_NO_COMPILATION_CACHE", env_of(cfg("mac-release", "macos", "vm")))


class TestPerConfigMachineFlags(unittest.TestCase):
    """A machine may carry flags for one config, narrowest last so it wins."""

    ENV = {"WK_TARGET_CMAKE": "-DMACHINEWIDE=1", "WK_TARGET_CMAKE_wpe_release": "-DONECONFIG=1", "WK_BUILD_ARGS_wpe_release": "--one-config"}

    def test_a_machines_flags_for_one_config_reach_that_config(self):
        e = env_of(cfg("wpe-release"), self.ENV)
        self.assertIn("-DONECONFIG=1", e["WK_BUILD_CMAKE"])
        self.assertIn("--one-config", e["WK_BUILD_ARGS"])
        self.assertLess(e["WK_BUILD_CMAKE"].index("-DMACHINEWIDE=1"), e["WK_BUILD_CMAKE"].index("-DONECONFIG=1"))

    def test_another_config_on_the_same_machine_does_not_get_them(self):
        e = env_of(cfg("jsc-release"), self.ENV)
        self.assertNotIn("-DONECONFIG=1", e["WK_BUILD_CMAKE"])
        self.assertNotIn("--one-config", e["WK_BUILD_ARGS"])

    def test_target_build_args_fold_in_and_are_absent_when_unset(self):
        c = cfg("jsc-release")
        self.assertEqual(env_of(c)["WK_BUILD_ARGS"], "--jsc-only --no-fatal-warnings --release")
        self.assertEqual(env_of(c, target_build_args="--extra")["WK_BUILD_ARGS"], "--jsc-only --no-fatal-warnings --release --extra")

    def test_cmake_and_env_from_the_command_line_go_last(self):
        e = buildconf.build_env(cfg("jsc-release"), "/src/WebKit", 4, 10, "native", "/ccache", {}, "-DMINE=1", ["CC=gcc-14"])
        self.assertTrue(dict(x.split("=", 1) for x in e)["WK_BUILD_CMAKE"].endswith("-DMINE=1"))
        self.assertEqual(e[-1], "CC=gcc-14")

    def test_an_armhf_workspace_carries_its_arch(self):
        e = env_of(cfg("wpe-release"), arch="armhf")
        self.assertEqual((e["WK_ARCH"], e["WK_ARCH_WRAPPER"]), ("armhf", "linux32"))
        self.assertIn("-DUSE_LD_LLD=OFF -DUSE_VULKAN=OFF", e["WK_BUILD_CMAKE"])
        self.assertNotIn("WK_ARCH", env_of(cfg("wpe-release")))


class TestLibcxxDefault(unittest.TestCase):
    """-stdlib=libc++ is opt-in per machine with WK_TARGET_LIBCXX=1: the wkdev SDK image has no libc++."""

    def test_absent_by_default_for_every_kind(self):
        for kind in ("container", "vm", "local", "remote"):
            self.assertNotIn("-stdlib=libc++", cfg("jsc-release", kind=kind).cmake)
        self.assertNotIn("-stdlib=libc++", cfg("jsc-release", env={"WK_TARGET_LIBCXX": "0"}).cmake)

    def test_present_with_WK_TARGET_LIBCXX_1(self):
        cmake = cfg("jsc-release", env={"WK_TARGET_LIBCXX": "1"}).cmake
        for flag in ("-DCMAKE_CXX_FLAGS=-stdlib=libc++", "-DCMAKE_EXE_LINKER_FLAGS=-stdlib=libc++",
                     "-DCMAKE_SHARED_LINKER_FLAGS=-stdlib=libc++", "-DCMAKE_MODULE_LINKER_FLAGS=-stdlib=libc++"):
            self.assertIn(flag, cmake)

    def test_anything_else_is_refused_and_names_the_conf(self):
        err = refused(self, lambda: cfg("jsc-release", env={"WK_TARGET_LIBCXX": "yes", "WK_TARGET": "moose"}))
        self.assertIn("WK_TARGET_LIBCXX='yes'", err)
        self.assertIn("moose.conf", err)

    def test_absent_for_apple_configs(self):
        self.assertEqual(cfg("mac-release", "macos", "vm", {"WK_TARGET_LIBCXX": "1"}).cmake, "")

    def test_the_cxx_flags_merge_keeps_both_values(self):
        c = cfg("jsc-release", kind="remote", env={"WK_TARGET_LIBCXX": "1"})
        cmake = env_of(c, {"WK_TARGET_CMAKE": "-DCMAKE_CXX_FLAGS=-Wno-invalid-constexpr"})["WK_BUILD_CMAKE"]
        self.assertEqual(cmake.count("-DCMAKE_CXX_FLAGS="), 1, cmake)
        self.assertIn('-DCMAKE_CXX_FLAGS="-stdlib=libc++ -Wno-invalid-constexpr"', cmake)
        self.assertIn('-DCMAKE_C_FLAGS_RELWITHDEBINFO="-O3 -g -DNDEBUG"', cmake)


class TestCcacheIsBlindToTheJobCount(unittest.TestCase):
    """Two builds differing only in -j must be one ccache cache: the job count and nice level reach
    only the four variables that carry them to the build driver, never a compiler or CCACHE_* setting."""

    MOVES = frozenset({"NUMBER_OF_PROCESSORS", "CMAKE_BUILD_PARALLEL_LEVEL", "WK_JOBS", "WK_NICE"})

    def _only_job_and_nice_move(self, c):
        low, high = env_of(c, jobs=1, nice=19), env_of(c, jobs=64, nice=0)
        self.assertEqual(set(low), set(high))
        self.assertFalse({k for k in low if low[k] != high[k]} - self.MOVES)

    def test_cmake_port(self):
        self._only_job_and_nice_move(cfg("jsc-release"))

    def test_apple_port(self):
        self._only_job_and_nice_move(cfg("mac-release", "macos", "vm"))

    def test_a_jsc_config_asks_for_ccache_and_the_other_ports_do_not(self):
        for name in JSC_CONFIGS:
            self.assertEqual(env_of(cfg(name)).get("WK_USE_CCACHE"), "YES")
        self.assertNotIn("WK_USE_CCACHE", env_of(cfg("gtk-release")))
        self._only_job_and_nice_move(cfg("jsc-debug"))


class TestTheBashShim(unittest.TestCase):
    """build/configs.sh's config_load hands a bash caller exactly the Python fields."""

    def _bash(self, name, os, kind="container", env=None):
        # Only cmd/bench still calls bash accessors, and only these four.
        return bash('. "%s/build/configs.sh"\nconfig_load %s %s %s || echo UNKNOWN\n'
                    'printf "%%s|" "$CFG_BUILDSYS" "$CFG_PORT" "$CFG_ARGS" "$CFG_CMAKE" "$CFG_PGO" "$(config_build_dir /s)" '
                    '"$(config_jsc_path /s)" "$(config_run_dir /s)" "$(config_run_var)" "$WK_MB_PER_JOB"\n'
                    % (REPO, name, os, kind), env=env)

    def test_every_config_on_both_platforms_matches(self):
        for os in ("linux", "macos"):
            for name in buildconf.names():
                if os == "linux" and name in XCODE_CONFIGS + ("mac-release-pgo",):
                    continue
                c = cfg(name, os)
                cp = self._bash(name, os)
                want = "|".join([c.buildsys, c.port, c.args, c.cmake, "1" if c.pgo else "", c.build_dir("/s"), c.jsc_path("/s"),
                                 c.run_dir("/s"), c.run_var(), str(c.mb_per_job())]) + "|"
                with self.subTest(config=name, os=os):
                    self.assertEqual(cp.returncode, 0, cp.stderr)
                    self.assertEqual(cp.stdout.replace("\n", ""), want)

    def test_an_unknown_name_returns_1_for_the_caller_to_name(self):
        cp = self._bash("nope", "linux")
        self.assertIn("UNKNOWN", cp.stdout)

    def test_a_refusal_ends_the_caller_with_its_reason(self):
        cp = self._bash("mac-release", "linux")
        self.assertNotEqual(cp.returncode, 0)
        self.assertNotIn("UNKNOWN", cp.stdout)
        self.assertIn("Xcode", cp.stderr)

    def test_an_explicit_mb_per_job_is_kept(self):
        cp = bash('. "%s/lib/resources.sh"\n. "%s/build/configs.sh"\nconfig_load mac-release macos vm\necho "$WK_MB_PER_JOB"\n'
                  % (REPO, REPO), env={"WK_MB_PER_JOB": "999"})
        self.assertEqual(cp.stdout.strip(), "999", cp.stderr)


if __name__ == "__main__":
    unittest.main()
