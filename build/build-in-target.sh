#!/usr/bin/env bash
# Runs inside a workspace, invoked by `wk build`: cmd/build decides policy outside the target and sets the WK_* variables read here. Runs on a Fedora container and a macOS guest alike -- bash 3.2, no cgroups/ionice/choom, and "${arr[@]}" on an empty array errors under `set -u`, hence lists as strings.

set -euo pipefail

export WK_BUILD=1   # the build wall (container/bin/wk-build-wall) passes cmake/ninja/make for this

_q() {   # ${var@Q} needs bash 4.4, and this file has to parse under macOS's bash 3.2
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

# CMake caches these at *configure* time, so an architecture is fixed at creation: WK_ARCH plus WK_ARCH_WRAPPER/WK_ARCH_CFLAGS/WK_ARCH_LDFLAGS for a non-native workspace, and WK_BUILDSYS, WK_BUILD_SCRIPT, WK_SRC, WK_BUILD_DIR, WK_DERIVED_DATA and -- for a profile-guided config -- WK_PGO and WK_PGO_DIR the config's own, exported by build/configs.sh.
arch=${WK_ARCH:-native}
if [ "$arch" != native ]; then
    export CFLAGS="${WK_ARCH_CFLAGS:-} ${CFLAGS:-}"
    export CXXFLAGS="${WK_ARCH_CFLAGS:-} ${CXXFLAGS:-}"
    export LDFLAGS="${WK_ARCH_LDFLAGS:-} ${LDFLAGS:-}"
fi

wrapper=${WK_ARCH_WRAPPER:-}   # `uname -m` in an armhf container answers aarch64, the host kernel, without linux32

args=()   # an array and not a string: --cmakeargs is several -D flags as ONE argument
# shellcheck disable=SC2206 -- deliberate word splitting of the config string.
args+=(${WK_BUILD_ARGS:-})

[ -n "${WK_NO_COMPILE_COMMANDS:-}" ] || args+=(--export-compile-commands)   # it disables the Apple ports' precompiled prefix headers, but clangd needs it

# xcodebuild takes -jobs N, ignores --makeargs, and otherwise uses its own count.
_xc_settings() {   # <products dir> -- fills XC with the settings every Apple-port build takes; a phase of a PGO build calls it once per products directory
    XC=(-jobs "$jobs")

    if [ -n "$1" ]; then   # WEBKIT_OUTPUTDIR alone disagrees with webkitdirs by one directory level, and SHARED_PRECOMPS_DIR has to be repeated or the Apple configs share a PCH dir
        XC+=("WK_CONFIGURATION_BUILD_DIR=$1")
        XC+=("SHARED_PRECOMPS_DIR=$1/PrecompiledHeaders")
    fi

    if [ -n "${WK_DERIVED_DATA:-}" ]; then   # NOT -derivedDataPath: build-webkit's second xcodebuild call refuses it
        XC+=("COMPILATION_CACHE_CAS_PATH=$WK_DERIVED_DATA/CompilationCache.noindex")
        XC+=("MODULE_CACHE_DIR=$WK_DERIVED_DATA/ModuleCache.noindex")
    fi

    [ -n "${WK_NO_COMPILATION_CACHE:-}" ] && XC+=("COMPILATION_CACHE_ENABLE_CACHING=NO")   # for debugging Swift types: with caching on, debug info lives only in the CAS
    return 0
}

case "$buildsys" in
xcode)
    _xc_settings "${WEBKIT_OUTPUTDIR:-}"

    if [ -n "${WK_PGO:-}" ]; then   # three phases with a benchmark run between them, so it cannot be one exec
        . "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/mac-pgo.sh"
        pgo_build ${args[@]+"${args[@]}"}
        exit $?
    fi

    case "$script" in   # build-webkit hands an unrecognised argument to xcodebuild, build-jsc appends it to a `make` line, and `ARGS=` is Makefile.shared's hole for them
        */build-jsc) args+=("ARGS=${XC[*]}") ;;
        *)           args+=("${XC[@]}") ;;
    esac
    ;;
*)
    # Six "identity variables" are stamped in .webkit-config-stamp, outside the cache build-webkit wipes: changing one stops configure dead, and rm -rf is the only way on.
    if [ -n "$cmakeargs" ] && [ -f "${WK_BUILD_DIR:-}/.webkit-config-stamp" ]; then (
        stamp="$WK_BUILD_DIR/.webkit-config-stamp"   # a subshell: `set --` word-splits the flags without losing the caller's "$@"
        stale=""
        eval "set -- $cmakeargs"
        while IFS='=' read -r name prev; do
            [ -n "$name" ] || continue
            want=""   # last -D wins, matching cmake's own behavior on a repeat
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

if [ -n "${WK_DRY_RUN:-}" ]; then   # printed here and not in cmd/build: this half resolves ionice/choom and the cgroup clamp
    printf 'cd %s && ' "$(_q "$SRC")"
    # shellcheck disable=SC2086,SC2046 -- $wrapper and the guard prefix are deliberate word lists.
    set -- $wrapper $(_guard_prefix) \
        "$script" "${args[@]}" ${@+"$@"}
    for _a in "$@"; do printf '%s ' "$(_q "$_a")"; done
    printf '\n'
    exit 0
fi

echo "wk: build pid $$" >&2   # the pid `wk build --kill` and ^C signal: down the log, the one channel back to the driver on every target kind, and kept by guard_exec's exec, so every compiler is under it

set -x
# shellcheck disable=SC2086 -- $wrapper is a deliberate list of bare words.
guard_exec "$jobs" -- $wrapper "$script" "${args[@]}" ${@+"$@"}
