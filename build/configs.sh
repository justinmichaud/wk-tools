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
#
# clang everywhere, including in an armhf workspace: the arm32 image's
# /usr/local/bin/clang is a native armhf clang targeting
# arm-unknown-linux-gnueabihf, so this needs no per-architecture special case
# and gets none. What the architecture does change is flags, and those live in
# lib/arch.sh -- deliberately not here, because a config names a port and a
# build type and must go on meaning exactly that in every workspace.
WK_CC="${WK_CC:-clang}"
WK_CXX="${WK_CXX:-clang++}"

# The architecture vocabulary, loaded on demand: not every caller of this file
# has sourced it, and the functions are only reached for a non-native build.
command -v arch_canon >/dev/null 2>&1 || . "$WK_ROOT/lib/arch.sh"

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
#
# The `-asan` suffix on the Apple side is ours, not Xcode's. Xcode has no
# directory for a sanitizer: --asan only adds ENABLE_ADDRESS_SANITIZER=YES to
# the xcodebuild command line, and webkitdirs.pm:3132-3133 says so outright --
# "Xcode toggles ASan within Debug/Release, so its path is unchanged". So
# mac-release and mac-release-asan resolved to the *same* Release/ directory,
# and building one after the other left a tree that was half instrumented and
# half not. That is not a build failure; it is a crash a week later in
# something that was linked against both halves.
#
# The suffix only becomes true because config_build_env() exports
# WEBKIT_OUTPUTDIR to exactly this path -- a name here that the build does not
# honour would be the same quiet lie in the other direction. It is derived from
# CFG_ARGS rather than from a separate flag because the --asan in CFG_ARGS *is*
# what changes the products; a second field could drift out of step with it.
# CMake is left alone: those ports encode the sanitizer in CMAKE_BUILD_TYPE and
# their directory names are upstream's, not ours, to choose.
# What a config contributes to --cmakeargs, for an error message that has to
# name what would have been lost.
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

# What "release, with debug info" means on the CMake ports, in one place because
# every release config needs the same three flags and two of them are not
# obvious.
#
# The build type is RelWithDebInfo for what WebKit hangs off that *name*, not
# for CMake's idea of it:
#
#   DEBUG_FISSION            -gsplit-dwarf: the debug info lands in .dwo files
#                            beside the objects instead of going through the
#                            linker, which is what makes "release with -g"
#                            affordable on this codebase.
#   GCC_OFFLINEASM_SOURCE_MAP  line information for the offlineasm-generated
#                            LLInt, same file (:200). A JSC stack that passes
#                            through the interpreter is otherwise raw addresses.
#
# The optimization flags are pinned, and that is the load-bearing half. CMake's
# own RelWithDebInfo is `-O2 -g -DNDEBUG` where Release is `-O3 -DNDEBUG` --
# read out of a configured cache, not assumed -- so adopting the build type
# alone would drop every release build by an optimization level. This repo
# exists to produce benchmark numbers; a silent -O3 -> -O2 is the worst
# available way to change them. Release goes on meaning -O3, and gains -g.
#
# The two -asan configs already said RelWithDebInfo and so were already building
# at -O2 by accident. They use this too, which moves them to -O3: an accidental
# value replaced by a stated one, and a sanitizer build that now optimizes like
# the release build whose crash it is reproducing.
#
# The quotes survive: build-webkit runs `system("cmake @args")`, joined and
# parsed by a shell (webkitdirs.pm:2966), so a -D value with spaces in it
# reaches CMake as one argument.
_CFG_RELWITHDEBINFO='-DCMAKE_BUILD_TYPE=RelWithDebInfo'
_CFG_RELWITHDEBINFO="$_CFG_RELWITHDEBINFO -DCMAKE_C_FLAGS_RELWITHDEBINFO=\"-O3 -g -DNDEBUG\""
_CFG_RELWITHDEBINFO="$_CFG_RELWITHDEBINFO -DCMAKE_CXX_FLAGS_RELWITHDEBINFO=\"-O3 -g -DNDEBUG\""

