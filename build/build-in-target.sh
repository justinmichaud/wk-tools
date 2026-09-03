#!/usr/bin/env bash
#
# Runs inside a workspace, invoked by `wk build`. Kept separate from cmd/build
# because that half runs outside the target and decides policy, while this half
# runs inside and only carries it out.
#
# Environment supplied by cmd/build: WK_JOBS, WK_NICE, WK_BUILD_ARGS,
# WK_BUILD_CMAKE, WK_BUILDSYS, WK_BUILD_SCRIPT, WK_SRC, WK_BUILD_DIR (the
# config's own build directory, config_build_dir in build/configs.sh),
# WK_ARCH plus WK_ARCH_WRAPPER/WK_ARCH_CFLAGS/WK_ARCH_LDFLAGS for a
# non-native workspace, WK_NO_COMPILATION_CACHE for the Apple ports,
# WEBKIT_OUTPUTDIR/WK_DERIVED_DATA for the Apple configs.
#
# Runs on both a Fedora container and a macOS guest: bash 3.2 (macOS still
# ships it, and "${arr[@]}" for an empty array errors under `set -u`, hence
# lists built as strings), and no Linux-only interface (cgroups, ionice,
# choom) is assumed present.

set -euo pipefail

# This is wk's own build: the build wall (container/bin/wk-build-wall) lets
# ninja/cmake/make through for it and refuses them to an agent's shell.
export WK_BUILD=1

# ${var@Q} needs bash 4.4; this file has to parse under macOS's bash 3.2.
_q() {
    case "$1" in
        *[!A-Za-z0-9._/=:-]*) printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")" ;;
        *) printf '%s' "$1" ;;
    esac
}

SRC=${WK_SRC:-/src/WebKit}
cd "$SRC"

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/guard.sh"
jobs=$(guard_jobs "${WK_JOBS:-4}")
buildsys=${WK_BUILDSYS:-cmake}

# Which Tools/Scripts entry point (build/configs.sh, CFG_SCRIPT). build-jsc for
# the Apple port's JavaScriptCore, build-webkit for everything else.
script=${WK_BUILD_SCRIPT:-Tools/Scripts/build-webkit}

cmakeargs=${WK_BUILD_CMAKE:-}

# --- architecture: empty (inert) for a native workspace ----------------------
# CMake caches CFLAGS/CXXFLAGS/LDFLAGS at *configure* time, so an
# architecture is fixed at workspace creation.
arch=${WK_ARCH:-native}
if [ "$arch" != native ]; then
    export CFLAGS="${WK_ARCH_CFLAGS:-} ${CFLAGS:-}"
    export CXXFLAGS="${WK_ARCH_CFLAGS:-} ${CXXFLAGS:-}"
    export LDFLAGS="${WK_ARCH_LDFLAGS:-} ${LDFLAGS:-}"
fi

# `uname -m` in an armhf container answers aarch64 (host kernel) without
# linux32, building a 64-bit-ARM tree.
wrapper=${WK_ARCH_WRAPPER:-}

# Array, not a string: --cmakeargs is several -D flags as ONE argument.
args=()
# shellcheck disable=SC2206 -- deliberate word splitting of the config string.
args+=(${WK_BUILD_ARGS:-})

# Free on the CMake ports; costly on the Apple ports (disables the
# precompiled prefix headers WebCore/WebKit/JSC use), but without it clangd
# and jump-to-definition stop working. WK_NO_COMPILE_COMMANDS=1 opts out.
[ -n "${WK_NO_COMPILE_COMMANDS:-}" ] || args+=(--export-compile-commands)

