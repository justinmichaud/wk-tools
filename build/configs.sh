# The named build configurations are lib/wk/buildconf.py; `config_load <name> <os> [kind]` sets its CFG_* fields for a bash caller, and the accessors below read them.

command -v warn >/dev/null 2>&1 || . "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

config_load() { # <name> <os: linux|macos> [kind: container|vm|local|remote]
    local out rc=0 root
    root="${WK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
    out=$(PYTHONPATH="$root/lib" WK_ROOT="$root" WK_TARGET_KIND="${WK_TARGET_KIND:-}" WK_TARGET="${WK_TARGET:-}" \
          WK_TARGET_LIBCXX="${WK_TARGET_LIBCXX:-}" python3 -m wk.buildconf shell "$1" "${2:-}" ${3:+"$3"}) || rc=$?
    case "$rc" in
        0) ;;
        3) return 1 ;;
        *) exit "$rc" ;;
    esac
    eval "$out"
    [ -n "${WK_MB_PER_JOB_EXPLICIT:-}" ] || WK_MB_PER_JOB=$CFG_MB_PER_JOB
    return 0
}

config_build_dir()             { printf '%s/%s\n' "${1:-/src/WebKit}" "$CFG_BUILD_SUBDIR"; }
config_jsc_path()              { printf '%s/%s\n' "$1" "$CFG_JSC_REL"; }
config_run_var()               { printf '%s\n' "$CFG_RUN_VAR"; }
config_run_dir()               { printf '%s/%s\n' "$1" "$CFG_RUN_REL"; }

# A board's WebKit is not built by any of the above: `wk sysimage webkit` cross-builds it with build-webkit --cross-target against a Yocto SDK, and its port and flags are the release branch's own (Tools/yocto/targets.conf). So the cross configs are named here, beside the rest, and what each one adds is the PGO phase and the toolchain that phase requires. GCC is not one of them -- upstream's own PGO support is clang-only (WebKitCommon.cmake).
config_cross_list() {
    cat <<'EOF'
wpe-cross              the release branch's own flags and nothing else
wpe-cross-pgo-collect  clang and LLVM profile generation -- the collection
                       build, which nothing measures
wpe-cross-pgo-use      clang and a collected profile -- the build every number
                       from a 2.52+ board is taken from
EOF
}

[ -n "${PGO_BOARD_DIR:-}" ] || . "$(dirname "${BASH_SOURCE[0]}")/pgo.sh"

config_cross_load() {   # <name> [merged .profdata, as the builder sees it] -> XCFG_PGO, XCFG_CC/CXX, XCFG_CMAKE. The two PGO configs share one build directory, so each states both options: cmake refuses the pair (WEBKIT_OPTION_CONFLICT), and leaving the other's cached value in place is how that happens
    XCFG_NAME="$1"; XCFG_PGO=""; XCFG_CC=""; XCFG_CXX=""; XCFG_CMAKE=""
    case "$1" in
        wpe-cross) ;;
        wpe-cross-pgo-collect)
            XCFG_PGO=collect; XCFG_CC=clang; XCFG_CXX=clang++
            XCFG_CMAKE="-DENABLE_LLVM_PROFILE_GENERATION=ON -DUSE_PGO_PROFILE=OFF -DPGO_PROFILE_DIR=$PGO_BOARD_DIR" ;;
        wpe-cross-pgo-use)
            [ -n "${2:-}" ] || die "config_cross_load wpe-cross-pgo-use: no profile given.
    The measured build reads one merged .profdata, and cmake refuses without
    it (PGO_PROFILE_PATH); image/pgo.sh collects one first."
            XCFG_PGO=use; XCFG_CC=clang; XCFG_CXX=clang++
            XCFG_CMAKE="-DENABLE_LLVM_PROFILE_GENERATION=OFF -DUSE_PGO_PROFILE=ON -DPGO_PROFILE_PATH=$2" ;;
        *) return 1 ;;
    esac
    return 0
}