# DEBUG_FISSION is stated rather than left to its default, because its default
# cannot fire on these ports. OptionsCommon.cmake:175 gates
# ENABLE_DEBUG_FISSION_DEFAULT on ENABLE_DEVELOPER_MODE -- but
# WebKitCommon.cmake:338 includes OptionsCommon *before* Options${PORT}, and
# ENABLE_DEVELOPER_MODE is what Options${PORT} sets (OptionsWPE.cmake:46,
# OptionsGTK.cmake:77). So at the moment the test runs the variable is not yet
# defined, the default is computed as OFF, and no WPE or GTK build has ever
# turned fission on by itself. Measured: DEBUG_FISSION:BOOL=OFF and zero .dwo
# files in a RelWithDebInfo tree that had asked for nothing else.
#
# `option()` honours a value already in the cache, so naming it here is enough
# and nothing upstream has to change.
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

    # How much memory a job of *this* build system is worth, decided here so
    # that everything downstream agrees: the job count (build_jobs, which reads
    # WK_MB_PER_JOB), the memory budget the watchdog enforces (jobs x this),
    # and the value carried into the target. Setting it only in the environment
    # handed to the far side left the job count derived from the CMake figure,
    # which is how a mac-release came to be built with -j9 and killed at
    # 16.6 GB. An explicit WK_MB_PER_JOB from the caller still wins.
    [ -n "${WK_MB_PER_JOB_EXPLICIT:-}" ] || WK_MB_PER_JOB=$(config_mb_per_job)
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
#
# The architecture is the workspace's, from t_arch, and is passed in rather
# than read here: this function is also what the golden-base prebuild uses, and
# a build environment that went looking for the current workspace would be
# assembling one thing while describing another.
# How much memory one compile job is worth, which is a property of the build
# system and not of the machine.
#
# 1536 MB is right for the CMake ports and was measured there (lib/resources.sh
# explains it): typical WebKit translation units peak near 1-1.5 GB. The Apple
# build is a different shape -- unified sources, a content-addressed compilation
# cache, and Xcode scheduling work inside each job -- and the same number is an
# underestimate that shows up as the memory watchdog killing a healthy build.
#
# Measured 2026-08-20, in the golden base: `-j9` derived from 13824 MB at
# 1536 MB/job peaked at **16593 MB** and was killed at 95% of the way through a
# mac-release. At 3072 the same guest derived `-j6` and an 18432 MB budget, and
# the build finished at a peak of **16783 MB**.
#
# Read those two together, because they say something the fix alone does not:
# the peak barely moved with a third fewer jobs, so it is dominated by one step
# -- the big link -- rather than by parallelism. What was actually wrong was
# the *budget*; the job count mostly buys wall-clock. An Apple build of WebKit
# on this machine wants about 17 GB whatever it is told to do, and a budget
# derived from the CMake figure is below that for any plausible job count.
#
# `claude/CLAUDE.md` already told agents to pass `WK_MB_PER_JOB=3072` by hand
# for exactly this; a default that needs a manual override on one of the two
# build systems is a wrong default.
config_mb_per_job() {
    case "$CFG_BUILDSYS" in
        xcode) echo 3072 ;;
        *)     echo 1536 ;;
    esac
}

