# `wk build <ws> mac-release-pgo`: the three phases README.md describes, sourced by build/build-in-target.sh inside the guest that builds (bash 3.2, macOS). The flags are `make release`'s (WebKit's Makefile.shared) as build-webkit arguments, and $(inherited) is not optional -- OTHER_LDFLAGS and OTHER_CFLAGS replace a framework's own flags without it.

_pgo_tools="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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

_pgo_py() {   # the collection and its evidence are lib/wk/bench/mac.py's PgoCollect
    PYTHONPATH="$_pgo_tools/lib" WK_ROOT="$_pgo_tools" /usr/bin/python3 -m wk.bench.mac "$@"
}

pgo_build() {
    [ "$(uname -s)" = Darwin ] || [ -n "${WK_DRY_RUN:-}" ] \
        || { echo "wk: a PGO build is the Apple port's, and this is not macOS" >&2; return 1; }
    local final="${WEBKIT_OUTPUTDIR:-}" pgo="${WK_PGO_DIR:-}" arch
    [ -n "$final" ] || { echo "wk: WEBKIT_OUTPUTDIR is unset; lib/wk/buildconf.py sets it for every Apple config" >&2; return 1; }
    [ -n "$pgo" ] || { echo "wk: WK_PGO_DIR is unset; lib/wk/buildconf.py sets it for a PGO config" >&2; return 1; }
    arch=$(uname -m)
    local instr
    instr=$(_pgo_py pgo-instr "$final") || return 1

    _pgo_run "instrumented (thin LTO, profile generation)" "$instr" \
        "${@}" --lto-mode=thin \
        ENABLE_LLVM_PROFILE_GENERATION=YES \
        'OTHER_LDFLAGS=$(inherited) -fprofile-generate' || return $?

    _pgo_py pgo-collect "$SRC" "$instr" "$pgo" "$arch" || return $?

    # ENABLE_USER_SCRIPT_SANDBOXING=NO: bmalloc/WTF/JavaScriptCore run "Copy Profiling Data" under Xcode's script sandbox, which declares arm64e and x86_64 and so denies reading this arch's profile.
    _pgo_py pgo-evidence "$SRC" "$final" "$pgo" || return $?

    _pgo_run "measured (full LTO, -O3, profile use)" "$final" \
        "${@}" --lto-mode=full \
        WK_ENABLE_PGO_USE=YES \
        "WK_COMPRESSED_OPTIMIZATION_PROFILE_FOLDER=$pgo" \
        ENABLE_USER_SCRIPT_SANDBOXING=NO \
        DEBUG_INFORMATION_FORMAT=dwarf-with-dsym \
        WK_DEFAULT_GCC_OPTIMIZATION_LEVEL=3 \
        'OTHER_CFLAGS=$(inherited) -fno-omit-frame-pointer' || return $?
}
