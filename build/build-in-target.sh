#!/usr/bin/env bash
#
# Runs inside a workspace, invoked by `wk build`. Kept separate from cmd/build
# because that half runs outside the target and decides policy, while this half
# runs inside and only carries it out.
#
# Environment supplied by cmd/build: WK_JOBS, WK_NICE, WK_BUILD_ARGS,
# WK_BUILD_CMAKE, WK_BUILDSYS, WK_SRC, plus the ccache and cross-build caches.
# A non-native workspace adds WK_ARCH and the WK_ARCH_* flags; see lib/arch.sh
# and the architecture block below.
# The Apple configs add WEBKIT_OUTPUTDIR and WK_DERIVED_DATA, which the xcode
# case below turns into build settings; both are absent for the CMake ports.
#
# This runs in two very different places -- a Fedora container and a macOS
# guest -- so two constraints apply throughout:
#
#   bash 3.2. macOS still ships it, and it errors on "${arr[@]}" for an empty
#   array under `set -u`. Lists are therefore built as strings and split
#   deliberately, exactly as lib/resources.sh does.
#
#   No Linux-only interface may be assumed present. cgroups, ionice and choom
#   all exist on one side and not the other, so every use is guarded.

set -euo pipefail

SRC=${WK_SRC:-/src/WebKit}
cd "$SRC"

jobs=${WK_JOBS:-4}
buildsys=${WK_BUILDSYS:-cmake}

# Authoritative clamp: this half runs *inside* the target, so its own cgroup
# limit is the real ceiling. Belt and braces against the caller having sized the
# build from the wrong machine's memory. There is no cgroup on macOS, and no
# equivalent either -- the guest's memory is fixed at boot by `tart set`, and
# cmd/build sizes from that, so nothing is lost by skipping this there.
if [ -r /sys/fs/cgroup/memory.max ]; then
    limit=$(cat /sys/fs/cgroup/memory.max)
    if [ "$limit" != max ]; then
        max_jobs=$(( limit / 1024 / 1024 / ${WK_MB_PER_JOB:-4096} ))
        [ "$max_jobs" -lt 1 ] && max_jobs=1
        if [ "$jobs" -gt "$max_jobs" ]; then
            echo "clamping -j$jobs -> -j$max_jobs (cgroup limit $(( limit / 1024 / 1024 ))MB)" >&2
            jobs=$max_jobs
        fi
    fi
fi
nicelevel=${WK_NICE:-10}

cmakeargs=${WK_BUILD_CMAKE:-}

# --- architecture ------------------------------------------------------------
# Empty for a native workspace, which is every workspace on macOS and the
# default everywhere, so this whole block is inert there.
#
# The flags are prepended to whatever is already set rather than replacing it,
# for the same reason LD_LIBRARY_PATH is prepended in cmd/run: the image puts
# things there and a build that overwrites them fails to find libraries that
# were on the path all along.
#
# CMake reads CFLAGS/CXXFLAGS/LDFLAGS at *configure* time and caches them, so
# these take effect on a fresh build directory and are ignored by an incremental
# build of a tree configured without them. That is the same behaviour
# webkitdirs.pm has for its own --32-bit path, and the reason an architecture is
# fixed when a workspace is created rather than chosen per build.
arch=${WK_ARCH:-native}
if [ "$arch" != native ]; then
    export CFLAGS="${WK_ARCH_CFLAGS:-} ${CFLAGS:-}"
    export CXXFLAGS="${WK_ARCH_CFLAGS:-} ${CXXFLAGS:-}"
    export LDFLAGS="${WK_ARCH_LDFLAGS:-} ${LDFLAGS:-}"
fi

# The personality wrapper, and it is load-bearing rather than cosmetic. The
# kernel is the host's, so `uname -m` in an armhf container answers aarch64;
# CMake takes CMAKE_SYSTEM_PROCESSOR from it and WebKitCommon.cmake:167 turns
# aarch64 into WTF_CPU_ARM64. Without linux32 a 32-bit compiler therefore
# builds a tree configured for 64-bit ARM, and the first thing to notice is the
# assembler, thousands of files in. Outermost of the wrappers because the
# personality is inherited: cmake, ninja and every compiler underneath need it,
# not just build-webkit.
wrapper=${WK_ARCH_WRAPPER:-}