# cmake takes --makeargs=-jN; xcodebuild takes -jobs N and silently ignores
# --makeargs, defaulting to its own core count.
case "$buildsys" in
xcode)
    # One list of xcodebuild settings, however it has to be delivered below.
    xc=(-jobs "$jobs")

    # WEBKIT_OUTPUTDIR alone disagrees with webkitdirs by one directory
    # level; WK_CONFIGURATION_BUILD_DIR is WebKit's own hook for pinning it.
    # SHARED_PRECOMPS_DIR has to be repeated by hand, or all four Apple
    # configs share one precompiled-header directory.
    if [ -n "${WEBKIT_OUTPUTDIR:-}" ]; then
        xc+=("WK_CONFIGURATION_BUILD_DIR=$WEBKIT_OUTPUTDIR")
        xc+=("SHARED_PRECOMPS_DIR=$WEBKIT_OUTPUTDIR/PrecompiledHeaders")
    fi

    # NOT -derivedDataPath: build-webkit's second xcodebuild call
    # (build-imagediff, `-project` with no `-scheme`) refuses it outright.
    if [ -n "${WK_DERIVED_DATA:-}" ]; then
        xc+=("COMPILATION_CACHE_CAS_PATH=$WK_DERIVED_DATA/CompilationCache.noindex")
        xc+=("MODULE_CACHE_DIR=$WK_DERIVED_DATA/ModuleCache.noindex")
    fi

    # WK_NO_COMPILATION_CACHE=1 is the escape hatch for debugging Swift
    # types: with caching on (the default), debug info is only as durable as
    # the CAS. Costs a full rebuild to switch; C++ debugging is unaffected
    # either way.
    [ -n "${WK_NO_COMPILATION_CACHE:-}" ] && xc+=("COMPILATION_CACHE_ENABLE_CACHING=NO")

    # The one place the two scripts genuinely differ, and it is forced: an
    # unrecognised argument to build-webkit goes on to xcodebuild, while
    # build-jsc appends it to a `make` command line, where -jobs is make's own
    # flag and a setting is a make variable. `ARGS=` is the project Makefile's
    # declared hole for xcodebuild settings (Makefile.shared: XCODE_OPTIONS =
    # $(ARGS)), and it is expanded unquoted there, so this joins on spaces --
    # a checkout path containing a space is not buildable either way.
    case "$script" in
        */build-jsc) args+=("ARGS=${xc[*]}") ;;
        *)           args+=("${xc[@]}") ;;
    esac
    ;;
*)
    # build-webkit removes CMakeCache.txt itself on an ordinary -D change.
    # Six "identity variables" are stamped in .webkit-config-stamp *outside*
    # the cache instead, so wiping it cannot revert them -- changing one
    # stops configure dead, mid-build, with a manual rm -rf the only way
    # forward. That remedy is applied here first.
    if [ -n "$cmakeargs" ] && [ -f "${WK_BUILD_DIR:-}/.webkit-config-stamp" ]; then (
        # A subshell: `set --` word-splits the flag string, and "$@" here
        # is the caller's passthrough arguments.
        stamp="$WK_BUILD_DIR/.webkit-config-stamp"
        stale=""
        eval "set -- $cmakeargs"
        while IFS='=' read -r name prev; do
            [ -n "$name" ] || continue
            # Last -D wins, matching cmake's own behavior on a repeat.
            want=""
            for a in "$@"; do
                case "$a" in -D"$name"=*) want=${a#-D"$name"=} ;; esac
            done
            [ -n "$want" ] || continue
            [ "$want" = "$prev" ] && continue
            stale="$stale  $name: '$prev' -> '$want'
"
        done < "$stamp"
        [ -n "$stale" ] || exit 0
        echo "wiping $WK_BUILD_DIR -- WebKit's identity variables changed:" >&2
        printf '%s' "$stale" >&2
        echo "  a build directory cannot be reconfigured across these" >&2
        echo "  (Source/cmake/WebKitCommon.cmake:29), so it is rebuilt from scratch" >&2
        rm -rf "$WK_BUILD_DIR"
    ); fi

    [ -n "$cmakeargs" ] && args+=(--cmakeargs="$cmakeargs")
    args+=("--makeargs=-j$jobs")
    ;;
esac

# WK_DRY_RUN: print the command, build nothing. Done here, not in cmd/build,
# since this is the half that resolves ionice/choom/the cgroup clamp.
if [ -n "${WK_DRY_RUN:-}" ]; then
    printf 'cd %s && ' "$(_q "$SRC")"
    # shellcheck disable=SC2086,SC2046 -- $wrapper and the guard prefix are deliberate word lists.
    set -- $wrapper $(_guard_prefix) \
        "$script" "${args[@]}" ${@+"$@"}
    for _a in "$@"; do printf '%s ' "$(_q "$_a")"; done
    printf '\n'
    exit 0
fi

set -x
# The guard (build/guard.sh): memory watchdog on this pid, ionice, nice.
# shellcheck disable=SC2086 -- $wrapper is a deliberate list of bare words.
guard_exec "$jobs" -- $wrapper "$script" "${args[@]}" ${@+"$@"}
