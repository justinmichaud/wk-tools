#!/usr/bin/env bash
#
# Runs inside a workspace, invoked by `wk build`. Kept separate from cmd/build
# because that half runs outside the target and decides policy, while this half
# runs inside and only carries it out.
#
# Environment supplied by cmd/build: WK_JOBS, WK_NICE, WK_BUILD_ARGS,
# WK_BUILD_CMAKE, WK_BUILDSYS, WK_SRC, plus the ccache and cross-build caches.
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

    # Where the caches go. -derivedDataPath moves the DerivedData root, which
    # is what the compilation CAS and the module cache hang off; the two
    # explicit settings pin the only two things in it that are large (9.6 GB
    # and 1.6 GB measured after one mac-release), so this does not depend on
    # which of them Xcode chooses to derive from the root in a given release.
    # Compilation caching itself is already on -- WebKit turns it on in
    # Configurations/CommonBase.xcconfig:82-83 -- so the cache exists whether or
    # not anyone decides where it lives.
    if [ -n "${WK_DERIVED_DATA:-}" ]; then
        args+=(-derivedDataPath "$WK_DERIVED_DATA")
        args+=("COMPILATION_CACHE_CAS_PATH=$WK_DERIVED_DATA/CompilationCache.noindex")
        args+=("MODULE_CACHE_DIR=$WK_DERIVED_DATA/ModuleCache.noindex")
    fi
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
# shellcheck disable=SC2086 -- $pre is a deliberate list of bare words.
exec $pre nice -n "$nicelevel" \
    Tools/Scripts/build-webkit "${args[@]}" ${@+"$@"}
