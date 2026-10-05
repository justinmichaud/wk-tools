"""The build presets as data, resolved for a place's os and driver."""

import shlex

from wk import act, fleet, images, resources
from wk.store import dispatch_place

LIST_TEXT = """\
jsc-debug          JSCOnly, Debug, assertions on
jsc-release        JSCOnly, Release, the default for benchmarking
jsc-release-asan   JSCOnly, Release + AddressSanitizer
gtk-debug          GTK port, Debug, developer mode
gtk-release        GTK port, Release
gtk-release-asan   GTK port, Release + AddressSanitizer
wpe-release        WPE port, Release
mac-debug          macOS (Apple port), Debug, Xcode
mac-release        macOS (Apple port), Release, Xcode
mac-release-pgo    macOS (Apple port), Release + PGO and full LTO -- the perf build
mac-release-asan   macOS (Apple port), Release + AddressSanitizer
ios-sim-release    iOS Simulator, Release, Xcode

In a macOS workspace the three jsc-* presets build the Apple port's
JavaScriptCore with Xcode instead: there is no JSCOnly port there.
"""

# CMake's RelWithDebInfo is -O2 where Release is -O3, so the flags are pinned; DEBUG_FISSION is stated because WebKitCommon.cmake computes its default before DEVELOPER_MODE is set.
RELWITHDEBINFO = ('-DCMAKE_BUILD_TYPE=RelWithDebInfo -DCMAKE_C_FLAGS_RELWITHDEBINFO="-O3 -g -DNDEBUG"'
                  ' -DCMAKE_CXX_FLAGS_RELWITHDEBINFO="-O3 -g -DNDEBUG" -DDEBUG_FISSION=ON')
DEFAULT_ARGS = "--no-fatal-warnings"
DEFAULT_CMAKE = "-DDEVELOPER_MODE=ON -DUSE_VULKAN=OFF -DENABLE_THUNDER=OFF"
LIBCXX_CMAKE = ("-DCMAKE_CXX_FLAGS=-stdlib=libc++ -DCMAKE_EXE_LINKER_FLAGS=-stdlib=libc++"
                " -DCMAKE_SHARED_LINKER_FLAGS=-stdlib=libc++ -DCMAKE_MODULE_LINKER_FLAGS=-stdlib=libc++")
LIBBACKTRACE = {"container": "ON", "vm": "ON", "local": "ON", "remote": "OFF"}   # buildbox4 has no libbacktrace package
DISK_GB = 25
PGO_DISK_GB = 60   # two phases' products, 45 GB measured 2026-09-06 with the compilation cache off
CCACHE_SLOPPINESS = "pch_defines,time_macros,include_file_mtime,include_file_ctime"

ARCH = {"armhf": {"wrapper": "linux32", "cflags": "-mthumb -march=armv7-a+fp -Wno-pass-failed",
                  "ldflags": "-mthumb -march=armv7-a+fp -fuse-ld=gold -Wl,--no-map-whole-files -Wl,--no-keep-memory"
                             " -Wl,--no-keep-files-mapped -Wl,--no-mmap-output-file",
                  "cmake": "-DUSE_LD_LLD=OFF",
                  "cmake_port": {"--wpe": "-DUSE_VULKAN=OFF -DENABLE_WEB_RTC=OFF -DENABLE_WPE_QT_API=OFF",
                                 "--gtk": "-DUSE_VULKAN=OFF -DENABLE_WEB_RTC=OFF -DENABLE_WPE_QT_API=OFF"}}}

