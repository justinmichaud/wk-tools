# Named build configurations.
#
# One place, replacing the init-debug / init-release / init-ios-release scripts
# and the five different WebKit root conventions they carried between them.
# A config sets:
#
#   CFG_PORT      the WebKit port  (jsc-only, gtk, wpe, or empty for Apple)
#   CFG_TYPE      Debug | Release
#   CFG_ARGS      extra arguments to Tools/Scripts/build-webkit
#   CFG_CMAKE     extra -D flags
#   CFG_CC/CXX    compiler
#   CFG_BUILDSYS  cmake | xcode
#
# CFG_BUILDSYS is not cosmetic: the two build systems take a job count through
# completely different channels. CMake ports get it via --makeargs=-jN, while
# the Apple ports run xcodebuild, which ignores --makeargs entirely and wants
# `-jobs N` passed through to it. Getting this wrong does not fail -- it
# silently builds at xcodebuild's own default parallelism, which is sized from
# core count and is exactly the thing this whole system exists to avoid.
#
# WebKit is built with clang, not the distro default.
#
# This is not a preference. GCC fails outright on aarch64 in
# JSObject::crashDueToEmptyValueAtValidOffset, which uses register variables
# inside an #if CPU(ARM64) block:
#
#   error: optimization may eliminate reads and/or writes to register
#          variables [-Werror=volatile-register-var]
#
# The wkdev image defaults c++ to GCC 15, so leaving the compiler unset means
# every aarch64 JSC build dies partway through Source/JavaScriptCore/runtime.
#
# This is Linux-specific. On macOS the Xcode toolchain is the only option, and
# forcing CC/CXX there would override the SDK's own choice for no benefit, so
# the Apple configs leave both unset.
#
# Job count and nice level are NOT set here -- they are derived per machine at
# build time by lib/resources.sh, because a number that is right on a 10-core
# laptop will hang a Raspberry Pi and waste a build server.

config_list() {
    cat <<'EOF'
jsc-debug          JSCOnly, Debug, assertions on, libbacktrace
jsc-release        JSCOnly, Release, the default for benchmarking
jsc-release-asan   JSCOnly, Release + AddressSanitizer
gtk-debug          GTK port, Debug, developer mode
gtk-release        GTK port, Release
gtk-release-asan   GTK port, Release + AddressSanitizer
wpe-release        WPE port, Release
mac-debug          macOS (Apple port), Debug, Xcode
mac-release        macOS (Apple port), Release, Xcode
mac-release-asan   macOS (Apple port), Release + AddressSanitizer
ios-sim-release    iOS Simulator, Release, Xcode
EOF
}

# Override with WK_CC / WK_CXX if a config needs something specific.
WK_CC="${WK_CC:-clang}"
WK_CXX="${WK_CXX:-clang++}"

# Where a config's build tree lands, relative to the checkout.
#
# Each port gets its own directory -- WebKitBuild/JSCOnly/Release,
# WebKitBuild/WPE/Release, WebKitBuild/GTK/Release -- so a workspace can hold a
# JSC build and a browser build at once without one clobbering the other. This
# is derived rather than assumed because getting it wrong is quiet: `wk run`
# reports "no such file", and `wk bench` reports "no MiniBrowser", both of which
# read as "the build failed" rather than "you looked in the wrong place".
# The Apple ports add a fourth layout: no port directory, and an SDK suffix for
# anything embedded (webkitdirs.pm appends it for isEmbeddedWebKit()), so a bare
# Release/ is only ever right for the Mac itself. The authority for the CMake
# side is usesPerConfigurationBuildDirectory() in the same file.
config_build_dir() {
    local root="${1:-/src/WebKit}"
    case "$CFG_BUILDSYS:$CFG_PORT" in
        xcode:--ios-simulator) echo "$root/WebKitBuild/$CFG_TYPE-iphonesimulator" ;;
        xcode:--ios-device)    echo "$root/WebKitBuild/$CFG_TYPE-iphoneos" ;;
        xcode:*)               echo "$root/WebKitBuild/$CFG_TYPE" ;;
        cmake:--jsc-only)      echo "$root/WebKitBuild/JSCOnly/$CFG_TYPE" ;;
        cmake:--wpe)           echo "$root/WebKitBuild/WPE/$CFG_TYPE" ;;
        cmake:--gtk)           echo "$root/WebKitBuild/GTK/$CFG_TYPE" ;;
        *)                     echo "$root/WebKitBuild/$CFG_TYPE" ;;
    esac
}

