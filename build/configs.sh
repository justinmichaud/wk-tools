# Named build configurations. One place; a config sets:
#
#   CFG_PORT      the WebKit port  (jsc-only, gtk, wpe, or empty for Apple)
#   CFG_TYPE      Debug | Release
#   CFG_ARGS      extra arguments to Tools/Scripts/build-webkit
#   CFG_CMAKE     extra -D flags
#   CFG_CC/CXX    compiler
#   CFG_BUILDSYS  cmake | xcode
#
# CFG_BUILDSYS matters: CMake ports take -jN via --makeargs, but xcodebuild
# wants -jobs N and ignores --makeargs -- get it wrong and the build runs at
# xcodebuild's own core-count default. clang, not GCC: GCC fails on aarch64
# in JSObject::crashDueToEmptyValueAtValidOffset. Apple configs leave CC/CXX
# unset -- Xcode's toolchain is the only option on macOS.

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

# Override with WK_CC / WK_CXX if a config needs something specific. clang
# everywhere, including armhf's own native armhf clang -- architecture flags
# live in lib/arch.sh instead, so a config's meaning stays fixed everywhere.
WK_CC="${WK_CC:-clang}"
WK_CXX="${WK_CXX:-clang++}"

# Loaded on demand: not every caller of this file has sourced lib/arch.sh.
command -v arch_canon >/dev/null 2>&1 || . "$WK_ROOT/lib/arch.sh"

# Where a config's build tree lands, one directory per port. The `-asan`
# suffix is ours: without it mac-release and mac-release-asan share one
# Release/ tree, half instrumented after building each.

# What a config contributes to --cmakeargs, for an error message.
config_cmake_summary() {
    local c="${CFG_CMAKE:-}"
    [ -n "$c" ] || { echo "the flags it sets"; return 0; }
    printf '%s' "$c"
}

config_build_dir() {
    local root="${1:-/src/WebKit}"
    local variant=""
    case "$CFG_BUILDSYS:$CFG_ARGS" in xcode:*--asan*) variant="-asan" ;; esac
    case "$CFG_BUILDSYS:$CFG_PORT" in
        xcode:--ios-simulator) echo "$root/WebKitBuild/$CFG_TYPE-iphonesimulator$variant" ;;
        xcode:--ios-device)    echo "$root/WebKitBuild/$CFG_TYPE-iphoneos$variant" ;;
        xcode:*)               echo "$root/WebKitBuild/$CFG_TYPE$variant" ;;
        cmake:--jsc-only)      echo "$root/WebKitBuild/JSCOnly/$CFG_TYPE" ;;
        cmake:--wpe)           echo "$root/WebKitBuild/WPE/$CFG_TYPE" ;;
        cmake:--gtk)           echo "$root/WebKitBuild/GTK/$CFG_TYPE" ;;
        *)                     echo "$root/WebKitBuild/$CFG_TYPE" ;;
    esac
}

# "Release, with debug info" on the CMake ports. DEBUG_FISSION
# (-gsplit-dwarf) keeps debug info in .dwo files beside the objects, making
# "release with -g" affordable. Flags are pinned because CMake's own
# RelWithDebInfo is -O2 where Release is -O3 -- adopting the build type alone
# would drop every release build, including the two -asan configs, by an
# optimization level.
_CFG_RELWITHDEBINFO='-DCMAKE_BUILD_TYPE=RelWithDebInfo'
_CFG_RELWITHDEBINFO="$_CFG_RELWITHDEBINFO -DCMAKE_C_FLAGS_RELWITHDEBINFO=\"-O3 -g -DNDEBUG\""
_CFG_RELWITHDEBINFO="$_CFG_RELWITHDEBINFO -DCMAKE_CXX_FLAGS_RELWITHDEBINFO=\"-O3 -g -DNDEBUG\""