APPLE_JSC = {"buildsys": "xcode", "script": "Tools/Scripts/build-jsc", "port": "", "cmake": "", "apple": True}
PRESETS = {
    "jsc-debug": {"type": "Debug", "jsc_only": True, "args": "--debug", "port": "--jsc-only",
                  "cmake": "-DENABLE_OFFLINE_ASM_ALT_ENTRY=1", "macos": APPLE_JSC},
    "jsc-release": {"type": "Release", "jsc_only": True, "args": "--release", "port": "--jsc-only",
                    "cmake": RELWITHDEBINFO + " -DENABLE_OFFLINE_ASM_ALT_ENTRY=0", "macos": APPLE_JSC},
    "jsc-release-asan": {"type": "Release", "jsc_only": True, "args": "--release --asan", "port": "--jsc-only",
                         "cmake": RELWITHDEBINFO,
                         "macos": dict(APPLE_JSC, variant="-asan", args="--release ASAN=YES")},
    "gtk-debug": {"type": "Debug", "args": "--debug", "port": "--gtk"},
    "gtk-release": {"type": "Release", "args": "--release", "port": "--gtk", "cmake": RELWITHDEBINFO},
    "gtk-release-asan": {"type": "Release", "args": "--release --asan", "port": "--gtk", "cmake": RELWITHDEBINFO},
    "wpe-release": {"type": "Release", "args": "--release", "port": "--wpe", "cmake": RELWITHDEBINFO + " -DENABLE_WPE_PLATFORM=ON"},
    "mac-debug": {"type": "Debug", "args": "--debug", "apple": True},
    "mac-release": {"type": "Release", "args": "--release", "apple": True},
    "mac-release-pgo": {"type": "Release", "args": "--release", "apple": True, "variant": "-pgo", "pgo": True,
                        "disk_gb": PGO_DISK_GB},
    "mac-release-asan": {"type": "Release", "args": "--release --asan", "apple": True, "variant": "-asan"},
    "ios-sim-release": {"type": "Release", "args": "--release", "apple": True, "port": "--ios-simulator"},
}


def names():
    return list(PRESETS)


ARCHES = ("native", "armhf")
ARCH_NAMES = {"": "native", "native": "native", "host": "native", "arm64": "native", "aarch64": "native", "64": "native",
              "armhf": "armhf", "arm32": "armhf", "armv7": "armhf", "arm": "armhf", "32": "armhf"}
# Pinned, arm64 with armhf multiarch: `wkdev-create --arch` would hand podman the aarch64 image with --arch=arm.
IMAGE_ARMHF = "ghcr.io/igalia/wkdev-sdk:24.04_arm32"


def arch_canon(arch):
    if arch in ARCH_NAMES:
        return ARCH_NAMES[arch]
    act.die("unknown architecture '%s' (one of: %s)\n"
            "    A workspace's --arch is what it runs *natively*. To build for something\n"
            "    this machine cannot execute, that is a cross build -- see\n"
            "    docs/Nice to have/HANDOFF-cross-compile.md." % (arch, " ".join(ARCHES)))


def arch_has_gpu(arch):
    return (arch or "native") != "armhf"


def arch_label(arch):
    return "" if arch in ("", "native") else arch


def arch_cmake(arch, port):
    a = ARCH.get(arch)
    if not a:
        return ""
    extra = a["cmake_port"].get(port, "")
    return a["cmake"] + (" " + extra if extra else "")


def machine_cmake(env, preset=None):
    return env.get("WK_REMOTE_CMAKE", "") if preset is None else machine_var(env, "WK_REMOTE_CMAKE", preset)


def build_args(env, preset=None):
    return env.get("WK_BUILD_ARGS", "") if preset is None else machine_var(env, "WK_BUILD_ARGS", preset)


def disk_gb(env):
    return int(env.get("WK_BUILD_DISK_GB") or DISK_GB)


