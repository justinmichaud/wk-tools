# Named build configurations, in one place: `config_load <name> <os> <kind>` sets CFG_PORT, CFG_TYPE, CFG_ARGS, CFG_CMAKE, CFG_CC/CXX, CFG_BUILDSYS (cmake | xcode), CFG_SCRIPT (the Tools/Scripts entry point) and CFG_JSC_ONLY. Every CMake config also starts with _CFG_DEFAULT_ARGS and _CFG_DEFAULT_CMAKE below, plus USE_LIBBACKTRACE (from <kind>) and the libc++ flags unless the target's conf says otherwise. <os> is the *target's* (t_os, lib/target.sh).
# clang and not GCC, which fails on aarch64 in JSObject::crashDueToEmptyValueAtValidOffset. The Apple configs leave CC/CXX unset: Xcode's toolchain is the only option on macOS, and so is Xcode itself, build-webkit's CMake path needing a generator that supports Swift. So there is no JSCOnly port in a macOS guest, and the three jsc-* configs mean Tools/Scripts/build-jsc there -- Xcode's "Everything up to JavaScriptCore" scheme, in the same products directory as mac-debug/mac-release.

config_list() {   # `wk build --list` names no workspace, so it cannot know the platform
    cat <<'EOF'
jsc-debug          JSCOnly, Debug, assertions on
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

In a macOS workspace the three jsc-* configs build the Apple port's
JavaScriptCore with Xcode instead: there is no JSCOnly port there.
EOF
}

WK_CC="${WK_CC:-clang}"   # clang everywhere, WK_CC / WK_CXX overriding what a config asks for; architecture flags live in lib/arch.sh and not in a config
WK_CXX="${WK_CXX:-clang++}"

command -v arch_canon >/dev/null 2>&1 || . "$WK_ROOT/lib/arch.sh"
command -v target_registry_conf >/dev/null 2>&1 || . "$WK_ROOT/lib/target.sh"   # on demand: not every caller of this file has sourced these

config_cmake_summary() {
    local c="${CFG_CMAKE:-}"
    [ -n "$c" ] || { echo "the flags it sets"; return 0; }
    printf '%s' "$c"
}

config_build_dir() {
    local root="${1:-/src/WebKit}"
    local variant=""   # the `-asan` suffix is ours, the two mac-release configs otherwise sharing one tree; both spellings of "instrumented" count, build-webkit's --asan and build-jsc's ASAN=YES
    case "$CFG_BUILDSYS:$CFG_ARGS" in
        xcode:*--asan*|xcode:*ASAN=YES*) variant="-asan" ;;
    esac
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

# "Release, with debug info" on the CMake ports, DEBUG_FISSION (-gsplit-dwarf) keeping it in .dwo files beside the objects. The flags are pinned because CMake's RelWithDebInfo is -O2 where Release is -O3, so the build type alone would drop every release build by an optimization level.
_CFG_RELWITHDEBINFO='-DCMAKE_BUILD_TYPE=RelWithDebInfo'
_CFG_RELWITHDEBINFO="$_CFG_RELWITHDEBINFO -DCMAKE_C_FLAGS_RELWITHDEBINFO=\"-O3 -g -DNDEBUG\""
_CFG_RELWITHDEBINFO="$_CFG_RELWITHDEBINFO -DCMAKE_CXX_FLAGS_RELWITHDEBINFO=\"-O3 -g -DNDEBUG\""

# The flags that are a property not of any one config but of how this repository builds WebKit at all; a config's own are appended after, and cmake takes the last repeated -D. CMake ports only: xcodebuild takes no -D flags, and build-webkit's Apple path has no --no-fatal-warnings.
#   --no-fatal-warnings: DEVELOPER_MODE=ON makes a compiler warning stop the build, and a warning from a clang newer than the tree was written against is not ours to fix. USE_VULKAN/THUNDER: neither is measured here, and both pull in dependencies a build machine may not have.
_CFG_DEFAULT_ARGS='--no-fatal-warnings'
_CFG_DEFAULT_CMAKE='-DDEVELOPER_MODE=ON -DUSE_VULKAN=OFF -DENABLE_THUNDER=OFF'