config_build_env() {
    local src="$1" jobs="$2" nice="$3" arch="${4:-native}"

    # --- Apple ports: pin where the output and the caches go -----------------
    # Nothing here used to be set at all, and the defaults are worse than they
    # look. Xcode's own default puts the content-addressed compilation cache
    # and the module cache in ~/Library/Developer/Xcode/DerivedData -- one
    # machine-wide directory per *user*, not per checkout, per config or per
    # workspace. Measured in a guest after one mac-release: 9.6 GB of
    # CompilationCache.noindex and 1.6 GB of ModuleCache.noindex there, while
    # the products sat in the checkout's WebKitBuild. On the macOS remote
    # target that single directory would be shared by every workspace and every
    # other person on the box.
    #
    # Both paths are derived from $src, which is the target's own checkout
    # path, so they are per workspace by construction and they stay on the
    # guest's APFS volume rather than on a virtiofs --dir share, where a CAS
    # would cost more than it saves. Deriving them from a fixed path also keeps
    # them stable across `wk vm base --refresh`: provisioning does a `git reset
    # --hard`, which does not touch untracked WebKitBuild, so a refreshed base
    # still has its warm cache.
    #
    # WEBKIT_OUTPUTDIR is honoured at webkitdirs.pm:399 and becomes
    # SYMROOT/OBJROOT at :460, which is what finally gives mac-release-asan a
    # tree of its own, gives each config its own XCBuildData/build.db
    # (webkitdirs.pm:3149: "build.db is shared across Xcode configurations"),
    # and -- as a side effect worth knowing about -- makes webkitdirs skip the
    # whole IDEBuildLocationStyle block at :401-441, so a stray Xcode user
    # default can no longer silently relocate the output from under us.
    #
    # One DerivedData for all configs, deliberately: the CAS is
    # content-addressed and the module cache is keyed by build settings, so
    # sharing them across configs is the point of having them. Only the things
    # that are *not* content-addressed -- products, intermediates, build.db,
    # precompiled headers -- have to be separated per config.
    local out=""
    if [ "$CFG_BUILDSYS" = xcode ]; then
        out=$(config_build_dir "$src")
    fi

    # CMake flags that come from the architecture rather than from the config,
    # appended so a config's own flags still win where they overlap.
    local cmakeargs="$CFG_CMAKE"
    local archcmake; archcmake=$(arch_cmake "$arch" "$CFG_PORT")
    [ -n "$archcmake" ] && cmakeargs="$cmakeargs $archcmake"

    # The machine's own defaults, from its target conf (WK_TARGET_CMAKE).
    # Some flags belong to a machine rather than to a configuration -- a
    # library that distribution does not have, a toolchain quirk -- and
    # retyping them on every build is how they end up forgotten on the one
    # build that mattered.
    [ -n "${WK_TARGET_CMAKE:-}" ] && cmakeargs="$cmakeargs $WK_TARGET_CMAKE"

    # `wk build --cmake ...`, last of all: cmake takes the last value for a
    # repeated -D, so a flag typed on the command line overrides the same flag
    # from the config, the architecture or the machine rather than fighting it.
    [ -n "${WK_EXTRA_CMAKE:-}" ] && cmakeargs="$cmakeargs $WK_EXTRA_CMAKE"

    # /ccache is the container's bind-mounted store cache and the default for
    # everything that has one; a target on another machine sets WK_CCACHE_DIR
    # to a directory that exists over there. See t_ccache_dir.
    CFG_ENV=(
        "CCACHE_DIR=${WK_CCACHE_DIR:-/ccache}"
        "NUMBER_OF_PROCESSORS=$jobs"
        "CMAKE_BUILD_PARALLEL_LEVEL=$jobs"
        "WK_JOBS=$jobs"
        "WK_NICE=$nice"
        "WK_SRC=$src"
        "WK_BUILDSYS=$CFG_BUILDSYS"
        "WK_BUILD_ARGS=$CFG_PORT $CFG_ARGS"
        "WK_BUILD_CMAKE=$cmakeargs"
        # Where this config's tree is, so the far side can tell whether the one
        # already there was configured with these flags. Derived here rather
        # than re-derived over there, for the reason every other path in this
        # file is: four ports lay their trees out four ways.
        "WK_BUILD_DIR=$(config_build_dir "$src")"
        "WK_MB_PER_JOB=$WK_MB_PER_JOB"
    )
    # Only when the config asks for it: the Apple configs leave these empty on
    # purpose, and `env CC= ` is not the same as not setting CC at all.
    [ -n "$CFG_CC" ] && CFG_ENV+=("CC=$CFG_CC" "CXX=$CFG_CXX")

    # Only for a non-native workspace, so a native build's environment is
    # exactly what it was before --arch existed. That is not tidiness: these
    # variables are part of every ccache hash, and adding empty ones would
    # invalidate a shared cache that several workspaces are already using.
    if ! arch_is_native "$arch"; then
        CFG_ENV+=(
            "WK_ARCH=$arch"
            "WK_ARCH_WRAPPER=$(arch_wrapper "$arch")"
            "WK_ARCH_CFLAGS=$(arch_cflags "$arch")"
            "WK_ARCH_LDFLAGS=$(arch_ldflags "$arch")"
        )
    fi
    # Xcode only. Setting WEBKIT_OUTPUTDIR on a CMake port would change every
    # one of those layouts at once -- usesPerConfigurationBuildDirectory()
    # (webkitdirs.pm:1182-1184) keys off nothing but this variable being
    # defined, and JSCOnly/Release would flatten to the bare value.
    #
    # These are paths, not flags: they stay separate variables rather than
    # riding in WK_BUILD_ARGS, which is word-split by the build half and would
    # come apart on a checkout path containing a space. build-in-target.sh
    # turns them into the xcodebuild settings they have to become.
    if [ -n "$out" ]; then
        CFG_ENV+=("WEBKIT_OUTPUTDIR=$out" "WK_DERIVED_DATA=$src/WebKitBuild/DerivedData")
    fi
    # compile_commands.json is on by default; this only carries the opt-out
    # through to the build half, which runs in the target and cannot see the
    # caller's environment.
    # The memory watchdog's knobs, carried into the target only when set: the
    # build half runs over there and cannot see this caller's environment, and
    # an empty value is not the same as an unset one for a script that has its
    # own defaults.
    [ -n "${WK_MEM_BUDGET_MB:-}" ] && CFG_ENV+=("WK_MEM_BUDGET_MB=$WK_MEM_BUDGET_MB")
    [ -n "${WK_MEM_FLOOR_MB:-}" ]  && CFG_ENV+=("WK_MEM_FLOOR_MB=$WK_MEM_FLOOR_MB")
    [ -n "${WK_MEM_INTERVAL:-}" ]  && CFG_ENV+=("WK_MEM_INTERVAL=$WK_MEM_INTERVAL")

    [ -n "${WK_NO_COMPILE_COMMANDS:-}" ] && CFG_ENV+=("WK_NO_COMPILE_COMMANDS=1")
    # Same shape, for the CAS: build-in-target.sh explains what it buys and what
    # it costs. Carried through here because the build half runs in the target
    # and cannot see the caller's environment.
    [ -n "${WK_NO_COMPILATION_CACHE:-}" ] && CFG_ENV+=("WK_NO_COMPILATION_CACHE=1")

    # Last, and that is the whole mechanism: `wk build … --env CC=gcc-14`.
    #
    # `env` applies its assignments left to right, so an assignment added after
    # the config's own replaces it -- which makes this an override and not just
    # an addition, and is the difference between this and WK_EXTRA_CMAKE (cmake
    # takes the last -D, which is the same rule arrived at from the other end).
    #
    # Newline-separated rather than space-separated, because an environment
    # value may legitimately contain a space (CFLAGS is the obvious one) and
    # this must not be the place that comes apart on it.
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

# --- the browser, and the processes it spawns --------------------------------
# MiniBrowser is one binary on the CMake ports and an app bundle on the Apple
# ports, and the difference is not only the path. Two things were measured in
# the guest and neither is guessable:
#
#   `open -a` on the bundle is refused with LaunchServices -10825 -- the app is
#   built against the 26.5 SDK and the guest runs 26.4. Exec'ing the binary
#   inside the bundle skips that check, and keeps the browser a child of the
#   shell that launched it, so ctrl-c and a debugger both reach it.
#
#   The Apple MiniBrowser takes its page as `--url <URL>`
#   (Tools/MiniBrowser/mac/AppDelegate.m:47,265). A bare positional URL is
#   parsed by nothing and silently ignored: the window comes up on about:blank,
#   which reads as a browser that cannot load anything.

config_browser_path() {
    local d; d=$(config_build_dir "$1")
    case "$CFG_BUILDSYS" in
        xcode) echo "$d/MiniBrowser.app/Contents/MacOS/MiniBrowser" ;;
        *)     echo "$d/bin/MiniBrowser" ;;
    esac
}

