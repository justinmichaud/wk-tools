# `wk build <ws> mac-release-pgo`: the three phases README.md describes, sourced by build/build-in-target.sh inside the guest that builds (bash 3.2, macOS). The flags are `make release`'s (WebKit's Makefile.shared) as build-webkit arguments, and $(inherited) is not optional -- OTHER_LDFLAGS and OTHER_CFLAGS replace a framework's own flags without it.

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/pgo.sh"   # PGO_BENCHMARKS and PGO_COLLECT_TIMEOUT: what this lane shares with the board one
PGO_INSTR_SUFFIX=-instr

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

# A collection behind a pane that has the focus, or on a machine with no pyobjc to raise the browser with, profiles a throttled browser and looks exactly like a good one.
_pgo_screen_faults() {   # names every reason this machine cannot present an unthrottled browser
    local console blocker
    console=$(stat -f '%Su' /dev/console 2>/dev/null) || console=""
    [ "$console" = "$(id -un)" ] \
        || echo "  the screen belongs to '${console:-nobody}', not $(id -un) -- a browser driven over ssh has nowhere to draw"
    if ! ( . "$1/bench/mac-pyobjc.sh"; wk_pyobjc_have ) 2>/dev/null; then
        echo "  no pyobjc: run-benchmark cannot size the screen and no raiser can hold the browser in front"
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

# speedometer3 and jetstream3 name a moving branch, so an unpinned collection can profile the two arms against two revisions of the benchmark. seed_payload (lib/bench.sh) keys a copy by its upstream commit; collect-pgo-profiles passes `local-copy` through per benchmark.
_pgo_payload_args() {   # <tools> <pins file> -- prints one argument per line
    local tools="$1" pins="$2" plan dir
    : > "$pins"
    for plan in $PGO_BENCHMARKS; do
        dir=$( export WK_ROOT="$tools"
               . "$tools/lib/common.sh" >/dev/null 2>&1
               . "$tools/lib/store.sh" >/dev/null 2>&1
               . "$tools/lib/bench.sh" >/dev/null 2>&1
               bench_plan_read() { cat "$SRC/Tools/Scripts/$1"; }
               seed_payload "$plan" 2>/dev/null | tail -1 )
        [ -d "$dir" ] || { echo "wk: could not pin the $plan payload" >&2; return 1; }
        printf '%s\t%s\n' "$plan" "$dir" >> "$pins"
        printf -- '--benchmark-custom-options\n%s\nlocal-copy:%s\ntimeout:%s\n' \
            "$plan" "$dir" "$PGO_COLLECT_TIMEOUT"
    done
}

_pgo_collect() {   # <instrumented products> <profile dir> <arch>
    local instr="$1" pgo="$2" arch="$3" tools state rc=0
    tools="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

    if [ -n "${WK_DRY_RUN:-}" ]; then
        printf 'rm -rf %s && WK_WEBKIT_SCRIPTS=%s %s %s --benchmarks %s --output-directory %s --compressed-profile-sub-path %s --build-directory %s --browser minibrowser --run-benchmark-harness %s\n' \
            "$(_q "$pgo")" "$(_q "$SRC/Tools/Scripts")" /usr/bin/python3 \
            "$(_q "$SRC/Tools/Scripts/collect-pgo-profiles")" "$PGO_BENCHMARKS" \
            "$(_q "$pgo")" "$arch" "$(_q "$instr")" "$(_q "$tools/build/pgo-run-benchmark.py")"
        printf '  each benchmark --benchmark-custom-options <name> local-copy:<pinned payload>\n'
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
    mkdir -p "$state"
    mac_raiser_on "$state"

    if ! /usr/bin/python3 "$tools/bench/mac-browser-check.py" \
            --build-directory "$instr" \
            --json "$state/browser-check.json" >&2; then
        echo "wk: the instrumented build cannot present an accelerated, unthrottled browser here," >&2
        echo "  so every profile it collected would be of the wrong code (above)." >&2
        mac_raiser_off "$state"
        return 1
    fi

    # Into a file first: a `while read` fed by a process substitution reports the read's status and never the producer's, so a failed pin would collect anyway.
    local pins="$state/payload-pins" pargs=() line
    if ! _pgo_payload_args "$tools" "$pins" > "$state/payload-args"; then
        mac_raiser_off "$state"
        return 1
    fi
    while IFS= read -r line; do pargs+=("$line"); done < "$state/payload-args"
    echo "wk: profiling against pinned payloads:" >&2
    sed 's/^/  /' "$pins" >&2

    rm -rf "$pgo"

    screen_watch_start "$state/screen-watch"

    # shellcheck disable=SC2086 -- $PGO_BENCHMARKS is a deliberate word list.
    env WK_WEBKIT_SCRIPTS="$SRC/Tools/Scripts" \
        /usr/bin/python3 "$SRC/Tools/Scripts/collect-pgo-profiles" \
            --run-benchmark-harness "$tools/build/pgo-run-benchmark.py" \
            --benchmarks $PGO_BENCHMARKS \
            --output-directory "$pgo" \
            --compressed-profile-sub-path "$arch" \
            --build-directory "$instr" \
            --browser minibrowser \
            ${pargs[@]+"${pargs[@]}"} || rc=$?

    local seen
    seen=$(screen_watch_stop "$state/screen-watch") || {
        mac_raiser_off "$state"
        echo "wk: something drew over this collection, so every leg after it profiled a covered browser:" >&2
        printf '%s\n' "$seen" | sed 's/^/  /' >&2
        return 1
    }
    mac_raiser_off "$state"
    [ "$rc" -eq 0 ] || return "$rc"

    cp "$pins" "$pgo/payload-pins" 2>/dev/null || true

    /usr/bin/python3 "$tools/lib/wkpgo.py" check --dir "$pgo" \
        --scripts "$SRC/Tools/Scripts" --compressed "$arch" \
        --json "$state/profile-check.json" >&2 \
        || { echo "wk: the collection finished and its profile is not one to build against (above)." >&2
             return 1; }
}

# The readings that justify this build, beside the products so they are staged with it: a staged arm carries what was measured of the browser and of the profile it came from, rather than leaving that in a build log on another machine.
_pgo_evidence() {   # <products dir>
    local state="$HOME/.local/state/wk/pgo"
    [ -n "${WK_DRY_RUN:-}" ] && return 0
    mkdir -p "$1"
    cp "$state/browser-check.json" "$1/wk-browser-check.json" 2>/dev/null || true
    cp "$state/profile-check.json" "$1/wk-profile-check.json" 2>/dev/null || true
    cp "$WK_PGO_DIR/payload-pins"  "$1/wk-payload-pins"       2>/dev/null || true
}

pgo_build() {
    [ "$(uname -s)" = Darwin ] || [ -n "${WK_DRY_RUN:-}" ] \
        || { echo "wk: a PGO build is the Apple port's, and this is not macOS" >&2; return 1; }
    local final="${WEBKIT_OUTPUTDIR:-}" pgo="${WK_PGO_DIR:-}" arch
    [ -n "$final" ] || { echo "wk: WEBKIT_OUTPUTDIR is unset; build/configs.sh sets it for every Apple config" >&2; return 1; }
    [ -n "$pgo" ] || { echo "wk: WK_PGO_DIR is unset; build/configs.sh sets it for a PGO config" >&2; return 1; }
    arch=$(uname -m)
    local instr="$final$PGO_INSTR_SUFFIX"

    _pgo_run "instrumented (thin LTO, profile generation)" "$instr" \
        "${@}" --lto-mode=thin \
        ENABLE_LLVM_PROFILE_GENERATION=YES \
        'OTHER_LDFLAGS=$(inherited) -fprofile-generate' || return $?

    _pgo_collect "$instr" "$pgo" "$arch" || return $?

    # ENABLE_USER_SCRIPT_SANDBOXING=NO: bmalloc/WTF/JavaScriptCore run "Copy Profiling Data" under Xcode's script sandbox, which declares arm64e and x86_64 and so denies reading this arch's profile.
    _pgo_evidence "$final"

    _pgo_run "measured (full LTO, -O3, profile use)" "$final" \
        "${@}" --lto-mode=full \
        WK_ENABLE_PGO_USE=YES \
        "WK_COMPRESSED_OPTIMIZATION_PROFILE_FOLDER=$pgo" \
        ENABLE_USER_SCRIPT_SANDBOXING=NO \
        DEBUG_INFORMATION_FORMAT=dwarf-with-dsym \
        WK_DEFAULT_GCC_OPTIMIZATION_LEVEL=3 \
        'OTHER_CFLAGS=$(inherited) -fno-omit-frame-pointer' || return $?
}