# Whether the machine that builds actually has libbacktrace: the sandboxes and guests this repo creates carry it, where a remote target is someone else's machine (measured on buildbox4: no package, and configure fails without it off). In a variable and not on stdout, a die in a substitution killing only the subshell.
_cfg_use_libbacktrace() { # <kind: container|vm|local|remote> -> _CFG_LIBBACKTRACE
    case "$1" in
        container|vm|local) _CFG_LIBBACKTRACE=ON ;;
        remote)             _CFG_LIBBACKTRACE=OFF ;;
        *) die "config_load: unknown target kind '$1'" ;;
    esac
}

# libc++ over the system libstdc++ for every Linux CMake config. Apart from _CFG_DEFAULT_CMAKE because -D takes the last repeated value, so a host setting its own CMAKE_CXX_FLAGS (buildbox4's -Wno-invalid-constexpr) would drop this one; _config_merge_cxx_flags merges them instead.
_CFG_LIBCXX_CMAKE='-DCMAKE_CXX_FLAGS=-stdlib=libc++ -DCMAKE_EXE_LINKER_FLAGS=-stdlib=libc++ -DCMAKE_SHARED_LINKER_FLAGS=-stdlib=libc++ -DCMAKE_MODULE_LINKER_FLAGS=-stdlib=libc++'

_cfg_libcxx() { # <WK_TARGET_LIBCXX> -> _CFG_LIBCXX. Opt-in: only a machine whose conf says `1` gets it. A container, a guest and this workstation name no conf, and the wkdev SDK image carries no libc++ at all -- an `unset means on` default put `-stdlib=libc++` on every container build and failed CMake's own compiler check. Read rather than compared, so a typo (`WK_TARGET_LIBCXX=yes`) is named instead of silently meaning the default
    case "$1" in
        1)     _CFG_LIBCXX=ON ;;
        0|"")  _CFG_LIBCXX=OFF ;;
        *)     die "WK_TARGET_LIBCXX='$1' in $(target_registry_conf "${WK_TARGET:-<target>}")
    is neither 1 nor 0. It says whether that machine has libc++:
    1 to build with -stdlib=libc++, 0 (or unset) to leave it out." ;;
    esac
}

_CFG_RELWITHDEBINFO="$_CFG_RELWITHDEBINFO -DDEBUG_FISSION=ON"   # stated rather than left to its default: WebKitCommon.cmake computes that before Options${PORT} sets ENABLE_DEVELOPER_MODE, so no WPE or GTK build turns fission on by itself

_cfg_apple_jsc() {   # the Apple port -- no port flag, --jsc-only sending build-jsc down the CMake path -- by the scheme that stops at JavaScriptCore, with Xcode's own toolchain
    CFG_BUILDSYS=xcode
    CFG_SCRIPT=Tools/Scripts/build-jsc
    CFG_CC=""; CFG_CXX=""
}