class Preset:
    def __init__(self, name, os_name, kind, spec, env):
        self.name, self.os, self.kind = name, os_name, kind
        self.type = spec["type"]
        self.jsc_only = bool(spec.get("jsc_only"))
        self.port = spec.get("port", "")
        self.args = spec.get("args", "")
        self.cmake = spec.get("cmake", "")
        self.variant = spec.get("variant", "")
        self.pgo = bool(spec.get("pgo"))
        apple = spec.get("apple")
        self.buildsys = spec.get("buildsys", "xcode" if apple else "cmake")
        self.script = spec.get("script", "Tools/Scripts/build-webkit")
        self.cc = "" if apple else (env.get("WK_CC") or "clang")
        self.cxx = "" if apple else (env.get("WK_CXX") or "clang++")
        self.disk_gb = spec.get("disk_gb") or disk_gb(env)

    def build_subdir(self):
        """The build tree under the checkout; the variant suffix keeps a sanitized or profile-guided Apple build out of the shared one."""
        if self.buildsys == "xcode":
            sdk = {"--ios-simulator": "-iphonesimulator", "--ios-device": "-iphoneos"}.get(self.port, "")
            return "WebKitBuild/%s%s%s" % (self.type, sdk, self.variant)
        port = {"--jsc-only": "JSCOnly/", "--wpe": "WPE/", "--gtk": "GTK/"}.get(self.port, "")
        return "WebKitBuild/%s%s" % (port, self.type)

    def build_dir(self, src="/src/WebKit"):
        return "%s/%s" % (src, self.build_subdir())

    def xcode(self):
        return self.buildsys == "xcode"

    def jsc_path(self, src):
        return self.build_dir(src) + ("/jsc" if self.xcode() else "/bin/jsc")

    def run_var(self):
        return "DYLD_FRAMEWORK_PATH" if self.xcode() else "LD_LIBRARY_PATH"

    def run_dir(self, src):
        return self.build_dir(src) + ("" if self.xcode() else "/lib")

    def browser_path(self, src):
        return self.build_dir(src) + ("/MiniBrowser.app/Contents/MacOS/MiniBrowser" if self.xcode() else "/bin/MiniBrowser")

    def browser_url_flag(self):
        return "--url" if self.xcode() else ""

    BROWSER_ENV = ("DYLD_FRAMEWORK_PATH", "DYLD_LIBRARY_PATH", "__XPC_DYLD_FRAMEWORK_PATH", "__XPC_DYLD_LIBRARY_PATH")

    def browser_env(self, src):
        """launchd turns __XPC_FOO into FOO for the XPC children, which would otherwise run the system WebKit."""
        return " ".join("%s=%s" % (v, self.build_dir(src)) for v in self.BROWSER_ENV) if self.xcode() else ""

    def _proc(self, xcode, wpe, gtk):
        if self.jsc_only:
            return ""
        if self.xcode():
            return xcode
        return {"--wpe": wpe, "--gtk": gtk}.get(self.port, "")

    def web_process_name(self):
        return self._proc("com.apple.WebKit.WebContent.Development", "WPEWebProcess", "WebKitWebProcess")

    def network_process_name(self):
        return self._proc("", "WPENetworkProcess", "WebKitNetworkProcess")

    def gpu_process_name(self):
        return self._proc("", "WPEGPUProcess", "WebKitGPUProcess")

    def test_runner_name(self):
        return self._proc("WebKitTestRunner", "WebKitTestRunner", "WebKitTestRunner")

    def web_process_pause_env(self):
        return ("__XPC_" if self.xcode() else "") + "WEBKIT_PAUSE_WEB_PROCESS_ON_LAUNCH=1"

    def mb_per_job(self):
        return 1536 if self.jsc_only and not self.xcode() else 3072

    def cmake_summary(self):
        return self.cmake or "the flags it sets"


def resolve(name, os_name, kind, env):
    """LookupError for a name that is none; a refusal (`Refused`) for one this place cannot build."""
    if name not in PRESETS:
        raise LookupError(name)
    spec = PRESETS[name]
    if os_name == "macos" and "macos" in spec:
        spec = dict(spec, **spec["macos"])
    preset = Preset(name, os_name, kind, spec, env)
    if preset.xcode() and os_name != "macos":
        act.die("'%s' is an Apple-port preset and builds with Xcode, which this\n    workspace has no way to run (it is %s). The presets that build here\n"
                "    are the CMake ports:  wk build --list" % (name, os_name))
    if preset.buildsys == "cmake":
        if kind not in LIBBACKTRACE:
            act.die("presets.resolve: unknown driver '%s'" % kind)
        preset.args = DEFAULT_ARGS + (" " + preset.args if preset.args else "")
        default = "%s -DUSE_LIBBACKTRACE=%s" % (DEFAULT_CMAKE, LIBBACKTRACE[kind])
        if libcxx(env):
            default += " " + LIBCXX_CMAKE
        preset.cmake = default + (" " + preset.cmake if preset.cmake else "")
    return preset