# How the page is named on the command line, for the same reason: one port
# takes a positional URL and the other ignores it.
config_browser_url_flag() {
    case "$CFG_BUILDSYS" in
        xcode) echo "--url" ;;
        *)     echo "" ;;
    esac
}

# The environment a WebKit app needs to find its own frameworks in the build
# tree. Apple only -- the CMake ports go through Tools/Scripts/run-minibrowser,
# which sets up their own.
#
# The __XPC_ duplicates are the load-bearing half. WebKit's child processes --
# WebContent, GPU, Networking -- are XPC services launched by launchd rather
# than by the browser, so they inherit nothing from this shell; launchd passes
# a variable named __XPC_FOO on to the service as FOO. Without them the UI
# process runs out of the build tree while its children run against the system
# WebKit, which is not a configuration anyone means to debug.
config_browser_env() {
    local d; d=$(config_build_dir "$1")
    case "$CFG_BUILDSYS" in
        xcode) echo "DYLD_FRAMEWORK_PATH=$d DYLD_LIBRARY_PATH=$d __XPC_DYLD_FRAMEWORK_PATH=$d __XPC_DYLD_LIBRARY_PATH=$d" ;;
        *)     echo "" ;;
    esac
}

# What the web process is called, for `lldb --attach-name ... --waitfor`. It is
# the process a page or a layout test actually runs in, and the one worth
# attaching to -- the UI process spends its life in a run loop.
#
# One name per port, and none of them is guessed: each is the CMake target's own
# output name, from the port file that sets it -- PlatformWPE.cmake:16 gives
# WebProcess_OUTPUT_NAME WPEWebProcess, PlatformGTK.cmake:11 gives
# WebKitWebProcess. A wrong name here does not fail, it *waits*: `--waitfor`
# sits there for a process that will never be launched, which reads as a hung
# debugger rather than as a typo.
#
# Not the GLib prgname. WebProcessMainWPE.cpp:82 calls g_set_prgname(
# "WPEWebProcess") for GStreamer's benefit, which is the same string by
# coincidence of naming and would still be the wrong thing to read: it is set
# from inside the process, after launch, and the debugger matches on what
# exec'd.
#
# jsc-only builds no web process and keeps the empty string, which is what the
# callers refuse on.
config_web_process_name() {
    case "$CFG_BUILDSYS:$CFG_PORT" in
        xcode:*)     echo "com.apple.WebKit.WebContent.Development" ;;
        cmake:--wpe) echo "WPEWebProcess" ;;
        cmake:--gtk) echo "WebKitWebProcess" ;;
        *)           echo "" ;;
    esac
}