# Arrays, not a flat string: --cmakeargs carries several -D flags as ONE
# argument, and any unquoted expansion splits it apart -- build-webkit then
# sees the first -D as the whole value and the rest as unknown options.
#
# ${a[@]+"${a[@]}"} is the bash 3.2 workaround for an empty array under
# `set -u`, which is an error there and not on the Linux side. Both halves have
# to keep working, so the idiom stays even where the array cannot be empty.
args=()
# shellcheck disable=SC2206 -- deliberate word splitting of the config string.
args+=(${WK_BUILD_ARGS:-})

# compile_commands.json, always. Every build produces it, on every port and
# every target, because a checkout without it is a checkout where clangd, Zed
# and every editor-driven jump-to-definition quietly stops working -- and the
# person who needs it is never the person running the build.
#
# It is free on the CMake ports (one -D flag, CMake writes the file as a side
# effect). On the Apple ports it is not: it expands to four build settings, one
# of which is GCC_PRECOMPILE_PREFIX_HEADER=NO, and WebCore, WebKit and
# JavaScriptCore all set that to YES in their own xcconfigs and have prefix
# headers every translation unit includes. It also adds -gen-cdb-fragment-path,
# so each of ~6,300 compiles writes an extra JSON fragment.
#
# Measured cold on a 9-vCPU guest: 99 min, which is 8.5 s of CPU per
# translation unit -- far more than a WebKit TU should cost. That is the price
# of the file, it is paid deliberately, and WK_NO_COMPILE_COMMANDS=1 is the
# escape hatch for a one-off build where it does not matter.
[ -n "${WK_NO_COMPILE_COMMANDS:-}" ] || args+=(--export-compile-commands)

