#!/usr/bin/env bash
#
# Runs inside a workspace, invoked by `wk build`: cmd/build decides policy
# outside the target and sets the WK_* variables read here. Runs on a Fedora
# container and a macOS guest alike -- bash 3.2, no cgroups/ionice/choom, and
# "${arr[@]}" on an empty array errors under `set -u`, hence lists as strings.

set -euo pipefail

# The build wall (container/bin/wk-build-wall) passes cmake/ninja/make for this.
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

script=${WK_BUILD_SCRIPT:-Tools/Scripts/build-webkit}

cmakeargs=${WK_BUILD_CMAKE:-}

# CMake caches these at *configure* time: an architecture is fixed at creation.
# WK_ARCH plus WK_ARCH_WRAPPER/WK_ARCH_CFLAGS/WK_ARCH_LDFLAGS for a non-native
# workspace; WK_BUILDSYS, WK_BUILD_SCRIPT, WK_SRC, WK_BUILD_DIR and
# WK_DERIVED_DATA are the config's own, exported by build/configs.sh.
arch=${WK_ARCH:-native}
if [ "$arch" != native ]; then
    export CFLAGS="${WK_ARCH_CFLAGS:-} ${CFLAGS:-}"
    export CXXFLAGS="${WK_ARCH_CFLAGS:-} ${CXXFLAGS:-}"
    export LDFLAGS="${WK_ARCH_LDFLAGS:-} ${LDFLAGS:-}"
fi

# `uname -m` in an armhf container answers aarch64 (host kernel) without linux32.
wrapper=${WK_ARCH_WRAPPER:-}

# Array, not a string: --cmakeargs is several -D flags as ONE argument.
args=()
# shellcheck disable=SC2206 -- deliberate word splitting of the config string.
args+=(${WK_BUILD_ARGS:-})

# It disables the Apple ports' precompiled prefix headers, but clangd needs it.
[ -n "${WK_NO_COMPILE_COMMANDS:-}" ] || args+=(--export-compile-commands)

# xcodebuild takes -jobs N, ignores --makeargs, and otherwise uses its own count.
case "$buildsys" in
xcode)
    xc=(-jobs "$jobs")

    # WEBKIT_OUTPUTDIR alone disagrees with webkitdirs by one directory level, and
    # SHARED_PRECOMPS_DIR has to be repeated, or the Apple configs share a PCH dir.
    if [ -n "${WEBKIT_OUTPUTDIR:-}" ]; then
        xc+=("WK_CONFIGURATION_BUILD_DIR=$WEBKIT_OUTPUTDIR")
        xc+=("SHARED_PRECOMPS_DIR=$WEBKIT_OUTPUTDIR/PrecompiledHeaders")
    fi

    # NOT -derivedDataPath: build-webkit's second xcodebuild call refuses it.
    if [ -n "${WK_DERIVED_DATA:-}" ]; then
        xc+=("COMPILATION_CACHE_CAS_PATH=$WK_DERIVED_DATA/CompilationCache.noindex")
        xc+=("MODULE_CACHE_DIR=$WK_DERIVED_DATA/ModuleCache.noindex")
    fi

    # For debugging Swift types: with caching on, debug info lives only in the CAS.
    [ -n "${WK_NO_COMPILATION_CACHE:-}" ] && xc+=("COMPILATION_CACHE_ENABLE_CACHING=NO")

    # Forced: build-webkit hands an unrecognised argument to xcodebuild, build-jsc
    # appends it to a `make` line -- `ARGS=` is Makefile.shared's hole for them.
    case "$script" in
        */build-jsc) args+=("ARGS=${xc[*]}") ;;
        *)           args+=("${xc[@]}") ;;
    esac
    ;;
*)
    # Six "identity variables" are stamped in .webkit-config-stamp, outside the cache
    # build-webkit wipes: changing one stops configure dead, rm -rf the only way on.
    if [ -n "$cmakeargs" ] && [ -f "${WK_BUILD_DIR:-}/.webkit-config-stamp" ]; then (
        # A subshell: `set --` word-splits the flags without losing the caller's "$@".
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

# Here, not in cmd/build: this half resolves ionice/choom and the cgroup clamp.
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
# shellcheck disable=SC2086 -- $wrapper is a deliberate list of bare words.
guard_exec "$jobs" -- $wrapper "$script" "${args[@]}" ${@+"$@"}
