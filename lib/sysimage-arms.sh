#!/usr/bin/env bash
# cmd/sysimage's arms not yet ported -- the yocto and pmos builders and disks -- as cmd/sysimage runs them (lib/wk/shell.py sysimage_arms).

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
. "$WK_ROOT/image/yocto.sh"
. "$WK_ROOT/image/pgo.sh"
. "$WK_ROOT/image/pmos.sh"

# lib/wk/sysimage/cli.py has refused what cannot be built; the profile is loaded again for the bash builders' globals, and the machine half of the spec routed this here (wk).
_arm_load() {  # <spec>
    image_profile_load "$(image_spec_profile "$1")" || die "unknown profile '$1'.
    'wk sysimage --list' has every configuration."
}

cmd_yocto() {
    _arm_load "${1:-}"; shift
    load_target "${WK_TARGET:-$(default_target)}"   # a resolved workspace target first, so the builder can refuse a remote or VM one
    yocto_build "$IMG_PROFILE" ${WK_DRY_RUN:+--dry-run} "$@"
}

cmd_pmos() {
    _arm_load "${1:-}"; shift
    pmos_build "$IMG_PROFILE" ${WK_DRY_RUN:+--dry-run} "$@"
}

# 2.52 onwards the perf build *is* the profile-guided one, so one command is still one slot and three phases sit behind it (image/pgo.sh). The spec, not the profile: the cycle's own steps are commands, and each names the lane the way this one was named.
cmd_yocto_webkit() {
    local spec="${1:-}"
    _arm_load "$spec"; shift
    load_target "${WK_TARGET:-$(default_target)}"
    if image_pgo_wanted; then image_pgo_webkit "$spec" ${WK_DRY_RUN:+--dry-run} "$@"
    else yocto_build "$IMG_PROFILE" --stage webkit ${WK_DRY_RUN:+--dry-run} "$@"; fi
}

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
    yocto) shift; cmd_yocto "$@" ;;
    pmos) shift; cmd_pmos "$@" ;;
    yocto-webkit) shift; cmd_yocto_webkit "$@" ;;
    disks) shift; cmd_disks "$@" ;;
    *) die "usage: wk sysimage <sub> [args]; see wk sysimage -h" ;;
esac