# The layout-test driver, for the same `--attach-name ... --waitfor`. Attaching
# is what makes the UI process debuggable at all here: its stdin and stdout are
# the test protocol, so `--wrapper 'lldb'` would put a debugger's prompt in the
# middle of a pipe webkitpy is talking on. An attach touches no file descriptor.
#
# The CMake ports build it under the same name -- it is the one binary in
# WebKitBuild/<Port>/<Type>/bin that both build systems agree on -- so the case
# is spelled out per port rather than defaulted, to keep jsc-only, which has no
# test runner, on the empty string the callers refuse.
config_test_runner_name() {
    case "$CFG_BUILDSYS:$CFG_PORT" in
        xcode:*)                 echo "WebKitTestRunner" ;;
        cmake:--wpe|cmake:--gtk) echo "WebKitTestRunner" ;;
        *)                       echo "" ;;
    esac
}

# An environment assignment that makes the web process sleep immediately after
# launch, so a waiting debugger attaches before it does anything: 5 s on the
# Apple ports (WebProcessCocoa.mm:655), 30 s on the GLib ones
# (WebProcessMainWPE.cpp:74, WebProcessMainGtk.cpp:75).
#
# Belt and braces. `--waitfor` on its own was measured attaching during dyld
# start-up, well before main; the pause is what keeps that from being a race a
# faster machine could win.
#
# One variable, spelled twice. The Apple ports need the __XPC_ doubling because
# the web process is an XPC service launchd starts, and launchd is what turns
# __XPC_FOO into FOO for it; the GLib ports fork their children from the UI
# process, so the plain name is simply inherited and the doubled one would be
# read by nobody.
#
# Both readers sit inside `#if ENABLE(DEVELOPER_MODE)`. Every CMake config in
# this file passes -DDEVELOPER_MODE=ON, so this holds for all of them -- but a
# build without it ignores the variable silently, and the attach goes back to
# being the race this exists to remove.
config_web_process_pause_env() {
    case "$CFG_BUILDSYS" in
        xcode) echo "__XPC_WEBKIT_PAUSE_WEB_PROCESS_ON_LAUNCH=1" ;;
        cmake) echo "WEBKIT_PAUSE_WEB_PROCESS_ON_LAUNCH=1" ;;
        *)     echo "" ;;
    esac
}