# DEBUG_FISSION is stated rather than left to its default: WebKitCommon.cmake
# computes that default before Options${PORT} sets ENABLE_DEVELOPER_MODE, so
# no WPE or GTK build ever turns fission on by itself.
_CFG_RELWITHDEBINFO="$_CFG_RELWITHDEBINFO -DDEBUG_FISSION=ON"

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
            CFG_CMAKE="$_CFG_RELWITHDEBINFO -DUSE_LIBBACKTRACE=OFF -DDEVELOPER_MODE=ON -DENABLE_OFFLINE_ASM_ALT_ENTRY=0"
            ;;
        jsc-release-asan)
            CFG_PORT="--jsc-only"; CFG_TYPE=Release
            CFG_ARGS="--release --asan"
            CFG_CMAKE="$_CFG_RELWITHDEBINFO -DDEVELOPER_MODE=ON -DUSE_LIBBACKTRACE=OFF"
            ;;
        gtk-debug)
            CFG_PORT="--gtk"; CFG_TYPE=Debug
            CFG_ARGS="--debug --no-fatal-warnings"
            CFG_CMAKE="-DUSE_LIBBACKTRACE=ON -DDEVELOPER_MODE=ON"
            ;;
        gtk-release)
            CFG_PORT="--gtk"; CFG_TYPE=Release
            CFG_ARGS="--release"
            CFG_CMAKE="$_CFG_RELWITHDEBINFO -DDEVELOPER_MODE=ON"
            ;;
        gtk-release-asan)
            CFG_PORT="--gtk"; CFG_TYPE=Release
            CFG_ARGS="--release --asan --no-fatal-warnings"
            CFG_CMAKE="$_CFG_RELWITHDEBINFO -DDEVELOPER_MODE=ON -DUSE_LIBBACKTRACE=OFF -DUSE_VULKAN=OFF -DENABLE_THUNDER=OFF"
            ;;
        wpe-release)
            CFG_PORT="--wpe"; CFG_TYPE=Release
            CFG_ARGS="--release --no-fatal-warnings"
            CFG_CMAKE="$_CFG_RELWITHDEBINFO -DDEVELOPER_MODE=ON -DENABLE_WPE_PLATFORM=ON -DENABLE_THUNDER=OFF"
            ;;
        # --- Apple ports ------------------------------------------------------
        # No port flag: build-webkit defaults to Apple Cocoa on Darwin and
        # there is no --mac to pass. Empty CFG_PORT is the port selection.
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

    # Decided here so the job count, the watchdog's budget and the value
    # carried into the target all agree; an explicit WK_MB_PER_JOB wins.
    [ -n "${WK_MB_PER_JOB_EXPLICIT:-}" ] || WK_MB_PER_JOB=$(config_mb_per_job)
    return 0
}

# The environment a build runs with, assembled into CFG_ENV. config_load
# must have run first. Architecture is passed in rather than read here: this
# is also used by the golden-base prebuild, which has no current workspace.

# How much memory one compile job is worth. 1536 MB fits the CMake ports
# (WebKit TUs peak near 1-1.5 GB); the Apple build's peak is dominated by
# one step, the big link, and wants ~17 GB regardless of job count.
config_mb_per_job() {
    case "$CFG_BUILDSYS" in
        xcode) echo 3072 ;;
        *)     echo 1536 ;;
    esac
}

