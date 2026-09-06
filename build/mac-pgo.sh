# `wk build <ws> mac-release-pgo`: the three phases README.md describes, sourced by build/build-in-target.sh inside the guest that builds (bash 3.2, macOS). The flags are `make release`'s (WebKit's Makefile.shared) as build-webkit arguments, and $(inherited) is not optional -- OTHER_LDFLAGS and OTHER_CFLAGS replace a framework's own flags without it.

PGO_BENCHMARKS="speedometer3 jetstream3 motionmark"

_pgo_run() {   # <phase label> <products dir> ; the remaining arguments are build-webkit's
    local label="$1" products="$2"; shift 2
    echo "wk: pgo phase: $label -> $products" >&2
    mkdir -p "$products" || return 1
    _xc_settings "$products"
    if [ -n "${WK_DRY_RUN:-}" ]; then
        printf 'cd %s && WEBKIT_OUTPUTDIR=%s %s' "$(_q "$SRC")" "$(_q "$products")" "$(_q "$script")"
        local _a
        for _a in "$@" "${XC[@]}"; do printf ' %s' "$(_q "$_a")"; done
        printf '\n'
        return 0
    fi
    ( export WEBKIT_OUTPUTDIR="$products"
      guard_run "$jobs" -- $wrapper "$script" "$@" "${XC[@]}" )
}

# Measured on a Tart guest 2026-09-06: Setup Assistant is frontmost on every boot, killing it takes the console session with it (/dev/console goes admin -> root), and /usr/bin/python3 there has no pyobjc, so no raiser can displace it. A collection run behind it profiles a throttled browser and looks like a good one.
_pgo_screen_faults() {   # names every reason this machine cannot present an unthrottled browser
    local console blocker
    console=$(stat -f '%Su' /dev/console 2>/dev/null) || console=""
    [ "$console" = "$(id -un)" ] \
        || echo "  the screen belongs to '${console:-nobody}', not $(id -un) -- a browser driven over ssh has nowhere to draw"
    if ! /usr/bin/python3 -c 'import AppKit' 2>/dev/null; then
        echo "  /usr/bin/python3 cannot 'import objc' -- with no raiser, anything that takes focus throttles the run"
        return 0
    fi
    /usr/bin/python3 -c 'import AppKit, sys; sys.exit(0 if AppKit.NSScreen.mainScreen() else 1)' 2>/dev/null \
        || echo "  there is no main screen, so nothing can be drawn at all"
    blocker=$( . "$1/lib/quiet.sh" >/dev/null 2>&1; screen_blocker 2>/dev/null )
    case "$blocker" in
        ""|"?") ;;
        *) echo "  on the screen, and nothing this build put there: $blocker" ;;
    esac
}

_pgo_collect() {   # <instrumented products> <profile dir> <arch>
    local instr="$1" pgo="$2" arch="$3" tools state rc=0
    tools="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

    if [ -n "${WK_DRY_RUN:-}" ]; then
        printf 'rm -rf %s && WK_WEBKIT_SCRIPTS=%s %s %s --benchmarks %s --output-directory %s --compressed-profile-sub-path %s --build-directory %s --browser minibrowser --run-benchmark-harness %s\n' \
            "$(_q "$pgo")" "$(_q "$SRC/Tools/Scripts")" /usr/bin/python3 \
            "$(_q "$SRC/Tools/Scripts/collect-pgo-profiles")" "$PGO_BENCHMARKS" \
            "$(_q "$pgo")" "$arch" "$(_q "$instr")" "$(_q "$tools/build/pgo-run-benchmark.py")"
        return 0
    fi

    local faults; faults=$(_pgo_screen_faults "$tools")
    if [ -n "$faults" ]; then
        echo "wk: this machine cannot present an unthrottled browser, so every profile it collected would be of a throttled one:" >&2
        printf '%s\n' "$faults" >&2
        echo "  Collect on a machine that can -- the benchmark install (docs/HANDOFF-mac-perf-mode.md)." >&2
        return 1
    fi

    # shellcheck disable=SC1090
    . "$tools/lib/quiet.sh"
    state="$HOME/.local/state/wk/pgo"
    mac_raiser_on "$state"

    rm -rf "$pgo"   # collect-pgo-profiles refuses a directory that is not empty

    # TODO: pin the three payloads (docs/HANDOFF-mac-perf-mode.md); run-benchmark fetches each itself here.
    # shellcheck disable=SC2086 -- $PGO_BENCHMARKS is a deliberate word list.
    env WK_WEBKIT_SCRIPTS="$SRC/Tools/Scripts" \
        /usr/bin/python3 "$SRC/Tools/Scripts/collect-pgo-profiles" \
            --run-benchmark-harness "$tools/build/pgo-run-benchmark.py" \
            --benchmarks $PGO_BENCHMARKS \
            --output-directory "$pgo" \
            --compressed-profile-sub-path "$arch" \
            --build-directory "$instr" \
            --browser minibrowser || rc=$?

    mac_raiser_off "$state"
    return "$rc"
}

pgo_build() {
    [ "$(uname -s)" = Darwin ] || [ -n "${WK_DRY_RUN:-}" ] \
        || { echo "wk: a PGO build is the Apple port's, and this is not macOS" >&2; return 1; }
    local final="${WEBKIT_OUTPUTDIR:-}" pgo="${WK_PGO_DIR:-}" arch
    [ -n "$final" ] || { echo "wk: WEBKIT_OUTPUTDIR is unset; build/configs.sh sets it for every Apple config" >&2; return 1; }
    [ -n "$pgo" ] || { echo "wk: WK_PGO_DIR is unset; build/configs.sh sets it for a PGO config" >&2; return 1; }
    arch=$(uname -m)
    local instr="$final-instr"

    _pgo_run "instrumented (thin LTO, profile generation)" "$instr" \
        "${@}" --lto-mode=thin \
        ENABLE_LLVM_PROFILE_GENERATION=YES \
        'OTHER_LDFLAGS=$(inherited) -fprofile-generate' || return $?

    _pgo_collect "$instr" "$pgo" "$arch" || return $?

    if [ -z "${WK_DRY_RUN:-}" ] && [ ! -d "$pgo/$arch" ]; then
        echo "wk: the collection produced no profile at $pgo/$arch" >&2
        return 1
    fi

    # ENABLE_USER_SCRIPT_SANDBOXING=NO: bmalloc/WTF/JavaScriptCore run "Copy Profiling Data" under Xcode's script sandbox, which declares arm64e and x86_64 and so denies reading this arch's profile.
    _pgo_run "measured (full LTO, -O3, profile use)" "$final" \
        "${@}" --lto-mode=full \
        WK_ENABLE_PGO_USE=YES \
        "WK_COMPRESSED_OPTIMIZATION_PROFILE_FOLDER=$pgo" \
        ENABLE_USER_SCRIPT_SANDBOXING=NO \
        DEBUG_INFORMATION_FORMAT=dwarf-with-dsym \
        WK_DEFAULT_GCC_OPTIMIZATION_LEVEL=3 \
        'OTHER_CFLAGS=$(inherited) -fno-omit-frame-pointer' || return $?
}
