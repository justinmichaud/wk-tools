# Named build configurations.
#
# One place, replacing the init-debug / init-release / init-ios-release scripts
# and the five different WebKit root conventions they carried between them.
# A config sets:
#
#   CFG_PORT      the WebKit port  (jsc-only, gtk, wpe)
#   CFG_TYPE      Debug | Release
#   CFG_ARGS      extra arguments to Tools/Scripts/build-webkit
#   CFG_CMAKE     extra -D flags
#   CFG_CC/CXX    compiler
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
config_build_dir() {
    local root="${1:-/src/WebKit}"
    case "$CFG_PORT" in
        --jsc-only) echo "$root/WebKitBuild/JSCOnly/$CFG_TYPE" ;;
        --wpe)      echo "$root/WebKitBuild/WPE/$CFG_TYPE" ;;
        --gtk)      echo "$root/WebKitBuild/GTK/$CFG_TYPE" ;;
        *)          echo "$root/WebKitBuild/$CFG_TYPE" ;;
    esac
}

config_load() {
    CFG_PORT=""; CFG_TYPE=""; CFG_ARGS=""; CFG_CMAKE=""
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
        *)
            return 1
            ;;
    esac
    return 0
}
