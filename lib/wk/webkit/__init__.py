"""The project's names (project.json), the build presets and the Tools/Scripts command lines, each run from the
checkout. WebKit is named here and nowhere else."""

import dataclasses
import json
import os
import re

from wk.core.act import die

with open(os.path.join(os.path.dirname(__file__), "project.json")) as _f:
    PROJECT = json.load(_f)

CMAKE, XCODE = "cmake", "xcode"
LINUX, MAC = ("linux",), ("mac",)

# Without CMAKE_Swift_NUM_THREADS=1 one ninja job runs a swift-frontend per core (measured: nine at 1.7 GB in gtk-debug).
COMMON = "-DDEVELOPER_MODE=ON -DUSE_VULKAN=OFF -DENABLE_THUNDER=OFF -DCMAKE_Swift_NUM_THREADS=1"
# CMake's RelWithDebInfo is -O2 where Release is -O3, so the flags are pinned; WebKitCommon.cmake computes
# DEBUG_FISSION's default before DEVELOPER_MODE is set, so it is stated.
RELEASE = ('-DCMAKE_BUILD_TYPE=RelWithDebInfo -DCMAKE_C_FLAGS_RELWITHDEBINFO="-O3 -g -DNDEBUG"'
           ' -DCMAKE_CXX_FLAGS_RELWITHDEBINFO="-O3 -g -DNDEBUG" -DDEBUG_FISSION=ON')
SLOPPINESS = "pch_defines,time_macros,include_file_mtime,include_file_ctime"
SOFTWARE_GL = ["LIBGL_ALWAYS_SOFTWARE=1", "GALLIUM_DRIVER=llvmpipe", "WEBKIT_DISABLE_DMABUF_RENDERER=1"]


@dataclasses.dataclass(frozen=True)
class Preset:
    """`flags` are the port and configuration every script takes; `products` is its directory under the workspace's build/."""
    name: str
    kinds: tuple
    system: str
    script: str
    flags: tuple
    products: str
    cmake: str = ""
    jsc_only: bool = False

    @property
    def mb_per_job(self):
        """Measured peak of one compile job: 1536 MB JSC-only CMake, 4608 MB the other CMake ports, 3072 MB Xcode."""
        if self.system == XCODE:
            return 3072
        return 1536 if self.jsc_only else 4608


PRESETS = (
    Preset("jsc-debug", LINUX, CMAKE, "build-webkit", ("--jsc-only", "--debug"), "JSCOnly/Debug",
           "-DENABLE_OFFLINE_ASM_ALT_ENTRY=1", jsc_only=True),
    Preset("jsc-debug", MAC, XCODE, "build-jsc", ("--debug",), "Debug", jsc_only=True),
    Preset("jsc-release", LINUX, CMAKE, "build-webkit", ("--jsc-only", "--release"), "JSCOnly/Release",
           RELEASE + " -DENABLE_OFFLINE_ASM_ALT_ENTRY=0", jsc_only=True),
    Preset("jsc-release", MAC, XCODE, "build-jsc", ("--release",), "Release", jsc_only=True),
    Preset("gtk-debug", LINUX, CMAKE, "build-webkit", ("--gtk", "--debug"), "GTK/Debug"),
    Preset("gtk-release", LINUX, CMAKE, "build-webkit", ("--gtk", "--release"), "GTK/Release", RELEASE),
    Preset("wpe-release", LINUX, CMAKE, "build-webkit", ("--wpe", "--release"), "WPE/Release", RELEASE + " -DENABLE_WPE_PLATFORM=ON"),
    Preset("mac-debug", MAC, XCODE, "build-webkit", ("--debug",), "Debug"),
    Preset("mac-release", MAC, XCODE, "build-webkit", ("--release",), "Release"),
)


def names(kind=None):
    return list(dict.fromkeys(p.name for p in PRESETS if kind is None or kind in p.kinds))


def resolve(name, kind):
    found = [p for p in PRESETS if p.name == name]
    if not found:
        die("no preset named %s: the presets are %s" % (name, ", ".join(names())))
    here = [p for p in found if kind in p.kinds]
    if not here:
        die("%s builds on a %s server, and this one is %s: the presets that build here are %s"
            % (name, " or ".join(k for p in found for k in p.kinds), kind, ", ".join(names(kind))))
    return here[0]