config_load() { # <name> <os: linux|macos> [kind: container|vm|local|remote]
    CFG_NAME="$1"
    CFG_OS="${2:-}"
    [ -n "$CFG_OS" ] || die "config_load '$1': no platform given. It is the
    target's t_os, so load_target has to run first -- on macOS a config does
    not choose its own build system (build/configs.sh)."
    CFG_KIND="${3:-${WK_TARGET_KIND:-}}"
    [ -n "$CFG_KIND" ] || die "config_load '$1': no target kind given. It is
    WK_TARGET_KIND, so load_target has to run first (build/configs.sh)."
    CFG_PORT=""; CFG_TYPE=""; CFG_ARGS=""; CFG_CMAKE=""
    CFG_BUILDSYS=cmake
    CFG_SCRIPT=Tools/Scripts/build-webkit
    CFG_JSC_ONLY=""
    CFG_CC="$WK_CC"; CFG_CXX="$WK_CXX"

    case "$1" in   # the CMake -D flags have no Xcode counterpart: WK_BUILD_CMAKE reaches no xcodebuild, and is simply absent on macOS
        jsc-debug)
            CFG_TYPE=Debug; CFG_JSC_ONLY=1
            CFG_ARGS="--debug"
            if [ "$CFG_OS" = macos ]; then
                _cfg_apple_jsc
            else
                CFG_PORT="--jsc-only"
                CFG_CMAKE="-DENABLE_OFFLINE_ASM_ALT_ENTRY=1"
            fi
            ;;
        jsc-release)
            CFG_TYPE=Release; CFG_JSC_ONLY=1
            CFG_ARGS="--release"
            if [ "$CFG_OS" = macos ]; then
                _cfg_apple_jsc
            else
                CFG_PORT="--jsc-only"
                CFG_CMAKE="$_CFG_RELWITHDEBINFO -DENABLE_OFFLINE_ASM_ALT_ENTRY=0"
            fi
            ;;
        jsc-release-asan)
            CFG_TYPE=Release; CFG_JSC_ONLY=1
            if [ "$CFG_OS" = macos ]; then
                _cfg_apple_jsc
                CFG_ARGS="--release ASAN=YES"   # not --asan: build-jsc has no such flag, and its passthrough hands ASAN=YES to the project Makefile as set-webkit-configuration --asan
            else
                CFG_PORT="--jsc-only"
                CFG_ARGS="--release --asan"
                CFG_CMAKE="$_CFG_RELWITHDEBINFO"
            fi
            ;;
        gtk-debug)
            CFG_PORT="--gtk"; CFG_TYPE=Debug
            CFG_ARGS="--debug"
            ;;
        gtk-release)
            CFG_PORT="--gtk"; CFG_TYPE=Release
            CFG_ARGS="--release"
            CFG_CMAKE="$_CFG_RELWITHDEBINFO"
            ;;
        gtk-release-asan)
            CFG_PORT="--gtk"; CFG_TYPE=Release
            CFG_ARGS="--release --asan"
            CFG_CMAKE="$_CFG_RELWITHDEBINFO"
            ;;
        wpe-release)
            CFG_PORT="--wpe"; CFG_TYPE=Release
            CFG_ARGS="--release"
            CFG_CMAKE="$_CFG_RELWITHDEBINFO -DENABLE_WPE_PLATFORM=ON"
            ;;
        mac-debug)   # no port flag: build-webkit defaults to Apple Cocoa on Darwin, and empty CFG_PORT is the port selection
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

    if [ "$CFG_BUILDSYS" = xcode ] && [ "$CFG_OS" != macos ]; then
        die "'$1' is an Apple-port config and builds with Xcode, which this
    workspace has no way to run (it is $CFG_OS). The configs that build here
    are the CMake ports:  wk build --list"
    fi

    if [ "$CFG_BUILDSYS" = cmake ]; then
        CFG_ARGS="$_CFG_DEFAULT_ARGS${CFG_ARGS:+ $CFG_ARGS}"
        _cfg_use_libbacktrace "$CFG_KIND"
        _cfg_libcxx "${WK_TARGET_LIBCXX:-}"
        local _cfg_def="$_CFG_DEFAULT_CMAKE -DUSE_LIBBACKTRACE=$_CFG_LIBBACKTRACE"
        [ "$_CFG_LIBCXX" = OFF ] || _cfg_def="$_cfg_def $_CFG_LIBCXX_CMAKE"
        CFG_CMAKE="$_cfg_def${CFG_CMAKE:+ $CFG_CMAKE}"
    fi

    [ -n "${WK_MB_PER_JOB_EXPLICIT:-}" ] || WK_MB_PER_JOB=$(config_mb_per_job)   # decided here so the job count, the watchdog's budget and the value carried into the target agree; an explicit WK_MB_PER_JOB wins
    return 0
}