config_load() {
    CFG_PORT=""; CFG_TYPE=""; CFG_ARGS=""; CFG_CMAKE=""
    CFG_BUILDSYS=cmake
    CFG_CC="$WK_CC"; CFG_CXX="$WK_CXX"

    case "$1" in
        jsc-debug)
            CFG_PORT="--jsc-only"; CFG_TYPE=Debug
            CFG_ARGS="--debug --no-fatal-warnings"
            # ALT_ENTRY on in debug: the extra offline-asm entry points make
            # crash backtraces through generated code readable.
            CFG_CMAKE="-DUSE_LIBBACKTRACE=ON -DDEVELOPER_MODE=ON -DENABLE_OFFLINE_ASM_ALT_ENTRY=1"
            ;;
        jsc-release)
            CFG_PORT="--jsc-only"; CFG_TYPE=Release
            CFG_ARGS="--release"
            CFG_CMAKE="-DUSE_LIBBACKTRACE=OFF -DDEVELOPER_MODE=ON -DENABLE_OFFLINE_ASM_ALT_ENTRY=0"
            ;;
        jsc-release-asan)
            CFG_PORT="--jsc-only"; CFG_TYPE=Release
            CFG_ARGS="--release --asan"
            CFG_CMAKE="-DCMAKE_BUILD_TYPE=RelWithDebInfo -DDEVELOPER_MODE=ON -DUSE_LIBBACKTRACE=OFF"
            ;;
        gtk-debug)
            CFG_PORT="--gtk"; CFG_TYPE=Debug
            CFG_ARGS="--debug --no-fatal-warnings"
            CFG_CMAKE="-DUSE_LIBBACKTRACE=ON -DDEVELOPER_MODE=ON"
            ;;
        gtk-release)
            CFG_PORT="--gtk"; CFG_TYPE=Release
            CFG_ARGS="--release"
            CFG_CMAKE="-DDEVELOPER_MODE=ON"
            ;;
        gtk-release-asan)
            CFG_PORT="--gtk"; CFG_TYPE=Release
            CFG_ARGS="--release --asan --no-fatal-warnings"
            CFG_CMAKE="-DCMAKE_BUILD_TYPE=RelWithDebInfo -DDEVELOPER_MODE=ON -DUSE_LIBBACKTRACE=OFF -DUSE_VULKAN=OFF -DENABLE_THUNDER=OFF"
            ;;
        wpe-release)
            CFG_PORT="--wpe"; CFG_TYPE=Release
            CFG_ARGS="--release --no-fatal-warnings"
            CFG_CMAKE="-DDEVELOPER_MODE=ON -DENABLE_WPE_PLATFORM=ON -DENABLE_THUNDER=OFF"
            ;;
        # --- Apple ports ------------------------------------------------------
        # No port flag: build-webkit treats Apple Cocoa as the default on
        # Darwin, and there is no --mac to pass. The empty CFG_PORT is the
        # port selection.
        mac-debug)
            CFG_TYPE=Debug; CFG_BUILDSYS=xcode
            CFG_ARGS="--debug"
            CFG_CC=""; CFG_CXX=""
            ;;
        mac-release)
            CFG_TYPE=Release; CFG_BUILDSYS=xcode
            CFG_ARGS="--release"
            CFG_CC=""; CFG_CXX=""
            ;;
        mac-release-asan)
            CFG_TYPE=Release; CFG_BUILDSYS=xcode
            CFG_ARGS="--release --asan"
            CFG_CC=""; CFG_CXX=""
            ;;
        ios-sim-release)
            CFG_PORT="--ios-simulator"; CFG_TYPE=Release; CFG_BUILDSYS=xcode
            CFG_ARGS="--release"
            CFG_CC=""; CFG_CXX=""
            ;;
        *)
            return 1
            ;;
    esac
    return 0
}

# The environment a build runs with, assembled into the CFG_ENV array. Both
# `wk build` and the golden-base prebuild go through this, because two call
# sites spelling the same environment by hand is how they end up building
# different things.
#
# An array, not a string: WK_BUILD_ARGS holds several words as ONE value, and
# any string form of this gets word-split into `WK_BUILD_ARGS=` plus a stray
# `--release` that env then treats as the command to run.
#
# config_load must have run first. Sets CFG_ENV.
config_build_env() {
    local src="$1" jobs="$2" nice="$3"
    CFG_ENV=(
        "CCACHE_DIR=/ccache"
        "NUMBER_OF_PROCESSORS=$jobs"
        "CMAKE_BUILD_PARALLEL_LEVEL=$jobs"
        "WK_JOBS=$jobs"
        "WK_NICE=$nice"
        "WK_SRC=$src"
        "WK_BUILDSYS=$CFG_BUILDSYS"
        "WK_BUILD_ARGS=$CFG_PORT $CFG_ARGS"
        "WK_BUILD_CMAKE=$CFG_CMAKE"
        "WK_MB_PER_JOB=${WK_MB_PER_JOB:-1536}"
    )
    # Only when the config asks for it: the Apple configs leave these empty on
    # purpose, and `env CC= ` is not the same as not setting CC at all.
    [ -n "$CFG_CC" ] && CFG_ENV+=("CC=$CFG_CC" "CXX=$CFG_CXX")
    [ -n "${WK_COMPILE_COMMANDS:-}" ] && CFG_ENV+=("WK_COMPILE_COMMANDS=1")
    return 0
}

# --- where the products land -------------------------------------------------
# The two remaining layout differences, on top of config_build_dir above:
# JSCOnly and the CMake full ports put binaries in bin/ with shared libraries in
# lib/, while the Xcode build has no bin/ at all and resolves its libraries as
# frameworks. config_load must have run first.

config_jsc_path() {
    local d; d=$(config_build_dir "$1")
    case "$CFG_BUILDSYS" in
        xcode) echo "$d/jsc" ;;
        *)     echo "$d/bin/jsc" ;;
    esac
}

# The loader variable needed to run a binary out of the build tree. Framework
# path on Darwin, library path elsewhere -- same purpose, different spelling,
# and neither works for the other platform.
#
# The caller must PREPEND to whatever is already set, never replace it. The
# wkdev image serves libwpe and the rest of the jhbuild prefix through
# LD_LIBRARY_PATH set in the login profile, so overwriting it makes the WPE and
# GTK binaries fail to load libraries that were there all along.
config_run_var() {
    case "$CFG_BUILDSYS" in
        xcode) echo DYLD_FRAMEWORK_PATH ;;
        *)     echo LD_LIBRARY_PATH ;;
    esac
}

config_run_dir() {
    local d; d=$(config_build_dir "$1")
    case "$CFG_BUILDSYS" in
        xcode) echo "$d" ;;
        *)     echo "$d/lib" ;;
    esac
}
