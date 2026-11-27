import os
import subprocess
import tempfile
import unittest

from wk import webkit
from wk.core.errors import Refused

SRC, OUT = "/w/x/src", "/w/x/build"
RELEASE = ('-DCMAKE_BUILD_TYPE=RelWithDebInfo -DCMAKE_C_FLAGS_RELWITHDEBINFO="-O3 -g -DNDEBUG" '
           '-DCMAKE_CXX_FLAGS_RELWITHDEBINFO="-O3 -g -DNDEBUG" -DDEBUG_FISSION=ON')
COMMON = "-DDEVELOPER_MODE=ON -DUSE_VULKAN=OFF -DENABLE_THUNDER=OFF -DCMAKE_Swift_NUM_THREADS=1"
CCACHE = ["CC=clang", "CXX=clang++", "WEBKIT_OUTPUTDIR=/w/x/build/JSCOnly/Release", "CCACHE_DIR=/w/x/build/ccache", "CCACHE_BASEDIR=/w/x/src",
          "CCACHE_SLOPPINESS=pch_defines,time_macros,include_file_mtime,include_file_ctime", "CCACHE_NOHASHDIR=true"]


class TestPresets(unittest.TestCase):
    def test_the_names_and_which_kind_of_machine_builds_each(self):
        self.assertEqual(webkit.names(), ["jsc-debug", "jsc-release", "gtk-debug", "gtk-release", "wpe-release", "mac-debug", "mac-release"])
        self.assertEqual((webkit.names("linux"), webkit.names("mac")),
                         (["jsc-debug", "jsc-release", "gtk-debug", "gtk-release", "wpe-release"], ["jsc-debug", "jsc-release", "mac-debug", "mac-release"]))

    def test_the_memory_of_a_job_is_by_build_system_and_port(self):
        self.assertEqual([webkit.resolve(name, kind).mb_per_job for name, kind in (("jsc-release", "linux"), ("gtk-debug", "linux"),
                                                                                  ("jsc-release", "mac"), ("mac-debug", "mac"))],
                         [1536, 4608, 3072, 3072])

    def test_an_unknown_preset_is_refused_listing_the_names(self):
        self.assertRaisesRegex(Refused, "no preset named jsc-fast: the presets are jsc-debug, jsc-release, gtk-debug, gtk-release, "
                                        "wpe-release, mac-debug, mac-release", webkit.resolve, "jsc-fast", "linux")

    def test_a_preset_this_kind_of_machine_cannot_build_is_refused_naming_those_it_can(self):
        self.assertRaisesRegex(Refused, "mac-release builds on a mac server, and this one is linux: the presets that build here are "
                                        "jsc-debug, jsc-release, gtk-debug, gtk-release, wpe-release", webkit.resolve, "mac-release", "linux")
        self.assertRaisesRegex(Refused, "gtk-debug builds on a linux server, and this one is mac: the presets that build here are "
                                        "jsc-debug, jsc-release, mac-debug, mac-release", webkit.resolve, "gtk-debug", "mac")


class TestBuild(unittest.TestCase):
    def build(self, name, kind, jobs=8, extra=""):
        return webkit.build_argv(webkit.resolve(name, kind), SRC, OUT, jobs, extra)

    def test_a_cmake_jsc_build(self):
        self.assertEqual(self.build("jsc-release", "linux"),
                         ["env"] + CCACHE + ["NUMBER_OF_PROCESSORS=8", "CMAKE_BUILD_PARALLEL_LEVEL=8", "WEBKIT_NINJA_LINK_MAX=1", "WK_USE_CCACHE=YES",
                                             "Tools/Scripts/build-webkit", "--no-fatal-warnings", "--jsc-only", "--release",
                                             "--cmakeargs=%s %s -DENABLE_OFFLINE_ASM_ALT_ENTRY=0" % (COMMON, RELEASE), "--makeargs=-j8"])

    def test_a_debug_build_pins_no_optimisation(self):
        self.assertEqual(self.build("jsc-debug", "linux")[-4:], ["--jsc-only", "--debug", "--cmakeargs=%s -DENABLE_OFFLINE_ASM_ALT_ENTRY=1" % COMMON,
                                                                 "--makeargs=-j8"])
        self.assertEqual(self.build("gtk-debug", "linux")[-4:], ["--gtk", "--debug", "--cmakeargs=" + COMMON, "--makeargs=-j8"])

    def test_a_full_port_does_not_ask_webkit_for_ccache_and_takes_the_machines_own_flags_last(self):
        argv = self.build("wpe-release", "linux", 3, "-DUSE_LIBBACKTRACE=OFF,-DENABLE_JOURNALD_LOG=OFF")
        self.assertEqual(argv[8:], ["NUMBER_OF_PROCESSORS=3", "CMAKE_BUILD_PARALLEL_LEVEL=3", "WEBKIT_NINJA_LINK_MAX=1", "Tools/Scripts/build-webkit",
                                    "--no-fatal-warnings", "--wpe", "--release",
                                    "--cmakeargs=%s %s -DENABLE_WPE_PLATFORM=ON -DUSE_LIBBACKTRACE=OFF -DENABLE_JOURNALD_LOG=OFF" % (COMMON, RELEASE),
                                    "--makeargs=-j3"])
        self.assertEqual(self.build("gtk-release", "linux")[-2], "--cmakeargs=%s %s" % (COMMON, RELEASE))

    def test_xcode_is_told_its_jobs_through_each_scripts_own_door(self):
        self.assertEqual(self.build("mac-release", "mac", 6), ["env", "WEBKIT_OUTPUTDIR=/w/x/build/Release", "Tools/Scripts/build-webkit", "--release", "-jobs", "6"])
        self.assertEqual(self.build("jsc-debug", "mac", 6), ["env", "WEBKIT_OUTPUTDIR=/w/x/build/Debug", "Tools/Scripts/build-jsc", "--debug", "ARGS=-jobs 6"])


