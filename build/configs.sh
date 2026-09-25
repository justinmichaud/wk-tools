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