# How the job count reaches the compiler differs entirely between the two build
# systems, and neither flag is accepted by the other.
#
#   cmake   --makeargs=-jN, which build-webkit forwards to ninja/make.
#   xcode   -jobs N, which survives build-webkit's pass_through getopt and
#           reaches xcodebuild. --makeargs is silently ignored on this path, so
#           using it would leave xcodebuild at its own core-count default -- a
#           build that looks correct and takes the machine down anyway.
case "$buildsys" in
xcode)
    args+=(-jobs "$jobs")

    # Where the products go. build-webkit hands anything it does not recognise
    # straight to xcodebuild (Getopt::Long is in pass_through mode there, and
    # buildXcodeScheme appends the leftovers at webkitdirs.pm:2420), so a build
    # setting written here reaches the build system unchanged.
    #
    # WEBKIT_OUTPUTDIR on its own is not enough, and the failure would be
    # silent. webkitdirs turns it into SYMROOT/OBJROOT (:460) and Xcode then
    # appends the configuration name to SYMROOT to get the products directory
    # -- while webkitdirs itself reports the products as being in
    # WEBKIT_OUTPUTDIR with no configuration subdirectory at all
    # (:1195-1196). Left alone the two disagree by exactly one level, and every
    # WebKit script that resolves a path for itself -- run-minibrowser,
    # run-webkit-tests, webkit-build-directory -- looks one directory above the
    # frameworks. WK_CONFIGURATION_BUILD_DIR is WebKit's own hook for pinning
    # that directory (Configurations/WebKitProjectPaths.xcconfig:36-41, which
    # every project picks up through CommonBase.xcconfig), so setting it makes
    # the build agree with what webkitdirs already claims -- and with
    # config_build_dir() on this side.
    #
    # SHARED_PRECOMPS_DIR has to be repeated by hand. webkitdirs only passes it
    # when it computed the product directory itself: :461 sits under the
    # `if (!defined($baseProductDir))` branch at :401, which WEBKIT_OUTPUTDIR
    # skips. Without it all four Apple configs would go back to sharing one
    # precompiled-header directory, which is half of what this change is for.
    if [ -n "${WEBKIT_OUTPUTDIR:-}" ]; then
        args+=("WK_CONFIGURATION_BUILD_DIR=$WEBKIT_OUTPUTDIR")
        args+=("SHARED_PRECOMPS_DIR=$WEBKIT_OUTPUTDIR/PrecompiledHeaders")
    fi

    # Where the caches go: the two big ones by name, and NOT -derivedDataPath.
    #
    # The two settings pin what is actually large -- 9.6 GB of compilation CAS
    # and 1.6 GB of module cache, measured after one mac-release -- and they are
    # build settings, so every xcodebuild in the build inherits them.
    #
    # -derivedDataPath is a *flag*, and that is the problem. build-webkit runs
    # xcodebuild twice: the scheme build, and then Tools/Scripts/build-imagediff,
    # which uses buildXCodeProject (webkitdirs.pm:2399) -- `-project`, no
    # `-scheme`. Every argument we pass reaches both, and xcodebuild refuses:
    #
    #   xcodebuild: error: The flag -scheme, -testProductsPath, or -xctestrun
    #   is required when specifying -derivedDataPath.
    #
    # Measured on a full base prebuild: `** BUILD SUCCEEDED ** [5149 sec]`
    # followed immediately by that error, exit 64, and no ImageDiff -- which
    # every pixel and reftest comparison needs. A build that reports failure
    # after succeeding is bad; one that quietly skips a tool the tests need is
    # worse, and the two arrived together.
    #
    # Nothing is lost by dropping it. The products, intermediates and
    # XCBuildData/build.db already follow WEBKIT_OUTPUTDIR through
    # SYMROOT/OBJROOT, indexing is off (COMPILER_INDEX_STORE_ENABLE=NO), and
    # the caches are named above -- so the machine-wide DerivedData root is left
    # holding nothing that matters.
    if [ -n "${WK_DERIVED_DATA:-}" ]; then
        args+=("COMPILATION_CACHE_CAS_PATH=$WK_DERIVED_DATA/CompilationCache.noindex")
        args+=("MODULE_CACHE_DIR=$WK_DERIVED_DATA/ModuleCache.noindex")
    fi

    # The escape hatch for debugging Swift types, and the reason it exists:
    #
    # With caching on -- WebKit's default, CommonBase.xcconfig:82-83 -- the
    # explicit precompiled modules under WebKitBuild/SwiftExplicitPrecompiledModules
    # record their dependencies as `llvmcas:/<hash>` rather than as paths, so
    # the debug info is only as durable as the CAS. Measured in a guest: the
    # .pcm files were all still on disk, but `llvm-cas --print-kind` reported
    # "unknown object" for the ids inside them, and lldb printed a hundred
    # `llvmcas:/... does not exist` warnings while resolving one breakpoint,
    # ending in "Debugging will be degraded due to missing types". A cache is
    # entitled to evict; debug info pointing into one is the mistake.
    #
    # It costs a full rebuild to switch, because a build with different
    # settings shares nothing with the cached one, so this is deliberately not
    # the default: C++ debugging is unaffected either way -- breakpoints,
    # source lines and WebKit's own lldb summaries all measured working with
    # caching on -- and only Swift-interop types are degraded.
    [ -n "${WK_NO_COMPILATION_CACHE:-}" ] && args+=("COMPILATION_CACHE_ENABLE_CACHING=NO")
    ;;
*)
    [ -n "$cmakeargs" ] && args+=(--cmakeargs="$cmakeargs")
    args+=("--makeargs=-j$jobs")
    ;;
esac

# ionice keeps the build off the desktop's I/O path; choom makes the OOM killer
# choose the build rather than the session. oom_score_adj is inherited, so
# setting it on the wrapper covers every compiler process underneath. Neither
# exists on macOS, where `nice` alone is what there is.
pre=""
command -v ionice >/dev/null 2>&1 && pre="ionice -c3"
command -v choom  >/dev/null 2>&1 && pre="$pre choom -n 500 --"

set -x
# shellcheck disable=SC2086 -- $wrapper and $pre are deliberate lists of bare words.
exec $wrapper $pre nice -n "$nicelevel" \
    Tools/Scripts/build-webkit "${args[@]}" ${@+"$@"}