config_mb_per_job() {   # 1536 MB fits the CMake ports, WebKit TUs peaking near 1-1.5 GB; the Apple build's peak is one step, the big link, wanting ~17 GB whatever the job count
    case "$CFG_BUILDSYS" in
        xcode) echo 3072 ;;
        *)     echo 1536 ;;
    esac
}

config_target_var() { # <variable stem> -- a machine's flags for *one* config: targets/hosts/<name>.conf may set WK_TARGET_CMAKE_<config> or WK_BUILD_ARGS_<config>, the config's dashes as underscores. ${!name} rather than eval, bash 3.2 having indirect expansion
    local n="$1_$(printf '%s' "${CFG_NAME:-}" | tr -- - _)"
    printf '%s' "${!n:-}"
}

_config_requote() { # <one -D flag, quotes already stripped> -- re-adds the quotes around a -D value with spaces, which the split below strips
    case "$1" in
        *' '*) printf '%s="%s"' "${1%%=*}" "${1#*=}" ;;
        *)     printf '%s' "$1" ;;
    esac
}

_config_merge_cxx_flags() { # <cmake flags string> -- every -DCMAKE_CXX_FLAGS= (the libc++ default plus a target's own) collapses into one, kept last: a host stating its own is stating what to *add*
    local a cxx="" out=""   # strings and not arrays: bash 3.2 errors on "${arr[@]}" for an empty array under `set -u`, and this runs on the host, which may be a macOS workstation
    eval "set -- $1"
    for a in "$@"; do
        case "$a" in
            -DCMAKE_CXX_FLAGS=*) cxx="$cxx${cxx:+ }${a#-DCMAKE_CXX_FLAGS=}" ;;
            *) out="$out${out:+ }$(_config_requote "$a")" ;;
        esac
    done
    [ -z "$cxx" ] || out="$out${out:+ }-DCMAKE_CXX_FLAGS=\"$cxx\""
    printf '%s' "$out"
}