def libcxx(env):
    v = env.get("WK_REMOTE_LIBCXX", "")
    if v in ("0", ""):
        return False
    if v == "1":
        return True
    act.die("libcxx='%s' in %s\n    is neither 1 nor 0. It says whether that machine has libc++:\n"
            "    1 to build with -stdlib=libc++, 0 (or unset) to leave it out."
            % (v, fleet.Fleet(images.root(env), env).path(dispatch_place(env) or "<place>")))


def machine_var(env, stem, preset):
    return env.get("%s_%s" % (stem, preset.name.replace("-", "_")), "")


def _requote(flag):
    return '%s="%s"' % tuple(flag.split("=", 1)) if " " in flag else flag


def merge_cxx_flags(text):
    cxx, out = [], []
    for a in shlex.split(text):
        if a.startswith("-DCMAKE_CXX_FLAGS="):
            cxx.append(a[len("-DCMAKE_CXX_FLAGS="):])
        else:
            out.append(_requote(a))
    if cxx:
        out.append('-DCMAKE_CXX_FLAGS="%s"' % " ".join(cxx))
    return " ".join(out)


def mb_per_job(preset, env):
    return resources.mb_per_job_setting(env, preset.mb_per_job())


def build_env(preset, src, jobs, nice, arch, ccache_dir, env, extra_cmake="", extra_env=(), machine_build_args=""):
    """What build/build-in-workspace.sh runs under, narrowest last: `env` applies left to right."""
    cmake = [preset.cmake, arch_cmake(arch, preset.port), machine_cmake(env), machine_cmake(env, preset), extra_cmake]
    preset_args = build_args(env, preset)
    args = "%s %s%s%s" % (preset.port, preset.args, " " + machine_build_args if machine_build_args else "", " " + preset_args if preset_args else "")
    out = ["CCACHE_DIR=" + ccache_dir, "CCACHE_BASEDIR=" + src, "CCACHE_SLOPPINESS=" + CCACHE_SLOPPINESS,
           "CCACHE_NOHASHDIR=true", "NUMBER_OF_PROCESSORS=%s" % jobs, "CMAKE_BUILD_PARALLEL_LEVEL=%s" % jobs,
           "WK_JOBS=%s" % jobs, "WK_NICE=%s" % nice, "WK_SRC=" + src, "WK_BUILDSYS=" + preset.buildsys,
           "WK_BUILD_SCRIPT=" + preset.script, "WK_BUILD_ARGS=" + args,
           "WK_BUILD_CMAKE=" + merge_cxx_flags(" ".join(c for c in cmake if c)),
           "WK_BUILD_DIR=" + preset.build_dir(src), "WK_MB_PER_JOB=%d" % mb_per_job(preset, env)]
    if preset.cc:
        out += ["CC=" + preset.cc, "CXX=" + preset.cxx]
    if preset.port == "--jsc-only":
        out.append("WK_USE_CCACHE=YES")   # WebKitCCache.cmake reads it; without it every JSC build goes cold silently
    if arch in ARCH:
        a = ARCH[arch]
        out += ["WK_ARCH=" + arch, "WK_ARCH_WRAPPER=" + a["wrapper"], "WK_ARCH_CFLAGS=" + a["cflags"], "WK_ARCH_LDFLAGS=" + a["ldflags"]]
    out_dir = preset.build_dir(src) if preset.xcode() else ""   # Xcode's DerivedData is per user, shared by every workspace on a machine
    if out_dir:
        out += ["WEBKIT_OUTPUTDIR=" + out_dir, "WK_DERIVED_DATA=%s/WebKitBuild/DerivedData" % src]
    if preset.pgo:
        out += ["WK_PGO=1", "WK_PGO_DIR=%s-profile" % out_dir, "WK_NO_COMPILE_COMMANDS=1", "WK_NO_COMPILATION_CACHE=1"]
    for v in ("WK_MEM_BUDGET_MB", "WK_MEM_FLOOR_MB", "WK_MEM_INTERVAL"):
        if env.get(v):
            out.append("%s=%s" % (v, env[v]))
    for v in ("WK_NO_COMPILE_COMMANDS", "WK_NO_COMPILATION_CACHE"):
        if env.get(v):
            out.append(v + "=1")
    return out + [e for e in extra_env if e]