def products(preset, out):
    """Where a preset builds, under the workspace's build directory. It is the scripts' WEBKIT_OUTPUTDIR, which they
    take whole as the product directory (webkitdirs.pm, usesPerConfigurationBuildDirectory): so each preset has its own."""
    return "%s/%s" % (out, preset.products)


def build_argv(preset, src, out, jobs, extra_cmake=""):
    """`extra_cmake` is a machine's own flags, comma-separated as its conf holds them."""
    script = "Tools/Scripts/" + preset.script
    if preset.system == XCODE:
        # build-jsc appends an unknown argument to a `make` line, where ARGS= is the hole for xcodebuild's.
        limit = ["ARGS=-jobs %d" % jobs] if preset.script == "build-jsc" else ["-jobs", str(jobs)]
        return ["env", "WEBKIT_OUTPUTDIR=" + products(preset, out), script] + list(preset.flags) + limit
    cmake = " ".join(filter(None, [COMMON, preset.cmake] + extra_cmake.split(",")))
    env = ["CC=clang", "CXX=clang++", "WEBKIT_OUTPUTDIR=" + products(preset, out), "CCACHE_DIR=%s/ccache" % out, "CCACHE_BASEDIR=" + src,
           "CCACHE_SLOPPINESS=" + SLOPPINESS, "CCACHE_NOHASHDIR=true", "NUMBER_OF_PROCESSORS=%d" % jobs,
           "CMAKE_BUILD_PARALLEL_LEVEL=%d" % jobs,
           "WEBKIT_NINJA_LINK_MAX=1"]   # a job is budgeted as a compile; two gtk-debug links measured 5.5 + 4.2 GB at -j3
    if preset.jsc_only:
        env.append("WK_USE_CCACHE=YES")   # WebKitCCache.cmake reads it; without it every JSC build goes cold silently
    return ["env"] + env + [script, "--no-fatal-warnings"] + list(preset.flags) + ["--cmakeargs=" + cmake, "--makeargs=-j%d" % jobs]


def run_argv(preset, out, args):
    built = products(preset, out)
    if not preset.jsc_only:
        return ["env", "WEBKIT_OUTPUTDIR=" + products(preset, out), "Tools/Scripts/run-minibrowser"] + list(preset.flags) + list(args)
    if preset.system == XCODE:
        return ["env", "DYLD_FRAMEWORK_PATH=" + built, built + "/jsc", "--validateOptions=1"] + list(args)
    return ["env", "LD_LIBRARY_PATH=%s/lib" % built, built + "/bin/jsc", "--validateOptions=1"] + list(args)


def paths(args):
    return [a for a in args if not a.startswith("-")]


def test_argv(preset, out, jobs, args):
    env = ["env", "WEBKIT_OUTPUTDIR=" + products(preset, out)]
    options = [a for a in args if a.startswith("-")]
    if preset.jsc_only:
        # run-javascriptcore-tests takes no path: a bare word is ignored, so the tests named become its filter.
        named = [re.escape(p[len("JSTests/"):] if p.startswith("JSTests/") else p) for p in paths(args)]
        return (env + ["Tools/Scripts/run-javascriptcore-tests"] + list(preset.flags)
                + ["--no-build", "--no-fail-fast", "--memory-limited", "--child-processes=%d" % jobs]
                + (["--filter", "^(%s)(\\..*)?$" % "|".join(named)] if named else []) + options)
    return (env + (SOFTWARE_GL if preset.system == CMAKE else []) + ["Tools/Scripts/run-webkit-tests"] + list(preset.flags)
            + ["--no-build", "--no-retry-failures", "--child-processes=%d" % jobs] + list(args))


def missing_argv(src, tests):
    """Prints each of `tests` (its ?query aside) that is under neither LayoutTests/ nor the checkout."""
    return ["sh", "-c", 'for t in "$@"; do p=${t%%\\?*}; [ -e "$0/LayoutTests/$p" ] || [ -e "$0/$p" ] || printf "%s\\n" "$t"; done',
            src] + list(tests)