class TestRun(unittest.TestCase):
    def test_a_jsc_preset_runs_the_built_jsc_against_its_own_libraries_validating_options(self):
        self.assertEqual(webkit.run_argv(webkit.resolve("jsc-debug", "linux"), OUT, ["-e", "1"]),
                         ["env", "LD_LIBRARY_PATH=/w/x/build/JSCOnly/Debug/lib", "/w/x/build/JSCOnly/Debug/bin/jsc", "--validateOptions=1", "-e", "1"])
        self.assertEqual(webkit.run_argv(webkit.resolve("jsc-release", "mac"), OUT, []),
                         ["env", "DYLD_FRAMEWORK_PATH=/w/x/build/Release", "/w/x/build/Release/jsc", "--validateOptions=1"])

    def test_a_full_port_runs_its_browser_launcher(self):
        self.assertEqual(webkit.run_argv(webkit.resolve("gtk-release", "linux"), OUT, ["https://example.org"]),
                         ["env", "WEBKIT_OUTPUTDIR=/w/x/build/GTK/Release", "Tools/Scripts/run-minibrowser", "--gtk", "--release", "https://example.org"])


class TestTest(unittest.TestCase):
    def test_jsc_tests_take_no_path_so_the_tests_named_become_one_filter(self):
        head = ["env", "WEBKIT_OUTPUTDIR=/w/x/build/JSCOnly/Release", "Tools/Scripts/run-javascriptcore-tests", "--jsc-only", "--release", "--no-build",
                "--no-fail-fast", "--memory-limited", "--child-processes=4"]
        preset = webkit.resolve("jsc-release", "linux")
        self.assertEqual(webkit.test_argv(preset, OUT, 4, []), head)
        self.assertEqual(webkit.test_argv(preset, OUT, 4, ["JSTests/stress/a.js", "--quick", "wasm.yaml/x"]),
                         head + ["--filter", r"^(stress/a\.js|wasm\.yaml/x)(\..*)?$", "--quick"])

    def test_layout_tests_render_in_software_on_a_cmake_port_and_take_their_words_as_they_are(self):
        self.assertEqual(webkit.test_argv(webkit.resolve("wpe-release", "linux"), OUT, 2, ["fast/dom", "--verbose"]),
                         ["env", "WEBKIT_OUTPUTDIR=/w/x/build/WPE/Release", "LIBGL_ALWAYS_SOFTWARE=1", "GALLIUM_DRIVER=llvmpipe", "WEBKIT_DISABLE_DMABUF_RENDERER=1",
                          "Tools/Scripts/run-webkit-tests", "--wpe", "--release", "--no-build", "--no-retry-failures", "--child-processes=2",
                          "fast/dom", "--verbose"])
        self.assertEqual(webkit.test_argv(webkit.resolve("mac-debug", "mac"), OUT, 2, [])[:4],
                         ["env", "WEBKIT_OUTPUTDIR=/w/x/build/Debug", "Tools/Scripts/run-webkit-tests", "--debug"])

    def test_the_words_that_name_tests_are_those_that_are_not_options(self):
        self.assertEqual(webkit.paths(["fast/dom", "--verbose", "a.html?q=1"]), ["fast/dom", "a.html?q=1"])

    def test_the_existence_check_prints_the_tests_that_are_nowhere(self):
        with tempfile.TemporaryDirectory() as src:
            os.makedirs(src + "/LayoutTests/fast")
            for path in ("/LayoutTests/fast/a.html", "/b.html"):
                open(src + path, "w").close()
            r = subprocess.run(webkit.missing_argv(src, ["fast/a.html?x=1", "b.html", "fast/gone.html", "nope"]), stdout=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(r.stdout, "fast/gone.html\nnope\n")