config_build_env() {   # assembled into CFG_ENV, config_load first; the architecture is passed in rather than read here, the golden-base prebuild using this and having no workspace
    local src="$1" jobs="$2" nice="$3" arch="${4:-native}"

    local out=""   # Xcode's default DerivedData is one directory per *user*, shared by every workspace on the macOS remote target, so the paths below derive from $src
    if [ "$CFG_BUILDSYS" = xcode ]; then
        out=$(config_build_dir "$src")
    fi

    local cmakeargs="$CFG_CMAKE"   # appended narrowest last: architecture, machine conf, that machine's flags for *this* config, then `wk build --cmake` as WK_EXTRA_CMAKE
    local archcmake; archcmake=$(arch_cmake "$arch" "$CFG_PORT")
    [ -n "$archcmake" ] && cmakeargs="$cmakeargs $archcmake"
    [ -n "${WK_TARGET_CMAKE:-}" ] && cmakeargs="$cmakeargs $WK_TARGET_CMAKE"
    local cfgcmake; cfgcmake=$(config_target_var WK_TARGET_CMAKE)
    [ -n "$cfgcmake" ] && cmakeargs="$cmakeargs $cfgcmake"
    local cfgargs; cfgargs=$(config_target_var WK_BUILD_ARGS)
    [ -n "${WK_EXTRA_CMAKE:-}" ] && cmakeargs="$cmakeargs $WK_EXTRA_CMAKE"
    cmakeargs="$(_config_merge_cxx_flags "$cmakeargs")"

    CFG_ENV=(   # WK_CCACHE_DIR is set by cmd/build (t_ccache_dir) before this runs, the /ccache default mattering only to a caller that skips that step (targets/vm.sh's base-image prebuild); the sloppiness desensitises __TIMESTAMP__ and BuildRevision.h
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
        "WK_BUILD_SCRIPT=$CFG_SCRIPT"
        "WK_BUILD_ARGS=$CFG_PORT $CFG_ARGS${WK_TARGET_BUILD_ARGS:+ $WK_TARGET_BUILD_ARGS}${cfgargs:+ $cfgargs}"
        "WK_BUILD_CMAKE=$cmakeargs"
        "WK_BUILD_DIR=$(config_build_dir "$src")"
        "WK_MB_PER_JOB=$WK_MB_PER_JOB"
    )
    [ -n "$CFG_CC" ] && CFG_ENV+=("CC=$CFG_CC" "CXX=$CFG_CXX")   # `env CC= ` is not the same as not setting CC, and the Apple configs want it unset

    [ "$CFG_PORT" = --jsc-only ] && CFG_ENV+=("WK_USE_CCACHE=YES")   # WebKitCCache.cmake reads this to wire ccache in, so leaving it off is one `--no-use-ccache` away from every JSC build going cold with no message

    if ! arch_is_native "$arch"; then   # part of every ccache hash: empty ones would invalidate what native builds share
        CFG_ENV+=(
            "WK_ARCH=$arch"
            "WK_ARCH_WRAPPER=$(arch_wrapper "$arch")"
            "WK_ARCH_CFLAGS=$(arch_cflags "$arch")"
            "WK_ARCH_LDFLAGS=$(arch_ldflags "$arch")"
        )
    fi
    if [ -n "$out" ]; then   # Xcode only: WEBKIT_OUTPUTDIR on a CMake port collapses every per-port layout. Separate variables rather than WK_BUILD_ARGS, which is word-split
        CFG_ENV+=("WEBKIT_OUTPUTDIR=$out" "WK_DERIVED_DATA=$src/WebKitBuild/DerivedData")
    fi
    # Carried through only when set, empty not being unset for build-in-target.sh: WK_MEM_BUDGET_MB/WK_MEM_FLOOR_MB come from --mem-budget/--mem-floor, and WK_NO_COMPILATION_CACHE and WK_NO_COMPILE_COMMANDS opt out of those two.
    [ -n "${WK_MEM_BUDGET_MB:-}" ] && CFG_ENV+=("WK_MEM_BUDGET_MB=$WK_MEM_BUDGET_MB")
    [ -n "${WK_MEM_FLOOR_MB:-}" ]  && CFG_ENV+=("WK_MEM_FLOOR_MB=$WK_MEM_FLOOR_MB")
    [ -n "${WK_MEM_INTERVAL:-}" ]  && CFG_ENV+=("WK_MEM_INTERVAL=$WK_MEM_INTERVAL")

    [ -n "${WK_NO_COMPILE_COMMANDS:-}" ] && CFG_ENV+=("WK_NO_COMPILE_COMMANDS=1")
    [ -n "${WK_NO_COMPILATION_CACHE:-}" ] && CFG_ENV+=("WK_NO_COMPILATION_CACHE=1")

    if [ -n "${WK_EXTRA_ENV:-}" ]; then   # WK_EXTRA_ENV is --env from the command line, over the config's own: `wk build ... --env CC=gcc-14` goes last because `env` applies left to right, newline-separated because a value may contain a space (CFLAGS)
        while IFS= read -r _e; do
            [ -n "$_e" ] || continue
            CFG_ENV+=("$_e")
        done <<EOF
$WK_EXTRA_ENV
EOF
    fi
    return 0
}


config_jsc_path() {
    local d; d=$(config_build_dir "$1")
    case "$CFG_BUILDSYS" in
        xcode) echo "$d/jsc" ;;
        *)     echo "$d/bin/jsc" ;;
    esac
}