config_build_env() {
    local src="$1" jobs="$2" nice="$3" arch="${4:-native}"

    # --- Apple ports: pin where the output and the caches go -----------------
    # Xcode's default DerivedData is one machine-wide directory per *user*,
    # shared by every workspace on the macOS remote target. Paths below
    # derive from $src instead, so they're per workspace; only what is *not*
    # content-addressed is separated per config, via WEBKIT_OUTPUTDIR.
    local out=""
    if [ "$CFG_BUILDSYS" = xcode ]; then
        out=$(config_build_dir "$src")
    fi

    # Appended in order (architecture, machine conf, `wk build --cmake`), so
    # each wins where it overlaps -- cmake takes the last repeated -D.
    local cmakeargs="$CFG_CMAKE"
    local archcmake; archcmake=$(arch_cmake "$arch" "$CFG_PORT")
    [ -n "$archcmake" ] && cmakeargs="$cmakeargs $archcmake"
    [ -n "${WK_TARGET_CMAKE:-}" ] && cmakeargs="$cmakeargs $WK_TARGET_CMAKE"
    [ -n "${WK_EXTRA_CMAKE:-}" ] && cmakeargs="$cmakeargs $WK_EXTRA_CMAKE"

    # /ccache is the container's bind-mounted store cache; see t_ccache_dir
    # for other targets. Sloppiness desensitises __TIMESTAMP__ and the glib
    # ports' regenerated BuildRevision.h.
    CFG_ENV=(
        "CCACHE_DIR=${WK_CCACHE_DIR:-/ccache}"
        "CCACHE_BASEDIR=$src"
        "CCACHE_SLOPPINESS=pch_defines,time_macros,include_file_mtime,include_file_ctime"
        "CCACHE_NOHASHDIR=true"
        "NUMBER_OF_PROCESSORS=$jobs"
        "CMAKE_BUILD_PARALLEL_LEVEL=$jobs"
        "WK_JOBS=$jobs"
        "WK_NICE=$nice"
        "WK_SRC=$src"
        "WK_BUILDSYS=$CFG_BUILDSYS"
        # WK_TARGET_BUILD_ARGS is targets/hosts/<name>.conf's WK_BUILD_ARGS,
        # named differently so folding it in doesn't overwrite these flags.
        "WK_BUILD_ARGS=$CFG_PORT $CFG_ARGS${WK_TARGET_BUILD_ARGS:+ $WK_TARGET_BUILD_ARGS}"
        "WK_BUILD_CMAKE=$cmakeargs"
        "WK_BUILD_DIR=$(config_build_dir "$src")"
        "WK_MB_PER_JOB=$WK_MB_PER_JOB"
    )
    # `env CC= ` is not the same as not setting CC: the Apple configs leave
    # these empty on purpose, so only set when the config asks for it.
    [ -n "$CFG_CC" ] && CFG_ENV+=("CC=$CFG_CC" "CXX=$CFG_CXX")

    # Stated rather than left implicit: WebKitCCache.cmake reads this to wire
    # ccache in, so leaving it off is one `--no-use-ccache` away from every
    # JSC build going cold with no message.
    [ "$CFG_PORT" = --jsc-only ] && CFG_ENV+=("WK_USE_CCACHE=YES")

    # Only for non-native: part of every ccache hash; empty ones would
    # invalidate a cache native builds already share.
    if ! arch_is_native "$arch"; then
        CFG_ENV+=(
            "WK_ARCH=$arch"
            "WK_ARCH_WRAPPER=$(arch_wrapper "$arch")"
            "WK_ARCH_CFLAGS=$(arch_cflags "$arch")"
            "WK_ARCH_LDFLAGS=$(arch_ldflags "$arch")"
        )
    fi
    # Xcode only: WEBKIT_OUTPUTDIR on a CMake port collapses every per-port
    # layout. Separate variables, not WK_BUILD_ARGS, which is word-split.
    if [ -n "$out" ]; then
        CFG_ENV+=("WEBKIT_OUTPUTDIR=$out" "WK_DERIVED_DATA=$src/WebKitBuild/DerivedData")
    fi
    # Carried through only when set: empty isn't unset for build-in-target.sh.
    [ -n "${WK_MEM_BUDGET_MB:-}" ] && CFG_ENV+=("WK_MEM_BUDGET_MB=$WK_MEM_BUDGET_MB")
    [ -n "${WK_MEM_FLOOR_MB:-}" ]  && CFG_ENV+=("WK_MEM_FLOOR_MB=$WK_MEM_FLOOR_MB")
    [ -n "${WK_MEM_INTERVAL:-}" ]  && CFG_ENV+=("WK_MEM_INTERVAL=$WK_MEM_INTERVAL")

    [ -n "${WK_NO_COMPILE_COMMANDS:-}" ] && CFG_ENV+=("WK_NO_COMPILE_COMMANDS=1")
    [ -n "${WK_NO_COMPILATION_CACHE:-}" ] && CFG_ENV+=("WK_NO_COMPILATION_CACHE=1")

    # `wk build … --env CC=gcc-14`, last: `env` applies left to right, so
    # this overrides the config's own. Newline-, not space-separated: an
    # environment value may legitimately contain a space (CFLAGS).
    if [ -n "${WK_EXTRA_ENV:-}" ]; then
        while IFS= read -r _e; do
            [ -n "$_e" ] || continue
            CFG_ENV+=("$_e")
        done <<EOF
$WK_EXTRA_ENV
EOF
    fi
    return 0
}

# --- where the products land: bin/+lib/ on CMake ports, no bin/ (frameworks)
# on Xcode. config_load must have run first. ------------------------------

