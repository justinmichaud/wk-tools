#!/usr/bin/env bash
# cmd/sysimage's arm not yet ported -- disks -- as cmd/sysimage runs them (lib/wk/shell.py sysimage_arms).

set -euo pipefail
WK_ROOT="${WK_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/image.sh"
. "$WK_ROOT/image/profiles.sh"
. "$WK_ROOT/boot/machines.sh"
. "$WK_ROOT/lib/target.sh"
. "$WK_ROOT/lib/resources.sh"
. "$WK_ROOT/boot/disk.sh"

cmd_disks() {
    local machine="${1:-}"
    [ -n "$machine" ] || die "usage: wk sysimage disks <machine>
    machines:
$(machine_list | sed 's/^/      /')"
    machine_load "$machine" || die "unknown machine '$machine'"
    m_reachable || die "$machine is not reachable over ssh"
    log "removable disks attached to $machine:"
    disk_list
    log ""
    log "  write one with:  wk sysimage write --from <path> --disk $machine:<device>"
    log "  ('wk sysimage ls' prints the paths)"
    log "  a machine's own system disk is never listed and never writable."
}

if (return 0 2>/dev/null); then
    [ "${1:-}" = functions ] && return 0
fi

case "${1:-}" in
    disks) shift; cmd_disks "$@" ;;
    *) die "usage: wk sysimage <sub> [args]; see wk sysimage -h" ;;
esac