config_run_var() {   # the loader variable to run a binary out of the build tree; the caller must PREPEND and never replace, wkdev already serving libwpe through LD_LIBRARY_PATH
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

config_browser_path() {   # MiniBrowser is a binary on the CMake ports and an app bundle on the Apple ports, where this names the binary inside it so the browser stays a child of the launching shell; its page is `--url <URL>` (AppDelegate.m:47,265)
    local d; d=$(config_build_dir "$1")
    case "$CFG_BUILDSYS" in
        xcode) echo "$d/MiniBrowser.app/Contents/MacOS/MiniBrowser" ;;
        *)     echo "$d/bin/MiniBrowser" ;;
    esac
}

config_browser_url_flag() {
    case "$CFG_BUILDSYS" in
        xcode) echo "--url" ;;
        *)     echo "" ;;
    esac
}

config_browser_env() {   # what a WebKit app needs to find its own frameworks, Apple only; the __XPC_ duplicates are load-bearing, launchd turning __XPC_FOO into FOO for the XPC children, which would otherwise run against the system WebKit
    local d; d=$(config_build_dir "$1")
    case "$CFG_BUILDSYS" in
        xcode) echo "DYLD_FRAMEWORK_PATH=$d DYLD_LIBRARY_PATH=$d __XPC_DYLD_FRAMEWORK_PATH=$d __XPC_DYLD_LIBRARY_PATH=$d" ;;
        *)     echo "" ;;
    esac
}

config_jsc_only() { [ -n "${CFG_JSC_ONLY:-}" ]; }   # a JSC-only config builds no browser and no test runner, so the names below answer nothing rather than naming an unbuilt binary

config_web_process_name() {   # what the web process is called, for `lldb --attach-name ... --waitfor`: a wrong name here does not fail, it *waits*, reading as a hung debugger
    config_jsc_only && { echo ""; return 0; }
    case "$CFG_BUILDSYS:$CFG_PORT" in
        xcode:*)     echo "com.apple.WebKit.WebContent.Development" ;;
        cmake:--wpe) echo "WPEWebProcess" ;;
        cmake:--gtk) echo "WebKitWebProcess" ;;
        *)           echo "" ;;
    esac
}

config_network_process_name() {   # same use, and CMake ports only (Source/WebKit/Platform{WPE,GTK}.cmake:17-18)
    config_jsc_only && { echo ""; return 0; }
    case "$CFG_BUILDSYS:$CFG_PORT" in
        cmake:--wpe) echo "WPENetworkProcess" ;;
        cmake:--gtk) echo "WebKitNetworkProcess" ;;
        *)           echo "" ;;
    esac
}

config_gpu_process_name() {
    config_jsc_only && { echo ""; return 0; }
    case "$CFG_BUILDSYS:$CFG_PORT" in
        cmake:--wpe) echo "WPEGPUProcess" ;;
        cmake:--gtk) echo "WebKitGPUProcess" ;;
        *)           echo "" ;;
    esac
}

config_test_runner_name() {   # the layout-test driver, for the same attach: its stdin/stdout are the test protocol, which `--wrapper 'lldb'` would otherwise interrupt
    config_jsc_only && { echo ""; return 0; }
    case "$CFG_BUILDSYS:$CFG_PORT" in
        xcode:*)                 echo "WebKitTestRunner" ;;
        cmake:--wpe|cmake:--gtk) echo "WebKitTestRunner" ;;
        *)                       echo "" ;;
    esac
}

config_web_process_pause_env() {   # makes the web process sleep after launch so a waiting debugger beats `--waitfor`'s race with dyld start-up; spelled twice, the Apple ports needing __XPC_FOO and the GLib ports the plain name, both requiring ENABLE(DEVELOPER_MODE)
    case "$CFG_BUILDSYS" in
        xcode) echo "__XPC_WEBKIT_PAUSE_WEB_PROCESS_ON_LAUNCH=1" ;;
        cmake) echo "WEBKIT_PAUSE_WEB_PROCESS_ON_LAUNCH=1" ;;
        *)     echo "" ;;
    esac
}