config_jsc_path() {
    local d; d=$(config_build_dir "$1")
    case "$CFG_BUILDSYS" in
        xcode) echo "$d/jsc" ;;
        *)     echo "$d/bin/jsc" ;;
    esac
}

# The loader variable to run a binary out of the build tree. The caller must
# PREPEND, never replace: wkdev already serves libwpe through LD_LIBRARY_PATH.
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

# --- the browser, and the processes it spawns --------------------------------
# MiniBrowser is one binary on the CMake ports and an app bundle on the Apple
# ports; the path below is the binary inside it, not the bundle, so the
# browser stays a child of the launching shell and ctrl-c/a debugger reach
# it. Its page is `--url <URL>` (AppDelegate.m:47,265); a bare positional URL
# is silently ignored.

config_browser_path() {
    local d; d=$(config_build_dir "$1")
    case "$CFG_BUILDSYS" in
        xcode) echo "$d/MiniBrowser.app/Contents/MacOS/MiniBrowser" ;;
        *)     echo "$d/bin/MiniBrowser" ;;
    esac
}

# One port takes a positional URL; the other ignores it.
config_browser_url_flag() {
    case "$CFG_BUILDSYS" in
        xcode) echo "--url" ;;
        *)     echo "" ;;
    esac
}

# The environment a WebKit app needs to find its own frameworks. Apple only.
# The __XPC_ duplicates are load-bearing: launchd turns __XPC_FOO into FOO
# for the XPC child services (WebContent, GPU, Networking), so without them
# those children run against the system WebKit instead of the build tree.
config_browser_env() {
    local d; d=$(config_build_dir "$1")
    case "$CFG_BUILDSYS" in
        xcode) echo "DYLD_FRAMEWORK_PATH=$d DYLD_LIBRARY_PATH=$d __XPC_DYLD_FRAMEWORK_PATH=$d __XPC_DYLD_LIBRARY_PATH=$d" ;;
        *)     echo "" ;;
    esac
}

# What the web process is called, for `lldb --attach-name ... --waitfor`. A
# wrong name here does not fail, it *waits*, reading as a hung debugger.
config_web_process_name() {
    case "$CFG_BUILDSYS:$CFG_PORT" in
        xcode:*)     echo "com.apple.WebKit.WebContent.Development" ;;
        cmake:--wpe) echo "WPEWebProcess" ;;
        cmake:--gtk) echo "WebKitWebProcess" ;;
        *)           echo "" ;;
    esac
}

# The network and GPU process names, same shape as config_web_process_name
# and the same use: naming a process to attach to. CMake ports only --
# Source/WebKit/Platform{WPE,GTK}.cmake:17-18.
config_network_process_name() {
    case "$CFG_BUILDSYS:$CFG_PORT" in
        cmake:--wpe) echo "WPENetworkProcess" ;;
        cmake:--gtk) echo "WebKitNetworkProcess" ;;
        *)           echo "" ;;
    esac
}

config_gpu_process_name() {
    case "$CFG_BUILDSYS:$CFG_PORT" in
        cmake:--wpe) echo "WPEGPUProcess" ;;
        cmake:--gtk) echo "WebKitGPUProcess" ;;
        *)           echo "" ;;
    esac
}

# The layout-test driver, for the same attach: its stdin/stdout are the test
# protocol, which `--wrapper 'lldb'` would otherwise interrupt.
config_test_runner_name() {
    case "$CFG_BUILDSYS:$CFG_PORT" in
        xcode:*)                 echo "WebKitTestRunner" ;;
        cmake:--wpe|cmake:--gtk) echo "WebKitTestRunner" ;;
        *)                       echo "" ;;
    esac
}

# Makes the web process sleep after launch so a waiting debugger beats
# `--waitfor`'s race with dyld start-up. Spelled twice: the Apple ports need
# __XPC_FOO (launchd turns it into FOO); the GLib ports fork directly and
# inherit the plain name. Both readers require ENABLE(DEVELOPER_MODE).
config_web_process_pause_env() {
    case "$CFG_BUILDSYS" in
        xcode) echo "__XPC_WEBKIT_PAUSE_WEB_PROCESS_ON_LAUNCH=1" ;;
        cmake) echo "WEBKIT_PAUSE_WEB_PROCESS_ON_LAUNCH=1" ;;
        *)     echo "" ;;
    esac
}
