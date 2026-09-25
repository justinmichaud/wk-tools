command -v reach_tailnet >/dev/null 2>&1 || . "$WK_ROOT/lib/reach.sh"
command -v disk_part >/dev/null 2>&1 || . "$WK_ROOT/boot/disk.sh"

machine_names() { wk_fleet list --kind board --kind mac --kind guest; }

machine_list() {
    local n
    for n in $(machine_names); do
        machine_load "$n" 2>/dev/null || continue
        printf '%-8s%s\n' "$n" "$NODE_NOTE"
    done
}

machine_declare() {
    local n
    for n in $(machine_names); do
        machine_load "$n" 2>/dev/null || continue
        printf '%s\t%s\t%s\t%s\t%s\n' \
            "$n" "${NODE_ROLE:-workstation}" "${NODE_OS:-any}" "${NODE_PROFILE:-}" "$NODE_NOTE"
    done
}

machine_load() {
    local out
    out=$(wk_fleet load "$1" --kind board --kind mac --kind guest) || return 1
    NODE_NAME="$1"
    NODE_SSH=""; NODE_DRIVER=""; NODE_DEVICE=""; NODE_ROOT=""; NODE_PROFILE=""
    NODE_NOTE=""; NODE_MAC=""; NODE_VOLUME=""; NODE_DTB=""
    NODE_BENCH_SSH=""; NODE_NET=""; NODE_DISPLAY=""
    NODE_BRIDGE=""  # declared, not discovered: readable when unreachable
    NODE_ROLE=workstation
    NODE_OS=any  # or an OS name for a machine that answers only for itself
    eval "$out"
    [ -n "$NODE_DRIVER" ] && [ -n "$NODE_NOTE" ] || return 1
}

load_driver() {
    local d="$WK_ROOT/boot/$1.sh"
    [ -f "$d" ] || die "no boot driver '$1'"
    # shellcheck disable=SC1090
    . "$d"
}

m_here() { # am I the machine NODE_SSH names? Case-insensitively: a machine's own spelling of its name need not be the conf's
    local me
    [ -n "${NODE_SSH:-}" ] || return 1
    me=$(hostname -s 2>/dev/null) || return 1
    [ "$(printf '%s' "$me" | tr '[:upper:]' '[:lower:]')" \
        = "$(printf '%s' "$NODE_SSH" | tr '[:upper:]' '[:lower:]')" ]
}

m_ssh() { # the one way to run a command on a machine, whichever machine this is
    if m_here; then bash -c "$*"; return $?; fi   # standing on it, and not a flag a conf declares: a machine drives itself only from its own keyboard, and from anywhere else that answers about the wrong computer
    reach_offline "$NODE_SSH" && { warn "$REACH_WHY"; return 255; }   # 255 is ssh's own "could not connect", which every caller here already reads
    # shellcheck disable=SC2086
    ssh -o BatchMode=yes -o ConnectTimeout="$(wk_ssh_timeout)" \
        $(m_ssh_opts) "$NODE_SSH" "$@"
}
# -l root on a bench-device's host mode: the driving key is in root's authorized_keys and a written system's host key regenerates every time.
m_ssh_opts() {
    [ "${NODE_ROLE:-}" = bench-device ] || return 0
    printf '%s' "-l root $(_unpinned_host_key_opts)"
}

m_reachable() { m_ssh true >/dev/null 2>&1; }

# The driver interface is lib/wk/boot; every b_* here and in boot/pi-*.sh is its shim over this shell's NODE_* and MODE.
_wk_boot() { # <driver> <verb> [args]
    ( export NODE_NAME ${!NODE_*} MODE MODE_CHANNEL
      PYTHONPATH="$WK_ROOT/lib" exec python3 -m wk.boot "$@" )
}

boot_facts() { # <driver> -- BOOT_ARMING and the other facts a bash caller reads after load_driver
    local f
    f=$(_wk_boot "$1" facts) || { echo "boot driver $1: python3 -m wk.boot $1 facts failed" >&2; return 1; }
    eval "$f"
}

_b_probe_sh=$(cat "$WK_ROOT/boot/onboard/probe.sh")

b_probe() {
    local _o
    _o=$(_wk_boot "${NODE_DRIVER:-}" probe) || _o="MODE=unreachable MODE_CHANNEL=none"
    eval "$_o"
}
b_probeable() { _wk_boot "${NODE_DRIVER:-}" probeable; }
b_system_kind() { _wk_boot "${NODE_DRIVER:-}" system-kind "${1:-}"; }
b_display() { _wk_boot "${NODE_DRIVER:-}" display; }
b_media() { _wk_boot "${NODE_DRIVER:-}" media; }
b_boot_id() { _wk_boot "${NODE_DRIVER:-}" boot-id; }
b_systems() { _wk_boot "${NODE_DRIVER:-}" systems; }
b_reboot() { _wk_boot "${NODE_DRIVER:-}" reboot || exit $?; }
record_write() { _wk_boot "${NODE_DRIVER:-}" record-write "$@"; }
record_read() { _wk_boot "${NODE_DRIVER:-}" record-read; }
machine_armed_barrier() { b_probe; _wk_boot "${NODE_DRIVER:-}" barrier "$1" || exit $?; }   # the probe sets this shell's channel for what follows

